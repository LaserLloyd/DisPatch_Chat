"""Regression: a conversation reply must survive a gateway messageSeq gap backfill.

THE BUG
-------
DisPatch used to lose user replies to "the wipe": a reply lands in the chat
UI and then vanishes a few minutes later, row deleted from `messages`. The
mechanism:

    1. The gateway emits ``session.message`` for one transcript item at a
       time; the only loss signal is ``messageSeq`` (an absolute per-session
       counter). A skip from e.g. 2 -> 6 queues a backfill that walks
       ``chat.history`` and re-delivers the gap (``items 3..5``).
    2. The live path delivers the conversational reply at ``seq=6`` inline.
    3. The backfill runs 6 s later, persists items 3..5 (each with its own
       gateway id and therefore its own ``source_id``). Items 3..5 happen to
       be drift-tolerant LONGER twins of the live reply — they share the
       same opening 60+ chars and the same media count, so ``_is_twin``
       treats them as twins.
    4. The first item that ``_is_duplicate_message`` finds the live reply in
       the trailing run as a SHORTER twin of itself; the "abridged-twin
       replacement" path then ``superseded = live_reply`` and the persist
       chokepoint ``db.delete_message(superseded.id)``'s the live reply.

Symptom: reply in the UI, then gone minutes later, row missing.
Log: ``session <id> jumped 2 -> 6; queued a backfill``.

THE FIX
-------
A message with a stable ``source_id`` IS the identity. The comment at
``gateway_router._backfill`` says "every message carries a stable id, so
re-delivering one already stored is a no-op" — the original design was
purely identity-based. The content-based twin check (``_is_twin``) was added
later for /api/inject and legacy transcript-tail paths, which have no
source identity, and it kept applying to source_id messages too — that is
the asymmetry the wipe exploits.

These tests pin the new contract:

  * A source_id delivery is NOT subject to content-based twin suppression,
    in-memory or DB. The live reply persists even when a backfilled message
    in the same thread is a drift-tolerant longer twin of it.
  * The abridged-twin replacement on a source-less delivery still works
    exactly as before (the smaller twin is suppressed OR replaced with the
    longer one). Unchanged behaviour for source-less paths is essential.
  * Source-id identity dedup still wins over content: re-delivering the
    same gateway id (live + backfill overlap, reconnect) is idempotent.

Reversibility
-------------
The fix in ``_deliver_assistant_text`` is gated on ``and not source_id`` at
two clauses. Removing both gates restores the previous behaviour
exactly — no other code or test changes required.
"""
from __future__ import annotations

import pytest

from app import config, main

# --------------------------------------------------------------------------- #
# The delivery-probe rig, shared with test_double_post_fix.py (its fixtures
# are file-local). A real Database, captures broadcast frames, no stubs.
# --------------------------------------------------------------------------- #


class DeliveryProbe:
    def __init__(self):
        self.frames: list[dict] = []

    async def broadcast(self, frame: dict) -> None:
        self.frames.append(frame)

    def messages(self):
        return [f.get("message", f) for f in self.frames if f.get("type") in ("message", "message_update")]


@pytest.fixture
async def wired(tmp_path, monkeypatch):
    from app import database as db_module

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(config, "AVATAR_DIR", tmp_path / "avatars")
    config._invalidate_bots_cache()
    db = db_module.Database(tmp_path / "chats.db")
    await db.connect()
    p = DeliveryProbe()
    monkeypatch.setattr(main.manager, "broadcast", p.broadcast)
    main._delivered.clear()
    main._ACK_SEEN.clear()
    p._db = db
    yield p
    await db.close()


# A long opening both messages share (>= the 60-char ``_TWIN_MIN_SHARED_PREFIX``),
# and an extra paragraph the backfilled copy carries that the live reply drops.
# Twin-ness comes from the structural predicate ("shared opening 60+ chars,
# same media count"), not from exact canonical equality.
SHARED_OPENING = (
    "Here is the analysis you asked for — the staffing model assumes three "
    "cohorts of eleven, a rotation that keeps one cohort in seat every month, "
    "and a soft launch in the last two weeks of the quarter."
)
LONG_BACKFILL = SHARED_OPENING + (
    "\n\nNote that the second cohort carries the launch week — making it "
    "smaller (seven instead of eleven) would surface the gap earlier and let "
    "us retune before the public date. The third cohort is the safety net."
)
SHORT_LIVE = SHARED_OPENING + (
    "\n\nLet me know if you want the second-cohort scenario modelled too."
)


