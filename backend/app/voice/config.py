"""Drive-mode settings, read from the environment.

Everything is optional. ``DISPATCH_VOICE`` decides whether the feature exists:

* ``auto`` (default) — on iff the Python deps import AND the model files the
  chosen engines need are on disk. An install that never ran the downloader
  simply has no Drive button; nothing errors.
* ``1`` / ``true`` / ``on`` — on; a missing piece is reported by
  ``/api/voice/status`` and the voice socket refuses with a reason.
* ``0`` / anything else — off.

Model files (VAD, Smart Turn, Parakeet, Kokoro) live in
``DISPATCH_SPEECH_MODELS`` (default ``<data>/speech-models``) with the layout
``app.voice.download`` produces.

Voice PROFILES (cloned voices, the Voices GUI) are NOT this package's job:
an extension registers them through ``app.voice.registry``. Here a profile is
only an opaque ``profile_id`` handed to the TTS engine.
"""
from __future__ import annotations

import importlib.util
import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.environ.get(f"DISPATCH_{name}", default)


def _float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


def _int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _data_dir() -> Path:
    try:
        from app.config import DATA_DIR
        return Path(DATA_DIR)
    except Exception:  # pragma: no cover - only outside the app package
        return Path.home() / ".local" / "share" / "dispatch"


def _default_models_dir() -> Path:
    return _data_dir() / "speech-models"


#: Patience presets the driver can pick (multiplies the merge window and the
#: hard-silence fallback). "patient" suits a driver who pauses mid-thought.
PATIENCE = {"quick": 0.7, "normal": 1.0, "patient": 1.5}


@dataclass(frozen=True)
class VoiceSettings:
    flag: str = "auto"
    models_dir: Path = field(default_factory=_default_models_dir)
    stt_engine: str = "parakeet"          # parakeet
    #: auto (cloned-voice engine if installed, else kokoro) | chatterbox |
    #: kokoro | minimax | any name an extension registered
    tts_engine: str = "auto"
    kokoro_voice: str = "af_heart"        # built-in voice when no profile applies
    kokoro_speed: float = 1.05
    minimax_model: str = "speech-2.8-turbo"
    minimax_voice: str = "Wise_Woman"
    minimax_base_url: str = "https://api.minimax.io"
    #: Optional "HQ" engine (e.g. Qwen3-TTS on the GPU rig) behind an
    #: OpenAI-compatible POST /v1/audio/speech. Falls back to the local engine on ANY
    #: failure, timeout or 409/503 — a drive never waits on the rig.
    hq_url: str = ""
    hq_model: str = ""
    hq_timeout_s: float = 4.0
    # Endpointing (seconds unless noted)
    vad_threshold: float = 0.5
    min_speech_s: float = 0.25            # shorter blips are noise, not words
    turn_probe_silence_s: float = 0.25    # silence before Smart Turn is asked
    turn_hard_silence_s: float = 1.4      # silence that ends a turn regardless
    no_smart_turn_silence_s: float = 0.8  # fallback when Smart Turn is absent
    max_utterance_s: float = 30.0
    smart_turn_threshold: float = 0.5
    #: After a turn ends we keep listening: speech resuming within this window
    #: means the turn ended too early (Smart Turn misfired on mid-sentence
    #: pauses in 4/10 road-noise simulation runs). The in-flight reply is
    #: cancelled and the merged utterance re-submitted.
    merge_window_s: float = 1.8
    patience: float = 1.0                 # multiplier; see PATIENCE presets
    barge_in_speech_s: float = 0.3        # sustained speech while speaking
    ack_after_s: float = 0.7              # "still working" cue if no reply yet
    barge_abort_turn: bool = True         # barge-in also aborts the agent turn
    voice_turn_thinking: str = "off"      # TurnOptions.thinking for voice turns ("" = agent default)
    threads: int = 6                      # onnxruntime intra-op threads (of 20)
    tts_threads: int = 8                  # onnxruntime threads for Kokoro

    # ---- derived paths -------------------------------------------------- #
    @property
    def silero_path(self) -> Path:
        return self.models_dir / "silero_vad.onnx"

    @property
    def smart_turn_path(self) -> Path:
        return self.models_dir / "smart-turn-v3.2-cpu.onnx"

    @property
    def parakeet_dir(self) -> Path:
        return self.models_dir / "parakeet-tdt-0.6b-v2"

    @property
    def kokoro_model_path(self) -> Path:
        return self.models_dir / "kokoro" / "kokoro-v1.0.int8.onnx"

    @property
    def kokoro_voices_path(self) -> Path:
        return self.models_dir / "kokoro" / "voices-v1.0.bin"

    # ---- availability --------------------------------------------------- #
    def missing(self) -> list[str]:
        """Human-readable list of what stops voice from working (empty = ready)."""
        out: list[str] = []
        for mod in ("numpy", "onnxruntime"):
            if importlib.util.find_spec(mod) is None:
                out.append(f"python package {mod}")
        if not self.silero_path.exists():
            out.append(f"model {self.silero_path.name}")
        if self.stt_engine == "parakeet":
            if importlib.util.find_spec("onnx_asr") is None:
                out.append("python package onnx-asr")
            if not (self.parakeet_dir / "encoder-model.int8.onnx").exists():
                out.append("model parakeet-tdt-0.6b-v2")
        if self.tts_engine in ("auto", "kokoro", "chatterbox"):
            have_clone = self.tts_engine != "kokoro" and _dispatch_voice_ready()
            if self.tts_engine == "chatterbox" and not have_clone:
                out.append("dispatch-voice package or its models (dispatch-voice fetch-models)")
            elif not have_clone and not self.kokoro_ready():
                if importlib.util.find_spec("kokoro_onnx") is None:
                    out.append("python package kokoro-onnx (or install dispatch-voice)")
                else:
                    out.append("model kokoro-v1.0")
        elif self.tts_engine == "minimax":
            if not os.environ.get("MINIMAX_API_KEY"):
                out.append("MINIMAX_API_KEY (DISPATCH_VOICE_TTS=minimax)")
        return out

    def kokoro_ready(self) -> bool:
        return (importlib.util.find_spec("kokoro_onnx") is not None
                and self.kokoro_model_path.exists() and self.kokoro_voices_path.exists())

    @property
    def enabled(self) -> bool:
        f = self.flag.strip().lower()
        if f in ("0", "false", "no", "off", ""):
            return False
        if f == "auto":
            return not self.missing()
        return True

    def merge_window(self, patience: float | None = None) -> float:
        return self.merge_window_s * (patience or self.patience)

    def status(self) -> dict:
        """Safe to show an unlocked client: no paths, no keys."""
        miss = self.missing()
        return {
            "enabled": self.enabled,
            "ready": not miss,
            "missing": miss,
            "stt": self.stt_engine,
            "tts": self.tts_engine,
            "hq": bool(self.hq_url),
            "smart_turn": self.smart_turn_path.exists(),
            "patience": self.patience,
            "patience_presets": PATIENCE,
        }


