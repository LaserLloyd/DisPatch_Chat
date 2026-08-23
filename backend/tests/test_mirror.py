"""Tests for the continuous gateway-chat mirror (_gateway_mirror_loop et al).

Hermetic like test_recovery.py: throwaway DB + a synthetic OpenClaw agents tree
(OPENCLAW_AGENTS_DIR monkeypatched), so nothing touches live data or sessions.
"""
from __future__ import annotations

import contextlib
import json
import os
import tempfile
import time
import uuid
from pathlib import Path

import pytest

from app import main, openclaw
from app.database import Database

# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #

_open_dbs: list[Database] = []


@pytest.fixture(autouse=True)
async def _close_dbs_after_test():
    yield
    for db in _open_dbs:
        with contextlib.suppress(Exception):
            await db.close()
    _open_dbs.clear()


async def _fresh_db() -> Database:
    tmp = Path(tempfile.mkdtemp(prefix="dtest-")) / "chats.db"
    db = Database(tmp)
    await db.connect()
    _open_dbs.append(db)
    main.db = db
    main._delivered.clear()
    main._thread_bot.clear()
    # Any earlier test that drove the app lifespan to completion leaves the
    # module-global shutdown latch set; the mirror honors it and would no-op.
    main._shutting_down = False

    async def _noop(_frame):
        return None
    main.manager.broadcast = _noop
    return db


def _user(t: str) -> dict:
    return {"type": "message", "message": {
        "role": "user", "content": [{"type": "text", "text": t}]}}


def _asst(t: str) -> dict:
    return {"type": "message", "message": {"role": "assistant", "content": [
        {"type": "thinking", "thinking": "hmm"},
        {"type": "toolCall", "name": "exec", "arguments": {"cmd": "true"}},
        {"type": "text", "text": t},
    ]}}


def _write_session(root: Path, agent: str, key: str, sid: str,
                   lines: list[dict]) -> Path:
    sdir = root / agent / "sessions"
    sdir.mkdir(parents=True, exist_ok=True)
    idx_path = sdir / "sessions.json"
    idx = json.loads(idx_path.read_text()) if idx_path.is_file() else {}
    idx[key] = {"sessionId": sid}
    idx_path.write_text(json.dumps(idx))
    f = sdir / f"{sid}.jsonl"
    f.write_text("".join(json.dumps(x) + "\n" for x in lines))
    return f


def _append_lines(f: Path, lines: list[dict]) -> None:
    with open(f, "a") as fh:
        for x in lines:
            fh.write(json.dumps(x) + "\n")


def _fresh_state() -> dict:
    return {"version": 1, "sessions": {}}


UUID_TAG = "282f02d4-eb90-457a-abae-6ccd5e3c243e"


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #

def test_mirror_kind_classification():
    mk = openclaw.mirror_kind
    assert mk(f"agent:main:{UUID_TAG}") == "webchat"
    assert mk("agent:main:main") == "main"
    assert mk("agent:ai_swift:main") == "main"
    assert mk("agent:ai_swift:ai_swift") == "main"
    assert mk("agent:main:daily-main-2026-07-17") == "daily"
    assert mk("agent:main:cron:e99748e3") == "cron"
    assert mk("agent:x:subagent:y") == "subagent"
    assert mk("agent:x:dashboard") == "dashboard"
    assert mk("agent:ai_swift:wd-6b7fa86dd3a4") == "other"
    assert mk("agent:main:explicit:codex-email-scope") == "other"
    assert mk(f"agent:example:acp:{UUID_TAG}") == "other"
    assert mk("agent:main:email-inventory-20260716") == "other"
    assert mk("short") == "other"


# --------------------------------------------------------------------------- #
# Core mirroring
# --------------------------------------------------------------------------- #

async def test_webchat_session_mirrored(monkeypatch_root):
    db = await _fresh_db()
    openclaw.OPENCLAW_AGENTS_DIR = monkeypatch_root
    _write_session(monkeypatch_root, "main", f"agent:main:{UUID_TAG}", "sid-1",
                   [_user("hi bot"), _asst("hello there")])
    state = _fresh_state()
    assert await main._mirror_cycle(state) is True

    t = await db.get_thread(UUID_TAG)          # thread id == gateway thread tag
    assert t is not None and t.bot_id == "main"
    assert t.title.startswith("Webchat · hi bot")
    msgs = await db.dump_messages(UUID_TAG)
    assert [(m.role, m.content) for m in msgs] == [
        ("user", "hi bot"), ("assistant", "hello there")]
    assert all((m.metadata or {}).get("mirrored") for m in msgs)


