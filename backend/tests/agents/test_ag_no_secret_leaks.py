"""G17 Secrets: a seeded fake credential never reaches events, logs, a support-bundle
style dump, task info, or anything written under the data dir.

The scenarios deliberately make the credential travel the dangerous routes: a tool
result that echoes it, assistant text that quotes it, a tool-call argument that
contains it, an error path, a budget stop, an interrupt, an approval.
"""
from __future__ import annotations

import asyncio
import json
import logging

import pytest
from agent_fakes import FAKE_API_KEY, FAKE_GATEWAY_TOKEN
from agent_harness import HARNESSES

from app.agents.budget import Budget
from app.agents.events import ApprovalRequestEvent, TextDeltaEvent
from app.agents.fake import Finish, Say, Tool
from app.agents.interface import AgentSpec, TaskContext
from app.agents.secrets import REDACTED


def blob_of_events(events) -> str:
    return json.dumps([e.model_dump(mode="json") for e in events])


async def collect_all(h, task_id):
    return [e async for e in h.backend.events(task_id)]


async def run_scenario(h, scenario, *, spec=None, approve=None, interrupt=False):
    h.prepare(scenario)
    tid = await h.backend.start_task(spec or h.spec(), f"scenario:{scenario}", h.ctx())
    seen = []
    it = h.backend.events(tid).__aiter__()

    async def go():
        async for e in it:
            seen.append(e)
            if approve and isinstance(e, ApprovalRequestEvent):
                await h.backend.respond_approval(e.request_id, approve, "once")
            if interrupt and isinstance(e, TextDeltaEvent):
                await h.backend.interrupt(tid)
    await asyncio.wait_for(go(), 20)
    return tid, seen


@pytest.mark.parametrize("adapter,secret", [("claude_sdk", FAKE_API_KEY), ("openclaw", FAKE_GATEWAY_TOKEN)])
async def test_the_credential_never_leaves_the_adapter(adapter, secret, tmp_path, caplog):
    h = HARNESSES[adapter](tmp_path)
    await h.setup()
    all_events, tids = [], []
    with caplog.at_level(logging.DEBUG):
        plan = [("simple", {}), ("leaky", {}), ("long", {"interrupt": True})]
        if adapter == "claude_sdk":
            plan.append(("approval", {"approve": "allow"}))
        plan += [("budget", {"spec": h.spec(budget=Budget(max_usd=0.05, max_tokens=1000))}),
                 ("error_unavailable", {}), ("error_auth", {})]       # error_auth LAST: it swaps the gateway token
        for scenario, kw in plan:
            tid, ev = await run_scenario(h, scenario, **kw)
            tids.append(tid)
            all_events += ev

        # the leaky run is the one that MUST have been scrubbed, not merely absent
        leaky = await collect_all(h, tids[1])
        assert REDACTED in blob_of_events(leaky), "the echo was redacted, not silently dropped"

        # task info and a support-bundle style dump
        infos = json.dumps([h.backend.task_info(t).model_dump(mode="json") for t in tids])
        dump = json.dumps(h.backend.support_dump())
        await h.backend.aclose()

    # The websockets LIBRARY dumps frame heads at DEBUG - including the FAKE SERVER's own
    # echo in the leaky scenario. That is the test double talking to itself, not DisPatch
    # code, so library loggers are excluded; every dispatch/local-chat logger is in scope.
    ours = [r for r in caplog.records if not r.name.startswith(("websockets", "asyncio"))]
    surfaces = {
        "events": blob_of_events(all_events),
        "task_info": infos,
        "support_dump": dump,
        "logs": "".join(r.getMessage() + (r.exc_text or "") + str(r.args or "") for r in ours),
        "exceptions": "".join(str(r.exc_info[1]) for r in ours if r.exc_info),
    }
    for name, text in surfaces.items():
        assert secret not in text, f"{adapter}: the credential leaked into {name}"

    on_disk = [p for p in h.data_dir.rglob("*") if p.is_file()]
    for p in on_disk:
        assert secret.encode() not in p.read_bytes(), f"{adapter}: the credential was written to {p}"
    await h.teardown()


async def test_the_claude_plan_and_options_dump_carry_no_credential(tmp_path):
    h = HARNESSES["claude_sdk"](tmp_path)
    await h.setup()
    tid, _ = await run_scenario(h, "simple")
    rt = h.backend._tasks[tid]
    plan = rt.extras["plan"]
    assert FAKE_API_KEY not in json.dumps(plan.options) + json.dumps(plan.describe())
    assert FAKE_API_KEY in json.dumps(plan.secret_env), "...it exists only in the pipe-bound field"
    await h.teardown()


async def test_the_fake_backend_redacts_registered_secrets_in_tool_output(tmp_path):
    from app.agents.fake import FakeBackend
    from app.agents.secrets import SecretRedactor

    red = SecretRedactor()
    red.register(FAKE_API_KEY)
    be = FakeBackend(scripts={"default": [Tool("Read", {"file_path": FAKE_API_KEY}, output=f"k={FAKE_API_KEY}"),
                                          Say(f"leak {FAKE_API_KEY}"), Finish(f"end {FAKE_API_KEY}")]},
                     redactor=red)
    tid = await be.start_task(AgentSpec(backend="fake", tool_allowlist=["Read"]), "go",
                              TaskContext(data_dir=tmp_path))
    ev = [e async for e in be.events(tid)]
    assert FAKE_API_KEY not in blob_of_events(ev) and REDACTED in blob_of_events(ev)
    assert FAKE_API_KEY not in json.dumps(be.support_dump())
    await be.aclose()


async def test_key_shaped_strings_are_redacted_even_if_never_registered(tmp_path):
    from app.agents.fake import FakeBackend
    from app.agents.secrets import SecretRedactor

    unknown = "sk-ant-api03-NEVER-REGISTERED-0123456789"  # scrub-ok: a fake, never-registered key
    be = FakeBackend(scripts={"default": [Say(f"pasted {unknown}"), Finish("x")]}, redactor=SecretRedactor())
    tid = await be.start_task(AgentSpec(backend="fake"), "go", TaskContext(data_dir=tmp_path))
    ev = [e async for e in be.events(tid)]
    assert unknown not in blob_of_events(ev)
    await be.aclose()
