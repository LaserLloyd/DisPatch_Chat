"""The policy engine: principals, declared requirements, one decision function.

Before 3.0 access control was four mechanisms (a global middleware with a default-
allow path deny-list, ~100 in-handler guards, FastAPI dependencies on some routers,
and per-action checks in the WebSocket handler) with eight re-implementations of
"is this the operator?". Now there is exactly one question - *may this principal
satisfy this requirement?* - answered by :func:`authorize`, and exactly one place
every route and WebSocket action declares its answer (``app/route_policy.py``).
``tests/test_route_table.py`` fails the build if a route is missing from it.

Principals
----------
``profile``    a signed-in person: Owner, Admin, Member or Restricted
``anonymous``  nobody signed in on an installation that has an owner: **Restricted**
               (this is the old "Safe Mode": the door is open to look, nothing more)
``machine``    an agent, cron or the image rig, holding named :class:`MachineScope`s.
               Identity is a *credential* (the install's ``api_token``). Network
               position - being on loopback - only counts while the owner leaves
               ``machine_loopback_trust`` on (the default for installs upgraded from
               2.x, which have on-box crons); it is never a role.
``open``       a brand-new install with no owner and no PIN, reached from this
               machine. Everything is allowed - exactly the 2.x "no PIN" behaviour -
               until the owner account is created (``POST /api/auth/setup``).
"""

from __future__ import annotations

import functools
import re
import threading
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from fastapi import HTTPException, Request
from starlette.routing import Match, Mount

from . import accounts, auth, config
from .contracts.permissions import (
    MACHINE_DEFAULT_SCOPES,
    RESTRICTED_NEVER,
    ROLE_PERMISSIONS,
    TOOL_PREFIX,
    MachineScope,
    Permission,
    Role,
    is_valid_permission,
)

SESSION_COOKIE = "lc_session"
DEVICE_COOKIE = "lc_device"

#: What every denied browser sees for a Restricted caller: the 2.x wording, so the
#: frontend's lock handling and every existing client keep working.
UNLOCK_DETAIL = "Unlock for full access"


# --------------------------------------------------------------------------- #
# Requirements (what a route declares)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Requirement:
    """What one route (or WebSocket action) demands. Exactly one shape per route."""

    permission: str | None = None          # a Permission value, or None
    machine: tuple[str, ...] = ()          # MachineScope values that may also call it
    public: bool = False                   # no credential needed (explicit PUBLIC marker)
    authenticated: bool = False            # any signed-in profile, whatever its role
    #: The role may lack ``permission`` and still reach the handler, which then MUST
    #: narrow the answer to what the principal may see (a Restricted profile lists
    #: only audience=restricted|all characters, only ``safe`` tools). The G5 test
    #: exercises every scoped route with a Restricted session.
    scoped: bool = False
    note: str = ""
    #: What a signed-in person is told when the route is machine-only.
    denied_detail: str = ""

    @property
    def machine_only(self) -> bool:
        return self.permission is None and bool(self.machine) and not self.public \
            and not self.authenticated

    def describe(self) -> dict:
        d: dict[str, Any] = {}
        if self.public:
            d["public"] = True
        if self.permission:
            d["permission"] = self.permission
        if self.authenticated:
            d["authenticated"] = True
        if self.machine:
            d["machine"] = list(self.machine)
        if self.scoped:
            d["scoped"] = True
        if self.note:
            d["note"] = self.note
        return d


PUBLIC = Requirement(public=True)


def R(permission: Permission | str | None = None, *, machine: Iterable[MachineScope | str] = (),
      scoped: bool = False, note: str = "") -> Requirement:
    perm = permission.value if isinstance(permission, Permission) else permission
    return Requirement(permission=perm,
                       machine=tuple(m.value if isinstance(m, MachineScope) else m for m in machine),
                       scoped=scoped, note=note)


def public(note: str) -> Requirement:
    """An explicit PUBLIC marker. The note says WHY no credential is needed."""
    return Requirement(public=True, note=note)


AUTHENTICATED = Requirement(authenticated=True)


# --------------------------------------------------------------------------- #
# Principals
# --------------------------------------------------------------------------- #


