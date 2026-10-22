"""Copying a household bot into a profile (4.0 chat-first profiles, SPEC §4)."""
from __future__ import annotations

import pytest
from profiles_fixtures import scratch
from test_personal_bots import voices

from app import accounts, characters, config, policy, profiles_service
from app.config import Bot
from app.contracts.permissions import Role
from app.profiles_service import ProfileServiceError

PNG = b"\x89PNG\r\n\x1a\n"


def _principal(profile):
    return policy.profile_principal(profile)


def _household_with_setup(s, **extra):
    """A household bot with a personality, a picture, a description and chats."""
    c = s.connection(name="Household")
    base = config.get_bot("pack_otter")
    from dataclasses import replace
    bot = replace(base, description="Quick and curious", color="#aa3300",
                  avatar="pack_otter-face.png", **extra,
                  advisor={**base.advisor, "persona": "You are Otter.", "knowledge_dir": "/srv/private-notes",
                           "tools_picked": ["weather"], "providers": [
                               {"provider": "openai_compatible", "base_url": c.base_url, "model": "m1",
                                "connection": c.id, "api_key": "SENT-KEY-do-not-copy"}]})
    config.upsert_bot(bot)
    s.snapshot  # noqa: B018 - the avatar files live in AVATAR_DIR, not the snapshot store
    config.AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    for role in ("face", "full", "thumb"):
        (config.AVATAR_DIR / f"pack_otter-{role}.png").write_bytes(PNG + role.encode())
    return c, config.get_bot("pack_otter")


def test_a_user_copies_a_household_bots_setup_and_nothing_else(scratch):
    s = scratch
    s.admin()
    u = s.profile("Bram")
    c, src = _household_with_setup(s)
    s.thread(accounts.get_protected_admin().id, "pack_otter", "SENT-C-household-chat")
    out = profiles_service.clone_to_own("pack_otter", _principal(u))
    row = out["bot"]
    assert out["voice_cleared"] is False
    copy = config.get_bot(row["id"])
    assert copy.scope == "personal" and copy.owner_profile == u.id and copy.cloned_from == "pack_otter"
    assert copy.name == "Otter" and copy.description == "Quick and curious" and copy.color == "#aa3300"
    assert copy.advisor["persona"] == "You are Otter."
    assert characters.connection_of(copy) == c.id and characters.model_of(copy) == "m1"
    # never: knowledge folder, lookups configuration, agents, pools, jobs, keys
    adv = copy.advisor
    assert "knowledge_dir" not in adv and "tools_picked" not in adv and adv["tools"] is True
    assert "api_key" not in adv["providers"][0]
    assert not copy.agent and not copy.reactions and not copy.avatar_pool and not copy.image_jobs and not copy.api
    # the picture is copied under the new id, the original untouched
    assert copy.avatar == f"{copy.id}-face.png"
    assert [(config.AVATAR_DIR / f"{copy.id}-{r}.png").read_bytes() for r in ("face", "full", "thumb")] == [
        PNG + b"face", PNG + b"full", PNG + b"thumb"]
    assert (config.AVATAR_DIR / "pack_otter-face.png").exists()
    # chats are never copied, and the household bot is unchanged
    assert profiles_service.content_counts(u.id)["chats"] == 0
    assert s.grep("SENT-KEY-do-not-copy")["SENT-KEY-do-not-copy"] == ["config.yaml"]       # still the source's, once
    assert config.get_bot("pack_otter").scope == "household"


def test_a_default_personality_is_not_copied_as_custom_text(scratch):
    s = scratch
    s.connection()
    s.admin()
    u = s.profile("Bram")
    out = profiles_service.clone_to_own("pack_heron", _principal(u))
    assert "persona" not in config.get_bot(out["bot"]["id"]).advisor


def test_agent_and_legacy_api_bots_cannot_be_copied(scratch):
    s = scratch
    s.admin()
    config.upsert_bot(Bot(id="legacy", name="Legacy", api={"provider": "openai_compatible", "base_url": "http://x/v1",
                                                         "model": "m", "api_key": "k"}))
    u = s.profile("Bram")
    for bot_id in ("helper", "legacy"):
        with pytest.raises(ProfileServiceError) as e:
            profiles_service.clone_to_own(bot_id, _principal(u))
        assert e.value.code == "not_copyable" and e.value.status == 422
    assert s.personal(u.id) == []


def test_only_a_household_bot_can_be_the_source(scratch):
    s = scratch
    s.connection()
    s.admin()
    a, b = s.profile("Bram"), s.profile("Cy")
    theirs = profiles_service.create_personal(a.id, {"name": "Private"})
    for bot_id in (theirs.id, "no-such-bot"):
        with pytest.raises(ProfileServiceError) as e:
            profiles_service.clone_to_own(bot_id, _principal(b))
        assert e.value.status == 404


