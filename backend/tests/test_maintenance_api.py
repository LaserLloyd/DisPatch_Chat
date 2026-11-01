"""/api/maintenance/* and /api/me/archive: Admin-only host control, plus one self-service value."""
from __future__ import annotations

import time
from datetime import datetime, timedelta

import pytest
from maint_helpers import iso, put

from app import maintenance

ADMIN_ROUTES = [
    ("GET", "/api/maintenance/status", None),
    ("GET", "/api/maintenance/storage", None),
    ("POST", "/api/maintenance/storage/refresh", {}),
    ("POST", "/api/maintenance/run", {"dry_run": True}),
    ("GET", "/api/maintenance/images/preview?mode=months&months=12", None),
    ("GET", "/api/maintenance/retention", None),
    ("PUT", "/api/maintenance/retention", {"system.retention.images.mode": "forever"}),
    ("POST", "/api/maintenance/images/restore", {}),
]


@pytest.fixture
def crew(world):
    owner = world.create_owner()
    world.person(owner, "Ada", "admin", password="admin-pass1")
    world.person(owner, "Max", "user", pin="5555")
    world.person(owner, "Bea", "basic", pin="6666")
    admin = world.client(("203.0.113.61", 41001))
    assert world.login(admin, name="Ada", password="admin-pass1").status_code == 200
    user = world.client(("203.0.113.62", 41002))
    assert world.login(user, name="Max", pin="5555").status_code == 200
    basic = world.client(("203.0.113.63", 41003))
    assert world.login(basic, name="Bea", pin="6666").status_code == 200
    stranger = world.client(("203.0.113.64", 41004))
    return type("Crew", (), {"world": world, "owner": owner, "admin": admin, "user": user, "basic": basic,
                             "stranger": stranger})


def _call(c, method, url, body):
    return c.request(method, url, json=body) if body is not None else c.request(method, url)


@pytest.mark.parametrize("method,url,body", ADMIN_ROUTES)
def test_users_basic_and_strangers_are_refused_everywhere(crew, method, url, body):
    for who in (crew.user, crew.basic):
        assert _call(who, method, url, body).status_code == 403, (method, url)
    assert _call(crew.stranger, method, url, body).status_code in (401, 403)


@pytest.mark.parametrize("method,url,body", ADMIN_ROUTES[:2] + ADMIN_ROUTES[3:6])
def test_admins_and_the_owner_reach_them(crew, method, url, body):
    for who in (crew.admin, crew.owner):
        r = _call(who, method, url, body)
        assert r.status_code == 200, (method, url, r.text)


def test_status_reports_the_schedule_and_the_last_run(crew):
    s = crew.admin.get("/api/maintenance/status").json()
    assert s["enabled"] is True and s["time"] == "04:30" and s["running"] is False and s["last_run"] is None
    assert s["images"] == {"mode": "forever", "months": 12, "trash_days": 14}
    assert datetime.fromisoformat(s["next_run"]) > datetime.now().astimezone()
    assert s["blocked"] in (None, "maintenance_marker")


def test_a_dry_run_answers_at_once_and_a_real_run_is_started_in_the_background(crew):
    dry = crew.admin.post("/api/maintenance/run", json={"dry_run": True, "steps": ["prune_activity"]}).json()
    assert dry["dry_run"] is True and [s["step"] for s in dry["steps"]] == ["prune_activity"]
    assert crew.admin.get("/api/maintenance/status").json()["last_run"] is None          # a dry run records nothing
    r = crew.admin.post("/api/maintenance/run", json={"steps": ["prune_activity", "prune_usage_daily"]})
    assert r.status_code == 202 and r.json() == {"started": True}
    for _ in range(100):
        last = crew.admin.get("/api/maintenance/status").json()["last_run"]
        if last:
            break
        time.sleep(0.05)
    assert last and last["ok"] and [s["step"] for s in last["steps"]] == ["prune_activity", "prune_usage_daily"]
    assert "maintenance.run" in str(crew.owner.get("/api/people/audit").json())


def test_a_bad_step_or_a_busy_run_is_refused(crew):
    assert crew.admin.post("/api/maintenance/run", json={"steps": ["nope"]}).status_code == 422
    assert maintenance._RUN_LOCK.acquire(blocking=False)
    try:
        assert crew.admin.post("/api/maintenance/run", json={}).status_code == 409
        assert crew.admin.post("/api/maintenance/run", json={"dry_run": True}).status_code == 409
    finally:
        maintenance._RUN_LOCK.release()


def test_a_run_waits_for_the_maintenance_marker(crew, tmp_path, monkeypatch):
    marker = tmp_path / "marker"
    marker.write_text("pid=1")
    monkeypatch.setenv("DISPATCH_MAINTENANCE_MARKER", str(marker))
    r = crew.admin.post("/api/maintenance/run", json={})
    assert r.status_code == 409 and "maintenance window" in r.json()["detail"]
    assert crew.admin.get("/api/maintenance/status").json()["blocked"] == "maintenance_marker"
    assert crew.admin.post("/api/maintenance/run", json={"dry_run": True}).status_code == 200      # read-only is fine


