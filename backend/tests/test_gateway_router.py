"""The gateway router: which thread, what text, exactly once, in order.

These are the properties the transport must satisfy. They are deliberately
transport-shaped — sanitizers, reaction markers and Safe Mode are NOT tested
here because the router does not touch them; it hands text to the existing
funnel, which is the point.
"""
from __future__ import annotations

import pytest

from app import gateway_router as gr


def _msg(text, *, role="assistant", mid="abc123", seq=1, ts=1786258883110):
    return {
        "role": role,
        "content": [{"type": "text", "text": text}],
        "timestamp": ts,
        "__openclaw": {"id": mid, "seq": seq},
    }


# --- reading the message ---------------------------------------------------

def test_text_comes_from_content_blocks_not_a_text_field():
    """`message["text"]` does not exist on a gateway message.

    Reading it returned "" for every reply — a silent total loss that looks
    exactly like an agent saying nothing. Verified against a real frame:
    content is [{"type":"text","text":"SHAPE-OK"}].
    """
    assert gr.text_of(_msg("SHAPE-OK")) == "SHAPE-OK"


def test_multiple_text_blocks_are_joined_in_order():
    m = {"role": "assistant", "content": [
        {"type": "text", "text": "first"},
        {"type": "thinking", "text": "ignored"},
        {"type": "text", "text": "second"},
    ]}
    assert gr.text_of(m) == "first\n\nsecond"


def test_non_text_blocks_are_not_spoken():
    m = {"role": "assistant", "content": [{"type": "tool_use", "text": "ls -la"}]}
    assert gr.text_of(m) == ""


def test_a_plain_string_content_still_works():
    assert gr.text_of({"content": "plain"}) == "plain"


# --- identity --------------------------------------------------------------

def test_identity_comes_from_openclaw_id():
    """Stable across the live event and a later chat.history read — the
    property that makes backfill idempotent instead of a replay."""
    assert (gr.source_id_of(_msg("x", mid="12e175eb"), "agent:main:abc")
            == "gw:agent:main:abc:12e175eb")


def test_the_same_message_gets_the_same_identity_from_either_path():
    """The live event and the history re-read must agree, or backfill stops
    being idempotent and becomes the replay it exists to prevent."""
    live = gr.source_id_of(_msg("x", mid="12e175eb"), "agent:main:abc")
    replayed = gr.source_id_of(_msg("x", mid="12e175eb"), "agent:main:abc")
    assert live == replayed


def test_identity_is_scoped_to_its_session():
    """__openclaw.id is 8 hex characters — 32 bits. At 100k messages there is a
    69% chance two of them are identical, and a collision here does not raise or
    log: source_id_seen() returns True and the reply is silently dropped.

    The gateway only promises the id is unique within a session, so two
    different sessions sharing an id must NOT be treated as the same message."""
    a = gr.source_id_of(_msg("first", mid="deadbeef"), "agent:main:one")
    b = gr.source_id_of(_msg("second", mid="deadbeef"), "agent:scout:two")
    assert a != b, "a 32-bit id collision across sessions would drop a message"


def test_a_message_with_no_identity_is_not_given_a_fake_one():
    """None means "dedup by content instead" — inventing an id would make two
    different messages collide."""
    assert gr.source_id_of({"role": "assistant", "content": []}) is None


def test_original_timestamp_is_preserved():
    """A backfilled answer must land where the conversation happened, not at
    the bottom of the thread stamped now."""
    got = gr.created_at_of(_msg("x", ts=1786258883110))
    assert got and got.startswith("2026-"), got
    assert gr.created_at_of({"timestamp": 0}) is None


# --- routing ---------------------------------------------------------------

class _Recorder:
    def __init__(self, known=None):
        self.known = known or {"agent:main:t1": ("t1", "main")}
        self.calls = []
        self.seen_ids = set()

    async def resolve(self, key):
        return self.known.get(key)

    async def deliver(self, thread_id, text, **kw):
        """Mirrors the real funnel's contract: the stored row, or None.

        The router reads that return value to tell a repair that recovered a
        reply from one that found nothing missing, so a recorder that always
        returned None would make every backfill look like a no-op.
        """
        call = {"thread": thread_id, "text": text, **kw}
        self.calls.append(call)
        if kw.get("source_id") in self.seen_ids:
            return None
        if kw.get("source_id"):
            self.seen_ids.add(kw["source_id"])
        return call


