# Agents and tasks

DisPatch Chat can hand real work to an AI agent: read a repository and propose a change,
research a question, run a long job on this machine. That is **entirely optional**. With no
agent configured DisPatch is a complete chat — threads, files, search, media, the PWA — and
nothing below is switched on.

> **Just want something to talk to?** You do not need any of this. **Connect an AI** (the
> 🔌 button in the left rail) points a character straight at an LLM provider's HTTP API — LM
> Studio, Ollama, OpenAI, Anthropic, or anything OpenAI-compatible — with no agent runtime
> installed. See [llm-providers.md](llm-providers.md). This page is about the *other* kind of
> backend: one that can use tools and act on a machine.

## The model in one screen

| Concept | What it is |
|---|---|
| **Agent** | a configured worker: a *backend*, a model, instructions, a tool allow-list, MCP servers, a workspace policy, an approval policy and a budget. An Admin defines it (Settings → AI & Connections → Agents) and *publishes* it to the profiles that may use it. |
| **Task** | one dispatched job: a live timeline (what the agent said and did, subagents nested), approvals, artifacts, an **estimated** cost, and one state — `queued`, `running`, `awaiting_approval`, `succeeded`, `failed`, `cancelled`. |
| **Backend** | what actually runs it. Two ship: **OpenClaw** (the gateway or CLI on this machine) and **Claude Agent** (the Anthropic agent SDK, one supervised process per task). Both speak one interface (`AgentBackend`, [agents/backends.md](agents/backends.md)). |
| **Approval** | the agent stops and asks before a tool that changes things. You allow it once, allow it for the rest of the task (never for a destructive tool), or deny it. |

The three permissions (see [security.md](security.md)):

| Permission | Lets you | Admin | User | Basic |
|---|---|---|---|---|
| `manage_agents` | define agents, store the Claude key, set per-agent budgets and retention | yes | **never** | **never** |
| `dispatch_agents` | assign tasks to the agents published to you, watch, steer, stop | yes | off, an Admin may switch it on | off, an Admin may switch it on |
| `approve_agent_actions` | answer approval cards; Send a held hand-off | yes | off, an Admin may switch it on | off, an Admin may switch it on |

**Agent bots can run commands on this computer, including reading anyone's chats**, so these two
permissions are off for Users and Basic profiles until an Admin switches them on (the screen says
so plainly), and they are never effective on the profile an unsigned device lands on (the
shared DisPatch profile). Without `dispatch_agents` every route answers 403 (listing included)
and the WebSocket frames are never sent. A profile that has it can use only agents an Admin has
published to it; an agent that was not published answers "not found", exactly like one that does
not exist. **A task is its dispatcher's own: everyone, Admins included, sees only their own tasks**
(an Admin keeps the aggregate spend and the audit log).

## Using it

- **Assign task** — in a chat, open ⋯ → *Assign task…*, or type `@agent-name do the thing` at
  the start of a message (`@code-reviewer` finds "Code Reviewer"). The mention opens the same
  dialog, filled in; **nothing runs until you press Assign**. The dialog says where the prompt
  goes (a Claude Agent task sends what it reads and writes to Anthropic's API; an OpenClaw task
  stays on this machine) and that it will ask before changing anything.
- A task starts from one of your own chats: the prompt, approval requests and the result are
  written into that chat, so you can only start one from a chat you own and a character you may use
  (anything else is a 404). From the Tasks page there is no chat.
- The task's **card** appears in the chat and on the **Tasks** page (the 📋 button in the left
  rail, with a dot while an approval waits for you). It shows the timeline, tool calls that fold
  open, approval cards inline, artifacts, the cost labelled *estimate*, and the controls:
  **Stop** (resumable), **Resume**, a **steer** box while it works and a **follow-up** box when
  it is idle, and **End task** (for good).
- The Tasks page filters by state, agent and date.
- You are told when a task needs approval, finishes or fails: a Web Push ping with no content
  (see [privacy.md](privacy.md)), a collapsed notice line in the originating chat, and a
  badge on the rail.

## Setting it up (Admin)

**Settings → AI & Connections → Agents.**

