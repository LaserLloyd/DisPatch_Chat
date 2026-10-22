"""Sign-in routes beyond the PIN, and the People & Profiles API.

* ``/api/auth/login`` and ``/api/auth/switch`` - sign in as a profile with its PIN or
  password, or - credentials are optional on every profile (4.0) - simply pick a profile
  that has neither. Throttled per (device, profile), per address, and globally.
* ``/api/auth/me``, ``/api/auth/password``, ``/api/auth/pin``, ``/api/auth/api-token`` - who
  am I, change my password or my own PIN, the machine API key.
* ``/api/people`` - list / create / change / delete profiles, set PINs and passwords,
  sign a profile out, list / forget devices, the activity summary and the admin audit
  log. Declared ``manage_users`` (or ``view_activity``) in ``route_policy.py``; the rank
  rules (every Admin manages every profile but the protected Admin) and the lock-target
  fence (BUILD-PLAN-v2 §1.1) are enforced here.

Every mutation writes an ``audit`` row (who, what, to whom) - the baseline's "audit log
of admin actions".
"""

from __future__ import annotations

import contextlib
import logging
import secrets
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from . import accounts, auth, config, credentials, policy
from .contracts import errors
from .contracts.permissions import (
    LOCK_TARGET_FENCE,
    MACHINE_DEFAULT_SCOPES,
    ROLE_DEFAULTS,
    ROLE_GRANTABLE,
    ROLE_NEVER,
    ROLE_ORDER,
    Permission,
    Role,
    is_valid_permission,
    normalise_role,
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

#: What a Basic profile is told (4.0): an Admin sees an activity summary (counts, never
#: content); the chats themselves are private to the profile - no other profile, Admins
#: included, opens them in the app. tests/test_people_api.py pins it against the ownership
#: rules. The UI renders its own localised copy.
DISCLOSURE = ("An Admin can see a summary of what happens in this profile (how much, not what). Chats are "
              "private to this profile: other profiles, Admins included, cannot open them in the app.")


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
    """``shared`` (4.0): this is the lock-target default profile - everyone who lands on
    it shares its characters and chats (the sign-in popup says so)."""
    if p is None:
        return None
    return {"id": p.id, "name": p.name, "role": p.role.value, "protected": p.protected,
            "avatar": p.avatar, "shared": policy.is_lock_target(p)}


# --------------------------------------------------------------------------- #
# Notices for the sign-in popup (4.0): a tamper-evident PIN / password reset
# --------------------------------------------------------------------------- #

NOTICES_FILE = "profile-notices.json"
_notice_lock = __import__("threading").Lock()


def _notices_path():
    from pathlib import Path
    return Path(config.DATA_DIR) / NOTICES_FILE


def _load_notices() -> dict:
    import json
    try:
        data = json.loads(_notices_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_notices(data: dict) -> None:
    import json
    import os
    import tempfile
    path = _notices_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, sort_keys=True)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def add_notice(profile_id: str, kind: str, *, by: str) -> None:
    """Record a notice the profile is shown once, in the popup of its next sign-in."""
    from datetime import UTC, datetime
    with _notice_lock:
        data = _load_notices()
        rows = [n for n in data.get(profile_id) or [] if isinstance(n, dict)][-9:]
        rows.append({"kind": kind, "by": by[:40],
                     "at": datetime.now(UTC).isoformat(timespec="seconds"), "session": None})
        data[profile_id] = rows
        _save_notices(data)


def _claim_notices(profile_id: str, token: str) -> None:
    """A new session of ``profile_id``: the notices nobody has seen yet become this
    session's; ones an earlier session already showed are dropped."""
    h = auth.device_hash(token)
    with _notice_lock:
        data = _load_notices()
        rows = data.get(profile_id)
        if not rows:
            return
        fresh = [{**n, "session": h} for n in rows if isinstance(n, dict) and not n.get("session")]
        if fresh:
            data[profile_id] = fresh
        else:
            data.pop(profile_id, None)
        _save_notices(data)


def notices_for(principal: policy.Principal) -> list[dict]:
    """``[{kind, by, at}]`` for the caller's CURRENT session (the sign-in popup)."""
    pid = getattr(principal, "profile_id", None)
    tok = getattr(principal, "session_token", None)
    if not pid or not tok:
        return []
    h = auth.device_hash(tok)
    rows = _load_notices().get(pid) or []
    return [{"kind": n.get("kind"), "by": n.get("by"), "at": n.get("at")}
            for n in rows if isinstance(n, dict) and n.get("session") == h]


def session_response(request: Request, body: dict, profile: accounts.Profile | None, *,
                     persistent: bool = False) -> JSONResponse:
    """Issue a session for ``profile`` and attach it (and the device cookie) to a reply."""
    # A device cookie is minted only when it earns its keep: the browser asked to be
    # remembered (or already has one). A plain sign-in leaves the response with the
    # session cookie alone.
    have = _device_token(request)
    if have or persistent:
        dev, new_dev_tok = device_for(request)
    else:
        dev, new_dev_tok = None, None
    old_token = request.cookies.get(SESSION_COOKIE) if request is not None else None
    token = auth.issue_session(persistent=persistent, user_agent=_ua(request),
                               profile_id=profile.id if profile else None, device_id=dev)
    sess = auth.get_session(token)
    remembered = bool(sess and sess.persistent)
    _rebind_push(old_token, token if remembered else None, profile)
    if profile is not None:
        with contextlib.suppress(Exception):
            _claim_notices(profile.id, token)
    body["remembered"] = remembered
    resp = JSONResponse(body)
    max_age = auth.remember_days() * 86400 if remembered else None
    _set_cookie(resp, request, SESSION_COOKIE, token, max_age)
    if new_dev_tok:
        _set_cookie(resp, request, DEVICE_COOKIE, new_dev_tok, _DEVICE_MAX_AGE)
    return resp


def _rebind_push(old_token: str | None, new_token: str | None,
                 profile: accounts.Profile | None) -> None:
    """4.0 (BUILD-PLAN-v2 §1.9): the push subscription of the session this device signed in
    over follows it to the new session as the NEW profile, or is dropped when the new
    session cannot receive pushes - so a device switched to someone else never keeps
    getting the previous profile's pings."""
    if not old_token:
        return
    from . import push
    try:
        push.rebind(auth.device_hash(old_token),
                    auth.device_hash(new_token) if new_token else None,
                    profile.id if profile is not None else None)
    except Exception:
        log.exception("push: could not re-bind a subscription on sign-in")


def _was_remembered(request: Request) -> bool:
    """Was this request's session a remembered-device one? Read before a credential
    change revokes it, so the re-issued session keeps the same persistence."""
    sess = auth.get_session(request.cookies.get(SESSION_COOKIE))
    return bool(sess and sess.persistent)


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
    """Check the profile's credential. 4.0: PINs and passwords are optional on every
    profile - one with neither may simply be picked; one with either needs one of them."""
    if profile.protected:
        if payload.password:
            return profile.has_password and auth.verify_profile_secret(profile, "password", payload.password)
        if payload.pin:
            # The install PIN (security.yaml) is the protected Admin's PIN.
            return cfg.pin_set and auth.verify_pin(payload.pin)
        return False
    if not profile.has_pin and not profile.has_password:
        return not payload.pin and not payload.password
    if payload.password:
        return profile.has_password and auth.verify_profile_secret(profile, "password", payload.password)
    if payload.pin:
        return profile.has_pin and auth.verify_profile_secret(profile, "pin", payload.pin)
    return False


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
    persistent = bool(payload.remember) and cfg.remember_days > 0
    body = {"ok": True, "profile": public_profile(profile), "role": profile.role.value,
            "protected": profile.protected,
            "needs_password": profile.role is Role.ADMIN and not profile.has_password}
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
        if p.protected:
            cred = ("pin_or_password" if pin_set and p.has_password
                    else "password" if p.has_password else "pin")
        elif p.has_pin and p.has_password:
            cred = "pin_or_password"
        elif p.has_password:
            cred = "password"
        else:
            cred = "pin" if p.has_pin else "none"
        out.append({"id": p.id, "name": p.name, "role": p.role.value, "protected": p.protected,
                    "avatar": p.avatar, "credential": cred, "shared": policy.is_lock_target(p)})
    return out


def principal_document(request: Request) -> dict:
    """Body of GET /api/auth/me."""
    pr = _caller(request)
    prof = pr.profile
    cfg = auth.load()
    doc: dict[str, Any] = {
        "kind": pr.kind, "role": pr.role, "protected": pr.is_protected, "signed_in": pr.signed_in,
        "name": pr.name,
        "profile": public_profile(prof),
        "permissions": sorted(pr.permissions) if pr.kind != "open" else ["*"],
        "claimed": policy.claimed(cfg),
        "needs_password": bool(prof and prof.role is Role.ADMIN and not prof.has_password),
        # What an Admin may see about this profile (counts, never content): said to every
        # profile that is not itself an Admin (view_activity).
        "disclosure": DISCLOSURE if pr.kind == "profile" and not pr.can(Permission.VIEW_ACTIVITY) else None,
        # 4.0: the lock target, whether this is the shared default profile, and the notices
        # the sign-in popup shows once (a PIN reset by an Admin).
        "lock_target": cfg.lock_target,
        "shared": policy.is_lock_target(prof),
        "notices": notices_for(pr),
    }
    dev = pr.device_id or device_hash_of(request)
    if dev:
        d = accounts.get_device(dev)
        doc["device"] = d.public() if d else None
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
    if prof.role is not Role.ADMIN:
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
    remembered = _was_remembered(request)
    try:
        auth.set_profile_password(prof.id, payload.new)
    except accounts.AccountError as e:
        return _account_error(e)
    accounts.log_activity("password_changed", profile_id=prof.id, category="audit")
    # set_profile_password signs the profile out everywhere; keep THIS device in,
    # remembered if it was.
    fresh = accounts.get_profile(prof.id)
    return session_response(request, {"ok": True}, fresh, persistent=remembered)


class OwnPinIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    current: str = Field(default="", max_length=256)
    new: str = Field(min_length=1, max_length=128)


@router.post("/api/auth/pin", dependencies=[_CHAT])
async def auth_own_pin(request: Request, payload: OwnPinIn):
    """Change YOUR OWN PIN (3.2): a Member, or an Admin who uses one.

    Confirmed with the current PIN (an Admin without a PIN yet confirms with the password).
    The Owner's PIN is the install PIN and changes through ``/api/auth/setup``; a Restricted
    profile's PIN is set for it by the Owner or an Admin (People & Access). Throttled like
    every other credential check."""
    pr = _caller(request)
    prof = pr.profile
    if prof is None:
        return _problem(403, "permission_denied", "Sign in to a profile to change its PIN.")
    if prof.protected:
        return _problem(409, "owner_immutable", "The Owner's PIN is the install PIN; change it in Settings > Account.")
    if policy.is_lock_target(prof):
        return _problem(403, "lock_target_fence",
                        "This profile is shared by everyone who uses this device; only an Admin "
                        "sets its PIN (People & Access).")
    pkey = prof.id
    blocked = _throttled(request, pkey)
    if blocked is not None:
        return blocked
    if prof.has_pin:
        ok = bool(payload.current) and auth.verify_profile_secret(prof, "pin", payload.current)
    elif prof.has_password:
        ok = bool(payload.current) and auth.verify_profile_secret(prof, "password", payload.current)
    else:
        ok = True                       # 4.0: a profile with no credential sets its first PIN
    if not ok:
        return _failed(request, pkey, prof.name)
    _succeeded(request, pkey)
    remembered = _was_remembered(request)
    try:
        auth.set_profile_pin(prof.id, payload.new)
    except accounts.AccountError as e:
        return _account_error(e)
    accounts.log_activity("pin_changed", profile_id=prof.id, category="audit")
    # set_profile_pin signs the profile out everywhere; keep THIS device in,
    # remembered if it was.
    fresh = accounts.get_profile(prof.id)
    return session_response(request, {"ok": True}, fresh, persistent=remembered)


class MachineIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rotate: bool = False
    machine_loopback_trust: bool | None = None
    machine_scopes: list[str] | None = None


@router.get("/api/auth/api-token", dependencies=[_SETTINGS])
async def api_token_get(request: Request):
    if not _caller(request).is_admin:
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
    if not _caller(request).is_admin:
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

def clean_permissions(role: Role | str, items: Any, field_name: str, *,
                      lock_target: bool = False) -> list[str]:
    """Validate a grants/denies list for a profile of ``role`` (BUILD-PLAN-v2 §1.1).

    A grant must be in the role's grantable column (``ROLE_GRANTABLE``): a permission the
    role can never hold (``ROLE_NEVER``, every ``use_tools:<id>`` panel for non-Admins
    included) is refused with **422**; one the role already has by default is dropped (a
    no-op). For the lock-target default profile, agents and voice cloning are not grantable
    either (the lock-target fence, 422). An Admin holds everything: its grants are dropped.
    Denies may name any known permission."""
    if items is None:
        return []
    if not isinstance(items, list):
        raise accounts.AccountError("permissions_invalid", f"{field_name} must be a list.", 422)
    r = normalise_role(role)
    out: list[str] = []
    grantable = {p.value for p in ROLE_GRANTABLE[r]}
    defaults = {p.value for p in ROLE_DEFAULTS[r]}
    fence = {p.value for p in LOCK_TARGET_FENCE}
    for it in items:
        if not isinstance(it, str) or not is_valid_permission(it):
            raise accounts.AccountError("permissions_invalid", f"Unknown permission {it!r}.", 422)
        if field_name == "grants":
            if lock_target and it in fence:
                raise accounts.AccountError(
                    "lock_target_fence",
                    "This is the shared default profile everyone lands on: agents and voice "
                    "cloning cannot be switched on for it.", 422)
            if r is Role.ADMIN or it in defaults:
                continue
            if it not in grantable:
                if it == Permission.ADMIN_SYSTEM.value:
                    raise accounts.AccountError(
                        "permissions_invalid",
                        "Host control belongs to Admins; it cannot be granted.", 422)
                raise accounts.AccountError(
                    "permissions_invalid",
                    f"A {r.value.capitalize()} profile can never be granted {it}.", 422)
        out.append(it)
    return list(dict.fromkeys(out))


#: 4.0 role words, plus the 3.x ones old clients still send (normalised by ``normalise_role``).
RoleIn = Literal["admin", "user", "basic", "member", "restricted"]


class PersonIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=40)
    role: RoleIn = "user"
    pin: str | None = Field(default=None, max_length=128)
    password: str | None = Field(default=None, max_length=256)
    avatar: str = Field(default="", max_length=120)
    grants: list[str] | None = None
    denies: list[str] | None = None
    session_timeout: int | None = Field(default=None, ge=60, le=30 * 86400)
    household_bots: str | list[str] | None = None


class PersonPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=40)
    role: RoleIn | None = None
    avatar: str | None = Field(default=None, max_length=120)
    grants: list[str] | None = None
    denies: list[str] | None = None
    session_timeout: int | None = Field(default=None, ge=60, le=30 * 86400)
    disabled: bool | None = None
    #: 4.0: refused here with a pointer to the route that owns it.
    household_bots: str | list[str] | None = None


class SecretIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pin: str = Field(default="", max_length=128)
    password: str = Field(default="", max_length=256)


def _may_manage(caller: policy.Principal, target_protected: bool) -> bool:
    """Every Admin manages every profile but THE protected Admin's (the 3.x Owner).

    That one exception is structural, not a permission: an Admin who could delete,
    disable, demote or re-credential the protected Admin could lock the household's
    owner out of their own install (and there is exactly one). Everything else the
    protected Admin may do to a profile - other Admins included - an Admin may do too."""
    if caller.is_admin:
        return not target_protected
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
        "profiles": [_person_out(p) for p in accounts.list_profiles()],
        "me": caller.profile_id,
        "roles": [r.value for r in ROLE_ORDER],
        "role_permissions": {r.value: sorted(p.value for p in ROLE_DEFAULTS[r]) for r in ROLE_ORDER},
        "role_grantable": {r.value: sorted(p.value for p in ROLE_GRANTABLE[r]) for r in ROLE_ORDER},
        "role_never": {r.value: sorted(p.value for p in ROLE_NEVER[r]) for r in ROLE_ORDER},
        "can_create_admin": caller.is_admin,
        "owner_has_password": _owner_has_password(),
        "disclosure": DISCLOSURE,
    }


