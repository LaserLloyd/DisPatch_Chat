"""Normalized agent events, the per-task event log, and the approval broker.

Every adapter (OpenClaw, Claude Agent SDK, generic CLI, the fake) speaks THIS
vocabulary and nothing else upward, so the task timeline, the approval cards and
the audit trail are written once. The shape is the brief's "AGENTS AND TASK
DISPATCH" list: status, text_delta, tool_call, tool_result, approval_request,
subagent_started/finished (with parent id, to render a tree), retry, artifact,
cost_update, result, error.

Run/stream contract (asserted by tests/agents/contract for every adapter):

* ``seq`` starts at 0 for a task and increases by exactly 1 per event, across
  runs (a run = ``start_task`` / ``resume`` / follow-up ``send``). It is the
  replay cursor: ``events(task_id, after_seq=n)`` continues where a consumer
  stopped, so a reconnecting UI or the W11b persistence layer loses nothing.
* A run ends with exactly ONE ``result`` event, and the stream iterator ends
  after yielding it. ``result.state`` is succeeded, failed or cancelled.
* ``failed`` is always preceded by an ``error`` event carrying a catalogued
  :class:`ErrorCode`; ``result.reason`` repeats that code.
* An interrupted run ends ``cancelled`` with ``resumable=True``; ``cancel()``
  ends ``cancelled`` with ``resumable=False`` (RESEARCH.md F6d, section 5 item 11).
* Costs are ESTIMATES: :class:`CostUpdateEvent.estimate` is the literal
  ``True`` and cannot be set otherwise (RESEARCH.md F6e, R-COST, section 5 item 9).
"""
from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


class TaskState(StrEnum):
    """The six task states from the brief."""

    queued = "queued"
    running = "running"
    awaiting_approval = "awaiting_approval"
    succeeded = "succeeded"
    failed = "failed"
    cancelled = "cancelled"


TERMINAL_STATES = frozenset({TaskState.succeeded, TaskState.failed, TaskState.cancelled})


class ErrorCode(StrEnum):
    """The error catalogue. Adapters map EVERY backend failure onto one of these."""

    backend_unavailable = "backend_unavailable"          # down, draining, CLI missing; retryable
    backend_protocol_mismatch = "backend_protocol_mismatch"  # RESEARCH.md section 5 item 26
    auth_failed = "auth_failed"                           # bad/missing credentials
    rate_limited = "rate_limited"                         # retryable
    timeout = "timeout"
    refused = "refused"                                   # backend understood and said no
    invalid_request = "invalid_request"                   # caller error (unknown task, bad state)
    budget_exceeded = "budget_exceeded"                   # usd / turns / wall clock / tokens
    mcp_error = "mcp_error"                               # MCP server / plugin failed to start
    sdk_missing = "sdk_missing"                           # optional SDK not installed
    workspace_violation = "workspace_violation"           # path escape, bad task id
    policy_violation = "policy_violation"                 # quarantine / unattended-destructive
    not_supported = "not_supported"                       # capability the backend lacks
    backend_error = "backend_error"                       # the backend ran and failed
    internal = "internal"                                 # a bug in this layer


RETRYABLE_CODES = frozenset({ErrorCode.backend_unavailable, ErrorCode.rate_limited})

# Strings in events are bounded: a tool that returns a megabyte must not become
# a megabyte row in the timeline (the full output stays in the backend's own
# transcript).
EVENT_TEXT_LIMIT = 8000
_CLIP_MARK = "...(clipped)..."


def clip(text: str, limit: int = EVENT_TEXT_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - len(_CLIP_MARK))] + _CLIP_MARK


