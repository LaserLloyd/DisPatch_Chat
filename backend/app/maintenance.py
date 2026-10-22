"""The daily maintenance task (4.0 scale and retention, BUILD-PLAN-v2 1.13.3).

``main.py`` calls :func:`start` once in the lifespan startup and ``await`` :func:`stop` once at
shutdown (Foundation wired both), so this module never needs a ``main.py`` edit.

**What it is.** An asyncio loop that runs :func:`run_nightly` once a day at ``maintenance.time``
(default 04:30 local; after the 02:00-02:40 backup window and clear of the 04:00-04:45 operator
crons that call DisPatch). It *waits* (and retries every 30 minutes) while
``~/.local/state/openclaw/maintenance`` exists or a backup is being written. ``POST
/api/maintenance/run`` starts the same run on demand (``maintenance_api.py``).

**How it behaves.** One process-wide lock; its own SQLite connection with a busy timeout; every
delete in bounded batches (a short sleep between them) so request handling never waits more than
one batch; every step individually try/except'd, logged and recorded in ``maintenance_runs`` (one
row per step plus a ``run`` summary row). A step that fails never stops the next one. A run that
meets the maintenance marker half way stops before the next step.

**What the defaults do.** They delete no chat and no image: the image rule is *forever* until an
Admin chooses "N months", and archiving old chats is off until a profile asks for it for itself
(reversible, and it never deletes). Everything else is the housekeeping a year of use needs: prune
the log and event tables, sweep unreferenced files, compact the database, thin the snapshots.

Steps, in order (:data:`STEPS`):

    sync_media_index  prune_activity  prune_task_events  prune_job_events  prune_usage_daily
    prune_transcript_seen  prune_sessions  prune_agent_spend  prune_approvals  prune_job_feedback
    archive_chats  image_retention  prune_image_jobs  orphan_sweep  trash_purge  backup_tiers
    fts_maintenance  sqlite_optimize  incremental_vacuum  wal_checkpoint  storage_scan

(``wal_checkpoint`` is the last step that changes anything; ``storage_scan`` only measures.)
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from . import media_inventory as mi

log = logging.getLogger("local-chat.maintenance")

BATCH = 5000                 # rows per delete, then commit
BATCH_SLEEP = 0.05           # seconds between batches
STARTUP_DELAY = 600          # a fresh start is busy; the first look at the schedule waits
RETRY_SECONDS = 30 * 60      # while blocked (marker / backup running)
POLL_SECONDS = 60
CLOSED_JOB_STATES = ("no", "applied", "archived", "duplicate")
FINISHED_TASK_STATES = ("succeeded", "failed", "cancelled")

#: ``maintenance_runs`` (migration 011); created here too, identically, if a build runs without it.
RUNS_DDL = """
CREATE TABLE IF NOT EXISTS maintenance_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started TEXT NOT NULL,
    finished TEXT,
    step TEXT NOT NULL,
    rows INTEGER NOT NULL DEFAULT 0,
    bytes INTEGER NOT NULL DEFAULT 0,
    ms INTEGER NOT NULL DEFAULT 0,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_maintenance_runs_started ON maintenance_runs(started);
CREATE INDEX IF NOT EXISTS idx_maintenance_runs_step ON maintenance_runs(step, started);
CREATE TABLE IF NOT EXISTS storage_stats (
    area TEXT PRIMARY KEY,
    bytes INTEGER NOT NULL DEFAULT 0,
    files INTEGER NOT NULL DEFAULT 0,
    measured_at TEXT NOT NULL
);
"""

_RUN_LOCK = threading.Lock()
_STORAGE_LOCK = threading.Lock()
_task: asyncio.Task | None = None
_state: dict[str, Any] = {"running": False, "since": None, "trigger": None}


class MaintenanceBusy(RuntimeError):
    """Another run holds the process-wide lock."""


class MaintenanceBlocked(RuntimeError):
    """The maintenance marker exists or a backup is being written."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# --------------------------------------------------------------------------- #
# Settings, paths, skip rules
# --------------------------------------------------------------------------- #


def _data_dir() -> Path:
    from . import config
    return Path(config.DATA_DIR)


def db_path() -> Path:
    return _data_dir() / "chats.db"


def marker_path() -> Path:
    env = os.environ.get("DISPATCH_MAINTENANCE_MARKER")
    return Path(env) if env else Path.home() / ".local" / "state" / "openclaw" / "maintenance"


def cfg(key: str) -> Any:
    """The effective value of a retention/maintenance setting (the schema default if the store
    cannot be read - maintenance must never fail on a settings problem)."""
    from . import settings_schema, settings_store
    try:
        return settings_store.get(key)
    except Exception:
        d = settings_schema.get_def(key)
        return d.default if d is not None else None


def blocked_reason() -> str | None:
    """Why a run must wait: the operator maintenance marker or a backup being written."""
    try:
        if marker_path().exists():
            return "maintenance_marker"
    except OSError:
        pass
    from . import backups
    with contextlib.suppress(Exception):
        if backups.backup_in_progress():
            return "backup_running"
    return None


def parse_time(text: Any) -> tuple[int, int]:
    """``HH:MM`` (24 h) as (hour, minute); anything else reads as the 04:30 default."""
    try:
        h, m = str(text).strip().split(":")
        h, m = int(h), int(m)
        if 0 <= h < 24 and 0 <= m < 60:
            return h, m
    except (ValueError, AttributeError):
        pass
    return 4, 30


