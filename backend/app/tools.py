"""Tools — operator-defined pages in the left rail, read from ``<DATA_DIR>/tools.yaml``.

Contract: ``docs/design/2026-09-25-tools-plugins.md``. Three kinds of tool:

  * **builtin** — the five panes that already exist (Harness, StudioForge,
    Emails, Clients, Job Board). The manifest only switches them off:
    ``enabled: false`` makes main's ``*_available()`` false, so their routes
    404 and their feature flag drops out of ``/api/auth/status``. No entry =
    behaviour as before.
  * **static** — a directory served read-only at ``/tools/<id>/``. This is a
    disk-read surface, so it is written around REFUSING, the same way
    ``localview`` is (and it borrows localview's deny list rather than keeping
    a second copy that could drift): the root must resolve to an existing
    directory outside the built-in deny subtrees, every request resolves with
    ``strict=True`` and must stay under the resolved root, dot-components and
    secret-shaped filenames are refused, and every refusal is the same 404.
  * **url** — an external page the client frames. The server only reports
    whether it answers (``status.reachable``).

A static tool may carry a ``refresh`` block: a FIXED argv (never a shell
string) that regenerates the page. It can only come from the file — the write
API refuses to introduce or change one — and only an operator session can run
it.

Frame tickets. A static page is framed under ``sandbox`` WITHOUT
``allow-same-origin``, so it runs as an opaque origin and its own
stylesheets/scripts/fetches carry no cookie. With a PIN set those subresource
requests would land in Safe Mode and the page would render bare — the exact
failure localview solved with tickets. Same cure here: when an operator
session asks for an HTML page, it is redirected to
``/tools/_t/<ticket>/<path>``, a random client-bound, expiring capability
scoped to that one tool's root. Relative links then resolve to sibling ticket
URLs. The client just frames ``/tools/<id>/`` and never needs to know.

This module does not import ``main`` (main mounts this router); main registers
the builtin feature probes via :func:`set_builtin_probes`.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import secrets
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Collection
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx
import yaml
from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from . import auth, config, localview

log = logging.getLogger("local-chat.tools")

router = APIRouter()

COOKIE_NAME = "lc_session"          # must match main.COOKIE_NAME (import cycle)
MANIFEST_NAME = "tools.yaml"

ID_RE = re.compile(r"\A[a-z0-9-]{1,40}\Z")
KINDS = ("static", "url", "builtin")
DEFAULT_ICON = "🧩"
DEFAULT_TIMEOUT_S = 300
MAX_TIMEOUT_S = 3600
MAX_FILE_BYTES = 64 * 1024 * 1024
TAIL_BYTES = 2048

NOT_FOUND = {"detail": "Not found"}
DECOY_BODY = {"detail": "Unlock for full access", "decoy": True}

#: builtin id → (feature key main uses, default rail title, default icon)
BUILTINS: dict[str, tuple[str, str, str]] = {
    "deepseek-harness": ("harness", "DeepSeek Harness", "🐋"),
    "studioforge-panel": ("studioforge", "StudioForge", "🎛️"),
    "mail-panel": ("mail", "Emails", "✉️"),
    "clients-panel": ("practice", "Clients", "👥"),
    # The Job Board (2026-09-25; was a rail bot). Its id is also the id of the
    # hidden `jobboard` bot row its threads and the scout agent route through —
    # the one place a tool id and a bot id coincide, which is why builtins skip
    # the "already a bot id" check below.
    "jobboard": ("jobs", "Job Board", "📋"),
}
FEATURE_TO_ID = {feat: tid for tid, (feat, _t, _i) in BUILTINS.items()}

_COMMON_KEYS = {"id", "title", "icon", "kind", "enabled", "safe"}
_ALLOWED_KEYS = {
    "static": _COMMON_KEYS | {"root", "entry", "refresh"},
    "url": _COMMON_KEYS | {"url"},
    "builtin": {"id", "kind", "enabled"},
}
_REFRESH_KEYS = {"argv", "cwd", "timeout_s"}
#: Keys GET /api/tools adds that are not part of the manifest. PUT drops them so
#: the Settings tab can send back the rows it was given.
_OUTPUT_ONLY_KEYS = {"has_refresh", "builtin_feature", "available"}

#: Env names ending in these are never handed to a refresh process.
_SCRUB_SUFFIXES = ("_KEY", "_TOKEN", "_SECRET", "_PIN", "_PASSWORD", "_PASSWD")
#: ...and these exact names, which carry no secret-shaped suffix but hand the
#: child a live capability: the ssh agent, the X/Wayland display and its
#: cookie, and the desktop session bus.
_SCRUB_NAMES = frozenset({"SSH_AUTH_SOCK", "XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS",
                          "WAYLAND_DISPLAY", "DISPLAY"})

DATA_DIR_MSG = "the app's own data directory is never served"


# --------------------------------------------------------------------------- #
# Model
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Refresh:
    argv: tuple[str, ...]
    cwd: str | None = None
    timeout_s: int = DEFAULT_TIMEOUT_S

    def as_dict(self) -> dict:
        return {"argv": list(self.argv), "cwd": self.cwd, "timeout_s": self.timeout_s}


@dataclass(frozen=True)
class Tool:
    id: str
    kind: str
    title: str = ""
    icon: str = DEFAULT_ICON
    enabled: bool = True
    safe: bool = False
    root: str | None = None
    entry: str = "index.html"
    url: str | None = None
    refresh: Refresh | None = None

    def to_manifest(self) -> dict:
        if self.kind == "builtin":
            return {"id": self.id, "kind": "builtin", "enabled": self.enabled}
        d: dict = {"id": self.id, "title": self.title, "icon": self.icon, "kind": self.kind}
        if self.kind == "static":
            d["root"] = self.root
            d["entry"] = self.entry
        else:
            d["url"] = self.url
        d["enabled"] = self.enabled
        d["safe"] = self.safe
        if self.refresh is not None:
            r = {"argv": list(self.refresh.argv)}
            if self.refresh.cwd:
                r["cwd"] = self.refresh.cwd
            r["timeout_s"] = self.refresh.timeout_s
            d["refresh"] = r
        return d


class ToolValidationError(ValueError):
    def __init__(self, index: int | None, field: str, message: str):
        super().__init__(f"tools[{index}].{field}: {message}")
        self.index = index
        self.field = field
        self.message = message


class ToolNotFound(Exception):
    """Any static-serving refusal. Deliberately carries no reason to the client."""


class RefreshBusy(Exception):
    pass


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #

def _bot_ids() -> set[str]:
    with contextlib.suppress(Exception):
        return {b.id.lower() for b in config.load_bots()}
    return set()


def _in_deny_roots(p: Path) -> bool:
    return any(localview._under(p, d) for d in localview._builtin_deny_roots())


def _in_data_dir(p: Path) -> bool:
    """Is ``p`` the app's data directory or anything under it? The deny roots
    only name the data dir's SECRET files; a root AT the data dir would still
    serve ``files/``, ``media/`` and ``tools.yaml`` — to Safe Mode if ``safe``."""
    data = config.DATA_DIR
    candidates = {data}
    with contextlib.suppress(OSError, RuntimeError, ValueError):
        candidates.add(data.resolve())
    return any(localview._under(p, d) for d in candidates)


def _check_rel(value, index: int, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ToolValidationError(index, field, "must be a relative path inside root")
    v = value.strip()
    if v.startswith("/") or "\\" in v or "\x00" in v:
        raise ToolValidationError(index, field, "must be a relative path inside root")
    parts = [p for p in v.split("/") if p]
    if not parts or any(p in (".", "..") or p.startswith(".") for p in parts):
        raise ToolValidationError(index, field, "may not contain '..' or hidden components")
    return "/".join(parts)


def _check_abs_dir(value, index: int, field: str, check_fs: bool) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ToolValidationError(index, field, "required: an absolute directory path")
    v = value.strip()
    if not v.startswith("/") or "\x00" in v:
        raise ToolValidationError(index, field, "must be an absolute path")
    p = Path(v)
    try:
        r = p.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        raise ToolValidationError(index, field, "cannot be resolved") from None
    if r == Path("/"):
        raise ToolValidationError(index, field, "the whole filesystem is too broad")
    if _in_data_dir(r) or _in_data_dir(p):
        raise ToolValidationError(index, field, DATA_DIR_MSG)
    if _in_deny_roots(r) or _in_deny_roots(p):
        raise ToolValidationError(index, field, "is inside a folder that is never served")
    if check_fs:
        try:
            ok = p.resolve(strict=True).is_dir()
        except (OSError, RuntimeError):
            ok = False
        if not ok:
            raise ToolValidationError(index, field, "is not an existing directory")
    return v


def _check_refresh(value, index: int, check_fs: bool) -> Refresh:
    if not isinstance(value, dict):
        raise ToolValidationError(index, "refresh", "must be a mapping with argv")
    for k in value:
        if k not in _REFRESH_KEYS:
            raise ToolValidationError(index, f"refresh.{k}", "unknown key")
    argv = value.get("argv")
    if (not isinstance(argv, list) or not argv or len(argv) > 64
            or not all(isinstance(a, str) and a and "\x00" not in a for a in argv)):
        raise ToolValidationError(index, "refresh.argv",
                                  "must be a non-empty list of strings (never a shell string)")
    cwd = value.get("cwd")
    if cwd is not None:
        cwd = _check_abs_dir(cwd, index, "refresh.cwd", check_fs)
    t = value.get("timeout_s", DEFAULT_TIMEOUT_S)
    if t is None:
        t = DEFAULT_TIMEOUT_S
    if isinstance(t, bool) or not isinstance(t, int) or not (1 <= t <= MAX_TIMEOUT_S):
        raise ToolValidationError(index, "refresh.timeout_s",
                                  f"must be an integer 1..{MAX_TIMEOUT_S}")
    return Refresh(argv=tuple(argv), cwd=cwd, timeout_s=t)


def _check_bool(row: dict, key: str, default: bool, index: int) -> bool:
    v = row.get(key, default)
    if not isinstance(v, bool):
        raise ToolValidationError(index, key, "must be true or false")
    return v


def validate_tools(rows: list, check_fs: bool | Collection[int] = True) -> list[Tool]:
    """Raw manifest rows → Tools. Raises ToolValidationError(index, field, msg).

    ``check_fs`` — True: every static root / refresh cwd must exist now; False:
    shape only (used when READING the file, so a root that vanished makes that
    one tool 404 instead of emptying the whole rail); a collection of indices:
    check only those rows (PUT checks the rows it adds or changes).
    """
    if not isinstance(rows, list):
        raise ToolValidationError(None, "tools", "must be a list")
    bots = _bot_ids()
    seen: set[str] = set()
    out: list[Tool] = []
    for i, row in enumerate(rows):
        fs = check_fs if isinstance(check_fs, bool) else (i in check_fs)
        if not isinstance(row, dict):
            raise ToolValidationError(i, "tools", "each tool must be a mapping")
        kind = row.get("kind")
        if kind not in KINDS:
            raise ToolValidationError(i, "kind", "must be one of static, url, builtin")
        for k in row:
            if k not in _ALLOWED_KEYS[kind]:
                raise ToolValidationError(i, str(k), f"unknown key for a {kind} tool")
        tid = row.get("id")
        if not isinstance(tid, str) or not ID_RE.match(tid):
            raise ToolValidationError(i, "id", "must match [a-z0-9-]{1,40}")
        if tid in seen:
            raise ToolValidationError(i, "id", f"duplicate id {tid!r}")
        seen.add(tid)
        if kind == "builtin":
            if tid not in BUILTINS:
                raise ToolValidationError(i, "id", "not a builtin id: "
                                          + ", ".join(BUILTINS))
            out.append(Tool(id=tid, kind="builtin", title=BUILTINS[tid][1],
                            icon=BUILTINS[tid][2],
                            enabled=_check_bool(row, "enabled", True, i)))
            continue
        if tid in BUILTINS:
            raise ToolValidationError(i, "id", "is reserved for a builtin")
        if tid in bots:
            raise ToolValidationError(i, "id", "is already a bot id")

        title = row.get("title", tid)
        if not isinstance(title, str) or not title.strip() or len(title) > 80:
            raise ToolValidationError(i, "title", "must be 1..80 characters")
        icon = row.get("icon", DEFAULT_ICON)
        if icon is None:
            icon = DEFAULT_ICON
        if not isinstance(icon, str) or not icon.strip() or len(icon) > 16:
            raise ToolValidationError(i, "icon", "must be a short glyph (1..16 characters)")
        enabled = _check_bool(row, "enabled", True, i)
        safe = _check_bool(row, "safe", False, i)

        if kind == "static":
            root = _check_abs_dir(row.get("root"), i, "root", fs)
            entry = _check_rel(row.get("entry", "index.html") or "index.html", i, "entry")
            refresh = None
            if row.get("refresh") is not None:
                refresh = _check_refresh(row["refresh"], i, fs)
            out.append(Tool(id=tid, kind=kind, title=title.strip(), icon=icon.strip(),
                            enabled=enabled, safe=safe, root=root, entry=entry,
                            refresh=refresh))
        else:
            url = row.get("url")
            ok = False
            if isinstance(url, str) and len(url) <= 2048 and "\x00" not in url:
                u = urlsplit(url.strip())
                ok = u.scheme in ("http", "https") and bool(u.netloc)
            if not ok:
                raise ToolValidationError(i, "url", "must be an http(s) URL")
            out.append(Tool(id=tid, kind=kind, title=title.strip(), icon=icon.strip(),
                            enabled=enabled, safe=safe, url=url.strip()))
    return out


# --------------------------------------------------------------------------- #
# Manifest load / write
# --------------------------------------------------------------------------- #

_lock = threading.Lock()
#: (path, mtime_ns, size, tools) — path is part of the key because DATA_DIR
#: moves under tests.
_cache: tuple[str, int, int, list[Tool]] | None = None


def manifest_path() -> Path:
    return config.DATA_DIR / MANIFEST_NAME


def load_tools() -> list[Tool]:
    """The manifest, cached by mtime. Absent → []. Malformed → logged, [] —
    a typo in a hand-edited file must never take the chat app down."""
    global _cache
    path = manifest_path()
    with _lock:
        try:
            st = path.stat()
        except OSError:
            return []
        if (_cache and _cache[0] == str(path) and _cache[1] == st.st_mtime_ns
                and _cache[2] == st.st_size):
            return _cache[3]
        try:
            doc = yaml.safe_load(path.read_text())
            if doc is None:
                doc = {}
            if not isinstance(doc, dict):
                raise ValueError(f"expected a mapping, got {type(doc).__name__}")
            for k in doc:
                if k != "tools":
                    raise ToolValidationError(None, str(k), "unknown top-level key")
            result = validate_tools(doc.get("tools") or [], check_fs=False)
        except (yaml.YAMLError, OSError, ValueError) as e:
            log.error("tools.yaml cannot be used (%s) — no tools until it is fixed: %s",
                      e, path)
            result = []
        _cache = (str(path), st.st_mtime_ns, st.st_size, result)
        return result


def write_tools(tool_list: list[Tool]) -> None:
    """Atomic 0600 write: a torn file reads as malformed = no tools."""
    doc = {"tools": [t.to_manifest() for t in tool_list]}
    text = yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)
    path = manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{MANIFEST_NAME}.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    global _cache
    with _lock:
        _cache = None


def get_tool(tool_id: str) -> Tool | None:
    return next((t for t in load_tools() if t.id == tool_id), None)


def builtin_enabled(tool_id: str) -> bool | None:
    """The manifest's switch for a builtin: None = no entry (behave as before)."""
    t = get_tool(tool_id)
    if t is None or t.kind != "builtin":
        return None
    return t.enabled


