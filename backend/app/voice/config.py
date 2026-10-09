"""Drive-mode settings, read from the environment and ``<data>/voice.yaml``.

Voice is an OPTIONAL feature with an on/off switch (Settings › Voice). Who
decides whether it is on:

* ``DISPATCH_VOICE`` unset or ``auto`` (default) — the switch decides. It is
  ``enabled`` in ``<data>/voice.yaml`` and is OFF until someone turns it on,
  even when every package and model is present: a fresh install never loads
  ~1.5 GB of speech models nobody asked for. When it is on, voice works once
  the Python deps import AND the model files the chosen engines need are on
  disk; until then ``/api/voice/status`` lists what is missing.
* ``1`` / ``true`` / ``on`` / ``yes`` — forced on (the switch is shown on and
  locked); a missing piece is reported by ``/api/voice/status`` and the voice
  socket refuses with a reason.
* ``0`` / anything else — forced off (the switch is hidden, with a note).

The switch applies live: turning it off ends open Drive sessions with 1013
and drops the loaded speech models; turning it on starts the background
prewarm (routes.apply_switch).

Model files (VAD, Smart Turn, Parakeet) live in
``DISPATCH_SPEECH_MODELS`` (default ``<data>/speech-models``) with the layout
``app.voice.download`` produces.

A few choices are PREFERENCES, not install decisions, and are changed from the
app (``/api/voice/settings``, unlocked only): the on/off switch, the built-in
voice (when a plugin provides built-in voices), the speaking speed, and the
Voice lab's thinking level and two timing values (the turn-merge window and
the hard-silence end of turn). They live in ``<data>/voice.yaml``
and override the matching environment defaults; :func:`load_settings` reads
the file (cached on its mtime), so a change applies to the next turn without a
restart.

Voice PROFILES (cloned voices, the Voices GUI) are NOT this package's job:
an extension registers them through ``app.voice.registry``. Here a profile is
only an opaque ``profile_id`` handed to the TTS engine.

Setting names are engine-neutral. The names from DisPatch 2.1, when the
built-in voice was always Kokoro, still work as aliases:
``DISPATCH_VOICE_KOKORO_VOICE`` / ``DISPATCH_VOICE_KOKORO_SPEED`` (env) and
``kokoro_voice`` / ``kokoro_speed`` (voice.yaml). The new name wins when both
are set; saving from the app rewrites voice.yaml with the new names.
"""
from __future__ import annotations

import importlib.util
import logging
import os
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path

log = logging.getLogger("dispatch.voice")


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

#: How long a spoken reply should be (``start.reply_style``; persona.py).
REPLY_STYLES = ("short", "normal", "longer")
#: Allowed thinking levels for voice turns (``voice_turn_thinking``).
THINKING_LEVELS = ("off", "low", "medium")
#: Speaking-speed bounds (engines that support a speed, e.g. a built-in voice
#: plugin; a cloned voice speaks at its own pace).
SPEED_MIN, SPEED_MAX = 0.8, 1.3
#: Voice lab timing bounds (seconds). Outside these the endpointer either cuts
#: people off mid-sentence or makes every reply wait noticeably; the env
#: variables are not limited, the app is.
MERGE_WINDOW_MIN, MERGE_WINDOW_MAX = 0.5, 4.0
HARD_SILENCE_MIN, HARD_SILENCE_MAX = 0.8, 3.0
#: Values of DISPATCH_VOICE that force voice on; ``auto`` (or unset) leaves it
#: to the switch; anything else forces it off.
FORCE_ON = ("1", "true", "on", "yes")


