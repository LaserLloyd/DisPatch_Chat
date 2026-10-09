"""Drive mode: the voice session and the /ws/voice gate, with fakes.

No models, no app import: the session is driven through a fake socket, a
scripted turn detector and fake STT/TTS engines; the gate through a fake auth
module. The same gate against the REAL app lives in test_voice_ws_route.py and
runs once main.py carries the integration patch.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import numpy as np
import pytest

from app.voice.config import VoiceSettings
from app.voice.routes import FrameTap, check_gate, origin_ok, serve_voice
from app.voice.session import VoiceHost, VoiceSession
from app.voice.vad_turn import EndpointerConfig, SpeechStart, TurnEnd

THREAD = "t-1"


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #

class FakeWS:
    def __init__(self, headers=None, cookies=None):
        self.headers = headers or {}
        self.cookies = cookies or {}
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.sent: list = []          # dicts (JSON) and bytes, in order
        self.accepted = False
        self.closed_with = None

    async def accept(self):
        self.accepted = True

    async def close(self, code=1000):
        self.closed_with = code

    async def send_text(self, s):
        self.sent.append(json.loads(s))

    async def send_bytes(self, b):
        self.sent.append(bytes(b))

    async def receive(self):
        return await self.inbox.get()

    # helpers
    def push_json(self, obj):
        self.inbox.put_nowait({"type": "websocket.receive", "text": json.dumps(obj)})

    def push_audio(self, b=b"\x00\x00" * 320):
        self.inbox.put_nowait({"type": "websocket.receive", "bytes": b})

    def hang_up(self):
        self.inbox.put_nowait({"type": "websocket.disconnect"})

    def json_of(self, t):
        return [m for m in self.sent if isinstance(m, dict) and m.get("type") == t]


class ScriptedDetector:
    """feed() returns the next scripted event list, one per audio chunk."""

    def __init__(self, script):
        self.script = list(script)
        self.ep = SimpleNamespace(cfg=EndpointerConfig(), turn_fn=None)
        self.resets = 0

    def feed(self, pcm):
        return self.script.pop(0) if self.script else []

    def reset(self):
        self.resets += 1


class FakeSTT:
    def __init__(self, text="what's the weather"):
        self.text = text

    def transcribe(self, audio):
        return self.text


class FakeTTS:
    sample_rate = 24000

    def __init__(self):
        self.said = []
        self.voices = []

    def synth(self, text, voice=None):
        self.said.append(text)
        return np.zeros(480, "<i2"), 24000

    def stream(self, text, voice=None, stop=None):
        self.said.append(text)
        self.voices.append(voice)
        for _ in range(3):              # a sentence arrives as several chunks
            if stop is not None and stop.is_set():
                return
            yield np.zeros(160, "<i2")


def settings(**kw):
    base = {"flag": "1", "ack_after_s": 0.05, "merge_window_s": 0.15}
    base.update(kw)
    return VoiceSettings(**base)


def make_session(script, stt_text="what's the weather", **host_kw):
    ws = FakeWS()
    submitted = []

    async def submit(tid, text):
        submitted.append((tid, text))
        return "m-user"

    host = VoiceHost(submit=submit, **host_kw)
    tts = FakeTTS()
    s = VoiceSession(ws, thread_id=THREAD, settings=settings(),
                     detector=ScriptedDetector(script), stt_engine=FakeSTT(stt_text),
                     tts_engine=tts, host=host)
    return s, ws, tts, submitted


async def settle(n=30):
    for _ in range(n):
        await asyncio.sleep(0.005)


def utterance():
    return TurnEnd(audio=np.zeros(16000, np.float32), reason="smart_turn", turn_prob=0.9)


# --------------------------------------------------------------------------- #
# Session behaviour
# --------------------------------------------------------------------------- #

async def test_turn_end_submits_transcript_through_host():
    s, ws, tts, submitted = make_session([[SpeechStart(), utterance()]])
    task = asyncio.create_task(s.run())
    ws.push_json({"type": "start"})
    ws.push_audio(); ws.push_audio()
    await settle()
    assert submitted == [(THREAD, "what's the weather")]
    assert [m["type"] for m in ws.sent if isinstance(m, dict)][:1] == ["ready"]
    assert ws.json_of("eot") and ws.json_of("transcript")[0]["text"] == "what's the weather"
    assert {"type": "state", "state": "thinking"} in ws.sent
    ws.hang_up(); await task


async def test_audio_is_ignored_until_start():
    s, ws, tts, submitted = make_session([[utterance()]])
    task = asyncio.create_task(s.run())
    ws.push_audio()
    await settle()
    assert submitted == [] and not ws.json_of("eot")
    ws.hang_up(); await task


async def test_empty_transcript_is_not_sent():
    s, ws, tts, submitted = make_session([[utterance()]], stt_text="")
    task = asyncio.create_task(s.run())
    ws.push_json({"type": "start"}); ws.push_audio()
    await settle()
    assert submitted == [] and ws.json_of("transcript")[0]["text"] == ""
    ws.hang_up(); await task


async def test_ack_fires_when_reply_is_slow():
    s, ws, tts, submitted = make_session([[utterance()]])
    task = asyncio.create_task(s.run())
    ws.push_json({"type": "start"}); ws.push_audio()
    await asyncio.sleep(0.15)
    assert ws.json_of("ack")
    ws.hang_up(); await task


async def test_streamed_reply_is_spoken_sentence_by_sentence_with_paired_binary():
    s, ws, tts, _ = make_session([])
    task = asyncio.create_task(s.run())
    ws.push_json({"type": "start"})
    await settle(5)
    for chunk in ("Sure. It is sun", "ny today. Bring", " a hat."):
        s.on_app_frame({"type": "stream_chunk", "thread_id": THREAD,
                        "message_id": "run:1", "text": chunk})
    s.on_app_frame({"type": "stream_done", "thread_id": THREAD, "provisional_id": "run:1",
                    "message_id": "m-9", "message": {"id": "m-9", "role": "assistant",
                    "content": "Sure. It is sunny today. Bring a hat."}})
    # The same reply re-announced as a plain message must not be spoken twice.
    s.on_app_frame({"type": "message", "thread_id": THREAD,
                    "message": {"id": "m-9", "role": "assistant", "content": "x"}})
    await settle()
    assert tts.said == ["Sure. It is sunny today.", "Bring a hat."]
    # Every audio header is immediately followed by its bytes.
    for i, m in enumerate(ws.sent):
        if isinstance(m, dict) and m.get("type") == "audio":
            assert isinstance(ws.sent[i + 1], bytes)
    assert ws.json_of("audio")[-1]["message_id"] == "m-9"
    ws.hang_up(); await task


async def test_other_threads_users_and_sub_rows_are_not_spoken():
    s, ws, tts, _ = make_session([])
    task = asyncio.create_task(s.run())
    ws.push_json({"type": "start"})
    s.on_app_frame({"type": "message", "thread_id": "other",
                    "message": {"id": "a", "role": "assistant", "content": "Nope."}})
    s.on_app_frame({"type": "message", "thread_id": THREAD,
                    "message": {"id": "b", "role": "user", "content": "Mine."}})
    s.on_app_frame({"type": "message", "thread_id": THREAD,
                    "message": {"id": "c", "role": "assistant", "content": "Working.",
                                "metadata": {"sub": True}}})
    s.on_app_frame({"type": "message", "thread_id": THREAD,
                    "message": {"id": "d", "role": "assistant", "content": "Yes."}})
    await settle()
    assert tts.said == ["Yes."]
    ws.hang_up(); await task


async def test_replace_chunks_do_not_duplicate_speech():
    s, ws, tts, _ = make_session([])
    task = asyncio.create_task(s.run())
    ws.push_json({"type": "start"})
    s.on_app_frame({"type": "stream_chunk", "thread_id": THREAD, "message_id": "r",
                    "text": "One. Two"})
    s.on_app_frame({"type": "stream_chunk", "thread_id": THREAD, "message_id": "r",
                    "text": "One. Two. Three.", "replace": True})
    s.on_app_frame({"type": "stream_done", "thread_id": THREAD, "message_id": "r",
                    "message": {"id": "r", "role": "assistant", "content": "One. Two. Three."}})
    await settle()
    assert tts.said == ["One. Two. Three."]
    ws.hang_up(); await task


async def test_user_speech_while_speaking_barges_in():
    aborted = []

    async def abort(tid):
        aborted.append(tid)

    s, ws, tts, _ = make_session([[SpeechStart()]], abort=abort)
    s.settings = settings(barge_abort_turn=True)
    task = asyncio.create_task(s.run())
    ws.push_json({"type": "start"})
    s.on_app_frame({"type": "message", "thread_id": THREAD,
                    "message": {"id": "m1", "role": "assistant", "content": "A long answer."}})
    await settle()
    assert s.speaking
    gen_before = s.gen
    ws.push_audio()
    await settle()
    barge = ws.json_of("barge_in")
    assert barge and barge[0]["gen"] == gen_before + 1
    assert aborted == [THREAD] and not s.speaking
    # Late chunks of the barged reply stay silent.
    n = len(tts.said)
    s.on_app_frame({"type": "stream_done", "thread_id": THREAD, "message_id": "m1",
                    "message": {"id": "m1", "role": "assistant", "content": "A long answer. More."}})
    await settle()
    assert len(tts.said) == n
    ws.hang_up(); await task


async def test_speaking_raises_min_speech_for_barge_in_and_restores_it():
    s, ws, tts, _ = make_session([])
    task = asyncio.create_task(s.run())
    ws.push_json({"type": "start"})
    normal = s.detector.ep.cfg.min_speech_s
    s.on_app_frame({"type": "message", "thread_id": THREAD,
                    "message": {"id": "m1", "role": "assistant", "content": "Hi."}})
    await settle()
    assert s.detector.ep.cfg.min_speech_s == s.settings.barge_in_speech_s
    ws.push_json({"type": "playback_done", "gen": s.gen})
    await settle()
    assert s.detector.ep.cfg.min_speech_s == normal and not s.speaking
    ws.hang_up(); await task


async def test_own_voice_echo_is_not_submitted():
    s, ws, tts, submitted = make_session([[utterance()]], stt_text="It is sunny today")
    task = asyncio.create_task(s.run())
    ws.push_json({"type": "start"})
    s.on_app_frame({"type": "message", "thread_id": THREAD,
                    "message": {"id": "m1", "role": "assistant", "content": "It is sunny today."}})
    await settle()
    ws.push_audio()
    await settle()
    assert submitted == [] and ws.json_of("echo_ignored")
    ws.hang_up(); await task


async def test_lapsed_session_locks_and_ends():
    live = {"ok": True}
    s, ws, tts, _ = make_session([], session_live=lambda: live["ok"])
    t = {"now": 0.0}
    s.clock = lambda: t["now"]
    task = asyncio.create_task(s.run())
    ws.push_json({"type": "start"}); await settle(5)
    live["ok"] = False
    t["now"] = 5.0
    ws.push_audio()
    await asyncio.wait_for(task, 1.0)
    assert ws.json_of("locked")


async def test_resume_after_reconnect_speaks_missed_replies_once():
    async def replies_after(tid, after):
        assert (tid, after) == (THREAD, "m-old")
        return [("m-a", "First."), ("m-b", "Second.")]

    s, ws, tts, _ = make_session([], replies_after=replies_after)
    task = asyncio.create_task(s.run())
    ws.push_json({"type": "start", "after": "m-old"})
    await settle()
    s.on_app_frame({"type": "message", "thread_id": THREAD,
                    "message": {"id": "m-b", "role": "assistant", "content": "Second."}})
    await settle()
    assert tts.said == ["First.", "Second."]
    ws.hang_up(); await task


async def test_stop_playback_and_pause():
    s, ws, tts, _ = make_session([[utterance()]])
    task = asyncio.create_task(s.run())
    ws.push_json({"type": "start"}); ws.push_json({"type": "pause"})
    ws.push_audio(); await settle()
    assert not ws.json_of("eot") and {"type": "state", "state": "paused"} in ws.sent
    ws.push_json({"type": "stop_playback"}); await settle()
    assert ws.json_of("barge_in")
    ws.hang_up(); await task


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #

class FakeAuth:
    def __init__(self, pin_set=True, sessions=()):
        self._pin = pin_set
        self._sessions = set(sessions)

    def load(self):
        return SimpleNamespace(pin_set=self._pin)

    def get_session(self, token):
        return SimpleNamespace(token=token) if token in self._sessions else None


def bots(safe=False, exists=True):
    async def lookup(tid):
        return SimpleNamespace(id="b", safe=safe) if exists else None
    return lookup


READY = VoiceSettings(flag="1")


def ready_settings(monkeypatch):
    monkeypatch.setattr(VoiceSettings, "missing", lambda self: [])
    return READY


@pytest.mark.parametrize("origin,host,ok", [
    (None, "box:8765", True),
    ("http://box:8765", "box:8765", True),
    ("https://evil.example", "box:8765", False),
    ("null", "box:8765", False),
    ("http://box:8765", "", False),
])
def test_origin_rule_matches_main_ws(origin, host, ok):
    h = {"host": host}
    if origin is not None:
        h["origin"] = origin
    assert origin_ok(h) is ok


async def test_gate_refuses_locked_device(monkeypatch):
    ws = FakeWS({"host": "h"}, {})
    g = await check_gate(ws, THREAD, settings=ready_settings(monkeypatch),
                         auth=FakeAuth(pin_set=True), cookie_name="lc_session",
                         thread_bot=bots())
    assert not g.ok and g.reason == "locked" and g.code == 1008


async def test_gate_refuses_cross_origin_even_when_unlocked(monkeypatch):
    ws = FakeWS({"host": "h", "origin": "https://evil"}, {"lc_session": "tok"})
    g = await check_gate(ws, THREAD, settings=ready_settings(monkeypatch),
                         auth=FakeAuth(sessions={"tok"}), cookie_name="lc_session",
                         thread_bot=bots())
    assert not g.ok and g.reason == "cross-origin"


async def test_gate_refuses_safe_bot_and_unknown_thread(monkeypatch):
    ws = FakeWS({"host": "h"}, {"lc_session": "tok"})
    kw = {"settings": ready_settings(monkeypatch), "auth": FakeAuth(sessions={"tok"}),
              "cookie_name": "lc_session"}
    assert (await check_gate(ws, THREAD, thread_bot=bots(safe=True), **kw)).reason == "safe bot"
    assert (await check_gate(ws, THREAD, thread_bot=bots(exists=False), **kw)).reason == "no such thread"


async def test_gate_refuses_when_feature_off():
    ws = FakeWS({"host": "h"}, {"lc_session": "tok"})
    g = await check_gate(ws, THREAD, settings=VoiceSettings(flag="0"),
                         auth=FakeAuth(sessions={"tok"}), cookie_name="lc_session",
                         thread_bot=bots())
    assert not g.ok and g.code == 1013


async def test_gate_accepts_unlocked_session(monkeypatch):
    ws = FakeWS({"host": "h", "origin": "http://h"}, {"lc_session": "tok"})
    g = await check_gate(ws, THREAD, settings=ready_settings(monkeypatch),
                         auth=FakeAuth(sessions={"tok"}), cookie_name="lc_session",
                         thread_bot=bots())
    assert g.ok and g.token == "tok"


async def test_serve_voice_refusal_never_accepts(monkeypatch):
    ws = FakeWS({"host": "h"}, {})

    class Mgr:
        async def connect(self, *a, **k): raise AssertionError("must not register")
        async def disconnect(self, *a): pass

    await serve_voice(ws, THREAD, auth=FakeAuth(pin_set=True), cookie_name="lc_session",
                      manager=Mgr(), thread_bot=bots(), submit=None,
                      settings=ready_settings(monkeypatch), engines=(FakeSTT(), FakeTTS()),
                      detector=ScriptedDetector([]))
    assert not ws.accepted and ws.closed_with == 1008


async def test_serve_voice_registers_tap_and_unregisters(monkeypatch):
    ws = FakeWS({"host": "h"}, {"lc_session": "tok"})
    events = []

    class Mgr:
        async def connect(self, tap, decoy, token):
            events.append(("connect", decoy, token)); await tap.accept()
            self.tap = tap
        async def disconnect(self, tap):
            events.append(("disconnect",))

    async def submit(tid, text):
        return "m"

    mgr = Mgr()
    ws.hang_up()
    await serve_voice(ws, THREAD, auth=FakeAuth(sessions={"tok"}), cookie_name="lc_session",
                      manager=mgr, thread_bot=bots(), submit=submit,
                      settings=ready_settings(monkeypatch), engines=(FakeSTT(), FakeTTS()),
                      detector=ScriptedDetector([]))
    assert ws.accepted and events == [("connect", False, "tok"), ("disconnect",)]
    assert isinstance(mgr.tap, FrameTap)


# --------------------------------------------------------------------------- #
# Turn merge, patience, acks, voice
# --------------------------------------------------------------------------- #

class SeqSTT:
    def __init__(self, texts):
        self.texts = list(texts)

    def transcribe(self, audio):
        return self.texts.pop(0) if self.texts else ""


def merge_session(texts, script, **kw):
    ws = FakeWS()
    calls = []

    async def submit(tid, text):
        calls.append(("submit", text))
        return f"u{sum(1 for c in calls if c[0] == 'submit')}"

    async def abort(tid):
        calls.append(("abort", tid))

    async def retract(tid, mid):
        calls.append(("retract", mid))

    host = VoiceHost(submit=submit, abort=abort, retract=retract)
    tts = FakeTTS()
    s = VoiceSession(ws, thread_id=THREAD, settings=settings(merge_window_s=1.0, **kw),
                     detector=ScriptedDetector(script), stt_engine=SeqSTT(texts),
                     tts_engine=tts, host=host)
    return s, ws, tts, calls


async def test_resume_within_window_merges_into_one_message():
    s, ws, tts, calls = merge_session(
        ["Remind me to call the garage", "about the brakes"],
        [[utterance()], [SpeechStart()], [utterance()]])
    task = asyncio.create_task(s.run())
    ws.push_json({"type": "start"})
    ws.push_audio(); await settle()
    # The early turn's reply starts arriving while we are still in the window...
    s.on_app_frame({"type": "message", "thread_id": THREAD,
                    "message": {"id": "u1", "role": "user", "content": "x"}})
    s.on_app_frame({"type": "stream_chunk", "thread_id": THREAD, "message_id": "run:a",
                    "text": "Sure, which garage do you mean? "})
    ws.push_audio(); await settle()            # driver resumes
    ws.push_audio(); await settle()            # ...and finishes
    assert calls == [("submit", "Remind me to call the garage"), ("abort", THREAD),
                     ("retract", "u1"), ("submit", "Remind me to call the garage about the brakes")]
    assert ws.json_of("transcript")[-1] == {"type": "transcript", "merged": True,
                                            "text": "Remind me to call the garage about the brakes"}
    # The abandoned turn never got a word out, even when it finishes late.
    s.on_app_frame({"type": "stream_done", "thread_id": THREAD, "provisional_id": "run:a",
                    "message_id": "a1", "message": {"id": "a1", "role": "assistant",
                    "content": "Sure, which garage do you mean? Tell me."}})
    # Then the merged message echoes and ITS reply is spoken.
    s.on_app_frame({"type": "message", "thread_id": THREAD,
                    "message": {"id": "u2", "role": "user", "content": "merged"}})
    s.on_app_frame({"type": "message", "thread_id": THREAD,
                    "message": {"id": "a2", "role": "assistant", "content": "Done, brakes it is."}})
    await asyncio.sleep(1.2); await settle()
    assert tts.said == ["Done, brakes it is."]
    ws.hang_up(); await task


async def test_reply_is_held_during_merge_window_then_spoken():
    s, ws, tts, calls = merge_session(["What time is it"], [[utterance()]])
    task = asyncio.create_task(s.run())
    ws.push_json({"type": "start"}); ws.push_audio(); await settle()
    s.on_app_frame({"type": "message", "thread_id": THREAD,
                    "message": {"id": "a1", "role": "assistant", "content": "It is three o'clock."}})
    await settle()
    assert tts.said == []                     # still inside the 1 s window
    await asyncio.sleep(1.1); await settle()
    assert tts.said == ["It is three o'clock."]
    ws.hang_up(); await task


async def test_resume_that_is_only_noise_releases_the_hold():
    s, ws, tts, calls = merge_session(["What time is it", ""],
                                      [[utterance()], [SpeechStart()], [utterance()]])
    task = asyncio.create_task(s.run())
    ws.push_json({"type": "start"}); ws.push_audio(); await settle()
    ws.push_audio(); await settle()
    assert s._hold
    ws.push_audio(); await settle()
    assert [c[0] for c in calls] == ["submit"]        # no abort, no retract
    assert not s._hold
    ws.hang_up(); await task


async def test_patience_preset_scales_endpointing():
    s, ws, tts, _ = make_session([])
    base = s.detector.ep.cfg.turn_hard_silence_s
    task = asyncio.create_task(s.run())
    ws.push_json({"type": "start", "patience": "patient"}); await settle()
    assert s.patience == 1.5
    assert s.detector.ep.cfg.turn_hard_silence_s == pytest.approx(base * 1.5)
    assert s.settings.merge_window(s.patience) == pytest.approx(0.15 * 1.5)
    ws.push_json({"type": "start", "patience": "nonsense"}); await settle()
    assert s.patience == s.settings.patience
    ws.hang_up(); await task


async def test_voice_ack_plays_at_submit_in_the_session_voice():
    ws = FakeWS()

    async def submit(tid, text):
        return "u1"

    tts = FakeTTS()
    s = VoiceSession(ws, thread_id=THREAD, settings=settings(),
                     detector=ScriptedDetector([[utterance()]]), stt_engine=FakeSTT(),
                     tts_engine=tts, host=VoiceHost(submit=submit),
                     profile_id="host-voice",
                     acks=[np.ones(100, "<i2")])
    task = asyncio.create_task(s.run())
    ws.push_json({"type": "start"}); ws.push_audio(); await settle()
    acks = [m for m in ws.json_of("audio") if m["kind"] == "ack"]
    assert len(acks) == 1 and not ws.json_of("ack")    # spoken ack replaces the cue
    s._merge_until = 0
    s.on_app_frame({"type": "message", "thread_id": THREAD,
                    "message": {"id": "a1", "role": "assistant", "content": "Here you go then."}})
    await settle()
    assert tts.voices == ["host-voice"]
    ws.hang_up(); await task


async def test_reply_audio_streams_in_chunks_with_one_seq():
    s, ws, tts, _ = make_session([])
    task = asyncio.create_task(s.run())
    ws.push_json({"type": "start"})
    s.on_app_frame({"type": "message", "thread_id": THREAD,
                    "message": {"id": "a1", "role": "assistant", "content": "One full sentence here."}})
    await settle()
    hdrs = [m for m in ws.json_of("audio") if m["kind"] == "reply"]
    assert [h["chunk"] for h in hdrs] == [0, 1, 2]
    assert len({h["seq"] for h in hdrs}) == 1 and hdrs[0]["text"] and hdrs[1]["text"] is None
    ws.hang_up(); await task


# --------------------------------------------------------------------------- #
# 2.1.0 RC review regressions
# --------------------------------------------------------------------------- #

async def test_audio_and_control_frames_slide_the_session_idle_window():
    """H1: a drive longer than the idle lock must not lock mid-sentence."""
    touches = []
    clock = {"t": 0.0}
    s, ws, tts, _ = make_session([], touch=lambda: touches.append(clock["t"]))
    s.clock = lambda: clock["t"]
    task = asyncio.create_task(s.run())
    ws.push_json({"type": "start"})
    await settle(5)
    assert len(touches) == 1                     # a control frame is activity
    ws.push_json({"type": "ping"})               # a ping is not
    ws.push_audio(); ws.push_audio()             # throttled: same instant
    await settle(5)
    assert len(touches) == 1
    clock["t"] = 31.0
    ws.push_audio()                              # real mic audio, 31 s later
    await settle(5)
    assert len(touches) == 2
    ws.hang_up(); await task


async def test_serve_voice_host_touch_slides_the_real_session(monkeypatch):
    ws = FakeWS({"host": "h"}, {"lc_session": "tok"})
    touched = []

    class Auth(FakeAuth):
        def touch_session(self, token):
            touched.append(token)

    class Mgr:
        async def connect(self, tap, decoy, token): pass
        async def disconnect(self, tap): pass

    async def submit(tid, text):
        return "m"

    ws.push_json({"type": "start"})
    ws.hang_up()
    await serve_voice(ws, THREAD, auth=Auth(sessions={"tok"}), cookie_name="lc_session",
                      manager=Mgr(), thread_bot=bots(), submit=submit,
                      settings=ready_settings(monkeypatch), engines=(FakeSTT(), FakeTTS()),
                      detector=ScriptedDetector([]))
    assert touched == ["tok"]


async def test_speaking_survives_playback_done_while_next_sentence_synthesises():
    """M6: the client drains sentence 1 while sentence 2 is still streaming in.
    `speaking` must stay on, or our own voice counts as a barge-in."""
    s, ws, tts, _ = make_session([])
    task = asyncio.create_task(s.run())
    ws.push_json({"type": "start"})
    await settle(5)
    s.on_app_frame({"type": "stream_chunk", "thread_id": THREAD,
                    "message_id": "run:1", "text": "This is the first sentence. And the"})
    await settle()
    assert s.speaking and tts.said == ["This is the first sentence."]
    ws.push_json({"type": "playback_done", "gen": s.gen})
    await settle()
    assert s.speaking, "reply still streaming: must stay speaking"
    s.on_app_frame({"type": "stream_done", "thread_id": THREAD, "provisional_id": "run:1",
                    "message_id": "m-1", "message": {"id": "m-1", "role": "assistant",
                    "content": "This is the first sentence. And the second one follows."}})
    await settle()
    assert s.speaking                            # sentence 2 sent, not yet played
    ws.push_json({"type": "playback_done", "gen": s.gen})
    await settle()
    assert not s.speaking
    ws.hang_up(); await task


async def test_crashed_worker_sends_error_and_ends_session():
    """M7: a worker exception must not leave a zombie "Listening" session."""
    class Boom(ScriptedDetector):
        def feed(self, pcm):
            raise RuntimeError("detector exploded")

    ws = FakeWS()

    async def submit(tid, text):
        return "m"

    s = VoiceSession(ws, thread_id=THREAD, settings=settings(), detector=Boom([]),
                     stt_engine=FakeSTT(), tts_engine=FakeTTS(), host=VoiceHost(submit=submit))
    task = asyncio.create_task(s.run())
    ws.push_json({"type": "start"}); ws.push_audio()
    await asyncio.wait_for(task, 2)              # run() returns on its own
    assert ws.json_of("error")


async def test_merged_utterance_is_capped():
    from app.voice import session as vs
    s, ws, tts, calls = merge_session(
        ["a " * 3000, "b " * 3000], [[utterance()], [SpeechStart()], [utterance()]])
    task = asyncio.create_task(s.run())
    ws.push_json({"type": "start"})
    for _ in range(3):
        ws.push_audio(); await settle()
    submits = [c[1] for c in calls if c[0] == "submit"]
    assert len(submits) == 2 and len(submits[1]) <= vs.MAX_UTTERANCE_CHARS
    ws.hang_up(); await task


async def test_voice_sockets_per_session_are_capped(monkeypatch):
    from app.voice import routes
    monkeypatch.setattr(routes, "_OPEN", {"tok": routes.MAX_SOCKETS_PER_SESSION})
    ws = FakeWS({"host": "h"}, {"lc_session": "tok"})

    class Mgr:
        async def connect(self, *a, **k): raise AssertionError("must not register")
        async def disconnect(self, *a): pass

    await serve_voice(ws, THREAD, auth=FakeAuth(sessions={"tok"}), cookie_name="lc_session",
                      manager=Mgr(), thread_bot=bots(), submit=None,
                      settings=ready_settings(monkeypatch), engines=(FakeSTT(), FakeTTS()),
                      detector=ScriptedDetector([]))
    # Authorised caller: accepted so the browser sees the real 1013 (not 1006).
    assert ws.closed_with == 1013
