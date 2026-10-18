"""The worker side: SDK message conversion, can_use_tool wiring, and the real
subprocess transport (pipes, graceful close, escalation) over a FAKE SDK.

Shapes of SDK messages are derived from the documented dataclasses (RESEARCH.md
F6e, R-SESS, R-COST); no live run was possible (no key, AGENT_LIVE_TESTS=off).
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest
from agent_fakes import (
    FAKE_API_KEY,
    AssistantMessage,
    ClaudeSDKClient,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    fake_sdk_module,
)

from app.agents import claude_sdk_adapter as adapter
from app.agents.claude_sdk_adapter import (
    ClaudeLaunchPlan,
    SubprocessTransport,
    message_to_wire,
    run_worker,
)
from app.agents.events import ErrorCode
from app.agents.interface import AgentBackendError
from app.agents.workspace import prepare_workspace

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "agents"


# ---- message conversion ------------------------------------------------------------------ #

def test_assistant_messages_convert_and_thinking_text_is_never_forwarded():
    w = message_to_wire(AssistantMessage([ThinkingBlock("secret reasoning"), TextBlock("hi"),
                                          ToolUseBlock("t1", "Read", {"file_path": "a"})],
                                         parent_tool_use_id="p", error="rate_limit"))
    assert w["kind"] == "assistant" and w["parent_tool_use_id"] == "p" and w["error"] == "rate_limit"
    assert w["content"] == [{"type": "thinking"}, {"type": "text", "text": "hi"},
                            {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "a"}}]
    assert "secret reasoning" not in json.dumps(w)


def test_user_result_system_and_stream_messages_convert():
    u = message_to_wire(UserMessage([ToolResultBlock("t1", "out", True)], parent_tool_use_id=None))
    assert u["content"][0] == {"type": "tool_result", "tool_use_id": "t1", "content": "out", "is_error": True}
    r = message_to_wire(ResultMessage("success", 12, False, 3, "s1", 0.5, {"input_tokens": 1}, "done"))
    assert r["kind"] == "result" and r["total_cost_usd"] == 0.5 and r["session_id"] == "s1"
    assert r["num_turns"] == 3 and r["result"] == "done" and r["usage"] == {"input_tokens": 1}
    assert message_to_wire(SystemMessage("init", {"session_id": "s1"})) == {
        "kind": "system", "subtype": "init", "data": {"session_id": "s1"}}
    assert message_to_wire(StreamEvent({"type": "x"}))["kind"] == "stream_event"
    assert message_to_wire(object()) is None


# ---- run_worker over the fake SDK ----------------------------------------------------------- #

def start_cmd(**over):
    plan = ClaudeLaunchPlan(
        options={"cwd": "/w", "setting_sources": [], "permission_mode": "default", "allowed_tools": ["Read"],
                 "disallowed_tools": [], "mcp_servers": {"sf": {"type": "http", "url": "http://x/mcp",
                                                                 "headers": {}}},
                 "extra_args": {"strict-mcp-config": None}, "env": {"CLAUDE_CODE_OAUTH_TOKEN": ""},
                 "include_partial_messages": True, "bogus_future_option": 1},
        secret_env={"ANTHROPIC_API_KEY": FAKE_API_KEY},
        secret_mcp_headers={"sf": {"Authorization": "Bearer sf-secret-123456"}},
        worker={"can_use_tool": True, "keepalive_pretooluse_hook": True})
    cmd = plan.to_wire()
    cmd.update(over)
    return cmd


async def drive(commands, *, sdk=None, wait_kinds=()):
    """Run the worker, feed commands, return (outputs, sdk_clients)."""
    inbox: asyncio.Queue = asyncio.Queue()
    out: list[dict] = []
    sdk = sdk or fake_sdk_module()
    task = asyncio.create_task(run_worker(inbox, out.append, sdk))
    for c in commands:
        if callable(c):
            await c(out)
        else:
            inbox.put_nowait(c)
            await asyncio.sleep(0.05)
    inbox.put_nowait(None)
    await asyncio.wait_for(task, 5)
    return out, list(ClaudeSDKClient.instances)


async def test_the_worker_builds_options_from_the_plan_and_injects_secrets_in_process():
    out, clients = await drive([start_cmd(), {"op": "query", "prompt": "hello"}])
    assert out[0] == {"kind": "ready"}
    c = clients[0]
    o = c.options
    assert o.setting_sources == [], "isolation survives the pipe"
    assert o.permission_mode == "default" and o.extra_args == {"strict-mcp-config": None}
    assert o.env["ANTHROPIC_API_KEY"] == FAKE_API_KEY and o.env["CLAUDE_CODE_OAUTH_TOKEN"] == ""
    assert o.mcp_servers["sf"]["headers"]["Authorization"] == "Bearer sf-secret-123456"
    assert o.can_use_tool is not None and "PreToolUse" in o.hooks, "the documented dummy-hook workaround"
    assert c.connected and c.disconnected and c.queries == ["hello"]
    kinds = [m["kind"] for m in out]
    assert kinds[:2] == ["ready", "system"] and kinds[-1] == "result"
    assert not any(FAKE_API_KEY in json.dumps(m) for m in out), "the key is never echoed back"


async def test_can_use_tool_round_trips_through_the_parent_allow_and_deny():
    async def answer(allow):
        async def go(out):
            for _ in range(100):
                req = next((m for m in out if m["kind"] == "permission_request"), None)
                if req:
                    break
                await asyncio.sleep(0.02)
            assert req["tool_name"] == "Bash" and req["input"] == {"command": "ls"}
            assert req["tool_use_id"] == "tu1"
            adapter_cmd = {"op": "permission", "request_id": req["request_id"], "allow": allow,
                           "message": None if allow else "nope"}
            inbox.put_nowait(adapter_cmd)
            await asyncio.sleep(0.1)
        return go

    for allow in (True, False):
        inbox: asyncio.Queue = asyncio.Queue()
        out: list[dict] = []
        ClaudeSDKClient.instances = []
        sdk = fake_sdk_module()
        task = asyncio.create_task(run_worker(inbox, out.append, sdk))
        inbox.put_nowait(start_cmd())
        inbox.put_nowait({"op": "query", "prompt": "needs-tool"})
        await (await answer(allow))(out)
        inbox.put_nowait(None)
        await asyncio.wait_for(task, 5)
        results = [b for m in out if m["kind"] == "user" for b in m["content"]]
        assert results[0]["is_error"] is (not allow)


async def test_interrupt_is_delivered_as_client_interrupt():
    out, clients = await drive([start_cmd(), {"op": "query", "prompt": "hello"}, {"op": "interrupt"}])
    assert clients[0].interrupted == 1


async def test_pending_permission_requests_are_denied_when_the_worker_closes():
    inbox: asyncio.Queue = asyncio.Queue()
    out: list[dict] = []
    ClaudeSDKClient.instances = []
    task = asyncio.create_task(run_worker(inbox, out.append, fake_sdk_module()))
    inbox.put_nowait(start_cmd())
    inbox.put_nowait({"op": "query", "prompt": "needs-tool"})
    await asyncio.sleep(0.1)
    inbox.put_nowait({"op": "close"})
    await asyncio.wait_for(task, 5)
    assert ClaudeSDKClient.instances[0].disconnected


async def test_a_missing_sdk_is_reported_as_sdk_missing(monkeypatch):
    def boom():
        raise AgentBackendError(ErrorCode.sdk_missing, "claude-agent-sdk is not installed")
    monkeypatch.setattr(adapter, "_import_sdk", boom)
    inbox: asyncio.Queue = asyncio.Queue()
    out: list[dict] = []
    inbox.put_nowait(start_cmd())
    await asyncio.wait_for(run_worker(inbox, out.append, None), 5)
    assert out == [{"kind": "worker_error", "code": "sdk_missing",
                    "message": "claude-agent-sdk is not installed"}]


async def test_an_sdk_without_a_security_option_is_a_hard_failure_not_a_silent_drop():
    import dataclasses

    @dataclasses.dataclass
    class NoSources:
        cwd: str | None = None
        permission_mode: str | None = None
        allowed_tools: list = dataclasses.field(default_factory=list)
        env: dict = dataclasses.field(default_factory=dict)
        mcp_servers: dict = dataclasses.field(default_factory=dict)
        extra_args: dict = dataclasses.field(default_factory=dict)
        disallowed_tools: list = dataclasses.field(default_factory=list)

    from agent_fakes import HookMatcher
    sdk = type("S", (), {"ClaudeAgentOptions": NoSources, "ClaudeSDKClient": ClaudeSDKClient,
                         "HookMatcher": HookMatcher})
    out, _ = await drive([start_cmd()], sdk=sdk)
    assert out[0]["kind"] == "worker_error" and out[0]["code"] == "options"
    assert "setting_sources" in out[0]["message"]


async def test_the_first_command_must_be_start():
    inbox: asyncio.Queue = asyncio.Queue()
    out: list[dict] = []
    inbox.put_nowait({"op": "query", "prompt": "x"})
    await asyncio.wait_for(run_worker(inbox, out.append, fake_sdk_module()), 5)
    assert out[0]["kind"] == "worker_error" and out[0]["code"] == "protocol"


# ---- a REAL subprocess ------------------------------------------------------------------------- #

def plan_for_subprocess():
    return start_plan()


def start_plan():
    return ClaudeLaunchPlan(
        options={"cwd": ".", "setting_sources": [], "permission_mode": "default", "allowed_tools": ["Read"],
                 "disallowed_tools": [], "mcp_servers": {}, "extra_args": {"strict-mcp-config": None},
                 "env": {"CLAUDE_CODE_OAUTH_TOKEN": ""}, "include_partial_messages": True},
        secret_env={"ANTHROPIC_API_KEY": FAKE_API_KEY},
        worker={"can_use_tool": True, "keepalive_pretooluse_hook": True})


async def test_subprocess_transport_runs_a_turn_and_closes_gracefully(agent_data, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-AMBIENT-12345678")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-AMBIENT-12345678")
    ws = prepare_workspace(agent_data, "proc-1")
    t = SubprocessTransport(ws, argv=[sys.executable, "-P", str(FIX / "fake_worker.py")])
    await t.start(start_plan())
    assert t.alive
    it = t.messages().__aiter__()
    await t.query("hello")
    kinds = []
    while True:
        m = await asyncio.wait_for(it.__anext__(), 10)
        kinds.append(m["kind"])
        if m["kind"] == "result":
            assert m["session_id"] == "w-sess" and m["total_cost_usd"] == 0.01
            break
    assert kinds[0] == "system"
    t0 = time.monotonic()
    await t.close()
    assert time.monotonic() - t0 < 8, "stdin EOF is enough: no escalation was needed"
    assert not t.alive

    report = json.loads((ws.base / "worker_report.json").read_text())
    blob = json.dumps(report)
    assert "oauth-AMBIENT" not in blob and "CLAUDE_CODE_OAUTH_TOKEN" not in report["env_keys"]
    assert "ANTHROPIC_API_KEY" not in report["env_keys"], "the key is not in the worker's own environment"
    assert report["home"] == str(ws.home) and Path(report["cwd"]).resolve() == ws.base.resolve()
    client = report["clients"][0]
    assert client["setting_sources"] == [] and client["has_callback"] and client["has_hooks"]
    assert "ANTHROPIC_API_KEY" in client["env_keys"], "...it is injected into the SDK options in-process"
    assert client["disconnected"] is True


async def test_subprocess_transport_permission_round_trip(agent_data):
    ws = prepare_workspace(agent_data, "proc-2")
    t = SubprocessTransport(ws, argv=[sys.executable, "-P", str(FIX / "fake_worker.py")])
    await t.start(start_plan())
    it = t.messages().__aiter__()
    await t.query("needs-tool")
    seen = []
    while True:
        m = await asyncio.wait_for(it.__anext__(), 10)
        seen.append(m)
        if m["kind"] == "permission_request":
            await t.answer_permission(m["request_id"], True)
        if m["kind"] == "result":
            break
    tool_results = [b for m in seen if m["kind"] == "user" for b in m["content"]]
    assert tool_results and tool_results[0]["is_error"] is False
    await t.close()


async def test_a_worker_that_ignores_close_is_escalated_and_reaped(agent_data):
    ws = prepare_workspace(agent_data, "proc-3")
    t = SubprocessTransport(ws, argv=[sys.executable, "-P", str(FIX / "stubborn_worker.py")])
    t.CLOSE_GRACE_S = 0.5
    await t.start(start_plan())
    pid = t.pid
    assert pid and t.alive
    await asyncio.wait_for(t.close(), 30)
    assert not t.alive and t._proc.returncode is not None


async def test_a_worker_that_dies_during_start_up_is_a_catalogued_error(agent_data):
    ws = prepare_workspace(agent_data, "proc-4")
    t = SubprocessTransport(ws, argv=[sys.executable, "-P", "-c", "import sys; sys.exit(3)"])
    with pytest.raises(AgentBackendError) as ei:
        await t.start(start_plan())
    assert ei.value.code == ErrorCode.backend_unavailable
    await t.close()


async def test_the_real_entry_point_without_the_sdk_reports_sdk_missing(agent_data):
    """`python -P -m app.agents.claude_sdk_adapter` in an env without claude-agent-sdk."""
    import importlib.util
    if importlib.util.find_spec("claude_agent_sdk") is not None:
        pytest.skip("the SDK is installed here")
    ws = prepare_workspace(agent_data, "proc-5")
    t = SubprocessTransport(ws)                                    # the production argv
    with pytest.raises(AgentBackendError) as ei:
        await t.start(start_plan())
    assert ei.value.code == ErrorCode.sdk_missing
    assert "agents-claude" in ei.value.message
    await t.close()
