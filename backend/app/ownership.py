"""Who owns a conversation or a file (4.0 ownership, data model).

Every thread and every File Server record carries ``owner_profile_id``: the profile it
belongs to. This module answers one question for the creation paths - *whose is the
thing being created right now?* - so every path stamps the same answer:

* a signed-in profile (cookie, PIN, a device bound to a profile): that profile;
* a profile session minted before accounts existed, an open install (no PIN), a
  machine caller (loopback agent, API token, image callback), a mirror of a gateway
  session, a first-run seed: the **Owner**;
* an unsigned (anonymous, guest) device: the profile unsigned devices are meant to
  land in - the one Restricted profile, when an install has exactly one - else the
  Owner. This is the same rule migration ``009_ownership`` uses for the existing
  chats of characters made for Restricted profiles, so a chat a family tablet starts
  today and one it started before the upgrade end up with the same owner.

``None`` comes back only while an install has no Owner yet; creating the Owner adopts
every row still without one (``accounts.create_profile``).

Stamping only: nothing here decides who may READ a thread. That is the enforcement
round (``may_see_thread`` / ``require_thread`` land here then).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from . import accounts


def owner_id() -> str | None:
    """The Owner profile's id, or None when the install has none yet."""
    own = accounts.get_owner()
    return own.id if own is not None else None


def anonymous_target(profiles: Iterable[accounts.Profile] | None = None,
                     designated: str | None = None) -> str | None:
    """The profile an unsigned device's new things belong to.

    ``designated`` (a profile id an Admin picked for unsigned devices) wins when it
    names an existing, enabled Restricted profile. Otherwise exactly one enabled
    Restricted profile -> that one; none or several -> the Owner (never a guess
    between two children's profiles)."""
    rows = list(profiles) if profiles is not None else accounts.list_profiles()
    restricted = [p for p in rows if p.role == "restricted" and not p.disabled]
    if designated:
        hit = next((p for p in restricted if p.id == designated), None)
        if hit is not None:
            return hit.id
    if len(restricted) == 1:
        return restricted[0].id
    own = next((p for p in rows if p.role == "owner"), None)
    return own.id if own is not None else None


def for_principal(principal: Any) -> str | None:
    """Whose new thread / file this is, for the principal creating it."""
    if principal is None:
        return owner_id()
    kind = getattr(principal, "kind", "")
    if kind == "profile":
        return getattr(principal, "profile_id", None) or owner_id()
    if kind == "anonymous":
        return anonymous_target()
    # "open" (no PIN: everyone is the Owner) and "machine" (agents, cron, callbacks).
    return owner_id()


def for_request(request: Any) -> str | None:
    """:func:`for_principal` for an HTTP request (via the gate's policy verdict)."""
    from . import policy
    return for_principal(policy.of(request).principal)