@pytest.mark.asyncio
async def test_a_users_own_message_is_not_re_delivered():
    """DisPatch persists the user's message before dispatching the turn.
    Echoing it back shows the family their own question below the answer —
    an ordering bug observed live."""
    r = _Recorder()
    router = gr.SessionRouter(r.resolve, r.deliver)
    await router.handle("session.message", {
        "sessionKey": "agent:main:t1", "messageSeq": 1,
        "message": _msg("my question", role="user")})
    assert r.calls == []


@pytest.mark.asyncio
async def test_a_session_dispatch_does_not_track_is_ignored():
    """The subscription is a firehose over every session on the box. Most
    events are legitimately not ours; treating them as ours would import
    other people's conversations into the family chat."""
    r = _Recorder()
    router = gr.SessionRouter(r.resolve, r.deliver)
    await router.handle("session.message", {
        "sessionKey": "agent:someone:else", "messageSeq": 1,
        "message": _msg("not ours")})
    assert r.calls == []
    assert router.stats["skipped_not_ours"] == 1


@pytest.mark.asyncio
async def test_an_assistant_reply_is_delivered_with_identity_and_time():
    r = _Recorder()
    router = gr.SessionRouter(r.resolve, r.deliver)
    await router.handle("session.message", {
        "sessionKey": "agent:main:t1", "messageSeq": 2,
        "message": _msg("the answer", mid="deadbeef")})
    assert len(r.calls) == 1
    c = r.calls[0]
    assert c["thread"] == "t1" and c["text"] == "the answer"
    assert c["source_id"] == "gw:agent:main:t1:deadbeef"
    assert c["created_at"] and c["bot_id"] == "main"


# --- gaps ------------------------------------------------------------------

class _FakeClient:
    def __init__(self, history=None, full=None):
        self._history = history or []
        self._full = full
        self.history_calls = []

    @staticmethod
    def looks_truncated(text):
        return text.rstrip().endswith("...(truncated)...")

    @staticmethod
    def blocks_truncated(message):
        # Mirrors GatewayClient: per-block, because the gateway truncates each
        # block and the joined string can hide a marker in a non-final one.
        content = message.get("content")
        if isinstance(content, str):
            return content.rstrip().endswith("...(truncated)...")
        if isinstance(content, list):
            return any("...(truncated)..." in (b.get("text") or "")
                       for b in content if isinstance(b, dict))
        return False

    async def full_text(self, session_key, message_id):
        return self._full

    async def history(self, session_key, *, limit=50, offset=0):
        self.history_calls.append({"limit": limit, "offset": offset})
        return self._history


@pytest.mark.asyncio
async def test_a_sequence_gap_triggers_a_backfill():
    """messageSeq is the ONLY loss signal: the event is best-effort
    (dropIfSlow) and carries no frame sequence, so the generic gap detector
    does not apply to it."""
    missed = _msg("the message that was dropped", mid="lost1", seq=2)
    client = _FakeClient(history=[missed])
    r = _Recorder()
    router = gr.SessionRouter(r.resolve, r.deliver, client=client)

    await router.handle("session.message", {
        "sessionKey": "agent:main:t1", "messageSeq": 1, "message": _msg("one", mid="m1")})
    await router.handle("session.message", {
        "sessionKey": "agent:main:t1", "messageSeq": 4, "message": _msg("four", mid="m4")})


    # The repair is deferred off the reply path (see GAP_BACKFILL_DELAY_S);
    # flush_gaps() is what makes it observable without a sleep in a test.
    await router.flush_gaps()

    assert router.stats["gaps"] == 1, "the skip from 1 to 4 went unnoticed"
    assert client.history_calls, "no backfill was attempted"
    assert client.history_calls[0]["offset"] == 0, (
        "offset must ALWAYS be passed — omitting it switches the gateway to a "
        "tail read whose sequence numbers are window-relative garbage")
    texts = [c["text"] for c in r.calls]
    assert "the message that was dropped" in texts


