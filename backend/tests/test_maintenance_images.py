"""Generated-image retention ("Keep images N months (default 1 yr)"), applying to ALL
generated images, with a shipped default of forever (BUILD-PLAN-v2 1.13.6).

One fixture install holds a picture of every kind the rule covers, plus every picture the rule must
never touch. The assertions are the category matrix, the in-use set, the age basis, preview == job,
the trash (restore and the 14-day purge) and the "image expired" bookkeeping.
"""
from __future__ import annotations

import json
from datetime import timedelta

from maint_helpers import NOW, age, db, epoch, iso, put, run, set_message_time, set_setting
from profiles_fixtures import scratch

from app import maintenance
from app import media_inventory as mi

OLD = 400          # days; the 12-month cutoff is ~365 days back


def _job(con, scratch, thread, msg_id, jid, state, media_url, days, spec="{}"):
    con.execute("INSERT INTO image_jobs(id,message_id,thread_id,bot_id,spec,state,media_url,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)", (jid, msg_id, thread, "pack_otter", spec, state, media_url, iso(days), iso(days)))


class W:
    """The fixture install."""

    def __init__(self, scratch):
        self.s = scratch
        self.root = scratch.root
        scratch.admin()
        self.bram = scratch.profile("Bram")
        self.con = db(scratch)
        r = self.root
        # reaction pictures that were fired
        self.spent = {k: age(put(r / "reactions" / "spent" / "joy" / f"{k}.png"), d)
                      for k, d in (("aged", OLD), ("recent", 10), ("traced", OLD), ("oldtrace", OLD))}
        self.shelf = age(put(r / "reactions" / "moods-pack_otter" / "joy" / "shelf.png"), OLD)
        # chat [[pic:]] output (image jobs) and Image Studio results, all in media/
        self.media = {k: age(put(r / "media" / f"{k}.png"), OLD)
                      for k in ("chatpic-old", "chatpic-shown", "studio-old", "studio-shown", "upload", "refpic")}
        # avatars
        self.pool_spent = age(put(r / "avatar-pool" / "pack_otter" / "spent" / "old-face.png"), OLD)
        self.pool_ready = age(put(r / "avatar-pool" / "pack_otter" / "ready" / "next-face.png"), OLD)
        self.avatar = age(put(r / "avatars" / "pack_otter.png"), OLD)
        self.snap_used = age(put(r / "avatar-snapshots" / "aaaaaaaaaaaaaaaa.png"), OLD)
        self.snap_unref = age(put(r / "avatar-snapshots" / "bbbbbbbbbbbbbbbb.png"), OLD)

        t_old = scratch.thread(self.bram.id, "pack_otter", "old chat", snapshot="aaaaaaaaaaaaaaaa.png",
                               messages=[("assistant", "![a](/media/chatpic-old.png)"),
                                         ("assistant", "![b](/media/studio-old.png)"),
                                         ("system", "trace", None, {"kind": "reaction", "reaction_id": "pool-joy-oldtrace"})])
        t_new = scratch.thread(self.bram.id, "pack_otter", "live chat",
                               messages=[("assistant", "![c](/media/chatpic-shown.png)"),
                                         ("assistant", "![d](/media/studio-shown.png)"),
                                         ("system", "trace", None, {"kind": "reaction", "reaction_id": "pool-joy-traced"})])
        set_message_time(self.con, t_old, OLD + 5)
        set_message_time(self.con, t_new, 20)
        m_old = scratch.message_ids(t_old)
        m_new = scratch.message_ids(t_new)
        _job(self.con, scratch, t_old, m_old[0], "j-old", "done", "/media/chatpic-old.png", OLD + 5)
        _job(self.con, scratch, t_new, m_new[0], "j-shown", "done", "/media/chatpic-shown.png", OLD + 5)
        _job(self.con, scratch, t_new, m_new[1], "j-run", "running", None, 1,
             spec=json.dumps({"prompt": "again", "reference": "/media/refpic.png"}))
        scratch.ledger([
            {"id": "s1", "url": "/media/studio-old.png", "created_at": iso(OLD + 5), "owner_profile_id": self.bram.id},
            {"id": "s2", "url": "/media/studio-shown.png", "created_at": iso(OLD + 5), "owner_profile_id": self.bram.id},
            {"id": "s3", "url": "/media/refpic.png", "created_at": iso(OLD + 5), "owner_profile_id": self.bram.id},
        ])
        self.expect_expired = {
            "reaction_spent": [self.spent["aged"], self.spent["oldtrace"]],
            "chat_pic": [self.media["chatpic-old"]],
            "imagestudio": [self.media["studio-old"]],
            "avatar_pool_spent": [self.pool_spent],
            "avatar_snapshot": [self.snap_unref],
        }
        self.expect_kept = [self.spent["recent"], self.spent["traced"], self.shelf, self.media["chatpic-shown"],
                            self.media["studio-shown"], self.media["upload"], self.media["refpic"], self.pool_ready,
                            self.avatar, self.snap_used]

    @property
    def all_expired(self):
        return [p for v in self.expect_expired.values() for p in v]

    def index(self):
        run(["sync_media_index"])


