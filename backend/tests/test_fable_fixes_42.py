"""4.2 Fable-review fix pass (backend): H1 Owner deliberate delete + agent create gate, H3
detach keeps lineage (re-link on re-tick), M1 everyone_to_list, M2 "Get my copy" row, M4 Sage
protected, M8 digest slug, L1..L8. Neutral fixtures only."""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from profiles_fixtures import scratch
from test_characters_manage import PROV, _copy, _manage, _row, hh

from app import (
    accounts,
    advisor,
    characters,
    characters_manage,
    config,
    kits,
    migrate,
    ownership,
    policy,
    profiles_service,
)

PNG = b"\x89PNG\r\n\x1a\n"


def _chat(hh, owner_id, bot_id, title="my chat"):
    from profiles_fixtures import Scratch
    return Scratch(hh.world.tmp).thread(owner_id, bot_id, title)


# ---- H1 ------------------------------------------------------------------------------- #

def test_h1_the_owner_may_delete_his_agent_bot_the_row_says_so(hh):
    d = _manage(hh.Olive)
    assert _row(d["mine"], "bits")["actions"]["delete"] is True


def test_h1_delete_an_agent_bot_needs_the_confirmation_then_removes_it_and_its_chats(hh):
    tid = _chat(hh, hh.oid, "bits", "bits chat")
    r = hh.Olive.get("/api/me/characters/bits/delete-preview")
    assert r.status_code == 200, r.text
    assert r.json() == {"chats": 1, "agent": True, "kept_by_reset": True}
    r = hh.Olive.delete("/api/me/characters/bits")
    assert r.status_code == 409 and r.json()["error"]["code"] == "confirm_required", r.text
    assert config.get_bot("bits") is not None
    r = hh.Olive.delete("/api/me/characters/bits?confirm=true")
    assert r.status_code == 200, r.text
    assert config.get_bot("bits") is None
    from profiles_fixtures import Scratch
    assert Scratch(hh.world.tmp).run(lambda db: db.get_thread(tid)) is None


def test_h1_reset_still_keeps_the_agent_bot(hh):
    _chat(hh, hh.oid, "bits")
    profiles_service.wipe_profile_content(hh.oid)
    assert config.get_bot("bits") is not None


def test_h1_another_admin_cannot_delete_it_and_a_user_cannot_either(hh):
    for who in (hh.Eve, hh.Max):
        assert who.delete("/api/me/characters/bits?confirm=true").status_code == 404
    assert config.get_bot("bits") is not None


def test_h1_a_second_admin_may_not_create_an_agent_backed_character(hh):
    body = {"name": "Spy", "backend": "agent", "agent": "main"}
    r = hh.Eve.post("/api/characters", json=body)
    assert r.status_code == 403 and r.json()["error"]["code"] == "agent_create_owner_only", r.text
    assert not any(b.name == "Spy" for b in config.load_bots())
    r = hh.Olive.post("/api/characters", json=body)                        # the Owner may
    assert r.status_code == 201, r.text
    # a direct character is still fine for the second Admin
    assert hh.Eve.post("/api/characters", json={"name": "Plain"}).status_code == 201


# ---- M4 ------------------------------------------------------------------------------- #

def _sage(hh):
    return config.upsert_bot(config.Bot(id="advisor", name="Sage", scope="personal", owner_profile=hh.oid,
                                        advisor=json.loads(json.dumps(PROV))))


def test_m4_sage_survives_reset_and_is_named_as_kept(hh):
    _sage(hh)
    prev = profiles_service.reset_preview(hh.oid)
    kept = {k["id"] for k in prev["kept_bots"]}
    assert {"bits", "advisor"} <= kept
    profiles_service.wipe_profile_content(hh.oid)
    assert config.get_bot("advisor") is not None


def test_m4_sage_delete_follows_the_owner_deliberate_rule(hh):
    _sage(hh)
    assert _row(_manage(hh.Olive)["mine"], "advisor")["actions"]["delete"] is True
    r = hh.Olive.delete("/api/me/characters/advisor")
    assert r.status_code == 409 and r.json()["error"]["code"] == "confirm_required"
    assert hh.Olive.delete("/api/me/characters/advisor?confirm=true").status_code == 200
    assert config.get_bot("advisor") is None


def test_m4_an_ordinary_own_bot_is_not_protected(hh):
    mine = profiles_service.create_personal(hh.oid, {"name": "Plain"})
    assert not characters.is_protected_personal(mine)
    assert hh.Olive.delete(f"/api/me/characters/{mine.id}").status_code == 200


# ---- H3 ------------------------------------------------------------------------------- #

def _tpl_world(s):
    s.connection()
    a, u = s.admin(), s.profile("Bram")
    tpl = config.get_bot("pack_otter")
    config.update_bot(tpl.id, lambda b: replace(b, give_to=[a.id, u.id]))
    for p in (a, u):
        profiles_service.make_copy(p, "pack_otter")
    return a, u


