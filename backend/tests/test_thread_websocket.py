"""The thread-websocket gate the host owns (app/extensions.py, CONTRACTS §B.1
rule 3 and rule 9): Origin==Host → the thread exists → an unlocked session, or
(since 2.3.4) a locked device on a thread it can see — a `safe` bot's, its
turns stamped locked like a typed Safe-Mode send → max_total →
per-session slots → the extension's handler. Refusals 1–3 close
1008 BEFORE accept; "busy" and "nothing provides this" are accept-then-1013
for authorised callers only. Plus the uvicorn frame cap the live unit runs
with (--ws-max-size 262144) against the main /ws's 65,536-char messages.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml
from _extfake import KNOBS, ext_env, install, lock, thread_for, unlock
from starlette.websockets import WebSocketDisconnect

from app import auth, config

ORIGIN = {"origin": "http://testserver"}


def refused(c, path, **kw):
    """Close code of a socket the server refused (pre- or post-accept)."""
    with pytest.raises(WebSocketDisconnect) as e:
        with c.websocket_connect(path, **kw) as ws:
            ws.receive_text()
            ws.receive_text()
    return e.value.code, e.value.reason


# --------------------------------------------------------------------------- #
# Gate refusals: 1008 before accept, handler never runs
# --------------------------------------------------------------------------- #


def test_cross_origin_is_refused_even_unlocked(ext_env):
    c, _ = ext_env
    install()
    tid = thread_for(c)
    unlock(c)
    assert refused(c, f"/ws/voice/{tid}", headers={"origin": "https://evil.example"})[0] == 1008
    assert refused(c, f"/ws/voice/{tid}", headers={"origin": "null"})[0] == 1008
    assert KNOBS.sockets == []


def test_locked_device_is_refused_on_a_non_safe_bots_thread(ext_env):
    c, _ = ext_env
    install()
    tid = thread_for(c)
    lock(c)
    assert refused(c, f"/ws/voice/{tid}", headers=ORIGIN)[0] == 1008
    assert KNOBS.sockets == []


def test_unknown_thread_is_refused(ext_env):
    c, _ = ext_env
    install()
    unlock(c)
    assert refused(c, "/ws/voice/no-such-thread", headers=ORIGIN)[0] == 1008
    assert KNOBS.sockets == []


def test_a_safe_bots_thread_from_an_unlocked_device(ext_env):
    c, _ = ext_env
    install()
    tid = thread_for(c, "alpha")                      # a Safe-Mode bot
    unlock(c)
    KNOBS.handler_mode = "hold"
    with c.websocket_connect(f"/ws/voice/{tid}", headers=ORIGIN) as ws:
        ws.receive_text()
        assert _live(ws) is True
    sock = KNOBS.sockets[-1]
    assert sock.thread.id == tid and sock.bot.safe is True and sock.locked is False


# --------------------------------------------------------------------------- #
# Locked devices (2.3.4): the threads a locked device can see (`safe` bots),
# every turn locked
# --------------------------------------------------------------------------- #


def test_a_locked_device_is_still_refused_on_a_non_safe_bots_thread(ext_env):
    c, _ = ext_env
    install()
    tid = thread_for(c, "main")                       # not safe
    lock(c)
    assert refused(c, f"/ws/voice/{tid}", headers=ORIGIN)[0] == 1008
    assert refused(c, "/ws/voice/no-such-thread", headers=ORIGIN)[0] == 1008
    assert KNOBS.sockets == []


def test_a_locked_device_keeps_the_origin_check(ext_env):
    c, _ = ext_env
    install()
    tid = thread_for(c, "alpha")
    lock(c)
    assert refused(c, f"/ws/voice/{tid}", headers={"origin": "https://evil.example"})[0] == 1008
    assert refused(c, f"/ws/voice/{tid}", headers={"origin": "null"})[0] == 1008
    assert KNOBS.sockets == []


def _add_bot(bot_id: str, **fields) -> None:
    doc = yaml.safe_load(config.CONFIG_PATH.read_text())
    doc["bots"].append({"id": bot_id, "name": bot_id.title(), "order": 40, **fields})
    config.CONFIG_PATH.write_text(yaml.safe_dump(doc))
    config._invalidate_bots_cache()


def _set_bot(bot_id: str, **fields) -> None:
    doc = yaml.safe_load(config.CONFIG_PATH.read_text())
    for b in doc["bots"]:
        if b["id"] == bot_id:
            b.update(fields)
    config.CONFIG_PATH.write_text(yaml.safe_dump(doc))
    config._invalidate_bots_cache()


def test_a_locked_device_is_refused_on_a_hidden_safe_bots_thread(ext_env):
    """The threads a locked device may use are the ones its chat list shows:
    a `safe` bot that is LISTED (main._safe_bot_ids — the main /ws refuses a
    Safe-Mode send to a hidden bot's thread, so does the extension gate)."""
    c, _ = ext_env
    install()
    unlock(c)
    _add_bot("ghost", safe=True, visible=False)
    tid = thread_for(c, "ghost")
    with c.websocket_connect(f"/ws/voice/{tid}", headers=ORIGIN) as ws:
        ws.receive_text()                       # unlocked: a hidden bot is fine
    lock(c)
    refused(c, f"/ws/voice/{tid}", headers=ORIGIN)


def _turn_opts(c, n):
    for _ in range(100):
        if len(c.turns) >= n:
            break
        c.portal.call(asyncio.sleep, 0.01)
    return c.turns[n - 1][3]


def test_unclaimed_name_runs_the_gate_then_1013(ext_env):
    c, _ = ext_env
    tid = thread_for(c)
    # Nothing installed at all: the voice client gets a real reason, not a
    # bare 1006 it would retry for 15 minutes (hardening L1).
    code, reason = refused(c, f"/ws/voice/{tid}", headers=ORIGIN)
    assert (code, reason) == (1013, "No extension provides voice")
    install()
    code, reason = refused(c, f"/ws/other/{tid}", headers=ORIGIN)
    assert (code, reason) == (1013, "No extension provides other")
    # …and an unauthorised caller still only ever sees 1008.
    lock(c)
    assert refused(c, f"/ws/other/{tid}", headers=ORIGIN)[0] == 1008
    assert refused(c, f"/ws/voice/{tid}", headers={"origin": "https://evil.example"})[0] == 1008


def test_gated_socket_reaches_the_handler_with_a_bot_view(ext_env):
    c, _ = ext_env
    install()
    tid = thread_for(c)
    unlock(c)
    with c.websocket_connect(f"/ws/voice/{tid}", headers=ORIGIN) as ws:
        ready = json.loads(ws.receive_text())
        assert ready == {"type": "ready", "bot": "main", "backend": "gateway", "token": True,
                         "locked": False}
        ws.send_text("hi")
        assert ws.receive_text() == "echo:hi"
    sock = KNOBS.sockets[0]
    assert sock.thread.id == tid and sock.bot.safe is False
    assert not hasattr(sock.bot, "api")


def test_close_unavailable_is_accept_then_1013(ext_env):
    c, _ = ext_env
    KNOBS.handler_mode = "unavailable"
    install()
    tid = thread_for(c)
    code, reason = refused(c, f"/ws/voice/{tid}", headers=ORIGIN)
    assert (code, reason) == (1013, "Voice is turned off in Settings › Voice")


# --------------------------------------------------------------------------- #
# Slots: per session (4001, oldest) and in total (busy, 1013)
# --------------------------------------------------------------------------- #


def test_a_third_socket_of_one_session_replaces_the_oldest(ext_env):
    c, _ = ext_env
    install()
    tid = thread_for(c)
    unlock(c)
    with c.websocket_connect(f"/ws/voice/{tid}", headers=ORIGIN) as a:
        a.receive_text()
        with c.websocket_connect(f"/ws/voice/{tid}", headers=ORIGIN) as b:
            b.receive_text()
            assert KNOBS.ctx.socket_count("voice") == (2, 3)
            with c.websocket_connect(f"/ws/voice/{tid}", headers=ORIGIN) as d:
                d.receive_text()
                with pytest.raises(WebSocketDisconnect) as e:
                    a.receive_text()
                assert e.value.code == 4001 and e.value.reason == "replaced"
                assert KNOBS.replaced == [KNOBS.sockets[0]]
                assert KNOBS.ctx.socket_count("voice") == (2, 3)
    assert KNOBS.ctx.socket_count("voice") == (0, 3)


def test_eviction_never_parks_the_replacement_behind_a_half_open_peer(ext_env, monkeypatch):
    """Review T1 M1: the stale socket's close (a half-open phone never answers
    it; websockets waits ~10 s) runs in the background, so the reconnecting
    socket reaches the handler at once."""
    from app import extensions
    c, _ = ext_env
    KNOBS.max_per_session = 1
    install()
    tid = thread_for(c)
    unlock(c)
    real_close = extensions._Slot.close
    closes = []

    async def hung_close(self):
        closes.append(time.monotonic())
        import asyncio
        await asyncio.sleep(3)
        await real_close(self)

    monkeypatch.setattr(extensions._Slot, "close", hung_close)
    with c.websocket_connect(f"/ws/voice/{tid}", headers=ORIGIN) as a:
        a.receive_text()
        t0 = time.monotonic()
        with c.websocket_connect(f"/ws/voice/{tid}", headers=ORIGIN) as b:
            assert json.loads(b.receive_text())["type"] == "ready"
            assert time.monotonic() - t0 < 0.5
            assert KNOBS.replaced == [KNOBS.sockets[0]]     # callbacks ran first
            assert KNOBS.ctx.socket_count("voice") == (1, 3)
            assert len(closes) == 1                         # the close was started


def test_slow_on_replaced_callback_is_bounded(ext_env):
    """A hanging on_replaced callback delays the replacement by at most
    EVICT_CALLBACK_WAIT_S, never without bound."""
    from app import extensions
    c, _ = ext_env
    KNOBS.max_per_session = 1
    KNOBS.handler_mode = "slow_replaced"
    install()
    tid = thread_for(c)
    unlock(c)
    with c.websocket_connect(f"/ws/voice/{tid}", headers=ORIGIN) as a:
        a.receive_text()
        t0 = time.monotonic()
        with c.websocket_connect(f"/ws/voice/{tid}", headers=ORIGIN) as b:
            b.receive_text()
            waited = time.monotonic() - t0
            assert waited < extensions.EVICT_CALLBACK_WAIT_S + 0.5


def test_handler_that_never_accepts_gets_a_real_close_code(ext_env):
    """Review T1 L4: a handler returning without accept() must not leave the
    browser with a bare 1006 it would retry."""
    c, _ = ext_env
    KNOBS.handler_mode = "noaccept"
    install()
    tid = thread_for(c)
    code, reason = refused(c, f"/ws/voice/{tid}", headers=ORIGIN)
    assert (code, reason) == (1013, "extension did not accept")
    assert KNOBS.ctx.socket_count("voice") == (0, 3)


def test_max_total_answers_busy_for_another_session(ext_env):
    c, _ = ext_env
    KNOBS.max_total = 2
    install()
    tid = thread_for(c)
    # No PIN: the device id (?d=) keys the slot, so these are three devices.
    with c.websocket_connect(f"/ws/voice/{tid}?d=device-aaaa", headers=ORIGIN) as a:
        a.receive_text()
        with c.websocket_connect(f"/ws/voice/{tid}?d=device-bbbb", headers=ORIGIN) as b:
            b.receive_text()
            code, reason = refused(c, f"/ws/voice/{tid}?d=device-cccc", headers=ORIGIN)
            assert (code, reason) == (1013, "Voice is busy on this server (2 chats open)")
            # The SAME device reconnecting is never "busy": its oldest is replaced.
            with c.websocket_connect(f"/ws/voice/{tid}?d=device-aaaa", headers=ORIGIN) as a2:
                a2.receive_text()
                with pytest.raises(WebSocketDisconnect) as e:
                    a.receive_text()
                assert e.value.code == 4001
    assert KNOBS.ctx.socket_count("voice") == (0, 2)


# --------------------------------------------------------------------------- #
# ThreadSocket.session_live / touch
# --------------------------------------------------------------------------- #


def _live(ws) -> bool:
    ws.send_text("live?")
    return json.loads(ws.receive_text())["live"]


def test_session_live_follows_the_session(ext_env):
    c, _ = ext_env
    KNOBS.handler_mode = "hold"
    install()
    tid = thread_for(c)
    unlock(c)
    with c.websocket_connect(f"/ws/voice/{tid}", headers=ORIGIN) as ws:
        ws.receive_text()
        assert _live(ws) is True
        auth._sessions.clear()                        # idle lock / lock-now
        assert _live(ws) is False


def test_session_live_false_when_the_thread_is_deleted(ext_env):
    c, _ = ext_env
    KNOBS.handler_mode = "hold"
    install()
    tid = thread_for(c)
    with c.websocket_connect(f"/ws/voice/{tid}", headers=ORIGIN) as ws:
        ws.receive_text()
        assert _live(ws) is True
        assert c.delete(f"/api/threads/{tid}").status_code == 200
        assert _live(ws) is False


def test_session_live_stays_true_when_the_bot_turns_safe(ext_env):
    """Marking the bot `safe` mid-call widens who may chat with it; it does not
    end an unlocked device's socket (2.3.1). Removing the bot still does."""
    c, _ = ext_env
    KNOBS.handler_mode = "hold"
    install()
    tid = thread_for(c)
    with c.websocket_connect(f"/ws/voice/{tid}", headers=ORIGIN) as ws:
        ws.receive_text()
        assert _live(ws) is True
        bots = c.get("/api/bots/all").json()["bots"]
        items = [{"id": b["id"], "order": i, "safe": (True if b["id"] == "main" else None)}
                 for i, b in enumerate(bots)]
        assert c.put("/api/bots/order", json={"bots": items}).status_code == 200
        assert config.get_bot("main").safe is True
        assert _live(ws) is True
        assert c.delete("/api/bots/main").status_code == 200
        assert _live(ws) is False


def test_session_live_false_when_a_pin_appears_on_a_pinless_install(ext_env):
    c, _ = ext_env
    KNOBS.handler_mode = "hold"
    install()
    tid = thread_for(c)
    with c.websocket_connect(f"/ws/voice/{tid}", headers=ORIGIN) as ws:
        ws.receive_text()
        auth.set_pin("1234")
        assert _live(ws) is False


def test_touch_slides_the_session_window(ext_env, monkeypatch):
    c, _ = ext_env
    KNOBS.handler_mode = "hold"
    install()
    tid = thread_for(c)
    unlock(c)
    seen = []
    monkeypatch.setattr(auth, "touch_session", lambda tok: seen.append(tok))
    with c.websocket_connect(f"/ws/voice/{tid}", headers=ORIGIN) as ws:
        ws.receive_text()
        ws.send_text("touch")
        assert ws.receive_text() == "touched"
    assert seen and seen[0] == KNOBS.sockets[0].token


# --------------------------------------------------------------------------- #
# The unit's --ws-max-size 262144 vs the main /ws message cap (hardening M5)
# --------------------------------------------------------------------------- #


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live_server(tmp_path):
    port = _free_port()
    env = {**os.environ, "DISPATCH_DATA_DIR": str(tmp_path / "data"), "DISPATCH_GATEWAY_WS": "0",
           "DISPATCH_MIRROR": "0", "DISPATCH_EXTENSIONS": "off"}
    backend = Path(__file__).resolve().parents[1]
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(port), "--no-access-log", "--ws-max-size", "262144"],
        cwd=backend, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                pytest.fail("uvicorn exited: " + proc.stderr.read().decode()[-2000:])
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    break
            except OSError:
                time.sleep(0.2)
        else:
            pytest.fail("uvicorn did not come up")
        yield port
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_a_65536_char_cjk_send_passes_under_the_unit_frame_cap(live_server):
    from websockets.exceptions import ConnectionClosed
    from websockets.sync.client import connect

    url = f"ws://127.0.0.1:{live_server}/ws"
    text = "汉" * 65536                                  # 196,608 bytes of UTF-8
    with connect(url, max_size=None, open_timeout=10) as ws:
        hello = json.loads(ws.recv(timeout=10))
        assert hello["type"] == "hello"
        frame = json.dumps({"type": "send", "thread_id": "nope", "text": text,
                            "client_msg_id": "cjk-1"}, ensure_ascii=False)
        assert 196_608 < len(frame.encode()) <= 262_144
        ws.send(frame)
        got = [json.loads(ws.recv(timeout=10)) for _ in range(2)]
        # The frame was read and handled (the thread does not exist) — not
        # closed 1009 by the transport.
        assert {"type": "error", "thread_id": "nope", "message": "Thread not found"} in got
    # The control: a frame over the cap is closed by uvicorn before it is read.
    with connect(url, max_size=None, open_timeout=10) as ws:
        ws.recv(timeout=10)
        ws.send(json.dumps({"type": "send", "thread_id": "nope", "text": "汉" * 90_000},
                           ensure_ascii=False))
        with pytest.raises(ConnectionClosed) as e:
            ws.recv(timeout=10)
        assert e.value.rcvd is not None and e.value.rcvd.code == 1009