def test_storage_is_cached_and_a_refresh_walks_in_the_background(crew):
    root = crew.world.tmp
    put(root / "files" / "f.bin", b"z" * 4321)
    first = crew.admin.get("/api/maintenance/storage").json()
    assert first["measured_at"] is None and first["areas"] == {}                  # nothing walked yet, nothing guessed
    assert crew.admin.post("/api/maintenance/storage/refresh").status_code == 202
    for _ in range(100):
        got = crew.admin.get("/api/maintenance/storage").json()
        if got["measured_at"] and not got["scanning"]:
            break
        time.sleep(0.05)
    assert got["areas"]["files"]["bytes"] == 4321 and got["free_disk_bytes"] > 0 and "stray" in got


def test_the_image_preview_validates_and_counts(crew):
    root = crew.world.tmp
    p = put(root / "reactions" / "spent" / "joy" / "old.png")
    import os
    old = time.time() - 500 * 86400
    os.utime(p, (old, old))
    r = crew.admin.get("/api/maintenance/images/preview?mode=months&months=12")
    body = r.json()
    assert r.status_code == 200 and body["would_expire"]["count"] == 1 and body["would_expire"]["by_kind"]["reaction_spent"]["count"] == 1
    assert p.exists()                                                         # a preview never deletes
    assert crew.admin.get("/api/maintenance/images/preview?mode=forever").json()["would_expire"]["count"] == 0
    for q in ("mode=months&months=0", "mode=months&months=121", "mode=months", "mode=weekly"):
        assert crew.admin.get(f"/api/maintenance/images/preview?{q}").status_code == 422, q


def test_the_retention_setting_is_saved_validated_and_defaults_to_forever(crew):
    got = crew.admin.get("/api/maintenance/retention").json()
    assert got["values"]["system.retention.images.mode"] == "forever" and got["values"]["system.retention.images.months"] == 12
    assert got["defaults"]["system.retention.activity_days"] == 90
    ok = crew.admin.put("/api/maintenance/retention", json={"system.retention.images.mode": "months",
                                                            "system.retention.images.months": 18})
    assert ok.status_code == 200 and ok.json()["values"]["system.retention.images.months"] == 18
    assert maintenance.cfg("system.retention.images.mode") == "months"
    for bad in ({"system.retention.images.months": 0}, {"system.retention.images.months": 121},
                {"system.retention.images.mode": "sometimes"}, {"system.retention.activity_days": 1},
                {"system.maintenance.time": "25:00"}, {"system.public_url": "http://x"}, {"unknown.key": 1}, {}):
        r = crew.admin.put("/api/maintenance/retention", json=bad)
        assert r.status_code == 422, (bad, r.text)
    assert maintenance.cfg("system.retention.images.months") == 18             # a refused request changes nothing
    assert "retention.update" in str(crew.owner.get("/api/people/audit").json())


def test_expired_pictures_can_be_restored_through_the_api(crew):
    import os
    root = crew.world.tmp
    p = put(root / "reactions" / "spent" / "joy" / "old.png")
    old = time.time() - 500 * 86400
    os.utime(p, (old, old))
    assert crew.admin.put("/api/maintenance/retention", json={"system.retention.images.mode": "months"}).status_code == 200
    assert crew.admin.post("/api/maintenance/run", json={"steps": ["image_retention"]}).status_code == 202
    for _ in range(100):
        if not p.exists():
            break
        time.sleep(0.05)
    assert not p.exists()
    r = crew.admin.post("/api/maintenance/images/restore", json={}).json()
    assert r["restored"] == 1 and p.exists()
    assert crew.admin.post("/api/maintenance/images/restore", json={"date": "2026"}).status_code == 422


# --------------------------------------------------------------------------- #
# Archive my chats older than N months: a profile sets its own value, and nobody sets another's
# --------------------------------------------------------------------------- #


def test_a_profile_sets_only_its_own_archive_value(crew):
    assert crew.user.get("/api/me/archive").json() == {"months": None}
    assert crew.user.put("/api/me/archive", json={"months": 6}).json() == {"months": 6}
    assert crew.user.get("/api/me/archive").json() == {"months": 6}
    assert crew.admin.get("/api/me/archive").json() == {"months": None}        # the Admin's own value is untouched
    assert crew.basic.get("/api/me/archive").json() == {"months": None}
    for bad in (0, 121, -1, "x"):
        assert crew.user.put("/api/me/archive", json={"months": bad}).status_code == 422
    assert crew.user.put("/api/me/archive", json={"months": 6, "profile_id": "x"}).status_code == 422     # no way to name someone else
    assert crew.user.put("/api/me/archive", json={"months": None}).json() == {"months": None}
    assert crew.stranger.get("/api/me/archive").status_code in (401, 403)


