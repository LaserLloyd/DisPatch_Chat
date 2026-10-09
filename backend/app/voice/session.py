"""One Drive-mode conversation: mic audio in, spoken reply out.

    browser ──PCM16 16 kHz──▶ TurnDetector ──TurnEnd──▶ STT ──text──▶ host.submit
                                   │ SpeechStart                         │ (the NORMAL
                                   ▼ (barge-in / merge)                  ▼  turn path)
    browser ◀──PCM16 chunks── TTS stream ◀── SentenceSplitter ◀── reply frames
                                                (stream_chunk / stream_done /
                                                 message) for this thread, tapped
                                                 off the app's own broadcast

Everything the session needs from the app comes through :class:`VoiceHost`, so
the module never imports main.py and the tests drive it with fakes.

Turn merging (the road-noise simulation's main finding)
-------------------------------------------------------
Smart Turn scored mid-sentence pauses as "finished" in 4 of 10 runs. So after
a turn ends we KEEP LISTENING for ``merge_window`` seconds (× patience):

* reply speech is HELD (not muted) during the window — never speak a reply to
  an utterance that may still be growing. The LLM takes 1–3 s anyway, so the
  hold rarely costs audible time;
* if the driver resumes and the new piece transcribes to real words, the
  in-flight turn is aborted, the first user row is retracted, and the merged
  utterance is submitted as ONE message;
* a cough or an empty transcript just releases the hold.

Wire protocol on ``/ws/voice/{thread_id}``
------------------------------------------
Client → server: binary = PCM16LE mono 16 kHz (any chunk size; 20–100 ms is
typical). JSON control frames:

* ``{"type":"start", "after": <message id|null>, "after_ids": [ids], "patience": "quick|normal|patient"|<float>,
  "barge_in": bool, "reply_style": "short|normal|longer", "follow_up": bool}``
  — begin/resume listening; replies newer than ``after`` that this device has
  not heard are spoken (how a reconnect after a data drop picks the thread up).
  May be RE-SENT mid-session to change the settings; that never resets the
  conversation. Unknown values fall back to the defaults, silently.
* ``{"type":"pause"}`` — stop listening (audio ignored), keep speaking.
* ``{"type":"stop_playback"}`` — the user tapped to silence the reply.
* ``{"type":"playback_done", "gen": n, "seq"?: m}`` — the client's audio queue
  drained (``seq`` = the last audio seq it played; optional).
* ``{"type":"cancel"}`` — drop the pending/running turn: abort it, stop playback.
* ``{"type":"undo"}`` — within ``UNDO_WINDOW_S`` of the last transcript: abort
  that turn and take its user row back.
* ``{"type":"ping", "t": x}`` — answered at once with ``pong {t: x}``. Once a
  client pings, silence for ``DEAD_AFTER_S`` ends the socket (it is gone).

Server → client (JSON unless noted):

* ``ready`` {sample_rate_in, sample_rate_out, sample_format, streaming_tts, smart_turn,
  patience, voice_acks, barge_in, reply_style, follow_up} — models loaded, the
  session can hear (the client plays its ready tone and flushes buffered mic audio)
* ``state`` {state: listening|hearing|transcribing|thinking|speaking|paused}
* ``eot`` {reason} — end of turn heard; the client plays its earcon NOW
* ``transcript`` {text, merged, message_id} — empty text = nothing intelligible was heard
* ``echo_ignored`` {text} — the mic picked up our own voice; not sent
* ``ack`` — no reply and no voice ack within ``ack_after_s``: "still working"
* ``audio`` {gen, seq, chunk, sample_rate, text, message_id, kind}
  followed IMMEDIATELY by one binary frame (PCM16LE mono). A sentence arrives
  as several chunks with the same seq; ``kind`` is ``reply``, ``ack`` or
  ``notice`` (a spoken error line).
* ``duck`` {on, gen} — the driver may be talking over the reply: lower/pause
  playback (on) until the words are checked; ``on: false`` = it was our own
  echo, carry on where you paused. A real interruption follows with ``barge_in``.
* ``barge_in`` {gen} — drop every queued/playing chunk with gen < this
* ``turn_error`` {message_id, reason: agent_failed|no_speech_in_reply|timeout|tts_failed, detail}
  — the turn ended without a spoken answer; a short spoken line follows when
  possible, then ``state listening``
* ``undone`` {message_id} · ``pong`` {t}
* ``metrics`` {eot_to_transcript_ms | eot_to_first_audio_ms | first_chunk_ms ...}
* ``locked`` — the unlocked session lapsed; the socket closes next (1008)
* ``error`` {code, message} — code: undo_expired | stt_failed | bad_frame |
  rate_limited | unavailable

Close codes the session ends with: 1008 locked, 4001 replaced (a newer socket
of the same session took over; routes.py), 1001 heartbeat lost, 1011 internal
error.
"""
from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import random
import re
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import numpy as np

from .config import PATIENCE, REPLY_STYLES
from .tts import REPLY_ACTIVITY, SentenceSplitter, speakable
from .vad_turn import Discarded, SpeechStart, TurnDetector, TurnEnd

log = logging.getLogger("dispatch.voice")


