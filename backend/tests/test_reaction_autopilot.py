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


async def _persist(monkeypatch, thread_id, text, *, hour=DAY_HOUR, metadata=None,
                   stream=False):
    """Run one assistant persist with broadcast muted and fires captured."""
    fired: list[list[str]] = []

    async def _noop(_frame):
        return None

    async def _capture(ids, tid, bid, *, autopilot=False):
        fired.append(list(ids))

    monkeypatch.setattr(main.manager, "broadcast", _noop)
    monkeypatch.setattr(main, "_fire_marker_reactions", _capture)
    monkeypatch.setattr(main, "_autopilot_now_hour", lambda: hour)
    persist = (main._persist_and_stream_message if stream
               else main._persist_and_broadcast_message)
    await persist(thread_id, "assistant", text, metadata=metadata)
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


# --------------------------------------------------------------------------- #
# The STREAM chokepoint is the one final replies actually take
# --------------------------------------------------------------------------- #

STREAM_LONG = ("Crew dispatched and the verification loop is running clean. " * 6)


async def test_autopilot_fires_on_the_stream_path(rx_env, monkeypatch):
    """_persist_and_stream_message had drifted: no autopilot at all, so the
    path every visible final reply takes never fired one."""
    await main.db.connect()
    _enable_autopilot()
    await main.db.create_thread(bot_id="main", thread_id="t-ap6")
    assert len(STREAM_LONG) >= main._STREAM_MIN_CHARS   # the streaming branch
    fired = await _persist(monkeypatch, "t-ap6", STREAM_LONG, stream=True)
    assert fired == [["thinking"]]


async def test_stream_path_honours_replay_freshness(rx_env, monkeypatch):
    """The other half of the drift: a stale replay must not fire anything."""
    await main.db.connect()
    _enable_autopilot()
    await main.db.create_thread(bot_id="main", thread_id="t-ap7")

    fired: list[list[str]] = []

    async def _noop(_frame):
        return None

    async def _capture(ids, tid, bid, *, autopilot=False):
        fired.append(list(ids))

    monkeypatch.setattr(main.manager, "broadcast", _noop)
    monkeypatch.setattr(main, "_fire_marker_reactions", _capture)
    monkeypatch.setattr(main, "_autopilot_now_hour", lambda: DAY_HOUR)
    await main._persist_and_stream_message(
        "t-ap7", "assistant", STREAM_LONG + " :react:nice:",
        metadata={"recovered": True}, created_at="2020-01-01T00:00:00Z")
    assert fired == [], "a days-old replay fired its reactions"


async def test_stream_path_keeps_the_original_timestamp(rx_env, monkeypatch):
    """_deliver_assistant_text used to drop created_at when stream=True."""
    await main.db.connect()
    await main.db.create_thread(bot_id="main", thread_id="t-ap8")

    async def _noop(_frame):
        return None

    monkeypatch.setattr(main.manager, "broadcast", _noop)
    msg = await main._persist_and_stream_message(
        "t-ap8", "assistant", STREAM_LONG, created_at="2020-01-01T00:00:00Z")
    assert msg.created_at.startswith("2020-01-01")


# --------------------------------------------------------------------------- #
# Autopilot's OWN misfire must not spam the chat
# --------------------------------------------------------------------------- #


async def test_autopilot_falls_back_and_never_notes_a_refusal(rx_env, monkeypatch):
    """Stock installs seed `thinking` only, so autopilot's task_complete /
    morning picks 404'd — leaving a visible "didn't fire" row under nearly
    every reply. It must degrade to a mood that exists, silently."""
    await main.db.connect()
    _enable_autopilot()
    await main.db.create_thread(bot_id="main", thread_id="t-ap9")

    async def _noop(_frame):
        return None

    monkeypatch.setattr(main.manager, "broadcast", _noop)
    monkeypatch.setattr(main, "_autopilot_now_hour", lambda: DAY_HOUR)

    tried: list[str] = []
    notes: list[str] = []

    def _fake_get(key, *, bot_id=None):
        tried.append(key)
        return object() if key == "thinking" else None

    async def _fake_fire(key, **kw):
        return {"key": key}

    async def _note(*a, **kw):
        notes.append(a[1] if len(a) > 1 else "?")

    monkeypatch.setattr(main.reactions, "get", _fake_get)
    monkeypatch.setattr(main, "fire_reaction", _fake_fire)
    monkeypatch.setattr(main, "_note_reaction_refusal", _note)

    # "Deployed … green" routes to the task_complete mood, which is absent.
    await main._persist_and_broadcast_message(
        "t-ap9", "assistant",
        "Deployed and verified end to end — all suites green.")
    assert tried[:2] == ["task_complete", "thinking"], tried
    assert notes == [], "autopilot's own misfire persisted a warning row"


async def test_autopilot_stays_silent_when_nothing_resolves(rx_env, monkeypatch):
    await main.db.connect()
    _enable_autopilot()
    await main.db.create_thread(bot_id="main", thread_id="t-ap10")

    async def _noop(_frame):
        return None

    monkeypatch.setattr(main.manager, "broadcast", _noop)
    monkeypatch.setattr(main, "_autopilot_now_hour", lambda: DAY_HOUR)

    tried: list[str] = []
    notes: list[str] = []

    def _fake_get(key, *, bot_id=None):
        tried.append(key)
        return None

    async def _note(*a, **kw):
        notes.append("noted")

    monkeypatch.setattr(main.reactions, "get", _fake_get)
    monkeypatch.setattr(main, "_note_reaction_refusal", _note)

    await main._persist_and_broadcast_message("t-ap10", "assistant", LONG)
    assert tried == ["thinking", "random"], tried
    assert notes == []
    rows, _ = await main.db.list_messages("t-ap10")
    assert not [m for m in rows if "didn't fire" in (m.content or "")]


# --------------------------------------------------------------------------- #
# The empty-body guard is role-agnostic
# --------------------------------------------------------------------------- #


async def test_marker_only_user_message_persists_nothing(rx_env, monkeypatch):
    """`POST /api/inject` with a body of just `:react:random:` used to leave an
    empty bubble: the guard was gated on role == "assistant"."""
    await main.db.connect()
    await main.db.create_thread(bot_id="main", thread_id="t-ap11")

    async def _noop(_frame):
        return None

    monkeypatch.setattr(main.manager, "broadcast", _noop)
    for role in ("user", "system"):
        await main._persist_and_broadcast_message("t-ap11", role, ":react:random:")
    rows, _ = await main.db.list_messages("t-ap11")
    assert rows == []
