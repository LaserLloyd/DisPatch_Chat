"""#33 (4.3, WP-A2): a socket on the lock-target default profile lapses at the NEXT broadcast.

An unsigned device that landed on the default profile holds no session, so the manager
never saw it lapse: the /ws loop re-resolved ``policy.unsigned()`` only when the client sent
something, and a tab that only listens kept receiving the profile's frames after the Owner
disabled it, moved the lock target or changed its role (a PIN on it is refused since 4.3 Fable).

4.3: every broadcast computes a generation key (``account_rev``, ``security.yaml`` mtime);
when it moved, ``policy.unsigned()`` is resolved once and every token-less profile socket
whose (kind, profile id, role, permissions) no longer matches gets ``locked`` and loses its
principal - the frame that triggered the check is not delivered to it.

Run: cd backend && uv run pytest tests/test_i43_ws_default_lapse.py
"""
from __future__ import annotations

import asyncio

import pytest

from app import accounts, auth, main, policy
from app import ws as ws_mod

LAN = ("203.0.113.143", 43000)
SENTINEL = {"type": "a2_sentinel"}          # about nothing: reaches every socket, even nobody's


def _direct_bot(bot_id="chatty"):
    import yaml

    from app import config
    doc = yaml.safe_load(config.CONFIG_PATH.read_text()) or {}
    doc.setdefault("bots", []).append({"id": bot_id, "name": bot_id.title(), "advisor": {
        "providers": [{"provider": "openai", "model": "m", "base_url": "http://127.0.0.1:9/v1"}]}})
    config.CONFIG_PATH.write_text(yaml.safe_dump(doc))
    config._invalidate_bots_cache()


def _lobby(world, role):
    """Owner + a PIN-less "Lobby" profile as the lock target + a guest landed on it, with a chat."""
    from agents_world import sibling
    owner = world.create_owner(world.client())
    _direct_bot()
    p = world.person(owner, "Lobby", role)
    auth.set_lock_target("default_profile", p["id"])
    guest = sibling(world, owner, LAN)
    t = guest.post("/api/threads", json={"bot_id": "chatty", "title": "lobby chat"})
    assert t.status_code in (200, 201), t.text
    return {"owner": owner, "guest": guest, "pid": p["id"], "thread": t.json()["id"]}


@pytest.fixture
def lobby(world):
    return _lobby(world, "user")


def _msg(thread_id: str, n: int) -> dict:
    return {"type": "message", "thread_id": thread_id, "bot_id": "chatty",
            "message": {"id": f"m{n}", "content": f"secret {n}", "role": "assistant"}}


def _broadcast_and_read(lobby, ws, frames: list[dict]) -> list[dict]:
    """Broadcast ``frames`` then the sentinel; return what the socket got up to the sentinel.
    Never sends on the socket (a ping would make the /ws loop re-resolve on its own). Only
    ``message`` / ``locked`` frames are kept (a profile write also broadcasts the roster)."""
    for f in [*frames, SENTINEL]:
        lobby["guest"].portal.call(main.manager.broadcast, f)
    got = []
    while True:
        f = ws.receive_json()
        if f.get("type") == SENTINEL["type"]:
            return got
        if f.get("type") in ("message", "locked"):
            got.append(f)


def _assert_lapses_on_next_broadcast(lobby, change) -> None:
    with lobby["guest"].websocket_connect("/ws") as ws:
        hello = ws.receive_json()
        while hello.get("type") != "hello":
            hello = ws.receive_json()
        assert hello["profile"]["id"] == lobby["pid"]
        # Control: the listening socket gets its own chat's frames.
        before = _broadcast_and_read(lobby, ws, [_msg(lobby["thread"], 1)])
        assert [f["type"] for f in before] == ["message"], before
        change()
        after = _broadcast_and_read(lobby, ws, [_msg(lobby["thread"], 2), _msg(lobby["thread"], 3)])
    assert [f["type"] for f in after] == ["locked"], after
    assert "secret 2" not in str(after) and "secret 3" not in str(after)


def test_disabling_the_default_profile_locks_a_listening_socket(lobby):
    def change():
        accounts.update_profile(lobby["pid"], disabled=True)
        auth._bust_cache()
    _assert_lapses_on_next_broadcast(lobby, change)


def test_disabling_through_the_people_api_locks_a_listening_socket(lobby):
    def change():
        r = lobby["owner"].patch(f"/api/people/{lobby['pid']}", json={"disabled": True})
        assert r.status_code == 200, r.text
    _assert_lapses_on_next_broadcast(lobby, change)


