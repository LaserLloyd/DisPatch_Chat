# Agent backends

DisPatch Chat can route a conversation to an AI agent instead of another person. This
is **entirely optional**. With no agent configured, DisPatch is a complete
human-to-human chat — threads, files, search, media, the PWA, all of it — and
bot replies simply fail with a clear message instead of appearing.

> **Just want something to talk to?** You do not need any of this. **Connect an
> AI** (the 🔌 button in the left rail) points a bot straight at an LLM
> provider's HTTP API — LM Studio, Ollama, OpenAI, Anthropic, or anything
> OpenAI-compatible — with no runtime installed and no constraint on where
> DisPatch runs. It takes about a minute; see
> [llm-providers.md](llm-providers.md).
>
> The rest of this page is about the *other* kind of backend: a full agent
> runtime that can use tools and act on the machine. That is a bigger thing,
> and it comes with the hosting constraint described below.

Read this page before planning a deployment around agents, because the current
integration has a real constraint that affects how you can host it.

## How it works today

A "bot" is a chat participant whose replies come from a local command-line agent
process. When someone sends a message to a bot's thread, DisPatch:

1. Writes the message to a temporary file.
2. Spawns the configured agent binary with a fixed argument vector, passing that
   file with `--message-file` and a deterministic session key. (A file, not an
   argument — a long message would otherwise blow past the kernel's argument
   size limit.)
3. Tails the agent's session transcript on disk to stream progress into the UI
   while the turn runs.
4. Parses the final reply and posts it into the thread.