@pytest.mark.asyncio
async def test_no_gap_means_no_backfill():
    client = _FakeClient()
    r = _Recorder()
    router = gr.SessionRouter(r.resolve, r.deliver, client=client)
    for i in (1, 2, 3):
        await router.handle("session.message", {
            "sessionKey": "agent:main:t1", "messageSeq": i,
            "message": _msg(f"m{i}", mid=f"m{i}")})
    assert router.stats["gaps"] == 0
    assert client.history_calls == []


# --- truncation ------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_truncated_projection_is_refetched_in_full():
    """The single most likely way this migration makes things worse.

    session.message projects text through an 8000-char cap with no config
    knob; the CLI path had none. Unhandled, a long reply silently stops
    mid-sentence with no error, no log and no failing test — and the person
    who notices is a family member.
    """
    long_text = "x" * 8000 + "\n...(truncated)..."
    client = _FakeClient(full="the complete answer, all of it")
    r = _Recorder()
    router = gr.SessionRouter(r.resolve, r.deliver, client=client)

    await router.handle("session.message", {
        "sessionKey": "agent:main:t1", "messageSeq": 1,
        "message": _msg(long_text, mid="big")})
    # The refetch is a gateway round trip, so it runs OFF the single event
    # consumer — see test_a_truncation_refetch_does_not_block_the_event_pump.
    await router.drain_detached()

    assert router.stats["refetched"] == 1
    assert r.calls[0]["text"] == "the complete answer, all of it"


@pytest.mark.asyncio
async def test_a_failed_refetch_keeps_the_projection_rather_than_dropping_it():
    """Degrade to a shortened message, never to silence."""
    client = _FakeClient(full=None)
    r = _Recorder()
    router = gr.SessionRouter(r.resolve, r.deliver, client=client)
    await router.handle("session.message", {
        "sessionKey": "agent:main:t1", "messageSeq": 1,
        "message": _msg("y" * 10 + "\n...(truncated)...", mid="big2")})
    await router.drain_detached()
    assert len(r.calls) == 1 and r.calls[0]["text"].endswith("...(truncated)...")
    assert router.stats["refetch_failed"] == 1, (
        "a repair that could not repair must SAY so; a clean refetched:0 "
        "reads as good news rather than as a path that never worked")


@pytest.mark.asyncio
async def test_backfilled_deliveries_are_marked_not_live():
    """The deliverer must be able to tell a replay from a live event.

    A backfilled message is history being replayed: it may sit behind later
    turns (trailing-run content dedup cannot see it) and it may carry
    `:react:` markers whose one-shot images were already spent. Delivering it
    indistinguishably from a live event hands both problems downstream with no
    way to solve them there.
    """
    missed = _msg("the message that was dropped", mid="lost1", seq=2)
    client = _FakeClient(history=[missed])
    r = _Recorder()
    router = gr.SessionRouter(r.resolve, r.deliver, client=client)

    await router.handle("session.message", {
        "sessionKey": "agent:main:t1", "messageSeq": 1, "message": _msg("one", mid="m1")})
    await router.handle("session.message", {
        "sessionKey": "agent:main:t1", "messageSeq": 4, "message": _msg("four", mid="m4")})

    # The repair is deferred off the reply path (see GAP_BACKFILL_DELAY_S);
    # flush_gaps() is what makes it observable without a sleep in a test.
    await router.flush_gaps()
    by_text = {c["text"]: c for c in r.calls}

    assert by_text["one"]["live"] is True
    assert by_text["four"]["live"] is True
    assert by_text["the message that was dropped"]["live"] is False


# --- error placeholders ------------------------------------------------------
#
# The gateway emits an assistant message with stopReason "error" (projected to
# "The agent run failed before producing a reply.") the moment a turn errors.
# For a context overflow it then auto-compacts, retries the SAME run and deletes
# that synthetic error from its own transcript. Persisting it on arrival turned
# 14 of 17 transient overflows (2026-08-16..19) into permanent failure bubbles.

