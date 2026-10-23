"""Tools — operator-defined pages in the left rail, read from ``<DATA_DIR>/tools.yaml``.

Contract: ``docs/design/2026-09-25-tools-plugins.md`` (apps:
``docs/design/2026-09-25-apps.md``). Four kinds of row:

  * **builtin** — the panes that ship with DisPatch. Two families:
    the four that already existed (Harness, StudioForge, Emails, Clients),
    which the manifest only switches OFF (``enabled: false`` makes main's
    ``*_available()`` false, so their routes 404 and their feature flag drops
    out of ``/api/auth/status``; no entry = behaviour as before); and the
    rows an EXTENSION registers (``register_builtins``, app/extensions.py —
    the voice add-on's Voice cloning, Built-in voices and Voice lab), which
    are usually OPT-IN: off until a row says ``enabled: true`` (``OPT_IN``;
    no entry = off). Both are listed to operators only. A builtin row whose id nobody provides — the add-on was uninstalled
    — is a DORMANT row: kept verbatim through load and save, never listed,
    logged once. (Until 2.2.0 it invalidated the whole manifest.)
  * **static** — a directory served read-only at ``/tools/<id>/``. This is a
    disk-read surface, so it is written around REFUSING, the same way
    ``localview`` is (and it borrows localview's deny list rather than keeping
    a second copy that could drift): the root must resolve to an existing
    directory outside the built-in deny subtrees, every request resolves with
    ``strict=True`` and must stay under the resolved root, dot-components and
    secret-shaped filenames are refused, and every refusal is the same 404.
  * **url** — an external page the client frames. The server only reports
    whether it answers (``status.reachable``).
  * **app** — a trusted package under ``apps/<id>/`` (see ``apps_loader``).
    Its row carries only ``id, kind, enabled, trusted``: ``enabled: false``
    404s the app, and ``trusted: true`` is what lets a DATA-DIR package load
    at all. ``trusted`` runs code, so like a refresh argv it can only be set
    by editing the file — the write API refuses to introduce or change it.

How a tool opens (``open``, 2026-09-26): ``frame`` (the default) puts the
page in the full-width pane; ``window`` launches it in its own browser tab
instead. It exists because some pages refuse to be embedded at all —
StudioForge's panel sends ``X-Frame-Options: DENY`` and ``frame-ancestors
'none'``, so framed it could only ever draw an empty rectangle — and a click
that opens a working tab beats a pane that explains why it is blank. Allowed
on static and url rows, and on the builtins that ARE a page at an address
(StudioForge, the Harness web UI); the StudioForge builtin defaults to
``window``. Never on an app (our own code behind a message bridge) or on a
native pane (Emails, Clients). ``open`` changes WHERE a page opens, never WHO
may open it: every tier gate below applies unchanged, and a builtin row's
``open_url`` is only ever in the operator listing. A url tool's status also
reports ``framable`` (read off the same two headers the browser obeys) so the
client can offer the tab instead of a blank frame.

A static tool may carry a ``refresh`` block: a FIXED argv (never a shell
string) that regenerates the page. It can only come from the file — the write
API refuses to introduce or change one — and only an operator session can run
it.

Frame tickets. A static page is framed under ``sandbox`` WITHOUT
``allow-same-origin``, so it runs as an opaque origin and its own
stylesheets/scripts/fetches carry no cookie. With a PIN set those subresource
requests would arrive signed out and the page would render bare — the exact
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

from . import auth, config, localview, policy
from .contracts.permissions import Permission

log = logging.getLogger("local-chat.tools")

router = APIRouter()

COOKIE_NAME = "lc_session"          # must match main.COOKIE_NAME (import cycle)
MANIFEST_NAME = "tools.yaml"

ID_RE = re.compile(r"\A[a-z0-9-]{1,40}\Z")
KINDS = ("static", "url", "builtin", "app")
DEFAULT_ICON = "🧩"
DEFAULT_TIMEOUT_S = 300
MAX_TIMEOUT_S = 3600
MAX_FILE_BYTES = 64 * 1024 * 1024
TAIL_BYTES = 2048

NOT_FOUND = {"detail": "Not found"}
FORBIDDEN_BODY = {"detail": "Your profile does not have permission to do that.",
                  "error": {"code": "permission_denied",
                            "message": "Your profile does not have permission to do that."}}

#: builtin id → (feature key main uses, default rail title, default icon).
#: The panes that ship with DisPatch. (The Job Board was a builtin here for
#: one day, 2026-09-25, and is now an APP — apps/jobboard/ — whose switch is
#: a `kind: app` row. The voice tools left with voice in 2.2.0: the voice
#: add-on registers them through register_builtins.)
CORE_BUILTINS: dict[str, tuple[str, str, str]] = {
    "deepseek-harness": ("harness", "DeepSeek Harness", "🐋"),
    "studioforge-panel": ("studioforge", "StudioForge", "🎛️"),
    "mail-panel": ("mail", "Emails", "✉️"),
    "clients-panel": ("practice", "Clients", "👥"),
}
#: The live set: CORE_BUILTINS plus what extensions registered. Mutated in
#: place (other modules hold references to these objects).
BUILTINS: dict[str, tuple[str, str, str]] = dict(CORE_BUILTINS)
FEATURE_TO_ID = {feat: tid for tid, (feat, _t, _i) in BUILTINS.items()}
#: Builtins that are OFF unless the manifest has a row with ``enabled: true``.
#: A missing row (or a missing tools.yaml) means the tool simply is not there.
OPT_IN: set[str] = set()
#: builtin id → the extension that registered it (register_builtins).
_EXT_OWNER: dict[str, str] = {}
#: builtin id → "installed on this box" probe, for extension rows. Kept apart
#: from _builtin_probes so main's set_builtin_probes() never clobbers them.
_ext_probes: dict[str, Callable[[], bool]] = {}
#: Dormant ids already logged (once per id per process).
_dormant_logged: set[str] = set()

#: How a tool opens: in the pane, or launched in its own browser tab.
OPEN_MODES = ("frame", "window")
#: Builtins that are a page at an address, so a tab is a real alternative to
#: the pane. Emails needs a one-time launch key its pane fetches, Clients is
#: native DOM — neither has anything to put in a window.
BUILTIN_WINDOWABLE = frozenset({"studioforge-panel", "deepseek-harness"})
#: Builtins whose default is NOT the pane. StudioForge refuses every embed.
BUILTIN_OPEN_DEFAULT = {"studioforge-panel": "window"}

_COMMON_KEYS = {"id", "title", "icon", "kind", "enabled", "safe", "open"}
_ALLOWED_KEYS = {
    "static": _COMMON_KEYS | {"root", "entry", "refresh"},
    "url": _COMMON_KEYS | {"url", "remote_url"},
    "builtin": {"id", "kind", "enabled", "open"},
    "app": {"id", "kind", "enabled", "trusted"},
}
#: What GET /api/tools says about an app that the PACKAGE owns (app.yaml). The
#: Settings tab sends rows back as it got them; these are dropped from a PUT
#: when they still say what the package says, and refused when they do not —
#: a title is changed in app.yaml, not through this API.
_APP_MIRROR_KEYS = ("title", "icon", "order", "has_refresh", "bot_id",
                    "thread_hook", "entry", "hook_version", "mounted")
_REFRESH_KEYS = {"argv", "cwd", "timeout_s"}
#: Keys GET /api/tools adds that are not part of the manifest. PUT drops them so
#: the Settings tab can send back the rows it was given.
_OUTPUT_ONLY_KEYS = {"has_refresh", "builtin_feature", "available",
                     "open_url", "open_remote_url"}

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
    #: url tools only: where a browser that is NOT on the host frames the page
    #: (e.g. a Tailscale Serve HTTPS port in front of a loopback service).
    remote_url: str | None = None
    refresh: Refresh | None = None
    trusted: bool = False
    #: "frame" (the pane) or "window" (its own browser tab). See module doc.
    open: str = "frame"

    def default_open(self) -> str:
        return default_open(self.kind, self.id)

    def to_manifest(self) -> dict:
        if self.kind == "builtin":
            d = {"id": self.id, "kind": "builtin", "enabled": self.enabled}
            if self.open != self.default_open():
                d["open"] = self.open
            return d
        if self.kind == "app":
            d = {"id": self.id, "kind": "app", "enabled": self.enabled}
            if self.trusted:
                d["trusted"] = True
            return d
        d: dict = {"id": self.id, "title": self.title, "icon": self.icon, "kind": self.kind}
        if self.kind == "static":
            d["root"] = self.root
            d["entry"] = self.entry
        else:
            d["url"] = self.url
            if self.remote_url:
                d["remote_url"] = self.remote_url
        d["enabled"] = self.enabled
        if self.safe:                            # legacy 3.x flag: inert, kept only as data
            d["safe"] = True
        if self.open != self.default_open():     # a default is never written
            d["open"] = self.open
        if self.refresh is not None:
            r = {"argv": list(self.refresh.argv)}
            if self.refresh.cwd:
                r["cwd"] = self.refresh.cwd
            r["timeout_s"] = self.refresh.timeout_s
            d["refresh"] = r
        return d


def default_open(kind: str, tool_id: str) -> str:
    if kind == "builtin":
        return BUILTIN_OPEN_DEFAULT.get(tool_id, "frame")
    return "frame"


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


def _app_ids() -> set[str]:
    """Ids of every app package the loader knows (lazy import: apps_loader
    imports this module)."""
    with contextlib.suppress(Exception):
        from . import apps_loader
        return apps_loader.app_ids()
    return set()


def _in_deny_roots(p: Path) -> bool:
    return any(localview._under(p, d) for d in localview._builtin_deny_roots())


def _in_data_dir(p: Path) -> bool:
    """Is ``p`` the app's data directory or anything under it? The deny roots
    only name the data dir's SECRET files; a root AT the data dir would still
    serve ``files/``, ``media/`` and ``tools.yaml`` to anyone the tool is listed to."""
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
    # POSIX paths start with "/"; on Windows a drive path (C:/site or C:\site) is absolute too.
    if "\x00" in v or not (v.startswith("/") or (os.name == "nt" and Path(v).is_absolute())):
        raise ToolValidationError(index, field, "must be an absolute path")
    p = Path(v)
    try:
        r = p.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        raise ToolValidationError(index, field, "cannot be resolved") from None
    if r == Path(r.anchor or "/"):
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


def _http_url_ok(url) -> bool:
    """An http(s) URL with a host, bounded, no NUL — what a frame may load."""
    if not isinstance(url, str) or len(url) > 2048 or "\x00" in url:
        return False
    u = urlsplit(url.strip())
    return u.scheme in ("http", "https") and bool(u.netloc)


def _check_bool(row: dict, key: str, default: bool, index: int) -> bool:
    v = row.get(key, default)
    if not isinstance(v, bool):
        raise ToolValidationError(index, key, "must be true or false")
    return v


def _check_open(row: dict, kind: str, tid: str, index: int) -> str:
    v = row.get("open")
    if v is None:
        return default_open(kind, tid)
    if not isinstance(v, str) or v not in OPEN_MODES:
        raise ToolValidationError(index, "open", "must be frame or window")
    if v == "window" and kind == "builtin" and tid not in BUILTIN_WINDOWABLE:
        raise ToolValidationError(index, "open",
                                  "this built-in has no page of its own to open in a window")
    return v


def validate_tools(rows: list, check_fs: bool | Collection[int] = True, *,
                   dormant_ok: bool = False) -> list[Tool]:
    """Raw manifest rows → Tools. Raises ToolValidationError(index, field, msg).

    ``dormant_ok`` — a builtin row whose id no core pane and no installed
    extension provides is kept as a DORMANT row instead of refused. True when
    reading the file (and for the stored rows a PUT round-trips), so a
    manifest holding an uninstalled add-on's rows is never "no tools at all";
    False (the default) for anything a caller is introducing.

    ``check_fs`` — True: every static root / refresh cwd must exist now; False:
    shape only (used when READING the file, so a root that vanished makes that
    one tool 404 instead of emptying the whole rail); a collection of indices:
    check only those rows (PUT checks the rows it adds or changes).
    """
    if not isinstance(rows, list):
        raise ToolValidationError(None, "tools", "must be a list")
    bots = _bot_ids()
    apps = _app_ids()
    seen: set[str] = set()
    out: list[Tool] = []
    for i, row in enumerate(rows):
        fs = check_fs if isinstance(check_fs, bool) else (i in check_fs)
        if not isinstance(row, dict):
            raise ToolValidationError(i, "tools", "each tool must be a mapping")
        kind = row.get("kind")
        if kind not in KINDS:
            raise ToolValidationError(i, "kind", "must be one of static, url, builtin, app")
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
            if tid not in BUILTINS and not dormant_ok:
                raise ToolValidationError(i, "id", "not a builtin id: "
                                          + ", ".join(BUILTINS))
            if tid not in BUILTINS:
                # A DORMANT row: an extension provided this tool once and is
                # not installed now. Kept verbatim (it comes back the day the
                # add-on does), never listed, never an error — a manifest
                # holding voice rows must not become "no tools at all" the
                # moment the add-on is uninstalled.
                if tid not in _dormant_logged:
                    _dormant_logged.add(tid)
                    log.info("tool %s: no extension provides it; row kept", tid)
                enabled = _check_bool(row, "enabled", True, i)
                open_ = row.get("open")
                if open_ not in OPEN_MODES:
                    open_ = "frame"
                out.append(Tool(id=tid, kind="builtin", title=tid, icon=DEFAULT_ICON,
                                enabled=enabled, open=open_))
                continue
            out.append(Tool(id=tid, kind="builtin", title=BUILTINS[tid][1],
                            icon=BUILTINS[tid][2],
                            enabled=_check_bool(row, "enabled", True, i),
                            open=_check_open(row, kind, tid, i)))
            continue
        if kind == "app":
            # No bot-id check: an app may share its id with its OWN bot (the
            # loader refuses an app whose id is some other bot's).
            if tid in BUILTINS:
                raise ToolValidationError(i, "id", "is reserved for a builtin")
            out.append(Tool(id=tid, kind="app", title=tid,
                            enabled=_check_bool(row, "enabled", True, i),
                            trusted=_check_bool(row, "trusted", False, i)))
            continue
        if tid in BUILTINS:
            raise ToolValidationError(i, "id", "is reserved for a builtin")
        if tid in apps:
            raise ToolValidationError(i, "id", "is reserved for an app")
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
        open_ = _check_open(row, kind, tid, i)

        if kind == "static":
            root = _check_abs_dir(row.get("root"), i, "root", fs)
            entry = _check_rel(row.get("entry", "index.html") or "index.html", i, "entry")
            refresh = None
            if row.get("refresh") is not None:
                refresh = _check_refresh(row["refresh"], i, fs)
            out.append(Tool(id=tid, kind=kind, title=title.strip(), icon=icon.strip(),
                            enabled=enabled, safe=safe, root=root, entry=entry,
                            refresh=refresh, open=open_))
        else:
            url = row.get("url")
            if not _http_url_ok(url):
                raise ToolValidationError(i, "url", "must be an http(s) URL")
            remote = row.get("remote_url")
            if remote is not None and remote != "" and not _http_url_ok(remote):
                raise ToolValidationError(i, "remote_url", "must be an http(s) URL")
            out.append(Tool(id=tid, kind=kind, title=title.strip(), icon=icon.strip(),
                            enabled=enabled, safe=safe, url=url.strip(),
                            remote_url=remote.strip() if remote else None,
                            open=open_))
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
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
            if doc is None:
                doc = {}
            if not isinstance(doc, dict):
                raise ValueError(f"expected a mapping, got {type(doc).__name__}")
            for k in doc:
                if k not in ("tools", "livetools"):
                    raise ToolValidationError(None, str(k), "unknown top-level key")
            _check_livetools_block(doc.get("livetools"))
            result = validate_tools(doc.get("tools") or [], check_fs=False, dormant_ok=True)
        except (yaml.YAMLError, OSError, ValueError) as e:
            log.error("tools.yaml cannot be used (%s) — no tools until it is fixed: %s",
                      e, path)
            result = []
        _cache = (str(path), st.st_mtime_ns, st.st_size, result)
        return result


#: ((path, mtime_ns, size), enabled) for livetools_enabled(): the advisor asks
#: once per turn, so a stat is all an unchanged file costs.
_live_cache: tuple[tuple[str, int, int], bool] | None = None


def _check_livetools_block(block) -> None:
    if block is None:
        return
    if (not isinstance(block, dict) or set(block) - {"enabled"}
            or not isinstance(block.get("enabled", True), bool)):
        raise ToolValidationError(None, "livetools", "must be {enabled: true|false}")


def _raw_doc() -> dict:
    """tools.yaml as a plain mapping ({} when absent). Raises ValueError when
    the file cannot be read as one."""
    try:
        doc = yaml.safe_load(manifest_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (yaml.YAMLError, OSError) as e:
        raise ValueError(str(e)) from e
    if doc is None:
        return {}
    if not isinstance(doc, dict):
        raise ValueError(f"expected a mapping, got {type(doc).__name__}")
    return doc


def livetools_enabled() -> bool:
    """Is the built-in lookup set (``app/livetools``: web search, weather, ...)
    switched on install-wide? Default True. ``livetools: {enabled: false}`` in
    tools.yaml (Settings -> Plugins) turns it off for every bot at once; the
    advisor must offer no tools while this is False. An unreadable file reads
    as the default (on) -- a typo must not silently strip every bot's tools."""
    global _live_cache
    path = manifest_path()
    try:
        st = path.stat()
    except OSError:
        return True
    key = (str(path), st.st_mtime_ns, st.st_size)
    if _live_cache and _live_cache[0] == key:
        return _live_cache[1]
    try:
        block = _raw_doc().get("livetools")
    except ValueError:
        return True
    on = not (isinstance(block, dict) and block.get("enabled") is False)
    _live_cache = (key, on)
    return on


