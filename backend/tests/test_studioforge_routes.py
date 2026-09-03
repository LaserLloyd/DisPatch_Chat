"""Route-level tests for /api/studioforge/status — the read-only embed of the
LLM rig's own control panel.

The panel itself is a different machine and has no authentication of its own,
so everything worth testing here is a GATE: off by default, 404 when
unconfigured, 403 without a PIN, 403 in Safe Mode. The reachability probe is
mocked in every test — no test may touch the network, let alone the rig.

Mirrors test_harness_routes.py. Run: cd backend && uv run pytest.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app import auth, config, main
from app.database import Database

PANEL_URL = "http://198.51.100.7:8080"     # TEST-NET-2; never resolved here.
# Captured before the autouse stub below can replace it, so the probe's own
# behaviour can still be exercised directly.
_REAL_PROBE = main._studioforge_reachable


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
        config.SETTINGS, studioforge_enabled=True, studioforge_url=PANEL_URL))

    with TestClient(main.app) as client:
        yield client

    asyncio.run(temp_db.close())


@pytest.fixture(autouse=True)
def no_real_probe(monkeypatch):
    """Nothing in this file may open a socket. Every test gets a stub probe and
    a cleared cache, so a cached answer never leaks between tests."""
    monkeypatch.setattr(main, "_sf_probe", None)
    calls = []

    async def probe():
        calls.append(1)
        return True, 1_700_000_000.0

    monkeypatch.setattr(main, "_studioforge_reachable", probe)
    return calls


def _unlock(client: TestClient, pin: str = "1234") -> None:
    auth.set_pin(pin)
    r = client.post("/api/auth/unlock", json={"pin": pin})
    assert r.status_code == 200, r.text


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #

def test_status_returns_url_and_reachability(route_client, no_real_probe):
    _unlock(route_client)
    r = route_client.get("/api/studioforge/status")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["url"] == PANEL_URL
    assert body["reachable"] is True
    assert isinstance(body["checked_at"], (int, float))
    assert len(no_real_probe) == 1


def test_features_advertise_studioforge_when_enabled(route_client):
    _unlock(route_client)
    assert route_client.get("/api/auth/status").json()["features"]["studioforge"] is True


# --------------------------------------------------------------------------- #
# Gates
# --------------------------------------------------------------------------- #

def test_feature_off_is_404(route_client, monkeypatch, no_real_probe):
    _unlock(route_client)
    monkeypatch.setattr(main, "SETTINGS", replace(main.SETTINGS, studioforge_enabled=False))
    assert route_client.get("/api/studioforge/status").status_code == 404
    assert route_client.get("/api/auth/status").json()["features"]["studioforge"] is False
    assert no_real_probe == []


def test_empty_url_is_404_even_with_the_flag_on(route_client, monkeypatch, no_real_probe):
    """The flag alone cannot turn the feature on: with no address there is
    nothing to frame, and guessing one is not on the table."""
    _unlock(route_client)
    monkeypatch.setattr(main, "SETTINGS", replace(main.SETTINGS, studioforge_url=""))
    assert route_client.get("/api/studioforge/status").status_code == 404
    assert route_client.get("/api/auth/status").json()["features"]["studioforge"] is False
    assert no_real_probe == []


def test_no_pin_set_is_403(route_client, no_real_probe):
    """The panel has no password of its own, so DisPatch's PIN is the only
    thing gating the rig's admin UI. No PIN → the feature stays shut."""
    r = route_client.get("/api/studioforge/status")
    assert r.status_code == 403, r.text
    assert "PIN" in r.json()["detail"]
    assert route_client.get("/api/auth/status").json()["features"]["studioforge"] is False
    assert no_real_probe == []


def test_safe_mode_gets_403(route_client, no_real_probe):
    auth.set_pin("1234")            # a PIN exists, this client never unlocks
    r = route_client.get("/api/studioforge/status")
    assert r.status_code == 403, r.text
    assert no_real_probe == []


def test_locking_shuts_the_route_again(route_client, no_real_probe):
    _unlock(route_client)
    assert route_client.get("/api/studioforge/status").status_code == 200
    route_client.post("/api/auth/lock")
    assert route_client.get("/api/studioforge/status").status_code == 403


def test_prefix_is_in_the_full_session_deny_list():
    """Second, independent gate: the Safe-Mode path check refuses the whole
    /api/studioforge prefix regardless of what the route handler does."""
    assert main._decoy_blocked("GET", "/api/studioforge/status") is True


