# Agent backends

This is the reference for the agent-dispatch core in `backend/app/agents/`: the
`AgentBackend` interface, the normalized events every adapter emits, and each
adapter. Every design decision cites `RESEARCH.md` (the 2026-10-18 look-up pass) by
item id: `F*` facts, `R-*` extra facts, `C*` conflicts, and `§5 item N` for the
"implications the plan must honor".

Status: the core is a self-contained library. It is **not yet wired** into the
app (routes, database, WebSocket frames, UI): that is the next workstream
(see "What the integration layer must supply" at the end).

## Concepts

- **Agent** - a configured worker: `AgentSpec` (backend, model, instructions,
  tool allowlist, MCP servers, workspace policy, approval policy, budget,
  `required_permission`). Defined in `interface.py`.
- **Task** - one dispatched job: a timeline of events, artifacts, cost and a
  state (`queued`, `running`, `awaiting_approval`, `succeeded`, `failed`,
  `cancelled`).
- **Run** - one `start_task` / `resume` / follow-up `send`. A task has one or
  more runs; each ends with exactly one `result` event.
- **Session** - the backend's own conversation id. It is what makes interrupt
  followed by resume continue the same conversation (R-SESS, §5 item 10).

## The interface

```python
class AgentBackend(Protocol):
    name: str
    def capabilities(self) -> Capabilities
    async def start_task(self, spec: AgentSpec, prompt: str, ctx: TaskContext) -> str   # task_id
    async def send(self, task_id, message) -> None        # steer a running task, or follow up an idle one
    def events(self, task_id, *, after_seq=-1) -> AsyncIterator[Event]
    async def respond_approval(self, request_id, decision: "allow"|"deny", scope: "once"|"task") -> None
    async def interrupt(self, task_id) -> None            # stop the current turn; resumable
    async def resume(self, task_id, message=None) -> None # continue the SAME session
    async def cancel(self, task_id) -> None               # terminal
    async def list_sessions(self, ctx=None) -> list[SessionInfo]
    def task_info(self, task_id) -> TaskInfo
    async def aclose(self) -> None
```

`BaseBackend` (same file) implements everything that must not differ between
adapters: the task registry, the event log, approvals, the wall-clock watchdog,
and the single definition of how a run ends. An adapter supplies `_run`,
`_do_interrupt`, optionally `_do_steer`, `_prepare` and `_release`.

### Run and stream contract

Asserted for every adapter by `backend/tests/agents/contract/`:

1. `seq` starts at 0 and increases by exactly 1 per event, **across runs**. It is
   the replay cursor: `events(task_id, after_seq=n)` continues where a consumer
   stopped. A finished task replays identically.
2. A run starts `status(queued)` (first run only), `status(running)`, and ends
   with exactly one `result`; the iterator stops after it. If nothing is running
   the iterator ends instead of waiting.
3. `result.state` is `succeeded`, `failed` or `cancelled`. `failed` is always
   preceded by an `error` event with a catalogued code, repeated in
   `result.reason`.
4. `interrupt()` ends the run `cancelled`, `reason="interrupted"`,
   `resumable=True`. `cancel()` ends it `cancelled`, `reason="cancelled"`,
   `resumable=False`. Both honour (§5 item 11, F6d): the Claude backend uses the
   SDK's `interrupt()`, never a signal mid-turn.
5. A budget stop is `cost_update` then `error(budget_exceeded)` then
   `result(failed, reason="budget_exceeded", resumable=False)`.
6. Costs are estimates: `CostUpdateEvent.estimate` is the literal `True` (F6e,
   R-COST, §5 item 9). Never bill from it.

### Events (`events.py`, pydantic)

Every event has `task_id`, `ts` (unix seconds) and `seq`.

