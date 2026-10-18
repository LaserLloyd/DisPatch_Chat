"""Claude Agent (local, Python SDK) wrapped onto the AgentBackend interface.

One supervised WORKER PROCESS per task runs ``ClaudeSDKClient`` in streaming
mode (RESEARCH.md section 5 items 6-12, 15). The parent (this module's
``ClaudeAgentBackend``) never imports the SDK: it talks to the worker through
the small :class:`SdkTransport` protocol, which the tests fake. The SDK itself
is an optional extra (``agents-claude = ["claude-agent-sdk==0.2.168"]``) imported
lazily, only inside the worker.

                parent (DisPatch backend)                  worker (python -P -m ...)
    start_task -> build_launch_plan() -> SubprocessTransport  -> ClaudeSDKClient
    events    <- normalize wire dicts <- JSON lines on stdout <- SDK messages
    approval  <- permission_request  <-                       <- can_use_tool callback
    respond   -> answer_permission    -> JSON line on stdin    -> Allow/Deny result
    interrupt -> "interrupt" command  ->                       -> client.interrupt()

THE ISOLATION DEFAULTS (every one is asserted in tests/agents):

* ``setting_sources=[]`` ALWAYS passed explicitly; ``["project"]`` only when the
  agent's workspace policy sets ``trust_repo_settings`` (then the repo's hooks
  EXECUTE - docs/agents/security.md). RESEARCH.md F4/C1, section 5 item 6.
* ``--strict-mcp-config`` plus an explicit ``mcp_servers`` dict: a repo's
  ``.mcp.json`` never loads. F6b, R-MCP.
* An explicit ``permission_mode``: default (approval cards), dontAsk
  (unattended), plan (investigate only). ``bypassPermissions`` and ``auto`` are
  not representable. R-PM, section 5 item 7.
* Explicit ``allowed_tools`` + bare-name ``disallowed_tools`` for every built-in
  tool the agent was not given. In interactive mode a tool that needs approval
  is deliberately NOT in ``allowed_tools``: allow rules sit BEFORE ``canUseTool``
  in the permission order, so listing it would skip the approval card (F5).
* ``max_budget_usd`` (with overshoot headroom) and ``max_turns`` on every run. R-COST.
* A private ``CLAUDE_CONFIG_DIR``/HOME per task, auto-memory off, subagent
  depth/concurrency caps, ``CLAUDE_AGENT_SDK_CLIENT_APP="DisPatch"``, and the
  OAuth token blanked and never forwarded. R-ISO, section 5 items 2, 5, 9, 10.
* The API key travels to the worker over its stdin pipe, never argv or env.
* Interrupts use ``client.interrupt()``; the worker is closed by closing its
  stdin, never SIGTERM mid-turn. F6d, section 5 items 11, 15.
"""
from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import os
import signal
import subprocess
import sys
import uuid
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from .branding import tool_matches
from .budget import BudgetVerdict, sdk_run_cap
from .events import (
    ArtifactEvent,
    ErrorCode,
    RetryEvent,
    SubagentFinishedEvent,
    SubagentStartedEvent,
    TextDeltaEvent,
    ToolCallEvent,
    ToolResultEvent,
    clip,
)
from .interface import (
    AgentBackendError,
    AgentSpec,
    BaseBackend,
    Capabilities,
    SessionInfo,
    TaskContext,
    TaskRuntime,
    classify_tool,
    tool_in_allowlist,
)
from .secrets import (
    SCRUBBED_ENV,
    SECRET_ANTHROPIC_API_KEY,
    get_agent_logger,
)
from .workspace import Workspace, WorkspaceError, existing_workspace, prepare_workspace, resolve_inside

log = get_agent_logger("claude_sdk")

SDK_PIN = "0.2.168"                       # RESEARCH.md section 1 item 6; pinned exactly
CLIENT_APP = "DisPatch"                   # CLAUDE_AGENT_SDK_CLIENT_APP (section 5 item 5)
SUBAGENT_TOOL_NAMES = ("Agent", "Task")   # the tool was renamed; match both (item 17)

# Built-in tools the SDK may offer. Anything here that the agent's allowlist does
# not cover is disallowed BY NAME (bare name, RESEARCH.md section 5 item 7).
KNOWN_BUILTIN_TOOLS: tuple[str, ...] = (
    "Bash", "BashOutput", "KillShell", "Edit", "MultiEdit", "Write", "NotebookEdit", "Read",
    "Glob", "Grep", "LS", "WebFetch", "WebSearch", "Task", "Agent", "TodoWrite", "ExitPlanMode",
    "Skill", "SlashCommand", "AskUserQuestion",
)

# The only permission modes this adapter can produce. bypassPermissions and auto
# are intentionally absent (brief; RESEARCH.md R-PM, section 5 item 7).
PERMISSION_MODE_FOR = {"interactive": "default", "unattended": "dontAsk", "plan": "plan"}
FORBIDDEN_PERMISSION_MODES = frozenset({"bypassPermissions", "auto"})

# Provider routes (RESEARCH.md F2/F2b). Only the API-key route is verified end
# to end by this module's tests; the others set the documented switch and rely
# on the provider's own credential chain being present in ``ctx.extra_env``.
AUTH_ROUTE_ENV = {
    "api_key": {},
    "bedrock": {"CLAUDE_CODE_USE_BEDROCK": "1"},
    "vertex": {"CLAUDE_CODE_USE_VERTEX": "1"},
    "foundry": {"CLAUDE_CODE_USE_FOUNDRY": "1"},
    "anthropic_aws": {"CLAUDE_CODE_USE_ANTHROPIC_AWS": "1"},
}

# ResultMessage.subtype values (docs: cost-tracking / headless), tolerant reader.
_SUB_MAX_BUDGET = "error_max_budget_usd"
_SUB_MAX_TURNS = "error_max_turns"

