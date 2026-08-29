"""Pool identity, pool-yaml concurrency and the Safe-Mode edges around them.

These pin the review findings of 2026-08-29: one id must mean exactly one
picture (and keep meaning it after a fire), a refill round must not write back
a stale config, a spent blob must not inherit some other bot's `safe` flag, and
a pool must never offer a file it cannot serve.

Hermetic like the rest of tests/: a throwaway data dir, no image rig.
Run: cd backend && uv run pytest tests/test_pool_identity.py
"""
from __future__ import annotations

import pytest

from app import auth, config, dashboard, reactions

# --------------------------------------------------------------------------- #
# Environment
# --------------------------------------------------------------------------- #

COMPANION = "Scout"                    # in the shared test roster, no reactions


@pytest.fixture
def rx(tmp_path, monkeypatch):
    """Isolated data dir with a seeded starter pack — no client, no DB."""
    for name, sub in [("DATA_DIR", ""), ("CONFIG_PATH", "config.yaml"),
                      ("MEDIA_DIR", "media"), ("FILES_DIR", "files"),
                      ("LOG_DIR", "logs"), ("BACKUP_DIR", "backups")]:
        monkeypatch.setattr(config, name, tmp_path / sub if sub else tmp_path)
    monkeypatch.setattr(auth, "SECURITY_PATH", tmp_path / "security.yaml")
    monkeypatch.setattr(auth, "RECOVERY_PATH", tmp_path / "RECOVERY-CODE.txt")
    config._invalidate_bots_cache()
    reactions.invalidate()
    reactions._pool_cache.clear()
    reactions._bank_cache.clear()
    reactions._quarantined.clear()
    reactions.limiter.reset()
    config.ensure_dirs()
    reactions.seed_starter_pack()
    yield tmp_path


def _drop(name: str, *, mood: str = "bravo", bot_id: str | None = None,
          folder: str | None = None) -> None:
    """Hand-drop one real image into moods-<bot>/<folder or mood>/."""
    d = reactions._moods_dir(bot_id) / (folder or mood)
    d.mkdir(parents=True, exist_ok=True)
    src = next(config.REACTIONS_BUILTIN_DIR.glob("*.png"))
    (d / name).write_bytes(src.read_bytes())


def _enable_reactions(bot_id: str) -> None:
    rows = [{"id": b.id, "order": b.order,
             "reactions": b.reactions or b.id == bot_id}
            for b in config.load_bots()]
    config.save_bot_order(rows)
    config._invalidate_bots_cache()


# --------------------------------------------------------------------------- #
# 1. One id ↔ one file
# --------------------------------------------------------------------------- #


def test_colliding_hand_dropped_names_get_distinct_ids(rx):
    """`a b.png` and `a-b.png` slug to the SAME base id. One id for two files
    means a fire retires one picture while the trace re-opens the other — and
    that one id could burn both in turn. Each file must get its own id, and
    each id must resolve to the file that minted it."""
    _drop("a b.png")
    _drop("a-b.png")
    d = reactions._moods_dir() / "bravo"
    ids = {p.name: reactions.pool_file_id_for("bravo", p)
           for p in sorted(d.iterdir())}
    assert len(set(ids.values())) == 2, ids
    for name, rid in ids.items():
        r = reactions.pool_get(rid)
        assert r is not None and r.file.endswith(f"/{name}"), (rid, r)

    # Firing one leaves the other exactly where it was, and the fired id still
    # resolves — from spent/, for the trace.
    first, second = sorted(ids.items())
    assert reactions.pool_consume(first[1]) is True
    assert not (d / first[0]).exists()
    assert (d / second[0]).is_file()
    assert reactions.pool_get(second[1]) is not None
    assert reactions.pool_consume(second[1]) is True, "the survivor is still firable"


