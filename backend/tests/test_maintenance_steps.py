"""The prune, archive, orphan and SQLite steps of the daily maintenance task (BUILD-PLAN-v2 1.13.3).

Every prune removes only rows older than its window (checked one day inside and one day outside),
and a second run changes nothing. Neutral fixtures, scratch folders only.
"""
from __future__ import annotations

import threading
import time

import pytest
from maint_helpers import (
    NOW,
    day,
    db,
    ensure_archive_column,
    epoch,
    iso,
    put,
    run,
    set_message_time,
    set_setting,
    set_thread_time,
)
from profiles_fixtures import scratch

from app import maintenance
from app.contracts.permissions import Role


def _ids(con, sql, *p):
    return sorted(r[0] for r in con.execute(sql, p))


# --------------------------------------------------------------------------- #
# Log and event tables
# --------------------------------------------------------------------------- #


def test_activity_and_audit_rows_are_pruned_at_their_own_windows(scratch):
    con = db(scratch)
    rows = [("keep-act", "activity", 89), ("gone-act", "activity", 91),
            ("keep-aud", "audit", 399), ("gone-aud", "audit", 401), ("keep-aud90", "audit", 120)]
    for kind, cat, days in rows:
        con.execute("INSERT INTO activity(ts,category,kind,subject) VALUES(?,?,?,?)", (iso(days), cat, kind, ""))
    out = run(["prune_activity"])
    assert out["prune_activity"]["rows"] == 2
    assert _ids(con, "SELECT kind FROM activity") == ["keep-act", "keep-aud", "keep-aud90"]
    assert run(["prune_activity"])["prune_activity"]["rows"] == 0          # idempotent


def test_the_windows_come_from_the_settings(scratch):
    con = db(scratch)
    con.execute("INSERT INTO activity(ts,category,kind,subject) VALUES(?,?,?,?)", (iso(20), "activity", "x", ""))
    set_setting(**{"system.retention.activity_days": 7})
    assert run(["prune_activity"])["prune_activity"]["rows"] == 1


def test_usage_daily_keeps_45_days(scratch):
    con = db(scratch)
    for d, k in ((44, "keep"), (46, "gone")):
        con.execute("INSERT INTO usage_daily(subject,day,kind,amount) VALUES('machine',?,?,1)", (day(d), k))
    assert run(["prune_usage_daily"])["prune_usage_daily"]["rows"] == 1
    assert _ids(con, "SELECT kind FROM usage_daily") == ["keep"]


def test_transcript_seen_keeps_180_days(scratch):
    t = scratch.thread(None, "pack_otter", "mirror")
    con = db(scratch)
    for i, d in enumerate((179, 181)):
        con.execute("INSERT INTO transcript_seen(item_id,thread_id,created_at) VALUES(?,?,?)", (f"i{i}", t, iso(d)))
    assert run(["prune_transcript_seen"])["prune_transcript_seen"]["rows"] == 1
    assert _ids(con, "SELECT item_id FROM transcript_seen") == ["i0"]


def _task(con, tid, state, days):
    con.execute(
        "INSERT INTO tasks(task_id,agent_id,backend,owner,state,spec_json,created_at,finished_at) "
        "VALUES(?,?,?,?,?,?,?,?)", (tid, "a1", "b", "", state, "{}", iso(days), iso(days)))
    for seq in range(3):
        con.execute("INSERT INTO task_events(task_id,seq,ts,type,data) VALUES(?,?,?,?,?)",
                    (tid, seq, epoch(days), "text", "{}"))


def test_task_events_of_finished_tasks_older_than_90_days_go(scratch):
    con = db(scratch)
    _task(con, "fresh", "succeeded", 89)
    _task(con, "old", "failed", 91)
    _task(con, "old-running", "running", 200)              # not finished: its events are kept
    con.execute("INSERT INTO task_events(task_id,seq,ts,type,data) VALUES('ghost',0,?, 't','{}')", (epoch(120),))
    out = run(["prune_task_events"])
    assert out["prune_task_events"]["rows"] == 3 + 1
    assert _ids(con, "SELECT DISTINCT task_id FROM task_events") == ["fresh", "old-running"]
    assert run(["prune_task_events"])["prune_task_events"]["rows"] == 0