@router.post("/api/people", status_code=201, dependencies=[_MANAGE])
async def people_create(request: Request, payload: PersonIn):
    caller = _caller(request)
    if not accounts.has_owner():
        auth.ensure_owner_for_pin()
    if not accounts.has_owner():
        return _problem(409, "owner_required", "Create the owner account first (Settings > Account).")
    role = normalise_role(payload.role)
    if not _may_manage(caller, False):
        return _problem(403, "permission_denied", "Only the Owner and Admins can create Admins.")
    try:
        # 4.0: PINs and passwords are optional on every profile (People warns about an
        # Admin with neither: ``no_credential`` in the reply).
        password_hash = None
        if payload.password:
            if role is not Role.ADMIN:
                return _problem(400, "password_role", "Only Admins have a password; use a PIN.")
            err = credentials.check_password(payload.password)
            if err:
                return _problem(400, "password_invalid", err)
            password_hash = credentials.hash_secret(payload.password)
        pin_hash = None
        if payload.pin:
            err = credentials.check_pin(payload.pin)
            if err:
                return _problem(400, "pin_invalid", err)
            pin_hash = credentials.hash_secret(payload.pin)
        prof = accounts.create_profile(
            payload.name, role, pin_hash=pin_hash, password_hash=password_hash,
            avatar=payload.avatar,
            grants=clean_permissions(role, payload.grants, "grants"),
            denies=clean_permissions(role, payload.denies, "denies"),
            session_timeout=payload.session_timeout,
            # a Basic profile sees only what the Admin picks: nothing until then
            household_bots=(clean_household_bots(payload.household_bots)
                            if payload.household_bots is not None
                            else ([] if role is Role.BASIC else None)))
    except accounts.AccountError as e:
        return _account_error(e)
    _audit(request, "profile_created", prof.id, f"{prof.name} ({prof.role.value})")
    profile_created(prof)
    return JSONResponse(_person_out(prof), status_code=201)


