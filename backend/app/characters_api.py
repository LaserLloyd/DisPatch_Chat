"""``/api/characters`` and ``/api/starter-packs`` - the 3.0 character API.

* ``GET    /api/characters``            the characters this caller may talk to (public fields)
* ``GET    /api/characters/all``        every character with its settings   (manage_characters)
* ``GET    /api/characters/personas``   the personality presets             (manage_characters)
* ``GET    /api/characters/{id}``       one character, in full              (manage_characters)
* ``POST   /api/characters``            add one                             (manage_characters)
* ``PATCH  /api/characters/{id}``       change it - audience, profiles, connection, model,
                                        personality, voice, flags...        (manage_characters)
* ``DELETE /api/characters/{id}``       remove it; its chats stay           (manage_characters)
* ``PUT    /api/characters/order``      reorder / flags in bulk             (manage_characters)
* ``GET    /api/starter-packs``         the packs and what is installed     (manage_characters)
* ``POST   /api/starter-packs/{id}/install``                                 (manage_characters)

``/api/bots*`` (main.py) is the same operations under the old name for one more release; both call
:mod:`app.characters`, so they cannot drift.
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from . import characters, config, personas, policy, starter_packs
from .api_common import audit, caller, fail
from .contracts.permissions import Permission
from .models import AGENT_ID_PATTERN, BOT_ID_PATTERN, UpdateBotOrderIn
from .ws import manager

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
    audience: Literal["all", "members", "restricted"] | None = None
    profiles: list[str] | None = None
    description: str = Field(default="", max_length=400)
    personality: str | None = Field(default=None, max_length=32)
    persona: str | None = Field(default=None, max_length=8000)
    color: str = Field(default="", max_length=32)
    voice: str = Field(default="", max_length=80)
    voice_rp: str = Field(default="", max_length=32)
    quick_actions: bool = True
    tools: bool | None = None

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
    visible: bool | None = None
    safe: bool | None = None
    hide_unlocked: bool | None = None
    audience: Literal["all", "members", "restricted"] | None = None
    profiles: list[str] | None = None
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


class StarterPackInstallIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connection: str | None = Field(default=None, max_length=48)
    model: str | None = Field(default=None, max_length=200)
    only: list[str] | None = None
    profiles: list[str] | None = None


def _err(e: characters.CharacterError | starter_packs.PackError) -> Any:
    return fail(e.status, e.code, str(e))


async def broadcast_roster() -> None:
    """Tell every open tab the roster changed (the WS redactor narrows it per connection)."""
    await manager.broadcast({"type": "bots", "bots": [b.to_dict() for b in config.load_bots()]})


@router.get("/api/characters", dependencies=[_CHAT])
async def list_characters(request: Request):
    pr = caller(request)
    bots = await asyncio.to_thread(config.load_bots)
    return {"characters": [b.to_dict() for b in characters.visible_to(pr, bots)]}


@router.get("/api/characters/all", dependencies=[_MANAGE])
async def list_all_characters():
    bots = await asyncio.to_thread(config.load_bots)
    return {"characters": [characters.character_out(b) for b in bots]}


@router.get("/api/characters/personas", dependencies=[_MANAGE])
async def personality_presets():
    return personas.public()


@router.put("/api/characters/order", dependencies=[_MANAGE])
async def put_order(payload: UpdateBotOrderIn):
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
    return {"characters": [characters.character_out(b) for b in bots]}


@router.get("/api/characters/{character_id}", dependencies=[_MANAGE])
async def get_character(character_id: str):
    b = await asyncio.to_thread(config.get_bot, character_id)
    if b is None:
        raise fail(404, "bot_not_found", "Unknown bot")
    return characters.character_out(b)


@router.post("/api/characters", status_code=201, dependencies=[_MANAGE])
async def create_character(request: Request, payload: CharacterCreateIn):
    body = payload.model_dump(exclude={"personality"}, exclude_none=True)
    if payload.personality:
        preset = next((p for p in personas.presets() if p.id == payload.personality), None)
        if preset is None:
            raise fail(422, "character_invalid", f"Unknown personality {payload.personality!r}")
        body["persona"] = "" if preset.id == personas.DEFAULT_ID else preset.text
    try:
        saved = await asyncio.to_thread(characters.create, **body)
    except characters.CharacterError as e:
        raise _err(e) from e
    audit(request, "character.create", saved.id)
    await broadcast_roster()
    return JSONResponse(characters.character_out(saved), status_code=201)


@router.patch("/api/characters/{character_id}", dependencies=[_MANAGE])
async def patch_character(request: Request, character_id: str, payload: CharacterPatchIn):
    changes = payload.model_dump(exclude_unset=True)
    try:
        saved = await asyncio.to_thread(characters.patch, character_id, changes)
    except characters.CharacterError as e:
        raise _err(e) from e
    audit(request, "character.update", character_id, ",".join(sorted(changes))[:200])
    await broadcast_roster()
    return characters.character_out(saved)


@router.delete("/api/characters/{character_id}", dependencies=[_MANAGE])
async def delete_character(request: Request, character_id: str):
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


@router.post("/api/starter-packs/{pack_id}/install", dependencies=[_MANAGE])
async def install_starter_pack(request: Request, pack_id: str, payload: StarterPackInstallIn | None = None):
    p = payload or StarterPackInstallIn()
    try:
        out = await asyncio.to_thread(
            starter_packs.install, pack_id, connection=p.connection, model=p.model,
            only=p.only, profiles=p.profiles)
    except (starter_packs.PackError, characters.CharacterError) as e:
        raise _err(e) from e
    audit(request, "starter_pack.install", pack_id, ",".join(out["installed"])[:200])
    if out["installed"]:
        await broadcast_roster()
    return out
