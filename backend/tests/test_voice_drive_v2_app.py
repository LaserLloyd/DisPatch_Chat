"""Drive mode v2 against the REAL app: /api/voice/settings, /api/voice/preview,
/api/voice/status additions, per-bot voice on the Bots save, the abort hook
(review M3) and the engine-side fixes (opt-in disk cache, M2, L6, H5).

Hermetic: throwaway data dir (voice_env from test_voice_ws_route), fake
engines, a fake built-in voice provider (what the optional Kokoro plugin
registers), no models.
"""
from __future__ import annotations

import asyncio
import io
import threading
import time
import wave
from types import SimpleNamespace

import numpy as np
import pytest
from test_voice_ws_route import FakeTTS, thread_for, unlock, voice_env

from app import auth, config, main
from app.voice import config as vconfig
from app.voice import registry
from app.voice import routes as vroutes
from app.voice import tts as vtts

BUILTINS = [{"id": "builtin:af_heart", "name": "Heart (US, female)", "lang": "en-us"},
            {"id": "builtin:am_michael", "name": "Michael (US, male)", "lang": "en-us"}]


@pytest.fixture
def app_voice(voice_env, monkeypatch):
    c, turns = voice_env
    monkeypatch.setattr(registry, "_builtin_list", lambda settings: BUILTINS)
    eng = FakeTTS()
    eng.calls = []
    orig_synth = eng.synth

    def synth(text, voice=None):
        eng.calls.append((text, voice))
        return orig_synth(text, voice)

    eng.synth = synth
    monkeypatch.setattr(vtts, "shared_engine", lambda settings: eng)
    vroutes._PREVIEWS.clear()
    return c, turns, eng


# --------------------------------------------------------------------------- #
# /api/voice/settings
# --------------------------------------------------------------------------- #

def test_settings_round_trip_persist_and_apply_live(app_voice):
    c, _, _ = app_voice
    unlock(c)
    r = c.get("/api/voice/settings")
    assert r.status_code == 200
    # voice_env forces voice on with DISPATCH_VOICE=1: the switch reads on
    # and says who decided it.
    assert r.json() == {"enabled": True, "forced": "on",
                        "default_voice": "builtin:af_heart", "speed": 1.05,
                        "voice_turn_thinking": "off", "merge_window_s": 1.8,
                        "hard_silence_s": 1.4}
    r = c.patch("/api/voice/settings", json={"speed": 1.2, "voice_turn_thinking": "low",
                                             "default_voice": "builtin:am_michael"})
    assert r.status_code == 200, r.text
    assert r.json() == {"enabled": True, "forced": "on",
                        "default_voice": "builtin:am_michael", "speed": 1.2,
                        "voice_turn_thinking": "low", "merge_window_s": 1.8,
                        "hard_silence_s": 1.4}
    f = config.DATA_DIR / "voice.yaml"
    assert f.exists() and (f.stat().st_mode & 0o777) == 0o600
    st = vconfig.load_settings()                       # what the next turn reads
    assert (st.builtin_voice, st.speed, st.voice_turn_thinking) == ("am_michael", 1.2, "low")
    import yaml
    assert yaml.safe_load(f.read_text()) == {"builtin_voice": "am_michael", "speed": 1.2,
                                             "voice_turn_thinking": "low"}
    assert c.get("/api/voice/settings").json()["speed"] == 1.2


def test_settings_reach_the_next_voice_turn(app_voice):
    c, turns, _ = app_voice
    unlock(c)
    assert c.patch("/api/voice/settings", json={"voice_turn_thinking": "medium"}).status_code == 200
    tid = thread_for(c)

    async def go():
        await main._voice_submit(tid, "hello there", {"reply_style": "short", "follow_up": False})
        for _ in range(50):
            if turns:
                return
            await asyncio.sleep(0.01)

    c.portal.call(go)
    (_, _, _, opts) = turns[-1]
    assert opts.thinking == "medium"
    assert "one short spoken sentence" in opts.voice_hint
    assert "do not end with a question" in opts.voice_hint


