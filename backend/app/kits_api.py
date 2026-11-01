"""``/api/characters/{id}/kit`` - managed bots, the Admin's side (4.1, app/kits.py).

* ``GET    /api/characters/{id}/kit``       the kit of a household bot (or ``managed: false``):
                                            auto_clone, the chats, how many copies exist
* ``PUT    /api/characters/{id}/kit``       attach / save it: ``{auto_clone, chats: [{id?, title,
                                            body, audience, resend}]}`` (household chats; the
                                            shipped ones are not edited here)
* ``DELETE /api/characters/{id}/kit``       stop managing the copies (they keep what they have)
* ``POST   /api/characters/{id}/kit/push``  sync every copy now

``manage_characters`` (an Admin) for all four. Household bots only: a personal bot's id is
404 here, exactly like the other household routes. The copy count is a number; nothing here
names or reads another profile's bot or chats. Chat bodies are shown, never acted on (the
markers an assistant message may carry are neutralised when posted: ``kits.neutralise``).
"""

from __future__ import annotations

import asyncio
from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from . import config, kits, policy, profiles_service
from .api_common import audit, fail
from .contracts.permissions import Permission

router = APIRouter()

_MANAGE = Depends(policy.require_permission(Permission.MANAGE_CHARACTERS))


class KitChatIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str | None = Field(default=None, pattern=r"^c[0-9a-f]{8}$")
    title: str = Field(min_length=1, max_length=kits.MAX_TITLE)
    body: str = Field(min_length=1, max_length=kits.MAX_BODY)
    audience: Literal["everyone", "admins"] = "everyone"
    resend: bool = True


class KitIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: The 4.1 switch, kept for the old form: true = ``give_to: everyone``, false = stop handing
    #: it out. Absent leaves ``give_to`` alone (the Characters page sets it with ``give-to``).
    auto_clone: bool | None = None
    chats: list[KitChatIn] = Field(default_factory=list, max_length=kits.MAX_CHATS)


def _household(character_id: str) -> config.Bot:
    b = config.get_bot(character_id)
    if b is None or b.scope != "household":
        raise fail(404, "bot_not_found", "Unknown bot")
    return b


async def _push() -> dict:
    pair = kits.bound()
    if pair is None:
        kits.request_sync()
        return {"posted": 0}
    out = await kits.sync(*pair)
    return {"posted": len(out["chats"])}


@router.get("/api/characters/{character_id}/kit", dependencies=[_MANAGE])
async def get_kit(character_id: str):
    await asyncio.to_thread(_household, character_id)
    return await asyncio.to_thread(kits.kit_out, character_id)


@router.put("/api/characters/{character_id}/kit", dependencies=[_MANAGE])
async def put_kit(request: Request, character_id: str, payload: KitIn):
    bot = await asyncio.to_thread(_household, character_id)
    if not kits.kittable(bot):
        raise fail(422, "not_copyable", "This bot runs on an agent and cannot be copied.")
    for c in payload.chats:
        if not c.title.strip() or not c.body.strip():
            raise fail(422, "kit_chat_invalid", "A chat needs a title and some text.")
    await asyncio.to_thread(kits.save_kit, character_id, chats=[c.model_dump() for c in payload.chats])
    if payload.auto_clone is not None and not bot.template:
        # The 4.1 switch only ever meant "new profiles get a copy". It never takes a copy away:
        # off freezes an "everyone" template to today's profiles; only the give-to preview and
        # token remove or detach copies.
        await asyncio.to_thread(profiles_service.set_new_profile_default, character_id, payload.auto_clone)
    audit(request, "kit.update", character_id,
          f"auto_clone={payload.auto_clone if payload.auto_clone is None else int(payload.auto_clone)} "
          f"chats={len(payload.chats)}")
    pushed = await _push()
    return {**await asyncio.to_thread(kits.kit_out, character_id), **pushed}


@router.delete("/api/characters/{character_id}/kit", dependencies=[_MANAGE])
async def delete_kit(request: Request, character_id: str):
    await asyncio.to_thread(_household, character_id)
    if character_id in kits.SHIPPED:
        raise fail(409, "kit_shipped", "This bot's copies are managed by DisPatch itself.")
    gone = await asyncio.to_thread(kits.delete_household, character_id)
    if gone:
        audit(request, "kit.delete", character_id)
    return await asyncio.to_thread(kits.kit_out, character_id)


@router.post("/api/characters/{character_id}/kit/push", dependencies=[_MANAGE])
async def push_kit(request: Request, character_id: str):
    await asyncio.to_thread(_household, character_id)
    if kits.get_kit(character_id) is None:
        raise fail(404, "kit_not_found", "This bot has no copies to manage.")
    pushed = await _push()
    audit(request, "kit.push", character_id, f"posted={pushed['posted']}")
    return {**await asyncio.to_thread(kits.kit_out, character_id), **pushed}
