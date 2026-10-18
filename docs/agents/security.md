# Agent security model

What the agent-dispatch core enforces, why, and what it deliberately does not.
Citations are `RESEARCH.md` item ids. Every rule below has a test under
`backend/tests/agents/` (gate G17); the test file is named at the end of each
section.

An agent is a process that can read and write files and run commands on the
owner's machine, guided by text that may come from the internet. Treat the agent as
untrusted code with the owner's privileges, and the repo it works on as untrusted
input.

## 1. Isolation of the host's own configuration

The agent CLI behind the Claude backend reads configuration from many places by
default. The adapter closes each one explicitly rather than trusting defaults
(R-ISO: "do not rely on default options").

| risk | control | cite |
|---|---|---|
| The user's own `~/.claude` hooks, MCP servers, plugins and skills run inside the task | `setting_sources=[]` is **always passed**; omitting it means user + project + local (F4, C1) | §5 item 6 |
| A repo's `.claude/settings.json` hooks and `.mcp.json` servers execute with no prompt in non-interactive runs | `setting_sources=[]` plus `--strict-mcp-config` with an explicit `mcp_servers` dict | F6b, R-MCP |
| `~/.claude.json`, managed policy settings, auto-memory and claude.ai connectors are read regardless of `setting_sources` | a private `HOME`/`USERPROFILE` per task, `CLAUDE_CONFIG_DIR=<data>/agents/<task>/.claude`, `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` | R-ISO |
| The worker inherits the owner's environment (cloud keys, tokens) | the worker gets an **allowlisted** environment (PATH and a few OS variables), not a scrubbed copy | §5 item 5 |
| A hostile cwd shadows Python imports in the worker | the worker runs `python -P -m ...` with its own working directory outside the untrusted workspace | - |

`trust_repo_settings` is the single opt-in (`WorkspacePolicy`). When true the Claude
adapter passes `setting_sources=["project"]` and logs a warning, and **the repo's
hooks execute** with the agent's privileges. `.mcp.json` servers still do not load
(strict MCP stays on). Enable it only for repositories the owner wrote and trusts;
the UI must say so.

Tests: `test_ag_claude_options.py` (the hostile fixture repo
`tests/fixtures/agents/malicious_repo/` has a `PreToolUse` and `SessionStart` hook that
write marker files, an `.mcp.json` server that writes a marker, and a prompt-injecting
instructions file; the test puts it in a real git worktree, asserts the files really
are in the agent's cwd, and asserts the options would load none of them and that no
marker exists afterwards), plus `test_ag_cli_adapter.py` for the M3 preset (`--bare`,
`--setting-sources ""`).

## 2. Permissions and approvals

- The permission mode is always explicit (the default can silently become `auto` on
  third-party providers - R-PM, §5 item 5/7). Interactive agents use `default` with a
  `can_use_tool` callback wired to approval cards; unattended agents `dontAsk`;
  investigate-only agents `plan`. **`bypassPermissions` and `auto` cannot be expressed**
  - `ApprovalPolicy.mode` has three values and the plan builder asserts the result.
- The SDK checks hooks, deny rules, ask rules, the mode, allow rules and only then
  `canUseTool` (F5). A tool listed in `allowed_tools` therefore never reaches the
  callback. The adapter keeps every tool that needs approval *out* of `allowed_tools`,
  and disallows by bare name every built-in tool the agent was not given (§5 item 7).
  Do not combine `bypassPermissions` with an allowlist expecting a restriction.
- Destructive tools always raise `approval_request`: the default `always_ask` list
  covers shells, file writers and MCP tools named like delete, remove, admin, exec,
  send, post, create or update. For those, `scope="task"` is neither offered nor
  honoured, so a single "always allow" click cannot turn into blanket approval.
- Unattended agents cannot ask. A spec that gives one a tool needing approval is
  rejected at construction unless an admin names the tool in `unattended_allow`.
- Approval ids are single use and checked; an interrupt, cancel or budget stop denies
  every pending request of that task.
- Not done yet: path-scoping and Bash allow-listing as a `PreToolUse` hook, which is
  where policy that must apply to *every* call belongs (§5 item 8). Until it exists the
  boundary for allowed tools is the workspace plus the allowlist.

Tests: `test_ag_interface.py`, `test_ag_fake.py`, `contract/test_ag_contract.py`
(approval round trip, deny, single use, destructive scope), `test_ag_claude_options.py`.

## 3. Quarantine: readers do not hold privileged tools

An agent that reads untrusted content (web pages, email, uploads) must not also hold
high-privilege tools; split the work into a reader and an actor with an approval gate
between them (brief). `AgentSpec` enforces it on construction
(`branding.quarantine_violations`):

- `reads_untrusted_content=True` plus any high-privilege tool, or a privileged MCP
  server, is rejected;
- holding a web tool (`WebFetch`, `WebSearch`, ...) *implies* untrusted content even if
  the author left the flag false;
- a spec may add its own high-privilege patterns.

The rule is conservative: a false rejection is fixed by splitting the agent, a false
acceptance is a prompt-injection path to a shell.

Test: `test_ag_interface.py`.

## 4. Budgets and runaway control

- Every run passes `max_budget_usd` (90% of the remaining budget, because the SDK's cap
  is client-side and can overshoot) and `max_turns`; the adapter re-checks the reported
  total at 100% (R-COST, §5 item 9).
- A resumed session restores its earlier totals but they do not count toward the new
  run's cap, so the adapter tracks the task's lifetime total and passes only the
  remainder.
