"""Voice as an optional feature: the Settings › Voice switch and the opt-in
voice tools (Voice cloning, Built-in voices, Voice lab).

* The switch (``enabled`` in <data>/voice.yaml) is OFF on a fresh install,
  even with every package and model present. ``DISPATCH_VOICE=1`` / ``0``
  overrides it, and then the API refuses to move it (409).
* It applies live: off ends open Drive sessions with 1013 and drops the
  loaded speech models; on starts the prewarm. Nothing is preloaded while off.
* /ws/voice refuses with 1013 and a reason that names the switch.
* The Voice lab's new settings (merge window, hard silence) are validated and
  can be reset to the server default.
* The voice tools are builtins that are off without a tools.yaml row, never
  listed for Safe Mode, and their routes 404 while off.

Hermetic: voice_env from test_voice_ws_route (throwaway data dir, fake
engines, no models).
"""
from __future__ import annotations

import base64
import threading

import numpy as np
import pytest
import yaml
from starlette.websockets import WebSocketDisconnect
from test_voice_ws_route import FakeSTT, FakeTTS, thread_for, unlock, voice_env

from app import auth, config, main, tools
from app.voice import config as vconfig
from app.voice import routes as vroutes


@pytest.fixture
def auto_env(voice_env, monkeypatch):
    """voice_env, but DISPATCH_VOICE unset: the switch decides."""
    monkeypatch.delenv("DISPATCH_VOICE", raising=False)
    tools._reset_state()
    return voice_env


def write_tools(rows: str) -> None:
    (config.DATA_DIR / "tools.yaml").write_text(f"tools:\n{rows}")
    tools._reset_state()


def voice_yaml() -> dict:
    p = config.DATA_DIR / "voice.yaml"
    return yaml.safe_load(p.read_text()) if p.exists() else {}


# --------------------------------------------------------------------------- #
# The switch: default, env override, persistence
# --------------------------------------------------------------------------- #

def test_fresh_install_is_off_even_when_everything_is_installed(monkeypatch, tmp_path):
    monkeypatch.delenv("DISPATCH_VOICE", raising=False)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(vconfig.VoiceSettings, "missing", lambda self: [])
    st = vconfig.load_settings()
    assert (st.forced, st.switch, st.switched_on, st.enabled) == (None, False, False, False)
    s = st.status()
    assert s["enabled"] is False and s["switch"] is False and s["ready"] is True


@pytest.mark.parametrize("flag,forced,on", [
    ("1", "on", True), ("true", "on", True), ("yes", "on", True), ("ON", "on", True),
    ("0", "off", False), ("false", "off", False), ("", "off", False), ("maybe", "off", False),
    ("auto", None, False),
])
def test_env_flag_forces_the_switch(monkeypatch, tmp_path, flag, forced, on):
    monkeypatch.setenv("DISPATCH_VOICE", flag)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(vconfig.VoiceSettings, "missing", lambda self: [])
    (tmp_path / "voice.yaml").write_text("enabled: true\n")
    st = vconfig.load_settings()
    assert st.forced == forced
    # The env wins over voice.yaml; with `auto` the file's switch decides.
    assert st.switched_on is (True if forced is None else on)
    assert st.enabled is (True if forced is None else on)


def test_switch_on_but_not_ready_is_not_enabled(monkeypatch, tmp_path):
    monkeypatch.delenv("DISPATCH_VOICE", raising=False)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    (tmp_path / "voice.yaml").write_text("enabled: true\n")
    monkeypatch.setattr(vconfig.VoiceSettings, "missing", lambda self: ["model silero_vad.onnx"])
    st = vconfig.load_settings()
    assert st.switched_on is True and st.enabled is False
    assert st.status()["switch"] is True and st.status()["enabled"] is False


def test_existing_install_migrates_by_adding_enabled_true(monkeypatch, tmp_path):
    """The live box's voice.yaml predates the switch (2.1 names). Adding one
    line turns voice back on and keeps the old preference."""
    monkeypatch.delenv("DISPATCH_VOICE", raising=False)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(vconfig.VoiceSettings, "missing", lambda self: [])
    (tmp_path / "voice.yaml").write_text("kokoro_speed: 1.3\n")
    assert vconfig.load_settings().enabled is False
    (tmp_path / "voice.yaml").write_text("kokoro_speed: 1.3\nenabled: true\n")
    st = vconfig.load_settings()
    assert st.enabled is True and st.speed == 1.3


