"""4.3 Fable (session): the idle sign-out follows the CALLER's own timeout, and a tab's
background requests are not "use".

Was: ``GET /api/auth/status`` sent the Admin ``lock_timeout`` to everyone, so a User's tab
signed out after the Admin's 10 minutes; and every request (the status poll included) slid
the server's idle window, so an open tab kept a session alive that nobody was using.
"""
from __future__ import annotations

import pytest

from app import auth, settings_store


@pytest.fixture
def owner(world):
    return world.create_owner()


def _session_of(profile_id):
    return next(s for s in auth._sessions.values() if s.profile_id == profile_id)


def _set_timeouts(admin=600, user=3600):
    assert settings_store.set_many({"people.lock_timeout_seconds": admin,
                                    "people.session_timeout_user": user}).ok


def test_a_users_status_carries_the_users_own_idle_timeout(world, owner):
    _set_timeouts(admin=600, user=3600)
    mia = world.person(owner, "Mia", "member")
    c = world.client()
    assert world.login(c, profile_id=mia["id"]).status_code == 200
    st = c.get("/api/auth/status").json()
    assert st["idle_timeout_seconds"] == 3600
    # The Admin still gets the Admin's own clock.
    assert owner.get("/api/auth/status").json()["idle_timeout_seconds"] == 600


def test_a_per_profile_override_wins_in_the_status(world, owner):
    _set_timeouts(admin=600, user=3600)
    mia = world.person(owner, "Mia", "member", session_timeout=900)
    c = world.client()
    world.login(c, profile_id=mia["id"])
    assert c.get("/api/auth/status").json()["idle_timeout_seconds"] == 900


def test_no_session_means_no_idle_clock(world, owner):
    stranger = world.client(("203.0.113.9", 1))
    assert stranger.get("/api/auth/status").json()["idle_timeout_seconds"] == 0


def test_the_status_poll_does_not_keep_an_idle_session_alive(world, owner):
    _set_timeouts(admin=600, user=3600)
    mia = world.person(owner, "Mia", "member")
    c = world.client()
    world.login(c, profile_id=mia["id"])
    _session_of(mia["id"]).last_seen -= 3000
    assert c.get("/api/auth/status").json()["signed_in"] is True
    _session_of(mia["id"]).last_seen -= 700            # 3700 s since the last real use
    assert c.get("/api/auth/status").json()["signed_in"] is False


def test_a_request_marked_background_is_not_use_but_a_real_one_is(world, owner):
    _set_timeouts(admin=600, user=3600)
    mia = world.person(owner, "Mia", "member")
    c = world.client()
    world.login(c, profile_id=mia["id"])
    _session_of(mia["id"]).last_seen -= 3000
    assert c.get("/api/bots", headers={"X-DisPatch-Background": "1"}).status_code == 200
    _session_of(mia["id"]).last_seen -= 700
    assert c.get("/api/auth/status").json()["signed_in"] is False
    # A person's own request slides the window, as before.
    world.login(c, profile_id=mia["id"])
    _session_of(mia["id"]).last_seen -= 3000
    assert c.get("/api/bots").status_code == 200
    _session_of(mia["id"]).last_seen -= 700
    assert c.get("/api/auth/status").json()["signed_in"] is True


def test_an_active_frame_is_use_and_a_ping_is_not(world, owner):
    """A person reading a long chat sends nothing; the page says "active" over the socket
    instead (throttled), so the server's clock agrees with the tab's."""
    _set_timeouts(admin=600, user=3600)
    mia = world.person(owner, "Mia", "member")
    c = world.client()
    world.login(c, profile_id=mia["id"])
    with c.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "hello"
        _session_of(mia["id"]).last_seen -= 3000
        ws.send_json({"type": "ping"})
        assert ws.receive_json()["type"] == "pong"
        assert _session_of(mia["id"]).last_seen < auth.time.monotonic() - 2900   # a ping is not use
        ws.send_json({"type": "active"})
        ws.send_json({"type": "ping"})
        assert ws.receive_json()["type"] == "pong"                           # no "Unknown type" error
        assert _session_of(mia["id"]).last_seen > auth.time.monotonic() - 60