async def test_tail_appends_only_new(monkeypatch_root):
    db = await _fresh_db()
    openclaw.OPENCLAW_AGENTS_DIR = monkeypatch_root
    f = _write_session(monkeypatch_root, "main", f"agent:main:{UUID_TAG}", "sid-1",
                       [_user("q1"), _asst("a1")])
    state = _fresh_state()
    await main._mirror_cycle(state)
    assert len(await db.dump_messages(UUID_TAG)) == 2

    _append_lines(f, [_user("q2"), _asst("a2")])
    await main._mirror_cycle(state)
    msgs = await db.dump_messages(UUID_TAG)
    assert [(m.role, m.content) for m in msgs] == [
        ("user", "q1"), ("assistant", "a1"), ("user", "q2"), ("assistant", "a2")]

    # A third cycle with nothing new must be a no-op.
    await main._mirror_cycle(state)
    assert len(await db.dump_messages(UUID_TAG)) == 4


async def test_partial_line_left_for_next_cycle(monkeypatch_root):
    db = await _fresh_db()
    openclaw.OPENCLAW_AGENTS_DIR = monkeypatch_root
    f = _write_session(monkeypatch_root, "main", f"agent:main:{UUID_TAG}", "sid-1",
                       [_user("q1"), _asst("a1")])
    # Simulate the gateway mid-append: half a JSON line, no newline yet.
    whole = json.dumps(_asst("a2")) + "\n"
    with open(f, "a") as fh:
        fh.write(whole[:25])
    state = _fresh_state()
    await main._mirror_cycle(state)
    assert len(await db.dump_messages(UUID_TAG)) == 2   # a2 not parsed yet

    with open(f, "a") as fh:
        fh.write(whole[25:])
    await main._mirror_cycle(state)
    msgs = await db.dump_messages(UUID_TAG)
    assert msgs[-1].content == "a2" and len(msgs) == 3


async def test_native_thread_gap_backfill(monkeypatch_root):
    """A session bound to a real DisPatch thread is tailed INTO that thread:
    the mirror fills what the live funnel structurally misses (a user message
    typed on the Control-UI side, a reply after the follower window) without
    double-posting what the live funnel already delivered."""
    db = await _fresh_db()
    openclaw.OPENCLAW_AGENTS_DIR = monkeypatch_root
    native_id = str(uuid.uuid4())
    await db.create_thread(bot_id="main", title="native", thread_id=native_id)
    # The live funnel already delivered the DisPatch-initiated turn.
    await db.add_message(native_id, "user", "dispatch turn")
    await db.add_message(native_id, "assistant", "dispatch reply")
    # The transcript additionally holds a webchat-side continuation.
    f = _write_session(monkeypatch_root, "main", f"agent:main:{native_id}", "sid-n",
                       [_user("dispatch turn"), _asst("dispatch reply"),
                        _user("webchat side q"), _asst("late reply")])
    state = _fresh_state()
    await main._mirror_cycle(state)
    ent = state["sessions"][f"main|agent:main:{native_id}"]
    assert ent["status"] == "native" and ent["thread_id"] == native_id
    msgs = await db.dump_messages(native_id)
    assert [(m.role, m.content) for m in msgs] == [
        ("user", "dispatch turn"), ("assistant", "dispatch reply"),
        ("user", "webchat side q"), ("assistant", "late reply")]

    # Tail continues to follow the session afterwards.
    _append_lines(f, [_asst("even later")])
    await main._mirror_cycle(state)
    assert (await db.dump_messages(native_id))[-1].content == "even later"


async def test_native_thread_deleted_mutes(monkeypatch_root):
    db = await _fresh_db()
    openclaw.OPENCLAW_AGENTS_DIR = monkeypatch_root
    native_id = str(uuid.uuid4())
    await db.create_thread(bot_id="main", title="native", thread_id=native_id)
    f = _write_session(monkeypatch_root, "main", f"agent:main:{native_id}", "sid-n",
                       [_user("q"), _asst("a")])
    state = _fresh_state()
    await main._mirror_cycle(state)

    # Deleting the native thread must NOT let the mirror resurrect it.
    await db.delete_thread(native_id)
    _append_lines(f, [_asst("ghost")])
    await main._mirror_cycle(state)
    ent = state["sessions"][f"main|agent:main:{native_id}"]
    assert ent["status"] == "muted"
    assert await db.get_thread(native_id) is None


