"""OpenClaw wrapped onto the AgentBackend interface.

This ADAPTS the existing transports; it does not replace them. The gateway
WebSocket client (``app/gateway_ws.py``, protocol 4 - RESEARCH.md R-OC1) and the
CLI fallback (``app/openclaw.py::send_to_agent``) keep doing the wire work; this
module turns what they produce into normalized events.

Mapping (frame shapes are the ones the existing code already consumes,
``gateway_router.py`` and ``openclaw.py``; the upstream schema itself is
UNVERIFIED - RESEARCH.md R-OC3):

    ``chat`` event, state=delta   ->  text_delta (cumulative snapshot diffed;
                                       a non-extension becomes ``replace=True``)
    ``session.tool`` / ``agent``  ->  tool_call on phase "start" (the only phase
                                       the repo handles today); tool_result on a
                                       terminal phase if the gateway sends one
    ``chat`` event, state=status  ->  status(running, detail=<phase>)
    ``session.message`` assistant ->  text_delta ONLY when no ``chat`` delta
                                       arrived for the run; a body ending in the
                                       truncation marker is refetched in full
                                       first (the 8000-char hazard, section 5 item 26)
    ``agent`` request's final res ->  result (text from the payloads), plus a
                                       token cost_update
    ``chat.abort`` (abort_run)    ->  interrupt / cancel

What OpenClaw does NOT give us, stated plainly because the contract suite
encodes it in ``Capabilities``: no approval round-trip (OpenClaw applies its own
tool policy inside the agent; DisPatch does not mediate it), no subagent events,
no steering of a running turn, no money (tokens only), so budgets are enforced
post-hoc for tokens/turns and by an abort for wall-clock.

SAME-HOST REQUIREMENT (RESEARCH.md section 5 item 26, audit section N): the CLI
transport hands OpenClaw a file PATH and transcripts/media are read from the
agent's home on the same filesystem; the gateway socket alone does not carry
them. This adapter is therefore ``same_host_required=True``. It never edits
OpenClaw configuration.

PROTOCOL PROBE: before the first task the adapter performs one handshake and
compares the gateway's reported protocol with ``gateway_ws.PROTOCOL`` (4). A
mismatch (the gateway closes 1002/PROTOCOL_MISMATCH, or reports another number,
or omits it) fails the task LOUDLY with ``backend_protocol_mismatch`` - and is
not retried and not masked by the CLI fallback.
"""
from __future__ import annotations

import asyncio
import contextlib
import math
import re
import uuid
from dataclasses import dataclass
from typing import Any