def _dispatch_voice_ready() -> bool:
    from .dv import available
    return available()


def load_settings() -> VoiceSettings:
    models = _env("SPEECH_MODELS")
    return VoiceSettings(
        flag=_env("VOICE", "auto"),
        models_dir=Path(models).expanduser() if models else _default_models_dir(),
        stt_engine=_env("VOICE_STT", "parakeet").strip().lower(),
        tts_engine=_env("VOICE_TTS", "auto").strip().lower(),
        kokoro_voice=_env("VOICE_KOKORO_VOICE", "af_heart"),
        kokoro_speed=_float("VOICE_KOKORO_SPEED", 1.05),
        minimax_model=_env("VOICE_MINIMAX_MODEL", "speech-2.8-turbo"),
        minimax_voice=_env("VOICE_MINIMAX_VOICE", "Wise_Woman"),
        minimax_base_url=_env("VOICE_MINIMAX_BASE_URL", "https://api.minimax.io"),
        hq_url=_env("VOICE_HQ_URL", ""),
        hq_model=_env("VOICE_HQ_MODEL", ""),
        hq_timeout_s=_float("VOICE_HQ_TIMEOUT", 4.0),
        turn_hard_silence_s=_float("VOICE_HARD_SILENCE", 1.4),
        no_smart_turn_silence_s=_float("VOICE_SILENCE", 0.8),
        merge_window_s=_float("VOICE_MERGE_WINDOW", 1.8),
        patience=_float("VOICE_PATIENCE", 1.0),
        barge_abort_turn=_env("VOICE_BARGE_ABORT", "1").strip().lower() in ("1", "true", "on", "yes"),
        voice_turn_thinking=_env("VOICE_THINKING", "off").strip(),
        threads=_int("VOICE_THREADS", 6),
        tts_threads=_int("VOICE_TTS_THREADS", 8),
    )
