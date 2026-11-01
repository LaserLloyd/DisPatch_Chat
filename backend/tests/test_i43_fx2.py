"""INT-3 #33 promptness (4.3, WP-FX2): a default-profile write locks an idle tab at once.

A2 made a token-less default-profile socket lapse at the NEXT broadcast. In the headless
matrix an idle tab stayed usable for 15-20 s after the Owner disabled the default profile,
or moved the lock target, because those writes broadcast nothing themselves.
Now each of them nudges the manager: it re-resolves ``policy.unsigned()`` and sends
``locked`` to the default-profile sockets that lapsed - and nothing to anyone else (no frame
carries profile data, and a socket that did not lapse gets no frame at all).

Run: cd backend && uv run pytest tests/test_i43_fx2.py
"""
from __future__ import annotations

import time

import pytest

from app import main
from tests.test_i43_ws_default_lapse import SENTINEL, _lobby


@pytest.fixture
def lobby(world):
    return _lobby(world, "user")


def _server_socket_for(pid: str, *, default: bool):
    for ws, meta in list(main.manager._conns.items()):
        pr = meta.get("principal")
        if pr is not None and getattr(pr, "profile_id", None) == pid and (not meta.get("token")) == default:
            return ws, meta
    raise AssertionError("socket not registered")


def _frames_until_sentinel(client, ws_client, server_ws) -> list[dict]:
    """A targeted sentinel (``send`` never runs the lapse check's ``locked`` path) marks the
    end of what the socket got; no broadcast happens in between."""
    client.portal.call(main.manager.send, server_ws, SENTINEL)
    got = []
    while True:
        f = ws_client.receive_json()
        if f.get("type") == SENTINEL["type"]:
            return got
        got.append(f)


def _wait_lapsed(meta, timeout=2.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end and meta.get("principal") is not None:
        time.sleep(0.02)


def _idle_tab_locks_on(lobby, change) -> list[dict]:
    with lobby["guest"].websocket_connect("/ws") as ws:
        hello = ws.receive_json()
        while hello.get("type") != "hello":
            hello = ws.receive_json()
        assert hello["profile"]["id"] == lobby["pid"]
        server_ws, meta = _server_socket_for(lobby["pid"], default=True)
        assert _frames_until_sentinel(lobby["guest"], ws, server_ws) == []   # control: quiet
        change()
        _wait_lapsed(meta)
        return _frames_until_sentinel(lobby["guest"], ws, server_ws)


def test_disable_locks_an_idle_default_tab_with_no_other_traffic(lobby):
    def change():
        r = lobby["owner"].patch(f"/api/people/{lobby['pid']}", json={"disabled": True})
        assert r.status_code == 200, r.text
    got = _idle_tab_locks_on(lobby, change)
    assert [f.get("type") for f in got if f.get("type") == "locked"] == ["locked"], got


def test_a_refused_pin_on_the_default_profile_keeps_the_idle_tab(lobby):
    """4.3 Fable: the default profile never gets a PIN (409 default_profile_no_credential), so
    the old "a PIN ends the landing" path is gone: the refusal changes nothing and sends nothing."""
    with lobby["guest"].websocket_connect("/ws") as ws:
        hello = ws.receive_json()
        while hello.get("type") != "hello":
            hello = ws.receive_json()
        server_ws, meta = _server_socket_for(lobby["pid"], default=True)
        assert _frames_until_sentinel(lobby["guest"], ws, server_ws) == []
        r = lobby["owner"].post(f"/api/people/{lobby['pid']}/pin", json={"pin": "8642"})
        assert r.status_code == 409 and r.json()["error"]["code"] == "default_profile_no_credential", r.text
        time.sleep(0.3)
        assert meta.get("principal") is not None
        got = _frames_until_sentinel(lobby["guest"], ws, server_ws)
    assert "locked" not in [f.get("type") for f in got], got


def test_moving_the_lock_target_in_settings_locks_an_idle_default_tab(lobby):
    def change():
        r = lobby["owner"].patch("/api/settings", json={"set": {"people.lock_target": "profile_list"}})
        assert r.status_code == 200, r.text
    got = _idle_tab_locks_on(lobby, change)
    assert "locked" in [f.get("type") for f in got], got


def test_a_write_that_keeps_the_default_profile_sends_nothing(lobby):
    """A rename of the default profile: same identity, no lapse, and the nudge itself sends
    no frame (so it never carries profile data to anyone)."""
    def change():
        r = lobby["owner"].patch(f"/api/people/{lobby['pid']}", json={"name": "Lobby Two"})
        assert r.status_code == 200, r.text
    with lobby["guest"].websocket_connect("/ws") as ws:
        hello = ws.receive_json()
        while hello.get("type") != "hello":
            hello = ws.receive_json()
        server_ws, meta = _server_socket_for(lobby["pid"], default=True)
        assert _frames_until_sentinel(lobby["guest"], ws, server_ws) == []
        change()
        time.sleep(0.3)
        assert meta.get("principal") is not None
        got = _frames_until_sentinel(lobby["guest"], ws, server_ws)
    assert "locked" not in [f.get("type") for f in got], got


def test_nudge_without_sockets_or_loop_is_a_no_op():
    from app import ws as ws_mod
    mgr = ws_mod.ConnectionManager()
    mgr.nudge()                                # no loop captured yet: nothing happens