def next_run_at(now_local: datetime, hhmm: Any) -> datetime:
    h, m = parse_time(hhmm)
    t = now_local.replace(hour=h, minute=m, second=0, microsecond=0)
    return t if t > now_local else t + timedelta(days=1)


def connect() -> sqlite3.Connection:
    """The task's own connection: autocommit (each batch is its own transaction), a busy timeout,
    foreign keys on (the cascades some deletes rely on)."""
    from . import accounts
    with accounts.connect():          # the accounts tables (activity, sessions, profiles ...) exist
        pass
    con = sqlite3.connect(str(db_path()), timeout=10.0, isolation_level=None)
    con.execute("PRAGMA busy_timeout=10000")
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(RUNS_DDL)
    mi.ensure_schema(con)
    return con


# --------------------------------------------------------------------------- #
# Step plumbing
# --------------------------------------------------------------------------- #


@dataclass
class Ctx:
    con: sqlite3.Connection
    data: Path
    now: datetime
    dry: bool = False
    batch: int = BATCH
    sleep: float = BATCH_SLEEP
    note: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def cutoff_iso(self, days: int) -> str:
        return (self.now - timedelta(days=int(days))).isoformat(timespec="seconds")

    def cutoff_day(self, days: int) -> str:
        return (self.now.astimezone() - timedelta(days=int(days))).strftime("%Y-%m-%d")


def _count(ctx: Ctx, table: str, where: str, params: tuple) -> int:
    return int(ctx.con.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", params).fetchone()[0])


def _delete_batches(ctx: Ctx, table: str, where: str, params: tuple = ()) -> int:
    """Delete rows of ``table`` matching ``where`` in batches of ``ctx.batch`` (each its own
    transaction). Dry run: the count. Idempotent: a second run finds nothing."""
    if ctx.dry:
        return _count(ctx, table, where, params)
    total = 0
    while True:
        cur = ctx.con.execute(
            f"DELETE FROM {table} WHERE rowid IN (SELECT rowid FROM {table} WHERE {where} LIMIT ?)",
            (*params, ctx.batch))
        n = cur.rowcount
        total += n
        if n < ctx.batch:
            return total
        time.sleep(ctx.sleep)


def _table_exists(con: sqlite3.Connection, name: str) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE name=?", (name,)).fetchone() is not None


def _columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}


# --------------------------------------------------------------------------- #
# The steps. Each returns (rows, bytes); ``ctx.note`` carries an explanation for the report.
# --------------------------------------------------------------------------- #


def step_sync_media_index(ctx: Ctx) -> tuple[int, int]:
    """Keep ``media_refs`` and the generated-media record current (new messages only)."""
    out = mi.scan_refs(ctx.con, sleep=ctx.sleep)
    added = mi.sync_generated(ctx.con, ctx.data)
    ctx.note = f"{out['scanned']} messages read, {added} generated pictures recorded"
    return out["scanned"] + added, 0


def step_prune_activity(ctx: Ctx) -> tuple[int, int]:
    act = int(cfg("system.retention.activity_days"))
    aud = int(cfg("system.retention.audit_days"))
    n = _delete_batches(ctx, "activity", "category != 'audit' AND ts < ?", (ctx.cutoff_iso(act),))
    n += _delete_batches(ctx, "activity", "category = 'audit' AND ts < ?", (ctx.cutoff_iso(aud),))
    return n, 0


def step_prune_task_events(ctx: Ctx) -> tuple[int, int]:
    if not _table_exists(ctx.con, "task_events"):
        return 0, 0
    days = int(cfg("system.retention.task_event_days"))
    cut = ctx.cutoff_iso(days)
    states = ",".join(f"'{s}'" for s in FINISHED_TASK_STATES)
    if ctx.dry:
        return int(ctx.con.execute(
            "SELECT COUNT(*) FROM task_events e WHERE EXISTS (SELECT 1 FROM tasks t WHERE t.task_id=e.task_id "
            f"AND t.state IN ({states}) AND COALESCE(t.finished_at, t.created_at) < ?)", (cut,)).fetchone()[0]), 0
    total = 0
    while True:
        ids = [r[0] for r in ctx.con.execute(
            f"SELECT t.task_id FROM tasks t WHERE t.state IN ({states}) AND COALESCE(t.finished_at, t.created_at) < ? "
            "AND EXISTS (SELECT 1 FROM task_events e WHERE e.task_id = t.task_id) LIMIT 100", (cut,))]
        if not ids:
            break
        for tid in ids:
            while True:                    # a long task has many events: bounded groups
                cur = ctx.con.execute(
                    "DELETE FROM task_events WHERE task_id = ? AND seq IN "
                    "(SELECT seq FROM task_events WHERE task_id = ? LIMIT ?)", (tid, tid, ctx.batch))
                total += cur.rowcount
                if cur.rowcount < ctx.batch:
                    break
                time.sleep(ctx.sleep)
        time.sleep(ctx.sleep)
    # events whose task row is gone altogether (ts is epoch seconds)
    epoch = (ctx.now - timedelta(days=days)).timestamp()
    while True:
        cur = ctx.con.execute(
            "DELETE FROM task_events WHERE task_id IN (SELECT task_id FROM (SELECT DISTINCT task_id FROM task_events "
            "WHERE ts < ? AND task_id NOT IN (SELECT task_id FROM tasks) LIMIT 50))", (epoch,))
        total += cur.rowcount
        if cur.rowcount == 0:
            break
        time.sleep(ctx.sleep)
    return total, 0


