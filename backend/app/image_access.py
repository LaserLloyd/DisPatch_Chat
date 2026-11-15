"""Which profile types may use which image generator (Civitai plan WP2).

Two generator classes: ``standard`` (MiniMax, OpenAI, xAI, custom servers) and ``civitai``.
The matrix lives in six boolean settings, ``connections.image_access.<generator>_<role>``.
This sits ON TOP of the ``use_images`` permission, never instead of it. It must not import
``image_providers`` (no import cycle).

The image server in Image Studio is NOT part of that matrix. It is one person at a
time, by name: ``connections.image_access.server_profiles`` holds the profile ids the
Owner ticked, and ``allows_server`` is the gate. A role switch would turn it on for
every Admin (or every User) at once, which is the wrong shape.
"""
from __future__ import annotations

from . import accounts, policy, settings_store
from .contracts.permissions import Role

GENERATORS: tuple[str, ...] = ("standard", "civitai")
ROLES: tuple[str, ...] = ("admin", "user", "basic")

#: The Owner-only list of the profiles that may use the image server in Image Studio.
SERVER_PROFILES_KEY = "connections.image_access.server_profiles"
#: How many names that list may hold.
SERVER_PROFILES_MAX = 64


def setting_key(generator: str, role: str) -> str:
    return f"connections.image_access.{generator}_{role}"


def allows(principal, generator: str) -> bool:
    """May ``principal`` use ``generator``? Fail closed on anything unexpected."""
    if generator not in GENERATORS or principal is None:
        return False
    if getattr(principal, "kind", None) == "machine" or not getattr(principal, "signed_in", False):
        return False
    if principal.is_protected:
        return True
    role = getattr(principal, "role", None)
    if role not in ROLES:
        return False
    if generator == "civitai" and policy.is_lock_target(getattr(principal, "profile", None)):
        return False
    return bool(settings_store.get(setting_key(generator, role)))


def allows_profile(profile, generator: str) -> bool:
    """Same rule for a stored profile (e.g. a thread's owner)."""
    if profile is None:
        return False
    return allows(policy.profile_principal(profile), generator)


# --------------------------------------------------------------------------- #
# The image server in Image Studio: the Owner, and only the names the Owner ticked
# --------------------------------------------------------------------------- #


def allows_server(principal) -> bool:
    """May ``principal`` use the image server in Image Studio? Fail closed.

    The Owner (and an open install) always may. Nobody else may until the Owner
    ticks that person's own name: a Basic profile and the shared sign-in profile
    (the lock target) never may, whatever the stored list says.
    """
    if principal is None:
        return False
    if getattr(principal, "kind", None) == "machine" or not getattr(principal, "signed_in", False):
        return False
    if getattr(principal, "is_protected", False):
        return True
    profile = getattr(principal, "profile", None)
    if not _server_eligible(profile):
        return False
    return profile.id in server_profiles()


def server_profiles() -> list[str]:
    """The profile ids that may use the image server: the stored list, sanitised."""
    return sanitize_server_profiles(settings_store.get(SERVER_PROFILES_KEY))


def sanitize_server_profiles(ids) -> list[str]:
    """The ids of ``ids`` that may be stored: text only, no duplicates, and only profiles
    that exist and may be named at all. Capped at 64.

    This runs on every read AND before every save, so a hand-edited value can
    never grant a Basic profile or the shared sign-in profile.
    """
    if not isinstance(ids, (list, tuple)):
        return []
    out: list[str] = []
    for raw in ids:
        if not isinstance(raw, str):
            continue
        pid = raw.strip()
        if not pid or pid in out:
            continue
        profile = accounts.get_profile(pid)
        if not _server_eligible(profile):
            continue
        out.append(pid)
        if len(out) >= SERVER_PROFILES_MAX:
            break
    return out


def eligible_server_profiles() -> list[accounts.Profile]:
    """The profiles that may be named in the list at all: not the Owner, not a
    Basic profile, not the shared sign-in profile."""
    return [p for p in accounts.list_profiles() if _server_eligible(p)]


def _server_eligible(profile) -> bool:
    """May this profile be named in the list at all? Not the Owner, not a Basic
    profile, and not the shared sign-in profile (the lock target)."""
    if profile is None:
        return False
    if getattr(profile, "protected", False):
        return False
    if getattr(profile, "role", None) is Role.BASIC:
        return False
    if policy.is_lock_target(profile):
        return False
    return True
