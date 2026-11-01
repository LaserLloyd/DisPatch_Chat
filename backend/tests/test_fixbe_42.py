"""4.2 backend fix pass (Opus code review): H-1 kit switch, H-2 ticked bots, H-3 open install,
H-4 voice, and the MEDIUMs. Uses the Characters-page ``hh`` fixture."""
from __future__ import annotations

import json
from dataclasses import replace

import pytest
from test_characters_manage import PROV, _chat, _copy, _manage, hh

from app import accounts, config, kits, profiles_service
from app.contracts.permissions import Role


def _copies(tid):
    return [b for b in config.load_bots() if b.template == tid]


# ---- H-1 ------------------------------------------------------------------------------- #

def test_kit_switch_off_never_removes_a_copy_and_only_stops_future_copies(hh):
    profiles_service.set_give_to("tmpl", "everyone")
    before = {b.id for b in _copies("tmpl")}
    assert len(before) >= 4
    _chat(hh, hh.ids["Max"], _copy(hh.ids["Max"]).id)
    r = hh.Olive.put("/api/characters/tmpl/kit", json={"auto_clone": False, "chats": []})
    assert r.status_code == 200, r.text
    assert {b.id for b in _copies("tmpl")} == before
    assert all(not b.overrides.get("detached") for b in _copies("tmpl"))
    given = config.get_bot("tmpl").give_to
    assert isinstance(given, list) and set(given) >= {hh.ids["Max"], hh.ids["Eve"]}
    # a profile made later does not get one
    late = accounts.create_profile("Late", Role.USER)
    profiles_service.on_profile_created(late)
    assert not [b for b in _copies("tmpl") if b.owner_profile == late.id]
    # switching it on again means "everyone"
    r = hh.Olive.put("/api/characters/tmpl/kit", json={"auto_clone": True, "chats": []})
    assert r.status_code == 200
    assert config.get_bot("tmpl").give_to == "everyone"
    assert [b for b in _copies("tmpl") if b.owner_profile == late.id]


def test_kit_switch_off_on_an_explicit_list_changes_nothing(hh):
    r = hh.Olive.put("/api/characters/tmpl/kit", json={"auto_clone": False, "chats": []})
    assert r.status_code == 200
    assert config.get_bot("tmpl").give_to == [hh.ids["Max"], hh.ids["Dee"]]
    assert len(_copies("tmpl")) == 2


# ---- H-2 ------------------------------------------------------------------------------- #

def _owned_templates(pid):
    return {b.template for b in config.load_bots() if b.owner_profile == pid and b.template}


def test_people_create_ticked_templates_get_a_copy(hh):
    r = hh.Olive.post("/api/people", json={"name": "Kid", "role": "basic", "household_bots": ["tmplb"]})
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    assert "tmplb" in _owned_templates(pid)
    assert pid in config.get_bot("tmplb").give_to


def test_wizard_basic_default_household_gets_copies(hh):
    r = hh.Olive.post("/api/wizard/family", json={"people": [
        {"name": "Tiny", "role": "basic"},
        {"name": "Tot", "role": "basic", "household_bots": ["tmplb"]}]})
    assert r.status_code == 200, r.text
    tiny, tot = r.json()["basic"]
    assert {"tmpl", "tmplb"} <= _owned_templates(tiny)
    assert _owned_templates(tot) == {"tmplb"} | ({"atlas"} & {b.id for b in config.load_bots()
                                                              if b.give_to == "everyone"})


def test_cli_create_household_gets_copies(hh, monkeypatch):
    import argparse

    from app import profiles
    ns = argparse.Namespace(name="Cli", role="basic", household="tmplb")
    assert profiles.cmd_create(ns) == 0
    pid = next(p.id for p in accounts.list_profiles() if p.name == "Cli")
    assert "tmplb" in _owned_templates(pid)


# ---- H-3 ------------------------------------------------------------------------------- #

class _Reached(Exception):
    pass