#: builtin id → probe for "the feature exists on this install" (env flag,
#: config, PIN) WITHOUT the manifest switch. Registered by main.
_builtin_probes: dict[str, Callable[[], bool]] = {}


def set_builtin_probes(probes: dict[str, Callable[[], bool]]) -> None:
    _builtin_probes.clear()
    _builtin_probes.update(probes)


def _builtin_available(tool_id: str) -> bool:
    fn = _builtin_probes.get(tool_id)
    if fn is None:
        return False
    try:
        return bool(fn())
    except Exception:
        log.exception("builtin probe %s failed", tool_id)
        return False


def builtin_resolved(tool_id: str) -> bool:
    return _builtin_available(tool_id) and builtin_enabled(tool_id) is not False


def _reset_state() -> None:
    """Tests: drop every cache and the in-memory refresh/ticket bookkeeping."""
    global _cache
    with _lock:
        _cache = None
    with _ticket_lock:
        _tickets.clear()
    _last_refresh.clear()
    _probe_cache.clear()


# --------------------------------------------------------------------------- #
# Static resolution — the whole security decision, in one place
# --------------------------------------------------------------------------- #

def resolve_static(tool: Tool, rel: str) -> Path:
    """``rel`` under the tool's root → the resolved path. ToolNotFound for any
    refusal, one exception type so the route cannot leak which rule fired."""
    if tool.kind != "static" or not tool.root:
        raise ToolNotFound
    rel = rel or ""
    if "\x00" in rel or "\\" in rel or rel.startswith("/"):
        raise ToolNotFound
    parts = [p for p in rel.split("/") if p]
    # Lexical refusal BEFORE resolve(): `..` is never normalised away under
    # the hidden/deny checks, and `.x` is refused as asked for (a symlink
    # named `.x` is still hidden).
    if any(p in (".", "..") or p.startswith(".") for p in parts):
        raise ToolNotFound
    try:
        root = Path(tool.root).resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        raise ToolNotFound from None
    if (root == Path("/") or not root.is_dir() or _in_deny_roots(root)
            or _in_data_dir(root)):
        raise ToolNotFound       # a hand-edited tools.yaml is read with check_fs=False
    target = root.joinpath(*parts) if parts else root
    if localview._pattern_denied(root, target):
        raise ToolNotFound
    try:
        resolved = target.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        raise ToolNotFound from None
    if not localview._under(resolved, root):
        raise ToolNotFound                       # symlink escape
    if localview._hidden_below(root, resolved) or localview._pattern_denied(root, resolved):
        raise ToolNotFound
    if _in_deny_roots(resolved) or _in_data_dir(resolved):
        raise ToolNotFound
    return resolved