_CLOSED = ",".join(f"'{s}'" for s in CLOSED_JOB_STATES)


def step_prune_job_events(ctx: Ctx) -> tuple[int, int]:
    if not _table_exists(ctx.con, "job_events") or not _table_exists(ctx.con, "jobs"):
        return 0, 0
    cut = ctx.cutoff_iso(int(cfg("system.retention.job_event_days")))
    where = ("created_at < ? AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.job_id = job_events.job_id "
             f"AND j.state NOT IN ({_CLOSED}))")
    return _delete_batches(ctx, "job_events", where, (cut,)), 0


def step_prune_usage_daily(ctx: Ctx) -> tuple[int, int]:
    cut = ctx.cutoff_day(int(cfg("system.retention.usage_days")))
    return _delete_batches(ctx, "usage_daily", "day < ?", (cut,)), 0


def step_prune_transcript_seen(ctx: Ctx) -> tuple[int, int]:
    if not _table_exists(ctx.con, "transcript_seen"):
        return 0, 0
    cut = ctx.cutoff_iso(int(cfg("system.retention.transcript_seen_days")))
    n = _delete_batches(ctx, "transcript_seen", "created_at < ?", (cut,))
    n += _delete_batches(ctx, "transcript_seen", "thread_id NOT IN (SELECT id FROM threads)")
    return n, 0


def step_prune_sessions(ctx: Ctx) -> tuple[int, int]:
    """Expired session rows and long-idle devices. A remembered device is kept while its sliding
    window (``people.remember_device_days``, at least 35 days) is still open."""
    remember = 0
    with contextlib.suppress(Exception):
        remember = int(cfg("people.remember_device_days") or 0)
    window = max(35, remember)
    now_iso = ctx.now.isoformat(timespec="seconds")
    idle = ctx.cutoff_iso(35)
    keep_persistent = ctx.cutoff_iso(window)
    n = _delete_batches(
        ctx, "sessions",
        "(expires IS NOT NULL AND expires < ?) OR (persistent = 0 AND last_seen < ?) "
        "OR (persistent = 1 AND last_seen < ?)", (now_iso, idle, keep_persistent))
    n += _delete_batches(
        ctx, "devices",
        "COALESCE(last_seen, created) < ? AND NOT EXISTS (SELECT 1 FROM sessions s WHERE s.device_id = devices.id)",
        (keep_persistent,))
    return n, 0


def step_prune_agent_spend(ctx: Ctx) -> tuple[int, int]:
    if not _table_exists(ctx.con, "agent_spend"):
        return 0, 0
    return _delete_batches(ctx, "agent_spend", "day < ?", (ctx.cutoff_day(int(cfg("system.retention.agent_spend_days"))),)), 0


def step_prune_approvals(ctx: Ctx) -> tuple[int, int]:
    if not _table_exists(ctx.con, "approvals"):
        return 0, 0
    cut = ctx.cutoff_iso(int(cfg("system.retention.approval_days")))
    return _delete_batches(ctx, "approvals", "decision IS NOT NULL AND decided_at IS NOT NULL AND decided_at < ?",
                           (cut,)), 0


def step_prune_job_feedback(ctx: Ctx) -> tuple[int, int]:
    if not _table_exists(ctx.con, "job_feedback") or not _table_exists(ctx.con, "jobs"):
        return 0, 0
    cut = ctx.cutoff_iso(int(cfg("system.retention.job_feedback_days")))
    where = ("created_at < ? AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.job_id = job_feedback.job_id "
             f"AND j.state NOT IN ({_CLOSED}))")
    return _delete_batches(ctx, "job_feedback", where, (cut,)), 0


def _protected_admin_id() -> str | None:
    with contextlib.suppress(Exception):
        from . import ownership
        return ownership.protected_admin_id()
    return None


def _archive_where(con: sqlite3.Connection, pid: str, months: int, now: datetime) -> tuple[str, list[Any]]:
    """The thread filter "this profile's chats older than ``months``" (shared by the step and the
    count the Account card shows before saving). A thread is left alone while pinned, thinking, or
    tied to a queued/running picture job or an open agent task; it counts as active until both its
    last message and its last read are older than the cutoff, so opening an archived chat and
    un-archiving it sticks."""
    cutoff = mi.months_ago(now, int(months)).isoformat(timespec="seconds")
    owner = "owner_profile_id = ?"
    if pid == _protected_admin_id():
        owner = "(owner_profile_id = ? OR owner_profile_id IS NULL)"
    where = (f"{owner} AND is_archived = 0 AND is_pinned = 0 AND status != 'thinking' "
             "AND updated_at < ? AND COALESCE(last_read_at, '') < ?")
    if _table_exists(con, "image_jobs"):
        where += (" AND NOT EXISTS (SELECT 1 FROM image_jobs j WHERE j.thread_id = threads.id "
                  "AND j.state IN ('queued','running'))")
    if _table_exists(con, "tasks"):
        open_states = ",".join(f"'{s}'" for s in ("queued", "running", "awaiting_approval"))
        where += (" AND NOT EXISTS (SELECT 1 FROM tasks k WHERE k.origin_thread_id = threads.id "
                  f"AND k.state IN ({open_states}))")
    return where, [pid, cutoff, cutoff]


