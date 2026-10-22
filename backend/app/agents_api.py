"""HTTP API for agents and tasks (3.0).

Three families of routes, each declared in ``route_policy.py`` and enforced by the gate
before a handler runs (the ``Depends`` below say the same sentence again at the handler):

``manage_agents``           define agents (``/api/agents``), pick what they may do, publish
                            them to profiles, store the Claude API key and MCP secrets, set
                            budgets and retention
``dispatch_agents``         use the agents published to you: ``/api/agents/available``,
                            ``/api/tasks`` and everything under it. A User or Basic profile
                            holds it only when an Admin switches it on (4.0 ``ROLE_GRANTABLE``;
                            never on the lock-target profile), so every route below answers
                            403 to anyone else - listing included
``approve_agent_actions``   answer an approval card (``/api/approvals/{id}``) and press the Send
                            on a held handoff (``/api/tasks/{id}/release``)

Who sees which task is one rule (``agents_ws.may_see_task``): a profile sees its OWN tasks -
the Owner and Admins included since 4.0 (they keep the aggregate spend, ``/api/agents/usage``).
A task you may not see answers 404, exactly like one that does not exist. An agent you may not
use answers 404 too.

Secrets: the Claude API key and MCP header secrets are WRITE-ONLY. No route returns one, no
event, log line or error carries one (``tests/agents/test_ag_no_secret_leaks.py``,
``tests/test_agents_api.py``).
"""

from __future__ import annotations

import logging
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import accounts, agents_service, characters, config, policy
from . import agents_store as store
from .agents.events import ErrorCode
from .agents.interface import AgentBackendError, AgentSpec
from .agents_service import BudgetExhausted, TaskError, public_agent, public_approval, public_task
from .agents_ws import may_see_task
from .contracts import errors
from .contracts.permissions import Permission, is_valid_permission

log = logging.getLogger("local-chat.agents.api")

router = APIRouter()

_MANAGE = Depends(policy.require_permission(Permission.MANAGE_AGENTS))
_DISPATCH = Depends(policy.require_permission(Permission.DISPATCH_AGENTS))
_APPROVE = Depends(policy.require_permission(Permission.APPROVE_AGENT_ACTIONS))

PROMPT_MAX = 20_000
MESSAGE_MAX = 8_000
INSTRUCTIONS_MAX = 20_000
STATES = store.STATES


# --------------------------------------------------------------------------- #
# Plumbing
# --------------------------------------------------------------------------- #


def _problem(status: int, code: str, message: str, **extra: Any) -> JSONResponse:
    body = {"detail": message, "error": errors.envelope(code, message=message), **extra}
    return JSONResponse(body, status_code=status)


def _principal(request: Request) -> policy.Principal:
    return policy.of(request).principal


def _mgr() -> agents_service.TaskManager:
    try:
        return agents_service.get_manager()
    except RuntimeError:
        raise HTTPException(503, "The agent backend is not available.") from None


def _task_error(e: TaskError) -> JSONResponse:
    return _problem(e.status, e.code, e.message)


def _backend_error(e: AgentBackendError) -> JSONResponse:
    if e.code in (ErrorCode.backend_unavailable, ErrorCode.sdk_missing, ErrorCode.auth_failed,
                  ErrorCode.backend_protocol_mismatch, ErrorCode.timeout):
        return _problem(503, "agent_backend_unavailable", e.message)
    if e.code == ErrorCode.budget_exceeded:
        return _problem(429, "agent_budget_exhausted", e.message)
    return _problem(409, "agent_backend_rejected", e.message)


def _validation_text(e: ValidationError) -> str:
    first = e.errors()[0]
    loc = ".".join(str(x) for x in first.get("loc", ()) if x != "__root__")
    msg = str(first.get("msg", "invalid")).removeprefix("Value error, ")
    return f"{loc}: {msg}" if loc else msg


def _published(agent: dict[str, Any], principal: policy.Principal) -> bool:
    pub = agent["published_to"]
    return "*" in pub or (bool(principal.profile_id) and principal.profile_id in pub)