@dataclass
class VoiceHost:
    """What the app lends a voice session. main.py builds one per socket."""
    #: Persist ``text`` as a user message in the thread and run the normal
    #: agent turn (voice persona, no-thinking). Returns the new message id.
    #: May take a third argument: ``{"reply_style": .., "follow_up": ..}``.
    submit: Callable[..., Awaitable[str | None]]
    #: Is the unlocked session that opened this socket still valid?
    session_live: Callable[[], bool] = lambda: True
    #: Stop the voice turns this thread is waiting on: every one that has not
    #: started yet is cancelled, the running one is aborted.
    abort: Callable[[str], Awaitable[None]] | None = None
    #: Remove a user row this session submitted (turn merge, undo).
    retract: Callable[[str, str], Awaitable[None]] | None = None
    #: Assistant replies newer than a message id: [(id, content), ...].
    replies_after: Callable[[str, str], Awaitable[list[tuple[str, str]]]] | None = None
    #: Slide the opening session's idle window (auth.touch_session). Called on
    #: real activity — mic audio and control frames, never a ping — at most
    #: once per ``TOUCH_EVERY_S``, so a drive longer than the idle lock does
    #: not lock the session mid-sentence.
    touch: Callable[[], None] | None = None


#: Minimum seconds between two VoiceHost.touch calls.
TOUCH_EVERY_S = 30.0
#: A merged utterance (driver kept resuming) never grows past this many chars.
MAX_UTTERANCE_CHARS = 4000
#: A reply stream with no frame for this long no longer counts as "still
#: coming" (a turn that died without a stream_done must not pin `speaking`).
REPLY_STALE_S = 30.0
#: `undo` works this long after the transcript it takes back.
UNDO_WINDOW_S = 8.0
#: A turn with nothing spoken this long after it was sent is given up on.
TURN_TIMEOUT_S = 120.0
#: After the agent says it stopped, wait this long for late frames before
#: deciding the reply had nothing to say out loud.
NOTHING_SPOKEN_GRACE_S = 1.0
#: The unlocked session is re-checked at least this often, even when the
#: socket is silent (paused) — review M7.
LIVE_CHECK_S = 15.0
#: A client that pings and then goes silent this long is gone (half-open).
DEAD_AFTER_S = 30.0
#: A duck (possible barge-in) that never turns into a transcript is undone.
DUCK_MAX_S = 8.0
#: Control-frame budget (L9): a bucket of RATE_BURST frames refilled at RATE_PER_S.
RATE_BURST = 40
RATE_PER_S = 10.0

#: What the driver hears when a turn ends with nothing to say (H1).
NOTICE_LINES = {
    "agent_failed": "Sorry, that didn't go through.",
    "no_speech_in_reply": "Nothing to read out. It's in the chat.",
    "timeout": "Still working on that. It'll be in the chat.",
}


@dataclass
class _Reply:
    splitter: SentenceSplitter = field(default_factory=SentenceSplitter)
    consumed: int = 0
    done: bool = False
    muted: bool = False
    updated: float = 0.0


@dataclass
class _Turn:
    """One spoken message this session submitted, from submit to its answer."""
    msg_id: str | None
    text: str
    at: float                      # clock() when the transcript was sent
    echoed: bool = False           # its user row came back through the tap
    started: bool = False          # the agent run for it began (thinking/started)
    stopped: bool = False          # ... and ended (thinking/stopped)
    spoke: bool = False            # at least one speakable sentence queued
    closed: bool = False           # answered, failed or abandoned: no more checks
    abandoned: bool = False        # cancelled/merged/undone: its reply stays silent
    abort_seq: int | None = None   # frame counter when the host abort ran


