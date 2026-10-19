"""Ops check of the 3.0 security surfaces (2026-10-19), one test per finding.

S-1  a socket opened by a device bound to a default profile (the kitchen tablet, no session)
     kept the profile as it was at the handshake: quiet hours or budgets the Owner set later,
     disabling the profile or unbinding the device never reached it

Run: cd backend && uv run pytest tests/test_ops_security_20261019.py
"""
from __future__ import annotations

from datetime import datetime, timedelta

from app import accounts

LAN = ("203.0.113.61", 41001)


def _inside_now() -> str:
    now = datetime.now()
    return f"{(now - timedelta(minutes=5)).strftime('%H:%M')}-{(now + timedelta(minutes=5)).strftime('%H:%M')}"


# S-1 ------------------------------------------------------------------------ #

def _bound_tablet(world):
    owner = world.create_owner(world.client())
    mia = world.person(owner, "Mia", "restricted")
    tablet = world.client(LAN)
    assert world.login(tablet, name="Olive", password="owner-pass1").status_code == 200
    r = tablet.put("/api/people/devices/this", json={"profile_id": mia["id"]})
    assert r.status_code == 200, r.text
    assert tablet.post("/api/auth/lock").status_code == 200
    me = tablet.get("/api/auth/me").json()
    assert (me["kind"], me["name"]) == ("profile", "Mia"), me
    return owner, mia, tablet


def test_device_bound_socket_sees_quiet_hours_set_later(world):
    _owner, mia, tablet = _bound_tablet(world)
    with tablet.websocket_connect("/ws") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello" and hello["profile"]["name"] == "Mia"
        accounts.update_profile(mia["id"], budgets={"quiet_hours": _inside_now()})  # set afterwards
        ws.send_json({"type": "get_threads", "bot_id": "alpha"})
        f = ws.receive_json()
        assert f["type"] == "error" and f.get("code") == "quiet_hours", f


def test_device_bound_socket_drops_a_disabled_profile(world):
    owner, mia, tablet = _bound_tablet(world)
    with tablet.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "hello"
        assert owner.patch(f"/api/people/{mia['id']}", json={
            "disabled": True}).status_code == 200
        accounts.update_profile(mia["id"], budgets={"quiet_hours": _inside_now()})
        # Disabled: the device falls back to an anonymous guest, whose own (per-address)
        # rules apply - not Mia's, and not Mia's identity.
        ws.send_json({"type": "get_threads", "bot_id": "alpha"})
        f = ws.receive_json()
        assert f.get("code") != "quiet_hours", f
        conn = next(iter(world.main.manager._conns.values()))
        assert conn["principal"].kind == "anonymous"