def archive_count(profile_id: str, months: int, now: datetime | None = None) -> int:
    """How many of ``profile_id``'s chats an "older than ``months``" rule would archive now."""
    con = connect()
    try:
        where, params = _archive_where(con, profile_id, months, now or datetime.now(UTC))
        return int(con.execute(f"SELECT COUNT(*) FROM threads WHERE {where}", params).fetchone()[0])
    finally:
        con.close()


def step_archive_chats(ctx: Ctx) -> tuple[int, int]:
    """Per-profile "archive chats older than N months": sets ``is_archived`` (reversible), never
    deletes. Off for every profile until it sets a value for itself."""
    if "archive_after_months" not in _columns(ctx.con, "profiles"):
        ctx.note = "profiles.archive_after_months is not there yet (migration 011)"
        return 0, 0
    profs = ctx.con.execute(
        "SELECT id, archive_after_months FROM profiles WHERE archive_after_months IS NOT NULL "
        "AND archive_after_months > 0").fetchall()
    total = 0
    for pid, months in profs:
        where, params = _archive_where(ctx.con, pid, int(months), ctx.now)
        if ctx.dry:
            total += _count(ctx, "threads", where, tuple(params))
            continue
        while True:
            cur = ctx.con.execute(
                f"UPDATE threads SET is_archived = 1 WHERE rowid IN (SELECT rowid FROM threads WHERE {where} LIMIT ?)",
                (*params, 1000))
            total += cur.rowcount
            if cur.rowcount < 1000:
                break
            time.sleep(ctx.sleep)
    return total, 0


def step_image_retention(ctx: Ctx) -> tuple[int, int]:
    mode = str(cfg("system.retention.images.mode") or "forever")
    months = int(cfg("system.retention.images.months") or 12)
    if mode != "months":
        ctx.note = "images are kept forever: nothing to do"
        return 0, 0
    plan = mi.plan_expiry(ctx.con, ctx.data, "months", months, ctx.now)
    ctx.extra["images"] = plan.summary()
    if ctx.dry:
        return len(plan.candidates), sum(a.size for a in plan.candidates)
    res = mi.execute_expiry(ctx.con, ctx.data, plan, now=ctx.now, sleep=ctx.sleep)
    ctx.note = f"moved to {res['trash']}" if res["rows"] else ""
    return res["rows"], res["bytes"]


def step_prune_image_jobs(ctx: Ctx) -> tuple[int, int]:
    if not _table_exists(ctx.con, "image_jobs"):
        return 0, 0
    cut = ctx.cutoff_iso(int(cfg("system.retention.image_job_days")))
    return _delete_batches(ctx, "image_jobs", "state IN ('done','failed','cancelled') AND updated_at < ?", (cut,)), 0


def step_orphan_sweep(ctx: Ctx) -> tuple[int, int]:
    found = mi.find_orphans(ctx.con, ctx.data, grace_days=int(cfg("system.retention.orphan_grace_days")), now=ctx.now)
    ctx.extra["orphans_refused"] = found["refused"]
    ctx.extra["files_without_blob"] = found["missing"][:50]
    notes = []
    if found["refused"]:
        notes.append("refused: " + "; ".join(f"{k}: {v}" for k, v in found["refused"].items()))
    if found["missing"]:
        notes.append(f"{len(found['missing'])} file record(s) have no blob (reported, not deleted)")
    ctx.note = " | ".join(notes)
    if ctx.dry:
        return len(found["candidates"]), sum(c[2] for c in found["candidates"])
    res = mi.move_orphans(found, ctx.data, now=ctx.now, sleep=ctx.sleep)
    return res["rows"], res["bytes"]


def step_trash_purge(ctx: Ctx) -> tuple[int, int]:
    if ctx.dry:
        return 0, 0
    res = mi.purge_trash(ctx.data, images_days=int(cfg("system.retention.images.trash_days")),
                         orphans_days=int(cfg("system.retention.orphan_trash_days")), now=ctx.now)
    return res["rows"], res["bytes"]


def step_backup_tiers(ctx: Ctx) -> tuple[int, int]:
    from . import backups
    res = backups.prune_snapshot_tiers(
        keep_recent=int(cfg("system.backups.keep_recent")), keep_daily=int(cfg("system.backups.keep_daily")),
        keep_weekly=int(cfg("system.backups.keep_weekly")), dry_run=ctx.dry)
    pre = backups.prune_premigration(3, dry_run=ctx.dry)
    ctx.note = f"kept {res['kept']} snapshot(s)"
    return res["removed"] + pre["removed"], res["bytes"] + pre["bytes"]


