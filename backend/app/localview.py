"""Local Viewer — serve files, folders and static sites off the host's disk.

An unlocked operator taps a local path in a message and the bytes come back
through DisPatch, so any device that can reach the app can read a report, a log,
a PDF or a whole static site without filesystem access of its own.

This is the widest read surface in the app, so the whole module is written
around REFUSING:

  * **Roots allowlist.** Nothing is served unless it resolves (symlinks
    followed, ``Path.resolve(strict=True)``) to inside a configured root. No
    roots configured = feature OFF, which is the default.
  * **A built-in deny list that no config can override** — the ingest deny roots
    main.py already uses for ``[[media:]]``, plus the gateway env file, systemd
    units, this app's own secrets in DATA_DIR, and a filename pattern list for
    keys/envs/databases.
  * **Dotfiles below a root** are refused unless ``show_hidden``. Above the root
    they are the operator's business (``~/.agent/workspace`` is a legal root).
  * **One refusal message.** A denied path and a hidden path and a path outside
    every root all answer ``Not served by the local viewer`` — the client is not
    told *why*, so the surface cannot be used to probe the disk.

Two things this module deliberately does NOT do:

  * It does not import ``main`` at module level (main mounts this router, so
    that would be a cycle). The two things it borrows from main —
    ``_INGEST_DENY_ROOTS`` and the full-access rule — are imported lazily inside
    a function or re-derived here from ``auth``, the same trade dashboard_routes
    makes and for the same reason: the gate must hold even if the middleware
    chain is not there.
  * It does not join the machine-inbound surface. This is a BROWSER feature: an
    X-API-Key holder gets Safe Mode here, deliberately, because an api_token is
    handed to on-box automation and a GPU rig, not to something that should be
    able to read ``~`` over HTTP.

Config lives at ``<DATA_DIR>/local-viewer.yaml`` and is resolved LAZILY on every
call (never bound at import) because the test suite redirects ``config.DATA_DIR``
per test — a module-level constant would point at the live install.
"""

from __future__ import annotations

import contextlib
import fnmatch
import logging
import mimetypes
import os
import re
import secrets
import stat
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

import yaml
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from . import auth, config

log = logging.getLogger("local-chat.localview")

router = APIRouter()

COOKIE_NAME = "lc_session"          # must match main.COOKIE_NAME (see module docstring)

CONFIG_NAME = "local-viewer.yaml"
LS_CAP = 2000                        # entries per listing before `truncated`
DEFAULT_MAX_TEXT_BYTES = 2_000_000   # in-app text render cap; larger = download

DENIED_DETAIL = "Not served by the local viewer"
OFF_DETAIL = "Local viewer is off — add a root in Settings"
NOT_FOUND_DETAIL = "Not found"


# --------------------------------------------------------------------------- #
# The deny list — the half of the security model config cannot touch
# --------------------------------------------------------------------------- #

#: Filenames that are never served, wherever they live. Matched against every
#: path component BELOW the root, so a directory called `secrets.env` hides its
#: children too.
DENY_PATTERNS = (
    "*.env", ".env*", "*.pem", "*.key", "*.p12", "*.pfx", "id_*",
    "*.kdbx", "*.gpg", "*.asc", "known_hosts", "authorized_keys",
    "*.sqlite*", "*.db", "*.db-*", "*credentials*", "*.netrc", "netrc",
    "*.crt", "*.jks", "*.keystore", "*.ovpn", "*.htpasswd", "*secret*",
)

#: Editor/backup siblings of a denied file are the standard way a secret
#: survives a blacklist (`.env.bak`, `id_rsa~`, `key.pem.orig`). A component is
#: matched as written AND with these suffixes stripped, repeatedly.
_DENY_STRIP_SUFFIXES = (".bak", ".old", ".orig", ".save", ".swp", ".tmp",
                        ".copy", ".backup", ".dist", ".example", "~")
_DENY_STRIP_RE = re.compile(r"\.\d+$")


#: (home, DATA_DIR) → the resolved deny tuple for that pair.
_deny_roots_cache: tuple[tuple[str, str], tuple[Path, ...]] | None = None