def _mine_of(s, pid):
    return [b for b in s.personal(pid) if b.name == "Otter" or b.template == "pack_otter"
            or b.detached_from == "pack_otter"]


def test_h3_untick_then_retick_relinks_the_same_bot_and_keeps_changes(scratch):
    s = scratch
    a, u = _tpl_world(s)
    mine = profiles_service.copy_of(u.id, "pack_otter")
    profiles_service.update_personal(u.id, mine.id, {"name": "Mine!"})
    thread = s.thread(u.id, mine.id, "SENT-mine")
    profiles_service.set_give_to("pack_otter", [a.id])
    det = config.get_bot(mine.id)
    assert det.template == "" and det.detached_from == "pack_otter"
    assert kits.kit_id_of(det) == "", "lineage is not a kit link"
    prev = profiles_service.give_to_preview("pack_otter", [a.id, u.id])
    assert prev["add"] == [u.id]
    profiles_service.set_give_to("pack_otter", [a.id, u.id])
    again = config.get_bot(mine.id)
    assert again.template == "pack_otter" and again.detached_from == ""
    assert again.name == "Mine!" and "name" in again.overrides, "customisation survives as overrides"
    assert len([b for b in s.personal(u.id) if b.id != ""]) == 1, "no twin"
    assert s.run(lambda db: db.get_thread(thread)) is not None
    config.update_bot("pack_otter", lambda t: replace(t, description="Fresh"))
    assert config.get_bot(mine.id).description == "Fresh", "follows the template again"


def test_h3_stop_sharing_then_share_again_relinks(scratch):
    s = scratch
    a, u = _tpl_world(s)
    mine = profiles_service.copy_of(u.id, "pack_otter")
    s.thread(u.id, mine.id, "SENT-x")
    characters.remove("pack_otter")                         # stop sharing: copies detach
    assert config.get_bot(mine.id).detached_from == "pack_otter"
    # the same household bot comes back under the same id and is given to Bram again
    config.upsert_bot(config.Bot(id="pack_otter", name="Otter", emoji="🦦", give_to=[],
                                 advisor={"providers": [{"provider": "openai_compatible", "model": "m1"}],
                                          "tools": True}))
    profiles_service.set_give_to("pack_otter", [u.id])
    assert config.get_bot(mine.id).template == "pack_otter"
    assert len(s.personal(u.id)) == 1


def test_h3_a_deleted_own_bot_gets_a_fresh_copy_not_a_ghost(scratch):
    s = scratch
    a, u = _tpl_world(s)
    mine = profiles_service.copy_of(u.id, "pack_otter")
    s.thread(u.id, mine.id, "SENT-x")
    profiles_service.set_give_to("pack_otter", [a.id])
    profiles_service.delete_personal(u.id, mine.id)
    profiles_service.set_give_to("pack_otter", [a.id, u.id])
    new = profiles_service.copy_of(u.id, "pack_otter")
    assert new is not None and new.id != mine.id


def test_h3_detached_from_round_trips_through_config(scratch):
    s = scratch
    a, u = _tpl_world(s)
    mine = profiles_service.copy_of(u.id, "pack_otter")
    s.thread(u.id, mine.id, "SENT-y")
    profiles_service.set_give_to("pack_otter", [a.id])
    config._invalidate_bots_cache()
    assert config.get_bot(mine.id).detached_from == "pack_otter"


# ---- M1 ------------------------------------------------------------------------------- #

def test_m1_the_preview_says_when_everyone_becomes_a_list(hh):
    config.update_bot("tmpl", lambda b: replace(b, give_to="everyone"))
    q = json.dumps([hh.ids["Max"]])
    r = hh.Olive.get(f"/api/characters/tmpl/give-to/preview?give_to={q}")
    assert r.status_code == 200 and r.json()["everyone_to_list"] is True, r.text
    r = hh.Olive.get("/api/characters/tmpl/give-to/preview?give_to=everyone")
    assert r.json()["everyone_to_list"] is False
    config.update_bot("tmpl", lambda b: replace(b, give_to=[hh.ids["Max"]]))
    r = hh.Olive.get(f"/api/characters/tmpl/give-to/preview?give_to={q}")
    assert r.json()["everyone_to_list"] is False


# ---- M2 ------------------------------------------------------------------------------- #