def _person_out(prof: accounts.Profile) -> dict:
    out = prof.public()
    out["no_credential"] = not prof.protected and not prof.has_pin and not prof.has_password
    out["lock_target"] = policy.is_lock_target(prof)
    return out


def profile_created(prof: accounts.Profile, *, first_admin: bool = False) -> None:
    """WP-PROF's hook (an own Atlas for Admin and User profiles; none for the first Admin of a
    fresh install, ``first_admin``). Never fails the create."""
    from . import profiles_service
    try:
        profiles_service.on_profile_created(prof, first_admin=first_admin)
    except Exception:
        log.exception("on_profile_created failed for %s", prof.id)


def clean_household_bots(value: Any) -> str | list[str] | None:
    """``"all"`` or a list of household character ids (unknown and personal ids refused)."""
    if value is None:
        return None
    if value == "all":
        return "all"
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise accounts.AccountError("household_bots_invalid",
                                    'household_bots must be "all" or a list of character ids.', 422)
    from . import characters
    out = []
    for bid in dict.fromkeys(value):
        b = config.resolve_bot(bid)
        if b is None or characters.is_personal(b):
            raise accounts.AccountError("household_bots_invalid", f"Unknown household character {bid!r}.", 422)
        out.append(b.id)
    return out


def _target(request: Request, profile_id: str) -> tuple[accounts.Profile | None, JSONResponse | None]:
    prof = accounts.get_profile(profile_id)
    if prof is None:
        return None, _problem(404, "profile_not_found", "No such profile.")
    caller = _caller(request)
    if prof.id == caller.profile_id or _may_manage(caller, prof.protected):
        return prof, None
    return None, _problem(403, "permission_denied", "You cannot manage that profile.")


