"""Drive mode v2: the contract additions and the 2026-10-10 review fixes,
driven through the voice session with fakes (no models, no app).

Contract: ping/pong, ready, start fields (re-sendable), undo, cancel,
turn_error, structured error, close codes. Review: H1 (failures are spoken,
never stuck in thinking), H3 (echo-safe barge-in, in test_voice_session.py),
M1, M3/M4 (an abandoned turn is never answered aloud), M7 (silent sockets
still lock), L9 (control-frame budget).
"""
from __future__ import annotations

import asyncio

import numpy as np
import pytest
from test_voice_session import THREAD, FakeSTT, FakeTTS, FakeWS, ScriptedDetector, SeqSTT, settings, settle, utterance

from app.voice import session as vs
from app.voice.session import VoiceHost, VoiceSession
from app.voice.vad_turn import SpeechStart


def make(script, stt=None, *, submit_opts=False, **host_kw):
    ws = FakeWS()
    calls = []
    n = {"i": 0}

    if submit_opts:
        async def submit(tid, text, opts):
            n["i"] += 1
            calls.append(("submit", text, opts))
            return f"u{n['i']}"
    else:
        async def submit(tid, text):
            n["i"] += 1
            calls.append(("submit", text))
            return f"u{n['i']}"

    async def abort(tid):
        calls.append(("abort",))

    async def retract(tid, mid):
        calls.append(("retract", mid))

    host_kw.setdefault("abort", abort)
    host_kw.setdefault("retract", retract)
    host = VoiceHost(submit=submit, **host_kw)
    tts = FakeTTS()
    s = VoiceSession(ws, thread_id=THREAD, settings=settings(merge_window_s=0.0),
                     detector=ScriptedDetector(script),
                     stt_engine=stt or FakeSTT(), tts_engine=tts, host=host,
                     notice=lambda text: np.ones(48, "<i2"))
    return s, ws, tts, calls


def frame(**kw):
    return {"thread_id": THREAD, **kw}


def echo(mid):
    return frame(type="message", message={"id": mid, "role": "user", "content": "x"})


def started():
    return frame(type="thinking", status="started")


def stopped():
    return frame(type="thinking", status="stopped")


def reply(mid, text):
    return frame(type="message", message={"id": mid, "role": "assistant", "content": text})


def spoken(ws):
    return [m["text"] for m in ws.json_of("audio") if m["kind"] == "reply" and m["chunk"] == 0]


async def start(s, ws, **extra):
    task = asyncio.create_task(s.run())
    ws.push_json({"type": "start", **extra})
    await settle(5)
    return task


# --------------------------------------------------------------------------- #
# Wire basics
# --------------------------------------------------------------------------- #

async def test_ping_is_answered_with_pong_and_its_t():
    s, ws, _, _ = make([])
    task = await start(s, ws)
    ws.push_json({"type": "ping", "t": 12345})
    await settle(5)
    assert ws.json_of("pong") == [{"type": "pong", "t": 12345}]
    ws.hang_up(); await task


async def test_ready_reports_the_session_settings():
    s, ws, _, _ = make([])
    task = await start(s, ws)
    ready = ws.json_of("ready")[0]
    assert ready["barge_in"] is True and ready["reply_style"] == "normal" and ready["follow_up"] is True
    ws.hang_up(); await task


async def test_start_fields_reach_the_turn_and_resend_does_not_reset():
    s, ws, _, calls = make([[utterance()]], submit_opts=True)
    task = await start(s, ws, reply_style="short", follow_up=False, barge_in=False)
    resets = s.detector.resets
    ws.push_json({"type": "start", "reply_style": "longer", "follow_up": False})   # mid-session
    await settle(5)
    assert s.detector.resets == resets            # settings re-send keeps the conversation
    assert s.barge_in_enabled is False             # absent key = unchanged
    ws.push_audio(); await settle()
    assert calls[0] == ("submit", "what's the weather", {"reply_style": "longer", "follow_up": False})
    ws.push_json({"type": "start", "reply_style": "shouty", "follow_up": "yes", "barge_in": 3})
    await settle(5)
    assert (s.reply_style, s.follow_up, s.barge_in_enabled) == ("normal", True, True)
    assert not ws.json_of("error")                # unknown values: defaults, silently
    ws.hang_up(); await task


