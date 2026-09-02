"""Live gateway deltas: the words on screen while the model is still typing.

WHAT THIS FILE IS DEFENDING
---------------------------
The gateway emits a `chat` event stream for a run in flight. It is
``dropIfSlow``, carries no frame sequence, and is explicitly a DISPLAY channel:
the transcript event remains the only thing anything is ever persisted from.
Three properties make that safe, and each of them has a failure mode that looks
exactly like success:

1. A DROPPED DELTA COSTS NOTHING. The text a client receives is derived from
   the gateway's CUMULATIVE snapshot on every event, never accumulated from
   `deltaText` — otherwise one dropped frame silently corrupts the rest of the
   reply with nothing to compare it against.

2. A LOCKED DEVICE SEES NO MORE THAN IT WOULD HAVE. Delta text is raw model
   output: absolute paths inside `[[media:…]]`, `:react:` markers, internal
   scaffolding. The persisted copy has stripped all of it since long before
   streaming existed, and a provisional bubble must not be the hole in that.

3. EXACTLY ONE BUBBLE PER REPLY. The provisional bubble and the persisted row
   are two views of one message; if the row lands as a plain `message` frame
   the client keeps both, and the family reads the answer twice.
"""
from __future__ import annotations

import asyncio

import pytest

from app import gateway_router as gr


# --------------------------------------------------------------------------- #
# Doubles
# --------------------------------------------------------------------------- #

class _Bus:
    """Captures the frames a browser would receive."""

    def __init__(self) -> None:
        self.frames: list[dict] = []

    async def broadcast(self, frame: dict) -> None:
        self.frames.append(frame)

    def of(self, kind: str) -> list[dict]:
        return [f for f in self.frames if f.get("type") == kind]

    def text(self) -> str:
        """Reassemble what the client would be showing, chunk by chunk."""
        out = ""
        for f in self.of("stream_chunk"):
            out = f["text"] if f.get("replace") else out + f["text"]
        return out


class _Recorder:
    def __init__(self, target=("t1", "main")) -> None:
        self.target = target
        self.resolved: list[str] = []
        self.calls: list[dict] = []

    async def resolve(self, key):
        self.resolved.append(key)
        return self.target

    async def deliver(self, thread_id, text, **kw):
        self.calls.append({"thread_id": thread_id, "text": text, **kw})
        return {"id": "row-1"}


def _router(bus, rec, *, sanitize=None, opened=None, closed=None, client=None,
            is_open=None):
    return gr.SessionRouter(
        rec.resolve, rec.deliver, client=client,
        broadcast=bus.broadcast,
        sanitize=sanitize or (lambda t: t),
        open_stream=((lambda t, pid: opened.append((t, pid)))
                     if opened is not None else None),
        close_stream=(closed if closed is not None else (lambda *a: True)),
        stream_open=is_open)


def _delta(text, *, run="r1", key="agent:main:t1", replace=False, seq=1):
    frame = {"state": "delta", "runId": run, "sessionKey": key, "seq": seq,
             "deltaText": text,
             "message": {"role": "assistant",
                         "content": [{"type": "text", "text": text}]}}
    if replace:
        frame["replace"] = True
    return frame


async def _settle(router, run_id="r1"):
    """Wait out whatever timers the run armed."""
    run = router._runs.get(run_id)
    if run is not None and run.settle is not None:
        with pytest.raises(BaseException) if False else _noraise():
            await run.settle


class _noraise:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return True


# --------------------------------------------------------------------------- #
# 1. A dropped delta must cost nothing
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_chunks_are_the_difference_of_the_cumulative_snapshot():
    """The client's text must equal the gateway's, chunk arithmetic aside."""
    bus, rec = _Bus(), _Recorder()
    router = _router(bus, rec)
    for cumulative in ("The ", "The sea ", "The sea is grey."):
        await router.handle("chat", _delta(cumulative))
        await asyncio.sleep(gr.CHUNK_MIN_INTERVAL_S * 1.5)
    await router.handle("chat", {"state": "final", "runId": "r1",
                                 "sessionKey": "agent:main:t1", "seq": 9})
    assert bus.text() == "The sea is grey."
    assert bus.of("stream_start"), "the first chunk must open the bubble"