def _err(mid="e0000001", seq=3):
    m = _msg("The agent run failed before producing a reply.", mid=mid, seq=seq)
    m["stopReason"] = "error"
    return m


@pytest.mark.asyncio
async def test_an_error_placeholder_is_not_delivered_immediately(monkeypatch):
    monkeypatch.setattr(gr, "ERROR_PLACEHOLDER_GRACE_S", 0.2)
    r = _Recorder()
    router = gr.SessionRouter(r.resolve, r.deliver)
    await router.handle("session.message", {
        "sessionKey": "agent:main:t1", "messageSeq": 3, "message": _err()})
    assert r.calls == [], "a stopReason=error message must be held, not shown"
    assert router.stats["error_held"] == 1


@pytest.mark.asyncio
async def test_a_later_assistant_message_suppresses_the_held_error(monkeypatch):
    """The run recovered (compaction + retry) — the placeholder was the
    gateway's transient, not the reply. A tool-call assistant message with no
    text is enough: it proves the run is alive."""
    import asyncio
    monkeypatch.setattr(gr, "ERROR_PLACEHOLDER_GRACE_S", 0.2)
    r = _Recorder()
    router = gr.SessionRouter(r.resolve, r.deliver)
    await router.handle("session.message", {
        "sessionKey": "agent:main:t1", "messageSeq": 3, "message": _err()})
    tool_call = {"role": "assistant", "timestamp": 1786258883110,
                 "content": [{"type": "toolCall", "name": "exec"}],
                 "__openclaw": {"id": "aa000001", "seq": 4}}
    await router.handle("session.message", {
        "sessionKey": "agent:main:t1", "messageSeq": 4, "message": tool_call})
    await router.handle("session.message", {
        "sessionKey": "agent:main:t1", "messageSeq": 6,
        "message": _msg("real answer", mid="bb000001", seq=6)})
    await asyncio.sleep(0.4)
    assert [c["text"] for c in r.calls] == ["real answer"]
    assert router.stats["error_suppressed"] == 1
    assert router.stats["error_released"] == 0


@pytest.mark.asyncio
async def test_a_run_that_stays_dead_still_shows_its_failure(monkeypatch):
    """Holding is not hiding: with no sign of life the placeholder is
    delivered after the grace period, with its original identity and time."""
    import asyncio
    monkeypatch.setattr(gr, "ERROR_PLACEHOLDER_GRACE_S", 0.1)
    r = _Recorder()
    router = gr.SessionRouter(r.resolve, r.deliver)
    await router.handle("session.message", {
        "sessionKey": "agent:main:t1", "messageSeq": 3, "message": _err()})
    await asyncio.sleep(0.3)
    assert len(r.calls) == 1
    assert r.calls[0]["text"].startswith("The agent run failed")
    assert r.calls[0]["source_id"] == "gw:agent:main:t1:e0000001"
    assert router.stats["error_released"] == 1


@pytest.mark.asyncio
async def test_a_second_error_replaces_the_first_hold(monkeypatch):
    """Overflow recovery can emit several errors in a row (attempt 1/3, 2/3,
    3/3); only the last can be the turn's real end. One bubble, not three."""
    import asyncio
    monkeypatch.setattr(gr, "ERROR_PLACEHOLDER_GRACE_S", 0.1)
    r = _Recorder()
    router = gr.SessionRouter(r.resolve, r.deliver)
    await router.handle("session.message", {
        "sessionKey": "agent:main:t1", "messageSeq": 3,
        "message": _err(mid="e0000001", seq=3)})
    await router.handle("session.message", {
        "sessionKey": "agent:main:t1", "messageSeq": 5,
        "message": _err(mid="e0000002", seq=5)})
    await asyncio.sleep(0.3)
    assert [c["source_id"] for c in r.calls] == ["gw:agent:main:t1:e0000002"]