def test_a_case_variant_mood_folder_shares_one_id_namespace(rx):
    """`Bravo/` beside `bravo/` is ONE mood as far as ids are concerned
    (_mood_dirs lowercases the key), so files in the two folders collide the
    same way and must be disambiguated together."""
    _drop("pic.png", folder="bravo")
    _drop("pic.png", folder="Bravo")
    lower = reactions._moods_dir() / "bravo" / "pic.png"
    upper = reactions._moods_dir() / "Bravo" / "pic.png"
    a = reactions.pool_file_id_for("bravo", lower)
    b = reactions.pool_file_id_for("bravo", upper)
    assert a != b
    assert reactions.pool_get(a).file.split("/")[1] == "bravo"
    assert reactions.pool_get(b).file.split("/")[1] == "Bravo"


def test_an_uncontested_name_keeps_its_plain_id(rx):
    """Existing chat traces name the plain id — disambiguation must only
    engage where there is an actual clash."""
    _drop("My Great Pic.png")
    p = reactions._moods_dir() / "bravo" / "My Great Pic.png"
    assert reactions.pool_file_id_for("bravo", p) == "pool-bravo-my-great-pic"
    assert reactions.get("pool-bravo-my-great-pic") is not None


def test_a_legacy_id_cannot_retire_a_blob_in_another_mood(rx):
    """The `pool-<hex>` fallback used to scan every folder, so an id naming
    one mood could burn a blob sitting in a different one."""
    _drop("ff00aa.png", folder="mad")
    rid = "pool-bravo-ff00aa"              # names bravo, blob lives in mad
    assert reactions._resolve_pool_file(rid, reactions._moods_dir()) is None
    assert reactions.pool_consume(rid) is False
    assert (reactions._moods_dir() / "mad" / "ff00aa.png").is_file()
    # A genuinely unscoped legacy id (no mood in it) still finds its blob.
    assert reactions.pool_consume("pool-ff00aa") is True


# --------------------------------------------------------------------------- #
# 2. yaml read-modify-write races
# --------------------------------------------------------------------------- #


def test_refill_preserves_a_config_change_made_mid_round(rx, monkeypatch):
    """A refill holds its PoolState across minutes of rig calls. Writing that
    snapshot back reverted whatever the operator changed meanwhile."""
    monkeypatch.setattr(reactions, "image_cli_available", lambda: True)
    reactions.bank_save({"categories": {"solo": {"label": "Solo", "prompts": ["p"]}}})
    st = reactions.pool_load()
    st.config.per_mood = 2
    st.config.min_per_mood = 1
    reactions.pool_save(st)

    def _fake_gen(cfg, category, *, bot_id=None):
        # The operator edits the pool WHILE the round is running.
        reactions.pool_update_config({"style": "watercolour"}, bot_id)
        _drop("gen.png", folder=category, bot_id=bot_id)
        return reactions.pool_file_id("solo", "gen.png")

    monkeypatch.setattr(reactions, "_pool_generate_one", _fake_gen)
    monkeypatch.setattr(reactions.pool_guard, "free_vram_before_mint",
                        lambda: {"ok": True, "reason": ""})
    assert reactions.pool_refill() >= 1
    assert reactions.pool_load().config.style == "watercolour", \
        "the refill wrote back its pre-edit config snapshot"


def test_pool_load_hands_out_a_private_copy(rx):
    """Editing a loaded state must not mutate what the next reader sees —
    that is what made one caller's unsaved edit everybody's truth."""
    a = reactions.pool_load()
    a.config.per_mood = 99
    a.last_error = "scribble"
    b = reactions.pool_load()
    assert b.config.per_mood != 99
    assert b.last_error == ""


def test_a_card_added_during_a_heal_is_not_erased(rx):
    """heal_pack's load→save window used to drop a concurrently added entry,
    orphaning its blob. The registry lock closes it."""
    import threading
    pack = reactions.load()
    victim = pack.reactions[0]
    reactions.image_path(victim).unlink()   # one dangling card to heal

    started = threading.Event()
    real_save = reactions.save

    def _slow_save(p):
        started.set()
        return real_save(p)

    added: list = []

    def _adder():
        started.wait(2.0)
        added.append(reactions.add(name="Late Arrival", image_bytes=b"x" * 32))

    t = threading.Thread(target=_adder)
    t.start()
    reactions.save = _slow_save
    try:
        reactions.heal_pack()
    finally:
        reactions.save = real_save
    t.join(5.0)
    assert added, "the concurrent add never ran"
    assert reactions.load().by_id(added[0].id) is not None, \
        "heal_pack erased a card added while it was running"


