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

from . import policy, settings_schema, settings_store, tool_manifests
from .api_common import actor, audit, fail
from .contracts.permissions import Permission

router = APIRouter()

_MANAGE = Depends(policy.require_permission(Permission.MANAGE_SETTINGS))


def _with_plugins() -> None:
    """Make sure every plugin's settings fragment (tools.<id>.*) is in the schema before it is read."""
    tool_manifests.register_fragments()


@router.get("/api/settings/schema", dependencies=[_MANAGE])
async def settings_schema_get():
    _with_plugins()
    return settings_schema.schema_document(settings_schema.all_defs())


@router.get("/api/settings", dependencies=[_MANAGE])
async def settings_get(group: str | None = Query(None), level: str | None = Query(None),
                       q: str | None = Query(None, max_length=100)):
    if group and group not in settings_schema.GROUP_IDS:
        raise fail(422, "settings_invalid", "Unknown settings group")
    if level and level not in ("basic", "advanced"):
        raise fail(422, "settings_invalid", "level must be basic or advanced")
    _with_plugins()
    return settings_store.values_document(group=group, level=level, q=q)


@router.patch("/api/settings", dependencies=[_MANAGE])
async def settings_patch(request: Request, payload: dict[str, Any] = Body(...)):
    unknown_top = set(payload) - {"set", "reset"}
    if unknown_top:
        raise fail(422, "settings_invalid", "Send {\"set\": {key: value}, \"reset\": [key]}")
    _with_plugins()
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
    is_owner = policy.of(request).can(Permission.ADMIN_SYSTEM)
    if not is_owner:
        locked = [k for k in {*changes, *reset}
                  if (d := settings_schema.get_def(k)) is not None and d.owner_only]
        if locked:
            raise fail(403, "permission_denied",
                       f"Only the Owner or an Admin can change {settings_schema.get_def(sorted(locked)[0]).label}.")
    res = settings_store.set_many(changes, actor=actor(request), reset=reset, owner=is_owner)
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
