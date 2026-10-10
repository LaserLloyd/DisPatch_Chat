"""What a gateway (OpenClaw) bot leaves behind when its turn is aborted.

C-§B.2: "what OpenClaw persists on `chat.abort` is documented by T2's contract
test, never assumed". Voice chat's `abort_turn(keep_partial=True)` returns "the
id of whatever the gateway's abort persisted (None when nothing)" for gateway
bots, and the session hangs its `note_heard` on that row — so this file pins
down what that row is.

Documented behaviour of OpenClaw 2026.9.9 (read from its dist, then observed
live by ``test_live_gateway_abort`` below, see ``LIVE_OBSERVATION``):

1. ``chat.abort {sessionKey, runId}`` takes the run's buffered live text
   (``chatRunState.resolveBuffer(runId, {final: true})`` — the same text the
   ``chat`` deltas carried) and broadcasts ``chat {state: "aborted",
   stopReason: "rpc", message: {role: "assistant", content: [{type: "text",
   text: <buffer>}]}}`` (``chat-abort-*.mjs: broadcastChatAborted``). With an
   empty buffer ``message`` is absent.
2. When the buffer is non-empty it APPENDS that text to the session transcript
   as an ordinary assistant message (``stopReason: "stop"``, plus
   ``openclawAbort {aborted: true, origin: "rpc", runId}``, idempotency key
   ``<runId>:assistant``; ``chat-aborted-partial-*.mjs: captureAbortedPartial``
   → ``chat-transcript-persistence-*.mjs: persistAbortedPartial``). Transcript
   writes publish ``session.message`` (``server-session-events-*.mjs``), so the
   partial reaches DisPatch like any reply.
3. An empty buffer persists nothing.

DisPatch's side (``gateway_router.SessionRouter``), tested offline here
against exactly those frames: the ``aborted`` chat event retires the
provisional bubble at once (``stream_done {message: None}``) and persists
nothing; the transcript's ``session.message`` then lands as ONE assistant row
(``source_id = gw:<sessionKey>:<__openclaw.id>``) holding the streamed prefix.
So for a gateway bot the row `abort_turn` should return is the row that
``session.message`` creates — it arrives AFTER the abort call returns, and
when nothing had streamed there is no row at all.

The live probe is opt-in (``DISPATCH_CONTRACT_GATEWAY=1``): it runs one short
turn on a throwaway session key whose tag classifies as ``other`` (never
mirrored into a DisPatch thread), aborts it mid-stream and reads the
transcript back. Needs the gateway token (``OPENCLAW_GATEWAY_TOKEN`` or
``~/.openclaw/openclaw.json``; never printed). ``DISPATCH_CONTRACT_AGENT``
picks the agent (default ``ds_flash``); ``DISPATCH_CONTRACT_OUT`` writes the
observation as JSON.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
import uuid
from pathlib import Path

import pytest

from app import gateway_router

SESSION = "agent:main:3f1c2a10-5b7e-4c1d-9e8f-0a1b2c3d4e5f"
RUN = "7d9c0e2a6b5f4e3d8c1b0a9f8e7d6c5b"
PARTIAL = "You have two things this afternoon. The dentist is at three,"

#: The last live run (2026-10-10, OpenClaw 2026.9.9, a fast worker agent),
#: recorded by hand from its DISPATCH_CONTRACT_OUT file:
#: 62 characters had streamed (M3 thought for ~10 s, then sent text) when
#: `chat.abort` answered {ok: true, aborted: true}; within 12 ms a `chat`
#: event {state: "aborted", stopReason: "rpc"} carried those 62 characters;
#: a `session.message` (role assistant, 62 chars, stopReason "stop",
#: openclawAbort {aborted: true, origin: "rpc", runId}, with an __openclaw id)
#: followed, and `chat.history` held the same message. An earlier run where
#: the reply had already finished answered {ok: true, aborted: false} and the
#: complete reply was the only assistant message (nothing extra persisted).
LIVE_OBSERVATION = ("aborted -> chat{aborted, rpc, buffer} + transcript assistant row "
                    "(stopReason stop, openclawAbort) == the streamed buffer")


def _router(delivered: list, frames: list, provisional: set):
    async def resolve(session_key):
        return ("thread-1", "main") if session_key == SESSION else None

    async def deliver(thread_id, text, source_id=None, created_at=None, bot_id=None,
                      live=True, **kw):
        delivered.append({"thread_id": thread_id, "text": text, "source_id": source_id,
                          "live": live})
        return {"id": f"row-{len(delivered)}"}

    async def broadcast(frame):
        frames.append(frame)

    def open_stream(tid, prov):
        provisional.add(prov)

    def close_stream(tid, prov):
        if prov in provisional:
            provisional.discard(prov)
            return True
        return False

    return gateway_router.SessionRouter(
        resolve, deliver, broadcast=broadcast, open_stream=open_stream,
        close_stream=close_stream, stream_open=lambda tid, prov: prov in provisional)


def _delta(text: str, seq: int) -> dict:
    return {"runId": RUN, "sessionKey": SESSION, "seq": seq, "state": "delta",
            "deltaText": text, "replace": True,
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def _aborted(text: str | None) -> dict:
    out = {"runId": RUN, "sessionKey": SESSION, "seq": 9, "state": "aborted",
           "stopReason": "rpc"}
    if text:
        out["message"] = {"role": "assistant", "content": [{"type": "text", "text": text}],
                          "timestamp": 1_760_000_000_000}
    return out


def _transcript_partial(text: str) -> dict:
    return {"sessionKey": SESSION, "messageSeq": 2, "runId": RUN, "message": {
        "role": "assistant", "content": [{"type": "text", "text": text}],
        "stopReason": "stop", "timestamp": 1_760_000_000_500,
        "openclawAbort": {"aborted": True, "origin": "rpc", "runId": RUN},
        "__openclaw": {"id": "a1b2c3d4"}}}


async def test_an_aborted_stream_is_retired_and_its_transcript_partial_becomes_one_row():
    delivered, frames, provisional = [], [], set()
    router = _router(delivered, frames, provisional)
    await router.handle("chat", _delta("You have two things", 1))
    await router.handle("chat", _delta(PARTIAL, 2))
    await router.handle("chat", _aborted(PARTIAL))
    await asyncio.sleep(0.05)              # the settle task (immediate for "aborted")
    # The chat event persists NOTHING; it only retires the bubble.
    assert delivered == []
    done = [f for f in frames if f["type"] == "stream_done"]
    assert len(done) == 1 and done[0]["message"] is None
    assert done[0]["provisional_id"] == f"run:{RUN}"
    shown = ""
    for f in frames:
        if f["type"] == "stream_chunk":
            shown = f["text"] if f.get("replace") else shown + f["text"]
    assert shown == PARTIAL                # what the chat (and a voice session) saw
    # The transcript append arrives as session.message: exactly one row, the
    # streamed prefix, with the gateway's stable identity.
    await router.handle("session.message", _transcript_partial(PARTIAL))
    assert delivered == [{"thread_id": "thread-1", "text": PARTIAL,
                          "source_id": f"gw:{SESSION}:a1b2c3d4", "live": True}]
    # Replayed (a backfill after a reconnect): identity makes it a no-op at
    # the funnel; the router hands it on with the SAME source id.
    await router.handle("session.message", _transcript_partial(PARTIAL))
    assert {d["source_id"] for d in delivered} == {f"gw:{SESSION}:a1b2c3d4"}


async def test_an_abort_before_any_text_leaves_nothing():
    delivered, frames, provisional = [], [], set()
    router = _router(delivered, frames, provisional)
    await router.handle("chat", _aborted(None))
    await asyncio.sleep(0.05)
    assert delivered == [] and frames == []


# --------------------------------------------------------------------------- #
# Live probe (opt-in)
# --------------------------------------------------------------------------- #

PROMPT = ("Write the numbers from one to three hundred as words, one per line, and "
          "nothing else. Do not use any tools.")


def _text(message: dict | None) -> str:
    return gateway_router.text_of(message) if isinstance(message, dict) else ""


@pytest.mark.skipif(os.environ.get("DISPATCH_CONTRACT_GATEWAY") != "1",
                    reason="live gateway probe: set DISPATCH_CONTRACT_GATEWAY=1")
async def test_live_gateway_abort():
    from app import gateway_ws

    agent = os.environ.get("DISPATCH_CONTRACT_AGENT", "main")
    key = f"agent:{agent}:voice-contract-{uuid.uuid4().hex[:12]}"
    from app import openclaw
    assert openclaw.mirror_kind(key) == "other"          # never mirrored into DisPatch
    run_id = uuid.uuid4().hex
    events: list[tuple[float, str, dict]] = []
    t0 = time.monotonic()

    async def on_event(name, payload):
        if isinstance(payload, dict) and payload.get("sessionKey") in (key, None):
            events.append((round(time.monotonic() - t0, 3), name, payload))

    client: gateway_ws.GatewayClient

    async def on_connect():
        await client.subscribe_sessions()
        await client.resubscribe_sessions()

    client = gateway_ws.GatewayClient(on_event, on_connect=on_connect)
    await client.start()
    try:
        await asyncio.wait_for(client.connected.wait(), 15)
        await client.subscribe_session(key)
        turn = asyncio.create_task(client.call_agent({
            "message": PROMPT, "agentId": agent, "sessionKey": key, "deliver": False,
            "timeout": 90, "idempotencyKey": run_id, "cleanupBundleMcpOnRunEnd": True},
            timeout=120))

        def streamed() -> str:
            texts = [_text(p.get("message")) for _, n, p in events
                     if n == "chat" and p.get("state") == "delta"]
            return texts[-1] if texts else ""

        deadline = time.monotonic() + 60
        while len(streamed()) < 40 and time.monotonic() < deadline and not turn.done():
            await asyncio.sleep(0.05)
        before_abort = streamed()
        abort_res = await client.abort_run(key, run_id)
        await asyncio.sleep(6)                       # transcript append + session.message
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(turn, 5)
        hist = await client.call("chat.history", {"sessionKey": key, "limit": 10})
    finally:
        await client.stop()

    aborted = [p for _, n, p in events if n == "chat" and p.get("state") == "aborted"]
    sm = [p for _, n, p in events if n == "session.message"
          and (p.get("message") or {}).get("role") == "assistant"]
    msgs = (hist or {}).get("messages") or []
    hist_assistant = [m for m in msgs if isinstance(m, dict) and m.get("role") == "assistant"]
    obs = {
        "agent": agent, "streamed_chars_before_abort": len(before_abort),
        "abort_result": {k: v for k, v in (abort_res or {}).items() if k != "runIds"},
        "aborted_event": [{"stopReason": p.get("stopReason"),
                           "message_chars": len(_text(p.get("message")))} for p in aborted],
        "session_message_assistant": [{
            "chars": len(_text(p.get("message"))),
            "stopReason": (p.get("message") or {}).get("stopReason"),
            "openclawAbort": (p.get("message") or {}).get("openclawAbort"),
            "has_id": bool(((p.get("message") or {}).get("__openclaw") or {}).get("id")),
            "equals_aborted_text": bool(aborted) and _text(p.get("message")) == _text(
                aborted[0].get("message"))} for p in sm],
        "history_assistant": [{"chars": len(_text(m)), "stopReason": m.get("stopReason"),
                               "openclawAbort": m.get("openclawAbort")}
                              for m in hist_assistant],
        "event_names": sorted({n for _, n, _ in events}),
        "timeline": [[t, n, p.get("state") or p.get("stream") or p.get("phase"),
                      len(_text(p.get("message")))] for t, n, p in events
                     if n in ("chat", "session.message", "agent")][:80],
    }
    out = os.environ.get("DISPATCH_CONTRACT_OUT")
    if out:
        Path(out).write_text(json.dumps(obs, indent=1))
    assert before_abort, "nothing streamed before the abort; probe inconclusive"
    assert aborted, "no `aborted` chat event"
    aborted_text = _text(aborted[0].get("message"))
    assert aborted_text.startswith(before_abort[:20])
    # The transcript keeps the partial as an assistant message, published as
    # session.message (the row DisPatch creates).
    assert sm and _text(sm[-1].get("message")) == aborted_text
    assert hist_assistant and _text(hist_assistant[-1]) == aborted_text


