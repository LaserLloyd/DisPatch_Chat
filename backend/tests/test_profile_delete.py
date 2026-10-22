"""Deleting a profile, the lock target, the default profile and the counts (service level)."""
from __future__ import annotations

import json

import pytest
from profiles_fixtures import scratch

from app import accounts, auth, ext_profile_hooks, firstrun, profiles_service, push
from app.contracts.permissions import Role
from app.profiles_service import ProfileServiceError


def _delete(pid):
    return profiles_service.delete_profile(pid, profiles_service.delete_preview(pid)["token"])


def test_deleting_a_profile_deletes_everything_it_owned_including_its_file_server_drops(scratch):
    s = scratch
    s.connection()
    s.admin()
    u, keep = s.profile("Bram"), s.profile("Cy")
    purged = []
    ext_profile_hooks.register_profile_data("voice", count=lambda pid: {"voices": 1},
                                            purge=lambda pid: purged.append(pid) or 1)
    bot = profiles_service.create_personal(u.id, {"name": "SENT-Z-bot"})
    s.thread(u.id, bot.id, "SENT-Z-chat")
    s.file_row(u.id, "SENT-Z-drop.txt", b"SENT-Z-bytes", source="fileserver")
    s.file_row(keep.id, "KEEP-drop.txt", b"KEEP-bytes", source="fileserver")
    deleted = _delete(u.id)
    assert deleted["chats"] == 1 and deleted["bots"] == 1 and deleted["voices"] == 1
    assert purged == [u.id]
    assert accounts.get_profile(u.id) is None
    gone = s.grep("SENT-Z-bot", "SENT-Z-chat", "SENT-Z-drop.txt", "SENT-Z-bytes")
    assert all(v == [] for v in gone.values()), gone
    assert s.grep("KEEP-bytes")["KEEP-bytes"]


def test_a_deleted_profiles_push_subscriptions_go(scratch, monkeypatch):
    s = scratch
    s.admin()
    u, keep = s.profile("Bram"), s.profile("Cy")
    subs = {"dev1": {"endpoint": "https://push.example/1", "profile_id": u.id},
            "dev2": {"endpoint": "https://push.example/2", "profile_id": keep.id}}
    monkeypatch.setattr(push, "_load_subs", lambda: dict(subs))
    removed = []
    monkeypatch.setattr(push, "unsubscribe", lambda key: removed.append(key) or True)
    _delete(u.id)
    assert removed == ["dev1"]


def test_the_protected_admin_cannot_be_deleted(scratch):
    s = scratch
    admin = s.admin()
    for call in (lambda: profiles_service.delete_preview(admin.id),
                 lambda: profiles_service.delete_profile(admin.id, "x")):
        with pytest.raises(ProfileServiceError) as e:
            call()
        assert e.value.code == "owner_immutable"
    assert accounts.get_profile(admin.id) is not None


def test_deleting_needs_the_token_and_deletes_nothing_without_it(scratch):
    s = scratch
    s.admin()
    u = s.profile("Bram")
    s.thread(u.id)
    with pytest.raises(ProfileServiceError) as e:
        profiles_service.delete_profile(u.id, "")
    assert e.value.code == "delete_stale" and e.value.status == 409
    assert accounts.get_profile(u.id) is not None and profiles_service.content_counts(u.id)["chats"] == 1


def test_deleting_the_lock_target_sends_the_lock_back_to_the_profile_list(scratch):
    s = scratch
    s.connection()
    s.admin()
    d = profiles_service.ensure_default_profile()
    assert profiles_service.lock_target_profile_id() == d.id
    _delete(d.id)
    assert profiles_service.lock_target_profile_id() is None
    cfg = auth.load()
    assert cfg.lock_target == "profile_list" and not cfg.default_profile


def test_deleting_another_profile_leaves_the_lock_target_alone(scratch):
    s = scratch
    s.connection()
    s.admin()
    d = profiles_service.ensure_default_profile()
    _delete(s.profile("Bram").id)
    assert profiles_service.lock_target_profile_id() == d.id


# ---- the default profile ------------------------------------------------------ #

def test_the_default_profile_is_a_user_with_no_pin_its_own_atlas_and_is_the_lock_target(scratch):
    s = scratch
    s.connection()
    s.admin()
    d = profiles_service.ensure_default_profile()
    assert d.name == firstrun.DEFAULT_PROFILE_NAME == "DisPatch"
    assert d.role is Role.USER and not d.has_pin and not d.has_password and d.household_bots == "all"
    assert len(s.personal(d.id)) == 1 and profiles_service.has_atlas(d.id)
    cfg = auth.load()
    assert cfg.lock_target == "default_profile" and cfg.default_profile == d.id


