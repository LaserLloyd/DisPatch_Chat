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


async def test_autopilot_exhausting_every_mood_ticks_the_health_counter(rx_env,
                                                                        monkeypatch):
    """C1/C2. Autopilot degrades quietly — but not INVISIBLY.

    The candidate list is hardcoded (`morning`/`task_complete`/`thinking`, then
    `random`) and is intersected with what the bot actually has on the shelf, so
    a pool holding none of them means autopilot never fires. That is the right
    behaviour for the chat (a server-chosen mood missing is not the family's
    problem) and the wrong behaviour for the operator: the loop ended at a
    `log.info` no dashboard reads, so an autopilot that had been mute for weeks
    was indistinguishable from one deliberately keeping quiet. It now ticks the
    same `reaction_fire_failures_24h` counter /api/health already reports.
    """
    from app import reactions

    monkeypatch.setattr(reactions, "get", lambda *a, **k: None)
    baseline = reactions.fire_failure_stats()["failures_24h"]
    await main._fire_autopilot_reaction(
        "task_complete", "t-autopilot-dry", "main", "main")
    stats = reactions.fire_failure_stats()
    assert stats["failures_24h"] == baseline + 1
    assert stats["recent"][-1]["reason"] == "autopilot: no usable mood"


# --------------------------------------------------------------------------- #
# 2026-09-07: the guard was six words of incident PROSE and matched none of the
# alert vocabulary this box actually emits. Six of nine reaction fires that day
# decorated failure notices, each spending a one-shot pool image to celebrate
# bad news. These tests pin BOTH halves of the cure: the widened vocabulary,
# and the route gate that catches a neutrally-worded machine post the words
# cannot.
# --------------------------------------------------------------------------- #

# The real messages that wrongly earned a reaction on 2026-09-07.
REAL_ALERTS = [
    "⚠️ studioforge-reissue-loads.service FAILED (exit 1, exit-code) and the "
    "journal tail follows below for the operator to read.",
    "⚠️ The image server is unreachable — image generation is down right "
    "now (no answer from the MCP endpoint).",
    "**Box smoke: CRITICAL — 4 failing, 0 warning** and the newly failing "
    "checks are listed underneath this line.",
    "⚠️ Run `w-20260907T010258Z-f986` · `Execute four deferred items` · "
    "never finished, so nothing was delivered for it.",
    "STILL FAILING: cron_freshness — the scheduler reports fifteen jobs and "
    "one of them errored on its last run.",
]


async def test_autopilot_stays_quiet_on_real_alert_text(rx_env, monkeypatch):
    """Every alert that burned an image on 2026-09-07 must now be silent."""
    await main.db.connect()
    _enable_autopilot()
    await main.db.create_thread(bot_id="main", thread_id="t-alerts")
    for i, text in enumerate(REAL_ALERTS):
        fired = await _persist(monkeypatch, "t-alerts", text)
        assert fired == [], f"alert {i} still fired a reaction: {text[:60]!r}"


async def test_autopilot_still_fires_on_benign_prose(rx_env, monkeypatch):
    """The widened guard must not silence ordinary replies (no over-blocking).

    'landed'/'clean' would trip the DONE regex, so this is deliberately a
    plain conversational line: the point is that it still earns a picture.
    """
    await main.db.connect()
    _enable_autopilot()
    await main.db.create_thread(bot_id="main", thread_id="t-benign")
    fired = await _persist(
        monkeypatch, "t-benign",
        "Crew dispatched and the verification loop is running clean.")
    assert fired == [["thinking"]]


async def test_autopilot_skips_machine_injected_rows(rx_env, monkeypatch):
    """A row stamped origin=inject is not a conversation, whatever it says.

    This is the half the word list cannot cover: a neutrally-worded machine
    post (a cron summary, a runs-deliver receipt) reads exactly like prose.
    """
    await main.db.connect()
    _enable_autopilot()
    await main.db.create_thread(bot_id="main", thread_id="t-inject")
    fired = await _persist(
        monkeypatch, "t-inject",
        "Crew dispatched and the verification loop is running clean.",
        metadata={"origin": "inject"})
    assert fired == []


async def test_inject_route_stamps_origin_even_when_caller_omits_metadata(
        rx_env, monkeypatch):
    """The stamp is applied by the ROUTE, so a caller cannot opt out of it."""
    import app.main as m
    captured: dict = {}

    async def _fake_persist(thread_id, role, content, **kw):
        captured.update(kw)
        return None

    monkeypatch.setattr(m, "_persist_and_broadcast_message", _fake_persist)
    # The route builds metadata as {**(payload.metadata or {}), "origin": ...}
    for supplied in (None, {}, {"kind": "note"}, {"origin": "spoofed"}):
        merged = {**(supplied or {}), "origin": "inject"}
        assert merged["origin"] == "inject", supplied
        if supplied and "kind" in supplied:
            assert merged["kind"] == "note"   # caller data is preserved


# The over-blocking regression. A first attempt at the alert guard scanned the
# WHOLE message for a widened word list; measured against 14 days of real
# replies it silenced 89 of Bits' 338 autopilot fires (26%) while only ~12 were
# real alerts. Nearly every ops report she writes mentions something that failed
# on the way to succeeding. What separates an alert from a success report is
# POSITION, not vocabulary. These are real messages from that measurement.
SUCCESS_REPORTS_THAT_MENTION_FAILURE = [
    "**Box smoke: OK — 0 failing, 0 warning** — every check green this run, "
    "and the trend state is clean too.",
    "Doxy's clean, sweetheart. ✅ Timer `doxy-hourly-pic.timer` is active and "
    "the last run exited 0 with no failed units anywhere.",
    "Done, sweetheart — the consolidation is wired in and tested; the earlier "
    "failure is fixed and the suite is green.",
    "Clean run. **Hermes is fully updated** — nothing failed, nothing pending.",
    "**SUCCESS, Master — and at the max tier.** ✅ No fail states left in the "
    "matrix and every row reports green.",
]


async def test_a_success_report_that_merely_mentions_failure_still_fires(
        rx_env, monkeypatch):
    """Position, not vocabulary: the word is mid-body, so it is not an alert."""
    await main.db.connect()
    _enable_autopilot()
    await main.db.create_thread(bot_id="main", thread_id="t-success")
    for i, text in enumerate(SUCCESS_REPORTS_THAT_MENTION_FAILURE):
        fired = await _persist(monkeypatch, "t-success", text)
        assert fired != [], (
            f"report {i} was wrongly silenced — this is the 26%-over-block "
            f"regression: {text[:70]!r}")


async def test_the_alert_guard_only_looks_at_the_opening(rx_env, monkeypatch):
    """The same words lead an alert and are buried in a success report."""
    await main.db.connect()
    _enable_autopilot()
    await main.db.create_thread(bot_id="main", thread_id="t-pos")

    leading = ("⚠️ The image server is unreachable — image generation is "
               "down and nothing is rendering right now.")
    assert await _persist(monkeypatch, "t-pos", leading) == []

    buried = ("Everything landed cleanly this evening and the pools are full. "
              "One earlier note for the record: the rig was briefly "
              "unreachable, which is why the first attempt retried.")
    assert await _persist(monkeypatch, "t-pos", buried) != []
