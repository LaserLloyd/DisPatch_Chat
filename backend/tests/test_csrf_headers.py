"""CSRF / Origin protection on the core mutating routes, cookie flags and the baseline
security headers (3.0).

A cookie is ambient authority: a page on another origin can make the browser send it.
The gate therefore refuses a state-changing request that carries a session (or device)
cookie when its Origin is not ours or Sec-Fetch-Site says cross-site / same-site (another
port on the same host). A caller with no cookie has no ambient authority to abuse, and a
script that sends neither header is judged by its credential alone.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def owner(world):
    return world.create_owner()


EVIL = {"Origin": "https://evil.example"}


def test_a_cross_origin_post_with_the_session_cookie_is_refused(world, owner):
    r = owner.post("/api/threads", json={"bot_id": "alpha"}, headers=EVIL)
    assert r.status_code == 403 and r.json()["error"]["code"] == "csrf_origin"
    assert r.json()["detail"] == "cross-origin request refused"


@pytest.mark.parametrize("site", ["cross-site", "same-site"])
def test_sec_fetch_site_must_say_same_origin(world, owner, site):
    r = owner.post("/api/threads", json={"bot_id": "alpha"},
                   headers={"Origin": "http://testserver", "Sec-Fetch-Site": site})
    assert r.status_code == 403 and r.json()["error"]["code"] == "csrf_origin"
    ok = owner.post("/api/threads", json={"bot_id": "alpha"},
                    headers={"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"})
    assert ok.status_code == 200


def test_body_less_mutations_are_covered_too(world, owner):
    """FastAPI would accept a bare POST from a CORS 'simple' request; the gate does not."""
    for method, path in (("post", "/api/auth/forget-devices"), ("post", "/api/auth/lock"),
                         ("post", "/api/harness/restart"), ("post", "/api/reactions/reseed"),
                         ("delete", "/api/threads/x")):
        r = getattr(owner, method)(path, headers=EVIL)
        assert r.status_code == 403, path
    assert owner.get("/api/auth/me").json()["role"] == "owner"        # lock was NOT executed


def test_reads_are_not_affected(world, owner):
    assert owner.get("/api/bots", headers=EVIL).status_code == 200


def test_a_script_without_browser_headers_is_judged_by_its_cookie_alone(world, owner):
    owner.headers.pop("Origin", None)
    assert owner.post("/api/threads", json={"bot_id": "alpha"}).status_code == 200


def test_an_anonymous_caller_has_no_cookie_to_abuse(world):
    world.create_owner()
    anon = world.client(("203.0.113.9", 1))
    anon.headers.pop("Origin", None)
    r = anon.post("/api/threads", json={"bot_id": "alpha"}, headers=EVIL)
    assert r.status_code == 200          # Restricted may start a thread; no ambient authority


def test_a_device_cookie_alone_also_triggers_the_check(world, owner):
    mia = world.person(owner, "Mia", "restricted")
    owner.put("/api/people/devices/this", json={"profile_id": mia["id"]})
    tablet = world.client(("203.0.113.9", 1))
    tablet.cookies.set("lc_device", owner.cookies.get("lc_device"))
    assert tablet.post("/api/threads", json={"bot_id": "alpha"}, headers=EVIL).status_code == 403
    assert tablet.post("/api/threads", json={"bot_id": "alpha"}).status_code == 200      # our own origin


def _session_cookie(response):
    return next(x for x in response.headers.get_list("set-cookie") if x.startswith("lc_session="))


def test_the_session_cookie_is_httponly_and_samesite_strict(world):
    c = world.client()
    r = c.post("/api/auth/setup", json={"password": "owner-pass1"})
    sess = _session_cookie(r)
    assert "HttpOnly" in sess and "SameSite=strict" in sess and "Secure" not in sess    # plain http
    assert "Max-Age" not in sess                                                           # a browser-session cookie


def test_secure_is_set_over_tls_and_behind_a_proxy_that_says_so(world):
    c = world.client()
    r = c.post("/api/auth/setup", json={"password": "owner-pass1"},
               headers={"X-Forwarded-For": "198.51.100.9", "X-Forwarded-Proto": "https"})
    assert "Secure" in _session_cookie(r)
    assert r.headers.get("strict-transport-security", "").startswith("max-age=")


def test_x_forwarded_proto_alone_cannot_set_secure_on_a_plain_request(world):
    """A client must not be able to pin Secure on a cookie the browser would then drop."""
    c = world.client()
    r = c.post("/api/auth/setup", json={"password": "owner-pass1"},
               headers={"X-Forwarded-Proto": "https"})
    assert "Secure" not in _session_cookie(r)
    assert "strict-transport-security" not in r.headers


def test_baseline_headers_are_on_every_answer(world, owner):
    for path in ("/", "/api/health", "/api/bots", "/api/auth/status", "/api/no-such-route"):
        h = owner.get(path).headers
        assert h["x-content-type-options"] == "nosniff", path
        assert h["referrer-policy"], path
        if path != "/":                      # the shell says it with CSP frame-ancestors
            assert h["x-frame-options"] == "SAMEORIGIN", path
    assert owner.get("/api/auth/status").headers["cache-control"] == "no-store"
    assert owner.get("/api/people").headers["cache-control"] == "no-store"


def test_the_shell_cannot_be_framed(world):
    r = world.client().get("/")
    assert "frame-ancestors 'self'" in r.headers["content-security-policy"]
