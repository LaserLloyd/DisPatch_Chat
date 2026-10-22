"""An unsigned device is the lock-target default profile, or nobody (4.0, BUILD-PLAN-v2
§1.4) - at all five sites: the HTTP gate, /ws (handshake and while it stays open), the
add-on thread socket and its session_live check. Device binding is gone: a device
row that still names a default profile signs nobody in.

Run: cd backend && uv run pytest tests/test_unsigned_lock_target.py
"""
from __future__ import annotations

import sqlite3

import pytest
from starlette.websockets import WebSocketDisconnect

from app import accounts, auth, extensions, policy

LAN = ("203.0.113.130", 46000)


def _guest(world, owner, addr=LAN):
    from agents_world import sibling
    return sibling(world, owner, addr)


@pytest.fixture
def owner(world):
    return world.create_owner(world.client())


def test_profile_list_means_sign_in_first(world, owner):
    g = _guest(world, owner)
    r = g.get("/api/bots")
    assert r.status_code == 401 and r.json()["error"]["code"] == "signin_required"
    st = g.get("/api/auth/status").json()                  # public
    assert st["lock_target"] == "profile_list" and st["signed_in"] is False
    assert st["profiles"]                                  # the tiles
    with pytest.raises(WebSocketDisconnect), g.websocket_connect("/ws") as ws:
        ws.receive_json()


def _direct_bot():
    import yaml

    from app import config
    doc = yaml.safe_load(config.CONFIG_PATH.read_text()) or {}
    doc.setdefault("bots", []).append({"id": "chatty", "name": "Chatty", "advisor": {
        "providers": [{"provider": "openai", "model": "m", "base_url": "http://127.0.0.1:9/v1"}]}})
    config.CONFIG_PATH.write_text(yaml.safe_dump(doc))
    config._invalidate_bots_cache()


def test_default_profile_means_landing_on_it(world, owner):
    _direct_bot()
    p = world.person(owner, "Lobby", "user")
    auth.set_lock_target("default_profile", p["id"])
    g = _guest(world, owner)
    me = g.get("/api/auth/me").json()
    assert me["kind"] == "profile" and me["profile"]["id"] == p["id"]
    assert me["profile"]["shared"] is True
    t = g.post("/api/threads", json={"bot_id": "chatty"})
    assert t.status_code in (200, 201)
    with g.websocket_connect("/ws") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello" and hello["profile"]["id"] == p["id"]
    st = g.get("/api/auth/status").json()
    assert st["default_profile"]["id"] == p["id"]


def test_a_disabled_or_missing_default_profile_is_nobody(world, owner):
    p = world.person(owner, "Lobby", "user")
    auth.set_lock_target("default_profile", p["id"])
    accounts.update_profile(p["id"], disabled=True)
    assert policy.unsigned() is None
    assert _guest(world, owner).get("/api/bots").status_code == 401


def test_the_open_socket_follows_the_lock_target(world, owner):
    p = world.person(owner, "Lobby", "user")
    auth.set_lock_target("default_profile", p["id"])
    g = _guest(world, owner)
    with g.websocket_connect("/ws") as ws:
        ws.receive_json()
        auth.set_lock_target("profile_list", None)
        ws.send_json({"type": "get_threads", "bot_id": "alpha"})
        assert ws.receive_json()["type"] == "locked"


def test_a_bound_device_is_not_signed_in(world, owner):
    p = world.person(owner, "Kiosk", "basic")
    g = _guest(world, owner)
    tok = "d" * 32
    g.cookies.set(policy.DEVICE_COOKIE, tok)
    dev = auth.device_hash(tok)
    accounts.ensure_device(dev)
    con = sqlite3.connect(world.tmp / "chats.db")
    con.execute("UPDATE devices SET default_profile_id = ? WHERE id = ?", (p["id"], dev))
    con.commit()
    con.close()
    assert g.get("/api/bots").status_code == 401
    assert not hasattr(policy, "for_device") and not hasattr(policy, "anonymous")
    assert not hasattr(accounts, "bind_device")


def test_session_live_follows_the_lock_target(world, owner):
    p = world.person(owner, "Lobby", "user")
    auth.set_lock_target("default_profile", p["id"])
    live = extensions._session_live_fn(None, "t", "alpha", device_id=None, who=("profile", p["id"]))
    assert live() is True
    auth.set_lock_target("profile_list", None)
    assert live() is False
