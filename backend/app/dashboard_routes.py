"""HTTP surface for the operator dashboard: summary, deep check, log tail.

MOUNTING IT (the exact change in main.py — one added name, one added line):

    1. add ``dashboard_routes`` to the existing package import near the top:

           from . import (auth, config, dashboard_routes, openclaw,
                          openclaw_text, reactions, terminal)

    2. THE ONE LINE, anywhere after ``app = FastAPI(...)`` and before the static
       mounts at the bottom of the file:

           app.include_router(dashboard_routes.router)

    3. and — belt-and-braces, not the gate itself — add the prefix to the tuple
       of paths ``_decoy_blocked()`` bars for limited (Safe-Mode) sessions:

           if path.startswith(("/media/", "/api/media", "/api/files",
                               "/api/search", "/api/export", "/api/recover",
                               "/api/openclaw", "/api/terminal",
                               "/api/dashboard")):        # <- add this
               return True

Step 3 is a second lock on the same door, deliberately. This router does NOT
depend on it, and does not depend on main's ``auth_gate`` middleware having run
at all: :func:`_require_operator` re-derives the answer from ``auth`` itself and
is attached as a router-level dependency, so it applies to every route here —
including any added later. Mount this router on a bare FastAPI app with no
middleware whatsoever and a sessionless caller still gets 403. That is the
fail-closed property; step 3 just means a limited session is turned away one
layer earlier, consistent with every other admin surface in the app.

Access model, matching the rest of the app exactly (see main.auth_gate):
  * a live full-access session cookie  → allowed;
  * no PIN configured at all           → allowed, because the whole app is open
    in that state — and this page's own `auth.no_pin` finding is what tells the
    operator to fix it;
  * anything else (Safe Mode, expired session, no cookie) → 403.

Everything here is a read. The expensive path (`/deep`) is serialised behind a
lock and returns 409 rather than queueing, mirroring the service panels'
busy-op contract.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from . import auth, dashboard

# Must match main.COOKIE_NAME. Duplicated rather than imported: main imports
# THIS module to mount it, so importing main here would be a cycle. It is the
# cookie name the app has always used; changing it means changing both.
COOKIE_NAME = "lc_session"

# Poll-collapsing micro-cache for the summary. Several tabs (and a phone) can
# have the dashboard open at once; one probe per window is plenty, and it keeps
# the page cheap enough to poll.
SUMMARY_TTL_S = 2.0
_summary_cache: dict = {"ts": 0.0, "body": None}


class _DeepGate:
    """One-at-a-time gate for the deep check, with a real try-acquire.

    ``asyncio.Lock`` has none, and the obvious substitutes don't work:
    ``if lock.locked(): 409`` followed by ``async with lock`` is a TOCTOU — two
    callers can both see it free, and the second one silently QUEUES a full
    database scan instead of getting the 409 the contract promises — while
    ``wait_for(lock.acquire(), 0)`` always times out, because the task it wraps
    has not run yet when wait_for tests it.

    So: a bare flag, tested and set with no ``await`` in between. On a
    single-threaded event loop that pair cannot be interleaved, which is exactly
    the atomicity the route needs. ``locked()`` is kept so the shape still reads
    like the lock it replaces.
    """

    __slots__ = ("_busy",)

    def __init__(self) -> None:
        self._busy = False

    def locked(self) -> bool:
        return self._busy

    def try_acquire(self) -> bool:
        if self._busy:
            return False
        self._busy = True
        return True

    def release(self) -> None:
        self._busy = False


# The deep check walks the whole database. One at a time, and a second caller
# gets told so rather than piling on.
_deep_gate = _DeepGate()


def _require_operator(request: Request) -> None:
    """Full-session-only gate that trusts nothing but ``auth``.

    Reads the cookie itself rather than relying on ``request.state`` being
    populated, so the answer is the same whether or not the auth middleware ran
    (or ever runs). Explicitly honours a middleware verdict of "this is Safe
    Mode" when one exists, and otherwise refuses whenever a lock is configured
    and no live session was presented.
    """
    # Record the socket we are actually served on before doing anything else.
    # It is the ASGI server's own value (not a header, not user input), and it
    # is the only thing in the process that knows the real bind: SETTINGS.host
    # is what was configured, and every launcher passes uvicorn its own --host.
    # Done here rather than per-route so a route added later gets it too.
    dashboard.note_bound_socket(request.scope.get("server"))

    if getattr(request.state, "decoy", False):
        raise HTTPException(403, "Unlock for full access")
    session = getattr(request.state, "session", None)
    if session is None:
        session = auth.get_session(request.cookies.get(COOKIE_NAME))
    if session is not None:
        auth.touch_session(session.token)
        return
    # No session. The only state in which that is still the operator is the one
    # where the app has no lock at all — the same rule the rest of the app uses.
    if auth.load().pin_set:
        raise HTTPException(403, "Unlock for full access")


router = APIRouter(prefix="/api/dashboard", tags=["dashboard"],
                   dependencies=[Depends(_require_operator)])


def _db():
    """The live Database, resolved at call time.

    Imported lazily on purpose: main imports this module, so a module-level
    import would be circular — and reading the attribute per request is also
    what lets the test suite point ``main.db`` at a throwaway database.
    """
    from . import main
    return main.db


def _no_store(body: dict) -> JSONResponse:
    """Health data must never be served from a cache — a stale dashboard is a
    lying dashboard — and the payload names host paths, so it stays out of any
    intermediary's store too."""
    return JSONResponse(body, headers={"Cache-Control": "no-store"})