# --------------------------------------------------------------------------- #
# 3. Safe Mode and the shared spent/ store
# --------------------------------------------------------------------------- #


def test_an_unsafe_bots_spent_image_is_not_safe_mode_visible(rx):
    """spent/ is shared and no longer records who fired a blob. "Some pool is
    safe" therefore published EVERY spent picture — including the unsafe bot's,
    which a locked device could then fetch."""
    _enable_reactions(COMPANION)
    _drop("aa11.png", folder="mad")                       # main: unsafe pool
    _drop("bb22.png", folder="mad", bot_id=COMPANION)     # companion: safe pool
    st = reactions.pool_load(COMPANION)
    st.config.safe = True
    reactions.pool_save(st, COMPANION)

    mine = reactions.pool_file_id("mad", "aa11.png")
    theirs = reactions.pool_file_id("mad", "bb22.png")
    assert reactions.pool_consume(mine) is True
    reactions.invalidate()

    ids = reactions.safe_ids()
    assert mine not in ids, "an unsafe bot's fired image became Safe-Mode visible"
    assert theirs in ids, "the safe bot's own ready image is still visible"
    r = reactions.get_for_display(mine, bot_id=COMPANION)
    assert r is not None and r.safe is False, \
        "the spent blob inherited the queried pool's safe flag"


def test_a_spent_image_stays_safe_when_every_pool_agrees(rx):
    """The conservative rule must not cost the ordinary single-pool install
    its trace pictures."""
    st = reactions.pool_load()
    st.config.safe = True
    reactions.pool_save(st)
    _drop("cc33.png", folder="mad")
    rid = reactions.pool_file_id("mad", "cc33.png")
    assert reactions.pool_consume(rid) is True
    reactions.invalidate()
    assert rid in reactions.safe_ids()
    assert reactions.get_for_display(rid).safe is True


# --------------------------------------------------------------------------- #
# 4. Aliases survive a heal
# --------------------------------------------------------------------------- #


def test_heal_keeps_its_newest_aliases_within_the_cap(rx):
    """heal_pack moves a stranded id onto its kin AS AN ALIAS, which is what
    keeps old chat traces resolving. It used to append past the load path's
    cap, so the next load() silently cut the list back."""
    r = reactions.add(name="Check In", image_bytes=b"x" * 32)
    reactions.update(r.id, aliases=[f"old{i}" for i in range(reactions.MAX_ALIASES)])
    # A dangling regen sibling whose id must survive as an alias.
    kin = reactions.add(name="Check In 2", image_bytes=b"y" * 32)
    reactions.update(kin.id, aliases=[])
    stranded = reactions.load().by_id(kin.id)
    reactions.image_path(stranded).unlink()
    reactions.invalidate()

    reactions.heal_pack()
    survivor = reactions.load().by_id(r.id)
    assert survivor is not None
    assert len(survivor.aliases) <= reactions.MAX_ALIASES
    assert kin.id in survivor.aliases, "the healed id was dropped by the cap"
    # And it round-trips through a reload rather than being trimmed away.
    reactions.invalidate()
    assert kin.id in reactions.load().by_id(r.id).aliases
    assert reactions.get(kin.id).id == r.id


# --------------------------------------------------------------------------- #
# 5. A named bot must actually exist
# --------------------------------------------------------------------------- #


def test_a_syntactically_valid_unknown_bot_is_refused(rx):
    """`?bot_id=Ghost` is well-formed, and a write under it used to MINT a
    prompt bank, a pool config and a moods-Ghost/ directory that then joined
    _all_moods_roots for good."""
    with pytest.raises(reactions.ReactionError) as e:
        reactions.require_bot_id("Ghost")
    assert e.value.status == 404
    with pytest.raises(reactions.ReactionError):
        reactions.pool_update_config({"per_mood": 3}, "Ghost")
    assert not (config.DATA_DIR / "reaction-pool-Ghost.yaml").exists()
    assert not (config.REACTIONS_DIR / "moods-Ghost").exists()
    # A roster bot is fine, and so is a bot that still owns pool files.
    assert reactions.require_bot_id(COMPANION) == COMPANION
    (config.REACTIONS_DIR / "moods-Departed").mkdir(parents=True)
    assert reactions.require_bot_id("Departed") == "Departed"


