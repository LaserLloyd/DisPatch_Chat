"""Claude Managed Agents (Anthropic-hosted agent loop + sandbox) on the AgentBackend interface.

OFF BY DEFAULT and NOT VERIFIED AGAINST THE LIVE SERVICE (4.3, #13): the setting
``agents.managed.enabled`` (default false) must be switched on, and the API key comes from the
secret store (``ANTHROPIC_API_KEY``, the same key the Claude Agent uses). The tests replay
recorded event streams (``tests/agents/fixtures/managed/``); nothing here has talked to the real
API. The wire shapes follow the ``claude-api`` skill's Managed Agents reference (beta header
``managed-agents-2026-04-01``, set by the SDK).

How a task maps onto the service
--------------------------------
* The remote AGENT (model, system prompt, tools, MCP servers) is created ONCE outside DisPatch
  (Console or ``ant apply``) and named in the spec: ``backend_options = {"agent_id": "agent_...",
  "agent_version": 3 (optional), "environment_id": "env_..."}``. DisPatch never creates or
  archives agents, environments or sessions.
* One task = one SESSION (``POST /v1/sessions``). A follow-up/resume sends another
  ``user.message`` into the same session; a steer while running does the same (messages queue).
* Each run: open the event stream FIRST, then send the ``user.message`` (stream-first: the SSE
  stream has no replay), then read until the session goes idle with a stop reason other than
  ``requires_action`` or terminates.
* Events -> the catalogue: ``agent.message`` text -> ``text_delta``; ``agent.tool_use`` /
  ``agent.mcp_tool_use`` -> ``tool_call``; their results -> ``tool_result``;
  ``span.model_request_end.model_usage`` -> tokens + a turn; ``session.usage.list_cost`` -> the
  USD total; ``session.status_idle`` ``end_turn`` -> succeeded (or interrupted, after a
  ``user.interrupt``), ``budget_reached`` -> budget_exceeded, ``retries_exhausted`` ->
  backend_error; ``session.status_terminated`` -> failed.
* A call the server parks for a decision (``evaluated_permission == "ask"``) becomes OUR
  approval card when the tool is in the agent's allowlist (interactive), an allow only for an
  ``unattended_allow`` match (unattended), and a deny otherwise; the answer goes back as
  ``user.tool_confirmation``. Calls the server allows on its own are reported, not gated: the
  remote agent's permission policy is the gate there (configure it ``always_ask``/``auto``).
* ``agent.custom_tool_use``: DisPatch provides no custom tools, so it answers with an error result.
* Budget: the session is created with ``budget.max_list_cost`` when the task has a USD cap
  (platform-enforced), and our own tracker checks every usage report as well.
* SDK errors map to the catalogue: 401/403 -> ``auth_failed``, 429 -> ``rate_limited``,
  5xx / connection / timeout -> ``backend_unavailable``, other 4xx -> ``refused``.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import re
from collections.abc import Callable
from typing import Any

from .branding import tool_matches
from .events import ErrorCode, TextDeltaEvent, ToolCallEvent, ToolResultEvent, clip
from .interface import (
    AgentBackendError,
    BaseBackend,
    Capabilities,
    TaskRuntime,
    tool_in_allowlist,
)
from .secrets import SECRET_ANTHROPIC_API_KEY, get_agent_logger

log = get_agent_logger("managed")

_ID = re.compile(r"^[A-Za-z0-9_-]{3,128}$")


def _enabled_setting() -> bool:
    from .. import agents_store
    return bool(agents_store.get_setting("agents.managed.enabled"))


def _default_client(api_key: str) -> Any:
    from anthropic import AsyncAnthropic
    return AsyncAnthropic(api_key=api_key, max_retries=2)


def map_sdk_error(e: BaseException) -> AgentBackendError:
    """An Anthropic SDK exception -> a catalogued error (never the key, never the body)."""
    try:
        import anthropic
    except ImportError:                                     # pragma: no cover
        return AgentBackendError(ErrorCode.sdk_missing, "the anthropic package is not installed")
    if isinstance(e, AgentBackendError):
        return e
    if isinstance(e, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
        return AgentBackendError(ErrorCode.auth_failed,
                                 "Anthropic refused the API key for Managed Agents.")
    if isinstance(e, anthropic.RateLimitError):
        return AgentBackendError(ErrorCode.rate_limited, "Managed Agents is rate limiting this key.")
    if isinstance(e, (anthropic.APITimeoutError, anthropic.APIConnectionError)):
        return AgentBackendError(ErrorCode.backend_unavailable, "Managed Agents could not be reached.")
    if isinstance(e, anthropic.APIStatusError):
        if e.status_code >= 500:
            return AgentBackendError(ErrorCode.backend_unavailable,
                                     f"Managed Agents answered {e.status_code}.")
        return AgentBackendError(ErrorCode.refused, f"Managed Agents refused the request ({e.status_code}).",
                                 detail=clip(str(getattr(e, "message", "") or ""), 300) or None)
    return AgentBackendError(ErrorCode.backend_error, f"{type(e).__name__}: {clip(str(e), 300)}")


def _as_dict(obj: Any) -> dict[str, Any]:
    if isinstance(obj, dict):
        return obj
    fn = getattr(obj, "to_dict", None)
    if callable(fn):
        with contextlib.suppress(Exception):
            d = fn()
            if isinstance(d, dict):
                return d
    return {}


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(str(b.get("text") or "") for b in content
                   if isinstance(b, dict) and b.get("type") == "text")


def _usd_of(list_cost: Any) -> float | None:
    """``{"amount": "1234", "currency": "USD"}`` -> 12.34.

    The unit is DOCUMENTED (claude-api skill, Managed Agents core reference, session ``usage``:
    "``list_cost`` (consumption priced at public list rates, as ``{amount, currency}`` with the
    amount an integer string in minor units - cents)"; the budget's ``max_list_cost`` uses the
    same form, ``"2500"`` = $25.00, and is rounded to the nearest cent). An integer (string or
    number) is therefore cents. A decimal string or a float is not a documented form: it is
    read as dollars, the conservative reading (over-counting stops a run early; reading dollars
    as cents would under-count 100x). Still not exercised against the live service."""
    if not isinstance(list_cost, dict) or str(list_cost.get("currency") or "USD").upper() != "USD":
        return None
    amt = list_cost.get("amount")
    if isinstance(amt, bool) or amt is None:
        return None
    if isinstance(amt, int) or (isinstance(amt, str) and amt.strip().isdigit()):
        return int(amt) / 100.0
    try:
        return float(amt)
    except (TypeError, ValueError):
        return None


async def sse_events(lines) -> Any:
    """Server-sent events -> one dict per ``data:`` payload (``event:`` names and ``:`` heartbeats
    are skipped; the type is inside the JSON). Tolerant: a payload that is not a JSON object is
    ignored."""
    buf: list[str] = []
    async for line in lines:
        line = line.rstrip("\r")
        if line == "":
            if buf:
                with contextlib.suppress(ValueError):
                    obj = json.loads("\n".join(buf))
                    if isinstance(obj, dict):
                        yield obj
                buf = []
            continue
        if line.startswith("data:"):
            buf.append(line[5:].lstrip(" "))
    if buf:
        with contextlib.suppress(ValueError):
            obj = json.loads("\n".join(buf))
            if isinstance(obj, dict):
                yield obj


class ManagedAgentsBackend(BaseBackend):
    name = "managed"
    #: after a user.interrupt the session should go idle; past this the run is cut
    INTERRUPT_GRACE_S = 15.0

    def __init__(self, *, client_factory: Callable[[str], Any] | None = None,
                 enabled: Callable[[], bool] | None = None, **kw: Any) -> None:
        super().__init__(**kw)
        self._client_factory = client_factory or _default_client
        self._enabled = enabled or _enabled_setting

    def capabilities(self) -> Capabilities:
        return Capabilities(
            name="managed", label="Claude Managed Agents", streaming_text=True, approvals=True,
            interrupt=True, resume=True, steering=True, subagents=False, cost_reporting="usd",
            budget_enforcement="hard", session_listing=False, same_host_required=False,
            data_leaves_host=True, permission_modes=["interactive", "unattended"])

    # -- set-up ---------------------------------------------------------------- #

    def _options(self, rt: TaskRuntime) -> dict[str, Any]:
        opts = rt.spec.backend_options or {}
        agent_id, env_id = str(opts.get("agent_id") or ""), str(opts.get("environment_id") or "")
        if not _ID.match(agent_id) or not _ID.match(env_id):
            raise AgentBackendError(ErrorCode.invalid_request,
                                    "a Managed Agents agent needs backend_options.agent_id and "
                                    "backend_options.environment_id (created in the Console)")
        version = opts.get("agent_version")
        if version is not None and (isinstance(version, bool) or not isinstance(version, int) or version < 1):
            raise AgentBackendError(ErrorCode.invalid_request, "backend_options.agent_version: a positive integer")
        return {"agent_id": agent_id, "environment_id": env_id, "agent_version": version}

    async def _prepare(self, rt: TaskRuntime) -> None:
        if not self._enabled():
            raise AgentBackendError(ErrorCode.not_supported,
                                    "Managed Agents are switched off (Settings, Agents).")
        self._options(rt)
        if rt.extras.get("client") is not None:
            return
        key = rt.ctx.secrets.get(SECRET_ANTHROPIC_API_KEY) if rt.ctx.secrets is not None else None
        if not key:
            raise AgentBackendError(ErrorCode.auth_failed,
                                    "Managed Agents need an Anthropic API key (Settings, Agents).")
        self.redactor.register(key)
        rt.extras["client"] = self._client_factory(key)

    async def _call(self, coro) -> Any:
        try:
            return await coro
        except AgentBackendError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as e:
            raise map_sdk_error(e) from None

    async def _send(self, rt: TaskRuntime, events: list[dict[str, Any]]) -> None:
        client = rt.extras["client"]
        await self._call(client.beta.sessions.events.send(rt.session_id, events=events))

    async def _ensure_session(self, rt: TaskRuntime) -> None:
        if rt.session_id:
            return
        o = self._options(rt)
        agent: Any = o["agent_id"]
        if o["agent_version"] is not None:
            agent = {"type": "agent", "id": o["agent_id"], "version": o["agent_version"]}
        extra: dict[str, Any] = {}
        cap = rt.spec.budget.max_usd
        if cap is not None:
            extra["budget"] = {"type": "limit",
                               "max_list_cost": {"amount": str(max(1, round(cap * 100))), "currency": "USD"}}
        client = rt.extras["client"]
        sess = await self._call(client.beta.sessions.create(
            agent=agent, environment_id=o["environment_id"], title=f"DisPatch {rt.task_id}"[:120],
            metadata={"dispatch_task": rt.task_id}, extra_body=extra or None))
        sid = getattr(sess, "id", None) or _as_dict(sess).get("id")
        if not sid:
            raise AgentBackendError(ErrorCode.backend_protocol_mismatch, "Managed Agents returned no session id")
        rt.session_id = str(sid)

    # -- run --------------------------------------------------------------------- #

    async def _run(self, rt: TaskRuntime, message: str, resumed: bool) -> None:
        verdict = rt.tracker.check()
        if verdict is not None:
            self._finish_budget(rt, verdict)
            return
        await self._ensure_session(rt)
        client = rt.extras["client"]
        # Stream FIRST, then send: the stream has no replay. The raw SSE lines are read (not the
        # SDK's typed union) so event types and stop reasons newer than the pinned SDK - e.g.
        # ``session.usage`` and ``budget_reached`` - still arrive with every field.
        cm = client.beta.sessions.events.with_streaming_response.stream(rt.session_id)
        resp = await self._call(cm.__aenter__())
        rt.extras.update(stream=cm, text="", last_text=None, session_error=None)
        self._begin_running(rt, "resumed" if resumed else None)
        try:
            await self._send(rt, [{"type": "user.message", "content": [{"type": "text", "text": message}]}])
            it = sse_events(resp.iter_lines()).__aiter__()
            while rt.log.active:
                try:
                    ev = await it.__anext__()
                except StopAsyncIteration:
                    break
                except AgentBackendError:
                    raise
                except Exception as e:
                    raise map_sdk_error(e) from None
                await self._handle(rt, ev)
            if rt.log.active:
                if rt.interrupt_requested:
                    self._finish_interrupted(rt)
                    return
                raise AgentBackendError(ErrorCode.backend_unavailable,
                                        "the Managed Agents event stream ended before the session went idle")
        finally:
            if rt.extras.pop("stream", None) is not None:
                with contextlib.suppress(Exception):
                    await cm.__aexit__(None, None, None)

    async def _handle(self, rt: TaskRuntime, ev: dict[str, Any]) -> None:
        t = str(ev.get("type") or "")
        if t == "agent.message":
            text = _text_of(ev.get("content"))
            if text:
                rt.extras["text"] += text
                rt.extras["last_text"] = text
                rt.log.emit(TextDeltaEvent, text=text)
        elif t in ("agent.tool_use", "agent.mcp_tool_use"):
            name = str(ev.get("name") or ev.get("tool_name") or "tool")
            if t == "agent.mcp_tool_use" and ev.get("mcp_server_name"):
                name = f"mcp__{ev['mcp_server_name']}__{name}"
            call_id = str(ev.get("id") or "")
            inp = ev.get("input") if isinstance(ev.get("input"), dict) else {}
            rt.log.emit(ToolCallEvent, call_id=call_id, name=name, input=inp)
            if ev.get("evaluated_permission") == "ask":
                await self._confirm(rt, call_id, name, inp)
        elif t in ("agent.tool_result", "agent.mcp_tool_result"):
            out = ev.get("content")
            rt.log.emit(ToolResultEvent, call_id=str(ev.get("tool_use_id") or ev.get("mcp_tool_use_id") or ""),
                        ok=not ev.get("is_error"), output=clip(_text_of(out) if not isinstance(out, str) else out))
        elif t == "agent.custom_tool_use":
            await self._send(rt, [{"type": "user.custom_tool_result", "custom_tool_use_id": str(ev.get("id")),
                                   "is_error": True,
                                   "content": [{"type": "text", "text": "DisPatch provides no custom tools."}]}])
        elif t == "span.model_request_end":
            u = ev.get("model_usage") if isinstance(ev.get("model_usage"), dict) else {}
            rt.tracker.record_tokens(u.get("input_tokens"), u.get("output_tokens"))
            rt.tracker.record_turns(rt.tracker.turns + 1)
            if self._check_budget_after_usage(rt):
                await self._interrupt_remote(rt)
        elif t == "session.usage":
            usd = _usd_of(ev.get("list_cost"))
            if usd is not None:
                rt.tracker.record_total_cost(usd)
                if self._check_budget_after_usage(rt):
                    await self._interrupt_remote(rt)
        elif t == "session.error":
            err = ev.get("error") if isinstance(ev.get("error"), dict) else {}
            rt.extras["session_error"] = clip(str(err.get("message") or ev.get("message") or "session error"), 300)
        elif t == "session.status_idle":
            stop = ev.get("stop_reason") if isinstance(ev.get("stop_reason"), dict) else {}
            self._on_idle(rt, str(stop.get("type") or ""))
        elif t == "session.status_terminated":
            if rt.interrupt_requested:
                self._finish_interrupted(rt)
            else:
                self._finish_failed(rt, AgentBackendError(
                    ErrorCode.backend_error,
                    rt.extras.get("session_error") or "the Managed Agents session ended"))
        # every other type (status_running, thinking, spans, previews, echoes) is ignored

    def _on_idle(self, rt: TaskRuntime, stop: str) -> None:
        if stop == "requires_action":
            return                                  # waiting on us; _confirm already answered
        if rt.interrupt_requested:
            self._finish_interrupted(rt)
            return
        if stop == "end_turn":
            if self._check_budget_after_usage(rt):
                return
            self._finish_succeeded(rt, rt.extras.get("last_text") or rt.extras.get("text") or None)
        elif stop == "budget_reached":
            from .budget import BudgetVerdict
            b = rt.spec.budget
            self._finish_budget(rt, BudgetVerdict("usd", b.max_usd or 0.0, rt.tracker.usd))
        else:
            self._finish_failed(rt, AgentBackendError(
                ErrorCode.backend_error,
                rt.extras.get("session_error") or f"the Managed Agents session stopped ({stop or 'unknown'})"),
                resumable=True)

    async def _confirm(self, rt: TaskRuntime, call_id: str, name: str, inp: dict[str, Any]) -> None:
        pol = rt.spec.approval_policy
        if not tool_in_allowlist(rt.spec, name):
            decision = "deny"
        elif pol.mode == "unattended":
            decision = "allow" if tool_matches(name, pol.unattended_allow) else "deny"
        elif pol.mode == "plan":
            decision = "deny"
        else:
            decision = await self._request_approval(rt, tool_name=name, input=inp, call_id=call_id,
                                                    reason="the Managed Agents session asked")
        with contextlib.suppress(AgentBackendError):
            await self._send(rt, [{"type": "user.tool_confirmation", "tool_use_id": call_id,
                                   "result": decision}])

    # -- control ------------------------------------------------------------------ #

    async def _interrupt_remote(self, rt: TaskRuntime) -> None:
        if rt.session_id and rt.extras.get("client") is not None:
            with contextlib.suppress(Exception):
                await self._send(rt, [{"type": "user.interrupt"}])

    async def _do_interrupt(self, rt: TaskRuntime) -> None:
        await self._interrupt_remote(rt)
        self._spawn(self._force_after_grace(rt, rt.run_task))

    async def _force_after_grace(self, rt: TaskRuntime, run: asyncio.Task | None) -> None:
        await asyncio.sleep(self.INTERRUPT_GRACE_S)
        if run is not None and not run.done() and rt.log.active:
            log.warning("Managed Agents session for task %s did not go idle after the interrupt", rt.task_id)
            run.cancel()

    async def _do_steer(self, rt: TaskRuntime, message: str) -> None:
        await self._send(rt, [{"type": "user.message", "content": [{"type": "text", "text": message}]}])

    async def _release(self, rt: TaskRuntime) -> None:
        cm = rt.extras.pop("stream", None)
        if cm is not None:
            with contextlib.suppress(Exception):
                await cm.__aexit__(None, None, None)
        client = rt.extras.pop("client", None)
        if client is not None:
            with contextlib.suppress(Exception):
                await client.close()

    def _support_task(self, rt: TaskRuntime) -> dict[str, Any]:
        out = super()._support_task(rt)
        opts = rt.spec.backend_options or {}
        out["remote"] = {"agent_id": opts.get("agent_id"), "environment_id": opts.get("environment_id")}
        return out
