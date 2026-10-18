"""The AgentBackend interface, the agent spec, and the shared task runtime.

``AgentBackend`` is the contract every adapter implements (OpenClaw, Claude
Agent SDK, Managed Agents (M2), generic CLI (M3)) and the contract suite in
``tests/agents/contract`` holds them all to. ``BaseBackend`` is the shared
skeleton - task registry, event log, approvals, budget watchdog, and the ONE
implementation of how a run ends - so three adapters cannot drift into three
slightly different terminal sequences.

How a task lives (see events.py for the stream contract):

    start_task -> status(queued) -> status(running) -> ... -> result
    result.state = succeeded  the session is idle and can be continued with
                              send(task_id, ...) or resume(task_id, ...)
    result.state = cancelled  reason "interrupted": idle, resumable (Stop button)
                              reason "cancelled": terminal (Cancel)
    result.state = failed     always after an ``error`` event with a catalogued code

Imports here are deliberately light (pydantic + stdlib + sibling modules): the
whole package imports without ``app.main`` (which is not importable on Windows
until W4 lands) and without any backend SDK.
"""
from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .branding import (
    HIGH_PRIVILEGE_TOOL_PATTERNS,
    quarantine_violations,
    tool_matches,
)
from .budget import Budget, BudgetTracker, BudgetVerdict
from .events import (
    RETRYABLE_CODES,
    TERMINAL_STATES,
    ApprovalBroker,
    ApprovalError,
    ApprovalRequestEvent,
    CostUpdateEvent,
    ErrorCode,
    ErrorEvent,
    EventLog,
    ResultEvent,
    StatusEvent,
    TaskState,
)
from .secrets import GLOBAL_REDACTOR, SecretProvider, SecretRedactor, get_agent_logger
from .workspace import check_task_id

log = get_agent_logger("core")

__all__ = [
    "AgentBackend", "AgentBackendError", "AgentSpec", "ApprovalPolicy", "BaseBackend",
    "Capabilities", "McpServerSpec", "SessionInfo", "TaskContext", "TaskInfo", "TaskRuntime",
    "TaskState", "WorkspacePolicy", "classify_tool",
]

# --------------------------------------------------------------------------- #
# Spec
# --------------------------------------------------------------------------- #


class McpServerSpec(BaseModel):
    """One MCP server an agent may use. Always passed EXPLICITLY (RESEARCH.md
    R-MCP, section 5 item 6): a backend never discovers servers on its own."""

    model_config = ConfigDict(extra="forbid")

    transport: Literal["stdio", "http", "sse"] = "http"
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    # header name -> secret name, resolved at launch from the SecretProvider and
    # sent to the worker over its pipe; never part of the spec dump or any event.
    header_secrets: dict[str, str] = Field(default_factory=dict)
    # A privileged server (e.g. an admin-scoped StudioForge key) counts as a
    # high-privilege tool for the quarantine rule.
    privileged: bool = False

    @model_validator(mode="after")
    def _shape(self) -> McpServerSpec:
        if self.transport == "stdio" and not self.command:
            raise ValueError("a stdio MCP server needs a command")
        if self.transport in ("http", "sse") and not self.url:
            raise ValueError("an http/sse MCP server needs a url")
        return self


class WorkspacePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["scratch", "worktree"] = "scratch"
    # RESEARCH.md section 5 item 6: the default is isolation. True makes the
    # Claude backend honour the repo's own settings (setting_sources=["project"]),
    # which means the repo's hooks EXECUTE - only for repos the owner trusts.
    trust_repo_settings: bool = False
    ref: str = "HEAD"


# Destructive-by-default tools: every call raises an approval_request (G17).
DEFAULT_ALWAYS_ASK: tuple[str, ...] = HIGH_PRIVILEGE_TOOL_PATTERNS


class ApprovalPolicy(BaseModel):
    """Who decides, per tool, in the three tiers below.

    * ``auto``        - in the allowlist, matches neither list: runs unasked.
    * ``ask``         - matches ``ask_tools``: approval_request; the approver may
                        grant ``scope="task"`` to stop asking for this task.
    * ``always_ask``  - matches ``always_ask`` (default: destructive tools):
                        approval_request EVERY time; ``scope="task"`` is not
                        offered and not honoured.
    """

    model_config = ConfigDict(extra="forbid")

    mode: Literal["interactive", "unattended", "plan"] = "interactive"
    ask_tools: list[str] = Field(default_factory=list)
    always_ask: list[str] = Field(default_factory=lambda: list(DEFAULT_ALWAYS_ASK))
    # Unattended runs cannot ask, so a tool that would need approval must be
    # named here by an admin to be usable at all.
    unattended_allow: list[str] = Field(default_factory=list)
    timeout_s: float | None = Field(default=None, gt=0)       # None: wait indefinitely