def step_fts_maintenance(ctx: Ctx) -> tuple[int, int]:
    """A bounded ``merge`` every night (many short write locks), a full ``optimize`` on Sundays
    for a small index, or an extended bounded merge for a large one."""
    if ctx.dry:
        return 0, 0
    if not _table_exists(ctx.con, "messages_fts"):
        ctx.note = "no full-text index"
        return 0, 0
    sunday = ctx.now.astimezone().weekday() == 6
    rounds = 200 if sunday else 20
    size = ctx.con.execute("SELECT COALESCE(MAX(rowid),0) FROM messages").fetchone()[0]
    if sunday and size < 200_000:
        ctx.con.execute("INSERT INTO messages_fts(messages_fts) VALUES('optimize')")
        ctx.note = "optimize"
        return 1, 0
    done = 0
    for _ in range(rounds):
        before = ctx.con.total_changes
        ctx.con.execute("INSERT INTO messages_fts(messages_fts, rank) VALUES('merge', 64)")
        done += 1
        if ctx.con.total_changes - before < 2:
            break
        time.sleep(ctx.sleep)
    ctx.note = f"merge x{done}" + (" (weekly, extended)" if sunday else "")
    return done, 0


def step_sqlite_optimize(ctx: Ctx) -> tuple[int, int]:
    if ctx.dry:
        return 0, 0
    ctx.con.execute("PRAGMA analysis_limit=1000")
    ctx.con.execute("PRAGMA optimize")
    return 1, 0


def step_incremental_vacuum(ctx: Ctx) -> tuple[int, int]:
    """Give freed pages back to the filesystem, a few thousand pages per call."""
    mode = ctx.con.execute("PRAGMA auto_vacuum").fetchone()[0]
    page = ctx.con.execute("PRAGMA page_size").fetchone()[0]
    free = ctx.con.execute("PRAGMA freelist_count").fetchone()[0]
    if mode != 2:
        ctx.note = "auto_vacuum is not incremental on this database (migration 011 turns it on)"
        return 0, 0
    if ctx.dry:
        return free, free * page
    freed = 0
    for _ in range(200):
        left = ctx.con.execute("PRAGMA freelist_count").fetchone()[0]
        if left <= 0:
            break
        ctx.con.execute("PRAGMA incremental_vacuum(2000)").fetchall()
        freed += left - ctx.con.execute("PRAGMA freelist_count").fetchone()[0]
        time.sleep(ctx.sleep)
    return freed, freed * page


def step_wal_checkpoint(ctx: Ctx) -> tuple[int, int]:
    if ctx.dry:
        return 0, 0
    for attempt in (1, 2):
        busy, log_pages, ckpt = ctx.con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if not busy:
            return ckpt, 0
        if attempt == 1:
            time.sleep(1.0)
    ctx.note = "a reader kept the WAL open; it will shrink at the next checkpoint"
    return 0, 0


def step_storage_scan(ctx: Ctx) -> tuple[int, int]:
    if ctx.dry:
        return 0, 0
    areas = scan_storage(ctx.data, ctx.con)
    return sum(a["files"] for a in areas.values()), sum(a["bytes"] for a in areas.values())


STEPS: tuple[tuple[str, Callable[[Ctx], tuple[int, int]]], ...] = (
    ("sync_media_index", step_sync_media_index),
    ("prune_activity", step_prune_activity),
    ("prune_task_events", step_prune_task_events),
    ("prune_job_events", step_prune_job_events),
    ("prune_usage_daily", step_prune_usage_daily),
    ("prune_transcript_seen", step_prune_transcript_seen),
    ("prune_sessions", step_prune_sessions),
    ("prune_agent_spend", step_prune_agent_spend),
    ("prune_approvals", step_prune_approvals),
    ("prune_job_feedback", step_prune_job_feedback),
    ("archive_chats", step_archive_chats),
    ("image_retention", step_image_retention),
    ("prune_image_jobs", step_prune_image_jobs),
    ("orphan_sweep", step_orphan_sweep),
    ("trash_purge", step_trash_purge),
    ("backup_tiers", step_backup_tiers),
    ("fts_maintenance", step_fts_maintenance),
    ("sqlite_optimize", step_sqlite_optimize),
    ("incremental_vacuum", step_incremental_vacuum),
    ("wal_checkpoint", step_wal_checkpoint),
    ("storage_scan", step_storage_scan),
)
STEP_NAMES = tuple(n for n, _ in STEPS)


# --------------------------------------------------------------------------- #
# Storage measurement (the Storage card; cached in storage_stats)
# --------------------------------------------------------------------------- #

AREAS = ("database", "wal", "media", "imagestudio", "reactions_spent", "reactions_shelf", "avatars",
         "avatar_pool", "files", "voices", "backups_6h", "backups_daily", "backups_weekly",
         "backups_other", "trash", "stray_bak", "other")


def _area_of(rel: str, parts: list[str]) -> str:
    top = parts[0]
    if len(parts) == 1:
        if top in ("chats.db", "chats.db-shm"):
            return "database"
        if top == "chats.db-wal":
            return "wal"
        if ".bak" in top:
            return "stray_bak"
        return "other"
    if top == "media":
        return "media"
    if top == "reactions":
        return "reactions_spent" if parts[1] == "spent" else "reactions_shelf"
    if top in ("avatars", "avatar-snapshots"):
        return "avatars"
    if top == "avatar-pool":
        return "avatar_pool"
    if top in ("files",):
        return "files"
    if top == "voices":
        return "voices"
    if top == "trash":
        return "trash"
    if top == "backups":
        if len(parts) >= 3 and parts[1] == "tiers":
            return "backups_daily" if parts[2] == "daily" else "backups_weekly" if parts[2] == "weekly" else "backups_other"
        if len(parts) == 2 and parts[1].startswith("chats-") and parts[1].endswith(".db"):
            return "backups_6h"
        return "backups_other"
    return "other"


