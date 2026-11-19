"""``GET /api/image-spend``: today's picture spending against the daily caps (Settings).

Owner and Admins (``manage_settings``, the permission that edits the caps). Profiles are named,
never keyed by anything a page could pass in; no key, URL or prompt text is returned."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from . import accounts, image_spend, policy
from .contracts.permissions import Permission

router = APIRouter()


@router.get("/api/image-spend", dependencies=[Depends(policy.require_permission(Permission.MANAGE_SETTINGS))])
async def image_spend_get():
    import asyncio

    doc = await asyncio.to_thread(image_spend.usage_all)
    names = {p.id: p.name for p in accounts.list_profiles()}
    for row in doc["profiles"]:
        row["name"] = names.get(row["profile_id"], "")
    return doc
