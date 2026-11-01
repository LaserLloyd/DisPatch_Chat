"""4.3 WP-A6 (#34): two answers to the same approval request.

A backend that accepts an answer idempotently (it does not refuse the second one) used to let
both answers succeed, the second overwriting the first in the audit trail. Now exactly one
answer wins: the other gets 404 ``approval_not_found`` and the row keeps the first decision."""
from __future__ import annotations

import asyncio
import contextlib
import time

import pytest

from app import agents_store as store
from app import config
from app.agents.fake import FakeBackend
from app.agents.interface import AgentBackendError, AgentSpec
from app.agents_service import Hooks, TaskError, TaskManager

pytestmark = pytest.mark.timeout(60)


class SlowLenient(FakeBackend):
    """Answers take a moment and a repeated answer is silently accepted (like a remote API)."""

    async def respond_approval(self, request_id, decision, scope="once"):
        await asyncio.sleep(0.05)
        with contextlib.suppress(AgentBackendError):
            await super().respond_approval(request_id, decision, scope)


class Refusing(FakeBackend):
    """The backend says the request is unknown (it already ended on its own)."""

    async def respond_approval(self, request_id, decision, scope="once"):
        raise AgentBackendError(__import__("app.agents.events", fromlist=["ErrorCode"]).ErrorCode.invalid_request,
                                "unknown request")


async def settle(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached")


@pytest.fixture
async def mgr(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    store.reset_for_tests()
    m = TaskManager(config.DATA_DIR, hooks=Hooks())
    m.register_backend("fake", SlowLenient())
    await m.startup()
    yield m
    await m.shutdown()


def _agent(name):
    spec = AgentSpec.model_validate({"backend": "fake", "name": name, "tool_allowlist": ["Bash"]})
    return store.create_agent(name=name, spec=spec.model_dump(mode="json"), published_to=["*"], created_by="")


async def _waiting(mgr, name):
    row = await mgr.dispatch(agent=_agent(name), prompt="scenario:approval", owner="o1")
    tid = row["task_id"]
    await settle(lambda: store.get_task(tid)["state"] == "awaiting_approval")
    await mgr.flush()
    return tid, store.list_approvals(tid)[0]["request_id"]


async def test_two_concurrent_answers_exactly_one_wins(mgr):
    tid, rid = await _waiting(mgr, "Race")
    results = await asyncio.gather(mgr.respond_approval(rid, "allow", "once", "p1"),
                                   mgr.respond_approval(rid, "deny", "once", "p2"),
                                   return_exceptions=True)
    wins = [r for r in results if isinstance(r, dict)]
    losses = [r for r in results if isinstance(r, TaskError)]
    assert len(wins) == 1 and len(losses) == 1, results
    assert losses[0].status == 404 and losses[0].code == "approval_not_found"
    await mgr.flush()
    row = store.get_approval(rid)
    assert row["decision"] == wins[0]["state"]
    assert row["decided_by"] == wins[0]["decided_by"]
    t = store.get_task(tid)
    assert t["approvals_allowed"] + t["approvals_denied"] == 1


async def test_a_late_answer_after_a_decision_is_a_404_and_changes_nothing(mgr):
    tid, rid = await _waiting(mgr, "Late")
    await mgr.respond_approval(rid, "deny", "once", "p1")
    with pytest.raises(TaskError) as e:
        await mgr.respond_approval(rid, "allow", "once", "p2")
    assert e.value.status == 404
    await mgr.flush()
    assert store.get_approval(rid)["decision"] == "deny"
    assert store.get_approval(rid)["decided_by"] == "p1"


async def test_the_backends_refusal_never_overwrites_a_recorded_decision(mgr):
    tid, rid = await _waiting(mgr, "Refuse")
    assert store.decide_approval(rid, "allow", "once", "someone")
    mgr.backends["fake"].__class__ = Refusing
    with pytest.raises(TaskError) as e:
        await mgr.respond_approval(rid, "deny", "once", "p2")
    assert e.value.code == "approval_not_found"
    await mgr.flush()
    assert store.get_approval(rid)["decision"] == "allow"
    mgr.backends["fake"].__class__ = SlowLenient
    await mgr.cancel(store.get_task(tid))

