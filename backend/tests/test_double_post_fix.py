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


# --------------------------------------------------------------------------- #
# Part 3: the abridged twin — trailing content dropped at the transport
# --------------------------------------------------------------------------- #
#
# The gateway WebSocket delivers the COMPLETE reply; the CLI/transcript-tail
# path delivers an ABRIDGED copy with trailing content silently dropped (a
# whole paragraph, the MEDIA lines, the `:react:` emoji — live 2026-08-26:
# 1,148 vs 1,034 chars, the shorter a PREFIX of the longer). The two copies
# get different canonical keys, so exact-key dedup posts both. The fix:
# structural twin detection (_is_twin — prefix with a 60-char floor, or the
# same 80-char head + media count) at EVERY dedup chokepoint, with the
# authority rule: keep the LONGER (complete) copy, suppress the SHORTER
# (abridged) one; equal length keeps the first; never suppress the complete
# copy.
# --------------------------------------------------------------------------- #

# A realistic complete reply whose tail — the final paragraph AND a media
# line — is exactly what the abridged transport dropped. Cutting at a clean
# paragraph boundary makes the abridged copy a strict prefix of the complete
# one (canonicalisation preserves the prefix; see _is_twin).
_TWIN_COMPLETE = (
    "Here is the full picture of what happened.\n\n"
    "The gateway persisted the complete reply, but the transcript-tail path "
    "recorded an abridged copy with the final paragraph silently dropped — "
    "so the two recordings of ONE reply became two different dedup keys, "
    "and both posted. The abridged copy is a prefix of the complete one, "
    "so structural comparison must see them as the same message and keep "
    "only the longer, complete copy.\n\n"
    "Final paragraph: this is the tail content the abridged recording "
    "lost, including the picture line below.\n\n"
    "[[media:/media/abc.png|the picture]]"
)
_TWIN_ABRIDGED = (
    "Here is the full picture of what happened.\n\n"
    "The gateway persisted the complete reply, but the transcript-tail path "
    "recorded an abridged copy with the final paragraph silently dropped — "
    "so the two recordings of ONE reply became two different dedup keys, "
    "and both posted. The abridged copy is a prefix of the complete one, "
    "so structural comparison must see them as the same message and keep "
    "only the longer, complete copy."
)


def test_twin_fixture_is_a_long_prefix():
    """The test texts themselves must satisfy the twin shape: the abridged
    copy is a strict prefix of the complete one, comfortably past the 60-char
    floor, and the complete copy carries a media line the abridged one lost."""
    assert len(_TWIN_ABRIDGED) >= 60
    assert _TWIN_COMPLETE.startswith(_TWIN_ABRIDGED)
    assert _TWIN_COMPLETE != _TWIN_ABRIDGED
    assert main._canon_msg(_TWIN_COMPLETE).startswith(main._canon_msg(_TWIN_ABRIDGED))
    assert "[[media:" in main._canon_msg(_TWIN_COMPLETE)
    assert "[[media:" not in main._canon_msg(_TWIN_ABRIDGED)


def test_msg_signature_counts_media_directives_in_canonical_text():
    """The signature head is the leading 80 canonical chars; the media count
    is the second component, so a tail-dropped media line CHANGES the
    signature (that is deliberate — media count is part of the fingerprint)
    while the same-head/same-count pair still matches."""
    bare = "A short reply with no pictures at all, just prose that runs on " \
           "long enough that the first eighty canonical characters are the " \
           "same with or without the trailing picture."
    with_pic = bare + "\n\n[[media:/media/a.png|pic]]"
    assert len(main._canon_msg(bare)) >= 80
    sig_bare = main._msg_signature(bare)
    sig_pic = main._msg_signature(with_pic)
    assert sig_bare[0] == sig_pic[0]          # identical opening
    assert sig_bare[1] == 0 and sig_pic[1] == 1
    assert sig_bare != sig_pic                # media count is part of the key
    assert sig_bare == main._msg_signature(main._canon_msg(bare))  # idempotent


def test_is_twin_rules_and_false_positive_guard():
    """The predicate itself: exact equality, prefix-with-60-char-floor, and
    same-signature all count as twins; a SHORT shared prefix does not."""
    # (a) exact canonical equality
    assert main._is_twin(_TWIN_ABRIDGED, _TWIN_ABRIDGED)
    # (b) abridged = strict prefix of complete, both orders
    assert main._is_twin(_TWIN_COMPLETE, _TWIN_ABRIDGED)
    assert main._is_twin(_TWIN_ABRIDGED, _TWIN_COMPLETE)
    # (c) same leading 80 chars + same media count, divergent tails
    head = "The opening of this reply is identical for both recordings. " * 2
    assert main._is_twin(head + "AAA", head + "BBB")
    # FALSE-POSITIVE GUARD: a shared prefix below 60 chars is coincidence
    assert not main._is_twin("The answer is A.", "The answer is B.")
    assert not main._is_twin("Done!", "Done — more below.")
    # different openings / different media counts are never twins
    assert not main._is_twin(_TWIN_COMPLETE, "Something else entirely, " + _TWIN_ABRIDGED)