1. **Backends** shows what can run here and what each can and cannot do. *OpenClaw* needs
   OpenClaw installed on this machine. *Claude Agent* needs the optional extra
   (`uv sync --extra agents-claude`) and credentials: an **Anthropic Console API key**, or
   **Amazon Bedrock**, **Google Cloud** or **Microsoft Foundry** credentials already set up on
   this machine. The key is write-only: it is stored in the OS keyring when one is usable (otherwise a 0600 file under the
   data directory) and never shown, logged or put in a support bundle. There is deliberately no
   "sign in with a subscription" option.
2. **Agents → New agent.** Name, backend, model, instructions, the tools it may use (one per
   line; a shell, file writes and anything that sends, posts, deletes or creates always asks),
   MCP servers, working folder, approval mode, limits, and who it is published to. *Add
   StudioForge (read-only key)* adds the StudioForge MCP server with the read-only key's secret
   name; an admin-scope key needs `admin_system` (an Admin).
3. **Limits and history.** Each agent's own dollars, turns, minutes and tokens; how long
   transcripts are kept (default **30 days**); how many tasks run at once; the
   `agents.unified_dispatch` switch (below).
4. **Secrets.** The names MCP servers need (`MCP_…`, `STUDIOFORGE_…`) and whether each is set.

What only an **Admin** (`admin_system`) may grant an agent, because each is code execution on this machine in
effect or hands out a stored secret: trusting a repository's own settings (its hooks will run),
a local-program MCP server, a server marked admin-scope, a stored secret sent to an address that
is not on this machine, and pre-approved tools for an agent that never asks.

An agent is read afresh at every run: editing its tools, limits or approval policy applies to the
next follow-up or resume of tasks it already has, and unpublishing, switching off or deleting it
stops a profile from continuing them (they can still read the history; Stop always works).

### Safety rules that are enforced, not advised

- **Isolation.** The Claude Agent runs with an explicit empty settings source, strict MCP
  config, a private `HOME` and a per-task config directory, so your own `~/.claude`, hooks and
  plugins are never inherited and a repository's `.claude/settings.json` hooks and `.mcp.json`
  servers do not execute. See [agents/security.md](agents/security.md).
- **Permission modes** are `default` (approval cards), `dontAsk` (unattended) and `plan`
  (investigate only). `bypassPermissions` and `auto` cannot be expressed.
- **Destructive tools always ask**, every time, and the card offers only "once".
- **Quarantine.** An agent that reads untrusted content (web pages, email, uploads — a web tool
  counts) cannot also hold a shell or write tools; split it into a reader and an actor.
- **Budgets** are a hard stop plus a notification: per task (dollars, turns, minutes, tokens)
  and per agent per day (there are no per-person limits). Costs are client-side estimates and are labelled so;
  never bill from them.
- **Audit.** Every finished task records who, which agent, approvals allowed and denied, the
  estimated cost, the duration and the outcome (Settings → People & Access → Activity & audit); every approval and
  every change to an agent is logged too.
- **Retention.** The purge deletes the transcript (prompt, events, result text, the agent's
  working folder) of finished tasks after the configured days and keeps the audit row.

## Where a task's data goes

| Backend | What leaves this process | Where to |
|---|---|---|
| OpenClaw | the prompt and file paths | the local OpenClaw gateway (loopback) or CLI; whatever model provider the OpenClaw agent is configured with then sees it |
| Claude Agent | prompts, tool results, files the agent reads | Anthropic's API (or the Bedrock / Google Cloud / Foundry account you chose); the working folder and the transcript stay on this machine |

Claude **Managed Agents** (hosted sessions, stored by Anthropic until deleted, not eligible for
zero-data-retention) are **not built**; they are milestone M2 and would be opt-in. Details:
[privacy.md](privacy.md).

## OpenClaw: the same-host requirement

OpenClaw is the adapter DisPatch has always had; it now runs behind the same interface and
shows up as a task backend. Its constraint is unchanged and is stated plainly:

**The agent must run on the same filesystem as DisPatch.** The CLI transport hands OpenClaw a
file *path*, and transcripts and media are read from the agent's home. The gateway socket alone
does not carry them.

| Topology | Works? |
|---|---|
| Agent installed in the DisPatch image | Yes |
| Agent in a sibling container | No — separate filesystems |
| Agent on the host, DisPatch in a container | Only with the agent's home directory bind-mounted in, and matching paths on both sides. Fragile. |
| Agent on another machine | No |
| No agent at all | Yes — fully supported |

