"""FastAPI application: REST + WebSocket + static serving + OpenClaw bridge.

Run with:  uvicorn app.main:app --host 127.0.0.1 --port 8765
(or use ../run.sh from the project root).
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import fcntl
import io
import ipaddress
import json
import logging
import mimetypes
import os
import re
import secrets
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from collections import OrderedDict, defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple

import httpx
from fastapi import (
    Body,
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.encoders import jsonable_encoder
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import (
    auth,
    avatar_pool,
    avatar_snapshots,
    config,
    dashboard_routes,
    gateway_router,
    gateway_ws,
    harness,
    image_jobs,
    jobs,
    llm_api,
    localview,
    openclaw,
    openclaw_text,
    pool_guard,
    problem,
    reactions,
)
from .config import AVATAR_DIR, FILES_DIR, FRONTEND_DIR, MEDIA_DIR, SETTINGS
from .database import Database, local_date, new_id, now_iso
from .models import (
    MESSAGE_MAX_CHARS,
    DailyThreadIn,
    FireReactionIn,
    GenerateReactionIn,
    ImageJobIn,
    InjectIn,
    MessageOut,
    ReactionPatchIn,
    ReactionSettingsIn,
    ThreadOut,
    UpdateBotOrderIn,
)
from .ws import manager

log = logging.getLogger("local-chat")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# httpx logs every request at INFO — that was a journald write per minute
# per open tab, 24/7. Warnings still pass.
logging.getLogger("httpx").setLevel(logging.WARNING)

db = Database(config.DB_PATH)

# Per-thread locks serialise agent turns; a global semaphore caps concurrent
# subprocesses so we never overwhelm the local models.
_thread_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
_agent_sem = asyncio.Semaphore(SETTINGS.max_concurrency)
_background: set[asyncio.Task] = set()
# Set during lifespan teardown; guards against spawning fresh background work
# (e.g. a post-turn follower) while the loop and DB are shutting down.
_shutting_down = False

# Note: SVG is deliberately excluded — an SVG served inline can carry <script>
# and would execute as a same-origin document (stored XSS). Raster formats only.
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".avif"}
# Video formats (gif-equivalents and general clips). Served with the same
# script-disabling CSP as images, so they can't execute in our origin.
VIDEO_EXTS = {".mp4", ".webm", ".mov", ".m4v", ".ogv", ".ogg", ".mkv", ".avi"}
MEDIA_EXTS = IMAGE_EXTS | VIDEO_EXTS
# Document types for chat reference (ingested for agent access).
DOC_EXTS = {
    ".md", ".txt", ".pdf", ".csv", ".json", ".yaml", ".yml", ".xml",
    ".py", ".js", ".ts", ".sh", ".bash", ".zsh", ".c", ".cpp", ".h", ".hpp",
    ".rs", ".go", ".java", ".kt", ".swift", ".rb", ".php",
    ".html", ".css", ".scss", ".less",
    ".log", ".toml", ".ini", ".cfg", ".conf",
    ".rst", ".tex", ".org", ".adoc",
}
# Text MIME types we serve inline (raw content for agent reading / preview).
_TEXT_MIMES = {
    "text/plain", "text/markdown", "text/x-markdown", "text/csv",
    "text/html", "text/css", "text/x-python", "text/x-script.python",
    "text/javascript", "text/x-typescript", "text/x-sh", "text/x-bash",
    "text/x-csrc", "text/x-c++src", "text/x-go", "text/x-java-source",
    "text/x-rust", "text/x-kotlin", "text/x-swift", "text/x-ruby",
    "text/x-php", "text/x-log",
    "application/json", "application/xml", "application/x-yaml",
    "application/x-toml", "application/javascript", "application/typescript",
}

UPLOAD_MAX_IMAGE = 25 * 1024 * 1024    # 25MB
UPLOAD_MAX_VIDEO = 200 * 1024 * 1024   # 200MB
UPLOAD_MAX_DOC = 50 * 1024 * 1024      # 50MB for documents
# Safe-Mode availability guard: decoy (no-PIN) clients get a modest per-client
# daily upload budget so an unauthenticated LAN/tailnet peer can't fill the
# disk through POST /api/upload. Full sessions are unaffected. Env override is
# for tests. In-memory by design: resets on restart, which is fine for a
# best-effort availability cap.
DECOY_UPLOAD_QUOTA = int(config.env("DECOY_UPLOAD_QUOTA") or (200 * 1024 * 1024))  # 200MB/day/client
# Safe Mode may send messages, and every message to a safe bot spawns an agent
# subprocess — a billed model turn. Without a cap an unauthenticated LAN or
# tailnet peer can run the operator's API bill up indefinitely and fill the DB
# with threads, while _agent_sem only caps CONCURRENCY, not rate. Same shape as
# DECOY_UPLOAD_QUOTA: per-client, per-day, in-memory, resets on restart.
DECOY_TURN_QUOTA = int(config.env("DECOY_TURN_QUOTA", "200"))
DECOY_THREAD_QUOTA = int(config.env("DECOY_THREAD_QUOTA", "50"))
# Server-wide storage ceiling across ALL stored blobs (chat media + File
# Server). Prevents even trusted-LAN uploaders from filling the disk over time.
# Tunable via DISPATCH_FILES_TOTAL_MAX (bytes); 0 disables the cap.
FILES_TOTAL_MAX = int(config.env("FILES_TOTAL_MAX") or (20 * 1024 * 1024 * 1024))  # 20GB total


def _allowed_media_bases() -> list[Path]:
    home = Path.home()
    # Served/ingested media only. Workspace dirs are deliberately NOT included:
    # the normal flow ingests bytes into MEDIA_DIR first (see _normalize_media),
    # so /api/media never needs to read arbitrary agent scratch space. Exposing
    # every agent workspace tree widened the readable surface — any
    # media-extension file there became retrievable (unauthenticated when no PIN
    # is set), so the glob was removed.
    bases = [MEDIA_DIR, home / ".openclaw" / "media", Path("/tmp/openclaw")]
    resolved = []
    for b in bases:
        with contextlib.suppress(OSError):
            if _is_trustworthy_base(b):
                resolved.append(b.resolve())
    return resolved


def _is_trustworthy_base(p: Path) -> bool:
    """Is this directory safe to treat as a readable media root?

    `/tmp/openclaw` sits under a world-writable parent, so on a shared host
    anybody can create it — or replace it with a symlink to `/` — before we
    do, and every media-extension file underneath becomes retrievable through
    /api/media. A base is only honoured when it is a REAL directory (not a
    symlink) owned by the uid we run as. A base that fails the test is simply
    not a base; nothing else changes.
    """
    try:
        st = p.lstat()
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        return False
    if st.st_uid != os.getuid():
        log.warning("ignoring media base %s: owned by uid %s, not %s",
                    p, st.st_uid, os.getuid())
        return False
    return True


# --------------------------------------------------------------------------- #
# Lifespan
# --------------------------------------------------------------------------- #


def _claim_single_instance() -> io.IOBase | None:
    """Take an exclusive lock on the data directory, or refuse to start.

    Sessions, WebSocket clients, rate-limit counters, per-thread locks and the
    background loops are all in-process dictionaries. A second process on the
    same data directory therefore does not scale the app — it silently breaks
    it: half your sockets miss broadcasts, rate limits count to half, and two
    writers race the SQLite WAL and the backup loop.

    The usual way people hit this is `uvicorn --workers 4`, which looks like an
    obvious win and produces symptoms nobody would connect to it. An advisory
    flock is the honest answer: it catches --workers, a double `docker compose
    up`, and a systemd unit racing a hand-started dev server, all with the same
    message. The lock releases automatically if the process dies, so a crash
    never leaves the app unstartable.
    """
    lock_path = config.DATA_DIR / ".instance.lock"
    try:
        fh = lock_path.open("w")
        # lockf (POSIX record locks), NOT flock. The distinction is the whole
        # behaviour: flock locks are owned by the open file DESCRIPTION, so a
        # second lock inside the same process fails — which broke the test
        # suite, where several app instances legitimately share one data dir in
        # one interpreter. POSIX locks are owned by the PROCESS, which is
        # exactly the invariant being enforced ("one process per data dir").
        # Worker processes are forked and do NOT inherit the lock, so
        # `--workers 4` still fails on workers 2-4, which is the point.
        fcntl.lockf(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        # Only a CONTENDED lock means "another instance". Anything else — an
        # unwritable data dir, a full disk, a filesystem with no lock support —
        # is a different problem, and exiting with the wrong explanation sends
        # the operator hunting for a process that was never there.
        if e.errno not in (errno.EACCES, errno.EAGAIN):
            log.warning("Could not take the instance lock at %s (%s). Continuing, "
                        "but make sure only ONE process serves this data dir.",
                        lock_path, e)
            return None
        log.error(
            "Another DisPatch instance is already using %s.\n"
            "  This app keeps sessions, live connections and rate limits in "
            "process memory, so it must run as a SINGLE process.\n"
            "  If you passed --workers, remove it. To run a second instance, "
            "give it its own DISPATCH_DATA_DIR.",
            config.DATA_DIR,
        )
        raise SystemExit(1)
    fh.write(f"{os.getpid()}\n")
    fh.flush()
    return fh


def _warn_if_wide_open() -> None:
    """Say so, loudly, when the app is reachable off-box with no credential.

    With no PIN configured every route is open — which is the right first-run
    experience on a laptop, and a genuinely bad surprise on a machine with a
    port forward. The dashboard reports this as a finding too; this is for the
    operator who only ever reads the startup log.
    """
    if auth.load().pin_set:
        return
    host = config.env("HOST", "127.0.0.1")
    if host in ("127.0.0.1", "localhost", "::1"):
        return
    log.warning(
        "\n"
        "  ================================================================\n"
        "   NO PASSWORD IS SET and DisPatch is listening on %s.\n"
        "   Anyone who can reach this port has full access: every message,\n"
        "   every file, and the ability to delete both.\n"
        "   Open the app and finish setup, or bind to 127.0.0.1.\n"
        "  ================================================================",
        host,
    )


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    config.ensure_dirs()
    # Bound to the app, not a local, to make the lifetime obvious: the flock is
    # held for exactly as long as the process serves, and released by the OS if
    # it dies. Dropping this reference would close the fd and free the lock.
    app.state.instance_lock = _claim_single_instance()
    _warn_if_wide_open()
    config.load_bots()  # materialises config.yaml on first run
    # Reaction pack: renders the starter cards + reactions.yaml on first run.
    # Idempotent and non-fatal — a box without usable fonts just starts empty.
    await asyncio.to_thread(reactions.seed_starter_pack)
    # One-shot pool-layout migration (flat pool/ + manifest → moods/<mood>/
    # folders). Marker-guarded: an already-migrated or brand-new data dir is a
    # cheap no-op, so this runs unconditionally on every start.
    await asyncio.to_thread(reactions.migrate_mood_folders)
    await db.connect()
    # Safe-Mode connections get image/media stripped from every frame; a full
    # connection whose session lapses is redacted from that moment too.
    manager.redactor = redact_for_decoy
    manager.is_session_live = lambda tok: auth.get_session(tok) is not None
    # Lets the manager demote token-less "full" connections (opened while no
    # PIN existed) the moment a PIN is set. auth.load() is mtime-cached.
    manager.pin_set = lambda: auth.load().pin_set
    cfg = auth.load()
    log.info("DisPatch Chat started. data=%s openclaw=%s lock=%s fts=%s",
             config.DATA_DIR, SETTINGS.openclaw_bin,
             "on" if cfg.pin_set else "off", db.fts_ok)
    if not openclaw.cli_available():
        log.warning("openclaw CLI not found at %r — agent replies will fail.",
                    SETTINGS.openclaw_bin)
    # Media/files are symlinks into ~/.openclaw/media — warn loudly if a target
    # is missing, so backups/restores that drop them don't fail silently.
    for d in (config.MEDIA_DIR, config.FILES_DIR):
        if d.is_symlink() and not d.exists():
            log.error("media store symlink target missing: %s -> %s", d, os.readlink(d))
    # One-time: everything already in the chat is not a gap. Must run BEFORE
    # anything that imports from a transcript, or the very act of upgrading
    # re-posts history (7 messages in one thread alone, when measured).
    await _migrate_transcript_seen_backfill()
    # Self-heal: clear crash-stranded 'thinking' threads + recover their replies
    # from the transcript, and verify DB integrity.
    await _startup_recovery()
    # One-time cleanup of scaffolding stored before the sanitizer existed.
    await _migrate_sanitize_stored_messages()
    # Reconcile blob storage: drop partial/orphan uploads, flag missing blobs.
    await _sweep_orphan_blobs()
    # Same idea for the per-thread avatar store: a pinned snapshot whose file
    # has vanished is data loss, and used to show up only as a broken image.
    await _audit_avatar_snapshots()
    # DeepSeek Harness: broadcast headless-job start/end flips to open tabs.
    # add_state_hook is idempotent, so repeated lifespans (tests) don't stack.
    harness.runner.add_state_hook(_harness_state_changed)
    purge_task = asyncio.create_task(_session_purge_loop())
    _track(purge_task)
    backup_task = asyncio.create_task(_backup_loop())
    _track(backup_task)
    # Continuous gateway-chat mirror (Control-UI webchat + agent main sessions).
    global _mirror_task
    _mirror_task = asyncio.create_task(_gateway_mirror_loop())
    _track(_mirror_task)
    watchdog_task = asyncio.create_task(_mirror_watchdog_loop())
    _track(watchdog_task)
    # Filesystem is truth: reconcile the pack registry with the blobs on disk
    # before anything fires (a regen can strand ids — see reactions.heal_pack).
    healed = await asyncio.to_thread(reactions.heal_pack)
    if healed.get("dangling"):
        log.warning("reaction pack heal at startup: %s", healed)
    # Rotating reaction pool: nightly per-mood top-up + low-water refill.
    _track(asyncio.create_task(_reaction_pool_loop()))
    # Adopt or fail out image jobs left mid-render by the last shutdown BEFORE
    # the worker starts, so the sweep never sees a half-state it has to guess at.
    await _resume_image_jobs()
    _track(asyncio.create_task(_image_job_loop()))
    # Backstop for answers every live path missed (see _gap_sweep_loop).
    _track(asyncio.create_task(_gap_sweep_loop()))
    # Native gateway transport. OFF unless DISPATCH_GATEWAY_WS says otherwise,
    # so nothing about how replies arrive changes without someone deciding it.
    await _gateway_ws_start()
    try:
        yield
    finally:
        global _shutting_down
        _shutting_down = True
        harness.runner.remove_state_hook(_harness_state_changed)
        with contextlib.suppress(Exception):
            await harness.runner.shutdown()
        await _gateway_ws_stop()
        # Only OUR loop's tasks: a second app instance (tests open several
        # clients) parks its tasks in this same module-level set, and cancelling
        # a future from another loop raises instead of shutting down cleanly.
        loop = asyncio.get_running_loop()
        mine = [t for t in _background if t.get_loop() is loop]
        for t in mine:
            t.cancel()
        # Let cancelled tasks actually finish their finally-blocks BEFORE the DB
        # closes — otherwise a cancelled turn's cleanup races a closed database.
        await asyncio.gather(*mine, return_exceptions=True)
        _background.difference_update(mine)
        # Fold the WAL back into the main file so the on-disk DB is self-complete
        # for any external backup taken while we're stopped.
        await db.checkpoint("TRUNCATE")
        await db.close()


# The interactive API docs are disabled in this deployment: /openapi.json,
# /docs and /redoc would hand a sessionless caller the complete route + model
# inventory (harness, inject, recovery, reactions…) — exactly the unlocked
# feature surface Safe Mode exists to hide, on a service bound to 0.0.0.0.
#
# The schema itself is still BUILT — it is served, gated, at /api/openapi.json
# for a full session or an on-box machine (see openapi_schema below). An agent
# that has to call this API cold needs the route inventory; a stranger on the
# network still gets nothing.
app = FastAPI(title="DisPatch Chat", version="1.0.0", lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)


@app.exception_handler(StarletteHTTPException)
async def _http_exception_handler(request: Request, exc: StarletteHTTPException):
    """RFC 9457 problem documents for machines; the old body for browsers.

    See app/problem.py for why. In short: a model cannot branch on prose, so
    every refusal a machine receives now carries a stable `code` beside the
    unchanged `detail`. Nothing a browser sees changes shape.
    """
    if exc.status_code >= 400 and _wants_problem_json(request):
        return JSONResponse(
            problem.body(exc.status_code, exc.detail),
            status_code=exc.status_code,
            headers=getattr(exc, "headers", None),
            media_type=problem.MEDIA_TYPE,
        )
    return await http_exception_handler(request, exc)


@app.exception_handler(RequestValidationError)
async def _validation_exception_handler(request: Request, exc: RequestValidationError):
    """Same treatment for FastAPI's own 422s.

    `detail` keeps the full pydantic error list — that is what it always was,
    and it is genuinely the most useful thing in the body — with the problem
    envelope wrapped around it.
    """
    if _wants_problem_json(request):
        return JSONResponse(
            problem.body(422, jsonable_encoder(exc.errors()), code="validation_error"),
            status_code=422,
            media_type=problem.MEDIA_TYPE,
        )
    return await request_validation_exception_handler(request, exc)


def _wants_problem_json(request: Request) -> bool:
    """Only the machine-inbound surface, and only for a non-browser caller.

    Scoped this tightly on purpose. The frontend reads `err.detail` off these
    responses; a problem document keeps that key, but the content type change
    is pointless for a browser and every pointless change to a shipped wire
    format is a chance to break something for nothing.
    """
    try:
        return (_is_inbound(request.method, request.url.path)
                and not _browser_request(request))
    except Exception:                     # a handler must never raise
        return False


@app.middleware("http")
async def media_security_headers(request: Request, call_next):
    """Defense-in-depth: never let a served file execute as an active document.

    Applies a script-disabling CSP + nosniff to anything under /media or
    /api/media, so even an image that slips through (e.g. a mislabeled SVG)
    cannot run script in our origin.
    """
    response = await call_next(request)
    path = request.url.path
    if path.startswith(("/media/", "/api/media", "/api/files", "/api/reactions/")):
        response.headers["Content-Security-Policy"] = "default-src 'none'; sandbox"
        response.headers["X-Content-Type-Options"] = "nosniff"
    # Local Viewer. HTML is the one thing here that is ALLOWED to be an active
    # document (the operator asked to open that page), so it gets a sandbox
    # that runs script under an OPAQUE origin — never allow-same-origin, or the
    # framed page could read this app's DOM, cookies and storage. Everything
    # else gets the inert policy. `frame-ancestors 'self'` on both so a viewer
    # response cannot be framed by another site.
    elif path.startswith(("/local/", "/api/local")):
        html = response.headers.get("content-type", "").startswith("text/html")
        response.headers["Content-Security-Policy"] = (
            "sandbox allow-scripts allow-forms allow-popups allow-modals "
            "allow-downloads; frame-ancestors 'self'" if html
            else "default-src 'none'; sandbox; frame-ancestors 'self'")
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "private, max-age=0, must-revalidate"
        response.headers["X-Robots-Tag"] = "noindex"
    # The app's own shell says in index.html that frame-ancestors is the
    # server's job, and the server did not do it. A header CSP is ADDITIVE to
    # the meta one, so this adds clickjacking protection without touching the
    # meta policy's script/style rules.
    elif path in ("/", "/static/index.html"):
        response.headers.setdefault("Content-Security-Policy", "frame-ancestors 'self'")
    # The HTML entry point and the service worker must ALWAYS revalidate, or the
    # browser heuristically caches a stale index.html (no Cache-Control => it may
    # serve from cache without hitting us) and keeps loading old `?v=` assets — so
    # shipped frontend fixes never reach the tab. `no-cache` = "revalidate every
    # time", which is cheap here because ETag/Last-Modified yield a 304. Versioned
    # assets (main.js?v=N, app.css?v=N) stay freely cacheable: their URL changes
    # when they change, so they don't need this.
    if path == "/" or path in ("/static/sw.js", "/static/index.html",
                               "/manifest.webmanifest", "/static/manifest.webmanifest"):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    # A worker file served from /static/ may only claim a scope at or below
    # /static/ unless it says otherwise. The app lives at /, so without this the
    # SW registered but never controlled the document: controller stayed null,
    # the fetch handler never ran, and the CACHE-bump auto-reload was inert.
    if path == "/static/sw.js":
        response.headers["Service-Worker-Allowed"] = "/"
    return response


_MULTIPART_SLACK = 16 * 1024 * 1024   # boundary/header overhead headroom

# POST re-pins ONE thread's avatar (full image + optional face crop); GET is
# the browser-facing image route with its own decoy gating. Used by both the
# upload ceiling below and the inbound machine-surface allowlist.
_INBOUND_THREAD_AVATAR_RE = re.compile(r"^/api/threads/[^/]+/avatar$")


def _refuse_oversize_part(part: UploadFile, detail: str) -> None:
    """413 on a multipart part whose DECLARED size is already over the image
    cap, before its bytes are pulled into memory. Starlette fills `.size`
    while it spools the part; None means it could not tell, and the read-then-
    measure check behind this one still applies."""
    size = getattr(part, "size", None)
    if isinstance(size, int) and size > UPLOAD_MAX_IMAGE:
        raise HTTPException(413, detail)


def _upload_ceiling(path: str) -> int | None:
    """Per-endpoint hard ceiling on a single upload body, or None if not an
    upload endpoint. This is the LARGEST body the endpoint could ever legitimately
    accept (the finer per-type caps still apply in the handler): /api/upload tops
    out at the video cap; /api/files at the File Server max. Checked against the
    declared Content-Length BEFORE FastAPI buffers, so a genuinely-oversized
    upload is rejected without spooling the body to disk/RAM at all."""
    # /api/drop shares /api/upload's ceiling: both top out at the video cap.
    if path in ("/api/upload", "/api/drop"):
        return UPLOAD_MAX_VIDEO + _MULTIPART_SLACK
    if path == "/api/files":
        return FILE_UPLOAD_MAX + _MULTIPART_SLACK
    if re.match(r"^/api/bots/[^/]+/avatar$", path):
        return UPLOAD_MAX_IMAGE + _MULTIPART_SLACK
    # Same shape as the bot avatar upload — a full image plus an optional
    # pre-cropped face. Without this the thread pin had NO ceiling at all and
    # the whole body was read into RAM before its size was looked at.
    if _INBOUND_THREAD_AVATAR_RE.match(path):
        return UPLOAD_MAX_IMAGE + _MULTIPART_SLACK
    if path == "/api/reactions":
        return reactions.UPLOAD_MAX + _MULTIPART_SLACK
    return None


@app.middleware("http")
async def limit_upload_body(request: Request, call_next):
    """Reject oversized / unbounded uploads before the body is buffered.

    Pairs with the TMPDIR=real-disk service drop-in: UploadFile spools the whole
    body to the temp dir before the handler runs, so (a) this rejects anything
    over the endpoint ceiling up front, and (b) the drop-in ensures whatever DOES
    get spooled lands on disk, never RAM-backed /tmp."""
    if request.method == "POST":
        ceiling = _upload_ceiling(request.url.path)
        if ceiling is not None:
            cl = request.headers.get("content-length")
            if cl is None:
                # No declared size (e.g. chunked): refuse rather than spool an
                # unbounded body.
                if "chunked" in request.headers.get("transfer-encoding", "").lower():
                    return JSONResponse(
                        {"detail": "Content-Length required for uploads"}, status_code=411)
            else:
                try:
                    declared = int(cl)
                except ValueError:
                    return JSONResponse({"detail": "Bad Content-Length"}, status_code=400)
                if declared > ceiling:
                    return JSONResponse({"detail": "File too large"}, status_code=413)
    return await call_next(request)


@app.middleware("http")
async def auth_gate(request: Request, call_next):
    """Apply the Safe-Mode model once a PIN is configured.

    No PIN set → app is fully open (original behaviour). With a PIN set, a valid
    full-session cookie gets everything; anything else is served **Safe Mode**
    (the default): image/media endpoints are blocked here and message bodies are
    redacted downstream. The WebSocket route does its own equivalent check (http
    middleware doesn't run for the ws scope). Added after media_security_headers,
    so it runs OUTERMOST.
    """
    request.state.session = None
    request.state.decoy = False
    request.state.machine = False
    cfg = auth.load()
    if not cfg.pin_set:
        return await call_next(request)

    path = request.url.path
    method = request.method

    # Local Viewer frame tickets: a page framed under `sandbox` runs as an
    # opaque origin and sends NO cookie for its stylesheets/scripts, so this
    # prefix is authenticated by the random ticket in the URL instead (minted
    # by the cookie-bearing stat call, client-bound, expiring). The route is
    # its own lock and fails closed — see localview.local_view.
    if path.startswith(localview.TICKET_PREFIX):
        return await call_next(request)

    # Always-open: app shell, static assets, auth endpoints, health.
    # Exception: avatar images stay behind the session EXCEPT for safe bots,
    # whose pictures Safe Mode is allowed to render (see the per-file check below).
    if (path in _OPEN_EXACT or path.startswith(_OPEN_PREFIXES)
            or path == _AUTH_PREFIX or path.startswith(_AUTH_PREFIX + "/")):
        request.state.session = auth.get_session(request.cookies.get(COOKIE_NAME))
        if request.state.session is None and _gated_avatar_static(path):
            # Safe Mode may still show real pictures for the bots it lists, so
            # serve a safe bot's avatar file even without a session. Anything
            # else avatar-shaped — a non-safe bot's picture, a nested backup
            # path, a sibling copy like avatars.backup-<date>/ — stays behind
            # the PIN (fail closed; see _gated_avatar_static).
            if not _safe_avatar_static(path):
                return JSONResponse({"detail": "Unlock for full access", "decoy": True}, status_code=403)
        return await call_next(request)

    # OpenClaw inbound: API key instead of a session. Loopback callers (local
    # agents/crons on 127.0.0.1/::1) are always exempt so on-box automation
    # keeps working tokenless. Remote (LAN/tailnet) callers MUST present the
    # configured api_token — and when no token is configured they are refused
    # outright (fail closed) rather than allowed to inject messages.
    #
    # Browser-shaped requests never take the machine branch: several inbound
    # paths (threads, messages, unread) double as the locked frontend's own
    # API, and a REMOTE Safe-Mode tab must keep its decoy view instead of
    # being told to present an API key. Falling through also marks the request
    # decoy for the per-route guards — the same agents-yes/locked-tab-no split
    # _deny_agent_route_to_browser enforces, decided once, here.
    if _is_inbound(method, path) and not _browser_request(request):
        # The image server's completion callback carries its own credential —
        # the per-job token DisPatch generated and handed to the rig at submit
        # time — and it arrives from another machine, so the api_token check
        # below would refuse every one of them (the rig has no api_token, and
        # giving it one would hand a GPU box a key to the whole inbound
        # surface). The handler verifies the token in constant time and
        # DISCARDS the body: a valid callback is a poke to re-poll one job,
        # never content. Worst case for a forged one is a wasted get_job.
        if method == "POST" and _INBOUND_IMAGE_JOB_CALLBACK_RE.match(path):
            request.state.machine = True
            return await call_next(request)
        # A logged-in full session may always use these endpoints (e.g. a family
        # member driving the app over the tailnet) — check that first.
        sess = auth.get_session(request.cookies.get(COOKIE_NAME))
        if sess is not None:
            request.state.session = sess
            auth.touch_session(sess.token)
            return await call_next(request)
        # A reverse proxy in front of us (a tailnet Serve terminates TLS and
        # forwards from loopback) makes remote callers appear local, which would
        # silently skip the token check. Any forwarding / Serve-identity header
        # proves the request did NOT originate from an on-box process, so force
        # the token even when the socket peer is 127.0.0.1. Both tests live in
        # _loopback_socket/_proxied_request so the health endpoint's machine
        # branch cannot drift from this one.
        _is_loopback = _loopback_socket(request)
        _proxied = _proxied_request(request)
        if not _is_loopback or _proxied:
            key = request.headers.get("x-api-key") or _bearer(request) or ""
            if not cfg.api_token or not auth.verify_api_token(key):
                # Dual-use GETs: these feed the locked UI's own rendering AND
                # the machine surface. On a plain-HTTP origin (LAN IP — not a
                # trustworthy context) a browser attaches NO Sec-Fetch-* and
                # no Origin to same-origin GETs, so a locked tab's reads are
                # indistinguishable from a remote machine here. A keyless
                # remote GET therefore degrades to the decoy view (exactly
                # what it got before these routes joined the inbound surface)
                # rather than 401. Mutations are unaffected: browsers attach
                # Origin to every non-GET request, so those tabs never reach
                # this branch — and a keyless machine mutation stays
                # fail-closed.
                if method == "GET" and (
                        path in ("/api/reactions", "/api/bots", "/api/threads",
                                 "/api/unread")
                        or _INBOUND_THREAD_ONE_RE.match(path)
                        or _INBOUND_THREAD_SUB_RE.match(path)):
                    request.state.decoy = True
                    return await call_next(request)
                return JSONResponse({"detail": "Invalid or missing API key"}, status_code=401)
        # An authenticated MACHINE caller (on-box loopback, or a remote holding
        # the api_token). Recorded so an in-handler guard can tell it apart
        # from "sessionless, therefore Safe Mode" — see _require_full_access.
        request.state.machine = True
        return await call_next(request)

    # Full session → everything (and slide the idle window). Otherwise Safe Mode.
    sess = auth.get_session(request.cookies.get(COOKIE_NAME))
    if sess is not None:
        request.state.session = sess
        auth.touch_session(sess.token)
        return await call_next(request)
    request.state.decoy = True
    if _decoy_blocked(method, path):
        return JSONResponse({"detail": "Unlock for full access", "decoy": True}, status_code=403)
    return await call_next(request)


def _task_done(task: asyncio.Task) -> None:
    _background.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        # A background loop dying must never be silent: the 2026-07-29 mirror
        # death produced zero journal lines because nothing retrieved the task
        # exception.
        log.error("background task %r died", task.get_name(), exc_info=exc)


def _track(task: asyncio.Task) -> None:
    _background.add(task)
    task.add_done_callback(_task_done)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _truncate(text: str, n: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


_MEDIA_DIRECTIVE_RE = re.compile(r"\[\[media:([^\]|]+)(\|[^\]]*)?\]\]")
_DOC_REF_RE = re.compile(r"\[\[doc:([^\]|]+)(?:\|([^\]]*))?\]\]")
_INGEST_MAX = 500 * 1024 * 1024  # 500MB safety cap


# --------------------------------------------------------------------------- #
# Where an ingested picture CAME FROM.
#
# Ingest copies the bytes out of the agent's scratch file and serves them from
# /media/<uuid> — after which the original is free to disappear, and it does:
# agents write batches to /tmp/<something>/1.png and overwrite or delete them
# minutes later. Two things then went wrong at once (live, 2026-08-10):
#
#   * dedup lost the thread. The canonical key bridged "/tmp/x.png" and
#     "/media/<uuid>.png" by HASHING THE BYTES AT BOTH ENDS, which stops working
#     the moment one end is deleted — so the gap sweep decided a delivered reply
#     was missing and re-imported it, every ten minutes, forever.
#   * the re-imported copy could no longer find /tmp/x.png, so each picture in
#     it degraded to "🖼️ *(image unavailable: …)*" — even though the bytes were
#     sitting in /media the whole time.
#
# Recording origin→served at ingest fixes both: the key is a stable string
# instead of a file read, and a directive whose source is gone resolves to the
# copy we already made. The file is a cache, not a source of truth — losing it
# costs a re-copy and (at worst) one duplicate, never a picture.
# --------------------------------------------------------------------------- #

_media_origins: dict[str, str] | None = None      # served URL -> original path
_media_origins_lock = threading.Lock()


def _media_origins_path() -> Path:
    return config.DATA_DIR / "media-origins.json"


def _media_origins_reset() -> None:
    """Drop the in-process cache (tests, and after a DATA_DIR change)."""
    global _media_origins
    with _media_origins_lock:
        _media_origins = None


def _media_origins_all() -> dict[str, str]:
    global _media_origins
    if _media_origins is None:
        try:
            data = json.loads(_media_origins_path().read_text())
            _media_origins = {str(k): str(v) for k, v in data.items()} \
                if isinstance(data, dict) else {}
        except (OSError, ValueError):
            _media_origins = {}
    return _media_origins


def _media_origin_of(url: str) -> str | None:
    """The path a served picture was copied from, if we recorded it."""
    with _media_origins_lock:
        return _media_origins_all().get(url)


def _media_served_for(src: str) -> str | None:
    """The served copy of a source path — only if that copy still exists."""
    with _media_origins_lock:
        origins = _media_origins_all()
        # Newest wins: an agent that reuses a scratch filename for a second
        # picture re-ingests it, and the later entry is the current meaning of
        # that path. Live sources never reach here (they are re-ingested
        # directly), so this only ever answers for a path already deleted.
        for url, origin in reversed(list(origins.items())):
            if origin == src:
                return url if (MEDIA_DIR / url[len("/media/"):]).is_file() else None
    return None


def _media_origins_record(url: str, src: str) -> None:
    if not url.startswith("/media/") or not src:
        return
    with _media_origins_lock:
        origins = _media_origins_all()
        if origins.get(url) == src:
            return
        origins.pop(url, None)          # re-insert so ordering stays newest-last
        origins[url] = src
        try:
            path = _media_origins_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(origins, indent=0))
            tmp.replace(path)           # atomic: a torn file would read as empty
        except OSError:
            log.warning("could not record media origin for %s", url, exc_info=True)


def _ingest_deny_roots() -> tuple[Path, ...]:
    home = Path.home()
    roots = [home / ".ssh", home / ".gnupg", home / ".config" / "secrets",
             home / ".openclaw" / "secrets", home / ".openclaw" / "agents",
             Path("/etc"), Path("/proc"), Path("/sys"), Path("/dev")]
    out = []
    for r in roots:
        with contextlib.suppress(OSError, RuntimeError):
            out.append(r.resolve())
    return tuple(out)


_INGEST_DENY_ROOTS = _ingest_deny_roots()


def _ingest_local_file(path_str: str) -> str | None:
    """Copy a local media file into MEDIA_DIR; return its served /media/ URL.

    This makes the image/video pipeline robust: agents can reference ANY
    readable local path (not just allow-listed dirs), and the message keeps
    working even if the original file is later moved or deleted.
    """
    try:
        p = Path(path_str).expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if not p.is_file() or p.suffix.lower() not in MEDIA_EXTS:
        return None
    if any(root == p or root in p.parents for root in _INGEST_DENY_ROOTS):
        # A directive is text anyone with send rights can write; the media
        # extension check already keeps keys out, but nothing under these
        # trees is ever a picture to post, so refuse the whole subtree.
        return None
    try:
        if p.stat().st_size > _INGEST_MAX:
            return None
        if MEDIA_DIR.resolve() in p.parents:   # already in the served store
            # Keep any subdirectory components — StaticFiles serves nested
            # paths, and flattening to p.name 404s for e.g. /media/feed/x.png.
            url = f"/media/{p.relative_to(MEDIA_DIR.resolve())}"
            # Record the origin for this branch too. It skips the copy, but the
            # DIRECTIVE is still rewritten (abs path -> /media/<rel>), and the
            # dedup key bridges that rewrite through the origins ledger — with
            # no entry, the transcript side said src:<abs> while the stored
            # side said src:/media/<rel>, and the reply re-posted once on the
            # sweep's first visit. Same drift as the deleted-scratch-file one,
            # reached whenever an agent re-sends a picture by its full path.
            _media_origins_record(url, path_str)
            return url
        name = f"{uuid.uuid4().hex}{p.suffix.lower()}"
        MEDIA_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(p, MEDIA_DIR / name)
        url = f"/media/{name}"
        _media_origins_record(url, path_str)
        return url
    except OSError:
        return None


def _ingest_content_media(content: str) -> str:
    """Rewrite [[media:/local/path|cap]] directives to served /media/ URLs.

    A directive whose source file is gone but which we ingested EARLIER keeps
    its picture: it resolves to the copy already in /media. Only a path we have
    never held bytes for — a hallucinated one, or a genuine gap first seen after
    the file died — becomes a visible "unavailable" note rather than a broken
    image.

    CODE IS SKIPPED — a directive inside backticks is someone explaining the
    syntax. An agent's own write-up of how to send pictures came out with its
    example rewritten to "(image unavailable: path)", which reads as the
    feature being broken in the very message that documents it.
    """
    if not content or "[[media:" not in content:
        return content

    def repl(m: re.Match) -> str:
        path, cap = m.group(1).strip(), m.group(2) or ""
        if path.startswith("file://"):
            path = path[len("file://"):]
        if path.startswith("~"):
            # A `~` directive used to fall through every branch below — not
            # ingested, not noted as unavailable — and reach the browser
            # verbatim, where it rendered as a RELATIVE url and 404'd.
            # _media_fingerprint already expands `~`, so the dedup key and the
            # ingested origin agree on the expanded spelling.
            with contextlib.suppress(RuntimeError):
                path = str(Path(path).expanduser())
        if path.startswith("/") and not path.startswith(("/media/", "/static/", "/api/")):
            url = _ingest_local_file(path)
            if url:
                return f"[[media:{url}{cap}]]"
            prior = _media_served_for(path)
            if prior:
                return f"[[media:{prior}{cap}]]"
            if not Path(path).expanduser().is_file():
                label = (cap[1:].strip() if cap else "") or Path(path).name
                return f"🖼️ *(image unavailable: {label})*"
        return m.group(0)

    return openclaw_text.sub_outside_code(
        content, lambda seg: _MEDIA_DIRECTIVE_RE.sub(repl, seg))


# --------------------------------------------------------------------------- #
# Media salvage + delivery watchdog
#
# Local models (large ones especially) keep "delivering" images in formats
# DisPatch doesn't render: dead-text `MEDIA:/path`, markdown images pointing at
# local files, or bare file paths — or they claim "here's the image" with no
# reference at all. Two defenses, both server-side and model-agnostic:
#   1. _salvage_media_refs(): deterministically rewrite malformed references
#      into [[media:...]] directives before ingest (applied to every persisted
#      assistant message: agent turns, follow-ups, and /api/inject).
#   2. A post-turn "second look" (in run_agent_turn): if the reply CLAIMS media
#      but nothing renderable was attached, send one automated corrective turn
#      into the same session asking the agent to re-emit proper directives.
# --------------------------------------------------------------------------- #

_MEDIA_EXT_GROUP = "|".join(e.lstrip(".") for e in sorted(MEDIA_EXTS))
_SALVAGE_RE = re.compile(
    # markdown image whose URL is a local path: ![cap](/path/file.jpg)
    r"!\[(?P<alt>[^\]]*)\]\(\s*(?P<md>(?:file://)?(?:~/|/)[^)\s]+)\s*\)"
    # dead-text form the model hallucinates: MEDIA:/path/file.jpg — requires a
    # media extension so prose like "social media: /r/pics" can't false-match
    rf"|MEDIA:\s*(?P<dead>(?:file://)?(?:~/|/)[^\s\"'|\])]+\.(?:{_MEDIA_EXT_GROUP}))\b"
    # bare local path with a media extension (only wrapped if the file exists)
    rf"|(?P<bare>(?:~/|/)[\w][\w./@%+-]*\.(?:{_MEDIA_EXT_GROUP}))\b",
    re.IGNORECASE,
)
_STASH_RE = re.compile("\x00(\\d+)\x00")


def _salvage_media_refs(content: str, *, assume_files_exist: bool = False) -> str:
    """Rewrite malformed media references in assistant text to [[media:...]].

    Existing [[media:...]] directives are protected (stashed) so their paths
    are never re-matched. Bare paths are only wrapped when the file actually
    exists — a path merely *mentioned* in prose stays plain text.

    CODE IS SKIPPED, for the same reason it is in the ingest below: a path in a
    fenced block or backticks is being SHOWN, not sent. Salvaging it turned a
    shell command in an explanation into an image.

    ``assume_files_exist`` is for :func:`_canon_msg` ONLY. That existence check
    is disk state inside a dedup key — the exact disease the byte-hash had: a
    bare-path reply was wrapped at persist (file alive), the agent wiped its
    scratch dir, and every later canon of the RAW text declined to wrap, so
    the key no longer matched its own stored copy and a redundant path
    re-posted the reply. A canonical key is a comparison, not a display —
    wrapping a merely-mentioned path there is harmless as long as BOTH sides
    do it, and it makes the key a pure function of the text again.
    """
    if not content or ("/" not in content and "~" not in content):
        return content

    stash: list[str] = []

    def _protect(m: re.Match) -> str:
        stash.append(m.group(0))
        return f"\x00{len(stash) - 1}\x00"

    s = _MEDIA_DIRECTIVE_RE.sub(_protect, content)

    def _fix(m: re.Match) -> str:
        def clean(p: str) -> str:
            p = p.strip().rstrip(".,;:!?'\")]")
            if p.startswith("file://"):
                p = p[len("file://"):]
            # _ingest_content_media only handles absolute paths — expand ~ here.
            if p.startswith("~"):
                p = str(Path(p).expanduser())
            return p

        if m.group("md") is not None:
            path = clean(m.group("md"))
            if path.startswith(("/media/", "/static/", "/api/")):
                return m.group(0)          # already a served URL — leave it
            if Path(path).expanduser().suffix.lower() not in MEDIA_EXTS:
                return m.group(0)
            cap = (m.group("alt") or "").strip()
            return f"[[media:{path}{'|' + cap if cap else ''}]]"
        if m.group("dead") is not None:
            return f"[[media:{clean(m.group('dead'))}]]"
        path = clean(m.group("bare"))
        if assume_files_exist or Path(path).expanduser().is_file():
            return f"[[media:{path}]]"
        return m.group(0)

    s = openclaw_text.sub_outside_code(s, lambda seg: _SALVAGE_RE.sub(_fix, seg))
    return _STASH_RE.sub(lambda m: stash[int(m.group(1))], s)


# Verbiage that promises media. Checked against the persisted (post-salvage)
# text, so it only fires when the promise is genuinely unbacked.
_MEDIA_CLAIM_RE = re.compile(
    r"\bhere (?:are|is|'s) (?:\w+[ ,]){0,4}(?:image|picture|pic|photo|video|gif|art)s?\b"
    r"|\b(?:image|picture|pic|photo|video|gif)s? (?:is |are )?"
    r"(?:attached|below|incoming|ready|posted|delivered|coming right up)\b"
    r"|\b(?:found|grabbed|got|pulled|generated|made|created|posted|sent|"
    r"attached|delivering|uploaded) (?:\w+[ ,]){0,4}(?:image|picture|pic|photo|video|gif|art)s?\b"
    r"|\blet me show you what i found\b"
    r"|\btake a look at (?:these|those)\b"
    r"|\bfresh batch\b"
    r"|\bshow(?:ing)? you (?:\w+ ){0,3}(?:image|pic|photo|video)s?\b",
    re.IGNORECASE,
)
# "couldn't find any images" must NOT count as a claim.
_MEDIA_NEGATION_RE = re.compile(
    r"\b(?:no|couldn't|could not|can't|cannot|didn't|did not|unable to|failed to|"
    r"won't|will not)\b[^.!?\n]{0,40}\b(?:image|picture|pic|photo|video|gif|file)s?\b",
    re.IGNORECASE,
)
# Marker _ingest_content_media leaves when a directive pointed at a phantom path.
_MEDIA_UNAVAILABLE_MARK = "(image unavailable"

_MEDIA_RECHECK_PROMPT = (
    "SYSTEM MEDIA CHECK (automated — the user did not write this): your previous "
    "reply indicated you were sharing images/videos, but no media actually rendered "
    "in the chat. If you have real files, re-send them now, one per line, exactly:\n"
    "[[media:/absolute/path/to/file.jpg|short caption]]\n"
    "Use the exact local file paths from your tool output (e.g. files saved under "
    "~/.openclaw/media/). Never write MEDIA:/path — that renders as dead "
    "text. Do not invent paths. If there are no actual files, reply with one short "
    "sentence telling the user the images are not available."
)


def _claims_media(text: str) -> bool:
    return bool(text) and bool(_MEDIA_CLAIM_RE.search(text)) \
        and not _MEDIA_NEGATION_RE.search(text)


# OpenClaw's reply-suppression token. Local models sometimes APPEND it to real
# text instead of replying with it alone, and it then renders as literal chat
# text ("...enjoy! NO_REPLY"). Strip standalone-line occurrences; a reply that
# was ONLY the token becomes empty and callers skip persisting it.
_NO_REPLY_RE = re.compile(r"^[ \t]*NO_REPLY[.!]?[ \t]*$\n?", re.MULTILINE)


def _strip_no_reply(text: str) -> str:
    if not text or "NO_REPLY" not in text:
        return text
    return _NO_REPLY_RE.sub("", text).strip()


# OpenClaw's native quote/reply directive: the runtime injects
# "Native quote/reply: first token [[reply_to_current]]" into every agent's
# system prompt, and some local models emit that token literally as the
# first token of a reply. DisPatch has no parser for it, so it would render
# as visible chat text. Strip a LEADING directive token — current-reply or
# an explicit [[reply_to:<id>]] — plus optional surrounding whitespace: it
# is a rendering instruction, never content.
_REPLY_TO_CURRENT_RE = re.compile(r"^\s*\[\[reply_to_current\]\]\s*")
_REPLY_TO_ID_RE = re.compile(r"^\s*\[\[reply_to:[^\]]+\]\]\s*")


def _strip_reply_directive(text: str) -> str:
    if not text or "[[" not in text:
        return text
    return _REPLY_TO_ID_RE.sub("", _REPLY_TO_CURRENT_RE.sub("", text)).strip()


# The gateway WebSocket transport redacts quote/reply directives ANYWHERE in
# the text (unanchored, case-insensitive) before its copy is delivered, while
# the transcript files the legacy tail paths read keep the token verbatim. The
# same reply therefore arrives as two genuinely different strings — the WS
# copy without the token, the tail copy with it — and text-based dedup keys
# treated them as distinct messages: both posted. Mirror the gateway's regex
# exactly so canonicalisation erases the divergence and the two copies key
# equal. (The anchored leading-only strip above stays: it fixes the separate
# visible-token-at-start rendering bug, and is what the persist paths run.)
_REPLY_DIRECTIVE_ANYWHERE_RE = re.compile(
    r"\[\[\s*(?:reply_to_current|reply_to\s*:\s*[^\]\n]+)\s*\]\]",
    re.IGNORECASE)


def _strip_reply_directive_anywhere(text: str) -> str:
    if not text or "[[" not in text:
        return text
    return _REPLY_DIRECTIVE_ANYWHERE_RE.sub("", text).strip()


def _normalize_media(media: str | None) -> str | None:
    """Turn an agent-provided media reference into a URL the browser can load."""
    if not media or not isinstance(media, str):
        return None
    if media.startswith(("http://", "https://", "/media/", "/static/", "/api/media", "data:")):
        return media
    if media.startswith("file://"):
        media = media[len("file://"):]
    if media.startswith("~"):
        # Same rule as the [[media:...]] ingest: a `~` reference left verbatim
        # reaches the browser as a relative URL and 404s.
        with contextlib.suppress(RuntimeError):
            media = str(Path(media).expanduser())
    if media.startswith("/"):
        # Local path — ingest into the served store (robust), else fall back
        # to the allow-listed live endpoint.
        ingested = _ingest_local_file(media)
        if ingested:
            return ingested
        from urllib.parse import quote
        return f"/api/media?path={quote(media)}"
    return media


# --------------------------------------------------------------------------- #
# Auth / lock helpers (the model lives in auth.py)
# --------------------------------------------------------------------------- #

COOKIE_NAME = "lc_session"

# Inline markdown image, e.g. ![alt](url) — stripped (with [[media:...]]) from
# any text a decoy ("safe view") session would otherwise see.
_IMG_MD_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")


def _strip_media_text(s: str | None) -> str | None:
    if not s:
        return s
    s = _IMG_MD_RE.sub("", s)
    s = _MEDIA_DIRECTIVE_RE.sub("", s)
    s = _DOC_REF_RE.sub("", s)
    return s


# Inline-image URL capture (Safe-Mode upload allowlist uses the captured URL).
_IMG_MD_URL_RE = re.compile(r"!\[[^\]]*\]\(([^)]*)\)")


def _decoy_keep_uploaded_media(s: str | None) -> str | None:
    """Safe-Mode SEND filter: keep store-resident upload refs, drop local paths.

    The composer "＋" uploads through /api/upload first, so its references always
    point INTO the served store (/media/<uuid> for images/videos, [[doc:<id>]]
    for files). Those are safe to deliver — they reach the agent and unlocked
    devices, while the decoy DISPLAY still fully redacts media (deniability
    preserved, images stay hidden in the locked view). A directive that points
    at a local filesystem path (someone TYPING [[media:/home/secret.png]]) is
    dropped: Safe Mode must never let an unauthenticated sender copy arbitrary
    local files into the served store. [[doc:<id>]] refs are kept as-is.
    """
    if not s:
        return s
    s = _IMG_MD_URL_RE.sub(
        lambda m: m.group(0) if m.group(1).strip().startswith("/media/") else "", s)
    s = _MEDIA_DIRECTIVE_RE.sub(
        lambda m: m.group(0) if m.group(1).strip().startswith("/media/") else "", s)
    return s


def _redact_message_dict(m: dict) -> dict | None:
    """The locked-view copy of one message, or ``None`` to hide it entirely.

    Locked DisPatch is a family-safe afterthought, not a second product: the
    only hard requirement is that nothing — a prompt, a caption, a rig error,
    a filesystem path — leaks to an unauthenticated viewer. Beyond that, the
    cheapest correct answer for something Safe Mode has no real use for is to
    make it not exist there, not to hand-craft a degraded version of it. An
    image-job placeholder is exactly that case: it names its own prompt in
    `content`, carries the prompt/caption/rig-error/workflow/seed in
    `metadata`, and Safe Mode has no bot that can fire one today anyway (that
    is a config flag, not a structure, so the hide cannot depend on it staying
    true) — so it is simply not shown, at any stage of its life.
    """
    out = dict(m)
    out["media_url"] = None
    if out.get("content"):
        out["content"] = _strip_media_text(out["content"])
    # A reaction trace names an image. If that reaction isn't flagged safe, the
    # locked view keeps the fact that *someone reacted* but loses which one —
    # no name, no id, so nothing points at an image it may not fetch.
    meta = out.get("metadata")
    if isinstance(meta, dict) and meta.get("kind") == "reaction" and not meta.get("reaction_safe"):
        out["metadata"] = {k: v for k, v in meta.items()
                           if k not in ("reaction_id", "reaction_name")}
        actor = str(meta.get("actor") or "Someone")
        out["content"] = f"⚡ {actor} reacted"
    elif isinstance(meta, dict) and meta.get("kind") == _IMAGE_JOB_KIND:
        return None
    return out


#: The `metadata.kind` an image-job placeholder carries — hidden outright from
#: a locked device, see `_redact_message_dict`.
_IMAGE_JOB_KIND = "image_job"


def _redact_thread_dict(t: dict) -> dict:
    out = dict(t)
    if out.get("last_message"):
        out["last_message"] = _strip_media_text(out["last_message"])
    return out


def _safe_bot_ids() -> set[str]:
    """Bots flagged for Safe Mode (visible there without a PIN). Cheap: the
    bot registry is mtime-cached in config.load_bots()."""
    return {b.id for b in config.load_bots() if b.safe and b.visible}


def _safe_avatar_files() -> set[str]:
    """Avatar filenames belonging to safe bots. These specific files may be
    served to a Safe-Mode (decoy) session so the locked view can render the same
    pictures it already lists — non-safe bots' avatars stay behind the PIN."""
    return {b.avatar for b in config.load_bots() if b.safe and b.visible}


# thread_id -> bot_id, so the (synchronous) redactor can scope broadcast frames
# to safe bots. Populated by the async call sites before they broadcast.
_thread_bot: dict[str, str] = {}


async def _bot_of_thread(thread_id: str) -> str | None:
    bot = _thread_bot.get(thread_id)
    if bot is None:
        t = await db.get_thread(thread_id)
        if t:
            bot = _thread_bot[thread_id] = t.bot_id
    return bot


def _frame_bot(frame: dict) -> str | None:
    """Best-effort bot attribution for a frame (for Safe-Mode scoping)."""
    if frame.get("bot_id"):
        return frame["bot_id"]
    th = frame.get("thread")
    if isinstance(th, dict) and th.get("bot_id"):
        return th["bot_id"]
    tid = frame.get("thread_id")
    if tid:
        return _thread_bot.get(tid)
    return None


# Every frame type a Safe-Mode connection may receive AT ALL. This list is an
# ALLOWLIST on purpose: the redactor used to end in `return frame`, so a frame
# type nobody had thought about was delivered verbatim. That is not theoretical
# — `avatar_pool` shipped exactly that way and put non-safe bot ids, absolute
# data-dir paths and rig error strings onto locked family devices. Default-deny
# means the next new frame is silent for Safe Mode until someone decides
# otherwise, which is the correct direction for a fail-closed tier.
#
# stream_start / stream_chunk / turn_status ARE here now, and the reason they
# were not is the reason they are safe to be: a media directive straddling two
# chunks made per-chunk stripping leak. The live transport never strips a
# chunk — it sanitizes the gateway's CUMULATIVE text and sends the difference,
# so a directive is whole before it is ever looked at, and a half-written one
# is held back until it completes (see _sanitize_delta). They are bot-scoped
# exactly like `message`: a locked device sees a safe bot's reply arrive as it
# is typed, and learns nothing at all about any other bot.
#
# Deliberately NOT here, and why:
#   progress                     raw reply text, tool args, file paths
#   harness_state                the DeepSeek Harness pane is full-session
#                                only — Safe Mode must not learn a job exists
#   reaction_pool / avatar_pool  pool telemetry: batch sizes, prompts, rig
#                                errors, absolute paths, every bot's id
#   message_update               a message that was already delivered, rewritten
#                                in place (an image job's placeholder becoming
#                                the picture, or the ⚠️ line). Allowed because
#                                it carries exactly a `message` frame's payload
#                                and is redacted by exactly the same rules —
#                                the bot must be safe, and the media is stripped
#                                from the copy a locked device receives. A
#                                locked device that legitimately saw the
#                                placeholder must see it stop saying "pending",
#                                or Safe Mode is where failures go to hide.
_DECOY_FRAME_ALLOW = frozenset({
    "hello", "bots", "locked", "ack", "pong", "error",
    "message", "message_update", "stream_done", "message_deleted", "thinking",
    "stream_start", "stream_chunk", "turn_status",
    "thread_update", "thread_created", "thread_deleted", "checklist_update",
    "threads_list", "threads", "messages", "reaction",
})


def redact_for_decoy(frame: dict):
    """Make a frame safe for a Safe-Mode connection.

    Returns a redacted copy, the frame unchanged, or ``None`` to DROP it.

    - A frame type not in :data:`_DECOY_FRAME_ALLOW` is dropped outright.
    - Frames about bots NOT flagged safe are dropped entirely — Safe Mode must
      not even learn those conversations exist.
    - Bot-list frames are filtered down to the safe set.
    """
    t = frame.get("type")
    if t not in _DECOY_FRAME_ALLOW:
        return None

    safe = _safe_bot_ids()

    # A reaction overlay is an image pushed onto the screen, so it obeys the
    # same rule as any other media: Safe Mode sees it only when the reaction is
    # explicitly flagged safe AND (if it is attributed at all) it came from a
    # safe bot's thread. Fail closed — an unflagged reaction simply doesn't
    # exist for a locked device.
    if t == "reaction":
        if not frame.get("safe"):
            return None
        bot = _frame_bot(frame)
        if bot is not None and bot not in safe:
            return None
        return frame

    if t in ("hello", "bots") and isinstance(frame.get("bots"), list):
        return {**frame, "bots": [b for b in frame["bots"]
                                  if isinstance(b, dict) and b.get("id") in safe]}

    # Scope per-thread frames to safe bots. For content-bearing frames an
    # unknown attribution drops the frame (safe default). Plain "error" frames
    # are only dropped when they're attributed to an unsafe bot — validation
    # errors with no bot context must still reach the requester.
    if t in ("message", "message_update", "stream_done", "thread_update",
             "thread_created", "thread_deleted", "thinking", "message_deleted",
             "checklist_update", "stream_start", "stream_chunk", "turn_status"):
        bot = _frame_bot(frame)
        if bot not in safe:
            return None
    if t == "error":
        bot = _frame_bot(frame)
        if bot is not None and bot not in safe:
            return None

    if (t in ("message", "message_update", "stream_done")
            and isinstance(frame.get("message"), dict)):
        redacted = _redact_message_dict(frame["message"])
        # An image-job placeholder redacts to nothing (see
        # _redact_message_dict) — the whole frame is dropped, same as an
        # unsafe bot's frame above, rather than delivered with a null message.
        if redacted is None:
            return None
        return {**frame, "message": redacted}
    if t in ("thread_update", "thread_created") and isinstance(frame.get("thread"), dict):
        return {**frame, "thread": _redact_thread_dict(frame["thread"])}
    if t in ("threads_list", "threads", "messages"):
        key = "messages" if t == "messages" else "threads"
        items = frame.get(key)
        if isinstance(items, list):
            fn = _redact_message_dict if t == "messages" else _redact_thread_dict
            kept = []
            for x in items:
                if not isinstance(x, dict):
                    kept.append(x)
                    continue
                r = fn(x)
                if r is not None:      # an image-job row hides outright
                    kept.append(r)
            return {**frame, key: kept}
    return frame


def _session_of(request: Request):
    return getattr(request.state, "session", None)


def _is_decoy(request: Request) -> bool:
    """True when this request is being served Safe Mode (no full session)."""
    return bool(getattr(request.state, "decoy", False))


def _browser_request(request: Request) -> bool:
    """Did this request come from a page in a browser tab?

    Every modern browser stamps Sec-Fetch-* on fetch/XHR, and our own frontend
    sends Origin on its JSON POSTs. Agents, curl and cron send neither. Used
    only to keep a session-exempt endpoint from handing a *browser* the
    machine-to-machine bypass — never to grant anything.
    """
    return bool(request.headers.get("sec-fetch-site") or request.headers.get("origin"))


def _proxied_request(request: Request) -> bool:
    """Did this request pass through a reverse proxy to get here?

    Presence-based, and the same three headers the auth gate uses: a proxy in
    front of us (TLS terminator, tailnet Serve) makes a REMOTE caller's socket
    peer look like 127.0.0.1, so "the peer is loopback" only means "on this
    box" once no forwarding header is present. Forging one of these on a
    genuine local call can only tighten a check, never loosen one.
    """
    return any(request.headers.get(h) for h in _PROXY_MARKER_HEADERS)


# Every header a reverse proxy in front of us is known to stamp. The auth gate
# used to look at three of these; a proxy that sets only `X-Real-IP` or the
# `Tailscale-User-*` identity headers (Serve does, for tailnet callers) left a
# REMOTE request looking like a loopback one. Presence is all that is read.
_PROXY_MARKER_HEADERS = (
    "x-forwarded-for", "forwarded", "x-real-ip", "x-forwarded-host",
    "x-forwarded-proto", "via", "tailscale-headers-info",
    "tailscale-user-login", "tailscale-user-name", "tailscale-user-profile-pic",
)


def _ip_is_loopback(host: str) -> bool:
    """Is this peer address this machine? (Address only — no header input.)"""
    return bool(host) and (host in ("127.0.0.1", "::1", "::ffff:127.0.0.1")
                           or host.startswith("127."))


def _loopback_socket(request: Request) -> bool:
    """Is the peer on this machine? See _proxied_request for the caveat."""
    return _ip_is_loopback(request.client.host if request.client else "")


def _on_box_machine(request: Request) -> bool:
    """An on-box process (not a browser tab) calling us over loopback.

    The same three-part test the machine branch of the auth gate applies:
    loopback socket, no proxy in front, and no browser fingerprint. It grants
    nothing on its own — callers use it to decide whether a response may carry
    operator detail that a locked browser tab must not see.
    """
    return (_loopback_socket(request) and not _proxied_request(request)
            and not _browser_request(request))


def _bearer(request: Request) -> str | None:
    h = request.headers.get("authorization") or ""
    return h[7:].strip() if h.lower().startswith("bearer ") else None


# Path classification for the auth gate.
# Past the gate, but NOT ungated: /api/health and /api/openapi.json both decide
# for themselves what a given caller may see (liveness for anyone, detail for a
# full session or an on-box machine). Listing them here only means the gate
# does not answer on their behalf.
_OPEN_EXACT = {"/", "/favicon.ico", "/favicon.svg", "/manifest.webmanifest",
               "/api/health", "/api/openapi.json"}
_OPEN_PREFIXES = ("/static/",)
_AUTH_PREFIX = "/api/auth"


def _gated_avatar_static(path: str) -> bool:
    """Is this /static/ path avatar-shaped, i.e. subject to the session gate?

    Structural, not a literal prefix: ANY path segment that merely *starts*
    with "avatars" counts. Sibling copies like /static/avatars.backup-<date>/
    hold the same pictures as the live directory, and the old exact
    "/static/avatars/" prefix check let them serve every non-safe bot's face
    to a sessionless client (found 2026-08-01). Matching the shape instead of
    the one blessed name means a future stray copy fails closed too.
    """
    if not path.startswith("/static/"):
        return False
    return any(seg.lower().startswith("avatars") for seg in path.split("/"))


def _safe_avatar_static(path: str) -> bool:
    """The ONE avatar shape Safe Mode may fetch: a safe bot's own file,
    directly under the live /static/avatars/ directory — no nesting (backup
    subdirs), no sibling directories. Everything else stays behind the PIN."""
    if not path.startswith("/static/avatars/"):
        return False
    fname = path[len("/static/avatars/"):]
    return "/" not in fname and fname in _safe_avatar_files()
_INBOUND_MSG_RE = re.compile(r"^/api/threads/[^/]+/messages$")


# Reaction endpoints an on-box agent legitimately drives — everything the
# dispatch-reactions skill instructs it to do: fire one, read/edit the prompt
# bank, check and refill the pool, tune the overlay. Kept as an explicit table
# so widening it is a deliberate edit, not a regex accident. Pack curation
# (upload / edit / delete an image) stays full-session: that is the operator's shelf.
_INBOUND_REACTION = {
    ("GET", "/api/reactions"),
    ("POST", "/api/reactions/fire"),
    ("GET", "/api/reactions/prompts"), ("PUT", "/api/reactions/prompts"),
    ("GET", "/api/reactions/pool"), ("PUT", "/api/reactions/pool"),
    ("POST", "/api/reactions/pool/refill"),
    ("PUT", "/api/reactions/settings"),
    ("POST", "/api/reactions/generate"),
}


# Agent-fired image jobs. Exactly two routes, and both belong on the machine
# surface for the same reason the reaction fire does: this is a bot asking for
# something, not an operator configuring something. Firing is POST, reading a
# job's state is GET, and there is deliberately no list route — an agent that
# has lost its job id has also lost the thread it was for, and the placeholder
# message in that thread is the answer either way.
_INBOUND_IMAGE_JOB_RE = re.compile(r"^/api/image-jobs(?:/[A-Za-z0-9_-]{1,64})?$")

# The rig's completion callback. A third route, and the only one on this
# surface whose caller is neither on this box nor holding the api_token: the
# image server is a LAN peer that knows one thing about us, the per-job token
# we handed it at submit time. That token is the whole credential, which is
# why the body is discarded — see image_job_callback.
_INBOUND_IMAGE_JOB_CALLBACK_RE = re.compile(
    r"^/api/image-jobs/[A-Za-z0-9_-]{1,64}/callback$")


# Avatar management an on-box agent legitimately drives. Agents already
# generate the images (the image CLI) and a cron rotates them daily — but the API
# was full-session only, so that rotation had to be a shell script writing
# files and config.yaml directly, going around the app entirely. OPENCLAW.md
# meanwhile documented the upload endpoint as available to OpenClaw, which it
# was not: it returned 403.
#
# Scoped to ONE bot's own avatar, by regex, so this cannot become a general
# write channel. Reading a face is included because an agent that is about to
# replace an avatar should be able to see the current one first.
_INBOUND_AVATAR_RE = re.compile(
    r"^/api/bots/[^/]+/avatar(?:/full|/history(?:/[^/]+)?|/restore)?$")

# Re-pinning ONE thread's avatar is part of the same avatar-management surface
# (POST only — the GET of this path is the browser-facing image route and keeps
# its own decoy gating). The pattern itself is defined next to the upload
# ceilings, which need it too.

# The avatar pool is the same machine surface as the reaction pool: the
# on-box watchdog CLI checks status and kicks refills, and agents curate the
# prompt banks (deliberately data an agent can rewrite — see reactions'
# prompts routes). The bot-id segment shares BOT_ID_RE's charset, so the
# regex admits no dot and no separator.
_INBOUND_AVATAR_POOL_RE = re.compile(
    r"^/api/avatar-pool(?:/[A-Za-z0-9_-]+(?:/refill|/prompts)?)?$")

# Jobs board (added 2026-09-14): the two routes on-box agents legitimately
# drive. BEFORE 2026-09-14 these were NOT in the allowlist and every agent
# following the job-board spec got a 403 — see the scar comment at
# main.py:1541–1545 above (the thread-management trap) for why it mattered
# to add these entries proactively with a comment that names the failure.
# The session-only routes (vote / applied / tags / archive /
# profile/recompute) deliberately stay on the session tier: those are the
# surfaces the operator drives from the browser, not agents.
_JOBS_INBOUND = (
    ("POST", "/api/jobs"),
    ("POST", "/api/jobs/score"),
)


# Thread management OPENCLAW.md has always documented as available to on-box
# agents: list/read a thread, create/rename/pin/archive/delete one, delete a
# message, mark read, unread counts. Until 2026-08-14 only the POST-message
# half was actually exempt — every agent following the doc's read/manage
# examples got a 403 and flailed (same story as the avatar routes above).
# Each route keeps its own _is_safe_mode_caller/_deny_decoy_* guard, so a
# sessionless BROWSER still gets exactly the Safe-Mode view it always had.
_INBOUND_THREAD_ONE_RE = re.compile(r"^/api/threads/[^/]+$")
_INBOUND_THREAD_SUB_RE = re.compile(r"^/api/threads/[^/]+/(?:messages|read)$")
_INBOUND_MESSAGE_ONE_RE = re.compile(r"^/api/messages/[^/]+$")


def _is_inbound(method: str, path: str) -> bool:
    """OpenClaw machine-to-machine endpoints (session-exempt; API-key optional)."""
    if method == "POST" and path in ("/api/inject", "/api/daily"):
        return True
    if method in ("GET", "POST") and path == "/api/threads":
        return True
    if method == "GET" and path == "/api/bots":
        # Roster discovery. The dispatch skill has to WARN agents off this
        # route today ("do NOT discover ids via GET /api/bots") because a
        # sessionless machine got the Safe-Mode subset — so a model that
        # looked up the roster the REST-natural way concluded the non-safe
        # bots do not exist, then injected at the wrong one or gave up. A warning
        # is not a fix for something a small model forgets under context load.
        # Same machine-branch shape as GET /api/reactions, and the handler
        # keeps _is_safe_mode_caller, so a browser tab with no session still
        # sees exactly the Safe-Mode roster it always did.
        return True
    if method in ("GET", "PATCH", "DELETE") and _INBOUND_THREAD_ONE_RE.match(path):
        return True
    if method in ("GET", "POST") and _INBOUND_THREAD_SUB_RE.match(path):
        return True
    if method == "DELETE" and _INBOUND_MESSAGE_ONE_RE.match(path):
        return True
    if method == "GET" and path == "/api/unread":
        return True
    if method == "GET" and path == "/api/files":
        # List ONLY — the metadata an on-box agent needs to map an upload to
        # its blob (name → stored_name → FILES_DIR/<stored_name>). Retrieval
        # (/download, /raw) deliberately stays off the machine surface: agents
        # read the disk, and the one-way drop stays one-way for everyone else.
        return True
    if (method, path) in _INBOUND_REACTION:
        return True
    if method in ("GET", "POST") and _INBOUND_IMAGE_JOB_RE.match(path):
        return True
    if method == "POST" and _INBOUND_IMAGE_JOB_CALLBACK_RE.match(path):
        return True
    if method in ("GET", "POST") and _INBOUND_AVATAR_RE.match(path):
        return True
    if method == "POST" and _INBOUND_THREAD_AVATAR_RE.match(path):
        return True
    if method in ("GET", "PUT", "POST") and _INBOUND_AVATAR_POOL_RE.match(path):
        return True
    # Jobs board — agents post and pre-score; everything else is session
    # tier and refuses here normally (with the route's own 403).
    if (method, path) in _JOBS_INBOUND:
        return True
    return bool(method == "POST" and _INBOUND_MSG_RE.match(path))


def _decoy_blocked(method: str, path: str) -> bool:
    """Paths a decoy session must never reach (image/file RETRIEVAL + management).

    `POST /api/upload` is intentionally NOT here: Safe Mode's "＋" button uploads
    through it (the endpoint is POST-only, so there's no media to leak back) —
    but decoy uploads are capped by a daily byte quota (DECOY_UPLOAD_QUOTA).
    Retrieval (/media, /api/media, /api/files*) and management stay barred, so a
    locked session can send an upload but can't browse or pull anything down.
    """
    # Belt-and-braces: /static/ is an open prefix so avatar paths normally get
    # their safe-file filter in the auth gate itself — but if one ever reaches
    # here, the same structural rule (any avatars* segment, incl. sibling
    # backup copies) blocks it.
    if _gated_avatar_static(path):
        return True
    if path.startswith(("/media/", "/api/media", "/api/files",
                        # Operator diagnostics: storage layout, log tail, config
                        # findings. The router fails closed on its own too.
                        "/api/dashboard",
                        # "Connect an AI" — provider presets, the connection
                        # probe, and the route that writes an API key into
                        # config.yaml. Setup UI is never shown in Safe Mode, and
                        # the routes re-check for themselves (_require_operator).
                        "/api/llm",
                        # Recovery / retrieval tools are full-access only: they
                        # read raw transcripts + dump all history (would bypass
                        # Safe-Mode redaction). Locked sessions can't reach them.
                        "/api/search", "/api/export", "/api/recover",
                        "/api/openclaw",
                        # Avatar-pool management (status, config, prompts,
                        # refill) — full-session or on-box machine only; a
                        # locked device has no business with the shelf.
                        "/api/avatar-pool",
                        # Image jobs: firing one spends GPU time and puts a
                        # picture in a conversation, and reading one back names
                        # the prompt and the rig's error text. Agents and the
                        # operator only — a locked device sees the resulting
                        # message (redacted) and nothing else.
                        "/api/image-jobs",
                        # Jobs board: write surface is gated on a full session;
                        # reads redact to the empty shape (jobs.py handles it).
                        # Either way a locked device must never see a job row,
                        # so the path is blocked here as well.
                        "/api/jobs",
                        # The harness is code execution — a headless job runs
                        # a shell agent in the operator's home directory —
                        # so this is belt-and-braces on top of
                        # _require_harness's own gate.
                        # StudioForge is not code execution, but its panel is
                        # an UNAUTHENTICATED admin surface for the LLM rig:
                        # anyone who learns the address from a Safe-Mode device
                        # can load and unload models on it. The status route
                        # discloses that address, so it is operator-only too --
                        # belt-and-braces on top of _require_studioforge.
                        "/api/harness", "/api/studioforge",
                        # Local Viewer: reads arbitrary bytes off the host's
                        # disk. Unlocked operator only — Safe Mode never even
                        # renders the affordance, and this is the server half
                        # of that promise.
                        "/local/", "/api/local")):
        return True
    if path == "/api/reactions" or path.startswith("/api/reactions/"):
        # Safe Mode gets the reaction feature READ-ONLY, through the `safe`
        # flag — the same opt-in model as safe bots' avatars: it may list (the
        # route filters to safe entries) and fetch a safe reaction's image.
        # Firing is agent-only (refused in reactions_fire — that endpoint is
        # inbound-exempt so it normally never reaches this gate; no exception
        # here is belt-and-braces). Pack management (upload, edit, delete,
        # settings, image generation) stays full-session only.
        if method == "GET" and path == "/api/reactions":
            return False
        mi = re.match(r"^/api/reactions/([^/]+)/image$", path)
        if mi and method == "GET":
            return mi.group(1) not in reactions.safe_ids()
        return True

    m = re.match(r"^/api/bots/([^/]+)/avatar(/full)?$", path)
    if m:
        # /avatar/full is always PIN-gated (full-resolution images stay locked).
        # GET /avatar is allowed for safe bots so the locked view can display
        # their pictures; POST (replace) is a mutation — Safe Mode is VIEW +
        # SEND only, so changing an avatar requires a full PIN session.
        if m.group(2) or method != "GET":
            return True
        return m.group(1) not in _safe_bot_ids()
    return path in ("/api/bots/order", "/api/bots/all")


def _deny_decoy_bot(request: Request, bot_id: str | None) -> None:
    """403 a Safe-Mode request that targets a bot not flagged for Safe Mode."""
    if _is_decoy(request) and bot_id not in _safe_bot_ids():
        raise HTTPException(403, "Unlock for full access")


async def _deny_decoy_thread(request: Request, thread_id: str) -> None:
    if _is_decoy(request):
        _deny_decoy_bot(request, await _bot_of_thread(thread_id))


async def _canonical_thread_id(thread_id: str) -> str:
    """The real row's id for ``thread_id``, matched case-insensitively.

    The write paths (/api/inject, POST …/messages) have always resolved this
    way because the OpenClaw gateway lowercases whole session keys, so an agent
    reads its own thread id back as `daily-doxy-…` when the row is
    `daily-Doxy-…`. The read/verify paths did NOT, which made the asymmetry
    worse than either behaviour on its own: an agent could post successfully
    and then 404 on the very next "verify, then stop" step, and a model that
    cannot tell a phantom failure from a real one starts retrying a send that
    already worked. Every thread-taking endpoint goes through here now.

    Raises 404 for an id that matches no row, exactly as the old exact-match
    ``get_thread`` check did — this widens what is found, never what is
    permitted; the caller still runs its own tier checks on the canonical id.
    """
    canonical = await db.resolve_thread_id(thread_id)
    if not canonical:
        raise HTTPException(404, "Thread not found")
    return canonical


# Per-client decoy upload accounting: {client_ip: bytes_uploaded_today}.
_decoy_upload_used: dict[str, int] = {}
_decoy_upload_day: str = ""


def _quota_ip(headers, peer: str) -> str:
    """The identity a Safe-Mode daily budget is charged to.

    The socket peer, and ONLY the socket peer, unless the peer is loopback.

    This used to read the leftmost `X-Forwarded-For` whenever one was present,
    with the reasoning that a forger "splits its OWN budget into more buckets".
    That is exactly backwards: a bucket is a fresh ALLOWANCE, so N forged
    values buy N times the daily quota — and a same-origin `fetch()` from the
    locked page can set the header freely, so the limited tier could lift its
    own limit from the browser.

    There is no trusted reverse proxy in this deployment. Tailscale Serve is
    the only thing that ever fronts the app, and it connects from loopback —
    so a forwarding header is believed only when the peer IS loopback, where
    the alternative (one shared household bucket for every tailnet device)
    would be the worse failure. A remote peer's headers are ignored outright.
    Of a forwarded list only the RIGHTMOST value — the one the proxy itself
    appended — is read; anything to its left was written by the caller.
    """
    if _ip_is_loopback(peer):
        # RIGHTMOST entry: a proxy appends the address it saw, so the last
        # value is the one the proxy wrote and the leftmost is whatever the
        # caller put there itself — a tailnet device could otherwise mint a
        # fresh bucket per forged value, the very thing this docstring warns of.
        first = (headers.get("x-forwarded-for") or "").split(",")[-1].strip()
        # An IPv6 literal may arrive bracketed and/or with a port.
        if first.startswith("[") and "]" in first:
            first = first[1:first.index("]")]
        elif first.count(":") == 1 and "." in first:
            first = first.rsplit(":", 1)[0]
        try:
            return str(ipaddress.ip_address(first))
        except ValueError:
            pass
    return peer or "?"


def _request_quota_ip(request: Request) -> str:
    client = getattr(request, "client", None)
    return _quota_ip(getattr(request, "headers", None) or {},
                     client.host if client else "?")


def _decoy_quota_left(request: Request) -> int | None:
    """Bytes a decoy client may still upload today; None for full sessions."""
    global _decoy_upload_day
    if not _is_decoy(request):
        return None
    today = datetime.now().strftime("%Y-%m-%d")
    if today != _decoy_upload_day:
        _decoy_upload_day = today
        _decoy_upload_used.clear()
    ip = _request_quota_ip(request)
    return max(0, DECOY_UPLOAD_QUOTA - _decoy_upload_used.get(ip, 0))


def _decoy_quota_add(request: Request, n: int) -> None:
    ip = _request_quota_ip(request)
    _decoy_upload_used[ip] = max(0, _decoy_upload_used.get(ip, 0) + n)


def _decoy_over_quota(request: Request) -> bool:
    """True once this client's SHARED running total exceeds the daily budget.

    Read live (not from a per-request snapshot) so overlapping uploads all
    contend on the same counter — closes the TOCTOU window where each request
    read the full remaining budget before any of them committed their bytes."""
    ip = _request_quota_ip(request)
    return _decoy_upload_used.get(ip, 0) > DECOY_UPLOAD_QUOTA


# {(client_ip, action): count} for the current day; cleared with the upload day.
_decoy_action_used: dict[tuple[str, str], int] = {}


def _decoy_action_allowed(ip: str, action: str, limit: int) -> bool:
    """Charge one unit of a Safe-Mode daily action budget. False once spent."""
    global _decoy_upload_day
    today = datetime.now().strftime("%Y-%m-%d")
    if today != _decoy_upload_day:
        _decoy_upload_day = today
        _decoy_upload_used.clear()
        _decoy_action_used.clear()
    key = (ip or "?", action)
    used = _decoy_action_used.get(key, 0)
    if used >= limit:
        return False
    _decoy_action_used[key] = used + 1
    return True


def _ws_client_ip(ws: WebSocket) -> str:
    """Same budget identity as the REST path — see _quota_ip."""
    return _quota_ip(ws.headers, ws.client.host if ws.client else "?")


def _deny_decoy_mutation(request: Request) -> None:
    """Safe Mode is VIEW + SEND only. Destructive/management ops (delete a message
    or thread, rename, pin, archive) require a full PIN session even for the safe
    bots — otherwise an unauthenticated tailnet client could permanently delete or
    rename the family's chat history through the decoy view."""
    if _is_decoy(request):
        raise HTTPException(403, "Unlock for full access")


def _require_full_access(request: Request) -> None:
    """In-handler full-access gate for the operator-only routes.

    A second lock on the same door, for the same reason dashboard_routes states
    it: those routes are protected ONLY by the `_decoy_blocked` prefix tuple in
    the middleware, so their security is a string-prefix list in another
    function — an edit to that tuple, a route mounted at a new path, or a
    handler called from anywhere but the middleware chain and the guard is
    simply gone, silently. `/api/files/wipe` deletes the entire File Server.

    Derives the answer itself: a Safe-Mode verdict is refused, a live session
    or an AUTHENTICATED MACHINE caller (the gate's inbound branch — on-box
    agents list files this way) is allowed, and a caller with neither is
    allowed only when the app has no lock at all, which is the rule the rest
    of the app uses.
    """
    if getattr(request.state, "decoy", False):
        raise HTTPException(403, "Unlock for full access")
    if getattr(request.state, "machine", False):
        return
    session = _session_of(request) or auth.get_session(
        request.cookies.get(COOKIE_NAME))
    if session is not None:
        return
    if auth.load().pin_set:
        raise HTTPException(403, "Unlock for full access")


# Liveness of the long-running background loops: {name: (last_beat_ts, period_s)}.
# A loop that dies takes its duty with it and NOTHING said so — the app kept
# answering "ok" while sessions stopped being purged and backups stopped being
# taken. Each loop beats once per pass; /api/health reports the age.
_loop_beats: dict[str, tuple[float, float]] = {}
_LOOP_STALE_FACTOR = 2.5      # allow a full period of slack before "stale"


def _loop_beat(name: str, period_s: float) -> None:
    _loop_beats[name] = (time.time(), period_s)


def _loop_health() -> dict[str, dict]:
    """Per-loop {age_s, period_s, stale} for the health body."""
    now = time.time()
    out: dict[str, dict] = {}
    for name, (ts, period) in _loop_beats.items():
        age = now - ts
        out[name] = {"age_s": round(age, 1), "period_s": period,
                     "stale": age > period * _LOOP_STALE_FACTOR}
    return out


async def _session_purge_loop() -> None:
    """Drop sessions abandoned past the inactivity window (lazy purge backstop).

    One bad pass must not end the loop. It used to be a bare `while True`
    inside a single try: any exception (not just cancellation) unwound out of
    the task and sessions were never purged again for the life of the process,
    silently — the task's exception is never retrieved, so nothing even logged.
    """
    _loop_beat("session_purge", 300)
    try:
        while True:
            await asyncio.sleep(300)
            try:
                auth.purge_expired()
            except Exception:
                log.exception("session purge pass failed; loop continues")
            _loop_beat("session_purge", 300)
    except asyncio.CancelledError:
        _loop_beats.pop("session_purge", None)   # shutdown, not a fault


async def _broadcast_thread_update(thread_id: str) -> None:
    thread = await db.get_thread(thread_id)
    if thread:
        await manager.broadcast({"type": "thread_update", "thread": thread.model_dump()})


def _demote_tool_warning(content: str, metadata: dict | None) -> dict | None:
    """Collapse a gateway tool-status warning ("⚠️ 🛠️ Exec failed: `…`") to
    "sub" (collapsed working-output) style. These are the runtime narrating a
    failed tool call, not the bot speaking; demoting at the persist chokepoint
    covers every assistant path — turn payloads, watcher/reconciler/follower,
    the gateway mirror AND /api/inject — so one can never land as a full chat
    bubble in the family chat again (seen 2026-07-17 and 2026-07-29)."""
    if openclaw_text.is_tool_warning(content) and not (metadata or {}).get("sub"):
        return {**(metadata or {}), "sub": True}
    return metadata


# How old a recovered/followup reply may be and still fire its reactions.
# Wide enough to cover a WS seq-gap backfill or a follower turn landing late;
# narrow enough that the startup gap sweep replaying last Friday stays silent.
REACTION_REPLAY_FRESH_S = 600


# --- Reaction autopilot ----------------------------------------------------
# A standing "react on every reply" instruction decays with context depth and
# tool load — measured live: one long session produced 36 consecutive replies
# with zero markers while the model stayed otherwise coherent, and even a
# fresh session missed 6 of 8. Prompt-side nagging cannot guarantee a
# per-reply behavior; this chokepoint can. When an autopilot-enabled bot's
# reply carries no marker, the server picks a mood and fires on its behalf.
# The bot's own marker always wins; autopilot only fills silence, and its
# refusals surface exactly like marker fires (sub row + health counter).
_AUTOPILOT_MIN_CHARS = 40
_AUTOPILOT_NIGHT_HOURS = range(7)          # box-local; "never at night"
# 2026-09-07. Two lessons, in order.
#
# The list used to be six words of INCIDENT PROSE and matched none of the alert
# vocabulary this box emits, so six of nine reaction fires that day decorated
# failure notices, each spending a one-shot pool image to celebrate bad news.
#
# The first fix was to widen the vocabulary and scan the whole message. Measured
# against 14 days of real replies, that silenced 89 of Bits' 338 autopilot fires
# (26%) while only ~12 of them were actual alerts. Nearly every ops report she
# writes mentions something that failed on the way to succeeding -- "Box smoke:
# OK — 0 failing", "Done, sweetheart… ✅" with a ⚠️ inside a status table -- so a
# whole-body scan reads a success report as an emergency.
#
# What actually separates the two is POSITION, not vocabulary: a real alert
# LEADS with it ("⚠️ …", "**Box smoke: CRITICAL…", "Morning brief failed…"),
# while a success report buries the word mid-body. So the test is scoped to the
# opening of the message.
#
# Note this is the SECOND net. The origin gate in _prepare_persist already
# blocks every machine-injected row by route, which covered all six of the
# 2026-09-07 fires on its own. This one only catches a bot RELAYING an alert in
# its own conversational turn, so it can afford to be narrow -- and being narrow
# is what keeps it from eating the replies autopilot exists for.
_AUTOPILOT_ALERT_HEAD_CHARS = 120

_AUTOPILOT_SERIOUS_RE = re.compile(
    r"(?:^\s*(?:\*\*)?\s*(?:⚠️|❌|🚨)"
    r"|\b(?:outage|security\s+breach|breach|urgent|emergency|incident)\b"
    r"|\bbox\s+smoke:\s*(?:critical|warn)"
    r"|\b(?:still|newly)\s+failing\b"
    r"|\bnever\s+finished\b"
    r"|\bunreachable\b"
    r"|\bcritical(?:ly)?\s+(?:fail|error|down|broken)"
    r")", re.IGNORECASE)


_AUTOPILOT_DONE_RE = re.compile(
    r"\b(deploy(?:ed)?|shipp?ed|publish(?:ed)?|done|completed?|fixed|landed|"
    r"passed|verified|green)\b", re.IGNORECASE)


def _autopilot_now_hour() -> int:
    """Box-local hour — a hook so tests can pin the clock."""
    return datetime.now().hour


async def _reaction_autopilot_mood(thread_id: str, content: str) -> str | None:
    """The mood the server fires when the bot forgot — or None to stay quiet.

    The skip rules mirror the agent-facing doctrine (skills/dispatch-reactions):
    no picture on a question (the ball is in the user's court), on serious
    moments, at night, or on tiny acks that never earned one.
    """
    text = (content or "").strip()
    if len(text) < _AUTOPILOT_MIN_CHARS:
        return None
    if text.endswith("?"):
        return None
    # Position, not vocabulary: only the OPENING of the message is tested.
    if _AUTOPILOT_SERIOUS_RE.search(text[:_AUTOPILOT_ALERT_HEAD_CHARS]):
        return None
    hour = _autopilot_now_hour()
    if hour in _AUTOPILOT_NIGHT_HOURS:
        return None
    if (thread_id.startswith("daily-") and hour < 11
            and not await db.has_assistant_message(thread_id)):
        return "morning"
    if _AUTOPILOT_DONE_RE.search(text):
        return "task_complete"
    return "thinking"


def _replay_is_fresh(created_at: str | None) -> bool:
    """Whether a replayed reply is recent enough that its reactions are news.

    ``created_at`` is the ORIGINAL timestamp a recovery path carries (the DB
    files recovered answers at their real time — see add_message). A replay
    with NO timestamp cannot prove it is fresh, and an unparseable one is
    treated the same — when in doubt, no side effects.
    """
    if not created_at:
        return False
    try:
        ts = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)   # transcript stamps are UTC
    return (datetime.now(UTC) - ts).total_seconds() <= REACTION_REPLAY_FRESH_S


def _iso_age_seconds(created_at: str | None) -> float | None:
    """Age of an ISO-8601 timestamp in seconds; None when absent/unparseable.

    Same tolerance as :func:`_replay_is_fresh` — transcript stamps may be
    `Z`-suffixed or naive-UTC. Callers skip a None row without treating it as
    inside (or outside) any window.
    """
    if not created_at:
        return None
    try:
        ts = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return (datetime.now(UTC) - ts).total_seconds()


class _Prepared(NamedTuple):
    """What the shared pre-persist pass decided about one message."""
    content: str
    metadata: dict | None
    bot_id: str | None
    fired: list[str]
    autopilot: bool     # `fired` is the SERVER's pick, not the bot's marker
    skip: bool          # nothing left to say — persist no empty bubble
    # Image jobs this message's `[[pic:…]]` markers earned. Started by the
    # CALLERS, after the message exists: this pass cannot persist a second
    # message of its own. Never mutated in place — and the default is a tuple
    # precisely so the shared class-level default CANNOT be mutated (RUF012).
    pic_specs: Sequence = ()
    # Markers that were found and produced NO job (invalid, over the cap, rate
    # limited). Carried rather than dropped so the caller can leave a visible
    # trace: silence here is indistinguishable from "the model never asked".
    pic_drops: Sequence = ()


async def _prepare_persist(
    thread_id: str, role: str, content: str,
    media_url: str | None, metadata: dict | None, created_at: str | None,
) -> _Prepared:
    """Everything that happens to a message BEFORE it is written down.

    The single implementation behind both persist chokepoints
    (:func:`_persist_and_broadcast_message` and
    :func:`_persist_and_stream_message`). It used to be copy-pasted into each,
    and the copy drifted: the streaming path — the one final agent replies
    actually take — never grew the reaction autopilot, the replay-freshness
    window, or a `created_at`, so autopilot simply never saw a live reply.
    One function, so the next rule lands on every path by construction.
    """
    fired: list[str] = []
    pic_specs: list = []
    pic_drops: list = []
    bot_id: str | None = None
    autopilot = False
    if role == "assistant":
        bot_id = await _bot_of_thread(thread_id)
        content = _salvage_media_refs(
            openclaw_text.sanitize_assistant_visible_text(
                _strip_reply_directive(_strip_no_reply(content))))
        # `:react:<id>:` markers are the agent's way to pop a reaction image.
        # Stripped here, at the persist chokepoint, so the marker syntax can
        # never reach a chat bubble on any path.
        #
        # An OLD recovered message strips its markers but must NOT fire them.
        # The gap sweep replays answers a live path missed, sometimes days
        # later — and the first sweep popped two reaction images on every
        # device in the house for conversations that had finished on Friday,
        # permanently spending two one-shot pool images to celebrate old news.
        # Reactions are a live, interruptive, single-use side effect;
        # replaying HISTORY must not have side effects at all.
        #
        # But recovery is not always history: in practice ~40% of live replies
        # arrive via the follower/seq-gap backfill seconds after they were
        # generated, and a blanket suppression ate those fires too (204 in the
        # two weeks before the freshness window existed). A replay inside
        # REACTION_REPLAY_FRESH_S is a live reply that took the scenic route.
        replaying = bool((metadata or {}).get("followup")
                         or (metadata or {}).get("recovered"))
        content, fired = reactions.extract_markers(content, bot_id=bot_id)
        if replaying and fired and _replay_is_fresh(created_at):
            replaying = False
        if replaying and fired:
            log.info("suppressed %d reaction(s) on a recovered message (%s)",
                     len(fired), thread_id)
            fired = []
        # `[[pic:<prompt>|<caption>]]` is the same idea for a generated
        # picture, and obeys the same replay rule: an old reply keeps its
        # marker stripped but must not occupy a GPU for news that has already
        # been read.
        content, pic_specs, pic_drops = await _pic_specs_from_markers(
            content, bot_id, thread_id, metadata=metadata,
            replaying=replaying and not _replay_is_fresh(created_at))
        metadata = _demote_tool_warning(content, metadata)
        # Autopilot fills the silence AFTER the demote so a collapsed tool
        # warning never earns a picture, and only for LIVE replies — a replay
        # that was too old to fire its own markers must not gain server ones.
        # An image-job placeholder is not a reply — it is a receipt for one
        # that is still rendering. Letting autopilot decorate it would spend a
        # one-shot pool image on "generating an image…", and then the row it
        # decorated gets rewritten into something else entirely.
        # A machine-injected row is not a conversation. Watchdogs, box-smoke,
        # cron reports and runs-deliver all post through /api/inject; on
        # 2026-09-07 six of nine reaction fires decorated exactly those rows.
        # The word guard above catches the ones that read like alerts, but it
        # cannot catch a neutrally-worded machine post, so gate on the ROUTE
        # too: autopilot exists to fill silence in a bot's own conversational
        # replies. (doxy-pics deliveries arrive the same way and are excluded
        # by the same rule -- they already carry a picture.)
        if (not fired and not replaying and bot_id
                and not (metadata or {}).get("sub")
                and (metadata or {}).get("origin") != "inject"
                and (metadata or {}).get("kind") != "image_job"):
            bot = config.get_bot(bot_id)
            if bot is not None and bot.reactions and bot.reaction_autopilot:
                mood = await _reaction_autopilot_mood(thread_id, content)
                if mood:
                    fired = [mood]
                    autopilot = True
                    log.info("reaction autopilot: %r for %s (%s)",
                             mood, bot_id, thread_id)
    elif content:
        # Markers are stripped on EVERY path — /api/inject with role user or
        # system included — but only an assistant's markers fire a reaction or
        # ask for a picture. A human typing either syntax gets it removed and
        # nothing else happens.
        if ":react:" in content.lower():
            content, _ = reactions.extract_markers(content)
        if "[[pic:" in content.lower():
            content = image_jobs.strip_pic_markers(content)
    if content and "[[media:" in content:
        # Ingesting can copy up to 500MB off disk — never on the event loop
        # (this helper sits on the persist/WS hot path for every message).
        content = await asyncio.to_thread(_ingest_content_media, content)
    # Nothing left to say (an all-marker message) — persist no empty bubble.
    # Deliberately role-agnostic: the guard used to apply to assistants only,
    # so `POST /api/inject` with a user/system body of just `:react:random:`
    # dropped a blank bubble into the thread.
    skip = not (content or "").strip() and not media_url
    return _Prepared(content, metadata, bot_id, fired, autopilot, skip,
                     pic_specs, pic_drops)


# --------------------------------------------------------------------------- #
# Provisional bubbles (live gateway deltas)
#
# While a turn is running the client shows the model's words under a
# PROVISIONAL id — `run:<runId>` — because no database row exists yet. When the
# row lands, the frame that announces it must tell the client which
# provisional bubble it replaces, or the client is left with two: the one it
# streamed and the one that arrived.
#
# The registry is keyed by thread rather than by run because the persist
# chokepoints know a thread, not a run. One open bubble per thread is also the
# truth of the product: a thread's turns are serialised by its own lock.
# --------------------------------------------------------------------------- #

_provisional_runs: dict[str, str] = {}


def _open_provisional(thread_id: str, provisional_id: str) -> None:
    _provisional_runs[thread_id] = provisional_id


def _close_provisional(thread_id: str, provisional_id: str) -> bool:
    """Claim a thread's open bubble. False when it was already claimed.

    Both the persist chokepoint and the router's settle timer race to retire
    the same bubble; whichever gets here first owns it, and the loser must send
    nothing — a second `stream_done` for a bubble that already has its message
    would blank it out.
    """
    if _provisional_runs.get(thread_id) != provisional_id:
        return False
    _provisional_runs.pop(thread_id, None)
    return True


def _take_provisional(thread_id: str) -> str | None:
    return _provisional_runs.pop(thread_id, None)


def _provisional_open(thread_id: str, provisional_id: str) -> bool:
    """True while this bubble is still the thread's live one (unclaimed)."""
    return _provisional_runs.get(thread_id) == provisional_id


# The tail of a cumulative delta that may still be growing into a directive or
# a marker. `[[med` is not `[[media:…]]` yet, so no strip pattern matches it —
# and sending it would put the beginning of an absolute path on screen a
# fraction of a second before the strip catches up.
_DELTA_PARTIAL_RE = re.compile(
    r"(\[\[[^\]]*|:(?:r|re|rea|reac|react|react:[a-z0-9_-]*))$", re.I)


def _sanitize_delta(text: str) -> str:
    """What a live delta may show, by exactly the persist chokepoint's rules.

    Delta text is raw model output. It still carries `[[media:/abs/path]]`,
    `[[doc:…]]`, `[[pic:…]]`, `:react:…:` and the gateway's internal-context
    scaffolding — every one of which the persisted copy has stripped since
    long before streaming existed. A locked family device must never see an
    absolute path or an internal marker in a provisional bubble that the
    finished message would not have shown it.

    Applied to the CUMULATIVE text, never to a chunk: a directive split across
    two deltas is whole here, and a directive that is only half-written yet is
    held back until it completes.
    """
    if not text:
        return ""
    clean = openclaw_text.sanitize_assistant_visible_text(
        _strip_reply_directive_anywhere(
            _strip_reply_directive(_strip_no_reply(text))))
    clean = reactions.strip_markers(clean)
    clean = image_jobs.strip_pic_markers(clean)
    clean = _MEDIA_DIRECTIVE_RE.sub("", clean)
    clean = _DOC_REF_RE.sub("", clean)
    return _DELTA_PARTIAL_RE.sub("", clean)


async def _persist_and_broadcast_message(
    thread_id: str, role: str, content: str,
    media_url: str | None = None, metadata: dict | None = None,
    source_id: str | None = None, created_at: str | None = None,
) -> MessageOut:
    prep = await _prepare_persist(thread_id, role, content, media_url,
                                  metadata, created_at)
    content, metadata, bot_id, fired = (prep.content, prep.metadata,
                                        prep.bot_id, prep.fired)
    if prep.skip:
        # The reactions and pictures it asked for still happen — an all-marker
        # message is a request with no prose, not a message to discard.
        if fired:
            await _fire_marker_reactions(
                fired, thread_id, bot_id or await _bot_of_thread(thread_id),
                autopilot=prep.autopilot)
        if prep.pic_specs or prep.pic_drops:
            await _start_pic_jobs(prep.pic_specs, thread_id,
                                  bot_id or await _bot_of_thread(thread_id),
                                  drops=prep.pic_drops)
        return _unpersisted_message(thread_id, role)
    msg = await db.add_message(thread_id, role, content, media_url=media_url,
                               metadata=metadata, source_id=source_id,
                               created_at=created_at)
    bot_id = await _bot_of_thread(thread_id)   # lets the redactor scope the frame
    await manager.broadcast(_landing_frame(thread_id, bot_id, msg))
    if fired:
        await _fire_marker_reactions(fired, thread_id, bot_id,
                                     autopilot=prep.autopilot)
    if prep.pic_specs or prep.pic_drops:
        await _start_pic_jobs(prep.pic_specs, thread_id, bot_id,
                              drops=prep.pic_drops)
    return msg


def _landing_frame(thread_id: str, bot_id: str | None,
                   msg: MessageOut) -> dict:
    """The frame that announces a newly persisted row.

    Normally `message`. When this thread has a provisional bubble open — the
    client has been watching these very words stream in under `run:<id>` — it
    is a `stream_done` naming that bubble instead, so the row REPLACES what is
    on screen rather than landing underneath it as a second copy.
    """
    # A sub row (tool chatter, a reaction-fire notice) that lands while the
    # reply is still streaming is NOT the reply — claiming the bubble for it
    # would swap the half-painted answer for a collapsed "working" line and
    # then drop the real answer underneath as a second row.
    meta = msg.metadata if isinstance(getattr(msg, "metadata", None), dict) else {}
    prov = None if (meta.get("sub") or msg.role != "assistant") \
        else _take_provisional(thread_id)
    if prov is None:
        return {"type": "message", "thread_id": thread_id, "bot_id": bot_id,
                "message": msg.model_dump()}
    return {"type": "stream_done", "thread_id": thread_id, "bot_id": bot_id,
            "message_id": msg.id, "message": msg.model_dump(),
            "provisional_id": prov}


def _unpersisted_message(thread_id: str, role: str) -> MessageOut:
    """Placeholder for a message deliberately not written to the DB (an
    all-marker reply). Keeps the MessageOut contract of the persist helpers
    without dropping an empty bubble into the chat."""
    return MessageOut(id=new_id(), thread_id=thread_id, role=role,
                      content="", created_at=now_iso(), metadata=None)


# Simulated streaming: message is written to DB atomically, then delivered
# word-by-word over WS. Total delivery time ≈ 2.5s regardless of length.
_STREAM_MIN_CHARS = 180    # below this, deliver as regular message
_STREAM_CHUNK_CHARS = 40   # chars per chunk (adjusted up for long text)
_STREAM_DELAY_S = 0.030    # 30 ms between chunks → ~2.5 s for long text
_STREAM_MAX_CHUNKS = 83    # cap so very long text doesn't stream forever


async def _persist_and_stream_message(
    thread_id: str, role: str, content: str,
    media_url: str | None = None, metadata: dict | None = None,
    source_id: str | None = None, created_at: str | None = None,
) -> MessageOut:
    """Persist the message, then stream its text to clients.

    Short or non-assistant messages fall back to a regular broadcast.
    Media-only messages (empty content + media_url) are also broadcast normally.

    The pre-persist pass is :func:`_prepare_persist` — the SAME one the
    broadcast chokepoint uses. It is shared rather than repeated because the
    copy that used to live here silently drifted (no autopilot, no replay
    freshness, no `created_at`).
    """
    prep = await _prepare_persist(thread_id, role, content, media_url,
                                  metadata, created_at)
    content, metadata, bot_id, fired = (prep.content, prep.metadata,
                                        prep.bot_id, prep.fired)
    if prep.skip:
        if fired:
            await _fire_marker_reactions(
                fired, thread_id, bot_id or await _bot_of_thread(thread_id),
                autopilot=prep.autopilot)
        if prep.pic_specs or prep.pic_drops:
            await _start_pic_jobs(prep.pic_specs, thread_id,
                                  bot_id or await _bot_of_thread(thread_id),
                                  drops=prep.pic_drops)
        return _unpersisted_message(thread_id, role)
    msg = await db.add_message(thread_id, role, content, media_url=media_url,
                               metadata=metadata, source_id=source_id,
                               created_at=created_at)
    bot_id = await _bot_of_thread(thread_id)   # lets the redactor scope frames

    is_sub = bool(metadata and metadata.get("sub"))
    # THE REAL STREAM WINS. When the gateway's live deltas already painted this
    # reply word by word, replaying it a second time from the finished text
    # would rewind the bubble and type it out again. `_landing_frame` closes
    # the provisional bubble with the persisted row and the simulation is
    # skipped — one animation per reply, whichever road it came down.
    if (role != "assistant" or is_sub or len(content) < _STREAM_MIN_CHARS
            or thread_id in _provisional_runs):
        await manager.broadcast(_landing_frame(thread_id, bot_id, msg))
        if fired:
            await _fire_marker_reactions(fired, thread_id, bot_id,
                                         autopilot=prep.autopilot)
        if prep.pic_specs or prep.pic_drops:
            await _start_pic_jobs(prep.pic_specs, thread_id, bot_id,
                                  drops=prep.pic_drops)
        return msg

    # Adaptive chunk size so total stream time converges to ~2.5 s.
    chunk_chars = max(_STREAM_CHUNK_CHARS, len(content) // _STREAM_MAX_CHUNKS)

    await manager.broadcast({
        "type": "stream_start", "thread_id": thread_id, "bot_id": bot_id,
        "message_id": msg.id,
    })
    for i in range(0, len(content), chunk_chars):
        await manager.broadcast({
            "type": "stream_chunk", "thread_id": thread_id, "bot_id": bot_id,
            "message_id": msg.id, "text": content[i: i + chunk_chars],
        })
        await asyncio.sleep(_STREAM_DELAY_S)

    await manager.broadcast({
        "type": "stream_done", "thread_id": thread_id, "bot_id": bot_id,
        "message_id": msg.id, "message": msg.model_dump(),
    })
    if fired:
        await _fire_marker_reactions(fired, thread_id, bot_id,
                                     autopilot=prep.autopilot)
    if prep.pic_specs or prep.pic_drops:
        await _start_pic_jobs(prep.pic_specs, thread_id, bot_id,
                              drops=prep.pic_drops)
    return msg


# --------------------------------------------------------------------------- #
# Reaction images (ephemeral overlay pack)
# --------------------------------------------------------------------------- #


async def fire_reaction(
    key: str, *, actor: str, actor_kind: str = "user",
    thread_id: str | None = None, bot_id: str | None = None,
    duration_ms: int | None = None, caption: str | None = None,
    trace: bool = True, require_safe: bool = False,
) -> dict:
    """Resolve, rate-limit and broadcast one reaction overlay.

    The single chokepoint for every trigger path — the composer picker, the
    inbound API, the CLI, and `:react:<id>:` markers inside an agent reply — so
    the enabled flag, the rate limits and the Safe-Mode rule are enforced once.
    Raises :class:`reactions.ReactionError` (which carries an HTTP status).

    The reaction itself lives in the chat: when ``trace`` is set and the
    reaction is aimed at a thread, a one-line system message is persisted FIRST
    (so the event can carry its ``trace_id`` and the UI can expand that row
    into the picture). The event is then broadcast; every connected device
    shows the image embedded in the thread and it collapses back to the trace
    line after its duration. Only an untargeted, app-wide pop (no ``thread_id``)
    is a pure broadcast overlay with nothing written down.
    """
    pack = reactions.load()
    if not pack.settings.enabled:
        raise reactions.ReactionError("Reactions are switched off", 403)

    if thread_id and not bot_id:
        bot_id = await _bot_of_thread(thread_id)
    # Per-bot capability. Reactions are a character trait, not an ambient
    # feature: a reaction aimed at a bot's conversation requires that bot to
    # have them switched on (Bot Manager → Reactions; no shipped bot has them
    # on). The bot also decides WHICH pool the draw comes from, below.
    if bot_id is not None:
        bot = config.get_bot(bot_id)
        if bot is None or not bot.reactions:
            raise reactions.ReactionError(
                f"Reactions aren't enabled for {bot.name if bot else bot_id}", 403)
    elif actor_kind == "agent":
        # An UNTARGETED agent fire must still name a reaction-enabled bot:
        # `actor` is a free-form caller field and the overlay renders it
        # verbatim, so without this check an app-wide pop could put a
        # reactions-off bot's name on every screen in the house (byline
        # spoofing). Genuinely anonymous pops belong to actor_kind "user".
        wanted = actor.strip().lower()
        claimed = next((b for b in config.load_bots()
                        if b.id.lower() == wanted or b.name.strip().lower() == wanted),
                       None)
        if claimed is None or not claimed.reactions:
            raise reactions.ReactionError(
                f"Reactions aren't enabled for {actor or 'this bot'}", 403)

    r = reactions.get(key, bot_id=bot_id)
    if r is None:
        # A dry pool is the common case here, and "no such reaction: random"
        # would be a baffling thing to read in a log.
        if key in reactions.DRAW_KEYS:
            raise reactions.ReactionError(
                "No fresh reaction images on hand right now", 503)
        raise reactions.ReactionError(f"No such reaction: {key}", 404)
    if require_safe and not r.safe:
        raise reactions.ReactionError("Unlock for full access", 403)
    # Prove the image is actually there before every screen in the house is told
    # to display it (a hand-deleted blob would otherwise pop an empty card).
    reactions.image_path(r)

    err = reactions.limiter.check(f"{actor_kind}:{actor}", pack.settings)
    if err:
        raise reactions.ReactionError(err, 429)

    # One-shot: retire a pool image BEFORE the broadcast, so two clients racing
    # the same draw can never both fire it. Once consumed the id stops
    # resolving, and the picture is never seen again.
    from_pool = r.source == "pool"
    if from_pool and not reactions.pool_consume(r.id, bot_id=bot_id):
        # The limiter already recorded this fire; the actor lost a race they
        # weren't at fault for, so give the slot back.
        reactions.limiter.refund(f"{actor_kind}:{actor}")
        raise reactions.ReactionError("That image was just used — try again", 409)

    # The trace is persisted BEFORE the broadcast: it is the chat's copy of the
    # reaction (the row the image embeds in and collapses back to), and the
    # event carries its id so clients can expand exactly that row. A failure
    # here must not take the reaction down with it — log and carry on.
    trace_id: str | None = None
    if trace and thread_id:
        try:
            tmsg = await _persist_and_broadcast_message(
                thread_id, "system", f"⚡ {actor} reacted · {r.name}",
                metadata={"kind": "reaction", "reaction_id": r.id,
                          "reaction_name": r.name, "reaction_safe": r.safe,
                          "actor": actor, "actor_kind": actor_kind,
                          # Fired pool images are kept in spent/ for good, so
                          # every trace can pop its picture back up on click.
                          # (Old rows stamped False predate that retention —
                          # their blobs are already gone; the UI honours it.)
                          "replayable": True},
            )
            trace_id = tmsg.id
        except Exception as e:                     # pragma: no cover
            log.warning("could not persist reaction trace for %r: %s", r.id, e)

    event = {
        "type": "reaction",
        "event_id": uuid.uuid4().hex,
        "reaction_id": r.id,
        "name": r.name,
        "image_url": reactions.image_url(r.id, r.file.rsplit("/", 1)[-1]),
        "duration_ms": pack.settings.clamp_duration(duration_ms or r.duration_ms),
        "safe": r.safe,
        "actor": actor,
        "actor_kind": actor_kind,
        "caption": (caption or "").strip()[:120],
        "thread_id": thread_id,
        "bot_id": bot_id,
        "trace_id": trace_id,
        "pool": from_pool,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    await manager.broadcast(event)

    if from_pool:
        # Firing just took one off the shelf — check whether that dropped us
        # under the low-water mark and, if so, start refilling now rather than
        # waiting for the next sweep.
        _nudge_reaction_pool()

    return event


async def _fire_marker_reactions(ids: list[str], thread_id: str, bot_id: str | None,
                                 *, autopilot: bool = False) -> None:
    """Fire the `:react:<id>:` markers an agent embedded in its reply.

    The reaction lands in the chat right after the reply — the trace row is
    what the image embeds into and collapses back to — so it leaves a trace.
    A refusal (rate limit, missing blob) is still swallowed — a bot's flourish
    must never fail its actual message — but no longer INVISIBLE: each one
    leaves a collapsed sub row in the thread and a /api/health counter tick.
    A success and a failure used to look identical from the chat, which is how
    two weeks of refused fires read as "reactions stopped working" with
    nothing to debug.

    ``autopilot`` marks the SERVER's own pick (see _reaction_autopilot_mood),
    which gets the opposite treatment: it falls back to a mood that exists and
    otherwise says nothing at all. See :func:`_fire_autopilot_reaction`.
    """
    bot = config.get_bot(bot_id) if bot_id else None
    actor = bot.name if bot else "Assistant"
    if autopilot:
        for rid in ids:
            await _fire_autopilot_reaction(rid, thread_id, bot_id, actor)
        return
    for rid in ids:
        try:
            await fire_reaction(rid, actor=actor, actor_kind="agent",
                                thread_id=thread_id, bot_id=bot_id)
        except reactions.ReactionError as e:
            log.info("reaction %r from %s not fired: %s", rid, actor, e.message)
            reactions.note_fire_failure(rid, e.message, actor=actor)
            await _note_reaction_refusal(thread_id, rid, e.message)
        except Exception:
            log.exception("failed to fire reaction %r", rid)
            reactions.note_fire_failure(rid, "internal error", actor=actor)
            await _note_reaction_refusal(thread_id, rid, "internal error")


_AUTOPILOT_FALLBACK_MOODS = ("thinking", "random")


async def _fire_autopilot_reaction(mood: str, thread_id: str,
                                   bot_id: str | None, actor: str) -> None:
    """Fire the server's OWN reaction pick — quietly, or not at all.

    Autopilot chooses a mood heuristically (`morning` / `task_complete` /
    `thinking`), but a pool only holds the moods someone actually generated —
    a stock install seeds `thinking` alone. Firing the chosen name blindly
    therefore 404'd on most replies and, worse, left a visible
    "⚠️ Reaction 'task_complete' didn't fire" row under EVERY one of them:
    the server's own misfire spamming the family's chat.

    So autopilot degrades instead: try the mood, then `thinking`, then a
    `random` draw, skipping any candidate that does not resolve, and if none
    does, say nothing. A refusal is a log line and a health counter, never a
    chat row — the visible-refusal behaviour is for a BOT's explicit marker,
    where the agent asked for something specific and deserves to be told.
    """
    for cand in dict.fromkeys((mood, *_AUTOPILOT_FALLBACK_MOODS)):
        try:
            if reactions.get(cand, bot_id=bot_id) is None:
                continue               # nothing on the shelf under that name
        except Exception:
            log.exception("autopilot could not probe reaction %r", cand)
            continue
        try:
            await fire_reaction(cand, actor=actor, actor_kind="agent",
                                thread_id=thread_id, bot_id=bot_id)
            return
        except reactions.ReactionError as e:
            log.info("autopilot reaction %r not fired: %s", cand, e.message)
            reactions.note_fire_failure(cand, e.message, actor=actor)
        except Exception:
            log.exception("autopilot failed to fire reaction %r", cand)
            return
    # Exhausted: none of the candidates resolved for this bot. A log line alone
    # made an autopilot that never fires indistinguishable from one that is
    # simply keeping quiet on purpose, so it also ticks the health counter that
    # /api/health already reports as `reaction_fire_failures_24h`. Still no chat
    # row — a server-chosen mood missing is not the family's problem.
    log.info("reaction autopilot: no usable mood for %s (%s)", bot_id, thread_id)
    reactions.note_fire_failure(mood, "autopilot: no usable mood", actor=actor)


async def _note_reaction_refusal(thread_id: str, rid: str, reason: str) -> None:
    """A collapsed sub row marking a reaction that did not fire.

    Sub rows render as collapsed working-output — quiet enough for the family
    view, visible enough that the agent's next transcript read (and the owner)
    can see WHY the picture never appeared. Best effort: a failure to note a
    failure must not cascade.
    """
    with contextlib.suppress(Exception):
        await _persist_and_broadcast_message(
            thread_id, "assistant",
            f"⚠️ Reaction '{rid}' didn't fire: {reason}",
            metadata={"sub": True})


# --------------------------------------------------------------------------- #
# Image jobs — a placeholder now, the picture when it lands
#
# The agent's whole involvement is one POST. Everything below is deterministic:
# write a real message saying a picture is coming, enqueue the render, poll it,
# fetch it, prove it is an image, ingest it through the same path every other
# picture takes, and rewrite that message. On any failure — a refusal from the
# rig, a corrupt file, ten minutes gone — the same message becomes a visible
# ⚠️ line instead. There is no branch that leaves it saying "pending".
#
# See app/image_jobs.py for the rig client and the rules; this half owns the
# message, the broadcast and the Safe-Mode scoping.
# --------------------------------------------------------------------------- #

#: How often the worker sweeps open jobs.
_IMAGE_JOB_TICK_S = 5.0
#: How many rig calls one sweep may have in flight. Matches the rig rule of
#: thumb ("keep parallel calls ≤ 2–3") and, more to the point, stops N jobs
#: against a down rig from costing N connect timeouts in series.
_IMAGE_JOB_CONCURRENCY = 3

#: How many renders may be OUTSTANDING on the image server at once, across
#: every bot and thread. The server is one serial ComfyUI: one rendering plus a
#: short queue behind it is the most that can finish inside a job's deadline,
#: and anything past that is just building a backlog that will time out. This
#: is a QUEUE, not a limit -- a held job keeps its placeholder and goes on the
#: next tick.
MAX_OPEN_RENDERS = 4
#: Set by anything that just changed what the worker should look at (a new
#: job, a rig callback) so the sweep runs NOW instead of at the next tick —
#: the tick was the only DisPatch-side latency on the whole path.
_image_job_wake: asyncio.Event | None = None
#: Jobs currently being advanced. A rig callback and the sweep can both hold
#: the same row at once; whichever is second must wait for the first, or the
#: job is enqueued twice / delivered twice.
_image_jobs_in_flight: set[str] = set()


def _wake_image_jobs() -> None:
    global _image_job_wake
    if _image_job_wake is None:
        _image_job_wake = asyncio.Event()
    _image_job_wake.set()


def _image_jobs_configured() -> bool:
    """Is the feature switched on AND pointed at a server?

    Both halves matter: `DISPATCH_IMAGE_JOBS=1` on a box with no endpoint would
    otherwise advertise a route that can only ever fail, and "auto" already
    keys off the endpoint being set.
    """
    return bool(SETTINGS.image_jobs_enabled and SETTINGS.clawforge_url)


_clawforge_client: image_jobs.ClawForge | None = None


def _clawforge() -> image_jobs.ClawForge:
    """The ONE rig client. Its MCP session and its unreachable-breaker are
    only worth anything if every job in the sweep shares them."""
    global _clawforge_client
    c = _clawforge_client
    if (c is None or c.url != SETTINGS.clawforge_url
            or c.files_url != (SETTINGS.clawforge_files_url or c.files_url)):
        c = image_jobs.ClawForge(SETTINGS.clawforge_url,
                                 files_url=SETTINGS.clawforge_files_url,
                                 client_name="dispatch")
        _clawforge_client = c
    return c


def _image_job_waiting_text(spec: image_jobs.ImageSpec, why: str,
                            kind: str = "unreachable") -> str:
    """The pending line while the render cannot start: still pending, but
    honest about WHY. Same shape as the pending text so the card is unchanged.

    Three different waits, three different sentences, because they are three
    different things to a reader: the rig is down, the rig is BOOKED by
    somebody else, or our own renders are queued behind each other. "Waiting
    for the image rig (gpu_leased)" would technically be true and would tell a
    human nothing they could act on.
    """
    what = spec.caption or spec.prompt
    tail = f" — {what[:80]}" if what else ""
    if kind == "reserved":
        return (f"🖼️ The image rig is reserved by {why} — waiting for it to "
                f"free up…{tail}")
    if kind == "quota":
        return f"🖼️ Waiting for a free slot on the image rig…{tail}"
    return f"🖼️ Waiting for the image rig ({why})…{tail}"


#: A short human phrase per coded refusal, for the pending line. Without one,
#: the placeholder took the rig's own sentence and normalised it to the text
#: after its last colon — which on a live staging run produced
#: "🖼️ Waiting for the image rig (llama-server.exe)…", a process name on a
#: Windows box, in a family chat, as the explanation for a missing picture.
_WAITING_WORDS = {
    "insufficient_vram": "the GPUs are busy",
    "backend_unavailable": "the renderer is starting up",
    "captioner_unavailable": "the captioner is busy",
}


def _waiting_words(e: image_jobs.ImageJobError) -> str:
    return _WAITING_WORDS.get(e.code) or e.message


def _image_job_reserved_text(reservation: str) -> str:
    """The ending for a booking we are not going to sit through.

    A benchmark holds every card for hours; the deadline would run out first
    and the reader would get a ⚠️ that blamed the render. This says what
    actually happened, and that the answer is to ask again later.

    Built from the holder and the kind, NOT from the rig's own sentence: that
    sentence is free prose and has been seen to name the rig's internal
    ComfyUI URL, and this line lands in a family thread. The rig's wording
    goes to the journal.
    """
    return (f"🗓️ The image rig is reserved by {reservation or 'another job'} "
            f"— no picture this time; ask again once it is free.")


def _image_job_backend_shape_text() -> str:
    """The ending for a workflow the rig's renderer is not set up to run.

    `[multi_gpu_required]`: the workflow's graph splits its work across more
    GPUs than the rig's ComfyUI was given. Two reasons this gets its own
    sentence rather than the generic `⚠️ image failed: <reason>`:

    First, the rig's own sentence is rig-admin prose — it names the workflow,
    the card counts, `gpu_multi_mode` and a `comfy_control(action="repin", …)`
    call. That is an instruction for whoever administers the rig, and this line
    lands in a family thread where nobody can act on it. Same reasoning as
    `_image_job_reserved_text`: build the sentence from what we know, and let
    the rig's wording go to the journal.

    Second, the code READS like `insufficient_vram` — and a reader who has seen
    that one learns "ask for something simpler", which is exactly wrong here.
    The picture was not too big; the renderer is set up for one card and this
    workflow wants more. So the sentence must not invite a smaller retry.

    It does move the health counter (unlike a booked rig): a renderer pinned to
    the wrong shape is a setup problem somebody should see, not contention that
    clears on its own.
    """
    return ("⚠️ That picture needs the image rig set up differently than it is "
            "right now, so it could not be made. Nothing you can change on "
            "this end — worth flagging to whoever looks after the image rig.")


def _image_job_pending_text(spec: image_jobs.ImageSpec) -> str:
    """What the thread says while the render is in flight.

    Deliberately readable on its own: this exact string is what a No-Image-Mode
    device and a Safe-Mode device render (they get no card), and it is what
    lands in the thread list preview. "Generating an image…" with nothing else
    is a worse trace than one that says what of."""
    what = spec.caption or spec.prompt
    tail = f" — {what[:80]}" if what else ""
    return f"🖼️ Generating an image…{tail}"


def _image_job_failed_text(reason: str) -> str:
    return f"⚠️ image failed: {reason}"


def _image_job_cancelled_text(reason: str = "") -> str:
    """A withdrawn render is not a failure, and must not read as one.

    It happens when the deadline is up, when the thread was deleted under the
    job, or when somebody pressed Interrupt on the rig — three things a reader
    would rather see named than see dressed up as the GPU letting them down.
    """
    tail = f" — {reason}" if reason else ""
    return f"✋ The image was cancelled on the rig.{tail}"


def _image_job_superseded_text() -> str:
    """The quiet ending for a placeholder a newer request overtook.

    Not a failure, not a withdrawal somebody chose against their will, and
    emphatically not a refusal: in a fast exchange the scene simply changed
    before the rig ever picked the old one up, so the picture that was coming
    is not the picture anybody still wants. No warning glyph, and — the whole
    point — no NEW row in the thread: coalescing a stale request onto the
    newest one is normal operation, and dressing it up as an error would train
    a reader to worry about something that worked exactly as intended.
    """
    return "🖼️ (superseded by a newer picture)"


def _compose_pic_prompt(bot, scene: str, caption: str) -> tuple[str, str]:
    """The rig-bound prompt and the chat-visible caption for one `[[pic:…]]`.

    A bot with `image_identity_source` writes the SCENE ONLY — the caller
    passes exactly what was inside the marker, e.g. "kneeling by the window,
    morning light" — and this prepends the bot's canonical identity block (see
    `image_jobs.identity_prompt`) so the render still looks like the character
    without the bot ever having to restate its own appearance.

    The identity block does NOT reach the visible "generating…" line. Without
    a caption of its own that line falls back to `spec.prompt` (see
    `_image_job_pending_text`), and an unabridged canonical-identity paragraph
    landing in a family chat bubble would be exactly the leak this feature
    must not create — so a bot that left the caption blank gets the SCENE as
    its caption instead, never the composed prompt.

    A bot with no `image_identity_source` gets `(scene, caption)` back
    unchanged — this is the byte-identical-behaviour guarantee for every bot
    that has not opted in.
    """
    if not bot.image_identity_source:
        return scene, caption
    identity = image_jobs.identity_prompt(bot.image_identity_source)
    room = max(image_jobs.MAX_PROMPT_CHARS - len(identity) - 2, 0)
    trimmed_scene = scene[:room]
    full_prompt = f"{identity}, {trimmed_scene}" if trimmed_scene else identity
    display_caption = caption or scene[:image_jobs.MAX_CAPTION_CHARS]
    return full_prompt, display_caption


#: The MESSAGE's displayed status for a superseded job — deliberately NOT
#: `image_jobs.CANCELLED` (see `_fail_image_job`'s `visible_status`/`sub`):
#: the DB row still ends CANCELLED for every bookkeeping purpose, but
#: `imagejobs.js` renders any recognised non-pending status as a failure card,
#: and a supersede is normal operation, not a failure.
_IMAGE_JOB_SUPERSEDED_STATUS = "superseded"


async def _supersede_stale_pic_jobs(bot_id: str, thread_id: str) -> int:
    """Quietly end this bot's not-yet-actually-rendering jobs in this thread.
    Returns how many were ended.

    Called before a NEW `[[pic:…]]`/`/api/image-jobs` request is admitted, so
    a fast exchange (the roleplay case this exists for) coalesces onto the
    newest scene instead of piling up stale placeholders or hitting the rate
    limiter's hard, final refusal.

    "Unstarted" is NOT the same as "still QUEUED in our own db". `enqueue()`
    answers in about a second and a submit wakes the sweep immediately
    (`_wake_image_jobs`), so a row spends only ~1s locally QUEUED before
    becoming RUNNING — on a shared, serial renderer the real backlog is
    RUNNING rows the RIG itself has not started yet. `_job_unstarted_on_rig`
    is what tells those apart from a job actually generating: only a job that
    is either still QUEUED here, or RUNNING but not yet rendering on the rig,
    is fair game. One the rig IS rendering has already spent GPU minutes, and
    cancelling it would waste them — it is asked to withdraw via
    `_cancel_on_rig` when it was handed to the rig at all, but the local
    ending happens either way (that call is best-effort by contract; see
    `_cancel_on_rig`'s own docstring, and this module's report for what could
    and could not be verified against the live rig from here).

    "Silent" per the design: this ends the job through the same terminal path
    every other ending uses (so the placeholder is always rewritten, never
    left to just stop changing), but with a distinct visible status and no
    new row anywhere — see `_fail_image_job`'s `visible_status`/`sub`.
    Superseding a stale request is normal operation, not an error, and must
    not read as one.
    """
    jobs = await db.open_image_jobs_for_thread(thread_id)
    superseded = 0
    for job in jobs:
        if (job.get("bot_id") or "").lower() != bot_id.lower():
            continue
        if job["state"] == image_jobs.QUEUED:
            pass
        elif job["state"] == image_jobs.RUNNING and _job_unstarted_on_rig(job):
            await _cancel_on_rig(job, "superseded by a newer request")
        else:
            continue
        await _fail_image_job(
            job, "superseded by a newer request",
            state=image_jobs.CANCELLED,
            text=_image_job_superseded_text(), counts=False,
            visible_status=_IMAGE_JOB_SUPERSEDED_STATUS, sub=True)
        superseded += 1
    return superseded


def _image_job_meta(job_id: str, status: str, spec: image_jobs.ImageSpec,
                    *, error: str = "", progress: dict | None = None,
                    seed: int | None = None) -> dict:
    meta = {"kind": _IMAGE_JOB_KIND, "job_id": job_id, "status": status,
            "prompt": spec.prompt[:200], "caption": spec.caption}
    if spec.workflow:
        meta["workflow"] = spec.workflow
    if error:
        meta["error"] = error
    if progress:
        meta["progress"] = progress
    if seed is not None:
        # Not in the visible text: it is machinery for "again, but…", and a
        # ten-digit number in a chat bubble is noise to everyone else.
        meta["seed"] = seed
    return meta


def _image_job_callback_url(job_id: str) -> str:
    """Where the rig should POST when this job finishes, or "" for no callback.

    Empty unless an operator has said what DisPatch's address looks like from
    the rig — it cannot be derived here, and a guessed one would either fail
    silently or point the rig at something that is not us.
    """
    base = SETTINGS.callback_base
    return f"{base}/api/image-jobs/{job_id}/callback" if base else ""


async def _start_image_job(thread_id: str, bot, spec: image_jobs.ImageSpec
                           ) -> tuple[str, str] | None:
    """Placeholder message + job row for one accepted request.

    Returns ``(job_id, message_id)``, or None when the placeholder could not be
    written. Shared by `POST /api/image-jobs` and the inline `[[pic:…]]`
    marker so the two cannot drift: everything above this call is a gate,
    everything below it is the worker, and the order in between is load-bearing.

    The placeholder is persisted BEFORE the row that will rewrite it, so a
    crash in between leaves a message with no job (visible, wrong, and fixable)
    rather than a job pointing at a message that does not exist (invisible, and
    the worker's rewrite would silently no-op forever). The caller's rate-limit
    slot is refunded on failure — nothing was spent.
    """
    job_id = image_jobs.new_job_id()
    # One token per job, minted here whether or not callbacks are configured:
    # the arming decision belongs to the submit call (which only sends a URL
    # when there is a base), and a row that always has a token cannot grow a
    # "callbacks were switched on mid-flight, this job has none" case.
    callback_token = secrets.token_urlsafe(24)
    try:
        msg = await _persist_and_broadcast_message(
            thread_id, "assistant", _image_job_pending_text(spec),
            metadata=_image_job_meta(job_id, image_jobs.QUEUED, spec))
    except Exception:
        image_jobs.limiter.refund(bot.id.lower())
        log.exception("could not write the image-job placeholder")
        return None
    await db.add_image_job(job_id, msg.id, thread_id, bot.id, spec.to_json(),
                           callback_token=callback_token)
    _wake_image_jobs()
    return job_id, msg.id


async def _pic_specs_from_markers(content: str, bot_id: str | None,
                                  thread_id: str, *,
                                  metadata: dict | None = None,
                                  replaying: bool = False,
                                  ) -> tuple[str, list[image_jobs.ImageSpec], list[str]]:
    """Strip `[[pic:…]]` from an assistant reply and decide what it earned.

    The STRIP is unconditional — marker syntax must never reach a chat bubble,
    whatever the guards decide — and every guard below ends the same way: the
    text persists clean, one line goes to the journal, and nothing is created.
    A refusal that ate the reply's text, or one that answered the model, would
    both be worse than a picture that does not arrive.

    Returns ``(clean_text, specs, drops)``. A DROP is a marker that was found
    and produced nothing, and it is returned rather than merely logged for the
    reason reactions grew the same thing in August: the marker is stripped
    either way, the model is told nothing either way, and the model's own
    skill forbids it from mentioning pictures — so a dropped marker with only
    a journal line is a request that vanished without anybody, human or agent,
    being able to tell it ever existed. The house calls that class of defect
    "a failure that looks like success".

    Deliberately NOT a drop: the guards above the loop (a replay, a sub row,
    no image server, a bot without the capability). Those are decisions about
    the whole message, they are correct, and a chat row for each would be
    noise — the same line reactions' autopilot draws between a server-chosen
    miss and a bot's explicit ask.

    Before the rate limiter runs, a marker first COALESCES onto this bot's own
    not-yet-actually-rendering jobs in this thread (see
    `_supersede_stale_pic_jobs`) — a fast exchange replaces a stale render
    with the current scene instead of burning through the limiter's window
    and then hitting its final refusal. Every slot freed that way is
    refunded, so coalescing never costs a bot capacity it would otherwise
    have had for a genuinely new request.
    """
    content, markers = image_jobs.extract_pic_markers(content)
    if not markers:
        return content, [], []
    meta = metadata or {}
    if replaying:
        # Same rule as reaction markers: replaying HISTORY has no side effects.
        log.info("suppressed %d image marker(s) on a recovered message",
                 len(markers))
        return content, [], []
    if meta.get("sub") or meta.get("kind") == "image_job":
        # A placeholder is a receipt for a render, not a reply — letting one
        # spawn renders of its own is a loop with a GPU on the end of it.
        return content, [], []
    if not _image_jobs_configured():
        log.info("ignored %d image marker(s): no image server configured",
                 len(markers))
        return content, [], []
    bot = config.resolve_bot(bot_id)
    if bot is None or not bot.image_jobs:
        log.info("ignored %d image marker(s): not enabled for %s",
                 len(markers), bot_id)
        return content, [], []

    specs: list[image_jobs.ImageSpec] = []
    drops: list[str] = []
    for prompt, caption in markers:
        # CLAMPED, not rejected. A 2,500-character styled prompt is a model
        # doing its job well, and truncating it renders a picture while
        # refusing it renders nothing at all — the marker is already stripped
        # by the time we find out, so "nothing" is genuinely nothing.
        clamped = prompt[:image_jobs.MAX_PROMPT_CHARS]
        if len(prompt) > len(clamped):
            log.info("clamped an image prompt from %s: %d -> %d chars",
                     bot.id, len(prompt), len(clamped))

        # BUILD THE REPLACEMENT FIRST, retire the old one second. Composing can
        # fail — a mis-typed marker, or for an identity-injected bot a missing
        # or mangled identity file — and superseding before we know we have
        # something to put in its place kills the queued picture and delivers
        # nothing, leaving the reader a "(superseded by a newer picture)" line
        # with no newer picture behind it. A supersede that hides a real drop
        # is the house's "failure that looks like success", built by hand.
        try:
            full_prompt, display_caption = _compose_pic_prompt(
                bot, clamped, caption)
            spec = image_jobs.ImageSpec(
                prompt=full_prompt, caption=display_caption,
                workflow=bot.image_workflow, ratio=bot.image_ratio,
                # Explicit, not the dataclass default: this is the bot
                # decorating its own reply, nobody is waiting on it, and the
                # rig contract reserves tier 1 for work a person IS waiting
                # on. Background, always.
                priority=3)
        except (ValueError, image_jobs.ImageJobError) as e:
            # Dropped, not fatal: the reply it arrived in is already sanitized
            # and about to be persisted. Nothing was superseded and no limiter
            # slot was spent, so there is nothing to refund.
            log.info("ignored an image marker from %s: %s", bot.id, e)
            drops.append(str(e))
            continue

        # Now there is a real replacement. Coalescing stays AHEAD of the
        # limiter (and refunds what it frees) so a fast exchange replaces a
        # stale render instead of burning the window and hitting the final
        # refusal — which is the whole point of coalescing.
        freed = await _supersede_stale_pic_jobs(bot.id, thread_id)
        for _ in range(freed):
            image_jobs.limiter.refund(bot.id.lower())
        if refusal := image_jobs.limiter.check(bot.id.lower()):
            log.info("image marker refused by the rate limiter (%s)", bot.id)
            drops.append(refusal)
            break
        specs.append(spec)
    return content, specs, drops


async def _start_pic_jobs(specs: list, thread_id: str,
                          bot_id: str | None, *, drops: Sequence = ()) -> None:
    """Start the jobs a persisted message's markers earned — and mark the ones
    it did not.

    Called by the persist chokepoints AFTER the reply itself exists, so the
    placeholder always lands under the sentence that asked for it.
    """
    for reason in drops:
        await _note_pic_refusal(thread_id, reason)
    bot = config.resolve_bot(bot_id)
    if bot is None:
        return
    for spec in specs:
        if await _start_image_job(thread_id, bot, spec) is None:
            break


async def _note_pic_refusal(thread_id: str, reason: str) -> None:
    """A collapsed sub row marking a `[[pic:…]]` that earned no picture.

    Same shape and the same reasoning as the reaction refusal row next door:
    quiet enough for the family view, visible enough that the owner and the
    agent's next transcript read can see why no picture appeared. Best effort
    — a failure to note a failure must not cascade — and it also ticks the
    health counter, so a bot dropping markers all day is a number an operator
    can see without reading the journal.
    """
    image_jobs.note_marker_drop(reason)
    with contextlib.suppress(Exception):
        await _persist_and_broadcast_message(
            thread_id, "assistant",
            f"⚠️ No picture: {image_jobs._clip(reason, 160)}",
            metadata={"sub": True})


async def _broadcast_message_update(msg: MessageOut, thread_id: str) -> None:
    """Tell open clients that an EXISTING message changed.

    A second `message` frame would append a duplicate bubble, so this is its
    own type — and being its own type means it had to be added to
    _DECOY_FRAME_ALLOW deliberately, which is the point of that list being
    default-deny. It carries the same payload as `message` and is redacted by
    the same rules.
    """
    bot_id = await _bot_of_thread(thread_id)
    await manager.broadcast({"type": "message_update", "thread_id": thread_id,
                             "bot_id": bot_id, "message_id": msg.id,
                             "message": msg.model_dump()})


async def _rewrite_image_job_message(job: dict, content: str,
                                     metadata: dict) -> None:
    """Persist a new body for the placeholder and push it to open clients.

    Both halves or neither, as far as the operator is concerned: a write that
    lands but is not broadcast leaves every open tab showing a stale spinner
    until it reloads, which is the same failure this feature exists to avoid.
    The broadcast is best-effort *after* the durable write, so a dead WS
    connection cannot roll back the row.
    """
    mid = job["message_id"]
    await db.update_message_content(mid, content)
    await db.update_message_metadata(mid, metadata)
    msg = await db.get_message(mid)
    if msg is not None:
        with contextlib.suppress(Exception):
            await _broadcast_message_update(msg, job["thread_id"])
        # The thread list previews `last_message`, and the placeholder is
        # usually the newest row — without this the list keeps reading
        # "Generating an image…" after the picture landed (seen on staging
        # 2026-09-02; the bubble itself was fine).
        with contextlib.suppress(Exception):
            await _broadcast_thread_update(job["thread_id"])


async def _fail_image_job(job: dict, reason: str, *,
                          state: str = image_jobs.FAILED,
                          text: str = "", counts: bool | None = None,
                          visible_status: str = "", sub: bool = False) -> None:
    """Terminal ending: the row, the message and the counter, in that order.

    `state` is the third terminal value, cancelled, taking the same path — the
    bookkeeping is identical and only the wording differs, so splitting it into
    a second function is how the two drift.

    `text` overrides the sentence for an ending that is neither a fault nor a
    withdrawal (a rig booked by somebody else), and `counts` overrides whether
    it moves the /api/health failure counter — that number exists to make a rig
    going WRONG visible, and a rig that is merely busy is not that.

    `visible_status`/`sub` split the MESSAGE's displayed ending from the DB
    row's real one — used by a supersede (see `_supersede_stale_pic_jobs`),
    which is a CANCELLED row for every bookkeeping purpose (audit, counters,
    TERMINAL_STATES) but must not draw the frontend's `is-failed` card a plain
    `cancelled` status would: `imagejobs.js` only recognises a fixed status
    vocabulary and falls through to a plain, uncarded row for anything else,
    so a distinct status string ("superseded") is what keeps it from reading
    as a failure. `sub` additionally collapses it to a one-line trace, the
    same treatment a reaction refusal gets.
    """
    reason = image_jobs._clip(reason, 200)
    spec = _image_job_spec(job)
    cancelled = state == image_jobs.CANCELLED
    text = text or (_image_job_cancelled_text(reason) if cancelled
                    else _image_job_failed_text(reason))
    if not await db.update_image_job(job["id"], state=state, error=reason,
                                     require_open=True):
        # Somebody else ended this job first (a delivery that was inside
        # `fetch()` when the deadline came round), or the placeholder was
        # deleted under us. Either way the message must NOT be rewritten:
        # that is how a delivered picture became "⚠️ image failed: timed out".
        log.info("image job %s: already closed, not writing %r over it",
                 job["id"], state)
        return
    status = visible_status or state
    meta = ({"sub": True, "kind": _IMAGE_JOB_KIND, "status": status,
            "job_id": job["id"]} if sub else
           _image_job_meta(job["id"], status, spec, error=reason,
                           seed=job.get("seed")))
    await _rewrite_image_job_message(job, text, meta)
    # A cancellation is an outcome somebody chose, not the rig letting us
    # down, so it deliberately does NOT move the failure counter /api/health
    # exposes — that number exists to make a rig going wrong visible.
    if counts if counts is not None else not cancelled:
        image_jobs.note_failure(job["id"], reason, bot=job.get("bot_id", ""))
    log.info("image job %s %s: %s", job["id"], state, reason)


async def _reserve_image_job(job: dict, reservation: str, detail: str = "") -> None:
    """End a job because the rig is BOOKED by somebody else.

    Terminal, and deliberately not dressed as a fault: nothing went wrong, the
    GPUs are promised to a benchmark for the next few hours. It therefore does
    not move the failure counter either — that number is for a rig going
    wrong, and a night of benchmarking would otherwise light it up.
    """
    log.info("image job %s: standing down, the rig is reserved by %s (%s)",
             job["id"], reservation or "another job", detail or "—")
    # A machine-readable marker for this ending, in the progress block (which
    # `GET /api/image-jobs/<id>` already returns) rather than a new column: a
    # cron caller must be able to tell "the rig was booked, ask later" from
    # "the render failed" without matching on our prose.
    prog = {**_without_waiting(_image_job_progress(job) or {}),
            "reserved": reservation or "another job"}
    await db.update_image_job(job["id"], progress=json.dumps(prog))
    job["progress"] = json.dumps(prog)
    await _fail_image_job(
        job, f"the rig is reserved by {reservation or 'another job'}",
        text=_image_job_reserved_text(reservation), counts=False)


async def _repin_for_vram(job: dict, forge) -> bool:
    """Clear an `insufficient_vram` refusal by making the rig repin its card.

    Waiting this one out does not work, which is why it needs its own path.
    The shortage on this rig is a background model sprawled onto the card
    ComfyUI is pinned to, and that clears on the model's own idle TTL (900s
    for the background tier) -- LONGER than a job's 600s deadline. A job that
    politely waits therefore always dies first, and the reader gets a ⚠️ for
    something that a single restart would have fixed in about a minute. The
    standalone picture CLI has done exactly this for months; the marker path
    was the one that just waited.

    ONCE per job, like `_retry_stalled`: the marker lives in the progress
    block, so a rig that is genuinely out of memory still ends visibly instead
    of restarting ComfyUI in a loop.

    Returns True when the job was re-queued and the caller must stop.
    """
    prog = _image_job_progress(job) or {}
    if prog.get("vram_repin"):
        return False
    if _image_job_expired(job):
        return False

    ok, note = await forge.comfy_restart()
    log.info("image job %s: insufficient VRAM; asked the rig to repin (%s: %s)",
             job["id"], "ok" if ok else "failed", note)
    if not ok:
        # The restart itself failed. Still mark the attempt so we do not spin,
        # and fall through to the ordinary retryable-wait path.
        prog = {**prog, "vram_repin": 1}
        await db.update_image_job(job["id"], progress=json.dumps(prog),
                                  require_open=True)
        job["progress"] = json.dumps(prog)
        return False

    prog = {**_without_waiting(prog), "vram_repin": 1}
    if not await db.update_image_job(job["id"], state=image_jobs.QUEUED,
                                     rig_job_id=None, progress=json.dumps(prog),
                                     require_open=True):
        return True          # ended under us (superseded); nothing left to do
    job["state"], job["rig_job_id"] = image_jobs.QUEUED, None
    job["progress"] = json.dumps(prog)
    spec = _image_job_spec(job)
    await _rewrite_image_job_message(
        job, _image_job_pending_text(spec),
        _image_job_meta(job["id"], image_jobs.QUEUED, spec,
                        seed=job.get("seed")))
    _wake_image_jobs()
    return True


async def _retry_stalled(job: dict) -> bool:
    """One more go at a render that moved and then went quiet.

    `[stalled]` is the rig's word for a job that made progress and then stopped
    making it past the ceiling — a wedged sampler rather than a graph that
    cannot run, which is why it is the one failure worth resubmitting. ONCE:
    the marker lives in the progress block (no column, and it survives the
    row), so a rig stalling every attempt still ends visibly rather than
    looping until the deadline.

    Returns True when the job was put back in the queue and the caller must
    stop; False when it has already had its retry and should fail normally.
    """
    prog = _image_job_progress(job) or {}
    if prog.get("stall_retry"):
        return False
    if _image_job_expired(job):
        # No point re-queueing into a deadline that has already passed.
        return False
    prog = {**_without_waiting(prog), "stall_retry": 1}
    await db.update_image_job(job["id"], state=image_jobs.QUEUED,
                              rig_job_id=None, progress=json.dumps(prog))
    job["state"], job["rig_job_id"] = image_jobs.QUEUED, None
    job["progress"] = json.dumps(prog)
    # The card is carrying the step counter of the render that stalled. Put it
    # back to a plain "generating" rather than leaving a frozen 87% on screen
    # for the second attempt, which reads as a hung DisPatch.
    spec = _image_job_spec(job)
    await _rewrite_image_job_message(
        job, _image_job_pending_text(spec),
        _image_job_meta(job["id"], image_jobs.QUEUED, spec,
                        seed=job.get("seed")))
    log.info("image job %s: the rig reported [stalled]; resubmitting once",
             job["id"])
    _wake_image_jobs()
    return True


async def _cancel_on_rig(job: dict, why: str) -> None:
    """Ask the rig to stop rendering. Best effort, and never load-bearing.

    Cancelling is about not burning a GPU on a picture nobody will see; the
    local job is failed by the caller regardless, so every outcome here — a
    refusal, an unreachable rig, an already-finished job — is a log line.
    """
    rig_id = job.get("rig_job_id")
    if not rig_id:
        return
    try:
        await _clawforge().cancel(str(rig_id))
        log.info("image job %s: asked the rig to cancel %s (%s)",
                 job["id"], rig_id, why)
    except Exception as e:
        log.info("image job %s: could not cancel %s on the rig (%s)",
                 job["id"], rig_id, e)


async def _cancel_open_image_jobs(jobs: list[dict], why: str) -> None:
    """Withdraw a set of open jobs whose placeholder is about to disappear.

    The rows are cascaded away with their messages, so failing them locally
    would be writing to something that no longer exists; the point of this is
    purely the rig-side withdrawal.
    """
    for job in jobs:
        await _cancel_on_rig(job, why)


def _image_job_spec(job: dict) -> image_jobs.ImageSpec:
    """The stored spec, or a placeholder one if the row was hand-mangled.

    A job whose spec no longer parses still has a message to rewrite, and
    dying here would strand exactly the row this module promises never to
    strand.
    """
    try:
        return image_jobs.ImageSpec.from_json(job["spec"])
    except Exception:
        return image_jobs.ImageSpec(prompt="(unreadable request)")


async def _deliver_image_job(job: dict, data: bytes, files_rel: str) -> None:
    """Store the bytes, rewrite the placeholder into the picture.

    The file is written into MEDIA_DIR and then run through
    ``_ingest_content_media`` — the same directive rewrite every agent-sent
    picture goes through. That is not ceremony: it is what records the media
    origin, what produces the `/media/<uuid>` URL the lightbox and the dedup
    ledger expect, and what makes No-Image Mode and Safe Mode strip this
    picture by the rules they already have, with no new code on either side.

    Success is defined as the REWRITE happening. If the directive comes back
    unchanged the ingest declined the file, and shipping the raw path would put
    an absolute filesystem path into a chat bubble that renders as a broken
    relative URL — the silent version of this failing.
    """
    spec = _image_job_spec(job)
    suffix = image_jobs.image_suffix(data, files_rel)
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    dest = MEDIA_DIR / f"{uuid.uuid4().hex}{suffix}"

    def _write() -> None:
        tmp = dest.with_name(dest.name + ".part")
        tmp.write_bytes(data)
        os.replace(tmp, dest)          # atomic: a torn file would 404 forever

    await asyncio.to_thread(_write)

    cap = f"|{spec.caption}" if spec.caption else ""
    directive = f"[[media:{dest}{cap}]]"
    content = await asyncio.to_thread(_ingest_content_media, directive)
    if "[[media:/media/" not in content:
        with contextlib.suppress(OSError):
            dest.unlink()
        # The directive itself, in the journal: this branch used to be
        # indistinguishable from a real storage failure, and the one bug that
        # ever reached it was in the directive, not the disk.
        log.warning("image job %s: the ingest declined %r", job["id"], directive)
        await _fail_image_job(job, "the image could not be stored")
        return

    meta = _image_job_meta(job["id"], image_jobs.DONE, spec,
                           seed=job.get("seed"))
    media_url = content.split("[[media:", 1)[1].split("|", 1)[0].rstrip("]")
    meta["media_url"] = media_url
    if not await db.update_image_job(job["id"], state=image_jobs.DONE,
                                     media_url=media_url, error=None,
                                     require_open=True):
        # Nothing left to deliver INTO: the job was ended by the deadline
        # while this fetch was in flight, or the placeholder (and with it, by
        # cascade, the row) was deleted. Throw the bytes away rather than
        # leaving a file nothing references — this used to write the picture,
        # rewrite a message that no longer existed, and log "delivered".
        with contextlib.suppress(OSError):
            dest.unlink()
        log.warning("image job %s: finished, but the job was already closed "
                    "or deleted — discarding the picture", job["id"])
        return
    await _rewrite_image_job_message(job, content, meta)
    log.info("image job %s delivered (%d bytes)", job["id"], len(data))


def _image_job_expired(job: dict) -> bool:
    try:
        started = datetime.fromisoformat(job["created_at"])
    except (TypeError, ValueError):
        return True          # an unreadable timestamp is not a reason to wait
    # `now_iso()` is UTC-aware, but a row written by hand (a repair, a test)
    # may be naive. Comparing the two raises, and an exception here would
    # abandon the sweep — so normalise instead of assuming.
    now = datetime.now(UTC) if started.tzinfo else datetime.now()
    return (now - started).total_seconds() > image_jobs.DEADLINE_S


#: Progress keys the WORKER writes, not the rig: the waiting wording and its
#: flavour, and (see `_note_rig_state`) the rig's own last-reported job state.
#: None of these is the step/percent a reader watches, so none of them may
#: make an unrelated re-poll look like "the visible progress changed".
_WAITING_KEYS = ("waiting", "waiting_kind", "rig_state")


def _without_waiting(progress: dict) -> dict:
    return {k: v for k, v in progress.items() if k not in _WAITING_KEYS}


#: Rig-reported states (PollResult.state) that mean "not actually rendering
#: yet" for a job OUR db already calls RUNNING — still sitting in the rig's
#: OWN queue behind other work. `done`/`failed`/`cancelled` never reach this
#: check: each ends the job through its own path before `_job_unstarted_on_rig`
#: is ever consulted.
_RIG_UNSTARTED_STATES = frozenset({"queued", "pending"})


async def _note_rig_state(job: dict, rig_state: str) -> None:
    """Persist the rig's OWN state for this job into the existing progress
    JSON blob (no schema migration).

    Read only by the coalesce decision (`_job_unstarted_on_rig`): a job the
    rig has not actually started rendering — still queued on ITS side, or
    handed off with no progress reported yet — is fair game to supersede; one
    it is actually generating must be left to finish. Written UNCONDITIONALLY
    on every poll of a RUNNING job, independently of
    `_record_image_job_progress`'s broadcast dedup, so the coalesce decision
    is never working off a rig_state a "nothing visible changed" skip left
    un-persisted.
    """
    if not rig_state:
        return
    stored = _image_job_progress(job) or {}
    if stored.get("rig_state") == rig_state:
        return
    merged = {**stored, "rig_state": rig_state}
    if not await db.update_image_job(job["id"], progress=json.dumps(merged),
                                     require_open=True):
        return          # ended under us (superseded); do not touch it further
    job["progress"] = json.dumps(merged)


def _job_unstarted_on_rig(job: dict) -> bool:
    """True when a RUNNING (in OUR db) job has not actually begun rendering.

    Coalescing a QUEUED job — never even reached the rig — is unconditionally
    safe (see `_supersede_stale_pic_jobs`). A RUNNING job is the harder call
    a design review raised: `enqueue()` answers in about a second and the
    sweep wakes on submission, so a job can sit RUNNING in OUR state machine
    for most of a serial renderer's queue length while the RIG has not
    actually started it — THAT is the real backlog a fast exchange builds,
    not the ~1s a row spends locally QUEUED. This reads the rig's own
    last-reported state (persisted by `_note_rig_state`) to tell the two
    apart: still queued on the rig, or handed off with no progress reported
    yet, is fair game; real step/percent progress means it is actually
    generating, and cancelling it would throw away GPU time already spent.
    """
    prog = _image_job_progress(job) or {}
    if prog.get("rig_state") in _RIG_UNSTARTED_STATES:
        return True
    step, percent = prog.get("step"), prog.get("percent")
    has_progress = ((isinstance(step, int) and not isinstance(step, bool) and step > 0)
                    or (isinstance(percent, (int, float))
                        and not isinstance(percent, bool) and percent > 0))
    return not has_progress


async def _record_image_job_progress(job: dict, progress: dict | None) -> None:
    """Store the rig's step counter and show it, but only when it CHANGED.

    Every write here is a message metadata update plus a broadcast to every
    open device, and the poll runs every few seconds for up to ten minutes —
    so re-publishing an unchanged number would be a steady stream of frames
    that redraw the same card. The metadata is rebuilt rather than patched:
    `update_message_metadata` replaces wholesale, so a partial dict would drop
    the prompt, the caption and the workflow off the placeholder.
    """
    stored = _image_job_progress(job) or {}
    if not progress:
        if not stored.get("waiting"):
            return
        # Reachable again but no step counter yet: drop the waiting wording.
        progress = _without_waiting(stored)
    elif progress == _without_waiting(stored):
        if not stored.get("waiting"):
            return
    # A marker the worker owns rather than the rig (the one stall retry this
    # job is allowed) has to survive the rig's own progress block replacing it.
    if "stall_retry" in stored:
        progress = {**progress, "stall_retry": stored["stall_retry"]}
    # CAS: if a supersede collapsed this row while we were inside forge.poll(),
    # rewriting it from our stale in-memory state would restore the pending
    # text AND the full meta (prompt, caption, workflow) on a row no sweep will
    # ever touch again -- a permanent "Generating an image… 46%" with the
    # prompt on screen. A failure that looks like progress.
    if not await db.update_image_job(job["id"], progress=json.dumps(progress),
                                     require_open=True):
        return
    await _rewrite_image_job_message(
        job, _image_job_pending_text(_image_job_spec(job)),
        _image_job_meta(job["id"], job["state"], _image_job_spec(job),
                        progress=progress, seed=job.get("seed")))
    job["progress"] = json.dumps(progress)


async def _mark_image_job_waiting(job: dict, why: str,
                                  kind: str = "unreachable") -> None:
    """Rewrite the placeholder to say why the render has not started — once
    per REASON, not once per tick (the marker is `waiting` in the progress
    block).

    Keyed on the stored value rather than on its mere presence: a wait that
    began as "the rig is unreachable" and became "the rig is reserved by
    bench-runner" is new information, and a reader watching the bubble should
    get it. The same wait repeating is not, and stays silent.
    """
    prog = _image_job_progress(job) or {}
    # Only the transport wording needs trimming ("image server unreachable:
    # ConnectError (backing off)" -> "ConnectError"). A reservation is already
    # short and its parenthesis is the lease KIND, which is the informative
    # half — cutting at "(" would turn "bench-runner (benchmark)" into
    # "bench-runner" and lose the reason a reader would care about.
    short = (why.split(":", 1)[-1].split("(")[0].strip() or "unreachable"
             if kind == "unreachable" else image_jobs._clip(why, 80))
    if prog.get("waiting") == short and prog.get("waiting_kind") == kind:
        return
    spec = _image_job_spec(job)
    prog = {**prog, "waiting": short, "waiting_kind": kind}
    if not await db.update_image_job(job["id"], progress=json.dumps(prog),
                                     require_open=True):
        return          # same race as _record_image_job_progress above
    job["progress"] = json.dumps(prog)
    await _rewrite_image_job_message(
        job, _image_job_waiting_text(spec, short, kind),
        _image_job_meta(job["id"], job["state"], spec, progress=prog,
                        seed=job.get("seed")))


def _image_job_progress(job: dict) -> dict | None:
    """The stored progress block, or None if there is none / it is junk."""
    try:
        stored = json.loads(job.get("progress") or "null")
    except (TypeError, ValueError):
        return None
    return stored if isinstance(stored, dict) else None


async def _advance_image_job(job: dict) -> None:
    """Move one job one step. Never raises — the loop must survive a bad row.

    One advancer per job at a time: a second caller (the callback route while
    the sweep holds the row, or vice versa) returns at once. The row is
    re-read by the next sweep anyway, and the callback's poke has already
    been honoured by whoever is in there.
    """
    if job["id"] in _image_jobs_in_flight:
        return
    _image_jobs_in_flight.add(job["id"])
    try:
        if _image_job_expired(job):
            # INSIDE the guard, deliberately. This check used to sit in the
            # sweep, outside it, so a fetch that spanned the deadline (the
            # fetch timeout is 180 s of the 600) raced its own delivery: one
            # coroutine wrote the picture, the other wrote "timed out", and
            # whichever landed second was what the thread said.
            #
            # Withdraw it first: past the deadline nobody is going to be shown
            # this picture, and a render left running holds a GPU the next
            # request wants. The local ending does not wait on the answer.
            await _cancel_on_rig(job, "deadline")
            await _fail_image_job(
                job, f"timed out after {image_jobs.DEADLINE_S // 60} minutes")
            return
        await _advance_image_job_locked(job)
    finally:
        _image_jobs_in_flight.discard(job["id"])


async def _advance_image_job_locked(job: dict) -> None:
    forge = _clawforge()
    spec = _image_job_spec(job)
    try:
        if job["state"] == image_jobs.QUEUED:
            # Ask the rig whether a submit would run at all before making one.
            # One cached `comfy_status` serves the whole sweep, and it is the
            # rig's own answer rather than an inference from `comfy.running` —
            # which says "false" routinely on a rig that renders fine, because
            # ClawForge unloads the backend when idle and starts it on the next
            # job. Fails OPEN: an unknown answer submits as before.
            ready = await forge.readiness()
            if ready.blocked:
                if ready.stand_down:
                    # A benchmark holds every card for hours. Waiting it out
                    # means a spinner that runs the full deadline and then
                    # blames the render, so end it now and say who has the rig.
                    await _reserve_image_job(job, ready.reservation, ready.detail)
                elif ready.leased:
                    await _mark_image_job_waiting(job, ready.reservation,
                                                  kind="reserved")
                else:
                    await _mark_image_job_waiting(job, ready.reason)
                log.info("image job %s: not submitting — %s (%s)",
                         job["id"], ready.reason, ready.detail or "—")
                return
            try:
                res = await forge.enqueue(
                    spec, callback_url=_image_job_callback_url(job["id"]),
                    callback_token=job.get("callback_token") or "")
            except image_jobs.ImageJobError as e:
                if not e.uncertain:
                    raise
                # The submit left the box and the answer did not come back, so
                # the rig may be rendering this right now. Resubmitting is the
                # tempting move and the wrong one: there is no idempotency key
                # on `generate_image`, so a lost answer inside the deadline
                # could mean up to ten real renders for one placeholder, nine
                # of them with no job id — unpollable, uncancellable, and
                # holding cards on the box's most contended resource.
                #
                # One visible ending beats nine invisible renders.
                log.warning("image job %s: submit answer lost (%s) — not "
                            "sending it a second time", job["id"], e.message)
                await _fail_image_job(
                    job, "the image server did not answer the request; it may "
                         "have started rendering, so it was not asked twice")
                return
            if res.seed is not None:
                # Known at enqueue on this rig, and the only moment it is
                # offered on the fast path — an idle rig delivers the file in
                # the same breath and there is no poll to read it off.
                await db.update_image_job(job["id"], seed=res.seed)
                job["seed"] = res.seed
            if res.files_rel:
                # An idle rig answered with the finished file straight away.
                data = await forge.fetch(res.files_rel)
                await _deliver_image_job(job, data, res.files_rel)
                return
            # CAS, not a blind write. `_supersede_stale_pic_jobs` runs OUTSIDE
            # the in-flight guard, so a marker arriving during this ~0.8s
            # enqueue can cancel this row while we are inside it. A blind write
            # flips the row back to RUNNING with a rig id, the next sweep polls
            # it, and the collapsed "superseded" line silently becomes a
            # picture -- two renders, and the stale prompt is the one delivered.
            if not await db.update_image_job(job["id"], state=image_jobs.RUNNING,
                                             rig_job_id=res.job_id,
                                             require_open=True):
                # It was ended under us. The rig is still holding the job, so
                # tell it to stop; the local row is already terminal.
                await _cancel_on_rig({**job, "rig_job_id": res.job_id},
                                     "superseded during submit")
            return

        rig_id = job.get("rig_job_id")
        if not rig_id:
            # RUNNING with no rig id is not a state the writer can produce; a
            # row in it has been edited by hand or half-written by a crash.
            await _fail_image_job(job, "the render was lost")
            return
        poll = await forge.poll(rig_id)
        # ALWAYS, independent of anything below: the coalesce decision reads
        # this off the row and must never see a stale rig_state a "nothing
        # visible changed" skip elsewhere left un-persisted.
        await _note_rig_state(job, poll.state)
        if poll.seed is not None and job.get("seed") is None:
            await db.update_image_job(job["id"], seed=poll.seed)
            job["seed"] = poll.seed
        if poll.files_rel:
            data = await forge.fetch(poll.files_rel)
            await _deliver_image_job(job, data, poll.files_rel)
            return
        if poll.state == image_jobs.CANCELLED:
            # Reachable WITHOUT DisPatch asking: an operator interrupt on the
            # rig lands here. Checked before poll.error because a cancellation
            # carries an error string of its own, and it is not a failure.
            await _fail_image_job(job, poll.error,
                                  state=image_jobs.CANCELLED)
            return
        if poll.error:
            if poll.code == image_jobs.STALLED and await _retry_stalled(job):
                return
            if poll.code == image_jobs.MULTI_GPU_REQUIRED:
                # Not reachable today — the rig's shape gate runs before it
                # accepts a job, so this arrives as a submit refusal above.
                # Kept so the two paths cannot say different things about the
                # same code if the rig ever moves the check.
                await _fail_image_job(job, poll.error,
                                      text=_image_job_backend_shape_text())
                return
            await _fail_image_job(job, poll.error)
            return
        if poll.done:
            await _fail_image_job(job, "the render finished with no image")
            return
        await _record_image_job_progress(job, poll.progress)
    except image_jobs.ImageJobError as e:
        if e.code == image_jobs.GPU_LEASED and not e.retryable:
            # Somebody else's benchmark owns the cards. The rig's own advice is
            # to stand down rather than retry into a reservation, and a reader
            # is better served by "come back later" now than by a ⚠️ in ten
            # minutes that reads as if the picture went wrong.
            await _reserve_image_job(job, e.reservation, e.message)
            return
        if e.code == image_jobs.MULTI_GPU_REQUIRED:
            # Terminal, and the one refusal whose generic wording would teach
            # the reader the wrong lesson (see `_image_job_backend_shape_text`).
            # Checked before the retryable branch even though the code is not
            # retryable, so a future widening of RETRYABLE_CODES cannot quietly
            # put this one into a poll loop against an answer that cannot move.
            log.warning("image job %s: the rig's ComfyUI is short of GPUs for "
                        "workflow %s (%s)", job["id"],
                        _image_job_spec(job).workflow or "default", e.message)
            await _fail_image_job(job, e.message,
                                  text=_image_job_backend_shape_text())
            return
        if e.retryable and not _image_job_expired(job):
            # The rig is briefly unreachable (a restart, a dropped link), or it
            # is busy in a way it says will pass. Say so once and let the
            # deadline decide — failing on the first blip would turn every rig
            # restart into a thread full of ⚠️ lines. The placeholder does
            # change wording, once, so a reader knows the wait is the rig and
            # not DisPatch — and changes back on progress.
            log.info("image job %s: %s (will retry%s)", job["id"], e.message,
                     f", rig asked for {int(e.retry_after_s)}s"
                     if e.retry_after_s else "")
            if e.code == "insufficient_vram" and await _repin_for_vram(job, forge):
                # Re-queued behind a fresh card; the sweep picks it straight up.
                return
            if e.code == image_jobs.GPU_LEASED:
                await _mark_image_job_waiting(job, e.reservation, kind="reserved")
            elif e.code == image_jobs.CLIENT_QUOTA:
                await _mark_image_job_waiting(job, "quota", kind="quota")
            else:
                await _mark_image_job_waiting(job, _waiting_words(e))
            return
        await _fail_image_job(job, e.message)
    except Exception:
        log.exception("image job %s crashed", job["id"])
        await _fail_image_job(job, "internal error")


async def _image_job_sweep() -> None:
    """One pass over every open job: expire the old, advance the rest.

    Up to ``_IMAGE_JOB_CONCURRENCY`` jobs are advanced at once. The sweep
    used to be strictly serial, which was fine while every call answered in
    70 ms and ruinous when the rig was down: each open job then cost a full
    connect timeout before the next one was even looked at.
    """
    sem = asyncio.Semaphore(_IMAGE_JOB_CONCURRENCY)

    async def one(job: dict) -> None:
        async with sem:
            if _shutting_down:
                return
            # Expiry is checked by `_advance_image_job`, behind the in-flight
            # guard — not here, where it raced the delivery it was ending.
            await _advance_image_job(job)
            # Beat per JOB, not per sweep: one slow multi-megabyte fetch can
            # hold the sweep for up to FETCH_TIMEOUT_S, and a beat only at the
            # end of it made /api/health report this loop as stalled while it
            # was doing exactly its job.
            _loop_beat("image_jobs", _IMAGE_JOB_TICK_S)

    jobs = await db.open_image_jobs()
    if not jobs:
        return

    # ADMISSION CAP. `_IMAGE_JOB_CONCURRENCY` bounds how many rig CALLS this
    # sweep makes at once; it does not bound how many renders are open on the
    # rig, and the rig is ONE serial ComfyUI. Coalescing bounds a single bot in
    # a single thread, but three bots firing per turn is ~9 submits a minute
    # against roughly 1.2 renders a minute, and the first thing to break is the
    # 600s deadline -- a thread of "⚠️ timed out" for pictures that were never
    # going to get a card.
    #
    # So: jobs already ACCEPTED by the rig are always advanced (they are its
    # problem now, and abandoning them would strand real renders), but new
    # submissions are held back once MAX_OPEN_RENDERS are outstanding. A held
    # job keeps its placeholder and is picked up by the next tick -- this is a
    # queue, not a refusal, and nothing is lost.
    accepted = [j for j in jobs if j.get("rig_job_id")]
    waiting = [j for j in jobs if not j.get("rig_job_id")]
    room = max(0, MAX_OPEN_RENDERS - len(accepted))
    if len(waiting) > room:
        held = waiting[room:]
        waiting = waiting[:room]
        log.info("image queue: %d render(s) open on the rig, holding %d "
                 "submission(s) for the next tick", len(accepted), len(held))
    await asyncio.gather(*(one(j) for j in accepted + waiting))


async def _resume_image_jobs() -> None:
    """Startup: adopt what survives a restart, fail out what cannot.

    A crash mid-render leaves rows in `queued`/`running`. The ones the rig
    still holds a job id for are simply picked up by the next sweep — the
    render kept going on the rig, it does not care that we restarted. The rest
    (placeholder written, request never accepted) have nothing to poll and are
    failed here, immediately and visibly, rather than being left to expire ten
    minutes into the new process's life.

    Also fails out everything if the feature has since been switched off: a
    thread must never keep a placeholder alive for a worker that will not run.
    """
    open_jobs = await db.open_image_jobs()
    if not open_jobs:
        return
    for job in open_jobs:
        if not _image_jobs_configured():
            await _fail_image_job(job, "image generation is switched off")
        elif job["state"] == image_jobs.QUEUED or not job.get("rig_job_id"):
            await _fail_image_job(job, "interrupted by a restart")
        elif _image_job_expired(job):
            # Withdraw it too. Failing it locally and leaving it running is how
            # a deploy-time restart leaves renders burning GPUs that nobody is
            # waiting on — on a rig that is usually the contended resource.
            await _cancel_on_rig(job, "expired during downtime")
            await _fail_image_job(job, "timed out while DisPatch was down")
    log.info("image jobs: %d open at startup", len(open_jobs))


async def _image_job_loop() -> None:
    """The worker. One task, one sweep every few seconds, forever.

    Sequential rather than a task per job on purpose: the whole point of the
    rate limit is that a handful of renders can be in flight at once, and a
    sweep of a handful of cheap polls costs less than the bookkeeping to run
    them concurrently — while a single loop can be cancelled cleanly at
    shutdown and can never leak a task per abandoned job.
    """
    global _image_job_wake
    if _image_job_wake is None:
        _image_job_wake = asyncio.Event()
    try:
        await asyncio.sleep(3)
        while True:
            # The health beat says "this loop runs every tick" and the stale
            # factor supplies the slack — reporting tick×4 as the period made
            # /api/health read as a 20 s poll that does not exist.
            _loop_beat("image_jobs", _IMAGE_JOB_TICK_S)
            _image_job_wake.clear()
            try:
                if _image_jobs_configured():
                    await _image_job_sweep()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("image job sweep failed")
            # Sleep a tick, or less if something new arrives.
            try:
                await asyncio.wait_for(_image_job_wake.wait(), _IMAGE_JOB_TICK_S)
            except TimeoutError:
                pass
    except asyncio.CancelledError:
        pass


# --- Rotating pool: keep a fresh batch on hand ------------------------------ #

_pool_lock = asyncio.Lock()
_pool_wake = asyncio.Event()


def _nudge_reaction_pool() -> None:
    """Ask the pool loop to look now (a fire may have crossed the low-water mark)."""
    _pool_wake.set()


async def _broadcast_pool_state(bot_id: str | None = None) -> None:
    """Push pool telemetry to the manager panel.

    One frame, two readings: `pool` is a single bot's status, `pools` maps
    every reactions-enabled bot's id to the same shape. A round that only
    touched one shelf sends just `pool`; the all-bots sweep sends `pools` AND
    a `pool` for the default bot, so a client that only knows the original
    single-pool frame still refreshes instead of silently ignoring the update.
    Every status object names its own bot in `bot_id`.
    """
    with contextlib.suppress(Exception):
        if bot_id:
            await manager.broadcast({"type": "reaction_pool",
                                     "pool": reactions.pool_status(bot_id)})
            return
        pools = {}
        for bid in reactions.reaction_bots():
            # One bot's unreadable pool file must not cost the others their
            # frame — the panel shows what it can and says nothing about it.
            with contextlib.suppress(Exception):
                pools[bid] = reactions.pool_status(bid)
        frame = {"type": "reaction_pool", "pools": pools}
        default = reactions.default_bot_id()
        frame["pool"] = pools.get(default) or reactions.pool_status(default)
        await manager.broadcast(frame)


async def _top_up_pool(max_rounds: int = 8, *, only_low: bool = False,
                       bot_id: str | None = None) -> int:
    """Generate toward the per-mood targets: spin refill rounds until the
    deficits are gone, the rig stops cooperating, or `max_rounds` is hit (each
    round is capped at max_per_cycle). Broadcasts after every round so the
    manager panel ticks up while a long fill is running."""
    made = 0
    for _ in range(max_rounds):
        if _shutting_down:
            break
        n = await asyncio.to_thread(reactions.pool_refill, None, bot_id=bot_id, only_low=only_low)
        if n == 0:              # done, rig unreachable, or every prompt failed
            break
        made += n
        await _broadcast_pool_state(bot_id)
    return made


async def _reaction_pool_cycle() -> None:
    """One pass: sweep stale staging across all bots, run the nightly refill
    for each reaction-enabled bot, honour the low-water mark. Nightly = top-up,
    never discard — one-shot consumption already guarantees a picture can't
    repeat, so unfired images keep their turn."""
    if _shutting_down:
        return
    async with _pool_lock:
        # Sweep every pool on disk, even a disabled one — crash-stranded *.part
        # staging must not strand behind the toggle. (Fired images are never
        # swept: spent/<mood>/ is chat history now.)
        await asyncio.to_thread(reactions.pool_sweep, None)
        # Registry ↔ disk reconcile rides the same cadence: a regen that
        # strands a pack id heals within one cycle instead of refusing fires
        # until someone greps the journal.
        await asyncio.to_thread(reactions.heal_pack)

        # Each reactions-enabled bot fills its own shelf, on its own schedule:
        # per-bot refresh_hour, per-bot low-water mark, per-bot prompt bank. A
        # bot with no bank simply generates nothing.
        for bot_id in reactions.reaction_bots():
            st = reactions.pool_load(bot_id)
            if not st.config.enabled:
                continue
            status = reactions.pool_status(bot_id)
            if status["due_daily"]:
                n = await _top_up_pool(max_rounds=12, bot_id=bot_id)
                if not reactions.pool_deficits(bot_id=bot_id):
                    reactions.pool_mark_daily(bot_id)
                    log.info("reaction pool: nightly refill complete for %s (%d generated)", bot_id, n)
                elif n:
                    log.info("reaction pool: nightly refill progressed for %s (%d generated, "
                             "%d still owed)", bot_id, n, reactions.pool_status(bot_id)["deficit"])
            elif status["needs_refill"]:
                n = await _top_up_pool(only_low=True, bot_id=bot_id)
                if n:
                    log.info("reaction pool: low-mood top-up for %s (%d generated, %d on hand)",
                             bot_id, n, reactions.pool_status(bot_id)["remaining"])
        await _broadcast_pool_state()


async def _broadcast_avatar_pool_state() -> None:
    """Push avatar-pool telemetry to the manager panel — one frame, every
    pool-enabled bot, each status object naming its own bot."""
    with contextlib.suppress(Exception):
        pools = {}
        for bid in avatar_pool.enabled_bots():
            # One bot's unreadable pool must not cost the others their frame.
            with contextlib.suppress(Exception):
                pools[bid] = avatar_pool.status(bid)
        await manager.broadcast({"type": "avatar_pool", "pools": pools})


async def _top_up_avatar_pool(max_rounds: int = 8, *, only_low: bool = False,
                              bot_id: str) -> int:
    """Generate avatar pairs toward the target: refill rounds until the
    deficit is gone, the rig stops cooperating, or `max_rounds` is hit."""
    made = 0
    for _ in range(max_rounds):
        if _shutting_down:
            break
        n = await asyncio.to_thread(avatar_pool.refill, None,
                                    bot_id=bot_id, only_low=only_low)
        if n == 0:
            break
        made += n
        await _broadcast_avatar_pool_state()
    return made


async def _avatar_pool_cycle() -> None:
    """One pass over the avatar pools — same shape as the reaction cycle:
    sweep stranded staging, nightly top-up per bot, honour the low-water
    mark. Shares _pool_lock with reactions so the rig never runs both fills
    at once."""
    if _shutting_down:
        return
    async with _pool_lock:
        await asyncio.to_thread(avatar_pool.sweep)
        for bot_id in avatar_pool.enabled_bots():
            st = avatar_pool.load_state(bot_id)
            if not st.config.enabled:
                continue
            if avatar_pool.daily_due(bot_id):
                n = await _top_up_avatar_pool(max_rounds=12, bot_id=bot_id)
                if not avatar_pool.deficit(bot_id):
                    avatar_pool.mark_daily(bot_id)
                    log.info("avatar pool: nightly refill complete for %s (%d generated)",
                             bot_id, n)
                elif n:
                    log.info("avatar pool: nightly refill progressed for %s (%d generated, "
                             "%d still owed)", bot_id, n, avatar_pool.deficit(bot_id))
            elif avatar_pool.needs_refill(bot_id):
                n = await _top_up_avatar_pool(only_low=True, bot_id=bot_id)
                if n:
                    log.info("avatar pool: low-water top-up for %s (%d generated)",
                             bot_id, n)
        await _broadcast_avatar_pool_state()


async def _reaction_pool_loop() -> None:
    """Keep the pools stocked (reactions AND avatar pairs). Wakes on a nudge,
    otherwise sweeps periodically.

    Deliberately lazy: with the image host unreachable every cycle is a no-op and the
    named pack carries on working, so a rig that's off never breaks reactions.
    """
    try:
        await asyncio.sleep(20)      # let startup settle before touching the rig
        while True:
            _pool_wake.clear()       # clear FIRST: a nudge during the cycle must survive
            try:
                await _reaction_pool_cycle()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("reaction pool cycle failed")
            try:
                await _avatar_pool_cycle()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("avatar pool cycle failed")
            # Backoff + alert (the VRAM-contention guard): after consecutive
            # refill failures (refusals, rig errors, vram-short blocks) the
            # loop backs off exponentially instead of tight-looping refused
            # calls, and shouts once the streak passes the alert threshold.
            consec = pool_guard.consecutive_failures()
            timeout = pool_guard.backoff_s(consec)
            if consec and consec >= pool_guard.ALERT_AFTER:
                # Name the cause instead of guessing between two. The old text
                # said "(VRAM contention or rig down)" on every streak, which
                # is how fourteen hours of a dead rig-side renderer read
                # identically to a busy GPU.
                stats = pool_guard.refill_failure_stats()
                log.error("POOL-REFILL-ALERT: %d consecutive refill failures "
                          "%s; next cycle in %ds — details: %s",
                          consec, stats["by_kind"] or "(no kinds recorded)",
                          int(timeout), stats["recent"][-1:])
            elif consec:
                log.warning("pool refill: %d consecutive failures; next cycle in %ds",
                            consec, int(timeout))
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(_pool_wake.wait(), timeout=timeout)
    except asyncio.CancelledError:
        pass


# --------------------------------------------------------------------------- #
# Core agent turn
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# The transcript backstops, and when they are simply not there
#
# The watcher, the follower, the reconciler, the gap sweep and the mirror
# poller all read `~/.openclaw/agents/<bot>/sessions/*.jsonl`, indexed by
# `sessions.json`. OpenClaw 8.1 moved sessions into
# `agent/openclaw-agent.sqlite`; on such a host neither the index nor the
# transcripts exist, `resolve_session_file` always answers None, and every one
# of those paths does nothing — silently, and at a cost: `_follow_session`
# alone burns thirty two-second sleeps per turn polling for a file that will
# never appear.
#
# The code is NOT removed. A host that still has the files is still served by
# it, and deleting a whole redundant delivery path on the strength of one box's
# layout is how a "cleanup" becomes an outage. Instead it is PROBED once, said
# out loud once, reported in /api/health, and skipped while the socket — which
# on such a host is the only transport there is — is the one carrying replies.
# --------------------------------------------------------------------------- #

_transcript_backstop: dict[str, Any] = {"probed": False, "available": True}


def _probe_transcript_files() -> bool:
    """Does this host keep live session transcripts on disk at all?

    Deliberately about the STORE, not about one session: a brand-new session
    has no file yet on a host where the store exists, so probing a single
    lookup would call a healthy backstop dead.
    """
    try:
        for agent_dir in openclaw.OPENCLAW_AGENTS_DIR.iterdir():
            sessions = agent_dir / "sessions"
            if (sessions / "sessions.json").is_file():
                return True
            if any(sessions.glob("*.jsonl")):
                return True
    except OSError:
        pass
    return False


def _transcript_files_available() -> bool:
    if not _transcript_backstop["probed"]:
        _transcript_backstop["available"] = _probe_transcript_files()
        _transcript_backstop["probed"] = True
        if not _transcript_backstop["available"]:
            log.warning(
                "no OpenClaw session transcripts under %s — the file-based "
                "watcher/follower/reconciler/sweep backstops cannot run on "
                "this host (sessions moved into the agent sqlite in 8.1). "
                "The gateway socket is the delivery path; /api/health reports "
                "transcript_backstop=unavailable.",
                openclaw.OPENCLAW_AGENTS_DIR)
    return bool(_transcript_backstop["available"])


def _transcript_backstop_state() -> str:
    if not _transcript_backstop["probed"]:
        return "unprobed"
    return "available" if _transcript_backstop["available"] else "unavailable"


def _socket_delivery_live() -> bool:
    """Is the gateway socket the transport carrying replies right now?"""
    return (_gateway_router is not None
            and SETTINGS.gateway_ws in ("1", "true", "on", "yes"))


def _transcript_paths_dead() -> bool:
    """Skip a file backstop: there are no files, and the socket is delivering.

    Both halves matter. Without the socket, a host with no transcripts has no
    delivery path at all and the loops should still run (and still find
    nothing) rather than quietly stand down.
    """
    return not _transcript_files_available() and _socket_delivery_live()


async def _watch_progress(
    thread_id: str, bot_id: str, session_key: str, handoff: dict,
) -> None:
    """Tail the OpenClaw session transcript during a turn and broadcast
    progress items (thinking / tool calls) so the UI can show live activity
    inside the typing indicator. Cancelled when the turn completes.

    `handoff` receives {"path", "offset"} — the byte position of the next
    unconsumed line — so the post-turn follower can resume EXACTLY where this
    watcher stopped (a gap here would swallow fast subagent announces).
    """
    if _transcript_paths_dead():
        return
    offset: int | None = None
    path = openclaw.resolve_session_file(bot_id, session_key)
    if path is not None:
        with contextlib.suppress(OSError):
            offset = path.stat().st_size   # existing session: only new events
            # start_offset (set once) marks where THIS turn's content begins, so
            # the post-turn reconciler can re-scan the whole turn as a safety net.
            handoff["path"], handoff["offset"] = path, offset
            handoff.setdefault("start_offset", offset)
    buf = b""
    try:
        while True:
            await asyncio.sleep(1.0)
            if path is None:
                path = openclaw.resolve_session_file(bot_id, session_key)
                if path is None:
                    continue
                offset = 0                  # brand-new session: read from start
                handoff["path"] = path
                handoff.setdefault("start_offset", 0)
            try:
                size = path.stat().st_size
                if offset is not None and size < offset:
                    # File shrank (truncation/compaction/rotation) — a stale
                    # offset would skip every subsequent poll. Re-scan from the
                    # start; the shared dedup funnel absorbs any re-read text.
                    offset = 0
                    buf = b""
                if offset is None or size <= offset:
                    continue
                with path.open("rb") as f:
                    f.seek(offset)
                    chunk = f.read(size - offset)
                offset = size
            except OSError:
                continue
            buf += chunk
            lines = buf.split(b"\n")
            buf = lines.pop()               # keep any partial trailing line
            # Next-unconsumed-line position = consumed bytes minus the partial.
            handoff["path"], handoff["offset"] = path, offset - len(buf)
            for line in lines:
                for item in openclaw.parse_progress_line(line.decode("utf-8", "replace")):
                    # Assistant TEXT blocks are real messages, not just live
                    # progress: a multi-step turn narrates between tool calls and
                    # the CLI's final payload keeps only the LAST block. Capture
                    # every block in transcript order; run_agent_turn persists the
                    # intermediate ones (deduped) once the turn returns, so order
                    # is preserved and nothing the agent says is lost. Tool calls
                    # and thinking stay ephemeral (live panel only).
                    if item.get("kind") == "text":
                        ft = item.get("full_text") or item.get("text") or ""
                        if _strip_reply_directive(_strip_no_reply(ft)).strip():
                            handoff.setdefault("texts", []).append(ft)
                    slim = {k: v for k, v in item.items() if k != "full_text"}
                    await manager.broadcast(
                        {"type": "progress", "thread_id": thread_id, "item": slim}
                    )
    except asyncio.CancelledError:
        pass


# --------------------------------------------------------------------------- #
# Post-turn session follower
#
# Agents often DELEGATE: the CLI turn returns "I sent Swift to grab that…",
# and the real answer arrives minutes later when the subagent announces back —
# but that follow-up is written only to the OpenClaw session transcript (the
# CLI has no delivery channel). Without this follower those replies are lost
# ("the agent responds on the OpenClaw side but nothing shows in the app").
# --------------------------------------------------------------------------- #

_followers: dict[str, asyncio.Task] = {}      # thread_id -> follower task
# How long to keep tailing a transcript after a turn returns.
#
# This was 30 minutes and it silently dropped whole answers. A delegated turn
# produces ZERO transcript bytes while it waits on a subagent, so the "silence"
# clock runs out during exactly the situation the follower exists for. Observed
# in the family chat on 2026-08-07: the gap between the turn's last transcript
# byte and the agent's resume was 1808.997s against a 1800s window — NINE
# SECONDS over. Seventeen assistant blocks, including the finished answer, never
# reached the chat, and the next message in the thread is a human asking "So....
# Did you forget to follow up?".
#
# The race is structural, not bad luck: the gateway's own subagent budget
# (subagents.runTimeoutSeconds) is also 1800s, so a subagent that uses its full
# allowance lands its announce at or after the follower's deadline BY
# CONSTRUCTION. The window must comfortably exceed it.
FOLLOW_WINDOW_S = 2 * 60 * 60                 # 2h — 4x the gateway subagent budget

# ...but a window is a poor primitive on its own, so the deadline is also
# extended by DisPatch-side activity, not only by transcript growth. A turn the
# user is still watching keeps its follower alive.
FOLLOW_IDLE_EXTEND_S = 30 * 60


def _stop_follower(thread_id: str) -> None:
    t = _followers.pop(thread_id, None)
    if t:
        t.cancel()


def _media_fingerprint(path_part: str) -> str:
    """Rewrite-stable token for a media directive path.

    Persisting turns [[media:/tmp/x.png]] into [[media:/media/<uuid>.png]], so a
    path-sensitive compare would re-deliver every media message as "new". The
    token bridges that by naming the ORIGIN — the path the bytes were ingested
    from — which both sides can produce: the transcript has it verbatim, and the
    served copy carries it in the origins ledger.

    This used to hash the file's bytes at both ends instead, which reads
    correct and is not: it silently stopped bridging the moment either end was
    deleted, and agents delete their scratch files constantly. A token must not
    depend on state that can vanish while the message it identifies persists.
    Distinct sources still get distinct tokens, so two captionless pictures in
    one turn cannot collide — the property the byte-hash was introduced for.

    Legacy rows ingested before the ledger existed have no recorded origin;
    they fall back to the served path, which is at least stable.
    """
    p = (path_part or "").strip()
    if p.startswith("/media/"):
        return f"src:{_media_origin_of(p) or p}"
    if p.startswith("file://"):
        p = p[len("file://"):]
    if p.startswith("~"):
        with contextlib.suppress(RuntimeError):
            p = str(Path(p).expanduser())
    return f"src:{p or '?'}"


def _media_norm(s: str) -> str:
    """Collapse whitespace and neutralise media-directive PATHS (keep captions).

    Persisting rewrites [[media:/local/path|cap]] to [[media:/media/<uuid>|cap]],
    so a path-sensitive compare would re-deliver media-only messages as "new".
    The path is replaced by a content fingerprint (NOT erased): erasing it made
    two distinct captionless images in one turn dedup-collide, silently dropping
    the second — violating the "nothing is ever dropped" guarantee.
    """
    s = " ".join(s.split())
    return _MEDIA_DIRECTIVE_RE.sub(
        lambda m: f"[[media:{_media_fingerprint(m.group(1))}{m.group(2) or ''}]]", s
    ).strip()


def _canon_msg(s: str) -> str:
    """Canonical key for an assistant message — used for dedup across the three
    delivery paths (synchronous CLI payload, in-turn narration, post-turn
    follower). Mirrors the transforms applied at persist time (NO_REPLY strip +
    reply-directive strip + media salvage + path-neutralised whitespace), so
    raw transcript text and already-persisted content compare equal. The
    directive strip runs BOTH anchored (the persist chokepoint's rule) and
    unanchored: the gateway WS transport redacts [[reply_to_current]] /
    [[reply_to:<id>]] anywhere in the text before delivering, while the
    transcript the legacy tail paths read keeps the token verbatim — the two
    recordings of one reply must key equal or both post.

    Scaffolding is stripped here as well as at persist time, so a message stored
    RAW before the sanitizer existed still compares equal to the same message
    re-read from the transcript today — otherwise every mirror backfill would
    re-post the sanitized twin of a message already in the thread.

    EVERY transform persisting applies has to appear here. The `:react:` strip
    did not, and so a reply that fired a reaction never matched its own stored
    copy: the gap sweep re-posted one short text reply five times
    in one morning. Adding a transform at the persist chokepoint without adding
    it here is the bug, not an oversight in the sweep — which is why the
    `[[pic:…]]` strip is here too.

    ``assume_files_exist`` because a key must be a pure function of the text:
    the salvage walk normally wraps a bare path only while the file exists,
    which made this key change when the agent deleted its scratch files — the
    third state-that-can-vanish drift, found auditing the first two.
    """
    return _media_norm(image_jobs.strip_pic_markers(
        reactions.strip_markers(_salvage_media_refs(
            openclaw_text.sanitize_assistant_visible_text(
                _strip_reply_directive_anywhere(
                    _strip_reply_directive(_strip_no_reply(s)))),
            assume_files_exist=True))))


# Memo for the whole-thread dedup scan. _canon_msg is half a dozen regex
# passes; the whole-thread branch runs it over EVERY assistant row for EVERY
# delivered message, so a backfill batch onto a long thread was quadratic work
# with not one await in it — the event loop stalled for the whole batch.
#
# Keyed by the raw content, which is exact: _canon_msg is a pure function of
# its text (that is the point of `assume_files_exist`), so a hit can never be
# stale, and a row whose content is later rewritten simply misses. Bounded, so
# a long-running process does not accumulate every message it has ever seen.
_canon_memo: OrderedDict[str, str] = OrderedDict()
_CANON_MEMO_MAX = 4096


def _canon_msg_cached(s: str) -> str:
    hit = _canon_memo.get(s)
    if hit is not None:
        _canon_memo.move_to_end(s)
        return hit
    out = _canon_msg(s)
    _canon_memo[s] = out
    while len(_canon_memo) > _CANON_MEMO_MAX:
        _canon_memo.popitem(last=False)
    return out


# --------------------------------------------------------------------------- #
# Drift-tolerant dedup (the abridged twin)
#
# The gateway WebSocket and the CLI/transcript-tail paths deliver the SAME
# reply as two genuinely different strings: the abridged copy is the complete
# copy with TRAILING content silently dropped (a whole paragraph, the MEDIA
# lines, the `:react:` emoji — live 2026-08-26: 1,148 vs 1,034 chars, the
# shorter a PREFIX of the longer). Exact `_canon_msg` equality keys them as
# distinct messages and both post. The helpers below make the dedup predicate
# tolerant to that drop; _is_twin is the ONE predicate used at every dedup
# chokepoint (in-memory _delivered, the DB trailing-run scan, and the 300s
# recent window), so a twin is recognised consistently everywhere.
# --------------------------------------------------------------------------- #

# A shared opening this long is a twin, not a coincidence. Kept deliberately
# short of the 80-char signature head: a genuine abridged twin usually drops
# a whole tail paragraph (hundreds of chars), while two DISTINCT replies
# sharing 60+ leading chars are vanishingly rare — and the pair that matters
# (the double-post) is the failure that actually happens, so the floor errs
# toward catching it. Below this, a shared prefix is treated as coincidence:
# "Done!" and "Done — more below." must never collide.
_TWIN_MIN_SHARED_PREFIX = 60


def _msg_signature(text: str) -> tuple[str, int]:
    """Stable structural fingerprint of a message, tolerant to trailing drop.

    ``(canonical_text[:80], media_directive_count)``: the two recordings of
    one reply share their leading 80 canonical characters and their media
    lines either survive or are dropped together, so a copy that differs
    from its twin ONLY in the dropped tail still matches — while genuinely
    different replies (different openings, or a different number of
    pictures) do not. ``media_directive_count`` counts ``MEDIA:`` and
    ``[[media:`` occurrences in the whole canonical text (the bare form only
    survives salvage when it was not a real file reference; counting both
    spellings keeps the two transports' recordings comparable either way).
    Input may be raw or already canonical (canonicalisation is idempotent).
    """
    canon = _canon_msg(text)
    return canon[:80], canon.count("MEDIA:") + canon.count("[[media:")


def _is_twin(a: str, b: str) -> bool:
    """True if two texts are the SAME reply recorded twice by the two
    transports — even when their canonical dedup keys differ.

    Two messages are duplicates (twins) when, within the dedup window:
      a. their canonical texts are exactly equal (the pre-existing rule), or
      b. the SHORTER canonical text is a PREFIX of the LONGER, with the
         shared prefix at least ``_TWIN_MIN_SHARED_PREFIX`` (60) chars — the
         abridged-copy shape, where the tail was cut cleanly, or
      c. they share the same ``_msg_signature`` — same leading 80 canonical
         chars AND the same media-directive count — AND the same media
         directives themselves (same picture refs). The ref identity is
         what keeps two DISTINCT captionless pictures posted in one turn
         apart: they share the long serving path for 80+ chars and both
         count one picture, so head+count alone would collapse them — the
         exact anti-collision property the media fingerprint exists for
         (test_two_captionless_images_in_one_turn_stay_distinct).

    FALSE-POSITIVE GUARD: a shared opening is coincidence, not truncation,
    below 60 chars, so two distinct one-liners that start alike never
    collide. Rule (c) is stricter still (80 chars + identical media); its
    accepted trade-off is that two genuinely DISTINCT replies sharing the
    first 80 canonical characters verbatim AND carrying the same pictures
    collapse to one — replies diverge within their opening sentence in
    practice, and the alternative (an abridged twin double-posting into the
    family chat) is the failure that actually happens. Inputs may be raw or
    already canonical.
    """
    # MEMOIZED, not recomputed. Both call sites already hand this function
    # canonical strings — the in-memory claim set stores keys, and the
    # whole-thread scan canonicalizes each row before comparing — so every
    # comparison was re-running half a dozen regex passes over text that was
    # already canonical, once per candidate, for every delivered message.
    ca, cb = _canon_msg_cached(a), _canon_msg_cached(b)
    if ca == cb:
        return True
    if len(ca) < _TWIN_MIN_SHARED_PREFIX or len(cb) < _TWIN_MIN_SHARED_PREFIX:
        return False
    shorter, longer = (ca, cb) if len(ca) <= len(cb) else (cb, ca)
    if longer.startswith(shorter):
        return True
    return (_msg_signature(ca) == _msg_signature(cb)
            and _media_refs(ca) == _media_refs(cb))


def _media_refs(canon: str) -> list[tuple[str, str]]:
    """The (path, caption) pairs of every media directive in canonical text.

    Two recordings of one reply carry the SAME picture references — a cut
    tail can drop them together or leave them together, it cannot swap them.
    Two distinct replies can share a count ("here's a picture" twice) but
    not the references, so ref identity is the discriminator that keeps
    same-count pairs apart without weakening the twin test.
    """
    return _MEDIA_DIRECTIVE_RE.findall(canon)


def _media_ref_count(text: str) -> int:
    """How many pictures this text would deliver, after salvage."""
    return _salvage_media_refs(text or "").count("[[media:")


def _prefer_richer_media_twin(text: str, candidates: list[str]) -> str:
    """Return the version of this block that still has its picture lines.

    ONE assistant block reaches the turn by several routes, and they do not
    always carry the same text: the gateway's reply payload has been observed
    dropping the agent's `MEDIA:/path` lines that the transcript kept. Dedup
    compares text and those two texts are genuinely different, so both posted —
    the reply appeared in the chat twice, 0.3s apart, once without its
    pictures. Downstream cannot fix that; by then they are two messages.

    So the choice is made HERE, where both versions are in hand, and only in
    the one direction that is unambiguous: same prose, and the candidate has
    pictures this text has lost. A payload that kept its own pictures, or whose
    prose differs at all, is returned untouched — this must never pick between
    two genuinely different messages.
    """
    if not text or not candidates or _media_ref_count(text):
        return text
    key = _presence_key(text)
    if not key:
        return text
    # Newest match wins. The payload being repaired is the turn's FINAL block;
    # a turn can say the same prose twice with different pictures ("Here you
    # go:" + image, twice), and taking the first match dressed the final
    # message in the earlier block's image while the narration loop posted the
    # later one — both pictures delivered, order swapped.
    for cand in reversed(candidates):
        if cand and _media_ref_count(cand) and _presence_key(cand) == key:
            return cand
    return text


def _settle_payload(payload, narrations: list[str]) -> tuple[str, str | None]:
    """One (text, media_url) per payload, settled against the transcript.

    When the twin substitution fires, the payload's own ``media_url`` is
    DROPPED: the gateway has been seen hoisting a block's picture into
    ``mediaUrl`` while cutting the MEDIA line from the text (live, 2026-07-31
    08:40:40 — the transcript copy carried the picture inline, the payload
    carried the same prose with the picture as media_url, and both posted).
    The chosen transcript text is the block's complete media record; keeping
    the attachment as well renders the same picture twice in one message.
    A payload with no matching twin keeps its media_url untouched — there it
    is the only copy of the picture.
    """
    text = payload.text or ""
    chosen = _prefer_richer_media_twin(text, narrations)
    if chosen != text and payload.media_url:
        return chosen, None
    return chosen, payload.media_url


# Source-less deliveries get a second, wider dedup horizon — the trailing-run
# scan only sees the CURRENT turn, and a legacy path can re-deliver an OLD
# turn's text. 300s is the trade-off: generous for the double-delivery it must
# catch (the two transports post the same reply seconds apart), tight enough
# that a legitimately repeated line ("Done!", "No new items today.") is
# normally turns/hours apart.
DEDUP_RECENT_WINDOW_S = 300


async def _is_duplicate_message(thread_id: str, text: str, *,
                                whole_thread: bool = False,
                                recent_window_s: float | None = None) -> MessageOut | None:
    """Return the already-posted message if `text` was posted *within the
    current turn* — as a duplicate OR as a drift-tolerant twin (see
    _is_twin: the gateway and transcript-tail transports record one reply
    with different text, the abridged copy being a prefix of the complete
    one). None when the text is new.

    Dedup must collapse the four redundant in-turn delivery sources (sync
    payload, narration, reconcile, follower) WITHOUT suppressing a message that
    is legitimately repeated in a later turn (e.g. a second "Done!" or "No new
    items today"). A turn produces a contiguous run of assistant messages after
    the user's message, so we compare ONLY against that trailing run
    (newest-first, stopping at the user row that opened the turn). A cross-turn
    repeat has the next user message in between, so it is no longer a duplicate
    and gets delivered — closing the silent-drop the persistent key set used to
    cause. Backstops the in-memory _delivered set across restarts / cold sets.

    A REACTION TRACE IS PART OF THE TURN, NOT ITS BOUNDARY. A reply that fires
    a reaction persists a `system` trace row right after itself, mid-turn; the
    scan used to stop at ANY non-assistant row, so once a trace landed, every
    reply above it was invisible to the scan and a cold-set redundant path
    (crash recovery, a WS backfill after restart) re-posted the reply that had
    just been celebrated. Only the trace is skipped — any OTHER system row
    keeps its boundary role, because in user-less mirror threads those rows
    are the only thing separating turns, and skipping them would let this
    window wrongly swallow a legitimately repeated daily line.

    ``whole_thread`` is for REPLAYS — deliveries that can lawfully sit behind
    later turns (the WS backfill after an outage). There the trailing run is
    the wrong window by construction, and the rule every other replay path uses
    (the transcript sweep, the mirror's truncation pass) applies: dedup against
    the entire thread.

    ``recent_window_s`` widens the search for SOURCE-LESS deliveries: when
    set, a matching assistant row ANYWHERE in the thread is a duplicate if it
    was posted within the last ``recent_window_s`` seconds. The trailing run
    above stops at the user row that opened the CURRENT turn, so it cannot see
    an OLD turn's text — and the in-memory key set is cleared at every turn
    start — which let a legacy re-delivery of an earlier turn (crashed-turn
    recovery, a follower that resumed late) double-post. Five minutes is
    generous for the double-delivery this exists to catch and tight enough
    that a legitimately repeated line is normally turns/hours apart. Trade-off:
    two genuinely separate identical lines inside the window collapse to one.
    Accepted: the re-post is the failure that actually happens, and 300s
    bounds the collateral. Deliveries WITH a source_id never take this path —
    identity dedup is the whole answer for them.

    Both sides go through the same canonicalisation as persisting (NO_REPLY
    strip + media salvage) so a repaired MEDIA:/path reply compares equal to
    itself. Twin detection is structural (same head / prefix / signature), so
    an abridged copy whose tail was dropped still matches its complete twin.

    Returns the MATCHED ROW, not a bool: the caller applies the authority rule
    (keep the longer/complete copy, suppress the shorter; equal length keeps
    the first) and may need the row's id to replace an abridged copy that was
    persisted before its complete twin arrived.
    """
    norm = _canon_msg(text)
    if whole_thread:
        # Memoised canon + a yield every so often: this scan is O(rows) per
        # delivered message and a backfill delivers many, so unbroken it held
        # the event loop for the length of the batch (nothing else — a WS
        # frame, a heartbeat, another thread's turn — ran meanwhile).
        for i, m in enumerate(await db.dump_messages(thread_id)):
            if i and not i % 200:
                await asyncio.sleep(0)
            if m.role == "assistant" and _is_twin(
                    norm, _canon_msg_cached(m.content or "")):
                return m
        return None
    # 40, not 10: a single multi-step turn can narrate many assistant blocks; the
    # trailing contiguous assistant run we compare against must be able to hold a
    # whole turn's worth so a within-turn repeat is still caught.
    msgs, _ = await db.list_messages(thread_id, limit=40)
    for m in reversed(msgs):              # newest -> oldest
        if m.role != "assistant":
            if (m.role == "system"
                    and (m.metadata or {}).get("kind") == "reaction"):
                continue                 # the turn's own trace, not a boundary
            break                        # reached the row that opened the turn
        if _is_twin(norm, _canon_msg(m.content or "")):
            return m
    if recent_window_s is not None:
        msgs, _ = await db.list_messages(thread_id, limit=200)
        for m in reversed(msgs):       # newest -> oldest (created_at, rowid)
            age = _iso_age_seconds(m.created_at)
            if age is not None and age > recent_window_s:
                break                  # this row and all below: outside the window
            if (age is not None and m.role == "assistant"
                    and _is_twin(norm, _canon_msg(m.content or ""))):
                return m
    return None


# --------------------------------------------------------------------------- #
# Unified assistant-text delivery (redundant paths, one dedup)
#
# Every assistant text message reaches DisPatch through ONE funnel, fed by FOUR
# redundant sources so a failure in any one still delivers:
#   1. synchronous CLI payload      (run_agent_turn)        — the final reply
#   2. in-turn narration            (_watch_progress)       — running commentary
#   3. post-turn reconciliation     (_reconcile_transcript) — re-scan the turn
#   4. post-turn follower           (_follow_session)       — late announces
# Dedup is layered: a bounded in-memory per-thread key set (reliable regardless
# of how many blocks a turn emits) backed by a DB recent-message check (survives
# restarts / the set being cold). Belt and suspenders — the user's #1 ask is
# that nothing the agent says is ever silently dropped.
# --------------------------------------------------------------------------- #

_delivered: dict[str, OrderedDict[str, None]] = {}
_DELIVERED_CAP = 256                          # recent keys kept per thread


def _mark_delivered(thread_id: str, key: str) -> None:
    d = _delivered.setdefault(thread_id, OrderedDict())
    d[key] = None
    d.move_to_end(key)
    while len(d) > _DELIVERED_CAP:
        d.popitem(last=False)


def _forget_thread_delivery(thread_id: str) -> None:
    _delivered.pop(thread_id, None)


async def _deliver_assistant_text(
    thread_id: str, text: str, *,
    metadata: dict | None = None, media_url: str | None = None,
    stream: bool = False, source_id: str | None = None,
    created_at: str | None = None, dedup_whole_thread: bool = False,
    dedup_recent_window: bool = True,
) -> MessageOut | None:
    """The single funnel every assistant message passes through.

    Skips (returns None) when the text is empty after NO_REPLY-stripping and has
    no media, when it was already delivered (in-memory key OR a matching recent
    DB message — matching now includes drift-tolerant TWINS, see _is_twin, so
    an abridged copy of an already-posted reply is suppressed), or when the
    thread has been deleted. Otherwise persists + broadcasts (streamed for the
    visible final reply, instant for everything else) and records the key so
    the other redundant paths won't repeat it. When the complete copy arrives
    AFTER its abridged twin was already persisted (the gateway-vs-transcript
    ordering seen live), the abridged row is deleted and replaced by the
    complete one — never the other way round.

    ``dedup_recent_window`` gates the recent-window whole-thread check for
    source-less deliveries (see _is_duplicate_message). Deliberate recovery
    paths — the crashed-turn sweep — set it False: restoring a reply lost to
    a crash may legitimately repeat an earlier turn's words, and the window
    would eat the very message being recovered.
    """
    # IDENTITY BEATS CONTENT. When the source has a stable id, that is the
    # answer: it does not care how far back the message was (content matching
    # only ever saw the trailing assistant run), and it never confuses a
    # legitimately repeated line — "Done.", "No new items today." — with a
    # repeat of the same line. Content heuristics remain below for /api/inject
    # and manual import, which have no source identity.
    if source_id and await db.source_id_seen(source_id):
        return None
    superseded: MessageOut | None = None
    has_media = bool(media_url) or "[[media:" in (text or "")
    # Emptiness is judged AFTER scaffolding removal, matching what actually gets
    # persisted: a transcript row that is nothing but a runtime-context block
    # sanitizes to "" and must post nothing rather than an empty bubble.
    if not openclaw_text.sanitize_assistant_visible_text(
            _strip_reply_directive(_strip_no_reply(text or ""))).strip() and not has_media:
        return None
    key = _canon_msg(text) if (text or "").strip() else None
    # -----------------------------------------------------------------------
    #  WIPE FIX (2026-09-14). The content-based twin check used to apply to
    #  EVERY message — including ones with a stable source_id — and that
    #  closed on the live reply in this scenario:
    #
    #    1. Gateway detects a messageSeq gap (e.g. 2 -> 6), queues a backfill.
    #    2. Live path delivers the conversational reply at seq=6 inline.
    #    3. Backfill fires 6s later, walks history, persists items 3..5
    #       (each has its own source_id, each is a drift-tolerant LONGER twin
    #       of the live reply — they share >60 chars of opening).
    #    4. The NEXT backfilled message's whole-thread scan matches the live
    #       reply in the trailing run; "live longer → superseded" then DELETES
    #       the live reply via db.delete_message().
    #
    #  Symptom: reply in the UI, then gone minutes later, row missing from
    #  `messages`. Log shows the jump + backfill.
    #
    #  The comment at gateway_router._backfill says "every message carries a
    #  stable id, so re-delivering one already stored is a no-op" — that is
    #  the original design, and identity is enough for any source_id message.
    #  Source-less deliveries (/api/inject, legacy transcript tail) still
    #  need the content check (see test_double_post_fix.py).
    #
    #  Skipping the content-based twin check here, in-memory and DB, for
    #  messages WITH a source_id, closes the wipe with no other change.
    #  Reversible: a one-line revert — drop `and not source_id` from both
    #  clauses — restores the old behaviour exactly.
    # -----------------------------------------------------------------------
    if key:
        if not source_id:
            # Twin-aware in-memory dedup (see _is_twin): the two transports record
            # ONE reply with different text (the abridged copy is the complete copy
            # with its tail dropped), so exact-key equality lets the twin through.
            # Compare structurally and apply the AUTHORITY RULE: on a duplicate the
            # LONGER (complete/gateway) copy wins, equal length keeps the first,
            # and the complete copy is never suppressed.
            claims = _delivered.get(thread_id)
            if claims:
                for stored in list(claims):
                    if _is_twin(key, stored):
                        if len(key) <= len(stored):
                            # Shorter (or equal) twin of an already-delivered
                            # message: the abridged copy arriving after the
                            # complete one. Suppress it.
                            return None
                        # The COMPLETE copy arriving after an abridged twin was
                        # already delivered. Never suppress the complete copy:
                        # drop the shorter claim so this message persists — the
                        # DB check below then finds the abridged row and replaces
                        # it with this one, leaving exactly one post.
                        claims.pop(stored, None)
                        break
        # Claim BEFORE any await: two sources delivering the same text can
        # otherwise both pass the checks below and double-post. Released on
        # failure so a transient error can't permanently drop the message.
        # Source-id messages claim the same way so a follow-up cold-set
        # source-less delivery in the same process still finds them.
        _mark_delivered(thread_id, key)
    try:
        # Content-based dedup. The in-memory twin check above is already
        # skipped for source-id'd messages (its `_is_twin` would
        # otherwise mis-identify a fresh reply that happens to share
        # 60+ chars with an older source-id'd one as the same message).
        #
        # The DB layer does the same work — except for source-id'd
        # messages it ALSO post-filters by the existing row's source_id:
        # the row is the SAME message (drop or supersede) only when it
        # has no source_id (CLI / manual import of the same text) OR
        # its source_id is the same as mine (the same gateway event
        # arriving twice). Otherwise it is a different message with a
        # coincidentally shared prefix — the wipe — and we persist.
        if key:
            existing = await _is_duplicate_message(
                thread_id, text, whole_thread=dedup_whole_thread,
                recent_window_s=(None if source_id or not dedup_recent_window
                                 else DEDUP_RECENT_WINDOW_S))
            if existing is not None:
                if source_id:
                    # MessageOut doesn't carry source_id (deliberately — it
                    # would leak onto the wire). Pull just that one column
                    # for the identity comparison; the wipe-fix path is
                    # rare-race so the extra round-trip is fine.
                    existing_source_id = await db.get_message_source_id(
                        existing.id)
                    same_message = (
                        existing_source_id is None
                        or existing_source_id == source_id
                    )
                    if not same_message:
                        existing = None
                if existing is not None:
                    if len(key) <= len(_canon_msg_cached(existing.content or "")):
                        return None            # shorter/equal twin — keep the
                                               # longer/earlier copy; keep the claim
                    # Upgrade: the ABRIDGED copy was persisted first; the complete
                    # copy arriving now replaces it so exactly one post remains.
                    # THE REPLACEMENT IS WRITTEN FIRST. Deleting the abridged row
                    # up here and then persisting meant every failure in between —
                    # a closed database, a thread deleted mid-flight, the persist
                    # chokepoint deciding the message was all-marker — left the
                    # thread with NEITHER copy: a reply the family had already read
                    # vanishing to repair a duplicate.
                    superseded = existing
        if not await db.get_thread(thread_id):
            return None                        # thread deleted mid-flight
        if stream:
            msg = await _persist_and_stream_message(
                thread_id, "assistant", text, media_url=media_url,
                metadata=metadata, source_id=source_id,
                created_at=created_at)
        else:
            msg = await _persist_and_broadcast_message(
                thread_id, "assistant", text, media_url=media_url,
                metadata=metadata, source_id=source_id, created_at=created_at)
        # Only once the complete copy is genuinely on disk. `msg` can be an
        # unpersisted placeholder (an all-marker reply), and retiring the old
        # row for one of those would delete a message and post nothing.
        if superseded is not None and await db.get_message(msg.id) is not None:
            await db.delete_message(superseded.id)
            await manager.broadcast({
                "type": "message_deleted",
                "thread_id": thread_id,
                "bot_id": await _bot_of_thread(thread_id),
                "message_id": superseded.id,
            })
        # The streamed path returned straight out of here, so a thread's
        # preview, unread count and ordering did not move for the ONE message
        # type that matters most: the agent's final reply.
        await _broadcast_thread_update(thread_id)
        return msg
    except BaseException:
        if key:
            _delivered.get(thread_id, OrderedDict()).pop(key, None)
        raise


async def _reconcile_transcript(
    thread_id: str, bot_id: str, session_key: str, handoff: dict,
    session_id: str | None = None,
) -> list[MessageOut]:
    """Safety net: re-read the turn's transcript window from scratch and deliver
    any assistant text block the in-turn watcher missed (e.g. it resolved the
    session file late). Independent of handoff["texts"] — it re-resolves and
    re-parses the file — so it covers watcher gaps the live path can't. Deduped
    by the shared funnel, so it never double-posts what was already delivered.

    Prefers the authoritative path built from the CLI reply's exact sessionId
    (race-free) over the index lookup, which can lag for a brand-new session.
    """
    if _transcript_paths_dead():
        return []
    by_id = openclaw.session_file_by_id(bot_id, session_id) if session_id else None
    path = by_id or handoff.get("path") or openclaw.resolve_session_file(bot_id, session_key)
    if path is None:
        return []
    start = handoff.get("start_offset", 0)
    # If we fell back to the authoritative-by-id file but the watcher tracked a
    # different (or no) file, the saved offset doesn't apply — re-scan whole file
    # (the shared funnel dedups, so re-reading already-delivered text is free).
    if by_id is not None and handoff.get("path") != by_id:
        start = 0
    try:
        with path.open("rb") as f:
            f.seek(start)
            data = f.read()
    except OSError:
        return []
    out: list[MessageOut] = []
    for line in data.split(b"\n"):
        if not line.strip():
            continue
        for item in openclaw.parse_progress_line(line.decode("utf-8", "replace")):
            if item.get("kind") != "text":
                continue
            txt = item.get("full_text") or item.get("text") or ""
            msg = await _deliver_assistant_text(thread_id, txt)
            if msg:
                log.info("transcript reconcile recovered a message (%s/%s)",
                         bot_id, thread_id)
                out.append(msg)
    return out


async def _follow_session(
    thread_id: str, bot_id: str, session_key: str,
    path: Path | None = None, offset: int | None = None,
    session_id: str | None = None,
) -> None:
    """Tail the transcript AFTER a turn; deliver late assistant messages.

    `path`/`offset` come from the in-turn watcher's handoff so there is NO gap:
    a subagent announce can land seconds after the CLI returns, and any reads
    skipped here would lose it. The turn's own reply may be re-read — the
    duplicate check filters it (it was just persisted to the thread).
    """
    # No transcripts on this host: the polling loop below would sleep 30x2s
    # per turn re-resolving a file that cannot exist.
    if _transcript_paths_dead():
        return
    if path is None and session_id:
        path = openclaw.session_file_by_id(bot_id, session_id)
    if path is None:
        # Poll for the transcript: a brand-new session's file/index can lag the
        # CLI return by seconds. Giving up immediately (the old behaviour) lost
        # late delegated answers — the exact "replies on OpenClaw side, nothing
        # in the app" bug. Re-resolve a few times before conceding.
        for _ in range(30):                      # ~60s at 2s/poll
            await asyncio.sleep(2.0)
            if not await db.get_thread(thread_id):
                return
            path = (openclaw.session_file_by_id(bot_id, session_id) if session_id
                    else None) or openclaw.resolve_session_file(bot_id, session_key)
            if path is not None:
                break
        if path is None:
            return
        offset = 0      # resolved late → scan from the start (dedup guards repeats)
    if offset is None:
        offset = 0
    deadline = asyncio.get_event_loop().time() + FOLLOW_WINDOW_S
    buf = b""
    try:
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(2.0)
            try:
                size = path.stat().st_size
                if size < offset:
                    # Shrunk file (truncation/compaction/rotation): reset and
                    # re-scan — the dedup funnel filters anything re-read.
                    offset = 0
                    buf = b""
                if size <= offset:
                    continue
                with path.open("rb") as f:
                    f.seek(offset)
                    chunk = f.read(size - offset)
                offset = size
            except OSError:
                continue
            deadline = asyncio.get_event_loop().time() + FOLLOW_WINDOW_S  # activity extends
            buf += chunk
            lines = buf.split(b"\n")
            buf = lines.pop()
            for line in lines:
                for item in openclaw.parse_progress_line(line.decode("utf-8", "replace")):
                    if item.get("kind") != "text":
                        continue
                    text = item.get("full_text") or item.get("text") or ""
                    if not await db.get_thread(thread_id):
                        return                      # thread deleted — stop
                    msg = await _deliver_assistant_text(
                        thread_id, text, metadata={"followup": True})
                    if msg:
                        log.info("follow-up delivery (%s/%s): %d chars",
                                 bot_id, thread_id, len(text))
    except asyncio.CancelledError:
        pass
    finally:
        # Only deregister OURSELVES. A cancelled predecessor's finally runs a
        # tick after its replacement registered — an unconditional pop would
        # orphan the new follower (unfindable, uncancellable → duplicates).
        if _followers.get(thread_id) is asyncio.current_task():
            _followers.pop(thread_id, None)


# --------------------------------------------------------------------------- #
# Transcript bridge + disaster recovery
#
# The OpenClaw .jsonl transcripts are an independent SECOND copy of every
# conversation. These helpers turn that copy back into DisPatch messages on
# demand — covering anything the live funnel never delivered (downtime, late
# delegation, a crash mid-turn) — and power the startup self-heal.
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Native gateway transport
#
# The transcript followers each watch a WINDOW of a file the gateway has already
# written. This path is told, by the writer, the moment the write happens. The
# wiring below is deliberately thin: resolve which thread, hand the text to the
# same funnel every other path uses, and keep a log of what it did.
# --------------------------------------------------------------------------- #

_gateway_client: gateway_ws.GatewayClient | None = None
_gateway_router: gateway_router.SessionRouter | None = None
# When the router's counters started counting. They live in memory, so a zero
# means nothing without the window it covers — see /api/health's gateway_ws.
_gateway_ws_since: str | None = None
_gateway_shadow_log: list[dict] = []
GATEWAY_SHADOW_MAX = 2000        # bounded: this runs for days, memory does not


async def _gateway_resolve_thread(session_key: str) -> tuple[str, str] | None:
    """Which DisPatch thread does this gateway session belong to?

    The mapping is by CONSTRUCTION, not by a stored column: DisPatch dispatches
    a turn as ``agent:<bot_id>:<thread_id>``, so the thread id is recoverable
    from the key. Verified against live data — thread ``daily-main-2026-08-09``
    and session ``agent:main:daily-main-2026-08-09`` are the same conversation.

    Returns None for anything that is not ours, which is MOST events: the
    subscription is a firehose over every session on the box, including cron
    jobs, subagents and other tools. None is the normal answer, not an error.
    """
    if not session_key.startswith("agent:"):
        return None                     # 'main' and friends stay with the mirror
    parts = session_key.split(":", 2)
    if len(parts) != 3:
        return None
    _, bot_id, tag = parts
    if not tag or ":" in tag:
        return None                     # 'agent:scout:subagent:…' is not a thread
    # MUTED STAYS MUTED. Deleting a mirrored thread means "never show me this
    # conversation again" — a promise made by the old transport that the new one
    # would otherwise break by re-importing everything on its first connect.
    # The mirror's key is "<bot>|<session key>", both lowercased; looking it up
    # by the bare session key silently matched nothing and the guard did nothing.
    state = _load_mirror_state()
    ent = (state.get("sessions") or {}).get(f"{bot_id.lower()}|{session_key.lower()}")
    if isinstance(ent, dict) and ent.get("status") == "muted":
        return None
    # CASE. The gateway lowercases whole session keys; DisPatch thread ids and
    # bot ids are mixed case — thread 'daily-Scout-2026-07-12' arrives as
    # 'agent:scout:daily-scout-2026-07-12'. An exact-match lookup answers None and
    # the reply is dropped with no error, which is the failure this transport is
    # meant to remove. resolve_thread_id matches COLLATE NOCASE.
    real_tid = await db.resolve_thread_id(tag)
    if real_tid is None and tag in ("main", bot_id.lower()):
        # An agent's own main session is shown as the 'gw-main-<bot>' thread.
        real_tid = await db.resolve_thread_id(f"gw-main-{bot_id.lower()}")
    if real_tid is None:
        return None                     # not a DisPatch conversation
    thread = await db.get_thread(real_tid)
    if thread is None:
        return None
    # The tag is only unique within a bot. Without this check any session whose
    # tag happens to equal one of our thread ids routes into that thread — and
    # that is not hypothetical: a live gateway session was routable into the
    # staging database on the day this was written.
    if (getattr(thread, "bot_id", "") or "").lower() != bot_id.lower():
        return None
    return real_tid, (getattr(thread, "bot_id", None) or bot_id)


async def _gateway_shadow_deliver(thread_id: str, text: str, *,
                                  source_id: str | None, created_at: str | None,
                                  bot_id: str | None, live: bool = True) -> Any:
    """Record what WOULD have been delivered. Persist nothing, broadcast nothing.

    A separate function, not a branch inside the live one, so a shadow router
    holds no reference to anything that can write. Inertness that depends on a
    string comparison at call time is one typo from a live delivery — and three
    of the four values someone might reasonably type for "on" meant LIVE.
    """
    if len(_gateway_shadow_log) < GATEWAY_SHADOW_MAX:
        _gateway_shadow_log.append({
            "at": datetime.now(UTC).isoformat(),
            "thread_id": thread_id, "source_id": source_id,
            "created_at": created_at, "bot_id": bot_id, "live": live,
            "chars": len(text), "text": text[:400],
            # The one failure that is invisible everywhere else.
            "truncated": gateway_ws.GatewayClient.looks_truncated(text),
        })
    log.info("gateway-ws SHADOW would deliver %d chars to %s (%s)",
             len(text), thread_id, source_id)
    # Truthy on purpose: the router reads the return value to decide whether a
    # backfill actually landed anything, and a shadow run that always answered
    # None would report every repair as a no-op — the comparison run's numbers
    # have to mean the same thing as the live one's.
    return {"shadow": True, "thread_id": thread_id, "source_id": source_id}


async def _gateway_deliver(thread_id: str, text: str, *, source_id: str | None,
                           created_at: str | None, bot_id: str | None,
                           live: bool = True) -> Any:
    """Hand a gateway-sourced reply to the one funnel every path shares.

    A NON-live delivery (the router's gap/reconnect backfill) is history being
    replayed, and gets the same treatment as every other replay path:

    - it dedups against the WHOLE thread, not the trailing run. During an
      outage the CLI paths keep delivering — with no gw source_id recorded —
      so identity dedup finds nothing on backfill, and once a later turn's
      user row sits above the replayed reply the trailing-run scan cannot see
      it either: every turn of a multi-turn outage but the last re-posted.
    - it is marked ``followup`` so the persist chokepoint holds its `:react:`
      markers to the replay rule: fire only when ``created_at`` proves the
      reply is fresh (REACTION_REPLAY_FRESH_S). A dated seq-gap backfill from
      seconds ago is a live reply on a slower road and still pops; an old or
      undated replay stays silent — the sweep learned that rule the day a
      replay popped two spent pool images on every device in the house.
    """
    # RETURNS the persisted row, or None when the funnel recognised the message
    # as one the thread already has. The router counts on that distinction to
    # tell a repair that recovered a lost reply from one that found nothing
    # missing (the normal outcome of a tool-call seq gap).
    return await _deliver_assistant_text(
        thread_id, text, source_id=source_id, created_at=created_at,
        metadata=None if live else {"followup": True},
        dedup_whole_thread=not live)


# --------------------------------------------------------------------------- #
# In-flight runs
#
# A turn dispatched over the socket and ACCEPTED keeps running on the gateway
# even if our connection dies a millisecond later. Its reply is emitted to a
# subscription that no longer exists — there is no queue and no replay — so
# unless we can name the run afterwards it is simply gone, and the family sees
# a thread that thought for a while and then said nothing.
#
# The id is the `idempotencyKey` we chose, which the gateway adopts verbatim as
# the runId (principal.ts: `const runId = request.idempotencyKey`). That is what
# makes `agent.wait` and the delta stream addressable at all.
# --------------------------------------------------------------------------- #

class _InflightRun(NamedTuple):
    run_id: str
    session_key: str
    thread_id: str
    bot_id: str
    started: float


_inflight_runs: dict[str, _InflightRun] = {}

# How long an entry may sit before it is assumed dead. Longer than the turn
# timeout, because the entry's whole purpose is to outlive the turn that
# created it when the connection does not.
INFLIGHT_TTL_S = 3600.0


def _inflight_register(run: _InflightRun) -> None:
    _expire_inflight()
    _inflight_runs[run.run_id] = run


def _inflight_done(run_id: str) -> None:
    _inflight_runs.pop(run_id, None)


def _expire_inflight() -> None:
    """Drop entries older than the TTL.

    A run whose socket died and whose `agent.wait` never answered would
    otherwise be re-chased on every reconnect for the life of the process.
    """
    cutoff = time.time() - INFLIGHT_TTL_S
    for run_id, run in list(_inflight_runs.items()):
        if run.started < cutoff:
            _inflight_runs.pop(run_id, None)


async def _recover_inflight_runs(client, router) -> None:
    """After a reconnect: ask about every run we lost track of, then repair.

    WHATEVER `agent.wait` ANSWERS, THE BACKFILL RUNS. "The run finished" is not
    "the reply was delivered" — it finished into a subscription that had gone
    away, which is the entire reason we are here. The wait is what stops us
    repairing a turn that is still mid-sentence; the backfill is what actually
    recovers the words.
    """
    _expire_inflight()
    runs = list(_inflight_runs.values())
    if not runs:
        return
    log.info("gateway-ws reconnect: chasing %d in-flight run(s)", len(runs))
    for run in runs:
        with contextlib.suppress(Exception):
            await client.agent_wait(run.run_id, timeout_ms=30_000)
        try:
            # `force`: a turn accepted just before the drop has no seq cursor
            # at all, and resync used to skip exactly those sessions —
            # declining to repair the one case it exists for.
            await router.catch_up(run.session_key, force=True)
        except Exception:
            log.exception("gateway-ws catch-up failed for %s", run.session_key)


async def _gateway_ws_start() -> None:
    """Bring the native transport up, if it is switched on."""
    global _gateway_client, _gateway_router
    mode = SETTINGS.gateway_ws
    live = mode in ("1", "true", "on", "yes")
    if not live and mode != "shadow":
        return
    router = gateway_router.SessionRouter(
        _gateway_resolve_thread,
        _gateway_deliver if live else _gateway_shadow_deliver,
        # SHADOW MODE BROADCASTS NOTHING. A shadow run exists to compare what
        # the transport WOULD do against the live path; a shadow that painted
        # provisional bubbles onto the family's screens would not be a shadow.
        broadcast=(manager.broadcast if live else None),
        sanitize=_sanitize_delta,
        open_stream=_open_provisional,
        close_stream=_close_provisional,
        stream_open=_provisional_open)

    async def _on_event(event: str, payload: dict) -> None:
        # This must not raise. An exception here kills the reader task and the
        # transport goes silent while looking perfectly healthy — the precise
        # failure shape this module was written to end.
        try:
            await router.handle(event, payload)
        except Exception:
            log.exception("gateway-ws handler failed on %r", event)

    # subscribe_sessions runs on EVERY connect, from inside the client, so a
    # reconnect cannot leave us connected-but-deaf. resync_known then backfills
    # anything emitted while the socket was down (no-op on first connect, since
    # there are no cursors yet). Both are wrapped so a hiccup in either cannot
    # kill the connect path — the legacy transcript sweep remains the backstop.
    async def _on_connect() -> None:
        try:
            await client.subscribe_sessions()
        except Exception:
            log.exception("gateway-ws subscribe on connect failed")
        # Per-session subscriptions die with the socket exactly like the global
        # one. Re-established here, from the client's own set, so a reconnect
        # cannot leave us connected and receiving transcript events but no
        # deltas — the shape of failure that looks completely healthy.
        try:
            await client.resubscribe_sessions()
        except Exception:
            log.exception("gateway-ws per-session resubscribe failed")
        try:
            await router.resync_known()
        except Exception:
            log.exception("gateway-ws resync on reconnect failed")
        # NOT INSIDE THE HANDLER, AND NOT AWAITED HERE. `agent.wait` is a
        # `call()`, and _on_connect runs before the reader is draining
        # responses for the connection it belongs to; awaiting one here would
        # deadlock the connect path for the whole request timeout. It is also
        # a wait, by definition — the connect path must not sit on it.
        _track(asyncio.create_task(_recover_inflight_runs(client, router)))

    client = gateway_ws.GatewayClient(_on_event)
    client._on_connect = _on_connect
    router._client = client
    _gateway_client, _gateway_router = client, router
    globals()["_gateway_ws_since"] = now_iso()
    await client.start()
    try:
        await asyncio.wait_for(client.connected.wait(), timeout=20)
    except TimeoutError:
        # Not fatal: the client keeps retrying and subscribes itself when it
        # lands. Returning here used to leave it silently inert.
        log.error("gateway-ws: no connection after 20s — still retrying")
    log.warning("gateway-ws ACTIVE in %s mode", "LIVE" if live else "SHADOW")


async def _gateway_ws_stop() -> None:
    # Repairs first, socket second. A queued gap repair is a deferred task; if
    # the loop goes away underneath it the repair simply never happens and the
    # only trace is a counter that was already incremented — a fix that reports
    # itself done. Draining it here is bounded (one history read per session
    # that saw a gap) and the socket is still up to serve it.
    if _gateway_router is not None:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(_gateway_router.flush_gaps(), timeout=10)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(_gateway_router.drain_detached(), timeout=10)
        # Shutdown must never raise: whatever state the transport is in, the
        # app still has to come down cleanly.
        with contextlib.suppress(Exception):
            _gateway_router.cancel_streams()
    if _gateway_client is not None:
        with contextlib.suppress(Exception):
            await _gateway_client.stop()


# How often to sweep for answers the live paths missed, and how far back.
GAP_SWEEP_EVERY_S = 10 * 60
GAP_SWEEP_LOOKBACK_H = 48


async def _gap_sweep_loop() -> None:
    """Deliver answers that every live path missed.

    THE BACKSTOP. The watcher, the reconciler and the follower each cover a
    window, and a window can always be missed — the follower's was missed by
    NINE SECONDS on 2026-08-07 and seventeen assistant blocks, including the
    finished answer, never reached the chat. Widening a window makes that
    rarer; it cannot make it impossible, because the thing being waited for has
    no bound.

    So this does not wait at all. It periodically compares each recent thread's
    transcript against what is actually in the database and imports the
    difference. _import_transcript_messages dedups against the WHOLE thread
    history, so a sweep that finds nothing new writes nothing — running it
    often is cheap and running it twice is harmless.

    Deliberately conservative: recent threads only, never a crashed-turn
    import (that is startup recovery's job, with different dedup rules), and
    every failure is swallowed per-thread so one bad transcript cannot stop
    the sweep for everyone.
    """
    await asyncio.sleep(60)                      # let startup settle
    if _transcript_paths_dead():
        log.info("gap sweep stood down: no transcripts on this host and the "
                 "gateway socket is delivering")
        return
    while True:
        try:
            cutoff = (datetime.now(UTC)
                      - timedelta(hours=GAP_SWEEP_LOOKBACK_H)).isoformat()
            threads = await db.all_threads(include_archived=False)
            recent = [t for t in threads if (t.updated_at or "") >= cutoff]
            filled = 0
            for t in recent:
                try:
                    n = await _import_transcript_messages(
                        t.id, t.bot_id, mark_followup=True)
                except Exception:
                    log.debug("gap sweep: %s failed", t.id, exc_info=True)
                    continue
                if n:
                    filled += n
                    log.warning(
                        "gap sweep recovered %d message(s) for %s/%s — a live "
                        "delivery path missed them", n, t.bot_id, t.id)
            if filled:
                log.warning("gap sweep: %d message(s) recovered this pass", filled)
        except Exception:
            log.exception("gap sweep loop error")
        await asyncio.sleep(GAP_SWEEP_EVERY_S)


def _iso_from_transcript_ts(ts) -> str | None:
    """A transcript item's timestamp, in the exact spelling now_iso() uses.

    The DB honours a supplied created_at precisely so a recovered answer files
    where the conversation actually happened — but the sweep never SUPPLIED
    one, so every swept answer was stamped "now" anyway (observed on the
    sweep's first live run: 41 messages from three different days all landed
    at once, timestamped today). Message lines stamp `timestamp` as ISO-8601
    with a Z suffix (verified against live session files); created_at sorts as
    a STRING, so the Z spelling must be normalised to the +00:00 one or a
    same-second pair of rows from the two sources can interleave wrongly.
    Older trajectory-style lines carry epoch milliseconds; both are accepted,
    anything else falls back to now (late but ordered).
    """
    if isinstance(ts, (int, float)) and ts > 0:
        with contextlib.suppress(ValueError, OSError, OverflowError):
            return datetime.fromtimestamp(ts / 1000, tz=UTC).isoformat()
        return None
    if not ts or not isinstance(ts, str):
        return None
    with contextlib.suppress(ValueError):
        return datetime.fromisoformat(
            ts.replace("Z", "+00:00")).astimezone(UTC).isoformat()
    return None


async def _import_transcript_messages(
    thread_id: str, bot_id: str, *, session_id: str | None = None,
    mark_followup: bool = False, crashed_turn: bool = False,
) -> int:
    """Idempotently re-deliver every assistant text block from a thread's
    transcript into the DB. Returns the count newly imported.

    Genuinely idempotent across MULTI-TURN threads: a transcript holds every turn
    of the session, but the live funnel's in-turn dedup (_is_duplicate_message)
    only compares against the trailing assistant run, so earlier-turn replies
    would re-append as duplicates here. We therefore dedup against the WHOLE
    thread history (the same canonical-key set the transcript viewer uses), so
    re-running only fills genuine gaps. The trailing-run dedup is kept for the
    live in-turn paths, which legitimately allow cross-turn repeats.

    ``crashed_turn`` (startup recovery of a thread stranded in 'thinking'):
    the LAST turn's items — everything after the transcript's final user
    message — skip the whole-history set and use only the live trailing-run
    dedup. A crash-lost reply that happens to repeat an earlier message in a
    long thread ("Done.", "No new items today.") is then still recovered,
    while earlier-turn replies keep the whole-history dedup and never
    re-import. The manual /api/recover sweep keeps whole-history dedup for
    everything (its job is idempotent gap-filling, not turn recovery).

    IDENTITY OVER TEXT. Text matching is the fallback, not the guarantee: an
    item this function has already CONSIDERED — delivered, or found already in
    the thread — is recorded and never looked at again. Two separate drifts
    between the canonical key and what persisting stores each turned this
    backstop into a duplicate machine, and a third is only a matter of time.
    Recording the decision costs one row and makes re-running genuinely free.
    """
    session_key = openclaw.session_key_for(bot_id, thread_id)
    path = (openclaw.session_file_by_id(bot_id, session_id) if session_id else None) \
        or openclaw.resolve_session_file(bot_id, session_key)
    if path is None:
        return 0
    existing = {_canon_msg(m.content or "")
                for m in await db.dump_messages(thread_id) if m.role == "assistant"}
    seen = await db.transcript_items_seen(thread_id)
    # Scoped to the session file: two sessions can back one thread over its
    # life (a resumed gateway session gets a new id), and their item positions
    # both start at zero. `uid` (not `idx`) because idx counts only the items
    # the current include_all emitted — crash recovery reads the same block at
    # a different idx, and the two paths would not recognise each other's work.
    item_prefix = f"tx:{path.stem}:"
    # For crash recovery, find where the stranded turn starts in the transcript
    # (the last user item). include_all=True keeps user items so the boundary
    # is visible; the delivery loop below still imports only text/note kinds.
    items = openclaw.read_transcript_items(path, include_all=crashed_turn)
    last_user_idx = -1
    if crashed_turn:
        for it in items:
            if it.get("kind") == "user":
                last_user_idx = it.get("idx", -1)
    count = 0
    considered: list[str] = []
    for it in items:
        if it.get("kind") not in ("text", "note"):
            continue
        text = it.get("text") or ""
        key = _canon_msg(text)
        if not key:
            continue
        in_crashed_turn = crashed_turn and it.get("idx", -1) > last_user_idx
        item_id = f"{item_prefix}{it.get('uid') or it.get('idx', -1)}"
        # Crash recovery deliberately re-examines the stranded turn: a reply
        # lost to the crash may sit at an index a routine sweep already passed.
        if item_id in seen and not in_crashed_turn:
            continue
        considered.append(item_id)
        if key in existing and not in_crashed_turn:
            continue                      # already somewhere in this thread
        meta = {"followup": True} if mark_followup else None
        # _deliver_assistant_text still dedups against the trailing assistant
        # run, so a crashed-turn item that DID land before the crash is caught.
        # The recent window is OFF here: crash recovery exists to restore a
        # reply that may repeat an earlier turn's words ("Done." twice), and
        # the window must not eat the message being recovered.
        msg = await _deliver_assistant_text(
            thread_id, text, metadata=meta,
            created_at=_iso_from_transcript_ts(it.get("ts")),
            dedup_recent_window=not in_crashed_turn)
        if msg:
            existing.add(key)
            count += 1
    await db.mark_transcript_items(thread_id, considered)
    return count


async def _startup_recovery() -> None:
    """On boot: integrity-check the DB, clear threads stranded in 'thinking' by a
    crash, and reconcile each from its transcript (a reply that landed just before
    the crash is then recovered). No turn survives a restart, so any 'thinking'
    on boot is stale."""
    global _db_integrity_ok
    _db_integrity_ok = await db.integrity_ok()
    if not _db_integrity_ok:
        log.error("DB quick_check did NOT return 'ok' — data may be corrupt; "
                  "consider restoring from %s", config.BACKUP_DIR)
    try:
        stranded = await db.reset_inflight_threads()
    except Exception:
        log.exception("startup: could not reset in-flight threads")
        return
    for t in stranded:
        tid, bid = t["id"], t["bot_id"]
        try:
            n = await _import_transcript_messages(tid, bid, mark_followup=True,
                                                  crashed_turn=True)
            log.info("startup recovery (%s/%s): thinking→idle, recovered %d msg(s)",
                     bid, tid, n)
            await _broadcast_thread_update(tid)
        except Exception:
            log.exception("startup recovery failed for thread %s", tid)
    # Same reasoning one step on: a FAILED turn cannot resume across a restart
    # either, so an 'error' still on a thread at boot is stale state from a turn
    # that ended long ago (seven of them here, the oldest three weeks old). The
    # failure itself is already in the messages and the log — the status badge
    # is just noise that never clears itself.
    try:
        stale = await db.clear_stale_error_threads()
    except Exception:
        log.exception("startup: could not clear stale error threads")
        return
    if stale:
        log.info("startup recovery: cleared stale error status on %d thread(s): %s",
                 len(stale), ", ".join(t["id"] for t in stale[:5]))
        for t in stale:
            with contextlib.suppress(Exception):
                await _broadcast_thread_update(t["id"])


# A picture that degraded to a note. Kept out of the presence comparison below
# so a degraded copy still counts as "this message is already in the thread".
_MEDIA_UNAVAILABLE_RE = re.compile(r"🖼️\s*\*\(image unavailable:.*?\)\*")


def _presence_key(s: str) -> str:
    """Canonical text with every picture REFERENCE removed — prose only.

    Deliberately blunter than :func:`_canon_msg`, and used for exactly one
    question: is this transcript item already represented in the thread, in any
    form? A message can be stored with its pictures served (`/media/…`), with
    some of them degraded to notes, or both, and all three are the same message.
    Too blunt to decide DELIVERY — two different pictures under one caption
    would collide — which is why nothing but the one-time backfill uses it.
    """
    s = _MEDIA_UNAVAILABLE_RE.sub(" ", _MEDIA_DIRECTIVE_RE.sub(" ", _canon_msg(s)))
    return " ".join(s.split())


async def _migrate_transcript_seen_backfill() -> None:
    """Mark transcript items already present in their thread as considered.

    Without this, the first sweep after the dedup fix behaves like the LAST
    sweep before it: legacy rows were ingested before origins were recorded, so
    their served paths cannot be bridged back to the agent's deleted scratch
    files, and every media message in history reads as missing exactly once
    more. The fix stops the bleeding; this stops the upgrade itself from
    bleeding.

    Conservative in the direction that matters: an item is marked only when its
    prose is already in the thread. A genuine undelivered gap has no match, is
    left unmarked, and the next sweep delivers it as usual.
    """
    marker = config.DATA_DIR / ".transcript-seen-backfill-done"
    if marker.exists():
        return
    threads = marked = 0
    for t in await db.all_threads(include_archived=True):
        try:
            path = openclaw.resolve_session_file(
                t.bot_id, openclaw.session_key_for(t.bot_id, t.id))
            if path is None:
                continue
            present = {_presence_key(m.content or "")
                       for m in await db.dump_messages(t.id) if m.role == "assistant"}
            present.discard("")
            ids = [f"tx:{path.stem}:{it.get('uid') or it.get('idx', -1)}"
                   for it in openclaw.read_transcript_items(path)
                   if it.get("kind") in ("text", "note")
                   and _presence_key(it.get("text") or "") in present]
            await db.mark_transcript_items(t.id, ids)
            threads += 1
            marked += len(ids)
        except Exception:
            log.warning("transcript-seen backfill skipped thread %s", t.id, exc_info=True)
    with contextlib.suppress(OSError):
        marker.write_text(now_iso())
    log.info("transcript-seen backfill: %d item(s) across %d thread(s)", marked, threads)


async def _migrate_sanitize_stored_messages() -> None:
    """One-time backfill: strip internal scaffolding from messages persisted
    before the sanitizer existed (raw runtime-context walls, system-reminder
    blocks). Runs once, guarded by a marker file. A message that is nothing but
    scaffolding sanitizes to empty and, if it has no media, is deleted — matching
    the gateway, which never stores such rows in the first place.

    Idempotent by construction (sanitize is idempotent) and cheap on reruns
    (the marker short-circuits), but also safe if the marker is lost."""
    marker = config.DATA_DIR / ".sanitize-migration-done"
    if marker.exists():
        return
    changed = deleted = 0
    try:
        for t in await db.all_threads(include_archived=True):
            for m in await db.dump_messages(t.id):
                if m.role not in ("assistant", "user"):
                    continue
                cleaned = (openclaw_text.sanitize_assistant_visible_text(m.content or "")
                           if m.role == "assistant"
                           else openclaw_text.sanitize_user_visible_text(m.content or ""))
                # Only act when real scaffolding was removed from the interior —
                # skip rows whose sole difference is surrounding whitespace (the
                # sanitizer's trailing .strip()), which is pointless row churn.
                if cleaned == (m.content or "").strip():
                    continue
                has_media = bool(m.media_url) or "[[media:" in (m.content or "")
                if not cleaned.strip() and not has_media:
                    await db.delete_message(m.id)
                    deleted += 1
                else:
                    await db.update_message_content(m.id, cleaned)
                    changed += 1
    except Exception:
        log.exception("sanitize migration failed (will retry next boot)")
        return
    with contextlib.suppress(OSError):
        marker.write_text(f"changed={changed} deleted={deleted}\n")
    if changed or deleted:
        log.info("sanitize migration: cleaned %d message(s), removed %d scaffolding-only row(s)",
                 changed, deleted)


async def _sweep_orphan_blobs() -> None:
    """Reconcile the files table against FILES_DIR/MEDIA_DIR on boot.

    - Leftover *.part temp files from an interrupted upload → delete.
    - FILES_DIR blobs with no DB row → ADOPT (insert a row), never delete.
      On-box agents legitimately write straight into FILES_DIR and reference
      the path (a scheduled agent pipeline drops dated images there daily);
      those files never get a row. This sweep used to delete them, and because
      the service restarts nightly around 02:30 for the backup quiesce, every
      boot wiped the day's agent-dropped files — the File Server "periodic
      wipe". A blob still mid-write at boot is likewise adopted, which is
      recoverable; deleting it was not.
    - DB rows whose blob is missing (e.g. a restore that dropped the blobs) →
      purge, since their download would 404.
    """
    try:
        rows = await db.list_files()
    except Exception:
        log.exception("orphan sweep: could not list files")
        return
    known = {r["stored_name"] for r in rows}
    for d in (FILES_DIR, MEDIA_DIR):
        with contextlib.suppress(OSError):
            for p in d.glob("*.part"):
                with contextlib.suppress(OSError):
                    p.unlink()
                    log.info("orphan sweep: removed stale partial upload %s", p.name)
    adopted = 0
    untracked: list[Path] = []
    with contextlib.suppress(OSError):
        untracked = sorted(
            p for p in FILES_DIR.iterdir()
            if p.is_file() and not p.name.endswith(".part") and p.name not in known
        )
    for p in untracked:
        try:
            st = p.stat()
            created = datetime.fromtimestamp(st.st_mtime, UTC).isoformat()
            mime = (mimetypes.guess_type(p.name)[0] or "").lower()
            # An agent drops the file under its real name, so name == stored_name;
            # the size counts toward the server-wide storage cap from now on.
            await db.adopt_file(p.name[:255], p.name, st.st_size, mime or None,
                                created, source="fileserver")
            adopted += 1
        except Exception:
            log.exception("orphan sweep: could not adopt untracked blob %s", p.name)
    if adopted:
        log.info("orphan sweep: adopted %d untracked File Server blob(s)", adopted)
    # A row whose blob is gone is worse than useless: it lists in /api/files,
    # its download 404s, an agent told to read it off disk fails on a path that
    # cannot exist, and its `size` still eats the server-wide storage cap. This
    # box reached 43 rows / 0 blobs — a File Server listing that was 100% dead.
    # It used to only WARN, which is why they accumulated for two months.
    #
    # Guard first: if FILES_DIR itself does not resolve (unmounted share, broken
    # symlink — it IS a symlink on this box) then EVERY blob looks absent and a
    # blind purge would delete the whole table. That case skips entirely.
    try:
        dir_ok = FILES_DIR.is_dir()
    except OSError:
        dir_ok = False
    if not dir_ok:
        log.error("orphan sweep: %s does not resolve to a directory — skipping "
                  "the phantom-record purge (every row would look orphaned)",
                  FILES_DIR)
        return
    try:
        purged = await db.delete_files_missing_from(FILES_DIR)
    except Exception:
        log.exception("orphan sweep: could not purge file records with no blob")
        return
    if purged:
        log.warning("orphan sweep: removed %d file record(s) with NO blob on "
                    "disk (their download would 404) — e.g. %s", len(purged),
                    ", ".join(r["name"] for r in purged[:5]))


# Health/monitoring state (surfaced by /api/health). None = not yet known.
_db_integrity_ok: bool | None = None      # startup quick_check, refreshed per backup
_last_backup_ok: bool | None = None       # did the most recent snapshot verify?
_last_backup_at: str | None = None        # ISO timestamp of the last attempt
_last_good_backup: Path | None = None     # newest verified-good snapshot (pinned)


def _prune_backups() -> None:
    keep = max(1, SETTINGS.backup_keep)
    snaps = sorted(config.BACKUP_DIR.glob("chats-*.db"))
    for p in snaps[:-keep]:
        # Never rotate away the newest verified-good snapshot: if the live DB
        # goes corrupt, every later snapshot fails verification and would
        # otherwise push the last healthy copy out of the keep window.
        if _last_good_backup is not None and p == _last_good_backup:
            continue
        with contextlib.suppress(OSError):
            p.unlink()
    # Failed snapshots are renamed *.corrupt so they never count toward
    # retention; keep at most 2 for forensics so persistent live-DB corruption
    # can't accumulate them unboundedly.
    corrupt = sorted(config.BACKUP_DIR.glob("chats-*.db.corrupt"))
    for p in corrupt[:-2]:
        with contextlib.suppress(OSError):
            p.unlink()


def _mirror_blobs_sync() -> None:
    """Best-effort mirror of the blob stores into ONE rolling dir under
    BACKUP_DIR (not per-snapshot — disk stays ~= live blob size). Uses
    `rsync -a --delete` when available, else a shutil copy+prune fallback.
    Raised errors are swallowed by the caller so a mirror failure never breaks
    the DB snapshot."""
    mirror = config.BACKUP_DIR / "blobs-mirror"
    mirror.mkdir(parents=True, exist_ok=True)
    rsync = shutil.which("rsync")
    for src in (config.FILES_DIR, config.MEDIA_DIR):
        if not src.exists():
            continue
        dst = mirror / src.name
        if rsync:
            # trailing slashes: mirror the CONTENTS of src into dst.
            subprocess.run([rsync, "-a", "--delete", f"{src}/", f"{dst}/"],
                           check=True, capture_output=True, timeout=1800)
        else:
            dst.mkdir(parents=True, exist_ok=True)
            names: set[str] = set()
            for p in src.iterdir():
                if p.is_file():
                    names.add(p.name)
                    tgt = dst / p.name
                    if (not tgt.exists()
                            or tgt.stat().st_size != p.stat().st_size
                            or tgt.stat().st_mtime < p.stat().st_mtime):
                        shutil.copy2(p, tgt)
            for p in dst.iterdir():
                if p.is_file() and p.name not in names:
                    with contextlib.suppress(OSError):
                        p.unlink()


async def _make_backup() -> Path | None:
    """Write + verify one rotated online snapshot of the DB. WAL-safe.

    A snapshot that fails verification is renamed aside (*.corrupt) BEFORE
    pruning, so it never evicts a known-good snapshot from the rotation. The
    outcome is recorded for /api/health (last_backup_ok/last_backup_at)."""
    global _db_integrity_ok, _last_backup_ok, _last_backup_at, _last_good_backup
    # UTC, and AWARE. This is read back by _iso_age_seconds, which treats a
    # naive stamp as UTC — so a local-time stamp on a UTC+9 box reported
    # backup_age_s = -24714, i.e. a backup nine hours in the future, and
    # `backup_stale` (age > threshold) could never fire. A staleness alarm
    # that cannot go off is worse than none: it reports health.
    _last_backup_at = datetime.now(UTC).isoformat(timespec="seconds")
    try:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = config.BACKUP_DIR / f"chats-{stamp}.db"
        await db.backup_to(dest)
        # Mirror the blob stores alongside the DB (best-effort; the DB row →
        # blob-on-disk relationship must survive a restore, or downloads 404).
        with contextlib.suppress(Exception):
            await asyncio.to_thread(_mirror_blobs_sync)

        # Verify the snapshot opens + passes a quick structural check. Runs the
        # blocking stdlib sqlite3 read OFF the event loop so it never stalls
        # live WS/HTTP traffic while scanning the file.
        def _verify(p: str) -> bool:
            import sqlite3 as _sq
            c = _sq.connect(p)
            try:
                r = c.execute("PRAGMA quick_check").fetchone()
                return bool(r) and str(r[0]).lower() == "ok"
            finally:
                c.close()
        ok = False
        with contextlib.suppress(Exception):
            ok = await asyncio.to_thread(_verify, str(dest))
        _last_backup_ok = ok
        # Refresh the live-DB integrity signal on the same cadence.
        with contextlib.suppress(Exception):
            _db_integrity_ok = await db.integrity_ok()
        if not ok:
            log.error("DB snapshot failed integrity check: %s", dest)
            with contextlib.suppress(OSError):
                dest = dest.rename(dest.parent / (dest.name + ".corrupt"))
            _prune_backups()
            return None
        _last_good_backup = dest
        _prune_backups()
        log.info("DB snapshot: %s (%d bytes, verified=%s)",
                 dest.name, dest.stat().st_size, ok)
        return dest
    except Exception:
        _last_backup_ok = False
        log.exception("DB backup failed")
        return None


async def _backup_loop() -> None:
    """Snapshot the DB on a cadence, for the life of the process.

    Every pass is guarded: a snapshot that raises (disk full, a VACUUM losing
    a race) logs and the loop keeps its cadence. The unguarded version ended
    the task on the first such failure, and because `last_backup_ok` only ever
    holds the LAST recorded outcome, health went on reporting that stale
    success indefinitely — see the backup_age_s / background_loops fields in
    /api/health, which exist to make that visible.
    """
    interval = SETTINGS.backup_interval
    if interval <= 0:
        return
    _loop_beat("backup", interval)
    try:
        # First snapshot shortly after boot, then on the configured cadence.
        await asyncio.sleep(60)
        while True:
            try:
                await _make_backup()
            except Exception:
                log.exception("backup pass failed; loop continues")
            _loop_beat("backup", interval)
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        _loop_beats.pop("backup", None)          # shutdown, not a fault


# How many [[doc:…]] references one message may inline, and how much of each.
# Both are availability guards — see the comment inside _resolve_doc_refs.
_DOC_REF_MAX = 8
_DOC_REF_MAX_CHARS = 150_000


def _read_head(path: Path, max_chars: int) -> tuple[str, bool]:
    """Read at most `max_chars` characters. Returns (text, was_truncated)."""
    with path.open("r", encoding="utf-8", errors="replace") as f:
        chunk = f.read(max_chars + 1)
    if len(chunk) > max_chars:
        return chunk[:max_chars], True
    return chunk, False


_PDF_OCR_PAGES = 10          # OCR page cap (bounds worst-case cost per doc)
_PDF_OCR_DPI = 150
_PDF_OCR_TIMEOUT = 120       # per-file budget for pdftoppm rendering


def _pdf_head_text(path: Path, max_chars: int) -> tuple[str, bool]:
    """Extract up to `max_chars` characters of text from a PDF.

    Fast path is poppler's `pdftotext` for text-layer PDFs. Image-only PDFs
    (scans, app screenshots) have no text layer and fall back to OCR via
    rapidocr-onnxruntime when installed (pages rendered with `pdftoppm`).
    Returns ("", False) when nothing could be extracted — callers fall back
    to an honest attachment marker instead of inlining binary mojibake.
    Off the event loop, because this sits on the send hot path.
    """
    try:
        exe = shutil.which("pdftotext")
        if exe:
            out = subprocess.run(
                [exe, "-enc", "UTF-8", "-q", str(path), "-"],
                capture_output=True, timeout=60)
            if out.returncode == 0:
                text = out.stdout.decode("utf-8", errors="replace").strip()
                if text:
                    if len(text) > max_chars:
                        return text[:max_chars], True
                    return text, False
    except (OSError, subprocess.SubprocessError):
        pass
    # No text layer — try OCR.
    return _pdf_ocr_head_text(path, max_chars)


def _pdf_ocr_head_text(path: Path, max_chars: int) -> tuple[str, bool]:
    """OCR an image-only PDF with rapidocr-onnxruntime (optional dependency).

    Renders up to _PDF_OCR_PAGES pages with pdftoppm and runs OCR per page,
    concatenating the results. Returns ("", False) when OCR is unavailable or
    nothing was detected — the caller then shows the attachment marker.
    """
    try:
        from rapidocr_onnxruntime import RapidOCR  # type: ignore
    except ImportError:
        return "", False
    renderer = shutil.which("pdftoppm")
    if not renderer:
        return "", False
    try:
        ocr = RapidOCR()
    except Exception:
        return "", False
    with tempfile.TemporaryDirectory(prefix="dispatch-pdf-ocr-") as tmp:
        out_prefix = str(Path(tmp) / "page")
        try:
            subprocess.run(
                [renderer, "-png", "-r", str(_PDF_OCR_DPI),
                 "-f", "1", "-l", str(_PDF_OCR_PAGES),
                 str(path), out_prefix],
                capture_output=True, timeout=_PDF_OCR_TIMEOUT)
        except (OSError, subprocess.SubprocessError):
            return "", False
        pages = sorted(Path(tmp).glob("page-*.png"))
        if not pages:
            return "", False
        chunks: list[str] = []
        total = 0
        truncated = False
        for png in pages:
            try:
                result, _ = ocr(str(png))
            except Exception:
                continue
            if not result:
                continue
            page_text = "\n".join(line[1] for line in result).strip()
            if not page_text:
                continue
            room = max_chars - total
            if room <= 0:
                truncated = True
                break
            if len(page_text) > room:
                chunks.append(page_text[:room])
                truncated = True
                break
            chunks.append(page_text)
            total += len(page_text)
        return "\n\n".join(chunks).strip(), truncated


async def _resolve_doc_refs(text: str) -> str:
    """Resolve [[doc:file_id|name]] refs to inline content (text docs) or download links.

    Kept a separate pass from _ingest_content_media so [[doc:…]] stays intact in
    the DB for frontend rendering — only the agent-facing copy is resolved here.
    """
    if not text or "[[doc:" not in text:
        return text

    parts: list[str] = []
    last_end = 0
    # Bounds. Without them a single 64KB message packing ~1,450 repeats of one
    # [[doc:<id>]] made 1,450 sequential whole-file read_text() calls ON THE
    # EVENT LOOP and assembled a ~218MB prompt string — an unauthenticated
    # freeze-plus-cost attack, since Safe Mode may both drop a file and send a
    # message naming it. Cap how many refs one message may expand, expand each
    # distinct document only once, and read only what we are willing to keep.
    expanded = 0
    seen_ids: set[str] = set()
    for m in _DOC_REF_RE.finditer(text):
        parts.append(text[last_end:m.start()])
        file_id = m.group(1).strip()
        name = (m.group(2) or "").strip()

        if expanded >= _DOC_REF_MAX or file_id in seen_ids:
            # Already inlined (or over budget): leave a cheap marker instead.
            parts.append(f"[📎 {name or file_id}]")
            last_end = m.end()
            continue

        rec = await db.get_file(file_id)
        if not rec:
            parts.append(m.group(0))  # keep original
        else:
            mime = rec.get("mime", "")
            stored_name = rec.get("stored_name", "")
            ext = Path(stored_name).suffix.lower()
            is_pdf = ext == ".pdf" or mime == "application/pdf"
            is_text = mime.startswith("text/") or mime in _TEXT_MIMES or ext in DOC_EXTS

            if is_pdf or is_text:
                stored_path = _safe_file_path(stored_name)
                if stored_path is None:
                    # A row whose stored name resolves outside FILES_DIR is
                    # not a document we hold; never read through it.
                    parts.append(f"[📎 {name or file_id}]")
                    last_end = m.end()
                    continue
                try:
                    max_chars = _DOC_REF_MAX_CHARS
                    # Read the cap (+1 to detect truncation) rather than the
                    # whole blob: a 50MB text file was fully loaded into memory
                    # only to be sliced down to 150KB immediately after. Off the
                    # event loop, because this sits on the send hot path.
                    if is_pdf:
                        # PDFs are binary, not text: read them through
                        # pdftotext so the agent gets real content instead of a
                        # bare path or mojibake. Image-only PDFs (scans, app
                        # screenshots) fall back to OCR when rapidocr is
                        # installed; otherwise the marker below is used.
                        content, truncated = await asyncio.to_thread(
                            _pdf_head_text, stored_path, max_chars)
                    else:
                        content, truncated = await asyncio.to_thread(
                            _read_head, stored_path, max_chars)
                    if not content.strip():
                        raise OSError("no extractable text")
                    if truncated:
                        content += (
                            f"\n\n[... truncated at {max_chars // 1000}KB; "
                            f"full file at: {stored_path}]"
                        )
                    label = name or rec.get("name", stored_name)
                    parts.append(
                        f"\n--- BEGIN DOCUMENT: {label} ---\n"
                        f"{content}\n"
                        f"--- END DOCUMENT: {label} ---\n"
                    )
                    expanded += 1
                    seen_ids.add(file_id)
                except (OSError, UnicodeDecodeError):
                    if is_pdf:
                        # Honest fallback: tell the agent where the file is
                        # rather than inline binary garbage.
                        label = name or rec.get("name", stored_name)
                        kb = rec.get("size", 0) // 1024
                        parts.append(
                            f"[📎 Attached file: {label} ({kb}KB, {mime or 'unknown type'}) — "
                            f"{FILES_DIR / stored_name}]"
                        )
                    else:
                        parts.append(m.group(0))
            else:
                label = name or rec.get("name", stored_name)
                kb = rec.get("size", 0) // 1024
                parts.append(
                    f"[📎 Attached file: {label} ({kb}KB, {mime or 'unknown type'}) — "
                    f"{FILES_DIR / stored_name}]"
                )
        last_end = m.end()

    parts.append(text[last_end:])
    return "".join(parts)


async def _media_second_look(
    thread_id: str, bot_id: str, session_key: str, persisted: list[MessageOut],
) -> None:
    """One automated corrective turn when a reply claims media it didn't deliver.

    Checks the PERSISTED messages (post-salvage/ingest): if any text promises
    images/videos but no message in the turn carries a media_url, a [[media:...]]
    directive, or even an "unavailable" note that resolved, nudge the agent to
    re-emit proper [[media:/path|caption]] lines. Exactly one nudge per turn —
    the nudge's own reply is persisted (salvage applies to it too) but never
    re-checked, so this cannot loop.
    """
    delivered = any(
        m.media_url or "[[media:" in (m.content or "") for m in persisted
    )
    if delivered:
        return
    claims = any(_claims_media(m.content or "") for m in persisted)
    broken = any(_MEDIA_UNAVAILABLE_MARK in (m.content or "") for m in persisted)
    if not (claims or broken):
        return
    log.info("media second look (%s/%s): reply claimed media but delivered none",
             bot_id, thread_id)
    try:
        async with _agent_sem:
            fix = await openclaw.send_to_agent(
                bot_id=bot_id, session_key=session_key,
                message=_MEDIA_RECHECK_PROMPT,
            )
    except openclaw.AgentError as e:
        log.warning("media second look failed (%s/%s): %s — %s",
                    bot_id, thread_id, e.message, e.detail)
        return
    slim = {k: fix.metadata[k] for k in ("model", "provider")
            if fix.metadata.get(k)}
    for payload in fix.payloads:
        # Through the SHARED funnel, like every other delivery. This used to
        # persist directly, which was safe when the transcript followers were
        # the only other readers of the session — but the gateway WS transport
        # delivers the fix reply the moment the gateway writes it, seconds
        # before send_to_agent returns, and a direct persist has no dedup at
        # all: both copies posted (live, 2026-08-10 05:48:33 + 05:48:38,
        # on one thread), and each ingest also duplicated the picture blobs.
        await _deliver_assistant_text(
            thread_id, payload.text,
            media_url=_normalize_media(payload.media_url),
            metadata={**slim, "media_recheck": True,
                      **({"sub": True} if payload.sub else {})},
            stream=True,
        )


# Backoff for turns the gateway refused at the door (draining / restarting /
# not up yet). ~90s total — comfortably covers a normal gateway restart, so a
# family member mid-conversation sees the bot "thinking" for a moment instead
# of an error. The turn never started on the gateway, so retries can't
# double-run anything (see openclaw.GatewayUnavailable).
_GATEWAY_RETRY_DELAYS = (3, 6, 12, 24, 45)


# Which way turns actually went since boot. `auto` mode picks per attempt, so
# without this a box could be quietly paying the CLI spawn on every turn while
# the socket flag said "1" — surfaced in /api/health as `turn_transport`.
_turn_transport_counts: dict[str, int] = {"socket": 0, "cli": 0}


async def _dispatch_turn(bot_id: str, session_key: str, message: str
                         ) -> openclaw.AgentReply:
    """One attempt at a turn, over whichever transport is available.

    The socket path and the subprocess path answer with the same
    `AgentReply` — `_parse_reply` is shared — so everything downstream of here
    is transport-blind. The choice is made per attempt, not once at boot,
    because the socket can drop and come back while the app runs.
    """
    mode = SETTINGS.turn_transport
    if mode != "0":
        client = _gateway_client
        if client is not None and client.connected.is_set():
            _turn_transport_counts["socket"] += 1
            # Name the run BEFORE sending it. The gateway adopts our
            # idempotency key as the runId, so choosing it here is what lets
            # the delta stream be routed to this thread and what lets a
            # reconnect ask `agent.wait` how this exact turn ended.
            run_id = uuid.uuid4().hex
            thread_id = session_key.split(":", 2)[-1]
            # Subscribing to the session is what admits its `chat` deltas and
            # `session.tool` events; the global subscription only carries
            # transcript messages. Failure is not fatal — it costs live
            # streaming for this turn, never the reply.
            try:
                await client.subscribe_session(session_key)
            except Exception:
                client.track_session(session_key)
                log.warning("could not subscribe to %s; live deltas may be "
                            "silent for this turn", session_key, exc_info=True)
            _inflight_register(_InflightRun(run_id, session_key, thread_id,
                                            bot_id, time.time()))
            try:
                reply = await openclaw.send_via_gateway(
                    client, bot_id=bot_id, session_key=session_key,
                    message=message, run_id=run_id)
            except (openclaw.GatewayUnavailable, gateway_ws.GatewayRunRefused):
                # Refused at the door: nothing ran, so there is nothing to
                # chase after a reconnect. Anything else — a timeout, a lost
                # socket — leaves the entry in place ON PURPOSE, because the
                # run is (or was) underway and its reply has nowhere else to
                # come from.
                _inflight_done(run_id)
                raise
            # The answer is in hand, so the run is over and cannot be stranded.
            _inflight_done(run_id)
            return reply
        if mode == "1":
            raise openclaw.GatewayUnavailable(
                f"The agent gateway is down or restarting, so {bot_id} can't "
                "answer right now. Your message is saved — try again in a "
                "minute.",
                detail="DISPATCH_TURN_TRANSPORT=1 and no gateway socket")
    _turn_transport_counts["cli"] += 1
    return await openclaw.send_to_agent(
        bot_id=bot_id, session_key=session_key, message=message)


async def _send_with_gateway_retry(
    bot_id: str, session_key: str, message: str, thread_id: str,
) -> openclaw.AgentReply:
    """Dispatch a turn, retrying only turns the gateway refused at the door.

    The semaphore is taken per attempt and released during the sleeps, so a
    gateway restart doesn't serialize every other thread's turn behind it.
    """
    for delay in _GATEWAY_RETRY_DELAYS:
        try:
            async with _agent_sem:
                return await _dispatch_turn(bot_id, session_key, message)
        except openclaw.GatewayUnavailable as e:
            if _shutting_down:
                raise
            log.warning("gateway unavailable for %s/%s — retrying in %ds (%s)",
                        bot_id, thread_id, delay, (e.detail or "")[:160])
            await asyncio.sleep(delay)
    async with _agent_sem:
        return await _dispatch_turn(bot_id, session_key, message)


async def run_agent_turn(thread_id: str, bot_id: str, text: str) -> None:
    """Send `text` to the bot for `thread_id`, persist + broadcast the reply.

    Serialised per-thread (lock) and globally rate-limited (semaphore).
    """
    # Two backends, one entry point. A bot carrying an `api` block was set up
    # through "Connect an AI" and talks straight to an LLM provider: no CLI to
    # spawn, no session transcript to tail, so none of the watcher / follower /
    # reconciler machinery below applies to it. It still takes THIS thread's
    # lock (queued turns serialise identically) and still persists through
    # _deliver_assistant_text, so from the UI's side the two are the same thing.
    bot = config.get_bot(bot_id)
    if bot is not None and bot.api:
        _thread_bot[thread_id] = bot_id   # authoritative attribution for redaction
        async with _thread_locks[thread_id]:
            _forget_thread_delivery(thread_id)
            await llm_api.run_api_turn(thread_id, bot_id, text)
        return

    lock = _thread_locks[thread_id]
    session_key = openclaw.session_key_for(bot_id, thread_id)
    _thread_bot[thread_id] = bot_id   # authoritative attribution for redaction
    _mirror_nudge()                   # someone is chatting → mirror polls fast
    async with lock:
        # Inside the lock: with turns queued, stopping the follower any earlier
        # would let the PREVIOUS turn's finally spawn a fresh follower that
        # tails this turn's transcript concurrently with its watcher.
        _stop_follower(thread_id)   # in-turn watcher takes over from here
        # Turn-scoped dedup: clear the in-memory delivered-key set at turn start
        # so the four redundant in-turn sources still dedup against each other,
        # but a message legitimately repeated in a LATER turn isn't suppressed.
        # (The previous turn's follower was just cancelled above, so nothing is
        # mid-delivery against these keys.)
        _forget_thread_delivery(thread_id)
        await db.update_thread_status(thread_id, "thinking")
        await manager.broadcast(
            {"type": "thinking", "thread_id": thread_id, "bot_id": bot_id,
             "status": "started"}
        )
        await _broadcast_thread_update(thread_id)
        handoff: dict = {}
        session_id: str | None = None   # exact sessionId from the CLI reply
        # Hoisted out of the try: the error handlers need to know whether the
        # reply already reached the family before they call the turn a failure.
        persisted: list[MessageOut] = []
        watcher = asyncio.create_task(
            _watch_progress(thread_id, bot_id, session_key, handoff)
        )
        try:
            agent_text = await _resolve_doc_refs(text)
            reply = await _send_with_gateway_retry(
                bot_id, session_key, agent_text, thread_id)
            # The authoritative transcript path is built from this exact id (the
            # index can lag), so the reconciler/follower below never miss a reply.
            session_id = reply.metadata.get("session_id")
            # Full metadata (incl. token totals) goes on the LAST message only,
            # so a multi-message reply doesn't double-count tokens; earlier
            # messages keep just model/provider for the per-message badge.
            slim = {k: reply.metadata[k] for k in ("model", "provider")
                    if reply.metadata.get(k)} or None

            # All messages, not just the final one: a multi-step turn narrates
            # between tool calls, but send_to_agent returns ONLY the last block
            # as the reply. The watcher captured every assistant text block
            # (handoff["texts"], in transcript order); persist each one that
            # isn't a final payload and isn't already in the thread — instantly
            # (they already streamed live in the working panel), in order, ahead
            # of the final streamed reply. Tool calls / thinking stay ephemeral.
            # The payload and the transcript are two recordings of the same
            # block and can differ — the gateway's copy has been seen without
            # the agent's `MEDIA:/path` lines. Settle on one text per payload
            # BEFORE final_keys is built from it, or the narration loop below
            # compares against a version nothing will actually post and lets
            # the twin through (16 of the 34 historic in-turn duplicate pairs).
            narrations = handoff.get("texts", [])
            settled = [_settle_payload(p, narrations) for p in reply.payloads]
            final_keys = {_canon_msg(t) for t, _ in settled if t.strip()}
            for narr in handoff.get("texts", []):
                # The final block(s) come from the CLI payload below (with full
                # metadata); skip them here. Everything else goes out instantly,
                # in transcript order, through the shared funnel (deduped).
                if _canon_msg(narr) in final_keys:
                    continue
                msg = await _deliver_assistant_text(thread_id, narr, metadata=slim)
                if msg:
                    persisted.append(msg)

            # The LAST payload is the reply; earlier ones are narration and
            # collapse. But a trailing tool warning is not a reply — it is the
            # runtime narrating a failed tool call — and when one arrives last
            # it takes the slot, demoting the agent's ACTUAL message to
            # collapsed working output. Seen in the family chat: a turn ending
            # in a question to the operator ("What's your vision for the bot's
            # avatar style?") was persisted with sub=True and never appeared as
            # a message, so it was never answered. Both payloads were hidden and
            # the entire turn went silent.
            #
            # So `last` is the last payload that is actually the agent SPEAKING.
            def _is_speech(p) -> bool:
                return bool((p.text or "").strip()) and not openclaw_text.is_tool_warning(p.text or "")

            speech = [i for i, p in enumerate(reply.payloads) if _is_speech(p)]
            last = speech[-1] if speech else len(reply.payloads) - 1
            for i, payload in enumerate(reply.payloads):
                text, media_url = settled[i]
                # A payload that was ONLY the NO_REPLY token (and has no media)
                # is the agent declining to post — persist nothing for it.
                if not _strip_reply_directive(_strip_no_reply(text)) and not media_url:
                    continue
                # Extra payloads of a multi-part reply (rare; the narration above
                # is the usual multi-message case) collapse as "sub".
                is_sub = payload.sub or (i < last)
                meta = (reply.metadata or None) if i == last else slim
                if is_sub:
                    meta = {**(meta or {}), "sub": True}
                msg = await _deliver_assistant_text(
                    thread_id, text,
                    media_url=_normalize_media(media_url),
                    metadata=meta, stream=True,
                )
                if msg:
                    persisted.append(msg)
            # THE TURN IS OVER THE MOMENT THE LAST PAYLOAD IS OUT. The second
            # look is a repair pass over messages the family has ALREADY READ,
            # and running it before the thread goes idle held the typing
            # indicator up for its whole duration — measured at 276 ms of the
            # bot visibly "still thinking" after it had finished speaking.
            await db.update_thread_status(thread_id, "idle")
            await manager.broadcast(
                {"type": "thinking", "thread_id": thread_id, "bot_id": bot_id,
                 "status": "stopped"}
            )
            await _broadcast_thread_update(thread_id)
            # POST-delivery, so its failure is not the turn's failure. Letting
            # it raise put the thread in `error` — a red banner over a
            # conversation that went perfectly.
            try:
                await _media_second_look(thread_id, bot_id, session_key, persisted)
            except Exception:
                log.exception("media second look failed after a delivered turn "
                              "(%s/%s)", bot_id, thread_id)
        except openclaw.AgentError as e:
            log.warning("agent turn failed (%s/%s): %s — %s", bot_id, thread_id, e.message, e.detail)
            # The model may have finished and flushed its reply to the transcript
            # even though the CLI reported an error/timeout. Recover it before
            # showing an error, so a real answer isn't buried behind a toast.
            recovered: list[MessageOut] = []
            with contextlib.suppress(Exception):
                recovered = await _reconcile_transcript(
                    thread_id, bot_id, session_key, handoff, session_id)
            if recovered:
                log.info("recovered %d message(s) from transcript despite CLI error (%s/%s)",
                         len(recovered), bot_id, thread_id)
                await db.update_thread_status(thread_id, "idle")
            else:
                await db.update_thread_status(thread_id, "error")
                await manager.broadcast(
                    {"type": "error", "thread_id": thread_id, "bot_id": bot_id,
                     "message": e.message, "detail": e.detail}
                )
        except Exception as e:  # pragma: no cover - defensive
            log.exception("unexpected agent turn error")
            if persisted:
                # The reply is already in the thread and on every screen. What
                # failed is something after it, so the turn is not an error —
                # marking it one puts a red banner and a toast over a
                # conversation the family can see went fine.
                await db.update_thread_status(thread_id, "idle")
            else:
                await db.update_thread_status(thread_id, "error")
                await manager.broadcast(
                    {"type": "error", "thread_id": thread_id, "bot_id": bot_id,
                     "message": "Something went wrong.", "detail": str(e)[:300]}
                )
        finally:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher
            await manager.broadcast(
                {"type": "thinking", "thread_id": thread_id, "bot_id": bot_id,
                 "status": "stopped"}
            )
            await _broadcast_thread_update(thread_id)
            # Safety net: re-scan this turn's whole transcript window and deliver
            # anything the live watcher missed (e.g. it resolved the session file
            # late). Deduped by the shared funnel — harmless if nothing's missing.
            with contextlib.suppress(Exception):
                await _reconcile_transcript(thread_id, bot_id, session_key, handoff, session_id)
            # Keep listening for OpenClaw-side follow-ups (subagent announces
            # that arrive after the CLI turn has already returned). Resume at
            # the watcher's exact transcript position — zero gap.
            _stop_follower(thread_id)
            # Don't spawn a fresh follower while the app is tearing down — a
            # cancelled turn's finally still runs, and the orphan task would
            # outlive the DB ("Task was destroyed but it is pending").
            if not _shutting_down:
                task = asyncio.create_task(_follow_session(
                    thread_id, bot_id, session_key,
                    path=handoff.get("path"), offset=handoff.get("offset"),
                    session_id=session_id,
                ))
                _followers[thread_id] = task
                _track(task)


# Hand llm_api the five pieces of this module it needs to run a turn.
#
# Every one is a lambda rather than a direct reference, and that is deliberate:
# `db` and `manager` are module-level singletons that the test suites replace
# wholesale, and `_deliver_assistant_text` is patched by anything asserting on
# what got persisted. Capturing the bound methods HERE would freeze the
# direct-API path onto whatever existed at import time — the turn would keep
# writing to the process's real database while the test watched a temp one.
# The lambdas re-resolve the globals on every call, so a monkeypatch anywhere
# reaches this path exactly as it reaches the agent path.
llm_api.bind(llm_api.Hooks(
    list_messages=lambda tid, limit: db.list_messages(tid, limit),
    deliver=lambda *a, **kw: _deliver_assistant_text(*a, **kw),
    set_status=lambda tid, status: db.update_thread_status(tid, status),
    broadcast=lambda frame: manager.broadcast(frame),
    thread_update=lambda tid: _broadcast_thread_update(tid),
))


# --------------------------------------------------------------------------- #
# REST: meta / bots
# --------------------------------------------------------------------------- #


# The OpenClaw Node gateway (the thing agent turns actually talk to). A cheap
# TCP connect with a short cache — /api/health must never block on a probe.
_GATEWAY_ADDR = ("127.0.0.1", 18789)
_GATEWAY_PROBE_TTL = 15.0
_gateway_probe: dict = {"ts": 0.0, "ok": None}


async def _gateway_ok() -> bool:
    now = asyncio.get_event_loop().time()
    if _gateway_probe["ok"] is not None and now - _gateway_probe["ts"] < _GATEWAY_PROBE_TTL:
        return _gateway_probe["ok"]
    ok = False
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(*_GATEWAY_ADDR), timeout=1.0)
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        ok = True
    except (TimeoutError, OSError):
        ok = False
    _gateway_probe["ts"] = now
    _gateway_probe["ok"] = ok
    return ok


@app.get("/api/health")
async def health(request: Request):
    """Liveness + degraded-state signals.

    Deliberately un-gated so the container healthcheck works before (and
    without) a PIN — it reads `status` and `db_integrity_ok`, and those two
    are always present. Everything else is operator detail: the absolute data
    directory, whether an agent CLI is installed, whether the gateway answers,
    how many clients are connected. On a 0.0.0.0-bound service that is free
    reconnaissance for anyone who can reach the port, so it is withheld unless
    the caller holds a full session — or no PIN is configured at all, the
    state in which the whole app is open by design.

    An ON-BOX MACHINE caller gets the detail too. Monitoring is the whole
    reason those fields exist — `avatar_snapshots_missing` says a chat is
    showing the wrong face, `truncation_unrepaired` says replies are being
    delivered cut short — and a cron job or a smoke script holds no session
    and never will. Withholding them from the one caller whose job is to
    notice made every signal here invisible to anything but a human with the
    PIN, which is the failure mode this app keeps re-learning: a fault that
    reports nothing looks exactly like health.

    The test is the same one the auth gate's machine branch uses (loopback
    socket, no proxy header, no browser fingerprint), so nothing reachable
    from a browser tab widens by it — a locked tab on this very box still gets
    the two-field body, because a browser stamps Sec-Fetch-*/Origin and a
    remote caller is not on loopback.
    """
    cfg = auth.load()
    detailed = (bool(_session_of(request)) or not cfg.pin_set
                or _on_box_machine(request))
    loops = _loop_health()
    backup_age = _iso_age_seconds(_last_backup_at)
    # `last_backup_ok` latches: it holds the last outcome forever, so a loop
    # that died a week ago still reports the success it had before it died.
    # Age is what distinguishes "backups are fine" from "backups stopped".
    backup_stale = bool(
        SETTINGS.backup_interval > 0 and backup_age is not None
        and backup_age > SETTINGS.backup_interval * 2)
    degraded = (_db_integrity_ok is False
                or any(v["stale"] for v in loops.values())
                or backup_stale or _last_backup_ok is False)
    body = {
        # "ok" unless something we can actually name is wrong. A liveness
        # field nobody reads is the same as no field at all, so a stale loop
        # or an old/failed backup shows up in the ALWAYS-visible status too.
        "status": "degraded" if degraded else "ok",
        # Degraded-state signals (all cached/cheap; see finding "health does
        # not surface DB integrity, backup health, or gateway reachability").
        "db_integrity_ok": _db_integrity_ok,
    }
    if not detailed:
        return body
    # One read of the refill tally for both fields below: a second call would
    # be cheap but could disagree with the first across a cycle boundary.
    _pool_refill_stats = pool_guard.refill_failure_stats()
    body.update({
        "openclaw_available": openclaw.cli_available(),
        "clients": manager.count,
        "data_dir": str(config.DATA_DIR),
        "last_backup_ok": _last_backup_ok,
        "last_backup_at": _last_backup_at,
        # Additive companions to the two latching fields above: how long ago
        # the last attempt was, and whether that is longer than the cadence
        # allows. Existing consumers reading last_backup_ok are untouched.
        "backup_age_s": None if backup_age is None else round(backup_age, 1),
        "backup_stale": backup_stale,
        # name -> {age_s, period_s, stale}. A loop missing from this map never
        # started; a stale one stopped beating. Either way the duty it owns
        # (session purge, DB snapshots) is not being done.
        "background_loops": loops,
        "gateway_ok": await _gateway_ok(),
        # Count only — refusal details (bot names, reasons) stay in the
        # journal and the unlocked UI.
        "reaction_fire_failures_24h":
            reactions.fire_failure_stats()["failures_24h"],
        "image_job_failures_24h":
            image_jobs.failure_stats()["failures_24h"],
        # Local Viewer refusals (outside a root, denied path, hidden file).
        # Count only, same rule as the reaction failures above: the paths a
        # refusal names are exactly what must not leak into a health body.
        "viewer_denied_24h": localview.denial_stats()["denials_24h"],
        # Is the rig answering RIGHT NOW (the worker's breaker), and if not,
        # for how long. Reachability, not prose: no rig URL, no job ids.
        "image_rig": (_clawforge().status() if _image_jobs_configured()
                      else {"reachable": None}),
        # Markers that asked for a picture and got none. A different problem
        # from a failed render — a bot writing markers the pipeline cannot
        # honour — so a different number, count-only like the rest.
        "image_marker_drops_24h": image_jobs.marker_drop_stats()["drops_24h"],
        # Pool-refill refusals (VRAM contention / rig down) — the guard's
        # alert counter, same count-only rule as fire failures above.
        "pool_refill_failures_24h":
            _pool_refill_stats["failures_24h"],
        # ...and WHICH failure. A bare count cannot tell "the rig's renderer
        # is down" (wait, or restart it) from "a co-tenant has the GPU"
        # (wait) from "the image CLI is unset" (fix the unit file), and those
        # want different humans. Kinds only — no prompts, bots or rig prose.
        "pool_refill_failures_24h_by_kind": _pool_refill_stats["by_kind"],
        # Threads pinned to an avatar snapshot whose bytes are gone. Count
        # only, same rule as above: nonzero means the store lost files and
        # those chats are showing the wrong face.
        "avatar_snapshots_missing": len(_missing_snapshots),
        # Gateway WebSocket transport counters. `truncation_unrepaired > 0` is
        # a stop-the-line signal — it means replies are being DELIVERED cut
        # short — and until now the only way to see it was a journal grep the
        # reader had to know to run. Same for the gap/refetch counters. They
        # are process-local and reset on restart, so `since` says what window
        # they cover; without it a reassuring zero could just mean "restarted
        # a minute ago".
        "gateway_ws": _gateway_ws_stats(),
        "turn_transport": {"mode": SETTINGS.turn_transport, **_turn_transport_counts},
    })
    return body


@app.get("/api/openapi.json", include_in_schema=False)
async def openapi_schema(request: Request):
    """The generated OpenAPI document — full session or on-box machine only.

    /openapi.json, /docs and /redoc stay disabled: on a service bound to
    0.0.0.0 they hand a stranger the whole route and model inventory
    (harness, inject, recovery, reactions), which is precisely what Safe Mode
    exists to hide.

    That reasoning never applied to a process on this box. An agent calling
    this API cold has to be TOLD what the routes are, and the alternative to
    serving the schema is a hand-written document that drifts from the code —
    which is what happened, repeatedly. Same gate as the detailed health body.
    """
    if not (_session_of(request) or not auth.load().pin_set
            or _on_box_machine(request)):
        raise HTTPException(403, "Unlock for full access")
    return JSONResponse(app.openapi())


def _agent_backend_available() -> bool:
    """Can *anything* on this host answer a bot turn right now?

    Two backends exist: the `openclaw` CLI on disk and the gateway socket. The
    socket transport needs no binary at all, so an install whose CLI is absent
    (or, in staging, deliberately dead) but whose socket is up is a working
    install — and the first-run "Connect an AI" card must not be shown to it.
    Keying the card on the CLI alone did exactly that on staging.
    """
    if openclaw.cli_available():
        return True
    client = _gateway_client
    return bool(client is not None and client.connected.is_set())


def _gateway_ws_stats() -> dict[str, Any] | None:
    """The router's in-memory counters, or None when the transport is off."""
    router = _gateway_router
    if router is None:
        return None
    # `connected` is the socket's live state, not a counter: the counters can
    # all read zero on a healthy box that simply had no turns since boot, so
    # they cannot say whether the transport is up right now. This can.
    client = _gateway_client
    connected = bool(client is not None and client.connected.is_set())
    return {"mode": SETTINGS.gateway_ws, "since": _gateway_ws_since,
            "connected": connected,
            # In-flight runs: how many turns the gateway has accepted and not
            # yet answered. A number that only grows is the signal that
            # reconnect recovery is not clearing what it chased.
            "inflight_runs": len(_inflight_runs),
            # Events the local queue could not hold. Visible loss beats silent
            # loss, and it lived only in a log line before this.
            "dropped_local": getattr(client, "dropped_local", 0),
            "tick_closes": getattr(client, "tick_closes", 0),
            "transcript_backstop": _transcript_backstop_state(),
            **router.stats}


# --------------------------------------------------------------------------- #
# REST: auth / lock
# --------------------------------------------------------------------------- #


def _request_is_https(request: Request | None) -> bool:
    """Did this request arrive over TLS, directly or through our proxy?

    `X-Forwarded-Proto` is only consulted when a forwarding header proves the
    request came through a proxy (the same presence test the auth gate uses);
    otherwise a client could set it on a plain-http call and pin `Secure` on a
    cookie the browser would then refuse to send back.
    """
    if request is None:
        return False
    if request.url.scheme == "https":
        return True
    proxied = bool(request.headers.get("x-forwarded-for")
                   or request.headers.get("forwarded")
                   or request.headers.get("tailscale-headers-info"))
    if not proxied:
        return False
    proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
    return proto.lower() == "https"


def _issue_cookie_response(body: dict, persistent: bool = False,
                           user_agent: str = "",
                           request: Request | None = None) -> JSONResponse:
    """Issue a fresh full-access session and attach it as an HttpOnly cookie.

    `Secure` is set exactly when the request arrived over TLS (directly, or
    through a proxy that terminated it — Tailscale Serve does). It cannot be
    unconditional: the app is commonly served over plain http on a LAN, and a
    Secure cookie would simply never come back, locking the family out. On an
    https origin, though, withholding it let the token ride a downgraded
    request. SameSite=Lax + HttpOnly apply either way.

    A `persistent` (remembered-device) session gets a long-lived cookie to
    match its server-side lifetime; a normal one stays a browser-session
    cookie, exactly as before.
    """
    token = auth.issue_session(persistent=persistent, user_agent=user_agent)
    resp = JSONResponse(body)
    max_age = auth.remember_days() * 86400 if persistent else None
    resp.set_cookie(COOKIE_NAME, token, httponly=True, samesite="lax", path="/",
                    max_age=max_age, secure=_request_is_https(request))
    return resp


@app.get("/api/auth/status")
async def auth_status(request: Request):
    cfg = auth.load()
    sess = _session_of(request)            # middleware attaches it for /api/auth
    authed = bool(sess)
    return {
        "pin_set": cfg.pin_set,
        "authenticated": authed,
        "decoy": cfg.pin_set and not authed,   # Safe Mode = PIN set, not unlocked
        "lock_timeout_seconds": cfg.lock_timeout,
        "min_pin_length": auth.MIN_PIN_LENGTH,
        # Absolute local paths — recon for an unauthenticated caller on a
        # 0.0.0.0-bound service, and only the Security panel (unlocked) shows
        # them. Empty strings keep the response shape stable for the client.
        "recovery_path": str(auth.RECOVERY_PATH) if authed else "",
        "config_path": str(auth.SECURITY_PATH) if authed else "",
        # Remembered-device ("keep this device unlocked") feature. The days
        # value is public — the unlock overlay needs it to offer the checkbox —
        # but the device COUNT is only revealed to an unlocked session.
        "remember_days": cfg.remember_days,
        "remembered": bool(sess and sess.persistent),
        "trusted_devices": auth.trusted_count() if authed else 0,
        # Which optional subsystems this build has switched on. The client used
        # to discover these by CALLING them and catching the 404, which works
        # but writes a failed request to the console on every boot of a default
        # install — where both are off. Only disclosed to a full session:
        # both surfaces are admin-only anyway, and an unauthenticated caller
        # has no use for the inventory.
        # Disclosed to anyone with FULL access, which is a live session OR any
        # caller at all when no PIN is configured (the app is open in that
        # state). Keying this on `authed` alone missed the no-PIN case, so a
        # fresh install still probed and still logged the 404 this replaced.
        # `api_bots` + `agent` are what the first-run "Connect an AI" card keys
        # off. Reported here rather than probed separately so the decision costs
        # the client nothing: a fresh install with no agent CLI and no connected
        # provider is exactly the state where the card is the right thing to
        # show, and every other state is exactly where it is not.
        "features": ({"harness": harness_available(),
                      "studioforge": studioforge_available(),
                      "api_bots": config.api_bot_count(),
                      "agent": _agent_backend_available()}
                     if (authed or not cfg.pin_set) else {}),
    }


@app.post("/api/auth/unlock")
async def auth_unlock(request: Request, payload: dict = Body(...)):
    cfg = auth.load()
    if not cfg.pin_set:
        return JSONResponse({"detail": "No PIN is set"}, status_code=400)
    wait = auth.throttle_wait()
    if wait > 0:
        secs = int(wait) + 1
        return JSONResponse(
            {"detail": f"Too many attempts — wait {secs}s.", "retry_after": secs},
            status_code=429,
        )
    if not auth.verify_pin(str(payload.get("pin") or "")):
        auth.register_failure()
        return JSONResponse({"detail": "Incorrect PIN"}, status_code=401)
    auth.register_success()
    # Remember-this-device is an opt-in checkbox AND the feature must be on.
    persistent = bool(payload.get("remember")) and cfg.remember_days > 0
    return _issue_cookie_response(
        {"ok": True, "remembered": persistent},
        persistent=persistent,
        user_agent=request.headers.get("user-agent") or "",
        request=request,
    )


@app.post("/api/auth/lock")
async def auth_lock(request: Request):
    auth.revoke(request.cookies.get(COOKIE_NAME))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp


@app.post("/api/auth/remember-config")
async def auth_remember_config(request: Request, payload: dict = Body(...)):
    """Toggle the remember-device feature (0 = off, N = sliding days).

    Requires an unlocked session once a PIN exists. Enabling only makes the
    unlock screen OFFER the checkbox — becoming remembered still takes a
    correct PIN — and disabling forgets every remembered device.
    """
    if auth.is_pin_set() and _session_of(request) is None:
        return JSONResponse({"detail": "Unlock for full access"}, status_code=403)
    try:
        days = int(payload.get("days", 0))
    except (TypeError, ValueError):
        return JSONResponse({"detail": "days must be a number"}, status_code=400)
    if not (0 <= days <= 365):
        return JSONResponse({"detail": "days must be 0–365"}, status_code=400)
    auth.set_remember_days(days)
    return {"ok": True, "remember_days": auth.remember_days()}


@app.post("/api/auth/forget-devices")
async def auth_forget_devices(request: Request):
    """Forget every remembered device. Other devices drop dead immediately;
    the calling session survives but demoted to normal idle-expiry."""
    sess = _session_of(request)
    if sess is None:
        return JSONResponse({"detail": "Unlock for full access"}, status_code=403)
    auth.forget_all_trusted(keep_session_token=sess.token)
    return {"ok": True, "trusted_devices": 0}


@app.post("/api/auth/setup")
async def auth_setup(request: Request, payload: dict = Body(...)):
    """Set, change, or remove the real PIN.

    First-time set (no PIN yet) is open — there is no session to require yet.
    Changing or removing an existing PIN requires a *real* (non-decoy) unlocked
    session AND the current PIN. (The config file is the offline recovery path.)
    """
    cfg = auth.load()
    new_pin = str(payload.get("new_pin") or "")
    current = str(payload.get("current_pin") or "")
    sess = _session_of(request)

    if cfg.pin_set:
        if sess is None:
            return JSONResponse({"detail": "Unlock with your PIN first"}, status_code=403)
        if not auth.verify_pin(current):
            return JSONResponse({"detail": "Current PIN is incorrect"}, status_code=403)
        if new_pin == "":
            auth.clear_pin()
            resp = JSONResponse({"ok": True, "pin_set": False})
            resp.delete_cookie(COOKIE_NAME, path="/")
            return resp

    if len(new_pin) < auth.MIN_PIN_LENGTH:
        return JSONResponse(
            {"detail": f"PIN must be at least {auth.MIN_PIN_LENGTH} characters"},
            status_code=400,
        )
    auth.set_pin(new_pin)
    return _issue_cookie_response({"ok": True, "pin_set": True}, request=request)


@app.post("/api/auth/recover")
async def auth_recover(request: Request, payload: dict = Body(...)):
    """Reset the lock using the recovery code from RECOVERY-CODE.txt.

    On success the PIN is removed (lock disabled) and a fresh real session is
    issued, so the user is straight in and can set a new PIN from Security.
    """
    cfg = auth.load()
    if not cfg.pin_set:
        return JSONResponse({"detail": "No PIN is set"}, status_code=400)
    wait = auth.throttle_wait()
    if wait > 0:
        secs = int(wait) + 1
        return JSONResponse(
            {"detail": f"Too many attempts — wait {secs}s.", "retry_after": secs},
            status_code=429,
        )
    if not auth.verify_recovery(str(payload.get("code") or "")):
        auth.register_failure()
        return JSONResponse({"detail": "Incorrect recovery code"}, status_code=401)
    auth.register_success()
    auth.clear_pin()
    return _issue_cookie_response({"ok": True, "pin_set": False, "recovered": True},
                                  request=request)


# --------------------------------------------------------------------------- #
# REST: "Connect an AI" (direct LLM providers)
# --------------------------------------------------------------------------- #
#
# Three admin-only routes: list the presets, probe a provider, save it as a bot.
# All of them are configuration surface — the probe reaches out to a host the
# operator typed, and the connect route writes an API key to disk — so they get
# the SAME gate the dashboard uses, re-derived from `auth` rather than trusted
# from middleware state. `/api/llm` is also in `_decoy_blocked`, which turns a
# Safe-Mode caller away a layer earlier; neither lock depends on the other.


def _require_operator(request: Request) -> None:
    """Full session, or no PIN configured at all. Anything else: 403.

    Deliberately a copy of dashboard_routes._require_operator's rule rather
    than an import: that module is standalone by design (it can be mounted on a
    bare app), and this one is the in-main version of the same sentence.
    """
    if _is_decoy(request):
        raise HTTPException(403, "Unlock for full access")
    if _session_of(request) is not None:
        return
    session = auth.get_session(request.cookies.get(COOKIE_NAME))
    if session is not None:
        auth.touch_session(session.token)
        return
    # No session is still the operator in exactly one state: the app has no
    # lock at all, in which case everything is open and this changes nothing.
    if auth.load().pin_set:
        raise HTTPException(403, "Unlock for full access")


def _llm_error(e: llm_api.ApiError) -> HTTPException:
    """A configuration mistake is a 400, not a 500 — and the detail is the
    half the operator needs, so it travels with the message."""
    return HTTPException(400, f"{e.message} — {e.detail}".strip(" —"))


@app.get("/api/llm/providers", dependencies=[Depends(_require_operator)])
async def llm_providers():
    """The preset table. Contains base URLs and env-var NAMES, never keys."""
    return {"providers": llm_api.providers_public(),
            "connected": [b.to_admin_dict() for b in config.load_bots() if b.api]}


@app.post("/api/llm/test", dependencies=[Depends(_require_operator)])
async def llm_test(payload: dict = Body(...)):
    """Probe a provider and report what it can run.

    Never raises for a provider-side failure: `{ok: false, error: "..."}` with
    a sentence the operator can act on is the product here. Only a malformed
    request (unknown provider, non-http scheme) is a 4xx.
    """
    return await llm_api.probe(
        str(payload.get("provider") or ""),
        base_url=str(payload.get("base_url") or ""),
        api_key=str(payload.get("api_key") or ""),
        model=str(payload.get("model") or ""),
    )


@app.post("/api/llm/connect", dependencies=[Depends(_require_operator)])
async def llm_connect(payload: dict = Body(...)):
    """Create-or-update a bot backed by a direct provider.

    The response echoes the bot with its key redacted to `has_key` — there is
    no route anywhere that reads an API key back out.
    """
    try:
        bot = llm_api.connect(llm_api.ConnectSpec(
            provider=str(payload.get("provider") or ""),
            model=str(payload.get("model") or ""),
            base_url=str(payload.get("base_url") or ""),
            api_key=str(payload.get("api_key") or ""),
            api_key_env=str(payload.get("api_key_env") or ""),
            name=str(payload.get("name") or ""),
            system_prompt=str(payload.get("system_prompt") or ""),
            bot_id=str(payload.get("bot_id") or ""),
        ))
    except llm_api.ApiError as e:
        raise _llm_error(e) from e
    # Every open tab is holding the roster from before this bot existed. Push
    # the new one exactly the way the Bot Manager's own save does — the WS
    # redactor filters that frame to safe bots per connection, so a Safe-Mode
    # device does not learn a non-safe bot appeared.
    await manager.broadcast(
        {"type": "bots", "bots": [b.to_dict() for b in config.load_bots()
                                  if b.visible]})
    return {"bot": bot.to_admin_dict()}


@app.get("/api/bots", responses=problem.SAFE_MODE)
async def get_bots(request: Request):
    """The roster as this client is allowed to see it.

    Session-exempt for machines so an on-box agent gets the TRUE roster (see
    _is_inbound) — but _is_safe_mode_caller, not a bare _is_decoy, decides:
    a browser tab with no full session is Safe Mode even on loopback, which is
    what this box's own idle-locked tab is.
    """
    bots = [b for b in config.load_bots() if b.visible]
    if _is_safe_mode_caller(request):
        bots = [b for b in bots if b.safe]   # Safe Mode sees only safe bots
    return {"bots": [b.to_dict() for b in bots]}


@app.get("/api/bots/all")
async def get_all_bots(request: Request):
    """All bots including hidden — used by the Bot Manager."""
    _require_full_access(request)      # second lock, see _require_full_access
    return {"bots": [b.to_dict() for b in config.load_bots()]}


@app.put("/api/bots/order")
async def put_bot_order(request: Request, payload: UpdateBotOrderIn):
    _require_full_access(request)      # second lock, see _require_full_access
    bots = config.save_bot_order([i.model_dump() for i in payload.bots])
    data = [b.to_dict() for b in bots]
    await manager.broadcast({"type": "bots", "bots": data})
    return {"bots": data}


@app.get("/api/bots/{bot_id}/avatar", responses=problem.MACHINE)
async def get_bot_avatar(request: Request, bot_id: str):
    # Safe Mode may see a SAFE bot's face by design, so mark a session-less
    # browser as decoy and then apply the per-bot rule — rather than the strict
    # agents-only guard used on /avatar/full. Without the first call the inbound
    # allowlist would let a locked tab read a non-safe bot's avatar.
    _is_safe_mode_caller(request)
    bot = config.resolve_bot(bot_id)
    if bot:
        bot_id = bot.id  # canonical — the decoy gate matches safe ids exactly
    _deny_decoy_bot(request, bot_id)
    if not bot:
        raise HTTPException(404, "Unknown bot")
    # bot.avatar comes from config.yaml (trusted, local) — contain it anyway.
    path = (config.AVATAR_DIR / bot.avatar).resolve()
    base = config.AVATAR_DIR.resolve()
    if (path == base or base in path.parents) and path.is_file():
        return FileResponse(path)
    raise HTTPException(404, "Avatar not found")


@app.get("/api/bots/{bot_id}/avatar/full")
async def get_bot_avatar_full(request: Request, bot_id: str):
    """Full-resolution avatar: `<stem>-full.<ext>` if present, else the regular one.

    Had NO gate of its own — it relied entirely on the middleware's decoy
    blocklist. Adding avatar routes to the inbound allowlist took that away and
    left a locked browser tab able to fetch any bot's full-resolution face.
    Guarded here now, where it cannot be removed by a change somewhere else.
    Full-res is PIN-gated even for safe bots, which is why this is the strict
    agents-yes/browser-no guard rather than the per-bot one.
    """
    _deny_agent_route_to_browser(request)
    bot = config.resolve_bot(bot_id)
    if not bot:
        raise HTTPException(404, "Unknown bot")
    bot_id = bot.id
    base = config.AVATAR_DIR.resolve()
    # Same resolution rule the snapshotter uses (avatar_snapshots._full_sibling):
    # the face's own extension first, then stale cross-extension leftovers by
    # newest write. Two resolvers with two precedence orders meant the snapshot
    # and this route could serve DIFFERENT files for the same avatar.
    face_path = (config.AVATAR_DIR / bot.avatar).resolve()
    if not (face_path == base or base in face_path.parents):
        raise HTTPException(404, "Avatar not found")
    sibling = avatar_snapshots._full_sibling(face_path)
    # This URL is MUTABLE — the file behind it is overwritten in place on every
    # rotation — so it must revalidate. With no explicit header, browsers apply
    # heuristic freshness and keep serving yesterday's full-res after a change.
    hdrs = {"Cache-Control": "no-cache"}
    if sibling is not None and base in sibling.resolve().parents:
        return FileResponse(sibling, headers=hdrs)
    # Fallback: the regular avatar IS the full resolution.
    if face_path.is_file():
        return FileResponse(face_path, headers=hdrs)
    raise HTTPException(404, "Avatar not found")


def _decode_avatar_image(raw: bytes):
    """Open + bomb-guard + orient one uploaded image. Shared by full and face."""
    from io import BytesIO

    from PIL import Image, ImageOps, UnidentifiedImageError

    try:
        im = Image.open(BytesIO(raw))
        # Decompression-bomb guard: check declared dimensions BEFORE decoding.
        # A tiny zlib-packed PNG can claim billions of pixels and eat all RAM.
        if im.size[0] * im.size[1] > 64_000_000:   # 64 MP is plenty for an avatar
            raise HTTPException(400, "Image dimensions too large (max 64 megapixels)")
        im.load()
        # Bake in EXIF orientation. A phone portrait carries orientation=6 and
        # stores its pixels landscape; without this the full is served sideways
        # and the face is cropped from the wrong region (the crop fractions come
        # from the browser, which HAS rotated the preview). exif_transpose
        # returns an upright copy with the tag cleared.
        im = ImageOps.exif_transpose(im)
        return im
    except HTTPException:
        raise
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise HTTPException(400, "Could not decode image")


def _avatar_pair_images(
    raw: bytes,
    crop_x: float | None, crop_y: float | None, crop_size: float | None,
    face_raw: bytes | None = None,
):
    """Build the (face, full) PIL pair for an avatar from ONE original image.

    The FULL half is always the uploaded original. The FACE half is, in order
    of preference: the separately-uploaded pre-cropped face (this is how a
    image CLI `crop_to_face` result arrives — a real detector's crop, not a
    blind square), else the crop_x/crop_y/crop_size fractional crop, else a
    centered square. The face is capped at 512×512; the full is left alone.
    """
    from PIL import Image

    im = _decode_avatar_image(raw)

    # Preserve transparency: keep an alpha channel when the source has one
    # (PNG / WebP / transparent GIF). PNG stores both RGB and RGBA, so a
    # transparent avatar renders against the UI background instead of getting a
    # solid (black) fill, which is what convert("RGB") used to do.
    has_alpha = im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info)
    target_mode = "RGBA" if has_alpha else "RGB"

    if face_raw:
        face = _decode_avatar_image(face_raw)
        fa = face.mode in ("RGBA", "LA") or (face.mode == "P" and "transparency" in face.info)
        face = face.convert("RGBA" if fa else "RGB")
        side = min(face.size)
        if face.size[0] != face.size[1]:
            # A pre-cropped face should already be square; tolerate a few px of
            # detector slack rather than reject the whole upload.
            w, h = face.size
            left, top = (w - side) // 2, (h - side) // 2
            face = face.crop((left, top, left + side, top + side))
    else:
        w, h = im.size
        if crop_x is None or crop_y is None or crop_size is None:
            side = min(w, h)
            left, top = (w - side) // 2, (h - side) // 2
        else:
            if not (0 <= crop_x <= 1 and 0 <= crop_y <= 1 and 0 < crop_size <= 1):
                raise HTTPException(400, "crop_x/crop_y/crop_size must be fractions in 0..1")
            side = max(8, int(crop_size * min(w, h)))
            left = min(int(crop_x * w), w - side)
            top = min(int(crop_y * h), h - side)
            left, top = max(0, left), max(0, top)
        face = im.crop((left, top, left + side, top + side)).convert(target_mode)

    if side > 512:
        face = face.resize((512, 512), Image.LANCZOS)
    return face, im.convert(target_mode)


def _clean_stale_avatar_siblings(stem_prefix: str, keep: set[str]) -> None:
    """Delete `<bot>-face.*` / `<bot>-full.*` variants other than the pair just
    written. A leftover `main-full.png` beside a new `main-full.jpg` would win
    the extension probe and serve the PREVIOUS avatar as this one's full
    resolution — the pair on disk must be exactly the pair that was saved."""
    for role in ("face", "full"):
        for ext in (".png", ".jpg", ".jpeg", ".webp"):
            name = f"{stem_prefix}-{role}{ext}"
            if name in keep:
                continue
            stale = config.AVATAR_DIR / name
            if stale.is_file():
                try:
                    stale.unlink()
                except OSError:
                    pass


def _process_avatar_upload(
    raw: bytes, bot_id: str,
    crop_x: float | None, crop_y: float | None, crop_size: float | None,
    face_raw: bytes | None = None,
) -> str:
    """Decode, crop, resize and save an avatar pair. Returns the face filename.

    Deliberately a plain sync function: decoding + LANCZOS-resizing a 25MB /
    64MP source takes real CPU time, so the route runs it via asyncio.to_thread
    instead of on the event loop. Raises the same HTTPExceptions the route
    always returned (they propagate cleanly out of the worker thread).
    """
    face, full = _avatar_pair_images(raw, crop_x, crop_y, crop_size, face_raw)

    config.AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    # Save full-res original (normalised to png) and the face crop atomically —
    # a thread created mid-write must never snapshot half of one avatar and
    # half of another.
    full_name = f"{bot_id}-full.png"
    face_name = f"{bot_id}-face.png"

    def _atomic_save(img, name: str) -> None:
        tmp = config.AVATAR_DIR / f".{name}.partial"
        img.save(tmp, "PNG")
        tmp.replace(config.AVATAR_DIR / name)

    _atomic_save(full, full_name)
    _atomic_save(face, face_name)
    _clean_stale_avatar_siblings(bot_id, {face_name, full_name})
    return face_name


@app.post("/api/bots/{bot_id}/avatar", responses={**problem.MACHINE, **problem.TOO_LARGE})
async def upload_bot_avatar(
    request: Request,
    bot_id: str,
    file: UploadFile = File(...),
    face: UploadFile | None = File(None),
    crop_x: float | None = None,
    crop_y: float | None = None,
    crop_size: float | None = None,
):
    """Replace a bot's avatar. Saves the FULL-RESOLUTION original plus a
    square face crop that becomes the avatar shown in the UI.

    `file` is always the full-resolution original. The face crop comes from,
    in order of preference: the optional `face` file (a real face crop — this
    is where an image CLI `crop_to_face` result belongs), else the
    crop_x/crop_y/crop_size FRACTIONS of the source image (0..1: top-left
    corner and side length of the square), else a centered square. Works from
    the browser crop UI and from curl alike.
    """
    # Session-exempt for ON-BOX AGENTS, still closed to a locked browser tab.
    #
    # _deny_decoy_mutation is wrong here now: the inbound allowlist clears
    # request.state.decoy for a loopback caller, so that check would pass for a
    # browser on this machine with no session — i.e. the locked tab, and any
    # page that can reach loopback. _deny_agent_route_to_browser is the guard
    # built for exactly this shape (agents yes, browser without a session no),
    # and it is what the session-exempt reaction routes use.
    _deny_agent_route_to_browser(request)

    bot = config.resolve_bot(bot_id)
    if not bot:
        raise HTTPException(404, "Unknown bot")
    bot_id = bot.id  # canonical — bot_id names the avatar files on disk
    ctype = (file.content_type or "").lower()
    if not ctype.startswith("image/") or ctype == "image/svg+xml":
        raise HTTPException(400, "Avatar must be a raster image")

    raw = await file.read()
    if len(raw) > UPLOAD_MAX_IMAGE:
        raise HTTPException(413, "Image too large (max 25MB)")
    face_raw = None
    if face is not None:
        fctype = (face.content_type or "").lower()
        if not fctype.startswith("image/") or fctype == "image/svg+xml":
            raise HTTPException(400, "Face crop must be a raster image")
        face_raw = await face.read()
        if len(face_raw) > UPLOAD_MAX_IMAGE:
            raise HTTPException(413, "Face crop too large (max 25MB)")
    # The PIL work (decode/crop/resize/save) is CPU+disk bound — off the loop.
    face_name = await asyncio.to_thread(
        _process_avatar_upload, raw, bot_id, crop_x, crop_y, crop_size, face_raw)

    updated = config.save_bot_avatar(bot_id, face_name)
    if not updated:
        raise HTTPException(500, "Failed to update bot config")
    data = [b.to_dict() for b in config.load_bots()]
    await manager.broadcast({"type": "bots", "bots": [b for b in data if b["visible"]]})
    await _after_avatar_change(bot_id)
    return {"ok": True, "avatar_url": updated.avatar_url,
            "full_url": f"/api/bots/{bot_id}/avatar/full"}


async def _after_avatar_change(bot_id: str) -> None:
    """Everything a NEW current avatar implies beyond the files themselves.

    1. Snapshot it now. History is otherwise only recorded when a thread is
       created, so an avatar that rotated in and out between two threads was
       unrecoverable. Capturing at change time closes that gap for free
       (content-addressed: seeing the same image twice writes nothing).
    2. Re-pin UNUSED threads. A thread wears the face it started under — but a
       thread that exists and has no messages yet hasn't "started" in any
       meaningful sense (the daily rollover pre-creates threads; so can an
       agent). Leaving those pinned to the pre-rotation face is how "today's
       chat wears yesterday's picture" happens. Once a thread has a single
       message its face is frozen and this never touches it again.
    """
    snap = await asyncio.to_thread(avatar_snapshots.snapshot_id, config.get_bot(bot_id))
    if not snap:
        return
    repinned = await db.repin_unused_thread_avatars(bot_id, snap)
    for tid in repinned:
        thread = await db.get_thread(tid)
        if thread:
            await manager.broadcast({"type": "thread_update", "thread": thread.model_dump()})


# --------------------------------------------------------------------------- #
# REST: threads + messages
# --------------------------------------------------------------------------- #


@app.get("/api/threads", responses=problem.MACHINE)
async def list_threads(request: Request, bot_id: str = Query(...), include_archived: bool = False):
    # Inbound-exempt for machines; a sessionless browser stays Safe Mode.
    _is_safe_mode_caller(request)
    bot = config.resolve_bot(bot_id)
    if bot:
        bot_id = bot.id  # canonical — SQL matches bot_id case-sensitively
    _deny_decoy_bot(request, bot_id)
    threads = await db.list_threads(bot_id, include_archived=include_archived)
    out = [t.model_dump() for t in threads]
    if _is_decoy(request):
        out = [_redact_thread_dict(t) for t in out]
    return {"bot_id": bot_id, "threads": out}


@app.post("/api/threads", responses=problem.MACHINE)
async def create_thread(request: Request, payload: dict = Body(...)):
    # Inbound-exempt for machines; a sessionless browser stays Safe Mode
    # (decoy creation keeps its daily quota below).
    _is_safe_mode_caller(request)
    bot = config.resolve_bot(payload.get("bot_id"))
    if not bot:
        raise HTTPException(400, "Unknown or missing bot_id")
    bot_id = bot.id  # canonical — a lowercased id must not fork a thread
    _deny_decoy_bot(request, bot_id)
    # A locked device may start a conversation, but under the same daily budget
    # the WS path charges — without this the REST endpoint was an unmetered way
    # for a Safe-Mode caller to create unlimited threads.
    if _is_decoy(request):
        ip = _request_quota_ip(request)
        if not _decoy_action_allowed(ip, "thread", DECOY_THREAD_QUOTA):
            raise HTTPException(429, "Daily limit reached on this device")
    title = payload.get("title")
    thread = await db.create_thread(
        bot_id=bot_id, title=title if isinstance(title, str) else None,
        avatar_from_pool=True,
    )
    await manager.broadcast({"type": "thread_created", "thread": thread.model_dump()})
    if SETTINGS.greeting:
        greeting = f"Hey! {bot.name} here {bot.emoji}. What's up?"
        await _persist_and_broadcast_message(thread.id, "assistant", greeting)
    return thread.model_dump()


@app.get("/api/threads/{thread_id}", responses=problem.MACHINE)
async def get_thread(request: Request, thread_id: str):
    _is_safe_mode_caller(request)
    thread_id = await _canonical_thread_id(thread_id)
    thread = await db.get_thread(thread_id)
    if not thread:
        raise HTTPException(404, "Thread not found")
    _deny_decoy_bot(request, thread.bot_id)
    data = thread.model_dump()
    return _redact_thread_dict(data) if _is_decoy(request) else data


@app.get("/api/bots/{bot_id}/avatar/history")
async def bot_avatar_history(request: Request, bot_id: str):
    """Past avatars for this bot, newest first.

    The rotation overwrites the avatar file in place, so before thread
    snapshots existed the previous picture was simply gone. This reads that
    history back: one entry per DISTINCT past image, with the first and last
    thread that started under it, so "the one from last Tuesday" resolves to
    something concrete.
    """
    # Mark a session-less BROWSER as Safe Mode before the bot check, or the
    # inbound exemption would let a locked tab read a non-safe bot's history.
    _is_safe_mode_caller(request)
    bot = config.resolve_bot(bot_id)
    if bot:
        bot_id = bot.id  # canonical — snapshots and the decoy gate key on it
    _deny_decoy_bot(request, bot_id)
    if not bot:
        raise HTTPException(404, "Unknown bot")
    threads = await db.all_threads(include_archived=True)
    items = avatar_snapshots.history(bot_id, threads)
    return {"bot_id": bot_id, "avatars": [
        {**e, "url": f"/api/bots/{bot_id}/avatar/history/{e['id']}"} for e in items]}


@app.get("/api/bots/{bot_id}/avatar/history/{snapshot_id}")
async def bot_avatar_history_image(request: Request, bot_id: str, snapshot_id: str,
                                   full: bool = False):
    """One past avatar. `?full=1` for the full-resolution half.

    A thumbnail and the image its lightbox opens must be the SAME picture. The
    face crop and the full-res original are captured together, so asking for
    one by the other's id always agrees. When a snapshot has no full-res half
    (older snapshots, or an avatar that never had one) this falls back to the
    face — lower resolution, still the right image.

    The snapshot must belong to THIS bot's history. The store is shared and
    content-addressed, so gating on the bot in the path alone let any snapshot
    id — including one only a non-safe bot ever wore — be fetched by naming a
    safe bot in the URL.
    """
    _is_safe_mode_caller(request)
    bot = config.resolve_bot(bot_id)
    if bot:
        bot_id = bot.id                    # canonical, as the sibling list route
    _deny_decoy_bot(request, bot_id)
    if not bot:
        raise HTTPException(404, "Unknown bot")
    threads = await db.all_threads(include_archived=True)
    if snapshot_id not in {e["id"] for e in avatar_snapshots.history(bot_id, threads)}:
        raise HTTPException(404, "No such avatar snapshot")
    full_path = avatar_snapshots.path_for_full(snapshot_id) if full else None
    if full:
        # Full resolution is PIN-gated even for safe bots, the same as
        # get_bot_avatar_full and the thread ?full=1 route. This route is on the
        # inbound allowlist, so the middleware blocklist is skipped and this
        # inline guard is the only thing withholding a safe bot's untouched
        # original from a locked device. (Without it, Safe Mode could pull
        # full-res past avatars via the history list — thumbnails only is the
        # rule, uniformly.)
        _deny_agent_route_to_browser(request)
    path = full_path or avatar_snapshots.path_for(snapshot_id)
    if not path:
        raise HTTPException(404, "No such avatar snapshot")
    if full and full_path is None:
        # Serving the FACE because this snapshot has no full half — not
        # immutable, a backfill can improve it later (matches get_thread_avatar).
        return FileResponse(path, headers={"Cache-Control": "no-cache"})
    return FileResponse(path, headers={"Cache-Control": "public, max-age=31536000, immutable"})


@app.post("/api/bots/{bot_id}/avatar/restore")
async def restore_bot_avatar(request: Request, bot_id: str, payload: dict = Body(...)):
    """Make a PAST avatar the current one again.

    Takes a snapshot id from the history endpoint. This is the "set an old one"
    half of avatar management: the rotation can move forward on its own, but
    going back needed a human with the file. It writes through the same
    save_bot_avatar path an upload uses, so the result is indistinguishable
    from having uploaded that image — including the broadcast that refreshes
    every connected device.
    """
    _deny_agent_route_to_browser(request)
    bot = config.resolve_bot(bot_id)
    if not bot:
        raise HTTPException(404, "Unknown bot")
    bot_id = bot.id  # canonical — bot_id names the restored files on disk
    sid = (payload or {}).get("snapshot_id") or ""
    src = avatar_snapshots.path_for(sid)
    if not src:
        raise HTTPException(404, "No such avatar snapshot")

    # AN AVATAR IS A PAIR: a square face crop for the UI and a full-resolution
    # original the lightbox opens. Writing only the face left the PREVIOUS
    # avatar's `-full` file in place, so the thumbnail updated and clicking it
    # showed a different picture entirely. Reported from use.
    #
    # So: write both, or write the face and REMOVE the stale full. A missing
    # full-res degrades correctly — /avatar/full falls back to the face, which
    # is merely lower resolution. A mismatched one is a lie.
    face_name = f"{bot_id}-face{src.suffix.lower()}"
    full_name = f"{bot_id}-full{src.suffix.lower()}"
    dest = config.AVATAR_DIR / face_name
    full_src = avatar_snapshots.path_for_full(sid)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)

        def _atomic(target: Path, data: bytes) -> None:
            tmp = target.with_name(f".{target.name}.partial")
            tmp.write_bytes(data)
            tmp.replace(target)

        _atomic(dest, src.read_bytes())
        keep = {face_name}
        if full_src is not None:
            _atomic(config.AVATAR_DIR / full_name, full_src.read_bytes())
            keep.add(full_name)
        # Drop every OTHER face/full variant. Restoring a .jpg snapshot used to
        # clean stale fulls only when the snapshot had no full half at all — so
        # a leftover `<bot>-full.png` outlived the restore, won the extension
        # probe, and the lightbox opened the PREVIOUS avatar.
        _clean_stale_avatar_siblings(bot_id, keep)
    except OSError:
        raise HTTPException(500, "Could not write the avatar")

    updated = config.save_bot_avatar(bot_id, face_name)
    if not updated:
        raise HTTPException(500, "Failed to update bot config")
    data = [b.to_dict() for b in config.load_bots()]
    await manager.broadcast({"type": "bots", "bots": [b for b in data if b["visible"]]})
    await _after_avatar_change(bot_id)
    return {"ok": True, "restored": sid, "avatar_url": updated.avatar_url,
            "full_restored": full_src is not None}




@app.get("/api/threads/{thread_id}/avatar", responses=problem.MACHINE)
async def get_thread_avatar(request: Request, thread_id: str, full: bool = False):
    """The bot's face as it was when this thread started. `?full=1` for the
    full-resolution half, so a lightbox opens the SAME picture as the thumbnail
    it was clicked from rather than today's avatar.

    Served BY THREAD, not by snapshot hash, and that is the security design
    rather than a convenience. Snapshots are content-addressed, so the id says
    nothing about which bot it belongs to — a hash-keyed route would have had to
    reverse-map it to decide whether a Safe-Mode session may see it, and the
    obvious "any thread references it" answer leaks a non-safe bot's face the
    moment one safe bot ever shared the same picture. Going through the thread
    means the existing _deny_decoy_bot rule applies unchanged: if you may not
    see the thread, you may not see its avatar.

    Immutable by construction (the filename IS the hash of the bytes), so it is
    safe to cache hard.
    """
    thread_id = await _canonical_thread_id(thread_id)
    thread = await db.get_thread(thread_id)
    if not thread:
        raise HTTPException(404, "Thread not found")
    _deny_decoy_bot(request, thread.bot_id)
    if full:
        # Full resolution is PIN-gated even for safe bots, same as
        # /api/bots/<id>/avatar/full — Safe Mode sees thumbnails only.
        _deny_agent_route_to_browser(request)
    full_path = avatar_snapshots.path_for_full(thread.avatar_snapshot or "") if full else None
    path = full_path or avatar_snapshots.path_for(thread.avatar_snapshot or "")
    if not path:
        # No snapshot, or the file is gone. 404 rather than falling back to the
        # live avatar: the frontend already handles a missing snapshot by
        # rendering the current one, and silently substituting a DIFFERENT
        # picture here would make the feature look broken in a way nobody could
        # explain ("why is Tuesday's chat showing today's face?").
        #
        # Those two cases are NOT the same, though, and conflating them cost
        # two days: a thread with no snapshot is ordinary, while a thread that
        # PINS one whose bytes have vanished is data loss. The second used to
        # render as an unremarkable broken thumbnail. Say so, loudly and once
        # per thread, so the store's state reaches the journal and /api/health
        # instead of only the eye of whoever happens to open that chat.
        if thread.avatar_snapshot:
            _note_missing_snapshot(thread_id, thread.avatar_snapshot)
        raise HTTPException(404, "No avatar snapshot for this thread")
    if full and full_path is None:
        # Serving the FACE because the full half is missing. This answer is not
        # immutable — a backfill can create the real full later — and caching it
        # for a year was exactly how repaired snapshots kept opening low-res.
        return FileResponse(path, headers={"Cache-Control": "no-cache"})
    return FileResponse(path, headers={"Cache-Control": "public, max-age=31536000, immutable"})


@app.post("/api/threads/{thread_id}/avatar", responses=problem.MACHINE)
async def pin_thread_avatar(
    request: Request,
    thread_id: str,
    file: UploadFile | None = File(None),
    face: UploadFile | None = File(None),
    source: str | None = None,
    crop_x: float | None = None,
    crop_y: float | None = None,
    crop_size: float | None = None,
):
    """Re-pin THIS thread's avatar, leaving the bot's current avatar alone.

    A thread normally wears the face it started under, forever. This is the
    deliberate exception: give ONE conversation its own picture — send `file`
    (the full-resolution image, with an optional pre-cropped `face` exactly
    like the bot avatar upload), or pass `?source=current` to re-pin the
    thread to the bot's avatar as it is right now.

    The pin is stored as a content-addressed face/full snapshot pair, so the
    thumbnail and its lightbox are the same picture by construction, and the
    thread's avatar URL changes with the pin (cache-busted by snapshot hash).
    """
    _deny_agent_route_to_browser(request)
    thread_id = await _canonical_thread_id(thread_id)
    thread = await db.get_thread(thread_id)
    if not thread:
        raise HTTPException(404, "Thread not found")

    if file is not None:
        ctype = (file.content_type or "").lower()
        if not ctype.startswith("image/") or ctype == "image/svg+xml":
            raise HTTPException(400, "Avatar must be a raster image")
        _refuse_oversize_part(file, "Image too large (max 25MB)")
        raw = await file.read()
        if len(raw) > UPLOAD_MAX_IMAGE:
            raise HTTPException(413, "Image too large (max 25MB)")
        face_raw = None
        if face is not None:
            fctype = (face.content_type or "").lower()
            if not fctype.startswith("image/") or fctype == "image/svg+xml":
                raise HTTPException(400, "Face crop must be a raster image")
            _refuse_oversize_part(face, "Face crop too large (max 25MB)")
            face_raw = await face.read()
            if len(face_raw) > UPLOAD_MAX_IMAGE:
                raise HTTPException(413, "Face crop too large (max 25MB)")

        def _pair_bytes() -> tuple[bytes, bytes]:
            from io import BytesIO
            face_im, full_im = _avatar_pair_images(raw, crop_x, crop_y, crop_size, face_raw)
            fb, gb = BytesIO(), BytesIO()
            face_im.save(fb, "PNG")
            full_im.save(gb, "PNG")
            return fb.getvalue(), gb.getvalue()

        face_bytes, full_bytes = await asyncio.to_thread(_pair_bytes)
        snap = avatar_snapshots.snapshot_pair(face_bytes, full_bytes)
        if not snap:
            raise HTTPException(500, "Could not store the avatar snapshot")
    elif source == "current":
        snap = await asyncio.to_thread(
            avatar_snapshots.snapshot_id, config.get_bot(thread.bot_id))
        if not snap:
            raise HTTPException(409, "The bot has no capturable avatar right now")
    else:
        raise HTTPException(400, "Send an image file, or pass source=current")

    if not await db.set_thread_avatar(thread_id, snap, explicit=True):
        raise HTTPException(500, "Could not update the thread")
    updated = await db.get_thread(thread_id)
    if updated:
        await manager.broadcast({"type": "thread_update", "thread": updated.model_dump()})
    return {"ok": True, "thread_id": thread_id, "avatar_snapshot": snap,
            "avatar_url": avatar_snapshots.url_for(thread_id, snap)}


@app.get("/api/threads/{thread_id}/messages", responses=problem.MACHINE)
async def get_messages(request: Request, thread_id: str,
                       limit: int = Query(200, ge=1, le=500),
                       before_id: str | None = None):
    _is_safe_mode_caller(request)
    thread_id = await _canonical_thread_id(thread_id)
    thread = await db.get_thread(thread_id)
    if not thread:
        raise HTTPException(404, "Thread not found")
    _deny_decoy_bot(request, thread.bot_id)
    msgs, has_more = await db.list_messages(thread_id, limit=limit, before_id=before_id)
    out = [m.model_dump() for m in msgs]
    if _is_decoy(request):
        # An image-job placeholder hides outright — see _redact_message_dict.
        out = [r for m in out if (r := _redact_message_dict(m)) is not None]
    return {
        "thread_id": thread_id,
        "messages": out,
        "has_more": has_more,
    }


@app.patch("/api/threads/{thread_id}", responses=problem.MACHINE)
async def patch_thread(request: Request, thread_id: str, payload: dict = Body(...)):
    _is_safe_mode_caller(request)
    _deny_decoy_mutation(request)
    thread_id = await _canonical_thread_id(thread_id)
    await _deny_decoy_thread(request, thread_id)
    if not await db.get_thread(thread_id):
        raise HTTPException(404, "Thread not found")
    if "title" in payload:
        title = (payload.get("title") or "").strip()
        if not title:
            raise HTTPException(400, "title required")
        await db.rename_thread(thread_id, title[:120])
    if "pinned" in payload:
        await db.pin_thread(thread_id, bool(payload["pinned"]))
    await _broadcast_thread_update(thread_id)
    return {"ok": True}


@app.post("/api/threads/{thread_id}/read", responses=problem.MACHINE)
async def mark_read(request: Request, thread_id: str):
    """Mark a thread read (clears its unread indicator on every device).

    THE MISSING `_deny_decoy_mutation` HERE IS DELIBERATE — every sibling
    route (patch, delete, archive) has one, so its absence looks like an
    oversight and has been "fixed" by review more than once.

    Safe Mode is view + send, and this endpoint is the *view* half writing
    down that the view happened: the locked tab calls it when a family member
    opens a thread on the tablet, and denying it would leave a dot that never
    clears on exactly the devices the tier exists for. The blast radius is one
    boolean on a thread the caller is already allowed to READ — Safe Mode's
    bot gate still applies through `_deny_decoy_thread` below, so a locked
    caller can no more clear a non-safe bot's dot than see it. Nothing is
    created, renamed, deleted or sent.
    """
    _is_safe_mode_caller(request)
    thread_id = await _canonical_thread_id(thread_id)
    await _deny_decoy_thread(request, thread_id)
    if not await db.get_thread(thread_id):
        raise HTTPException(404, "Thread not found")
    await db.mark_thread_read(thread_id)
    await _broadcast_thread_update(thread_id)
    return {"ok": True}


async def _abort_thread_turn(thread_id: str) -> dict:
    """Stop the run this thread is waiting on. Full-access callers only.

    Resolves the run by the same key everything else uses — the session key is
    built from the thread, and the in-flight registry is keyed by the runId the
    gateway adopted from our idempotency key. A run we cannot name is still
    abortable by session; the gateway resolves the active one.
    """
    thread = await db.get_thread(thread_id)
    if thread is None:
        raise HTTPException(404, "Thread not found")
    client = _gateway_client
    if client is None or not client.connected.is_set():
        raise HTTPException(503, "The agent gateway is not connected")
    session_key = openclaw.session_key_for(thread.bot_id, thread_id)
    run_id = next((r.run_id for r in _inflight_runs.values()
                   if r.thread_id == thread_id), None)
    try:
        await client.abort_run(session_key, run_id)
    except Exception as e:
        log.warning("abort failed for %s: %s", thread_id, e)
        raise HTTPException(502, "The gateway would not stop that turn")
    log.info("aborted turn on %s (run %s)", thread_id, run_id or "unnamed")
    return {"ok": True, "thread_id": thread_id, "run_id": run_id}


@app.post("/api/threads/{thread_id}/abort", responses=problem.MACHINE)
async def abort_turn(request: Request, thread_id: str):
    """Stop a turn in flight.

    A MUTATION, and gated like every other one: Safe Mode may view and send,
    never cancel. A locked tablet that could abort a turn could silence any
    conversation in the house.
    """
    _deny_decoy_mutation(request)
    _require_full_access(request)
    thread_id = await _canonical_thread_id(thread_id)
    return await _abort_thread_turn(thread_id)


@app.get("/api/unread", responses=problem.MACHINE)
async def unread_summary(request: Request):
    """Threads with unread bot messages, across all bots (for sidebar dots)."""
    _is_safe_mode_caller(request)
    unread = await db.unread_summary()
    if _is_decoy(request):
        safe = _safe_bot_ids()
        unread = [u for u in unread if u.get("bot_id") in safe]
    return {"unread": unread}


@app.delete("/api/messages/{message_id}", responses=problem.MACHINE)
async def delete_message_endpoint(request: Request, message_id: str):
    _is_safe_mode_caller(request)
    _deny_decoy_mutation(request)
    msg = await db.get_message(message_id)
    if not msg:
        raise HTTPException(404, "Message not found")
    await _deny_decoy_thread(request, msg.thread_id)
    thread = await db.get_thread(msg.thread_id)
    if thread and thread.status == "thinking":
        raise HTTPException(409, "Cannot delete messages while a reply is in progress")
    bot_id = await _bot_of_thread(msg.thread_id)   # resolve BEFORE deleting
    # An image-job row cascades away with its placeholder, so after this the
    # render has nothing left to rewrite — read the open ones BEFORE deleting
    # and tell the rig to stop.
    orphaned = await db.open_image_jobs_for_message(message_id)
    await db.delete_message(message_id)
    await _cancel_open_image_jobs(orphaned, "placeholder deleted")
    await manager.broadcast({
        "type": "message_deleted",
        "thread_id": msg.thread_id,
        "bot_id": bot_id,
        "message_id": message_id,
    })
    await _broadcast_thread_update(msg.thread_id)
    return {"ok": True}


# Checklist rows are authored indices (0-based) into the ```checklist table.
# `checked` is their CHECK ORDER — the first index in the list is the row that
# was completed first, so the array alone reconstructs both the checkbox state
# and the "completed rows grouped at the bottom" ordering after a reload.
# `(?:\s|$)` (not `\b`): the frontend treats only the fence whose FIRST
# whitespace-delimited token is exactly `checklist` as a checklist, so
# `checklist-title` (a different language) must not pass this gate either.
_CHECKLIST_FENCE_RE = re.compile(r"^[ \t]*(?:```|~~~)\s*checklist(?:\s|$)", re.MULTILINE)
_CHECKLIST_MAX_ROWS = 5000


@app.patch("/api/messages/{message_id}/checklist")
async def update_message_checklist(request: Request, message_id: str,
                                   payload: dict = Body(...)):
    """Persist a checklist message's checkbox state (and resulting row order).

    The frontend renders a ```checklist fenced table as an interactive widget;
    this is where its checked rows are stored, on the message itself, so the
    state survives a reload and reaches every other device via the broadcast
    below. Full-session only — Safe Mode is VIEW + SEND, so a locked device
    sees the checkboxes but cannot change them (_deny_decoy_mutation).
    """
    _is_safe_mode_caller(request)
    _deny_decoy_mutation(request)
    msg = await db.get_message(message_id)
    if not msg:
        raise HTTPException(404, "Message not found")
    await _deny_decoy_thread(request, msg.thread_id)
    if not _CHECKLIST_FENCE_RE.search(msg.content or ""):
        raise HTTPException(400, "Message is not a checklist")

    meta = dict(msg.metadata or {})
    stored = dict(meta.get("checklist") or {})
    # Which widget in the message. A message may carry more than one checklist
    # fence, and they must not share one index space -- checking row 0 of the
    # second list used to overwrite row 0 of the first. List 0 stays in
    # `checked` so anything already stored keeps working; the rest live under
    # `lists`.
    list_idx = payload.get("list", 0)
    if isinstance(list_idx, bool) or not isinstance(list_idx, int) or not 0 <= list_idx < 32:
        raise HTTPException(400, "list must be a small non-negative integer")
    lists = dict(stored.get("lists") or {})

    def _current(i: int) -> list[int]:
        return list(stored.get("checked") or []) if i == 0 else list(lists.get(str(i)) or [])

    if "index" in payload:
        # ROW OPERATION -- the server owns the merge.
        #
        # The client used to PATCH the whole array it had computed from its own
        # DOM, so two devices ticking different rows at the same time raced:
        # the second write was built from a snapshot taken before the first
        # landed, and silently discarded it. On a shared family list that is
        # data loss with no error and no indication. A row op cannot carry a
        # stale view of the other rows, because it does not mention them.
        index = payload.get("index")
        want = payload.get("checked")
        if isinstance(index, bool) or not isinstance(index, int):
            raise HTTPException(400, "index must be an integer")
        if not isinstance(want, bool):
            raise HTTPException(400, "checked must be a boolean for a row update")
        if index < 0 or index >= _CHECKLIST_MAX_ROWS:
            raise HTTPException(400, "index out of range")
        clean = [x for x in _current(list_idx) if x != index]
        if want:
            # Appended, not inserted: check ORDER is what pins completed rows
            # to the bottom in the order they were done.
            clean.append(index)
        clean = clean[-1000:]
    else:
        checked = payload.get("checked")
        if not isinstance(checked, list):
            raise HTTPException(400, "checked must be a list of row indices")
        # Bound the INPUT before iterating it. Validation alone did not: every
        # entry after the first duplicate is a dedup-skip, so the output cap
        # never trips and a 20M-element body is parsed into memory and walked
        # in full.
        if len(checked) > 5000:
            raise HTTPException(400, "too many entries")
        seen: set[int] = set()
        clean = []
        for v in checked:
            if isinstance(v, bool) or not isinstance(v, int):
                raise HTTPException(400, "checked entries must be integers")
            if v < 0 or v >= _CHECKLIST_MAX_ROWS:
                continue
            if v in seen:
                continue
            seen.add(v)
            clean.append(v)
            if len(clean) >= 1000:
                break

    if list_idx == 0:
        stored["checked"] = clean
    else:
        lists[str(list_idx)] = clean
    if lists:
        stored["lists"] = lists
    stored.setdefault("checked", [])
    meta["checklist"] = stored
    await db.update_message_metadata(message_id, meta)
    bot_id = await _bot_of_thread(msg.thread_id)   # lets the redactor scope frames
    await manager.broadcast({
        "type": "checklist_update",
        "thread_id": msg.thread_id,
        "bot_id": bot_id,
        "message_id": message_id,
        "checklist": stored,
    })
    return {"ok": True, "message_id": message_id, "checklist": stored}


@app.delete("/api/threads/{thread_id}", responses=problem.MACHINE)
async def delete_thread(request: Request, thread_id: str, hard: bool = False):
    _is_safe_mode_caller(request)
    _deny_decoy_mutation(request)
    thread_id = await _canonical_thread_id(thread_id)
    thread = await db.get_thread(thread_id)
    if not thread:
        raise HTTPException(404, "Thread not found")
    _deny_decoy_bot(request, thread.bot_id)
    if hard:
        # Same reasoning as deleting one placeholder, one level up: the rows
        # go with the messages, so read them first and withdraw the renders
        # rather than leaving the rig working on pictures with nowhere to land.
        orphaned = await db.open_image_jobs_for_thread(thread_id)
        await db.delete_thread(thread_id)
        await _cancel_open_image_jobs(orphaned, "thread deleted")
        # The lock is deliberately NOT dropped. Thread ids are not all random:
        # a daily thread's id is derived from the bot and the date, so deleting
        # today's and letting it be recreated hands the new thread a FRESH lock
        # while a turn may still be running under the old one — two concurrent
        # turns on one session, which is the exact thing this lock exists to
        # prevent. Leaving the entry costs one lock object per thread ever
        # seen, which is already the growth pattern of this defaultdict.
        _stop_follower(thread_id)
        _thread_bot.pop(thread_id, None)
        _forget_thread_delivery(thread_id)
    else:
        await db.archive_thread(thread_id)
    await manager.broadcast(
        {"type": "thread_deleted", "thread_id": thread_id,
         "bot_id": thread.bot_id, "hard": hard}
    )
    return {"ok": True}


# --------------------------------------------------------------------------- #
# REST: media (upload + validated serving of agent images)
# --------------------------------------------------------------------------- #


async def _stream_upload(
    file: UploadFile,
    dest_dir: Path,
    stored_name: str,
    *,
    max_size: int,
    request: Request | None = None,
    quota: bool = False,
) -> int:
    """Stream an UploadFile to dest_dir/stored_name atomically and safely.

    - Writes to a temp `.part`, fsyncs, then os.replace()s into place, so a crash
      mid-write never leaves a torn blob under the final (listed) name.
    - Enforces the per-file `max_size` (413), the server-wide storage cap (507),
      and — when `quota` and the request is a decoy — the daily per-client byte
      budget (429) using shared LIVE accounting with refund-on-failure (so
      concurrent decoy uploads can't each spend the full budget).
    Returns bytes written; cleans up the partial and refunds quota on any error.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / stored_name
    part = dest.with_name(dest.name + ".part")

    # Server-wide storage headroom (checked once up front; a bounded concurrent
    # overshoot is acceptable for a coarse safety cap).
    remaining_total: int | None = None
    if FILES_TOTAL_MAX > 0:
        remaining_total = FILES_TOTAL_MAX - await db.total_file_bytes()
        if remaining_total <= 0:
            raise HTTPException(507, "Storage limit reached — free space or delete files")

    is_decoy = bool(quota and request is not None
                    and _decoy_quota_left(request) is not None)
    if is_decoy and _decoy_quota_left(request) <= 0:
        raise HTTPException(429, "Daily upload limit reached — try again tomorrow or unlock")

    size = 0
    charged = 0
    published = False
    try:
        with part.open("wb") as f:
            while chunk := await file.read(1 << 20):
                n = len(chunk)
                size += n
                if size > max_size:
                    raise HTTPException(413, f"File too large (max {max_size // (1024*1024)}MB)")
                if remaining_total is not None and size > remaining_total:
                    raise HTTPException(507, "Storage limit reached — free space or delete files")
                if is_decoy:
                    _decoy_quota_add(request, n)     # charge the SHARED live counter
                    charged += n
                    if _decoy_over_quota(request):
                        raise HTTPException(429, "Daily upload limit reached — try again tomorrow or unlock")
                f.write(chunk)
            f.flush()
            os.fsync(f.fileno())
        os.replace(part, dest)          # atomic publish of the completed blob
        published = True
    except OSError:
        raise HTTPException(507, "Disk write failed")
    finally:
        # `finally`, not two `except` clauses. The clauses named HTTPException
        # and OSError only — and the two exceptions this function ACTUALLY sees
        # most are neither: Starlette raises ClientDisconnect when a phone
        # walks out of range mid-upload, and a cancelled request raises
        # CancelledError. Both left the .part file on disk forever AND left the
        # decoy's daily byte budget charged for an upload that never landed.
        if not published:
            if is_decoy and charged:
                _decoy_quota_add(request, -charged)  # refund the failed upload
            with contextlib.suppress(OSError):
                part.unlink(missing_ok=True)
    return size


@app.post("/api/upload", responses={**problem.MACHINE, **problem.TOO_LARGE})
async def upload_media(request: Request, file: UploadFile = File(...)):
    """Upload a file for chat sharing.

    Raster images and videos are stored inline for in-bubble rendering; every
    other file type (documents, archives, binaries — anything) is stored via the
    File Server and shared as a download link, so the composer "＋" accepts any
    file from the unlocked side.
    """
    ctype = (file.content_type or "").lower()
    if ctype in ("", "application/octet-stream"):
        # curl & friends send octet-stream — fall back to the file extension.
        ctype = (mimetypes.guess_type(file.filename or "")[0] or "").lower()
    is_image = ctype.startswith("image/") and ctype != "image/svg+xml"
    is_video = ctype.startswith("video/")
    suffix = Path(file.filename or "").suffix.lower()
    # Anything that isn't a renderable image/video is treated as a downloadable
    # file (the old whitelist only let through specific document extensions).
    is_doc = not is_image and not is_video

    # Files → store via File Server (so they get DB records + download URLs).
    # Safe Mode may send uploads, but on a daily per-client byte budget
    # (availability guard — see DECOY_UPLOAD_QUOTA). Full sessions: no quota.
    if is_doc:
        orig = (Path(file.filename or "file").name or "file")[:255]
        doc_suffix = Path(orig).suffix.lower()[:16]
        stored = f"{uuid.uuid4().hex}{doc_suffix}"
        size = await _stream_upload(file, FILES_DIR, stored,
                                    max_size=UPLOAD_MAX_DOC, request=request, quota=True)
        mime = ctype or (mimetypes.guess_type(orig)[0] or "")
        rec = await db.add_file(orig, stored, size, mime)
        return {"url": f"/api/files/{rec['id']}/download",
                "id": rec["id"], "name": rec["name"], "size": size,
                "kind": "document", "mime": mime}

    # Images / videos → existing behaviour.
    allowed = IMAGE_EXTS if is_image else VIDEO_EXTS
    cap = UPLOAD_MAX_IMAGE if is_image else UPLOAD_MAX_VIDEO
    if suffix not in allowed:
        guessed = mimetypes.guess_extension(ctype) or ""
        suffix = guessed if guessed in allowed else (".png" if is_image else ".mp4")
    name = f"{uuid.uuid4().hex}{suffix}"
    size = await _stream_upload(file, MEDIA_DIR, name,
                                max_size=cap, request=request, quota=True)
    return {"url": f"/media/{name}", "name": name, "size": size,
            "kind": "video" if is_video else "image"}


def _fmt_bytes(n: int) -> str:
    """Human-readable size for the drop notice (1023 B, 2.3 MB, 1.1 GB)."""
    size = float(n)
    for unit in ("B", "KB", "MB"):
        if size < 1024:
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def _drop_notice_bot() -> str | None:
    """Default bot whose daily thread receives drop notices: the first visible
    safe bot in sidebar order. None when no bot is flagged safe."""
    for b in config.load_bots():
        if b.safe and b.visible:
            return b.id
    return None


async def _resolve_drop_thread(request: Request, thread_id: str | None):
    """Pick the thread a drop notice lands in.

    A caller may name a thread (the chat open on their device). A Safe-Mode
    caller may only name a SAFE bot's thread — anything else silently falls back
    to the default, so an unauthenticated LAN peer can never write into a
    conversation it isn't allowed to see. Returns (thread, created) or None when
    no safe bot exists to receive the notice.
    """
    if thread_id:
        thread = await db.get_thread(thread_id)
        if thread and not (_is_decoy(request) and thread.bot_id not in _safe_bot_ids()):
            return thread, False
    bot_id = _drop_notice_bot()
    if not bot_id:
        return None
    return await db.find_or_create_daily_thread(bot_id, date=local_date())


@app.post("/api/drop")
async def drop_file(
    request: Request,
    file: UploadFile = File(...),
    thread_id: str | None = Form(None),
):
    """One-way file drop — the only upload path a LOCKED device gets on its own.

    Deliberately reachable in Safe Mode (it is NOT in `_decoy_blocked`): a family
    device can push a file to the box without a PIN. Everything about it is
    one-way:

    - Bytes land in the File Server store tagged `source="fileserver"`, so
      `_block_fileserver_read` bars the sender from ever pulling one back even
      if the `/api/files*` path gate were to change.
    - The notice posted into chat is TEXT ONLY (no `media_url`), and its
      `[[doc:...]]` link is stripped by `_strip_media_text` for Safe-Mode
      viewers — the sender sees that their file arrived and its name, an
      unlocked viewer gets the download link.
    - Decoy uploads spend the same daily per-client byte budget as
      `/api/upload` (`DECOY_UPLOAD_QUOTA`), so this can't be used to fill the
      disk.

    The upload is reported as successful once the bytes are safely stored; a
    failure to post the chat notice is logged and surfaced as `notice: false`
    rather than losing a file the sender was told had failed.
    """
    orig = (Path(file.filename or "file").name or "file")[:255]
    ctype = (file.content_type or "").lower()
    if ctype in ("", "application/octet-stream"):
        ctype = (mimetypes.guess_type(orig)[0] or "").lower()
    # Same per-type ceilings as /api/upload so a phone can drop a video, but
    # everything is stored as a File Server blob regardless of type — a drop is
    # never inline chat media.
    if ctype.startswith("image/") and ctype != "image/svg+xml":
        cap, kind = UPLOAD_MAX_IMAGE, "image"
    elif ctype.startswith("video/"):
        cap, kind = UPLOAD_MAX_VIDEO, "video"
    else:
        cap, kind = UPLOAD_MAX_DOC, "document"

    stored = f"{uuid.uuid4().hex}{Path(orig).suffix.lower()[:16]}"
    size = await _stream_upload(file, FILES_DIR, stored,
                               max_size=cap, request=request, quota=True)
    rec = await db.add_file(orig, stored, size, ctype or None, source="fileserver")

    # Bytes are safe from here on — a notice failure must not fail the drop.
    posted_thread: str | None = None
    try:
        resolved = await _resolve_drop_thread(request, thread_id)
        if resolved:
            thread, created = resolved
            if created:
                await manager.broadcast(
                    {"type": "thread_created", "thread": thread.model_dump()})
            origin = request.client.host if request.client else "an unknown device"
            # Line 1 survives Safe-Mode redaction (the sender sees their file
            # arrived); line 2 is the download link, which does not.
            content = (f"📎 **{orig}** · {_fmt_bytes(size)} · dropped from {origin}\n\n"
                       f"[[doc:{rec['id']}|Download {orig}]]")
            await _persist_and_broadcast_message(
                thread.id, "system", content, metadata={"kind": "drop"})
            await _broadcast_thread_update(thread.id)
            posted_thread = thread.id
    except Exception:
        log.exception("drop: file %s stored but chat notice failed", rec["id"])

    return {"id": rec["id"], "name": orig, "size": size, "kind": kind,
            "thread_id": posted_thread, "notice": posted_thread is not None}


@app.get("/api/media")
async def serve_media(request: Request, path: str = Query(...)):
    """Serve a local image/video file, but only from allow-listed base directories."""
    _require_full_access(request)      # second lock, see _require_full_access
    try:
        candidate = Path(path).resolve(strict=True)
    except (OSError, RuntimeError):
        raise HTTPException(404, "Not found")
    if candidate.suffix.lower() not in MEDIA_EXTS:
        raise HTTPException(403, "Forbidden")
    if not any(
        candidate == base or base in candidate.parents
        for base in _allowed_media_bases()
    ):
        raise HTTPException(403, "Forbidden")
    if not candidate.is_file():
        raise HTTPException(404, "Not found")
    return FileResponse(candidate)


# --------------------------------------------------------------------------- #
# REST: File Server (share any file between devices via browser)
# --------------------------------------------------------------------------- #

FILE_UPLOAD_MAX = 4 * 1024 * 1024 * 1024   # 4GB


def _safe_file_path(stored_name: str) -> Path | None:
    p = (FILES_DIR / stored_name).resolve()
    base = FILES_DIR.resolve()
    if base in p.parents and p.is_file():
        return p
    return None


@app.post("/api/files")
async def file_upload(request: Request, file: UploadFile = File(...)):
    """Upload any file type to the File Server."""
    _require_full_access(request)      # second lock, see _require_full_access
    orig = (Path(file.filename or "file").name or "file")[:255]
    suffix = Path(orig).suffix.lower()[:16]
    stored = f"{uuid.uuid4().hex}{suffix}"
    size = await _stream_upload(file, FILES_DIR, stored, max_size=FILE_UPLOAD_MAX)
    mime = (file.content_type or "").lower() or (mimetypes.guess_type(orig)[0] or "")
    rec = await db.add_file(orig, stored, size, mime, source="fileserver")
    return rec


@app.get("/api/files", responses=problem.MACHINE)
async def file_list(request: Request):
    _require_full_access(request)      # see _require_full_access: second lock
    return {"files": await db.list_files()}


# File Server uploads (source='fileserver') are full-access only: a locked
# (Safe-Mode) session can neither list nor read them — the auth gate already
# bars /api/files* for decoy sessions, and this guard is defence-in-depth so a
# decoy can never pull a blob even if that gate changes. An UNLOCKED session
# gets full view + download. On-box agents still consume them straight off disk
# (FILES_DIR). Chat attachments (source='chat') stay readable for everyone so
# posted media keeps rendering.
def _block_fileserver_read(request: Request, rec: dict) -> None:
    if _is_decoy(request) and (rec.get("source") or "chat") == "fileserver":
        raise HTTPException(403, "Unlock for full access")


@app.get("/api/files/{file_id}/download")
async def file_download(file_id: str, request: Request):
    rec = await db.get_file(file_id)
    if not rec:
        raise HTTPException(404, "File not found")
    _block_fileserver_read(request, rec)
    p = _safe_file_path(rec["stored_name"])
    if not p:
        raise HTTPException(404, "Blob missing")
    return FileResponse(p, filename=rec["name"], media_type="application/octet-stream")


@app.get("/api/files/{file_id}/raw")
async def file_raw(file_id: str, request: Request):
    """Inline serving for previews (images, videos, and text documents)."""
    rec = await db.get_file(file_id)
    if not rec:
        raise HTTPException(404, "File not found")
    _block_fileserver_read(request, rec)
    mime = rec.get("mime") or ""
    p = _safe_file_path(rec["stored_name"])
    if not p:
        raise HTTPException(404, "Blob missing")
    if mime.startswith("image/") and mime != "image/svg+xml":
        return FileResponse(p, media_type=mime)
    if mime.startswith("video/"):
        return FileResponse(p, media_type=mime)
    if mime.startswith("text/") or mime in _TEXT_MIMES:
        return FileResponse(p, media_type="text/plain; charset=utf-8")
    raise HTTPException(403, "Preview not available for this type")


@app.delete("/api/files/{file_id}")
async def file_delete(file_id: str, request: Request):
    _require_full_access(request)
    rec = await db.delete_file(file_id)
    if not rec:
        raise HTTPException(404, "File not found")
    p = _safe_file_path(rec["stored_name"])
    if p:
        p.unlink(missing_ok=True)
    return {"ok": True}


@app.post("/api/files/wipe")
async def file_wipe(request: Request, payload: dict = Body(...)):
    """Delete every file uploaded at or before `before` (ISO timestamp).

    Powers the per-day "delete this and previous files" buttons. The sharpest
    route in the app to leave on a prefix tuple alone — one call empties the
    File Server — so it re-derives the gate itself.
    """
    _require_full_access(request)
    cutoff = payload.get("before")
    if not isinstance(cutoff, str) or not cutoff:
        raise HTTPException(400, "before (ISO timestamp) required")
    # Validate as a real timestamp: the comparison downstream is lexicographic
    # against ISO strings, so an unvalidated "9" sorts after every stored row
    # and silently wipes the whole File Server.
    try:
        datetime.fromisoformat(cutoff.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(400, "before must be an ISO-8601 timestamp")
    removed = await db.delete_files_before(cutoff)
    for rec in removed:
        p = _safe_file_path(rec["stored_name"])
        if p:
            p.unlink(missing_ok=True)
    return {"ok": True, "deleted": len(removed)}


# --------------------------------------------------------------------------- #
# REST: OpenClaw inbound API (proactive messages + daily threads)
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# REST: Reaction images (ephemeral overlay pack)
# --------------------------------------------------------------------------- #


def _raise_for_reaction_error(e: reactions.ReactionError) -> None:
    raise HTTPException(e.status, e.message)


def _is_safe_mode_caller(request: Request) -> bool:
    """Safe-Mode test for the session-exempt reaction endpoints.

    Those routes skip the auth gate so on-box agents can drive them tokenlessly
    (see _INBOUND_REACTION). That exemption is for MACHINES: a browser with no
    full session is Safe Mode no matter where it connected from — which is
    exactly this box's own tab after the idle auto-lock, on loopback. Marks the
    request so anything downstream (_deny_decoy_thread, the redactor) agrees.
    """
    if _is_decoy(request):
        return True
    if (_browser_request(request) and _session_of(request) is None
            and auth.load().pin_set):
        request.state.decoy = True
        return True
    return False


def _deny_agent_route_to_browser(request: Request) -> None:
    """Full-access gate for a session-exempt route: agents yes, locked tab no."""
    if _is_safe_mode_caller(request):
        raise HTTPException(403, "Unlock for full access")


@app.get("/api/reactions", responses=problem.SAFE_MODE)
async def reactions_list(request: Request):
    """The pack as this client is allowed to see it.

    Safe Mode gets only the reactions flagged `safe` and no management
    affordances — the same shape the Bot Manager uses for safe bots.

    Session-exempt for machines (see _INBOUND_REACTION) so `dispatch-react
    --list` and on-box agents get the TRUE pack + pool + reaction_bots — but a
    browser tab with no session is Safe Mode even on loopback, same as fire.
    """
    decoy = _is_safe_mode_caller(request)
    pack = reactions.load()
    items = reactions.list_for(decoy=decoy)
    # Curation (upload / edit / delete) is full-session only — a tokenless
    # machine caller reading this list could NOT actually curate, so don't
    # claim it can. Generation IS session-exempt, so that one stays capability-
    # based. With no PIN configured the app is fully open (original behaviour).
    can_manage = (not decoy) and (_session_of(request) is not None
                                  or not auth.load().pin_set)
    return {
        "reactions": items,
        "settings": pack.settings.to_dict(),
        "categories": sorted({r["category"] for r in items}),
        "can_manage": can_manage,
        "can_generate": (not decoy) and reactions.image_cli_available(),
        # Management detail (prompts, rig errors, batch size) stays behind the PIN;
        # a locked device only needs to know whether a draw is currently possible.
        "pool": (reactions.pool_status() if not decoy
                 else {"remaining": reactions.pool_status()["remaining"]}),
        # Which bots may fire reactions — drives the picker's "off for this bot"
        # notice, and mirrors the server-side gate in fire_reaction().
        "reaction_bots": [b.id for b in config.load_bots() if b.reactions
                          and (b.safe or not decoy)],
    }


@app.get("/api/reactions/pool")
async def reaction_pool_status(request: Request, bot_id: str = Query("")):
    """One bot's pool. Omitting `bot_id` reports the DEFAULT reaction bot's —
    the status object names the bot it describes, so a caller that omitted it
    still knows what it got."""
    _deny_agent_route_to_browser(request)
    return {"pool": reactions.pool_status(bot_id)}


@app.put("/api/reactions/pool")
async def reaction_pool_config(request: Request, payload: ReactionSettingsIn):
    _deny_agent_route_to_browser(request)
    try:
        bot_id = payload.target_bot()
    except ValueError as e:
        raise HTTPException(400, str(e))
    try:
        cfg = reactions.pool_update_config(payload.values, bot_id)
    except reactions.ReactionError as e:
        _raise_for_reaction_error(e)
    except (TypeError, ValueError):
        raise HTTPException(400, "Invalid pool settings")
    _nudge_reaction_pool()
    return {"pool": {**cfg.to_dict(), **reactions.pool_status(bot_id)}}


@app.get("/api/reactions/prompts")
async def reaction_prompts_get(request: Request, bot_id: str = Query("")):
    """The prompt bank — what the pool generates. This is the file to edit to
    change how reaction images look; see the dispatch-reactions skill.

    Per bot: `bot_id` selects whose bank, and only the default reaction bot has
    one materialised for it — a companion's comes back empty until it is
    written (PUT with the same `bot_id`)."""
    _deny_agent_route_to_browser(request)
    return {"prompts": reactions.bank_load(bot_id),
            "on_hand": reactions.pool_categories(bot_id),
            "path": str(reactions.bank_path(bot_id)),
            "bot_id": reactions.resolve_bot_id(bot_id)}


@app.put("/api/reactions/prompts")
async def reaction_prompts_put(request: Request, payload: dict = Body(...)):
    """Replace the prompt bank. Takes the same shape GET returns (either the
    whole body or a bare `prompts` object). Applies to the NEXT refill —
    existing images are left alone."""
    _deny_agent_route_to_browser(request)
    body = payload.get("prompts") if isinstance(payload.get("prompts"), dict) else payload
    bot_id = str(payload.get("bot_id") or "")
    try:
        bank = reactions.bank_save(body, bot_id=bot_id)
    except reactions.ReactionError as e:
        _raise_for_reaction_error(e)
    except (TypeError, ValueError, AttributeError):
        raise HTTPException(400, "Malformed prompt bank")
    return {"prompts": bank, "categories": list(bank["categories"]),
            "bot_id": reactions.require_bot_id(bot_id)}


@app.post("/api/reactions/pool/refill")
async def reaction_pool_refill(request: Request, replace: bool = Query(False),
                               bot_id: str = Query("")):
    """Kick a refill now, or (`replace=true`) discard what's on hand and rebuild.

    At per-mood scale a fill can run for many minutes, so the work happens in a
    background task (the pool lock keeps it from racing the nightly cycle) and
    the response returns immediately; `reaction_pool` broadcasts tick the
    manager panel as images land.
    """
    _deny_agent_route_to_browser(request)
    if not reactions.image_cli_available():
        raise HTTPException(503, "Image CLI not available on this box")
    # A named-but-unknown bot must 404 rather than quietly refill (or REPLACE,
    # which discards stock) the default bot's pool.
    try:
        bot_id = reactions.require_bot_id(bot_id)
    except reactions.ReactionError as e:
        _raise_for_reaction_error(e)

    async def _run() -> None:
        async with _pool_lock:
            if replace:
                await asyncio.to_thread(reactions.pool_replace_batch, bot_id)
                await _broadcast_pool_state()
            n = await _top_up_pool(max_rounds=64, bot_id=bot_id)
            if not reactions.pool_deficits(bot_id=bot_id):
                reactions.pool_mark_daily(bot_id)
            log.info("reaction pool: manual %s for %s generated %d",
                     "replace" if replace else "refill",
                     reactions.resolve_bot_id(bot_id), n)
        await _broadcast_pool_state()

    _track(asyncio.create_task(_run(), name="reaction-pool-manual-refill"))
    return {"started": True, "pool": reactions.pool_status(bot_id)}


# --- Avatar pools: one-shot face/full pairs for new threads ----------------- #


def _raise_for_pool_error(e: avatar_pool.PoolError) -> None:
    raise HTTPException(e.status, e.message)


@app.get("/api/avatar-pool")
async def avatar_pool_status(request: Request):
    """Every pool-enabled bot's shelf. Same machine surface as the reaction
    pool (the watchdog CLI reads this); a sessionless browser is refused."""
    _deny_agent_route_to_browser(request)
    pools = {}
    for bid in avatar_pool.enabled_bots():
        with contextlib.suppress(Exception):
            pools[bid] = avatar_pool.status(bid)
    return {"pools": pools}


@app.put("/api/avatar-pool/{bot_id}")
async def avatar_pool_config(request: Request, bot_id: str, payload: dict = Body(...)):
    _deny_agent_route_to_browser(request)
    values = payload.get("values") if isinstance(payload.get("values"), dict) else payload
    try:
        cfg = avatar_pool.update_config(values, bot_id)
    except avatar_pool.PoolError as e:
        _raise_for_pool_error(e)
    except (TypeError, ValueError):
        raise HTTPException(400, "Invalid pool settings")
    _nudge_reaction_pool()
    return {"pool": {**cfg.to_dict(), **avatar_pool.status(bot_id)}}


@app.get("/api/avatar-pool/{bot_id}/prompts")
async def avatar_pool_prompts_get(request: Request, bot_id: str):
    """The avatar prompt bank — what the nightly top-up generates. Like the
    reaction bank this is deliberately DATA an on-box agent may rewrite."""
    _deny_agent_route_to_browser(request)
    try:
        return {"prompts": avatar_pool.bank_load(bot_id),
                "path": str(avatar_pool.bank_path(bot_id)),
                "bot_id": bot_id}
    except avatar_pool.PoolError as e:
        _raise_for_pool_error(e)


@app.put("/api/avatar-pool/{bot_id}/prompts")
async def avatar_pool_prompts_put(request: Request, bot_id: str, payload: dict = Body(...)):
    _deny_agent_route_to_browser(request)
    # Same check the refill route makes: a bank written for a bot with no
    # avatar pool is a file nothing will ever read, and the 200 makes a typo
    # look like it worked.
    if bot_id not in avatar_pool.enabled_bots():
        raise HTTPException(404, "No avatar pool for that bot")
    body = payload.get("prompts") if isinstance(payload.get("prompts"), dict) else payload
    try:
        bank = avatar_pool.bank_save(body, bot_id)
    except avatar_pool.PoolError as e:
        _raise_for_pool_error(e)
    except (TypeError, ValueError, AttributeError):
        raise HTTPException(400, "Malformed prompt bank")
    return {"prompts": bank, "bot_id": bot_id}


@app.post("/api/avatar-pool/{bot_id}/refill")
async def avatar_pool_refill(request: Request, bot_id: str):
    """Kick a refill now. A pair is two rig calls, so the work happens in a
    background task (under the shared pool lock) and the response returns
    immediately; `avatar_pool` broadcasts tick the panel as pairs land."""
    _deny_agent_route_to_browser(request)
    if not reactions.image_cli_available():
        raise HTTPException(503, "Image CLI not available on this box")
    if bot_id not in avatar_pool.enabled_bots():
        raise HTTPException(404, "No avatar pool for that bot")

    async def _run() -> None:
        async with _pool_lock:
            n = await _top_up_avatar_pool(max_rounds=64, bot_id=bot_id)
            if not avatar_pool.deficit(bot_id):
                avatar_pool.mark_daily(bot_id)
            log.info("avatar pool: manual refill for %s generated %d", bot_id, n)
        await _broadcast_avatar_pool_state()

    _track(asyncio.create_task(_run(), name="avatar-pool-manual-refill"))
    return {"started": True, "pool": avatar_pool.status(bot_id)}


@app.post("/api/reactions/fire", responses={**problem.MACHINE, **problem.RATE_LIMITED})
async def reactions_fire(request: Request, payload: FireReactionIn):
    """Pop a reaction on every connected device.

    Reachable by OpenClaw over loopback and by a remote caller with the API
    key — the same inbound rules as /api/inject. Browsers have no fire
    control: reactions belong to the bots (the composer button was removed
    for good on 2026-08-01), so the only in-app caller left is the Reaction
    Manager's Test button, which fires as the enabled reaction bot.
    """
    # A locked device gets no fire path at all — not even safe cards.
    if _is_safe_mode_caller(request):
        raise HTTPException(403, "Unlock for full access")

    # A bad thread id must refuse loudly. The trace persist downstream
    # swallows its own failures (a fire mustn't die halfway), so without this
    # check a typo'd thread returned ok:true while the trace silently
    # FK-failed — an agent's fire vanished with a success receipt (seen in
    # the journal 2026-08-01, documented operator confusion).
    # Case-insensitive for the same reason the write paths are: the gateway
    # hands agents lowercased ids, and a mistyped-case fire that 404s here
    # reads to a small model as "bad reaction id" — it then cycles reaction
    # names instead of fixing the thread. Resolve, then use the CANONICAL id
    # everywhere downstream so the trace row's FK matches the real thread.
    if payload.thread_id:
        canonical_fire_tid = await db.resolve_thread_id(payload.thread_id)
        if canonical_fire_tid is None:
            raise HTTPException(404, "Unknown thread")
        payload.thread_id = canonical_fire_tid

    # Every fire must claim agent kind. The claim is caller-supplied, so it
    # is not trusted on its own: fire_reaction authenticates it against the
    # roster and refuses unless it resolves to a reactions-enabled bot.
    kind = payload.actor_kind or ("agent" if payload.bot_id else "user")
    if kind != "agent":
        raise HTTPException(403, "Only agents may fire reactions")

    bot = config.resolve_bot(payload.bot_id) if payload.bot_id else None
    actor = (payload.actor or "").strip()[:60]
    if not actor and payload.bot_id:
        actor = bot.name if bot else payload.bot_id

    try:
        event = await fire_reaction(
            payload.reaction, actor=actor, actor_kind=kind,
            thread_id=payload.thread_id,
            bot_id=bot.id if bot else payload.bot_id,
            duration_ms=payload.duration_ms, caption=payload.caption,
            # Safe-Mode callers were already refused above; who *sees* the
            # overlay is decided per connection by the WS frame filter.
            trace=payload.trace, require_safe=False,
        )
    except reactions.ReactionError as e:
        _raise_for_reaction_error(e)
    return {"ok": True, "event": event}


@app.post("/api/image-jobs", status_code=202,
          responses={**problem.MACHINE, **problem.RATE_LIMITED})
async def image_job_create(request: Request, payload: ImageJobIn):
    """Ask for a picture. Returns in milliseconds; the picture arrives later.

    The gate is the reaction fire's gate, for the same reasons: a locked device
    gets no path at all, an unknown thread refuses loudly rather than
    succeeding into nowhere, and the bot must have the capability switched on.
    The one addition is that the bot has to be the thread's OWN bot — the
    placeholder is persisted as an assistant message and will be attributed to
    whoever owns the thread regardless, so accepting a mismatch would let one
    bot put a picture in another's conversation under that bot's name.
    """
    if not _image_jobs_configured():
        raise HTTPException(503, "Image generation isn't configured")
    # A locked device has no fire path at all — same rule as reactions.
    if _is_safe_mode_caller(request):
        raise HTTPException(403, "Unlock for full access")

    canonical = await db.resolve_thread_id(payload.thread_id)
    if canonical is None:
        raise HTTPException(404, "Unknown thread")

    # Explicit, not the model's own default: this route always has a
    # placeholder sitting in a thread with somebody looking at it — the
    # genuinely user-initiated case the rig contract reserves tier 1 for —
    # so an OMITTED priority means interactive here, even though
    # `ImageJobIn.priority` itself now defaults to background (3) per the
    # contract's general rule for every other, unspecified caller.
    # `model_fields_set` is what tells "the caller said 3" apart from
    # "the caller said nothing and pydantic filled in 3".
    priority = (payload.priority if "priority" in payload.model_fields_set
               else 1)

    bot = config.resolve_bot(payload.bot_id)
    if bot is None:
        raise HTTPException(404, f"Unknown bot: {payload.bot_id}")
    thread_bot = await _bot_of_thread(canonical)
    if not thread_bot or thread_bot.lower() != bot.id.lower():
        raise HTTPException(403, "That thread belongs to another bot")
    if not bot.image_jobs:
        raise HTTPException(403, f"Image requests aren't enabled for {bot.name}")

    # Same coalesce rule the inline marker takes: a fast burst of explicit
    # requests replaces a still-QUEUED render of this bot's own rather than
    # piling placeholders up behind each other, and every slot freed that way
    # is handed straight back to the rate limiter below.
    freed = await _supersede_stale_pic_jobs(bot.id, canonical)
    for _ in range(freed):
        image_jobs.limiter.refund(bot.id.lower())

    try:
        # Same server-side identity injection the inline marker gets: a bot
        # configured with `image_identity_source` has its canonical prompt
        # prepended here too, so this explicit route cannot be used to bypass
        # it and post an off-model picture under that bot's name.
        full_prompt, display_caption = _compose_pic_prompt(
            bot, payload.prompt, payload.caption or "")
        spec = image_jobs.ImageSpec(
            # A request that names no workflow gets the bot's own default, if
            # it has one — the same rule the inline marker path uses, since a
            # marker has no room to name one at all.
            prompt=full_prompt,
            workflow=payload.workflow or bot.image_workflow,
            ratio=payload.ratio or bot.image_ratio, width=payload.width,
            height=payload.height, negative=payload.negative or "",
            caption=display_caption, priority=priority)
    except (ValueError, image_jobs.ImageJobError) as e:
        raise HTTPException(422, str(e))

    err = image_jobs.limiter.check(bot.id.lower())
    if err:
        raise HTTPException(429, err)

    started = await _start_image_job(canonical, bot, spec)
    if started is None:
        raise HTTPException(500, "Could not start the image request")
    job_id, message_id = started
    return {"job_id": job_id, "message_id": message_id,
            "thread_id": canonical, "state": image_jobs.QUEUED}


@app.get("/api/image-jobs/{job_id}", responses=problem.MACHINE)
async def image_job_status(request: Request, job_id: str):
    """One job's state. Machine-facing, same gate as creating one."""
    if _is_safe_mode_caller(request):
        raise HTTPException(403, "Unlock for full access")
    job = await db.get_image_job(job_id)
    if job is None:
        raise HTTPException(404, "Unknown image job")
    spec = _image_job_spec(job)
    # The callback token is deliberately absent: this route is readable by any
    # on-box agent, and handing one of them the credential that advances a job
    # would make the callback's own gate pointless.
    return {"job_id": job["id"], "state": job["state"],
            "message_id": job["message_id"], "thread_id": job["thread_id"],
            "bot_id": job["bot_id"], "prompt": spec.prompt,
            "workflow": spec.workflow, "priority": spec.priority,
            "media_url": job["media_url"], "seed": job.get("seed"),
            "progress": _image_job_progress(job),
            "error": job["error"], "created_at": job["created_at"],
            "updated_at": job["updated_at"]}


@app.post("/api/image-jobs/{job_id}/callback", responses=problem.MACHINE)
async def image_job_callback(request: Request, job_id: str):
    """The rig saying "this one is done". A poke, never content.

    The BODY IS IGNORED. The rig posts what `get_job` would have returned, and
    trusting it would make a forged POST able to write a picture URL, an error
    string or a state into a family thread. Instead this advances the job
    through exactly the path the sweep uses, which re-polls the rig itself — so
    the worst a forged callback can do is cost one extra `get_job`.

    Its credential is the per-job token minted at submit time and given only to
    the image server. That is why this route is exempt from the api_token check
    the rest of the machine surface uses (see the auth middleware): the caller
    is a LAN peer, holding a capability scoped to one job, that expires with
    the job. A browser never reaches here — a locked tab gets Safe Mode's 403.

    The sweep is unchanged and still runs on its own cadence. A callback only
    makes a terminal transition arrive sooner; a callback that never comes is
    the case the sweep has always covered.
    """
    _deny_agent_route_to_browser(request)
    job = await db.get_image_job(job_id)
    if job is None:
        raise HTTPException(404, "Unknown image job")
    presented = request.headers.get("x-clawforge-token") or ""
    expected = job.get("callback_token") or ""
    # `compare_digest` on both halves, and an empty expected token can never
    # match: a job created before callbacks existed is not advanceable by
    # anyone who guesses "no token".
    if not expected or not secrets.compare_digest(presented, expected):
        raise HTTPException(403, "Invalid callback token")
    if job["state"] not in image_jobs.OPEN_STATES:
        # A late or duplicate delivery for a job that already ended. Not an
        # error — the rig retries, and this is what a retry after success
        # looks like.
        return {"ok": True, "state": job["state"]}
    await _advance_image_job(job)
    fresh = await db.get_image_job(job_id)
    if fresh and fresh["state"] in image_jobs.OPEN_STATES:
        # The sweep held the row: let it look again straight away rather than
        # at the next tick, so the poke still buys the reader its seconds.
        _wake_image_jobs()
    return {"ok": True, "state": (fresh or job)["state"]}


@app.get("/api/reactions/{rid}/image")
async def reaction_image(rid: str, request: Request):
    # Display resolver, not the fire resolver: a pool image is consumed BEFORE
    # the overlay is broadcast, so every client's fetch arrives after it has
    # moved to spent/. It must still serve for the grace window.
    r = reactions.get_for_display(rid)
    if r is None:
        raise HTTPException(404, "Not found")
    if _is_decoy(request) and not r.safe:
        raise HTTPException(403, "Unlock for full access")
    try:
        path = reactions.image_path(r)
    except reactions.ReactionError as e:
        _raise_for_reaction_error(e)
    # Blobs are content-addressed by a uuid suffix, so a reaction's image URL
    # only changes when the reaction does — BUT hand-edited registries (e.g. a
    # replaced pack card keeping the same id) reuse the URL with new bytes, so
    # hard caching serves stale images for the max-age. Revalidate instead:
    # FileResponse supplies ETag/Last-Modified, so no-cache costs one cheap 304.
    return FileResponse(path, headers={"Cache-Control": "no-cache"})


@app.post("/api/reactions")
async def reaction_upload(
    request: Request,
    file: UploadFile = File(...),
    name: str = Form(""),
    aliases: str = Form(""),
    category: str = Form("general"),
    safe: bool = Form(False),
    duration_ms: int = Form(0),
):
    """Add an image to the pack. `aliases` is comma/space separated."""
    _deny_decoy_mutation(request)
    data = await file.read(reactions.UPLOAD_MAX + 1)
    if len(data) > reactions.UPLOAD_MAX:
        raise HTTPException(
            413, f"Image too large (max {reactions.UPLOAD_MAX // (1024 * 1024)}MB)")
    if not data:
        raise HTTPException(400, "Empty file")

    ctype = (file.content_type or "").lower()
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in reactions.IMAGE_EXTS:
        suffix = mimetypes.guess_extension(ctype) or ""
    if suffix == ".jpe":                      # mimetypes' unhelpful jpeg guess
        suffix = ".jpg"
    if suffix not in reactions.IMAGE_EXTS:
        raise HTTPException(415, "Reaction images must be PNG, JPEG, GIF, WebP or AVIF")

    # Decode-verify rather than trusting the extension: a mislabeled file that
    # isn't really an image has no business in the pack.
    try:
        from PIL import Image
        Image.open(io.BytesIO(data)).verify()
    except Exception:
        raise HTTPException(415, "That file isn't a readable image")

    alias_list = [a for a in re.split(r"[,\s]+", aliases or "") if a]
    try:
        r = reactions.add(name=name or Path(file.filename or "reaction").stem,
                          image_bytes=data, suffix=suffix, aliases=alias_list,
                          category=category, safe=safe,
                          duration_ms=duration_ms or None, source="upload")
    except reactions.ReactionError as e:
        _raise_for_reaction_error(e)
    return {"reaction": r.to_dict(settings=reactions.load().settings)}


@app.post("/api/reactions/generate")
async def reaction_generate(request: Request, payload: GenerateReactionIn):
    """Mint a new reaction image on the configured image host via the image CLI."""
    _deny_agent_route_to_browser(request)
    try:
        # Blocking subprocess with a multi-minute ceiling (cold model load) —
        # off the event loop, or every other client stalls behind it.
        r = await asyncio.to_thread(
            reactions.generate, payload.prompt,
            name=payload.name or "", style=payload.style or "",
            workflow=payload.workflow or "", safe=payload.safe,
        )
    except reactions.ReactionError as e:
        _raise_for_reaction_error(e)
    return {"reaction": r.to_dict(settings=reactions.load().settings)}


@app.patch("/api/reactions/{rid}")
async def reaction_patch(rid: str, request: Request, payload: ReactionPatchIn):
    _deny_decoy_mutation(request)
    try:
        r = reactions.update(rid, **payload.model_dump(exclude_unset=True))
    except reactions.ReactionError as e:
        _raise_for_reaction_error(e)
    return {"reaction": r.to_dict(settings=reactions.load().settings)}


@app.delete("/api/reactions/{rid}")
async def reaction_delete(rid: str, request: Request):
    _deny_decoy_mutation(request)
    try:
        reactions.remove(rid)
    except reactions.ReactionError as e:
        _raise_for_reaction_error(e)
    return {"ok": True}


@app.put("/api/reactions/settings")
async def reaction_settings(request: Request, payload: ReactionSettingsIn):
    _deny_agent_route_to_browser(request)
    try:
        st = reactions.update_settings(payload.values)
    except (reactions.ReactionError, TypeError, ValueError) as e:
        if isinstance(e, reactions.ReactionError):
            _raise_for_reaction_error(e)
        raise HTTPException(400, "Invalid settings")
    return {"settings": st.to_dict()}


@app.post("/api/reactions/reseed")
async def reaction_reseed(request: Request):
    """Re-render the built-in starter cards (uploads are left untouched)."""
    _deny_decoy_mutation(request)
    written = await asyncio.to_thread(reactions.seed_starter_pack, True)
    return {"ok": True, "written": written,
            "reactions": reactions.list_for(decoy=False)}


@app.post("/api/inject", responses=problem.MACHINE)
async def inject_message(request: Request, payload: InjectIn):
    """Push a message into the chat from OpenClaw (proactive / scheduled).

    Used by OpenClaw cron/timers, e.g. a morning briefing into a daily thread.
    Resolves the target thread, persists, and broadcasts to all clients.

    Session-exempt for MACHINES (see _is_inbound) — but a browser tab with no
    session is Safe Mode even on loopback, same as the reaction routes: this
    box's own idle-locked tab must not be able to write into any thread (or
    fire `:react:` markers) via the machine-to-machine bypass.
    """
    _deny_agent_route_to_browser(request)
    thread: ThreadOut | None = None
    created = False
    if payload.thread_id:
        # Case-insensitive: the gateway lowercases whole session keys, so an
        # agent or cron echoing a lowercased id (`daily-scout-…`) back into
        # /api/inject would 404 against the mixed-case row (`daily-Scout-…`).
        # The gateway WS route already resolves this way; the inbound REST
        # paths must too.
        canonical = await db.resolve_thread_id(payload.thread_id)
        thread = await db.get_thread(canonical) if canonical else None
        if not thread:
            raise HTTPException(404, "thread_id not found")
    elif payload.bot_id:
        # Case-forgiving for the same reason thread_id is: agents echo the
        # gateway's lowercased ids. The CANONICAL id must be what creates the
        # thread, or `daily-scout-…` forks off `daily-Scout-…`.
        bot = config.resolve_bot(payload.bot_id)
        if not bot:
            raise HTTPException(400, "Unknown bot_id")
        thread, created = await db.find_or_create_daily_thread(
            bot.id, date=payload.date, title=payload.title
        )
    else:
        raise HTTPException(400, "Provide thread_id or bot_id")

    if created:
        await manager.broadcast({"type": "thread_created", "thread": thread.model_dump()})

    msg = await _persist_and_broadcast_message(
        thread.id, payload.role, payload.content,
        media_url=_normalize_media(payload.media_url),
        # Stamp the ROUTE, not the payload. This is what lets the reaction (and
        # any future image) autopilot tell a machine post from a bot's own
        # conversational reply -- see the origin gate in _prepare_persist. A
        # caller cannot spoof its way out of it by omitting metadata, and a
        # caller that sets its own `origin` is overridden here on purpose.
        metadata={**(payload.metadata or {}), "origin": "inject"},
    )
    await _broadcast_thread_update(thread.id)
    return {"thread_id": thread.id, "created": created, "message": msg.model_dump()}


@app.post("/api/daily", responses=problem.MACHINE)
async def ensure_daily_thread(request: Request, payload: DailyThreadIn):
    """Find-or-create today's (or a given date's) daily thread for a bot.

    Session-exempt for machines; a sessionless browser is refused (see
    inject_message)."""
    _deny_agent_route_to_browser(request)
    bot = config.resolve_bot(payload.bot_id)
    if not bot:
        raise HTTPException(400, "Unknown bot_id")
    thread, created = await db.find_or_create_daily_thread(
        bot.id, date=payload.date or local_date(), title=payload.title
    )
    if created:
        await manager.broadcast({"type": "thread_created", "thread": thread.model_dump()})
        # Housekeeping that needed a daily hook: drop snapshot files no thread
        # references any more (the daily cleanup deletes empty threads straight
        # in SQLite, so orphans accumulate silently otherwise). Keyed off the
        # first creation of the day; a no-op every other call.
        # Keep a strong reference: the loop only holds the task weakly, so a
        # bare create_task() can be collected mid-flight and never finish.
        task = asyncio.create_task(_prune_avatar_snapshots())
        _housekeeping_tasks.add(task)
        task.add_done_callback(_housekeeping_tasks.discard)
    return {"thread": thread.model_dump(), "created": created}


# Threads whose pinned snapshot has no bytes on disk: {thread_id: snapshot_id}.
# Populated by the boot audit and by any serve that finds the blob gone.
_missing_snapshots: dict[str, str] = {}


def _note_missing_snapshot(thread_id: str, sid: str) -> None:
    """Record (and log once) a thread pinned to a snapshot that isn't there."""
    if _missing_snapshots.get(thread_id) == sid:
        return                                   # already known; don't spam
    _missing_snapshots[thread_id] = sid
    log.warning("avatar snapshot missing for thread %s: %s is not in the store",
                thread_id, sid)


async def _audit_avatar_snapshots() -> None:
    """At boot, compare what the threads pin against what is on disk.

    The store is content-addressed and written once, so a referenced file
    disappearing is always a fault -- but nothing looked, so the first symptom
    was a family member noticing a blank thumbnail. Checking at startup costs
    one directory read and turns silent loss into a startup warning.
    """
    try:
        threads = await db.all_threads(include_archived=True)
        gone = await asyncio.to_thread(avatar_snapshots.missing_blobs, threads)
        _missing_snapshots.clear()
        for tid, sid in gone:
            _missing_snapshots[tid] = sid
        if gone:
            log.warning(
                "%d thread(s) reference an avatar snapshot that is not on disk "
                "(%d distinct image(s)); thumbnails will fall back to the live "
                "avatar. First few: %s",
                len(gone), len({s for _, s in gone}), gone[:5])
        else:
            log.info("avatar snapshot store is complete (%d thread(s) checked)",
                     sum(1 for t in threads if getattr(t, "avatar_snapshot", None)))
    except Exception:
        log.warning("avatar snapshot audit failed", exc_info=True)


_last_snapshot_prune: str | None = None


_housekeeping_tasks: set[asyncio.Task] = set()


async def _prune_avatar_snapshots() -> None:
    global _last_snapshot_prune
    today = local_date()
    if _last_snapshot_prune == today:
        return
    _last_snapshot_prune = today
    try:
        threads = await db.all_threads(include_archived=True)
        keep: set[str] = set()
        for t in threads:
            sid = getattr(t, "avatar_snapshot", None)
            if sid:
                keep.add(sid)
                keep.add(avatar_snapshots._full_name(sid))
        # The current avatars' snapshots survive too — captured at change time,
        # they may not be referenced by any thread yet.
        for bot in config.load_bots():
            sid = await asyncio.to_thread(avatar_snapshots.snapshot_id, bot)
            if sid:
                keep.add(sid)
                keep.add(avatar_snapshots._full_name(sid))
        removed = await asyncio.to_thread(avatar_snapshots.prune, keep)
        if removed:
            log.info("pruned %d orphaned avatar snapshot(s)", removed)
    except Exception:                                    # never break the daily path
        log.warning("avatar snapshot prune failed", exc_info=True)


@app.post("/api/threads/{thread_id}/messages", responses=problem.MACHINE)
async def post_message_rest(request: Request, thread_id: str, payload: dict = Body(...)):
    """REST alias for injecting a single message into a known thread.

    Session-exempt for machines; a sessionless browser is refused (see
    inject_message). The frontend never POSTs here — it sends over WS."""
    _deny_agent_route_to_browser(request)
    # Case-insensitive, same reason as /api/inject: a lowercased session-key id
    # must still hit its mixed-case thread row.
    canonical = await db.resolve_thread_id(thread_id)
    if not canonical:
        raise HTTPException(404, "Thread not found")
    thread_id = canonical
    role = payload.get("role", "assistant")
    if role not in ("assistant", "user", "system"):
        role = "assistant"
    # `text` accepted as an alias for the same reason InjectIn takes it: with a
    # silent "" default the caller's mistake persisted an empty bubble.
    #
    # Oversize REFUSES rather than truncating. This used to slice to 65536 and
    # return 200, so an agent posting a long report got a success receipt for a
    # message the reader saw cut off mid-sentence — a failure that reports
    # success, and the same operation via /api/inject already 422s on
    # InjectIn's max_length. One operation, one behaviour, and the loud one.
    content = str(payload.get("content") or payload.get("text") or "")
    if len(content) > MESSAGE_MAX_CHARS:
        raise HTTPException(
            422,
            f"content is too long ({len(content)} chars; max {MESSAGE_MAX_CHARS}) — "
            "split it across messages rather than letting it be cut")
    media_url = _normalize_media(payload.get("media_url"))
    if not content.strip() and not media_url:
        raise HTTPException(
            422, "content is required — the message text field is `content`")
    msg = await _persist_and_broadcast_message(
        thread_id, role, content,
        media_url=media_url,
        metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else None,
    )
    await _broadcast_thread_update(thread_id)
    return msg.model_dump()


# --------------------------------------------------------------------------- #
# REST: search · export · transcript bridge · disaster recovery
# (full-session only — _decoy_blocked bars these for Safe-Mode connections)
# --------------------------------------------------------------------------- #


@app.get("/api/search")
async def search_messages(request: Request, q: str = Query(...),
                          bot_id: str | None = None, limit: int = 60):
    """Full-text search across every thread's messages (FTS5, LIKE fallback)."""
    _require_full_access(request)      # reads every thread, Safe-Mode-unredacted
    bot_ids = [bot_id] if bot_id else None
    results = await db.search_messages(q, bot_ids=bot_ids, limit=limit)
    return {"query": q, "results": results, "fts": db.fts_ok}


def _export_markdown(threads: list[dict]) -> str:
    lines = [f"# DisPatch Chat export — {datetime.now().isoformat(timespec='seconds')}", ""]
    bots = {b.id: b for b in config.load_bots()}
    for t in threads:
        bot = bots.get(t["bot_id"])
        bname = bot.name if bot else t["bot_id"]
        lines.append(f"\n## {t.get('title') or 'Untitled'}  ·  {bname}")
        lines.append(f"*thread `{t['id']}` · created {t['created_at']}*\n")
        for m in t["messages"]:
            who = "You" if m["role"] == "user" else (bname if m["role"] == "assistant" else "System")
            lines.append(f"**{who}** · {m['created_at']}")
            lines.append("")
            lines.append(m.get("content") or "")
            if m.get("media_url"):
                lines.append(f"\n[media] {m['media_url']}")
            lines.append("")
    return "\n".join(lines)


def _export_html(threads: list[dict]) -> str:
    import html as _html
    bots = {b.id: b for b in config.load_bots()}
    parts = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        "<title>DisPatch Chat export</title><style>",
        "body{font:15px/1.6 -apple-system,Segoe UI,sans-serif;max-width:820px;margin:24px auto;"
        "padding:0 16px;background:#0f0f1a;color:#e8e8f0}h1,h2{font-weight:700}"
        ".t{margin:32px 0 8px;border-bottom:1px solid #2a2a45;padding-bottom:6px}"
        ".m{margin:10px 0;padding:10px 12px;border:1px solid #2a2a45;border-radius:12px;background:#16162a}"
        ".m.user{background:#241a3a}.who{font-weight:700;font-size:13px;color:#9a9ab8}"
        ".tm{font-size:11px;color:#7a7a96}.c{white-space:pre-wrap;margin-top:4px}"
        "a{color:#a78bfa}</style></head><body>",
        f"<h1>DisPatch Chat export</h1><p class='tm'>{datetime.now().isoformat(timespec='seconds')}</p>",
    ]
    for t in threads:
        bot = bots.get(t["bot_id"])
        bname = _html.escape(bot.name if bot else t["bot_id"])
        parts.append(f"<h2 class='t'>{_html.escape(t.get('title') or 'Untitled')} · {bname}</h2>")
        for m in t["messages"]:
            who = "You" if m["role"] == "user" else (bname if m["role"] == "assistant" else "System")
            parts.append(
                f"<div class='m {m['role']}'><div class='who'>{who} "
                f"<span class='tm'>· {m['created_at']}</span></div>"
                f"<div class='c'>{_html.escape(m.get('content') or '')}</div></div>"
            )
    parts.append("</body></html>")
    return "".join(parts)


@app.get("/api/export")
async def export_all(
    request: Request, format: str = Query("json"),
    bot_id: str | None = None, thread_id: str | None = None,
):
    """Export all messages (or one bot / one thread) as JSON, Markdown, or HTML.

    The user's 'retrieve ALL messages' guarantee: a single action that dumps the
    full conversation history into an open, human-readable + re-importable file.
    """
    _require_full_access(request)      # dumps EVERY thread, unredacted
    threads = await db.all_threads(include_archived=True)
    if thread_id:
        threads = [t for t in threads if t.id == thread_id]
    elif bot_id:
        threads = [t for t in threads if t.bot_id == bot_id]
    bundle = []
    for t in threads:
        msgs = await db.dump_messages(t.id)
        td = t.model_dump()
        td["messages"] = [m.model_dump() for m in msgs]
        bundle.append(td)

    fmt = (format or "json").lower()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    if fmt == "md" or fmt == "markdown":
        body = _export_markdown(bundle)
        return _download_text(body, f"dispatch-export-{stamp}.md", "text/markdown")
    if fmt == "html":
        body = _export_html(bundle)
        return _download_text(body, f"dispatch-export-{stamp}.html", "text/html")
    payload = {"exported_at": datetime.now().isoformat(), "threads": bundle}
    body = json.dumps(payload, indent=2, ensure_ascii=False, default=str)
    return _download_text(body, f"dispatch-export-{stamp}.json", "application/json")


def _download_text(body: str, filename: str, media_type: str):
    from fastapi.responses import Response
    return Response(
        content=body, media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/recover/transcript")
async def recover_transcript(request: Request, payload: dict = Body(...)):
    """Pull any messages missing from a thread out of its OpenClaw transcript.

    Idempotent (routes through the shared dedup funnel): re-running only fills
    gaps. `thread_id` recovers one thread; `all: true` sweeps every thread.
    """
    _require_full_access(request)
    if payload.get("all"):
        total, scanned = 0, 0
        for t in await db.all_threads(include_archived=True):
            scanned += 1
            with contextlib.suppress(Exception):
                total += await _import_transcript_messages(
                    t.id, t.bot_id, mark_followup=True)
        return {"ok": True, "threads_scanned": scanned, "recovered": total}
    thread_id = payload.get("thread_id")
    if not thread_id:
        raise HTTPException(400, "thread_id (or all:true) required")
    t = await db.get_thread(thread_id)
    if not t:
        raise HTTPException(404, "Thread not found")
    n = await _import_transcript_messages(thread_id, t.bot_id, mark_followup=True)
    if n:
        await _broadcast_thread_update(thread_id)
    return {"ok": True, "thread_id": thread_id, "recovered": n}


@app.get("/api/openclaw/sessions")
async def openclaw_sessions(request: Request, bot_id: str = Query(...)):
    """Every OpenClaw session for an agent — including cron/main/subagent/dashboard
    sessions DisPatch never created. The doorway to messages that would otherwise
    never reach DisPatch."""
    _require_full_access(request)
    bot = config.resolve_bot(bot_id)
    if not bot:
        raise HTTPException(400, "Unknown bot_id")
    bot_id = bot.id
    sessions = openclaw.list_agent_sessions(bot_id)
    # Flag which sessions correspond to an existing DisPatch thread.
    for s in sessions:
        key = s["session_key"]
        tid = key.split(":", 2)[2] if key.count(":") >= 2 else None
        # The gateway lowercases session keys; resolve case-insensitively or
        # mixed-case bots' daily threads all show as "not in DisPatch" and
        # invite a duplicate import.
        real_tid = await db.resolve_thread_id(tid) if tid else None
        s["thread_id"] = real_tid or tid
        s["in_dispatch"] = bool(real_tid)
    return {"bot_id": bot_id, "sessions": sessions}


@app.get("/api/openclaw/transcript")
async def openclaw_transcript(
    request: Request, bot_id: str = Query(...), thread_id: str | None = None,
    session_key: str | None = None,
):
    """Full raw transcript for a session — EVERYTHING, including the tool calls,
    thinking, subagent chatter and late items the normal delivery funnel drops.
    Each text/note item is flagged whether it's already in the DisPatch DB."""
    _require_full_access(request)
    bot = config.resolve_bot(bot_id)
    if not bot:
        raise HTTPException(400, "Unknown bot_id")
    bot_id = bot.id
    in_db: set[str] = set()
    if thread_id:
        session_key = openclaw.session_key_for(bot_id, thread_id)
        msgs = await db.dump_messages(thread_id)
        in_db = {_canon_msg(m.content or "") for m in msgs if m.role == "assistant"}
    if not session_key:
        raise HTTPException(400, "thread_id or session_key required")
    path = openclaw.resolve_session_file(bot_id, session_key)
    if path is None:
        return {"bot_id": bot_id, "session_key": session_key, "items": [],
                "found": False}
    items = openclaw.read_transcript_items(path, include_all=True)
    for it in items:
        it["in_db"] = (it.get("kind") in ("text", "note")
                       and _canon_msg(it.get("text") or "") in in_db)
    deliverable = sum(1 for it in items if it.get("kind") in ("text", "note"))
    missing = sum(1 for it in items
                  if it.get("kind") in ("text", "note") and not it["in_db"])
    return {"bot_id": bot_id, "session_key": session_key, "thread_id": thread_id,
            "found": True, "items": items, "deliverable": deliverable,
            "missing_from_dispatch": missing}


@app.post("/api/openclaw/import")
async def openclaw_import_session(request: Request, payload: dict = Body(...)):
    """Import an arbitrary agent session (cron/main/subagent/…) into a NEW
    DisPatch thread, so conversations the app never created become first-class."""
    _require_full_access(request)
    bot = config.resolve_bot(payload.get("bot_id"))
    if not bot:
        raise HTTPException(400, "Unknown bot_id")
    bot_id = bot.id
    session_key = payload.get("session_key")
    if not session_key:
        raise HTTPException(400, "session_key required")
    path = openclaw.resolve_session_file(bot_id, session_key)
    if path is None:
        raise HTTPException(404, "No transcript for that session")
    title = (payload.get("title") or f"Imported · {session_key.split(':')[-1][:18]}")[:120]
    thread = await db.create_thread(bot_id=bot_id, title=title)
    await manager.broadcast({"type": "thread_created", "thread": thread.model_dump()})
    count = 0
    for it in openclaw.read_transcript_items(path, include_all=False):
        if it.get("kind") not in ("text", "note"):
            continue
        msg = await _deliver_assistant_text(thread.id, it.get("text") or "",
                                            metadata={"imported": True})
        if msg:
            count += 1
    await _broadcast_thread_update(thread.id)
    return {"ok": True, "thread_id": thread.id, "imported": count}


# --------------------------------------------------------------------------- #
# Gateway chat mirror
#
# The transcript bridge above is ON-DEMAND: it recovers messages when asked to.
# The mirror is CONTINUOUS: a background poller that tails the gateway's own
# conversations — the Control-UI webchat threads and each agent's main session,
# which DisPatch never created and whose replies therefore never reach it — and
# imports BOTH sides (user + assistant) into ordinary DisPatch threads as they
# are written. A webchat mirror thread reuses the gateway thread tag as its own
# id, so replying to it from DisPatch continues the very same gateway session.
#
# A session whose tag already IS a DisPatch thread ("native") is tailed into
# that SAME thread instead of a new one. The four live delivery paths remain
# the fast path there; the mirror is the always-on safety net behind them — it
# catches what they structurally can't: a user message typed on the Control-UI
# side of the same session (the live funnel never delivers user rows), and
# assistant replies landing after the 30-minute follower window expires.
#
# Safety properties:
#   - append-only byte-offset tailing: only new complete lines are parsed each
#     cycle; a truncated/compacted transcript falls back to one whole-history-
#     dedup pass instead of re-importing everything;
#   - assistant text goes through the same _deliver_assistant_text funnel as
#     the four live paths, so mirror and live delivery can never double-post;
#   - the native/webchat decision is made ONCE per session and persisted;
#   - deleting a mirrored OR native thread mutes its session permanently —
#     the mirror never resurrects a deleted conversation;
#   - a thread with a live DisPatch turn in flight is skipped for that cycle.
# State (decisions + offsets) lives in DATA_DIR/gateway-mirror.json.
# --------------------------------------------------------------------------- #

_MIRROR_DOC_MARKER = "--- BEGIN DOCUMENT:"   # agent-facing doc inline, not typed chat


def _mirror_state_path() -> Path:
    return config.DATA_DIR / "gateway-mirror.json"


# Parsed mirror state, kept until the file changes underneath us.
#
# `_gateway_resolve_thread` calls _load_mirror_state() for EVERY event on the
# firehose, and the firehose covers every session on the box — so a synchronous
# read + JSON parse of a file that grows with the install was running on the
# event loop, several times a second, to answer one membership test. The mtime
# check keeps a hand-edited file (or another process) from being missed while
# costing one stat instead of a read and a parse.
_mirror_state_cache: dict[str, Any] = {"path": None, "mtime": None, "data": None}


def _load_mirror_state() -> dict:
    path = _mirror_state_path()
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        mtime = None
    cache = _mirror_state_cache
    if (cache["data"] is not None and cache["path"] == path
            and cache["mtime"] == mtime):
        return cache["data"]
    data: dict = {"version": 1, "sessions": {}}
    try:
        parsed = json.loads(path.read_text())
        if isinstance(parsed, dict) and isinstance(parsed.get("sessions"), dict):
            data = parsed
    except (OSError, json.JSONDecodeError):
        pass
    cache["path"], cache["mtime"], cache["data"] = path, mtime, data
    return data


def _save_mirror_state(state: dict) -> None:
    path = _mirror_state_path()
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=1))
    tmp.replace(path)
    # Adopt what we just wrote rather than re-reading it on the next event.
    _mirror_state_cache["path"] = path
    _mirror_state_cache["data"] = state
    try:
        _mirror_state_cache["mtime"] = path.stat().st_mtime_ns
    except OSError:
        _mirror_state_cache["mtime"] = None


def _mirror_kind_set() -> set[str]:
    return {k.strip() for k in SETTINGS.mirror_kinds.split(",") if k.strip()}


def _read_transcript_tail(path: Path, offset: int) -> tuple[list[dict], int]:
    """Parse the COMPLETE lines appended past ``offset``.

    Returns (items, new_offset). A trailing partial line (the gateway is
    mid-append) is left in place for the next cycle — new_offset only ever
    advances past a terminating newline, so a line is never parsed twice or
    half-parsed once.
    """
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            chunk = f.read()
    except OSError:
        return [], offset
    nl = chunk.rfind(b"\n")
    if nl < 0:
        return [], offset
    items = openclaw.transcript_items_from_bytes(chunk[: nl + 1], include_all=True)
    return items, offset + nl + 1


async def _mirror_recent_dup(thread_id: str, role: str, text: str) -> bool:
    """True if an identical message with this role is in the recent window —
    catches the transcript echoing back a message DisPatch itself sent."""
    msgs, _ = await db.list_messages(thread_id, limit=40)
    norm = _canon_msg(text)
    return any(m.role == role and _canon_msg(m.content or "") == norm for m in msgs)


async def _mirror_import_items(thread_id: str, bot_id: str, items: list[dict],
                               *, whole_history: bool = False) -> int:
    """Persist parsed transcript items into a mirror thread, in order.

    ``whole_history`` (truncation-reset pass): dedup BOTH roles against the
    entire thread, like the transcript importer — re-parsing from offset 0 must
    only fill gaps. Tail passes rely on the live funnel's dedup for assistant
    text and a recent-window check for user echoes.
    """
    count = 0
    existing_user: set[str] = set()
    existing_asst: set[str] = set()
    if whole_history:
        msgs = await db.dump_messages(thread_id)
        existing_user = {_canon_msg(m.content or "") for m in msgs if m.role == "user"}
        existing_asst = {_canon_msg(m.content or "") for m in msgs if m.role == "assistant"}
    for it in items:
        kind = it.get("kind")
        text = (it.get("text") or "").strip()
        if not text:
            continue
        if kind == "user":
            # A transcript "user" row is often not something a human typed: the
            # runtime injects subagent completion events and other context the
            # same way. Strip that scaffolding (the Control UI never shows it);
            # what's left of a pure-context row is "", which we skip.
            text = openclaw_text.sanitize_user_visible_text(text)
            if not text:
                continue
            if _MIRROR_DOC_MARKER in text:
                continue          # inlined attachment copy, not what was typed
            key = _canon_msg(text)
            if whole_history:
                if key in existing_user:
                    continue
                existing_user.add(key)
            elif await _mirror_recent_dup(thread_id, "user", text):
                continue          # the DisPatch send path already posted it
            await _persist_and_broadcast_message(thread_id, "user", text,
                                                 metadata={"mirrored": True})
            count += 1
        elif kind in ("text", "note"):
            if whole_history:
                key = _canon_msg(text)
                if key in existing_asst:
                    continue
                existing_asst.add(key)
            msg = await _deliver_assistant_text(thread_id, text,
                                                metadata={"mirrored": True})
            if msg:
                count += 1
    return count


async def _mirror_create_thread(bot: config.Bot, kind: str, tag: str,
                                items: list[dict]) -> str | None:
    """Create — or adopt — the DisPatch thread for a gateway session.

    If the deterministic id already exists and belongs to the same bot (a
    native thread that appeared since first-sight, or an earlier mirror
    thread), tail into it. None = the id belongs to a DIFFERENT bot's thread;
    the caller mutes the session rather than cross-posting.
    """
    if kind == "webchat":
        tid = tag
        first_user = next((it.get("text") or "" for it in items
                           if it.get("kind") == "user"), "").strip()
        title = f"Webchat · {_truncate(first_user, 48)}" if first_user else "Webchat session"
    else:
        # A main-session mirror gets its own deterministic id — "main" itself
        # would collide across bots (thread ids are globally unique).
        tid = f"gw-main-{bot.id.lower()}"
        title = "Gateway main session"
    existing = await db.resolve_thread_id(tid)
    if existing:
        t = await db.get_thread(existing)
        if t and t.bot_id.lower() == bot.id.lower():
            return existing
        return None
    try:
        thread = await db.create_thread(bot_id=bot.id, title=title, thread_id=tid)
    except Exception:
        log.exception("gateway mirror: could not create thread %s", tid)
        return None
    _thread_bot[thread.id] = bot.id
    await manager.broadcast({"type": "thread_created", "thread": thread.model_dump()})
    return thread.id


async def _mirror_cycle(state: dict) -> bool:
    """One poll over every roster bot's gateway sessions. Returns True when the
    state dict changed (the caller persists it)."""
    dirty = False
    kinds = _mirror_kind_set()
    horizon_s = max(0, SETTINGS.mirror_horizon_h) * 3600
    now = time.time()
    for bot in config.load_bots():
        try:
            sessions = openclaw.list_agent_sessions(bot.id)
        except Exception:
            log.exception("gateway mirror: session listing failed for %s", bot.id)
            continue
        for s in sessions:
            if _shutting_down:
                return dirty
            kind = openclaw.mirror_kind(s["session_key"])
            if kind not in kinds:
                continue
            tag = s["session_key"].split(":", 2)[2]
            skey = f"{bot.id.lower()}|{s['session_key'].lower()}"
            ent = state["sessions"].get(skey)
            if ent is not None and "sid" not in ent:
                ent = None      # legacy schema (pre-native-tail) — re-decide
            if ent is None:
                # First sight — decide once. Only a webchat-shaped tag can be a
                # native DisPatch thread; a native session tails into that same
                # thread (offset seeding identical: young sessions get one
                # whole-history-dedup backfill, old ones tail from EOF).
                native_tid = (await db.resolve_thread_id(tag)
                              if kind == "webchat" else None)
                offset = 0 if (now - s["mtime"]) <= horizon_s else s["size"]
                ent = {"status": "native" if native_tid else "active",
                       "sid": s["session_id"], "thread_id": native_tid,
                       "offset": offset}
                state["sessions"][skey] = ent
                dirty = True
            if ent.get("status") not in ("active", "native"):
                continue
            if ent.get("sid") != s["session_id"]:
                # Same key, new session id (cleared/recreated) — start over.
                ent.update(sid=s["session_id"], offset=0)
                dirty = True
            thread_id = ent.get("thread_id")
            if thread_id:
                if not await db.get_thread(thread_id):
                    ent["status"] = "muted"    # user deleted the mirror thread
                    dirty = True
                    continue
                lock = _thread_locks.get(thread_id)
                if lock is not None and lock.locked():
                    continue        # a live DisPatch turn owns this thread now
            path = openclaw.session_file_by_id(bot.id, s["session_id"])
            if path is None:
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            offset = int(ent.get("offset") or 0)
            if size < offset:                  # truncated/compacted — reparse
                offset = 0
            # An offset-0 pass sweeps the whole transcript (native backfill or
            # truncation reset) — dedup against the whole thread, not just the
            # live funnel's trailing window.
            whole_history = offset == 0
            if size <= offset:
                continue                       # nothing new
            items, new_offset = _read_transcript_tail(path, offset)
            deliverable = [it for it in items
                           if it.get("kind") in ("user", "text", "note")
                           and (it.get("text") or "").strip()]
            if thread_id is None:
                if not deliverable:            # tool noise only — just advance
                    if new_offset != offset:
                        ent["offset"] = new_offset
                        dirty = True
                    continue
                thread_id = await _mirror_create_thread(bot, kind, tag, deliverable)
                if thread_id is None:      # id owned by another bot's thread
                    ent["status"] = "muted"
                    dirty = True
                    continue
                ent["thread_id"] = thread_id
                dirty = True
            n = await _mirror_import_items(thread_id, bot.id, deliverable,
                                           whole_history=whole_history)
            if new_offset != int(ent.get("offset") or 0):
                ent["offset"] = new_offset
                dirty = True
            if n:
                await _broadcast_thread_update(thread_id)
                log.info("gateway mirror (%s/%s): +%d message(s)", bot.id, tag, n)
    return dirty


# Bumped (monotonically) by user-facing activity so the mirror snaps back to
# its fast poll immediately instead of waiting out an idle-backoff sleep.
_mirror_nudge_seq = 0
_mirror_nudge_event: asyncio.Event | None = None
_mirror_task: asyncio.Task | None = None
_mirror_beat = 0.0            # wall-clock of the last started loop iteration

# On 2026-07-29 one cycle parked forever on an await and silently killed the
# mirror for 21 h (state file frozen, zero journal lines). Two layers now make
# that impossible: every cycle is time-bounded, and a watchdog respawns the
# whole loop if the heartbeat ever goes stale anyway.
_MIRROR_CYCLE_TIMEOUT_S = 180.0
_MIRROR_STALL_S = 600.0


def _mirror_nudge() -> None:
    global _mirror_nudge_seq
    _mirror_nudge_seq += 1
    if _mirror_nudge_event is not None:
        _mirror_nudge_event.set()       # interrupt an idle-backoff sleep now


def _mirror_delay(idle_cycles: int, base: int, idle_max: int) -> float:
    """Poll delay after ``idle_cycles`` consecutive no-change cycles.

    Ramp: +2s per quiet cycle, capped at idle_max. One quiet minute already
    slows the scan several-fold; a change resets to the base instantly.
    """
    if idle_cycles <= 0:
        return float(base)
    return float(min(idle_max, base + 2 * idle_cycles))


async def _gateway_mirror_loop() -> None:
    global _mirror_beat, _mirror_nudge_event
    if not SETTINGS.mirror_enabled:
        return
    if _transcript_paths_dead():
        log.info("gateway mirror stood down: no transcripts on this host and "
                 "the gateway socket is delivering")
        return
    if _mirror_nudge_event is None:
        _mirror_nudge_event = asyncio.Event()
    state = _load_mirror_state()
    base = max(2, SETTINGS.mirror_poll)
    idle_max = max(base, SETTINGS.mirror_idle_max)
    idle = 0
    nudge_seen = _mirror_nudge_seq
    try:
        await asyncio.sleep(3)              # let startup recovery settle first
        while not _shutting_down:
            _mirror_beat = time.time()
            try:
                if await asyncio.wait_for(_mirror_cycle(state),
                                          _MIRROR_CYCLE_TIMEOUT_S):
                    _save_mirror_state(state)
                    idle = 0
                else:
                    idle += 1
            except TimeoutError:
                idle += 1
                # The cycle mutates `state` in place, so any offsets it
                # advanced before the stall are real progress — persist them
                # (best-effort) like the success/shutdown paths do, or a
                # restart right after a hang re-reads work already done.
                # wait_for has finished cancelling the inner task by the time
                # TimeoutError is raised, so nothing is still mutating state.
                with contextlib.suppress(Exception):
                    _save_mirror_state(state)
                log.error("gateway mirror: cycle hung >%ds — cancelled, "
                          "continuing", int(_MIRROR_CYCLE_TIMEOUT_S))
            except Exception:
                # A persistently failing cycle backs off too — otherwise it
                # would also spam the journal every `base` seconds.
                idle += 1
                log.exception("gateway mirror: cycle failed")
            if _mirror_nudge_seq != nudge_seen:
                nudge_seen = _mirror_nudge_seq
                idle = 0                    # user activity → fast poll now
            _mirror_nudge_event.clear()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(_mirror_nudge_event.wait(),
                                       _mirror_delay(idle, base, idle_max))
    except asyncio.CancelledError:
        # Persist offsets so the next boot resumes exactly where we stopped —
        # but never let a watchdog-cancelled stale task clobber the state its
        # replacement is already advancing.
        if _mirror_task is asyncio.current_task():
            with contextlib.suppress(Exception):
                _save_mirror_state(state)


async def _mirror_watchdog_loop() -> None:
    """Respawn the mirror loop if its task dies or its heartbeat goes stale.

    The cycle timeout above should make a permanent hang impossible; this is
    the backstop for whatever the next 2026-07-29 turns out to be. A respawn
    reloads state from disk, so at worst a few already-imported items get
    re-read and deduped.
    """
    global _mirror_task, _mirror_beat
    if not SETTINGS.mirror_enabled:
        return
    while not _shutting_down:
        await asyncio.sleep(60)
        if _shutting_down:
            return
        dead = _mirror_task is None or _mirror_task.done()
        beat_age = time.time() - _mirror_beat
        if not dead and beat_age < _MIRROR_STALL_S:
            continue
        log.error("gateway mirror: %s (last heartbeat %.0fs ago) — respawning",
                  "task is dead" if dead else "loop stalled", beat_age)
        if _mirror_task is not None and not _mirror_task.done():
            _mirror_task.cancel()
        _mirror_beat = time.time()      # fresh grace window for the new task
        _mirror_task = asyncio.create_task(_gateway_mirror_loop())
        _track(_mirror_task)


# --------------------------------------------------------------------------- #
# Code-execution surfaces: the shared PIN gate + feature-availability helpers
# used by the harness / StudioForge panes and the /api/auth/status inventory.
# --------------------------------------------------------------------------- #


# Code-execution surfaces (PTY, headless jobs) are unavailable until this
# install has a PIN. Without one the whole app is deliberately open — which is
# fine for a chat log on a home LAN and NOT fine for an unauthenticated shell,
# and DisPatch ships listening on 0.0.0.0. The env flags gate the FEATURE; a
# PIN is what gates access to it, so "enabled" plus "no PIN" is off, not open.
_NO_PIN_MSG = ("Set a PIN first — this feature runs code and stays unavailable "
               "until DisPatch has one.")


def harness_available() -> bool:
    return SETTINGS.harness_enabled and auth.load().pin_set


def studioforge_available() -> bool:
    """Flag ON, an address configured, and a PIN set. The URL half matters as
    much as the flag: with no address there is nothing to frame, and the panel
    it points at has no authentication of its own, so the PIN gate is the only
    thing standing between a passer-by and the rig's admin UI."""
    return (SETTINGS.studioforge_enabled and bool(SETTINGS.studioforge_url)
            and auth.load().pin_set)


# --------------------------------------------------------------------------- #
# DeepSeek Harness (dsh): `dsh web` service control + default model + headless
# jobs. A headless job is code execution, so
# every surface (status included) is FULL-SESSION ONLY; Safe Mode gets 403.
# --------------------------------------------------------------------------- #


def _require_harness(request: Request) -> None:
    if not SETTINGS.harness_enabled:
        raise HTTPException(404, "Harness disabled")
    if not auth.load().pin_set:
        raise HTTPException(403, _NO_PIN_MSG)
    _deny_decoy_mutation(request)


def _raise_for_harness_error(e: Exception):
    if isinstance(e, harness.ValidationError):
        raise HTTPException(422, str(e)) from e
    if isinstance(e, harness.HarnessBusyError):
        raise HTTPException(409, str(e)) from e
    if isinstance(e, harness.HealthTimeoutError):
        raise HTTPException(504, str(e)) from e
    raise HTTPException(502, str(e)) from e


def _harness_state_frame(job_status: dict | None = None) -> dict:
    return {"type": "harness_state", "jobs": job_status or harness.runner.status()}


def _harness_state_changed(status: dict) -> None:
    if _shutting_down:
        return
    _track(asyncio.create_task(manager.broadcast(_harness_state_frame(status))))


async def _harness_service_status() -> dict:
    unit = await harness.unit_state(SETTINGS.harness_unit)
    healthy = await harness.health(SETTINGS.harness_port)
    return {
        "installed": harness.installed(),
        "binary": harness.resolve_binary(),
        "unit": unit,
        "healthy": healthy,
        "port": SETTINGS.harness_port,
        "url": f"http://127.0.0.1:{SETTINGS.harness_port}/",
        "home": str(harness.dsh_home()),
    }


@app.get("/api/harness/status")
async def harness_status(request: Request):
    _require_harness(request)
    st = await _harness_service_status()
    st["models"] = await asyncio.to_thread(harness.discover_models)
    st["jobs"] = harness.runner.status()
    return st


async def _harness_service_op(request: Request, op: str):
    _require_harness(request)
    try:
        if op == "start":
            await harness.start(SETTINGS.harness_unit, SETTINGS.harness_port)
        elif op == "stop":
            await harness.stop(SETTINGS.harness_unit)
        else:
            await harness.restart(SETTINGS.harness_unit, SETTINGS.harness_port)
    except harness.HarnessError as e:
        _raise_for_harness_error(e)
    st = await _harness_service_status()
    if not _shutting_down:
        await manager.broadcast({"type": "harness_state", "service": st})
    return st


@app.post("/api/harness/start")
async def harness_start(request: Request):
    return await _harness_service_op(request, "start")


@app.post("/api/harness/stop")
async def harness_stop(request: Request):
    return await _harness_service_op(request, "stop")


@app.post("/api/harness/restart")
async def harness_restart(request: Request):
    return await _harness_service_op(request, "restart")


@app.get("/api/harness/models")
async def harness_models(request: Request):
    """Provider/model catalog from dsh's settings.yaml + the current default."""
    _require_harness(request)
    return await asyncio.to_thread(harness.discover_models)


@app.post("/api/harness/model")
async def harness_set_model(request: Request):
    """Set `agent-default-model` (applies to the next new dsh session, Web UI
    and headless alike). Body: {provider, model}."""
    _require_harness(request)
    body = {}
    with contextlib.suppress(Exception):
        body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(422, "body must be an object")
    try:
        sel = await asyncio.to_thread(harness.set_default_model, body.get("provider"), body.get("model"))
    except harness.HarnessError as e:
        _raise_for_harness_error(e)
    models = await asyncio.to_thread(harness.discover_models)
    if not _shutting_down:
        await manager.broadcast({"type": "harness_state", "models": models})
    return {"current": sel, "models": models}


@app.get("/api/harness/jobs")
async def harness_jobs(request: Request):
    _require_harness(request)
    return {"jobs": harness.runner.jobs()}


@app.get("/api/harness/jobs/{job_id}")
async def harness_job(job_id: int, request: Request):
    _require_harness(request)
    j = harness.runner.job(job_id)
    if j is None:
        raise HTTPException(404, "no such job")
    return j


@app.post("/api/harness/jobs")
async def harness_submit_job(request: Request):
    """Run one `dsh --profile headless "<task>"`. Body: {task, cwd?}. The task
    is a single argv element (no shell); cwd must be an existing directory
    under $HOME. 409 while another job runs."""
    _require_harness(request)
    body = {}
    with contextlib.suppress(Exception):
        body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(422, "body must be an object")
    try:
        task = harness.validate_task(body.get("task"))
        cwd = harness.validate_cwd(body.get("cwd"))
        return await harness.runner.submit(task, cwd)
    except harness.HarnessError as e:
        _raise_for_harness_error(e)


@app.post("/api/harness/jobs/cancel")
async def harness_cancel_job(request: Request):
    _require_harness(request)
    try:
        return await harness.runner.cancel()
    except harness.HarnessError as e:
        _raise_for_harness_error(e)


# --------------------------------------------------------------------------- #
# StudioForge control panel (the LLM rig's own web UI, embedded)
#
# DisPatch does exactly three things here: say whether the feature is on, hand
# the client the configured address, and answer "did a GET of it come back".
# What it deliberately does NOT do:
#
#   * proxy. The panel's asset paths are absolute, so a sub-path mount cannot
#     work -- but the real reason is WHO could reach the rig. Framed, the
#     client device must itself be on the rig's network; proxied, every LAN
#     device on :8765 and everything behind the Tailscale-Serve front door
#     would inherit full admin on a panel that has no password.
#   * touch the inference port, take a GPU lease, or call any mutating route.
#     A benchmark may be holding the rig; a status widget must never be able to
#     disturb it. Read-only GET of the panel URL, nothing else, ever.
#   * hold a PIN or an API key. There is no credential in this feature.
# --------------------------------------------------------------------------- #

# How long a reachability answer is reused. A repainting client (or several
# devices) must not turn a status widget into a poll of somebody else's box;
# 15s is short enough that "the rig came back" shows up promptly and long
# enough that a render loop costs one request, not hundreds.
_SF_PROBE_TTL = 15.0
_SF_PROBE_TIMEOUT = 3.0
_sf_probe: tuple[float, bool, bool | None] | None = None   # (checked_at, reachable, framable)
_sf_probe_lock = asyncio.Lock()


def _require_studioforge(request: Request) -> None:
    """404 when the feature is off or unconfigured (an install that was never
    told the panel's address must not even admit the route exists), 403 with no
    PIN, 403 for a Safe-Mode session. Mirrors _require_harness."""
    if not SETTINGS.studioforge_enabled or not SETTINGS.studioforge_url:
        raise HTTPException(404, "StudioForge panel disabled")
    if not auth.load().pin_set:
        raise HTTPException(403, _NO_PIN_MSG)
    _deny_decoy_mutation(request)


def _framable(headers) -> bool:
    """Whether a browser would let us put this response in an <iframe>.

    Not a guess and not a policy of ours -- it is the two headers the browser
    itself obeys, read from the panel's own answer:

      * `X-Frame-Options: DENY` / `SAMEORIGIN` (we are never same-origin with
        the rig, so both refuse us).
      * CSP `frame-ancestors`, which SUPERSEDES X-Frame-Options where both are
        present. `'none'` refuses everyone; a source list is only satisfied by
        an origin on it, and DisPatch is served from whatever host the reader
        typed -- a value we do not know here. So any frame-ancestors that is
        not a bare wildcard is treated as a refusal: a wrong "yes" costs the
        reader a blank rectangle and no explanation, a wrong "no" costs one
        extra click on a link that is already on screen.

    Anything else (no header at all, an unparseable one) is framable, which is
    the web's own default."""
    # frame-ancestors first: where both are present the CSP directive wins and
    # the browser ignores X-Frame-Options entirely.
    csp = (headers.get("content-security-policy") or "").lower()
    for directive in csp.split(";"):
        parts = directive.split()
        if parts and parts[0] == "frame-ancestors":
            return parts[1:] == ["*"]
    xfo = (headers.get("x-frame-options") or "").strip().lower()
    if xfo in ("deny", "sameorigin") or xfo.startswith("allow-from"):
        return False
    return True


async def _studioforge_reachable() -> tuple[bool, float, bool | None]:
    """Plain GET of the panel URL, cached. ANY HTTP response counts as
    reachable -- a 404, a 403, a redirect all prove something is listening and
    answering, and the panel's own routing is none of our business. Only a
    transport failure (refused, DNS, timeout) is "down".

    The same response also answers the second question the pane needs: whether
    the panel permits being framed. StudioForge as shipped does NOT (it sends
    `X-Frame-Options: DENY`), so the embedded frame could only ever draw an
    empty box -- which is exactly what it did. Reading the headers here means
    the pane can say so and offer the link instead of pretending."""
    global _sf_probe
    now = time.time()
    cached = _sf_probe
    if cached and now - cached[0] < _SF_PROBE_TTL:
        return cached[1], cached[0], cached[2]
    async with _sf_probe_lock:
        cached = _sf_probe            # another caller may have filled it
        if cached and time.time() - cached[0] < _SF_PROBE_TTL:
            return cached[1], cached[0], cached[2]
        ok = False
        framable: bool | None = None
        try:
            async with httpx.AsyncClient(timeout=_SF_PROBE_TIMEOUT,
                                         follow_redirects=False) as client:
                resp = await client.get(SETTINGS.studioforge_url)
            ok = True
            framable = _framable(resp.headers)
        except Exception:
            ok = False
        _sf_probe = (time.time(), ok, framable)
        return ok, _sf_probe[0], framable


@app.get("/api/studioforge/status")
async def studioforge_status(request: Request):
    _require_studioforge(request)
    reachable, checked_at, framable = await _studioforge_reachable()
    return {"url": SETTINGS.studioforge_url,
            "reachable": reachable,
            # None when the panel could not be reached at all -- "we do not
            # know yet", which the pane must not read as "refused".
            "framable": framable,
            "checked_at": checked_at}


# --------------------------------------------------------------------------- #
# WebSocket
# --------------------------------------------------------------------------- #


async def _ws_bot_allowed(ws: WebSocket, bot_id: str | None) -> bool:
    """False (with an error frame) when a Safe-Mode connection targets an
    unsafe bot. Full connections always pass."""
    if manager.conn_decoy(ws) and bot_id not in _safe_bot_ids():
        await manager.send(ws, {"type": "error", "message": "Unlock for full access"})
        return False
    return True


# Recently persisted client_msg_ids (WS send-ack protocol). A reconnecting
# client re-sends its pending frames unchanged; a duplicate id is re-acked
# 'ok' but never persisted twice. Bounded LRU — single user, tiny volume.
#
# THE ENTRY RECORDS WHETHER THE TURN WAS SCHEDULED, not merely that the message
# was stored. The id was marked immediately after `db.add_message` while the
# agent turn is dispatched at the very end of the handler — and everything in
# between can stall or die (the broadcast alone waits up to five seconds per
# half-dead client). A resend then matched the id, was re-acked "ok", and
# nothing ever ran: the message sat in the thread and the bot never answered.
# Recording the dispatch as its own fact lets the replay finish the job instead
# of confirming a turn that does not exist.
_ACK_SEEN: OrderedDict[str, dict] = OrderedDict()
_ACK_SEEN_CAP = 512


def _ack_mark(client_msg_id: str, **fields: Any) -> None:
    entry = _ACK_SEEN.get(client_msg_id) or {"scheduled": False}
    entry.update(fields)
    _ACK_SEEN[client_msg_id] = entry
    _ACK_SEEN.move_to_end(client_msg_id)
    while len(_ACK_SEEN) > _ACK_SEEN_CAP:
        _ACK_SEEN.popitem(last=False)


async def _ack(ws: WebSocket, client_msg_id: str | None,
               status: str = "ok", reason: str | None = None) -> None:
    """Ack a 'send' on the ORIGINATING connection. No client_msg_id (an old
    client) ⇒ no ack — backwards compatible. Reasons must stay short + neutral:
    decoy connections receive these frames too."""
    if not client_msg_id:
        return
    frame: dict = {"type": "ack", "client_msg_id": client_msg_id, "status": status}
    if reason:
        frame["reason"] = reason
    await manager.send(ws, frame)


async def _handle_send(ws: WebSocket, data: dict) -> None:
    thread_id = data.get("thread_id")
    text = (data.get("text") or "").strip()
    cmid = data.get("client_msg_id")
    cmid = cmid if isinstance(cmid, str) and cmid else None
    seen = _ACK_SEEN.get(cmid) if cmid else None
    if seen is not None:
        # Reconnect resend of an already-persisted message: re-ack, don't dup.
        await _ack(ws, cmid, "ok")
        if not seen.get("scheduled") and seen.get("thread_id"):
            # Stored but never dispatched (see _ACK_SEEN). Finish the job
            # rather than confirming a turn that was never started.
            log.warning("resend of %s: the message was stored but its turn "
                        "never ran — dispatching it now", cmid)
            seen["scheduled"] = True
            _track(asyncio.create_task(run_agent_turn(
                seen["thread_id"], seen["bot_id"], seen["text"])))
        return
    if not thread_id or not text:
        await _ack(ws, cmid, "rejected", "Empty message")
        return
    if len(text) > MESSAGE_MAX_CHARS:
        await manager.send(ws, {"type": "error", "thread_id": thread_id,
                                "message": "Message too long (max 64KB)."})
        await _ack(ws, cmid, "rejected", "Message too long")
        return
    thread = await db.get_thread(thread_id)
    if not thread:
        await manager.send(ws, {"type": "error", "thread_id": thread_id,
                                "message": "Thread not found"})
        await _ack(ws, cmid, "rejected", "Thread not found")
        return
    if not await _ws_bot_allowed(ws, thread.bot_id):
        await _ack(ws, cmid, "rejected", "Not allowed")
        return

    if manager.conn_decoy(ws):
        if not _decoy_action_allowed(_ws_client_ip(ws), "turn", DECOY_TURN_QUOTA):
            await manager.send(ws, {"type": "error", "thread_id": thread_id,
                                    "message": "Daily message limit reached — "
                                               "try again tomorrow or unlock."})
            await _ack(ws, cmid, "rejected", "Daily limit reached")
            return
        # Safe Mode CAN attach uploads (the "＋" button) but must never ingest a
        # local path: keep store-resident refs (/media/<uuid>, [[doc:<id>]]) so
        # they reach the agent + unlocked devices, drop anything pointing at a
        # local file. The decoy DISPLAY still fully redacts media, so what's sent
        # here stays invisible in the locked view (images stay hidden).
        text = (_decoy_keep_uploaded_media(text) or "").strip()
        if not text:
            await _ack(ws, cmid, "rejected", "Message could not be sent")
            return
    else:
        # Persist + echo the user message immediately (ingesting any local
        # media — a blocking disk copy, so off the event loop).
        if "[[media:" in text:
            text = await asyncio.to_thread(_ingest_content_media, text)
    # Marker strip, as the persist chokepoint does it on every other path. The
    # composer went straight to db.add_message, so a typed `:react:x:` reached
    # the bubble verbatim AND left _canon_msg disagreeing with the stored text
    # (dedup then failed to recognise the message it had just written). Only an
    # assistant's markers ever FIRE; a user typing one just loses the syntax.
    if ":react:" in text.lower():
        text, _ = reactions.extract_markers(text)
        text = text.strip()
        if not text:
            await _ack(ws, cmid, "rejected", "Empty message")
            return
    try:
        user_msg = await db.add_message(thread_id, "user", text)
    except Exception:
        # Neutral reason — no exception detail in any client-visible frame.
        await _ack(ws, cmid, "rejected", "Server error — please retry")
        raise
    if cmid:
        _ack_mark(cmid, thread_id=thread_id, bot_id=thread.bot_id, text=text)
    await db.set_title_if_empty(thread_id, _truncate(text, 50))
    msg_frame = {"type": "message", "thread_id": thread_id, "bot_id": thread.bot_id,
                 "message": user_msg.model_dump()}
    if cmid:
        # Secondary pending-clear signal (and lets other tabs ignore their own).
        msg_frame["client_msg_id"] = cmid
    # Ack BEFORE the broadcast: the message is durable at this point, and the
    # broadcast can stall up to 5s per half-dead client.
    await _ack(ws, cmid, "ok")
    await manager.broadcast(msg_frame)
    await _broadcast_thread_update(thread_id)

    _track(asyncio.create_task(run_agent_turn(thread_id, thread.bot_id, text)))
    if cmid:
        # AFTER the dispatch, so "seen" can never mean "acked but never run".
        _ack_mark(cmid, scheduled=True)


async def _handle_create_thread(ws: WebSocket, data: dict) -> None:
    bot_id = data.get("bot_id")
    if not bot_id or not config.get_bot(bot_id):
        await manager.send(ws, {"type": "error", "message": "Unknown bot_id"})
        return
    if not await _ws_bot_allowed(ws, bot_id):
        return
    if manager.conn_decoy(ws) and not _decoy_action_allowed(
            _ws_client_ip(ws), "thread", DECOY_THREAD_QUOTA):
        await manager.send(ws, {"type": "error",
                                "message": "Daily chat limit reached — "
                                           "try again tomorrow or unlock."})
        return
    thread = await db.create_thread(bot_id=bot_id, avatar_from_pool=True)
    await manager.broadcast({"type": "thread_created", "thread": thread.model_dump()})
    if SETTINGS.greeting:
        bot = config.get_bot(bot_id)
        greeting = f"Hey! {bot.name} here {bot.emoji}. What's up?"
        await _persist_and_broadcast_message(thread.id, "assistant", greeting)


async def _handle_get_threads(ws: WebSocket, data: dict) -> None:
    bot_id = data.get("bot_id")
    if not bot_id:
        return
    if not await _ws_bot_allowed(ws, bot_id):
        return
    threads = await db.list_threads(bot_id)
    await manager.send(ws, {
        "type": "threads_list", "bot_id": bot_id,
        "threads": [t.model_dump() for t in threads],
    })


async def _handle_get_messages(ws: WebSocket, data: dict) -> None:
    thread_id = data.get("thread_id")
    if not thread_id:
        return
    if manager.conn_decoy(ws):
        if not await _ws_bot_allowed(ws, await _bot_of_thread(thread_id)):
            return
    try:
        limit = max(1, min(500, int(data.get("limit") or 200)))
    except (TypeError, ValueError):
        limit = 200
    before_id = data.get("before_id")
    msgs, has_more = await db.list_messages(
        thread_id, limit=limit,
        before_id=before_id if isinstance(before_id, str) else None,
    )
    await manager.send(ws, {
        "type": "messages", "thread_id": thread_id,
        "messages": [m.model_dump() for m in msgs], "has_more": has_more,
    })


async def _handle_archive(ws: WebSocket, data: dict) -> None:
    thread_id = data.get("thread_id")
    if not thread_id:
        return
    # Archiving is a mutation — Safe Mode is view + send only, so a decoy
    # connection may not archive even a safe bot's thread.
    if manager.conn_decoy(ws):
        await manager.send(ws, {"type": "error", "message": "Unlock for full access"})
        return
    bot_id = await _bot_of_thread(thread_id)
    if not await _ws_bot_allowed(ws, bot_id):
        return
    await db.archive_thread(thread_id)
    # NOTE: the per-thread lock is intentionally KEPT. Archived threads remain
    # sendable, so dropping the lock here would let a queued send run a second
    # concurrent agent turn on the same OpenClaw session.
    await manager.broadcast({"type": "thread_deleted", "thread_id": thread_id,
                             "bot_id": bot_id, "hard": False})


async def _handle_retry(ws: WebSocket, data: dict) -> None:
    """Re-run the agent on the last user message without creating a duplicate."""
    thread_id = data.get("thread_id")
    if not thread_id:
        return
    thread = await db.get_thread(thread_id)
    if not thread or thread.status == "thinking":
        return
    if not await _ws_bot_allowed(ws, thread.bot_id):
        return
    last_user = await db.get_last_user_message(thread_id)
    if not last_user:
        await manager.send(ws, {"type": "error", "thread_id": thread_id,
                                "message": "No message to retry"})
        return
    # A retry costs exactly what a send costs — one model turn — so it is
    # charged to the same Safe-Mode daily quota. Without this a locked tab
    # could loop `retry` for unlimited turns while `send` stayed capped.
    if manager.conn_decoy(ws) and not _decoy_action_allowed(
            _ws_client_ip(ws), "turn", DECOY_TURN_QUOTA):
        await manager.send(ws, {"type": "error", "thread_id": thread_id,
                                "message": "Daily message limit reached — "
                                           "try again tomorrow or unlock."})
        return
    _track(asyncio.create_task(run_agent_turn(thread_id, thread.bot_id, last_user.content)))


async def _handle_abort(ws: WebSocket, data: dict) -> None:
    """Stop the turn a thread is waiting on. Unlocked connections only."""
    thread_id = data.get("thread_id")
    if not thread_id:
        return
    if manager.conn_decoy(ws):
        # The same rule as every other mutation, stated here because a
        # WebSocket never passes through the HTTP middleware that enforces it.
        await manager.send(ws, {"type": "error", "thread_id": thread_id,
                                "message": "Unlock for full access"})
        return
    try:
        result = await _abort_thread_turn(thread_id)
    except HTTPException as e:
        await manager.send(ws, {"type": "error", "thread_id": thread_id,
                                "message": str(e.detail)})
        return
    frame = {"type": "ack", "status": "ok", "thread_id": result["thread_id"]}
    cmid = data.get("client_msg_id")
    if isinstance(cmid, str) and cmid:
        frame["client_msg_id"] = cmid
    await manager.send(ws, frame)


WS_HANDLERS = {
    "send": _handle_send,
    "abort": _handle_abort,
    "create_thread": _handle_create_thread,
    "get_threads": _handle_get_threads,
    "get_messages": _handle_get_messages,
    "archive_thread": _handle_archive,
    "retry": _handle_retry,
}


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    # CSWSH guard: browsers attach the page's Origin to a WS handshake. Reject any
    # cross-origin handshake (Origin host != our Host). Non-browser clients send no
    # Origin and are allowed (CSRF is a browser-only vector). SameSite=Lax already
    # keeps the session cookie off cross-site handshakes, so this is defense-in-depth.
    origin = ws.headers.get("origin")
    if origin:
        from urllib.parse import urlparse
        origin_host = urlparse(origin).netloc
        host = ws.headers.get("host", "")
        # A present Origin must POSITIVELY match the request Host. An empty
        # Origin host (e.g. "Origin: null") or a missing Host header cannot be
        # matched — reject rather than fall through open.
        if not origin_host or not host or origin_host != host:
            await ws.close(code=1008)
            return
    # HTTP middleware doesn't run for the websocket scope, so the Safe-Mode
    # model is applied here directly. A full session → full connection; anything
    # else (no/expired session, PIN set) → a redacted Safe-Mode connection.
    cfg = auth.load()
    session = None
    decoy = False
    if cfg.pin_set:
        session = auth.get_session(ws.cookies.get(COOKIE_NAME))
        decoy = session is None      # no full session → Safe Mode
    await manager.connect(ws, decoy=decoy, token=(session.token if session else None))
    try:
        bots = [b for b in config.load_bots() if b.visible]
        if decoy:
            bots = [b for b in bots if b.safe]
        await ws.send_json({
            "type": "hello",
            "bots": [b.to_dict() for b in bots],
            "decoy": decoy,
        })
        while True:
            data = await ws.receive_json()
            mtype = data.get("type")
            # A full session that idle-expires drops to Safe Mode: tell the
            # client so it reconnects (as a Safe-Mode connection). A ping is NOT
            # activity (so idle genuinely expires); real actions slide the window.
            if session is not None:
                if auth.get_session(session.token) is None:
                    with contextlib.suppress(Exception):
                        await ws.send_json({"type": "locked"})
                    break
                if mtype != "ping":
                    auth.touch_session(session.token)
            elif not decoy and auth.load().pin_set:
                # Token-less full connection (opened while no PIN was set) and a
                # PIN exists now — nudge the client to reconnect as Safe Mode.
                with contextlib.suppress(Exception):
                    await ws.send_json({"type": "locked"})
                break
            if mtype == "ping":
                await manager.send(ws, {"type": "pong"})
                continue
            handler = WS_HANDLERS.get(mtype)
            if handler:
                # One bad statement must not tear down the whole connection:
                # log, send a NEUTRAL error frame (no exception detail — decoy
                # connections see these frames) and keep the receive loop alive.
                try:
                    await handler(ws, data)
                except WebSocketDisconnect:
                    # The handler's own send can surface a disconnect mid-turn;
                    # swallowing it would keep looping on a dead socket.
                    raise
                except Exception:
                    log.exception("ws handler %r failed", mtype)
                    with contextlib.suppress(Exception):
                        await manager.send(ws, {
                            "type": "error", "thread_id": data.get("thread_id"),
                            "message": "Server error — please retry.",
                        })
            else:
                await manager.send(ws, {"type": "error", "message": f"Unknown type: {mtype}"})
    except WebSocketDisconnect:
        pass
    except Exception:  # pragma: no cover - defensive
        log.exception("websocket error")
    finally:
        await manager.disconnect(ws)


# --------------------------------------------------------------------------- #
# Static frontend (mounted last so it doesn't shadow /api or /ws)
# --------------------------------------------------------------------------- #


# The one directive in index.html's meta CSP that has to know about site
# configuration. The StudioForge pane asks "can THIS device reach the rig"
# with a no-cors fetch before it loads the frame -- reachability depends on the
# viewing device's network, so it cannot be read off the hostname, and it
# cannot be read off the iframe either (onload fires for the browser's own
# error page, and the document is cross-origin and unreadable). `connect-src
# 'self' ws: wss:` blocks that fetch, so the configured ORIGIN (never the full
# URL) is spliced in at serve time. Widening by one operator-chosen origin, and
# only when the feature is on -- an install with it off gets the shipped file
# byte-for-byte, via the FileResponse below.
_CSP_CONNECT_SRC = "connect-src 'self' ws: wss:;"


def _studioforge_origin() -> str:
    if not (SETTINGS.studioforge_enabled and SETTINGS.studioforge_url):
        return ""
    from urllib.parse import urlsplit
    u = urlsplit(SETTINGS.studioforge_url)
    return f"{u.scheme}://{u.netloc}" if u.scheme and u.netloc else ""


@app.get("/")
async def index():
    index_file = FRONTEND_DIR / "index.html"
    if not index_file.exists():
        return JSONResponse({"error": "frontend not built"}, status_code=404)
    origin = _studioforge_origin()
    if origin:
        html = index_file.read_text(encoding="utf-8")
        if _CSP_CONNECT_SRC in html:
            html = html.replace(
                _CSP_CONNECT_SRC,
                f"connect-src 'self' ws: wss: {origin};", 1)
            return HTMLResponse(html)
        # The directive moved or was reformatted: serve the file unchanged
        # rather than an app with a half-applied policy. The pane then reports
        # "not reachable from this device" -- wrong, but honest and inert.
        log.warning("index.html connect-src directive not found; "
                    "StudioForge client probe will be blocked by CSP")
    return FileResponse(index_file)


# /media → user-uploaded images;  /static → app assets, avatars, vendor libs.
MEDIA_DIR.mkdir(parents=True, exist_ok=True)
# Operator dashboard (admin-only; the router carries its own fail-closed
# dependency, so it does not rely on auth_gate having run).
app.include_router(dashboard_routes.router)
# Local Viewer: /local/file/* + /api/local/*. The router carries its own
# full-access gate (localview._require_full_access), and _decoy_blocked bars
# the prefixes one layer earlier — both, deliberately.
app.include_router(localview.router)
# Jobs board (added 2026-09-14). Mounted only when the feature flag is
# set — when disabled, every /api/jobs/* route returns 404 (the router
# itself is not registered, so a sessionless caller never sees an empty
# 200). Plan §9 "Rollout order".
if bool(os.environ.get("JOBS_ENABLED") == "1"):
    jobs.JOBS_ENABLED = True
    app.include_router(jobs.router)

app.mount("/media", StaticFiles(directory=str(MEDIA_DIR)), name="media")
# BEFORE /static, and that order is the whole trick: Starlette matches mounts in
# order, so this claims /static/avatars/* and the general mount never sees it.
# Avatars therefore serve from the DATA directory while keeping the URL they
# have always had -- no frontend change, no config migration, and the Safe-Mode
# path checks (which match on a path segment starting "avatars") are untouched.
AVATAR_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static/avatars", StaticFiles(directory=str(AVATAR_DIR)), name="avatars")
app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="static")
