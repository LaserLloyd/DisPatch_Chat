"""Profiles, roles, sign-in, throttling and the People & Profiles API (3.0).

Everything runs through the real app (``world`` fixture, tests/conftest.py): the gate,
the policy engine and the SQLite account layer are all live; only the clock and the
agent backend are not.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app import accounts, auth, limits


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
    assert st["claimed"] and st["authenticated"] and st["role"] == "owner"
    assert st["profile"]["name"] == "Olive" and st["needs_password"] is False
    assert st["pin_set"] is True
    owner_row = accounts.get_owner()
    assert owner_row is not None and owner_row.has_password and owner_row.has_pin
    # The Owner's PIN is the install PIN: the old unlock flow still works.
    c2 = world.client()
    assert c2.post("/api/auth/unlock", json={"pin": "2468"}).status_code == 200
    assert c2.get("/api/auth/me").json()["role"] == "owner"


def test_setup_with_a_password_only_leaves_no_pin(world):
    c = world.client()
    assert c.post("/api/auth/setup", json={"password": "owner-pass1"}).status_code == 200
    assert c.get("/api/auth/status").json()["pin_set"] is False
    # Claimed all the same: a stranger is Restricted, not owner.
    stranger = world.client()
    assert stranger.get("/api/auth/me").json()["role"] == "restricted"
    assert stranger.get("/api/bots/all").status_code == 403


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


def test_an_admin_needs_the_owner_to_have_a_password_first(world):
    c = world.client()
    assert c.post("/api/auth/setup", json={"new_pin": "2468"}).status_code == 200   # PIN-only owner
    r = c.post("/api/people", json={"name": "Ada", "role": "admin", "password": "admin-pass1"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "password_required"
    # The owner sets a password (confirmed with the install PIN), then it works.
    r = c.post("/api/auth/password", json={"current_pin": "2468", "new": "owner-pass1"})
    assert r.status_code == 200, r.text
    r = c.post("/api/people", json={"name": "Ada", "role": "admin", "password": "admin-pass1"})
    assert r.status_code == 201


def test_a_member_needs_a_pin_and_a_restricted_profile_does_not(world, owner):
    assert owner.post("/api/people", json={"name": "Max", "role": "member"}).status_code == 400
    assert owner.post("/api/people", json={"name": "Max", "role": "member", "pin": "5555"}).status_code == 201
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
        accounts.update_profile(me.id, role="admin")
    with pytest.raises(accounts.AccountError):
        accounts.create_profile("Second", "owner")


def test_a_restricted_profile_can_never_be_granted_management(world, owner):
    r = owner.post("/api/people", json={"name": "Mia", "role": "restricted",
                                        "grants": ["manage_settings"]})
    assert r.status_code == 400
    ok = owner.post("/api/people", json={"name": "Mia", "role": "restricted", "grants": ["use_images"]})
    assert ok.status_code == 201
    r = owner.patch(f"/api/people/{ok.json()['id']}", json={"grants": ["admin_system"]})
    assert r.status_code == 400


def test_budgets_are_validated(world, owner):
    bad = [{"minutes_per_day": 0}, {"quiet_hours": "late"}, {"turns": -1}, {"nonsense": 1}]
    for b in bad:
        assert owner.post("/api/people", json={"name": "Mia", "role": "restricted",
                                               "budgets": b}).status_code == 400, b
    ok = owner.post("/api/people", json={"name": "Mia", "role": "restricted",
                                         "budgets": {"minutes_per_day": 30, "quiet_hours": "20:30-07:00",
                                                     "turns": 40, "threads": 5, "upload_mb": 10}})
    assert ok.status_code == 201


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
    assert world.login(c, profile_id=max_["id"], pin="5555").json()["role"] == "member"
    assert world.login(c, profile_id=mia["id"]).json()["role"] == "restricted"        # no PIN: just pick it
    assert world.login(c, profile_id=pin_kid["id"]).status_code == 401               # has a PIN: needs it
    assert world.login(c, profile_id=pin_kid["id"], pin="1212").status_code == 200
    # The Owner: password or the install PIN.
    me = accounts.get_owner()
    assert world.login(c, profile_id=me.id, password="owner-pass1").json()["role"] == "owner"
    assert world.login(c, profile_id=me.id, pin="2468").json()["role"] == "owner"
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
    assert c.get("/api/auth/me").json()["role"] == "restricted"                      # still Mia
    r = c.post("/api/auth/switch", json={"name": "Ada", "password": "admin-pass1"})
    assert r.status_code == 200 and c.get("/api/auth/me").json()["role"] == "admin"
    assert c.get("/api/people").status_code == 200


def test_lock_returns_to_restricted(world, owner):
    assert owner.get("/api/people").status_code == 200
    owner.post("/api/auth/lock")
    assert owner.get("/api/people").status_code == 403
    assert owner.get("/api/auth/me").json()["role"] == "restricted"


def test_a_role_change_ends_the_old_sessions(world, owner):
    max_ = world.person(owner, "Max", "member", pin="5555")
    member = world.client()
    assert world.login(member, profile_id=max_["id"], pin="5555").status_code == 200
    assert member.get("/api/auth/me").json()["role"] == "member"
    assert owner.patch(f"/api/people/{max_['id']}", json={"role": "restricted"}).status_code == 200
    assert member.get("/api/auth/me").json()["role"] == "restricted"       # session died -> anonymous
    assert member.get("/api/auth/me").json()["profile"] is None


def test_disabling_and_deleting_a_profile_signs_it_out(world, owner):
    max_ = world.person(owner, "Max", "member", pin="5555")
    member = world.client()
    world.login(member, profile_id=max_["id"], pin="5555")
    assert owner.patch(f"/api/people/{max_['id']}", json={"disabled": True}).status_code == 200
    assert member.get("/api/auth/me").json()["profile"] is None
    assert world.login(world.client(), profile_id=max_["id"], pin="5555").status_code == 401
    assert owner.delete(f"/api/people/{max_['id']}").status_code == 200
    assert max_["id"] not in {p["id"] for p in owner.get("/api/people").json()["profiles"]}


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
    # The session is gone, the device is still Mia's (2026-10-19: no way out of her limits).
    me = c.get("/api/auth/me").json()
    assert (me["kind"], me["profile"]["id"]) == ("profile", mia["id"]), me
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


# --------------------------------------------------------------------------- #
# Devices bound to a default profile
# --------------------------------------------------------------------------- #


def test_a_bound_device_opens_as_its_profile(world, owner):
    mia = world.person(owner, "Mia", "restricted")
    r = owner.put("/api/people/devices/this", json={"profile_id": mia["id"], "label": "Kitchen tablet"})
    assert r.status_code == 200 and r.json()["default_profile_id"] == mia["id"]
    tablet = world.client()
    tablet.cookies.set("lc_device", owner.cookies.get("lc_device"))
    me = tablet.get("/api/auth/me").json()
    assert me["role"] == "restricted" and me["profile"]["name"] == "Mia" and me["disclosure"]
    # An unrelated browser (no device cookie) is plain anonymous Restricted.
    assert world.client().get("/api/auth/me").json()["profile"] is None


def test_admins_and_the_owner_cannot_be_a_device_default(world, owner):
    ada = world.person(owner, "Ada", "admin", password="admin-pass1")
    assert owner.put("/api/people/devices/this", json={"profile_id": ada["id"]}).status_code == 409
    me = accounts.get_owner()
    assert owner.put("/api/people/devices/this", json={"profile_id": me.id}).status_code == 409


def test_unbinding_and_forgetting_a_device(world, owner):
    mia = world.person(owner, "Mia", "restricted")
    dev = owner.put("/api/people/devices/this", json={"profile_id": mia["id"]}).json()["id"]
    tablet = world.client()
    tablet.cookies.set("lc_device", owner.cookies.get("lc_device"))
    assert tablet.get("/api/auth/me").json()["profile"]["name"] == "Mia"
    assert owner.put("/api/people/devices/this", json={"profile_id": None}).status_code == 200
    assert tablet.get("/api/auth/me").json()["profile"] is None
    assert owner.delete(f"/api/people/devices/{dev}").status_code == 200
    assert dev not in {d["id"] for d in owner.get("/api/people/devices").json()["devices"]}


def test_a_device_bound_member_is_a_member_not_an_admin(world, owner):
    max_ = world.person(owner, "Max", "member", pin="5555")
    owner.put("/api/people/devices/this", json={"profile_id": max_["id"]})
    tablet = world.client()
    tablet.cookies.set("lc_device", owner.cookies.get("lc_device"))
    assert tablet.get("/api/auth/me").json()["role"] == "member"
    assert tablet.get("/api/people").status_code == 403
    assert tablet.get("/api/bots").status_code == 200


# --------------------------------------------------------------------------- #
# Restricted safeguards: budgets, time limits, the activity summary
# --------------------------------------------------------------------------- #


def _safe_bot(world):
    from app import config
    return next(b.id for b in config.load_bots() if b.safe)


def test_daily_budgets_belong_to_the_profile_and_survive_a_restart(world, owner):
    mia = world.person(owner, "Mia", "restricted", budgets={"threads": 2})
    kit = world.person(owner, "Kit", "restricted", budgets={"threads": 1})
    bot = _safe_bot(world)
    a, b = world.client(("203.0.113.50", 1)), world.client(("203.0.113.50", 2))   # SAME address
    world.login(a, profile_id=mia["id"])
    world.login(b, profile_id=kit["id"])
    codes = [a.post("/api/threads", json={"bot_id": bot}).status_code for _ in range(3)]
    assert codes == [200, 200, 429]
    # Kit has her own allowance although she shares Mia's address.
    assert [b.post("/api/threads", json={"bot_id": bot}).status_code for _ in range(2)] == [200, 429]
    # Persisted: forget every in-memory cache and the count is still there.
    accounts.reset_for_tests()
    assert a.post("/api/threads", json={"bot_id": bot}).status_code == 429
    assert accounts.usage_today(mia["id"]).get("action:thread") == 2


def test_the_budget_resets_with_the_day(world, owner, monkeypatch):
    mia = world.person(owner, "Mia", "restricted", budgets={"threads": 1})
    bot = _safe_bot(world)
    c = world.client()
    world.login(c, profile_id=mia["id"])
    assert c.post("/api/threads", json={"bot_id": bot}).status_code == 200
    assert c.post("/api/threads", json={"bot_id": bot}).status_code == 429
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    monkeypatch.setattr(accounts, "today", lambda: tomorrow)
    assert c.post("/api/threads", json={"bot_id": bot}).status_code == 200


def test_time_limits_and_quiet_hours(world, owner):
    mia = world.person(owner, "Mia", "restricted", budgets={"minutes_per_day": 10})
    c = world.client()
    world.login(c, profile_id=mia["id"])
    assert c.get("/api/bots").status_code == 200
    accounts.usage_add(mia["id"], "active_seconds", 10 * 60)
    r = c.get("/api/bots")
    assert r.status_code == 403 and r.json()["error"]["code"] == "time_limit"
    # Auth routes still answer, so the child can see why and sign out.
    assert c.get("/api/auth/me").status_code == 200
    # Quiet hours: a window that contains "now" blocks; one that does not, does not.
    now = datetime.now()
    inside = f"{(now - timedelta(minutes=5)).strftime('%H:%M')}-{(now + timedelta(minutes=5)).strftime('%H:%M')}"
    outside = f"{(now + timedelta(hours=2)).strftime('%H:%M')}-{(now + timedelta(hours=3)).strftime('%H:%M')}"
    accounts.update_profile(mia["id"], budgets={"quiet_hours": inside})
    c2 = world.client()
    world.login(c2, profile_id=mia["id"])
    r = c2.get("/api/bots")
    assert r.status_code == 403 and r.json()["error"]["code"] == "quiet_hours"
    accounts.update_profile(mia["id"], budgets={"quiet_hours": outside})
    c3 = world.client()
    world.login(c3, profile_id=mia["id"])
    assert c3.get("/api/bots").status_code == 200


def _at(hour, minute):
    return datetime(2026, 1, 1, hour, minute)  # noqa: DTZ001 - local wall-clock time is the point


def test_quiet_hours_window_arithmetic():
    assert limits.in_quiet_hours("20:30-07:00", _at(22, 0))
    assert limits.in_quiet_hours("20:30-07:00", _at(6, 59))
    assert not limits.in_quiet_hours("20:30-07:00", _at(7, 0))
    assert not limits.in_quiet_hours("20:30-07:00", _at(12, 0))
    assert limits.in_quiet_hours("13:00-14:00", _at(13, 30))
    assert not limits.in_quiet_hours("garbage", _at(13, 30))


def test_the_owner_sees_an_activity_summary_and_the_child_is_told(world, owner):
    mia = world.person(owner, "Mia", "restricted", budgets={"threads": 5})
    c = world.client()
    world.login(c, profile_id=mia["id"])
    bot = _safe_bot(world)
    assert c.post("/api/threads", json={"bot_id": bot}).status_code == 200
    assert c.post("/api/threads", json={"bot_id": bot}).status_code == 200
    # What the child sees: the disclosure, and its own summary.
    me = c.get("/api/auth/me").json()
    assert "owner can see" in me["disclosure"].lower()
    mine = c.get("/api/me/activity").json()
    assert mine["visible_to_owner"] is True and mine["today"]["action:thread"] == 2
    # What the owner sees: counts, not content.
    act = owner.get("/api/people/activity", params={"profile_id": mia["id"]}).json()
    counts = act["summary"]["days"][0]["counts"]
    assert counts["login"] == 1 and counts["thread"] == 2
    assert all("content" not in e for e in act["recent"])
    # A restricted child cannot read anyone's summary.
    assert c.get("/api/people/activity").status_code == 403


def test_admin_actions_are_audited(world, owner):
    mia = world.person(owner, "Mia", "restricted")
    owner.patch(f"/api/people/{mia['id']}", json={"budgets": {"turns": 9}})
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
    tid = owner.post("/api/threads", json={"bot_id": agent_bot}).json()["id"]
    member = world.client()
    assert world.login(member, profile_id=max_["id"], pin="5555").status_code == 200

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

    frames = send("a1")
    err = next(f for f in frames if f.get("type") == "error")
    assert err["code"] == "permission_denied" and err["required_permission"] == "dispatch_agents"
    assert turns == []
    assert owner.patch(f"/api/people/{max_['id']}", json={"grants": ["dispatch_agents"]}).status_code == 200
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
# anonymous_access: none
# --------------------------------------------------------------------------- #


def test_anonymous_access_none_shows_a_signed_out_device_only_the_signin_page(world, owner):
    mia = world.person(owner, "Mia", "restricted")
    cfg = auth.load()
    cfg.anonymous_access = "none"
    auth._write(cfg)
    auth._bust_cache()
    stranger = world.client(("203.0.113.9", 1))
    assert stranger.get("/").status_code == 200
    st = stranger.get("/api/auth/status").json()
    assert st["anonymous_access"] == "none" and any(p["name"] == "Mia" for p in st["profiles"])
    r = stranger.get("/api/bots")
    assert r.status_code == 401 and r.json()["error"]["code"] == "signin_required"
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect), stranger.websocket_connect("/ws"):
        pass
    # Signing in as the Restricted profile restores the Restricted view.
    assert world.login(stranger, profile_id=mia["id"]).status_code == 200
    assert stranger.get("/api/bots").status_code == 200
    # A device bound to a profile is not anonymous.
    owner.put("/api/people/devices/this", json={"profile_id": mia["id"]})
    tablet = world.client(("203.0.113.10", 1))
    tablet.cookies.set("lc_device", owner.cookies.get("lc_device"))
    assert tablet.get("/api/bots").status_code == 200


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
