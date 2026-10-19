"""Sign-in routes beyond the PIN, and the People & Profiles API.

* ``/api/auth/login`` and ``/api/auth/switch`` - sign in as a profile (Owner PIN or
  password, Admin password, Member/Restricted PIN) or, for a Restricted profile with
  no PIN, simply pick it. Throttled per (device, profile), per address, and globally.
* ``/api/auth/me``, ``/api/auth/password``, ``/api/auth/api-token`` - who am I, change
  my password, the machine API key.
* ``/api/people`` - list / create / change / delete profiles, set PINs and passwords,
  sign a profile out, bind this device to a default profile, the activity summary and
  the admin audit log. Declared ``manage_users`` (or ``view_activity``) in
  ``route_policy.py``; the rank rules (an Admin manages Members and Restricted, only
  the Owner manages Admins) are enforced here.

Every mutation writes an ``audit`` row (who, what, to whom) - the baseline's "audit log
of admin actions".
"""

from __future__ import annotations

import contextlib
import logging
import re
import secrets
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from . import accounts, auth, config, credentials, limits, policy
from .contracts import errors
from .contracts.permissions import (
    MACHINE_DEFAULT_SCOPES,
    RESTRICTED_NEVER,
    ROLE_ORDER,
    ROLE_PERMISSIONS,
    Permission,
    Role,
    is_valid_permission,
)

log = logging.getLogger("local-chat.people")

router = APIRouter()

# The gate has already enforced each route's declared requirement (route_policy.py);
# these state the same sentence again at the handler (defence in depth).
_MANAGE = Depends(policy.require_permission(Permission.MANAGE_USERS))
_VIEW = Depends(policy.require_permission(Permission.VIEW_ACTIVITY))
_CHAT = Depends(policy.require_permission(Permission.USE_CHAT))
_SETTINGS = Depends(policy.require_permission(Permission.MANAGE_SETTINGS))

SESSION_COOKIE = policy.SESSION_COOKIE
DEVICE_COOKIE = policy.DEVICE_COOKIE
_DEVICE_MAX_AGE = 2 * 365 * 86400

#: The one sentence the Restricted UI shows so a child (and a parent) know the
#: activity summary exists. The UI renders its own localised copy of it.
DISCLOSURE = "The owner can see a summary of what happens in this profile (how many chats and uploads, not what was said)."


# --------------------------------------------------------------------------- #
# Plumbing
# --------------------------------------------------------------------------- #


def _problem(status: int, code: str, message: str, **extra: Any) -> JSONResponse:
    body = {"detail": message, "error": errors.envelope(code, message=message), **extra}
    return JSONResponse(body, status_code=status)


def _fail(status: int, code: str, message: str) -> HTTPException:
    exc = HTTPException(status, message)
    exc.code = code                      # type: ignore[attr-defined]
    return exc


def _account_error(e: accounts.AccountError) -> JSONResponse:
    return _problem(e.status, e.code, str(e))


def _caller(request: Request) -> policy.Principal:
    return policy.of(request).principal


def _device_token(request: Request) -> str | None:
    tok = request.cookies.get(DEVICE_COOKIE)
    return tok if tok and 16 <= len(tok) <= 128 else None


def device_hash_of(request: Request) -> str | None:
    tok = _device_token(request)
    return auth.device_hash(tok) if tok else None


def _ua(request: Request) -> str:
    return request.headers.get("user-agent") or ""


def _set_cookie(resp: JSONResponse, request: Request, name: str, value: str, max_age: int | None) -> None:
    resp.set_cookie(name, value, httponly=True, samesite="strict", path="/", max_age=max_age,
                    secure=policy.request_is_https(request))


def device_for(request: Request) -> tuple[str, str | None]:
    """(device id, new cookie token or None) for this browser, registering it."""
    tok = _device_token(request)
    new_tok = None
    if tok is None:
        tok = new_tok = secrets.token_urlsafe(24)
    dev = auth.device_hash(tok)
    with contextlib.suppress(Exception):
        accounts.ensure_device(dev, _ua(request))
    return dev, new_tok


