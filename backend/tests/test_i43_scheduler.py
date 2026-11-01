"""4.3 (#13): recurring agent schedules - the rule maths, the fire checks, the routes and I10.

Runs against the real app (``aw``: an Owner, Members Eve and Fay holding ``dispatch_agents``,
Max without it) with the scripted fake backend. The clock is frozen: every tick is given its
``now``; nothing waits for the 30-second loop.

(c) a daily_at schedule fires once, writes last_run, and fires ONCE after a 3-day outage
(d) an owner who lost dispatch_agents -> skipped + a notice
(e) a deleted owner -> disabled, not run; a deleted chat -> disabled + a notice to the owner
(f) DST: daily_at 02:30 in a spring-forward zone fires once that day
plus the route rules (own schedules only, 404 for someone else's, an Admin may only switch
another's off or delete it, bounds) and invariant I10.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from agents_world import aw, make_agent

from app import accounts, invariants
from app import agents_store as store
from app.agents import schedule_store, scheduler
from app.agents.scheduler import RuleError, Scheduler, next_after, parse_rule

pytestmark = pytest.mark.timeout(90)


# --------------------------------------------------------------------------- #
# Rules (pure)
# --------------------------------------------------------------------------- #


def test_rule_bounds():
    assert parse_rule({"every_minutes": 15}) == {"every_minutes": 15}
    assert parse_rule({"daily_at": "8:05".zfill(5)}) == {"daily_at": "08:05"}
    assert parse_rule({"weekly_day_at": {"day": "Monday", "at": "09:30"}}) == {
        "weekly_day_at": {"day": "mon", "at": "09:30"}}
    for bad in ({"every_minutes": 14}, {"every_minutes": True}, {"every_minutes": "30"},
                {"daily_at": "25:00"}, {"daily_at": "8"}, {"weekly_day_at": {"day": "xyz", "at": "08:00"}},
                {"every_minutes": 30, "daily_at": "08:00"}, {}, {"cron": "* * * * *"}):
        with pytest.raises(RuleError):
            parse_rule(bad)
    with pytest.raises(RuleError):
        scheduler.zone("Mars/Olympus")
    with pytest.raises(RuleError):
        scheduler.zone("../../etc/passwd")


def test_daily_and_weekly_next_runs_in_the_zone():
    after = datetime(2026, 10, 31, 0, 0, tzinfo=UTC)          # 09:00 in Tokyo
    assert next_after({"daily_at": "08:00"}, "Asia/Tokyo", after) == datetime(2026, 10, 31, 23, 0, tzinfo=UTC)
    assert next_after({"daily_at": "10:00"}, "Asia/Tokyo", after) == datetime(2026, 10, 31, 1, 0, tzinfo=UTC)
    # 2026-10-31 is a Saturday; the next Monday 08:00 Tokyo is 2026-11-01 23:00 UTC
    assert next_after({"weekly_day_at": {"day": "mon", "at": "08:00"}}, "Asia/Tokyo", after) == \
        datetime(2026, 11, 1, 23, 0, tzinfo=UTC)
    assert next_after({"every_minutes": 30}, "UTC", after) == after + timedelta(minutes=30)


def test_f_dst_a_time_in_the_spring_forward_gap_fires_once_that_day():
    tz = "America/New_York"                                   # 2026-03-08: 02:00 -> 03:00
    t = datetime(2026, 3, 7, 12, 0, tzinfo=UTC)
    fires = []
    for _ in range(4):
        t = next_after({"daily_at": "02:30"}, tz, t)
        fires.append(t)
    days = [f.astimezone(scheduler.zone(tz)).date().isoformat() for f in fires]
    assert days == ["2026-03-08", "2026-03-09", "2026-03-10", "2026-03-11"], "exactly once per day"
    assert fires[0] == datetime(2026, 3, 8, 7, 30, tzinfo=UTC)  # = 03:30 EDT, the shifted instant
    assert fires[1] == datetime(2026, 3, 9, 6, 30, tzinfo=UTC)  # 02:30 EDT


def test_f_dst_an_ambiguous_fall_back_time_fires_once():
    tz = "America/New_York"                                   # 2026-11-01: 02:00 -> 01:00
    t = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
    a = next_after({"daily_at": "01:30"}, tz, t)
    b = next_after({"daily_at": "01:30"}, tz, a)
    assert a == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)       # the first 01:30 (EDT)
    assert b.astimezone(scheduler.zone(tz)).date().isoformat() == "2026-11-02"


# --------------------------------------------------------------------------- #
# The app: routes + the tick
# --------------------------------------------------------------------------- #


@pytest.fixture
def sw(aw):
    """``aw`` with a scheduler of its own (main.py mounts the schedules router)."""
    schedule_store.reset_for_tests()
    aw.sch = Scheduler(manager=lambda: aw.mgr)
    return aw


def tick(sw, now):
    return sw.owner.portal.call(sw.sch.tick, now)


def count_tasks(agent_id):
    with store.connect() as con:
        return con.execute("SELECT COUNT(*) FROM tasks WHERE agent_id=?", (agent_id,)).fetchone()[0]


def create(client, **body):
    return client.post("/api/agents/schedules", json=body)


def settle(sw):
    sw.owner.portal.call(sw.mgr.flush)


def test_c_daily_fires_once_writes_last_run_and_catches_up_once(sw):
    a = make_agent(sw.owner, "Daily", published=["*"])
    thread = sw.eve.post("/api/threads", json={"bot_id": "main"}).json()["id"]
    r = create(sw.eve, agent_id=a["id"], prompt="daily summary", rule={"daily_at": "08:00"},
               tz="Asia/Tokyo", thread_id=thread)
    assert r.status_code == 201, r.text
    s = r.json()["schedule"]
    assert s["own"] and s["prompt"] == "daily summary" and len(s["upcoming"]) == 3
    sid = s["id"]
    due = scheduler.parse_iso(schedule_store.get(sid)["next_run"])
    before = due - timedelta(minutes=1)
    assert tick(sw, before) == []
    res = tick(sw, due + timedelta(seconds=5))
    assert [x["outcome"] for x in res] == ["started"]
    row = schedule_store.get(sid)
    assert row["last_run"] == scheduler.utc_iso(due + timedelta(seconds=5)) and row["last_task_id"]
    assert row["next_run"] == scheduler.utc_iso(due + timedelta(days=1))
    assert tick(sw, due + timedelta(seconds=40)) == [], "the same slot never fires twice"
    settle(sw)
    task = store.get_task(row["last_task_id"])
    assert task["owner"] == sw.eve_id and task["origin_thread_id"] == thread and task["prompt"] == "daily summary"
    assert count_tasks(a["id"]) == 1
    # --- a 3-day outage: one catch-up run, not three
    later = due + timedelta(days=3, hours=1)
    res = tick(sw, later)
    assert [x["outcome"] for x in res] == ["started"]
    assert tick(sw, later + timedelta(seconds=30)) == []
    assert count_tasks(a["id"]) == 2
    assert scheduler.parse_iso(schedule_store.get(sid)["next_run"]) > later


def test_d_an_owner_who_lost_dispatch_agents_is_skipped_with_a_notice(sw):
    a = make_agent(sw.owner, "Lost", published=["*"])
    thread = sw.eve.post("/api/threads", json={"bot_id": "main"}).json()["id"]
    sid = create(sw.eve, agent_id=a["id"], prompt="p", rule={"every_minutes": 15},
                 thread_id=thread).json()["schedule"]["id"]
    accounts.update_profile(sw.eve_id, grants=[])
    now = scheduler.parse_iso(schedule_store.get(sid)["next_run"]) + timedelta(seconds=1)
    res = tick(sw, now)
    assert [(x["outcome"], x["why"]) for x in res] == [("skipped", "no_dispatch")]
    row = schedule_store.get(sid)
    assert row["enabled"] and row["status"] == "skipped" and "dispatch_agents" in row["status_detail"]
    assert row["last_task_id"] is None and count_tasks(a["id"]) == 0
    assert scheduler.parse_iso(row["next_run"]) > now, "skipped, not stuck: the next slot is planned"
    texts = [t for tid, t, _ in sw.notes.posts if tid == thread]
    assert any("Scheduled task not run" in t for t in texts), "the owner hears why, in its own chat"
    assert any(k == "schedule" for k, _, _ in sw.notes.pushes)
    n_posts = len(sw.notes.posts)
    tick(sw, now + timedelta(minutes=16))
    assert len(sw.notes.posts) == n_posts, "the same notice is not repeated every slot"


def test_e_a_deleted_owner_disables_the_schedule_and_nothing_runs(sw):
    a = make_agent(sw.owner, "Orphan", published=["*"])
    sid = create(sw.fay, agent_id=a["id"], prompt="p", rule={"every_minutes": 15}).json()["schedule"]["id"]
    fay = accounts.get_by_name("Fay")
    accounts.delete_profile(fay.id)
    now = scheduler.parse_iso(schedule_store.get(sid)["next_run"]) + timedelta(seconds=1)
    res = tick(sw, now)
    assert [(x["outcome"], x["why"]) for x in res] == [("disabled", "owner_deleted")]
    row = schedule_store.get(sid)
    assert not row["enabled"] and row["next_run"] is None and count_tasks(a["id"]) == 0


def test_e_a_deleted_chat_disables_the_schedule_with_a_notice(sw):
    a = make_agent(sw.owner, "Chatless", published=["*"])
    thread = sw.eve.post("/api/threads", json={"bot_id": "main"}).json()["id"]
    sid = create(sw.eve, agent_id=a["id"], prompt="p", rule={"every_minutes": 15},
                 thread_id=thread).json()["schedule"]["id"]
    sw.owner.portal.call(sw.world.main.db.delete_thread, thread)
    pushes = len(sw.notes.pushes)
    now = scheduler.parse_iso(schedule_store.get(sid)["next_run"]) + timedelta(seconds=1)
    res = tick(sw, now)
    assert [(x["outcome"], x["why"]) for x in res] == [("disabled", "thread_deleted")]
    row = schedule_store.get(sid)
    assert not row["enabled"] and "chat was deleted" in row["status_detail"]
    assert len(sw.notes.pushes) == pushes + 1 and count_tasks(a["id"]) == 0
    acts = accounts.recent_activity(sw.eve_id, category="audit", limit=50)
    assert any(r["kind"] == "agent_schedule_disabled" and r["subject"] == sid for r in acts)


def test_the_tick_starts_at_most_max_concurrent_tasks(sw):
    store.set_setting("agents.max_concurrent", 2)
    a = make_agent(sw.owner, "Many", published=["*"])
    sids = [create(sw.eve, agent_id=a["id"], prompt=f"p{i}", rule={"every_minutes": 15}).json()["schedule"]["id"]
            for i in range(3)]
    now = max(scheduler.parse_iso(schedule_store.get(s)["next_run"]) for s in sids) + timedelta(seconds=1)
    assert [x["outcome"] for x in tick(sw, now)] == ["started", "started"]
    assert [x["outcome"] for x in tick(sw, now + timedelta(seconds=30))] == ["started"]


def test_routes_own_schedules_only_and_admins_may_only_switch_off(sw):
    a = make_agent(sw.owner, "Shared", published=["*"])
    eve_thread = sw.eve.post("/api/threads", json={"bot_id": "main"}).json()["id"]
    sid = create(sw.eve, agent_id=a["id"], prompt="secret plan", rule={"every_minutes": 60},
                 thread_id=eve_thread).json()["schedule"]["id"]
    # another Member: not listed, 404 on every verb
    assert sw.fay.get("/api/agents/schedules").json()["schedules"] == []
    assert sw.fay.patch(f"/api/agents/schedules/{sid}", json={"enabled": False}).status_code == 404
    assert sw.fay.delete(f"/api/agents/schedules/{sid}").status_code == 404
    # no dispatch_agents at all: 403
    assert sw.max.get("/api/agents/schedules").status_code == 403
    assert sw.kid.get("/api/agents/schedules").status_code == 403
    # a schedule never names another profile's chat
    r = create(sw.fay, agent_id=a["id"], prompt="x", rule={"every_minutes": 60}, thread_id=eve_thread)
    assert r.status_code == 404
    # the Admin sees it with scope=all, without its prompt or chat
    rows = sw.owner.get("/api/agents/schedules?scope=all").json()["schedules"]
    row = next(x for x in rows if x["id"] == sid)
    assert row["own"] is False and row["prompt"] is None and row["thread_id"] is None
    assert sw.owner.get("/api/agents/schedules").json()["schedules"] == [], "default scope is own"
    # ... may not edit it, nor switch it back on
    for body in ({"prompt": "do something else"}, {"thread_id": None}, {"rule": {"every_minutes": 15}},
                 {"enabled": True}, {"enabled": False, "prompt": "x"}):
        r = sw.owner.patch(f"/api/agents/schedules/{sid}", json=body)
        assert r.status_code == 403 and r.json()["error"]["code"] == "schedule_owner_only", body
    assert schedule_store.get(sid)["prompt"] == "secret plan"
    # ... may switch it off
    r = sw.owner.patch(f"/api/agents/schedules/{sid}", json={"enabled": False})
    assert r.status_code == 200 and r.json()["schedule"]["enabled"] is False
    # the owner may switch it back on and edit it
    r = sw.eve.patch(f"/api/agents/schedules/{sid}", json={"enabled": True, "prompt": "new plan"})
    assert r.status_code == 200 and r.json()["schedule"]["prompt"] == "new plan"
    assert r.json()["schedule"]["next_run"]
    # ... and an Admin may delete it
    assert sw.owner.delete(f"/api/agents/schedules/{sid}").status_code == 200
    assert schedule_store.get(sid) is None


def test_route_bounds(sw):
    a = make_agent(sw.owner, "Bounds", published=["*"])
    r = create(sw.eve, agent_id=a["id"], prompt="p", rule={"every_minutes": 5})
    assert r.status_code == 422 and r.json()["error"]["code"] == "schedule_invalid"
    r = create(sw.eve, agent_id=a["id"], prompt="p", rule={"daily_at": "08:00"}, tz="Nowhere/Land")
    assert r.status_code == 422
    r = create(sw.eve, agent_id="agent-that-does-not-exist", prompt="p", rule={"every_minutes": 15})
    assert r.status_code == 404
    hidden = make_agent(sw.owner, "OwnerOnly", published=[])
    assert create(sw.eve, agent_id=hidden["id"], prompt="p", rule={"every_minutes": 15}).status_code == 404
    for i in range(scheduler.MAX_PER_PROFILE):
        assert create(sw.eve, agent_id=a["id"], prompt=f"p{i}", rule={"every_minutes": 15}).status_code == 201
    r = create(sw.eve, agent_id=a["id"], prompt="one too many", rule={"every_minutes": 15})
    assert r.status_code == 409 and r.json()["error"]["code"] == "schedule_limit"


def test_cli_presets_are_the_protected_admins_alone(sw, tmp_path):
    import sys
    good = {"presets": [{"id": "fake", "kind": "generic", "executable": sys.executable, "args": ["-V"]}]}
    # a second, unprotected Admin
    sw.world.person(sw.owner, "Ada", "admin", pin="8888")
    ada = sw.caller(7)
    assert sw.world.login(ada, name="Ada", pin="8888").status_code == 200
    for c in (ada,):
        assert c.get("/api/agents/cli-presets").status_code == 403
        r = c.put("/api/agents/cli-presets", json=good)
        assert r.status_code == 403
    assert store.get_setting("agents.cli.presets") == []
    for c in (sw.eve, sw.kid):
        assert c.put("/api/agents/cli-presets", json=good).status_code == 403
    r = sw.owner.put("/api/agents/cli-presets", json=good)
    assert r.status_code == 200, r.text
    assert sw.owner.get("/api/agents/cli-presets").json()["presets"][0]["executable"] == sys.executable
    bad = {"presets": [{"id": "x", "kind": "generic", "executable": "sh"}]}
    r = sw.owner.put("/api/agents/cli-presets", json=bad)
    assert r.status_code == 422 and r.json()["error"]["code"] == "cli_preset_invalid"


def test_i10_flags_enabled_schedules_with_a_dead_owner_or_unusable_agent(sw):
    a = make_agent(sw.owner, "Inv", published=["*"])
    private = make_agent(sw.owner, "Private", published=[])
    ok = create(sw.eve, agent_id=a["id"], prompt="p", rule={"every_minutes": 15}).json()["schedule"]["id"]
    assert [v for v in invariants.check() if v.code == "I10"] == []
    # forged rows (the routes refuse both): another profile's private agent, and a dead owner
    bad1 = schedule_store.create(agent_id=private["id"], owner_profile=sw.eve_id, prompt="p",
                                 rule={"every_minutes": 15}, tz="UTC", enabled=True)
    bad2 = schedule_store.create(agent_id=a["id"], owner_profile="ghost", prompt="p",
                                 rule={"every_minutes": 15}, tz="UTC", enabled=True)
    off = schedule_store.create(agent_id=a["id"], owner_profile="ghost", prompt="p",
                                rule={"every_minutes": 15}, tz="UTC", enabled=False)
    got = {v.subject for v in invariants.check() if v.code == "I10"}
    assert got == {bad1["id"], bad2["id"]} and ok not in got and off["id"] not in got
    # seeded (non-live) worlds work too
    w = invariants.World(profiles=accounts.list_profiles(),
                         extra={"schedules": [{"id": "s1", "owner_profile": "nope", "agent_id": a["id"]}],
                                "agents": [store.get_agent(a["id"])]})
    assert [v.subject for v in invariants._i10(w)] == ["s1"]


async def test_run_loop_is_cancellable_and_survives_a_failing_tick():
    calls = []

    class Boom(Scheduler):
        async def tick(self, now=None):
            calls.append(1)
            raise RuntimeError("boom")

    task = asyncio.create_task(scheduler.run_loop(0.01, scheduler=Boom(manager=lambda: None)))
    await asyncio.sleep(0.08)
    assert len(calls) >= 2, "a failing tick does not stop the loop"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()


def test_lifespan_starts_the_loop_after_the_agents_service_and_cancels_it_at_shutdown(aw):
    """INT-1 wiring: one loop per app instance (event loop), gone after shutdown."""
    from fastapi.testclient import TestClient
    main = aw.world.main
    before = set(main._schedule_tasks)
    with TestClient(main.app, base_url="http://localhost") as c:
        mine = [loop for loop in main._schedule_tasks if loop not in before]
        assert len(mine) == 1
        task = main._schedule_tasks[mine[0]]
        assert task.get_name() == "agent-scheduler"
        assert c.portal.call(lambda: asyncio.sleep(0)) is None
        assert not task.done()
    assert mine[0] not in main._schedule_tasks
    assert task.cancelled() or task.done()


def test_a_schedule_push_reaches_the_schedules_owner(sw, monkeypatch):
    """INT-1: the scheduler pings kind "schedule" with ref "schedule:<id>"; push accepts the
    kind and main routes the ping to the schedule's owner (not to a chat that does not exist)."""
    from app import push
    assert "schedule" in push.KINDS
    a = make_agent(sw.owner, "Pinged", published=["*"])
    sid = create(sw.eve, agent_id=a["id"], prompt="p", rule={"every_minutes": 15}).json()["schedule"]["id"]
    seen = []

    async def fake_notify(kind, thread_id, title, *, owner=None):
        seen.append((kind, thread_id, owner))
        return 1
    monkeypatch.setattr(push, "notify", fake_notify)
    main = sw.world.main
    assert sw.owner.portal.call(main._notify_thread_owner, "schedule", f"schedule:{sid}", "t") == 1
    assert seen == [("schedule", f"schedule:{sid}", sw.eve_id)]
    assert sw.owner.portal.call(main._notify_thread_owner, "schedule", "schedule:nope", "t") == 0
