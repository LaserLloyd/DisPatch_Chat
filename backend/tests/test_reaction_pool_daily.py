"""Reaction pool: the disable switch mid-fill, and the daily freshness model.

2026-10-10. Two things are pinned here:

* Switching a pool off stops a fill before its NEXT image. A fill used to read
  ``enabled`` once per round (up to max_per_cycle images, ~38 s each on the
  rig) and once per round in main._top_up_pool, so a disable written while a
  long nightly fill was running kept minting for the rest of the round.
* The daily run: per bank mood, ``daily_add_per_mood`` fresh images plus one
  per image FIRED since the previous daily run, capped at the shelf ceiling
  (per_mood / override), retiring the oldest unfired images past it. The fire
  count comes from a ledger written by pool_consume — retirements land in
  spent/ too and must never be counted as fires.

Hermetic: a throwaway data dir and a fake generator, never the image rig.
Run: cd backend && uv run pytest tests/test_reaction_pool_daily.py
"""
from __future__ import annotations

import asyncio
import os
import time

import pytest

from app import auth, config, main, reactions


@pytest.fixture
def rx(tmp_path, monkeypatch):
    """Isolated data dir with a seeded starter pack, a two-mood bank, a
    generator that drops a real file instead of calling the rig, and the VRAM
    guard waved through."""
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

    monkeypatch.setattr(reactions, "image_cli_available", lambda: True)
    monkeypatch.setattr(reactions.pool_guard, "free_vram_before_mint",
                        lambda: {"ok": True, "reason": ""})
    reactions.bank_save({"categories": {
        "alpha": {"label": "Alpha", "prompts": ["a"]},
        "bravo": {"label": "Bravo", "prompts": ["b"]},
    }})
    gen = {"calls": [], "hook": None}

    def _fake_gen(cfg, category, *, bot_id=None):
        gen["calls"].append(category)
        name = f"g{len(gen['calls']):04d}.png"
        _drop(name, mood=category, bot_id=bot_id)
        if gen["hook"]:
            gen["hook"](len(gen["calls"]))
        return reactions.pool_file_id(category, name)

    monkeypatch.setattr(reactions, "_pool_generate_one", _fake_gen)
    yield gen


_clock = [1_000_000_000.0]


def _drop(name: str, *, mood: str, bot_id: str | None = None) -> None:
    """One real image in moods-<bot>/<mood>/, with a strictly increasing mtime
    so "oldest" is well defined however fast the test runs."""
    d = reactions._moods_dir(bot_id) / mood
    d.mkdir(parents=True, exist_ok=True)
    src = next(config.REACTIONS_BUILTIN_DIR.glob("*.png"))
    p = d / name
    p.write_bytes(src.read_bytes())
    _clock[0] += 10
    os.utime(p, (_clock[0], _clock[0]))


def _have(mood: str) -> int:
    return reactions.pool_categories().get(mood, 0)


def _configure(**cfg) -> None:
    st = reactions.pool_load()
    for k, v in cfg.items():
        setattr(st.config, k, v)
    st.config.refresh_hour = 0           # the window is open all day
    st.batch_date = ""
    reactions.pool_save(st)


def _new_day() -> None:
    """Pretend the previous daily run happened on an earlier day."""
    st = reactions.pool_load()
    st.batch_date = "2000-01-01"
    if st.daily_date:
        st.daily_date = "2000-01-01"
    reactions.pool_save(st)


def _run_daily_until_done(max_rounds: int = 50) -> int:
    made = 0
    for _ in range(max_rounds):
        n = reactions.pool_refill(bot_id=None, daily=True)
        made += n
        if n == 0:
            break
    return made


# --------------------------------------------------------------------------- #
# Disable mid-fill
# --------------------------------------------------------------------------- #


def test_disabling_mid_round_stops_before_the_next_image(rx):
    """The reported bug: `enabled: false` written while a round is minting
    must stop it after the image in flight, not after the round."""
    _configure(per_mood=20, min_per_mood=5, max_per_cycle=6)

    def _switch_off(n):
        if n == 1:
            reactions.pool_update_config({"enabled": False}, reactions.resolve_bot_id(None))
    rx["hook"] = _switch_off

    made = reactions.pool_refill()
    assert made == 1, f"kept minting {made - 1} image(s) after the pool was switched off"
    assert len(rx["calls"]) == 1
    # A deliberate stop is not a generation failure.
    assert reactions.pool_load().last_error == ""


def test_hand_edited_disable_is_seen_mid_round(rx):
    """Same, but the way an operator might do it: editing the yaml on disk."""
    _configure(per_mood=20, min_per_mood=5, max_per_cycle=6)
    path = reactions._pool_path(reactions.resolve_bot_id(None))

    def _edit_file(n):
        if n == 2:
            text = path.read_text(encoding="utf-8").replace("enabled: true", "enabled: false")
            path.write_text(text, encoding="utf-8")
            st = os.stat(path)            # make sure the mtime cache sees a change
            os.utime(path, (st.st_atime, st.st_mtime + 5))
    rx["hook"] = _edit_file

    assert reactions.pool_refill() == 2


