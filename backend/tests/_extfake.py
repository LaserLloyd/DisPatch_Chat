"""A fake extension for the host's tests (app/extensions.py).

Not a pytest module (leading underscore): test_extensions.py,
test_thread_websocket.py and test_turn_hooks.py import it. It plays the part
of the DisPatch Voice add-on with none of its code — a router under
/api/voice, a static tree, the /ws/voice/ handler, a bot-field validator, a
tools row and lifecycle hooks — so every host rule can be driven without the
real package installed (DisPatch's CI never installs it: that is the point).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from fastapi import APIRouter, Request
from fastapi.testclient import TestClient

EXTENSION = {
    "id": "voice", "name": "Fake Voice", "version": "9.9.9", "api": [1, 1],
    "setup": "_extfake:setup", "env": ["FAKE_EXT_ENV"],
}
INCOMPATIBLE_NEW = {**EXTENSION, "api": [2, 3], "name": "Future Voice", "version": "3.0.0"}
INCOMPATIBLE_OLD = {**EXTENSION, "api": [0, 0], "name": "Old Voice", "version": "0.1.0"}
NOT_A_DICT = ["voice"]
BAD_ID = {**EXTENSION, "id": "Voice!"}
OTHER = {**EXTENSION, "id": "other", "name": "Other", "setup": "_extfake:setup_other"}


@dataclass
class Knobs:
    """What the next setup(ctx) does. Reset per test by the fixture."""
    static_dir: Path | None = None
    raise_in_setup: BaseException | None = None
    raise_after_router: bool = False
    bad_prefix: str | None = None
    ws_route_in_router: bool = False
    max_total: int = 3
    max_per_session: int = 2
    async_setup: bool = False
    block_setup: threading.Event | None = None   # setup() waits on it (a hung setup)
    with_tools: bool = True
    handler_mode: str = "echo"          # echo | unavailable | hold | noaccept | slow_replaced | submit
    locked_status: Any = None           # ctx.locked_status(fn) when set
    ctx: Any = None
    sockets: list = field(default_factory=list)
    startup_threads: list = field(default_factory=list)
    startup_calls: list = field(default_factory=list)
    shutdown_calls: list = field(default_factory=list)
    replaced: list = field(default_factory=list)
    release: asyncio.Event | None = None


KNOBS = Knobs()


@dataclass
class Tool:
    """Built by the extension, read by attribute (amendment T0-2)."""
    id: str
    feature: str
    title: str
    icon: str
    opt_in: bool
    probe: Any


def _validator(value: str) -> None:
    if value not in ("", "stock-host", "builtin:af_heart"):
        raise ValueError(f"Unknown voice: {value}")


def _rp_validator(value: str) -> None:
    if value not in ("", "all", "skip_actions", "dialogue", "dialogue_narrator"):
        raise ValueError(f"Unknown role-play mode: {value}")


async def handler(ws, sock) -> None:
    KNOBS.sockets.append(sock)
    sock.on_replaced(lambda: KNOBS.replaced.append(sock))
    if KNOBS.handler_mode == "slow_replaced":
        async def slow():
            await asyncio.sleep(5)
        sock.on_replaced(slow)
    if KNOBS.handler_mode == "noaccept":
        return                               # a buggy handler: never accepts
    if KNOBS.handler_mode == "unavailable":
        await sock.close_unavailable("Voice is turned off in Settings › Voice")
        return
    await ws.accept()
    await ws.send_text(json.dumps({"type": "ready", "bot": sock.bot.id,
                                   "backend": sock.bot.backend,
                                   "token": bool(sock.token),
                                   "locked": bool(getattr(sock, "locked", False))}))
    if KNOBS.handler_mode == "submit":
        # Plays the voice add-on's turn path: every text frame becomes a turn
        # through ctx.submit_turn (spoken or typed alike). The fake never says
        # whether its socket is locked — the host must know on its own.
        with contextlib.suppress(Exception):
            while True:
                msg = await ws.receive_text()
                if msg.startswith("spec:"):
                    run = await KNOBS.ctx.speculate_turn(sock.thread.id, msg[5:])
                    await ws.send_text(json.dumps({"spec": run is not None}))
                    continue
                mid = await KNOBS.ctx.submit_turn(sock.thread.id, msg, metadata={"voice": True})
                await ws.send_text(json.dumps({"id": mid}))
        return
    if KNOBS.handler_mode == "hold":
        with contextlib.suppress(Exception):
            while True:
                msg = await ws.receive_text()
                if msg == "live?":
                    await ws.send_text(json.dumps({"live": sock.session_live()}))
                elif msg == "touch":
                    sock.touch()
                    await ws.send_text("touched")
        return
    with contextlib.suppress(Exception):
        while True:
            msg = await ws.receive_text()
            await ws.send_text("echo:" + msg)


def setup(ctx) -> None:
    if KNOBS.block_setup is not None:
        KNOBS.block_setup.wait(10)
        return                               # "hung", then gave up quietly (the host moved on)
    if KNOBS.async_setup:
        async def nope():
            return None
        return nope()                     # type: ignore[return-value]
    KNOBS.ctx = ctx
    if KNOBS.raise_in_setup is not None and not KNOBS.raise_after_router:
        raise KNOBS.raise_in_setup
    router = APIRouter()

    @router.get("/status")
    def status(request: Request):
        return {"ok": True, "env": sorted(ctx.env), "counters": ctx.counters}

    @router.get("/providers")
    def providers():
        return {"from": "addon", "providers": {"stt": [], "tts": []}}

    @router.post("/upload")
    async def upload(request: Request):
        body = await request.body()
        return {"bytes": len(body)}

    if KNOBS.ws_route_in_router:
        @router.websocket("/sneaky")
        async def sneaky(ws):            # pragma: no cover - must never mount
            await ws.accept()

    ctx.include_router(router, prefix=KNOBS.bad_prefix or "/api/voice",
                       body_limits={"/api/voice/upload": 1000})
    if KNOBS.raise_in_setup is not None:
        raise KNOBS.raise_in_setup
    if KNOBS.static_dir is not None:
        ctx.serve_static(KNOBS.static_dir, entry="index.js", css=["voice.css"], locales=True)
    ctx.thread_websocket("voice", handler, max_per_session=KNOBS.max_per_session,
                         max_total=KNOBS.max_total)
    if KNOBS.locked_status is not None:
        ctx.locked_status(KNOBS.locked_status)
    ctx.set_bot_field_validator("voice", _validator)
    ctx.set_bot_field_validator("voice_rp", _rp_validator)
    if KNOBS.with_tools:
        ctx.register_tools([Tool("voice-lab", "voice_lab", "Voice lab", "🧪", True, lambda: True),
                            Tool("voice-cloning", "voice_cloning", "Voice cloning", "🎙️", True,
                                 lambda: False)])
    ctx.on_startup(lambda: KNOBS.startup_threads.append(threading.current_thread().name),
                   thread=True)
    ctx.on_startup(lambda: KNOBS.startup_calls.append("plain"))

    def broken():
        raise RuntimeError("hook broke")

    ctx.on_startup(broken)
    ctx.on_shutdown(lambda: KNOBS.shutdown_calls.append("down"))


def setup_other(ctx) -> None:
    router = APIRouter()

    @router.get("/ping")
    def ping():
        return {"pong": True}

    ctx.include_router(router, prefix="/api/other")


# --------------------------------------------------------------------------- #
# Static tree + environment fixture
# --------------------------------------------------------------------------- #

def make_static(root: Path) -> Path:
    d = root / "static"
    (d / "locales").mkdir(parents=True)
    (d / "index.js").write_text("export const EXT = { id: 'voice', api: [1, 1] };\n")
    (d / "voice.css").write_text(".voice-ring { opacity: 1; }\n")
    (d / "locales" / "en.json").write_text('{"drive": {"title": "Voice chat"}}')
    (d / "voices.html").write_text("<html><script>alert(1)</script></html>")
    (d / "icon.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>")
    (d / "notes.xml").write_text("<x/>")
    (d / "helper.py").write_text("print('no')\n")
    (d / ".hidden.js").write_text("secret\n")
    return d


def install(target: str = "_extfake:EXTENSION", source: str = "test"):
    """Load one extension into main.app the way mount_all does."""
    from app import extensions, main
    ext = extensions.load_one(main.app, extensions.Candidate(target, source, "voice"))
    extensions._register(ext)
    return ext


@pytest.fixture
def ext_env(tmp_path, monkeypatch):
    """A hermetic app (throwaway data dir + DB) with NO extension installed;
    tests call install() to add the fake one. Yields (client, tmp_path)."""
    from app import auth, config, extensions, main, tools
    from app.database import Database
    from app.ws import ConnectionManager

    for name, val in (("DATA_DIR", tmp_path), ("CONFIG_PATH", tmp_path / "config.yaml"),
                      ("MEDIA_DIR", tmp_path / "media"), ("FILES_DIR", tmp_path / "files"),
                      ("LOG_DIR", tmp_path / "logs"), ("BACKUP_DIR", tmp_path / "backups")):
        monkeypatch.setattr(config, name, val)
    monkeypatch.setattr(main, "MEDIA_DIR", tmp_path / "media")
    monkeypatch.setattr(main, "FILES_DIR", tmp_path / "files")
    monkeypatch.setattr(auth, "SECURITY_PATH", tmp_path / "security.yaml")
    monkeypatch.setattr(auth, "RECOVERY_PATH", tmp_path / "RECOVERY-CODE.txt")
    auth._sessions.clear()
    auth._cache = None
    auth._fail_count = 0
    auth._fail_until = 0.0
    config._invalidate_bots_cache()
    tools._reset_state()
    temp_db = Database(tmp_path / "chats.db")
    monkeypatch.setattr(main, "db", temp_db)
    main._ACK_SEEN.clear()
    main._delivered.clear()
    main._thread_bot.clear()
    main._speculations.clear()
    main._ext_counters.clear()
    # Older suites swap manager.broadcast for a no-op and never put it back;
    # these tests listen for the REAL broadcast (and its observers).
    monkeypatch.setattr(main.manager, "broadcast",
                        ConnectionManager.broadcast.__get__(main.manager))
    KNOBS.__init__()                                  # fresh knobs per test
    KNOBS.static_dir = make_static(tmp_path)
    extensions.unload_all(main.app)
    turns: list = []

    async def fake_turn(thread_id, bot_id, text, opts=None):
        turns.append((thread_id, bot_id, text, opts))

    monkeypatch.setattr(main, "run_agent_turn", fake_turn)
    c = TestClient(main.app)
    c.__enter__()
    c.turns = turns                                    # type: ignore[attr-defined]
    try:
        yield c, tmp_path
    finally:
        c.__exit__(None, None, None)
        extensions.unload_all(main.app)
        tools._reset_state()
        asyncio.run(temp_db.close())


def unlock(c, pin: str = "1234") -> None:
    from app import auth
    auth.set_pin(pin)
    r = c.post("/api/auth/unlock", json={"pin": pin})
    assert r.status_code == 200, r.text


def lock(c, pin: str = "1234") -> None:
    from app import auth
    auth.set_pin(pin)
    c.cookies.clear()


def thread_for(c, bot_id: str = "main") -> str:
    r = c.post("/api/threads", json={"bot_id": bot_id})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def run(c, coro_fn):
    """Run an async callable on the TestClient's loop (where the app lives)."""
    return c.portal.call(coro_fn)