@pytest.mark.asyncio
async def test_a_dropped_delta_cannot_corrupt_the_reply():
    """`dropIfSlow` means frames GO MISSING; that is the normal case.

    Accumulating `deltaText` would leave the client permanently short of the
    dropped fragment, silently, with nothing downstream able to notice.
    Deriving from the cumulative snapshot makes the next surviving frame carry
    the repair for free.
    """
    bus, rec = _Bus(), _Recorder()
    router = _router(bus, rec)
    await router.handle("chat", _delta("one "))
    await asyncio.sleep(gr.CHUNK_MIN_INTERVAL_S * 1.5)
    # "one two " is dropped in transit; only "one two three" arrives.
    await router.handle("chat", _delta("one two three"))
    await router.handle("chat", {"state": "final", "runId": "r1",
                                 "sessionKey": "agent:main:t1", "seq": 9})
    assert bus.text() == "one two three"


@pytest.mark.asyncio
async def test_a_non_extension_is_sent_as_a_replace():
    """A model that rewrites what it already said is not an append."""
    bus, rec = _Bus(), _Recorder()
    router = _router(bus, rec)
    await router.handle("chat", _delta("draft one"))
    await asyncio.sleep(gr.CHUNK_MIN_INTERVAL_S * 1.5)
    await router.handle("chat", _delta("completely different"))
    await router.handle("chat", {"state": "final", "runId": "r1",
                                 "sessionKey": "agent:main:t1", "seq": 9})
    replaces = [f for f in bus.of("stream_chunk") if f.get("replace")]
    assert replaces, "a prefix mismatch must arrive as a replace, not an append"
    assert bus.text() == "completely different"


@pytest.mark.asyncio
async def test_the_gateways_replace_flag_is_honoured_even_on_a_prefix():
    """`replace: true` is the server telling us its own buffer was rewritten."""
    bus, rec = _Bus(), _Recorder()
    router = _router(bus, rec)
    await router.handle("chat", _delta("hello"))
    await asyncio.sleep(gr.CHUNK_MIN_INTERVAL_S * 1.5)
    await router.handle("chat", _delta("hello world", replace=True))
    await router.handle("chat", {"state": "final", "runId": "r1",
                                 "sessionKey": "agent:main:t1", "seq": 9})
    assert any(f.get("replace") for f in bus.of("stream_chunk"))
    assert bus.text() == "hello world"


@pytest.mark.asyncio
async def test_bursts_are_coalesced_but_the_last_word_is_never_lost():
    """Rate limiting may cost latency. It may never cost text."""
    bus, rec = _Bus(), _Recorder()
    router = _router(bus, rec)
    text = ""
    for word in "a b c d e f g h".split():
        text += word
        await router.handle("chat", _delta(text))
    await router.handle("chat", {"state": "final", "runId": "r1",
                                 "sessionKey": "agent:main:t1", "seq": 9})
    assert bus.text() == "abcdefgh", "the final flush must include the tail"
    assert len(bus.of("stream_chunk")) < 8, (
        "eight deltas inside one interval must not be eight fan-outs to every "
        "connected device")


# --------------------------------------------------------------------------- #
# 2. Nothing reaches a screen that the persisted copy would have stripped
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_directives_and_markers_never_reach_a_delta_frame():
    """The explicit Safe-Mode requirement, at the router's own boundary."""
    from app import main

    bus, rec = _Bus(), _Recorder()
    router = _router(bus, rec, sanitize=main._sanitize_delta)
    await router.handle("chat", _delta(
        "Here you go [[media:/srv/pics/x.png|c]] :react:morning: done"))
    await router.handle("chat", {"state": "final", "runId": "r1",
                                 "sessionKey": "agent:main:t1", "seq": 9})
    shown = bus.text()
    assert "/srv/pics" not in shown
    assert "[[media:" not in shown
    assert ":react:" not in shown
    assert "Here you go" in shown and "done" in shown


@pytest.mark.asyncio
async def test_a_directive_split_across_two_deltas_never_half_appears():
    """The reason streaming frames were kept out of Safe Mode until now.

    Stripping per CHUNK cannot see a directive that straddles a chunk
    boundary. Stripping the cumulative text and diffing can — and a directive
    that is only half WRITTEN yet is held back rather than shown.
    """
    from app import main

    bus, rec = _Bus(), _Recorder()
    router = _router(bus, rec, sanitize=main._sanitize_delta)
    for cumulative in ("See [[media:/var/home/",
                       "See [[media:/srv/pics/x.png",
                       "See [[media:/srv/pics/x.png|cap]] there"):
        await router.handle("chat", _delta(cumulative))
        await asyncio.sleep(gr.CHUNK_MIN_INTERVAL_S * 1.5)
    await router.handle("chat", {"state": "final", "runId": "r1",
                                 "sessionKey": "agent:main:t1", "seq": 9})
    every_chunk = "".join(f["text"] for f in bus.of("stream_chunk"))
    assert "/var/home" not in every_chunk, (
        "a half-written directive must be held back, not streamed and "
        "retracted")
    assert bus.text().strip() == "See  there".strip() or "there" in bus.text()