# --------------------------------------------------------------------------- #
# URL validation (config-time, before anything is ever framed)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("raw", [
    "", "   ",
    "ftp://host:8080",                      # wrong scheme
    "file:///etc/passwd",
    "javascript:alert(1)",
    "http://",                              # no host
    "http://user:pw@host:8080",             # credentials would land in the DOM
    "https://:pw@host",
])
def test_bad_urls_disable_the_feature(raw):
    assert config._studioforge_url(raw) == ""


@pytest.mark.parametrize("raw", [
    "http://198.51.100.7:8080",
    "https://panel.example.ts.net:8080/",
    "  http://198.51.100.7:8080  ",         # trimmed, still fine
])
def test_good_urls_survive(raw):
    assert config._studioforge_url(raw) == raw.strip()


# --------------------------------------------------------------------------- #
# The probe itself (the one piece of real logic): any HTTP answer is
# "reachable", a transport failure is not, and the answer is cached.
# --------------------------------------------------------------------------- #

class _FakeClient:
    """Stands in for httpx.AsyncClient. Records every URL it is asked for so a
    test can assert the probe is a single plain GET of the panel and nothing
    else -- no inference port, no /api/*, no POST."""

    def __init__(self, seen, raiser=None, **kw):
        self._seen = seen
        self._raiser = raiser

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, **kw):
        self._seen.append(("GET", url))
        if self._raiser:
            raise self._raiser
        return object()          # status code is deliberately never inspected


@pytest.mark.parametrize("raiser,expected", [
    (None, True),
    (OSError("connection refused"), False),
])
def test_probe_reachability_and_cache(monkeypatch, raiser, expected):
    monkeypatch.setattr(main, "SETTINGS", replace(
        config.SETTINGS, studioforge_enabled=True, studioforge_url=PANEL_URL))
    monkeypatch.setattr(main, "_sf_probe", None)
    seen = []
    monkeypatch.setattr(main.httpx, "AsyncClient",
                        lambda **kw: _FakeClient(seen, raiser, **kw))

    ok, at = asyncio.run(_REAL_PROBE())
    assert ok is expected
    assert seen == [("GET", PANEL_URL)]

    # Second call inside the TTL reuses the cached answer: a repainting client
    # must not turn this into a poll of somebody else's machine.
    ok2, at2 = asyncio.run(_REAL_PROBE())
    assert (ok2, at2) == (ok, at)
    assert len(seen) == 1


# --------------------------------------------------------------------------- #
# The shipped CSP blocks the client-side reachability probe, so index() splices
# the configured ORIGIN into connect-src. Widening by exactly one origin, only
# when the feature is on.
# --------------------------------------------------------------------------- #

def test_index_widens_connect_src_to_the_panel_origin(route_client):
    html = route_client.get("/").text
    assert "connect-src 'self' ws: wss: http://198.51.100.7:8080;" in html
    # The ORIGIN, never the full URL, and nothing else moved.
    assert main._CSP_CONNECT_SRC not in html
    assert "frame-src 'self' http: https:;" in html


def test_index_is_byte_identical_when_the_feature_is_off(route_client, monkeypatch):
    """An install that never configured this must get the shipped file exactly
    as it ships -- no runtime CSP edit, no widened directive."""
    monkeypatch.setattr(main, "SETTINGS", replace(main.SETTINGS, studioforge_enabled=False))
    html = route_client.get("/").text
    assert main._CSP_CONNECT_SRC in html
    assert "198.51.100.7" not in html
    assert html == (config.FRONTEND_DIR / "index.html").read_text(encoding="utf-8")


def test_index_widening_is_not_gated_on_a_session(route_client):
    """index() runs before anyone has unlocked, so the origin is in the policy
    for a Safe-Mode tab too. That is the CSP, not an affordance: the rail
    button is still absent and every route still 403s -- a locked device gains
    the ability to connect to an address it has no way to learn or use."""
    html = route_client.get("/").text
    assert "198.51.100.7:8080" in html
    assert route_client.get("/api/studioforge/status").status_code in (403, 404)


@pytest.mark.parametrize("url,origin", [
    ("http://198.51.100.7:8080", "http://198.51.100.7:8080"),
    ("https://panel.example.ts.net:8080/some/path", "https://panel.example.ts.net:8080"),
])
def test_origin_is_scheme_host_port_only(monkeypatch, url, origin):
    monkeypatch.setattr(main, "SETTINGS", replace(
        config.SETTINGS, studioforge_enabled=True, studioforge_url=url))
    assert main._studioforge_origin() == origin


def test_no_origin_without_the_flag(monkeypatch):
    monkeypatch.setattr(main, "SETTINGS", replace(
        config.SETTINGS, studioforge_enabled=False, studioforge_url="http://198.51.100.7:8080"))
    assert main._studioforge_origin() == ""