| type | fields beyond the common ones |
|---|---|
| `status` | `state`, `detail` |
| `text_delta` | `text`, `replace` (the text replaces everything streamed so far), `parent_id` (set for subagent output) |
| `tool_call` | `call_id`, `name`, `input`, `parent_id` |
| `tool_result` | `call_id`, `ok`, `output` (clipped to 8000 chars), `name`, `parent_id` |
| `approval_request` | `request_id`, `tool_name`, `input`, `call_id`, `reason`, `scopes` (`["once","task"]`, or `["once"]` for destructive tools) |
| `subagent_started` / `subagent_finished` | `subagent_id`, `parent_id` (None = child of the main agent, so the UI can draw a tree), `name`/`description` or `ok`/`summary` |
| `retry` | `attempt`, `max_attempts`, `delay_s`, `reason`, `code` |
| `artifact` | `artifact_id`, `kind`, `name`, `path` (relative to the task workspace, never absolute), `mime`, `size` |
| `cost_update` | `estimate` (always true), `total_cost_usd` (None if the backend reports no money), `input_tokens`, `output_tokens`, `turns`, `source` |
| `result` | `state`, `reason`, `text`, `session_id`, `resumable`, `num_turns`, `duration_ms` |
| `error` | `code`, `message`, `retryable`, `detail` |

Reasoning ("thinking") text is never forwarded as an event. Every string in every
event passes through the secret redactor before it is stored (§5 item 5; see
`security.md`).

### Error catalogue (`ErrorCode`)

`backend_unavailable` (retryable), `backend_protocol_mismatch`, `auth_failed`,
`rate_limited` (retryable), `timeout`, `refused`, `invalid_request`,
`budget_exceeded`, `mcp_error`, `sdk_missing`, `workspace_violation`,
`policy_violation`, `not_supported`, `backend_error`, `internal`. Every adapter
maps every failure onto one of these; an unexpected exception becomes `internal`.

### Capabilities

`capabilities()` says what the backend can do so the UI and the contract suite
do not guess: approvals, steering a running turn, subagents, cost reporting
(`usd` / `tokens` / `none`), budget enforcement (`hard` / `post_hoc` / `none`),
whether it needs the same host, whether prompts leave the host, and the
permission modes it offers. A missing capability is explicit: the call fails with
`not_supported`, never silently.

### Approvals

Per tool in the allowlist there are three tiers (`ApprovalPolicy`):

| tier | matches | behaviour |
|---|---|---|
| `auto` | neither list | runs without asking |
| `ask` | `ask_tools` | `approval_request`; the approver may answer `scope="task"` to stop being asked for this task |
| `always_ask` | `always_ask` (default: Bash, PowerShell, Write, Edit, MultiEdit, NotebookEdit and MCP tools named like delete/remove/admin/exec/send/post/create/update) | `approval_request` **every time**; `scope="task"` is neither offered nor honoured |

A tool not in the allowlist is denied without asking. `plan` agents are
investigate-only. `unattended` agents cannot ask, so a spec that gives one a tool
needing approval is rejected unless an admin lists it in `unattended_allow`.
While a request is pending the task is `awaiting_approval` and the wall-clock
budget is paused (a human's think time is not the agent's).

## Adapters

### Fake (`fake.py`)

A scriptable backend: `start_task(spec, "scenario:<name>", ctx)` plays a script
(`simple`, `tools`, `approval`, `subagent`, `retry`, `artifact`, `long`, `budget`,
`error_*`, `crash`). It is the reference implementation of the contract and what
the integration layer's route and UI tests should drive.

### OpenClaw (`openclaw_adapter.py`)

Wraps the existing transports, unchanged: the gateway WebSocket client
(`app/gateway_ws.py`, protocol 4 - R-OC1) and the CLI fallback
(`app/openclaw.py::send_to_agent`, which gained one optional keyword,
`on_spawn`, so a cancel can reach the process).