def _file_for(tool: Tool, rel: str) -> Path | str:
    """Resolve and pick the file to send; a directory without a trailing slash
    returns the string "redirect". Raises ToolNotFound."""
    if rel == "":
        resolved = resolve_static(tool, tool.entry)
    else:
        resolved = resolve_static(tool, rel)
    if resolved.is_dir():
        if rel and not rel.endswith("/"):
            return "redirect"
        base = rel.strip("/")
        resolved = resolve_static(tool, f"{base}/index.html" if base else "index.html")
    if not resolved.is_file():
        raise ToolNotFound
    try:
        if resolved.stat().st_size > MAX_FILE_BYTES:
            raise ToolNotFound
    except OSError:
        raise ToolNotFound from None
    return resolved


def _send(resolved: Path, *, cors: bool = True):
    ctype = localview.content_type(resolved.name)
    # Same as the Local Viewer: never in a shared cache, always revalidated
    # (a refresh rewrites the page in place). main's header middleware sets
    # the same value on every /tools/ response, including refusals.
    headers = {"Cache-Control": CACHE_CONTROL}
    if cors:
        # The framed page is an opaque origin; without this its own fetch() of a
        # sibling data file is a CORS failure. `*` never admits credentials, so
        # a foreign page gains nothing a cookieless request would not.
        headers["Access-Control-Allow-Origin"] = "*"
    if ctype == localview.OCTET:
        safe = resolved.name.replace('"', "")
        headers["Content-Disposition"] = f'attachment; filename="{safe}"'
    return FileResponse(resolved, media_type=ctype, headers=headers)


