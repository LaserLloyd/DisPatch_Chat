"""Extension seam for Drive mode: voice profiles, extra TTS engines, a GUI.

Drive mode itself only needs "speak this text in voice X". Everything about
WHERE voices come from (cloning, a Voices settings GUI, which bot uses which
voice, pre-rendered acknowledgements in a cloned voice, a persona per voice)
lives in a separate extension package. That package plugs in here, without
DisPatch importing it by name.

Wiring:

* ``DISPATCH_VOICE_EXTENSIONS=pkg.module:setup[,other.module:setup]``. At
  startup main.py calls :func:`mount_extensions`, which imports each target
  and calls ``setup(api)`` with a :class:`ExtensionAPI`. The extension uses it
  to register things. A broken extension is logged and skipped, and never
  stops DisPatch booting.
* ``api.include_router(router)`` mounts the extension's FastAPI router with
  the app's UNLOCKED-TIER dependency added to every route. The extension
  cannot forget the gate, and Safe Mode gets 403 from every one of its
  routes. Prefix it under ``/api/voice/`` so the middleware's Safe-Mode
  prefix list covers it too.
* ``api.register_engine(name, factory, *, role="primary", missing=None,
  sample_rate=None)`` adds a TTS engine selectable with
  ``DISPATCH_VOICE_TTS=<name>``. ``factory(settings)`` returns an object with
  the :class:`app.voice.tts.TTSEngine` shape. ``role="fallback"`` marks an
  engine that may sit BEHIND the primary one (it speaks a sentence the
  primary cannot: a voice it does not know, or a failure before audio).
  ``missing(settings) -> list[str]`` says what the engine still needs (empty =
  ready); ``sample_rate`` lets DisPatch pair a fallback with a primary of the
  same rate without building either. Optional engine attributes DisPatch
  uses when present: ``cache_tag(profile_id) -> str | None`` (opt-in disk
  cache of acknowledgements and spoken notices: return a tag only when the
  output is a pure function of the text for that voice), ``apply_settings
  (settings)`` (the in-app voice/speed preferences changed) and ``warm()``
  (start-up prewarm).
* ``api.set_builtin_voices_provider(list_fn, preview=None, info=None)``: the
  "built-in voices" a plugin brings. ``list_fn(settings) -> [{id:
  "builtin:<x>", name, lang, default?}]``; ``preview(settings, voice_id,
  text) -> (int16 pcm, rate)`` renders the Settings preview. ``info``
  (API 3) describes the provider for the Built-in voices tool, so DisPatch
  names no plugin itself: ``{name, licence, install: [{where, cmd}],
  models: [{where, cmd}]}`` (``where`` is ``plugin``, ``env`` or
  ``backend``). ``builtin:<x>`` ids are opaque to DisPatch. With no provider
  the list is empty and a saved ``builtin:`` voice degrades to the default
  cloned/stock voice.
* ``api.add_release_hook(fn)`` (API 3): ``fn()`` is called when voice is
  switched off, after DisPatch dropped its own engines, so a plugin can drop
  the models it caches in its own module (switching off frees memory for
  plugins too).
* ``api.set_profile_resolver(fn)``: ``fn(bot_id) -> profile_id | None``. It
  picks the voice for a thread's bot, and the id is handed to the engine
  untouched.
* ``api.set_ack_provider(fn)``: ``fn(profile_id) -> list[int16 ndarray]``
  returns short acknowledgements already rendered in that voice, at the
  engine's sample rate.
* ``api.set_persona_provider(fn)``: ``fn(bot_id, profile_id) -> str | None``
  returns the spoken-reply instruction for that voice. ``None`` means the
  built-in default (persona.py).
"""
from __future__ import annotations

import importlib
import logging
from collections.abc import Callable
from dataclasses import dataclass

from fastapi import Depends, Request

log = logging.getLogger("dispatch.voice")

