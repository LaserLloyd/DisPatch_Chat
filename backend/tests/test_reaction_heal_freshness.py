"""The reaction pipeline's two silent-failure fixes.

1. Registry self-heal (``reactions.heal_pack``): a pack entry whose blob is
   gone either becomes an alias of its newest surviving kin or leaves the
   registry — 29 dangling ids once sat in the live registry refusing fires
   for weeks while resolving "successfully".
2. Replay freshness (``main._replay_is_fresh``): a recovered/followup reply
   still fires its reactions while it is recent; only genuinely old replays
   (the startup gap sweep re-filing last Friday) stay side-effect-free. The
   blanket suppression ate 204 live fires in two weeks.
3. Refusal visibility: a fire that dies no longer looks identical to one that
   landed — it leaves a collapsed sub row and ticks the health counter.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

from test_reactions import rx_env

from app import config, main, reactions
from app.database import now_iso

# --------------------------------------------------------------------------- #
# heal_pack
# --------------------------------------------------------------------------- #


def _mint_pack_entry(rid: str, *, blob: bool, created_at: str = "2026-08-01") -> None:
    """Register ``rid`` in the pack; with ``blob=False`` the file never exists."""
    fname = f"pack/{rid}-deadbeef.png"
    pack = reactions.load()
    entries = list(pack.reactions)
    entries.append(reactions.Reaction(
        id=rid, name=rid, file=fname, category="generated",
        source="upload", created_at=created_at))
    reactions.save(reactions.Pack(settings=pack.settings, reactions=entries))
    if blob:
        src = next(config.REACTIONS_BUILTIN_DIR.glob("*.png"))
        dst = reactions._root_dir("pack") / f"{rid}-deadbeef.png"
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(src.read_bytes())


def test_heal_aliases_dangling_onto_surviving_kin(rx_env):
    _mint_pack_entry("check_in-6", blob=True)
    _mint_pack_entry("check_in", blob=False)
    out = reactions.heal_pack()
    assert out == {"dangling": 1, "aliased": 1, "dropped": 0}
    pack = reactions.load()
    assert not any(r.id == "check_in" for r in pack.reactions)
    kin = next(r for r in pack.reactions if r.id == "check_in-6")
    assert "check_in" in kin.aliases
    # The old spelling still fires something real.
    got = reactions.get("check_in")
    assert got is not None and got.id == "check_in-6"


def test_heal_drops_dangling_without_kin(rx_env):
    _mint_pack_entry("sasha_var1", blob=False)
    out = reactions.heal_pack()
    assert out["dropped"] == 1 and out["aliased"] == 0
    assert reactions.get("sasha_var1") is None


def test_heal_is_idempotent_and_spares_the_healthy(rx_env):
    before = {r.id for r in reactions.load().reactions}
    assert reactions.heal_pack() == {"dangling": 0, "aliased": 0, "dropped": 0}
    assert {r.id for r in reactions.load().reactions} == before


def test_heal_refuses_to_wipe_a_registry_with_no_survivors(rx_env):
    """An absent reactions dir is a broken mount, not 40 stranded cards."""
    before = config.REACTIONS_PATH.read_text(encoding="utf-8")
    n_before = len(reactions.load().reactions)
    for blob in list(config.REACTIONS_BUILTIN_DIR.glob("*")):
        blob.unlink()
    out = reactions.heal_pack()
    assert out["skipped"] == "too many dangling"
    assert out["dropped"] == 0 and out["aliased"] == 0
    assert config.REACTIONS_PATH.read_text(encoding="utf-8") == before
    reactions.invalidate()
    assert len(reactions.load().reactions) == n_before


def test_heal_refuses_when_a_pack_root_is_unreadable(rx_env):
    before = config.REACTIONS_PATH.read_text(encoding="utf-8")
    import shutil
    shutil.rmtree(config.REACTIONS_BUILTIN_DIR)
    out = reactions.heal_pack()
    assert out["skipped"] == "pack root unreadable"
    assert config.REACTIONS_PATH.read_text(encoding="utf-8") == before


def test_heal_still_drops_a_minority_of_dangling_entries(rx_env):
    """The floor must not disable healing for the case it exists for."""
    _mint_pack_entry("stranded_one", blob=False)
    out = reactions.heal_pack()
    assert out["dropped"] == 1 and "skipped" not in out


def test_heal_prefers_the_newest_kin(rx_env):
    _mint_pack_entry("hype-4", blob=True, created_at="2026-08-02")
    _mint_pack_entry("hype-6", blob=True, created_at="2026-08-09")
    _mint_pack_entry("hype", blob=False)
    reactions.heal_pack()
    newest = next(r for r in reactions.load().reactions if r.id == "hype-6")
    assert "hype" in newest.aliases


# --------------------------------------------------------------------------- #
# Replay freshness
# --------------------------------------------------------------------------- #


def test_replay_freshness_window():
    now = datetime.now(UTC)
    assert main._replay_is_fresh(None) is False   # undated replay: can't prove fresh
    assert main._replay_is_fresh(now.isoformat()) is True
    z_spelling = now.strftime("%Y-%m-%dT%H:%M:%S") + "Z"  # transcript spelling
    assert main._replay_is_fresh(z_spelling) is True
    old = (now - timedelta(seconds=main.REACTION_REPLAY_FRESH_S + 60)).isoformat()
    assert main._replay_is_fresh(old) is False
    assert main._replay_is_fresh("not-a-time") is False  # in doubt → no side effects


async def test_fresh_followup_still_fires(rx_env, monkeypatch):
    """A reply that took the scenic route (follower/backfill) seconds after it
    was generated is live — its reactions must fire."""
    await main.db.connect()
    fired: list[list[str]] = []

    async def _noop(_frame):
        return None

    async def _capture(ids, thread_id, bot_id, *, autopilot=False):
        fired.append(list(ids))

    monkeypatch.setattr(main.manager, "broadcast", _noop)
    monkeypatch.setattr(main, "_fire_marker_reactions", _capture)
    await main.db.create_thread(bot_id="main", thread_id="t-fresh")
    msg = await main._persist_and_broadcast_message(
        "t-fresh", "assistant", "Done. :react:nice:",
        metadata={"followup": True}, created_at=now_iso(), source_id="s-fresh")
    assert fired == [["nice"]]
    assert ":react:" not in msg.content   # marker still stripped


async def test_old_replay_stays_silent(rx_env, monkeypatch):
    """The startup gap sweep re-filing last week must not pop pictures."""
    await main.db.connect()
    fired: list[list[str]] = []

    async def _noop(_frame):
        return None

    async def _capture(ids, thread_id, bot_id, *, autopilot=False):
        fired.append(list(ids))

    monkeypatch.setattr(main.manager, "broadcast", _noop)
    monkeypatch.setattr(main, "_fire_marker_reactions", _capture)
    await main.db.create_thread(bot_id="main", thread_id="t-old")
    stale = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    msg = await main._persist_and_broadcast_message(
        "t-old", "assistant", "Old news. :react:nice:",
        metadata={"followup": True}, created_at=stale, source_id="s-old")
    assert fired == []
    assert ":react:" not in msg.content


# --------------------------------------------------------------------------- #
# Refusal visibility
# --------------------------------------------------------------------------- #


async def test_refused_fire_leaves_a_sub_row(rx_env, monkeypatch):
    """A refusal is swallowed (the reply must land) but not silent: the thread
    gains a collapsed sub row and the health counter ticks."""
    await main.db.connect()

    async def _noop(_frame):
        return None

    monkeypatch.setattr(main.manager, "broadcast", _noop)
    baseline = reactions.fire_failure_stats()["failures_24h"]
    # A thread owned by a bot with reactions OFF: the fire path refuses it.
    await main.db.create_thread(bot_id="NoSuchBot", thread_id="t-refuse")
    await main._persist_and_broadcast_message(
        "t-refuse", "assistant", "Hi there :react:nice:")
    msgs, _ = await main.db.list_messages("t-refuse")
    subs = [m for m in msgs if (m.metadata or {}).get("sub")]
    assert any("didn't fire" in m.content for m in subs), \
        [m.content for m in msgs]
    assert reactions.fire_failure_stats()["failures_24h"] == baseline + 1
