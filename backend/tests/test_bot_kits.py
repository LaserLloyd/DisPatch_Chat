"""Kit chats (4.1, reduced to chats in 4.2): the chats a template pushes to every copy, and the
upgrade from 4.0 (app/kits.py). Neutral fixtures only."""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from app import accounts, characters, config, firstrun, kits, policy, profiles_service
from app.contracts.permissions import Role
from app.profiles_service import ProfileServiceError
from tests.profiles_fixtures import scratch

PNG = b"\x89PNG\r\n\x1a\n"


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv("DISPATCH_ATLAS_GUIDES", "1")
    monkeypatch.setenv("DISPATCH_FIRSTRUN", "1")
    kits.unbind_server()
    yield
    kits.unbind_server()


def _deliver(db):
    async def deliver(thread_id, text, *, metadata=None, source_id=None, dedup_recent_window=True):
        return await db.add_message(thread_id, "assistant", text, metadata=metadata, source_id=source_id)
    return deliver


def _titles(s, bot_id, owner=None):
    async def go(db):
        return sorted(t.title for t in await db.list_threads(bot_id)
                      if owner is None or t.owner_profile_id == owner)
    return s.run(go)


def _texts(s, bot_id, title):
    async def go(db):
        out = []
        for t in await db.list_threads(bot_id):
            if t.title == title:
                out += [m.content for m in await db.dump_messages(t.id)]
        return sorted(out)
    return s.run(go)


def _sync(s, **kw):
    return s.run(lambda db: kits.sync(db, _deliver(db), **kw))["chats"]


def _copies(pid, kit_id):
    return [b for b in config.load_bots() if b.scope == "personal" and b.owner_profile == pid
            and kits.kit_id_of(b) == kit_id]


def _otter_kit(s, *, auto_clone=False, chats=()):
    """pack_otter with a picture, as a template (given to everyone when ``auto_clone``) with a kit."""
    config.AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    for role in ("face", "full", "thumb"):
        (config.AVATAR_DIR / f"pack_otter-{role}.png").write_bytes(PNG + b"v1" + role.encode())
    config.upsert_bot(replace(config.get_bot("pack_otter"), avatar="pack_otter-face.png",
                              description="Quick and curious",
                              give_to="everyone" if auto_clone else []))
    kits.save_kit("pack_otter", chats=list(chats))


# ---- the reported bug -------------------------------------------------------- #

def test_deleting_atlas_then_adding_it_back_brings_the_guide_chats(scratch, on):
    s = scratch
    s.connection()
    s.admin()
    s.atlas()                                              # the Atlas template
    _sync(s)                                               # the server has started once
    u = s.profile("Bram")
    first = profiles_service.spawn_atlas(u.id)
    _sync(s)
    assert _titles(s, first, u.id) == ["Meet Atlas", "Using DisPatch"]
    profiles_service.delete_personal(u.id, first)
    assert _sync(s) == [] and not profiles_service.has_atlas(u.id), "a deletion is respected"
    out = profiles_service.clone_to_own(firstrun.BOT_ID, policy.profile_principal(u))
    again = config.get_bot(out["bot"]["id"])
    assert again.pack == firstrun.ATLAS_PACK, "the copy is an Atlas (has_atlas, What's new, connect)"
    assert profiles_service.has_atlas(u.id)
    _sync(s)
    assert _titles(s, again.id, u.id) == ["Meet Atlas", "Using DisPatch"]
    assert kits.removed_for(u.id) == set()


def test_an_atlas_copy_takes_its_picture_from_the_shipped_set_not_the_household_one(scratch, on):
    s = scratch
    s.connection()
    s.admin()
    house = replace(firstrun.atlas_template(), avatar="atlas-house.png", give_to="everyone",
                    advisor={"providers": [], "tools": True, "persona": "You are the house Atlas."})
    config.upsert_bot(house)
    u = s.profile("Bram")
    out = profiles_service.clone_to_own(firstrun.BOT_ID, policy.profile_principal(u))
    copy = config.get_bot(out["bot"]["id"])
    assert copy.advisor.get("persona") == "You are the house Atlas.", "it follows the template"
    assert (config.AVATAR_DIR / copy.avatar).read_bytes() == (firstrun.AVATAR_SRC_DIR / "atlas-face.png").read_bytes()
    assert copy.template == firstrun.BOT_ID


