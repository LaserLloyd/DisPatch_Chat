"""AgentSpec validation (quarantine, unattended rule), tiers, restore, budgets."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.agents.branding import (
    effective_reads_untrusted,
    high_privilege_tools_of,
    quarantine_violations,
)
from app.agents.budget import (
    SDK_BUDGET_HEADROOM,
    Budget,
    BudgetTracker,
    effective_usd_cap,
    sdk_run_cap,
)
from app.agents.cli_adapter import GenericCliBackend
from app.agents.events import ErrorCode, TaskState
from app.agents.fake import FakeBackend, Say, Tool
from app.agents.interface import (
    AgentBackend,
    AgentBackendError,
    AgentSpec,
    ApprovalPolicy,
    McpServerSpec,
    TaskContext,
    classify_tool,
)

# ---- quarantine ---------------------------------------------------------------- #


def spec(**kw):
    return AgentSpec(backend="fake", **kw)


def test_a_reader_with_only_read_tools_is_fine():
    s = spec(tool_allowlist=["Read", "Grep", "WebFetch"], reads_untrusted_content=True)
    assert quarantine_violations(s) == []


@pytest.mark.parametrize("tool", ["Bash", "Write", "Edit", "MultiEdit", "NotebookEdit",
                                  "mcp__studioforge__delete_model", "mcp__x__admin_reset"])
def test_untrusted_content_and_a_high_privilege_tool_cannot_both_be_set(tool):
    with pytest.raises(ValidationError, match="untrusted content"):
        spec(tool_allowlist=["Read", tool], reads_untrusted_content=True)


def test_a_web_tool_implies_untrusted_even_if_the_author_said_otherwise():
    s = spec(tool_allowlist=["WebSearch"])
    assert effective_reads_untrusted(s) is True
    with pytest.raises(ValidationError):
        spec(tool_allowlist=["WebSearch", "Bash"], reads_untrusted_content=False)


def test_a_privileged_mcp_server_counts_as_high_privilege():
    srv = McpServerSpec(transport="http", url="http://127.0.0.1:1/mcp", privileged=True)
    with pytest.raises(ValidationError):
        spec(tool_allowlist=["Read"], reads_untrusted_content=True, mcp_servers={"sf": srv})
    ok = spec(tool_allowlist=["Read"], reads_untrusted_content=True,
              mcp_servers={"sf": srv.model_copy(update={"privileged": False})})
    assert high_privilege_tools_of(ok) == []


def test_extra_high_privilege_patterns_can_be_declared_by_the_spec():
    with pytest.raises(ValidationError):
        spec(tool_allowlist=["mcp__mail__archive"], reads_untrusted_content=True,
             high_privilege_tools=["mcp__mail__*"])


# ---- unattended rule -------------------------------------------------------------- #

def test_an_unattended_agent_cannot_hold_a_tool_that_needs_approval():
    with pytest.raises(ValidationError, match="unattended"):
        spec(tool_allowlist=["Read", "Bash"], approval_policy=ApprovalPolicy(mode="unattended"))
    ok = spec(tool_allowlist=["Read", "Bash"],
              approval_policy=ApprovalPolicy(mode="unattended", unattended_allow=["Bash"]))
    assert ok.approval_policy.mode == "unattended"
    assert spec(tool_allowlist=["Read"], approval_policy=ApprovalPolicy(mode="unattended"))


def test_no_bypass_permission_mode_is_representable():
    with pytest.raises(ValidationError):
        ApprovalPolicy(mode="bypassPermissions")
    with pytest.raises(ValidationError):
        ApprovalPolicy(mode="auto")


def test_unknown_spec_fields_are_rejected():
    with pytest.raises(ValidationError):
        spec(permission_mode="bypassPermissions")


def test_tool_tiers():
    pol = ApprovalPolicy(ask_tools=["WebFetch", "mcp__sf__*"])
    assert classify_tool(pol, "Read") == "auto"
    assert classify_tool(pol, "WebFetch") == "ask"
    assert classify_tool(pol, "mcp__sf__status") == "ask"
    assert classify_tool(pol, "Bash") == "always_ask"
    assert classify_tool(pol, "bash") == "always_ask", "case-insensitive"
    assert classify_tool(ApprovalPolicy(ask_tools=["Bash"]), "Bash") == "always_ask", "destructive wins"


def test_mcp_server_shapes_are_validated():
    with pytest.raises(ValidationError):
        McpServerSpec(transport="stdio")
    with pytest.raises(ValidationError):
        McpServerSpec(transport="http")


def test_required_permission_defaults_to_dispatch_agents():
    assert spec().required_permission == "dispatch_agents"


# ---- budgets ------------------------------------------------------------------------ #

def test_effective_cap_is_the_tighter_of_task_and_day():
    b = Budget(max_usd=5.0, max_usd_per_day=10.0)
    assert effective_usd_cap(b, 0) == 5.0
    assert effective_usd_cap(b, 7.0) == 3.0
    assert effective_usd_cap(b, 99.0) == 0.0
    assert effective_usd_cap(Budget(), 1.0) is None


def test_the_sdk_cap_leaves_headroom_and_counts_only_the_remainder_on_resume():
    b = Budget(max_usd=1.0)
    assert sdk_run_cap(b) == pytest.approx(1.0 * SDK_BUDGET_HEADROOM)
    assert sdk_run_cap(b, spent_task_usd=0.4) == pytest.approx(0.6 * SDK_BUDGET_HEADROOM)
    assert sdk_run_cap(b, spent_task_usd=2.0) == 0.0
    assert sdk_run_cap(Budget(max_turns=3)) is None


def test_tracker_limits_and_clock():
    now = [0.0]
    t = BudgetTracker(Budget(max_usd=1.0, max_tokens=100, max_turns=2, max_wall_clock_s=10),
                      clock=lambda: now[0])
    t.start()
    assert t.check() is None
    t.record_total_cost(1.0)
    assert t.check() is None, "exactly the cap is within budget"
    t.record_total_cost(1.01)
    assert t.check().dimension == "usd"
    t2 = BudgetTracker(Budget(max_tokens=100))
    t2.record_tokens(60, 50)
    assert t2.check().dimension == "tokens"
    t3 = BudgetTracker(Budget(max_turns=2))
    t3.record_turns(3)
    assert t3.check().dimension == "turns"
    now[0] = 11
    assert t.check().dimension == "usd"            # first failing dimension wins
    t4 = BudgetTracker(Budget(max_wall_clock_s=10), clock=lambda: now[0])
    t4.start()
    now[0] = 25
    assert t4.check().dimension == "wall_clock"


def test_pausing_stops_the_wall_clock_and_resume_does_not_reset_it():
    now = [0.0]
    t = BudgetTracker(Budget(max_wall_clock_s=10), clock=lambda: now[0])
    t.start(); now[0] = 4; t.pause()
    now[0] = 100                                    # a human thinking, or the task idle
    assert t.elapsed_s == 4 and t.check() is None
    t.start(); now[0] = 107
    assert t.elapsed_s == 11 and t.check().dimension == "wall_clock"


def test_per_day_spend_from_the_ledger_counts():
    t = BudgetTracker(Budget(max_usd_per_day=2.0), spent_today_usd=1.9)
    t.record_total_cost(0.2)
    assert t.check().dimension == "usd_per_day"


def test_snapshot_labels_cost_as_estimate():
    t = BudgetTracker(Budget())
    t.record_total_cost(0.5)
    assert t.snapshot()["estimate"] is True and t.snapshot()["usd"] == 0.5


# ---- the interface itself ----------------------------------------------------------------- #

def test_fake_and_cli_satisfy_the_protocol():
    assert isinstance(FakeBackend(), AgentBackend)
    assert isinstance(GenericCliBackend(), AgentBackend)


async def test_restore_task_makes_a_task_resumable_after_a_restart(tmp_path):
    be = FakeBackend(scripts={"resume": [Say("back")], "default": [Say("x")]})
    s = AgentSpec(backend="fake", tool_allowlist=["Read"])
    ctx = TaskContext(data_dir=tmp_path)
    be.restore_task("t_restored", s, ctx, session_id="sess-9", last_seq=41, usd_so_far=0.25)
    info = be.task_info("t_restored")
    assert info.resumable and info.session_id == "sess-9" and info.cost_usd == 0.25
    assert info.state == TaskState.cancelled
    await be.resume("t_restored")
    ev = [e async for e in be.events("t_restored", after_seq=41)]
    assert ev[0].seq == 42 and ev[-1].type == "result" and ev[-1].session_id == "sess-9"
    # a task with no backend session cannot be resumed
    be.restore_task("t_nosession", s, ctx, session_id=None)
    with pytest.raises(AgentBackendError) as ei:
        await be.resume("t_nosession")
    assert ei.value.code == ErrorCode.invalid_request
    await be.aclose()


async def test_task_ids_are_validated(tmp_path):
    be = FakeBackend()
    with pytest.raises(AgentBackendError) as ei:
        await be.start_task(AgentSpec(backend="fake"), "x", TaskContext(data_dir=tmp_path, task_id="../evil"))
    assert ei.value.code == ErrorCode.invalid_request


def test_cli_adapter_is_an_m3_stub():
    import asyncio
    with pytest.raises(NotImplementedError, match="M3"):
        asyncio.run(GenericCliBackend().start_task(AgentSpec(backend="cli"), "x", TaskContext(data_dir=".")))


async def test_a_denied_tool_in_the_fake_is_a_failed_tool_result_not_a_crash(tmp_path):
    be = FakeBackend(scripts={"default": [Tool("Write", {"file_path": "x"}), Say("ok")]})
    s = AgentSpec(backend="fake", tool_allowlist=["Read"])       # Write is not allowlisted
    tid = await be.start_task(s, "go", TaskContext(data_dir=tmp_path))
    ev = [e async for e in be.events(tid)]
    res = next(e for e in ev if e.type == "tool_result")
    assert res.ok is False and "allowlist" in res.output
    assert ev[-1].state == TaskState.succeeded
    await be.aclose()