| gateway frame | normalized |
|---|---|
| `chat` state `delta` (cumulative snapshot) | `text_delta` (diffed; a non-extension is `replace=True`) |
| `chat` state `status` | `status(running, detail=<phase>)` |
| `session.tool` / `agent` stream `tool`, phase `start` | `tool_call` |
| the same, a terminal phase (`end`/`result`/`done`/`error`) | `tool_result` - the repo only handles `start` today, so this mapping is an assumption (R-OC3 is unverified upstream) |
| `session.message` assistant | `text_delta`, only when no `chat` delta arrived for the run; a body ending in the truncation marker is refetched in full first (the 8000-character hazard, §5 item 26) |
| final response to the `agent` request | `result` (+ token `cost_update`) |
| `chat.abort` | the transport of `interrupt` / `cancel` |

Decisions and limits:

- **Protocol probe.** Before the first task one handshake runs and the reported
  protocol is compared with `gateway_ws.PROTOCOL` (4). A mismatch - another number,
  a missing number, a `1002/PROTOCOL_MISMATCH` close, or a hello frame without the
  methods/events DisPatch depends on - fails the task loudly with
  `backend_protocol_mismatch`; it is never retried and never masked by the CLI
  fallback (§5 item 26). The mismatch close code is documented only in the repo,
  not upstream (R-OC1).
- **Same host.** `same_host_required=True`. The CLI transport passes a file path and
  transcripts/media are read from the agent's home; the gateway socket alone does
  not carry them (audit section N, §5 item 26). OpenClaw configuration is never
  edited.
- **Session identity.** `session_id` is the gateway session *key*
  (`agent:<agent>:<task>`), which is stable across the gateway minting a new
  internal session id on rewind. Resume sends to the same key.
- **No approvals, subagents or steering.** OpenClaw applies its own tool policy
  inside the agent. `respond_approval` raises `not_supported`.
- **Budgets are tokens and wall clock only.** OpenClaw reports tokens, not money.
  Token and turn caps are checked after a turn finishes (`post_hoc`); the wall-clock
  cap aborts the run.
- **Auth.** The gateway token (`OPENCLAW_GATEWAY_TOKEN`, or the secret store, or
  `gateway.auth.token`); the gateway has no loopback exemption.
- **Reachability.** `transport="auto"` falls back to the CLI only when the gateway
  is *unreachable*; auth failures and protocol mismatches fail loudly.
- The HTTP OpenAI-compatible surface (R-OC2) is not used: it is off by default and
  grants full operator access.

### Claude Agent, local (`claude_sdk_adapter.py`) - milestone M1

One supervised worker process per task runs `ClaudeSDKClient` in streaming mode
(so `interrupt()` works; `query()` cannot - §5 item 11). The parent never imports
the SDK; it talks to the worker over newline-delimited JSON on stdin/stdout through
a small `SdkTransport` protocol (which the tests fake). The SDK is an optional,
exactly pinned extra: `uv sync --extra agents-claude`
(`claude-agent-sdk==0.2.168`, §1 item 6, §5 item 6).

Options the adapter always sets (all asserted by tests; the reasoning is in
`security.md`):

| option | value | source |
|---|---|---|
| `setting_sources` | `[]`; `["project"]` only if the agent's workspace policy sets `trust_repo_settings` | F4, C1, §5 item 6 |
| `extra_args` | `--strict-mcp-config`, with an explicit `mcp_servers` dict | F6b, R-MCP |
| `permission_mode` | `default` (approval cards), `dontAsk` (unattended), `plan` (investigate only); `bypassPermissions` and `auto` are not representable | F5, R-PM, §5 item 7 |
| `allowed_tools` / `disallowed_tools` | tools needing approval are *not* in `allowed_tools` (allow rules precede `canUseTool`); every built-in tool not granted is disallowed by bare name | F5, §5 items 7-8 |
| `max_budget_usd` / `max_turns` | on every run; the USD cap is 90% of the budget and only the *remaining* budget on resume | R-COST, §5 item 9 |
| `env` | `CLAUDE_CONFIG_DIR=<data>/agents/<task>/.claude`, `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`, subagent depth and concurrency caps, `CLAUDE_AGENT_SDK_CLIENT_APP=DisPatch`, a private `HOME`, `CLAUDE_CODE_OAUTH_TOKEN` blanked | R-ISO, §5 items 2, 5, 9, 10 |
| `can_use_tool` + a dummy `PreToolUse` hook | in `default` and `plan` modes; the hook is the documented workaround that keeps the stream open so the callback can fire | R-CUT |
| `include_partial_messages` | true | F6e |

