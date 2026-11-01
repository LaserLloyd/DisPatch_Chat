"""4.2 live templates in the roster (PLAN-v2 §2, slice S1).

A linked copy (`Bot.template`) is stored as identity + `overrides` only and resolved from its
template at load, so it carries EVERY inheritable field of the template and follows it. The
fidelity tests run against the redacted live-4.1-shape fixture (tests/fixtures/live-4.1-shape).

Hermetic: config paths monkeypatched to a tmp dir. Run: cd backend && uv run pytest.
"""
from __future__ import annotations

import copy
import logging
import shutil
import threading
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from app import config

FIXTURE = Path(__file__).parent / "fixtures" / "live-4.1-shape"
OWNER = "c30db2d188b5c09aa9a6768ca6ab6cd6"
KID = "8f78572fc3dff747bd6eaf8f0b384867"


@pytest.fixture
def cfg_env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(config, "MEDIA_DIR", tmp_path / "media")
    monkeypatch.setattr(config, "FILES_DIR", tmp_path / "files")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path / "backups")
    config._invalidate_bots_cache()
    yield tmp_path
    config._invalidate_bots_cache()


@pytest.fixture
def live(cfg_env):
    """The roster of the live-shape fixture, as config.yaml."""
    shutil.copy(FIXTURE / "roster.yaml", config.CONFIG_PATH)
    config._invalidate_bots_cache()
    return cfg_env


def _templates():
    raw = yaml.safe_load((FIXTURE / "roster.yaml").read_text())["bots"]
    return [e["id"] for e in raw
            if e.get("advisor") and not e.get("api") and e.get("scope", "household") == "household"]


def _copy(template_id: str, cid: str = "cp1", owner: str = OWNER, **kw) -> config.Bot:
    return config.new_copy(config.get_bot(template_id), id=cid, owner_profile=owner,
                           avatar=f"{cid}-face.png", order=50, **kw)


def _file_entry(bot_id: str) -> dict:
    raw = yaml.safe_load(config.CONFIG_PATH.read_text())["bots"]
    return next(e for e in raw if e["id"] == bot_id)


def _inherited(b: config.Bot) -> dict:
    return {f: getattr(b, f) for f in config.INHERITED_FIELDS}


def test_the_fixture_has_the_expected_templates():
    assert {"atlas", "wikka", "advisor", "kid_owl"} <= set(_templates())


@pytest.mark.parametrize("tid", _templates())
def test_a_resolved_copy_carries_every_field_of_its_template(live, tid):
    tpl = config.get_bot(tid)
    config.upsert_bot(_copy(tid))
    cp = config.get_bot("cp1")

    assert cp.template == tid and cp.scope == "personal" and cp.owner_profile == OWNER
    assert _inherited(cp) == _inherited(tpl)          # name, emoji, voice, image_jobs, advisor ...
    assert cp.advisor == tpl.advisor                   # every provider, thinking, caps, lookup, tools
    # nothing inherited is written: the entry is identity + (empty) overrides
    entry = _file_entry("cp1")
    assert not entry.get("overrides")
    for k in ("advisor", "name", "emoji", "voice", "voice_rp", "image_jobs", "persona"):
        assert k not in entry
    # per-copy fields are the copy's own
    assert cp.avatar == "cp1-face.png" and cp.id == "cp1" and cp.order != tpl.order
    # never inherited: pools and reactions are household-only
    assert (cp.reactions, cp.reaction_autopilot, cp.avatar_pool) == (False, False, False)
    # mutating one resolved copy never reaches the template
    cp.advisor["providers"][0]["model"] = "scribble"
    assert config.get_bot(tid).advisor["providers"][0]["model"] != "scribble"


