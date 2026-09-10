"""Operator host dashboard — the one page that answers "is this install healthy?".

This module is PURE. It gathers facts about the
host and turns them into *findings*; the routes in dashboard_routes.py own the
HTTP mapping and the auth gate. No FastAPI in here, so every probe is testable
with nothing but a :class:`~app.database.Database`.

Two invariants, non-negotiable, because this is the page an operator opens when
something is ALREADY broken:

  1. **It never raises.** Every probe is wrapped; a failure becomes a ``fail``
     finding carrying the reason, never a 500. A dashboard that dies on a full
     disk is worse than no dashboard at all.
  2. **It never blocks the event loop.** Filesystem walks, statvfs and the
     agent-version subprocess all go through ``asyncio.to_thread`` /
     ``create_subprocess_exec`` with a timeout, and the genuinely expensive
     checks only run on the ``deep`` path.

``collect(db)`` is the cheap summary a page may poll every few seconds;
``collect(db, deep=True)`` adds what you only want when a human asked for it:
``PRAGMA integrity_check`` (a full scan, on its own connection) and a real
directory walk of the blob stores.

Findings are the point of the module. Each one is
``{id, level, title, detail, fix}`` with level in ``ok`` | ``warn`` | ``fail``,
and ``fix`` is written for someone running this on a Raspberry Pi who has never
heard of a WAL: a concrete next action, not a diagnosis. The ids are stable —
they are what docs/dashboard.md is indexed by, so renaming one breaks an
operator's ability to look up what they are staring at.

What this module deliberately does NOT do: import main. Backup health is read
off the filesystem (BACKUP_DIR) rather than main's in-memory counters, which
also means it survives a restart — the moment you most want to know whether
last night's snapshot worked is right after the thing crashed.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import os
import platform
import shutil
import sqlite3
import stat
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from . import auth, config

try:                                        # package metadata, not a hard dep
    from . import __version__ as APP_VERSION
except ImportError:                         # pragma: no cover — packaging accident
    APP_VERSION = "unknown"

log = logging.getLogger("local-chat.dashboard")

# --------------------------------------------------------------------------- #
# Thresholds (one table, so "why is this amber?" is answerable from the source)
# --------------------------------------------------------------------------- #

# Free space on the data dir's filesystem. Ratio OR absolute, whichever is worse:
# 10% of a 2TB disk is plenty, and 1GB free on a 8GB SD card is not.
DISK_WARN_BYTES = 1024 * 1024 * 1024            # 1GB
DISK_FAIL_BYTES = 200 * 1024 * 1024             # 200MB
DISK_WARN_RATIO = 0.10
DISK_FAIL_RATIO = 0.02

# Stored blobs against the configured cap.
BLOB_WARN_RATIO = 0.80

# A backup older than interval * this is "stale" (the loop should have run).
BACKUP_STALE_FACTOR = 2.5
# Grace after boot before "no snapshot yet" is worth mentioning: the backup loop
# deliberately waits 60s, and the first interval hasn't elapsed yet either.
BACKUP_GRACE_S = 15 * 60

# Clock: how far the wall clock may disagree with our own data before we say so.
CLOCK_WARN_SKEW_S = 300                         # 5 min
CLOCK_FAIL_SKEW_S = 3600                        # 1 hour

# Per-probe ceiling. Nothing here is allowed to hang the page: a probe that
# blows this budget becomes a `fail` finding naming the probe.
PROBE_TIMEOUT_S = 5.0
# The deep pass is human-triggered and scans the whole database, so it gets a
# far bigger budget — 5s would turn every large-database deep check into a
# "probe timed out", which is the opposite of the answer being asked for.
DEEP_PROBE_TIMEOUT_S = 180.0
AGENT_VERSION_TIMEOUT_S = 3.0
# A FAILING `--version` is cached too, and briefly: an agent CLI that hangs
# costs a spawn + the timeout + the reap on EVERY 5s poll otherwise, which is a
# process storm aimed squarely at a box that is already unwell. Short, because
# "I fixed it" must show up on the page within a minute.
AGENT_VERSION_FAIL_TTL_S = 60.0

# The agent gateway — the process an agent turn actually talks to. A CLI on disk
# proves nothing about whether anything will reply, which is the exact symptom
# ("nothing answers") this page is opened for, so it gets its own probe.
# Deliberately a bare TCP connect with a short cache, mirroring main._gateway_ok:
# reimplemented rather than imported, because this module must not import main.
# Keep the address in lock-step with main._GATEWAY_ADDR.
GATEWAY_HOST = "127.0.0.1"
GATEWAY_PORT = 18789
GATEWAY_PROBE_TIMEOUT_S = 1.0
GATEWAY_PROBE_TTL_S = 15.0

# Blob-store walk: bounded work, cached between polls. A media dir with a
# million files must cost the same as one with ten. The deep pass lifts the
# budget, because "exact" is what the button promises.
DU_TTL_S = 30.0
DU_MAX_ENTRIES = 50_000
DU_MAX_ENTRIES_DEEP = 2_000_000

# Log tail limits (also the route's Query bounds — keep them in lock-step).
LOG_TAIL_DEFAULT = 200
LOG_TAIL_MAX_LINES = 2000
LOG_TAIL_MAX_BYTES = 4 * 1024 * 1024

# api_token values that mean "the operator copied the example and moved on".
_PLACEHOLDER_SECRETS = {
    "changeme", "change-me", "change_me", "changethis", "secret", "password",
    "token", "apikey", "api-key", "api_key", "dispatch", "local-chat",
    "localchat", "example", "test", "xxx", "todo", "replace-me", "replaceme",
    # Added with the rename: "dispatch-chat" is now the name an operator is
    # most likely to type when a field wants "something memorable". The older
    # names stay — this list only ever grows, because a token that was weak
    # before does not become strong when the product is renamed.
    "dispatch-chat", "dispatchchat",
}
_MIN_TOKEN_LEN = 16

OK, WARN, FAIL = "ok", "warn", "fail"
_LEVEL_ORDER = {OK: 0, WARN: 1, FAIL: 2}


class DashboardError(Exception):
    """A dashboard request could not be served. ``.status`` is the HTTP status
    the route should return; ``str(e)`` is safe to show the caller (no paths
    beyond ones the operator configured, no tracebacks).

    Note this is for the *routes* (a busy deep check, an unreadable log file the
    operator explicitly asked for) — :func:`collect` itself never raises.
    """

    def __init__(self, message: str, status: int = 500) -> None:
        self.status = status
        super().__init__(message)


def _finding(fid: str, level: str, title: str, detail: str, fix: str = "") -> dict:
    return {"id": fid, "level": level, "title": title, "detail": detail, "fix": fix}


def _worst(levels) -> str:
    return max(levels, key=lambda lv: _LEVEL_ORDER.get(lv, 0), default=OK)


def _iso(epoch: float | None) -> str | None:
    if not epoch:
        return None
    with contextlib.suppress(OSError, OverflowError, ValueError):
        return datetime.fromtimestamp(epoch, UTC).isoformat(timespec="seconds")
    return None


def _human_bytes(n: float | None) -> str:
    """Only for finding text — the UI formats numbers itself."""
    if n is None:
        return "unknown"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"                        # pragma: no cover — unreachable


def _human_secs(s: float | None) -> str:
    if s is None:
        return "unknown"
    s = int(s)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h {(s % 3600) // 60}m"
    return f"{s // 86400}d {(s % 86400) // 3600}h"


async def _probe(name: str, coro, timeout: float | None = None) -> tuple[dict, str | None]:
    """Await one section, bounded and swallowing everything.

    Returns ``(data, error)``. A blown budget or an exception becomes the error
    string; the caller turns that into a `fail` finding. Nothing that happens in
    a probe may ever reach the route as an exception.
    """
    budget = PROBE_TIMEOUT_S if timeout is None else timeout
    try:
        return await asyncio.wait_for(coro, timeout=budget), None
    except TimeoutError:
        log.warning("dashboard probe %s timed out after %.0fs", name, budget)
        return {}, f"timed out after {budget:.0f}s"
    except asyncio.CancelledError:              # shutting down — not our error
        raise
    except Exception as e:
        log.warning("dashboard probe %s failed: %s", name, e)
        return {}, f"{type(e).__name__}: {e}"


# --------------------------------------------------------------------------- #
# Process
# --------------------------------------------------------------------------- #

# Import time is close enough to process start for an uptime an operator cares
# about, and unlike /proc/self/stat's starttime it needs no clock-tick maths.
_STARTED_MONOTONIC = time.monotonic()
_STARTED_WALL = time.time()

# (monotonic_ts, cpu_seconds) of the previous sample — CPU% is the delta between
# two collects, so the first call after boot legitimately reports null.
_cpu_prev: tuple[float, float] | None = None


def _cpu_seconds() -> float | None:
    """Total CPU time this process has used. Portable: /proc first (cheapest),
    then getrusage, which POSIX guarantees."""
    with contextlib.suppress(OSError, ValueError, IndexError):
        raw = Path("/proc/self/stat").read_text()
        # comm can contain spaces AND parens — everything after the LAST ')'
        # is the stable part of the record.
        fields = raw[raw.rindex(")") + 1:].split()
        ticks = os.sysconf("SC_CLK_TCK") or 100
        return (int(fields[11]) + int(fields[12])) / ticks     # utime + stime
    with contextlib.suppress(Exception):
        import resource
        ru = resource.getrusage(resource.RUSAGE_SELF)
        return float(ru.ru_utime + ru.ru_stime)
    return None


def _rss_bytes() -> tuple[int | None, bool]:
    """(bytes, is_peak). /proc gives current RSS; the getrusage fallback can
    only offer the peak, and saying which is which matters when someone is
    chasing a leak."""
    with contextlib.suppress(OSError, ValueError, IndexError):
        pages = int(Path("/proc/self/statm").read_text().split()[1])
        return pages * os.sysconf("SC_PAGE_SIZE"), False
    with contextlib.suppress(Exception):
        import resource
        maxrss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # Linux reports KB, macOS/BSD bytes.
        return int(maxrss * (1024 if platform.system() == "Linux" else 1)), True
    return None, False


def _container() -> str | None:
    """Which containerisation we appear to be inside, or None for bare metal.

    Cheap and best-effort: the point is to change the ADVICE (a root user is
    normal in a container, alarming on a Pi), never to gate behaviour.
    """
    if Path("/.dockerenv").exists():
        return "docker"
    if Path("/run/.containerenv").exists():
        return "podman"
    if os.environ.get("KUBERNETES_SERVICE_HOST"):
        return "kubernetes"
    env = (os.environ.get("container") or "").strip()
    if env:
        return env                              # systemd-nspawn, podman, lxc…
    with contextlib.suppress(OSError):
        cgroup = Path("/proc/self/cgroup").read_text()
        for needle, name in (("kubepods", "kubernetes"), ("docker", "docker"),
                             ("containerd", "containerd"), ("lxc", "lxc")):
            if needle in cgroup:
                return name
    return None


# --------------------------------------------------------------------------- #
# Bind — the socket we are ACTUALLY served on
# --------------------------------------------------------------------------- #

# config.SETTINGS.host is what the app was CONFIGURED with, and nothing in the
# app binds it: every launcher hands uvicorn its own --host/--port, so the two
# routinely disagree (a panel reporting 127.0.0.1:8765 while the process was
# bound to :8766 is what prompted this). Two sources, ordered by what they
# actually prove:
#
#   1. The ASGI scope's ``server`` tuple, stashed by the routes on the first
#      dashboard request. That is the LOCAL address of the connection we are
#      answering — proof that this address works, but NOT proof it is the only
#      one: a socket bound to 0.0.0.0 reports 127.0.0.1 for a request that
#      arrived over loopback.
#   2. The kernel's listener table (/proc/net/tcp{,6}, Linux). Any LISTEN row on
#      our port is ours — nothing else can hold it while we are serving on it —
#      so this is what turns "probably loopback" into a verified answer.
#
# When neither can answer we fall back to the configured value AND SAY SO. A
# green "only reachable from this machine" is never emitted from a number nobody
# confirmed; unverified degrades to a warning instead.
_observed_bind: dict = {"host": None, "port": None}

_TCP_LISTEN = "0A"                              # /proc/net/tcp state for LISTEN


def note_bound_socket(server) -> None:
    """Record ``scope["server"]`` from a real request (called by the routes).

    Only an address we can classify is accepted: a unix-socket path or a test
    client's hostname tells us nothing about network reach, and a value we
    cannot reason about is worse than admitting we don't know.
    """
    if not server:
        return
    try:
        host, port = server[0], server[1]
    except (TypeError, IndexError, KeyError):
        return
    try:
        ipaddress.ip_address(str(host).strip("[]"))
    except ValueError:
        return
    with contextlib.suppress(TypeError, ValueError):
        _observed_bind.update(host=str(host), port=int(port) if port else None)


def _hex_to_addr(addr_hex: str, v6: bool) -> str:
    """/proc's address encoding (little-endian per 32-bit word) → printable."""
    raw = bytes.fromhex(addr_hex)
    if not v6:
        return str(ipaddress.IPv4Address(raw[::-1]))
    flat = b"".join(raw[i:i + 4][::-1] for i in range(0, len(raw), 4))
    return str(ipaddress.IPv6Address(flat))


