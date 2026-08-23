"""Remembered-device ("keep this device unlocked") regression tests.

Covers the trusted-device store: opt-in gating, restart survival, idle-timeout
exemption, the sliding day window, every revocation path (lock, PIN change,
forget-all, feature-off), and fail-closed handling of a corrupt store file.

Same hermetic style as the rest of tests/: throwaway data dir + monkeypatched
paths, nothing touches the live data dir. Run: cd backend && uv run pytest.
"""
from __future__ import annotations

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from app import auth, config, main
from app.database import Database

PIN = "1234"


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated data dir + DB (mirrors test_auth_gate's fixture)."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(config, "MEDIA_DIR", tmp_path / "media")
    monkeypatch.setattr(config, "FILES_DIR", tmp_path / "files")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(main, "MEDIA_DIR", tmp_path / "media")
    monkeypatch.setattr(main, "FILES_DIR", tmp_path / "files")
    monkeypatch.setattr(auth, "SECURITY_PATH", tmp_path / "security.yaml")
    monkeypatch.setattr(auth, "RECOVERY_PATH", tmp_path / "RECOVERY-CODE.txt")
    monkeypatch.setattr(auth, "TRUSTED_PATH", tmp_path / "trusted-devices.yaml")
    auth._sessions.clear()
    auth._trusted = None
    auth._cache = None
    auth._fail_count = 0
    auth._fail_until = 0.0
    config._invalidate_bots_cache()

    temp_db = Database(tmp_path / "chats.db")
    monkeypatch.setattr(main, "db", temp_db)
    main._ACK_SEEN.clear()
    main._delivered.clear()
    main._thread_bot.clear()

    clients: list[TestClient] = []

    def make_client() -> TestClient:
        c = TestClient(main.app)
        c.__enter__()
        clients.append(c)
        return c

    yield make_client

    for c in clients:
        c.__exit__(None, None, None)
    asyncio.run(temp_db.close())


def _enable(days: int = 30) -> None:
    auth.set_remember_days(days)


def _simulate_restart() -> None:
    """A server restart = in-memory sessions gone, disk state intact."""
    auth._sessions.clear()
    auth._trusted = None          # forces a reload from TRUSTED_PATH
    auth._cache = None


def _unlock(client: TestClient, remember: bool):
    return client.post("/api/auth/unlock", json={"pin": PIN, "remember": remember})


# --------------------------------------------------------------------------- #
# Opt-in gating
# --------------------------------------------------------------------------- #


def test_feature_off_by_default_remember_is_ignored(env):
    client = env()
    auth.set_pin(PIN)
    r = _unlock(client, remember=True)
    assert r.status_code == 200
    assert r.json()["remembered"] is False
    assert "Max-Age" not in (r.headers.get("set-cookie") or "")
    assert not auth.TRUSTED_PATH.exists()
    # ...and a restart logs the device out, as ever.
    _simulate_restart()
    assert client.get("/api/auth/status").json()["authenticated"] is False


def test_remember_config_needs_session_once_pin_set(env):
    client = env()
    auth.set_pin(PIN)
    assert client.post("/api/auth/remember-config", json={"days": 30}).status_code == 403
    _unlock(client, remember=False)
    r = client.post("/api/auth/remember-config", json={"days": 30})
    assert r.status_code == 200 and r.json()["remember_days"] == 30
    # Persisted in security.yaml (and survives a config-cache bust).
    assert "remember_device_days: 30" in auth.SECURITY_PATH.read_text()
    assert client.post("/api/auth/remember-config", json={"days": 9999}).status_code == 400


def test_unchecked_box_stays_ephemeral_even_when_enabled(env):
    client = env()
    auth.set_pin(PIN)
    _enable()
    r = _unlock(client, remember=False)
    assert r.json()["remembered"] is False
    _simulate_restart()
    assert client.get("/api/auth/status").json()["authenticated"] is False


# --------------------------------------------------------------------------- #
# The remembered path
# --------------------------------------------------------------------------- #


def test_remembered_device_survives_restart(env):
    client = env()
    auth.set_pin(PIN)
    _enable()
    r = _unlock(client, remember=True)
    assert r.status_code == 200 and r.json()["remembered"] is True
    assert "Max-Age" in (r.headers.get("set-cookie") or "")
    s = client.get("/api/auth/status").json()
    assert s["authenticated"] and s["remembered"] and s["trusted_devices"] == 1

    _simulate_restart()
    s = client.get("/api/auth/status").json()
    assert s["authenticated"] is True, "cookie should re-mint the session"
    assert s["remembered"] is True
    # Full-access route really works after the restart, not just status.
    assert client.get("/api/bots/all").status_code == 200


def test_store_holds_hashes_never_the_token(env):
    client = env()
    auth.set_pin(PIN)
    _enable()
    _unlock(client, remember=True)
    token = client.cookies.get(main.COOKIE_NAME)
    assert token and auth.TRUSTED_PATH.exists()
    content = auth.TRUSTED_PATH.read_text()
    assert token not in content
    assert auth._token_hash(token) in content
    assert (auth.TRUSTED_PATH.stat().st_mode & 0o777) == 0o600


def test_persistent_session_skips_idle_timeout(env):
    client = env()
    auth.set_pin(PIN)
    _enable()
    _unlock(client, remember=True)
    token = client.cookies.get(main.COOKIE_NAME)
    # Age the in-memory session far past the idle window.
    auth._sessions[token].last_seen = time.monotonic() - 10 * auth.lock_timeout()
    assert auth.get_session(token) is not None
    assert client.get("/api/bots/all").status_code == 200


def test_sliding_window_expiry_forgets_the_device(env):
    client = env()
    auth.set_pin(PIN)
    _enable(days=30)
    _unlock(client, remember=True)
    token = client.cookies.get(main.COOKIE_NAME)
    # Device unseen for > 30 days → trust lapses (and the session with it).
    auth._trusted_store()[auth._token_hash(token)]["last_seen"] = time.time() - 31 * 86400
    assert auth.get_session(token) is None
    assert client.get("/api/auth/status").json()["authenticated"] is False


# --------------------------------------------------------------------------- #
# Revocation paths
# --------------------------------------------------------------------------- #


def test_lock_forgets_the_device(env):
    client = env()
    auth.set_pin(PIN)
    _enable()
    _unlock(client, remember=True)
    assert client.post("/api/auth/lock").status_code == 200
    assert auth.trusted_count() == 0
    _simulate_restart()
    assert client.get("/api/auth/status").json()["authenticated"] is False


def test_pin_change_forgets_all_devices(env):
    client = env()
    auth.set_pin(PIN)
    _enable()
    _unlock(client, remember=True)
    assert auth.trusted_count() == 1
    auth.set_pin("5678")
    assert auth.trusted_count() == 0
    assert not auth.TRUSTED_PATH.exists()


def test_forget_all_kills_others_demotes_caller(env):
    # One TestClient plays two devices (two live clients fight over event
    # loops); "switching device" = swapping the cookie jar.
    client = env()
    auth.set_pin(PIN)
    _enable()
    _unlock(client, remember=True)
    phone_token = client.cookies.get(main.COOKIE_NAME)
    client.cookies.clear()
    _unlock(client, remember=True)               # now acting as the desk
    desk_token = client.cookies.get(main.COOKIE_NAME)
    assert auth.trusted_count() == 2 and phone_token != desk_token

    r = client.post("/api/auth/forget-devices")  # called from the desk
    assert r.status_code == 200
    assert auth.trusted_count() == 0
    # The other device is out cold; the caller keeps a now-ephemeral session.
    client.cookies.set(main.COOKIE_NAME, phone_token)
    assert client.get("/api/auth/status").json()["authenticated"] is False
    client.cookies.set(main.COOKIE_NAME, desk_token)
    s = client.get("/api/auth/status").json()
    assert s["authenticated"] is True and s["remembered"] is False
    # ...which no longer survives a restart.
    _simulate_restart()
    assert client.get("/api/auth/status").json()["authenticated"] is False


def test_forget_devices_requires_session(env):
    client = env()
    auth.set_pin(PIN)
    assert client.post("/api/auth/forget-devices").status_code == 403


def test_disabling_feature_demotes_not_drops(env):
    client = env()
    auth.set_pin(PIN)
    _enable()
    _unlock(client, remember=True)
    r = client.post("/api/auth/remember-config", json={"days": 0})
    assert r.status_code == 200
    assert auth.trusted_count() == 0 and not auth.TRUSTED_PATH.exists()
    # Still unlocked right now (no rug-pull), but back to ephemeral rules.
    s = client.get("/api/auth/status").json()
    assert s["authenticated"] is True and s["remembered"] is False
    _simulate_restart()
    assert client.get("/api/auth/status").json()["authenticated"] is False


# --------------------------------------------------------------------------- #
# Fail-closed + information exposure
# --------------------------------------------------------------------------- #


def test_corrupt_trusted_file_fails_closed_to_pin(env):
    client = env()
    auth.set_pin(PIN)
    _enable()
    _unlock(client, remember=True)
    _simulate_restart()
    auth.TRUSTED_PATH.write_text("]] not: yaml: at: all [[")
    # The remembered cookie is not honoured...
    assert client.get("/api/auth/status").json()["authenticated"] is False
    # ...but the PIN path still works fine.
    assert _unlock(client, remember=False).status_code == 200
    assert client.get("/api/auth/status").json()["authenticated"] is True


def test_decoy_status_reveals_offer_but_not_device_count(env):
    client = env()
    auth.set_pin(PIN)
    _enable()
    _unlock(client, remember=True)
    client.cookies.clear()                       # same box, now an anonymous snoop
    s = client.get("/api/auth/status").json()
    assert s["remember_days"] == 30          # needed to render the checkbox
    assert s["trusted_devices"] == 0         # count is unlocked-only
    assert s["remembered"] is False