@dataclass(frozen=True)
class EngineSpec:
    name: str
    factory: Callable
    role: str = "primary"                     # "primary" | "fallback"
    missing: Callable | None = None           # settings -> list[str]
    sample_rate: int | None = None

    def missing_for(self, settings) -> list[str]:
        if self.missing is None:
            return []
        try:
            return list(self.missing(settings) or [])
        except Exception as e:
            log.exception("voice: engine %s readiness check failed", self.name)
            return [f"TTS engine {self.name} check failed ({type(e).__name__})"]

    def ready(self, settings) -> bool:
        return not self.missing_for(settings)


_engines: dict[str, EngineSpec] = {}
_builtin_list: Callable | None = None
_builtin_preview: Callable | None = None
_builtin_info: dict = {}
_release_hooks: list[Callable] = []
_profile_resolver: Callable[[str | None], str | None] | None = None
_ack_provider: Callable[[str | None], list] | None = None
_persona_provider: Callable[[str | None, str | None], str | None] | None = None


def register_engine(name: str, factory: Callable, *, role: str = "primary",
                    missing: Callable | None = None, sample_rate: int | None = None) -> None:
    if role not in ("primary", "fallback"):
        raise ValueError(f"role must be 'primary' or 'fallback', not {role!r}")
    key = name.strip().lower()
    _engines[key] = EngineSpec(key, factory, role, missing, sample_rate)


def engine_spec(name: str) -> EngineSpec | None:
    return _engines.get((name or "").strip().lower())


def engine_factory(name: str) -> Callable | None:
    spec = engine_spec(name)
    return spec.factory if spec else None


def engine_names() -> list[str]:
    return sorted(_engines)


def primary_engines() -> list[EngineSpec]:
    """Registered primary engines, in registration order."""
    return [e for e in _engines.values() if e.role == "primary"]


def fallback_for(settings, primary: str, sample_rate: int | None = None) -> EngineSpec | None:
    """The first ready fallback engine that can sit behind ``primary``: not
    the same engine, and (when both declare one) the same sample rate."""
    for spec in _engines.values():
        if spec.role != "fallback" or spec.name == (primary or "").strip().lower():
            continue
        if sample_rate and spec.sample_rate and spec.sample_rate != sample_rate:
            continue
        if spec.ready(settings):
            return spec
    return None


def set_builtin_voices_provider(list_fn: Callable, preview: Callable | None = None,
                                info: dict | None = None) -> None:
    global _builtin_list, _builtin_preview, _builtin_info
    _builtin_list, _builtin_preview = list_fn, preview
    _builtin_info = _clean_info(info)


def _clean_info(info) -> dict:
    """Only the documented keys, as short strings: it is shown to the client."""
    if not isinstance(info, dict):
        return {}
    out: dict = {}
    for k in ("name", "licence"):
        v = info.get(k)
        if isinstance(v, str) and v.strip():
            out[k] = v.strip()[:80]
    for k in ("install", "models"):
        steps = []
        for st in info.get(k) or []:
            if (isinstance(st, dict) and st.get("where") in ("plugin", "env", "backend")
                    and isinstance(st.get("cmd"), str) and st["cmd"].strip()):
                steps.append({"where": st["where"], "cmd": st["cmd"].strip()[:300]})
        if steps:
            out[k] = steps[:6]
    return out


def builtin_info() -> dict:
    """What the provider says about itself ({} without a provider or info)."""
    return dict(_builtin_info)


def add_release_hook(fn: Callable) -> None:
    _release_hooks.append(fn)


def run_release_hooks() -> None:
    for fn in list(_release_hooks):
        try:
            fn()
        except Exception:
            log.exception("voice: a release hook failed")


def has_builtin_voices() -> bool:
    return _builtin_list is not None


def builtin_voices(settings) -> list[dict]:
    """The provider's built-in voices; [] when no provider is installed or it fails."""
    if _builtin_list is None:
        return []
    try:
        out = _builtin_list(settings) or []
        return [dict(v) for v in out if isinstance(v, dict) and isinstance(v.get("id"), str)]
    except Exception:
        log.exception("voice: built-in voice provider failed")
        return []


