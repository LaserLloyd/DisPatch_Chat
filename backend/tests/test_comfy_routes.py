"""Route-level tests for /api/comfy/service/* — FastAPI TestClient, with
comfy_service's systemctl/tailscale calls mocked (never touches the real
comfyui.service or tailscaled). Run: cd backend && uv run pytest.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app import auth, config, main
from app import comfy_service as cs
from app.database import Database


@pytest.fixture
def route_client(tmp_path, monkeypatch):
    """Full isolation from the live data dir / DB / security.yaml / comfy.env,
    with the feature flag forced on. comfy_service still talks to the real
    ~/comfy dir unless a test also monkeypatches COMFY_DIR/COMFY_ENV_PATH —
    tests that don't want that MUST mock the relevant comfy_service function.
    """
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
    monkeypatch.setattr(main, "SETTINGS", replace(config.SETTINGS, comfy_enabled=True))

    cs._svc_lock = asyncio.Lock()
    cs._hostname_cache = None
    # The aggregate-status micro-cache is module-global; a body cached by one
    # test must never satisfy the next test's differently-mocked request.
    main._comfy_status_cache.update(ts=0.0, body=None)

    with TestClient(main.app) as client:
        yield client

    asyncio.run(temp_db.close())


def _unlock(client: TestClient, pin: str = "1234") -> None:
    auth.set_pin(pin)
    r = client.post("/api/auth/unlock", json={"pin": pin})
    assert r.status_code == 200, r.text


COMFY_ROUTES = [
    ("GET", "/api/comfy/service/status", None),
    ("POST", "/api/comfy/service/start", None),
    ("POST", "/api/comfy/service/stop", None),
    ("POST", "/api/comfy/service/restart", None),
    ("POST", "/api/comfy/service/launch", None),
    ("GET", "/api/comfy/service/flags", None),
    ("PUT", "/api/comfy/service/flags", {"values": {}}),
    ("POST", "/api/comfy/service/gateway", {"on": False}),
    ("GET", "/api/comfy/service/logs", None),
]


# --------------------------------------------------------------------------- #
# 403 for decoy/Safe-Mode on every route
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("method,path,body", COMFY_ROUTES)
def test_decoy_session_gets_403_on_every_route(route_client, method, path, body):
    auth.set_pin("1234")   # PIN set, but this client never unlocks -> decoy
    r = route_client.request(method, path, json=body)
    assert r.status_code == 403, f"{method} {path} -> {r.status_code}"


def test_no_pin_set_is_full_access(route_client, monkeypatch):
    """No PIN configured at all -> the whole app (incl. ComfyUI) is open."""
    monkeypatch.setattr(cs, "unit_state", _async(return_value={"active_state": "active",
                         "sub_state": "running", "start_timestamp": None, "n_restarts": 0}))
    monkeypatch.setattr(cs, "health", _async(return_value={"system": {}}))
    monkeypatch.setattr(cs, "gateway_status", _async(return_value={"on": False, "url": None}))
    monkeypatch.setattr(cs, "flags_dirty", _async(return_value=False))
    monkeypatch.setattr(cs, "start_timestamp_epoch", _async(return_value=1751000000.0))
    r = route_client.get("/api/comfy/service/status")
    assert r.status_code == 200
    body = r.json()
    # Contract for the frontend: machine-readable start time + dirty-flags flag.
    assert body["unit"]["start_epoch"] == 1751000000
    assert body["flags_dirty"] is False


def test_feature_disabled_404s(route_client, monkeypatch):
    monkeypatch.setattr(main, "SETTINGS", replace(config.SETTINGS, comfy_enabled=False))
    r = route_client.get("/api/comfy/service/status")
    assert r.status_code == 404


# --------------------------------------------------------------------------- #
# Typed error -> HTTP status mapping
# --------------------------------------------------------------------------- #


def _async(return_value=None, side_effect=None):
    async def fn(*args, **kwargs):
        if side_effect is not None:
            raise side_effect
        return return_value
    return fn


def test_busy_error_maps_to_409(route_client, monkeypatch):
    _unlock(route_client)
    monkeypatch.setattr(cs, "start", _async(side_effect=cs.ServiceBusyError()))
    r = route_client.post("/api/comfy/service/start")
    assert r.status_code == 409


def test_tailscale_unavailable_maps_to_503(route_client, monkeypatch):
    _unlock(route_client)
    monkeypatch.setattr(cs, "gateway_on", _async(side_effect=cs.TailscaleUnavailableError("no operator")))
    r = route_client.post("/api/comfy/service/gateway", json={"on": True})
    assert r.status_code == 503
    assert "operator" in r.text.lower() or "no operator" in r.text.lower()


def test_health_timeout_maps_to_504_with_journal(route_client, monkeypatch):
    _unlock(route_client)
    monkeypatch.setattr(cs, "start", _async(side_effect=cs.HealthTimeoutError("journal tail here")))
    r = route_client.post("/api/comfy/service/start")
    assert r.status_code == 504
    assert "journal tail here" in r.text


def test_generic_service_error_maps_to_502(route_client, monkeypatch):
    _unlock(route_client)
    monkeypatch.setattr(cs, "start", _async(side_effect=cs.ServiceError("systemctl exploded")))
    r = route_client.post("/api/comfy/service/start")
    assert r.status_code == 502


def test_flag_validation_error_maps_to_422(route_client, monkeypatch):
    _unlock(route_client)
    r = route_client.put("/api/comfy/service/flags", json={"values": {"nope": 1}})
    assert r.status_code == 422


# --------------------------------------------------------------------------- #
# Flags PUT round-trips (real write_flags/read_flags, scratch comfy.env)
# --------------------------------------------------------------------------- #


@pytest.fixture
def scratch_comfy_env(tmp_path, monkeypatch):
    env_path = tmp_path / "comfy.env"
    env_path.write_text("COMFY_PORT=8188\nCOMFY_ARGS=\n")
    monkeypatch.setattr(cs, "COMFY_DIR", tmp_path)
    monkeypatch.setattr(cs, "COMFY_ENV_PATH", env_path)
    return env_path


def test_flags_put_round_trips_without_restart(route_client, scratch_comfy_env, monkeypatch):
    _unlock(route_client)
    r = route_client.put("/api/comfy/service/flags", json={"values": {"disable_mmap": True}, "restart": False})
    assert r.status_code == 200
    assert r.json()["values"]["disable_mmap"] is True

    r2 = route_client.get("/api/comfy/service/flags")
    assert r2.status_code == 200
    assert r2.json()["values"]["disable_mmap"] is True
    assert "known_good" in r2.json()


def test_flags_put_with_restart_calls_restart_and_regateways(route_client, scratch_comfy_env, monkeypatch):
    _unlock(route_client)
    calls = []
    monkeypatch.setattr(cs, "gateway_status", _async(return_value={"on": True, "url": "https://x:8444/"}))
    monkeypatch.setattr(cs, "restart", lambda: calls.append("restart") or _restart_result())
    monkeypatch.setattr(cs, "gateway_on", lambda comfy_port=None: calls.append("gateway_on") or _gateway_url())

    r = route_client.put("/api/comfy/service/flags", json={"values": {"bf16_vae": True}, "restart": True})
    assert r.status_code == 200
    assert calls == ["restart", "gateway_on"], calls


async def _restart_result():
    return {"healthy": True, "stats": {}}


async def _gateway_url():
    return "https://x:8444/"


# --------------------------------------------------------------------------- #
# Redaction: comfy_service WS frames never carry the gateway URL
# --------------------------------------------------------------------------- #


def test_redact_for_decoy_passes_comfy_service_frame_through_unchanged():
    frame = {"type": "comfy_service", "state": "running", "gateway": {"on": True}}
    assert main.redact_for_decoy(frame) == frame


def test_broadcast_comfy_state_never_includes_a_url(route_client, monkeypatch):
    _unlock(route_client)
    monkeypatch.setattr(cs, "gateway_status", _async(return_value={"on": True, "url": "https://secret:8444/"}))
    monkeypatch.setattr(cs, "gateway_on", _async(return_value="https://secret:8444/"))

    captured = []

    async def fake_broadcast(frame):
        captured.append(frame)
    monkeypatch.setattr(main.manager, "broadcast", fake_broadcast)

    r = route_client.post("/api/comfy/service/gateway", json={"on": True})
    assert r.status_code == 200
    assert captured, "expected a comfy_service broadcast"
    for frame in captured:
        assert "url" not in frame.get("gateway", {}), frame
        assert "https://secret" not in str(frame)


def test_status_survives_tailscale_down_with_gateway_error(route_client, monkeypatch):
    """tailscaled being down must not 503 the whole panel — service control
    still works without the gateway; status reports it as unavailable."""
    _unlock(route_client)
    monkeypatch.setattr(cs, "unit_state", _async(return_value={
        "active_state": "active", "sub_state": "running",
        "start_timestamp": None, "n_restarts": 0}))
    monkeypatch.setattr(cs, "health", _async(return_value={"system": {}}))
    monkeypatch.setattr(cs, "flags_dirty", _async(return_value=False))
    monkeypatch.setattr(cs, "start_timestamp_epoch", _async(return_value=None))
    monkeypatch.setattr(cs, "gateway_status",
                        _async(side_effect=cs.TailscaleUnavailableError("tailscaled not running")))
    r = route_client.get("/api/comfy/service/status")
    assert r.status_code == 200
    body = r.json()
    gw = body["gateway"]
    assert gw["on"] is False and "error" in gw
    assert body["unit"]["start_epoch"] is None


def test_status_micro_cache_collapses_probes(route_client, monkeypatch):
    """Repeated polls inside the TTL serve the cached body (one probe set for
    N tabs); a state broadcast (any mutation) invalidates immediately."""
    calls = {"n": 0}

    async def counting_unit_state():
        calls["n"] += 1
        return {"active_state": "active", "sub_state": "running",
                "start_timestamp": None, "n_restarts": 0}

    monkeypatch.setattr(cs, "unit_state", counting_unit_state)
    monkeypatch.setattr(cs, "health", _async(return_value={"system": {}}))
    monkeypatch.setattr(cs, "gateway_status", _async(return_value={"on": False, "url": None}))
    monkeypatch.setattr(cs, "flags_dirty", _async(return_value=False))
    monkeypatch.setattr(cs, "start_timestamp_epoch", _async(return_value=1751000000.0))
    monkeypatch.setattr(auth, "load", lambda: auth.SecurityConfig())  # no PIN -> full access

    assert route_client.get("/api/comfy/service/status").status_code == 200
    assert route_client.get("/api/comfy/service/status").status_code == 200
    assert route_client.get("/api/comfy/service/status").status_code == 200
    assert calls["n"] == 1                       # two hits served from cache

    main._comfy_status_cache.update(ts=0.0, body=None)   # what a mutation does
    assert route_client.get("/api/comfy/service/status").status_code == 200
    assert calls["n"] == 2