async def test_deleted_mirror_thread_mutes(monkeypatch_root):
    db = await _fresh_db()
    openclaw.OPENCLAW_AGENTS_DIR = monkeypatch_root
    f = _write_session(monkeypatch_root, "main", f"agent:main:{UUID_TAG}", "sid-1",
                       [_user("q1"), _asst("a1")])
    state = _fresh_state()
    await main._mirror_cycle(state)
    assert await db.get_thread(UUID_TAG) is not None

    await db.delete_thread(UUID_TAG)
    _append_lines(f, [_asst("late reply")])
    await main._mirror_cycle(state)
    ent = state["sessions"][f"main|agent:main:{UUID_TAG}"]
    assert ent["status"] == "muted"
    assert await db.get_thread(UUID_TAG) is None
    await main._mirror_cycle(state)                      # stays muted
    assert await db.get_thread(UUID_TAG) is None


async def test_truncation_reparse_no_dupes(monkeypatch_root):
    db = await _fresh_db()
    openclaw.OPENCLAW_AGENTS_DIR = monkeypatch_root
    f = _write_session(monkeypatch_root, "main", f"agent:main:{UUID_TAG}", "sid-1",
                       [_user("q1"), _asst("a1"), _asst("a2")])
    state = _fresh_state()
    await main._mirror_cycle(state)
    assert len(await db.dump_messages(UUID_TAG)) == 3

    # Compaction: the file is rewritten SMALLER (drops a1) with a new line.
    f.write_text("".join(json.dumps(x) + "\n"
                         for x in [_user("q1"), _asst("a3")]))
    await main._mirror_cycle(state)
    msgs = await db.dump_messages(UUID_TAG)
    contents = [m.content for m in msgs]
    assert contents == ["q1", "a1", "a2", "a3"]          # gap-fill only, no dupes


async def test_old_session_tails_from_eof(monkeypatch_root):
    db = await _fresh_db()
    openclaw.OPENCLAW_AGENTS_DIR = monkeypatch_root
    f = _write_session(monkeypatch_root, "main", f"agent:main:{UUID_TAG}", "sid-1",
                       [_user("ancient q"), _asst("ancient a")])
    old = time.time() - 8 * 24 * 3600
    os.utime(f, (old, old))
    state = _fresh_state()
    await main._mirror_cycle(state)
    assert await db.get_thread(UUID_TAG) is None         # history not imported

    _append_lines(f, [_user("new q"), _asst("new a")])   # session wakes up
    await main._mirror_cycle(state)
    msgs = await db.dump_messages(UUID_TAG)
    assert [(m.role, m.content) for m in msgs] == [
        ("user", "new q"), ("assistant", "new a")]


async def test_dispatch_user_echo_and_doc_inline_skipped(monkeypatch_root):
    db = await _fresh_db()
    openclaw.OPENCLAW_AGENTS_DIR = monkeypatch_root
    # The thread already exists as a mirror thread with the user's message posted
    # by the DisPatch send path; the transcript echoes that message back.
    f = _write_session(monkeypatch_root, "main", f"agent:main:{UUID_TAG}", "sid-1",
                       [_user("hello there")])
    state = _fresh_state()
    await main._mirror_cycle(state)
    assert len(await db.dump_messages(UUID_TAG)) == 1

    _append_lines(f, [
        _user("hello there"),                              # echo of the same text
        _user("ctx --- BEGIN DOCUMENT: notes.txt --- body"),  # inlined attachment
        _asst("got it"),
    ])
    await main._mirror_cycle(state)
    msgs = await db.dump_messages(UUID_TAG)
    assert [(m.role, m.content) for m in msgs] == [
        ("user", "hello there"), ("assistant", "got it")]


async def test_main_session_mirrors_to_gw_thread(monkeypatch_root):
    db = await _fresh_db()
    openclaw.OPENCLAW_AGENTS_DIR = monkeypatch_root
    _write_session(monkeypatch_root, "main", "agent:main:main", "sid-m",
                   [_user("cli chat"), _asst("cli reply")])
    state = _fresh_state()
    await main._mirror_cycle(state)
    t = await db.get_thread("gw-main-main")
    assert t is not None and t.title == "Gateway main session"
    msgs = await db.dump_messages("gw-main-main")
    assert [(m.role, m.content) for m in msgs] == [
        ("user", "cli chat"), ("assistant", "cli reply")]