def _builtin_deny_roots() -> tuple[Path, ...]:
    """Subtrees that are never served, keyed on (home, DATA_DIR).

    Not frozen at import because DATA_DIR moves under tests (and can be
    re-pointed by env in a container), and a tuple bound at import would then
    guard the wrong directory — the failure mode being "the app serves its own
    security.yaml", which is the one outcome that must not depend on import
    order. Not recomputed per call either: it resolves ~24 paths (≈0.7 ms), and
    `ls` calls resolve() once PER ENTRY, so a 2 000-entry listing paid 1.4 s of
    it on the event loop. Memoised on the only two inputs that can change it.
    """
    global _deny_roots_cache
    home = Path.home()
    data = config.DATA_DIR
    key = (str(home), str(data))
    if _deny_roots_cache is not None and _deny_roots_cache[0] == key:
        return _deny_roots_cache[1]
    roots: list[Path] = []
    # Shared with the [[media:]] ingest guard so the two lists cannot drift.
    # Imported lazily: main imports THIS module to mount the router.
    with contextlib.suppress(Exception):
        from .main import _INGEST_DENY_ROOTS
        roots.extend(_INGEST_DENY_ROOTS)
    roots += [
        home / ".ssh", home / ".gnupg", home / ".config" / "secrets",
        home / ".openclaw" / "secrets", home / ".openclaw" / "agents",
        Path("/etc"), Path("/proc"), Path("/sys"), Path("/dev"),
        home / ".openclaw" / "gateway.systemd.env",
        home / ".config" / "systemd",
        # This app's own state: the PIN hash + api_token, the trusted-device
        # tokens, the recovery code, every message ever sent, and the file that
        # configures this very feature.
        data / "security.yaml", data / "trusted-devices.yaml",
        data / "RECOVERY-CODE.txt", data / "backups", data / CONFIG_NAME,
    ]
    out: list[Path] = []
    for r in roots:
        with contextlib.suppress(OSError, RuntimeError):
            # Non-strict: /etc must be denied whether or not it exists here, and
            # a deny path that is itself a symlink resolves to its target.
            out.append(r.resolve())
        out.append(r)
    result = tuple(dict.fromkeys(out))
    _deny_roots_cache = (key, result)
    return result


def _data_dir_patterns() -> tuple[str, ...]:
    """`chats.db*` — the DB plus its -wal/-shm siblings, which `*.db` misses."""
    return ("chats.db*",)


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

@dataclass
class ViewerConfig:
    roots: list[Path] = field(default_factory=list)          # as written (expanded, unresolved)
    resolved_roots: list[Path] = field(default_factory=list)  # symlinks followed
    deny: list[str] = field(default_factory=list)             # user-supplied prefixes/globs
    show_hidden: bool = False
    max_text_bytes: int = DEFAULT_MAX_TEXT_BYTES

    @property
    def enabled(self) -> bool:
        return bool(self.resolved_roots)


_lock = threading.Lock()
#: (config path, mtime, parsed) — the path is part of the key because the tests
#: (and a re-pointed DATA_DIR) move the file, and a cache keyed on mtime alone
#: would answer one install's question with another's config.
_cache: tuple[str, float, ViewerConfig] | None = None


def _reset_cache() -> None:
    global _cache
    with _lock:
        _cache = None
    with _ticket_lock:
        _tickets.clear()


# --------------------------------------------------------------------------- #
# Frame tickets
# --------------------------------------------------------------------------- #
#
# The viewer frames HTML under `sandbox` WITHOUT allow-same-origin, so the
# framed page runs as an opaque origin — and an opaque origin's subresource
# requests (<link>, <script>, <img>, fetch) carry NO cookie. With a PIN set
# that meant every stylesheet of a framed static site came back 403 from the
# session gate and the page rendered bare. The fix is a capability instead of
# a cookie: the cookie-authenticated `stat` call mints a short-lived random
# ticket scoped to the page's own directory, and the page is framed at
# `/local/view/<ticket>/<name>` where its relative links resolve to sibling
# ticket URLs. The route is let through the session gate and fails closed on
# its own: unknown/expired ticket, a different client, or anything outside
# the scope directory is the same uniform 403 as the rest of the viewer.
#
# Scope = the page's directory, not the whole served root: a hostile HTML
# file can `fetch()` (CORS `*` is set on ticket responses so an opaque origin
# may read them) only its own subtree, never the rest of the home directory.

TICKET_PREFIX = "/local/view/"   # main's session gate lets this through
TICKET_TTL_S = 2 * 3600        # sliding: every use pushes expiry out again
TICKET_CAP = 200               # oldest evicted past this — a bounded table

_ticket_lock = threading.Lock()
_tickets: dict[str, _Ticket] = {}


@dataclass
class _Ticket:
    scope: Path          # resolved directory every request must stay under
    client: str          # request.client.host that minted it
    expires: float