def test_a_pin_on_the_default_profile_is_refused_and_the_landing_stays(lobby):
    """4.3 Fable: the default profile never gets a PIN (409 default_profile_no_credential); the
    landing it would have ended stays exactly as it was."""
    before = policy.unsigned()
    r = lobby["owner"].post(f"/api/people/{lobby['pid']}/pin", json={"pin": "8642"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "default_profile_no_credential", r.text
    after = policy.unsigned()
    assert after is not None and after.profile_id == before.profile_id == lobby["pid"]


def test_moving_the_lock_target_locks_a_listening_socket(lobby, world):
    def change():
        auth.set_lock_target("profile_list", None)
    _assert_lapses_on_next_broadcast(lobby, change)


def test_moving_the_lock_target_to_another_profile_locks_a_listening_socket(lobby, world):
    other = world.person(lobby["owner"], "Hall", "user")

    def change():
        auth.set_lock_target("default_profile", other["id"])
    _assert_lapses_on_next_broadcast(lobby, change)


def test_a_role_change_locks_a_listening_socket(lobby):
    """User -> Basic, a role without the thread's character. Written straight to accounts
    (the route's User -> Basic path also wipes the chats, and a frame about a gone thread is
    dropped as unattributable anyway): the chat survives, the role no longer reaches it."""
    from app import characters
    before = policy.unsigned()
    assert characters.may_use_id("chatty", before)

    def change():
        accounts.update_profile(lobby["pid"], role="basic")
        auth._bust_cache()
        after = policy.unsigned()
        assert after is not None and after.profile_id == before.profile_id
        assert after.role != before.role and not characters.may_use_id("chatty", after)
    _assert_lapses_on_next_broadcast(lobby, change)


def test_an_unrelated_profile_write_keeps_the_socket(lobby, world):
    """The generation moves (account_rev), the default profile does not: nothing is locked."""
    with lobby["guest"].websocket_connect("/ws") as ws:
        ws.receive_json()
        world.person(lobby["owner"], "Someone", "user")       # an account write elsewhere
        got = _broadcast_and_read(lobby, ws, [_msg(lobby["thread"], 1)])
    assert [f["type"] for f in got] == ["message"], got


def test_the_old_refusal_holds_for_a_lapsed_session_socket(lobby, world):
    """Rule 6: a signed-in socket whose session died is still scoped as nobody."""
    from agents_world import sibling
    m = world.person(lobby["owner"], "Max", "user", pin="5555")
    member = sibling(world, lobby["owner"], ("203.0.113.144", 43001))
    assert world.login(member, name="Max", pin="5555").status_code == 200
    th = member.post("/api/threads", json={"bot_id": "chatty"}).json()["id"]
    with member.websocket_connect("/ws") as ws:
        ws.receive_json()
        accounts.update_profile(m["id"], disabled=True)
        for f in (_msg(th, 1), SENTINEL):
            member.portal.call(main.manager.broadcast, f)
        assert ws.receive_json()["type"] == SENTINEL["type"]


# --------------------------------------------------------------------------- #
# Hardening: every recipient is checked; the check costs one resolve per change.
# --------------------------------------------------------------------------- #

class _FakeWS:
    def __init__(self):
        self.frames: list[dict] = []

    async def accept(self):
        return None

    async def send_json(self, frame):
        self.frames.append(frame)


def _default_sockets(n: int):
    mgr = ws_mod.ConnectionManager()
    mgr.scoper = main.scope_frame_for_principal       # as the lifespan wires it
    mgr.pin_set = lambda: policy.claimed()
    pr = policy.unsigned()
    assert pr is not None and pr.via == "default"
    socks = [_FakeWS() for _ in range(n)]

    async def setup():
        for s in socks:
            await mgr.connect(s, token=None, principal=pr)
    asyncio.run(setup())
    return mgr, socks


def test_broadcast_calls_lapsed_for_every_recipient(lobby, monkeypatch):
    mgr, socks = _default_sockets(7)
    seen = []
    real = mgr._lapsed

    def counting(meta):
        seen.append(id(meta))
        return real(meta)
    monkeypatch.setattr(mgr, "_lapsed", counting)
    asyncio.run(mgr.broadcast(SENTINEL))
    assert len(set(seen)) == len(socks)
    seen.clear()
    asyncio.run(mgr.broadcast_where(SENTINEL, lambda pr: True))
    assert len(set(seen)) == len(socks)


def test_idle_default_sockets_resolve_unsigned_once(lobby, monkeypatch):
    mgr, socks = _default_sockets(200)
    calls = []
    real = policy.unsigned

    def counting(*a, **k):
        calls.append(1)
        return real(*a, **k)
    monkeypatch.setattr(policy, "unsigned", counting)

    async def run():
        for _ in range(100):
            await mgr.broadcast(SENTINEL)
    asyncio.run(run())
    assert len(calls) == 1, len(calls)
    assert all(len(s.frames) == 100 for s in socks)
    # One write later, one more resolve - and every socket is told at once.
    accounts.update_profile(lobby["pid"], disabled=True)
    asyncio.run(run())
    assert len(calls) == 2, len(calls)
    assert all(s.frames[100] == {"type": "locked"} for s in socks)
    assert all(len(s.frames) == 200 for s in socks)          # the sentinel still reaches them