async def test_live_turn_lock_skips_cycle(monkeypatch_root):
    db = await _fresh_db()
    openclaw.OPENCLAW_AGENTS_DIR = monkeypatch_root
    f = _write_session(monkeypatch_root, "main", f"agent:main:{UUID_TAG}", "sid-1",
                       [_user("q1"), _asst("a1")])
    state = _fresh_state()
    await main._mirror_cycle(state)
    _append_lines(f, [_asst("a2")])

    lock = main._thread_locks[UUID_TAG]
    await lock.acquire()                      # a DisPatch turn is in flight
    try:
        await main._mirror_cycle(state)
        assert len(await db.dump_messages(UUID_TAG)) == 2   # skipped this cycle
    finally:
        lock.release()
    await main._mirror_cycle(state)
    assert len(await db.dump_messages(UUID_TAG)) == 3       # caught up after


async def test_runtime_context_wall_never_reaches_the_db(monkeypatch_root):
    """The regression that started this: a transcript 'user' row that is really
    a runtime-injected subagent completion event used to mirror in as a 10KB
    wall of scaffolding the Control UI never displays."""
    db = await _fresh_db()
    openclaw.OPENCLAW_AGENTS_DIR = monkeypatch_root
    from app import openclaw_text as ot
    wall = (f"{ot.INTERNAL_RUNTIME_CONTEXT_BEGIN}\n"
            "OpenClaw runtime context (internal):\n"
            "[Internal task completion event]\nsource: subagent\n"
            + ("noise " * 500) + f"\n{ot.INTERNAL_RUNTIME_CONTEXT_END}")
    _write_session(monkeypatch_root, "main", f"agent:main:{UUID_TAG}", "sid-1",
                   [_user("real question"), _user(wall),
                    _asst("<system-reminder>hidden</system-reminder>real answer")])
    state = _fresh_state()
    await main._mirror_cycle(state)
    msgs = await db.dump_messages(UUID_TAG)
    assert [(m.role, m.content) for m in msgs] == [
        ("user", "real question"), ("assistant", "real answer")]
    joined = " ".join(m.content for m in msgs)
    assert "INTERNAL_CONTEXT" not in joined and "system-reminder" not in joined


async def test_sanitize_migration_cleans_and_guards(tmp_path, monkeypatch):
    db = await _fresh_db()
    monkeypatch.setattr(main.config, "DATA_DIR", tmp_path)
    from app import openclaw_text as ot
    await db.create_thread(bot_id="main", thread_id="t1")
    wall = f"{ot.INTERNAL_RUNTIME_CONTEXT_BEGIN}\njunk\n{ot.INTERNAL_RUNTIME_CONTEXT_END}"
    await db.add_message("t1", "user", wall)                       # scaffolding-only -> deleted
    await db.add_message("t1", "assistant", "keep me <system-reminder>x</system-reminder> done")
    await db.add_message("t1", "assistant", "plain prose, untouched")
    keep_id = (await db.dump_messages("t1"))[2].id

    await main._migrate_sanitize_stored_messages()
    rows = await db.dump_messages("t1")
    contents = [m.content for m in rows]
    assert wall not in " ".join(contents)          # wall row deleted
    assert "system-reminder" not in " ".join(contents)
    assert "keep me" in " ".join(contents) and "done" in " ".join(contents)
    # Untouched row keeps its exact content and id (no whitespace churn).
    assert any(m.id == keep_id and m.content == "plain prose, untouched" for m in rows)
    assert (tmp_path / ".sanitize-migration-done").exists()

    # Second run is a marker-guarded no-op.
    before = [(m.id, m.content) for m in await db.dump_messages("t1")]
    await main._migrate_sanitize_stored_messages()
    after = [(m.id, m.content) for m in await db.dump_messages("t1")]
    assert before == after


async def test_state_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(main.config, "DATA_DIR", tmp_path)
    state = {"version": 1, "sessions": {"k": {"status": "active", "offset": 7}}}
    main._save_mirror_state(state)
    assert main._load_mirror_state() == state
    # Corrupt file falls back to a fresh state, never raises.
    main._mirror_state_path().write_text("{nope")
    assert main._load_mirror_state() == {"version": 1, "sessions": {}}