# --------------------------------------------------------------------------- #
# Part 1: the wipe scenario
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_live_reply_survives_backfilled_longer_twin(wired, monkeypatch):
    """The exact wipe repro.

    A backfilled (live=False, followup=True) message in the thread is a
    drift-tolerant LONGER twin of the live reply. The backfilled one persists
    first, then the live reply arrives. Without the fix, the live reply is
    matched in the trailing run, marked ``superseded``, and
    ``db.delete_message`` runs against it. With the fix, the live reply's
    source_id short-circuits the content check and the row stays.
    """
    p = wired
    monkeypatch.setattr(main, "db", p._db)
    t = await p._db.create_thread(bot_id="main", title="wipe")

    # Step 1: backfill (say, gap item 3) lands first.
    backfill = await main._deliver_assistant_text(
        t.id, LONG_BACKFILL,
        source_id="gw:agent:main:t:backfilled_3",
        metadata={"followup": True},
        dedup_whole_thread=True,
    )
    assert backfill is not None

    # Step 2: live reply (gap-detected sequence, seq=6) lands second.
    live = await main._deliver_assistant_text(
        t.id, SHORT_LIVE,
        source_id="gw:agent:main:t:live_6",
    )
    assert live is not None, (
        "the live reply was wiped — a longer-twin backfilled message in the "
        "trailing run suppressed it via the content-based twin check"
    )

    # Both rows present, live reply intact.
    msgs = {m.id: m for m in await p._db.dump_messages(t.id)
            if m.role == "assistant"}
    assert live.id in msgs, "the live reply's row is missing from `messages`"
    assert backfill.id in msgs, "the backfilled row should still be there too"
    assert msgs[live.id].content == SHORT_LIVE


@pytest.mark.asyncio
async def test_live_reply_survives_when_backfilled_arrives_second(wired, monkeypatch):
    """The other ordering. Live first, then the backfilled longer twin.

    Without the fix, the BACKFILL's whole-thread scan sees the live reply in
    the trail, matches a twin, and ``db.delete_message``'s the live reply
    (the ``superseded = live_reply`` branch). With the fix, the backfilled
    message's source_id skips the content check and the live reply stays.
    """
    p = wired
    monkeypatch.setattr(main, "db", p._db)
    t = await p._db.create_thread(bot_id="main", title="wipe-flipped")

    live = await main._deliver_assistant_text(
        t.id, SHORT_LIVE,
        source_id="gw:agent:main:t:live_6",
    )
    assert live is not None

    backfill = await main._deliver_assistant_text(
        t.id, LONG_BACKFILL,
        source_id="gw:agent:main:t:backfilled_3",
        metadata={"followup": True},
        dedup_whole_thread=True,
    )
    assert backfill is not None, (
        "a backfilled message with its own source_id must persist"
    )

    msgs = {m.id: m for m in await p._db.dump_messages(t.id)
            if m.role == "assistant"}
    assert live.id in msgs, (
        "the live reply was DELETED by the backfilled twin — this is the "
        "wipe. Both messages must remain."
    )
    assert backfill.id in msgs


# --------------------------------------------------------------------------- #
# Part 2: source-id identity dedup still wins
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_source_id_dedup_is_still_identity_based(wired, monkeypatch):
    """The same source_id delivered twice (live + backfill overlap) is
    idempotent — exactly one row, regardless of the content-based check.
    This is the property the WIPE fix relies on; it would be a regression
    for source_id messages to double-post after the fix."""
    p = wired
    monkeypatch.setattr(main, "db", p._db)
    t = await p._db.create_thread(bot_id="main", title="identity")

    text = "Same gateway id, same body, two paths."
    first = await main._deliver_assistant_text(
        t.id, text, source_id="gw:agent:main:t:once")
    second = await main._deliver_assistant_text(
        t.id, text, source_id="gw:agent:main:t:once")
    assert first is not None
    assert second is None, (
        "the same source_id must dedup — the identity path is the WHOLE "
        "answer for source_id messages"
    )
    asst = [m.content for m in await p._db.dump_messages(t.id)
            if m.role == "assistant"]
    assert asst == [text]


# --------------------------------------------------------------------------- #
# Part 3: source-less paths still get the content-based twin check
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_source_less_twin_replacement_unchanged(wired, monkeypatch):
    """The fix must not change source-less behaviour: /api/inject and the
    legacy transcript-tail paths still rely on the in-memory + DB twin
    check to collapse the abridged/complete pair from two transports. Pin
    that here so a future "let's just always skip content" simplification
    is loudly broken by tests instead of silently regressing."""
    p = wired
    monkeypatch.setattr(main, "db", p._db)
    t = await p._db.create_thread(bot_id="main", title="source-less")

    # First the abridged (source-less) copy lands — like a transcript tail.
    abridged = await main._deliver_assistant_text(t.id, SHORT_LIVE)
    assert abridged is not None

    # Then the complete copy lands without a source_id (e.g. another
    # transcript path catches up after a slow follow).
    complete = await main._deliver_assistant_text(t.id, LONG_BACKFILL)
    # The longer copy REPLACES the shorter one and the shorter row is
    # deleted — that is the "abridged twin replacement" the comment above
    # describes, and it must keep working for source-less paths.
    assert complete is not None
    msgs = [m for m in await p._db.dump_messages(t.id) if m.role == "assistant"]
    contents = [m.content for m in msgs]
    assert contents == [LONG_BACKFILL], (
        f"source-less abridged twin replacement regressed; got {contents!r}"
    )