@pytest.mark.asyncio
async def test_a_hold_is_per_session(monkeypatch):
    """Another bot's reply must not release or suppress this session's hold."""
    import asyncio
    monkeypatch.setattr(gr, "ERROR_PLACEHOLDER_GRACE_S", 0.1)
    r = _Recorder(known={"agent:main:t1": ("t1", "main"),
                         "agent:tutor:t2": ("t2", "tutor")})
    router = gr.SessionRouter(r.resolve, r.deliver)
    await router.handle("session.message", {
        "sessionKey": "agent:main:t1", "messageSeq": 3, "message": _err()})
    await router.handle("session.message", {
        "sessionKey": "agent:tutor:t2", "messageSeq": 1,
        "message": _msg("tutor talking", mid="cc000001")})
    await asyncio.sleep(0.3)
    assert [c["thread"] for c in r.calls] == ["t2", "t1"]
    assert router.stats["error_suppressed"] == 0


# --- the cursor map is bounded --------------------------------------------

@pytest.mark.asyncio
async def test_cursors_are_only_kept_for_sessions_we_own():
    """The cursor was recorded before the ownership check, so every session on
    the box — the overwhelming majority of a firehose — got a permanent entry."""
    r = _Recorder()
    router = gr.SessionRouter(r.resolve, r.deliver)
    for i in range(50):
        await router.handle("session.message", {
            "sessionKey": f"agent:stranger:{i}", "messageSeq": 1,
            "message": _msg("not ours")})
    assert router._seq == {}
    await router.handle("session.message", {
        "sessionKey": "agent:main:t1", "messageSeq": 7,
        "message": _msg("ours")})
    assert router._seq == {"agent:main:t1": 7}


@pytest.mark.asyncio
async def test_cursor_map_is_lru_bounded(monkeypatch):
    monkeypatch.setattr(gr, "SEQ_CURSOR_MAX", 8)
    known = {f"agent:main:t{i}": (f"t{i}", "main") for i in range(20)}
    r = _Recorder(known)
    router = gr.SessionRouter(r.resolve, r.deliver)
    for i in range(20):
        await router.handle("session.message", {
            "sessionKey": f"agent:main:t{i}", "messageSeq": 1,
            "message": _msg("ours")})
    assert len(router._seq) == 8
    assert list(router._seq) == [f"agent:main:t{i}" for i in range(12, 20)]


# --- backfill must cover the whole gap, or say it did not -------------------

class _PagedClient(_FakeClient):
    """A real tail read: `offset` counts backwards from the newest message.

    The single call the router used to make could only ever see one page, so
    any gap wider than it was permanently unrecovered.
    """

    def __init__(self, messages: list[dict]):
        super().__init__()
        # newest last, like a transcript
        self._all = messages

    async def history(self, session_key, *, limit=50, offset=0):
        self.history_calls.append({"limit": limit, "offset": offset})
        end = len(self._all) - offset
        if end <= 0:
            return []
        return self._all[max(0, end - limit):end]


def _session(n: int) -> list[dict]:
    return [_msg(f"reply {i}", mid=f"m{i}", seq=i) for i in range(1, n + 1)]


@pytest.mark.asyncio
async def test_a_gap_wider_than_one_page_is_fully_backfilled():
    client = _PagedClient(_session(300))
    r = _Recorder()
    router = gr.SessionRouter(r.resolve, r.deliver, client=client)

    await router.handle("session.message", {
        "sessionKey": "agent:main:t1", "messageSeq": 1,
        "message": _msg("reply 1", mid="m1", seq=1)})
    await router.handle("session.message", {
        "sessionKey": "agent:main:t1", "messageSeq": 300,
        "message": _msg("reply 300", mid="m300", seq=300)})


    # The repair is deferred off the reply path (see GAP_BACKFILL_DELAY_S);
    # flush_gaps() is what makes it observable without a sleep in a test.
    await router.flush_gaps()

    assert len(client.history_calls) > 1, "one page cannot cover a 299-wide gap"
    assert router.stats["backfill_incomplete"] == 0
    texts = [c["text"] for c in r.calls]
    for i in range(2, 300):
        assert f"reply {i}" in texts, f"reply {i} was never recovered"


