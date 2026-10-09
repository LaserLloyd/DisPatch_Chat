"""Voice activity + end-of-turn detection (Silero VAD + Smart Turn v3.2, CPU).

Three layers, so the interesting logic is testable without a model:

* :class:`Endpointer` — a pure state machine over (frame, speech-probability)
  pairs. It decides when speech started, when a turn ended and when a blip was
  only noise. No numpy-free promise, but no onnxruntime either.
* :class:`SileroVAD` / :class:`SmartTurn` — thin onnxruntime wrappers that
  turn audio into the probabilities the state machine consumes.
* :class:`TurnDetector` — glue: raw PCM16 bytes in, events out.

Endpointing, in words. A frame is 512 samples (32 ms at 16 kHz). Speech has to
last ``min_speech_s`` before it counts (a door slam or a turn signal click is
shorter). Once someone is talking, a short pause (``turn_probe_silence_s``)
asks Smart Turn "does that sound finished?" — it hears intonation, so "yes."
ends quickly and "I was thinking that…" does not. If Smart Turn says
unfinished, the turn still ends after ``turn_hard_silence_s`` of silence. With
no Smart Turn model, a plain ``no_smart_turn_silence_s`` silence ends it.
"""
from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

log = logging.getLogger("dispatch.voice")

SAMPLE_RATE = 16000
FRAME = 512                       # Silero's native window at 16 kHz
FRAME_S = FRAME / SAMPLE_RATE     # 0.032 s


# --------------------------------------------------------------------------- #
# Events
# --------------------------------------------------------------------------- #

@dataclass
class SpeechStart:
    """Real speech began (after the min-speech debounce). Used for barge-in."""


@dataclass
class TurnEnd:
    audio: np.ndarray             # float32 mono 16 kHz, the whole utterance
    reason: str                   # smart_turn | silence | max_length
    turn_prob: float | None = None


@dataclass
class Discarded:
    """Speech-like noise that never reached ``min_speech_s``."""
    seconds: float


# --------------------------------------------------------------------------- #
# The state machine
# --------------------------------------------------------------------------- #

@dataclass
class EndpointerConfig:
    threshold: float = 0.5
    min_speech_s: float = 0.25
    turn_probe_silence_s: float = 0.25
    turn_hard_silence_s: float = 1.4
    no_smart_turn_silence_s: float = 0.8
    max_utterance_s: float = 30.0
    smart_turn_threshold: float = 0.5
    pre_roll_s: float = 0.3       # audio kept from before speech was detected


@dataclass
class Endpointer:
    cfg: EndpointerConfig = field(default_factory=EndpointerConfig)
    #: audio(float32) -> P(turn complete). None = no Smart Turn model.
    turn_fn: Callable[[np.ndarray], float] | None = None

    def __post_init__(self) -> None:
        self._pre: deque[np.ndarray] = deque(
            maxlen=max(1, round(self.cfg.pre_roll_s / FRAME_S)))
        self.reset()

    def reset(self) -> None:
        self._frames: list[np.ndarray] = []
        self._in_utterance = False   # some speech seen, collecting
        self._confirmed = False      # passed min_speech (SpeechStart sent)
        self._speech_frames = 0
        self._silence_frames = 0
        self._probed = False         # Smart Turn already asked this pause
        self._pre.clear()

    @property
    def active(self) -> bool:
        return self._in_utterance

    def process(self, frame: np.ndarray, prob: float) -> list:
        """Feed one FRAME of float32 audio with its speech probability."""
        c = self.cfg
        events: list = []
        # Hysteresis: once talking, a frame has to drop clearly below the
        # threshold to count as silence, so breathy word endings don't split.
        is_speech = prob >= (c.threshold - 0.15 if self._in_utterance else c.threshold)

        if not self._in_utterance:
            if is_speech:
                self._in_utterance = True
                self._frames = [*self._pre, frame]
                self._speech_frames = 1
                self._silence_frames = 0
                self._pre.clear()
            else:
                self._pre.append(frame)
            return events

        self._frames.append(frame)
        if is_speech:
            self._speech_frames += 1
            self._silence_frames = 0
            self._probed = False
            if not self._confirmed and self._speech_frames * FRAME_S >= c.min_speech_s:
                self._confirmed = True
                events.append(SpeechStart())
        else:
            self._silence_frames += 1

        silence_s = self._silence_frames * FRAME_S
        length_s = len(self._frames) * FRAME_S

        if not self._confirmed:
            # A blip that went quiet before it ever counted as speech.
            if silence_s >= c.turn_probe_silence_s:
                events.append(Discarded(self._speech_frames * FRAME_S))
                self.reset()
            return events

        if length_s >= c.max_utterance_s:
            events.append(self._finish("max_length"))
            return events

        if self.turn_fn is None:
            if silence_s >= c.no_smart_turn_silence_s:
                events.append(self._finish("silence"))
            return events

        if silence_s >= c.turn_hard_silence_s:
            events.append(self._finish("silence"))
        elif silence_s >= c.turn_probe_silence_s and not self._probed:
            self._probed = True
            try:
                p = float(self.turn_fn(self._audio()))
            except Exception:
                log.exception("smart turn failed; falling back to silence")
                p = None
            if p is not None and p >= c.smart_turn_threshold:
                events.append(self._finish("smart_turn", p))
        return events

    def _audio(self) -> np.ndarray:
        return np.concatenate(self._frames) if self._frames else np.zeros(0, np.float32)

    def _finish(self, reason: str, prob: float | None = None) -> TurnEnd:
        # Trim the trailing silence (keep ~0.2 s) — STT does not need it.
        keep_tail = max(0, self._silence_frames - round(0.2 / FRAME_S))
        frames = self._frames[: len(self._frames) - keep_tail] if keep_tail else self._frames
        audio = np.concatenate(frames) if frames else np.zeros(0, np.float32)
        self.reset()
        return TurnEnd(audio=audio, reason=reason, turn_prob=prob)


