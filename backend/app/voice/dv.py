"""Adapter for the ``dispatch-voice`` package: cloned voices + the Voices GUI.

``dispatch-voice`` is a separate MIT package (Chatterbox-Turbo ONNX on the
CPU, voice profiles, the Voices panel). This module is the ONLY place DisPatch
touches it. When the package or its models are missing, :func:`available` is
False and Drive mode falls back to the local Kokoro engine. Nothing else in
the app changes.

What it wires, from :func:`install` (called by routes.mount_http):

* The profile store at ``<data dir>/voices/``. This is personal data, never in
  the repo, a deploy or a zip. The engine is LAZY: ``ChatterboxOnnx`` (~3 s,
  ~700 MB of sessions) loads on first use, not at app import, so tests and
  installs that never open Drive mode pay nothing.
* The Voices router at ``/api/voices`` (panel at ``/api/voices/ui``). Every
  route sits behind the operator dependency DisPatch passes in, so Safe Mode
  and locked devices get 403.
* A TTS engine named ``chatterbox``. It streams 40 ms PCM16 frames at 24 kHz
  through ``dispatch_voice.speak.speak`` with the barge-in ``stop`` event.
* A profile resolver: the bot's ``voice:`` key (config.yaml) if that profile
  exists, else the store's default profile. With neither, the chatterbox
  engine raises LookupError and the fallback engine speaks instead.
* An acknowledgement provider: the profile's pre-rendered fillers ("Mm-hmm.",
  "Right." …), in the same voice.
"""
from __future__ import annotations

import importlib.util
import logging
import os
import sys
import threading
from pathlib import Path

import numpy as np

log = logging.getLogger("dispatch.voice")

SAMPLE_RATE = 24000


def _add_package_path() -> None:
    """Make an out-of-tree ``dispatch_voice`` importable.

    The cloned-voice engine is optional and lives in its own repository; it
    is not a dependency of this project (a sibling path in pyproject would
    break ``uv lock`` for everyone else). ``DISPATCH_VOICE_PKG`` names the
    directory that CONTAINS the ``dispatch_voice`` package (its ``src/``).
    """
    raw = os.environ.get("DISPATCH_VOICE_PKG", "").strip()
    if not raw:
        return
    path = str(Path(raw).expanduser())
    if Path(path, "dispatch_voice").is_dir() and path not in sys.path:
        sys.path.append(path)


_add_package_path()


def available() -> bool:
    """Package importable AND its models fetched (``dispatch-voice fetch-models``)."""
    if importlib.util.find_spec("dispatch_voice") is None:
        return False
    try:
        from dispatch_voice.engine import default_model_dir
        d = Path(default_model_dir())
        return (d / "tokenizer.json").exists() and (d / "onnx").is_dir()
    except Exception:
        return False


class LazyEngine:
    """Stands in for ``ChatterboxOnnx`` until something actually needs it."""

    def __init__(self) -> None:
        self._eng = None
        self._lock = threading.Lock()

    def _get(self):
        if self._eng is None:
            with self._lock:
                if self._eng is None:
                    from dispatch_voice.engine import ChatterboxOnnx
                    self._eng = ChatterboxOnnx()
                    log.info("voice: Chatterbox-Turbo ONNX engine loaded")
        return self._eng

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._get(), name)


_STORE = None
_STORE_LOCK = threading.Lock()


def voices_dir() -> Path:
    from app import config
    return Path(config.DATA_DIR) / "voices"


def store():
    global _STORE
    with _STORE_LOCK:
        root = voices_dir()
        if _STORE is None or Path(_STORE.root) != root:
            from dispatch_voice.profiles import ProfileStore
            _STORE = ProfileStore(root, LazyEngine())
        return _STORE


class LazyStore:
    """Forwards to :func:`store` on first use, so nothing touches the data dir
    until a request actually needs a profile."""

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return getattr(store(), name)


class ChatterboxTTS:
    """The ``chatterbox`` engine in app.voice.tts terms."""

    sample_rate = SAMPLE_RATE
    sample_format = "pcm_s16le"
    channels = 1
    streaming = True

    def stream(self, text: str, profile_id: str | None = None,
               stop: threading.Event | None = None):
        from dispatch_voice import speak
        st = store()
        # Resolve up front so "no voice chosen" surfaces as LookupError before
        # any audio, which is what lets FallbackTTS switch engines cleanly.
        speak.resolve(st, profile_id)
        for frame in speak.speak(st, [text], profile_id=profile_id, stop=stop):
            yield np.frombuffer(frame, dtype="<i2")

    def synth(self, text: str, profile_id: str | None = None):
        parts = list(self.stream(text, profile_id))
        return (np.concatenate(parts) if parts else np.zeros(0, "<i2")), SAMPLE_RATE


def resolve_profile(bot_id: str | None) -> str | None:
    """bot.voice if that profile exists, else the store default, else None."""
    from app import config
    st = store()
    bot = config.get_bot(bot_id) if bot_id else None
    want = (getattr(bot, "voice", "") or "").strip()
    if want:
        try:
            st.get(want)
            return want
        except Exception:
            log.warning("voice: bot %s names voice %r, which does not exist", bot_id, want)
    return st.default_id()


def fillers(profile_id: str | None) -> list[np.ndarray] | None:
    """Every pre-rendered filler of this profile (int16, 24 kHz), or None."""
    if not profile_id:
        return None
    import soundfile as sf
    from dispatch_voice.profiles import FILLERS
    st = store()
    out = []
    for i in range(len(FILLERS)):
        try:
            a, sr = sf.read(st.file(profile_id, f"fillers/{i}.wav"), dtype="float32")
        except Exception:
            continue
        if sr == SAMPLE_RATE:
            out.append((np.clip(a, -1, 1) * 32767).astype("<i2"))
    return out or None


def install(app, require_operator) -> bool:
    """Register engine, resolver, fillers and mount the Voices router.
    False (and nothing registered) when dispatch-voice is not available."""
    if not available():
        return False
    from dispatch_voice.router import build_router
    from fastapi import Depends

    from . import registry
    registry.register_engine("chatterbox", lambda settings: ChatterboxTTS())
    registry.set_profile_resolver(resolve_profile)
    registry.set_ack_provider(fillers)
    # A lazy store: building the router must not create <data>/voices at app
    # import (tests point DATA_DIR elsewhere after import; a box that never
    # opens the panel never gets the directory).
    app.include_router(build_router(LazyStore(), [Depends(require_operator)]), prefix="/api/voices")
    log.info("voice: dispatch-voice mounted at /api/voices (unlocked tier only)")
    return True
