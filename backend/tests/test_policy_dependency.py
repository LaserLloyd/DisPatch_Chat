"""``require_permission`` as a FastAPI dependency: the second lock, on a bare app.

The auth gate enforces the declared requirement before any handler runs; the dependency
states the same sentence again at the handler, so a router mounted on an app with no
middleware at all (a bare test app, an extension's router) still fails closed.
"""
from __future__ import annotations

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app import policy
from app.contracts.permissions import Permission


def _bare_app() -> FastAPI:
    app = FastAPI()

    @app.get("/settings", dependencies=[Depends(policy.require_permission(Permission.MANAGE_SETTINGS))])
    def settings():
        return {"ok": True}

    @app.get("/chat", dependencies=[Depends(policy.require_permission("use_chat"))])
    def chat():
        return {"ok": True}

    @app.get("/tool", dependencies=[Depends(policy.require_permission("use_tools:weather"))])
    def tool():
        return {"ok": True}
    return app


def _client(world, signed_in_as=None):
    c = TestClient(_bare_app(), client=("192.168.1.9", 1))
    if signed_in_as is not None:
        c.cookies.set("lc_session", signed_in_as.cookies.get("lc_session"))
    return c


def test_the_dependency_refuses_without_the_permission_on_a_bare_app(world):
    owner = world.create_owner()
    mia = world.person(owner, "Mia", "restricted")
    kid = world.client()
    world.login(kid, profile_id=mia["id"])
    max_ = world.person(owner, "Max", "member", pin="5555")
    member = world.client()
    world.login(member, profile_id=max_["id"], pin="5555")

    assert _client(world).get("/settings").status_code == 403                  # nobody signed in
    assert _client(world).get("/chat").status_code == 200                      # Restricted may chat
    assert _client(world, kid).get("/settings").status_code == 403
    r = _client(world, member).get("/settings")
    assert r.status_code == 403 and "permission" in r.json()["detail"].lower()
    assert _client(world, owner).get("/settings").status_code == 200
    # A parametrised permission is satisfied by the blanket one (Member, Owner), not by Restricted.
    assert _client(world, member).get("/tool").status_code == 200
    assert _client(world, kid).get("/tool").status_code == 403


def test_an_unclaimed_install_is_open_on_a_bare_app(world):
    assert _client(world).get("/settings").status_code == 200


def test_the_dependency_records_what_it_requires():
    dep = policy.require_permission(Permission.MANAGE_USERS)
    assert dep.required_permission == "manage_users"