# ---- auto-clone --------------------------------------------------------------- #

def test_a_new_profile_gets_atlas_and_every_auto_clone_kit_it_may_use(scratch, on):
    s = scratch
    s.connection()
    s.admin()
    s.atlas()
    _otter_kit(s, auto_clone=True)
    u = s.profile("Bram")
    profiles_service.on_profile_created(u)
    assert len(_copies(u.id, "atlas")) == 1 and len(_copies(u.id, "pack_otter")) == 1
    b = s.profile("Dee", Role.BASIC)
    profiles_service.on_profile_created(b)
    assert len(s.personal(b.id)) == 2, "a Basic profile has managed copies"


def test_the_sync_gives_every_entitled_profile_its_copy_and_nobody_else(scratch, on):
    s = scratch
    s.connection()
    owner = s.admin()
    s.atlas()
    _sync(s)
    u = s.profile("Bram")
    a = s.profile("Cy", Role.ADMIN)
    b = s.profile("Dee", Role.BASIC)
    r = s.profile("Rae")
    _otter_kit(s, auto_clone=False)
    config.update_bot("pack_otter", lambda t: replace(t, give_to=[u.id, a.id, b.id]))
    _sync(s)
    for who in (u, a, b):
        assert len(_copies(who.id, "pack_otter")) == 1, who.id
    assert _copies(owner.id, "pack_otter") == [] and _copies(r.id, "pack_otter") == [], "not ticked"
    assert len(_copies(u.id, "atlas")) == 1 and len(_copies(owner.id, "atlas")) == 1, "Atlas: everyone"
    _sync(s)
    assert len(_copies(u.id, "pack_otter")) == 1, "idempotent"


def test_a_deleted_copy_stays_deleted_until_added_back_or_reset(scratch, on):
    s = scratch
    s.connection()
    s.admin()
    s.atlas()
    _otter_kit(s, auto_clone=True)
    u = s.profile("Bram")
    profiles_service.on_profile_created(u)
    (copy,) = _copies(u.id, "pack_otter")
    profiles_service.delete_personal(u.id, copy.id)
    _sync(s)
    assert _copies(u.id, "pack_otter") == []
    prev = profiles_service.reset_preview(u.id)
    profiles_service.reset_profile(u.id, prev["token"])
    assert len(_copies(u.id, "pack_otter")) == 1 and len(_copies(u.id, "atlas")) == 1


def test_an_atlas_template_given_to_nobody_stops_new_copies(scratch, on):
    s = scratch
    s.connection()
    s.admin()
    s.atlas([])
    u = s.profile("Bram")
    profiles_service.on_profile_created(u)
    assert _copies(u.id, "atlas") == []
    assert profiles_service.spawn_atlas(u.id), "Reset and the CLI still make one on purpose"


# ---- setup sync ----------------------------------------------------------------- #

def test_an_edited_template_updates_the_copies_that_did_not_change_that_field(scratch, on):
    s = scratch
    s.connection()
    admin = s.admin()
    _otter_kit(s)
    u, v = s.profile("Bram"), s.profile("Cy")
    cu = profiles_service.clone_to_profile("pack_otter", u.id, actor=policy.profile_principal(admin))["bot_id"]
    cv = profiles_service.clone_to_profile("pack_otter", v.id, actor=policy.profile_principal(admin))["bot_id"]
    profiles_service.update_personal(v.id, cv, {"emoji": "🐳"})
    characters.patch("pack_otter", {"emoji": "🐟", "description": "Now with fish"})
    _sync(s)
    a, b = config.get_bot(cu), config.get_bot(cv)
    assert (a.emoji, a.description) == ("🐟", "Now with fish")
    assert (b.emoji, b.description) == ("🐳", "Now with fish"), "the owner's own emoji is kept"
    # and a later change still follows for the fields nobody touched
    characters.patch("pack_otter", {"emoji": "🦈"})
    _sync(s)
    assert config.get_bot(cu).emoji == "🦈" and config.get_bot(cv).emoji == "🐳"