from .events import (
    ErrorCode,
    StatusEvent,
    TaskState,
    TextDeltaEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from .interface import (
    AgentBackendError,
    BaseBackend,
    Capabilities,
    SessionInfo,
    TaskContext,
    TaskRuntime,
)
from .secrets import SECRET_OPENCLAW_TOKEN, get_agent_logger

log = get_agent_logger("openclaw")

_AUTH_RE = re.compile(r"unauthori[sz]ed|forbidden|\bauth|token|credential", re.I)


@dataclass(frozen=True)
class GatewayProbe:
    protocol: int
    methods: tuple[str, ...]
    events: tuple[str, ...]


async def probe_gateway(url: str, token: str | None, *, timeout: float = 5.0) -> GatewayProbe:
    """One handshake against the gateway; raises AgentBackendError with a catalogued code.

    Uses the existing client's own handshake (so the probe speaks exactly what
    production speaks) over a throw-away socket.
    """
    from .. import gateway_ws

    if not token:
        raise AgentBackendError(
            ErrorCode.auth_failed,
            "no gateway token (set OPENCLAW_GATEWAY_TOKEN or gateway.auth.token) - "
            "the gateway has no loopback exemption")
    import websockets

    async def _go() -> dict:
        async with websockets.connect(url, open_timeout=timeout, origin=None,
                                      max_size=gateway_ws.MAX_FRAME_BYTES) as ws:
            probe_client = gateway_ws.GatewayClient(_noop_event, url=url, token=token)
            return await probe_client._handshake(ws, token)

    try:
        hello = await asyncio.wait_for(_go(), timeout=timeout + 2)
    except AgentBackendError:
        raise
    except Exception as e:
        raise _classify_probe_error(e, url) from e

    proto = hello.get("protocol")
    if proto != gateway_ws.PROTOCOL:
        raise AgentBackendError(
            ErrorCode.backend_protocol_mismatch,
            f"the OpenClaw gateway reports protocol {proto!r}; this build speaks "
            f"{gateway_ws.PROTOCOL}. Update DisPatch or pin OpenClaw (RESEARCH.md R-OC1).",
            retryable=False)
    try:
        gateway_ws.GatewayClient._assert_capabilities(hello)
    except RuntimeError as e:
        raise AgentBackendError(ErrorCode.backend_protocol_mismatch, str(e), retryable=False) from e
    feats = hello.get("features") or {}
    return GatewayProbe(protocol=proto, methods=tuple(feats.get("methods") or ()),
                        events=tuple(feats.get("events") or ()))


async def _noop_event(event: str, payload: dict) -> None:
    return None


def _classify_probe_error(e: BaseException, url: str) -> AgentBackendError:
    code = getattr(getattr(e, "rcvd", None), "code", None)
    reason = getattr(getattr(e, "rcvd", None), "reason", "") or ""
    text = f"{type(e).__name__}: {e}"
    if code == 1002 or "PROTOCOL_MISMATCH" in reason or "PROTOCOL_MISMATCH" in text:
        return AgentBackendError(
            ErrorCode.backend_protocol_mismatch,
            "the OpenClaw gateway closed the connection with 1002/PROTOCOL_MISMATCH: it does not "
            "speak protocol 4 (RESEARCH.md R-OC1, section 5 item 26)", retryable=False)
    if isinstance(e, RuntimeError) and "refused connect" in str(e):
        if _AUTH_RE.search(str(e)):
            return AgentBackendError(ErrorCode.auth_failed, f"the gateway refused our credentials: {e}")
        return AgentBackendError(ErrorCode.refused, str(e))
    if isinstance(e, (OSError, TimeoutError, asyncio.TimeoutError)) or type(e).__name__.startswith(
            ("ConnectionClosed", "InvalidHandshake", "InvalidURI", "InvalidMessage")):
        return AgentBackendError(
            ErrorCode.backend_unavailable,
            f"cannot reach the OpenClaw gateway at {url}: {text[:200]}")
    return AgentBackendError(ErrorCode.backend_error, text[:300])


def map_openclaw_exception(e: BaseException) -> AgentBackendError:
    """Existing OpenClaw exception classes -> the error catalogue."""
    from .. import openclaw

    if isinstance(e, AgentBackendError):
        return e
    if isinstance(e, openclaw.GatewayUnavailable):
        return AgentBackendError(ErrorCode.backend_unavailable, e.message, detail=e.detail)
    if isinstance(e, openclaw.AgentRefused):
        return AgentBackendError(ErrorCode.refused, e.message, detail=e.detail)
    if isinstance(e, openclaw.AgentTimeout):
        return AgentBackendError(ErrorCode.timeout, e.message, detail=e.detail)
    if isinstance(e, openclaw.AgentNotFound):
        return AgentBackendError(ErrorCode.backend_unavailable, e.message, detail=e.detail,
                                 retryable=False)
    if isinstance(e, openclaw.AgentError):
        return AgentBackendError(ErrorCode.backend_error, e.message, detail=e.detail)
    if isinstance(e, TimeoutError):
        return AgentBackendError(ErrorCode.timeout, "the OpenClaw call timed out")
    return AgentBackendError(ErrorCode.internal, f"{type(e).__name__}: {e}")


class OpenClawBackend(BaseBackend):
    name = "openclaw"
    #: how long after chat.abort the adapter waits for the gateway to end the run
    ABORT_GRACE_S = 10.0

    def __init__(self, *, gateway_url: str | None = None, token: str | None = None,
                 transport: str = "auto", probe_timeout_s: float = 5.0,
                 connect_timeout_s: float = 10.0, **kw: Any) -> None:
        super().__init__(**kw)
        if transport not in ("auto", "gateway", "cli"):
            raise ValueError("transport must be auto, gateway or cli")
        self._url = gateway_url
        self._token = token
        self._transport = transport
        self._probe_timeout = probe_timeout_s
        self._connect_timeout = connect_timeout_s
        self._client: Any = None
        self._probe: GatewayProbe | None = None
        self._connect_lock = asyncio.Lock()
        self._by_key: dict[str, TaskRuntime] = {}
        self._by_run: dict[str, TaskRuntime] = {}

    def capabilities(self) -> Capabilities:
        return Capabilities(
            name="openclaw", label="OpenClaw", streaming_text=self._transport != "cli",
            approvals=False, interrupt=True, resume=True, steering=False, subagents=False,
            cost_reporting="tokens", budget_enforcement="post_hoc", session_listing=True,
            same_host_required=True, data_leaves_host=False, permission_modes=[])

    # -- connection ---------------------------------------------------------- #

    def _resolve_token(self, rt: TaskRuntime | None) -> str | None:
        from .. import gateway_ws
        tok = self._token
        if not tok and rt is not None and rt.ctx.secrets is not None:
            tok = rt.ctx.secrets.get(SECRET_OPENCLAW_TOKEN)
        tok = tok or gateway_ws.gateway_token()
        self.redactor.register(tok)
        return tok

    async def _ensure_gateway(self, rt: TaskRuntime | None) -> Any:
        from .. import gateway_ws

        async with self._connect_lock:
            if self._client is not None and self._client.connected.is_set():
                return self._client
            url = self._url or gateway_ws.gateway_url()
            token = self._resolve_token(rt)
            self._probe = await probe_gateway(url, token, timeout=self._probe_timeout)
            if self._client is None:
                self._client = gateway_ws.GatewayClient(
                    self._on_event, url=url, token=token, on_connect=self._on_connect)
                await self._client.start()
            try:
                await asyncio.wait_for(self._client.connected.wait(), timeout=self._connect_timeout)
            except TimeoutError:
                raise AgentBackendError(
                    ErrorCode.backend_unavailable,
                    f"the OpenClaw gateway at {url} did not finish connecting") from None
            return self._client

    async def _on_connect(self) -> None:
        # Subscriptions die with the socket, so they are re-made on EVERY connect
        # (see gateway_ws.GatewayClient._run_forever).
        await self._client.subscribe_sessions()
        await self._client.resubscribe_sessions()

    async def _prepare(self, rt: TaskRuntime) -> None:
        from .. import openclaw

        want = self._transport
        if want in ("auto", "gateway"):
            try:
                rt.extras["client"] = await self._ensure_gateway(rt)
                rt.extras["transport"] = "gateway"
                return
            except AgentBackendError as e:
                fall_back = (want == "auto" and e.code == ErrorCode.backend_unavailable
                             and openclaw.cli_available())
                if not fall_back:
                    raise
                log.warning("gateway unavailable (%s); using the CLI transport", e.message)
        elif not openclaw.cli_available():
            raise AgentBackendError(ErrorCode.backend_unavailable,
                                    "OpenClaw is not available. Is it installed and on PATH?",
                                    retryable=False)
        rt.extras["transport"] = "cli"

    # -- events from the gateway ---------------------------------------------- #

    def _route(self, payload: dict) -> TaskRuntime | None:
        rid = payload.get("runId")
        if isinstance(rid, str) and rid in self._by_run:
            return self._by_run[rid]
        key = payload.get("sessionKey")
        return self._by_key.get(key.lower()) if isinstance(key, str) else None

    async def _on_event(self, event: str, payload: dict) -> None:
        from .. import gateway_router

        rt = self._route(payload)
        if rt is None or not rt.log.active:
            return
        if event == "chat":
            state = payload.get("state")
            if state == "delta":
                # 2026.10: only the first delta carries `message`; the rest are
                # append-only `deltaText` (gateway_router.merge_delta_text).
                text = gateway_router.merge_delta_text(rt.extras.get("raw"), payload)
                if text is not None:
                    rt.extras["raw"] = text
                    self._emit_delta(rt, text, replace=bool(payload.get("replace")))
            elif state == "status" and isinstance(payload.get("phase"), str):
                rt.log.emit(StatusEvent, state=TaskState.running, detail=payload["phase"])
        elif event in ("session.tool", "agent"):
            self._on_tool_event(rt, payload)
        elif event == "session.message":
            await self._on_session_message(rt, payload)

    def _emit_delta(self, rt: TaskRuntime, cumulative: str, *, replace: bool = False,
                    live: bool = True) -> None:
        sent: str = rt.extras.get("sent", "")
        if not cumulative or cumulative == sent:
            return
        if live:
            rt.extras["saw_delta"] = True
        if not replace and cumulative.startswith(sent):
            rt.log.emit(TextDeltaEvent, text=cumulative[len(sent):])
        else:
            rt.log.emit(TextDeltaEvent, text=cumulative, replace=True)
        rt.extras["sent"] = cumulative

    def _on_tool_event(self, rt: TaskRuntime, payload: dict) -> None:
        if payload.get("stream") != "tool":
            return
        data = payload.get("data")
        if not isinstance(data, dict):
            return
        name = data.get("name") or data.get("tool") or data.get("toolName")
        if not isinstance(name, str) or not name:
            return
        call_id = str(data.get("toolCallId") or data.get("id") or f"{name}-{rt.log.next_seq}")
        phase = data.get("phase")
        if phase == "start":
            args = data.get("args") or data.get("input") or {}
            rt.log.emit(ToolCallEvent, call_id=call_id, name=name,
                        input=args if isinstance(args, dict) else {"value": str(args)[:500]})
        elif phase in ("end", "result", "done", "error"):
            rt.log.emit(ToolResultEvent, call_id=call_id, name=name, ok=phase != "error",
                        output=str(data.get("result") or data.get("output") or data.get("error") or "")[:2000])

    async def _on_session_message(self, rt: TaskRuntime, payload: dict) -> None:
        from .. import gateway_router, gateway_ws

        message = payload.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            return
        if message.get("stopReason") == "error":
            return
        if rt.extras.get("saw_delta"):
            return                       # the deltas already carried this text
        text = gateway_router.text_of(message)
        if gateway_ws.GatewayClient.blocks_truncated(message) and rt.extras.get("client") is not None:
            mid = (message.get("__openclaw") or {}).get("id")
            if mid:
                full = await rt.extras["client"].full_text(payload.get("sessionKey") or "", mid)
                text = full or text
        if text:
            rt.extras["saw_session_msg"] = True
            self._emit_delta(rt, rt.extras.get("sent", "") + ("\n\n" if rt.extras.get("sent") else "") + text,
                         live=False)

    @staticmethod
    async def _drain(client: Any) -> None:
        """Let the gateway client's event pump finish what it already queued, so a
        reply that outran its own deltas still lands after them."""
        q = getattr(client, "_events", None)
        for _ in range(100):
            if q is None or q.empty():
                break
            await asyncio.sleep(0.005)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    # -- the run -------------------------------------------------------------- #

    async def _run(self, rt: TaskRuntime, message: str, resumed: bool) -> None:
        from .. import openclaw

        spec = rt.spec
        agent_id = openclaw.normalize_agent_id(spec.backend_options.get("agent_id") or spec.name)
        key = openclaw.session_key_for(agent_id, rt.task_id)
        # The session KEY is the durable address (OpenClaw mints a new internal sessionId on
        # rewind but keeps the key), so it is what resume continues and what we report.
        rt.session_id = key
        rt.extras.update(agent_id=agent_id, session_key=key, sent="", saw_delta=False,
                         saw_session_msg=False)
        verdict = rt.tracker.check()
        if verdict is not None:
            self._finish_budget(rt, verdict)
            return

        run_id = uuid.uuid4().hex
        rt.extras["run_id"] = run_id
        self._by_key[key.lower()] = rt
        self._by_run[run_id] = rt
        self._begin_running(rt, "resumed" if resumed else None)
        remaining = rt.tracker.remaining_wall_clock_s()
        timeout = max(1, math.ceil(remaining)) if remaining is not None else None
        client = rt.extras.get("client")
        call = asyncio.create_task(self._dispatch(rt, client, agent_id, key, message, timeout, run_id))
        rt.extras["call"] = call
        try:
            try:
                reply = await call
            except asyncio.CancelledError:
                if not rt.interrupt_requested:
                    raise
                self._finish_interrupted(rt)
                return
            except Exception as e:
                if rt.interrupt_requested:
                    self._finish_interrupted(rt)
                    return
                raise map_openclaw_exception(e) from e

            if client is not None:
                await self._drain(client)       # routes are still registered here
        finally:
            self._by_key.pop(key.lower(), None)
            self._by_run.pop(run_id, None)

        if rt.interrupt_requested:
            self._finish_interrupted(rt)
            return
        meta = reply.metadata or {}
        rt.extras["gateway_session_id"] = meta.get("session_id")      # changes on rewind; informational
        rt.turns_this_task += 1
        rt.tracker.record_turns(rt.turns_this_task)
        rt.tracker.record_tokens(meta.get("tokens_in"), meta.get("tokens_out"))
        text = "\n\n".join(p.text for p in reply.payloads if p.text and not p.sub).strip()
        if not text:
            text = "\n\n".join(p.text for p in reply.payloads if p.text).strip()
        if text and not rt.extras.get("saw_delta") and not rt.extras.get("saw_session_msg"):
            rt.log.emit(TextDeltaEvent, text=text)
        if self._check_budget_after_usage(rt):
            return
        self._finish_succeeded(rt, text or None)

    async def _dispatch(self, rt: TaskRuntime, client: Any, agent_id: str, key: str, message: str,
                        timeout: int | None, run_id: str) -> Any:
        from .. import openclaw

        thinking = rt.spec.backend_options.get("thinking")
        if rt.extras.get("transport") == "gateway":
            await client.subscribe_session(key)
            return await openclaw.send_via_gateway(
                client, agent_id, key, message, timeout=timeout, run_id=run_id,
                model=rt.spec.model, thinking=thinking)
        return await openclaw.send_to_agent(
            agent_id, key, message, timeout=timeout, model=rt.spec.model, thinking=thinking,
            on_spawn=lambda proc: rt.extras.__setitem__("proc", proc))

    # -- control -------------------------------------------------------------- #

    async def _do_interrupt(self, rt: TaskRuntime) -> None:
        from .. import openclaw

        if rt.extras.get("transport") == "gateway":
            client = rt.extras.get("client")
            if client is not None and rt.extras.get("session_key"):
                try:
                    await client.abort_run(rt.extras["session_key"], rt.extras.get("run_id"))
                except Exception as e:
                    log.warning("chat.abort failed for task %s: %s", rt.task_id, e)
        else:
            proc = rt.extras.get("proc")
            if proc is not None and proc.returncode is None:
                with contextlib.suppress(Exception):
                    openclaw._kill_tree(proc)
        self._spawn(self._force_after_grace(rt, rt.extras.get("call")))

    async def _force_after_grace(self, rt: TaskRuntime, call: asyncio.Task | None) -> None:
        await asyncio.sleep(self.ABORT_GRACE_S)
        if call is not None and not call.done():
            log.warning("gateway did not end the aborted run for task %s; abandoning the call",
                        rt.task_id)
            call.cancel()

    async def respond_approval(self, request_id: str, decision: str, scope: str = "once") -> None:
        raise AgentBackendError(
            ErrorCode.not_supported,
            "OpenClaw applies its own tool policy; DisPatch does not mediate approvals for it")

    async def _release(self, rt: TaskRuntime) -> None:
        key = rt.extras.get("session_key")
        if key:
            self._by_key.pop(key.lower(), None)

    async def list_sessions(self, ctx: TaskContext | None = None, *,
                            agent_id: str | None = None) -> list[SessionInfo]:
        """Sessions OpenClaw knows for an agent (read from its sessions index; same-host)."""
        from .. import openclaw
        ids = {agent_id} if agent_id else {
            rt.extras.get("agent_id") or openclaw.normalize_agent_id(
                rt.spec.backend_options.get("agent_id") or rt.spec.name)
            for rt in self._tasks.values()}
        out: list[SessionInfo] = []
        for aid in sorted(i for i in ids if i):
            rows = await asyncio.to_thread(openclaw.list_agent_sessions, aid)
            out.extend(SessionInfo(backend=self.name, session_id=r["session_id"],
                                   task_id=r["session_key"].rsplit(":", 1)[-1],
                                   title=r["session_key"], updated_at=r["mtime"], size=r["size"])
                       for r in rows)
        return out

    async def aclose(self) -> None:
        await super().aclose()
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.stop()
            self._client = None