@dataclass(frozen=True)
class VoiceSettings:
    flag: str = "auto"
    #: The Settings › Voice switch (voice.yaml ``enabled``). Only consulted
    #: when ``flag`` is ``auto``. Off by default.
    switch: bool = False
    models_dir: Path = field(default_factory=_default_models_dir)
    stt_engine: str = "parakeet"          # parakeet
    #: auto (the cloned-voice engine ``chatterbox`` if installed, else the
    #: first ready registered engine) | chatterbox | minimax | any name an
    #: extension registered (e.g. ``kokoro`` from dispatch-voice-kokoro)
    tts_engine: str = "auto"
    #: Built-in voice (a provider's id, without ``builtin:``) used when no
    #: profile applies. "" = the provider's own default.
    builtin_voice: str = ""
    speed: float = 1.05
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
    #: Sustained speech needed to count as the driver talking OVER a reply.
    #: Residual echo of our own voice is short and bursty; a real interruption
    #: is not. Even then it only DUCKS the reply until the words are checked.
    barge_in_speech_s: float = 0.35
    ack_after_s: float = 0.7              # "still working" cue if no reply yet
    barge_abort_turn: bool = True         # barge-in also aborts the agent turn
    voice_turn_thinking: str = "off"      # TurnOptions.thinking for voice turns ("" = agent default)
    threads: int = 6                      # onnxruntime intra-op threads (of 20)
    tts_threads: int = 8                  # onnxruntime threads for a local TTS engine
    #: Load the speech models in the background at app startup (H5), so the
    #: first Drive session after a restart does not pay for it.
    prewarm: bool = True

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

    # ---- 2.1 names (read-only aliases) ---------------------------------- #
    @property
    def kokoro_voice(self) -> str:
        return self.builtin_voice

    @property
    def kokoro_speed(self) -> float:
        return self.speed

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
        out += self._tts_missing()
        return out

    def _tts_missing(self) -> list[str]:
        """What the chosen TTS engine still needs (empty = it can speak)."""
        from . import registry
        name = self.tts_engine
        if name == "minimax":
            return [] if os.environ.get("MINIMAX_API_KEY") else ["MINIMAX_API_KEY (DISPATCH_VOICE_TTS=minimax)"]
        if name == "chatterbox" or (name == "auto" and _dispatch_voice_installed()):
            if _dispatch_voice_ready():
                return []
            if name == "chatterbox" or not any(s.ready(self) for s in registry._engines.values()):
                return ["dispatch-voice models (run `dispatch-voice fetch-models`)"]
            return []
        if name == "auto":
            if any(s.ready(self) for s in registry._engines.values()):
                return []
            return [NO_TTS]
        if name == "chatterbox":
            return ["dispatch-voice package (install it into DisPatch's environment; "
                    "DISPATCH_VOICE_TTS=chatterbox)"]
        spec = registry.engine_spec(name)
        if spec is None:
            hint = " (it comes with the optional dispatch-voice-kokoro plugin)" if name == "kokoro" else ""
            return [f"TTS engine {name!r} is not installed{hint}"]
        return spec.missing_for(self)

    @property
    def forced(self) -> str | None:
        """"on" / "off" when DISPATCH_VOICE decides, None when the switch does."""
        f = (self.flag or "").strip().lower()
        if f == "auto":
            return None
        return "on" if f in FORCE_ON else "off"

    @property
    def switched_on(self) -> bool:
        """Has the owner asked for voice (env, else the switch)? Says nothing
        about whether the pieces are installed."""
        forced = self.forced
        return self.switch if forced is None else forced == "on"

    @property
    def enabled(self) -> bool:
        """Drive mode is offered: forced on, or switched on and ready."""
        forced = self.forced
        if forced is not None:
            return forced == "on"
        return self.switch and not self.missing()

    def merge_window(self, patience: float | None = None) -> float:
        return self.merge_window_s * (patience or self.patience)

    def status(self) -> dict:
        """Safe to show an unlocked client: no paths, no keys."""
        miss = self.missing()
        forced = self.forced
        return {
            "enabled": forced == "on" if forced is not None else (self.switch and not miss),
            # The switch as stored (what Settings › Voice draws) and whether
            # DISPATCH_VOICE overrides it ("on" / "off" / None).
            "switch": self.switched_on,
            "forced": forced,
            "ready": not miss,
            "missing": miss,
            "stt": self.stt_engine,
            "tts": self.tts_engine,
            "hq": bool(self.hq_url),
            "smart_turn": self.smart_turn_path.exists(),
            "patience": self.patience,
            "patience_presets": PATIENCE,
            "speed": self.speed,
            "voice_turn_thinking": self.voice_turn_thinking or "off",
            "merge_window_s": self.merge_window_s,
            "hard_silence_s": self.turn_hard_silence_s,
        }


#: What /api/voice/status says when nothing can speak. Deliberately not
#: "python package …": the Settings tab treats that prefix as "voice is off".
NO_TTS = ("a text-to-speech engine: install dispatch-voice (MIT, "
          "github.com/LaserLloyd/DisPatch_Voice) and run `dispatch-voice "
          "fetch-models`, or set DISPATCH_VOICE_TTS=minimax")


def _dispatch_voice_installed() -> bool:
    from . import dv  # noqa: F401  (a DISPATCH_VOICE_PKG checkout goes on sys.path)
    return importlib.util.find_spec("dispatch_voice") is not None


def _dispatch_voice_ready() -> bool:
    from .dv import available
    return available()


def _bool(name: str, default: bool) -> bool:
    raw = _env(name, "1" if default else "0").strip().lower()
    return raw in ("1", "true", "on", "yes")


# --------------------------------------------------------------------------- #
# <data>/voice.yaml — the in-app preferences (see the module docstring)
# --------------------------------------------------------------------------- #

PREF_KEYS = ("enabled", "builtin_voice", "speed", "voice_turn_thinking",
             "merge_window_s", "hard_silence_s")