def test_disabling_daily_run_mid_round_stops_too(rx):
    _configure(per_mood=20, daily_add_per_mood=6, max_per_cycle=6)

    def _switch_off(n):
        if n == 1:
            reactions.pool_update_config({"enabled": False}, reactions.resolve_bot_id(None))
    rx["hook"] = _switch_off
    assert reactions.pool_refill(daily=True) == 1


def test_top_up_loop_stops_between_rounds_when_disabled(rx, monkeypatch):
    """main._top_up_pool re-checks the switch before every round, so a
    stubborn round that keeps reporting progress cannot outlive a disable."""
    monkeypatch.setattr(main, "_shutting_down", False)
    rounds: list[int] = []

    def _fake_refill(limit=None, *, bot_id=None, only_low=False, daily=False):
        rounds.append(1)
        if len(rounds) == 1:
            reactions.pool_update_config({"enabled": False}, reactions.resolve_bot_id(bot_id))
        return 3

    monkeypatch.setattr(reactions, "pool_refill", _fake_refill)

    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(main, "_broadcast_pool_state", _noop)
    made = asyncio.run(main._top_up_pool(max_rounds=12))
    assert len(rounds) == 1 and made == 3


# --------------------------------------------------------------------------- #
# Daily freshness
# --------------------------------------------------------------------------- #


def test_daily_quota_is_fresh_plus_fired_and_stamps_when_paid(rx):
    _configure(per_mood=20, min_per_mood=1, daily_add_per_mood=2, max_per_cycle=6)
    for i in range(3):
        _drop(f"s{i}.png", mood="alpha")
    # Fire one alpha image (the real path), so the ledger holds 1.
    rid = reactions.pool_file_id("alpha", "s0.png")
    assert reactions.pool_consume(rid)
    assert reactions.pool_load().fired == {"alpha": 1}

    made = _run_daily_until_done()
    assert made == 5                       # alpha 2+1, bravo 2
    assert rx["calls"].count("alpha") == 3 and rx["calls"].count("bravo") == 2
    st = reactions.pool_load()
    assert st.batch_date == time.strftime("%Y-%m-%d")
    assert st.daily_owed == {}
    assert st.fired == {}                  # zeroed when the day's quota was taken
    assert not reactions.pool_status()["due_daily"]
    # Stamped: running again today mints nothing.
    assert reactions.pool_refill(daily=True) == 0


def test_a_full_shelf_mints_nothing_and_a_fire_is_replaced_next_day(rx):
    """2026-10-14: the daily quota is capped at the room left on the shelf. A
    full shelf costs nothing (no mint-and-retire churn); an image fired since
    the last run is replaced the next morning, plus fresh ones only while there
    is room. The shelf is the outage buffer, never a treadmill."""
    _configure(per_mood=4, min_per_mood=1, daily_add_per_mood=2, max_per_cycle=10,
               per_mood_overrides={"bravo": 0})          # bravo is switched off
    old = [f"old{i}.png" for i in range(4)]
    for n in old:
        _drop(n, mood="alpha")

    assert _run_daily_until_done() == 0, "a full shelf mints nothing"
    files = sorted(p.name for p in reactions._moods_dir().joinpath("alpha").iterdir())
    assert files == sorted(old), "and retires nothing"
    assert not (config.REACTIONS_SPENT_DIR / "alpha").exists() or not any(
        (config.REACTIONS_SPENT_DIR / "alpha").iterdir())
    assert rx["calls"] == []

    # One fired: next day the quota is that one image (room 1), not add + fired.
    st = reactions.pool_load()
    st.fired = {"alpha": 1}
    reactions.pool_save(st)
    reactions._retire(reactions._moods_dir() / "alpha" / "old0.png")
    _new_day()
    rx["calls"].clear()
    assert _run_daily_until_done() == 1
    assert _have("alpha") == 4 and rx["calls"] == ["alpha"]

    # Room without fires (the operator raised the target): fresh ones fill it,
    # capped by the add. (The call log is NOT cleared here: the fake generator
    # names files by call count, and a reset would overwrite g0001.)
    _configure(per_mood=8, min_per_mood=1, daily_add_per_mood=2, max_per_cycle=10,
               per_mood_overrides={"bravo": 0})
    _new_day()
    assert _run_daily_until_done() == 2
    assert _have("alpha") == 6 and rx["calls"] == ["alpha", "alpha", "alpha"]


