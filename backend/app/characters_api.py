"""``/api/characters`` and ``/api/starter-packs`` - the 3.0 character API.

* ``GET    /api/characters``            the characters this caller may talk to (public fields)
* ``GET    /api/characters/all``        every HOUSEHOLD character with its settings (manage_characters)
                                        - a profile's personal bots are not here for anyone
* ``GET    /api/characters/personas``   the personality presets             (manage_characters)
* ``GET    /api/characters/{id}``       one character, in full              (manage_characters)
* ``POST   /api/characters``            add one                             (manage_characters)
* ``PATCH  /api/characters/{id}``       change it - hidden, connection, model, personality, voice,
                                        flags...; unified (4.2): a template (manage_characters),
                                        an own bot or copy (its owner), an agent bot (the Owner)
* ``GET    /api/characters/manage``     the Characters page's rows: Mine + Household (4.2)
* give-to preview / PUT, stop-sharing preview / POST, ``POST /api/me/characters/{id}/share`` (4.2)
* ``DELETE /api/characters/{id}``       remove it; its chats stay           (manage_characters)
* ``GET    /api/characters/{id}/delete-preview``  how many chats and profiles use it (counts only)
* ``PUT    /api/characters/order``      reorder / flags in bulk             (manage_characters)
* ``GET    /api/starter-packs``         the packs and what is installed     (manage_characters)
* ``POST   /api/starter-packs/{id}/install``                                 (manage_characters)
* ``GET    /api/starter-packs/{id}/pictures/{character_id}``  a pack character's shipped thumb
                                        (manage_characters; the Add-a-pack dialog shows it)

``/api/bots*`` (main.py) is the same operations under the old name for one more release; both call
:mod:`app.characters`, so they cannot drift.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from . import (
    characters,
    characters_manage,
    config,
    kits,
    ownership,
    personas,
    policy,
    profiles_service,
    starter_packs,
)
from .api_common import audit, caller, fail
from .contracts.permissions import Permission
from .models import AGENT_ID_PATTERN, BOT_ID_PATTERN, UpdateBotOrderIn
from .profiles_service import ProfileServiceError
from .ws import manager

log = logging.getLogger("local-chat.characters")

router = APIRouter()

_MANAGE = Depends(policy.require_permission(Permission.MANAGE_CHARACTERS))
_CHAT = Depends(policy.require_permission(Permission.USE_CHAT))


def _clean(v: str | None) -> str | None:
    return None if v is None else v.strip()


class CharacterCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str | None = Field(default=None, pattern=BOT_ID_PATTERN)
    name: str = Field(min_length=1, max_length=40)
    emoji: str = Field(default="", max_length=8)
    backend: Literal["direct", "agent"] = "direct"
    agent: str | None = Field(default=None, pattern=AGENT_ID_PATTERN)
    connection: str | None = Field(default=None, max_length=48)
    model: str | None = Field(default=None, max_length=200)
    model_hint: str = Field(default="", max_length=60)
    description: str = Field(default="", max_length=400)
    personality: str | None = Field(default=None, max_length=32)
    persona: str | None = Field(default=None, max_length=8000)
    color: str = Field(default="", max_length=32)
    voice: str = Field(default="", max_length=80)
    voice_rp: str = Field(default="", max_length=32)
    quick_actions: bool = True
    tools: bool | None = None
    hidden: bool = False                 # keep it off the rail; an explicit list on a profile still shows it

    @field_validator("name", "emoji", "model_hint")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()

    @model_validator(mode="after")
    def _shape(self) -> CharacterCreateIn:
        if not self.name:
            raise ValueError("name must not be blank")
        if self.backend == "direct" and self.agent:
            raise ValueError("agent is only for backend 'agent'")
        if self.backend == "agent" and (self.tools is not None or self.connection):
            raise ValueError("tools and connection are only for direct characters")
        if self.personality and self.persona:
            raise ValueError("send personality (a preset id) or persona (text), not both")
        return self


class CharacterPatchIn(BaseModel):
    """Present keys are set, absent ones left alone. An explicit null is refused, except
    ``connection``: null/"" detaches the character from its connection."""
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=40)
    emoji: str | None = Field(default=None, max_length=8)
    model_hint: str | None = Field(default=None, max_length=60)
    description: str | None = Field(default=None, max_length=400)
    quick_actions: bool | None = None
    reactions: bool | None = None
    reaction_autopilot: bool | None = None
    avatar_pool: bool | None = None
    image_jobs: bool | None = None
    tools: bool | None = None
    persona: str | None = Field(default=None, max_length=8000)
    personality: str | None = Field(default=None, max_length=32)
    connection: str | None = Field(default=None, max_length=48)
    model: str | None = Field(default=None, max_length=200)
    color: str | None = Field(default=None, max_length=32)
    agent: str | None = Field(default=None, pattern=AGENT_ID_PATTERN)
    voice: str | None = Field(default=None, max_length=80)
    voice_rp: str | None = Field(default=None, max_length=32)
    hidden: bool | None = None           # the Admin's "Hide"

    @field_validator("name", "emoji", "model_hint", "color")
    @classmethod
    def _strip(cls, v: str | None) -> str | None:
        return _clean(v)

    @model_validator(mode="after")
    def _something(self) -> CharacterPatchIn:
        if not self.model_fields_set:
            raise ValueError("nothing to change")
        if "name" in self.model_fields_set and not self.name:
            raise ValueError("name must not be blank")
        for k in self.model_fields_set - {"connection"}:
            if getattr(self, k) is None:
                raise ValueError(f"{k} must not be null")
        if "personality" in self.model_fields_set and "persona" in self.model_fields_set:
            raise ValueError("send personality (a preset id) or persona (text), not both")
        return self


class GiveToIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    give_to: str | list[str]
    token: str = Field(min_length=1, max_length=2048)


class TokenIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=1, max_length=2048)


class StarterPackInstallIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connection: str | None = Field(default=None, max_length=48)
    model: str | None = Field(default=None, max_length=200)
    only: list[str] | None = Field(default=None, min_length=1, max_length=64)


def _err(e: characters.CharacterError | starter_packs.PackError | ProfileServiceError) -> Any:
    return fail(e.status, e.code, str(e))


async def broadcast_roster() -> None:
    """Tell every open tab the roster changed (the WS redactor narrows it per connection), and
    sync the kits: a kitted household bot's copies follow its edits (app/kits.py)."""
    await manager.broadcast({"type": "bots", "bots": [b.to_dict() for b in config.load_bots()]})
    kits.request_sync()