def set_livetools_enabled(enabled: bool) -> None:
    """Persist the switch, keeping every other key in tools.yaml as it is.
    Raises ValueError when the file is unreadable (never overwrite what we
    could not parse)."""
    doc = _raw_doc()
    doc["livetools"] = {"enabled": bool(enabled)}
    _write_doc(doc)


def write_tools(tool_list: list[Tool]) -> None:
    """Atomic 0600 write: a torn file reads as malformed = no tools."""
    doc: dict = {"tools": [t.to_manifest() for t in tool_list]}
    try:
        block = _raw_doc().get("livetools")
    except ValueError:
        block = None
    if block is not None:
        doc["livetools"] = block
    _write_doc(doc)


def _write_doc(doc: dict) -> None:
    text = yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)
    path = manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{MANIFEST_NAME}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    global _cache, _live_cache
    with _lock:
        _cache = None
        _live_cache = None


def get_tool(tool_id: str) -> Tool | None:
    return next((t for t in load_tools() if t.id == tool_id), None)


def builtin_enabled(tool_id: str) -> bool | None:
    """The manifest's switch for a builtin: None = no entry (behave as before)."""
    t = get_tool(tool_id)
    if t is None or t.kind != "builtin":
        return None
    return t.enabled


def builtin_switch(tool_id: str) -> bool:
    """Is this builtin switched on? No entry means on for the original panes
    (the env flags decide) and OFF for an opt-in tool (``OPT_IN``)."""
    v = builtin_enabled(tool_id)
    if tool_id in OPT_IN:
        return v is True
    return v is not False


