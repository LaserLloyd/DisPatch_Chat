"""Gateway-outage resilience: retryable classification + tool-warning demotion.

Covers the 2026-07-29 incident: the gateway was stopped mid-drain and
SIGKILLed, DisPatch turns failed instantly with a raw "main failed (exit 1)"
toast, and a gateway tool-error notice ("⚠️ 🛠️ Exec failed: …") had earlier
been posted into a family thread as if the bot said it.
"""
from __future__ import annotations

import asyncio

import pytest

from app import main, openclaw

WARN = '⚠️ 🛠️ Exec failed: `run ps aux -> search "Z"`'


# --------------------------------------------------------------------------- #
# Classification: gateway-refused turns raise GatewayUnavailable
# --------------------------------------------------------------------------- #


def test_parse_reply_draining_is_gateway_unavailable():
    parsed = {"status": "error",
              "summary": "GatewayDrainingError: Gateway is draining for "
                         "restart; new tasks are not accepted"}
    with pytest.raises(openclaw.GatewayUnavailable):
        openclaw._parse_reply(parsed, "main", "")


def test_parse_reply_ordinary_error_is_not_retryable():
    parsed = {"status": "error", "summary": "model backend exploded"}
    with pytest.raises(openclaw.AgentError) as ei:
        openclaw._parse_reply(parsed, "main", "")
    assert not isinstance(ei.value, openclaw.GatewayUnavailable)


def test_parse_reply_aborted_never_retryable():
    # An aborted turn RAN — retrying could double-run it, even if the abort
    # message happens to mention the gateway going down.
    parsed = {"status": "ok", "summary": "Gateway is draining for restart",
              "result": {"meta": {"aborted": True}}}
    with pytest.raises(openclaw.AgentError) as ei:
        openclaw._parse_reply(parsed, "main", "")
    assert not isinstance(ei.value, openclaw.GatewayUnavailable)


def test_gateway_down_signatures():
    hit = ("GatewayDrainingError: no", "Gateway is draining for restart",
           "connect ECONNREFUSED 127.0.0.1:18789",
           "errorCode=UNAVAILABLE something")
    miss = ("exit status 1", "model timeout", "tool exec failed")
    for s in hit:
        assert openclaw._GATEWAY_DOWN_RE.search(s), s
    for s in miss:
        assert not openclaw._GATEWAY_DOWN_RE.search(s), s


# --------------------------------------------------------------------------- #
# Retry loop
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _not_shutting_down(monkeypatch):
    # An earlier test that drove the app lifespan to completion leaves the
    # module-global shutdown latch set; the retry loop honors it and would
    # (correctly, in production) give up on the first refusal.
    monkeypatch.setattr(main, "_shutting_down", False)


async def test_send_with_gateway_retry_recovers(monkeypatch):
    calls = {"n": 0}

    async def fake_send(*, bot_id, session_key, message):
        calls["n"] += 1
        if calls["n"] < 3:
            raise openclaw.GatewayUnavailable("gateway down", detail="drain")
        return openclaw.AgentReply(
            payloads=[openclaw.AgentPayload(text="hello")], metadata={})

    async def no_sleep(_):
        return None

    monkeypatch.setattr(openclaw, "send_to_agent", fake_send)
    monkeypatch.setattr(main.asyncio, "sleep", no_sleep)
    reply = await main._send_with_gateway_retry("bot", "agent:bot:t", "hi", "t")
    assert calls["n"] == 3
    assert reply.payloads[0].text == "hello"


async def test_send_with_gateway_retry_gives_up(monkeypatch):
    calls = {"n": 0}

    async def fake_send(*, bot_id, session_key, message):
        calls["n"] += 1
        raise openclaw.GatewayUnavailable("gateway down", detail="drain")

    async def no_sleep(_):
        return None

    monkeypatch.setattr(openclaw, "send_to_agent", fake_send)
    monkeypatch.setattr(main.asyncio, "sleep", no_sleep)
    with pytest.raises(openclaw.GatewayUnavailable):
        await main._send_with_gateway_retry("bot", "agent:bot:t", "hi", "t")
    assert calls["n"] == len(main._GATEWAY_RETRY_DELAYS) + 1


async def test_non_gateway_error_is_not_retried(monkeypatch):
    calls = {"n": 0}

    async def fake_send(*, bot_id, session_key, message):
        calls["n"] += 1
        raise openclaw.AgentError("bot failed (exit 1).", detail="boom")

    monkeypatch.setattr(openclaw, "send_to_agent", fake_send)
    with pytest.raises(openclaw.AgentError):
        await main._send_with_gateway_retry("bot", "agent:bot:t", "hi", "t")
    assert calls["n"] == 1


