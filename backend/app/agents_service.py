"""The task manager: runs agents on their backends and keeps the books.

This is the layer between ``app/agents/`` (a library: the :class:`AgentBackend` interface,
normalized events, adapters) and the app (routes, WebSocket frames, the database, the
UI). Every design decision cites ``RESEARCH.md`` by id, as the library does.

What it owns
------------
* ONE :class:`TaskManager` holding the backend instances: the scriptable fake (tests), the
  OpenClaw adapter, and the Claude Agent adapter - created lazily, and only when the
  optional ``agents-claude`` extra is importable and a credential exists.
* The event pipeline. Each task gets a *pump* that reads ``backend.events(task_id,
  after_seq=cursor)`` (the public interface, so any future adapter plugs in unchanged),
  assigns nothing itself (``seq`` is the backend's), and for every event: queues a database
  row, updates the task row, charges the spend ledger, opens/answers approval rows, sends
  WebSocket frames to the people allowed to see the task, and raises notifications. A
  single writer coroutine applies the queued rows in batches, one transaction each, so a
  token stream never costs an fsync per delta.
* Budgets (brief: "per agent and per profile - USD/day, max turns, max wall-clock, token
  cap. Hard stop plus a notification at the limit"). Before every run the effective
  :class:`Budget` is recomputed from the spend ledger and the profile's limits; while a task
  runs, every ``cost_update`` is checked against the *shared* per-day caps and the task is
  hard-stopped through ``stop_for_budget`` when a cap other tasks also spend from is crossed.
  Figures are ESTIMATES (R-COST, section 5 item 9) - never billing data.
* Approvals. An ``approval_request`` becomes a row, a frame and a notification; the answer
  arrives through :meth:`respond_approval`, which records who decided what. Destructive
  tools always arrive as a request with scope ``once`` only (G17).
* Restart recovery (:meth:`restore`): a task that was running when the process died comes
  back ``cancelled``/``restart`` and, when it has a backend session, *resumable* - the same
  conversation continues (R-SESS, section 5 item 10).
* Retention (``agents.retention_days``, default 30): the purge job deletes the transcript
  (events, prompt, result, workspace) of finished tasks and keeps the audit row.
* The advisor handoff bridge and the OpenClaw chat bridge, behind the feature flag
  ``agents.unified_dispatch`` (default OFF - see docs/agents.md for what each does).

What it deliberately does not do: talk to a gateway or to Anthropic on its own (the
adapters do, when an owner dispatches a task), touch ``app.main``, or decide who may do
what - ``agents_api`` and ``policy`` do that and pass in resolved facts.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import os
import shutil
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from . import accounts, config
from . import agents_store as store
from . import agents_ws as ws_frames
from .agents.budget import Budget, BudgetVerdict
from .agents.events import (
    TERMINAL_STATES,
    ErrorCode,
    ErrorEvent,
    ResultEvent,
    TaskState,
)
from .agents.interface import (
    AgentBackendError,
    AgentSpec,
    BaseBackend,
    Capabilities,
    TaskContext,
)
from .agents.secrets import (
    GLOBAL_REDACTOR,
    SECRET_ANTHROPIC_API_KEY,
    SecretError,
    SecretProvider,
    default_provider,
    get_agent_logger,
)
from .agents.workspace import WorkspaceError, cleanup_workspace, existing_workspace

log = get_agent_logger("service")

#: What a person sees on the backend picker (branding rules: docs/agents/security.md s8).
BACKEND_LABELS = {"openclaw": "OpenClaw", "claude_sdk": "Claude Agent", "fake": "Fake agent (tests)"}

#: Backends an agent definition may name. ``cli`` (M3) and Managed Agents (M2) are not built.
KNOWN_BACKENDS = ("openclaw", "claude_sdk")

#: Auth routes the Claude screen offers (RESEARCH.md F2/F2b, section 5 item 1). "google" is the
#: UI word for the adapter's ``vertex`` route. There is deliberately no subscription login.
CLAUDE_ROUTES = {"api_key": "api_key", "bedrock": "bedrock", "google": "vertex", "foundry": "foundry"}
CLAUDE_ROUTE_ENV = {"bedrock": "CLAUDE_CODE_USE_BEDROCK", "vertex": "CLAUDE_CODE_USE_VERTEX",
                    "foundry": "CLAUDE_CODE_USE_FOUNDRY"}

#: Secrets an MCP server header may name. The Anthropic key and the gateway token are NOT
#: eligible: an admin could otherwise route them to an MCP URL of their choosing.
MCP_SECRET_RE_PREFIXES = ("MCP_", "STUDIOFORGE_")

NOTIFY_TITLES = {"approval": "A task needs your approval", "done": "A task finished",
                 "failed": "A task failed"}
RESULT_PREVIEW_CHARS = 600


# --------------------------------------------------------------------------- #
# Hooks (main.py's plumbing, handed over explicitly - see llm_api.Hooks)
# --------------------------------------------------------------------------- #


@dataclass
class Hooks:
    #: ``main._deliver_assistant_text`` - the one funnel every assistant row passes through.
    deliver: Callable[..., Awaitable[Any]] | None = None
    #: ``main._notify_push(kind, ref, title)`` - fire and forget, never raises.
    push: Callable[[str, str, str], None] | None = None
    #: ``thread_id -> {"bot_id": ...} | None`` so a task can only attach to a thread that exists.
    thread_info: Callable[[str], Awaitable[dict | None]] | None = None
    #: a callable returning True while the app is tearing down (no new pushes/posts).
    shutting_down: Callable[[], bool] = lambda: False


class BudgetExhausted(Exception):
    def __init__(self, dimension: str, message: str) -> None:
        super().__init__(message)
        self.dimension = dimension
        self.message = message


class TaskError(Exception):
    """A request the manager refuses; ``code`` is an error-catalog slug."""

    def __init__(self, code: str, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


@dataclass
class ChatOutcome:
    state: str
    reason: str | None = None
    text: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    cost_usd: float | None = None


def unified_dispatch_enabled() -> bool:
    """The ``agents.unified_dispatch`` flag: environment override first, then the setting."""
    v = (config.env("AGENTS_UNIFIED_DISPATCH", "") or "").strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    return bool(store.get_setting("agents.unified_dispatch"))


def principal_key(principal: Any) -> str:
    """The id stored as a task's owner."""
    pid = getattr(principal, "profile_id", None)
    if pid:
        return str(pid)
    return "owner" if getattr(principal, "kind", "") == "open" else ""


def owner_name(owner: str) -> str:
    if not owner:
        return ""
    prof = accounts.get_profile(owner)
    if prof is not None:
        return prof.name
    return "Owner" if owner == "owner" else owner


def mcp_secret_ok(name: str) -> bool:
    import re
    return bool(re.fullmatch(r"[A-Z][A-Z0-9_]{2,63}", name or "")) and name.startswith(MCP_SECRET_RE_PREFIXES)