def _parse_listeners(text: str, port: int, v6: bool) -> list[str]:
    """LISTEN rows for `port` out of a /proc/net/tcp{,6} dump → addresses.

    Split out from the file read so it can be tested against a captured dump:
    the format is stable, but a bad parse here would silently mis-report the
    bind, which is the precise failure this whole path exists to prevent.
    """
    out: list[str] = []
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 4 or parts[3] != _TCP_LISTEN:
            continue
        addr_hex, _, port_hex = parts[1].partition(":")
        try:
            if int(port_hex, 16) != port:
                continue
            out.append(_hex_to_addr(addr_hex, v6))
        except ValueError:
            continue                            # malformed row — skip it, quietly
    return out


# The listener table only changes when something rebinds — which means a
# restart — and on a busy server /proc/net/tcp is a long file, so it is read at
# most this often, like every other repeated read on this page.
LISTENERS_TTL_S = 30.0
_listeners_cache: dict = {"ts": 0.0, "port": None, "value": None}


def _listening_addrs(port: int) -> list[str] | None:
    """Every local address LISTENing on `port`, or None if we cannot tell.

    Linux only by design. /proc/net/tcp is per network namespace, so inside a
    container we see the container's own sockets — exactly the scope the
    operator cares about. Anywhere else this returns None and the caller falls
    back to the configured value with a caveat attached.
    """
    now = time.monotonic()
    if (_listeners_cache["port"] == port
            and now - _listeners_cache["ts"] < LISTENERS_TTL_S):
        cached = _listeners_cache["value"]
        return list(cached) if cached is not None else None

    found: list[str] = []
    seen = False
    for name, v6 in (("/proc/net/tcp", False), ("/proc/net/tcp6", True)):
        try:
            text = Path(name).read_text()
        except OSError:
            continue
        seen = True
        with contextlib.suppress(Exception):    # a weird /proc must not be fatal
            found.extend(_parse_listeners(text, port, v6))
    value = found if seen else None
    _listeners_cache.update(ts=now, port=port, value=None if value is None else list(value))
    return value


def _bind_state(*, scan: bool = True) -> dict:
    """Host/port we are served on, plus how much of that we actually know.

    ``scan`` reads /proc, so callers on the event loop pass ``scan=False``; the
    normal path computes this inside ``_process_sync``'s worker thread.
    """
    observed = bool(_observed_bind.get("host"))
    host = _observed_bind.get("host") or config.SETTINGS.host
    port = _observed_bind.get("port") or config.SETTINGS.port
    listeners = _listening_addrs(port) if (scan and observed and port) else None

    if listeners:
        public = sorted({a for a in listeners if _bind_kind(a) != "loopback"})
        if public:
            kind, verified = "reachable", True
            why = f"the kernel reports {', '.join(public)} listening on port {port}"
        else:
            kind, verified = "loopback", True
            why = (f"the only listener on port {port} is "
                   f"{', '.join(sorted(set(listeners)))}")
    elif observed and _bind_kind(host) != "loopback":
        # We answered a request whose LOCAL address was off-loopback, so this
        # box is reachable there. (The converse proves nothing — hence below.)
        kind, verified = "reachable", True
        why = f"a request was served on {host}:{port}"
    else:
        # Configured only. Err towards "you are exposed": a wider configured
        # bind is believed, a loopback one is not treated as proof of anything.
        kind = "reachable" if _bind_kind(config.SETTINGS.host) in ("all", "specific") \
            else "loopback"
        verified = False
        why = ("observed over loopback, but the full bind could not be confirmed"
               if observed else
               "nothing in the app binds this value — the launcher's --host decides")

    caveat = ""
    if not verified:
        caveat = ("observed on loopback, unverified" if observed
                  else "configured, unverified")
    return {
        "host": host, "port": port,
        "source": "observed" if observed else "configured",
        "verified": verified,
        "kind": kind,
        "reachable": kind == "reachable",
        "listeners": sorted(set(listeners)) if listeners else [],
        "configured_host": config.SETTINGS.host,
        "configured_port": config.SETTINGS.port,
        "caveat": caveat,
        "why": why,
    }


