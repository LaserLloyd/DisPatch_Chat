"""Migration 011_scale: additive, idempotent, an exact dry run, media_refs backfilled,
auto_vacuum made INCREMENTAL (BUILD-PLAN-v2 §1.13.1)."""
from __future__ import annotations

import asyncio
import sqlite3

import pytest

from app import accounts, auth, config, database, migrate


@pytest.fixture
def datadir(tmp_path, monkeypatch):
    d = tmp_path / "scale011"
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

    async def build():
        db = database.Database(d / "chats.db")
        await db.connect()
        await db.create_thread("pal", "one", thread_id="t1", owner_profile_id=None)
        await db.add_message("t1", "assistant", "look [[media:/media/aaa.png|a cat]]",
                             created_at="2026-01-01T00:00:00+00:00")
        await db.add_message("t1", "assistant", "again /media/aaa.png and /media/sub/bbb.webp.",
                             created_at="2026-03-01T00:00:00+00:00")
        await db.add_message("t1", "assistant", "pic", media_url="/media/ccc.jpg",
                             created_at="2026-02-01T00:00:00+00:00")
        await db.add_message("t1", "assistant", "not ours: ~/.openclaw/media/zzz.png",
                             created_at="2026-02-01T00:00:00+00:00")
        await db.add_message("t1", "assistant", "meta", metadata={"image": "/media/ddd.png"},
                             created_at="2026-02-02T00:00:00+00:00")
        await db.close()
    asyncio.run(build())
    # Back to a pre-011 shape: no 011 tables or indexes, auto_vacuum NONE.
    con = sqlite3.connect(d / "chats.db", isolation_level=None)
    for t in ("maintenance_runs", "media_refs", "media_expired", "storage_stats", "maintenance_state"):
        con.execute(f"DROP TABLE IF EXISTS {t}")
    for _t, name, _ddl in database.SCALE_INDEXES:
        con.execute(f"DROP INDEX IF EXISTS {name}")
    con.execute("PRAGMA journal_mode=DELETE")
    con.execute("PRAGMA auto_vacuum=0")
    con.execute("VACUUM")
    con.close()
    return d