async def test_resent_start_while_waiting_reports_thinking_not_listening():
    """Amendment B4: a settings re-send answers with the CURRENT state. With a
    turn still waiting on its answer that is `thinking` — `listening` made the
    client drop its Thinking counter and cancel affordance mid-turn."""
    s, ws, _, _ = make([[utterance()]])
    task = await start(s, ws)
    ws.push_audio(); await settle()
    assert ws.json_of("transcript") and ws.json_of("state")[-1]["state"] == "thinking"
    ws.push_json({"type": "start", "reply_style": "short"})          # the quick sheet
    await settle(5)
    assert ws.json_of("state")[-1]["state"] == "thinking"
    s.on_app_frame(echo("u1")); s.on_app_frame(started()); s.on_app_frame(reply("a1", "Sure."))
    await settle()
    assert spoken(ws) == ["Sure."]                                  # the turn went on as before
    ws.hang_up(); await task


async def test_resent_start_does_not_reread_the_backlog():
    reads = []

    async def replies_after(tid, after):
        reads.append(after)
        return []

    s, ws, _, _ = make([], replies_after=replies_after)
    task = await start(s, ws, after="m-old")
    ws.push_json({"type": "start", "after": "m-old", "patience": "quick"})
    await settle(5)
    assert reads == ["m-old"]
    ws.hang_up(); await task


async def test_bad_frame_and_rate_limit_are_structured_errors(monkeypatch):
    monkeypatch.setattr(vs, "RATE_BURST", 5)
    monkeypatch.setattr(vs, "RATE_PER_S", 0.0)
    s, ws, _, _ = make([])
    task = asyncio.create_task(s.run())
    ws.inbox.put_nowait({"type": "websocket.receive", "text": "{not json"})
    for _ in range(10):
        ws.push_json({"type": "ping"})
    await settle(10)
    errs = ws.json_of("error")
    assert {"type": "error", "code": "bad_frame", "message": errs[0]["message"]} == errs[0]
    assert [e["code"] for e in errs] == ["bad_frame", "rate_limited"]   # once, not per frame
    assert len(ws.json_of("pong")) == 4            # 5 tokens: 1 bad frame + 4 pings
    ws.hang_up(); await task


async def test_stt_failure_is_a_structured_error():
    class Boom:
        def transcribe(self, audio):
            raise RuntimeError("asr down")

    s, ws, _, _ = make([[utterance()]], stt=Boom())
    task = await start(s, ws)
    ws.push_audio(); await settle()
    assert ws.json_of("error")[0]["code"] == "stt_failed"
    assert ws.sent[-1] == {"type": "state", "state": "listening"}
    ws.hang_up(); await task


# --------------------------------------------------------------------------- #
# H1: a failed turn is spoken and returns to listening
# --------------------------------------------------------------------------- #

async def test_agent_error_speaks_a_line_and_returns_to_listening():
    s, ws, _, _ = make([[utterance()]])
    task = await start(s, ws)
    ws.push_audio(); await settle()
    assert ws.sent[-1] == {"type": "state", "state": "thinking"} or ws.json_of("state")[-1]["state"] == "thinking"
    for f in (echo("u1"), started(), frame(type="error", message="Provider down"), stopped()):
        s.on_app_frame(f)
    await settle()
    errs = ws.json_of("turn_error")
    assert errs == [{"type": "turn_error", "message_id": "u1", "reason": "agent_failed",
                     "detail": "Provider down"}]
    notice = [m for m in ws.json_of("audio") if m["kind"] == "notice"]
    assert notice and notice[0]["text"] == vs.NOTICE_LINES["agent_failed"]
    assert ws.json_of("state")[-1]["state"] == "listening"
    ws.hang_up(); await task


async def test_reply_with_nothing_speakable_is_reported(monkeypatch):
    monkeypatch.setattr(vs, "NOTHING_SPOKEN_GRACE_S", 0.05)
    s, ws, _, _ = make([[utterance()]])
    task = await start(s, ws)
    ws.push_audio(); await settle()
    for f in (echo("u1"), started(), reply("a1", "[[pic:a cat|cat]] :react:happy: 🎉"), stopped()):
        s.on_app_frame(f)
    await asyncio.sleep(0.1); await settle()
    assert [e["reason"] for e in ws.json_of("turn_error")] == ["no_speech_in_reply"]
    assert spoken(ws) == []
    assert ws.json_of("state")[-1]["state"] == "listening"
    ws.hang_up(); await task


