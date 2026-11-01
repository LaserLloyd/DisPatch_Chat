"""The pure half of 4.0 ownership (Foundation, Fable amendment 16): who may read a thread.

The route wiring is WP-CORE's; these pin the decision functions it will call.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import accounts, ownership, policy
from app.contracts.permissions import Role


@pytest.fixture
def people():
    accounts.reset_for_tests()
    owner = accounts.create_profile("Owner", Role.ADMIN, protected=True)
    admin2 = accounts.create_profile("Ann", Role.ADMIN)
    user = accounts.create_profile("Uma", Role.USER)
    basic = accounts.create_profile("Bo", Role.BASIC)
    ownership.forget_all()
    return SimpleNamespace(owner=owner, admin2=admin2, user=user, basic=basic,
                           p=lambda prof: policy.profile_principal(prof))


def test_a_chat_is_its_owners_for_every_role(people):
    for prof in (people.owner, people.admin2, people.user, people.basic):
        me = people.p(prof)
        assert ownership.may_see_thread(me, prof.id)
        assert ownership.may_see_thread(me, prof.id, write=True)
        for other in (people.owner, people.admin2, people.user, people.basic):
            if other.id != prof.id:
                assert not ownership.may_see_thread(me, other.id), (prof.name, other.name)


def test_null_owner_rows_are_the_protected_admins(people):
    assert ownership.effective_owner(None) == people.owner.id
    assert ownership.may_see_thread(people.p(people.owner), None)
    assert not ownership.may_see_thread(people.p(people.admin2), None)
    assert ownership.protected_admin_id() == ownership.owner_id() == people.owner.id


def test_machines_read_only_the_protected_admins_chats(people):
    m = policy.machine_principal("api_token")
    assert ownership.may_see_thread(m, people.owner.id)
    assert ownership.may_see_thread(m, None)
    for other in (people.admin2, people.user, people.basic):
        assert not ownership.may_see_thread(m, other.id)
    assert ownership.readable_owner_ids(m) == {people.owner.id, None}


def test_open_install_reads_everything_and_unknown_reads_nothing(people):
    assert ownership.may_see_thread(policy.open_principal(), people.user.id)
    assert ownership.readable_owner_ids(policy.open_principal()) is None
    assert not ownership.may_see_thread(None, people.user.id)
    assert ownership.readable_owner_ids(None) == set()
    assert not ownership.may_see_thread(SimpleNamespace(kind="weird"), people.user.id)


def test_nobody_reads_nothing(people):
    guest = policy.nobody()
    for owner in (people.owner.id, people.basic.id, None):
        assert not ownership.may_see_thread(guest, owner)
    assert ownership.readable_owner_ids(guest) == set()
    assert ownership.owner_filter_sql(guest) == ("0=1", [])


def test_owner_filter_sql(people):
    sql, params = ownership.owner_filter_sql(people.p(people.user))
    assert sql == "(t.owner_profile_id IN (?))" and params == [people.user.id]
    sql, params = ownership.owner_filter_sql(people.p(people.owner), alias="")
    assert sql == "(owner_profile_id IN (?) OR owner_profile_id IS NULL)" and params == [people.owner.id]
    assert ownership.owner_filter_sql(policy.open_principal()) == ("1=1", [])


def test_a_pre_accounts_session_owns_what_the_protected_admin_owns(people):
    legacy = policy.Principal(kind="profile", role="admin", protected=True)
    assert ownership.may_see_thread(legacy, people.owner.id)
    unprotected = policy.Principal(kind="profile", role="admin")
    assert not ownership.may_see_thread(unprotected, people.owner.id)


class _Db:
    def __init__(self, rows):
        self.rows = rows

    async def get_thread(self, tid):
        own = self.rows.get(tid, ...)
        return None if own is ... else SimpleNamespace(id=tid, owner_profile_id=own)

    async def get_message(self, mid):
        return SimpleNamespace(id=mid, thread_id="t1") if mid == "m1" else None


def test_require_thread_and_message_answer_404_alike(people):
    db = _Db({"t1": people.user.id})
    u, b = people.p(people.user), people.p(people.basic)
    assert asyncio.run(ownership.require_thread(db, u, "t1")).id == "t1"
    for args in ((db, b, "t1"), (db, u, "nope"), (db, u, None)):
        with pytest.raises(HTTPException) as e:
            asyncio.run(ownership.require_thread(*args))
        assert (e.value.status_code, e.value.detail) == (404, ownership.NOT_FOUND)
    msg, th = asyncio.run(ownership.require_message(db, u, "m1"))
    assert (msg.id, th.id) == ("m1", "t1")
    for who, mid in ((b, "m1"), (u, "m2")):
        with pytest.raises(HTTPException) as e:
            asyncio.run(ownership.require_message(db, who, mid))
        assert (e.value.status_code, e.value.detail) == (404, ownership.MESSAGE_NOT_FOUND)


def test_the_frame_owner_cache(people):
    ownership.forget_all()
    assert not ownership.is_known(ownership.cached_owner("t9"))
    db = _Db({"t9": people.basic.id})
    asyncio.run(ownership.prime_for_frame(db, {"type": "message", "message": {"thread_id": "t9"}}))
    assert ownership.cached_owner("t9") == people.basic.id
    asyncio.run(ownership.prime_for_frame(db, {"thread_id": "gone"}))
    assert not ownership.is_known(ownership.cached_owner("gone"))
    assert ownership.frame_thread_id({"thread": {"id": "x"}}) == "x"
    assert ownership.frame_thread_id({"type": "bots"}) is None


def test_corpus_profile(people):
    assert ownership.corpus_profile_id(None) == people.owner.id
    assert ownership.corpus_profile_id({"corpus_profile": people.user.id}) == people.user.id
    assert ownership.corpus_profile_id({"corpus_profile": "missing"}) == people.owner.id