def test_m2_the_get_my_copy_row_is_a_clean_call_to_action(hh):
    mine = _copy(hh.ids["Max"])
    assert hh.Max.delete(f"/api/me/characters/{mine.id}").status_code == 200
    row = _row(_manage(hh.Max)["mine"], "tmpl")
    assert row["actions"]["make_copy"] is True and row["actions"]["details"] is False
    assert row["give_to"] is None and row["copies"] == []
    assert not any(v for k, v in row["actions"].items() if k != "make_copy")
    tpl = config.get_bot("tmpl")
    pr = policy.profile_principal(accounts.get_profile(hh.ids["Max"]))
    assert characters.may_view_item(tpl, pr), "the picture of the bot given to you is readable"
    other = policy.profile_principal(accounts.get_profile(hh.ids["Lobby"]))
    assert not characters.may_view_item(tpl, other), "not given to Lobby: still refused"


def test_m2_other_rows_keep_details(hh):
    d = _manage(hh.Olive)
    assert _row(d["mine"], "bits")["actions"]["details"] is True
    assert _row(d["household"], "tmpl")["actions"]["details"] is True


# ---- M8 ------------------------------------------------------------------------------- #

def test_m8_the_digest_slug_uses_the_template_id(tmp_path, monkeypatch):
    from test_advisor import _corpus, _rec_hooks, datetime_at
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.yaml")
    config._invalidate_bots_cache()
    root = _corpus(tmp_path)
    adv = {"owner": "Sam", "knowledge_dir": str(root),
           "providers": [{"provider": "custom", "base_url": "http://p/v1", "model": "m"}]}
    tpl = config.upsert_bot(config.Bot(id="atlas", name="Atlas", give_to="everyone", advisor=adv))
    config.upsert_bot(config.new_copy(tpl, id="pdeadbeef0001", owner_profile="x", cloned_from="atlas"))
    monkeypatch.setattr(advisor, "_ledger", None)
    advisor._indexes.clear()
    old = advisor._hooks
    hooks, _ = _rec_hooks(tmp_path)
    ts = datetime_at("2026-10-08")

    async def threads_since(bot_id, since):
        return [("t1", "Chat")]

    async def list_messages(tid, limit):
        return ([SimpleNamespace(role="user", content="hi", metadata=None, created_at=ts),
                 SimpleNamespace(role="assistant", content="hello", metadata=None, created_at=ts)], False)
    hooks.threads_since, hooks.list_messages = threads_since, list_messages
    advisor.bind(hooks)

    async def fake_stream(res, entry, messages, on_text):
        return "# Conversations\n\nbody"
    monkeypatch.setattr(advisor, "_stream_openai", fake_stream)
    try:
        res = [r for r in asyncio.run(advisor.digest("2026-10-08")) if r.get("path")]
        assert res and all(Path(r["path"]).name == "2026-10-08-atlas.md" for r in res), res
    finally:
        advisor._hooks = old
        advisor._indexes.clear()
        config._invalidate_bots_cache()


# ---- L1 / L3 -------------------------------------------------------------------------- #

@pytest.mark.parametrize("body", [{"connection": "c1"}, {"model": "m"}, {"tools": True}])
def test_l1_an_agent_row_takes_no_connection_model_or_lookups(hh, body):
    r = hh.Olive.patch("/api/characters/bits", json=body)
    assert r.status_code == 422 and r.json()["error"]["code"] == "character_invalid", r.text


def test_l3_a_template_has_no_hide_action_and_no_hidden_field(hh):
    row = _row(_manage(hh.Olive)["household"], "tmpl")
    assert row["actions"]["hide"] is False and row["hidden"] is False
    r = hh.Olive.patch("/api/characters/tmpl", json={"hidden": True})
    assert r.status_code == 422 and r.json()["error"]["code"] == "character_invalid"
    # a copy / own bot / agent bot still hides
    assert _row(_manage(hh.Olive)["mine"], "bits")["actions"]["hide"] is True


# ---- L2 ------------------------------------------------------------------------------- #

def test_l2_connection_null_on_a_copy_goes_back_to_the_template(scratch):
    s = scratch
    c1 = s.connection(name="One")
    c2 = s.connection(default=False, name="Two")
    tpl = config.upsert_bot(config.Bot(id="tp", name="Tp", give_to=[], advisor={
        "providers": [{"provider": "openai_compatible", "model": "m1", "connection": c1.id,
                       "base_url": c1.base_url}], "tools": True}))
    u = s.profile("Bram")
    config.update_bot(tpl.id, lambda b: replace(b, give_to=[u.id]))
    mine = profiles_service.make_copy(u, "tp")[0]
    profiles_service.update_personal(u.id, mine.id, {"connection": c2.id})
    got = config.get_bot(mine.id)
    assert characters.connection_of(got) == c2.id and "connection" in characters_manage.overridden_fields(got)
    profiles_service.update_personal(u.id, mine.id, {"connection": None})
    got = config.get_bot(mine.id)
    assert characters.connection_of(got) == c1.id
    assert "connection" not in characters_manage.overridden_fields(got)
    assert "provider0_picked" not in (got.overrides or {})