@pytest.mark.parametrize("body", [
    {"speed": 2.0}, {"speed": "fast"}, {"speed": True}, {"voice_turn_thinking": "high"},
    {"default_voice": "builtin:nope"}, {"default_voice": "no-such-profile"},
    {"default_voice": ""}, {"volume": 3},
])
def test_settings_reject_bad_values(app_voice, body):
    c, _, _ = app_voice
    unlock(c)
    assert c.patch("/api/voice/settings", json=body).status_code == 422
    assert not (config.DATA_DIR / "voice.yaml").exists()


def test_settings_and_preview_are_unlocked_only(app_voice):
    c, _, _ = app_voice
    auth.set_pin("1234")
    c.cookies.clear()
    assert c.get("/api/voice/settings").status_code in (401, 403)
    assert c.patch("/api/voice/settings", json={"speed": 1.0}).status_code in (401, 403)
    assert c.get("/api/voice/preview", params={"voice": "builtin:af_heart"}).status_code in (401, 403)


def test_new_voice_routes_are_on_the_safe_mode_block_list():
    # The middleware refuses /api/voice* for Safe Mode before any route runs.
    src = (main.__file__ and open(main.__file__).read())
    assert '"/api/voice", "/api/voices",' in src


# --------------------------------------------------------------------------- #
# /api/voice/preview and /api/voice/status
# --------------------------------------------------------------------------- #

def test_preview_returns_a_wav_and_caches_it(app_voice):
    c, _, eng = app_voice
    unlock(c)
    r = c.get("/api/voice/preview", params={"voice": "builtin:am_michael"})
    assert r.status_code == 200 and r.headers["content-type"] == "audio/wav"
    with wave.open(io.BytesIO(r.content)) as w:
        assert w.getframerate() == 24000 and w.getnchannels() == 1
    assert eng.calls == [(vroutes.PREVIEW_TEXT, "builtin:am_michael")]
    c.get("/api/voice/preview", params={"voice": "builtin:am_michael"})
    assert len(eng.calls) == 1                         # cached per voice + speed


def test_preview_validates_voice_and_text(app_voice):
    c, _, _ = app_voice
    unlock(c)
    assert c.get("/api/voice/preview", params={"voice": "builtin:nope"}).status_code == 422
    assert c.get("/api/voice/preview", params={"voice": ""}).status_code == 422
    assert c.get("/api/voice/preview", params={"voice": "builtin:af_heart",
                                               "text": "x" * 121}).status_code == 422
    assert c.get("/api/voice/preview", params={"voice": "builtin:af_heart",
                                               "text": "Short line."}).status_code == 200


def test_status_lists_what_settings_need(app_voice):
    c, _, _ = app_voice
    unlock(c)
    st = c.get("/api/voice/status").json()
    for k in ("default_voice", "builtin_voices", "profiles", "speed", "voice_turn_thinking",
              "ready", "missing", "enabled"):
        assert k in st, k
    assert st["builtin_voices"] == BUILTINS and st["default_voice"] == "builtin:af_heart"


# --------------------------------------------------------------------------- #
# Per-bot voice on the Bots save
# --------------------------------------------------------------------------- #

def test_bot_voice_is_saved_validated_and_admin_only(app_voice):
    c, _, _ = app_voice
    unlock(c)
    r = c.put("/api/bots/order", json={"bots": [{"id": "main", "voice": "builtin:am_michael"}]})
    assert r.status_code == 200, r.text
    assert next(b for b in r.json()["bots"] if b["id"] == "main")["voice"] == "builtin:am_michael"
    assert config.get_bot("main").voice == "builtin:am_michael"
    allb = c.get("/api/bots/all").json()["bots"]
    assert next(b for b in allb if b["id"] == "main")["voice"] == "builtin:am_michael"
    # The roster other screens get (incl. Safe Mode for safe bots) never carries it.
    assert "voice" not in config.get_bot("main").to_dict()
    assert c.put("/api/bots/order", json={"bots": [{"id": "main", "voice": "who-knows"}]}).status_code == 422
    assert config.get_bot("main").voice == "builtin:am_michael"
    # "" = back to the default voice; an absent key leaves it alone.
    assert c.put("/api/bots/order", json={"bots": [{"id": "main"}]}).status_code == 200
    assert config.get_bot("main").voice == "builtin:am_michael"
    assert c.put("/api/bots/order", json={"bots": [{"id": "main", "voice": ""}]}).status_code == 200
    assert config.get_bot("main").voice == ""


