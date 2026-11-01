"""Every Atlas copy opens with the guide chats, and an update posts one "What's new" chat per
Admin and User profile on its Atlas copy (app/firstrun.py, app/kits.py). Neutral fixtures only."""
from __future__ import annotations

import pytest

from app import __version__, config, firstrun, kits, profiles_service
from app.contracts.permissions import Role
from tests.profiles_fixtures import scratch


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv("DISPATCH_ATLAS_GUIDES", "1")
    monkeypatch.setenv("DISPATCH_FIRSTRUN", "1")
    monkeypatch.setenv("DISPATCH_RELEASE_NOTES", "1")
    kits.unbind_server()
    yield
    kits.unbind_server()


def _deliver(db):
    async def deliver(thread_id, text, *, metadata=None, source_id=None, dedup_recent_window=True):
        return await db.add_message(thread_id, "assistant", text, metadata=metadata, source_id=source_id)
    return deliver


def _threads(s, bot_id):
    async def go(db):
        return [(t.title, t.owner_profile_id) for t in await db.list_threads(bot_id)]
    return s.run(go)


def _drain(s):
    """The 4.1 kit engine: the Atlas guides are the shipped Atlas kit's chats."""
    return s.run(lambda db: kits.sync(db, _deliver(db)))["chats"]


def test_a_new_user_atlas_opens_with_the_guides(scratch, on):
    s = scratch
    s.connection()
    s.admin()
    s.atlas([])                                   # the template, given to nobody by itself
    u = s.profile("Bram")
    bot_id = profiles_service.spawn_atlas(u.id)
    assert len(_drain(s)) == 2
    titles = sorted(t for t, owner in _threads(s, bot_id) if owner == u.id)
    assert titles == ["Meet Atlas", "Using DisPatch"]
    assert _drain(s) == [], "the queue is empty once posted"
    assert _threads(s, firstrun.BOT_ID) == [], "nobody chats with the template"


def test_an_admin_atlas_also_gets_the_setup_tour(scratch, on):
    s = scratch
    s.connection()
    s.admin()
    s.atlas([])
    a = s.profile("Cy", Role.ADMIN)
    bot_id = profiles_service.spawn_atlas(a.id)
    _drain(s)
    assert {t for t, _ in _threads(s, bot_id)} == {"Meet Atlas", "Using DisPatch", "How DisPatch works"}


def test_a_repeat_never_posts_twice_and_reset_gets_fresh_guides(scratch, on):
    s = scratch
    s.connection()
    s.admin()
    s.atlas([])
    u = s.profile("Bram")
    bot_id = profiles_service.spawn_atlas(u.id)
    _drain(s)
    assert _drain(s) == [], "same Atlas again: a no-op"
    s.atlas([u.id])                               # Reset gives back what the template gives this profile
    prev = profiles_service.reset_preview(u.id)
    profiles_service.reset_profile(u.id, prev["token"])
    fresh = next(b.id for b in config.load_bots() if b.owner_profile == u.id)
    assert fresh != bot_id and len(_drain(s)) == 2


def test_a_profile_that_has_chats_but_no_copy_gets_one_with_the_guides(scratch, on):
    s = scratch
    s.admin()
    s.atlas()
    u = s.profile("Bram")
    s.thread(u.id, "pack_otter", "an older chat")      # a 3.x profile: the sync gives it its Atlas copy
    (config.DATA_DIR / firstrun.PENDING_NAME).write_text('[{"profile": "x", "bot": "p000000000000"}]')
    posted = set(_drain(s))
    (own,) = [b.id for b in config.load_bots() if b.owner_profile == u.id]
    mine = {t.id for t in s.run(lambda db: db.list_threads(own))}
    assert len(mine & posted) == 2 and mine <= posted, "nothing for the Atlas that is gone"
    assert not (config.DATA_DIR / firstrun.PENDING_NAME).exists()


def test_switched_off_queues_nothing(scratch, on, monkeypatch):
    monkeypatch.setenv("DISPATCH_ATLAS_GUIDES", "0")
    s = scratch
    s.connection()
    s.admin()
    s.atlas([])
    u = s.profile("Bram")
    bot_id = profiles_service.spawn_atlas(u.id)
    assert _drain(s) == [] and _threads(s, bot_id) == []


