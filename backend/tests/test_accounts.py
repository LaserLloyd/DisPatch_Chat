"""Unit tests for the account layer, credential hashing, the file lock and the policy."""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from app import _filelock, accounts, credentials, policy
from app.contracts.permissions import (
    ROLE_DEFAULTS,
    ROLE_NEVER,
    MachineScope,
    Permission,
    Role,
    role_rank,
    tool_permission,
)

BACKEND = str(Path(__file__).resolve().parents[1])

# --------------------------------------------------------------------------- #
# credentials
# --------------------------------------------------------------------------- #


def test_the_shipped_argon2_cost_is_not_weakened(monkeypatch):
    """The suite runs at a token cost (conftest); the shipped numbers are the OWASP
    minimum for argon2id and must not drift down unnoticed."""
    assert credentials.DEFAULT_MEMORY_KIB >= 19_456
    assert credentials.DEFAULT_PASSES >= 2
    monkeypatch.delenv("DISPATCH_ARGON2_MEMORY_KIB")
    monkeypatch.delenv("DISPATCH_ARGON2_PASSES")
    assert credentials._cost() == (19_456, 2, 1)
    h = credentials.hash_secret("correct horse")
    assert h.startswith("$argon2id$v=19$m=19456,t=2,p=1$")  # scrub-ok: hash FORMAT, no secret
    assert credentials.verify_secret("correct horse", h)
    assert not credentials.needs_rehash(h)


def test_hash_and_verify_round_trip():
    h = credentials.hash_secret("1234")
    assert h != credentials.hash_secret("1234")                  # salted
    assert credentials.verify_secret("1234", h)
    assert not credentials.verify_secret("1235", h)
    assert not credentials.verify_secret("", h)
    assert not credentials.verify_secret("1234", None)
    assert not credentials.verify_secret("1234", "plaintext")
    assert not credentials.verify_secret("1234", "$argon2id$garbage")  # scrub-ok: hash FORMAT, no secret


def test_a_stored_hash_claiming_a_trivial_cost_is_not_trusted():
    h = credentials.hash_secret("1234")
    cheap = h.replace("m=8,t=1,p=1", "m=0,t=0,p=0")
    assert cheap != h
    assert not credentials.verify_secret("1234", cheap)


def test_the_legacy_pbkdf2_format_still_verifies_and_asks_for_a_rehash():
    salt = "ab" * 16
    stored = credentials.legacy_string(credentials.legacy_pbkdf2("4321", salt, 1000), salt, 1000)
    assert credentials.verify_secret("4321", stored)
    assert not credentials.verify_secret("0000", stored)
    assert credentials.needs_rehash(stored)
    assert credentials.needs_rehash(credentials.hash_secret("x")) is False


def test_pin_and_password_minimums():
    assert credentials.check_pin("123")
    assert credentials.check_pin("1234") is None
    assert credentials.check_password("short")
    assert credentials.check_password("long enough") is None


# --------------------------------------------------------------------------- #
# the file lock
# --------------------------------------------------------------------------- #


def test_the_instance_lock_is_exclusive_across_processes(tmp_path):
    lock = tmp_path / ".instance.lock"
    probe = "\n".join([
        "import sys, errno",
        f"sys.path.insert(0, {BACKEND!r})",
        "from app import _filelock",
        f"fh = open({str(lock)!r}, 'w')",
        "try:",
        "    _filelock.try_lock_exclusive(fh)",
        "    print('LOCKED')",
        "except OSError as e:",
        "    print('BUSY', e.errno in (errno.EACCES, errno.EAGAIN))",
    ])
    with lock.open("w") as fh:
        _filelock.try_lock_exclusive(fh, lock)
        out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=60)
        assert out.stdout.strip() == "BUSY True", out.stdout + out.stderr


def test_the_same_process_may_take_the_lock_twice(tmp_path):
    """Several app instances share a data dir inside the test run (and uvicorn --reload)."""
    lock = tmp_path / ".instance.lock"
    a, b = lock.open("w"), lock.open("w")
    try:
        _filelock.try_lock_exclusive(a, lock)
        _filelock.try_lock_exclusive(b, lock)
    finally:
        a.close()
        b.close()


