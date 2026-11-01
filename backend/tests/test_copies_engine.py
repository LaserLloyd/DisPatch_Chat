"""4.2 S3: the copies engine on live templates (profiles_service.make_copy / give_to / reconcile,
app/kits.py reduced to pushed chats). One test per user path of PLAN-v2 section 6 that sits in
this slice. Neutral fixtures only."""
from __future__ import annotations

import ast
import json
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from app import accounts, characters, config, firstrun, kits, ownership, policy, profiles_service
from app.contracts.permissions import Role
from app.profiles_service import ProfileServiceError
from tests.profiles_fixtures import scratch

PNG = b"\x89PNG\r\n\x1a\n"
APP = Path(__file__).resolve().parent.parent / "app"

RICH = {
    "providers": [
        {"provider": "openai_compatible", "model": "m1", "extra_body": {"thinking": {"type": "disabled"}},
         "max_tokens": 700},
        {"provider": "openai_compatible", "model": "fallback-model"},
    ],
    "tools": True, "lookup": {"command": ["infoforge", "search"]}, "history_chars": 8000,
    "location": "Tokyo, Japan", "persona": "You are Otter.",
}


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


def _sync(s, **kw):
    return s.run(lambda db: kits.sync(db, _deliver(db), **kw))["chats"]


def _titles(s, bot_id, owner=None):
    async def go(db):
        return sorted(t.title for t in await db.list_threads(bot_id)
                      if owner is None or t.owner_profile_id == owner)
    return s.run(go)


def _template(bot_id="pack_otter", give_to="everyone", picture=True, **kw):
    """Make a household bot a template: its full brain, a picture, ``give_to``."""
    if picture:
        config.AVATAR_DIR.mkdir(parents=True, exist_ok=True)
        for role in ("face", "full", "thumb"):
            (config.AVATAR_DIR / f"{bot_id}-{role}.png").write_bytes(PNG + b"v1" + role.encode())
    return config.update_bot(bot_id, lambda b: replace(
        b, give_to=give_to, advisor=json.loads(json.dumps(RICH)),
        avatar=f"{bot_id}-face.png" if picture else b.avatar, description="Quick and curious", **kw))


def _copy(pid, template_id="pack_otter"):
    got = [b for b in config.load_bots() if b.scope == "personal" and b.owner_profile == pid
           and b.template == template_id]
    assert len(got) <= 1, "R4: at most one copy per (profile, template)"
    return got[0] if got else None


def _atlas_template(s, give_to="everyone"):
    config.upsert_bot(replace(firstrun.atlas_template(), avatar="atlas-face.png", give_to=give_to))
    firstrun.install_default_avatars()


def _stored(bot_id):
    raw = yaml.safe_load(config.CONFIG_PATH.read_text(encoding="utf-8"))
    return next(e for e in raw["bots"] if e["id"] == bot_id)


# ---- one copy path --------------------------------------------------------------------- #


def _calls(tree):
    """(function name enclosing the call, call node) for every call in a module."""
    out = []

    class V(ast.NodeVisitor):
        def __init__(self):
            self.stack: list[str] = []

        def visit_FunctionDef(self, node):
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()
        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Call(self, node):
            out.append((self.stack[-1] if self.stack else "<module>", node))
            self.generic_visit(node)
    V().visit(tree)
    return out


def _callee(node):
    f = node.func
    return f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")


def test_only_one_copy_path():
    """No code builds a copy except profiles_service.make_copy: ``new_copy(...)`` is called only
    there, a ``Bot(...)`` with ``template=`` only in config.py (the loader) and make_copy, and no
    caller hands ``cloned_from=`` / ``template=`` to a creator."""
    offenders: list[str] = []
    for path in sorted(APP.rglob("*.py")):
        rel = path.relative_to(APP).as_posix()
        for fn, call in _calls(ast.parse(path.read_text(encoding="utf-8"))):
            name = _callee(call)
            kws = {k.arg for k in call.keywords}
            if name == "new_copy" and not (rel == "profiles_service.py" and fn == "make_copy"):
                offenders.append(f"{rel}:{call.lineno} {fn}: new_copy")
            if name == "Bot" and "template" in kws and rel != "config.py":
                offenders.append(f"{rel}:{call.lineno} {fn}: Bot(template=)")
            if name in ("create_personal", "create") and kws & {"cloned_from", "template"}:
                offenders.append(f"{rel}:{call.lineno} {fn}: {name}(cloned_from/template=)")
    assert offenders == []


