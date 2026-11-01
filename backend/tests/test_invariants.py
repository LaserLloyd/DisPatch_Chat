"""The 4.2 data invariants I1-I8 (app/invariants.py, PLAN-v2 section 7).

Every invariant fires on a seeded violation and the clean world stays quiet; the start-up run
logs and audits but never raises; ``GET /api/health/invariants`` answers an Admin and a machine
holding ``machine:health`` and nobody else.

Run: cd backend && uv run pytest tests/test_invariants.py
"""
from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app import accounts, auth, config, invariants, ownership
from app.contracts.permissions import MachineScope, Role
from app.invariants import World, check

ADM, UMA, BAS = "adm", "uma", "bas"
DIRECT = {"providers": [{"provider": "openai_compatible", "model": "m"}]}


def prof(pid, role, *, protected=False, hb="all", disabled=False):
    return accounts.Profile(id=pid, name=pid.title(), role=role, protected=protected,
                            household_bots=hb, disabled=disabled)


def tpl(bid, give_to="everyone"):
    return config.Bot(id=bid, name=bid, advisor=dict(DIRECT), give_to=give_to)


def copy_of(t, cid, owner):
    return config.Bot(id=cid, name=t.id, advisor=dict(DIRECT), scope="personal", owner_profile=owner,
                      template=t.id)


def clean() -> World:
    """Owner + User; one template given to everyone with a copy each; an Owner agent bot, an Owner
    own bot; chats only on bots their owner owns; every kit chat on record."""
    t = tpl("atlas")
    bots = [t, copy_of(t, "c-adm", ADM), copy_of(t, "c-uma", UMA),
            config.Bot(id="main", name="Bits", scope="personal", owner_profile=ADM),
            config.Bot(id="mine", name="Mine", advisor=dict(DIRECT), scope="personal", owner_profile=UMA)]
    w = World(bots=bots, profiles=[prof(ADM, Role.ADMIN, protected=True), prof(UMA, Role.USER),
                                   prof(BAS, Role.BASIC, hb=[])],
              admin=ADM)
    w.profiles[2] = prof(BAS, Role.BASIC, hb=[])
    w.bots.append(copy_of(t, "c-bas", BAS))
    w.threads.update({("c-adm", ADM): 4, ("main", None): 7, ("main", ADM): 2, ("c-uma", UMA): 3,
                      ("mine", UMA): 1, ("c-bas", BAS): 1, ("gone-bot", ADM): 5})   # a retired id
    w.kinds = {"atlas": "direct", "c-adm": "direct", "c-uma": "direct", "c-bas": "direct",
               "mine": "direct", "main": "agent"}
    w.expected = {"c-adm": {"a", "b"}, "c-uma": {"a", "b"}, "c-bas": set()}
    w.delivered = {"c-adm": {"a", "b"}, "c-uma": {"a", "b", "extra"}}
    return w


def codes(w: World) -> set[str]:
    return {v.code for v in check(w)}


def swap(w: World, bot_id: str, **kw) -> World:
    w.bots = [replace(b, **kw) if b.id == bot_id else b for b in w.bots]
    return w


def test_a_clean_world_is_quiet():
    assert check(clean()) == []


def test_i1_a_template_with_threads():
    w = clean()
    w.threads[("atlas", ADM)] = 2
    vs = check(w)
    assert [v.code for v in vs] == ["I1"] and vs[0].subject == "atlas"


def test_i2_a_thread_on_somebody_elses_bot():
    w = clean()
    w.threads[("c-adm", UMA)] = 1                                  # Uma's thread on the Admin's copy
    assert codes(w) == {"I2"}
    w = clean()
    w.threads[("main", UMA)] = 1                                   # Uma talking to an Owner agent bot
    assert codes(w) == {"I2"}
    w = clean()
    w.threads[("mine", None)] = 1                                  # a NULL owner is the Admin, not Uma
    assert codes(w) == {"I2"}


def test_i2_household_bot_with_threads_only_tolerated_without_an_admin():
    w = clean()
    w.bots.append(config.Bot(id="legacy", name="L", advisor=dict(DIRECT)))
    w.kinds["legacy"] = "direct"
    w.threads[("legacy", UMA)] = 1
    assert codes(w) == {"I2"}
    w.admin = None
    w.profiles = [p for p in w.profiles if not p.protected]
    swap(w, "main", scope="household", owner_profile="")
    w.threads = Counter({("legacy", None): 1})
    assert "I2" not in codes(w)