def _mint_ticket(scope: Path, client: str) -> str:
    now = time.monotonic()
    with _ticket_lock:
        for k in [k for k, v in _tickets.items() if v.expires <= now]:
            del _tickets[k]
        while len(_tickets) >= TICKET_CAP:
            del _tickets[next(iter(_tickets))]      # dict is insertion-ordered
        tok = secrets.token_urlsafe(24)
        _tickets[tok] = _Ticket(scope=scope, client=client, expires=now + TICKET_TTL_S)
    return tok


def _use_ticket(tok: str, client: str) -> Path | None:
    """The scope for a live ticket presented by the client that minted it,
    else None. Never raises; the caller turns None into the uniform 403."""
    now = time.monotonic()
    with _ticket_lock:
        t = _tickets.get(tok)
        if t is None:
            return None
        if t.expires <= now:
            del _tickets[tok]
            return None
        if t.client != client:
            return None
        t.expires = now + TICKET_TTL_S
        return t.scope


def config_path() -> Path:
    return config.DATA_DIR / CONFIG_NAME


def _expand(raw: str) -> Path | None:
    """`~`-prefixed or absolute → Path; anything else is not a root."""
    s = str(raw or "").strip()
    if not s:
        return None
    if s == "~" or s.startswith("~/"):
        s = str(Path.home()) + s[1:]
    if not s.startswith("/"):
        return None
    return Path(s)


def _resolve_roots(raw_roots: list[str]) -> tuple[list[Path], list[Path]]:
    written: list[Path] = []
    resolved: list[Path] = []
    for entry in raw_roots:
        p = _expand(entry)
        if p is None:
            log.warning("local viewer: ignoring root %r (not an absolute or ~ path)", entry)
            continue
        try:
            r = p.resolve(strict=True)
        except (OSError, RuntimeError):
            # A root that does not exist is reported by /api/local/roots as
            # exists:false rather than silently dropped — but it serves nothing.
            written.append(p)
            continue
        if not r.is_dir():
            log.warning("local viewer: ignoring root %s (not a directory)", p)
            continue
        written.append(p)
        resolved.append(r)
    return written, resolved


def load() -> ViewerConfig:
    """Parsed local-viewer.yaml, cached by mtime. Malformed → feature OFF.

    Fail-closed for the same reason auth.load() is: the file exists, so somebody
    configured something, and guessing at a broken file is how a read surface
    ends up wider than its owner intended.
    """
    global _cache
    path = config_path()
    with _lock:
        if not path.exists():
            # No file: the env override is the container escape hatch. Not
            # cached — it is one getenv, and caching "absent" would miss the
            # file appearing (the Settings pane writes it).
            raw = config.env("VIEWER_ROOTS", "")
            entries = [e for e in raw.split(os.pathsep) if e.strip()] if raw else []
            written, resolved = _resolve_roots(entries)
            return ViewerConfig(roots=written, resolved_roots=resolved)

        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        if _cache and _cache[0] == str(path) and _cache[1] == mtime:
            return _cache[2]

        try:
            loaded = yaml.safe_load(path.read_text())
            if loaded is None:
                loaded = {}
            if not isinstance(loaded, dict):
                raise yaml.YAMLError(f"expected a mapping, got {type(loaded).__name__}")
        except (yaml.YAMLError, OSError) as e:
            log.error("local-viewer.yaml cannot be read/parsed (%s) — the local "
                      "viewer is OFF until it is fixed or deleted: %s", e, path)
            cfg = ViewerConfig()
            _cache = (str(path), mtime, cfg)
            return cfg

        raw_roots = loaded.get("roots") or []
        if not isinstance(raw_roots, list):
            raw_roots = []
        written, resolved = _resolve_roots([str(r) for r in raw_roots])

        raw_deny = loaded.get("deny") or []
        deny = [str(d) for d in raw_deny] if isinstance(raw_deny, list) else []

        try:
            cap = int(loaded.get("max_text_bytes") or DEFAULT_MAX_TEXT_BYTES)
        except (TypeError, ValueError):
            cap = DEFAULT_MAX_TEXT_BYTES

        cfg = ViewerConfig(roots=written, resolved_roots=resolved, deny=deny,
                           show_hidden=bool(loaded.get("show_hidden", False)),
                           max_text_bytes=max(0, cap))
        _cache = (str(path), mtime, cfg)
        return cfg