# --------------------------------------------------------------------------- #
# Frame tickets (see module docstring)
# --------------------------------------------------------------------------- #

CACHE_CONTROL = "private, max-age=0, must-revalidate"

TICKET_PREFIX = "/tools/_t/"     # main's auth gate lets this through
TICKET_TTL_S = 2 * 3600
TICKET_CAP = 200

_ticket_lock = threading.Lock()
_tickets: dict[str, tuple[str, str, float]] = {}    # tok → (tool_id, client, expires)


def _mint_ticket(tool_id: str, client: str) -> str:
    now = time.monotonic()
    with _ticket_lock:
        for k in [k for k, v in _tickets.items() if v[2] <= now]:
            del _tickets[k]
        while len(_tickets) >= TICKET_CAP:
            del _tickets[next(iter(_tickets))]
        tok = secrets.token_urlsafe(24)
        _tickets[tok] = (tool_id, client, now + TICKET_TTL_S)
    return tok


def _use_ticket(tok: str, client: str) -> str | None:
    now = time.monotonic()
    with _ticket_lock:
        t = _tickets.get(tok)
        if t is None:
            return None
        if t[2] <= now:
            del _tickets[tok]
            return None
        if t[1] != client:
            return None
        _tickets[tok] = (t[0], t[1], now + TICKET_TTL_S)
        return t[0]