def test_named_fields_the_4_1_copy_path_used_to_drop(live):
    for tid in ("wikka", "atlas", "kid_owl"):
        config.upsert_bot(_copy(tid, f"cp-{tid}"))
    wikka = config.get_bot("cp-wikka").advisor
    assert wikka["lookup"]["kind"] == "infoforge" and wikka["lookup"]["max_articles"] == 3
    atlas = config.get_bot("cp-atlas")
    assert atlas.image_jobs and atlas.image_workflow == "z-image-turbo"
    assert len(atlas.advisor["providers"]) == 2          # the fallback chain
    assert atlas.advisor["location"] and atlas.advisor["tools"] is True
    kid = config.get_bot("cp-kid_owl")
    assert kid.advisor["history_chars"] == 8000
    assert kid.advisor["providers"][0]["max_tokens"] == 700
    assert kid.advisor["providers"][0]["extra_body"]["thinking"] == {"type": "disabled"}
    assert len(kid.advisor["providers"]) == 2
    assert kid.voice.startswith("builtin:")


def test_a_template_edit_reaches_every_copy_without_that_override(live):
    config.upsert_bot(_copy("wikka", "a"))
    config.upsert_bot(_copy("wikka", "b", owner=KID))
    config.update_bot("b", lambda b: replace(b, name="Mine"))

    def edit(t):
        adv = copy.deepcopy(t.advisor)
        adv["lookup"]["max_articles"] = 5
        return replace(t, name="Wikka 2", advisor=adv, voice="builtin:af_sky", emoji="W")
    config.update_bot("wikka", edit)

    a, b = config.get_bot("a"), config.get_bot("b")
    assert (a.name, a.voice, a.emoji, a.advisor["lookup"]["max_articles"]) == \
        ("Wikka 2", "builtin:af_sky", "W", 5)
    assert b.name == "Mine"                              # the copy's own change survives
    assert (b.voice, b.advisor["lookup"]["max_articles"]) == ("builtin:af_sky", 5)
    # and neither copy pinned the template's old values by being rewritten
    assert not _file_entry("a").get("overrides")
    assert _file_entry("b")["overrides"] == {"name": "Mine"}


def test_overrides_are_exactly_the_fields_that_differ(live):
    config.upsert_bot(_copy("atlas"))

    def mutate(b):
        adv = copy.deepcopy(b.advisor)
        adv["persona"] = "You are my Atlas."
        adv["tools"] = False
        adv["tools_picked"] = ["weather"]
        adv["providers"][0] = {"provider": "custom", "model": "other", "connection": "mine"}
        return replace(b, name="Globe", voice="builtin:af_sky", image_jobs=False, advisor=adv)
    config.update_bot("cp1", mutate)

    ov = _file_entry("cp1")["overrides"]
    assert set(ov) == {"name", "voice", "image_jobs", "advisor"}
    assert ov["advisor"] == {"persona": "You are my Atlas.", "tools": False,
                             "tools_picked": ["weather"],
                             "provider0": {"provider": "custom", "model": "other",
                                           "connection": "mine"}}
    cp, tpl = config.get_bot("cp1"), config.get_bot("atlas")
    assert cp.advisor["persona"] == "You are my Atlas."
    assert cp.advisor["providers"][0]["model"] == "other"
    assert cp.advisor["providers"][1:] == tpl.advisor["providers"][1:]   # the fallback follows
    assert cp.advisor["lookup" if "lookup" in tpl.advisor else "agents"] == \
        tpl.advisor["lookup" if "lookup" in tpl.advisor else "agents"]
    assert (cp.name, cp.voice, cp.image_jobs) == ("Globe", "builtin:af_sky", False)
    assert cp.emoji == tpl.emoji and cp.image_workflow == tpl.image_workflow


def test_setting_a_field_back_to_the_template_value_makes_it_follow_again(live):
    config.upsert_bot(_copy("atlas"))
    config.update_bot("cp1", lambda b: replace(b, name="Globe"))
    assert _file_entry("cp1")["overrides"] == {"name": "Globe"}
    tpl_name = config.get_bot("atlas").name
    config.update_bot("cp1", lambda b: replace(b, name=tpl_name))
    assert not _file_entry("cp1").get("overrides")

    def persona_round(b):
        adv = dict(b.advisor)
        adv["persona"] = "x"
        return replace(b, advisor=adv)
    config.update_bot("cp1", persona_round)
    assert _file_entry("cp1")["overrides"]["advisor"] == {"persona": "x"}

    def persona_gone(b):
        adv = dict(b.advisor)
        adv.pop("persona")
        return replace(b, advisor=adv)
    config.update_bot("cp1", persona_gone)
    assert not _file_entry("cp1").get("overrides")


