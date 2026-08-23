"""The delivery invariant: what a person sees equals what the agent said.

WHY THIS FILE EXISTS
--------------------
Every other test that exercises the reply pipeline replaces the broadcast with
a no-op::

    tests/test_recovery.py:59            main.manager.broadcast = _noop
    tests/test_mirror.py:51              main.manager.broadcast = _noop
    tests/test_gateway_resilience.py:159 monkeypatch.setattr(main.manager, "broadcast", _noop)
    tests/test_audit_fixes.py:248        monkeypatch.setattr(main.manager, "broadcast", _noop)

and then asserts on rows in SQLite. Every bug that has reached the family chat
lives strictly DOWNSTREAM of ``db.add_message`` and UPSTREAM of a human's eye —
exactly the region those lines delete. A green suite over an invisible reply is
not a coincidence; it is what the suite was measuring.

Four real examples, all of which passed 500+ tests:

  · a reply persisted with ``sub: True`` because a trailing tool warning took
    the "last payload" slot — the whole turn invisible, including a direct
    question to the operator
  · a thread-list preview advertising "⚠️ Exec failed" while the real answer
    sat one row above it
  · a delegated answer discarded because a 30-minute window expired nine
    seconds early — seventeen blocks, never delivered
  · a transcript re-scan replaying an entire day into the chat

THE INVARIANT
-------------
    For every turn, what a client renders equals what the agent said —
    exactly once each, in order, with the same content.

Four properties, asserted here rather than assumed:

  1. CONSERVATION  every payload either becomes a visible bubble or is
                   classified non-speech by ONE predicate (not three
                   disagreeing ones).
  2. UNIQUENESS    no bubble corresponds to a block already rendered.
  3. FIDELITY      rendered text is stored text minus only declared transforms.
  4. VISIBILITY    a thread carries an unread indicator IFF it contains
                   something a person can actually see.

The probe below captures broadcasts instead of discarding them, and models the
frontend's own visibility rules, so "did a human see it" is a question this
suite can finally answer.
"""
from __future__ import annotations

import asyncio
import sqlite3

import pytest

from app import config, main
from app.database import Database

# --------------------------------------------------------------------------- #
# The probe
# --------------------------------------------------------------------------- #

class DeliveryProbe:
    """Captures what a connected client would receive, and what it would show.

    `frames` is every broadcast. `visible()` applies the FRONTEND's rules for
    what actually reaches a person's eye, so a test can assert on perception
    rather than on storage.
    """

    def __init__(self) -> None:
        self.frames: list[dict] = []

    async def broadcast(self, payload: dict) -> None:
        self.frames.append(payload)

    # -- what a person would see ------------------------------------------- #

    def messages(self) -> list[dict]:
        """Every message frame, in the order the client received it."""
        return [f.get("message", f) for f in self.frames
                if f.get("type") in ("message", "message_update")]

    @staticmethod
    def is_visible(msg: dict) -> bool:
        """Would a human see this as a chat bubble?

        Mirrors frontend/static/js/main.js:
          · metadata.sub renders as a COLLAPSED <details> ("working output"),
            not a bubble — present in the DOM, absent from the conversation
          · a reaction trace is a picture's footprint, not speech
          · empty content with no media renders nothing
        """
        meta = msg.get("metadata") or {}
        if meta.get("sub"):
            return False
        if meta.get("kind") == "reaction":
            return False
        return bool((msg.get("content") or "").strip() or msg.get("media_url"))

    def visible(self) -> list[str]:
        return [(m.get("content") or "").strip()
                for m in self.messages() if self.is_visible(m)]


@pytest.fixture
def probe(tmp_path, monkeypatch):
    """A real Database and a real delivery funnel, with the broadcast CAPTURED.

    Note what is NOT stubbed: _deliver_assistant_text, the sanitizers, the
    reaction stripper, the dedup. The point is to exercise the real path.
    """
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(config, "AVATAR_DIR", tmp_path / "avatars")
    config._invalidate_bots_cache()

    db = Database(tmp_path / "chats.db")
    asyncio.get_event_loop().run_until_complete(db.connect()) \
        if False else None                      # connected in the async fixture below
    p = DeliveryProbe()
    monkeypatch.setattr(main.manager, "broadcast", p.broadcast)
    main._delivered.clear()
    main._ACK_SEEN.clear()
    p._db = db
    return p


@pytest.fixture
async def wired(probe):
    """probe + a connected database bound into main."""
    await probe._db.connect()
    yield probe
    await probe._db.close()


