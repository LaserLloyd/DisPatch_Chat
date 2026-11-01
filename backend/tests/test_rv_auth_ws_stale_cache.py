"""RV-auth: the unsigned-principal cache goes stale when no default socket is open at the
time of a change, and a NEW default-profile socket's targeted replies are then dropped."""
from __future__ import annotations

import asyncio

from test_i43_ws_default_lapse import SENTINEL, _FakeWS, _msg, lobby

from app import auth, main, policy
from app import ws as ws_mod


def test_new_default_socket_after_lock_target_move_gets_its_own_replies(lobby, world):
    mgr = ws_mod.ConnectionManager()
    mgr.scoper = main.scope_frame_for_principal
    mgr.pin_set = lambda: policy.claimed()
    a = policy.unsigned()
    s1 = _FakeWS()

    async def first():
        await mgr.connect(s1, token=None, principal=a)
        await mgr.broadcast(SENTINEL)                # primes the cache with profile A
        await mgr.disconnect(s1)
    asyncio.run(first())

    other = world.person(lobby["owner"], "Hall", "user")
    auth.set_lock_target("default_profile", other["id"])   # nudge: no default socket open
    t = lobby["guest"].post("/api/threads", json={"bot_id": "chatty", "title": "hall chat"})
    assert t.status_code in (200, 201), t.text
    th = t.json()["id"]
    b = policy.unsigned()
    assert b is not None and b.profile_id == other["id"]
    s2 = _FakeWS()

    async def second():
        await mgr.connect(s2, token=None, principal=b)
        await mgr.send(s2, _msg(th, 9))              # a reply to its own request
    asyncio.run(second())
    assert [f.get("type") for f in s2.frames] == ["message"], s2.frames
