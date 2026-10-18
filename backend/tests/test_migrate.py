"""The 2.x -> 3.0 data migration: accurate dry run, backup first, no data loss,
idempotent, restorable (gate G8)."""
from __future__ import annotations

import sqlite3

import pytest
import yaml

from app import accounts, auth, config, migrate


@pytest.fixture
def legacy(tmp_path, monkeypatch):
    """A 2.x-shaped data directory: a PIN, a roster with the two visibility flags, a
    populated chats.db with none of the 3.0 tables, remembered devices."""
    d = tmp_path / "legacy"
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
    (d / "config.yaml").write_text(yaml.safe_dump({"bots": [
        {"id": "kid", "name": "Kit", "avatar": "kit.svg", "emoji": "x", "order": 0,
         "safe": True, "hide_unlocked": True},
        {"id": "helper", "name": "Helper", "avatar": "h.svg", "emoji": "x", "order": 1,
         "safe": True, "hide_unlocked": False},
        {"id": "work", "name": "Work", "avatar": "w.svg", "emoji": "x", "order": 2,
         "safe": False, "hide_unlocked": False,
         "api": {"provider": "openai", "api_key": "sk-test-not-real", "model": "m"}},
    ]}, sort_keys=False))
    # A real 2.x chats.db: the app's own schema (which has no 3.0 tables) and two threads.
    import asyncio

    from app.database import Database

    async def _build():
        db = Database(d / "chats.db")
        await db.connect()
        await db.create_thread("kid", "hello", thread_id="t1")
        await db.create_thread("helper", "world", thread_id="t2")
        await db.close()
    asyncio.run(_build())
    # A 2.x security.yaml (no 3.0 keys) with a real PIN hash.
    auth.set_pin("1234")
    accounts.delete_owner_row()
    accounts.reset_for_tests()
    raw = yaml.safe_load((d / "security.yaml").read_text())
    for k in ("machine_loopback_trust", "machine_scopes", "session_timeout_member",
              "session_timeout_restricted"):
        raw.pop(k, None)
    (d / "security.yaml").write_text(yaml.safe_dump(raw))
    con = sqlite3.connect(d / "chats.db")           # set_pin touched the accounts tables; undo
    for t in accounts.TABLES:
        con.execute(f"DROP TABLE IF EXISTS {t}")
    con.commit()
    con.execute("PRAGMA journal_mode=DELETE")
    con.close()
    accounts.reset_for_tests()
    auth._cache = None
    return d


def text_of(rep):
    return chr(10).join(rep.lines())


def _snapshot(d):
    return {str(p.relative_to(d)): (p.stat().st_size, p.read_bytes() if p.is_file() else None)
            for p in sorted(d.rglob("*"))}


def test_the_dry_run_describes_every_step_and_changes_nothing(legacy):
    before = _snapshot(legacy)
    rep = migrate.run(dry_run=True)
    assert _snapshot(legacy) == before, "a dry run wrote something"
    steps = {a.step for a in rep.actions}
    assert steps >= {"001_accounts", "002_owner_from_pin", "003_audience", "004_security_keys",
                     "005_settings", "006_connections"}
    assert "connection" in text_of(rep) and "'work'" in text_of(rep)
    text = "\n".join(rep.lines())
    assert "character 'kid'" in text and "audience=restricted" in text
    assert "character 'helper'" in text and "audience=all" in text
    assert "character 'work'" in text and "audience=members" in text
    assert "Owner profile" in text and "password" in text
    assert rep.backup is None and rep.applied == []


