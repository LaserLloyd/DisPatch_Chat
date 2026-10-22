"""4.0 (INT-2 lead decision): the 3.x character visibility inputs are gone on write.

Who uses a character is each profile's household list (People) and the Admin's "Hide" is the
``hidden`` field. So:

* ``audience``, ``safe``, ``hide_unlocked``, ``visible`` and ``profiles`` are refused (422) by
  every write route that used to take them: ``POST/PATCH /api/characters``, ``POST/PATCH
  /api/bots`` and the Bot Manager's ``PUT /api/bots/order`` / ``/api/characters/order``;
* a starter pack file that still carries them installs with the keys ignored;
* nothing derives ``hidden`` from a legacy ``audience: restricted`` (or ``hide_unlocked``) left
  in config.yaml - migration 010 folded those into ``hidden`` once, and ``is_hidden`` reads
  ``hidden`` alone.

Run: cd backend && uv run pytest tests/test_legacy_character_inputs.py
"""
from __future__ import annotations

import pytest

from app import characters, config
from tests.test_platform_bots import env

PIN = "1234"
LEGACY = [{"audience": "restricted"}, {"safe": True}, {"hide_unlocked": True},
          {"visible": False}, {"profiles": []}]


def _admin(env):
    c = env(real_defaults=True)
    assert c.post("/api/auth/setup", json={"new_pin": PIN}).status_code == 200
    return c


@pytest.mark.parametrize("extra", LEGACY, ids=lambda d: next(iter(d)))
def test_character_writes_refuse_the_legacy_keys(env, extra):
    c = _admin(env)
    assert c.post("/api/characters", json={"name": "Probe", **extra}).status_code == 422
    assert c.post("/api/bots", json={"name": "Probe", **extra}).status_code == 422
    assert c.patch("/api/characters/atlas", json=extra).status_code == 422
    assert c.patch("/api/bots/atlas", json=extra).status_code == 422
    for path in ("/api/bots/order", "/api/characters/order"):
        r = c.put(path, json={"bots": [{"id": "atlas", "order": 0, **extra}]})
        assert r.status_code == 422, (path, r.text)


def test_hidden_is_the_one_hide_switch(env):
    c = _admin(env)
    r = c.patch("/api/characters/atlas", json={"hidden": True})
    assert r.status_code == 200, r.text
    config._invalidate_bots_cache()
    assert characters.is_hidden(config.get_bot("atlas"))
    assert c.patch("/api/characters/atlas", json={"hidden": False}).status_code == 200
    config._invalidate_bots_cache()
    assert not characters.is_hidden(config.get_bot("atlas"))


def test_a_legacy_audience_left_in_config_hides_nothing(env):
    c = _admin(env)
    (env.tmp_path / "config.yaml").write_text(
        "bots:\n"
        "- {id: atlas, name: Atlas}\n"
        "- {id: probe_a, name: Probe A, audience: restricted, safe: true}\n"
        "- {id: probe_b, name: Probe B, safe: true, hide_unlocked: true}\n"
        "- {id: probe_c, name: Probe C, hidden: true}\n")
    config._invalidate_bots_cache()
    assert not characters.is_hidden(config.get_bot("probe_a"))
    assert not characters.is_hidden(config.get_bot("probe_b"))
    assert characters.is_hidden(config.get_bot("probe_c"))
    rows = {b["id"]: b for b in c.get("/api/characters/all").json()["characters"]}
    assert rows["probe_a"]["flags"]["hidden"] is False
    assert "hide_unlocked" not in rows["probe_a"]["flags"]


def test_a_pack_file_with_legacy_keys_still_installs(env, monkeypatch, tmp_path):
    from app import starter_packs
    c = _admin(env)
    d = tmp_path / "packs"
    d.mkdir()
    (d / "probe.yaml").write_text(
        "schema: 1\nid: probe\nname: Probe pack\naudience: restricted\n"
        "characters:\n  - {id: probe_x, name: Probe X, audience: restricted, safe: true, profiles: [p1]}\n")
    monkeypatch.setattr(starter_packs, "PACK_DIR", d)
    starter_packs._load_all.cache_clear()
    try:
        r = c.post("/api/starter-packs/probe/install", json={})
        assert r.status_code == 200, r.text
        assert r.json()["installed"] == ["probe_x"]
        config._invalidate_bots_cache()
        assert not characters.is_hidden(config.get_bot("probe_x"))
        assert c.post("/api/starter-packs/probe/install", json={"profiles": ["p1"]}).status_code == 422
    finally:
        starter_packs._load_all.cache_clear()
