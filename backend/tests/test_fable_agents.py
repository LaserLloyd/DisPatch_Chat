"""4.3 Fable review: the smaller agent items the Opus code review left open.

1. an approval answer cannot take a slot while ``_kick`` is between its count check and its
   start (``max_concurrent`` is never exceeded)
2. ``send()`` / ``resume()`` release their reservation when the backend raises after it
5. Glob/Grep patterns whose ``{...}`` / ``[...]`` group holds a ``/`` or ``..`` are refused
6. the scheduler writes ``next_run`` BEFORE it dispatches (a crash in between cannot fire twice)
7. Managed Agents ``list_cost`` is read in the documented unit (integer-string cents)
8. ``/api/oidc/start`` is throttled per address
9. a role change re-checks idle default-profile tabs at once (``ws_manager.nudge``)
"""
from __future__ import annotations

import asyncio
import time
from datetime import timedelta

import pytest
from agents_world import aw, make_agent
from test_i43_agent_limits import SCRIPTS, UsdOpenClaw, data, new_manager, run, settle, state
from test_i43_agent_limits import make_agent as store_agent

from app import agents_store as store
from app import config
from app.agents import schedule_store, scheduler, tool_policy
from app.agents.fake import FakeBackend, Finish, Tool, WaitForSteer
from app.agents.managed_adapter import _usd_of
from app.agents.scheduler import Scheduler
from app.agents_service import Hooks, TaskManager

pytestmark = pytest.mark.timeout(60)


# --------------------------------------------------------------------------- #
# 1. _decide vs _kick: max_concurrent holds
# --------------------------------------------------------------------------- #


class _MaxSet(set):
    """``_live`` that remembers the most tasks it ever held at once."""
    peak = 0

    def add(self, x):
        super().add(x)
        type(self).peak = max(type(self).peak, len(self))


async def test_an_answer_cannot_take_a_slot_a_starting_task_already_counted_on(data):
    m = await new_manager()
    try:
        store.set_setting("agents.max_concurrent", 1)
        _MaxSet.peak = 0
        m._live = _MaxSet(m._live)
        m.backends["fake"].scripts["approval_long"] = [Tool("Bash", {"command": "echo hi"}, output="hi"),
                                                       WaitForSteer(), Finish("done")]
        a = await run(m, "scenario:approval_long", store_agent("Asker"))
        await settle(lambda: state(a) == "awaiting_approval")
        await m.flush()
        await asyncio.sleep(0.1)                 # the kick the freed slot spawned is done
        rid = store.list_approvals(a)[0]["request_id"]

        orig = m._reserve
        entered = asyncio.Event()
        loop = asyncio.get_running_loop()

        def slow_reserve(*args, **kw):
            loop.call_soon_threadsafe(entered.set)
            time.sleep(0.3)                      # _kick is past its count check, before _start
            return orig(*args, **kw)

        m._reserve = slow_reserve
        c_agent = store_agent("Busy")
        dispatching = asyncio.create_task(m.dispatch(agent=c_agent, prompt="scenario:long", owner="o1"))
        await asyncio.wait_for(entered.wait(), 5)
        await m.respond_approval(rid, "allow", "once", "o1")
        c = (await dispatching)["task_id"]
        m._reserve = orig
        await settle(lambda: state(c) == "running")
        await asyncio.sleep(0.2)
        assert _MaxSet.peak <= 1, f"{_MaxSet.peak} tasks ran at once with max_concurrent=1"
        assert state(a) == "awaiting_approval", "the answer waits for the busy slot"
        await m.cancel(store.get_task(c))
        await settle(lambda: state(a) == "running")
        assert _MaxSet.peak <= 1
        await m.cancel(store.get_task(a))
    finally:
        await m.shutdown()


# --------------------------------------------------------------------------- #
# 2. send()/resume(): a raise after the reservation releases it
# --------------------------------------------------------------------------- #


