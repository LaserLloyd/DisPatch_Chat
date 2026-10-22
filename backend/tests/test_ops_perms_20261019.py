"""Permission changes the owner ordered on 2026-10-19, one block per order.

P-1  "Admin should have full permissions": an Admin holds every permission the Owner has
     (host control included). The single structural exception: an Admin may not delete,
     disable, demote, sign out or re-credential THE Owner profile.

Run: cd backend && uv run pytest tests/test_ops_perms_20261019.py
"""
from __future__ import annotations

from app import policy
from app.contracts.permissions import ROLE_DEFAULTS, Permission, Role

LAN = ("203.0.113.81", 43001)


def _admin(world, owner, name="Ada", password="admin-pass1", addr=LAN):
    prof = world.person(owner, name, "admin", password=password)
    c = world.client(addr)
    assert world.login(c, name=name, password=password).status_code == 200
    return prof, c


# P-1 ------------------------------------------------------------------------ #

def test_admin_holds_every_permission_the_owner_has():
    assert ROLE_DEFAULTS[Role.ADMIN] == frozenset(Permission)    # 4.0: one Admin role


def test_admin_reaches_every_owner_surface(world):
    owner = world.create_owner(world.client())
    _ada, admin = _admin(world, owner)
    me = admin.get("/api/auth/me").json()
    assert "admin_system" in me["permissions"], me
    # Health (the dashboard), backups, the API key, host-control settings, the network step.
    assert admin.get("/api/dashboard").status_code == 200
    assert admin.get("/api/backups").status_code == 200
    r = admin.post("/api/auth/api-token", json={"rotate": True})
    assert r.status_code == 200 and len(r.json()["api_token"]) >= 32, r.text
    assert admin.get("/api/auth/api-token").json()["has_token"] is True
    r = admin.patch("/api/settings", json={"set": {"connections.image_cli": "/usr/bin/true"}})
    assert r.status_code == 200, r.text
    assert admin.get("/api/export").status_code == 200
    assert admin.get("/api/plugins").status_code == 200
    assert admin.get("/api/connections").status_code == 200
    assert admin.get("/api/agents").status_code == 200
    assert admin.get("/api/people/audit").status_code == 200


def test_admin_manages_other_admins(world):
    owner = world.create_owner(world.client())
    _ada, admin = _admin(world, owner)
    r = admin.post("/api/people", json={"name": "Bea", "role": "admin", "password": "bea-pass-123"})
    assert r.status_code == 201, r.text
    bea = r.json()
    assert admin.get("/api/people").json()["can_create_admin"] is True
    r = admin.post(f"/api/people/{bea['id']}/password", json={"password": "bea-pass-456"})
    assert r.status_code == 200, r.text
    assert world.login(world.client(("203.0.113.82", 43002)), name="Bea",
                       password="bea-pass-456").status_code == 200  # scrub-ok: throwaway test fixture
    assert admin.patch(f"/api/people/{bea['id']}", json={"role": "member"}).status_code in (200, 409)
    assert admin.post(f"/api/people/{bea['id']}/sign-out").status_code == 200
    # 4.0: deleting goes through POST /api/people/{id}/delete (profiles_service, counts first)
    assert admin.delete(f"/api/people/{bea['id']}").json()["error"]["code"] == "use_delete_route"
    # A Member can never be granted host control; an Admin needs no grant for it.
    r = admin.post("/api/people", json={"name": "Max", "role": "member", "pin": "5555",
                                        "grants": ["admin_system"]})
    assert r.status_code == 422 and r.json()["error"]["code"] == "permissions_invalid"


def test_the_owner_profile_stays_out_of_an_admins_reach(world):
    owner = world.create_owner(world.client())
    _ada, admin = _admin(world, owner)
    olive = next(p for p in admin.get("/api/people").json()["profiles"] if p["protected"])
    oid = olive["id"]
    assert admin.delete(f"/api/people/{oid}").status_code == 403
    assert admin.patch(f"/api/people/{oid}", json={"disabled": True}).status_code == 403
    assert admin.patch(f"/api/people/{oid}", json={"role": "member"}).status_code == 403
    assert admin.post(f"/api/people/{oid}/pin", json={"pin": "9999"}).status_code == 403
    assert admin.delete(f"/api/people/{oid}/pin").status_code == 403
    assert admin.post(f"/api/people/{oid}/password", json={"password": "taken-over-1"}).status_code == 403
    assert admin.post(f"/api/people/{oid}/sign-out").status_code == 403
    assert admin.delete(f"/api/users/{oid}").status_code == 403
    # The install PIN is the Owner's credential.
    r = admin.post("/api/auth/setup", json={"current_pin": "2468", "new_pin": "1111"})
    assert r.status_code == 403
    # And the owner still signs in with what they had.
    assert world.login(world.client(("203.0.113.83", 43003)), name="Olive",
                       password="owner-pass1").status_code == 200


def test_effective_permissions_of_an_admin_include_host_control(world):
    owner = world.create_owner(world.client())
    ada, _admin_client = _admin(world, owner)
    from app import accounts
    prof = accounts.get_profile(ada["id"])
    assert Permission.ADMIN_SYSTEM.value in policy.effective_permissions(prof)


