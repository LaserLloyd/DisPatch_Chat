"""Generic CLI agent adapter (M3 STUB).

Any agent CLI that streams newline-delimited JSON can become a backend. M3 builds
the supervisor; this milestone ships the two pieces that must not wait:

* :func:`build_claude_cli_command` - the Claude preset command line as a PURE
  function, so the isolation flags are fixed and tested before anything runs it
  (RESEARCH.md section 5 item 13; brief: "The Claude preset always runs
  ``claude --bare -p --output-format stream-json --verbose`` with explicit
  --allowedTools, --permission-mode and --mcp-config").
* :func:`parse_stream_json_line` - a tolerant reader for that stream (item 14).
  Its dicts are the same message shapes the SDK adapter normalizes, so M3 reuses
  the normalizer rather than growing a second one.

``start_task`` raises ``NotImplementedError("M3")``.

CAVEATS recorded from RESEARCH.md rather than hidden:

* ``--bare`` requires ``ANTHROPIC_API_KEY`` (or an apiKeyHelper / provider
  credentials) in the CLI's environment - unavoidable for a CLI, unlike the
  SDK worker where the key travels over a pipe (F6a).
* Whether an EMPTY ``--setting-sources ""`` is accepted was not tested (item 13).
  The command carries both ``--bare`` and ``--setting-sources ""``: if the empty
  value is rejected the run fails closed (the CLI refuses to start), it never
  falls back to loading the user's settings. Verify against the pinned CLI
  before M3 ships.
* The prompt goes over STDIN by default (``prompt=None``): a Windows command
  line tops out near 32k characters and argv is visible to other local users.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Literal

from .interface import AgentSpec, BaseBackend, Capabilities, TaskContext

CLAUDE_CLI_PERMISSION_MODES = ("default", "dontAsk", "plan", "acceptEdits")
_REFUSED_MODES = ("bypassPermissions", "auto")


def build_claude_cli_command(
    *,
    executable: str = "claude",
    prompt: str | None = None,
    permission_mode: Literal["default", "dontAsk", "plan", "acceptEdits"] = "dontAsk",
    allowed_tools: list[str],
    mcp_config: str | Path,
    max_turns: int | None = None,
    max_budget_usd: float | None = None,
    unattended: bool = False,
    json_schema: dict[str, Any] | str | None = None,
    resume_session_id: str | None = None,
    persist_session: bool = False,
    forward_subagent_text: bool = False,
) -> list[str]:
    """The isolated headless command (RESEARCH.md section 5 item 13)."""
    if permission_mode in _REFUSED_MODES or permission_mode not in CLAUDE_CLI_PERMISSION_MODES:
        raise ValueError(f"permission mode {permission_mode!r} is not available to agents")
    if not allowed_tools:
        raise ValueError("an explicit --allowedTools list is required (empty means 'ask everything')")
    cmd = [executable, "--bare", "-p"]
    if prompt is not None:
        cmd.append(prompt)
    cmd += ["--output-format", "stream-json", "--verbose", "--include-partial-messages",
            "--permission-mode", permission_mode]
    if unattended:
        cmd += ["--permission-prompts", "none"]               # >= 2.1.259 (item 16)
    cmd += ["--allowedTools", ",".join(allowed_tools),
            "--mcp-config", str(mcp_config), "--strict-mcp-config",
            "--setting-sources", ""]
    if max_turns is not None:
        cmd += ["--max-turns", str(max_turns)]
    if max_budget_usd is not None:
        cmd += ["--max-budget-usd", f"{max_budget_usd:g}"]
    if json_schema is not None:
        cmd += ["--json-schema", json_schema if isinstance(json_schema, str) else json.dumps(json_schema)]
    if forward_subagent_text:
        cmd.append("--forward-subagent-text")                 # >= 2.1.211
    if resume_session_id:
        cmd += ["--resume", resume_session_id]
    elif not persist_session:
        cmd.append("--no-session-persistence")
    return cmd


def parse_stream_json_line(line: str | bytes) -> dict[str, Any] | None:
    """One line of the CLI's stream-json output -> dict, or None for noise.

    Tolerant on purpose (item 14): blank lines, non-JSON and non-object lines are
    skipped; unknown ``type``/``subtype`` values pass through for the caller to
    ignore.
    """
    if isinstance(line, bytes):
        line = line.decode("utf-8", "replace")
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


class GenericCliBackend(BaseBackend):
    name = "cli"

    def capabilities(self) -> Capabilities:
        return Capabilities(
            name="cli", label="Command-line agent", streaming_text=True, approvals=False,
            interrupt=True, resume=True, steering=False, subagents=False, cost_reporting="usd",
            budget_enforcement="hard", session_listing=False, same_host_required=True,
            data_leaves_host=True, permission_modes=["unattended", "plan"])

    async def start_task(self, spec: AgentSpec, prompt: str, ctx: TaskContext) -> str:
        raise NotImplementedError("M3")

    async def _run(self, rt, message: str, resumed: bool) -> None:           # pragma: no cover
        raise NotImplementedError("M3")

    async def _do_interrupt(self, rt) -> None:                                # pragma: no cover
        raise NotImplementedError("M3")

    def events(self, task_id: str, *, after_seq: int = -1) -> AsyncIterator[Any]:
        raise NotImplementedError("M3")