class _BreaksOnRefresh(FakeBackend):
    broken = False

    def update_budget(self, task_id, budget, spent):
        if self.broken:
            raise RuntimeError("backend fell over after the money was set aside")
        return super().update_budget(task_id, budget, spent)


async def _finished_capped_task(m):
    tid = await run(m, "scenario:simple", store_agent("Capped", budget={"max_usd": 0.5, "max_usd_per_day": 5.0}))
    await settle(lambda: state(tid) == "succeeded")
    await m.flush()
    assert store.list_reservations() == []
    return tid


@pytest.mark.parametrize("how", ["send", "resume"])
async def test_a_failed_continue_leaves_no_reservation(data, how):
    store.reset_for_tests()
    m = TaskManager(config.DATA_DIR, hooks=Hooks())
    be = _BreaksOnRefresh(scripts=dict(SCRIPTS))
    m.register_backend("fake", be)
    m.register_backend("openclaw", UsdOpenClaw(scripts=dict(SCRIPTS)))
    await m.startup()
    try:
        tid = await _finished_capped_task(m)
        be.broken = True
        row = store.get_task(tid)
        with pytest.raises(RuntimeError):
            if how == "send":
                await m.send(row, "again")
            else:
                await m.resume(row, "again")
        await m.flush()
        assert store.list_reservations() == [], "the reservation outlived a continue that never ran"
        assert tid not in m._live
    finally:
        await m.shutdown()


# --------------------------------------------------------------------------- #
# 5. Glob/Grep groups
# --------------------------------------------------------------------------- #


@pytest.fixture
def root(tmp_path):
    r = tmp_path / "ws"
    (r / "src").mkdir(parents=True)
    return r


@pytest.mark.parametrize("tool,inp", [
    ("Glob", {"pattern": "{/etc,src}/*"}),
    ("Glob", {"pattern": "src/{a,../../x}/*.py"}),
    ("Glob", {"pattern": "[/]etc/passwd"}),
    ("Grep", {"pattern": "x", "glob": "{a,/etc/*}"}),
    ("Grep", {"pattern": "x", "glob": "*.{py,..}"}),
    ("Glob", {"pattern": "src/[\\]x"}),
])
def test_a_pattern_group_with_a_slash_or_dotdot_is_refused(root, tool, inp):
    d = tool_policy.decide(tool, inp, root, [])
    assert d.verdict == "deny", d


@pytest.mark.parametrize("tool,inp", [
    ("Glob", {"pattern": "**/*.py"}),
    ("Glob", {"pattern": "src/{a,b}.py"}),
    ("Glob", {"pattern": "src/[ab]*.txt"}),
    ("Grep", {"pattern": "x", "glob": "*.{py,md}"}),
    ("Grep", {"pattern": "x"}),
])
def test_plain_patterns_still_pass(root, tool, inp):
    assert tool_policy.decide(tool, inp, root, []).verdict == "allow"


# --------------------------------------------------------------------------- #
# 6. scheduler: next_run is written before the dispatch
# --------------------------------------------------------------------------- #


@pytest.fixture
def sw(aw):
    schedule_store.reset_for_tests()
    aw.sch = Scheduler(manager=lambda: aw.mgr)
    return aw


def test_next_run_is_claimed_before_the_task_is_created(sw):
    a = make_agent(sw.owner, "Hourly", published=["*"])
    r = sw.eve.post("/api/agents/schedules", json={"agent_id": a["id"], "prompt": "p",
                                                    "rule": {"every_minutes": 60}, "tz": "UTC"})
    assert r.status_code == 201, r.text
    sid = r.json()["schedule"]["id"]
    due = scheduler.parse_iso(schedule_store.get(sid)["next_run"])
    seen: list[str | None] = []
    real = sw.mgr.dispatch

    async def spy(**kw):
        seen.append(schedule_store.get(sid)["next_run"])      # what a crash right now would leave
        return await real(**kw)

    sw.mgr.dispatch = spy
    try:
        res = sw.owner.portal.call(sw.sch.tick, due + timedelta(seconds=5))
    finally:
        sw.mgr.dispatch = real
    assert [x["outcome"] for x in res] == ["started"]
    assert seen and scheduler.parse_iso(seen[0]) > due + timedelta(seconds=5), \
        "next_run was still the due slot while the task was being created"
    row = schedule_store.get(sid)
    assert row["last_task_id"] and row["status"] == "ok"
    assert scheduler.parse_iso(row["next_run"]) == due + timedelta(seconds=5) + timedelta(minutes=60)