@router.patch("/api/people/{profile_id}", dependencies=[_MANAGE])
async def people_patch(request: Request, profile_id: str, payload: PersonPatch):
    """Name, picture, grants / denies, timeout, disabled. 4.0: a role change and the
    household list go through ``profiles_service`` (``POST /api/people/{id}/role`` with its
    preview token - a User -> Basic change deletes content - and
    ``PUT /api/people/{id}/household-bots``), so the wipe can never be skipped."""
    prof, err = _target(request, profile_id)
    if err is not None:
        return err
    assert prof is not None
    caller = _caller(request)
    fields = payload.model_dump(exclude_unset=True)
    own = prof.id == caller.profile_id
    lock_target = policy.is_lock_target(prof)
    if "role" in fields:
        if normalise_role(fields["role"]) is prof.role:
            fields.pop("role")
        elif own or prof.protected:
            return _problem(409, "owner_immutable" if prof.protected else "permission_denied",
                            "You cannot change your own role.")
        else:
            return _problem(409, "use_role_route",
                            "Change a profile's type with POST /api/people/{id}/role (preview first).")
    if "household_bots" in fields:
        return _problem(409, "use_household_route",
                        "Set the household bots with PUT /api/people/{id}/household-bots.")
    try:
        if own and "disabled" in fields:
            return _problem(409, "owner_immutable" if prof.protected else "permission_denied",
                            "You cannot disable yourself.")
        if prof.protected and set(fields) - {"name", "avatar"}:
            return _problem(409, "owner_immutable", "The owner's role and limits cannot be changed here.")
        if lock_target and {"name", "avatar"} & set(fields) and not caller.is_admin:
            # The shared default profile: whoever lands on it may not rename it for everyone.
            return _problem(403, "lock_target_fence",
                            "Only an Admin changes the shared default profile's name or picture.")
        if own and set(fields) - {"name", "avatar"} and not _may_manage(caller, prof.protected):
            # A profile edits its own name and picture, nothing else: grants, denies
            # and timeouts are set by someone who may manage it.
            return _problem(403, "permission_denied",
                            "You can change your own name and picture; limits and permissions are "
                            "set by an Admin.")
        if "grants" in fields:
            fields["grants"] = clean_permissions(prof.role, fields["grants"], "grants",
                                                 lock_target=lock_target)
        if "denies" in fields:
            fields["denies"] = clean_permissions(prof.role, fields["denies"], "denies")
        updated = accounts.update_profile(profile_id, **fields)
    except accounts.AccountError as e:
        return _account_error(e)
    if "disabled" in fields:
        auth.revoke_profile(profile_id)
    _audit(request, "profile_updated", profile_id, ",".join(sorted(fields)))
    return _person_out(updated)