# --------------------------------------------------------------------------- #
# Model wrappers
# --------------------------------------------------------------------------- #

def _session(path: Path, threads: int):
    import onnxruntime as ort
    so = ort.SessionOptions()
    so.intra_op_num_threads = max(1, threads)
    so.inter_op_num_threads = 1
    so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    so.log_severity_level = 3
    return ort.InferenceSession(str(path), sess_options=so,
                                providers=["CPUExecutionProvider"])


class SileroVAD:
    """Silero VAD v5/v6 ONNX, one 512-sample frame at a time, stateful."""

    CONTEXT = 64  # samples of the previous frame the v5 graph expects

    def __init__(self, path: Path, threads: int = 1) -> None:
        self._sess = _session(path, threads)
        self.reset()

    def reset(self) -> None:
        self._state = np.zeros((2, 1, 128), np.float32)
        self._context = np.zeros(self.CONTEXT, np.float32)

    def __call__(self, frame: np.ndarray) -> float:
        x = np.concatenate([self._context, frame.astype(np.float32)])[None, :]
        out, self._state = self._sess.run(
            None, {"input": x, "state": self._state, "sr": np.array(SAMPLE_RATE, np.int64)})
        self._context = frame[-self.CONTEXT:].astype(np.float32)
        return float(out[0][0])


class EnergyVAD:
    """Crude RMS gate. A TEST DOUBLE and a no-model fallback — not for the car."""

    def __init__(self, threshold_db: float = -35.0) -> None:
        self.threshold_db = threshold_db

    def reset(self) -> None:
        pass

    def __call__(self, frame: np.ndarray) -> float:
        rms = float(np.sqrt(np.mean(np.square(frame), dtype=np.float64)) + 1e-12)
        return 1.0 if 20 * np.log10(rms) > self.threshold_db else 0.0


# ---- Whisper log-mel, numpy only (what Smart Turn's encoder was trained on) #

def _hz_to_mel(f: np.ndarray) -> np.ndarray:
    f = np.asarray(f, np.float64)
    mel = f / (200.0 / 3)
    log_region = f >= 1000.0
    mel = np.where(log_region, 15.0 + np.log(np.maximum(f, 1e-10) / 1000.0) / (np.log(6.4) / 27.0), mel)
    return mel


def _mel_to_hz(m: np.ndarray) -> np.ndarray:
    m = np.asarray(m, np.float64)
    f = m * (200.0 / 3)
    log_region = m >= 15.0
    return np.where(log_region, 1000.0 * np.exp((np.log(6.4) / 27.0) * (m - 15.0)), f)


def mel_filters(n_mels: int = 80, n_fft: int = 400, sr: int = SAMPLE_RATE) -> np.ndarray:
    """Slaney-scale, slaney-normalised filterbank, shape (n_freqs, n_mels)."""
    n_freqs = n_fft // 2 + 1
    fft_freqs = np.linspace(0, sr / 2, n_freqs)
    mel_pts = np.linspace(_hz_to_mel(0.0), _hz_to_mel(sr / 2), n_mels + 2)
    hz_pts = _mel_to_hz(mel_pts)
    diff = np.diff(hz_pts)
    slopes = hz_pts[None, :] - fft_freqs[:, None]           # (n_freqs, n_mels+2)
    down = -slopes[:, :-2] / diff[:-1]
    up = slopes[:, 2:] / diff[1:]
    fb = np.maximum(0, np.minimum(down, up))
    enorm = 2.0 / (hz_pts[2: n_mels + 2] - hz_pts[:n_mels])
    return fb * enorm[None, :]


