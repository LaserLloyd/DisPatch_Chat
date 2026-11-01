"""4.3 WP-A1: the unclaimed-install gate (#2), forwarded headers (#5), presented keys (#7),
the fresh-install default (#7), N-15, and the "nothing for a foreign Host" walk.

Everything runs through the real app (``world`` fixture, tests/conftest.py).
"""
from __future__ import annotations

import pytest
from starlette.websockets import WebSocketDisconnect

from app import auth, main, policy


@pytest.fixture(autouse=True)
def _shipped_fresh_install_default(monkeypatch):
    """conftest models upgraded installs; these tests check the shipped default (False)."""
    from app import auth as _auth
    monkeypatch.setattr(_auth, "FRESH_INSTALL_LOOPBACK_TRUST", False)


EVIL = "evil.example"
LOCAL = "localhost:8765"


@pytest.fixture
def strict(monkeypatch):
    monkeypatch.delenv("DISPATCH_ALLOW_OPEN_INSTALL", raising=False)


def _local_client(world, addr=("127.0.0.1", 41000)):
    """A browser on this machine that typed http://localhost:8765."""
    c = world.client(addr, origin=False)
    c.headers.update({"Host": LOCAL, "Origin": f"http://{LOCAL}"})
    return c


def _ws_close_code(client, path, headers):
    with pytest.raises(WebSocketDisconnect) as ei, client.websocket_connect(path, headers=headers) as ws:
        ws.receive_json()
    return ei.value.code


# --------------------------------------------------------------------------- #
# (a) DNS rebinding / another site's page against an unclaimed install
# --------------------------------------------------------------------------- #