def builtin_preview():
    """The provider's preview renderer, or None."""
    return _builtin_preview


def set_profile_resolver(fn) -> None:
    global _profile_resolver
    _profile_resolver = fn


def set_ack_provider(fn) -> None:
    global _ack_provider
    _ack_provider = fn


def set_persona_provider(fn) -> None:
    global _persona_provider
    _persona_provider = fn


def resolve_profile(bot_id: str | None) -> str | None:
    if _profile_resolver is None:
        # No profile extension: a bot can still pick a built-in voice.
        try:
            from app import config

            from .tts import builtin_id
            bot = config.get_bot(bot_id) if bot_id else None
            want = (getattr(bot, "voice", "") or "").strip()
            # A saved built-in voice only means something while a provider
            # is installed; without one the engine's default voice speaks.
            return want if builtin_id(want) and has_builtin_voices() else None
        except Exception:
            return None
    try:
        return _profile_resolver(bot_id) or None
    except Exception:
        log.exception("voice: profile resolver failed; using the default voice")
        return None


def acks_for(profile_id: str | None) -> list | None:
    """Pre-rendered acks for this profile, or None when no provider has any."""
    if _ack_provider is None:
        return None
    try:
        acks = _ack_provider(profile_id)
        return list(acks) if acks else None
    except Exception:
        log.exception("voice: ack provider failed")
        return None


def persona_for(bot_id: str | None, profile_id: str | None) -> str | None:
    if _persona_provider is None:
        return None
    try:
        p = _persona_provider(bot_id, profile_id)
        return p.strip() if isinstance(p, str) and p.strip() else None
    except Exception:
        log.exception("voice: persona provider failed")
        return None


def reset() -> None:
    """Tests only."""
    global _profile_resolver, _ack_provider, _persona_provider, _builtin_list, _builtin_preview
    global _builtin_info
    _engines.clear()
    _profile_resolver = _ack_provider = _persona_provider = None
    _builtin_list = _builtin_preview = None
    _builtin_info = {}
    _release_hooks.clear()


@dataclass
class ExtensionAPI:
    """Handed to an extension's ``setup(api)``."""
    app: object
    require_full: Callable
    #: 2 = role/missing/sample_rate on register_engine + the built-in voices
    #: provider. 3 = ``info=`` on set_builtin_voices_provider and
    #: add_release_hook. An extension can check it and refuse politely, or
    #: pass the newer arguments only when they exist.
    api_version: int = 3

    def include_router(self, router) -> None:
        def _unlocked_only(request: Request) -> None:
            self.require_full(request)

        self.app.include_router(router, dependencies=[Depends(_unlocked_only)])

    register_engine = staticmethod(register_engine)
    set_builtin_voices_provider = staticmethod(set_builtin_voices_provider)
    add_release_hook = staticmethod(add_release_hook)
    set_profile_resolver = staticmethod(set_profile_resolver)
    set_ack_provider = staticmethod(set_ack_provider)
    set_persona_provider = staticmethod(set_persona_provider)


def mount_extensions(app, require_full: Callable, spec: str | None = None) -> list[str]:
    """Load every ``module:callable`` in DISPATCH_VOICE_EXTENSIONS. Returns
    the targets that loaded."""
    import os
    spec = spec if spec is not None else os.environ.get("DISPATCH_VOICE_EXTENSIONS", "")
    loaded: list[str] = []
    for target in (t.strip() for t in spec.split(",")):
        if not target:
            continue
        mod_name, _, attr = target.partition(":")
        try:
            fn = getattr(importlib.import_module(mod_name), attr or "setup")
            fn(ExtensionAPI(app, require_full))
            loaded.append(target)
            log.info("voice: extension %s loaded", target)
        except Exception:
            log.exception("voice: extension %s failed to load; skipped", target)
    return loaded