def scan_storage(data: Path, con: sqlite3.Connection | None = None) -> dict[str, dict[str, int]]:
    """Walk the data folder once and total each area (hard links counted once, so the sum matches
    ``du``). Stores the result in ``storage_stats`` (+ the stray-file list and the database's
    full-text share in ``maintenance_state``) and returns ``{area: {bytes, files}}``."""
    areas = {a: {"bytes": 0, "files": 0} for a in AREAS}
    seen: set[tuple[int, int]] = set()
    stray: list[dict[str, Any]] = []
    studio = set()
    own = con is None
    if own:
        con = connect()
    try:
        studio = {r[0] for r in con.execute("SELECT name FROM generated_media WHERE kind='imagestudio'")}
        tiers_first = [data / "backups" / "tiers"]
        roots: list[Path] = [*tiers_first, data]
        done_tiers = False
        for root in roots:
            stack = [root]
            while stack:
                d = stack.pop()
                if d == data / "backups" / "tiers" and done_tiers:
                    continue
                try:
                    it = os.scandir(d)
                except OSError:
                    continue
                with it:
                    for e in it:
                        try:
                            if e.is_dir(follow_symlinks=False):
                                stack.append(Path(e.path))
                                continue
                            st = e.stat(follow_symlinks=False)
                        except OSError:
                            continue
                        if not (e.is_file(follow_symlinks=False)):
                            continue
                        key = (st.st_dev, st.st_ino)
                        if st.st_nlink > 1:
                            if key in seen:
                                continue
                            seen.add(key)
                        rel = os.path.relpath(e.path, data)
                        parts = rel.split(os.sep)
                        area = _area_of(rel, parts)
                        if area == "media" and e.name in studio:
                            area = "imagestudio"
                        a = areas[area]
                        a["bytes"] += st.st_size
                        a["files"] += 1
                        if area == "stray_bak" and len(stray) < 100:
                            stray.append({"name": e.name, "bytes": st.st_size})
            done_tiers = True
        fts = 0
        with contextlib.suppress(sqlite3.Error):
            fts = int(con.execute("SELECT COALESCE(SUM(pgsize),0) FROM dbstat WHERE name LIKE 'messages_fts%'").fetchone()[0])
        now = datetime.now(UTC).isoformat(timespec="seconds")
        con.execute("BEGIN IMMEDIATE")
        try:
            for name, v in areas.items():
                con.execute("INSERT INTO storage_stats(area,bytes,files,measured_at) VALUES(?,?,?,?) "
                            "ON CONFLICT(area) DO UPDATE SET bytes=excluded.bytes, files=excluded.files, "
                            "measured_at=excluded.measured_at", (name, v["bytes"], v["files"], now))
            con.execute("INSERT INTO storage_stats(area,bytes,files,measured_at) VALUES('fts',?,0,?) "
                        "ON CONFLICT(area) DO UPDATE SET bytes=excluded.bytes, measured_at=excluded.measured_at",
                        (fts, now))
            mi.state_set(con, "storage.stray", json.dumps(stray))
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        return areas
    finally:
        if own:
            con.close()


def storage_report() -> dict[str, Any]:
    """What ``GET /api/maintenance/storage`` returns: the cached numbers, never a tree walk."""
    from . import backups
    data = _data_dir()
    out: dict[str, Any] = {"measured_at": None, "scanning": _STORAGE_LOCK.locked(), "areas": {},
                           "total_bytes": 0, "database_fts_bytes": 0, "stray": [], "free_disk_bytes": 0,
                           "backups": {"recent": 0, "daily": 0, "weekly": 0}}
    with contextlib.suppress(OSError):
        import shutil
        out["free_disk_bytes"] = shutil.disk_usage(data).free
    try:
        con = connect()
    except sqlite3.Error:
        return out
    try:
        rows = con.execute("SELECT area, bytes, files, measured_at FROM storage_stats").fetchall()
        for area, nbytes, files, at in rows:
            if area == "fts":
                out["database_fts_bytes"] = nbytes
                continue
            out["areas"][area] = {"bytes": nbytes, "files": files}
            if at and (out["measured_at"] is None or at > out["measured_at"]):
                out["measured_at"] = at
        out["total_bytes"] = sum(a["bytes"] for a in out["areas"].values())
        with contextlib.suppress(Exception):
            out["stray"] = json.loads(mi.state_get(con, "storage.stray", "[]") or "[]")
    finally:
        con.close()
    with contextlib.suppress(Exception):
        for r in backups.list_backups():
            if r["kind"] == "snapshot":
                out["backups"][r.get("tier", "recent")] = out["backups"].get(r.get("tier", "recent"), 0) + 1
    return out


