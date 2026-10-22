"""Per-profile character visibility holds on BROADCAST frames, not only on the roster.

``characters.may_use`` / ``visible_to`` (4.0: the profile's ``household_bots`` list, personal
characters, the agents permission) filter ``GET /api/bots`` and the WebSocket ``hello``; every
later ``bots`` / ``thread_*`` / ``message`` broadcast goes through
``main.scope_frame_for_principal`` (installed as ``manager.scoper``) per connection, so a
profile left off a character's list never learns its new name or avatar.
Run: cd backend && uv run pytest tests/test_ws_profile_visibility.py
"""
from __future__ import annotations

import pytest

from app import accounts, main

LAN = ("203.0.113.60", 41000)


def _drain(ws) -> list[dict]:
    """Every frame queued on ``ws`` up to a ping sentinel."""
    ws.send_json({"type": "ping"})
    got = []
    while True:
        f = ws.receive_json()
        if f.get("type") == "pong":
            return got
        got.append(f)


def _bot_ids(frames: list[dict], ftype: str) -> set[str]:
    return {b["id"] for f in frames if f.get("type") == ftype for b in f.get("bots", [])}


@pytest.fixture
def people(world):
    owner = world.create_owner(world.client())
    # alpha / beta are agent characters in the test roster, hence dispatch_agents.
    mia = world.person(owner, "Mia", "restricted", household_bots=["alpha"],
                       grants=["dispatch_agents"])
    world.person(owner, "Max", "member", pin="5555", household_bots=["beta"],
                 grants=["dispatch_agents"])
    # The extra callers ride the owner's portal (one event loop, as in production).
    from agents_world import sibling
    kid = sibling(world, owner, LAN)
    assert world.login(kid, profile_id=mia["id"]).status_code == 200
    member = sibling(world, owner, ("203.0.113.61", 41001))
    assert world.login(member, name="Max", pin="5555").status_code == 200
    return {"owner": owner, "kid": kid, "member": member}


def test_the_roster_and_hello_hide_a_character_off_the_profiles_list(people):
    member_ids = {b["id"] for b in people["member"].get("/api/bots").json()["bots"]}
    kid_ids = {b["id"] for b in people["kid"].get("/api/bots").json()["bots"]}
    assert "alpha" not in member_ids and "beta" in member_ids
    assert "alpha" in kid_ids and "beta" not in kid_ids
    with people["member"].websocket_connect("/ws") as ws:
        hello = ws.receive_json()
        assert "alpha" not in {b["id"] for b in hello["bots"]}


def test_a_bots_broadcast_after_an_edit_is_narrowed_per_socket(people):
    with people["member"].websocket_connect("/ws") as m_ws, people["kid"].websocket_connect("/ws") as k_ws:
        m_ws.receive_json()
        k_ws.receive_json()
        # The Owner renames both characters: the Bot Manager's save broadcasts the whole roster.
        r = people["owner"].patch("/api/bots/alpha", json={"name": "Alpha Secret"})
        assert r.status_code == 200, r.text
        r = people["owner"].patch("/api/bots/beta", json={"name": "Beta Secret"})
        assert r.status_code == 200, r.text
        m_frames, k_frames = _drain(m_ws), _drain(k_ws)
    assert _bot_ids(m_frames, "bots") and _bot_ids(k_frames, "bots")
    assert "alpha" not in _bot_ids(m_frames, "bots") and "beta" in _bot_ids(m_frames, "bots")
    assert "beta" not in _bot_ids(k_frames, "bots") and "alpha" in _bot_ids(k_frames, "bots")
    assert "Alpha Secret" not in str(m_frames)
    assert "Beta Secret" not in str(k_frames)


def test_thread_and_message_frames_of_someone_elses_chat_never_arrive(people):
    owner = people["owner"]
    with people["member"].websocket_connect("/ws") as m_ws:
        m_ws.receive_json()
        theirs = owner.post("/api/threads", json={"bot_id": "beta", "title": "Owner's own"}).json()
        mine = people["member"].post("/api/threads", json={"bot_id": "beta", "title": "Max's own"}).json()
        assert owner.patch(f"/api/threads/{theirs['id']}", json={"title": "Renamed secret"}).status_code == 200
        people["member"].portal.call(main.manager.broadcast, {
            "type": "message", "thread_id": theirs["id"], "bot_id": "beta",
            "message": {"id": "z", "content": "for the owner only", "role": "assistant"}})
        people["member"].portal.call(main.manager.broadcast, {
            "type": "message", "thread_id": mine["id"], "bot_id": "beta",
            "message": {"id": "y", "content": "for Max", "role": "assistant"}})
        frames = _drain(m_ws)
    kinds = [(f.get("type"), (f.get("thread") or {}).get("id") or f.get("thread_id")) for f in frames]
    assert ("thread_created", mine["id"]) in kinds
    assert ("message", mine["id"]) in kinds
    assert all(tid != theirs["id"] for _, tid in kinds), kinds
    assert "secret" not in str(frames) and "for the owner only" not in str(frames)


def test_get_messages_over_the_socket_is_the_owners_only(people):
    owner = people["owner"]
    theirs = owner.post("/api/threads", json={"bot_id": "beta"}).json()
    with people["member"].websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"type": "get_messages", "thread_id": theirs["id"]})
        frames = _drain(ws)
    assert all(f.get("type") != "messages" for f in frames), frames
    assert any(f.get("type") == "error" for f in frames)


def test_the_open_install_and_machines_get_the_roster_and_nobody_gets_nothing(world):
    frame = {"type": "bots", "bots": [{"id": "alpha"}, {"id": "main"}]}
    assert main.scope_frame_for_principal(frame, None) is None              # 4.0: fail closed
    assert main.scope_frame_for_principal(frame, main.policy.open_principal()) is frame
    out = main.scope_frame_for_principal(frame, main.policy.machine_principal("test", set()))
    assert {b["id"] for b in out["bots"]} == {"alpha", "main"}


def test_unattributed_and_unknown_frames(people):
    """An ack or plain error carries nothing about a character; a conversation frame that
    names a thread nobody can attribute is dropped (fail closed, 4.0)."""
    pr = main.policy.profile_principal(accounts.get_by_name("Max"))
    assert main.scope_frame_for_principal({"type": "ack", "client_msg_id": "c"}, pr) is not None
    assert main.scope_frame_for_principal({"type": "error", "message": "nope"}, pr) is not None
    assert main.scope_frame_for_principal({"type": "message", "thread_id": "ghost-thread"}, pr) is None
    assert main.scope_frame_for_principal({"type": "error", "bot_id": "alpha"}, pr) is None
    assert main.scope_frame_for_principal({"type": "error", "bot_id": "beta"}, pr) is not None