@router.delete("/api/people/{profile_id}", dependencies=[_MANAGE])
async def people_delete(request: Request, profile_id: str):
    """4.0: deleting a profile deletes its chats, characters, files and cloned voices, so it
    goes through ``profiles_service.delete_profile`` with the counts confirmed first:
    ``GET /api/people/{id}/delete/preview`` then ``POST /api/people/{id}/delete``."""
    prof, err = _target(request, profile_id)
    if err is not None:
        return err
    assert prof is not None
    if prof.protected:
        return _problem(409, "owner_immutable", "The protected Admin cannot be deleted.")
    return _problem(409, "use_delete_route",
                    "Delete a profile with POST /api/people/{id}/delete (preview the counts first).")


@router.post("/api/people/{profile_id}/pin", dependencies=[_MANAGE])
async def people_set_pin(request: Request, profile_id: str, payload: SecretIn):
    prof, err = _target(request, profile_id)
    if err is not None:
        return err
    assert prof is not None
    if prof.protected:
        return _problem(409, "owner_immutable", "Change the Owner's PIN in Settings > Account.")
    try:
        auth.set_profile_pin(profile_id, payload.pin)
    except accounts.AccountError as e:
        return _account_error(e)
    _audit(request, "pin_set", profile_id, prof.name)
    _note_reset(request, prof)
    return {"ok": True}


