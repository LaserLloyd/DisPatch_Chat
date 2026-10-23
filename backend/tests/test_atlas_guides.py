"""Every Atlas copy opens with the guide chats, and an update posts one "What's new" chat per
Admin and User profile (app/firstrun.py). Neutral fixtures only."""
from __future__ import annotations

import pytest

from app import __version__, config, firstrun, profiles_service
from app.contracts.permissions import Role
from tests.profiles_fixtures import scratch


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv("DISPATCH_ATLAS_GUIDES", "1")
    monkeypatch.setenv("DISPATCH_FIRSTRUN", "1")
    monkeypatch.setenv("DISPATCH_RELEASE_NOTES", "1")
    firstrun.unbind_server()
    yield
    firstrun.unbind_server()


def _deliver(db):
    async def deliver(thread_id, text, *, metadata=None, source_id=None, dedup_recent_window=True):
        return await db.add_message(thread_id, "assistant", text, metadata=metadata, source_id=source_id)
    return deliver


def _threads(s, bot_id):
    async def go(db):
        return [(t.title, t.owner_profile_id) for t in await db.list_threads(bot_id)]
    return s.run(go)


def _drain(s):
    return s.run(lambda db: firstrun.drain_guides(db, _deliver(db)))


def test_a_new_user_atlas_opens_with_the_guides(scratch, on):
    s = scratch
    s.connection()
    s.admin()
    u = s.profile("Bram")
    bot_id = profiles_service.spawn_atlas(u.id)
    assert len(_drain(s)) == 2
    titles = sorted(t for t, owner in _threads(s, bot_id) if owner == u.id)
    assert titles == ["Meet Atlas", "Using DisPatch"]
    assert _drain(s) == [], "the queue is empty once posted"


def test_an_admin_atlas_also_gets_the_setup_tour(scratch, on):
    s = scratch
    s.connection()
    s.admin()
    a = s.profile("Cy", Role.ADMIN)
    bot_id = profiles_service.spawn_atlas(a.id)
    _drain(s)
    assert {t for t, _ in _threads(s, bot_id)} == {"Meet Atlas", "Using DisPatch", "How DisPatch works"}


def test_a_repeat_never_posts_twice_and_reset_gets_fresh_guides(scratch, on):
    s = scratch
    s.connection()
    s.admin()
    u = s.profile("Bram")
    bot_id = profiles_service.spawn_atlas(u.id)
    _drain(s)
    firstrun.request_guides(u.id, bot_id)               # same Atlas again: a no-op
    assert _drain(s) == []
    prev = profiles_service.reset_preview(u.id)
    profiles_service.reset_profile(u.id, prev["token"])
    fresh = next(b.id for b in config.load_bots() if b.owner_profile == u.id)
    assert fresh != bot_id and len(_drain(s)) == 2


def test_a_queued_atlas_that_is_gone_is_dropped(scratch, on):
    s = scratch
    s.admin()
    u = s.profile("Bram")
    firstrun.request_guides(u.id, "p000000000000")
    assert _drain(s) == []
    assert not (config.DATA_DIR / firstrun.PENDING_NAME).exists()


def test_switched_off_queues_nothing(scratch, on, monkeypatch):
    monkeypatch.setenv("DISPATCH_ATLAS_GUIDES", "0")
    s = scratch
    s.connection()
    s.admin()
    u = s.profile("Bram")
    profiles_service.spawn_atlas(u.id)
    assert not (config.DATA_DIR / firstrun.PENDING_NAME).exists()


def test_the_backfill_queues_existing_copies_once(scratch, on, monkeypatch):
    s = scratch
    s.connection()
    s.admin()
    u = s.profile("Bram")
    monkeypatch.setenv("DISPATCH_ATLAS_GUIDES", "0")
    bot_id = profiles_service.spawn_atlas(u.id)         # made before the guides existed
    monkeypatch.setenv("DISPATCH_ATLAS_GUIDES", "1")
    assert firstrun.backfill_guides_once() == 1
    assert len(_drain(s)) == 2 and len(_threads(s, bot_id)) == 2
    assert firstrun.backfill_guides_once() == 0


# ---- "What's new" ----------------------------------------------------------- #

def _notes(s, version):
    return s.run(lambda db: firstrun.post_release_notes(db, _deliver(db), version))


def test_every_shipped_version_has_its_notes():
    assert firstrun.release_notes_file(__version__).is_file()


def test_an_update_posts_whats_new_once_per_admin_and_user(scratch, on, monkeypatch):
    s = scratch
    s.connection()
    owner = s.admin()
    u = s.profile("Bram")
    b = s.profile("Dee", Role.BASIC)
    user_atlas = profiles_service.spawn_atlas(u.id)
    s.run(lambda db: db.create_thread("pack_otter", title="old", owner_profile_id=owner.id))
    # the household Atlas the protected Admin talks to
    config.upsert_bot(firstrun.atlas_template())
    made = _notes(s, __version__)
    assert len(made) == 2
    assert [o for _, o in _threads(s, user_atlas)] == [u.id]
    assert [o for _, o in _threads(s, firstrun.BOT_ID)] == [owner.id]
    assert all(o != b.id for _, o in _threads(s, firstrun.BOT_ID))
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
    s.run(lambda db: db.create_thread("pack_otter", title="old", owner_profile_id=owner.id))
    config.upsert_bot(firstrun.atlas_template())
    assert _notes(s, "0.0.1") == []