If you are containerising and want OpenClaw agents, bake the agent into the image
([deploy-docker.md](deploy-docker.md)). A startup probe checks the gateway protocol
(version 4): an OpenClaw upgrade that changes it fails the task loudly
(`backend_protocol_mismatch`) instead of misbehaving. DisPatch never edits OpenClaw's
configuration. OpenClaw reports tokens, not money, and applies its own tool policy, so it shows
no approval cards and its turn / token limits are checked after each turn.

The Claude Agent has no such constraint on where DisPatch runs; the agent worker runs next to
DisPatch, and only its API calls leave the machine.

## `agents.unified_dispatch` — the flag, and what it changes

**Default: off.** With it off, DisPatch behaves exactly as before: an OpenClaw character's chat
turn runs through the existing transport/watcher/mirror stack, and an advisor's
`[[handoff:…]]` card is only a card and a row in the advisor's ledger.

With it **on** (Settings → AI & Connections → Agents → Limits, or `DISPATCH_AGENTS_UNIFIED_DISPATCH=1`, which
overrides the setting either way):

- an OpenClaw character's chat turn runs as a hidden *chat task* whose id is the thread id (so
  the OpenClaw session key is the same one the old path used) — same lock, typing indicator,
  delivery funnel and error frames, with limits, a record and the protocol probe;
- a `[[handoff:<agent>|brief]]` card also becomes a task in `queued`, held until the chat's owner
  presses Send (the card's button, or Send on the Tasks page — same UX, same
  `approve_agent_actions` permission); Send runs the advisor's own pipeline (the brief,
  the saved note, the report) as a visible task, and Dismiss cancels it.

What the flag **does not carry over**, which is why it is off: the in-turn transcript watcher
(narration between tool calls arrives as the one final reply), the media second look, the
follower for late sub-agent announcements and the gateway-mirror reconciliation. Those patch
the legacy transport; the adapter has its own. Turn it on only after trying it on a
non-critical character.

Three queue implementations exist today — image jobs, the advisor ledger and agent tasks. They
are documented as converging on the task model; this release does not rewrite image jobs or the
DeepSeek harness.

## API

All routes are declared in `backend/app/route_policy.py` and listed in
[contracts/routes.json](contracts/routes.json); errors use the catalogue in [errors.md](errors.md).

| Route | Permission |
|---|---|
| `GET/POST /api/agents`, `PATCH/DELETE /api/agents/{id}` | `manage_agents` |
| `GET /api/agents/backends`, `…/settings`, `…/usage`, `POST …/retention/purge` | `manage_agents` |
| `GET/PUT/DELETE /api/agents/credentials/claude`, `GET/PUT/DELETE /api/agents/secrets/{name}` | `manage_agents` (write-only secrets) |
| `GET /api/agents/available` | `dispatch_agents` (only agents published to you) |
| `POST/GET /api/tasks`, `GET /api/tasks/{id}`, `…/events?after_seq=`, `POST …/send`, `…/interrupt`, `…/resume`, `…/cancel` | `dispatch_agents` |
| `POST /api/approvals/{request_id}` (`allow` / `deny`, scope `once` / `task`), `POST /api/tasks/{id}/release`, `…/dismiss` | `approve_agent_actions` |