def public_profile(p: accounts.Profile | None) -> dict | None:
    if p is None:
        return None
    return {"id": p.id, "name": p.name, "role": p.role, "avatar": p.avatar}


def keep_device_for_restricted(device_id: str | None, profile: accounts.Profile | None) -> bool:
    """A device a Restricted profile signs in on stays that profile's device.

    "Anonymous is the same as a locked session" (the owner, 2026-10-19): locking the
    screen, an idle timeout or a dropped session cookie must not turn a child's device
    into a guest with its own (per-address) budgets and no quiet hours. So the first
    Restricted sign-in on a device nobody bound binds it to that profile - from then on
    the locked device opens as that profile, under its quiet hours, minutes and daily
    budgets (People & Profiles > This device shows it; an Owner or Admin can unbind or
    re-bind it). A device already bound to someone else is left alone. True if bound."""
    if not device_id or profile is None or profile.role != Role.RESTRICTED.value or profile.disabled:
        return False
    if accounts.default_profile_for_device(device_id) is not None:
        return False
    with contextlib.suppress(accounts.AccountError):
        accounts.bind_device(device_id, profile.id)
        accounts.log_activity("device_bound", profile_id=profile.id, subject=device_id[:12],
                              detail="restricted sign-in keeps the device", category="audit")
        return True
    return False


def session_response(request: Request, body: dict, profile: accounts.Profile | None, *,
                     persistent: bool = False) -> JSONResponse:
    """Issue a session for ``profile`` and attach it (and the device cookie) to a reply."""
    # A device cookie is minted only when it earns its keep: the browser asked to be
    # remembered (or already has one). A plain sign-in leaves the response with the
    # session cookie alone.
    have = _device_token(request)
    restricted = profile is not None and profile.role == Role.RESTRICTED.value
    if have or persistent or restricted:
        dev, new_dev_tok = device_for(request)
    else:
        dev, new_dev_tok = None, None
    if restricted:
        keep_device_for_restricted(dev, profile)
    token = auth.issue_session(persistent=persistent, user_agent=_ua(request),
                               profile_id=profile.id if profile else None, device_id=dev)
    sess = auth.get_session(token)
    remembered = bool(sess and sess.persistent)
    body["remembered"] = remembered
    resp = JSONResponse(body)
    max_age = auth.remember_days() * 86400 if remembered else None
    _set_cookie(resp, request, SESSION_COOKIE, token, max_age)
    if new_dev_tok:
        _set_cookie(resp, request, DEVICE_COOKIE, new_dev_tok, _DEVICE_MAX_AGE)
    return resp


def _client_keys(request: Request) -> tuple[str, str]:
    """(device key, address). A browser with no device cookie is keyed by its address,
    so one noisy deviceless client can never lock the others out."""
    ip = policy.request_quota_ip(request)
    return device_hash_of(request) or f"ip:{ip}", ip


def _throttled(request: Request, profile_key: str) -> JSONResponse | None:
    dev, ip = _client_keys(request)
    wait = auth.throttle_wait(dev, profile_key, ip)
    if wait > 0:
        secs = int(wait) + 1
        return JSONResponse(
            {"detail": f"Too many attempts — wait {secs}s.", "retry_after": secs,
             "error": errors.envelope("too_many_attempts", message=f"Too many attempts — wait {secs}s.")},
            status_code=429, headers={"Retry-After": str(secs)})
    return None


def _failed(request: Request, profile_key: str, who: str = "") -> JSONResponse:
    dev, ip = _client_keys(request)
    auth.register_failure(dev, profile_key, ip)
    accounts.log_activity("login_failed", subject=f"{ip}", detail=who[:60], category="audit")
    return _problem(401, "incorrect_credential", "Incorrect PIN or password")


def _succeeded(request: Request, profile_key: str) -> None:
    dev, ip = _client_keys(request)
    auth.register_success(dev, profile_key, ip)


# --------------------------------------------------------------------------- #
# Sign-in
# --------------------------------------------------------------------------- #


class LoginIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile_id: str | None = Field(default=None, max_length=64)
    name: str | None = Field(default=None, max_length=64)
    pin: str | None = Field(default=None, max_length=128)
    password: str | None = Field(default=None, max_length=256)
    remember: bool = False


def _find_profile(payload: LoginIn) -> accounts.Profile | None:
    if payload.profile_id:
        return accounts.get_profile(payload.profile_id)
    if payload.name:
        return accounts.get_by_name(payload.name)
    return None


def _verify_for_login(profile: accounts.Profile, payload: LoginIn, cfg) -> bool:
    """Check the credential the profile's role asks for."""
    role = profile.role
    if role == Role.OWNER.value:
        if payload.password:
            return auth.verify_profile_secret(profile, "password", payload.password)
        if payload.pin:
            # The install PIN (security.yaml) is the owner's PIN.
            return cfg.pin_set and auth.verify_pin(payload.pin)
        return False
    if role == Role.ADMIN.value:
        return bool(payload.password) and auth.verify_profile_secret(profile, "password", payload.password)
    # Member / Restricted: a PIN; a Restricted profile without one may simply be picked.
    if not profile.has_pin:
        return role == Role.RESTRICTED.value and not payload.pin and not payload.password
    return bool(payload.pin) and auth.verify_profile_secret(profile, "pin", payload.pin)


def login_profile(request: Request, payload: LoginIn, *, swap: bool) -> JSONResponse:
    cfg = auth.load()
    if not policy.claimed(cfg):
        return _problem(400, "pin_not_set", "No PIN is set")
    profile = _find_profile(payload)
    pkey = profile.id if profile else f"?{(payload.name or payload.profile_id or '')[:40].casefold()}"
    blocked = _throttled(request, pkey)
    if blocked is not None:
        return blocked
    if profile is None or profile.disabled:
        return _failed(request, pkey, "unknown profile")
    if not _verify_for_login(profile, payload, cfg):
        return _failed(request, pkey, profile.name)
    _succeeded(request, pkey)
    if swap:
        old = request.cookies.get(SESSION_COOKIE)
        if old:
            auth.revoke(old)
    persistent = bool(payload.remember) and cfg.remember_days > 0 and profile.role != "restricted"
    body = {"ok": True, "profile": public_profile(profile), "role": profile.role,
            "needs_password": profile.role in ("owner", "admin") and not profile.has_password}
    accounts.log_activity("login", profile_id=profile.id, subject=f"{policy.request_quota_ip(request)}")
    return session_response(request, body, profile, persistent=persistent)


@router.post("/api/auth/login")
async def auth_login(request: Request, payload: LoginIn):
    return login_profile(request, payload, swap=False)


@router.post("/api/auth/switch")
async def auth_switch(request: Request, payload: LoginIn):
    """Quick profile switch. Same checks as login; the caller's current session ends."""
    return login_profile(request, payload, swap=True)


def lock_screen_profiles() -> list[dict]:
    """What the lock screen lists so a household member can pick their tile.

    ``credential`` says what to ask for: the Owner's PIN lives in security.yaml, so the
    Owner may have a PIN, a password or both; an Admin has a password; a Member a PIN; a
    Restricted profile a PIN or nothing at all."""
    pin_set = auth.load().pin_set
    out = []
    for p in accounts.list_profiles():
        if p.disabled:
            continue
        if p.role == "owner":
            cred = ("pin_or_password" if pin_set and p.has_password
                    else "password" if p.has_password else "pin")
        elif p.role == "admin":
            cred = "password"
        else:
            cred = "pin" if p.has_pin else "none"
        out.append({"id": p.id, "name": p.name, "role": p.role, "avatar": p.avatar,
                    "credential": cred})
    return out


