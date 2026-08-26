"""Regression tests: the two delivery transports must not double-post.

DisPatch runs the gateway WebSocket transport and the legacy transcript-tail
paths side by side. The gateway REDACTS [[reply_to_current]] /
[[reply_to:<id>]] directives ANYWHERE in the text (unanchored,
case-insensitive) before its copy is delivered, while the transcript file the
tail paths read keeps the token verbatim — so the same reply arrives as two
genuinely different strings, gets two different canonical dedup keys, and
BOTH post. Second class: the in-memory dedup set is cleared at every turn
start and the DB scan only covers the current turn's trailing run, so a
legacy re-delivery of an OLD turn's text also double-posts.

The fix, both halves tested here:
  1. ``_canon_msg`` strips the directive UNANCHORED (mirroring the gateway's
     own regex), so the two recordings of one reply canonicalise equal and
     one key wins;
  2. source-less deliveries dedup against the whole thread within a ~5-minute
     recent horizon, so an old-turn re-delivery is suppressed — and the
     window is bounded: a repeat older than that still posts, distinct texts
     are never collapsed, and source_id'd deliveries are untouched.

Both halves are additive to the existing same-turn dedup (in-memory key set +
trailing-run DB scan); those paths keep working as before.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app import config, main

# --------------------------------------------------------------------------- #
# The delivery-probe rig, copied from test_delivery_invariant.py (its fixtures
# are file-local, so a sibling test file cannot import them): a real Database
# and a real delivery funnel with the broadcast CAPTURED. Nothing about the
# dedup under test is stubbed.
# --------------------------------------------------------------------------- #


class DeliveryProbe:
    def __init__(self):
        self.frames: list[dict] = []

    async def broadcast(self, frame: dict) -> None:
        self.frames.append(frame)

    def messages(self):
        return [f.get("message", f) for f in self.frames if f.get("type") in ("message", "message_update")]

    @staticmethod
    def is_visible(msg: dict) -> bool:
        meta = msg.get("metadata") or {}
        if meta.get("sub"):
            return False
        if meta.get("kind") == "reaction":
            return False
        return bool((msg.get("content") or "").strip() or msg.get("media_url"))

    def visible(self) -> list[str]:
        return [(m.get("content") or "").strip() for m in self.messages() if self.is_visible(m)]


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


# --------------------------------------------------------------------------- #
# Part 1: canonical keys must not diverge on the reply directive
# --------------------------------------------------------------------------- #


def test_anywhere_strip_mirrors_the_gateway_redaction_regex():
    r"""Parity pin: the helper must remove EXACTLY what the gateway's own
    redaction removes. The gateway regex (upstream reference) is
    /\[\[\s*(?:reply_to_current|reply_to\s*:\s*([^\]\n]+))\s*\]\]/gi."""
    import re as _re

    gateway = _re.compile(r"\[\[\s*(?:reply_to_current|reply_to\s*:\s*([^\]\n]+))\s*\]\]", _re.IGNORECASE)
    samples = [
        "[[reply_to_current]] hello",
        "hello [[reply_to_current]]",
        "a [[reply_to_current]] b [[reply_to:xyz]] c",
        "[[ reply_to_current ]]",
        "[[reply_to:some id with spaces]] done",
        "done [[REPLY_TO_CURRENT]]",
        "[[reply_to:agent:main:thread:abc123]] here it is",
        "no directive here",
    ]
    for s in samples:
        assert main._strip_reply_directive_anywhere(s) == gateway.sub("", s).strip(), s


def test_canon_keys_with_and_without_directive_are_equal():
    """The WS copy (token redacted) and the tail copy (token verbatim) of one
    reply must canonicalise to the SAME dedup key."""
    clean = "The zip is at files/site.zip, enjoy."
    for with_directive in (
        "The zip is at files/site.zip, enjoy. [[reply_to_current]]",
        "The zip is at files/site.zip, enjoy. [[reply_to:agent:main:x:aa11bb]]",
        "The zip is at files/site.zip, enjoy. [[REPLY_TO_CURRENT]]",
    ):
        assert main._canon_msg(with_directive) == main._canon_msg(clean), with_directive


@pytest.mark.asyncio
async def test_clean_copy_then_directive_copy_posts_once(wired, monkeypatch):
    """Gateway-first ordering (the common one): the WS copy lands without the
    token, then the tail path re-offers the same reply WITH the token
    mid-text. Only the first may post."""
    p = wired
    monkeypatch.setattr(main, "db", p._db)
    t = await p._db.create_thread(bot_id="main", title="ws-first")

    clean = "The zip is at files/site.zip, enjoy."
    with_directive = "The zip is at files/site.zip, enjoy. [[reply_to_current]]"

    assert await main._deliver_assistant_text(t.id, clean) is not None
    assert await main._deliver_assistant_text(t.id, with_directive) is None, (
        "the tail copy re-posted the reply the gateway copy already delivered"
    )

    asst = [m.content for m in await p._db.dump_messages(t.id) if m.role == "assistant"]
    assert asst == [clean], f"expected exactly one post, got {asst!r}"


@pytest.mark.asyncio
async def test_directive_copy_then_clean_copy_posts_once(wired, monkeypatch):
    """Tail-first ordering: the token-carrying transcript copy lands first, the
    redacted WS copy second. Still exactly one message."""
    p = wired
    monkeypatch.setattr(main, "db", p._db)
    t = await p._db.create_thread(bot_id="main", title="tail-first")

    with_directive = "Here you go: [[reply_to:agent:main:thread:abc123]] done."
    clean = "Here you go: done."

    assert await main._deliver_assistant_text(t.id, with_directive) is not None
    assert await main._deliver_assistant_text(t.id, clean) is None, (
        "the WS copy re-posted the reply the tail copy already delivered"
    )

    asst = [m.content for m in await p._db.dump_messages(t.id) if m.role == "assistant"]
    assert len(asst) == 1, f"expected exactly one post, got {asst!r}"
    assert asst[0].startswith("Here you go:"), asst


@pytest.mark.asyncio
async def test_leading_directive_still_stripped_and_deduped(wired, monkeypatch):
    """The anchored leading-only strip keeps working (the separate
    visible-token-at-start bug), and both strips together still yield one
    post for a leading-token reply delivered twice."""
    p = wired
    monkeypatch.setattr(main, "db", p._db)
    t = await p._db.create_thread(bot_id="main", title="leading")

    with_directive = "[[reply_to_current]] Fresh batch is ready."
    clean = "Fresh batch is ready."

    assert await main._deliver_assistant_text(t.id, with_directive) is not None
    stored = [m.content for m in await p._db.dump_messages(t.id) if m.role == "assistant"]
    assert stored == [clean], f"the token was not stripped at persist: {stored!r}"

    assert await main._deliver_assistant_text(t.id, clean) is None
    asst = [m.content for m in await p._db.dump_messages(t.id) if m.role == "assistant"]
    assert asst == [clean], f"expected exactly one post, got {asst!r}"


# --------------------------------------------------------------------------- #
# Part 2: source-less re-delivery of an OLD turn's text, within the window
# --------------------------------------------------------------------------- #


async def _simulate_new_turn(p, thread_id, user_text="ok now what?"):
    """What run_agent_turn does at turn start (clear the in-memory key set)
    plus the user message that opens the new turn — so the trailing-run DB
    scan stops before the old turn's reply."""
    main._forget_thread_delivery(thread_id)
    await p._db.add_message(thread_id, "user", user_text)