WebSocket frames (server to client only): `task.event` (one normalized event with a task
summary), `task.approval` (pending / allow / deny / expired) and `task.update`. They go only to
sockets whose profile may see the task (its dispatcher's); a client that sees a gap in
`event.seq` refetches `…/events?after_seq=`.

For the support bundle, `agents_store.redacted_summary()` returns counts and configuration shape
only — never a prompt, instruction, event, path or secret name. W5's `/api/support-bundle`
embeds it under `agents`.

## Configuring characters (bots)

A *character* is a chat participant; it is not an agent. A character whose replies come from a
local OpenClaw agent is configured in `config.yaml` (hot-reloaded):

```yaml
bots:
  - id: assistant          # the OpenClaw agent id passed to the CLI
    name: Assistant        # what people see
    emoji: "✨"
    avatar: assistant.png  # under frontend/static/avatars/
    order: 0
```

Who may use a character is decided on the server (household list, personal ownership and, for an
agent-backed one, `dispatch_agents`; see [characters.md](characters.md)): a character a profile may
not use is invisible *and* unreachable to it, not merely hidden. An **advisor** character answers from a model API
and can hand work to agents through `[[research:…]]` and `[[handoff:…]]` markers, with a
Send/Dismiss card for anything that does work — see [advisor.md](advisor.md).

## Running without agents

Set no characters, or leave OpenClaw unconfigured. Everything else works. The Tasks button and
the Agents tab appear only for people who hold the permissions.

## Pushing messages in from outside (the machine API)

Independent of the agent integration, anything on the host can post into a
thread over HTTP — a cron job, a monitoring script, a home-automation hook:

```bash
curl -X POST http://127.0.0.1:8765/api/inject \
  -H 'Content-Type: application/json' \
  -d '{"bot_id": "assistant", "content": "Backup finished: 4.2 GB in 6 minutes."}'
```

The field is `content`. Give a `thread_id` to target an existing conversation,
or a `bot_id` to find-or-create that bot's daily thread. From loopback this
needs no credential; from anywhere else it requires an API key. See
[configuration.md](configuration.md).

Three things worth knowing before you write a script against this, because each
one is a place where a naive caller silently does the wrong thing:

- **`GET /api/bots` gives a machine caller the household roster** (plus the first Admin's
  personal bots). A browser session gets only the bots its profile may use, so a script on the
  host is the way to discover ids.
- **Ids are matched case-insensitively**, both `bot_id` and `thread_id`, on
  every route: post, read back, rename, mark read, delete. The response carries
  the canonical id; store that one. This matters when the id reaches you
  lowercased from somewhere else (an agent session key, a log line) while the
  row it names is mixed case.
- **A message body over 65536 characters is refused with 422 and nothing is
  posted.** Neither `/api/inject` nor `POST /api/threads/<id>/messages`
  truncates, so a `200` means the whole body landed. Split long output across
  messages rather than relying on a silent cut.

### Pinging phones: `notify`

Add `"notify": true` (titled "<bot> posted") or `"notify": "Morning brief
posted"` (your own title, 80 characters at most) and every device that turned
on **Settings → Account → Notifications** gets a lock-screen ping. The ping
is the title and the thread id only — never the message text — and tapping it
opens the thread. Bot replies and finished pictures ping on their own; nothing
else does. Push needs the app's **https** address (a browser only allows push
in a secure context) and a remembered device ("Remember this device" when
unlocking); a plain `http://<ip>:8765` page shows the switch disabled.

### Posting events: one-line notices

A status line — a run finished, a watchdog tripped, a backup summary — is not
conversation, and should not arrive as a full chat bubble. Mark it with
`metadata.notice` and the chat shows it as ONE collapsed line: a coloured
level dot, the message's first line, and the time. Tapping it opens the full
text.

```bash
curl -X POST http://127.0.0.1:8765/api/inject \
  -H 'Content-Type: application/json' \
  -d '{"bot_id": "assistant",
       "content": "❌ Run bench-42 failed — see report\n\nFull log: /var/log/bench-42.txt",
       "metadata": {"notice": {"level": "error"}}}'
```

- `level` is one of `info`, `ok`, `warn`, `error`. `"notice": true` means
  `info`; an unknown level is clamped to `info`.
- Send `notice` only. The server also sets `metadata.sub`, so the row is kept
  out of the thread-list preview, the agent's conversation history and the
  reaction autopilot — you do not need to set `sub` yourself.
- The **first line** is the headline. Put the whole story on line one; details
  go below it.
- Quiet by design: `info`, `ok` and `warn` notices do **not** light the
  thread's unread dot. **`error` does** — a failure that reports nothing is
  the worst outcome, so a failed run stays visible even though it is
  collapsed.
- Anything that needs a person to answer — a question, a decision, "approve
  this?" — is **not** a notice. Post it as a plain message so it arrives as a
  full bubble and lights the dot.

Rows posted before this convention existed are still recognised by their
leading glyph (`⚠️`, `❌`, `✅ RECOVERED`) plus a failure word, and a one-time
startup migration marked the old `🔧 Run` / `📊` / `❌ Run` / `⚠️ Run` /
`✅ RECOVERED` injected lines as notices (the database is copied to
`chats.db.pre-notice-migration` first).

## Asking for a generated image

A bot can ask DisPatch for a picture instead of generating one itself. This is
**optional and off by default** — it needs an image server configured on the
host, and it needs switching on for the individual bot.

The point of it is that the agent does not wait. One call returns in
milliseconds; a real message appears in the thread straight away saying a
picture is coming, and DisPatch drives the image server in the background and
**rewrites that same message** when the render lands:

```bash
curl -X POST http://127.0.0.1:8765/api/image-jobs \
  -H 'Content-Type: application/json' \
  -d '{"bot_id": "nova", "thread_id": "daily-nova-2026-08-30",
       "prompt": "a blue ceramic teapot on a white table",
       "caption": "the teapot"}'
# 202 {"job_id": "…", "message_id": "…", "thread_id": "…", "state": "queued"}
```

Optional fields: `workflow` (image-server-specific; omit it and the bot's
`image_workflow` fills in, or the server picks its own default), `ratio` (`"3:2"`), `width` + `height` (both or
neither), `negative`, `caption`, `priority` (`1` interactive — the default for
this route — `2` normal, `3` background; anything else is a `422`).

`GET /api/image-jobs/<job_id>` reports one job's state — but an agent rarely
needs it. **The thread is the status display.** The message goes from
"🖼️ Generating an image…" (with the render's percentage once the image server
starts reporting one) to the picture, or to one of two endings:

- "⚠️ image failed: \<reason\>" — the image server's own words for what went
  wrong.
- "✋ The image was cancelled on the rig." — the render was withdrawn, because
  it passed its deadline, its thread was deleted, or somebody stopped it on
  the image server itself.

It never stays pending: every job has a ten-minute deadline, and a DisPatch
restart mid-render either resumes the job or fails it visibly.

The shorter way is to write the marker inline in a reply, which costs no call
at all:

```
Here's how that would look. [[pic:a blue ceramic teapot on a white table]]
```

`[[pic:<prompt>]]`, or `[[pic:<prompt>|<caption>]]` to caption the picture.
The marker is removed from the reply before it reaches the thread, the
placeholder appears underneath it, and nothing comes back to the agent — the
picture arrives on its own. At most two per message; the prompt may not
contain `|`; a marker inside backticks or a fenced block is quoted text and
does nothing. Everything else (the flags, the rate limit, the endings) is
identical to the endpoint, and `workflow` comes from the bot's own
`image_workflow` setting, since the marker has no room to name one.

Rules worth knowing:

- **Per bot, and per thread.** The bot needs `image_jobs: true` in its
  `config.yaml` entry, and the thread must be that bot's own — the placeholder
  is posted as an assistant message and would otherwise appear under another
  bot's name.
- **Three requests per bot per minute.** A render occupies a GPU for tens of
  seconds; a refusal is a `429` and is final, not queued.
- Same access rules as the rest of the machine surface: the API key (loopback
  without one only while `machine_loopback_trust` is on). The prompt passes the
  image hard-line guard (adult-only depictions; no sexual imagery of real,
  identifiable people).
- The finished picture is an ordinary chat image. It opens in the lightbox,
  and No-Image Mode omits the row entirely — the
  job pipeline adds no exceptions to any of that.

Host configuration for this is `DISPATCH_CLAWFORGE_URL` (the image server's
MCP endpoint), `DISPATCH_IMAGE_JOBS` (`auto`/`1`/`0`; `auto` means on iff that
URL is set) and the optional `DISPATCH_CALLBACK_BASE`, which only makes a
finished render appear sooner. See [configuration.md](configuration.md).

## Managing the Jobs board (post + vote + archive + tag)

The Jobs board is a curated feed of job postings, surfaced in the chat app
as a **structured list grouped by month** — not a chat. People open it from
the rail's **Tools** group (it is the app `jobboard`, a package under
`apps/jobboard/` — see [apps.md](apps.md)). The bot id `jobboard` owns the
data (its hidden `config.yaml` row is added on startup if missing — see
configuration.md); every post lands in the **current month's
discussion thread** (title `Jobs — YYYY-MM`), so all jobs from a given
month are visible in one place and the dedup hash short-circuits reposts
to the existing job row.

**`/api/jobs` still works.** Since the board became an app (2026-09-25) its
API is mounted twice, identically: at `/api/apps/jobboard/…` and at the
legacy `/api/jobs/…`. The `dispatch-jobs` CLI (`~/.local/bin/dispatch-jobs`,
documented in the shared OpenClaw `dispatch` skill), the scout agent's brief
(the `AGENTS.md` in its OpenClaw workspace) and the daily sweep cron keep calling `/api/jobs/…` exactly as before — sessionless, from
loopback, with or without a PIN — and nothing about their requests or the
responses changed. New callers may use either prefix.