def effective_permissions(profile: accounts.Profile) -> frozenset[str]:
    """Role set + the profile's grants - its denies, within the role's ceiling."""
    role = Role(profile.role)
    base = {p.value for p in ROLE_PERMISSIONS[role]}
    if role is Role.OWNER:
        return frozenset(base)                       # the owner cannot be restricted
    for g in profile.grants:
        if not is_valid_permission(g):
            continue
        if role is Role.RESTRICTED and g in {p.value for p in RESTRICTED_NEVER}:
            continue
        if g == Permission.ADMIN_SYSTEM.value:
            continue                                  # host control is Owner-only, for every other role
        base.add(g)
    for d in profile.denies:
        base.discard(d)
    return frozenset(base)


@dataclass(frozen=True)
class Principal:
    kind: str                                   # profile | anonymous | machine | open
    role: str | None = None                     # owner|admin|member|restricted (None for machine)
    profile_id: str | None = None
    name: str = ""
    permissions: frozenset[str] = frozenset()
    machine_scopes: frozenset[str] = frozenset()
    session_token: str | None = None
    device_id: str | None = None
    via: str = ""                               # cookie | device | api_token | loopback | callback | open | anonymous
    profile: accounts.Profile | None = field(default=None, compare=False, repr=False)

    # -- classification ---------------------------------------------------- #
    @property
    def is_machine(self) -> bool:
        return self.kind == "machine"

    @property
    def is_restricted(self) -> bool:
        """The old "decoy": Safe Mode, a locked device, a Restricted profile."""
        return self.kind == "anonymous" or self.role == Role.RESTRICTED.value

    @property
    def is_operator(self) -> bool:
        """The old "operator": an Owner or Admin session, or an open install."""
        return self.kind == "open" or self.role in (Role.OWNER.value, Role.ADMIN.value)

    @property
    def is_owner(self) -> bool:
        return self.kind == "open" or self.role == Role.OWNER.value

    @property
    def signed_in(self) -> bool:
        return self.kind in ("profile", "open")

    # -- decisions ---------------------------------------------------------- #
    def can(self, permission: Permission | str) -> bool:
        p = permission.value if isinstance(permission, Permission) else permission
        if self.kind == "open":
            return True
        if self.kind == "machine":
            return False
        if p in self.permissions:
            return True
        # use_tools:<id> is satisfied by the blanket use_tools too.
        return p.startswith(TOOL_PREFIX) and Permission.USE_TOOLS.value in self.permissions

    def has_scope(self, scope: MachineScope | str) -> bool:
        s = scope.value if isinstance(scope, MachineScope) else scope
        return self.kind == "machine" and s in self.machine_scopes

    def summary(self) -> dict:
        return {"kind": self.kind, "role": self.role, "profile_id": self.profile_id,
                "name": self.name, "via": self.via,
                "permissions": sorted(self.permissions) if self.kind != "open" else ["*"]}


def anonymous(device_id: str | None = None) -> Principal:
    return Principal(kind="anonymous", role=Role.RESTRICTED.value, name="Guest",
                     permissions=frozenset(p.value for p in ROLE_PERMISSIONS[Role.RESTRICTED]),
                     device_id=device_id, via="anonymous")


def open_principal() -> Principal:
    return Principal(kind="open", role=Role.OWNER.value, name="Owner", via="open")


def machine_principal(via: str, scopes: Iterable[str] | None = None) -> Principal:
    sc = frozenset(scopes) if scopes is not None else frozenset(s.value for s in MACHINE_DEFAULT_SCOPES)
    return Principal(kind="machine", name="machine", machine_scopes=sc, via=via)


def profile_principal(profile: accounts.Profile, *, token: str | None = None,
                      device_id: str | None = None, via: str = "cookie") -> Principal:
    return Principal(kind="profile", role=profile.role, profile_id=profile.id, name=profile.name,
                     permissions=effective_permissions(profile), session_token=token,
                     device_id=device_id, via=via, profile=profile)


def for_session(sess: Any, device_id: str | None = None) -> Principal | None:
    """The principal behind a live auth session (None = the session is not valid)."""
    if sess is None:
        return None
    token = getattr(sess, "token", None)
    pid = getattr(sess, "profile_id", None)
    if pid is None:
        # A session minted before accounts existed (or before the owner row was
        # created): the Owner. With no owner row yet it is an owner with no profile.
        owner = accounts.get_owner()
        if owner is None:
            return Principal(kind="profile", role=Role.OWNER.value, name="Owner",
                             permissions=frozenset(p.value for p in ROLE_PERMISSIONS[Role.OWNER]),
                             session_token=token, device_id=device_id, via="cookie")
        return profile_principal(owner, token=token, device_id=device_id)
    prof = accounts.get_profile(pid)
    if prof is None or prof.disabled or prof.epoch != getattr(sess, "epoch", 0):
        return None
    return profile_principal(prof, token=token, device_id=device_id)


