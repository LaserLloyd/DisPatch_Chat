"""The AgentBackend contract suite (gate G17, "contract suite").

Every test is parametrized over every adapter - the scripted fake, OpenClaw
against a fake gateway, and the Claude Agent adapter against a fake SDK
transport - and asserts IDENTICAL normalized behaviour. The adapters see the
same prompts ("scenario:<name>"); each harness (tests/agents/agent_harness.py)
makes its own protocol answer them.

Where an adapter genuinely lacks a feature the test is SKIPPED with the
capability reason, and a companion test asserts the lack is explicit (the call
fails with the catalogued ``not_supported``), never silent.
"""
from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio
from agent_harness import ADAPTERS, HARNESSES

from app.agents.branding import scan_text
from app.agents.budget import Budget
from app.agents.events import (
    EVENT_TYPES,
    ApprovalRequestEvent,
    CostUpdateEvent,
    ErrorCode,
    ErrorEvent,
    ResultEvent,
    StatusEvent,
    SubagentFinishedEvent,
    SubagentStartedEvent,
    TaskState,
    TextDeltaEvent,
    ToolCallEvent,
    ToolResultEvent,
    parse_event,
)
from app.agents.interface import AgentBackend, AgentBackendError

TIMEOUT = 20


@pytest_asyncio.fixture(params=ADAPTERS)
async def h(request, tmp_path):
    harness = HARNESSES[request.param](tmp_path)
    await harness.setup()
    yield harness
    await harness.teardown()


def need(h, scenario: str) -> None:
    if scenario not in h.scenarios:
        pytest.skip(f"{h.name} cannot act out {scenario!r} (see Capabilities)")


async def start(h, scenario: str, *, spec=None, **ctx_over) -> str:
    h.prepare(scenario)
    return await h.backend.start_task(spec or h.spec(), f"scenario:{scenario}", h.ctx(**ctx_over))


async def collect(h, task_id: str, after_seq: int = -1) -> list:
    async def go():
        return [e async for e in h.backend.events(task_id, after_seq=after_seq)]
    return await asyncio.wait_for(go(), timeout=TIMEOUT)


def last_result(events) -> ResultEvent:
    assert isinstance(events[-1], ResultEvent), [e.type for e in events]
    return events[-1]


def assert_stream_invariants(events, task_id: str, *, first_seq: int = 0) -> None:
    assert events, "no events"
    assert [e.seq for e in events] == list(range(first_seq, first_seq + len(events))), "seq must be consecutive"
    assert all(e.task_id == task_id for e in events)
    assert all(e.type in EVENT_TYPES for e in events)
    ts = [e.ts for e in events]
    assert ts == sorted(ts), "ts must not go backwards"
    assert sum(isinstance(e, ResultEvent) for e in events) == 1, "exactly one result per run"
    assert isinstance(events[-1], ResultEvent), "the result is the last event of a run"
    for e in events:                                   # every event survives a JSON round trip
        assert parse_event(e.model_dump(mode="json")) == e


def types(events) -> list[str]:
    return [e.type for e in events]


# --------------------------------------------------------------------------- #
# shape, ordering, required fields
# --------------------------------------------------------------------------- #

async def test_adapter_satisfies_the_protocol_and_branding(h):
    assert isinstance(h.backend, AgentBackend)
    caps = h.backend.capabilities()
    assert caps.name == h.backend.name == h.name
    assert not scan_text(caps.label), f"forbidden label {caps.label!r}"


async def test_simple_run_ordering_and_fields(h):
    tid = await start(h, "simple")
    ev = await collect(h, tid)
    assert_stream_invariants(ev, tid)

    assert isinstance(ev[0], StatusEvent) and ev[0].state == TaskState.queued
    assert isinstance(ev[1], StatusEvent) and ev[1].state == TaskState.running
    text = "".join(e.text for e in ev if isinstance(e, TextDeltaEvent) and not e.replace)
    assert text == "Hello world"

    costs = [e for e in ev if isinstance(e, CostUpdateEvent)]
    assert costs and all(c.estimate is True for c in costs), "cost is labelled an estimate"
    assert ev.index(costs[-1]) < len(ev) - 1, "cost_update precedes the result"

    res = last_result(ev)
    assert res.state == TaskState.succeeded and res.text
    assert res.session_id, "the result carries the session id for resume"
    assert res.resumable is True
    info = h.backend.task_info(tid)
    assert info.state == TaskState.succeeded and info.session_id == res.session_id