def _write_config(cfg_roots: list[str], show_hidden: bool, keep: ViewerConfig) -> None:
    """Atomic 0600 write, mirroring auth._write.

    0600 because the file names directories the operator has chosen to expose;
    atomic because a torn write reads as malformed, and malformed means the
    feature turns itself off mid-session.
    """
    doc: dict = {"roots": cfg_roots, "show_hidden": bool(show_hidden)}
    if keep.deny:
        doc["deny"] = list(keep.deny)
    doc["max_text_bytes"] = int(keep.max_text_bytes)
    text = yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)

    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{CONFIG_NAME}.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
    _reset_cache()


# --------------------------------------------------------------------------- #
# Denial bookkeeping — a refusal nobody can see is a refusal nobody fixes
# --------------------------------------------------------------------------- #

_DENIALS: deque[dict] = deque(maxlen=500)


def note_denied(path: str, client: str, reason: str) -> None:
    log.warning("local viewer refused %s (%s) for %s", path[:300], reason, client)
    _DENIALS.append({"at": time.time(), "path": str(path)[:300],
                     "reason": str(reason)[:80], "client": str(client)[:60]})


def denial_stats(window_s: float = 24 * 3600.0) -> dict:
    cutoff = time.time() - window_s
    recent = [d for d in _DENIALS if d["at"] >= cutoff]
    return {"denials_24h": len(recent), "recent": recent[-10:]}


# --------------------------------------------------------------------------- #
# Classification: extension → viewer kind, extension → content type
# --------------------------------------------------------------------------- #

_IMAGE_EXTS = {"png", "jpg", "jpeg", "gif", "webp", "avif", "bmp", "ico"}
_VIDEO_EXTS = {"mp4", "webm", "mov", "m4v", "ogv", "mkv"}
_AUDIO_EXTS = {"mp3", "m4a", "ogg", "oga", "wav", "flac", "aac", "opus"}
_MARKDOWN_EXTS = {"md", "markdown"}
_HTML_EXTS = {"html", "htm", "xhtml"}
_TEXT_EXTS = {
    "txt", "log", "csv", "tsv", "json", "jsonl", "ndjson", "yaml", "yml",
    "toml", "ini", "cfg", "conf", "xml", "py", "pyi", "js", "mjs", "cjs",
    "ts", "tsx", "jsx", "css", "scss", "sh", "bash", "zsh", "fish", "sql",
    "rs", "go", "c", "h", "cpp", "hpp", "cc", "java", "kt", "swift", "rb",
    "php", "pl", "lua", "r", "m", "diff", "patch", "rst", "tex", "gitignore",
    "dockerfile", "makefile", "service", "desktop", "srt", "vtt",
}

#: Extension → Content-Type. Never sniffed: a `.txt` that happens to start with
#: `<html>` must not become an active document, and a `.css` fetched as a
#: subresource of a framed page must carry its REAL type or the page renders
#: unstyled. Both requirements are met by keying on the extension alone.
_CTYPES: dict[str, str] = {
    "html": "text/html; charset=utf-8",
    "htm": "text/html; charset=utf-8",
    "xhtml": "text/html; charset=utf-8",
    "css": "text/css; charset=utf-8",
    "js": "text/javascript; charset=utf-8",
    "mjs": "text/javascript; charset=utf-8",
    "cjs": "text/javascript; charset=utf-8",
    "json": "application/json; charset=utf-8",
    "jsonl": "application/json; charset=utf-8",
    "svg": "image/svg+xml",
    "pdf": "application/pdf",
    "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "gif": "image/gif", "webp": "image/webp", "avif": "image/avif",
    "bmp": "image/bmp", "ico": "image/x-icon",
    "mp4": "video/mp4", "webm": "video/webm", "mov": "video/quicktime",
    "m4v": "video/x-m4v", "ogv": "video/ogg", "mkv": "video/x-matroska",
    "mp3": "audio/mpeg", "m4a": "audio/mp4", "ogg": "audio/ogg",
    "oga": "audio/ogg", "wav": "audio/wav", "flac": "audio/flac",
    "aac": "audio/aac", "opus": "audio/opus",
}

OCTET = "application/octet-stream"


def _ext(name: str) -> str:
    suffix = Path(name).suffix.lower().lstrip(".")
    if suffix:
        return suffix
    # Extensionless but well-known by NAME (Makefile, Dockerfile) still reads
    # as text; anything else is a download.
    stem = Path(name).name.lower()
    return stem if stem in ("makefile", "dockerfile") else ""