@pytest.mark.skipif(_filelock.IS_WINDOWS, reason="LockFileEx handle semantics differ between threads")
def test_the_blocking_lock_serialises_two_threads(tmp_path):
    path = tmp_path / "f.lock"
    order = []
    fd1 = os.open(path, os.O_WRONLY | os.O_CREAT)
    _filelock.lock_exclusive(fd1)

    def other():
        fd2 = os.open(path, os.O_WRONLY | os.O_CREAT)
        _filelock.lock_exclusive(fd2)
        order.append("second")
        _filelock.unlock(fd2)
        os.close(fd2)
    t = threading.Thread(target=other)
    t.start()
    t.join(0.3)
    assert order == []                       # blocked behind the first holder
    order.append("first")
    _filelock.unlock(fd1)
    os.close(fd1)
    t.join(5)
    assert order == ["first", "second"]


# --------------------------------------------------------------------------- #
# accounts
# --------------------------------------------------------------------------- #


def test_schema_is_additive_and_idempotent(tmp_path, monkeypatch):
    from app import config
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    accounts.reset_for_tests()
    with accounts.connect() as con:
        con.execute("CREATE TABLE IF NOT EXISTS threads (id TEXT)")
    accounts.reset_for_tests()
    with accounts.connect() as con:                    # a second pass changes nothing
        names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert set(accounts.TABLES) <= names and "threads" in names


def test_exactly_one_owner_and_unique_names(world):
    o = accounts.create_profile("Olive", "owner")
    with pytest.raises(accounts.AccountError) as e:
        accounts.create_profile("Second", "owner")
    assert e.value.code == "owner_exists"
    with pytest.raises(accounts.AccountError) as e:
        accounts.create_profile("olive", "member")
    assert e.value.code == "profile_name_taken"
    with pytest.raises(sqlite3.IntegrityError), accounts.connect() as con:        # the DB itself refuses it too
        con.execute("INSERT INTO profiles (id,name,role,created) VALUES ('x','Other','owner','now')")
    assert accounts.get_owner().id == o.id


def test_updates_bump_the_epoch_only_when_they_must(world):
    p = accounts.create_profile("Max", "member", pin_hash="x")
    e0 = p.epoch
    assert accounts.update_profile(p.id, avatar="a.png", budgets={"turns": 3}).epoch == e0
    assert accounts.update_profile(p.id, role="restricted").epoch == e0 + 1
    assert accounts.update_profile(p.id, disabled=True).epoch == e0 + 2
    assert accounts.update_profile(p.id, pin_hash="y").epoch == e0 + 3


def test_devices_bind_only_to_members_and_restricted(world):
    admin = accounts.create_profile("Ada", "admin")
    kid = accounts.create_profile("Mia", "restricted")
    accounts.ensure_device("dev1", "UA")
    with pytest.raises(accounts.AccountError) as e:
        accounts.bind_device("dev1", admin.id)
    assert e.value.code == "device_bind_role"
    d = accounts.bind_device("dev1", kid.id, "Kitchen")
    assert d.default_profile_id == kid.id and accounts.default_profile_for_device("dev1").id == kid.id
    accounts.update_profile(kid.id, disabled=True)
    assert accounts.default_profile_for_device("dev1") is None       # a disabled profile is not a default
    accounts.update_profile(kid.id, disabled=False)
    accounts.delete_profile(kid.id)                                   # ON DELETE SET NULL
    assert accounts.get_device("dev1").default_profile_id is None


