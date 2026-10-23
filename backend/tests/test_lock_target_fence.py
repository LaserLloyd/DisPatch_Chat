"""The lock-target fence (4.0, BUILD-PLAN-v2 §1.1): the shared default profile can
never run agents or clone voices, whatever its role or grants say; only an Admin edits its
PIN, password, name or picture; the fence is re-checked when the lock target moves.

Run: cd backend && uv run pytest tests/test_lock_target_fence.py
"""
from __future__ import annotations

import pytest

from app import accounts, auth, policy


@pytest.fixture
def lobby(world):
    owner = world.create_owner(world.client())
    p = world.person(owner, "Lobby", "user")
    auth.set_lock_target("default_profile", p["id"])
    return {"owner": owner, "id": p["id"], "world": world}


def test_agents_and_voice_cloning_are_not_grantable_to_the_lock_target(lobby):
    for perm in ("dispatch_agents", "approve_agent_actions", "voice_lab"):
        r = lobby["owner"].patch(f"/api/people/{lobby['id']}", json={"grants": [perm]})
        assert r.status_code == 422, (perm, r.text)
        assert r.json()["error"]["code"] == "lock_target_fence"
    eff = policy.effective_permissions(accounts.get_profile(lobby["id"]))
    assert "voice_lab" not in eff and "dispatch_agents" not in eff
    assert "use_chat" in eff


def test_moving_the_lock_target_strips_fenced_grants(world):
    owner = world.create_owner(world.client())
    p = world.person(owner, "Later", "user")
    r = owner.patch(f"/api/people/{p['id']}", json={"grants": ["dispatch_agents"]})
    assert r.status_code == 200
    auth.set_lock_target("default_profile", p["id"])
    assert "dispatch_agents" not in accounts.get_profile(p["id"]).grants
    assert "dispatch_agents" not in policy.effective_permissions(accounts.get_profile(p["id"]))


def test_an_admin_cannot_be_the_default_profile(world):
    owner = world.create_owner(world.client())
    a = world.person(owner, "Boss", "admin")
    with pytest.raises(accounts.AccountError):
        auth.set_lock_target("default_profile", a["id"])
    with pytest.raises(accounts.AccountError):
        auth.set_lock_target("default_profile", "nope")


def test_the_lock_target_cannot_rename_itself_or_set_its_pin(lobby):
    world = lobby["world"]
    from agents_world import sibling
    guest = sibling(world, lobby["owner"], ("203.0.113.120", 45000))      # unsigned -> Lobby
    me = guest.get("/api/auth/me").json()
    assert me["profile"]["id"] == lobby["id"] and me["shared"] is True
    r = guest.post("/api/auth/pin", json={"current": "", "new": "9999"})
    assert r.status_code == 403
    assert accounts.get_profile(lobby["id"]).has_pin is False
    # an Admin may
    assert lobby["owner"].patch(f"/api/people/{lobby['id']}", json={"name": "Hall"}).status_code == 200
    assert lobby["owner"].post(f"/api/people/{lobby['id']}/pin", json={"pin": "2580"}).status_code == 200


def test_the_settings_store_sets_the_lock_target_through_the_same_check(lobby):
    from app import settings_store
    from app.settings_schema import SettingError
    a = lobby["world"].person(lobby["owner"], "Boss", "admin")
    refused = settings_store.set_many({"people.default_profile": a["id"]}, actor="t", owner=True)
    assert not refused.ok and "Admin" in refused.errors["people.default_profile"]
    assert auth.load().default_profile == lobby["id"]                  # nothing was written
    res = settings_store.set_many({"people.lock_target": "profile_list"}, actor="t", owner=True)
    assert auth.load().lock_target == "profile_list", res
    assert SettingError is not None


def test_a_refused_lock_target_is_a_422_with_the_reason_and_writes_nothing(lobby):
    """PATCH /api/settings: an Admin as the default profile, or the default-profile landing with
    no profile named, is a plain 422 (not a 500), and no other key of the PATCH is applied."""
    o = lobby["owner"]
    a = lobby["world"].person(o, "Boss", "admin")
    before = auth.load()
    r = o.patch("/api/settings", json={"set": {"people.default_profile": a["id"],
                                                "people.lock_timeout_seconds": 1234}})
    assert r.status_code == 422, r.text
    assert "people.default_profile" in r.json()["errors"]
    after = auth.load()
    assert (after.default_profile, after.lock_target, after.lock_timeout) == (
        before.default_profile, before.lock_target, before.lock_timeout)
    auth.set_lock_target("profile_list", None)
    r = o.patch("/api/settings", json={"set": {"people.lock_target": "default_profile",
                                                "people.default_profile": ""}})
    assert r.status_code == 422, r.text
    # Both keys in one PATCH are one decision: target + profile together are accepted.
    r = o.patch("/api/settings", json={"set": {"people.lock_target": "default_profile",
                                                "people.default_profile": lobby["id"]}})
    assert r.status_code == 200, r.text
    cfg = auth.load()
    assert (cfg.lock_target, cfg.default_profile) == ("default_profile", lobby["id"])


def test_the_shared_default_profile_can_never_be_made_an_admin(lobby):
    """Every unsigned device lands on the default profile: an Admin there would be an Admin on
    every device that reaches the port. Both the preview and the change refuse it."""
    o, pid = lobby["owner"], lobby["id"]
    r = o.get(f"/api/people/{pid}/role/preview", params={"role": "admin"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "lock_target_admin", r.text
    r = o.post(f"/api/people/{pid}/role", json={"role": "admin"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "lock_target_admin", r.text
    assert accounts.get_profile(pid).role.value == "user"
    # Once another profile is the landing (or the profile list is), it may be promoted.
    auth.set_lock_target("profile_list", None)
    assert o.post(f"/api/people/{pid}/role", json={"role": "admin"}).status_code == 200


def test_a_profile_with_a_pin_is_never_opened_by_the_lock_target(world):
    """A profile with a PIN (or password) cannot be made the default profile: it would open on
    every unsigned device without it. A PIN an Admin sets on the default profile later is
    honoured: unsigned devices get the sign-in screen."""
    owner = world.create_owner(world.client())
    pinned = world.person(owner, "Kit", "basic", pin="1357")
    with pytest.raises(accounts.AccountError):
        auth.set_lock_target("default_profile", pinned["id"])
    r = owner.patch("/api/settings", json={"set": {"people.lock_target": "default_profile",
                                                    "people.default_profile": pinned["id"]}})
    assert r.status_code == 422, r.text
    lobby = world.person(owner, "Hall", "user")
    auth.set_lock_target("default_profile", lobby["id"])
    assert policy.unsigned() is not None
    assert owner.post(f"/api/people/{lobby['id']}/pin", json={"pin": "2580"}).status_code == 200
    assert policy.unsigned() is None