@router.get("/api/characters", dependencies=[_CHAT])
async def list_characters(request: Request):
    pr = caller(request)
    bots = await asyncio.to_thread(config.load_bots)
    # ``managed``: a household bot with a kit (4.1), which My bots offers even when hidden.
    return {"characters": [{**b.to_dict(), "managed": kits.managed(b)}
                           for b in characters.usable_bots(pr, bots)]}


def _household(bots: list[config.Bot]) -> list[config.Bot]:
    """The Admin's surface lists household characters only: a profile's own bots are private
    to it, Admins included (BUILD-PLAN-v2 §1.2)."""
    return [b for b in bots if b.scope == "household"]


@router.get("/api/characters/manage", dependencies=[_CHAT])
async def manage_characters(request: Request):
    """The Characters page (4.2): ``{me, profiles, mine, household}``, every row from one
    serializer (``characters_manage.manage_row``). Templates (Household) are for Admins and are
    fetched here - the ``bots`` frame never carries them."""
    return await asyncio.to_thread(characters_manage.manage_list, caller(request))


@router.get("/api/characters/all", dependencies=[_MANAGE])
async def list_all_characters():
    bots = await asyncio.to_thread(config.load_bots)
    return {"characters": [characters.character_out(b) for b in _household(bots)]}


@router.get("/api/characters/personas", dependencies=[_MANAGE])
async def personality_presets():
    return personas.public()


@router.put("/api/characters/order", dependencies=[_MANAGE])
async def put_order(payload: UpdateBotOrderIn):
    known = {b.id for b in _household(await asyncio.to_thread(config.load_bots))}
    for item in payload.bots:
        if item.id not in known:         # a personal bot is as good as absent for an Admin
            raise fail(404, "bot_not_found", "Unknown bot")
    try:
        for item in payload.bots:
            for fld in ("voice", "voice_rp"):
                value = getattr(item, fld, None)
                if value is not None:
                    await asyncio.to_thread(characters.check_extension_field, fld, item.id, value.strip())
    except characters.CharacterError as e:
        raise _err(e) from e
    bots = await asyncio.to_thread(config.save_bot_order, [i.model_dump() for i in payload.bots])
    await manager.broadcast({"type": "bots", "bots": [b.to_dict() for b in bots]})
    kits.request_sync()
    return {"characters": [characters.character_out(b) for b in _household(bots)]}


@router.get("/api/characters/{character_id}", dependencies=[_MANAGE])
async def get_character(character_id: str):
    b = await asyncio.to_thread(config.get_bot, character_id)
    if b is None or b.scope != "household":
        raise fail(404, "bot_not_found", "Unknown bot")
    return characters.character_out(b)