async def test_tool_call_then_result_share_a_call_id(h):
    need(h, "tools")
    tid = await start(h, "tools")
    ev = await collect(h, tid)
    assert_stream_invariants(ev, tid)
    call = next(e for e in ev if isinstance(e, ToolCallEvent))
    res = next(e for e in ev if isinstance(e, ToolResultEvent))
    assert call.name == "Read" and call.call_id == res.call_id and res.ok
    assert ev.index(call) < ev.index(res)
    assert last_result(ev).state == TaskState.succeeded


async def test_subagent_tree_has_parent_ids(h):
    need(h, "subagent")
    if not h.backend.capabilities().subagents:
        pytest.skip("no subagent support")
    tid = await start(h, "subagent", spec=h.spec(tool_allowlist=["Read", "Bash", "Agent"]))
    ev = await collect(h, tid)
    assert_stream_invariants(ev, tid)
    started = next(e for e in ev if isinstance(e, SubagentStartedEvent))
    finished = next(e for e in ev if isinstance(e, SubagentFinishedEvent))
    assert started.parent_id is None and finished.subagent_id == started.subagent_id
    child_text = [e for e in ev if isinstance(e, TextDeltaEvent) and e.parent_id == started.subagent_id]
    assert child_text, "the child's output is attributed to the subagent"
    assert ev.index(started) < ev.index(child_text[0]) < ev.index(finished)


async def test_retry_is_surfaced(h):
    need(h, "retry")
    tid = await start(h, "retry")
    ev = await collect(h, tid)
    assert any(e.type == "retry" for e in ev)
    assert last_result(ev).state == TaskState.succeeded


# --------------------------------------------------------------------------- #
# approvals
# --------------------------------------------------------------------------- #

async def _run_until_approval(h, scenario="approval"):
    tid = await start(h, scenario)
    it = h.backend.events(tid).__aiter__()
    seen = []
    async def go():
        async for e in it:
            seen.append(e)
            if isinstance(e, ApprovalRequestEvent):
                return e
    req = await asyncio.wait_for(go(), timeout=TIMEOUT)
    return tid, it, seen, req


async def _drain(it, seen):
    async def go():
        async for e in it:
            seen.append(e)
    await asyncio.wait_for(go(), timeout=TIMEOUT)


async def test_approval_round_trip_allow(h):
    need(h, "approval")
    tid, it, seen, req = await _run_until_approval(h)
    assert req.tool_name == "Bash" and req.request_id
    assert req.scopes == ["once"], "destructive tools never offer a task-wide grant"
    assert any(isinstance(e, ToolCallEvent) and e.name == "Bash" for e in seen), "the call precedes its approval"
    await asyncio.sleep(0.05)
    assert h.backend.task_info(tid).state == TaskState.awaiting_approval

    await h.backend.respond_approval(req.request_id, "allow", "once")
    await _drain(it, seen)
    assert_stream_invariants(seen, tid)
    res = next(e for e in seen if isinstance(e, ToolResultEvent) and e.name in (None, "Bash"))
    assert res.ok and seen.index(req) < seen.index(res)
    states = [e.state for e in seen if isinstance(e, StatusEvent)]
    assert TaskState.awaiting_approval in states and states[-1] == TaskState.running
    assert last_result(seen).state == TaskState.succeeded


async def test_approval_round_trip_deny(h):
    need(h, "approval")
    tid, it, seen, req = await _run_until_approval(h)
    await h.backend.respond_approval(req.request_id, "deny", "once")
    await _drain(it, seen)
    res = next(e for e in seen if isinstance(e, ToolResultEvent))
    assert res.ok is False
    assert last_result(seen).state == TaskState.succeeded      # the agent carries on after a denial


async def test_approval_ids_are_single_use_and_checked(h):
    need(h, "approval")
    tid, it, seen, req = await _run_until_approval(h)
    with pytest.raises(AgentBackendError) as ei:
        await h.backend.respond_approval("apr_nope", "allow", "once")
    assert ei.value.code == ErrorCode.invalid_request
    with pytest.raises(AgentBackendError):
        await h.backend.respond_approval(req.request_id, "maybe", "once")      # bad decision
    await h.backend.respond_approval(req.request_id, "allow", "once")
    with pytest.raises(AgentBackendError):
        await h.backend.respond_approval(req.request_id, "allow", "once")     # already answered
    await _drain(it, seen)


