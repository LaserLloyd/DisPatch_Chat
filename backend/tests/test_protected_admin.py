"""The protected first Admin (4.0, SPEC §1): it cannot be demoted, disabled or deleted, and no
other Admin changes its credentials, so the install can never be locked out.

Run: cd backend && uv run pytest tests/test_protected_admin.py
"""
from __future__ import annotations

import pytest

from app import accounts, auth


@pytest.fixture
def two_admins(world):
    owner = world.create_owner(world.client())
    world.person(owner, "Ann", "admin", password="ann-pass-1")
    from agents_world import sibling
    ann = sibling(world, owner, ("203.0.113.140", 47000))
    assert world.login(ann, name="Ann", password="ann-pass-1").status_code == 200
    oid = accounts.get_protected_admin().id
    return owner, ann, oid


def test_another_admin_cannot_touch_the_protected_admin(two_admins):
    _owner, ann, oid = two_admins
    for method, path, body in (
            ("PATCH", f"/api/people/{oid}", {"role": "user"}),
            ("PATCH", f"/api/people/{oid}", {"disabled": True}),
            ("DELETE", f"/api/people/{oid}", None),
            ("POST", f"/api/people/{oid}/pin", {"pin": "1357"}),
            ("DELETE", f"/api/people/{oid}/pin", None),
            ("POST", f"/api/people/{oid}/password", {"password": "hijack-pass-9"})):
        r = ann.request(method, path, json=body)
        assert r.status_code in (403, 409), (method, path, r.status_code, r.text)
    prof = accounts.get_protected_admin()
    assert prof is not None and prof.id == oid and not prof.disabled


def test_the_protected_admin_cannot_demote_or_delete_itself(two_admins):
    owner, _ann, oid = two_admins
    assert owner.patch(f"/api/people/{oid}", json={"role": "user"}).status_code == 409
    assert owner.delete(f"/api/people/{oid}").status_code == 409
    with pytest.raises(accounts.AccountError):
        accounts.delete_profile(oid)


def test_removing_the_pin_never_deletes_the_protected_row(world):
    c = world.client()
    r = c.post("/api/auth/setup", json={"name": "Solo", "new_pin": "2468"})
    assert r.status_code == 200, r.text
    oid = accounts.get_protected_admin().id
    cfg = auth.load()
    cfg.pin_hash = None
    auth._sync_owner_pin(cfg)
    assert accounts.get_protected_admin() is not None
    assert accounts.get_protected_admin().id == oid
