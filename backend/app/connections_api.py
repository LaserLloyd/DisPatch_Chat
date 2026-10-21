"""``/api/connections`` - the servers DisPatch talks to.

* ``GET    /api/connections``              every connection (never a key), the type table, who uses each
* ``GET    /api/connections/types``        the connection types and what each needs
* ``POST   /api/connections``              add one: ``{type, name?, base_url?, api_key? | api_key_env?, ...}``
* ``GET    /api/connections/{id}``         one connection
* ``PATCH  /api/connections/{id}``         rename, change the address / key / default model / capabilities
* ``DELETE /api/connections/{id}``         remove it (refused while a character uses it)
* ``POST   /api/connections/test``         Test a form that is not saved yet
* ``POST   /api/connections/{id}/test``    Test a saved connection; records the model list
* ``POST   /api/connections/discover``     find StudioForge / LM Studio / Ollama / OpenAI-style servers
* ``POST   /api/connections/adopt``        give every character's inline provider a connection now
                                           (the same step the 3.0 migration runs at start-up)

All of them are ``manage_connections``. A key is accepted on create/update and never returned;
``credential.has_key`` says whether one is stored. The "Connect an AI" routes (``/api/llm/*``)
are unchanged and keep writing inline provider settings; the next start (or ``/adopt``) turns those
into connections.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from . import characters, config, connections, policy
from .api_common import audit, fail
from .contracts.permissions import Permission

router = APIRouter()

_MANAGE = Depends(policy.require_permission(Permission.MANAGE_CONNECTIONS))


class ConnectionCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str = Field(max_length=32)
    id: str | None = Field(default=None, max_length=48)
    name: str = Field(default="", max_length=80)
    base_url: str = Field(default="", max_length=2048)
    api_key: str = Field(default="", max_length=4096)
    api_key_env: str = Field(default="", max_length=64)
    capabilities: list[str] | None = None
    default_model: str = Field(default="", max_length=200)


class ConnectionPatchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, max_length=80)
    base_url: str | None = Field(default=None, max_length=2048)
    api_key: str | None = Field(default=None, max_length=4096)
    api_key_env: str | None = Field(default=None, max_length=64)
    clear_key: bool | None = None
    capabilities: list[str] | None = None
    default_model: str | None = Field(default=None, max_length=200)
    required_permission: str | None = Field(default=None, max_length=64)


class ConnectionTestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str = Field(max_length=32)
    base_url: str = Field(default="", max_length=2048)
    api_key: str = Field(default="", max_length=4096)
    model: str = Field(default="", max_length=200)
    use_connection: str | None = Field(default=None, max_length=48)      # borrow that connection's key


class DiscoverIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hosts: list[str] = Field(default_factory=list, max_length=32)
    lan: bool = False


def _err(e: connections.ConnectionError_):
    return fail(e.status, e.code, str(e))


def _public(c: connections.Connection) -> dict:
    return c.public(connections.used_by(c.id))


@router.get("/api/connections", dependencies=[_MANAGE])
async def list_connections():
    def build():
        return [_public(c) for c in connections.list_all()]
    return {"connections": await asyncio.to_thread(build), "types": connections.types_public(),
            "capabilities": list(connections.CAPABILITIES),
            "allow_private_targets": connections.allow_private_targets(),
            "default": connections.default_id()}


@router.get("/api/connections/types", dependencies=[_MANAGE])
async def connection_types():
    return {"types": connections.types_public()}


@router.post("/api/connections/test", dependencies=[_MANAGE])
async def test_unsaved(payload: ConnectionTestIn):
    key = payload.api_key
    if not key and payload.use_connection:
        # Re-testing a form for a saved connection: the stored key is borrowed for the SAME type
        # and address only, so a Test can never carry one server's key to another.
        c = connections.get(payload.use_connection)
        if c is not None and c.type == payload.type and (
                not payload.base_url or payload.base_url.rstrip("/") == c.base_url.rstrip("/")):
            key = connections.credential(c)
    return await connections.run_test(type=payload.type, base_url=payload.base_url, api_key=key,
                                      model=payload.model)


@router.post("/api/connections/discover", dependencies=[_MANAGE])
async def discover(payload: DiscoverIn | None = None):
    p = payload or DiscoverIn()
    found = await connections.discover(hosts=p.hosts, lan=p.lan)
    return {"found": found, "scanned_lan": p.lan}


@router.post("/api/connections/adopt", dependencies=[_MANAGE])
async def adopt(request: Request):
    """Give every character's inline provider a connection now (idempotent)."""
    def run():
        import yaml
        path = config.CONFIG_PATH
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            return connections.AdoptPlan(actions=["config.yaml could not be read"])
        roster = [e for e in (raw.get("bots") or []) if isinstance(e, dict)]
        plan = connections.adopt_inline(roster)
        if plan.linked:
            config._write_bots(roster)
        return plan
    plan = await asyncio.to_thread(run)
    audit(request, "connection.adopt", "", f"linked {plan.linked}, created {len(plan.created)}")
    return {"linked": plan.linked, "created": plan.created, "notes": plan.actions}


