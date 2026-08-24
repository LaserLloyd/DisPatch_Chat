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