def _process_sync() -> dict:
    global _cpu_prev
    now = time.monotonic()
    cpu = _cpu_seconds()
    percent = None
    if cpu is not None and _cpu_prev is not None:
        dt, dcpu = now - _cpu_prev[0], cpu - _cpu_prev[1]
        if dt >= 0.5 and dcpu >= 0:             # too short a gap is just noise
            percent = round(100.0 * dcpu / dt, 1)
    if cpu is not None:
        _cpu_prev = (now, cpu)
    rss, rss_is_peak = _rss_bytes()

    uid = getattr(os, "geteuid", lambda: None)()
    user = None
    if uid is not None:
        with contextlib.suppress(Exception):
            import pwd
            user = pwd.getpwuid(uid).pw_name

    uptime = now - _STARTED_MONOTONIC
    bind = _bind_state()
    return {
        "version": APP_VERSION,
        "python": platform.python_version(),
        "platform": f"{platform.system()} {platform.release()} ({platform.machine()})",
        "container": _container(),
        "pid": os.getpid(),
        "uptime_seconds": int(uptime),
        "started_at": _iso(_STARTED_WALL),
        "rss_bytes": rss,
        "rss_is_peak": rss_is_peak,
        "cpu_percent": percent,
        "cpu_seconds": round(cpu, 2) if cpu is not None else None,
        "uid": uid,
        "user": user,
        "root": uid == 0,
        # Bind + reachability, used by the auth/TLS findings and worth showing
        # plainly: half of "why can't my phone see it" is this line. host/port
        # are the EFFECTIVE values (observed socket when we have one); `bind`
        # carries where they came from and whether anyone confirmed them, and
        # the UI must render that caveat rather than presenting a guess as fact.
        "host": bind["host"],
        "port": bind["port"],
        "bind": bind,
    }


async def _process() -> dict:
    # /proc reads are page-cache hits, but they are still syscalls on a shared
    # loop — and pwd/cgroup can touch NSS. Off-thread, like everything else.
    return await asyncio.to_thread(_process_sync)


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #

# Rolling cache for the blob-store walk, so a page polling every 5s doesn't
# re-walk the media dir every time.
_du_cache: dict = {"ts": 0.0, "value": None}

# In-flight guard for that walk. asyncio.wait_for cancels the AWAIT, never the
# thread behind asyncio.to_thread — and the cache is only written when a walk
# COMPLETES, so on storage slow enough to blow the probe budget every 5s poll
# used to start another walker. Eight of those exhaust the default thread-pool
# executor (8 workers on a Pi 4) and starve every other to_thread in the app.
# One walk at a time; everyone else gets the stale reading, clearly labelled.
_du_lock = threading.Lock()
_du_walking = False


def _du_claim() -> bool:
    """True if the caller now owns the walk; False if one is already running."""
    global _du_walking
    with _du_lock:
        if _du_walking:
            return False
        _du_walking = True
        return True


def _du_release() -> None:
    global _du_walking
    with _du_lock:
        _du_walking = False


def _avatar_snapshots_dir() -> Path:
    """Where avatar_snapshots.py keeps its store, derived the same way it
    does — importing that module here would be a cycle for one path."""
    return config.DATA_DIR / "avatar-snapshots"


def _du_measuring() -> dict:
    """Placeholder for "a walk is running and we have nothing cached yet".

    Sizes are None rather than 0 so nothing downstream mistakes "not measured"
    for "empty" — in particular the cap ratio stays None, which suppresses the
    cap findings instead of asserting you are well under a limit nobody read.
    """
    empty = {"bytes": None, "files": None, "complete": False}
    return {"media": dict(empty), "files": dict(empty), "backups": dict(empty),
            "reactions": dict(empty), "avatar_pool": dict(empty),
            "avatar_snapshots": dict(empty), "blob_bytes": None,
            "complete": False, "blob_complete": False,
            "cached": False, "measuring": True}


def _cap_bytes() -> int:
    """The server-wide blob ceiling.

    Read through config.env(), which is what main.FILES_TOTAL_MAX uses — so the
    legacy LOCAL_CHAT_FILES_TOTAL_MAX name resolves here too. Reading os.environ
    directly meant an install on the legacy name got a dashboard reporting the
    20GB default ("well under cap") while the uploader was enforcing something
    else entirely and 507-ing. A dashboard that reports a cap the uploader
    doesn't enforce is a lie.
    """
    default = 20 * 1024 * 1024 * 1024
    try:
        return int(config.env("FILES_TOTAL_MAX") or default)
    except (TypeError, ValueError):
        return default


def _dir_usage(root: Path, budget: list[int]) -> dict:
    """Bytes + file count under `root`, sharing a global entry budget.

    Never follows directory symlinks (a loop must not be able to hang the
    dashboard), and stops when the shared budget runs out — a truncated answer
    flagged ``complete: False`` beats an unbounded walk.
    """
    total = files = 0
    complete = True
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for entry in it:
                    if budget[0] <= 0:
                        return {"bytes": total, "files": files, "complete": False}
                    budget[0] -= 1
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                            continue
                        total += entry.stat(follow_symlinks=False).st_size
                        files += 1
                    except OSError:
                        complete = False        # vanished mid-walk / unreadable
        except FileNotFoundError:
            continue                            # not created yet — 0 bytes
        except OSError:
            complete = False
    return {"bytes": total, "files": files, "complete": complete}


def _storage_sync(db_path: Path, *, fresh: bool, max_entries: int = DU_MAX_ENTRIES) -> dict:
    data_dir = config.DATA_DIR
    out: dict = {"data_dir": str(data_dir)}

    # Free space. This is the single most useful number on the page: a full
    # disk is what actually takes a self-hosted install down.
    try:
        usage = shutil.disk_usage(data_dir if data_dir.exists() else data_dir.parent)
        out["disk"] = {
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
            "free_ratio": round(usage.free / usage.total, 4) if usage.total else None,
        }
    except OSError as e:
        out["disk"] = {"error": str(e)}

    out["writable"] = os.access(data_dir, os.W_OK | os.X_OK)

    def _size(p: Path) -> int | None:
        with contextlib.suppress(OSError):
            return p.stat().st_size
        return None

    out["db_bytes"] = _size(db_path)
    out["wal_bytes"] = _size(Path(f"{db_path}-wal"))
    out["shm_bytes"] = _size(Path(f"{db_path}-shm"))

    now = time.monotonic()
    cached = _du_cache["value"]
    if not fresh and cached is not None and now - _du_cache["ts"] < DU_TTL_S:
        blobs = dict(cached, cached=True)
    elif not _du_claim():
        # Someone else is already walking these directories. Hand back what we
        # have (labelled stale) rather than piling a second walker onto storage
        # that is evidently slow — see _du_lock.
        blobs = (dict(cached, cached=True, measuring=True) if cached is not None
                 else _du_measuring())
    else:
        try:
            budget = [max_entries]
            # Order matters: the budget is shared and spent in this order, so
            # the two stores the cap governs are measured first and reactions —
            # the biggest and least urgent — absorbs any truncation.
            media = _dir_usage(config.MEDIA_DIR, budget)
            files = _dir_usage(config.FILES_DIR, budget)
            backups = _dir_usage(config.BACKUP_DIR, budget)
            reactions = _dir_usage(config.REACTIONS_DIR, budget)
            # The other two keep-forever stores. Avatar pools burn a pair per
            # thread and keep the spent halves; a thread's avatar snapshot is
            # kept as long as the thread is. Both grow the same way reactions
            # do, and being unmeasured is the same problem: "where did the disk
            # go" was unanswerable from this card.
            avatar_pool = _dir_usage(config.AVATAR_POOL_DIR, budget)
            avatar_snapshots = _dir_usage(_avatar_snapshots_dir(), budget)
            blobs = {
                "media": media, "files": files, "backups": backups,
                "avatar_pool": avatar_pool, "avatar_snapshots": avatar_snapshots,
                # Reaction images are reported SEPARATELY and deliberately left
                # out of blob_bytes: spent one-shots are kept forever by design,
                # so this is the fastest-growing directory on the box and being
                # invisible on the storage card was the whole problem — but the
                # cap main.py enforces governs media/ and files/ only, and
                # folding reactions in would make the dashboard's cap maths
                # disagree with the uploader's.
                "reactions": reactions,
                "blob_bytes": media["bytes"] + files["bytes"],
                "complete": (media["complete"] and files["complete"]
                             and backups["complete"] and reactions["complete"]
                             and avatar_pool["complete"]
                             and avatar_snapshots["complete"]),
                # What the cap findings depend on, specifically: media+files.
                "blob_complete": media["complete"] and files["complete"],
                "cached": False, "measuring": False,
            }
            _du_cache.update(ts=time.monotonic(), value=dict(blobs))
        finally:
            _du_release()
    out.update(blobs)

    cap = _cap_bytes()
    out["cap_bytes"] = cap
    out["cap_enabled"] = cap > 0
    out["cap_ratio"] = (round(out["blob_bytes"] / cap, 4)
                        if cap > 0 and out.get("blob_bytes") is not None else None)
    out["cap_warn_ratio"] = BLOB_WARN_RATIO
    return out