def for_device(device_id: str | None) -> Principal | None:
    """A device bound to a default profile opens as that profile - capped at Member."""
    prof = accounts.default_profile_for_device(device_id)
    if prof is None or prof.role in (Role.OWNER.value, Role.ADMIN.value):
        return None
    return profile_principal(prof, device_id=device_id, via="device")


# --------------------------------------------------------------------------- #
# Decisions
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Decision:
    allowed: bool
    status: int = 200
    code: str = ""
    detail: str = ""
    scoped: bool = False
    required: str = ""


ALLOW = Decision(True)


def authorize(principal: Principal, req: Requirement) -> Decision:
    """The one decision function. Pure: no I/O, no request."""
    if req.public:
        return ALLOW
    if principal.kind == "open":
        return Decision(True, scoped=False)
    need = req.permission or (",".join(req.machine) if req.machine else "")
    if principal.kind == "machine":
        if any(principal.has_scope(m) for m in req.machine):
            return ALLOW
        return Decision(False, 403, "machine_scope_denied",
                        "This API key is not allowed to call that endpoint.", required=need)
    if req.authenticated:
        if principal.signed_in:
            return ALLOW
        return Decision(False, 403, "safe_mode", UNLOCK_DETAIL, required="authenticated")
    if req.permission is None:
        # machine-only route, or a declaration bug: no human role satisfies it.
        if principal.is_restricted:
            return Decision(False, 403, "safe_mode", UNLOCK_DETAIL, required=need)
        return Decision(False, 403, "agents_only" if req.machine else "route_undeclared",
                        (req.denied_detail or "Only agents may call this.") if req.machine
                        else UNLOCK_DETAIL, required=need)
    if principal.can(req.permission):
        return ALLOW
    if req.scoped:
        return Decision(True, scoped=True)
    if principal.is_restricted:
        return Decision(False, 403, "safe_mode", UNLOCK_DETAIL, required=req.permission)
    return Decision(False, 403, "permission_denied",
                    "Your profile does not have permission to do that.", required=req.permission)


# --------------------------------------------------------------------------- #
# Per-request handle: request.state.policy
# --------------------------------------------------------------------------- #


class Policy:
    """What a handler holds. Everything it needs to ask, nothing it can change."""

    def __init__(self, principal: Principal, scoped: bool = False) -> None:
        self.principal = principal
        self.scoped = scoped            # the gate let a non-holder through a scoped route

    # compat names for the old booleans
    @property
    def restricted(self) -> bool:
        return self.principal.is_restricted

    @property
    def machine(self) -> bool:
        return self.principal.is_machine

    @property
    def operator(self) -> bool:
        return self.principal.is_operator

    def can(self, permission: Permission | str) -> bool:
        return self.principal.can(permission)

    def require(self, permission: Permission | str, detail: str | None = None) -> None:
        if not self.principal.can(permission):
            raise denied(self.principal, permission, detail)

    def require_signed_in(self) -> None:
        if not self.principal.signed_in:
            raise denied(self.principal, "authenticated")

    def require_owner(self) -> None:
        if not self.principal.is_owner:
            raise denied(self.principal, "owner")

    def can_use_tool(self, tool_id: str, *, safe: bool = False) -> bool:
        """Blanket ``use_tools``, an explicit ``use_tools:<id>`` grant, or - for the
        Restricted role only - a tool the owner flagged ``safe: true``."""
        p = self.principal
        if p.can(tool_permission(tool_id)):
            return True
        return bool(safe) and p.is_restricted

    def allowed_bot_ids(self, all_bots: Iterable[config.Bot]) -> set[str]:
        """Characters this principal may see and talk to (audience filter)."""
        if self.principal.is_restricted:
            return {b.id for b in all_bots if b.audience in ("all", "restricted")}
        return {b.id for b in all_bots}


def tool_permission(tool_id: str) -> str:
    return f"{TOOL_PREFIX}{tool_id}"