def _months(n=12):
    set_setting(images_mode="months", images_months=n)


# --------------------------------------------------------------------------- #


def test_forever_is_the_shipped_default_and_does_nothing(scratch):
    w = W(scratch)
    w.index()
    out = run(["image_retention"])
    assert out["image_retention"]["rows"] == 0 and "forever" in out["image_retention"]["note"]
    assert all(p.exists() for p in w.all_expired + w.expect_kept)
    assert w.con.execute("SELECT COUNT(*) FROM media_expired").fetchone()[0] == 0
    assert maintenance.cfg("system.retention.images.mode") == "forever" and maintenance.cfg("system.retention.images.months") == 12


def test_n_months_expires_exactly_the_aged_assets_of_every_category(scratch):
    w = W(scratch)
    _months()
    w.index()
    out = run(["image_retention"])["image_retention"]
    assert out["error"] is None
    assert out["rows"] == len(w.all_expired)
    for p in w.all_expired:
        assert not p.exists(), p
    for p in w.expect_kept:
        assert p.exists(), f"must never be expired: {p}"
    by_kind = out["images"]["would_expire"]["by_kind"]
    assert {k: v["count"] for k, v in by_kind.items()} == {k: len(v) for k, v in w.expect_expired.items()}


def test_the_in_use_set_is_reported_as_protected(scratch):
    w = W(scratch)
    _months()
    w.index()
    plan = mi.plan_expiry(w.con, w.root, "months", 12, NOW).summary()
    assert plan["protected"] == {
        "referenced_recently": 3,        # a trace, a chat picture and a studio picture a newer message shows
        "active_job": 1,                 # a running job reads refpic.png
        "unfired_reaction_shelf": 1,
        "avatar_pool_ready": 1,
        "current_avatars": 1,
        "avatar_snapshot_in_use": 1,
    }


def test_preview_equals_what_the_job_does(scratch):
    w = W(scratch)
    _months()
    w.index()
    preview = mi.plan_expiry(w.con, w.root, "months", 12, NOW).summary()
    size = sum(p.stat().st_size for p in w.all_expired)
    res = run(["image_retention"])["image_retention"]
    assert preview["would_expire"]["count"] == res["rows"] == len(w.all_expired)
    assert preview["would_expire"]["bytes"] == res["bytes"] == size
    assert preview["would_expire"]["by_kind"] == res["images"]["would_expire"]["by_kind"]
    assert preview["oldest"] and preview["newest"] and preview["cutoff"]


def test_a_dry_run_changes_nothing_and_counts_the_same(scratch):
    w = W(scratch)
    _months()
    w.index()
    res = run(["image_retention"], dry_run=True)["image_retention"]
    assert res["rows"] == len(w.all_expired)
    assert all(p.exists() for p in w.all_expired)
    assert w.con.execute("SELECT COUNT(*) FROM media_expired").fetchone()[0] == 0
    assert not (w.root / "trash").exists()


def test_a_second_run_is_a_no_op(scratch):
    w = W(scratch)
    _months()
    w.index()
    run(["image_retention"])
    again = run(["image_retention"])["image_retention"]
    assert again["rows"] == 0 and again["error"] is None


def test_expired_names_are_recorded_and_files_wait_in_the_dated_trash(scratch):
    w = W(scratch)
    _months()
    w.index()
    run(["image_retention"])
    rows = {r[0]: r[1] for r in w.con.execute("SELECT name, kind FROM media_expired")}
    assert rows["chatpic-old.png"] == "chat_pic" and rows["studio-old.png"] == "imagestudio"
    assert rows["reactions/spent/joy/aged.png"] == "reaction_spent"
    assert rows["avatar-pool/pack_otter/spent/old-face.png"] == "avatar_pool_spent"
    assert rows["avatar-snapshots/bbbbbbbbbbbbbbbb.png"] == "avatar_snapshot"
    trash = w.root / "trash" / f"images-{NOW.strftime('%Y%m%d')}"
    assert (trash / "media" / "chatpic-old.png").exists()
    assert (trash / "reactions" / "spent" / "joy" / "aged.png").exists()


def test_trash_restores_and_purges_after_the_hold(scratch):
    w = W(scratch)
    _months()
    w.index()
    run(["image_retention"])
    got = mi.restore_images(w.con, w.root, names=["chatpic-old.png"])
    assert got["restored"] == 1 and w.media["chatpic-old"].exists()
    assert w.con.execute("SELECT COUNT(*) FROM media_expired WHERE name='chatpic-old.png'").fetchone()[0] == 0
    assert not w.spent["aged"].exists()                                  # only what was asked for
    full = mi.restore_images(w.con, w.root)
    assert full["restored"] == len(w.all_expired) - 1
    assert all(p.exists() for p in w.all_expired)
    assert w.con.execute("SELECT COUNT(*) FROM media_expired").fetchone()[0] == 0
    # expire again, then let the hold lapse: day 13 keeps the trash, day 14 deletes it for good
    run(["image_retention"])
    trash = w.root / "trash" / f"images-{NOW.strftime('%Y%m%d')}"
    maintenance.run_nightly(now=NOW + timedelta(days=13), steps=["trash_purge"], sleep=0)
    assert trash.exists()
    res = maintenance.run_nightly(now=NOW + timedelta(days=14), steps=["trash_purge"], sleep=0)
    assert not trash.exists() and res["steps"][0]["rows"] == len(w.all_expired)
    assert mi.restore_images(w.con, w.root)["restored"] == 0


