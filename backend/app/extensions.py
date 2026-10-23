"""Extensions — installed add-on packages that plug a whole feature into DisPatch.

Contract: ``docs/extensions.md`` (the developer guide) and, for the voice
add-on, CONTRACTS §A/§B.1/§D of the v3 design. Today there is one extension,
DisPatch Voice (``dispatch-voice``, MIT, a separate repository): voice chat in
its entirety — speech pipeline, session, voices, its own frontend — lives in
that package, and DisPatch carries only this host plus the Settings → Voice
page that explains how to add it when it is absent.

How an extension differs from an **app** (``apps_loader.py``): an app is a
package that ships in this repository's ``apps/`` (or a data-dir package the
operator trusted); an extension is a Python distribution installed into
DisPatch's environment and found through the ``dispatch.extensions`` entry
point group. Both are trusted code running in this process. The difference is
what they may reach: an app gets a router and a few hooks, an extension gets
:class:`ExtensionContext` — thread turns, a gated thread websocket, the frame
stream of one thread, static assets, tools rows and bot-field validators.

Discovery (:func:`discover`), in order:

  * ``DISPATCH_EXTENSIONS=off`` → nothing is imported (installed ones list as
    ``disabled``);
  * ``DISPATCH_EXTENSIONS=mod:attr[,mod:attr]`` → exactly those;
  * otherwise every entry point in ``dispatch.extensions``, plus — for ONE
    release, logged as deprecated — ``DISPATCH_VOICE_PKG=<dir>`` (the 2.1
    drop-in), which puts ``<dir>`` on ``sys.path`` and implies
    ``dispatch_voice:EXTENSION``.

The handshake reads the target's metadata dict (the add-on's top module must be
cheap: metadata only), checks ``api[0] <= EXT_API <= api[1]`` and only then
imports and calls ``setup(ctx)``. A raising ``setup`` is state ``failed`` and
everything it registered is rolled back. **The shell never fails to boot
because of an extension.**

Security: every gate stays here. ``include_router`` puts the operator
dependency on every route, refuses websocket routes and keeps the prefix off the
machine-inbound surface. ``thread_websocket`` runs Origin==Host → the caller
(a session, else the lock-target default profile) → its thread and a character it
may use → slots, all before ``accept`` (HTTP middleware never sees a websocket), and hands the
extension only a gated socket. ``/ext/<id>/…`` serves an allowlist of asset
types, operator only. This module does not import ``main``; main lends what an
extension may reach through :class:`Hooks`.
"""
from __future__ import annotations

import asyncio
import contextlib
import copy
import hashlib
import importlib
import importlib.util
import inspect
import logging
import os
import re
import sys
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import urlparse

import yaml
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.routing import APIRoute
from starlette.routing import Match, WebSocketRoute
from starlette.websockets import WebSocketState

from . import auth, config, ext_profile_hooks, fsperm, livetools, tools
from .contracts.permissions import Permission

log = logging.getLogger("local-chat.ext")

#: The host's extension API. Bump ONLY on a breaking change; additive growth
#: (a new hook, a new optional argument) never bumps it — an extension
#: feature-tests with ``hasattr(ctx, "x")``.
EXT_API = 1
GROUP = "dispatch.extensions"
ID_RE = re.compile(r"\A[a-z][a-z0-9-]{1,31}\Z")
TOOL_ID_RE = re.compile(r"\A[a-z0-9-]{1,40}\Z")
DETAIL_MAX = 200
#: Bot fields DisPatch stores for extensions (config.yaml bots[]); their
#: meaning — and their validation — is the extension's.
BOT_FIELDS = {"voice": 80, "voice_rp": 32}

#: The folder an extension's model files live in (``ExtPaths.models_dir``): ``<data>/models``,
#: with a subfolder per family that the add-on chooses (the voice add-on: ``speech/``,
#: ``chatterbox/``). It was ``<data>/speech-models`` until 3.0, a name that also held the
#: Chatterbox voice model and confused every support thread; :func:`migrate_models_dir`
#: moves the old folder across once.
MODELS_DIRNAME = "models"
LEGACY_MODELS_DIRNAME = "speech-models"

FORBIDDEN_BODY = {"detail": "Your profile does not have permission to do that.",
                  "error": {"code": "permission_denied",
                            "message": "Your profile does not have permission to do that."}}
NOT_FOUND = {"detail": "Not found"}

#: /ext/<id>/<path>: what may be served, and as what. Documents (html, htm,
#: svg, xml) are NOT here on purpose: an extension renders into the shell's
#: DOM, and a document served from this origin would be a same-origin page
#: (hardening M9). Anything else answers 404.
STATIC_TYPES: dict[str, str] = {
    "js": "text/javascript; charset=utf-8",
    "mjs": "text/javascript; charset=utf-8",
    "css": "text/css; charset=utf-8",
    "json": "application/json",
    "map": "application/json",
    "wasm": "application/wasm",
    "onnx": "application/octet-stream",
    "wav": "audio/wav",
    "mp3": "audio/mpeg",
    "ogg": "audio/ogg",
    "png": "image/png",
    "jpg": "image/jpeg",
    "webp": "image/webp",
    "woff2": "font/woff2",
}
STATIC_MAX_BYTES = 64 * 1024 * 1024
STATIC_CSP = "default-src 'none'; frame-ancestors 'self'"
#: ``private``: an operator-only resource is never for a shared cache, even
#: though the content is the public add-on (FR-hardening I1).
CACHE_IMMUTABLE = "private, max-age=31536000, immutable"

#: WebSocket close codes the gate uses (the same ones /ws and the 2.1 voice
#: route used, so a client needs no new vocabulary).
POLICY = 1008
UNAVAILABLE = 1013
REPLACED = 4001
INTERNAL = 1011

#: ``setup(ctx)`` only registers things (slow work belongs in
#: ``on_startup(thread=True)``). One that has not returned after this long is
#: abandoned — the extension is ``failed``, its routes rolled back, its context
#: sealed so a late registration raises — and DisPatch boots without it
#: (FR-hardening L4: a 20 s setup delayed the boot 23 s; a real hang would never
#: have bound the port, and systemd does not restart a live process).
SETUP_TIMEOUT_S = 60.0
#: ``<data>/ext/.booting`` counts start-ups that never got going: written before
#: the extensions load, removed by a clean shutdown or after SETTLE_S of uptime.
#: A native crash (segfault, abort, the OOM killer) during an extension's import,
#: setup or start-up prewarm leaves it behind; at BOOT_FAILS_MAX the next start
#: mounts NO extension, so a crash loop ends with DisPatch up and Settings › Voice
#: saying why. A restart after the box has settled tries the extensions again.
BOOT_MARKER = ".booting"
BOOT_FAILS_MAX = 2
SETTLE_S = 60.0

#: subscribe_frames: per-subscriber queue bound (overflow drops the oldest).
FRAME_QUEUE_MAX = 256
#: Open streams older than this are dropped from the subscribe snapshot (a
#: stream that never saw its stream_done — a crashed turn — must not be
#: replayed to every voice chat for the rest of the process).
STREAM_STALE_S = 15 * 60
STREAM_TEXT_MAX = 64 * 1024
#: A thread's last extension caller is remembered this long after its socket closed: a
#: spoken turn committed on hang-up is still checked against that person's dispatch_agents.
HANGUP_GRACE_S = 120.0


# --------------------------------------------------------------------------- #
# Contract types (CONTRACTS §B.1). An extension never imports these: objects
# it builds (VoiceInstructions, ToolSpec) are read by attribute only (T0-2).
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class VoiceInstructions:
    """Per-turn instructions the add-on composes; DisPatch never holds persona
    text. ``system`` goes into the system slot of backends that have one
    (advisor, API bots) after the bot's persona/rules and before data blocks;
    ``trailer`` is appended to the composed user text of gateway agents as
    ``f"{composed}\\n\\n{trailer}"``. Empty string = omit that placement."""
    system: str = ""
    trailer: str = ""


class SpeculativeRun(Protocol):
    """A model call started before the turn was committed (T2 builds them).
    Persists nothing and broadcasts nothing until adopted."""
    text: str
    done: bool
    head_id: str | None
    prompt_chars: int

    async def discard(self) -> None: ...


Backend = Literal["gateway", "api", "advisor"]


@dataclass(frozen=True)
class BotView:
    """The only bot shape an extension sees: no api/provider blocks."""
    id: str
    name: str
    voice: str
    voice_rp: str
    backend: Backend | None
    #: Additive (2.2.0, amendment FINAL-FIX-1; an extension reads them with
    #: ``getattr``): how the bot's persona names the person it talks to (an
    #: advisor's ``owner``; "" when DisPatch does not know) …
    owner: str = ""
    #: … and whether the bot has a persona of its own: False for an API bot on
    #: DisPatch's default system prompt, True for an API bot with its own prompt
    #: and for an advisor, None for a gateway agent (its persona lives in the
    #: agent runtime, which DisPatch cannot see).
    has_persona: bool | None = None
    #: Additive (4.0, BUILD-PLAN-v2 §1.12; read with ``getattr``): ``"household"`` or
    #: ``"personal"``, and for a personal bot the profile id that owns it ("" otherwise).
    #: The voice add-on passes ``owner_profile or None`` to its voice-access rule.
    scope: str = "household"
    owner_profile: str = ""


@dataclass(frozen=True)
class ExtPaths:
    """An extension's files, by name — never the data directory itself (which
    holds security.yaml and the chat database)."""
    voice_yaml: Path
    acks_yaml: Path
    voices_dir: Path
    cache_dir: Path
    models_dir: Path
    state_dir: Path


@dataclass
class ToolSpec:
    id: str
    feature: str
    title: str
    icon: str
    opt_in: bool
    probe: Callable[[], bool]


@dataclass
class ThreadSocket:
    thread: Any
    bot: BotView
    token: str | None
    session_live: Callable[[], bool]
    touch: Callable[[], None]
    close_unavailable: Callable[[str], Awaitable[None]]
    #: Additive (T1-1): register a callback the host runs when a newer socket
    #: of the same session replaces this one (closed 4001 right after). A
    #: half-open client never answers the close frame, so the extension's
    #: receive loop may not wake on its own.
    on_replaced: Callable[[Callable[[], Any]], None] = lambda fn: None
    #: Additive (4.0): the profile behind the socket - the principal's profile id, which is
    #: the thread's owner by construction (WP-CORE fills it; None until then, and for an
    #: open install). The voice add-on resolves a personal bot's voice with it.
    profile_id: str | None = None


def bot_backend_of(bot) -> Backend | None:
    if bot is None:
        return None
    if getattr(bot, "advisor", None):
        return "advisor"
    if getattr(bot, "api", None):
        return "api"
    return "gateway"


def _persona_facts(bot, backend: Backend | None) -> tuple[str, bool | None]:
    """(owner, has_persona) for :class:`BotView` — names and a flag only, never
    the prompt text or a provider block."""
    if backend == "advisor":
        conf = getattr(bot, "advisor", None)
        owner = str(conf.get("owner") or "").strip() if isinstance(conf, dict) else ""
        return owner[:40], True
    if backend == "api":
        api = getattr(bot, "api", None)
        prompt = str(api.get("system_prompt") or "").strip() if isinstance(api, dict) else ""
        return "", bool(prompt)
    return "", None