def test_a_bot_the_profile_may_not_use_cannot_be_copied(scratch, monkeypatch):
    s = scratch
    s.admin()
    u = s.profile("Bram")
    monkeypatch.setattr(characters, "may_use", lambda bot, principal: False)
    with pytest.raises(ProfileServiceError) as e:
        profiles_service.clone_to_own("pack_otter", _principal(u))
    assert e.value.status == 404


def test_a_basic_profile_cannot_copy(scratch):
    s = scratch
    s.admin()
    b = s.profile("Dee", Role.BASIC)
    with pytest.raises(ProfileServiceError) as e:
        profiles_service.clone_to_own("pack_otter", _principal(b))
    assert e.value.code == "basic_no_bots"


def test_nobody_signed_in_cannot_copy(scratch):
    scratch.admin()
    with pytest.raises(ProfileServiceError) as e:
        profiles_service.clone_to_own("pack_otter", policy.open_principal())
    assert e.value.status == 403


def test_the_copy_keeps_the_voice_only_if_the_target_may_use_it(scratch, voices):
    from dataclasses import replace
    s = scratch
    s.connection()
    s.admin()
    u, other = s.profile("Bram"), s.profile("Cy")
    voices["clone-x"] = other.id                            # somebody else's clone
    config.upsert_bot(replace(config.get_bot("pack_otter"), voice="stock"))
    config.upsert_bot(replace(config.get_bot("pack_heron"), voice="clone-x"))
    kept = profiles_service.clone_to_own("pack_otter", _principal(u))
    assert config.get_bot(kept["bot"]["id"]).voice == "stock" and kept["voice_cleared"] is False
    cleared = profiles_service.clone_to_own("pack_heron", _principal(u))
    assert config.get_bot(cleared["bot"]["id"]).voice == "" and cleared["voice_cleared"] is True
    mine = profiles_service.clone_to_own("pack_heron", _principal(other))        # their own clone: kept
    assert config.get_bot(mine["bot"]["id"]).voice == "clone-x" and mine["voice_cleared"] is False


def test_without_the_voice_addon_the_voice_is_cleared_not_an_error(scratch):
    from dataclasses import replace
    s = scratch
    s.connection()
    s.admin()
    u = s.profile("Bram")
    config.upsert_bot(replace(config.get_bot("pack_otter"), voice="stock"))
    out = profiles_service.clone_to_own("pack_otter", _principal(u))
    assert out["voice_cleared"] is True and config.get_bot(out["bot"]["id"]).voice == ""


def test_the_copy_falls_back_to_a_connection_the_profile_may_use(scratch):
    s = scratch
    c1, src = _household_with_setup(s)
    c2 = s.connection(default=False, name="Other", model="m2")
    s.admin()
    u = s.profile("Bram")
    accounts.update_profile(u.id, backends={"allowed": [c2.id]})
    out = profiles_service.clone_to_own("pack_otter", _principal(u))
    copy = config.get_bot(out["bot"]["id"])
    assert characters.connection_of(copy) == c2.id and characters.model_of(copy) == "m2"


# ---- Admin copies into a profile ------------------------------------------ #

def test_an_admin_copies_into_a_profile_and_cannot_see_the_copy(scratch):
    s = scratch
    admin = s.admin()
    u = s.profile("Bram")
    _household_with_setup(s)
    out = profiles_service.clone_to_profile("pack_otter", u.id, actor=_principal(admin))
    assert set(out) == {"bot_id", "voice_cleared"}
    copy = config.get_bot(out["bot_id"])
    assert copy.owner_profile == u.id and copy.cloned_from == "pack_otter"
    with pytest.raises(characters.CharacterError) as e:
        characters.get_personal(out["bot_id"], admin.id)
    assert e.value.status == 404
    with pytest.raises(characters.CharacterError):
        characters.patch(out["bot_id"], {"name": "x"})            # the household surface cannot either
    assert profiles_service.content_counts(u.id)["chats"] == 0     # chats are never copied


def test_only_an_admin_may_copy_into_a_profile_and_only_into_one_that_can_have_bots(scratch):
    s = scratch
    admin = s.admin()
    u, b = s.profile("Bram"), s.profile("Dee", Role.BASIC)
    with pytest.raises(ProfileServiceError) as e:
        profiles_service.clone_to_profile("pack_otter", u.id, actor=_principal(u))
    assert e.value.status == 403
    with pytest.raises(ProfileServiceError) as e:
        profiles_service.clone_to_profile("pack_otter", b.id, actor=_principal(admin))
    assert e.value.code == "basic_no_bots"
    with pytest.raises(ProfileServiceError) as e:
        profiles_service.clone_to_profile("helper", u.id, actor=_principal(admin))
    assert e.value.code == "not_copyable"
    with pytest.raises(ProfileServiceError) as e:
        profiles_service.clone_to_profile("pack_otter", "nobody", actor=_principal(admin))
    assert e.value.status == 404
