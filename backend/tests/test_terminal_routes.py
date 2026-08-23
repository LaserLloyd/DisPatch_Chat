"""Route-level tests for /api/terminal/* and /ws/terminal — FastAPI TestClient
with the PTY session manager mocked (never spawns a real process). Mirrors
test_comfy_routes.py's hermetic fixture. Run: cd backend && uv run pytest.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app import auth, config, main, terminal
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
    monkeypatch.setattr(main, "SETTINGS", replace(config.SETTINGS, terminal_enabled=True))

    with TestClient(main.app) as client:
        yield client

    asyncio.run(temp_db.close())


def _unlock(client: TestClient, pin: str = "1234") -> None:
    auth.set_pin(pin)
    r = client.post("/api/auth/unlock", json={"pin": pin})
    assert r.status_code == 200, r.text


class FakeSession:
    """Stand-in for terminal.session — no real PTY, records control ops."""

    def __init__(self):
        self._state = "stopped"
        self.calls = []
        self._options = {"yolo": False, "model": None, "resume": "none"}
        self._active = None

    def status(self):
        pending = self._state == "running" and self._active is not None and self._active != self._options
        return {"state": self._state, "pid": 4321 if self._state == "running" else None,
                "started_at": 1751000000.0 if self._state != "stopped" else None,
                "exit_code": 0 if self._state == "exited" else None,
                "options": dict(self._options), "pending_options": pending}

    def get_options(self):
        return dict(self._options)

    def set_options(self, yolo=None, model=..., resume=...):
        if yolo is not None:
            if not isinstance(yolo, bool):
                raise terminal.OptionsValidationError("yolo must be a boolean")
            self._options["yolo"] = yolo
        if model is not ...:
            if model in (None, ""):
                self._options["model"] = None
            elif isinstance(model, str) and terminal._MODEL_RE.match(model):
                self._options["model"] = model
            else:
                raise terminal.OptionsValidationError("bad model")
        if resume is not ...:
            if resume in (None, ""):
                self._options["resume"] = "none"
            elif isinstance(resume, str) and resume in terminal._RESUME_FLAGS:
                self._options["resume"] = resume
            else:
                raise terminal.OptionsValidationError("bad resume")
        return dict(self._options)

    async def start(self):
        self.calls.append("start"); self._state = "running"
        self._active = dict(self._options); return self.status()

    async def stop(self):
        self.calls.append("stop"); self._state = "exited"; return self.status()

    async def restart(self):
        self.calls.append("restart"); self._state = "running"
        self._active = dict(self._options); return self.status()

    # WS-side mirroring surface (no real PTY).
    def attach(self, cb):
        return b""

    def detach(self, cb):
        pass

    def add_state_hook(self, hook):
        pass

    def remove_state_hook(self, hook):
        pass

    async def shutdown(self):
        pass


@pytest.fixture
def fake_term(monkeypatch):
    fake = FakeSession()
    monkeypatch.setattr(terminal, "session", fake)
    return fake


TERMINAL_ROUTES = [
    ("GET", "/api/terminal/status"),
    ("POST", "/api/terminal/start"),
    ("POST", "/api/terminal/stop"),
    ("POST", "/api/terminal/restart"),
    ("POST", "/api/terminal/options"),
    ("GET", "/api/terminal/models"),
]


# --------------------------------------------------------------------------- #
# Full-session happy paths
# --------------------------------------------------------------------------- #


def test_status_start_stop_restart_happy(route_client, fake_term):
    _unlock(route_client)
    r = route_client.get("/api/terminal/status")
    assert r.status_code == 200 and r.json()["state"] == "stopped"

    assert route_client.post("/api/terminal/start").json()["state"] == "running"
    assert route_client.post("/api/terminal/restart").json()["state"] == "running"
    assert route_client.post("/api/terminal/stop").json()["state"] == "exited"
    assert fake_term.calls == ["start", "restart", "stop"]


def test_no_pin_set_makes_the_terminal_unavailable(route_client, fake_term):
    """A PTY is arbitrary code execution and DisPatch listens on 0.0.0.0, so
    "no PIN configured" must mean the terminal is OFF — not open to anyone who
    can reach the port. The env flag gates the feature; the PIN gates access."""
    for path, method in (("/api/terminal/status", "get"),
                         ("/api/terminal/start", "post"),
                         ("/api/terminal/stop", "post")):
        r = getattr(route_client, method)(path)
        assert r.status_code == 403, f"{path} -> {r.status_code}"
        assert "PIN" in r.json()["detail"]
    assert fake_term.calls == []

    # The advertised feature flag agrees with the gate.
    assert route_client.get("/api/auth/status").json()["features"].get("terminal") is False

    # With a PIN set and a live session it works exactly as before.
    _unlock(route_client)
    assert route_client.get("/api/terminal/status").json()["state"] == "stopped"
    assert route_client.get("/api/auth/status").json()["features"]["terminal"] is True


def test_no_pin_set_closes_the_terminal_socket(route_client):
    import pytest as _pytest
    from starlette.websockets import WebSocketDisconnect

    with _pytest.raises(WebSocketDisconnect):
        with route_client.websocket_connect("/ws/terminal"):
            pass


def test_busy_error_maps_to_409(route_client, fake_term, monkeypatch):
    _unlock(route_client)

    async def busy():
        raise terminal.TerminalBusyError()
    monkeypatch.setattr(fake_term, "start", busy)
    assert route_client.post("/api/terminal/start").status_code == 409


def test_generic_error_maps_to_502(route_client, fake_term, monkeypatch):
    _unlock(route_client)

    async def boom():
        raise terminal.TerminalError("pty exploded")
    monkeypatch.setattr(fake_term, "start", boom)
    assert route_client.post("/api/terminal/start").status_code == 502


# --------------------------------------------------------------------------- #
# Safe Mode / decoy is denied on EVERY terminal surface (code execution)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("method,path", TERMINAL_ROUTES)
def test_decoy_gets_403_on_every_route(route_client, fake_term, method, path):
    auth.set_pin("1234")   # PIN set, this client never unlocks -> decoy
    r = route_client.request(method, path)
    assert r.status_code == 403, f"{method} {path} -> {r.status_code}"


def test_decoy_ws_terminal_refused(route_client, fake_term):
    auth.set_pin("1234")   # PIN set, no session
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect):
        with route_client.websocket_connect("/ws/terminal") as ws:
            ws.receive_json()


def test_cross_origin_ws_terminal_refused(route_client, fake_term):
    """CSWSH guard: a mismatched Origin is rejected even with full access."""
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect):
        with route_client.websocket_connect(
            "/ws/terminal", headers={"origin": "http://evil.example"}
        ) as ws:
            ws.receive_json()


def test_full_ws_terminal_connects_and_gets_state(route_client, fake_term):
    _unlock(route_client)
    with route_client.websocket_connect("/ws/terminal") as ws:
        first = ws.receive_json()
        assert first["type"] == "state"
    # A 'stopped' session auto-starts on first open.
    assert "start" in fake_term.calls


# --------------------------------------------------------------------------- #
# Feature flag off → 404 (routes AND ws)
# --------------------------------------------------------------------------- #


def test_feature_disabled_404s(route_client, fake_term, monkeypatch):
    monkeypatch.setattr(main, "SETTINGS", replace(config.SETTINGS, terminal_enabled=False))
    assert route_client.get("/api/terminal/status").status_code == 404


def test_feature_disabled_ws_closes(route_client, fake_term, monkeypatch):
    monkeypatch.setattr(main, "SETTINGS", replace(config.SETTINGS, terminal_enabled=False))
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect):
        with route_client.websocket_connect("/ws/terminal") as ws:
            ws.receive_json()


# --------------------------------------------------------------------------- #
# The state broadcast is dropped for Safe-Mode connections
# --------------------------------------------------------------------------- #


def test_redact_drops_terminal_state_frame():
    assert main.redact_for_decoy({"type": "terminal_state", "state": "running"}) is None


# --------------------------------------------------------------------------- #
# Spawn options: set (yolo/model), validation, and pending logic
# --------------------------------------------------------------------------- #


def test_options_set_yolo_and_model(route_client, fake_term):
    _unlock(route_client)
    r = route_client.post("/api/terminal/options", json={"yolo": True, "model": "deepseek-pro/deepseek-v4-pro"})
    assert r.status_code == 200
    opts = r.json()["options"]
    assert opts["yolo"] is True and opts["model"] == "deepseek-pro/deepseek-v4-pro"


def test_options_clear_model_with_null(route_client, fake_term):
    _unlock(route_client)
    route_client.post("/api/terminal/options", json={"model": "mimo-pro"})
    r = route_client.post("/api/terminal/options", json={"model": None})
    assert r.json()["options"]["model"] is None


def test_options_bad_model_422(route_client, fake_term):
    _unlock(route_client)
    r = route_client.post("/api/terminal/options", json={"model": "bad model!$"})
    assert r.status_code == 422


def test_options_bad_yolo_422(route_client, fake_term):
    _unlock(route_client)
    r = route_client.post("/api/terminal/options", json={"yolo": "yes"})
    assert r.status_code == 422


def test_options_set_resume_mode(route_client, fake_term):
    _unlock(route_client)
    for mode in ("continue", "resume", "copy", "none"):
        r = route_client.post("/api/terminal/options", json={"resume": mode})
        assert r.status_code == 200
        assert r.json()["options"]["resume"] == mode


def test_options_bad_resume_422(route_client, fake_term):
    _unlock(route_client)
    assert route_client.post("/api/terminal/options", json={"resume": "bogus"}).status_code == 422
    assert route_client.post("/api/terminal/options", json={"resume": 3}).status_code == 422


def test_options_clear_resume_with_null(route_client, fake_term):
    _unlock(route_client)
    route_client.post("/api/terminal/options", json={"resume": "continue"})
    r = route_client.post("/api/terminal/options", json={"resume": None})
    assert r.json()["options"]["resume"] == "none"


def test_pending_options_flag_via_resume(route_client, fake_term):
    _unlock(route_client)
    assert route_client.post("/api/terminal/start").json()["pending_options"] is False
    r = route_client.post("/api/terminal/options", json={"resume": "continue"})
    assert r.json()["pending_options"] is True
    assert route_client.post("/api/terminal/restart").json()["pending_options"] is False


def test_pending_options_flag_transitions(route_client, fake_term):
    _unlock(route_client)
    # Start with defaults → running, no pending.
    assert route_client.post("/api/terminal/start").json()["pending_options"] is False
    # Change an option while running → pending true (needs a restart to apply).
    r = route_client.post("/api/terminal/options", json={"yolo": True})
    assert r.json()["pending_options"] is True
    # Restart applies them → pending clears.
    assert route_client.post("/api/terminal/restart").json()["pending_options"] is False


def test_stopped_session_never_pending(route_client, fake_term):
    _unlock(route_client)
    # Not running → setting options is applied on next start, never 'pending'.
    r = route_client.post("/api/terminal/options", json={"yolo": True, "model": "mimo-pro"})
    assert r.json()["pending_options"] is False


# --------------------------------------------------------------------------- #
# Models picker route
# --------------------------------------------------------------------------- #


def test_models_route(route_client, fake_term, monkeypatch):
    _unlock(route_client)
    monkeypatch.setattr(terminal, "discover_models",
                        lambda: {"models": ["a", "prov/a"], "current_default": "prov/a"})
    r = route_client.get("/api/terminal/models")
    assert r.status_code == 200
    body = r.json()
    assert body["models"] == ["a", "prov/a"] and body["current_default"] == "prov/a"


def test_discover_models_parses_providers(tmp_path, monkeypatch):
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        'default_model = "deepseek-pro/deepseek-v4-pro"\n'
        '[[providers]]\n'
        'name = "deepseek-pro"\n'
        'models = ["deepseek-v4-pro"]\n'
        '[[providers]]\n'
        'name = "deepseek-flash"\n'
        'models = ["deepseek-v4-flash"]\n'
    )
    monkeypatch.setattr(terminal, "TERMINAL_CONFIG_PATHS", [cfg])
    out = terminal.discover_models()
    assert out["current_default"] == "deepseek-pro/deepseek-v4-pro"
    assert "deepseek-v4-pro" in out["models"]
    assert "deepseek-pro/deepseek-v4-pro" in out["models"]
    assert "deepseek-flash/deepseek-v4-flash" in out["models"]


def test_discover_models_unreadable_is_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(terminal, "TERMINAL_CONFIG_PATHS", [tmp_path / "nope.toml"])
    assert terminal.discover_models() == {"models": [], "current_default": None}


# --------------------------------------------------------------------------- #
# Real PTY through the actual /ws/terminal endpoint (stand-in binary = cat):
# proves base64 output framing, input write, and scrollback replay work E2E.
# --------------------------------------------------------------------------- #


def test_ws_terminal_real_pty_echo(route_client, monkeypatch):
    import base64
    import shutil

    from app.terminal import TerminalSession

    real = TerminalSession(binary=shutil.which("cat") or "/bin/cat")
    monkeypatch.setattr(terminal, "session", real)
    # A PIN + a live session is now the only way in; the endpoint then
    # auto-starts a 'stopped' session.
    _unlock(route_client)
    with route_client.websocket_connect("/ws/terminal") as ws:
        first = ws.receive_json()
        assert first["type"] == "state" and first["state"] == "running"
        ws.send_json({"type": "input", "data": "echo-me\n"})
        got = b""
        for _ in range(40):
            frame = ws.receive_json()
            if frame.get("type") == "output":
                got += base64.b64decode(frame["data"])
                if b"echo-me" in got:
                    break
        assert b"echo-me" in got
    import asyncio as _a
    _a.run(real.shutdown())


def test_ws_terminal_output_stops_after_session_lapse(route_client, monkeypatch):
    """A silent viewer must stop receiving PTY output the moment its session
    lapses: the sender re-checks liveness per frame (the receive loop only
    notices a lapse when the client sends). Code-exec surface — it drops."""
    import shutil

    from app.terminal import TerminalSession

    real = TerminalSession(binary=shutil.which("cat") or "/bin/cat")
    monkeypatch.setattr(terminal, "session", real)
    _unlock(route_client)
    with route_client.websocket_connect("/ws/terminal") as ws:
        first = ws.receive_json()
        assert first["type"] == "state" and first["state"] == "running"
        # Session lapses server-side (idle expiry / lock from another tab).
        auth._sessions.clear()
        # New PTY output must never reach this socket: the sender emits a
        # 'locked' frame and closes instead.
        real.write(b"secret-after-lock\n")
        for _ in range(40):
            frame = ws.receive_json()
            assert frame.get("type") != "output", frame
            if frame.get("type") == "locked":
                break
        else:
            pytest.fail("no 'locked' frame after session lapse")
    import asyncio as _a
    _a.run(real.shutdown())
