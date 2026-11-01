"""Helpers for the maintenance suites (WP-SCALE-OPS): a scratch data folder with a real chats.db,
fixed clocks, and small row builders. Fixture names are neutral (profiles Ada/Bram, bots
pack_otter/pack_heron).
"""
from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from profiles_fixtures import scratch

from app import maintenance

NOW = datetime.now(UTC).replace(microsecond=0)      # files carry real mtimes, so the clock is real too


def iso(days_ago: float, *, now: datetime = NOW) -> str:
    return (now - timedelta(days=days_ago)).isoformat(timespec="seconds")


def day(days_ago: float, *, now: datetime = NOW) -> str:
    return (now.astimezone() - timedelta(days=days_ago)).strftime("%Y-%m-%d")


def epoch(days_ago: float, *, now: datetime = NOW) -> float:
    return (now - timedelta(days=days_ago)).timestamp()


def db(s) -> sqlite3.Connection:
    """A plain autocommit connection to the scratch chats.db."""
    from app import accounts
    with accounts.connect():
        pass
    con = sqlite3.connect(s.root / "chats.db", timeout=10, isolation_level=None)
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=10000")
    return con


def run(steps: list[str], **kw) -> dict[str, dict]:
    """Run only ``steps`` at the fixed clock; return ``{step: result row}``."""
    kw.setdefault("sleep", 0)
    res = maintenance.run_nightly(now=NOW, steps=steps, **kw)
    assert res["interrupted"] is None
    return {r["step"]: r for r in res["steps"]}


def age(path: Path, days_ago: float) -> Path:
    t = epoch(days_ago)
    os.utime(path, (t, t))
    return path


def put(path: Path, data: bytes = b"\x89PNG-fake") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def set_message_time(con: sqlite3.Connection, thread_id: str, days_ago: float) -> None:
    con.execute("UPDATE messages SET created_at = ? WHERE thread_id = ?", (iso(days_ago), thread_id))


def set_thread_time(con: sqlite3.Connection, thread_id: str, days_ago: float) -> None:
    con.execute("UPDATE threads SET updated_at = ?, created_at = ? WHERE id = ?",
                (iso(days_ago), iso(days_ago), thread_id))


def ensure_archive_column(con: sqlite3.Connection) -> None:
    cols = {r[1] for r in con.execute("PRAGMA table_info(profiles)")}
    if "archive_after_months" not in cols:
        con.execute("ALTER TABLE profiles ADD COLUMN archive_after_months INTEGER")


def set_setting(**values) -> None:
    """Set retention/maintenance settings by their short names: ``images_mode="months"``."""
    from app import settings_store
    names = {"images_mode": "system.retention.images.mode", "images_months": "system.retention.images.months",
             "images_trash_days": "system.retention.images.trash_days"}
    changes = {names.get(k, k): v for k, v in values.items()}
    res = settings_store.set_many(changes)
    assert res.ok, res.errors
