"""4.0 ownership enforcement, the parts that need no running app (WP-CORE).

* the frame scoper (``main.scope_frame_for_principal``): no Admin short-circuit, a missing
  principal drops thread / roster / character / operator frames (Fable amendment 15), and
  roster frames go through ``characters.visible_to`` for everyone;
* push routing to the thread owner's devices and the re-bind on a profile switch;
* ``agents_ws.may_see_task``: a task is its dispatcher's, Admins included.

Run: cd backend && uv run pytest tests/test_ownership_unit.py
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app import accounts, agents_ws, config, main, ownership, policy, push
from app.contracts.permissions import Role


@pytest.fixture
def house(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.yaml")
    config._invalidate_bots_cache()
    accounts.reset_for_tests()
    owner = accounts.create_profile("Owner", Role.ADMIN, protected=True)
    admin2 = accounts.create_profile("Ann", Role.ADMIN)
    user1 = accounts.create_profile("Uma", Role.USER)
    user2 = accounts.create_profile("Una", Role.USER)
    basic = accounts.create_profile("Bo", Role.BASIC, household_bots=["helpful"])
    _write_roster(tmp_path, user1.id)
    config._invalidate_bots_cache()
    ownership.forget_all()
    yield SimpleNamespace(owner=owner, admin2=admin2, user1=user1, user2=user2, basic=basic,
                          p=lambda prof: policy.profile_principal(accounts.get_profile(prof.id)))
    ownership.forget_all()
    accounts.reset_for_tests()
    config._invalidate_bots_cache()


def _write_roster(root, owner_id):
    import yaml
    prov = {"providers": [{"provider": "openai", "model": "m", "base_url": "http://x"}]}
    (root / "config.yaml").write_text(yaml.safe_dump({"bots": [
        {"id": "helpful", "name": "Helpful", "emoji": "x", "advisor": prov},
        {"id": "plain", "name": "Plain", "emoji": "y", "advisor": prov},
        {"id": "pa1b2c3d4e5f6", "name": "Mine", "emoji": "z", "scope": "personal",
         "owner_profile": owner_id, "advisor": prov},
    ]}))


# --------------------------------------------------------------------------- #
# The frame scoper
# --------------------------------------------------------------------------- #

def _msg(tid, bot="helpful"):
    return {"type": "message", "thread_id": tid, "bot_id": bot,
            "message": {"id": "m1", "thread_id": tid, "content": "SENTINEL", "role": "assistant"}}


def test_a_thread_frame_reaches_only_the_owner_admins_included(house):
    ownership.remember("t-user1", house.user1.id)
    frame = _msg("t-user1")
    assert main.scope_frame_for_principal(frame, house.p(house.user1)) == frame
    for other in (house.owner, house.admin2, house.user2, house.basic):
        assert main.scope_frame_for_principal(frame, house.p(other)) is None, other.name
    # A machine reads only the protected Admin's chats.
    assert main.scope_frame_for_principal(frame, policy.machine_principal("api_token")) is None
    assert main.scope_frame_for_principal(frame, policy.open_principal()) == frame


def test_an_unattributable_thread_frame_is_dropped(house):
    frame = _msg("never-cached")
    assert main.scope_frame_for_principal(frame, house.p(house.owner)) is None
    # ... except a neutral reply to the socket that asked about it.
    err = {"type": "error", "thread_id": "never-cached", "message": "Thread not found"}
    assert main.scope_frame_for_principal(err, house.p(house.user1), targeted=True) == err
    assert main.scope_frame_for_principal(err, house.p(house.user1)) is None


def test_a_missing_principal_gets_nothing_about_threads_bots_or_operators(house):
    ownership.remember("t-owner", house.owner.id)
    for frame in (_msg("t-owner"), {"type": "bots", "bots": [{"id": "helpful"}]},
                  {"type": "hello", "bots": []}, {"type": "harness_state"},
                  {"type": "reaction_pool"}, {"type": "task.event", "task_id": "x"},
                  {"type": "thinking", "bot_id": "helpful"},
                  {"type": "app:jobboard:job_created", "job": {}}):
        assert main.scope_frame_for_principal(frame, None) is None, frame["type"]
    pong = {"type": "pong"}
    assert main.scope_frame_for_principal(pong, None) == pong


def test_roster_frames_hide_a_personal_character_from_everyone_but_its_owner(house):
    frame = {"type": "bots", "bots": [{"id": "helpful"}, {"id": "plain"},
                                      {"id": "pa1b2c3d4e5f6"}]}

    def ids(pr):
        out = main.scope_frame_for_principal(frame, pr)
        return {b["id"] for b in out["bots"]}

    assert ids(house.p(house.user1)) == {"helpful", "plain", "pa1b2c3d4e5f6"}
    assert ids(house.p(house.owner)) == {"helpful", "plain"}          # no Admin short-circuit
    assert ids(house.p(house.admin2)) == {"helpful", "plain"}
    assert ids(house.p(house.user2)) == {"helpful", "plain"}
    assert ids(house.p(house.basic)) == {"helpful"}                   # its explicit list


def test_a_frame_about_a_foreign_personal_character_is_dropped_for_an_admin(house):
    frame = {"type": "error", "bot_id": "pa1b2c3d4e5f6", "message": "x"}
    assert main.scope_frame_for_principal(frame, house.p(house.owner)) is None
    assert main.scope_frame_for_principal(frame, house.p(house.user1)) == frame
    # a conversation frame naming no conversation is dropped for everyone (fail closed)
    assert main.scope_frame_for_principal({"type": "thinking", "bot_id": "pa1b2c3d4e5f6"},
                                          house.p(house.user1)) is None


def test_operator_and_app_frames_need_their_permission(house):
    for t in ("harness_state", "reaction_pool", "avatar_pool"):
        assert main.scope_frame_for_principal({"type": t}, house.p(house.user1)) is None
        assert main.scope_frame_for_principal({"type": t}, house.p(house.admin2)) is not None
    job = {"type": "app:jobboard:job_updated", "job": {"id": "j"}}
    assert main.scope_frame_for_principal(job, house.p(house.user1)) is None


def test_the_scoper_has_no_admin_short_circuit_in_its_source():
    import inspect
    src = inspect.getsource(main.scope_frame_for_principal) + inspect.getsource(main._frame_owner_allows)
    for word in ("is_admin", "is_operator", ".operator", "is_protected"):
        assert word not in src, word


# --------------------------------------------------------------------------- #
# Push
# --------------------------------------------------------------------------- #

def test_for_owner_keeps_only_that_profiles_devices(house):
    subs = {"k1": {"profile_id": house.user1.id}, "k2": {"profile_id": house.owner.id},
            "k3": {}, "open:abc": {}}
    assert set(push.for_owner(subs, house.user1.id)) == {"k1"}
    # an open: key (no PIN install) counts as the protected Admin's; a key with no profile
    # gets nothing (fail closed)
    assert set(push.for_owner(subs, house.owner.id)) == {"k2", "open:abc"}


def test_rebind_moves_the_subscription_to_the_new_profile_or_drops_it(house):
    good = {"endpoint": "https://fcm.googleapis.com/fcm/send/abc",
            "keys": {"p256dh": "B" + "A" * 86, "auth": "A" * 22}}
    try:
        push.subscribe("old", good, profile_id=house.user1.id)
    except push.PushError:
        pytest.skip("push validation refused the synthetic subscription")
    assert push.rebind("old", "new", house.user2.id) == "moved"
    subs = push._load_subs()
    assert "old" not in subs and subs["new"]["profile_id"] == house.user2.id
    assert push.rebind("new", None, house.user1.id) == "dropped"
    assert push._load_subs() == {}
    assert push.rebind("missing", "x", house.user1.id) == ""


@pytest.mark.asyncio
async def test_notify_requires_an_owner_and_pings_only_that_owner(house, monkeypatch):
    sent = []
    monkeypatch.setattr(push, "_load_subs", lambda: {
        "a": {"profile_id": house.user1.id, "endpoint": "e1"},
        "b": {"profile_id": house.owner.id, "endpoint": "e2"}})
    monkeypatch.setattr(push, "_alive", lambda k: True)
    monkeypatch.setattr(push, "_send", lambda sub, data, topic: sent.append(sub["endpoint"]))
    assert await push.notify("reply", "t1", "x") == 0                # no owner named: nobody
    push._last_sent.clear()
    await push.notify("reply", "t1", "x", owner=house.user1.id)
    assert sent == ["e1"]


# --------------------------------------------------------------------------- #
# Tasks
# --------------------------------------------------------------------------- #

def test_a_task_is_its_dispatchers_admins_included(house):
    accounts.update_profile(house.user1.id, grants=["dispatch_agents"])
    u1 = house.p(house.user1)
    assert agents_ws.may_see_task(u1, house.user1.id)
    assert not agents_ws.may_see_task(house.p(house.owner), house.user1.id)
    assert not agents_ws.may_see_task(house.p(house.admin2), house.user1.id)
    assert not agents_ws.may_see_task(house.p(house.user2), house.user1.id)
    # a task dispatched while the install was open ("owner") is the protected Admin's
    assert agents_ws.may_see_task(house.p(house.owner), "owner")
    assert not agents_ws.may_see_task(house.p(house.admin2), "owner")
    assert not agents_ws.may_see_task(policy.machine_principal("api_token"), house.owner.id)
    assert agents_ws.may_see_task(policy.open_principal(), house.user1.id)
    assert not agents_ws.may_see_task(None, house.user1.id)