async def test_spoken_reply_is_not_an_error(monkeypatch):
    monkeypatch.setattr(vs, "NOTHING_SPOKEN_GRACE_S", 0.05)
    s, ws, _, _ = make([[utterance()]])
    task = await start(s, ws)
    ws.push_audio(); await settle()
    for f in (echo("u1"), started(), reply("a1", "It is sunny all day."), stopped()):
        s.on_app_frame(f)
    await asyncio.sleep(0.1); await settle()
    assert not ws.json_of("turn_error") and spoken(ws) == ["It is sunny all day."]
    ws.hang_up(); await task


async def test_a_turn_that_never_answers_times_out(monkeypatch):
    monkeypatch.setattr(vs, "TURN_TIMEOUT_S", 5.0)
    clock = {"t": 100.0}
    s, ws, _, _ = make([[utterance()]])
    s.clock = lambda: clock["t"]
    s.watch_tick_s = 0.01
    task = await start(s, ws)
    ws.push_audio(); await settle()
    clock["t"] += 6
    await settle()
    assert [e["reason"] for e in ws.json_of("turn_error")] == ["timeout"]
    assert ws.json_of("state")[-1]["state"] == "listening"
    s.on_app_frame(reply("a1", "Finally, here it is."))    # late, but the driver is here
    await settle()
    assert spoken(ws) == ["Finally, here it is."]
    ws.hang_up(); await task


async def test_tts_failure_is_reported_and_does_not_strand_thinking():
    class BrokenTTS(FakeTTS):
        def stream(self, text, voice=None, stop=None):
            raise RuntimeError("engine gone")
            yield  # pragma: no cover

    s, ws, _, _ = make([[utterance()]])
    s.tts = BrokenTTS()
    task = await start(s, ws)
    ws.push_audio(); await settle()
    s.on_app_frame(reply("a1", "This will not be heard."))
    await settle()
    assert [e["reason"] for e in ws.json_of("turn_error")] == ["tts_failed"]
    assert ws.json_of("state")[-1]["state"] == "listening"
    ws.hang_up(); await task


# --------------------------------------------------------------------------- #
# undo / cancel
# --------------------------------------------------------------------------- #

async def test_undo_within_the_window_takes_the_turn_back():
    s, ws, _, calls = make([[utterance()]])
    task = await start(s, ws)
    ws.push_audio(); await settle()
    s.on_app_frame(echo("u1")); s.on_app_frame(started())
    await settle()
    ws.push_json({"type": "undo"}); await settle()
    assert ("abort",) in calls and ("retract", "u1") in calls
    assert ws.json_of("undone") == [{"type": "undone", "message_id": "u1"}]
    assert ws.json_of("state")[-1]["state"] == "listening"
    s.on_app_frame(reply("a1", "An answer to a question you took back."))
    await settle()
    assert spoken(ws) == []
    ws.hang_up(); await task


async def test_undo_outside_the_window_is_refused():
    clock = {"t": 0.0}
    s, ws, _, calls = make([[utterance()]])
    s.clock = lambda: clock["t"]
    task = await start(s, ws)
    ws.push_audio(); await settle()
    clock["t"] += vs.UNDO_WINDOW_S + 1
    ws.push_json({"type": "undo"}); await settle()
    assert ws.json_of("error")[-1]["code"] == "undo_expired"
    assert not [c for c in calls if c[0] in ("abort", "retract")]
    ws.hang_up(); await task


async def test_undo_after_the_answer_started_is_refused():
    """Review M1: an API/advisor reply lands in 1–3 s. Undo after that would
    delete the question and leave the answer orphaned in the thread."""
    s, ws, _, calls = make([[utterance()]])
    task = await start(s, ws)
    ws.push_audio(); await settle()
    s.on_app_frame(echo("u1")); s.on_app_frame(started())
    s.on_app_frame(reply("a1", "Here is the answer."))
    await settle()
    ws.push_json({"type": "undo"}); await settle()
    assert ws.json_of("error")[-1]["code"] == "undo_expired"
    assert not [c for c in calls if c[0] == "retract"]
    ws.hang_up(); await task


async def test_undo_with_nothing_said_is_refused():
    s, ws, _, _ = make([])
    task = await start(s, ws)
    ws.push_json({"type": "undo"}); await settle()
    assert ws.json_of("error")[-1]["code"] == "undo_expired"
    ws.hang_up(); await task


