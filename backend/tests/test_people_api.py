"""Profiles, roles, sign-in, throttling and the People & Profiles API (3.0).

Everything runs through the real app (``world`` fixture, tests/conftest.py): the gate,
the policy engine and the SQLite account layer are all live; only the clock and the
agent backend are not.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app import accounts, auth, main


@pytest.fixture
def owner(world):
    return world.create_owner()


# --------------------------------------------------------------------------- #
# First run: the owner account
# --------------------------------------------------------------------------- #


def test_setup_from_this_machine_creates_the_owner_and_signs_in(world):
    c = world.client()
    r = c.post("/api/auth/setup", json={"name": "Olive", "password": "owner-pass1", "new_pin": "2468"})
    assert r.status_code == 200
    assert r.json()["claimed"] is True
    st = c.get("/api/auth/status").json()
    assert st["claimed"] and st["authenticated"] and st["role"] == "admin"
    assert st["profile"]["name"] == "Olive" and st["needs_password"] is False
    assert st["pin_set"] is True
    owner_row = accounts.get_owner()
    assert owner_row is not None and owner_row.has_password and owner_row.has_pin
    # The Owner's PIN is the install PIN: the old unlock flow still works.
    c2 = world.client()
    assert c2.post("/api/auth/unlock", json={"pin": "2468"}).status_code == 200
    assert c2.get("/api/auth/me").json()["role"] == "admin"


def test_setup_with_a_password_only_leaves_no_pin(world):
    c = world.client()
    assert c.post("/api/auth/setup", json={"password": "owner-pass1"}).status_code == 200
    assert c.get("/api/auth/status").json()["pin_set"] is False
    # Claimed all the same: a stranger is nobody (4.0: the profile list), not the owner.
    stranger = world.client()
    assert stranger.get("/api/bots/all").status_code == 401


def test_setup_refuses_a_weak_credential(world):
    c = world.client()
    assert c.post("/api/auth/setup", json={"password": "short"}).status_code == 400
    assert c.post("/api/auth/setup", json={"new_pin": "12"}).status_code == 400
    assert c.post("/api/auth/setup", json={}).status_code == 400
    assert not accounts.has_owner()


def test_a_second_setup_cannot_replace_the_owner(world, owner):
    r = world.client().post("/api/auth/setup", json={"password": "attacker-pw1", "new_pin": "9999"})
    assert r.status_code == 403            # claimed: needs the owner's session + current PIN


def test_the_owner_changes_the_pin_with_the_current_one(world, owner):
    r = owner.post("/api/auth/setup", json={"new_pin": "1357", "current_pin": "0000"})
    assert r.status_code == 403
    r = owner.post("/api/auth/setup", json={"new_pin": "1357", "current_pin": "2468"})
    assert r.status_code == 200
    assert world.client().post("/api/auth/unlock", json={"pin": "1357"}).status_code == 200
    assert world.client().post("/api/auth/unlock", json={"pin": "2468"}).status_code == 401


def test_changing_the_owner_pin_does_not_sign_the_children_out(world, owner):
    kid = world.person(owner, "Mia", "restricted")
    kid_client = world.client()
    assert world.login(kid_client, profile_id=kid["id"]).status_code == 200
    assert owner.post("/api/auth/setup", json={"new_pin": "1357", "current_pin": "2468"}).status_code == 200
    assert kid_client.get("/api/auth/me").json()["profile"]["name"] == "Mia"


# --------------------------------------------------------------------------- #
# Creating and ranking profiles
# --------------------------------------------------------------------------- #


def test_credentials_are_optional_for_new_admins_too(world):
    c = world.client()
    assert c.post("/api/auth/setup", json={"new_pin": "2468"}).status_code == 200   # PIN-only owner
    # 4.0: PINs and passwords are optional on every profile (People warns, no_credential).
    r = c.post("/api/people", json={"name": "Ada", "role": "admin"})
    assert r.status_code == 201 and r.json()["no_credential"] is True
    r = c.post("/api/people", json={"name": "Bea", "role": "admin", "password": "admin-pass1"})
    assert r.status_code == 201 and r.json()["no_credential"] is False
    r = c.post("/api/people", json={"name": "Cy", "role": "user", "password": "user-pass-12"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "password_role"


def test_no_profile_needs_a_pin(world, owner):
    assert owner.post("/api/people", json={"name": "Max", "role": "member"}).status_code == 201
    assert owner.post("/api/people", json={"name": "Moe", "role": "member", "pin": "5555"}).status_code == 201
    assert owner.post("/api/people", json={"name": "Mia", "role": "restricted"}).status_code == 201
    assert owner.post("/api/people", json={"name": "mia", "role": "restricted"}).status_code == 409   # unique, case-folded


def test_an_admin_manages_everyone_but_the_owner(world, owner):
    world.person(owner, "Ada", "admin", password="admin-pass1")
    admin = world.client()
    assert world.login(admin, name="Ada", password="admin-pass1").status_code == 200
    assert admin.post("/api/people", json={"name": "Max", "role": "member", "pin": "5555"}).status_code == 201
    assert admin.post("/api/people", json={"name": "Mia", "role": "restricted"}).status_code == 201
    r = admin.post("/api/people", json={"name": "Eve", "role": "admin", "password": "eve-pass-12"})
    assert r.status_code == 201, r.text                 # 2026-10-19: an Admin may add Admins
    people = {p["name"]: p for p in admin.get("/api/people").json()["profiles"]}
    assert admin.patch(f"/api/people/{people['Olive']['id']}", json={"name": "Hijack"}).status_code == 403
    assert admin.delete(f"/api/people/{people['Olive']['id']}").status_code == 403
    assert admin.post(f"/api/people/{people['Max']['id']}/password", json={"password": "max-pass-123"}).status_code == 409
    assert admin.patch(f"/api/people/{people['Eve']['id']}", json={"disabled": True}).status_code == 200


def test_members_and_restricted_cannot_open_the_people_api(world, owner):
    world.person(owner, "Max", "member", pin="5555")
    world.person(owner, "Mia", "restricted")
    for name, pin in (("Max", "5555"), ("Mia", None)):
        c = world.client()
        assert world.login(c, name=name, pin=pin).status_code == 200
        assert c.get("/api/people").status_code == 403
        assert c.post("/api/people", json={"name": "X", "role": "restricted"}).status_code == 403
        assert c.get("/api/people/activity").status_code == 403


def test_the_owner_is_immutable(world, owner):
    me = accounts.get_owner()
    assert owner.patch(f"/api/people/{me.id}", json={"role": "member"}).status_code == 409
    assert owner.delete(f"/api/people/{me.id}").status_code == 409
    with pytest.raises(accounts.AccountError):
        accounts.update_profile(me.id, role="user")
    assert accounts.update_profile(me.id, role="admin").protected      # 4.0: still the protected Admin
    with pytest.raises(accounts.AccountError):
        accounts.create_profile("Second", "owner")


def test_a_restricted_profile_can_never_be_granted_management(world, owner):
    r = owner.post("/api/people", json={"name": "Mia", "role": "restricted",
                                        "grants": ["manage_settings"]})
    assert r.status_code == 422
    ok = owner.post("/api/people", json={"name": "Mia", "role": "restricted", "grants": ["use_images"]})
    assert ok.status_code == 201
    r = owner.patch(f"/api/people/{ok.json()['id']}", json={"grants": ["admin_system"]})
    assert r.status_code == 422


def test_budgets_are_gone(world, owner):
    """4.0 (SPEC section 5): no daily limits or budgets of any kind - the field is refused."""
    r = owner.post("/api/people", json={"name": "Mia", "role": "basic", "budgets": {"turns": 40}})
    assert r.status_code == 422
    mia = world.person(owner, "Mia", "basic")
    assert owner.patch(f"/api/people/{mia['id']}", json={"budgets": {"turns": 9}}).status_code == 422


# --------------------------------------------------------------------------- #
# Sign-in, switching, sessions
# --------------------------------------------------------------------------- #


def test_sign_in_credentials_follow_the_role(world, owner):
    ada = world.person(owner, "Ada", "admin", password="admin-pass1")
    max_ = world.person(owner, "Max", "member", pin="5555")
    mia = world.person(owner, "Mia", "restricted")
    pin_kid = world.person(owner, "Kit", "restricted", pin="1212")

    c = world.client()
    assert world.login(c, profile_id=ada["id"], pin="5555").status_code == 401       # admin: password only
    assert world.login(c, profile_id=ada["id"], password="admin-pass1").status_code == 200
    assert world.login(c, profile_id=max_["id"], pin="0000").status_code == 401
    assert world.login(c, profile_id=max_["id"], pin="5555").json()["role"] == "user"
    assert world.login(c, profile_id=mia["id"]).json()["role"] == "basic"        # no PIN: just pick it
    assert world.login(c, profile_id=pin_kid["id"]).status_code == 401               # has a PIN: needs it
    assert world.login(c, profile_id=pin_kid["id"], pin="1212").status_code == 200
    # The Owner: password or the install PIN.
    me = accounts.get_owner()
    assert world.login(c, profile_id=me.id, password="owner-pass1").json()["role"] == "admin"
    assert world.login(c, profile_id=me.id, pin="2468").json()["role"] == "admin"
    assert world.login(c, profile_id=me.id, pin="9999").status_code == 401
    assert world.login(c, profile_id="nope", pin="1").status_code == 401


def test_stepping_up_to_admin_needs_their_credential(world, owner):
    world.person(owner, "Ada", "admin", password="admin-pass1")
    mia = world.person(owner, "Mia", "restricted")
    c = world.client()
    assert world.login(c, profile_id=mia["id"]).status_code == 200
    assert c.get("/api/people").status_code == 403                                    # Restricted
    r = c.post("/api/auth/switch", json={"name": "Ada", "pin": "1234"})
    assert r.status_code == 401
    assert c.get("/api/auth/me").json()["role"] == "basic"                      # still Mia
    r = c.post("/api/auth/switch", json={"name": "Ada", "password": "admin-pass1"})
    assert r.status_code == 200 and c.get("/api/auth/me").json()["role"] == "admin"
    assert c.get("/api/people").status_code == 200


def test_lock_lands_on_the_lock_target(world, owner):
    assert owner.get("/api/people").status_code == 200
    r = owner.post("/api/auth/lock")
    assert r.json()["lock_target"] == "profile_list" and r.json()["landed"] is None
    assert owner.get("/api/people").status_code == 401
    # With a default profile, the lock lands there.
    c = world.client()
    assert world.login(c, profile_id=accounts.get_owner().id, pin="2468").status_code == 200
    lobby = c.post("/api/people", json={"name": "Lobby", "role": "user"}).json()
    auth.set_lock_target("default_profile", lobby["id"])
    r = c.post("/api/auth/lock")
    assert r.json()["landed"]["id"] == lobby["id"] and r.json()["landed"]["shared"] is True
    assert c.get("/api/auth/me").json()["profile"]["id"] == lobby["id"]


def test_a_role_change_goes_through_the_role_route(world, owner):
    max_ = world.person(owner, "Max", "member", pin="5555")
    # 4.0: User -> Basic deletes content, so PATCH points at POST /api/people/{id}/role.
    r = owner.patch(f"/api/people/{max_['id']}", json={"role": "restricted"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "use_role_route"
    assert owner.patch(f"/api/people/{max_['id']}", json={"role": "user"}).status_code == 200   # same role


def test_disabling_and_deleting_a_profile_signs_it_out(world, owner):
    max_ = world.person(owner, "Max", "member", pin="5555")
    member = world.client()
    world.login(member, profile_id=max_["id"], pin="5555")
    assert owner.patch(f"/api/people/{max_['id']}", json={"disabled": True}).status_code == 200
    assert member.get("/api/auth/me").json()["profile"] is None
    assert world.login(world.client(), profile_id=max_["id"], pin="5555").status_code == 401
    # 4.0: deleting goes through POST /api/people/{id}/delete (counts confirmed first).
    r = owner.delete(f"/api/people/{max_['id']}")
    assert r.status_code == 409 and r.json()["error"]["code"] == "use_delete_route"


def test_a_changed_pin_ends_that_profiles_sessions_only(world, owner):
    a = world.person(owner, "Max", "member", pin="5555")
    b = world.person(owner, "Moe", "member", pin="6666")
    ca, cb = world.client(), world.client()
    world.login(ca, profile_id=a["id"], pin="5555")
    world.login(cb, profile_id=b["id"], pin="6666")
    assert owner.post(f"/api/people/{a['id']}/pin", json={"pin": "7777"}).status_code == 200
    assert ca.get("/api/auth/me").json()["profile"] is None
    assert cb.get("/api/auth/me").json()["profile"]["name"] == "Moe"
    assert world.login(world.client(), profile_id=a["id"], pin="7777").status_code == 200


def test_sessions_are_registered_and_forgotten(world, owner):
    mia = world.person(owner, "Mia", "restricted")
    c = world.client()
    world.login(c, profile_id=mia["id"])
    assert len(accounts.list_sessions(mia["id"])) == 1
    c.post("/api/auth/lock")
    assert accounts.list_sessions(mia["id"]) == []
    world.login(c, profile_id=mia["id"])
    assert owner.post(f"/api/people/{mia['id']}/sign-out").json()["sessions"] == 1
    assert accounts.list_sessions(mia["id"]) == []


def test_session_timeouts_follow_the_role_and_the_profile(world, owner):
    mia = world.person(owner, "Mia", "restricted")
    c = world.client()
    world.login(c, profile_id=mia["id"])

    def mia_session():
        return next(s for s in auth._sessions.values() if s.profile_id == mia["id"])

    mia_session().last_seen -= 1700                         # the restricted default is 1800 s
    assert c.get("/api/auth/status").json()["signed_in"] is True    # (this request slides the window)
    mia_session().last_seen -= 1900
    assert c.get("/api/auth/status").json()["signed_in"] is False
    # The session is gone and (4.0, no device binding) the device is nobody's.
    assert c.get("/api/auth/me").json()["kind"] == "none"
    # A per-profile override wins over the role default.
    assert owner.patch(f"/api/people/{mia['id']}", json={"session_timeout": 60}).status_code == 200
    world.login(c, profile_id=mia["id"])
    mia_session().last_seen -= 120
    assert c.get("/api/auth/status").json()["signed_in"] is False


# --------------------------------------------------------------------------- #
# The brute-force throttle: per (device, profile), per address, then global
# --------------------------------------------------------------------------- #


def _bad(client, profile_id, n):
    return [world_login(client, profile_id, "0000").status_code for _ in range(n)]


def world_login(client, profile_id, pin):
    return client.post("/api/auth/login", json={"profile_id": profile_id, "pin": pin})


def test_one_noisy_client_cannot_lock_everyone_out(world, owner):
    max_ = world.person(owner, "Max", "member", pin="5555")
    noisy = world.client(("203.0.113.99", 40000))
    codes = _bad(noisy, max_["id"], 7)
    assert 429 in codes and codes[:5] == [401] * 5
    # The noisy client is locked out - even with the right PIN ...
    assert world_login(noisy, max_["id"], "5555").status_code == 429
    # ... and a different device is not: the 2.x throttle was process-global.
    quiet = world.client(("203.0.113.50", 40001))
    assert world_login(quiet, max_["id"], "5555").status_code == 200


def test_failures_against_one_profile_do_not_lock_another_on_the_same_device(world, owner):
    a = world.person(owner, "Max", "member", pin="5555")
    b = world.person(owner, "Moe", "member", pin="6666")
    c = world.client(("203.0.113.99", 40000))
    _bad(c, a["id"], 6)
    assert world_login(c, a["id"], "5555").status_code == 429
    assert world_login(c, b["id"], "6666").status_code == 200


def test_the_unlock_route_is_throttled_too(world, owner):
    noisy = world.client(("203.0.113.99", 40000))
    codes = [noisy.post("/api/auth/unlock", json={"pin": "0000"}).status_code for _ in range(7)]
    assert 429 in codes
    r = noisy.post("/api/auth/unlock", json={"pin": "2468"})
    assert r.status_code == 429 and "retry_after" in r.json()
    assert world.client(("203.0.113.50", 40001)).post("/api/auth/unlock", json={"pin": "2468"}).status_code == 200


def test_the_global_ceiling_stops_a_distributed_guesser(world, owner, monkeypatch):
    monkeypatch.setattr(auth, "_GLOBAL_CEILING", 4)
    max_ = world.person(owner, "Max", "member", pin="5555")
    for i in range(4):
        world_login(world.client((f"203.0.113.{100 + i}", 40000)), max_["id"], "0000")
    late = world.client(("203.0.113.10", 40000))
    assert world_login(late, max_["id"], "5555").status_code == 429


def test_a_good_login_clears_that_clients_failures(world, owner):
    max_ = world.person(owner, "Max", "member", pin="5555")
    c = world.client(("203.0.113.99", 40000))
    _bad(c, max_["id"], 3)
    assert world_login(c, max_["id"], "5555").status_code == 200
    assert _bad(c, max_["id"], 4) == [401] * 4          # the count started again from zero


def test_device_binding_is_gone(world, owner):
    # 4.0: the bind route no longer exists; listing and forgetting devices stay.
    assert owner.put("/api/people/devices/this", json={"profile_id": None}).status_code in (404, 405)
    assert owner.get("/api/people/devices").status_code == 200


# --------------------------------------------------------------------------- #
# The activity summary (Basic profiles have no budgets or time limits any more)
# --------------------------------------------------------------------------- #


def _household_bot(world):
    from app import characters, config
    return next(b.id for b in config.load_bots() if not characters.is_personal(b))


def test_a_basic_profile_has_no_daily_limit(world, owner, monkeypatch):
    bot = _household_bot(world)
    mia = world.person(owner, "Mia", "basic", household_bots=[bot], grants=["dispatch_agents"])
    c = world.client(("203.0.113.50", 1))
    world.login(c, profile_id=mia["id"])
    codes = [c.post("/api/threads", json={"bot_id": bot}).status_code for _ in range(5)]
    assert codes == [200] * 5
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    monkeypatch.setattr(accounts, "today", lambda: tomorrow)
    assert c.get("/api/bots").status_code == 200


def test_the_owner_sees_an_activity_summary_and_the_profile_is_told(world, owner):
    mia = world.person(owner, "Mia", "basic")
    c = world.client()
    world.login(c, profile_id=mia["id"])
    # What the profile sees: the disclosure, and its own summary.
    me = c.get("/api/auth/me").json()
    assert "admin can see" in me["disclosure"].lower()
    assert me["disclosure_code"] == "activity_summary"     # the UI's locale key, not the English
    assert c.get("/api/me/activity").status_code == 404      # 4.0: no per-profile activity page
    # What the owner sees: counts, not content.
    act = owner.get("/api/people/activity", params={"profile_id": mia["id"]}).json()
    counts = act["summary"]["days"][0]["counts"]
    assert counts["login"] == 1
    assert all("content" not in e for e in act["recent"])
    # A Basic profile cannot read anyone's summary (view_activity is the Admins').
    assert c.get("/api/people/activity").status_code == 403
    # An Admin is not told it is being summarised.
    assert owner.get("/api/auth/me").json()["disclosure"] is None
    assert owner.get("/api/auth/me").json()["disclosure_code"] is None


def test_admin_actions_are_audited(world, owner):
    mia = world.person(owner, "Mia", "restricted")
    owner.patch(f"/api/people/{mia['id']}", json={"name": "Mia B"})
    owner.post(f"/api/people/{mia['id']}/pin", json={"pin": "4321"})
    kinds = [e["kind"] for e in owner.get("/api/people/audit").json()["entries"]]
    assert {"profile_created", "profile_updated", "pin_set"} <= set(kinds)


# --------------------------------------------------------------------------- #
# The machine API key
# --------------------------------------------------------------------------- #


def test_the_owner_and_admins_manage_the_api_key(world, owner):
    ada = world.person(owner, "Ada", "admin", password="admin-pass1")
    admin = world.client()
    world.login(admin, profile_id=ada["id"], password="admin-pass1")
    assert admin.get("/api/auth/api-token").status_code == 200
    r = owner.post("/api/auth/api-token", json={"rotate": True})
    token = r.json()["api_token"]
    assert len(token) >= 32
    info = owner.get("/api/auth/api-token").json()
    assert info["has_token"] is True and "api_token" not in info
    assert auth.verify_api_token(token)


def test_machine_scopes_can_be_narrowed(world, owner):
    token = owner.post("/api/auth/api-token", json={"rotate": True}).json()["api_token"]
    owner.post("/api/auth/api-token", json={"machine_scopes": ["machine:inject", "machine:threads"],
                                            "machine_loopback_trust": False})
    machine = world.client(("203.0.113.77", 40000), origin=False)
    h = {"X-API-Key": token}
    # inject is in scope ...
    r = machine.post("/api/inject", headers=h, json={"bot_id": "alpha", "role": "user", "content": "hi"})
    assert r.status_code == 200, r.text
    # ... avatar management is not.
    r = machine.get("/api/bots/alpha/avatar", headers=h)
    assert r.status_code == 403 and r.json()["error"]["code"] == "machine_scope_denied"
    # With loopback trust off, an on-box process without the key is refused.
    local = world.client(("127.0.0.1", 40001), origin=False)
    assert local.post("/api/inject", json={"bot_id": "alpha", "role": "user", "content": "x"}).status_code == 401


def test_machine_upload_needs_its_scope_and_the_key(world, owner, monkeypatch):
    """R1.5: an agent hands a document back by uploading it (then [[doc:id]]). The
    3.0 route table gave POST /api/upload no machine scope, so every machine caller
    got 401 signin_required, key or not."""
    token = owner.post("/api/auth/api-token", json={"rotate": True}).json()["api_token"]
    owner.post("/api/auth/api-token", json={"machine_loopback_trust": False})
    local = world.client(("127.0.0.1", 40002), origin=False)
    files = {"file": ("report.txt", b"hello from a cron", "text/plain")}
    r = local.post("/api/upload", headers={"X-API-Key": token}, files=files)
    assert r.status_code == 200, r.text
    assert r.json()["id"] and r.json()["name"] == "report.txt"
    # A machine's uploads share the 200 MB/day machine guard (an agent in a loop must
    # not fill the disk).
    monkeypatch.setattr(main, "MACHINE_UPLOAD_QUOTA", 64)
    big = {"file": ("big.txt", b"y" * 200, "text/plain")}
    assert local.post("/api/upload", headers={"X-API-Key": token}, files=big).status_code == 429
    # Without the key an on-box process is no machine (trust is off): it is a
    # signed-out device, which needs a sign-in to upload ...
    r = local.post("/api/upload", files=files)
    assert r.status_code == 401 and r.json()["error"]["code"] == "signin_required", r.text
    # ... and a WRONG key is refused outright, not downgraded.
    r = local.post("/api/upload", headers={"X-API-Key": "not-the-key"}, files=files)
    assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_api_key", r.text
    # ... and a key whose scopes leave upload out is refused by scope.
    owner.post("/api/auth/api-token", json={"machine_scopes": ["machine:inject"]})
    r = local.post("/api/upload", headers={"X-API-Key": token}, files=files)
    assert r.status_code == 403 and r.json()["error"]["code"] == "machine_scope_denied"


# --------------------------------------------------------------------------- #
# Dispatching an agent is its own permission
# --------------------------------------------------------------------------- #


def test_a_member_cannot_run_an_agent_until_the_owner_grants_it(world, owner, monkeypatch):
    from app import config, main
    turns = []

    async def fake_turn(thread_id, bot_id, text, opts=None):
        turns.append((thread_id, bot_id, opts))

    monkeypatch.setattr(main, "run_agent_turn", fake_turn)
    max_ = world.person(owner, "Max", "member", pin="5555")
    agent_bot = next(b.id for b in config.load_bots() if b.audience == "members")
    member = world.client()
    assert world.login(member, profile_id=max_["id"], pin="5555").status_code == 200
    # 4.0: an agent character is not usable at all without dispatch_agents (characters.may_use)
    r = member.post("/api/threads", json={"bot_id": agent_bot})
    assert r.status_code == 403
    assert owner.patch(f"/api/people/{max_['id']}", json={"grants": ["dispatch_agents"]}).status_code == 200
    assert world.login(member, profile_id=max_["id"], pin="5555").status_code == 200   # grant re-signs
    tid = member.post("/api/threads", json={"bot_id": agent_bot}).json()["id"]

    def send(cmid):
        got = []
        with member.websocket_connect("/ws") as ws:
            ws.receive_json()
            ws.send_json({"type": "send", "thread_id": tid, "text": "run it", "client_msg_id": cmid})
            ws.send_json({"type": "ping"})
            while True:
                f = ws.receive_json()
                if f.get("type") == "pong":
                    return got
                got.append(f)

    frames = send("a2")
    assert any(f.get("type") == "ack" and f.get("status") == "ok" for f in frames), frames
    import time
    for _ in range(40):                      # the turn is a background task on the app's loop
        if turns:
            break
        time.sleep(0.05)
    assert [t[1] for t in turns] == [agent_bot]
    assert turns[0][2] is None or turns[0][2].locked is False


# --------------------------------------------------------------------------- #
# The lock target (4.0): the profile list or the default profile
# --------------------------------------------------------------------------- #


def test_a_signed_out_device_sees_only_the_signin_page_with_the_profile_list(world, owner):
    mia = world.person(owner, "Mia", "restricted", household_bots=["alpha"])
    stranger = world.client(("203.0.113.9", 1))
    assert stranger.get("/").status_code == 200
    st = stranger.get("/api/auth/status").json()
    assert st["lock_target"] == "profile_list" and any(p["name"] == "Mia" for p in st["profiles"])
    r = stranger.get("/api/bots")
    assert r.status_code == 401 and r.json()["error"]["code"] == "signin_required"
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect), stranger.websocket_connect("/ws"):
        pass
    # Signing in as the Basic profile gives its view.
    assert world.login(stranger, profile_id=mia["id"]).status_code == 200
    assert stranger.get("/api/bots").status_code == 200


def test_a_profile_override_can_be_cleared_and_the_lock_screen_names_the_credential(world, owner):
    mia = world.person(owner, "Mia", "restricted", session_timeout=600)
    r = owner.patch(f"/api/people/{mia['id']}", json={"session_timeout": None})
    assert r.status_code == 200 and r.json()["session_timeout"] is None
    tiles = {t["name"]: t["credential"] for t in world.client(("203.0.113.9", 1)).get(
        "/api/auth/status").json()["profiles"]}
    assert tiles == {"Olive": "pin_or_password", "Mia": "none"}
    world.person(owner, "Max", "member", pin="5555")
    world.person(owner, "Ada", "admin", password="admin-pass1")
    tiles = {t["name"]: t["credential"] for t in owner.get("/api/auth/status").json()["profiles"]}
    assert tiles["Max"] == "pin" and tiles["Ada"] == "password"


def test_a_members_status_does_not_advertise_host_control_features(world, owner):
    world.person(owner, "Max", "member", pin="5555")
    member = world.client()
    world.login(member, name="Max", pin="5555")
    feats = member.get("/api/auth/status").json()["features"]
    assert feats["harness"] is False and feats["mail"] is False and feats["practice"] is False
    # And a signed-out device on a password-only install learns nothing about features.
    cfg = auth.load()
    assert cfg.pin_set                              # the fixture's owner has a PIN; clear it
    owner.post("/api/auth/setup", json={"new_pin": "", "current_pin": "2468"})
    stranger = world.client(("203.0.113.9", 1))
    assert set(stranger.get("/api/auth/status").json()["features"]) == {"extensions"}


def test_the_disclosure_matches_who_can_read_a_basic_profiles_chats(world, owner):
    """The line a Basic profile reads (people_api.DISCLOSURE) must say what ownership
    allows (4.0): a chat is its owner's - no other profile, Admins included, opens it."""
    from app import people_api
    mia = world.person(owner, "Mia", "restricted", household_bots=["alpha"], grants=["dispatch_agents"])
    leo = world.person(owner, "Leo", "restricted", household_bots=["alpha"], grants=["dispatch_agents"])
    world.person(owner, "Max", "member", pin="5555")
    c_mia = world.client(("203.0.113.70", 42000))
    assert world.login(c_mia, profile_id=mia["id"]).status_code == 200
    own = c_mia.post("/api/threads", json={"bot_id": "alpha"}).json()["id"]
    assert c_mia.get(f"/api/threads/{own}/messages").status_code == 200
    c_leo = world.client(("203.0.113.71", 42001))
    assert world.login(c_leo, profile_id=leo["id"]).status_code == 200
    c_max = world.client(("203.0.113.72", 42002))
    assert world.login(c_max, name="Max", pin="5555").status_code == 200
    stranger = world.client(("203.0.113.73", 42003))
    for who, c in (("another Basic profile", c_leo), ("a User", c_max), ("the Owner", owner)):
        assert c.get(f"/api/threads/{own}/messages").status_code == 404, who
    assert stranger.get(f"/api/threads/{own}/messages").status_code == 401
    text = people_api.DISCLOSURE.lower()
    assert "summary" in text and "private" in text


