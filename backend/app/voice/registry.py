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
* ``api.register_engine(name, factory)`` adds a TTS engine selectable with
  ``DISPATCH_VOICE_TTS=<name>``. ``factory(settings)`` returns an object
  with the :class:`app.voice.tts.TTSEngine` shape.
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

_engines: dict[str, Callable] = {}
_profile_resolver: Callable[[str | None], str | None] | None = None
_ack_provider: Callable[[str | None], list] | None = None
_persona_provider: Callable[[str | None, str | None], str | None] | None = None


def register_engine(name: str, factory: Callable) -> None:
    _engines[name.strip().lower()] = factory


def engine_factory(name: str) -> Callable | None:
    return _engines.get((name or "").strip().lower())


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
    global _profile_resolver, _ack_provider, _persona_provider
    _engines.clear()
    _profile_resolver = _ack_provider = _persona_provider = None


@dataclass
class ExtensionAPI:
    """Handed to an extension's ``setup(api)``."""
    app: object
    require_full: Callable

    def include_router(self, router) -> None:
        def _unlocked_only(request: Request) -> None:
            self.require_full(request)

        self.app.include_router(router, dependencies=[Depends(_unlocked_only)])

    register_engine = staticmethod(register_engine)
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
