"""Every hot query is index-backed: EXPLAIN QUERY PLAN per query (BUILD-PLAN-v2 §1.13.1).

The statements are the ones the real code runs: each reader is called on a database built
through the real DDL after ``migrate.run`` (011_scale included), the SQL it executes is
captured with sqlite's trace hook (parameters bound), and its plan is checked: the intended
index is used and nothing scans ``messages``, ``threads`` or ``activity`` (the tables that
grow with use). The prune and orphan statements the maintenance task (WP-SCALE-OPS) is meant
to issue are checked by their shape, so a missing time index fails here, not at 2M rows.
Plans are taken without ``sqlite_stat1`` (a live install has none until ``PRAGMA optimize``
runs), and again after ``ANALYZE``.
"""
from __future__ import annotations

import asyncio
import re
import sqlite3
from types import SimpleNamespace

import pytest

from app import accounts, auth, config, database, migrate

#: Tables that grow without bound: a SCAN of one of these (by name or by the aliases the
#: queries use) is a failure. Small tables (profiles, connections, settings...) may be scanned.
BIG = {"messages", "threads", "activity", "m", "t"}


@pytest.fixture
def built(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(config, "DATA_DIR", d)
    monkeypatch.setattr(config, "CONFIG_PATH", d / "config.yaml")
    monkeypatch.setattr(config, "BACKUP_DIR", d / "backups")
    monkeypatch.setattr(auth, "SECURITY_PATH", d / "security.yaml")
    monkeypatch.setattr(auth, "RECOVERY_PATH", d / "RECOVERY-CODE.txt")
    monkeypatch.setattr(auth, "TRUSTED_PATH", d / "trusted-devices.yaml")
    config._invalidate_bots_cache()
    accounts.reset_for_tests()
    auth._cache = None
    migrate.run(take_backup=False)
    a = accounts.create_profile("Ann", "user")
    b = accounts.create_profile("Bo", "user")
    db = database.Database(d / "chats.db")
    loop = asyncio.new_event_loop()

    async def seed():
        await db.connect()
        for owner in (a.id, b.id):
            for i in range(30):
                t = await db.create_thread("pal_a" if i % 2 else "pal_b", f"t{i}",
                                           owner_profile_id=owner)
                for k in range(4):
                    await db.add_message(t.id, "user" if k % 2 == 0 else "assistant",
                                         f"hello lantern {i} {k} /media/x{i}.png")
                if i % 7 == 0:
                    await db.pin_thread(t.id, True)
                if i % 9 == 0:
                    await db.archive_thread(t.id)
                if i % 3 == 0:
                    await db.mark_thread_read(t.id)
    loop.run_until_complete(seed())
    yield SimpleNamespace(db=db, loop=loop, a=a, b=b, path=d / "chats.db")
    loop.run_until_complete(db.close())
    loop.close()


def _principal(pid):
    return SimpleNamespace(kind="profile", profile_id=pid, protected=False)


def _captured(env, coro_fn) -> list[str]:
    """The statements ``coro_fn()`` runs (SELECT / DELETE / UPDATE only), parameters bound."""
    seen: list[str] = []
    conn = env.db.db._conn

    async def go():
        await conn.set_trace_callback(seen.append)
        try:
            return await coro_fn()
        finally:
            await conn.set_trace_callback(None)
    env.loop.run_until_complete(go())
    return [s for s in seen if re.match(r"\s*(SELECT|DELETE|UPDATE|WITH)\b", s, re.I)]


def _plan(path, sql: str) -> list[str]:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return [r[3] for r in con.execute("EXPLAIN QUERY PLAN " + sql)]
    finally:
        con.close()


def _scans(plan: list[str]) -> list[str]:
    bad = []
    for line in plan:
        m = re.match(r"SCAN (\w+)", line)
        if m and m.group(1) in BIG and "VIRTUAL TABLE" not in line:
            bad.append(line)
    return bad


def _check(env, sql: str, want: str | None) -> list[str]:
    plan = _plan(env.path, sql)
    assert not _scans(plan), f"full scan in:\n{sql}\n" + "\n".join(plan)
    if want:
        assert any(want in line for line in plan), f"{want} not used by:\n{sql}\n" + "\n".join(plan)
    return plan


def _check_all(env, coro_fn, want: str | None, *, must_match: str | None = None):
    stmts = _captured(env, coro_fn)
    assert stmts, "no statement captured"
    hits = [s for s in stmts if must_match is None or re.search(must_match, s, re.S)]
    assert hits, f"no statement matching {must_match!r} in {stmts}"
    for s in stmts:
        _check(env, s, None)                      # nothing scans a big table
    for s in hits:
        _check(env, s, want)                      # the main statement uses its index


@pytest.mark.parametrize("analyzed", [False, True], ids=["no-stats", "analyzed"])
def test_thread_lists_unread_and_messages_use_their_indexes(built, analyzed):
    env = built
    if analyzed:
        con = sqlite3.connect(env.path)
        con.execute("ANALYZE")
        con.close()
    pa = _principal(env.a.id)
    # The rail, all characters (page one: pinned + unpinned; and a later page).
    _check_all(env, lambda: env.db.list_threads_page(principal=pa, limit=5),
               "idx_threads_owner_list", must_match=r"FROM threads t WHERE")
    _rows, nxt = env.loop.run_until_complete(env.db.list_threads_page(principal=pa, limit=5))
    assert nxt
    _check_all(env, lambda: env.db.list_threads_page(principal=pa, limit=5, cursor=nxt),
               "idx_threads_owner_list", must_match=r"\(t\.updated_at, t\.id\) <")
    # One character's list, the Archived view, 3.x include_archived (both).
    _check_all(env, lambda: env.db.list_threads_page(principal=pa, bot_id="pal_a", limit=5),
               "idx_threads_owner_bot_list", must_match=r"FROM threads t WHERE")
    _check_all(env, lambda: env.db.list_threads_page(principal=pa, archived=True, limit=5),
               "idx_threads_owner_list", must_match=r"FROM threads t WHERE")
    _check_all(env, lambda: env.db.list_threads_page(principal=pa, archived=None, limit=5),
               "idx_threads_owner_list", must_match=r"FROM threads t WHERE")
    # Unread dots: the partial index.
    _check_all(env, lambda: env.db.unread_summary(principal=pa), "idx_threads_owner_unread",
               must_match=r"FROM threads t")
    # Open a thread (last N) and page back (before-cursor); the per-row subqueries.
    rows, _ = env.loop.run_until_complete(env.db.list_threads_page(principal=pa, limit=1))
    tid = rows[0].id
    msgs, _ = env.loop.run_until_complete(env.db.list_messages(tid, limit=2))
    _check_all(env, lambda: env.db.list_messages(tid, limit=2), "idx_messages_thread",
               must_match=r"FROM messages WHERE thread_id")
    _check_all(env, lambda: env.db.list_messages(tid, limit=2, before_id=msgs[0].id),
               "idx_messages_thread", must_match=r"\(created_at, rowid\) <")
    _check_all(env, lambda: env.db.get_thread(tid), None, must_match=r"FROM threads t WHERE t.id")
    # The canonical-id lookup: the primary key (the NOCASE form only on a miss).
    stmts = _captured(env, lambda: env.db.resolve_thread_id(tid))
    assert len(stmts) == 1 and "COLLATE NOCASE" not in stmts[0]
    _check(env, stmts[0], "sqlite_autoindex_threads_1")


def test_owner_scoped_search_ranks_inside_the_index(built):
    env = built
    pa = _principal(env.a.id)
    stmts = _captured(env, lambda: env.db.search_messages("lantern", principal=pa))
    fts = [s for s in stmts if "messages_fts MATCH" in s]
    assert fts and all("owner_profile_id :" in s for s in fts)   # the owner is an FTS filter
    for s in stmts:
        _check(env, s, None)
    join = [s for s in stmts if "CROSS JOIN threads t" in s]
    assert join
    plan = _check(env, join[0], None)
    assert any("SEARCH m USING INTEGER PRIMARY KEY" in line for line in plan), plan
    # And it only ever finds the caller's own messages.
    hits = env.loop.run_until_complete(env.db.search_messages("lantern", principal=pa))
    assert hits
    owners = {env.loop.run_until_complete(env.db.thread_owner(h["thread_id"])) for h in hits}
    assert owners == {env.a.id}


#: The statements the daily maintenance task (WP-SCALE-OPS) prunes and sweeps with, by shape:
#: each one must find its rows through an index (``SEARCH``), never a scan.
MAINTENANCE_STATEMENTS = [
    ("DELETE FROM activity WHERE category = 'activity' AND ts < '2026-01-01'", "idx_activity_category_ts"),
    ("DELETE FROM activity WHERE category = 'audit' AND ts < '2026-01-01'", "idx_activity_category_ts"),
    ("DELETE FROM task_events WHERE ts < 1.0", "idx_task_events_ts"),
    ("SELECT task_id FROM tasks WHERE finished_at < '2026-01-01'", "idx_tasks_finished"),
    ("DELETE FROM job_events WHERE created_at < '2026-01-01'", "idx_job_events_created"),
    ("DELETE FROM job_feedback WHERE created_at < '2026-01-01'", "idx_job_feedback_created"),
    ("DELETE FROM usage_daily WHERE day < '2026-01-01'", "idx_usage_daily_day"),
    ("DELETE FROM transcript_seen WHERE created_at < '2026-01-01'", "idx_transcript_seen_created"),
    ("DELETE FROM image_jobs WHERE state = 'done' AND updated_at < '2026-01-01'", "idx_image_jobs_state_updated"),
    ("DELETE FROM sessions WHERE last_seen < '2026-01-01'", "idx_sessions_last_seen"),
    ("DELETE FROM devices WHERE last_seen < '2026-01-01'", "idx_devices_last_seen"),
    ("DELETE FROM approvals WHERE decided_at < '2026-01-01'", "idx_approvals_decided"),
    ("DELETE FROM agent_spend WHERE day < '2026-01-01'", "sqlite_autoindex_agent_spend_1"),
    ("SELECT * FROM maintenance_runs WHERE started > '2026-01-01' ORDER BY started", "idx_maintenance_runs_started"),
    ("SELECT * FROM maintenance_runs WHERE step = 'x' ORDER BY started DESC LIMIT 1", "idx_maintenance_runs_step"),
    # Orphan / in-use lookups.
    ("SELECT last_ref_at FROM media_refs WHERE name = 'x.png'", "sqlite_autoindex_media_refs_1"),
    ("SELECT 1 FROM media_expired WHERE name = 'x.png'", "sqlite_autoindex_media_expired_1"),
    ("SELECT 1 FROM files WHERE stored_name = 'x.bin'", "idx_files_stored_name"),
    ("SELECT id FROM image_jobs WHERE message_id = 'x'", "idx_image_jobs_message"),
    ("SELECT id FROM threads WHERE owner_profile_id = 'x' AND is_archived = 0 AND updated_at < '2026-01-01'",
     "idx_threads_owner_list"),
    ("SELECT rowid, media_url, content, metadata, created_at FROM messages WHERE rowid > 10 "
     "ORDER BY rowid LIMIT 5000", "INTEGER PRIMARY KEY"),
    ("SELECT token_hash FROM sessions WHERE device_id = 'x'", "idx_sessions_device"),
    ("SELECT id, name FROM files WHERE owner_profile_id = 'x' ORDER BY created_at DESC",
     "idx_files_owner_created"),
]


def test_prune_and_sweep_statements_use_an_index(built):
    for sql, index in MAINTENANCE_STATEMENTS:
        plan = _check(built, sql, index)
        assert any(line.startswith("SEARCH") for line in plan), (sql, plan)


def test_every_011_index_exists_and_is_proven_here():
    """Every index 011 builds is named by a plan assertion in this file (or it does not earn
    its keep and must go)."""
    src = open(__file__, encoding="utf-8").read()
    names = [name for _t, name, _d in database.SCALE_INDEXES]
    unproven = [n for n in names if n not in src]       # every mention here is an assertion
    assert not unproven, unproven
