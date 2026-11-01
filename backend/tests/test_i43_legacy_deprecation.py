"""4.3 L1: the legacy /api/bots mutating routes carry Deprecation + Link headers.

Headers only: never a status or body change; GET /api/bots and /api/bots/all stay clean
(agents, crons and the voice add-on read them).
"""
from __future__ import annotations

from test_platform_bots import env  # the fixture

PIN = "1234"


def _c(env):
    c = env(real_defaults=True)
    assert c.post("/api/auth/setup", json={"new_pin": PIN}).status_code == 200
    return c


def _deprecated(r, successor):
    assert r.headers.get("Deprecation") == "true", r.status_code
    assert f"<{successor}>" in r.headers.get("Link", "")
    assert 'rel="successor-version"' in r.headers["Link"]


def test_reads_carry_no_deprecation(env):
    c = _c(env)
    for path in ("/api/bots", "/api/bots/all"):
        r = c.get(path)
        assert r.status_code == 200
        assert "Deprecation" not in r.headers and "successor-version" not in r.headers.get("Link", "")


def test_mutations_carry_the_headers_and_keep_status_and_body(env):
    c = _c(env)
    r = c.get("/api/bots/personas")
    assert r.status_code == 200
    _deprecated(r, "/api/characters/personas")
    r = c.post("/api/bots", json={"name": "Dep Tester", "kind": "api"})
    _deprecated(r, "/api/characters")
    body = r.json()
    if r.status_code == 201:
        bid = body["id"]
        r2 = c.patch(f"/api/bots/{bid}", json={"emoji": "x"})
        _deprecated(r2, f"/api/characters/{bid}")
        assert r2.status_code == 200 and r2.json()["id"] == bid
        r3 = c.delete(f"/api/bots/{bid}")
        _deprecated(r3, f"/api/characters/{bid}")
        assert r3.status_code == 200 and r3.json() == {"removed": bid}
    r = c.put("/api/bots/order", json={"bots": []})
    _deprecated(r, "/api/characters/order")


def test_errors_carry_the_headers_too_and_status_is_unchanged(env):
    c = _c(env)
    r = c.patch("/api/bots/nope-not-here", json={"emoji": "x"})
    assert r.status_code in (404, 422)
    _deprecated(r, "/api/characters/nope-not-here")
    r = c.delete("/api/bots/nope-not-here")
    assert r.status_code == 404
    _deprecated(r, "/api/characters/nope-not-here")


def test_other_bot_subroutes_are_not_deprecated(env):
    c = _c(env)
    r = c.get("/api/bots/atlas/avatar")
    assert "Deprecation" not in r.headers