def test_job_events_and_feedback_of_closed_jobs_only(scratch):
    t = scratch.thread(None, "pack_otter", "board")
    con = db(scratch)
    for jid, state in (("j-open", "yes"), ("j-closed", "applied")):
        con.execute(
            "INSERT INTO jobs(job_id,thread_id,url,title,first_seen,last_seen,state,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)", (jid, t, "http://x/" + jid, jid, iso(300), iso(300), state, iso(300), iso(300)))
        for n, d in (("a", 59), ("b", 61), ("c", 179), ("d", 181)):
            con.execute("INSERT INTO job_events(id,thread_id,job_id,type,actor,created_at) VALUES(?,?,?,?,?,?)",
                        (f"{jid}-{n}", t, jid, "comment", "system", iso(d)))
            con.execute("INSERT INTO job_feedback(id,thread_id,job_id,signal,actor,created_at) VALUES(?,?,?,?,?,?)",
                        (f"{jid}-{n}", t, jid, "vote_yes", "system", iso(d)))
    run(["prune_job_events", "prune_job_feedback"])
    ev = _ids(con, "SELECT id FROM job_events")
    fb = _ids(con, "SELECT id FROM job_feedback")
    assert ev == sorted(["j-open-a", "j-open-b", "j-open-c", "j-open-d", "j-closed-a"])
    assert fb == sorted(["j-open-a", "j-open-b", "j-open-c", "j-open-d", "j-closed-a", "j-closed-b", "j-closed-c"])


def test_agent_spend_and_decided_approvals(scratch):
    con = db(scratch)
    for d, sub in ((399, "keep"), (401, "gone")):
        con.execute("INSERT INTO agent_spend(day,kind,subject) VALUES(?,?,?)", (day(d), "k", sub))
    for rid, dec, d in (("keep-dec", "allow", 179), ("gone-dec", "allow", 181), ("undecided-old", None, 400)):
        con.execute("INSERT INTO approvals(request_id,task_id,tool,requested_at,decision,decided_at) VALUES(?,?,?,?,?,?)",
                    (rid, "t", "x", iso(d), dec, iso(d) if dec else None))
    run(["prune_agent_spend", "prune_approvals"])
    assert _ids(con, "SELECT subject FROM agent_spend") == ["keep"]
    assert _ids(con, "SELECT request_id FROM approvals") == ["keep-dec", "undecided-old"]


def test_finished_image_jobs_go_after_90_days_and_open_ones_stay(scratch):
    t = scratch.thread(None, "pack_otter", "pics")
    mids = scratch.message_ids(t)
    con = db(scratch)
    for i, (state, d) in enumerate((("done", 89), ("done", 91), ("failed", 91), ("running", 200), ("queued", 200))):
        con.execute("INSERT INTO messages(id,thread_id,role,content,created_at) VALUES(?,?,?,?,?)",
                    (f"m{i}", t, "assistant", "x", iso(d)))
        con.execute("INSERT INTO image_jobs(id,message_id,thread_id,bot_id,spec,state,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?)", (f"job{i}", f"m{i}", t, "pack_otter", "{}", state, iso(d), iso(d)))
    assert mids
    assert run(["prune_image_jobs"])["prune_image_jobs"]["rows"] == 2
    assert _ids(con, "SELECT id FROM image_jobs") == ["job0", "job3", "job4"]