async def _storage(db_path: Path, *, deep: bool) -> dict:
    return await asyncio.to_thread(
        _storage_sync, db_path, fresh=deep,
        max_entries=DU_MAX_ENTRIES_DEEP if deep else DU_MAX_ENTRIES)


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #


async def _pragma(db, name: str):
    cur = await db.db.execute(f"PRAGMA {name};")
    row = await cur.fetchone()
    await cur.fetchall()          # drain, or the statement stays "in progress"
    await cur.close()
    return row[0] if row else None


def _deep_integrity_sync(path: str) -> tuple[bool, str]:
    """Full PRAGMA integrity_check on a SEPARATE connection, off the loop.

    Deliberately not the app's shared aiosqlite connection: that one serialises
    every request behind it, and a full scan of a large DB would stall live chat
    for as long as it takes. WAL lets a second reader in for free.
    """
    conn = sqlite3.connect(path, timeout=5.0)
    try:
        conn.execute("PRAGMA busy_timeout=3000;")
        rows = conn.execute("PRAGMA integrity_check;").fetchall()
    finally:
        conn.close()
    messages = [str(r[0]) for r in rows if r]
    ok = len(messages) == 1 and messages[0].lower() == "ok"
    return ok, "ok" if ok else "; ".join(messages[:5])


# Every SQLite database file starts with this. A snapshot that does not is not
# a database — most often a zero-byte file left behind when VACUUM INTO failed
# part-way (out of disk, killed mid-write).
_SQLITE_MAGIC = b"SQLite format 3\x00"


def _snapshot_usable(path: str, size: int) -> bool:
    """Could this file actually be restored? Size + header only.

    Deliberately NOT an integrity check: this runs over the whole backups folder
    on every poll, so it must stay at two cheap syscalls per file. It exists to
    stop the one failure mode that used to read as perfect health — a failed
    ``VACUUM INTO`` leaves a 0-byte ``chats-<stamp>.db`` behind, which the
    newest-file scan then counted as the current backup while every single
    backup was failing.
    """
    if size <= 0:
        return False
    with contextlib.suppress(OSError), open(path, "rb") as fh:
        return fh.read(len(_SQLITE_MAGIC)) == _SQLITE_MAGIC
    return False                                # unreadable is not restorable


def _backup_state_sync() -> dict:
    """Backup health, read off BACKUP_DIR rather than main's counters.

    Filesystem truth survives a restart — which is exactly when an operator
    wants to know whether the last snapshot worked. `_make_backup` renames a
    snapshot that fails verification to ``*.corrupt``, so a corrupt file NEWER
    than the newest good one means the most recent attempt failed.

    A snapshot only counts once it looks like a database (see
    :func:`_snapshot_usable`). Ones that don't are reported separately as
    ``unusable_count`` — they are neither backups nor `.corrupt` rejects, they
    are writes that died on the way out.
    """
    d = config.BACKUP_DIR
    good: list[tuple[float, str]] = []
    bad: list[tuple[float, str]] = []
    unusable: list[tuple[float, str]] = []
    try:
        with os.scandir(d) as it:
            for entry in it:
                if not entry.is_file(follow_symlinks=False):
                    continue
                if not entry.name.startswith("chats-"):
                    continue
                with contextlib.suppress(OSError):
                    st = entry.stat()
                    ts = st.st_mtime
                    if entry.name.endswith(".corrupt"):
                        bad.append((ts, entry.name))
                    elif _snapshot_usable(entry.path, st.st_size):
                        good.append((ts, entry.name))
                    else:
                        unusable.append((ts, entry.name))
    except FileNotFoundError:
        pass
    except OSError as e:
        return {"error": str(e), "dir": str(d)}

    good.sort()
    bad.sort()
    unusable.sort()
    newest_good = good[-1] if good else None
    newest_bad = bad[-1] if bad else None
    last_ok = None
    if newest_good or newest_bad:
        last_ok = bool(newest_good) and (
            not newest_bad or newest_good[0] >= newest_bad[0])
    attempts = good + bad + unusable
    return {
        "dir": str(d),
        "count": len(good),
        "corrupt_count": len(bad),
        # Files named like a snapshot that are 0 bytes or lack a SQLite header.
        "unusable_count": len(unusable),
        "unusable_newest_at": _iso(unusable[-1][0]) if unusable else None,
        "last_backup_epoch": int(newest_good[0]) if newest_good else None,
        "last_backup_at": _iso(newest_good[0]) if newest_good else None,
        "last_attempt_epoch": int(max(x[0] for x in attempts)) if attempts else None,
        "last_backup_ok": last_ok,
        "interval_seconds": config.SETTINGS.backup_interval,
        "keep": config.SETTINGS.backup_keep,
    }


async def _database(db, *, deep: bool) -> dict:
    out: dict = {"path": str(db.path), "deep": deep}
    out["journal_mode"] = await _pragma(db, "journal_mode")
    page_size = await _pragma(db, "page_size")
    page_count = await _pragma(db, "page_count")
    out["page_size"] = page_size
    out["page_count"] = page_count
    out["logical_bytes"] = (page_size or 0) * (page_count or 0)
    out["freelist_pages"] = await _pragma(db, "freelist_count")
    out["fts_enabled"] = bool(getattr(db, "fts_ok", False))

    if deep:
        # A corrupt file makes sqlite raise rather than answer — that IS the
        # result, so record it as a failed check instead of letting it take the
        # whole section down as a probe error.
        try:
            ok, detail = await asyncio.to_thread(_deep_integrity_sync, str(db.path))
        except Exception as e:
            ok, detail = False, f"{type(e).__name__}: {e}"
        out["check"] = {"kind": "integrity_check", "ok": ok, "detail": detail}
        # Row counts are a full scan on a contentless FTS DB — deep only.
        with contextlib.suppress(Exception):
            cur = await db.db.execute(
                "SELECT (SELECT COUNT(*) FROM threads), (SELECT COUNT(*) FROM messages)")
            row = await cur.fetchone()
            await cur.close()
            out["threads"] = row[0]
            out["messages"] = row[1]
    else:
        ok = await db.integrity_ok()
        out["check"] = {"kind": "quick_check", "ok": bool(ok),
                        "detail": "ok" if ok else "quick_check did not report ok"}

    out["backup"] = await asyncio.to_thread(_backup_state_sync)
    return out


# --------------------------------------------------------------------------- #
# Connections
# --------------------------------------------------------------------------- #


async def _connections() -> dict:
    """Live WebSocket clients, split by access tier — counts ONLY.

    No addresses, no user agents, no tokens, not even a per-connection id: the
    operator needs to know "three devices are watching, one of them is limited",
    and nothing on this page should be able to tell them which family member is
    in the kitchen. The manager's per-connection map is read read-only; if its
    internals ever change we still report the total and say the split is
    unavailable, rather than guessing.
    """
    # Local import: keeps this module import-light and free of a cycle risk.
    from .ws import manager

    out = {"total": int(manager.count), "full": None, "limited": None,
           "tiers_available": False}
    try:
        conns = getattr(manager, "_conns", None)
        is_safe = getattr(manager, "_is_safe", None)
        if isinstance(conns, dict) and callable(is_safe):
            limited = 0
            for meta in list(conns.values()):
                if is_safe(meta):
                    limited += 1
            out["limited"] = limited
            out["full"] = max(0, out["total"] - limited)
            out["tiers_available"] = True
    except Exception as e:
        log.debug("connection tier split unavailable: %s", e)
    return out


