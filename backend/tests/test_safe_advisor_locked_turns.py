"""Advisor bots may be Safe-Mode bots (2026-10-13).

The Bot Manager shield was flipped on two advisor bots and silently reverted:
``config.Bot`` forced ``safe`` off for every advisor, and nothing told the
person. The replacement rule is tier-aware instead of bot-wide:

* ``safe: true`` sticks on an advisor like on any other bot;
* a turn that reaches a safe advisor from an UNLOCKED device keeps everything
  (corpus, research and handoff markers);
* a turn that arrives over a Safe-Mode (decoy) connection runs LOCKED: no
  marker is acted on, the dispatch rules leave the prompt, and the bot is
  told research is unavailable — a locked family device never gets to steer
  an exec-capable agent through an LLM-written brief.
"""
from __future__ import annotations

import asyncio

from app import advisor, auth, config, main
from tests.test_advisor import _rec_hooks, _run_with_reply, adv_env  # noqa: F401
from tests.test_socket_turn_round import app_client  # noqa: F401

ADVISOR = {"providers": [{"provider": "custom", "base_url": "http://p/v1", "model": "m"}],
           "research_agent": "worker", "action_agent": "lead"}


# -- 1. the flag sticks ------------------------------------------------------- #

def test_safe_sticks_on_an_advisor(tmp_path, monkeypatch):
    from dataclasses import replace
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.yaml")
    config._invalidate_bots_cache()
    b = config.Bot(id="adv", name="A", safe=True, advisor={"providers": []})
    assert b.safe is True
    assert replace(b, safe=True).safe is True
    (tmp_path / "config.yaml").write_text(
        "bots:\n  - id: adv\n    name: A\n    safe: true\n"
        "    advisor:\n      providers: [{provider: custom, model: m}]\n")
    config._invalidate_bots_cache()
    assert config.get_bot("adv").safe is True
    # The Bot Manager's toggle, both directions.
    config.save_bot_order([{"id": "adv", "order": 0, "safe": False}])
    assert config.get_bot("adv").safe is False
    config.save_bot_order([{"id": "adv", "order": 0, "safe": True}])
    assert config.get_bot("adv").safe is True
    config._invalidate_bots_cache()


# -- 2. a locked turn never dispatches ---------------------------------------- #

def test_a_locked_turn_answers_but_never_dispatches(adv_env, monkeypatch):
    tmp_path, _ = adv_env
    asked = []

    async def ask(agent, key, msg, timeout):
        asked.append(agent)
        return "# r\n\nreport"
    hooks, log = _rec_hooks(tmp_path, ask=ask)
    advisor.bind(hooks)
    seen = _run_with_reply(
        monkeypatch, "Let me check.\n[[research:worker|Something worth researching]]")

    async def go():
        await advisor.run_turn("t1", "adv", "Is rooftop solar worth it?", locked=True)
        await asyncio.gather(*list(advisor._tasks))

    asyncio.run(go())
    texts = [t for _, t, _ in log.delivered]
    assert texts and texts[0].startswith("Let me check."), texts
    assert not any("[[research" in t or "Asked" in t or "Back with" in t for t in texts), texts
    assert asked == [], "a locked turn must never reach an agent"
    assert advisor.ledger().list(thread_id="t1") == [], "no request was ledgered"
    assert "[[research:" not in seen["system"], "the dispatch rules left the prompt"
    assert "Safe Mode" in seen["system"], "the bot is told research is unavailable"
    assert "Sam likes hiking" in seen["system"], "the corpus still reaches the prompt"
    assert "error" not in log.statuses and log.statuses[-1] == "idle"