# --------------------------------------------------------------------------- #
# 6. A pool never offers a file it cannot fire
# --------------------------------------------------------------------------- #


def test_an_unservable_filename_is_never_drawn(rx):
    """A backslash in the name is drawable but unfirable (image_path refuses
    it), so `:react:random:` drew it, failed with a ⚠️ row, and left it in the
    rotation to fail again."""
    _drop("ok.png", folder="mad")
    d = reactions._moods_dir() / "mad"
    (d / "bad\\name.png").write_bytes((d / "ok.png").read_bytes())

    for _ in range(12):
        drawn = reactions.pool_draw()
        assert drawn is not None
        assert "\\" not in drawn.file
        reactions.image_path(drawn)        # would raise on the quarantined one
    assert reactions.pool_categories() == {"mad": 1}


# --------------------------------------------------------------------------- #
# 7. A refund returns the refunder's own slot
# --------------------------------------------------------------------------- #


def test_refund_returns_the_actors_own_global_slot(rx):
    lim = reactions.RateLimiter()
    st = reactions.Settings()
    assert lim.check("bits", st) is None
    assert lim.check("doxy", st) is None
    lim.refund("bits")
    assert [a for _t, a in lim._global] == ["doxy"], \
        "the refund handed back another actor's global slot"
    lim.refund("nobody")                   # unknown actor = no-op
    assert [a for _t, a in lim._global] == ["doxy"]


# --------------------------------------------------------------------------- #
# 8. Replace-batch stamps only a batch that landed
# --------------------------------------------------------------------------- #


def test_an_interrupted_replace_batch_leaves_the_nightly_window_open(rx, monkeypatch):
    """Stamping batch_date BEFORE refilling made an interrupted manual replace
    look like tonight's top-up had run — over an empty shelf."""
    monkeypatch.setattr(reactions, "image_cli_available", lambda: True)
    reactions.bank_save({"categories": {"solo": {"label": "Solo", "prompts": ["p"]}}})
    st = reactions.pool_load()
    st.config.per_mood = 2
    st.config.min_per_mood = 1
    st.config.refresh_hour = 0
    reactions.pool_save(st)
    _drop("old.png", folder="solo")

    monkeypatch.setattr(reactions, "_pool_generate_one",
                        lambda *a, **k: None)          # the rig is down
    monkeypatch.setattr(reactions.pool_guard, "free_vram_before_mint",
                        lambda: {"ok": True, "reason": ""})
    assert reactions.pool_replace_batch() == 0
    assert reactions.pool_load().batch_date == "", "an empty shelf was stamped done"
    assert reactions.pool_status()["due_daily"] is True


# --------------------------------------------------------------------------- #
# 9. The storage card measures every keep-forever store
# --------------------------------------------------------------------------- #


def test_storage_measures_the_avatar_stores(rx):
    """avatar-pool/ (spent pairs are kept) and avatar-snapshots/ grow the same
    way reactions/ does; being unmeasured made "where did the disk go"
    unanswerable from the card that exists to answer it."""
    (config.AVATAR_POOL_DIR / "main" / "spent").mkdir(parents=True)
    (config.AVATAR_POOL_DIR / "main" / "spent" / "a-face.png").write_bytes(b"x" * 512)
    snaps = config.DATA_DIR / "avatar-snapshots"
    snaps.mkdir(parents=True, exist_ok=True)
    (snaps / "s1.png").write_bytes(b"y" * 256)

    dashboard._du_cache.update(ts=0.0, value=None)
    out = dashboard._storage_sync(config.DATA_DIR / "chats.db", fresh=True)
    assert out["avatar_pool"]["bytes"] >= 512
    assert out["avatar_snapshots"]["bytes"] >= 256
    # Still deliberately outside the cap maths, like reactions.
    assert out["blob_bytes"] == out["media"]["bytes"] + out["files"]["bytes"]