# --------------------------------------------------------------------------- #
# Caller tier
# --------------------------------------------------------------------------- #

def _client_of(request: Request) -> str:
    return getattr(getattr(request, "client", None), "host", "") or "?"


def _session(request: Request):
    return getattr(request.state, "session", None) or auth.get_session(
        request.cookies.get(COOKIE_NAME))


def _is_operator(request: Request) -> bool:
    """A real operator: a live PIN session, or anyone when no PIN exists (the
    whole app is open then). Re-derived here — not trusted from the middleware
    alone — so the routes fail closed on a bare app. A machine (API-key /
    loopback) caller is never the operator: tools are a browser feature."""
    if getattr(request.state, "decoy", False) or getattr(request.state, "machine", False):
        return False
    if _session(request) is not None:
        return True
    return not auth.load().pin_set


def _require_operator(request: Request) -> None:
    if not _is_operator(request):
        raise HTTPException(403, "Unlock for full access")


def _limited_visible(t: Tool) -> bool:
    return t.kind in ("static", "url") and t.safe and t.enabled


# --------------------------------------------------------------------------- #
# Output shaping
# --------------------------------------------------------------------------- #

def _out_operator(t: Tool) -> dict:
    d = {"id": t.id, "title": t.title, "icon": t.icon, "kind": t.kind,
         "enabled": t.enabled, "safe": t.safe, "has_refresh": t.refresh is not None}
    if t.kind == "static":
        d["root"] = t.root
        d["entry"] = t.entry
        if t.refresh is not None:
            d["refresh"] = t.refresh.as_dict()
    elif t.kind == "url":
        d["url"] = t.url
    return d


