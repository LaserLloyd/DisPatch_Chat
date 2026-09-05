"""Dispatching a turn over the socket DisPatch already holds open.

DisPatch used to ask an agent a question by spawning `openclaw agent --json` —
a Node process that boots, reads the config, opens its OWN socket to the very
gateway DisPatch is already connected to, and asks. Measured on this box with
a live trace, that wrapper cost **2.3 s before the run started**, on every
turn, with an idle socket to the same gateway two feet away.

The wire contract that makes the socket path possible, and the thing these
tests pin down: an `agent` request is answered **twice on one request id** —
first a receipt (`status: "accepted"`), then the finished turn. A generic
single-shot call resolves on the receipt and hands the caller an empty
acknowledgement as if it were the reply, while the real answer arrives for a
request nobody is waiting on.

The accepted/not-accepted split is also the retry boundary: before the receipt
nothing has run and a retry is free; after it a turn is underway and a retry
double-runs a billed model call.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest

from app import gateway_ws, openclaw


class TurnWS:
    """A gateway that accepts an `agent` run, then answers it."""

    def __init__(self, *, accept=True, final=None, final_delay=0.0,
                 accept_delay=0.0, fail_accept=None):
        self._outbound: asyncio.Queue = asyncio.Queue()
        self.sent: list[dict] = []
        self._accept = accept
        self._final = final
        self._final_delay = final_delay
        self._accept_delay = accept_delay
        self._fail_accept = fail_accept
        self._tasks: list[asyncio.Task] = []
        self.closed = False

    async def send(self, raw: str) -> None:
        req = json.loads(raw)
        self.sent.append(req)
        if req.get("method") != "agent":
            return
        self._tasks.append(asyncio.create_task(self._answer(req["id"])))

    async def _answer(self, rid: str) -> None:
        if self._fail_accept is not None:
            await self._outbound.put(json.dumps({
                "type": "res", "id": rid, "ok": False,
                "error": self._fail_accept}))
            return
        if self._accept:
            if self._accept_delay:
                await asyncio.sleep(self._accept_delay)
            await self._outbound.put(json.dumps({
                "type": "res", "id": rid, "ok": True,
                "payload": {"runId": "r1", "sessionKey": "agent:beta:t1",
                            "status": "accepted", "acceptedAt": 1}}))
        if self._final is None:
            return
        if self._final_delay:
            await asyncio.sleep(self._final_delay)
        await self._outbound.put(json.dumps({
            "type": "res", "id": rid, "ok": True, "payload": self._final}))

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        try:
            return await asyncio.wait_for(self._outbound.get(), timeout=3.0)
        except TimeoutError:
            raise StopAsyncIteration

    async def close(self) -> None:
        self.closed = True


def _final_ok(text="pong"):
    return {
        "runId": "r1", "status": "ok", "summary": "completed",
        "result": {
            "payloads": [{"text": text, "mediaUrl": None}],
            "meta": {"durationMs": 4471,
                     "agentMeta": {"provider": "buildpc", "model": "m",
                                   "sessionId": "s1", "usage": {"total": 9}}},
        },
    }


async def _client_on(ws):
    client = gateway_ws.GatewayClient(lambda ev, p: asyncio.sleep(0))
    client._ws = ws
    reader = asyncio.create_task(client._reader(ws))
    return client, reader


@pytest.mark.asyncio
async def test_the_acceptance_receipt_is_not_mistaken_for_the_reply():
    """Two responses, one id. Resolving on the first returns an empty receipt."""
    ws = TurnWS(final=_final_ok("THE ACTUAL ANSWER"), final_delay=0.05)
    client, reader = await _client_on(ws)
    try:
        payload = await client.call_agent(
            {"message": "hi", "agentId": "beta"}, timeout=5)
    finally:
        reader.cancel()
    assert payload["status"] == "ok"
    reply = openclaw._parse_reply(payload, "beta", "")
    assert [p.text for p in reply.payloads] == ["THE ACTUAL ANSWER"]
    assert reply.metadata["model"] == "m"


@pytest.mark.asyncio
async def test_a_run_the_gateway_never_accepted_is_retryable():
    """Nothing ran, so the caller's backoff may safely try again.

    GatewayUnavailable is the retryable class; anything else must not be, or a
    restart mid-turn double-runs a billed model call.
    """
    ws = TurnWS(fail_accept={"code": "UNAVAILABLE", "message": "draining"})
    client, reader = await _client_on(ws)
    try:
        with pytest.raises(openclaw.GatewayUnavailable):
            await openclaw.send_via_gateway(
                client, bot_id="beta", session_key="agent:beta:t1",
                message="hi", timeout=5)
    finally:
        reader.cancel()


@pytest.mark.asyncio
async def test_no_acceptance_within_the_window_is_refused_not_hung():
    ws = TurnWS(accept=False)
    client, reader = await _client_on(ws)
    orig = gateway_ws.AGENT_ACCEPT_TIMEOUT_S
    gateway_ws.AGENT_ACCEPT_TIMEOUT_S = 0.1
    try:
        with pytest.raises(gateway_ws.GatewayRunRefused):
            await client.call_agent({"message": "hi"}, timeout=5)
    finally:
        gateway_ws.AGENT_ACCEPT_TIMEOUT_S = orig
        reader.cancel()


@pytest.mark.asyncio
async def test_an_accepted_run_that_never_finishes_is_not_retryable():
    """It may still be running on the gateway; retrying would double-run it."""
    ws = TurnWS(final=None)
    client, reader = await _client_on(ws)
    try:
        with pytest.raises(gateway_ws.GatewayRunTimeout):
            await client.call_agent({"message": "hi"}, timeout=0.2)
    finally:
        reader.cancel()

    class _Stalled:
        async def call_agent(self, params, *, timeout):
            raise gateway_ws.GatewayRunTimeout("nope")

    with pytest.raises(openclaw.AgentTimeout) as e:
        await openclaw.send_via_gateway(
            _Stalled(), bot_id="beta", session_key="agent:beta:t1",
            message="hi", timeout=5)
    assert not isinstance(e.value, openclaw.GatewayUnavailable), (
        "a run that was accepted must never be classified retryable")


@pytest.mark.asyncio
async def test_a_disconnect_mid_run_fails_the_call_instead_of_hanging():
    """Nothing else can resolve that future — the socket carrying it is gone.

    Left pending it holds the thread lock for the whole agent timeout, and the
    thread looks like it is still thinking for a quarter of an hour.
    """
    ws = TurnWS(final=None)
    client, reader = await _client_on(ws)
    call = asyncio.create_task(client.call_agent({"message": "hi"}, timeout=30))
    await asyncio.sleep(0.05)
    client._fail_pending(gateway_ws.GatewayDisconnected("socket closed"))
    try:
        with pytest.raises(gateway_ws.GatewayDisconnected):
            await asyncio.wait_for(call, timeout=2)
    finally:
        reader.cancel()


@pytest.mark.asyncio
async def test_the_request_carries_an_idempotency_key_and_never_delivers():
    """`deliver: false` for the same reason the CLI is never given --deliver:
    the reply comes back to us and is not re-sent to any messaging channel."""
    ws = TurnWS(final=_final_ok())
    client, reader = await _client_on(ws)
    try:
        await openclaw.send_via_gateway(
            client, bot_id="DS_Flash", session_key="agent:DS_Flash:t1",
            message="hi", timeout=5)
    finally:
        reader.cancel()
    params = ws.sent[0]["params"]
    assert params["deliver"] is False
    assert params["idempotencyKey"]
    assert params["sessionKey"] == "agent:DS_Flash:t1", (
        "the session key must stay byte-identical to the CLI path's, or the "
        "turn lands in a different conversation")
    assert params["agentId"] == "ds_flash", (
        "the gateway normalizes agent ids to lowercase; sending the mixed-case "
        "id risks resolving to a different agent than the session key does")


def test_agent_id_normalization_matches_the_gateways_rule():
    assert openclaw.normalize_agent_id("DS_Flash") == "ds_flash"
    assert openclaw.normalize_agent_id("Doxy") == "doxy"
    assert openclaw.normalize_agent_id("a b.c") == "a-b-c"
    assert openclaw.normalize_agent_id("") == "main"


# --- picking a transport ---------------------------------------------------

class _Sock:
    def __init__(self, up: bool):
        self.connected = asyncio.Event()
        if up:
            self.connected.set()
        self.calls = 0
        self.subscribed: list[str] = []
        self.run_ids: list[str] = []

    async def subscribe_session(self, key):
        self.subscribed.append(key)

    def track_session(self, key):
        self.subscribed.append(key)

    async def call_agent(self, params, *, timeout):
        self.calls += 1
        self.run_ids.append(params.get("idempotencyKey"))
        return _final_ok("over the socket")


@pytest.mark.asyncio
async def test_the_transport_is_chosen_per_attempt_not_once_at_boot(monkeypatch):
    """The socket can drop and come back while the app runs.

    A choice frozen at startup means a gateway restart either strands every
    turn or costs the subprocess forever after.
    """
    from app import main
    monkeypatch.setattr(main, "SETTINGS", replace(main.SETTINGS,
                                                 turn_transport="auto"))
    sock = _Sock(up=True)
    monkeypatch.setattr(main, "_gateway_client", sock, raising=False)
    main._inflight_runs.clear()
    before = dict(main._turn_transport_counts)
    reply = await main._dispatch_turn("beta", "agent:beta:t1", "hi")
    assert [p.text for p in reply.payloads] == ["over the socket"]
    assert main._turn_transport_counts["socket"] == before["socket"] + 1, (
        "/api/health must be able to say which way turns actually went")
    assert sock.subscribed == ["agent:beta:t1"], (
        "the session must be subscribed or its live deltas never arrive")
    assert sock.run_ids and sock.run_ids[0], (
        "the run must be NAMED by us: the gateway adopts the idempotency key "
        "as the runId, and a run we cannot name we cannot stream, abort, or "
        "ask agent.wait about after a disconnect")
    assert not main._inflight_runs, (
        "a turn that answered is finished; leaving it in the registry would "
        "have every reconnect chase a run that ended long ago")

    spawned = []

    async def _cli(**kw):
        spawned.append(kw)
        return openclaw.AgentReply(payloads=[openclaw.AgentPayload(text="via cli")])

    monkeypatch.setattr(main.openclaw, "send_to_agent", _cli)
    monkeypatch.setattr(main, "_gateway_client", None, raising=False)
    reply = await main._dispatch_turn("beta", "agent:beta:t1", "hi")
    assert [p.text for p in reply.payloads] == ["via cli"], (
        "with no socket the turn must still go out, over the subprocess")
    assert spawned and spawned[0]["session_key"] == "agent:beta:t1"

    # A socket that exists but is not connected is not a transport.
    monkeypatch.setattr(main, "_gateway_client", _Sock(up=False), raising=False)
    reply = await main._dispatch_turn("beta", "agent:beta:t1", "hi")
    assert [p.text for p in reply.payloads] == ["via cli"]
    assert main._turn_transport_counts["cli"] == before["cli"] + 2


@pytest.mark.asyncio
async def test_transport_0_keeps_the_old_subprocess_path(monkeypatch):
    from app import main
    monkeypatch.setattr(main, "SETTINGS", replace(main.SETTINGS,
                                                 turn_transport="0"))
    sock = _Sock(up=True)
    monkeypatch.setattr(main, "_gateway_client", sock, raising=False)

    async def _cli(**kw):
        return openclaw.AgentReply(payloads=[openclaw.AgentPayload(text="via cli")])

    monkeypatch.setattr(main.openclaw, "send_to_agent", _cli)
    reply = await main._dispatch_turn("beta", "agent:beta:t1", "hi")
    assert [p.text for p in reply.payloads] == ["via cli"]
    assert sock.calls == 0


@pytest.mark.asyncio
async def test_transport_1_refuses_rather_than_silently_spawning(monkeypatch):
    """On a box where the socket is meant to be up, falling back to a 1.1 s
    subprocess quietly is the failure — the refusal is retryable and loud."""
    from app import main
    monkeypatch.setattr(main, "SETTINGS", replace(main.SETTINGS,
                                                 turn_transport="1"))
    monkeypatch.setattr(main, "_gateway_client", None, raising=False)
    with pytest.raises(openclaw.GatewayUnavailable):
        await main._dispatch_turn("beta", "agent:beta:t1", "hi")