def default_limits() -> dict:
    """The install-wide daily defaults a Restricted caller without its own numbers gets
    (the DISPATCH_DECOY_* settings), in the same keys a profile's budgets use."""
    mib = 1024 * 1024
    return {"turns": int(config.env("DECOY_TURN_QUOTA", "200")),
            "threads": int(config.env("DECOY_THREAD_QUOTA", "50")),
            "feedback": int(config.env("DECOY_FEEDBACK_QUOTA", "100")),
            "upload_mb": int(config.env("DECOY_UPLOAD_QUOTA") or 200 * mib) // mib}


def principal_document(request: Request) -> dict:
    """Body of GET /api/auth/me."""
    pr = _caller(request)
    prof = pr.profile
    cfg = auth.load()
    doc: dict[str, Any] = {
        "kind": pr.kind, "role": pr.role, "signed_in": pr.signed_in,
        "restricted": pr.is_restricted, "name": pr.name,
        "profile": public_profile(prof),
        "permissions": sorted(pr.permissions) if pr.kind != "open" else ["*"],
        "claimed": policy.claimed(cfg),
        "needs_password": bool(prof and prof.role in ("owner", "admin") and not prof.has_password),
        "disclosure": DISCLOSURE if pr.is_restricted and pr.kind != "machine" else None,
    }
    dev = pr.device_id or device_hash_of(request)
    if dev:
        d = accounts.get_device(dev)
        doc["device"] = d.public() if d else None
    if prof is not None and prof.role == "restricted":
        doc["today"] = accounts.usage_today(prof.id)
        doc["limits"] = {**default_limits(), **prof.budgets}
    elif pr.kind == "anonymous":
        # A guest is a Restricted caller with the default budgets, charged to its address.
        doc["today"] = accounts.usage_today(policy.request_quota_ip(request))
        doc["limits"] = default_limits()
    return doc


@router.get("/api/auth/me")
async def auth_me(request: Request):
    return principal_document(request)


class PasswordIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    current: str | None = Field(default=None, max_length=256)
    current_pin: str | None = Field(default=None, max_length=128)
    new: str = Field(min_length=1, max_length=256)


@router.post("/api/auth/password", dependencies=[_SETTINGS])
async def auth_password(request: Request, payload: PasswordIn):
    """Set or change YOUR password (Owner or Admin).

    The first password of a PIN-only owner (the migration leaves it unset) is
    confirmed with the install PIN; after that, with the current password."""
    pr = _caller(request)
    prof = pr.profile
    if prof is None:
        return _problem(409, "owner_required", "Create the owner account first.")
    if prof.role not in ("owner", "admin"):
        return _problem(403, "password_role", "Only the Owner and Admins have a password; "
                        "Members and Restricted profiles use a PIN.")
    pkey = prof.id
    blocked = _throttled(request, pkey)
    if blocked is not None:
        return blocked
    if prof.has_password:
        ok = bool(payload.current) and auth.verify_profile_secret(prof, "password", payload.current)
    else:
        cfg = auth.load()
        ok = (not cfg.pin_set) or (bool(payload.current_pin) and auth.verify_pin(payload.current_pin))
    if not ok:
        return _failed(request, pkey, prof.name)
    _succeeded(request, pkey)
    try:
        auth.set_profile_password(prof.id, payload.new)
    except accounts.AccountError as e:
        return _account_error(e)
    accounts.log_activity("password_changed", profile_id=prof.id, category="audit")
    # set_profile_password signs the profile out everywhere; keep THIS device in.
    fresh = accounts.get_profile(prof.id)
    return session_response(request, {"ok": True}, fresh, persistent=False)


class MachineIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rotate: bool = False
    machine_loopback_trust: bool | None = None
    machine_scopes: list[str] | None = None


@router.get("/api/auth/api-token", dependencies=[_SETTINGS])
async def api_token_get(request: Request):
    if not _caller(request).is_operator:
        return _problem(403, "permission_denied", "Only the Owner and Admins manage the API key.")
    cfg = auth.load()
    return {"has_token": bool(cfg.api_token), "machine_loopback_trust": cfg.machine_loopback_trust,
            "machine_scopes": cfg.machine_scopes if cfg.machine_scopes is not None
            else sorted(s.value for s in MACHINE_DEFAULT_SCOPES),
            "all_scopes": sorted(s.value for s in MACHINE_DEFAULT_SCOPES)}


@router.post("/api/auth/api-token", dependencies=[_SETTINGS])
async def api_token_set(request: Request, payload: MachineIn):
    """Rotate the machine API key (shown once) and/or change how machines are
    recognised. The Owner and Admins."""
    if not _caller(request).is_operator:
        return _problem(403, "permission_denied", "Only the Owner and Admins manage the API key.")
    cfg = auth.load()
    token = None
    with auth._lock:
        if payload.rotate:
            token = secrets.token_urlsafe(32)
            cfg.api_token = token
        if payload.machine_loopback_trust is not None:
            cfg.machine_loopback_trust = payload.machine_loopback_trust
        if payload.machine_scopes is not None:
            valid = {s.value for s in MACHINE_DEFAULT_SCOPES}
            bad = [s for s in payload.machine_scopes if s not in valid]
            if bad:
                return _problem(422, "validation_error", f"Unknown machine scope(s): {', '.join(bad)}")
            cfg.machine_scopes = list(payload.machine_scopes)
        auth._write(cfg)
        auth._bust_cache()
    accounts.log_activity("api_token_changed", profile_id=_caller(request).profile_id,
                          detail="rotated" if payload.rotate else "settings", category="audit")
    out: dict[str, Any] = {"ok": True, "machine_loopback_trust": cfg.machine_loopback_trust}
    if token:
        out["api_token"] = token            # the only time it is ever returned
    return out


# --------------------------------------------------------------------------- #
# People & profiles
# --------------------------------------------------------------------------- #

_BUDGET_INT_KEYS = {"minutes_per_day": (1, 1440), "turns": (0, 100000), "threads": (0, 100000),
                    "feedback": (0, 100000), "upload_mb": (0, 100000)}
_QUIET_RE = re.compile(r"^\d{1,2}:\d{2}-\d{1,2}:\d{2}$")


def clean_budgets(raw: Any) -> dict:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise accounts.AccountError("budgets_invalid", "budgets must be an object.")
    out: dict[str, Any] = {}
    for k, v in raw.items():
        if k in _BUDGET_INT_KEYS:
            lo, hi = _BUDGET_INT_KEYS[k]
            if v is None or v == "":
                continue
            if isinstance(v, bool) or not isinstance(v, int) or not (lo <= v <= hi):
                raise accounts.AccountError("budgets_invalid", f"{k} must be a whole number {lo}..{hi}.")
            out[k] = v
        elif k == "quiet_hours":
            if v in (None, ""):
                continue
            if not isinstance(v, str) or not _QUIET_RE.match(v) or limits.parse_window(v) is None:
                raise accounts.AccountError("budgets_invalid", "quiet_hours must look like 20:30-07:00.")
            out[k] = v
        else:
            raise accounts.AccountError("budgets_invalid", f"Unknown budget {k!r}.")
    return out


def clean_permissions(role: str, items: Any, field_name: str) -> list[str]:
    if items is None:
        return []
    if not isinstance(items, list):
        raise accounts.AccountError("permissions_invalid", f"{field_name} must be a list.")
    out: list[str] = []
    never = {p.value for p in RESTRICTED_NEVER}
    for it in items:
        if not isinstance(it, str) or not is_valid_permission(it):
            raise accounts.AccountError("permissions_invalid", f"Unknown permission {it!r}.")
        if field_name == "grants" and role == "restricted" and it in never:
            raise accounts.AccountError(
                "permissions_invalid", f"A Restricted profile can never be granted {it}.")
        if field_name == "grants" and it == "admin_system" and role not in ("owner", "admin"):
            raise accounts.AccountError("permissions_invalid",
                                        "Host control belongs to the Owner and Admins; it cannot be granted.")
        out.append(it)
    return list(dict.fromkeys(out))


class PersonIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=40)
    role: Literal["admin", "member", "restricted"] = "member"
    pin: str | None = Field(default=None, max_length=128)
    password: str | None = Field(default=None, max_length=256)
    avatar: str = Field(default="", max_length=120)
    budgets: dict | None = None
    grants: list[str] | None = None
    denies: list[str] | None = None
    session_timeout: int | None = Field(default=None, ge=60, le=30 * 86400)


class PersonPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=40)
    role: Literal["admin", "member", "restricted"] | None = None
    avatar: str | None = Field(default=None, max_length=120)
    budgets: dict | None = None
    grants: list[str] | None = None
    denies: list[str] | None = None
    session_timeout: int | None = Field(default=None, ge=60, le=30 * 86400)
    disabled: bool | None = None


class SecretIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pin: str = Field(default="", max_length=128)
    password: str = Field(default="", max_length=256)


def _may_manage(caller: policy.Principal, target_role: str) -> bool:
    """The Owner and every Admin manage every profile but THE Owner's.

    That one exception is structural, not a permission: an Admin who could delete,
    disable, demote or re-credential the Owner could lock the household's owner out of
    their own install (and there is exactly one owner row). Everything else an Owner
    may do to a profile - other Admins included - an Admin may do too."""
    if caller.is_operator:
        return target_role != Role.OWNER.value
    return False


def _audit(request: Request, kind: str, target: str, detail: str = "") -> None:
    accounts.log_activity(kind, profile_id=_caller(request).profile_id, subject=target,
                          detail=detail, category="audit")


def _owner_has_password() -> bool:
    o = accounts.get_owner()
    return bool(o and o.has_password)


@router.get("/api/people", dependencies=[_MANAGE])
async def people_list(request: Request):
    caller = _caller(request)
    return {
        "profiles": [p.public() for p in accounts.list_profiles()],
        "me": caller.profile_id,
        "roles": [r.value for r in ROLE_ORDER],
        "role_permissions": {r.value: sorted(p.value for p in ROLE_PERMISSIONS[r]) for r in ROLE_ORDER},
        "can_create_admin": caller.is_operator,
        "owner_has_password": _owner_has_password(),
        "disclosure": DISCLOSURE,
    }