def _usable_agent(principal: policy.Principal, agent_id: str) -> dict[str, Any]:
    """The agent row if ``principal`` may dispatch to it, else a 404 (never a hint that it exists)."""
    a = store.get_agent(agent_id)
    if a is None:
        raise _not_found_agent()
    if not principal.is_admin and (a["disabled"] or not _published(a, principal)):
        raise _not_found_agent()
    need = (a["spec"] or {}).get("required_permission") or Permission.DISPATCH_AGENTS.value
    if not principal.can(need):
        raise _not_found_agent()
    return a


def _not_found_agent() -> HTTPException:
    exc = HTTPException(404, "Agent not found")
    exc.code = "agent_not_found"                    # type: ignore[attr-defined]
    return exc


def _still_usable(principal: policy.Principal, row: dict[str, Any]) -> None:
    """Following up on or resuming a task is dispatching again: the agent must still be one this
    person may use (not unpublished, switched off, deleted or given a stricter permission since)."""
    if principal.is_admin:
        return
    a = store.get_agent(row["agent_id"])
    if a is None:
        raise _not_found_agent()
    _usable_agent(principal, a["id"])


def _task_for(principal: policy.Principal, task_id: str) -> dict[str, Any]:
    row = store.get_task(task_id)
    if row is None or row["kind"] == "chat" or not may_see_task(principal, row["owner"]):
        exc = HTTPException(404, "Task not found")
        exc.code = "task_not_found"                 # type: ignore[attr-defined]
        raise exc
    return row


# --------------------------------------------------------------------------- #
# Agent definitions
# --------------------------------------------------------------------------- #


class AgentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=400)
    spec: dict[str, Any]
    published_to: list[str] = Field(default_factory=list, max_length=200)
    disabled: bool = False


class AgentPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=400)
    spec: dict[str, Any] | None = None
    published_to: list[str] | None = Field(default=None, max_length=200)
    disabled: bool | None = None


def _is_local_url(url: str | None) -> bool:
    from urllib.parse import urlparse
    try:
        host = (urlparse(url or "").hostname or "").lower()
    except ValueError:
        return False
    return host in ("localhost", "127.0.0.1", "::1") or host.endswith(".localhost")


def owner_only_features(spec: AgentSpec) -> set[str]:
    """Powers that are host-level in effect: only the Owner (``admin_system``) may grant them.

    Each entry names the power AND what it is granted over (the stdio command line, the remote
    address a secret goes to, the tool run unattended), so that an edit is compared grant by
    grant. Comparing bare names let an Admin keep an Owner-approved ``stdio_mcp`` entry and
    rewrite its command - arbitrary code on the host - or move an approved remote server, with
    its stored secrets, to an address of their choosing."""
    import json as _json

    def _fp(*parts: object) -> str:
        return _json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str)

    out: set[str] = set()
    if spec.workspace_policy.trust_repo_settings:
        out.add("trust_repo_settings")
    for name, srv in spec.mcp_servers.items():
        if srv.transport == "stdio":
            out.add("stdio_mcp:" + _fp(name, srv.command, list(srv.args), dict(srv.env)))
        if srv.privileged:
            out.add("privileged_mcp:" + _fp(name, srv.transport, srv.url, srv.command, list(srv.args)))
        if srv.header_secrets and not _is_local_url(srv.url):
            # A stored secret is sent to this address: only to a server on this machine unless the
            # Owner says otherwise, or an Admin could point a stored key at a host of their choosing.
            out.add("secret_to_remote_server:" + _fp(name, srv.url, dict(srv.header_secrets)))
    for tool in spec.approval_policy.unattended_allow:
        out.add("unattended_allow:" + tool)
    return out


def _build_spec(raw: dict[str, Any], name: str) -> AgentSpec:
    data = dict(raw)
    data["name"] = name
    return AgentSpec.model_validate(data)