_MEL = None


def whisper_features(audio: np.ndarray, seconds: int = 8) -> np.ndarray:
    """(1, 80, seconds*100) float32 — WhisperFeatureExtractor(chunk_length=8)
    as Smart Turn calls it: keep the last 8 s, LEFT-pad with zeros, normalise
    to zero mean / unit variance, log-mel."""
    global _MEL
    if _MEL is None:
        _MEL = mel_filters()
    n = seconds * SAMPLE_RATE
    x = np.asarray(audio, np.float64)[-n:]
    if len(x) < n:
        x = np.pad(x, (n - len(x), 0))
    x = (x - x.mean()) / np.sqrt(x.var() + 1e-7)
    n_fft, hop = 400, 160
    xp = np.pad(x, (n_fft // 2, n_fft // 2), mode="reflect")
    n_frames = 1 + (len(xp) - n_fft) // hop
    idx = np.arange(n_fft)[None, :] + hop * np.arange(n_frames)[:, None]
    window = np.hanning(n_fft + 1)[:-1]
    spec = np.fft.rfft(xp[idx] * window, axis=1)
    power = (np.abs(spec) ** 2)[:-1]                        # whisper drops the last frame
    mel = power @ _MEL                                        # (frames, 80)
    log_spec = np.log10(np.maximum(mel, 1e-10)).T
    log_spec = np.maximum(log_spec, log_spec.max() - 8.0)
    return ((log_spec + 4.0) / 4.0).astype(np.float32)[None, :, :]


class SmartTurn:
    """Smart Turn v3.x ONNX: P(the speaker has finished)."""

    def __init__(self, path: Path, threads: int = 1) -> None:
        self._sess = _session(path, threads)

    def __call__(self, audio: np.ndarray) -> float:
        out = self._sess.run(None, {"input_features": whisper_features(audio)})
        return float(np.asarray(out[0]).reshape(-1)[0])


# --------------------------------------------------------------------------- #
# Glue
# --------------------------------------------------------------------------- #

class TurnDetector:
    """PCM16 little-endian mono 16 kHz bytes in → events out. Not thread-safe;
    one per voice session."""

    def __init__(self, vad: Callable[[np.ndarray], float],
                 endpointer: Endpointer) -> None:
        self.vad = vad
        self.ep = endpointer
        self._buf = np.zeros(0, np.float32)
        self.last_prob = 0.0

    def reset(self) -> None:
        self._buf = np.zeros(0, np.float32)
        self.ep.reset()
        if hasattr(self.vad, "reset"):
            self.vad.reset()

    def feed(self, pcm16: bytes) -> list:
        if len(pcm16) % 2:
            pcm16 = pcm16[:-1]
        samples = np.frombuffer(pcm16, dtype="<i2").astype(np.float32) / 32768.0
        self._buf = np.concatenate([self._buf, samples])
        events: list = []
        while len(self._buf) >= FRAME:
            frame, self._buf = self._buf[:FRAME], self._buf[FRAME:]
            self.last_prob = self.vad(frame)
            events.extend(self.ep.process(frame, self.last_prob))
        return events


def build_detector(settings) -> TurnDetector:
    """Production detector from VoiceSettings (Silero + Smart Turn if present)."""
    cfg = EndpointerConfig(
        threshold=settings.vad_threshold,
        min_speech_s=settings.min_speech_s,
        turn_probe_silence_s=settings.turn_probe_silence_s,
        turn_hard_silence_s=settings.turn_hard_silence_s,
        no_smart_turn_silence_s=settings.no_smart_turn_silence_s,
        max_utterance_s=settings.max_utterance_s,
        smart_turn_threshold=settings.smart_turn_threshold,
    )
    turn_fn = _shared_smart_turn(settings)
    return TurnDetector(SileroVAD(settings.silero_path), Endpointer(cfg, turn_fn))


_SMART_TURN: dict[str, SmartTurn] = {}


def _shared_smart_turn(settings) -> SmartTurn | None:
    p = settings.smart_turn_path
    if not p.exists():
        return None
    key = str(p)
    if key not in _SMART_TURN:
        _SMART_TURN[key] = SmartTurn(p, threads=2)
    return _SMART_TURN[key]
