"""Profile lifecycle routes (4.0 chat-first profiles, BUILD-PLAN-v2 §1.3).

``main.py`` includes this router once. Every route is declared in ``route_policy.py`` (the WP-PROF
section); the checks that cannot be a permission (this bot is yours, that profile is not the
protected Admin) are made here and answer exactly like a thing that does not exist.

Yours (the signed-in profile)
-----------------------------
* ``GET  /api/me/reset/preview``  -> ``{counts, token, expires_in, outcome, keeps_voices}``
* ``POST /api/me/reset`` ``{token}`` -> what was deleted; a stale token is 409 ``reset_stale``
* ``GET/POST /api/me/characters``, ``GET/PATCH/DELETE /api/me/characters/{bot_id}``,
  ``POST /api/me/characters/{bot_id}/avatar``: your own bots (never anyone else's: 404)
* ``GET  /api/me/export``: your chats and your bots' setup, as JSON
* ``GET  /api/me/personas``: the personality presets the bot editor offers
* ``GET  /api/me/backends``: the household connections YOUR bots may run on (ids, names and
  model names only - never a key or a URL); empty without ``choose_backend``

Copies
------
* ``POST /api/characters/{bot_id}/clone``: a User copies a household bot into their profile
* ``POST /api/characters/{bot_id}/clone-to/{profile_id}``: an Admin copies one into a profile
  (setup only; the Admin does not get to see the copy)

People (an Admin acting on a profile; counts only, never content)
-----------------------------------------------------------------
* ``GET  /api/people/{profile_id}/reset/preview`` and ``POST .../reset`` (not the protected
  Admin unless it is the caller)
* ``GET  /api/people/{profile_id}/role/preview?role=`` and ``POST .../role``: the only way a
  role changes to Basic, because that deletes the profile's bots and chats
* ``PUT  /api/people/{profile_id}/household-bots`` and ``PUT .../backends``: which household
  bots a profile may use, and which connections its own bots may run on
* ``GET  /api/people/{profile_id}/delete/preview`` and ``POST .../delete``: the only way a
  profile is removed, for the same reason

Audit rows carry ids and counts, never names or content.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from . import characters, policy, profiles_service
from .api_common import audit, caller, fail
from .contracts.permissions import Permission
from .profiles_service import ProfileServiceError

log = logging.getLogger("local-chat.profiles.api")

router = APIRouter()

_HISTORY = Depends(policy.require_permission(Permission.MANAGE_HISTORY))
_CREATE = Depends(policy.require_permission(Permission.CREATE_CHARACTERS))
_USERS = Depends(policy.require_permission(Permission.MANAGE_USERS))


# --------------------------------------------------------------------------- #
# Bodies
# --------------------------------------------------------------------------- #


class TokenIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=1, max_length=2048)


class MyCharacterCreateIn(BaseModel):
    """What the editor sends for a new bot. Everything else (flags, agents, pools, audience)
    is household and is not accepted here."""
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=40)
    emoji: str = Field(default="", max_length=8)
    description: str = Field(default="", max_length=400)
    color: str = Field(default="", max_length=32)
    personality: str | None = Field(default=None, max_length=32)
    persona: str | None = Field(default=None, max_length=characters.MAX_PERSONAL_PERSONA)
    voice: str = Field(default="", max_length=80)
    connection: str | None = Field(default=None, max_length=48)
    model: str | None = Field(default=None, max_length=200)
    tools: bool = True
    quick_actions: bool = True

    @field_validator("name", "emoji")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()

    @model_validator(mode="after")
    def _shape(self) -> MyCharacterCreateIn:
        if not self.name:
            raise ValueError("name must not be blank")
        if self.personality and self.persona is not None:
            raise ValueError("send personality (a preset id) or persona (text), not both")
        return self


class MyCharacterPatchIn(BaseModel):
    """Present keys are set, absent ones left alone; ``connection`` null/"" detaches."""
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=40)
    emoji: str | None = Field(default=None, max_length=8)
    description: str | None = Field(default=None, max_length=400)
    color: str | None = Field(default=None, max_length=32)
    personality: str | None = Field(default=None, max_length=32)
    persona: str | None = Field(default=None, max_length=characters.MAX_PERSONAL_PERSONA)
    voice: str | None = Field(default=None, max_length=80)
    connection: str | None = Field(default=None, max_length=48)
    model: str | None = Field(default=None, max_length=200)
    tools: bool | None = None
    quick_actions: bool | None = None

    @model_validator(mode="after")
    def _something(self) -> MyCharacterPatchIn:
        if not self.model_fields_set:
            raise ValueError("nothing to change")
        for k in self.model_fields_set - {"connection"}:
            if getattr(self, k) is None:
                raise ValueError(f"{k} must not be null")
        if "personality" in self.model_fields_set and "persona" in self.model_fields_set:
            raise ValueError("send personality (a preset id) or persona (text), not both")
        return self


class RoleChangeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["admin", "user", "basic", "member", "restricted"]
    confirm_token: str | None = Field(default=None, max_length=2048)
    #: Basic only: the household bots the profile keeps (default: what it had, else every
    #: bot a Basic profile can use).
    household_bots: list[str] | Literal["all"] | None = None


class HouseholdIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    household_bots: list[str] | Literal["all"]


class BackendsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Connection ids; null = every household connection.
    allowed: list[str] | None = None


class DeleteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm_token: str = Field(min_length=1, max_length=2048)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _err(e: ProfileServiceError) -> Any:
    return fail(e.status, e.code, str(e))


def _my_id(request: Request) -> str:
    pid = caller(request).profile_id
    if not pid:
        # An open install has no profile yet: there is nothing of "yours" to reset or list.
        raise fail(409, "profile_required", "Sign in to a profile first.")
    return pid


def _counts_detail(counts: dict[str, int]) -> str:
    return " ".join(f"{k}={v}" for k, v in sorted(counts.items()))


def _target(request: Request, profile_id: str, *, allow_self_protected: bool = False):
    """The profile an Admin acts on. The protected Admin is out of reach of every other Admin
    (structural, as in the People routes); it may act on itself where the route allows."""
    from . import accounts
    prof = accounts.get_profile(profile_id)
    if prof is None:
        raise fail(404, "profile_not_found", "No such profile.")
    me = caller(request)
    if prof.protected and not (allow_self_protected and prof.id == me.profile_id) and not (
            me.kind == "open"):
        raise fail(403, "permission_denied", "You cannot manage that profile.")
    return prof


async def _withdraw_image_jobs(jobs: list[dict], why: str) -> None:
    """Cancel the rig-side work of image jobs whose chats were just deleted."""
    if not jobs:
        return
    try:
        from . import main
        await main._cancel_open_image_jobs(jobs, why)
    except Exception:
        log.warning("could not withdraw %d image job(s) from the rig", len(jobs), exc_info=True)


async def _roster_changed() -> None:
    from .characters_api import broadcast_roster
    try:
        await broadcast_roster()
    except Exception:
        log.debug("roster broadcast failed", exc_info=True)


# --------------------------------------------------------------------------- #
# Reset (yours)
# --------------------------------------------------------------------------- #


@router.get("/api/me/reset/preview", dependencies=[_HISTORY])
async def my_reset_preview(request: Request):
    pid = _my_id(request)
    try:
        return await asyncio.to_thread(profiles_service.reset_preview, pid)
    except ProfileServiceError as e:
        raise _err(e) from e


@router.post("/api/me/reset", dependencies=[_HISTORY])
async def my_reset(request: Request, payload: TokenIn):
    pid = _my_id(request)
    return await _do_reset(request, pid, payload.token)


async def _do_reset(request: Request, pid: str, token: str) -> dict:
    jobs = await asyncio.to_thread(profiles_service.open_image_jobs, pid)
    try:
        deleted = await asyncio.to_thread(profiles_service.reset_profile, pid, token)
    except ProfileServiceError as e:
        raise _err(e) from e
    audit(request, "profile.reset", pid, _counts_detail(deleted))
    await _withdraw_image_jobs(jobs, "profile reset")
    await _roster_changed()
    return {"deleted": deleted, "keeps_voices": True}


# --------------------------------------------------------------------------- #
# My bots
# --------------------------------------------------------------------------- #


@router.get("/api/me/personas", dependencies=[_CREATE])
async def my_personas(request: Request):
    """The personality presets, for the owner's bot editor (the Admin manager reads the same
    list at ``/api/characters/personas``). Preset texts are shipped content, not anyone's data."""
    from . import personas
    return personas.public()