@pytest.mark.asyncio
async def test_an_uncoverable_gap_is_counted_not_hidden(monkeypatch):
    """The ceiling exists so a corrupt cursor cannot ask for the whole
    history — but stopping at it must be VISIBLE, because stats["backfilled"]
    counts what was delivered, never what was missed."""
    monkeypatch.setattr(gr, "BACKFILL_PAGE", 10)
    monkeypatch.setattr(gr, "BACKFILL_MAX_MESSAGES", 20)
    client = _PagedClient(_session(200))
    r = _Recorder()
    router = gr.SessionRouter(r.resolve, r.deliver, client=client)

    await router.handle("session.message", {
        "sessionKey": "agent:main:t1", "messageSeq": 1,
        "message": _msg("reply 1", mid="m1", seq=1)})
    await router.handle("session.message", {
        "sessionKey": "agent:main:t1", "messageSeq": 200,
        "message": _msg("reply 200", mid="m200", seq=200)})


    # The repair is deferred off the reply path (see GAP_BACKFILL_DELAY_S);
    # flush_gaps() is what makes it observable without a sleep in a test.
    await router.flush_gaps()

    assert router.stats["backfill_incomplete"] == 1


@pytest.mark.asyncio
async def test_resync_is_not_capped_at_a_guessed_window(monkeypatch):
    """Reconnect used to guess `last + 50` and DISCARD everything past it."""
    monkeypatch.setattr(gr, "BACKFILL_PAGE", 10)
    client = _PagedClient(_session(80))
    r = _Recorder()
    router = gr.SessionRouter(r.resolve, r.deliver, client=client)
    router._seq["agent:main:t1"] = 1

    await router.resync_known()

    texts = [c["text"] for c in r.calls]
    assert "reply 80" in texts, "the tail past the old +50 window was dropped"
    assert router.stats["backfill_incomplete"] == 0


# --- stats keys are not minted from the wire --------------------------------

@pytest.mark.asyncio
async def test_unknown_roles_share_one_stats_bucket():
    """`dropped_role_<role>` took its key from a wire-supplied string and then
    served it in /api/health — unbounded growth from remote input."""
    r = _Recorder()
    router = gr.SessionRouter(r.resolve, r.deliver)
    for role in ("weird", "x" * 500, "role-2", "user"):
        await router.handle("session.message", {
            "sessionKey": "agent:main:t1", "messageSeq": 1,
            "message": _msg("hi", role=role)})

    assert router.stats["dropped_role_other"] == 3
    assert router.stats["dropped_role_user"] == 1
    assert not [k for k in router.stats if k.startswith("dropped_role_")
                and k not in ("dropped_role_other", "dropped_role_user")]


# --- gaps are usually not loss ---------------------------------------------

@pytest.mark.asyncio
async def test_a_gap_does_not_delay_the_reply_that_revealed_it():
    """The repair must not sit in front of the message.

    `messageSeq` counts every transcript row, but the gateway only emits
    `session.message` for rows its display projection keeps — and a toolResult
    is folded into the assistant message that called the tool, so it is counted
    and never sent. Measured on this box: every gap in a day was a jump of
    exactly 2 across a tool call, on a session that had lost nothing. Repairing
    inline put a `chat.history` round trip in front of the reply, once per tool
    call, for nothing.
    """
    client = _FakeClient(history=[])
    r = _Recorder()
    router = gr.SessionRouter(r.resolve, r.deliver, client=client)

    await router.handle("session.message", {
        "sessionKey": "agent:main:t1", "messageSeq": 1,
        "message": _msg("one", mid="m1", seq=1)})
    await router.handle("session.message", {
        "sessionKey": "agent:main:t1", "messageSeq": 3,
        "message": _msg("after the tool call", mid="m3", seq=3)})

    assert router.stats["gaps"] == 1
    assert [c["text"] for c in r.calls] == ["one", "after the tool call"]
    assert client.history_calls == [], (
        "the gap was chased on the reply's own path — that is the latency the "
        "deferral exists to remove")
    await router.flush_gaps()
    assert client.history_calls, "the deferred repair never ran"


