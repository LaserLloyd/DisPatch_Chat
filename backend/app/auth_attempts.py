"""One throttle for every credential check (4.3, issue #3).

Every route that checks a secret a person typed (unlock, switch, setup token, recovery code,
the backup-key reveal, an OIDC sign-in) goes through :func:`guarded_verify`, so the buckets,
the "refuse before verifying" rule and the failure bookkeeping live in one place.

The rule (4.3, issue #3; PLAN-issues-v2 section 1 row 3, tightened in v2):

1. The (device, profile) bucket and the address bucket (an IPv6 address counts by its
   /64) refuse BEFORE verifying, as in 3.x/4.x.
2. At the install-wide ceiling (a distributed guesser) only two kinds of caller are
   verified at all: a *remembered device bound to that profile* (its session or device
   cookie is a live trusted entry of the profile) and *this computer* (a loopback peer
   with no proxy headers, so Tailscale Serve traffic does not count). A correct
   credential from them succeeds; a wrong one is registered and answered 429.
3. Everybody else at the ceiling is refused BEFORE ``verify()`` runs - an unknown device
   learns nothing, however many /64s it owns.

A remembered device is keyed by device, not by address: a noisy neighbour on the same
address cannot lock the owner's own phone out.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable

from fastapi import Request

log = logging.getLogger("dispatch.auth")


class AttemptRefused(Exception):
    """Refused before verifying: answer ``status`` (429) with ``code`` and Retry-After."""

    def __init__(self, retry_after: float, *, status: int = 429,
                 code: str = "too_many_attempts") -> None:
        super().__init__(f"{code}: retry after {retry_after:.0f}s")
        self.status = status
        self.code = code
        self.retry_after = float(retry_after)


def guarded_verify(request: Request, *, scope: str, subject: str,
                   verify: Callable[[], bool]) -> bool:
    """Charge the buckets, verify only when allowed, register the outcome.

    ``scope`` names the kind of credential ("unlock", "switch", "setup", "recovery",
    "reveal", "oidc"); ``subject`` is the throttle's profile key (a profile id, or the scope
    itself for install-wide secrets such as the setup token).

    Returns True on a correct credential, False on a wrong one (the failure is already
    registered; the caller answers 401 and writes its own audit line). Raises
    :class:`AttemptRefused` when refused before verifying.
    """
    from . import auth, people_api, policy

    dev, ip = people_api._client_keys(request)
    client = getattr(request, "client", None)
    peer = client.host if client else ""
    remembered = auth.remembered_for(
        subject,
        session_token=request.cookies.get(policy.SESSION_COOKIE),
        device_id=people_api.device_hash_of(request))
    local, glob = auth.throttle_state(dev, subject, ip, skip_ip=remembered)
    if local > 0:
        raise AttemptRefused(int(local) + 1)
    privileged = remembered or policy.is_loopback_origin(peer, policy.proxied_request(request))
    if glob > 0 and not privileged:
        # Refused before verifying: the credential is never looked at (scope names the
        # route for the log; the count is not charged, as nothing was tested).
        log.info("credential check refused at the global ceiling (%s)", scope)
        raise AttemptRefused(int(glob) + 1)
    if verify():
        auth.register_success(dev, subject, ip)
        return True
    auth.register_failure(dev, subject, ip)
    if glob > 0:
        raise AttemptRefused(int(glob) + 1)
    return False


def refusal_response(e: AttemptRefused):
    """The 429 every credential route answers for :class:`AttemptRefused` (the same body and
    Retry-After header the 3.x/4.x throttle sent, so clients need no change)."""
    from fastapi.responses import JSONResponse

    from .contracts import errors
    secs = max(1, int(e.retry_after))
    msg = f"Too many tries. Wait {secs} seconds, then try again."
    return JSONResponse({"detail": msg, "retry_after": secs,
                         "error": errors.envelope(e.code, message=msg)},
                        status_code=e.status, headers={"Retry-After": str(secs)})


# --------------------------------------------------------------------------- #
# A cheap per-address rate for public routes that check no secret (4.3 Fable)
# --------------------------------------------------------------------------- #

#: ``GET /api/oidc/start`` mints one sign-in state per call into a table of 512; without a
#: rate a flood from one address pushes real sign-ins out of it. 30 a minute is far above a
#: person clicking "Sign in with ...".
START_LIMIT = 30
START_WINDOW_S = 60.0
_MAX_KEYS = 4096
_starts: dict[str, deque[float]] = {}
_starts_lock = threading.Lock()


def rate_ok(key: str, *, limit: int = START_LIMIT, window_s: float = START_WINDOW_S,
            now: float | None = None) -> float:
    """Count one call for ``key`` (an address). Returns 0 when it may go on, else the seconds
    to wait (the call is not counted)."""
    t = time.monotonic() if now is None else now
    with _starts_lock:
        q = _starts.get(key)
        if q is None:
            if len(_starts) >= _MAX_KEYS:            # forget the quietest addresses first
                for k in [k for k, v in _starts.items() if not v or t - v[-1] >= window_s]:
                    _starts.pop(k, None)
                while len(_starts) >= _MAX_KEYS:
                    _starts.pop(next(iter(_starts)))
            q = _starts[key] = deque()
        while q and t - q[0] >= window_s:
            q.popleft()
        if len(q) >= limit:
            return max(1.0, window_s - (t - q[0]))
        q.append(t)
        return 0.0


def reset_for_tests() -> None:
    with _starts_lock:
        _starts.clear()
