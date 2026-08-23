"""Route-level tests for /api/harness/* — FastAPI TestClient with systemctl /
health / the job runner mocked (never touches systemd or spawns dsh). Mirrors
test_terminal_routes.py. Run: cd backend && uv run pytest.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app import auth, config, harness, main
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
    monkeypatch.setattr(main, "SETTINGS", replace(config.SETTINGS, harness_enabled=True))
    # dsh's home → tmp so the model routes never touch the real ~/.dsh.
    monkeypatch.setenv("DSH_HOME", str(tmp_path / "dsh-home"))

    with TestClient(main.app) as client:
        yield client

    asyncio.run(temp_db.close())


def _unlock(client: TestClient, pin: str = "1234") -> None:
    auth.set_pin(pin)
    r = client.post("/api/auth/unlock", json={"pin": pin})
    assert r.status_code == 200, r.text


class FakeRunner:
    def __init__(self):
        self.calls = []
        self._running = False
        self._jobs = []

    def status(self):
        return {"running": self._running, "current": None, "history": []}

    def jobs(self):
        return list(self._jobs)

    def job(self, job_id):
        for j in self._jobs:
            if j["id"] == job_id:
                return j
        return None

    async def submit(self, task, cwd):
        if self._running:
            raise harness.HarnessBusyError("a job is already running")
        self.calls.append(("submit", task, str(cwd)))
        self._running = True
        j = {"id": len(self._jobs) + 1, "task": task, "cwd": str(cwd), "state": "running",
             "output": "", "error": ""}
        self._jobs.append(j)
        return j

    async def cancel(self):
        if not self._running:
            raise harness.HarnessError("no job is running")
        self.calls.append(("cancel",))
        self._running = False
        return {"state": "cancelled"}

    def add_state_hook(self, h): pass
    def remove_state_hook(self, h): pass
    async def shutdown(self): pass


@pytest.fixture
def fake_svc(monkeypatch):
    """Mock everything that would touch systemd, the network or a real binary."""
    calls = []

    async def unit_state(unit=harness.UNIT):
        return {"unit": unit, "active_state": "active", "sub_state": "running",
                "start_timestamp": None, "n_restarts": 0, "unit_file_state": "enabled"}

    async def health(port=harness.DEFAULT_PORT):
        return True

    async def start(unit, port):
        calls.append(("start", unit, port)); return await unit_state(unit)

    async def stop(unit):
        calls.append(("stop", unit)); return await unit_state(unit)

    async def restart(unit, port):
        calls.append(("restart", unit, port)); return await unit_state(unit)

    monkeypatch.setattr(harness, "unit_state", unit_state)
    monkeypatch.setattr(harness, "health", health)
    monkeypatch.setattr(harness, "start", start)
    monkeypatch.setattr(harness, "stop", stop)
    monkeypatch.setattr(harness, "restart", restart)
    monkeypatch.setattr(harness, "resolve_binary", lambda: "/fake/dsh")
    fake = FakeRunner()
    monkeypatch.setattr(harness, "runner", fake)
    return {"calls": calls, "runner": fake}


HARNESS_ROUTES = [
    ("GET", "/api/harness/status"),
    ("POST", "/api/harness/start"),
    ("POST", "/api/harness/stop"),
    ("POST", "/api/harness/restart"),
    ("GET", "/api/harness/models"),
    ("POST", "/api/harness/model"),
    ("GET", "/api/harness/jobs"),
    ("POST", "/api/harness/jobs"),
    ("POST", "/api/harness/jobs/cancel"),
    ("GET", "/api/harness/jobs/1"),
]


# --------------------------------------------------------------------------- #
# Full-session happy paths
# --------------------------------------------------------------------------- #

def test_status_and_service_ops(route_client, fake_svc):
    _unlock(route_client)
    r = route_client.get("/api/harness/status")
    assert r.status_code == 200, r.text
    st = r.json()
    assert st["installed"] is True and st["healthy"] is True
    assert st["unit"]["active_state"] == "active"
    assert st["url"].startswith("http://127.0.0.1:")
    assert st["models"]["providers"][0]["id"] == "deepseek-official"
    assert st["jobs"]["running"] is False
    for op in ("start", "stop", "restart"):
        r = route_client.post(f"/api/harness/{op}")
        assert r.status_code == 200, r.text
    assert [c[0] for c in fake_svc["calls"]] == ["start", "stop", "restart"]
    # The unit + port from SETTINGS reached the service layer.
    assert fake_svc["calls"][0][1:] == (main.SETTINGS.harness_unit, main.SETTINGS.harness_port)


def test_model_get_set_round_trip(route_client, fake_svc, tmp_path):
    _unlock(route_client)
    r = route_client.get("/api/harness/models")
    assert r.status_code == 200
    assert r.json()["current"] is None
    r = route_client.post("/api/harness/model", json={"provider": "deepseek-official", "model": "deepseek-v4-pro"})
    assert r.status_code == 200, r.text
    assert r.json()["current"] == {"provider": "deepseek-official", "model": "deepseek-v4-pro"}
    assert (tmp_path / "dsh-home" / "settings.yaml").exists()
    assert route_client.get("/api/harness/models").json()["current"]["model"] == "deepseek-v4-pro"
    # Validation → 422, never a write.
    r = route_client.post("/api/harness/model", json={"provider": "x y", "model": "m"})
    assert r.status_code == 422
    r = route_client.post("/api/harness/model", json=[1, 2])
    assert r.status_code == 422


def test_jobs_submit_list_cancel(route_client, fake_svc, tmp_path, monkeypatch):
    _unlock(route_client)
    home = tmp_path / "home"; (home / "p").mkdir(parents=True)
    monkeypatch.setattr(harness.Path, "home", staticmethod(lambda: home))
    r = route_client.post("/api/harness/jobs", json={"task": "  hello ", "cwd": str(home / "p")})
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "running"
    assert fake_svc["runner"].calls[-1] == ("submit", "hello", str((home / "p").resolve()))
    # Busy → 409
    assert route_client.post("/api/harness/jobs", json={"task": "again"}).status_code == 409
    assert route_client.get("/api/harness/jobs").json()["jobs"][0]["task"] == "hello"
    assert route_client.get("/api/harness/jobs/1").status_code == 200
    assert route_client.get("/api/harness/jobs/99").status_code == 404
    assert route_client.post("/api/harness/jobs/cancel").status_code == 200
    # Nothing running → 502-class error, not a crash.
    assert route_client.post("/api/harness/jobs/cancel").status_code == 502
    # Validation: empty task / cwd outside home → 422, runner never called.
    n = len(fake_svc["runner"].calls)
    assert route_client.post("/api/harness/jobs", json={"task": "   "}).status_code == 422
    assert route_client.post("/api/harness/jobs", json={"task": "x", "cwd": str(tmp_path)}).status_code == 422
    assert route_client.post("/api/harness/jobs", json="nope").status_code == 422
    assert len(fake_svc["runner"].calls) == n


# --------------------------------------------------------------------------- #
# Gates: Safe Mode (decoy) 403 everywhere; disabled → 404; feature flag
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("method,path", HARNESS_ROUTES)
def test_safe_mode_gets_403_everywhere(route_client, fake_svc, method, path):
    auth.set_pin("1234")            # a PIN exists, no session → Safe Mode
    r = route_client.request(method, path, json={} if method == "POST" else None)
    assert r.status_code == 403, (path, r.status_code, r.text)
    assert fake_svc["calls"] == [] and fake_svc["runner"].calls == []


@pytest.mark.parametrize("method,path", HARNESS_ROUTES)
def test_disabled_flag_404s_everything(route_client, fake_svc, monkeypatch, method, path):
    _unlock(route_client)
    monkeypatch.setattr(main, "SETTINGS", replace(main.SETTINGS, harness_enabled=False))
    r = route_client.request(method, path, json={} if method == "POST" else None)
    assert r.status_code == 404, (path, r.status_code)


@pytest.mark.parametrize("method,path", HARNESS_ROUTES)
def test_no_pin_set_makes_the_harness_unavailable(route_client, fake_svc,
                                                  method, path):
    """A headless job is code execution. Without a PIN there is no session to
    hold, so the whole surface stays 403 rather than open to the LAN."""
    r = route_client.request(method, path, json={} if method == "POST" else None)
    assert r.status_code == 403, (path, r.status_code, r.text)
    assert "PIN" in r.json()["detail"]
    assert fake_svc["calls"] == [] and fake_svc["runner"].calls == []


def test_no_pin_set_reports_the_harness_as_off(route_client, fake_svc):
    assert route_client.get("/api/auth/status").json()["features"]["harness"] is False


def test_features_advertise_harness_flag(route_client, fake_svc):
    _unlock(route_client)
    r = route_client.get("/api/auth/status")
    assert r.status_code == 200
    assert r.json()["features"]["harness"] is True


def test_not_installed_is_reported_not_500(route_client, fake_svc, monkeypatch):
    _unlock(route_client)
    monkeypatch.setattr(harness, "resolve_binary", lambda: None)
    r = route_client.get("/api/harness/status")
    assert r.status_code == 200
    assert r.json()["installed"] is False


def test_redactor_drops_harness_state_frames():
    assert main.redact_for_decoy({"type": "harness_state", "jobs": {}}) is None