async def test_cancel_aborts_the_pending_turn_and_mutes_it():
    s, ws, _, calls = make([[utterance()]])
    task = await start(s, ws)
    ws.push_audio(); await settle()
    s.on_app_frame(echo("u1")); s.on_app_frame(started())
    await settle()
    ws.push_json({"type": "cancel"}); await settle()
    assert ("abort",) in calls and ws.json_of("barge_in")
    assert ws.json_of("state")[-1]["state"] == "listening"
    s.on_app_frame(reply("a1", "Too late, cancelled."))
    s.on_app_frame(stopped())
    await settle()
    assert spoken(ws) == [] and not ws.json_of("turn_error")
    ws.hang_up(); await task


# --------------------------------------------------------------------------- #
# M3 / M4: never two answers
# --------------------------------------------------------------------------- #

async def test_new_question_while_thinking_replaces_the_pending_one():
    """M4: "What's the weather?" … "Never mind, what's on my calendar?" —
    only the second is answered aloud, even if the first answer lands later."""
    s, ws, _, calls = make([[utterance()], [utterance()]],
                           stt=SeqSTT(["What's the weather", "Never mind, what's on my calendar"]))
    task = await start(s, ws)
    ws.push_audio(); await settle()
    s.on_app_frame(echo("u1")); s.on_app_frame(started())      # turn 1 is running
    await settle()
    ws.push_audio(); await settle()                             # the driver asks again
    assert [c[0] for c in calls] == ["submit", "abort", "submit"]
    # Turn 1 finishes anyway (a gateway run that ignored the abort)...
    s.on_app_frame(reply("a1", "It will rain at four."))
    s.on_app_frame(stopped())
    # ...then turn 2 runs.
    s.on_app_frame(echo("u2")); s.on_app_frame(started())
    s.on_app_frame(reply("a2", "Two meetings this afternoon."))
    s.on_app_frame(stopped())
    await settle()
    assert spoken(ws) == ["Two meetings this afternoon."]
    assert not ws.json_of("turn_error")
    ws.hang_up(); await task


async def test_cancelled_queued_turn_does_not_steal_the_next_answer():
    """M3: the host cancelled turn 1 before it ever ran, so the next `started`
    belongs to turn 2 and its answer must be spoken."""
    s, ws, _, calls = make([[utterance()], [utterance()]],
                           stt=SeqSTT(["First question here", "Second question here"]))
    task = await start(s, ws)
    ws.push_audio(); await settle()
    s.on_app_frame(echo("u1"))                                   # queued, never started
    await settle()
    ws.push_audio(); await settle()
    s.on_app_frame(echo("u2")); s.on_app_frame(started())
    s.on_app_frame(reply("a2", "Here is the second answer."))
    await settle()
    assert spoken(ws) == ["Here is the second answer."]
    ws.hang_up(); await task


async def test_merged_first_half_loses_its_trailing_punctuation():
    s, ws, _, calls = make([[utterance()], [SpeechStart()], [utterance()]],
                           stt=SeqSTT(["What's on my calendar?", "for this afternoon"]))
    s.settings = settings(merge_window_s=1.0)
    task = await start(s, ws)
    for _ in range(3):
        ws.push_audio(); await settle()
    assert calls[-1] == ("submit", "What's on my calendar for this afternoon")
    ws.hang_up(); await task


# --------------------------------------------------------------------------- #
# M7 / H2: timers that run while the socket is silent
# --------------------------------------------------------------------------- #

async def test_silent_socket_still_locks_when_the_session_lapses(monkeypatch):
    monkeypatch.setattr(vs, "LIVE_CHECK_S", 0.0)
    live = {"ok": True}
    s, ws, _, _ = make([], session_live=lambda: live["ok"])
    s.watch_tick_s = 0.01
    task = await start(s, ws)
    live["ok"] = False                       # no frame arrives after this
    await asyncio.wait_for(task, 1.0)
    assert ws.json_of("locked") and s.close_code[0] == 1008


async def test_a_pinging_client_that_goes_silent_is_dropped(monkeypatch):
    monkeypatch.setattr(vs, "DEAD_AFTER_S", 5.0)
    clock = {"t": 0.0}
    s, ws, _, _ = make([])
    s.clock = lambda: clock["t"]
    s.watch_tick_s = 0.01
    task = await start(s, ws)
    clock["t"] = 30.0                        # no ping yet: an old client, never dropped
    await settle(5)
    assert not task.done()
    ws.push_json({"type": "ping", "t": 1}); await settle(5)
    clock["t"] = 36.0
    await asyncio.wait_for(task, 1.0)
    assert s.close_code[0] == 1001


