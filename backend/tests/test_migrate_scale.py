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
