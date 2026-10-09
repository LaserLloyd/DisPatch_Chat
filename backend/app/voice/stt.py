"""Speech-to-text: NVIDIA Parakeet TDT 0.6B v2 (English) through onnx-asr, CPU.

Why this runtime: onnx-asr runs the NeMo export with nothing but numpy and
onnxruntime — no torch, no NeMo, no CUDA, which matters on a ROCm/CPU box and
keeps the uv env small. Parakeet is an offline (whole-utterance) model here:
the Endpointer hands it one finished turn and it returns in a small fraction
of the turn's length (see docs/voice-drive-mode.md for measured numbers), so a
streaming partial adds CPU cost for no audible gain in a car.

The interface still matches the plan's ``feed → partial/final``: ``feed`` only
buffers, ``final`` transcribes. A streaming engine can drop in behind it.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path

import numpy as np

log = logging.getLogger("dispatch.voice")


class ParakeetSTT:
    def __init__(self, model_dir: Path, threads: int = 4) -> None:
        import onnx_asr
        import onnxruntime as ort
        so = ort.SessionOptions()
        so.intra_op_num_threads = max(1, threads)
        so.log_severity_level = 3
        self._model = onnx_asr.load_model(
            "nemo-parakeet-tdt-0.6b-v2", str(model_dir), quantization="int8",
            sess_options=so, providers=["CPUExecutionProvider"])
        # onnxruntime sessions are thread-safe for run(), but one transcription
        # at a time keeps a second driver from halving the first one's speed.
        self._lock = threading.Lock()

    def transcribe(self, audio: np.ndarray) -> str:
        if audio.size < 1600:          # <0.1 s: nothing to hear
            return ""
        with self._lock:
            text = self._model.recognize(audio.astype(np.float32), sample_rate=16000)
        return (text or "").strip()


class Transcriber:
    """Per-session façade: ``feed`` buffers, ``final`` transcribes off-loop."""

    def __init__(self, engine) -> None:
        self.engine = engine
        self._chunks: list[np.ndarray] = []

    def feed(self, audio: np.ndarray) -> None:
        self._chunks.append(audio)

    async def final(self, audio: np.ndarray | None = None) -> str:
        if audio is None:
            audio = np.concatenate(self._chunks) if self._chunks else np.zeros(0, np.float32)
        self._chunks = []
        return await asyncio.to_thread(self.engine.transcribe, audio)


_ENGINES: dict[str, object] = {}
_ENGINES_LOCK = threading.Lock()


def shared_engine(settings):
    """Load once per process (the encoder is ~650 MB; loading takes seconds)."""
    key = f"{settings.stt_engine}:{settings.parakeet_dir}"
    with _ENGINES_LOCK:
        if key not in _ENGINES:
            if settings.stt_engine != "parakeet":
                raise RuntimeError(f"unknown STT engine {settings.stt_engine!r}")
            _ENGINES[key] = ParakeetSTT(settings.parakeet_dir, settings.threads)
        return _ENGINES[key]