@router.post("/api/people", status_code=201, dependencies=[_MANAGE])
async def people_create(request: Request, payload: PersonIn):
    caller = _caller(request)
    if not accounts.has_owner():
        auth.ensure_owner_for_pin()
    if not accounts.has_owner():
        return _problem(409, "owner_required", "Create the owner account first (Settings > Security).")
    if not _may_manage(caller, payload.role):
        return _problem(403, "permission_denied", "Only the Owner and Admins can create Admins.")
    try:
        if payload.role == "admin":
            if not _owner_has_password():
                return _problem(409, "password_required", "Set a password for the owner first.")
            err = credentials.check_password(payload.password or "")
            if err:
                return _problem(400, "password_invalid", err)
        pin_hash = None
        if payload.pin:
            err = credentials.check_pin(payload.pin)
            if err:
                return _problem(400, "pin_invalid", err)
            pin_hash = credentials.hash_secret(payload.pin)
        elif payload.role == "member":
            return _problem(400, "pin_invalid", "A Member needs a PIN of at least 4 characters.")
        prof = accounts.create_profile(
            payload.name, payload.role, pin_hash=pin_hash,
            password_hash=credentials.hash_secret(payload.password) if payload.role == "admin" else None,
            avatar=payload.avatar, budgets=clean_budgets(payload.budgets),
            grants=clean_permissions(payload.role, payload.grants, "grants"),
            denies=clean_permissions(payload.role, payload.denies, "denies"),
            session_timeout=payload.session_timeout)
    except accounts.AccountError as e:
        return _account_error(e)
    _audit(request, "profile_created", prof.id, f"{prof.name} ({prof.role})")
    return JSONResponse(prof.public(), status_code=201)


def _target(request: Request, profile_id: str) -> tuple[accounts.Profile | None, JSONResponse | None]:
    prof = accounts.get_profile(profile_id)
    if prof is None:
        return None, _problem(404, "profile_not_found", "No such profile.")
    caller = _caller(request)
    if prof.id == caller.profile_id or _may_manage(caller, prof.role):
        return prof, None
    return None, _problem(403, "permission_denied", "You cannot manage that profile.")