def test_sessions_and_devices(scratch):
    ada = scratch.admin()
    con = db(scratch)
    con.execute("INSERT INTO devices(id,created,last_seen) VALUES('dev-live',?,?)", (iso(300), iso(300)))
    con.execute("INSERT INTO devices(id,created,last_seen) VALUES('dev-old',?,?)", (iso(300), iso(300)))
    con.execute("INSERT INTO devices(id,created,last_seen) VALUES('dev-new',?,?)", (iso(300), iso(2)))

    def sess(h, device, last_seen_days, expires_days=None, persistent=0):
        con.execute("INSERT INTO sessions(token_hash,profile_id,device_id,created,last_seen,expires,persistent) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (h, ada.id, device, iso(400), iso(last_seen_days),
                     iso(expires_days) if expires_days is not None else None, persistent))
    sess("expired", None, 1, expires_days=2)                  # expiry passed
    sess("valid", "dev-live", 1, expires_days=-5)             # expires in the future (iso(-5) = +5 days)
    sess("idle", None, 40)                                    # idle past 35 days
    sess("remembered", None, 20, persistent=1)                # a remembered device inside its window
    sess("long-gone", None, 40, persistent=1)
    run(["prune_sessions"])
    assert _ids(con, "SELECT token_hash FROM sessions") == ["remembered", "valid"]
    assert _ids(con, "SELECT id FROM devices") == ["dev-live", "dev-new"]       # dev-live still has a session


def test_a_remember_window_longer_than_35_days_is_respected(scratch):
    ada = scratch.admin()
    con = db(scratch)
    set_setting(**{"people.remember_device_days": 90})
    con.execute("INSERT INTO sessions(token_hash,profile_id,created,last_seen,persistent) VALUES('r',?,?,?,1)",
                (ada.id, iso(100), iso(60)))
    run(["prune_sessions"])
    assert _ids(con, "SELECT token_hash FROM sessions") == ["r"]


# --------------------------------------------------------------------------- #
# Bounded batches
# --------------------------------------------------------------------------- #


def test_no_delete_removes_more_than_a_batch(scratch):
    con = db(scratch)
    con.executemany("INSERT INTO activity(ts,category,kind,subject) VALUES(?,?,?,?)",
                    [(iso(200), "activity", "old", "")] * 2500)

    class Spy:
        def __init__(self, inner):
            self.inner, self.counts = inner, []

        def execute(self, sql, params=()):
            cur = self.inner.execute(sql, params)
            if sql.lstrip().upper().startswith("DELETE"):
                self.counts.append(cur.rowcount)
            return cur

        def __getattr__(self, name):
            return getattr(self.inner, name)

    spy = Spy(db(scratch))
    ctx = maintenance.Ctx(con=spy, data=scratch.root, now=NOW, batch=1000, sleep=0)
    assert maintenance.step_prune_activity(ctx)[0] == 2500
    assert max(spy.counts) <= 1000 and len([c for c in spy.counts if c]) == 3


def test_a_concurrent_writer_is_never_stalled_for_long(scratch):
    con = db(scratch)
    con.execute("BEGIN")
    con.executemany("INSERT INTO activity(ts,category,kind,subject) VALUES(?,?,?,?)",
                    [(iso(200), "activity", "old", "")] * 60000)
    con.execute("COMMIT")
    stalls: list[float] = []
    stop = threading.Event()

    def writer():
        w = db(scratch)
        while not stop.is_set():
            t0 = time.perf_counter()
            w.execute("INSERT INTO usage_daily(subject,day,kind,amount) VALUES('w',?,?,1) "
                      "ON CONFLICT DO UPDATE SET amount = amount + 1", (day(0), "probe"))
            stalls.append(time.perf_counter() - t0)
            time.sleep(0.002)
        w.close()

    th = threading.Thread(target=writer)
    th.start()
    try:
        res = maintenance.run_nightly(now=NOW, steps=["prune_activity"], batch=2000, sleep=0.01)
    finally:
        stop.set()
        th.join()
    assert res["steps"][0]["rows"] == 60000 and len(stalls) > 5
    assert max(stalls) < 0.25, f"a writer waited {max(stalls):.3f}s"


