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

* ``{"type":"start", "after": <message id|null>, "patience": "quick|normal|patient"|<float>}``
  — begin/resume listening; replies newer than ``after`` that this device has
  not heard are spoken (how a reconnect after a data drop picks the thread up).
* ``{"type":"pause"}`` — stop listening (audio ignored), keep speaking.
* ``{"type":"stop_playback"}`` — the user tapped to silence the reply.
* ``{"type":"playback_done", "gen": n}`` — the client's audio queue drained.
* ``{"type":"ping"}``.

Server → client (JSON unless noted):

* ``ready`` {sample_rate_in, sample_rate_out, sample_format, streaming_tts, smart_turn, patience, voice_acks}
* ``state`` {state: listening|hearing|transcribing|thinking|speaking|paused}
* ``eot`` {reason} — end of turn heard; the client plays its earcon NOW
* ``transcript`` {text, merged} — empty text = nothing intelligible was heard
* ``echo_ignored`` {text} — the mic picked up our own voice; not sent
* ``ack`` — no reply and no voice ack within ``ack_after_s``: "still working"
* ``audio`` {gen, seq, chunk, sample_rate, text, message_id, kind}
  followed IMMEDIATELY by one binary frame (PCM16LE mono). A sentence arrives
  as several chunks with the same seq; ``kind`` is ``reply`` or ``ack``.