@router.patch("/api/people/{profile_id}", dependencies=[_MANAGE])
async def people_patch(request: Request, profile_id: str, payload: PersonPatch):
    prof, err = _target(request, profile_id)
    if err is not None:
        return err
    assert prof is not None
    caller = _caller(request)
    fields = payload.model_dump(exclude_unset=True)
    own = prof.id == caller.profile_id
    try:
        if own and any(k in fields for k in ("role", "disabled")):
            return _problem(409, "owner_immutable" if prof.role == "owner" else "permission_denied",
                            "You cannot change your own role or disable yourself.")
        if prof.role == "owner" and set(fields) - {"name", "avatar"}:
            return _problem(409, "owner_immutable", "The owner's role and limits cannot be changed here.")
        if own and set(fields) - {"name", "avatar"} and not _may_manage(caller, prof.role):
            # A profile edits its own name and picture, nothing else: grants, denies, budgets
            # and timeouts are set by someone who may manage it. Without this a Member holding
            # manage_users could grant itself every permission (host control included).
            return _problem(403, "permission_denied",
                            "You can change your own name and picture; limits and permissions are "
                            "set by the Owner or an Admin.")
        new_role = fields.get("role", prof.role)
        if "role" in fields and new_role != prof.role:
            if not _may_manage(caller, new_role):
                return _problem(403, "permission_denied", "Only the Owner and Admins can create Admins.")
            if new_role == "admin" and not prof.has_password:
                return _problem(409, "password_required",
                                "Set the new Admin's password first (People > the profile > Password).")
            if new_role == "member" and not prof.has_pin:
                return _problem(409, "pin_invalid", "A Member needs a PIN; set one first.")
        if "budgets" in fields:
            fields["budgets"] = clean_budgets(fields["budgets"])
        if "grants" in fields:
            fields["grants"] = clean_permissions(new_role, fields["grants"], "grants")
        if "denies" in fields:
            fields["denies"] = clean_permissions(new_role, fields["denies"], "denies")
        updated = accounts.update_profile(profile_id, **fields)
    except accounts.AccountError as e:
        return _account_error(e)
    if {"role", "disabled"} & set(fields):
        auth.revoke_profile(profile_id)
    _audit(request, "profile_updated", profile_id, ",".join(sorted(fields)))
    return updated.public()


@router.delete("/api/people/{profile_id}", dependencies=[_MANAGE])
async def people_delete(request: Request, profile_id: str):
    prof, err = _target(request, profile_id)
    if err is not None:
        return err
    assert prof is not None
    if prof.id == _caller(request).profile_id:
        return _problem(409, "permission_denied", "You cannot delete the profile you are using.")
    auth.revoke_profile(profile_id)
    try:
        accounts.delete_profile(profile_id)
    except accounts.AccountError as e:
        return _account_error(e)
    _audit(request, "profile_deleted", profile_id, prof.name)
    return {"ok": True}


@router.post("/api/people/{profile_id}/pin", dependencies=[_MANAGE])
async def people_set_pin(request: Request, profile_id: str, payload: SecretIn):
    prof, err = _target(request, profile_id)
    if err is not None:
        return err
    assert prof is not None
    if prof.role == "owner":
        return _problem(409, "owner_immutable", "Change the Owner's PIN in Settings > Security.")
    try:
        auth.set_profile_pin(profile_id, payload.pin)
    except accounts.AccountError as e:
        return _account_error(e)
    _audit(request, "pin_set", profile_id, prof.name)
    return {"ok": True}


@router.delete("/api/people/{profile_id}/pin", dependencies=[_MANAGE])
async def people_clear_pin(request: Request, profile_id: str):
    prof, err = _target(request, profile_id)
    if err is not None:
        return err
    assert prof is not None
    if prof.role == "member":
        return _problem(409, "pin_invalid", "A Member needs a PIN; set a new one instead.")
    try:
        auth.clear_profile_pin(profile_id)
    except accounts.AccountError as e:
        return _account_error(e)
    _audit(request, "pin_cleared", profile_id, prof.name)
    return {"ok": True}