async def test_a_task_scope_grant_is_not_honoured_for_destructive_tools(h):
    need(h, "approval")
    tid, it, seen, req = await _run_until_approval(h)
    await h.backend.respond_approval(req.request_id, "allow", "task")
    await _drain(it, seen)
    assert not h.backend._approvals.has_grant(tid, "Bash")


async def test_missing_approval_support_is_explicit(h):
    if h.backend.capabilities().approvals:
        pytest.skip("approvals supported")
    with pytest.raises(AgentBackendError) as ei:
        await h.backend.respond_approval("apr_x", "allow", "once")
    assert ei.value.code == ErrorCode.not_supported


# --------------------------------------------------------------------------- #
# interrupt -> resume, cancel, steer, follow-up
# --------------------------------------------------------------------------- #

async def _until_first_text(h, tid):
    it = h.backend.events(tid).__aiter__()
    seen = []
    async def go():
        async for e in it:
            seen.append(e)
            if isinstance(e, TextDeltaEvent):
                return
    await asyncio.wait_for(go(), timeout=TIMEOUT)
    return it, seen


async def test_interrupt_then_resume_continues_the_same_session(h):
    need(h, "long")
    tid = await start(h, "long")
    it, seen = await _until_first_text(h, tid)
    before = h.backend.task_info(tid).session_id
    await h.backend.interrupt(tid)
    await _drain(it, seen)
    assert_stream_invariants(seen, tid)
    res = last_result(seen)
    assert res.state == TaskState.cancelled and res.reason == "interrupted"
    assert res.resumable is True and res.session_id
    session = res.session_id
    assert h.backend.task_info(tid).resumable is True
    if before:
        assert before == session

    await h.backend.resume(tid)
    ev2 = await collect(h, tid, after_seq=res.seq)
    assert_stream_invariants(ev2, tid, first_seq=res.seq + 1)
    assert ev2[0].type == "status" and ev2[0].state == TaskState.running
    res2 = last_result(ev2)
    assert res2.state == TaskState.succeeded
    assert res2.session_id == session, "resume continues the SAME session id"
    assert "resumed" in "".join(e.text for e in ev2 if isinstance(e, TextDeltaEvent))
    assert h.backend.task_info(tid).session_id == session


async def test_interrupt_of_an_idle_task_is_rejected(h):
    tid = await start(h, "simple")
    await collect(h, tid)
    with pytest.raises(AgentBackendError) as ei:
        await h.backend.interrupt(tid)
    assert ei.value.code == ErrorCode.invalid_request


async def test_cancel_is_terminal(h):
    need(h, "long")
    tid = await start(h, "long")
    it, seen = await _until_first_text(h, tid)
    await h.backend.cancel(tid)
    await _drain(it, seen)
    assert_stream_invariants(seen, tid)
    res = last_result(seen)
    assert res.state == TaskState.cancelled and res.reason == "cancelled" and res.resumable is False
    assert h.backend.task_info(tid).state == TaskState.cancelled
    with pytest.raises(AgentBackendError) as ei:
        await h.backend.resume(tid)
    assert ei.value.code == ErrorCode.invalid_request
    await h.backend.cancel(tid)                                    # idempotent


async def test_cancel_of_a_finished_task_ends_with_a_cancelled_result(h):
    tid = await start(h, "simple")
    first = await collect(h, tid)
    await h.backend.cancel(tid)
    ev = await collect(h, tid, after_seq=first[-1].seq)
    res = last_result(ev)
    assert res.state == TaskState.cancelled and res.resumable is False


async def test_steering_a_running_task(h):
    need(h, "long")
    if not h.backend.capabilities().steering:
        tid = await start(h, "long")
        await _until_first_text(h, tid)
        with pytest.raises(AgentBackendError) as ei:
            await h.backend.send(tid, "change course")
        assert ei.value.code == ErrorCode.not_supported
        await h.backend.cancel(tid)
        return
    tid = await start(h, "long")
    it, seen = await _until_first_text(h, tid)
    await h.backend.send(tid, "change course")
    await _drain(it, seen)
    assert last_result(seen).state == TaskState.succeeded
    assert "change course" in "".join(e.text for e in seen if isinstance(e, TextDeltaEvent))


async def test_send_to_an_idle_task_is_a_follow_up_turn(h):
    tid = await start(h, "simple")
    first = await collect(h, tid)
    session = last_result(first).session_id
    await h.backend.send(tid, "scenario:simple")
    ev = await collect(h, tid, after_seq=first[-1].seq)
    res = last_result(ev)
    assert res.state == TaskState.succeeded and res.session_id == session