@router.get("/api/me/backends", dependencies=[_CREATE])
async def my_backends(request: Request):
    """The connections the caller's own bots may use: chat-capable household connections
    within the profile's ``backends.allowed``. No key, no base URL, no usage - those stay
    with the Admin's Connections page. A profile without ``choose_backend`` gets an empty
    list (its bots run on the household default)."""
    from . import accounts, backends_policy, connections
    pid = _my_id(request)
    prof = await asyncio.to_thread(accounts.get_profile, pid)
    if prof is None:
        raise fail(404, "profile_not_found", "No such profile.")
    may_choose = Permission.CHOOSE_BACKEND.value in policy.effective_permissions(prof)
    rows: list[dict[str, Any]] = []
    default = ""
    if may_choose:
        every = await asyncio.to_thread(connections.list_all)
        # Non-Admins never see the Admin's name for a connection (it may hold an address or an
        # account): the kind plus a number. An Admin's own editor shows the real names.
        labels = ({c.id: c.name for c in every} if caller(request).is_admin
                  else connections.neutral_labels(every))
        for c in every:
            if "chat" not in c.capabilities or not backends_policy.permits(prof, c.id):
                continue
            rows.append({"id": c.id, "name": labels[c.id], "type": c.type, "type_label": c.ctype.label,
                         "default_model": c.default_model, "models": list(c.models)})
        default = await asyncio.to_thread(backends_policy.resolve_connection, prof, None)
    return {"may_choose": may_choose, "default": default, "connections": rows}