def _norm_words(s: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", s.lower())


def _patience_value(v, default: float) -> float:
    if isinstance(v, str) and v in PATIENCE:
        return PATIENCE[v]
    if isinstance(v, (int, float)) and not isinstance(v, bool) and 0.3 <= float(v) <= 3.0:
        return float(v)
    return default


def _takes_opts(fn) -> bool:
    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return False
    positional = [p for p in params if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    return len(positional) >= 3 or any(p.kind == p.VAR_POSITIONAL for p in params)


class VoiceSession:
    def __init__(self, ws, *, thread_id: str, settings, detector: TurnDetector,
                 stt_engine, tts_engine, host: VoiceHost,
                 profile_id: str | None = None, acks: list | None = None,
                 notice: Callable[[str], object] | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.ws = ws
        self.thread_id = thread_id
        self.settings = settings
        self.detector = detector
        self.stt = stt_engine
        self.tts = tts_engine
        self.host = host
        self.profile_id = profile_id      # opaque voice id for the TTS engine
        self.acks = acks if acks is not None else []   # shared list; may fill later
        #: text -> int16 PCM at the engine rate (pre-rendered spoken lines);
        #: None = synthesise with the session engine.
        self._notice_fn = notice
        self.clock = clock

        self.listening = False
        self.gen = 0                      # bumps on every barge-in
        self.seq = 0
        self.speaking = False             # audio sent and not yet confirmed played
        self.patience = settings.patience
        self.barge_in_enabled = True      # start.barge_in
        self.reply_style = "normal"       # start.reply_style
        self.follow_up = True             # start.follow_up
        self._audio_q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=400)
        self._say_q: asyncio.Queue[tuple[int, str, str | None]] = asyncio.Queue()
        self._frames_q: asyncio.Queue[tuple[int, dict]] = asyncio.Queue(maxsize=2000)
        self._frame_seq = 0
        self._replies: dict[str, _Reply] = {}
        self._spoken_ids: deque[str] = deque(maxlen=64)
        self._recent_said: deque[str] = deque(maxlen=6)
        self._send_lock = asyncio.Lock()
        self._stop_tts = threading.Event()
        self._eot_at: float | None = None
        self._first_audio_logged = True
        self._ack_task: asyncio.Task | None = None
        self._awaiting_reply = False
        self._last_live_check = 0.0
        self._closed = False
        self._last_touch = float("-inf")
        self._tts_busy = False            # the TTS worker is on a sentence
        self._client_drained = True       # last playback_done matched our gen
        self._fatal = asyncio.Event()     # run() must end (worker died, lapsed, evicted)
        self.close_code: tuple[int, str] = (1000, "")
        self._base = (detector.ep.cfg.min_speech_s, detector.ep.cfg.turn_hard_silence_s,
                      detector.ep.cfg.no_smart_turn_silence_s)
        self._last_state: str | None = None
        self._deaf = False                # mic ignored while speaking (barge_in off)
        self._last_after: object = object()
        self._last_reply_seq = 0
        # turn merge
        self._merge_until = 0.0           # replies held until this clock time
        self._hold = False                # held while the driver is mid-resume
        self._last_submit: tuple[str, str | None] | None = None   # (text, msg id)
        self._await_user_id: str | None = None   # ignore replies until this row echoes
        # soft barge-in (H3)
        self._duck = False
        self._duck_at = 0.0
        # turn tracking (H1, M3, M4, undo, cancel)
        self._turns: deque[_Turn] = deque(maxlen=16)   # submitted, not started yet
        self._owner: _Turn | str | None = None   # whose run the reply frames are from
        self._current: _Turn | None = None
        self._turn_tasks: set[asyncio.Task] = set()
        self._echoed_ids: deque[str] = deque(maxlen=16)
        # heartbeat + rate limit
        self._pinged = False
        self._last_rx = clock()
        self._tokens = float(RATE_BURST)
        self._tokens_at = clock()
        self._last_limited: dict[str, float] = {}
        self.watch_tick_s = 1.0
        self._submit_opts = _takes_opts(host.submit)

    # ------------------------------------------------------------------ io #
    async def send(self, obj: dict) -> None:
        if self._closed:
            return
        async with self._send_lock:
            await self.ws.send_text(json.dumps(obj))

    async def _send_audio(self, header: dict, pcm: bytes) -> None:
        if self._closed:
            return
        # Header and payload under ONE lock hold so no other frame can land
        # between them — the client pairs them by adjacency.
        async with self._send_lock:
            await self.ws.send_text(json.dumps(header))
            await self.ws.send_bytes(pcm)

    async def state(self, s: str) -> None:
        self._last_state = s
        await self.send({"type": "state", "state": s})

    async def error(self, code: str, message: str) -> None:
        await self.send({"type": "error", "code": code, "message": message})

    def _idle_state(self) -> str:
        """The state to report when nothing just changed: a re-sent ``start``
        (amendment B4), an empty transcript, an echo. A turn still waiting on
        its answer stays ``thinking`` — reporting ``listening`` there made the
        client drop its Thinking counter and cancel affordance mid-turn."""
        if self.speaking:
            return "speaking"
        if self.listening and (self._awaiting_reply or self._talking()):
            return "thinking"
        return "listening" if self.listening else "paused"

    def end(self, code: int, reason: str = "") -> None:
        """Ask run() to finish; the route closes the socket with ``code``."""
        self.close_code = (code, reason)
        self._fatal.set()

    # ------------------------------------------------------- app frames #
    def on_app_frame(self, frame: dict) -> None:
        """Called (synchronously) for every frame the app broadcasts."""
        if frame.get("thread_id") != self.thread_id:
            return
        if frame.get("type") not in ("stream_chunk", "stream_done", "message",
                                     "thinking", "error"):
            return
        self._frame_seq += 1
        with contextlib.suppress(asyncio.QueueFull):
            self._frames_q.put_nowait((self._frame_seq, frame))

    def _reply_text_update(self, rid: str, full: str | None, delta: str | None,
                           final: bool, message_id: str | None = None) -> list[tuple[str, str | None]]:
        r = self._replies.get(rid)
        if r is None:
            r = self._replies[rid] = _Reply()
        if r.muted or r.done:
            return []
        r.updated = self.clock()
        if full is not None:
            new = full[r.consumed:] if len(full) > r.consumed else ""
            r.consumed = max(r.consumed, len(full))
        else:
            new = delta or ""
            r.consumed += len(new)
        sentences = r.splitter.push(new) if new else []
        if final:
            sentences += r.splitter.flush()
            r.done = True
        return [(s, message_id or rid) for s in sentences]

    def _mute(self, rid: str | None) -> None:
        if rid:
            self._replies.setdefault(rid, _Reply()).muted = True

    def _mute_frame(self, f: dict) -> None:
        msg = f.get("message") or {}
        if msg.get("role") == "user":
            return
        for rid in (f.get("message_id"), f.get("provisional_id"), msg.get("id")):
            self._mute(rid)
        if f.get("type") == "message" and msg.get("id"):
            self._spoken_ids.append(msg["id"])

    def handle_reply_frame(self, f: dict) -> list[tuple[str, str | None]]:
        """Reply frame → sentences to say. Pure bookkeeping (testable)."""
        t = f.get("type")
        msg = f.get("message") or {}
        if self._await_user_id is not None:
            # After an abandoned turn, everything until the next user row we
            # sent echoes back belongs to the abandoned turn.
            if t == "message" and msg.get("role") == "user" and msg.get("id") == self._await_user_id:
                self._await_user_id = None
            else:
                self._mute_frame(f)
            return []
        if t == "stream_chunk":
            mid = f.get("message_id")
            if not mid:
                return []
            if f.get("replace"):
                return self._reply_text_update(mid, f.get("text") or "", None, False)
            return self._reply_text_update(mid, None, f.get("text") or "", False)
        if msg.get("role") != "assistant" or (msg.get("metadata") or {}).get("sub"):
            return []
        mid = msg.get("id")
        if t == "stream_done":
            rid = f.get("provisional_id") or f.get("message_id") or mid
            out = self._reply_text_update(rid, msg.get("content") or "", None, True, mid)
            if rid != mid and self._replies.get(rid, _Reply()).muted:
                self._mute(mid)
            for i in (rid, mid):
                if i:
                    self._spoken_ids.append(i)
            return out
        # plain "message"
        if not mid or mid in self._spoken_ids:
            return []
        self._spoken_ids.append(mid)
        return self._reply_text_update(mid, msg.get("content") or "", None, True, mid)

    # ---------------------------------------------------- turn tracking #
    def _on_user_echo(self, mid: str | None) -> None:
        if not mid:
            return
        self._echoed_ids.append(mid)       # may echo before submit() returns
        if mid == self._await_user_id:
            self._await_user_id = None
        for t in self._turns:
            if t.msg_id == mid:
                t.echoed = True

    def _on_started(self, seq: int) -> None:
        """A run began on this thread. It belongs to the oldest voice turn of
        ours whose row has echoed and that has not started — except a turn we
        abandoned and had the host cancel: a `started` that arrived AFTER that
        cancel cannot be its run (review M3/M4: no double answers)."""
        while self._turns:
            t = self._turns[0]
            if not t.echoed:
                break
            if t.abandoned and t.abort_seq is not None and seq > t.abort_seq:
                self._turns.popleft()          # cancelled before it ever ran
                continue
            self._turns.popleft()
            t.started = True
            self._owner = t
            return
        self._owner = "other"                  # a typed turn, another device…

    async def _on_stopped(self) -> None:
        t = self._owner
        if not isinstance(t, _Turn) or t.closed:
            return
        t.stopped = True
        if t.spoke:
            t.closed = True
            return
        task = asyncio.create_task(self._nothing_spoken_later(t))
        self._turn_tasks.add(task)
        task.add_done_callback(self._turn_tasks.discard)

    async def _nothing_spoken_later(self, t: _Turn) -> None:
        await asyncio.sleep(NOTHING_SPOKEN_GRACE_S)
        if not t.closed and not t.spoke and not t.abandoned:
            await self._turn_failed(t, "no_speech_in_reply")

    async def _on_agent_error(self, f: dict) -> None:
        t = self._owner
        if not isinstance(t, _Turn) or t.closed or t.abandoned:
            return
        detail = f.get("message") if isinstance(f.get("message"), str) else None
        await self._turn_failed(t, "agent_failed", detail)

    async def _turn_failed(self, t: _Turn | None, reason: str, detail: str | None = None) -> None:
        """turn_error + a short spoken line + back to listening (H1)."""
        if t is not None:
            if t.closed:
                return
            t.closed = True
        self._awaiting_reply = False
        if self._ack_task:
            self._ack_task.cancel()
        frame = {"type": "turn_error", "message_id": t.msg_id if t else None, "reason": reason}
        if detail:
            frame["detail"] = detail[:200]
        await self.send(frame)
        line = NOTICE_LINES.get(reason)
        if line:
            await self._say_notice(line)
        if not self.speaking:
            await self.state("listening" if self.listening else "paused")

    async def _say_notice(self, text: str) -> None:
        try:
            if self._notice_fn is not None:
                pcm = await asyncio.to_thread(self._notice_fn, text)
            else:
                pcm, _ = await asyncio.to_thread(self.tts.synth, text, self.profile_id)
        except Exception:
            log.exception("voice: could not render the spoken notice")
            return
        if pcm is None or not len(pcm):
            return
        self._recent_said.append(text)       # its echo must never become a turn
        self.seq += 1
        self._client_drained = False
        await self._send_audio({"type": "audio", "gen": self.gen, "seq": self.seq, "chunk": 0,
                                "sample_rate": self.tts.sample_rate, "text": text,
                                "message_id": None, "kind": "notice"},
                               np.asarray(pcm, "<i2").tobytes())

    async def _abandon(self, t: _Turn | None, *, abort: bool) -> None:
        """Give up on a voice turn: its reply (now or late) is never spoken,
        and with ``abort`` the host stops it (cancelled if not started yet)."""
        if t is None or t.abandoned:
            return
        t.abandoned = True
        t.closed = True
        self._awaiting_reply = False
        if self._ack_task:
            self._ack_task.cancel()
        if self._owner is t:
            # Its run is the one producing frames right now: mute them.
            for r in self._replies.values():
                if not r.done:
                    r.muted = True
        if abort and self.host.abort:
            t.abort_seq = self._frame_seq
            with contextlib.suppress(Exception):
                await self.host.abort(self.thread_id)

    def _owner_is_abandoned(self) -> bool:
        return isinstance(self._owner, _Turn) and self._owner.abandoned

    async def _frames_worker(self) -> None:
        while True:
            seq, f = await self._frames_q.get()
            t = f.get("type")
            if t == "thinking":
                if f.get("status") == "started":
                    self._on_started(seq)
                elif f.get("status") == "stopped":
                    await self._on_stopped()
                continue
            if t == "error":
                await self._on_agent_error(f)
                continue
            msg = f.get("message") or {}
            if t == "message" and msg.get("role") == "user":
                self._on_user_echo(msg.get("id"))
            if self._owner_is_abandoned():
                self._mute_frame(f)
                continue
            owner = self._owner
            for sentence, mid in self.handle_reply_frame(f):
                if not speakable(sentence):
                    continue
                if isinstance(owner, _Turn):
                    owner.spoke = True
                    if owner.stopped:
                        owner.closed = True
                await self._enqueue_say(sentence, mid)
            await self._maybe_finish_speaking()

    def _reply_pending(self) -> bool:
        """A reply is still streaming in (more sentences may follow)."""
        now = self.clock()
        return any(not r.done and not r.muted and now - r.updated < REPLY_STALE_S
                   for r in self._replies.values())

    async def _maybe_finish_speaking(self) -> None:
        """Leave the speaking state only when the whole reply is out: the
        client drained its queue for this generation, nothing is queued or
        being synthesised, and no reply stream is still open. Clearing it any
        earlier (the next sentence still synthesising) made our own voice look
        like a barge-in and aborted the agent run."""
        if (self.speaking and self._client_drained and self._say_q.empty()
                and not self._tts_busy and not self._reply_pending()):
            self._set_speaking(False)
            await self.state("listening" if self.listening else "paused")

    async def _enqueue_say(self, sentence: str, message_id: str | None) -> None:
        if self._awaiting_reply:
            self._awaiting_reply = False
            if self._ack_task:
                self._ack_task.cancel()
        await self._say_q.put((self.gen, sentence, message_id))

    # -------------------------------------------------------------- tts #
    def _held(self) -> bool:
        return self._hold or self._duck or self.clock() < self._merge_until

    async def _stream_sentence(self, words: str, gen: int, mid: str | None, kind: str) -> bool:
        """Synthesise ``words`` and ship every chunk as it is produced.

        Synthesis starts AT ONCE, even while playback is held (merge window,
        duck): only the sending waits (review M1). Returns False when the
        engine failed before any audio."""
        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue()
        self._stop_tts.clear()
        stop = self._stop_tts

        def produce():
            REPLY_ACTIVITY.enter()
            try:
                for chunk in self.tts.stream(words, self.profile_id, stop):
                    loop.call_soon_threadsafe(q.put_nowait, ("chunk", chunk))
                    if stop.is_set():
                        break
            except Exception as e:      # surfaced on the loop side
                loop.call_soon_threadsafe(q.put_nowait, ("error", e))
            finally:
                REPLY_ACTIVITY.exit()
            loop.call_soon_threadsafe(q.put_nowait, ("done", None))

        t0 = self.clock()
        fut = loop.run_in_executor(None, produce)
        self.seq += 1
        n = 0
        ok = True
        try:
            while True:
                what, item = await q.get()
                if what == "done":
                    break
                if what == "error":
                    log.error("voice: TTS failed: %s", item)
                    ok = n > 0
                    if not ok:
                        await self.send({"type": "turn_error", "message_id": mid,
                                         "reason": "tts_failed", "detail": str(item)[:200]})
                    break
                while self._held() and gen == self.gen and not self._closed:
                    await asyncio.sleep(0.03)
                if gen != self.gen:
                    stop.set()
                    continue
                if not self.speaking:
                    self._set_speaking(True)
                    await self.state("speaking")
                self._client_drained = False
                if kind == "reply":
                    self._last_reply_seq = self.seq
                pcm = item.tobytes() if hasattr(item, "tobytes") else bytes(item)
                await self._send_audio({"type": "audio", "gen": gen, "seq": self.seq, "chunk": n,
                                        "sample_rate": self.tts.sample_rate,
                                        "text": words if n == 0 else None,
                                        "message_id": mid, "kind": kind}, pcm)
                if n == 0:
                    first_ms = round((self.clock() - t0) * 1000)
                    if kind == "reply" and not self._first_audio_logged and self._eot_at is not None:
                        self._first_audio_logged = True
                        ms = round((self.clock() - self._eot_at) * 1000)
                        log.info("voice: eot→first reply audio %d ms (first chunk %d ms)", ms, first_ms)
                        await self.send({"type": "metrics", "eot_to_first_audio_ms": ms,
                                         "first_chunk_ms": first_ms})
                n += 1
        finally:
            stop.set()
            with contextlib.suppress(Exception):
                await fut
        if n and kind == "reply":
            self._recent_said.append(words)
        return ok

    async def _tts_worker(self) -> None:
        while True:
            gen, sentence, mid = await self._say_q.get()
            self._tts_busy = True
            failed = False
            try:
                if gen != self.gen:
                    continue
                words = speakable(sentence)
                if words:
                    failed = not await self._stream_sentence(words, gen, mid, "reply")
            finally:
                self._tts_busy = False
            if failed and not self.speaking and self._say_q.empty():
                await self.state("listening" if self.listening else "paused")
            await self._maybe_finish_speaking()

    async def _play_ack(self) -> bool:
        """A pre-rendered acknowledgement in the session's voice, if any."""
        if not self.acks or self.speaking:
            return False
        pcm = random.choice(self.acks)
        self.seq += 1
        self._client_drained = False
        await self._send_audio({"type": "audio", "gen": self.gen, "seq": self.seq, "chunk": 0,
                                "sample_rate": self.tts.sample_rate, "text": None,
                                "message_id": None, "kind": "ack"},
                               np.asarray(pcm, "<i2").tobytes())
        return True

    # ------------------------------------------------------------ audio #
    def _set_speaking(self, on: bool) -> None:
        self.speaking = on
        # While we are talking, demand more sustained speech before treating
        # mic input as the driver — residual echo is short and bursty.
        self.detector.ep.cfg.min_speech_s = (
            self.settings.barge_in_speech_s if on else self._base[0])

    def _apply_patience(self, p: float) -> None:
        self.patience = p
        cfg = self.detector.ep.cfg
        cfg.turn_hard_silence_s = self._base[1] * p
        cfg.no_smart_turn_silence_s = self._base[2] * p

    async def barge_in(self, *, by_user_voice: bool, abort: bool | None = None) -> None:
        self.gen += 1
        self._stop_tts.set()
        self._duck = False
        for r in self._replies.values():
            r.muted = True
        while not self._say_q.empty():
            self._say_q.get_nowait()
        self._set_speaking(False)
        await self.send({"type": "barge_in", "gen": self.gen})
        do_abort = (by_user_voice and self.settings.barge_abort_turn) if abort is None else abort
        if do_abort and self.host.abort:
            with contextlib.suppress(Exception):
                await self.host.abort(self.thread_id)

    async def _set_duck(self, on: bool) -> None:
        """Soft barge-in (H3). ``on``: someone may be talking over the reply —
        hold further audio and ask the client to duck/pause playback. Nothing
        is cancelled until the transcript proves it was not our own echo."""
        if self._duck == on:
            return
        self._duck = on
        self._duck_at = self.clock()
        await self.send({"type": "duck", "on": on, "gen": self.gen})

    def _is_echo(self, text: str) -> bool:
        # STT writes numbers as digits ("at 3", "4.30") where we SAID words
        # ("at three", "four thirty"), so digit tokens are left out of the
        # comparison — otherwise every number-heavy sentence missed the 80 %
        # bar and our own echo barged in (seen in the echo probe).
        heard = [w for w in _norm_words(text) if not w.isdigit()]
        if not heard or not self._recent_said:
            return False
        said = set(_norm_words(" ".join(self._recent_said)))
        overlap = sum(1 for w in heard if w in said) / len(heard)
        return overlap >= 0.8 and len(heard) >= 2

    def _talking(self) -> bool:
        return self.speaking or not self._say_q.empty() or self._tts_busy

    async def _audio_worker(self) -> None:
        while True:
            chunk = await self._audio_q.get()
            # Coalesce whatever queued up while the last batch ran.
            parts = [chunk]
            while not self._audio_q.empty() and len(parts) < 50:
                parts.append(self._audio_q.get_nowait())
            if not self.listening:
                continue
            if not self.barge_in_enabled and self._talking():
                # "Interrupt by talking" is off: the mic is deaf while we talk.
                self._deaf = True
                continue
            if self._deaf:
                self._deaf = False
                self.detector.reset()
            events = await asyncio.to_thread(self.detector.feed, b"".join(parts))
            for ev in events:
                if isinstance(ev, SpeechStart):
                    if self.clock() < self._merge_until and self._last_submit:
                        # Maybe the driver wasn't finished: hold the reply
                        # until we know whether this is more of the same turn.
                        self._hold = True
                        if self.speaking:
                            await self._set_duck(True)
                    elif self._talking():
                        await self._set_duck(True)
                    await self.state("hearing")
                elif isinstance(ev, TurnEnd):
                    await self._on_turn_end(ev)
                elif isinstance(ev, Discarded):
                    if self._hold:
                        self._hold = False
                    await self._undo_duck()

    async def _undo_duck(self) -> None:
        if self._duck:
            await self._set_duck(False)
            if self.speaking or self._talking():
                await self.state("speaking")

    async def _on_turn_end(self, ev: TurnEnd) -> None:
        self._eot_at = self.clock()
        merging = self._hold and self._last_submit is not None
        await self.send({"type": "eot", "reason": ev.reason})
        await self.state("transcribing")
        try:
            text = await asyncio.to_thread(self.stt.transcribe, ev.audio)
        except Exception:
            log.exception("voice: STT failed")
            self._hold = False
            await self._undo_duck()
            await self.error("stt_failed", "Couldn't make that out. Try again.")
            await self.state(self._idle_state())
            return
        stt_ms = round((self.clock() - self._eot_at) * 1000)
        if self._is_echo(text):
            self._hold = False
            await self.send({"type": "echo_ignored", "text": text})
            await self._undo_duck()
            await self.state(self._idle_state())
            return
        if not text:
            self._hold = False
            await self.send({"type": "transcript", "text": ""})
            await self._undo_duck()
            await self.state(self._idle_state())
            return
        if not self._check_live():
            return
        prev = self._current
        if merging:
            prev_text, prev_id = self._last_submit
            prev_text = prev_text.rstrip().rstrip("?.!…").rstrip()
            text = f"{prev_text} {text}".strip()[:MAX_UTTERANCE_CHARS]
            # Abandon the early turn: stop it, mute anything it already said,
            # take its user row back, then send the whole utterance once.
            await self.barge_in(by_user_voice=False, abort=False)
            await self._abandon(prev, abort=True)
            if prev_id and self.host.retract:
                with contextlib.suppress(Exception):
                    await self.host.retract(self.thread_id, prev_id)
        elif self._duck or self.speaking:
            # Confirmed: real words over the reply. NOW cut it (and the run).
            if prev is not None and not prev.abandoned:
                await self.barge_in(by_user_voice=True, abort=False)
                await self._abandon(prev, abort=self.settings.barge_abort_turn)
            else:
                await self.barge_in(by_user_voice=True)
        elif prev is not None and not prev.closed:
            # Speech while still waiting on the last answer (review M4): the
            # new question replaces it — never two answers back to back.
            await self._abandon(prev, abort=True)
        self._hold = False
        try:
            msg_id = await self._submit(text)
        except Exception:
            log.exception("voice: submit failed")
            await self.send({"type": "transcript", "text": text, "merged": merging})
            await self._turn_failed(None, "agent_failed", "Could not send that message.")
            return
        # Track the turn BEFORE the next await: its echo and its run's
        # frames may already be on their way.
        turn = _Turn(msg_id=msg_id, text=text, at=self.clock(),
                     echoed=bool(msg_id) and msg_id in self._echoed_ids)
        self._current = turn
        if msg_id:
            self._turns.append(turn)
        if prev is not None and prev.abandoned and not turn.echoed:
            self._await_user_id = msg_id
        await self.send({"type": "transcript", "text": text, "merged": merging,
                         "message_id": msg_id})
        await self.send({"type": "metrics", "eot_to_transcript_ms": stt_ms,
                         "utterance_ms": round(len(ev.audio) / 16), "reason": ev.reason})
        self._last_submit = (text, msg_id)
        self._merge_until = self.clock() + self.settings.merge_window(self.patience)
        self._first_audio_logged = False
        self._awaiting_reply = True
        await self.state("thinking")
        acked = await self._play_ack()
        if self._ack_task:
            self._ack_task.cancel()
        if not acked:
            self._ack_task = asyncio.create_task(self._ack_later())

    async def _submit(self, text: str) -> str | None:
        if self._submit_opts:
            return await self.host.submit(self.thread_id, text,
                                          {"reply_style": self.reply_style,
                                           "follow_up": self.follow_up})
        return await self.host.submit(self.thread_id, text)

    async def _ack_later(self) -> None:
        await asyncio.sleep(self.settings.ack_after_s)
        if self._awaiting_reply:
            await self.send({"type": "ack"})

    # ---------------------------------------------------------- control #
    def _check_live(self) -> bool:
        try:
            return bool(self.host.session_live())
        except Exception:
            return False

    async def _resume_after(self, after: str | None, after_ids: list | None = None) -> None:
        """Speak what this device missed. ``after`` is the last reply it
        heard; ``after_ids`` (contract F1) are older ones, tried in order when
        ``after`` is unknown (a streamed reply's provisional id is never
        stored). ``replies_after`` returns None for an unknown id."""
        if not self.host.replies_after:
            return
        candidates = [a for a in [after, *(after_ids or [])] if isinstance(a, str) and a][:9]
        rows = None
        for cand in candidates:
            try:
                rows = await self.host.replies_after(self.thread_id, cand)
            except Exception:
                log.exception("voice: replies_after failed")
                return
            if rows is not None:
                break
        if not rows:
            return
        for mid, content in rows[-3:]:      # never read out a backlog of more than 3
            if mid in self._spoken_ids:
                continue
            self._spoken_ids.append(mid)
            for s, m in self._reply_text_update(mid, content, None, True, mid):
                await self._enqueue_say(s, m)

    def _apply_start(self, msg: dict) -> None:
        if "patience" in msg:
            self._apply_patience(_patience_value(msg.get("patience"), self.settings.patience))
        if "barge_in" in msg:
            v = msg.get("barge_in")
            self.barge_in_enabled = v if isinstance(v, bool) else True
        if "reply_style" in msg:
            v = msg.get("reply_style")
            self.reply_style = v if v in REPLY_STYLES else "normal"
        if "follow_up" in msg:
            v = msg.get("follow_up")
            self.follow_up = v if isinstance(v, bool) else True

    async def _undo(self) -> None:
        t = self._current
        # Once the reply has started (a sentence queued) or the turn is
        # answered, Undo would delete the question and orphan the stored
        # answer below it (review M1): too late, like the 8 s window.
        if (t is None or not t.msg_id or t.abandoned or t.spoke or t.closed
                or self.clock() - t.at > UNDO_WINDOW_S):
            await self.error("undo_expired", "Too late to take that back.")
            return
        await self.barge_in(by_user_voice=False, abort=False)
        await self._abandon(t, abort=True)
        if self.host.retract:
            with contextlib.suppress(Exception):
                await self.host.retract(self.thread_id, t.msg_id)
        self._last_submit = None
        self._merge_until = 0.0
        self._hold = False
        await self.send({"type": "undone", "message_id": t.msg_id})
        await self.state("listening" if self.listening else "paused")

    async def _cancel(self) -> None:
        await self.barge_in(by_user_voice=False, abort=False)
        t = self._current
        if t is not None and not t.closed:
            await self._abandon(t, abort=True)
        self._merge_until = 0.0
        self._hold = False
        await self.state("listening" if self.listening else "paused")

    async def handle_control(self, msg: dict) -> None:
        t = msg.get("type")
        if t == "start":
            self._apply_start(msg)
            if not self.listening:
                self.listening = True
                self.detector.reset()
            await self.state(self._idle_state())
            after = msg.get("after")
            after = after if isinstance(after, str) else None
            ids = msg.get("after_ids")
            ids = [i for i in ids if isinstance(i, str)][:8] if isinstance(ids, list) else []
            key = (after, tuple(ids))
            if key != self._last_after:        # a settings re-send is not a resume
                self._last_after = key
                await self._resume_after(after, ids)
        elif t == "pause":
            self.listening = False
            self._hold = False
            self.detector.reset()
            await self._undo_duck()
            await self.state("paused")
        elif t == "stop_playback":
            await self.barge_in(by_user_voice=False)
            await self.state("listening" if self.listening else "paused")
        elif t == "playback_done":
            # Optional `seq`: a drain report for audio older than the last
            # reply sentence we sent (an ack, an earcon-length filler) never
            # ends the speaking state.
            seq = msg.get("seq")
            stale = isinstance(seq, int) and not isinstance(seq, bool) and seq < self._last_reply_seq
            if msg.get("gen") == self.gen and not stale:
                self._client_drained = True
                await self._maybe_finish_speaking()
        elif t == "cancel":
            await self._cancel()
        elif t == "undo":
            await self._undo()
        elif t == "ping":
            self._pinged = True
            out = {"type": "pong"}
            if isinstance(msg.get("t"), (int, float, str)) and not isinstance(msg.get("t"), bool):
                out["t"] = msg["t"] if not isinstance(msg["t"], str) else msg["t"][:32]
            await self.send(out)

    def _maybe_touch(self) -> None:
        if self.host.touch is None:
            return
        now = self.clock()
        if now - self._last_touch < TOUCH_EVERY_S:
            return
        self._last_touch = now
        try:
            self.host.touch()
        except Exception:
            log.exception("voice: session touch failed")

    def _take_token(self) -> bool:
        now = self.clock()
        self._tokens = min(float(RATE_BURST),
                           self._tokens + max(0.0, now - self._tokens_at) * RATE_PER_S)
        self._tokens_at = now
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        return False

    async def _limited(self, code: str, message: str) -> None:
        """At most one error frame per code every 2 s (a flood stays a flood
        on our side only)."""
        now = self.clock()
        if now - self._last_limited.get(code, float("-inf")) >= 2.0:
            self._last_limited[code] = now
            await self.error(code, message)

    async def _guarded(self, name: str, worker: Callable[[], Awaitable[None]]) -> None:
        """Run a worker; if it dies, tell the client and end the session.

        Without this a crashed worker left a zombie: the socket stayed open,
        the client kept showing "Listening", and nothing was ever heard again."""
        try:
            await worker()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("voice: %s worker failed; ending the session", name)
            with contextlib.suppress(Exception):
                await self.error("unavailable", "Drive mode hit an internal error and stopped.")
            self.end(1011, "internal error")

    async def _watchdog(self) -> None:
        """Timers that must fire even when the socket is silent: the session
        re-check (M7), the heartbeat (H2), a turn that never answers (H1) and
        a duck that never became a transcript (H3)."""
        while True:
            await asyncio.sleep(self.watch_tick_s)
            now = self.clock()
            if now - self._last_live_check >= LIVE_CHECK_S:
                self._last_live_check = now
                if not self._check_live():
                    await self.send({"type": "locked"})
                    self.end(1008, "locked")
                    return
            if self._pinged and now - self._last_rx > DEAD_AFTER_S:
                log.info("voice: no heartbeat for %.0f s; closing", now - self._last_rx)
                self.end(1001, "heartbeat lost")
                return
            t = self._current
            if (t is not None and not t.closed and not t.spoke
                    and now - t.at > TURN_TIMEOUT_S):
                # Not abandoned: if the answer does land while the driver is
                # still here, it is read out like any other reply.
                await self._turn_failed(t, "timeout")
            if self._duck and now - self._duck_at > DUCK_MAX_S:
                await self._undo_duck()

    async def run(self) -> None:
        """Receive loop. Returns when the socket closes, the session locks,
        is replaced, or a worker fails; ``close_code`` says how to close."""
        sr_out = getattr(self.tts, "sample_rate", 24000)
        self._last_live_check = self.clock()
        await self.send({"type": "ready", "sample_rate_in": 16000,
                         "sample_rate_out": sr_out,
                         "smart_turn": self.detector.ep.turn_fn is not None,
                         "patience": self.patience,
                         "voice_acks": len(self.acks),
                         "barge_in": self.barge_in_enabled,
                         "reply_style": self.reply_style,
                         "follow_up": self.follow_up,
                         "sample_format": getattr(self.tts, "sample_format", "pcm_s16le"),
                         "streaming_tts": bool(getattr(self.tts, "streaming", False))})
        tasks = [asyncio.create_task(self._guarded(n, c)) for n, c in
                 (("audio", self._audio_worker), ("tts", self._tts_worker),
                  ("frames", self._frames_worker), ("watchdog", self._watchdog))]
        fatal = asyncio.create_task(self._fatal.wait())
        recv: asyncio.Task | None = None
        try:
            while True:
                recv = asyncio.create_task(self.ws.receive())
                await asyncio.wait({recv, fatal}, return_when=asyncio.FIRST_COMPLETED)
                if not recv.done():
                    break                     # ended from inside (worker, lapse, eviction)
                m = recv.result()
                recv = None
                if m.get("type") == "websocket.disconnect":
                    break
                now = self.clock()
                self._last_rx = now
                if now - self._last_live_check > 1.0:
                    self._last_live_check = now
                    if not self._check_live():
                        await self.send({"type": "locked"})
                        self.close_code = (1008, "locked")
                        break
                if m.get("bytes") is not None:
                    if self.listening:
                        self._maybe_touch()
                    try:
                        self._audio_q.put_nowait(m["bytes"])
                    except asyncio.QueueFull:
                        pass   # the CPU is behind; dropping beats unbounded lag
                elif m.get("text") is not None:
                    if not self._take_token():
                        await self._limited("rate_limited", "Too many requests; slow down.")
                        continue
                    try:
                        msg = json.loads(m["text"])
                    except ValueError:
                        msg = None
                    if not isinstance(msg, dict) or not isinstance(msg.get("type"), str):
                        await self._limited("bad_frame", "That message wasn't understood.")
                        continue
                    if msg.get("type") != "ping":
                        self._maybe_touch()
                    await self.handle_control(msg)
        finally:
            for t in (recv, fatal):
                if t is not None and not t.done():
                    t.cancel()
            self._closed = True
            self._stop_tts.set()
            for t in [*tasks, *self._turn_tasks]:
                t.cancel()
            if self._ack_task:
                self._ack_task.cancel()
            await asyncio.gather(*tasks, *self._turn_tasks, return_exceptions=True)