def refresh_storage_async() -> bool:
    """Start a storage walk in a thread unless one is running. True when started."""
    if not _STORAGE_LOCK.acquire(blocking=False):
        return False

    def run() -> None:
        try:
            scan_storage(_data_dir())
        except Exception:
            log.exception("storage scan failed")
        finally:
            _STORAGE_LOCK.release()

    threading.Thread(target=run, name="storage-scan", daemon=True).start()
    return True


# --------------------------------------------------------------------------- #
# Running
# --------------------------------------------------------------------------- #


def _iso(t: datetime) -> str:
    return t.astimezone(UTC).isoformat(timespec="microseconds")


def _record(con: sqlite3.Connection, run_started: str, step: str, started: str, finished: str,
            rows: int, nbytes: int, ms: int, error: str | None) -> None:
    con.execute("INSERT INTO maintenance_runs(started, finished, step, rows, bytes, ms, error) "
                "VALUES(?,?,?,?,?,?,?)", (run_started, finished, step, rows, nbytes, ms, error))


def run_nightly(*, now: datetime | None = None, steps: list[str] | None = None, dry_run: bool = False,
                trigger: str = "schedule", respect_blockers: bool = False,
                batch: int | None = None, sleep: float | None = None) -> dict:
    """Run every maintenance step (or only ``steps``) once and return
    ``{"started", "finished", "dry_run", "trigger", "ok", "steps": [{"step", "rows", "bytes", "ms",
    "error", "note"}]}``. A real run records ``maintenance_runs`` rows; a dry run changes and records
    nothing (``rows`` is what the step would do). Raises :class:`MaintenanceBusy` when another run
    holds the lock and, with ``respect_blockers``, :class:`MaintenanceBlocked`."""
    if not _RUN_LOCK.acquire(blocking=False):
        raise MaintenanceBusy("a maintenance run is already in progress")
    try:
        if respect_blockers and not dry_run:
            reason = blocked_reason()
            if reason:
                raise MaintenanceBlocked(reason)
        wanted = list(steps) if steps else list(STEP_NAMES)
        unknown = [s for s in wanted if s not in STEP_NAMES]
        if unknown:
            raise ValueError(f"unknown maintenance step(s): {', '.join(unknown)}")
        now = now or datetime.now(UTC)
        _state.update(running=True, since=_iso(now), trigger=trigger)
        con = connect()
        run_started = _iso(datetime.now(UTC))
        t_run = time.monotonic()
        results: list[dict[str, Any]] = []
        interrupted: str | None = None
        try:
            ctx = Ctx(con=con, data=_data_dir(), now=now, dry=dry_run)
            if batch:
                ctx.batch = batch
            if sleep is not None:
                ctx.sleep = sleep
            for name, fn in STEPS:
                if name not in wanted:
                    continue
                if respect_blockers and not dry_run:
                    reason = blocked_reason()
                    if reason:
                        interrupted = reason
                        break
                ctx.note = ""
                ctx.extra = {}
                started = _iso(datetime.now(UTC))
                t0 = time.monotonic()
                rows = nbytes = 0
                error: str | None = None
                try:
                    rows, nbytes = fn(ctx)
                except Exception as e:
                    error = f"{type(e).__name__}: {e}"[:500]
                    log.exception("maintenance step %s failed", name)
                ms = int((time.monotonic() - t0) * 1000)
                finished = _iso(datetime.now(UTC))
                results.append({"step": name, "rows": int(rows), "bytes": int(nbytes), "ms": ms, "error": error,
                                "note": ctx.note, **({"images": ctx.extra["images"]} if "images" in ctx.extra else {})})
                if not dry_run:
                    try:
                        _record(con, run_started, name, started, finished, int(rows), int(nbytes), ms, error)
                    except Exception:
                        log.exception("could not record maintenance step %s", name)
            failed = [r for r in results if r["error"]]
            total_ms = int((time.monotonic() - t_run) * 1000)
            ok = not failed and interrupted is None
            if not dry_run:
                summary_error = None
                if interrupted:
                    summary_error = f"stopped before the next step: {interrupted}"
                elif failed:
                    summary_error = f"{len(failed)} step(s) failed: " + ", ".join(r["step"] for r in failed)
                with contextlib.suppress(Exception):
                    _record(con, run_started, "run", run_started, _iso(datetime.now(UTC)),
                            sum(r["rows"] for r in results), sum(r["bytes"] for r in results), total_ms,
                            summary_error)
            if not dry_run and "wal_checkpoint" in wanted:
                # the bookkeeping rows written after the checkpoint step grew the WAL again
                with contextlib.suppress(Exception):
                    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            return {"started": run_started, "finished": _iso(datetime.now(UTC)), "dry_run": dry_run,
                    "trigger": trigger, "ok": ok, "interrupted": interrupted, "ms": total_ms, "steps": results}
        finally:
            con.close()
    finally:
        _state.update(running=False, since=None, trigger=None)
        _RUN_LOCK.release()


def last_run() -> dict[str, Any] | None:
    """The newest recorded run with its steps (from ``maintenance_runs``), or None."""
    try:
        con = connect()
    except sqlite3.Error:
        return None
    try:
        row = con.execute("SELECT started, finished, rows, bytes, ms, error FROM maintenance_runs "
                          "WHERE step='run' ORDER BY id DESC LIMIT 1").fetchone()
        if row is None:
            return None
        steps = con.execute("SELECT step, rows, bytes, ms, error, started, finished FROM maintenance_runs "
                            "WHERE started=? AND step != 'run' ORDER BY id", (row[0],)).fetchall()
        return {"started": row[0], "finished": row[1], "rows": row[2], "bytes": row[3], "ms": row[4],
                "ok": row[5] is None, "error": row[5],
                "steps": [{"step": s, "rows": r, "bytes": b, "ms": m, "error": e} for s, r, b, m, e, _a, _f in steps]}
    finally:
        con.close()