# --------------------------------------------------------------------------- #
# Tool-warning payloads collapse instead of posting as chat bubbles
# --------------------------------------------------------------------------- #


def test_flagged_payload_is_demoted_to_sub():
    parsed = {
        "status": "ok",
        "result": {
            "payloads": [
                {"text": "real reply", "mediaUrl": None},
                {"text": WARN, "mediaUrl": None,
                 "metadata": {"nonTerminalToolErrorWarning": True}},
            ],
            "meta": {},
        },
    }
    reply = openclaw._parse_reply(parsed, "main", "")
    assert [p.sub for p in reply.payloads] == [False, True]


async def test_funnel_demotes_tool_warning(monkeypatch, tmp_path):
    """End-to-end through the delivery funnel: a tool-warning persists with
    metadata.sub=True (collapsed in the UI); real prose stays a normal bubble."""
    import contextlib

    from app.database import Database

    db = Database(tmp_path / "chats.db")
    await db.connect()
    try:
        main.db = db
        main._delivered.clear()
        main._shutting_down = False

        async def _noop(_frame):
            return None
        monkeypatch.setattr(main.manager, "broadcast", _noop)

        await db.create_thread(bot_id="main", thread_id="t1")
        warn = await main._deliver_assistant_text("t1", WARN)
        prose = await main._deliver_assistant_text("t1", "Zombies are gone.")
        assert warn is not None and (warn.metadata or {}).get("sub") is True
        assert prose is not None and not (prose.metadata or {}).get("sub")
    finally:
        with contextlib.suppress(Exception):
            await db.close()


async def test_inject_path_also_demotes(monkeypatch, tmp_path):
    """/api/inject persists via _persist_and_broadcast_message directly — a
    proactively injected tool-warning must collapse there too."""
    import contextlib

    from app.database import Database

    db = Database(tmp_path / "chats.db")
    await db.connect()
    try:
        main.db = db

        async def _noop(_frame):
            return None
        monkeypatch.setattr(main.manager, "broadcast", _noop)

        await db.create_thread(bot_id="main", thread_id="t2")
        msg = await main._persist_and_broadcast_message("t2", "assistant", WARN)
        assert (msg.metadata or {}).get("sub") is True
        user = await main._persist_and_broadcast_message("t2", "user", WARN)
        assert not (user.metadata or {}).get("sub")   # user text is never demoted
    finally:
        with contextlib.suppress(Exception):
            await db.close()


# --------------------------------------------------------------------------- #
# Mirror idle backoff (wasted-compute fix: no 4s full-roster scans while idle)
# --------------------------------------------------------------------------- #


def test_mirror_delay_ramp():
    assert main._mirror_delay(0, 4, 60) == 4.0          # active → base poll
    assert main._mirror_delay(1, 4, 60) == 6.0          # ramps gently
    assert main._mirror_delay(10, 4, 60) == 24.0
    assert main._mirror_delay(28, 4, 60) == 60.0        # capped at idle max
    assert main._mirror_delay(10_000, 4, 60) == 60.0    # stays capped
    assert main._mirror_delay(5, 4, 4) == 4.0           # idle_max <= base → flat


def test_mirror_nudge_bumps_sequence():
    before = main._mirror_nudge_seq
    main._mirror_nudge()
    assert main._mirror_nudge_seq == before + 1


# --------------------------------------------------------------------------- #
# A trailing tool warning must not silence the agent's actual reply
# --------------------------------------------------------------------------- #

def test_trailing_tool_warning_does_not_demote_the_real_reply():
    """Reported from the family chat: an entire turn went invisible.

    Multi-payload replies collapse everything except the LAST payload to "sub"
    (collapsed working output). When the runtime appends a tool warning
    ("⚠️ 🛠️ Exec failed: …") AFTER the agent speaks, that warning takes the
    last slot — so the warning is demoted for being a warning, and the real
    message is demoted for not being last. Both hidden; nothing appears in the
    chat at all.

    The observed case ended in a direct question to the operator, which was
    therefore never seen and never answered.
    """
    from app import main, openclaw_text

    class P:
        def __init__(self, text, sub=False):
            self.text, self.sub, self.media_url = text, sub, None

    real = "Waiting on Swift's thread count. What's your vision for Scout's style?"
    warning = "⚠️ 🛠️ Exec failed: `show ~/notes/x.yaml` (exit 1)"
    payloads = [P("Scoping it now."), P(real), P(warning)]

    # Mirror the selection main.py makes.
    def is_speech(p):
        return bool((p.text or "").strip()) and not openclaw_text.is_tool_warning(p.text or "")

    speech = [i for i, p in enumerate(payloads) if is_speech(p)]
    last = speech[-1] if speech else len(payloads) - 1

    assert last == 1, f"the real reply is at index 1, selection chose {last}"
    assert not payloads[last].sub, "the real reply would be collapsed"
    # ...and the warning still collapses, via the same rule it always used.
    assert openclaw_text.is_tool_warning(warning)