# ---- make_copy ------------------------------------------------------------------------- #


def test_make_copy_builds_a_live_copy_that_carries_the_whole_template(scratch, on):
    s = scratch
    s.connection()
    s.admin()
    _template()
    u = s.profile("Bram")
    bot, cleared = profiles_service.make_copy(accounts.get_profile(u.id), "pack_otter")
    assert cleared is False
    got = config.get_bot(bot.id)
    assert got.template == "pack_otter" and got.scope == "personal" and got.owner_profile == u.id
    assert got.overrides in ({}, None)
    for key in ("lookup", "history_chars", "location", "providers", "persona"):
        assert got.advisor[key] == RICH[key], key
    entry = _stored(bot.id)
    assert "advisor" not in entry and "overrides" not in entry, "nothing is stored but the identity"
    assert not got.reactions and not got.avatar_pool
    # the picture is the copy's own file, byte for byte the template's
    assert got.avatar == f"{bot.id}-face.png"
    assert (config.AVATAR_DIR / got.avatar).read_bytes() == (config.AVATAR_DIR / "pack_otter-face.png").read_bytes()


def test_make_copy_refuses_what_is_not_a_template(scratch, on):
    s = scratch
    s.admin()
    u = s.profile("Bram")
    prof = accounts.get_profile(u.id)
    with pytest.raises(ProfileServiceError) as e:
        profiles_service.make_copy(prof, "pack_otter")             # household, not a template
    assert e.value.code == "not_copyable"
    with pytest.raises(ProfileServiceError) as e:
        profiles_service.make_copy(prof, "helper")                 # an agent bot
    assert e.value.code == "not_copyable"
    with pytest.raises(ProfileServiceError) as e:
        profiles_service.make_copy(prof, "no-such-bot")
    assert e.value.status == 404


def test_the_second_copy_for_the_same_profile_is_the_first(scratch, on):
    s = scratch
    s.admin()
    _template()
    u = s.profile("Bram")
    prof = accounts.get_profile(u.id)
    one, _ = profiles_service.make_copy(prof, "pack_otter")
    two, _ = profiles_service.make_copy(prof, "pack_otter")
    assert one.id == two.id and _copy(u.id).id == one.id


# ---- new profile / Basic / the protected Admin ----------------------------------------- #


@pytest.mark.parametrize("role", [Role.USER, Role.ADMIN, Role.BASIC])
def test_a_new_profile_gets_the_everyone_templates_and_the_ticked_ones(scratch, on, role):
    s = scratch
    s.connection()
    s.admin()
    _atlas_template(s)
    _template("pack_otter", give_to="everyone")
    _template("pack_heron", give_to=[])
    u = s.profile("Bram", role)
    profiles_service.on_profile_created(accounts.get_profile(u.id))
    assert _copy(u.id, "atlas") and _copy(u.id, "pack_otter")
    assert _copy(u.id, "pack_heron") is None, "not given to this profile"
    w = s.profile("Wren", role)                      # the create dialog ticked Heron
    profiles_service.on_profile_created(accounts.get_profile(w.id), ticked=["pack_heron"])
    assert _copy(w.id, "pack_heron") is not None and config.get_bot("pack_heron").give_to == [w.id]
    _sync(s)
    if role is Role.BASIC:
        assert _titles(s, _copy(u.id, "atlas").id) == [], "a Basic profile has no guide chats"
    else:
        assert _titles(s, _copy(u.id, "atlas").id, u.id), "kit chats follow the copy"


def test_the_protected_admin_gets_copies_like_everyone_else(scratch, on):
    s = scratch
    owner = s.admin()
    _atlas_template(s)
    profiles_service.on_profile_created(owner, first_admin=True)
    assert _copy(owner.id, "atlas") is not None
    _sync(s)
    assert _titles(s, _copy(owner.id, "atlas").id, owner.id)
    # ... and What's new lands on the copy, not on the template
    assert firstrun._atlas_of(owner) == _copy(owner.id, "atlas").id