class _Base(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str
    ts: float                    # unix seconds
    seq: int = Field(ge=0)


class StatusEvent(_Base):
    type: Literal["status"] = "status"
    state: TaskState
    detail: str | None = None


class TextDeltaEvent(_Base):
    type: Literal["text_delta"] = "text_delta"
    text: str
    # True: ``text`` replaces everything this run streamed so far (the gateway
    # sends cumulative snapshots; a non-extension is a replace, not an append).
    replace: bool = False
    parent_id: str | None = None   # set for subagent output


class ToolCallEvent(_Base):
    type: Literal["tool_call"] = "tool_call"
    call_id: str
    name: str
    input: dict[str, Any] = Field(default_factory=dict)
    parent_id: str | None = None


class ToolResultEvent(_Base):
    type: Literal["tool_result"] = "tool_result"
    call_id: str
    ok: bool = True
    output: str = ""
    name: str | None = None
    parent_id: str | None = None


class ApprovalRequestEvent(_Base):
    type: Literal["approval_request"] = "approval_request"
    request_id: str
    tool_name: str
    input: dict[str, Any] = Field(default_factory=dict)
    call_id: str | None = None
    reason: str | None = None
    # Which scopes the approver may grant. Destructive tools only offer "once"
    # (G17: destructive tool calls always produce an approval_request).
    scopes: list[Literal["once", "task"]] = Field(default_factory=lambda: ["once", "task"])
    parent_id: str | None = None


class SubagentStartedEvent(_Base):
    type: Literal["subagent_started"] = "subagent_started"
    subagent_id: str
    parent_id: str | None = None      # None = child of the main agent
    name: str | None = None
    description: str | None = None


class SubagentFinishedEvent(_Base):
    type: Literal["subagent_finished"] = "subagent_finished"
    subagent_id: str
    parent_id: str | None = None
    ok: bool = True
    summary: str | None = None


class RetryEvent(_Base):
    type: Literal["retry"] = "retry"
    attempt: int = 1
    max_attempts: int | None = None
    delay_s: float | None = None
    reason: str | None = None
    code: ErrorCode | None = None


class ArtifactEvent(_Base):
    type: Literal["artifact"] = "artifact"
    artifact_id: str
    kind: Literal["file", "text", "url", "diff"] = "file"
    name: str | None = None
    path: str | None = None     # relative to the task workspace, never absolute
    mime: str | None = None
    size: int | None = None


class CostUpdateEvent(_Base):
    type: Literal["cost_update"] = "cost_update"
    # Always an estimate: SDK/CLI cost figures are computed client-side and "not
    # authoritative billing data" (RESEARCH.md F6e). The literal makes it
    # impossible to emit an unlabelled cost.
    estimate: Literal[True] = True
    total_cost_usd: float | None = None     # None when the backend reports no money
    input_tokens: int | None = None
    output_tokens: int | None = None
    turns: int | None = None
    source: str | None = None


class ResultEvent(_Base):
    type: Literal["result"] = "result"
    state: Literal[TaskState.succeeded, TaskState.failed, TaskState.cancelled]
    reason: str | None = None            # error code for failed; "interrupted"/"cancelled"
    text: str | None = None
    session_id: str | None = None
    resumable: bool = False
    num_turns: int | None = None
    duration_ms: int | None = None


class ErrorEvent(_Base):
    type: Literal["error"] = "error"
    code: ErrorCode
    message: str
    retryable: bool = False
    detail: str | None = None


Event = Annotated[
    StatusEvent | TextDeltaEvent | ToolCallEvent | ToolResultEvent | ApprovalRequestEvent
    | SubagentStartedEvent | SubagentFinishedEvent | RetryEvent | ArtifactEvent
    | CostUpdateEvent | ResultEvent | ErrorEvent,
    Field(discriminator="type"),
]
EVENT_ADAPTER: TypeAdapter[Any] = TypeAdapter(Event)

EVENT_TYPES = (
    "status", "text_delta", "tool_call", "tool_result", "approval_request",
    "subagent_started", "subagent_finished", "retry", "artifact", "cost_update",
    "result", "error",
)


def parse_event(data: dict[str, Any]) -> Any:
    return EVENT_ADAPTER.validate_python(data)


def _redact_obj(obj: Any, redact: Callable[[str], str]) -> Any:
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        return {k: _redact_obj(v, redact) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact_obj(v, redact) for v in obj]
    return obj


class EventLog:
    """Append-only, per-task event log with replay and live follow.

    Assigns ``seq`` and ``ts``; applies the redactor to every string in every
    event before it is stored or delivered (RESEARCH.md section 5 item 5: keys
    never in events). Not persistent: W11b mirrors it into the task-event table
    and can re-seed a restored task with ``start_seq``.
    """

    def __init__(self, task_id: str, *, redact: Callable[[str], str] | None = None,
                 clock: Callable[[], float] = time.time, start_seq: int = 0) -> None:
        self.task_id = task_id
        self._redact = redact
        self._clock = clock
        self._events: list[Any] = []
        self._base = start_seq               # seq of _events[0]
        self._cond = asyncio.Condition()
        self.active = False                  # a run is in progress
        self.listeners: list[Callable[[Any], None]] = []
        self._bg: set[asyncio.Task] = set()

    # -- writing ------------------------------------------------------------ #

    @property
    def next_seq(self) -> int:
        return self._base + len(self._events)

    @property
    def last_seq(self) -> int:
        return self.next_seq - 1

    def begin_run(self) -> None:
        self.active = True

    def emit(self, cls: type[_Base], **fields: Any) -> Any:
        """Build, redact, store and announce one event."""
        ev = cls(task_id=self.task_id, ts=self._clock(), seq=self.next_seq, **fields)
        if self._redact is not None:
            ev = EVENT_ADAPTER.validate_python(
                _redact_obj(ev.model_dump(mode="json"), self._redact))
        self._events.append(ev)
        if isinstance(ev, ResultEvent):
            self.active = False
        for fn in list(self.listeners):
            with contextlib.suppress(Exception):
                fn(ev)
        self._notify()
        return ev

    def _notify(self) -> None:
        # Condition needs the lock to notify; do it from a tiny task so emit()
        # stays synchronous (adapters call it from callbacks and handlers).
        async def _go() -> None:
            async with self._cond:
                self._cond.notify_all()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(_go())
        self._bg.add(task)                       # hold a reference until it ran
        task.add_done_callback(self._bg.discard)

    # -- reading ------------------------------------------------------------ #

    def snapshot(self, after_seq: int = -1) -> list[Any]:
        start = max(0, after_seq + 1 - self._base)
        return list(self._events[start:])

    async def stream(self, after_seq: int = -1) -> AsyncIterator[Any]:
        """Yield events with ``seq > after_seq``; stop after a ``result``.

        If nothing is left to read and no run is active the stream ends instead
        of waiting for ever (a finished task has nothing more to say until it is
        resumed, and then ``events(task_id, after_seq=last)`` continues it).
        """
        cursor = after_seq
        while True:
            pending = self.snapshot(cursor)
            for ev in pending:
                cursor = ev.seq
                yield ev
                if isinstance(ev, ResultEvent):
                    return
            if pending:
                continue
            if not self.active:
                return
            async with self._cond:
                if self.last_seq > cursor or not self.active:
                    continue
                await self._cond.wait()


# --------------------------------------------------------------------------- #
# Approvals
# --------------------------------------------------------------------------- #

Decision = Literal["allow", "deny"]
Scope = Literal["once", "task"]


@dataclass
class PendingApproval:
    request_id: str
    task_id: str
    tool_name: str
    input: dict[str, Any]
    call_id: str | None
    sticky_ok: bool                          # may a "task" scope grant be stored?
    future: asyncio.Future = field(repr=False, default=None)  # type: ignore[assignment]
    decision: Decision | None = None
    scope: Scope = "once"


class ApprovalError(Exception):
    """Unknown request id, double response, or a bad decision/scope."""


class ApprovalBroker:
    """Correlates ``approval_request`` events with ``respond_approval`` calls.

    ``scope="task"`` remembers an ALLOW for the rest of the task, but only for
    tools whose request was sticky-eligible: destructive tools (the policy's
    ``always_ask`` list) are never remembered, so every destructive call raises
    a fresh approval_request (brief, G17).
    """

    def __init__(self) -> None:
        self._pending: dict[str, PendingApproval] = {}
        self._grants: set[tuple[str, str]] = set()

    def has_grant(self, task_id: str, tool_name: str) -> bool:
        return (task_id, tool_name) in self._grants

    def request(self, *, task_id: str, tool_name: str, input: dict[str, Any],
                call_id: str | None = None, sticky_ok: bool = True) -> PendingApproval:
        pa = PendingApproval(
            request_id="apr_" + uuid.uuid4().hex[:16], task_id=task_id, tool_name=tool_name,
            input=input, call_id=call_id, sticky_ok=sticky_ok,
            future=asyncio.get_running_loop().create_future())
        self._pending[pa.request_id] = pa
        return pa

    def respond(self, request_id: str, decision: str, scope: str = "once") -> PendingApproval:
        pa = self._pending.get(request_id)
        if pa is None:
            raise ApprovalError(f"unknown or already-answered approval request {request_id!r}")
        if decision not in ("allow", "deny"):
            raise ApprovalError(f"decision must be 'allow' or 'deny', not {decision!r}")
        if scope not in ("once", "task"):
            raise ApprovalError(f"scope must be 'once' or 'task', not {scope!r}")
        del self._pending[request_id]
        pa.decision = decision        # type: ignore[assignment]
        pa.scope = scope              # type: ignore[assignment]
        if decision == "allow" and scope == "task" and pa.sticky_ok:
            self._grants.add((pa.task_id, pa.tool_name))
        if not pa.future.done():
            pa.future.set_result(decision)
        return pa

    def deny_all(self, task_id: str, *, reason: str = "task stopped") -> int:
        """Deny every open request of a task (interrupt / cancel)."""
        n = 0
        for rid, pa in list(self._pending.items()):
            if pa.task_id == task_id:
                del self._pending[rid]
                pa.decision = "deny"
                if not pa.future.done():
                    pa.future.set_result("deny")
                n += 1
        return n

    def pending_for(self, task_id: str) -> list[PendingApproval]:
        return [p for p in self._pending.values() if p.task_id == task_id]

    def forget_task(self, task_id: str) -> None:
        self.deny_all(task_id)
        self._grants = {g for g in self._grants if g[0] != task_id}