@pytest.mark.asyncio
async def test_old_turn_redelivery_within_window_is_suppressed(wired, monkeypatch):
    """A legacy tail path re-delivering an OLD turn's text (crashed-turn
    recovery, a follower that resumed late) must not re-post it. The old
    reply sits below the new turn's user row, so only the recent-window
    whole-thread scan can see it."""
    p = wired
    monkeypatch.setattr(main, "db", p._db)
    t = await p._db.create_thread(bot_id="main", title="old-turn")

    text = "Done. No new items today."
    assert await main._deliver_assistant_text(t.id, text) is not None

    await _simulate_new_turn(p, t.id)

    assert await main._deliver_assistant_text(t.id, text) is None, (
        "the old turn's reply was re-posted by a source-less re-delivery"
    )
    asst = [m.content for m in await p._db.dump_messages(t.id) if m.role == "assistant"]
    assert asst == [text], f"expected exactly one post, got {asst!r}"


@pytest.mark.asyncio
async def test_old_turn_redelivery_outside_window_still_posts(wired, monkeypatch):
    """The window is bounded, not eternal: a legitimate repeat of a line from
    beyond the ~5-minute horizon must still get through."""
    p = wired
    monkeypatch.setattr(main, "db", p._db)
    t = await p._db.create_thread(bot_id="main", title="old-turn-expired")

    text = "Done. No new items today."
    first = await main._deliver_assistant_text(t.id, text)
    assert first is not None

    # Backdate the stored row past the horizon, then open a new turn.
    old = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    await p._db.db.execute("UPDATE messages SET created_at = ? WHERE id = ?", (old, first.id))
    await p._db.db.commit()
    await _simulate_new_turn(p, t.id)

    again = await main._deliver_assistant_text(t.id, text)
    assert again is not None, "a repeat outside the recent window was wrongly eaten"
    asst = [m.content for m in await p._db.dump_messages(t.id) if m.role == "assistant"]
    assert asst == [text, text], asst


@pytest.mark.asyncio
async def test_source_id_delivery_is_not_subject_to_the_window(wired, monkeypatch):
    """Identity beats content: a NEW gateway source id for the same words is a
    new message even inside the window. The recent horizon is only for
    source-less deliveries (the legacy tail paths)."""
    p = wired
    monkeypatch.setattr(main, "db", p._db)
    t = await p._db.create_thread(bot_id="main", title="identity")

    text = "Same words, new identity."
    assert await main._deliver_assistant_text(t.id, text, source_id="gw:test:1") is not None
    await _simulate_new_turn(p, t.id)
    assert await main._deliver_assistant_text(t.id, text, source_id="gw:test:2") is not None, (
        "a new gateway message was wrongly eaten by the recent window"
    )

    asst = [m.content for m in await p._db.dump_messages(t.id) if m.role == "assistant"]
    assert asst == [text, text], asst


@pytest.mark.asyncio
async def test_distinct_texts_are_never_collapsed_by_the_window(wired, monkeypatch):
    """The conservative-window promise: two genuinely different lines inside
    the horizon are both posted."""
    p = wired
    monkeypatch.setattr(main, "db", p._db)
    t = await p._db.create_thread(bot_id="main", title="distinct")

    assert await main._deliver_assistant_text(t.id, "First answer.") is not None
    await _simulate_new_turn(p, t.id)
    assert await main._deliver_assistant_text(t.id, "Second answer.") is not None

    asst = [m.content for m in await p._db.dump_messages(t.id) if m.role == "assistant"]
    assert asst == ["First answer.", "Second answer."], asst