def test_a_bots_builtin_voice_resolves_without_a_profile_extension(app_voice):
    from app.voice import registry
    c, _, _ = app_voice
    unlock(c)
    c.put("/api/bots/order", json={"bots": [{"id": "main", "voice": "builtin:am_michael"}]})
    saved = registry._profile_resolver
    registry._profile_resolver = None
    try:
        assert registry.resolve_profile("main") == "builtin:am_michael"
    finally:
        registry._profile_resolver = saved


# --------------------------------------------------------------------------- #
# M3: the abort hook stops API/advisor turns and queued voice turns
# --------------------------------------------------------------------------- #

def _bot(**kw):
    base = {"api": None, "advisor": None, "agent_id": "x"}
    base.update(kw)
    return SimpleNamespace(**base)


async def _abort_scenario(monkeypatch, bot):
    gate = asyncio.Event()

    async def turn():
        await gate.wait()

    running = asyncio.create_task(turn())
    queued = asyncio.create_task(turn())
    await asyncio.sleep(0)
    tid = "t-abort"
    main._voice_turns[tid] = [(running, "u1"), (queued, "u2")]
    main._turn_holder[tid] = running
    gw = []

    async def fake_abort(thread_id):
        gw.append(thread_id)
        return {"ok": True}

    async def get_thread(thread_id):
        return SimpleNamespace(bot_id="b")

    monkeypatch.setattr(main, "_abort_thread_turn", fake_abort)
    monkeypatch.setattr(main.db, "get_thread", get_thread)
    monkeypatch.setattr(main.config, "get_bot", lambda b: bot)
    await main._voice_abort(tid)
    await asyncio.sleep(0)
    out = (running.cancelled() or running.cancelling(), queued.cancelled() or queued.cancelling(), gw)
    gate.set()
    for t in (running, queued):
        with pytest.raises(asyncio.CancelledError) if t.cancelling() else _null():
            await t
    main._voice_turns.pop(tid, None)
    main._turn_holder.pop(tid, None)
    return out


class _null:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


async def test_abort_cancels_a_running_api_turn_and_the_queued_ones(monkeypatch):
    run_c, queued_c, gw = await _abort_scenario(monkeypatch, _bot(api={"provider": "x"}))
    assert run_c and queued_c and gw == []


async def test_abort_cancels_queued_voice_turns_and_stops_the_gateway_run(monkeypatch):
    run_c, queued_c, gw = await _abort_scenario(monkeypatch, _bot())
    assert not run_c and queued_c and gw == ["t-abort"]


async def test_gateway_abort_uses_the_agent_seat(monkeypatch):
    """jobboard -> scout: the abort must name scout's session, not jobboard's."""
    keys = []

    class Client:
        connected = SimpleNamespace(is_set=lambda: True)

        async def abort_run(self, session_key, run_id):
            keys.append(session_key)

    async def get_thread(thread_id):
        return SimpleNamespace(bot_id="jobboard")

    monkeypatch.setattr(main, "_gateway_client", Client())
    monkeypatch.setattr(main.db, "get_thread", get_thread)
    monkeypatch.setattr(main.config, "get_bot", lambda b: _bot(agent_id="scout"))
    await main._abort_thread_turn("t-1")
    assert keys == [main.openclaw.session_key_for("scout", "t-1")]


# --------------------------------------------------------------------------- #
# Engines: downloader, background renders yield (M2), opt-in cache, L6, H5
# --------------------------------------------------------------------------- #

def test_downloader_no_longer_fetches_kokoro(capsys):
    from app.voice import download
    names = [rel for rel, _, _ in download.files_for()]
    assert names and not any("kokoro" in n for n in names)
    assert {"silero_vad.onnx", "smart-turn-v3.2-cpu.onnx"} <= set(names)


def test_background_renders_wait_for_reply_synthesis():
    act = vtts._Activity()
    act.enter()
    done = threading.Event()

    def bg():
        act.wait_idle(quiet_s=0.05)
        done.set()

    threading.Thread(target=bg, daemon=True).start()
    time.sleep(0.15)
    assert not done.is_set()                 # a reply is being synthesised
    act.exit()
    assert done.wait(1.0)


