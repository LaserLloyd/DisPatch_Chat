"""The wizard's people step (4.0 chat-first profiles): Users, Basic profiles, the default profile.

Driven through the API like the rest of the wizard (tests/test_wizard_api.py).
"""
from __future__ import annotations

from test_wizard_api import _dangling_default, _lm_studio_on_loopback

from app import accounts, config, profiles_service


def test_the_default_profile_ignores_a_dangling_default_connection(world):
    c = world.create_owner()
    _dangling_default()
    assert c.get("/api/connections").json()["default"] == ""
    fam = c.post("/api/wizard/family", json={"people": []})
    assert fam.status_code == 200, fam.text
    mine = [b for b in config.load_bots() if b.owner_profile == fam.json()["default_profile"]]
    assert len(mine) == 1
    assert not any((p or {}).get("connection") for p in (mine[0].advisor or {}).get("providers", []))


def test_older_pages_may_still_send_the_pack_flags(world):
    c = world.create_owner()
    r = c.post("/api/wizard/family", json={"people": [], "install_animal_friends": True,
                                           "install_assistants": True})
    assert r.status_code == 200, r.text
    assert "packs" not in r.json()


def test_the_default_profile_is_made_once_and_the_lock_choice_is_kept(world):
    c = world.create_owner()
    first = c.post("/api/wizard/family", json={"people": []}).json()["default_profile"]
    again = c.post("/api/wizard/family", json={"people": []}).json()["default_profile"]
    assert first == again
    assert len([p for p in accounts.list_profiles() if p.name == "DisPatch"]) == 1
    assert len([b for b in config.load_bots() if b.owner_profile == first]) == 1     # one Atlas, not two
    profiles_service.set_lock_target(None)                       # an Admin chooses the profile list
    c.post("/api/wizard/family", json={"people": []})
    assert profiles_service.lock_target_profile_id() is None


def test_a_connection_added_later_reaches_the_personal_atlases(world, monkeypatch):
    c = world.create_owner()
    fam = c.post("/api/wizard/family", json={"people": []}).json()
    mine = [b for b in config.load_bots() if b.owner_profile == fam["default_profile"]][0]
    assert not config.direct_connected(mine)
    _lm_studio_on_loopback(monkeypatch)
    found = c.post("/api/wizard/detect", json={}).json()["found"]
    r = c.post("/api/wizard/connection", json={"type": "lmstudio", "base_url": found[0]["base_url"], "model": "qwen2.5-7b"})
    assert r.status_code == 200, r.text
    assert r.json()["personal_connected"] == 1
    assert config.direct_connected(config.get_bot(mine.id))


def test_the_first_admin_is_protected_and_sees_the_household_atlas_not_a_copy(world):
    c = world.create_owner()
    c.post("/api/wizard/family", json={"people": [{"name": "Max", "role": "user"}]})
    admin = accounts.get_protected_admin()
    assert admin is not None and admin.protected
    assert not [b for b in config.load_bots() if b.owner_profile == admin.id]


def test_a_basic_person_gets_the_household_bots_an_admin_picks(world):
    c = world.create_owner()
    r = c.post("/api/wizard/family", json={"people": [
        {"name": "Dee", "role": "basic", "household_bots": ["alpha"]}, {"name": "Eli", "role": "restricted"}]})
    assert r.status_code == 200, r.text
    by = {p.name: p for p in accounts.list_profiles()}
    assert by["Dee"].role.value == "basic" and by["Dee"].household_bots == ["alpha"]
    assert by["Eli"].household_bots == profiles_service.basic_default_household()     # nothing picked: the usable ones
    assert not [b for b in config.load_bots() if b.owner_profile in (by["Dee"].id, by["Eli"].id)]


def test_a_pin_is_optional_for_every_role(world):
    c = world.create_owner()
    r = c.post("/api/wizard/family", json={"people": [{"name": "Max", "role": "user"}, {"name": "Dee", "role": "basic"}]})
    assert r.status_code == 200
    assert not any(p.has_pin for p in accounts.list_profiles() if p.name in ("Max", "Dee"))
