"""4.0 ownership enforcement through the real routes and sockets (WP-CORE, the R2 port).

A chat is its owner's for every role: nobody else - Admins included, the protected one too -
lists, opens, edits, searches, exports or hears it. A thread you may not read is a 404, word
for word the answer for one that does not exist.

Run: cd backend && uv run pytest tests/test_ownership_routes.py
"""
from __future__ import annotations

import pytest
import yaml

from app import accounts, config, ownership

LAN1 = ("203.0.113.81", 43001)
LAN2 = ("203.0.113.82", 43002)
LAN3 = ("203.0.113.83", 43003)
PROV = {"providers": [{"provider": "openai", "model": "m", "base_url": "http://127.0.0.1:9/v1"}]}


def _drain(ws) -> list[dict]:
    ws.send_json({"type": "ping"})
    got = []
    while True:
        f = ws.receive_json()
        if f.get("type") == "pong":
            return got
        got.append(f)


@pytest.fixture
def house(world):
    owner = world.create_owner(world.client())
    doc = yaml.safe_load(config.CONFIG_PATH.read_text()) if config.CONFIG_PATH.exists() else {}
    doc = doc or {}
    bots = [b for b in (doc.get("bots") or []) if b.get("id") != "chatty"]
    bots.append({"id": "chatty", "name": "Chatty", "emoji": "c", "advisor": PROV})
    doc["bots"] = bots
    config.CONFIG_PATH.write_text(yaml.safe_dump(doc))
    config._invalidate_bots_cache()
    world.person(owner, "Ann", "admin", password="ann-pass-1")
    world.person(owner, "Uma", "user", pin="2222")
    world.person(owner, "Una", "user", pin="3333")
    from agents_world import sibling
    admin2, user1, user2 = (sibling(world, owner, a) for a in (LAN1, LAN2, LAN3))
    assert world.login(admin2, name="Ann", password="ann-pass-1").status_code == 200
    for c, name, pin in ((user1, "Uma", "2222"), (user2, "Una", "3333")):
        assert world.login(c, name=name, pin=pin).status_code == 200
    ownership.forget_all()
    ids = {p.name: p.id for p in accounts.list_profiles()}
    return {"owner": owner, "admin2": admin2, "user1": user1, "user2": user2, "ids": ids,
            "world": world}


def _thread_with_sentinel(world, client, word):
    t = client.post("/api/threads", json={"bot_id": "chatty", "title": f"title {word}"})
    assert t.status_code in (200, 201), t.text
    tid = t.json()["id"]
    world.main._thread_bot[tid] = "chatty"
    client.portal.call(world.db.add_message, tid, "assistant", f"hello {word} there")
    return tid


def test_nobody_but_the_owner_opens_a_chat(house):
    w = house["world"]
    tid = _thread_with_sentinel(w, house["user1"], "SENTINELU1")
    assert house["user1"].get(f"/api/threads/{tid}").status_code == 200
    assert house["user1"].get(f"/api/threads/{tid}/messages").status_code == 200
    for who in ("owner", "admin2", "user2"):
        c = house[who]
        for method, path, body in (
                ("GET", f"/api/threads/{tid}", None),
                ("GET", f"/api/threads/{tid}/messages", None),
                ("PATCH", f"/api/threads/{tid}", {"title": "x"}),
                ("POST", f"/api/threads/{tid}/read", None),
                ("DELETE", f"/api/threads/{tid}", None)):
            r = c.request(method, path, json=body)
            assert r.status_code == 404, (who, method, path, r.status_code, r.text)
            assert "SENTINELU1" not in r.text
    # still there for its owner
    assert house["user1"].get(f"/api/threads/{tid}").status_code == 200


def test_message_routes_answer_404_to_a_stranger(house):
    w = house["world"]
    tid = _thread_with_sentinel(w, house["user1"], "SENTINELM")
    mid = house["user1"].get(f"/api/threads/{tid}/messages").json()["messages"][0]["id"]
    for who in ("owner", "user2"):
        r = house[who].delete(f"/api/messages/{mid}")
        assert r.status_code == 404, (who, r.text)
        r = house[who].patch(f"/api/messages/{mid}", json={"content": "x"})
        assert r.status_code == 404, (who, r.text)


