"""A failed startup must END the process (2026-09-16 outage).

A migration raised inside the lifespan, uvicorn logged "Application startup
failed. Exiting." -- and the process lived on for 11.5 hours: aiosqlite's
NON-daemon worker thread was left open by the failed connect, the interpreter
sat in threading._shutdown joining it, and systemd (seeing a live main
process) never restarted the service.

Three layers are pinned here:
  * Database.connect closes its half-open connection when a migration fails;
  * a database still on the pre-2026-09-15 jobs shape connects and migrates
    (the exact shape that raised "no such column: message_id");
  * the lifespan backstop: even if some other non-daemon thread holds the
    interpreter open, a real uvicorn process exits non-zero within seconds.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from app import database
from app.database import Database

BACKEND = Path(__file__).resolve().parents[1]


def _aiosqlite_threads() -> int:
    # aiosqlite's worker threads run _connection_worker_thread.
    return sum(1 for t in threading.enumerate()
               if getattr(t, "_target", None) is not None
               and getattr(t._target, "__name__", "") == "_connection_worker_thread")


def test_failed_migration_closes_the_connection_thread(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "SCHEMA",
                        database.SCHEMA + "\nCREATE INDEX idx_boom ON nosuch(message_id);\n")
    before = _aiosqlite_threads()
    db = Database(tmp_path / "chats.db")
    try:
        with pytest.raises(sqlite3.OperationalError):
            asyncio.run(db.connect())
        assert db._db is None
        # The worker thread exits once close() has been processed.
        deadline = time.monotonic() + 5
        while _aiosqlite_threads() > before and time.monotonic() < deadline:
            time.sleep(0.05)
        assert _aiosqlite_threads() == before, "a failed connect left its worker thread alive"
    finally:
        # If the fix regresses, the leaked worker thread would keep pytest
        # itself from exiting — a hang, not a red test. Close it here so the
        # assertion above is what reports the regression.
        if db._db is not None:
            asyncio.run(db.close())


_PRE_JOBS_MIGRATION = """
CREATE TABLE jobs (
    thread_id TEXT PRIMARY KEY, url TEXT NOT NULL, title TEXT NOT NULL,
    company TEXT NOT NULL DEFAULT '', location TEXT NOT NULL DEFAULT '',
    remote_type TEXT NOT NULL DEFAULT 'unknown',
    seniority TEXT NOT NULL DEFAULT 'unknown',
    salary_min INTEGER, salary_max INTEGER,
    salary_currency TEXT NOT NULL DEFAULT 'USD',
    tags TEXT NOT NULL DEFAULT '[]', source_agent TEXT NOT NULL DEFAULT '',
    source_run_id TEXT, posted_at TEXT, first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL, brief TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'pending', duplicate_of TEXT, expires_at TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE job_events (
    id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, type TEXT NOT NULL,
    from_state TEXT, to_state TEXT, reason_tag TEXT, comment TEXT,
    actor TEXT NOT NULL, payload TEXT, created_at TEXT NOT NULL
);
CREATE TABLE job_feedback (
    id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, signal TEXT NOT NULL,
    reason_tag TEXT, comment TEXT, actor TEXT NOT NULL, payload TEXT,
    created_at TEXT NOT NULL
);
"""


def test_pre_jobs_migration_database_connects(tmp_path):
    path = tmp_path / "chats.db"
    con = sqlite3.connect(path)
    con.executescript(_PRE_JOBS_MIGRATION)
    con.close()

    async def run():
        db = Database(path)
        await db.connect()
        try:
            async def cols(table):
                cur = await db.db.execute(f"PRAGMA table_info({table})")
                return {r["name"] for r in await cur.fetchall()}
            assert {"job_id", "message_id"} <= await cols("jobs")
            assert "job_id" in await cols("job_events")
            assert "job_id" in await cols("job_feedback")
        finally:
            await db.close()
    asyncio.run(run())


_BOOT = textwrap.dedent("""
    import threading, time
    from app import database, main
    import uvicorn

    async def broken(self):
        # Something non-daemon that would hold the interpreter open forever,
        # exactly like the aiosqlite worker did on 2026-09-16.
        threading.Thread(target=time.sleep, args=(3600,), daemon=False).start()
        raise RuntimeError("deliberately broken migration")

    database.Database.connect = broken
    main._FAILED_STARTUP_EXIT_GRACE_S = 1.0
    uvicorn.run(main.app, host="127.0.0.1", port=0, log_level="warning")
""")


def test_failed_startup_process_exits_nonzero(tmp_path):
    env = dict(os.environ)
    env.update({
        "DISPATCH_DATA_DIR": str(tmp_path / "data"),
        "DISPATCH_MIRROR": "0",
        "DISPATCH_GATEWAY_WS": "0",
        "OPENCLAW_BIN": str(tmp_path / "no-openclaw"),
        "PYTHONPATH": str(BACKEND),
    })
    started = time.monotonic()
    proc = subprocess.run([sys.executable, "-c", _BOOT], cwd=BACKEND, env=env,
                          capture_output=True, text=True, timeout=60)
    elapsed = time.monotonic() - started
    assert proc.returncode != 0, proc.stderr[-2000:]
    assert elapsed < 30, f"took {elapsed:.1f}s to exit"
    assert "startup failed" in proc.stderr.lower()