def test_a_copy_made_while_the_guides_were_off_gets_them_once_they_are_on(scratch, on, monkeypatch):
    s = scratch
    s.connection()
    s.admin()
    s.atlas([])
    u = s.profile("Bram")
    monkeypatch.setenv("DISPATCH_ATLAS_GUIDES", "0")
    bot_id = profiles_service.spawn_atlas(u.id)
    assert _drain(s) == []
    monkeypatch.setenv("DISPATCH_ATLAS_GUIDES", "1")
    assert len(_drain(s)) == 2 and len(_threads(s, bot_id)) == 2
    assert _drain(s) == []


# ---- "What's new" ----------------------------------------------------------- #

def _notes(s, version):
    return s.run(lambda db: firstrun.post_release_notes(db, _deliver(db), version))


def test_every_shipped_version_has_its_notes():
    assert firstrun.release_notes_file(__version__).is_file()


def test_an_update_posts_whats_new_once_per_admin_and_user(scratch, on, monkeypatch):
    s = scratch
    s.connection()
    owner = s.admin()
    s.atlas([])
    u = s.profile("Bram")
    b = s.profile("Dee", Role.BASIC)
    user_atlas = profiles_service.spawn_atlas(u.id)
    owner_atlas = profiles_service.spawn_atlas(owner.id)       # the main Admin has a copy like anyone
    basic_atlas = profiles_service.spawn_atlas(b.id)
    s.run(lambda db: db.create_thread("pack_otter", title="old", owner_profile_id=owner.id))
    made = _notes(s, __version__)
    assert len(made) == 2
    assert [o for _, o in _threads(s, user_atlas)] == [u.id]
    assert [o for _, o in _threads(s, owner_atlas)] == [owner.id]
    assert _threads(s, basic_atlas) == [] and _threads(s, firstrun.BOT_ID) == []
    assert _notes(s, __version__) == [], "once per version"
    assert (config.DATA_DIR / firstrun.RELEASE_FLAG).read_text().strip() == __version__


def test_a_fresh_install_records_the_version_without_posting(scratch, on):
    s = scratch
    s.admin()
    assert _notes(s, __version__) == []
    assert (config.DATA_DIR / firstrun.RELEASE_FLAG).read_text().strip() == __version__


def test_a_version_without_notes_posts_nothing(scratch, on):
    s = scratch
    owner = s.admin()
    s.atlas([])
    profiles_service.spawn_atlas(owner.id)
    s.run(lambda db: db.create_thread("pack_otter", title="old", owner_profile_id=owner.id))
    assert _notes(s, "0.0.1") == []


# ---- role changes, retries, the on-demand guide, What's new without a copy ---------------- #

def _atlas_ids(pid):
    return [b.id for b in config.load_bots() if b.owner_profile == pid and b.pack == firstrun.ATLAS_PACK]


def test_basic_made_user_gets_an_atlas_with_the_guides_and_user_made_admin_gets_the_tour(scratch, on):
    s = scratch
    s.connection()
    s.admin()
    s.atlas()
    b = s.profile("Dee", Role.BASIC)
    assert _atlas_ids(b.id) == []
    profiles_service.change_role(b.id, "user")
    (atlas,) = _atlas_ids(b.id)
    _drain(s)
    assert sorted(t for t, _ in _threads(s, atlas)) == ["Meet Atlas", "Using DisPatch"]
    profiles_service.change_role(b.id, "admin")
    assert _atlas_ids(b.id) == [atlas], "no second Atlas"
    _drain(s)
    assert sorted(t for t, _ in _threads(s, atlas)) == ["How DisPatch works", "Meet Atlas", "Using DisPatch"]


def test_a_guide_post_that_fails_is_tried_again_and_only_adds_what_is_missing(scratch, on):
    s = scratch
    s.connection()
    s.admin()
    s.atlas([])
    u = s.profile("Bram")
    bot_id = profiles_service.spawn_atlas(u.id)
    calls = {"n": 0}

    def flaky(db):
        real = _deliver(db)

        async def deliver(*a, **k):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("disk hiccup")
            return await real(*a, **k)
        return deliver
    s.run(lambda db: kits.sync(db, flaky(db)))
    assert len(_threads(s, bot_id)) == 1, "the failed one left no empty chat behind"
    assert len(_drain(s)) == 1, "tried again"
    titles = sorted(t for t, _ in _threads(s, bot_id))
    assert titles.count("Meet Atlas") == 1 and titles.count("Using DisPatch") == 1
    assert _drain(s) == []


def test_a_user_without_a_copy_gets_no_whats_new_nobody_chats_with_the_template(scratch, on):
    s = scratch
    owner = s.admin()
    s.atlas([])
    u = s.profile("Bram")                                # no copy of its own
    s.run(lambda db: db.create_thread("pack_otter", title="old", owner_profile_id=owner.id))
    assert _notes(s, __version__) == []
    assert _threads(s, firstrun.BOT_ID) == [] and not _atlas_ids(u.id)