def _check_spec(spec: AgentSpec, principal: policy.Principal, old: AgentSpec | None) -> None:
    """Raises HTTPException-shaped TaskError for anything the schema alone cannot say."""
    mgr = agents_service.manager
    known = set(agents_service.KNOWN_BACKENDS) | (set(mgr.backends) if mgr else set())
    if spec.backend not in known:
        raise TaskError("agent_invalid", f"backend: unknown backend {spec.backend!r}", 422)
    if not is_valid_permission(spec.required_permission):
        raise TaskError("agent_invalid", f"required_permission: unknown permission {spec.required_permission!r}", 422)
    if len(spec.instructions) > INSTRUCTIONS_MAX:
        raise TaskError("agent_invalid", f"instructions: longer than {INSTRUCTIONS_MAX} characters", 422)
    if len(spec.tool_allowlist) > 100 or any(len(t) > 120 or not t.strip() for t in spec.tool_allowlist):
        raise TaskError("agent_invalid", "tool_allowlist: at most 100 non-empty entries of 120 characters", 422)
    for name, srv in spec.mcp_servers.items():
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", name):
            raise TaskError("agent_invalid", f"mcp_servers: {name!r} is not a valid server name", 422)
        for secret in srv.header_secrets.values():
            if not agents_service.mcp_secret_ok(secret):
                raise TaskError(
                    "agent_invalid",
                    f"mcp_servers.{name}.header_secrets: a secret name must start with MCP_ or "
                    "STUDIOFORGE_ (the Anthropic key and the gateway token are never sent to a server)", 422)
        if srv.url and not re.match(r"https?://", srv.url, re.I):
            raise TaskError("agent_invalid", f"mcp_servers.{name}.url: must be http or https", 422)
    if spec.backend == "openclaw":
        seat = str(spec.backend_options.get("agent_id") or spec.name)
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", seat):
            raise TaskError("agent_invalid", "backend_options.agent_id: letters, digits, '.', '_' and '-' only", 422)
        try:
            from . import advisor
            if seat.strip().lower() in advisor.safe_agent_ids():
                raise TaskError("agent_invalid", f"backend_options.agent_id: {seat} is a character that "
                                "Restricted profiles can chat with; a task is never handed to one", 422)
        except ImportError:                                   # pragma: no cover
            pass
    new_owner_only = owner_only_features(spec) - (owner_only_features(old) if old else set())
    if new_owner_only and not principal.can(Permission.ADMIN_SYSTEM):
        raise TaskError("agent_needs_owner",
                        "Only the Owner or an Admin can give an agent that capability.", 403)


def _clean_published(ids: list[str]) -> list[str]:
    out: list[str] = []
    for i in ids:
        i = str(i).strip()
        if i == "*" or (i and accounts.get_profile(i) is not None):
            if i not in out:
                out.append(i)
        else:
            raise TaskError("agent_invalid", f"published_to: {i!r} is not a profile", 422)
    return out


@router.get("/api/agents")
async def list_agents(request: Request, _=_MANAGE):
    mgr = _mgr()
    profiles = [{"id": p.id, "name": p.name, "role": p.role} for p in accounts.list_profiles()
                if not p.disabled]
    return {"agents": [public_agent(a) for a in store.list_agents()], "profiles": profiles,
            "backends": [{"name": b["name"], "label": b["label"], "available": b["available"]}
                         for b in mgr.backend_info()]}


@router.post("/api/agents", status_code=201)
async def create_agent(body: AgentIn, request: Request, _=_MANAGE):
    pr = _principal(request)
    try:
        spec = _build_spec(body.spec, body.name.strip())
        _check_spec(spec, pr, None)
        published = _clean_published(body.published_to)
    except ValidationError as e:
        return _problem(422, "agent_invalid", _validation_text(e))
    except TaskError as e:
        return _task_error(e)
    if store.agent_by_name(body.name.strip()) is not None:
        return _problem(409, "agent_name_taken", "An agent with that name already exists.")
    row = store.create_agent(name=body.name.strip(), description=body.description.strip(),
                             spec=spec.model_dump(mode="json"), published_to=published,
                             created_by=agents_service.principal_key(pr), disabled=body.disabled)
    accounts.log_activity("agent_created", profile_id=pr.profile_id, subject=row["name"],
                          detail=f"backend={spec.backend}", category="audit")
    return public_agent(row)


