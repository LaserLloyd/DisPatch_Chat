# Design: a pluggable agent backend

[← Back to the README](../../README.md) · Related:
[agents.md](../agents.md) · [llm-providers.md](../llm-providers.md)

Status: **the interface is built (3.0); the remote HTTP backend is not.**
`backend/app/agents/` defines `AgentBackend`, the normalized events and the OpenClaw
and Claude Agent adapters ([agents.md](../agents.md),
[agents/backends.md](../agents/backends.md)). OpenClaw is still bound to the host
filesystem, which is why the only supported OpenClaw topology is "same host as
DisPatch"; the `http` backend this page designs, a remote agent, is still unbuilt.

## The problem, and the fix

> **Partly addressed.** DisPatch Chat now ships a *second* backend that has none of
> these constraints: a bot with an `api` block in `config.yaml` talks straight
> to an LLM provider over HTTP (`backend/app/llm_api.py`, set up from the
> **Connect an AI** panel — see [llm-providers.md](../llm-providers.md)). It works
> in any container topology and needs nothing on the host, which removes the
> "you cannot use this in Docker without baking an agent into the image"
> problem for anyone who only wants a conversational assistant.
>
> It does **not** close this item. What it adds is a direct-API path, not a
> pluggable *agent* interface: no tools, no streaming partials, no session
> transcripts. `run_agent_turn` currently branches on `bot.api` rather than
> dispatching through the `AgentBackend` protocol below, and the `http` backend
> described here — a remote *agent* — is still unbuilt. When that interface
> lands, the direct-API path should become one more implementation behind it.

**Problem.** The agent integration is bound to the host filesystem in three ways, and
the second is the one that blocks every containerised topology:

1. It spawns a binary — `openclaw agent --agent … --json`.
2. It passes a **filesystem path**: `--message-file /tmp/dispatch-msg-XXXX.txt`
   (argv has a ~128 KB per-argument cap that long messages exceed).
3. It reads transcripts directly: `~/.openclaw/agents/<id>/sessions/*.jsonl`, for both
   streaming partials and the conversation mirror.

Because of (2) and (3), a sibling container or a remote host cannot serve as the agent
backend at all. The only working container topology is "bake the agent into the same
image", which defeats much of the point.

**Fix.** Introduce an `AgentBackend` interface:

```python
class AgentBackend(Protocol):
    async def send(self, bot_id: str, session_key: str, message: str,
                   timeout: int) -> AgentReply: ...
    def available(self) -> bool: ...
    def stream(self, bot_id: str, session_key: str) -> AsyncIterator[dict]: ...
```

with three implementations, selected by `DISPATCH_AGENT_BACKEND`:

| Value | Behaviour |
|---|---|
| `none` | **New default.** `available()` is False; turns return a clean "no agent backend configured" error. Makes the current warning-and-fail path explicit rather than incidental. |
| `subprocess` | Today's behaviour, unchanged. Default when `OPENCLAW_BIN` is set. |
| `http` | POST the message **in the request body** to a configured URL; stream the reply over that connection. No temp file, no shared filesystem, no transcript tailing. |

Only `http` makes `dispatch` + `agent` a clean two-container compose stack, and it also
covers "agent on a different machine" for free.

Keep the transcript-tailing mirror as a `subprocess`-only feature; it is inherently
filesystem-coupled and does not need an HTTP equivalent on day one.
