"""The route-table gate (G5 part 1): every route and WebSocket action declares who may
call it, and nothing declares a route that does not exist.

Walks the REAL FastAPI app (every router, mounted app and static mount), so adding an
endpoint without an entry in ``app/route_policy.py`` fails the build here - before the
auth gate would have refused it at runtime. Run alone:

    uv run pytest tests/test_route_table.py -s

and it prints the count of routes per permission.
"""
from __future__ import annotations

from collections import Counter

import pytest
from fastapi.routing import APIRoute, APIWebSocketRoute
from starlette.routing import Mount

from app import main, policy, route_policy
from app.contracts.permissions import MachineScope, Permission, is_valid_permission

_SAFE = {"HEAD", "OPTIONS"}


def _http_routes():
    """(method, template) for every HTTP route of the real app."""
    for r in main.app.routes:
        if isinstance(r, APIRoute):
            for m in sorted(r.methods - _SAFE):
                yield m, r.path
        elif isinstance(r, APIWebSocketRoute):
            continue
        elif isinstance(r, Mount):
            continue
        elif hasattr(r, "methods") and getattr(r, "path", None):
            for m in sorted((r.methods or {"GET"}) - _SAFE):
                yield m, r.path


def _ws_routes():
    return [r.path for r in main.app.routes if isinstance(r, APIWebSocketRoute)]


def _requirement(method: str, template: str):
    path = template
    # Concrete-ise path params so the dynamic (app / extension) lookups can match.
    for part in ("{job_id}", "{key}", "{rid}", "{bot_id}", "{thread_id}", "{message_id}",
                 "{file_id}", "{snapshot_id}", "{tool_id}", "{ext_id}", "{sid}",
                 "{client_id}", "{app_id}", "{profile_id}", "{device_id}"):
        path = path.replace(part, "x")
    path = path.replace("{rel:path}", "x").replace("{path:path}", "x").replace("{ticket}", "x")
    return route_policy.requirement_for(method, template, path)


def test_every_http_route_declares_a_requirement():
    missing = [f"{m} {t}" for m, t in _http_routes() if _requirement(m, t) is None]
    assert not missing, (
        "These routes have no declared permission. Add them to app/route_policy.py "
        "(and say who may call them):\n  " + "\n  ".join(missing))


def test_every_websocket_route_and_action_declares_a_requirement():
    for path in _ws_routes():
        assert path in route_policy.WS_ROUTES, f"WebSocket {path} has no declared requirement"
    from app.main import WS_HANDLERS
    undeclared = [a for a in WS_HANDLERS if route_policy.ws_action_requirement(a) is None]
    assert not undeclared, f"WebSocket actions without a requirement: {undeclared}"
    stale = [a for a in route_policy.WS_ACTION_PERMISSIONS if a != "ping" and a not in WS_HANDLERS]
    assert not stale, f"declared WebSocket actions that no longer exist: {stale}"


def test_no_declaration_names_a_route_that_does_not_exist():
    real = {(m, t) for m, t in _http_routes()}
    real |= {("HEAD", t) for m, t in real if m == "GET"}
    stale = sorted(f"{m} {t}" for (m, t) in route_policy.declared() if (m, t) not in real)
    assert not stale, "route_policy declares routes the app does not have:\n  " + "\n  ".join(stale)


def test_requirements_are_well_formed():
    bad = []
    for (m, t), req in route_policy.declared().items():
        if req.public:
            if not req.note:
                bad.append(f"{m} {t}: PUBLIC needs a note saying why")
            continue
        if req.authenticated:
            continue
        if req.permission is None:
            if not req.machine:
                bad.append(f"{m} {t}: no permission and no machine scope")
            continue
        if not is_valid_permission(req.permission):
            bad.append(f"{m} {t}: unknown permission {req.permission!r}")
        for s in req.machine:
            if s not in {x.value for x in MachineScope}:
                bad.append(f"{m} {t}: unknown machine scope {s!r}")
    assert not bad, "\n".join(bad)


def test_state_changing_routes_are_never_public_without_a_stated_reason():
    """The only PUBLIC mutations are the sign-in family and the rig's callback."""
    allowed = {"/api/auth/unlock", "/api/auth/login", "/api/auth/switch", "/api/auth/lock",
               "/api/auth/recover", "/api/auth/setup", "/api/image-jobs/{job_id}/callback"}
    for (m, t), req in route_policy.declared().items():
        if req.public and m in ("POST", "PUT", "PATCH", "DELETE"):
            assert t in allowed, f"{m} {t} is PUBLIC; if that is right, add it to this allow-list"


def test_restricted_never_holds_a_management_permission():
    from app.contracts.permissions import ROLE_DEFAULTS, ROLE_NEVER, Role
    for role in (Role.USER, Role.BASIC):
        assert not (ROLE_DEFAULTS[role] & ROLE_NEVER[role])
        assert Permission.MANAGE_USERS in ROLE_NEVER[role] and Permission.ADMIN_SYSTEM in ROLE_NEVER[role]
    assert set(ROLE_DEFAULTS[Role.ADMIN]) == set(Permission)      # Admin is full (2026-10-19)


