"""Avatar pools — one-shot face/full pairs drawn by new threads.

What must hold for this to ship:

* a pair is only drawable when COMPLETE, and burning it is atomic — two
  racing creators get exactly one winner and the loser redraws;
* the FIRST eligible thread of a day wears the current (daily) face, every
  later one draws its own pair, and a pool-drawn face survives the daily
  rotation's re-pin sweep;
* ineligible creation paths (mirror/import/drop) and pool-less bots behave
  byte-for-byte as before — the feature is invisible until opted into.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient

from app import auth, avatar_pool, avatar_snapshots, config, main
from app.database import Database, local_date

# --------------------------------------------------------------------------- #
# Environment
# --------------------------------------------------------------------------- #


@pytest.fixture
def pool_env(tmp_path, monkeypatch):
    """Isolated data dir with one pool-enabled bot ('main') wearing a pair."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(config, "AVATAR_DIR", tmp_path / "avatars")
    monkeypatch.setattr(auth, "SECURITY_PATH", tmp_path / "security.yaml")
    monkeypatch.setattr(auth, "RECOVERY_PATH", tmp_path / "RECOVERY-CODE.txt")
    auth._sessions.clear()
    auth._cache = None
    auth._fail_count = 0
    auth._fail_until = 0.0
    config._invalidate_bots_cache()
    avatar_pool._state_cache.clear()
    avatar_pool._bank_cache.clear()

    config.AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    (config.AVATAR_DIR / "main-face.png").write_bytes(b"FACE-current-" + uuid.uuid4().hex.encode())
    (config.AVATAR_DIR / "main-full.png").write_bytes(b"FULL-current-" + uuid.uuid4().hex.encode())
    config._write_bots([
        {"id": "alpha", "name": "Alpha", "avatar": "", "emoji": "✨",
         "order": 0, "visible": True, "safe": True},
        {"id": "main", "name": "Nova", "avatar": "main-face.png", "emoji": "⚡",
         "order": 1, "visible": True, "safe": False, "avatar_pool": True},
        {"id": "plain", "name": "Plain", "avatar": "", "emoji": "🔷",
         "order": 2, "visible": True, "safe": False},
    ])
    config._invalidate_bots_cache()
    yield tmp_path


def _seed_pair(bot_id: str, tag: str, where: str = "ready") -> str:
    """A complete hand-dropped pair; returns its stem. This IS the hand-drop
    contract: two files in the folder, no registry write."""
    d = (avatar_pool.ready_dir(bot_id) if where == "ready"
         else avatar_pool.spent_dir(bot_id))
    d.mkdir(parents=True, exist_ok=True)
    stem = f"pair-{tag}"
    (d / f"{stem}-face.png").write_bytes(f"FACE-{tag}-{uuid.uuid4().hex}".encode())
    (d / f"{stem}-full.png").write_bytes(f"FULL-{tag}-{uuid.uuid4().hex}".encode())
    return stem


def _bot(bot_id="main"):
    b = config.get_bot(bot_id)
    assert b is not None
    return b


# --------------------------------------------------------------------------- #
# Pair mechanics
# --------------------------------------------------------------------------- #


def test_hand_dropped_pair_is_instantly_drawable(pool_env):
    _seed_pair("main", "one")
    pair = avatar_pool.draw("main")
    assert pair is not None and pair.stem == "pair-one"


def test_incomplete_pair_is_invisible(pool_env):
    d = avatar_pool.ready_dir("main")
    d.mkdir(parents=True, exist_ok=True)
    (d / "lonely-face.png").write_bytes(b"FACE-no-full")
    assert avatar_pool.draw("main") is None
    # ...and the moment the full half lands, it is stock.
    (d / "lonely-full.png").write_bytes(b"FULL-arrived")
    assert avatar_pool.draw("main") is not None


def test_staging_and_dotfiles_are_never_drawn(pool_env):
    d = avatar_pool.ready_dir("main")
    d.mkdir(parents=True, exist_ok=True)
    (d / "x-face.png.part").write_bytes(b"half-copied")
    (d / "x-full.png").write_bytes(b"FULL")
    (d / ".DS_Store").write_bytes(b"junk")
    assert avatar_pool.draw("main") is None


