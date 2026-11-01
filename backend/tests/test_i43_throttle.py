"""4.3 WP-A1: one credential throttle (#3), IPv6 /64 buckets, credential-less picks and
session caps (#4).

The security-critical case is (j): at the global ceiling an UNKNOWN device is refused
before its credential is looked at, however right it is; only a remembered device bound to
that profile (or this computer) is verified. That is what keeps #3 from loosening the gate.
"""
from __future__ import annotations

import pytest

from app import auth, auth_attempts, policy


@pytest.fixture(autouse=True)
def _shipped_fresh_install_default(monkeypatch):
    """conftest models upgraded installs; these tests check the shipped default (False)."""
    from app import auth as _auth
    monkeypatch.setattr(_auth, "FRESH_INSTALL_LOOPBACK_TRUST", False)


OWNER_PIN = "2468"


@pytest.fixture
def owner(world):
    c = world.create_owner()
    c.post("/api/auth/remember-config", json={"days": 30})
    return c


def _v6(n: int, host: int = 1) -> tuple[str, int]:
    return (f"2001:db8:{n:x}::{host:x}", 42000 + n)


def _fill_ceiling(world, monkeypatch) -> None:
    """Drive the install to the global ceiling from four different /64s (each address is
    cut off by its own (device, profile) bucket after 5 failures, so the ceiling is set to
    4 x 5 here; the shipped one is 60)."""
    monkeypatch.setattr(auth, "_GLOBAL_CEILING", 20)
    for n in range(4):
        c = world.client(_v6(0x100 + n))
        for _ in range(5):
            assert c.post("/api/auth/unlock", json={"pin": "0000"}).status_code in (401, 429)
    assert auth._fail_count >= 20 and auth.throttle_state()[1] > 0


def _remembered_owner(world):
    """The owner's own phone: signed in with "keep this device signed in"."""
    phone = world.client(("198.51.100.20", 43000))
    r = phone.post("/api/auth/unlock", json={"pin": OWNER_PIN, "remember": True})
    assert r.status_code == 200 and r.json()["remembered"] is True, r.text
    return phone


def _move(world, client, addr):
    """The same browser (cookies and all) seen from another address."""
    c = world.client(addr)
    for name, value in client.cookies.items():
        c.cookies.set(name, value)
    return c


# --------------------------------------------------------------------------- #
# (b) (j) the global ceiling
# --------------------------------------------------------------------------- #


def test_b_remembered_device_signs_in_at_the_ceiling(world, owner, monkeypatch):
    phone = _remembered_owner(world)
    _fill_ceiling(world, monkeypatch)
    there = _move(world, phone, _v6(0x200))           # a 5th address
    r = there.post("/api/auth/unlock", json={"pin": OWNER_PIN})
    assert r.status_code == 200, r.text
    there = _move(world, phone, _v6(0x201))
    r = there.post("/api/auth/unlock", json={"pin": "9999"})
    assert r.status_code == 429


def test_j_unknown_device_is_refused_before_verify_at_the_ceiling(world, owner, monkeypatch):
    phone = _remembered_owner(world)
    _fill_ceiling(world, monkeypatch)
    calls = []
    real = auth.verify_pin

    def spy(pin):
        calls.append(pin)
        return real(pin)

    monkeypatch.setattr(auth, "verify_pin", spy)
    stranger = world.client(_v6(0x300))
    r = stranger.post("/api/auth/unlock", json={"pin": OWNER_PIN})
    assert r.status_code == 429 and r.json()["error"]["code"] == "too_many_attempts"
    assert calls == [], "the credential was verified for an unknown device at the ceiling"
    # The same through the profile login route.
    oid = auth.ensure_owner_for_pin().id
    r = stranger.post("/api/auth/login", json={"profile_id": oid, "pin": OWNER_PIN})
    assert r.status_code == 429 and calls == []
    # A remembered device bound to that profile: verified.
    there = _move(world, phone, _v6(0x301))
    assert there.post("/api/auth/unlock", json={"pin": OWNER_PIN}).status_code == 200
    assert calls == [OWNER_PIN]
    there = _move(world, phone, _v6(0x302))
    assert there.post("/api/auth/unlock", json={"pin": "1111"}).status_code == 429
    assert calls == [OWNER_PIN, "1111"]


def test_j_guarded_verify_spy_directly(world, owner, monkeypatch):
    """The API A4/A13 code against: refused before verify() for an unknown caller."""
    _fill_ceiling(world, monkeypatch)
    from starlette.requests import Request
    scope = {"type": "http", "method": "POST", "path": "/x", "headers": [],
             "client": ("203.0.113.200", 1), "query_string": b""}
    called = []
    with pytest.raises(auth_attempts.AttemptRefused) as ei:
        auth_attempts.guarded_verify(Request(scope), scope="reveal", subject="someone",
                                     verify=lambda: called.append(1) or True)
    assert called == [] and ei.value.status == 429 and ei.value.code == "too_many_attempts"


def test_a_remembered_device_of_another_profile_gets_no_pass(world, owner, monkeypatch):
    max_ = world.person(owner, "Max", "user", pin="5555")
    kid = world.client(("198.51.100.30", 43001))
    r = kid.post("/api/auth/login", json={"profile_id": max_["id"], "pin": "5555", "remember": True})
    assert r.status_code == 200 and r.json()["remembered"]
    _fill_ceiling(world, monkeypatch)
    there = _move(world, kid, _v6(0x400))
    assert there.post("/api/auth/unlock", json={"pin": OWNER_PIN}).status_code == 429


