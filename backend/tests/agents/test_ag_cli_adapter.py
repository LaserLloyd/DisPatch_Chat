"""The generic CLI adapter stub and the Claude preset command (RESEARCH.md section 5 items 13, 14, 16)."""
from __future__ import annotations

import pytest

from app.agents.cli_adapter import (
    GenericCliBackend,
    build_claude_cli_command,
    parse_stream_json_line,
)
from app.agents.interface import AgentSpec, TaskContext

BASE = {"allowed_tools": ["Read", "Grep"], "mcp_config": "mcp.json"}


def flag(cmd, name):
    return cmd[cmd.index(name) + 1]


def test_the_preset_is_bare_headless_stream_json_with_isolation_flags():
    cmd = build_claude_cli_command(prompt="hello", **BASE)
    assert cmd[:3] == ["claude", "--bare", "-p"] and cmd[3] == "hello"
    assert flag(cmd, "--output-format") == "stream-json" and "--verbose" in cmd
    assert "--include-partial-messages" in cmd
    assert flag(cmd, "--setting-sources") == "", "empty setting sources, passed explicitly"
    assert "--strict-mcp-config" in cmd and flag(cmd, "--mcp-config") == "mcp.json"
    assert flag(cmd, "--allowedTools") == "Read,Grep"
    assert flag(cmd, "--permission-mode") == "dontAsk"
    assert "--no-session-persistence" in cmd and "--resume" not in cmd
    assert "--permission-prompts" not in cmd, "only unattended runs add it"


def test_unattended_runs_add_permission_prompts_none():
    cmd = build_claude_cli_command(unattended=True, **BASE)
    assert flag(cmd, "--permission-prompts") == "none"


def test_budget_turns_schema_and_resume_flags():
    cmd = build_claude_cli_command(max_turns=5, max_budget_usd=1.5, json_schema={"type": "object"},
                                   resume_session_id="sess-1", forward_subagent_text=True, **BASE)
    assert flag(cmd, "--max-turns") == "5" and flag(cmd, "--max-budget-usd") == "1.5"
    assert flag(cmd, "--json-schema") == '{"type": "object"}'
    assert flag(cmd, "--resume") == "sess-1" and "--no-session-persistence" not in cmd
    assert "--forward-subagent-text" in cmd


def test_the_prompt_can_go_over_stdin():
    cmd = build_claude_cli_command(prompt=None, **BASE)
    assert cmd[:3] == ["claude", "--bare", "-p"] and cmd[3].startswith("--")


@pytest.mark.parametrize("mode", ["bypassPermissions", "auto", "whatever"])
def test_unsafe_permission_modes_are_refused(mode):
    with pytest.raises(ValueError):
        build_claude_cli_command(permission_mode=mode, **BASE)


def test_an_empty_allowlist_is_refused():
    with pytest.raises(ValueError):
        build_claude_cli_command(allowed_tools=[], mcp_config="m.json")


def test_the_stream_reader_is_tolerant():
    assert parse_stream_json_line('{"type":"system","subtype":"init","new_field":1}') == {
        "type": "system", "subtype": "init", "new_field": 1}
    assert parse_stream_json_line(b'{"type":"result"}') == {"type": "result"}
    for noise in ("", "   ", "not json", "[1,2]", '"str"', "null"):
        assert parse_stream_json_line(noise) is None


async def test_start_task_is_the_m3_stub(tmp_path):
    be = GenericCliBackend()
    with pytest.raises(NotImplementedError, match="M3"):
        await be.start_task(AgentSpec(backend="cli"), "x", TaskContext(data_dir=tmp_path))
    caps = be.capabilities()
    assert caps.same_host_required and "bypassPermissions" not in caps.permission_modes
