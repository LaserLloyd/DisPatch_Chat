"""Per-profile hooks the host offers add-ons (4.0 chat-first profiles, BUILD-PLAN-v2 §1.12).

An add-on that keeps per-person data (the voice add-on's cloned voices) needs three things
from DisPatch: *who is calling* (:func:`caller`), *which bots that caller may see*
(:func:`bots_visible`), and a way to be asked about a profile's data when the profile is
deleted or downgraded (:func:`register_profile_data`). It declares that its voices are
per-profile by registering a voice-access rule (:func:`register_voice_access`); the host
then gates the clone routes on ``voice_lab`` instead of ``manage_plugins``
(:func:`voice_owned_clones_active`, read by ``route_policy.extension_requirement``) and
checks a bot's ``voice`` field through :func:`voice_allowed`.

All of it is **additive**: ``EXT_API`` stays 1, and an add-on feature-tests with
``hasattr(ctx, "register_voice_access")``. An add-on that never registers (the live voice
add-on 1.5.0) keeps the old behaviour: every clone route is Admin-only.

``ExtensionContext`` (``app/extensions.py``) exposes ``ctx.caller(request)``,
``ctx.bots_visible(request)``, ``ctx.register_voice_access(fn)`` and
``ctx.register_profile_data(count=..., purge=...)`` as thin delegates to this module, and
every thread socket carries ``sock.profile_id``.

Every body here is real (WP-CORE). ``~/.cache/cfp/voice-host-contract.md`` is the add-on
side of the contract.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("local-chat.ext.profiles")

#: ``voice_access(voice_id, bot_owner) -> bool``. ``bot_owner`` is the profile id of a
#: personal bot's owner, or ``None`` for a household bot.
VoiceAccessFn = Callable[[str, "str | None"], bool]
#: ``count(profile_id) -> {"voices": n, ...}`` (counts only, never names or content).
CountFn = Callable[[str], Mapping[str, int]]
#: ``purge(profile_id) -> int``: delete everything the add-on keeps for that profile and
#: return how many items went.
PurgeFn = Callable[[str], int]

#: The ``voice`` field values that never belong to anybody, whatever an add-on says: a
#: built-in voice of a voice plugin (``builtin:<id>``) and "no voice set" (the default
#: voice). Every other id (a clone, the stock voice) is the add-on's to judge.
_BUILTIN_PREFIX = "builtin:"


@dataclass(frozen=True)
class Caller:
    """Who is calling an add-on route: no secrets, no profile record, just what a rule needs.

    ``role`` is ``"admin" | "user" | "basic"`` (``contracts.permissions.Role``); an open
    install (nobody owns it yet) is an Admin with every permission. ``can`` answers from
    the caller's effective permissions (the role table, the profile's overrides and the
    lock-target fence already applied)."""

    profile_id: str | None
    is_admin: bool
    role: str
    permissions: frozenset[str] = field(default_factory=frozenset, repr=False)
    #: True only for an open (unclaimed) install, where everything is allowed.
    open_install: bool = field(default=False, repr=False)

    def can(self, perm: str) -> bool:
        if self.open_install or self.is_admin:
            return True
        return str(perm) in self.permissions


# --------------------------------------------------------------------------- #
# Registries (real): what add-ons declared during setup
# --------------------------------------------------------------------------- #

_lock = threading.RLock()
_voice_access: dict[str, VoiceAccessFn] = {}
_profile_data: dict[str, tuple[CountFn | None, PurgeFn | None]] = {}


def register_voice_access(ext_id: str, fn: VoiceAccessFn) -> None:
    """Record ``ext_id``'s voice-access rule. Calling this IS how an add-on declares that its
    voices have owners (``voice_owned_clones_active``)."""
    if not callable(fn):
        raise TypeError("register_voice_access wants a callable(voice_id, bot_owner) -> bool")
    with _lock:
        _voice_access[ext_id] = fn


def register_profile_data(ext_id: str, *, count: CountFn | None = None,
                          purge: PurgeFn | None = None) -> None:
    """Record ``ext_id``'s per-profile data callbacks (either may be None)."""
    for name, fn in (("count", count), ("purge", purge)):
        if fn is not None and not callable(fn):
            raise TypeError(f"register_profile_data: {name} must be callable")
    with _lock:
        _profile_data[ext_id] = (count, purge)


def forget(ext_id: str) -> None:
    """Drop everything ``ext_id`` registered (its setup failed, or it is being unloaded)."""
    with _lock:
        _voice_access.pop(ext_id, None)
        _profile_data.pop(ext_id, None)


def reset_for_tests() -> None:
    with _lock:
        _voice_access.clear()
        _profile_data.clear()


def registered_voice_access() -> dict[str, VoiceAccessFn]:
    with _lock:
        return dict(_voice_access)


