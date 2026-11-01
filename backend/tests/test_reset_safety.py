"""Reset / delete safety (DisPatch 4.2, BL-2): an agent bot that lives in the Owner's Mine
survives Reset and Delete, and a template's copies survive its deletion as plain own bots."""
from __future__ import annotations

import pytest
from profiles_fixtures import scratch

from app import characters, config, profiles_service
from app.profiles_service import ProfileServiceError

PNG = b"\x89PNG\r\n\x1a\n"


def _owner_with_bits(s):
    s.connection()
    owner = s.admin()
    bits = config.upsert_bot(config.Bot(id="bits", name="Bits", agent="main", scope="personal",
                                        owner_profile=owner.id))
    mine = profiles_service.create_personal(owner.id, {"name": "Mine"})
    av = config.AVATAR_DIR
    av.mkdir(parents=True, exist_ok=True)
    for role in ("face", "full", "thumb"):
        (av / f"bits-{role}.png").write_bytes(PNG)
        (av / f"{mine.id}-{role}.png").write_bytes(PNG)
    s.voice_acks({"bots": {"bits": {"statement": ["Kept."]}, mine.id: {"statement": ["Gone."]}}})
    return owner, bits, mine


def test_an_owner_reset_keeps_agent_bots_with_pictures_and_voice_acks(scratch):
    s = scratch
    owner, bits, mine = _owner_with_bits(s)
    tid = s.thread(owner.id, "bits", "bits chat")
    counts = profiles_service.wipe_profile_content(owner.id)
    assert counts["bots"] == 1 and counts["chats"] == 1          # only "Mine" and the chat go
    ids = {b.id for b in s.bots()}
    assert "bits" in ids and mine.id not in ids
    names = sorted(p.name for p in config.AVATAR_DIR.iterdir())
    assert names == ["bits-face.png", "bits-full.png", "bits-thumb.png"]
    import yaml
    doc = yaml.safe_load((s.root / "voice-acks.yaml").read_text())
    assert doc["bots"] == {"bits": {"statement": ["Kept."]}}
    assert s.run(lambda db: db.get_thread(tid)) is None            # its chats did go


def test_the_reset_preview_counts_what_goes_and_lists_what_is_kept(scratch):
    s = scratch
    owner, bits, mine = _owner_with_bits(s)
    p = profiles_service.reset_preview(owner.id)
    assert p["counts"]["bots"] == 1
    assert p["kept_bots"] == [{"id": "bits", "name": "Bits"}]


def test_reset_profile_keeps_bits_and_still_gives_atlas(scratch):
    s = scratch
    owner, bits, mine = _owner_with_bits(s)
    tok = profiles_service.reset_preview(owner.id)["token"]
    profiles_service.reset_profile(owner.id, tok)
    ids = {b.id for b in s.personal(owner.id)}
    assert "bits" in ids and mine.id not in ids


def test_deleting_an_agent_bot_from_mine_is_a_409_and_changes_nothing(scratch):
    s = scratch
    owner, bits, mine = _owner_with_bits(s)
    tid = s.thread(owner.id, "bits", "keep me")
    with pytest.raises(ProfileServiceError) as e:
        profiles_service.delete_personal(owner.id, "bits")
    assert e.value.code == "agent_bot_protected" and e.value.status == 409
    assert config.get_bot("bits") is not None
    assert s.run(lambda db: db.get_thread(tid)) is not None        # the chat was not deleted first
    assert (config.AVATAR_DIR / "bits-face.png").exists()
    with pytest.raises(characters.CharacterError) as e2:
        characters.remove_personal("bits", owner.id)
    assert e2.value.status == 409


def test_deleting_a_direct_personal_bot_still_works(scratch):
    s = scratch
    owner, bits, mine = _owner_with_bits(s)
    profiles_service.delete_personal(owner.id, mine.id)
    assert config.get_bot(mine.id) is None and config.get_bot("bits") is not None


def _template_with_copies(s):
    s.connection()
    a = s.admin()
    u = s.profile("Bram")
    tpl = config.get_bot("pack_otter")
    config.update_bot(tpl.id, lambda b: __import__("dataclasses").replace(b, give_to="everyone"))
    tpl = config.get_bot("pack_otter")
    copies = []
    for owner in (a, u):
        c = config.new_copy(tpl, id=f"p{owner.id[:8]}", owner_profile=owner.id)
        copies.append(config.upsert_bot(c))
    return tpl, copies


def test_deleting_a_template_detaches_its_copies(scratch):
    s = scratch
    tpl, copies = _template_with_copies(s)
    assert all(config.get_bot(c.id).template == "pack_otter" for c in copies)
    characters.remove("pack_otter")
    assert config.get_bot("pack_otter") is None
    for c in copies:
        now = config.get_bot(c.id)
        assert now is not None and now.template == "" and not now.overrides
        assert now.name == "Otter" and now.advisor == tpl.advisor        # keeps the whole brain


def test_the_template_delete_preview_counts_copies(scratch):
    s = scratch
    _template_with_copies(s)
    s.thread(None, "pack_otter", "t")
    u = profiles_service.household_bot_usage("pack_otter")
    assert u["copies"] == 2 and u["chats"] == 1 and u["profiles"] == 1
    assert profiles_service.household_bot_usage("pack_heron")["copies"] == 0


def test_the_delete_route_needs_the_owner_and_his_confirmation_for_an_agent_bot(world):
    from app import accounts
    owner = world.create_owner()
    me = accounts.list_profiles()[0]
    config.upsert_bot(config.Bot(id="bits", name="Bits", agent="main", scope="personal", owner_profile=me.id))
    r = owner.delete("/api/me/characters/bits")
    assert r.status_code == 409, r.text
    assert "confirm_required" in r.text
    assert config.get_bot("bits") is not None
    assert owner.delete("/api/me/characters/bits?confirm=true").status_code == 200
    assert config.get_bot("bits") is None