@router.patch("/api/agents/{agent_id}")
async def patch_agent(agent_id: str, body: AgentPatch, request: Request, _=_MANAGE):
    pr = _principal(request)
    cur = store.get_agent(agent_id)
    if cur is None:
        raise _not_found_agent()
    name = (body.name or cur["name"]).strip()
    fields: dict[str, Any] = {}
    try:
        if body.spec is not None or body.name is not None:
            raw = body.spec if body.spec is not None else cur["spec"]
            spec = _build_spec(raw, name)
            old = AgentSpec.model_validate(cur["spec"])
            _check_spec(spec, pr, old)
            fields["spec"] = spec.model_dump(mode="json")
        if body.published_to is not None:
            fields["published_to"] = _clean_published(body.published_to)
    except ValidationError as e:
        return _problem(422, "agent_invalid", _validation_text(e))
    except TaskError as e:
        return _task_error(e)
    if body.name is not None:
        other = store.agent_by_name(name)
        if other is not None and other["id"] != agent_id:
            return _problem(409, "agent_name_taken", "An agent with that name already exists.")
        fields["name"] = name
    if body.description is not None:
        fields["description"] = body.description.strip()
    if body.disabled is not None:
        fields["disabled"] = body.disabled
    row = store.update_agent(agent_id, **fields)
    accounts.log_activity("agent_changed", profile_id=pr.profile_id, subject=name,
                          detail=",".join(sorted(fields)), category="audit")
    return public_agent(row)  # type: ignore[arg-type]


@router.delete("/api/agents/{agent_id}")
async def delete_agent(agent_id: str, request: Request, _=_MANAGE):
    pr = _principal(request)
    cur = store.get_agent(agent_id)
    if cur is None:
        raise _not_found_agent()
    if store.list_tasks(agent_id=agent_id, states=["queued", "running", "awaiting_approval"], limit=1):
        return _problem(409, "agent_has_active_tasks", "That agent has tasks that are still running.")
    store.delete_agent(agent_id)
    accounts.log_activity("agent_deleted", profile_id=pr.profile_id, subject=cur["name"], category="audit")
    return {"ok": True}


@router.get("/api/agents/available")
async def available_agents(request: Request, _=_DISPATCH):
    pr = _principal(request)
    mgr = _mgr()
    leaves = {b["name"]: bool((b["capabilities"] or {}).get("data_leaves_host", True))
              for b in mgr.backend_info()}
    out = []
    for a in store.list_agents():
        if not pr.is_admin and (a["disabled"] or not _published(a, pr)):
            continue
        need = (a["spec"] or {}).get("required_permission") or Permission.DISPATCH_AGENTS.value
        if not pr.can(need):
            continue
        spec_backend = (a["spec"] or {}).get("backend") or ""
        problem = mgr.backend_problem(spec_backend)
        entry = public_agent(a, full=False)
        entry.pop("published_to", None)            # who else may use it is the managers' business
        entry["data_leaves_host"] = leaves.get(spec_backend, True)
        entry["available"] = problem is None and not a["disabled"]
        entry["problem"] = None if problem is None else {"code": problem[0].value, "message": problem[1]}
        out.append(entry)
    return {"agents": out}


@router.get("/api/agents/backends")
async def list_backends(request: Request, _=_MANAGE):
    return {"backends": _mgr().backend_info()}


# --------------------------------------------------------------------------- #
# Credentials and secrets (write-only)
# --------------------------------------------------------------------------- #


class AgentClaudeCredentialsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    route: Literal["api_key", "bedrock", "google", "foundry"] = "api_key"
    api_key: str | None = Field(default=None, max_length=512, repr=False)