def _out_limited(t: Tool) -> dict:
    d = {"id": t.id, "title": t.title, "icon": t.icon, "kind": t.kind,
         "enabled": t.enabled, "safe": t.safe, "has_refresh": False}
    if t.kind == "static":
        d["entry"] = t.entry
    else:
        d["url"] = t.url
    return d


def _builtin_row(tid: str) -> dict:
    feat, title, icon = BUILTINS[tid]
    return {"id": tid, "title": title, "icon": icon, "kind": "builtin",
            # `enabled` is the manifest SWITCH (what a Settings save round-trips),
            # never the resolved value — otherwise saving while a feature is
            # merely unavailable would persist `enabled: false` into tools.yaml.
            "enabled": builtin_enabled(tid) is not False, "safe": False,
            "has_refresh": False, "builtin_feature": feat,
            "available": _builtin_available(tid)}


def list_out(operator: bool) -> list[dict]:
    manifest = load_tools()
    if not operator:
        return [_out_limited(t) for t in manifest if _limited_visible(t)]
    rows = [_builtin_row(tid) for tid in BUILTINS]
    rows += [_out_operator(t) for t in manifest if t.kind != "builtin"]
    return rows


# --------------------------------------------------------------------------- #
# Refresh runner
# --------------------------------------------------------------------------- #

_locks_guard = threading.Lock()
_refresh_locks: dict[str, threading.Lock] = {}
_last_refresh: dict[str, dict] = {}


def _tool_lock(tool_id: str) -> threading.Lock:
    with _locks_guard:
        lk = _refresh_locks.get(tool_id)
        if lk is None:
            lk = _refresh_locks[tool_id] = threading.Lock()
        return lk


def is_refreshing(tool_id: str) -> bool:
    return _tool_lock(tool_id).locked()


def scrubbed_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items()
            if not k.upper().endswith(_SCRUB_SUFFIXES) and k.upper() not in _SCRUB_NAMES}


def _tail(b: bytes | None) -> str:
    return (b or b"")[-TAIL_BYTES:].decode("utf-8", "replace")


def run_refresh(tool: Tool) -> dict:
    """Run the tool's fixed argv. Raises RefreshBusy when one is in flight.

    shell=False, own process group (so a timeout kills `uv run`'s children
    too, not just the launcher), stdin closed, env scrubbed of credentials.
    """
    assert tool.refresh is not None
    lk = _tool_lock(tool.id)
    if not lk.acquire(blocking=False):
        raise RefreshBusy
    try:
        r = tool.refresh
        cwd = r.cwd or tool.root
        t0 = time.monotonic()
        out = err = b""
        try:
            proc = subprocess.Popen(
                list(r.argv), cwd=cwd, env=scrubbed_env(), shell=False,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                start_new_session=True)
        except FileNotFoundError as e:
            rc, err = 127, str(e).encode()
        except (PermissionError, NotADirectoryError, OSError) as e:
            rc, err = 126, str(e).encode()
        else:
            try:
                out, err = proc.communicate(timeout=r.timeout_s)
                rc = proc.returncode
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(proc.pid, signal.SIGKILL)
                out, err = proc.communicate()
                err = (err or b"") + f"\n[timed out after {r.timeout_s}s]".encode()
                rc = 124
        seconds = round(time.monotonic() - t0, 3)
        _last_refresh[tool.id] = {"at": time.time(), "rc": rc, "seconds": seconds}
        if rc != 0:
            log.warning("tool %s refresh exited rc=%s after %.1fs", tool.id, rc, seconds)
        return {"rc": rc, "seconds": seconds,
                "stdout_tail": _tail(out), "stderr_tail": _tail(err)}
    finally:
        lk.release()


