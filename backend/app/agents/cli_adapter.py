"""Generic CLI agent adapter (M3, 4.3 #13).

Any agent CLI that streams newline-delimited JSON in the Claude ``stream-json`` shape can be a
backend. Three pieces:

* :func:`build_claude_cli_command` - the Claude preset command line as a PURE function, so the
  isolation flags are fixed and tested before anything runs it (RESEARCH.md section 5 item 13;
  "``claude --bare -p --output-format stream-json --verbose`` with explicit --allowedTools,
  --permission-mode and --mcp-config").
* :func:`parse_stream_json_line` - a tolerant reader for that stream (item 14); :func:`cli_to_wire`
  turns each line into the wire dict the Claude Agent adapter's normalizer already reads, so
  there is ONE normalizer (``ClaudeAgentBackend._on_*``), not two.
* :class:`GenericCliBackend` - supervises one subprocess per run.

WHAT MAY RUN (host code, so the protected Admin decides): only a SAVED PRESET
(``agents.cli.presets``, ``PUT /api/agents/cli-presets``, :func:`validate_presets`). An agent
names a preset by id in ``backend_options.preset`` and nothing else: a spec that carries its
own ``argv``/``command``/``executable``/``args`` is refused (``policy_violation``), and so is a
preset id that is not saved. A preset is ``kind: "claude"`` (the argv is
:func:`build_claude_cli_command` over the agent's allowlist, budget and MCP servers) or
``kind: "generic"`` (``[executable, *args]``, fixed). The executable is an absolute path that
must still exist and be executable when the run starts.

HOW IT RUNS: a fixed argv (never a shell), cwd = the task's workspace, a minimal allowlisted
environment (HOME/TMP inside the workspace; the preset's ``env_secrets`` resolved from the
secret store and registered with the redactor), the prompt over STDIN (never argv: a Windows
command line tops out near 32k characters and argv is visible to other local users), its own
process group/session. Interrupt = SIGINT to the group, then SIGTERM, then SIGKILL after a
grace (Windows: CTRL_BREAK, then ``taskkill /T /F`` by PID). Cancel does the same and the
release kills whatever is left of the group.

CAVEATS recorded from RESEARCH.md rather than hidden:

* ``--bare`` requires ``ANTHROPIC_API_KEY`` (or an apiKeyHelper / provider credentials) in the
  CLI's environment (a preset's ``env_secrets``) - unavoidable for a CLI, unlike the SDK worker
  where the key travels over a pipe (F6a).
* Whether an EMPTY ``--setting-sources ""`` is accepted was not tested (item 13). If it is
  rejected the run fails closed (the CLI refuses to start); it never falls back to loading
  the user's settings. Verify against the pinned CLI before relying on the Claude preset.
* A CLI has no approval channel: tools that would need an approval are left out of
  ``--allowedTools`` (so ``dontAsk`` denies them), unless named in ``unattended_allow``.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import signal
import subprocess
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from .branding import tool_matches
from .budget import BudgetVerdict, sdk_run_cap
from .claude_sdk_adapter import ClaudeAgentBackend, expand_allowlist
from .events import ErrorCode
from .interface import (
    AgentBackendError,
    AgentSpec,
    Capabilities,
    TaskRuntime,
    classify_tool,
)
from .secrets import SCRUBBED_ENV, get_agent_logger
from .workspace import Workspace

log = get_agent_logger("cli")

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




# --------------------------------------------------------------------------- #
# Presets (host code: saved by the protected Admin only)
# --------------------------------------------------------------------------- #

MAX_PRESETS = 16
MAX_ARGS = 64
PRESET_KINDS = ("claude", "generic")
_PRESET_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_SECRET_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
#: Variables a preset may never set: they would change what runs or where it looks.
_ENV_REFUSED = ("PATH", "HOME", "USERPROFILE", "PYTHONPATH", "PYTHONHOME", "NODE_OPTIONS",
                "CLAUDE_CONFIG_DIR", "TMPDIR", "TEMP", "TMP", "SHELL", "COMSPEC", "PATHEXT")
_SPEC_REFUSED_KEYS = ("argv", "command", "cmd", "executable", "args", "program", "shell")


def _executable_ok(path: str) -> bool:
    p = Path(path)
    return p.is_absolute() and p.is_file() and os.access(p, os.X_OK)


def validate_presets(raw: Any) -> list[dict[str, Any]]:
    """The cleaned preset list, or ``ValueError`` naming the first problem.

    ``{"id", "label", "kind": "claude"|"generic", "executable": "/abs/path",
    "args": [...] (generic only), "env_secrets": {"ENV_NAME": "secret-name"},
    "resume_args": [..., "{session_id}"] (generic only, optional)}``
    """
    if not isinstance(raw, list):
        raise ValueError("presets must be a list")
    if len(raw) > MAX_PRESETS:
        raise ValueError(f"at most {MAX_PRESETS} presets")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, p in enumerate(raw):
        where = f"presets[{i}]"
        if not isinstance(p, dict):
            raise ValueError(f"{where}: must be an object")
        extra = set(p) - {"id", "label", "kind", "executable", "args", "env_secrets", "resume_args"}
        if extra:
            raise ValueError(f"{where}: unknown field(s) {', '.join(sorted(extra))}")
        pid = str(p.get("id") or "")
        if not _PRESET_ID.match(pid):
            raise ValueError(f"{where}.id: lower-case letters, digits, '_' and '-' (max 40)")
        if pid in seen:
            raise ValueError(f"{where}.id: {pid!r} is used twice")
        seen.add(pid)
        kind = p.get("kind") or "generic"
        if kind not in PRESET_KINDS:
            raise ValueError(f"{where}.kind: one of {', '.join(PRESET_KINDS)}")
        exe = str(p.get("executable") or "")
        if "\x00" in exe or not _executable_ok(exe):
            raise ValueError(f"{where}.executable: an absolute path to an executable file on this computer")
        args = p.get("args") or []
        resume = p.get("resume_args") or []
        for name, lst in (("args", args), ("resume_args", resume)):
            if not isinstance(lst, list) or len(lst) > MAX_ARGS or any(
                    not isinstance(a, str) or len(a) > 1000 or "\x00" in a for a in lst):
                raise ValueError(f"{where}.{name}: a list of at most {MAX_ARGS} strings")
        if kind == "claude" and (args or resume):
            raise ValueError(f"{where}: a claude preset builds its own arguments (no args/resume_args)")
        if resume and "{session_id}" not in resume:
            raise ValueError(f"{where}.resume_args: must contain {{session_id}}")
        env = p.get("env_secrets") or {}
        if not isinstance(env, dict) or len(env) > 16:
            raise ValueError(f"{where}.env_secrets: an object of at most 16 entries")
        for k, v in env.items():
            if not _ENV_NAME.match(str(k)) or k in _ENV_REFUSED or str(k).startswith(("LD_", "DYLD_")):
                raise ValueError(f"{where}.env_secrets: {k!r} is not an allowed variable name")
            if not isinstance(v, str) or not _SECRET_NAME.match(v):
                raise ValueError(f"{where}.env_secrets.{k}: a secret name")
        label = str(p.get("label") or pid)[:80]
        out.append({"id": pid, "label": label, "kind": kind, "executable": exe,
                    "args": list(args), "env_secrets": dict(env), "resume_args": list(resume)})
    return out


def _saved_presets() -> list[dict[str, Any]]:
    from .. import agents_store
    return list(agents_store.get_setting("agents.cli.presets") or [])


def resolve_preset(spec: AgentSpec, presets: list[dict[str, Any]]) -> dict[str, Any]:
    """The saved preset this spec names. Refuses any free-form command in the spec."""
    opts = spec.backend_options or {}
    bad = [k for k in opts if k in _SPEC_REFUSED_KEYS]
    if bad:
        raise AgentBackendError(ErrorCode.policy_violation,
                                "a CLI agent runs a saved preset only; a command in the agent "
                                f"definition is refused ({', '.join(bad)})")
    pid = str(opts.get("preset") or "")
    for p in presets:
        if isinstance(p, dict) and p.get("id") == pid:
            try:
                clean = validate_presets([p])[0]
            except ValueError as e:
                raise AgentBackendError(ErrorCode.backend_unavailable,
                                        f"CLI preset {pid!r} is not usable: {e}") from None
            return clean
    raise AgentBackendError(ErrorCode.policy_violation,
                            f"CLI preset {pid!r} is not one the Owner saved")


# --------------------------------------------------------------------------- #
# stream-json -> the normalizer's wire dicts
# --------------------------------------------------------------------------- #


def cli_to_wire(obj: dict[str, Any], *, keep_session: bool = True) -> dict[str, Any] | None:
    """One stream-json object from the CLI -> the wire dict ``ClaudeAgentBackend`` reads.
    Unknown types are None (tolerant reader)."""
    t = obj.get("type")
    if t == "system":
        sub = obj.get("subtype")
        data = {k: v for k, v in obj.items() if k not in ("type", "subtype")}
        if sub == "init":
            failed = [s for s in (obj.get("mcp_servers") or [])
                      if isinstance(s, dict) and str(s.get("status")) in ("failed", "error")]
            data = {"session_id": obj.get("session_id") if keep_session else None,
                    "mcp_server_errors": failed, "plugin_errors": obj.get("plugin_errors") or []}
        return {"kind": "system", "subtype": sub, "data": data}
    if t == "stream_event":
        return {"kind": "stream_event", "event": obj.get("event") or {},
                "parent_tool_use_id": obj.get("parent_tool_use_id")}
    if t in ("assistant", "user"):
        msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
        out = {"kind": t, "content": msg.get("content"), "parent_tool_use_id": obj.get("parent_tool_use_id")}
        if t == "assistant" and obj.get("error"):
            out["error"] = obj["error"]
        return out
    if t == "result":
        out = {k: v for k, v in obj.items() if k != "type"}
        out["kind"] = "result"
        if not keep_session:
            out.pop("session_id", None)
        return out
    return None


# --------------------------------------------------------------------------- #
# The backend
# --------------------------------------------------------------------------- #


def cli_env(ws: Workspace, extra: dict[str, str], parent_env: dict[str, str] | None = None) -> dict[str, str]:
    """An allowlisted environment (never a scrubbed copy of ours)."""
    src = dict(os.environ if parent_env is None else parent_env)
    keep = ("PATH", "LANG", "LC_ALL", "TZ", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT")
    env = {k: src[k] for k in keep if k in src}
    tmp = ws.base / "tmp"
    env.update({"HOME": str(ws.home), "USERPROFILE": str(ws.home), "TMPDIR": str(tmp), "TEMP": str(tmp),
                "TMP": str(tmp), "CLAUDE_CONFIG_DIR": str(ws.config_dir), "PYTHONIOENCODING": "utf-8"})
    env.update(extra)
    for k in SCRUBBED_ENV:
        env.pop(k, None)
    return env


def _mcp_config(spec: AgentSpec) -> dict[str, Any]:
    servers: dict[str, Any] = {}
    for name, s in spec.mcp_servers.items():
        if s.header_secrets:
            raise AgentBackendError(ErrorCode.not_supported,
                                    f"MCP server {name!r} needs a secret header; a CLI agent cannot be "
                                    "given one (it would have to be written to a file)")
        if s.transport == "stdio":
            servers[name] = {"type": "stdio", "command": s.command, "args": list(s.args), "env": dict(s.env)}
        else:
            servers[name] = {"type": s.transport, "url": s.url, "headers": dict(s.headers)}
    return {"mcpServers": servers}


def cost_reporting_for(spec: AgentSpec, presets: list[dict[str, Any]] | None = None) -> str:
    """What a run of ``spec`` reports: a ``claude`` preset runs the Claude CLI, whose result line
    carries ``total_cost_usd`` and which honours ``--max-budget-usd``, so "usd"; a ``generic``
    preset (or one that cannot be resolved) claims nothing ("none"), so the Admin's money
    defaults and the per-day reservations never apply to it (the turn default still does)."""
    try:
        preset = resolve_preset(spec, _saved_presets() if presets is None else presets)
    except Exception:
        return "none"
    return "usd" if preset.get("kind") == "claude" else "none"


class GenericCliBackend(ClaudeAgentBackend):
    """One subprocess per run; the Claude normalizer reads its stream (see the module doc)."""

    name = "cli"
    INTERRUPT_GRACE_S = 3.0
    KILL_GRACE_S = 3.0
    LINE_LIMIT = 64 * 1024 * 1024

    def __init__(self, *, presets: Callable[[], list[dict[str, Any]]] | None = None,
                 parent_env: dict[str, str] | None = None, **kw: Any) -> None:
        kw.pop("transport_factory", None)
        super().__init__(**kw)
        self._presets = presets or _saved_presets
        self._parent_env = parent_env

    def capabilities(self) -> Capabilities:
        return Capabilities(
            name="cli", label="Command-line agent", streaming_text=True, approvals=False,
            interrupt=True, resume=True, steering=False, subagents=False, cost_reporting="usd",
            budget_enforcement="hard", session_listing=False, same_host_required=True,
            data_leaves_host=True, permission_modes=["unattended", "plan"])

    async def start_task(self, spec: AgentSpec, prompt: str, ctx: Any) -> str:
        # Refuse a free-form command or an unsaved preset BEFORE any task exists.
        self._check_spec(spec)
        resolve_preset(spec, self._presets())
        return await super().start_task(spec, prompt, ctx)

    def cost_reporting_for(self, spec: AgentSpec) -> str:
        return cost_reporting_for(spec, self._presets())

    # -- argv ----------------------------------------------------------------- #

    def _argv(self, rt: TaskRuntime, preset: dict[str, Any], ws: Workspace, resumed: bool) -> list[str]:
        spec = rt.spec
        resume_id = rt.session_id if (resumed and rt.session_id) else None
        if preset["kind"] == "generic":
            argv = [preset["executable"], *preset["args"]]
            if resume_id and preset["resume_args"]:
                argv += [a.replace("{session_id}", resume_id) for a in preset["resume_args"]]
            return argv
        pol = spec.approval_policy
        allowed = [t for t in expand_allowlist(spec.tool_allowlist)
                   if classify_tool(pol, t) == "auto" or tool_matches(t, pol.unattended_allow)]
        mcp_path = ws.base / "mcp.json"
        mcp_path.write_text(json.dumps(_mcp_config(spec)), encoding="utf-8")
        cap = sdk_run_cap(spec.budget, spent_task_usd=rt.tracker.usd,
                          spent_today_usd=rt.tracker.spent_today_usd)
        try:
            return build_claude_cli_command(
                executable=preset["executable"], prompt=None,
                permission_mode="plan" if pol.mode == "plan" else "dontAsk",
                allowed_tools=allowed, mcp_config=mcp_path, max_turns=spec.budget.max_turns,
                max_budget_usd=cap, unattended=pol.mode == "unattended",
                resume_session_id=resume_id, persist_session=True)
        except ValueError as e:
            raise AgentBackendError(ErrorCode.invalid_request, str(e)) from None

    def _env(self, rt: TaskRuntime, preset: dict[str, Any], ws: Workspace) -> dict[str, str]:
        extra: dict[str, str] = {}
        for var, secret in preset["env_secrets"].items():
            val = rt.ctx.secrets.get(secret) if rt.ctx.secrets is not None else None
            if not val:
                raise AgentBackendError(ErrorCode.auth_failed,
                                        f"CLI preset {preset['id']!r} needs secret {secret!r}, which is not set")
            self.redactor.register(val)
            extra[var] = val
        return cli_env(ws, extra, self._parent_env)

    # -- run ------------------------------------------------------------------- #

    async def _run(self, rt: TaskRuntime, message: str, resumed: bool) -> None:
        ws: Workspace = rt.extras["workspace"]
        preset = resolve_preset(rt.spec, self._presets())
        keep_session = preset["kind"] == "claude" or bool(preset["resume_args"])
        verdict = rt.tracker.check()
        if verdict is not None:
            self._finish_budget(rt, verdict)
            return
        cap = sdk_run_cap(rt.spec.budget, spent_task_usd=rt.tracker.usd,
                          spent_today_usd=rt.tracker.spent_today_usd)
        if cap is not None and cap <= 0:
            self._finish_budget(rt, BudgetVerdict("usd", rt.spec.budget.max_usd or 0.0, rt.tracker.usd))
            return
        argv = self._argv(rt, preset, ws, resumed)
        env = self._env(rt, preset, ws)
        (ws.base / "tmp").mkdir(exist_ok=True)
        kw: dict[str, Any] = {}
        if os.name == "nt":
            kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        else:
            kw["start_new_session"] = True
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, cwd=str(ws.root), env=env, limit=self.LINE_LIMIT, **kw)
        except OSError as e:
            raise AgentBackendError(ErrorCode.backend_unavailable,
                                    f"could not start the command-line agent: {e}") from e
        tail: deque[str] = deque(maxlen=40)
        rt.extras.update(proc=proc, stderr_tail=tail, subagents={}, streamed=False, text="",
                         assistant_error=None)
        drain = asyncio.create_task(self._drain(proc, tail))
        self._begin_running(rt, "resumed" if resumed else None)
        try:
            assert proc.stdin is not None and proc.stdout is not None
            with contextlib.suppress(BrokenPipeError, ConnectionResetError, OSError):
                proc.stdin.write(message.encode("utf-8"))
                await proc.stdin.drain()
            with contextlib.suppress(Exception):
                proc.stdin.close()
            while rt.log.active:
                try:
                    line = await proc.stdout.readline()
                except (ValueError, OSError):
                    line = b""
                if not line:
                    break
                obj = parse_stream_json_line(line)
                wire = cli_to_wire(obj, keep_session=keep_session) if obj else None
                if wire is not None:
                    self._on_wire(rt, wire)
            if rt.log.active:
                code = await self._wait(proc)
                if rt.interrupt_requested:
                    self._finish_interrupted(rt)
                    return
                raise AgentBackendError(
                    ErrorCode.backend_error,
                    f"the command-line agent exited without a result (exit code {code})",
                    detail=self.redactor.redact("\n".join(tail))[-500:] or None)
        except asyncio.CancelledError:
            if not rt.interrupt_requested:
                raise
            if rt.log.active:
                self._finish_interrupted(rt)
        finally:
            await self._stop_group(proc)
            drain.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await drain
            rt.extras.pop("proc", None)

    def _on_wire(self, rt: TaskRuntime, m: dict[str, Any]) -> None:
        kind = m.get("kind")
        if kind == "system":
            self._on_system(rt, m)
        elif kind == "stream_event":
            self._on_stream_event(rt, m)
        elif kind == "assistant":
            self._on_assistant(rt, m)
        elif kind == "user":
            self._on_user(rt, m)
        elif kind == "result":
            self._on_result(rt, m)

    @staticmethod
    async def _drain(proc: asyncio.subprocess.Process, tail: deque[str]) -> None:
        if proc.stderr is None:
            return
        with contextlib.suppress(Exception):
            while True:
                line = await proc.stderr.readline()
                if not line:
                    return
                tail.append(line.decode("utf-8", "replace").rstrip()[:500])

    async def _wait(self, proc: asyncio.subprocess.Process) -> int | None:
        with contextlib.suppress(TimeoutError):
            return await asyncio.wait_for(proc.wait(), timeout=self.KILL_GRACE_S)
        return proc.returncode

    # -- control --------------------------------------------------------------- #

    @staticmethod
    def _signal_group(proc: asyncio.subprocess.Process, sig: int) -> None:
        if os.name == "nt":                                      # pragma: no cover - Windows
            with contextlib.suppress(Exception):
                proc.send_signal(signal.CTRL_BREAK_EVENT)
            return
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(proc.pid, sig)            # start_new_session: the group id is the pid

    async def _stop_group(self, proc: asyncio.subprocess.Process) -> None:
        """End the whole process group: whatever the CLI started dies with it. The group is
        signalled even when the leader has already exited (a grandchild may linger)."""
        if os.name == "nt":                                      # pragma: no cover - Windows
            if proc.returncode is None:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(subprocess.run, ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                                            capture_output=True, timeout=15)
            return
        if proc.returncode is None:
            self._signal_group(proc, signal.SIGTERM)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=self.KILL_GRACE_S)
        self._signal_group(proc, signal.SIGKILL)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), timeout=5)

    async def _do_interrupt(self, rt: TaskRuntime) -> None:
        proc = rt.extras.get("proc")
        if proc is None or proc.returncode is not None:
            return
        self._signal_group(proc, signal.SIGINT)
        self._spawn(self._escalate(rt, proc))

    async def _escalate(self, rt: TaskRuntime, proc: asyncio.subprocess.Process) -> None:
        await asyncio.sleep(self.INTERRUPT_GRACE_S)
        if proc.returncode is None:
            log.warning("command-line agent for task %s ignored the interrupt; stopping its group", rt.task_id)
            await self._stop_group(proc)

    async def _do_steer(self, rt: TaskRuntime, message: str) -> None:
        raise AgentBackendError(ErrorCode.not_supported, "a command-line agent cannot be steered mid-run")

    async def _release(self, rt: TaskRuntime) -> None:
        proc = rt.extras.pop("proc", None)
        if proc is not None:
            await self._stop_group(proc)

    async def list_sessions(self, ctx: Any = None) -> list:
        return await super(ClaudeAgentBackend, self).list_sessions(ctx)

    def _support_task(self, rt: TaskRuntime) -> dict[str, Any]:
        out = super(ClaudeAgentBackend, self)._support_task(rt)
        proc = rt.extras.get("proc")
        out["process_alive"] = bool(proc is not None and proc.returncode is None)
        out["preset"] = (rt.spec.backend_options or {}).get("preset")
        return out

    def support_dump(self) -> dict[str, Any]:
        return super(ClaudeAgentBackend, self).support_dump()
