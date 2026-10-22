"""Tamper-evident credential reset (4.0, BUILD-PLAN-v2 §1.4): when an Admin sets or clears
somebody else's PIN (or password), the profile is signed out, the reset is audited, and its
next sign-in carries a notice ``{kind: "pin_reset", by, at}`` on /api/auth/me and
/api/auth/status - for that session only.

Run: cd backend && uv run pytest tests/test_pin_reset_notice.py
"""
from __future__ import annotations

from app import accounts


def test_an_admin_pin_reset_is_noticed_once(world):
    owner = world.create_owner(world.client())
    p = world.person(owner, "Uma", "user", pin="1111")
    from agents_world import sibling
    uma = sibling(world, owner, ("203.0.113.150", 48000))
    assert world.login(uma, profile_id=p["id"], pin="1111").status_code == 200
    assert uma.get("/api/auth/me").json()["notices"] == []
    assert owner.post(f"/api/people/{p['id']}/pin", json={"pin": "2222"}).status_code == 200
    # signed out by the reset
    assert uma.get("/api/auth/me").json().get("profile", {}) in (None, {}) or \
        uma.get("/api/auth/me").json()["signed_in"] is False
    assert world.login(uma, profile_id=p["id"], pin="2222").status_code == 200
    notes = uma.get("/api/auth/me").json()["notices"]
    assert [n["kind"] for n in notes] == ["pin_reset"] and notes[0]["by"] == "Olive" and notes[0]["at"]
    assert uma.get("/api/auth/status").json()["notices"] == notes
    rows = accounts.recent_activity(None, category="audit", limit=50)
    assert any(r.get("kind") == "pin_set" for r in rows)
    # the next session does not see it again
    uma2 = sibling(world, owner, ("203.0.113.151", 48001))
    assert world.login(uma2, profile_id=p["id"], pin="2222").status_code == 200
    assert uma2.get("/api/auth/me").json()["notices"] == []


def test_changing_your_own_pin_leaves_no_notice(world):
    owner = world.create_owner(world.client())
    p = world.person(owner, "Una", "user", pin="1111")
    from agents_world import sibling
    una = sibling(world, owner, ("203.0.113.152", 48002))
    assert world.login(una, profile_id=p["id"], pin="1111").status_code == 200
    assert una.post("/api/auth/pin", json={"current": "1111", "new": "3333"}).status_code == 200
    assert una.get("/api/auth/me").json()["notices"] == []