@router.get("/api/characters/{character_id}/delete-preview", dependencies=[_MANAGE])
async def character_delete_preview(character_id: str):
    """What deleting a household character leaves behind, as numbers: the chats people have
    with it (they stay, but can no longer be continued) and how many profiles they belong to.
    Copies people made of it are their own bots and are not touched."""
    b = await asyncio.to_thread(config.get_bot, character_id)
    if b is None or b.scope != "household":
        raise fail(404, "bot_not_found", "Unknown bot")
    return await asyncio.to_thread(profiles_service.household_bot_usage, character_id)


def _give_creator(request: Request, bot_ids: list[str]) -> None:
    """A template an Admin just made is theirs to use: it is given to the creator (their own copy,
    chats and all), nobody else. Others get it through give-to. Never fails the create."""
    pr = caller(request)
    pid = getattr(pr, "profile_id", None)
    if getattr(pr, "kind", "") != "profile" or not pid:
        return
    for bid in bot_ids:
        try:
            profiles_service.set_give_to(bid, [pid])
        except Exception:
            log.warning("could not give %s to its creator", bid, exc_info=True)


@router.post("/api/characters", status_code=201, dependencies=[_MANAGE])
async def create_character(request: Request, payload: CharacterCreateIn):
    body = payload.model_dump(exclude={"personality"}, exclude_none=True)
    if payload.personality:
        preset = next((p for p in personas.presets() if p.id == payload.personality), None)
        if preset is None:
            raise fail(422, "character_invalid", f"Unknown personality {payload.personality!r}")
        body["persona"] = "" if preset.id == personas.DEFAULT_ID else preset.text
    if body.get("backend", "direct") != "direct":
        # R1: an agent runs on this computer, so only the protected Admin may make one (it is his
        # personal bot). Any other Admin would create a bot nobody can see or delete.
        pid = getattr(caller(request), "profile_id", None)
        owner = ownership.protected_admin_id()
        if owner and pid != owner:
            raise fail(403, "agent_create_owner_only",
                       "Only the Owner can make a bot that runs an agent on the DisPatch computer.")
    if body.get("backend", "direct") == "direct":
        body["give_to"] = []                    # 4.2: a household bot is a template, never shared
    try:
        saved = await asyncio.to_thread(characters.create, **body)
    except characters.CharacterError as e:
        raise _err(e) from e
    if saved.is_template:
        await asyncio.to_thread(_give_creator, request, [saved.id])
    audit(request, "character.create", saved.id)
    await broadcast_roster()
    return JSONResponse(characters.character_out(saved), status_code=201)


@router.patch("/api/characters/{character_id}", dependencies=[_CHAT])
async def patch_character(request: Request, character_id: str, payload: CharacterPatchIn):
    """The one edit route (4.2): a template (``manage_characters``), an own bot or a copy (its
    owner, ``create_characters``; a copy stores overrides) or the Owner's agent bot. The allowlist
    is per kind and server-side (``characters_manage``); answers 404 for a bot that is not the
    caller's, 403 without permission, 422 for a field the kind does not take."""
    changes = payload.model_dump(exclude_unset=True)
    pr = caller(request)
    try:
        saved = await asyncio.to_thread(characters_manage.patch, pr, character_id, changes)
    except ProfileServiceError as e:
        raise _err(e) from e
    audit(request, "character.update", character_id, ",".join(sorted(changes))[:200])
    await broadcast_roster()
    row = await asyncio.to_thread(characters_manage.manage_row, saved, pr)
    return {**characters.character_out(saved), "row": row}


def _parse_give_to(raw: str) -> str | list:
    try:
        return json.loads(raw)
    except ValueError:
        return raw                                   # a bare ``everyone``


@router.get("/api/characters/{character_id}/give-to/preview", dependencies=[_MANAGE])
async def give_to_preview(request: Request, character_id: str, give_to: str = Query(max_length=4000)):
    """What ticking exactly ``give_to`` (JSON: ``"everyone"`` or ``["<profile id>", ...]``) does,
    as profile ids: ``add``, ``remove`` (a copy nobody wrote in), ``detach`` (a copy with chats,
    kept as the owner's own bot), and a ``token`` the PUT must carry."""
    try:
        return await asyncio.to_thread(characters_manage.give_to_preview, caller(request),
                                       character_id, _parse_give_to(give_to))
    except ProfileServiceError as e:
        raise _err(e) from e