def test_daily_quota_is_capped_at_the_ceiling(rx):
    _configure(per_mood=3, min_per_mood=1, daily_add_per_mood=10, max_per_cycle=50,
               per_mood_overrides={"bravo": 0})
    assert _run_daily_until_done() == 3
    assert _have("alpha") == 3


def test_interrupted_daily_run_resumes_without_double_generating(rx):
    _configure(per_mood=20, min_per_mood=1, daily_add_per_mood=3, max_per_cycle=2)
    # One round: 2 of 6 minted, nothing stamped, the rest still owed.
    assert reactions.pool_refill(daily=True) == 2
    st = reactions.pool_load()
    assert st.batch_date == ""
    assert sum(st.daily_owed.values()) == 4
    assert reactions.pool_status()["due_daily"]
    # A fire mid-run belongs to TOMORROW's quota, not today's.
    _drop("x.png", mood="alpha")
    assert reactions.pool_consume(reactions.pool_file_id("alpha", "x.png"))
    # Resume (as the next pool cycle would): exactly the remaining 4.
    assert _run_daily_until_done() == 4
    assert len(rx["calls"]) == 6
    st = reactions.pool_load()
    assert st.batch_date == time.strftime("%Y-%m-%d")
    assert st.fired == {"alpha": 1}


def test_unfinished_quota_carries_into_the_next_day_capped(rx):
    _configure(per_mood=5, min_per_mood=1, daily_add_per_mood=3, max_per_cycle=1,
               per_mood_overrides={"bravo": 0})
    assert reactions.pool_refill(daily=True) == 1           # 2 still owed
    _new_day()
    # 3 fresh + 2 carried = 5, but the shelf already holds 1 of its 5: the
    # quota is the ROOM (4), never a mint that would retire an unused image.
    assert _run_daily_until_done() == 4
    assert _have("alpha") == 5


def test_refill_spent_today_off_ignores_fires(rx):
    _configure(per_mood=20, min_per_mood=1, daily_add_per_mood=1, max_per_cycle=10,
               refill_spent_today=False, per_mood_overrides={"bravo": 0})
    _drop("s.png", mood="alpha")
    assert reactions.pool_consume(reactions.pool_file_id("alpha", "s.png"))
    assert _run_daily_until_done() == 1


def test_disabled_pool_takes_no_daily_snapshot(rx):
    _configure(per_mood=20, daily_add_per_mood=5, enabled=False)
    assert reactions.pool_refill(daily=True) == 0
    st = reactions.pool_load()
    assert st.daily_date == "" and st.daily_owed == {} and rx["calls"] == []


def test_new_fields_are_clamped_and_round_trip(rx):
    bid = reactions.resolve_bot_id(None)
    cfg = reactions.pool_update_config({"daily_add_per_mood": 500,
                                        "refill_spent_today": False}, bid)
    assert cfg.daily_add_per_mood == 100 and cfg.refill_spent_today is False
    cfg = reactions.pool_update_config({"daily_add_per_mood": -4}, bid)
    assert cfg.daily_add_per_mood == 0
    # Hand-edited junk degrades to the default instead of bricking pool_load.
    path = reactions._pool_path(bid)
    text = path.read_text(encoding="utf-8").replace(
        "daily_add_per_mood: 0", "daily_add_per_mood: lots")
    path.write_text(text, encoding="utf-8")
    reactions._pool_cache.clear()
    assert reactions.pool_load().config.daily_add_per_mood == 2
    # Status exposes them for the manager.
    s = reactions.pool_status()
    for k in ("daily_add_per_mood", "refill_spent_today", "fired_since_daily",
              "daily_owed", "daily_date"):
        assert k in s


def test_ledger_survives_junk_in_the_yaml(rx):
    path = reactions._pool_path(reactions.resolve_bot_id(None))
    reactions.pool_save(reactions.pool_load())
    path.write_text(path.read_text(encoding="utf-8")
                    + "fired: {alpha: 2, bravo: nope, '': 3}\ndaily_owed: [1, 2]\n",
                    encoding="utf-8")
    reactions._pool_cache.clear()
    st = reactions.pool_load()
    assert st.fired == {"alpha": 2} and st.daily_owed == {}


def test_pool_cycle_runs_the_daily_quota_when_due(rx, monkeypatch):
    """The nightly branch of main's cycle now drives the daily run."""
    _configure(per_mood=20, min_per_mood=0, daily_add_per_mood=1, max_per_cycle=10)
    monkeypatch.setattr(main, "_shutting_down", False)
    monkeypatch.setattr(reactions, "reaction_bots",
                        lambda: [reactions.resolve_bot_id(None)])

    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(main, "_broadcast_pool_state", _noop)
    asyncio.run(main._reaction_pool_cycle())
    assert sorted(rx["calls"]) == ["alpha", "bravo"]
    assert reactions.pool_daily_done()