# --------------------------------------------------------------------------- #
# Public shapes
# --------------------------------------------------------------------------- #


_EVENT_JSON_LIMIT = 64_000


def _bound(d: dict[str, Any]) -> dict[str, Any]:
    """Keep one event from becoming a megabyte row and frame: a tool that writes a big file,
    or returns one, would otherwise ship it whole. The backend's own transcript keeps the full
    text (docs/agents/backends.md: strings in events are clipped to 8000 characters)."""
    try:
        if len(json.dumps(d, separators=(",", ":"), default=str)) <= _EVENT_JSON_LIMIT:
            return d
    except (TypeError, ValueError):
        return d
    from .agents.events import clip

    def walk(v: Any) -> Any:
        if isinstance(v, str):
            return clip(v)
        if isinstance(v, dict):
            return {k: walk(x) for k, x in v.items()}
        if isinstance(v, list):
            return [walk(x) for x in v[:200]]
        return v
    return walk(d)


def args_summary(inp: dict[str, Any] | None, limit: int = 300) -> str:
    """A short, already-redacted rendering of a tool call's arguments for an approval card."""
    if not inp:
        return ""
    try:
        text = json.dumps(inp, ensure_ascii=False, separators=(", ", ": "), default=str)
    except (TypeError, ValueError):
        text = str(inp)
    text = GLOBAL_REDACTOR.redact(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def duration_s(row: dict[str, Any]) -> float | None:
    a, b = row.get("started_at"), row.get("finished_at")
    if not a or not b:
        return None
    try:
        return round((datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds(), 1)
    except ValueError:
        return None


def public_task(row: dict[str, Any], *, pending: list[dict] | None = None,
                with_text: bool = True) -> dict[str, Any]:
    """A task row as the API returns it. The session id is internal and never leaves."""
    out = {
        "id": row["task_id"], "agent_id": row["agent_id"], "agent_name": row["agent_name"],
        "backend": row["backend"], "backend_label": BACKEND_LABELS.get(row["backend"], row["backend"]),
        "owner": row["owner"], "owner_name": owner_name(row["owner"]), "kind": row["kind"],
        "state": row["state"], "reason": row["reason"], "hold": row["hold"],
        "cost_usd": row["cost_usd"], "cost_is_estimate": True, "tokens": row["tokens"],
        "turns": row["turns"], "resumable": bool(row["resumable"]), "created_at": row["created_at"],
        "started_at": row["started_at"], "finished_at": row["finished_at"],
        "duration_s": duration_s(row), "origin_thread_id": row["origin_thread_id"],
        "origin_message_id": row["origin_message_id"], "last_seq": row["last_seq"],
        "approvals": {"allowed": row["approvals_allowed"], "denied": row["approvals_denied"]},
        "purged": bool(row["purged"]),
    }
    if with_text:
        out["prompt"] = row["prompt"]
        out["result_text"] = row["result_text"]
    if pending is not None:
        out["pending_approvals"] = pending
    return out


def public_approval(a: dict[str, Any]) -> dict[str, Any]:
    return {"request_id": a["request_id"], "task_id": a["task_id"], "tool": a["tool"],
            "args_summary": a["args_summary"], "scopes": a["scopes"], "state": a["decision"] or "pending",
            "scope": a["scope"], "decided_by": a["decided_by"], "decided_at": a["decided_at"],
            "requested_at": a["requested_at"]}


def public_agent(a: dict[str, Any], *, full: bool = True) -> dict[str, Any]:
    spec = a["spec"] if isinstance(a["spec"], dict) else {}
    out = {"id": a["id"], "name": a["name"], "description": a["description"],
           "backend": spec.get("backend"), "backend_label": BACKEND_LABELS.get(spec.get("backend"), spec.get("backend")),
           "disabled": a["disabled"], "published_to": a["published_to"],
           "approval_mode": (spec.get("approval_policy") or {}).get("mode", "interactive"),
           "model": spec.get("model")}
    if full:
        out.update({"spec": spec, "created_by": a["created_by"], "created_at": a["created_at"],
                    "updated_at": a["updated_at"]})
    return out


# --------------------------------------------------------------------------- #
# The manager
# --------------------------------------------------------------------------- #


class TaskManager:
    """See the module docstring. One per process; the app keeps it in ``agents_service.manager``."""

    #: results older than this are dropped from the in-memory registry after a run ends
    FORGET_AFTER_S = 600.0

    def __init__(self, data_dir: Path, *, hooks: Hooks | None = None,
                 secrets: SecretProvider | None = None) -> None:
        self.data_dir = Path(data_dir)
        self.hooks = hooks or Hooks()
        self.secrets: SecretProvider = secrets or default_provider(self.data_dir)
        self.backends: dict[str, BaseBackend] = {}
        self._cursor: dict[str, int] = {}
        self._rows: dict[str, dict[str, Any]] = {}
        self._pumps: dict[str, asyncio.Task] = {}
        self._pump_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._cost_seen: dict[str, tuple[float, int, int]] = {}
        self._waiters: dict[str, list[asyncio.Future]] = defaultdict(list)
        self._last_error: dict[str, dict[str, Any]] = {}
        self._wq: asyncio.Queue | None = None
        self._writer: asyncio.Task | None = None
        self._kick_lock = asyncio.Lock()
        self._bg: set[asyncio.Task] = set()
        self._closing = False
        self._live: set[str] = set()           # tasks handed to a backend and not yet finished
        self._launched: set[str] = set()       # every task this process has handed to a backend
        self._purger: asyncio.Task | None = None

    # -- lifecycle ----------------------------------------------------------- #

    async def startup(self) -> None:
        await asyncio.to_thread(self._ensure_schema)
        self._wq = asyncio.Queue()
        self._writer = asyncio.create_task(self._writer_loop(), name="agents-writer")
        await self.restore()
        await self._kick()
        self._purger = asyncio.create_task(self.purge_loop(), name="agents-purge")

    @staticmethod
    def _ensure_schema() -> None:
        with store.connect():
            pass

    async def shutdown(self) -> None:
        self._closing = True
        for fut_list in self._waiters.values():
            for fut in fut_list:
                if not fut.done():
                    fut.cancel()
        if self._purger is not None:
            await _stop(self._purger)
            self._purger = None
        for t in list(self._pumps.values()):
            await _stop(t)
        for b in list(self.backends.values()):
            with contextlib.suppress(Exception):
                await b.aclose()
        with contextlib.suppress(Exception):
            await self.flush()
        if self._writer is not None:
            await _stop(self._writer)
            self._writer = None
        for t in list(self._bg):
            await _stop(t)

    def _spawn(self, coro: Awaitable[Any]) -> asyncio.Task:
        task = asyncio.ensure_future(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)
        return task

    # -- the single writer ----------------------------------------------------- #

    def _enqueue(self, op: store.Op) -> None:
        if self._wq is None:                              # not started: write straight through
            store.run_batch([op])
            return
        self._wq.put_nowait(op)

    async def _writer_loop(self) -> None:
        assert self._wq is not None
        while True:
            op = await self._wq.get()
            batch = [op]
            while len(batch) < 500:
                try:
                    batch.append(self._wq.get_nowait())
                except asyncio.QueueEmpty:
                    break
            try:
                await asyncio.to_thread(store.run_batch, batch)
            except Exception:
                log.exception("task writer: a batch of %d row(s) failed; retrying one by one", len(batch))
                for one in batch:
                    with contextlib.suppress(Exception):
                        await asyncio.to_thread(store.run_batch, [one])
            finally:
                for _ in batch:
                    self._wq.task_done()

    async def flush(self) -> None:
        """Wait until everything queued so far is on disk (read-your-writes for the routes)."""
        if self._wq is not None:
            await self._wq.join()

    # -- backends --------------------------------------------------------------- #

    def register_backend(self, name: str, backend: BaseBackend) -> None:
        self.backends[name] = backend

    def claude_route(self) -> str:
        v = store.get_setting("agents.claude.auth_route") or "api_key"
        return v if v in CLAUDE_ROUTES else "api_key"

    def credential_status(self) -> dict[str, Any]:
        """What the credentials screen shows. NEVER the key itself."""
        env_set = bool(os.environ.get(SECRET_ANTHROPIC_API_KEY))
        stored = False
        with contextlib.suppress(Exception):
            stored = bool(self.secrets.get(SECRET_ANTHROPIC_API_KEY)) and not env_set
        route = self.claude_route()
        return {"route": route, "routes": list(CLAUDE_ROUTES),
                "api_key_set": env_set or stored,
                "api_key_source": "environment" if env_set else ("stored" if stored else None),
                "provider_env": CLAUDE_ROUTE_ENV.get(CLAUDE_ROUTES[route])}

    def set_claude_key(self, key: str) -> None:
        self.secrets.set(SECRET_ANTHROPIC_API_KEY, key)
        GLOBAL_REDACTOR.register(key)

    def clear_claude_key(self) -> None:
        self.secrets.delete(SECRET_ANTHROPIC_API_KEY)

    def set_claude_route(self, route: str) -> None:
        if route not in CLAUDE_ROUTES:
            raise TaskError("agent_credentials_invalid", "The credentials were not accepted.", 422)
        store.set_setting("agents.claude.auth_route", route)
        old = self.backends.get("claude_sdk")
        if old is not None and not old.any_active():
            self.backends.pop("claude_sdk", None)         # rebuilt with the new route on next use

    def _make_backend(self, name: str) -> BaseBackend:
        if name == "openclaw":
            from .agents.openclaw_adapter import OpenClawBackend
            return OpenClawBackend()
        if name == "claude_sdk":
            from .agents.claude_sdk_adapter import ClaudeAgentBackend
            return ClaudeAgentBackend(auth_route=CLAUDE_ROUTES[self.claude_route()])
        raise AgentBackendError(ErrorCode.not_supported, f"unknown agent backend {name!r}")

    def backend_problem(self, name: str) -> tuple[ErrorCode, str] | None:
        """Why ``name`` cannot run a task right now, or None. Cheap: never connects anywhere."""
        if name in self.backends and name not in KNOWN_BACKENDS:
            return None
        if name == "openclaw":
            if "openclaw" in self.backends:
                return None
            from . import openclaw
            if not openclaw.cli_available() and not _gateway_configured():
                return (ErrorCode.backend_unavailable,
                        "OpenClaw is not installed on this machine (it must run on the same host).")
            return None
        if name == "claude_sdk":
            if importlib.util.find_spec("claude_agent_sdk") is None and "claude_sdk" not in self.backends:
                return (ErrorCode.sdk_missing,
                        "The Claude Agent needs the optional extra: install DisPatch with "
                        "`uv sync --extra agents-claude`.")
            st = self.credential_status()
            if st["route"] == "api_key" and not st["api_key_set"]:
                return (ErrorCode.auth_failed, "The Claude Agent needs an Anthropic API key (Settings, Agents).")
            return None
        if name in self.backends:
            return None
        return ErrorCode.not_supported, f"unknown agent backend {name!r}"

    def backend_for(self, name: str) -> BaseBackend:
        b = self.backends.get(name)
        if b is not None:
            return b
        problem = self.backend_problem(name)
        if problem is not None:
            raise AgentBackendError(problem[0], problem[1])
        b = self._make_backend(name)
        self.backends[name] = b
        return b

    def backend_info(self) -> list[dict[str, Any]]:
        """Capabilities per backend for the picker and the Agents page."""
        out: list[dict[str, Any]] = []
        names = list(KNOWN_BACKENDS) + [n for n in self.backends if n not in KNOWN_BACKENDS]
        for name in names:
            problem = self.backend_problem(name)
            caps: Capabilities | None = None
            try:
                caps = (self.backends.get(name) or self._make_backend(name)).capabilities()
            except Exception:
                log.debug("capabilities unavailable for %s", name, exc_info=True)
            entry: dict[str, Any] = {
                "name": name, "label": BACKEND_LABELS.get(name, name),
                "available": problem is None,
                "problem": None if problem is None else {"code": problem[0].value, "message": problem[1]},
                "capabilities": caps.model_dump(mode="json") if caps else None,
            }
            if name == "claude_sdk":
                st = self.credential_status()
                entry["needs_api_key"] = st["route"] == "api_key" and not st["api_key_set"]
                entry["credentials"] = st
            out.append(entry)
        return out

    # -- budgets ------------------------------------------------------------------ #

    def _day_remaining(self, spec: AgentSpec, agent_id: str, owner: str) -> tuple[float | None, str]:
        rems: list[tuple[float, str]] = []
        if spec.budget.max_usd_per_day is not None:
            rems.append((spec.budget.max_usd_per_day - store.spent_today("agent", agent_id), "agent"))
        pb = store.profile_budget(owner) if owner else {}
        if pb.get("usd_per_day"):
            rems.append((float(pb["usd_per_day"]) - store.spent_today("profile", owner), "profile"))
        if not rems:
            return None, ""
        return min(rems)

    def effective_budget(self, spec: AgentSpec, agent_id: str, owner: str, task_usd: float) -> Budget:
        """The budget one run is held to: the agent's own limits, tightened by the profile's,
        with the per-day cap turned into what is LEFT today (+ what this task already spent,
        because the tracker counts the task's lifetime total). Raises BudgetExhausted."""
        b = spec.budget.model_dump()
        rem, who = self._day_remaining(spec, agent_id, owner)
        if rem is not None:
            if rem <= 0:
                raise BudgetExhausted("usd_per_day", "This agent's budget for today is used up."
                                      if who == "agent" else "Your daily agent budget is used up.")
            b["max_usd_per_day"] = round(rem + (task_usd or 0.0), 6)
        pb = store.profile_budget(owner) if owner else {}
        for key in ("max_turns", "max_wall_clock_s", "max_tokens"):
            vals = [v for v in (b.get(key), pb.get(key)) if v]
            b[key] = min(vals) if vals else None
        return Budget(**b)

    # -- dispatch ------------------------------------------------------------------ #

    async def dispatch(self, *, agent: dict[str, Any], prompt: str, owner: str,
                       origin_thread_id: str | None = None, origin_message_id: str | None = None,
                       repo_path: str | None = None, kind: str = "task", hold: str | None = None,
                       advisor_request_id: str | None = None, task_id: str | None = None,
                       post_card: bool = True) -> dict[str, Any]:
        """Create a task for ``agent`` and start it (or queue it). Returns the task row.

        The caller (the route) has already decided that ``owner`` may use ``agent``.
        """
        spec = AgentSpec.model_validate(agent["spec"])
        problem = self.backend_problem(spec.backend)
        if problem is not None and hold is None:
            raise AgentBackendError(problem[0], problem[1])
        self.effective_budget(spec, agent["id"], owner, 0.0)          # fail early (429)
        fields: dict[str, Any] = {
            "agent_id": agent["id"], "agent_name": agent["name"], "backend": spec.backend, "owner": owner,
            "kind": kind, "state": "queued", "hold": hold, "spec_json": json.dumps(agent["spec"]),
            "prompt": prompt, "origin_thread_id": origin_thread_id, "origin_message_id": origin_message_id,
            "advisor_request_id": advisor_request_id, "repo_path": repo_path}
        if task_id:
            fields["task_id"] = task_id
        row = store.create_task(**fields)
        self._rows[row["task_id"]] = row
        await ws_frames.publish(ws_frames.update_frame(row), row["owner"])
        if post_card and kind == "task":
            await self._post_assign_card(row)
        if hold is None:
            await self._kick()
        return store.get_task(row["task_id"]) or row

    async def _post_assign_card(self, row: dict[str, Any]) -> None:
        """A line in the originating chat that carries the live task card."""
        tid = row.get("origin_thread_id")
        if not tid or self.hooks.deliver is None:
            return
        snippet = " ".join((row.get("prompt") or "").split())
        snippet = snippet if len(snippet) <= 160 else snippet[:159] + "…"
        try:
            msg = await self.hooks.deliver(
                tid, f"Assigned to **{row['agent_name']}**: {snippet}",
                metadata={"notice": {"level": "info"},
                          "task_card": {"task_id": row["task_id"], "agent_name": row["agent_name"]}},
                source_id=f"task:{row['task_id']}:assigned", provisional=False,
                dedup_recent_window=False)
            mid = getattr(msg, "id", None)
            if mid:
                self._enqueue(store.op_task(row["task_id"], origin_message_id=mid))
        except Exception:
            log.warning("could not post the task card for %s", row["task_id"], exc_info=True)

    async def _kick(self) -> None:
        """Start queued tasks while there are free slots."""
        if self._closing:
            return
        async with self._kick_lock:
            limit = max(1, int(store.get_setting("agents.max_concurrent") or 4))
            await self.flush()                  # the queue below reads what the writer has stored
            for row in store.queued_tasks():
                if len(self._live) >= limit:
                    break
                if row["task_id"] in self._launched:
                    continue
                await self._start(self._rows.setdefault(row["task_id"], row))

    async def _start(self, row: dict[str, Any]) -> None:
        tid = row["task_id"]
        self._live.add(tid)
        self._launched.add(tid)
        try:
            spec = AgentSpec.model_validate_json(row["spec_json"])
            backend = self.backend_for(spec.backend)
            budget = self.effective_budget(spec, row["agent_id"], row["owner"], row["cost_usd"] or 0.0)
            ctx = TaskContext(
                data_dir=self.data_dir, task_id=tid, owner=row["owner"] or None,
                origin_thread_id=row["origin_thread_id"],
                repo_path=Path(row["repo_path"]) if row["repo_path"] else None, secrets=self.secrets)
            self._cursor.setdefault(tid, -1)
            await backend.start_task(spec.model_copy(update={"budget": budget}), row["prompt"] or "", ctx)
        except BudgetExhausted as e:
            await self._fail_early(row, ErrorCode.budget_exceeded, e.message)
            return
        except AgentBackendError as e:
            await self._fail_early(row, e.code, e.message)
            return
        except Exception as e:
            log.exception("could not start task %s", tid)
            await self._fail_early(row, ErrorCode.internal, f"{type(e).__name__}: {e}")
            return
        self._enqueue(store.op_spend(row["agent_id"], row["owner"], 0.0, 0, 0, tasks=1))
        self._ensure_pump(tid)

    async def _fail_early(self, row: dict[str, Any], code: ErrorCode, message: str) -> None:
        """The task never reached its backend: record why, through the normal pipeline."""
        tid = row["task_id"]
        seq = self._cursor.get(tid, -1) + 1
        now = time.time()
        await self._handle(tid, ErrorEvent(task_id=tid, ts=now, seq=seq, code=code, message=message))
        await self._handle(tid, ResultEvent(task_id=tid, ts=now, seq=seq + 1, state=TaskState.failed,
                                            reason=code.value, resumable=False))

    # -- pumps and events ---------------------------------------------------------- #

    def _ensure_pump(self, tid: str) -> None:
        if self._closing:
            return
        # A pump that is still draining the previous run holds the lock; a new pump waits for
        # it and continues from the cursor, so nothing is missed and nothing is handled twice.
        self._pumps[tid] = self._spawn(self._pump(tid))

    async def _pump(self, tid: str) -> None:
        async with self._pump_locks[tid]:
            row = self._row(tid)
            if row is None:
                return
            backend = self.backends.get(row["backend"])
            if backend is None or not backend.has_task(tid):
                return
            try:
                async for ev in backend.events(tid, after_seq=self._cursor.get(tid, -1)):
                    await self._handle(tid, ev)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("event pump for task %s failed", tid)

    def _row(self, tid: str) -> dict[str, Any] | None:
        row = self._rows.get(tid)
        if row is None:
            row = store.get_task(tid)
            if row is not None:
                self._rows[tid] = row
        return row

    async def _handle(self, tid: str, ev: Any) -> None:
        """One event: persist, account, announce. Idempotent on ``seq``."""
        d = ev if isinstance(ev, dict) else ev.model_dump(mode="json")
        seq = int(d["seq"])
        if seq <= self._cursor.get(tid, -1):
            return
        self._cursor[tid] = seq
        row = self._row(tid)
        if row is None:
            return
        d = _bound(d)
        chat = row["kind"] == "chat"        # a chat turn's transcript is the chat itself
        if not chat:
            self._enqueue(store.op_event(tid, d))
        fields: dict[str, Any] = {"last_seq": seq}
        t = d["type"]
        after: list[Callable[[], Awaitable[None]]] = []

        if t == "status":
            fields["state"] = d["state"]
            if d["state"] == "running" and not row.get("started_at"):
                fields["started_at"] = store.now_iso()
        if t in ("status", "cost_update", "tool_call"):
            fields.update(self._session_fields(row))
        if t == "approval_request":
            appr = {"request_id": d["request_id"], "task_id": tid, "tool": d["tool_name"],
                    "args_summary": args_summary(d.get("input")), "scopes": d.get("scopes") or ["once"],
                    "call_id": d.get("call_id"), "requested_at": store.now_iso()}
            self._enqueue(store.op_approval_add(appr))
            fields["state"] = "awaiting_approval"
            after.append(lambda a=appr: self._announce_approval(row, a))
        elif t == "cost_update":
            fields.update(self._charge(row, d))
        elif t == "error":
            self._last_error[tid] = {"code": d["code"], "message": d["message"]}
        elif t == "result":
            fields.update(self._result_fields(row, d))
        row.update(fields)
        self._enqueue(store.op_task(tid, **{k: v for k, v in fields.items() if not k.startswith("_")}))

        if not chat:
            await ws_frames.publish(ws_frames.event_frame(row, d), row["owner"])
        for fn in after:
            await fn()
        if t == "cost_update":
            await self._enforce_shared_limits(row)
        elif t == "result":
            await self._finish(row, d)

    def _session_fields(self, row: dict[str, Any]) -> dict[str, Any]:
        be = self.backends.get(row["backend"])
        if be is None or not be.has_task(row["task_id"]):
            return {}
        info = be.task_info(row["task_id"])
        out: dict[str, Any] = {}
        if int(info.resumable) != int(bool(row.get("resumable"))):
            out["resumable"] = int(info.resumable)
        if info.session_id and info.session_id != row.get("session_id"):
            out["session_id"] = info.session_id
        return out

    def _charge(self, row: dict[str, Any], d: dict[str, Any]) -> dict[str, Any]:
        tid = row["task_id"]
        seen = self._cost_seen.get(tid) or (row.get("cost_usd") or 0.0, row.get("tokens") or 0,
                                            row.get("turns") or 0)
        usd = d.get("total_cost_usd")
        usd = seen[0] if usd is None else max(seen[0], float(usd))
        toks = (d.get("input_tokens") or 0) + (d.get("output_tokens") or 0)
        toks = seen[1] if not toks else max(seen[1], int(toks))
        turns = seen[2] if d.get("turns") is None else max(seen[2], int(d["turns"]))
        du, dt, dn = max(0.0, usd - seen[0]), toks - seen[1], turns - seen[2]
        self._cost_seen[tid] = (usd, toks, turns)
        if du or dt or dn:
            self._enqueue(store.op_spend(row["agent_id"], row["owner"], du, dt, dn))
        out: dict[str, Any] = {"tokens": toks, "turns": turns}
        if d.get("total_cost_usd") is not None or row.get("cost_usd") is not None:
            out["cost_usd"] = round(usd, 6)
        return out

    def _result_fields(self, row: dict[str, Any], d: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {"state": d["state"], "reason": d.get("reason"),
                               "finished_at": store.now_iso(), "resumable": int(bool(d.get("resumable")))}
        if d.get("text") is not None:
            out["result_text"] = d["text"]
        if d.get("session_id"):
            out["session_id"] = d["session_id"]
        return out

    async def _enforce_shared_limits(self, row: dict[str, Any]) -> None:
        """Per-day caps are shared between tasks: re-check them against the LEDGER on every
        cost update and hard-stop this task when another one already spent the rest."""
        be = self.backends.get(row["backend"])
        if be is None or not be.is_active(row["task_id"]):
            return
        try:
            spec = AgentSpec.model_validate_json(row["spec_json"])
        except Exception:
            return
        await self.flush()
        checks: list[tuple[float, float]] = []
        if spec.budget.max_usd_per_day:
            checks.append((spec.budget.max_usd_per_day, store.spent_today("agent", row["agent_id"])))
        pb = store.profile_budget(row["owner"]) if row["owner"] else {}
        if pb.get("usd_per_day"):
            checks.append((float(pb["usd_per_day"]), store.spent_today("profile", row["owner"])))
        for limit, spent in checks:
            if spent > limit:
                be.stop_for_budget(row["task_id"], BudgetVerdict("usd_per_day", limit, spent))
                return

    async def _finish(self, row: dict[str, Any], d: dict[str, Any]) -> None:
        tid = row["task_id"]
        self._live.discard(tid)
        # Whatever was still waiting on a person can no longer be answered.
        await self.flush()
        pend = store.list_approvals(tid, pending_only=True)
        self._enqueue(store.op_approvals_expire(tid))
        for a in pend:
            if row["kind"] != "chat":
                await ws_frames.publish(ws_frames.approval_frame(row, a, "expired"), row["owner"])
        state, reason = d["state"], d.get("reason")
        err = self._last_error.pop(tid, None)
        for fut in self._waiters.pop(tid, []):
            if not fut.done():
                fut.set_result(ChatOutcome(
                    state=state, reason=reason, text=d.get("text"),
                    error_code=(err or {}).get("code"), error_message=(err or {}).get("message"),
                    cost_usd=row.get("cost_usd")))
        if row["kind"] != "chat":
            self._audit(row, d)
            if state == "succeeded":
                await self._notify(row, "done", d)
            elif state == "failed":
                await self._notify(row, "failed", d, err)
        # A finished run frees a slot; let queued work start, and let the backend forget
        # an idle task later (it is restored from the database if someone continues it).
        self._spawn(self._after_run(tid))

    async def _after_run(self, tid: str) -> None:
        with contextlib.suppress(Exception):
            await self._kick()
        await asyncio.sleep(self.FORGET_AFTER_S)
        row = self._rows.get(tid)
        if row is not None and row["state"] in ("succeeded", "failed", "cancelled"):
            be = self.backends.get(row["backend"])
            if be is not None and be.forget_task(tid):
                self._rows.pop(tid, None)
                self._cursor.pop(tid, None)
                self._pump_locks.pop(tid, None)
                self._cost_seen.pop(tid, None)

    # -- notifications and audit ------------------------------------------------------ #

    def _audit(self, row: dict[str, Any], d: dict[str, Any]) -> None:
        """Who, which agent, approvals, cost, duration, outcome (brief: Audit)."""
        detail = json.dumps({
            "task": row["task_id"], "agent": row["agent_name"], "backend": row["backend"],
            "outcome": d["state"], "reason": d.get("reason"),
            "approvals_allowed": row["approvals_allowed"], "approvals_denied": row["approvals_denied"],
            "cost_usd_estimate": row.get("cost_usd"), "duration_s": duration_s(row),
            "turns": row.get("turns")}, separators=(",", ":"))
        accounts.log_activity("agent_task", profile_id=row["owner"] or None, subject=row["agent_name"],
                              detail=detail, category="audit")

    async def _announce_approval(self, row: dict[str, Any], appr: dict[str, Any]) -> None:
        await ws_frames.publish(ws_frames.approval_frame(row, appr, "pending"), row["owner"])
        await self._notify(row, "approval", None, extra=appr)

    async def _notify(self, row: dict[str, Any], kind: str, d: dict[str, Any] | None,
                      err: dict[str, Any] | None = None, extra: dict[str, Any] | None = None) -> None:
        """Web Push (a ping with no content) and a notice line in the originating chat."""
        if self._closing or self.hooks.shutting_down():
            return
        if self.hooks.push is not None:
            with contextlib.suppress(Exception):
                self.hooks.push(f"task_{kind}" if kind != "approval" else "approval",
                                f"task:{row['task_id']}", NOTIFY_TITLES[kind])
        tid, deliver = row.get("origin_thread_id"), self.hooks.deliver
        if not tid or deliver is None:
            return
        name = row["agent_name"]
        if kind == "approval":
            tool = (extra or {}).get("tool", "a tool")
            text, level = f"**{name}** needs your approval to use `{tool}`.", "warn"
            sid = f"task:{row['task_id']}:approval:{(extra or {}).get('request_id')}"
        elif kind == "done":
            body = " ".join(((d or {}).get("text") or "").split())
            if len(body) > RESULT_PREVIEW_CHARS:
                body = body[: RESULT_PREVIEW_CHARS - 1] + "…"
            text, level = f"**{name}** finished." + (f"\n\n{body}" if body else ""), "ok"
            sid = f"task:{row['task_id']}:done:{d.get('seq') if d else ''}"
        else:
            why = (err or {}).get("message") or (d or {}).get("reason") or "it stopped"
            text, level = f"**{name}** failed: {GLOBAL_REDACTOR.redact(str(why))[:300]}", "error"
            sid = f"task:{row['task_id']}:failed:{d.get('seq') if d else ''}"
        try:
            await deliver(tid, text, metadata={"notice": {"level": level},
                                               "task_card": {"task_id": row["task_id"],
                                                             "agent_name": name}},
                          source_id=sid, provisional=False, dedup_recent_window=False)
        except Exception:
            log.warning("could not post a task notice for %s", row["task_id"], exc_info=True)

    # -- control: approvals, steer, stop, resume, cancel ----------------------------------- #

    async def respond_approval(self, request_id: str, decision: str, scope: str, decided_by: str,
                               ) -> dict[str, Any]:
        await self.flush()
        appr = store.get_approval(request_id)
        if appr is None or appr["decision"] is not None:
            raise TaskError("approval_not_found", "That approval request is not pending any more.", 404)
        row = self._row(appr["task_id"])
        if row is None:
            raise TaskError("approval_not_found", "That approval request is not pending any more.", 404)
        if scope == "task" and "task" not in appr["scopes"]:
            raise TaskError("approval_scope_not_allowed", "That approval can only be given for this one call.", 422)
        be = self.backends.get(row["backend"])
        if be is None:
            raise TaskError("approval_not_found", "That approval request is not pending any more.", 404)
        try:
            await be.respond_approval(request_id, decision, scope)       # type: ignore[arg-type]
        except AgentBackendError as e:
            if e.code == ErrorCode.invalid_request:
                self._enqueue(store.op_approval_decide(request_id, "expired", None, None))
                raise TaskError("approval_not_found", "That approval request is not pending any more.", 404) from e
            raise
        self._enqueue(store.op_approval_decide(request_id, decision, scope, decided_by))
        key = "approvals_allowed" if decision == "allow" else "approvals_denied"
        row[key] = (row.get(key) or 0) + 1
        self._enqueue(store.op_task(row["task_id"], **{key: row[key]}))
        accounts.log_activity("agent_approval", profile_id=decided_by or None, subject=row["agent_name"],
                              detail=json.dumps({"task": row["task_id"], "tool": appr["tool"],
                                                 "decision": decision, "scope": scope}, separators=(",", ":")),
                              category="audit")
        done = {**appr, "decision": decision, "scope": scope, "decided_by": decided_by}
        await ws_frames.publish(ws_frames.approval_frame(row, done, decision), row["owner"])
        return public_approval({**done, "decided_at": store.now_iso()})

    def _backend_of(self, row: dict[str, Any]) -> BaseBackend:
        return self.backend_for(row["backend"])

    def _ensure_registered(self, row: dict[str, Any]) -> BaseBackend:
        """Make the backend know a task that only exists in the database (after a restart)."""
        be = self._backend_of(row)
        tid = row["task_id"]
        if not be.has_task(tid):
            spec = AgentSpec.model_validate_json(row["spec_json"])
            ctx = TaskContext(data_dir=self.data_dir, task_id=tid, owner=row["owner"] or None,
                              origin_thread_id=row["origin_thread_id"],
                              repo_path=Path(row["repo_path"]) if row["repo_path"] else None,
                              secrets=self.secrets)
            be.restore_task(tid, spec, ctx, session_id=row["session_id"], last_seq=row["last_seq"],
                            usd_so_far=row["cost_usd"] or 0.0, tokens_so_far=row["tokens"] or 0,
                            turns_so_far=row["turns"] or 0)
            self._cursor[tid] = max(self._cursor.get(tid, -1), row["last_seq"])
        return be

    def _refresh_budget(self, row: dict[str, Any], be: BaseBackend) -> None:
        spec = AgentSpec.model_validate_json(row["spec_json"])
        budget = self.effective_budget(spec, row["agent_id"], row["owner"], row["cost_usd"] or 0.0)
        be.update_budget(row["task_id"], budget, 0.0)

    async def interrupt(self, row: dict[str, Any]) -> None:
        be = self._ensure_registered(row)
        await be.interrupt(row["task_id"])

    async def send(self, row: dict[str, Any], message: str) -> str:
        """Steer a running task, or continue a finished one. Returns "steered" or "resumed"."""
        be = self._ensure_registered(row)
        tid = row["task_id"]
        active = be.is_active(tid)
        if not active:
            self._refresh_budget(row, be)
        await be.send(tid, message)
        if not active:
            self._ensure_pump(tid)
            return "resumed"
        return "steered"

    async def resume(self, row: dict[str, Any], message: str | None = None) -> None:
        be = self._ensure_registered(row)
        self._refresh_budget(row, be)
        await be.resume(row["task_id"], message)
        self._ensure_pump(row["task_id"])

    async def cancel(self, row: dict[str, Any]) -> None:
        tid = row["task_id"]
        if row["state"] == "queued" and not self._backend_has(row):
            # never reached a backend (waiting for a slot, or held for a human's Send)
            await self._cancel_unstarted(row)
            return
        be = self._ensure_registered(row)
        await be.cancel(tid)
        self._ensure_pump(tid)

    def _backend_has(self, row: dict[str, Any]) -> bool:
        be = self.backends.get(row["backend"])
        return bool(be is not None and be.has_task(row["task_id"]))

    async def _cancel_unstarted(self, row: dict[str, Any], *, reason: str = "cancelled") -> None:
        tid = row["task_id"]
        seq = self._cursor.get(tid, -1) + 1
        await self._handle(tid, ResultEvent(task_id=tid, ts=time.time(), seq=seq, state=TaskState.cancelled,
                                            reason=reason, resumable=False))

    async def release(self, row: dict[str, Any]) -> dict[str, Any]:
        """The human's Send for a task held in ``queued`` (an advisor handoff card)."""
        if row["hold"] != "send":
            raise TaskError("task_state_conflict", "The task cannot do that in its current state.")
        store.update_task(row["task_id"], hold=None)
        row["hold"] = None
        await ws_frames.publish(ws_frames.update_frame(row), row["owner"])
        await self._kick()
        return store.get_task(row["task_id"]) or row

    async def abort_chat(self, thread_id: str) -> bool:
        """Stop the unified chat turn running on a thread (the composer's Stop). True if one was."""
        be = self.backends.get("openclaw")
        if be is None or not be.is_active(thread_id):
            return False
        await be.interrupt(thread_id)
        return True

    async def wait_for_terminal(self, tid: str, timeout: float | None = None) -> ChatOutcome:
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._waiters[tid].append(fut)
        row = self._row(tid)
        if row is not None and row["state"] in ("succeeded", "failed", "cancelled") and not self._is_running(row):
            self._waiters[tid].remove(fut)
            return ChatOutcome(state=row["state"], reason=row["reason"], text=row["result_text"])
        return await asyncio.wait_for(fut, timeout)

    def _is_running(self, row: dict[str, Any]) -> bool:
        be = self.backends.get(row["backend"])
        return bool(be is not None and be.is_active(row["task_id"]))

    # -- recovery ---------------------------------------------------------------------- #

    async def restore(self) -> int:
        """Re-attach to tasks the last process left unfinished."""
        n = 0
        for row in store.nonterminal_tasks():
            tid = row["task_id"]
            if row["state"] == "queued":
                continue                                    # waiting for a slot or a human: still valid
            self._rows[tid] = row
            self._cursor[tid] = max(row["last_seq"], store.last_seq(tid))
            try:
                be = self._ensure_registered(row)
            except Exception as e:
                log.warning("task %s cannot be re-registered (%s)", tid, e)
                be = None
            seq = self._cursor[tid] + 1
            resumable = bool(row["session_id"]) and be is not None
            await self._handle(tid, ResultEvent(
                task_id=tid, ts=time.time(), seq=seq, state=TaskState.cancelled, reason="restart",
                session_id=row["session_id"], resumable=resumable))
            if be is not None and be.has_task(tid):
                # the backend continues numbering after OUR synthetic result, not on top of it
                be.forget_task(tid)
                self._ensure_registered(self._rows[tid])
            n += 1
        if n:
            log.info("agent tasks: %d task(s) were running when DisPatch stopped; they are now "
                     "cancelled (resumable when the backend kept a session)", n)
        return n

    # -- retention ------------------------------------------------------------------------ #

    async def purge_now(self) -> int:
        days = int(store.get_setting("agents.retention_days") or 0)
        await self.flush()
        ids = await asyncio.to_thread(store.purge, days)
        for tid in ids:
            await asyncio.to_thread(self._delete_workspace, tid)
            self._rows.pop(tid, None)
        if ids:
            log.info("agent tasks: purged the transcript of %d task(s) older than %d days", len(ids), days)
        return len(ids)

    def _delete_workspace(self, tid: str) -> None:
        try:
            ws = existing_workspace(self.data_dir, tid)
        except WorkspaceError:
            ws = None
        try:
            if ws is not None:
                cleanup_workspace(ws)
            base = self.data_dir / "agents" / tid
            if base.is_dir():
                shutil.rmtree(base, ignore_errors=True)
        except Exception:
            log.warning("could not remove the workspace of task %s", tid, exc_info=True)

    async def purge_loop(self, every_s: float = 6 * 3600) -> None:
        while True:
            with contextlib.suppress(Exception):
                await self.purge_now()
            await asyncio.sleep(every_s)

    # -- support bundle ----------------------------------------------------------------------- #

    def support_summary(self) -> dict[str, Any]:
        """No prompts, no instructions, no events, no secrets (docs/agents.md)."""
        out = store.redacted_summary()
        # Per backend: how many tasks it holds in memory and in which state - not the spec
        # (names, MCP addresses, seat ids) that the adapter's own support_dump() would add.
        backends: dict[str, Any] = {}
        for name, b in self.backends.items():
            tasks = (b.support_dump().get("tasks") or {}).values()
            states: dict[str, int] = {}
            for t in tasks:
                states[t["state"]] = states.get(t["state"], 0) + 1
            backends[name] = {"in_memory": sum(states.values()), "states": states}
        out["backends"] = backends
        return GLOBAL_REDACTOR.redact_obj(out)

    # -- unified dispatch: OpenClaw chat turns ----------------------------------------------------- #

    async def run_chat_turn(self, *, thread_id: str, agent_seat: str, text: str, model: str | None = None,
                            thinking: str | None = None, timeout_s: float | None = None,
                            owner: str = "") -> ChatOutcome:
        """One OpenClaw chat turn as a task (``agents.unified_dispatch`` on).

        A chat thread is a long-lived task whose id IS the thread id, so the OpenClaw session
        key is exactly the one the legacy path uses (``agent:<seat>:<thread>``). The first turn
        starts the task; later turns send a follow-up on the same session.
        """
        seat = agent_seat
        opts = {"agent_id": seat}
        if thinking:
            opts["thinking"] = thinking
        spec = AgentSpec(backend="openclaw", name=seat, model=model, backend_options=opts,
                         budget=Budget(max_wall_clock_s=timeout_s) if timeout_s else Budget())
        row = store.get_task(thread_id)
        be = self.backend_for("openclaw")
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._waiters[thread_id].append(fut)
        try:
            if row is None:
                row = store.create_task(
                    task_id=thread_id, agent_id=f"openclaw:{seat}", agent_name=seat, backend="openclaw",
                    owner=owner, kind="chat", state="queued", spec_json=spec.model_dump_json(),
                    prompt=None, origin_thread_id=thread_id)
                self._rows[thread_id] = row
                self._cursor.setdefault(thread_id, -1)
                ctx = TaskContext(data_dir=self.data_dir, task_id=thread_id, origin_thread_id=thread_id,
                                  secrets=self.secrets)
                await be.start_task(spec, text, ctx)
                self._ensure_pump(thread_id)
            else:
                self._rows[thread_id] = row
                be = self._ensure_registered(row)
                be.patch_spec(thread_id, model=model, backend_options=opts,
                              budget=spec.budget)
                if be.is_active(thread_id):
                    raise TaskError("task_state_conflict", "The task cannot do that in its current state.")
                if be.task_info(thread_id).resumable:
                    await be.send(thread_id, text)
                else:
                    # The last turn failed or was stopped for good, so the backend will not
                    # resume it. The session KEY is deterministic (agent:<seat>:<thread>), so
                    # starting the task again under the same id continues the same conversation.
                    be.forget_task(thread_id)
                    self._cursor[thread_id] = -1
                    self._cost_seen.pop(thread_id, None)
                    fresh = {"state": "queued", "reason": None, "finished_at": None, "last_seq": -1}
                    row.update(fresh)
                    store.update_task(thread_id, **fresh)
                    ctx = TaskContext(data_dir=self.data_dir, task_id=thread_id,
                                      origin_thread_id=thread_id, secrets=self.secrets)
                    await be.start_task(spec, text, ctx)
                self._ensure_pump(thread_id)
            return await asyncio.wait_for(fut, timeout=(timeout_s + 30) if timeout_s else None)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await be.interrupt(thread_id)
            raise
        except TimeoutError:
            with contextlib.suppress(Exception):
                await be.interrupt(thread_id)
            return ChatOutcome(state="failed", reason="timeout", error_code="timeout",
                               error_message="the agent did not answer in time")
        finally:
            with contextlib.suppress(ValueError):
                self._waiters[thread_id].remove(fut)

    # -- unified dispatch: advisor handoff cards ------------------------------------------------------ #

    def seat_agent(self, seat: str) -> dict[str, Any]:
        """A virtual agent row for an OpenClaw seat named in a ``[[handoff:seat|brief]]`` marker."""
        spec = AgentSpec(backend="openclaw", name=seat, backend_options={"agent_id": seat})
        return {"id": f"openclaw:{seat}", "name": seat, "spec": spec.model_dump(mode="json")}

    async def propose_handoff(self, req: dict[str, Any]) -> dict[str, Any] | None:
        """An advisor handoff card exists: mirror it as a task in ``queued`` + hold ``send``."""
        if not unified_dispatch_enabled():
            return None
        if store.task_by_advisor_request(req["id"]) is not None:
            return None
        agent = self.seat_agent(req["agent"])
        row = store.create_task(
            agent_id=agent["id"], agent_name=agent["name"], backend="openclaw", owner=_install_owner_key(),
            kind="handoff", state="queued", hold="send", spec_json=json.dumps(agent["spec"]),
            prompt=req.get("brief"), origin_thread_id=req.get("thread_id"),
            origin_message_id=req.get("card_message_id"), advisor_request_id=req["id"])
        self._rows[row["task_id"]] = row
        await ws_frames.publish(ws_frames.update_frame(row), row["owner"])
        return row

    async def dismiss_handoff(self, request_id: str) -> None:
        row = store.task_by_advisor_request(request_id)
        if row is not None and row["state"] == "queued":
            self._rows[row["task_id"]] = row
            await self._cancel_unstarted(row, reason="dismissed")

    async def run_handoff(self, request_id: str | None, seat: str, message: str, timeout_s: float,
                          *, thread_id: str | None = None, kind: str = "handoff") -> str:
        """The advisor's ``ask_agent`` seam, run as a visible task. Returns the agent's final text
        or raises the same family of errors the CLI path raises (the advisor handles them)."""
        row = store.task_by_advisor_request(request_id) if request_id else None
        agent = self.seat_agent(seat)
        if row is None:
            row = await self.dispatch(agent=agent, prompt=message, owner=_install_owner_key(),
                                      origin_thread_id=thread_id, kind=kind,
                                      advisor_request_id=request_id, post_card=False, hold="send")
        # The advisor owns the human's Send; by the time it asks us to run, it was pressed.
        spec = AgentSpec.model_validate_json(row["spec_json"])
        spec = spec.model_copy(update={"budget": Budget(max_wall_clock_s=timeout_s)})
        store.update_task(row["task_id"], hold=None, prompt=message, spec_json=spec.model_dump_json())
        row = store.get_task(row["task_id"]) or row
        self._rows[row["task_id"]] = row
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._waiters[row["task_id"]].append(fut)
        await self._kick()
        outcome: ChatOutcome = await asyncio.wait_for(fut, timeout=timeout_s + 60)
        if outcome.state != "succeeded":
            raise TaskError(outcome.error_code or "agent_backend_rejected",
                            outcome.error_message or outcome.reason or "the agent did not finish", 502)
        return outcome.text or ""


def _install_owner_key() -> str:
    """Handoff cards belong to the install's Owner (the advisor serves the owner)."""
    owner = accounts.get_owner()
    return owner.id if owner is not None else "owner"


def _gateway_configured() -> bool:
    try:
        from . import gateway_ws
        return bool(gateway_ws.gateway_token())
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Process-wide instance
# --------------------------------------------------------------------------- #

manager: TaskManager | None = None
_refs = 0
_hooks = Hooks()


async def _stop(task: asyncio.Task) -> None:
    """Cancel ``task`` and wait for it - unless it belongs to another event loop (a test that
    opens several clients runs each lifespan on its own loop), which cannot be awaited here."""
    if task.done():
        return
    task.cancel()
    if task.get_loop() is asyncio.get_running_loop():
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


def bind(hooks: Hooks) -> None:
    """main.py hands over its plumbing once, at import (see llm_api.bind)."""
    global _hooks
    _hooks = hooks
    if manager is not None:
        manager.hooks = hooks


def get_manager() -> TaskManager:
    if manager is None:
        raise RuntimeError("the agent task manager is not running")
    return manager


async def startup() -> TaskManager:
    """Start (or join) the process's task manager. Counted, because the test client runs the
    app's lifespan once per client; in production there is exactly one."""
    global manager, _refs
    _refs += 1
    if manager is not None:
        return manager
    store.reset_for_tests()
    mgr = TaskManager(config.DATA_DIR, hooks=_hooks)
    if (os.environ.get("DISPATCH_AGENTS_FAKE") or "").strip() in ("1", "true"):
        from .agents.fake import FakeBackend
        mgr.register_backend("fake", FakeBackend())
    manager = mgr
    await mgr.startup()
    return mgr


async def shutdown() -> None:
    global manager, _refs
    _refs = max(0, _refs - 1)
    if _refs > 0:
        return
    mgr, manager = manager, None
    if mgr is not None:
        await mgr.shutdown()


__all__ = [
    "BACKEND_LABELS",
    "TERMINAL_STATES",
    "ChatOutcome",
    "Hooks",
    "SecretError",
    "TaskError",
    "TaskManager",
    "args_summary",
    "bind",
    "get_manager",
    "manager",
    "public_agent",
    "public_approval",
    "public_task",
    "shutdown",
    "startup",
    "unified_dispatch_enabled",
]