def test_this_computer_is_verified_at_the_ceiling_but_not_through_a_proxy(world, owner, monkeypatch):
    _fill_ceiling(world, monkeypatch)
    here = world.client(("127.0.0.1", 43002))
    assert here.post("/api/auth/unlock", json={"pin": OWNER_PIN}).status_code == 200
    served = world.client(("127.0.0.1", 43003))
    r = served.post("/api/auth/unlock", headers={"X-Forwarded-For": "203.0.113.70"},
                    json={"pin": OWNER_PIN})
    assert r.status_code == 429


# --------------------------------------------------------------------------- #
# (c) one /64 is one bucket
# --------------------------------------------------------------------------- #


def test_c_two_addresses_in_one_64_share_a_bucket(world, owner, monkeypatch):
    assert auth.ipv6_bucket("2001:db8:1:2::5") == auth.ipv6_bucket("2001:db8:1:2:ffff::9")
    assert auth.ipv6_bucket("2001:db8:1:2::5") != auth.ipv6_bucket("2001:db8:1:3::5")
    assert auth.ipv6_bucket("192.0.2.7") == "192.0.2.7"
    assert auth.ipv6_bucket("::ffff:192.0.2.7") == "192.0.2.7"
    monkeypatch.setattr(auth, "_IP_LOCKOUT_AFTER", 6)
    # Six failures spread over six addresses (and fresh device keys) in ONE /64 ...
    for h in range(6):
        world.client((f"2001:db8:5:6::{h + 1:x}", 44000 + h)).post(
            "/api/auth/unlock", json={"pin": "0000"})
    # ... lock a seventh address in the same /64, even with the right PIN ...
    r = world.client(("2001:db8:5:6::77", 44100)).post("/api/auth/unlock", json={"pin": OWNER_PIN})
    assert r.status_code == 429
    # ... and not one next door.
    r = world.client(("2001:db8:5:7::77", 44101)).post("/api/auth/unlock", json={"pin": OWNER_PIN})
    assert r.status_code == 200


# --------------------------------------------------------------------------- #
# (d) credential-less picks are counted; (d)(o) session caps
# --------------------------------------------------------------------------- #


def test_d_pinless_switches_are_capped_at_twenty(world, owner):
    kid = world.person(owner, "Pip", "basic")
    c = world.client(("203.0.113.61", 45000))
    codes = [c.post("/api/auth/switch", json={"profile_id": kid["id"]}).status_code
             for _ in range(25)]
    assert codes[:20] == [200] * 20 and codes[20] == 429, codes
    r = c.post("/api/auth/switch", json={"profile_id": kid["id"]})
    assert r.json()["error"]["code"] == "too_many_attempts" and int(r.headers["Retry-After"]) > 0
    # Another address is not affected.
    other = world.client(("203.0.113.62", 45001))
    assert other.post("/api/auth/switch", json={"profile_id": kid["id"]}).status_code == 200


def test_d_the_65th_session_evicts_the_oldest(world, owner):
    max_ = world.person(owner, "Max", "user", pin="5555")
    pid = max_["id"]
    tokens = [auth.issue_session(profile_id=pid) for _ in range(64)]
    first = tokens[0]
    assert auth.get_session(first) is not None
    newest = auth.issue_session(profile_id=pid)
    assert auth.get_session(first) is None, "the oldest session was not evicted"
    assert auth.get_session(newest) is not None and auth.get_session(tokens[1]) is not None
    assert sum(1 for s in auth._sessions.values() if s.profile_id == pid) == 64


def test_d_the_callers_own_session_is_never_evicted(world, owner):
    max_ = world.person(owner, "Max", "user", pin="5555")
    pid = max_["id"]
    mine = auth.issue_session(profile_id=pid)
    for _ in range(63):
        auth.issue_session(profile_id=pid)
    auth.issue_session(profile_id=pid, keep_token=mine)
    assert auth.get_session(mine) is not None


def test_o_all_remembered_refuses_the_65th_without_evicting(world, owner, monkeypatch):
    monkeypatch.setattr(auth, "_MAX_TRUSTED", 1000)
    max_ = world.person(owner, "Max", "user", pin="5555")
    pid = max_["id"]
    tokens = [auth.issue_session(persistent=True, profile_id=pid) for _ in range(64)]
    assert all(auth._sessions[t].persistent for t in tokens)
    c = world.client(("203.0.113.63", 45002))
    r = c.post("/api/auth/login", json={"profile_id": pid, "pin": "5555"})
    assert r.status_code == 429 and r.json()["error"]["code"] == "too_many_sessions", r.text
    assert all(auth.get_session(t) is not None for t in tokens)


def test_the_install_wide_cap_evicts_too(world, owner, monkeypatch):
    monkeypatch.setattr(auth, "_MAX_SESSIONS", 10)
    max_ = world.person(owner, "Max", "user", pin="5555")
    for _ in range(12):
        auth.issue_session(profile_id=max_["id"])
    assert len(auth._sessions) <= 10


# --------------------------------------------------------------------------- #
# Every credential route is on the one throttle
# --------------------------------------------------------------------------- #


def test_recovery_and_claimed_setup_are_throttled(world, owner):
    c = world.client(("203.0.113.64", 45003))
    codes = [c.post("/api/auth/recover", json={"code": "LC-XXXX-XXXX"}).status_code
             for _ in range(7)]
    assert codes[:5] == [401] * 5 and 429 in codes
    # The claimed-install PIN change confirms the current PIN through the throttle too.
    codes = [owner.post("/api/auth/setup", json={"current_pin": "0000", "new_pin": "1357"}).status_code
             for _ in range(7)]
    assert codes[:5] == [403] * 5 and 429 in codes
    assert policy.claimed()