The ships-with adapter targets [OpenClaw](https://github.com/openclaw). The
coupling lives in `backend/app/openclaw.py`, and it is the only module that
knows anything about a specific agent.

## The constraint, stated plainly

**The agent must run on the same filesystem as DisPatch.**

Two things force this: the message is handed over as a *path*, and progress is
read by tailing transcript files the agent writes. Neither survives a process
boundary that does not share a filesystem.

The practical consequence for containers:

| Topology | Works? |
|---|---|
| Agent installed in the DisPatch image | Yes |
| Agent in a sibling container | No — separate filesystems |
| Agent on the host, DisPatch in a container | Only with the agent's home directory bind-mounted in, and matching paths on both sides. Fragile. |
| Agent on another machine | No |
| No agent at all | Yes — fully supported |

If you are containerising and want agents, bake the agent into the image. There
is a documented example in [deploy-docker.md](deploy-docker.md).

This is a design limitation we intend to remove, not a deliberate choice — see
[design/agent-backend.md](design/agent-backend.md) for the planned pluggable
`AgentBackend` interface with an HTTP implementation, which makes the sibling
and remote topologies work.

## Configuring bots

Bots are defined in `config.yaml` in your data directory. It is created on first
run and hot-reloads — no restart needed.

```yaml
bots:
  - id: assistant          # the agent id passed to the CLI
    name: Assistant        # what people see
    emoji: "✨"
    avatar: assistant.png  # under frontend/static/avatars/
    order: 0
    visible: true
    safe: false            # true = also reachable from Safe Mode (no PIN)
```

`safe` is what makes a bot reachable from the limited (no-password) tier, and it
is enforced on the server: a bot without it is invisible *and* unreachable to a
Safe-Mode session, not merely hidden. Set it thoughtfully — a bot reachable
without a password can be talked to by anyone who can reach the port, and each
message costs you an agent turn.

## Running without agents

Set no bots, or leave the agent binary unconfigured. Everything else works.
People can still chat with each other, share files, search history, and install
the PWA. The dashboard reports "no agent backend configured" as informational,
not as a fault.

## Pushing messages in from outside

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

- **`GET /api/bots` gives an on-box caller the real roster.** A browser tab with
  no unlocked session still gets the Safe-Mode subset — that is the two-tier
  gate, not a bug — but a script on the host sees every visible bot, so it is a
  fine way to discover ids.
- **Ids are matched case-insensitively**, both `bot_id` and `thread_id`, on
  every route: post, read back, rename, mark read, delete. The response carries
  the canonical id; store that one. This matters when the id reaches you
  lowercased from somewhere else (an agent session key, a log line) while the
  row it names is mixed case.
- **A message body over 65536 characters is refused with 422 and nothing is
  posted.** Neither `/api/inject` nor `POST /api/threads/<id>/messages`
  truncates, so a `200` means the whole body landed. Split long output across
  messages rather than relying on a silent cut.

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
- Same access rules as the rest of the machine surface: loopback needs no
  credential, remote needs the API key, and a locked browser tab gets a `403`.
- The finished picture is an ordinary chat image. It opens in the lightbox,
  it is hidden by Safe Mode, and No-Image Mode omits the row entirely — the
  job pipeline adds no exceptions to any of that.

Host configuration for this is `DISPATCH_CLAWFORGE_URL` (the image server's
MCP endpoint), `DISPATCH_IMAGE_JOBS` (`auto`/`1`/`0`; `auto` means on iff that
URL is set) and the optional `DISPATCH_CALLBACK_BASE`, which only makes a
finished render appear sooner. See [configuration.md](configuration.md).

## Managing the Jobs board (post + vote + archive + tag)

The Jobs board is a curated feed of job postings, surfaced in the chat app
as a **structured list grouped by month** — not a chat. The bot id
`jobboard` owns the surface; every post lands in the **current month's
discussion thread** (title `Jobs — YYYY-MM`), so all jobs from a given
month are visible in one place and the dedup hash short-circuits reposts
to the existing job row.

Every route is **inbound-exempt** (machine-callable without a PIN-derived
session) since the 2026-09-15 OpenClaw audit. Safe-Mode browsers are still
blocked at the `/api/jobs` prefix in `_decoy_blocked`, so a locked device
never sees a row.

The full lifecycle from one call:

```bash
# 1. (Optional) dry-run a candidate score against the current profile.
curl -X POST http://127.0.0.1:8765/api/jobs/score \
  -H 'Content-Type: application/json' \
  -d '{
    "url": "https://anthropic.com/careers/staff-swe",
    "title": "Staff Software Engineer",
    "company": "Anthropic",
    "location": "Tokyo, JP",
    "remote_type": "onsite",
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
    "url": "https://anthropic.com/careers/staff-swe",
    "title": "Staff Software Engineer",
    "company": "Anthropic",
    "location": "Tokyo, JP",
    "remote_type": "onsite",
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
dispatch-jobs post --url https://... --title "Staff SWE" --company Anthropic \
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

**Auth matrix.** The 14 routes split cleanly:

- **Inbound-exempt** (no session needed): `POST /api/jobs`,
  `POST /api/jobs/score`, every `GET /api/jobs*` read, and the manage
  verbs `vote / applied / tags / archive / recompute` on individual
  jobs.
- **Locked browser** (`_decoy_blocked` prefix match on `/api/jobs`):
  every route refuses a Safe-Mode caller with 403, so a locked device
  never sees a row.
- **No-PIN install**: `_require_full` is no-op (passes when no cookie is
  present), so a freshly-installed box lets any caller reach every verb.
  A PIN-protected install requires a full session for the manage verbs
  via the inbound allowlist bypass.

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
the configuration says. It is also an unlocked-tier affordance: a Safe-Mode
device sees the path as plain text and nothing happens when it is tapped. See
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
- Checking requires an unlocked (full) session. Safe Mode shows the checkboxes
  read-only.

## Writing your own adapter

The surface an agent backend has to satisfy is small: given a message, a session
identifier and a timeout, return the reply text and optionally stream progress.
`backend/app/openclaw.py` is about 600 lines and roughly a third of that is
defensive parsing of one specific CLI's output shapes.

If you write an adapter for another agent runner, a pull request is very
welcome — that is the fastest route to the pluggable interface being real rather
than planned.