# --------------------------------------------------------------------------- #
# Archive chats older than N months
# --------------------------------------------------------------------------- #


def _fill_threads(scratch):
    ada = scratch.admin()
    bram = scratch.profile("Bram", Role.USER)
    cora = scratch.profile("Cora", Role.USER)
    con = db(scratch)
    ensure_archive_column(con)
    ids = {
        "old": scratch.thread(bram.id, "pack_otter", "old", messages=[("user", "MARK-old needle")]),
        "edge_in": scratch.thread(bram.id, "pack_otter", "just inside"),
        "edge_out": scratch.thread(bram.id, "pack_otter", "just outside"),
        "pinned": scratch.thread(bram.id, "pack_otter", "pinned"),
        "busy": scratch.thread(bram.id, "pack_otter", "busy"),
        "read": scratch.thread(bram.id, "pack_otter", "read recently"),
        "other": scratch.thread(cora.id, "pack_otter", "someone else, no setting"),
        "admin_null": scratch.thread(None, "pack_otter", "unowned"),
    }
    for key, days in (("old", 400), ("edge_in", 364), ("edge_out", 367), ("pinned", 400), ("busy", 400),
                      ("read", 400), ("other", 400), ("admin_null", 400)):
        set_thread_time(con, ids[key], days)
        set_message_time(con, ids[key], days)
    con.execute("UPDATE threads SET is_pinned = 1 WHERE id = ?", (ids["pinned"],))
    con.execute("UPDATE threads SET last_read_at = ? WHERE id = ?", (iso(3), ids["read"]))
    mid = scratch.message_ids(ids["busy"])[0]
    con.execute("INSERT INTO image_jobs(id,message_id,thread_id,bot_id,spec,state,created_at,updated_at) "
                "VALUES('jb',?,?,?,?,?,?,?)", (mid, ids["busy"], "pack_otter", "{}", "running", iso(1), iso(1)))
    return ada, bram, cora, con, ids


def test_archive_is_off_until_a_profile_sets_it(scratch):
    _ada, _bram, _cora, con, ids = _fill_threads(scratch)
    assert run(["archive_chats"])["archive_chats"]["rows"] == 0
    assert con.execute("SELECT COUNT(*) FROM threads WHERE is_archived=1").fetchone()[0] == 0


def test_archive_after_n_archives_exactly_the_aged_threads_and_deletes_nothing(scratch):
    ada, bram, cora, con, ids = _fill_threads(scratch)
    con.execute("UPDATE profiles SET archive_after_months = 12 WHERE id = ?", (bram.id,))
    before = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    out = run(["archive_chats"])
    archived = _ids(con, "SELECT id FROM threads WHERE is_archived = 1")
    assert archived == sorted([ids["old"], ids["edge_out"]])
    assert out["archive_chats"]["rows"] == 2
    assert con.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == before            # never deletes
    assert run(["archive_chats"])["archive_chats"]["rows"] == 0                             # idempotent
    # still found by search and openable by id, and the user can un-archive it
    assert con.execute("SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH 'needle'").fetchone()[0] == 1
    assert scratch.run(lambda d: d.get_thread(ids["old"])) is not None
    # Move back exactly as the route does (PATCH archived:false -> Database.set_archived),
    # without opening the chat: tonight's run must not archive it again.
    scratch.run(lambda d: d.set_archived(ids["old"], False))
    run(["archive_chats"])
    assert ids["old"] not in _ids(con, "SELECT id FROM threads WHERE is_archived = 1")      # stays un-archived


def test_the_unowned_threads_follow_the_protected_admin(scratch):
    ada, _bram, _cora, con, ids = _fill_threads(scratch)
    con.execute("UPDATE profiles SET archive_after_months = 6 WHERE id = ?", (ada.id,))
    run(["archive_chats"])
    assert _ids(con, "SELECT id FROM threads WHERE is_archived = 1") == [ids["admin_null"]]


