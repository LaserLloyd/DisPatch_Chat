"""Permission changes the owner ordered on 2026-10-19, one block per order.

P-1  "Admin should have full permissions": an Admin holds every permission the Owner has
     (host control included). The single structural exception: an Admin may not delete,
     disable, demote, sign out or re-credential THE Owner profile.

Run: cd backend && uv run pytest tests/test_ops_perms_20261019.py
"""
from __future__ import annotations

from app import policy
from app.contracts.permissions import ROLE_PERMISSIONS, Permission, Role

LAN = ("203.0.113.81", 43001)


def _admin(world, owner, name="Ada", password="admin-pass1", addr=LAN):
    prof = world.person(owner, name, "admin", password=password)
    c = world.client(addr)
    assert world.login(c, name=name, password=password).status_code == 200
    return prof, c


# P-1 ------------------------------------------------------------------------ #

def test_admin_holds_every_permission_the_owner_has():
    assert ROLE_PERMISSIONS[Role.ADMIN] == ROLE_PERMISSIONS[Role.OWNER] == frozenset(Permission)


def test_admin_reaches_every_owner_surface(world):
    owner = world.create_owner(world.client())
    _ada, admin = _admin(world, owner)
    me = admin.get("/api/auth/me").json()
    assert "admin_system" in me["permissions"], me
    # Health (the dashboard), backups, the API key, host-control settings, the network step.
    assert admin.get("/api/dashboard").status_code == 200
    assert admin.get("/api/backups").status_code == 200
    r = admin.post("/api/auth/api-token", json={"rotate": True})
    assert r.status_code == 200 and len(r.json()["api_token"]) >= 32, r.text
    assert admin.get("/api/auth/api-token").json()["has_token"] is True
    r = admin.patch("/api/settings", json={"set": {"connections.image_cli": "/usr/bin/true"}})
    assert r.status_code == 200, r.text
    assert admin.get("/api/export").status_code == 200
    assert admin.get("/api/plugins").status_code == 200
    assert admin.get("/api/connections").status_code == 200
    assert admin.get("/api/agents").status_code == 200
    assert admin.get("/api/people/audit").status_code == 200


def test_admin_manages_other_admins(world):
    owner = world.create_owner(world.client())
    _ada, admin = _admin(world, owner)
    r = admin.post("/api/people", json={"name": "Bea", "role": "admin", "password": "bea-pass-123"})
    assert r.status_code == 201, r.text
    bea = r.json()
    assert admin.get("/api/people").json()["can_create_admin"] is True
    r = admin.post(f"/api/people/{bea['id']}/password", json={"password": "bea-pass-456"})
    assert r.status_code == 200, r.text
    assert world.login(world.client(("203.0.113.82", 43002)), name="Bea",
                       password="bea-pass-456").status_code == 200  # scrub-ok: throwaway test fixture
    assert admin.patch(f"/api/people/{bea['id']}", json={"role": "member"}).status_code in (200, 409)
    assert admin.post(f"/api/people/{bea['id']}/sign-out").status_code == 200
    assert admin.delete(f"/api/people/{bea['id']}").status_code == 200
    # A Member can never be granted host control; an Admin needs no grant for it.
    r = admin.post("/api/people", json={"name": "Max", "role": "member", "pin": "5555",
                                        "grants": ["admin_system"]})
    assert r.status_code == 400 and r.json()["error"]["code"] == "permissions_invalid"


def test_the_owner_profile_stays_out_of_an_admins_reach(world):
    owner = world.create_owner(world.client())
    _ada, admin = _admin(world, owner)
    olive = next(p for p in admin.get("/api/people").json()["profiles"] if p["role"] == "owner")
    oid = olive["id"]
    assert admin.delete(f"/api/people/{oid}").status_code == 403
    assert admin.patch(f"/api/people/{oid}", json={"disabled": True}).status_code == 403
    assert admin.patch(f"/api/people/{oid}", json={"role": "member"}).status_code == 403
    assert admin.post(f"/api/people/{oid}/pin", json={"pin": "9999"}).status_code == 403
    assert admin.delete(f"/api/people/{oid}/pin").status_code == 403
    assert admin.post(f"/api/people/{oid}/password", json={"password": "taken-over-1"}).status_code == 403
    assert admin.post(f"/api/people/{oid}/sign-out").status_code == 403
    assert admin.delete(f"/api/users/{oid}").status_code == 403
    # The install PIN is the Owner's credential.
    r = admin.post("/api/auth/setup", json={"current_pin": "2468", "new_pin": "1111"})
    assert r.status_code == 403
    # And the owner still signs in with what they had.
    assert world.login(world.client(("203.0.113.83", 43003)), name="Olive",
                       password="owner-pass1").status_code == 200


def test_effective_permissions_of_an_admin_include_host_control(world):
    owner = world.create_owner(world.client())
    ada, _admin_client = _admin(world, owner)
    from app import accounts
    prof = accounts.get_profile(ada["id"])
    assert Permission.ADMIN_SYSTEM.value in policy.effective_permissions(prof)
