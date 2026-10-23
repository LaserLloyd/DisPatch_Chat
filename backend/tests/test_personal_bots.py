"""A profile's own bots (4.0 chat-first profiles): create / change / delete, Atlas, backends, voices."""
from __future__ import annotations

import re

import pytest
from profiles_fixtures import scratch

from app import accounts, backends_policy, characters, config, ext_profile_hooks, extensions, firstrun, profiles_service
from app.contracts.permissions import Role
from app.profiles_service import ProfileServiceError

ID = re.compile(r"\Ap[0-9a-f]{12}\Z")


@pytest.fixture
def voices(monkeypatch):
    """A voice add-on that knows two voices and declares per-profile clones: ``clone-x`` belongs to
    profile ``X`` (set via ``owners``), ``stock`` is the household's."""
    owners = {"clone-x": None, "stock": "household"}
    monkeypatch.setattr(extensions, "field_validator",
                        lambda name: (lambda v: None if v in owners or v.startswith("builtin:")
                                      else (_ for _ in ()).throw(ValueError("Unknown voice"))) if name == "voice" else None)
    ext_profile_hooks.register_voice_access(
        "voice", lambda voice, bot_owner: owners.get(voice) in ("household", bot_owner) if voice in owners else True)
    return owners


def test_a_personal_bot_has_an_opaque_id_belongs_to_its_owner_and_is_direct(scratch):
    s = scratch
    c = s.connection()
    s.admin()
    u = s.profile("Bram")
    bot = profiles_service.create_personal(u.id, {"name": "Mine", "emoji": "🦊", "persona": "Be brief."})
    assert ID.match(bot.id) and bot.scope == "personal" and bot.owner_profile == u.id
    assert config.is_direct(bot) and not bot.agent and not bot.reactions and not bot.avatar_pool and not bot.image_jobs
    assert characters.connection_of(bot) == c.id and characters.model_of(bot) == "m1"
    assert config.direct_tools_on(bot) and bot.advisor["persona"] == "Be brief."
    assert (config.get_bot(bot.id).owner_profile == u.id)


def test_two_bots_never_share_an_id(scratch):
    s = scratch
    s.admin()
    u = s.profile("Bram")
    ids = {profiles_service.create_personal(u.id, {"name": f"B{i}"}).id for i in range(12)}
    assert len(ids) == 12


def test_with_no_connection_yet_the_bot_is_made_unconnected(scratch):
    s = scratch
    s.admin()
    u = s.profile("Bram")
    bot = profiles_service.create_personal(u.id, {"name": "Mine"})
    assert not config.direct_connected(bot)


def test_a_connection_with_no_model_is_an_error_for_the_editor_but_not_for_the_atlas(scratch):
    s = scratch
    s.connection(model="")
    s.admin()
    u = s.profile("Bram")
    with pytest.raises(ProfileServiceError) as e:
        profiles_service.create_personal(u.id, {"name": "Mine", "connection": s.connection(default=False, model="", name="Two").id})
    assert e.value.status == 422
    atlas = profiles_service.spawn_atlas(u.id)                 # soft: left unconnected
    assert not config.direct_connected(config.get_bot(atlas))


def test_a_basic_profile_cannot_have_bots_of_its_own(scratch):
    s = scratch
    s.admin()
    b = s.profile("Dee", Role.BASIC)
    for call in (lambda: profiles_service.create_personal(b.id, {"name": "x"}),
                 lambda: profiles_service.spawn_atlas(b.id),
                 lambda: profiles_service.update_personal(b.id, "p" + "0" * 12, {"name": "y"})):
        with pytest.raises(ProfileServiceError) as e:
            call()
        assert e.value.code == "basic_no_bots"


def test_household_only_fields_are_refused_on_a_personal_bot(scratch):
    s = scratch
    s.admin()
    u = s.profile("Bram")
    for field, value in (("agent", "helper"), ("reactions", True), ("audience", "all"), ("image_jobs", True),
                         ("profiles", []), ("hidden", True), ("safe", True)):
        with pytest.raises(ProfileServiceError) as e:
            profiles_service.create_personal(u.id, {"name": "x", field: value})
        assert e.value.status == 422
    bot = profiles_service.create_personal(u.id, {"name": "ok"})
    with pytest.raises(ProfileServiceError):
        profiles_service.update_personal(u.id, bot.id, {"agent": "helper"})