def test_the_dry_run_counts_without_changing(scratch):
    ada, bram, _cora, con, ids = _fill_threads(scratch)
    con.execute("UPDATE profiles SET archive_after_months = 12 WHERE id = ?", (bram.id,))
    out = run(["archive_chats"], dry_run=True)
    assert out["archive_chats"]["rows"] == 2
    assert con.execute("SELECT COUNT(*) FROM threads WHERE is_archived = 1").fetchone()[0] == 0
    assert maintenance.archive_count(bram.id, 12, NOW) == 2


# --------------------------------------------------------------------------- #
# Orphan sweep
# --------------------------------------------------------------------------- #


def _orphan_world(scratch):
    scratch.admin()
    root = scratch.root
    con = db(scratch)
    t = scratch.thread(None, "pack_otter", "pics", messages=[("assistant", "see ![x](/media/shown.png)")])
    # indexed by the first step, like the real nightly order
    put(root / "media" / "shown.png")
    put(root / "media" / "unsent.png")
    put(root / "media" / "fresh-unsent.png")
    put(root / "files" / "100-a.txt", b"a")
    put(root / "files" / "200-stray.txt", b"b")
    for n in ("shown.png", "unsent.png"):
        os_age(root / "media" / n, 30)
    os_age(root / "files" / "100-a.txt", 30)
    os_age(root / "files" / "200-stray.txt", 30)
    con.execute("INSERT INTO files(id,name,stored_name,size,created_at) VALUES('f1','a.txt','100-a.txt',1,?)", (iso(30),))
    con.execute("INSERT INTO files(id,name,stored_name,size,created_at) VALUES('f2','gone.txt','300-missing.txt',1,?)", (iso(30),))
    return con, t


def os_age(path, days):
    import os
    ts = epoch(days)
    os.utime(path, (ts, ts))


def test_orphan_sweep_honours_the_grace_period_and_the_trash(scratch):
    con, _t = _orphan_world(scratch)
    out = run(["sync_media_index", "orphan_sweep"])
    assert out["orphan_sweep"]["rows"] == 2                       # unsent.png and 200-stray.txt only
    root = scratch.root
    trash = root / "trash" / f"orphans-{NOW.strftime('%Y%m%d')}"
    assert (trash / "media" / "unsent.png").exists() and (trash / "files" / "200-stray.txt").exists()
    assert (root / "media" / "shown.png").exists()                 # a message shows it
    assert (root / "media" / "fresh-unsent.png").exists()          # inside the 7-day grace
    assert (root / "files" / "100-a.txt").exists()                 # has a files row
    # the row whose blob is missing is reported, never deleted
    assert con.execute("SELECT COUNT(*) FROM files WHERE id='f2'").fetchone()[0] == 1
    assert run(["orphan_sweep"])["orphan_sweep"]["rows"] == 0      # idempotent


def test_orphan_sweep_keeps_a_nested_media_file_a_message_shows(scratch):
    """/media/<dir>/<file> is keyed by its path under the store ("feed/pic.png"), not its
    basename: a picture a message shows is never swept because it sits in a sub-folder."""
    scratch.admin()
    root = scratch.root
    scratch.thread(None, "pack_otter", "nested", messages=[("assistant", "look ![](/media/feed/pic.png)")])
    os_age(put(root / "media" / "feed" / "pic.png"), 30)
    os_age(put(root / "media" / "feed" / "lost.png"), 30)
    out = run(["sync_media_index", "orphan_sweep"])
    assert (root / "media" / "feed" / "pic.png").exists()
    assert out["orphan_sweep"]["rows"] == 1 and not (root / "media" / "feed" / "lost.png").exists()


def test_orphan_sweep_reports_files_rows_without_a_blob(scratch):
    _orphan_world(scratch)
    res = maintenance.run_nightly(now=NOW, steps=["orphan_sweep"], dry_run=True, sleep=0)
    assert "1 file record(s) have no blob" in res["steps"][0]["note"]