# --------------------------------------------------------------------------- #
# Agent backend (optional — the open-source build ships without one)
# --------------------------------------------------------------------------- #

# (resolved_path, mtime, size) -> version string. The binary rarely changes and
# `--version` costs a process spawn, so cache it across polls.
_agent_version_cache: dict[tuple, str] = {}
# The same key -> (expires_at_monotonic, error). FAILURES are cached too, and
# that is the point: a CLI that hangs used to cost a spawn + a 3s timeout + a 1s
# reap on every single 5s poll, so an already-sick box got a process storm from
# the page opened to diagnose it. Short TTL so a fix shows up within a minute.
_agent_version_fail_cache: dict[tuple, tuple[float, str]] = {}


def _remember_version_failure(key: tuple, err: str) -> tuple[None, str]:
    _agent_version_fail_cache[key] = (time.monotonic() + AGENT_VERSION_FAIL_TTL_S, err)
    return None, err


async def _agent_version(path: str, key: tuple) -> tuple[str | None, str | None]:
    if key in _agent_version_cache:
        return _agent_version_cache[key], None
    cached_fail = _agent_version_fail_cache.get(key)
    if cached_fail is not None:
        if time.monotonic() < cached_fail[0]:
            return None, cached_fail[1]
        _agent_version_fail_cache.pop(key, None)
    try:
        proc = await asyncio.create_subprocess_exec(
            path, "--version",                  # fixed argv, never shell=True
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except OSError as e:
        return _remember_version_failure(key, str(e))
    try:
        out, err = await asyncio.wait_for(proc.communicate(),
                                          timeout=AGENT_VERSION_TIMEOUT_S)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        # Bounded reap. Process.wait() does not return until every pipe has hit
        # EOF, so a CLI that left a grandchild holding stdout (a wrapper script
        # exec'ing a daemon — exactly what a stuck agent looks like) would park
        # here for as long as the grandchild lives. One second, then we move on
        # and let the OS clean up.
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), timeout=1.0)
        return _remember_version_failure(
            key, f"--version timed out after {AGENT_VERSION_TIMEOUT_S:.0f}s")
    if proc.returncode != 0:
        detail = (err or b"").decode("utf-8", "replace").strip().splitlines()
        return _remember_version_failure(
            key, detail[0] if detail else f"exited {proc.returncode}")
    text = (out or b"").decode("utf-8", "replace").strip().splitlines()
    version = text[0][:120] if text else None
    if version:
        _agent_version_cache[key] = version
    else:
        # Exit 0 and nothing on stdout: still a spawn we don't want to repeat
        # every poll, so it is cached as a (short-lived) failure like the rest.
        return _remember_version_failure(key, "--version printed nothing")
    return version, None


# Cheap gateway reachability, cached like main's own probe.
_gateway_probe: dict = {"ts": 0.0, "ok": None}


async def _gateway_reachable() -> bool:
    """Is anything listening on the agent gateway's port?

    A bare TCP connect: enough to tell "the gateway is down" from "the gateway
    is up and the turn failed", which is the distinction an operator staring at
    a silent chat actually needs. Mirrors main._gateway_ok() — reimplemented
    here because this module must not import main (see the module docstring).
    """
    now = time.monotonic()
    if _gateway_probe["ok"] is not None and now - _gateway_probe["ts"] < GATEWAY_PROBE_TTL_S:
        return bool(_gateway_probe["ok"])
    ok = False
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(GATEWAY_HOST, GATEWAY_PORT),
            timeout=GATEWAY_PROBE_TIMEOUT_S)
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        ok = True
    except (TimeoutError, OSError):
        ok = False
    except Exception as e:
        log.debug("gateway probe failed: %s", e)
        ok = False
    _gateway_probe.update(ts=now, ok=ok)
    return ok


async def _agent() -> dict:
    """Is the configured agent CLI there and runnable — and is anything home?

    Optional by design: the open-source build has no agent backend at all, and
    that is a supported configuration (chat history, file drop and the
    dashboard itself all work without one). Set OPENCLAW_BIN="" to say so
    explicitly and this stops nagging.

    Two INDEPENDENT answers, reported separately, because they fail separately:
    the CLI on disk, and the gateway it talks to. "Agent CLI available" on its
    own was reassurance handed out for the exact symptom ("nothing replies")
    that a dead gateway produces.
    """
    binary = (config.SETTINGS.openclaw_bin or "").strip()
    out: dict = {"bin": binary, "configured": bool(binary), "optional": True,
                 "path": None, "present": False, "executable": False,
                 "version": None, "version_error": None, "gateway": None,
                 # Bots wired straight to an LLM provider ("Connect an AI").
                 # Reported alongside the CLI because they answer the same
                 # operator question — "can anything in here reply to me?" —
                 # and because "no agent backend" reads very differently on a
                 # box that has three connected providers than on one that has
                 # none. Counted from config.yaml; no keys are ever read here.
                 "api_bots": config.api_bot_count()}
    if not binary:
        return out

    # Same resolution rule as openclaw.cli_available(): absolute paths are
    # checked directly, bare names go through PATH.
    def _resolve() -> tuple[str | None, bool, bool, tuple | None]:
        if os.path.isabs(binary):
            path = binary if os.path.exists(binary) else None
        else:
            path = shutil.which(binary)
        if not path:
            return None, False, False, None
        executable = os.access(path, os.X_OK)
        key = None
        with contextlib.suppress(OSError):
            st = os.stat(path)
            key = (path, st.st_mtime, st.st_size)
        return path, True, executable, key

    path, present, executable, key = await asyncio.to_thread(_resolve)
    out.update(path=path, present=present, executable=executable)
    if path and executable:
        version, err = await _agent_version(path, key or (path,))
        out["version"] = version
        out["version_error"] = err
    out["gateway"] = {
        "host": GATEWAY_HOST, "port": GATEWAY_PORT,
        "reachable": await _gateway_reachable(),
    }
    return out


# --------------------------------------------------------------------------- #
# Clock
# --------------------------------------------------------------------------- #


async def _clock(db) -> dict:
    """Two independent skew signals, both free.

    1. Wall clock vs monotonic since boot: catches a clock that JUMPED while we
       were running (an NTP step, a VM resume, a Pi finally reaching a time
       server after a cold boot with no RTC).
    2. Newest data timestamp vs now: catches a clock that went BACKWARDS. Rows
       dated in the future are the symptom operators actually hit — messages
       sort wrong and the daily thread lands on the wrong day.
    """
    now_wall, now_mono = time.time(), time.monotonic()
    jump = (now_wall - _STARTED_WALL) - (now_mono - _STARTED_MONOTONIC)
    out = {
        "now": _iso(now_wall),
        "timezone": time.strftime("%Z") or None,
        "utc_offset_seconds": -(time.altzone if time.daylight and time.localtime().tm_isdst
                                else time.timezone),
        "jump_seconds": round(jump, 1),
        "future_data_seconds": None,
    }
    newest: float | None = None
    with contextlib.suppress(Exception):
        cur = await db.db.execute("SELECT MAX(created_at) FROM messages")
        row = await cur.fetchone()
        await cur.close()
        if row and row[0]:
            with contextlib.suppress(ValueError, TypeError):
                dt = datetime.fromisoformat(str(row[0]))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=UTC)
                newest = dt.timestamp()
    if newest is not None:
        out["newest_message_at"] = _iso(newest)
        out["future_data_seconds"] = round(max(0.0, newest - now_wall), 1)
    return out


# --------------------------------------------------------------------------- #
# Config sanity checks — the part an operator actually reads
# --------------------------------------------------------------------------- #


def _bind_kind(host: str) -> str:
    h = (host or "").strip().lower()
    if h in ("0.0.0.0", "::", "*", ""):
        return "all"
    if h.startswith("127.") or h in ("localhost", "::1", "[::1]"):
        return "loopback"
    return "specific"


def _tls_declared() -> bool:
    """Has the operator told us a TLS terminator sits in front?

    We cannot detect a reverse proxy from inside the process, so this is an
    explicit declaration rather than a guess: DISPATCH_PUBLIC_URL=https://…
    (which is also what you'd set for links), or DISPATCH_BEHIND_TLS=1. Read
    through config.env(), so the documented DISPATCH_ names work and the legacy
    LOCAL_CHAT_ ones keep working — reading os.environ directly meant the
    documented names did nothing at all here.
    """
    if config.env("PUBLIC_URL").strip().lower().startswith("https://"):
        return True
    return config.env("BEHIND_TLS").strip().lower() not in ("", "0", "false", "no")