def test_a_name_is_required_and_trimmed(scratch):
    s = scratch
    s.admin()
    u = s.profile("Bram")
    with pytest.raises(ProfileServiceError):
        profiles_service.create_personal(u.id, {"name": "   "})
    assert profiles_service.create_personal(u.id, {"name": "  Padded  "}).name == "Padded"


def test_the_personality_limit_and_presets(scratch):
    s = scratch
    s.admin()
    u = s.profile("Bram")
    with pytest.raises(ProfileServiceError) as e:
        profiles_service.create_personal(u.id, {"name": "x", "persona": "a" * (characters.MAX_PERSONAL_PERSONA + 1)})
    assert e.value.status == 422
    preset = next(p for p in __import__("app.personas", fromlist=["x"]).presets() if p.id != "default")
    bot = profiles_service.create_personal(u.id, {"name": "x", "personality": preset.id})
    assert bot.advisor["persona"] == preset.text
    with pytest.raises(ProfileServiceError):
        profiles_service.create_personal(u.id, {"name": "x", "personality": "no-such-preset"})


def test_only_the_owner_can_see_change_or_delete_a_bot_and_everyone_else_gets_404(scratch):
    s = scratch
    s.connection()
    admin = s.admin()
    a, b = s.profile("Bram"), s.profile("Cy")
    bot = profiles_service.create_personal(a.id, {"name": "Private"})
    for who in (b.id, admin.id):
        with pytest.raises(ProfileServiceError) as e:
            profiles_service.update_personal(who, bot.id, {"name": "Taken"})
        assert e.value.status == 404
        with pytest.raises(ProfileServiceError) as e:
            profiles_service.delete_personal(who, bot.id)
        assert e.value.status == 404
        with pytest.raises(characters.CharacterError) as e2:
            characters.get_personal(bot.id, who)
        assert e2.value.status == 404
    assert config.get_bot(bot.id).name == "Private"


def test_the_household_surface_cannot_touch_a_personal_bot(scratch):
    s = scratch
    s.connection()
    s.admin()
    a = s.profile("Bram")
    bot = profiles_service.create_personal(a.id, {"name": "Private"})
    with pytest.raises(characters.CharacterError) as e:
        characters.patch(bot.id, {"name": "Seized"})
    assert e.value.status == 404
    with pytest.raises(characters.CharacterError) as e:
        characters.remove(bot.id)
    assert e.value.status == 404
    assert config.get_bot(bot.id).name == "Private"


def test_changing_a_bot(scratch):
    s = scratch
    c = s.connection()
    s.admin()
    u = s.profile("Bram")
    bot = profiles_service.create_personal(u.id, {"name": "Mine", "persona": "old"})
    out = profiles_service.update_personal(u.id, bot.id, {"name": "Renamed", "emoji": "🐙", "persona": "new",
                                                          "tools": False, "description": "d" * 500})
    assert out.name == "Renamed" and out.emoji == "🐙" and out.advisor["persona"] == "new"
    assert not config.direct_tools_on(out) and len(out.description) == 400
    assert characters.connection_of(out) == c.id
    out = profiles_service.update_personal(u.id, bot.id, {"persona": ""})
    assert "persona" not in out.advisor


def test_deleting_a_bot_deletes_its_chats_and_nothing_else(scratch):
    s = scratch
    s.connection()
    s.admin()
    u = s.profile("Bram")
    bot, keep = (profiles_service.create_personal(u.id, {"name": n}) for n in ("Gone", "Stay"))
    s.thread(u.id, bot.id, "SENT-X-gone", messages=[("user", "SENT-X-gone-msg")])
    s.thread(u.id, bot.id, "SENT-X-gone2")
    s.thread(u.id, keep.id, "SENT-X-stay")
    s.thread(u.id, "pack_otter", "SENT-X-household")
    listed = {r["id"]: r["chats"] for r in profiles_service.list_personal(u.id)}
    assert listed == {bot.id: 2, keep.id: 1}
    gone = profiles_service.delete_personal(u.id, bot.id)
    assert gone["chats"] == 2
    assert [b.id for b in s.personal(u.id)] == [keep.id]
    assert profiles_service.content_counts(u.id)["chats"] == 2
    found = s.grep("SENT-X-gone", "SENT-X-gone-msg", "SENT-X-stay", "SENT-X-household")
    assert found["SENT-X-gone"] == [] and found["SENT-X-gone-msg"] == []
    assert found["SENT-X-stay"] and found["SENT-X-household"]


