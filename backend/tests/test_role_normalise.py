"""4.0 role vocabulary: 3.x words are input only, normalised through LEGACY_ROLE; the stored
column keeps the 3.x words and only accounts.py maps them (BUILD-PLAN-v2 §1.1)."""
from __future__ import annotations

import sqlite3

import pytest

from app import accounts, policy
from app.contracts.permissions import LEGACY_ROLE, Role, normalise_role, role_rank


@pytest.mark.parametrize(("given", "want"), [
    ("admin", Role.ADMIN), ("user", Role.USER), ("basic", Role.BASIC),
    ("owner", Role.ADMIN), ("member", Role.USER), ("restricted", Role.BASIC),
    (" Member ", Role.USER), ("BASIC", Role.BASIC), (Role.USER, Role.USER),
])
def test_normalise(given, want):
    assert normalise_role(given) is want


@pytest.mark.parametrize("bad", ["", None, "guest", "superuser", "owners"])
def test_normalise_refuses_unknown_words(bad):
    with pytest.raises(ValueError):
        normalise_role(bad)


def test_legacy_map_and_rank():
    assert LEGACY_ROLE == {"owner": Role.ADMIN, "admin": Role.ADMIN, "member": Role.USER,
                           "restricted": Role.BASIC}
    assert [role_rank(r) for r in ("restricted", "member", "admin", "owner")] == [0, 1, 2, 2]


def test_stored_words_map_both_ways():
    assert accounts.ROLE_OF_STORED == {"owner": Role.ADMIN, "admin": Role.ADMIN,
                                       "member": Role.USER, "restricted": Role.BASIC}
    assert accounts.STORED_OF_ROLE == {Role.ADMIN: "admin", Role.USER: "member",
                                       Role.BASIC: "restricted"}
    assert accounts.stored_role(Role.ADMIN, protected=True) == "owner"
    assert set(accounts.STORED_ROLES) == set(accounts.ROLE_OF_STORED)


def test_profiles_carry_roles_and_protected_never_stored_words():
    owner = accounts.create_profile("Owner", Role.ADMIN, protected=True)
    adm = accounts.create_profile("Ann", "admin")
    usr = accounts.create_profile("Uma", "member")          # a 3.x word: normalised
    bas = accounts.create_profile("Bo", Role.BASIC)
    assert (owner.role, owner.protected) == (Role.ADMIN, True)
    assert (adm.role, adm.protected) == (Role.ADMIN, False)
    assert (usr.role, bas.role) == (Role.USER, Role.BASIC)
    assert accounts.get_protected_admin().id == owner.id == accounts.get_owner().id
    for p in accounts.list_profiles():
        assert p.public()["role"] in {"admin", "user", "basic"}
        assert p.public()["household_bots"] == "all"
    con = sqlite3.connect(accounts.db_path())
    stored = dict(con.execute("SELECT name, role FROM profiles").fetchall())
    con.close()
    assert stored == {"Owner": "owner", "Ann": "admin", "Uma": "member", "Bo": "restricted"}


def test_the_owner_word_still_makes_the_protected_admin():
    p = accounts.create_profile("Owner", "owner")
    assert p.protected and p.role is Role.ADMIN
    with pytest.raises(accounts.AccountError) as e:
        accounts.create_profile("Second", Role.ADMIN, protected=True)
    assert e.value.code == "owner_exists"
    with pytest.raises(accounts.AccountError):
        accounts.create_profile("Kid", Role.BASIC, protected=True)


def test_protected_admin_cannot_be_demoted_disabled_or_deleted():
    p = accounts.create_profile("Owner", Role.ADMIN, protected=True)
    for kw in ({"role": Role.USER}, {"role": "member"}, {"disabled": True}):
        with pytest.raises(accounts.AccountError) as e:
            accounts.update_profile(p.id, **kw)
        assert e.value.code == "owner_immutable"
    with pytest.raises(accounts.AccountError):
        accounts.delete_profile(p.id)
    # Admin -> Admin keeps the stored owner word (and the protection)
    assert accounts.update_profile(p.id, role="admin").protected


def test_role_changes_store_the_mapped_word():
    p = accounts.create_profile("Uma", Role.USER)
    q = accounts.update_profile(p.id, role=Role.BASIC)
    assert q.role is Role.BASIC and q.epoch == p.epoch + 1
    with pytest.raises(accounts.AccountError) as e:
        accounts.update_profile(p.id, role="owner")
    assert e.value.code == "owner_exists"
    con = sqlite3.connect(accounts.db_path())
    assert con.execute("SELECT role FROM profiles WHERE id=?", (p.id,)).fetchone()[0] == "restricted"
    con.close()


def test_household_bots_round_trip():
    p = accounts.create_profile("Bo", Role.BASIC, household_bots=["a", "b", "a"])
    assert p.household_bots == ["a", "b"]
    assert accounts.update_profile(p.id, household_bots="all").household_bots == "all"
    with pytest.raises(accounts.AccountError):
        accounts.update_profile(p.id, household_bots=[1, 2])


def test_principals_use_the_new_vocabulary():
    owner = accounts.create_profile("Owner", Role.ADMIN, protected=True)
    adm = accounts.create_profile("Ann", Role.ADMIN)
    bas = accounts.create_profile("Bo", Role.BASIC)
    po, pa, pb = (policy.profile_principal(x) for x in (owner, adm, bas))
    assert (po.role, po.is_admin, po.is_protected) == ("admin", True, True)
    assert (pa.role, pa.is_admin, pa.is_protected) == ("admin", True, False)
    assert (pb.role, pb.is_admin, pb.is_protected) == ("basic", False, False)
    assert not hasattr(pb, "is_restricted"), "the Restricted tier is gone (WP-DEL)"
    assert policy.open_principal().is_protected and policy.open_principal().is_admin
    assert not hasattr(po, "is_operator") and not hasattr(po, "is_owner")