def test_l2_connection_null_on_an_own_bot_detaches_the_reference(scratch):
    s = scratch
    c1 = s.connection(name="One")
    u = s.profile("Bram")
    own = profiles_service.create_personal(u.id, {"name": "Mine", "connection": c1.id})
    assert characters.connection_of(own) == c1.id
    profiles_service.update_personal(u.id, own.id, {"connection": None})
    assert characters.connection_of(config.get_bot(own.id)) in ("", None)


# ---- L4 ------------------------------------------------------------------------------- #

def test_l4_the_migration_names_itself_4_2_0():
    assert migrate.TARGET_VERSION == "4.2.0"
    assert migrate._BACKUP_DIR_RE.match("4.0.0-20261024-020000"), "older folders are still found"


# ---- L5 ------------------------------------------------------------------------------- #

def test_l5_the_owner_old_meet_atlas_thread_is_adopted_not_posted_twice(scratch, monkeypatch):
    monkeypatch.setenv("DISPATCH_ATLAS_GUIDES", "1")
    monkeypatch.setenv("DISPATCH_FIRSTRUN", "1")
    kits.unbind_server()
    s = scratch
    s.connection()
    owner = s.admin()
    s.atlas()
    profiles_service.ensure_copies(owner)
    mine = profiles_service.copy_of(owner.id, "atlas")
    old = s.thread(owner.id, mine.id, "Meet Atlas", messages=[("assistant", "the 2026-10-14 text")])

    async def go(db):
        async def deliver(thread_id, text, *, metadata=None, source_id=None, dedup_recent_window=True):
            return await db.add_message(thread_id, "assistant", text, metadata=metadata, source_id=source_id)
        await kits.deliver_chats(db, deliver)
        return [t.title for t in await db.list_threads(mine.id)]
    titles = s.run(go)
    assert titles.count("Meet Atlas") == 1, titles
    assert old and s.run(lambda db: db.get_thread(old)) is not None
    kits.unbind_server()


def test_l5_another_profile_still_gets_its_meet_atlas(scratch, monkeypatch):
    monkeypatch.setenv("DISPATCH_ATLAS_GUIDES", "1")
    monkeypatch.setenv("DISPATCH_FIRSTRUN", "1")
    kits.unbind_server()
    s = scratch
    s.connection()
    s.admin()
    u = s.profile("Bram")
    s.atlas()
    profiles_service.ensure_copies(u)
    mine = profiles_service.copy_of(u.id, "atlas")
    s.thread(u.id, mine.id, "Meet Atlas", messages=[("assistant", "hand made")])

    async def go(db):
        async def deliver(thread_id, text, *, metadata=None, source_id=None, dedup_recent_window=True):
            return await db.add_message(thread_id, "assistant", text, metadata=metadata, source_id=source_id)
        await kits.deliver_chats(db, deliver)
        return [t.title for t in await db.list_threads(mine.id)]
    assert s.run(go).count("Meet Atlas") == 2
    kits.unbind_server()


# ---- L7 / L8 -------------------------------------------------------------------------- #

def test_l7_clone_answers_200_when_the_copy_exists_and_201_when_made(hh):
    r = hh.Max.post("/api/characters/tmpl/clone")
    assert r.status_code == 200, r.text
    mine = _copy(hh.ids["Max"])
    assert r.json()["bot"]["id"] == mine.id
    assert hh.Max.delete(f"/api/me/characters/{mine.id}").status_code == 200
    r = hh.Max.post("/api/characters/tmpl/clone")
    assert r.status_code == 201, r.text


def test_l8_me_characters_overridden_matches_the_manage_row(hh):
    mine = _copy(hh.ids["Max"])
    assert hh.Max.patch(f"/api/characters/{mine.id}", json={"name": "Maxed"}).status_code == 200
    row = _row(_manage(hh.Max)["mine"], mine.id)
    got = hh.Max.get(f"/api/me/characters/{mine.id}").json()
    assert row["overridden"] == ["name"] and got["overridden"] == row["overridden"]
    plain = profiles_service.create_personal(hh.ids["Max"], {"name": "P"})
    assert hh.Max.get(f"/api/me/characters/{plain.id}").json()["overridden"] == []


def test_h1_the_household_delete_route_also_takes_the_owner_agent_bot(hh):
    tid = _chat(hh, hh.oid, "bits", "bits chat")
    assert hh.Eve.delete("/api/characters/bits").status_code == 404
    assert config.get_bot("bits") is not None
    r = hh.Olive.delete("/api/characters/bits")
    assert r.status_code == 200 and r.json()["removed"] == "bits", r.text
    assert config.get_bot("bits") is None
    from profiles_fixtures import Scratch
    assert Scratch(hh.world.tmp).run(lambda db: db.get_thread(tid)) is None
