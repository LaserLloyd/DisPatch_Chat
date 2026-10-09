"""Text-to-speech for Drive mode: sentence splitting + a swappable engine.

The reply arrives as a token stream (``stream_chunk`` frames, or one whole
``message``). Waiting for the end would put the whole reply's generation time
in front of the first word, so :class:`SentenceSplitter` hands out each
sentence as soon as it is complete and the session synthesises it while the
next one is still being written.

What gets spoken is not what is on screen: :func:`speakable` drops code
blocks, links, media directives, reaction markers, markdown punctuation and
emoji. A driver cannot use a code block read aloud; "code is on the screen"
is the honest substitute.

Engines share one small interface (see :class:`TTSEngine`), all blocking —
the session runs them in a worker thread:

* ``sample_rate`` / ``sample_format`` (``"pcm_s16le"``) / ``channels`` (1) /
  ``streaming`` are declared on the engine;
* ``stream(text, profile_id=None, stop=None)`` yields int16 chunks;
* ``synth(text, profile_id=None) -> (int16 ndarray, rate)``.

``profile_id`` is opaque here: it names a voice the engine knows (a cloned
voice from an extension, or a built-in voice name). ``None`` = the default.

Built in:

* :class:`KokoroTTS` — Kokoro 82M int8 ONNX (Apache-2.0 weights, MIT
  ``kokoro-onnx`` runtime; its phonemizer/espeak-ng chain is GPL-3.0),
  ungated, CPU, built-in voices only. The FALLBACK, and the default when the
  cloned-voice engine is not installed. It does not stream inside a sentence,
  so first audio = the time to synthesise the first (deliberately short)
  sentence; see docs/voice-drive-mode.md for numbers.
* ``chatterbox`` (app/voice/dv.py) — the ``dispatch-voice`` package:
  Chatterbox-Turbo ONNX (MIT, ungated), cloned voice profiles. The DEFAULT
  when installed, with Kokoro behind it.
* :class:`HQTTS` — optional "HQ" engine behind an OpenAI-compatible
  ``/v1/audio/speech``; always wrapped in :class:`FallbackTTS`.
* :class:`MiniMaxTTS` — MiniMax T2A v2 over HTTPS, opt-in only (network
  dependency; Token Plan coverage of speech minutes NOT verified).

Other engines (e.g. a voice-cloning engine shipped as an extension) register
through ``app.voice.registry.register_engine`` and are picked by name with
``DISPATCH_VOICE_TTS``.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
from collections.abc import AsyncIterator, Iterable, Iterator
from typing import Protocol

import numpy as np

log = logging.getLogger("dispatch.voice")

# --------------------------------------------------------------------------- #
# Cleaning
# --------------------------------------------------------------------------- #

_FENCE_RE = re.compile(r"```.*?(```|$)", re.S)
_DIRECTIVE_RE = re.compile(r"\[\[(?:media|doc|pic|reply_to[^\]]*)[^\]]*\]\]", re.I)
_REACT_RE = re.compile(r":react:[A-Za-z0-9_\-]+:", re.I)
_IMG_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK_RE = re.compile(r"\[([^\]]+)\]\((?:[^)]*)\)")
_URL_RE = re.compile(r"\bhttps?://\S+|\bwww\.\S+", re.I)
_INLINE_CODE_RE = re.compile(r"`([^`]*)`")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s*", re.M)
_QUOTE_RE = re.compile(r"^\s*>\s?", re.M)
_BULLET_RE = re.compile(r"^\s*(?:[-*+•]|\d+[.)])\s+", re.M)
_TABLE_RULE_RE = re.compile(r"^\s*\|?\s*:?-{2,}.*$", re.M)
_EMPH_RE = re.compile(r"(\*\*|__|\*|_|~~)(?=\S)(.+?)(?<=\S)\1")
_HTML_RE = re.compile(r"<[^>]{1,200}>")
# Emoji & pictographs, dingbats, variation selectors, ZWJ, keycaps, flags.
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF"
    "\U0000FE00-\U0000FE0F\U0000200D\U000020E3\U00002B00-\U00002BFF]+")


def speakable(text: str) -> str:
    """Markdown reply → plain words worth saying out loud ('' = nothing)."""
    if not text:
        return ""
    t = _FENCE_RE.sub(" The code is on the screen. ", text)
    t = _DIRECTIVE_RE.sub(" ", t)
    t = _REACT_RE.sub(" ", t)
    t = _IMG_RE.sub(" ", t)
    t = _LINK_RE.sub(r"\1", t)
    t = _URL_RE.sub(" a link ", t)
    t = _INLINE_CODE_RE.sub(r"\1", t)
    t = _TABLE_RULE_RE.sub(" ", t)
    t = _HEADING_RE.sub("", t)
    t = _QUOTE_RE.sub("", t)
    t = _BULLET_RE.sub("", t)
    for _ in range(2):
        t = _EMPH_RE.sub(r"\2", t)
    t = _HTML_RE.sub(" ", t)
    t = t.replace("|", ", ")
    t = _EMOJI_RE.sub(" ", t)
    t = re.sub(r"\s+", " ", t).strip()
    # Nothing but punctuation left (an all-emoji reply) is not worth a breath.
    return t if re.search(r"[0-9A-Za-zÀ-￿]", t) else ""


# --------------------------------------------------------------------------- #
# Sentence splitting
# --------------------------------------------------------------------------- #

_ABBREV = {"mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "etc", "e.g", "i.e",
           "approx", "no", "fig", "inc", "ltd", "co", "jan", "feb", "mar", "apr", "jun",
           "jul", "aug", "sep", "sept", "oct", "nov", "dec", "a.m", "p.m", "u.s"}
_END_RE = re.compile(r"([.!?…]+[\"')\]]*)(\s+)|(\n\s*\n)|(\n)(?=\s*(?:[-*+•]|\d+[.)])\s)")


class SentenceSplitter:
    """Push text as it streams; get back complete sentences.

    Holds back while a code fence or a ``[[…]]`` directive is still open, so a
    sentence never ends inside one. A first chunk that runs long without a
    full stop is cut at a comma — the first words matter most for latency.
    """

    def __init__(self, first_soft_chars: int = 80, hard_chars: int = 260,
                 min_words: int = 4) -> None:
        self._buf = ""
        self._held = ""          # a too-short sentence waiting for company
        self._first = True
        self.min_words = min_words
        self.first_soft_chars = first_soft_chars
        self.hard_chars = hard_chars

    def push(self, text: str) -> list[str]:
        self._buf += text
        return self._drain(final=False)

    def flush(self) -> list[str]:
        out = self._drain(final=True)
        rest = " ".join(p for p in (self._held, self._buf.strip()) if p)
        self._buf = self._held = ""
        if rest:
            out.append(rest)
        return out

    def _open_construct(self, s: str) -> int | None:
        """Index where an unclosed fence/directive starts, else None."""
        fences = [m.start() for m in re.finditer(r"```", s)]
        if len(fences) % 2:
            return fences[-1]
        i = s.rfind("[[")
        if i != -1 and s.find("]]", i) == -1:
            return i
        return None

    def _drain(self, final: bool) -> list[str]:
        out: list[str] = []
        while True:
            hold = None if final else self._open_construct(self._buf)
            scan = self._buf if hold is None else self._buf[:hold]
            cut = self._find_cut(scan, final)
            if cut is None:
                break
            sent, self._buf = self._buf[:cut].strip(), self._buf[cut:].lstrip()
            if not sent:
                continue
            # Prosody: a two-word fragment on its own sounds clipped. Hold it
            # and say it together with the next sentence.
            joined = f"{self._held} {sent}".strip() if self._held else sent
            if len(joined.split()) < self.min_words and not final:
                self._held = joined
                continue
            self._held = ""
            out.append(joined)
            self._first = False
        return out

    @staticmethod
    def _protected(s: str) -> list[tuple[int, int]]:
        """Spans a sentence must not end inside: closed fences and directives."""
        spans = [m.span() for m in re.finditer(r"```.*?```", s, re.S)]
        spans += [m.span() for m in re.finditer(r"\[\[.*?\]\]", s, re.S)]
        return spans

    def _find_cut(self, s: str, final: bool) -> int | None:
        spans = self._protected(s)
        for m in _END_RE.finditer(s):
            if any(a <= m.start() < b for a, b in spans):
                continue
            if m.group(1):
                end_word = s[: m.start(1)].rsplit(None, 1)
                word = end_word[-1].lower().rstrip(".") if end_word else ""
                if m.group(1).startswith(".") and (word in _ABBREV or (len(word) == 1 and word.isalpha())):
                    continue
                # A decimal ("3. 5" never happens; "3.5" has no space) — fine.
                return m.end(1)
            if m.group(3):
                return m.end(3)
            if m.group(4):
                return m.end(4)
        limit = self.first_soft_chars if self._first else self.hard_chars
        if spans and len(s) > limit and not final:
            # Never cut a code block or directive in half: wait for its end.
            a, b = spans[0]
            if a < limit:
                return b if len(s) > b and b > 0 and a == 0 else (a if a > 20 else None)
        if len(s) > limit and not final:
            window = s[:limit]
            j = max(window.rfind(", "), window.rfind("; "), window.rfind(": "), window.rfind(" — "))
            if j > 20:
                return j + 1
            if len(s) > self.hard_chars:
                k = window.rfind(" ")
                return k if k > 0 else limit
        return None


# --------------------------------------------------------------------------- #
# Engines
# --------------------------------------------------------------------------- #

def to_pcm16(audio) -> np.ndarray:
    a = np.clip(np.asarray(audio, np.float32).reshape(-1), -1.0, 1.0)
    return (a * 32767.0).astype("<i2")


class TTSEngine(Protocol):
    """What the session needs from a TTS engine."""
    sample_rate: int
    sample_format: str        # always "pcm_s16le" today
    channels: int             # always 1
    streaming: bool           # yields several chunks per sentence

    def stream(self, text: str, profile_id: str | None = None,
               stop: threading.Event | None = None) -> Iterator[np.ndarray]: ...

    def synth(self, text: str, profile_id: str | None = None) -> tuple[np.ndarray, int]: ...


class _Base:
    sample_format = "pcm_s16le"
    channels = 1
    streaming = False

    def stream(self, text, profile_id=None, stop=None):
        if stop is not None and stop.is_set():
            return
        pcm, _ = self.synth(text, profile_id)
        yield pcm


class KokoroTTS(_Base):
    sample_rate = 24000

    def __init__(self, model_path, voices_path, voice: str = "af_heart",
                 speed: float = 1.05, threads: int = 8) -> None:
        import onnxruntime as ort
        from kokoro_onnx import Kokoro
        so = ort.SessionOptions()
        so.intra_op_num_threads = max(1, threads)
        so.log_severity_level = 3
        sess = ort.InferenceSession(str(model_path), sess_options=so,
                                    providers=["CPUExecutionProvider"])
        self._k = Kokoro.from_session(sess, str(voices_path))
        self.voice = voice
        self.speed = speed
        self.voices = set(self._k.get_voices())
        self._lock = threading.Lock()

    def synth(self, text: str, profile_id: str | None = None) -> tuple[np.ndarray, int]:
        # A profile_id this engine does not know (a cloned voice meant for
        # another engine) falls back to the default voice rather than failing.
        voice = profile_id if profile_id in self.voices else self.voice
        with self._lock:
            audio, sr = self._k.create(text, voice=voice, speed=self.speed, lang="en-us")
        return to_pcm16(audio), sr


class MiniMaxTTS(_Base):
    """MiniMax T2A v2 (non-streaming per sentence). Untested against the live
    API from this repo — see docs/voice-drive-mode.md."""

    sample_rate = 24000

    def __init__(self, api_key: str, model: str, voice: str, base_url: str) -> None:
        self.api_key = api_key
        self.model = model
        self.voice = voice
        self.url = base_url.rstrip("/") + "/v1/t2a_v2"

    def synth(self, text: str, profile_id: str | None = None) -> tuple[np.ndarray, int]:
        import httpx
        body = {
            "model": self.model, "text": text, "stream": False,
            "voice_setting": {"voice_id": self.voice, "speed": 1.0, "vol": 1.0, "pitch": 0},
            "audio_setting": {"sample_rate": self.sample_rate, "format": "pcm", "channel": 1},
        }
        r = httpx.post(self.url, json=body, timeout=20.0,
                       headers={"Authorization": f"Bearer {self.api_key}"})
        r.raise_for_status()
        data = r.json()
        base = data.get("base_resp") or {}
        if base.get("status_code") not in (0, None):
            raise RuntimeError(f"MiniMax T2A error {base.get('status_code')}")
        audio_hex = (data.get("data") or {}).get("audio") or ""
        return np.frombuffer(bytes.fromhex(audio_hex), dtype="<i2"), self.sample_rate


class HQTTS(_Base):
    """OpenAI-compatible ``POST {url}/v1/audio/speech`` returning raw PCM16
    mono at ``sample_rate`` (``response_format: "pcm"``), e.g. a GPU engine on
    another machine. NOT verified against a live server — no such server
    exists yet; this is the contract it is wired against."""

    def __init__(self, url: str, model: str, timeout_s: float, sample_rate: int) -> None:
        self.url = url.rstrip("/") + "/v1/audio/speech"
        self.model = model
        self.timeout_s = timeout_s
        self.sample_rate = sample_rate

    def synth(self, text: str, profile_id: str | None = None) -> tuple[np.ndarray, int]:
        import httpx
        body = {"model": self.model, "input": text, "response_format": "pcm",
                "sample_rate": self.sample_rate}
        if profile_id:
            body["voice"] = profile_id
        r = httpx.post(self.url, json=body, timeout=self.timeout_s)
        r.raise_for_status()      # 409 (lease) / 503 (priority hold) -> fallback
        return np.frombuffer(r.content, dtype="<i2"), self.sample_rate


class FallbackTTS:
    """Speak with ``primary``; if it fails BEFORE producing audio, speak that
    sentence with ``fallback``.

    Two uses: a remote "HQ" engine with a local fallback (``cooldown_s`` keeps
    a dead remote from costing one timeout per sentence), and the cloned-voice
    engine with the built-in voice behind it (no voice profile chosen yet →
    ``LookupError`` → built-in voice; no cooldown, so the moment a default
    profile exists it is used). An error after audio has started ends that
    sentence instead — two voices spliced mid-sentence is worse than a cut.
    Both engines must share the sample rate; a primary that does not is
    treated as failed.
    """

    sample_format = "pcm_s16le"
    channels = 1

    def __init__(self, primary, fallback, cooldown_s: float = 120.0) -> None:
        self.primary, self.fallback = primary, fallback
        self.sample_rate = fallback.sample_rate
        self.streaming = bool(getattr(primary, "streaming", False))
        self.cooldown_s = cooldown_s
        self._down_until = 0.0

    def stream(self, text, profile_id=None, stop=None):
        import time
        if time.monotonic() >= self._down_until and \
                getattr(self.primary, "sample_rate", None) == self.sample_rate:
            started = False
            try:
                for chunk in self.primary.stream(text, profile_id, stop):
                    started = True
                    yield chunk
                if started:
                    return
            except LookupError as e:
                log.debug("voice: %s; using the fallback voice", e)
            except Exception as e:
                if started:
                    log.warning("voice: TTS failed mid-sentence: %s", e)
                    return
                log.info("voice: primary TTS unavailable (%s); using fallback", e)
                self._down_until = time.monotonic() + self.cooldown_s
            if stop is not None and stop.is_set():
                return
        yield from self.fallback.stream(text, profile_id, stop)

    def synth(self, text, profile_id=None):
        parts = list(self.stream(text, profile_id))
        return (np.concatenate(parts) if parts else np.zeros(0, "<i2")), self.sample_rate


def _kokoro(settings):
    return KokoroTTS(settings.kokoro_model_path, settings.kokoro_voices_path,
                     settings.kokoro_voice, settings.kokoro_speed, settings.tts_threads)


def build_engine(settings):
    """The engine DISPATCH_VOICE_TTS names.

    * ``auto`` (default): the cloned-voice engine (``chatterbox``, registered
      by app.voice.dv when dispatch-voice is installed) with Kokoro behind it,
      else Kokoro alone.
    * ``chatterbox`` / any registered name: that engine, Kokoro behind it when
      Kokoro is installed.
    * ``kokoro`` / ``minimax``: built in.

    Wrapped with the HQ fallback when DISPATCH_VOICE_HQ_URL is set.
    """
    from . import registry
    name = settings.tts_engine
    kokoro_ok = settings.kokoro_ready()
    if name == "auto":
        name = "chatterbox" if registry.engine_factory("chatterbox") else "kokoro"
    factory = registry.engine_factory(name)
    if factory is not None:
        eng = factory(settings)
        if kokoro_ok and name != "kokoro":
            eng = FallbackTTS(eng, _kokoro(settings), cooldown_s=30.0)
    elif name == "kokoro":
        eng = _kokoro(settings)
    elif name == "minimax":
        eng = MiniMaxTTS(os.environ.get("MINIMAX_API_KEY", ""), settings.minimax_model,
                         settings.minimax_voice, settings.minimax_base_url)
    else:
        raise RuntimeError(f"unknown TTS engine {name!r}")
    if settings.hq_url:
        eng = FallbackTTS(HQTTS(settings.hq_url, settings.hq_model,
                                settings.hq_timeout_s, eng.sample_rate), eng)
    return eng


_ENGINES: dict[str, object] = {}
_ENGINES_LOCK = threading.Lock()


def shared_engine(settings):
    from . import registry
    key = f"{settings.tts_engine}:{settings.hq_url}:{sorted(registry._engines)}"
    with _ENGINES_LOCK:
        if key not in _ENGINES:
            _ENGINES[key] = build_engine(settings)
        return _ENGINES[key]


async def speak(text_chunks: AsyncIterator[str] | Iterable[str],
                profile_id: str | None = None, *, engine=None,
                stop: threading.Event | None = None) -> AsyncIterator[bytes]:
    """THE TTS seam: text chunks in → PCM frames out.

    Splits the incoming text on sentences as it arrives, drops what should
    not be read aloud (:func:`speakable`), and synthesises each sentence off
    the event loop in ``profile_id``'s voice, yielding raw frames in the
    engine's declared format (``engine.sample_rate``, ``sample_format``,
    ``channels``). ``engine`` defaults to the configured shared engine. The
    live session uses the same pieces with its own cancellable queue.
    """
    if engine is None:
        from .config import load_settings
        engine = await asyncio.to_thread(shared_engine, load_settings())
    splitter = SentenceSplitter()
    loop = asyncio.get_running_loop()

    async def _say(sentence: str):
        words = speakable(sentence)
        if not words:
            return
        q: asyncio.Queue = asyncio.Queue()

        def produce():
            try:
                for c in engine.stream(words, profile_id, stop):
                    loop.call_soon_threadsafe(q.put_nowait, c)
            finally:
                loop.call_soon_threadsafe(q.put_nowait, None)

        fut = loop.run_in_executor(None, produce)
        while (c := await q.get()) is not None:
            yield np.asarray(c, "<i2").tobytes()
        await fut

    async def _chunks():
        if hasattr(text_chunks, "__aiter__"):
            async for c in text_chunks:
                yield c
        else:
            for c in text_chunks:
                yield c

    async for chunk in _chunks():
        for s in splitter.push(chunk):
            async for frame in _say(s):
                yield frame
    for s in splitter.flush():
        async for frame in _say(s):
            yield frame