_OWNER_KINDS = {"thread", "message", "character", "file", "task"}


def _valid_owner_scoped(v: str) -> bool:
    return v in _OWNER_KINDS or (v.startswith("none: ") and len(v) > len("none: ") + 8)


def test_every_parameterised_route_declares_owner_scoped():
    """4.0 ownership: a route whose path names a thing (``/{id}``) must say whose thing it is
    - which owner check its handler runs - or ``none: <why>``. A new ``/{thing_id}`` route
    without that decision fails here."""
    missing, bad = [], []
    for m, t in _http_routes():
        if "{" not in t:
            continue
        req = _requirement(m, t)
        v = (req.owner_scoped if req is not None else "") or route_policy.owner_scoped_for(t)
        if not v:
            missing.append(f"{m} {t}")
        elif not _valid_owner_scoped(v):
            bad.append(f"{m} {t}: {v!r}")
    for path in _ws_routes():
        if "{" in path:
            v = route_policy.WS_ROUTES[path].owner_scoped
            if not v:
                missing.append(f"WS {path}")
            elif not _valid_owner_scoped(v):
                bad.append(f"WS {path}: {v!r}")
    assert not missing, ("Parameterised routes without owner_scoped (add them to "
                         "route_policy.OWNER_SCOPED):\n  " + "\n  ".join(missing))
    assert not bad, "Malformed owner_scoped values:\n  " + "\n  ".join(bad)


def test_owner_scoped_names_only_real_routes_and_ws_actions():
    real = {t for _m, t in _http_routes()} | set(_ws_routes())
    stale = sorted(t for t in route_policy.OWNER_SCOPED if t not in real)
    assert not stale, "OWNER_SCOPED names routes the app does not have:\n  " + "\n  ".join(stale)
    from app.main import WS_HANDLERS
    for action in WS_HANDLERS:
        v = route_policy.ws_action_requirement(action).owner_scoped
        assert _valid_owner_scoped(v), f"/ws action {action!r} has no owner_scoped"


def test_owner_scoped_is_published_in_routes_json():
    """routes.json (the contract, regenerated by scripts/gen_contracts.py and pinned by
    test_contracts_drift) carries the decision for every declared parameterised route."""
    import json
    from pathlib import Path
    doc = json.loads((Path(__file__).resolve().parents[2] / "docs/contracts/routes.json").read_text())
    by = {(r.get("method"), r.get("path")): r for r in doc.get("routes") or []}
    for (m, t), req in route_policy.declared().items():
        if "{" in t and m != "HEAD" and req.owner_scoped and (m, t) in by:
            assert by[(m, t)].get("requirement", {}).get("owner_scoped") == req.owner_scoped, (m, t)


def test_report_routes_per_permission(capsys):
    """Print the table the lead asked for: how many routes sit behind each permission."""
    rows = Counter()
    for m, t in _http_routes():
        req = _requirement(m, t)
        assert req is not None
        key = "PUBLIC" if req.public else ("authenticated" if req.authenticated
                                           else (req.permission or "machine-only"))
        rows[key] += 1
    ws = Counter(("PUBLIC" if r.public else r.permission) for r in route_policy.WS_ACTION_PERMISSIONS.values())
    with capsys.disabled():
        print("\nHTTP routes per declared permission:")
        for k, n in sorted(rows.items(), key=lambda kv: (-kv[1], kv[0])):
            print(f"  {k:24s} {n}")
        print(f"  {'TOTAL':24s} {sum(rows.values())}")
        print("WebSocket actions per declared permission:")
        for k, n in sorted(ws.items(), key=lambda kv: (-kv[1], kv[0])):
            print(f"  {k:24s} {n}")
    assert sum(rows.values()) > 100


def test_the_gate_refuses_a_route_without_a_declaration(monkeypatch):
    """Fail closed at runtime too: an undeclared route answers 403, not 200."""
    from fastapi.testclient import TestClient

    from app import auth

    @main.app.get("/api/_t_undeclared")
    async def _undeclared():
        return {"ok": True}

    try:
        auth.set_pin("1234")
        main._route_index._cache.clear()
        with TestClient(main.app) as c:
            assert c.post("/api/auth/unlock", json={"pin": "1234"}).status_code == 200
            r = c.get("/api/_t_undeclared")
            assert r.status_code == 403
            assert r.json()["error"]["code"] == "route_undeclared"
    finally:
        main.app.router.routes[:] = [r for r in main.app.router.routes
                                     if getattr(r, "path", "") != "/api/_t_undeclared"]
        main._route_index._cache.clear()
        policy.clear_route_cache(main._route_index)


@pytest.fixture(autouse=True)
def _open_install(monkeypatch):
    # These tests only read the route table; the one runtime check sets its own PIN.
    monkeypatch.setenv("DISPATCH_ALLOW_OPEN_INSTALL", "1")