def _valid_api_key(key: str) -> str | None:
    """None if fine, else why not. Subscription OAuth tokens are refused by name (RESEARCH.md
    section 5 items 1-2): a third-party product may not use them."""
    k = key.strip()
    if re.search(r"\s", k):
        return "An API key has no spaces or line breaks."
    if k.startswith("sk-ant-oat"):
        return "That is a subscription login token, which this product cannot use. Paste an API key from the Anthropic Console."
    if not k.startswith("sk-ant-") or len(k) < 24:
        return "That does not look like an Anthropic Console API key (it starts with sk-ant-)."
    return None


@router.get("/api/agents/credentials/claude")
async def claude_credentials(request: Request, _=_MANAGE):
    return _mgr().credential_status()


@router.put("/api/agents/credentials/claude")
async def put_claude_credentials(body: AgentClaudeCredentialsIn, request: Request, _=_MANAGE):
    mgr = _mgr()
    pr = _principal(request)
    if body.api_key:
        why = _valid_api_key(body.api_key)
        if why:
            return _problem(422, "agent_credentials_invalid", "The credentials were not accepted.", hint=why)
    try:
        if body.api_key and body.route == "api_key":
            mgr.set_claude_key(body.api_key.strip())
        mgr.set_claude_route(body.route)
    except TaskError as e:
        return _task_error(e)
    except Exception:
        log.warning("could not store the Claude credentials", exc_info=False)
        return _problem(422, "agent_credentials_invalid", "The credentials were not accepted.",
                        hint="No place to keep the key: set ANTHROPIC_API_KEY in the service environment instead.")
    accounts.log_activity("agent_credentials", profile_id=pr.profile_id, subject="claude",
                          detail=f"route={body.route}", category="audit")
    return mgr.credential_status()


@router.delete("/api/agents/credentials/claude")
async def delete_claude_key(request: Request, _=_MANAGE):
    mgr = _mgr()
    mgr.clear_claude_key()
    accounts.log_activity("agent_credentials", profile_id=_principal(request).profile_id,
                          subject="claude", detail="key removed", category="audit")
    return mgr.credential_status()


class AgentSecretIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str = Field(min_length=1, max_length=4096, repr=False)


@router.get("/api/agents/secrets")
async def list_secrets(request: Request, _=_MANAGE):
    """The secret NAMES agents refer to, and whether each is set. Never a value."""
    mgr = _mgr()
    names: set[str] = set()
    for a in store.list_agents():
        for srv in ((a["spec"] or {}).get("mcp_servers") or {}).values():
            names.update((srv.get("header_secrets") or {}).values())
    return {"secrets": [{"name": n, "set": bool(mgr.secrets.get(n))} for n in sorted(names)]}


@router.put("/api/agents/secrets/{name}")
async def put_secret(name: str, body: AgentSecretIn, request: Request, _=_MANAGE):
    if not agents_service.mcp_secret_ok(name):
        return _problem(422, "agent_invalid", "A secret name must start with MCP_ or STUDIOFORGE_ "
                        "(capital letters, digits and underscores).")
    mgr = _mgr()
    try:
        mgr.secrets.set(name, body.value)
        agents_service.GLOBAL_REDACTOR.register(body.value)
    except Exception:
        return _problem(422, "agent_credentials_invalid", "The credentials were not accepted.")
    accounts.log_activity("agent_secret", profile_id=_principal(request).profile_id, subject=name,
                          detail="set", category="audit")
    return {"name": name, "set": True}


@router.delete("/api/agents/secrets/{name}")
async def delete_secret(name: str, request: Request, _=_MANAGE):
    if not agents_service.mcp_secret_ok(name):
        return _problem(422, "agent_invalid", "A secret name must start with MCP_ or STUDIOFORGE_ "
                        "(capital letters, digits and underscores).")
    _mgr().secrets.delete(name)
    return {"name": name, "set": False}


# --------------------------------------------------------------------------- #
# Settings, budgets, usage, retention
# --------------------------------------------------------------------------- #


class AgentSettingsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    unified_dispatch: bool | None = None
    retention_days: int | None = Field(default=None, ge=0, le=3650)
    max_concurrent: int | None = Field(default=None, ge=1, le=16)


