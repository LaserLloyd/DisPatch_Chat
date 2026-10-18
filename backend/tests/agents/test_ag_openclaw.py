"""OpenClaw adapter specifics, against a fake gateway on an ephemeral loopback port.

The fake speaks the frames the existing client code consumes (gateway_ws.py,
gateway_router.py, openclaw.py); the upstream schema is UNVERIFIED (RESEARCH.md
R-OC3) and the gateway's protocol number is 4 (R-OC1). The real gateway is never
contacted: OPENCLAW_GATEWAY_URL points at a dead port for the whole suite.
"""
from __future__ import annotations

import asyncio
import json

import pytest
import pytest_asyncio
from agent_fakes import (
    FAKE_GATEWAY_TOKEN,
    TRUNCATED_FULL,
    FakeGateway,
)

from app import gateway_ws, openclaw
from app.agents.budget import Budget
from app.agents.events import (
    ErrorCode,
    ErrorEvent,
    TaskState,
    TextDeltaEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from app.agents.interface import AgentBackendError, AgentSpec, TaskContext
from app.agents.openclaw_adapter import OpenClawBackend, map_openclaw_exception, probe_gateway


@pytest_asyncio.fixture
async def gw():
    g = FakeGateway()
    await g.start()
    yield g
    await g.stop()


def make_backend(gw, **kw):
    kw.setdefault("token", FAKE_GATEWAY_TOKEN)
    kw.setdefault("transport", "gateway")
    kw.setdefault("probe_timeout_s", 3.0)
    kw.setdefault("connect_timeout_s", 5.0)
    be = OpenClawBackend(gateway_url=gw.url, **kw)
    be.ABORT_GRACE_S = 3.0
    return be


def spec(**kw):
    base = {"backend": "openclaw", "name": "Scout", "tool_allowlist": ["Read"], "budget": Budget()}
    base.update(kw)
    return AgentSpec(**base)


async def run(be, scenario, tmp_path, **spec_kw):
    tid = await be.start_task(spec(**spec_kw), f"scenario:{scenario}", TaskContext(data_dir=tmp_path))
    return tid, [e async for e in be.events(tid)]


# ---- the protocol probe (RESEARCH.md R-OC1, section 5 item 26) ----------------------------- #

async def test_the_probe_accepts_protocol_4_and_reports_the_advertised_surface(gw):
    p = await probe_gateway(gw.url, FAKE_GATEWAY_TOKEN, timeout=3)
    assert p.protocol == gateway_ws.PROTOCOL == 4
    assert "sessions.subscribe" in p.methods and "session.message" in p.events


@pytest.mark.parametrize("make", [
    lambda: FakeGateway(protocol=5),
    lambda: FakeGateway(protocol=None),
    lambda: FakeGateway(connect_close=(1002, "PROTOCOL_MISMATCH")),
    lambda: FakeGateway(methods=[]),
    lambda: FakeGateway(events=["something.else"]),
], ids=["protocol-5", "no-protocol-field", "closed-1002", "empty-methods", "no-session-message"])
async def test_a_protocol_mismatch_fails_the_task_loudly_and_is_never_retried(make, tmp_path, monkeypatch):
    async def must_not_run(*a, **k):
        raise AssertionError("the CLI fallback must not mask a protocol mismatch")
    monkeypatch.setattr(openclaw, "cli_available", lambda: True)
    monkeypatch.setattr(openclaw, "send_to_agent", must_not_run)
    g = make()
    await g.start()
    try:
        be = make_backend(g, transport="auto")            # auto must NOT mask this with the CLI fallback
        tid, ev = await run(be, "simple", tmp_path)
        err = next(e for e in ev if isinstance(e, ErrorEvent))
        assert err.code == ErrorCode.backend_protocol_mismatch and err.retryable is False
        assert ev[-1].state == TaskState.failed and ev[-1].reason == "backend_protocol_mismatch"
        assert "agent" not in g.methods_called(), "no work was sent to a gateway we cannot speak to"
        await be.aclose()
    finally:
        await g.stop()


async def test_a_wrong_token_is_auth_failed(gw, tmp_path):
    be = make_backend(gw, token="not-the-token")  # scrub-ok: a deliberately wrong test token
    tid, ev = await run(be, "simple", tmp_path)
    err = next(e for e in ev if isinstance(e, ErrorEvent))
    assert err.code == ErrorCode.auth_failed and "token mismatch" in err.message
    await be.aclose()


async def test_no_token_at_all_is_auth_failed_before_any_connection(gw, tmp_path, monkeypatch):
    monkeypatch.setattr(gateway_ws, "gateway_token", lambda *a, **k: None)
    be = make_backend(gw, token=None)
    tid, ev = await run(be, "simple", tmp_path)
    assert next(e for e in ev if isinstance(e, ErrorEvent)).code == ErrorCode.auth_failed
    assert gw.connections == 0
    await be.aclose()


async def test_an_unreachable_gateway_is_backend_unavailable(tmp_path):
    g = FakeGateway()
    await g.start()
    url = g.url
    await g.stop()
    be = OpenClawBackend(gateway_url=url, token=FAKE_GATEWAY_TOKEN, transport="gateway", probe_timeout_s=1.0)
    tid, ev = await run(be, "simple", tmp_path)
    err = next(e for e in ev if isinstance(e, ErrorEvent))
    assert err.code == ErrorCode.backend_unavailable and err.retryable
    await be.aclose()


# ---- request shape -------------------------------------------------------------------------- #

async def test_the_agent_request_matches_what_the_existing_transport_sends(gw, tmp_path):
    be = make_backend(gw)
    tid, ev = await run(be, "simple", tmp_path, model="some-model", backend_options={"thinking": "low"})
    methods = gw.methods_called()
    assert methods[0] == "connect" and "sessions.subscribe" in methods and "sessions.messages.subscribe" in methods
    assert methods.index("sessions.messages.subscribe") < methods.index("agent")
    req = next(r for r in gw.requests if r["method"] == "agent")["params"]
    assert req["agentId"] == "scout" and req["sessionKey"] == f"agent:scout:{tid}"
    assert req["deliver"] is False and req["model"] == "some-model" and req["thinking"] == "low"
    assert req["idempotencyKey"] and req["message"] == "scenario:simple"
    hello_req = gw.requests[0]["params"]
    assert hello_req["minProtocol"] == hello_req["maxProtocol"] == 4
    assert ev[-1].session_id == f"agent:scout:{tid}", "the session KEY is the durable address"
    await be.aclose()


async def test_backend_options_choose_the_openclaw_agent(gw, tmp_path):
    be = make_backend(gw)
    tid, _ = await run(be, "simple", tmp_path, backend_options={"agent_id": "Sage"})
    req = next(r for r in gw.requests if r["method"] == "agent")["params"]
    assert req["agentId"] == "sage"
    await be.aclose()


async def test_interrupt_sends_chat_abort_for_this_session_and_run(gw, tmp_path):
    be = make_backend(gw)
    tid = await be.start_task(spec(), "scenario:long", TaskContext(data_dir=tmp_path))
    it = be.events(tid).__aiter__()
    async for e in it:
        if isinstance(e, TextDeltaEvent):
            break
    await be.interrupt(tid)
    ev = [e async for e in it]
    abort = next(r for r in gw.requests if r["method"] == "chat.abort")["params"]
    run_id = next(r for r in gw.requests if r["method"] == "agent")["params"]["idempotencyKey"]
    assert abort == {"sessionKey": f"agent:scout:{tid}", "runId": run_id}
    assert ev[-1].reason == "interrupted"
    await be.aclose()


# ---- frame mapping ----------------------------------------------------------------------------- #

async def test_cumulative_chat_snapshots_become_incremental_deltas(gw, tmp_path):
    be = make_backend(gw)
    tid, ev = await run(be, "simple", tmp_path)
    deltas = [e for e in ev if isinstance(e, TextDeltaEvent)]
    assert [d.text for d in deltas] == ["Hello", " world"] and not any(d.replace for d in deltas)
    await be.aclose()


async def test_the_transcript_event_supplies_text_when_no_chat_deltas_arrive(gw, tmp_path):
    be = make_backend(gw)
    tid, ev = await run(be, "no_deltas", tmp_path)
    assert [d.text for d in ev if isinstance(d, TextDeltaEvent)] == ["only the transcript event"]
    assert ev[-1].text == "only the transcript event"
    await be.aclose()


async def test_a_truncated_projection_is_refetched_in_full(gw, tmp_path):
    """The 8000-character hazard (RESEARCH.md section 5 item 26)."""
    be = make_backend(gw)
    tid, ev = await run(be, "truncated", tmp_path)
    assert "chat.message.get" in gw.methods_called()
    text = "".join(e.text for e in ev if isinstance(e, TextDeltaEvent))
    assert text == TRUNCATED_FULL and "truncated" not in text
    await be.aclose()


async def test_tool_lifecycle_frames_become_tool_events(gw, tmp_path):
    be = make_backend(gw)
    tid, ev = await run(be, "tools", tmp_path)
    call = next(e for e in ev if isinstance(e, ToolCallEvent))
    res = next(e for e in ev if isinstance(e, ToolResultEvent))
    assert (call.name, call.call_id, call.input) == ("Read", "tc1", {"file_path": "notes.txt"})
    assert res.call_id == "tc1" and res.output == "file contents"
    await be.aclose()


async def test_token_usage_is_reported_without_money(gw, tmp_path):
    be = make_backend(gw)
    tid, ev = await run(be, "simple", tmp_path)
    cost = next(e for e in ev if e.type == "cost_update")
    assert cost.total_cost_usd is None and (cost.input_tokens, cost.output_tokens) == (100, 20)
    assert cost.estimate is True
    await be.aclose()


async def test_the_token_budget_is_enforced_after_the_fact(gw, tmp_path):
    be = make_backend(gw)
    tid, ev = await run(be, "budget", tmp_path, budget=Budget(max_tokens=1000))
    assert ev[-1].reason == "budget_exceeded"
    assert be.capabilities().budget_enforcement == "post_hoc", "documented: OpenClaw cannot stop mid-run"
    await be.aclose()


async def test_the_turn_cap_counts_dispatched_messages(gw, tmp_path):
    be = make_backend(gw)
    tid, ev = await run(be, "simple", tmp_path, budget=Budget(max_turns=1))
    assert ev[-1].state == TaskState.succeeded
    await be.send(tid, "scenario:simple")
    ev2 = [e async for e in be.events(tid, after_seq=ev[-1].seq)]
    assert ev2[-1].reason == "budget_exceeded"
    await be.aclose()


# ---- capabilities and the same-host rule --------------------------------------------------------- #

def test_capabilities_are_honest():
    caps = OpenClawBackend().capabilities()
    assert caps.same_host_required is True and caps.approvals is False and caps.steering is False
    assert caps.subagents is False and caps.cost_reporting == "tokens" and caps.label == "OpenClaw"


async def test_the_gateway_token_never_appears_in_events(gw, tmp_path):
    be = make_backend(gw)
    tid, ev = await run(be, "simple", tmp_path)
    assert FAKE_GATEWAY_TOKEN not in json.dumps([e.model_dump(mode="json") for e in ev])
    await be.aclose()


# ---- CLI transport and fallback --------------------------------------------------------------------- #

def _reply(text="from the cli"):
    return openclaw.AgentReply(payloads=[openclaw.AgentPayload(text=text)],
                               metadata={"session_id": "cli-sess", "tokens_in": 10, "tokens_out": 5})


async def test_the_cli_transport_runs_a_turn_without_a_gateway(tmp_path, monkeypatch):
    seen = {}

    async def fake_send(agent_id, key, message, timeout=None, model=None, thinking=None, on_spawn=None):
        seen.update(agent_id=agent_id, key=key, message=message, model=model)
        return _reply()

    monkeypatch.setattr(openclaw, "send_to_agent", fake_send)
    monkeypatch.setattr(openclaw, "cli_available", lambda: True)
    be = OpenClawBackend(transport="cli")
    tid, ev = await run(be, "simple", tmp_path, model="m1")
    assert seen["agent_id"] == "scout" and seen["key"] == f"agent:scout:{tid}" and seen["model"] == "m1"
    assert [e.text for e in ev if isinstance(e, TextDeltaEvent)] == ["from the cli"]
    assert ev[-1].state == TaskState.succeeded and ev[-1].text == "from the cli"
    assert be.capabilities().streaming_text is False
    await be.aclose()


async def test_cli_cancel_kills_the_process_tree_through_the_spawn_seam(tmp_path, monkeypatch):
    killed = []
    started = asyncio.Event()
    release = asyncio.Event()

    class Proc:
        returncode = None
        pid = 4242

    async def fake_send(agent_id, key, message, timeout=None, model=None, thinking=None, on_spawn=None):
        on_spawn(Proc())
        started.set()
        await release.wait()
        raise openclaw.AgentError("scout reported an error.", detail="killed")

    monkeypatch.setattr(openclaw, "send_to_agent", fake_send)
    monkeypatch.setattr(openclaw, "cli_available", lambda: True)
    monkeypatch.setattr(openclaw, "_kill_tree", lambda proc: (killed.append(proc.pid), release.set()))
    be = OpenClawBackend(transport="cli")
    tid = await be.start_task(spec(), "scenario:long", TaskContext(data_dir=tmp_path))
    await asyncio.wait_for(started.wait(), 5)
    await be.cancel(tid)
    ev = [e async for e in be.events(tid)]
    assert killed == [4242] and ev[-1].state == TaskState.cancelled and ev[-1].reason == "cancelled"
    await be.aclose()


async def test_auto_falls_back_to_the_cli_only_when_the_gateway_is_unreachable(tmp_path, monkeypatch):
    g = FakeGateway()
    await g.start()
    dead = g.url
    await g.stop()

    async def fake_send(agent_id, key, message, timeout=None, model=None, thinking=None, on_spawn=None):
        return _reply("via cli")

    monkeypatch.setattr(openclaw, "send_to_agent", fake_send)
    monkeypatch.setattr(openclaw, "cli_available", lambda: True)
    be = OpenClawBackend(gateway_url=dead, token=FAKE_GATEWAY_TOKEN, transport="auto", probe_timeout_s=1.0)
    tid, ev = await run(be, "simple", tmp_path)
    assert ev[-1].state == TaskState.succeeded and ev[-1].text == "via cli"
    await be.aclose()


async def test_the_cli_must_exist_when_it_is_the_chosen_transport(tmp_path, monkeypatch):
    monkeypatch.setattr(openclaw, "cli_available", lambda: False)
    be = OpenClawBackend(transport="cli")
    tid, ev = await run(be, "simple", tmp_path)
    assert next(e for e in ev if isinstance(e, ErrorEvent)).code == ErrorCode.backend_unavailable
    await be.aclose()


# ---- error mapping of the existing exception classes ---------------------------------------------------- #

@pytest.mark.parametrize("exc,code", [
    (openclaw.GatewayUnavailable("down"), ErrorCode.backend_unavailable),
    (openclaw.AgentRefused("no"), ErrorCode.refused),
    (openclaw.AgentTimeout("slow"), ErrorCode.timeout),
    (openclaw.AgentNotFound("missing"), ErrorCode.backend_unavailable),
    (openclaw.AgentError("boom"), ErrorCode.backend_error),
    (TimeoutError(), ErrorCode.timeout),
    (ValueError("bug"), ErrorCode.internal),
])
def test_openclaw_exceptions_map_to_catalogued_codes(exc, code):
    assert map_openclaw_exception(exc).code == code


def test_a_missing_cli_is_not_marked_retryable():
    assert map_openclaw_exception(openclaw.AgentNotFound("x")).retryable is False
    assert map_openclaw_exception(openclaw.GatewayUnavailable("x")).retryable is True


# ---- sessions ------------------------------------------------------------------------------------------------ #

async def test_list_sessions_wraps_the_existing_sessions_index(tmp_path, monkeypatch):
    root = tmp_path / "agents"
    sess = root / "scout" / "sessions"
    sess.mkdir(parents=True)
    (sess / "sid-1.jsonl").write_bytes(b"{}\n")
    (sess / "sessions.json").write_text(json.dumps({"agent:scout:abc": {"sessionId": "sid-1"}}))
    monkeypatch.setattr(openclaw, "OPENCLAW_AGENTS_DIR", root)
    be = OpenClawBackend()
    rows = await be.list_sessions(agent_id="scout")
    assert [(r.session_id, r.task_id, r.title) for r in rows] == [("sid-1", "abc", "agent:scout:abc")]
    assert await be.list_sessions(agent_id="nobody") == []


async def test_resume_continues_the_same_openclaw_session_key(gw, tmp_path):
    be = make_backend(gw)
    tid, ev = await run(be, "simple", tmp_path)
    await be.resume(tid)
    ev2 = [e async for e in be.events(tid, after_seq=ev[-1].seq)]
    keys = {r["params"]["sessionKey"] for r in gw.requests if r["method"] == "agent"}
    assert keys == {f"agent:scout:{tid}"} and ev2[-1].session_id == ev[-1].session_id
    await be.aclose()


async def test_respond_approval_is_explicitly_unsupported():
    with pytest.raises(AgentBackendError) as ei:
        await OpenClawBackend().respond_approval("apr_x", "allow")
    assert ei.value.code == ErrorCode.not_supported
