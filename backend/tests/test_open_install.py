"""An installation nobody has claimed: closed to the network, open to this machine.

2.x was fully open until a PIN was set, and the first caller to ``POST /api/auth/setup``
became the owner - from anywhere. 3.0 keeps the old behaviour ONLY behind
``DISPATCH_ALLOW_OPEN_INSTALL=1``; by default a remote caller can read the shell and the
sign-in state and nothing else, and may finish setup only with the one-time token the
server prints at startup. (The suite sets the switch for everything else; these tests
turn it off.)
"""
from __future__ import annotations

import logging

import pytest

from app import accounts, auth


@pytest.fixture
def strict(monkeypatch):
    monkeypatch.delenv("DISPATCH_ALLOW_OPEN_INSTALL", raising=False)


LAN = ("203.0.113.50", 40000)


def test_a_remote_caller_sees_only_the_shell_and_the_signin_state(world, strict):
    lan = world.client(LAN)
    assert lan.get("/").status_code == 200
    assert lan.get("/api/health").status_code == 200
    st = lan.get("/api/auth/status").json()
    assert st["claimed"] is False and st["setup_required"] is True
    for method, path in (("get", "/api/bots"), ("get", "/api/threads"), ("get", "/api/files"),
                         ("get", "/api/export"), ("get", "/api/harness/status"),
                         ("get", "/api/people")):
        r = getattr(lan, method)(path)
        assert r.status_code == 403, path
        assert r.json()["error"]["code"] == "setup_required", path
    assert lan.post("/api/threads", json={"bot_id": "alpha"}).status_code == 403
    assert lan.delete("/api/threads/x").status_code == 403


def test_this_machine_is_open_until_the_owner_exists(world, strict):
    local = world.client(("127.0.0.1", 40000))
    assert local.get("/api/bots").status_code == 200
    assert local.get("/api/auth/status").json()["setup_required"] is False


def test_a_proxied_request_is_not_this_machine(world, strict):
    """Tailscale Serve and every reverse proxy connect from 127.0.0.1."""
    local = world.client(("127.0.0.1", 40000))
    r = local.get("/api/bots", headers={"X-Forwarded-For": "198.51.100.9"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "setup_required"


def test_setup_from_the_network_needs_the_one_time_token(world, strict):
    lan = world.client(LAN)
    body = {"name": "Olive", "password": "owner-pass1"}
    r = lan.post("/api/auth/setup", json=body)
    assert r.status_code == 403 and r.json()["error"]["code"] == "setup_forbidden"
    assert lan.post("/api/auth/setup", json={**body, "setup_token": "guess"}).status_code == 403
    assert not accounts.has_owner()
    r = lan.post("/api/auth/setup", json={**body, "setup_token": auth.setup_token()})
    assert r.status_code == 200, r.text
    assert accounts.has_owner()
    # The token is spent; the install is claimed; the same call no longer works.
    again = world.client(LAN).post("/api/auth/setup", json={**body, "setup_token": auth.setup_token()})
    assert again.status_code == 403
    # And the new owner is signed in on that client.
    assert lan.get("/api/auth/me").json()["role"] == "admin"


def test_guessing_the_setup_token_is_throttled(world, strict):
    lan = world.client(LAN)
    codes = [lan.post("/api/auth/setup", json={"password": "owner-pass1",
                                               "setup_token": f"guess-{i}"}).status_code
             for i in range(8)]
    assert codes[:5] == [403] * 5 and 429 in codes


def test_the_websocket_is_closed_to_the_network_too(world, strict):
    from starlette.websockets import WebSocketDisconnect
    lan = world.client(LAN)
    with pytest.raises(WebSocketDisconnect), lan.websocket_connect("/ws"):
        pass


def test_the_token_is_printed_in_the_log_at_startup(world, strict, caplog):
    with caplog.at_level(logging.WARNING):
        world.client(LAN)             # entering the client runs the lifespan
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "SETUP TOKEN:" in text and auth.setup_token() in text


def test_no_token_is_printed_once_the_install_is_claimed(world, strict, caplog):
    world.create_owner()
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        world.client(LAN)
    assert "SETUP TOKEN" not in "\n".join(r.getMessage() for r in caplog.records)


def test_the_2x_open_behaviour_is_still_available_on_request(world, monkeypatch):
    monkeypatch.setenv("DISPATCH_ALLOW_OPEN_INSTALL", "1")
    lan = world.client(LAN)
    assert lan.get("/api/bots").status_code == 200
    assert lan.post("/api/auth/setup", json={"new_pin": "2468"}).status_code == 200


def test_a_pin_only_2x_install_is_claimed_on_upgrade(world, strict):
    """An upgraded install has a PIN in security.yaml and no owner row yet."""
    auth.set_pin("1234")
    accounts.delete_owner_row()           # as a 2.x data dir looks before the first 3.0 start
    lan = world.client(LAN)
    assert lan.get("/api/auth/status").json()["claimed"] is True
    assert lan.get("/api/bots/all").status_code in (401, 403)           # Restricted, not "setup required"
    assert lan.post("/api/auth/unlock", json={"pin": "1234"}).status_code == 200
    assert lan.get("/api/auth/me").json()["role"] == "admin"
    assert accounts.get_owner() is not None                       # created on first use


def test_an_open_socket_is_reconnected_not_locked_once_the_install_is_claimed(world):
    """INT-1 (scratch E2E): the first-run wizard claims the install over a socket opened while
    it was open. That socket must be closed for a reconnect, never sent "locked": the client
    answers "locked" with POST /api/auth/lock, which signed the brand-new Admin out."""
    from starlette.websockets import WebSocketDisconnect
    local = world.client(("127.0.0.1", 40000))
    with local.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "hello"
        r = local.post("/api/auth/setup", json={"name": "Olive", "password": "owner-pass1"})
        assert r.status_code == 200, r.text
        ws.send_json({"type": "ping"})
        with pytest.raises(WebSocketDisconnect) as e:
            frame = ws.receive_json()
            assert frame.get("type") != "locked", frame
            ws.receive_json()
        assert e.value.code == 1012
    assert local.get("/api/auth/status").json()["signed_in"] is True    # still the new Admin