def _security_findings(bind: dict | None = None) -> list[dict]:
    # `bind` comes from the process probe (which computed it in a worker
    # thread). Falling back means re-deriving it here WITHOUT the /proc scan —
    # this runs on the event loop.
    out: list[dict] = []
    cfg = auth.load()
    bind = bind or _bind_state(scan=False)
    reachable = bool(bind.get("reachable"))
    verified = bool(bind.get("verified"))
    where = f"{bind.get('host')}:{bind.get('port')}"
    why = bind.get("why") or ""

    # --- no lock at all, on a network-reachable bind ---
    if cfg.pin_set:
        out.append(_finding(
            "auth.lock", OK, "Lock configured",
            "A PIN is set: unlisted bots and media stay behind it, and everything "
            "else is served the limited Safe-Mode view."))
    elif reachable:
        out.append(_finding(
            "auth.no_pin", FAIL, "No PIN set while reachable on the network",
            f"The app answers on {where} ({why}), so anyone who can reach this "
            "machine gets full access — including the harness, file store and "
            "every conversation.",
            "Open Settings → 🔒 Security & PIN and set a PIN. To keep it "
            "machine-local instead, start it with --host 127.0.0.1 (and set "
            "DISPATCH_HOST=127.0.0.1 to match), then restart."))
    elif verified:
        out.append(_finding(
            "auth.no_pin", OK, "No PIN, but only reachable from this machine",
            f"Confirmed loopback-only: {why}. Nothing off this box can connect, "
            "so the open state is contained."))
    else:
        # The dangerous green. config.SETTINGS.host says loopback, but nothing
        # in the app binds it and we could not confirm the live socket — so this
        # degrades to a warning instead of promising containment we can't prove.
        out.append(_finding(
            "auth.no_pin", WARN, "No PIN set, and the bind could not be confirmed",
            f"The configured bind is {bind.get('configured_host')} "
            f"({bind.get('caveat') or 'unverified'}) — but every launcher passes "
            "its own --host to uvicorn, so that is the configuration, not the "
            "live socket. If the real bind is wider, anyone who can reach this "
            "machine has full access.",
            "Set a PIN (Settings → 🔒 Security & PIN) — that verdict does not "
            "depend on the bind at all. To confirm the bind instead, check the "
            f"listening socket: `ss -ltnp | grep {bind.get('port')}`."))

    # --- TLS ---
    # Declared first: a terminator in front settles the question whatever the
    # bind turns out to be.
    if _tls_declared():
        out.append(_finding(
            "net.tls", OK, "TLS terminated in front",
            "A TLS terminator is declared (DISPATCH_PUBLIC_URL / DISPATCH_BEHIND_TLS)."))
    elif reachable:
        out.append(_finding(
            "net.tls", WARN, "Served without TLS",
            "Connections are plain HTTP. On a trusted home LAN or a private "
            "tailnet that is a reasonable trade-off; over anything else your PIN "
            "and every message cross the network in the clear.",
            "Put it behind a TLS terminator (Caddy, nginx, `tailscale serve`), "
            "then set DISPATCH_BEHIND_TLS=1 so this check knows."))
    elif verified:
        out.append(_finding(
            "net.tls", OK, "Not exposed off this machine",
            f"Confirmed loopback-only ({why}), so traffic never leaves this box "
            "and there is nothing to encrypt."))
    else:
        out.append(_finding(
            "net.tls", WARN, "No TLS, and the bind could not be confirmed",
            f"The configured bind is {bind.get('configured_host')}, which would "
            "mean nothing to encrypt — but the live socket could not be read "
            f"({bind.get('caveat') or 'unverified'}). If it is actually on the "
            "network, every message and your PIN cross it in the clear.",
            f"Confirm the bind (`ss -ltnp | grep {bind.get('port')}`). If it is "
            "reachable, put it behind a TLS terminator and set "
            "DISPATCH_BEHIND_TLS=1 so this check knows."))

    # --- default / weak API token ---
    token = (cfg.api_token or "").strip()
    if not token:
        out.append(_finding(
            "auth.api_token", OK, "No remote API token configured",
            "Remote calls to the inbound endpoints are refused outright; on-box "
            "automation on loopback still works."))
    elif token.lower() in _PLACEHOLDER_SECRETS:
        out.append(_finding(
            "auth.api_token_default", FAIL, "API token is still a placeholder",
            "security.yaml carries a well-known example value as api_token. "
            "Anyone on the network can post messages as your assistants.",
            "Edit api_token in security.yaml to a long random string "
            "(`python -c \"import secrets;print(secrets.token_urlsafe(32))\"`)."))
    elif len(token) < _MIN_TOKEN_LEN:
        out.append(_finding(
            "auth.api_token_weak", WARN, "API token is short",
            f"api_token is {len(token)} characters; it is a password for posting "
            "messages into the chat.",
            f"Use at least {_MIN_TOKEN_LEN} random characters."))
    else:
        out.append(_finding(
            "auth.api_token", OK, "API token set", "Remote inbound calls require it."))

    # --- security.yaml permissions ---
    with contextlib.suppress(OSError):
        if auth.SECURITY_PATH.exists():
            mode = stat.S_IMODE(auth.SECURITY_PATH.stat().st_mode)
            if mode & 0o077:
                out.append(_finding(
                    "auth.security_file_mode", WARN, "security.yaml is readable by others",
                    f"{auth.SECURITY_PATH} is mode {oct(mode)}; it holds the PIN hash "
                    "and the API token.",
                    f"chmod 600 {auth.SECURITY_PATH}"))
    return out


