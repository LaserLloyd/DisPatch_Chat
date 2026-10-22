"""An advisor's research/handoff markers need a caller holding dispatch_agents (security
review 2026-10-21; ownership plan §3 "[[research]] / [[handoff]]").

A Member chatting with an advisor character could make it start an agent run on the host:
the model writes ``[[research:<agent>|brief]]`` and the advisor dispatched it. Now a turn
whose caller lacks ``dispatch_agents`` runs with dispatch OFF - the locked path for markers
(none offered in the prompt, none acted on) without the other Safe-Mode restrictions - and
the handoff card's Send / Dismiss need ``dispatch_agents`` too. The Owner/Admin are unchanged.
Run: cd backend && uv run pytest tests/test_advisor_dispatch_gate.py
"""
from __future__ import annotations

import asyncio
import json

import pytest
from _extfake import KNOBS, ext_env, install, thread_for  # noqa: F401
from test_advisor import _hooks, _run_with_reply, adv_env  # noqa: F401

from app import advisor, config, extensions, main

ORIGIN = {"origin": "http://testserver"}
ADV = {"providers": [{"provider": "custom", "base_url": "http://p/v1", "model": "m"}],
       "research_agent": "worker", "action_agent": "lead"}


# --------------------------------------------------------------------------- #
# advisor.run_turn(dispatch=False)
# --------------------------------------------------------------------------- #

def _turn(adv_env, monkeypatch, reply, **kw):
    tmp_path, _ = adv_env
    delivered, frames, asked = [], [], []

    async def ask(agent, key, msg, timeout):
        asked.append(agent)
        return "# Found\n\nSomething."

    advisor.bind(_hooks(tmp_path, delivered, frames, ask=ask))
    seen = _run_with_reply(monkeypatch, reply)

    async def go():
        await advisor.run_turn("t1", "adv", "Is rooftop solar worth it?", **kw)
        await asyncio.gather(*list(advisor._tasks))

    asyncio.run(go())
    return delivered, asked, seen


def test_a_caller_without_dispatch_agents_gets_the_reply_but_no_research(adv_env, monkeypatch):
    delivered, asked, seen = _turn(adv_env, monkeypatch,
                                   "Let me look.\n[[research:worker|Current rooftop solar rebates]]",
                                   dispatch=False)
    texts = [t for t, _ in delivered]
    assert texts == ["Let me look."]                              # the reply still lands
    assert asked == []                                            # no agent ran
    assert not any("advisor_request" in m or "advisor_handoff" in m for _, m in delivered)
    assert advisor.ledger().list() == []                          # no request row either
    # The prompt offers no markers and says so - without the Safe-Mode wording.
    assert "[[research:" not in seen["system"]
    assert advisor.NO_DISPATCH_RULES in seen["system"]
    assert "Safe Mode" not in seen["system"]


def test_a_handoff_marker_makes_no_card_without_dispatch_agents(adv_env, monkeypatch):
    delivered, asked, _ = _turn(adv_env, monkeypatch,
                                "Want me to sort it?\n[[handoff:lead|Book the service]]",
                                dispatch=False)
    assert not any("advisor_handoff" in m for _, m in delivered) and asked == []


def test_the_owner_turn_still_dispatches_research(adv_env, monkeypatch):
    delivered, asked, seen = _turn(adv_env, monkeypatch,
                                   "Let me look.\n[[research:worker|Current rooftop solar rebates]]")
    assert asked == ["worker"]
    assert any(t.startswith("🔎 Asked **worker**") for t, _ in delivered)
    assert "[[research:" in seen["system"]
    assert len(advisor.ledger().list()) == 1


def test_dispatch_off_restricts_nothing_else():
    cfg = advisor.AdvisorConfig.from_bot(config.Bot(id="adv", name="Sage", advisor=dict(ADV)))
    off = cfg.dispatch_off()
    assert off.can_dispatch is False and cfg.can_dispatch is True
    assert off.locked is False                       # tools_locked etc. do not apply
    assert off.offered_tools() == cfg.offered_tools()


# --------------------------------------------------------------------------- #
# The flag reaches the advisor from every turn entry point
# --------------------------------------------------------------------------- #

@pytest.fixture
def hh(ext_env, monkeypatch):
    from fastapi.testclient import TestClient
    c, _ = ext_env
    extensions._ext_callers.clear(); extensions._ext_last_closed.clear()
    config.upsert_bot(config.Bot(id="adv", name="Sage", advisor=dict(ADV)))
    install()
    r = c.post("/api/auth/setup", json={"name": "Olive", "password": "owner-pass1", "new_pin": "2468"})
    assert r.status_code == 200, r.text
    made = []

    def member(grants=None, addr=("203.0.113.130", 46001)):
        body = {"name": "Max", "role": "member", "pin": "5555"}
        if grants:
            body["grants"] = grants
        assert c.post("/api/people", json=body).status_code == 201
        k = TestClient(main.app, client=addr)
        k.__enter__()
        k.headers.update({"Origin": "http://testserver"})
        made.append(k)
        assert k.post("/api/auth/login", json={"name": "Max", "pin": "5555"}).status_code == 200
        return k
    try:
        yield c, member
    finally:
        for k in made:
            k.__exit__(None, None, None)
        extensions._ext_callers.clear(); extensions._ext_last_closed.clear()