ToolTier = Literal["auto", "ask", "always_ask"]


def classify_tool(policy: ApprovalPolicy, name: str) -> ToolTier:
    if tool_matches(name, policy.always_ask):
        return "always_ask"
    if tool_matches(name, policy.ask_tools):
        return "ask"
    return "auto"


class AgentSpec(BaseModel):
    """A configured worker: backend, model, instructions, tools, policy, budget."""

    model_config = ConfigDict(extra="forbid")

    backend: str                                   # "fake" | "openclaw" | "claude_sdk" | "cli"
    name: str = "agent"
    model: str | None = None
    instructions: str = ""
    tool_allowlist: list[str] = Field(default_factory=list)
    mcp_servers: dict[str, McpServerSpec] = Field(default_factory=dict)
    workspace_policy: WorkspacePolicy = Field(default_factory=WorkspacePolicy)
    approval_policy: ApprovalPolicy = Field(default_factory=ApprovalPolicy)
    budget: Budget = Field(default_factory=Budget)
    required_permission: str = "dispatch_agents"   # checked by W11b's policy engine
    reads_untrusted_content: bool = False
    high_privilege_tools: list[str] = Field(default_factory=list)   # extra patterns
    max_subagent_depth: int = Field(default=2, ge=0, le=5)
    max_concurrent_subagents: int = Field(default=3, ge=0, le=16)
    backend_options: dict[str, Any] = Field(default_factory=dict)  # adapter-specific, non-secret

    @model_validator(mode="after")
    def _policy(self) -> AgentSpec:
        problems = quarantine_violations(self)
        if problems:
            raise ValueError(problems[0])
        if self.approval_policy.mode == "unattended":
            pol = self.approval_policy
            needs = [t for t in self.tool_allowlist
                     if classify_tool(pol, t) != "auto" and not tool_matches(t, pol.unattended_allow)]
            if needs:
                raise ValueError(
                    "an unattended agent cannot ask for approval, so these tools need an "
                    f"explicit admin 'unattended_allow' entry or must be removed: {', '.join(needs)}")
        return self


# --------------------------------------------------------------------------- #
# Context, capabilities, info
# --------------------------------------------------------------------------- #


@dataclass
class TaskContext:
    """Per-dispatch facts that are not part of the agent's definition."""

    data_dir: Path
    task_id: str | None = None             # caller-assigned id (W11b's DB row id)
    owner: str | None = None               # profile id, for audit only
    origin_thread_id: str | None = None
    repo_path: Path | None = None          # the repo this task targets, if any
    secrets: SecretProvider | None = None
    extra_env: dict[str, str] = field(default_factory=dict)


class Capabilities(BaseModel):
    """What a backend can and cannot do; the UI and the contract suite read this."""

    model_config = ConfigDict(extra="forbid")

    name: str
    label: str                              # the user-facing label (branding rules apply)
    streaming_text: bool = True
    approvals: bool = False
    interrupt: bool = True
    resume: bool = True
    steering: bool = False                  # send() into a RUNNING task
    subagents: bool = False
    cost_reporting: Literal["usd", "tokens", "none"] = "none"
    budget_enforcement: Literal["hard", "post_hoc", "none"] = "none"
    session_listing: bool = True
    same_host_required: bool = False
    data_leaves_host: bool = True           # prompts/outputs go to a remote service
    permission_modes: list[str] = Field(default_factory=list)


class SessionInfo(BaseModel):
    backend: str
    session_id: str
    task_id: str | None = None
    title: str | None = None
    updated_at: float | None = None
    size: int | None = None


class TaskInfo(BaseModel):
    task_id: str
    backend: str
    state: TaskState
    session_id: str | None = None
    resumable: bool = False
    last_seq: int = -1
    cost_usd: float | None = None
    cost_is_estimate: Literal[True] = True
    tokens: int = 0
    turns: int = 0
    created_at: float = 0.0