def test_a_basic_profile_has_managed_copies_it_cannot_edit(scratch, on):
    s = scratch
    s.admin()
    _template()
    b = s.profile("Dee", Role.BASIC)
    profiles_service.on_profile_created(accounts.get_profile(b.id))
    mine = _copy(b.id)
    assert mine is not None and mine.name == "Otter"
    with pytest.raises(ProfileServiceError) as e:
        profiles_service.update_personal(b.id, mine.id, {"name": "Mine"})
    assert e.value.code == "basic_no_bots"
    with pytest.raises(ProfileServiceError):
        profiles_service.create_personal(b.id, {"name": "Own"})
    # a template edit still reaches it
    config.update_bot("pack_otter", lambda t: replace(t, name="Otter 2"))
    assert config.get_bot(mine.id).name == "Otter 2"


# ---- template edit / Reset ------------------------------------------------------------- #


def test_a_template_edit_reaches_every_copy_without_that_override(scratch, on):
    s = scratch
    s.connection()
    s.admin()
    _template()
    a, b = s.profile("Ada2"), s.profile("Bram")
    for p in (a, b):
        profiles_service.make_copy(accounts.get_profile(p.id), "pack_otter")
    profiles_service.update_personal(b.id, _copy(b.id).id, {"name": "Mine", "persona": "Own text"})
    config.update_bot("pack_otter", lambda t: replace(
        t, name="Otter 2", advisor={**t.advisor, "lookup": {"command": ["other"]}, "persona": "Newer persona"}))
    ca, cb = _copy(a.id), _copy(b.id)
    assert ca.name == "Otter 2" and ca.advisor["persona"] == "Newer persona"
    assert ca.advisor["lookup"] == {"command": ["other"]}
    assert cb.name == "Mine" and cb.advisor["persona"] == "Own text", "the owner's own change survives"
    assert cb.advisor["lookup"] == {"command": ["other"]}, "and the rest still follows"


def test_reset_clears_the_overrides_and_brings_the_picture_back(scratch, on):
    s = scratch
    s.connection()
    s.admin()
    _template()
    u = s.profile("Bram")
    profiles_service.make_copy(accounts.get_profile(u.id), "pack_otter")
    mine = _copy(u.id)
    profiles_service.update_personal(u.id, mine.id, {"name": "Mine", "emoji": "X", "persona": "Own text"})
    from io import BytesIO

    from PIL import Image
    buf = BytesIO()
    Image.new("RGB", (64, 64), (200, 10, 10)).save(buf, "PNG")
    profiles_service.set_personal_avatar(u.id, mine.id, buf.getvalue())
    assert (config.AVATAR_DIR / f"{mine.id}-face.png").read_bytes() != (config.AVATAR_DIR / "pack_otter-face.png").read_bytes()
    assert _stored(mine.id)["overrides"]
    got = profiles_service.reset_personal(u.id, mine.id)
    assert (got.name, got.emoji, got.advisor["persona"]) == ("Otter", config.get_bot("pack_otter").emoji, "You are Otter.")
    assert "overrides" not in _stored(mine.id)
    assert (config.AVATAR_DIR / got.avatar).read_bytes() == (config.AVATAR_DIR / "pack_otter-face.png").read_bytes()


def test_the_picture_follows_the_template_unless_the_owner_set_their_own(scratch, on):
    s = scratch
    s.connection()
    s.admin()
    _template()
    a, b = s.profile("Ada2"), s.profile("Bram")
    for p in (a, b):
        profiles_service.make_copy(accounts.get_profile(p.id), "pack_otter")
    from io import BytesIO

    from PIL import Image
    buf = BytesIO()
    Image.new("RGB", (64, 64), (10, 10, 200)).save(buf, "PNG")
    profiles_service.set_personal_avatar(b.id, _copy(b.id).id, buf.getvalue())
    own = (config.AVATAR_DIR / _copy(b.id).avatar).read_bytes()
    (config.AVATAR_DIR / "pack_otter-face.png").write_bytes(PNG + b"v2-face")
    assert profiles_service.reconcile_copies() is True
    assert (config.AVATAR_DIR / _copy(a.id).avatar).read_bytes() == PNG + b"v2-face"
    assert (config.AVATAR_DIR / _copy(b.id).avatar).read_bytes() == own
    assert profiles_service.reconcile_copies() is False, "nothing left to do"