# --------------------------------------------------------------------------- #
# 1. CONSERVATION — nothing the agent said may vanish
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_every_spoken_payload_reaches_a_visible_bubble(wired, monkeypatch):
    """The bug class that keeps reaching the family chat.

    Three payloads, the last of which is a runtime tool warning. The two real
    ones must BOTH be visible. Before the fix, the warning took the "last
    payload" slot: it was collapsed for being a warning, the real answer was
    collapsed for not being last, and the entire turn was invisible.
    """
    p = wired
    monkeypatch.setattr(main, "db", p._db)
    t = await p._db.create_thread(bot_id="main", title="conservation")

    spoken = ["Scoping it now.", "Here is the answer, and a question for you."]
    warning = "⚠️ 🛠️ Exec failed: `show ~/notes/x.md` (exit 1)"

    for text in spoken:
        await main._deliver_assistant_text(t.id, text)
    await main._deliver_assistant_text(t.id, warning)

    seen = p.visible()
    for text in spoken:
        assert text in seen, (
            f"the agent said {text!r} and no client would show it.\n"
            f"visible: {seen}")
    assert warning not in seen, "a runtime tool warning was shown as speech"


@pytest.mark.asyncio
async def test_a_reply_is_never_silently_dropped_by_a_sanitizer(wired, monkeypatch):
    """Sanitizers may TRIM, never DELETE.

    A message that quotes an internal delimiter used to sanitize to the empty
    string, and empty means "nothing to persist" — so the reply was never
    written at all.
    """
    from app import openclaw_text

    p = wired
    monkeypatch.setattr(main, "db", p._db)
    t = await p._db.create_thread(bot_id="main", title="fidelity")

    quoted = ("The opener is:\n\n```\n" + openclaw_text.INTERNAL_RUNTIME_CONTEXT_BEGIN
              + "\n```\n\nAnd this sentence is the actual answer.")
    await main._deliver_assistant_text(t.id, quoted)

    seen = p.visible()
    assert seen, "the reply was swallowed entirely"
    assert "actual answer" in seen[-1], f"the answer was truncated: {seen[-1]!r}"


# --------------------------------------------------------------------------- #
# 2. UNIQUENESS — nothing may be shown twice
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_the_same_reply_delivered_twice_is_shown_once(wired, monkeypatch):
    """Re-scans happen (late session resolve, shrunk file, reconciler). They
    must not replay the conversation at the family."""
    p = wired
    monkeypatch.setattr(main, "db", p._db)
    t = await p._db.create_thread(bot_id="main", title="uniqueness")

    text = "Done. The zip is at files/site.zip."
    await main._deliver_assistant_text(t.id, text)
    await main._deliver_assistant_text(t.id, text)

    assert p.visible().count(text) == 1, (
        f"the same reply was shown {p.visible().count(text)} times")


# --------------------------------------------------------------------------- #
# 3. FIDELITY — the words are the agent's words
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_indentation_survives_a_message_that_fires_a_reaction(wired, monkeypatch):
    """Firing a reaction used to collapse every run of 2+ spaces in the same
    message, flattening code blocks and nested lists into nonsense."""
    p = wired
    monkeypatch.setattr(main, "db", p._db)
    t = await p._db.create_thread(bot_id="main", title="fidelity2")

    code = "def f(x):\n    if x:\n        return 1\n    return 0"
    await main._deliver_assistant_text(t.id, f":react:facepalm:\n\n```python\n{code}\n```")

    seen = p.visible()
    assert seen, "the message vanished"
    assert code in seen[-1], f"indentation was destroyed:\n{seen[-1]!r}"


@pytest.mark.asyncio
async def test_a_quoted_reaction_marker_is_not_stripped_from_the_sentence(wired, monkeypatch):
    """An agent explaining the syntax must keep its sentence intact."""
    p = wired
    monkeypatch.setattr(main, "db", p._db)
    t = await p._db.create_thread(bot_id="main", title="fidelity3")

    text = "No files — the `:react:check_in:` is a reaction marker, not an attachment."
    await main._deliver_assistant_text(t.id, text)

    seen = p.visible()
    assert seen and "`:react:check_in:`" in seen[-1], (
        f"the quoted marker was cut out of its own sentence: {seen}")
    assert "``" not in seen[-1], "left an empty code span"


