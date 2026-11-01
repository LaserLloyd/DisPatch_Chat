"""4.3 (#13): the Claude Managed Agents adapter, replaying recorded event streams.

Nothing here reaches the network: the real ``anthropic.AsyncAnthropic`` client runs over an
``httpx.MockTransport`` that serves the recordings in ``fixtures/managed/`` (synthetic, built
from the documented event shapes - the adapter is unverified against the live service).

(b) a replayed stream ends in the same ``ChatOutcome`` shape as the Claude Agent (SDK) path;
    an authentication error maps to ``auth_failed``.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
from pathlib import Path

import httpx
import pytest
from agent_fakes import FakeSdkTransport

from app import agents_store as store
from app import config
from app.agents.claude_sdk_adapter import ClaudeAgentBackend
from app.agents.events import ErrorCode
from app.agents.interface import AgentSpec, TaskContext
from app.agents.managed_adapter import ManagedAgentsBackend, _usd_of
from app.agents.secrets import MemorySecretProvider
from app.agents_service import ChatOutcome, Hooks, TaskManager

pytestmark = pytest.mark.timeout(60)

REC = Path(__file__).parent / "fixtures" / "managed"
KEY = "sk-ant-api03-" + "Mm7nN6oO" * 6


class Remote:
    """The mock API: records every request, serves one recording on the stream."""

    def __init__(self, stream: str = "end_turn.sse", *, create_status: int = 200):
        self.stream = stream
        self.create_status = create_status
        self.requests: list[httpx.Request] = []
        self.sent: list[dict] = []

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        path = req.url.path
        if req.method == "POST" and path == "/v1/sessions":
            if self.create_status != 200:
                return httpx.Response(self.create_status, json=json.loads((REC / "auth_error.json").read_text()))
            return httpx.Response(200, json=json.loads((REC / "session.json").read_text()))
        if req.method == "GET" and path.endswith("/events/stream"):
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  content=(REC / self.stream).read_bytes())
        if req.method == "POST" and path.endswith("/events"):
            self.sent.extend(json.loads(req.content)["events"])
            return httpx.Response(200, json={"data": []})
        return httpx.Response(404, json={"type": "error", "error": {"type": "not_found_error", "message": path}})

    def client(self, key: str):
        from anthropic import AsyncAnthropic
        return AsyncAnthropic(api_key=key, max_retries=0,
                              http_client=httpx.AsyncClient(transport=httpx.MockTransport(self.handler)))


def backend(remote: Remote, enabled: bool = True) -> ManagedAgentsBackend:
    be = ManagedAgentsBackend(client_factory=remote.client, enabled=lambda: enabled)
    be.INTERRUPT_GRACE_S = 1.0
    return be


def spec(**over):
    base = {"backend": "managed", "tool_allowlist": ["read", "bash"],
            "backend_options": {"agent_id": "agent_01TEST", "environment_id": "env_01TEST"}}
    base.update(over)
    return AgentSpec(**base)


def ctx(tmp_path, tid, key=KEY):
    return TaskContext(data_dir=tmp_path, task_id=tid,
                       secrets=MemorySecretProvider({"ANTHROPIC_API_KEY": key} if key else {}))


async def drain(be, tid, *, approve=None, timeout=20.0):
    out = []

    async def go():
        async for ev in be.events(tid):
            out.append(ev)
            if ev.type == "approval_request" and approve is not None:
                await be.respond_approval(ev.request_id, approve)
            if ev.type == "result":
                return
    await asyncio.wait_for(go(), timeout)
    return out


async def test_b_a_replayed_stream_succeeds_with_text_tools_and_cost(tmp_path):
    remote = Remote()
    be = backend(remote)
    tid = await be.start_task(spec(), "Summarize README.md", ctx(tmp_path, "t_ma_ok"))
    evs = await drain(be, tid)
    types = [e.type for e in evs]
    assert types[:2] == ["status", "status"] and "tool_call" in types and "tool_result" in types
    call = next(e for e in evs if e.type == "tool_call")
    assert call.name == "read" and call.call_id == "sevt_05"
    assert next(e for e in evs if e.type == "tool_result").call_id == "sevt_05"
    final = evs[-1]
    assert final.state.value == "succeeded" and final.text == "The README says hello."
    assert final.session_id == "sesn_01TEST" and final.resumable
    info = be.task_info(tid)
    assert info.cost_usd == pytest.approx(0.03) and info.tokens == 1200 + 80 + 1400 + 30 and info.turns == 2
    # stream FIRST, then the message (the stream has no replay)
    kinds = [(r.method, r.url.path.rsplit("/", 1)[-1]) for r in remote.requests]
    assert kinds == [("POST", "sessions"), ("GET", "stream"), ("POST", "events")]
    assert remote.sent == [{"type": "user.message", "content": [{"type": "text", "text": "Summarize README.md"}]}]
    create = json.loads(remote.requests[0].content)
    assert create["agent"] == "agent_01TEST" and create["environment_id"] == "env_01TEST"
    assert create["metadata"] == {"dispatch_task": tid}
    assert remote.requests[0].headers["x-api-key"] == KEY
    assert "managed-agents" in remote.requests[0].headers.get("anthropic-beta", "")
    await be.aclose()


async def test_b_auth_error_maps_to_auth_failed_and_never_echoes_the_key(tmp_path):
    remote = Remote(create_status=401)
    be = backend(remote)
    tid = await be.start_task(spec(), "hi", ctx(tmp_path, "t_ma_auth"))
    evs = await drain(be, tid)
    err = next(e for e in evs if e.type == "error")
    assert err.code == ErrorCode.auth_failed
    assert evs[-1].state.value == "failed" and evs[-1].reason == "auth_failed"
    assert KEY not in json.dumps([e.model_dump(mode="json") for e in evs])
    await be.aclose()


async def test_b_no_key_and_switched_off_fail_closed_without_a_request(tmp_path):
    remote = Remote()
    be = backend(remote, enabled=False)
    tid = await be.start_task(spec(), "hi", ctx(tmp_path, "t_ma_off"))
    evs = await drain(be, tid)
    assert next(e for e in evs if e.type == "error").code == ErrorCode.not_supported
    be2 = backend(remote)
    tid2 = await be2.start_task(spec(), "hi", ctx(tmp_path, "t_ma_nokey", key=None))
    evs2 = await drain(be2, tid2)
    assert next(e for e in evs2 if e.type == "error").code == ErrorCode.auth_failed
    assert remote.requests == [], "nothing may reach the API while off or without a key"
    await be.aclose()
    await be2.aclose()


async def test_a_parked_tool_call_becomes_our_approval_and_is_confirmed(tmp_path):
    remote = Remote("requires_action.sse")
    be = backend(remote)
    tid = await be.start_task(spec(), "list files", ctx(tmp_path, "t_ma_ask"))
    evs = await drain(be, tid, approve="allow")
    assert any(e.type == "approval_request" and e.tool_name == "bash" for e in evs)
    assert {"type": "user.tool_confirmation", "tool_use_id": "sevt_22", "result": "allow"} in remote.sent
    assert evs[-1].state.value == "succeeded" and evs[-1].text == "One file."
    await be.aclose()


async def test_a_tool_outside_the_allowlist_is_denied_without_asking(tmp_path):
    remote = Remote("requires_action.sse")
    be = backend(remote)
    tid = await be.start_task(spec(tool_allowlist=["read"]), "list files", ctx(tmp_path, "t_ma_deny"))
    evs = await drain(be, tid)
    assert not any(e.type == "approval_request" for e in evs)
    assert {"type": "user.tool_confirmation", "tool_use_id": "sevt_22", "result": "deny"} in remote.sent
    await be.aclose()


async def test_budget_reached_and_retries_exhausted_map_onto_the_catalogue(tmp_path):
    be = backend(Remote("budget_reached.sse"))
    tid = await be.start_task(spec(budget={"max_usd": 0.4}), "go", ctx(tmp_path, "t_ma_bud"))
    evs = await drain(be, tid)
    assert next(e for e in evs if e.type == "error").code == ErrorCode.budget_exceeded
    assert evs[-1].state.value == "failed"
    await be.aclose()
    remote = Remote("retries_exhausted.sse")
    be = backend(remote)
    tid = await be.start_task(spec(budget={"max_usd": 2.0}), "go", ctx(tmp_path, "t_ma_retry"))
    evs = await drain(be, tid)
    err = next(e for e in evs if e.type == "error")
    assert err.code == ErrorCode.backend_error and "overloaded" in err.message
    create = json.loads(remote.requests[0].content)
    assert create["budget"] == {"type": "limit", "max_list_cost": {"amount": "200", "currency": "USD"}}
    await be.aclose()


def test_list_cost_is_read_as_cents():
    assert _usd_of({"amount": "250", "currency": "USD"}) == 2.5
    assert _usd_of({"amount": "1.25", "currency": "USD"}) == 1.25
    assert _usd_of({"amount": "9", "currency": "EUR"}) is None


async def test_b_the_outcome_has_the_same_shape_as_the_claude_agent_path(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    (tmp_path / "data").mkdir(exist_ok=True)
    store.reset_for_tests()
    mgr = TaskManager(config.DATA_DIR, hooks=Hooks(), secrets=MemorySecretProvider({"ANTHROPIC_API_KEY": KEY}))
    mgr.register_backend("managed", backend(Remote()))
    mgr.register_backend("claude_sdk", ClaudeAgentBackend(
        transport_factory=lambda rt, ws: FakeSdkTransport(session_id=f"sdk-{rt.task_id}")))
    store.set_setting("agents.managed.enabled", True)
    await mgr.startup()
    try:
        outcomes = {}
        for name, sp in (("managed", spec()), ("claude_sdk", AgentSpec(backend="claude_sdk", tool_allowlist=["Read"]))):
            agent = store.create_agent(name=f"A-{name}", spec=sp.model_dump(mode="json"), published_to=["*"],
                                       created_by="")
            row = await mgr.dispatch(agent=agent, prompt="scenario:simple", owner="o1")
            outcomes[name] = await mgr.wait_for_terminal(row["task_id"], timeout=20)
        a, b = outcomes["managed"], outcomes["claude_sdk"]
        assert isinstance(a, ChatOutcome) and isinstance(b, ChatOutcome)
        assert [f.name for f in dataclasses.fields(a)] == [f.name for f in dataclasses.fields(b)]
        assert (a.state, a.reason, a.error_code) == (b.state, b.reason, b.error_code) == ("succeeded", None, None)
        assert isinstance(a.text, str) and isinstance(b.text, str) and a.text and b.text
    finally:
        await mgr.shutdown()