# AssistantMessage.error -> catalogue (documented set; unknown -> backend_error).
ASSISTANT_ERROR_CODES: dict[str, ErrorCode] = {
    "authentication_failed": ErrorCode.auth_failed,
    "billing_error": ErrorCode.refused,
    "rate_limit": ErrorCode.rate_limited,
    "invalid_request": ErrorCode.refused,
    "server_error": ErrorCode.backend_unavailable,
    "unknown": ErrorCode.backend_error,
}
WORKER_ERROR_CODES: dict[str, ErrorCode] = {
    "sdk_missing": ErrorCode.sdk_missing,
    "cli_not_found": ErrorCode.backend_unavailable,
    "connection": ErrorCode.backend_unavailable,
    "process_error": ErrorCode.backend_error,
    "protocol": ErrorCode.backend_error,
    "options": ErrorCode.backend_error,
}


# --------------------------------------------------------------------------- #
# Launch plan (pure; this is what the isolation tests inspect)
# --------------------------------------------------------------------------- #


@dataclass
class ClaudeLaunchPlan:
    """Everything the worker needs. ``options`` is JSON-safe and holds NO secret."""

    options: dict[str, Any]
    secret_env: dict[str, str] = field(default_factory=dict)          # -> options.env in the worker
    secret_mcp_headers: dict[str, dict[str, str]] = field(default_factory=dict)
    worker: dict[str, Any] = field(default_factory=dict)              # callables the worker builds

    def to_wire(self) -> dict[str, Any]:
        return {"op": "start", "options": self.options, "secret_env": self.secret_env,
                "secret_mcp_headers": self.secret_mcp_headers, "worker": self.worker}

    def describe(self) -> dict[str, Any]:
        """A loggable view: env VALUES and secret names only (support bundles)."""
        opts = dict(self.options)
        opts["env"] = {k: ("<set>" if v else "<blank>") for k, v in self.options.get("env", {}).items()}
        if "system_prompt" in opts:           # prompts stay out of support bundles by default
            opts["system_prompt"] = f"<omitted: {len(str(opts['system_prompt']))} chars>"
        return {"options": opts, "secret_env": sorted(self.secret_env),
                "secret_mcp_headers": {k: sorted(v) for k, v in self.secret_mcp_headers.items()},
                "worker": self.worker}


def _mcp_options(spec: AgentSpec) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for name, s in spec.mcp_servers.items():
        if s.transport == "stdio":
            out[name] = {"type": "stdio", "command": s.command, "args": list(s.args), "env": dict(s.env)}
        else:
            # "http" (the SDK's streamable-HTTP type), never "sse" unless asked: item 21.
            out[name] = {"type": s.transport, "url": s.url, "headers": dict(s.headers)}
    return out


def expand_allowlist(allowlist: list[str]) -> list[str]:
    """Add the old subagent tool name beside the new one (RESEARCH.md item 17)."""
    out = list(allowlist)
    if "Agent" in out and "Task" not in out:
        out.append("Task")
    if "Task" in out and "Agent" not in out:
        out.append("Agent")
    return out


def build_launch_plan(spec: AgentSpec, ctx: TaskContext, ws: Workspace, *, api_key: str | None,
                      auth_route: str = "api_key", resume_session_id: str | None = None,
                      spent_task_usd: float = 0.0, spent_today_usd: float = 0.0,
                      header_secrets: dict[str, dict[str, str]] | None = None) -> ClaudeLaunchPlan:
    """Turn an AgentSpec into the SDK options the worker will use."""
    pol = spec.approval_policy
    mode = PERMISSION_MODE_FOR[pol.mode]
    assert mode not in FORBIDDEN_PERMISSION_MODES

    allow = expand_allowlist(spec.tool_allowlist)
    if pol.mode == "interactive":
        # Approval-needing tools are NOT pre-allowed: they must reach can_use_tool.
        allowed = [t for t in allow if classify_tool(pol, t) == "auto"]
    elif pol.mode == "unattended":
        allowed = list(allow)             # AgentSpec validation guarantees nothing here needs a human
    else:  # plan: read-only only; the callback denies the rest
        from .branding import HIGH_PRIVILEGE_TOOL_PATTERNS
        allowed = [t for t in allow if classify_tool(pol, t) == "auto"
                   and not tool_matches(t, HIGH_PRIVILEGE_TOOL_PATTERNS)]
    keep_visible = {"ExitPlanMode"} if pol.mode == "plan" else set()
    disallowed = [t for t in KNOWN_BUILTIN_TOOLS
                  if t not in keep_visible and not tool_matches(t, allow)]

    env: dict[str, str] = {
        "CLAUDE_CONFIG_DIR": str(ws.config_dir),
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        "CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH": str(spec.max_subagent_depth),
        "CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS": str(spec.max_concurrent_subagents),
        "CLAUDE_AGENT_SDK_CLIENT_APP": CLIENT_APP,
        "HOME": str(ws.home),
        "USERPROFILE": str(ws.home),
        **AUTH_ROUTE_ENV[auth_route],
    }
    for name in SCRUBBED_ENV:             # blank beats absent: the SDK merges with os.environ
        env[name] = ""
    for k, v in ctx.extra_env.items():
        if k in SCRUBBED_ENV or k == SECRET_ANTHROPIC_API_KEY:
            raise AgentBackendError(ErrorCode.policy_violation,
                                    f"{k} must not be passed through extra_env")
        env[k] = v

    options: dict[str, Any] = {
        "cwd": str(ws.root),
        "setting_sources": ["project"] if spec.workspace_policy.trust_repo_settings else [],
        "permission_mode": mode,
        "allowed_tools": allowed,
        "disallowed_tools": disallowed,
        "mcp_servers": _mcp_options(spec),
        "extra_args": {"strict-mcp-config": None},
        "env": env,
        "include_partial_messages": True,
    }
    if spec.budget.max_turns is not None:
        options["max_turns"] = spec.budget.max_turns
    cap = sdk_run_cap(spec.budget, spent_task_usd=spent_task_usd, spent_today_usd=spent_today_usd)
    if cap is not None:
        options["max_budget_usd"] = cap
    if spec.model:
        options["model"] = spec.model
    if spec.instructions:
        options["system_prompt"] = spec.instructions
    if resume_session_id:
        options["resume"] = resume_session_id

    secret_env: dict[str, str] = {}
    if auth_route == "api_key":
        if not api_key:
            raise AgentBackendError(
                ErrorCode.auth_failed,
                "no Anthropic API key is configured (ANTHROPIC_API_KEY, or the secret store). "
                "claude.ai login and subscription tokens are not supported (RESEARCH.md F2).")
        secret_env[SECRET_ANTHROPIC_API_KEY] = api_key
    use_callback = pol.mode in ("interactive", "plan")
    worker = {"can_use_tool": use_callback, "keepalive_pretooluse_hook": use_callback}
    return ClaudeLaunchPlan(options, secret_env, dict(header_secrets or {}), worker)