def test_a_turn_that_is_only_a_tool_warning_still_persists_something():
    """Degenerate case: no speech at all. The selection must not crash or drop
    the turn entirely — falling back to the final payload keeps prior
    behaviour."""
    from app import main, openclaw_text

    class P:
        def __init__(self, text):
            self.text, self.sub, self.media_url = text, False, None

    payloads = [P("⚠️ 🛠️ Exec failed: `a`"), P("⚠️ 🛠️ Exec failed: `b`")]

    def is_speech(p):
        return bool((p.text or "").strip()) and not openclaw_text.is_tool_warning(p.text or "")

    speech = [i for i, p in enumerate(payloads) if is_speech(p)]
    last = speech[-1] if speech else len(payloads) - 1
    assert last == 1, "fell back to something other than the final payload"


# --------------------------------------------------------------------------- #
# The backstop for answers every live path missed
# --------------------------------------------------------------------------- #

def test_follow_window_comfortably_exceeds_the_gateway_subagent_budget():
    """The 30-minute window was not merely too short — it RACED by construction.

    A delegated turn writes zero transcript bytes while it waits, so the
    "silence" clock runs out during exactly the case the follower exists for.
    The gateway's own subagent budget is 1800s, so a subagent using its full
    allowance lands its announce at or after a 1800s deadline every time.

    Observed on 2026-08-07: the gap was 1808.997s. Nine seconds. Seventeen
    assistant blocks, including the finished answer, never reached the chat.
    """
    from app import main

    gateway_subagent_budget_s = 1800
    assert main.FOLLOW_WINDOW_S >= 2 * gateway_subagent_budget_s, (
        f"FOLLOW_WINDOW_S={main.FOLLOW_WINDOW_S} does not clear the gateway's "
        f"{gateway_subagent_budget_s}s subagent budget with margin")


def test_a_gap_sweep_exists_and_is_started():
    """A window can always be missed; the sweep is what makes it recoverable.

    It compares each recent thread's transcript against the database and
    imports the difference, so an answer that every live path missed still
    arrives — late, but arrives.
    """
    from app import main

    assert hasattr(main, "_gap_sweep_loop"), "the backstop is gone"
    # The task must actually be started, not merely defined.
    import inspect
    lifespan_src = inspect.getsource(main.lifespan)
    assert "_gap_sweep_loop" in lifespan_src, (
        "_gap_sweep_loop is defined but never scheduled — a backstop that does "
        "not run is worse than none, because it is believed")


@pytest.mark.asyncio
async def test_gap_sweep_is_idempotent(tmp_path, monkeypatch):
    """Running it twice must not duplicate anything.

    It leans on _import_transcript_messages, which dedups against the WHOLE
    thread history — that is the property that makes a frequent sweep safe.
    """
    from app import config, main
    from app.database import Database

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    db = Database(tmp_path / "t.db")
    await db.connect()
    monkeypatch.setattr(main, "db", db)
    try:
        t = await db.create_thread(bot_id="main", title="sweep")
        await db.add_message(thread_id=t.id, role="assistant", content="Only once.")

        calls = []

        async def fake_import(thread_id, bot_id, **kw):
            calls.append(thread_id)
            return 0                     # nothing new to fill

        monkeypatch.setattr(main, "_import_transcript_messages", fake_import)
        # Drive the body once by calling the importer the way the sweep does.
        threads = await db.all_threads(include_archived=False)
        for th in threads:
            await main._import_transcript_messages(th.id, th.bot_id, mark_followup=True)

        msgs = await db.dump_messages(t.id)
        assert len([m for m in msgs if m.content == "Only once."]) == 1
        assert calls, "the sweep never consulted the importer"
    finally:
        await db.close()


# --------------------------------------------------------------------------- #
# WS backfill after an outage: a replay, and it must dedup like one
# --------------------------------------------------------------------------- #


@pytest.fixture
async def _main_db(tmp_path, monkeypatch):
    from app import config
    from app.database import Database

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    db = Database(tmp_path / "t.db")
    await db.connect()
    monkeypatch.setattr(main, "db", db)
    main._delivered.clear()

    async def _noop(_frame):
        return None

    monkeypatch.setattr(main.manager, "broadcast", _noop)
    yield db
    await db.close()