Every route is **machine-callable** (no PIN-derived session needed from
loopback, or remotely with the `api_token`). A browser session needs the app's
permission (`manage_agents`, so an Admin).

The full lifecycle from one call:

```bash
# 1. (Optional) dry-run a candidate score against the current profile.
curl -X POST http://127.0.0.1:8765/api/jobs/score \
  -H 'Content-Type: application/json' \
  -d '{
    "url": "https://example.com/careers/senior-engineer",
    "title": "Senior Engineer",
    "company": "Example Corp",
    "location": "Remote",
    "remote_type": "remote",
    "salary_min": 220000, "salary_max": 320000,
    "tags": ["python", "ml"]
  }'
# -> {"score": 0.83, "breakdown": {...}, "explanation": [...], "embedding_unavailable": false}

# 2. Post the job. Auto-routes to the current month's discussion thread;
#    creates the thread if missing. Returns the new job_id + message_id.
curl -X POST http://127.0.0.1:8765/api/jobs \
  -H 'Content-Type: application/json' \
  -d '{
    "bot_id": "jobboard",
    "url": "https://example.com/careers/senior-engineer",
    "title": "Senior Engineer",
    "company": "Example Corp",
    "location": "Remote",
    "remote_type": "remote",
    "salary_min": 220000, "salary_max": 320000,
    "tags": ["python", "ml"],
    "brief": "Inference team; LLM serving.",
    "source_agent": "scout"
  }'
# -> {"duplicate": false, "job_id": "...", "thread_id": "...",
#      "message_id": "...", "thread": {...}, "job": {...}}

# 3. List months (for the picker).
curl http://127.0.0.1:8765/api/jobs/months
# -> {"months": [{year, month, key, label, thread_id, ...}, ...], "current": {...}}

# 4. Fetch one job (with its events + score against the live profile).
curl http://127.0.0.1:8765/api/jobs/<job_id>
# -> {"thread": {...}, "job": {...}, "events": [...], "score": {...}}

# 5. Vote (yes/no/maybe/undo) on a job.
curl -X POST http://127.0.0.1:8765/api/jobs/<job_id>/vote \
  -H 'Content-Type: application/json' \
  -d '{"signal": "yes"}'
# -> {"ok": true}
# A "no" vote requires a reason_tag from /api/jobs/reasons.

# 6. Edit tags.
curl -X POST http://127.0.0.1:8765/api/jobs/<job_id>/tags \
  -H 'Content-Type: application/json' \
  -d '{"add": ["staff", "remote-ok"], "remove": ["junior"]}'

# 7. Archive.
curl -X POST http://127.0.0.1:8765/api/jobs/<job_id>/archive
```

**CLI wrapper.** Every verb has a subcommand under `dispatch-jobs` (see
`scripts/dispatch-jobs` in this repo, or install via `install-tools.sh`).
Pattern:

```bash
dispatch-jobs post --url https://... --title "Senior Engineer" --company "Example Corp" \
                   --salary-min 220000 --salary-max 320000 --tags python,ml
dispatch-jobs vote <job_id> --signal yes
dispatch-jobs list --state pending --source-agent scout
dispatch-jobs months           # the picker data
dispatch-jobs view <job_id>    # full record + score
dispatch-jobs profile          # tag_weights, blocklists, counts
dispatch-jobs recompute        # rebuild profile from feedback
```

**Dedup.** A repost with the same `(url, title, company)` within 30 days
returns `{duplicate: true, existing_job_id, existing_thread_id, last_seen}` —
no second row is written. A repost with the same URL but a changed title
or company (90-day window) returns `{duplicate: false, repost_of:
<existing_job_id>, hash}` so callers can link the new and old jobs.

**Auth matrix.** Same at both prefixes:

- **On-box machine** (loopback, or a remote caller with the `api_token`): every
  route, reads and writes (`POST /api/jobs`, `score`, `find`, and the manage
  verbs `vote / applied / feedback / tags / archive / recompute`). The app's
  write routes all use `ctx.require_access` = operator OR machine.
- **Admin browser session**: every route.
- **Any other profile**: 403 `permission_denied` on every route and on
  the `/apps/jobboard/` page.
- **Remote caller without the token**: 401.
- **No-PIN install**: everything is open, as for the rest of DisPatch.
- **Switched off** (`{id: jobboard, kind: app, enabled: false}` in
  `tools.yaml`): 404 everywhere.