def test_at_most_fifty_bots(scratch, monkeypatch):
    s = scratch
    s.admin()
    u = s.profile("Bram")
    monkeypatch.setattr(characters, "MAX_PERSONAL_BOTS", 3)
    for i in range(3):
        profiles_service.create_personal(u.id, {"name": f"B{i}"})
    with pytest.raises(ProfileServiceError) as e:
        profiles_service.create_personal(u.id, {"name": "one too many"})
    assert e.value.status == 409


# ---- backends ------------------------------------------------------------- #

def test_validate_backends():
    assert backends_policy.validate_backends(None) == {} == backends_policy.validate_backends({})
    assert backends_policy.validate_backends({"allowed": ["a", "a", " b "]}, known_connections=["a", "b"]) == {
        "allowed": ["a", "b"]}
    assert backends_policy.validate_backends({"allowed": []}, known_connections=[]) == {"allowed": []}
    for bad in ("x", ["a"], {"allowed": "a"}, {"allowed": [1]}, {"default": "a"}, {"allowed": ["a"], "models": {}}):
        with pytest.raises(backends_policy.BackendsError):
            backends_policy.validate_backends(bad, known_connections=["a"])
    with pytest.raises(backends_policy.BackendsError) as e:
        backends_policy.validate_backends({"allowed": ["zzz"]}, known_connections=["a"])
    assert e.value.code == "backends_unknown_connection"


def test_allowed_backends_reads_the_profile(scratch):
    s = scratch
    s.admin()
    assert backends_policy.allowed_backends(s.profile("Bram")) is None
    p = s.profile("Cy")
    p = accounts.update_profile(p.id, backends={"allowed": ["x"]})
    assert backends_policy.allowed_backends(p) == ["x"]
    assert backends_policy.permits(p, "x") and not backends_policy.permits(p, "y")


def test_a_bot_only_runs_on_connections_its_owner_may_use(scratch):
    s = scratch
    c1 = s.connection(name="One")
    c2 = s.connection(default=False, name="Two", model="m2")
    s.admin()
    u = s.profile("Bram")
    accounts.update_profile(u.id, backends={"allowed": [c2.id]})
    # default is c1 but not allowed: the allowed one is used
    bot = profiles_service.create_personal(u.id, {"name": "A"})
    assert characters.connection_of(bot) == c2.id and characters.model_of(bot) == "m2"
    with pytest.raises(ProfileServiceError) as e:
        profiles_service.create_personal(u.id, {"name": "B", "connection": c1.id})
    assert e.value.code == "backend_not_allowed"
    with pytest.raises(ProfileServiceError) as e:
        profiles_service.update_personal(u.id, bot.id, {"connection": c1.id})
    assert e.value.code == "backend_not_allowed"
    assert characters.connection_of(config.get_bot(bot.id)) == c2.id


def test_an_empty_allowed_list_leaves_the_bots_unconnected(scratch):
    s = scratch
    s.connection()
    s.admin()
    u = s.profile("Bram")
    accounts.update_profile(u.id, backends={"allowed": []})
    assert not config.direct_connected(profiles_service.create_personal(u.id, {"name": "A"}))


def test_choosing_a_backend_needs_the_permission(scratch):
    s = scratch
    c1 = s.connection(name="One")
    c2 = s.connection(default=False, name="Two", model="m2")
    s.admin()
    u = s.profile("Bram", denies=["choose_backend"])
    ok = profiles_service.create_personal(u.id, {"name": "Default"})
    assert characters.connection_of(ok) == c1.id
    with pytest.raises(ProfileServiceError) as e:
        profiles_service.create_personal(u.id, {"name": "Picked", "connection": c2.id})
    assert e.value.status == 403
    free = s.profile("Cy")
    assert characters.connection_of(profiles_service.create_personal(free.id, {"name": "P", "connection": c2.id})) == c2.id