@pytest.mark.parametrize("raw", ["enabled: yes-please\n", "enabled: 1\n", "enabled: null\n"])
def test_a_junk_switch_value_reads_as_off(monkeypatch, tmp_path, raw):
    monkeypatch.delenv("DISPATCH_VOICE", raising=False)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    (tmp_path / "voice.yaml").write_text(raw)
    assert vconfig.load_settings().switch is False


def test_patch_enabled_persists_and_status_follows(auto_env, monkeypatch):
    c, _ = auto_env
    unlock(c)
    monkeypatch.setattr(vroutes, "apply_switch", _noop_apply)
    st = c.get("/api/voice/status").json()
    assert (st["enabled"], st["switch"], st["forced"]) == (False, False, None)
    r = c.patch("/api/voice/settings", json={"enabled": True})
    assert r.status_code == 200, r.text
    assert r.json()["enabled"] is True and r.json()["forced"] is None
    assert voice_yaml() == {"enabled": True}
    st = c.get("/api/voice/status").json()
    assert (st["enabled"], st["switch"]) == (True, True)
    assert c.patch("/api/voice/settings", json={"enabled": False}).json()["enabled"] is False
    assert voice_yaml() == {"enabled": False}


@pytest.mark.parametrize("bad", ["yes", 1, None, "true"])
def test_patch_enabled_must_be_a_bool(auto_env, bad):
    c, _ = auto_env
    unlock(c)
    assert c.patch("/api/voice/settings", json={"enabled": bad}).status_code == 422
    assert voice_yaml() == {}


def test_patch_enabled_refused_while_the_env_decides(voice_env, monkeypatch):
    c, _ = voice_env                      # DISPATCH_VOICE=1
    unlock(c)
    assert c.patch("/api/voice/settings", json={"enabled": False}).status_code == 409
    # Saying what is already true is not a change.
    assert c.patch("/api/voice/settings", json={"enabled": True}).status_code == 200
    monkeypatch.setenv("DISPATCH_VOICE", "0")
    assert c.patch("/api/voice/settings", json={"enabled": True}).status_code == 409
    assert c.get("/api/voice/status").json()["forced"] == "off"
    assert "enabled" not in voice_yaml()


# --------------------------------------------------------------------------- #
# Live: refusal, preload, release
# --------------------------------------------------------------------------- #

async def _noop_apply(was_on, settings):
    return None


def _close(c, path):
    with pytest.raises(WebSocketDisconnect) as e:
        with c.websocket_connect(path) as ws:
            ws.receive_text()
    return e.value


def test_ws_refuses_with_1013_naming_the_switch_when_off(auto_env):
    c, _ = auto_env
    unlock(c)
    tid = thread_for(c)
    e = _close(c, f"/ws/voice/{tid}")
    assert e.code == 1013
    assert e.reason == "Voice is turned off in Settings › Voice"


def test_ws_refusal_names_the_server_when_forced_off(voice_env, monkeypatch):
    c, _ = voice_env
    unlock(c)
    tid = thread_for(c)
    monkeypatch.setenv("DISPATCH_VOICE", "0")
    e = _close(c, f"/ws/voice/{tid}")
    assert e.code == 1013 and "on this server" in e.reason


def test_ws_off_still_refuses_locked_callers_with_1008(auto_env):
    """Only an authorised caller learns that voice is off."""
    c, _ = auto_env
    tid = thread_for(c)
    auth.set_pin("1234")
    c.cookies.clear()
    assert _close(c, f"/ws/voice/{tid}").code == 1008


def test_ws_works_after_switching_on(auto_env, monkeypatch):
    c, _ = auto_env
    unlock(c)
    monkeypatch.setattr(vroutes, "apply_switch", _noop_apply)
    tid = thread_for(c)
    assert c.patch("/api/voice/settings", json={"enabled": True}).status_code == 200
    with c.websocket_connect(f"/ws/voice/{tid}") as ws:
        ws.send_json({"type": "start"})
        seen = set()
        for _ in range(20):
            m = ws.receive()
            if m.get("text"):
                import json
                seen.add(json.loads(m["text"]).get("type"))
            if "ready" in seen:
                break
        assert "ready" in seen