def test_removing_a_persona_the_template_has_is_an_override(live):
    config.upsert_bot(_copy("wikka"))

    def drop(b):
        adv = dict(b.advisor)
        adv.pop("persona")
        return replace(b, advisor=adv)
    config.update_bot("cp1", drop)
    assert _file_entry("cp1")["overrides"]["advisor"] == {"persona": None}
    assert "persona" not in config.get_bot("cp1").advisor
    assert "persona" in config.get_bot("wikka").advisor


def test_other_writers_never_pin_template_values_into_a_copy(live):
    config.upsert_bot(_copy("atlas"))
    config.update_bot("cp1", lambda b: replace(b, name="Globe"))
    tpl = config.get_bot("atlas")

    config.save_bot_avatar("cp1", "new-face.png")
    config.save_bot_order([{"id": "cp1", "order": 3}, {"id": "atlas", "order": 0}])
    config.save_bot_avatar("atlas", "atlas-new.png")
    config.upsert_bot(replace(config.get_bot("atlas"), emoji="E"))   # template changes elsewhere

    assert _file_entry("cp1")["overrides"] == {"name": "Globe"}
    cp = config.get_bot("cp1")
    assert cp.avatar == "new-face.png" and cp.order == 1 and cp.emoji == "E"
    assert tpl.name == config.get_bot("atlas").name


def test_save_bot_order_flags_on_a_copy_are_overrides_or_forced_off(live):
    config.upsert_bot(_copy("atlas"))
    config.save_bot_order([{"id": "cp1", "order": 1, "voice": "builtin:am_puck",
                            "reactions": True, "avatar_pool": True}])
    assert _file_entry("cp1")["overrides"] == {"voice": "builtin:am_puck"}
    cp = config.get_bot("cp1")
    assert (cp.reactions, cp.avatar_pool) == (False, False)


def test_a_template_and_its_copy_edited_at_once_both_land(live):
    config.upsert_bot(_copy("atlas"))
    errs = []

    def run(fn):
        try:
            for i in range(15):
                fn(i)
        except Exception as e:                     # pragma: no cover
            errs.append(e)
    t1 = threading.Thread(target=run, args=(
        lambda i: config.update_bot("atlas", lambda b: replace(b, description=f"d{i}")),))
    t2 = threading.Thread(target=run, args=(
        lambda i: config.update_bot("cp1", lambda b: replace(b, name=f"n{i}")),))
    t1.start(); t2.start(); t1.join(); t2.join()
    assert not errs
    assert config.get_bot("atlas").description == "d14"
    assert config.get_bot("cp1").name == "n14" and config.get_bot("cp1").description == "d14"
    assert _file_entry("cp1")["overrides"] == {"name": "n14"}


def test_reset_overrides_follows_the_template_again(live):
    config.upsert_bot(_copy("atlas"))
    config.update_bot("cp1", lambda b: replace(b, name="Globe", voice="builtin:af_sky"))
    out = config.reset_overrides("cp1")
    assert out.name == config.get_bot("atlas").name and out.voice == ""
    assert not _file_entry("cp1").get("overrides")
    assert config.reset_overrides("nope") is None