@pytest.mark.asyncio
async def test_complete_then_abridged_twin_posts_once(wired, monkeypatch):
    """The common ordering: the gateway's COMPLETE copy lands first, then the
    tail path re-offers the same reply ABRIDGED (trailing paragraph and the
    MEDIA line dropped). The abridged copy must be suppressed — exactly one
    post, and it is the complete one."""
    p = wired
    monkeypatch.setattr(main, "db", p._db)
    t = await p._db.create_thread(bot_id="main", title="twin-complete-first")

    full = await main._deliver_assistant_text(t.id, _TWIN_COMPLETE)
    assert full is not None
    abridged = await main._deliver_assistant_text(t.id, _TWIN_ABRIDGED)
    assert abridged is None, "the abridged twin re-posted the complete reply"

    asst = [m.content for m in await p._db.dump_messages(t.id) if m.role == "assistant"]
    assert asst == [_TWIN_COMPLETE], f"expected exactly one post, got {asst!r}"
    assert p.visible() == [_TWIN_COMPLETE], p.frames


@pytest.mark.asyncio
async def test_abridged_then_complete_twin_upgrades_to_single_complete_post(wired, monkeypatch):
    """The incident ordering: the ABRIDGED copy is persisted first, then the
    complete gateway copy arrives. Authority rule: the complete copy is never
    suppressed — the abridged row is replaced, leaving exactly one post, the
    complete one (and a message_deleted broadcast so live clients drop the
    abridged bubble)."""
    p = wired
    monkeypatch.setattr(main, "db", p._db)
    t = await p._db.create_thread(bot_id="main", title="twin-abridged-first")

    abridged = await main._deliver_assistant_text(t.id, _TWIN_ABRIDGED)
    assert abridged is not None
    full = await main._deliver_assistant_text(t.id, _TWIN_COMPLETE)
    assert full is not None, "the complete copy was suppressed by its abridged twin"

    asst = [m.content for m in await p._db.dump_messages(t.id) if m.role == "assistant"]
    assert asst == [_TWIN_COMPLETE], f"expected exactly one post, got {asst!r}"
    deleted = [f for f in p.frames if f.get("type") == "message_deleted"]
    assert deleted and deleted[0]["message_id"] == abridged.id, p.frames


@pytest.mark.asyncio
async def test_distinct_messages_sharing_only_a_short_prefix_both_post(wired, monkeypatch):
    """False-positive guard: two genuinely DIFFERENT replies that share a
    short opening (well under the 60-char floor) are both delivered."""
    p = wired
    monkeypatch.setattr(main, "db", p._db)
    t = await p._db.create_thread(bot_id="main", title="short-prefix-distinct")

    a = "The answer is definitely A. Here is why: it has been the case for years."
    b = "The answer is definitely B. Here is why: the evidence points elsewhere."
    assert not main._is_twin(a, b)
    assert await main._deliver_assistant_text(t.id, a) is not None
    assert await main._deliver_assistant_text(t.id, b) is not None

    asst = [m.content for m in await p._db.dump_messages(t.id) if m.role == "assistant"]
    assert asst == [a, b], f"distinct replies were collapsed: {asst!r}"


@pytest.mark.asyncio
async def test_equal_length_twin_keeps_the_first(wired, monkeypatch):
    """Two recordings of the same length (identical 80-char head, divergent
    tails — the signature rule) — equal length keeps the FIRST; the second is
    suppressed."""
    p = wired
    monkeypatch.setattr(main, "db", p._db)
    t = await p._db.create_thread(bot_id="main", title="equal-length-twin")

    head = "The opening of this reply is identical for both recordings. " * 2
    first = head + "AAA"
    second = head + "BBB"
    assert len(first) == len(second)
    assert main._msg_signature(first) == main._msg_signature(second)

    assert await main._deliver_assistant_text(t.id, first) is not None
    assert await main._deliver_assistant_text(t.id, second) is None

    asst = [m.content for m in await p._db.dump_messages(t.id) if m.role == "assistant"]
    assert asst == [first], f"expected the first copy only, got {asst!r}"