def registered_profile_data() -> dict[str, tuple[CountFn | None, PurgeFn | None]]:
    with _lock:
        return dict(_profile_data)


# --------------------------------------------------------------------------- #
# Who is calling (real)
# --------------------------------------------------------------------------- #


def caller_of(principal: Any) -> Caller | None:
    """The :class:`Caller` for a policy principal; ``None`` when nobody is signed in (an
    unsigned device, a machine caller, no principal at all)."""
    if principal is None:
        return None
    kind = getattr(principal, "kind", "")
    if kind == "open":
        return Caller(profile_id=None, is_admin=True, role="admin", open_install=True)
    if kind != "profile":
        return None
    role = str(getattr(principal, "role", "") or "")
    return Caller(profile_id=getattr(principal, "profile_id", None),
                  is_admin=bool(getattr(principal, "is_admin", False)),
                  role=role, permissions=frozenset(getattr(principal, "permissions", ()) or ()))


def caller(request: Any) -> Caller | None:
    """``ctx.caller(request)``: who is calling, from the gate's verdict for ``request``."""
    from . import policy
    try:
        principal = policy.of(request).principal
    except Exception:
        log.debug("caller(): no policy for this request", exc_info=True)
        return None
    return caller_of(principal)


# --------------------------------------------------------------------------- #
# The host-side consumers (WP-CORE)
# --------------------------------------------------------------------------- #


def bots_visible(request: Any) -> list:
    """``ctx.bots_visible(request) -> list[BotView]``: the bots the caller may see -
    ``config.load_bots()`` through ``characters.visible_to(bot, principal)``. Another
    profile's personal bot is never listed, Admins included; no principal (or nobody
    signed in) -> ``[]``."""
    from . import characters, config, policy
    from .extensions import bot_view
    try:
        principal = policy.of(request).principal
    except Exception:
        log.debug("bots_visible(): no policy for this request", exc_info=True)
        return []
    if principal is None or getattr(principal, "kind", "") in ("none", ""):
        return []
    return [bot_view(b) for b in config.load_bots() if characters.visible_to(b, principal)]


def is_household_voice(voice_id: str | None) -> bool:
    """A voice that never has an owner: none set (the default voice) or a plugin built-in."""
    v = str(voice_id or "").strip()
    return v == "" or v.startswith(_BUILTIN_PREFIX)


def voice_allowed(voice_id: str | None, bot_owner: str | None) -> bool:
    """May a bot owned by ``bot_owner`` (a profile id; ``None`` = household bot) speak with
    ``voice_id``? Used by ``profiles_service`` (create / copy / patch of personal bots) and
    by the character editor's validator.

    True for :func:`is_household_voice` ids; otherwise every registered voice-access rule
    must agree (``fn(voice_id, bot_owner)``; a rule that raises counts as "no"); with no rule
    registered (an add-on without per-profile clones), True."""
    if is_household_voice(voice_id):
        return True
    rules = registered_voice_access()
    for ext_id, fn in rules.items():
        try:
            if not fn(str(voice_id), bot_owner):
                return False
        except Exception:
            log.warning("voice_access rule of %s failed; counted as no", ext_id, exc_info=True)
            return False
    return True


def voice_owned_clones_active() -> bool:
    """Has an add-on declared per-profile clones (registered a voice-access rule)?

    ``route_policy.extension_requirement`` reads this: while it is False the old table
    applies (only the three voice reads are ``use_voice``; everything else is
    ``manage_plugins``, Admin only), so no non-Admin can create a household-wide clone."""
    return bool(registered_voice_access())


def count_profile_data(profile_id: str) -> dict[str, int]:
    """Sum of every registered ``count(profile_id)`` (``{"voices": n}``); feeds
    ``profiles_service.content_counts`` and the downgrade / delete confirmations. A count
    that raises is logged and skipped; non-integer values are ignored."""
    out: dict[str, int] = {}
    for ext_id, (count, _purge) in registered_profile_data().items():
        if count is None:
            continue
        try:
            got = count(profile_id) or {}
            for k, v in dict(got).items():
                if isinstance(v, int) and not isinstance(v, bool):
                    out[str(k)] = out.get(str(k), 0) + v
        except Exception:
            log.warning("profile-data count of %s failed", ext_id, exc_info=True)
    return out


def purge_profile_data(profile_id: str) -> int:
    """Ask every add-on to delete what it keeps for ``profile_id``; the total it deleted.
    Called on profile **deletion** and **User -> Basic downgrade**, never on Reset. A purge
    that raises is logged and the others still run."""
    total = 0
    for ext_id, (_count, purge) in registered_profile_data().items():
        if purge is None:
            continue
        try:
            n = purge(profile_id)
            if isinstance(n, int) and not isinstance(n, bool):
                total += n
        except Exception:
            log.warning("profile-data purge of %s failed", ext_id, exc_info=True)
    return total
