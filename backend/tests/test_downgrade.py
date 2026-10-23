"""Role changes (4.0 chat-first profiles, BUILD-PLAN-v2 §1.3): User <-> Basic, service level.

User -> Basic is the one change that deletes: every private bot and every chat, and the cloned
voices. Basic -> User deletes nothing. The protected Admin never changes.
"""
from __future__ import annotations

import pytest
from profiles_fixtures import scratch

from app import accounts, ext_profile_hooks, profiles_service
from app.contracts.permissions import Role
from app.profiles_service import ProfileServiceError


def _downgrade(pid, **kw):
    prev = profiles_service.change_role_preview(pid, "basic")
    return profiles_service.change_role(pid, "basic", confirm_token=prev["token"], **kw)


def test_user_to_basic_deletes_the_bots_chats_and_clones_and_keeps_the_profile(scratch):
    s = scratch
    s.connection()
    s.admin()
    u = s.profile("Bram", pin_hash="x" * 20)
    purged = []
    ext_profile_hooks.register_profile_data("voice", count=lambda pid: {"voices": 3},
                                            purge=lambda pid: purged.append(pid) or 3)
    bot = profiles_service.create_personal(u.id, {"name": "Mine", "persona": "SENT-D-persona"})
    s.thread(u.id, bot.id, "SENT-D-chat", messages=[("user", "SENT-D-msg")])
    s.thread(u.id, "pack_otter", "SENT-D-chat2")

    preview = profiles_service.change_role_preview(u.id, "basic")
    assert preview["counts"] == {"bots": 1, "chats": 2, "pictures": 0, "voices": 3, "files": 0} and preview["deletes"]

    out = profiles_service.change_role(u.id, "basic", confirm_token=preview["token"],
                                       household_bots=["pack_otter"])
    assert out.role is Role.BASIC and out.name == "Bram" and out.pin_hash == "x" * 20      # name and PIN kept
    assert out.household_bots == ["pack_otter"]
    assert s.personal(u.id) == []
    assert profiles_service.content_counts(u.id)["chats"] == 0
    assert purged == [u.id]                                  # the clones went through the purge hook
    assert s.grep("SENT-D-persona", "SENT-D-chat", "SENT-D-msg") == {
        "SENT-D-persona": [], "SENT-D-chat": [], "SENT-D-msg": []}


def test_without_a_matching_token_nothing_is_deleted(scratch):
    s = scratch
    s.connection()
    s.admin()
    u = s.profile("Bram")
    s.thread(u.id)
    for token in (None, "", "junk"):
        with pytest.raises(ProfileServiceError) as e:
            profiles_service.change_role(u.id, "basic", confirm_token=token)
        assert e.value.status == 409 and e.value.code == "role_change_stale"
    assert accounts.get_profile(u.id).role is Role.USER and profiles_service.content_counts(u.id)["chats"] == 1


def test_a_token_for_another_role_or_profile_does_not_open_the_downgrade(scratch):
    s = scratch
    s.admin()
    a, b = s.profile("Bram"), s.profile("Cy")
    ta = profiles_service.change_role_preview(a.id, "basic")["token"]
    with pytest.raises(ProfileServiceError):
        profiles_service.change_role(b.id, "basic", confirm_token=ta)
    reset_token = profiles_service.reset_preview(a.id)["token"]
    with pytest.raises(ProfileServiceError):
        profiles_service.change_role(a.id, "basic", confirm_token=reset_token)
    assert accounts.get_profile(a.id).role is Role.USER


def test_a_change_after_the_preview_makes_the_token_stale(scratch):
    s = scratch
    s.admin()
    u = s.profile("Bram")
    s.thread(u.id)
    token = profiles_service.change_role_preview(u.id, "basic")["token"]
    s.thread(u.id, title="more")
    with pytest.raises(ProfileServiceError) as e:
        profiles_service.change_role(u.id, "basic", confirm_token=token)
    assert e.value.code == "role_change_stale"


def test_basic_to_user_keeps_everything_and_needs_no_token(scratch):
    s = scratch
    s.admin()
    b = s.profile("Dee", Role.BASIC, household_bots=["pack_otter"])
    s.thread(b.id, "pack_otter", "kept chat")
    preview = profiles_service.change_role_preview(b.id, "user")
    assert preview == {"counts": {}, "token": "", "expires_in": profiles_service.TOKEN_TTL_SECONDS,
                       "deletes": False}
    out = profiles_service.change_role(b.id, "user")
    assert out.role is Role.USER and profiles_service.content_counts(b.id)["chats"] == 1
    assert out.household_bots == ["pack_otter"]


def test_user_to_admin_and_back_changes_the_role_only(scratch):
    s = scratch
    s.connection()
    s.admin()
    u = s.profile("Bram")
    bot = profiles_service.create_personal(u.id, {"name": "Mine"})
    assert profiles_service.change_role(u.id, "admin").role is Role.ADMIN
    assert profiles_service.change_role(u.id, "user").role is Role.USER
    assert [b.id for b in s.personal(u.id)] == [bot.id]


def test_admin_to_basic_deletes_too(scratch):
    s = scratch
    s.connection()
    s.admin()
    a2 = s.profile("Eve", Role.ADMIN)
    s.thread(a2.id)
    out = _downgrade(a2.id)
    assert out.role is Role.BASIC and profiles_service.content_counts(a2.id)["chats"] == 0