def builtin_open(tool_id: str) -> str:
    """How a builtin opens: the manifest's `open`, else its default."""
    t = get_tool(tool_id)
    if t is not None and t.kind == "builtin":
        return t.open
    return default_open("builtin", tool_id)


def _settings():
    """main's SETTINGS (tests swap it there), without importing main: this
    module is imported BY main, so by the time a request runs it is loaded."""
    import sys
    m = sys.modules.get(f"{__package__}.main")
    return getattr(m, "SETTINGS", None) or config.SETTINGS


def builtin_addresses(tool_id: str) -> tuple[str | None, str | None]:
    """(url, remote_url) a window-capable builtin opens in a tab. Nothing
    unless the feature is actually available on this install — the same
    condition under which its own status route answers with these values."""
    if tool_id not in BUILTIN_WINDOWABLE or not _builtin_available(tool_id):
        return None, None
    s = _settings()
    if tool_id == "studioforge-panel":
        return (getattr(s, "studioforge_url", "") or None,
                getattr(s, "studioforge_remote_url", "") or None)
    if tool_id == "deepseek-harness":
        port = getattr(s, "harness_port", None)
        # Loopback on the host; off it only the operator's remote address (the
        # client falls back to the pane, and its note, when there is none).
        return ((f"http://127.0.0.1:{port}/" if port else None),
                getattr(s, "harness_remote_url", "") or None)
    return None, None


