"""Behaviours of the scripted fake beyond the shared contract (approval tiers etc.)."""
from __future__ import annotations

import asyncio

from app.agents.events import ApprovalRequestEvent, ToolResultEvent
from app.agents.fake import FakeBackend, Finish, MakeArtifact, Say, Tool
from app.agents.interface import AgentSpec, ApprovalPolicy, TaskContext


async def run_with_approvals(be, spec, answers, prompt="go"):
    """Start a task and answer each approval_request from ``answers`` in order."""
    tid = await be.start_task(spec, prompt, TaskContext(data_dir="."))
    seen, asked = [], []

    async def go():
        async for e in be.events(tid):
            seen.append(e)
            if isinstance(e, ApprovalRequestEvent):
                asked.append(e)
                decision, scope = answers[len(asked) - 1]
                await be.respond_approval(e.request_id, decision, scope)

    await asyncio.wait_for(go(), 10)
    return tid, seen, asked


async def test_a_task_scope_grant_silences_later_asks_for_an_ask_tier_tool():
    be = FakeBackend(scripts={"default": [Tool("WebFetch", {"url": "https://a.example"}),
                                          Tool("WebFetch", {"url": "https://b.example"}), Finish("ok")]})
    spec = AgentSpec(backend="fake", tool_allowlist=["WebFetch"],
                     approval_policy=ApprovalPolicy(ask_tools=["WebFetch"]))
    tid, seen, asked = await run_with_approvals(be, spec, [("allow", "task")])
    assert len(asked) == 1, "second WebFetch ran under the task-wide grant"
    assert asked[0].scopes == ["once", "task"]
    assert [e.ok for e in seen if isinstance(e, ToolResultEvent)] == [True, True]


async def test_a_once_grant_asks_again():
    be = FakeBackend(scripts={"default": [Tool("WebFetch"), Tool("WebFetch"), Finish("ok")]})
    spec = AgentSpec(backend="fake", tool_allowlist=["WebFetch"],
                     approval_policy=ApprovalPolicy(ask_tools=["WebFetch"]))
    _, _, asked = await run_with_approvals(be, spec, [("allow", "once"), ("allow", "once")])
    assert len(asked) == 2


async def test_destructive_tools_ask_every_time_even_after_a_task_scope_allow():
    be = FakeBackend(scripts={"default": [Tool("Bash", {"command": "a"}), Tool("Bash", {"command": "b"}),
                                          Finish("ok")]})
    spec = AgentSpec(backend="fake", tool_allowlist=["Bash"])
    _, seen, asked = await run_with_approvals(be, spec, [("allow", "task"), ("allow", "task")])
    assert len(asked) == 2 and all(a.scopes == ["once"] for a in asked)


async def test_unattended_agents_do_not_ask_and_do_not_get_unlisted_tools(tmp_path):
    be = FakeBackend(scripts={"default": [Tool("Read"), Tool("Write", {"file_path": "x"}), Finish("ok")]})
    spec = AgentSpec(backend="fake", tool_allowlist=["Read", "Write"],
                     approval_policy=ApprovalPolicy(mode="unattended", unattended_allow=["Write"]))
    tid = await be.start_task(spec, "go", TaskContext(data_dir=tmp_path))
    ev = [e async for e in be.events(tid)]
    assert not any(isinstance(e, ApprovalRequestEvent) for e in ev)
    assert all(e.ok for e in ev if isinstance(e, ToolResultEvent))


async def test_plan_mode_is_investigate_only(tmp_path):
    be = FakeBackend(scripts={"default": [Tool("Read"), Tool("Write", {"file_path": "x"}), Finish("ok")]})
    spec = AgentSpec(backend="fake", tool_allowlist=["Read", "Write"],
                     approval_policy=ApprovalPolicy(mode="plan"))
    tid = await be.start_task(spec, "go", TaskContext(data_dir=tmp_path))
    results = [e for e in [x async for x in be.events(tid)] if isinstance(e, ToolResultEvent)]
    assert results[0].ok is False or results[0].name == "Read"
    assert results[1].ok is False and "plan" in results[1].output


async def test_artifact_events_are_emitted(tmp_path):
    be = FakeBackend(scripts={"default": [MakeArtifact("report", "out/report.md"), Say("x")]})
    tid = await be.start_task(AgentSpec(backend="fake"), "go", TaskContext(data_dir=tmp_path))
    art = next(e for e in [x async for x in be.events(tid)] if e.type == "artifact")
    assert art.path == "out/report.md" and art.kind == "file"


async def test_interrupt_while_awaiting_approval_denies_the_request(tmp_path):
    be = FakeBackend(scripts={"default": [Tool("Bash", {"command": "rm -rf x"}), Say("after")]})
    spec = AgentSpec(backend="fake", tool_allowlist=["Bash"])
    tid = await be.start_task(spec, "go", TaskContext(data_dir=tmp_path))
    it = be.events(tid).__aiter__()
    seen = []
    async for e in it:
        seen.append(e)
        if isinstance(e, ApprovalRequestEvent):
            break
    await be.interrupt(tid)
    async for e in it:
        seen.append(e)
    assert seen[-1].type == "result" and seen[-1].reason == "interrupted"
    assert not be._approvals.pending_for(tid)
    await be.aclose()
