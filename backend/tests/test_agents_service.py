"""The task manager, without HTTP: recovery, retention, the queue, redaction, the unified chat
turn and the advisor hand-off bridge. A scriptable fake backend stands in for every adapter,
so nothing here touches a gateway, a model or the network."""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from app import accounts, agents_service, config
from app import agents_store as store
from app.agents.fake import FakeBackend, Say, Tool
from app.agents.interface import AgentSpec
from app.agents.secrets import GLOBAL_REDACTOR
from app.agents_service import Hooks, TaskError, TaskManager

pytestmark = pytest.mark.timeout(60)

KEY = "sk-ant-api03-" + "Zz9yY8xX" * 6


class FakeOpenClaw(FakeBackend):
    name = "openclaw"


async def settle(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = pred()
        if v:
            return v
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached")


def make_agent(name="A", **over):
    spec = {"backend": "fake", "name": name, "tool_allowlist": ["Read", "Bash"], **over}
    return store.create_agent(name=name, spec=AgentSpec.model_validate(spec).model_dump(mode="json"),
                              published_to=["*"], created_by="")


async def new_manager(**kw) -> TaskManager:
    store.reset_for_tests()
    m = TaskManager(config.DATA_DIR, hooks=Hooks(), **kw)
    m.register_backend("fake", FakeBackend(scripts={**FakeBackend().scripts,
                                                    "leak": [Tool("Bash", {"command": f"echo {KEY}"}, output=KEY)]}))
    m.register_backend("openclaw", FakeOpenClaw())
    await m.startup()
    return m


@pytest.fixture
async def mgr(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    m = await new_manager()
    yield m
    await m.shutdown()


async def run(mgr, prompt, agent=None, owner="o1", **kw):
    row = await mgr.dispatch(agent=agent or make_agent(f"A{time.monotonic_ns()}"), prompt=prompt,
                             owner=owner, **kw)
    return row["task_id"]


def state(tid):
    return store.get_task(tid)["state"]


# --------------------------------------------------------------------------- #
# Restart recovery
# --------------------------------------------------------------------------- #


async def test_a_task_running_when_the_process_died_comes_back_resumable(mgr, tmp_path):
    agent = make_agent("Crashy")
    tid = await run(mgr, "scenario:long", agent)
    await settle(lambda: state(tid) == "running")
    await mgr.flush()
    # --- the crash: nothing is closed politely, the writer simply stops existing
    mgr._closing = True
    mgr._writer.cancel()
    for t in list(mgr._pumps.values()):
        t.cancel()
    await mgr.backends["fake"].aclose()
    # --- the next process
    m2 = await new_manager()
    try:
        row = store.get_task(tid)
        assert row["state"] == "cancelled" and row["reason"] == "restart"
        assert row["resumable"] is True and row["session_id"]
        assert store.list_events(tid)[-1]["type"] == "result"
        seqs = [e["seq"] for e in store.list_events(tid)]
        assert seqs == list(range(len(seqs)))                    # our synthetic result continues the numbering
        await m2.resume(row, "carry on")
        await settle(lambda: state(tid) == "succeeded")
        seqs = [e["seq"] for e in store.list_events(tid)]
        assert seqs == list(range(len(seqs))), "no gap and no overlap after the resume"
        assert store.list_events(tid)[-1]["type"] == "result"
        assert len([e for e in store.list_events(tid) if e["type"] == "result"]) == 2
    finally:
        await m2.shutdown()


async def test_a_pending_approval_does_not_survive_a_restart(mgr):
    tid = await run(mgr, "scenario:approval")
    await settle(lambda: state(tid) == "awaiting_approval")
    await mgr.flush()
    assert [a["decision"] for a in store.list_approvals(tid)] == [None]
    mgr._closing = True
    mgr._writer.cancel()
    for t in list(mgr._pumps.values()):
        t.cancel()
    await mgr.backends["fake"].aclose()
    m2 = await new_manager()
    try:
        await m2.flush()
        assert [a["decision"] for a in store.list_approvals(tid)] == ["expired"]
        assert state(tid) == "cancelled"
        with pytest.raises(TaskError) as e:
            await m2.respond_approval(store.list_approvals(tid)[0]["request_id"], "allow", "once", "o1")
        assert e.value.code == "approval_not_found"
    finally:
        await m2.shutdown()


async def test_a_finished_task_continues_after_a_restart_from_its_stored_session(mgr):
    tid = await run(mgr, "scenario:simple")
    await settle(lambda: state(tid) == "succeeded")
    await mgr.flush()
    await mgr.shutdown()
    m2 = await new_manager()
    try:
        row = store.get_task(tid)
        assert row["resumable"] is True
        assert await m2.send(row, "and then?") == "resumed"
        await settle(lambda: store.get_task(tid)["last_seq"] > row["last_seq"] + 2)
        await m2.flush()
        evs = store.list_events(tid, after_seq=row["last_seq"])
        assert evs[0]["seq"] == row["last_seq"] + 1
    finally:
        await m2.shutdown()


# --------------------------------------------------------------------------- #
# Queue
# --------------------------------------------------------------------------- #


async def test_tasks_beyond_the_concurrency_limit_wait_their_turn(mgr):
    store.set_setting("agents.max_concurrent", 1)
    agent = make_agent("Slotted")
    first = await run(mgr, "scenario:long", agent)
    second = await run(mgr, "scenario:long", agent)
    await settle(lambda: state(first) == "running")
    await asyncio.sleep(0.1)
    assert state(second) == "queued"
    await mgr.cancel(store.get_task(first))
    await settle(lambda: state(first) == "cancelled")
    await settle(lambda: state(second) == "running")
    await mgr.cancel(store.get_task(second))
    await settle(lambda: state(second) == "cancelled")


async def test_a_queued_task_can_be_cancelled_before_it_starts(mgr):
    store.set_setting("agents.max_concurrent", 1)
    agent = make_agent("Slotted2")
    first = await run(mgr, "scenario:long", agent)
    second = await run(mgr, "scenario:simple", agent)
    await settle(lambda: state(first) == "running")
    await mgr.cancel(store.get_task(second))
    row = store.get_task(second)
    assert row["state"] == "cancelled" and row["reason"] == "cancelled"
    await mgr.cancel(store.get_task(first))


# --------------------------------------------------------------------------- #
# Retention
# --------------------------------------------------------------------------- #


async def test_the_purge_deletes_the_transcript_and_keeps_the_audit_row(mgr, tmp_path):
    tid = await run(mgr, "SECRET-PROMPT scenario:simple".replace("SECRET-PROMPT ", ""), make_agent("Purged"),
                    )
    store.update_task(tid, prompt="the-private-prompt")
    await settle(lambda: state(tid) == "succeeded")
    await mgr.flush()
    ws = mgr.data_dir / "agents" / tid / "work"
    ws.mkdir(parents=True)
    (ws / "notes.txt").write_text("private", encoding="utf-8")
    recent = await mgr.purge_now()
    assert recent == 0 and store.list_events(tid), "inside the retention window nothing is touched"
    store.update_task(tid, finished_at="2020-01-01T00:00:00+00:00")
    assert await mgr.purge_now() == 1
    row = store.get_task(tid)
    assert row["purged"] is True and row["prompt"] is None and row["result_text"] is None
    assert row["agent_name"] == "Purged" and row["state"] == "succeeded" and row["cost_usd"] == 0.01
    assert store.list_events(tid) == []
    assert not (mgr.data_dir / "agents" / tid).exists()
    assert await mgr.purge_now() == 0, "idempotent"


async def test_retention_zero_keeps_everything(mgr):
    store.set_setting("agents.retention_days", 0)
    tid = await run(mgr, "scenario:simple")
    await settle(lambda: state(tid) == "succeeded")
    store.update_task(tid, finished_at="2001-01-01T00:00:00+00:00")
    assert await mgr.purge_now() == 0


# --------------------------------------------------------------------------- #
# Secrets and the support bundle
# --------------------------------------------------------------------------- #


async def test_a_key_in_a_tool_call_never_reaches_a_row_a_frame_or_a_summary(mgr):
    GLOBAL_REDACTOR.register(KEY)
    agent = make_agent("Leaky", instructions="INSTRUCTIONS-NEVER-IN-A-BUNDLE")
    tid = await run(mgr, "scenario:leak", agent)
    await settle(lambda: state(tid) == "awaiting_approval")
    await mgr.flush()
    [appr] = store.list_approvals(tid)
    assert KEY not in appr["args_summary"] and "***redacted***" in appr["args_summary"]
    await mgr.respond_approval(appr["request_id"], "allow", "once", "o1")
    await settle(lambda: state(tid) == "succeeded")
    await mgr.flush()
    dump = json.dumps([store.get_task(tid), store.list_events(tid), store.list_approvals(tid)])
    assert KEY not in dump
    bundle = json.dumps(mgr.support_summary())
    assert KEY not in bundle
    assert "INSTRUCTIONS-NEVER-IN-A-BUNDLE" not in bundle


async def test_the_support_summary_has_counts_and_shapes_but_no_prompt(mgr):
    agent = make_agent("Counted", instructions="SECRET-INSTRUCTIONS")
    tid = await run(mgr, "scenario:simple", agent)
    store.update_task(tid, prompt="TOPSECRET-PROMPT scenario:simple")
    await settle(lambda: state(tid) == "succeeded")
    await mgr.flush()
    summary = mgr.support_summary()
    text = json.dumps(summary)
    for needle in ("TOPSECRET-PROMPT", "SECRET-INSTRUCTIONS", "Counted", tid, "Hello world"):
        assert needle not in text, needle
    assert summary["prompts_included"] is False
    assert summary["tasks_by_state"] == {"succeeded": 1}
    assert summary["agents"][0]["backend"] == "fake" and summary["agents"][0]["tools"] == 2
    assert summary["task_events"] > 0
    redacted = store.redacted_summary()
    assert "TOPSECRET-PROMPT" not in json.dumps(redacted)


async def test_a_huge_event_is_bounded_before_it_is_stored(mgr):
    mgr.backends["fake"].scripts["big"] = [Tool("Read", {"file_path": "x" * 500_000}, output="y" * 500_000),
                                           Say("done")]
    tid = await run(mgr, "scenario:big")
    await settle(lambda: state(tid) == "succeeded")
    await mgr.flush()
    sizes = [len(json.dumps(e)) for e in store.list_events(tid)]
    assert max(sizes) < 120_000


# --------------------------------------------------------------------------- #
# Unified dispatch: chat turns
# --------------------------------------------------------------------------- #


async def test_a_chat_turn_runs_as_a_hidden_task_on_the_threads_own_session(mgr):
    out = await mgr.run_chat_turn(thread_id="thr1", agent_seat="alpha", text="scenario:simple", timeout_s=20)
    assert out.state == "succeeded" and out.text == "Hello world"
    row = store.get_task("thr1")
    assert row["kind"] == "chat" and row["agent_id"] == "openclaw:alpha"
    assert row["session_id"] == "fake-session-thr1"         # the thread id IS the task id
    assert store.list_tasks() == [], "a chat turn is not a task the person assigned"
    assert [t["task_id"] for t in store.list_tasks(include_chat=True)] == ["thr1"]
    assert store.list_events("thr1") == [], "the chat itself is the transcript"
    # the next message continues the same session
    out2 = await mgr.run_chat_turn(thread_id="thr1", agent_seat="alpha", text="more", timeout_s=20)
    assert out2.state == "succeeded"
    assert store.get_task("thr1")["session_id"] == "fake-session-thr1"


async def test_a_failed_chat_turn_does_not_stop_the_next_one(mgr):
    bad = await mgr.run_chat_turn(thread_id="thr2", agent_seat="alpha", text="scenario:error_refused", timeout_s=20)
    assert bad.state == "failed" and bad.error_code == "refused" and "model not allowed" in bad.error_message
    good = await mgr.run_chat_turn(thread_id="thr2", agent_seat="alpha", text="scenario:simple", timeout_s=20)
    assert good.state == "succeeded" and good.text == "Hello world"


async def test_stopping_a_chat_turn(mgr):
    t = asyncio.create_task(mgr.run_chat_turn(thread_id="thr3", agent_seat="alpha",
                                              text="scenario:long", timeout_s=20))
    await settle(lambda: mgr.backends["openclaw"].is_active("thr3"))
    assert await mgr.abort_chat("thr3") is True
    out = await asyncio.wait_for(t, 5)
    assert out.state == "cancelled" and out.reason == "interrupted"
    assert await mgr.abort_chat("thr3") is False


# --------------------------------------------------------------------------- #
# Unified dispatch: advisor hand-off cards
# --------------------------------------------------------------------------- #


def _req(rid="rq1"):
    return {"id": rid, "kind": "handoff", "agent": "lead", "brief": "Rotate the keys", "thread_id": None,
            "card_message_id": None}


async def test_a_handoff_card_is_a_queued_task_until_the_owner_presses_send(mgr, monkeypatch):
    monkeypatch.setenv("DISPATCH_AGENTS_UNIFIED_DISPATCH", "0")
    assert await mgr.propose_handoff(_req()) is None, "flag off: the ledger and the card are all there is"
    monkeypatch.setenv("DISPATCH_AGENTS_UNIFIED_DISPATCH", "1")
    row = await mgr.propose_handoff(_req())
    assert row["state"] == "queued" and row["hold"] == "send" and row["kind"] == "handoff"
    assert row["prompt"] == "Rotate the keys" and row["advisor_request_id"] == "rq1"
    assert await mgr.propose_handoff(_req()) is None, "one task per card"
    await mgr._kick()
    await asyncio.sleep(0.1)
    assert state(row["task_id"]) == "queued", "a held task never starts by itself"
    # Send: the advisor asks the agent to run it
    text = await mgr.run_handoff("rq1", "lead", "scenario:simple", 20)
    assert text == "Hello world"
    done = store.get_task(row["task_id"])
    assert done["state"] == "succeeded" and done["hold"] is None and done["advisor_request_id"] == "rq1"


async def test_dismissing_a_card_cancels_its_task(mgr, monkeypatch):
    monkeypatch.setenv("DISPATCH_AGENTS_UNIFIED_DISPATCH", "1")
    row = await mgr.propose_handoff(_req("rq2"))
    await mgr.dismiss_handoff("rq2")
    got = store.get_task(row["task_id"])
    assert got["state"] == "cancelled" and got["reason"] == "dismissed"


async def test_a_failing_handoff_run_raises_for_the_advisor_to_report(mgr, monkeypatch):
    monkeypatch.setenv("DISPATCH_AGENTS_UNIFIED_DISPATCH", "1")
    with pytest.raises(TaskError) as e:
        await mgr.run_handoff("rq3", "lead", "scenario:error_refused", 20)
    assert "model not allowed" in e.value.message
    [t] = store.list_tasks()
    assert t["state"] == "failed" and t["advisor_request_id"] == "rq3"


# --------------------------------------------------------------------------- #
# Audit
# --------------------------------------------------------------------------- #


async def test_every_finished_task_leaves_an_audit_row(mgr):
    tid = await run(mgr, "scenario:simple", make_agent("Audited"), owner="o9")
    await settle(lambda: state(tid) == "succeeded")
    rows = await settle(lambda: [r for r in accounts.recent_activity(category="audit")
                                 if r["kind"] == "agent_task"])
    assert rows[0]["subject"] == "Audited"
    d = json.loads(rows[0]["detail"])
    assert d["task"] == tid and d["outcome"] == "succeeded" and d["cost_usd_estimate"] == 0.01
    assert d["approvals_allowed"] == 0 and "duration_s" in d