#: builtin id → probe for "the feature exists on this install" (env flag,
#: config, PIN) WITHOUT the manifest switch. Registered by main.
_builtin_probes: dict[str, Callable[[], bool]] = {}


def set_builtin_probes(probes: dict[str, Callable[[], bool]]) -> None:
    _builtin_probes.clear()
    _builtin_probes.update(probes)


def builtin_owner(tool_id: str) -> str | None:
    """The extension that registered this builtin, or None (core / unknown)."""
    return _EXT_OWNER.get(tool_id)


def is_dormant(tool: Tool) -> bool:
    return tool.kind == "builtin" and tool.id not in BUILTINS


def register_builtins(ext_id: str, rows) -> None:
    """An extension's tools rows (app/extensions.py ``ctx.register_tools``).
    Rows are read by ATTRIBUTE (id, feature, title, icon, opt_in, probe) —
    the extension builds its own objects and never imports DisPatch."""
    staged = []
    for r in rows:
        tid, feat = str(r.id), str(r.feature)
        if tid in CORE_BUILTINS or _EXT_OWNER.get(tid, ext_id) != ext_id:
            raise ValueError(f"tool id {tid!r} is already taken")
        other = FEATURE_TO_ID.get(feat)
        if other is not None and other != tid:
            raise ValueError(f"tool feature {feat!r} is already {other!r}'s")
        title = str(getattr(r, "title", "") or tid)[:80]
        icon = str(getattr(r, "icon", "") or DEFAULT_ICON)[:16]
        staged.append((tid, feat, title, icon, bool(getattr(r, "opt_in", True)), r.probe))
    for tid, feat, title, icon, opt_in, probe in staged:
        BUILTINS[tid] = (feat, title, icon)
        FEATURE_TO_ID[feat] = tid
        _EXT_OWNER[tid] = ext_id
        _ext_probes[tid] = probe
        if opt_in:
            OPT_IN.add(tid)
        else:
            OPT_IN.discard(tid)
        _dormant_logged.discard(tid)
    _drop_cache()


