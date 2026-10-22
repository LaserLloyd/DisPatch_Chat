"""Maintenance, storage and retention routes (4.0 scale and retention, BUILD-PLAN-v2 §1.13).

All Admin-only (permission ``admin_system``; Users and Basic profiles get 403). Declared in
``route_policy.py``'s WP-SCALE-OPS section.

* ``GET  /api/maintenance/status``             schedule, next run, whether one is running or blocked,
                                               the last run with every step's rows/bytes/ms/error
* ``GET  /api/maintenance/storage``            cached size per area (never a tree walk), free disk,
                                               stray ``*.bak`` files, snapshot counts per tier
* ``POST /api/maintenance/storage/refresh``    start a new walk in the background (202)
* ``POST /api/maintenance/run``                ``{"dry_run": bool, "steps": [..]}``: a dry run answers
                                               at once with what each step would do; a real run starts
                                               in the background (202) and ``status`` reports it
* ``GET  /api/maintenance/images/preview``     ``?mode=months&months=12`` the dry-run count the
                                               Settings card shows before it saves the image rule
* ``GET  /api/maintenance/retention``          every retention/maintenance setting with its default
* ``PUT  /api/maintenance/retention``          ``{"system.retention.images.mode": "months", ...}`` validated
                                               and saved (an unknown key or a bad value is 422)
* ``POST /api/maintenance/images/restore``     ``{"date": "YYYYMMDD", "names": [..]}`` put expired
                                               pictures back from the trash (inside the hold)
"""

from __future__ import annotations

import asyncio
import re
import threading

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from . import maintenance, policy, settings_store
from . import media_inventory as mi
from .api_common import audit, caller, fail
from .contracts.permissions import Permission

router = APIRouter()
_SYSTEM = Depends(policy.require_permission(Permission.ADMIN_SYSTEM))
_HISTORY = Depends(policy.require_permission(Permission.MANAGE_HISTORY))

#: The keys PUT /retention accepts.
_PUT_PREFIXES = ("system.retention.", "system.maintenance.", "system.backups.keep_")


class RunIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dry_run: bool = False
    steps: list[str] | None = Field(default=None, max_length=40)


class ArchiveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    months: int | None = Field(default=None, ge=1, le=120)      # null = off


class RestoreIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date: str | None = Field(default=None, pattern=r"^\d{8}$")
    names: list[str] | None = Field(default=None, max_length=5000)


@router.get("/api/maintenance/status", dependencies=[_SYSTEM])
async def get_status():
    return await asyncio.to_thread(maintenance.status)


@router.get("/api/maintenance/storage", dependencies=[_SYSTEM])
async def get_storage():
    return await asyncio.to_thread(maintenance.storage_report)


@router.post("/api/maintenance/storage/refresh", status_code=202, dependencies=[_SYSTEM])
async def refresh_storage():
    return {"started": maintenance.refresh_storage_async()}


@router.post("/api/maintenance/run", dependencies=[_SYSTEM])
async def run_now(request: Request, payload: RunIn | None = None):
    p = payload or RunIn()
    bad = [s for s in (p.steps or []) if s not in maintenance.STEP_NAMES]
    if bad:
        raise fail(422, "maintenance_step_unknown", f"Unknown step: {', '.join(bad[:5])}")
    if p.dry_run:
        try:
            res = await asyncio.to_thread(maintenance.run_nightly, steps=p.steps, dry_run=True, trigger="manual")
        except maintenance.MaintenanceBusy:
            raise fail(409, "maintenance_running", "A maintenance run is already in progress.") from None
        audit(request, "maintenance.dry_run", "")
        return res
    reason = maintenance.blocked_reason()
    if reason:
        raise fail(409, "maintenance_blocked",
                   "A backup is being written." if reason == "backup_running"
                   else "An operator maintenance window is open; try again when it closes.")
    if maintenance._RUN_LOCK.locked():
        raise fail(409, "maintenance_running", "A maintenance run is already in progress.")

    def go() -> None:
        try:
            maintenance.run_nightly(steps=p.steps, trigger="manual", respect_blockers=True)
        except (maintenance.MaintenanceBusy, maintenance.MaintenanceBlocked):
            pass
        except Exception:
            maintenance.log.exception("manual maintenance run failed")

    threading.Thread(target=go, name="maintenance-manual", daemon=True).start()
    audit(request, "maintenance.run", ",".join(p.steps or ["all"])[:80])
    return JSONResponse({"started": True}, status_code=202)