# --------------------------------------------------------------------------- #
# URL reachability
# --------------------------------------------------------------------------- #

PROBE_TTL_S = 10.0
PROBE_OP_TIMEOUT_S = 3.0      # httpx: per connect/read/write/pool operation
PROBE_TOTAL_S = 4.0           # the whole probe, whatever the server does
_probe_cache: dict[str, tuple[float, bool]] = {}


async def _probe_once(url: str) -> bool:
    async with httpx.AsyncClient(timeout=httpx.Timeout(PROBE_OP_TIMEOUT_S),
                                 follow_redirects=False) as c:
        r = await c.head(url)
        if r.status_code == 405:
            # HEAD not allowed: the server answered, but confirm with a GET whose
            # body is NEVER read — the stream is closed as soon as the status
            # line and headers are in.
            async with c.stream("GET", url) as g:
                _ = g.status_code
        return True


async def _probe_url(url: str) -> bool:
    """Does anything answer at ``url``? Never reads a body, never follows a
    redirect, and bounded in TOTAL time: httpx's timeout is per operation, so a
    server dribbling headers could otherwise hold a status call open."""
    try:
        return await asyncio.wait_for(_probe_once(url), PROBE_TOTAL_S)
    except Exception:
        return False


async def _reachable(url: str) -> bool:
    now = time.monotonic()
    hit = _probe_cache.get(url)
    if hit and now - hit[0] < PROBE_TTL_S:
        return hit[1]
    ok = await _probe_url(url)
    _probe_cache[url] = (now, ok)
    return ok


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #

def _err422(e: ToolValidationError) -> JSONResponse:
    return JSONResponse({"detail": str(e), "index": e.index, "field": e.field,
                         "message": e.message}, status_code=422)


@router.get("/api/tools")
def api_tools(request: Request):
    operator = _is_operator(request)
    body: dict = {"tools": list_out(operator)}
    if operator:
        body["path"] = str(manifest_path())
    return body


@router.put("/api/tools")
def api_tools_put(request: Request, payload: dict = Body(...)):
    _require_operator(request)
    rows = payload.get("tools") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return _err422(ToolValidationError(None, "tools", "body must be {tools: [...]}"))
    current = {t.id: t for t in load_tools()}
    cleaned: list = []
    fs_rows: set[int] = set()
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            cleaned.append(row)            # validate_tools names it
            continue
        row = {k: v for k, v in row.items() if k not in _OUTPUT_ONLY_KEYS}
        if row.get("kind") == "builtin":
            row = {k: row[k] for k in ("id", "kind", "enabled") if k in row}
        tid = row.get("id")
        stored = current.get(tid) if isinstance(tid, str) else None
        # The UI can never introduce or change a refresh argv: sent = must equal
        # what the file already holds; omitted/null = keep what the file holds.
        if row.get("refresh") is not None:
            if stored is None or stored.refresh is None or not _same_refresh(
                    row["refresh"], stored.refresh):
                return _err422(ToolValidationError(
                    i, "refresh", "refresh can only be set by editing tools.yaml"))
            row["refresh"] = stored.refresh.as_dict()
        else:
            row.pop("refresh", None)
            if (stored is not None and stored.refresh is not None
                    and row.get("kind") == "static"):
                row["refresh"] = stored.refresh.as_dict()
        if (stored is None or stored.kind != row.get("kind")
                or (row.get("kind") == "static" and row.get("root") != stored.root)):
            fs_rows.add(i)
        cleaned.append(row)
    try:
        validated = validate_tools(cleaned, check_fs=fs_rows)
    except ToolValidationError as e:
        return _err422(e)
    write_tools(validated)
    return {"tools": list_out(True), "path": str(manifest_path())}


def _same_refresh(sent, stored: Refresh) -> bool:
    if not isinstance(sent, dict) or set(sent) - _REFRESH_KEYS:
        return False
    argv = sent.get("argv")
    t = sent.get("timeout_s")
    return (isinstance(argv, list) and tuple(argv) == stored.argv
            and (sent.get("cwd") or None) == stored.cwd
            and (DEFAULT_TIMEOUT_S if t is None else t) == stored.timeout_s)


