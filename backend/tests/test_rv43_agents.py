"""4.3 Opus review: agent grant gaps, the OpenClaw seat-name bug, schedule PATCH nulls, the
Owner's schedules, and approval bookkeeping (a passing backend error, the lock table)."""
from __future__ import annotations

import sys

import pytest
from agents_world import aw, make_agent, spec
from test_i43_agent_grants import _refused, ada
from test_i43_agent_limits import SCRIPTS, UsdOpenClaw, data, run, settle, state
from test_i43_agent_limits import make_agent as store_agent

from app import agents_store as store
from app import config
from app.agents import schedule_store
from app.agents.fake import FakeBackend
from app.agents.interface import AgentBackendError, ErrorCode
from app.agents.scheduler import Scheduler
from app.agents_service import Hooks, TaskError, TaskManager

pytestmark = pytest.mark.timeout(60)


@pytest.fixture
def sw(aw):
    schedule_store.reset_for_tests()
    aw.sch = Scheduler(manager=lambda: aw.mgr)
    return aw


# -- grants ------------------------------------------------------------------ #

def test_an_unprotected_admin_may_not_bind_an_agent_to_a_cli_preset(aw, ada):
    good = {"presets": [{"id": "sh", "kind": "generic", "executable": sys.executable, "args": ["-c", "pass"]}]}
    assert aw.owner.put("/api/agents/cli-presets", json=good).status_code == 200
    body = {"name": "Host", "published_to": ["*"],
            "spec": {"backend": "cli", "backend_options": {"preset": "sh"},
                     "tool_allowlist": ["Read"], "approval_policy": {"mode": "interactive"}}}
    _refused(ada.post("/api/agents", json=body))
    r = aw.owner.post("/api/agents", json=body)
    assert r.status_code == 201, r.text
    # the Admin may still edit the Owner's approved agent (same preset)...
    aid = r.json()["id"]
    assert ada.patch(f"/api/agents/{aid}", json={"description": "notes"}).status_code == 200


def test_powershell_is_a_host_level_grant(aw, ada):
    _refused(ada.post("/api/agents", json={"name": "Ps", "spec": spec(tool_allowlist=["Read", "PowerShell"])}))


# -- OpenClaw seat from a display name --------------------------------------- #

def test_an_openclaw_agent_name_with_a_space_saves(aw):
    r = aw.owner.post("/api/agents", json={"name": "My Agent", "spec": {"backend": "openclaw"}})
    assert r.status_code == 201, r.text


def test_an_explicit_bad_seat_and_a_name_with_no_usable_letter_are_refused(aw):
    r = aw.owner.post("/api/agents", json={"name": "Seat", "spec": {
        "backend": "openclaw", "backend_options": {"agent_id": "a b"}}})
    assert r.status_code == 422
    # "日本" would normalise to the "main" fallback (the lead seat): needs an explicit seat
    r = aw.owner.post("/api/agents", json={"name": "日本", "spec": {"backend": "openclaw"}})
    assert r.status_code == 422, r.text
    r = aw.owner.post("/api/agents", json={"name": "日本", "spec": {
        "backend": "openclaw", "backend_options": {"agent_id": "nihon"}}})
    assert r.status_code == 201, r.text


# -- schedules ----------------------------------------------------------------- #

def _owner_schedule(sw):
    a = make_agent(sw.owner, "Mine", published=[])
    r = sw.owner.post("/api/agents/schedules", json={"agent_id": a["id"], "prompt": "p",
                                                    "rule": {"every_minutes": 60}})
    assert r.status_code == 201, r.text
    return r.json()["schedule"]["id"]


def test_another_admin_may_not_act_on_the_owners_schedules(sw):
    sid = _owner_schedule(sw)
    sw.world.person(sw.owner, "Ada", "admin", pin="8888")
    ada_c = sw.caller(7)
    assert sw.world.login(ada_c, name="Ada", pin="8888").status_code == 200
    assert ada_c.patch(f"/api/agents/schedules/{sid}", json={"enabled": False}).status_code == 404
    assert ada_c.delete(f"/api/agents/schedules/{sid}").status_code == 404
    assert schedule_store.get(sid)["enabled"]


@pytest.mark.parametrize("field", ["prompt", "agent_id", "tz", "rule", "enabled"])
def test_a_null_in_a_schedule_patch_is_a_422_not_a_500(sw, field):
    sid = _owner_schedule(sw)
    r = sw.owner.patch(f"/api/agents/schedules/{sid}", json={field: None})
    assert r.status_code == 422, r.text
    assert schedule_store.get(sid)["enabled"]


# -- approvals ----------------------------------------------------------------- #

class FlakyAnswer(FakeBackend):
    failed = False

    async def respond_approval(self, request_id, decision, scope="once"):
        if not self.failed:
            self.failed = True
            raise AgentBackendError(ErrorCode.internal, "transient")
        return await super().respond_approval(request_id, decision, scope)


async def test_a_passing_backend_error_on_an_answer_does_not_strand_the_task(data):
    store.reset_for_tests()
    m = TaskManager(config.DATA_DIR, hooks=Hooks())
    m.register_backend("fake", FlakyAnswer(scripts=dict(SCRIPTS)))
    m.register_backend("openclaw", UsdOpenClaw(scripts=dict(SCRIPTS)))
    await m.startup()
    try:
        tid = await run(m, "scenario:approval", store_agent("Flaky"))
        await settle(lambda: state(tid) == "awaiting_approval")
        await m.flush()
        rid = store.list_approvals(tid)[0]["request_id"]
        with pytest.raises(AgentBackendError):
            await m.respond_approval(rid, "allow", "once", "o1")
        assert store.get_approval(rid)["decision"] is None, "the request is pending again"
        await m.respond_approval(rid, "allow", "once", "o1")
        await settle(lambda: state(tid) == "succeeded", timeout=5)
    finally:
        await m.shutdown()


async def test_answers_to_unknown_requests_do_not_grow_the_lock_table(data):
    store.reset_for_tests()
    m = TaskManager(config.DATA_DIR, hooks=Hooks())
    m.register_backend("fake", FakeBackend(scripts=dict(SCRIPTS)))
    await m.startup()
    try:
        for i in range(50):
            with pytest.raises(TaskError):
                await m.respond_approval(f"nope-{i}", "allow", "once", "o1")
        assert len(m._appr_locks) == 0
    finally:
        await m.shutdown()
