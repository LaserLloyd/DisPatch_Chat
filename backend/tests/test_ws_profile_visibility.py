"""Per-profile character visibility holds on BROADCAST frames, not only on the roster.

W5's open item: ``characters.may_use`` (audience + the character's ``profiles`` list)
filtered ``GET /api/bots`` and the WebSocket ``hello``, but every later ``bots`` /
``thread_*`` / ``message`` broadcast went to every socket unfiltered, so a profile left off
a character's list learned its new name and avatar the moment the Owner edited it.
``main.scope_frame_for_principal`` (installed as ``manager.scoper``) closes that per
connection. Run: cd backend && uv run pytest tests/test_ws_profile_visibility.py
"""
from __future__ import annotations

import pytest

from app import config, main

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
    mia = world.person(owner, "Mia", "restricted")
    world.person(owner, "Max", "member", pin="5555")
    kid = world.client(LAN)
    assert world.login(kid, profile_id=mia["id"]).status_code == 200
    member = world.client(("203.0.113.61", 41001))
    assert world.login(member, name="Max", pin="5555").status_code == 200
    # alpha (audience=all) is made for Mia only; beta (audience=all) for Max only.
    assert owner.patch("/api/characters/alpha", json={"profiles": [mia["id"]]}).status_code == 200, \
        owner.patch("/api/characters/alpha", json={"profiles": [mia["id"]]}).text
    max_id = next(p["id"] for p in owner.get("/api/people").json()["profiles"] if p["name"] == "Max")
    assert owner.patch("/api/characters/beta", json={"profiles": [max_id]}).status_code == 200
    return {"owner": owner, "kid": kid, "member": member}


def test_the_roster_and_hello_already_hide_a_character_made_for_someone_else(people):
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
    # Each socket got the broadcast (the frame is not over-dropped) ...
    assert _bot_ids(m_frames, "bots") and _bot_ids(k_frames, "bots")
    # ... but only with the characters its profile may use.
    assert "alpha" not in _bot_ids(m_frames, "bots") and "beta" in _bot_ids(m_frames, "bots")
    assert "beta" not in _bot_ids(k_frames, "bots") and "alpha" in _bot_ids(k_frames, "bots")
    text = str(m_frames)
    assert "Alpha Secret" not in text
    assert "Beta Secret" not in str(k_frames)


def test_thread_and_message_frames_about_a_hidden_character_never_arrive(people):
    owner = people["owner"]
    with people["member"].websocket_connect("/ws") as m_ws:
        m_ws.receive_json()
        hidden = owner.post("/api/threads", json={"bot_id": "alpha", "title": "Mia's secret"}).json()
        shown = owner.post("/api/threads", json={"bot_id": "beta", "title": "Max's own"}).json()
        assert owner.patch(f"/api/threads/{hidden['id']}", json={"title": "Renamed secret"}).status_code == 200
        main._thread_bot[hidden["id"]] = "alpha"
        people["member"].portal.call(main.manager.broadcast, {
            "type": "message", "thread_id": hidden["id"], "bot_id": "alpha",
            "message": {"id": "z", "content": "for Mia only", "role": "assistant"}})
        people["member"].portal.call(main.manager.broadcast, {
            "type": "message", "thread_id": shown["id"], "bot_id": "beta",
            "message": {"id": "y", "content": "for Max", "role": "assistant"}})
        frames = _drain(m_ws)
    kinds = [(f.get("type"), (f.get("thread") or {}).get("id") or f.get("thread_id")) for f in frames]
    assert ("thread_created", shown["id"]) in kinds
    assert ("message", shown["id"]) in kinds
    assert all(tid != hidden["id"] for _, tid in kinds), kinds
    assert "secret" not in str(frames) and "for Mia only" not in str(frames)


def test_get_messages_over_the_socket_respects_the_profile_list(people):
    owner = people["owner"]
    hidden = owner.post("/api/threads", json={"bot_id": "alpha"}).json()
    with people["member"].websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"type": "get_messages", "thread_id": hidden["id"]})
        frames = _drain(ws)
    assert all(f.get("type") != "messages" for f in frames), frames
    assert any(f.get("type") == "error" for f in frames)


def test_the_owner_and_the_open_install_are_untouched():
    frame = {"type": "bots", "bots": [{"id": "alpha"}, {"id": "main"}]}
    assert main.scope_frame_for_principal(frame, None) is frame
    assert main.scope_frame_for_principal(frame, main.policy.open_principal()) is frame
    assert main.scope_frame_for_principal(frame, main.policy.machine_principal("test", set())) is frame


def test_an_unattributed_frame_passes_and_an_unknown_character_is_not_hidden(people):
    """A Member's ack or plain error carries nothing about a character; a frame naming
    a character that no longer exists is left alone (may_use_id says True)."""
    from app import accounts
    pr = main.policy.profile_principal(accounts.get_by_name("Max"))
    assert main.scope_frame_for_principal({"type": "ack", "client_msg_id": "c"}, pr) is not None
    assert main.scope_frame_for_principal({"type": "error", "message": "nope"}, pr) is not None
    assert main.scope_frame_for_principal({"type": "message", "thread_id": "ghost-thread"}, pr) is not None
    assert main.scope_frame_for_principal({"type": "message", "bot_id": "alpha"}, pr) is None
    assert main.scope_frame_for_principal({"type": "error", "bot_id": "alpha"}, pr) is None
    assert set(config.get_bot("alpha").profiles)
