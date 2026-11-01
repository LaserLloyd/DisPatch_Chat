"""The managed-bot routes (4.1): an Admin's kit for a household bot, a copy's Reset. Through
the real app (gate, policy engine, SQLite, config store: the ``world`` fixture)."""
from __future__ import annotations

import pytest
from agents_world import sibling
from test_profiles_api import team

from app import config, kits


@pytest.fixture(autouse=True)
def _guides_on(monkeypatch):
    monkeypatch.setenv("DISPATCH_ATLAS_GUIDES", "1")
    monkeypatch.setenv("DISPATCH_FIRSTRUN", "1")


def _mine(client):
    r = client.get("/api/me/characters")
    assert r.status_code == 200, r.text
    return r.json()["characters"]


def _threads(client):
    r = client.get("/api/threads")
    assert r.status_code == 200, r.text
    body = r.json()
    return body["threads"] if isinstance(body, dict) else body


def test_only_an_admin_reaches_a_kit_and_only_a_household_one(team):
    max_, eve = team["Max"], team["Eve"]
    for method, path in (("GET", "/api/characters/otter/kit"), ("PUT", "/api/characters/otter/kit"),
                         ("DELETE", "/api/characters/otter/kit"), ("POST", "/api/characters/otter/kit/push")):
        r = max_.request(method, path, json={"auto_clone": True} if method == "PUT" else None)
        assert r.status_code == 403, (method, path, r.status_code)
    r = eve.get("/api/characters/otter/kit")
    assert r.status_code == 200 and r.json()["managed"] is False and r.json()["copyable"] is True
    atlas = next(b for b in _mine(max_) if b["pack"] == "atlas")
    assert atlas["managed"] is True
    assert eve.get(f"/api/characters/{atlas['id']}/kit").status_code == 404, "a personal bot is not there"
    assert eve.post(f"/api/characters/{atlas['id']}/kit/push").status_code == 404


def test_an_admin_pushes_a_bot_and_a_chat_to_every_profile(team):
    max_, eve = team["Max"], team["Eve"]
    r = eve.put("/api/characters/otter/kit", json={
        "auto_clone": True, "chats": [{"title": "House rules", "body": "Be **kind**.", "audience": "everyone"}]})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["managed"] and out["auto_clone"] and out["copies"] >= 2
    assert set(out) >= {"chats", "copies", "posted"} and "profiles" not in out
    otter = next(b for b in _mine(max_) if b["cloned_from"] == "otter")
    assert otter["managed"] is True
    titles = [t["title"] for t in _threads(max_) if t["bot_id"] == otter["id"]]
    assert titles == ["House rules"]
    # Push again: nothing new
    r = eve.post("/api/characters/otter/kit/push")
    assert r.status_code == 200 and r.json()["posted"] == 0
    # Max deletes his copy: it stays deleted after another push
    assert max_.delete(f"/api/me/characters/{otter['id']}").status_code == 200
    eve.post("/api/characters/otter/kit/push")
    assert not any(b["cloned_from"] == "otter" for b in _mine(max_))
    # ... and "Make my own copy" brings it back with its chat
    r = max_.post("/api/characters/otter/clone")
    assert r.status_code == 201, r.text
    new_id = r.json()["bot"]["id"]
    eve.post("/api/characters/otter/kit/push")
    assert [t["title"] for t in _threads(max_) if t["bot_id"] == new_id] == ["House rules"]


def test_reset_a_copy(team):
    max_, eve = team["Max"], team["Eve"]
    atlas = next(b for b in _mine(max_) if b["pack"] == "atlas")
    assert max_.patch(f"/api/me/characters/{atlas['id']}", json={"name": "Globe"}).status_code == 200
    r = max_.post(f"/api/me/characters/{atlas['id']}/reset")
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "Atlas"
    own = max_.post("/api/me/characters", json={"name": "Mine"}).json()
    r = max_.post(f"/api/me/characters/{own['id']}/reset")
    assert r.status_code == 422 and r.json()["error"]["code"] == "not_managed"
    eve_atlas = next(b for b in _mine(eve) if b["pack"] == "atlas")
    assert max_.post(f"/api/me/characters/{eve_atlas['id']}/reset").status_code == 404


def test_a_cross_site_push_is_refused(team):
    eve = team["Eve"]
    bare = sibling(team["world"], team["Olive"], ("203.0.113.61", 41001), origin=False)
    bare.cookies.update(eve.cookies)
    bare.headers.update({"Origin": "http://evil.example"})
    assert bare.post("/api/characters/otter/kit/push").status_code == 403


def test_a_shipped_kit_cannot_be_detached(team):
    eve = team["Eve"]
    config.upsert_bot(config.Bot(id="atlas", name="Atlas", advisor={"providers": [], "tools": True}))
    r = eve.delete("/api/characters/atlas/kit")
    assert r.status_code == 409 and r.json()["error"]["code"] == "kit_shipped"
    r = eve.get("/api/characters/atlas/kit")
    assert r.json()["shipped"] is True and [c["builtin"] for c in r.json()["chats"]] == [True, True, True]
    assert kits.get_kit("atlas") is not None
