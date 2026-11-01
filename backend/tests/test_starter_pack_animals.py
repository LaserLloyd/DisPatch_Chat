"""The Animal Friends starter pack (4.3): eight gentle animal characters for small children,
installed with their pictures, their kid-safe settings and the same ids a hand-made roster used,
so an install that already has them skips them.

Everything goes through the real app (the ``world`` fixture): gate, policy engine, config.yaml.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from PIL import Image

from app import config, starter_packs

ANIMALS = ["kids_lobster", "kids_fruitbat", "kids_octopus", "kids_bee", "kids_owl", "kids_frog",
           "kids_tortoise", "kids_penguin"]
#: What must never ship in a public pack: names from the maintainer's own household and notes,
#: e-mail addresses, home paths.
_PERSONAL = re.compile(r"(?i)\b(lisbon|portugal|wikka|scout|lloyd|laserlloyd|jlloy|puppy|bluefin)\b|@|/home/|/var/")  # scrub-ok: the deny-list a pack must never contain


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


def test_the_pack_loads_with_eight_animals_and_kid_safe_defaults():
    p = starter_packs.get_pack("animal_friends")
    assert [c.id for c in p["characters"]] == ANIMALS
    assert p["defaults"]["tools"] is False and p["defaults"]["quick_actions"] is False
    assert p["defaults"]["advisor"] == {"owner": "the child", "history_chars": 8000}
    for c in p["characters"]:
        assert c.voice.startswith("builtin:") and c.emoji and re.fullmatch(r"#[0-9a-f]{6}", c.color)
        assert c.description and len(c.persona) > 1000
        assert "grown-up" in c.persona and "Never ask for the child's name" in c.persona
        for text in (c.name, c.description, c.persona):
            assert not _PERSONAL.search(text), (c.id, _PERSONAL.search(text).group(0))


def test_every_animal_ships_a_small_picture_set():
    total = 0
    for cid in ANIMALS:
        files = starter_packs.avatar_files(cid)
        assert set(files) == {"face", "full", "thumb"}, cid
        for part, path in files.items():
            with Image.open(path) as im:
                assert im.format == "PNG" and im.size[0] == im.size[1] <= 512, (cid, part, im.size)
            total += path.stat().st_size
    assert total < 1_200_000, f"the pack's pictures grew to {total} bytes"


def test_a_pack_defaults_block_cannot_reach_beyond_its_characters(tmp_path, monkeypatch):
    d = tmp_path / "packs"
    d.mkdir()
    (d / "bad.yaml").write_text(
        "schema: 1\nid: bad\nname: Bad\ndefaults:\n  advisor: {knowledge_dir: /etc}\n"
        "characters:\n  - {id: bad_x, name: Bad X}\n")
    monkeypatch.setattr(starter_packs, "PACK_DIR", d)
    starter_packs._load_all.cache_clear()
    try:
        assert "bad" not in starter_packs._load_all()
    finally:
        starter_packs._load_all.cache_clear()


def test_the_list_marks_pictures_and_serves_them_to_managers_only(world, owner):
    packs = {p["id"]: p for p in owner.get("/api/starter-packs").json()["packs"]}
    assert set(packs) == {"assistants", "animal_friends"}
    owl = next(c for c in packs["animal_friends"]["characters"] if c["id"] == "kids_owl")
    assert owl["picture"] == "/api/starter-packs/animal_friends/pictures/kids_owl"
    assert owl["color"] and owl["installed"] is False
    assert all(c["picture"] == "" for c in packs["assistants"]["characters"])
    r = owner.get(owl["picture"])
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert r.content == starter_packs.avatar_files("kids_owl")["thumb"].read_bytes()
    assert owner.get("/api/starter-packs/animal_friends/pictures/sage").status_code == 404
    assert owner.get("/api/starter-packs/assistants/pictures/kids_owl").status_code == 404
    assert owner.get("/api/starter-packs/animal_friends/pictures/..%2F..%2Fconfig").status_code == 404
    assert owner.get("/api/starter-packs/ghost/pictures/kids_owl").status_code == 404
    stranger = world.client(("203.0.113.66", 41006))
    assert stranger.get(owl["picture"]).status_code in (401, 403)
    max_ = world.person(owner, "Max", "member", pin="5555")
    member = world.client(("203.0.113.67", 41007))
    world.login(member, profile_id=max_["id"], pin="5555")
    assert member.get(owl["picture"]).status_code in (401, 403)


def test_install_with_an_empty_only_list_is_refused(world, owner, conn):
    r = owner.post("/api/starter-packs/animal_friends/install",
                   json={"connection": conn, "model": "qwen", "only": []})
    assert r.status_code == 422, r.text
    assert "kids_frog" not in _roster()


def test_install_only_two_animals_with_pictures_and_kid_settings(world, owner, conn):
    r = owner.post("/api/starter-packs/animal_friends/install",
                   json={"connection": conn, "model": "qwen", "only": ["kids_owl", "kids_bee"]})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["installed"] == ["kids_bee", "kids_owl"]            # pack order, not request order
    assert out["pictures"] == ["kids_bee", "kids_owl"] and out["skipped"] == []
    roster = _roster()
    assert "kids_frog" not in roster
    for cid in ("kids_bee", "kids_owl"):
        e = roster[cid]
        assert e["pack"] == "animal_friends" and e["quick_actions"] is False
        adv = e["advisor"]
        assert adv["tools"] is False and adv["owner"] == "the child" and adv["history_chars"] == 8000
        assert adv["providers"][0]["connection"] == conn and adv["providers"][0]["max_tokens"] == 700
        assert adv["persona"].startswith("You are ")
        assert e["voice"].startswith("builtin:")
        # the pictures sit where the avatar upload route writes them, under the same names
        assert e["avatar"] == f"{cid}-face.png"
        for part in ("face", "full", "thumb"):
            dest = Path(config.AVATAR_DIR) / f"{cid}-{part}.png"
            assert dest.read_bytes() == starter_packs.avatar_files(cid)[part].read_bytes()
        assert config.get_bot(cid).is_template
    # the installer's own copy wears the picture too, through the avatar route
    page = owner.get("/api/characters/manage").json()
    mine = {r["template"]["id"]: r for r in page["mine"] if r.get("template")}
    assert {"kids_bee", "kids_owl"} <= set(mine)
    copy_id = mine["kids_owl"]["id"]
    av = owner.get(f"/api/bots/{copy_id}/avatar")
    assert av.status_code == 200 and av.headers["content-type"] == "image/png"
    # a pack listing now shows them added
    pk = next(p for p in owner.get("/api/starter-packs").json()["packs"] if p["id"] == "animal_friends")
    assert {c["id"] for c in pk["characters"] if c["installed"]} == {"kids_bee", "kids_owl"}
    assert pk["partly_installed"] and not pk["installed"]


def test_installing_skips_ids_that_already_exist(world, owner):
    # a roster that already has a hand-made kids_lobster (the live install's case)
    config.upsert_bot(config.Bot(id="kids_lobster", name="My Lobster", emoji="L", avatar="mine.png"))
    r = owner.post("/api/starter-packs/animal_friends/install", json={})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["skipped"] == [{"id": "kids_lobster", "reason": "a character with this id already exists"}]
    assert out["installed"] == [c for c in ANIMALS if c != "kids_lobster"]
    lob = config.get_bot("kids_lobster")
    assert lob.name == "My Lobster" and lob.avatar == "mine.png"     # never overwritten
    assert not (Path(config.AVATAR_DIR) / "kids_lobster-face.png").exists()
    # without a connection the reply cap has nowhere to go; the rest still applies
    adv = _roster()["kids_frog"]["advisor"]
    assert adv["providers"] == [] and adv["owner"] == "the child"
    # a second install adds nothing
    again = owner.post("/api/starter-packs/animal_friends/install", json={}).json()
    assert again["installed"] == [] and len(again["skipped"]) == 8