def _raise_for_dashboard_error(e: dashboard.DashboardError):
    """Map the module's typed error to HTTP status — never a raw 500 trace."""
    raise HTTPException(e.status, str(e)) from e


@router.get("")
async def dashboard_summary():
    """The cheap snapshot: everything except the full-scan checks.

    ``collect`` never raises, so this route has no error path of its own — a
    probe that failed comes back as a `fail` finding inside a 200. That is
    deliberate: the page must still render when the box is sick.
    """
    now = asyncio.get_event_loop().time()
    if (_summary_cache["body"] is not None
            and now - _summary_cache["ts"] < SUMMARY_TTL_S):
        return _no_store(_summary_cache["body"])
    body = await dashboard.collect(_db(), deep=False)
    _summary_cache.update(ts=asyncio.get_event_loop().time(), body=body)
    return _no_store(body)


@router.get("/deep")
async def dashboard_deep():
    """The expensive checks: full integrity_check + a fresh blob-store walk.

    Human-triggered only (there is a button); never polled. A second concurrent
    request gets 409 instead of a queue, so a jumpy click can't stack full
    database scans.
    """
    # Test-and-claim in one step (see _DeepGate): checking first and acquiring
    # afterwards let a second caller slip through and queue behind the first.
    if not _deep_gate.try_acquire():
        raise HTTPException(409, "A deep check is already running")
    try:
        body = await dashboard.collect(_db(), deep=True)
    finally:
        _deep_gate.release()
    # The deep pass is strictly more informative than the cached summary — let
    # the next poll serve it rather than immediately regressing the page.
    _summary_cache.update(ts=asyncio.get_event_loop().time(), body=body)
    return _no_store(body)


@router.get("/logs")
async def dashboard_logs(
    lines: int = Query(dashboard.LOG_TAIL_DEFAULT, ge=1,
                       le=dashboard.LOG_TAIL_MAX_LINES),
):
    """Tail of the app log (path from config; LOCAL_CHAT_LOG_FILE overrides).

    A missing or unreadable file is a 200 with ``available: false`` and a reason
    naming where the logs actually are — on a default install they go to
    stdout, and telling the operator that is more useful than a 404.
    """
    try:
        body = await dashboard.tail_log(lines)
    except dashboard.DashboardError as e:
        _raise_for_dashboard_error(e)
        return
    return _no_store(body)