def make_sdk_options(options: dict[str, Any], sdk: Any, *, can_use_tool: Any = None,
                     hooks: Any = None) -> Any:
    """Build the SDK's options object from the plan, failing LOUDLY on drift.

    A security-relevant key the installed SDK does not know is an error, never a
    silent drop: running without ``setting_sources`` would re-enable the user's
    own hooks.
    """
    cls = sdk.ClaudeAgentOptions
    known = {f.name for f in dataclasses.fields(cls)}
    kwargs = dict(options)
    if can_use_tool is not None:
        kwargs["can_use_tool"] = can_use_tool
    if hooks is not None:
        kwargs["hooks"] = hooks
    critical = {"setting_sources", "permission_mode", "allowed_tools", "disallowed_tools", "env", "cwd",
                "extra_args", "mcp_servers", "max_budget_usd", "max_turns"}
    missing = sorted(k for k in kwargs if k in critical and k not in known)
    if missing:
        raise AgentBackendError(
            ErrorCode.backend_error,
            f"the installed claude-agent-sdk has no option(s) {missing}; this build pins {SDK_PIN}")
    return cls(**{k: v for k, v in kwargs.items() if k in known})


def worker_env(ws: Workspace, parent_env: dict[str, str] | None = None) -> dict[str, str]:
    """A minimal environment for the worker: an allowlist, not a scrubbed copy."""
    src = dict(os.environ if parent_env is None else parent_env)
    keep_win = ("SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "PROCESSOR_ARCHITECTURE")
    keep_posix = ("LANG", "LC_ALL", "TZ")
    env = {k: src[k] for k in ("PATH", *keep_win, *keep_posix) if k in src}
    if os.name == "nt":
        for k in ("PATH", *keep_win):     # Windows env names are case-insensitive
            for real in src:
                if real.upper() == k and real not in env:
                    env[real] = src[real]
    env.update({
        "HOME": str(ws.home), "USERPROFILE": str(ws.home),
        "APPDATA": str(ws.home / "AppData" / "Roaming"),
        "LOCALAPPDATA": str(ws.home / "AppData" / "Local"),
        "TEMP": str(ws.base / "tmp"), "TMP": str(ws.base / "tmp"), "TMPDIR": str(ws.base / "tmp"),
        "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    for k in SCRUBBED_ENV:
        env.pop(k, None)
    return env


# --------------------------------------------------------------------------- #
# Permission policy (parent side; pure)
# --------------------------------------------------------------------------- #


def decide_permission(spec: AgentSpec, tool_name: str) -> tuple[Literal["allow", "deny", "ask"], str]:
    """What to do when the SDK asks about ``tool_name`` (the can_use_tool path)."""
    pol = spec.approval_policy
    if pol.mode == "plan":
        if tool_name == "ExitPlanMode":
            return "allow", "proposing the plan"
        if tool_in_allowlist(spec, tool_name) and classify_tool(pol, tool_name) == "auto":
            from .branding import HIGH_PRIVILEGE_TOOL_PATTERNS
            if not tool_matches(tool_name, HIGH_PRIVILEGE_TOOL_PATTERNS):
                return "allow", "read-only tool in plan mode"
        return "deny", "this agent is investigate-only (plan mode)"
    allow_names = expand_allowlist(spec.tool_allowlist)
    if not tool_matches(tool_name, allow_names):
        return "deny", f"{tool_name} is not in this agent's tool allowlist"
    if pol.mode == "unattended":
        if classify_tool(pol, tool_name) == "auto" or tool_matches(tool_name, pol.unattended_allow):
            return "allow", "unattended allow"
        return "deny", "unattended agents cannot ask for approval"
    if classify_tool(pol, tool_name) == "auto":
        return "allow", "allowlisted"
    return "ask", "needs approval"


# --------------------------------------------------------------------------- #
# Transport
# --------------------------------------------------------------------------- #


class SdkTransport(Protocol):
    """The parent's view of one SDK session. Messages are plain dicts ("wire")."""

    @property
    def alive(self) -> bool: ...

    async def start(self, plan: ClaudeLaunchPlan) -> None: ...

    async def query(self, prompt: str) -> None: ...

    def messages(self) -> AsyncIterator[dict[str, Any]]: ...

    async def interrupt(self) -> None: ...

    async def answer_permission(self, request_id: str, allow: bool,
                                message: str | None = None) -> None: ...

    async def close(self) -> None: ...


class SubprocessTransport:
    """Runs the worker (``python -P -m app.agents.claude_sdk_adapter``) and speaks
    newline-delimited JSON with it."""

    START_TIMEOUT_S = 60.0
    CLOSE_GRACE_S = 10.0
    LINE_LIMIT = 64 * 1024 * 1024

    def __init__(self, ws: Workspace, *, python: str | None = None,
                 module: str = "app.agents.claude_sdk_adapter",
                 argv: list[str] | None = None, parent_env: dict[str, str] | None = None) -> None:
        self.ws = ws
        self._argv = argv or [python or sys.executable, "-P", "-X", "utf8", "-m", module]
        self._parent_env = parent_env
        self._proc: asyncio.subprocess.Process | None = None
        self._stderr_tail: deque[str] = deque(maxlen=40)
        self._stderr_task: asyncio.Task | None = None
        self._write_lock = asyncio.Lock()

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc else None

    def stderr_tail(self) -> str:
        return "\n".join(self._stderr_tail)

    async def start(self, plan: ClaudeLaunchPlan) -> None:
        (self.ws.base / "tmp").mkdir(exist_ok=True)
        kw: dict[str, Any] = {}
        if os.name == "nt":
            # Its own process group: a CTRL_BREAK can reach the worker alone, and the
            # parent's console signals never reach it (RESEARCH.md section 5 item 15).
            kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        else:
            kw["start_new_session"] = True
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *self._argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, env=worker_env(self.ws, self._parent_env),
                cwd=str(self.ws.base),          # NOT the workspace: it is untrusted
                limit=self.LINE_LIMIT, **kw)
        except OSError as e:
            raise AgentBackendError(ErrorCode.backend_unavailable,
                                    f"could not start the agent worker: {e}") from e
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        await self._send(plan.to_wire())
        try:
            first = await asyncio.wait_for(self._read(), timeout=self.START_TIMEOUT_S)
        except TimeoutError:
            await self.close()
            raise AgentBackendError(ErrorCode.backend_unavailable,
                                    "the agent worker did not become ready") from None
        if first is None:
            await self.close()
            raise AgentBackendError(ErrorCode.backend_unavailable,
                                    "the agent worker exited during start-up",
                                    detail=self.stderr_tail()[-500:])
        if first.get("kind") == "worker_error":
            await self.close()
            raise _worker_error(first)
        if first.get("kind") != "ready":
            await self.close()
            raise AgentBackendError(ErrorCode.backend_error,
                                    f"unexpected first worker message: {str(first)[:120]}")

    async def _drain_stderr(self) -> None:
        assert self._proc and self._proc.stderr
        with contextlib.suppress(Exception):
            while True:
                line = await self._proc.stderr.readline()
                if not line:
                    return
                self._stderr_tail.append(line.decode("utf-8", "replace").rstrip()[:500])

    async def _send(self, obj: dict[str, Any]) -> None:
        if not self.alive or self._proc is None or self._proc.stdin is None:
            raise AgentBackendError(ErrorCode.backend_unavailable, "the agent worker is not running")
        data = (json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8")
        async with self._write_lock:
            try:
                self._proc.stdin.write(data)
                await self._proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError, OSError) as e:
                raise AgentBackendError(ErrorCode.backend_unavailable,
                                        "the agent worker went away") from e

    async def _read(self) -> dict[str, Any] | None:
        assert self._proc and self._proc.stdout
        while True:
            try:
                line = await self._proc.stdout.readline()
            except (ValueError, OSError):
                return None
            if not line:
                return None
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue                       # tolerant reader: ignore noise (item 14)
            if isinstance(obj, dict):
                return obj

    async def messages(self) -> AsyncIterator[dict[str, Any]]:
        while True:
            obj = await self._read()
            if obj is None:
                return
            yield obj

    async def query(self, prompt: str) -> None:
        await self._send({"op": "query", "prompt": prompt})

    async def interrupt(self) -> None:
        await self._send({"op": "interrupt"})

    async def answer_permission(self, request_id: str, allow: bool, message: str | None = None) -> None:
        await self._send({"op": "permission", "request_id": request_id, "allow": allow,
                          "message": message})

    async def close(self) -> None:
        """Graceful first: close command + stdin EOF; only then escalate."""
        proc = self._proc
        if proc is None:
            return
        if proc.returncode is None:
            with contextlib.suppress(Exception):
                await self._send({"op": "close"})
            with contextlib.suppress(Exception):
                if proc.stdin is not None:
                    proc.stdin.close()
            try:
                await asyncio.wait_for(proc.wait(), timeout=self.CLOSE_GRACE_S)
            except TimeoutError:
                await self._escalate(proc)
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._stderr_task

    async def _escalate(self, proc: asyncio.subprocess.Process) -> None:
        log.warning("agent worker %s ignored the close request; escalating", proc.pid)
        if os.name == "nt":
            with contextlib.suppress(Exception):
                proc.send_signal(signal.CTRL_BREAK_EVENT)    # reaches only the new group
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=3)
            if proc.returncode is None:
                # By PID and as a tree (the SDK's CLI child belongs to the worker).
                # Never by image name: that once killed unrelated live services.
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(subprocess.run, ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                                            capture_output=True, timeout=15)
        else:
            with contextlib.suppress(Exception):
                os.killpg(proc.pid, signal.SIGINT)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=3)
            if proc.returncode is None:
                with contextlib.suppress(Exception):
                    os.killpg(proc.pid, signal.SIGKILL)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), timeout=5)