def viewer_kind(name: str, *, is_dir: bool = False) -> str:
    """The single classifier stat/ls report and the client's icon table mirrors."""
    if is_dir:
        return "listing"
    e = _ext(name)
    if e in _IMAGE_EXTS:
        return "image"
    if e == "svg":
        return "svg"
    if e in _VIDEO_EXTS:
        return "video"
    if e in _AUDIO_EXTS:
        return "audio"
    if e == "pdf":
        return "pdf"
    if e in _MARKDOWN_EXTS:
        return "markdown"
    if e in _HTML_EXTS:
        return "html"
    if e in _TEXT_EXTS:
        return "text"
    return "download"


def content_type(name: str) -> str:
    e = _ext(name)
    if e in _CTYPES:
        return _CTYPES[e]
    if e in _TEXT_EXTS or e in _MARKDOWN_EXTS:
        # Markdown, code and logs are served as PLAIN text on purpose: the
        # in-app renderer fetches the bytes and formats them itself, and a
        # text/markdown served inline would be one browser quirk away from
        # rendering as a document in a tab.
        return "text/plain; charset=utf-8"
    guessed, _ = mimetypes.guess_type(name)
    if guessed and guessed.split("/")[0] in ("image", "video", "audio") and e:
        return guessed
    return OCTET


# --------------------------------------------------------------------------- #
# Resolution — the whole security decision, in one place
# --------------------------------------------------------------------------- #

class ViewerError(Exception):
    """Carries the HTTP answer plus the reason, which is logged but never sent."""

    def __init__(self, status: int, detail: str, reason: str = ""):
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.reason = reason or detail


def _off() -> ViewerError:
    return ViewerError(404, OFF_DETAIL, "feature off")


def _denied(reason: str) -> ViewerError:
    return ViewerError(403, DENIED_DETAIL, reason)


def _under(child: Path, parent: Path) -> bool:
    return child == parent or parent in child.parents


def _hidden_below(root: Path, path: Path) -> bool:
    """Any dot-component strictly below `root`?"""
    try:
        rel = path.relative_to(root)
    except ValueError:
        return False
    return any(part.startswith(".") for part in rel.parts)


def _pattern_denied(root: Path, path: Path) -> bool:
    """Filename patterns, applied to every component below the root.

    Below the root only: a root of `~/site` under a home directory called
    `.../id_someone` is the operator's choice, and matching above the root would
    make legitimate roots unusable for reasons nobody could see.
    """
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        parts = (path.name,)
    data = config.DATA_DIR
    patterns = DENY_PATTERNS
    if _under(path, data) or _under(root, data):
        patterns = patterns + _data_dir_patterns()
    for part in parts:
        for low in _deny_forms(part.lower()):
            if any(fnmatch.fnmatch(low, pat.lower()) for pat in patterns):
                return True
    return False


def _deny_forms(low: str) -> list[str]:
    """`x.env.bak.1` → [`x.env.bak.1`, `x.env.bak`, `x.env`] (suffixes peeled)."""
    forms = [low]
    cur = low
    for _ in range(4):
        nxt = _DENY_STRIP_RE.sub("", cur)
        if nxt == cur:
            for suf in _DENY_STRIP_SUFFIXES:
                if cur.endswith(suf) and len(cur) > len(suf):
                    nxt = cur[: -len(suf)]
                    break
        if nxt == cur or not nxt:
            break
        forms.append(nxt)
        cur = nxt
    return forms


def _user_denied(cfg: ViewerConfig, path: Path) -> bool:
    for entry in cfg.deny:
        p = _expand(entry)
        if p is None:
            continue
        s = str(p)
        if any(ch in s for ch in "*?["):
            if fnmatch.fnmatch(str(path), s):
                return True
            continue
        with contextlib.suppress(OSError, RuntimeError):
            if _under(path, p.resolve()):
                return True
        if _under(path, p):
            return True
    return False


