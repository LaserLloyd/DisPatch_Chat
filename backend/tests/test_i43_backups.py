"""4.3 WP-A4: backups and migrations (issues #35, #36, #17 N-14 restore + N-16 reveal, #18 bundle).

(a)  an agent secret survives archive -> restore into an empty data directory
(a2) with two separate stores (keyring mode) an agent secret goes back to the agent store
(b)  a ``pre-restore-<stamp>`` copy is its own kind, never a pre-migration copy
(c)  ``migrate.backup`` + ``restore`` round-trip ``agents/secrets.json`` (with and without it)
(d)  the recovery-key reveal: protected Admin + right credential only, throttled, audited, no-store
(e)  the support bundle's ``log-tail.txt`` is filled from the in-memory log ring when no file exists
(f)  the archive's secrets document holds every name in the secret store(s), each once
(g)  restoring an archive without secrets never creates an empty ``agents/secrets.json``
"""
from __future__ import annotations

import io
import json
import shutil
import sqlite3
import sys
import types
import zipfile
from pathlib import Path

import pytest
import yaml

from app import accounts, auth, backups, config, connections, migrate, secret_store
from app.agents.secrets import FileSecretProvider, MemorySecretProvider

PASS = "correct horse battery staple"
AGENT_SECRET = "mcp-planted-agent-value-0001"      # scrub-ok: test fixture, not a real credential
OIDC_SECRET = "oidc-planted-client-secret-0002"    # scrub-ok: test fixture, not a real credential


def _make_data(d: Path, monkeypatch) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config, "DATA_DIR", d)
    monkeypatch.setattr(config, "CONFIG_PATH", d / "config.yaml")
    monkeypatch.setattr(config, "BACKUP_DIR", d / "backups")
    monkeypatch.setattr(config, "LOG_DIR", d / "logs")
    monkeypatch.setattr(auth, "SECURITY_PATH", d / "security.yaml")
    monkeypatch.setattr(auth, "RECOVERY_PATH", d / "RECOVERY-CODE.txt")
    monkeypatch.setattr(auth, "TRUSTED_PATH", d / "trusted-devices.yaml")
    config._invalidate_bots_cache()
    accounts.reset_for_tests()
    secret_store.reset_for_tests()
    auth._cache = None
    return d


@pytest.fixture
def data(tmp_path, monkeypatch):
    d = _make_data(tmp_path / "home", monkeypatch)
    con = sqlite3.connect(d / "chats.db")
    con.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, title TEXT)")
    con.execute("INSERT INTO threads VALUES ('t1', 'hello')")
    con.commit()
    con.close()
    (d / "config.yaml").write_text(yaml.safe_dump({"bots": [{"id": "a", "name": "Alpha"}]}), encoding="utf-8")
    return d


def _file_store(d: Path) -> FileSecretProvider:
    return FileSecretProvider(d / "agents" / "secrets.json")


def _restore_into(fresh: Path, archive: Path, monkeypatch) -> dict:
    """Copy ``archive`` into a brand-new data directory and restore it there."""
    _make_data(fresh, monkeypatch)
    dest = backups.archive_dir()
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copy2(archive, dest / archive.name)
    staged = backups.stage_restore(f"archive:{archive.name}", PASS)
    assert staged["staged"]
    out = backups.apply_pending_restore()
    assert out and out["applied"] is True
    return out


# --------------------------------------------------------------------------- #
# (a) (a2) (f) (g): every secret travels, once, to the right store
# --------------------------------------------------------------------------- #


def test_a_an_agent_secret_survives_archive_and_restore_into_an_empty_data_dir(data, tmp_path, monkeypatch):
    _file_store(data).set("MCP_SEARCH_TOKEN", AGENT_SECRET)
    row = backups.create_archive(PASS)
    archive = backups.archive_dir() / row["name"]
    fresh = tmp_path / "fresh"
    out = _restore_into(fresh, archive, monkeypatch)
    assert _file_store(fresh).get("MCP_SEARCH_TOKEN") == AGENT_SECRET
    assert out["re_enter"] == []


