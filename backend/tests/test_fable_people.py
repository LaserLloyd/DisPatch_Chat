"""4.3 Fable (people): a second Admin may READ the machine-key settings but neither rotate the
key nor switch "Trust programs on this computer" / the scope list: both change who can reach
DisPatch without a credential and would break the Owner's own scripts (host tier)."""
from __future__ import annotations

from test_i43_host_tier import _install

from app import auth


def test_second_admin_reads_but_cannot_change_the_machine_key(world):
    owner, ann, _ = _install(world)
    token = owner.post("/api/auth/api-token", json={"rotate": True}).json()["api_token"]
    assert ann.get("/api/auth/api-token").status_code == 200        # read stays every Admin's
    trust_before = auth.load().machine_loopback_trust
    for body in ({"rotate": True}, {"machine_loopback_trust": True},
                 {"machine_scopes": ["machine:inject"]}):
        r = ann.post("/api/auth/api-token", json=body)
        assert r.status_code == 403, (body, r.text)
        assert r.json()["error"]["code"] == "host_owner_only", (body, r.text)
    assert auth.verify_api_token(token), "a refused rotate must not touch the key"
    assert auth.load().machine_loopback_trust == trust_before
    r = owner.post("/api/auth/api-token", json={"machine_loopback_trust": False})
    assert r.status_code == 200, r.text


def test_a_user_is_still_refused_the_machine_key(world):
    owner, _ann, _ = _install(world)
    uma = world.person(owner, "Uma", "user", pin="2468")
    u = world.client()
    assert world.login(u, profile_id=uma["id"], pin="2468").status_code == 200
    assert u.get("/api/auth/api-token").status_code == 403
    assert u.post("/api/auth/api-token", json={"rotate": True}).status_code == 403


def test_second_admin_cannot_flip_machine_trust_through_settings_either(world):
    owner, ann, _ = _install(world)
    before = auth.load().machine_loopback_trust
    r = ann.patch("/api/settings", json={"set": {"people.machine_loopback_trust": not before}})
    assert r.status_code == 403 and r.json()["error"]["code"] == "host_owner_only", r.text
    assert auth.load().machine_loopback_trust == before
    r = owner.patch("/api/settings", json={"set": {"people.machine_loopback_trust": not before}})
    assert r.status_code == 200, r.text
    assert auth.load().machine_loopback_trust == (not before)