@pytest.mark.asyncio
async def test_ws_backfill_does_not_replay_an_earlier_turn(_main_db):
    """While the gateway socket is down, the CLI paths still deliver — with no
    gw source_id recorded. On reconnect the router backfills the outage window,
    and identity dedup finds nothing. If another turn has happened since, the
    trailing-run content check stops at its user row and the whole earlier
    turn re-posts. A backfill is a replay of history; it must dedup against
    the WHOLE thread, the same rule every other replay path already follows."""
    db = _main_db
    t = await db.create_thread(bot_id="main", title="outage")
    await db.add_message(t.id, "user", "first question")
    await db.add_message(t.id, "assistant", "Done. The zip is ready.")   # CLI path
    await db.add_message(t.id, "user", "second question")
    await db.add_message(t.id, "assistant", "On it.")              # CLI path

    await main._gateway_deliver(
        t.id, "Done. The zip is ready.",
        source_id="gw:agent:main:outage:aa11bb22", created_at=None,
        bot_id="main", live=False)

    msgs = await db.dump_messages(t.id)
    copies = [m for m in msgs if m.content == "Done. The zip is ready."]
    assert len(copies) == 1, "the backfill replayed an already-delivered turn"

    # A LIVE delivery of the same words after a new user turn is a legitimate
    # repeat ("Done." again) and must still get through — the whole-thread rule
    # applies to replays only.
    await main._gateway_deliver(
        t.id, "On it.",
        source_id="gw:agent:main:outage:cc33dd44", created_at=None,
        bot_id="main", live=True)
    # live re-delivery of the trailing turn's text IS deduped (same turn)…
    msgs = await db.dump_messages(t.id)
    assert len([m for m in msgs if m.content == "On it."]) == 1


@pytest.mark.asyncio
async def test_ws_backfill_does_not_refire_reactions(_main_db, monkeypatch):
    """Reactions are a live, interruptive, single-use side effect; replaying
    history must not have side effects. The sweep already suppresses fires on
    recovered messages — a WS backfill is the same replay arriving on a newer
    transport and was firing them anyway."""
    from types import SimpleNamespace

    db = _main_db
    t = await db.create_thread(bot_id="main", title="refire")
    fired: list[list[str]] = []

    async def _record(ids, thread_id, bot_id, *, autopilot=False):
        fired.append(list(ids))

    monkeypatch.setattr(main, "_fire_marker_reactions", _record)
    # Resolve every marker without touching the real registry (which would
    # seed a starter pack into the throwaway DATA_DIR).
    monkeypatch.setattr(main.reactions, "get",
                        lambda key, *, bot_id=None: SimpleNamespace(id=key))

    await main._gateway_deliver(
        t.id, "Celebrate! :react:task_complete:",
        source_id="gw:agent:main:refire:ee55ff66", created_at=None,
        bot_id="main", live=False)

    msgs = await db.dump_messages(t.id)
    assert any("Celebrate!" in (m.content or "") for m in msgs), \
        "the backfilled message itself must still be delivered"
    assert not any(":react:" in (m.content or "") for m in msgs), \
        "markers are stripped on every path"
    assert fired == [], "a replayed message fired a live reaction"

    # Control: the same marker on a LIVE delivery does fire — otherwise this
    # test would pass in a harness where firing never works at all.
    await main._gateway_deliver(
        t.id, "Landed! :react:task_complete:",
        source_id="gw:agent:main:refire:ff77aa88", created_at=None,
        bot_id="main", live=True)
    assert fired == [["task_complete"]], fired


# --------------------------------------------------------------------------- #
# The CLI's output is bounded
# --------------------------------------------------------------------------- #


def test_a_flooding_cli_is_killed_instead_of_buffered(tmp_path, monkeypatch):
    """`communicate()` buffers everything the child writes before anything
    truncates it, so a runaway `openclaw agent` was an OOM rather than a
    failed turn. The read is capped and the process killed at the ceiling."""
    import dataclasses
    import stat

    from app import config as config_module

    fake = tmp_path / "openclaw"
    fake.write_text("#!/bin/sh\nyes AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\n")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(openclaw, "SETTINGS", dataclasses.replace(
        config_module.SETTINGS, openclaw_bin=str(fake)))
    monkeypatch.setattr(openclaw, "AGENT_OUTPUT_MAX", 256 * 1024)

    async def go():
        with pytest.raises(openclaw.AgentError) as ei:
            await openclaw.send_to_agent("main", "agent:main:t", "hi", timeout=30)
        assert "unreadably large" in ei.value.message
        assert not isinstance(ei.value, openclaw.AgentTimeout)

    asyncio.run(asyncio.wait_for(go(), timeout=20))