# ---- kit chats ------------------------------------------------------------------ #

def test_kit_chats_reach_every_copy_once_by_audience_and_a_new_rev_once_more(scratch, on):
    s = scratch
    s.connection()
    s.admin()
    _sync(s)
    u, a = s.profile("Bram"), s.profile("Cy", Role.ADMIN)
    _otter_kit(s, auto_clone=True, chats=[
        {"title": "Welcome", "body": "Hello from the house.", "audience": "everyone"},
        {"title": "For Admins", "body": "Admin notes.", "audience": "admins"}])
    _sync(s)
    (cu,), (ca,) = _copies(u.id, "pack_otter"), _copies(a.id, "pack_otter")
    assert _titles(s, cu.id) == ["Welcome"]
    assert _titles(s, ca.id) == ["For Admins", "Welcome"]
    assert _sync(s) == []
    hh = kits.load_household("pack_otter")["chats"]
    welcome = next(c for c in hh if c.title == "Welcome")
    kits.save_kit("pack_otter", chats=[
        {"id": welcome.id, "title": "Welcome", "body": "Hello again.", "audience": "everyone"},
        *[{"id": c.id, "title": c.title, "body": c.body, "audience": c.audience} for c in hh if c is not welcome]])
    _sync(s)
    assert _titles(s, cu.id) == ["Welcome"], "the revised chat, once, in place of the untouched old one"
    assert _texts(s, cu.id, "Welcome") == ["Hello again."]
    kits.save_kit("pack_otter", chats=[
        {"id": welcome.id, "title": "Welcome", "body": "Typo fixed.", "audience": "everyone", "resend": False}])
    assert _sync(s) == []


def test_kit_chats_carry_their_metadata_and_cannot_act_on_the_host(scratch, on):
    s = scratch
    s.connection()
    s.admin()
    _sync(s)
    u = s.profile("Bram")
    _otter_kit(s, auto_clone=True, chats=[
        {"title": "Look", "body": "[[media:/etc/hostname|x]] :react:happy: [[pic:a cat]]", "audience": "everyone"}])
    _sync(s)
    (cu,) = _copies(u.id, "pack_otter")

    async def go(db):
        (t,) = await db.list_threads(cu.id)
        return await db.dump_messages(t.id)
    (m,) = s.run(go)
    assert "[[" not in m.content and ":react:" not in m.content
    assert m.metadata["seeded"] is True and m.metadata["kit"]["bot"] == "pack_otter"


def test_a_deleted_kit_chat_does_not_come_back_but_reset_brings_it_and_the_setup_back(scratch, on):
    s = scratch
    s.connection()
    s.admin()
    s.atlas([])
    _sync(s)
    u = s.profile("Bram")
    atlas = profiles_service.spawn_atlas(u.id)
    _sync(s)
    s.thread(u.id, atlas, "SENT-my-own-chat")

    async def drop_meet(db):
        t = next(t for t in await db.list_threads(atlas) if t.title == "Meet Atlas")
        await db.delete_thread(t.id)
    s.run(drop_meet)
    assert _sync(s) == [], "the owner deleted it: not posted again by itself"
    profiles_service.update_personal(u.id, atlas, {"name": "Globe", "emoji": "🗺"})
    bot = profiles_service.reset_personal(u.id, atlas)
    assert (bot.name, bot.emoji) == ("Atlas", firstrun.atlas_template().emoji)
    assert len(_sync(s, repost_missing_for=atlas)) == 1
    assert _titles(s, atlas) == ["Meet Atlas", "SENT-my-own-chat", "Using DisPatch"]


