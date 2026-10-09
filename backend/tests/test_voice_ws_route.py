"""Drive mode against the REAL app: /ws/voice/{thread} and /api/voice/*.

These need main.py to carry the voice integration (docs/voice-drive-mode.md,
"Integration patch"). Until it is applied the whole module skips — on purpose,
so the voice package can land before the wiring does.

Hermetic like test_auth_gate.py: throwaway data dir and DB, the speech
engines are fakes (no models, no torch), and run_agent_turn is replaced so no
agent or gateway is ever called.
"""
from __future__ import annotations

import asyncio
import json

import numpy as np
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app import auth, config, main
from app.database import Database

pytestmark = pytest.mark.skipif(
    not any(getattr(r, "path", "") == "/ws/voice/{thread_id}" for r in main.app.routes),
    reason="voice integration patch not applied to main.py yet")


class FakeSTT:
    text = "what is on my calendar"

    def transcribe(self, audio):
        return self.text


class FakeTTS:
    sample_rate = 24000

    def stream(self, text, voice=None, stop=None):
        yield np.zeros(240, "<i2")

    def synth(self, text, voice=None):
        return np.zeros(240, "<i2"), 24000


class OneTurnDetector:
    """Every audio chunk ends a turn (the real endpointing is unit-tested)."""

    def __init__(self):
        from types import SimpleNamespace

        from app.voice.vad_turn import EndpointerConfig
        self.ep = SimpleNamespace(cfg=EndpointerConfig(), turn_fn=None)

    def feed(self, pcm):
        from app.voice.vad_turn import TurnEnd
        return [TurnEnd(audio=np.zeros(16000, np.float32), reason="silence")]

    def reset(self):
        pass


@pytest.fixture
def voice_env(tmp_path, monkeypatch):
    for name, val in (("DATA_DIR", tmp_path), ("CONFIG_PATH", tmp_path / "config.yaml"),
                      ("MEDIA_DIR", tmp_path / "media"), ("FILES_DIR", tmp_path / "files"),
                      ("LOG_DIR", tmp_path / "logs"), ("BACKUP_DIR", tmp_path / "backups")):
        monkeypatch.setattr(config, name, val)
    monkeypatch.setattr(main, "MEDIA_DIR", tmp_path / "media")
    monkeypatch.setattr(main, "FILES_DIR", tmp_path / "files")
    monkeypatch.setattr(auth, "SECURITY_PATH", tmp_path / "security.yaml")
    monkeypatch.setattr(auth, "RECOVERY_PATH", tmp_path / "RECOVERY-CODE.txt")
    auth._sessions.clear(); auth._cache = None; auth._fail_count = 0; auth._fail_until = 0.0
    config._invalidate_bots_cache()
    temp_db = Database(tmp_path / "chats.db")
    monkeypatch.setattr(main, "db", temp_db)
    main._ACK_SEEN.clear(); main._delivered.clear(); main._thread_bot.clear()
    # Some older suites (test_recovery, test_mirror, test_media_dedup) swap
    # manager.broadcast for a no-op and never put it back. This test listens
    # for the REAL broadcast, so restore it for its own duration.
    from app.ws import ConnectionManager
    monkeypatch.setattr(main.manager, "broadcast",
                        ConnectionManager.broadcast.__get__(main.manager))

    # Voice: on, models "present", engines fake.
    monkeypatch.setenv("DISPATCH_VOICE", "1")
    from app.voice import config as vconfig
    from app.voice import routes as vroutes
    monkeypatch.setattr(vconfig.VoiceSettings, "missing", lambda self: [])

    async def fake_warm(settings):
        return FakeSTT(), FakeTTS()

    monkeypatch.setattr(vroutes, "warm_models", fake_warm)
    import app.voice.vad_turn as vt
    monkeypatch.setattr(vt, "build_detector", lambda s: OneTurnDetector())
    monkeypatch.setattr(vroutes, "_voice_for_bot", lambda s, e, b: (None, []))

    turns = []

    async def fake_turn(thread_id, bot_id, text, opts=None):
        turns.append((thread_id, bot_id, text, opts))
        await main._persist_and_broadcast_message(thread_id, "assistant", "Two meetings today. Want details?")

    monkeypatch.setattr(main, "run_agent_turn", fake_turn)
    c = TestClient(main.app)
    c.__enter__()
    yield c, turns
    c.__exit__(None, None, None)
    asyncio.run(temp_db.close())


def thread_for(c, bot_id="main"):
    r = c.post("/api/threads", json={"bot_id": bot_id})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def unlock(c):
    auth.set_pin("1234")
    assert c.post("/api/auth/unlock", json={"pin": "1234"}).status_code == 200


def refused(c, path, **kw):
    with pytest.raises(WebSocketDisconnect) as e:
        with c.websocket_connect(path, **kw) as ws:
            ws.receive_text()
    return e.value.code


def test_locked_device_is_refused(voice_env):
    c, _ = voice_env
    tid = thread_for(c)
    auth.set_pin("1234")
    c.cookies.clear()
    assert refused(c, f"/ws/voice/{tid}") == 1008


def test_cross_origin_handshake_is_refused_even_unlocked(voice_env):
    c, _ = voice_env
    tid = thread_for(c)
    unlock(c)
    assert refused(c, f"/ws/voice/{tid}", headers={"origin": "https://evil.example"}) == 1008