async def test_eviction_ends_the_session_with_4001():
    s, ws, _, _ = make([])
    task = await start(s, ws)
    s.end(4001, "replaced")
    await asyncio.wait_for(task, 1.0)
    assert s.close_code == (4001, "replaced")


# --------------------------------------------------------------------------- #
# Persona variants
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("style,follow,expect", [
    ("short", True, "one short spoken sentence"),
    ("normal", True, "one to three short spoken sentences"),
    ("longer", False, "do not end with a question"),
])
def test_reply_style_variants(style, follow, expect):
    from app.voice import registry
    from app.voice.persona import DEFAULT_PERSONA, voice_hint
    registry.reset()
    hint = voice_hint("main", None, style=style, follow_up=follow)
    assert expect in hint and hint.startswith("[Voice mode]")
    assert voice_hint("main", None) == DEFAULT_PERSONA
    assert voice_hint("main", None, style="nonsense") == DEFAULT_PERSONA


def test_extension_persona_keeps_the_drivers_style():
    from app.voice import registry
    from app.voice.persona import voice_hint
    registry.reset()
    registry.set_persona_provider(lambda b, p: "Be a pirate.")
    try:
        assert voice_hint("main", None) == "Be a pirate."
        assert "one short spoken sentence" in voice_hint("main", None, style="short")
    finally:
        registry.reset()


def test_echo_check_ignores_digits_the_stt_wrote_for_our_words():
    s, ws, _, _ = make([])
    s._recent_said.append("A dentist appointment at three, and a call with the school at four thirty.")
    assert s._is_echo("a dentist appointment at 3, and a call with the school at 4.30.")
    assert not s._is_echo("move the dentist to friday at 5")


async def test_playback_done_for_the_ack_never_ends_or_fails_the_turn(monkeypatch):
    """Lead/frontend report: the browser sends playback_done for EVERY sound,
    including the ack filler while the agent is thinking. That must neither
    complete nor fail the turn; the real reply is still spoken."""
    monkeypatch.setattr(vs, "NOTHING_SPOKEN_GRACE_S", 0.05)
    s, ws, _, _ = make([[utterance()]])
    s.acks = [np.ones(10, "<i2")]
    task = await start(s, ws)
    ws.push_audio(); await settle()
    ack = [m for m in ws.json_of("audio") if m["kind"] == "ack"]
    assert ack
    ws.push_json({"type": "playback_done", "gen": ack[0]["gen"], "seq": ack[0]["seq"]})
    ws.push_json({"type": "playback_done", "gen": ack[0]["gen"]})      # old clients: no seq
    await settle()
    assert not ws.json_of("turn_error") and ws.json_of("state")[-1]["state"] == "thinking"
    for f in (echo("u1"), started(), reply("a1", "It is sunny all day today."), stopped()):
        s.on_app_frame(f)
    await asyncio.sleep(0.1); await settle()
    assert spoken(ws) == ["It is sunny all day today."] and not ws.json_of("turn_error")
    assert s.speaking
    # A late drain report for the ack (older seq) does not end `speaking`...
    ws.push_json({"type": "playback_done", "gen": s.gen, "seq": ack[0]["seq"]})
    await settle()
    assert s.speaking
    # ...the one for the reply does.
    last = max(m["seq"] for m in ws.json_of("audio") if m["kind"] == "reply")
    ws.push_json({"type": "playback_done", "gen": s.gen, "seq": last})
    await settle()
    assert not s.speaking and ws.json_of("state")[-1]["state"] == "listening"
    ws.hang_up(); await task


async def test_after_ids_resume_from_the_first_known_id():
    """Contract F1: `after` may be a provisional id the server never stored."""
    asked = []

    async def replies_after(tid, after):
        asked.append(after)
        if after == "m-known":
            return [("m-new", "You missed this one.")]
        return None                                   # unknown id

    s, ws, tts, _ = make([], replies_after=replies_after)
    task = await start(s, ws, after="run:prov", after_ids=["run:prov", "m-older-gone", "m-known", "m-x"])
    await settle()
    assert asked == ["run:prov", "run:prov", "m-older-gone", "m-known"]
    assert spoken(ws) == ["You missed this one."]
    ws.hang_up(); await task
