"""The protocol surface the live-delta transport depends on.

Each of these is a thing the gateway offers that DisPatch previously did not
ask for, and every one of them fails SILENTLY when it is missing: a connection
with no `caps` never sees tool events, a connection with no per-session
subscription is healthy and receives no deltas, and a half-open socket that
passes TCP keepalive but delivers nothing looks exactly like a quiet evening.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app import gateway_ws


class _CaptureWS:
    """A socket that records what we send and answers what we ask."""

    def __init__(self, *, hello_policy=None, answers=None) -> None:
        self.sent: list[dict] = []
        self.closed_with: int | None = None
        self._hello_policy = hello_policy
        self._answers = answers or {}
        self._inbox: asyncio.Queue = asyncio.Queue()

    async def send(self, raw: str) -> None:
        frame = json.loads(raw)
        self.sent.append(frame)
        method = frame.get("method")
        if method == "connect":
            payload = {
                "protocol": 4,
                "features": {"methods": ["sessions.subscribe", "chat.history",
                                         "chat.message.get"],
                             "events": ["session.message"]},
            }
            if self._hello_policy is not None:
                payload["policy"] = self._hello_policy
            await self._inbox.put({"type": "res", "id": frame["id"],
                                   "ok": True, "payload": payload})
            return
        answer = self._answers.get(method, {})
        if isinstance(answer, Exception):
            await self._inbox.put({"type": "res", "id": frame["id"],
                                   "ok": False, "error": str(answer)})
            return
        await self._inbox.put({"type": "res", "id": frame["id"],
                               "ok": True, "payload": answer})

    async def recv(self) -> str:
        return json.dumps(await self._inbox.get())

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        try:
            return json.dumps(
                await asyncio.wait_for(self._inbox.get(), timeout=0.2))
        except TimeoutError:
            raise StopAsyncIteration

    async def close(self, code: int = 1000) -> None:
        self.closed_with = code


def _client(ws=None):
    async def _noop(_e, _p):
        return None
    c = gateway_ws.GatewayClient(_noop, token="t")
    if ws is not None:
        c._ws = ws
    return c


# --------------------------------------------------------------------------- #
# Handshake
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_the_handshake_advertises_the_tool_events_capability():
    """The gateway omits capability-gated tool events unless the client asks.

    A connection that does not ask is not refused — it is served silently
    without them, which is exactly the shape of failure this transport exists
    to remove.
    """
    ws = _CaptureWS()
    c = _client(ws)
    await c._handshake(ws, "token")
    connect = ws.sent[0]
    assert connect["params"]["caps"] == ["tool-events"]


@pytest.mark.asyncio
async def test_the_server_tick_interval_is_adopted_not_guessed():
    """`hello-ok.policy.tickIntervalMs` is the server's own number; the
    pre-handshake default is a guess and the docs say to honour the former."""
    ws = _CaptureWS(hello_policy={"tickIntervalMs": 7000})
    c = _client(ws)
    hello = await c._handshake(ws, "token")
    policy = hello.get("policy") or {}
    assert policy["tickIntervalMs"] == 7000


@pytest.mark.asyncio
async def test_a_socket_that_goes_silent_is_closed_with_4000():
    """A TCP connection can outlive its usefulness perfectly.

    Silence past twice the tick is the gateway's own liveness rule, and 4000 is
    its own close code — matching both keeps our reconnect behaviour the one
    the server expects.
    """
    ws = _CaptureWS()
    c = _client(ws)
    c._tick_ms = 10                      # 20ms of silence is a dead socket
    c._mark_inbound()
    await asyncio.wait_for(c._tick_watchdog(ws), timeout=2.0)
    assert ws.closed_with == gateway_ws.TICK_CLOSE_CODE
    assert c.tick_closes == 1


@pytest.mark.asyncio
async def test_a_talking_socket_is_never_closed_by_the_watchdog():
    ws = _CaptureWS()
    c = _client(ws)
    c._tick_ms = 200
    c._mark_inbound()
    watchdog = asyncio.create_task(c._tick_watchdog(ws))
    for _ in range(6):
        await asyncio.sleep(0.05)
        c._mark_inbound()
    watchdog.cancel()
    assert ws.closed_with is None


def test_websocket_keepalive_is_configured():
    """Without ping_interval, `websockets` never pings — and a connection
    killed by a NAT idle timeout stays 'open' in this process for ever, with
    connected.is_set() True and every turn dispatched into nothing."""
    assert gateway_ws.PING_INTERVAL_S > 0 and gateway_ws.PING_TIMEOUT_S > 0
    import inspect
    src = inspect.getsource(gateway_ws.GatewayClient._connect_once)
    assert "ping_interval=" in src and "ping_timeout=" in src


# --------------------------------------------------------------------------- #
# Per-session subscriptions
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_subscribing_to_a_session_is_remembered_for_the_next_connect():
    """Subscriptions live on the socket and die with it. A client that
    subscribes once after start() is, after its first reconnect, connected,
    logging healthily, and receiving no deltas at all."""
    ws = _CaptureWS()
    c = _client(ws)
    c._pending_task = None
    reader = asyncio.create_task(c._reader(ws))
    await c.subscribe_session("agent:main:t1")
    reader.cancel()
    assert c.tracked_sessions == {"agent:main:t1"}
    subs = [f for f in ws.sent if f.get("method") == "sessions.messages.subscribe"]
    assert subs and subs[0]["params"] == {"key": "agent:main:t1"}


@pytest.mark.asyncio
async def test_one_forgotten_session_does_not_cost_the_others_their_subscription():
    ws = _CaptureWS(answers={"sessions.messages.subscribe": RuntimeError("gone")})
    c = _client(ws)
    c.track_session("agent:main:a")
    c.track_session("agent:main:b")
    reader = asyncio.create_task(c._reader(ws))
    await c.resubscribe_sessions()
    reader.cancel()
    asked = [f["params"]["key"] for f in ws.sent
             if f.get("method") == "sessions.messages.subscribe"]
    assert sorted(asked) == ["agent:main:a", "agent:main:b"]


# --------------------------------------------------------------------------- #
# Naming a run: wait and abort
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_agent_wait_asks_by_run_id():
    ws = _CaptureWS(answers={"agent.wait": {"state": "final"}})
    c = _client(ws)
    reader = asyncio.create_task(c._reader(ws))
    res = await c.agent_wait("run-1", timeout_ms=5000)
    reader.cancel()
    call = next(f for f in ws.sent if f.get("method") == "agent.wait")
    assert call["params"] == {"runId": "run-1", "timeoutMs": 5000}
    assert res == {"state": "final"}


@pytest.mark.asyncio
async def test_abort_scopes_to_the_named_run():
    ws = _CaptureWS(answers={"chat.abort": {"ok": True}})
    c = _client(ws)
    reader = asyncio.create_task(c._reader(ws))
    await c.abort_run("agent:main:t1", "run-9")
    reader.cancel()
    call = next(f for f in ws.sent if f.get("method") == "chat.abort")
    assert call["params"] == {"sessionKey": "agent:main:t1", "runId": "run-9"}


@pytest.mark.asyncio
async def test_abort_falls_back_to_the_session_plane():
    """`chat.abort` is the session-scoped verb; a run the chat plane will not
    resolve is still abortable by session, and refusing to try would leave a
    turn burning tokens with no way to stop it."""
    ws = _CaptureWS(answers={"chat.abort": RuntimeError("no such run"),
                             "sessions.abort": {"ok": True}})
    c = _client(ws)
    reader = asyncio.create_task(c._reader(ws))
    await c.abort_run("agent:main:t1", "run-9")
    reader.cancel()
    assert any(f.get("method") == "sessions.abort" for f in ws.sent)


# --------------------------------------------------------------------------- #
# History envelopes
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_a_cursor_read_keeps_the_envelope_the_page_read_throws_away():
    """`history()` returns only `messages`, and the two fields a reconnect
    needs — `deltaCursor` and `inFlightRun` — live in the envelope."""
    ws = _CaptureWS(answers={"chat.history": {
        "kind": "delta", "messages": [], "deltaCursor": "c9",
        "inFlightRun": {"runId": "r1", "text": "half a sentence"}}})
    c = _client(ws)
    reader = asyncio.create_task(c._reader(ws))
    res = await c.history_from_cursor("agent:main:t1", "c1")
    tail = await c.history_tail("agent:main:t1", limit=10)
    reader.cancel()
    assert res["deltaCursor"] == "c9"
    assert tail["inFlightRun"]["runId"] == "r1"
    cursor_call = next(f for f in ws.sent if f.get("method") == "chat.history")
    assert cursor_call["params"]["cursor"] == "c1"
