"""Backups, restore, the support bundle, and per-user data export / erase.

* ``GET    /api/backups``                  every backup (archives, rotating snapshots, pre-migration copies)
                                           plus the schedule state            (admin_system)
* ``POST   /api/backups``                  write an encrypted archive now      (admin_system)
* ``POST   /api/backups/{id}/verify``      decrypt and check an archive        (admin_system)
* ``POST   /api/backups/{id}/restore``     stage a restore; applied at the next start (admin_system)
* ``GET    /api/backups/pending`` / ``DELETE /api/backups/pending``  see / cancel a staged restore
* ``DELETE /api/backups/{id}``             delete an archive or snapshot       (admin_system)
* ``POST   /api/support-bundle``           a redacted zip for a bug report     (admin_system)
* ``GET    /api/users/{id}/export``        everything DisPatch holds about a profile (manage_users)
* ``DELETE /api/users/{id}``               erase a profile and what is attached to it (manage_users)

Ids look like ``archive:dispatch-20261018-120000.dpbk``, ``snapshot:chats-....db`` or
``premigration:3.0.0-....``. A passphrase typed here is never stored; the one in Settings is read
from the secret store.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from . import accounts, auth, backups, config, policy, secret_store, settings_store
from .api_common import audit, caller, fail
from .contracts.permissions import Permission

router = APIRouter()

_SYSTEM = Depends(policy.require_permission(Permission.ADMIN_SYSTEM))
_USERS = Depends(policy.require_permission(Permission.MANAGE_USERS))


class BackupCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    passphrase: str | None = Field(default=None, max_length=256)
    include_media: bool | None = None


class RestoreIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    passphrase: str | None = Field(default=None, max_length=256)
    confirm: bool = False


class SupportBundleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    include_prompts: bool = False


def _err(e: backups.BackupError):
    return fail(e.status, e.code, str(e))


def _state() -> dict:
    return {
        "enabled": bool(settings_store.get("system.encrypted_backup_enabled", False)),
        "interval_hours": settings_store.get("system.encrypted_backup_interval_hours", 24),
        "keep": settings_store.get("system.encrypted_backup_keep", 7),
        "include_media": bool(settings_store.get("system.encrypted_backup_include_media", False)),
        "has_passphrase": bool(backups.configured_passphrase()),
        "last_archive_age_hours": backups.last_archive_age_hours(),
        "pending_restore": backups.pending_path().exists(),
    }


@router.get("/api/backups", dependencies=[_SYSTEM])
async def list_backups():
    rows = await asyncio.to_thread(backups.list_backups)
    return {"backups": rows, "schedule": await asyncio.to_thread(_state)}


@router.get("/api/backups/pending", dependencies=[_SYSTEM])
async def pending():
    mp = backups.pending_path()
    if not mp.exists():
        return {"pending": False}
    try:
        m = json.loads(mp.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        m = {}
    return {"pending": True, "source": m.get("source"), "staged_at": m.get("staged_at"),
            "files": m.get("files", [])}


@router.delete("/api/backups/pending", dependencies=[_SYSTEM])
async def cancel_pending(request: Request):
    gone = await asyncio.to_thread(backups.cancel_pending)
    if gone:
        audit(request, "backup.restore_cancel", "")
    return {"cancelled": gone}


@router.post("/api/backups", status_code=201, dependencies=[_SYSTEM])
async def create_backup(request: Request, payload: BackupCreateIn | None = None):
    p = payload or BackupCreateIn()
    generated = None
    pw = p.passphrase or backups.configured_passphrase()
    if not pw:
        # No passphrase anywhere: make a recovery key, keep it in the secret store, and show it ONCE.
        generated = pw = backups.recovery_key()
        secret_store.put("setting.system.backup_passphrase", pw)
    media = bool(settings_store.get("system.encrypted_backup_include_media", False)
                 if p.include_media is None else p.include_media)
    try:
        row = await asyncio.to_thread(backups.create_archive, pw, include_media=media)
    except backups.BackupError as e:
        raise _err(e) from e
    await asyncio.to_thread(backups.prune_archives, int(settings_store.get("system.encrypted_backup_keep", 7)))
    audit(request, "backup.create", row["name"])
    out = {"backup": row, "passphrase_source": "request" if p.passphrase else "settings"}
    if generated:
        out.update(recovery_key=generated, recovery_key_note=("Shown once. Write it down and keep it OFF this computer: it is also saved in this "
                                      "install's secret store as the backup passphrase, so only a copy kept elsewhere "
                                      "opens the backup if this computer is lost."),
                   passphrase_source="generated")
    from fastapi.responses import JSONResponse
    return JSONResponse(out, status_code=201)


@router.post("/api/backups/{backup_id}/verify", dependencies=[_SYSTEM])
async def verify_backup(backup_id: str, payload: RestoreIn | None = None):
    p = payload or RestoreIn()
    try:
        kind, path = backups.resolve_id(backup_id)
        if kind != "archive":
            return {"ok": True, "kind": kind, "note": "Plain backups carry no checksums; they are checked when restored."}
        pw = p.passphrase or backups.configured_passphrase()
        if not pw:
            raise backups.BackupError("backup_passphrase_required", "Enter the backup's passphrase (or recovery key).")
        manifest = await asyncio.to_thread(backups.read_archive, path, pw)
    except backups.BackupError as e:
        raise _err(e) from e
    return {"ok": True, "kind": kind, "app_version": manifest.get("app_version"), "created": manifest.get("created"),
            "files": len(manifest.get("files", [])), "includes_media": manifest.get("include_media", False)}


@router.post("/api/backups/{backup_id}/restore", dependencies=[_SYSTEM])
async def restore_backup(request: Request, backup_id: str, payload: RestoreIn):
    if not payload.confirm:
        raise fail(422, "backup_confirm_required",
                   "Restoring replaces your current chats and settings. Send confirm: true to go ahead.")
    try:
        out = await asyncio.to_thread(backups.stage_restore, backup_id, payload.passphrase)
    except backups.BackupError as e:
        raise _err(e) from e
    audit(request, "backup.restore_staged", backup_id[:80])
    out["message"] = ("The backup is checked and ready. Restart DisPatch to apply it; your current data is "
                      "copied to backups/pre-restore-<time> first. Cancel with DELETE /api/backups/pending.")
    return out


@router.delete("/api/backups/{backup_id}", dependencies=[_SYSTEM])
async def delete_backup(request: Request, backup_id: str):
    try:
        kind, path = backups.resolve_id(backup_id)
    except backups.BackupError as e:
        raise _err(e) from e
    if kind == "premigration":
        raise fail(409, "backup_protected", "Pre-migration copies are kept until they age out; delete them from the backups folder by hand if you must.")
    await asyncio.to_thread(path.unlink)
    audit(request, "backup.delete", backup_id[:80])
    return {"deleted": backup_id}


@router.post("/api/support-bundle", dependencies=[_SYSTEM])
async def support_bundle(request: Request, payload: SupportBundleIn | None = None):
    p = payload or SupportBundleIn()
    data = await asyncio.to_thread(backups.build_support_bundle, include_prompts=p.include_prompts)
    audit(request, "support_bundle", "prompts" if p.include_prompts else "")
    name = f"dispatch-support-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}.zip"
    return Response(data, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})


# --------------------------------------------------------------------------- #
# Per-user data
# --------------------------------------------------------------------------- #


def _target(request: Request, profile_id: str) -> accounts.Profile:
    from .people_api import _may_manage
    prof = accounts.get_profile(profile_id)
    if prof is None:
        raise fail(404, "profile_not_found", "No such profile.")
    me = caller(request)
    if prof.id != me.profile_id and not _may_manage(me, prof.protected):
        raise fail(403, "permission_denied", "You cannot manage that profile.")
    return prof


def export_user(prof: accounts.Profile) -> dict:
    with accounts.connect() as con:
        activity = [dict(r) for r in con.execute(
            "SELECT ts, category, kind, subject, detail FROM activity WHERE profile_id=? ORDER BY id", (prof.id,))]
        about = [dict(r) for r in con.execute(
            "SELECT ts, kind, profile_id AS by_profile, detail FROM activity WHERE category='audit' AND subject=? "
            "ORDER BY id", (prof.id,))]
        usage = [dict(r) for r in con.execute(
            "SELECT day, kind, amount FROM usage_daily WHERE subject=? ORDER BY day, kind", (prof.id,))]
        devices = [dict(r) for r in con.execute(
            "SELECT id, label, bound_at, created, last_seen FROM devices WHERE default_profile_id=?", (prof.id,))]
    sessions = accounts.list_sessions(prof.id)
    # 4.0: the profile's own bots (setup only - ids); which household bots it may use is
    # ``profile.household_bots``.
    chars = [b.id for b in config.load_bots()
             if b.scope == "personal" and b.owner_profile == prof.id]
    return {
        "schema": 1, "exported_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "profile": prof.public(), "devices": devices, "sessions": sessions, "usage": usage,
        "activity": activity, "audit_entries_about_this_profile": about,
        "own_characters": chars,
        "conversations": ("Chats belong to the profile that started them (4.0) and are not part of this "
                          "account export. Each person takes their own with the History export (Settings > "
                          "Account > Recovery & Export), signed in as themselves."),
    }


@router.get("/api/users/{profile_id}/export", dependencies=[_USERS])
async def export_user_route(request: Request, profile_id: str):
    prof = _target(request, profile_id)
    doc = await asyncio.to_thread(export_user, prof)
    audit(request, "user.export", prof.id)
    body = json.dumps(doc, indent=1, ensure_ascii=False, default=str)
    return Response(body, media_type="application/json", headers={
        "Content-Disposition": f'attachment; filename="dispatch-profile-{prof.id[:8]}.json"', "Cache-Control": "no-store"})


@router.delete("/api/users/{profile_id}", dependencies=[_USERS])
async def erase_user(request: Request, profile_id: str):
    prof = _target(request, profile_id)
    me = caller(request)
    if prof.id == me.profile_id:
        raise fail(409, "permission_denied", "You cannot delete the profile you are using.")
    if prof.protected:
        raise fail(409, "owner_immutable", "The owner cannot be deleted.")

    def run() -> dict:
        auth.revoke_profile(prof.id)
        with accounts.connect() as con:
            n_act = con.execute("DELETE FROM activity WHERE profile_id=? AND category='activity'", (prof.id,)).rowcount
            n_use = con.execute("DELETE FROM usage_daily WHERE subject=?", (prof.id,)).rowcount
        for b in config.load_bots():
            if prof.id in b.profiles:
                config.update_bot(b.id, lambda x: _without_profile(x, prof.id))
        accounts.delete_profile(prof.id)
        return {"activity_rows": n_act, "usage_rows": n_use}

    out = await asyncio.to_thread(run)
    audit(request, "user.erase", prof.id, f"{out['activity_rows']} activity, {out['usage_rows']} usage rows")
    return {"erased": prof.id, **out}


def _without_profile(b, profile_id: str):
    """``b`` with ``profile_id`` taken off its profiles list. An EMPTY list means "everyone", so a
    character that was made for this profile alone must not become visible to the whole household
    (and its chats readable) because that profile was erased: it is left to the Owner instead."""
    from dataclasses import replace
    rest = [p for p in b.profiles if p != profile_id]
    if not rest and b.profiles:
        owner = accounts.get_owner()
        rest = [owner.id] if owner is not None else [profile_id]
    return replace(b, profiles=rest)