# --------------------------------------------------------------------------- #
# 3.2: a Member changes their own PIN (Settings -> Account)
# --------------------------------------------------------------------------- #


def test_a_member_changes_their_own_pin_with_the_current_one(world, owner):
    world.person(owner, "Max", "member", pin="5555")
    c = world.client()
    assert world.login(c, name="Max", pin="5555").status_code == 200
    r = c.post("/api/auth/pin", json={"current": "0000", "new": "7777"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "incorrect_credential"
    r = c.post("/api/auth/pin", json={"current": "5555", "new": "7777"})
    assert r.status_code == 200, r.text
    # This device stays signed in; the new PIN is the one that works.
    assert c.get("/api/auth/me").json()["profile"]["name"] == "Max"
    assert world.login(world.client(), name="Max", pin="5555").status_code == 401
    assert world.login(world.client(), name="Max", pin="7777").status_code == 200
    kinds = [e["kind"] for e in owner.get("/api/people/audit").json()["entries"]]
    assert "pin_changed" in kinds


def test_own_pin_refuses_a_weak_pin_and_the_owner_and_restricted(world, owner):
    world.person(owner, "Max", "member", pin="5555")
    world.person(owner, "Mia", "restricted")
    c = world.client()
    assert world.login(c, name="Max", pin="5555").status_code == 200
    assert c.post("/api/auth/pin", json={"current": "5555", "new": "12"}).status_code == 400
    # The Owner's PIN is the install PIN: its own route.
    r = owner.post("/api/auth/pin", json={"current": "2468", "new": "1357"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "owner_immutable"
    kid = world.client()
    assert world.login(kid, name="Mia").status_code == 200
    # 4.0: a Basic profile sets its own PIN too (a first PIN needs no current one)
    assert kid.post("/api/auth/pin", json={"current": "", "new": "1234"}).status_code == 200
    # A device nobody signed in on has no profile to change.
    assert world.client().post("/api/auth/pin", json={"current": "", "new": "1234"}).status_code in (401, 403)


def test_own_pin_attempts_are_throttled(world, owner):
    world.person(owner, "Max", "member", pin="5555")
    c = world.client()
    assert world.login(c, name="Max", pin="5555").status_code == 200
    codes = [c.post("/api/auth/pin", json={"current": "0000", "new": "7777"}).status_code for _ in range(12)]
    assert 429 in codes


# A PIN or password change re-issues this device's session (the change signs the
# profile out everywhere else); a remembered device must stay remembered, and a
# plain sign-in must not be promoted to one.


def _remembered_login(world, c, **who):
    return c.post("/api/auth/login", json={**who, "remember": True})


def test_own_pin_change_keeps_a_remembered_device_remembered(world, owner):
    auth.set_remember_days(30)
    world.person(owner, "Max", "member", pin="5555")
    c = world.client()
    r = _remembered_login(world, c, name="Max", pin="5555")
    assert r.status_code == 200 and r.json()["remembered"] is True
    r = c.post("/api/auth/pin", json={"current": "5555", "new": "7777"})
    assert r.status_code == 200, r.text
    assert r.json()["remembered"] is True
    assert c.get("/api/auth/status").json()["remembered"] is True
    # A device that signed in without "remember me" is not promoted by the change.
    plain = world.client()
    assert world.login(plain, name="Max", pin="7777").status_code == 200
    r = plain.post("/api/auth/pin", json={"current": "7777", "new": "8888"})
    assert r.status_code == 200 and r.json()["remembered"] is False


def test_password_change_keeps_a_remembered_device_remembered(world, owner):
    auth.set_remember_days(30)
    world.person(owner, "Ada", "admin", password="admin-pass1")
    c = world.client()
    r = _remembered_login(world, c, name="Ada", password="admin-pass1")
    assert r.status_code == 200 and r.json()["remembered"] is True
    r = c.post("/api/auth/password", json={"current": "admin-pass1", "new": "admin-pass2"})
    assert r.status_code == 200, r.text
    assert r.json()["remembered"] is True
    assert c.get("/api/auth/status").json()["remembered"] is True
    plain = world.client()
    assert world.login(plain, name="Ada", password="admin-pass2").status_code == 200
    r = plain.post("/api/auth/password", json={"current": "admin-pass2", "new": "admin-pass3"})
    assert r.status_code == 200 and r.json()["remembered"] is False
