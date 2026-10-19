"""Locked-out recovery without the command line: the RESET_LOGIN sentinel and DISPATCH_RESET_OWNER.

Both are read once, at start-up, from the filesystem / process environment of whoever starts the
server. These tests pin: what each form does, that it cannot replay, that every refusal still
consumes the file, and that nothing on the network can trigger it. The cross-process tests prove
a command-line change reaches a RUNNING server (profile-cache revision, trusted-device file).
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from app import accounts, auth, credentials, policy, recovery

BACKEND = Path(__file__).resolve().parents[1]
LAN = ("203.0.113.50", 40000)
OLD = "owner-pass1"


@pytest.fixture(autouse=True)
def strict(monkeypatch):
    """The secure default: an install nobody owns is closed to the network."""
    monkeypatch.delenv("DISPATCH_ALLOW_OPEN_INSTALL", raising=False)
    monkeypatch.delenv(recovery.ENV_NAME, raising=False)


def sentinel(world, text: str | bytes = "", name: str = recovery.SENTINEL_NAME) -> Path:
    p = world.tmp / name
    if isinstance(text, str):
        p.write_text(text, encoding="utf-8", newline="")
    else:
        p.write_bytes(text)
    return p


def status(client) -> dict:
    return client.get("/api/auth/status").json()


def login(world, client, password, name="Olive"):
    return world.login(client, name=name, password=password)


def recovery_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("login recovery")]


# --------------------------------------------------------------------------- #
# The sentinel
# --------------------------------------------------------------------------- #


def test_an_empty_sentinel_reopens_setup_and_is_consumed(world, caplog):
    owner_c = world.create_owner()
    owner_id = accounts.get_owner().id
    session = owner_c.cookies.get(world.main.COOKIE_NAME)
    assert session and auth.get_session(session) is not None
    p = sentinel(world, "")
    with caplog.at_level(logging.INFO):
        lan = world.client(LAN)
    assert not p.exists()                                    # consumed
    assert status(lan)["claimed"] is False and status(lan)["setup_required"] is True
    owner = accounts.get_owner()
    assert owner.id == owner_id and owner.name == "Olive"    # the row, and its history, stay
    assert not owner.has_password and not owner.has_pin
    assert auth.get_session(session) is None                 # the old sign-in died
    token = auth.SETUP_TOKEN_PATH.read_text().strip()
    assert token in caplog.text                              # the banner prints the same token
    lines = recovery_lines(caplog)
    assert len(lines) == 1 and "RESET_LOGIN consumed" in lines[0]
    assert OLD not in caplog.text and token not in lines[0]

    # ...and the wizard, from another device with that token, gives the SAME row its new password.
    bad = lan.post("/api/auth/setup", json={"password": "fresh-pass-99", "setup_token": "nope-nope-nope-nope"})
    assert bad.status_code == 403
    ok = lan.post("/api/auth/setup", json={"name": "Whoever", "password": "fresh-pass-99", "setup_token": token})
    assert ok.status_code == 200, ok.text
    assert accounts.get_owner().id == owner_id and accounts.get_owner().name == "Olive"
    assert not auth.SETUP_TOKEN_PATH.exists()                # a used token is gone
    assert login(world, world.client(LAN), "fresh-pass-99").status_code == 200
    assert login(world, world.client(LAN), OLD).status_code == 401


def test_a_sentinel_with_name_checks_the_name(world, caplog):
    world.create_owner()
    sentinel(world, "someone-else\n")
    with caplog.at_level(logging.INFO):
        lan = world.client(LAN)
    assert status(lan)["claimed"] is True                    # nothing was reset
    assert accounts.get_owner().has_password
    assert any("NOT applied" in ln and "'Olive'" in ln for ln in recovery_lines(caplog))
    sentinel(world, "OLIVE")
    world.client(LAN)
    assert status(lan)["claimed"] is False                   # the right name (any case) resets


@pytest.mark.parametrize("content", ["Olive:brand-new-pass1", "Olive:brand-new-pass1\r\nignored second line\n",
                                     "﻿Olive:brand-new-pass1\n"])
def test_name_colon_password_sets_the_owner_password(world, caplog, content):
    world.create_owner()
    p = sentinel(world, content)
    with caplog.at_level(logging.INFO):
        lan = world.client(LAN)
    assert not p.exists()
    assert status(lan)["claimed"] is True
    assert login(world, world.client(LAN), "brand-new-pass1").status_code == 200
    assert login(world, world.client(LAN), OLD).status_code == 401
    lines = recovery_lines(caplog)
    assert len(lines) == 1 and "brand-new-pass1" not in caplog.text and "password" in lines[0]
    assert not auth.SETUP_TOKEN_PATH.exists()


def test_the_password_may_contain_colons_and_spaces(world):
    world.create_owner()
    sentinel(world, "Olive:pa:ss word:99")
    world.client(LAN)
    assert login(world, world.client(LAN), "pa:ss word:99").status_code == 200


def test_utf16_sentinels_from_windows_powershell_are_read(world):
    world.create_owner()
    sentinel(world, "Olive:brand-new-pass1\r\n".encode("utf-16"))      # BOM + UTF-16LE
    world.client(LAN)
    assert login(world, world.client(LAN), "brand-new-pass1").status_code == 200


def test_the_txt_spelling_notepad_makes_is_accepted(world):
    world.create_owner()
    p = sentinel(world, "Olive:brand-new-pass1", name="RESET_LOGIN.txt")
    world.client(LAN)
    assert not p.exists()
    assert login(world, world.client(LAN), "brand-new-pass1").status_code == 200


def test_a_sentinel_creates_the_owner_when_there_is_none(world):
    sentinel(world, "Newbie:first-pass-12")
    lan = world.client(LAN)
    assert status(lan)["claimed"] is True
    assert accounts.get_owner().name == "Newbie"
    assert login(world, world.client(LAN), "first-pass-12", name="Newbie").status_code == 200


def test_a_short_password_is_refused_but_the_file_is_still_consumed(world, caplog):
    world.create_owner()
    p = sentinel(world, "Olive:abc")
    with caplog.at_level(logging.INFO):
        world.client(LAN)
    assert not p.exists()
    assert login(world, world.client(LAN), OLD).status_code == 200      # unchanged
    assert any("NOT applied" in ln for ln in recovery_lines(caplog))


def test_a_sentinel_cannot_replay(world):
    world.create_owner()
    sentinel(world, "Olive:brand-new-pass1")
    world.client(LAN)
    auth.set_profile_password(accounts.get_owner().id, "another-pass-77")   # the owner moved on
    world.client(LAN)                                                       # a second start
    world.client(LAN)                                                       # and a third
    assert login(world, world.client(LAN), "another-pass-77").status_code == 200
    assert login(world, world.client(LAN), "brand-new-pass1").status_code == 401


def test_a_sentinel_that_cannot_be_deleted_is_not_acted_on(world, monkeypatch, caplog):
    world.create_owner()
    p = sentinel(world, "")
    real = Path.unlink

    def stuck(self, *a, **kw):
        if self.name == recovery.SENTINEL_NAME:
            raise PermissionError("read-only volume")
        return real(self, *a, **kw)
    monkeypatch.setattr(Path, "unlink", stuck)
    with caplog.at_level(logging.INFO):
        lan = world.client(LAN)
    assert p.exists() and status(lan)["claimed"] is True                  # a survivor would replay
    assert any("NOT acting" in ln for ln in recovery_lines(caplog))


def test_an_oversized_or_binary_sentinel_is_deleted_unused(world):
    world.create_owner()
    big = sentinel(world, "Olive:" + "x" * 5000)
    world.client(LAN)
    assert not big.exists() and login(world, world.client(LAN), OLD).status_code == 200
    junk = sentinel(world, b"\xff\xfd\xfa\x80\x81")
    world.client(LAN)
    assert not junk.exists() and login(world, world.client(LAN), OLD).status_code == 200


def test_a_directory_named_like_the_sentinel_is_ignored(world):
    world.create_owner()
    (world.tmp / recovery.SENTINEL_NAME).mkdir()
    lan = world.client(LAN)
    assert status(lan)["claimed"] is True


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_a_world_writable_sentinel_is_deleted_unused(world):
    world.create_owner()
    p = sentinel(world, "")
    p.chmod(0o666)
    lan = world.client(LAN)
    assert not p.exists() and status(lan)["claimed"] is True


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need a privilege on Windows")
def test_a_symlink_sentinel_is_not_followed(world, tmp_path_factory):
    world.create_owner()
    target = tmp_path_factory.mktemp("elsewhere") / "note"
    target.write_text("")
    (world.tmp / recovery.SENTINEL_NAME).symlink_to(target)
    lan = world.client(LAN)
    assert status(lan)["claimed"] is True and target.exists()


# --------------------------------------------------------------------------- #
# The environment variable
# --------------------------------------------------------------------------- #


def test_the_variable_resets_once_per_value(world, monkeypatch, caplog):
    world.create_owner()
    monkeypatch.setenv(recovery.ENV_NAME, "1")
    with caplog.at_level(logging.INFO):
        lan = world.client(LAN)
    assert status(lan)["claimed"] is False and auth.SETUP_TOKEN_PATH.exists()
    assert (world.tmp / recovery.APPLIED_NAME).read_text().strip() == "1"
    assert len(recovery_lines(caplog)) == 1

    # The owner sets a new password in the wizard; the variable is still in the .env file...
    auth.set_profile_password(accounts.get_owner().id, "fresh-pass-99")
    auth.burn_setup_token()
    caplog.clear()
    with caplog.at_level(logging.INFO):
        lan = world.client(LAN)
    assert status(lan)["claimed"] is True                    # ...and a restart does NOT wipe it again
    assert any("already applied" in ln for ln in recovery_lines(caplog))

    monkeypatch.setenv(recovery.ENV_NAME, "2")               # a different value is a new request
    lan = world.client(LAN)
    assert status(lan)["claimed"] is False


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off"])
def test_off_values_do_nothing(world, monkeypatch, value):
    world.create_owner()
    monkeypatch.setenv(recovery.ENV_NAME, value)
    assert status(world.client(LAN))["claimed"] is True
    assert not (world.tmp / recovery.APPLIED_NAME).exists()


def test_the_legacy_prefix_and_the_settings_store_do_not_count(world, monkeypatch):
    world.create_owner()
    monkeypatch.setenv("LOCAL_CHAT_RESET_OWNER", "1")        # config.env() would honour this
    assert status(world.client(LAN))["claimed"] is True


def test_the_variable_never_sets_a_password(world, monkeypatch):
    world.create_owner()
    monkeypatch.setenv(recovery.ENV_NAME, "Olive:sneaky-pass-1")
    world.client(LAN)
    assert not accounts.get_owner().has_password             # it only ever opens setup
    assert login(world, world.client(LAN), "sneaky-pass-1").status_code != 200


# --------------------------------------------------------------------------- #
# Not over the network
# --------------------------------------------------------------------------- #


def test_nothing_on_the_network_can_trigger_it(world):
    local = world.create_owner()
    lan = world.client(LAN)
    p = sentinel(world, "")                                   # planted AFTER start-up
    hits = []
    for c in (local, lan):
        hits += [c.get("/"), c.get("/api/health"), c.get("/api/auth/status"), c.get("/api/people"),
                 c.get("/api/bots"), c.get("/static/../RESET_LOGIN"), c.get("/ext/RESET_LOGIN"),
                 c.post("/api/auth/setup", json={"password": "grab-it-now-1", "setup_token": "x" * 20}),
                 c.post("/api/auth/login", json={"name": "Olive", "password": "wrong-pass-1"}),
                 c.post("/api/auth/unlock", json={"pin": "0000"}),
                 c.post("/api/auth/recover", json={"code": "LC-AAAA-BBBB"}),
                 c.post("/api/auth/switch", json={"name": "Olive", "password": "wrong-pass-1"})]
    assert all(h.status_code < 500 for h in hits)
    assert p.exists() and p.read_text() == ""                 # still there, still unread
    assert status(lan)["claimed"] is True and accounts.get_owner().has_password
    # (a NEW client would be a new start, which is exactly when the file is read)
    assert login(world, lan, OLD).status_code == 200


def test_only_the_lifespan_calls_the_start_up_hook():
    """The structural half of "not over the network": no route handler, WebSocket action or
    background loop can reach the sentinel code, because only `_lifespan_startup` calls it."""
    app_dir = BACKEND / "app"
    callers = {p.name for p in app_dir.rglob("*.py")
               if "apply_startup_reset" in p.read_text(encoding="utf-8")}
    assert callers == {"main.py", "recovery.py"}
    main_src = (app_dir / "main.py").read_text(encoding="utf-8")
    assert main_src.count("apply_startup_reset") == 1
    start = main_src.index("async def _lifespan_startup")
    end = main_src.index("\nasync def ", start + 10)
    assert "apply_startup_reset" in main_src[start:end]
    # and nothing else names the file or the variable
    namers = {p.name for p in app_dir.rglob("*.py")
              if re.search(r"RESET_LOGIN|DISPATCH_RESET_OWNER|SENTINEL_NAME", p.read_text(encoding="utf-8"))}
    assert namers <= {"recovery.py", "cli.py", "main.py"}


# --------------------------------------------------------------------------- #
# The command line reaches a running server
# --------------------------------------------------------------------------- #


def _cli(world, *argv, stdin=""):
    env = {**os.environ, "PYTHONPATH": str(BACKEND), "DISPATCH_DATA_DIR": str(world.tmp)}
    env.pop("LOCAL_CHAT_DATA_DIR", None)
    cp = subprocess.run([sys.executable, "-m", "app", "--data-dir", str(world.tmp), *argv], cwd=BACKEND,
                        env=env, input=stdin, capture_output=True, text=True, timeout=180)
    assert cp.returncode == 0, cp.stderr
    return cp


def test_a_cli_password_change_reaches_the_running_server(world, monkeypatch):
    monkeypatch.setattr(accounts, "REV_RECHECK_SECONDS", 0.0)       # the server looks on every read
    owner_c = world.create_owner()
    session = owner_c.cookies.get(world.main.COOKIE_NAME)
    lan = world.client(LAN)
    assert login(world, lan, OLD).status_code == 200
    _cli(world, "users", "passwd", "Olive", "--password-stdin", stdin="from-the-cli-1\n")
    # No restart: the same process answers with the new account state.
    assert auth.get_session(session) is None                        # the old session died with its epoch
    assert login(world, world.client(LAN), OLD).status_code == 401
    assert login(world, world.client(LAN), "from-the-cli-1").status_code == 200


def test_a_cli_owner_reset_reaches_the_running_server(world, monkeypatch):
    monkeypatch.setattr(accounts, "REV_RECHECK_SECONDS", 0.0)
    world.create_owner()
    lan = world.client(LAN)
    out = _cli(world, "owner", "reset", "--yes").stdout
    token = out.split("SETUP TOKEN:")[1].split()[0]
    assert status(lan)["claimed"] is False
    # the running server accepts the token the other process wrote
    ok = lan.post("/api/auth/setup", json={"password": "fresh-pass-99", "setup_token": token})
    assert ok.status_code == 200, ok.text


def test_a_cli_delete_signs_the_profile_out_of_the_running_server(world, monkeypatch):
    monkeypatch.setattr(accounts, "REV_RECHECK_SECONDS", 0.0)
    owner_c = world.create_owner()
    kit = world.person(owner_c, "Kit", "member", pin="1111")
    kit_c = world.client(LAN)
    assert world.login(kit_c, name="Kit", pin="1111").status_code == 200
    assert kit_c.get("/api/auth/me").json().get("profile", {}).get("id") == kit["id"]
    _cli(world, "users", "delete", "Kit", "--yes")
    assert accounts.get_by_name("Kit") is None
    assert (kit_c.get("/api/auth/me").json().get("profile") or {}).get("id") != kit["id"]


def test_the_profile_cache_follows_another_process(world, monkeypatch):
    import sqlite3
    owner = accounts.create_profile("Olive", "owner", password_hash=credentials.hash_secret(OLD))
    monkeypatch.setattr(accounts, "REV_RECHECK_SECONDS", 0.0)
    assert accounts.get_profile(owner.id).name == "Olive"           # cached
    con = sqlite3.connect(accounts.db_path())
    con.execute("UPDATE profiles SET name = 'Renamed' WHERE id = ?", (owner.id,))
    con.commit()
    assert accounts.get_profile(owner.id).name == "Olive"           # no revision bump: still cached
    con.execute("UPDATE account_rev SET rev = rev + 1")
    con.commit()
    con.close()
    assert accounts.get_profile(owner.id).name == "Renamed"         # bumped: reloaded
    # and a normal write in this process bumps it for everyone else
    before = sqlite3.connect(accounts.db_path()).execute("SELECT rev FROM account_rev").fetchone()[0]
    accounts.update_profile(owner.id, avatar="x.png")
    after = sqlite3.connect(accounts.db_path()).execute("SELECT rev FROM account_rev").fetchone()[0]
    assert after > before


def test_the_cache_is_not_reread_every_call(world):
    """Reads inside the recheck window cost nothing; that is why the window exists."""
    owner = accounts.create_profile("Olive", "owner", password_hash=credentials.hash_secret(OLD))
    assert accounts.REV_RECHECK_SECONDS >= 0.5
    first = accounts._all()
    assert accounts._all() is first and accounts.get_profile(owner.id) is first[owner.id]


def test_remembered_devices_follow_a_deleted_file(world):
    """`trusted-devices.yaml` says "delete this file to forget every device (picked up immediately)":
    a command-line reset in another process relies on it."""
    world.create_owner()
    auth.set_remember_days(30)
    token = auth.issue_session(persistent=True, profile_id=accounts.get_owner().id)
    assert auth.get_session(token) is not None and auth.TRUSTED_PATH.exists()
    auth.TRUSTED_PATH.unlink()                                       # another process wiped it
    assert auth.get_session(token) is None
    assert auth.trusted_count() == 0


def test_owner_reset_is_also_available_to_code(world):
    """`recovery.reset_owner` is what the CLI, the sentinel and the variable share."""
    world.create_owner()
    res = recovery.reset_owner(via="test")
    assert res.had_password and res.had_pin and res.owner_name == "Olive"
    assert not policy.claimed() and auth.verify_setup_token(res.setup_token)