**WebSocket frames.** A new or changed job is broadcast as
`app:jobboard:job_created` / `app:jobboard:job_updated` (they were
`job_created` / `job_updated` before the move). Only sockets of profiles
that may see the app receive them.

## Pointing at a file on the host

An agent that has just written a report, a log or a generated HTML page can hand
it over as a **link the operator opens in the app** rather than as an
attachment. Write the path in the reply:

```
Build finished. [[view:/var/log/dispatch/build.html|build report]]
```

That renders a 👁 card; tapping it opens the file full-screen inside DisPatch —
an HTML page, a PDF, markdown, a log, an image, a video, or a folder listing —
on whatever device the operator is holding. A bare path in the text
(`/var/log/dispatch/build.html`, `~/reports/`) becomes the same link, as does a
markdown link whose href is a local path.

The file is never copied. That has two consequences worth stating: the link
breaks if you delete the file, and **only paths under the folders the operator
configured for the local viewer open at all** — everything else answers "Not
served by the local viewer", as do secrets, dotfiles and system paths whatever
the configuration says. It is also an Admin affordance: any other profile
sees the path as plain text and nothing happens when it is tapped. See
[configuration.md](configuration.md) and [security.md](security.md).

The four markers an agent can write in a message:

| Marker | Renders as | Use it for |
|---|---|---|
| `[[view:/abs/path\|label]]` | 👁 card, opens in the app | Something the operator should **look at**: a page, a report, a log, a folder |
| `[[doc:<id>\|name]]` | Download card | Something they should **keep** — the id comes from `POST /api/upload` |
| `[[media:/abs/path\|caption]]` | Inline image or video | Pictures and video you already have on disk; the bytes are copied into the media store |
| `[[pic:<prompt>\|caption]]` | Placeholder, then the picture | A picture that does not exist yet (see above) |

Anything else in `[[…]]` is literal text. A marker inside backticks or a fenced
block is quoted and does nothing — that is how you show a path without linking
it.

## Interactive checklist tables

An agent can post a **daily routine or task list** that renders as an
interactive widget instead of a plain table: each row gets a checkbox, checking
a row marks it complete and groups it at the bottom in check order, columns sort
by clicking their headers, and the state is stored with the message so it
survives a reload and is shared across every device.

The syntax is one fenced block whose language tag is exactly `checklist`,
containing an ordinary GFM table:

````markdown
```checklist
| Exercise  | Sets | Reps | Rest |
|-----------|------|------|------|
| Squat     | 3    | 10   | 60s  |
| Push-up   | 3    | 15   | 30s  |
| Deadlift  | 5    | 5    | 3min |
| Plank     | 1    | 60s  | —    |
```
````

Rules:

- The first token of the fence must be exactly `checklist` (```` ```checklist ```` or
  `~~~checklist`); a caption paragraph before the table is allowed and renders
  above the widget.
- The body is a standard markdown table — the same one that already renders
  elsewhere. The leading checkbox column is added automatically; do **not** add
  one yourself.
- Ticking a box edits the chat, so only the profile that owns the chat can do it.

## Writing your own adapter

A backend is a class that satisfies `AgentBackend` (`backend/app/agents/interface.py`): start a
task, stream normalized events (`status`, `text_delta`, `tool_call`, `tool_result`,
`approval_request`, `subagent_started` / `subagent_finished`, `retry`, `artifact`,
`cost_update`, `result`, `error`), answer approvals, interrupt, resume, cancel, and say what it
can do (`capabilities()`). `BaseBackend` supplies the registry, the event log, approvals, the
wall-clock watchdog and the one definition of how a run ends; an adapter writes `_run` and
`_do_interrupt`. The contract suite (`backend/tests/agents/contract/`) runs the same assertions
against every adapter, driven by recorded or scripted streams. The reference is
[agents/backends.md](agents/backends.md); the threat model is
[agents/security.md](agents/security.md). A pull request for another runner is very welcome.

For development, `DISPATCH_AGENTS_FAKE=1` registers the scripted fake backend
(`backend/app/agents/fake.py`): create an agent with backend `fake` and send a task whose prompt
is `scenario:simple`, `scenario:approval`, `scenario:long` (steerable), `scenario:subagent`,
`scenario:budget` or `scenario:error_refused` to watch every state of the card without a
model. It never appears in a normal install.
