"""Which profile types may use which image generator (Civitai plan WP2).

Two generator classes: ``standard`` (MiniMax, OpenAI, xAI, custom servers) and ``civitai``.
The matrix lives in six boolean settings, ``connections.image_access.<generator>_<role>``.
This sits ON TOP of the ``use_images`` permission, never instead of it. It must not import
``image_providers`` (no import cycle).
"""
from __future__ import annotations

from . import policy, settings_store

GENERATORS: tuple[str, ...] = ("standard", "civitai")
ROLES: tuple[str, ...] = ("admin", "user", "basic")


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