@router.get("/api/me/characters", dependencies=[_CREATE])
async def my_characters(request: Request):
    pid = _my_id(request)
    return {"characters": await asyncio.to_thread(profiles_service.list_personal, pid)}


@router.post("/api/me/characters", status_code=201, dependencies=[_CREATE])
async def my_character_create(request: Request, payload: MyCharacterCreateIn):
    pid = _my_id(request)
    body = payload.model_dump(exclude_none=True)
    try:
        bot = await asyncio.to_thread(profiles_service.create_personal, pid, body)
    except ProfileServiceError as e:
        raise _err(e) from e
    audit(request, "character.create", bot.id)
    await _roster_changed()
    return JSONResponse(characters.character_out(bot), status_code=201)


@router.get("/api/me/characters/{bot_id}", dependencies=[_CREATE])
async def my_character(request: Request, bot_id: str):
    pid = _my_id(request)
    try:
        bot = await asyncio.to_thread(characters.get_personal, bot_id, pid)
    except characters.CharacterError as e:
        raise fail(e.status, e.code, str(e)) from e
    return characters.character_out(bot)


@router.patch("/api/me/characters/{bot_id}", dependencies=[_CREATE])
async def my_character_patch(request: Request, bot_id: str, payload: MyCharacterPatchIn):
    pid = _my_id(request)
    changes = payload.model_dump(exclude_unset=True)
    try:
        bot = await asyncio.to_thread(profiles_service.update_personal, pid, bot_id, changes)
    except ProfileServiceError as e:
        raise _err(e) from e
    audit(request, "character.update", bot_id, ",".join(sorted(changes))[:200])
    await _roster_changed()
    return characters.character_out(bot)