def test_i3_an_agent_bot_that_is_not_the_admins_or_is_a_template():
    w = clean()
    swap(w, "main", owner_profile=UMA)
    assert "I3" in codes(w)
    w = clean()
    swap(w, "main", scope="household", owner_profile="")             # still household after a claim
    assert "I3" in codes(w)
    w = clean()
    swap(w, "main", give_to="everyone")
    assert "I3" in codes(w)
    w = clean()                                                        # an unclaimed install: household is right
    w.admin = None
    swap(w, "main", scope="household", owner_profile="")
    w.profiles = [p for p in w.profiles if not p.protected]
    assert "I3" not in codes(w)


def test_i4_a_copy_whose_template_is_gone_or_a_copy():
    w = clean()
    w.bots = [b for b in w.bots if b.id != "atlas"]
    assert codes(w) == {"I4"}
    w = clean()
    swap(w, "c-uma", template="c-adm")
    assert "I4" in codes(w)
    w = clean()
    swap(w, "atlas", scope="personal", owner_profile=ADM)
    assert "I4" in codes(w)


def test_i5_two_copies_in_one_profile_and_a_missing_copy():
    w = clean()
    w.bots.append(copy_of(w.bots[0], "c-uma2", UMA))
    w.kinds["c-uma2"] = "direct"
    assert codes(w) == {"I5"}
    w = clean()
    w.bots = [b for b in w.bots if b.id != "c-uma"]
    w.threads.pop(("c-uma", UMA))
    assert codes(w) == {"I5"}
    w.removed = {UMA: {"atlas"}}                                      # a recorded deletion is fine
    assert codes(w) == set()
    w = clean()
    w.profiles.append(prof("off", Role.USER, disabled=True))          # a disabled profile is not owed one
    assert codes(w) == set()


def test_i6_give_to_names_a_profile_that_is_gone():
    w = clean()
    swap(w, "atlas", give_to=[ADM, UMA, BAS, "ghost"])
    vs = check(w)
    assert [v.code for v in vs] == ["I6"] and vs[0].subject == "atlas@ghost"


def test_i7_a_copy_without_its_kit_chats_and_the_pending_skip(monkeypatch):
    monkeypatch.setenv("DISPATCH_FIRSTRUN", "1")
    monkeypatch.setenv("DISPATCH_ATLAS_GUIDES", "1")
    w = clean()
    w.delivered["c-uma"] = {"a"}
    vs = check(w)
    assert [v.code for v in vs] == ["I7"] and "1 kit chat" in vs[0].detail
    w.sync_pending = True
    assert check(w) == []


def test_i8_a_personal_id_in_household_bots():
    w = clean()
    w.profiles[1] = prof(UMA, Role.USER, hb=["atlas", "mine"])
    vs = check(w)
    assert [v.code for v in vs] == ["I8"] and vs[0].subject == f"mine@{UMA}"


def test_every_invariant_has_a_test_and_a_statement():
    assert set(invariants.INVARIANTS) == {f"I{i}" for i in range(1, 13)}


def test_the_report_carries_ids_and_counts_only():
    w = clean()
    w.threads[("atlas", ADM)] = 2
    for v in check(w):
        assert "Adm" not in v.detail and "Uma" not in v.detail


# --------------------------------------------------------------------------- #
# The live data: the migrated fixture is clean; start-up never raises; the route
# --------------------------------------------------------------------------- #


def test_the_migrated_live_shape_fixture_is_clean(tmp_path, monkeypatch):
    import sqlite3

    from app import kits, migrate
    from tests.migrate_templates_helpers import build
    monkeypatch.setenv("DISPATCH_FIRSTRUN", "1")
    monkeypatch.setenv("DISPATCH_ATLAS_GUIDES", "1")
    d = build(tmp_path, monkeypatch)
    # before 012 the rules do not hold yet: household agent bots and household bots with chats
    assert {"I2", "I3"} <= {v.code for v in invariants.check(sync_pending=True)}
    migrate.run()
    got = invariants.check(sync_pending=True)
    assert got == [], [v.to_dict() for v in got]
    # with the kit chats still unposted, I7 says so; posting them (recording them) clears it
    assert {v.code for v in invariants.check()} == {"I7"}
    for b in config.load_bots():
        if b.template:
            kit = kits.get_kit(b.template)
            prof_ = accounts.get_profile(b.owner_profile)
            for c in kits.chats_for(kit, prof_.role) if kit else []:
                kits._record(b.id, c.id, c.rev, f"kit:{b.id}:{c.id}:{c.rev}")
    assert invariants.check() == []
    # a hand edit shows at once: a thread back on a template
    con = sqlite3.connect(d / "chats.db")
    con.execute("UPDATE threads SET bot_id = 'atlas' WHERE id = (SELECT id FROM threads LIMIT 1)")
    con.commit()
    con.close()
    assert "I1" in {v.code for v in invariants.check()}