def test_reset_is_only_for_a_copy_of_a_managed_bot_and_only_your_own(scratch, on):
    s = scratch
    s.connection()
    s.admin()
    s.atlas([])
    u, v = s.profile("Bram"), s.profile("Cy")
    own = profiles_service.create_personal(u.id, {"name": "Mine"})
    with pytest.raises(ProfileServiceError) as e:
        profiles_service.reset_personal(u.id, own.id)
    assert e.value.code == "not_managed"
    atlas = profiles_service.spawn_atlas(u.id)
    with pytest.raises(ProfileServiceError) as e:
        profiles_service.reset_personal(v.id, atlas)
    assert e.value.status == 404


# ---- upgrading a 4.0 install -------------------------------------------------- #

def test_a_4_0_install_gets_no_duplicate_guides_and_keeps_deletions(scratch, on, monkeypatch):
    s = scratch
    s.connection()
    s.admin()
    s.atlas()
    u, w, fresh = s.profile("Bram"), s.profile("Cy"), s.profile("Fay")
    # 4.0 shape: an Atlas copy without kit fields, its guides posted under guide:<bot>:<file>
    monkeypatch.setenv("DISPATCH_ATLAS_GUIDES", "0")
    atlas = profiles_service.spawn_atlas(u.id)
    config.update_bot(atlas, lambda b: replace(b, kit_version="", kit_baseline=None))
    monkeypatch.setenv("DISPATCH_ATLAS_GUIDES", "1")
    for title, f in (("Meet Atlas", "meet-your-atlas.md"), ("Using DisPatch", "using-dispatch.md")):
        s.thread(u.id, atlas, title, messages=[("assistant", "guide")])

        async def tag(db, f=f, title=title):
            t = next(t for t in await db.list_threads(atlas) if t.title == title)
            await db.db.execute("UPDATE messages SET source_id = ? WHERE thread_id = ?", (f"guide:{atlas}:{f}", t.id))
            await db.db.commit()
        s.run(tag)
    # Cy has no Atlas copy but other chats; Fay was made with none and has no chats
    s.thread(w.id, "pack_otter", "SENT-cy")
    (config.DATA_DIR / firstrun.BACKFILL_FLAG).write_text("done\n")
    firstrun._write_flag()                     # a 4.0 install has long been past its first run
    posted = _sync(s)
    assert _titles(s, atlas) == ["Meet Atlas", "Using DisPatch"], "no duplicates of the 4.0 guides"
    (fay_atlas,) = _copies(fresh.id, "atlas")
    (cy_atlas,) = _copies(w.id, "atlas")
    assert len(posted) == 7 and _titles(s, fay_atlas.id) == ["Meet Atlas", "Using DisPatch"]
    assert _titles(s, cy_atlas.id) == ["Meet Atlas", "Using DisPatch"], "every profile gets its Atlas"
    assert len(_copies(fresh.id, "atlas")) == 1
    assert json.loads((kits.kit_dir() / kits.STATE_NAME).read_text())["migrated"] is True


def test_the_kit_fields_round_trip_and_old_entries_load(scratch):
    config.upsert_bot(config.Bot(id="old", name="Old", scope="personal", owner_profile="x"))
    b = config.get_bot("old")
    assert b.kit_version == "" and b.kit_baseline is None
    config.upsert_bot(replace(b, kit_version="v1", kit_baseline={"name": "Old"}))
    b = config.get_bot("old")
    assert (b.kit_version, b.kit_baseline) == ("v1", {"name": "Old"})


def test_an_agent_bot_cannot_carry_a_kit(scratch):
    scratch.admin()
    with pytest.raises(ValueError):
        kits.save_kit("helper", chats=[])
    assert kits.get_kit("helper") is None and kits.kit_out("helper")["copyable"] is False