def test_orphans_are_purged_from_the_trash_after_14_days_not_before(scratch):
    _orphan_world(scratch)
    run(["sync_media_index", "orphan_sweep"])
    trash = scratch.root / "trash" / f"orphans-{NOW.strftime('%Y%m%d')}"
    from datetime import timedelta
    maintenance.run_nightly(now=NOW + timedelta(days=13), steps=["trash_purge"], sleep=0)
    assert trash.exists()
    maintenance.run_nightly(now=NOW + timedelta(days=14), steps=["trash_purge"], sleep=0)
    assert not trash.exists()


def test_a_sweep_that_would_move_most_of_a_store_refuses(scratch):
    scratch.admin()
    for i in range(260):
        os_age(put(scratch.root / "media" / f"m{i}.png"), 30)
    res = maintenance.run_nightly(now=NOW, steps=["orphan_sweep"], sleep=0)
    step = res["steps"][0]
    assert step["rows"] == 0 and "refused" in step["note"]
    assert len(list((scratch.root / "media").iterdir())) == 260


def test_unreferenced_avatars_of_deleted_bots_are_swept_but_used_ones_stay(scratch):
    scratch.admin()
    put(scratch.root / "avatars" / "pack_otter.png")
    put(scratch.root / "avatars" / "pack_otter-face.png")
    put(scratch.root / "avatars" / "deleted-bot.png")
    put(scratch.root / "avatars" / "deleted-bot-face.png")
    from app import config
    bots = config.load_bots()
    for b in bots:
        if b.id == "pack_otter":
            from dataclasses import replace
            config.upsert_bot(replace(b, avatar="pack_otter.png"))
    for f in (scratch.root / "avatars").iterdir():
        os_age(f, 30)
    run(["orphan_sweep"])
    left = sorted(p.name for p in (scratch.root / "avatars").iterdir())
    assert left == ["pack_otter-face.png", "pack_otter.png"]


# --------------------------------------------------------------------------- #
# SQLite housekeeping
# --------------------------------------------------------------------------- #


def test_vacuum_and_checkpoint_shrink_the_files_and_run_last(scratch):
    scratch.admin()
    con = db(scratch)
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.execute("PRAGMA auto_vacuum = 2")
    con.execute("VACUUM")
    assert con.execute("PRAGMA auto_vacuum").fetchone()[0] == 2
    t = scratch.thread(None, "pack_otter", "bulk")
    con.execute("BEGIN")
    con.executemany("INSERT INTO messages(id,thread_id,role,content,created_at) VALUES(?,?,?,?,?)",
                    [(f"b{i}", t, "user", "lorem ipsum " * 40, iso(1)) for i in range(4000)])
    con.execute("COMMIT")
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    big = (scratch.root / "chats.db").stat().st_size
    con.execute("DELETE FROM messages WHERE id LIKE 'b%'")
    res = maintenance.run_nightly(now=NOW, steps=["fts_maintenance", "sqlite_optimize", "incremental_vacuum",
                                                  "wal_checkpoint"], sleep=0)
    order = [r["step"] for r in res["steps"]]
    assert order == ["fts_maintenance", "sqlite_optimize", "incremental_vacuum", "wal_checkpoint"]
    assert all(r["error"] is None for r in res["steps"]), res
    assert (scratch.root / "chats.db").stat().st_size < big / 2
    wal = scratch.root / "chats.db-wal"
    assert not wal.exists() or wal.stat().st_size == 0


def test_the_order_has_the_checkpoint_after_every_other_change(scratch):
    names = [n for n, _ in maintenance.STEPS]
    assert names.index("wal_checkpoint") > max(names.index(n) for n in names if n not in
                                                ("wal_checkpoint", "storage_scan"))
    assert names[-1] == "storage_scan"


