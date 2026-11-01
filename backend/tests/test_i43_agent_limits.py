"""4.3 WP-A6 (#10, #11): default limits for agent tasks, the approval timeout, the slot an
approval wait frees, and per-day reservations.

The live install runs Bits through OpenClaw with ``agents.unified_dispatch`` possibly on, so
two tests prove the exemptions: a chat turn and the advisor handoff are never held to the new
defaults (f), and a backend that reports tokens gets the turn default but no money cap and no
reservation (g)."""
from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from agents_world import aw, eventually
from agents_world import make_agent as api_make_agent
from agents_world import spec as api_spec

from app import agents_store as store
from app import config
from app.agents.fake import Cost, FakeBackend, Finish, Say
from app.agents.interface import AgentSpec, Capabilities
from app.agents_service import Hooks, TaskManager

pytestmark = pytest.mark.timeout(60)


class UsdOpenClaw(FakeBackend):
    """Stands in for OpenClaw in chat/handoff tests (reports money: the exemption is by kind)."""
    name = "openclaw"


class TokenOpenClaw(FakeBackend):
    """OpenClaw as it really is: it reports tokens, never money."""
    name = "openclaw"

    def capabilities(self) -> Capabilities:
        return super().capabilities().model_copy(update={"name": "openclaw", "cost_reporting": "tokens"})


SCRIPTS = {**FakeBackend().scripts,
           "heavy": [Say("lots"), Cost(usd=5.0, input_tokens=10, output_tokens=10, turns=61), Finish("done")]}