# --------------------------------------------------------------------------- #
# 4. VISIBILITY <=> NOTIFICATION — no dot without something to read
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_a_turn_with_nothing_visible_raises_no_unread_dot(wired, monkeypatch):
    """The inverse failure, and the one the audit's critic found.

    _UNREAD_SQL counts any row with role != 'user'. It knows nothing about
    metadata.sub — while _LAST_MESSAGE_SQL four lines above explicitly excludes
    it. So a turn whose only output was collapsed working output lights the dot
    on every device, the family opens the thread, and there is nothing new.
    """
    p = wired
    monkeypatch.setattr(main, "db", p._db)
    t = await p._db.create_thread(bot_id="main", title="visibility")
    await p._db.add_message(thread_id=t.id, role="user", content="do the thing")
    await p._db.mark_thread_read(t.id) if hasattr(p._db, "mark_thread_read") else None

    # The turn produces ONLY a collapsed tool warning.
    await main._deliver_assistant_text(
        t.id, "⚠️ 🛠️ Exec failed: `show ~/x` (exit 1)")

    fresh = await p._db.get_thread(t.id)
    visible_now = p.visible()
    if not visible_now:
        assert fresh.unread_since is None, (
            "the thread is flagged unread but a person would see nothing new — "
            "the dot lights, they open it, and the only new row is collapsed "
            "working output")


# --------------------------------------------------------------------------- #
# "Like a normal messaging app"
#
# The owner's acceptance criterion, written down so it is testable rather than
# a matter of opinion. WhatsApp/Signal/iMessage semantics, stated as properties:
#
#   ARRIVES      a sent message reaches the other side, or the sender is told
#                it did not. Silence is never success.
#   ONCE         it appears exactly once, no matter how many times any layer
#                retries, reconnects or re-reads.
#   IN ORDER     it appears in the order it was said, in its own conversation.
#   INTACT       the text shown is the text sent.
#   HONEST BADGE the unread indicator means "there is something to read", and
#                its absence means there is not.
#
# Every one of today's confirmed defects is a violation of exactly one of
# these. That is the whole specification; the transport underneath is an
# implementation detail that either satisfies it or does not.
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_messages_appear_in_the_order_they_were_said(wired, monkeypatch):
    """IN ORDER. A reply must never sort above the question it answers.

    Live data shows seven messages sharing one timestamp; ordering that falls
    back to insertion is fine, ordering that falls back to nothing is not.
    """
    p = wired
    monkeypatch.setattr(main, "db", p._db)
    t = await p._db.create_thread(bot_id="main", title="order")

    said = [f"part {i}" for i in range(1, 8)]
    for text in said:
        await main._deliver_assistant_text(t.id, text)

    stored = [m.content for m in await p._db.dump_messages(t.id)
              if m.role == "assistant"]
    assert stored == said, f"order changed in storage:\n  said:   {said}\n  stored: {stored}"

    seen = p.visible()
    assert seen == said, f"order changed on the way to the screen:\n  {seen}"


@pytest.mark.asyncio
async def test_a_recovered_message_keeps_its_original_time(wired, monkeypatch):
    """IN ORDER, across recovery.

    The gap sweep imports answers a live path missed — sometimes days later.
    Stamping them "now" files a Friday reply at the bottom of Friday's thread
    dated today, out of order inside its own conversation. Observed on the
    sweep's first live run: 41 messages from three different days all landed
    at once, timestamped today.
    """
    p = wired
    monkeypatch.setattr(main, "db", p._db)
    t = await p._db.create_thread(bot_id="main", title="recovery-order")

    original = "2026-08-07T08:56:14+00:00"
    await p._db.add_message(thread_id=t.id, role="assistant",
                            content="the late answer", created_at=original)

    rows = await p._db.dump_messages(t.id)
    late = [m for m in rows if m.content == "the late answer"][0]
    assert late.created_at == original, (
        f"a recovered message was re-dated to {late.created_at!r}; it belongs "
        f"at {original!r}, where the conversation actually happened")


