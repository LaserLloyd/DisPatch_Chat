"""A scriptable fake AgentBackend.

It is both the reference implementation of the interface contract and the
backend W11b's route/UI tests drive (no model, no network, no processes). A
script is a list of steps; ``start_task`` picks one by the prompt
(``"scenario:<name>"``), so the contract suite can send the SAME prompt to every
adapter and each one answers in its own protocol (see tests/agents/contract).

The built-in scenarios (``builtin_scripts``):

    simple            two text deltas, a cost, a successful result
    tools             a read-only tool call and its result
    approval          a destructive tool call that needs approval, then its result
    subagent          a subagent started/finished under the main agent
    retry             a retry notice, then success
    long              streams, then waits (steerable) until interrupted or steered
    budget            reports a cost far over any sane budget
    error_unavailable / error_refused / error_auth   backend failures by code
"""
from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field

from .branding import tool_matches
from .events import (
    ArtifactEvent,
    ErrorCode,
    RetryEvent,
    SubagentFinishedEvent,
    SubagentStartedEvent,
    TextDeltaEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from .interface import (
    AgentBackendError,
    BaseBackend,
    Capabilities,
    TaskRuntime,
    classify_tool,
    tool_in_allowlist,
)

# --------------------------------------------------------------------------- #
# Script steps
# --------------------------------------------------------------------------- #


@dataclass
class Say:
    text: str
    chunks: int = 1
    delay: float = 0.0


@dataclass
class Tool:
    name: str
    input: dict = field(default_factory=dict)
    output: str = "ok"
    ok: bool = True


@dataclass
class Subagent:
    name: str
    description: str
    steps: list = field(default_factory=list)


@dataclass
class Retry:
    reason: str
    attempt: int = 1
    delay_s: float = 0.0


@dataclass
class MakeArtifact:
    name: str
    path: str


@dataclass
class Cost:
    usd: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    turns: int | None = None


@dataclass
class WaitForSteer:
    """Block until send() delivers a message (or the run is interrupted)."""


@dataclass
class Fail:
    code: ErrorCode
    message: str = "scripted failure"


@dataclass
class Crash:
    """An unexpected exception: must surface as ErrorCode.internal."""


@dataclass
class Finish:
    text: str


Step = Say | Tool | Subagent | Retry | MakeArtifact | Cost | WaitForSteer | Fail | Crash | Finish


def builtin_scripts() -> dict[str, list]:
    return {
        "simple": [Say("Hello world", chunks=2), Cost(usd=0.01, input_tokens=100, output_tokens=20,
                                                       turns=1), Finish("Hello world")],
        "tools": [Tool("Read", {"file_path": "notes.txt"}, output="file contents"),
                  Say("Read it."), Finish("Read it.")],
        "approval": [Tool("Bash", {"command": "echo hi"}, output="hi"), Say("Done."), Finish("Done.")],
        "subagent": [Subagent("researcher", "look things up", [Say("child says hi")]),
                     Say("All done."), Finish("All done.")],
        "retry": [Retry("rate limited", attempt=1, delay_s=0.0), Say("recovered"), Finish("recovered")],
        "artifact": [MakeArtifact("report", "report.md"), Finish("wrote report")],
        "long": [Say("working on it", chunks=3, delay=0.02), WaitForSteer(), Finish("steered")],
        "budget": [Say("expensive"), Cost(usd=0.10, input_tokens=4000, output_tokens=1000, turns=1),
                   Finish("never reached")],
        "error_unavailable": [Fail(ErrorCode.backend_unavailable, "backend is draining")],
        "error_refused": [Fail(ErrorCode.refused, "model not allowed")],
        "error_auth": [Fail(ErrorCode.auth_failed, "bad credentials")],
        "crash": [Crash()],
        # what every backend answers to a resume/continue turn
        "resume": [Say("resumed"), Finish("resumed")],
    }


# --------------------------------------------------------------------------- #
# The backend
# --------------------------------------------------------------------------- #


class FakeBackend(BaseBackend):
    name = "fake"

    def __init__(self, scripts: dict[str, list] | None = None, **kw) -> None:
        super().__init__(**kw)
        self.scripts = scripts if scripts is not None else builtin_scripts()

    def capabilities(self) -> Capabilities:
        return Capabilities(
            name="fake", label="Fake agent (tests)", approvals=True, steering=True, subagents=True,
            cost_reporting="usd", budget_enforcement="hard", same_host_required=False,
            data_leaves_host=False, permission_modes=["interactive", "unattended", "plan"])

    # -- run ----------------------------------------------------------------- #

    async def _run(self, rt: TaskRuntime, message: str, resumed: bool) -> None:
        rt.session_id = rt.session_id or f"fake-session-{rt.task_id}"
        rt.extras.setdefault("steer", asyncio.Queue())
        rt.extras["text"] = ""
        script = self._pick(message, resumed)
        self._begin_running(rt, "resumed" if resumed else None)
        inner = asyncio.create_task(self._play(rt, script))
        rt.extras["inner"] = inner
        try:
            await inner
        except asyncio.CancelledError:
            if not rt.interrupt_requested:
                raise
            self._finish_interrupted(rt)

    def _pick(self, message: str, resumed: bool) -> list:
        if message.startswith("scenario:"):
            name = message.split(":", 1)[1].strip()
            if name in self.scripts:
                return self.scripts[name]
        if resumed and "resume" in self.scripts:
            return self.scripts["resume"]
        return self.scripts.get("default", [Say("ok"), Finish("ok")])

    async def _play(self, rt: TaskRuntime, steps: list, parent_id: str | None = None) -> bool:
        """Run steps; return False when the run already ended (budget/failure)."""
        for step in steps:
            if isinstance(step, Say):
                await self._say(rt, step, parent_id)
            elif isinstance(step, Tool):
                await self._tool(rt, step, parent_id)
            elif isinstance(step, Subagent):
                if not await self._subagent(rt, step, parent_id):
                    return False
            elif isinstance(step, Retry):
                rt.log.emit(RetryEvent, attempt=step.attempt, delay_s=step.delay_s, reason=step.reason,
                            code=ErrorCode.rate_limited)
            elif isinstance(step, MakeArtifact):
                rt.log.emit(ArtifactEvent, artifact_id=f"art_{rt.task_id}_{step.name}", kind="file",
                            name=step.name, path=step.path)
            elif isinstance(step, Cost):
                rt.tracker.record_total_cost(step.usd)
                rt.tracker.record_tokens(step.input_tokens, step.output_tokens, total=True)
                rt.tracker.record_turns(step.turns)
                if self._check_budget_after_usage(rt):
                    return False
            elif isinstance(step, WaitForSteer):
                msg = await rt.extras["steer"].get()
                rt.log.emit(TextDeltaEvent, text=f"steered: {msg}", parent_id=parent_id)
                rt.extras["text"] += f"steered: {msg}"
            elif isinstance(step, Fail):
                raise AgentBackendError(step.code, step.message)
            elif isinstance(step, Crash):
                raise ValueError("scripted crash")
            elif isinstance(step, Finish):
                self._finish_succeeded(rt, step.text)
                return False
        if parent_id is None and rt.log.active:
            self._finish_succeeded(rt, rt.extras["text"] or None)
        return True

    async def _say(self, rt: TaskRuntime, step: Say, parent_id: str | None) -> None:
        words = step.text.split(" ")
        n = max(1, min(step.chunks, len(words)))
        size = -(-len(words) // n)
        for i in range(0, len(words), size):
            piece = " ".join(words[i:i + size])
            piece = (" " if i else "") + piece
            rt.log.emit(TextDeltaEvent, text=piece, parent_id=parent_id)
            rt.extras["text"] += piece
            if step.delay:
                await asyncio.sleep(step.delay)

    async def _tool(self, rt: TaskRuntime, step: Tool, parent_id: str | None) -> None:
        call_id = f"call_{rt.log.next_seq}"
        rt.log.emit(ToolCallEvent, call_id=call_id, name=step.name, input=step.input, parent_id=parent_id)
        pol = rt.spec.approval_policy
        allowed = tool_in_allowlist(rt.spec, step.name)
        decision = "allow"
        if not allowed:
            decision = "deny"
            why = "not in the agent's allowlist"
        elif pol.mode == "plan":
            decision, why = "deny", "plan mode is investigate-only"
        elif pol.mode == "unattended":
            ok = classify_tool(pol, step.name) == "auto" or tool_matches(step.name, pol.unattended_allow)
            decision, why = ("allow", "") if ok else ("deny", "unattended agents cannot ask")
        else:
            tier = classify_tool(pol, step.name)
            why = "denied by the approver"
            if tier != "auto":
                decision = await self._request_approval(
                    rt, tool_name=step.name, input=step.input, call_id=call_id, parent_id=parent_id)
        if decision == "allow":
            rt.log.emit(ToolResultEvent, call_id=call_id, ok=step.ok, output=step.output,
                        name=step.name, parent_id=parent_id)
        else:
            rt.log.emit(ToolResultEvent, call_id=call_id, ok=False, output=f"denied: {why}",
                        name=step.name, parent_id=parent_id)

    async def _subagent(self, rt: TaskRuntime, step: Subagent, parent_id: str | None) -> bool:
        sid = f"sub_{rt.log.next_seq}"
        rt.log.emit(ToolCallEvent, call_id=sid, name="Agent",
                    input={"description": step.description, "subagent_type": step.name},
                    parent_id=parent_id)
        rt.log.emit(SubagentStartedEvent, subagent_id=sid, parent_id=parent_id, name=step.name,
                    description=step.description)
        ok = await self._play(rt, step.steps, parent_id=sid)
        rt.log.emit(SubagentFinishedEvent, subagent_id=sid, parent_id=parent_id, ok=True,
                    summary=step.description)
        rt.log.emit(ToolResultEvent, call_id=sid, ok=True, output="subagent finished", name="Agent",
                    parent_id=parent_id)
        return ok

    # -- control ------------------------------------------------------------- #

    async def _do_interrupt(self, rt: TaskRuntime) -> None:
        inner = rt.extras.get("inner")
        if inner is not None and not inner.done():
            inner.cancel()

    async def _do_steer(self, rt: TaskRuntime, message: str) -> None:
        rt.extras["steer"].put_nowait(message)

    async def _release(self, rt: TaskRuntime) -> None:
        inner = rt.extras.get("inner")
        if inner is not None and not inner.done():
            inner.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await inner


__all__ = [
    "Cost",
    "Crash",
    "Fail",
    "FakeBackend",
    "Finish",
    "MakeArtifact",
    "Retry",
    "Say",
    "Subagent",
    "Tool",
    "WaitForSteer",
    "builtin_scripts",
]
