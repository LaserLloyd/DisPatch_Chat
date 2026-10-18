"""``/api/settings`` - read the schema, read and change values.

* ``GET  /api/settings/schema``    the versioned schema (groups, keys, types, ranges, defaults)
* ``GET  /api/settings``           every value with its ``source`` (env | store | default), the
                                   restart-pending list and the keys carried into ``settings_unknown``
* ``PATCH /api/settings``          ``{"set": {key: value}, "reset": [key]}`` - validated per key,
                                   all-or-nothing, refused for a key an environment variable sets
* ``GET  /api/settings/defaults``  the few values a device needs before anybody has signed in
                                   (default theme / language / avatar style); public on purpose

All but the last declare ``manage_settings`` in ``route_policy.py``.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, Query, Request

from . import policy, settings_schema, settings_store
from .api_common import actor, audit, fail
from .contracts.permissions import Permission

router = APIRouter()

_MANAGE = Depends(policy.require_permission(Permission.MANAGE_SETTINGS))


@router.get("/api/settings/schema", dependencies=[_MANAGE])
async def settings_schema_get():
    return settings_schema.schema_document(settings_schema.all_defs())


@router.get("/api/settings", dependencies=[_MANAGE])
async def settings_get(group: str | None = Query(None), level: str | None = Query(None),
                       q: str | None = Query(None, max_length=100)):
    if group and group not in settings_schema.GROUP_IDS:
        raise fail(422, "settings_invalid", "Unknown settings group")
    if level and level not in ("basic", "advanced"):
        raise fail(422, "settings_invalid", "level must be basic or advanced")
    return settings_store.values_document(group=group, level=level, q=q)


@router.patch("/api/settings", dependencies=[_MANAGE])
async def settings_patch(request: Request, payload: dict[str, Any] = Body(...)):
    unknown_top = set(payload) - {"set", "reset"}
    if unknown_top:
        raise fail(422, "settings_invalid", "Send {\"set\": {key: value}, \"reset\": [key]}")
    changes = payload.get("set") or {}
    reset = payload.get("reset") or []
    if not isinstance(changes, dict) or not isinstance(reset, list) \
            or not all(isinstance(k, str) for k in reset):
        raise fail(422, "settings_invalid", "Send {\"set\": {key: value}, \"reset\": [key]}")
    if not changes and not reset:
        raise fail(422, "settings_invalid", "Nothing to change")
    overlap = set(changes) & set(reset)
    if overlap:
        raise fail(422, "settings_invalid", f"{sorted(overlap)[0]} cannot be set and reset together")
    res = settings_store.set_many(changes, actor=actor(request), reset=reset)
    if res.errors:
        from fastapi.responses import JSONResponse

        from .contracts import errors as _errors
        return JSONResponse(
            {"detail": "Some settings were not accepted; nothing was changed",
             "error": _errors.envelope("settings_invalid",
                                       message="Some settings were not accepted; nothing was changed"),
             "errors": res.errors}, status_code=422)
    audit(request, "settings.change", ",".join(res.changed)[:80])      # keys only, never values
    doc = settings_store.values_document()
    return {"ok": True, "changed": res.changed, "restart_required": res.restart_required,
            "values": {k: doc["values"][k] for k in res.changed if k in doc["values"]}}


@router.get("/api/settings/defaults")
async def settings_defaults():
    """What a brand-new device needs before it has signed in. No secrets, three values."""
    return {
        "theme": settings_store.get("appearance.default_theme"),
        "language": settings_store.get("appearance.default_language"),
        "avatar_style": settings_store.get("appearance.avatar_style"),
    }
