"""The fail-closed guard: no test opens the real install's database, by any route."""
from __future__ import annotations

import asyncio
import sqlite3

import pytest

from app import accounts, config
from app.database import Database


def _live_targets():
    live = accounts.LIVE_DATA_DIR / "chats.db"
    return [str(live), live, f"file:{live}?mode=ro", accounts.IMPORT_DB_PATH,
            accounts.LIVE_DATA_DIR / "anything-else.db"]


@pytest.mark.parametrize("target", _live_targets(), ids=["str", "path", "ro-uri", "import-path", "other"])
def test_sqlite3_connect_refuses_the_live_data(target, monkeypatch):
    monkeypatch.setattr(accounts, "IMPORT_DB_EXPLICIT", False)   # as with nothing exported
    with pytest.raises(RuntimeError, match="real install"):
        sqlite3.connect(target)


def test_database_accounts_and_migrate_refuse_it(monkeypatch):
    monkeypatch.setattr(accounts, "IMPORT_DB_EXPLICIT", False)
    with pytest.raises(RuntimeError, match="real install"):
        asyncio.run(Database(accounts.IMPORT_DB_PATH).connect())
    with pytest.raises(RuntimeError, match="real install"):
        with accounts.connect(accounts.LIVE_DATA_DIR / "chats.db"):
            pass
    monkeypatch.setattr(config, "DATA_DIR", accounts.LIVE_DATA_DIR)
    from app import migrate
    with pytest.raises(RuntimeError, match="real install"):
        migrate.apply_scale()


def test_scratch_paths_are_allowed(tmp_path):
    con = sqlite3.connect(tmp_path / "x.db")
    con.execute("CREATE TABLE t (a)")
    con.close()
    assert sqlite3.connect(":memory:").execute("SELECT 1").fetchone() == (1,)


def test_an_exported_scratch_data_dir_is_allowed_and_the_live_dir_never(tmp_path, monkeypatch):
    """DISPATCH_DATA_DIR exported to a scratch path (the backstop): that path opens; the real
    data directory is refused whatever the environment says."""
    scratch = tmp_path / "exported" / "chats.db"
    scratch.parent.mkdir()
    monkeypatch.setattr(accounts, "IMPORT_DB_PATH", scratch)
    monkeypatch.setattr(accounts, "IMPORT_DB_EXPLICIT", True)
    sqlite3.connect(scratch).close()
    asyncio.run(_open_close(scratch))
    with pytest.raises(RuntimeError, match="real install"):
        sqlite3.connect(accounts.LIVE_DATA_DIR / "chats.db")
    # Not exported: the import-time default is refused even outside the live folder.
    monkeypatch.setattr(accounts, "IMPORT_DB_EXPLICIT", False)
    with pytest.raises(RuntimeError, match="real install"):
        sqlite3.connect(scratch)


async def _open_close(path):
    db = Database(path)
    await db.connect()
    await db.close()
