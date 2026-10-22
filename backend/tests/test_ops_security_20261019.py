"""Ops check of the 3.0 security surfaces (2026-10-19), one test per finding.

S-1  a socket opened by a device bound to a default profile (the kitchen tablet, no session)
     kept the profile as it was at the handshake: quiet hours or budgets the Owner set later,
     disabling the profile or unbinding the device never reached it
S-2  the recovery code on a PIN-only install with other profiles removed the only Owner
     credential: the install fell back to "unclaimed", every other device (the recoverer's
     included) got setup_required, and claiming it again with a password answered
     owner_exists because the credential-less owner row was still there

Run: cd backend && uv run pytest tests/test_ops_security_20261019.py
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

import pytest

from app import accounts, auth, policy

LAN = ("203.0.113.61", 41001)


def _inside_now() -> str:
    now = datetime.now()
    return f"{(now - timedelta(minutes=5)).strftime('%H:%M')}-{(now + timedelta(minutes=5)).strftime('%H:%M')}"


# S-1 ------------------------------------------------------------------------ #

def _bound_tablet(world):
    owner = world.create_owner(world.client())
    mia = world.person(owner, "Mia", "restricted")
    tablet = world.client(LAN)
    assert world.login(tablet, name="Olive", password="owner-pass1").status_code == 200
    r = tablet.put("/api/people/devices/this", json={"profile_id": mia["id"]})
    assert r.status_code == 200, r.text
    assert tablet.post("/api/auth/lock").status_code == 200
    me = tablet.get("/api/auth/me").json()
    assert (me["kind"], me["name"]) == ("profile", "Mia"), me
    return owner, mia, tablet


# S-2 ------------------------------------------------------------------------ #

@pytest.fixture
def closed_install(monkeypatch):
    """The 3.0 default: an unclaimed install is this computer's only."""
    monkeypatch.delenv("DISPATCH_ALLOW_OPEN_INSTALL", raising=False)


def _pin_only_household(world):
    owner = world.client()
    assert owner.post("/api/auth/setup", json={"new_pin": "2468"}).status_code == 200
    world.person(owner, "Mia", "restricted")
    code = re.search(r"LC-[A-Z0-9-]+", (world.tmp / "RECOVERY-CODE.txt").read_text()).group(0)
    return owner, code


def test_recovery_with_a_household_needs_a_new_pin(world, closed_install):
    _owner, code = _pin_only_household(world)
    phone = world.client(LAN)
    r = phone.post("/api/auth/recover", json={"code": code})
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "recovery_needs_new_pin"
    assert policy.claimed() and auth.verify_pin("2468")        # nothing was removed
    # the household still works: signing in still does (4.0: a signed-out device is nobody)
    assert world.client(LAN).get("/api/auth/status").json()["claimed"] is True


def test_recovery_with_a_new_pin_keeps_the_install_claimed(world, closed_install):
    _owner, code = _pin_only_household(world)
    phone = world.client(LAN)
    r = phone.post("/api/auth/recover", json={"code": code, "new_pin": "1357"})
    assert r.status_code == 200, r.text
    assert policy.claimed()
    me = phone.get("/api/auth/me").json()
    assert (me["kind"], me["role"], me["protected"]) == ("profile", "admin", True), me
    assert phone.get("/api/people").status_code == 200          # straight in, from the network
    assert auth.verify_pin("1357") and not auth.verify_pin("2468")
    assert not auth.verify_recovery(code)                        # the used code is spent
    assert world.client(LAN).post("/api/auth/recover", json={"code": code, "new_pin": "9999"}).status_code == 401


def test_recovery_without_profiles_still_removes_the_lock(world, closed_install):
    owner = world.client()
    assert owner.post("/api/auth/setup", json={"new_pin": "2468"}).status_code == 200
    code = re.search(r"LC-[A-Z0-9-]+", (world.tmp / "RECOVERY-CODE.txt").read_text()).group(0)
    r = world.client().post("/api/auth/recover", json={"code": code})
    assert r.status_code == 200 and r.json()["pin_set"] is False, r.text


def test_setup_claims_a_credential_less_owner_row(world, closed_install):
    _owner, _code = _pin_only_household(world)
    (world.tmp / "security.yaml").unlink()                       # "delete the file to drop the lock"
    auth._bust_cache()
    assert not policy.claimed()
    assert accounts.get_owner() is not None                      # the row outlived its PIN
    local = world.client()
    r = local.post("/api/auth/setup", json={"password": "fresh-owner-pass1"})
    assert r.status_code == 200, r.text
    assert policy.claimed()
    assert len([p for p in accounts.list_profiles() if p.protected]) == 1
    assert world.login(world.client(LAN), name="Owner", password="fresh-owner-pass1").status_code == 200  # scrub-ok: throwaway test fixture