def unregister_builtins(ext_id: str) -> None:
    """Tests / rollback: forget every row ``ext_id`` registered."""
    for tid in [t for t, owner in _EXT_OWNER.items() if owner == ext_id]:
        feat = BUILTINS.pop(tid, ("",))[0]
        if FEATURE_TO_ID.get(feat) == tid:
            FEATURE_TO_ID.pop(feat, None)
        _EXT_OWNER.pop(tid, None)
        _ext_probes.pop(tid, None)
        OPT_IN.discard(tid)
    _drop_cache()


def _drop_cache() -> None:
    """A registered (or dropped) builtin changes how tools.yaml validates."""
    global _cache
    with _lock:
        _cache = None


def _builtin_available(tool_id: str) -> bool:
    fn = _builtin_probes.get(tool_id) or _ext_probes.get(tool_id)
    if fn is None:
        return False
    try:
        return bool(fn())
    except Exception:
        log.exception("builtin probe %s failed", tool_id)
        return False


def builtin_resolved(tool_id: str) -> bool:
    return _builtin_available(tool_id) and builtin_switch(tool_id)


def _reset_state() -> None:
    """Tests: drop every cache and the in-memory refresh/ticket bookkeeping."""
    global _cache, _live_cache
    with _lock:
        _cache = None
        _live_cache = None
    with _ticket_lock:
        _tickets.clear()
    _last_refresh.clear()
    _probe_cache.clear()
    _frame_cache.clear()


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
    """May this caller CONFIGURE tools and plugins (``manage_plugins``)? Admin
    sessions, or an open install. A machine is never the operator: tools are
    a browser feature. The decision is ``policy``'s - this is only its old name."""
    return policy.of(request).can(Permission.MANAGE_PLUGINS)


