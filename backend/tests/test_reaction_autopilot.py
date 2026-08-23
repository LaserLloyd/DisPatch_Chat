"""Server-side reaction autopilot.

A standing "react on every reply" instruction decays with context depth and
tool load (measured live: 36 consecutive replies with zero markers). The
persist chokepoint therefore fires a heuristic mood when an autopilot-enabled
bot's reply carries none. These tests pin the guarantee AND its guardrails:
the bot's own marker wins, questions/night/serious/sub-rows stay quiet, and
the flag is off unless a bot opts in.
"""
from __future__ import annotations

import yaml
from test_reactions import rx_env

from app import config, main

DAY_HOUR = 14        # a civilised afternoon — outside the night window
LONG = "Crew dispatched and the verification loop is running clean."


def _enable_autopilot(bot_id: str = "main") -> None:
    config.load_bots()          # materialises config.yaml on first use
    raw = yaml.safe_load(config.CONFIG_PATH.read_text()) or {}
    for b in raw.get("bots", []):
        if b.get("id") == bot_id:
            b["reaction_autopilot"] = True
    config.CONFIG_PATH.write_text(yaml.safe_dump(raw))
    config._invalidate_bots_cache()


async def _persist(monkeypatch, thread_id, text, *, hour=DAY_HOUR, metadata=None):
    """Run one assistant persist with broadcast muted and fires captured."""
    fired: list[list[str]] = []

    async def _noop(_frame):
        return None

    async def _capture(ids, tid, bid):
        fired.append(list(ids))

    monkeypatch.setattr(main.manager, "broadcast", _noop)
    monkeypatch.setattr(main, "_fire_marker_reactions", _capture)
    monkeypatch.setattr(main, "_autopilot_now_hour", lambda: hour)
    await main._persist_and_broadcast_message(thread_id, "assistant", text,
                                              metadata=metadata)
    return fired


async def test_autopilot_fires_thinking_on_plain_reply(rx_env, monkeypatch):
    await main.db.connect()
    _enable_autopilot()
    await main.db.create_thread(bot_id="main", thread_id="t-ap")
    fired = await _persist(monkeypatch, "t-ap", LONG)
    assert fired == [["thinking"]]


async def test_autopilot_task_complete_on_done_language(rx_env, monkeypatch):
    await main.db.connect()
    _enable_autopilot()
    await main.db.create_thread(bot_id="main", thread_id="t-ap2")
    fired = await _persist(monkeypatch, "t-ap2",
                           "Deployed and verified end to end — all suites green.")
    assert fired == [["task_complete"]]


async def test_autopilot_morning_for_daily_opener(rx_env, monkeypatch):
    await main.db.connect()
    _enable_autopilot()
    await main.db.create_thread(bot_id="main", thread_id="daily-main-2099-01-01")
    fired = await _persist(monkeypatch, "daily-main-2099-01-01",
                           "Good morning — the overnight chain ran clean.",
                           hour=9)
    assert fired == [["morning"]]
    # The SECOND reply of the day is no longer the opener.
    fired = await _persist(monkeypatch, "daily-main-2099-01-01",
                           "Second update of the morning, still cruising along fine.",
                           hour=9)
    assert fired == [["thinking"]]


async def test_autopilot_respects_the_skip_rules(rx_env, monkeypatch):
    await main.db.connect()
    _enable_autopilot()
    await main.db.create_thread(bot_id="main", thread_id="t-ap3")
    # Question → the ball is in the user's court.
    assert await _persist(monkeypatch, "t-ap3",
                          "Want me to push the deploy now or hold until morning?") == []
    # Night hours stay dark.
    assert await _persist(monkeypatch, "t-ap3", LONG, hour=3) == []
    # Serious moments stay picture-free.
    assert await _persist(monkeypatch, "t-ap3",
                          "Heads up — the gateway outage took the mirror down for an hour.") == []
    # Tiny acks never earned one.
    assert await _persist(monkeypatch, "t-ap3", "On it.") == []
    # Collapsed working notes are not replies.
    assert await _persist(monkeypatch, "t-ap3", LONG, metadata={"sub": True}) == []


async def test_bot_marker_wins_over_autopilot(rx_env, monkeypatch):
    await main.db.connect()
    _enable_autopilot()
    await main.db.create_thread(bot_id="main", thread_id="t-ap4")
    fired = await _persist(monkeypatch, "t-ap4", LONG + " :react:nice:")
    assert fired == [["nice"]]


async def test_autopilot_is_off_by_default(rx_env, monkeypatch):
    await main.db.connect()
    await main.db.create_thread(bot_id="main", thread_id="t-ap5")
    assert await _persist(monkeypatch, "t-ap5", LONG) == []
