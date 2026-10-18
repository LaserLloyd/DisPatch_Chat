"""Claude Agent adapter behaviours beyond the shared contract (fake SDK transport)."""
from __future__ import annotations

import asyncio
import json

import pytest
from agent_fakes import FAKE_API_KEY, FakeSdkTransport

from app.agents.budget import Budget
from app.agents.claude_sdk_adapter import ClaudeAgentBackend
from app.agents.events import (
    ApprovalRequestEvent,
    ArtifactEvent,
    CostUpdateEvent,
    ErrorCode,
    ErrorEvent,
    RetryEvent,
    TaskState,
    TextDeltaEvent,
    ToolResultEvent,
)
from app.agents.interface import AgentBackendError, AgentSpec, TaskContext
from app.agents.secrets import MemorySecretProvider


class Rig:
    def __init__(self, agent_data, **backend_kw):
        self.data = agent_data
        self.transports: list[FakeSdkTransport] = []
        self.backend = ClaudeAgentBackend(transport_factory=self._factory, **backend_kw)

    def _factory(self, rt, ws):
        t = FakeSdkTransport(session_id=f"sdk-{rt.task_id}")
        t.die_after_next_result = getattr(self, "die_first", False) and not self.transports
        self.transports.append(t)
        return t

    def spec(self, **kw):
        base = {"backend": "claude_sdk", "name": "a", "tool_allowlist": ["Read", "Bash", "Write"],
                    "budget": Budget(max_usd=1.0, max_turns=8)}
        base.update(kw)
        return AgentSpec(**base)

    def ctx(self, **kw):
        return TaskContext(data_dir=self.data, secrets=MemorySecretProvider({"ANTHROPIC_API_KEY": FAKE_API_KEY}),
                           **kw)

    async def run(self, scenario, **spec_kw):
        tid = await self.backend.start_task(self.spec(**spec_kw), f"scenario:{scenario}", self.ctx())
        return tid, [e async for e in self.backend.events(tid)]


@pytest.fixture
async def rig(agent_data):
    r = Rig(agent_data)
    yield r
    await r.backend.aclose()


async def test_an_allow_is_forwarded_to_the_worker_and_a_deny_carries_a_reason(rig):
    for decision, scope in (("allow", "once"), ("deny", "once")):
        tid = await rig.backend.start_task(rig.spec(), "scenario:approval", rig.ctx())
        async for e in rig.backend.events(tid):
            if isinstance(e, ApprovalRequestEvent):
                await rig.backend.respond_approval(e.request_id, decision, scope)
        t = rig.transports[-1]
        rid, allow, msg = t.permission_answers[0]
        assert rid == "r1" and allow is (decision == "allow")
        assert (msg is None) if allow else ("Denied" in msg)


async def test_a_tool_outside_the_allowlist_is_denied_without_asking_anyone(rig):
    tid, ev = await rig.run("approval", tool_allowlist=["Read"])       # Bash is not allowlisted
    assert not any(isinstance(e, ApprovalRequestEvent) for e in ev)
    t = rig.transports[-1]
    assert t.permission_answers and t.permission_answers[0][1] is False
    assert "allowlist" in t.permission_answers[0][2]
    assert next(e for e in ev if isinstance(e, ToolResultEvent)).ok is False


async def test_a_soft_budget_overshoot_reported_as_success_is_still_stopped(rig):
    """The SDK's cap can overshoot (RESEARCH.md R-COST): the adapter re-checks at 100%."""
    tid, ev = await rig.run("budget_soft", budget=Budget(max_usd=0.05))
    res = ev[-1]
    assert res.state == TaskState.failed and res.reason == "budget_exceeded"
    assert [e for e in ev if isinstance(e, CostUpdateEvent)][0].total_cost_usd == pytest.approx(0.10)


async def test_the_sdk_max_turns_stop_is_a_budget_stop(rig):
    class T(FakeSdkTransport):
        async def _sc_simple(self, sid):
            self._put({"kind": "result", "subtype": "error_max_turns", "is_error": True, "num_turns": 8,
                       "session_id": sid, "total_cost_usd": 0.2, "usage": {}, "result": None})
    rig.backend._factory = lambda rt, ws: T(session_id="s")
    tid, ev = await rig.run("simple", budget=Budget(max_turns=8))
    assert ev[-1].reason == "budget_exceeded"
    assert next(e for e in ev if isinstance(e, ErrorEvent)).detail == "turns"