def _build_findings(*, process: dict, storage: dict, database: dict, agent: dict,
                    clock: dict, errors: dict[str, str]) -> list[dict]:
    """Turn the gathered facts into the list an operator reads top to bottom.

    Order is deliberate: whatever is most likely to be actionable first. Every
    probe that failed contributes its own `fail`, so a broken check is visible
    as a broken check — never as a silent "all good".
    """
    out: list[dict] = []

    # --- probe failures first: a check that didn't run is not a pass ---
    for name, err in errors.items():
        out.append(_finding(
            f"probe.{name}", FAIL, f"Could not read {name} status", err,
            "This usually means the data directory or /proc is unreadable by the "
            "user running DisPatch. Check the service log for the full error."))

    out.extend(_security_findings(process.get("bind")))

    # --- running as root ---
    if process.get("root"):
        if process.get("container"):
            out.append(_finding(
                "process.root", OK, "Running as root inside a container",
                f"Normal for a {process['container']} image, and the blast radius "
                "stops at the container.",
                "Hardening step if you want it: run the image with a non-root "
                "user and chown the data volume to match."))
        else:
            out.append(_finding(
                "process.root", WARN, "Running as root",
                "DisPatch does not need root. Anything that can talk it into "
                "writing a file — the harness especially — writes as root.",
                "Run the service as a normal user (systemd user unit, or "
                "User= in a system unit) and chown the data directory to them."))
    elif process:
        out.append(_finding(
            "process.root", OK, "Running as an unprivileged user",
            f"uid {process.get('uid')}" + (f" ({process['user']})" if process.get("user") else "")))

    # --- disk ---
    disk = (storage.get("disk") or {})
    free, total = disk.get("free_bytes"), disk.get("total_bytes")
    ratio = disk.get("free_ratio")
    if disk.get("error"):
        out.append(_finding(
            "storage.disk", FAIL, "Cannot read free space", str(disk["error"]),
            "Check that the data directory exists and is readable."))
    elif free is not None:
        where = storage.get("data_dir", "the data directory")
        if free < DISK_FAIL_BYTES or (ratio is not None and ratio < DISK_FAIL_RATIO):
            out.append(_finding(
                "storage.disk_full", FAIL, "Almost out of disk space",
                f"{_human_bytes(free)} free of {_human_bytes(total)} on the filesystem "
                f"holding {where}. Writes are about to start failing — messages, "
                "uploads and backups all stop at zero.",
                "Free space now: delete old snapshots in the backups/ folder, "
                "then use the File Server's wipe control to drop old uploads."))
        elif free < DISK_WARN_BYTES or (ratio is not None and ratio < DISK_WARN_RATIO):
            out.append(_finding(
                "storage.disk_low", WARN, "Disk space is getting low",
                f"{_human_bytes(free)} free of {_human_bytes(total)} on the filesystem "
                f"holding {where}.",
                "Trim old backups (LOCAL_CHAT_BACKUP_KEEP) or old uploads before "
                "it becomes urgent."))
        else:
            out.append(_finding(
                "storage.disk", OK, "Disk space is fine",
                f"{_human_bytes(free)} free of {_human_bytes(total)}."))

    if storage and storage.get("writable") is False:
        out.append(_finding(
            "storage.not_writable", FAIL, "Data directory is not writable",
            f"{storage.get('data_dir')} cannot be written by this process. Nothing "
            "can be saved: no messages, no uploads, no backups.",
            "Fix ownership/permissions on the data directory (chown it to the "
            "user the service runs as), then restart."))

    # --- blob cap ---
    cap_ratio = storage.get("cap_ratio")
    if cap_ratio is not None:
        used, cap = storage.get("blob_bytes"), storage.get("cap_bytes")
        # The walk stops at a shared entry budget. Truncation can only UNDER-
        # count, so "over the cap" stays true when it happens — but "well under
        # the cap" does not, and that is the reassurance that must not be given
        # from numbers we know are short.
        complete = storage.get("blob_complete", storage.get("complete", True)) is not False
        at_least = "at least " if not complete else ""
        if cap_ratio >= 1.0:
            out.append(_finding(
                "storage.cap_reached", FAIL, "Storage cap reached",
                f"Stored images and files total {at_least}{_human_bytes(used)} against "
                f"a cap of {_human_bytes(cap)}. New uploads are being refused.",
                "Delete files from the File Server, or raise the cap with "
                "DISPATCH_FILES_TOTAL_MAX (bytes) and restart."))
        elif cap_ratio >= BLOB_WARN_RATIO:
            out.append(_finding(
                "storage.cap_near", WARN, "Approaching the storage cap",
                f"{at_least}{_human_bytes(used)} of {_human_bytes(cap)} used "
                f"({cap_ratio * 100:.0f}%).",
                "Clear out old uploads, or raise DISPATCH_FILES_TOTAL_MAX."))
        elif complete:
            out.append(_finding(
                "storage.cap", OK, "Storage well under the cap",
                f"{_human_bytes(used)} of {_human_bytes(cap)}."))
        else:
            out.append(_finding(
                "storage.cap", OK, "Storage cap not fully measured",
                f"At least {_human_bytes(used)} of {_human_bytes(cap)} is in use — "
                f"the directory walk stopped at its {DU_MAX_ENTRIES:,}-entry budget, "
                "so the real total is higher and cannot be compared to the cap.",
                "Press Run deep check for an exact measurement."))

    # --- database ---
    check = database.get("check") or {}
    if check:
        if check.get("ok"):
            out.append(_finding(
                "db.integrity", OK, f"Database passed {check.get('kind')}",
                "No structural damage found."))
        else:
            out.append(_finding(
                "db.integrity", FAIL, "Database integrity check failed",
                f"{check.get('kind')}: {check.get('detail')}",
                "Stop the service and restore the newest snapshot from the "
                "backups/ folder (they are plain SQLite files — copy one over "
                "chats.db). Run the deep check again afterwards."))
    journal = (database.get("journal_mode") or "").lower()
    if journal and journal != "wal":
        out.append(_finding(
            "db.journal_mode", WARN, f"Database is in {journal} mode, not WAL",
            "WAL is what keeps reads fast while a write is in flight, and it is "
            "what the online snapshot relies on.",
            "Usually means the data directory is on a filesystem that cannot do "
            "shared memory (some network mounts). Move the data directory to "
            "local storage."))

    # --- backups ---
    backup = database.get("backup") or {}
    interval = backup.get("interval_seconds")
    last_epoch = backup.get("last_backup_epoch")
    uptime = process.get("uptime_seconds") or 0
    if backup.get("error"):
        out.append(_finding(
            "backup.unreadable", WARN, "Cannot read the backups folder",
            str(backup["error"]),
            "Check permissions on the backups/ folder inside the data directory."))
    elif interval is not None and interval <= 0:
        out.append(_finding(
            "backup.disabled", WARN, "Automatic backups are switched off",
            "LOCAL_CHAT_BACKUP_INTERVAL is 0, so no snapshots are being taken. "
            "A corrupt database would mean starting over.",
            "Set LOCAL_CHAT_BACKUP_INTERVAL to a number of seconds (21600 = every "
            "6 hours) and restart — or take your own copies of chats.db."))
    elif last_epoch is None:
        level = WARN if uptime > BACKUP_GRACE_S else OK
        out.append(_finding(
            "backup.never_run", level, "No backup has been taken yet",
            ("The backups/ folder holds no usable snapshot. "
             if backup.get("unusable_count") else
             "The backups/ folder holds no snapshot. ")
            + "The first one is written a minute after startup, then on the "
              "configured interval."
            + ("" if level == WARN else " This install only just started."),
            "If this stays empty, check the log for 'DB backup failed' — it is "
            "almost always a full disk or an unwritable data directory."))
    elif backup.get("last_backup_ok") is False:
        out.append(_finding(
            "backup.failed", FAIL, "The most recent backup failed its check",
            f"The newest snapshot did not verify and was set aside as .corrupt "
            f"({backup.get('corrupt_count')} kept for inspection). The last good "
            f"snapshot is from {backup.get('last_backup_at')}.",
            "A snapshot that fails verification usually means the live database "
            "is damaged — run the deep check on this page."))
    else:
        age = time.time() - last_epoch
        stale_after = (interval or 0) * BACKUP_STALE_FACTOR
        if stale_after and age > stale_after:
            out.append(_finding(
                "backup.stale", WARN, "Backups have stopped running",
                f"The newest snapshot is {_human_secs(age)} old, but they should be "
                f"taken every {_human_secs(interval)}.",
                "Check the log for backup errors — the loop only stops if the "
                "process was restarted or the write failed."))
        else:
            out.append(_finding(
                "backup.ok", OK, "Backups are current",
                f"{backup.get('count')} snapshot(s); newest {_human_secs(age)} old."))

    # A snapshot file that is 0 bytes or has no SQLite header is a write that
    # died on the way out — a failed VACUUM INTO leaves exactly that behind.
    # These do NOT count towards `count`/`last_backup_epoch` above (which is
    # what used to let "Backups are current" sit on top of a folder where every
    # single backup was failing), so they get their own line.
    if not backup.get("error") and backup.get("unusable_count"):
        n = backup["unusable_count"]
        out.append(_finding(
            "backup.bad_snapshot", WARN,
            f"{n} snapshot file(s) are not usable backups",
            f"{n} file(s) in {backup.get('dir')} are named like a snapshot but are "
            "empty or do not start with a SQLite header — the write failed part-way "
            f"(newest {backup.get('unusable_newest_at')}). They are not counted as "
            "backups and cannot be restored from.",
            "Delete them, then check the log for 'DB backup failed' — a snapshot "
            "that dies mid-write is almost always a full disk or an unwritable "
            "data directory. Confirm the newest real snapshot afterwards."))

    # --- direct LLM providers ("Connect an AI") ---
    # Reported BEFORE the CLI findings, because it changes what they mean: with
    # a provider connected, "no agent backend" is a deliberate configuration
    # rather than a gap, and an operator scanning this list should learn that
    # first. Emitted only when there IS one: a checklist line saying "you have
    # no direct providers" on an install that never wanted any is noise, and
    # the `agent.none` branch below already points at the panel in that case.
    api_bots = int(agent.get("api_bots") or 0)
    if api_bots:
        out.append(_finding(
            "agent.api_bots", OK,
            f"{api_bots} bot(s) connected to an LLM provider directly",
            "These reply over the provider's HTTP API and need no agent CLI. "
            "Their credentials live in config.yaml in the data directory, "
            "which is written owner-readable (0600)."))

    # --- agent backend ---
    if not agent.get("configured"):
        out.append(_finding(
            "agent.none", OK, "No agent backend configured",
            "Assistants cannot reply, which is a supported setup: chat history, "
            "uploads and this dashboard all work without one."
            + (" Bots connected through Connect an AI are unaffected — they do "
               "not use the CLI." if api_bots else
               " Use Connect an AI (the 🔌 button) to point a bot straight at "
               "an LLM provider, or install an agent CLI.")))
    elif not agent.get("present"):
        out.append(_finding(
            "agent.missing", WARN, "Agent CLI not found",
            f"'{agent.get('bin')}' is configured but is not on PATH (or does not "
            "exist). Messages will be accepted and stored, but nothing will reply.",
            "Install the agent CLI, or point OPENCLAW_BIN at its absolute path "
            "and restart. Set OPENCLAW_BIN=\"\" if you are running without one."))
    elif not agent.get("executable"):
        out.append(_finding(
            "agent.not_executable", WARN, "Agent CLI is not executable",
            f"{agent.get('path')} exists but this user cannot execute it.",
            f"chmod +x {agent.get('path')} (and check it is owned readably)."))
    else:
        detail = f"{agent.get('path')}"
        if agent.get("version"):
            detail += f" — {agent['version']}"
        elif agent.get("version_error"):
            detail += f" (version unavailable: {agent['version_error']})"
        out.append(_finding("agent.ok", OK, "Agent CLI available", detail))

    # --- agent gateway (a separate thing that fails separately) ---
    gateway = agent.get("gateway") or {}
    if agent.get("configured") and gateway:
        gw = f"{gateway.get('host')}:{gateway.get('port')}"
        if gateway.get("reachable"):
            out.append(_finding(
                "agent.gateway", OK, "Agent gateway is answering",
                f"{gw} accepted a connection, so turns have somewhere to go."))
        else:
            out.append(_finding(
                "agent.gateway", WARN, "Agent gateway is not answering",
                f"Nothing is listening on {gw}. The CLI is installed, but with the "
                "gateway down messages are accepted and stored and nothing ever "
                "replies — which looks exactly like the assistants ignoring you.",
                "Start the agent gateway (for the OpenClaw backend: `systemctl "
                "--user start openclaw-gateway`), then check its own log. If you "
                "run without an agent backend, set OPENCLAW_BIN=\"\" to silence "
                "both this and the CLI checks."))

    # --- clock ---
    future = clock.get("future_data_seconds") or 0
    jump = abs(clock.get("jump_seconds") or 0)
    if future > CLOCK_FAIL_SKEW_S:
        out.append(_finding(
            "time.clock_skew", FAIL, "System clock is behind your own data",
            f"Stored messages are dated up to {_human_secs(future)} in the future, "
            "so the clock has moved backwards. New messages will sort into the "
            "middle of old conversations and daily threads land on the wrong day.",
            "Fix the clock (`timedatectl set-ntp true`, or set the time by hand "
            "on a board with no RTC), then restart DisPatch."))
    elif future > CLOCK_WARN_SKEW_S or jump > CLOCK_WARN_SKEW_S:
        out.append(_finding(
            "time.clock_skew", WARN, "System clock looks unsteady",
            (f"Data is dated {_human_secs(future)} ahead of now. " if future else "")
            + (f"The clock jumped {_human_secs(jump)} since startup. " if jump > CLOCK_WARN_SKEW_S else "")
            + "Small steps are normal right after boot on a board with no battery-backed clock.",
            "Enable time sync (`timedatectl set-ntp true`) so it settles."))
    else:
        out.append(_finding(
            "time.clock", OK, "Clock looks sane",
            f"{clock.get('now')} ({clock.get('timezone') or 'local time'})."))

    return out