async def settle(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = pred()
        if v:
            return v
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached")


def make_agent(name, **over):
    spec = {"backend": "fake", "name": name, "tool_allowlist": ["Read", "Bash"], **over}
    return store.create_agent(name=name, spec=AgentSpec.model_validate(spec).model_dump(mode="json"),
                              published_to=["*"], created_by="")


class Posts:
    def __init__(self):
        self.lines: list[tuple[str, str]] = []

    async def deliver(self, thread_id, text, **kw):
        self.lines.append((thread_id, text))
        return SimpleNamespace(id=f"m{len(self.lines)}")


async def new_manager(openclaw=UsdOpenClaw, posts: Posts | None = None) -> TaskManager:
    store.reset_for_tests()
    m = TaskManager(config.DATA_DIR, hooks=Hooks(deliver=posts.deliver if posts else None))
    m.register_backend("fake", FakeBackend(scripts=dict(SCRIPTS)))
    m.register_backend("openclaw", openclaw(scripts=dict(SCRIPTS)))
    await m.startup()
    return m


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    monkeypatch.delenv("DISPATCH_AGENTS_UNIFIED_DISPATCH", raising=False)
    return tmp_path


@pytest.fixture
async def mgr(data):
    m = await new_manager()
    yield m
    await m.shutdown()


async def run(mgr, prompt, agent, **kw):
    row = await mgr.dispatch(agent=agent, prompt=prompt, owner=kw.pop("owner", "o1"), **kw)
    return row["task_id"]


def state(tid):
    return store.get_task(tid)["state"]


def launched_budget(mgr, tid, backend="fake"):
    return mgr.backends[backend]._tasks[tid].spec.budget


# --------------------------------------------------------------------------- #
# (a) defaults fill an empty budget, and the task row records them
# --------------------------------------------------------------------------- #


async def test_a_task_without_a_budget_runs_with_the_default_limits(mgr):
    tid = await run(mgr, "scenario:long", make_agent("Plain"))
    await settle(lambda: state(tid) == "running")
    b = launched_budget(mgr, tid)
    assert b.max_turns == 60
    assert b.max_usd == 2.0
    assert b.max_usd_per_day is not None and b.max_usd_per_day <= 10.0
    recorded = json.loads(store.get_task(tid)["spec_json"])["budget"]
    assert recorded["max_turns"] == 60 and recorded["max_usd"] == 2.0 and recorded["max_usd_per_day"] == 10.0
    await mgr.cancel(store.get_task(tid))


async def test_an_agents_own_budget_wins_over_the_defaults(mgr):
    tid = await run(mgr, "scenario:long", make_agent("Own", budget={"max_usd": 0.5, "max_turns": 7}))
    await settle(lambda: state(tid) == "running")
    b = launched_budget(mgr, tid)
    assert (b.max_usd, b.max_turns) == (0.5, 7)
    await mgr.cancel(store.get_task(tid))


async def test_the_default_turn_limit_stops_a_task(mgr):
    tid = await run(mgr, "scenario:heavy", make_agent("Heavy", budget={"max_usd": 100.0, "max_usd_per_day": 100.0}))
    await settle(lambda: state(tid) == "failed")
    assert store.get_task(tid)["reason"] == "budget_exceeded"


# --------------------------------------------------------------------------- #
# (f) chat turns and the handoff are exempt; (g) a token backend gets turns only
# --------------------------------------------------------------------------- #


async def test_a_chat_turn_and_a_handoff_are_never_held_to_the_defaults(mgr, monkeypatch):
    monkeypatch.setenv("DISPATCH_AGENTS_UNIFIED_DISPATCH", "1")
    for i in range(3):
        out = await mgr.run_chat_turn(thread_id="thr-bits", agent_seat="main", text="scenario:heavy", timeout_s=20)
        assert out.state == "succeeded", (i, out)
    b = launched_budget(mgr, "thr-bits", "openclaw")
    assert b.max_turns is None and b.max_usd is None and b.max_usd_per_day is None
    assert store.list_reservations() == []
    # 61 turns on one thread: never budget-stopped
    for _ in range(58):
        out = await mgr.run_chat_turn(thread_id="thr-bits", agent_seat="main", text="scenario:simple", timeout_s=20)
        assert out.state == "succeeded"
    text = await mgr.run_handoff(None, "lead", "scenario:heavy", 20)
    assert text == "done"
    row = store.list_tasks(kind="handoff")[0]
    hb = json.loads(row["spec_json"])["budget"]
    assert hb.get("max_turns") is None and hb.get("max_usd") is None and hb.get("max_usd_per_day") is None
    assert row["state"] == "succeeded"


async def test_an_openclaw_task_gets_the_turn_default_but_no_money_cap(data):
    m = await new_manager(openclaw=TokenOpenClaw)
    try:
        agent = make_agent("Seat", backend="openclaw", tool_allowlist=[])
        tid = await run(m, "scenario:long", agent)
        await settle(lambda: state(tid) == "running")
        b = launched_budget(m, tid, "openclaw")
        assert b.max_turns == 60
        assert b.max_usd is None and b.max_usd_per_day is None
        assert store.list_reservations() == []
        await m.cancel(store.get_task(tid))
    finally:
        await m.shutdown()


# --------------------------------------------------------------------------- #
# (c) the approval timeout; (d) a waiting task frees its slot; (i) the wait bound
# --------------------------------------------------------------------------- #


async def test_an_unanswered_approval_expires_and_the_task_goes_on(mgr):
    store.set_setting("agents.approval_timeout_s", 1)
    tid = await run(mgr, "scenario:approval", make_agent("Patient"))
    await settle(lambda: state(tid) == "awaiting_approval")
    await settle(lambda: state(tid) == "succeeded", timeout=6)
    await mgr.flush()
    [a] = store.list_approvals(tid)
    assert a["decision"] == "expired"
    assert store.get_task(tid)["approvals_denied"] == 1
    denied = [e for e in store.list_events(tid) if e["type"] == "tool_result"]
    assert denied and denied[0]["ok"] is False


async def test_a_task_waiting_for_approval_frees_its_slot(mgr):
    store.set_setting("agents.max_concurrent", 1)
    a = await run(mgr, "scenario:approval", make_agent("Asker"))
    await settle(lambda: state(a) == "awaiting_approval")
    b = await run(mgr, "scenario:simple", make_agent("Other"))
    await settle(lambda: state(b) == "succeeded")
    await mgr.flush()
    rid = store.list_approvals(a)[0]["request_id"]
    await mgr.respond_approval(rid, "allow", "once", "o1")
    await settle(lambda: state(a) == "succeeded")


async def test_an_answer_waits_for_a_free_slot(mgr):
    store.set_setting("agents.max_concurrent", 1)
    a = await run(mgr, "scenario:approval", make_agent("Asker2"))
    await settle(lambda: state(a) == "awaiting_approval")
    b = await run(mgr, "scenario:long", make_agent("Busy"))
    await settle(lambda: state(b) == "running")
    await mgr.flush()
    rid = store.list_approvals(a)[0]["request_id"]
    out = await mgr.respond_approval(rid, "allow", "once", "o1")
    assert out["state"] == "allow"
    await asyncio.sleep(0.2)
    assert state(a) == "awaiting_approval", "the answer is held until a slot is free"
    await mgr.cancel(store.get_task(b))
    await settle(lambda: state(a) == "succeeded")


async def test_no_more_than_twice_the_slots_may_wait_for_approval(data):
    posts = Posts()
    m = await new_manager(posts=posts)
    try:
        store.set_setting("agents.max_concurrent", 1)
        ids = []
        for i in range(3):
            ids.append(await run(m, "scenario:approval", make_agent(f"W{i}"), origin_thread_id="th1"))
        await settle(lambda: state(ids[0]) == "awaiting_approval" and state(ids[1]) == "awaiting_approval")
        await settle(lambda: state(ids[2]) == "succeeded")
        await m.flush()
        [third] = store.list_approvals(ids[2])
        assert third["decision"] == "deny" and third["decided_by"] is None
        assert any("too many" in text for _, text in posts.lines), posts.lines
        assert state(ids[0]) == "awaiting_approval" and state(ids[1]) == "awaiting_approval"
        for tid in ids[:2]:
            await m.cancel(store.get_task(tid))
    finally:
        await m.shutdown()


# --------------------------------------------------------------------------- #
# (e) reservations; (h) released on every terminal path and at boot
# --------------------------------------------------------------------------- #


async def test_two_starts_cannot_both_count_on_the_same_daily_budget(mgr):
    agent = make_agent("Capped", budget={"max_usd": 0.75, "max_usd_per_day": 1.0})
    first, second = await asyncio.gather(run(mgr, "scenario:long", agent), run(mgr, "scenario:long", agent))
    await settle(lambda: "running" in (state(first), state(second)))
    await asyncio.sleep(0.2)
    states = sorted([state(first), state(second)])
    assert states == ["queued", "running"], states
    res = store.list_reservations()
    assert len(res) == 1 and res[0]["usd"] == 0.75
    runner = first if state(first) == "running" else second
    waiter = second if runner == first else first
    await mgr.cancel(store.get_task(runner))
    await settle(lambda: state(waiter) == "running")
    assert [r["task_id"] for r in store.list_reservations()] == [waiter]
    await mgr.cancel(store.get_task(waiter))
    await settle(lambda: store.list_reservations() == [])


async def test_a_crashing_task_releases_its_reservation(mgr):
    tid = await run(mgr, "scenario:crash", make_agent("Crash", budget={"max_usd": 0.5, "max_usd_per_day": 1.0}))
    await settle(lambda: state(tid) == "failed")
    await mgr.flush()
    assert store.list_reservations() == []


async def test_finished_and_budget_stopped_tasks_release_theirs(mgr):
    ok = await run(mgr, "scenario:simple", make_agent("Ok"))
    stop = await run(mgr, "scenario:budget", make_agent("Stop", budget={"max_usd": 0.05}))
    await settle(lambda: state(ok) == "succeeded" and state(stop) == "failed")
    await mgr.flush()
    assert store.list_reservations() == []


async def test_a_stale_reservation_is_released_at_boot(mgr):
    store.try_reserve("t_gone", "ag_x", "o1", day_cap=10.0, want=2.0)
    assert len(store.list_reservations()) == 1
    m2 = await new_manager()
    try:
        assert store.list_reservations() == []
    finally:
        await m2.shutdown()


async def test_a_stale_pending_approval_is_expired_at_boot(mgr):
    store.create_task(task_id="t_old", agent_id="ag", agent_name="x", backend="fake", owner="o1",
                      state="failed", spec_json=json.dumps({"backend": "fake"}))
    store.run_batch([store.op_approval_add({"request_id": "rq_old", "task_id": "t_old", "tool": "Bash",
                                            "args_summary": "", "scopes": ["once"],
                                            "requested_at": store.now_iso()})])
    m2 = await new_manager()
    try:
        assert store.get_approval("rq_old")["decision"] == "expired"
    finally:
        await m2.shutdown()


async def test_the_support_summary_shows_open_reservations_and_waiting_approvals(mgr):
    a = await run(mgr, "scenario:approval", make_agent("Sum"))
    await settle(lambda: state(a) == "awaiting_approval")
    await mgr.flush()
    s = mgr.support_summary()
    assert s["reservations"]["open"] == 1
    assert s["approvals_waiting"] == 1
    assert s["limits"]["tasks_waiting_for_approval"] == 1
    await mgr.cancel(store.get_task(a))


# --------------------------------------------------------------------------- #
# (j) the settings API
# --------------------------------------------------------------------------- #


def test_an_admin_sets_the_default_and_the_next_task_carries_it(aw):
    aw.world.person(aw.owner, "Ada", "admin", pin="8888")
    ada = aw.caller(9)
    assert aw.world.login(ada, name="Ada", pin="8888").status_code == 200
    s = ada.get("/api/agents/settings").json()
    assert s["default_max_usd"] == 2.0 and s["default_max_turns"] == 60
    assert s["default_max_usd_per_day"] == 10.0 and s["approval_timeout_s"] == 1800
    r = ada.put("/api/agents/settings", json={"default_max_usd": 5})
    assert r.status_code == 200, r.text
    assert r.json()["default_max_usd"] == 5.0
    for bad in ({"default_max_usd": 0}, {"default_max_usd": 1001}, {"default_max_turns": 0},
                {"default_max_turns": 1001}, {"approval_timeout_s": 59}, {"approval_timeout_s": 86401},
                {"default_max_usd_per_day": 0.001}):
        assert ada.put("/api/agents/settings", json=bad).status_code == 422, bad
    agent = api_make_agent(aw.owner, "Defaulted", published=["*"], spec=api_spec(tool_allowlist=[]))
    t = aw.owner.post("/api/tasks", json={"agent_id": agent["id"], "prompt": "scenario:long"})
    assert t.status_code == 201, t.text
    tid = t.json()["task"]["id"]
    eventually(lambda: aw.mgr.backends["fake"].has_task(tid))
    assert aw.mgr.backends["fake"]._tasks[tid].spec.budget.max_usd == 5.0
    from app import accounts
    rows = accounts.recent_activity(category="audit", limit=50)
    assert any(r["kind"] == "agent_settings" and "default_max_usd" in (r["detail"] or "") for r in rows)
    aw.owner.post(f"/api/tasks/{tid}/cancel", json={})


def test_a_member_cannot_change_the_limits(aw):
    r = aw.eve.put("/api/agents/settings", json={"default_max_usd": 500})
    assert r.status_code == 403
    assert store.get_setting("agents.default_max_usd") == 2.0



# --------------------------------------------------------------------------- #
# INT-1: the CLI backend reports money only for a Claude preset
# --------------------------------------------------------------------------- #


def _cli_preset(kind="generic"):
    import sys
    from pathlib import Path
    fake = Path(__file__).parent / "agents" / "fixtures" / "fake_agent_cli.py"
    if kind == "claude":
        return {"id": "cl", "label": "Claude CLI", "kind": "claude", "executable": sys.executable}
    return {"id": "gen", "label": "Fake CLI", "kind": "generic", "executable": sys.executable,
            "args": ["-I", str(fake)]}


async def test_a_generic_cli_task_gets_the_turn_default_but_no_money_cap(data):
    from app.agents.cli_adapter import GenericCliBackend
    m = await new_manager()
    try:
        presets = [_cli_preset("generic"), _cli_preset("claude")]
        store.set_setting("agents.cli.presets", presets)
        m.register_backend("cli", GenericCliBackend(presets=lambda: presets))
        gen = AgentSpec(backend="cli", tool_allowlist=["Read"], backend_options={"preset": "gen"})
        cl = AgentSpec(backend="cli", tool_allowlist=["Read"], backend_options={"preset": "cl"})
        assert m.cost_reporting("cli", gen) == "none"
        assert m.cost_reporting("cli", cl) == "usd"          # the Claude CLI reports total_cost_usd
        assert m.cost_reporting("managed") == "usd"          # list_cost (static: not registered here)
        agent = make_agent("Cli", backend="cli", tool_allowlist=["Read"], backend_options={"preset": "gen"})
        tid = await run(m, "hello", agent)
        await settle(lambda: state(tid) != "queued")
        recorded = json.loads(store.get_task(tid)["spec_json"])["budget"]
        assert recorded["max_turns"] == 60
        assert recorded["max_usd"] is None and recorded["max_usd_per_day"] is None
        assert store.list_reservations() == []
        await settle(lambda: state(tid) in ("succeeded", "failed", "cancelled"), timeout=30)
        assert state(tid) == "succeeded", store.get_task(tid)
    finally:
        await m.shutdown()