def test_no_preload_while_off(monkeypatch, tmp_path):
    monkeypatch.delenv("DISPATCH_VOICE", raising=False)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(vconfig.VoiceSettings, "missing", lambda self: [])
    monkeypatch.setenv("DISPATCH_VOICE_PREWARM", "1")
    started = []
    monkeypatch.setattr(threading.Thread, "start", lambda self: started.append(self.name))
    main._voice_prewarm_start()
    assert "voice-prewarm" not in started
    assert vroutes.prewarm(vconfig.load_settings()) is False
    assert vroutes._PREWARM["state"] == "skipped"
    # Switched on: the start-up hook does start it.
    (tmp_path / "voice.yaml").write_text("enabled: true\n")
    main._voice_prewarm_start()
    assert "voice-prewarm" in started


def test_release_models_drops_every_cache(monkeypatch):
    from app.voice import stt, tts, vad_turn
    stt._ENGINES["x"] = object()
    tts._ENGINES["x"] = object()
    vad_turn._SMART_TURN["x"] = object()
    vroutes._ACKS["x"] = []
    vroutes._NOTICES[("x",)] = np.zeros(1)
    vroutes._PREVIEWS[("x",)] = b""
    vroutes._PREWARM.update(state="done", seconds=1.0)
    gen = vroutes._GEN[0]
    vroutes.release_models()
    assert not stt._ENGINES and not tts._ENGINES and not vad_turn._SMART_TURN
    assert not vroutes._ACKS and not vroutes._NOTICES and not vroutes._PREVIEWS
    assert vroutes._PREWARM == {"state": "idle"}
    assert vroutes._GEN[0] == gen + 1


def test_a_prewarm_overtaken_by_a_release_gives_its_models_back(monkeypatch):
    from app.voice import stt, tts, vad_turn

    class STT:
        def transcribe(self, a):
            vroutes.release_models()          # the switch went off mid-load
            stt._ENGINES["late"] = object()  # ...and this load finished after
            return ""

    class K:
        sample_rate = 24000

        def synth(self, text, profile_id=None):
            return np.zeros(4, "<i2"), 24000

    monkeypatch.setattr(vconfig.VoiceSettings, "missing", lambda self: [])
    monkeypatch.setattr(stt, "shared_engine", lambda s: STT())
    monkeypatch.setattr(tts, "shared_engine", lambda s: K())
    monkeypatch.setattr(vad_turn, "_shared_smart_turn", lambda s: None)
    monkeypatch.setattr(vad_turn, "build_detector", lambda s: None)
    assert vroutes.prewarm(vconfig.VoiceSettings(flag="1")) is False
    assert not stt._ENGINES
    assert vroutes._PREWARM["state"] == "idle"


def test_switching_off_ends_open_sessions_and_releases(monkeypatch):
    import asyncio

    ended, closed, released = [], [], []

    class Sess:
        def end(self, code, reason=""):
            ended.append((code, reason))

    class WS:
        async def close(self, code=1000, reason=None):
            closed.append(code)

    slot = vroutes._Slot(WS(), Sess())
    monkeypatch.setitem(vroutes._OPEN, "tok", [slot])
    monkeypatch.setattr(vroutes, "release_models", lambda: released.append(1))
    on = vconfig.VoiceSettings(switch=True)
    off = vconfig.VoiceSettings(switch=False)
    asyncio.run(vroutes.apply_switch(True, off))
    assert ended == [(1013, vroutes.OFF_SETTINGS)] and closed == [1013] and released == [1]
    assert slot.evicted
    # off → on starts the prewarm in the background (when voice can run).
    started = []
    monkeypatch.setattr(vconfig.VoiceSettings, "missing", lambda self: [])
    monkeypatch.setattr(threading.Thread, "start", lambda self: started.append(self.name))
    asyncio.run(vroutes.apply_switch(False, on))
    assert started == ["voice-prewarm"]
    # No change, nothing happens.
    asyncio.run(vroutes.apply_switch(True, on))
    assert started == ["voice-prewarm"] and released == [1]


def test_patch_off_runs_the_live_switch(auto_env, monkeypatch):
    c, _ = auto_env
    unlock(c)
    seen = []

    async def spy(was_on, settings):
        seen.append((was_on, settings.switched_on))

    monkeypatch.setattr(vroutes, "apply_switch", spy)
    c.patch("/api/voice/settings", json={"enabled": True})
    c.patch("/api/voice/settings", json={"enabled": False})
    assert seen == [(False, True), (True, False)]


