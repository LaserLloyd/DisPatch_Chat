"""Regression tests for DisPatch Chat reliability + retrieval (Workstreams A+B).

Runnable two ways:

  cd backend && uv run pytest tests/test_recovery.py       (or just `pytest`)
  cd backend && PYTHONPATH=. .venv/bin/python tests/test_recovery.py   (standalone)

Each test is hermetic: a throwaway DB and a synthetic OpenClaw transcript tree
(OPENCLAW_AGENTS_DIR is monkeypatched), so nothing touches the live data dir or
the real ~/.openclaw sessions.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import tempfile
import traceback
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
    """aiosqlite's connection worker threads are non-daemon — an unclosed DB
    keeps the interpreter (and so `pytest` itself) alive after the run
    finishes. Close whatever this test opened via _fresh_db()."""
    yield
    for db in _open_dbs:
        with contextlib.suppress(Exception):
            await db.close()
    _open_dbs.clear()


async def _fresh_db() -> Database:
    tmp = Path(tempfile.mkdtemp(prefix="dtest-")) / "chats.db"
    db = Database(tmp)
    await db.connect()
    # Track for teardown: aiosqlite's connection worker threads are non-daemon,
    # so an unclosed DB keeps the interpreter alive forever after the runner
    # finishes (the classic "tests pass, process hangs" symptom).
    _open_dbs.append(db)
    main.db = db                      # the funnel helpers use the module global
    main._delivered.clear()
    # Silence broadcasts.
    async def _noop(_frame):
        return None
    main.manager.broadcast = _noop
    return db


def _write_transcript(root: Path, bot: str, session_key: str, session_id: str,
                      texts: list[str]) -> None:
    """Build a minimal but realistic OpenClaw session tree under `root`."""
    sdir = root / bot / "sessions"
    sdir.mkdir(parents=True, exist_ok=True)
    idx_path = sdir / "sessions.json"
    idx = {}
    if idx_path.is_file():
        idx = json.loads(idx_path.read_text())
    idx[session_key] = {"sessionId": session_id}
    idx_path.write_text(json.dumps(idx))
    lines = []
    # a user turn, then assistant turns with text + thinking + a tool call
    lines.append({"type": "message", "message": {"role": "user",
                  "content": [{"type": "text", "text": "do the thing"}]}})
    for t in texts:
        lines.append({"type": "message", "message": {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "pondering " + t},
            {"type": "toolCall", "name": "search", "arguments": {"q": t}},
            {"type": "text", "text": t},
        ]}})
    lines.append({"type": "message", "message": {"role": "toolResult",
                  "content": [{"type": "text", "text": "tool output"}]}})
    (sdir / f"{session_id}.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n")


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #

async def test_fts_search_and_miss():
    db = await _fresh_db()
    th = await db.create_thread(bot_id="main")
    await db.add_message(th.id, "assistant", "the quick brown fox jumps")
    await db.add_message(th.id, "user", "tell me about pineapples please")
    hits = await db.search_messages("pineapples")
    assert len(hits) == 1, hits
    assert "pineapples" in (hits[0]["snippet"] or "").lower()
    assert await db.search_messages("zzznotpresent") == []
    # scoped by bot
    assert await db.search_messages("fox", bot_ids=["main"])
    assert await db.search_messages("fox", bot_ids=["other"]) == []


async def test_export_dump_and_count():
    db = await _fresh_db()
    th = await db.create_thread(bot_id="main", title="T")
    for i in range(5):
        await db.add_message(th.id, "assistant", f"msg {i}")
    assert await db.count_messages() == 5
    dump = await db.dump_messages(th.id)
    assert [m.content for m in dump] == [f"msg {i}" for i in range(5)]   # oldest→newest
    allt = await db.all_threads()
    assert len(allt) == 1 and allt[0].message_count == 5


async def test_reset_inflight_and_durability():
    db = await _fresh_db()
    th = await db.create_thread(bot_id="main")
    await db.update_thread_status(th.id, "thinking")
    stranded = await db.reset_inflight_threads()
    assert stranded and stranded[0]["id"] == th.id
    assert (await db.get_thread(th.id)).status == "idle"
    assert await db.reset_inflight_threads() == []          # nothing left
    assert await db.integrity_ok() is True
    dest = db.path.parent / "snap.db"
    await db.backup_to(dest)
    assert dest.exists() and dest.stat().st_size > 0
    await db.checkpoint("TRUNCATE")                          # must not raise


async def test_transcript_recover_is_idempotent(monkeypatch_root: Path):
    db = await _fresh_db()
    openclaw.OPENCLAW_AGENTS_DIR = monkeypatch_root
    th = await db.create_thread(bot_id="main")
    key = openclaw.session_key_for("main", th.id)
    _write_transcript(monkeypatch_root, "main", key, "sid-1",
                      ["alpha reply", "beta reply", "gamma reply"])
    # First import pulls all three text blocks (thinking/tool/toolResult ignored).
    n1 = await main._import_transcript_messages(th.id, "main")
    assert n1 == 3, n1
    # Second import is a no-op (dedup funnel).
    n2 = await main._import_transcript_messages(th.id, "main")
    assert n2 == 0, n2
    msgs = await db.dump_messages(th.id)
    assert [m.content for m in msgs] == ["alpha reply", "beta reply", "gamma reply"]


async def test_recover_multiturn_idempotent(monkeypatch_root: Path):
    """REC-1 regression: re-importing a transcript into a normal multi-turn
    thread (U-A-U-A-U-A) must add NOTHING — earlier-turn replies are not in the
    trailing assistant run, so a trailing-only dedup would wrongly re-append them.
    """
    db = await _fresh_db()
    openclaw.OPENCLAW_AGENTS_DIR = monkeypatch_root
    th = await db.create_thread(bot_id="main")
    key = openclaw.session_key_for("main", th.id)
    replies = ["alpha reply", "beta reply", "gamma reply"]
    _write_transcript(monkeypatch_root, "main", key, "sid-mt", replies)
    # Simulate the thread already delivered live, interleaved with user turns.
    for a in replies:
        await db.add_message(th.id, "user", "ask")
        await db.add_message(th.id, "assistant", a)
    before = await db.count_messages()
    n = await main._import_transcript_messages(th.id, "main")
    assert n == 0, f"re-imported {n} message(s) — should be 0 (duplicates!)"
    assert await db.count_messages() == before, "thread size changed on re-import"


async def test_list_sessions_and_full_transcript(monkeypatch_root: Path):
    await _fresh_db()
    openclaw.OPENCLAW_AGENTS_DIR = monkeypatch_root
    # a DisPatch thread session + a cron session DisPatch never created
    _write_transcript(monkeypatch_root, "main", "agent:main:thread-xyz", "sid-t", ["hi"])
    _write_transcript(monkeypatch_root, "main", "agent:main:cron:abc", "sid-c", ["cron ran"])
    sess = openclaw.list_agent_sessions("main")
    kinds = {s["kind"] for s in sess}
    assert "thread" in kinds and "cron" in kinds, kinds
    f = openclaw.session_file_by_id("main", "sid-c")
    assert f is not None
    full = openclaw.read_transcript_items(f, include_all=True)
    kset = {i["kind"] for i in full}
    assert {"user", "thinking", "tool", "tool_result", "text"} <= kset, kset
    deliverable = openclaw.read_transcript_items(f, include_all=False)
    assert all(i["kind"] in ("text", "note") for i in deliverable)


async def test_session_kind_classifier():
    assert openclaw._session_kind("agent:main:main") == "main"
    assert openclaw._session_kind("agent:main:cron:x") == "cron"
    assert openclaw._session_kind("agent:main:subagent:x") == "subagent"
    assert openclaw._session_kind("agent:main:dashboard:x") == "dashboard"
    assert openclaw._session_kind("agent:main:daily-main-2026-06-19") == "daily"
    assert openclaw._session_kind("agent:main:abc-123") == "thread"


async def test_salvage_and_no_reply_helpers():
    # NO_REPLY stripped only as a standalone line
    assert main._strip_no_reply("hi\nNO_REPLY\n") == "hi"
    assert "NO_REPLY" in main._strip_no_reply("the value is NO_REPLY_TOKEN")  # not standalone
    # dead-text MEDIA:/path with a media ext gets salvaged to a directive
    out = main._salvage_media_refs("see MEDIA:/tmp/x.png now")
    assert "[[media:/tmp/x.png]]" in out, out
    # prose path without media ext is left alone
    assert main._salvage_media_refs("social media: /r/pics") == "social media: /r/pics"


# --------------------------------------------------------------------------- #
# Runner (no pytest needed)
# --------------------------------------------------------------------------- #

async def _run_all() -> int:
    failures = 0
    tests = [
        ("fts_search_and_miss", test_fts_search_and_miss, False),
        ("export_dump_and_count", test_export_dump_and_count, False),
        ("reset_inflight_and_durability", test_reset_inflight_and_durability, False),
        ("transcript_recover_is_idempotent", test_transcript_recover_is_idempotent, True),
        ("recover_multiturn_idempotent", test_recover_multiturn_idempotent, True),
        ("list_sessions_and_full_transcript", test_list_sessions_and_full_transcript, True),
        ("session_kind_classifier", test_session_kind_classifier, False),
        ("salvage_and_no_reply_helpers", test_salvage_and_no_reply_helpers, False),
    ]
    orig_root = openclaw.OPENCLAW_AGENTS_DIR
    for name, fn, needs_root in tests:
        try:
            if needs_root:
                root = Path(tempfile.mkdtemp(prefix="dtest-oc-")) / "agents"
                root.mkdir(parents=True)
                await fn(root)
            else:
                await fn()
            print(f"  PASS  {name}")
        except Exception:
            failures += 1
            print(f"  FAIL  {name}")
            traceback.print_exc()
        finally:
            openclaw.OPENCLAW_AGENTS_DIR = orig_root
    for db in _open_dbs:
        try:
            await db.close()
        except Exception:
            pass
    _open_dbs.clear()
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return failures


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run_all()))


# --------------------------------------------------------------------------- #
# Stale per-turn state and phantom file records (BOX-05)
# --------------------------------------------------------------------------- #


async def test_stale_error_status_is_cleared_on_boot():
    """A failed turn cannot resume across a restart, so 'error' at boot is
    stale — seven threads on the live box wore one for up to three weeks."""
    db = await _fresh_db()
    bad = await db.create_thread(bot_id="main")
    good = await db.create_thread(bot_id="main")
    await db.update_thread_status(bad.id, "error")

    cleared = await db.clear_stale_error_threads()
    assert [t["id"] for t in cleared] == [bad.id]
    assert (await db.get_thread(bad.id)).status == "idle"
    assert (await db.get_thread(good.id)).status == "idle"     # untouched
    assert await db.clear_stale_error_threads() == []          # idempotent


async def test_error_and_thinking_are_cleared_independently():
    """The two resets must not swallow each other's rows: 'thinking' rows are
    returned for transcript reconciliation, 'error' rows are not."""
    db = await _fresh_db()
    t_think = await db.create_thread(bot_id="main")
    t_err = await db.create_thread(bot_id="main")
    await db.update_thread_status(t_think.id, "thinking")
    await db.update_thread_status(t_err.id, "error")

    assert [t["id"] for t in await db.reset_inflight_threads()] == [t_think.id]
    assert (await db.get_thread(t_err.id)).status == "error"   # still pending
    assert [t["id"] for t in await db.clear_stale_error_threads()] == [t_err.id]


async def test_only_file_rows_whose_blob_is_absent_are_purged(tmp_path):
    """Delete the phantoms, keep every record that still has its blob."""
    db = await _fresh_db()
    files_dir = tmp_path / "files"
    files_dir.mkdir()
    (files_dir / "alive.bin").write_bytes(b"still here")
    kept = await db.add_file("Alive.pdf", "alive.bin", 10, "application/pdf")
    ghost = await db.add_file("Ghost.pdf", "ghost.bin", 999, "application/pdf")

    purged = await db.delete_files_missing_from(files_dir)
    assert [r["id"] for r in purged] == [ghost["id"]]
    assert [r["id"] for r in await db.list_files()] == [kept["id"]]
    # The phantom's size was inflating the server-wide storage cap.
    assert await db.total_file_bytes() == 10
    assert await db.delete_files_missing_from(files_dir) == []   # idempotent
