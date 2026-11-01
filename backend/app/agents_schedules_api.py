"""HTTP API for agent schedules and CLI presets (4.3, #13).

``/api/agents/schedules`` (``dispatch_agents``)
    A profile lists, creates, changes and deletes its OWN schedules. Somebody else's schedule
    answers 404, exactly like one that does not exist - except for an Admin holding
    ``manage_agents``, who sees every schedule (``?scope=all``) and may switch another
    profile's schedule OFF or delete it, but never change its prompt, agent, chat or rule and
    never switch it back on: no profile acts as another. Another profile's prompt and chat are
    not shown to the Admin either (``scheduler.describe``).

    Bounds: ``every_minutes`` >= 15, at most 20 schedules per profile; the chat, when given,
    must be the caller's own and its character one the caller may use (checked here and again
    at every fire). The agent must be one the caller may use (``agents_api._usable_agent``).

``/api/agents/cli-presets`` (``manage_agents`` + host tier: the PROTECTED Admin only)
    The command lines the generic CLI backend may run (``cli_adapter.validate_presets``). A
    preset is host code, so the handler checks ``is_protected`` itself as well: that holds
    before WP-A14 makes the gate enforce ``host=True``.

The router is mounted by WP-INT (``app.include_router(agents_schedules_api.router)``).
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from . import accounts, agents_service, characters, ownership, policy
from . import agents_store as store
from .agents import cli_adapter, schedule_store, scheduler
from .agents_api import _usable_agent
from .contracts import errors
from .contracts.permissions import Permission

router = APIRouter()

_DISPATCH = Depends(policy.require_permission(Permission.DISPATCH_AGENTS))
_MANAGE = Depends(policy.require_permission(Permission.MANAGE_AGENTS))

PROMPT_MAX = 20_000


def _problem(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"detail": message, "error": errors.envelope(code, message=message)},
                        status_code=status)


def _not_found() -> HTTPException:
    exc = HTTPException(404, "Unknown schedule.")
    exc.code = "schedule_not_found"                 # type: ignore[attr-defined]
    return exc


def _principal(request: Request) -> policy.Principal:
    return policy.of(request).principal


def _me(pr: policy.Principal) -> str:
    return agents_service.principal_key(pr)


def _mgr() -> agents_service.TaskManager | None:
    try:
        return agents_service.get_manager()
    except RuntimeError:
        return None


# --------------------------------------------------------------------------- #
# Schedules
# --------------------------------------------------------------------------- #


class ScheduleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str = Field(min_length=1, max_length=80)
    prompt: str = Field(min_length=1, max_length=PROMPT_MAX)
    rule: dict[str, Any]
    tz: str = Field(default="UTC", min_length=1, max_length=64)
    thread_id: str | None = Field(default=None, max_length=120)
    enabled: bool = True


class SchedulePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str | None = Field(default=None, min_length=1, max_length=80)
    prompt: str | None = Field(default=None, min_length=1, max_length=PROMPT_MAX)
    rule: dict[str, Any] | None = None
    tz: str | None = Field(default=None, min_length=1, max_length=64)
    thread_id: str | None = Field(default=None, max_length=120)
    enabled: bool | None = None


def _visible(pr: policy.Principal, sid: str) -> tuple[dict[str, Any], bool]:
    """The row and whether the caller owns it; 404 for a row the caller may not see."""
    row = schedule_store.get(sid)
    if row is None:
        raise _not_found()
    own = row["owner_profile"] == _me(pr)
    if not own and not pr.can(Permission.MANAGE_AGENTS):
        raise _not_found()
    if not own and not pr.is_protected and row["owner_profile"] == ownership.protected_admin_id():
        # Admin = every Owner permission except acting on THE Owner profile (4.3 review): another
        # Admin may not switch off or delete the Owner's schedules.
        raise _not_found()
    return row, own


async def _check_thread(pr: policy.Principal, thread_id: str) -> None:
    """The chat must exist, be the caller's own, and its character one the caller may use
    (the same three checks ``POST /api/tasks`` makes for ``origin_thread_id``)."""
    mgr = _mgr()
    info = None
    if mgr is not None and mgr.hooks.thread_info is not None:
        info = await mgr.hooks.thread_info(thread_id)
    if info is None or not ownership.may_see_thread(pr, info.get("owner_profile_id"), write=True) \
            or not characters.may_use_id(info.get("bot_id"), pr):
        raise HTTPException(404, "Thread not found")


def _check_rule(rule: dict[str, Any], tz: str) -> dict[str, Any]:
    norm = scheduler.parse_rule(rule)
    scheduler.zone(tz)
    return norm


def _next(rule: dict[str, Any], tz: str) -> str:
    return scheduler.utc_iso(scheduler.next_after(rule, tz, datetime.now(UTC)))


@router.get("/api/agents/schedules")
async def list_schedules(request: Request, scope: str = "mine", _=_DISPATCH):
    pr = _principal(request)
    me = _me(pr)
    if scope == "all" and pr.can(Permission.MANAGE_AGENTS):
        rows = await asyncio.to_thread(schedule_store.list_all)
    else:
        rows = await asyncio.to_thread(schedule_store.list_all, owner=me)
    return {"schedules": [scheduler.describe(r, owner_view=r["owner_profile"] == me) for r in rows],
            "limits": {"every_minutes_min": scheduler.MIN_EVERY_MINUTES,
                       "per_profile": scheduler.MAX_PER_PROFILE}}


@router.post("/api/agents/schedules", status_code=201)
async def create_schedule(body: ScheduleIn, request: Request, _=_DISPATCH):
    pr = _principal(request)
    me = _me(pr)
    if not me:
        return _problem(403, "permission_denied", "Sign in to a profile to schedule a task.")
    _usable_agent(pr, body.agent_id)
    try:
        rule = _check_rule(body.rule, body.tz)
    except scheduler.RuleError as e:
        return _problem(422, "schedule_invalid", str(e))
    if body.thread_id:
        await _check_thread(pr, body.thread_id)
    if schedule_store.count_for(me) >= scheduler.MAX_PER_PROFILE:
        return _problem(409, "schedule_limit",
                        f"A profile can have at most {scheduler.MAX_PER_PROFILE} schedules.")
    row = schedule_store.create(
        agent_id=body.agent_id, owner_profile=me, prompt=body.prompt, rule=rule, tz=body.tz.strip(),
        thread_id=body.thread_id or None, enabled=body.enabled,
        next_run=_next(rule, body.tz) if body.enabled else None)
    accounts.log_activity("agent_schedule_created", profile_id=pr.profile_id, subject=row["id"],
                          detail=f"agent={body.agent_id}", category="audit")
    return {"schedule": scheduler.describe(row, owner_view=True)}


@router.patch("/api/agents/schedules/{schedule_id}")
async def patch_schedule(schedule_id: str, body: SchedulePatch, request: Request, _=_DISPATCH):
    pr = _principal(request)
    row, own = _visible(pr, schedule_id)
    changes = body.model_dump(exclude_unset=True)
    nulls = sorted(k for k in ("agent_id", "prompt", "rule", "tz", "enabled") if k in changes and changes[k] is None)
    if nulls:
        # An explicit null would hit a NOT NULL column (500) or quietly switch the schedule off.
        return _problem(422, "schedule_invalid", f"{', '.join(nulls)}: a value is required (null is not allowed)")
    if not own:
        # An Admin may switch another profile's schedule off - nothing else (no acting as them).
        if set(changes) != {"enabled"} or changes["enabled"] is not False:
            return _problem(403, "schedule_owner_only",
                            "Only the schedule's own profile can change it; an Admin may only "
                            "switch it off or delete it.")
        new = schedule_store.update(schedule_id, enabled=False, next_run=None, status="disabled",
                                    status_detail="Switched off by an Admin.")
        accounts.log_activity("agent_schedule_disabled", profile_id=pr.profile_id, subject=schedule_id,
                              detail=f"owner={row['owner_profile']}", category="audit")
        return {"schedule": scheduler.describe(new or row, owner_view=False)}
    agent_id = changes.get("agent_id") or row["agent_id"]
    if "agent_id" in changes or changes.get("enabled") is True:
        _usable_agent(pr, agent_id)
    rule = changes.get("rule", row["rule"])
    tz = changes.get("tz", row["tz"])
    try:
        rule = _check_rule(rule, tz)
    except scheduler.RuleError as e:
        return _problem(422, "schedule_invalid", str(e))
    if changes.get("thread_id"):
        await _check_thread(pr, changes["thread_id"])
    elif changes.get("enabled") is True and row["thread_id"] and "thread_id" not in changes:
        await _check_thread(pr, row["thread_id"])
    fields: dict[str, Any] = {k: v for k, v in changes.items() if k in ("agent_id", "prompt", "enabled")}
    fields.update(rule=rule, tz=tz.strip())
    if "thread_id" in changes:
        fields["thread_id"] = changes["thread_id"] or None
    enabled = changes.get("enabled", row["enabled"])
    timing = {"rule", "tz", "enabled"} & set(changes)
    if not enabled:
        fields["next_run"] = None
    elif timing or row["next_run"] is None:
        fields["next_run"] = _next(rule, tz)
    if changes.get("enabled") is True:
        fields.update(status="ok", status_detail="")
    new = schedule_store.update(schedule_id, **fields)
    accounts.log_activity("agent_schedule_changed", profile_id=pr.profile_id, subject=schedule_id,
                          detail=",".join(sorted(changes)), category="audit")
    return {"schedule": scheduler.describe(new or row, owner_view=True)}


@router.delete("/api/agents/schedules/{schedule_id}")
async def delete_schedule(schedule_id: str, request: Request, _=_DISPATCH):
    pr = _principal(request)
    row, own = _visible(pr, schedule_id)
    schedule_store.delete(schedule_id)
    accounts.log_activity("agent_schedule_deleted", profile_id=pr.profile_id, subject=schedule_id,
                          detail="" if own else f"owner={row['owner_profile']}", category="audit")
    return {"deleted": schedule_id}


# --------------------------------------------------------------------------- #
# CLI presets (host code: the protected Admin only)
# --------------------------------------------------------------------------- #


class CliPresetsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    presets: list[dict[str, Any]] = Field(max_length=cli_adapter.MAX_PRESETS)


def _require_protected(pr: policy.Principal) -> None:
    if not pr.is_protected:
        exc = HTTPException(403, "Only the Owner can do this, because it runs programs or reads "
                                 "files on the DisPatch computer.")
        exc.code = "host_owner_only"                # type: ignore[attr-defined]
        raise exc


@router.get("/api/agents/cli-presets")
async def get_cli_presets(request: Request, _=_MANAGE):
    _require_protected(_principal(request))
    return {"presets": store.get_setting("agents.cli.presets") or []}


@router.put("/api/agents/cli-presets")
async def put_cli_presets(body: CliPresetsIn, request: Request, _=_MANAGE):
    pr = _principal(request)
    _require_protected(pr)
    try:
        clean = cli_adapter.validate_presets(body.presets)
    except ValueError as e:
        return _problem(422, "cli_preset_invalid", str(e))
    store.set_setting("agents.cli.presets", clean)
    accounts.log_activity("agent_cli_presets", profile_id=pr.profile_id, subject="agents",
                          detail=",".join(p["id"] for p in clean), category="audit")
    return {"presets": clean}