def test_lists_search_export_and_unread_are_your_own(house):
    w = house["world"]
    _thread_with_sentinel(w, house["user1"], "SENTINELA")
    _thread_with_sentinel(w, house["owner"], "SENTINELO")
    for who, mine, theirs in (("user1", "SENTINELA", "SENTINELO"), ("owner", "SENTINELO", "SENTINELA"),
                              ("admin2", None, "SENTINELA"), ("user2", None, "SENTINELO")):
        c = house[who]
        lst = c.get("/api/threads", params={"bot_id": "chatty"}).text
        hits = c.get("/api/search", params={"q": "hello"}).text
        exp = c.get("/api/export", params={"format": "json"}).text
        for text in (lst, hits, exp):
            assert theirs not in text, who
        if mine:
            assert mine in lst and mine in hits and mine in exp, who


def test_ws_actions_on_someone_elses_thread_answer_thread_not_found(house):
    w = house["world"]
    tid = _thread_with_sentinel(w, house["user1"], "SENTINELW")
    with house["owner"].websocket_connect("/ws") as ws:
        ws.receive_json()
        for action in ({"type": "get_messages", "thread_id": tid},
                       {"type": "archive_thread", "thread_id": tid},
                       {"type": "send", "thread_id": tid, "content": "hi", "client_msg_id": "c1"}):
            ws.send_json(action)
        frames = _drain(ws)
    assert "SENTINELW" not in str(frames)
    assert all(f.get("type") != "messages" for f in frames)
    assert any(f.get("type") == "error" and f.get("message") == "Thread not found" for f in frames)
    # the owner's thread was not archived
    assert house["user1"].get(f"/api/threads/{tid}").json().get("is_archived") in (False, 0)


def test_a_thread_frame_reaches_only_the_owners_sockets(house):
    w = house["world"]
    tid = _thread_with_sentinel(w, house["user1"], "SENTINELF")
    with house["user1"].websocket_connect("/ws") as mine, \
            house["owner"].websocket_connect("/ws") as admin:
        mine.receive_json()
        admin.receive_json()
        house["user1"].patch(f"/api/threads/{tid}", json={"title": "renamed SENTINELF2"})
        got_mine, got_admin = _drain(mine), _drain(admin)
    assert "SENTINELF2" in str(got_mine)
    assert "SENTINELF" not in str(got_admin)


def test_a_personal_character_is_404_for_everyone_else_admins_included(house):
    doc = yaml.safe_load(config.CONFIG_PATH.read_text())
    doc["bots"].append({"id": "p0123456789ab", "name": "Secret Pal", "emoji": "s",
                        "scope": "personal", "owner_profile": house["ids"]["Uma"],
                        "avatar": "p0123456789ab-face.png", "advisor": PROV})
    config.CONFIG_PATH.write_text(yaml.safe_dump(doc))
    config._invalidate_bots_cache()
    (config.AVATAR_DIR).mkdir(parents=True, exist_ok=True)
    for who in ("owner", "admin2", "user2"):
        c = house[who]
        assert "Secret Pal" not in c.get("/api/bots").text, who
        r = c.get("/api/bots/all")
        assert "Secret Pal" not in r.text, who
        for path in ("/api/bots/p0123456789ab/avatar", "/api/bots/p0123456789ab/avatar/thumb",
                     "/api/characters/p0123456789ab", "/static/avatars/p0123456789ab-face.png"):
            r = c.get(path)
            # a route the role may not call at all answers its 403 first (permission first)
            ok = (404, 403) if (who == "user2" and path.startswith("/api/characters")) else (404,)
            assert r.status_code in ok, (who, path, r.status_code)
            assert "Secret Pal" not in r.text
    assert "Secret Pal" in house["user1"].get("/api/bots").text