def test_a_patch_release_with_notes_names_its_full_version(scratch, on, monkeypatch, tmp_path):
    s = scratch
    owner = s.admin()
    s.atlas([])
    atlas = profiles_service.spawn_atlas(owner.id)
    s.run(lambda db: db.create_thread("pack_otter", title="old", owner_profile_id=owner.id))
    (tmp_path / "whats-new-9.9.1.md").write_text("Fixes.", encoding="utf-8")
    monkeypatch.setattr(firstrun, "GUIDE_DIR", tmp_path)
    assert len(_notes(s, "9.9.1")) == 1
    assert [t for t, _ in _threads(s, atlas)] == ["What's new in DisPatch 9.9.1"]


def test_the_on_demand_guide_lands_in_the_callers_own_chats(scratch, on):
    s = scratch
    s.connection()
    owner = s.admin()
    s.atlas([])
    a = s.profile("Cy", Role.ADMIN)
    mine = profiles_service.spawn_atlas(a.id)
    owner_atlas = profiles_service.spawn_atlas(owner.id)
    tid = s.run(lambda db: firstrun.seed_one(db, _deliver(db), "set-up-add-ons", profile_id=a.id))
    assert ("Set up add-ons", a.id) in _threads(s, mine)
    with pytest.raises(firstrun.GuideExists):
        s.run(lambda db: firstrun.seed_one(db, _deliver(db), "set-up-add-ons", profile_id=a.id))
    # the main Admin gets hers on its own copy, separately (a machine caller means this profile)
    s.run(lambda db: firstrun.seed_one(db, _deliver(db), "set-up-add-ons"))
    assert ("Set up add-ons", owner.id) in _threads(s, owner_atlas)
    assert tid
    # a profile with no copy has no Atlas to talk to
    u = s.profile("Bram")
    with pytest.raises(KeyError):
        s.run(lambda db: firstrun.seed_one(db, _deliver(db), "set-up-add-ons", profile_id=u.id))


def test_whats_new_is_posted_after_the_guides_so_it_sits_on_top():
    import inspect

    from app import main
    src = inspect.getsource(main._lifespan_startup)
    assert src.index("fresh_install = await firstrun.count_threads(db)") < src.index("seed_if_first_run")
    assert src.index("kits.sync(db") < src.index("post_release_notes(db, _deliver_assistant_text, fresh=fresh_install)")


def test_the_guides_name_only_what_their_readers_have():
    """Users and the shared profile read meet-your-atlas + using-dispatch: no Admin-only
    section; and no 3.x names anywhere (lock screen, the Tools popup, ⋯ menus, fix grammar)."""
    g = firstrun.GUIDE_DIR
    for f in ("meet-your-atlas.md", "using-dispatch.md"):
        text = (g / f).read_text(encoding="utf-8")
        assert "Settings → Characters" not in text and "AI & Connections**" not in text.replace(
            "ask an Admin to add one under **Settings → AI & Connections**", ""), f
    for f in g.glob("*.md"):
        text = f.read_text(encoding="utf-8")
        for stale in ("fix grammar", "Tools popup", "lock screen", "(⋯", "on the bar", "summarise"):
            assert stale not in text, (f.name, stale)
    notes = firstrun.release_notes_file(__version__).read_text(encoding="utf-8")
    assert not notes.lstrip().startswith("## What's new"), "the thread title already says it"


def test_the_house_pass_is_gone():
    """4.1's pass that posted guides on the household Atlas for the main Admin: the claim hook and
    the kit engine do that on its copy now."""
    for name in ("backfill_house_guides", "HOUSE_FLAG", "HOUSE_GUIDES", "_house_revs", "_house_source"):
        assert not hasattr(firstrun, name), name


def test_rv43_the_user_guide_names_the_4_3_rail_and_no_removed_screen():
    """4.3 review: the rail says "Sign out to <default>" and has its own Switch profile button;
    the old "Settings -> My bots" screenshot shows the removed roster."""
    text = (firstrun.GUIDE_DIR / "using-dispatch.md").read_text(encoding="utf-8")
    assert "**Lock** sends" not in text and "in the profile menu" not in text
    assert "Sign out to DisPatch" in text and "**Switch profile**" in text
    assert "my-bots.png" not in text and "Settings → My bots" not in text
