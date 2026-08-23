"""The gap sweep must not manufacture duplicates — or break their pictures.

Observed failure: a reply carrying five generated images was delivered
correctly, and then re-posted by the 10-minute gap sweep four times over the
following half hour — each copy with more of
its pictures replaced by "🖼️ *(image unavailable: …)*" than the last, because
the agent's `/tmp/agent-batch/*.png` source files were deleted between sweeps.
A second, unrelated reply with no media at all re-posted five times over the
same period.

Both had the same shape: the sweep asks "is this transcript item already in the
DB?" by comparing a canonical key, and the key computed from the RAW transcript
text stopped matching the key computed from the PERSISTED text.

  * media  — persisting rewrites [[media:/tmp/x.png]] to [[media:/media/<uuid>]]
             and the key bridged that with a hash of the file's bytes. The
             instant the source file was deleted, the two sides hashed
             different things (or nothing), and every sweep re-imported.
  * markers — persisting strips `:react:<id>:`; the key did not, so any reply
             that fired a reaction never matched its own stored copy.

So the rules under test: a canonical key mirrors EVERY transform persisting
applies, it never depends on a file that can be deleted, and the sweep
considers each transcript item exactly once no matter what the key says.
"""
from __future__ import annotations

import contextlib
import json
import tempfile
from pathlib import Path

import pytest

from app import config, main, openclaw
from app.database import Database

_open_dbs: list[Database] = []


@pytest.fixture(autouse=True)
async def _close_dbs_after_test():
    yield
    for db in _open_dbs:
        with contextlib.suppress(Exception):
            await db.close()
    _open_dbs.clear()


@pytest.fixture(autouse=True)
def _media_dir(tmp_path, monkeypatch):
    """Ingest must land in a throwaway store, never the real ~/.local/share."""
    d = tmp_path / "media"
    d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config, "MEDIA_DIR", d)
    monkeypatch.setattr(main, "MEDIA_DIR", d)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    main._media_origins_reset()
    yield d
    main._media_origins_reset()


async def _fresh_db() -> Database:
    tmp = Path(tempfile.mkdtemp(prefix="mdtest-")) / "chats.db"
    db = Database(tmp)
    await db.connect()
    _open_dbs.append(db)
    main.db = db
    main._delivered.clear()

    async def _noop(_frame):
        return None

    main.manager.broadcast = _noop
    return db


def _write_transcript(root: Path, bot: str, session_key: str, session_id: str,
                      texts: list[str]) -> None:
    sdir = root / bot / "sessions"
    sdir.mkdir(parents=True, exist_ok=True)
    idx_path = sdir / "sessions.json"
    idx = json.loads(idx_path.read_text()) if idx_path.is_file() else {}
    idx[session_key] = {"sessionId": session_id}
    idx_path.write_text(json.dumps(idx))
    lines = [{"type": "message", "message": {"role": "user",
              "content": [{"type": "text", "text": "do the thing"}]}}]
    for t in texts:
        lines.append({"type": "message", "message": {"role": "assistant",
                      "content": [{"type": "text", "text": t}]}})
    (sdir / f"{session_id}.jsonl").write_text(
        "\n".join(json.dumps(x) for x in lines) + "\n")