def resolve(raw: str, cfg: ViewerConfig | None = None) -> tuple[Path, Path]:
    """The request path → (resolved path, the root it lives under).

    Raises ViewerError for every refusal. Order matters: feature-off first (so a
    disabled install leaks nothing about what exists), then the root allowlist,
    then hidden, then deny — all answering with the same 403 body — and only
    then existence, so a 404 is only ever given for a path the caller was
    entitled to read anyway.
    """
    cfg = cfg or load()
    if not cfg.enabled:
        raise _off()

    text = str(raw or "").strip()
    if not text:
        raise _denied("empty path")
    if "\x00" in text:
        raise _denied("nul byte")
    if text.startswith("file://"):
        text = text[len("file://"):] or "/"

    # `~` expands ONLY as the whole first segment. A directory literally named
    # "~" below a root is an ordinary path component and must keep working.
    if text == "~" or text.startswith("~/"):
        text = str(Path.home()) + text[1:]
    if not text.startswith("/"):
        raise _denied("not absolute")

    requested = Path(text)
    if ".." in requested.parts:
        # resolve() would normalise this away; refusing it outright means the
        # hidden/deny checks below never see a path that was rewritten under
        # them. (Percent-encoded forms arrive already decoded.)
        raise _denied("traversal")

    # Non-strict: the allowlist decides BEFORE existence is consulted, so a
    # path outside every root is a 403 whether or not it exists — a 404 there
    # would be an existence oracle for the whole filesystem.
    try:
        resolved = requested.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        raise _denied("unresolvable")

    root = next((r for r in cfg.resolved_roots if _under(resolved, r)), None)
    if root is None:
        raise _denied("outside every root")

    if not cfg.show_hidden:
        # Checked on BOTH the requested and the resolved path: the requested one
        # is what the operator asked for (a symlink named `.x` is still hidden),
        # the resolved one is what would actually be read.
        requested_root = next(
            (r for r in cfg.roots if _under(requested, r)), None)
        if requested_root is not None and _hidden_below(requested_root, requested):
            raise _denied("hidden component")
        if _hidden_below(root, resolved):
            raise _denied("hidden component")

    for deny_root in _builtin_deny_roots():
        if _under(resolved, deny_root):
            raise _denied(f"built-in deny: {deny_root}")
    if _pattern_denied(root, resolved):
        raise _denied("denied filename pattern")
    if _user_denied(cfg, resolved):
        raise _denied("configured deny entry")

    if not resolved.exists():
        raise ViewerError(404, NOT_FOUND_DETAIL, "not found")

    return resolved, root


def _display(path: Path) -> str:
    """`~`-shortened for the UI; feeds straight back into `resolve`."""
    home = str(Path.home())
    s = str(path)
    if s == home:
        return "~"
    if s.startswith(home + "/"):
        return "~" + s[len(home):]
    return s


def file_url(path: Path) -> str:
    return "/local/file/" + quote(str(path).lstrip("/"))


def _index_of(directory: Path, cfg: ViewerConfig) -> Path | None:
    """The directory's index page — re-run through the SAME gate as any other
    path. `is_file()` follows symlinks, so an `index.html` symlink inside a
    root is otherwise a read primitive for anything the process can open,
    served as text/html; a git clone can legitimately contain one."""
    for n in ("index.html", "index.htm"):
        cand = directory / n
        try:
            if not cand.is_file():
                continue
            resolved, _root = resolve(str(cand), cfg)
        except (ViewerError, OSError, RuntimeError, ValueError):
            continue
        if resolved.is_file():
            return resolved
    return None


def _frame_url(resolved: Path, is_dir: bool, has_index: bool | None, client: str) -> str | None:
    """A ticket URL for anything the viewer would FRAME (an HTML page, or a
    folder with an index) — None for everything else, which keeps the ticket
    table small: only framed content has the cookie problem."""
    if is_dir:
        if not has_index:
            return None
        return f"{TICKET_PREFIX}{_mint_ticket(resolved, client)}/"
    if viewer_kind(resolved.name) != "html":
        return None
    return f"{TICKET_PREFIX}{_mint_ticket(resolved.parent, client)}/{quote(resolved.name)}"


def _stat_body(resolved: Path, cfg: ViewerConfig, client: str = "?") -> dict:
    st = resolved.stat()
    is_dir = stat.S_ISDIR(st.st_mode)
    name = resolved.name or str(resolved)
    kind = "dir" if is_dir else "file"
    has_index = None
    if is_dir:
        has_index = _index_of(resolved, cfg) is not None
    size = 0 if is_dir else st.st_size
    return {
        "frame_url": _frame_url(resolved, is_dir, has_index, client),
        "ok": True,
        "path": _display(resolved),
        "name": name,
        "kind": kind,
        "size": size,
        "mtime": float(st.st_mtime),
        "mime": "inode/directory" if is_dir else content_type(name),
        "ext": "" if is_dir else _ext(name),
        "viewer": viewer_kind(name, is_dir=is_dir),
        "url": file_url(resolved) + ("/" if is_dir else ""),
        "has_index": has_index,
        "text_ok": (not is_dir) and size <= cfg.max_text_bytes,
    }


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #

