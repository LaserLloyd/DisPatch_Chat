"""Backups, restore, the support bundle, and per-user data export / erase.

* ``GET    /api/backups``                  every backup (archives, rotating snapshots, pre-migration copies)
                                           plus the schedule state            (admin_system)
* ``POST   /api/backups``                  write an encrypted archive now      (admin_system)
* ``POST   /api/backups/{id}/verify``      decrypt and check an archive        (admin_system)
* ``POST   /api/backups/{id}/restore``     stage a restore; applied at the next start (admin_system)
* ``GET    /api/backups/pending`` / ``DELETE /api/backups/pending``  see / cancel a staged restore
* ``DELETE /api/backups/{id}``             delete an archive or snapshot       (admin_system)
* ``POST   /api/backups/recovery-key/reveal``  show the GENERATED recovery key again, after the
                                           PIN or password is typed again      (the protected Admin only)
* ``POST   /api/support-bundle``           a redacted zip for a bug report     (admin_system)
* ``GET    /api/users/{id}/export``        everything DisPatch holds about a profile (manage_users)
* ``DELETE /api/users/{id}``               erase a profile: the same removal as ``POST /api/people/{id}/delete``
                                           (its bots, chats, files, pictures and voices go too) (manage_users)

Ids look like ``archive:dispatch-20261018-120000.dpbk``, ``snapshot:chats-....db``,
``premigration:3.0.0-....`` or ``prerestore:pre-restore-....``. A passphrase typed here is never stored; the one in Settings is read
from the secret store.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from . import accounts, auth, auth_attempts, backups, config, policy, settings_store
from .api_common import audit, caller, fail
from .contracts import errors
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


class RevealIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pin: str | None = Field(default=None, max_length=128, repr=False)
    password: str | None = Field(default=None, max_length=256, repr=False)


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
        # 4.3: is there a generated recovery key the protected Admin may show again (never the key)
        "generated_key": backups.generated_key() is not None,
        # 4.3 (#35): the last applied restore and the secrets it could not put back
        "last_restore": backups.last_restore(),
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
        backups.store_generated_key(pw)
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
    if kind in ("premigration", "prerestore"):
        raise fail(409, "backup_protected", "Pre-migration and pre-restore copies are kept until they age out; "
                   "delete them from the backups folder by hand if you must.")
    await asyncio.to_thread(path.unlink)
    audit(request, "backup.delete", backup_id[:80])
    return {"deleted": backup_id}


@router.post("/api/backups/recovery-key/reveal", dependencies=[_SYSTEM])
async def reveal_recovery_key(request: Request, payload: RevealIn):
    """Show the recovery key DisPatch generated (and keeps as the backup passphrase) once more.

    Only the protected Admin, signed in as a profile (route policy ``host=True``; checked here as
    well so it holds before the host tier is enforced), and only after typing the PIN or the
    password again: verified through ``auth_attempts.guarded_verify`` (refused before verifying
    once the throttle trips). Audited either way; the answer is never cached. A passphrase a person
    chose is never shown: 404 ``no_generated_key``."""
    me = caller(request)
    prof = me.profile
    if me.kind != "profile" or not me.is_protected or prof is None:
        raise fail(403, "host_owner_only", "Only the Owner can show the recovery key.")
    if not payload.pin and not payload.password:
        raise fail(422, "bad_request", "Type your PIN or your password again to show the recovery key.")
    cfg = auth.load()

    def verify() -> bool:
        if payload.password:
            return bool(prof.has_password and auth.verify_profile_secret(prof, "password", payload.password))
        return bool(cfg.pin_set and auth.verify_pin(payload.pin or ""))

    try:
        ok = auth_attempts.guarded_verify(request, scope="reveal", subject=prof.id, verify=verify)
    except auth_attempts.AttemptRefused as e:
        audit(request, "backup.reveal_key", prof.id, "refused: too many attempts")
        secs = max(1, int(e.retry_after))
        msg = f"Too many tries. Wait {secs} seconds, then try again."
        return JSONResponse({"detail": msg, "retry_after": secs,
                             "error": errors.envelope(e.code, message=msg)},
                            status_code=e.status, headers={"Retry-After": str(secs), "Cache-Control": "no-store"})
    if not ok:
        audit(request, "backup.reveal_key", prof.id, "wrong credential")
        raise fail(401, "incorrect_credential", "Incorrect PIN or password")
    key = await asyncio.to_thread(backups.generated_key)
    if not key:
        audit(request, "backup.reveal_key", prof.id, "no generated key")
        raise fail(404, "no_generated_key",
                   "This backup uses a passphrase you chose, so there is no generated key to show.")
    audit(request, "backup.reveal_key", prof.id, "shown")
    return JSONResponse({"recovery_key": key, "recovery_key_note": (
        "Write it down and keep it OFF this computer: only a copy kept elsewhere opens the backups "
        "if this computer is lost.")}, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})


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
    """Erase a profile. One removal path: this is ``POST /api/people/{id}/delete`` (the same
    ``profiles_api.people_delete``, so the same rank rules, the same purge of the profile's bots,
    chats, files, pictures, voices and push subscriptions, the same audit line) with the
    confirmation token minted here, because this route is the older one-call form. The protected
    Admin cannot be deleted and nobody deletes the profile they are using."""
    from . import profiles_api, profiles_service
    prof = profiles_api._target(request, profile_id, allow_self_protected=True)
    me = caller(request)
    if prof.id == me.profile_id:
        raise fail(409, "permission_denied", "You cannot delete the profile you are using.")
    try:
        preview = await asyncio.to_thread(profiles_service.delete_preview, profile_id)
    except profiles_service.ProfileServiceError as e:
        raise profiles_api._err(e) from e
    out = await profiles_api.people_delete(request, profile_id,
                                           profiles_api.DeleteIn(confirm_token=preview["token"]))
    return {"erased": profile_id, "deleted": out["deleted"]}
