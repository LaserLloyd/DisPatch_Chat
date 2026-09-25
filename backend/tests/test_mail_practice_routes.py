"""Route-level tests for the Emails (/api/mail/*) and Clients (/api/practice/*)
tabs — FastAPI TestClient with the upstream MailForge/practice services
mocked (no real MailForge/practice process is touched). Run: cd backend &&
uv run pytest.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app import auth, config, mailforge_bridge, main, practice_bridge
from app.database import Database


@pytest.fixture
def route_client(tmp_path, monkeypatch):
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
    auth._sessions.clear()
    auth._cache = None
    auth._fail_count = 0
    auth._fail_until = 0.0

    temp_db = Database(tmp_path / "chats.db")
    monkeypatch.setattr(main, "db", temp_db)
    monkeypatch.setattr(main, "SETTINGS", replace(
        config.SETTINGS, mail_enabled=True, practice_enabled=True,
        mail_data_dir=tmp_path / "mailforge-data",
        practice_pin_file=tmp_path / "practice-pin",
    ))
    practice_bridge.reset_session()

    with TestClient(main.app) as client:
        yield client

    asyncio.run(temp_db.close())


def _unlock(client: TestClient, pin: str = "1234") -> None:
    auth.set_pin(pin)
    r = client.post("/api/auth/unlock", json={"pin": pin})
    assert r.status_code == 200, r.text


# --------------------------------------------------------------------------- #
# Locked / Safe Mode gating — the load-bearing requirement for both tabs.
# --------------------------------------------------------------------------- #


def test_mail_status_403_no_pin(route_client):
    r = route_client.get("/api/mail/status")
    assert r.status_code == 403


def test_mail_status_403_locked(route_client):
    auth.set_pin("1234")
    r = route_client.get("/api/mail/status")
    assert r.status_code == 403


def test_practice_proxy_403_no_pin(route_client):
    r = route_client.get("/api/practice/board")
    assert r.status_code == 403


def test_practice_proxy_403_locked(route_client):
    auth.set_pin("1234")
    r = route_client.get("/api/practice/board")
    assert r.status_code == 403


def test_mail_and_practice_404_when_disabled(route_client, monkeypatch):
    monkeypatch.setattr(main, "SETTINGS", replace(main.SETTINGS, mail_enabled=False, practice_enabled=False))
    _unlock(route_client)
    assert route_client.get("/api/mail/status").status_code == 404
    assert route_client.get("/api/practice/board").status_code == 404


def test_decoy_blocked_prefixes_cover_mail_and_practice():
    assert main._decoy_blocked("GET", "/api/mail/status") is True
    assert main._decoy_blocked("GET", "/api/practice/board") is True
    assert main._decoy_blocked("POST", "/api/practice/clients/c1/actions/build") is True


# --------------------------------------------------------------------------- #
# Emails tab: /api/mail/status
# --------------------------------------------------------------------------- #


def test_mail_status_not_installed(route_client):
    _unlock(route_client)
    r = route_client.get("/api/mail/status")
    assert r.status_code == 200
    body = r.json()
    assert body["installed"] is False
    assert body["reachable"] is False
    assert body["launch_url"] is None


def test_mail_status_installed_and_reachable(route_client, monkeypatch):
    _unlock(route_client)
    data_dir = main.SETTINGS.mail_data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "ui_port").write_text("54321")
    (data_dir / "ui_launcher_key").write_text("SECRETKEY123")

    async def fake_reachable(settings):
        return True

    monkeypatch.setattr(mailforge_bridge, "reachable", fake_reachable)

    # A browser on the host (addressed DisPatch by a loopback name).
    r = route_client.get("/api/mail/status", headers={"Host": "127.0.0.1:8765"})
    assert r.status_code == 200
    body = r.json()
    assert body["installed"] is True
    assert body["reachable"] is True
    assert body["launch_url"] == "http://127.0.0.1:54321/launch?k=SECRETKEY123"
    assert body["remote_launch_url"] is None
    assert body["host_only"] is False


def test_mail_status_installed_but_unreachable_never_returns_launch_url(route_client, monkeypatch):
    """A launch URL for a dead service is worse than none — the pane must not
    hand the browser a key it can't use."""
    _unlock(route_client)
    data_dir = main.SETTINGS.mail_data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "ui_port").write_text("54321")
    (data_dir / "ui_launcher_key").write_text("SECRETKEY123")

    async def fake_unreachable(settings):
        return False

    monkeypatch.setattr(mailforge_bridge, "reachable", fake_unreachable)

    r = route_client.get("/api/mail/status")
    body = r.json()
    assert body["reachable"] is False
    assert body["launch_url"] is None


def test_mail_launcher_key_not_in_health_or_auth_status(route_client):
    """The one place the key intentionally appears is /api/mail/status's own
    launch_url field (once unlocked, once the service is actually up) — it
    must never leak into any OTHER response, including generic health/status
    surfaces available before unlock."""
    data_dir = main.SETTINGS.mail_data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "ui_port").write_text("54321")
    (data_dir / "ui_launcher_key").write_text("SECRETKEY123")

    for path in ("/api/health", "/api/auth/status"):
        r = route_client.get(path)
        assert "SECRETKEY123" not in r.text


# --------------------------------------------------------------------------- #
# Clients tab: /api/practice/* proxy
# --------------------------------------------------------------------------- #