def test_a_household_bot_removed_takes_its_kit_with_it(scratch, on):
    s = scratch
    s.admin()
    _otter_kit(s, auto_clone=True)
    assert kits.get_kit("pack_otter") is not None
    characters.remove("pack_otter")
    assert kits.get_kit("pack_otter") is None


def test_reaction_markers_are_defused_in_any_case():
    assert ":react:" not in kits.neutralise(":react:joy:").lower().replace("​", "x")
    assert ":REACT:" not in kits.neutralise(":REACT:joy:")
    assert "[[media:" not in kits.neutralise("[[media:/etc/hostname]]")


def test_household_atlas_edits_reach_the_copies_but_its_picture_pool_does_not(scratch, on):
    """Review M1: the household Atlas is Atlas's template while it exists."""
    s = scratch
    s.connection()
    s.admin()
    s.atlas()
    u = s.profile("Bram")
    _sync(s)
    (copy,) = _copies(u.id, "atlas")
    config.upsert_bot(replace(config.get_bot(firstrun.BOT_ID), emoji="🧭", avatar="atlas-pool-7.png"))
    _sync(s)
    again = config.get_bot(copy.id)
    assert again.emoji == "🧭" and again.name == "Atlas"
    assert (config.AVATAR_DIR / again.avatar).read_bytes() == \
        (firstrun.AVATAR_SRC_DIR / "atlas-face.png").read_bytes()


# ---- a revised chat replaces the version nobody wrote in ---------------------- #

def test_a_revised_chat_keeps_the_old_thread_when_the_owner_wrote_in_it(scratch, on):
    s = scratch
    s.connection()
    s.admin()
    _sync(s)
    u = s.profile("Bram")
    _otter_kit(s, auto_clone=True, chats=[{"title": "Welcome", "body": "Hello.", "audience": "everyone"}])
    _sync(s)
    (cu,) = _copies(u.id, "pack_otter")

    async def reply(db):
        (t,) = await db.list_threads(cu.id)
        await db.add_message(t.id, "user", "SENT-thanks")
    s.run(reply)
    (welcome,) = kits.load_household("pack_otter")["chats"]
    kits.save_kit("pack_otter", chats=[
        {"id": welcome.id, "title": "Welcome", "body": "Hello again.", "audience": "everyone"}])
    _sync(s)
    assert _titles(s, cu.id) == ["Welcome", "Welcome"], "the owner's own words are never thrown away"
    assert _texts(s, cu.id, "Welcome") == ["Hello again.", "Hello.", "SENT-thanks"]


def test_a_shipped_guide_revision_replaces_an_untouched_4_0_guide_thread(scratch, on, monkeypatch):
    s = scratch
    s.connection()
    s.admin()
    s.atlas([])
    _sync(s)
    u = s.profile("Bram")
    atlas = profiles_service.spawn_atlas(u.id)
    _sync(s)
    assert _titles(s, atlas, u.id) == ["Meet Atlas", "Using DisPatch"]
    revised = tuple(replace(c, rev=c.rev + 1) if c.id == "using-dispatch" else c
                    for c in kits.SHIPPED[kits.ATLAS])
    monkeypatch.setitem(kits.SHIPPED, kits.ATLAS, revised)
    made = _sync(s)
    assert len(made) == 1
    assert _titles(s, atlas, u.id) == ["Meet Atlas", "Using DisPatch"], "no second Using DisPatch"
    assert _sync(s) == []


def test_the_shipped_using_dispatch_guide_is_illustrated():
    (using,) = [c for c in kits.SHIPPED[kits.ATLAS] if c.id == "using-dispatch"]
    assert using.rev >= 2 and using.audience == "everyone"
    import re
    pics = re.findall(r"!\[[^\]]+\]\(/static/guide/([\w.-]+\.png)\)", using.text())
    assert len(pics) >= 4
    from pathlib import Path
    static = Path(__file__).resolve().parents[2] / "frontend" / "static" / "guide"
    for name in pics:
        assert (static / name).is_file(), name
