"""RV-auth (LOW): /api/auth/switch revokes the caller's current session BEFORE the new one is
issued; when the target profile is at its session cap with only remembered devices
(TooManySessions -> 429 too_many_sessions) the caller is left signed out of both."""
from __future__ import annotations

from app import auth


def test_a_refused_switch_keeps_the_current_session(world, monkeypatch):
    owner = world.create_owner()
    world.person(owner, "Kid", "user", pin="1357")
    auth.set_remember_days(30)
    phone = world.client(("203.0.113.71", 40071))
    r = phone.post("/api/auth/login", json={"name": "Kid", "pin": "1357", "remember": True})
    assert r.status_code == 200 and r.json()["remembered"], r.text
    monkeypatch.setattr(auth, "_MAX_SESSIONS_PER_PROFILE", 1)
    assert owner.get("/api/auth/me").json()["signed_in"]
    r = owner.post("/api/auth/switch", json={"name": "Kid", "pin": "1357"})
    assert r.status_code == 429 and r.json()["error"]["code"] == "too_many_sessions", r.text
    me = owner.get("/api/auth/me").json()
    assert me["signed_in"] and me["protected"], me