# --------------------------------------------------------------------------- #
# The Voice lab's settings keys
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("body", [
    {"merge_window_s": 0.2}, {"merge_window_s": 9}, {"merge_window_s": "long"},
    {"merge_window_s": True}, {"hard_silence_s": 0.5}, {"hard_silence_s": 3.5},
    {"hard_silence_s": None}, {"reset": ["speed"]}, {"reset": "merge_window_s"},
    {"reset": [1]}, {"patience": 2},
])
def test_lab_settings_reject_bad_values(auto_env, body):
    c, _ = auto_env
    unlock(c)
    assert c.patch("/api/voice/settings", json=body).status_code == 422
    assert voice_yaml() == {}


def test_lab_settings_persist_apply_and_reset(auto_env):
    c, _ = auto_env
    unlock(c)
    r = c.patch("/api/voice/settings", json={"merge_window_s": 2.5, "hard_silence_s": 1.1,
                                             "voice_turn_thinking": "low"})
    assert r.status_code == 200, r.text
    assert (r.json()["merge_window_s"], r.json()["hard_silence_s"]) == (2.5, 1.1)
    st = vconfig.load_settings()                       # what the next session reads
    assert (st.merge_window_s, st.turn_hard_silence_s, st.voice_turn_thinking) == (2.5, 1.1, "low")
    assert voice_yaml() == {"merge_window_s": 2.5, "hard_silence_s": 1.1,
                            "voice_turn_thinking": "low"}
    r = c.patch("/api/voice/settings", json={"reset": ["merge_window_s", "hard_silence_s"]})
    assert (r.json()["merge_window_s"], r.json()["hard_silence_s"]) == (1.8, 1.4)
    assert voice_yaml() == {"voice_turn_thinking": "low"}