# ---- connection policy ----------------------------------------------------------------- #


def test_the_connection_policy_follows_the_owners_backends_and_the_owners_pick(scratch, on):
    s = scratch
    s.admin()
    a = s.connection(default=True, name="A", model="m1")
    b = s.connection(default=False, name="B", model="m2")
    _template()
    config.update_bot("pack_otter", lambda t: characters.with_connection(t, a.id, "m1"))
    u = s.profile("Bram")
    profiles_service.make_copy(accounts.get_profile(u.id), "pack_otter")
    assert characters.connection_of(_copy(u.id)) == a.id, "allowed: it follows the template"
    profiles_service.set_backends(u.id, [b.id])
    profiles_service.reconcile_copies()
    mine = _copy(u.id)
    assert characters.connection_of(mine) == b.id and characters.model_of(mine) == "m2"
    assert mine.advisor["providers"][1] == RICH["providers"][1], "the fallback chain still follows"
    assert "lookup" in mine.advisor
    profiles_service.set_backends(u.id, None)
    profiles_service.reconcile_copies()
    assert characters.connection_of(_copy(u.id)) == a.id, "allowed again and never picked: back on the template's"
    # the owner picks: that is never overruled
    profiles_service.update_personal(u.id, _copy(u.id).id, {"connection": b.id})
    profiles_service.reconcile_copies()
    assert characters.connection_of(_copy(u.id)) == b.id
    config.update_bot("pack_otter", lambda t: characters.with_connection(t, a.id, "m1"))
    profiles_service.reconcile_copies()
    assert characters.connection_of(_copy(u.id)) == b.id


def test_a_template_with_no_connection_gets_the_owners_default(scratch, on):
    s = scratch
    s.admin()
    _template()
    config.update_bot("pack_otter", lambda t: replace(t, advisor={"providers": [], "tools": True}))
    u = s.profile("Bram")
    profiles_service.make_copy(accounts.get_profile(u.id), "pack_otter")
    assert characters.connection_of(_copy(u.id)) == ""
    c = s.connection(name="Late")
    profiles_service.connect_unconnected_atlases(c.id)
    assert characters.connection_of(_copy(u.id)) == c.id


# ---- give_to: tick, untick, re-add ----------------------------------------------------- #


def test_ticking_a_profile_gives_it_a_copy_and_its_kit_chats(scratch, on):
    s = scratch
    s.connection()
    s.admin()
    _template(give_to=[])
    kits.save_kit("pack_otter", chats=[{"title": "Hello", "body": "Welcome to Otter.", "audience": "everyone"}])
    u = s.profile("Bram")
    assert profiles_service.reconcile_copies() is False and _copy(u.id) is None
    prev = profiles_service.give_to_preview("pack_otter", [u.id])
    assert prev == {"add": [u.id], "remove": [], "detach": []}
    out = profiles_service.set_give_to("pack_otter", [u.id])
    assert out["added"] == [u.id]
    assert config.get_bot("pack_otter").give_to == [u.id]
    assert _copy(u.id) is not None
    _sync(s)
    assert _titles(s, _copy(u.id).id, u.id) == ["Hello"]


def test_an_explicit_tick_clears_a_deletion_but_auto_clone_alone_does_not(scratch, on):
    s = scratch
    s.connection()
    s.admin()
    _template(give_to=[])
    u = s.profile("Bram")
    profiles_service.set_give_to("pack_otter", [u.id])
    profiles_service.delete_personal(u.id, _copy(u.id).id)
    assert kits.removed_for(u.id) == {"pack_otter"}
    profiles_service.reconcile_copies()
    assert _copy(u.id) is None, "auto-clone respects the deletion"
    profiles_service.set_give_to("pack_otter", [])
    profiles_service.set_give_to("pack_otter", [u.id])
    assert _copy(u.id) is not None and kits.removed_for(u.id) == set(), "the Admin's tick is explicit"