- Wall-clock is enforced by a watchdog that interrupts the run and fails it with
  `budget_exceeded`; the clock pauses while a human decides an approval and is
  cumulative across resumes. A token cap exists for backends that report no money.
- Subagent fan-out is capped through `CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH` and
  `CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS` (the SDK enforces the spawn cap from CLI 2.1.217;
  R-COST).
- The budget stop is a hard stop and is *not* resumable. Per-day limits need a spend
  ledger the integration layer owns (`spent_today_usd`).
- All costs are labelled estimates; never bill from them.

Tests: `test_ag_interface.py`, `contract/test_ag_contract.py` (budget stop, wall clock),
`test_ag_claude_backend.py` (soft overshoot, max-turns stop, remainder on restore).

## 5. Secrets

- Credentials: an API key or Bedrock/Google/Foundry credentials only (F2, §5 item 1).
  A subscription OAuth token is never forwarded: it is blanked in the SDK environment,
  removed from the worker's allowlisted environment, and `extra_env` cannot carry it.
- Storage: `secrets.py` - the OS keyring if one is usable, else a JSON file created
  with mode 0600 under the data dir, never `config.yaml`. The environment overrides
  for reads.
- Transport: the key reaches the worker **on its stdin pipe**, not in argv and not in
  the worker's own environment, and is injected into the SDK options in-process.
  The generic CLI preset (M3) cannot do this: `--bare` requires the key in the CLI's
  environment (F6a).
- Redaction: every string of every event is scrubbed before it is stored (registered
  secrets, plus anything shaped like an Anthropic key); the agent loggers carry a
  redacting filter that also scrubs exception text; support-bundle dumps
  (`support_dump()`) omit prompts, instructions and environment values.
- Known limit: on Windows `chmod 0600` only toggles the read-only bit, so the secrets
  file relies on the ACL inherited from the per-user data directory. Prefer the keyring
  there. Another limit: enabling DEBUG logging for the `websockets` library logs frame
  heads; do not do that in production.

Tests: `test_ag_no_secret_leaks.py` seeds a fake key and checks events, logs, exception
text, task info, a support-style dump and every file written under the data dir for
both the Claude and OpenClaw adapters, over scenarios that echo the key through tool
output, assistant text and tool-call arguments; `test_ag_secrets.py`;
`test_ag_claude_options.py` (the key is only in the pipe-bound field).

## 6. Workspaces and escapes

- One workspace per task under `<data>/agents/<task_id>/`; the task id is a strict token
  so it cannot walk out.
- A task that targets a repo gets a **detached git worktree**; the repo's own hooks are
  neutralised while it is created (`core.hooksPath` -> an empty directory). Cleanup uses
  `git worktree remove` + `prune` (§5 item 19: runs never auto-clean).
- `resolve_inside(root, relative)` is the only way agent-supplied paths become real
  paths. It rejects absolute paths, `..`, drive and stream syntax on Windows, and
  anything that resolves outside the root after symlinks and junctions are followed.
  Artifact events report only paths inside the workspace.
- Not done yet: an optional container or distrobox sandbox for Bash (brief). Today a
  shell tool, once approved, runs with the owner's privileges inside the workspace cwd
  and is *not* confined to it.

Tests: `test_ag_workspace.py` (traversal, absolute, Windows drive forms, directory and
file symlinks/junctions, worktree lifecycle, repo hooks not run),
`test_ag_claude_backend.py` (artifact paths).

## 7. Process control

- One worker per task, in its own process group. A normal stop closes its stdin so the
  SDK disconnects cleanly; the first escalation is a console break / `SIGINT`, the last
  a by-PID tree kill. Nothing kills by image name (a past incident on this machine).
- A turn is interrupted with the SDK's `interrupt()`. A bare `SIGTERM` exits 143 and
  drops the turn (F6d), so it is never used mid-turn.

Tests: `test_ag_claude_worker.py` (real subprocess, graceful close, escalation).

## 8. Branding and disclosure

The backend is labelled "Claude Agent", or "Claude" inside a menu labelled "Agents", or
"<Name> Powered by Claude". The Anthropic CLI product's name, its "Agent" form, the
Cowork product names and their visuals are not used, and nothing may look like an
Anthropic product (F3). The proposed product label is "DisPatch Agent, Powered by
Claude" because Anthropic ships a feature called Dispatch (F9, C5). `branding.py` holds
the rules and `test_ag_branding.py` scans every locale file, the agents package and
these docs.

Operator duties that are not code (R-UP, R-CT, §5 item 4): disclose AI in the consumer
UI, tell users outputs may be wrong, require human review for high-risk advice, remember
that the operator is responsible for the agent's tool actions, do not resell. The Usage
Policy date in RESEARCH.md C3 needs a counsel re-read.

## 9. Residual risks (read this)

1. **Message shapes are from documentation.** No real run was possible; the first live
   run may reveal a field or subtype the fakes do not model. The tolerant reader ignores
   unknowns but a renamed required field would surface as an empty result.
2. **Private HOME is untested live.** Relocating `HOME` isolates `~/.claude.json` (R-ISO)
   but may break something the bundled CLI expects; verify on the first live run.
3. **Approved shell commands are not sandboxed.**
4. **`trust_repo_settings` executes the repo's hooks.**
5. **Transcripts are plaintext on disk** (R-SESS). Retention is the integration layer's
   job.
6. **OpenClaw approvals and budgets are weaker** than the Claude backend's (no approval
   round trip; token/turn caps are post-hoc). Capabilities say so.
7. **Windows secrets file** depends on directory ACLs (section 5).