def test_run_at_start_logs_audits_and_never_raises(monkeypatch, caplog, tmp_path):
    w = clean()
    w.threads[("atlas", ADM)] = 2
    calls = []
    monkeypatch.setattr(invariants, "snapshot", lambda **_: w)
    import app.accounts as acc
    monkeypatch.setattr(acc, "log_activity", lambda *a, **k: calls.append((a, k)))
    with caplog.at_level(logging.WARNING, logger="local-chat.invariants"):
        vs = invariants.run_at_start()
    assert [v.code for v in vs] == ["I1"]
    assert any("invariant I1 violated" in r.message for r in caplog.records)
    assert calls and calls[0][1]["category"] == "audit" and calls[0][1]["subject"] == "I1"

    def boom(**_):
        raise RuntimeError("db gone")
    monkeypatch.setattr(invariants, "snapshot", boom)
    assert invariants.run_at_start() == []                              # logged, not raised
    monkeypatch.setattr(invariants, "snapshot", lambda **_: w)
    monkeypatch.setattr(acc, "log_activity", lambda *a, **k: 1 / 0)
    assert [v.code for v in invariants.run_at_start()] == ["I1"]        # an audit failure is swallowed too


TOKEN = "k" * 40


@pytest.fixture
def hh(world):
    from tests.profiles_fixtures import add_atlas_template
    add_atlas_template()
    owner = world.create_owner(world.client())
    p = world.person(owner, "Uma", "user", pin="2222")
    from agents_world import sibling
    uma = sibling(world, owner, ("203.0.113.170", 49100))
    assert world.login(uma, profile_id=p["id"], pin="2222").status_code == 200
    cfg = auth.load()
    cfg.api_token = TOKEN
    cfg.machine_loopback_trust = False
    auth._write(cfg)
    auth._bust_cache()
    ownership.forget_all()
    machine = world.client(world.LOOPBACK, origin=False)
    machine.headers.update({"X-API-Key": TOKEN})
    return SimpleNamespace(owner=owner, uma=uma, machine=machine, world=world)


def test_the_route_answers_an_admin_and_a_machine_with_the_scope_only(hh):
    r = hh.owner.get("/api/health/invariants")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) >= {"ok", "count", "by_invariant", "invariants", "violations", "sync_pending"}
    assert set(body["by_invariant"]) == {f"I{i}" for i in range(1, 13)}
    assert hh.uma.get("/api/health/invariants").status_code == 403
    assert hh.machine.get("/api/health/invariants").status_code == 200       # default scopes: all of them
    cfg = auth.load()
    cfg.machine_scopes = [s.value for s in MachineScope if s is not MachineScope.HEALTH]
    auth._write(cfg)
    auth._bust_cache()
    assert hh.machine.get("/api/health/invariants").status_code == 403       # narrowed: needs machine:health
    cfg.machine_scopes = [MachineScope.HEALTH.value]
    auth._write(cfg)
    auth._bust_cache()
    assert hh.machine.get("/api/health/invariants").status_code == 200
    assert hh.world.client(("203.0.113.9", 40000)).get("/api/health/invariants").status_code in (401, 403)


def test_the_route_reports_a_seeded_violation_without_names(hh):
    config.upsert_bot(config.Bot(id="stray", name="Secret Name", advisor=dict(DIRECT), give_to=[]))
    import asyncio

    from app import main
    asyncio.run(main.db.create_thread("stray", "private title", thread_id="t1",
                                      owner_profile_id=accounts.get_protected_admin().id))
    body = hh.owner.get("/api/health/invariants").json()
    assert body["ok"] is False and body["by_invariant"]["I1"] == 1
    text = json.dumps(body)
    assert "Secret Name" not in text and "private title" not in text


def test_i12_the_default_profile_has_no_credential_and_is_no_admin():
    """4.3 Fable: I12 (more cases, and the live route, in test_fable_default_profile_credential.py)."""
    from types import SimpleNamespace as NS

    def run(**over):
        p = NS(id="d", role=Role.USER, has_pin=False, has_password=False, disabled=False,
               protected=False, household_bots="all")
        p.__dict__.update(over)
        w = invariants.World(profiles=[p], extra={"lock_target": {"target": "default_profile",
                                                                  "default_profile": "d"}})
        return [v.code for v in invariants.check(w) if v.code == "I12"]
    assert run() == []
    assert run(has_pin=True) == ["I12"]
    assert run(role=Role.ADMIN) == ["I12"]