@router.post("/api/people/{profile_id}/password", dependencies=[_MANAGE])
async def people_set_password(request: Request, profile_id: str, payload: SecretIn):
    """Set another Admin's password (the Owner or an Admin). Your own is /api/auth/password;
    the Owner's is the Owner's alone."""
    caller = _caller(request)
    prof, err = _target(request, profile_id)
    if err is not None:
        return err
    assert prof is not None
    if prof.id == caller.profile_id or prof.role == "owner" or not caller.is_operator:
        return _problem(409, "permission_denied", "Use Settings > Security to change your own password.")
    try:
        auth.set_profile_password(profile_id, payload.password)
    except accounts.AccountError as e:
        return _account_error(e)
    _audit(request, "password_set", profile_id, prof.name)
    return {"ok": True}


@router.post("/api/people/{profile_id}/sign-out", dependencies=[_MANAGE])
async def people_sign_out(request: Request, profile_id: str):
    prof, err = _target(request, profile_id)
    if err is not None:
        return err
    assert prof is not None
    n = auth.revoke_profile(profile_id)
    _audit(request, "signed_out", profile_id, f"{n} sessions")
    return {"ok": True, "sessions": n}


class DeviceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile_id: str | None = None
    label: str | None = Field(default=None, max_length=60)


@router.get("/api/people/devices", dependencies=[_MANAGE])
async def people_devices(request: Request):
    here = device_hash_of(request)
    return {"devices": [d.public() | {"this_device": d.id == here} for d in accounts.list_devices()],
            "this_device": here}


@router.put("/api/people/devices/this", dependencies=[_MANAGE])
async def people_bind_this_device(request: Request, payload: DeviceIn):
    """Bind THIS browser to a default profile (``null`` unbinds): a device bound to a
    Restricted profile opens as that profile with nobody signed in."""
    caller = _caller(request)
    if payload.profile_id:
        target = accounts.get_profile(payload.profile_id)
        # Binding opens the device as that profile with no credential: only someone who
        # may manage the profile (or the profile itself) may do it.
        if target is not None and target.id != caller.profile_id and not _may_manage(caller, target.role):
            return _problem(403, "permission_denied", "You cannot bind a device to that profile.")
    dev, new_tok = device_for(request)
    try:
        d = accounts.bind_device(dev, payload.profile_id, payload.label)
    except accounts.AccountError as e:
        return _account_error(e)
    _audit(request, "device_bound", dev[:12], payload.profile_id or "unbound")
    resp = JSONResponse(d.public())
    if new_tok:
        _set_cookie(resp, request, DEVICE_COOKIE, new_tok, _DEVICE_MAX_AGE)
    return resp


@router.delete("/api/people/devices/{device_id}", dependencies=[_MANAGE])
async def people_forget_device(request: Request, device_id: str):
    caller = _caller(request)
    if not caller.is_operator and device_id != device_hash_of(request):
        return _problem(403, "permission_denied", "You can only forget this device.")
    accounts.delete_device(device_id)
    _audit(request, "device_removed", device_id[:12])
    return {"ok": True}


@router.get("/api/people/activity", dependencies=[_VIEW])
async def people_activity(request: Request, profile_id: str | None = None, days: int = 7):
    days = max(1, min(days, 90))
    return {"summary": accounts.activity_summary(profile_id, days),
            "recent": accounts.recent_activity(profile_id, limit=100),
            "today": accounts.usage_today(profile_id) if profile_id else {}}


@router.get("/api/people/audit", dependencies=[_VIEW])
async def people_audit(request: Request):
    return {"entries": accounts.recent_activity(None, category="audit", limit=200)}


@router.get("/api/me/activity", dependencies=[_CHAT])
async def my_activity(request: Request, days: int = 7):
    """A profile's own summary - the disclosure the Restricted UI links to."""
    pr = _caller(request)
    pid = pr.profile_id
    return {"disclosure": DISCLOSURE, "visible_to_owner": True,
            "summary": accounts.activity_summary(pid, max(1, min(days, 30))) if pid else {"days": []},
            "today": accounts.usage_today(pid) if pid else {},
            "limits": {**default_limits(), **(pr.profile.budgets if pr.profile else {})}}
