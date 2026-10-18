"""``agents.unified_dispatch``: an OpenClaw chat turn and an advisor hand-off, run as tasks.

Default OFF - these tests drive the ON path against a scripted fake wearing the OpenClaw
backend's name, and prove the OFF path never touches it. (The legacy path itself is covered by
the existing delivery / gateway suites, which run with the flag off.)
"""
from __future__ import annotations

import pytest
from agents_world import FakeOpenClaw, aw, wait_state

from app import agents_service, main, openclaw
from app import agents_store as store

pytestmark = pytest.mark.timeout(60)


@pytest.fixture
def uni(aw, monkeypatch):
    aw.mgr.register_backend("openclaw", FakeOpenClaw())
    # The legacy transport must never be reached with the flag on; with it off, the fake must
    # never be reached. Both are recorded.
    calls = {"legacy": 0}

    async def legacy(*a, **kw):
        calls["legacy"] += 1
        raise openclaw.AgentError("legacy path reached", "")
    monkeypatch.setattr(main, "_send_with_gateway_retry", legacy)
    aw.calls = calls
    thread = aw.owner.post("/api/threads", json={"bot_id": "alpha"}).json()["id"]
    aw.thread = thread
    return aw


def _turn(aw, text="scenario:simple"):
    aw.owner.portal.call(main.run_agent_turn, aw.thread, "alpha", text)


def _assistant_texts(aw):
    rows = aw.owner.get(f"/api/threads/{aw.thread}/messages").json()
    rows = rows["messages"] if isinstance(rows, dict) else rows
    return [m["content"] for m in rows if m["role"] == "assistant"]


def test_the_flag_is_off_by_default_and_the_legacy_path_runs(uni):
    assert agents_service.unified_dispatch_enabled() is False
    _turn(uni)
    assert uni.calls["legacy"] == 1
    assert store.list_tasks(include_chat=True) == []
    status = uni.owner.get(f"/api/threads/{uni.thread}").json()["status"]
    assert status == "error"                       # the legacy stub above refused: nothing silently rerouted


def test_with_the_flag_on_a_chat_turn_runs_as_a_task_and_lands_in_the_thread(uni):
    assert uni.owner.put("/api/agents/settings", json={"unified_dispatch": True}).json()["unified_dispatch"] is True
    _turn(uni)
    assert uni.calls["legacy"] == 0
    assert _assistant_texts(uni) == ["Hello world"]
    t = store.get_task(uni.thread)
    assert t["kind"] == "chat" and t["state"] == "succeeded" and t["backend"] == "openclaw"
    assert uni.owner.get(f"/api/threads/{uni.thread}").json()["status"] == "idle"
    # the next message continues on the same session
    _turn(uni, "and again")
    assert len(_assistant_texts(uni)) == 2
    assert store.get_task(uni.thread)["session_id"] == f"fake-session-{uni.thread}"
    # a chat turn is not a task the person assigned: not on the Tasks page
    assert uni.owner.get("/api/tasks").json()["tasks"] == []
    assert uni.owner.get(f"/api/tasks/{uni.thread}").status_code == 404


def test_an_agent_failure_reaches_the_thread_as_the_usual_error_frame(uni):
    uni.owner.put("/api/agents/settings", json={"unified_dispatch": True})
    frames = []
    with uni.owner.websocket_connect("/ws") as ws:
        ws.receive_json()
        _turn(uni, "scenario:error_refused")
        ws.send_json({"type": "ping"})
        while (f := ws.receive_json()).get("type") != "pong":
            frames.append(f)
    err = [f for f in frames if f.get("type") == "error"]
    assert err and err[0]["thread_id"] == uni.thread and err[0]["refused"] is True
    assert "model not allowed" in err[0]["message"]
    assert uni.owner.get(f"/api/threads/{uni.thread}").json()["status"] == "error"
    # and the thread still works afterwards
    _turn(uni)
    assert _assistant_texts(uni) == ["Hello world"]
    assert uni.owner.get(f"/api/threads/{uni.thread}").json()["status"] == "idle"


def test_the_environment_can_force_the_flag_either_way(uni, monkeypatch):
    uni.owner.put("/api/agents/settings", json={"unified_dispatch": True})
    monkeypatch.setenv("DISPATCH_AGENTS_UNIFIED_DISPATCH", "0")
    assert agents_service.unified_dispatch_enabled() is False
    assert uni.owner.get("/api/agents/settings").json()["unified_dispatch_from_env"] is True
    monkeypatch.setenv("DISPATCH_AGENTS_UNIFIED_DISPATCH", "1")
    uni.owner.put("/api/agents/settings", json={"unified_dispatch": False})
    assert agents_service.unified_dispatch_enabled() is True


def test_the_handoff_task_is_listed_on_the_tasks_page_and_dismissable(uni, monkeypatch):
    monkeypatch.setenv("DISPATCH_AGENTS_UNIFIED_DISPATCH", "1")
    req = {"id": "rqx", "kind": "handoff", "agent": "lead", "brief": "Rotate the keys",
           "thread_id": uni.thread, "card_message_id": "m1"}
    row = uni.owner.portal.call(uni.mgr.propose_handoff, req)
    [listed] = uni.owner.get("/api/tasks?state=queued").json()["tasks"]
    assert listed["id"] == row["task_id"] and listed["hold"] == "send" and listed["kind"] == "handoff"
    # a Member (even one allowed to dispatch) cannot press the human's Send or Dismiss
    for verb in ("release", "dismiss"):
        assert uni.eve.post(f"/api/tasks/{row['task_id']}/{verb}").status_code == 403
    # The Owner may; with no advisor ledger entry behind the card the request is unknown -> 409
    r = uni.owner.post(f"/api/tasks/{row['task_id']}/dismiss")
    assert r.status_code == 409 and r.json()["error"]["code"] == "task_state_conflict"


def test_a_held_task_without_a_ledger_entry_is_released_from_the_tasks_page(uni):
    agent = uni.mgr.seat_agent("lead")
    row = uni.owner.portal.call(
        lambda: uni.mgr.dispatch(agent=agent, prompt="scenario:simple", owner="owner", kind="handoff",
                                 hold="send", post_card=False))
    assert row["state"] == "queued" and row["hold"] == "send"
    assert uni.owner.post(f"/api/tasks/{row['task_id']}/release").status_code == 202
    wait_state(uni.owner, row["task_id"], "succeeded")