# ---- voices --------------------------------------------------------------- #

def test_a_bot_may_use_household_voices_and_its_owners_own_clone_only(scratch, voices):
    s = scratch
    s.admin()
    u, other = s.profile("Bram"), s.profile("Cy")
    voices["clone-x"] = u.id                                  # Bram's clone
    for v in ("builtin:af_test", "stock", ""):
        assert profiles_service.create_personal(u.id, {"name": "ok", "voice": v}).voice == v
    assert profiles_service.create_personal(u.id, {"name": "mine", "voice": "clone-x"}).voice == "clone-x"
    with pytest.raises(ProfileServiceError) as e:                # Cy cannot name Bram's clone
        profiles_service.create_personal(other.id, {"name": "stolen", "voice": "clone-x"})
    assert e.value.code == "voice_not_allowed"
    mine = profiles_service.create_personal(other.id, {"name": "mine"})
    with pytest.raises(ProfileServiceError) as e:
        profiles_service.update_personal(other.id, mine.id, {"voice": "clone-x"})
    assert e.value.code == "voice_not_allowed"
    with pytest.raises(ProfileServiceError):                      # the add-on does not know it
        profiles_service.create_personal(u.id, {"name": "x", "voice": "ghost"})


def test_without_the_voice_addon_only_an_empty_voice_is_accepted(scratch):
    s = scratch
    s.admin()
    u = s.profile("Bram")
    with pytest.raises(ProfileServiceError) as e:
        profiles_service.create_personal(u.id, {"name": "x", "voice": "stock"})
    assert e.value.code == "voice_addon_missing"


# ---- Atlas ---------------------------------------------------------------- #

def test_spawn_atlas_copies_the_shipped_template(scratch):
    s = scratch
    c = s.connection()
    s.admin()
    u = s.profile("Bram")
    bot_id = profiles_service.spawn_atlas(u.id)
    bot = config.get_bot(bot_id)
    assert ID.match(bot_id) and bot.name == "Atlas" and bot.scope == "personal" and bot.owner_profile == u.id
    assert bot.pack == firstrun.ATLAS_PACK and characters.connection_of(bot) == c.id
    assert config.direct_tools_on(bot) and not bot.image_jobs and not bot.reactions and not (bot.advisor or {}).get("knowledge_dir")
    assert bot.avatar == f"{bot_id}-face.png"
    for role in ("face", "full", "thumb"):
        assert (config.AVATAR_DIR / f"{bot_id}-{role}.png").is_file()
    assert profiles_service.has_atlas(u.id) and not profiles_service.has_atlas(s.profile("Cy").id)


def test_spawn_atlas_uses_the_model_the_household_atlas_runs(scratch):
    s = scratch
    c = s.connection(model="")
    s.admin()
    u = s.profile("Bram")
    household = firstrun.atlas_template()
    from dataclasses import replace
    household = replace(household, id=firstrun.BOT_ID, advisor={"providers": [
        {"provider": "openai_compatible", "base_url": c.base_url, "model": "big-1", "connection": c.id}], "tools": True})
    config.upsert_bot(household)
    bot = config.get_bot(profiles_service.spawn_atlas(u.id))
    assert characters.model_of(bot) == "big-1"


def test_every_new_profile_gets_an_atlas_except_the_first_admin_and_basic_ones(scratch):
    s = scratch
    s.connection()
    first = s.admin()
    profiles_service.on_profile_created(first, first_admin=True)
    assert s.personal(first.id) == []
    for name, role in (("Bram", Role.USER), ("Eve", Role.ADMIN)):
        p = s.profile(name, role)
        profiles_service.on_profile_created(p)
        assert len(s.personal(p.id)) == 1
    basic = s.profile("Dee", Role.BASIC)
    profiles_service.on_profile_created(basic)
    assert s.personal(basic.id) == []