# --------------------------------------------------------------------------- #
# 7. Managed Agents list_cost unit
# --------------------------------------------------------------------------- #


def test_list_cost_is_read_as_documented_minor_units():
    # claude-api skill, managed-agents-core.md: "list_cost (... as {amount, currency} with the
    # amount an integer string in minor units - cents)"; "2500" is $25.00.
    assert _usd_of({"amount": "2500", "currency": "USD"}) == 25.0
    assert _usd_of({"amount": "50", "currency": "USD"}) == 0.5
    assert _usd_of({"amount": 7, "currency": "USD"}) == 0.07
    # an undocumented decimal form is read as dollars (over-counting stops a run early)
    assert _usd_of({"amount": "25.00", "currency": "USD"}) == 25.0
    assert _usd_of({"amount": "12", "currency": "EUR"}) is None
    assert _usd_of(None) is None


# --------------------------------------------------------------------------- #
# 8. /api/oidc/start is throttled per address
# --------------------------------------------------------------------------- #


def test_oidc_start_is_throttled_per_address(world, monkeypatch):
    import httpx
    from test_i43_oidc import PUBLIC, FakeIdP, _client, _configure

    from app import auth_attempts, oidc, secret_store
    monkeypatch.setenv("DISPATCH_PUBLIC_URL", PUBLIC)
    monkeypatch.setenv("DISPATCH_SECRET_STORE", "file")
    secret_store.reset_for_tests()
    fake = FakeIdP()
    monkeypatch.setattr(oidc, "_transport", httpx.MockTransport(fake.handler))
    oidc.reset_for_tests()
    auth_attempts.reset_for_tests()
    try:
        owner = _client(world)
        r = owner.post("/api/auth/setup", json={"name": "Olive", "password": "owner-pass1", "new_pin": "2468"})
        assert r.status_code == 200, r.text
        _configure(owner)
        flood = _client(world, addr=("198.51.100.7", 40000))
        codes = [flood.get("/api/oidc/start", follow_redirects=False).status_code for _ in range(35)]
        assert codes[:30] == [303] * 30, codes
        assert codes[30:] == [429] * 5, codes
        other = _client(world, addr=("198.51.100.99", 40000))
        assert other.get("/api/oidc/start", follow_redirects=False).status_code == 303, \
            "another address is not held back"
        assert owner.get("/api/oidc/start", follow_redirects=False).status_code == 303
    finally:
        auth_attempts.reset_for_tests()
        oidc.reset_for_tests()
        secret_store.reset_for_tests()


# --------------------------------------------------------------------------- #
# 9. a role change nudges idle default-profile tabs
# --------------------------------------------------------------------------- #


def test_a_role_change_nudges_the_default_profile_sockets(world, monkeypatch):
    from test_i43_ws_default_lapse import _lobby

    from app import accounts, main
    from app.contracts.permissions import Role
    lobby = _lobby(world, "user")
    accounts.update_profile(lobby["pid"], role=Role.BASIC)      # Basic -> User keeps the chats
    calls = []
    monkeypatch.setattr(main.manager, "nudge", lambda: calls.append(1))
    r = lobby["owner"].post(f"/api/people/{lobby['pid']}/role", json={"role": "user"})
    assert r.status_code == 200, r.text
    assert calls, "a role change did not re-check the default-profile tabs"
