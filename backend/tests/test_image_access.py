"""WP2 (Civitai plan): the per-profile-type image generator access matrix.

Run: cd backend && uv run pytest tests/test_image_access.py
"""
from __future__ import annotations

import pytest

from app import accounts, image_access, policy, settings_schema, settings_store

KEYS = [image_access.setting_key(g, r) for g in image_access.GENERATORS for r in image_access.ROLES]
LAN = ("203.0.113.82", 43002)


def _prin(world, owner, name, role):
    if role == "admin":
        world.person(owner, name, role, password="pw-pass-1234")  # scrub-ok: a throwaway test profile password
    else:
        world.person(owner, name, role, pin="1357")
    return policy.profile_principal(accounts.get_by_name(name))


@pytest.fixture
def people(world):
    owner = world.create_owner(world.client())
    return world, owner, {
        "owner": policy.profile_principal(accounts.get_protected_admin()),
        "admin": _prin(world, owner, "Ada", "admin"),
        "user": _prin(world, owner, "Ula", "user"),
        "basic": _prin(world, owner, "Bo", "basic"),
    }


def _cells(p):
    return image_access.allows(p, "standard"), image_access.allows(p, "civitai")


def test_defaults_cell_by_cell(people):
    _w, _o, p = people
    assert _cells(p["owner"]) == (True, True)
    assert _cells(p["admin"]) == (True, True)
    assert _cells(p["user"]) == (True, False)
    assert _cells(p["basic"]) == (False, False)


@pytest.mark.parametrize("gen,role", [(g, r) for g in image_access.GENERATORS for r in image_access.ROLES])
def test_each_flip_changes_exactly_its_cell(people, gen, role):
    _w, _o, p = people
    before = {(g, r): image_access.allows(p[r], g) for g in image_access.GENERATORS for r in image_access.ROLES}
    key = image_access.setting_key(gen, role)
    settings_store.set_many({key: not settings_store.get(key)})
    after = {(g, r): image_access.allows(p[r], g) for g in image_access.GENERATORS for r in image_access.ROLES}
    assert [c for c in before if before[c] != after[c]] == [(gen, role)]


def test_owner_stays_true_with_everything_off(people):
    _w, _o, p = people
    settings_store.set_many({k: False for k in KEYS})
    assert _cells(p["owner"]) == (True, True)
    assert _cells(p["admin"]) == (False, False)


def test_open_install_machine_and_garbage():
    assert image_access.allows(policy.open_principal(), "standard")
    assert image_access.allows(policy.open_principal(), "civitai")
    m = policy.machine_principal("api_token")
    assert not image_access.allows(m, "standard") and not image_access.allows(m, "civitai")
    assert not image_access.allows(None, "standard")
    assert not image_access.allows(policy.nobody(), "standard")
    assert not image_access.allows(policy.open_principal(), "bogus")
    assert not image_access.allows_profile(None, "standard")


def test_lock_target_user_never_gets_civitai(people, monkeypatch):
    _w, _o, p = people
    settings_store.set_many({image_access.setting_key("civitai", "user"): True})
    assert image_access.allows(p["user"], "civitai") is True
    uid = p["user"].profile_id
    monkeypatch.setattr(policy, "is_lock_target", lambda prof, cfg=None: bool(prof) and prof.id == uid)
    assert image_access.allows(p["user"], "civitai") is False
    assert image_access.allows(p["user"], "standard") is True
    assert image_access.allows_profile(accounts.get_by_name("Ula"), "civitai") is False


def test_schema_rows():
    for k in KEYS + ["connections.image_civitai_key"]:
        d = settings_schema.get_def(k)
        assert d is not None and d.owner_only is True, k
        assert d.ui_section == "connections#images", k
    for k in KEYS:
        assert settings_schema.get_def(k).type == "boolean"
    key = settings_schema.get_def("connections.image_civitai_key")
    assert key.secret is True and key.env == "CIVITAI_API_KEY" and key.legacy_prefix_alias is False
    assert key.env_names() == ("CIVITAI_API_KEY",)


def test_user_cannot_patch_the_matrix(people):
    world, owner, _p = people
    c = world.client(LAN)
    assert world.login(c, name="Ula", pin="1357").status_code == 200
    for k in KEYS:
        r = c.patch("/api/settings", json={"set": {k: True}})
        assert r.status_code == 403, (k, r.text)
    assert settings_store.get(image_access.setting_key("civitai", "user")) is False
    r = owner.patch("/api/settings", json={"set": {image_access.setting_key("civitai", "user"): True}})
    assert r.status_code == 200, r.text