def test_the_archive_step_uses_each_profiles_own_value_and_the_count_matches(crew):
    from maint_helpers import NOW, db

    con = db(type("S", (), {"root": crew.world.tmp}))
    from app import accounts
    ids = {p.name: p.id for p in accounts.list_profiles()}
    t_max, t_bea = "t-max", "t-bea"
    for tid, owner in ((t_max, ids["Max"]), (t_bea, ids["Bea"])):
        con.execute("INSERT INTO threads(id,bot_id,title,created_at,updated_at,owner_profile_id) VALUES(?,?,?,?,?,?)",
                    (tid, "alpha", "old", iso(400), iso(400), owner))
    assert crew.user.put("/api/me/archive", json={"months": 12}).status_code == 200
    assert crew.user.get("/api/me/archive?months=12").json()["would_archive"] == 1
    assert crew.basic.get("/api/me/archive?months=12").json()["would_archive"] == 1      # Bea may ask; nothing is set for her
    assert crew.user.get("/api/me/archive?months=0").status_code == 422
    maintenance.run_nightly(now=NOW, steps=["archive_chats"], sleep=0)
    assert {r[0] for r in con.execute("SELECT id FROM threads WHERE is_archived = 1")} == {t_max}
    assert crew.user.put("/api/me/archive", json={"months": None}).status_code == 200
    assert crew.user.get("/api/me/archive").json() == {"months": None}


# --------------------------------------------------------------------------- #
# The loop and the schedule
# --------------------------------------------------------------------------- #


def test_the_schedule_arithmetic():
    assert maintenance.parse_time("04:30") == (4, 30) and maintenance.parse_time("23:59") == (23, 59)
    for junk in ("", "25:00", "4", "ab:cd", None, "12:60"):
        assert maintenance.parse_time(junk) == (4, 30)
    now = datetime(2026, 11, 15, 5, 0).astimezone()
    assert maintenance.next_run_at(now, "04:30").day == 16
    assert maintenance.next_run_at(now, "06:00").day == 15
    assert maintenance.next_run_at(now, "05:00").day == 16              # not "now": strictly later


def test_due_logic(world, monkeypatch):
    from app import settings_store
    assert settings_store.set_many({"system.maintenance.time": "04:30"}).ok
    maintenance.connect().close()
    before = datetime(2026, 11, 15, 4, 0).astimezone()
    after = datetime(2026, 11, 15, 4, 31).astimezone()
    assert maintenance.due(before) is False
    assert maintenance.due(after) is True                               # never ran: due
    con = maintenance.connect()
    stamp = datetime(2026, 11, 15, 4, 30, 10).astimezone().isoformat()  # a run that began just after 04:30
    con.execute("INSERT INTO maintenance_runs(started,finished,step,rows,bytes,ms) VALUES(?,?,?,?,?,?)",
                (stamp, stamp, "run", 0, 0, 1))
    con.close()
    assert maintenance.due(after) is False                              # already ran after the scheduled time
    assert maintenance.due(after + timedelta(days=1)) is True           # and again the next day


def test_the_loop_runs_once_waits_for_the_marker_and_respects_the_switch(world, tmp_path, monkeypatch):
    import asyncio

    marker = tmp_path / "marker"
    monkeypatch.setenv("DISPATCH_MAINTENANCE_MARKER", str(marker))
    monkeypatch.setattr(maintenance, "STARTUP_DELAY", 0)
    monkeypatch.setattr(maintenance, "POLL_SECONDS", 0.05)
    monkeypatch.setattr(maintenance, "RETRY_SECONDS", 0.2)
    monkeypatch.setattr(maintenance, "STEPS", (("prune_activity", maintenance.step_prune_activity),))
    monkeypatch.setattr(maintenance, "STEP_NAMES", ("prune_activity",))
    from app import settings_store
    assert settings_store.set_many({"system.maintenance.time": "00:00"}).ok          # always past
    maintenance.connect().close()

    def runs():
        c = maintenance.connect()
        try:
            return c.execute("SELECT COUNT(*) FROM maintenance_runs WHERE step='run'").fetchone()[0]
        finally:
            c.close()

    async def go():
        marker.write_text("pid=1")
        maintenance.start(None)
        await asyncio.sleep(0.5)
        blocked = runs()
        marker.unlink()
        await asyncio.sleep(1.0)
        ran = runs()
        await asyncio.sleep(0.5)
        again = runs()
        await maintenance.stop()
        return blocked, ran, again

    blocked, ran, again = asyncio.run(go())
    assert blocked == 0                       # waited for the marker
    assert ran == 1 and again == 1            # ran once, and not again the same day

    async def off():
        assert settings_store.set_many({"system.maintenance.enabled": False}).ok
        c = maintenance.connect()
        c.execute("DELETE FROM maintenance_runs")
        c.close()
        maintenance.start(None)
        await asyncio.sleep(0.4)
        n = runs()
        await maintenance.stop()
        return n

    assert asyncio.run(off()) == 0


def test_start_and_stop_never_raise():
    maintenance.start(None)                   # no running loop: logged, not raised
    import asyncio
    asyncio.run(maintenance.stop())