def _summarise(status: str, findings: list[dict]) -> str:
    fails = [f for f in findings if f["level"] == FAIL]
    warns = [f for f in findings if f["level"] == WARN]
    if status == FAIL:
        head = fails[0]["title"]
        extra = f" (+{len(fails) - 1} more)" if len(fails) > 1 else ""
        return f"Needs attention: {head}{extra}."
    if status == WARN:
        head = warns[0]["title"]
        extra = f" (+{len(warns) - 1} more)" if len(warns) > 1 else ""
        return f"Running, with something worth a look: {head}{extra}."
    return "Everything checks out."


# --------------------------------------------------------------------------- #
# The entry point
# --------------------------------------------------------------------------- #


async def collect(db, *, deep: bool = False) -> dict:
    """Gather the whole dashboard payload. Never raises.

    ``deep=True`` swaps quick_check for a full integrity_check (on its own
    connection, off the event loop), forces a fresh blob-store walk and adds row
    counts. Everything else is identical, so a page can render one shape.
    """
    started = time.monotonic()
    errors: dict[str, str] = {}
    db_path = getattr(db, "path", config.DB_PATH)
    # The storage/database probes get the long budget on a deep pass; a full
    # integrity_check on a big database is meant to take a while.
    slow = DEEP_PROBE_TIMEOUT_S if deep else None

    # Concurrently: the probes are independent, so the page costs the SLOWEST
    # one, not their sum. (_probe swallows everything, so gather cannot raise
    # and no return_exceptions dance is needed.)
    (process, e_process), (storage, e_storage), (database, e_database), \
        (connections, e_conns), (agent, e_agent), (clock, e_clock) = await asyncio.gather(
            _probe("process", _process()),
            _probe("storage", _storage(db_path, deep=deep), slow),
            _probe("database", _database(db, deep=deep), slow),
            _probe("connections", _connections()),
            _probe("agent", _agent()),
            _probe("clock", _clock(db)),
        )
    for name, err in (("process", e_process), ("storage", e_storage),
                      ("database", e_database), ("connections", e_conns),
                      ("agent", e_agent), ("clock", e_clock)):
        if err:
            errors[name] = err

    try:
        findings = _build_findings(process=process, storage=storage, database=database,
                                   agent=agent, clock=clock, errors=errors)
    except Exception as e:                      # the contract again: never raise
        log.exception("dashboard findings failed to build")
        findings = [_finding("probe.findings", FAIL, "Health checks could not be evaluated",
                             f"{type(e).__name__}: {e}",
                             "This is a bug — please report it with the service log.")]

    status = _worst(f["level"] for f in findings)
    counts = {lv: sum(1 for f in findings if f["level"] == lv) for lv in (OK, WARN, FAIL)}
    return {
        "generated_at": _iso(time.time()),
        "deep": bool(deep),
        "status": status,
        "summary": _summarise(status, findings),
        "counts": counts,
        "took_ms": int((time.monotonic() - started) * 1000),
        "process": process,
        "storage": storage,
        "database": database,
        "connections": connections,
        "agent": agent,
        "clock": clock,
        "findings": findings,
    }


# --------------------------------------------------------------------------- #
# Log tail
# --------------------------------------------------------------------------- #


def log_path() -> Path:
    """Where the app log is expected to live.

    DISPATCH_LOG_FILE wins (the legacy LOCAL_CHAT_LOG_FILE still works — that is
    what config.env() is for); otherwise ``<data>/logs/dispatch.log``. Nothing in
    the app writes there by default — logging goes to stdout, which systemd and
    Docker capture — so "no log file" is a normal answer, not an error, and the
    viewer says where to look instead.
    """
    override = config.env("LOG_FILE").strip()
    return Path(override).expanduser() if override else config.LOG_DIR / "dispatch.log"


def _tail_sync(path: Path, lines: int) -> dict:
    st = path.stat()                            # OSError -> caller maps it
    if not stat.S_ISREG(st.st_mode):
        raise DashboardError("Log path is not a regular file", 409)
    data = b""
    with path.open("rb") as fh:
        pos = st.st_size
        while pos > 0 and data.count(b"\n") <= lines and len(data) < LOG_TAIL_MAX_BYTES:
            step = min(64 * 1024, pos)
            pos -= step
            fh.seek(pos)
            data = fh.read(step) + data
    text = data.decode("utf-8", "replace")
    truncated = pos > 0
    return {
        "path": str(path), "available": True, "reason": None,
        "lines": text.splitlines()[-lines:],
        "size_bytes": st.st_size,
        "modified_at": _iso(st.st_mtime),
        "truncated": truncated,
    }


async def tail_log(lines: int = LOG_TAIL_DEFAULT) -> dict:
    """Last `lines` lines of the app log. Never raises for the ordinary cases —
    a missing or unreadable file comes back as ``available: false`` with a
    reason, because "there is no log file, look at journalctl" is exactly the
    answer the operator needs.
    """
    lines = max(1, min(int(lines), LOG_TAIL_MAX_LINES))
    path = log_path()
    try:
        return await asyncio.to_thread(_tail_sync, path, lines)
    except FileNotFoundError:
        return {"path": str(path), "available": False, "lines": [],
                "reason": "No log file at this path. DisPatch logs to standard "
                          "output by default — use `journalctl --user -u "
                          "local-chat` (systemd) or `docker logs <container>`. "
                          "Set DISPATCH_LOG_FILE to write a file instead."}
    except PermissionError:
        return {"path": str(path), "available": False, "lines": [],
                "reason": "The log file exists but this user cannot read it."}
    except DashboardError as e:
        return {"path": str(path), "available": False, "lines": [], "reason": str(e)}
    except OSError as e:
        return {"path": str(path), "available": False, "lines": [],
                "reason": f"{type(e).__name__}: {e}"}