def test_applying_does_what_the_dry_run_said(legacy):
    planned = migrate.plan()
    rep = migrate.run()
    assert set(rep.applied) == {a.step for a in planned.actions}
    # Accounts: the owner exists, built from the existing PIN, no password yet.
    owner = accounts.get_owner()
    assert owner is not None and owner.has_pin and not owner.has_password
    assert auth.verify_pin("1234") and not auth.verify_pin("4321")
    # Audience is written, computed from the flags; the legacy flags are still there.
    roster = {e["id"]: e for e in yaml.safe_load((legacy / "config.yaml").read_text())["bots"]}
    assert {k: v["audience"] for k, v in roster.items()} == {
        "kid": "restricted", "helper": "all", "work": "members"}
    assert roster["kid"]["safe"] is True and roster["kid"]["hide_unlocked"] is True
    # Nothing that was in config.yaml was lost on the way through: the provider block stays
    # readable, and the API key moved to the secret store behind a connection that resolves to it.
    from app import connections, llm_api
    api = roster["work"]["api"]
    assert api["provider"] == "openai" and api["model"] == "m" and "api_key" not in api
    assert api["connection"] and connections.get(api["connection"]) is not None
    assert llm_api.resolve_key(api) == "sk-test-not-real"
    # security.yaml now spells out the new keys, at their compatible defaults.
    sec = yaml.safe_load((legacy / "security.yaml").read_text())
    assert sec["machine_loopback_trust"] is True and sec["machine_scopes"] is None
    # The conversation data is untouched.
    con = sqlite3.connect(legacy / "chats.db")
    assert con.execute("SELECT id, title FROM threads ORDER BY id").fetchall() == [("t1", "hello"), ("t2", "world")]
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    ids = {r[0] for r in con.execute("SELECT id FROM schema_migrations")}
    con.close()
    assert ids >= {"001_accounts", "002_owner_from_pin", "003_audience", "004_security_keys",
                   "005_settings", "006_connections", "007_plugin_approvals"}


def test_a_backup_is_taken_first_and_it_is_complete(legacy):
    rep = migrate.run()
    b = rep.backup
    assert b is not None and b.parent == legacy / "backups" and b.name.startswith("3.0.0-")
    assert {p.name for p in b.iterdir()} >= {"chats.db", "config.yaml", "security.yaml"}
    # The backup is the ORIGINAL: a 2.x database (no accounts tables) with its rows.
    con = sqlite3.connect(b / "chats.db")
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "profiles" not in tables and "threads" in tables
    assert con.execute("SELECT count(*) FROM threads").fetchone()[0] == 2
    con.close()
    assert "audience" not in (b / "config.yaml").read_text()


def test_running_it_twice_is_a_no_op(legacy):
    migrate.run()
    again = migrate.plan()
    assert again.actions == []
    rep = migrate.run()
    assert rep.applied == [] and rep.backup is None
    assert len(list((legacy / "backups").iterdir())) == 1
    assert accounts.get_owner() is not None


def test_rollback_restores_the_original_files(legacy):
    original = {n: (legacy / n).read_bytes() for n in ("config.yaml", "security.yaml")}
    rep = migrate.run()
    assert (legacy / "config.yaml").read_bytes() != original["config.yaml"]
    names = migrate.restore(rep.backup)
    assert set(names) >= {"chats.db", "config.yaml", "security.yaml"}
    assert (legacy / "config.yaml").read_bytes() == original["config.yaml"]
    assert (legacy / "security.yaml").read_bytes() == original["security.yaml"]
    con = sqlite3.connect(legacy / "chats.db")
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    con.close()
    assert "profiles" not in tables
    # ... and migrating again from the restored state works (the upgrade is repeatable).
    assert set(migrate.run().applied) >= {"001_accounts", "002_owner_from_pin", "003_audience",
                                          "004_security_keys", "005_settings", "006_connections"}


def test_only_the_newest_five_backups_are_kept(legacy):
    root = legacy / "backups"
    for i in range(7):
        (root / f"3.0.0-2026010{i + 1}-000000").mkdir(parents=True)
    migrate.run()
    kept = sorted(p.name for p in root.iterdir())
    assert len(kept) == migrate.KEEP_BACKUPS
    assert kept[0] > "3.0.0-20260102-000000"        # the oldest were dropped