def test_meta_override_keys_survive_writes_and_are_dropped_with_their_field(live):
    config.upsert_bot(_copy("atlas"))

    def pick(b):
        adv = copy.deepcopy(b.advisor)
        adv["providers"][0] = {"provider": "custom", "model": "m", "connection": "c"}
        return replace(b, advisor=adv, overrides={**(b.overrides or {}), "provider0_picked": True,
                                                  "picture": True})
    config.update_bot("cp1", pick)
    ov = _file_entry("cp1")["overrides"]
    assert ov["provider0_picked"] is True and ov["picture"] is True
    assert config.get_bot("cp1").overrides["provider0_picked"] is True
    config.update_bot("cp1", lambda b: replace(b, name="N"))        # an unrelated write keeps them
    assert _file_entry("cp1")["overrides"]["provider0_picked"] is True
    # provider0 follows the template again -> the "picked" mark has nothing to mark
    tpl0 = config.get_bot("atlas").advisor["providers"][0]

    def unpick(b):
        adv = copy.deepcopy(b.advisor)
        adv["providers"][0] = copy.deepcopy(tpl0)
        return replace(b, advisor=adv)
    config.update_bot("cp1", unpick)
    assert "provider0_picked" not in _file_entry("cp1")["overrides"]


def test_detach_makes_a_plain_own_bot_with_the_effective_values(live):
    config.upsert_bot(_copy("wikka"))
    config.update_bot("cp1", lambda b: replace(b, name="Mine"))
    before = config.get_bot("cp1")
    out = config.detach("cp1")
    assert out.template == "" and out.overrides is None
    entry = _file_entry("cp1")
    assert "template" not in entry and "overrides" not in entry
    assert entry["name"] == "Mine" and entry["advisor"] == before.advisor
    assert entry["scope"] == "personal" and entry["owner_profile"] == OWNER
    after = config.get_bot("cp1")
    assert _inherited(after) == _inherited(before)
    config.update_bot("wikka", lambda b: replace(b, name="Changed", emoji="Q"))
    assert config.get_bot("cp1").emoji == before.emoji          # no longer follows
    assert config.detach("nope") is None
    assert config.detach("cp1").template == ""                  # idempotent on an own bot


def test_a_copy_whose_template_is_gone_loads_from_overrides_and_defaults(live, caplog):
    config.upsert_bot(_copy("wikka"))
    config.update_bot("cp1", lambda b: replace(b, name="Mine"))
    config.remove_bot("wikka")
    with caplog.at_level(logging.WARNING):
        cp = config.get_bot("cp1")
    assert cp is not None and cp.name == "Mine" and cp.template == "wikka"
    assert cp.advisor is None and cp.voice == ""
    assert any("wikka" in r.getMessage() for r in caplog.records)
    # an edit of such a copy keeps what it shows (nothing is lost to the diff)
    config.update_bot("cp1", lambda b: replace(b, emoji="Z"))
    assert config.get_bot("cp1").name == "Mine" and config.get_bot("cp1").emoji == "Z"


def test_a_template_that_is_itself_a_copy_counts_as_missing(live, caplog):
    config.upsert_bot(_copy("wikka", "c1"))
    config.upsert_bot(_copy("c1", "c2"))
    with caplog.at_level(logging.WARNING):
        c2 = config.get_bot("c2")
    assert c2.name == "Wikka"                    # shown from its own (full) overrides
    assert any("c1" in r.getMessage() for r in caplog.records)


def test_unknown_and_forced_off_override_keys_are_ignored(live):
    config.upsert_bot(_copy("atlas"))
    raw = yaml.safe_load(config.CONFIG_PATH.read_text())
    for e in raw["bots"]:
        if e["id"] == "cp1":
            e["overrides"] = {"reactions": True, "avatar_pool": True, "scope": "household",
                              "owner_profile": "x", "mystery": 1, "name": "Ok"}
    config.CONFIG_PATH.write_text(yaml.safe_dump(raw))
    config._invalidate_bots_cache()
    cp = config.get_bot("cp1")
    assert cp.name == "Ok" and cp.scope == "personal" and cp.owner_profile == OWNER
    assert (cp.reactions, cp.avatar_pool) == (False, False)


