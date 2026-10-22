"""The shared aiosqlite connection must never be left inside a read transaction.

Since 3.0, accounts / settings / connections / agents write chats.db through their
own sqlite3 connections. A single-row ``fetchone()`` that leaves its statement
unfinished keeps a read snapshot open on the shared connection; the next write
there has to upgrade a now-stale snapshot and SQLite answers "database is locked"
at once (busy_timeout does not apply). Ops check 2026-10-19: four Restricted
sockets sending while a background writer ran -> `ws handler 'send' failed`.
"""
from __future__ import annotations

import sqlite3

import pytest

from app.database import Database


def _commit_elsewhere(path) -> None:
    con = sqlite3.connect(str(path), timeout=5, isolation_level=None)
    try:
        con.execute("CREATE TABLE IF NOT EXISTS _other (x)")
        con.execute("INSERT INTO _other VALUES (1)")
    finally:
        con.close()


@pytest.mark.asyncio
async def test_a_single_row_read_then_a_foreign_commit_does_not_lock_the_next_write(tmp_path):
    db = Database(tmp_path / "chats.db")
    await db.connect()
    try:
        thread = await db.create_thread("main", "t", owner_profile_id=None)
        first = await db.add_message(thread.id, "user", "one")
        # Every single-row getter below used to leave its statement active.
        assert await db.get_message(first.id) is not None
        assert await db.get_thread(thread.id) is not None
        assert await db.count_messages() >= 1
        _commit_elsewhere(tmp_path / "chats.db")
        await db.add_message(thread.id, "user", "two")      # raised "database is locked"
        await db.set_title_if_empty(thread.id, "x")
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_a_read_still_being_consumed_cannot_lock_another_coroutines_write(tmp_path):
    """The interleaving itself: coroutine A has run its SELECT but not yet fetched
    (it is between two awaits), coroutine B writes on the same shared connection,
    and in between some other connection committed."""
    db = Database(tmp_path / "chats.db")
    await db.connect()
    try:
        thread = await db.create_thread("main", "t", owner_profile_id=None)
        for i in range(3):
            await db.add_message(thread.id, "user", f"m{i}")
        cur = await db.db.execute("SELECT id FROM messages WHERE thread_id = ?", (thread.id,))
        _commit_elsewhere(tmp_path / "chats.db")
        await db.add_message(thread.id, "user", "while A still reads")   # "database is locked"
        rows = await cur.fetchall()
        assert len(rows) == 3
        # A write still answers rowcount through the real cursor.
        w = await db.db.execute("UPDATE threads SET title = 'y' WHERE id = ?", (thread.id,))
        assert w.rowcount == 1
    finally:
        await db.close()