def _dispatch_of(turn) -> bool:
    opts = turn[3]
    return True if opts is None else getattr(opts, "dispatch", True)


def _send(k, tid, text="hi"):
    with k.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "hello"
        ws.send_json({"type": "send", "thread_id": tid, "text": text, "client_msg_id": "c-" + text})
        for _ in range(8):
            f = ws.receive_json()
            if f.get("type") in ("ack", "error"):
                return f
    return None


def _wait_turns(c, n):
    for _ in range(100):
        if len(c.turns) >= n:
            return
        c.portal.call(asyncio.sleep, 0.02)


def test_a_member_typed_turn_on_an_advisor_runs_with_dispatch_off(hh):
    c, member = hh
    k = member()
    tid = thread_for(k, "adv")
    f = _send(k, tid, "member-asks")
    assert f and f.get("status", "ok") == "ok", f
    _wait_turns(c, 1)
    assert [t[1] for t in c.turns] == ["adv"] and _dispatch_of(c.turns[0]) is False


def test_the_owner_typed_turn_on_an_advisor_keeps_dispatch(hh):
    c, _member = hh
    tid = thread_for(c, "adv")
    _send(c, tid, "owner-asks")
    _wait_turns(c, 1)
    assert [t[1] for t in c.turns] == ["adv"] and _dispatch_of(c.turns[0]) is True


def test_a_member_voice_turn_on_an_advisor_runs_with_dispatch_off(hh):
    c, member = hh
    KNOBS.handler_mode = "submit"
    k = member()
    tid = thread_for(k, "adv")
    with k.websocket_connect(f"/ws/voice/{tid}", headers=ORIGIN) as ws:
        assert json.loads(ws.receive_text())["type"] == "ready"
        ws.send_text("spoken words")
        assert json.loads(ws.receive_text())["id"]
    assert [t[1] for t in c.turns] == ["adv"] and _dispatch_of(c.turns[0]) is False


def test_the_advisor_backend_receives_the_flag(monkeypatch):
    """run_agent_turn -> advisor.run_turn(dispatch=False) only when the turn says so."""
    calls = []

    async def fake_run_turn(thread_id, bot_id, text, **kw):
        calls.append(kw)

    async def no_prefs(tid, opts):
        return opts or main.TurnOptions()

    async def compose(tid, text, opts, gateway):
        return text

    monkeypatch.setattr(main.advisor, "run_turn", fake_run_turn)
    monkeypatch.setattr(main, "_with_thread_model_prefs", no_prefs)
    monkeypatch.setattr(main, "_compose_agent_text", compose)
    monkeypatch.setattr(main.config, "get_bot",
                        lambda b: config.Bot(id="adv", name="Sage", advisor=dict(ADV)))
    asyncio.run(main._run_agent_turn("t", "adv", "x", main.TurnOptions(dispatch=False)))
    asyncio.run(main._run_agent_turn("t", "adv", "x", main.TurnOptions()))
    assert calls[0].get("dispatch") is False and "dispatch" not in calls[1]


# --------------------------------------------------------------------------- #
# The handoff card's Send / Dismiss
# --------------------------------------------------------------------------- #

def test_handoff_send_and_dismiss_need_dispatch_agents(hh):
    c, member = hh
    # Every permission those routes asked for before, but not dispatch_agents.
    k = member(grants=["approve_agent_actions", "manage_characters"])
    me = k.get("/api/auth/me").json()["permissions"]
    assert "approve_agent_actions" in me and "dispatch_agents" not in me
    for verb in ("send", "dismiss"):
        r = k.post(f"/api/advisor/requests/r-1/{verb}", json={"message_id": "m-1"})
        assert r.status_code == 403, (verb, r.status_code, r.text)
        # The Owner gets past the gate (an unknown request is a 404, not a 403).
        assert c.post(f"/api/advisor/requests/r-1/{verb}",
                      json={"message_id": "m-1"}).status_code == 404


def test_a_member_regenerate_on_an_advisor_runs_with_dispatch_off(hh, monkeypatch):
    c, member = hh
    k = member()
    tid = thread_for(k, "adv")
    _send(k, tid, "first-question")
    _wait_turns(c, 1)
    seen = []

    async def fake_regen(thread_id, bot_id, user_msg, **kw):
        seen.append(kw)

    monkeypatch.setattr(main, "_regenerate_turn", fake_regen)
    with k.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "hello"
        ws.send_json({"type": "regenerate", "thread_id": tid})
        for _ in range(100):
            if seen:
                break
            c.portal.call(asyncio.sleep, 0.02)
    assert seen and seen[0].get("dispatch") is False, seen