@router.get("/api/maintenance/images/preview", dependencies=[_SYSTEM])
async def images_preview(mode: str = "months", months: int | None = None):
    if mode not in ("forever", "months"):
        raise fail(422, "retention_invalid", "mode must be forever or months.")
    if mode == "months" and (months is None or not 1 <= int(months) <= 120):
        raise fail(422, "retention_invalid", "months must be a whole number from 1 to 120.")

    def run() -> dict:
        con = maintenance.connect()
        try:
            return mi.plan_expiry(con, maintenance._data_dir(), mode, months).summary()
        finally:
            con.close()

    return await asyncio.to_thread(run)


@router.get("/api/maintenance/retention", dependencies=[_SYSTEM])
async def get_retention():
    return await asyncio.to_thread(maintenance.settings_view)


@router.put("/api/maintenance/retention", dependencies=[_SYSTEM])
async def put_retention(request: Request, body: dict):
    if not body or not isinstance(body, dict):
        raise fail(422, "retention_invalid", "Send an object of setting keys and values.")
    bad = [k for k in body if not isinstance(k, str) or not k.startswith(_PUT_PREFIXES)]
    if bad:
        raise fail(422, "retention_invalid", f"Not a retention setting: {bad[0][:60]}")
    if "system.maintenance.time" in body and not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(body["system.maintenance.time"])):
        raise fail(422, "retention_invalid", "maintenance.time must be HH:MM (24 hours).")
    res = await asyncio.to_thread(settings_store.set_many, body, actor=caller(request).profile_id or "", owner=True)
    if res.errors:
        raise fail(422, "retention_invalid", "; ".join(f"{k}: {v}" for k, v in res.errors.items())[:300])
    audit(request, "retention.update", ",".join(sorted(res.changed))[:80])
    return {"ok": True, "changed": res.changed, **maintenance.settings_view()}


@router.post("/api/maintenance/images/restore", dependencies=[_SYSTEM])
async def images_restore(request: Request, payload: RestoreIn | None = None):
    p = payload or RestoreIn()

    def run() -> dict:
        con = maintenance.connect()
        try:
            return mi.restore_images(con, maintenance._data_dir(), date=p.date, names=p.names)
        finally:
            con.close()

    out = await asyncio.to_thread(run)
    audit(request, "maintenance.images_restore", p.date or "all", f"{out['restored']} restored")
    return out


# --------------------------------------------------------------------------- #
# "Archive my chats older than N months" (the caller's own profile; nobody can set another's)
# --------------------------------------------------------------------------- #


def _my_archive(profile_id: str) -> int | None:
    from . import accounts
    with accounts.connect() as con:
        cols = {r[1] for r in con.execute("PRAGMA table_info(profiles)")}
        if "archive_after_months" not in cols:
            return None
        row = con.execute("SELECT archive_after_months FROM profiles WHERE id = ?", (profile_id,)).fetchone()
    return int(row[0]) if row and row[0] else None


def _set_my_archive(profile_id: str, months: int | None) -> None:
    import contextlib
    import sqlite3

    from . import accounts
    with accounts._lock, accounts.connect() as con:
        with contextlib.suppress(sqlite3.OperationalError):          # migration 011 adds it; be safe
            con.execute("ALTER TABLE profiles ADD COLUMN archive_after_months INTEGER")
        con.execute("UPDATE profiles SET archive_after_months = ? WHERE id = ?", (months, profile_id))
        accounts._bump_rev(con)


def _me(request: Request) -> str:
    pid = caller(request).profile_id
    if not pid:
        raise fail(403, "permission_denied", "Sign in to a profile to set this.")
    return pid


@router.get("/api/me/archive", dependencies=[_HISTORY])
async def get_my_archive(request: Request, months: int | None = None):
    """The caller's own setting; with ``?months=N`` also how many of their chats that would archive."""
    pid = _me(request)
    out = {"months": await asyncio.to_thread(_my_archive, pid)}
    if months is not None:
        if not 1 <= months <= 120:
            raise fail(422, "archive_invalid", "months must be a whole number from 1 to 120.")
        out["would_archive"] = await asyncio.to_thread(maintenance.archive_count, pid, months)
    return out


@router.put("/api/me/archive", dependencies=[_HISTORY])
async def put_my_archive(request: Request, payload: ArchiveIn):
    pid = _me(request)
    await asyncio.to_thread(_set_my_archive, pid, payload.months)
    audit(request, "profile.archive_after", pid, "off" if payload.months is None else f"{payload.months} months")
    return {"months": payload.months}