def test_charging_a_budget_is_atomic_and_never_overshoots(world):
    ok = [accounts.usage_charge("s", "turn", 5) for _ in range(8)]
    assert ok == [True] * 5 + [False] * 3
    assert accounts.usage_get("s", "turn") == 5
    # Concurrent chargers share the limit exactly.
    results = []

    def work():
        results.append(accounts.usage_charge("t", "turn", 10))
    ts = [threading.Thread(target=work) for _ in range(25)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert sum(results) == 10 and accounts.usage_get("t", "turn") == 10


def test_usage_map_keeps_the_dict_spelling_working(world):
    m = accounts.UsageMap("upload_bytes")
    assert m.get("1.2.3.4", 0) == 0
    m["1.2.3.4"] = 7
    assert m.get("1.2.3.4") == 7 and "1.2.3.4" in m
    m.clear()
    assert m.get("1.2.3.4", 0) == 0
    a = accounts.UsageMap("action:", tuple_keys=True)
    accounts.usage_charge("ip1", "action:turn", 3)
    assert a.get(("ip1", "turn")) == 1
    a.clear()
    assert a.get(("ip1", "turn"), 0) == 0


# --------------------------------------------------------------------------- #
# the role -> permission matrix and the decision function
# --------------------------------------------------------------------------- #


def test_the_matrix():
    # 4.0: three roles (the full cell-by-cell table is tests/test_permissions_table.py).
    admin, user, basic = (ROLE_DEFAULTS[r] for r in (Role.ADMIN, Role.USER, Role.BASIC))
    assert admin == set(Permission)                    # 2026-10-19: Admin is full
    assert basic < user < admin
    assert Permission.DISPATCH_AGENTS not in user
    assert not (basic & ROLE_NEVER[Role.BASIC]) and not (user & ROLE_NEVER[Role.USER])
    assert [role_rank(r) for r in ("restricted", "member", "admin", "owner")] == [0, 1, 2, 2]


def _prof(role, grants=(), denies=()):
    return accounts.Profile(id="p", name="n", role=role, grants=list(grants), denies=list(denies))


def test_grants_and_denies_stay_inside_the_role_ceiling():
    assert "dispatch_agents" in policy.effective_permissions(_prof("member", grants=["dispatch_agents"]))
    assert "use_voice" not in policy.effective_permissions(_prof("member", denies=["use_voice"]))
    kid = policy.effective_permissions(_prof("restricted", grants=["manage_settings", "use_images",
                                                                   tool_permission("weather")]))
    # 4.0: tool panels are Admin-only, so a Basic profile can no longer be granted one.
    assert "manage_settings" not in kid and "use_images" in kid and "use_tools:weather" not in kid
    adm = policy.effective_permissions(_prof("admin"))
    assert "admin_system" in adm                       # an Admin holds host control itself
    own = policy.effective_permissions(_prof("owner", denies=["manage_users"]))
    assert "manage_users" in own                       # the owner cannot be restricted


def test_authorize_decisions():
    kid = policy.profile_principal(_prof("restricted"))
    member = policy.profile_principal(_prof("member"))
    owner = policy.profile_principal(_prof("owner"))
    anon = policy.anonymous()
    R = policy.R
    assert policy.authorize(anon, policy.PUBLIC).allowed
    assert policy.authorize(kid, R(Permission.USE_CHAT)).allowed
    d = policy.authorize(kid, R(Permission.MANAGE_SETTINGS))
    assert (d.allowed, d.status, d.code) == (False, 403, "safe_mode")
    d = policy.authorize(member, R(Permission.MANAGE_SETTINGS))
    assert (d.allowed, d.code) == (False, "permission_denied")
    assert policy.authorize(owner, R(Permission.ADMIN_SYSTEM)).allowed
    # scoped: let through, flagged so the handler narrows
    s = policy.authorize(kid, R(Permission.USE_TOOLS, scoped=True))
    assert s.allowed and s.scoped
    # blanket use_tools satisfies a parametrised one; nothing else does (4.0: Admins only)
    assert owner.can(tool_permission("weather")) and not member.can(tool_permission("weather"))
    assert not kid.can(tool_permission("weather"))
    # machines hold scopes, never roles
    m = policy.machine_principal("test", {MachineScope.INJECT.value})
    assert policy.authorize(m, R(Permission.MANAGE_AGENTS, machine=[MachineScope.INJECT])).allowed
    assert not policy.authorize(m, R(Permission.MANAGE_AGENTS, machine=[MachineScope.DAILY])).allowed
    assert not policy.authorize(m, R(Permission.USE_CHAT)).allowed
    # an open install allows everything
    assert policy.authorize(policy.open_principal(), R(Permission.ADMIN_SYSTEM)).allowed
    # a route that declares nothing is refused to people
    assert not policy.authorize(owner, policy.Requirement()).allowed