class AgentBackendError(Exception):
    """A failure with a catalogued code; adapters raise and map to this."""

    def __init__(self, code: ErrorCode, message: str, *, detail: str | None = None,
                 retryable: bool | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail
        self.retryable = (code in RETRYABLE_CODES) if retryable is None else retryable


# --------------------------------------------------------------------------- #
# The protocol
# --------------------------------------------------------------------------- #

Decision = Literal["allow", "deny"]
Scope = Literal["once", "task"]


@runtime_checkable
class AgentBackend(Protocol):
    """What W11b (and the contract suite) may rely on from every adapter."""

    name: str

    def capabilities(self) -> Capabilities: ...

    async def start_task(self, spec: AgentSpec, prompt: str, ctx: TaskContext) -> str: ...

    async def send(self, task_id: str, message: str) -> None: ...

    def events(self, task_id: str, *, after_seq: int = -1) -> AsyncIterator[Any]: ...

    async def respond_approval(self, request_id: str, decision: Decision,
                               scope: Scope = "once") -> None: ...

    async def interrupt(self, task_id: str) -> None: ...

    async def resume(self, task_id: str, message: str | None = None) -> None: ...

    async def cancel(self, task_id: str) -> None: ...

    async def list_sessions(self, ctx: TaskContext | None = None) -> list[SessionInfo]: ...

    def task_info(self, task_id: str) -> TaskInfo: ...

    async def aclose(self) -> None: ...


# --------------------------------------------------------------------------- #
# Shared runtime
# --------------------------------------------------------------------------- #


@dataclass
class TaskRuntime:
    task_id: str
    spec: AgentSpec
    ctx: TaskContext
    log: EventLog
    tracker: BudgetTracker
    state: TaskState = TaskState.queued
    session_id: str | None = None
    resumable: bool = False
    closed: bool = False                   # cancel() was called: terminal
    run_no: int = 0                        # increments per run; guards late finalizers
    interrupt_requested: bool = False
    cancel_requested: bool = False
    budget_stop: BudgetVerdict | None = None
    run_task: asyncio.Task | None = None
    watchdog: asyncio.Task | None = None
    created_at: float = field(default_factory=time.time)
    turns_this_task: int = 0
    extras: dict[str, Any] = field(default_factory=dict)       # adapter scratch space


class BaseBackend:
    """Task registry + the single definition of how runs start, steer and end."""

    name = "base"
    #: grace for a run to wind down after interrupt/cancel before it is force-cancelled
    STOP_GRACE_S = 5.0

    def __init__(self, *, redactor: SecretRedactor | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.redactor = redactor or GLOBAL_REDACTOR
        self._clock = clock
        self._tasks: dict[str, TaskRuntime] = {}
        self._approvals = ApprovalBroker()
        self._request_task: dict[str, str] = {}     # approval request id -> task id
        self._bg: set[asyncio.Task] = set()

    # -- subclass surface --------------------------------------------------- #

    def capabilities(self) -> Capabilities:           # pragma: no cover - abstract
        raise NotImplementedError

    async def _prepare(self, rt: TaskRuntime) -> None:
        """Adapter hook run inside the run task before the first turn
        (workspace, credentials, connecting). Raise AgentBackendError to fail."""

    async def _run(self, rt: TaskRuntime, message: str, resumed: bool) -> None:
        raise NotImplementedError                     # pragma: no cover

    async def _do_interrupt(self, rt: TaskRuntime) -> None:
        raise NotImplementedError                     # pragma: no cover

    async def _do_steer(self, rt: TaskRuntime, message: str) -> None:
        raise AgentBackendError(ErrorCode.not_supported,
                                f"{self.name} cannot steer a running task")

    async def _release(self, rt: TaskRuntime) -> None:
        """Free per-task resources (worker process, subscriptions). Idempotent."""

    def _spawn(self, coro) -> asyncio.Task:
        """create_task that keeps a reference until the task is done."""
        task = asyncio.create_task(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)
        return task

    # -- registry ----------------------------------------------------------- #

    def _check_spec(self, spec: AgentSpec) -> None:
        if spec.backend != self.name:
            raise AgentBackendError(
                ErrorCode.invalid_request,
                f"spec is for backend {spec.backend!r}, this is {self.name!r}")

    def _create(self, spec: AgentSpec, ctx: TaskContext, *, task_id: str | None = None,
                start_seq: int = 0) -> TaskRuntime:
        tid = task_id or ctx.task_id or ("t_" + uuid.uuid4().hex[:12])
        try:
            check_task_id(tid)
        except Exception as e:
            raise AgentBackendError(ErrorCode.invalid_request, str(e)) from e
        if tid in self._tasks:
            raise AgentBackendError(ErrorCode.invalid_request, f"task {tid!r} already exists")
        rt = TaskRuntime(
            task_id=tid, spec=spec, ctx=ctx,
            log=EventLog(tid, redact=self.redactor.redact, clock=self._clock, start_seq=start_seq),
            tracker=BudgetTracker(spec.budget), created_at=self._clock())
        self._tasks[tid] = rt
        return rt

    def _rt(self, task_id: str) -> TaskRuntime:
        rt = self._tasks.get(task_id)
        if rt is None:
            raise AgentBackendError(ErrorCode.invalid_request, f"unknown task {task_id!r}")
        return rt

    def restore_task(self, task_id: str, spec: AgentSpec, ctx: TaskContext, *,
                     session_id: str | None, last_seq: int = -1, usd_so_far: float = 0.0,
                     tokens_so_far: int = 0, turns_so_far: int = 0) -> None:
        """Re-register a task after a restart so ``resume`` works on it.

        W11b calls this for every non-terminal task row at startup (and lazily for
        a finished task a person asks to continue). The task comes back idle and
        resumable iff it has a backend session id; its lifetime totals are restored
        so the budget keeps counting where it left off.
        """
        self._check_spec(spec)
        rt = self._create(spec, ctx, task_id=task_id, start_seq=last_seq + 1)
        rt.session_id = session_id
        rt.resumable = bool(session_id)
        rt.state = TaskState.cancelled if session_id else TaskState.failed
        rt.tracker.record_total_cost(usd_so_far if usd_so_far else None)
        if tokens_so_far:
            rt.tracker.record_tokens(tokens_so_far, 0, total=True)
        if turns_so_far:
            rt.tracker.record_turns(turns_so_far)
            rt.turns_this_task = int(turns_so_far)

    def has_task(self, task_id: str) -> bool:
        return task_id in self._tasks

    def is_active(self, task_id: str) -> bool:
        """Is a run in progress for this task? (False for an unknown task.)"""
        rt = self._tasks.get(task_id)
        return bool(rt is not None and rt.log.active)

    def any_active(self) -> bool:
        return any(rt.log.active for rt in self._tasks.values())

    def patch_spec(self, task_id: str, **changes: Any) -> None:
        """Replace parts of a task's spec between runs (a chat turn's model or thinking level,
        a recomputed budget). The spec was validated when it was built; callers pass whole,
        already-validated values."""
        rt = self._rt(task_id)
        rt.spec = rt.spec.model_copy(update=changes)
        if "budget" in changes:
            rt.tracker.budget = rt.spec.budget

    def update_budget(self, task_id: str, budget: Budget, spent_today_usd: float = 0.0) -> None:
        """Swap in the budget a (re)start should be held to (W11b recomputes it from the
        spend ledger before every run: the per-day caps move as other tasks spend)."""
        self.patch_spec(task_id, budget=budget)
        self._rt(task_id).tracker.spent_today_usd = spent_today_usd

    def stop_for_budget(self, task_id: str, verdict: BudgetVerdict) -> None:
        """Hard-stop a RUNNING task for a limit the backend cannot see (a per-day cap
        shared with other tasks). Ends exactly like the watchdog's stop: cost_update,
        error(budget_exceeded), result(failed, not resumable)."""
        rt = self._rt(task_id)
        if not rt.log.active or rt.budget_stop is not None:
            return
        rt.budget_stop = verdict
        rt.interrupt_requested = True
        self._approvals.deny_all(task_id, reason="budget")
        self._spawn(self._do_interrupt(rt))

    def forget_task(self, task_id: str) -> bool:
        """Drop an idle task from the in-memory registry (it can be restored from the
        database on demand). Returns False if a run is still going."""
        rt = self._tasks.get(task_id)
        if rt is None:
            return True
        if rt.log.active:
            return False
        self._cancel_watchdog(rt)
        self._approvals.forget_task(task_id)
        del self._tasks[task_id]
        return True

    def _info(self, rt: TaskRuntime) -> TaskInfo:
        return TaskInfo(
            task_id=rt.task_id, backend=self.name, state=rt.state, session_id=rt.session_id,
            resumable=rt.resumable and not rt.closed and not rt.log.active,
            last_seq=rt.log.last_seq,
            cost_usd=round(rt.tracker.usd, 6) if rt.tracker.has_cost else None,
            tokens=rt.tracker.tokens, turns=rt.tracker.turns, created_at=rt.created_at)

    def task_info(self, task_id: str) -> TaskInfo:
        return self._info(self._rt(task_id))

    def events(self, task_id: str, *, after_seq: int = -1) -> AsyncIterator[Any]:
        return self._rt(task_id).log.stream(after_seq)

    # -- lifecycle ---------------------------------------------------------- #

    async def start_task(self, spec: AgentSpec, prompt: str, ctx: TaskContext) -> str:
        self._check_spec(spec)
        rt = self._create(spec, ctx)
        rt.log.emit(StatusEvent, state=TaskState.queued)
        self._launch(rt, prompt, resumed=False)
        return rt.task_id

    def _launch(self, rt: TaskRuntime, message: str, *, resumed: bool) -> None:
        rt.interrupt_requested = False
        rt.budget_stop = None
        rt.run_no += 1
        rt.log.begin_run()
        rt.run_task = asyncio.create_task(self._run_wrapper(rt, message, resumed, rt.run_no),
                                          name=f"agent-run-{rt.task_id}")

    async def _run_wrapper(self, rt: TaskRuntime, message: str, resumed: bool, run_no: int) -> None:
        def mine() -> bool:
            # A run's finalizer can be scheduled AFTER the next run has begun
            # (resume straight after a result): it must never end that newer run.
            return rt.run_no == run_no and rt.log.active

        try:
            await self._prepare(rt)
            await self._run(rt, message, resumed)
        except asyncio.CancelledError:
            if mine():
                self._finish_interrupted(rt)
            if not (rt.cancel_requested or rt.interrupt_requested):
                raise
        except AgentBackendError as e:
            if mine():
                self._finish_failed(rt, e)
        except Exception as e:                       # a bug in an adapter
            log.exception("agent run crashed (task %s)", rt.task_id)
            if mine():
                self._finish_failed(rt, AgentBackendError(
                    ErrorCode.internal, f"{type(e).__name__}: {e}"))
        finally:
            if mine():                                # _run returned without a result
                self._finish_failed(rt, AgentBackendError(
                    ErrorCode.internal, "the run ended without a result"))
            if rt.run_no == run_no:
                self._cancel_watchdog(rt)

    async def send(self, task_id: str, message: str) -> None:
        """Steer a running task, or continue an idle one with a follow-up turn."""
        rt = self._rt(task_id)
        if rt.closed:
            raise AgentBackendError(ErrorCode.invalid_request, f"task {task_id!r} was cancelled")
        if rt.log.active:
            await self._do_steer(rt, message)
            return
        await self.resume(task_id, message)

    async def resume(self, task_id: str, message: str | None = None) -> None:
        rt = self._rt(task_id)
        if rt.closed:
            raise AgentBackendError(ErrorCode.invalid_request, f"task {task_id!r} was cancelled")
        if rt.log.active:
            raise AgentBackendError(ErrorCode.invalid_request, f"task {task_id!r} is still running")
        if not rt.resumable:
            raise AgentBackendError(ErrorCode.invalid_request,
                                    f"task {task_id!r} has no session to resume")
        if not self.capabilities().resume:
            raise AgentBackendError(ErrorCode.not_supported, f"{self.name} cannot resume")
        await self._settle_previous_run(rt)
        self._launch(rt, message or "Continue.", resumed=True)

    async def _settle_previous_run(self, rt: TaskRuntime) -> None:
        prev = rt.run_task
        if prev is not None and not prev.done():
            await asyncio.wait({prev}, timeout=self.STOP_GRACE_S)

    async def interrupt(self, task_id: str) -> None:
        rt = self._rt(task_id)
        if not rt.log.active:
            raise AgentBackendError(ErrorCode.invalid_request, f"task {task_id!r} is not running")
        rt.interrupt_requested = True
        self._approvals.deny_all(task_id, reason="interrupted")
        await self._do_interrupt(rt)

    async def cancel(self, task_id: str) -> None:
        rt = self._rt(task_id)
        if rt.closed:
            return
        rt.cancel_requested = True
        rt.closed = True
        rt.resumable = False
        if rt.log.active:
            rt.interrupt_requested = True
            self._approvals.deny_all(task_id, reason="cancelled")
            with contextlib.suppress(Exception):
                await self._do_interrupt(rt)
            run = rt.run_task
            if run is not None and not run.done():
                try:
                    await asyncio.wait_for(asyncio.shield(run), timeout=self.STOP_GRACE_S)
                except (TimeoutError, asyncio.CancelledError, Exception):
                    run.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await run
        else:
            rt.log.begin_run()
            self._finish_cancelled(rt, reason="cancelled", resumable=False)
        await self._release(rt)
        self._approvals.forget_task(task_id)

    async def respond_approval(self, request_id: str, decision: Decision,
                               scope: Scope = "once") -> None:
        try:
            self._approvals.respond(request_id, decision, scope)
        except ApprovalError as e:
            raise AgentBackendError(ErrorCode.invalid_request, str(e)) from e

    async def list_sessions(self, ctx: TaskContext | None = None) -> list[SessionInfo]:
        return [SessionInfo(backend=self.name, session_id=rt.session_id, task_id=rt.task_id,
                            updated_at=rt.created_at)
                for rt in self._tasks.values() if rt.session_id]

    def _support_task(self, rt: TaskRuntime) -> dict[str, Any]:
        return {
            "state": rt.state.value, "session_id": rt.session_id, "resumable": rt.resumable,
            "events": rt.log.next_seq, "budget": rt.tracker.snapshot(),
            # prompts/instructions are excluded from support bundles by default (brief)
            "spec": rt.spec.model_dump(mode="json", exclude={"instructions"}),
        }

    def support_dump(self) -> dict[str, Any]:
        """Adapter state for a support bundle: no prompts, no instructions, no secrets."""
        return self.redactor.redact_obj({
            "backend": self.name,
            "tasks": {tid: self._support_task(rt) for tid, rt in self._tasks.items()}})

    async def aclose(self) -> None:
        for rt in list(self._tasks.values()):
            self._cancel_watchdog(rt)
            if rt.run_task is not None and not rt.run_task.done():
                rt.run_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await rt.run_task
            with contextlib.suppress(Exception):
                await self._release(rt)

    # -- run helpers shared by adapters ------------------------------------- #

    def _set_state(self, rt: TaskRuntime, state: TaskState, detail: str | None = None) -> None:
        rt.state = state
        rt.log.emit(StatusEvent, state=state, detail=detail)

    def _begin_running(self, rt: TaskRuntime, detail: str | None = None) -> None:
        rt.tracker.start()
        self._arm_watchdog(rt)
        self._set_state(rt, TaskState.running, detail)

    async def _request_approval(self, rt: TaskRuntime, *, tool_name: str, input: dict[str, Any],
                                call_id: str | None = None, reason: str | None = None,
                                parent_id: str | None = None) -> str:
        """Raise an approval_request, flip the task to awaiting_approval, wait for
        the answer, flip back. Returns ``"allow"`` or ``"deny"``."""
        tier = classify_tool(rt.spec.approval_policy, tool_name)
        if tier != "always_ask" and self._approvals.has_grant(rt.task_id, tool_name):
            return "allow"
        sticky = tier != "always_ask"
        pa = self._approvals.request(task_id=rt.task_id, tool_name=tool_name, input=input,
                                     call_id=call_id, sticky_ok=sticky)
        self._request_task[pa.request_id] = rt.task_id
        rt.log.emit(ApprovalRequestEvent, request_id=pa.request_id, tool_name=tool_name,
                    input=input, call_id=call_id, reason=reason, parent_id=parent_id,
                    scopes=["once", "task"] if sticky else ["once"])
        self._set_state(rt, TaskState.awaiting_approval, f"waiting for approval of {tool_name}")
        rt.tracker.pause()                    # human think-time is not agent wall clock
        try:
            timeout = rt.spec.approval_policy.timeout_s
            try:
                decision = await asyncio.wait_for(pa.future, timeout=timeout)
            except TimeoutError:
                self._approvals.deny_all(rt.task_id)
                decision = "deny"
        finally:
            rt.tracker.start()
        if rt.log.active:
            self._set_state(rt, TaskState.running)
        return decision

    # -- watchdog ----------------------------------------------------------- #

    def _arm_watchdog(self, rt: TaskRuntime) -> None:
        self._cancel_watchdog(rt)
        remaining = rt.tracker.remaining_wall_clock_s()
        if remaining is None:
            return
        rt.watchdog = asyncio.create_task(self._watchdog(rt))

    @staticmethod
    def _cancel_watchdog(rt: TaskRuntime) -> None:
        if rt.watchdog is not None and not rt.watchdog.done():
            rt.watchdog.cancel()
        rt.watchdog = None

    async def _watchdog(self, rt: TaskRuntime) -> None:
        # The tracker pauses while waiting on a human, so poll in short steps
        # against its elapsed clock rather than one long sleep.
        try:
            while True:
                remaining = rt.tracker.remaining_wall_clock_s()
                if remaining is None:
                    return
                if remaining <= 0:
                    break
                await asyncio.sleep(max(0.02, min(remaining, 0.25)))
        except asyncio.CancelledError:
            return
        if not rt.log.active:
            return
        rt.budget_stop = rt.tracker.check() or BudgetVerdict(
            "wall_clock", rt.spec.budget.max_wall_clock_s or 0.0, rt.tracker.elapsed_s)
        rt.interrupt_requested = True
        self._approvals.deny_all(rt.task_id, reason="budget")
        with contextlib.suppress(Exception):
            await self._do_interrupt(rt)

    # -- the ONE definition of how a run ends ------------------------------- #

    def _emit_cost(self, rt: TaskRuntime, source: str) -> None:
        t = rt.tracker
        rt.log.emit(CostUpdateEvent, total_cost_usd=round(t.usd, 6) if t.has_cost else None,
                    input_tokens=t.input_tokens or None, output_tokens=t.output_tokens or None,
                    turns=t.turns or None, source=source)

    def _result(self, rt: TaskRuntime, state: TaskState, *, reason: str | None,
                text: str | None, resumable: bool) -> None:
        assert state in TERMINAL_STATES
        rt.tracker.pause()
        self._cancel_watchdog(rt)
        self._approvals.deny_all(rt.task_id)
        rt.state = state
        rt.resumable = resumable and bool(rt.session_id) and not rt.closed
        rt.log.emit(ResultEvent, state=state, reason=reason, text=text, session_id=rt.session_id,
                    resumable=rt.resumable, num_turns=rt.tracker.turns or None,
                    duration_ms=int(rt.tracker.elapsed_s * 1000))

    def _finish_succeeded(self, rt: TaskRuntime, text: str | None) -> None:
        self._result(rt, TaskState.succeeded, reason=None, text=text, resumable=True)

    def _finish_failed(self, rt: TaskRuntime, err: AgentBackendError, *, resumable: bool = False) -> None:
        rt.log.emit(ErrorEvent, code=err.code, message=err.message, retryable=err.retryable,
                    detail=err.detail)
        self._result(rt, TaskState.failed, reason=err.code.value, text=None, resumable=resumable)

    def _finish_cancelled(self, rt: TaskRuntime, *, reason: str, resumable: bool) -> None:
        self._result(rt, TaskState.cancelled, reason=reason, text=None, resumable=resumable)

    def _finish_budget(self, rt: TaskRuntime, verdict: BudgetVerdict, *, emit_cost: bool = True) -> None:
        """Hard stop: a cost_update (always, even for non-money limits), the error,
        then failed/budget_exceeded. Not resumable - raising the budget is a
        deliberate act, not a click on Resume."""
        if emit_cost:
            self._emit_cost(rt, "budget_stop")
        self._finish_failed(rt, AgentBackendError(
            ErrorCode.budget_exceeded, verdict.message, detail=verdict.dimension))

    def _finish_interrupted(self, rt: TaskRuntime) -> None:
        """Resolve a run that was stopped on purpose (or by the watchdog)."""
        if rt.budget_stop is not None:
            self._finish_budget(rt, rt.budget_stop)
        elif rt.cancel_requested:
            self._finish_cancelled(rt, reason="cancelled", resumable=False)
        else:
            self._finish_cancelled(rt, reason="interrupted", resumable=True)

    def _check_budget_after_usage(self, rt: TaskRuntime) -> bool:
        """Emit a cost_update and, if a limit is crossed, end the run as a budget
        failure. Returns True when the run was ended."""
        self._emit_cost(rt, "usage")
        verdict = rt.tracker.check()
        if verdict is not None:
            self._finish_budget(rt, verdict, emit_cost=False)
            return True
        return False


def tool_in_allowlist(spec: AgentSpec, name: str) -> bool:
    """Is ``name`` permitted by the spec's allowlist (fnmatch, case-insensitive)?"""
    return any(fnmatch.fnmatchcase(name.lower(), pat.lower()) for pat in spec.tool_allowlist)