def _is_member(request: Request) -> bool:
    """Signed in with the blanket ``use_tools`` (a Member) but not an operator."""
    pol = policy.of(request)
    return (not pol.can(Permission.MANAGE_PLUGINS)) and pol.can(Permission.USE_TOOLS)


def _require_operator(request: Request) -> None:
    policy.of(request).require(Permission.MANAGE_PLUGINS)


def _member_visible(t: Tool) -> bool:
    """What a Member may open: any enabled static page or URL tool the owner added."""
    return t.kind in ("static", "url") and t.enabled


# --------------------------------------------------------------------------- #
# Output shaping
# --------------------------------------------------------------------------- #

def _out_operator(t: Tool) -> dict:
    d = {"id": t.id, "title": t.title, "icon": t.icon, "kind": t.kind,
         "enabled": t.enabled, "has_refresh": t.refresh is not None,
         "open": t.open}
    if t.kind == "static":
        d["root"] = t.root
        d["entry"] = t.entry
        if t.refresh is not None:
            d["refresh"] = t.refresh.as_dict()
    elif t.kind == "url":
        d["url"] = t.url
        d["remote_url"] = t.remote_url
    return d


def _out_limited(t: Tool) -> dict:
    d = {"id": t.id, "title": t.title, "icon": t.icon, "kind": t.kind,
         "enabled": t.enabled, "has_refresh": False, "open": t.open}
    if t.kind == "static":
        d["entry"] = t.entry
    else:
        d["url"] = t.url
        d["remote_url"] = t.remote_url
    return d


def _builtin_row(tid: str) -> dict:
    feat, title, icon = BUILTINS[tid]
    d = {"id": tid, "title": title, "icon": icon, "kind": "builtin",
            # `enabled` is the manifest SWITCH (what a Settings save round-trips),
            # never the resolved value — otherwise saving while a feature is
            # merely unavailable would persist `enabled: false` into tools.yaml.
            "enabled": builtin_switch(tid),
            "has_refresh": False, "builtin_feature": feat,
            "available": _builtin_available(tid),
            "open": builtin_open(tid)}
    if tid in BUILTIN_WINDOWABLE:
        # Operator listing only (builtins are never listed to anyone else), and the
        # same values the feature's own full-session status route returns.
        d["open_url"], d["open_remote_url"] = builtin_addresses(tid)
    return d


def _app_row(la) -> dict:
    m = la.manifest
    d = {"id": m.id, "title": m.title, "icon": m.icon, "kind": "app",
         # The manifest SWITCH, like a builtin row (never a resolved value).
         "enabled": _app_enabled(m.id), "order": m.order,
         "has_refresh": False}
    if m.bot is not None:
        d["bot_id"] = m.bot.id
    d["thread_hook"] = f"/apps/{m.id}/thread.js" if m.thread_hook else None
    d["entry"] = f"/apps/{m.id}/"
    if m.thread_hook:
        from . import apps_loader
        d["hook_version"] = apps_loader.hook_version(la)
    return d


def _app_enabled(app_id: str) -> bool:
    t = get_tool(app_id)
    return t.enabled if t is not None and t.kind == "app" else True


def _mounted_apps() -> list:
    with contextlib.suppress(Exception):
        from . import apps_loader
        return apps_loader.mounted()
    return []