@router.get("/api/connections/{connection_id}", dependencies=[_MANAGE])
async def get_connection(connection_id: str):
    c = await asyncio.to_thread(connections.get, connection_id)
    if c is None:
        raise fail(404, "connection_not_found", "Unknown connection")
    return await asyncio.to_thread(_public, c)


@router.post("/api/connections", status_code=201, dependencies=[_MANAGE])
async def create_connection(request: Request, payload: ConnectionCreateIn):
    try:
        c = await asyncio.to_thread(
            connections.create, type=payload.type, name=payload.name, base_url=payload.base_url,
            api_key=payload.api_key, api_key_env=payload.api_key_env,
            capabilities=payload.capabilities, default_model=payload.default_model,
            conn_id=payload.id or "")
    except connections.ConnectionError_ as e:
        raise _err(e) from e
    audit(request, "connection.create", c.id, c.type)
    from fastapi.responses import JSONResponse
    return JSONResponse(await asyncio.to_thread(_public, c), status_code=201)


@router.patch("/api/connections/{connection_id}", dependencies=[_MANAGE])
async def patch_connection(request: Request, connection_id: str, payload: ConnectionPatchIn):
    changes: dict[str, Any] = payload.model_dump(exclude_unset=True)
    if not changes:
        raise fail(422, "connection_invalid", "Nothing to change")
    existing = await asyncio.to_thread(connections.get, connection_id)
    if existing is not None:
        # A connection may raise the permission needed to manage it, but only the Owner or an
        # Admin (host control) lowers it.
        perm = changes.get("required_permission")
        if perm and perm != existing.required_permission:
            policy.of(request).require(Permission.ADMIN_SYSTEM)
        need = existing.required_permission
        if need != "manage_connections":
            policy.of(request).require(need)
        if _moves_key_to_another_host(existing, changes) and not policy.of(request).can(Permission.ADMIN_SYSTEM):
            # The key is write-only for an Admin; repointing the address while keeping it would
            # hand it to a server of their choosing on the next Test or chat turn (the same rule
            # the agent editor applies to MCP header secrets, and POST /api/connections/test to a
            # borrowed key).
            raise fail(403, "permission_denied",
                       "Changing this connection's server keeps its saved key, so only the Owner or an Admin can do it. "
                       "Paste the key again (or clear it) to change the address.")
    try:
        c = await asyncio.to_thread(connections.update, connection_id, changes)
    except connections.ConnectionError_ as e:
        raise _err(e) from e
    if "base_url" in changes:
        await asyncio.to_thread(characters.refresh_inline_mirrors, c)
    audit(request, "connection.update", c.id, ",".join(sorted(k for k in changes if "key" not in k)))
    return await asyncio.to_thread(_public, c)


def _host_of(url: str) -> str:
    from urllib.parse import urlparse
    try:
        return (urlparse(url or "").hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def _moves_key_to_another_host(existing: connections.Connection, changes: dict[str, Any]) -> bool:
    """Would this PATCH send the connection's EXISTING credential to a different host?"""
    if "base_url" not in changes or not existing.credential_ref:
        return False
    if str(changes.get("api_key") or "").strip() or changes.get("clear_key"):
        return False                     # a new key, or no key: nothing is carried over
    new = str(changes.get("base_url") or existing.ctype.base_url)
    return _host_of(new) != _host_of(existing.base_url)


@router.delete("/api/connections/{connection_id}", dependencies=[_MANAGE])
async def delete_connection(request: Request, connection_id: str):
    try:
        await asyncio.to_thread(connections.delete, connection_id)
    except connections.ConnectionError_ as e:
        raise _err(e) from e
    audit(request, "connection.delete", connection_id)
    return {"removed": connection_id}


@router.post("/api/connections/{connection_id}/test", dependencies=[_MANAGE])
async def test_saved(connection_id: str):
    c = await asyncio.to_thread(connections.get, connection_id)
    if c is None:
        raise fail(404, "connection_not_found", "Unknown connection")
    res = await connections.test_and_record(c)
    fresh = await asyncio.to_thread(connections.get, connection_id)
    res["connection"] = await asyncio.to_thread(_public, fresh) if fresh else None
    return res