def test_the_delta_sanitizer_matches_the_persist_chokepoints_strips():
    from app import main

    raw = ("text [[doc:abc|f.pdf]] [[pic:a cat|caption]] "
           "[[media:/tmp/a.png]] :react:task_complete: tail")
    out = main._sanitize_delta(raw)
    for leak in ("[[doc:", "[[pic:", "[[media:", ":react:", "/tmp/a.png"):
        assert leak not in out, f"{leak!r} reached a live frame"
    assert "text" in out and "tail" in out


# --------------------------------------------------------------------------- #
# 3. Exactly one bubble per reply
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_the_bubble_is_opened_once_and_registered_with_the_app():
    bus, rec = _Bus(), _Recorder()
    opened: list = []
    router = _router(bus, rec, opened=opened)
    await router.handle("chat", _delta("hello"))
    await asyncio.sleep(gr.CHUNK_MIN_INTERVAL_S * 1.5)
    await router.handle("chat", _delta("hello there"))
    await router.handle("chat", {"state": "final", "runId": "r1",
                                 "sessionKey": "agent:main:t1", "seq": 9})
    assert len(bus.of("stream_start")) == 1
    assert opened == [("t1", "run:r1")], (
        "the app must know which provisional bubble the persisted row "
        "replaces, or the reply lands underneath itself")


@pytest.mark.asyncio
async def test_a_run_that_ends_with_no_row_retires_its_bubble():
    """Otherwise the client streams a reply and then waits for ever."""
    bus, rec = _Bus(), _Recorder()
    closed: list = []
    router = _router(bus, rec,
                     closed=lambda t, p: (closed.append((t, p)) or True))
    await router.handle("chat", _delta("half a sen"))
    await router.handle("chat", {"state": "error", "runId": "r1",
                                 "sessionKey": "agent:main:t1", "seq": 9,
                                 "errorMessage": "boom"})
    await asyncio.sleep(0)
    run = router._runs.get("r1")
    if run is not None and run.settle is not None:
        await run.settle
    done = bus.of("stream_done")
    assert done and done[0]["message"] is None
    assert done[0]["provisional_id"] == "run:r1"
    assert closed == [("t1", "run:r1")]
    assert router.stats["streams_orphaned"] == 1


@pytest.mark.asyncio
async def test_a_bubble_the_persisted_row_already_claimed_is_not_retired():
    """Two paths race to retire one bubble; a second stream_done blanks it."""
    bus, rec = _Bus(), _Recorder()
    router = _router(bus, rec, closed=lambda t, p: False)   # already claimed
    await router.handle("chat", _delta("done and dusted"))
    await router.handle("chat", {"state": "aborted", "runId": "r1",
                                 "sessionKey": "agent:main:t1", "seq": 9})
    run = router._runs.get("r1")
    if run is not None and run.settle is not None:
        await run.settle
    assert not bus.of("stream_done")


@pytest.mark.asyncio
async def test_a_chunk_after_the_row_landed_is_dropped_not_painted():
    """The staging orphan bubble: the persist chokepoint claims the bubble
    and sends stream_done; ~30 ms later the run's own `final` event flushes
    text that differs from the delta stream by a stripped marker, i.e. a
    `replace` chunk for a bubble the client has already retired. The
    reconnect path would rebuild it as a second copy of the whole reply."""
    bus, rec = _Bus(), _Recorder()
    live = {"open": True}
    router = _router(bus, rec, closed=lambda t, p: False,
                     is_open=lambda t, p: live["open"])
    await router.handle("chat", _delta("Hello there"))
    assert len(bus.of("stream_chunk")) == 1
    live["open"] = False                       # the row landed; bubble claimed
    # The trailing delta (cumulative text now differs from what was sent) and
    # the final arrive AFTER the persisted row's stream_done went out.
    await router.handle("chat", _delta("Hello there :react:thinking:", seq=2))
    run = router._runs.get("r1")
    if run is not None and run.flush is not None:
        await run.flush
    await router.handle("chat", {"state": "final", "runId": "r1",
                                 "sessionKey": "agent:main:t1", "seq": 3})
    await _settle(router)
    assert len(bus.of("stream_chunk")) == 1, "late replace must not be sent"
    assert router.stats["late_chunks_dropped"] == 1
    assert "r1" not in router._runs, "the run is retired, not left to settle"
    assert not bus.of("stream_done")


