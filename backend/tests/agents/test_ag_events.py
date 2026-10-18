"""Event models, the per-task log, and the approval broker."""
from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from app.agents.events import (
    EVENT_TYPES,
    ApprovalBroker,
    ApprovalError,
    ArtifactEvent,
    CostUpdateEvent,
    ErrorCode,
    ErrorEvent,
    EventLog,
    ResultEvent,
    RetryEvent,
    StatusEvent,
    SubagentFinishedEvent,
    SubagentStartedEvent,
    TaskState,
    TextDeltaEvent,
    ToolCallEvent,
    ToolResultEvent,
    clip,
    parse_event,
)
from app.agents.secrets import SecretRedactor


def test_the_catalogue_covers_the_twelve_event_types():
    assert set(EVENT_TYPES) == {
        "status", "text_delta", "tool_call", "tool_result", "approval_request", "subagent_started",
        "subagent_finished", "retry", "artifact", "cost_update", "result", "error"}


def test_task_states_are_the_briefs_six():
    assert [s.value for s in TaskState] == ["queued", "running", "awaiting_approval", "succeeded",
                                            "failed", "cancelled"]


def test_every_event_carries_task_id_ts_and_seq():
    log = EventLog("t1")
    made = [
        log.emit(StatusEvent, state=TaskState.running),
        log.emit(TextDeltaEvent, text="hi"),
        log.emit(ToolCallEvent, call_id="c", name="Read"),
        log.emit(ToolResultEvent, call_id="c"),
        log.emit(SubagentStartedEvent, subagent_id="s", parent_id=None),
        log.emit(SubagentFinishedEvent, subagent_id="s", parent_id=None),
        log.emit(RetryEvent, attempt=1),
        log.emit(ArtifactEvent, artifact_id="a"),
        log.emit(CostUpdateEvent, total_cost_usd=0.1),
        log.emit(ErrorEvent, code=ErrorCode.internal, message="x"),
        log.emit(ResultEvent, state=TaskState.failed),
    ]
    assert [e.seq for e in made] == list(range(len(made)))
    for e in made:
        d = e.model_dump(mode="json")
        assert d["task_id"] == "t1" and isinstance(d["ts"], float) and d["seq"] == e.seq
        assert parse_event(d) == e


def test_cost_is_always_an_estimate_and_cannot_be_unlabelled():
    ev = CostUpdateEvent(task_id="t", ts=0.0, seq=0, total_cost_usd=1.0)
    assert ev.estimate is True
    with pytest.raises(ValidationError):
        CostUpdateEvent(task_id="t", ts=0.0, seq=0, total_cost_usd=1.0, estimate=False)


def test_events_are_immutable_and_reject_unknown_fields():
    ev = StatusEvent(task_id="t", ts=0.0, seq=0, state=TaskState.queued)
    with pytest.raises(ValidationError):
        ev.seq = 5
    with pytest.raises(ValidationError):
        StatusEvent(task_id="t", ts=0.0, seq=0, state=TaskState.queued, bogus=1)
    with pytest.raises(ValidationError):
        ResultEvent(task_id="t", ts=0.0, seq=0, state=TaskState.running)       # not terminal


def test_clip_bounds_long_strings():
    assert clip("x" * 100, 50).endswith("...(clipped)...") and len(clip("x" * 100, 50)) == 50
    assert clip("short") == "short"


def test_the_log_redacts_secrets_in_every_string_before_storing():
    red = SecretRedactor()
    red.register("hunter2-super-secret")
    log = EventLog("t", redact=red.redact)
    log.emit(TextDeltaEvent, text="the key is hunter2-super-secret ok")
    log.emit(ToolCallEvent, call_id="c", name="Bash", input={"cmd": "echo hunter2-super-secret",
                                                              "nested": ["a", "hunter2-super-secret"]})
    dump = repr([e.model_dump() for e in log.snapshot()])
    assert "hunter2-super-secret" not in dump and "redacted" in dump


async def test_stream_replays_then_follows_then_ends_at_the_result():
    log = EventLog("t")
    log.begin_run()
    log.emit(StatusEvent, state=TaskState.queued)
    got: list = []

    async def consume():
        async for e in log.stream():
            got.append(e)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.02)
    assert [e.seq for e in got] == [0]
    log.emit(TextDeltaEvent, text="a")
    log.emit(ResultEvent, state=TaskState.succeeded)
    await asyncio.wait_for(task, 2)
    assert [e.type for e in got] == ["status", "text_delta", "result"]
    assert not log.active


async def test_a_finished_task_streams_nothing_new_and_does_not_hang():
    log = EventLog("t")
    log.begin_run()
    log.emit(ResultEvent, state=TaskState.succeeded)
    tail = [e async for e in log.stream(after_seq=0)]
    assert tail == []
    assert [e.seq async for e in log.stream()] == [0]


async def test_a_second_run_continues_the_sequence():
    log = EventLog("t")
    log.begin_run()
    log.emit(ResultEvent, state=TaskState.cancelled, reason="interrupted", resumable=True)
    log.begin_run()
    log.emit(StatusEvent, state=TaskState.running)
    log.emit(ResultEvent, state=TaskState.succeeded)
    second = [e async for e in log.stream(after_seq=0)]
    assert [e.seq for e in second] == [1, 2]


def test_start_seq_lets_a_restored_task_continue_numbering():
    log = EventLog("t", start_seq=41)
    assert log.emit(StatusEvent, state=TaskState.running).seq == 41
    assert log.last_seq == 41


async def test_approval_broker_resolves_and_remembers_task_scope_for_sticky_tools_only():
    b = ApprovalBroker()
    a = b.request(task_id="t", tool_name="WebFetch", input={}, sticky_ok=True)
    b.respond(a.request_id, "allow", "task")
    assert await a.future == "allow" and b.has_grant("t", "WebFetch")

    d = b.request(task_id="t", tool_name="Bash", input={}, sticky_ok=False)
    b.respond(d.request_id, "allow", "task")
    assert not b.has_grant("t", "Bash"), "destructive tools are never remembered"

    with pytest.raises(ApprovalError):
        b.respond(d.request_id, "allow")                      # single use
    with pytest.raises(ApprovalError):
        b.respond("apr_unknown", "allow")
    x = b.request(task_id="t", tool_name="X", input={})
    with pytest.raises(ApprovalError):
        b.respond(x.request_id, "yes")
    with pytest.raises(ApprovalError):
        b.respond(x.request_id, "allow", "forever")


async def test_deny_all_closes_only_that_tasks_requests():
    b = ApprovalBroker()
    mine = b.request(task_id="t", tool_name="Bash", input={})
    other = b.request(task_id="u", tool_name="Bash", input={})
    assert b.deny_all("t") == 1
    assert await mine.future == "deny" and not other.future.done()
    assert [p.request_id for p in b.pending_for("u")] == [other.request_id]
    b.forget_task("u")
    assert await other.future == "deny"