def _require_full_access(request: Request) -> None:
    """In-handler full-access gate — the second lock on the viewer routes.

    Same rule and same name as ``main._require_full_access``, re-derived here
    rather than imported because main imports this module to mount the router.
    Duplicated deliberately, like dashboard_routes._require_operator: mount this
    router on a bare app with no middleware at all and a sessionless caller is
    still refused.

    One difference from main's version, and it is the point of the feature: an
    authenticated MACHINE caller is NOT allowed through. The api_token belongs
    to on-box crons and a GPU rig; it is not a key to the operator's home
    directory. Browsers only — which is also why these routes are absent from
    ``main._is_inbound``.
    """
    if getattr(request.state, "decoy", False):
        raise HTTPException(403, "Unlock for full access")
    session = getattr(request.state, "session", None) or auth.get_session(
        request.cookies.get(COOKIE_NAME))
    if session is not None:
        return
    if auth.load().pin_set:
        raise HTTPException(403, "Unlock for full access")


def _roots_body(cfg: ViewerConfig) -> list[dict]:
    """One row per CONFIGURED root, including ones that do not exist — the
    Settings pane must show the operator the row they typed, marked broken,
    rather than silently dropping it."""
    served = {str(r) for r in cfg.resolved_roots}
    out = []
    for p in cfg.roots:
        exists = False
        resolved = None
        with contextlib.suppress(OSError, RuntimeError):
            resolved = str(p.resolve())
            exists = resolved in served
        row = {"path": _display(p), "exists": exists}
        # A symlinked root serves its TARGET; say so, so the Settings row can
        # never misrepresent what is actually shared.
        if resolved and resolved != str(p):
            row["serves"] = _display(Path(resolved))
        out.append(row)
    return out


def _client_of(request: Request) -> str:
    return getattr(getattr(request, "client", None), "host", "") or "?"


def _fail(request: Request, e: ViewerError):
    if e.status == 403:
        note_denied(str(request.url.query or request.url.path), _client_of(request), e.reason)
    raise HTTPException(e.status, e.detail)


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #

# Plain `def` on purpose: Starlette runs these in its threadpool, so a slow
# mount or a 2 000-entry listing never stalls the event loop that carries
# every WebSocket in the house.
@router.get("/api/local/roots")
def local_roots(request: Request):
    _require_full_access(request)      # second lock, see _require_full_access
    cfg = load()
    return {"roots": _roots_body(cfg), "enabled": cfg.enabled,
            "show_hidden": cfg.show_hidden}


@router.get("/api/local/stat")
def local_stat(request: Request, path: str = Query(...)):
    _require_full_access(request)      # second lock, see _require_full_access
    cfg = load()
    try:
        resolved, _root = resolve(path, cfg)
    except ViewerError as e:
        _fail(request, e)
    return _stat_body(resolved, cfg, _client_of(request))


@router.get("/api/local/ls")
def local_ls(request: Request, path: str = Query(...)):
    _require_full_access(request)      # second lock, see _require_full_access
    cfg = load()
    try:
        resolved, root = resolve(path, cfg)
    except ViewerError as e:
        _fail(request, e)
    if not resolved.is_dir():
        raise HTTPException(400, "Not a directory")

    entries: list[dict] = []
    truncated = False
    try:
        with os.scandir(resolved) as it:
            for de in it:
                if len(entries) >= LS_CAP:
                    truncated = True
                    break
                # A listing must never name something the file route would
                # refuse — otherwise the listing IS the disclosure.
                try:
                    resolve(str(resolved / de.name), cfg)
                except ViewerError:
                    continue
                try:
                    st = de.stat()
                except OSError:
                    continue
                is_dir = stat.S_ISDIR(st.st_mode)
                entries.append({
                    "name": de.name,
                    "kind": "dir" if is_dir else "file",
                    "size": 0 if is_dir else st.st_size,
                    "mtime": float(st.st_mtime),
                    "ext": "" if is_dir else _ext(de.name),
                    "viewer": viewer_kind(de.name, is_dir=is_dir),
                })
    except OSError:
        raise HTTPException(404, NOT_FOUND_DETAIL)

    entries.sort(key=lambda e: (e["kind"] != "dir", e["name"].lower()))
    parent = None if resolved == root else _display(resolved.parent)
    return {"path": _display(resolved), "parent": parent,
            "entries": entries, "truncated": truncated}


class ViewerConfigIn(BaseModel):
    roots: list[str] = Field(default_factory=list)
    show_hidden: bool = False