def _turn_on(client, bot_id, monkeypatch):
    from app import main
    sent: list[dict] = []

    async def fake_broadcast(frame, *a, **k):
        sent.append(frame)

    async def reached(*a, **k):
        raise _Reached

    monkeypatch.setattr(main.manager, "broadcast", fake_broadcast)
    monkeypatch.setattr(main, "_compose_agent_text", reached)

    async def go():
        try:
            await main._run_agent_turn("t-x", bot_id, "hi")
        except _Reached:
            return True
        return False

    got = client.portal.call(go)
    return [f.get("code") for f in sent if f.get("type") == "error"], got


def test_unclaimed_install_can_chat_with_atlas(world, monkeypatch):
    from app import firstrun, ownership
    monkeypatch.setattr(config, "DEFAULT_BOTS", config.shipped_default_bots())   # the real fresh roster
    config._invalidate_bots_cache()
    c = world.client()
    assert not accounts.has_owner()
    atlas = c.get("/api/bots").json()
    ids = [b["id"] for b in (atlas["bots"] if isinstance(atlas, dict) else atlas)]
    assert firstrun.BOT_ID in ids, ids              # offered on the open install ...
    assert ownership.protected_admin_id() is None
    codes, reached = _turn_on(c, firstrun.BOT_ID, monkeypatch)
    assert reached and not codes                    # ... so it must answer


# ---- H-4 ------------------------------------------------------------------------------- #