async def test_the_session_id_is_persisted_from_the_init_and_the_result(rig):
    tid, ev = await rig.run("simple")
    assert ev[-1].session_id == f"sdk-{tid}" == rig.backend.task_info(tid).session_id


async def test_cost_comes_from_the_result_message_and_is_an_estimate(rig):
    tid, ev = await rig.run("simple")
    c = [e for e in ev if isinstance(e, CostUpdateEvent)][-1]
    assert c.total_cost_usd == pytest.approx(0.01) and c.estimate is True
    assert (c.input_tokens, c.output_tokens, c.turns) == (100, 20, 1)
    assert rig.backend.task_info(tid).cost_usd == pytest.approx(0.01)


async def test_api_retry_notices_become_retry_events(rig):
    tid, ev = await rig.run("retry")
    r = next(e for e in ev if isinstance(e, RetryEvent))
    assert (r.attempt, r.max_attempts, r.delay_s, r.code) == (1, 3, 0.5, ErrorCode.rate_limited)


async def test_partial_and_final_text_are_not_duplicated(rig):
    tid, ev = await rig.run("simple")
    assert "".join(e.text for e in ev if isinstance(e, TextDeltaEvent)) == "Hello world"


async def test_an_mcp_startup_error_fails_the_run_and_the_worker_is_not_reused(rig):
    """RESEARCH.md section 5 item 14: fail if mcp_server_errors is non-empty."""
    tid, ev = await rig.run("mcp_error")
    assert next(e for e in ev if isinstance(e, ErrorEvent)).code == ErrorCode.mcp_error
    assert ev[-1].state == TaskState.failed
    assert rig.transports[-1].closed


async def test_a_worker_that_cannot_start_fails_the_task(rig):
    rig.backend._factory = lambda rt, ws: _failing()
    tid, ev = await rig.run("simple")
    err = next(e for e in ev if isinstance(e, ErrorEvent))
    assert err.code == ErrorCode.backend_unavailable and err.retryable is True


def _failing():
    t = FakeSdkTransport()
    t.fail_start = AgentBackendError(ErrorCode.backend_unavailable, "could not start the agent worker")
    return t


async def test_interrupt_uses_client_interrupt_and_never_kills_the_worker_mid_turn(rig):
    tid = await rig.backend.start_task(rig.spec(), "scenario:long", rig.ctx())
    it = rig.backend.events(tid).__aiter__()
    async for e in it:
        if isinstance(e, TextDeltaEvent):
            break
    t = rig.transports[-1]
    await rig.backend.interrupt(tid)
    ev = [e async for e in it]
    assert t.interrupts == 1 and t.closed is False, "SIGTERM mid-turn would lose the turn (F6d)"
    assert ev[-1].reason == "interrupted"
    assert rig.backend.task_info(tid).resumable


async def test_resume_reuses_the_live_worker_without_restarting_it(rig):
    tid, ev = await rig.run("simple")
    await rig.backend.resume(tid, "scenario:simple")
    [e async for e in rig.backend.events(tid, after_seq=ev[-1].seq)]
    assert len(rig.transports) == 1 and len(rig.transports[0].queries) == 2


async def test_resume_after_the_worker_died_restarts_it_with_the_same_session(agent_data):
    r = Rig(agent_data)
    r.die_first = True
    tid, ev = await r.run("simple")
    first_session = ev[-1].session_id
    await asyncio.sleep(0.05)
    assert not r.transports[0].alive
    await r.backend.resume(tid, "scenario:resume")
    ev2 = [e async for e in r.backend.events(tid, after_seq=ev[-1].seq)]
    assert len(r.transports) == 2
    assert r.transports[1].plan.options["resume"] == first_session, "restart resumes the SAME session"
    assert ev2[-1].state == TaskState.succeeded and ev2[-1].session_id == first_session
    await r.backend.aclose()


async def test_a_worker_that_dies_mid_turn_is_an_unavailable_error_not_a_hang(rig):
    class Dying(FakeSdkTransport):
        async def _sc_simple(self, sid):
            self._put({"kind": "assistant", "content": [{"type": "text", "text": "x"}],
                       "parent_tool_use_id": None})
            self.kill()
    rig.backend._factory = lambda rt, ws: Dying(session_id="s")
    tid, ev = await rig.run("simple")
    assert next(e for e in ev if isinstance(e, ErrorEvent)).code == ErrorCode.backend_unavailable
    assert ev[-1].state == TaskState.failed