def _worker_error(m: dict[str, Any]) -> AgentBackendError:
    code = WORKER_ERROR_CODES.get(str(m.get("code")), ErrorCode.backend_error)
    return AgentBackendError(code, str(m.get("message") or "the agent worker reported an error")[:500],
                             detail=str(m.get("detail") or "")[:500] or None)


# --------------------------------------------------------------------------- #
# The backend
# --------------------------------------------------------------------------- #

TransportFactory = Callable[[TaskRuntime, Workspace], SdkTransport]


class ClaudeAgentBackend(BaseBackend):
    name = "claude_sdk"
    ABORT_GRACE_S = 15.0

    def __init__(self, *, transport_factory: TransportFactory | None = None,
                 auth_route: str = "api_key", api_key: str | None = None, **kw: Any) -> None:
        super().__init__(**kw)
        if auth_route not in AUTH_ROUTE_ENV:
            raise ValueError(f"auth_route must be one of {sorted(AUTH_ROUTE_ENV)}")
        self._factory: TransportFactory = transport_factory or (
            lambda rt, ws: SubprocessTransport(ws))
        self._auth_route = auth_route
        self._api_key = api_key

    def capabilities(self) -> Capabilities:
        return Capabilities(
            name="claude_sdk", label="Claude Agent", streaming_text=True, approvals=True,
            interrupt=True, resume=True, steering=True, subagents=True, cost_reporting="usd",
            budget_enforcement="hard", session_listing=True, same_host_required=False,
            data_leaves_host=True, permission_modes=["interactive", "unattended", "plan"])

    # -- credentials & workspace --------------------------------------------- #

    def _resolve_key(self, rt: TaskRuntime) -> str | None:
        key = self._api_key
        if not key and rt.ctx.secrets is not None:
            key = rt.ctx.secrets.get(SECRET_ANTHROPIC_API_KEY)
        self.redactor.register(key)
        return key

    def _header_secrets(self, rt: TaskRuntime) -> dict[str, dict[str, str]]:
        out: dict[str, dict[str, str]] = {}
        for name, srv in rt.spec.mcp_servers.items():
            for header, secret_name in srv.header_secrets.items():
                val = rt.ctx.secrets.get(secret_name) if rt.ctx.secrets is not None else None
                if not val:
                    raise AgentBackendError(
                        ErrorCode.auth_failed,
                        f"MCP server {name!r} needs secret {secret_name!r}, which is not configured")
                self.redactor.register(val)
                out.setdefault(name, {})[header] = val
        return out

    async def _prepare(self, rt: TaskRuntime) -> None:
        if "workspace" in rt.extras:
            return
        pol = rt.spec.workspace_policy
        try:
            try:
                ws = await asyncio.to_thread(existing_workspace, rt.ctx.data_dir, rt.task_id)
            except WorkspaceError:
                ws = await asyncio.to_thread(
                    prepare_workspace, rt.ctx.data_dir, rt.task_id, mode=pol.mode,
                    repo_path=rt.ctx.repo_path, ref=pol.ref)
        except WorkspaceError as e:
            raise AgentBackendError(ErrorCode.workspace_violation, str(e)) from e
        rt.extras["workspace"] = ws
        if pol.trust_repo_settings:
            log.warning("task %s trusts its repo's settings: the repo's hooks WILL execute", rt.task_id)

    # -- run ------------------------------------------------------------------ #

    async def _run(self, rt: TaskRuntime, message: str, resumed: bool) -> None:
        ws: Workspace = rt.extras["workspace"]
        verdict = rt.tracker.check()
        if verdict is not None:
            self._finish_budget(rt, verdict)
            return
        key = self._resolve_key(rt)
        header_secrets = self._header_secrets(rt)

        transport: SdkTransport | None = rt.extras.get("transport")
        if transport is None or not transport.alive:
            cap = sdk_run_cap(rt.spec.budget, spent_task_usd=rt.tracker.usd,
                              spent_today_usd=rt.tracker.spent_today_usd)
            if cap is not None and cap <= 0:
                self._finish_budget(rt, BudgetVerdict("usd", rt.spec.budget.max_usd or 0.0, rt.tracker.usd))
                return
            plan = build_launch_plan(
                rt.spec, rt.ctx, ws, api_key=key, auth_route=self._auth_route,
                resume_session_id=rt.session_id, spent_task_usd=rt.tracker.usd,
                spent_today_usd=rt.tracker.spent_today_usd, header_secrets=header_secrets)
            transport = self._factory(rt, ws)
            await transport.start(plan)
            rt.extras.update(transport=transport, plan=plan, iter=transport.messages().__aiter__())
        self._begin_running(rt, "resumed" if resumed else None)
        rt.extras.update(subagents={}, streamed=False, text="", assistant_error=None)
        await transport.query(message)
        consume = asyncio.create_task(self._consume(rt))
        rt.extras["consume"] = consume
        try:
            await consume
        except asyncio.CancelledError:
            if not rt.interrupt_requested:
                raise
            if rt.log.active:
                self._finish_interrupted(rt)

    async def _consume(self, rt: TaskRuntime) -> None:
        it = rt.extras["iter"]
        while rt.log.active:
            try:
                m = await it.__anext__()
            except StopAsyncIteration:
                rt.extras.pop("transport", None)            # dead: the next run restarts it
                if rt.interrupt_requested:
                    self._finish_interrupted(rt)
                    return
                raise AgentBackendError(ErrorCode.backend_unavailable,
                                        "the agent worker exited unexpectedly") from None
            try:
                await self._handle_wire(rt, m)
            except AgentBackendError:
                await self._close_transport(rt)      # a failed worker/start-up is not reused
                raise

    # -- wire -> events ------------------------------------------------------- #

    async def _handle_wire(self, rt: TaskRuntime, m: dict[str, Any]) -> None:
        kind = m.get("kind")
        if kind == "system":
            self._on_system(rt, m)
        elif kind == "stream_event":
            self._on_stream_event(rt, m)
        elif kind == "assistant":
            self._on_assistant(rt, m)
        elif kind == "user":
            self._on_user(rt, m)
        elif kind == "permission_request":
            await self._on_permission_request(rt, m)
        elif kind == "result":
            self._on_result(rt, m)
        elif kind == "worker_error":
            raise _worker_error(m)
        # unknown kinds are ignored on purpose (tolerant reader, RESEARCH.md item 14)

    def _on_system(self, rt: TaskRuntime, m: dict[str, Any]) -> None:
        sub, data = m.get("subtype"), m.get("data") or {}
        if sub == "init":
            if data.get("session_id"):
                rt.session_id = data["session_id"]
            errs = list(data.get("mcp_server_errors") or []) + list(data.get("plugin_errors") or [])
            if errs:
                # "fail the run if mcp_server_errors or plugin_errors is non-empty" (item 14)
                raise AgentBackendError(ErrorCode.mcp_error,
                                        f"MCP/plugin start-up failed: {str(errs)[:300]}")
        elif sub == "api_retry":
            status = data.get("error_status") or data.get("status")
            delay = data.get("retry_delay_ms")
            rt.log.emit(
                RetryEvent, attempt=int(data.get("attempt") or 1),
                max_attempts=data.get("max_retries"),
                delay_s=(delay / 1000.0) if isinstance(delay, (int, float)) else None,
                reason=str(data.get("error") or status or "api retry")[:200],
                code=ErrorCode.rate_limited if status == 429 else ErrorCode.backend_unavailable)

    def _on_stream_event(self, rt: TaskRuntime, m: dict[str, Any]) -> None:
        ev = m.get("event") or {}
        if ev.get("type") == "message_start":
            rt.extras["streamed"] = False
        elif ev.get("type") == "content_block_delta":
            delta = ev.get("delta") or {}
            if delta.get("type") == "text_delta" and delta.get("text"):
                rt.extras["streamed"] = True
                rt.extras["text"] += delta["text"]
                rt.log.emit(TextDeltaEvent, text=delta["text"], parent_id=m.get("parent_tool_use_id"))

    def _on_assistant(self, rt: TaskRuntime, m: dict[str, Any]) -> None:
        parent = m.get("parent_tool_use_id")
        if m.get("error"):
            rt.extras["assistant_error"] = str(m["error"])
        streamed, rt.extras["streamed"] = rt.extras.get("streamed", False), False
        for block in m.get("content") or []:
            btype = block.get("type")
            if btype == "text" and block.get("text") and not streamed:
                rt.extras["text"] += block["text"]
                rt.log.emit(TextDeltaEvent, text=block["text"], parent_id=parent)
            elif btype == "tool_use":
                name, call_id = str(block.get("name")), str(block.get("id"))
                inp = block.get("input") if isinstance(block.get("input"), dict) else {}
                rt.log.emit(ToolCallEvent, call_id=call_id, name=name, input=inp, parent_id=parent)
                if name in SUBAGENT_TOOL_NAMES:
                    rt.extras["subagents"][call_id] = parent
                    rt.log.emit(SubagentStartedEvent, subagent_id=call_id, parent_id=parent,
                                name=str(inp.get("subagent_type") or inp.get("name") or "") or None,
                                description=clip(str(inp.get("description") or ""), 300) or None)
                elif name == "Write" and isinstance(inp.get("file_path"), str):
                    self._maybe_artifact(rt, inp["file_path"])

    def _maybe_artifact(self, rt: TaskRuntime, path: str) -> None:
        ws: Workspace | None = rt.extras.get("workspace")
        if ws is None:
            return
        try:
            rel = os.path.relpath(path, ws.root) if os.path.isabs(path) else path
            real = resolve_inside(ws.root, rel)           # never report a path outside the workspace
            rel_clean = real.relative_to(ws.root.resolve()).as_posix()
        except (WorkspaceError, ValueError, OSError):
            return
        rt.log.emit(ArtifactEvent, artifact_id=f"art_{uuid.uuid4().hex[:10]}", kind="file",
                    name=real.name, path=rel_clean)

    def _on_user(self, rt: TaskRuntime, m: dict[str, Any]) -> None:
        parent = m.get("parent_tool_use_id")
        content = m.get("content")
        if not isinstance(content, list):
            return
        for block in content:
            if block.get("type") != "tool_result":
                continue
            call_id = str(block.get("tool_use_id"))
            ok = not block.get("is_error")
            out = block.get("content")
            if isinstance(out, list):
                out = " ".join(str(b.get("text", "")) for b in out if isinstance(b, dict))
            output = clip(str(out or ""))
            if call_id in rt.extras.get("subagents", {}):
                rt.log.emit(SubagentFinishedEvent, subagent_id=call_id,
                            parent_id=rt.extras["subagents"].pop(call_id), ok=ok,
                            summary=clip(output, 300) or None)
            rt.log.emit(ToolResultEvent, call_id=call_id, ok=ok, output=output, parent_id=parent)

    async def _on_permission_request(self, rt: TaskRuntime, m: dict[str, Any]) -> None:
        tool, rid = str(m.get("tool_name")), str(m.get("request_id"))
        inp = m.get("input") if isinstance(m.get("input"), dict) else {}
        verdict, why = decide_permission(rt.spec, tool)
        decision = verdict
        if verdict == "ask":
            decision = await self._request_approval(
                rt, tool_name=tool, input=inp, call_id=m.get("tool_use_id"),
                reason=why, parent_id=m.get("parent_tool_use_id"))
            why = "approved" if decision == "allow" else "denied by the approver"
        transport: SdkTransport | None = rt.extras.get("transport")
        if transport is not None:
            with contextlib.suppress(AgentBackendError):
                await transport.answer_permission(
                    rid, decision == "allow", None if decision == "allow" else f"Denied: {why}")

    def _on_result(self, rt: TaskRuntime, m: dict[str, Any]) -> None:
        sub = str(m.get("subtype") or "")
        if m.get("session_id"):
            rt.session_id = m["session_id"]
        rt.tracker.record_total_cost(m.get("total_cost_usd"))
        usage = m.get("usage") if isinstance(m.get("usage"), dict) else {}
        rt.tracker.record_tokens(usage.get("input_tokens"), usage.get("output_tokens"), total=True)
        n = m.get("num_turns")
        rt.tracker.record_turns(n if isinstance(n, int) else None)
        budget = rt.spec.budget

        if rt.interrupt_requested:
            if rt.tracker.has_cost:
                self._emit_cost(rt, "result")
            self._finish_interrupted(rt)
            return
        if sub == _SUB_MAX_BUDGET:
            self._finish_budget(rt, BudgetVerdict("usd", budget.max_usd or budget.max_usd_per_day or 0.0,
                                                  rt.tracker.usd))
            return
        if sub == _SUB_MAX_TURNS:
            self._finish_budget(rt, BudgetVerdict("turns", float(budget.max_turns or 0),
                                                  float(rt.tracker.turns)))
            return
        if m.get("is_error") or sub.startswith("error"):
            code = ASSISTANT_ERROR_CODES.get(str(rt.extras.get("assistant_error")), ErrorCode.backend_error)
            if rt.tracker.has_cost:
                self._emit_cost(rt, "result")
            self._finish_failed(rt, AgentBackendError(
                code, clip(str(m.get("result") or rt.extras.get("assistant_error") or sub or "agent error"), 500),
                detail=sub or None), resumable=code in (ErrorCode.rate_limited, ErrorCode.backend_unavailable))
            return
        # Success path. The SDK's own cap can overshoot, so ours is re-checked at 100%.
        if self._check_budget_after_usage(rt):
            return
        self._finish_succeeded(rt, (m.get("result") or rt.extras.get("text") or None))

    # -- control -------------------------------------------------------------- #

    async def _do_interrupt(self, rt: TaskRuntime) -> None:
        transport: SdkTransport | None = rt.extras.get("transport")
        if transport is not None and transport.alive:
            try:
                await transport.interrupt()          # client.interrupt(); never SIGTERM mid-turn
            except AgentBackendError as e:
                log.warning("interrupt could not be delivered for task %s: %s", rt.task_id, e.message)
        self._spawn(self._force_after_grace(rt, rt.extras.get("consume")))

    async def _force_after_grace(self, rt: TaskRuntime, consume: asyncio.Task | None) -> None:
        await asyncio.sleep(self.ABORT_GRACE_S)
        if consume is not None and not consume.done() and rt.log.active:
            log.warning("worker did not end the interrupted turn for task %s; closing it", rt.task_id)
            consume.cancel()
            await self._close_transport(rt)

    async def _do_steer(self, rt: TaskRuntime, message: str) -> None:
        transport: SdkTransport | None = rt.extras.get("transport")
        if transport is None or not transport.alive:
            raise AgentBackendError(ErrorCode.backend_unavailable, "the agent worker is not running")
        await transport.query(message)

    async def _close_transport(self, rt: TaskRuntime) -> None:
        transport = rt.extras.pop("transport", None)
        if transport is not None:
            with contextlib.suppress(Exception):
                await transport.close()

    async def _release(self, rt: TaskRuntime) -> None:
        await self._close_transport(rt)

    # -- sessions & diagnostics ----------------------------------------------- #

    async def list_sessions(self, ctx: TaskContext | None = None) -> list[SessionInfo]:
        """Known tasks plus any session transcript under ``<data>/agents/*/.claude``.

        Transcripts are local plaintext JSONL (RESEARCH.md R-SESS); listing reads
        file names and sizes only.
        """
        seen: dict[str, SessionInfo] = {}
        for rt in self._tasks.values():
            if rt.session_id:
                seen[rt.session_id] = SessionInfo(backend=self.name, session_id=rt.session_id,
                                                  task_id=rt.task_id, updated_at=rt.created_at)
        data_dir = (ctx.data_dir if ctx is not None else None) or next(
            (t.ctx.data_dir for t in self._tasks.values()), None)
        if data_dir is not None:
            def scan() -> list[SessionInfo]:
                out = []
                for f in Path(data_dir).glob("agents/*/.claude/projects/*/*.jsonl"):
                    st = f.stat()
                    out.append(SessionInfo(backend=self.name, session_id=f.stem,
                                           task_id=f.parents[3].name, updated_at=st.st_mtime,
                                           size=st.st_size))
                return out
            for s in await asyncio.to_thread(scan):
                prev = seen.get(s.session_id)
                seen[s.session_id] = s if prev is None else prev.model_copy(
                    update={"updated_at": s.updated_at, "size": s.size})
        return sorted(seen.values(), key=lambda s: s.updated_at or 0, reverse=True)

    def _support_task(self, rt: TaskRuntime) -> dict[str, Any]:
        out = super()._support_task(rt)
        plan: ClaudeLaunchPlan | None = rt.extras.get("plan")
        transport = rt.extras.get("transport")
        out["plan"] = plan.describe() if plan else None        # env VALUES and secrets omitted
        out["worker_alive"] = bool(transport and transport.alive)
        return out

    def support_dump(self) -> dict[str, Any]:
        dump = super().support_dump()
        dump.update(auth_route=self._auth_route, sdk_pin=SDK_PIN)
        return dump