def test_a2_with_separate_stores_an_agent_secret_goes_back_to_the_agent_store(data, tmp_path, monkeypatch):
    """Keyring mode: the agents keep their keys under another keyring service than the
    connection keys. A restore must put each name back where its reader looks."""
    ss_mem, ag_mem = MemorySecretProvider(), MemorySecretProvider()
    monkeypatch.setattr(secret_store, "provider", lambda: ss_mem)
    monkeypatch.setattr(backups, "_agent_store", lambda: ag_mem)
    ag_mem.set("STUDIOFORGE_KEY", AGENT_SECRET)
    ag_mem.set("ANTHROPIC_API_KEY", "sk-ant-planted-0003")        # scrub-ok: test fixture, not a real credential
    secret_store.put("connection.openai", "sk-planted-conn-0004")   # scrub-ok: test fixture, not a real credential
    connections_refs = {"connection.openai"}
    monkeypatch.setattr(backups, "_known_store_names", lambda: set(connections_refs))
    monkeypatch.setattr(backups, "_known_agent_names", lambda: {"STUDIOFORGE_KEY", "ANTHROPIC_API_KEY"})
    archive = backups.archive_dir() / backups.create_archive(PASS)["name"]
    ss2, ag2 = MemorySecretProvider(), MemorySecretProvider()
    monkeypatch.setattr(secret_store, "provider", lambda: ss2)
    monkeypatch.setattr(backups, "_agent_store", lambda: ag2)
    _restore_into(tmp_path / "fresh", archive, monkeypatch)
    assert ag2.get("STUDIOFORGE_KEY") == AGENT_SECRET
    assert ag2.get("ANTHROPIC_API_KEY") == "sk-ant-planted-0003"   # scrub-ok: test fixture, not a real credential
    assert ss2.get("STUDIOFORGE_KEY") is None and ss2.get("ANTHROPIC_API_KEY") is None
    assert ss2.get("connection.openai") == "sk-planted-conn-0004"  # scrub-ok: test fixture, not a real credential
    assert ag2.get("connection.openai") is None


def _archived_secrets(archive: Path, tmp_path: Path) -> tuple[dict, str]:
    out = tmp_path / "peek"
    backups.read_archive(archive, PASS, extract_to=out)
    raw = (out / "secrets.json").read_text(encoding="utf-8")
    return json.loads(raw), raw


def test_f_every_name_in_the_store_is_archived_once_and_an_unknown_one_survives(data, tmp_path, monkeypatch):
    c = connections.create(type="openai", api_key="sk-planted-conn-0005")   # scrub-ok: test fixture, not a real credential
    ref = c.credential_ref[len("secret:"):]
    secret_store.put("oidc.client_secret", OIDC_SECRET)          # a name no code in this WP knows (A13's)
    _file_store(data).set("MCP_SEARCH_TOKEN", AGENT_SECRET)       # the agents' store shares the file here
    archive = backups.archive_dir() / backups.create_archive(PASS)["name"]
    doc, raw = _archived_secrets(archive, tmp_path)
    assert {ref, "oidc.client_secret", "MCP_SEARCH_TOKEN"} <= set(doc)
    for name in doc:                                             # no name twice in the document
        assert raw.count(json.dumps(name)) == 1, name
    assert "setting.system.backup_passphrase" not in doc         # the archive's own key never rides inside it
    fresh = tmp_path / "fresh"
    _restore_into(fresh, archive, monkeypatch)
    assert secret_store.get("oidc.client_secret") == OIDC_SECRET
    assert secret_store.get(ref) == "sk-planted-conn-0005"       # scrub-ok: test fixture, not a real credential


def test_g_a_restore_without_secrets_creates_no_secrets_file(data, tmp_path, monkeypatch):
    assert not (data / "agents" / "secrets.json").exists()
    archive = backups.archive_dir() / backups.create_archive(PASS)["name"]
    fresh = tmp_path / "fresh"
    out = _restore_into(fresh, archive, monkeypatch)
    assert not (fresh / "agents" / "secrets.json").exists()
    assert out["re_enter"] == []