def _voice_house(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.yaml")
    accounts.reset_for_tests()
    owner = accounts.create_profile("Owner", Role.ADMIN, protected=True)
    kid = accounts.create_profile("Kid", Role.BASIC)
    uma = accounts.create_profile("Uma", Role.USER)
    config.upsert_bot(config.Bot(id="tv", name="TV", advisor={"providers": [{"provider": "openai", "model": "m"}]},
                                 give_to="everyone", voice="builtin:af_heart"))
    config._invalidate_bots_cache()
    return owner, kid, uma


def test_a_start_without_the_voice_addon_keeps_builtin_voices(tmp_path, monkeypatch):
    from app import extensions
    monkeypatch.setattr(extensions, "field_validator", lambda f: None)       # add-on not loaded
    owner, kid, uma = _voice_house(tmp_path, monkeypatch)
    profiles_service.reconcile_copies()
    profiles_service.reconcile_copies()
    for p in (owner, kid, uma):
        b = next(x for x in config.load_bots() if x.template == "tv" and x.owner_profile == p.id)
        assert b.voice == "builtin:af_heart", p.name
        assert "voice" not in b.overrides


def test_blanked_voice_is_restored_when_the_voice_is_allowed_again(tmp_path, monkeypatch):
    from app import extensions
    owner, kid, uma = _voice_house(tmp_path, monkeypatch)
    profiles_service.reconcile_copies()
    # damaged 4.2 data: policy blanked the voice (marked) on Uma's copy; a Basic copy blanked unmarked
    for p, mark in ((uma, True), (kid, False)):
        b = next(x for x in config.load_bots() if x.template == "tv" and x.owner_profile == p.id)
        ov = {"voice_cleared": True} if mark else {}
        config.update_bot(b.id, lambda cur, ov=ov: replace(cur, voice="", overrides={**cur.overrides, **ov}))
        assert next(x for x in config.load_bots() if x.id == b.id).voice == ""
    monkeypatch.setattr(extensions, "field_validator", lambda f: None)
    profiles_service.reconcile_copies()
    for p in (uma, kid):
        b = next(x for x in config.load_bots() if x.template == "tv" and x.owner_profile == p.id)
        assert b.voice == "builtin:af_heart" and "voice" not in b.overrides and "voice_cleared" not in b.overrides


def test_an_owners_own_no_voice_choice_is_kept(tmp_path, monkeypatch):
    from app import extensions
    monkeypatch.setattr(extensions, "field_validator", lambda f: None)
    owner, kid, uma = _voice_house(tmp_path, monkeypatch)
    profiles_service.reconcile_copies()
    b = next(x for x in config.load_bots() if x.template == "tv" and x.owner_profile == uma.id)
    config.update_bot(b.id, lambda cur: replace(cur, voice=""))             # the owner picked "no voice"
    profiles_service.reconcile_copies()
    assert next(x for x in config.load_bots() if x.id == b.id).voice == ""


def test_a_voice_the_owner_may_not_use_is_cleared_with_a_mark_then_restored(tmp_path, monkeypatch):
    from app import ext_profile_hooks, extensions
    monkeypatch.setattr(extensions, "field_validator", lambda f: (lambda v: None))
    allowed = {"ok": False}
    owner, kid, uma = _voice_house(tmp_path, monkeypatch)
    config.update_bot("tv", lambda cur: replace(cur, voice="clone-x"))
    monkeypatch.setattr(ext_profile_hooks, "voice_allowed", lambda v, pid: allowed["ok"])
    profiles_service.reconcile_copies()
    b = next(x for x in config.load_bots() if x.template == "tv" and x.owner_profile == uma.id)
    assert b.voice == "" and b.overrides.get("voice_cleared")
    allowed["ok"] = True
    profiles_service.reconcile_copies()
    b = next(x for x in config.load_bots() if x.template == "tv" and x.owner_profile == uma.id)
    assert b.voice == "clone-x" and "voice" not in b.overrides


# ---- M-1 ------------------------------------------------------------------------------- #

def test_household_shim_refuses_to_untick_and_keeps_everyone(hh):
    profiles_service.set_give_to("tmpl", "everyone")
    mx = hh.ids["Max"]
    config_before = config.get_bot("tmpl").give_to
    copies = {b.id for b in _copies("tmpl")}
    accounts.update_profile(mx, household_bots=["tmpl", "tmplb"])
    r = hh.Olive.put(f"/api/people/{mx}/household-bots", json={"household_bots": ["tmplb"]})
    assert r.status_code == 409, r.text
    assert "give" in r.json()["detail"].lower()
    assert config.get_bot("tmpl").give_to == config_before == "everyone"
    assert {b.id for b in _copies("tmpl")} == copies
    assert set(accounts.get_profile(mx).household_bots) == {"tmpl", "tmplb"}      # nothing half-applied


# ---- M-2 ------------------------------------------------------------------------------- #

def test_a_household_bot_made_in_42_is_a_template_given_to_its_creator(hh):
    r = hh.Olive.post("/api/characters", json={"name": "Fresh"})
    assert r.status_code == 201, r.text
    bid = r.json()["id"]
    b = config.get_bot(bid)
    assert b.is_template and b.give_to == [hh.oid]
    assert [c for c in _copies(bid) if c.owner_profile == hh.oid]       # the creator can chat
    # nobody else shares it, and the preview works
    assert not [c for c in _copies(bid) if c.owner_profile != hh.oid]
    r = hh.Olive.get(f"/api/characters/{bid}/give-to/preview", params={"give_to": json.dumps("everyone")})
    assert r.status_code == 200, r.text
    from app import characters, policy
    eve = policy.profile_principal(accounts.get_profile(hh.ids["Eve"]))
    assert not characters.may_chat(config.get_bot(bid), eve)


def test_a_starter_pack_installs_templates_given_to_the_installer(hh):
    packs = hh.Olive.get("/api/starter-packs").json()["packs"]
    assert packs
    r = hh.Olive.post(f"/api/starter-packs/{packs[0]['id']}/install", json={})
    assert r.status_code == 200, r.text
    ids = r.json()["installed"]
    assert ids
    for bid in ids:
        assert config.get_bot(bid).is_template
        assert [c for c in _copies(bid) if c.owner_profile == hh.oid]
        assert not [c for c in _copies(bid) if c.owner_profile != hh.oid]


# ---- M-4 ------------------------------------------------------------------------------- #

def test_untick_never_deletes_a_chat_written_after_the_plan(hh, monkeypatch):
    mx = hh.ids["Max"]
    cp = _copy(mx)
    _chat(hh, mx, cp.id, "seeded")                    # a thread with a user message ...
    # ... that the plan did not see: pretend the plan found no user chats
    monkeypatch.setattr(profiles_service, "_has_user_chats", lambda pid, bid: False)
    out = profiles_service.set_give_to("tmpl", [hh.ids["Dee"]])
    assert mx in out["detached"] and mx not in out["removed"]
    kept = next(b for b in config.load_bots() if b.id == cp.id)
    assert not kept.template                            # detached: the owner's own bot
    assert profiles_service._has_any_thread(mx, cp.id)


# ---- MIG-M2 / MIG-M5 -------------------------------------------------------------------- #

def test_link_overrides_keeps_the_copys_own_connection_when_the_template_has_none():
    from app import migrate
    tpl = config.Bot(id="atl", name="Atlas", give_to="everyone", advisor={"providers": [], "tools": True})
    cp = config.Bot(id="p1c", name="Atlas", scope="personal", owner_profile="x", cloned_from="atl",
                    advisor={"providers": [{"provider": "openai_compatible", "connection": "c-mine",
                                            "model": "m-mine", "base_url": "http://h/v1",
                                            "extra_body": {"a": 1}}], "tools": True})
    ov = migrate._link_overrides(cp, tpl, None)
    assert ov["advisor"]["provider0"] == {"provider": "openai_compatible", "connection": "c-mine",
                                          "model": "m-mine", "base_url": "http://h/v1"}
    assert ov.get("provider0_picked") is True


def test_a_copy_with_its_own_connection_still_follows_the_templates_other_provider_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.yaml")
    config.upsert_bot(config.Bot(id="t5", name="T5", give_to=[], advisor={
        "providers": [{"provider": "openai_compatible", "connection": "c1", "model": "m1",
                       "base_url": "http://a/v1", "extra_body": {"x": 1}, "max_tokens": 700}],
        "tools": True, "persona": "P"}))
    cp = config.new_copy(config.get_bot("t5"), id="p5copy", owner_profile="o1")
    adv = dict(cp.advisor)
    adv["providers"] = [{**adv["providers"][0], "connection": "c-mine", "model": "m-mine",
                         "base_url": "http://mine/v1"}]
    config.upsert_bot(replace(cp, advisor=adv))
    got = config.get_bot("p5copy").advisor["providers"][0]
    assert (got["connection"], got["model"], got["base_url"]) == ("c-mine", "m-mine", "http://mine/v1")
    config.update_bot("t5", lambda b: replace(b, advisor={
        **b.advisor, "providers": [{**b.advisor["providers"][0], "extra_body": {"x": 2}, "max_tokens": 900}]}))
    got = config.get_bot("p5copy").advisor["providers"][0]
    assert got["extra_body"] == {"x": 2} and got["max_tokens"] == 900
    assert got["connection"] == "c-mine"                      # still its own


# ---- MIG-M1 ---------------------------------------------------------------------------- #

def test_a_012_crash_and_rerun_keep_the_first_pre_42_backup(tmp_path, monkeypatch):
    import sqlite3

    from app import migrate
    from tests.migrate_templates_helpers import build
    d = build(tmp_path, monkeypatch)
    real = profiles_service.repoint_threads
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        raise RuntimeError("disk gone")
    monkeypatch.setattr(profiles_service, "repoint_threads", flaky)
    for _ in range(4):                                    # more crash-starts than KEEP_BACKUPS
        with pytest.raises(RuntimeError):
            migrate.run()
    backups = sorted(p for p in (d / "backups").iterdir() if p.is_dir())
    assert len(backups) == 1, backups                     # one backup, the pre-4.2 one
    pre = yaml_bots(backups[0] / "config.yaml")
    assert not any(b.get("template") for b in pre)        # it holds the roster before 012
    monkeypatch.setattr(profiles_service, "repoint_threads", real)
    rep = migrate.run()
    assert rep.backup == backups[0]
    import json
    man = json.loads((backups[0] / "012-created.json").read_text())
    assert man["bots"], "the manifest still lists what the first run made"
    migrate.restore(backups[0])
    assert not any(b.get("template") for b in yaml_bots(d / "config.yaml"))


def yaml_bots(path):
    import yaml
    return yaml.safe_load(path.read_text())["bots"]


# ---- MIG-M3 ---------------------------------------------------------------------------- #

def test_012_dry_run_warns_about_other_profiles_chats_on_agent_bots(tmp_path, monkeypatch):
    import sqlite3

    from app import migrate
    from tests.migrate_templates_helpers import DISPATCH, build
    d = build(tmp_path, monkeypatch)
    con = sqlite3.connect(d / "chats.db")
    con.execute("INSERT INTO threads (id, bot_id, title, owner_profile_id, created_at, updated_at) "
                "SELECT 'zz-alpha-1', 'alpha', 't', ?, created_at, updated_at FROM threads LIMIT 1", (DISPATCH,))
    con.commit()
    con.close()
    text = "\n".join(a.text for a in migrate.run(dry_run=True).actions) + "\n" + \
        "\n".join(migrate.templates_plan().notes)
    assert "'alpha' becomes the Owner's personal bot" in text and "(1)" in text


# ---- MIG-M4 ---------------------------------------------------------------------------- #

def test_012_on_an_unclaimed_install_then_claim_leaves_no_thread_on_a_template(tmp_path, monkeypatch):
    from app import invariants, migrate
    from tests.migrate_templates_helpers import OWNER, build
    build(tmp_path, monkeypatch, claimed=False)
    migrate.run()
    owner = accounts.create_profile("Owner", "admin", protected=True, profile_id=OWNER)
    profiles_service.on_profile_created(owner, first_admin=True)
    bad = [v for v in invariants.check() if v.code == "I1"] if hasattr(invariants, "check") else []
    assert not bad, bad
    adv = config.get_bot("advisor")
    assert adv.scope == "personal" and adv.owner_profile == OWNER and not adv.is_template  # the Owner's own
    wk = config.get_bot("wikka")
    assert wk.is_template and OWNER in wk.give_to               # NULL-owner chats got an owner
    assert any(b.template == "wikka" and b.owner_profile == OWNER for b in config.load_bots())


# ---- MIG-L1 / MIG-L5 ---------------------------------------------------------------------- #

def test_i7_is_quiet_with_the_guides_switched_off(monkeypatch):
    from app import invariants
    monkeypatch.setenv("DISPATCH_ATLAS_GUIDES", "0")
    w = invariants.World.__new__(invariants.World)
    w.sync_pending, w.expected, w.delivered = False, {"c1": {"k1"}}, {}
    assert invariants._i7(w) == []
    monkeypatch.setenv("DISPATCH_ATLAS_GUIDES", "1")
    assert len(invariants._i7(w)) == 1


def test_invariants_route_answers_json_when_the_check_breaks(hh, monkeypatch):
    from app import invariants
    monkeypatch.setattr(invariants, "report", lambda **k: (_ for _ in ()).throw(RuntimeError("x")))
    r = hh.Olive.get("/api/health/invariants")
    assert r.status_code == 200 and r.json()["ok"] is False


# ---- FE-M3 ----------------------------------------------------------------------------- #

def test_a_user_who_deleted_their_copy_gets_a_get_my_copy_row(hh):
    mx = hh.ids["Max"]
    cid = _copy(mx).id
    assert hh.Max.delete(f"/api/me/characters/{cid}").status_code == 200
    d = _manage(hh.Max)
    row = next(r for r in d["mine"] if r["id"] == "tmpl")
    assert row["kind"] == "template" and row["actions"]["make_copy"] is True
    assert [k for k, v in row["actions"].items() if v] == ["make_copy"]
    assert row["give_to"] is None and row["copies"] == []
    assert d["household"] == []
    r = hh.Max.post("/api/characters/tmpl/clone")
    assert r.status_code == 201, r.text
    assert _copy(mx).id != cid
    d = _manage(hh.Max)
    assert all(r["id"] != "tmpl" for r in d["mine"])               # back to a normal copy
    # a profile the template is not given to sees nothing of it
    assert all(r["id"] != "tmpl" for r in _manage(hh.Eve)["mine"])


def test_a_basic_profile_with_a_removed_copy_is_not_offered_what_it_cannot_do(hh):
    cid = _copy(hh.ids["Dee"]).id
    config.update_bot(cid, lambda b: b)
    profiles_service.set_give_to("tmpl", [hh.ids["Max"]])          # Dee unticked: copy detached/removed
    assert all(r["id"] != "tmpl" for r in _manage(hh.Dee)["mine"])


def test_manage_me_names_the_backends_url(hh):
    assert _manage(hh.Olive)["me"]["backends_url"] == "/api/connections"
    assert _manage(hh.Max)["me"]["backends_url"] == "/api/me/backends"
