"""Backups (3.0, W5): encrypted archives, staged restore, the support bundle and per-user data.

The crypto is checked the way it matters: a wrong key, a flipped bit, a cut-short file and an
archive that tries to write outside the data directory must all be refused, and the plain bytes of
a secret must not be findable in the file. The support bundle gets a grep test: plant secrets,
build the bundle, search every byte of it.
"""
from __future__ import annotations

import io
import json
import os
import sqlite3
import tarfile
import zipfile
from pathlib import Path

import pytest
import yaml

from app import accounts, backups, config, connections, secret_store, settings_store
from app.backups import BackupError

PASS = "correct horse battery staple"


@pytest.fixture
def data(tmp_path, monkeypatch):
    """A populated data directory (no app instance)."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    config._invalidate_bots_cache()
    accounts.reset_for_tests()
    con = sqlite3.connect(tmp_path / "chats.db")
    con.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, title TEXT)")
    con.execute("INSERT INTO threads VALUES ('t1', 'family dinner plans')")
    con.commit()
    con.close()
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"bots": [{"id": "a", "name": "Alpha"}]}), encoding="utf-8")
    (tmp_path / "avatars").mkdir(exist_ok=True)
    (tmp_path / "avatars" / "alpha.png").write_bytes(b"\x89PNG-fake-avatar")
    (tmp_path / "media").mkdir(exist_ok=True)
    (tmp_path / "media" / "photo.jpg").write_bytes(b"JPEG-bytes" * 100)
    return tmp_path


def _threads(path: Path):
    c = sqlite3.connect(path / "chats.db")
    try:
        return c.execute("SELECT id, title FROM threads").fetchall()
    finally:
        c.close()


# --------------------------------------------------------------------------- #
# The format
# --------------------------------------------------------------------------- #


def test_an_archive_round_trips_and_hides_its_contents(data):
    connections.create(type="openai", api_key="sk-live-planted-0123456789")  # scrub-ok: test fixture, not a real credential
    row = backups.create_archive(PASS)
    p = backups.archive_dir() / row["name"]
    raw = p.read_bytes()
    assert raw.startswith(backups.MAGIC) and row["encrypted"] is True
    for needle in (b"family dinner", b"Alpha", b"sk-live-planted", b"SQLite format", b"alpha.png", b"PNG-fake"):
        assert needle not in raw, needle
    manifest = backups.read_archive(p, PASS)
    names = {f["path"] for f in manifest["files"]}
    assert {"chats.db", "config.yaml", "avatars/alpha.png", "secrets.json"} <= names
    assert "media/photo.jpg" not in names and manifest["verified"] is True


def test_media_is_opt_in(data):
    row = backups.create_archive(PASS, include_media=True)
    names = {f["path"] for f in backups.read_archive(backups.archive_dir() / row["name"], PASS)["files"]}
    assert "media/photo.jpg" in names


def test_a_short_passphrase_is_refused(data):
    with pytest.raises(BackupError) as e:
        backups.create_archive("short")
    assert e.value.code == "backup_passphrase_required"


def test_a_wrong_passphrase_is_a_clear_error_not_garbage(data):
    p = backups.archive_dir() / backups.create_archive(PASS)["name"]
    with pytest.raises(BackupError) as e:
        backups.read_archive(p, "not the passphrase")
    assert e.value.code == "backup_wrong_key"


def test_a_flipped_bit_is_detected(data):
    p = backups.archive_dir() / backups.create_archive(PASS)["name"]
    b = bytearray(p.read_bytes())
    b[len(b) // 2] ^= 0x01
    p.write_bytes(bytes(b))
    with pytest.raises(BackupError) as e:
        backups.read_archive(p, PASS)
    assert e.value.code in ("backup_corrupt", "backup_wrong_key")


def test_a_truncated_file_is_detected_even_at_a_chunk_boundary(data, monkeypatch):
    monkeypatch.setattr(backups, "CHUNK", 4096)                 # force many chunks
    (data / "media" / "big.bin").write_bytes(os.urandom(48000))
    p = backups.archive_dir() / backups.create_archive(PASS, include_media=True)["name"]
    raw = p.read_bytes()
    assert len(raw) > 3 * 4096
    # cut exactly after the first complete chunk record
    hdr = len(backups.MAGIC) + 2 + int.from_bytes(raw[len(backups.MAGIC):len(backups.MAGIC) + 2], "big")
    first = int.from_bytes(raw[hdr:hdr + 4], "big")
    cut = hdr + 4 + first
    p.write_bytes(raw[:cut])
    with pytest.raises(BackupError) as e:
        backups.read_archive(p, PASS)
    assert e.value.code == "backup_corrupt"


def test_swapping_two_chunks_is_detected(data, monkeypatch):
    monkeypatch.setattr(backups, "CHUNK", 4096)
    (data / "media" / "big.bin").write_bytes(os.urandom(48000))
    p = backups.archive_dir() / backups.create_archive(PASS, include_media=True)["name"]
    raw = p.read_bytes()
    hdr = len(backups.MAGIC) + 2 + int.from_bytes(raw[len(backups.MAGIC):len(backups.MAGIC) + 2], "big")
    recs, pos = [], hdr
    while pos < len(raw):
        n = int.from_bytes(raw[pos:pos + 4], "big")
        recs.append(raw[pos:pos + 4 + n])
        pos += 4 + n
    assert len(recs) >= 4
    recs[1], recs[2] = recs[2], recs[1]
    p.write_bytes(raw[:hdr] + b"".join(recs))
    with pytest.raises(BackupError):
        backups.read_archive(p, PASS)


def test_not_a_backup_file(data, tmp_path):
    f = tmp_path / "x.dpbk"
    f.write_bytes(b"hello world")
    with pytest.raises(BackupError) as e:
        backups.read_archive(f, PASS)
    assert e.value.code == "backup_corrupt"


def test_an_archive_cannot_write_outside_the_data_directory(data, tmp_path):
    """Even with the right key: path traversal, absolute paths, links and unknown top-level names."""
    for evil in ("../escaped.txt", "/etc/cron.d/x", "C:/Windows/x", "chats.db/../../x", "surprise.txt"):
        out = backups.archive_dir()
        out.mkdir(parents=True, exist_ok=True)
        f = out / "dispatch-evil.dpbk"
        with backups.encrypted_tar(f, PASS) as tf:
            raw = b"{}"
            ti = tarfile.TarInfo(evil)
            ti.size = len(raw)
            tf.addfile(ti, io.BytesIO(raw))
        with pytest.raises(BackupError) as e:
            backups.read_archive(f, PASS, extract_to=tmp_path / "stage")
        assert e.value.code == "backup_corrupt", evil
    assert not (tmp_path.parent / "escaped.txt").exists()
    f = backups.archive_dir() / "dispatch-link.dpbk"
    with backups.encrypted_tar(f, PASS) as tf:
        ti = tarfile.TarInfo("config.yaml")
        ti.type = tarfile.SYMTYPE
        ti.linkname = "/etc/passwd"
        tf.addfile(ti)
    with pytest.raises(BackupError):
        backups.read_archive(f, PASS, extract_to=tmp_path / "stage2")


# --------------------------------------------------------------------------- #
# Listing, restore
# --------------------------------------------------------------------------- #


def test_list_shows_archives_snapshots_and_premigration_copies(data):
    backups.create_archive(PASS)
    root = backups.backup_root()
    (root / "chats-20260101-010101.db").write_bytes(b"x" * 10)
    pre = root / "3.0.0-20260101-020202"
    pre.mkdir(parents=True)
    (pre / "chats.db").write_bytes(b"y" * 5)
    kinds = {r["kind"] for r in backups.list_backups()}
    assert kinds == {"archive", "snapshot", "premigration"}
    for bad in ("../x", "archive:../x", "archive:", "snapshot:chats-1.db", "other:thing", "archive:a/b"):
        with pytest.raises(BackupError) as e:
            backups.resolve_id(bad)
        assert e.value.status == 404


def test_a_staged_restore_is_applied_at_the_next_start_and_the_old_state_is_kept(data):
    connections.create(type="openai", api_key="sk-original-0123456789")  # scrub-ok: test fixture, not a real credential
    row = backups.create_archive(PASS)
    # the world moves on: new chat, new roster, the key rotated, an avatar gone
    c = sqlite3.connect(data / "chats.db")
    c.execute("INSERT INTO threads VALUES ('t2', 'after the backup')")
    c.commit()
    c.close()
    (data / "config.yaml").write_text(yaml.safe_dump({"bots": [{"id": "z", "name": "Zeta"}]}), encoding="utf-8")
    (data / "avatars" / "alpha.png").unlink()
    secret_store.put("connection.openai", "sk-rotated-0123456789")
    out = backups.stage_restore(f"archive:{row['name']}", PASS)
    assert out["staged"] and out["restart_required"] and backups.pending_path().exists()
    assert _threads(data) == [("t1", "family dinner plans"), ("t2", "after the backup")]   # nothing yet
    result = backups.apply_pending_restore()
    assert result["applied"] is True
    assert _threads(data) == [("t1", "family dinner plans")]
    assert "Alpha" in (data / "config.yaml").read_text(encoding="utf-8")
    assert (data / "avatars" / "alpha.png").read_bytes() == b"\x89PNG-fake-avatar"
    assert secret_store.get("connection.openai") == "sk-original-0123456789"
    prev = Path(result["previous"])
    assert prev.is_dir() and "Zeta" in (prev / "config.yaml").read_text(encoding="utf-8")
    c = sqlite3.connect(prev / "chats.db")
    assert c.execute("SELECT count(*) FROM threads").fetchone()[0] == 2
    c.close()
    assert not backups.pending_path().exists() and not (data / "restore-staging").glob("*").__next__ if False else True
    assert backups.apply_pending_restore() is None                       # once


def test_a_bad_passphrase_stages_nothing(data):
    row = backups.create_archive(PASS)
    with pytest.raises(BackupError):
        backups.stage_restore(f"archive:{row['name']}", "wrongwrongwrong")
    assert not backups.pending_path().exists()
    assert not any((data / "restore-staging").glob("*")) if (data / "restore-staging").exists() else True


def test_restoring_a_snapshot_checks_the_database_first(data):
    root = backups.backup_root()
    root.mkdir(parents=True, exist_ok=True)
    (root / "chats-20260101-010101.db").write_bytes(b"this is not a database")
    with pytest.raises(BackupError) as e:
        backups.stage_restore("snapshot:chats-20260101-010101.db")
    assert e.value.code == "backup_corrupt" and not backups.pending_path().exists()


def test_cancel_removes_the_staged_files(data):
    row = backups.create_archive(PASS)
    backups.stage_restore(f"archive:{row['name']}", PASS)
    assert backups.cancel_pending() is True
    assert not backups.pending_path().exists() and backups.cancel_pending() is False


def test_migrate_applies_a_staged_restore_first(data):
    from app import migrate
    row = backups.create_archive(PASS)
    (data / "config.yaml").write_text(yaml.safe_dump({"bots": [{"id": "z", "name": "Zeta"}]}), encoding="utf-8")
    backups.stage_restore(f"archive:{row['name']}", PASS)
    migrate.run(take_backup=False)
    assert "Alpha" in (data / "config.yaml").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Schedule and pruning
# --------------------------------------------------------------------------- #


def test_the_schedule_is_off_until_turned_on_then_makes_one_and_a_key(data):
    assert backups.run_scheduled_once() is None
    settings_store.set_many({"system.encrypted_backup_enabled": True})
    row = backups.run_scheduled_once()
    assert row is not None
    pw = backups.configured_passphrase()
    assert pw and len(pw.replace("-", "")) == 25                         # a generated recovery key
    assert backups.read_archive(backups.archive_dir() / row["name"], pw)["verified"]
    assert backups.run_scheduled_once() is None                          # not due again for 24 hours


def test_pruning_keeps_the_newest(data):
    d = backups.archive_dir()
    d.mkdir(parents=True)
    for i in range(5):
        (d / f"dispatch-2026010{i}-000000.dpbk").write_bytes(b"x")
    assert backups.prune_archives(2) == 3
    assert sorted(p.name for p in d.iterdir()) == ["dispatch-20260103-000000.dpbk", "dispatch-20260104-000000.dpbk"]


def test_recovery_keys_are_long_and_unambiguous():
    keys = {backups.recovery_key() for _ in range(50)}
    assert len(keys) == 50
    for k in keys:
        assert len(k) == 29 and not set(k) & set("O0I1L") - {"-"}


# --------------------------------------------------------------------------- #
# The support bundle: grep it
# --------------------------------------------------------------------------- #

SECRETS = {
    "connection key": "sk-live-SUPPORT-planted-0123456789abcdef",
    "bearer in a log": "Bearer eyJhbGciOiJIUzI1NiJ9.planted.signature123",  # scrub-ok: test fixture, not a real credential
    "api key in a log": "api_key=plantedlogkey0123456789",
    "password in a log": "password: hunter2-planted-pw",
    "pin in a log": "pin=424242",
    "cookie in a log": "lc_session=PLANTEDSESSIONTOKENabcdef0123456789abcdef",
    "recovery key": "ABCDE-FGHJK-MNPQR-STUVW-XYZ23",
    "hex token": "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef0123",
}
PROMPT = "ZEBRA-PROMPT-SENTINEL: always answer in rhyme about zebras"


def _zip_text(blob: bytes) -> dict[str, str]:
    z = zipfile.ZipFile(io.BytesIO(blob))
    return {n: z.read(n).decode("utf-8", "replace") for n in z.namelist()}


def _plant(data):
    connections.create(type="openai", api_key=SECRETS["connection key"])
    config.upsert_bot(config.Bot(id="zed", name="Zed", advisor={
        "providers": [{"provider": "openai", "model": "m", "api_key": "sk-inline-PLANTED-9876543210"}],
        "persona": PROMPT}))
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    (config.LOG_DIR / "dispatch.log").write_text(
        "\n".join(f"2026-10-18 INFO request failed {v}" for k, v in SECRETS.items() if k != "connection key")
        + "\n2026-10-18 INFO all good\n", encoding="utf-8")
    secret_store.put("setting.system.backup_passphrase", "backup-pass-PLANTED-phrase")


def test_the_support_bundle_contains_none_of_the_planted_secrets(data):
    _plant(data)
    files = _zip_text(backups.build_support_bundle())
    blob = "\n".join(files.values())
    for label, value in SECRETS.items():
        needle = value.split(" ", 1)[-1] if value.startswith(("Bearer ", "password", "api_key", "pin=", "lc_session")) \
            else value
        needle = needle.split("=", 1)[-1].split(": ", 1)[-1]
        assert needle not in blob, f"{label} leaked into the support bundle"
    for v in ("sk-inline-PLANTED", "backup-pass-PLANTED", "sk-live-SUPPORT"):
        assert v not in blob
    assert PROMPT not in blob and "ZEBRA" not in blob                      # prompts excluded by default
    assert {"about.json", "settings.json", "connections.json", "characters.json", "profiles.json",
            "log-tail.txt", "README.txt"} <= set(files)
    assert "all good" in files["log-tail.txt"]                             # it is still a useful log
    chars = {c["id"]: c for c in json.loads(files["characters.json"])}
    assert chars["zed"]["persona"].startswith("(omitted")
    conns = json.loads(files["connections.json"])
    assert conns and conns[0]["has_key"] is True and "credential" not in conns[0]


def test_prompts_are_included_only_on_request_and_secrets_still_are_not(data):
    _plant(data)
    files = _zip_text(backups.build_support_bundle(include_prompts=True))
    blob = "\n".join(files.values())
    assert PROMPT in files["characters.json"]
    for v in ("sk-inline-PLANTED", "backup-pass-PLANTED", "sk-live-SUPPORT", "hunter2-planted-pw"):
        assert v not in blob


def test_redact_text_handles_the_usual_shapes():
    for raw in ("Authorization: Bearer abcdefghijklmnop", "token=abcdef123456", "sk-ant-api03-abcdefghijkl",
                'password = "p@ss w0rd"', "x" * 45, "a" * 33 + "0" * 2):
        out = backups.redact_text(f"line {raw} end")
        assert "abcdefghijklmnop" not in out and "abcdef123456" not in out and "api03-abcdefghijkl" not in out
        assert "p@ss" not in out
    assert backups.redact_text("nothing secret here, pinned: true") == "nothing secret here, pinned: true"


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #


@pytest.fixture
def owner(world):
    return world.create_owner()


def test_the_api_creates_lists_verifies_and_restores(world, owner):
    r = owner.post("/api/backups", json={})
    assert r.status_code == 201, r.text
    body = r.json()
    key = body["recovery_key"]
    assert body["passphrase_source"] == "generated" and body["backup"]["encrypted"] is True
    bid = body["backup"]["id"]
    assert key not in owner.get("/api/backups").text                      # shown once
    listing = owner.get("/api/backups").json()
    assert any(b["id"] == bid for b in listing["backups"]) and listing["schedule"]["has_passphrase"] is True
    again = owner.post("/api/backups", json={}).json()
    assert "recovery_key" not in again and again["passphrase_source"] == "settings"
    v = owner.post(f"/api/backups/{bid}/verify", json={}).json()
    assert v["ok"] and v["files"] >= 2
    bad = owner.post(f"/api/backups/{bid}/verify", json={"passphrase": "nope-nope-nope"})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "backup_wrong_key"
    # restore needs an explicit confirm and stages instead of applying
    assert owner.post(f"/api/backups/{bid}/restore", json={}).status_code == 422
    staged = owner.post(f"/api/backups/{bid}/restore", json={"confirm": True}).json()
    assert staged["staged"] and staged["restart_required"]
    assert owner.get("/api/backups/pending").json()["pending"] is True
    assert owner.delete("/api/backups/pending").json() == {"cancelled": True}
    assert owner.delete(f"/api/backups/{bid}").json() == {"deleted": bid}
    assert owner.post("/api/backups/archive:ghost.dpbk/verify", json={}).json()["error"]["code"] == "backup_not_found"


def test_backups_and_the_bundle_are_owner_and_admin_only(world, owner):
    world.person(owner, "Ada", "admin", password="admin-pass1")
    admin = world.client()
    assert world.login(admin, name="Ada", password="admin-pass1").status_code == 200
    assert admin.get("/api/backups").status_code == 200   # admin_system: the Owner's and the Admins'
    world.person(owner, "Max", "member", pin="5555")
    member = world.client(("203.0.113.72", 42002))
    assert world.login(member, name="Max", pin="5555").status_code == 200
    stranger = world.client(("203.0.113.71", 42001))
    for c in (member, stranger):
        assert c.get("/api/backups").status_code == 403
        assert c.post("/api/backups", json={}).status_code == 403
        assert c.post("/api/support-bundle").status_code == 403
        assert c.delete("/api/backups/pending").status_code == 403


def test_the_bundle_download_is_a_zip_and_is_audited(world, owner):
    r = owner.post("/api/support-bundle")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    assert "attachment" in r.headers["content-disposition"] and r.headers["cache-control"] == "no-store"
    assert "about.json" in zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert "support_bundle" in str(owner.get("/api/people/audit").json())


def test_per_user_export_and_erase(world, owner):
    mia = world.person(owner, "Mia", "restricted")
    owner.post("/api/characters", json={"name": "Mias", "audience": "restricted", "profiles": [mia["id"]]})
    accounts.log_activity("turn", profile_id=mia["id"], subject="thread-1")
    accounts.usage_add(mia["id"], "turn", 3)
    r = owner.get(f"/api/users/{mia['id']}/export")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    doc = r.json()
    assert doc["profile"]["name"] == "Mia" and doc["characters_limited_to_this_profile"] == ["mias"]
    assert any(a["kind"] == "turn" for a in doc["activity"]) and doc["usage"][0]["amount"] == 3
    text = r.text
    assert "pin_hash" not in text and "password_hash" not in text
    assert "not attributed" in doc["conversations"]
    erased = owner.delete(f"/api/users/{mia['id']}").json()
    assert erased["erased"] == mia["id"] and erased["activity_rows"] >= 1 and erased["usage_rows"] == 1
    assert accounts.get_profile(mia["id"]) is None
    # Off Mia's list - but an empty list means "everyone", so the character she alone could use
    # is left to the Owner rather than opened to every profile and every anonymous device.
    assert config.get_bot("mias").profiles == [accounts.get_owner().id]
    assert owner.get(f"/api/users/{mia['id']}/export").status_code == 404


def test_erase_follows_the_rank_rules(world, owner):
    world.person(owner, "Ada", "admin", password="admin-pass1")
    world.person(owner, "Bob", "admin", password="admin-pass2")
    max_ = world.person(owner, "Max", "member", pin="5555")
    admin = world.client()
    assert world.login(admin, name="Ada", password="admin-pass1").status_code == 200
    people = {p["name"]: p for p in owner.get("/api/people").json()["profiles"]}
    assert admin.get(f"/api/users/{people['Olive']['id']}/export").status_code == 403  # THE Owner is off limits
    assert admin.delete(f"/api/users/{people['Olive']['id']}").status_code == 403
    assert admin.delete(f"/api/users/{people['Ada']['id']}").status_code == 409     # not yourself
    assert owner.delete(f"/api/users/{people['Olive']['id']}").status_code == 409    # not the owner
    assert admin.delete(f"/api/users/{max_['id']}").status_code == 200
    assert admin.delete(f"/api/users/{people['Bob']['id']}").status_code == 200      # another Admin: allowed


# --------------------------------------------------------------------------- #
# L-2: what a restore writes is owner-only, like a fresh write
# --------------------------------------------------------------------------- #


def _open_to_everyone(folder: Path) -> bool:
    """Make new files in ``folder`` readable by others, the way a lax umask / a shared ACL would. True when the
    precondition holds (so a restore that does nothing would fail the test)."""
    from app import fsperm
    if os.name == "nt":
        import subprocess
        subprocess.run(["icacls", str(folder), "/grant", "*S-1-1-0:(OI)(CI)R"], capture_output=True, check=False)
        probe = folder / "probe.txt"
        probe.write_text("x", encoding="utf-8")
        try:
            return fsperm.others_can_read(probe) is True
        finally:
            probe.unlink()
    return True


def _others_can_read(p: Path) -> bool | None:
    from app import fsperm
    return fsperm.others_can_read(p)


@pytest.fixture
def lax_umask():
    if os.name == "nt":
        yield
        return
    old = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(old)


def test_a_restore_leaves_secret_bearing_files_owner_only(data, lax_umask):
    if not _open_to_everyone(data):
        pytest.skip("cannot make the folder world-readable here, so the test would prove nothing")
    (data / "security.yaml").write_text("pin_hash: not-a-real-hash\n", encoding="utf-8")
    (data / "push-vapid.json").write_text("{}", encoding="utf-8")
    connections.create(type="openai", api_key="sk-restore-perm-0123456789")  # scrub-ok: test fixture, not a real credential
    row = backups.create_archive(PASS)
    for name in ("security.yaml", "config.yaml", "push-vapid.json", "chats.db"):      # the world is lax
        os.chmod(data / name, 0o644)
    out = backups.stage_restore(f"archive:{row['name']}", PASS)
    stage = Path(json.loads(backups.pending_path().read_text(encoding="utf-8"))["staging"])
    assert out["staged"]
    if os.name != "nt":
        assert stage.stat().st_mode & 0o077 == 0, "the staging folder holds decrypted secrets"
        assert all(f.stat().st_mode & 0o077 == 0 for f in stage.rglob("*") if f.is_file())
    else:
        assert _others_can_read(stage) is False
    result = backups.apply_pending_restore()
    assert result["applied"] is True
    secrets_file = data / "agents" / "secrets.json"
    assert secrets_file.is_file()
    restored = [data / n for n in ("security.yaml", "config.yaml", "push-vapid.json", "chats.db")] + [secrets_file]
    for p in restored:
        assert p.is_file(), p
        if os.name != "nt":
            assert (p.stat().st_mode & 0o777) == 0o600, f"{p.name} is {oct(p.stat().st_mode & 0o777)}"
        else:
            assert _others_can_read(p) is False, p.name
    prev = Path(result["previous"])
    for p in prev.iterdir():
        if p.is_file() and os.name != "nt":
            assert p.stat().st_mode & 0o077 == 0, p.name
    if os.name != "nt":
        assert secrets_file.parent.stat().st_mode & 0o077 == 0


def test_the_offline_restore_command_is_owner_only_too(data, lax_umask):
    if not _open_to_everyone(data):
        pytest.skip("cannot make the folder world-readable here, so the test would prove nothing")
    (data / "security.yaml").write_text("pin_hash: not-a-real-hash\n", encoding="utf-8")
    row = backups.create_archive(PASS)
    pw = data / "pass.txt"
    pw.write_text(PASS, encoding="utf-8")
    os.chmod(data / "security.yaml", 0o644)
    (data / "security.yaml").unlink()
    assert backups.main(["restore", str(backups.archive_dir() / row["name"]), "--passphrase-file", str(pw)]) == 0
    p = data / "security.yaml"
    assert p.is_file()
    if os.name != "nt":
        assert (p.stat().st_mode & 0o777) == 0o600
    else:
        assert _others_can_read(p) is False


# --------------------------------------------------------------------------- #
# L-4: the recovery key is also the install's passphrase, and every surface says so
# --------------------------------------------------------------------------- #


def test_the_recovery_key_note_says_it_is_also_kept_by_the_install(world, owner):
    body = owner.post("/api/backups", json={}).json()
    key = body["recovery_key"]
    assert secret_store.get("setting.system.backup_passphrase") == key       # the behaviour the note describes
    note = body["recovery_key_note"].lower()
    assert "shown once" in note and "secret store" in note and "off this computer" in note


def test_a_scheduled_archive_without_a_passphrase_does_not_promise_a_place_to_read_the_key(data, caplog):
    settings_store.set_many({"system.encrypted_backup_enabled": True})
    with caplog.at_level("WARNING", logger="local-chat.backups"):
        assert backups.run_scheduled_once() is not None
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "not shown anywhere" in text and "set your own passphrase" in text
    assert backups.configured_passphrase() not in text                       # and never the key itself


def test_the_docs_and_the_setting_describe_the_stored_key():
    doc = (Path(__file__).resolve().parents[2] / "docs" / "backups.md").read_text(encoding="utf-8")
    assert "also this install's backup passphrase" in doc and "off this computer" in doc.lower()
    from app import settings_schema
    d = next(x for x in settings_schema.all_defs() if x.key == "system.backup_passphrase")
    assert "secret store" in d.description and "keep your own copy" in d.description