def test_restore_lists_a_referenced_key_it_could_not_put_back(data, tmp_path, monkeypatch):
    """An archive from before this fix carries no agent secrets: the restore names what to re-enter,
    and the listing shows it until the key is there again."""
    c = connections.create(type="openai", api_key="sk-planted-conn-0006")   # scrub-ok: test fixture, not a real credential
    ref = c.credential_ref[len("secret:"):]
    monkeypatch.setattr(backups, "_secrets_document", dict)          # what an old archive held
    archive = backups.archive_dir() / backups.create_archive(PASS)["name"]
    fresh = tmp_path / "fresh"
    _make_data(fresh, monkeypatch)
    dest = backups.archive_dir()
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copy2(archive, dest / archive.name)
    staged = backups.stage_restore(f"archive:{archive.name}", PASS)
    assert staged["re_enter"] == [ref]                                   # said before the restart
    out = backups.apply_pending_restore()
    assert out["re_enter"] == [ref]
    assert backups.last_restore()["re_enter"] == [ref]
    secret_store.put(ref, "sk-entered-again-0007")                       # scrub-ok: test fixture, not a real credential
    assert backups.last_restore()["re_enter"] == []


# --------------------------------------------------------------------------- #
# (b): pre-restore copies are their own kind
# --------------------------------------------------------------------------- #


def _dir(root: Path, name: str) -> Path:
    p = root / name
    p.mkdir(parents=True)
    (p / "chats.db").write_bytes(b"x" * 3)
    return p


def test_b_a_pre_restore_copy_is_not_a_pre_migration_copy(data):
    root = backups.backup_root()
    _dir(root, "pre-restore-20261031-120000")
    _dir(root, "4.2.0-20261030-010101")
    rows = backups.list_backups()
    pre = [r for r in rows if r["kind"] == "premigration"]
    assert [r["name"] for r in pre] == ["4.2.0-20261030-010101"]
    prr = [r for r in rows if r["kind"] == "prerestore"]
    assert [r["id"] for r in prr] == ["prerestore:pre-restore-20261031-120000"]
    assert not migrate._BACKUP_DIR_RE.match("pre-restore-20261031-120000")
    assert backups.prune_premigration(0, dry_run=True)["removed"] == 1     # counts only the real one
    assert backups.resolve_id("prerestore:pre-restore-20261031-120000")[0] == "prerestore"
    with pytest.raises(backups.BackupError):
        backups.resolve_id("premigration:pre-restore-20261031-120000")


def test_b_migrations_rotation_never_eats_pre_restore_copies(data):
    root = backups.backup_root()
    for i in range(4):
        _dir(root, f"pre-restore-2026103{i}-120000")
    migrate.backup()
    migrate.backup()
    for i in range(4):
        assert (root / f"pre-restore-2026103{i}-120000").is_dir()


def test_b_pre_restore_retention_keeps_three_and_always_the_newest(data):
    root = backups.backup_root()
    names = [f"pre-restore-202610{d:02d}-120000" for d in (1, 2, 3, 4, 5)]
    for n in names:
        _dir(root, n)
    out = backups.prune_prerestore(3)
    assert out["removed"] == 2
    assert sorted(p.name for p in root.iterdir() if p.name.startswith("pre-restore-")) == names[2:]
    backups.prune_prerestore(0)                                          # never the newest
    assert (root / names[-1]).is_dir()


def test_b_a_pre_restore_copy_is_protected_from_delete_like_a_pre_migration_one(world):
    owner = world.create_owner()
    _dir(backups.backup_root(), "pre-restore-20261031-120000")
    r = owner.delete("/api/backups/prerestore:pre-restore-20261031-120000")
    assert r.status_code == 409 and r.json()["error"]["code"] == "backup_protected"


def test_a_restore_keeps_the_agent_secrets_file_in_its_pre_restore_copy(data):
    _file_store(data).set("MCP_SEARCH_TOKEN", AGENT_SECRET)
    archive = backups.create_archive(PASS)
    _file_store(data).set("MCP_SEARCH_TOKEN", "rotated-after-the-backup")
    backups.stage_restore(f"archive:{archive['name']}", PASS)
    out = backups.apply_pending_restore()
    prev = Path(out["previous"])
    assert prev.name.startswith("pre-restore-")
    assert json.loads((prev / "agents" / "secrets.json").read_text())["MCP_SEARCH_TOKEN"] == "rotated-after-the-backup"