def test_safe_bot_thread_is_refused(voice_env):
    c, _ = voice_env
    unlock(c)
    tid = thread_for(c, "alpha")       # a Safe-Mode bot in the test roster
    assert refused(c, f"/ws/voice/{tid}") == 1008


def test_feature_off_is_refused(voice_env, monkeypatch):
    c, _ = voice_env
    unlock(c)
    tid = thread_for(c)
    monkeypatch.setenv("DISPATCH_VOICE", "0")
    assert refused(c, f"/ws/voice/{tid}") == 1013


def test_voice_api_is_unlocked_only(voice_env):
    c, _ = voice_env
    auth.set_pin("1234")
    c.cookies.clear()
    assert c.get("/api/voice/status").status_code == 403
    unlock(c)
    st = c.get("/api/voice/status")
    assert st.status_code == 200 and st.json()["enabled"] is True


def test_spoken_turn_lands_in_thread_and_reply_is_spoken(voice_env):
    c, turns = voice_env
    unlock(c)
    tid = thread_for(c)
    with c.websocket_connect(f"/ws/voice/{tid}", headers={"origin": "http://testserver"}) as ws:
        assert json.loads(ws.receive_text())["type"] == "ready"
        ws.send_text(json.dumps({"type": "start"}))
        ws.send_bytes(b"\x00\x00" * 320)
        seen, audio = [], 0
        for _ in range(40):
            m = ws.receive()
            if m.get("bytes") is not None:
                audio += 1
                if audio >= 1:
                    break
                continue
            seen.append(json.loads(m["text"]))
        types = [f["type"] for f in seen]
        assert "eot" in types and "transcript" in types and audio >= 1
        hdr = [f for f in seen if f["type"] == "audio" and f.get("kind") == "reply"]
        assert hdr and hdr[0]["text"].startswith("Two meetings today")
    # The spoken message is a normal user row, tagged voice.
    msgs = c.get(f"/api/threads/{tid}/messages").json()
    rows = msgs["messages"] if isinstance(msgs, dict) else msgs
    user = [m for m in rows if m["role"] == "user"]
    assert user and user[0]["content"] == "what is on my calendar"
    assert user[0]["metadata"]["voice"] is True
    # The agent got the voice persona and the no-thinking override.
    (_, bot_id, text, opts) = turns[0]
    assert bot_id == "main" and text == "what is on my calendar"
    assert opts.voice_hint and "driving" in opts.voice_hint and opts.thinking == "off"


def test_compose_agent_text_prepends_voice_hint(voice_env):
    c, _ = voice_env
    unlock(c)
    tid = thread_for(c)

    async def go():
        return await main._compose_agent_text(tid, "hello", main.TurnOptions(voice_hint="[Voice] short."))

    out = c.portal.call(go) if hasattr(c, "portal") and c.portal else asyncio.run(go())
    assert out.startswith("[Voice] short.\n\nhello")


def test_retract_only_touches_voice_rows(voice_env):
    c, _ = voice_env
    unlock(c)
    tid = thread_for(c)
    typed = c.post(f"/api/threads/{tid}/messages", json={"content": "typed"})
    async def go():
        voice = await main.db.add_message(tid, "user", "spoken", metadata={"voice": True})
        rows = await main.db.dump_messages(tid)
        typed_id = [m.id for m in rows if m.content == "typed"]
        for mid in [*typed_id, voice.id]:
            await main._voice_retract(tid, mid)
        return [m.content for m in await main.db.dump_messages(tid)]
    left = c.portal.call(go)
    assert "spoken" not in left
    if typed.status_code == 200:
        assert "typed" in left


def _voices_mounted() -> bool:
    from app.voice import routes as vroutes
    return "dispatch-voice" in vroutes._LOADED


_VOICES_MOUNTED = _voices_mounted()


@pytest.mark.skipif(not _VOICES_MOUNTED, reason="dispatch-voice not installed (Voices panel not mounted)")
@pytest.mark.parametrize("method,path", [
    ("get", "/api/voices"), ("get", "/api/voices/ui"), ("get", "/api/voices/ui/voices.js"),
    ("get", "/api/voices/consent-kinds"), ("post", "/api/voices"), ("post", "/api/voices/analyse"),
    ("delete", "/api/voices/some-voice"),
])
def test_voices_panel_refuses_locked_devices(voice_env, method, path):
    c, _ = voice_env
    auth.set_pin("1234")
    c.cookies.clear()
    r = getattr(c, method)(path)
    assert r.status_code in (401, 403), (path, r.status_code)


@pytest.mark.skipif(not _VOICES_MOUNTED, reason="dispatch-voice not installed (Voices panel not mounted)")
def test_voices_panel_page_serves_when_unlocked(voice_env):
    c, _ = voice_env
    unlock(c)
    r = c.get("/api/voices/ui")
    assert r.status_code == 200 and "<html" in r.text.lower()
    assert c.get("/api/voice/status").json()["voices_ui"] == "/api/voices/ui"


def test_voice_status_refuses_locked_devices(voice_env):
    c, _ = voice_env
    auth.set_pin("1234")
    c.cookies.clear()
    assert c.get("/api/voice/status").status_code in (401, 403)