def _settings_view() -> dict[str, Any]:
    s = store.all_settings()
    return {"unified_dispatch": agents_service.unified_dispatch_enabled(),
            "unified_dispatch_from_env": bool((config.env("AGENTS_UNIFIED_DISPATCH", "") or "").strip()),
            "retention_days": int(s.get("agents.retention_days") or 0),
            "max_concurrent": int(s.get("agents.max_concurrent") or 4)}


@router.get("/api/agents/settings")
async def get_settings(request: Request, _=_MANAGE):
    return _settings_view()


@router.put("/api/agents/settings")
async def put_settings(body: AgentSettingsIn, request: Request, _=_MANAGE):
    pr = _principal(request)
    if body.unified_dispatch is not None:
        store.set_setting("agents.unified_dispatch", body.unified_dispatch)
    if body.retention_days is not None:
        store.set_setting("agents.retention_days", body.retention_days)
    if body.max_concurrent is not None:
        store.set_setting("agents.max_concurrent", body.max_concurrent)
    accounts.log_activity("agent_settings", profile_id=pr.profile_id, subject="agents",
                          detail=",".join(sorted(k for k, v in body.model_dump().items() if v is not None)),
                          category="audit")
    return _settings_view()


@router.get("/api/agents/usage")
async def usage(request: Request, _=_MANAGE):
    return {"day": store.today(), "estimate": True, "spend": store.spend_for_day()}


@router.post("/api/agents/retention/purge")
async def purge_now(request: Request, _=_MANAGE):
    n = await _mgr().purge_now()
    return {"purged": n}


# --------------------------------------------------------------------------- #
# Tasks
# --------------------------------------------------------------------------- #


class AgentTaskIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str = Field(min_length=1, max_length=80)
    prompt: str = Field(min_length=1, max_length=PROMPT_MAX)
    origin_thread_id: str | None = Field(default=None, max_length=120)
    origin_message_id: str | None = Field(default=None, max_length=120)
    repo_path: str | None = Field(default=None, max_length=1024)


class AgentTaskMessageIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=MESSAGE_MAX)


class AgentTaskResumeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str | None = Field(default=None, max_length=MESSAGE_MAX)


class AgentApprovalIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["allow", "deny"]
    scope: Literal["once", "task"] = "once"


@router.post("/api/tasks", status_code=201)
async def create_task(body: AgentTaskIn, request: Request, _=_DISPATCH):
    pr = _principal(request)
    mgr = _mgr()
    agent = _usable_agent(pr, body.agent_id)
    if agent["disabled"]:
        return _problem(409, "agent_disabled", "That agent is switched off.")
    repo = None
    if body.repo_path:
        if not pr.can(Permission.ADMIN_SYSTEM):
            return _problem(403, "agent_needs_owner", "Only the Owner or an Admin can give an agent that capability.")
        p = Path(body.repo_path).expanduser()
        if not p.is_dir():
            return _problem(422, "agent_invalid", "repo_path: that folder does not exist on this machine")
        repo = str(p)
    thread_id = body.origin_thread_id
    if thread_id:
        from . import ownership
        info = await mgr.hooks.thread_info(thread_id) if mgr.hooks.thread_info else None
        # 4.0: a task posts into its origin chat, so that chat must be the dispatcher's own.
        if info is not None and not ownership.may_see_thread(
                pr, info.get("owner_profile_id"), write=True):
            raise HTTPException(404, "Thread not found")
        # characters.may_use (4.0): the character must be one the dispatcher may use - the
        # same rule for every role, Admins included.
        if info is None or not characters.may_use_id(info.get("bot_id"), pr):
            raise HTTPException(404, "Thread not found")
        # (3.x refused a chat a Restricted profile could read. 4.0: what the agent is asked and
        # answers lands in the dispatcher's OWN chat - the ownership check above - and the shared
        # default profile can never dispatch: the lock-target fence removes dispatch_agents.)
    try:
        row = await mgr.dispatch(agent=agent, prompt=body.prompt, owner=agents_service.principal_key(pr),
                                 origin_thread_id=thread_id, origin_message_id=body.origin_message_id,
                                 repo_path=repo)
    except BudgetExhausted as e:
        return _problem(429, "agent_budget_exhausted", e.message)
    except AgentBackendError as e:
        return _backend_error(e)
    except ValidationError as e:
        return _problem(422, "agent_invalid", _validation_text(e))
    accounts.log_activity("agent_task_started", profile_id=pr.profile_id, subject=agent["name"],
                          detail=f"task={row['task_id']}", category="audit")
    return {"task": public_task(row, pending=[])}


