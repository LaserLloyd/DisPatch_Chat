"""Role x permission x override through the People API (4.0, BUILD-PLAN-v2 §1.1).

Grants must sit in the role's grantable column: anything the role can never hold is a 422;
a default is a no-op; denies take any known permission off.

Run: cd backend && uv run pytest tests/test_roles_permissions.py
"""
from __future__ import annotations

import pytest

from app import accounts, policy
from app.contracts.permissions import ROLE_DEFAULTS, ROLE_GRANTABLE, ROLE_NEVER, Role


@pytest.fixture
def owner(world):
    return world.create_owner(world.client())


def _make(world, owner, role, **extra):
    return world.person(owner, f"P{role}{len(accounts.list_profiles())}", role, **extra)


@pytest.mark.parametrize("role", [Role.USER, Role.BASIC])
def test_every_grantable_permission_can_be_granted(world, owner, role):
    p = _make(world, owner, role.value)
    for perm in sorted(x.value for x in ROLE_GRANTABLE[role]):
        r = owner.patch(f"/api/people/{p['id']}", json={"grants": [perm]})
        assert r.status_code == 200, (perm, r.text)
        eff = policy.effective_permissions(accounts.get_profile(p["id"]))
        assert perm in eff


@pytest.mark.parametrize("role", [Role.USER, Role.BASIC])
def test_never_permissions_are_refused_with_422(world, owner, role):
    p = _make(world, owner, role.value)
    for perm in sorted(x.value for x in ROLE_NEVER[role]):
        r = owner.patch(f"/api/people/{p['id']}", json={"grants": [perm]})
        assert r.status_code == 422, (perm, r.status_code, r.text)
    r = owner.patch(f"/api/people/{p['id']}", json={"grants": ["use_tools:benchmark"]})
    assert r.status_code == 422
    # creating with one is refused the same way
    r = owner.post("/api/people", json={"name": "Nope", "role": role.value, "grants": ["admin_system"]})
    assert r.status_code == 422, r.text


@pytest.mark.parametrize("role", [Role.USER, Role.BASIC])
def test_defaults_are_a_no_op_and_denies_take_them_away(world, owner, role):
    p = _make(world, owner, role.value)
    a_default = sorted(x.value for x in ROLE_DEFAULTS[role])[0]
    r = owner.patch(f"/api/people/{p['id']}", json={"grants": [a_default]})
    assert r.status_code == 200 and r.json()["grants"] == []
    r = owner.patch(f"/api/people/{p['id']}", json={"denies": ["use_lookups"]})
    assert r.status_code == 200
    assert "use_lookups" not in policy.effective_permissions(accounts.get_profile(p["id"]))


def test_an_admin_holds_everything_and_ignores_overrides(world, owner):
    p = _make(world, owner, "admin")
    r = owner.patch(f"/api/people/{p['id']}", json={"grants": ["admin_system"]})
    assert r.status_code == 200 and r.json()["grants"] == []
    eff = policy.effective_permissions(accounts.get_profile(p["id"]))
    assert {"admin_system", "manage_users", "voice_lab"} <= eff


def test_unknown_permissions_are_422(world, owner):
    p = _make(world, owner, "user")
    r = owner.patch(f"/api/people/{p['id']}", json={"grants": ["fly_to_the_moon"]})
    assert r.status_code == 422


def test_credentials_are_optional_on_every_profile(world, owner):
    for role in ("admin", "user", "basic"):
        r = owner.post("/api/people", json={"name": f"Free{role}", "role": role})
        assert r.status_code == 201, (role, r.text)
        assert r.json()["no_credential"] is True
    from agents_world import sibling
    c = sibling(world, owner, ("203.0.113.90", 44100))
    pid = next(p.id for p in accounts.list_profiles() if p.name == "Freeuser")
    r = world.login(c, profile_id=pid)
    assert r.status_code == 200, r.text
    # an Admin with neither may simply be picked too (People warns about it)
    aid = next(p.id for p in accounts.list_profiles() if p.name == "Freeadmin")
    c2 = sibling(world, owner, ("203.0.113.91", 44101))
    assert world.login(c2, profile_id=aid).status_code == 200


def test_a_profile_with_a_pin_still_needs_it(world, owner):
    p = world.person(owner, "Locked", "user", pin="4321")
    from agents_world import sibling
    c = sibling(world, owner, ("203.0.113.92", 44102))
    assert world.login(c, profile_id=p["id"]).status_code == 401
    assert world.login(c, profile_id=p["id"], pin="4321").status_code == 200


def test_household_bots_on_create_and_the_patch_points_elsewhere(world, owner):
    p = _make(world, owner, "basic")
    assert accounts.get_profile(p["id"]).household_bots == []          # Basic starts with none
    r = owner.post("/api/people", json={"name": "Pick", "role": "basic", "household_bots": ["alpha"]})
    assert r.status_code == 201 and r.json()["household_bots"] == ["alpha"]
    r = owner.post("/api/people", json={"name": "Bad", "role": "basic", "household_bots": ["nope"]})
    assert r.status_code == 422
    # role and household list changes belong to profiles_service's routes (the wipe)
    r = owner.patch(f"/api/people/{p['id']}", json={"household_bots": ["alpha"]})
    assert r.status_code == 409 and r.json()["error"]["code"] == "use_household_route"
    r = owner.patch(f"/api/people/{p['id']}", json={"role": "user"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "use_role_route"
    assert owner.delete(f"/api/people/{p['id']}").json()["error"]["code"] == "use_delete_route"
