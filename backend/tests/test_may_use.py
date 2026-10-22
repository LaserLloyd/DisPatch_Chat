"""characters.may_use / visible_to / may_use_id (4.0, BUILD-PLAN-v2 §1.2), cell by cell.

Run: cd backend && uv run pytest tests/test_may_use.py
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
import yaml

from app import accounts, characters, config, policy
from app.contracts.permissions import Role

PROV = {"providers": [{"provider": "openai", "model": "m", "base_url": "http://127.0.0.1:9/v1"}]}


@pytest.fixture
def house(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.yaml")
    accounts.reset_for_tests()
    owner = accounts.create_profile("Owner", Role.ADMIN, protected=True)
    admin2 = accounts.create_profile("Ann", Role.ADMIN)
    user_all = accounts.create_profile("Uma", Role.USER)
    user_some = accounts.create_profile("Una", Role.USER, household_bots=["direct1"])
    basic = accounts.create_profile("Bo", Role.BASIC, household_bots=["direct1", "hidden1"])
    basic_all = accounts.create_profile("Bea", Role.BASIC)              # stored "all"
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"bots": [
        {"id": "direct1", "name": "D1", "advisor": PROV},
        {"id": "direct2", "name": "D2", "advisor": PROV},
        # `hidden` once config.py reads it (migration 010 renames hide_unlocked -> hidden)
        {"id": "hidden1", "name": "H1", "advisor": PROV, "hide_unlocked": True},
        {"id": "agent1", "name": "A1"},                                  # gateway agent
        {"id": "p00000000mine", "name": "Mine", "advisor": PROV, "scope": "personal",
         "owner_profile": user_all.id},
        {"id": "p0000000owner", "name": "Ours", "advisor": PROV, "scope": "personal",
         "owner_profile": owner.id},
    ]}))
    config._invalidate_bots_cache()
    yield SimpleNamespace(owner=owner, admin2=admin2, user_all=user_all, user_some=user_some,
                          basic=basic, basic_all=basic_all)
    accounts.reset_for_tests()
    config._invalidate_bots_cache()


def P(prof):
    return policy.profile_principal(accounts.get_profile(prof.id))


def ok(bot_id, pr):
    return characters.may_use(config.get_bot(bot_id), pr)


def test_household_rules_per_role(house):
    for pr in (P(house.owner), P(house.admin2)):
        assert ok("direct1", pr) and ok("direct2", pr) and ok("agent1", pr) and ok("hidden1", pr)
    assert ok("direct1", P(house.user_all)) and ok("direct2", P(house.user_all))
    assert ok("direct1", P(house.user_some)) and not ok("direct2", P(house.user_some))
    assert ok("direct1", P(house.basic)) and not ok("direct2", P(house.basic))
    # "all" gives a Basic profile nothing: the Admin picks its characters
    assert not ok("direct1", P(house.basic_all))


def test_agent_characters_need_dispatch_agents_for_everyone_but_admins(house):
    assert not ok("agent1", P(house.user_all))
    accounts.update_profile(house.user_all.id, grants=["dispatch_agents"])
    assert ok("agent1", P(house.user_all))
    assert not ok("agent1", P(house.basic))          # not on its list, whatever the grant


def test_a_personal_character_is_its_owners_only_admins_included(house):
    assert ok("p00000000mine", P(house.user_all))
    for who in (house.owner, house.admin2, house.user_some, house.basic):
        assert not ok("p00000000mine", P(who)), who.name
    assert ok("p0000000owner", P(house.owner))
    assert not ok("p0000000owner", P(house.admin2))


def test_machines_open_installs_and_nobody(house):
    m = policy.machine_principal("api_token")
    assert ok("direct1", m) and ok("agent1", m)
    assert ok("p0000000owner", m)                      # the protected Admin's personal ones
    assert not ok("p00000000mine", m)
    assert ok("p00000000mine", policy.open_principal())
    assert not characters.may_use(config.get_bot("direct1"), None)
    assert not characters.may_use(None, P(house.owner))
    assert not ok("direct1", policy.nobody())
    assert not characters.may_use_id("no-such-bot", P(house.owner))
    assert not characters.may_use_id(None, P(house.owner))
    assert characters.may_use_id("direct1", P(house.user_all))


def test_visible_to_hides_hidden_unless_the_list_names_it(house):
    def ids(pr):
        return {b.id for b in characters.visible_bots(pr)}

    assert "hidden1" not in ids(P(house.owner))
    assert "hidden1" not in ids(P(house.user_all))
    assert "hidden1" in ids(P(house.basic))           # its explicit list names it
    assert ids(P(house.owner)) == {"direct1", "direct2", "agent1", "p0000000owner"}
    assert ids(P(house.user_all)) == {"direct1", "direct2", "p00000000mine"}
    # the per-bot form agrees with the lists; usable_bots keeps hidden ones (the manager)
    b = config.get_bot("direct1")
    assert characters.visible_to(b, P(house.user_some)) is True
    assert [x.id for x in characters.usable_bots(P(house.user_some))] == ["direct1"]
    assert "hidden1" in {x.id for x in characters.usable_bots(P(house.owner))}


def test_the_3x_audience_and_profiles_fields_are_inert(house, tmp_path):
    doc = yaml.safe_load((tmp_path / "config.yaml").read_text())
    doc["bots"].append({"id": "legacy", "name": "L", "advisor": PROV, "safe": True,
                        "profiles": [house.basic.id]})
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(doc))
    config._invalidate_bots_cache()
    assert not ok("legacy", P(house.basic))            # not on its household list
    assert ok("legacy", P(house.user_all))              # a User with "all"
