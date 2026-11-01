"""4.3 (#9): which Claude Agent auth routes can authenticate on this server.

``GET /api/agents/claude/routes`` (``manage_agents``, declared in ``route_policy.py``) answers::

    {"current": "api_key",
     "routes": [{"route": "api_key", "available": true,  "missing": []},
                {"route": "bedrock", "available": false, "missing": ["AWS_REGION or AWS_DEFAULT_REGION"]},
                ...]}

``missing`` holds environment variable NAMES (one entry per unmet requirement; ``or`` between
alternatives, ``+`` between names that go together), never a value. The Settings UI (WP-F3)
disables a route that is not available. The cloud routes are unverified against real clouds.
"""
from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from . import agents_service, policy
from .agents.claude_sdk_adapter import check_route_env
from .agents.secrets import SECRET_ANTHROPIC_API_KEY
from .contracts.permissions import Permission

router = APIRouter()

_MANAGE = Depends(policy.require_permission(Permission.MANAGE_AGENTS))


def route_rows(*, api_key_set: bool, environ: Any = None) -> list[dict[str, Any]]:
    env = os.environ if environ is None else environ
    rows: list[dict[str, Any]] = []
    for ui_route, adapter_route in agents_service.CLAUDE_ROUTES.items():
        if adapter_route == "api_key":
            missing = [] if api_key_set else [SECRET_ANTHROPIC_API_KEY]
        else:
            missing = check_route_env(adapter_route, env).missing
        rows.append({"route": ui_route, "available": not missing, "missing": missing})
    return rows


@router.get("/api/agents/claude/routes")
async def claude_routes(request: Request, _=_MANAGE):
    try:
        mgr = agents_service.get_manager()
    except RuntimeError:
        raise HTTPException(503, "Agents can't run right now.") from None
    st = mgr.credential_status()
    return {"current": st["route"], "routes": route_rows(api_key_set=bool(st["api_key_set"]))}
