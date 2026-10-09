"""Drive mode's extension seam (app/voice/registry.py).

A voice extension (cloning, the Voices GUI, per-bot voices) plugs in through
DISPATCH_VOICE_EXTENSIONS. These tests pin what DisPatch promises it:
its router is mounted behind the unlocked-tier gate whatever it declares, its
engine is selectable by name, a broken extension cannot stop the app, and the
resolver/ack/persona hooks fail soft.
"""
from __future__ import annotations

import sys
import types

import numpy as np
import pytest
from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from app.voice import persona, registry, routes, tts
from app.voice.config import VoiceSettings


@pytest.fixture(autouse=True)
def _clean_registry():
    registry.reset()
    yield
    registry.reset()
    for name in [m for m in sys.modules if m.startswith("fake_voice_ext")]:
        del sys.modules[name]


def install(name: str, setup) -> None:
    mod = types.ModuleType(name)
    mod.setup = setup
    sys.modules[name] = mod


def app_with(unlocked: bool, spec: str) -> TestClient:
    def require_full(request: Request) -> None:
        if not unlocked:
            raise HTTPException(403, "Unlock for full access")

    a = FastAPI()
    registry.mount_extensions(a, require_full, spec)
    return TestClient(a)


def ext_router():
    r = APIRouter(prefix="/api/voice/profiles")

    @r.get("")
    async def list_profiles():          # declares NO auth of its own
        return {"profiles": ["host"]}

    return r


def test_extension_router_is_unlocked_only_even_without_its_own_gate():
    install("fake_voice_ext_a", lambda api: api.include_router(ext_router()))
    assert app_with(False, "fake_voice_ext_a:setup").get("/api/voice/profiles").status_code == 403
    r = app_with(True, "fake_voice_ext_a:setup").get("/api/voice/profiles")
    assert r.status_code == 200 and r.json() == {"profiles": ["host"]}


def test_broken_extension_is_skipped_and_others_still_load():
    def boom(api):
        raise RuntimeError("bad extension")

    install("fake_voice_ext_bad", boom)
    install("fake_voice_ext_ok", lambda api: api.include_router(ext_router()))
    a = FastAPI()
    loaded = registry.mount_extensions(
        a, lambda r: None, "fake_voice_ext_bad:setup, nonexistent_pkg_xyz:setup ,fake_voice_ext_ok:setup")
    assert loaded == ["fake_voice_ext_ok:setup"]


def test_registered_engine_is_selected_by_name():
    class Eng:
        sample_rate = 22050
        sample_format = "pcm_s16le"
        channels = 1
        streaming = True

        def stream(self, text, profile_id=None, stop=None):
            yield np.zeros(10, "<i2")

        def synth(self, text, profile_id=None):
            return np.zeros(10, "<i2"), 22050

    install("fake_voice_ext_eng", lambda api: api.register_engine("cloner", lambda s: Eng()))
    registry.mount_extensions(FastAPI(), lambda r: None, "fake_voice_ext_eng:setup")
    eng = tts.build_engine(VoiceSettings(tts_engine="cloner"))
    assert isinstance(eng, Eng) and eng.sample_rate == 22050


def test_unknown_engine_is_an_error_not_a_silent_default():
    with pytest.raises(RuntimeError, match="unknown TTS engine"):
        tts.build_engine(VoiceSettings(tts_engine="nope"))


def test_hq_fallback_wraps_any_engine():
    class Local:
        sample_rate = 24000
        streaming = False

        def stream(self, text, profile_id=None, stop=None):
            yield np.ones(5, "<i2")

    registry.register_engine("local", lambda s: Local())
    eng = tts.build_engine(VoiceSettings(tts_engine="local", hq_url="http://127.0.0.1:9/", hq_timeout_s=0.2))
    assert isinstance(eng, tts.FallbackTTS)
    out = list(eng.stream("hi"))                 # HQ unreachable -> local voice
    assert len(out) == 1 and out[0].tolist() == [1] * 5
    assert eng._down_until > 0                   # and it stays on local for a while


def test_profile_ack_and_persona_hooks_fail_soft():
    assert registry.resolve_profile("main") is None
    assert persona.voice_hint("main") == persona.DEFAULT_PERSONA
    registry.set_profile_resolver(lambda bot: {"main": "host"}.get(bot))
    registry.set_ack_provider(lambda pid: [np.zeros(3, "<i2")] if pid == "host" else [])
    registry.set_persona_provider(lambda bot, pid: "Be brief." if pid == "host" else None)
    assert registry.resolve_profile("main") == "host" and registry.resolve_profile("x") is None
    assert len(registry.acks_for("host")) == 1 and registry.acks_for(None) is None
    assert persona.voice_hint("main", "host") == "Be brief."
    assert persona.voice_hint("x", None) == persona.DEFAULT_PERSONA

    def boom(*a):
        raise RuntimeError("down")

    registry.set_profile_resolver(boom); registry.set_ack_provider(boom); registry.set_persona_provider(boom)
    assert registry.resolve_profile("main") is None and registry.acks_for("host") is None
    assert persona.voice_hint("main", "host") == persona.DEFAULT_PERSONA


def test_voice_for_bot_prefers_extension_acks_and_renders_defaults_otherwise():
    class Eng:
        sample_rate = 24000

        def __init__(self):
            self.calls = []

        def synth(self, text, profile_id=None):
            self.calls.append(profile_id)
            return np.zeros(4, "<i2"), 24000

    registry.set_profile_resolver(lambda bot: "host" if bot == "main" else None)
    registry.set_ack_provider(lambda pid: [np.ones(2, "<i2")] if pid == "host" else None)
    eng = Eng()
    pid, acks = routes._voice_for_bot(VoiceSettings(), eng, "main")
    assert pid == "host" and len(acks) == 1 and eng.calls == []
    routes._ACKS.clear()
    pid, acks = routes._voice_for_bot(VoiceSettings(), eng, "other")
    import time
    for _ in range(50):
        if len(acks) == len(routes.ACK_LINES):
            break
        time.sleep(0.02)
    assert pid is None and len(acks) == len(routes.ACK_LINES)


def test_status_route_is_unlocked_only():
    def require_full(request: Request) -> None:
        raise HTTPException(403, "Unlock for full access")

    a = FastAPI()
    routes.mount_http(a, require_full)
    assert TestClient(a).get("/api/voice/status").status_code == 403
    b = FastAPI()
    routes.mount_http(b, lambda r: None)
    st = TestClient(b).get("/api/voice/status").json()
    assert "enabled" in st and "missing" in st and "extensions" in st
