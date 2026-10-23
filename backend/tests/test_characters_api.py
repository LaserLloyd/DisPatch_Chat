"""Characters (3.0, W5): the new API, the /api/bots aliases, connection assignment, personality
presets, per-profile visibility, and the starter packs.

Everything goes through the real app (the ``world`` fixture): gate, policy engine, config.yaml.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from app import config, connections, llm_api, personas, starter_packs
from app.contracts.manifests import CharacterManifest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def owner(world):
    return world.create_owner()


@pytest.fixture
def conn(owner):
    r = owner.post("/api/connections", json={"type": "lmstudio", "default_model": "qwen"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _roster():
    return {e["id"]: e for e in yaml.safe_load(config.CONFIG_PATH.read_text(encoding="utf-8"))["bots"]}


# --------------------------------------------------------------------------- #
# CRUD and the connection reference
# --------------------------------------------------------------------------- #


def test_create_a_character_on_a_connection(owner, conn):
    r = owner.post("/api/characters", json={"name": "Nova", "emoji": "N", "connection": conn, "model": "qwen-7b",
                                            "description": "Quick and curious", "personality": "tutor"})
    assert r.status_code == 201, r.text
    ch = r.json()
    assert ch["backend"] == {"kind": "direct", "connection": conn, "model": "qwen-7b", "agent": None}
    assert "audience" not in ch and "safe" not in ch and ch["description"] == "Quick and curious"
    assert ch["personality"]["preset"] == "tutor" and ch["voice_link"] == {
        "section": "voice", "anchor": "voices", "character": "nova"}
    entry = _roster()["nova"]["advisor"]["providers"][0]
    assert entry == {"provider": "lmstudio", "base_url": "http://127.0.0.1:1234/v1", "model": "qwen-7b",
                     "connection": conn}                                    # inline stays readable
    assert "api_key" not in entry
    # it resolves through the connection exactly like an inline block would
    res = llm_api.resolve_api(entry, "Nova", "nova")
    assert (res.provider.id, res.model) == ("lmstudio", "qwen-7b")
    assert connections.get(conn) is not None and "nova" in owner.get(f"/api/connections/{conn}").json()["in_use_by"]


def test_patch_connection_model_personality_and_detach(owner, conn):
    owner.post("/api/characters", json={"name": "Nova", "connection": conn, "model": "qwen-7b"})
    other = owner.post("/api/connections", json={"type": "ollama"}).json()["id"]
    r = owner.patch("/api/characters/nova", json={"connection": other, "model": "llama3"})
    assert r.status_code == 200 and r.json()["backend"]["connection"] == other and r.json()["backend"]["model"] == "llama3"
    assert _roster()["nova"]["advisor"]["providers"][0]["provider"] == "ollama"
    r = owner.patch("/api/characters/nova", json={"model": "llama3.1"})
    assert r.json()["backend"]["model"] == "llama3.1" and r.json()["backend"]["connection"] == other
    r = owner.patch("/api/characters/nova", json={"personality": "contrarian"})
    assert r.json()["personality"]["preset"] == "contrarian"
    text = personas.current(config.get_bot("nova"))[0]
    assert text == next(p.text for p in personas.presets() if p.id == "contrarian")
    r = owner.patch("/api/characters/nova", json={"personality": "default"})
    assert r.json()["personality"]["preset"] == "default" and r.json()["personality"]["custom"] is False
    r = owner.patch("/api/characters/nova", json={"persona": "You are terse."})
    assert r.json()["personality"]["preset"] == "custom" and r.json()["personality"]["custom"] is True
    r = owner.patch("/api/characters/nova", json={"connection": None})            # detach
    assert r.json()["backend"]["connection"] is None
    assert "connection" not in _roster()["nova"]["advisor"]["providers"][0]
    # errors are catalogued
    r = owner.patch("/api/characters/nova", json={"connection": "ghost"})
    assert r.status_code == 404 and r.json()["error"]["code"] == "connection_not_found"
    r = owner.patch("/api/characters/nova", json={"personality": "nope"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "character_invalid"
    assert owner.patch("/api/characters/ghost", json={"name": "x"}).json()["error"]["code"] == "bot_not_found"
    assert owner.patch("/api/characters/nova", json={}).status_code == 422
    assert owner.patch("/api/characters/nova", json={"bogus": 1}).status_code == 422


def test_an_agent_character_has_no_connection(owner, conn):
    r = owner.post("/api/characters", json={"name": "Runner", "backend": "agent", "agent": "runner"})
    assert r.status_code == 201 and r.json()["backend"]["kind"] == "agent"
    r = owner.patch("/api/characters/runner", json={"connection": conn})
    assert r.status_code == 422 and "agent" in r.json()["detail"].lower()
    assert owner.post("/api/characters", json={"name": "X", "backend": "agent", "connection": conn}).status_code == 422


def test_delete_and_the_last_character_is_protected(owner):
    owner.post("/api/characters", json={"name": "Temp"})
    assert owner.delete("/api/characters/temp").json() == {"removed": "temp"}
    assert owner.delete("/api/characters/temp").status_code == 404


def test_removing_the_only_character_is_refused(tmp_path):
    from app import characters
    config._write_bots([config._bot_entry(config.Bot(id="solo", name="Solo"))])
    with pytest.raises(characters.CharacterError) as e:
        characters.remove("solo")
    assert e.value.code == "character_last" and e.value.status == 409


def test_the_bots_aliases_return_the_same_data_under_the_old_name(owner, conn):
    owner.post("/api/characters", json={"name": "Nova", "connection": conn, "model": "m"})
    old = {b["id"]: b for b in owner.get("/api/bots/all").json()["bots"]}["nova"]
    assert old["backend"] == "direct" and old["connection"] == conn and old["model"] == "m"
    assert owner.get("/api/bots").json()["bots"] and owner.get("/api/characters").json()["characters"]
    r = owner.post("/api/bots", json={"name": "Old Way"})
    assert r.status_code == 201 and r.json()["id"] == "old-way"
    assert owner.patch("/api/bots/old-way", json={"emoji": "O"}).json()["emoji"] == "O"
    assert owner.delete("/api/bots/old-way").json() == {"removed": "old-way"}
    assert owner.get("/api/bots/personas").json() == owner.get("/api/characters/personas").json()


# --------------------------------------------------------------------------- #
# Who may see what
# --------------------------------------------------------------------------- #


def test_the_3x_profile_list_is_refused(owner):
    r = owner.post("/api/characters", json={"name": "Bad", "profiles": ["nobody"]})
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"


def test_only_manage_characters_may_change_characters(world, owner):
    max_ = world.person(owner, "Max", "member", pin="5555")
    member = world.client(("203.0.113.64", 41004))
    world.login(member, profile_id=max_["id"], pin="5555")
    for c in (member, world.client(("203.0.113.65", 41005))):
        assert c.post("/api/characters", json={"name": "Nope"}).status_code in (401, 403)
        assert c.patch("/api/characters/atlas", json={"name": "Nope"}).status_code in (401, 403)
        assert c.get("/api/characters/all").status_code in (401, 403)
        assert c.get("/api/starter-packs").status_code in (401, 403)
        assert c.post("/api/starter-packs/assistants/install").status_code in (401, 403)


# --------------------------------------------------------------------------- #
# Starter packs
# --------------------------------------------------------------------------- #

#: Names and places from the maintainer's own roster and notes that must never ship in a pack.
_PERSONAL = re.compile(r"(?i)\b(lisbon|portugal|wikka|nova|scout|atlas|lloyd|laserlloyd|sam\b|jlloy|@)")  # scrub-ok: the deny-list of names a pack must never contain


def test_the_packs_load_and_every_character_fits_the_character_manifest():
    packs = starter_packs._load_all()
    assert set(packs) == {"assistants"}
    for p in packs.values():
        assert p["characters"]
        for c in p["characters"]:
            assert isinstance(c, CharacterManifest) and c.pack == p["id"]
            assert re.fullmatch(r"[A-Za-z0-9_-]{1,48}", c.id)


def test_a_pack_can_be_installed_on_a_connection(world, owner, conn):
    r = owner.post("/api/starter-packs/assistants/install",
                   json={"connection": conn, "model": "qwen", "only": ["scribe", "dev"]})
    assert r.json()["installed"] == ["scribe", "dev"]
    e = _roster()["scribe"]
    assert e["advisor"]["providers"][0]["connection"] == conn
    assert "planner" not in _roster()
    assert owner.post("/api/starter-packs/assistants/install", json={"only": ["ghost"]}).status_code == 404
    assert owner.post("/api/starter-packs/ghost/install").json()["error"]["code"] == "starter_pack_not_found"


def test_the_assistants_pack_is_for_members(owner):
    r = owner.post("/api/starter-packs/assistants/install")
    assert r.json()["installed"] == ["sage", "scribe", "dev", "planner"]
    for cid in r.json()["installed"]:
        assert _roster()[cid]["audience"] == "members" and _roster()[cid]["advisor"]["tools"] is True