* ``barge_in`` {gen} — drop every queued/playing chunk with gen < this
* ``metrics`` {eot_to_transcript_ms | eot_to_first_audio_ms | first_chunk_ms ...}
* ``locked`` — the unlocked session lapsed; the socket closes next
* ``error`` {message}
"""
from __future__ import annotations

import asyncio
import contextlib
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

from .config import PATIENCE
from .tts import SentenceSplitter, speakable
from .vad_turn import Discarded, SpeechStart, TurnDetector, TurnEnd

log = logging.getLogger("dispatch.voice")


@dataclass
class VoiceHost:
    """What the app lends a voice session. main.py builds one per socket."""
    #: Persist ``text`` as a user message in the thread and run the normal
    #: agent turn (voice persona, no-thinking). Returns the new message id.
    submit: Callable[[str, str], Awaitable[str | None]]
    #: Is the unlocked session that opened this socket still valid?
    session_live: Callable[[], bool] = lambda: True
    #: Stop the agent turn the thread is waiting on (barge-in / merge).
    abort: Callable[[str], Awaitable[None]] | None = None
    #: Remove a user row this session submitted (turn merge).
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


@dataclass
class _Reply:
    splitter: SentenceSplitter = field(default_factory=SentenceSplitter)
    consumed: int = 0
    done: bool = False
    muted: bool = False
    updated: float = 0.0


def _norm_words(s: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", s.lower())


def _patience_value(v, default: float) -> float:
    if isinstance(v, str) and v in PATIENCE:
        return PATIENCE[v]
    if isinstance(v, (int, float)) and 0.3 <= float(v) <= 3.0:
        return float(v)
    return default


class VoiceSession:
    def __init__(self, ws, *, thread_id: str, settings, detector: TurnDetector,
                 stt_engine, tts_engine, host: VoiceHost,
                 profile_id: str | None = None, acks: list | None = None,
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
        self.clock = clock

        self.listening = False
        self.gen = 0                      # bumps on every barge-in
        self.seq = 0
        self.speaking = False             # audio sent and not yet confirmed played
        self.patience = settings.patience
        self._audio_q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=400)
        self._say_q: asyncio.Queue[tuple[int, str, str | None]] = asyncio.Queue()
        self._frames_q: asyncio.Queue[dict] = asyncio.Queue(maxsize=2000)
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
        self._fatal = asyncio.Event()     # a worker died; run() must end
        self._base = (detector.ep.cfg.min_speech_s, detector.ep.cfg.turn_hard_silence_s,
                      detector.ep.cfg.no_smart_turn_silence_s)
        # turn merge
        self._merge_until = 0.0           # replies held until this clock time
        self._hold = False                # held while the driver is mid-resume
        self._last_submit: tuple[str, str | None] | None = None   # (text, msg id)
        self._await_user_id: str | None = None   # ignore replies until this row echoes

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
        await self.send({"type": "state", "state": s})

    # ------------------------------------------------------- app frames #
    def on_app_frame(self, frame: dict) -> None:
        """Called (synchronously) for every frame the app broadcasts."""
        if frame.get("thread_id") != self.thread_id:
            return
        if frame.get("type") not in ("stream_chunk", "stream_done", "message"):
            return
        with contextlib.suppress(asyncio.QueueFull):
            self._frames_q.put_nowait(frame)

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

    def handle_reply_frame(self, f: dict) -> list[tuple[str, str | None]]:
        """Reply frame → sentences to say. Pure bookkeeping (testable)."""
        t = f.get("type")
        msg = f.get("message") or {}
        if self._await_user_id is not None:
            # After a merge, everything until the merged user row echoes back
            # belongs to the abandoned turn.
            if t == "message" and msg.get("role") == "user" and msg.get("id") == self._await_user_id:
                self._await_user_id = None
            else:
                for rid in (f.get("message_id"), f.get("provisional_id"), msg.get("id")):
                    if msg.get("role") != "user":
                        self._mute(rid)
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

    async def _frames_worker(self) -> None:
        while True:
            f = await self._frames_q.get()
            for sentence, mid in self.handle_reply_frame(f):
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
        return self._hold or self.clock() < self._merge_until

    async def _stream_sentence(self, words: str, gen: int, mid: str | None, kind: str) -> None:
        """Synthesise ``words`` and ship every chunk as it is produced."""
        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue()
        self._stop_tts.clear()
        stop = self._stop_tts

        def produce():
            try:
                for chunk in self.tts.stream(words, self.profile_id, stop):
                    loop.call_soon_threadsafe(q.put_nowait, ("chunk", chunk))
                    if stop.is_set():
                        break
            except Exception as e:      # surfaced on the loop side
                loop.call_soon_threadsafe(q.put_nowait, ("error", e))
            loop.call_soon_threadsafe(q.put_nowait, ("done", None))

        t0 = self.clock()
        fut = loop.run_in_executor(None, produce)
        self.seq += 1
        n = 0
        try:
            while True:
                what, item = await q.get()
                if what == "done":
                    break
                if what == "error":
                    log.error("voice: TTS failed: %s", item)
                    await self.send({"type": "error", "message": "Speech synthesis failed."})
                    break
                if gen != self.gen:
                    stop.set()
                    continue
                if not self.speaking:
                    self._set_speaking(True)
                    await self.state("speaking")
                self._client_drained = False
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

    async def _tts_worker(self) -> None:
        while True:
            gen, sentence, mid = await self._say_q.get()
            self._tts_busy = True
            try:
                while self._held() and gen == self.gen and not self._closed:
                    await asyncio.sleep(0.05)
                if gen != self.gen:
                    continue
                words = speakable(sentence)
                if words:
                    await self._stream_sentence(words, gen, mid, "reply")
            finally:
                self._tts_busy = False
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
        # While we are talking, demand a little more sustained speech before
        # treating mic input as a barge-in — residual echo is short and bursty.
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

    def _is_echo(self, text: str) -> bool:
        heard = _norm_words(text)
        if not heard or not self._recent_said:
            return False
        said = set(_norm_words(" ".join(self._recent_said)))
        overlap = sum(1 for w in heard if w in said) / len(heard)
        return overlap >= 0.8 and len(heard) >= 2

    async def _audio_worker(self) -> None:
        while True:
            chunk = await self._audio_q.get()
            # Coalesce whatever queued up while the last batch ran.
            parts = [chunk]
            while not self._audio_q.empty() and len(parts) < 50:
                parts.append(self._audio_q.get_nowait())
            if not self.listening:
                continue
            events = await asyncio.to_thread(self.detector.feed, b"".join(parts))
            for ev in events:
                if isinstance(ev, SpeechStart):
                    if self.clock() < self._merge_until and self._last_submit:
                        # Maybe the driver wasn't finished: hold the reply
                        # until we know whether this is more of the same turn.
                        self._hold = True
                        if self.speaking:
                            await self.barge_in(by_user_voice=False, abort=False)
                    elif self.speaking or not self._say_q.empty():
                        await self.barge_in(by_user_voice=True)
                    await self.state("hearing")
                elif isinstance(ev, TurnEnd):
                    await self._on_turn_end(ev)
                elif isinstance(ev, Discarded) and self._hold:
                    self._hold = False

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
            await self.send({"type": "error", "message": "Speech recognition failed."})
            await self.state("listening")
            return
        stt_ms = round((self.clock() - self._eot_at) * 1000)
        if self._is_echo(text):
            self._hold = False
            await self.send({"type": "echo_ignored", "text": text})
            await self.state("listening")
            return
        if not text:
            self._hold = False
            await self.send({"type": "transcript", "text": ""})
            await self.state("listening")
            return
        if not self._check_live():
            return
        if merging:
            prev_text, prev_id = self._last_submit
            text = f"{prev_text.rstrip()} {text}".strip()[:MAX_UTTERANCE_CHARS]
            # Abandon the early turn: stop it, mute anything it already said,
            # take its user row back, then send the whole utterance once.
            await self.barge_in(by_user_voice=False, abort=True)
            if prev_id and self.host.retract:
                with contextlib.suppress(Exception):
                    await self.host.retract(self.thread_id, prev_id)
        self._hold = False
        await self.send({"type": "transcript", "text": text, "merged": merging})
        await self.send({"type": "metrics", "eot_to_transcript_ms": stt_ms,
                         "utterance_ms": round(len(ev.audio) / 16), "reason": ev.reason})
        try:
            msg_id = await self.host.submit(self.thread_id, text)
        except Exception:
            log.exception("voice: submit failed")
            await self.send({"type": "error", "message": "Could not send that message."})
            await self.state("listening")
            return
        if merging:
            self._await_user_id = msg_id
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

    async def _resume_after(self, after: str | None) -> None:
        if not after or not self.host.replies_after:
            return
        try:
            rows = await self.host.replies_after(self.thread_id, after)
        except Exception:
            log.exception("voice: replies_after failed")
            return
        for mid, content in rows[-3:]:      # never read out a backlog of more than 3
            if mid in self._spoken_ids:
                continue
            self._spoken_ids.append(mid)
            for s, m in self._reply_text_update(mid, content, None, True, mid):
                await self._enqueue_say(s, m)

    async def handle_control(self, msg: dict) -> None:
        t = msg.get("type")
        if t == "start":
            self.listening = True
            self.detector.reset()
            if "patience" in msg:
                self._apply_patience(_patience_value(msg.get("patience"), self.settings.patience))
            await self.state("listening")
            after = msg.get("after")
            await self._resume_after(after if isinstance(after, str) else None)
        elif t == "pause":
            self.listening = False
            self._hold = False
            self.detector.reset()
            await self.state("paused")
        elif t == "stop_playback":
            await self.barge_in(by_user_voice=False)
            await self.state("listening" if self.listening else "paused")
        elif t == "playback_done":
            if msg.get("gen") == self.gen:
                self._client_drained = True
                await self._maybe_finish_speaking()
        elif t == "ping":
            await self.send({"type": "pong"})

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
                await self.send({"type": "error",
                                 "message": "Drive mode hit an internal error and stopped."})
            self._fatal.set()

    async def run(self) -> None:
        """Receive loop. Returns when the socket closes, the session locks,
        or a worker fails (after an ``error`` frame; the caller closes)."""
        sr_out = getattr(self.tts, "sample_rate", 24000)
        await self.send({"type": "ready", "sample_rate_in": 16000,
                         "sample_rate_out": sr_out,
                         "smart_turn": self.detector.ep.turn_fn is not None,
                         "patience": self.patience,
                         "voice_acks": len(self.acks),
                         "sample_format": getattr(self.tts, "sample_format", "pcm_s16le"),
                         "streaming_tts": bool(getattr(self.tts, "streaming", False))})
        tasks = [asyncio.create_task(self._guarded(n, c)) for n, c in
                 (("audio", self._audio_worker), ("tts", self._tts_worker),
                  ("frames", self._frames_worker))]
        fatal = asyncio.create_task(self._fatal.wait())
        recv: asyncio.Task | None = None
        try:
            while True:
                recv = asyncio.create_task(self.ws.receive())
                await asyncio.wait({recv, fatal}, return_when=asyncio.FIRST_COMPLETED)
                if not recv.done():
                    break                     # a worker died
                m = recv.result()
                recv = None
                if m.get("type") == "websocket.disconnect":
                    break
                now = self.clock()
                if now - self._last_live_check > 1.0:
                    self._last_live_check = now
                    if not self._check_live():
                        await self.send({"type": "locked"})
                        break
                if m.get("bytes") is not None:
                    if self.listening:
                        self._maybe_touch()
                    try:
                        self._audio_q.put_nowait(m["bytes"])
                    except asyncio.QueueFull:
                        pass   # the CPU is behind; dropping beats unbounded lag
                elif m.get("text") is not None:
                    try:
                        msg = json.loads(m["text"])
                    except ValueError:
                        continue
                    if isinstance(msg, dict):
                        if msg.get("type") != "ping":
                            self._maybe_touch()
                        await self.handle_control(msg)
        finally:
            for t in (recv, fatal):
                if t is not None and not t.done():
                    t.cancel()
            self._closed = True
            self._stop_tts.set()
            for t in tasks:
                t.cancel()
            if self._ack_task:
                self._ack_task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