def test_the_hold_is_the_setting(scratch):
    w = W(scratch)
    _months()
    set_setting(images_trash_days=30)
    w.index()
    run(["image_retention"])
    trash = w.root / "trash" / f"images-{NOW.strftime('%Y%m%d')}"
    maintenance.run_nightly(now=NOW + timedelta(days=20), steps=["trash_purge"], sleep=0)
    assert trash.exists()
    maintenance.run_nightly(now=NOW + timedelta(days=30), steps=["trash_purge"], sleep=0)
    assert not trash.exists()


def test_age_basis_is_the_newer_of_the_file_and_its_last_message(scratch):
    w = W(scratch)
    _months()
    # a fresh file that only an ancient message shows is NOT aged; an ancient file shown by a fresh message is not either
    age(w.media["chatpic-old"], 5)
    w.index()
    plan = mi.plan_expiry(w.con, w.root, "months", 12, NOW)
    assert w.media["chatpic-old"] not in [a.path for a in plan.candidates]
    assert w.media["chatpic-shown"] not in [a.path for a in plan.candidates]
    assert w.spent["oldtrace"] in [a.path for a in plan.candidates]


def test_a_picture_survives_when_its_job_row_was_pruned_before_the_rule_was_switched_on(scratch):
    """image_jobs rows go after 90 days; the generated-media record is what lets a later
    "N months" still tell a generated picture from an upload."""
    w = W(scratch)
    w.index()
    run(["prune_image_jobs"])
    assert w.con.execute("SELECT COUNT(*) FROM image_jobs WHERE state='done'").fetchone()[0] == 0
    _months()
    run(["image_retention"])
    assert not w.media["chatpic-old"].exists() and w.media["upload"].exists()


def test_uploads_and_agent_attachments_are_never_touched(scratch):
    w = W(scratch)
    _months()
    w.index()
    run(["image_retention"])
    assert w.media["upload"].exists()
    assert "upload.png" not in {r[0] for r in w.con.execute("SELECT name FROM media_expired")}


def test_the_reference_index_follows_a_rewritten_placeholder(scratch):
    """An image job rewrites an OLDER message when the picture arrives; the scan must see it."""
    scratch.admin()
    con = db(scratch)
    t = scratch.thread(None, "pack_otter", "x", messages=[("assistant", "image pending")])
    mid = scratch.message_ids(t)[0]
    for i in range(3):
        scratch.thread(None, "pack_otter", f"later{i}")
    mi.ensure_schema(con)
    mi.scan_refs(con)
    assert con.execute("SELECT COUNT(*) FROM media_refs").fetchone()[0] == 0
    con.execute("UPDATE messages SET content = ? WHERE id = ?", ("[[media:/media/late.png|cap]]", mid))
    _job(con, scratch, t, mid, "jx", "done", "/media/late.png", 0)
    con.execute("UPDATE image_jobs SET updated_at = ? WHERE id='jx'", (iso(0),))
    mi.scan_refs(con, recheck_tail=0)
    assert con.execute("SELECT name FROM media_refs").fetchall() == [("late.png",)]


def test_the_scan_is_incremental_and_survives_a_renumbered_table(scratch):
    scratch.admin()
    con = db(scratch)
    scratch.thread(None, "pack_otter", "a", messages=[("assistant", "/media/one.png")])
    mi.ensure_schema(con)
    mi.scan_refs(con, recheck_tail=0)
    assert mi.state_get(con, "media_refs.rowid") not in (None, "0")
    scratch.thread(None, "pack_otter", "b", messages=[("assistant", "/media/two.png")])
    out = mi.scan_refs(con, recheck_tail=0)
    assert out["scanned"] == 1                                   # only the new message was read
    con.execute("DELETE FROM media_refs")
    mi.state_set(con, "media_refs.rowid", "999999")               # a watermark above the table (VACUUM renumbered)
    mi.scan_refs(con, recheck_tail=0)
    assert {r[0] for r in con.execute("SELECT name FROM media_refs")} == {"one.png", "two.png"}


def test_the_writers_table_names_every_kind_the_rule_covers():
    covered = {w["kind"] for w in mi.WRITERS if w["covered"]}
    assert covered == {"chat_pic", "imagestudio", "reaction_spent", "avatar_pool_spent", "avatar_snapshot"}
    assert all(w.get("why") for w in mi.WRITERS if not w["covered"])


def test_months_ago_clamps_the_day():
    from datetime import UTC, datetime
    assert mi.months_ago(datetime(2026, 3, 31, tzinfo=UTC), 1).day == 28
    assert mi.months_ago(datetime(2026, 1, 15, tzinfo=UTC), 13).strftime("%Y-%m") == "2024-12"
    assert epoch(0) > epoch(1)
