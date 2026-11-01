"""4.3 (#9, #12): Claude Agent provider routes and the availability message/order.

Nothing here touches a real cloud: every route is checked against a FAKE environment
(the cloud routes are unverified live, see ~/.cache/cfp/42/i43/notes-a7.md).
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest
from agents_world import aw

from app import agents_service
from app.agents import claude_sdk_adapter as adapter
from app.agents.budget import Budget
from app.agents.claude_sdk_adapter import (
    NEVER_FORWARD,
    ROUTE_ENV,
    ClaudeLaunchPlan,
    build_launch_plan,
    check_route_env,
    run_worker,
)
from app.agents.events import ErrorCode
from app.agents.interface import AgentBackendError, AgentSpec, TaskContext
from app.agents.secrets import MemorySecretProvider
from app.agents.workspace import prepare_workspace


def _agent_fakes():
    """tests/agents/agent_fakes.py (on sys.path only when that suite is collected)."""
    mod = sys.modules.get("agent_fakes")
    if mod is None:
        spec_ = importlib.util.spec_from_file_location(
            "agent_fakes", Path(__file__).parent / "agents" / "agent_fakes.py")
        mod = importlib.util.module_from_spec(spec_)
        sys.modules["agent_fakes"] = mod
        spec_.loader.exec_module(mod)
    return mod


FAKE_KEY = "sk-ant-" + "api03-" + "x" * 30

BEDROCK_OK = {"AWS_REGION": "eu-west-1", "AWS_ACCESS_KEY_ID": "AKIAFAKEFAKEFAKE",
              "AWS_SECRET_ACCESS_KEY": "fake-secret-0123456789"}


def spec() -> AgentSpec:
    return AgentSpec(backend="claude_sdk", name="a", tool_allowlist=["Read"],
                     budget=Budget(max_usd=1.0, max_turns=4))


@pytest.fixture
def ws(tmp_path):
    d = tmp_path / "agentdata"
    d.mkdir()
    return d, prepare_workspace(d, "route-1")


# ---- (c) a route checks its credentials before start and forwards them ------------------- #

def test_bedrock_without_a_region_fails_auth_naming_the_variables(ws):
    data, w = ws
    env = {k: v for k, v in BEDROCK_OK.items() if k != "AWS_REGION"}
    with pytest.raises(AgentBackendError) as ei:
        build_launch_plan(spec(), TaskContext(data_dir=data), w, api_key=None, auth_route="bedrock",
                          route_env=env)
    assert ei.value.code == ErrorCode.auth_failed
    assert "AWS_REGION" in ei.value.message and "AWS_DEFAULT_REGION" in ei.value.message
    assert "fake-secret" not in ei.value.message, "names only, never a value"


def test_bedrock_without_any_credential_lists_the_alternatives():
    c = check_route_env("bedrock", {"AWS_REGION": "eu-west-1"})
    assert not c.available and len(c.missing) == 1
    assert "AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY" in c.missing[0]
    assert "AWS_BEARER_TOKEN_BEDROCK" in c.missing[0]


async def test_bedrock_env_is_forwarded_into_the_worker_env(ws):
    data, w = ws
    p = build_launch_plan(spec(), TaskContext(data_dir=data), w, api_key=None, auth_route="bedrock",
                          route_env=BEDROCK_OK)
    assert p.options["env"]["CLAUDE_CODE_USE_BEDROCK"] == "1"
    assert p.secret_env == BEDROCK_OK, "over the stdin pipe, not in the loggable options"
    assert not any(v in str(p.describe()) for v in BEDROCK_OK.values() if len(v) > 10)
    # ... and the worker merges them into the SDK's env.
    fakes = _agent_fakes()
    ClaudeSDKClient, fake_sdk_module = fakes.ClaudeSDKClient, fakes.fake_sdk_module
    inbox: asyncio.Queue = asyncio.Queue()
    out: list[dict] = []
    ClaudeSDKClient.instances = []
    t = asyncio.create_task(run_worker(inbox, out.append, fake_sdk_module()))
    inbox.put_nowait(p.to_wire())
    await asyncio.sleep(0.05)
    inbox.put_nowait(None)
    await asyncio.wait_for(t, 5)
    env = ClaudeSDKClient.instances[0].options.env
    assert {k: env[k] for k in BEDROCK_OK} == BEDROCK_OK
    assert env["CLAUDE_CODE_USE_BEDROCK"] == "1"


@pytest.mark.parametrize("route,env", [
    ("vertex", {"ANTHROPIC_VERTEX_PROJECT_ID": "p1", "GOOGLE_APPLICATION_CREDENTIALS": "/data/gcp.json",
                "CLOUD_ML_REGION": "us-east5", "VERTEX_REGION_CLAUDE_HAIKU_4_5": "us-central1"}),
    ("foundry", {"ANTHROPIC_FOUNDRY_RESOURCE": "res", "ANTHROPIC_FOUNDRY_API_KEY": "fake-foundry-key-123"}),
])
def test_vertex_and_foundry_forward_their_names(ws, route, env):
    data, w = ws
    p = build_launch_plan(spec(), TaskContext(data_dir=data), w, api_key=None, auth_route=route, route_env=env)
    assert p.secret_env == env
    with pytest.raises(AgentBackendError) as ei:
        build_launch_plan(spec(), TaskContext(data_dir=data), w, api_key=None, auth_route=route, route_env={})
    assert ei.value.code == ErrorCode.auth_failed


# ---- (g) only allow-listed names, never the Anthropic key or DisPatch's own ---------------- #

def test_the_forwarded_env_holds_only_allow_listed_names(ws):
    data, w = ws
    planted = {**BEDROCK_OK, "ANTHROPIC_API_KEY": "sk-ant-planted-0123456789", "DISPATCH_API_TOKEN": "x" * 20,
               "DISPATCH_DATA_DIR": "/data", "GITHUB_TOKEN": "ghp_planted", "HOME": "/root",
               "CLAUDE_CODE_OAUTH_TOKEN": "oauth-planted-12345", "VERTEX_REGION_CLAUDE_X": "nope"}
    p = build_launch_plan(spec(), TaskContext(data_dir=data), w, api_key=None, auth_route="bedrock",
                          route_env=planted)
    assert set(p.secret_env) == set(BEDROCK_OK)
    assert "sk-ant-planted" not in str(p.to_wire()["options"]) + str(p.secret_env)
    for route in ("bedrock", "vertex", "foundry"):
        fwd = check_route_env(route, planted).forward
        assert not any(k.startswith("DISPATCH_") or k in NEVER_FORWARD for k in fwd), (route, fwd)


def test_no_route_spec_names_a_forbidden_variable():
    for route, s in ROUTE_ENV.items():
        assert "ANTHROPIC_API_KEY" not in s.forward, route
        assert not any(n.startswith("DISPATCH_") for n in s.forward), route


def test_the_api_key_route_forwards_nothing_from_the_environment(ws):
    data, w = ws
    p = build_launch_plan(spec(), TaskContext(data_dir=data), w, api_key=FAKE_KEY,
                          route_env=BEDROCK_OK)
    assert set(p.secret_env) == {"ANTHROPIC_API_KEY"}


def test_empty_values_count_as_missing():
    assert not check_route_env("bedrock", {**BEDROCK_OK, "AWS_REGION": ""}).available


def test_secret_shaped_forwarded_names_are_redacted_but_a_region_is_not():
    assert adapter._SECRETISH.search("AWS_SECRET_ACCESS_KEY") and adapter._SECRETISH.search("AWS_ACCESS_KEY_ID")
    assert adapter._SECRETISH.search("ANTHROPIC_FOUNDRY_API_KEY")
    assert adapter._SECRETISH.search("AWS_BEARER_TOKEN_BEDROCK")
    assert not adapter._SECRETISH.search("AWS_REGION")
    assert isinstance(ClaudeLaunchPlan(options={}).policy, dict)


# ---- (d) availability: the key first, then the SDK; in a container, the rebuild hint ------- #

@pytest.fixture
def mgr(tmp_path, monkeypatch):
    for k in ("ANTHROPIC_API_KEY", *BEDROCK_OK, "AWS_DEFAULT_REGION"):
        monkeypatch.delenv(k, raising=False)
    m = agents_service.TaskManager(tmp_path, secrets=MemorySecretProvider())
    monkeypatch.setattr(agents_service.importlib.util, "find_spec",
                        lambda name, *a: None if name == "claude_agent_sdk" else __import__("importlib").util.find_spec(name, *a))
    monkeypatch.setattr(m, "claude_route", lambda: route_box[0])
    route_box = ["api_key"]
    m._route_box = route_box
    return m


def test_no_key_and_no_sdk_reports_the_key_first(mgr, monkeypatch):
    monkeypatch.setattr("app.connections.in_container", lambda: False)
    code, msg = mgr.backend_problem("claude_sdk")
    assert code == ErrorCode.auth_failed and "API key" in msg


def test_with_a_key_the_sdk_message_follows(mgr, monkeypatch):
    monkeypatch.setattr("app.connections.in_container", lambda: False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-" + "k" * 30)
    code, msg = mgr.backend_problem("claude_sdk")
    assert code == ErrorCode.sdk_missing and "uv sync --extra agents-claude" in msg


def test_in_a_container_the_message_says_rebuild(mgr, monkeypatch):
    monkeypatch.setattr("app.connections.in_container", lambda: True)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-" + "k" * 30)
    code, msg = mgr.backend_problem("claude_sdk")
    assert code == ErrorCode.sdk_missing and "--build-arg AGENTS_CLAUDE=1" in msg


def test_a_cloud_route_without_its_env_is_auth_failed_before_the_sdk(mgr, monkeypatch):
    mgr._route_box[0] = "bedrock"
    code, msg = mgr.backend_problem("claude_sdk")
    assert code == ErrorCode.auth_failed and "AWS_REGION" in msg
    st = mgr.credential_status()
    assert st["route_available"] is False and st["route_missing"]
    for k, v in BEDROCK_OK.items():
        monkeypatch.setenv(k, v)
    assert mgr.credential_status()["route_available"] is True
    assert mgr.backend_problem("claude_sdk")[0] == ErrorCode.sdk_missing
    assert "fake-secret" not in str(mgr.credential_status())


# ---- GET /api/agents/claude/routes --------------------------------------------------------- #

@pytest.fixture
def routes_app(aw):
    from app import agents_routes_api
    app = aw.world.main.app
    have = any(getattr(r, "path", None) == "/api/agents/claude/routes" for r in app.router.routes)
    before = list(app.router.routes)
    if not have:                                   # WP-INT adds the include line to main.py
        app.include_router(agents_routes_api.router)
    yield aw
    if not have:
        app.router.routes[:] = before


def test_the_routes_route_lists_availability_by_name(routes_app, monkeypatch):
    for k in ("ANTHROPIC_API_KEY", *BEDROCK_OK, "AWS_DEFAULT_REGION", "ANTHROPIC_VERTEX_PROJECT_ID",
              "GOOGLE_APPLICATION_CREDENTIALS", "ANTHROPIC_FOUNDRY_RESOURCE", "ANTHROPIC_FOUNDRY_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    for k, v in BEDROCK_OK.items():
        monkeypatch.setenv(k, v)
    r = routes_app.owner.get("/api/agents/claude/routes")
    assert r.status_code == 200, r.text
    body = r.json()
    rows = {x["route"]: x for x in body["routes"]}
    assert set(rows) == {"api_key", "bedrock", "google", "foundry"}
    assert body["current"] == "api_key"
    assert rows["bedrock"] == {"route": "bedrock", "available": True, "missing": []}
    assert rows["api_key"]["available"] is False and rows["api_key"]["missing"] == ["ANTHROPIC_API_KEY"]
    assert rows["google"]["available"] is False and "ANTHROPIC_VERTEX_PROJECT_ID" in rows["google"]["missing"]
    assert "fake-secret" not in r.text and "AKIAFAKE" not in r.text


def test_the_routes_route_is_manage_agents_only(routes_app):
    for who in ("max", "kid", "anonymous"):
        r = getattr(routes_app, who).get("/api/agents/claude/routes")
        assert r.status_code in (401, 403), (who, r.status_code)