def bot_view(bot) -> BotView | None:
    if bot is None:
        return None
    backend = bot_backend_of(bot)
    owner, has_persona = _persona_facts(bot, backend)
    return BotView(id=bot.id, name=bot.name,
                   voice=str(getattr(bot, "voice", "") or ""),
                   voice_rp=str(getattr(bot, "voice_rp", "") or ""),
                   backend=backend, owner=owner, has_persona=has_persona,
                   scope=str(getattr(bot, "scope", "") or "household"),
                   owner_profile=str(getattr(bot, "owner_profile", "") or ""))


class BotRegistryView:
    """``ctx.bots``: read-only roster lookups, always the live config."""

    def get(self, bot_id: str) -> BotView | None:
        return bot_view(config.get_bot(bot_id))

    def all(self) -> list[BotView]:
        return [bot_view(b) for b in config.load_bots()]


# --------------------------------------------------------------------------- #
# What main lends the host
# --------------------------------------------------------------------------- #

@dataclass
class Hooks:
    """Getters and coroutines main hands the host. Getters, not values, so a
    test that swaps ``main.db`` or ``main.manager`` is seen at call time."""
    db: Callable[[], Any] = lambda: None
    manager: Callable[[], Any] = lambda: None
    cookie_name: str = "lc_session"
    app_version: str = ""
    submit_turn: Callable[..., Awaitable[str | None]] | None = None
    speculate_turn: Callable[..., Awaitable[Any]] | None = None
    abort_turn: Callable[..., Awaitable[str | None]] | None = None
    retract_message: Callable[..., Awaitable[None]] | None = None
    replies_after: Callable[..., Awaitable[list | None]] | None = None
    note_heard: Callable[..., Awaitable[bool]] | None = None
    counters: Callable[[], dict] = dict


_hooks = Hooks()


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #

@dataclass
class _WsSpec:
    ext_id: str
    name: str
    handler: Callable[[WebSocket, ThreadSocket], Awaitable[None]]
    max_per_session: int
    max_total: int


def _hash_into(h, p: Path, chunk: int = 1 << 20):
    """Feed a file to a hash in 1 MiB chunks (review T1 L8: a large asset is
    never read into memory whole)."""
    with open(p, "rb") as f:
        while block := f.read(chunk):
            h.update(block)
    return h


@dataclass
class Extension:
    """One discovered extension, whatever became of it."""
    id: str
    name: str = ""
    version: str = ""
    api: tuple[int, int] = (0, 0)
    state: str = "failed"          # ok | incompatible | failed | disabled
    detail: str = ""
    source: str = ""
    # Registrations (committed to the live lookups only when state == ok).
    static_dir: Path | None = None
    entry: str | None = None
    css: list[str] = field(default_factory=list)
    locales: bool = False
    prefixes: list[str] = field(default_factory=list)
    body_limits: dict[str, int] = field(default_factory=dict)
    routes: list = field(default_factory=list)
    websockets: dict[str, _WsSpec] = field(default_factory=dict)
    tools: list[Any] = field(default_factory=list)
    live_tools: list[Any] = field(default_factory=list)   # livetools.ToolSpec rows
    validators: dict[str, Callable[[str], None]] = field(default_factory=dict)
    startup: list[tuple[Callable[[], Any], bool]] = field(default_factory=list)
    shutdown: list[Callable[[], Any]] = field(default_factory=list)
    ctx: ExtensionContext | None = None
    _assets: tuple[tuple, str] | None = None

    @property
    def ok(self) -> bool:
        return self.state == "ok"

    def assets_version(self) -> str | None:
        """sha256 over the static tree, recomputed only when a file's
        (path, mtime, size) changed — so an editable checkout edited in place
        never keeps serving a stale hash as `immutable`."""
        if self.static_dir is None:
            return None
        files = _static_files(self.static_dir)
        sig = tuple((rel, st.st_mtime_ns, st.st_size) for rel, _p, st in files)
        if self._assets is not None and self._assets[0] == sig:
            return self._assets[1]
        h = hashlib.sha256()
        for rel, p, _st in files:
            h.update(rel.encode() + b"\0")
            with contextlib.suppress(OSError):
                _hash_into(h, p)
            h.update(b"\0")
        version = h.hexdigest()[:16]
        self._assets = (sig, version)
        return version

    def public(self, *, operator: bool) -> dict:
        """The /api/extensions row: names, versions and states — never a path
        and never a ``module:attr`` string (interim L8)."""
        d: dict = {
            "id": self.id, "name": self.name, "version": self.version,
            "api": list(self.api), "state": self.state, "source": self.source,
            "assets_version": self.assets_version() if self.ok else None,
            "entry": self.entry if self.ok else None,
            "css": list(self.css) if self.ok else [],
            "locales": bool(self.locales) if self.ok else False,
            "tools": [getattr(t, "id", "") for t in self.tools] if self.ok else [],
            "live_tools": [t.name for t in self.live_tools] if self.ok else [],
        }
        if operator:
            d["detail"] = scrub_detail(self.detail)
        return d



_all: list[Extension] = []
_by_id: dict[str, Extension] = {}
_mode = "entry_points"
_wired_apps: set[int] = set()


def registry() -> list[Extension]:
    return list(_all)


def get(ext_id: str) -> Extension | None:
    e = _by_id.get(ext_id)
    return e if e is not None and e.ok else None


def ok_count() -> int:
    return sum(1 for e in _all if e.ok)


def mode() -> str:
    return _mode


def scrub_detail(text: str) -> str:
    """``detail`` as an operator may see it: the home directory reads ``~``
    (both its spellings on this box: /home → /var/home), ≤ 200 chars. The
    traceback stays in the journal."""
    s = " ".join(str(text or "").split())
    homes = {str(Path.home())}
    with contextlib.suppress(OSError, RuntimeError):
        homes.add(str(Path.home().resolve()))
    h = os.environ.get("HOME")
    if h:
        homes.add(h.rstrip("/"))
    for home in sorted((x for x in homes if x and x != "/"), key=len, reverse=True):
        s = s.replace(home, "~")
    return s[:DETAIL_MAX]


# --------------------------------------------------------------------------- #
# Lookups main consults (only `ok` extensions count)
# --------------------------------------------------------------------------- #

def _ok() -> list[Extension]:
    return [e for e in _all if e.ok]


def owns_path(path: str) -> bool:
    """Does an extension own this API path? (Its routes carry the add-on requirement, and
    are never on the machine-inbound surface: voice recordings are not agent business.)"""
    for e in _ok():
        for p in e.prefixes:
            if path == p or path.startswith(p + "/"):
                return True
    return False


def body_limit(path: str) -> int | None:
    for e in _ok():
        if path in e.body_limits:
            return e.body_limits[path]
    return None


def field_validator(field_name: str) -> Callable[[str], None] | None:
    for e in _ok():
        fn = e.validators.get(field_name)
        if fn is not None:
            return fn
    return None


def websocket_spec(name: str) -> _WsSpec | None:
    for e in _ok():
        spec = e.websockets.get(name)
        if spec is not None:
            return spec
    return None


# --------------------------------------------------------------------------- #
# Private folders, the models folder, the credential store, the settings writer
# --------------------------------------------------------------------------- #

_private_done: set[tuple[str, str]] = set()


def _ensure_private_tree(data: Path, state: Path) -> None:
    """Create ``state`` (and its parents) and make ``<data>/ext`` and ``state`` owner-only. The
    data folder itself is restricted only when THIS call created it: an existing one may be
    shared on purpose (a bind mount another service user reads). Once per path per process."""
    key = (str(data), str(state))
    if key in _private_done and state.is_dir():
        return
    created = not data.exists()
    with contextlib.suppress(OSError):
        state.mkdir(parents=True, exist_ok=True)
    if created:
        fsperm.ensure_private(data, directory=True)
    fsperm.ensure_private(data / "ext", directory=True)
    fsperm.ensure_private(state, directory=True)
    _private_done.add(key)


def migrate_models_dir(*, dry_run: bool = True, data: Path | None = None) -> dict:
    """Move ``<data>/speech-models`` to ``<data>/models`` (the 3.0 name). Never overwrites:

    * no old folder: nothing to do;
    * no new folder: one rename;
    * a new folder that is empty: it is removed, then one rename;
    * both have files: the old entries move across one by one, a name that already exists in
      the new folder stays where it is (listed under ``skipped``), the old folder is removed if
      it ends up empty.

    ``DISPATCH_SPEECH_MODELS`` (an operator's chosen folder) is never touched. Idempotent: a
    second run finds nothing. ``dry_run`` (the default here; startup passes False) only reports.
    A folder that is a mount point, or cannot be renamed, is left and reported under ``errors``;
    the add-on still reads files in the old place. Returns ``{"dry_run", "action", "old", "new",
    "moved", "skipped", "errors"}`` with ``action`` one of ``none``, ``explicit``, ``rename``,
    ``merge``, ``error``."""
    base = Path(data) if data is not None else Path(config.DATA_DIR)
    old, new = base / LEGACY_MODELS_DIRNAME, base / MODELS_DIRNAME
    rep: dict = {"dry_run": dry_run, "action": "none", "old": LEGACY_MODELS_DIRNAME, "new": MODELS_DIRNAME,
                 "moved": [], "skipped": [], "errors": []}
    if os.environ.get("DISPATCH_SPEECH_MODELS", "").strip():
        rep["action"] = "explicit"
        return rep
    try:
        if not old.is_dir() or old.is_symlink():
            return rep
        if new.exists() and not new.is_dir():
            rep.update(action="error", errors=[f"{MODELS_DIRNAME} exists and is not a folder"])
            return rep
        empty_new = new.is_dir() and not any(new.iterdir())
        if not new.exists() or empty_new:
            rep["action"] = "rename"
            rep["moved"] = sorted(c.name for c in old.iterdir())
            if not dry_run:
                if empty_new:
                    new.rmdir()
                old.rename(new)
            return rep
        rep["action"] = "merge"
        for child in sorted(old.iterdir(), key=lambda c: c.name):
            if (new / child.name).exists():
                rep["skipped"].append(child.name)
                continue
            rep["moved"].append(child.name)
            if not dry_run:
                os.replace(child, new / child.name)
        if not dry_run:
            with contextlib.suppress(OSError):
                old.rmdir()                      # only when now empty
    except OSError as e:
        rep["action"] = "error"
        rep["errors"].append(f"{type(e).__name__}: {str(e)[:120]}")
        log.warning("ext: could not move %s to %s (%s); the add-on keeps reading the old folder",
                    LEGACY_MODELS_DIRNAME, MODELS_DIRNAME, type(e).__name__)
    return rep