def test_consume_moves_both_halves_and_is_one_shot(pool_env):
    _seed_pair("main", "burn")
    pair = avatar_pool.draw("main")
    assert avatar_pool.consume("main", pair) is True
    assert avatar_pool.draw("main") is None, "a burnt pair stayed drawable"
    spent = avatar_pool.list_pairs(avatar_pool.spent_dir("main"))
    assert len(spent) == 1, "the pair did not arrive in spent/ whole"
    # Second consume of the same pair = the racing loser: exactly one winner.
    assert avatar_pool.consume("main", pair) is False


def test_racing_consumers_have_exactly_one_winner(pool_env):
    import threading
    _seed_pair("main", "race")
    pair = avatar_pool.draw("main")
    results: list[bool] = []
    barrier = threading.Barrier(4)

    def racer():
        barrier.wait()
        results.append(avatar_pool.consume("main", pair))

    threads = [threading.Thread(target=racer) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(True) == 1, f"{results.count(True)} winners of one pair"


def test_draw_snapshot_burns_and_pins_explicitly(pool_env):
    _seed_pair("main", "only")
    sid, explicit = avatar_pool.draw_snapshot_for_thread(_bot())
    assert sid and explicit is True
    assert avatar_snapshots.path_for(sid) is not None
    assert avatar_snapshots.path_for_full(sid) is not None, "the pair lost its full half"
    assert avatar_pool.draw("main") is None, "the drawn pair was not burnt"


def test_dry_pool_reuses_a_spent_pair(pool_env):
    _seed_pair("main", "used", where="spent")
    sid, explicit = avatar_pool.draw_snapshot_for_thread(_bot())
    assert sid and explicit is True
    # Nothing was burnt — the spent pair is still there for the next dry draw.
    assert len(avatar_pool.list_pairs(avatar_pool.spent_dir("main"))) == 1


def test_empty_everything_falls_back_to_no_draw(pool_env):
    sid, explicit = avatar_pool.draw_snapshot_for_thread(_bot())
    assert sid is None and explicit is False


def test_pool_less_bot_never_draws(pool_env):
    _seed_pair("main", "notyours")
    sid, explicit = avatar_pool.draw_snapshot_for_thread(_bot("plain"))
    assert sid is None and explicit is False
    assert avatar_pool.draw("main") is not None, "someone else's pool was touched"


# --------------------------------------------------------------------------- #
# Thread creation: daily-first, then the pool
# --------------------------------------------------------------------------- #


def _run(coro):
    return asyncio.run(coro)


def test_first_thread_wears_daily_face_then_pool_draws(pool_env, tmp_path):
    _seed_pair("main", "a")
    _seed_pair("main", "b")

    async def scenario():
        db = Database(tmp_path / "t.db")
        await db.connect()
        try:
            t1 = await db.create_thread("main", avatar_from_pool=True)
            t2 = await db.create_thread("main", avatar_from_pool=True)
            t3 = await db.create_thread("main", avatar_from_pool=True)
            return t1, t2, t3
        finally:
            await db.close()

    t1, t2, t3 = _run(scenario())
    daily_sid = avatar_snapshots.snapshot_id(_bot())
    assert t1.avatar_snapshot == daily_sid, "thread #1 must wear the daily face"
    assert t2.avatar_snapshot != daily_sid, "thread #2 must draw from the pool"
    assert t3.avatar_snapshot != daily_sid
    assert t2.avatar_snapshot != t3.avatar_snapshot, "two draws shared one pair"
    assert avatar_pool.draw("main") is None, "both pairs should now be burnt"


def test_daily_slot_resets_on_date_rollover(pool_env, tmp_path):
    _seed_pair("main", "a")

    async def scenario():
        db = Database(tmp_path / "t.db")
        await db.connect()
        try:
            assert await db._claim_daily_face("main") is True
            assert await db._claim_daily_face("main") is False, "the slot was claimed twice"
            # Yesterday's claim goes stale at midnight.
            await db.db.execute(
                "UPDATE avatar_pool_daily SET used_date = '2000-01-01' WHERE bot_id = 'main'")
            assert await db._claim_daily_face("main") is True
        finally:
            await db.close()

    _run(scenario())


def test_ineligible_paths_and_pool_less_bots_never_draw(pool_env, tmp_path):
    _seed_pair("main", "keep")

    async def scenario():
        db = Database(tmp_path / "t.db")
        await db.connect()
        try:
            # Default (mirror/import/drop shape): no draw, no daily claim.
            t = await db.create_thread("main")
            assert t.avatar_snapshot == avatar_snapshots.snapshot_id(_bot())
            # A pool-less bot on the eligible path: plain capture too.
            await db.create_thread("plain", avatar_from_pool=True)
        finally:
            await db.close()

    _run(scenario())
    assert avatar_pool.draw("main") is not None, "an ineligible creation burnt a pair"


def test_pool_drawn_face_survives_the_rotation_repin(pool_env, tmp_path):
    """The daily rotation re-pins message-less threads to the new face; a
    pool draw is the thread's OWN picture and must not be swept up."""
    _seed_pair("main", "mine")

    async def scenario():
        db = Database(tmp_path / "t.db")
        await db.connect()
        try:
            t1 = await db.create_thread("main", avatar_from_pool=True)  # daily face
            t2 = await db.create_thread("main", avatar_from_pool=True)  # pool draw
            # The avatar rotates: every unused unpinned thread re-pins.
            repinned = await db.repin_unused_thread_avatars("main", "newface.png")
            assert t1.id in repinned, "the daily-face thread should follow the rotation"
            assert t2.id not in repinned, "a pool-drawn thread was overwritten"
            return t2
        finally:
            await db.close()

    _run(scenario())


# --------------------------------------------------------------------------- #
# Config, bank, status
# --------------------------------------------------------------------------- #


def test_config_round_trip_and_clamps(pool_env):
    cfg = avatar_pool.update_config({"target": 7, "min_ready": 99}, "main")
    assert cfg.target == 7
    assert cfg.min_ready == 7, "min_ready must clamp to target"
    assert avatar_pool.load_state("main").config.target == 7


def test_unknown_config_key_is_refused_before_the_write(pool_env):
    with pytest.raises(avatar_pool.PoolError):
        avatar_pool.update_config({"per_mood": 5}, "main")
    assert avatar_pool.load_state("main").config.target == 20, "a refused PUT persisted"


def test_bad_bot_id_cannot_name_a_path(pool_env):
    with pytest.raises(avatar_pool.PoolError):
        avatar_pool.status("../../etc")


def test_bank_round_trip_and_compose(pool_env):
    assert avatar_pool.compose_prompt("main") is None, "an empty bank must not compose"
    avatar_pool.bank_save({"categories": {
                               "neutral": {"label": "Neutral",
                                           "expressions": ["neutral expression, cool violet gaze"],
                                           "prompts": ["full pre-composed prompt"]}},
                           "crop_size": 1024, "face_percent": 0.55,
                           "face_y_percent": 0.45,
                           "workflow": "anima-Rev8-26", "ratio": "1:1",
                           "background": "clean black background"}, "main")
    p = avatar_pool.compose_prompt("main")
    assert p is not None and p.startswith("masterpiece, best quality")  # Tier 1
    assert "neutral expression, cool violet gaze" in p                  # body
    assert p.endswith("clean black background")                         # bank background


def test_a_bank_with_a_base_is_its_own_character(pool_env, monkeypatch):
    """The defect this exists for: compose_prompt shelled out to bits-prompt
    for EVERY bot, so a second bot's pool minted the first bot's face. A bank
    that names a character composes from that character and never calls out."""
    def _never(*flags):
        raise AssertionError(f"bits-prompt was called with {flags}")
    monkeypatch.setattr(avatar_pool, "_bits_prompt", _never)
    avatar_pool.bank_save({"base": "a tall red-haired instructor in a gym",
                           "suffix": "photoreal, 85mm portrait lens",
                           "categories": {"smirk": {"label": "Smirk",
                                                    "expressions": ["one eyebrow raised"]}},
                           "background": "clean gym background"}, "main")
    p = avatar_pool.compose_prompt("main")
    assert p == ("a tall red-haired instructor in a gym, photoreal, 85mm portrait lens, "
                 "one eyebrow raised, clean gym background")


def test_the_identity_keys_survive_a_bank_round_trip(pool_env):
    """_clean_bank used to drop base/suffix, which is why the composer could
    not have used them even if it had wanted to."""
    avatar_pool.bank_save({"base": "someone specific", "suffix": "soft light",
                           "negative": "blurry, extra limbs",
                           "categories": {"calm": {"label": "Calm",
                                                   "prompts": ["at a desk"]}}}, "main")
    bank = avatar_pool.bank_load("main")
    assert bank["base"] == "someone specific"
    assert bank["suffix"] == "soft light"
    assert bank["negative"] == "blurry, extra limbs"
    assert bank["identity_source"] == "bank", "the default is the bank's own base"


def test_a_bank_with_no_base_still_uses_the_prompt_helper(pool_env, monkeypatch):
    """Unchanged behaviour for a bank that names nobody — it holds expression
    bodies only, so the character has to come from somewhere."""
    monkeypatch.setattr(avatar_pool, "_bits_prompt",
                        lambda *f: "tier1 identity" if "--tier1" in f else "tier2 detail")
    avatar_pool.bank_save({"categories": {"calm": {"label": "Calm",
                                                   "expressions": ["a level gaze"]}},
                           "background": "clean black background"}, "main")
    assert avatar_pool.compose_prompt("main") == (
        "tier1 identity, tier2 detail, a level gaze, clean black background")


def test_identity_source_opts_a_bank_back_in_to_the_helper(pool_env, monkeypatch):
    """The escape hatch for a bank whose `base` is only PART of what the helper
    emits: avatar-prompts-main.yaml carries Tier 1 alone, so composing from it
    would silently drop the Tier 2 fragments."""
    monkeypatch.setattr(avatar_pool, "_bits_prompt",
                        lambda *f: "tier1 identity" if "--tier1" in f else "tier2 detail")
    avatar_pool.bank_save({"base": "tier1 identity", "identity_source": "bits-prompt",
                           "categories": {"calm": {"label": "Calm",
                                                   "expressions": ["a level gaze"]}},
                           "background": "clean black background"}, "main")
    bank = avatar_pool.bank_load("main")
    assert avatar_pool.bank_owns_identity(bank) is False
    assert avatar_pool.compose_prompt("main") == (
        "tier1 identity, tier2 detail, a level gaze, clean black background")


def test_an_unknown_identity_source_falls_back_to_the_bank(pool_env, monkeypatch):
    """A typo must not silently hand the bot somebody else's face."""
    monkeypatch.setattr(avatar_pool, "_bits_prompt",
                        lambda *f: (_ for _ in ()).throw(AssertionError("helper called")))
    avatar_pool.bank_save({"base": "someone specific", "identity_source": "wat",
                           "categories": {"calm": {"label": "Calm",
                                                   "prompts": ["at a desk"]}},
                           "background": ""}, "main")
    assert avatar_pool.bank_load("main")["identity_source"] == "bank"
    assert avatar_pool.compose_prompt("main") == (
        "someone specific, at a desk, clean black background")   # the default background


def test_a_flag_shaped_identity_value_is_refused_at_the_write(pool_env):
    cats = {"calm": {"label": "Calm", "prompts": ["at a desk"]}}
    for key in ("base", "suffix", "negative"):
        with pytest.raises(avatar_pool.PoolError) as e:
            avatar_pool.bank_save({key: "--output=/tmp", "categories": cats}, "main")
        assert e.value.status == 400, key


def test_an_empty_bank_still_refuses_to_compose(pool_env):
    """Unchanged: no categories is the loud 'no-prompt-bank' path, and the
    empty bank now reports the identity keys too so the GET shape is stable."""
    bank = avatar_pool.bank_load("main")
    assert bank["categories"] == [] and bank["base"] == ""
    assert bank["identity_source"] == "bank" and bank["negative"] == ""
    assert avatar_pool.compose_prompt("main") is None


def test_deficit_and_low_water(pool_env):
    avatar_pool.update_config({"target": 3, "min_ready": 2}, "main")
    _seed_pair("main", "one")
    assert avatar_pool.deficit("main") == 2
    assert avatar_pool.needs_refill("main") is True
    assert avatar_pool.deficit("main", only_low=True) == 2
    _seed_pair("main", "two")
    assert avatar_pool.needs_refill("main") is False
    assert avatar_pool.deficit("main", only_low=True) == 0, "only_low must gate on the low-water mark"
    assert avatar_pool.deficit("main") == 1


def test_status_shape(pool_env):
    _seed_pair("main", "s")
    _seed_pair("main", "t", where="spent")
    st = avatar_pool.status("main")
    assert st["ready"] == 1 and st["spent"] == 1
    assert st["bot_id"] == "main" and st["deficit"] == 19
    assert st["has_prompts"] is False


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #


@pytest.fixture
def client(pool_env, tmp_path, monkeypatch):
    temp_db = Database(tmp_path / "chats.db")
    monkeypatch.setattr(main, "db", temp_db)
    with TestClient(main.app, client=("127.0.0.1", 50000)) as c:
        yield c
    asyncio.run(temp_db.close())


BROWSER = {"Sec-Fetch-Site": "same-origin", "Origin": "http://testserver"}


def _unlock(client, pin="4321"):
    auth.set_pin(pin)
    r = client.post("/api/auth/unlock", json={"pin": pin})
    assert r.status_code == 200, r.text


def test_routes_serve_machines_and_full_sessions_only(client):
    _seed_pair("main", "r")
    auth.set_pin("4321")               # a lock exists; nobody has unlocked
    # A machine caller (no browser fetch metadata) — the watchdog CLI's view —
    # rides the inbound exemption even while the app is locked.
    r = client.get("/api/avatar-pool")
    assert r.status_code == 200
    assert r.json()["pools"]["main"]["ready"] == 1
    # A sessionless BROWSER is Safe Mode even on loopback: refused.
    assert client.get("/api/avatar-pool", headers=BROWSER).status_code in (401, 403)


def test_locked_browser_cannot_manage_the_pool(client):
    auth.set_pin("4321")               # PIN set, NOT unlocked -> decoy session
    r = client.get("/api/avatar-pool", headers=BROWSER)
    assert r.status_code in (401, 403)
    r = client.put("/api/avatar-pool/main", headers=BROWSER, json={"target": 1})
    assert r.status_code in (401, 403)


def test_unlocked_browser_manages_the_pool(client):
    _unlock(client)
    r = client.put("/api/avatar-pool/main", headers=BROWSER,
                   json={"values": {"target": 9}})
    assert r.status_code == 200, r.text
    assert r.json()["pool"]["target"] == 9
    r = client.put("/api/avatar-pool/main", headers=BROWSER,
                   json={"values": {"bogus_knob": 1}})
    assert r.status_code == 400


def test_prompts_round_trip_over_the_api(client):
    # v5 bank: categories of expressions/prompts. The v1 base+variations shape
    # is refused (a bank with no usable categories 400s, tested below).
    bank = {"categories": {"calm": {"label": "Calm",
                                    "expressions": ["a relaxed smile"],
                                    "prompts": ["the companion at a desk"]}}}
    r = client.put("/api/avatar-pool/main/prompts", json={"prompts": bank})
    assert r.status_code == 200, r.text
    r = client.get("/api/avatar-pool/main/prompts")
    assert r.status_code == 200
    cats = r.json()["prompts"]["categories"]
    assert [c["name"] for c in cats] == ["calm"]
    assert cats[0]["prompts"] == ["the companion at a desk"]


def test_prompts_round_trip_carries_the_identity(client):
    """A bank PUT over the API is what an on-box agent writes; if `base` does
    not come back out of the GET, the bot has no face of its own."""
    bank = {"base": "a specific character", "suffix": "soft rim light",
            "identity_source": "bank", "negative": "blurry",
            "categories": {"calm": {"label": "Calm", "expressions": ["a level gaze"]}}}
    r = client.put("/api/avatar-pool/main/prompts", json={"prompts": bank})
    assert r.status_code == 200, r.text
    saved = r.json()["prompts"]
    assert saved["base"] == "a specific character" and saved["suffix"] == "soft rim light"
    got = client.get("/api/avatar-pool/main/prompts").json()["prompts"]
    assert got["base"] == "a specific character"
    assert got["identity_source"] == "bank" and got["negative"] == "blurry"


def test_prompts_put_refuses_a_v1_bank(client):
    """The v1 'base'+'variations' schema is gone in v5 — a bank with no usable
    categories must be refused loudly, not saved as something degenerate."""
    r = client.put("/api/avatar-pool/main/prompts",
                   json={"prompts": {"base": "the companion", "variations": ["at a desk"]}})
    assert r.status_code == 400


def test_prompts_put_refuses_a_pool_less_bot(client):
    """A bank written for a bot with no avatar pool is a file nothing reads —
    the 200 made a typo'd bot id look like it had worked."""
    r = client.put("/api/avatar-pool/plain/prompts",
                   json={"prompts": {"base": "someone"}})
    assert r.status_code == 404
    assert client.get("/api/avatar-pool/plain/prompts").status_code in (200, 404)


def test_refill_refuses_a_pool_less_bot(client, monkeypatch):
    monkeypatch.setattr("app.main.reactions.image_cli_available", lambda: True)
    r = client.post("/api/avatar-pool/plain/refill")
    assert r.status_code == 404


def test_new_thread_via_api_draws_from_the_pool(client):
    """The whole feature through the REST path a browser uses: first thread
    of the day wears the daily face, the second draws its own pair."""
    _seed_pair("main", "api")
    _unlock(client)
    r1 = client.post("/api/threads", headers=BROWSER, json={"bot_id": "main"})
    r2 = client.post("/api/threads", headers=BROWSER, json={"bot_id": "main"})
    assert r1.status_code == 200 and r2.status_code == 200
    t1, t2 = r1.json(), r2.json()
    daily_sid = avatar_snapshots.snapshot_id(_bot())
    assert t1["avatar_snapshot"] == daily_sid
    assert t2["avatar_snapshot"] not in (None, "", daily_sid)
    assert avatar_pool.draw("main") is None, "the API path did not burn the pair"


# --------------------------------------------------------------------------- #
# Oversized halves are refused by STAT, not by reading them first
# --------------------------------------------------------------------------- #


def test_snapshot_pair_stats_before_reading(pool_env, monkeypatch):
    """`_snapshot_pair` used to read_bytes() both halves and only then let the
    store reject them on size — so a hand-dropped huge file in ready/ was
    pulled wholly into memory to be thrown away. It stats first now."""
    from app import avatar_snapshots

    monkeypatch.setattr(avatar_snapshots, "MAX_SNAPSHOT_BYTES", 64)
    monkeypatch.setattr(avatar_snapshots, "MAX_FULL_BYTES", 64)
    stem = _seed_pair("main", "huge")
    face = avatar_pool.ready_dir("main") / f"{stem}-face.png"
    face.write_bytes(b"F" * 4096)

    reads: list[str] = []
    real_read = avatar_pool.Path.read_bytes

    def spy(self, *a, **kw):
        reads.append(self.name)
        return real_read(self, *a, **kw)

    monkeypatch.setattr(avatar_pool.Path, "read_bytes", spy)
    pair = next(p for p in avatar_pool.list_pairs(avatar_pool.ready_dir("main")))
    assert avatar_pool._snapshot_pair(pair) is None
    assert not reads, f"an oversized half was read into memory: {reads}"


# --------------------------------------------------------------------------- #
# Stale last_error (OC-08)
# --------------------------------------------------------------------------- #


def test_full_pool_clears_a_stale_error(pool_env, monkeypatch):
    """A pool at target reports NO error, even one recorded weeks ago.

    Regression for the observed symptom: the pool watchdog kept reporting
    `error=image-cli-unavailable` for two bots long after the CLI came back,
    because only a successful MINT cleared last_error — and a full pool never
    mints. The watchdog and the human both saw a phantom failure.
    """
    monkeypatch.setattr(avatar_pool, "image_cli_available", lambda: True)
    st = avatar_pool.load_state("main")
    st.config.target = 2
    st.last_error = "image-cli-unavailable"
    avatar_pool.save_state(st, "main")
    for i in range(2):                       # at target — nothing to generate
        _seed_pair("main", f"full{i}")

    assert avatar_pool.refill(bot_id="main") == 0
    assert avatar_pool.load_state("main").last_error == ""


def test_cli_back_clears_a_cli_error_even_with_work_to_do(pool_env, monkeypatch):
    """The CLI answering disproves a CLI fault immediately — the reset must not
    wait for a pair to land, or a rig that is up but busy keeps the stale text."""
    monkeypatch.setattr(avatar_pool, "image_cli_available", lambda: True)
    monkeypatch.setattr(avatar_pool, "generate_pair", lambda bot_id: None)
    st = avatar_pool.load_state("main")
    st.config.target = 3
    st.last_error = "image-cli-missing (/nope/clawforge)"
    avatar_pool.save_state(st, "main")
    _seed_pair("main", "one")                # deficit of 2 — real work queued

    avatar_pool.refill(bot_id="main")
    # The CLI error is gone; generation still failed, so the round says so.
    assert avatar_pool.load_state("main").last_error == "generation-failed"


def test_a_config_defect_survives_a_full_pool(pool_env, monkeypatch):
    """`no-prompt-bank` is not a failed attempt, it is a missing input: a full
    pool does not disprove it, and hiding it would just defer the surprise."""
    monkeypatch.setattr(avatar_pool, "image_cli_available", lambda: True)
    st = avatar_pool.load_state("main")
    st.config.target = 1
    st.last_error = "no-prompt-bank"
    avatar_pool.save_state(st, "main")
    _seed_pair("main", "only")

    assert avatar_pool.refill(bot_id="main") == 0
    assert avatar_pool.load_state("main").last_error == "no-prompt-bank"