def test_unticking_a_profile_with_chats_detaches_its_copy_and_keeps_everything(scratch, on):
    s = scratch
    s.connection()
    s.admin()
    _template()
    u = s.profile("Bram")
    profiles_service.make_copy(accounts.get_profile(u.id), "pack_otter")
    mine = _copy(u.id)
    thread = s.thread(u.id, mine.id, "SENT-my-chat")
    profiles_service.update_personal(u.id, mine.id, {"emoji": "Z"})
    prev = profiles_service.give_to_preview("pack_otter", [])
    assert prev == {"add": [], "remove": [], "detach": [u.id]}
    out = profiles_service.set_give_to("pack_otter", [])
    assert out["detached"] == [u.id] and out["removed"] == []
    kept = config.get_bot(mine.id)
    assert kept is not None and kept.template == "" and not kept.overrides and kept.cloned_from == ""
    assert (kept.name, kept.emoji) == ("Otter", "Z"), "its effective values are written out"
    assert kept.advisor["lookup"] == RICH["lookup"] and kept.advisor["providers"] == RICH["providers"]
    assert _stored(mine.id)["advisor"], "a plain own bot now"
    assert s.run(lambda db: db.get_thread(thread)) is not None
    config.update_bot("pack_otter", lambda t: replace(t, name="Changed"))
    assert config.get_bot(mine.id).name == "Otter", "no longer follows"
    assert not kits.managed(config.get_bot(mine.id))


def test_unticking_a_profile_with_only_seeded_chats_removes_the_copy_and_them(scratch, on):
    s = scratch
    s.connection()
    owner = s.admin()
    _template()
    kits.save_kit("pack_otter", chats=[{"title": "Hello", "body": "Welcome to Otter.", "audience": "everyone"}])
    u = s.profile("Bram")
    profiles_service.make_copy(accounts.get_profile(u.id), "pack_otter")
    _sync(s)
    mine = _copy(u.id)
    assert _titles(s, mine.id, u.id) == ["Hello"]
    assert _copy(owner.id) is not None, "the protected Admin has its copy like everyone"
    prev = profiles_service.give_to_preview("pack_otter", [])
    assert prev["add"] == [] and prev["detach"] == [] and sorted(prev["remove"]) == sorted([u.id, owner.id])
    out = profiles_service.set_give_to("pack_otter", [])
    assert sorted(out["removed"]) == sorted([u.id, owner.id])
    assert config.get_bot(mine.id) is None
    assert _titles(s, mine.id) == []
    assert not list(config.AVATAR_DIR.glob(f"{mine.id}-*"))
    assert _sync(s) == [], "nothing comes back"


def test_removing_a_copy_then_get_my_copy_makes_a_fresh_one_with_fresh_guides(scratch, on):
    s = scratch
    s.connection()
    s.admin()
    _atlas_template(s)
    u = s.profile("Bram")
    profiles_service.on_profile_created(accounts.get_profile(u.id))
    first = _copy(u.id, "atlas")
    _sync(s)
    profiles_service.delete_personal(u.id, first.id)
    assert _sync(s) == [] and _copy(u.id, "atlas") is None
    out = profiles_service.clone_to_own("atlas", policy.profile_principal(u))
    again = config.get_bot(out["bot"]["id"])
    assert again.id != first.id and again.template == "atlas" and again.pack == firstrun.ATLAS_PACK
    assert kits.removed_for(u.id) == set()
    _sync(s)
    assert _titles(s, again.id, u.id) == ["Meet Atlas", "Using DisPatch"]
    # not entitled -> not available
    _template("pack_otter", give_to=[])
    with pytest.raises(ProfileServiceError):
        profiles_service.clone_to_own("pack_otter", policy.profile_principal(u))


def test_a_deleted_profile_leaves_every_give_to(scratch, on):
    s = scratch
    s.admin()
    u, v = s.profile("Bram"), s.profile("Cy")
    _template(give_to=[u.id, v.id])
    profiles_service.reconcile_copies()
    profiles_service.delete_profile(v.id, profiles_service.delete_preview(v.id)["token"])
    assert config.get_bot("pack_otter").give_to == [u.id]