# --------------------------------------------------------------------------- #
# Worker side (runs in the child process; the only place the SDK is imported)
# --------------------------------------------------------------------------- #


def _import_sdk() -> Any:
    try:
        import claude_agent_sdk
    except ImportError as e:
        raise AgentBackendError(
            ErrorCode.sdk_missing,
            f"claude-agent-sdk is not installed (pip install 'claude-agent-sdk=={SDK_PIN}', "
            "extra: agents-claude)") from e
    return claude_agent_sdk


def _block_to_wire(b: Any) -> dict[str, Any]:
    n = type(b).__name__
    if n == "TextBlock":
        return {"type": "text", "text": getattr(b, "text", "")}
    if n == "ToolUseBlock":
        return {"type": "tool_use", "id": getattr(b, "id", ""), "name": getattr(b, "name", ""),
                "input": getattr(b, "input", {}) or {}}
    if n == "ToolResultBlock":
        return {"type": "tool_result", "tool_use_id": getattr(b, "tool_use_id", ""),
                "content": getattr(b, "content", None), "is_error": bool(getattr(b, "is_error", False))}
    if n == "ThinkingBlock":
        return {"type": "thinking"}                  # reasoning text is never forwarded
    return {"type": "unknown", "class": n}


def message_to_wire(msg: Any) -> dict[str, Any] | None:
    """SDK message object -> wire dict. Shapes derived from the documented
    dataclasses (RESEARCH.md F6e, R-SESS, R-COST); unknown classes return None."""
    n = type(msg).__name__
    if n == "AssistantMessage":
        return {"kind": "assistant", "content": [_block_to_wire(b) for b in (msg.content or [])],
                "parent_tool_use_id": getattr(msg, "parent_tool_use_id", None),
                "model": getattr(msg, "model", None), "error": getattr(msg, "error", None)}
    if n == "UserMessage":
        content = msg.content
        return {"kind": "user",
                "content": [_block_to_wire(b) for b in content] if isinstance(content, list) else content,
                "parent_tool_use_id": getattr(msg, "parent_tool_use_id", None)}
    if n == "SystemMessage":
        return {"kind": "system", "subtype": getattr(msg, "subtype", None),
                "data": getattr(msg, "data", None) or {}}
    if n == "ResultMessage":
        return {"kind": "result", "subtype": getattr(msg, "subtype", None),
                "is_error": bool(getattr(msg, "is_error", False)),
                "num_turns": getattr(msg, "num_turns", None), "session_id": getattr(msg, "session_id", None),
                "total_cost_usd": getattr(msg, "total_cost_usd", None),
                "usage": getattr(msg, "usage", None) or {}, "result": getattr(msg, "result", None),
                "duration_ms": getattr(msg, "duration_ms", None)}
    if n == "StreamEvent":
        return {"kind": "stream_event", "event": getattr(msg, "event", None) or {},
                "parent_tool_use_id": getattr(msg, "parent_tool_use_id", None)}
    return None