Behaviour:

- **Approvals.** The worker forwards every `can_use_tool` call to the parent, which
  decides (`decide_permission`): not allowlisted -> deny; `auto` -> allow; otherwise
  `approval_request`, then the answer travels back to the worker. A pending request
  can wait indefinitely; interrupt, cancel and budget stops deny it.
- **Policy for every call** belongs in a `PreToolUse` hook, not `can_use_tool`
  (auto-approved tools never reach the callback - F5, §5 item 8). The adapter does
  not add path-scoping hooks yet; the workspace sandbox and the allowlist are the
  boundary today.
- **Cost** comes from `ResultMessage.total_cost_usd`, labelled an estimate; the
  adapter re-checks it against 100% of the cap because the SDK can overshoot (R-COST).
  `error_max_budget_usd` and `error_max_turns` become `budget_exceeded`.
- **Sessions.** `session_id` is taken from `system/init` and every result and is
  persisted by the integration layer; `resume` restarts a dead worker with
  `resume=<session_id>` (R-SESS). `list_sessions` reads transcript file names under
  `<data>/agents/*/.claude/projects/` (plaintext JSONL - say so in the UI).
- **Failures.** An MCP/plugin start-up error fails the run (§5 item 14); worker and
  SDK errors map to `backend_unavailable`, `sdk_missing`, `backend_error`; assistant
  error kinds map to `auth_failed`, `rate_limited`, `refused`, `backend_unavailable`.
  Unknown message kinds and subtypes are ignored (a tolerant reader - §5 item 14).
- **Process control (Windows and POSIX, §5 item 15).** The worker starts in its own
  process group (`CREATE_NEW_PROCESS_GROUP` / `start_new_session`), with a minimal
  allowlisted environment, the module run with `python -P` and its working directory
  *outside* the untrusted workspace. A normal stop closes its stdin; only after a
  grace period does the transport escalate (Windows: `CTRL_BREAK_EVENT`, then a
  by-PID `taskkill /T`; POSIX: `SIGINT`, then `SIGKILL` of the group). It never kills
  by image name.
- **Workspace.** `<data>/agents/<task>/work` (scratch, or a detached git worktree
  when the task targets a repo - the product, not the SDK, removes it: §5 item 19).

Message shapes in the tests are *derived from the documentation* (F6e, R-SESS,
R-COST); no recorded real run exists because no API key was available. The first live
run must be diffed against `tests/agents/agent_fakes.py`.

### Claude Managed Agents, hosted - milestone M2 (not implemented)

Facts the M2 adapter must respect (all VERIFIED in RESEARCH.md; the feature is in
beta and must be opt-in behind a flag, §5 item 20):

- Beta header `managed-agents-2026-04-01` (set by the official SDK); concepts Agent,
  Environment, Session, Events; SSE stream; steer and `user.interrupt` (F7a, R-MA4).
  Open the event stream **before** sending events (R-MA4).
- Environments: an Anthropic cloud sandbox, or a self-hosted worker (`ant beta:worker
  poll`, Linux only). The environment key is created only in the Console; the API key
  stays off the worker host; self-hosted sessions accept only `memory_store`
  resources; `networking` omitted means *unrestricted*, so set it explicitly (F7b,
  F7c, R-MA2).
- **Retention.** Not eligible for ZDR or HIPAA; transcripts persist server-side until
  deleted with `DELETE /v1/sessions/{id}` (the session must be idle). The setup screen
  and the privacy docs must say so and offer "Delete remote session" (F7e).
