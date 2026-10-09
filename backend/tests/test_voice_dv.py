"""The dispatch-voice adapter (app/voice/dv.py): cloned voices + Voices GUI.

Skips entirely when the optional ``dispatch-voice`` package is not installed.
The Chatterbox engine is replaced with a fake, so no model is loaded and the
tests stay fast; the real engine's own tests live in the dispatch-voice repo.
"""
from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import numpy as np
import pytest

pytest.importorskip("dispatch_voice")

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from app import config
from app.voice import dv, registry, tts
from app.voice.config import VoiceSettings


class FakeChatterbox:
    sample_rate = 24000
    dtype = "fake"
    watermarking = False

    def __init__(self):
        self.calls = []

    def stream(self, text_chunks, voice, stop=None):
        self.calls.append(("".join(text_chunks), voice))
        yield np.zeros(2400, np.float32)            # 0.1 s -> 3 frames of 40 ms...
        yield np.zeros(2400, np.float32)


@pytest.fixture
def dv_env(tmp_path, monkeypatch):
    registry.reset()
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(dv, "_STORE", None)
    fake = FakeChatterbox()
    monkeypatch.setattr(dv.LazyEngine, "_get", lambda self: fake)
    monkeypatch.setattr(dv, "available", lambda: True)
    yield SimpleNamespace(tmp=tmp_path, engine=fake)
    registry.reset()


def make_profile(st, pid: str, default: bool = False) -> None:
    """A profile directory as dispatch-voice writes it, minus the clip."""
    d = st.root / pid
    d.mkdir(parents=True)
    from dispatch_voice.engine import VoiceTensors
    VoiceTensors(np.zeros((1, 2, 4), np.float32), np.zeros((1, 3), np.int64),
                 np.zeros((1, 192), np.float32), np.zeros((1, 5, 80), np.float32)).save(d / "voice.npz")
    (d / "profile.json").write_text(json.dumps({"id": pid, "name": pid, "default": default}))
    (d / "fillers").mkdir()
    import soundfile as sf
    sf.write(d / "fillers" / "0.wav", np.full(480, 0.1, np.float32), 24000)


def app_with(unlocked: bool) -> TestClient:
    def require_operator(request: Request) -> None:
        if not unlocked:
            raise HTTPException(403, "Unlock for full access")

    a = FastAPI()
    assert dv.install(a, require_operator)
    return TestClient(a)


@pytest.mark.parametrize("path", ["/api/voices", "/api/voices/ui", "/api/voices/ui/voices.js",
                                  "/api/voices/consent-kinds"])
def test_voices_panel_is_unlocked_only(dv_env, path):
    assert app_with(False).get(path).status_code == 403
    assert app_with(True).get(path).status_code == 200


def test_voices_writes_are_unlocked_only(dv_env):
    c = app_with(False)
    assert c.post("/api/voices/analyse", files={"file": ("a.wav", b"RIFF", "audio/wav")}).status_code == 403
    assert c.post("/api/voices", data={"name": "x", "consent": "own"},
                  files={"file": ("a.wav", b"RIFF", "audio/wav")}).status_code == 403


def test_profile_resolution_bot_key_then_default(dv_env, monkeypatch):
    st = dv.store()
    assert st.root == dv_env.tmp / "voices"
    assert dv.resolve_profile("main") is None                 # nothing yet
    make_profile(st, "host", default=True)
    make_profile(st, "pirate")
    bots = {"main": SimpleNamespace(voice="pirate"), "beta": SimpleNamespace(voice="gone"),
            "Scout": SimpleNamespace(voice="")}
    monkeypatch.setattr(config, "get_bot", lambda b: bots.get(b))
    assert dv.resolve_profile("main") == "pirate"
    assert dv.resolve_profile("beta") == "host"               # missing profile -> default
    assert dv.resolve_profile("Scout") == "host"


def test_engine_streams_pcm_frames_and_falls_back_without_a_voice(dv_env):
    eng = dv.ChatterboxTTS()

    class Fallback:
        sample_rate = 24000

        def stream(self, text, profile_id=None, stop=None):
            yield np.ones(10, "<i2")

    chain = tts.FallbackTTS(eng, Fallback(), cooldown_s=30)
    out = list(chain.stream("Hello there, how are you?"))     # no profile at all
    assert len(out) == 1 and out[0][0] == 1 and chain._down_until == 0.0
    make_profile(dv.store(), "host", default=True)
    out = list(chain.stream("Hello there, how are you?"))
    assert out and all(f.dtype == np.dtype("<i2") for f in out)
    assert sum(len(f) for f in out) == 4800                   # 2 x 0.1 s at 24 kHz
    assert all(len(f) <= 960 for f in out)                    # 40 ms frames


def test_stop_event_ends_the_stream(dv_env):
    make_profile(dv.store(), "host", default=True)
    stop = threading.Event(); stop.set()
    assert list(dv.ChatterboxTTS().stream("Hello there, how are you?", None, stop)) == []


def test_fillers_come_from_the_profile(dv_env):
    st = dv.store()
    make_profile(st, "host", default=True)
    f = dv.fillers("host")
    assert len(f) == 1 and f[0].dtype == np.dtype("<i2") and len(f[0]) == 480
    assert dv.fillers(None) is None and dv.fillers("nope") is None


def test_install_registers_engine_resolver_and_acks(dv_env):
    app_with(True)
    assert registry.engine_factory("chatterbox") is not None
    make_profile(dv.store(), "host", default=True)
    assert registry.resolve_profile("anyone") == "host"
    assert registry.acks_for("host")
    eng = tts.build_engine(VoiceSettings(tts_engine="auto"))
    inner = eng.primary if isinstance(eng, tts.FallbackTTS) else eng
    assert isinstance(inner, dv.ChatterboxTTS)


def test_not_available_means_nothing_mounted(monkeypatch):
    registry.reset()
    monkeypatch.setattr(dv, "available", lambda: False)
    a = FastAPI()
    assert dv.install(a, lambda r: None) is False
    assert not any(getattr(r, "path", "").startswith("/api/voices") for r in a.routes)
    assert registry.engine_factory("chatterbox") is None
