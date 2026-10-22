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
def test_sqlite3_connect_refuses_the_live_data(target):
    with pytest.raises(RuntimeError, match="real install"):
        sqlite3.connect(target)


def test_database_accounts_and_migrate_refuse_it(monkeypatch):
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