def test_an_unlocked_turn_on_a_safe_advisor_still_dispatches(adv_env, monkeypatch):
    tmp_path, _ = adv_env
    config.upsert_bot(config.Bot(id="adv", name="Sage", safe=True,
                                 advisor=config.get_bot("adv").advisor))
    assert config.get_bot("adv").safe is True
    asked = []

    async def ask(agent, key, msg, timeout):
        asked.append(agent)
        return "# Findings\n\nreport"
    hooks, log = _rec_hooks(tmp_path, ask=ask)
    advisor.bind(hooks)
    seen = _run_with_reply(
        monkeypatch, "Let me check.\n[[research:worker|Something worth researching]]")

    async def go():
        await advisor.run_turn("t1", "adv", "Is rooftop solar worth it?")
        await asyncio.gather(*list(advisor._tasks))

    asyncio.run(go())
    assert asked == ["worker"]
    assert "[[research:" in seen["system"]
    assert any("Back with" in t for _, t, _ in log.delivered)


# -- 3. the connection's tier reaches the turn --------------------------------- #

def _turn_recorder(monkeypatch):
    turns: list[tuple] = []

    async def _turn(thread_id, bot_id, text, opts=None):
        turns.append((thread_id, bot_id, text, opts))
    monkeypatch.setattr(main, "run_agent_turn", _turn)
    return turns


def _pump(ws, turns, n=20):
    for _ in range(n):
        if turns:
            return
        ws.send_json({"type": "ping"})
        while ws.receive_json().get("type") != "pong":
            pass


def test_a_decoy_send_marks_the_turn_locked(app_client, monkeypatch):
    config.upsert_bot(config.Bot(id="adv", name="Sage", safe=True, advisor=ADVISOR))
    tid = app_client.post("/api/threads", json={"bot_id": "adv"}).json()["id"]
    turns = _turn_recorder(monkeypatch)

    # Unlocked first: the ordinary send path is unchanged (no opts at all).
    with app_client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["decoy"] is False
        ws.send_json({"type": "send", "thread_id": tid, "text": "hi",
                      "client_msg_id": "cm-open"})
        _pump(ws, turns)
    assert turns and turns[0][1] == "adv" and turns[0][3] is None, turns
    turns.clear()

    auth.set_pin("1234")
    app_client.cookies.clear()
    with app_client.websocket_connect("/ws") as ws:
        hello = ws.receive_json()
        assert hello["decoy"] is True
        assert "adv" in {b["id"] for b in hello["bots"]}, "a safe advisor is listed in Safe Mode"
        ws.send_json({"type": "send", "thread_id": tid, "text": "hello there",
                      "client_msg_id": "cm-locked"})
        _pump(ws, turns)
    assert turns and turns[0][1] == "adv", turns
    opts = turns[0][3]
    assert isinstance(opts, main.TurnOptions) and opts.locked is True, opts


def test_a_decoy_retry_marks_the_turn_locked(app_client, monkeypatch):
    config.upsert_bot(config.Bot(id="adv", name="Sage", safe=True, advisor=ADVISOR))
    tid = app_client.post("/api/threads", json={"bot_id": "adv"}).json()["id"]
    app_client.post(f"/api/threads/{tid}/messages", json={"role": "user", "content": "again?"})
    turns = _turn_recorder(monkeypatch)
    auth.set_pin("1234")
    app_client.cookies.clear()
    with app_client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["decoy"] is True
        ws.send_json({"type": "retry", "thread_id": tid})
        _pump(ws, turns)
    assert turns, "the retry never ran"
    opts = turns[0][3]
    assert isinstance(opts, main.TurnOptions) and opts.locked is True, opts


def test_run_agent_turn_hands_locked_to_the_advisor(app_client, monkeypatch):
    config.upsert_bot(config.Bot(id="adv", name="Sage", safe=True, advisor=ADVISOR))
    tid = app_client.post("/api/threads", json={"bot_id": "adv"}).json()["id"]
    got: list[dict] = []

    async def fake_run_turn(thread_id, bot_id, text, **kw):
        got.append(kw)
    monkeypatch.setattr(advisor, "run_turn", fake_run_turn)
    asyncio.run(main._run_agent_turn(tid, "adv", "hi", main.TurnOptions(locked=True)))
    asyncio.run(main._run_agent_turn(tid, "adv", "hi", None))
    assert got[0].get("locked") is True, got
    assert not got[1].get("locked"), got