def test_the_protected_admin_cannot_be_demoted_and_nothing_is_deleted(scratch):
    s = scratch
    admin = s.admin()
    s.thread(admin.id)
    for call in (lambda: profiles_service.change_role_preview(admin.id, "basic"),
                 lambda: profiles_service.change_role(admin.id, "user"),
                 lambda: profiles_service.change_role(admin.id, "basic", confirm_token="x")):
        with pytest.raises(ProfileServiceError) as e:
            call()
        assert e.value.code == "owner_immutable" and e.value.status == 409
    assert profiles_service.change_role(admin.id, "admin").protected
    assert profiles_service.content_counts(admin.id)["chats"] == 1


def test_a_bad_household_list_is_refused_before_anything_is_deleted(scratch):
    s = scratch
    s.admin()
    u = s.profile("Bram")
    s.thread(u.id)
    token = profiles_service.change_role_preview(u.id, "basic")["token"]
    with pytest.raises(ProfileServiceError) as e:
        profiles_service.change_role(u.id, "basic", confirm_token=token, household_bots=["nope"])
    assert e.value.code == "household_invalid"
    assert profiles_service.content_counts(u.id)["chats"] == 1 and accounts.get_profile(u.id).role is Role.USER
    # the same token still works with a good list: the failed attempt did not spend it
    out = profiles_service.change_role(u.id, "basic", confirm_token=token, household_bots=["pack_heron"])
    assert out.household_bots == ["pack_heron"]


def test_a_personal_bot_id_is_not_a_household_bot(scratch):
    s = scratch
    s.connection()
    s.admin()
    other = s.profile("Cy")
    theirs = profiles_service.create_personal(other.id, {"name": "Private"})
    u = s.profile("Bram")
    token = profiles_service.change_role_preview(u.id, "basic")["token"]
    with pytest.raises(ProfileServiceError) as e:
        profiles_service.change_role(u.id, "basic", confirm_token=token, household_bots=[theirs.id])
    assert e.value.code == "household_invalid"


def test_without_a_pick_the_new_basic_profile_gets_the_bots_it_can_use(scratch):
    s = scratch
    s.admin()
    u = s.profile("Bram")                                   # household_bots "all"
    out = _downgrade(u.id)
    assert out.household_bots == ["pack_otter", "pack_heron"]          # direct bots; not the agent bot
    kept = s.profile("Fay", household_bots=["pack_heron"])
    assert _downgrade(kept.id).household_bots == ["pack_heron"]        # an explicit list is kept


def test_without_a_pick_a_hidden_household_bot_is_not_handed_out(scratch):
    """A hidden household bot is off every rail on purpose: a downgrade without a pick does not
    put it on the new Basic profile (an Admin who wants it there picks it by name)."""
    from app import config
    s = scratch
    s.admin()
    config.upsert_bot(config.Bot(id="pack_wren", name="Wren", hidden=True,
                                 advisor={"providers": [{"provider": "openai_compatible", "model": "m1"}]}))
    assert "pack_wren" not in profiles_service.basic_default_household()
    u = s.profile("Cleo")
    assert _downgrade(u.id).household_bots == ["pack_otter", "pack_heron"]
    picked = s.profile("Gus")
    assert "pack_wren" in _downgrade(picked.id, household_bots=["pack_wren"]).household_bots


def test_overrides_are_trimmed_to_the_basic_table(scratch):
    s = scratch
    s.admin()
    u = s.profile("Bram", grants=["dispatch_agents", "use_images"], denies=["use_voice", "files_read", "use_chat"])
    out = _downgrade(u.id)
    # dispatch_agents and use_images are both grantable to Basic; files_read is never Basic's
    assert set(out.grants) == {"dispatch_agents", "use_images"}
    assert "files_read" not in out.denies and set(out.denies) == {"use_voice", "use_chat"}


def test_the_profile_is_signed_out_when_its_role_changes(scratch, monkeypatch):
    s = scratch
    s.admin()
    u = s.profile("Bram")
    revoked = []
    from app import auth
    monkeypatch.setattr(auth, "revoke_profile", lambda pid: revoked.append(pid) or 0)
    _downgrade(u.id)
    profiles_service.change_role(u.id, "user")
    assert revoked == [u.id, u.id]


def test_an_unknown_role_is_a_422(scratch):
    s = scratch
    s.admin()
    u = s.profile("Bram")
    with pytest.raises(ProfileServiceError) as e:
        profiles_service.change_role(u.id, "owner-ish")
    assert e.value.status == 422


def test_no_other_module_changes_a_role_around_the_service():
    """User -> Basic must delete the profile's private content, so every role change in the app
    goes through ``profiles_service.change_role`` (the People PATCH refuses a role change with
    409 ``use_role_route``)."""
    import re
    from pathlib import Path
    app_dir = Path(__file__).resolve().parents[1] / "app"
    offenders = []
    for path in sorted(app_dir.glob("*.py")):
        if path.name in {"accounts.py", "profiles_service.py"}:
            continue
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(r"update_profile\(([^)]*)\)", text):
            if re.search(r"\brole\s*=", m.group(1)):
                offenders.append(path.name)
    assert offenders == []
