"""The advisor's hand-off card, absorbed by the task model (``agents.unified_dispatch``).

With the flag on, a ``[[handoff:<agent>|brief]]`` card is mirrored as a task held in
``queued``; the owner's Send (unchanged: the card button, ``advisor.send_handoff``) runs it as
a visible, budgeted, audited task, and Dismiss cancels it. With the flag off nothing changes.
Driven through the real advisor module with its own test hooks.
"""
from __future__ import annotations

import asyncio

import pytest
from agents_world import FakeOpenClaw
from test_advisor import _hooks, _run_with_reply, adv_env

from app import advisor, agents_service, main
from app import agents_store as store

pytestmark = pytest.mark.timeout(60)


def _bind(tmp_path, delivered, mgr):
    async def noop(*a, **kw):
        return None
    h = _hooks(tmp_path, delivered, [], ask=main._advisor_ask_agent)
    h.on_handoff = lambda req: mgr.propose_handoff(req)
    h.on_handoff_dismissed = lambda rid: mgr.dismiss_handoff(rid)
    advisor.bind(h)


async def _card(tmp_path, delivered):
    await advisor.run_turn("t1", "adv", "boiler is due")
    return [m for _, m in delivered if "advisor_handoff" in m][0]["advisor_handoff"]


def test_flag_on_a_card_is_a_held_task_and_send_runs_it(adv_env, monkeypatch):
    tmp_path, _ = adv_env
    monkeypatch.setenv("DISPATCH_AGENTS_UNIFIED_DISPATCH", "1")
    delivered: list = []
    _run_with_reply(monkeypatch, "Want me to sort it?\n[[handoff:lead|Book the service]]")

    async def go():
        mgr = await agents_service.startup()
        try:
            mgr.register_backend("openclaw", FakeOpenClaw())
            _bind(tmp_path, delivered, mgr)
            card = await _card(tmp_path, delivered)
            [task] = store.list_tasks(include_chat=True)
            assert task["state"] == "queued" and task["hold"] == "send" and task["kind"] == "handoff"
            assert task["advisor_request_id"] == card["id"] and task["prompt"] == "Book the service"
            assert task["origin_thread_id"] == "t1" and task["origin_message_id"]       # the card's own message
            mid = advisor.ledger().get(card["id"])["card_message_id"]
            await advisor.send_handoff(card["id"], message_id=mid)
            await asyncio.gather(*list(advisor._tasks))
            await mgr.flush()
            return card["id"], store.get_task(task["task_id"])
        finally:
            await agents_service.shutdown()

    rid, task = asyncio.run(go())
    assert task["state"] == "succeeded" and task["hold"] is None
    assert task["backend"] == "openclaw" and task["agent_name"] == "lead"
    assert advisor.ledger().get(rid)["state"] == "done"            # the advisor's own bookkeeping still ran
    assert any("advisor_report" in m for _, m in delivered)


def test_flag_on_dismiss_cancels_the_task(adv_env, monkeypatch):
    tmp_path, _ = adv_env
    monkeypatch.setenv("DISPATCH_AGENTS_UNIFIED_DISPATCH", "1")
    delivered: list = []
    _run_with_reply(monkeypatch, "Want me to?\n[[handoff:lead|Book the service]]")

    async def go():
        mgr = await agents_service.startup()
        try:
            _bind(tmp_path, delivered, mgr)
            card = await _card(tmp_path, delivered)
            mid = advisor.ledger().get(card["id"])["card_message_id"]
            await advisor.dismiss_handoff(card["id"], message_id=mid)
            await mgr.flush()
            return store.list_tasks(include_chat=True)
        finally:
            await agents_service.shutdown()

    [task] = asyncio.run(go())
    assert task["state"] == "cancelled" and task["reason"] == "dismissed"


def test_flag_off_the_card_is_only_a_card(adv_env, monkeypatch):
    tmp_path, _ = adv_env
    monkeypatch.setenv("DISPATCH_AGENTS_UNIFIED_DISPATCH", "0")
    delivered: list = []
    _run_with_reply(monkeypatch, "Want me to?\n[[handoff:lead|Book the service]]")

    async def go():
        mgr = await agents_service.startup()
        try:
            _bind(tmp_path, delivered, mgr)
            await _card(tmp_path, delivered)
            return store.list_tasks(include_chat=True)
        finally:
            await agents_service.shutdown()

    assert asyncio.run(go()) == []