# --------------------------------------------------------------------------- #
# (c) migrate backup + restore carry agents/secrets.json
# --------------------------------------------------------------------------- #


def test_c_migrate_round_trip_with_the_secrets_file(data):
    _file_store(data).set("MCP_SEARCH_TOKEN", AGENT_SECRET)
    dest = migrate.backup()
    assert (dest / "agents" / "secrets.json").is_file()
    _file_store(data).set("MCP_SEARCH_TOKEN", "changed-by-the-migration")
    restored = migrate.restore(dest)
    assert "agents/secrets.json" in restored
    assert _file_store(data).get("MCP_SEARCH_TOKEN") == AGENT_SECRET


def test_c_migrate_round_trip_without_the_secrets_file_removes_a_new_one(data):
    assert not (data / "agents" / "secrets.json").exists()
    dest = migrate.backup()
    assert not (dest / "agents" / "secrets.json").exists()
    _file_store(data).set("MCP_SEARCH_TOKEN", "written-by-the-migration")
    migrate.restore(dest)
    assert not (data / "agents" / "secrets.json").exists()


def test_c_an_old_backup_that_never_recorded_the_file_leaves_it_alone(data):
    dest = migrate.backup()
    (dest / migrate.SECRETS_STATE).unlink()                 # a pre-4.3 backup: nothing recorded
    _file_store(data).set("MCP_SEARCH_TOKEN", AGENT_SECRET)
    migrate.restore(dest)
    assert _file_store(data).get("MCP_SEARCH_TOKEN") == AGENT_SECRET


def test_g_migrate_restore_without_the_file_creates_none(data):
    dest = migrate.backup()
    migrate.restore(dest)
    assert not (data / "agents" / "secrets.json").exists()
    assert not (data / "agents").exists()


# --------------------------------------------------------------------------- #
# (d) reveal the generated recovery key
# --------------------------------------------------------------------------- #

REVEAL = "/api/backups/recovery-key/reveal"


def _generated(owner) -> str:
    body = owner.post("/api/backups", json={}).json()
    return body["recovery_key"]


def test_d_the_protected_admin_with_the_right_pin_or_password_sees_the_key(world):
    owner = world.create_owner()
    key = _generated(owner)
    assert owner.get("/api/backups").json()["schedule"]["generated_key"] is True
    r = owner.post(REVEAL, json={"pin": "2468"})
    assert r.status_code == 200, r.text
    assert r.json()["recovery_key"] == key
    assert r.headers["cache-control"] == "no-store"
    r = owner.post(REVEAL, json={"password": "owner-pass1"})
    assert r.status_code == 200 and r.json()["recovery_key"] == key
    audit = str(owner.get("/api/people/audit").json())
    assert "backup.reveal_key" in audit and key not in audit


def test_d_another_admin_is_refused_before_any_credential_check(world):
    owner = world.create_owner()
    _generated(owner)
    world.person(owner, "Ada", "admin", password="admin-pass1")
    admin = world.client(("203.0.113.80", 42080))
    assert world.login(admin, name="Ada", password="admin-pass1").status_code == 200
    for body in ({"password": "admin-pass1"}, {"pin": "2468"}, {"password": "owner-pass1"}):
        r = admin.post(REVEAL, json=body)
        assert r.status_code == 403, r.text
        assert "recovery_key" not in r.text
    world.person(owner, "Max", "user", pin="5555")
    member = world.client(("203.0.113.81", 42081))
    assert world.login(member, name="Max", pin="5555").status_code == 200
    assert member.post(REVEAL, json={"pin": "5555"}).status_code == 403
    stranger = world.client(("203.0.113.82", 42082))
    assert stranger.post(REVEAL, json={"pin": "2468"}).status_code in (401, 403)