@router.delete("/api/me/characters/{bot_id}", dependencies=[_CREATE])
async def my_character_delete(request: Request, bot_id: str):
    pid = _my_id(request)
    jobs = await asyncio.to_thread(profiles_service.open_image_jobs, pid)
    try:
        bot = await asyncio.to_thread(characters.get_personal, bot_id, pid)
        gone = await asyncio.to_thread(profiles_service.delete_personal, pid, bot.id)
    except characters.CharacterError as e:
        raise fail(e.status, e.code, str(e)) from e
    except ProfileServiceError as e:
        raise _err(e) from e
    audit(request, "character.delete", bot_id, _counts_detail(gone))
    await _withdraw_image_jobs([j for j in jobs if j.get("bot_id") == bot_id], "bot deleted")
    await _roster_changed()
    return {"removed": bot_id, "deleted": gone}


@router.post("/api/me/characters/{bot_id}/avatar", dependencies=[_CREATE])
async def my_character_avatar(request: Request, bot_id: str, file: UploadFile = File(...)):
    pid = _my_id(request)
    ctype = (file.content_type or "").lower()
    if not ctype.startswith("image/") or ctype == "image/svg+xml":
        raise fail(400, "avatar_invalid", "A picture must be a raster image.")
    raw = await file.read(profiles_service.MAX_AVATAR_BYTES + 1)
    try:
        bot = await asyncio.to_thread(profiles_service.set_personal_avatar, pid, bot_id, raw)
    except ProfileServiceError as e:
        raise _err(e) from e
    audit(request, "character.avatar", bot_id)
    await _roster_changed()
    return {"ok": True, "avatar_url": bot.avatar_url}


# --------------------------------------------------------------------------- #
# Export (yours)
# --------------------------------------------------------------------------- #


@router.get("/api/me/export", dependencies=[_HISTORY])
async def my_export(request: Request):
    pid = _my_id(request)
    from datetime import datetime

    def gen():
        yield from profiles_service.export_chunks(pid)

    audit(request, "profile.export", pid)
    name = f"dispatch-my-data-{datetime.now().strftime('%Y%m%d')}.json"
    return StreamingResponse(gen(), media_type="application/json",
                             headers={"Content-Disposition": f'attachment; filename="{name}"',
                                      "Cache-Control": "no-store"})


# --------------------------------------------------------------------------- #
# Copies
# --------------------------------------------------------------------------- #


@router.post("/api/characters/{bot_id}/clone", status_code=201, dependencies=[_CREATE])
async def clone_character(request: Request, bot_id: str):
    _my_id(request)
    try:
        out = await asyncio.to_thread(profiles_service.clone_to_own, bot_id, caller(request))
    except ProfileServiceError as e:
        raise _err(e) from e
    audit(request, "character.clone", out["bot"]["id"], f"from={bot_id}")
    await _roster_changed()
    return JSONResponse(out, status_code=201)


@router.post("/api/characters/{bot_id}/clone-to/{profile_id}", status_code=201, dependencies=[_USERS])
async def clone_character_to(request: Request, bot_id: str, profile_id: str):
    _target(request, profile_id, allow_self_protected=True)
    try:
        out = await asyncio.to_thread(profiles_service.clone_to_profile, bot_id, profile_id,
                                      actor=caller(request))
    except ProfileServiceError as e:
        raise _err(e) from e
    # The id of the copy is opaque; the audit row says which bot it came from and for whom.
    audit(request, "character.clone_to", profile_id, f"from={bot_id}")
    await _roster_changed()
    return JSONResponse(out, status_code=201)


# --------------------------------------------------------------------------- #
# People: reset, role, delete
# --------------------------------------------------------------------------- #


@router.get("/api/people/{profile_id}/reset/preview", dependencies=[_USERS])
async def people_reset_preview(request: Request, profile_id: str):
    _target(request, profile_id, allow_self_protected=True)
    try:
        return await asyncio.to_thread(profiles_service.reset_preview, profile_id)
    except ProfileServiceError as e:
        raise _err(e) from e