@pytest.mark.asyncio
async def test_the_sweep_supplies_the_original_time(wired, monkeypatch, tmp_path):
    """The other half of the invariant above. The DB honours a supplied
    created_at — but the sweep has to actually SUPPLY it. It never did: the
    transcript item's timestamp was read, displayed in the raw viewer, and
    then dropped on the floor at delivery, so every swept answer was stamped
    "now" anyway — the exact incident the DB-side comment describes."""
    import json as _json

    from app import openclaw

    p = wired
    monkeypatch.setattr(main, "db", p._db)
    root = tmp_path / "agents"
    orig_root = openclaw.OPENCLAW_AGENTS_DIR
    openclaw.OPENCLAW_AGENTS_DIR = root
    try:
        t = await p._db.create_thread(bot_id="main", title="sweep-time")
        key = openclaw.session_key_for("main", t.id)
        sdir = root / "main" / "sessions"
        sdir.mkdir(parents=True)
        (sdir / "sessions.json").write_text(_json.dumps({key: {"sessionId": "sid-t"}}))
        # The gateway stamps message lines with an ISO-8601 `timestamp`
        # (Z-suffixed) — the format verified against live session files.
        lines = [
            {"type": "message", "timestamp": "2026-08-07T08:56:14.184Z",
             "message": {"role": "assistant",
                         "content": [{"type": "text", "text": "the late answer"}]}},
        ]
        (sdir / "sid-t.jsonl").write_text(
            "\n".join(_json.dumps(x) for x in lines) + "\n")

        assert await main._import_transcript_messages(t.id, "main",
                                                      mark_followup=True) == 1
        row = [m for m in await p._db.dump_messages(t.id)
               if m.content == "the late answer"][0]
        assert row.created_at == "2026-08-07T08:56:14.184000+00:00", (
            f"the sweep stamped a recovered answer {row.created_at!r} instead "
            f"of the moment it was actually said")
    finally:
        openclaw.OPENCLAW_AGENTS_DIR = orig_root


@pytest.mark.asyncio
async def test_an_old_backfill_does_not_regress_the_thread_clock(wired, monkeypatch):
    """A thread's updated_at drives the list order AND the gap sweep's
    48-hour recency window. Backfilling one old message must not shove a
    thread with today's activity back two days — off the top of the list and,
    worse, out of the sweep's sight."""
    p = wired
    monkeypatch.setattr(main, "db", p._db)
    t = await p._db.create_thread(bot_id="main", title="clock")

    await p._db.add_message(t.id, "assistant", "today's reply")
    now_updated = (await p._db.get_thread(t.id)).updated_at

    await p._db.add_message(t.id, "assistant", "the answer from Tuesday",
                            created_at="2026-08-07T08:56:14+00:00")
    after = (await p._db.get_thread(t.id)).updated_at
    assert after >= now_updated, (
        f"one old backfilled message moved the thread's clock from "
        f"{now_updated!r} back to {after!r}")


@pytest.mark.asyncio
async def test_silence_is_never_reported_as_success(wired, monkeypatch):
    """ARRIVES. If a message cannot be delivered, that must be observable.

    The 2026-08-07 loss was silent: no error, no log, no row — the family app
    simply showed nothing while the agent had finished the job. A delivery
    function that returns None must be distinguishable from one that delivered.
    """
    p = wired
    monkeypatch.setattr(main, "db", p._db)
    t = await p._db.create_thread(bot_id="main", title="arrives")

    delivered = await main._deliver_assistant_text(t.id, "a real answer")
    assert delivered is not None, "a deliverable message reported nothing"

    # An empty payload is legitimately not delivered — and says so by returning
    # None rather than by inventing an empty bubble.
    nothing = await main._deliver_assistant_text(t.id, "   ")
    assert nothing is None
    assert "a real answer" in p.visible()
    assert "" not in p.visible(), "an empty bubble reached the conversation"


# --- the backup must not lose a race with ordinary traffic ------------------

@pytest.mark.asyncio
async def test_a_backup_succeeds_while_the_app_is_reading(tmp_path):
    """VACUUM refuses to run while a statement is active on the SAME
    connection. Sharing the app's connection meant the boot snapshot lost a
    race with startup recovery and failed with

        sqlite3.OperationalError: cannot VACUUM - SQL statements in progress

    ...then did not retry for six hours, while health reported
    last_backup_ok:false the whole time. Observed on the live family app.
    """
    from app.database import Database

    db = Database(tmp_path / "chats.db")
    await db.connect()
    try:
        th = await db.create_thread("t-backup", "main", "Backup race")
        for i in range(30):
            await db.add_message(th.id if hasattr(th, "id") else "t-backup",
                                 "assistant", f"line {i}")
        # Hold a live cursor open on the shared connection — exactly the state
        # that used to make the backup fail.
        cur = await db.db.execute("SELECT id, content FROM messages")
        await cur.fetchone()                     # deliberately NOT drained
        try:
            dest = await db.backup_to(tmp_path / "snap.db")
        finally:
            await cur.close()
        assert dest.exists() and dest.stat().st_size > 0
        # ...and the copy is usable, not merely present.
        con = sqlite3.connect(f"file:{dest}?mode=ro", uri=True)
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert con.execute("SELECT count(*) FROM messages").fetchone()[0] == 30
        con.close()
    finally:
        await db.close()