# P-2 ------------------------------------------------------------------------ #
# "Members should have voice": the add-on's user reads are use_voice, its administration
# stays manage_plugins; a Restricted caller keeps its own (locked) view.

import json  # noqa: E402

import pytest  # noqa: E402
from _extfake import KNOBS, ext_env, install, thread_for  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402

ORIGIN = {"origin": "http://testserver"}


def _household(c):
    """An owner (password + PIN) on the ext_env app, and a client factory."""
    from fastapi.testclient import TestClient

    from app import main
    r = c.post("/api/auth/setup", json={"name": "Olive", "password": "owner-pass1", "new_pin": "2468"})
    assert r.status_code == 200, r.text
    made = []

    def client(addr):
        k = TestClient(main.app, client=addr)
        k.__enter__()
        k.headers.update({"Origin": "http://testserver"})
        made.append(k)
        return k
    return client, made


def _person(owner, name, role, **body):
    r = owner.post("/api/people", json={"name": name, "role": role, **body})
    assert r.status_code == 201, r.text
    return r.json()


def _signed_in(client, addr, **cred):
    k = client(addr)
    r = k.post("/api/auth/login", json=cred)
    assert r.status_code == 200, r.text
    return k


def test_a_member_reads_what_voice_chat_needs_but_does_not_administer_it(ext_env):
    c, _ = ext_env
    install()
    client, made = _household(c)
    try:
        _person(c, "Max", "member", pin="5555")
        _person(c, "Ada", "admin", password="admin-pass1")
        _person(c, "Mia", "restricted")
        member = _signed_in(client, ("203.0.113.91", 44001), name="Max", pin="5555")
        admin = _signed_in(client, ("203.0.113.92", 44002), name="Ada", password="admin-pass1")
        kid = _signed_in(client, ("203.0.113.93", 44003), name="Mia")
        guest = client(("203.0.113.94", 44004))
        assert member.get("/api/voice/status").status_code == 200
        assert member.post("/api/voice/upload", content=b"x").status_code in (401, 403)    # administration
        assert member.get("/api/voice/providers").status_code in (401, 403)
        assert admin.get("/api/voice/status").status_code == 200
        assert admin.post("/api/voice/upload", content=b"x").status_code == 200
        for k in (kid, guest):                       # a locked device has its own view
            assert k.get("/api/voice/status").status_code in (401, 403)
        status = member.get("/api/auth/status").json()
        assert "use_voice" in status["permissions"] and "manage_plugins" not in status["permissions"]
    finally:
        for k in made:
            k.__exit__(None, None, None)


def test_a_member_opens_a_voice_call(ext_env):
    c, _ = ext_env
    install()
    client, made = _household(c)
    try:
        _person(c, "Max", "member", pin="5555")
        member = _signed_in(client, ("203.0.113.95", 44005), name="Max", pin="5555")
        # A direct-model character: an agent-backed one needs dispatch_agents, as a typed
        # send does (tests/test_voice_agent_gate.py).
        from app import config
        config.upsert_bot(config.Bot(id="adv", name="Sage", advisor={
            "providers": [{"provider": "custom", "base_url": "http://p/v1", "model": "m"}]}))
        tid = thread_for(member, "adv")
        with member.websocket_connect(f"/ws/voice/{tid}", headers=ORIGIN) as ws:
            ready = json.loads(ws.receive_text())
            assert ready["type"] == "ready" and ready["token"] is True and ready["locked"] is False
    finally:
        for k in made:
            k.__exit__(None, None, None)


# P-3 ------------------------------------------------------------------------ #
# "Anonymous is the same as a locked session": a Restricted child who locks the screen (or
# whose session times out) stays under their own profile's quiet hours and budgets; a guest
# on a device nobody bound gets the default Restricted budgets, over HTTP and WebSocket.

from datetime import datetime, timedelta  # noqa: E402

from app import accounts, auth  # noqa: E402

TABLET = ("203.0.113.97", 44007)


def _inside_now() -> str:
    now = datetime.now()
    return f"{(now - timedelta(minutes=5)).strftime('%H:%M')}-{(now + timedelta(minutes=5)).strftime('%H:%M')}"


def _kid_on_tablet(world, **budgets):
    owner = world.create_owner(world.client())
    mia = world.person(owner, "Mia", "restricted", **({"budgets": budgets} if budgets else {}))
    tablet = world.client(TABLET)
    assert world.login(tablet, profile_id=mia["id"]).status_code == 200
    return owner, mia, tablet


# The voice socket: before 2026-10-19 it skipped the device step, so a device-bound
# Restricted profile's call ran as an anonymous guest (per-address budget, no quiet hours).

def _ext_kid(c, client, **budgets):
    mia = _person(c, "Mia", "restricted", **({"budgets": budgets} if budgets else {}))
    tablet = client(TABLET)
    assert tablet.post("/api/auth/login", json={"profile_id": mia["id"]}).status_code == 200
    tid = thread_for(tablet, "alpha")
    return mia, tablet, tid