def list_out(operator: bool, member: bool = False, can_app=None) -> list[dict]:
    """Builtins first, then apps by `order` (then id), then static/url tools.

    Three audiences: the operator (everything, with paths and refresh commands), a
    holder of the blanket ``use_tools`` (every enabled page or URL tool, no host paths; the
    apps whose manifest permission they hold) and everyone else (only the apps whose
    manifest permission they hold - Image Studio for ``use_images``; no tools: a tool panel
    is never in a User's or Basic profile's column). A tool's ``safe`` flag is legacy 3.x
    data: kept in tools.yaml, never read."""
    manifest = load_tools()
    apps = _mounted_apps()
    if not operator:
        rows = [_app_row(la) for la in apps
                if _app_enabled(la.manifest.id) and (can_app is None or can_app(la.manifest))]
        if not member:
            return rows
        return rows + [_out_limited(t) for t in manifest if _member_visible(t)]
    rows = [_builtin_row(tid) for tid in BUILTINS]
    rows += [_app_row(la) for la in apps]
    rows += [_out_operator(t) for t in manifest if t.kind not in ("builtin", "app")]
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
#: url → would a browser frame it (None = unknown: unreachable, or a redirect
#: whose target we deliberately do not follow). Filled by the same probe.
_frame_cache: dict[str, bool | None] = {}


def framable(headers) -> bool:
    """Would a browser let another origin put this response in an <iframe>?
    The two headers the browser itself obeys: CSP ``frame-ancestors`` (which
    supersedes X-Frame-Options where both are present — only a bare ``*``
    admits an origin we cannot name in advance) and ``X-Frame-Options``
    (DENY / SAMEORIGIN / ALLOW-FROM all refuse us). Neither → framable, the
    web's default. Same reading as main._framable (StudioForge's pane)."""
    csp = (headers.get("content-security-policy") or "").lower()
    for directive in csp.split(";"):
        parts = directive.split()
        if parts and parts[0] == "frame-ancestors":
            return parts[1:] == ["*"]
    xfo = (headers.get("x-frame-options") or "").strip().lower()
    if xfo in ("deny", "sameorigin") or xfo.startswith("allow-from"):
        return False
    return True


def _frame_verdict(r) -> bool | None:
    return None if 300 <= r.status_code < 400 else framable(r.headers)


async def _probe_once(url: str) -> bool:
    async with httpx.AsyncClient(timeout=httpx.Timeout(PROBE_OP_TIMEOUT_S),
                                 follow_redirects=False) as c:
        r = await c.head(url)
        verdict = _frame_verdict(r)
        if r.status_code == 405:
            # HEAD not allowed: the server answered, but confirm with a GET whose
            # body is NEVER read — the stream is closed as soon as the status
            # line and headers are in. Its headers are the page's own.
            async with c.stream("GET", url) as g:
                verdict = _frame_verdict(g)
        _frame_cache[url] = verdict
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
    _frame_cache.pop(url, None)
    ok = await _probe_url(url)
    _probe_cache[url] = (now, ok)
    if not ok:
        _frame_cache[url] = None
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
    pol = policy.of(request)
    body: dict = {"tools": list_out(
        operator, member=_is_member(request),
        can_app=lambda m: pol.can(m.permission))}
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
    sent_ids: set[str] = set()
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            cleaned.append(row)            # validate_tools names it
            continue
        row = {k: v for k, v in row.items() if k not in _OUTPUT_ONLY_KEYS}
        if isinstance(row.get("id"), str):
            sent_ids.add(row["id"])
        if row.get("kind") == "builtin":
            row = {k: row[k] for k in ("id", "kind", "enabled", "open") if k in row}
            tid = row.get("id")
            stored = current.get(tid) if isinstance(tid, str) else None
            # Only the file (or an installed extension) introduces a builtin:
            # a NEW unknown id from a client is refused, a stored dormant one
            # round-trips untouched.
            if isinstance(tid, str) and tid not in BUILTINS:
                if stored is None or stored.kind != "builtin":
                    return _err422(ToolValidationError(
                        i, "id", "not a builtin id: " + ", ".join(BUILTINS)))
                cleaned.append(stored.to_manifest())
                continue
        if row.get("kind") == "app":
            try:
                row = _clean_app_row(i, row, current.get(row.get("id")))
            except ToolValidationError as e:
                return _err422(e)
            cleaned.append(row)
            continue
        tid = row.get("id")
        stored = current.get(tid) if isinstance(tid, str) else None
        # `safe` is a legacy 3.x key (no Restricted tier in 4.0): GET no longer lists it, so a
        # row sent back without it keeps what the file holds (inert data, round-tripped).
        if "safe" not in row and stored is not None and stored.safe:
            row["safe"] = True
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
    # App rows are never removed through this API (the Settings table has no
    # remove button for them — the package is on disk): a stored app row the
    # client did not send back is kept, `trusted` and all.
    cleaned += [t.to_manifest() for t in current.values()
                if t.kind == "app" and t.id not in sent_ids]
    # Dormant builtin rows are never listed, so the client never sends them
    # back; keep them, or a Settings save would delete the operator's voice
    # rows while the add-on is uninstalled.
    cleaned += [t.to_manifest() for t in current.values()
                if is_dormant(t) and t.id not in sent_ids]
    try:
        validated = validate_tools(cleaned, check_fs=fs_rows, dormant_ok=True)
    except ToolValidationError as e:
        return _err422(e)
    # An opt-in tool switched off is the same as no row; leave none behind,
    # so a Settings save never fills tools.yaml with rows nobody wrote.
    validated = [t for t in validated
                 if not (t.kind == "builtin" and t.id in OPT_IN and not t.enabled)]
    write_tools(validated)
    return {"tools": list_out(True), "path": str(manifest_path())}