def _note_reset(request: Request, prof: accounts.Profile) -> None:
    """Tamper-evident credential reset (BUILD-PLAN-v2 §1.4): audited (the caller's row),
    the profile signed out everywhere (the epoch moved with the credential), and a notice
    the profile is shown once in its next sign-in popup."""
    caller = _caller(request)
    if caller.profile_id == prof.id:
        return
    with contextlib.suppress(Exception):
        add_notice(prof.id, "pin_reset", by=caller.name or "an Admin")


@router.delete("/api/people/{profile_id}/pin", dependencies=[_MANAGE])
async def people_clear_pin(request: Request, profile_id: str):
    prof, err = _target(request, profile_id)
    if err is not None:
        return err
    assert prof is not None
    try:
        auth.clear_profile_pin(profile_id)
    except accounts.AccountError as e:
        return _account_error(e)
    _audit(request, "pin_cleared", profile_id, prof.name)
    _note_reset(request, prof)
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
    if prof.id == caller.profile_id or prof.protected or not caller.is_admin:
        return _problem(409, "permission_denied", "Use Settings > Account to change your own password.")
    try:
        auth.set_profile_password(profile_id, payload.password)
    except accounts.AccountError as e:
        return _account_error(e)
    _audit(request, "password_set", profile_id, prof.name)
    _note_reset(request, prof)
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


@router.get("/api/people/devices", dependencies=[_MANAGE])
async def people_devices(request: Request):
    here = device_hash_of(request)
    return {"devices": [d.public() | {"this_device": d.id == here} for d in accounts.list_devices()],
            "this_device": here}


@router.delete("/api/people/devices/{device_id}", dependencies=[_MANAGE])
async def people_forget_device(request: Request, device_id: str):
    caller = _caller(request)
    if not caller.is_admin and device_id != device_hash_of(request):
        return _problem(403, "permission_denied", "You can only forget this device.")
    accounts.delete_device(device_id)
    _audit(request, "device_removed", device_id[:12])
    return {"ok": True}


@router.get("/api/people/activity", dependencies=[_VIEW])
async def people_activity(request: Request, profile_id: str | None = None, days: int = 7):
    days = max(1, min(days, 90))
    return {"summary": accounts.activity_summary(profile_id, days),
            "recent": accounts.recent_activity(profile_id, limit=100)}


@router.get("/api/people/audit", dependencies=[_VIEW])
async def people_audit(request: Request):
    return {"entries": accounts.recent_activity(None, category="audit", limit=200)}


@router.get("/api/me/activity", dependencies=[_CHAT])
async def my_activity(request: Request, days: int = 7):
    """A profile's own activity summary: what an Admin's activity view counts about it."""
    pr = _caller(request)
    pid = pr.profile_id
    return {"disclosure": DISCLOSURE, "visible_to_owner": True,
            "summary": accounts.activity_summary(pid, max(1, min(days, 30))) if pid else {"days": []}}