@router.get("/api/tools/{tool_id}/status")
async def api_tool_status(request: Request, tool_id: str):
    operator = _is_operator(request)
    t = get_tool(tool_id)
    if t is None and tool_id in BUILTINS:
        t = Tool(id=tool_id, kind="builtin")
    if not operator:
        if t is None or not _limited_visible(t):
            return JSONResponse(DECOY_BODY, status_code=403)
    elif t is None:
        return JSONResponse(NOT_FOUND, status_code=404)
    if t.kind == "builtin":
        return {"id": t.id, "kind": "builtin", "enabled": builtin_resolved(t.id),
                "refreshing": False, "last_refresh": None}
    body: dict = {"id": t.id, "kind": t.kind, "enabled": t.enabled,
                  "refreshing": is_refreshing(t.id) if operator else False,
                  "last_refresh": _last_refresh.get(t.id) if operator else None}
    if t.kind == "static":
        mtime = None
        with contextlib.suppress(ToolNotFound, OSError):
            mtime = resolve_static(t, t.entry).stat().st_mtime
        body["mtime"] = mtime
    else:
        body["reachable"] = await _reachable(t.url or "")
    return body


@router.post("/api/tools/{tool_id}/refresh")
def api_tool_refresh(request: Request, tool_id: str):
    _require_operator(request)
    t = get_tool(tool_id)
    if t is None or t.kind != "static" or t.refresh is None or not t.enabled:
        raise HTTPException(404, "No such tool, or it has no refresh")
    try:
        return run_refresh(t)
    except RefreshBusy:
        raise HTTPException(409, "A refresh is already running for this tool") from None


# Ticket route FIRST: `_t` would otherwise match `{tool_id}` below (ids cannot
# contain `_`, so no real tool can shadow it either).
@router.api_route(TICKET_PREFIX + "{ticket}/{rel:path}", methods=["GET", "HEAD"])
def tool_ticket_file(request: Request, ticket: str, rel: str):
    """The only lock on this prefix — main's gate lets it through."""
    tool_id = _use_ticket(ticket, _client_of(request))
    t = get_tool(tool_id) if tool_id else None
    if t is None or t.kind != "static" or not t.enabled:
        return JSONResponse(NOT_FOUND, status_code=404)
    try:
        f = _file_for(t, rel)
    except ToolNotFound:
        return JSONResponse(NOT_FOUND, status_code=404)
    if f == "redirect":
        return RedirectResponse(request.url.path + "/", status_code=307)
    return _send(f)


def _will_be_framed(request: Request) -> bool:
    """``Sec-Fetch-Dest`` iframe/frame → framed. Absent (an old browser that
    does not send fetch metadata) → assume framed, the case the UI makes.
    ``document`` or anything else → not framed."""
    dest = request.headers.get("sec-fetch-dest")
    return dest is None or dest.lower() in ("iframe", "frame")


@router.get("/tools/{tool_id}")
def tool_bare(tool_id: str):
    return RedirectResponse(f"/tools/{quote(tool_id)}/", status_code=307)


@router.api_route("/tools/{tool_id}/{rel:path}", methods=["GET", "HEAD"])
def tool_file(request: Request, tool_id: str, rel: str):
    operator = _is_operator(request)
    t = get_tool(tool_id)
    if not operator:
        # Safe Mode: only safe, enabled static tools; everything else answers
        # the same decoy 403 whether or not it exists.
        if t is None or t.kind != "static" or not _limited_visible(t):
            return JSONResponse(DECOY_BODY, status_code=403)
    elif t is None or t.kind != "static" or not t.enabled:
        return JSONResponse(NOT_FOUND, status_code=404)
    try:
        f = _file_for(t, rel)
    except ToolNotFound:
        return JSONResponse(NOT_FOUND, status_code=404)
    if f == "redirect":
        return RedirectResponse(request.url.path + "/", status_code=307)
    # An operator's framed page needs a cookieless capability for its own
    # subresources (see module docstring). Only when a PIN exists — without
    # one nothing is gated and the plain URL already works — and only when the
    # page is being FRAMED: a top-level navigation ("open in new tab") is a
    # same-origin document that sends the cookie itself, so minting a ticket
    # for it would only hand out a capability nobody needs.
    if (operator and localview.viewer_kind(f.name) == "html"
            and _will_be_framed(request)
            and auth.load().pin_set and _session(request) is not None):
        tok = _mint_ticket(t.id, _client_of(request))
        target = rel if rel and not rel.endswith("/") else (
            f"{rel}index.html" if rel else t.entry)
        return RedirectResponse(f"{TICKET_PREFIX}{tok}/{quote(target)}", status_code=307)
    return _send(f)
