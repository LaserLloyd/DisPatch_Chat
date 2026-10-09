"""Adapter for the ``dispatch-voice`` package: cloned voices + the Voices GUI.

``dispatch-voice`` is a separate MIT package (Chatterbox-Turbo ONNX on the
CPU, voice profiles, a stock synthetic voice, the Voices panel). This module
is the ONLY place DisPatch touches it. When the package or its models are
missing, :func:`available` is False and Drive mode uses whatever other engine
is installed (a plugin, or MiniMax when configured); /api/voice/status says
what is missing. Nothing else in the app changes.

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
  exists, else the store's default profile. With neither: when a fallback
  engine (a built-in voice plugin) sits behind chatterbox, chatterbox raises
  LookupError and the fallback speaks, as in 2.1; when chatterbox is the last
  resort, the STOCK profile speaks (``dispatch_voice.stock``: a voice built
  from fully synthetic reference audio, not a real person). A saved
  ``builtin:`` voice with no provider installed degrades the same way.
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


#: Where dispatch-voice is published (MIT). Install it into DisPatch's
#: environment, e.g. ``uv run --group voice --with "dispatch-voice @
#: git+<this>" …``; DisPatch then finds it like any installed package.
DV_REPO = "https://github.com/LaserLloyd/DisPatch_Voice"


def _add_package_path() -> None:
    """Optionally load ``dispatch_voice`` from a LOCAL CHECKOUT.

    The cloned-voice engine is optional and lives in its own repository; it
    is not a dependency of this project (pinning it in pyproject would make
    every install fetch it). Installed as a package (``--with`` / pip), it is
    found like any other. ``DISPATCH_VOICE_PKG`` is the override for a local
    checkout: the directory that CONTAINS the ``dispatch_voice`` package (its
    ``src/``). It goes FIRST on sys.path, so it wins over an installed copy.
    """
    raw = os.environ.get("DISPATCH_VOICE_PKG", "").strip()
    if not raw:
        return
    path = str(Path(raw).expanduser())
    if Path(path, "dispatch_voice").is_dir() and path not in sys.path:
        sys.path.insert(0, path)


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

    def warm(self) -> None:
        """Load now (start-up prewarm) instead of on the first reply."""
        self._get()

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


def _stock_mod():
    """``dispatch_voice.stock``, or None with a dispatch-voice that predates it."""
    try:
        from dispatch_voice import stock
        return stock
    except ImportError:
        return None


def stock_id() -> str | None:
    s = _stock_mod()
    return s.STOCK_ID if s is not None else None


def stock_profile(build: bool = False) -> str | None:
    """The stock profile's id once it exists in the store, else None.

    ``build=False`` only copies the tensors ``dispatch-voice fetch-models``
    pre-built (cheap; never loads the engine). ``build=True`` may load the
    engine and build them (~1 s plus the engine load)."""
    s = _stock_mod()
    if s is None:
        return None
    try:
        pid = s.ensure_stock(store(), build=build)
    except Exception:
        log.exception("voice: the stock voice could not be prepared")
        return None
    if pid and build:
        _render_stock_extras(pid)
    return pid


_EXTRAS_STARTED: set[str] = set()


def _render_stock_extras(pid: str) -> None:
    """Audition + fillers for the stock voice, once, in the background."""
    s = _stock_mod()
    key = str(voices_dir())
    if s is None or key in _EXTRAS_STARTED:
        return
    _EXTRAS_STARTED.add(key)

    def run():
        from .config import load_settings
        from .tts import REPLY_ACTIVITY
        REPLY_ACTIVITY.wait_idle()
        if not load_settings().switched_on:
            # Voice was switched off while this waited: rendering now would
            # load the engine again right after release() gave it back.
            _EXTRAS_STARTED.discard(key)
            return
        try:
            s.render_extras(store(), pid)
        except Exception:
            log.exception("voice: rendering the stock voice's fillers failed")

    threading.Thread(target=run, name="voice-stock-extras", daemon=True).start()


class ChatterboxTTS:
    """The ``chatterbox`` engine in app.voice.tts terms.

    ``has_fallback`` is set by tts.build_engine when another engine (a
    built-in voice plugin) sits behind this one. Then a voice that is not a
    profile here (a ``builtin:`` id, an unknown id, no voice chosen) raises
    LookupError so the fallback speaks it. Without a fallback this engine is
    the last resort and speaks such a sentence in the default profile, else
    the stock voice, instead of failing."""

    sample_rate = SAMPLE_RATE
    sample_format = "pcm_s16le"
    channels = 1
    streaming = True

    def __init__(self) -> None:
        self.has_fallback = False

    def _resolve(self, profile_id: str | None) -> str:
        from dispatch_voice import speak

        from .tts import builtin_id
        st = store()
        sid = stock_id()
        if builtin_id(profile_id):
            if self.has_fallback:
                # A built-in voice was chosen: not ours. LookupError = "use the
                # fallback for this sentence", with no cooldown (review L6).
                raise LookupError(f"{profile_id} is a built-in voice")
            profile_id = None          # no provider installed: degrade
        if profile_id and profile_id == sid:
            if stock_profile(build=True) == sid:
                return sid
            raise LookupError("the stock voice is not available")
        # Resolve up front so "no voice chosen" surfaces as LookupError before
        # any audio, which is what lets FallbackTTS switch engines cleanly. An
        # unknown id is the same case, not an engine failure: a bad id must
        # not put the cloned engine on a 30 s cooldown for everyone.
        try:
            return speak.resolve(st, profile_id)
        except Exception as e:
            if self.has_fallback:
                if isinstance(e, LookupError):
                    raise
                raise LookupError(f"voice {profile_id!r} is not available: {e}") from e
        if profile_id:
            log.warning("voice: voice %r does not exist; using the default voice", profile_id)
        pid = st.default_id() or stock_profile(build=True)
        if not pid:
            raise LookupError("no voice profile and no stock voice available")
        return pid

    def stream(self, text: str, profile_id: str | None = None,
               stop: threading.Event | None = None):
        from dispatch_voice import speak
        pid = self._resolve(profile_id)
        for frame in speak.speak(store(), [text], profile_id=pid, stop=stop):
            yield np.frombuffer(frame, dtype="<i2")

    def synth(self, text: str, profile_id: str | None = None):
        parts = list(self.stream(text, profile_id))
        return (np.concatenate(parts) if parts else np.zeros(0, "<i2")), SAMPLE_RATE


def resolve_profile(bot_id: str | None) -> str | None:
    """bot.voice if that profile exists (or names a built-in voice while a
    provider is installed), else the store default, else, with no built-in
    voice plugin, the stock voice once it exists, else None."""
    from app import config

    from . import registry
    from .tts import builtin_id
    st = store()
    bot = config.get_bot(bot_id) if bot_id else None
    want = (getattr(bot, "voice", "") or "").strip()
    if builtin_id(want):
        if registry.has_builtin_voices():
            return want
        want = ""                      # provider gone: the default voice speaks
    if want:
        if want == stock_id():
            return want
        try:
            st.get(want)
            return want
        except Exception:
            log.warning("voice: bot %s names voice %r, which does not exist", bot_id, want)
    default = st.default_id()
    if default:
        return default
    if not registry.has_builtin_voices():
        return stock_profile(build=False)
    return None


def profiles() -> list[dict]:
    """[{id, name}] of every voice profile (newest first)."""
    return [{"id": m["id"], "name": m.get("name") or m["id"]} for m in store().list()]


def profile_exists(pid: str) -> bool:
    if pid and pid == stock_id():
        return True                    # built on first use
    try:
        store().get(pid)
        return True
    except Exception:
        return False


def default_profile() -> str | None:
    return store().default_id()


def set_default_profile(pid: str | None) -> None:
    """Make ``pid`` the default profile, or (None) clear the default so the
    built-in voice speaks."""
    st = store()
    if pid:
        if pid == stock_id() and stock_profile(build=True) != pid:
            raise RuntimeError("the stock voice could not be built")
        st.update(pid, default=True)
        return
    cur = st.default_id()
    if cur:
        st.update(cur, default=False)


def fillers(profile_id: str | None) -> list[np.ndarray] | None:
    """Every pre-rendered filler of this profile (int16, 24 kHz), or None."""
    from .tts import builtin_id
    if not profile_id or builtin_id(profile_id):
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


def release() -> None:
    """Drop the loaded Chatterbox sessions (voice switched off). The lazy
    engine loads them again on next use."""
    with _STORE_LOCK:
        st = _STORE
    eng = getattr(st, "engine", None) if st is not None else None
    if isinstance(eng, LazyEngine):
        with eng._lock:
            eng._eng = None
    # Fillers not rendered yet are rendered on the next use, not now.
    _EXTRAS_STARTED.discard(str(voices_dir()))


def install(app, require_operator, extra: list | None = None) -> bool:
    """Register engine, resolver, fillers and mount the Voices router.
    False (and nothing registered) when dispatch-voice is not available.

    ``extra`` are more router dependencies: DisPatch passes the Voice cloning
    tool's switch, so the Voices API answers 404 while that tool is off. The
    ENGINE is registered either way: a cloned voice keeps speaking in Drive
    mode with the tool switched off; only managing voices is the tool's."""
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
    deps = [Depends(require_operator), *(extra or [])]
    app.include_router(build_router(LazyStore(), deps), prefix="/api/voices")
    log.info("voice: dispatch-voice mounted at /api/voices (unlocked tier only)")
    return True