def test_d_a_wrong_pin_is_401_audited_and_throttled(world):
    owner = world.create_owner()
    key = _generated(owner)
    codes = []
    for _ in range(8):
        r = owner.post(REVEAL, json={"pin": "0000"})
        codes.append(r.status_code)
        assert key not in r.text
    assert codes[0] == 401 and owner.post(REVEAL, json={"pin": "0000"}).json()["error"]["code"] in (
        "incorrect_credential", "too_many_attempts")
    assert 429 in codes                                    # the throttle stops the guessing
    r = owner.post(REVEAL, json={"pin": "2468"})          # refused before verifying, even when right
    assert r.status_code == 429 and "retry-after" in {k.lower() for k in r.headers}
    assert "backup.reveal_key" in str(owner.get("/api/people/audit").json())


def test_d_a_passphrase_the_admin_chose_is_never_revealed(world):
    owner = world.create_owner()
    secret_store.put("setting.system.backup_passphrase", "my own long passphrase")
    assert owner.get("/api/backups").json()["schedule"]["generated_key"] is False
    r = owner.post(REVEAL, json={"pin": "2468"})
    assert r.status_code == 404 and r.json()["error"]["code"] == "no_generated_key"
    assert "my own long passphrase" not in r.text


def test_d_a_key_replaced_by_a_chosen_passphrase_is_not_revealed(world):
    owner = world.create_owner()
    _generated(owner)
    secret_store.put("setting.system.backup_passphrase", "chosen later, by hand")
    r = owner.post(REVEAL, json={"pin": "2468"})
    assert r.status_code == 404 and r.json()["error"]["code"] == "no_generated_key"


def test_d_a_scheduled_key_from_before_4_3_can_be_revealed(world):
    """N-16: a scheduled archive generated a key silently and recorded nothing; its shape tells."""
    owner = world.create_owner()
    legacy = backups.recovery_key()
    secret_store.put("setting.system.backup_passphrase", legacy)
    r = owner.post(REVEAL, json={"pin": "2468"})
    assert r.status_code == 200 and r.json()["recovery_key"] == legacy


def test_d_a_scheduled_archive_records_its_generated_key(data):
    from app import settings_store
    settings_store.set_many({"system.encrypted_backup_enabled": True})
    assert backups.run_scheduled_once() is not None
    assert backups.generated_key() == backups.configured_passphrase()


def test_d_the_route_declares_the_host_tier():
    from app import route_policy
    rows = [req for ms, path, req in route_policy.ROUTES if path == REVEAL]
    assert len(rows) == 1 and rows[0].host is True and getattr(rows[0].permission, "value", rows[0].permission) == "admin_system"


# --------------------------------------------------------------------------- #
# (e) the bundle reads the in-memory log ring when there is no log file
# --------------------------------------------------------------------------- #


def test_e_the_bundle_log_tail_comes_from_the_ring_when_no_file_exists(data, monkeypatch):
    """Passes with a stand-in ``app.logbuffer`` here, and with A1's real module once it lands
    (the stand-in has the same ``tail()`` shape A1's plan names)."""
    fake = types.ModuleType("app.logbuffer")
    fake.tail = lambda lines=2000: ["2026-10-31 INFO started", "token=abcdef0123456789abcdef planted"]  # scrub-ok: test fixture
    import app
    monkeypatch.setitem(sys.modules, "app.logbuffer", fake)
    monkeypatch.setattr(app, "logbuffer", fake, raising=False)
    assert not (data / "logs" / "dispatch.log").exists()
    z = zipfile.ZipFile(io.BytesIO(backups.build_support_bundle()))
    text = z.read("log-tail.txt").decode()
    assert "started" in text and text != "(no log file)"
    assert "abcdef0123456789abcdef" not in text                      # redacted on the way out


def test_e_a_log_file_still_wins(data, monkeypatch):
    fake = types.ModuleType("app.logbuffer")
    fake.tail = lambda lines=2000: "from the ring"
    import app
    monkeypatch.setitem(sys.modules, "app.logbuffer", fake)
    monkeypatch.setattr(app, "logbuffer", fake, raising=False)
    (data / "logs").mkdir()
    (data / "logs" / "dispatch.log").write_text("from the file\n", encoding="utf-8")
    text = zipfile.ZipFile(io.BytesIO(backups.build_support_bundle())).read("log-tail.txt").decode()
    assert "from the file" in text and "from the ring" not in text