@router.get("/api/tasks")
async def list_tasks(request: Request, state: str | None = None, agent_id: str | None = None,
                     profile: str | None = None, since: str | None = None, until: str | None = None,
                     thread_id: str | None = None, limit: int = 100, offset: int = 0, _=_DISPATCH):
    pr = _principal(request)
    states = [s.strip() for s in (state or "").split(",") if s.strip()]
    if any(s not in STATES for s in states):
        return _problem(422, "validation_error", "state: one of " + ", ".join(STATES))
    for label, v in (("since", since), ("until", until)):
        if v:
            try:
                date.fromisoformat(v[:10])
            except ValueError:
                return _problem(422, "validation_error", f"{label}: use YYYY-MM-DD")
    # 4.0: everyone is pinned to their own tasks, the Owner and Admins included (``profile`` is
    # accepted for old clients and ignored). An open install (no accounts) sees all.
    owner: str | None
    if pr.kind == "open":
        owner = profile or None
    else:
        owner = agents_service.principal_key(pr)
    mgr = _mgr()
    await mgr.flush()
    rows = store.list_tasks(owner=owner, states=states or None, agent_id=agent_id,
                            since=since[:10] if since else None,
                            until=_next_day(until) if until else None, thread_id=thread_id,
                            limit=limit, offset=offset)
    counts = store.pending_approval_count([r["task_id"] for r in rows])
    out = []
    for r in rows:
        t = public_task(r, with_text=False)
        t["pending_approval_count"] = counts.get(r["task_id"], 0)
        out.append(t)
    return {"tasks": out, "limit": max(1, min(int(limit), 500)), "offset": max(0, int(offset))}


def _next_day(day: str) -> str:
    return (date.fromisoformat(day[:10]) + timedelta(days=1)).isoformat()


@router.get("/api/tasks/{task_id}")
async def get_task(task_id: str, request: Request, _=_DISPATCH):
    pr = _principal(request)
    await _mgr().flush()
    row = _task_for(pr, task_id)
    approvals = [public_approval(a) for a in store.list_approvals(task_id)]
    pending = [a for a in approvals if a["state"] == "pending"]
    return {"task": public_task(row, pending=pending), "approvals": approvals}


@router.get("/api/tasks/{task_id}/events")
async def get_events(task_id: str, request: Request, after_seq: int = -1, limit: int = 2000,
                     _=_DISPATCH):
    pr = _principal(request)
    await _mgr().flush()
    row = _task_for(pr, task_id)
    events = store.list_events(task_id, after_seq, max(1, min(int(limit), 5000)))
    return {"events": events, "last_seq": row["last_seq"], "state": row["state"],
            "purged": bool(row["purged"])}


def _running(row: dict[str, Any]) -> bool:
    return row["state"] in ("running", "awaiting_approval")


@router.post("/api/tasks/{task_id}/send", status_code=202)
async def send_to_task(task_id: str, body: AgentTaskMessageIn, request: Request, _=_DISPATCH):
    pr = _principal(request)
    row = _task_for(pr, task_id)
    if row["hold"] or row["state"] == "queued":
        return _problem(409, "task_state_conflict", "The task cannot do that in its current state.")
    if not _running(row) and not row["resumable"]:
        return _problem(409, "task_state_conflict", "The task cannot do that in its current state.")
    _still_usable(pr, row)
    try:
        mode = await _mgr().send(row, body.message)
    except BudgetExhausted as e:
        return _problem(429, "agent_budget_exhausted", e.message)
    except AgentBackendError as e:
        return _backend_error(e)
    return {"ok": True, "mode": mode}