@pytest.mark.asyncio
async def test_a_first_chunk_never_consults_the_open_check():
    """Before stream_start nothing has been claimed, so an is_open that says
    False (nothing registered yet) must not suppress the opening chunk."""
    bus, rec = _Bus(), _Recorder()
    router = _router(bus, rec, is_open=lambda t, p: False)
    await router.handle("chat", _delta("first words"))
    assert len(bus.of("stream_start")) == 1
    assert router.stats["late_chunks_dropped"] == 0


# --------------------------------------------------------------------------- #
# 4. Status, tools, and sessions that are not ours
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_a_startup_phase_becomes_a_turn_status_frame():
    bus, rec = _Bus(), _Recorder()
    router = _router(bus, rec)
    await router.handle("chat", {"state": "status", "phase": "starting_model",
                                 "runId": "r1", "sessionKey": "agent:main:t1",
                                 "seq": 1})
    assert bus.of("turn_status")[0]["phase"] == "starting_model"
    assert bus.of("turn_status")[0]["bot_id"] == "main", (
        "Safe Mode scopes every frame by bot; an unattributed one is dropped")


@pytest.mark.asyncio
async def test_a_tool_call_becomes_a_turn_status_frame():
    bus, rec = _Bus(), _Recorder()
    router = _router(bus, rec)
    await router.handle("session.tool", {
        "runId": "r1", "sessionKey": "agent:main:t1", "stream": "tool",
        "data": {"phase": "start", "name": "brave_search"}})
    assert bus.of("turn_status")[0]["phase"] == "tool:brave_search"


@pytest.mark.asyncio
async def test_a_foreign_session_is_resolved_once_and_then_silent():
    """The subscription is a firehose. Most runs on this box are not ours, and
    re-resolving each delta would put a database read on the event loop for
    every frame of every session."""
    bus = _Bus()
    rec = _Recorder(target=None)
    router = _router(bus, rec)
    for i in range(20):
        await router.handle("chat", _delta("x" * i, key="agent:other:zzz"))
    assert bus.frames == []
    assert len(rec.resolved) == 1, (
        f"resolved {len(rec.resolved)} times for one run — the negative "
        "answer has to be cached too")


@pytest.mark.asyncio
async def test_a_delta_never_persists_anything():
    """Deltas are display-only. The transcript event is the only source."""
    bus, rec = _Bus(), _Recorder()
    router = _router(bus, rec)
    await router.handle("chat", _delta("words words words"))
    await router.handle("chat", {"state": "final", "runId": "r1",
                                 "sessionKey": "agent:main:t1", "seq": 9})
    assert rec.calls == []
    assert router.stats["delivered"] == 0


# --------------------------------------------------------------------------- #
# 5. Reconnect
# --------------------------------------------------------------------------- #

class _HistClient:
    """A gateway that answers the two catch-up reads."""

    def __init__(self, *, cursor_result=None, tail=None):
        self.cursor_result = cursor_result or {"kind": "reset"}
        self.tail = tail or {"messages": [], "deltaCursor": "c2"}
        self.cursor_calls: list[str] = []
        self.tail_calls: list[str] = []

    async def history_from_cursor(self, key, cursor):
        self.cursor_calls.append(cursor)
        return self.cursor_result

    async def history_tail(self, key, *, limit=50):
        self.tail_calls.append(key)
        return self.tail

    async def history(self, key, *, limit=50, offset=0):
        return []

    @staticmethod
    def blocks_truncated(_m):
        return False


def _hist_msg(text, mid, seq):
    return {"role": "assistant", "content": [{"type": "text", "text": text}],
            "__openclaw": {"id": mid, "seq": seq}}


@pytest.mark.asyncio
async def test_a_delta_cursor_is_a_forward_catch_up_not_a_backward_walk():
    bus, rec = _Bus(), _Recorder()
    client = _HistClient(cursor_result={
        "kind": "delta", "deltaCursor": "c9",
        "messages": [_hist_msg("missed while offline", "m1", 5)]})
    router = _router(bus, rec, client=client)
    router._record_cursor("agent:main:t1", "c1")
    await router.catch_up("agent:main:t1")
    assert client.cursor_calls == ["c1"]
    assert client.tail_calls == [], "a served cursor needs no tail read"
    assert [c["text"] for c in rec.calls] == ["missed while offline"]
    assert router.cursor_for("agent:main:t1") == "c9", (
        "the cursor must advance or the next reconnect replays the same window")