def test_a_failed_atlas_never_fails_profile_creation(scratch, monkeypatch):
    s = scratch
    u = s.profile("Bram")
    monkeypatch.setattr(profiles_service, "spawn_atlas", lambda pid: (_ for _ in ()).throw(RuntimeError("disk full")))
    profiles_service.on_profile_created(u)                   # logged, not raised


def test_a_personal_atlas_is_not_the_household_one(scratch):
    s = scratch
    s.connection()
    s.admin()
    u = s.profile("Bram")
    profiles_service.spawn_atlas(u.id)
    assert all(b.scope == "household" for b in config.load_bots() if b.owner_profile == "")
    assert config.get_bot("pack_otter").scope == "household"


def test_the_avatar_upload(scratch):
    from io import BytesIO

    from PIL import Image
    s = scratch
    s.connection()
    s.admin()
    u = s.profile("Bram")
    bot = profiles_service.create_personal(u.id, {"name": "Mine"})
    buf = BytesIO()
    Image.new("RGB", (800, 600), (200, 30, 30)).save(buf, "PNG")
    out = profiles_service.set_personal_avatar(u.id, bot.id, buf.getvalue())
    assert out.avatar == f"{bot.id}-face.png"
    sizes = {r: Image.open(config.AVATAR_DIR / f"{bot.id}-{r}.png").size for r in ("face", "thumb", "full")}
    assert sizes == {"face": (512, 512), "thumb": (128, 128), "full": (800, 600)}
    for bad in (b"", b"not an image"):
        with pytest.raises(ProfileServiceError) as e:
            profiles_service.set_personal_avatar(u.id, bot.id, bad)
        assert e.value.status == 422
    with pytest.raises(ProfileServiceError) as e:
        profiles_service.set_personal_avatar(s.profile("Cy").id, bot.id, buf.getvalue())
    assert e.value.status == 404


def test_the_avatar_face_is_the_square_the_user_placed(scratch):
    """The face picker's square (fractions) becomes the face, not a centred square."""
    from io import BytesIO

    from PIL import Image
    s = scratch
    s.connection()
    s.admin()
    u = s.profile("Bram")
    bot = profiles_service.create_personal(u.id, {"name": "Mine"})
    im = Image.new("RGB", (800, 400), (0, 0, 255))
    im.paste((255, 0, 0), (0, 0, 200, 200))            # the "face": top-left corner, red
    buf = BytesIO()
    im.save(buf, "PNG")
    profiles_service.set_personal_avatar(u.id, bot.id, buf.getvalue(), (0.0, 0.0, 0.5))
    face = Image.open(config.AVATAR_DIR / f"{bot.id}-face.png").convert("RGB")
    assert face.size == (512, 512) and face.getpixel((256, 256)) == (255, 0, 0)
    profiles_service.set_personal_avatar(u.id, bot.id, buf.getvalue())
    centre = Image.open(config.AVATAR_DIR / f"{bot.id}-face.png").convert("RGB")
    assert centre.getpixel((256, 256)) == (0, 0, 255), "no crop = a centred square"


def test_moving_a_bot_to_another_connection_takes_that_connections_model(scratch):
    """The editor's "Runs on" sends the connection only: the old server's model name must not
    follow the bot to a server that does not serve it."""
    s = scratch
    c1 = s.connection(name="One")
    c2 = s.connection(default=False, name="Two", model="m2")
    s.admin()
    u = s.profile("Bram")
    bot = profiles_service.create_personal(u.id, {"name": "A"})
    assert characters.connection_of(bot) == c1.id and characters.model_of(bot) == "m1"
    moved = profiles_service.update_personal(u.id, bot.id, {"connection": c2.id})
    assert characters.connection_of(moved) == c2.id and characters.model_of(moved) == "m2"
    # A model sent with it still wins, and staying on the same connection keeps the model.
    back = profiles_service.update_personal(u.id, bot.id, {"connection": c1.id, "model": "m1-big"})
    assert characters.model_of(back) == "m1-big"
    same = profiles_service.update_personal(u.id, bot.id, {"connection": c1.id})
    assert characters.model_of(same) == "m1-big"