@router.put("/api/characters/{character_id}/give-to", dependencies=[_MANAGE])
async def put_give_to(request: Request, character_id: str, payload: GiveToIn):
    try:
        row = await asyncio.to_thread(characters_manage.set_give_to, caller(request), character_id,
                                      payload.give_to, payload.token)
    except ProfileServiceError as e:
        raise _err(e) from e
    audit(request, "character.give_to", character_id,
          "everyone" if payload.give_to == "everyone" else f"count={len(payload.give_to)}")
    await broadcast_roster()
    return row


@router.post("/api/characters/{character_id}/stop-sharing/preview", dependencies=[_MANAGE])
async def stop_sharing_preview(request: Request, character_id: str):
    try:
        return await asyncio.to_thread(characters_manage.stop_sharing_preview, caller(request),
                                       character_id)
    except ProfileServiceError as e:
        raise _err(e) from e


@router.post("/api/characters/{character_id}/stop-sharing", dependencies=[_MANAGE])
async def stop_sharing(request: Request, character_id: str, payload: TokenIn):
    try:
        out = await asyncio.to_thread(characters_manage.stop_sharing, caller(request), character_id,
                                      payload.token)
    except ProfileServiceError as e:
        raise _err(e) from e
    audit(request, "character.stop_sharing", character_id)
    await broadcast_roster()
    return out


@router.post("/api/me/characters/{bot_id}/share", dependencies=[_MANAGE])
async def share_character(request: Request, bot_id: str):
    """An Admin shares one of its own direct bots with the household: a new template, and the
    original becomes the caller's linked copy of it."""
    try:
        out = await asyncio.to_thread(characters_manage.share, caller(request), bot_id)
    except ProfileServiceError as e:
        raise _err(e) from e
    audit(request, "character.share", bot_id, f"template={out['template']['id']}")
    await broadcast_roster()
    return out


@router.delete("/api/characters/{character_id}", dependencies=[_MANAGE])
async def delete_character(request: Request, character_id: str):
    """Delete a template (copies detach), or - the Owner only - one of his own protected bots
    (agent / app bot, Sage) on purpose, with its chats. The call itself is the confirmation
    (the page asks first); anyone else's personal bot is 404."""
    pid = getattr(caller(request), "profile_id", None)
    b = await asyncio.to_thread(config.get_bot, character_id)
    if (b is not None and b.scope == "personal" and pid and pid == ownership.protected_admin_id()
            and b.owner_profile == pid and characters.is_protected_personal(b)):
        try:
            gone = await asyncio.to_thread(profiles_service.delete_personal, pid, b.id, deliberate=True)
        except ProfileServiceError as e:
            raise _err(e) from e
        audit(request, "character.delete", character_id, f"chats={gone.get('chats', 0)}")
        await broadcast_roster()
        return {"removed": character_id, "deleted": gone}
    try:
        await asyncio.to_thread(characters.remove, character_id)
    except characters.CharacterError as e:
        raise _err(e) from e
    audit(request, "character.delete", character_id)
    await broadcast_roster()
    return {"removed": character_id}


@router.get("/api/starter-packs", dependencies=[_MANAGE])
async def list_starter_packs():
    return {"packs": await asyncio.to_thread(starter_packs.list_packs)}


@router.get("/api/starter-packs/{pack_id}/pictures/{character_id}", dependencies=[_MANAGE])
async def starter_pack_picture(pack_id: str, character_id: str):
    """The small picture a pack ships for one of its characters (the thumb, else the face).
    Only a character of that pack, from the pack's own folder: never a path the caller names."""
    try:
        pack = starter_packs.get_pack(pack_id)
    except starter_packs.PackError as e:
        raise _err(e) from e
    if character_id not in {c.id for c in pack["characters"]}:
        raise fail(404, "starter_pack_not_found", "The pack has no such character")
    files = starter_packs.avatar_files(character_id)
    path = files.get("thumb") or files.get("face")
    if path is None:
        raise fail(404, "starter_pack_not_found", "This character has no picture")
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "private, max-age=86400"})


@router.post("/api/starter-packs/{pack_id}/install", dependencies=[_MANAGE])
async def install_starter_pack(request: Request, pack_id: str, payload: StarterPackInstallIn | None = None):
    p = payload or StarterPackInstallIn()
    try:
        out = await asyncio.to_thread(
            starter_packs.install, pack_id, connection=p.connection, model=p.model,
            only=p.only)
    except (starter_packs.PackError, characters.CharacterError) as e:
        raise _err(e) from e
    await asyncio.to_thread(_give_creator, request, out["installed"])
    audit(request, "starter_pack.install", pack_id, ",".join(out["installed"])[:200])
    if out["installed"]:
        await broadcast_roster()
    return out