def test_lab_timings_reach_the_detector(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    (tmp_path / "voice.yaml").write_text("hard_silence_s: 2.2\nmerge_window_s: 3.0\n")
    from app.voice import vad_turn
    monkeypatch.setattr(vad_turn, "SileroVAD", lambda p: None)
    monkeypatch.setattr(vad_turn, "_shared_smart_turn", lambda s: None)
    st = vconfig.load_settings()
    det = vad_turn.build_detector(st)
    assert det.ep.cfg.turn_hard_silence_s == 2.2
    assert st.merge_window(1.0) == 3.0


# --------------------------------------------------------------------------- #
# The voice tools: opt-in builtins, unlocked only
# --------------------------------------------------------------------------- #

VOICE_TOOLS = ("voice-cloning", "voice-builtin", "voice-lab")


def _rows(c):
    return {t["id"]: t for t in c.get("/api/tools").json()["tools"]}


def test_voice_tools_are_off_without_a_tools_yaml_row(auto_env):
    c, _ = auto_env
    unlock(c)
    rows = _rows(c)
    for tid in VOICE_TOOLS:
        assert rows[tid]["kind"] == "builtin" and rows[tid]["enabled"] is False
        assert tools.builtin_resolved(tid) is False
    st = c.get("/api/voice/status").json()
    assert st["tools"] == dict.fromkeys(VOICE_TOOLS, False)
    assert c.get("/api/voice/lab").status_code == 404
    assert c.post("/api/voice/lab/test", content=b"\0" * 32000).status_code == 404


def test_voice_tools_switch_on_with_a_row(auto_env):
    c, _ = auto_env
    unlock(c)
    write_tools("- {id: voice-lab, kind: builtin, enabled: true}\n"
                "- {id: voice-builtin, kind: builtin, enabled: true}\n")
    rows = _rows(c)
    assert rows["voice-lab"]["enabled"] is True and rows["voice-lab"]["available"] is True
    assert rows["voice-builtin"]["enabled"] is True
    # Not installed here (no Kokoro plugin): the row says so; the pane explains.
    assert rows["voice-builtin"]["available"] is False
    assert rows["voice-cloning"]["enabled"] is False
    assert c.get("/api/voice/status").json()["tools"] == {
        "voice-cloning": False, "voice-builtin": True, "voice-lab": True}
    lab = c.get("/api/voice/lab")
    assert lab.status_code == 200
    j = lab.json()
    assert j["switch"] is False and j["timings"]["merge_window_s"]["min"] == 0.5
    assert j["timings"]["hard_silence_s"] == {"value": 1.4, "default": 1.4, "min": 0.8, "max": 3.0}
    assert "key" not in str(j["minimax"]).lower().replace("key_set", "")
    # explicit enabled: false is off too
    write_tools("- {id: voice-lab, kind: builtin, enabled: false}\n")
    assert c.get("/api/voice/lab").status_code == 404


def test_voice_tools_never_reach_safe_mode(auto_env):
    c, _ = auto_env
    write_tools("".join(f"- {{id: {tid}, kind: builtin, enabled: true}}\n" for tid in VOICE_TOOLS))
    auth.set_pin("1234")
    c.cookies.clear()                                   # locked = Safe Mode
    listed = {t["id"] for t in c.get("/api/tools").json()["tools"]}
    assert not listed & set(VOICE_TOOLS)
    for tid in VOICE_TOOLS:
        assert c.get(f"/api/tools/{tid}/status").status_code in (403, 404)
    assert c.get("/api/voice/lab").status_code in (401, 403)
    assert c.post("/api/voice/lab/test", content=b"\0" * 32000).status_code in (401, 403)
    assert c.get("/api/voice/status").status_code in (401, 403)
    assert c.get("/api/voices").status_code in (401, 403, 404)


def test_settings_save_leaves_no_rows_for_opt_in_tools_switched_off(auto_env):
    c, _ = auto_env
    unlock(c)
    rows = c.get("/api/tools").json()["tools"]
    assert c.put("/api/tools", json={"tools": rows}).status_code == 200
    text = (config.DATA_DIR / "tools.yaml").read_text()
    for tid in VOICE_TOOLS:
        assert tid not in text
    for r in rows:
        if r["id"] == "voice-lab":
            r["enabled"] = True
    assert c.put("/api/tools", json={"tools": rows}).status_code == 200
    doc = yaml.safe_load((config.DATA_DIR / "tools.yaml").read_text())
    assert {"id": "voice-lab", "kind": "builtin", "enabled": True} in doc["tools"]
    assert c.get("/api/voice/lab").status_code == 200


def test_voice_tools_cannot_open_in_a_window(auto_env):
    c, _ = auto_env
    unlock(c)
    rows = c.get("/api/tools").json()["tools"]
    for r in rows:
        if r["id"] == "voice-lab":
            r.update(enabled=True, open="window")
    assert c.put("/api/tools", json={"tools": rows}).status_code == 422


# --------------------------------------------------------------------------- #
# Voice lab quick test
# --------------------------------------------------------------------------- #

@pytest.fixture
def lab_on(auto_env, monkeypatch):
    c, turns = auto_env
    unlock(c)
    write_tools("- {id: voice-lab, kind: builtin, enabled: true}\n")
    (config.DATA_DIR / "voice.yaml").write_text("enabled: true\n")
    return c


def test_lab_test_needs_voice_on(auto_env):
    c, _ = auto_env
    unlock(c)
    write_tools("- {id: voice-lab, kind: builtin, enabled: true}\n")
    assert c.post("/api/voice/lab/test", content=b"\0" * 32000).status_code == 503


@pytest.mark.parametrize("size,code", [(100, 422), (32001, 422), (16000 * 2 * 10 + 2, 413)])
def test_lab_test_checks_the_clip(lab_on, size, code):
    assert lab_on.post("/api/voice/lab/test", content=b"\0" * size).status_code == code


def test_lab_test_transcribes_and_speaks_back_with_timings(lab_on):
    pcm = (np.sin(np.linspace(0, 400, 48000)) * 8000).astype("<i2").tobytes()
    r = lab_on.post("/api/voice/lab/test", content=pcm,
                    headers={"Content-Type": "application/octet-stream"})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["transcript"] == FakeSTT.text and j["said"] == FakeSTT.text
    assert j["heard_ms"] == 3000
    for k in ("load_ms", "stt_ms", "tts_first_ms", "tts_total_ms", "spoken_ms"):
        assert isinstance(j[k], int) and j[k] >= 0
    wav = base64.b64decode(j["audio_wav_b64"])
    assert wav[:4] == b"RIFF" and wav[8:12] == b"WAVE"


def test_lab_test_with_nothing_heard_says_so(lab_on, monkeypatch):
    monkeypatch.setattr(FakeSTT, "text", "")
    j = lab_on.post("/api/voice/lab/test", content=b"\0" * 32000).json()
    assert j["transcript"] == "" and j["said"] == vroutes.LAB_TEST_FALLBACK


# --------------------------------------------------------------------------- #
# /api/voices belongs to the Voice cloning tool
# --------------------------------------------------------------------------- #

def test_voices_api_follows_the_cloning_tool(auto_env):
    if "dispatch-voice" not in vroutes._LOADED:
        pytest.skip("dispatch-voice is not installed in this environment")
    c, _ = auto_env
    unlock(c)
    assert c.get("/api/voices").status_code == 404
    write_tools("- {id: voice-cloning, kind: builtin, enabled: true}\n")
    assert c.get("/api/voices").status_code == 200


def test_release_gives_the_heap_back_and_rearms_stock_fillers(monkeypatch, tmp_path):
    """Measured on the reference box: without malloc_trim the process kept
    ~700 MB after a release and grew by a model set per off/on cycle; and the
    stock voice's filler render, queued before the release, loaded Chatterbox
    straight back in. Both are pinned here."""
    from app.voice import dv
    trimmed = []
    monkeypatch.setattr(vroutes, "_trim_heap", lambda: trimmed.append(1))
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    dv._EXTRAS_STARTED.add(str(dv.voices_dir()))
    vroutes.release_models()
    assert trimmed == [1]
    assert str(dv.voices_dir()) not in dv._EXTRAS_STARTED


def test_trim_heap_never_raises():
    vroutes._trim_heap()


def test_a_profile_voice_never_renders_a_second_ack_set(monkeypatch):
    """Review M2: dispatch-voice renders a profile's fillers once into the
    store. While they are pending, DisPatch must not render its own set."""
    from app.voice import registry
    started = []
    monkeypatch.setattr(threading.Thread, "start", lambda self: started.append(self.name))
    monkeypatch.setattr(registry, "_profile_resolver", lambda bot_id: "stock-host")
    monkeypatch.setattr(registry, "_ack_provider", lambda pid: None)     # still pending
    pid, acks = vroutes._voice_for_bot(vconfig.VoiceSettings(), FakeTTS(), None)
    assert (pid, acks, started) == ("stock-host", [], [])
    # A built-in voice (no profile) still gets its rendered acks.
    monkeypatch.setattr(registry, "_profile_resolver", lambda bot_id: "builtin:af_heart")
    vroutes._ACKS.clear()
    pid, acks = vroutes._voice_for_bot(vconfig.VoiceSettings(), FakeTTS(), None)
    assert pid == "builtin:af_heart" and started == ["voice-acks"]


def test_pinless_installs_count_voice_sockets_per_device():
    """Review M3: with no PIN every device used to share one slot list, so a
    third phone's drive silently ended the first one."""
    from types import SimpleNamespace as NS
    a = NS(query_params={"d": "device-aaaa-1111"})
    b = NS(query_params={"d": "device-bbbb-2222"})
    assert vroutes.slot_key(a, None) == "device:device-aaaa-1111"
    assert vroutes.slot_key(a, None) != vroutes.slot_key(b, None)
    # With a PIN the session decides; the device id is ignored.
    assert vroutes.slot_key(a, "tok") == vroutes.slot_key(b, "tok") == "tok"
    # Junk or none: a list of its own, never the shared "" of old.
    for bad in ({"d": "../x"}, {"d": "short"}, {}):
        ws = NS(query_params=bad)
        assert vroutes.slot_key(ws, None) == f"conn:{id(ws)}"


def test_a_hand_edited_switch_applies_live(monkeypatch, tmp_path):
    """Review M4: editing `enabled` in voice.yaml must have the same live
    effect as the app's switch (drives end + models released / prewarm)."""
    import asyncio
    monkeypatch.delenv("DISPATCH_VOICE", raising=False)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    seen = []

    async def spy(was, settings):
        seen.append((was, settings.switched_on))

    monkeypatch.setattr(vroutes, "apply_switch", spy)
    monkeypatch.setitem(vroutes._APPLIED, "on", None)
    (tmp_path / "voice.yaml").write_text("enabled: true\n")
    asyncio.run(vroutes.sync_switch())          # first look: baseline only
    assert seen == []
    import os
    import time
    (tmp_path / "voice.yaml").write_text("enabled: false\n")
    os.utime(tmp_path / "voice.yaml", (time.time() + 5, time.time() + 5))
    asyncio.run(vroutes.sync_switch())
    asyncio.run(vroutes.sync_switch())          # no change, no second call
    assert seen == [(True, False)]


def test_the_status_route_starts_the_switch_watch(auto_env):
    c, _ = auto_env
    unlock(c)
    c.get("/api/voice/status")
    task = vroutes._WATCH.get("task")
    assert task is not None


def test_a_builtin_provider_describes_itself_and_releases(monkeypatch):
    """Review L6/L7: the Built-in voices tool shows the provider's own name,
    licence and steps (a permissive provider is never labelled GPL), and
    switching voice off reaches the plugin's own model cache."""
    from app.voice import registry
    monkeypatch.setattr(registry, "_builtin_list", None)
    monkeypatch.setattr(registry, "_builtin_info", {})
    monkeypatch.setattr(registry, "_release_hooks", [])
    st = vroutes._plugins(vconfig.VoiceSettings())["builtin"]
    assert st["installed"] is False and st["name"] is None
    assert st["suggested"]["licence"] == "GPL-3.0-or-later"          # only a suggestion
    released = []
    registry.set_builtin_voices_provider(
        lambda s: [{"id": "builtin:x", "name": "X"}],
        info={"name": "acme-voices", "licence": "MIT", "bogus": 1,
              "models": [{"where": "plugin", "cmd": "acme fetch"}, {"where": "/etc", "cmd": "rm"}]})
    registry.add_release_hook(lambda: released.append(1))
    st = vroutes._plugins(vconfig.VoiceSettings())["builtin"]
    assert (st["installed"], st["name"], st["licence"]) == (True, "acme-voices", "MIT")
    assert st["models"] == [{"where": "plugin", "cmd": "acme fetch"}]
    assert "bogus" not in registry.builtin_info()
    vroutes.release_models()
    assert released == [1]
    assert registry.ExtensionAPI(None, None).api_version == 3


def test_cache_tags_are_plain_names_and_old_ones_are_pruned(monkeypatch, tmp_path):
    import os
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)

    class Eng:
        def __init__(self, tag):
            self.tag = tag

        def cache_tag(self, pid):
            return self.tag

    for bad in ("../x", ".hidden", "a/b", "a b", "", "x" * 200):
        assert vroutes._cache_dir(None, Eng(bad), None) is None
    assert vroutes._cache_dir(None, Eng("kokoro-fp32-af_heart-1.050"), None).name == "kokoro-fp32-af_heart-1.050"
    root = tmp_path / "voice-cache"
    for i in range(vroutes.CACHE_KEEP + 3):
        (root / f"t{i}").mkdir(parents=True)
        os.utime(root / f"t{i}", (i, i))
    vroutes._prune_cache(root)
    kept = sorted(p.name for p in root.iterdir())
    assert len(kept) == vroutes.CACHE_KEEP and "t0" not in kept and f"t{vroutes.CACHE_KEEP + 2}" in kept


def test_dispatch_voice_pkg_is_only_an_override(monkeypatch, tmp_path):
    """dispatch-voice installed as a package (``--with "dispatch-voice @
    git+…"`` or pip) is found like any other; DISPATCH_VOICE_PKG, when set,
    points at a local checkout and wins over an installed copy."""
    import sys

    from app.voice import dv
    (tmp_path / "dispatch_voice").mkdir()
    monkeypatch.setattr(sys, "path", list(sys.path))
    before = list(sys.path)
    monkeypatch.delenv("DISPATCH_VOICE_PKG", raising=False)
    dv._add_package_path()
    assert sys.path == before                       # nothing to add: use what is installed
    monkeypatch.setenv("DISPATCH_VOICE_PKG", str(tmp_path))
    dv._add_package_path()
    assert sys.path[0] == str(tmp_path)             # the checkout first
    monkeypatch.setenv("DISPATCH_VOICE_PKG", str(tmp_path / "nope"))
    dv._add_package_path()
    assert str(tmp_path / "nope") not in sys.path
    assert dv.DV_REPO == "https://github.com/LaserLloyd/DisPatch_Voice"
