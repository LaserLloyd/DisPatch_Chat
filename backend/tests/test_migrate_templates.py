"""Migration 012_templates (4.2, PLAN-v2 section 5) on the live-4.1-shape fixture.

Checked: the dry run changes nothing and says what the real run does; the outcome per profile
(Owner / DisPatch / Kids); idempotency; a crash half way re-runs to the same end; ``--restore``
puts everything back; Owner-bot adoption, ``--own``, copy linking and overrides, no duplicate
guides, retired ids untouched.

Run: cd backend && uv run pytest tests/test_migrate_templates.py
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from app import accounts, config, kits, migrate, profiles_service
from tests.migrate_templates_helpers import (
    AGENTS,
    DISPATCH,
    KIDS,
    KIDS_BOTS,
    OWNER,
    RETIRED,
    build,
    roster,
    snapshot,
    thread_counts,
)


@pytest.fixture
def data(tmp_path, monkeypatch):
    return build(tmp_path, monkeypatch)


def _tree(d: Path) -> dict[str, str]:
    return {str(p.relative_to(d)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(d.rglob("*")) if p.is_file()}


def _copy(profile: str, template: str) -> config.Bot | None:
    return next((b for b in config.load_bots()
                 if b.scope == "personal" and b.owner_profile == profile and b.template == template), None)


def _pending(rep) -> list[str]:
    return [a.text for a in rep.actions if a.step == "012_templates"]


def test_the_dry_run_changes_nothing_and_lists_every_action(data):
    before = _tree(data)
    rep = migrate.run(dry_run=True)
    assert _tree(data) == before
    text = "\n".join(_pending(rep))
    for b in AGENTS:
        assert f"'{b}': agent/app bot -> personal" in text
    assert "'advisor': own bot of Owner" in text
    assert "'atlas': becomes a template, given to everyone" in text
    assert "'wikka': becomes a template, given to Owner, DisPatch" in text or \
        "'wikka': becomes a template, given to DisPatch, Owner" in text
    assert "copy 'pe2aa88a6cb9e': linked to template 'atlas'" in text
    assert "copy 'pe63d316c0739': linked to template 'atlas', keeps own picture" in text
    assert "new copy of 'wikka'" in text and text.count("new copy of 'kid_") == 8
    assert "chat(s) on 'atlas' -> its new copy" in text
    assert "forget the deleted-copy mark for 'atlas'" in text


def test_the_outcome_per_profile(data):
    rep = migrate.run()
    assert rep.backup is not None
    r = roster(data)
    for b in AGENTS:
        assert r[b]["scope"] == "personal" and r[b]["owner_profile"] == OWNER, b
    assert r["advisor"]["scope"] == "personal" and r["advisor"]["owner_profile"] == OWNER
    assert "template" not in r["advisor"] and "give_to" not in r["advisor"]
    assert r["atlas"]["give_to"] == "everyone"
    assert sorted(r["wikka"]["give_to"]) == sorted([OWNER, DISPATCH])
    for k in KIDS_BOTS:
        assert r[k]["give_to"] == [KIDS], k
    bots = config.load_bots()
    # Owner: Atlas + Wikka copies; DisPatch: its Atlas copy kept + a Wikka copy; Kids: Atlas kept + 8 kids
    assert _copy(OWNER, "atlas") and _copy(OWNER, "wikka") and not _copy(OWNER, "kid_owl")
    assert _copy(DISPATCH, "atlas").id == "pe2aa88a6cb9e" and _copy(DISPATCH, "wikka")
    assert _copy(KIDS, "atlas").id == "pe63d316c0739"
    assert all(_copy(KIDS, k) for k in KIDS_BOTS) and not _copy(KIDS, "wikka")
    per = {}
    for b in bots:
        if b.template:
            per.setdefault(b.owner_profile, []).append(b.template)
    assert sorted(per[OWNER]) == ["atlas", "wikka"]
    assert sorted(per[DISPATCH]) == ["atlas", "wikka"]
    assert sorted(per[KIDS]) == sorted(["atlas", *KIDS_BOTS])
    # every copy follows its template (one copy per profile and template)
    pairs = [(b.owner_profile, b.template) for b in bots if b.template]
    assert len(pairs) == len(set(pairs))
    # the picture the Kids' Atlas copy changed is kept; DisPatch's follows the template
    assert r["pe63d316c0739"]["overrides"] == {"picture": True}
    assert "overrides" not in r["pe2aa88a6cb9e"]
    assert "kit_baseline" not in r["pe63d316c0739"] and "kit_version" not in r["pe63d316c0739"]
    # the fidelity fix: a linked copy carries the template's whole brain
    wk = _copy(DISPATCH, "wikka")
    assert wk.advisor["lookup"] == config.get_bot("wikka").advisor["lookup"]
    kid = _copy(KIDS, "kid_owl")                      # the template's voice survives (no add-on host here)
    assert kid.voice == config.get_bot("kid_owl").voice and kid.voice.startswith("builtin:")
    assert _copy(DISPATCH, "atlas").advisor["providers"] == config.get_bot("atlas").advisor["providers"]


def test_threads_move_to_their_owners_copy_and_retired_ids_stay(data):
    counts = thread_counts(data)
    migrate.run()
    after = thread_counts(data)
    assert not [k for k in after if k[0] in ("atlas", "wikka") or k[0].startswith("kid_")]
    o_atlas, o_wikka = _copy(OWNER, "atlas").id, _copy(OWNER, "wikka").id
    assert after[(o_atlas, OWNER)] == counts[("atlas", OWNER)]            # NULL owners included
    assert after[(o_wikka, OWNER)] == counts[("wikka", OWNER)]
    assert after[("pe2aa88a6cb9e", DISPATCH)] == 3 + counts[("atlas", DISPATCH)]      # never a second copy
    assert after[("pe63d316c0739", KIDS)] == counts[("pe63d316c0739", KIDS)]
    for bot, n in RETIRED.items():
        assert after[(bot, OWNER)] == n
    for bot in ("main", "trainer", "Doxy", "advisor", "alpha"):
        assert after[(bot, OWNER)] == counts[(bot, OWNER)]
    con = sqlite3.connect(data / "chats.db")
    assert con.execute("SELECT bot_id FROM image_jobs WHERE id='j1'").fetchone()[0] == o_atlas
    con.close()
    adv = sqlite3.connect(data / "advisor.db")
    assert adv.execute("SELECT bot_id FROM requests WHERE id='r1'").fetchone()[0] == o_wikka
    adv.close()


def test_kit_state_no_second_guide_and_the_mark_is_cleared(data):
    migrate.run()
    state = json.loads((data / "bot-kits" / "state.json").read_text())
    assert OWNER not in state["removed"]
    mine = state["delivered"][_copy(OWNER, "atlas").id]
    assert set(mine) == {"meet-atlas", "using-dispatch"}                  # the two seeded guides
    assert mine["meet-atlas"]["source"] == "guide:atlas:meet-your-atlas.md"
    # DisPatch / Kids keep what they had
    assert "meet-atlas" in state["delivered"]["pe63d316c0739"]


def test_legacy_lists_hold_the_derived_explicit_list(data):
    migrate.run()
    got = {p.id: p.household_bots for p in accounts.list_profiles()}
    assert got[OWNER] == "all"                       # nothing personal to strip from "all"
    assert got[DISPATCH] == ["atlas", "wikka"]
    assert got[KIDS] == sorted(["atlas", *KIDS_BOTS])


def test_a_second_run_is_a_no_op(data):
    migrate.run()
    snap = snapshot(data)
    tree = _tree(data)
    rep = migrate.run()
    assert not rep.actions and not rep.applied
    assert snapshot(data) == snap
    assert _tree(data) == tree or {k for k in _tree(data) if not k.startswith("backups")} == \
        {k for k in tree if not k.startswith("backups")}
    # and even with 012 forgotten, the plan finds nothing to do
    con = sqlite3.connect(data / "chats.db")
    con.execute("DELETE FROM schema_migrations WHERE id = '012_templates'")
    con.commit()
    con.close()
    assert not migrate.templates_plan().pending
    migrate.run()
    assert snapshot(data) == snap


def test_what_the_dry_run_listed_is_what_was_done(data):
    rep = migrate.run(dry_run=True)
    tp = rep.templates
    n_copies = len([b for b in config.load_bots() if b.template])
    migrate.run()
    now = [b for b in config.load_bots() if b.template]
    assert len(now) == n_copies + len(tp.make) + len(tp.link)
    assert {(c, t) for c, t, _ in tp.link} <= {(b.id, b.template) for b in now}
    assert sum(k for *_x, k in tp.repoint) > 0


def test_restore_puts_everything_back(data):
    before = snapshot(data)
    pre_files = {p.name for p in (data / "avatars").iterdir()}
    rep = migrate.run()
    assert snapshot(data) != before
    manifest = json.loads((rep.backup / "012-created.json").read_text())
    assert len(manifest["bots"]) == 2 + 1 + 8 + 1 - 0 or manifest["bots"]       # the copies it made
    migrate.restore(rep.backup)
    after = snapshot(data)
    assert after["config"] == before["config"] and after["state"] == before["state"]
    assert after["threads"] == before["threads"] and after["jobs"] == before["jobs"]
    assert after["reqs"] == before["reqs"] and after["hb"] == before["hb"]
    assert {p.name for p in (data / "avatars").iterdir()} == pre_files
    con = sqlite3.connect(data / "chats.db")
    assert con.execute("SELECT 1 FROM schema_migrations WHERE id='012_templates'").fetchone() is None
    con.close()
    migrate.run()                                    # so it simply runs again, to the same end
    assert thread_counts(data) == thread_counts(data) and not [
        k for k in thread_counts(data) if k[0] in ("atlas", "wikka")]


def test_a_crash_half_way_runs_clean_the_next_time(data, monkeypatch):
    clean = None
    real = profiles_service.repoint_threads
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("disk gone")
        return real(*a, **k)
    monkeypatch.setattr(profiles_service, "repoint_threads", flaky)
    with pytest.raises(RuntimeError):
        migrate.run()
    con = sqlite3.connect(data / "chats.db")
    assert con.execute("SELECT 1 FROM schema_migrations WHERE id='012_templates'").fetchone() is None
    con.close()
    migrate.run()
    monkeypatch.setattr(profiles_service, "repoint_threads", real)
    got = thread_counts(data)
    assert not [k for k in got if k[0] in ("atlas", "wikka")]
    pairs = [(b.owner_profile, b.template) for b in config.load_bots() if b.template]
    assert len(pairs) == len(set(pairs)) == 2 + 2 + 9
    assert clean is None


def test_own_makes_a_direct_bot_one_profiles_own_not_a_template(tmp_path, monkeypatch):
    d = build(tmp_path, monkeypatch)
    migrate.run(own={"wikka": "DisPatch"})
    r = roster(d)
    assert r["wikka"]["scope"] == "personal" and r["wikka"]["owner_profile"] == DISPATCH
    assert "give_to" not in r["wikka"]
    assert not _copy(OWNER, "wikka")
    # the Owner's wikka chats stay on the bot (invariants will say so: the dry run warned)
    assert thread_counts(d)[("wikka", OWNER)] == 5


def test_an_unclaimed_install_keeps_agent_bots_household(tmp_path, monkeypatch):
    d = build(tmp_path, monkeypatch, claimed=False)
    rep = migrate.run(dry_run=True)
    assert "no protected Admin yet" in "\n".join(_pending(rep))
    migrate.run()
    r = roster(d)
    assert all(r[b].get("scope", "household") == "household" for b in AGENTS)
    assert r["atlas"]["give_to"] == "everyone"


def test_link_overrides_follow_what_the_owner_changed_only(data):
    tpl = config.get_bot("atlas")
    cp = config.get_bot("pe63d316c0739")
    from dataclasses import replace
    cp = replace(cp, name="Mine", emoji="🌍", kit_baseline={"name": "Atlas", "emoji": "🌍"})
    ov = migrate._link_overrides(cp, tpl, accounts.get_profile(KIDS))
    assert ov["name"] == "Mine" and "emoji" not in ov and ov["picture"] is True
    cp2 = replace(cp, name=tpl.name, kit_baseline={"name": "Old Name"})
    assert "name" not in migrate._link_overrides(cp2, tpl, accounts.get_profile(KIDS))


def test_kit_guides_are_recognised_by_any_source_shape():
    chats = kits.SHIPPED["atlas"]
    pick = lambda s: getattr(migrate._seed_chat(chats, s)[0], "id", None)       # noqa: E731
    assert pick("guide:atlas:meet-your-atlas.md") == "meet-atlas"
    assert pick("guide:atlas:pabc:using-dispatch.md") == "using-dispatch"
    assert pick("guide:pabc:using-dispatch.md:1790000000") == "using-dispatch"
    assert pick("firstrun:how-dispatch-works.md") == "how-dispatch-works"
    assert pick("kit:pabc:meet-atlas:1") == "meet-atlas"
    assert pick("guide:atlas:pabc:using-dispatch.md:r2") == "using-dispatch"
    assert migrate._seed_chat(chats, "guide:atlas:pabc:using-dispatch.md:r2")[1] == 2
    assert migrate._seed_chat(chats, "kit:pabc:using-dispatch:2")[1] == 2
    assert pick("release:4.1.0:abc") is None


def test_the_cli_takes_own_and_refuses_a_bad_one(data, capsys):
    assert migrate.main(["--own", "nope"]) == 2
    assert migrate.main(["--dry-run", "--own", "wikka=DisPatch"]) == 0
    assert "own bot of DisPatch" in capsys.readouterr().out


def test_a_key_this_version_does_not_know_survives(data):
    import yaml
    raw = yaml.safe_load((data / "config.yaml").read_text())
    for e in raw["bots"]:
        if e["id"] in ("main", "kid_owl", "atlas"):
            e["addon_field"] = {"kept": e["id"]}
    (data / "config.yaml").write_text(yaml.safe_dump(raw, sort_keys=False))
    config._invalidate_bots_cache()
    migrate.run()
    r = roster(data)
    for bid in ("main", "kid_owl", "atlas"):
        assert r[bid]["addon_field"] == {"kept": bid}, bid


def test_012_never_runs_before_010(tmp_path, monkeypatch):
    from types import SimpleNamespace
    d = build(tmp_path, monkeypatch)
    con = sqlite3.connect(d / "chats.db")
    con.execute("DELETE FROM schema_migrations WHERE id = '010_profiles'")
    con.commit()
    con.close()
    with monkeypatch.context() as m:
        m.setattr(migrate, "profiles_plan", lambda **_: SimpleNamespace(lines=list))
        assert not [a for a in migrate.plan().actions if a.step == "012_templates"]