def test_ensure_default_is_idempotent_and_respects_a_later_choice(scratch):
    s = scratch
    s.connection()
    s.admin()
    d1 = profiles_service.ensure_default_profile()
    d2 = profiles_service.ensure_default_profile()
    assert d1.id == d2.id and len(s.personal(d1.id)) == 1
    profiles_service.set_lock_target(None)
    profiles_service.ensure_default_profile()
    assert profiles_service.lock_target_profile_id() is None          # not put back


def test_an_existing_profile_with_that_name_is_reused_and_gets_a_missing_atlas(scratch):
    s = scratch
    s.connection()
    s.admin()
    existing = s.profile("DisPatch")
    d = profiles_service.ensure_default_profile()
    assert d.id == existing.id and profiles_service.has_atlas(d.id)
    assert profiles_service.lock_target_profile_id() is None          # it was not created here


def test_set_lock_target_round_trips(scratch):
    s = scratch
    s.admin()
    u = s.profile("Bram")
    profiles_service.set_lock_target(u.id)
    assert profiles_service.lock_target_profile_id() == u.id
    profiles_service.set_lock_target(None)
    assert profiles_service.lock_target_profile_id() is None


# ---- counts and usage ---------------------------------------------------------- #

def test_counts_are_numbers_for_this_profile_only(scratch):
    s = scratch
    s.connection()
    admin = s.admin()
    a, b = s.profile("Bram"), s.profile("Cy")
    s.media("p1.png", b"x")
    s.thread(a.id, messages=[("assistant", "see /media/p1.png", "/media/p1.png"), ("user", "again /media/p1.png")])
    s.thread(a.id)
    s.thread(b.id)
    s.thread(None)                                         # reads as the protected Admin's
    profiles_service.create_personal(a.id, {"name": "x"})
    ext_profile_hooks.register_profile_data("voice", count=lambda pid: {"voices": 4} if pid == a.id else {})
    assert profiles_service.content_counts(a.id) == {"bots": 1, "chats": 2, "pictures": 1, "voices": 4}
    assert profiles_service.content_counts(b.id) == {"bots": 0, "chats": 1, "pictures": 0, "voices": 0}
    assert profiles_service.content_counts(admin.id)["chats"] == 1


def test_a_failing_voice_counter_does_not_break_the_counts(scratch):
    s = scratch
    s.admin()
    u = s.profile("Bram")
    ext_profile_hooks.register_profile_data("voice", count=lambda pid: (_ for _ in ()).throw(RuntimeError("x")))
    assert profiles_service.content_counts(u.id)["voices"] == 0


def test_household_bot_usage_counts_chats_and_profiles(scratch):
    s = scratch
    s.admin()
    a, b = s.profile("Bram"), s.profile("Cy")
    s.thread(a.id, "pack_otter")
    s.thread(a.id, "pack_otter")
    s.thread(b.id, "pack_otter")
    s.thread(b.id, "pack_heron")
    assert profiles_service.household_bot_usage("pack_otter") == {"chats": 3, "profiles": 2}
    assert profiles_service.household_bot_usage("nobody") == {"chats": 0, "profiles": 0}


def test_a_profile_id_that_is_not_an_id_is_refused_before_it_touches_a_path(scratch):
    scratch.admin()
    for bad in ("../x", "a/b", "", " ", "x" * 80):
        with pytest.raises(ProfileServiceError) as e:
            profiles_service.wipe_profile_content(bad)
        assert e.value.status == 404


def test_export_has_only_the_profiles_own_chats_and_never_a_key(scratch):
    s = scratch
    c = s.connection()
    s.admin()
    a, b = s.profile("Bram"), s.profile("Cy")
    bot = profiles_service.create_personal(a.id, {"name": "Mine", "persona": "Be brief."})
    s.thread(a.id, bot.id, "mine", messages=[("user", "hello"), ("assistant", "hi there", "/media/x.png")])
    s.thread(b.id, "pack_otter", "theirs", messages=[("user", "SENT-E-theirs")])
    doc = json.loads("".join(profiles_service.export_chunks(a.id)))
    assert doc["profile"]["id"] == a.id
    assert [t["title"] for t in doc["threads"]] == ["mine"]
    assert [(m["role"], m["content"]) for m in doc["threads"][0]["messages"]] == [("user", "hello"), ("assistant", "hi there")]
    assert doc["bots"][0]["name"] == "Mine"
    text = json.dumps(doc)
    assert "SENT-E-theirs" not in text and c.id not in text and "api_key" not in text
    assert doc["threads"][0]["messages"][1]["media_url"] == "/media/x.png"


def test_the_pictures_a_chat_shows_are_counted_once_each(scratch):
    s = scratch
    s.admin()
    u = s.profile("Bram")
    s.ledger([{"id": "1" * 32, "url": "/media/s1.png", "owner_profile_id": u.id}])
    s.thread(u.id, messages=[("assistant", "/media/s1.png and /media/s2.png", "/media/s2.png")])
    assert profiles_service.content_counts(u.id)["pictures"] == 2          # s1 (studio + chat) and s2