def _migrate_models_at_start() -> None:
    """Startup: the one-time move, before any extension's setup sees ``paths.models_dir``.
    ``DISPATCH_MODELS_MIGRATE``: ``auto`` (default), ``dry-run`` (log what would move), ``off``."""
    mode = os.environ.get("DISPATCH_MODELS_MIGRATE", "auto").strip().lower()
    if mode == "off":
        return
    try:
        rep = migrate_models_dir(dry_run=(mode == "dry-run"))
    except Exception:           # the shell never fails to boot over a folder move
        log.exception("ext: models folder migration failed")
        return
    if rep["action"] in ("none", "explicit"):
        return
    log.info("ext: models folder %s -> %s: %s (%d moved%s, %d kept in place)%s",
             rep["old"], rep["new"], rep["action"], len(rep["moved"]),
             " in dry run" if rep["dry_run"] else "", len(rep["skipped"]),
             f"; errors: {rep['errors']}" if rep["errors"] else "")


SECRET_NAME_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")


class ExtSecrets:
    """``ctx.secrets``: one extension's credentials. ``get`` / ``set`` / ``delete`` / ``has`` by
    a short name; values are strings. Reuses ``agents/secrets.py`` (the OS keyring when usable,
    else a 0600 JSON file in the extension's ``state_dir``), registers every value with the
    redactor so it cannot reach a log line, and never returns one through any HTTP route (the
    host has none). ``backend`` names where values live: ``keyring`` or ``file``."""

    def __init__(self, ext_id: str) -> None:
        self._id = ext_id
        self._provider: Any = None
        self._lock = threading.Lock()

    def _store(self):
        with self._lock:
            if self._provider is None:
                self._provider = _make_secret_provider(self._id)
            return self._provider

    @staticmethod
    def _name(name: str) -> str:
        if not isinstance(name, str) or not SECRET_NAME_RE.match(name):
            raise ValueError("a secret name is 1-64 letters, digits, '.', '_' or '-'")
        return name

    @property
    def backend(self) -> str:
        return str(getattr(self._store(), "name", "file"))

    def get(self, name: str) -> str | None:
        v = self._store().get(self._name(name))
        if v:
            _redact(v)
        return v or None

    def has(self, name: str) -> bool:
        return bool(self._store().get(self._name(name)))

    def set(self, name: str, value: str) -> None:
        if not isinstance(value, str) or not value or len(value) > 4096:
            raise ValueError("a secret value is a non-empty string of at most 4096 characters")
        store = self._store()
        store.set(self._name(name), value)
        _redact(value)
        path = getattr(store, "path", None)
        if path is not None:
            fsperm.ensure_private(path)          # the file provider only chmods; Windows needs the ACL

    def delete(self, name: str) -> None:
        self._store().delete(self._name(name))

    def __repr__(self) -> str:      # never a value
        return f"ExtSecrets(ext={self._id!r})"


def _redact(value: str) -> None:
    with contextlib.suppress(Exception):
        from .agents.secrets import GLOBAL_REDACTOR
        GLOBAL_REDACTOR.register(value)


def _make_secret_provider(ext_id: str):
    """Keyring (service ``dispatch-chat-ext-<id>``) when importable and backed by something
    real, else a 0600 JSON file ``<data>/ext/<id>/secrets.json``."""
    from .agents import secrets as _s
    state = Path(config.DATA_DIR) / "ext" / ext_id
    _ensure_private_tree(Path(config.DATA_DIR), state)
    try:
        kr = _s.KeyringSecretProvider(f"dispatch-chat-ext-{ext_id}")
        if kr.usable():
            return kr
    except ImportError:
        pass
    return _s.FileSecretProvider(state / "secrets.json")


_secret_stores: dict[tuple[str, str], ExtSecrets] = {}


def _secrets_for(ext_id: str) -> ExtSecrets:
    key = (str(config.DATA_DIR), ext_id)         # tests move DATA_DIR: a store per data folder
    st = _secret_stores.get(key)
    if st is None:
        st = _secret_stores[key] = ExtSecrets(ext_id)
    return st


class _NoopWriter:
    """What ``settings_writer`` returns while the settings store does not exist: accepts and
    drops every write, and says so once."""

    live = False

    def __init__(self, namespace: str) -> None:
        self.namespace = namespace
        self._said = False

    def __call__(self, key: str, value: Any) -> None:
        if not self._said:
            self._said = True
            log.info("ext: the settings store is not available yet; writes under %r are ignored", self.namespace)


def is_live_writer(writer: Any) -> bool:
    """False for the no-op stand-in: do not mark a one-way migration done through it."""
    return not isinstance(writer, _NoopWriter)


def _settings_writer(namespace: str) -> Callable[[str, Any], None]:
    mod_name = f"{__package__}.settings_store"            # lands with the settings work (W5)
    try:
        present = importlib.util.find_spec(mod_name) is not None
    except (ImportError, ValueError):
        present = False
    if not present:
        return _NoopWriter(namespace)
    try:
        fn = getattr(importlib.import_module(mod_name), "writer", None)
        return fn(namespace) if callable(fn) else _NoopWriter(namespace)
    except Exception:           # a broken store module must not break an extension's start
        log.exception("ext: the settings store failed to load; writes under %r are ignored", namespace)
        return _NoopWriter(namespace)


def _migrate_ext_settings(ext: Extension) -> None:
    """Startup, once the settings store is live: hand the add-on's ``migrate_voice_yaml`` the
    store's writer. Idempotent (the add-on keeps a marker beside voice.yaml); skipped while the
    writer is the no-op stand-in, because the add-on would record the move as done."""
    if ext.id != "voice" or ext.ctx is None:
        return
    writer = ext.ctx.settings_writer(ext.id)
    if not is_live_writer(writer):
        return
    try:
        try:
            mod = importlib.import_module("dispatch_voice.host.migration")
        except ModuleNotFoundError as e:
            if e.name not in ("dispatch_voice.host.migration", "dispatch_voice.host"):
                raise
            # An add-on older than 1.6 has no settings migration; it keeps reading voice.yaml.
            log.info("ext %s: the add-on has no settings migration (pre-1.6); voice.yaml stays in use",
                     ext.id)
            return
        fn = getattr(mod, "migrate_voice_yaml", None)
        if fn is None:
            return
        rep = fn(writer, dry_run=False)
    except Exception:           # an add-on's migration never stops the shell
        log.exception("ext %s: settings migration failed", ext.id)
        return
    # "deferred": DisPatch Voice 1.6 still reads voice.yaml only and says so itself, once.
    if isinstance(rep, dict) and rep.get("status") not in ("nothing_to_migrate", "already_migrated", "deferred"):
        log.info("ext %s: settings migration: %s (%s key(s) written)", ext.id, rep.get("status"),
                 rep.get("written"))


# --------------------------------------------------------------------------- #
# ExtensionContext
# --------------------------------------------------------------------------- #