def _worker_exception_code(e: BaseException) -> str:
    n = type(e).__name__
    if n == "CLINotFoundError":
        return "cli_not_found"
    if n in ("CLIConnectionError", "ConnectionError"):
        return "connection"
    if n == "ProcessError":
        return "process_error"
    if n == "CLIJSONDecodeError":
        return "protocol"
    return "process_error"


async def run_worker(inbox: asyncio.Queue, send: Callable[[dict[str, Any]], None],
                     sdk: Any = None) -> None:
    """The worker's whole life. ``inbox`` yields command dicts (``None`` = stdin EOF)."""
    start = await inbox.get()
    if not isinstance(start, dict) or start.get("op") != "start":
        send({"kind": "worker_error", "code": "protocol", "message": "expected a start command"})
        return
    try:
        sdk = sdk or _import_sdk()
    except AgentBackendError as e:
        send({"kind": "worker_error", "code": "sdk_missing", "message": e.message})
        return

    loop = asyncio.get_running_loop()
    pending: dict[str, asyncio.Future] = {}
    wp = start.get("worker") or {}
    opts = json.loads(json.dumps(start.get("options") or {}))
    opts["env"] = {**(opts.get("env") or {}), **(start.get("secret_env") or {})}
    for server, headers in (start.get("secret_mcp_headers") or {}).items():
        srv = (opts.get("mcp_servers") or {}).get(server)
        if srv is not None:
            srv["headers"] = {**(srv.get("headers") or {}), **headers}

    can_use_tool = hooks = keepalive = None
    if wp.get("can_use_tool"):
        async def can_use_tool(tool_name, input_data, context):
            rid = uuid.uuid4().hex
            fut = loop.create_future()
            pending[rid] = fut
            send({"kind": "permission_request", "request_id": rid, "tool_name": tool_name,
                  "input": input_data, "tool_use_id": getattr(context, "tool_use_id", None)})
            answer = await fut
            if answer.get("allow"):
                return sdk.PermissionResultAllow()
            return sdk.PermissionResultDeny(message=answer.get("message") or "Denied")
    if wp.get("keepalive_pretooluse_hook"):
        # The documented workaround that keeps the stream open so can_use_tool can
        # fire (RESEARCH.md R-CUT).
        async def _keepalive(input_data, tool_use_id, context):
            return {"continue_": True}
        keepalive = _keepalive

    try:
        if keepalive is not None:
            hooks = {"PreToolUse": [sdk.HookMatcher(matcher=None, hooks=[keepalive])]}
        options = make_sdk_options(opts, sdk, can_use_tool=can_use_tool, hooks=hooks)
        client = sdk.ClaudeSDKClient(options)
        await client.connect()
    except AgentBackendError as e:
        send({"kind": "worker_error", "code": "options", "message": e.message})
        return
    except AttributeError as e:               # the SDK lacks a class we need (API drift)
        send({"kind": "worker_error", "code": "options",
              "message": f"the installed claude-agent-sdk does not match pin {SDK_PIN}: {e}"[:300]})
        return
    except Exception as e:
        send({"kind": "worker_error", "code": _worker_exception_code(e), "message": str(e)[:300]})
        return
    send({"kind": "ready"})

    async def pump() -> None:
        try:
            async for msg in client.receive_messages():
                wire = message_to_wire(msg)
                if wire is not None:
                    send(wire)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            send({"kind": "worker_error", "code": _worker_exception_code(e), "message": str(e)[:300]})

    pump_task = asyncio.create_task(pump())
    try:
        while True:
            cmd = await inbox.get()
            if cmd is None or cmd.get("op") == "close":
                break
            op = cmd.get("op")
            if op == "query":
                await client.query(cmd.get("prompt") or "")
            elif op == "interrupt":
                await client.interrupt()
            elif op == "permission":
                fut = pending.pop(cmd.get("request_id"), None)
                if fut is not None and not fut.done():
                    fut.set_result({"allow": bool(cmd.get("allow")), "message": cmd.get("message")})
    finally:
        for fut in pending.values():
            if not fut.done():
                fut.set_result({"allow": False, "message": "worker closing"})
        pump_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await pump_task
        with contextlib.suppress(Exception):
            await client.disconnect()


def _stdio_main(sdk: Any = None) -> int:                     # pragma: no cover - subprocess entry
    import threading

    # Protocol channel = the ORIGINAL stdout; anything the SDK or a stray print
    # writes to fd 1 afterwards goes to stderr instead of corrupting the stream.
    proto = os.fdopen(os.dup(1), "wb", buffering=0)
    os.dup2(2, 1)
    lock = threading.Lock()

    def send(obj: dict[str, Any]) -> None:
        data = (json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8")
        with lock:
            proto.write(data)

    async def main() -> None:
        loop = asyncio.get_running_loop()
        inbox: asyncio.Queue = asyncio.Queue()

        def reader() -> None:
            for line in sys.stdin.buffer:
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    loop.call_soon_threadsafe(inbox.put_nowait, obj)
            loop.call_soon_threadsafe(inbox.put_nowait, None)         # EOF = graceful close

        threading.Thread(target=reader, name="worker-stdin", daemon=True).start()
        await run_worker(inbox, send, sdk)

    asyncio.run(main())
    return 0


if __name__ == "__main__":                                   # pragma: no cover
    raise SystemExit(_stdio_main())