class CachedEngine:
    """An engine that opts in to the disk cache, like the Kokoro plugin."""
    sample_rate = 24000

    def __init__(self):
        self.n = 0

    def cache_tag(self, profile_id):
        if profile_id and not vtts.builtin_id(profile_id):
            return None
        return f"fake-{vtts.builtin_id(profile_id) or 'default'}-1.050"

    def synth(self, text, profile_id=None):
        self.n += 1
        return np.full(10, 7, "<i2"), 24000


def test_opt_in_engine_renders_are_cached_on_disk(voice_env):
    k = CachedEngine()
    st = vconfig.VoiceSettings(flag="1")
    a = vroutes._render_cached(st, k, None, "Got it.", wait_idle=False)
    b = vroutes._render_cached(st, k, None, "Got it.", wait_idle=False)
    assert k.n == 1 and list(a) == list(b) == [7] * 10
    d = vroutes._cache_dir(st, k, None)
    assert d.name == "fake-default-1.050"
    assert d.is_relative_to(config.DATA_DIR) and list(d.glob("*.npy"))
    # Found through a FallbackTTS chain too; a cloned profile is never cached.
    chain = vtts.FallbackTTS(SimpleNamespace(sample_rate=24000), k)
    assert vroutes._cache_dir(st, chain, "builtin:am_michael").name == "fake-am_michael-1.050"
    assert vroutes._cache_dir(st, chain, "some-cloned-profile") is None


def test_engines_without_cache_tag_are_not_cached(voice_env):
    class Plain:
        sample_rate = 24000
        n = 0

        def synth(self, text, profile_id=None):
            Plain.n += 1
            return np.zeros(4, "<i2"), 24000

    st = vconfig.VoiceSettings(flag="1")
    assert vroutes._cache_dir(st, Plain(), None) is None
    vroutes._render_cached(st, Plain(), None, "Got it.", wait_idle=False)
    vroutes._render_cached(st, Plain(), None, "Got it.", wait_idle=False)
    assert Plain.n == 2 and not (config.DATA_DIR / "voice-cache").exists()


def test_fallback_with_builtin_or_unknown_voice_has_no_cooldown():
    """L6: a voice the cloned engine does not know is not an engine failure."""
    class Primary:
        sample_rate = 24000
        streaming = True

        def stream(self, text, profile_id=None, stop=None):
            raise LookupError("not mine")
            yield  # pragma: no cover

    class Kok:
        sample_rate = 24000

        def stream(self, text, profile_id=None, stop=None):
            yield np.ones(3, "<i2")

    f = vtts.FallbackTTS(Primary(), Kok(), cooldown_s=30.0)
    assert len(list(f.stream("Hi.", "builtin:am_michael"))) == 1
    assert f._down_until == 0.0


def test_prewarm_is_a_no_op_when_voice_is_off():
    assert vroutes.prewarm(vconfig.VoiceSettings(flag="0")) is False
    assert vroutes.prewarm(vconfig.VoiceSettings(flag="1", prewarm=False)) is False


def test_prewarm_loads_engines_and_runs_a_warm_synthesis(monkeypatch):
    calls = []

    class STT:
        def transcribe(self, a):
            calls.append("stt")
            return ""

    class K:
        sample_rate = 24000

        def warm(self):
            self.synth("Okay.")

        def synth(self, text, profile_id=None):
            calls.append(("tts", text))
            return np.zeros(4, "<i2"), 24000

    from app.voice import stt as vstt
    from app.voice import vad_turn
    monkeypatch.setattr(vconfig.VoiceSettings, "missing", lambda self: [])
    monkeypatch.setattr(vstt, "shared_engine", lambda s: STT())
    monkeypatch.setattr(vtts, "shared_engine", lambda s: K())
    monkeypatch.setattr(vad_turn, "_shared_smart_turn", lambda s: None)
    monkeypatch.setattr(vad_turn, "build_detector", lambda s: None)
    monkeypatch.setattr(vroutes, "_voice_for_bot", lambda s, e, b: (None, []))
    assert vroutes.prewarm(vconfig.VoiceSettings(flag="1")) is True
    assert "stt" in calls and ("tts", "Okay.") in calls