def denied(principal: Principal, required: Permission | str, detail: str | None = None) -> HTTPException:
    req = required.value if isinstance(required, Permission) else required
    if principal.is_restricted:
        exc = HTTPException(403, detail or UNLOCK_DETAIL)
    else:
        exc = HTTPException(403, detail or "Your profile does not have permission to do that.")
    exc.required_permission = req          # type: ignore[attr-defined]
    return exc


def of(request: Request) -> Policy:
    """The Policy for ``request``. Derived from the gate's verdict when the
    middleware ran; otherwise re-derived from the cookies alone (a router mounted
    on a bare app must still fail closed)."""
    state = request.state
    pol = getattr(state, "policy", None)
    if pol is not None:
        return pol
    # No gate ran (a router on a bare app, a unit test handing over a stub request):
    # honour the 2.x flags if something set them, else derive from the cookies.
    if getattr(state, "machine", False):
        pol = Policy(machine_principal("state"))
    elif getattr(state, "decoy", False):
        pol = Policy(anonymous())
    else:
        cfg = auth.load()
        if not claimed(cfg):
            pol = Policy(open_principal())
        else:
            sess = getattr(state, "session", None) or auth.get_session(
                request.cookies.get(SESSION_COOKIE))
            pol = Policy(for_session(sess) or anonymous())
    try:
        state.policy = pol
    except Exception:
        pass
    return pol


def require_permission(permission: Permission | str):
    """FastAPI dependency: ``Depends(require_permission(Permission.MANAGE_SETTINGS))``.

    The gate already enforced the declared requirement before the handler ran; this
    is the same sentence stated a second time at the handler (defence in depth, and
    the way a router mounted on a bare app declares its own lock)."""
    perm = permission

    def dependency(request: Request) -> None:
        of(request).require(perm)
    dependency.required_permission = perm.value if isinstance(perm, Permission) else perm  # type: ignore[attr-defined]
    return dependency


# --------------------------------------------------------------------------- #
# Installation state
# --------------------------------------------------------------------------- #


def claimed(cfg: auth.SecurityConfig | None = None) -> bool:
    """Has this installation been claimed - a PIN, or an owner with a password?

    An owner row alone does not claim it: for a PIN-only install the row is just a
    mirror of security.yaml's PIN, so deleting that file (the documented "remove the
    lock" recovery) must open the install again."""
    cfg = cfg or auth.load()
    if cfg.pin_set:
        return True
    owner = accounts.get_owner()
    return bool(owner is not None and owner.has_password)


def allow_open_install() -> bool:
    """``DISPATCH_ALLOW_OPEN_INSTALL=1``: keep the 2.x "no PIN means open to the LAN"
    behaviour on an unclaimed install. Off by default."""
    return str(config.env("ALLOW_OPEN_INSTALL", "") or "").strip().lower() in ("1", "true", "yes", "on")


def is_loopback_origin(request_client_host: str | None, proxied: bool) -> bool:
    """A request from a process on THIS machine (a browser on the host, curl): the
    peer is loopback and no proxy stands between us and the real client."""
    return ip_is_loopback(request_client_host or "") and not proxied


# --------------------------------------------------------------------------- #
# Route lookup
# --------------------------------------------------------------------------- #

_lookup_lock = threading.Lock()


