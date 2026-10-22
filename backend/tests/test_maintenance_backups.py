"""Snapshot tiers (keep 8 recent / 7 daily / 4 weekly, never below 4) and the rest of the disk rules."""
from __future__ import annotations

import os
from datetime import datetime, timedelta

import pytest
from maint_helpers import NOW, db, put, run
from profiles_fixtures import scratch

from app import backups, config, maintenance

SQLITE = b"SQLite format 3\x00" + b"\x00" * 64


END = datetime(2026, 11, 15, 18, 0, 0)  # noqa: DTZ001  (the rotation stamps local time)


def _names(days: int, per_day: int = 4, end: datetime = END) -> list[str]:
    """6-hourly snapshot names for ``days`` days ending at ``end``."""
    out = []
    t = end
    for _ in range(days * per_day):
        out.append(f"chats-{t.strftime('%Y%m%d-%H%M%S')}.db")
        t -= timedelta(hours=24 // per_day)
    return out


def test_the_plan_keeps_8_recent_7_daily_and_4_weekly():
    names = _names(60)
    plan = backups.plan_snapshot_tiers(names)
    assert len(plan["recent"]) == 8 and plan["recent"] == sorted(names, reverse=True)[:8]
    assert len(plan["daily"]) == 7 and len(plan["weekly"]) == 4
    days = {n[6:14] for n in plan["daily"]}
    assert len(days) == 7                                       # one per day
    assert all(n[15:21] == "180000" for n in plan["daily"])      # the LAST snapshot of each day
    oldest_daily_day = min(days)
    assert all(n[6:14] < oldest_daily_day for n in plan["weekly"])   # weeklies reach past the dailies
    weeks = {datetime.strptime(n[6:14], "%Y%m%d").isocalendar()[:2] for n in plan["weekly"]}  # noqa: DTZ007
    assert len(weeks) == 4
    assert set(plan["keep"]) == set(plan["recent"]) | set(plan["daily"]) | set(plan["weekly"])
    assert len(plan["keep"]) <= 8 + 7 + 4 and set(plan["drop"]) == set(names) - set(plan["keep"])


def test_never_fewer_than_four_in_total():
    names = _names(1)                                           # 4 snapshots
    plan = backups.plan_snapshot_tiers(names, keep_recent=1, keep_daily=0, keep_weekly=0)
    assert len(plan["keep"]) == 4 and plan["drop"] == []
    plan = backups.plan_snapshot_tiers(_names(3), keep_recent=1, keep_daily=0, keep_weekly=0)
    assert len(plan["keep"]) == 4 and len(plan["drop"]) == 8


def test_other_file_names_are_ignored():
    plan = backups.plan_snapshot_tiers(["chats-before-x-20261101-000000.db", "notes.txt", *_names(2)])
    assert "notes.txt" not in plan["keep"] + plan["drop"]
    assert not any("before" in n for n in plan["keep"] + plan["drop"])


@pytest.fixture
def snaps(scratch):
    d = config.BACKUP_DIR
    d.mkdir(parents=True, exist_ok=True)
    names = _names(40)
    for n in names:
        (d / n).write_bytes(SQLITE + n.encode())
    return d, names


def test_pruning_links_the_keepers_and_frees_the_rest(snaps):
    d, names = snaps
    plan = backups.plan_snapshot_tiers(names)
    out = backups.prune_snapshot_tiers()
    assert out["kept"] == len(plan["keep"]) and out["removed"] == len(plan["drop"])
    root = sorted(p.name for p in d.glob("chats-*.db"))
    assert root == sorted(plan["recent"])
    assert sorted(p.name for p in (d / "tiers" / "daily").iterdir()) == sorted(set(plan["daily"]) - set(plan["recent"]))
    assert sorted(p.name for p in (d / "tiers" / "weekly").iterdir()) == sorted(plan["weekly"])
    # the kept snapshots are the real bytes, and a daily keeper is not a second copy on disk
    some = sorted(set(plan["daily"]) - set(plan["recent"]))[0]
    assert (d / "tiers" / "daily" / some).read_bytes().endswith(some.encode())


def test_the_main_rotation_cannot_take_a_tiered_keeper_away(snaps):
    """main._prune_backups keeps the newest N files in the root: the tier links are what survive it."""
    d, names = snaps
    backups.prune_snapshot_tiers()
    keeper = sorted(p.name for p in (d / "tiers" / "daily").iterdir())[0]
    # the rotation unlinks root files; a keeper's root copy is already gone and its tier copy untouched
    for p in d.glob("chats-*.db"):
        p.unlink()
    assert (d / "tiers" / "daily" / keeper).is_file()


def test_pruning_is_idempotent_and_a_dry_run_changes_nothing(snaps):
    d, names = snaps
    before = sorted(p.name for p in d.glob("chats-*.db"))
    dry = backups.prune_snapshot_tiers(dry_run=True)
    assert dry["removed"] > 0 and sorted(p.name for p in d.glob("chats-*.db")) == before
    assert not (d / "tiers").exists()
    first = backups.prune_snapshot_tiers()
    second = backups.prune_snapshot_tiers()
    assert first["removed"] > 0 and second["removed"] == 0


def test_tiered_keepers_are_listed_and_restorable(snaps):
    d, names = snaps
    backups.prune_snapshot_tiers()
    rows = {r["name"]: r for r in backups.list_backups() if r["kind"] == "snapshot"}
    tiers = {r["tier"] for r in rows.values()}
    assert tiers == {"recent", "daily", "weekly"}
    weekly = next(n for n, r in rows.items() if r["tier"] == "weekly")
    kind, path = backups.resolve_id(f"snapshot:{weekly}")
    assert kind == "snapshot" and path.parent.name == "weekly"
    with pytest.raises(backups.BackupError):
        backups.resolve_id("snapshot:chats-19990101-000000.db")


def test_an_empty_or_broken_snapshot_is_never_a_keeper(snaps):
    d, names = snaps
    newest = max(names)
    (d / newest).write_bytes(b"")                                # a failed VACUUM INTO leaves this
    out = backups.prune_snapshot_tiers()
    assert newest not in out["plan"]["keep"]


def test_only_the_rotations_own_files_are_touched(snaps):
    d, names = snaps
    hand = d / "chats-before-upgrade-20261101-000000.db"
    hand.write_bytes(SQLITE)
    stray = d / "notes.txt"
    stray.write_text("x")
    backups.prune_snapshot_tiers()
    assert hand.exists() and stray.exists()


def test_premigration_copies_keep_the_newest_three(scratch):
    d = config.BACKUP_DIR
    for i in range(6):
        p = d / f"4.0.0-2026110{i + 1}-000000"
        p.mkdir(parents=True)
        (p / "chats.db").write_bytes(b"x" * 10)
        os.utime(p, (1_700_000_000 + i * 100, 1_700_000_000 + i * 100))
    out = backups.prune_premigration(3)
    assert out["removed"] == 3 and out["bytes"] == 30
    assert sorted(p.name for p in d.iterdir()) == [f"4.0.0-2026110{i}-000000" for i in (4, 5, 6)]


def test_the_step_reads_its_keep_counts_from_the_settings(snaps):
    d, names = snaps
    from maint_helpers import set_setting
    set_setting(**{"system.backups.keep_recent": 4, "system.backups.keep_daily": 2, "system.backups.keep_weekly": 1})
    run(["backup_tiers"])
    assert len(list(d.glob("chats-*.db"))) == 4
    assert len(list((d / "tiers" / "daily").iterdir())) <= 2 and len(list((d / "tiers" / "weekly").iterdir())) == 1


# --------------------------------------------------------------------------- #
# Skip rules
# --------------------------------------------------------------------------- #


def test_a_backup_being_written_blocks_a_run(scratch, tmp_path, monkeypatch):
    monkeypatch.setenv("DISPATCH_MAINTENANCE_MARKER", str(tmp_path / "none"))
    assert maintenance.blocked_reason() is None
    arch = config.BACKUP_DIR / "archives"
    part = put(arch / "dispatch-20261101-000000.dpbk.part")
    assert maintenance.blocked_reason() == "backup_running"
    with pytest.raises(maintenance.MaintenanceBlocked):
        maintenance.run_nightly(now=NOW, steps=["prune_activity"], respect_blockers=True)
    part.unlink()
    assert maintenance.blocked_reason() is None
    # a snapshot file that was just touched and is not yet a database
    snap = put(config.BACKUP_DIR / "chats-20261101-000000.db", b"")
    assert maintenance.blocked_reason() == "backup_running"
    snap.write_bytes(SQLITE)
    assert maintenance.blocked_reason() is None


def test_the_operator_marker_blocks_a_run_and_stops_one_midway(scratch, tmp_path, monkeypatch):
    scratch.admin()
    maintenance.connect().close()                                # creates maintenance_runs
    marker = tmp_path / "maintenance-marker"
    monkeypatch.setenv("DISPATCH_MAINTENANCE_MARKER", str(marker))
    marker.write_text("pid=1")
    assert maintenance.blocked_reason() == "maintenance_marker"
    with pytest.raises(maintenance.MaintenanceBlocked):
        maintenance.run_nightly(now=NOW, respect_blockers=True)
    assert db(scratch).execute("SELECT COUNT(*) FROM maintenance_runs").fetchone()[0] == 0
    # it appears while a run is under way: the run stops before the next step and says so
    marker.unlink()
    calls = []

    def fake(ctx):
        calls.append(1)
        marker.write_text("pid=1")
        return 0, 0

    monkeypatch.setattr(maintenance, "STEPS", (("prune_activity", fake), ("prune_usage_daily", fake)))
    monkeypatch.setattr(maintenance, "STEP_NAMES", ("prune_activity", "prune_usage_daily"))
    res = maintenance.run_nightly(now=NOW, respect_blockers=True, sleep=0)
    assert calls == [1] and res["interrupted"] == "maintenance_marker" and res["ok"] is False
    run_row = db(scratch).execute("SELECT error FROM maintenance_runs WHERE step='run'").fetchone()
    assert "maintenance_marker" in run_row[0]


def test_the_storage_numbers_match_du(scratch):
    import subprocess
    scratch.admin()
    for i in range(30):
        put(scratch.root / "media" / f"m{i}.png", b"x" * (600_000 + i * 37))
    put(scratch.root / "reactions" / "spent" / "joy" / "a.png", b"y" * 1_234_567)
    put(scratch.root / "files" / "f.bin", b"z" * 777_777)
    for n in ("a.bak-1", "chats.db.bak-old"):
        put(scratch.root / n, b"b" * 1000)
    put(scratch.root / "backups" / "chats-20261101-000000.db", SQLITE + b"s" * 50000)
    area = maintenance.scan_storage(scratch.root)
    total = sum(a["bytes"] for a in area.values())
    du = int(subprocess.run(["du", "-sb", str(scratch.root)], capture_output=True, text=True).stdout.split()[0])
    assert abs(total - du) / du < 0.02, (total, du)
    assert area["files"]["bytes"] == 777_777 and area["reactions_spent"]["bytes"] == 1_234_567
    assert area["stray_bak"]["files"] == 2 and area["backups_6h"]["bytes"] == len(SQLITE) + 50000
    assert area["media"]["files"] == 30
    report = maintenance.storage_report()
    assert {s["name"] for s in report["stray"]} == {"a.bak-1", "chats.db.bak-old"}
    assert report["areas"]["files"]["bytes"] == 777_777 and report["free_disk_bytes"] > 0
    assert report["total_bytes"] == total and report["measured_at"]


def test_a_hard_linked_keeper_is_counted_once(snaps):
    d, names = snaps
    backups.prune_snapshot_tiers()
    area = maintenance.scan_storage(d.parent)
    kept = backups.plan_snapshot_tiers(names)["keep"]
    each = len(SQLITE) + len(names[0])
    total = area["backups_6h"]["bytes"] + area["backups_daily"]["bytes"] + area["backups_weekly"]["bytes"]
    assert total == len(kept) * each