class FakeUpstream:
    """Stands in for httpx against the practice box: proxy() is monkeypatched
    directly rather than mocking sockets, since practice_bridge.proxy() is
    the one seam main.py calls through."""

    def __init__(self, status_code=200, content=b'{"ok": true}', content_type="application/json"):
        self.status_code = status_code
        self.content = content
        self.content_type = content_type
        self.calls = []

    async def __call__(self, settings, method, path, *, query="", body=None, content_type=None):
        self.calls.append((method, path, query, body, content_type))
        return practice_bridge.ProxyResponse(self.status_code, self.content, self.content_type)


def test_practice_proxy_forwards_to_practice_api(route_client, monkeypatch):
    _unlock(route_client)
    fake = FakeUpstream(content=b'{"clients": []}')
    monkeypatch.setattr(practice_bridge, "proxy", fake)

    r = route_client.get("/api/practice/board")
    assert r.status_code == 200
    assert r.json() == {"clients": []}
    assert fake.calls[0][0] == "GET"
    assert fake.calls[0][1] == "board"


def test_practice_proxy_post_body_passthrough(route_client, monkeypatch):
    _unlock(route_client)
    fake = FakeUpstream(content=b'{"job_id": "j1"}')
    monkeypatch.setattr(practice_bridge, "proxy", fake)

    r = route_client.post("/api/practice/clients/c1/actions/build", json={"confirm": True})
    assert r.status_code == 200
    assert r.json() == {"job_id": "j1"}
    method, path, query, body, content_type = fake.calls[0]
    assert method == "POST"
    assert path == "clients/c1/actions/build"
    assert body is not None
    assert b"confirm" in body


def test_practice_proxy_rejects_bad_path(route_client, monkeypatch):
    _unlock(route_client)
    fake = FakeUpstream()
    monkeypatch.setattr(practice_bridge, "proxy", fake)
    # Characters outside the allowlist (spaces, angle brackets) — the id/path
    # regex, not filesystem traversal, is the thing under test here: this
    # proxy never touches a filesystem path, only an upstream JSON API route.
    r = route_client.get("/api/practice/board%20%3Cscript%3E")
    assert r.status_code == 400
    assert fake.calls == []


def test_practice_proxy_upstream_down_is_502(route_client, monkeypatch):
    _unlock(route_client)

    async def raising(*a, **kw):
        raise practice_bridge.UpstreamError("connection refused")

    monkeypatch.setattr(practice_bridge, "proxy", raising)
    r = route_client.get("/api/practice/board")
    assert r.status_code == 502


# --------------------------------------------------------------------------- #
# practice_bridge unit tests: PIN read, session cookie caching, no PIN leak.
# --------------------------------------------------------------------------- #


def test_practice_bridge_read_pin_missing_file(tmp_path, monkeypatch):
    settings = replace(config.SETTINGS, practice_pin_file=tmp_path / "no-such-pin")
    assert practice_bridge._read_pin(settings) is None


def test_practice_bridge_read_pin_present(tmp_path):
    pin_file = tmp_path / "gui-pin"
    pin_file.write_text("293380\n")
    settings = replace(config.SETTINGS, practice_pin_file=pin_file)
    assert practice_bridge._read_pin(settings) == "293380"


def test_mailforge_bridge_launch_url_none_without_runtime_files(tmp_path):
    settings = replace(config.SETTINGS, mail_data_dir=tmp_path / "mf")
    assert mailforge_bridge.launch_url(settings) is None
    assert mailforge_bridge.installed(settings) is False


def _mail_up(monkeypatch):
    data_dir = main.SETTINGS.mail_data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "ui_port").write_text("54321")
    (data_dir / "ui_launcher_key").write_text("SECRETKEY123")

    async def fake_reachable(settings):
        return True

    monkeypatch.setattr(mailforge_bridge, "reachable", fake_reachable)


@pytest.mark.parametrize("host", ["phone-front.tail.example", "192.0.2.37:8765", "[2001:db8::1]:8765"])
def test_mail_status_off_host_gets_no_loopback_key(route_client, monkeypatch, host):
    """MailForge binds loopback and refuses any other Host: a phone (the tailnet
    front door, a LAN IP) can never use the loopback launch URL, so it is not
    handed the key — it gets host_only and the pane says why."""
    _unlock(route_client)
    _mail_up(monkeypatch)
    body = route_client.get("/api/mail/status", headers={"Host": host}).json()
    assert body["reachable"] is True
    assert body["launch_url"] is None
    assert body["remote_launch_url"] is None
    assert body["host_only"] is True


@pytest.mark.parametrize("host", ["localhost:8765", "127.0.0.1", "[::1]:8765"])
def test_mail_status_loopback_names_are_the_host(route_client, monkeypatch, host):
    _unlock(route_client)
    _mail_up(monkeypatch)
    body = route_client.get("/api/mail/status", headers={"Host": host}).json()
    assert body["launch_url"] == "http://127.0.0.1:54321/launch?k=SECRETKEY123"


def test_mail_status_off_host_uses_the_remote_address(route_client, monkeypatch):
    _unlock(route_client)
    _mail_up(monkeypatch)
    monkeypatch.setattr(main, "SETTINGS", replace(
        main.SETTINGS, mail_remote_url="https://host.tailnet.example:8454/"))
    body = route_client.get("/api/mail/status", headers={"Host": "host.tailnet.example"}).json()
    assert body["launch_url"] is None
    assert body["remote_launch_url"] == "https://host.tailnet.example:8454/launch?k=SECRETKEY123"
    assert body["host_only"] is False
    # ...and the host itself still gets the loopback one, never the remote.
    body = route_client.get("/api/mail/status", headers={"Host": "127.0.0.1:8765"}).json()
    assert body["launch_url"].startswith("http://127.0.0.1:54321/")
    assert body["remote_launch_url"] is None