class RouteIndex:
    """Resolves (method, concrete path) -> (route path template, Requirement)."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self._cache: OrderedDict[tuple[str, str], tuple[str | None, Requirement | None]] = OrderedDict()

    def match(self, method: str, path: str) -> tuple[str | None, Requirement | None]:
        key = (method, path)
        with _lookup_lock:
            hit = self._cache.get(key)
            if hit is not None:
                self._cache.move_to_end(key)
                return hit
        out = self._match(method, path)
        with _lookup_lock:
            self._cache[key] = out
            while len(self._cache) > 4096:
                self._cache.popitem(last=False)
        return out

    def _match(self, method: str, path: str) -> tuple[str | None, Requirement | None]:
        from . import route_policy
        scope = {"type": "http", "path": path, "method": method, "root_path": "", "headers": []}
        partial = None
        for route in self.app.router.routes:
            m, _ = route.matches(scope)
            if m is Match.FULL:
                if isinstance(route, Mount):
                    return route.path, route_policy.requirement_for_mount(route.path, path)
                tmpl = getattr(route, "path", None)
                return tmpl, route_policy.requirement_for(method, tmpl, path)
            if m is Match.PARTIAL and partial is None:
                partial = route
        if partial is not None and not isinstance(partial, Mount):
            # The path exists but not for this method (the app will answer 405). Apply
            # the requirement the route declares for its OTHER methods, so a stranger
            # is refused the same way and learns nothing from the status code.
            tmpl = getattr(partial, "path", None)
            for other in sorted(getattr(partial, "methods", None) or ()):
                req = route_policy.requirement_for(other, tmpl, path)
                if req is not None:
                    return tmpl, req
            return tmpl, None
        # No route at all. App-shaped paths (/apps, /api/apps, a mounted app's legacy
        # prefix) are still refused to a caller who may not use apps, rather than 404'd.
        req = route_policy.requirement_for_unmatched(method, path)
        return (None, req) if req is not None else (None, None)


def clear_route_cache(index: RouteIndex | None) -> None:
    if index is not None:
        with _lookup_lock:
            index._cache.clear()


# --------------------------------------------------------------------------- #
# Small helpers shared by the gate
# --------------------------------------------------------------------------- #

_PERM_RE = re.compile(r"^[a-z_]+(:[A-Za-z0-9_.-]+)?$")


@functools.lru_cache(maxsize=256)
def valid_permission_name(name: str) -> bool:
    return bool(_PERM_RE.match(name)) and is_valid_permission(name)


# --------------------------------------------------------------------------- #
# Where a request came from (shared by the gate, the sign-in routes and the quotas)
# --------------------------------------------------------------------------- #

#: Every header a reverse proxy in front of us is known to stamp. Presence is all that
#: is read: a proxy (a TLS terminator, tailnet Serve) makes a REMOTE caller's socket
#: peer look like 127.0.0.1, so "the peer is loopback" only means "on this box" once
#: no forwarding header is present. Forging one on a genuine local call can only
#: tighten a check, never loosen one.
PROXY_MARKER_HEADERS = (
    "x-forwarded-for", "forwarded", "x-real-ip", "x-forwarded-host",
    "x-forwarded-proto", "via", "tailscale-headers-info",
    "tailscale-user-login", "tailscale-user-name", "tailscale-user-profile-pic",
)


def proxied_request(request: Request) -> bool:
    return any(request.headers.get(h) for h in PROXY_MARKER_HEADERS)


def ip_is_loopback(host: str) -> bool:
    return bool(host) and (host in ("127.0.0.1", "::1", "::ffff:127.0.0.1") or host.startswith("127."))


def loopback_socket(request: Request) -> bool:
    return ip_is_loopback(request.client.host if request.client else "")


def quota_ip(headers, peer: str) -> str:
    """The identity an anonymous device's budget and throttle are charged to.

    The socket peer, and ONLY the socket peer, unless the peer is loopback: a bucket
    is a fresh ALLOWANCE, so honouring a caller-writable ``X-Forwarded-For`` would let
    one device mint as many allowances as it likes. Tailscale Serve is the only thing
    that ever fronts the app and it connects from loopback, so there (and only there)
    the RIGHTMOST forwarded value - the one the proxy itself appended - is believed."""
    import ipaddress
    if ip_is_loopback(peer):
        last = (headers.get("x-forwarded-for") or "").split(",")[-1].strip()
        if last.startswith("[") and "]" in last:
            last = last[1:last.index("]")]
        elif last.count(":") == 1 and "." in last:
            last = last.rsplit(":", 1)[0]
        try:
            return str(ipaddress.ip_address(last))
        except ValueError:
            pass
    return peer or "?"


def request_quota_ip(request: Request) -> str:
    client = getattr(request, "client", None)
    return quota_ip(getattr(request, "headers", None) or {}, client.host if client else "?")


def request_is_https(request: Request | None) -> bool:
    """Did this request arrive over TLS, directly or through our proxy?

    ``X-Forwarded-Proto`` is believed only when a forwarding header proves the request
    came through a proxy; otherwise a client could set it on a plain-http call and pin
    ``Secure`` on a cookie the browser would then refuse to send back."""
    if request is None:
        return False
    if request.url.scheme == "https":
        return True
    proxied = bool(request.headers.get("x-forwarded-for") or request.headers.get("forwarded")
                   or request.headers.get("tailscale-headers-info"))
    if not proxied:
        return False
    proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
    return proto.lower() == "https"