#: voice.yaml keys from 2.1 → today's names.
PREF_ALIASES = {"kokoro_voice": "builtin_voice", "kokoro_speed": "speed"}
#: voice.yaml key → VoiceSettings field, where the two differ.
PREF_FIELDS = {"enabled": "switch", "hard_silence_s": "turn_hard_silence_s"}


def _num_in(v, lo: float, hi: float) -> float | None:
    if isinstance(v, (int, float)) and not isinstance(v, bool) and lo <= float(v) <= hi:
        return round(float(v), 3)
    return None
_PREFS_CACHE: tuple[float, dict] | None = None
_PREFS_LOCK = threading.Lock()
#: Serialises the read-modify-write of voice.yaml: two PATCHes at once used
#: to drop each other's key. (A save keeps only known keys; comments and
#: unknown keys in a hand-edited file are not preserved.)
_PREFS_WRITE_LOCK = threading.Lock()


def prefs_path() -> Path:
    return _data_dir() / "voice.yaml"


def clean_prefs(raw) -> dict:
    """Only known keys with valid values survive; anything else is dropped."""
    out: dict = {}
    if not isinstance(raw, dict):
        return out
    raw = dict(raw)
    for old, new in PREF_ALIASES.items():
        if old in raw and new not in raw:
            raw[new] = raw[old]
    v = raw.get("builtin_voice")
    if isinstance(v, str) and v.strip() and len(v) <= 64 and v.replace("_", "").replace("-", "").isalnum():
        out["builtin_voice"] = v.strip()
    sp = raw.get("speed")
    if isinstance(sp, (int, float)) and not isinstance(sp, bool) and SPEED_MIN <= float(sp) <= SPEED_MAX:
        out["speed"] = round(float(sp), 3)
    th = raw.get("voice_turn_thinking")
    if isinstance(th, str) and th.strip().lower() in THINKING_LEVELS:
        out["voice_turn_thinking"] = th.strip().lower()
    if isinstance(raw.get("enabled"), bool):
        out["enabled"] = raw["enabled"]
    mw = _num_in(raw.get("merge_window_s"), MERGE_WINDOW_MIN, MERGE_WINDOW_MAX)
    if mw is not None:
        out["merge_window_s"] = mw
    hs = _num_in(raw.get("hard_silence_s"), HARD_SILENCE_MIN, HARD_SILENCE_MAX)
    if hs is not None:
        out["hard_silence_s"] = hs
    return out


def forget_prefs(keys) -> dict:
    """Remove ``keys`` from voice.yaml (back to the env/default value)."""
    with _PREFS_WRITE_LOCK:
        return _write_prefs({k: v for k, v in load_prefs().items() if k not in set(keys)})


def load_prefs() -> dict:
    """voice.yaml, validated; {} when absent or unreadable. Cached on mtime."""
    global _PREFS_CACHE
    p = prefs_path()
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return {}
    with _PREFS_LOCK:
        if _PREFS_CACHE is not None and _PREFS_CACHE[0] == mtime:
            return dict(_PREFS_CACHE[1])
    try:
        import yaml
        prefs = clean_prefs(yaml.safe_load(p.read_text()) or {})
    except Exception:
        log.warning("voice: %s is unreadable; using the defaults", p)
        prefs = {}
    with _PREFS_LOCK:
        _PREFS_CACHE = (mtime, prefs)
    return dict(prefs)


def save_prefs(update: dict) -> dict:
    """Merge ``update`` (already validated) into voice.yaml atomically."""
    with _PREFS_WRITE_LOCK:
        return _write_prefs({**load_prefs(), **clean_prefs(update)})


def _write_prefs(prefs: dict) -> dict:
    global _PREFS_CACHE
    import yaml
    p = prefs_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".yaml.tmp")
    tmp.write_text(yaml.safe_dump(prefs, sort_keys=True))
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)
    with _PREFS_LOCK:
        _PREFS_CACHE = None
    return prefs


def load_settings() -> VoiceSettings:
    models = _env("SPEECH_MODELS")
    base = VoiceSettings(
        flag=_env("VOICE", "auto"),
        models_dir=Path(models).expanduser() if models else _default_models_dir(),
        stt_engine=_env("VOICE_STT", "parakeet").strip().lower(),
        tts_engine=_env("VOICE_TTS", "auto").strip().lower(),
        builtin_voice=_env("VOICE_BUILTIN_VOICE", _env("VOICE_KOKORO_VOICE", "")).strip(),
        speed=_float("VOICE_SPEED", _float("VOICE_KOKORO_SPEED", 1.05)),
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
        prewarm=_bool("VOICE_PREWARM", True),
    )
    prefs = {PREF_FIELDS.get(k, k): v for k, v in load_prefs().items()}
    return replace(base, **prefs) if prefs else base