def _state(d):
    con = sqlite3.connect(d / "chats.db")
    try:
        return {
            "tables": database.sqlite_tables(con),
            "indexes": {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='index'")},
            "auto_vacuum": con.execute("PRAGMA auto_vacuum").fetchone()[0],
            "applied": {r[0] for r in con.execute("SELECT id FROM schema_migrations")}
            if "schema_migrations" in database.sqlite_tables(con) else set(),
        }
    finally:
        con.close()


def test_media_names_in_finds_only_dispatch_media():
    assert database.media_names_in(
        "[[media:/media/a.png|x]] (/media/b.webp) ~/.openclaw/media/c.png https://h/media/d.png "
        "/media/../etc/passwd /media/e.jpg.", None) == {"a.png", "b.webp", "e.jpg"}


def test_dry_run_lists_011_and_writes_nothing(datadir):
    before = (datadir / "chats.db").read_bytes()
    rep = migrate.run(dry_run=True)
    lines = [a.text for a in rep.actions if a.step == "011_scale"]
    assert any(t.startswith("create tables: maintenance_runs") for t in lines)
    assert any("index(es)" in t for t in lines)
    assert any(t.startswith("backfill media_refs from 5 message") for t in lines)
    assert any(t.startswith("auto_vacuum -> INCREMENTAL") for t in lines)
    assert (datadir / "chats.db").read_bytes() == before
    assert "011_scale" not in _state(datadir)["applied"]


def test_apply_builds_everything_backfills_and_is_idempotent(datadir):
    migrate.run(take_backup=False)
    st = _state(datadir)
    assert "011_scale" in st["applied"]
    assert {"maintenance_runs", "media_refs", "media_expired", "storage_stats",
            "maintenance_state"} <= st["tables"]
    want = {name for table, name, _ in database.SCALE_INDEXES if table in st["tables"]}
    assert want and want <= st["indexes"]
    assert st["auto_vacuum"] == 2                         # INCREMENTAL, made real by VACUUM
    con = sqlite3.connect(datadir / "chats.db")
    refs = dict(con.execute("SELECT name, last_ref_at FROM media_refs"))
    cols = {r[1] for r in con.execute("PRAGMA table_info(profiles)")}
    mark = con.execute("SELECT value FROM maintenance_state WHERE key = ?",
                       (migrate.MEDIA_REFS_WATERMARK,)).fetchone()
    maxrow = con.execute("SELECT MAX(rowid) FROM messages").fetchone()[0]
    con.close()
    assert refs == {"aaa.png": "2026-03-01T00:00:00+00:00",     # the NEWEST reference wins
                    "sub/bbb.webp": "2026-03-01T00:00:00+00:00",
                    "ccc.jpg": "2026-02-01T00:00:00+00:00",
                    "ddd.png": "2026-02-02T00:00:00+00:00"}
    assert "archive_after_months" in cols
    assert mark is not None and int(mark[0]) == maxrow
    # Nothing left: a second plan has no 011 line, a second run changes nothing.
    assert not [a for a in migrate.plan().actions if a.step == "011_scale"]
    migrate.apply_scale()
    assert _state(datadir)["indexes"] == st["indexes"]


def test_archive_after_months_is_validated(datadir):
    migrate.run(take_backup=False)
    p = accounts.create_profile("Ann", "member")
    assert accounts.get_profile(p.id).archive_after_months is None
    assert accounts.update_profile(p.id, archive_after_months=6).archive_after_months == 6
    for bad in (0, 121, "6", True, 2.5):
        with pytest.raises(accounts.AccountError):
            accounts.update_profile(p.id, archive_after_months=bad)
    assert accounts.update_profile(p.id, archive_after_months=None).archive_after_months is None


def _schema(d):
    """Every schema object (normalised DDL) plus the column lists of the tables 011 touches."""
    con = sqlite3.connect(d / "chats.db")
    try:
        objs = sorted((t, n, database.norm_sql(s)) for t, n, s in con.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"))
        cols = {t: [tuple(r[1:6]) for r in con.execute(f"PRAGMA table_info({t})")]
                for t in ("profiles", "messages", "threads", "maintenance_runs", "media_refs",
                          "media_expired", "storage_stats", "maintenance_state")}
        return objs, cols, con.execute("PRAGMA auto_vacuum").fetchone()[0]
    finally:
        con.close()


#: What the first 011 commit (64a3bfb) left on a database that met it before the step ran:
#: the unread index on the owner alone, no stored_name index, and (here) a storage_stats
#: table from a draft without its ``files`` column.
FIRST_COMMIT_OBJECTS = [
    "CREATE INDEX idx_threads_owner_unread ON threads(owner_profile_id) "
    "WHERE is_archived = 0 AND (last_read_at IS NULL OR last_read_at < updated_at)",
    "CREATE TABLE maintenance_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, started TEXT NOT NULL, "
    "finished TEXT, step TEXT NOT NULL, rows INTEGER NOT NULL DEFAULT 0, bytes INTEGER NOT NULL "
    "DEFAULT 0, ms INTEGER NOT NULL DEFAULT 0, error TEXT)",
    "CREATE INDEX idx_maintenance_runs_started ON maintenance_runs(started)",
    "CREATE TABLE storage_stats (area TEXT PRIMARY KEY, bytes INTEGER NOT NULL DEFAULT 0, "
    "measured_at TEXT NOT NULL)",
    "CREATE INDEX idx_threads_owner_list ON threads(owner_profile_id, is_archived, is_pinned, "
    "updated_at DESC, id DESC)",
    "ALTER TABLE profiles ADD COLUMN archive_after_months INTEGER",
]


def test_011_over_a_partial_older_copy_ends_exactly_like_a_fresh_011(datadir, tmp_path):
    import shutil
    pristine = tmp_path / "pristine"
    shutil.copytree(datadir, pristine)
    # A: a fresh 011.
    migrate.run(take_backup=False)
    fresh = _schema(datadir)
    # B: the same data dir, but holding the first commit's objects before 011 runs.
    shutil.rmtree(datadir)
    shutil.copytree(pristine, datadir)
    from app import agents_store, connections, settings_store, tool_manifests
    accounts.reset_for_tests()                   # every module's "schema is there" cache
    settings_store.reset_schema_cache()
    connections.reset_schema_cache()
    tool_manifests.reset_schema_cache()
    agents_store.reset_for_tests()
    with accounts.connect():                     # the accounts tables, as 001 makes them
        pass
    con = sqlite3.connect(datadir / "chats.db", isolation_level=None)
    for ddl in FIRST_COMMIT_OBJECTS:
        try:
            con.execute(ddl)
        except sqlite3.OperationalError as e:    # accounts.connect already added the column
            assert "duplicate column" in str(e), e
    con.close()
    rep = migrate.plan()
    lines = [a.text for a in rep.actions if a.step == "011_scale"]
    assert any("rebuild 1 index(es)" in t and "idx_threads_owner_unread" in t for t in lines), lines
    assert any("storage_stats" in t for t in lines), lines
    migrate.run(take_backup=False)
    got = _schema(datadir)
    assert set(got[0]) ^ set(fresh[0]) == set(), sorted(set(got[0]) ^ set(fresh[0]))
    assert got == fresh
    con = sqlite3.connect(datadir / "chats.db")
    (sql,) = con.execute("SELECT sql FROM sqlite_master WHERE name = 'idx_threads_owner_unread'").fetchone()
    con.close()
    assert "owner_profile_id, is_archived" in sql