# ---- kits: pushed chats only ------------------------------------------------------------ #


def test_kit_chats_follow_the_template_link_and_a_new_rev_replaces_an_untouched_thread(scratch, on):
    s = scratch
    s.connection()
    s.admin()
    _template()
    kits.save_kit("pack_otter", chats=[{"title": "Hello", "body": "Welcome v1.", "audience": "everyone"}])
    u = s.profile("Bram")
    profiles_service.make_copy(accounts.get_profile(u.id), "pack_otter")
    assert kits.kit_id_of(_copy(u.id)) == "pack_otter"
    assert len(_sync(s)) == 2 and _sync(s) == [], "one chat per copy (the Owner has one too)"
    cid = kits.get_kit("pack_otter").chats[0].id
    kits.save_kit("pack_otter", chats=[{"id": cid, "title": "Hello", "body": "Welcome v2.", "audience": "everyone"}])
    assert len(_sync(s)) == 2
    assert _titles(s, _copy(u.id).id, u.id) == ["Hello"], "the untouched v1 thread was replaced"
    for gone in ("FIELDS", "sync_copy", "current_values", "version_of", "ensure_profile", "_wants_copy"):
        assert not hasattr(kits, gone), gone
    assert not hasattr(firstrun, "backfill_house_guides") and not hasattr(firstrun, "HOUSE_FLAG")


# ---- the claim hook -------------------------------------------------------------------- #


def test_a_fresh_install_seeds_nothing_on_the_template_and_the_claim_posts_the_first_run_chat(scratch, on):
    s = scratch
    s.connection()
    config.CONFIG_PATH.write_text(yaml.safe_dump({"bots": [
        {"id": "atlas", "name": "Atlas", "give_to": "everyone", "advisor": {"providers": [], "tools": True}}]}))
    config._invalidate_bots_cache()
    firstrun.install_default_avatars()
    assert s.run(lambda db: firstrun.seed_if_first_run(db, _deliver(db))) == []
    assert _titles(s, "atlas") == [], "no chat on a template"
    owner = s.admin()
    profiles_service.on_profile_created(owner, first_admin=True)
    copy = _copy(owner.id, "atlas")
    assert copy is not None
    _sync(s)
    assert "Set up add-ons" in _titles(s, copy.id, owner.id)
    assert {"Meet Atlas", "Using DisPatch", "How DisPatch works"} <= set(_titles(s, copy.id, owner.id))
    assert _titles(s, "atlas") == []
    assert _sync(s) == []
    assert firstrun.flag_path().exists()


def test_a_claim_moves_the_open_install_chats_on_a_template_to_the_admins_copy(scratch, on):
    s = scratch
    s.connection()
    _atlas_template(s)
    s.thread(None, "atlas", "SENT-before-claim")
    owner = s.admin()                    # adopts unowned rows: the thread is now the owner's
    profiles_service.on_profile_created(owner, first_admin=True)
    copy = _copy(owner.id, "atlas")
    assert _titles(s, "atlas") == [] and "SENT-before-claim" in _titles(s, copy.id, owner.id)


def test_repoint_threads_moves_threads_and_their_side_tables(scratch, on):
    s = scratch
    s.admin()
    u = s.profile("Bram")
    v = s.profile("Cy")
    t_u = s.thread(u.id, "pack_otter", "mine")
    t_v = s.thread(v.id, "pack_otter", "theirs")
    mid = s.message_ids(t_u)[0]
    s.image_job(t_u, mid, "a picture", "pack_otter")
    s.advisor_request(t_u, "look this up", "pack_otter")
    n = profiles_service.repoint_threads("pack_otter", "pcopy000000", u.id)
    assert n == 1
    assert _titles(s, "pcopy000000") == ["mine"] and _titles(s, "pack_otter") == ["theirs"]
    import sqlite3
    con = sqlite3.connect(s.root / "chats.db")
    assert con.execute("SELECT bot_id FROM image_jobs").fetchone()[0] == "pcopy000000"
    con = sqlite3.connect(s.root / "advisor.db")
    assert con.execute("SELECT bot_id FROM requests").fetchone()[0] == "pcopy000000"
    assert t_v