class ExtensionContext:
    """Everything an extension gets (CONTRACTS §B.1). Registration methods work
    during ``setup(ctx)`` only; the context is sealed afterwards."""

    def __init__(self, ext: Extension, app: FastAPI, env: dict[str, str]) -> None:
        self._ext = ext
        self._app = app
        self._sealed = False
        self.api_version = EXT_API
        self.app_version = _hooks.app_version
        self.ext_id = ext.id
        self.log = logging.getLogger(f"local-chat.ext.{ext.id}")
        self.bots = BotRegistryView()
        self.env = dict(env)

    # -- plain getters --------------------------------------------------- #

    @property
    def paths(self) -> ExtPaths:
        """Computed from the live DATA_DIR (tests move it). ``state_dir`` is created on first
        use and is owner-only (0700 on POSIX, an owner-only ACL on Windows); so is the data
        folder itself when this call is what created it."""
        data = Path(config.DATA_DIR)
        eid = self.ext_id
        models = os.environ.get("DISPATCH_SPEECH_MODELS", "").strip()
        state = data / "ext" / eid
        _ensure_private_tree(data, state)
        if eid == "voice":
            names = ("voice.yaml", "voice-acks.yaml", "voices", "voice-cache")
        else:
            names = (f"{eid}.yaml", f"{eid}-acks.yaml", f"{eid}-files", f"{eid}-cache")
        return ExtPaths(voice_yaml=data / names[0], acks_yaml=data / names[1],
                        voices_dir=data / names[2], cache_dir=data / names[3],
                        models_dir=Path(models) if models else data / MODELS_DIRNAME,
                        state_dir=state)

    @property
    def secrets(self) -> ExtSecrets:
        """The host's credential store for this extension (get / set / delete by name),
        backed by ``agents/secrets.py``: the OS keyring when one is usable, else a private file
        in ``state_dir`` (so it is on the data volume in a container and survives an image
        update). An extension keeps tokens here instead of a dot-file under the user's home."""
        return _secrets_for(self.ext_id)

    def settings_writer(self, namespace: str | None = None) -> Callable[[str, Any], None]:
        """``write(key, value)`` into the settings store under ``namespace`` (the extension's own
        id, or ``<id>.<sub>``; another extension's namespace is refused). Returns the settings
        store's writer when that module exists; until then a no-op that logs once, so an
        extension can call this today and become live when the store lands. Test
        ``is_live_writer(w)`` before a one-way move (a no-op writer must not count as moved)."""
        ns = (namespace or self.ext_id).strip()
        if ns != self.ext_id and not ns.startswith(self.ext_id + "."):
            raise ValueError(f"extension {self.ext_id!r} may only write settings under {self.ext_id!r}")
        return _settings_writer(ns)

    def db(self):
        return _hooks.db()

    def is_operator(self, request) -> bool:
        return tools._is_operator(request)

    def require_operator(self, request) -> None:
        require_operator(request)

    def bot_backend(self, bot_id: str) -> Backend | None:
        return bot_backend_of(config.get_bot(bot_id))

    def touch_session(self, token: str | None) -> None:
        if token:
            auth.touch_session(token)

    def socket_count(self, name: str) -> tuple[int, int]:
        spec = self._ext.websockets.get(name)
        return _open_total.get(name, 0), (spec.max_total if spec else 0)

    @property
    def counters(self) -> dict:
        """Additive (T1-2): host-side counters an extension may show in its
        metrics, e.g. ``speculation_stale`` / ``speculation_adopted``."""
        return dict(_hooks.counters())

    # -- registration (setup only) -------------------------------------- #

    def _check_open(self, what: str) -> None:
        if self._sealed:
            raise RuntimeError(f"{what} is only allowed inside setup(ctx)")

    def include_router(self, router, *, prefix: str,
                       body_limits: dict[str, int] | None = None) -> None:
        self._check_open("include_router")
        own = f"/api/{self.ext_id}"
        if not isinstance(prefix, str) or not (prefix == own or prefix.startswith(own + "/")):
            raise ValueError(f"extension routes must live under {own}, not {prefix!r}")
        if prefix.endswith("/") or "//" in prefix or ".." in prefix:
            raise ValueError(f"bad prefix {prefix!r}")
        for r in getattr(router, "routes", []):
            if isinstance(r, WebSocketRoute):
                raise ValueError("thread_websocket is the only websocket path an extension may "
                                 "have; this router carries " + repr(getattr(r, "path", r)))
            if not isinstance(r, APIRoute):
                # A Mount / plain starlette Route takes no `dependencies`, so
                # require_operator would never run on it: refuse, don't hope.
                raise ValueError("extension routers may carry only FastAPI API routes (static "
                                 "files go through serve_static); this router carries "
                                 + type(r).__name__ + " " + repr(getattr(r, "path", "")))
        limits: dict[str, int] = {}
        for p, n in (body_limits or {}).items():
            if not isinstance(p, str) or not (p == prefix or p.startswith(prefix + "/")):
                raise ValueError(f"body limit for {p!r} is outside {prefix}")
            if not isinstance(n, int) or isinstance(n, bool) or n <= 0:
                raise ValueError(f"body limit for {p!r} must be a positive int")
            limits[p] = n
        before = list(self._app.router.routes)
        self._app.include_router(router, prefix=prefix, dependencies=[Depends(require_extension_route)])
        self._ext.routes += [r for r in self._app.router.routes if not any(r is b for b in before)]
        if prefix not in self._ext.prefixes:
            self._ext.prefixes.append(prefix)
        self._ext.body_limits.update(limits)

    def serve_static(self, directory: Path, *, entry: str, css: Sequence[str] = (),
                     locales: bool = False) -> str:
        self._check_open("serve_static")
        if self._ext.static_dir is not None:
            raise ValueError("serve_static may be called once")
        d = Path(directory)
        if not d.is_dir():
            raise ValueError("serve_static: not a directory")
        for rel in (entry, *css):
            if not isinstance(rel, str) or rel.startswith("/") or ".." in rel.split("/"):
                raise ValueError(f"serve_static: bad asset path {rel!r}")
            if _ext_of(rel) not in STATIC_TYPES or not (d / rel).is_file():
                raise ValueError(f"serve_static: {rel!r} is not a servable file")
        if _ext_of(entry) not in ("js", "mjs"):
            raise ValueError("serve_static: the entry must be a JavaScript module")
        self._ext.static_dir = d
        self._ext.entry = entry
        self._ext.css = list(css)
        self._ext.locales = bool(locales)
        not_served = [rel for rel, _p, _st in _static_files(d, all_types=True)
                      if _ext_of(rel) not in STATIC_TYPES]
        if not_served:
            self.log.warning("ext %s: %d file(s) under its static dir are never served "
                             "(type not on the allowlist): %s", self.ext_id,
                             len(not_served), ", ".join(not_served[:5]))
        return self._ext.assets_version() or ""

    def thread_websocket(self, name: str,
                         handler: Callable[[WebSocket, ThreadSocket], Awaitable[None]],
                         *, max_per_session: int = 2, max_total: int = 3) -> None:
        self._check_open("thread_websocket")
        if not isinstance(name, str) or not (name == self.ext_id
                                              or name.startswith(self.ext_id + "-")):
            raise ValueError(f"websocket name must be {self.ext_id!r} or start with it")
        if not ID_RE.match(name):
            raise ValueError(f"bad websocket name {name!r}")
        if name in self._ext.websockets or websocket_spec(name) is not None:
            raise ValueError(f"/ws/{name}/ is already registered")
        if not callable(handler):
            raise ValueError("handler must be callable")
        mps = max(1, min(int(max_per_session), 8))
        mt = max(1, min(int(max_total), 32))
        self._ext.websockets[name] = _WsSpec(self.ext_id, name, handler, mps, mt)

    def set_bot_field_validator(self, field_name: str, fn: Callable[[str], None]) -> None:
        self._check_open("set_bot_field_validator")
        if field_name not in BOT_FIELDS:
            raise ValueError(f"DisPatch stores no bot field {field_name!r} for extensions "
                             f"(known: {', '.join(BOT_FIELDS)})")
        if not callable(fn):
            raise ValueError("validator must be callable")
        self._ext.validators[field_name] = fn

    def register_tools(self, rows: list) -> None:
        self._check_open("register_tools")
        for r in rows:
            tid = getattr(r, "id", None)
            feat = getattr(r, "feature", None)
            if not isinstance(tid, str) or not TOOL_ID_RE.match(tid):
                raise ValueError(f"tool id {tid!r} must match [a-z0-9-]{{1,40}}")
            if not isinstance(feat, str) or not re.match(r"\A[a-z][a-z0-9_]{0,40}\Z", feat):
                raise ValueError(f"tool feature {feat!r} must match [a-z][a-z0-9_]*")
            if tid in tools.CORE_BUILTINS or tools.builtin_owner(tid) not in (None, self.ext_id):
                raise ValueError(f"tool id {tid!r} is already taken")
            if not callable(getattr(r, "probe", None)):
                raise ValueError(f"tool {tid!r} needs a probe()")
            self._ext.tools.append(r)

    def register_live_tools(self, specs) -> None:
        """Add model-callable lookups to the advisor live-tools registry. Each
        spec is a dict (or ``livetools.LiveTool``): ``name`` (``[a-z][a-z0-9_]{1,30}``,
        not a built-in's, not another extension's), ``description``,
        ``parameters`` (JSON schema), ``fn`` (``async fn(args: dict) -> str |
        ToolResult``) and optionally ``summary`` (``fn(args) -> str``, the
        notice-row label). A bad spec raises ``ValueError`` and the extension
        fails to load."""
        self._check_open("register_live_tools")
        pending = [t.name for t in self._ext.live_tools]
        self._ext.live_tools += livetools.validate_live_tools(specs, self.ext_id, pending=pending)

    # -- 4.0 per-profile hooks (BUILD-PLAN-v2 §1.12; additive, EXT_API stays 1) ----------
    # Thin delegates to app/ext_profile_hooks.py, which documents each one. An add-on
    # feature-tests with hasattr(ctx, "register_voice_access").

    def caller(self, request) -> ext_profile_hooks.Caller | None:
        """Who is calling: ``Caller(profile_id, is_admin, role, can(perm))`` or None."""
        return ext_profile_hooks.caller(request)

    def bots_visible(self, request) -> list[BotView]:
        """The bots the caller may see (never another profile's personal bot)."""
        return ext_profile_hooks.bots_visible(request)

    def register_voice_access(self, fn: Callable[[str, str | None], bool]) -> None:
        """Declare per-profile voices: ``fn(voice_id, bot_owner) -> bool``."""
        self._check_open("register_voice_access")
        ext_profile_hooks.register_voice_access(self.ext_id, fn)

    def register_profile_data(self, *, count: Callable[[str], Mapping[str, int]] | None = None,
                              purge: Callable[[str], int] | None = None) -> None:
        """``count(profile_id) -> {"voices": n}``; ``purge(profile_id) -> int`` (deleted)."""
        self._check_open("register_profile_data")
        ext_profile_hooks.register_profile_data(self.ext_id, count=count, purge=purge)

    def on_startup(self, fn: Callable[[], Any], *, thread: bool = False) -> None:
        self._check_open("on_startup")
        self._ext.startup.append((fn, bool(thread)))

    def on_shutdown(self, fn: Callable[[], Any]) -> None:
        self._check_open("on_shutdown")
        self._ext.shutdown.append(fn)

    # -- thread turns (main's code, through Hooks) ----------------------- #

    async def submit_turn(self, thread_id: str, text: str, *,
                          instructions: Any = None, thinking: str | None = None,
                          metadata: dict | None = None, prefetched: Any = None) -> str | None:
        return await _hooks.submit_turn(self.ext_id, thread_id, text,
                                        instructions=instructions, thinking=thinking,
                                        metadata=metadata, prefetched=prefetched)

    async def speculate_turn(self, thread_id: str, text: str, *,
                             instructions: Any = None, thinking: str | None = None):
        return await _hooks.speculate_turn(thread_id, text, instructions=instructions,
                                           thinking=thinking)

    async def abort_turn(self, thread_id: str, *, keep_partial: bool = True) -> str | None:
        return await _hooks.abort_turn(thread_id, keep_partial=keep_partial)

    async def retract_message(self, thread_id: str, message_id: str, *,
                              only_metadata_key: str = "voice") -> None:
        await _hooks.retract_message(thread_id, message_id, only_metadata_key=only_metadata_key)

    async def replies_after(self, thread_id: str, after_id: str) -> list[tuple[str, str]] | None:
        return await _hooks.replies_after(thread_id, after_id)

    async def note_heard(self, thread_id: str, message_id: str, *, heard_upto: int,
                         heard_text: str, unheard_text: str) -> bool:
        return await _hooks.note_heard(thread_id, message_id, heard_upto=heard_upto,
                                       heard_text=heard_text, unheard_text=unheard_text)

    async def subscribe_frames(self, thread_id: str, on_frame: Callable[[dict], Any], *,
                               token: str | None) -> Callable[[], Awaitable[None]]:
        return subscribe(thread_id, on_frame, token=token)


def _ext_of(rel: str) -> str:
    name = rel.rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def _static_files(root: Path, *, all_types: bool = False) -> list[tuple[str, Path, os.stat_result]]:
    """(relpath, path, stat) for every regular file under ``root``, sorted,
    skipping dot-paths and __pycache__ (never served either)."""
    out = []
    try:
        base = root.resolve(strict=True)
    except OSError:
        return out
    for p in sorted(base.rglob("*")):
        rel = p.relative_to(base).as_posix()
        parts = rel.split("/")
        if any(x.startswith(".") or x == "__pycache__" for x in parts):
            continue
        if not all_types and _ext_of(rel) not in STATIC_TYPES:
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        if p.is_file():
            out.append((rel, p, st))
    return out


#: Methods a browser may send cross-site without a preflight are only these
#: "safe" ones when the route is a pure read; everything else is checked below.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def require_operator(request: Request) -> None:
    """The dependency on every extension operator route: ``manage_plugins`` (an Admin), or
    anyone when no PIN exists. Never the machine-inbound tier.

    For every method that can change something (POST, PUT, PATCH, DELETE) a
    browser request must also come from DisPatch's own page: a present Origin
    must equal the Host — the same rule as the websocket gate — and
    ``Sec-Fetch-Site`` must not say ``cross-site`` or ``same-site``. A raw-body or
    multipart POST is a CORS "simple" request that a page on another port of the
    same host name sends with the unlocked cookie and no preflight (FR-hardening
    L1: a cross-origin ``POST /api/voice/lab/test`` loaded the speech models).
    Non-browser callers send neither header and are judged by the session alone."""
    if request.method not in SAFE_METHODS:
        if not origin_ok(request.headers) or \
                request.headers.get("sec-fetch-site", "").lower() in ("cross-site", "same-site"):
            raise HTTPException(403, "cross-origin request refused")
    from . import policy
    policy.of(request).require(Permission.MANAGE_PLUGINS)


def require_extension_route(request: Request) -> None:
    """The dependency on every route an extension mounts (``ctx.include_router``).

    The same Origin rule as :func:`require_operator`, then the requirement the route
    table declares for this add-on path (``route_policy.extension_requirement``): the
    reads a voice user needs (``use_voice``, 2026-10-19: Members have voice) or, for
    everything else, ``manage_plugins``."""
    if request.method not in SAFE_METHODS:
        if not origin_ok(request.headers) or \
                request.headers.get("sec-fetch-site", "").lower() in ("cross-site", "same-site"):
            raise HTTPException(403, "cross-origin request refused")
    from . import policy, route_policy
    req = route_policy.extension_requirement(request.method, request.url.path)
    pol = policy.of(request)
    d = policy.authorize(pol.principal, req)       # the gate's own decision, stated again here
    if not d.allowed:
        raise policy.denied(pol.principal, req.permission or Permission.MANAGE_PLUGINS)