def test_a_fresh_install_has_nothing_to_back_up(tmp_path, monkeypatch):
    d = tmp_path / "fresh"
    d.mkdir()
    monkeypatch.setattr(config, "DATA_DIR", d)
    monkeypatch.setattr(config, "CONFIG_PATH", d / "config.yaml")
    monkeypatch.setattr(config, "BACKUP_DIR", d / "backups")
    monkeypatch.setattr(auth, "SECURITY_PATH", d / "security.yaml")
    accounts.reset_for_tests()
    rep = migrate.run()
    assert rep.backup is None
    assert accounts.get_owner() is None
    assert migrate.plan().actions == []


def test_a_pin_directive_in_security_yaml_is_not_applied_by_a_dry_run(legacy):
    """auth.load() applies a plaintext `pin:` (and writes); the dry run must not."""
    raw = yaml.safe_load((legacy / "security.yaml").read_text())
    raw["pin"] = "9876"
    (legacy / "security.yaml").write_text(yaml.safe_dump(raw))
    before = (legacy / "security.yaml").read_bytes()
    migrate.run(dry_run=True)
    assert (legacy / "security.yaml").read_bytes() == before


def test_the_cli_prints_the_plan(legacy, capsys):
    assert migrate.main(["--dry-run", "--data-dir", str(legacy)]) == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "[003_audience]" in out
    assert migrate.main(["--data-dir", str(legacy)]) == 0
    out = capsys.readouterr().out
    assert "backup:" in out
    assert migrate.main(["--dry-run", "--data-dir", str(legacy)]) == 0
    assert "already on 3.0" in capsys.readouterr().out


def test_starting_the_server_migrates_and_the_owner_unlocks_with_the_old_pin(legacy, monkeypatch):
    """The user-visible promise: upgrade, restart, type the same PIN."""
    import asyncio

    from fastapi.testclient import TestClient

    from app import main
    from app.database import Database

    monkeypatch.setattr(main, "MEDIA_DIR", legacy / "media")
    monkeypatch.setattr(main, "FILES_DIR", legacy / "files")
    monkeypatch.setattr(config, "MEDIA_DIR", legacy / "media")
    monkeypatch.setattr(config, "FILES_DIR", legacy / "files")
    monkeypatch.setattr(config, "LOG_DIR", legacy / "logs")
    temp_db = Database(legacy / "chats.db")
    monkeypatch.setattr(main, "db", temp_db)
    with TestClient(main.app, client=("203.0.113.50", 40000)) as c:
        assert accounts.get_owner() is not None
        assert list((legacy / "backups").iterdir())
        st = c.get("/api/auth/status").json()
        assert st["claimed"] and st["decoy"] and not st["authenticated"]
        assert c.post("/api/auth/unlock", json={"pin": "1234"}).status_code == 200
        me = c.get("/api/auth/me").json()
        assert me["role"] == "owner" and me["needs_password"] is True
        # Characters kept their audience across the upgrade.
        bots = {b["id"]: b["audience"] for b in c.get("/api/bots/all").json()["bots"]}
        assert {k: bots[k] for k in ("kid", "helper", "work")} == {
            "kid": "restricted", "helper": "all", "work": "members"}
    asyncio.run(temp_db.close())


def test_rollback_puts_the_files_back_owner_only(legacy):
    """L-2: ``--restore DIR`` copies the backup's files over the live ones; they must end up 0600 / an owner-only
    ACL, not whatever the backup copy or the folder happened to carry."""
    import os

    from app import fsperm
    if os.name == "nt":
        import subprocess
        subprocess.run(["icacls", str(legacy), "/grant", "*S-1-1-0:(OI)(CI)R"], capture_output=True, check=False)
    rep = migrate.run()
    for name in ("config.yaml", "security.yaml", "chats.db"):
        os.chmod(rep.backup / name, 0o644)
    old = os.umask(0o022) if os.name != "nt" else None
    try:
        names = migrate.restore(rep.backup)
    finally:
        if old is not None:
            os.umask(old)
    assert set(names) >= {"chats.db", "config.yaml", "security.yaml"}
    for name in ("config.yaml", "security.yaml", "chats.db"):
        p = legacy / name
        if os.name != "nt":
            assert (p.stat().st_mode & 0o777) == 0o600, name
        else:
            assert fsperm.others_can_read(p) is False, name