@router.put("/api/local/config")
def local_config(request: Request, body: ViewerConfigIn):
    _require_full_access(request)      # second lock, see _require_full_access
    cleaned: list[str] = []
    for entry in body.roots:
        p = _expand(entry)
        if p is None:
            raise HTTPException(400, "Each folder must be an absolute path or start with ~/")
        try:
            r = p.resolve(strict=True)
        except (OSError, RuntimeError):
            raise HTTPException(400, "Not an existing folder, or a folder that is always refused")
        if not r.is_dir():
            raise HTTPException(400, "Not an existing folder, or a folder that is always refused")
        # A root that is itself inside a built-in deny subtree would serve
        # nothing anyway; refusing it at write time says so out loud instead of
        # leaving a row in Settings that silently does not work.
        if any(_under(r, d) for d in _builtin_deny_roots()):
            raise HTTPException(400, "Not an existing folder, or a folder that is always refused")
        # The whole filesystem (or a path that only reaches it THROUGH a deny
        # root, `/proc/self/root`) is not a folder to share — the deny list is
        # a short blacklist, not a filesystem policy.
        if r == Path("/") or any(_under(p, d) for d in _builtin_deny_roots()):
            raise HTTPException(400, "That folder is too broad to share — pick a folder inside it")
        cleaned.append(str(p))

    keep = load()
    _write_config(cleaned, body.show_hidden, keep)
    cfg = load()
    return {"roots": _roots_body(cfg), "enabled": cfg.enabled,
            "show_hidden": cfg.show_hidden}


@router.get("/local/file/{path:path}")
def local_file(request: Request, path: str):
    _require_full_access(request)      # second lock, see _require_full_access
    cfg = load()
    # The router strips the leading slash; `~/x` is passed through untouched so
    # the `~`-as-first-segment rule in resolve() still applies.
    raw = path if path.startswith("~") else "/" + path
    try:
        resolved, _root = resolve(raw, cfg)
    except ViewerError as e:
        _fail(request, e)
    return _serve(request, resolved, cfg)


@router.get("/local/view/{ticket}/{rel:path}")
def local_view(request: Request, ticket: str, rel: str):
    """Ticket-scoped twin of `local_file` for FRAMED content — see the Frame
    tickets section. The session gate lets this prefix through, so this
    handler is the only lock: it must refuse on its own. No `_fail` logging
    here on purpose — a hostile framed page could otherwise flood the denial
    log with one <img> tag per request."""
    if "\x00" in rel or "\\" in rel or rel.startswith("/"):
        raise HTTPException(403, DENIED_DETAIL)
    scope = _use_ticket(ticket, _client_of(request))
    if scope is None:
        raise HTTPException(403, DENIED_DETAIL)
    cfg = load()
    # Lexical scope check FIRST, so a `..` probe cannot use resolve()'s 404 to
    # learn whether something outside this directory exists.
    lexical = Path(os.path.normpath(scope / rel)) if rel else scope
    if lexical != scope and not _under(lexical, scope):
        raise HTTPException(403, DENIED_DETAIL)
    try:
        resolved, _root = resolve(str(lexical), cfg)
    except ViewerError as e:
        # A missing file inside the page's own folder is an honest 404 (a
        # broken <img> in a site is normal); every refusal stays uniform.
        raise HTTPException(e.status, e.detail) from None
    # A symlink that resolves inside a root but OUTSIDE this ticket's
    # directory is exactly the escape the scope exists to stop.
    if resolved != scope and not _under(resolved, scope):
        raise HTTPException(403, DENIED_DETAIL)
    return _serve(request, resolved, cfg,
                  extra={"Access-Control-Allow-Origin": "*"})


def _serve(request: Request, resolved: Path, cfg: ViewerConfig, *, extra: dict | None = None):
    """Directory → 307 to the slash form → its index; file → the bytes."""
    if resolved.is_dir():
        if not request.url.path.endswith("/"):
            # Without the trailing slash the browser resolves a framed page's
            # relative <link>/<script> against the PARENT, so a static site
            # loads bare. 307 keeps the method and any query.
            target = request.url.path + "/"
            if request.url.query:
                target += "?" + request.url.query
            return RedirectResponse(target, status_code=307)
        index = _index_of(resolved, cfg)
        if index is None:
            return JSONResponse({"detail": "no index"}, status_code=404)
        resolved = index

    if not resolved.is_file():
        raise HTTPException(404, NOT_FOUND_DETAIL)

    ctype = content_type(resolved.name)
    headers = dict(extra or {})
    if ctype == OCTET:
        # Unknown type: never render it, hand it to the download manager.
        safe = resolved.name.replace('"', "")
        headers["Content-Disposition"] = f'attachment; filename="{safe}"'
    return FileResponse(resolved, media_type=ctype, headers=headers)