def test_a_vacuum_on_a_non_incremental_database_says_so_without_failing(scratch):
    scratch.admin()
    con = db(scratch)
    con.execute("PRAGMA auto_vacuum = 0")
    con.execute("VACUUM")
    out = run(["incremental_vacuum"])
    assert out["incremental_vacuum"]["error"] is None and "not incremental" in out["incremental_vacuum"]["note"]


def test_fts_merge_keeps_the_index_searchable(scratch):
    scratch.admin()
    scratch.thread(None, "pack_otter", "x", messages=[("user", "zebra crossing sentinel")])
    run(["fts_maintenance"])
    con = db(scratch)
    assert con.execute("SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH 'sentinel'").fetchone()[0] == 1


# --------------------------------------------------------------------------- #
# Recording
# --------------------------------------------------------------------------- #


def test_every_step_writes_its_own_row_and_a_failing_step_does_not_stop_the_next(scratch, monkeypatch):
    scratch.admin()

    def boom(ctx):
        raise RuntimeError("planted failure")

    monkeypatch.setattr(maintenance, "STEPS", tuple(
        (n, boom if n == "prune_usage_daily" else f) for n, f in maintenance.STEPS))
    res = maintenance.run_nightly(now=NOW, steps=["prune_activity", "prune_usage_daily", "prune_agent_spend"], sleep=0)
    by = {r["step"]: r for r in res["steps"]}
    assert by["prune_usage_daily"]["error"].startswith("RuntimeError: planted failure")
    assert by["prune_activity"]["error"] is None and by["prune_agent_spend"]["error"] is None
    assert res["ok"] is False
    con = db(scratch)
    rows = {r[0]: r for r in con.execute("SELECT step, finished, rows, bytes, ms, error FROM maintenance_runs")}
    assert set(rows) == {"prune_activity", "prune_usage_daily", "prune_agent_spend", "run"}
    assert rows["prune_usage_daily"][5] and rows["prune_activity"][5] is None and rows["prune_activity"][1]
    assert "1 step(s) failed" in rows["run"][5]
    last = maintenance.last_run()
    assert last["ok"] is False and [s["step"] for s in last["steps"]] == [
        "prune_activity", "prune_usage_daily", "prune_agent_spend"]


def test_a_dry_run_records_and_changes_nothing(scratch):
    scratch.admin()
    con = db(scratch)
    con.execute("INSERT INTO activity(ts,category,kind,subject) VALUES(?,?,?,?)", (iso(200), "activity", "old", ""))
    res = maintenance.run_nightly(now=NOW, dry_run=True, sleep=0)
    assert res["dry_run"] and res["ok"]
    assert {r["step"]: r for r in res["steps"]}["prune_activity"]["rows"] == 1
    assert con.execute("SELECT COUNT(*) FROM activity").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM maintenance_runs").fetchone()[0] == 0


def test_a_second_run_while_one_is_running_is_refused(scratch):
    scratch.admin()
    assert maintenance._RUN_LOCK.acquire(blocking=False)
    try:
        with pytest.raises(maintenance.MaintenanceBusy):
            maintenance.run_nightly(now=NOW, steps=["prune_activity"])
    finally:
        maintenance._RUN_LOCK.release()


def test_the_full_run_completes_on_a_fresh_install_without_errors(scratch):
    scratch.admin()
    res = maintenance.run_nightly(now=NOW, sleep=0)
    assert [r["step"] for r in res["steps"]] == list(maintenance.STEP_NAMES)
    assert res["ok"], [r for r in res["steps"] if r["error"]]


def test_unknown_step_names_are_refused(scratch):
    with pytest.raises(ValueError):
        maintenance.run_nightly(now=NOW, steps=["delete_everything"])


def test_a_corrupt_setting_does_not_stop_maintenance(scratch, monkeypatch):
    scratch.admin()
    from app import settings_store
    monkeypatch.setattr(settings_store, "get", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("store down")))
    assert maintenance.cfg("system.retention.activity_days") == 90