@pytest.mark.asyncio
async def test_the_message_that_revealed_a_gap_is_not_replayed_as_backfill():
    """`now` is exclusive.

    Including it let the repair deliver the live reply first, as a replay:
    live=False, so it was marked `followup`, deduped against the whole thread,
    and its `:react:` markers were held to the replay-freshness rule. The real
    live delivery then deduped away. The family saw a normal reply; the bot's
    reaction never fired and the counters said `delivered_backfill`.
    """
    live = _msg("the live reply", mid="m3", seq=3)
    client = _FakeClient(history=[live])
    r = _Recorder()
    router = gr.SessionRouter(r.resolve, r.deliver, client=client)

    await router.handle("session.message", {
        "sessionKey": "agent:main:t1", "messageSeq": 1,
        "message": _msg("one", mid="m1", seq=1)})
    await router.handle("session.message", {
        "sessionKey": "agent:main:t1", "messageSeq": 3, "message": live})

    delivered = [c for c in r.calls if c["text"] == "the live reply"]
    assert len(delivered) == 1
    assert delivered[0]["live"] is True, (
        "the reply was delivered as a replay by its own gap repair")


@pytest.mark.asyncio
async def test_a_phantom_gap_is_counted_apart_from_a_real_one():
    """`gaps` climbing on every tool call trains an operator to ignore it."""
    client = _FakeClient(history=[])
    r = _Recorder()
    router = gr.SessionRouter(r.resolve, r.deliver, client=client)
    await router.handle("session.message", {
        "sessionKey": "agent:main:t1", "messageSeq": 1,
        "message": _msg("one", mid="m1", seq=1)})
    await router.handle("session.message", {
        "sessionKey": "agent:main:t1", "messageSeq": 3,
        "message": _msg("two", mid="m3", seq=3)})
    await router.flush_gaps()
    assert router.stats["gaps_empty"] == 1
    assert router.stats["backfilled"] == 0, (
        "backfilled must count replies actually recovered, not attempts")


@pytest.mark.asyncio
async def test_many_gaps_in_one_turn_cost_one_history_read():
    """A turn that calls eight tools reports eight gaps and needs one read."""
    client = _FakeClient(history=[])
    r = _Recorder()
    router = gr.SessionRouter(r.resolve, r.deliver, client=client)
    await router.handle("session.message", {
        "sessionKey": "agent:main:t1", "messageSeq": 1,
        "message": _msg("one", mid="m1", seq=1)})
    for seq in (3, 5, 7, 9):
        await router.handle("session.message", {
            "sessionKey": "agent:main:t1", "messageSeq": seq,
            "message": _msg(f"m{seq}", mid=f"m{seq}", seq=seq)})
    assert router.stats["gaps"] == 4
    await router.flush_gaps()
    assert router.stats["gap_backfills_run"] == 1
    assert len(client.history_calls) <= 1


@pytest.mark.asyncio
async def test_flush_gaps_is_safe_with_nothing_queued():
    router = gr.SessionRouter(_Recorder().resolve, _Recorder().deliver)
    await router.flush_gaps()


@pytest.mark.asyncio
async def test_a_delivery_the_funnel_deduped_is_not_counted_as_delivered():
    """Counters must report landings, not offers.

    `delivered_backfill: 27` on the live install meant 27 OFFERS, nearly all of
    which the funnel discarded as messages the thread already had — and it read
    as 27 replies a repair had rescued. A counter that reports work it did not
    do is the same defect as a repair that reports success by silence.
    """
    r = _Recorder()
    router = gr.SessionRouter(r.resolve, r.deliver)
    for _ in range(2):
        await router.handle("session.message", {
            "sessionKey": "agent:main:t1", "messageSeq": 1,
            "message": _msg("same reply", mid="m1", seq=1)})
    assert router.stats["delivered"] == 1
    assert router.stats["deduped"] == 1