def test_a_template_secret_is_never_written_into_a_copy(cfg_env):
    tpl = config.Bot(id="t", name="T", advisor={
        "providers": [{"provider": "custom", "model": "m", "api_key": "SECRET-123"}]},
        give_to="everyone")
    config.upsert_bot(tpl)
    config.upsert_bot(_copy("t"))
    assert "SECRET-123" in str(config.get_bot("cp1").advisor)          # effective, in memory
    assert "SECRET-123" not in config.CONFIG_PATH.read_text().split("id: cp1")[1]


def test_give_to_round_trips_and_garbage_is_dropped(cfg_env):
    config.upsert_bot(config.Bot(id="t1", name="T1", give_to="everyone"))
    config.upsert_bot(config.Bot(id="t2", name="T2", give_to=["p1", "p2"]))
    config.upsert_bot(config.Bot(id="t3", name="T3", give_to=[]))
    config.upsert_bot(config.Bot(id="t4", name="T4"))
    assert config.get_bot("t1").give_to == "everyone"
    assert config.get_bot("t2").give_to == ["p1", "p2"]
    assert config.get_bot("t3").give_to == []                          # nobody, still a template
    assert config.get_bot("t4").give_to is None
    assert "give_to" not in _file_entry("t4")
    raw = yaml.safe_load(config.CONFIG_PATH.read_text())
    for e in raw["bots"]:
        if e["id"] == "t1":
            e["give_to"] = "anyone"
        if e["id"] == "t2":
            e["give_to"] = ["p1", 3, "", "p1"]
    config.CONFIG_PATH.write_text(yaml.safe_dump(raw))
    config._invalidate_bots_cache()
    assert config.get_bot("t1").give_to is None
    assert config.get_bot("t2").give_to == ["p1"]
    assert config.get_bot("t2").is_template and not config.get_bot("t4").is_template


def test_pre_4_2_copies_still_load_exactly_as_before(live):
    raw = {e["id"]: e for e in yaml.safe_load((FIXTURE / "roster.yaml").read_text())["bots"]}
    for cid in ("pe63d316c0739", "pe2aa88a6cb9e", "pe98625e74f37"):
        b = config.get_bot(cid)
        assert b.template == "" and b.overrides is None and not b.is_linked_copy
        assert b.name == raw[cid]["name"] and b.advisor == raw[cid]["advisor"]
        assert b.cloned_from == raw[cid].get("cloned_from", "")
        assert b.kit_version == raw[cid].get("kit_version", "")
    # writing the roster through the new serializer changes nothing about them
    config.save_bot_avatar("main", "m.png")
    for cid in ("pe63d316c0739", "pe98625e74f37"):
        assert config.get_bot(cid).advisor == raw[cid]["advisor"]
        assert config.get_bot(cid).kit_baseline == raw[cid].get("kit_baseline")


def test_the_fixture_roster_survives_a_whole_roster_rewrite_unchanged(live):
    before = {b.id: b for b in config.load_bots()}
    config.save_bot_order([{"id": b.id, "order": b.order} for b in before.values()])
    after = {b.id: b for b in config.load_bots()}
    # (save_bot_order compacts the order numbers; nothing else may change)
    assert after == {k: replace(v, order=after[k].order) for k, v in before.items()}


def test_every_bot_field_has_a_decided_place_in_a_linked_copy():
    """A new Bot field must be classified here (inherited, the copy's own, forced off or
    bookkeeping): otherwise a copy silently drops it, the 4.1 bug class."""
    from dataclasses import fields
    own = {"id", "scope", "owner_profile", "template", "overrides", "avatar", "hidden", "order",
           "cloned_from", "detached_from", "pack", "kit_version", "kit_baseline", "give_to"}
    forced_off = {"reactions", "reaction_autopilot", "avatar_pool"}
    legacy_or_direct_only = {"safe", "hide_unlocked", "profiles", "api", "agent"}
    bookkeeping = {"resolved_view"}
    decided = set(config.INHERITED_FIELDS) | own | forced_off | legacy_or_direct_only | bookkeeping
    assert {f.name for f in fields(config.Bot)} == decided
