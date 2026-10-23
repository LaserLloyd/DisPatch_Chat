"""Every Atlas copy opens with the guide chats, and an update posts one "What's new" chat per
Admin and User profile (app/firstrun.py). Neutral fixtures only."""
from __future__ import annotations

import pytest

from app import __version__, config, firstrun, profiles_service
from app.contracts.permissions import Role
from tests.profiles_fixtures import scratch  # noqa: F401  (fixture)


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
