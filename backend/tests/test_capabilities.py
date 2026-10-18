"""Gate G6 - parity: every surface a capability declares exists, and its permission is
the one the route table enforces.

``cli`` and ``mcp`` are ``None`` on every capability today (DisPatch has no CLI or MCP
server yet - audit P11); this test pins that so the day one appears the registry has to
say so.
"""
from __future__ import annotations

import re
from pathlib import Path

from fastapi.routing import APIRoute

from app import main, route_policy
from app.contracts import capabilities
from app.contracts.permissions import is_valid_permission

ROOT = Path(__file__).resolve().parents[2]
API_JS = (ROOT / "frontend" / "static" / "js" / "api.js").read_text(encoding="utf-8")


def _real_routes():
    return {(m, r.path) for r in main.app.routes if isinstance(r, APIRoute)
            for m in r.methods if m not in ("HEAD", "OPTIONS")}


def _req(method, template):
    return route_policy.requirement_for(method, template, re.sub(r"\{[^}]+\}", "x", template))


def test_ids_are_unique_and_every_http_route_is_claimed_once():
    ids = [c.id for c in capabilities.CAPABILITIES]
    assert len(ids) == len(set(ids))
    claimed = [h for c in capabilities.CAPABILITIES for h in c.http]
    assert len(claimed) == len(set(claimed)), "a route is claimed by two capabilities"


def test_every_declared_http_route_exists():
    real = _real_routes()
    missing = [f"{c.id}: {m} {p}" for c in capabilities.CAPABILITIES for (m, p) in c.http
               if (m, p) not in real]
    assert not missing, "\n".join(missing)


def test_every_declared_websocket_action_exists():
    from app.main import WS_HANDLERS
    missing = [f"{c.id}: {a}" for c in capabilities.CAPABILITIES for a in c.ws if a not in WS_HANDLERS]
    assert not missing, missing


def test_every_declared_ui_entry_point_exists():
    problems = []
    for c in capabilities.CAPABILITIES:
        if c.ui is None:
            continue
        kind, _, name = c.ui.partition(".")
        if kind == "api":
            if not re.search(rf"^\s*{re.escape(name)}\s*:", API_JS, re.M):
                problems.append(f"{c.id}: api.{name} is not in frontend/static/js/api.js")
        elif kind != "settings":
            problems.append(f"{c.id}: unknown ui kind {c.ui!r}")
    assert not problems, "\n".join(problems)


def test_a_capability_declares_the_permission_its_routes_enforce():
    bad = []
    for c in capabilities.CAPABILITIES:
        for (m, p) in c.http:
            req = _req(m, p)
            want = req.permission if req is not None else None
            if (c.permission or None) != want:
                bad.append(f"{c.id}: {m} {p} enforces {want!r}, the capability says {c.permission!r}")
            if req is not None and set(c.machine) - set(req.machine):
                bad.append(f"{c.id}: {m} {p} does not admit machine scopes {set(c.machine) - set(req.machine)}")
        for a in c.ws:
            req = route_policy.ws_action_requirement(a)
            want = req.permission if req is not None else None
            if (c.permission or None) != want:
                bad.append(f"{c.id}: ws {a} enforces {want!r}, the capability says {c.permission!r}")
    assert not bad, "\n".join(bad)


def test_permissions_named_by_capabilities_are_real():
    for c in capabilities.CAPABILITIES:
        assert c.permission is None or is_valid_permission(c.permission), c.id


def test_no_cli_or_mcp_surface_exists_yet():
    assert all(c.cli is None and c.mcp is None for c in capabilities.CAPABILITIES)
    assert not (ROOT / "backend" / "app" / "cli.py").exists()