async def test_cancel_closes_the_worker_gracefully_after_the_turn_ends(rig):
    tid = await rig.backend.start_task(rig.spec(), "scenario:long", rig.ctx())
    it = rig.backend.events(tid).__aiter__()
    async for e in it:
        if isinstance(e, TextDeltaEvent):
            break
    await rig.backend.cancel(tid)
    ev = [e async for e in it]
    assert ev[-1].reason == "cancelled" and rig.transports[-1].closed


async def test_steering_sends_the_message_into_the_live_session(rig):
    tid = await rig.backend.start_task(rig.spec(), "scenario:long", rig.ctx())
    it = rig.backend.events(tid).__aiter__()
    async for e in it:
        if isinstance(e, TextDeltaEvent):
            break
    await rig.backend.send(tid, "turn left")
    ev = [e async for e in it]
    assert rig.transports[-1].queries[-1] == "turn left"
    assert ev[-1].state == TaskState.succeeded


async def test_artifacts_inside_the_workspace_are_reported_and_escapes_are_not(rig):
    tid, ev = await rig.run("write_file")
    arts = [e for e in ev if isinstance(e, ArtifactEvent)]
    assert [a.path for a in arts] == ["out/report.md"], arts
    assert not any("escape" in (a.path or "") for a in arts)


async def test_unknown_wire_messages_are_ignored(rig):
    class Noisy(FakeSdkTransport):
        async def _sc_simple(self, sid):
            self._put({"kind": "telemetry_from_the_future", "x": 1})
            self._put({"kind": "system", "subtype": "something_new", "data": {}})
            await super()._sc_simple(sid)
    rig.backend._factory = lambda rt, ws: Noisy(session_id="s")
    tid, ev = await rig.run("simple")
    assert ev[-1].state == TaskState.succeeded


async def test_an_unknown_error_result_maps_to_backend_error(rig):
    class Weird(FakeSdkTransport):
        async def _sc_simple(self, sid):
            self._put({"kind": "result", "subtype": "error_something_new", "is_error": True, "num_turns": 1,
                       "session_id": sid, "total_cost_usd": None, "usage": {}, "result": "it broke"})
    rig.backend._factory = lambda rt, ws: Weird(session_id="s")
    tid, ev = await rig.run("simple")
    err = next(e for e in ev if isinstance(e, ErrorEvent))
    assert err.code == ErrorCode.backend_error and "it broke" in err.message


async def test_a_task_restored_after_a_restart_resumes_in_a_fresh_worker(agent_data):
    r = Rig(agent_data)
    tid, ev = await r.run("simple")
    session = ev[-1].session_id
    await r.backend.aclose()

    fresh = Rig(agent_data)
    fresh.backend.restore_task(tid, fresh.spec(), fresh.ctx(task_id=tid), session_id=session,
                               last_seq=ev[-1].seq, usd_so_far=0.01)
    await fresh.backend.resume(tid, "scenario:resume")
    ev2 = [e async for e in fresh.backend.events(tid, after_seq=ev[-1].seq)]
    assert fresh.transports[0].plan.options["resume"] == session
    assert ev2[0].seq == ev[-1].seq + 1 and ev2[-1].session_id == session
    # the earlier spend still counts: max_budget_usd is the REMAINDER (R-COST)
    assert fresh.transports[0].plan.options["max_budget_usd"] == pytest.approx(0.99 * 0.9)
    await fresh.backend.aclose()


async def test_list_sessions_sees_transcripts_under_the_task_config_dirs(rig, agent_data):
    tid, ev = await rig.run("simple")
    proj = agent_data / "agents" / tid / ".claude" / "projects" / "-some-cwd"
    proj.mkdir(parents=True)
    (proj / "abc-123.jsonl").write_bytes(b"{}\n")
    got = {s.session_id: s for s in await rig.backend.list_sessions()}
    assert got[f"sdk-{tid}"].task_id == tid
    assert got["abc-123"].task_id == tid and got["abc-123"].size == 3


async def test_the_support_dump_has_no_secrets_and_no_prompts(rig):
    tid, ev = await rig.run("simple", instructions="PRIVATE-SYSTEM-PROMPT-TEXT")
    dump = json.dumps(rig.backend.support_dump())
    assert FAKE_API_KEY not in dump and "PRIVATE-SYSTEM-PROMPT-TEXT" not in dump
    assert tid in dump and "setting_sources" in dump
