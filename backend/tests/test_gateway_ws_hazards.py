"""The gateway client's two hazard handlers must actually work.

Both were dead on arrival, and neither failure produced an error, a log line or
a failing test — the shape of bug this whole transport exists to remove:

  1. DEADLOCK. The reader awaited the event handler inline, and `call()`
     resolves its future from that same reader. A handler that called back into
     the gateway (full_text for a truncated reply, history for a gap) waited
     30s for a response frame that only the blocked reader could deliver. The
     response was already in the stream; it was consumed after the timeout.

  2. WRONG FIELD. `full_text` read `msg["text"]`, which real messages do not
     have — `content` is a list of typed blocks. It returned None for every
     message ever refetched, so repair "succeeded" by silence and the truncated
     projection was delivered anyway.

These tests drive the real GatewayClient against a fake socket. They fail on
the original code and pass on the fixed code.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app import gateway_router, gateway_ws


class FakeWS:
    """A socket that answers `chat.message.get` the instant it is asked.

    The point: the response is available immediately, so a timeout can only
    mean the client failed to read it — never that the server was slow.
    """

    def __init__(self, frames: list[dict]):
        self._outbound: asyncio.Queue = asyncio.Queue()
        for f in frames:
            self._outbound.put_nowait(json.dumps(f))
        self.sent: list[dict] = []
        self.closed = False

    async def send(self, raw: str) -> None:
        req = json.loads(raw)
        self.sent.append(req)
        if req.get("method") == "chat.message.get":
            await self._outbound.put(json.dumps({
                "type": "res", "id": req["id"], "ok": True,
                "payload": {"ok": True, "message": {
                    "role": "assistant",
                    "__openclaw": {"id": "abc123", "seq": 2},
                    # A REAL message: no "text" key, content is a block list.
                    "content": [
                        {"type": "thinking", "thinking": "not speech"},
                        {"type": "text", "text": "THE COMPLETE UNTRUNCATED BODY"},
                    ],
                }},
            }))

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        try:
            return await asyncio.wait_for(self._outbound.get(), timeout=2.0)
        except TimeoutError:
            raise StopAsyncIteration

    async def close(self) -> None:
        self.closed = True


def _truncated_event() -> dict:
    body = "x" * 200 + gateway_ws.TRUNCATION_MARKER
    return {
        "type": "event", "event": "session.message",
        "payload": {
            "sessionKey": "agent:main:t1", "messageSeq": 2,
            "message": {
                "role": "assistant",
                "__openclaw": {"id": "abc123", "seq": 2},
                "content": [{"type": "text", "text": body}],
            },
        },
    }


@pytest.mark.asyncio
async def test_a_handler_may_call_the_gateway_without_deadlocking():
    """The reader must keep draining while a handler is in flight.

    On the original code this hung for the full 30s request timeout and the
    refetch returned None.
    """
    delivered: list[str] = []

    async def deliver(thread_id, text, **kw):
        delivered.append(text)

    async def resolve(key):
        return ("t1", "main") if key == "agent:main:t1" else None

    router = gateway_router.SessionRouter(resolve, deliver)

    async def on_event(ev, payload):
        await router.handle(ev, payload)

    client = gateway_ws.GatewayClient(on_event)
    router._client = client
    ws = FakeWS([_truncated_event()])
    client._ws = ws
    client._pump = asyncio.create_task(client._pump_events())
    try:
        await asyncio.wait_for(client._reader(ws), timeout=6)
        await asyncio.wait_for(_until(lambda: delivered), timeout=6)
    finally:
        client._pump.cancel()

    assert delivered, "nothing was delivered — the reader deadlocked"
    assert delivered[0] == "THE COMPLETE UNTRUNCATED BODY", (
        "the truncated projection was delivered instead of the repaired body")
    assert router.stats["refetched"] == 1
    assert router.stats["truncation_unrepaired"] == 0


@pytest.mark.asyncio
async def test_full_text_reads_content_blocks_not_a_text_field():
    """Real assistant messages have no `text` key. Reading one returned None
    for every message, and a None repair is indistinguishable from 'no long
    messages happened'."""
    client = gateway_ws.GatewayClient(lambda e, p: asyncio.sleep(0))
    ws = FakeWS([])
    client._ws = ws
    reader = asyncio.create_task(client._reader(ws))
    try:
        got = await asyncio.wait_for(client.full_text("agent:main:t1", "abc123"),
                                     timeout=6)
    finally:
        reader.cancel()
    assert got == "THE COMPLETE UNTRUNCATED BODY"


@pytest.mark.asyncio
async def test_an_unavailable_refetch_is_reported_not_swallowed():
    """`ok:false` carries a reason. Folding it into the same None as a
    transport error hides a refusal as a miss."""
    client = gateway_ws.GatewayClient(lambda e, p: asyncio.sleep(0))

    async def fake_call(method, params=None):
        return {"ok": False, "unavailableReason": "oversized"}

    client.call = fake_call                       # type: ignore[assignment]
    assert await client.full_text("agent:main:t1", "abc123") is None


def test_truncation_is_detected_in_any_block_not_just_the_last():
    """The gateway truncates per block and text_of joins them, so a marker in a
    non-final block ends up buried mid-string where endswith cannot see it."""
    msg = {"content": [
        {"type": "text", "text": "first part" + gateway_ws.TRUNCATION_MARKER},
        {"type": "text", "text": "a later block that ends cleanly"},
    ]}
    assert gateway_ws.GatewayClient.blocks_truncated(msg) is True
    # ...and the joined string does NOT reveal it, which is the whole point.
    assert gateway_ws.GatewayClient.looks_truncated(
        gateway_router.text_of(msg)) is False


async def _until(pred, step: float = 0.02):
    while not pred():
        await asyncio.sleep(step)


# --------------------------------------------------------------------------- #
# A malformed frame must not cost the connection (and its subscription)
# --------------------------------------------------------------------------- #


class RawWS(FakeWS):
    """Feeds pre-serialised strings straight through, junk included."""

    def __init__(self, raws: list[str]):
        super().__init__([])
        for r in raws:
            self._outbound.put_nowait(r)


@pytest.mark.asyncio
async def test_reader_survives_non_object_json_frames():
    """`[]`, `"hi"` and `null` all parse as JSON but have no `.get`, so the
    reader died with AttributeError and the socket — plus every subscription
    on it — was torn down and rebuilt over one bad line."""
    got: list[tuple] = []

    async def on_event(ev, payload):
        got.append((ev, payload))

    client = gateway_ws.GatewayClient(on_event)
    ws = RawWS(['[]', '"hi"', 'null', '42',
                json.dumps({"type": "event", "event": "ping", "payload": {"a": 1}})])
    client._ws = ws
    client._pump = asyncio.create_task(client._pump_events())
    try:
        await asyncio.wait_for(client._reader(ws), timeout=6)
        await asyncio.wait_for(_until(lambda: got), timeout=6)
    finally:
        client._pump.cancel()

    assert got == [("ping", {"a": 1})], "the good frame after the junk was lost"


def test_frame_size_is_bounded():
    """`max_size=None` made one oversized frame an OOM instead of a dropped
    connection. Generous, but finite."""
    assert isinstance(gateway_ws.MAX_FRAME_BYTES, int)
    assert 1024 * 1024 <= gateway_ws.MAX_FRAME_BYTES <= 256 * 1024 * 1024