async def test_replay_from_a_cursor(h):
    tid = await start(h, "simple")
    full = await collect(h, tid)
    mid = full[len(full) // 2].seq
    tail = await collect(h, tid, after_seq=mid)
    assert [e.seq for e in tail] == [e.seq for e in full if e.seq > mid]
    assert await collect(h, tid) == full, "a finished task replays identically"


# --------------------------------------------------------------------------- #
# errors and budgets
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("scenario,code,retryable", [
    ("error_unavailable", ErrorCode.backend_unavailable, True),
    ("error_refused", ErrorCode.refused, False),
    ("error_auth", ErrorCode.auth_failed, False),
])
async def test_backend_errors_map_to_catalogued_codes(h, scenario, code, retryable):
    need(h, scenario)
    tid = await start(h, scenario)
    ev = await collect(h, tid)
    assert_stream_invariants(ev, tid)
    err = next(e for e in ev if isinstance(e, ErrorEvent))
    assert err.code == code and err.retryable is retryable and err.message
    res = last_result(ev)
    assert res.state == TaskState.failed and res.reason == code.value
    assert ev.index(err) < ev.index(res)
    assert h.backend.task_info(tid).state == TaskState.failed


async def test_an_unexpected_exception_is_an_internal_error(h):
    need(h, "crash")
    tid = await start(h, "crash")
    ev = await collect(h, tid)
    err = next(e for e in ev if isinstance(e, ErrorEvent))
    assert err.code == ErrorCode.internal
    assert last_result(ev).state == TaskState.failed


async def test_budget_stop_fails_with_budget_exceeded_and_a_cost_update(h):
    need(h, "budget")
    spec = h.spec(budget=Budget(max_usd=0.05, max_tokens=1000, max_turns=3))
    tid = await start(h, "budget", spec=spec)
    ev = await collect(h, tid)
    assert_stream_invariants(ev, tid)
    res = last_result(ev)
    assert res.state == TaskState.failed and res.reason == "budget_exceeded"
    assert res.resumable is False
    err = next(e for e in ev if isinstance(e, ErrorEvent))
    assert err.code == ErrorCode.budget_exceeded
    costs = [e for e in ev if isinstance(e, CostUpdateEvent)]
    assert costs and costs[-1].estimate is True
    assert ev.index(costs[-1]) < ev.index(err) < ev.index(res)
    with pytest.raises(AgentBackendError) as ei:
        await h.backend.resume(tid)
    assert ei.value.code == ErrorCode.invalid_request


async def test_wall_clock_budget_stops_a_long_run(h):
    need(h, "long")
    spec = h.spec(budget=Budget(max_wall_clock_s=0.4))
    tid = await start(h, "long", spec=spec)
    ev = await collect(h, tid)
    assert_stream_invariants(ev, tid)
    res = last_result(ev)
    assert res.state == TaskState.failed and res.reason == "budget_exceeded"
    assert any(isinstance(e, CostUpdateEvent) for e in ev)


# --------------------------------------------------------------------------- #
# misuse
# --------------------------------------------------------------------------- #

async def test_unknown_task_and_duplicate_ids_are_rejected(h):
    for call in (lambda: h.backend.send("nope", "x"), lambda: h.backend.interrupt("nope"),
                 lambda: h.backend.resume("nope"), lambda: h.backend.cancel("nope")):
        with pytest.raises(AgentBackendError) as ei:
            await call()
        assert ei.value.code == ErrorCode.invalid_request
    with pytest.raises(AgentBackendError):
        h.backend.task_info("nope")
    tid = await start(h, "simple", task_id="dup-1")
    await collect(h, tid)
    with pytest.raises(AgentBackendError) as ei:
        await start(h, "simple", task_id="dup-1")
    assert ei.value.code == ErrorCode.invalid_request


async def test_a_spec_for_another_backend_is_rejected(h):
    other = "fake" if h.name != "fake" else "openclaw"
    with pytest.raises(AgentBackendError) as ei:
        await h.backend.start_task(h.spec(backend=other), "scenario:simple", h.ctx())
    assert ei.value.code == ErrorCode.invalid_request


async def test_resume_of_a_running_task_is_rejected(h):
    need(h, "long")
    tid = await start(h, "long")
    it, seen = await _until_first_text(h, tid)
    with pytest.raises(AgentBackendError) as ei:
        await h.backend.resume(tid)
    assert ei.value.code == ErrorCode.invalid_request
    await h.backend.cancel(tid)
    await _drain(it, seen)