# ---- role changes, retries, the on-demand guide, What's new without a copy ---------------- #

def _atlas_ids(pid):
    return [b.id for b in config.load_bots() if b.owner_profile == pid and b.pack == firstrun.ATLAS_PACK]


def test_basic_made_user_gets_an_atlas_with_the_guides_and_user_made_admin_gets_the_tour(scratch, on):
    s = scratch
    s.connection()
    s.admin()
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
    s.run(lambda db: firstrun.drain_guides(db, flaky(db)))
    assert len(_threads(s, bot_id)) >= 1
    assert (config.DATA_DIR / firstrun.PENDING_NAME).exists(), "kept for another try"
    _drain(s)
    titles = sorted(t for t, _ in _threads(s, bot_id))
    assert titles.count("Meet Atlas") == 1 and titles.count("Using DisPatch") == 1
    assert not (config.DATA_DIR / firstrun.PENDING_NAME).exists()


def test_a_user_without_an_own_atlas_gets_whats_new_on_the_household_one_in_their_own_chats(scratch, on):
    s = scratch
    owner = s.admin()
    u = s.profile("Bram")                                # made before 4.0: no copy of its own
    for bid in _atlas_ids(u.id):
        config.remove_bot(bid)
    s.run(lambda db: db.create_thread("pack_otter", title="old", owner_profile_id=owner.id))
    config.upsert_bot(firstrun.atlas_template())
    _notes(s, __version__)
    owners = [o for _, o in _threads(s, firstrun.BOT_ID)]
    assert sorted(owners) == sorted([owner.id, u.id])


def test_a_patch_release_with_notes_names_its_full_version(scratch, on, monkeypatch, tmp_path):
    s = scratch
    owner = s.admin()
    s.run(lambda db: db.create_thread("pack_otter", title="old", owner_profile_id=owner.id))
    config.upsert_bot(firstrun.atlas_template())
    (tmp_path / "whats-new-9.9.1.md").write_text("Fixes.", encoding="utf-8")
    monkeypatch.setattr(firstrun, "GUIDE_DIR", tmp_path)
    assert len(_notes(s, "9.9.1")) == 1
    assert [t for t, _ in _threads(s, firstrun.BOT_ID)] == ["What's new in DisPatch 9.9.1"]


def test_the_on_demand_guide_lands_in_the_callers_own_chats(scratch, on):
    s = scratch
    s.connection()
    owner = s.admin()
    config.upsert_bot(firstrun.atlas_template())
    a = s.profile("Cy", Role.ADMIN)
    mine = profiles_service.spawn_atlas(a.id)
    tid = s.run(lambda db: firstrun.seed_one(db, _deliver(db), "set-up-add-ons", profile_id=a.id))
    assert ("Set up add-ons", a.id) in _threads(s, mine)
    with pytest.raises(firstrun.GuideExists):
        s.run(lambda db: firstrun.seed_one(db, _deliver(db), "set-up-add-ons", profile_id=a.id))
    # the main Admin (no copy of her own) gets hers on the household Atlas, separately
    s.run(lambda db: firstrun.seed_one(db, _deliver(db), "set-up-add-ons", profile_id=owner.id))
    assert ("Set up add-ons", owner.id) in _threads(s, firstrun.BOT_ID)
    assert tid


def test_whats_new_is_posted_after_the_guides_so_it_sits_on_top():
    import inspect

    from app import main
    src = inspect.getsource(main._lifespan_startup)
    assert src.index("fresh_install = await firstrun.count_threads(db)") < src.index("seed_if_first_run")
    assert src.index("drain_guides") < src.index("post_release_notes(db, _deliver_assistant_text, fresh=fresh_install)")


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


def test_the_main_admin_on_the_household_atlas_gets_the_current_guides_once_after_an_upgrade(scratch, on):
    s = scratch
    owner = s.admin()
    config.upsert_bot(firstrun.atlas_template())
    # 3.x left an old tour there (not from the first-run seed, so no firstrun: source id)
    s.run(lambda db: db.create_thread(firstrun.BOT_ID, title="How DisPatch works", owner_profile_id=owner.id))
    made = s.run(lambda db: firstrun.backfill_house_guides(db, _deliver(db)))
    assert len(made) == 2
    titles = sorted(t for t, o in _threads(s, firstrun.BOT_ID) if o == owner.id)
    assert titles == ["How DisPatch works", "How DisPatch works", "Using DisPatch"]
    assert s.run(lambda db: firstrun.backfill_house_guides(db, _deliver(db))) == []


def test_a_fresh_install_seeds_using_dispatch_too_and_the_backfill_adds_nothing(scratch, on, monkeypatch):
    s = scratch
    config.upsert_bot(firstrun.atlas_template())
    monkeypatch.setattr(firstrun, "flag_path", lambda: config.DATA_DIR / ".firstrun-test-flag")
    seeded = s.run(lambda db: firstrun.seed_if_first_run(db, _deliver(db)))
    assert len(seeded) == len(firstrun.THREADS) == 4
    s.admin()
    assert s.run(lambda db: firstrun.backfill_house_guides(db, _deliver(db))) == []