@router.post("/api/people/{profile_id}/reset", dependencies=[_USERS])
async def people_reset(request: Request, profile_id: str, payload: TokenIn):
    _target(request, profile_id, allow_self_protected=True)
    return await _do_reset(request, profile_id, payload.token)


@router.get("/api/people/{profile_id}/role/preview", dependencies=[_USERS])
async def people_role_preview(request: Request, profile_id: str, role: str):
    _target(request, profile_id, allow_self_protected=True)
    try:
        return await asyncio.to_thread(profiles_service.change_role_preview, profile_id, role)
    except ProfileServiceError as e:
        raise _err(e) from e


@router.post("/api/people/{profile_id}/role", dependencies=[_USERS])
async def people_role(request: Request, profile_id: str, payload: RoleChangeIn):
    me = caller(request)
    prof = _target(request, profile_id, allow_self_protected=True)
    if prof.id == me.profile_id:
        raise fail(409, "permission_denied", "You cannot change your own role.")
    jobs = await asyncio.to_thread(profiles_service.open_image_jobs, profile_id)
    try:
        updated = await asyncio.to_thread(
            profiles_service.change_role, profile_id, payload.role,
            confirm_token=payload.confirm_token, household_bots=payload.household_bots)
    except ProfileServiceError as e:
        raise _err(e) from e
    audit(request, "profile.role", profile_id, f"{prof.role.value}->{updated.role.value}")
    if prof.role is not updated.role:
        await _withdraw_image_jobs(jobs, "profile role changed")
        await _roster_changed()
    return updated.public()


@router.put("/api/people/{profile_id}/household-bots", dependencies=[_USERS])
async def people_household(request: Request, profile_id: str, payload: HouseholdIn):
    _target(request, profile_id)
    try:
        updated = await asyncio.to_thread(profiles_service.set_household_bots, profile_id,
                                          payload.household_bots)
    except ProfileServiceError as e:
        raise _err(e) from e
    audit(request, "profile.household", profile_id,
          "all" if updated.household_bots == "all" else f"count={len(updated.household_bots)}")
    return updated.public()


@router.put("/api/people/{profile_id}/backends", dependencies=[_USERS])
async def people_backends(request: Request, profile_id: str, payload: BackendsIn):
    _target(request, profile_id)
    try:
        updated = await asyncio.to_thread(profiles_service.set_backends, profile_id, payload.allowed)
    except ProfileServiceError as e:
        raise _err(e) from e
    audit(request, "profile.backends", profile_id,
          "all" if payload.allowed is None else f"count={len(payload.allowed)}")
    return {"id": updated.id, "backends": dict(updated.backends)}


@router.get("/api/people/{profile_id}/delete/preview", dependencies=[_USERS])
async def people_delete_preview(request: Request, profile_id: str):
    _target(request, profile_id, allow_self_protected=True)
    try:
        return await asyncio.to_thread(profiles_service.delete_preview, profile_id)
    except ProfileServiceError as e:
        raise _err(e) from e


@router.post("/api/people/{profile_id}/delete", dependencies=[_USERS])
async def people_delete(request: Request, profile_id: str, payload: DeleteIn):
    me = caller(request)
    _target(request, profile_id, allow_self_protected=True)
    if profile_id == me.profile_id:
        raise fail(409, "permission_denied", "You cannot delete the profile you are using.")
    jobs = await asyncio.to_thread(profiles_service.open_image_jobs, profile_id)
    try:
        deleted = await asyncio.to_thread(profiles_service.delete_profile, profile_id,
                                          payload.confirm_token)
    except ProfileServiceError as e:
        raise _err(e) from e
    audit(request, "profile.delete", profile_id, _counts_detail(deleted))
    await _withdraw_image_jobs(jobs, "profile deleted")
    await _roster_changed()
    return {"ok": True, "deleted": deleted}