def settings_view() -> dict[str, Any]:
    from . import settings_schema
    keys = [d.key for d in settings_schema.all_defs()
            if d.key.startswith(("system.retention.", "system.maintenance.", "system.backups.keep_"))]
    return {"values": {k: cfg(k) for k in keys},
            "defaults": {k: settings_schema.get_def(k).default for k in keys}}


def status() -> dict[str, Any]:
    now = datetime.now().astimezone()
    hhmm = cfg("system.maintenance.time")
    enabled = bool(cfg("system.maintenance.enabled"))
    return {"enabled": enabled, "time": "{:02d}:{:02d}".format(*parse_time(hhmm)),
            "next_run": next_run_at(now, hhmm).isoformat(timespec="minutes") if enabled else None,
            "running": _RUN_LOCK.locked(), "trigger": _state.get("trigger"), "since": _state.get("since"),
            "blocked": blocked_reason(), "last_run": last_run(),
            "images": {"mode": cfg("system.retention.images.mode"), "months": cfg("system.retention.images.months"),
                       "trash_days": cfg("system.retention.images.trash_days")}}


# --------------------------------------------------------------------------- #
# The loop
# --------------------------------------------------------------------------- #


def due(now_local: datetime | None = None) -> bool:
    """True when the scheduled time today has passed and no run has started since it."""
    now_local = now_local or datetime.now().astimezone()
    h, m = parse_time(cfg("system.maintenance.time"))
    sched = now_local.replace(hour=h, minute=m, second=0, microsecond=0)
    if now_local < sched:
        return False
    last = last_run()
    if last is None:
        return True
    try:
        started = datetime.fromisoformat(last["started"]).astimezone()
    except (ValueError, TypeError):
        return True
    return started < sched


async def _loop() -> None:
    try:
        await asyncio.sleep(STARTUP_DELAY)
        retry_at = 0.0
        while True:
            try:
                if cfg("system.maintenance.enabled") and time.monotonic() >= retry_at and await asyncio.to_thread(due):
                    reason = blocked_reason()
                    if reason:
                        retry_at = time.monotonic() + RETRY_SECONDS
                        log.info("maintenance waits (%s); trying again in 30 minutes", reason)
                    else:
                        res = await asyncio.to_thread(run_nightly, trigger="schedule", respect_blockers=True)
                        log.info("maintenance run finished: %s in %d ms", "ok" if res["ok"] else "with errors", res["ms"])
            except MaintenanceBlocked as e:
                retry_at = time.monotonic() + RETRY_SECONDS
                log.info("maintenance waits (%s); trying again in 30 minutes", e.reason)
            except MaintenanceBusy:
                pass
            except Exception:
                log.exception("maintenance loop pass failed; carrying on")
            await asyncio.sleep(POLL_SECONDS)
    except asyncio.CancelledError:
        pass


def start(app: Any) -> None:
    """Start the daily loop (once per process). Never raises: a failure to schedule maintenance
    must not stop the server."""
    global _task
    try:
        if os.environ.get("DISPATCH_MAINTENANCE", "1").strip().lower() in ("0", "off", "false", "no"):
            return
        if _task is not None and not _task.done():
            return
        _task = asyncio.get_running_loop().create_task(_loop(), name="maintenance")
    except Exception:
        log.exception("could not start the maintenance loop")


async def stop(app: Any = None) -> None:
    """Cancel the loop and wait for it. ``main`` calls this from the lifespan shutdown, which has
    no app handle, so the task is kept here."""
    global _task
    task, _task = _task, None
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task


# --------------------------------------------------------------------------- #
# Command line: python -m app.maintenance run|dry-run|restore-images
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="python -m app.maintenance",
                                 description="Run or inspect the daily maintenance task.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run every step now")
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--step", action="append", default=[])
    sub.add_parser("status", help="print the schedule and the last run")
    ri = sub.add_parser("restore-images", help="put expired pictures back from the trash")
    ri.add_argument("--date", help="YYYYMMDD of the trash folder (default: all)")
    ri.add_argument("--name", action="append", default=[])
    args = ap.parse_args(argv)
    if args.cmd == "status":
        print(json.dumps(status(), indent=1, default=str))
        return 0
    if args.cmd == "run":
        try:
            res = run_nightly(steps=args.step or None, dry_run=args.dry_run, trigger="cli",
                              respect_blockers=not args.dry_run)
        except MaintenanceBlocked as e:
            print(f"blocked: {e.reason}")
            return 3
        print(json.dumps(res, indent=1, default=str))
        return 0 if res["ok"] else 1
    con = connect()
    try:
        out = mi.restore_images(con, _data_dir(), date=args.date, names=args.name or None)
    finally:
        con.close()
    print(json.dumps(out))
    return 0


if __name__ == "__main__":      # pragma: no cover
    raise SystemExit(main())