@router.post("/api/tasks/{task_id}/interrupt", status_code=202)
async def interrupt_task(task_id: str, request: Request, _=_DISPATCH):
    row = _task_for(_principal(request), task_id)
    if not _running(row):
        return _problem(409, "task_state_conflict", "The task cannot do that in its current state.")
    try:
        await _mgr().interrupt(row)
    except AgentBackendError as e:
        return _backend_error(e)
    return {"ok": True}


@router.post("/api/tasks/{task_id}/resume", status_code=202)
async def resume_task(task_id: str, body: AgentTaskResumeIn, request: Request, _=_DISPATCH):
    pr = _principal(request)
    row = _task_for(pr, task_id)
    if _running(row) or row["hold"] or not row["resumable"]:
        return _problem(409, "task_state_conflict", "The task cannot do that in its current state.")
    _still_usable(pr, row)
    try:
        await _mgr().resume(row, body.message)
    except BudgetExhausted as e:
        return _problem(429, "agent_budget_exhausted", e.message)
    except AgentBackendError as e:
        return _backend_error(e)
    return {"ok": True}


@router.post("/api/tasks/{task_id}/cancel", status_code=202)
async def cancel_task(task_id: str, request: Request, _=_DISPATCH):
    row = _task_for(_principal(request), task_id)
    if row["state"] in ("failed", "cancelled") or (row["state"] == "succeeded" and not row["resumable"]):
        return _problem(409, "task_state_conflict", "The task cannot do that in its current state.")
    try:
        await _mgr().cancel(row)
    except AgentBackendError as e:
        return _backend_error(e)
    return {"ok": True}


@router.post("/api/tasks/{task_id}/release", status_code=202)
async def release_task(task_id: str, request: Request, _=_APPROVE):
    """The human's Send for a task held in ``queued`` (a handoff card)."""
    pr = _principal(request)
    row = _task_for(pr, task_id)
    if row["hold"] != "send":
        return _problem(409, "task_state_conflict", "The task cannot do that in its current state.")
    if row["advisor_request_id"]:
        from . import advisor
        try:
            await advisor.send_handoff(row["advisor_request_id"],
                                       message_id=row["origin_message_id"] or "")
        except (KeyError, ValueError) as e:
            return _problem(409, "task_state_conflict", "The task cannot do that in its current state.",
                            hint=str(e))
        accounts.log_activity("agent_task_sent", profile_id=pr.profile_id, subject=row["agent_name"],
                              detail=f"task={task_id}", category="audit")
        return {"ok": True}
    try:
        await _mgr().release(row)
    except TaskError as e:
        return _task_error(e)
    return {"ok": True}


@router.post("/api/tasks/{task_id}/dismiss", status_code=202)
async def dismiss_task(task_id: str, request: Request, _=_APPROVE):
    row = _task_for(_principal(request), task_id)
    if row["hold"] != "send":
        return _problem(409, "task_state_conflict", "The task cannot do that in its current state.")
    if row["advisor_request_id"]:
        from . import advisor
        try:
            await advisor.dismiss_handoff(row["advisor_request_id"], message_id=row["origin_message_id"] or "")
        except (KeyError, ValueError) as e:
            return _problem(409, "task_state_conflict", "The task cannot do that in its current state.",
                            hint=str(e))
    else:
        await _mgr().cancel(row)
    return {"ok": True}


@router.post("/api/approvals/{request_id}")
async def answer_approval(request_id: str, body: AgentApprovalIn, request: Request, _=_APPROVE):
    pr = _principal(request)
    mgr = _mgr()
    await mgr.flush()
    appr = store.get_approval(request_id)
    if appr is None:
        return _problem(404, "approval_not_found", "That approval request is not pending any more.")
    _task_for(pr, appr["task_id"])                     # 404 when the task is not yours to see
    try:
        done = await mgr.respond_approval(request_id, body.decision, body.scope,
                                          agents_service.principal_key(pr))
    except TaskError as e:
        return _task_error(e)
    except AgentBackendError as e:
        return _backend_error(e)
    return {"ok": True, "approval": done}