# --------------------------------------------------------------------------- #
# The global cap while an eviction is in flight (FR-hardening L2)
# --------------------------------------------------------------------------- #


def test_max_total_holds_while_a_replacement_is_evicting(monkeypatch):
    """The gate used to take the old socket out of the count, then await its
    eviction (up to 1 s), and only then count the new one: another session's
    handshake landing in that window was admitted — 3 open with max_total 2."""
    import asyncio
    from types import SimpleNamespace

    from starlette.websockets import WebSocketState

    from app import extensions as X

    class WS:
        def __init__(self, dev):
            self.headers = {"origin": "http://h", "host": "h"}
            self.cookies = {}
            self.query_params = {"d": dev}
            self.client_state = WebSocketState.CONNECTING
            self.closed = None

        async def accept(self):
            self.client_state = WebSocketState.CONNECTED

        async def close(self, code=1000, reason=None):
            self.closed = (code, reason)
            self.client_state = WebSocketState.DISCONNECTED

    release = None
    admitted = []

    async def handler(ws, sock):
        async def slow_replaced():
            await asyncio.sleep(0.6)             # the old session winds down
        sock.on_replaced(slow_replaced)
        await ws.accept()
        admitted.append(ws)
        await release.wait()

    spec = X._WsSpec("voice", "voice", handler, max_per_session=1, max_total=2)
    monkeypatch.setattr(X, "websocket_spec", lambda name: spec)
    monkeypatch.setattr(X.auth, "load", lambda: SimpleNamespace(pin_set=False))
    monkeypatch.setattr(X.config, "get_bot", lambda bid: SimpleNamespace(id="b", name="B", safe=False, voice="",
                                                                       voice_rp="", advisor=None, api=None))

    class DB:
        async def get_thread(self, tid):
            return SimpleNamespace(id=tid, bot_id="b")

    monkeypatch.setattr(X, "_hooks", X.Hooks(db=lambda: DB()))
    X._slots.clear()
    X._open_total.clear()

    async def scenario():
        nonlocal release
        release = asyncio.Event()
        a1, b1, a2, c1 = WS("device-aaaa"), WS("device-bbbb"), WS("device-aaaa"), WS("device-cccc")
        tasks = [asyncio.create_task(X.thread_websocket_endpoint(a1, "voice", "t1")),
                 asyncio.create_task(X.thread_websocket_endpoint(b1, "voice", "t1"))]
        await asyncio.sleep(0.05)
        assert X._open_total["voice"] == 2
        tasks.append(asyncio.create_task(X.thread_websocket_endpoint(a2, "voice", "t1")))   # evicts a1
        await asyncio.sleep(0.1)                                                       # mid-eviction
        tasks.append(asyncio.create_task(X.thread_websocket_endpoint(c1, "voice", "t1")))
        await asyncio.sleep(0.1)
        assert c1.closed is not None and c1.closed[0] == X.UNAVAILABLE, c1.closed
        assert "busy" in c1.closed[1]
        assert X._open_total["voice"] <= 2
        await asyncio.sleep(0.8)                                                       # eviction done
        assert X._open_total["voice"] == 2 and a2 in admitted and c1 not in admitted
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 5)
        assert X._open_total.get("voice", 0) == 0

    try:
        asyncio.run(scenario())
    finally:
        X._slots.clear()
        X._open_total.clear()