# --------------------------------------------------------------------------- #
# Discovery, handshake, setup
# --------------------------------------------------------------------------- #

@dataclass
class Candidate:
    target: str           # "module:attr"
    source: str           # shown in /api/extensions — never a path
    ep_name: str | None = None


def _entry_points() -> list:
    try:
        return list(entry_points(group=GROUP))
    except Exception:
        log.exception("ext: reading the %s entry points failed", GROUP)
        return []


def _ep_source(ep) -> str:
    dist = getattr(ep, "dist", None)
    if dist is not None:
        with contextlib.suppress(Exception):
            return f"entry point ({dist.metadata['Name']} {dist.version})"
    return "entry point"


USER_OFF_FILE = "extensions.yaml"
USER_OFF_DETAIL = "Switched off in Settings -> Plugins; it loads again after you switch it on and restart."


def user_disabled() -> set[str]:
    """Ids the operator switched off (``<data>/extensions.yaml``,
    ``{disabled: [ids]}``, written by Settings -> Plugins). An unreadable file
    switches nothing off."""
    try:
        doc = yaml.safe_load((config.DATA_DIR / USER_OFF_FILE).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return set()
    ids = doc.get("disabled") if isinstance(doc, dict) else None
    return {i for i in (ids or []) if isinstance(i, str)}


def _split_user_disabled(cands: list[Candidate], eps: list) -> tuple[list[Candidate], list[Extension]]:
    """Candidates the operator switched off become `disabled` rows (never
    imported); the rest still load."""
    off = user_disabled()
    if not off:
        return cands, []
    keep: list[Candidate] = []
    rows: list[Extension] = []
    for c in cands:
        cid = _guess_id(c)
        if cid not in off:
            keep.append(c)
            continue
        ep = next((e for e in eps if e.value == c.target), None)
        dist = getattr(ep, "dist", None) if ep is not None else None
        rows.append(Extension(
            id=cid, name=(dist.metadata["Name"] if dist is not None else cid),
            version=(dist.version if dist is not None else ""), state="disabled",
            detail=USER_OFF_DETAIL, source=c.source))
    return keep, rows


def discover() -> tuple[str, list[Candidate], list[Extension]]:
    """(mode, candidates to load, installed-but-disabled rows)."""
    spec = os.environ.get("DISPATCH_EXTENSIONS", "").strip()
    eps = _entry_points()

    def disabled_rows(skip: set[str]) -> list[Extension]:
        rows = []
        for ep in eps:
            if ep.value in skip:
                continue
            dist = getattr(ep, "dist", None)
            rows.append(Extension(
                id=ep.name, name=(dist.metadata["Name"] if dist is not None else ep.name),
                version=(dist.version if dist is not None else ""), state="disabled",
                detail="DISPATCH_EXTENSIONS leaves this extension out", source=_ep_source(ep)))
        return rows

    if spec.lower() == "off":
        return "off", [], disabled_rows(set())
    if spec:
        targets = [t.strip() for t in spec.split(",") if t.strip()]
        by_value = {ep.value: ep for ep in eps}
        cands = [Candidate(t, "DISPATCH_EXTENSIONS",
                           by_value[t].name if t in by_value else None) for t in targets]
        cands, off_rows = _split_user_disabled(cands, eps)
        return "env", cands, disabled_rows(set(targets)) + off_rows
    cands = [Candidate(ep.value, _ep_source(ep), ep.name) for ep in eps]
    legacy = os.environ.get("DISPATCH_VOICE_PKG", "").strip()
    if legacy:
        log.warning("ext: DISPATCH_VOICE_PKG is deprecated and is honoured for 2.2.x only; "
                    "install the add-on (see docs/extensions.md) instead")
        if Path(legacy).is_dir() and legacy not in sys.path:
            sys.path.insert(0, legacy)
        if not any(c.target == "dispatch_voice:EXTENSION" for c in cands):
            cands.append(Candidate("dispatch_voice:EXTENSION", "DISPATCH_VOICE_PKG (deprecated)",
                                   "voice"))
    cands, off_rows = _split_user_disabled(cands, eps)
    return "entry_points", cands, off_rows


def _guess_id(c: Candidate) -> str:
    if c.ep_name and ID_RE.match(c.ep_name):
        return c.ep_name
    mod = c.target.partition(":")[0].split(".")[0].replace("_", "-").lower()
    mod = re.sub(r"[^a-z0-9-]", "", mod)
    return mod if ID_RE.match(mod or "") else "unknown"


def _fail(ext: Extension, e: BaseException | str, *, what: str) -> Extension:
    ext.state = "failed"
    ext.detail = e if isinstance(e, str) else f"{type(e).__name__}: {e}"
    if isinstance(e, BaseException):
        log.error("ext %s: %s failed — extension skipped", ext.id, what,
                  exc_info=(type(e), e, e.__traceback__))
    else:
        log.error("ext %s: %s — extension skipped", ext.id, e)
    return ext


def _resolve_target(target: str):
    mod_name, _, attr = target.partition(":")
    if not mod_name or not re.match(r"\A[A-Za-z_][\w.]*\Z", mod_name):
        raise ValueError(f"not a module:attr target: {target!r}")
    mod = importlib.import_module(mod_name)
    obj = mod
    for part in (attr or "EXTENSION").split("."):
        obj = getattr(obj, part)
    return obj


def _meta(raw) -> dict:
    if not isinstance(raw, Mapping):
        raise TypeError(f"the extension metadata is a {type(raw).__name__}, not a dict")
    eid = raw.get("id")
    if not isinstance(eid, str) or not ID_RE.match(eid):
        raise ValueError(f"id {eid!r} must match [a-z][a-z0-9-]{{1,31}}")
    api = raw.get("api")
    if (not isinstance(api, (list, tuple)) or len(api) != 2
            or not all(isinstance(x, int) and not isinstance(x, bool) for x in api)
            or api[0] > api[1]):
        raise ValueError(f"api must be [min, max] integers, got {api!r}")
    name = raw.get("name") or eid
    version = raw.get("version") or ""
    setup = raw.get("setup")
    env = raw.get("env") or []
    if not isinstance(name, str) or len(name) > 80:
        raise ValueError("name must be a string of at most 80 characters")
    if not isinstance(version, str) or len(version) > 40:
        raise ValueError("version must be a string of at most 40 characters")
    if not isinstance(setup, str) or ":" not in setup:
        raise ValueError("setup must be a 'module:function' string")
    if not isinstance(env, (list, tuple)) or not all(isinstance(x, str) for x in env):
        raise ValueError("env must be a list of names")
    return {"id": eid, "name": name, "version": version, "api": (api[0], api[1]),
            "setup": setup, "env": list(env)}


def load_one(app: FastAPI, cand: Candidate) -> Extension:
    """Handshake, then setup. Never raises (KeyboardInterrupt/SystemExit aside)."""
    ext = Extension(id=_guess_id(cand), source=cand.source)
    try:
        meta = _meta(_resolve_target(cand.target))
    except BaseException as e:            # an extension must never take the shell down
        if isinstance(e, (KeyboardInterrupt, SystemExit)):
            raise
        return _fail(ext, e, what="import")
    ext.id, ext.name, ext.version, ext.api = meta["id"], meta["name"], meta["version"], meta["api"]
    if ext.id in _by_id and _by_id[ext.id].state != "disabled":
        return _fail(ext, f"another extension already provides {ext.id!r} "
                          f"({_by_id[ext.id].name} {_by_id[ext.id].version})", what="handshake")
    lo, hi = ext.api
    if not lo <= EXT_API <= hi:
        ext.state = "incompatible"
        side = "Update the add-on." if hi < EXT_API else "Update DisPatch."
        ext.detail = (f"{ext.name} {ext.version} needs DisPatch extension API "
                      f"{lo}..{hi}; this DisPatch has {EXT_API}. {side}")
        log.warning("ext %s: %s", ext.id, ext.detail)
        return ext
    try:
        setup = _resolve_target(meta["setup"])
        if not callable(setup):
            raise TypeError(f"{meta['setup']} is not callable")
    except BaseException as e:
        if isinstance(e, (KeyboardInterrupt, SystemExit)):
            raise
        return _fail(ext, e, what="import of setup")
    env = {n: os.environ[n] for n in meta["env"] if n in os.environ}
    if ext.id == "voice":
        _migrate_models_at_start()           # before setup(ctx) reads paths.models_dir
    ctx = ExtensionContext(ext, app, env)
    before = list(app.router.routes)
    try:
        res = _run_setup(setup, ctx, SETUP_TIMEOUT_S)
        if inspect.isawaitable(res):
            with contextlib.suppress(Exception):
                res.close()                  # type: ignore[union-attr]
            raise TypeError("setup(ctx) must be synchronous")
    except BaseException as e:
        if isinstance(e, (KeyboardInterrupt, SystemExit)):
            raise
        app.router.routes[:] = [r for r in app.router.routes if any(r is b for b in before)]
        _reset_registrations(ext)
        return _fail(ext, e, what="setup")
    ctx._sealed = True
    ext.ctx = ctx
    try:
        tools.register_builtins(ext.id, ext.tools)
        try:
            livetools.register_extension(ext.id, ext.live_tools)
        except Exception:
            tools.unregister_builtins(ext.id)
            raise
    except Exception as e:
        app.router.routes[:] = [r for r in app.router.routes if any(r is b for b in before)]
        _reset_registrations(ext)
        return _fail(ext, e, what="tool registration")
    ext.state = "ok"
    ext.detail = ""
    log.info("ext %s: %s %s ok (api %d)", ext.id, ext.name, ext.version, EXT_API)
    return ext


def _run_setup(setup: Callable[[Any], Any], ctx: ExtensionContext, timeout: float) -> Any:
    """``setup(ctx)`` on a daemon thread, joined for at most ``timeout`` seconds.
    A setup still running then is abandoned: the context is sealed (a late
    registration raises inside that thread) and TimeoutError goes to the caller,
    which rolls back and marks the extension failed. Whatever it raised is
    re-raised here, KeyboardInterrupt/SystemExit included."""
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["res"] = setup(ctx)
        except BaseException as e:            # handed to the caller as is
            box["exc"] = e

    t = threading.Thread(target=run, name=f"ext-{ctx.ext_id}-setup", daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        ctx._sealed = True
        raise TimeoutError(f"setup(ctx) did not return within {timeout:.0f} s "
                           "(slow work belongs in on_startup(thread=True))")
    if "exc" in box:
        raise box["exc"]
    return box.get("res")


def _boot_marker_on() -> bool:
    """``DISPATCH_EXT_BOOT_MARKER=0`` turns the crash-loop breaker off (the test
    suite does: it imports the app with no data directory of its own)."""
    return os.environ.get("DISPATCH_EXT_BOOT_MARKER", "1").strip().lower() not in ("0", "off", "false", "no")


def _boot_marker() -> Path:
    return Path(config.DATA_DIR) / "ext" / BOOT_MARKER


def boot_fails() -> int:
    """How many start-ups in a row never settled (0 when the marker is absent)."""
    if not _boot_marker_on():
        return 0
    try:
        return max(0, int(_boot_marker().read_text(encoding="utf-8").strip() or 0))
    except (OSError, ValueError):
        return 0


def _write_boot_marker(n: int) -> None:
    if not _boot_marker_on():
        return
    p = _boot_marker()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(p.parent, 0o700)
        p.write_text(f"{n}\n", encoding="utf-8")
    except OSError:
        log.warning("ext: cannot write %s (the crash-loop breaker is off this run)", p)


def clear_boot_marker() -> None:
    """This start settled (or shut down cleanly): it does not count as a failure."""
    if not _boot_marker_on():
        return
    with contextlib.suppress(OSError):
        _boot_marker().unlink()


def _reset_registrations(ext: Extension) -> None:
    ext_profile_hooks.forget(ext.id)
    ext.static_dir = None
    ext.entry = None
    ext.css = []
    ext.locales = False
    ext.prefixes = []
    ext.body_limits = {}
    ext.routes = []
    ext.websockets = {}
    ext.tools = []
    ext.live_tools = []
    ext.validators = {}
    ext.startup = []
    ext.shutdown = []


def _wire(app: FastAPI) -> None:
    if id(app) in _wired_apps:
        return
    app.include_router(router)
    _wired_apps.add(id(app))


def mount_all(app: FastAPI, hooks: Hooks) -> list[Extension]:
    """Discover, handshake and set up every extension. Called once by main at
    import time, where the 2.1 voice block was. Never raises."""
    global _hooks, _mode
    _hooks = hooks
    _wire(app)
    mgr = hooks.manager()
    if mgr is not None and hasattr(mgr, "add_observer"):
        mgr.add_observer(_observe)
    try:
        _mode, cands, disabled = discover()
    except Exception:
        log.exception("ext: discovery failed — no extensions this run")
        return []
    out: list[Extension] = []
    for row in disabled:
        _register(row)
        out.append(row)
    fails = boot_fails() if cands else 0
    if cands and fails >= BOOT_FAILS_MAX:
        # The last starts died before they settled (FR-hardening L4): keep the
        # family app up without extensions this time. The marker stays until
        # this start settles, so a restart after that tries them again.
        log.error("ext: DisPatch failed to start %d times in a row with its extensions loaded; "
                  "starting WITHOUT them this time (see the log above that crash). "
                  "Restart DisPatch once it is up to try them again.", fails)
        for c in cands:
            ext = Extension(id=_guess_id(c), source=c.source, state="failed",
                            detail=("Not loaded: DisPatch failed to start twice in a row with it "
                                    "(a crash or hang during start-up; see the server log). "
                                    "Restart DisPatch to try again."))
            _register(ext)
            out.append(ext)
        return out
    if cands:
        _write_boot_marker(fails + 1)
    for c in cands:
        ext = load_one(app, c)
        _register(ext)
        out.append(ext)
    return out


def _register(ext: Extension) -> None:
    _all.append(ext)
    cur = _by_id.get(ext.id)
    if cur is None or (not cur.ok and ext.ok) or cur.state == "disabled":
        _by_id[ext.id] = ext


def unload_all(app: FastAPI | None = None) -> None:
    """Tests: drop every extension, its routes and its tool rows."""
    for e in list(_all):
        if app is not None and e.routes:
            app.router.routes[:] = [r for r in app.router.routes
                                    if not any(r is o for o in e.routes)]
        with contextlib.suppress(Exception):
            tools.unregister_builtins(e.id)
        livetools.unregister_extension(e.id)
        ext_profile_hooks.forget(e.id)
    _all.clear()
    _by_id.clear()
    _open_total.clear()
    _slots.clear()


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #

async def run_startup() -> None:
    """Lifespan start: each extension's on_startup hooks. A raising hook is
    logged; ``thread=True`` runs it on a daemon thread, never awaited. After
    SETTLE_S of uptime this start no longer counts towards the crash-loop
    breaker (a prewarm that crashes the process natively does, within it)."""
    if _boot_marker_on() and _boot_marker().exists():
        with contextlib.suppress(RuntimeError):
            asyncio.get_running_loop().call_later(SETTLE_S, clear_boot_marker)
    for e in _ok():
        await asyncio.to_thread(_migrate_ext_settings, e)
    for e in _ok():
        for fn, in_thread in e.startup:
            if in_thread:
                threading.Thread(target=_safe_call, args=(e.id, fn), daemon=True,
                                 name=f"ext-{e.id}-startup").start()
                continue
            try:
                res = fn()
                if inspect.isawaitable(res):
                    await res
            except Exception:
                log.exception("ext %s: an on_startup hook failed", e.id)


def _safe_call(ext_id: str, fn: Callable[[], Any]) -> None:
    try:
        res = fn()
        if inspect.isawaitable(res):
            asyncio.run(res)                 # type: ignore[arg-type]
    except Exception:
        log.exception("ext %s: an on_startup(thread=True) hook failed", ext_id)


async def run_shutdown() -> None:
    clear_boot_marker()                  # a clean stop is not a crash
    for e in _ok():
        for fn in e.shutdown:
            try:
                res = fn()
                if inspect.isawaitable(res):
                    await asyncio.wait_for(res, timeout=10)
            except Exception:
                log.exception("ext %s: an on_shutdown hook failed", e.id)


# --------------------------------------------------------------------------- #
# Frames: the open-stream tracker and per-thread subscriptions
# --------------------------------------------------------------------------- #

class _Streams:
    """Every provisional/streaming reply that is open right now, per thread,
    with the text emitted so far — so a subscriber that joins mid-stream can
    be handed a snapshot (hardening H4: a late joiner spliced tails)."""

    def __init__(self) -> None:
        self.open: dict[str, OrderedDict[str, dict]] = {}

    def observe(self, frame: dict) -> None:
        t = frame.get("type")
        tid = frame.get("thread_id")
        if not isinstance(tid, str):
            return
        if t == "stream_start":
            mid = frame.get("message_id")
            if isinstance(mid, str):
                self.open.setdefault(tid, OrderedDict())[mid] = {
                    "bot_id": frame.get("bot_id"), "text": "", "t": time.monotonic()}
        elif t == "stream_chunk":
            mid = frame.get("message_id")
            if not isinstance(mid, str):
                return
            row = self.open.setdefault(tid, OrderedDict()).setdefault(
                mid, {"bot_id": frame.get("bot_id"), "text": "", "t": time.monotonic()})
            piece = frame.get("text") or ""
            if not isinstance(piece, str):
                return
            row["text"] = piece if frame.get("replace") else row["text"] + piece
            if len(row["text"]) > STREAM_TEXT_MAX:
                row["text"] = row["text"][-STREAM_TEXT_MAX:]
            row["t"] = time.monotonic()
        elif t == "stream_done":
            streams = self.open.get(tid)
            if streams:
                for k in ("message_id", "provisional_id"):
                    v = frame.get(k)
                    if isinstance(v, str):
                        streams.pop(v, None)
                if not streams:
                    self.open.pop(tid, None)
        elif t == "message_deleted":
            streams = self.open.get(tid)
            if streams and isinstance(frame.get("message_id"), str):
                streams.pop(frame["message_id"], None)
        elif t in ("thread_deleted", "error"):
            self.open.pop(tid, None)

    def snapshot(self, thread_id: str) -> list[dict]:
        streams = self.open.get(thread_id)
        if not streams:
            return []
        now = time.monotonic()
        for mid in [m for m, r in streams.items() if now - r["t"] > STREAM_STALE_S]:
            streams.pop(mid, None)
        out: list[dict] = []
        for mid, row in streams.items():
            out.append({"type": "stream_start", "thread_id": thread_id,
                        "bot_id": row["bot_id"], "message_id": mid})
            if row["text"]:
                out.append({"type": "stream_chunk", "thread_id": thread_id,
                            "bot_id": row["bot_id"], "message_id": mid,
                            "text": row["text"], "replace": True})
        return out


_streams = _Streams()
#: thread ids deleted since start (bounded) — ThreadSocket.session_live reads it.
_deleted_threads: OrderedDict[str, None] = OrderedDict()


class _Subscription:
    """One subscriber: a bounded queue the host owns, drained by a task —
    never synchronously inside ``manager.broadcast`` (hardening L12)."""

    def __init__(self, thread_id: str, on_frame: Callable[[dict], Any], token: str | None) -> None:
        self.thread_id = thread_id
        self.on_frame = on_frame
        self.token = token
        self.q: deque[dict] = deque()
        self.task: asyncio.Task | None = None
        self.closed = False
        self.overflow_logged = False

    def put(self, frame: dict) -> None:
        if self.closed:
            return
        if len(self.q) >= FRAME_QUEUE_MAX:
            self.q.popleft()
            if not self.overflow_logged:
                self.overflow_logged = True
                log.warning("ext: frame queue for thread %s is full; dropping the oldest",
                            self.thread_id)
        self.q.append(frame)
        if self.task is None or self.task.done():
            try:
                self.task = asyncio.get_running_loop().create_task(self._pump())
            except RuntimeError:
                self.task = None              # no loop (never in the app); next put retries

    async def _pump(self) -> None:
        await asyncio.sleep(0)
        while self.q and not self.closed:
            frame = copy.deepcopy(self.q.popleft())
            try:
                res = self.on_frame(frame)
                if inspect.isawaitable(res):
                    await res
            except Exception:
                log.exception("ext: a frame handler failed (thread %s)", self.thread_id)
            await asyncio.sleep(0)

    async def close(self) -> None:
        self.closed = True
        self.q.clear()
        t = self.task
        if t is not None and not t.done() and t is not asyncio.current_task():
            t.cancel()
            with contextlib.suppress(BaseException):
                await t


_subs: dict[str, list[_Subscription]] = {}


def _deliverable(sub: _Subscription, frame: dict) -> dict | None:
    """The frame as the chat UI of the subscriber's session would get it: the
    ConnectionManager drops thread and character frames the instant that session lapses
    (or a PIN appears on a PIN-less install)."""
    mgr = _hooks.manager()
    if mgr is None or not hasattr(mgr, "frame_for_token"):
        return frame
    return mgr.frame_for_token(sub.token, frame)


def _observe(frame: dict) -> None:
    """ConnectionManager.broadcast observer: synchronous, cheap, never raises."""
    try:
        if not isinstance(frame, dict):
            return
        _streams.observe(frame)
        tid = frame.get("thread_id")
        if frame.get("type") == "thread_deleted" and isinstance(tid, str):
            _deleted_threads[tid] = None
            while len(_deleted_threads) > 4096:
                _deleted_threads.popitem(last=False)
        if not isinstance(tid, str):
            return
        for sub in list(_subs.get(tid, ())):
            out = _deliverable(sub, frame)
            if out is not None:
                sub.put(out)
    except Exception:
        log.exception("ext: frame observer failed")


def subscribe(thread_id: str, on_frame: Callable[[dict], Any], *,
              token: str | None) -> Callable[[], Awaitable[None]]:
    """Deliver the B.1a frames of ONE thread, in broadcast order. A snapshot of
    every open stream of the thread arrives first (stream_start + stream_chunk
    {replace: true}). Snapshot and registration happen with no await between
    them, so no frame is lost or doubled across the join."""
    sub = _Subscription(thread_id, on_frame, token)
    for f in _streams.snapshot(thread_id):
        out = _deliverable(sub, f)
        if out is not None:
            sub.put(out)
    _subs.setdefault(thread_id, []).append(sub)

    async def unsubscribe() -> None:
        lst = _subs.get(thread_id)
        if lst and sub in lst:
            lst.remove(sub)
            if not lst:
                _subs.pop(thread_id, None)
        await sub.close()

    return unsubscribe


# --------------------------------------------------------------------------- #
# The thread websocket gate (CONTRACTS §B.1 rule 3 and rule 9)
# --------------------------------------------------------------------------- #

#: How long the gate lets an evicted socket's on_replaced callbacks run before
#: it accepts the replacement anyway (the rest finish in the background).
EVICT_CALLBACK_WAIT_S = 1.0
_bg_evictions: set[asyncio.Task] = set()


@dataclass
class _Slot:
    ws: Any
    replaced: list[Callable[[], Any]] = field(default_factory=list)
    evicted: bool = False

    async def run_replaced(self) -> None:
        self.evicted = True
        for fn in self.replaced:
            try:
                res = fn()
                if inspect.isawaitable(res):
                    await res
            except Exception:
                log.exception("ext: an on_replaced callback failed")

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self.ws.close(code=REPLACED, reason="replaced")


def _detach(coro: Awaitable[Any]) -> asyncio.Task:
    task = asyncio.ensure_future(coro)
    _bg_evictions.add(task)
    task.add_done_callback(_bg_evictions.discard)
    return task


async def _evict_detached(old: _Slot) -> None:
    """Evict a stale socket WITHOUT parking the replacement behind it (review
    T1 M1): a half-open peer (phone in a tunnel) never answers the close
    frame, and the websockets library waits ~10 s for it. The on_replaced
    callbacks get up to EVICT_CALLBACK_WAIT_S so the extension can stop the old
    session's engines first; the close itself always runs in the background."""
    cb = _detach(old.run_replaced())
    await asyncio.wait({cb}, timeout=EVICT_CALLBACK_WAIT_S)

    async def close_after_callbacks() -> None:
        with contextlib.suppress(Exception):
            await asyncio.shield(cb)
        await old.close()

    _detach(close_after_callbacks())


_slots: dict[tuple[str, str], list[_Slot]] = {}
_open_total: dict[str, int] = {}
_DEVICE_RE = re.compile(r"\A[A-Za-z0-9-]{8,64}\Z")


def origin_ok(headers) -> bool:
    """A present Origin must POSITIVELY equal the Host (CSWSH). No Origin =
    a non-browser client (CSRF is a browser-only vector)."""
    origin = headers.get("origin")
    if origin is None:
        return True
    origin_host = urlparse(origin).netloc
    host = headers.get("host", "")
    return bool(origin_host) and bool(host) and origin_host == host


def slot_key(ws, token: str | None) -> str:
    """Whose sockets this one counts against: the unlocked session; without a
    PIN the browser's device id (``?d=``), else a list of its own."""
    if token:
        return token
    try:
        dev = ws.query_params.get("d") or ""
    except Exception:
        dev = ""
    if _DEVICE_RE.match(dev):
        return f"device:{dev}"
    return f"conn:{id(ws)}"


async def _accept_then_close(ws, code: int, reason: str) -> None:
    """Refuse an ALREADY-AUTHORISED caller with a real close code: a close
    before accept reaches a browser as a bare 1006, which it would retry."""
    with contextlib.suppress(Exception):
        await ws.accept()
    with contextlib.suppress(Exception):
        await ws.close(code=code, reason=(reason or "unavailable")[:120])


_NOT_GIVEN: Any = object()


def _session_live_fn(token: str | None, thread_id: str, bot_id: str,
                     *, device_id: str | None = None,
                     who: tuple[str, str | None] | None = None,
                     owner: Any = _NOT_GIVEN) -> Callable[[], bool]:
    """``ThreadSocket.session_live``: may this socket go on? The add-on asks it all through a
    call (before each turn and while listening): the session (or the lock-target default
    profile) must still resolve to the same caller, who must still own the thread and be able
    to use its character. ``who`` is (kind, profile id) of the caller at the handshake."""
    def session_live() -> bool:
        if thread_id in _deleted_threads:
            return False
        bot = config.get_bot(bot_id)
        if bot is None:
            return False
        from . import characters, policy
        if who is None:
            # Opened on an unclaimed install; claimed since = refuse (the client reconnects).
            return not policy.claimed()
        if token is not None:
            pr = policy.for_session(auth.get_session(token), device_id)
            if pr is None:
                return False
        else:
            # 4.0: no session = the lock-target default profile (policy.unsigned). The lock
            # target moving, or the profile being disabled, ends the call.
            pr = policy.unsigned()
            if pr is None or (pr.kind, pr.profile_id) != who:
                return False
        if owner is not _NOT_GIVEN:
            # 4.0: still the thread owner's call (a session re-resolved to another profile,
            # or the Owner changing, ends it). Checked beside may_use, never instead of it.
            from . import ownership
            if not ownership.may_see_thread(pr, owner, write=True):
                return False
        # The character rule, re-asked: taken off the profile's household list mid-call (or a
        # use_voice switched off) ends the call, as it would refuse a new one.
        return pr.can(Permission.USE_VOICE) and (characters.may_use(bot, pr)
                                                 or _only_agents_missing(bot, pr))
    return session_live


def _ws_device_id(ws) -> str | None:
    """The hashed ``lc_device`` cookie of a socket, as the HTTP gate and /ws read it."""
    from . import policy
    try:
        tok = ws.cookies.get(policy.DEVICE_COOKIE)
    except Exception:
        tok = None
    return auth.device_hash(tok) if tok and 16 <= len(tok) <= 128 else None


def runs_agent(bot) -> bool:
    """Does a turn on this character run an AGENT on the host (the OpenClaw gateway
    backend, which can use tools and run commands)? A direct provider (``api``) or an
    advisor is only a model call. An UNKNOWN character (None: removed from config.yaml
    while its threads remain) counts as one - fail closed; main._run_agent_turn refuses
    its turns outright."""
    return bot is None or bot_backend_of(bot) == "gateway"


def caller_may_dispatch(principal) -> bool:
    """Does the person whose turn it is hold ``dispatch_agents``? None (an unclaimed /
    open install) and a machine (a cron, an on-box script) count as yes. An advisor acts on
    its research / handoff markers only when this is true (advisor.run_turn(dispatch=...))."""
    if principal is None or principal.kind in ("open", "machine"):
        return True
    return bool(principal.can(Permission.DISPATCH_AGENTS))


def may_dispatch(principal, bot) -> bool:
    """May ``principal`` start a turn on ``bot``? The ONE rule every turn entry point
    applies (typed /ws sends, retries, regenerate, edit & re-run, extension turns, app
    turns): an agent-backed character needs ``dispatch_agents``. No principal = an
    unclaimed/open install, where everyone is the owner. Every role asks the same
    question (4.0: a Basic profile has no exemption; an Admin switches dispatch_agents on
    for a profile, with the warning that agents run on the host)."""
    if principal is None or not runs_agent(bot):
        return True
    return bool(principal.can(Permission.DISPATCH_AGENTS))


DISPATCH_REFUSED = ("Your profile may not run agents. Ask an Admin for the "
                    "dispatch_agents permission.")

#: Extension turns (``ctx.submit_turn``) carry no caller, so the host remembers WHO has a
#: socket on each thread: thread id -> {socket key: (principal kind, profile id)} for the
#: open sockets, and thread id -> (monotonic close time, (kind, profile id)) for the last
#: one that closed (a turn committed on hang-up, HANGUP_GRACE_S). The permission is
#: re-checked on every submit, so a mid-call grant or revocation takes effect at once.
_ext_callers: dict[str, dict[int, tuple[str, str | None]]] = {}
_ext_last_closed: dict[str, tuple[float, tuple[str, str | None]]] = {}


def _caller_open(thread_id: str, key: int, who: tuple[str, str | None]) -> None:
    _ext_callers.setdefault(thread_id, {})[key] = who


def _caller_closed(thread_id: str, key: int) -> None:
    open_ = _ext_callers.get(thread_id)
    if open_ is None:
        return
    who = open_.pop(key, None)
    if not open_:
        _ext_callers.pop(thread_id, None)
    if who is not None:
        _ext_last_closed[thread_id] = (time.monotonic(), who)


def _who_may_dispatch(who: tuple[str, str | None]) -> bool:
    """Does this socket's person hold dispatch_agents NOW (re-read from the profile)?"""
    kind, pid = who
    if kind != "profile":
        return kind in ("open", "machine")
    from . import accounts, policy
    prof = accounts.get_profile(pid) if pid else None
    if prof is None or getattr(prof, "disabled", False):
        return False                       # deleted or disabled since: fail closed
    return caller_may_dispatch(policy.profile_principal(prof))


def turn_dispatch_refused(thread_id: str) -> bool:
    """Is an extension turn on this thread asked for by someone without dispatch_agents?
    While sockets are open: True if any open socket's person lacks it NOW. With none
    open: the person whose socket closed last, within HANGUP_GRACE_S (the turn a hang-up
    commits). Otherwise False (no extension caller: the extension's own doing)."""
    open_ = _ext_callers.get(thread_id)
    if open_:
        return any(not _who_may_dispatch(w) for w in list(open_.values()))
    last = _ext_last_closed.get(thread_id)
    if last is None:
        return False
    if time.monotonic() - last[0] < HANGUP_GRACE_S:
        return not _who_may_dispatch(last[1])
    _ext_last_closed.pop(thread_id, None)
    return False


router = APIRouter()


def _only_agents_missing(bot, principal) -> bool:
    """``characters.may_use`` said no only because an agent character needs
    ``dispatch_agents``: step 3b below refuses with that reason instead of a bare 1008."""
    from . import characters
    if not characters.is_agent_backed(bot) or characters.is_personal(bot) \
            or principal.can(Permission.DISPATCH_AGENTS):
        return False
    from dataclasses import replace
    try:
        granted = replace(principal, permissions=principal.permissions | {Permission.DISPATCH_AGENTS.value})
    except Exception:
        return False
    return characters.may_use(bot, granted)


@router.websocket("/ws/{name}/{thread_id}")
async def thread_websocket_endpoint(ws: WebSocket, name: str, thread_id: str):
    # 1. Origin == Host (pre-accept).
    if not origin_ok(ws.headers):
        await ws.close(code=POLICY)
        return
    # 2. The caller, as the main /ws decides it (pre-accept).
    from . import characters, policy
    cfg = auth.load()
    token = None
    principal = None
    device_id = None
    if not policy.claimed(cfg):
        # Nobody owns this install yet: like /ws and the HTTP gate, only this computer (or an
        # install that opted into DISPATCH_ALLOW_OPEN_INSTALL) gets in - not the whole network.
        client = getattr(ws, "client", None)
        local = policy.is_loopback_origin(client.host if client else "",
                                          any(ws.headers.get(h) for h in policy.PROXY_MARKER_HEADERS))
        if not (local or policy.allow_open_install()):
            await ws.close(code=POLICY)
            return
    else:
        # The same principal the HTTP gate and /ws resolve (4.0): a session -> that profile;
        # else the lock-target default profile (policy.unsigned); else nobody - closed, as
        # "nothing but the sign-in page" covers voice too. No device binding.
        device_id = _ws_device_id(ws)
        session = auth.get_session(ws.cookies.get(_hooks.cookie_name))
        principal = policy.for_session(session, device_id)
        if principal is None:
            session = None
            principal = policy.unsigned(cfg)
            if principal is None:
                await ws.close(code=POLICY)
                return
        if not principal.can(Permission.USE_VOICE):
            await ws.close(code=POLICY)        # a profile without voice
            return
        token = session.token if session is not None else None
    # 3. The thread exists, the caller owns it and may use its character (pre-accept, the
    # same 1008 as an unknown thread).
    db = _hooks.db()
    thread = await db.get_thread(thread_id) if db is not None else None
    bot = config.get_bot(thread.bot_id) if thread is not None else None
    from . import ownership
    if (thread is None or bot is None or thread_id in _deleted_threads
            or (principal is not None and not characters.may_use(bot, principal)
                and not _only_agents_missing(bot, principal))
            or (principal is not None
                and not ownership.may_see_thread(principal, getattr(thread, "owner_profile_id", None),
                                                 write=True))):
        # may_use: a character this profile may not use (off its household list, another
        # profile's personal one) is closed here exactly as in the roster, /ws and REST.
        # ownership (4.0): a voice call runs turns and hears every reply in the thread, so it
        # is the thread OWNER's only - the same 1008 as a thread that does not exist.
        await ws.close(code=POLICY)
        return
    # 3b. An agent-backed character (its turns run an agent on the host) needs
    # dispatch_agents, exactly as a typed send on /ws does (main._ws_bot_allowed). Said
    # with a real 1008 + reason after accept: the client stops and says why, no retry.
    if not may_dispatch(principal, bot):
        log.info("ext: /ws/%s/ refused on %s: the profile may not run agents", name, thread_id)
        await _accept_then_close(ws, POLICY, DISPATCH_REFUSED)
        return
    # Rule 9: nothing claims this name — the gate ran, so say why with a real
    # code instead of an unmatched-route 1006 the client would retry.
    spec = websocket_spec(name)
    if spec is None:
        log.info("ext: /ws/%s/ has no extension (thread %s)", name, thread_id)
        await _accept_then_close(ws, UNAVAILABLE, f"No extension provides {name}")
        return
    # 4. Slots: per session (oldest replaced, 4001) and in total (busy, 1013).
    key = slot_key(ws, token)
    slots = _slots.setdefault((name, key), [])
    total = _open_total.get(name, 0)
    if total >= spec.max_total and not slots:
        _slots.pop((name, key), None)
        log.info("ext: /ws/%s/ busy (%d open) — refused %s", name, total, thread_id)
        await _accept_then_close(
            ws, UNAVAILABLE,
            f"{name.capitalize()} is busy on this server ({spec.max_total} chats open)")
        return
    # Take the older sockets out of the count AND count this one with no await
    # in between: the eviction below yields for up to EVICT_CALLBACK_WAIT_S, and
    # another session's handshake landing in that window must see the cap as it
    # will be — not one short (FR-hardening L2: 3 open with max_total 2).
    evicted: list[_Slot] = []
    while slots and (len(slots) >= spec.max_per_session
                     or _open_total.get(name, 0) >= spec.max_total):
        evicted.append(slots.pop(0))
        _open_total[name] = max(0, _open_total.get(name, 0) - 1)
    me = _Slot(ws)
    slots.append(me)
    _open_total[name] = _open_total.get(name, 0) + 1

    def touch() -> None:
        if token is not None:
            auth.touch_session(token)

    sock = ThreadSocket(
        thread=thread, bot=bot_view(bot), token=token,
        session_live=_session_live_fn(
            token, thread_id, bot.id, device_id=device_id if principal is not None else None,
            who=(principal.kind, principal.profile_id) if principal is not None else None,
            owner=getattr(thread, "owner_profile_id", None)),
        touch=touch,
        close_unavailable=lambda reason: _accept_then_close(ws, UNAVAILABLE, reason),
        on_replaced=me.replaced.append,
        # 4.0 voice host contract: the principal's profile, which is the thread's owner by
        # construction (the ownership check above). None on an open install.
        profile_id=(principal.profile_id or None) if principal is not None else None)
    # Remembered for ctx.submit_turn, which is told no caller: whose socket this is, so an
    # extension turn is checked against that person's dispatch_agents at submit time (an
    # advisor's markers; defence in depth behind 3b for an agent character).
    who = None
    if principal is not None:
        who = (principal.kind, principal.profile_id)
        _caller_open(thread_id, id(me), who)
    try:
        # Inside the try: a handshake cancelled mid-eviction (shutdown) still
        # gives its slot back in the finally below.
        for old in evicted:
            log.info("ext: /ws/%s/ — a newer socket of the same session replaces an older one", name)
            await _evict_detached(old)
        await spec.handler(ws, sock)
        if getattr(ws, "client_state", None) == WebSocketState.CONNECTING:
            # The handler returned without accepting (review T1 L4): a close
            # before accept reaches the browser as a bare 1006 it would retry.
            log.warning("ext %s: /ws/%s/ handler returned without accepting", spec.ext_id, name)
            await _accept_then_close(ws, UNAVAILABLE, "extension did not accept")
    except Exception as e:
        if type(e).__name__ != "WebSocketDisconnect":
            log.exception("ext %s: /ws/%s/ handler failed", spec.ext_id, name)
            with contextlib.suppress(Exception):
                if getattr(ws, "client_state", None) == WebSocketState.CONNECTING:
                    await _accept_then_close(ws, INTERNAL, "extension failed")
                else:
                    await ws.close(code=INTERNAL)
    finally:
        if who is not None:
            _caller_closed(thread_id, id(me))
        cur = _slots.get((name, key))
        if cur is not None and me in cur:
            cur.remove(me)
            _open_total[name] = max(0, _open_total.get(name, 0) - 1)
        if cur is not None and not cur:
            _slots.pop((name, key), None)


# --------------------------------------------------------------------------- #
# HTTP: /api/extensions and /ext/<id>/<path>
# --------------------------------------------------------------------------- #

@router.get("/api/extensions")
def api_extensions(request: Request):
    """Anyone who may chat, or an authenticated on-box machine (the deploy check reads it
    with a plain loopback curl); ``detail`` operators only."""
    from . import policy
    operator = tools._is_operator(request)
    pol = policy.of(request)
    if not operator and not pol.machine and not pol.can(Permission.USE_CHAT):
        return JSONResponse(FORBIDDEN_BODY, status_code=403)
    return {"extensions": [e.public(operator=operator) for e in _all], "mode": _mode,
            "api": EXT_API}



#: Voice provider plugins (a speech-to-text, text-to-speech or turn-taking engine for the voice
#: add-on) are Python packages that declare an entry point in this group. The add-on loads the
#: ones the operator switched on; the host only LISTS them (Settings -> Plugins).
VOICE_PROVIDER_GROUP = "dispatch_voice.providers"


def voice_provider_plugins() -> list[dict]:
    """The installed ``dispatch_voice.providers`` entry points: ``[{name, package, version}]``.
    Nothing is imported (a listing never runs a plugin's code), and the ``module:attr`` target is
    left out, like everywhere else in the extension surface."""
    try:
        eps = list(entry_points(group=VOICE_PROVIDER_GROUP))
    except Exception:           # a broken metadata dir lists nothing
        log.exception("ext: could not list %s entry points", VOICE_PROVIDER_GROUP)
        return []
    out = []
    for ep in eps:
        dist = getattr(ep, "dist", None)
        out.append({"name": ep.name, "package": getattr(dist, "name", "") or "",
                    "version": getattr(dist, "version", "") or ""})
    return sorted(out, key=lambda r: r["name"])


class _WhenVoiceAbsent(APIRoute):
    """A route that exists only while the voice add-on is NOT mounted: with it mounted this one
    does not match, so the add-on's own route at the same path (its providers' health and
    settings) is the one that answers."""

    def matches(self, scope):
        match, child = super().matches(scope)
        if match != Match.NONE:
            v = get("voice")
            if v is not None and v.ok:
                return Match.NONE, {}
        return match, child


def api_voice_providers_absent():
    """``GET /api/voice/providers`` while the add-on is not running: nothing to configure, but
    the plugin page can still show which provider plugins are installed and why none is active.
    Needs ``manage_plugins`` (the route table says so); using a provider later needs ``use_voice``."""
    v = _by_id.get("voice")                       # any state: get() answers for a healthy one only
    return {"installed": v is not None, "state": v.state if v is not None else "absent",
            "providers": {}, "plugins": [{**p, "enabled": False} for p in voice_provider_plugins()],
            "permissions": {"list": Permission.MANAGE_PLUGINS.value, "use": Permission.USE_VOICE.value},
            "install": "docs/extensions.md#voice"}


router.add_api_route("/api/voice/providers", api_voice_providers_absent, methods=["GET"],
                     route_class_override=_WhenVoiceAbsent, include_in_schema=False,
                     dependencies=[Depends(require_operator)])


_etags: dict[str, tuple[int, int, str]] = {}


def _etag(p: Path, st: os.stat_result) -> str:
    key = str(p)
    hit = _etags.get(key)
    if hit and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
        return hit[2]
    h = _hash_into(hashlib.sha256(), p).hexdigest()[:32]
    tag = f'"{h}"'
    _etags[key] = (st.st_mtime_ns, st.st_size, tag)
    return tag


@router.api_route("/ext/{ext_id}/{rel:path}", methods=["GET", "HEAD"], include_in_schema=False)
def ext_static(request: Request, ext_id: str, rel: str):
    from . import policy
    if not policy.of(request).can(Permission.USE_VOICE):
        # Anyone who may use voice downloads the add-on's public frontend; a machine
        # caller never does.
        return JSONResponse(FORBIDDEN_BODY, status_code=403)
    ext = get(ext_id)
    if ext is None or ext.static_dir is None:
        return JSONResponse(NOT_FOUND, status_code=404)
    ctype = STATIC_TYPES.get(_ext_of(rel))
    if ctype is None:
        return JSONResponse(NOT_FOUND, status_code=404)
    from .apps_loader import _NotFound, _resolve_under
    try:
        f = _resolve_under(ext.static_dir, rel)
        st = f.stat()
    except (_NotFound, OSError):
        return JSONResponse(NOT_FOUND, status_code=404)
    if not f.is_file() or st.st_size > STATIC_MAX_BYTES:
        return JSONResponse(NOT_FOUND, status_code=404)
    etag = _etag(f, st)
    v = request.query_params.get("v")
    immutable = bool(v) and v == ext.assets_version()
    headers = {
        "Cache-Control": CACHE_IMMUTABLE if immutable else "no-cache",
        "ETag": etag,
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": STATIC_CSP,
        "X-Robots-Tag": "noindex",
        "Referrer-Policy": "same-origin",
    }
    inm = request.headers.get("if-none-match", "")
    if inm and etag in [x.strip() for x in inm.split(",")]:
        return Response(status_code=304, headers=headers)
    return FileResponse(f, media_type=ctype, headers=headers)