def test_a_foreign_host_from_loopback_gets_setup_required(world, strict):
    c = world.client(("127.0.0.1", 41001), origin=False)
    r = c.get("/api/settings", headers={"Host": EVIL})
    assert r.status_code == 403 and r.json()["error"]["code"] == "setup_required", r.text
    r = c.post("/api/auth/setup", headers={"Host": EVIL, "Origin": f"http://{EVIL}"},
               json={"name": "Mallory", "password": "evil-pass-1"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "setup_required", r.text
    assert not policy.claimed()


@pytest.mark.parametrize("host", ["127.0.0.1.nip.io", "127.0.0.1.nip.io:8765", "127.evil.example",
                                  "127.1.2.3.example.net", "localhost.evil.example"])
def test_a_rebinding_name_that_starts_like_loopback_is_still_foreign(world, strict, host):
    """INT-3 finding: a DNS name that merely starts with ``127.`` (``127.0.0.1.nip.io``) is not a
    loopback name; it must get setup_required and must not be able to claim the install."""
    c = world.client(("127.0.0.1", 41011), origin=False)
    r = c.get("/api/settings", headers={"Host": host})
    assert r.status_code == 403 and r.json()["error"]["code"] == "setup_required", (host, r.text)
    origin = "http://" + host
    r = c.post("/api/auth/setup", headers={"Host": host, "Origin": origin},
               json={"name": "Mallory", "password": "evil-pass-1"})
    assert r.status_code == 403, (host, r.text)
    assert _ws_close_code(c, "/ws", {"Host": host}) == 1008
    assert not policy.claimed()


def test_ip_is_loopback_parses_addresses_not_prefixes():
    for good in ("127.0.0.1", "127.5.6.7", "::1", "::ffff:127.0.0.1"):
        assert policy.ip_is_loopback(good), good
    for bad in ("127.0.0.1.nip.io", "127.evil.example", "127.", "1270.0.0.1", "localhost.example", "", "192.0.2.1"):
        assert not policy.ip_is_loopback(bad), bad


def test_another_sites_origin_on_a_localhost_post_is_refused(world, strict):
    c = world.client(("127.0.0.1", 41002), origin=False)
    h = {"Host": LOCAL, "Origin": f"http://{EVIL}"}
    r = c.post("/api/auth/setup", headers=h, json={"name": "Mallory", "password": "evil-pass-1"})
    assert r.status_code == 403, r.text
    r = c.post("/api/threads", headers=h, json={"bot_id": "alpha"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "setup_required"
    # Sec-Fetch-Site cross-site with no Origin is refused too.
    r = c.post("/api/threads", headers={"Host": LOCAL, "Sec-Fetch-Site": "cross-site"},
               json={"bot_id": "alpha"})
    assert r.status_code == 403
    assert not policy.claimed()


def test_the_sockets_close_for_a_foreign_host(world, strict):
    c = world.client(("127.0.0.1", 41003), origin=False)
    assert _ws_close_code(c, "/ws", {"Host": EVIL}) == 1008
    assert _ws_close_code(c, "/ws/voice/x", {"Host": EVIL}) == 1008
    assert _ws_close_code(c, "/ws", {"Host": LOCAL, "Origin": f"http://{EVIL}"}) == 1008


def test_this_machine_by_its_loopback_name_is_still_open(world, strict):
    c = _local_client(world)
    assert c.get("/api/settings").status_code == 200
    for host in ("127.0.0.1:8765", "[::1]:8765", "localhost"):
        assert c.get("/api/bots", headers={"Host": host}).status_code == 200, host
    with c.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "hello"
    r = c.post("/api/auth/setup", json={"name": "Olive", "password": "owner-pass1"})
    assert r.status_code == 200, r.text
    assert policy.claimed()


# --------------------------------------------------------------------------- #
# (n) the opt-in open install keeps its Host freedom, never another site's Origin
# --------------------------------------------------------------------------- #


def test_open_install_still_refuses_another_sites_origin(world, monkeypatch):
    monkeypatch.setenv("DISPATCH_ALLOW_OPEN_INSTALL", "1")
    c = world.client(("203.0.113.40", 41004), origin=False)
    r = c.post("/api/threads", headers={"Origin": f"http://{EVIL}"}, json={"bot_id": "alpha"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "setup_required"
    r = c.post("/api/auth/setup", headers={"Origin": f"http://{EVIL}"}, json={"new_pin": "2468"})
    assert r.status_code == 403 and not policy.claimed()
    # Without an Origin (or with our own) it behaves as before.
    assert c.get("/api/bots", headers={"Host": EVIL}).status_code == 200
    r = c.post("/api/auth/setup", json={"new_pin": "2468"})
    assert r.status_code == 200, r.text


# --------------------------------------------------------------------------- #
# (e) (k) forwarded headers are believed from loopback and configured proxies only
# --------------------------------------------------------------------------- #

FWD = {"X-Forwarded-For": "203.0.113.9", "X-Forwarded-Proto": "https"}
FWD_LC = {k.lower(): v for k, v in FWD.items()}       # what Starlette's Headers look up


def _cookie_secure(resp) -> bool:
    sc = [v for k, v in resp.headers.multi_items() if k.lower() == "set-cookie"
          and v.startswith(policy.SESSION_COOKIE + "=")]
    assert sc, resp.headers
    return "secure" in sc[0].lower()


def test_forwarded_proto_only_from_a_trusted_peer(world):
    world.create_owner()
    far = world.client(("192.0.2.55", 41005))
    r = far.post("/api/auth/login", headers=FWD, json={"name": "Olive", "password": "owner-pass1"})
    assert r.status_code == 200, r.text
    assert not _cookie_secure(r)
    near = world.client(("127.0.0.1", 41006))
    r = near.post("/api/auth/login", headers=FWD, json={"name": "Olive", "password": "owner-pass1"})
    assert r.status_code == 200, r.text
    assert _cookie_secure(r)


def test_a_configured_proxy_range_is_believed(world, monkeypatch):
    monkeypatch.setenv("DISPATCH_TRUSTED_PROXIES", "198.51.100.0/24")
    assert policy.quota_ip(FWD_LC, "198.51.100.5") == "203.0.113.9"
    assert policy.quota_ip(FWD_LC, "192.0.2.55") == "192.0.2.55"
    world.create_owner()
    caddy = world.client(("198.51.100.5", 41007))
    r = caddy.post("/api/auth/login", headers=FWD, json={"name": "Olive", "password": "owner-pass1"})
    assert r.status_code == 200 and _cookie_secure(r)
    other = world.client(("192.0.2.55", 41008))
    r = other.post("/api/auth/login", headers=FWD, json={"name": "Olive", "password": "owner-pass1"})
    assert r.status_code == 200 and not _cookie_secure(r)


def test_security_yaml_trusted_proxies_work_too(world, monkeypatch):
    monkeypatch.delenv("DISPATCH_TRUSTED_PROXIES", raising=False)
    auth.set_pin("2468")
    cfg = auth.load()
    cfg.trusted_proxies = ["192.0.2.10"]
    auth._write(cfg)
    auth._bust_cache()
    assert policy.trusted_proxy_peer("192.0.2.10")
    assert not policy.trusted_proxy_peer("192.0.2.11")
    assert policy.quota_ip(FWD_LC, "192.0.2.10") == "203.0.113.9"


# --------------------------------------------------------------------------- #
# (f) a presented key must be right, whatever the loopback trust
# --------------------------------------------------------------------------- #


def test_a_wrong_key_from_loopback_is_refused_even_with_loopback_trust(world):
    owner = world.create_owner()
    token = owner.post("/api/auth/api-token", json={"rotate": True}).json()["api_token"]
    r = owner.post("/api/auth/api-token", json={"machine_loopback_trust": True})
    assert r.status_code == 200 and auth.load().machine_loopback_trust is True
    local = world.client(("127.0.0.1", 41009), origin=False)
    body = {"bot_id": "alpha", "role": "user", "content": "hi"}
    r = local.post("/api/inject", headers={"X-API-Key": "not-the-key"}, json=body)
    assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_api_key", r.text
    r = local.post("/api/inject", headers={"Authorization": "Bearer not-the-key"}, json=body)
    assert r.status_code == 401
    # No key: an on-box machine, as before (trust is on).
    assert local.post("/api/inject", json=body).status_code == 200
    # The right key works.
    assert local.post("/api/inject", headers={"X-API-Key": token}, json=body).status_code == 200


# --------------------------------------------------------------------------- #
# (g) fresh installs start without loopback trust; upgrades keep theirs
# --------------------------------------------------------------------------- #


def test_fresh_install_writes_loopback_trust_false(world):
    assert not auth.SECURITY_PATH.exists()
    assert auth.load().machine_loopback_trust is False
    world.create_owner()
    assert "machine_loopback_trust: false" in auth.SECURITY_PATH.read_text()


def test_an_existing_file_without_the_key_reads_true(world):
    auth.SECURITY_PATH.write_text("lock_timeout_seconds: 600\n")
    auth._bust_cache()
    assert auth.load().machine_loopback_trust is True


# --------------------------------------------------------------------------- #
# (h) N-15: the household-character refusal names a real permission
# --------------------------------------------------------------------------- #


def test_a_disallowed_character_names_use_chat(world, monkeypatch):
    owner = world.create_owner()
    monkeypatch.setattr(main, "_household_character_item_denied", lambda *a, **k: True)
    r = owner.get("/api/bots")
    assert r.status_code == 403
    body = r.json()
    assert body["required_permission"] == "use_chat"
    assert body["reason"] == "character_not_allowed"


def test_clear_route_cache_is_gone():
    # N-13: RouteIndex.match never caches an add-on path (extensions.owns_path bypass) and
    # core routes do not change at runtime, so the uncalled helper was deleted.
    assert not hasattr(policy, "clear_route_cache")


# --------------------------------------------------------------------------- #
# Hardening: no route grants anything to an unclaimed install under a foreign Host
# --------------------------------------------------------------------------- #


def _concrete(path: str) -> str:
    import re
    return re.sub(r"\{[^}]+\}", "x", path)


def test_no_route_serves_an_unclaimed_install_under_a_foreign_host(world, strict):
    from fastapi.routing import APIRoute
    c = world.client(("127.0.0.1", 41010), origin=False)
    checked = 0
    for route in main.app.router.routes:
        if not isinstance(route, APIRoute):
            continue
        for method in sorted(route.methods or ()):
            if method in ("HEAD", "OPTIONS"):
                continue
            tmpl, req = main._route_index.match(method, _concrete(route.path))
            if req is None or req.public:
                continue
            r = c.request(method, _concrete(route.path), headers={"Host": EVIL})
            assert r.status_code == 403, (method, route.path, r.status_code)
            assert r.json()["error"]["code"] == "setup_required", (method, route.path)
            checked += 1
    assert checked > 100
    assert not policy.claimed()
