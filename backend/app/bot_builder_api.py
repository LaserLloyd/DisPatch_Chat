"""AI build: draft a personal bot from a description (4.0 chat-first profiles).

* ``POST /api/me/characters/draft``   body ``{"description": "..."}``  (``create_characters``)
  -> ``{"draft": {"name", "emoji", "description", "persona"}}``

The draft is returned for the person to review in the editor; nothing is saved here, and
the description and the draft are never logged or written to the audit trail. The model is
reached through the household's direct-API connection (the Atlas bot's provider chain, or
the first household direct bot's), narrowed to the connections the caller's profile may
choose (``backends_policy.allowed_backends``). The meta-prompt and the parse/repair logic
are in :mod:`app.bot_builder`.
"""

from __future__ import annotations

import dataclasses
import logging

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import backends_policy, bot_builder, config, llm_api, policy
from .api_common import caller, problem
from .contracts.permissions import Permission

log = logging.getLogger("dispatch.bot_builder")

router = APIRouter()

_CREATE = Depends(policy.require_permission(Permission.CREATE_CHARACTERS))


class DraftIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str = Field(min_length=1, max_length=bot_builder.MAX_DESCRIPTION_IN)

    @field_validator("description")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("description must not be blank")
        return v


def _allowed_connections(principal) -> list[str] | None:
    """The connection ids the caller's personal bots may use; ``None`` = every household one."""
    profile = getattr(principal, "profile", None)
    if profile is None:
        return None
    return backends_policy.allowed_backends(profile)


def provider_chain(principal) -> list[dict]:
    """The provider entries a draft may use, in order of preference."""
    bots = [b for b in config.load_bots()
            if b.scope == "household" and (b.advisor or {}).get("providers")]
    bots.sort(key=lambda b: b.id.lower() != "atlas")
    allowed = _allowed_connections(principal)
    chain: list[dict] = []
    for bot in bots:
        for entry in bot.advisor["providers"]:
            if not isinstance(entry, dict):
                continue
            if allowed is not None and str(entry.get("connection") or "") not in allowed:
                continue
            chain.append(entry)
        if chain:
            break
    return chain


async def provider_complete(chain: list[dict], system: str, user: str) -> str:
    """One completion through the first provider of ``chain`` that answers."""
    last: llm_api.ApiError | None = None
    for entry in chain:
        try:
            cfg = llm_api.resolve_api(entry, "AI build", "ai-build")
            cfg = dataclasses.replace(cfg, system_prompt=system)
            reply = await llm_api.complete(cfg, [{"role": "user", "content": user}])
        except llm_api.ApiError as e:
            last = e
            continue
        if reply.text.strip():
            return reply.text
    raise last or llm_api.ApiError("No AI connection answered", "")


@router.post("/api/me/characters/draft", dependencies=[_CREATE])
async def draft_character(body: DraftIn, request: Request):
    chain = provider_chain(caller(request))
    if not chain:
        return problem(503, "draft_unavailable",
                       "No AI connection is available for building a bot.")

    async def complete(system: str, user: str) -> str:
        return await provider_complete(chain, system, user)

    try:
        draft = await bot_builder.build_draft(body.description, complete)
    except bot_builder.DraftFailed as e:
        # Only the validation problem is logged, never the description or the reply.
        log.info("AI build draft unusable: %s", e.problem)
        return problem(502, "draft_failed",
                       "The AI did not return a usable bot. Try again, or add more detail.")
    except llm_api.ApiError as e:
        log.info("AI build provider failed: %s", e.message)
        return problem(502, "draft_failed", f"The AI could not be reached: {e.message}")
    return {"draft": draft.to_dict()}