def _png(path: Path, payload: bytes) -> Path:
    """A file with a real PNG signature and distinct bytes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + payload)
    return path


# --------------------------------------------------------------------------- #
# Root cause A: a media message whose source file is deleted after ingest
# --------------------------------------------------------------------------- #


async def test_media_message_not_reimported_after_source_file_is_deleted(
        monkeypatch_root: Path, tmp_path):
    """The exact live failure: deliver, delete the temp source, sweep again."""
    db = await _fresh_db()
    openclaw.OPENCLAW_AGENTS_DIR = monkeypatch_root
    th = await db.create_thread(bot_id="main")
    src = _png(tmp_path / "agent-batch" / "1-profile.png", b"profile-bytes")
    text = f"Five renders, all cooked and served hot ⚡\n[[media:{src}|① Profile]]"
    _write_transcript(monkeypatch_root, "main",
                      openclaw.session_key_for("main", th.id), "sid-1", [text])

    assert await main._import_transcript_messages(th.id, "main") == 1
    stored = (await db.dump_messages(th.id))[0].content
    assert "[[media:/media/" in stored, stored

    src.unlink()                       # the agent's next batch wipes /tmp
    main._delivered.clear()            # a restart, or 256 messages later

    assert await main._import_transcript_messages(th.id, "main") == 0
    msgs = await db.dump_messages(th.id)
    assert len(msgs) == 1, [m.content for m in msgs]
    assert "image unavailable" not in msgs[0].content


async def test_two_captionless_images_in_one_turn_stay_distinct(
        monkeypatch_root: Path, tmp_path):
    """The anti-collision property the byte-hash was introduced for.

    Erasing the path entirely made two different pictures posted in one turn
    with no caption compare equal, silently dropping the second. Whatever
    replaces the hash must keep them apart.
    """
    db = await _fresh_db()
    openclaw.OPENCLAW_AGENTS_DIR = monkeypatch_root
    th = await db.create_thread(bot_id="main")
    a = _png(tmp_path / "shots" / "a.png", b"aaaa")
    b = _png(tmp_path / "shots" / "b.png", b"bbbb")
    _write_transcript(monkeypatch_root, "main",
                      openclaw.session_key_for("main", th.id), "sid-1",
                      [f"[[media:{a}]]", f"[[media:{b}]]"])

    assert await main._import_transcript_messages(th.id, "main") == 2
    assert len(await db.dump_messages(th.id)) == 2


async def test_ingest_reuses_the_stored_copy_when_the_source_is_gone(tmp_path):
    """A picture already in /media never degrades to an 'unavailable' note.

    Ingest keeps the bytes; losing the temp file it was copied FROM must not
    lose the picture. Only a path that was never ingested can be unavailable.
    """
    src = _png(tmp_path / "gone" / "shot.png", b"kept-bytes")
    first = main._ingest_content_media(f"[[media:{src}|Tweak B]]")
    assert "[[media:/media/" in first, first

    src.unlink()
    again = main._ingest_content_media(f"[[media:{src}|Tweak B]]")
    assert again == first, again
    assert "image unavailable" not in again

    never = main._ingest_content_media("[[media:/tmp/never-existed.png|X]]")
    assert "image unavailable" in never


# --------------------------------------------------------------------------- #
# Root cause B: persisting strips `:react:` markers, the dedup key did not
# --------------------------------------------------------------------------- #


def test_a_quoted_directive_is_an_explanation_not_a_picture(tmp_path):
    """Talking about the syntax must not rewrite the sentence you said it in.

    Observed damage: an agent explaining how to send pictures had its own example
    replaced by the failure note — "correct `🖼️ *(image unavailable: path)*`
    directive" — so the message documenting the feature read as broken.
    """
    real = _png(tmp_path / "real.png", b"real")
    text = (f"Use `[[media:/tmp/x.png|cap]]` for pictures.\n"
            f"```\n[[media:/tmp/y.png|also quoted]]\n```\n"
            f"Here is an actual one: [[media:{real}|Real]]")
    out = main._ingest_content_media(text)
    assert "`[[media:/tmp/x.png|cap]]`" in out, out
    assert "[[media:/tmp/y.png|also quoted]]" in out, out
    assert "image unavailable" not in out, out
    assert "[[media:/media/" in out, out


def test_a_path_shown_in_code_is_not_salvaged_into_a_picture(tmp_path):
    shown = _png(tmp_path / "shown.png", b"shown")
    text = f"Run `cp {shown} /dest` to copy it."
    assert main._salvage_media_refs(text) == text


async def test_reply_that_fired_a_reaction_is_not_reimported(monkeypatch_root: Path):
    db = await _fresh_db()
    openclaw.OPENCLAW_AGENTS_DIR = monkeypatch_root
    th = await db.create_thread(bot_id="main")
    text = "Done and verified. Three files updated.\n\n:react:task_complete:"
    _write_transcript(monkeypatch_root, "main",
                      openclaw.session_key_for("main", th.id), "sid-1", [text])

    assert await main._import_transcript_messages(th.id, "main") == 1
    stored = (await db.dump_messages(th.id))[0].content
    assert ":react:" not in stored, stored

    main._delivered.clear()
    assert await main._import_transcript_messages(th.id, "main") == 0
    assert len(await db.dump_messages(th.id)) == 1


def test_canonical_key_mirrors_every_persist_transform():
    """Unit-level statement of the invariant both bugs violated."""
    raw = "Shipped it. :react:task_complete:"
    persisted = "Shipped it."
    assert main._canon_msg(raw) == main._canon_msg(persisted)


# --------------------------------------------------------------------------- #
# The structural backstop: content keys are a heuristic, identity is not
# --------------------------------------------------------------------------- #


async def test_upgrading_does_not_re_post_history(monkeypatch_root: Path, tmp_path):
    """The upgrade must not do one final round of the damage it fixes.

    Simulates a real pre-fix database: the reply is stored with its picture
    already served from /media, the agent's scratch file is long gone, and no
    origin was ever recorded — so the origin bridge cannot work retroactively
    and the text keys genuinely cannot match. Measured against the live DB,
    seven messages in one thread would have re-posted.
    """
    db = await _fresh_db()
    openclaw.OPENCLAW_AGENTS_DIR = monkeypatch_root
    th = await db.create_thread(bot_id="main")
    _png(config.MEDIA_DIR / "legacyuuid.png", b"served-bytes")
    await db.add_message(th.id, "assistant",
                         "Five renders ⚡\n[[media:/media/legacyuuid.png|① Profile]]")
    _write_transcript(monkeypatch_root, "main",
                      openclaw.session_key_for("main", th.id), "sid-1",
                      ["Five renders ⚡\n[[media:/tmp/agent-batch/1-profile.png|① Profile]]"])

    await main._migrate_transcript_seen_backfill()

    assert await main._import_transcript_messages(th.id, "main") == 0
    msgs = await db.dump_messages(th.id)
    assert len(msgs) == 1, [m.content for m in msgs]


async def test_backfill_still_delivers_a_genuine_gap(monkeypatch_root: Path):
    """It marks what is present, not everything — a real gap survives it."""
    db = await _fresh_db()
    openclaw.OPENCLAW_AGENTS_DIR = monkeypatch_root
    th = await db.create_thread(bot_id="main")
    await db.add_message(th.id, "assistant", "the reply that landed")
    _write_transcript(monkeypatch_root, "main",
                      openclaw.session_key_for("main", th.id), "sid-1",
                      ["the reply that landed", "the reply that never landed"])

    await main._migrate_transcript_seen_backfill()

    assert await main._import_transcript_messages(th.id, "main") == 1
    assert [m.content for m in await db.dump_messages(th.id)] == [
        "the reply that landed", "the reply that never landed"]


# --------------------------------------------------------------------------- #
# The in-turn twin: one block, two texts, both posted
# --------------------------------------------------------------------------- #


def test_payload_that_lost_its_picture_lines_defers_to_the_transcript():
    """The gateway's reply payload can drop an agent's `MEDIA:/path` lines.

    Live, 2026-08-08 21:17:58 — the same block reached the chat twice, 0.3s
    apart: the CLI payload with the picture lines MISSING, then the
    reconciler's transcript copy with them intact. Dedup compares text, the
    texts genuinely differed, so both posted — the reply appeared twice, once
    without its pictures. 34 such pairs across the history, all differing only
    in pictures/markers/whitespace.

    Nothing downstream can repair that: by the time both are text they are two
    different messages. The turn has both versions in hand, so it picks the one
    that still has the pictures and every path then agrees on one text.
    """
    prose = ("Here are the cropped images:\n\n**Test 1**\n{a}\n\n"
             "**Test 2**\n{b}\n\nTight enough?")
    payload = prose.format(a="", b="").replace("\n\n\n", "\n\n")
    transcript = prose.format(a="MEDIA:/tmp/a-tight.png", b="MEDIA:/tmp/b-tight.png")

    assert main._prefer_richer_media_twin(payload, [transcript]) == transcript
    # A payload that kept its own pictures is left alone.
    assert main._prefer_richer_media_twin(transcript, [payload]) == transcript
    # Different prose is never substituted, however similar.
    assert main._prefer_richer_media_twin(payload, ["Something else entirely"]) == payload
    # No candidates, no change.
    assert main._prefer_richer_media_twin(payload, []) == payload


async def test_one_block_delivered_by_three_paths_posts_once_with_pictures(tmp_path):
    """narration → payload → reconciler, the real in-turn sequence."""
    db = await _fresh_db()
    th = await db.create_thread(bot_id="main")
    a = _png(tmp_path / "a-tight.png", b"aaa")
    b = _png(tmp_path / "b-tight.png", b"bbb")
    transcript = (f"Here are the cropped images:\n\n**Test 1**\nMEDIA:{a}\n\n"
                  f"**Test 2**\nMEDIA:{b}\n\nTight enough?")
    payload = "Here are the cropped images:\n**Test 1**\n**Test 2**\nTight enough?"

    chosen = main._prefer_richer_media_twin(payload, [transcript])
    await main._deliver_assistant_text(th.id, transcript, metadata={"model": "x"})
    await main._deliver_assistant_text(th.id, chosen, metadata={"tokens": 1}, stream=True)
    await main._deliver_assistant_text(th.id, transcript, metadata=None)   # reconciler

    msgs = await db.dump_messages(th.id)
    assert len(msgs) == 1, [m.content for m in msgs]
    assert msgs[0].content.count("[[media:/media/") == 2, msgs[0].content


async def test_sweep_considers_each_transcript_item_exactly_once(
        monkeypatch_root: Path, monkeypatch):
    """Even with content matching sabotaged, a sweep cannot post a second copy.

    Canonicalisation has now drifted from persisting twice (media paths, then
    reaction markers). The sweep must not depend on it being perfect: a
    transcript item it has already accounted for is not a gap, whatever the
    key says.
    """
    db = await _fresh_db()
    openclaw.OPENCLAW_AGENTS_DIR = monkeypatch_root
    th = await db.create_thread(bot_id="main")
    _write_transcript(monkeypatch_root, "main",
                      openclaw.session_key_for("main", th.id), "sid-1",
                      ["the only reply"])

    assert await main._import_transcript_messages(th.id, "main") == 1

    seq = iter(range(1000))
    monkeypatch.setattr(main, "_canon_msg", lambda s: f"drifted-{next(seq)}")
    main._delivered.clear()

    assert await main._import_transcript_messages(th.id, "main") == 0
    assert len(await db.dump_messages(th.id)) == 1


# --------------------------------------------------------------------------- #
# The third drift, found in review of the fix above: the canonical key still
# read the DISK through _salvage_media_refs. A BARE path ("Here it is
# /tmp/shot.png", no directive) is wrapped at persist only while the file
# exists — so the raw-side key changed the moment the agent wiped its scratch
# dir, and the follower (2h window; rescans from offset 0 after a truncation)
# or the sweep's first visit re-posted the reply, pictureless. Same disease
# as the byte-hash, one seam over.
# --------------------------------------------------------------------------- #


async def test_bare_path_reply_not_duplicated_after_source_deletion(tmp_path):
    """The live shape: deliver a bare-path reply, wipe the scratch file, and a
    redundant path (follower/reconciler/sweep) re-offers the same raw text."""
    db = await _fresh_db()
    th = await db.create_thread(bot_id="main")
    src = _png(tmp_path / "scratch" / "shot.png", b"shot-bytes")
    raw = f"Here is the screenshot {src} enjoy"

    assert await main._deliver_assistant_text(th.id, raw) is not None
    stored = (await db.dump_messages(th.id))[0].content
    assert "[[media:/media/" in stored, stored

    src.unlink()                       # the agent's next batch wipes /tmp
    main._delivered.clear()            # a restart, or ring eviction

    assert await main._deliver_assistant_text(th.id, raw) is None
    assert len(await db.dump_messages(th.id)) == 1


def test_canon_key_does_not_depend_on_bare_path_file_existing(tmp_path):
    """Unit-level statement: canon(raw) == canon(stored) survives deletion."""
    src = _png(tmp_path / "scratch" / "pic.png", b"pic-bytes")
    raw = f"Saved it to {src} for you"
    stored = main._ingest_content_media(main._salvage_media_refs(raw))
    assert "[[media:/media/" in stored, stored
    assert main._canon_msg(raw) == main._canon_msg(stored)

    src.unlink()
    assert main._canon_msg(raw) == main._canon_msg(stored)


def test_salvage_display_behaviour_unchanged(tmp_path):
    """Only the CANON walk assumes files exist. The display path still wraps a
    bare path only when the file is real — a path merely mentioned in prose
    must stay prose (the rule _salvage_media_refs has always enforced)."""
    out = main._salvage_media_refs("try /tmp/never-was-here.png maybe")
    assert "[[media:" not in out, out


async def test_in_store_absolute_path_keeps_canon_stable(_media_dir, tmp_path):
    """An agent can reference a file by its absolute path INSIDE the served
    store (e.g. re-sending a picture DisPatch already holds). Persisting
    shortens it to /media/<rel> without copying — and before origins were
    recorded for that branch, the transcript side (src:<abs>) and the stored
    side (src:/media/<rel>) could never match, so the sweep's first visit
    after a live delivery re-posted the reply once."""
    inside = _png(_media_dir / "feedpic.png", b"feed-bytes")
    raw = f"Fresh from the feed [[media:{inside}|Feed]]"
    stored = main._ingest_content_media(raw)
    assert "[[media:/media/feedpic.png|Feed]]" in stored, stored
    assert main._canon_msg(raw) == main._canon_msg(stored)


def test_repair_script_heal_deletes_the_donor(tmp_path):
    """scripts/repair_swept_duplicates.py: after healing the earliest row with
    a later copy's pictures, that DONOR row is itself a swept duplicate and
    must go. An earlier version rebound its survivor variable to the donor, so
    the id check protected the wrong row — every healed thread kept one healed
    copy plus one identical donor copy."""
    import sqlite3
    import subprocess
    import sys

    db_path = tmp_path / "chats.db"
    media = tmp_path / "media"
    _png(media / "u1.png", b"pic-one")
    con = sqlite3.connect(db_path)
    con.executescript(
        "CREATE TABLE messages (id TEXT PRIMARY KEY, thread_id TEXT, role TEXT, "
        "content TEXT, metadata TEXT, created_at TEXT);")
    swept_meta = json.dumps({"followup": True})
    degraded = "Five renders ⚡\n🖼️ *(image unavailable: 1)*"
    intact = "Five renders ⚡\n[[media:/media/u1.png|1]]"
    con.executemany(
        "INSERT INTO messages VALUES (?,?,?,?,?,?)",
        [
            # The sweep delivered it first, picture already degraded…
            ("a", "t1", "assistant", degraded, swept_meta, "2020-01-01T07:17:00"),
            # …a re-import while the source still lived has the picture…
            ("b", "t1", "assistant", intact, swept_meta, "2020-01-01T07:38:00"),
            # …and a later one degraded again.
            ("c", "t1", "assistant", degraded, swept_meta, "2020-01-01T07:58:00"),
            # A live-path row saying something else is never touched.
            ("d", "t1", "assistant", "unrelated reply", None, "2020-01-01T08:00:00"),
        ])
    con.commit()
    con.close()

    script = Path(__file__).resolve().parents[2] / "scripts" / "repair_swept_duplicates.py"
    r = subprocess.run(
        [sys.executable, str(script), "--db", str(db_path),
         "--media-dir", str(media), "--go"],
        capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr

    con = sqlite3.connect(db_path)
    left = con.execute(
        "SELECT id, content FROM messages ORDER BY created_at").fetchall()
    con.close()
    assert [rid for rid, _ in left] == ["a", "d"], left
    assert left[0][1] == intact          # healed: earliest timestamp, pictures


def test_payload_media_url_is_dropped_when_the_transcript_twin_wins(tmp_path):
    """The gateway can hoist a block's picture into `mediaUrl` while dropping
    the MEDIA line from the text (live, 2026-07-31 08:40:40: the transcript
    copy carried [[media:…]] inline, the payload carried the same prose with
    the picture as media_url — both posted). When the transcript twin is
    chosen, its inline directives are the block's complete media record; the
    payload's media_url is the same picture again, and keeping it renders the
    image twice in one message."""
    from app.openclaw import AgentPayload
    pic = _png(tmp_path / "gpu" / "she.png", b"she-bytes")
    transcript = f"20 seconds on the GPU. Here it is.\n[[media:{pic}]]\n1024×1536."
    payload = AgentPayload(text="20 seconds on the GPU. Here it is.\n1024×1536.",
                           media_url=str(pic))

    text, media_url = main._settle_payload(payload, [transcript])
    assert text == transcript
    assert media_url is None, "the twin's inline picture would render twice"


def test_payload_media_url_survives_when_no_twin_matches(tmp_path):
    """No matching transcript twin means the media_url is the only copy of the
    picture — dropping it there would lose media, the cardinal sin."""
    from app.openclaw import AgentPayload
    pic = _png(tmp_path / "gpu" / "only.png", b"only-bytes")
    payload = AgentPayload(text="Here it is.", media_url=str(pic))

    text, media_url = main._settle_payload(payload, ["Something else entirely"])
    assert text == "Here it is."
    assert media_url == str(pic)


def test_substitution_prefers_the_last_matching_block():
    """Two same-prose blocks with different pictures in one turn: the payload
    is the turn's FINAL block, so the substitution must take the LAST matching
    narration — taking the first swapped the two pictures' order in the chat
    (the final message wore the earlier block's image)."""
    first = "Here you go:\nMEDIA:/tmp/a.png"
    second = "Here you go:\nMEDIA:/tmp/b.png"
    chosen = main._prefer_richer_media_twin("Here you go:", [first, second])
    assert chosen == second, chosen


async def test_reaction_trace_row_does_not_hide_an_in_turn_duplicate():
    """A reply that fires a reaction persists a `system` trace row right after
    itself. The DB-side dedup walks the trailing run newest-first and used to
    STOP at the first non-assistant row — so once the trace landed, the reply
    above it was invisible to the scan, and any cold-set redundant path
    (crash recovery, a WS backfill after restart) re-posted it. The trace is
    part of the turn, not its boundary; other system rows keep boundary duty
    (in user-less mirror threads they are all that separates turns)."""
    db = await _fresh_db()
    th = await db.create_thread(bot_id="main")
    await db.add_message(th.id, "user", "do the thing")
    text = "Done and verified. Three files updated."
    assert await main._deliver_assistant_text(th.id, text) is not None
    await db.add_message(th.id, "system", "⚡ the bot reacted · Task Complete",
                         metadata={"kind": "reaction"})

    main._delivered.clear()            # a restart: only the DB check remains
    assert await main._deliver_assistant_text(th.id, text) is None
    msgs = await db.dump_messages(th.id)
    assert [m.content for m in msgs if m.role == "assistant"] == [text]


async def test_second_look_reply_already_delivered_by_the_gateway_posts_once(
        tmp_path, monkeypatch):
    """_media_second_look used to persist its fix reply DIRECTLY, skipping the
    funnel — and with the gateway WS transport live, the same reply arrives
    over the socket seconds earlier. Both copies posted — two ingests seconds
    apart on one thread, and each ingest also duplicated the three picture
    blobs. Every path persists through the funnel, no
    exceptions; the funnel's dedup is what makes redundant delivery safe."""
    from app import openclaw

    db = await _fresh_db()
    th = await db.create_thread(bot_id="main")
    pic = _png(tmp_path / "fix" / "one.png", b"fix-bytes")
    fix_text = f"My bad — wrong directive format. Here they are properly:\n[[media:{pic}|One]]"

    async def _fake_send(*, bot_id, session_key, message):
        return openclaw.AgentReply(
            payloads=[openclaw.AgentPayload(text=fix_text)], metadata={})

    monkeypatch.setattr(openclaw, "send_to_agent", _fake_send)

    # The WS transport delivers the fix reply the moment the gateway writes it.
    assert await main._deliver_assistant_text(
        th.id, fix_text, source_id="gw:agent:main:t:7fbec832") is not None

    claiming = await db.add_message(
        th.id, "assistant", "Here are the images! Fresh batch for you.")
    await main._media_second_look(th.id, "main", f"agent:main:{th.id}", [claiming])

    msgs = [m for m in await db.dump_messages(th.id) if m.role == "assistant"]
    fixes = [m for m in msgs if "wrong directive format" in (m.content or "")]
    assert len(fixes) == 1, [m.content[:60] for m in fixes]


def test_tilde_directive_is_ingested(tmp_path, monkeypatch):
    """[[media:~/pics/x.png]] must serve like any other local path.

    It used to fall through every branch — not ingested (no leading slash),
    not noted as unavailable — and reach the browser verbatim, where
    normalizeMediaUrl left it relative and the <img> 404'd. The fingerprint
    side (`_media_fingerprint`) already expanded `~`; ingest now agrees."""
    monkeypatch.setenv("HOME", str(tmp_path))
    pic = _png(tmp_path / "pics" / "cat.png", b"cat-bytes")
    out = main._ingest_content_media("[[media:~/pics/cat.png|Cat]]")
    assert "[[media:/media/" in out, out
    # And the canon bridge holds once the tilde source dies.
    raw = "[[media:~/pics/cat.png|Cat]]"
    assert main._canon_msg(raw) == main._canon_msg(out)
    pic.unlink()
    assert main._canon_msg(raw) == main._canon_msg(out)


# --------------------------------------------------------------------------- #
# A media base under a world-writable parent must prove it is ours
# --------------------------------------------------------------------------- #


def test_media_base_must_be_a_real_directory_we_own(tmp_path, monkeypatch):
    """`/tmp/openclaw` lives under a world-writable parent, so on a shared host
    anyone can create it first — or point it at `/` — and every media-extension
    file underneath becomes retrievable through /api/media."""
    real = tmp_path / "real"
    real.mkdir()
    assert main._is_trustworthy_base(real) is True

    link = tmp_path / "link"
    link.symlink_to("/")
    assert main._is_trustworthy_base(link) is False, "a planted symlink was honoured"

    assert main._is_trustworthy_base(tmp_path / "missing") is False
    plain = tmp_path / "file"
    plain.write_text("x")
    assert main._is_trustworthy_base(plain) is False

    # Foreign ownership is refused (root owns /, and the tests do not run as root).
    import os
    if os.getuid() != 0:
        assert main._is_trustworthy_base(Path("/")) is False

    # And the base list drops whatever fails the test.
    monkeypatch.setattr(main, "MEDIA_DIR", link)
    assert link.resolve() not in main._allowed_media_bases()