@pytest.mark.asyncio
async def test_a_reset_cursor_falls_back_rather_than_giving_up():
    """`reset` is the gateway saying the cursor is too old — not an error."""
    bus, rec = _Bus(), _Recorder()
    client = _HistClient(cursor_result={"kind": "reset"},
                         tail={"messages": [_hist_msg("recovered", "m2", 3)],
                               "deltaCursor": "c5"})
    router = _router(bus, rec, client=client)
    router._record_cursor("agent:main:t1", "stale")
    await router.catch_up("agent:main:t1")
    assert router.stats["cursor_resets"] == 1
    assert [c["text"] for c in rec.calls] == ["recovered"]


@pytest.mark.asyncio
async def test_a_session_with_no_cursor_at_all_is_no_longer_skipped():
    """The one case a reconnect exists for.

    A turn accepted a millisecond before the socket dropped has produced no
    transcript event, so it has no seq cursor — and `resync` used to `continue`
    on exactly that, declining to repair the run it was there to repair.
    """
    bus, rec = _Bus(), _Recorder()
    client = _HistClient(tail={"messages": [_hist_msg("the lost reply", "m3", 1)],
                              "deltaCursor": "c1"})
    router = _router(bus, rec, client=client)
    await router.resync(["agent:main:t1"])          # unforced: still a no-op
    assert rec.calls == []
    await router.resync(["agent:main:t1"], force=True)
    assert [c["text"] for c in rec.calls] == ["the lost reply"]


@pytest.mark.asyncio
async def test_a_still_running_turn_gets_its_bubble_back_after_a_reconnect():
    """The client's screen stopped mid-sentence when the socket died."""
    bus, rec = _Bus(), _Recorder()
    client = _HistClient(tail={
        "messages": [], "deltaCursor": "c1",
        "inFlightRun": {"runId": "r7", "text": "still typing this out"}})
    router = _router(bus, rec, client=client)
    await router.catch_up("agent:main:t1", force=True)
    chunks = bus.of("stream_chunk")
    assert chunks and chunks[0]["replace"] is True, (
        "the client's buffer is unknown after a drop; only a whole-text "
        "replace is correct from either starting point")
    assert chunks[0]["text"] == "still typing this out"
    assert bus.of("stream_start")[0]["message_id"] == "run:r7"


# --------------------------------------------------------------------------- #
# 6. The truncation refetch no longer stalls every other session
# --------------------------------------------------------------------------- #

class _SlowRefetchClient:
    def __init__(self):
        self.gate = asyncio.Event()

    @staticmethod
    def blocks_truncated(message):
        return "...(truncated)..." in gr.text_of(message)

    async def full_text(self, key, mid):
        await self.gate.wait()
        return "the complete answer"

    async def history(self, key, *, limit=50, offset=0):
        return []


@pytest.mark.asyncio
async def test_a_truncation_refetch_does_not_block_the_event_pump():
    """One long reply on one thread used to stall delivery for every session.

    `full_text` is a gateway round trip and it was awaited on the SINGLE event
    consumer — so while it ran, no other session's messages were processed and
    the bounded queue filled (which is what `dropped_local` counts).
    """
    bus, rec = _Bus(), _Recorder()
    client = _SlowRefetchClient()
    router = _router(bus, rec, client=client)
    truncated = {"role": "assistant", "__openclaw": {"id": "big", "seq": 1},
                 "content": [{"type": "text", "text": "x" * 10 + "...(truncated)..."}]}
    await router.handle("session.message", {
        "sessionKey": "agent:main:t1", "messageSeq": 1, "message": truncated})
    # The pump is free RIGHT NOW, with the refetch still in flight.
    assert router.stats["refetch_pending"] == 1
    await router.handle("session.message", {
        "sessionKey": "agent:main:t1", "messageSeq": 2,
        "message": {"role": "assistant", "__openclaw": {"id": "small", "seq": 2},
                    "content": [{"type": "text", "text": "a later reply"}]}})
    assert [c["text"] for c in rec.calls] == ["a later reply"]
    client.gate.set()
    await router.drain_detached()
    assert "the complete answer" in [c["text"] for c in rec.calls]
    assert router.stats["refetched"] == 1


def test_the_holdback_only_catches_real_marker_prefixes():
    """A held-back tail is a one-frame delay; holding back ordinary text is a
    visible stutter on every reply that happens to contain a colon."""
    from app import main

    assert main._sanitize_delta("the ratio is 3:2") == "the ratio is 3:2"
    # (Trailing whitespace goes the same way it does at persist time; the
    # space returns in the next chunk's diff, so no text is ever lost.)
    assert main._sanitize_delta("Note: ") == "Note:"
    # Genuine prefixes ARE held, because completing them would otherwise put a
    # marker (or an absolute path) on screen and then retract it.
    assert main._sanitize_delta("done :rea").rstrip() == "done"
    assert main._sanitize_delta("see [[med").rstrip() == "see"