def _clean_app_row(i: int, row: dict, stored: Tool | None) -> dict:
    """An app row from the Settings tab → the manifest row to store.

    The package-owned fields GET returned may come back unchanged (dropped);
    a CHANGED one is refused, as is any other key. `trusted` keeps what the
    file holds and may not be introduced or changed here."""
    from . import apps_loader
    tid = row.get("id")
    la = apps_loader.get(tid) if isinstance(tid, str) else None
    if la is None and (stored is None or stored.kind != "app"):
        raise ToolValidationError(i, "id", "no such app (an app is a package on disk)")
    row = {k: v for k, v in row.items() if k != "safe"}     # legacy 3.x key, inert
    if la is not None:
        mirror = _app_row(la)
        for k in _APP_MIRROR_KEYS:
            if k in row:
                if k in mirror and row[k] == mirror[k]:
                    row = {kk: vv for kk, vv in row.items() if kk != k}
                else:
                    raise ToolValidationError(i, k, "is set by the app's app.yaml, not here")
    stored_trusted = bool(stored is not None and stored.kind == "app" and stored.trusted)
    if "trusted" in row and row["trusted"] != stored_trusted:
        raise ToolValidationError(i, "trusted", "trusted can only be set by editing tools.yaml")
    for k in row:
        if k not in _ALLOWED_KEYS["app"]:
            raise ToolValidationError(i, str(k), "unknown key for an app row")
    out = {"id": tid, "kind": "app", "enabled": row.get("enabled", True)}
    if stored_trusted:
        out["trusted"] = True
    return out


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
    member = _is_member(request)
    from . import apps_loader
    la = apps_loader.get(tool_id)
    if la is not None:
        visible = la.mounted and _app_enabled(tool_id) and \
            policy.of(request).can(la.manifest.permission)
        if not operator and not visible:
            return JSONResponse(FORBIDDEN_BODY, status_code=403)
        return {"id": tool_id, "kind": "app", "enabled": _app_enabled(tool_id),
                "mounted": la.mounted}
    t = get_tool(tool_id)
    if t is not None and t.kind == "app":
        # A tools.yaml app row whose package did not load (missing / untrusted).
        if not operator:
            return JSONResponse(FORBIDDEN_BODY, status_code=403)
        return {"id": tool_id, "kind": "app", "enabled": t.enabled, "mounted": False}
    if t is None and tool_id in BUILTINS:
        t = Tool(id=tool_id, kind="builtin")
    if not operator:
        if t is None or not member or not _member_visible(t):
            return JSONResponse(FORBIDDEN_BODY, status_code=403)
    elif t is None:
        return JSONResponse(NOT_FOUND, status_code=404)
    if t.kind == "builtin":
        return {"id": t.id, "kind": "builtin", "enabled": builtin_resolved(t.id),
                "refreshing": False, "last_refresh": None, "open": builtin_open(t.id)}
    body: dict = {"id": t.id, "kind": t.kind, "enabled": t.enabled, "open": t.open,
                  "refreshing": is_refreshing(t.id) if operator else False,
                  "last_refresh": _last_refresh.get(t.id) if operator else None}
    if t.kind == "static":
        mtime = None
        with contextlib.suppress(ToolNotFound, OSError):
            mtime = resolve_static(t, t.entry).stat().st_mtime
        body["mtime"] = mtime
    else:
        body["reachable"] = await _reachable(t.url or "")
        body["framable"] = _frame_cache.get(t.url or "") if body["reachable"] else None
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
@router.api_route(TICKET_PREFIX + "{ticket}/{rel:path}", methods=["GET", "HEAD"],
                  include_in_schema=False)
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


@router.api_route("/tools/{tool_id}/{rel:path}", methods=["GET", "HEAD"],
                  include_in_schema=False)
def tool_file(request: Request, tool_id: str, rel: str):
    operator = _is_operator(request)
    member = _is_member(request)
    t = get_tool(tool_id)
    if not operator:
        # A holder of use_tools: any enabled static tool. Everything else answers the
        # same 403 whether or not it exists.
        if t is None or t.kind != "static" or not member or not _member_visible(t):
            return JSONResponse(FORBIDDEN_BODY, status_code=403)
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