- Rate limits: create 300/min, read 1,200/min per organization (R-MA1).
- Private MCP servers need MCP tunnels (research preview, Cloudflare transport) or
  worker-served custom tools (F7d). Scheduled deployments run on Anthropic's
  infrastructure (R-MA3).

### Generic CLI agent (`cli_adapter.py`) - milestone M3 (stub)

`GenericCliBackend.start_task` raises `NotImplementedError("M3")`. Two pieces ship now
and are tested: `build_claude_cli_command(...)`, the preset command line (§5 item 13),
and `parse_stream_json_line(...)`, a tolerant reader (§5 item 14). The preset is
`claude --bare -p --output-format stream-json --verbose --include-partial-messages
--permission-mode <mode> [--permission-prompts none] --allowedTools <list>
--mcp-config <file> --strict-mcp-config --setting-sources "" --max-turns N
--max-budget-usd X (--no-session-persistence | --resume <id>)`. `--bare` needs
`ANTHROPIC_API_KEY` in the CLI's environment (F6a). An empty `--setting-sources ""`
was not tested upstream (§5 item 13): if it is rejected the run fails closed.

## Authentication options

Only these (F2, F2b, §5 items 1-2):

1. an **Anthropic Console API key**;
2. **Amazon Bedrock**, **Google Cloud (Agent Platform)** or **Microsoft Foundry**
   credentials, or **Claude Platform on AWS** (`CLAUDE_CODE_USE_ANTHROPIC_AWS=1`).

Never a claude.ai login, an OAuth token (`CLAUDE_CODE_OAUTH_TOKEN` is blanked in the
SDK environment and removed from the worker's), or anyone's subscription limits,
unless Anthropic has approved it. The API key is read from `secrets.py` (the OS
keyring if usable, else a 0600 file under the data dir; `ANTHROPIC_API_KEY` in the
environment overrides) and handed to the worker over its stdin pipe. The adapter's
`auth_route` selects the provider switch; only the API-key route is covered
end to end by tests.

## What is sent where

| backend | what leaves the process | where to |
|---|---|---|
| fake | nothing | - |
| OpenClaw | the user's prompt and file paths | the local OpenClaw gateway (loopback) or CLI; whatever model provider the OpenClaw agent is configured with then sees it |
| Claude Agent (local) | prompts, tool results, file contents the agent reads, MCP tool output | Anthropic's API (or the chosen cloud provider) from the worker; the workspace and transcripts stay on this machine |
| Managed Agents (M2) | the same, and the session history is stored there | Anthropic's platform, until deleted |

The `Capabilities.data_leaves_host` flag carries the first-order answer for the UI.
Support bundles exclude prompts and instructions by default.

## Retention notes

- Local Claude sessions are plaintext JSONL under the per-task `CLAUDE_CONFIG_DIR`
  (R-SESS). Deleting a task's directory deletes its transcript and memory.
- OpenClaw keeps its own transcripts in the agent's home.
- Managed Agents: see above - server-side, until deleted.
- DisPatch's own task/event retention is configurable by the integration layer
  (brief); the event log in this library is in-memory and not persistent.

## What the integration layer must supply

See the workstream report; in short: a `tasks` and `task_events` table (persist each
event by `(task_id, seq)` and call `restore_task` at start-up), routes behind the
`dispatch_agents` / `approve_agent_actions` / `manage_agents` permissions, WebSocket
frames for task events and approvals (and the locked-socket allow-list), a per-day
spend ledger feeding `Budget.max_usd_per_day`, a keyring-backed key setup screen, and
the Tasks UI.

## Testing

```
cd backend
uv sync --frozen --python 3.12
uv run --frozen --python 3.12 pytest tests/agents -q -p no:cacheprovider
```

The suite is hermetic: loopback only, no SDK, no key. `AGENT_LIVE_TESTS` is `off`
for the whole suite (the autouse guard in `tests/agents/conftest.py` sets it); live
runs, when enabled by an owner, must stop at `AGENT_TEST_BUDGET_USD` (§5 item 9).
