"""What the Claude Agent adapter would tell the SDK - the isolation and secrets rules (G17).

Everything here inspects the launch PLAN (pure function) or drives the backend
against a fake transport; the real SDK is never needed. RESEARCH.md ids are in
the test names' docstrings.
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from agent_fakes import FAKE_API_KEY, FakeSdkTransport, fake_sdk_module

from app.agents.budget import SDK_BUDGET_HEADROOM, Budget
from app.agents.claude_sdk_adapter import (
    CLIENT_APP,
    FORBIDDEN_PERMISSION_MODES,
    KNOWN_BUILTIN_TOOLS,
    ClaudeAgentBackend,
    build_launch_plan,
    decide_permission,
    expand_allowlist,
    make_sdk_options,
    worker_env,
)
from app.agents.cli_adapter import build_claude_cli_command
from app.agents.events import ErrorCode, ErrorEvent, TaskState
from app.agents.interface import (
    AgentBackendError,
    AgentSpec,
    ApprovalPolicy,
    McpServerSpec,
    TaskContext,
    WorkspacePolicy,
)
from app.agents.secrets import MemorySecretProvider
from app.agents.workspace import prepare_workspace

FIXTURE_REPO = Path(__file__).resolve().parents[1] / "fixtures" / "agents" / "malicious_repo"


def spec(**kw) -> AgentSpec:
    base = {"backend": "claude_sdk", "name": "a", "tool_allowlist": ["Read", "Grep", "Bash"],
                "budget": Budget(max_usd=1.0, max_turns=8)}
    base.update(kw)
    return AgentSpec(**base)


@pytest.fixture
def ws(agent_data):
    return prepare_workspace(agent_data, "task-1")


def plan_for(s, ws, agent_data, **kw):
    kw.setdefault("api_key", FAKE_API_KEY)
    return build_launch_plan(s, TaskContext(data_dir=agent_data), ws, **kw)


# ---- the always-on isolation options ------------------------------------------------ #

def test_setting_sources_is_explicitly_empty_by_default(ws, agent_data):
    """RESEARCH.md F4/C1, section 5 item 6: never rely on the SDK default."""
    p = plan_for(spec(), ws, agent_data)
    assert "setting_sources" in p.options and p.options["setting_sources"] == []


def test_trusting_repo_settings_is_an_explicit_opt_in(ws, agent_data):
    s = spec(workspace_policy=WorkspacePolicy(trust_repo_settings=True))
    assert plan_for(s, ws, agent_data).options["setting_sources"] == ["project"]


def test_mcp_is_explicit_and_strict(ws, agent_data):
    """R-MCP, F6b: a repo .mcp.json must never load."""
    s = spec(mcp_servers={"sf": McpServerSpec(transport="http", url="http://127.0.0.1:11234/mcp",
                                              headers={"X-Mode": "ro"})})
    p = plan_for(s, ws, agent_data)
    assert p.options["extra_args"] == {"strict-mcp-config": None}
    assert p.options["mcp_servers"] == {"sf": {"type": "http", "url": "http://127.0.0.1:11234/mcp",
                                               "headers": {"X-Mode": "ro"}}}
    assert plan_for(spec(), ws, agent_data).options["mcp_servers"] == {}


def test_strict_mcp_stays_on_even_when_repo_settings_are_trusted(ws, agent_data):
    s = spec(workspace_policy=WorkspacePolicy(trust_repo_settings=True))
    assert "strict-mcp-config" in plan_for(s, ws, agent_data).options["extra_args"]


def test_the_environment_isolates_config_memory_and_subagents(ws, agent_data):
    """R-ISO, section 5 items 5, 9, 10."""
    env = plan_for(spec(max_subagent_depth=1, max_concurrent_subagents=2), ws, agent_data).options["env"]
    assert env["CLAUDE_CONFIG_DIR"] == str(agent_data / "agents" / "task-1" / ".claude")
    assert env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
    assert env["CLAUDE_AGENT_SDK_CLIENT_APP"] == CLIENT_APP == "DisPatch"
    assert env["CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH"] == "1"
    assert env["CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS"] == "2"
    assert env["HOME"] == env["USERPROFILE"] == str(ws.home), "the user's ~/.claude.json is never read"


def test_the_oauth_token_is_blanked_in_the_sdk_env(ws, agent_data):
    """Section 5 item 2: a subscription token is never forwarded."""
    env = plan_for(spec(), ws, agent_data).options["env"]
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "" and env["ANTHROPIC_AUTH_TOKEN"] == ""


def test_extra_env_cannot_smuggle_credentials(ws, agent_data):
    for k in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"):
        with pytest.raises(AgentBackendError) as ei:
            build_launch_plan(spec(), TaskContext(data_dir=agent_data, extra_env={k: "x" * 20}), ws,
                              api_key=FAKE_API_KEY)
        assert ei.value.code == ErrorCode.policy_violation


def test_the_worker_environment_is_an_allowlist(ws, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-SECRET-12345678")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-ambient-12345678")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret-12345678")
    monkeypatch.setenv("SOME_RANDOM_VAR", "x")
    env = worker_env(ws)
    blob = json.dumps(env)
    for leaked in ("oauth-SECRET", "sk-ant-ambient", "aws-secret", "SOME_RANDOM_VAR"):
        assert leaked not in blob
    assert env["HOME"] == str(ws.home) and "PYTHONPATH" in env
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env and "ANTHROPIC_API_KEY" not in env


def test_the_api_key_travels_in_secret_env_only(ws, agent_data):
    """Section 5 item 5: not in options, not in describe(), not in the worker env."""
    p = plan_for(spec(), ws, agent_data)
    assert p.secret_env == {"ANTHROPIC_API_KEY": FAKE_API_KEY}
    assert FAKE_API_KEY not in json.dumps(p.options)
    assert FAKE_API_KEY not in json.dumps(p.describe())
    assert FAKE_API_KEY not in json.dumps(worker_env(ws))
    assert p.to_wire()["secret_env"] == p.secret_env      # ... and only on the pipe


def test_a_missing_key_is_an_auth_error_not_a_silent_login_fallback(ws, agent_data):
    with pytest.raises(AgentBackendError) as ei:
        plan_for(spec(), ws, agent_data, api_key=None)
    assert ei.value.code == ErrorCode.auth_failed and "claude.ai" in ei.value.message


@pytest.mark.parametrize("route,flag", [("bedrock", "CLAUDE_CODE_USE_BEDROCK"),
                                        ("vertex", "CLAUDE_CODE_USE_VERTEX"),
                                        ("anthropic_aws", "CLAUDE_CODE_USE_ANTHROPIC_AWS")])
def test_provider_routes_set_their_switch_and_need_no_api_key(ws, agent_data, route, flag):
    p = plan_for(spec(), ws, agent_data, api_key=None, auth_route=route)
    assert p.options["env"][flag] == "1" and p.secret_env == {}


# ---- permission modes and tools ------------------------------------------------------ #

def test_interactive_agents_use_default_mode_and_route_asks_through_can_use_tool(ws, agent_data):
    """F5/R-CUT: a tool that needs approval must NOT be in allowed_tools (allow rules precede canUseTool)."""
    p = plan_for(spec(), ws, agent_data)
    assert p.options["permission_mode"] == "default"
    assert p.options["allowed_tools"] == ["Read", "Grep"], "Bash is destructive: it must ask"
    assert "Bash" not in p.options["disallowed_tools"], "...but it is not removed either"
    assert p.worker == {"can_use_tool": True, "keepalive_pretooluse_hook": True}


def test_every_builtin_tool_the_agent_was_not_given_is_disallowed_by_name(ws, agent_data):
    """Section 5 item 7: bare names."""
    p = plan_for(spec(tool_allowlist=["Read"]), ws, agent_data)
    dis = set(p.options["disallowed_tools"])
    assert "Read" not in dis
    assert {"Bash", "Write", "Edit", "WebFetch", "WebSearch", "Task", "Agent", "AskUserQuestion"} <= dis
    assert dis <= set(KNOWN_BUILTIN_TOOLS)


def test_the_subagent_tool_is_matched_under_both_names(ws, agent_data):
    """Item 17: Agent (new) and Task (old)."""
    assert expand_allowlist(["Agent"]) == ["Agent", "Task"]
    p = plan_for(spec(tool_allowlist=["Agent"]), ws, agent_data)
    assert "Task" in p.options["allowed_tools"] and "Task" not in p.options["disallowed_tools"]


def test_unattended_agents_use_dontask_with_an_explicit_allowlist_and_no_callback(ws, agent_data):
    s = spec(tool_allowlist=["Read", "Grep"], approval_policy=ApprovalPolicy(mode="unattended"))
    p = plan_for(s, ws, agent_data)
    assert p.options["permission_mode"] == "dontAsk"
    assert p.options["allowed_tools"] == ["Read", "Grep"]
    assert p.worker == {"can_use_tool": False, "keepalive_pretooluse_hook": False}


def test_plan_agents_are_read_only(ws, agent_data):
    s = spec(approval_policy=ApprovalPolicy(mode="plan"), tool_allowlist=["Read", "Bash", "Write"])
    p = plan_for(s, ws, agent_data)
    assert p.options["permission_mode"] == "plan"
    assert p.options["allowed_tools"] == ["Read"]
    assert "ExitPlanMode" not in p.options["disallowed_tools"]


@pytest.mark.parametrize("mode", ["interactive", "unattended", "plan"])
def test_no_forbidden_permission_mode_can_be_produced(ws, agent_data, mode):
    """R-PM, section 5 item 7: bypassPermissions and auto are not representable."""
    tools = ["Read"] if mode != "interactive" else ["Read", "Bash"]
    p = plan_for(spec(tool_allowlist=tools, approval_policy=ApprovalPolicy(mode=mode)), ws, agent_data)
    assert p.options["permission_mode"] not in FORBIDDEN_PERMISSION_MODES
    assert p.options["permission_mode"] in {"default", "dontAsk", "plan"}


def test_budget_options_leave_headroom_and_cap_turns(ws, agent_data):
    """R-COST, section 5 item 9."""
    p = plan_for(spec(budget=Budget(max_usd=2.0, max_turns=5)), ws, agent_data)
    assert p.options["max_turns"] == 5
    assert p.options["max_budget_usd"] == pytest.approx(2.0 * SDK_BUDGET_HEADROOM)
    p2 = plan_for(spec(budget=Budget(max_usd=2.0)), ws, agent_data, spent_task_usd=1.0)
    assert p2.options["max_budget_usd"] == pytest.approx(1.0 * SDK_BUDGET_HEADROOM), "only the remainder"


def test_resume_passes_the_session_id(ws, agent_data):
    assert plan_for(spec(), ws, agent_data, resume_session_id="sess-7").options["resume"] == "sess-7"
    assert "resume" not in plan_for(spec(), ws, agent_data).options


@pytest.mark.parametrize("tool,mode,expect", [
    ("Read", "interactive", "allow"), ("Bash", "interactive", "ask"), ("Write", "interactive", "ask"),
    ("WebFetch", "interactive", "deny"),                       # not in the allowlist
    ("Read", "plan", "allow"), ("Bash", "plan", "deny"), ("ExitPlanMode", "plan", "allow"),
    ("Read", "unattended", "allow"), ("Bash", "unattended", "deny"),
])
def test_permission_decisions(tool, mode, expect):
    s = spec(tool_allowlist=["Read", "Bash", "Write"] if mode != "unattended" else ["Read"],
             approval_policy=ApprovalPolicy(mode=mode))
    assert decide_permission(s, tool)[0] == expect


# ---- the SDK options object -------------------------------------------------------------- #

def test_make_sdk_options_passes_the_plan_through_and_fails_loudly_on_drift():
    sdk = fake_sdk_module()
    opts = make_sdk_options({"setting_sources": [], "permission_mode": "default", "cwd": "/w",
                             "not_a_real_option": 1}, sdk)
    assert opts.setting_sources == [] and opts.permission_mode == "default"

    import dataclasses

    @dataclasses.dataclass
    class OldDc:
        cwd: str | None = None
        permission_mode: str | None = None
    old = type("Old", (), {"ClaudeAgentOptions": OldDc})
    with pytest.raises(AgentBackendError, match="setting_sources"):
        make_sdk_options({"setting_sources": [], "permission_mode": "default"}, old)


@pytest.mark.skipif(importlib.util.find_spec("claude_agent_sdk") is None,
                    reason="optional extra agents-claude not installed")
def test_the_real_sdk_accepts_the_options_we_build(ws, agent_data):
    import claude_agent_sdk

    p = plan_for(spec(mcp_servers={"sf": McpServerSpec(transport="http", url="http://127.0.0.1:1/mcp")}),
                 ws, agent_data)
    opts = make_sdk_options(p.options | {"env": p.options["env"] | p.secret_env}, claude_agent_sdk)
    assert opts.setting_sources == [] and opts.permission_mode == "default"
    assert opts.max_turns == 8


# ---- G17: a hostile repo --------------------------------------------------------------------- #

def git(*args, cwd):
    subprocess.run(["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t", *args], cwd=cwd,
                   check=True, capture_output=True)


@pytest.fixture
def hostile_repo(tmp_path):
    repo = tmp_path / "hostile"
    shutil.copytree(FIXTURE_REPO, repo)
    git("init", "-q", cwd=repo)
    git("add", "-A", "-f", cwd=repo)
    git("commit", "-q", "-m", "init", cwd=repo)
    return repo


def markers(*roots: Path) -> list[Path]:
    return [p for r in roots for p in r.rglob("*.marker")]


async def test_a_task_on_a_hostile_repo_loads_neither_its_hooks_nor_its_mcp_servers(agent_data, hostile_repo):
    be = ClaudeAgentBackend(transport_factory=lambda rt, w: FakeSdkTransport())
    s = spec(tool_allowlist=["Read", "Grep"], workspace_policy=WorkspacePolicy(mode="worktree"))
    ctx = TaskContext(data_dir=agent_data, task_id="hostile-1", repo_path=hostile_repo,
                      secrets=MemorySecretProvider({"ANTHROPIC_API_KEY": FAKE_API_KEY}))
    tid = await be.start_task(s, "scenario:simple", ctx)
    ev = [e async for e in be.events(tid)]
    assert ev[-1].state == TaskState.succeeded

    root = Path(be._tasks[tid].extras["workspace"].root)
    assert (root / ".claude" / "settings.json").is_file(), "the threat is really in the agent's cwd"
    assert (root / ".mcp.json").is_file() and (root / "CLAUDE.md").is_file()
    opts = be._tasks[tid].extras["plan"].options
    assert opts["cwd"] == str(root)
    assert opts["setting_sources"] == [], "no project settings: the repo's hooks cannot load"
    assert opts["mcp_servers"] == {} and "strict-mcp-config" in opts["extra_args"], ".mcp.json cannot load"
    assert opts["permission_mode"] == "default" and opts["allowed_tools"] == ["Read", "Grep"]
    assert markers(root, hostile_repo, agent_data) == [], "nothing from the repo executed"
    await be.aclose()


async def test_opting_in_to_repo_settings_is_visible_in_the_plan_and_the_log(agent_data, hostile_repo, caplog):
    import logging

    be = ClaudeAgentBackend(transport_factory=lambda rt, w: FakeSdkTransport())
    s = spec(tool_allowlist=["Read"],
             workspace_policy=WorkspacePolicy(mode="worktree", trust_repo_settings=True))
    ctx = TaskContext(data_dir=agent_data, task_id="trusted-1", repo_path=hostile_repo,
                      secrets=MemorySecretProvider({"ANTHROPIC_API_KEY": FAKE_API_KEY}))
    with caplog.at_level(logging.WARNING, logger="dispatch.agents"):
        tid = await be.start_task(s, "scenario:simple", ctx)
        [e async for e in be.events(tid)]
    assert be._tasks[tid].extras["plan"].options["setting_sources"] == ["project"]
    assert "repo's hooks WILL execute" in caplog.text
    await be.aclose()

def test_the_generic_cli_preset_is_bare_with_empty_setting_sources(tmp_path):
    """The M3 preset must be safe against the same hostile repo (F6a/F6b)."""
    cmd = build_claude_cli_command(prompt="x", allowed_tools=["Read"], mcp_config=tmp_path / "m.json")
    assert "--bare" in cmd
    assert cmd[cmd.index("--setting-sources") + 1] == ""
    assert "--strict-mcp-config" in cmd


async def test_a_missing_key_fails_the_task_with_auth_failed(agent_data):
    be = ClaudeAgentBackend(transport_factory=lambda rt, w: FakeSdkTransport())
    tid = await be.start_task(spec(), "scenario:simple", TaskContext(data_dir=agent_data))
    ev = [e async for e in be.events(tid)]
    assert next(e for e in ev if isinstance(e, ErrorEvent)).code == ErrorCode.auth_failed
    await be.aclose()
