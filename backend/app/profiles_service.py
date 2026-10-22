"""Profile lifecycle: counts, Reset to default, User <-> Basic, personal bots, copies, Atlas.

4.0 chat-first profiles (BUILD-PLAN-v2 §1.3). One synchronous module behind one lock
(:data:`LOCK`): every operation here changes several stores at once (chats.db threads and
files, config.yaml personal bots, avatar files, voice-acks entries, the Image Studio
ledger, the advisor ledger, agent tasks, voice cursors), and two of them racing for the
same profile must not interleave.

Status (Foundation): **signatures only**. Every function raises ``NotImplementedError``
until WP-PROF lands its body (WP-PROF owns this file; the INT-1 merge removes the last
stub). Callers in other packages may import and call these names now; the return shapes
below are the contract.

Counts are numbers only, never names or content: an Admin sees "3 bots, 41 chats" for a
profile whose chats they can never open.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

#: Held for the whole of every operation below.
LOCK = threading.RLock()

#: How long a preview token (Reset, downgrade) stays valid.
TOKEN_TTL_SECONDS = 300


class ProfileServiceError(ValueError):
    """A refused lifecycle operation. ``code`` is a stable slug (errors catalog); ``status``
    the HTTP status the API layer answers with (409 = a stale preview token)."""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class ContentCounts:
    """What a Reset / downgrade / delete would remove (or did remove)."""

    bots: int = 0           # personal bots (scope: personal, owner_profile == the profile)
    chats: int = 0          # threads owned by the profile
    pictures: int = 0       # chat images + Image Studio results owned by the profile
    voices: int = 0         # cloned voices the profile owns (ext_profile_hooks.count_profile_data)

    def as_dict(self) -> dict[str, int]:
        return {"bots": self.bots, "chats": self.chats, "pictures": self.pictures,
                "voices": self.voices}


def content_counts(profile_id: str) -> dict[str, int]:
    """``{"bots", "chats", "pictures", "voices"}`` for ``profile_id`` (counts only)."""
    raise NotImplementedError("profiles_service.content_counts: WP-PROF")


def wipe_profile_content(profile_id: str) -> dict[str, int]:
    """Delete the profile's threads (messages, image_jobs, thread media and avatar snapshots
    cascade), personal bots (config entries, avatar files, voice-acks entries),
    ``source='chat'`` files, Image Studio ledger rows and ``prompts-<pid>.json``,
    advisor-ledger requests, agents_store tasks and voice cursors. Keeps
    ``source='fileserver'`` files and push subscriptions; a ``/media/`` file is deleted only
    when no surviving message references it. Does NOT purge cloned voices (Reset keeps
    them; callers that must, call ``ext_profile_hooks.purge_profile_data``).
    Returns what was deleted, in the :class:`ContentCounts` keys."""
    raise NotImplementedError("profiles_service.wipe_profile_content: WP-PROF")


def spawn_atlas(profile_id: str) -> str:
    """Copy the shipped Atlas template into ``profile_id`` as a personal bot with an opaque
    ``p<12hex>`` id (direct backend on the household default connection, no knowledge_dir,
    image_jobs off, tools on, the Atlas avatar copied). Returns the new bot id."""
    raise NotImplementedError("profiles_service.spawn_atlas: WP-PROF")


def reset_preview(profile_id: str) -> dict[str, Any]:
    """``{"counts": {...}, "token": str, "expires_in": TOKEN_TTL_SECONDS}``. The token binds
    the profile id and the counts."""
    raise NotImplementedError("profiles_service.reset_preview: WP-PROF")


def reset_profile(profile_id: str, token: str) -> dict[str, int]:
    """Reset to default: check ``token`` (stale or mismatched counts ->
    ``ProfileServiceError("reset_stale", ..., 409)``), wipe, then Admin/User -> a fresh
    Atlas, Basic -> its assigned household list stays. Returns the deleted counts."""
    raise NotImplementedError("profiles_service.reset_profile: WP-PROF")


def change_role_preview(profile_id: str, new_role: str) -> dict[str, Any]:
    """For User -> Basic: ``{"counts": {... incl. voices}, "token": str, "expires_in": ...}``;
    for any other change ``{"counts": {}, "token": "", ...}`` (nothing is deleted)."""
    raise NotImplementedError("profiles_service.change_role_preview: WP-PROF")


def change_role(profile_id: str, new_role: str, *, confirm_token: str | None = None,
                household_bots: list[str] | str | None = None) -> Any:
    """Change a profile's role. User -> Basic needs ``confirm_token`` from
    :func:`change_role_preview`, wipes the content, purges cloned voices, resets the
    household list to ``household_bots`` (Admin-chosen) and trims overrides to the Basic
    table. Basic -> User changes the role only (chats kept). Returns the updated
    ``accounts.Profile``. The protected Admin's role never changes."""
    raise NotImplementedError("profiles_service.change_role: WP-PROF")


def create_personal(profile_id: str, fields: Mapping[str, Any]) -> Any:
    """Create a personal bot for ``profile_id`` from the editor's fields (name, emoji,
    personality, voice, backend). Direct backend only; ``voice`` checked with
    ``ext_profile_hooks.voice_allowed(voice, profile_id)``; backend within
    ``backends_policy.allowed_backends(profile)``. Returns the new ``config.Bot``."""
    raise NotImplementedError("profiles_service.create_personal: WP-PROF")


def clone_to_own(bot_id: str, principal: Any) -> dict[str, Any]:
    """A User copies a household bot into their own profile. Setup fields only (name, emoji,
    picture, personality, voice, backend); agent bots -> ``ProfileServiceError(
    "not_copyable", ..., 422)``. Returns ``{"bot": <character row>, "voice_cleared": bool}``."""
    raise NotImplementedError("profiles_service.clone_to_own: WP-PROF")


def clone_to_profile(bot_id: str, profile_id: str, *, actor: Any) -> dict[str, Any]:
    """An Admin copies a household bot into ``profile_id`` (which then owns it; the Admin
    cannot see or edit the copy). Same field allowlist and voice rule as
    :func:`clone_to_own`. Returns ``{"bot_id": str, "voice_cleared": bool}`` (no row: the
    Admin may not read it)."""
    raise NotImplementedError("profiles_service.clone_to_profile: WP-PROF")


def on_profile_created(profile: Any, *, first_admin: bool = False) -> None:
    """Called once after a profile is created: Admin and User profiles get their own Atlas
    (:func:`spawn_atlas`), except the first Admin of a fresh install (``first_admin``), who
    sees the household Atlas. Basic profiles get nothing."""
    raise NotImplementedError("profiles_service.on_profile_created: WP-PROF")
