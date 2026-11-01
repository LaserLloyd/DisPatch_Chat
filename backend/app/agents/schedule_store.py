"""The ``schedules`` table (4.3, #13): recurring agent tasks.

One row per schedule. It lives in the agents' database (``chats.db``, reached through
:func:`app.agents_store.connect`, which this module only imports and never changes) and is
created here with ``CREATE TABLE IF NOT EXISTS``: additive, so a 4.2 binary run against the
same file simply ignores it, and no numbered migration is needed.

Columns
-------
``id``             ``sch_`` + 12 hex
``agent_id``       the agent the schedule dispatches to (``agents.id``)
``owner_profile``  the profile the task runs as - checked again at every fire
``prompt``         what the agent is asked (the transcript of the task it starts is purged
                   by the normal retention; the prompt stays until the schedule is deleted)
``rule``           JSON, exactly one of ``{"every_minutes": n}``, ``{"daily_at": "HH:MM"}``,
                   ``{"weekly_day_at": {"day": "mon".."sun", "at": "HH:MM"}}``
``tz``             an IANA zone name (``Asia/Tokyo``); wall-clock rules are read in it
``thread_id``      optional: the owner's own chat that receives the task card and result
``next_run``       UTC ISO-8601; the loop fires a row whose ``next_run`` is due
``last_run``       UTC ISO-8601 of the last fire that started a task
``enabled``        0/1
``status``         ``ok`` | ``skipped`` | ``disabled`` - what the last fire did
``status_detail``  one sentence for the owner (why it skipped or was switched off)
``last_task_id``   the task the last fire started
"""
from __future__ import annotations

import json
import sqlite3
import threading
from typing import Any

from .. import agents_store as store

SCHEMA = """
CREATE TABLE IF NOT EXISTS schedules (
    id             TEXT PRIMARY KEY,
    agent_id       TEXT NOT NULL,
    owner_profile  TEXT NOT NULL,
    prompt         TEXT NOT NULL,
    rule           TEXT NOT NULL,
    tz             TEXT NOT NULL DEFAULT 'UTC',
    thread_id      TEXT,
    next_run       TEXT,
    last_run       TEXT,
    enabled        INTEGER NOT NULL DEFAULT 1,
    status         TEXT NOT NULL DEFAULT 'ok',
    status_detail  TEXT NOT NULL DEFAULT '',
    last_task_id   TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_schedules_owner ON schedules(owner_profile);
CREATE INDEX IF NOT EXISTS idx_schedules_due ON schedules(enabled, next_run);
"""

_lock = threading.RLock()
_ready: set[str] = set()

#: The columns a caller may set (``create``/``update``); the rest are the loop's.
FIELDS = ("agent_id", "owner_profile", "prompt", "rule", "tz", "thread_id", "next_run", "last_run",
          "enabled", "status", "status_detail", "last_task_id")


def _ensure(con: sqlite3.Connection) -> None:
    key = str(store.accounts.db_path())
    if key not in _ready:
        con.executescript(SCHEMA)
        _ready.add(key)


def reset_for_tests() -> None:
    with _lock:
        _ready.clear()


def _row(r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    try:
        d["rule"] = json.loads(d["rule"] or "{}")
    except ValueError:
        d["rule"] = {}
    d["enabled"] = bool(d["enabled"])
    return d


def _dump(fields: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for k, v in fields.items():
        if k not in FIELDS:
            raise KeyError(f"unknown schedule field {k!r}")
        if k == "rule":
            v = json.dumps(v, sort_keys=True)
        elif k == "enabled":
            v = 1 if v else 0
        out[k] = v
    return out


def create(**fields: Any) -> dict[str, Any]:
    vals = _dump(fields)
    now = store.now_iso()
    sid = store.new_id("sch_")
    vals.update(id=sid, created_at=now, updated_at=now)
    cols = ",".join(vals)
    with _lock, store.connect() as con:
        _ensure(con)
        con.execute(f"INSERT INTO schedules ({cols}) VALUES ({','.join('?' * len(vals))})",
                    tuple(vals.values()))
    got = get(sid)
    assert got is not None
    return got


def get(schedule_id: str) -> dict[str, Any] | None:
    with store.connect() as con:
        _ensure(con)
        r = con.execute("SELECT * FROM schedules WHERE id=?", (schedule_id,)).fetchone()
    return _row(r) if r else None


def update(schedule_id: str, **fields: Any) -> dict[str, Any] | None:
    if fields:
        vals = _dump(fields)
        vals["updated_at"] = store.now_iso()
        sets = ",".join(f"{k}=?" for k in vals)
        with _lock, store.connect() as con:
            _ensure(con)
            con.execute(f"UPDATE schedules SET {sets} WHERE id=?", (*vals.values(), schedule_id))
    return get(schedule_id)


def delete(schedule_id: str) -> bool:
    with _lock, store.connect() as con:
        _ensure(con)
        cur = con.execute("DELETE FROM schedules WHERE id=?", (schedule_id,))
    return cur.rowcount > 0


def list_all(*, owner: str | None = None) -> list[dict[str, Any]]:
    q, args = "SELECT * FROM schedules", ()
    if owner is not None:
        q, args = q + " WHERE owner_profile=?", (owner,)
    with store.connect() as con:
        _ensure(con)
        rows = con.execute(q + " ORDER BY created_at, id", args).fetchall()
    return [_row(r) for r in rows]


def count_for(owner: str) -> int:
    with store.connect() as con:
        _ensure(con)
        return int(con.execute("SELECT COUNT(*) FROM schedules WHERE owner_profile=?",
                               (owner,)).fetchone()[0])


def due(now_iso: str, limit: int = 100) -> list[dict[str, Any]]:
    """Enabled rows whose ``next_run`` is at or before ``now_iso``, oldest first."""
    with store.connect() as con:
        _ensure(con)
        rows = con.execute("SELECT * FROM schedules WHERE enabled=1 AND next_run IS NOT NULL "
                           "AND next_run <= ? ORDER BY next_run, id LIMIT ?",
                           (now_iso, int(limit))).fetchall()
    return [_row(r) for r in rows]


def read_only_enabled(path: str) -> list[dict[str, Any]]:
    """Every enabled schedule, read through a READ-ONLY connection to ``path`` (invariant I10).
    A database without the table reads as no schedules."""
    from pathlib import Path
    con = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    try:
        try:
            rows = con.execute("SELECT * FROM schedules WHERE enabled=1").fetchall()
        except sqlite3.OperationalError:
            return []
        return [_row(r) for r in rows]
    finally:
        con.close()