# ---- SF-2: creators make Owner-personal agent bots ---------------------------------------- #


def test_agent_bots_are_created_owner_personal_once_there_is_a_protected_admin(scratch, on):
    s = scratch
    made = characters.create(name="Early", backend="agent", agent="early")
    assert made.scope == "household", "an unclaimed install keeps it household"
    owner = s.admin()
    got = characters.create(name="Runner", backend="agent", agent="runner")
    assert (got.scope, got.owner_profile) == ("personal", owner.id)
    direct = characters.create(name="Plain")
    assert direct.scope == "household" and direct.give_to is None


def test_an_apps_bot_is_provisioned_owner_personal(scratch, on, monkeypatch):
    from types import SimpleNamespace

    from app import apps_loader
    s = scratch
    owner = s.admin()
    spec = SimpleNamespace(id="jobsapp", name="Jobs", emoji="J", avatar="", agent="scout", visible=False)
    la = SimpleNamespace(manifest=SimpleNamespace(id="jobsapp", bot=spec))
    monkeypatch.setattr(apps_loader, "mounted", lambda: [la])
    assert apps_loader.provision_bots() == ["jobsapp"]
    b = config.get_bot("jobsapp")
    assert (b.scope, b.owner_profile) == ("personal", owner.id)


def test_adopting_the_agent_bots_makes_them_the_admins_and_leaves_direct_ones(scratch, on):
    s = scratch
    owner = s.admin()
    assert profiles_service.adopt_agent_bots(owner.id) == ["helper"]
    h = config.get_bot("helper")
    assert (h.scope, h.owner_profile) == ("personal", owner.id)
    assert config.get_bot("pack_otter").scope == "household", "direct bots are not touched"
    assert profiles_service.adopt_agent_bots(owner.id) == [], "once"


# ---- Reset profile and role changes ------------------------------------------------------- #


def _reset(pid):
    return profiles_service.reset_profile(pid, profiles_service.reset_preview(pid)["token"])


@pytest.mark.parametrize("role", [Role.USER, Role.ADMIN, Role.BASIC])
def test_reset_profile_deletes_the_copies_and_makes_them_again_with_fresh_kit_chats(scratch, on, role):
    s = scratch
    s.connection()
    s.admin()
    _atlas_template(s)
    u = s.profile("Bram", role)
    profiles_service.on_profile_created(accounts.get_profile(u.id))
    first = _copy(u.id, "atlas")
    _sync(s)
    own = None
    if role is not Role.BASIC:
        own = profiles_service.create_personal(u.id, {"name": "Mine"})
        s.thread(u.id, own.id, "SENT-mine")
    deleted = _reset(u.id)
    assert deleted["bots"] == (2 if own else 1)
    again = _copy(u.id, "atlas")
    assert again is not None and again.id != first.id and config.get_bot(first.id) is None
    assert [b.id for b in s.personal(u.id)] == [again.id], "an own bot is gone, the copy is made again"
    _sync(s)
    expected = [] if role is Role.BASIC else ["Meet Atlas", "Using DisPatch"] + (
        ["How DisPatch works"] if role is Role.ADMIN else [])
    assert _titles(s, again.id, u.id) == sorted(expected)


def test_user_to_basic_makes_the_managed_copies_again_and_basic_to_user_keeps_them(scratch, on):
    s = scratch
    s.connection()
    s.admin()
    _template()
    u = s.profile("Bram")
    profiles_service.on_profile_created(accounts.get_profile(u.id))
    before = _copy(u.id)
    prev = profiles_service.change_role_preview(u.id, "basic")
    profiles_service.change_role(u.id, "basic", confirm_token=prev["token"], household_bots=[])
    as_basic = _copy(u.id)
    assert as_basic is not None and as_basic.id != before.id, "wiped, then made again"
    profiles_service.change_role(u.id, "user")
    assert _copy(u.id).id == as_basic.id, "Basic -> User keeps its copies"
