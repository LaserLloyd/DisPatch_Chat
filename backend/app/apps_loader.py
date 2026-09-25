"""Apps — trusted, self-contained add-on packages (``apps/<id>/``).

Contract: ``docs/design/2026-09-25-apps.md``; developer guide ``docs/apps.md``.

A **tool** (``tools.py``) is deliberately powerless: a sandboxed page with no
API. An **app** is the opposite: a package of first-party code the operator
trusts — a backend router, a full-page UI, an optional chat-side hook, its own
locale strings and an optional roster bot — loaded INTO this process. So this
module is written around two questions: what exactly does a package get
(``AppContext``, nothing else), and what can a caller who is not the operator
reach (the gate below, nothing else).

Where apps come from:

  * ``<repo>/apps/<id>/app.yaml`` — shipped in git and deployed with the app.
  * ``<DATA_DIR>/apps/<id>/app.yaml`` — only when ``tools.yaml`` carries
    ``{id, kind: app, trusted: true}``. Loading one IS running its Python in
    this server, so the grant lives in the operator's file and the write API
    refuses to introduce it (see ``tools.api_tools_put``).

What loading does, per app: validate the manifest (closed schema), import
``backend.py`` as the package ``dispatch_app_<id>`` (so sibling modules import
relatively), call ``build(ctx)`` and mount the returned router at
``/api/apps/<id>`` AND, if the manifest names one, at ``api.legacy_prefix``.
Any failure is logged and that app is skipped: the shell never fails to boot
because of an app.

Gating, in three layers, each sufficient on its own for Safe Mode:

  1. ``main.auth_gate`` — every app API path is on the machine-inbound surface
     (``is_inbound``: loopback or api_token, never a browser), and every app
     path is in ``_decoy_blocked`` (``decoy_blocked``) unless the app is
     ``safe: true`` and the method is GET/HEAD.
  2. A router-level dependency (``_gate_for``) on every mounted route: the
     uniform decoy 403 for a Safe-Mode caller of a non-safe app, then 404 when
     ``tools.yaml`` switches the app off.
  3. Per route, the app's own ``ctx.require_access`` (operator or on-box
     machine) / ``ctx.require_operator`` (a real operator session only).

This module does not import ``main`` (main mounts it); main hands the few
things an app may reach through :class:`Hooks`.
"""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import logging
import os
import re
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from . import config, localview, tools

log = logging.getLogger("local-chat.apps")

#: backend/app/apps_loader.py → the repository (or install) root's apps/.
REPO_APPS_DIR = Path(__file__).resolve().parents[2] / "apps"
DATA_APPS_DIRNAME = "apps"
DATA_STATE_DIRNAME = "apps-data"
MANIFEST = "app.yaml"
BACKEND = "backend.py"
API_PREFIX = "/api/apps"
STATIC_PREFIX = "/apps"
MODULE_PREFIX = "dispatch_app_"

ID_RE = re.compile(r"\A[a-z0-9-]{1,40}\Z")
# A legacy prefix is an /api/ path of plain lowercase segments. Deliberately
# narrow: it becomes a second mount point AND a machine-inbound prefix.
LEGACY_RE = re.compile(r"\A/api(?:/[a-z0-9][a-z0-9-]{0,39}){1,4}\Z")
BOT_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_-]{0,47}\Z")
AGENT_RE = re.compile(r"\A[a-z0-9][a-z0-9_-]{0,63}\Z")
AVATAR_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
ENV_RE = re.compile(r"\A[A-Z][A-Z0-9_]{0,63}\Z")
LOCALE_RE = re.compile(r"\Alocales/[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})?\.json\Z")
MAX_ENV = 32
DEFAULT_ORDER = 100
DEFAULT_ICON = "🧩"

TOP_KEYS = frozenset({"id", "title", "icon", "safe", "order", "api", "bot",
                      "thread_hook", "env"})
API_KEYS = frozenset({"legacy_prefix"})
BOT_KEYS = frozenset({"id", "name", "emoji", "avatar", "agent", "visible"})

#: Shell namespaces a legacy prefix may never claim, even if no route exists
#: there at mount time (routers mounted later, or path-level gates in main).
RESERVED_LEGACY = ("/api/apps", "/api/tools", "/api/auth", "/api/local",
                   "/api/media", "/api/files")

DECOY_BODY = {"detail": "Unlock for full access", "decoy": True}
NOT_FOUND = {"detail": "Not found"}

CACHE_ASSET = "private, max-age=0, must-revalidate"
CACHE_ENTRY = "private, no-cache"
#: First-party page, NOT sandboxed: an app page is trusted repo code that needs
#: the session cookie to call its own API. Contract, verbatim.
CSP_HTML = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob:; connect-src 'self'; frame-ancestors 'self'")
CSP_OTHER = "default-src 'none'; frame-ancestors 'self'"


# --------------------------------------------------------------------------- #
# Manifest
# --------------------------------------------------------------------------- #

class ManifestError(ValueError):
    def __init__(self, field_name: str, message: str):
        super().__init__(f"{field_name}: {message}")
        self.field = field_name
        self.message = message


@dataclass(frozen=True)
class BotSpec:
    id: str
    name: str
    emoji: str = "🤖"
    avatar: str = ""
    agent: str = ""
    visible: bool = False


@dataclass(frozen=True)
class Manifest:
    id: str
    title: str
    icon: str
    safe: bool
    order: int
    legacy_prefix: str | None
    bot: BotSpec | None
    thread_hook: bool
    env: tuple[str, ...]
    dir: Path
    source: str                      # "repo" | "data"

    @property
    def static_dir(self) -> Path:
        return self.dir / "static"


def _bool(doc: dict, key: str, default: bool, name: str | None = None) -> bool:
    v = doc.get(key, default)
    if not isinstance(v, bool):
        raise ManifestError(name or key, "must be true or false")
    return v


def _text(doc: dict, key: str, default: str, limit: int, name: str | None = None) -> str:
    v = doc.get(key, default)
    if v is None:
        v = default
    if not isinstance(v, str) or not v.strip() or len(v) > limit or "\x00" in v:
        raise ManifestError(name or key, f"must be 1..{limit} characters")
    return v.strip()


def parse_manifest(doc: Any, app_dir: Path, source: str) -> Manifest:
    """A raw ``app.yaml`` document → Manifest. Raises ManifestError(field, msg).

    Closed schema: an unknown key anywhere is refused, so a typo can never
    silently mean "default"."""
    if not isinstance(doc, dict):
        raise ManifestError("app.yaml", "must be a mapping")
    for k in doc:
        if k not in TOP_KEYS:
            raise ManifestError(str(k), "unknown key")
    app_id = doc.get("id")
    if not isinstance(app_id, str) or not ID_RE.match(app_id):
        raise ManifestError("id", "must match [a-z0-9-]{1,40}")
    if app_id != app_dir.name:
        raise ManifestError("id", f"must equal the package directory name {app_dir.name!r}")
    title = _text(doc, "title", app_id, 80)
    icon = _text(doc, "icon", DEFAULT_ICON, 16)
    safe = _bool(doc, "safe", False)
    order = doc.get("order", DEFAULT_ORDER)
    if isinstance(order, bool) or not isinstance(order, int) or not (-10000 <= order <= 10000):
        raise ManifestError("order", "must be an integer -10000..10000")

    legacy = None
    api = doc.get("api")
    if api is not None:
        if not isinstance(api, dict):
            raise ManifestError("api", "must be a mapping")
        for k in api:
            if k not in API_KEYS:
                raise ManifestError(f"api.{k}", "unknown key")
        legacy = api.get("legacy_prefix")
        if legacy is not None:
            if (not isinstance(legacy, str) or not LEGACY_RE.match(legacy)
                    or any(legacy == r or legacy.startswith(r + "/") for r in RESERVED_LEGACY)):
                raise ManifestError("api.legacy_prefix",
                                    "must be an /api/<segment>[/…] path outside the "
                                    "shell's reserved namespaces")

    bot = None
    raw_bot = doc.get("bot")
    if raw_bot is not None:
        if not isinstance(raw_bot, dict):
            raise ManifestError("bot", "must be a mapping")
        for k in raw_bot:
            if k not in BOT_KEYS:
                raise ManifestError(f"bot.{k}", "unknown key")
        bid = raw_bot.get("id")
        if not isinstance(bid, str) or not BOT_ID_RE.match(bid):
            raise ManifestError("bot.id", "must be a bot id ([A-Za-z0-9_-], ≤48)")
        name = _text(raw_bot, "name", title, 80, "bot.name")
        emoji = _text(raw_bot, "emoji", "🤖", 16, "bot.emoji")
        avatar = raw_bot.get("avatar", "") or ""
        if avatar and (not isinstance(avatar, str) or not AVATAR_RE.match(avatar)
                       or ".." in avatar):
            raise ManifestError("bot.avatar", "must be a plain filename in the avatar dir")
        agent = raw_bot.get("agent", "") or ""
        if agent and (not isinstance(agent, str) or not AGENT_RE.match(agent)):
            raise ManifestError("bot.agent", "must be an agent id ([a-z0-9_-], ≤64)")
        bot = BotSpec(id=bid, name=name, emoji=emoji, avatar=avatar, agent=agent,
                      visible=_bool(raw_bot, "visible", False, "bot.visible"))

    thread_hook = _bool(doc, "thread_hook", False)
    if thread_hook:
        if bot is None:
            raise ManifestError("thread_hook", "needs a `bot:` — the hook mounts in that bot's threads")
        if not (app_dir / "static" / "thread.js").is_file():
            raise ManifestError("thread_hook", "static/thread.js does not exist")

    env = doc.get("env", [])
    if env is None:
        env = []
    if (not isinstance(env, list) or len(env) > MAX_ENV
            or not all(isinstance(n, str) and ENV_RE.match(n) for n in env)):
        raise ManifestError("env", f"must be a list of at most {MAX_ENV} UPPER_CASE env names")

    return Manifest(id=app_id, title=title, icon=icon, safe=safe, order=order,
                    legacy_prefix=legacy, bot=bot, thread_hook=thread_hook,
                    env=tuple(dict.fromkeys(env)), dir=app_dir, source=source)


def _read_manifest(app_dir: Path, source: str) -> Manifest | None:
    try:
        doc = yaml.safe_load((app_dir / MANIFEST).read_text(encoding="utf-8"))
        return parse_manifest(doc, app_dir, source)
    except (OSError, yaml.YAMLError, ManifestError, UnicodeDecodeError) as e:
        log.error("app %s: %s cannot be used (%s) — app skipped", app_dir.name, MANIFEST, e)
        return None


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #

def data_apps_dir() -> Path:
    return config.DATA_DIR / DATA_APPS_DIRNAME


def discover(repo_dir: Path | None = None) -> list[Manifest]:
    """Every usable manifest, repo apps first-class, ordered by (order, id).

    A data-dir package is looked at ONLY when tools.yaml trusts its id; a
    data-dir app can never shadow a repo app of the same id."""
    repo_dir = REPO_APPS_DIR if repo_dir is None else repo_dir
    found: dict[str, Manifest] = {}
    with contextlib.suppress(OSError):
        for d in sorted(repo_dir.iterdir()):
            if (d.is_dir() and not d.name.startswith((".", "_"))
                    and (d / MANIFEST).is_file()):
                m = _read_manifest(d, "repo")
                if m is not None:
                    found[m.id] = m
    for row in tools.load_tools():
        if row.kind != "app":
            continue
        d = data_apps_dir() / row.id
        if not row.trusted:
            # A package is on disk and named in tools.yaml, but nobody said it
            # may run. Skipping it is right; skipping it SILENTLY left the
            # operator guessing why the app never appeared.
            if row.id not in found and (d / MANIFEST).is_file():
                log.warning("app %s: %s exists but its tools.yaml row lacks "
                            "`trusted: true` — data-dir app not loaded", row.id, d / MANIFEST)
            continue
        if not (d / MANIFEST).is_file():
            log.warning("app %s: trusted in tools.yaml but %s has no %s", row.id, d, MANIFEST)
            continue
        if row.id in found:
            log.error("app %s: a data-dir package may not shadow the repo app of the "
                      "same id — data-dir copy ignored", row.id)
            continue
        m = _read_manifest(d, "data")
        if m is not None:
            found[m.id] = m
    return sorted(found.values(), key=lambda m: (m.order, m.id))


# --------------------------------------------------------------------------- #
# Context handed to an app
# --------------------------------------------------------------------------- #

class AppDecoy(HTTPException):
    """The uniform Safe-Mode refusal. An HTTPException subclass so a direct
    call raises something ordinary; the handler :func:`load_all` registers turns
    it into the same ``{detail, decoy: true}`` body the auth gate sends."""

    def __init__(self) -> None:
        super().__init__(403, DECOY_BODY["detail"])


def _is_decoy(request: Request) -> bool:
    return bool(getattr(request.state, "decoy", False))


def _is_machine(request: Request) -> bool:
    return bool(getattr(request.state, "machine", False))


def require_operator(request: Request) -> None:
    """A real operator: a live PIN session, or anyone when no PIN exists.
    Never the machine-inbound tier, never Safe Mode."""
    if not tools._is_operator(request):
        raise AppDecoy


def require_access(request: Request) -> None:
    """The operator OR an authenticated on-box machine (loopback / api_token —
    the auth gate decided that and stamped ``request.state.machine``)."""
    if _is_decoy(request):
        raise AppDecoy
    if _is_machine(request) or tools._is_operator(request):
        return
    raise AppDecoy


@dataclass
class Hooks:
    """What main lends the loader. Getters, not values, so a test that swaps
    ``main.db`` (or ``main.manager``) is seen by every app at call time."""
    db: Callable[[], Any] = lambda: None
    broadcast: Callable[[dict], Awaitable[None]] | None = None
    dispatch_turn: Callable[[str, str, str], bool] | None = None


@dataclass
class AppContext:
    """Everything an app's ``build(ctx)`` gets from the shell — and all of it.

    Fields per the contract: ``app_id``, ``data_dir`` (property), ``db``
    (property), ``config``, ``require_operator``, ``require_access``,
    ``broadcast``, ``bot_id``, ``env``, ``log``. Added for the Job Board (see
    the contract's AppContext note): ``broadcast_message`` and
    ``dispatch_turn`` — posting into a thread and starting the bot's turn are
    shell behaviours an app must not reimplement.
    """
    app_id: str
    config: Any
    require_operator: Callable[[Request], None]
    require_access: Callable[[Request], None]
    bot_id: str | None
    env: dict[str, str]
    log: logging.Logger
    _hooks: Hooks = field(repr=False, default_factory=Hooks)

    @property
    def data_dir(self) -> Path:
        """``<DATA_DIR>/apps-data/<id>/``, created 0700 on first use. Computed
        from the CURRENT DATA_DIR (never frozen at import), so a test or a
        re-pointed install never writes into the wrong tree."""
        d = self.config.DATA_DIR / DATA_STATE_DIRNAME / self.app_id
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        with contextlib.suppress(OSError):
            os.chmod(d, 0o700)
        return d

    @property
    def db(self) -> Any:
        return self._hooks.db()

    async def broadcast(self, frame: dict) -> None:
        """WS broadcast. The type MUST be ``app:<id>:…``; Safe-Mode sockets
        never receive it (the decoy redactor is an allowlist)."""
        t = frame.get("type") if isinstance(frame, dict) else None
        if not isinstance(t, str) or not t.startswith(f"app:{self.app_id}:"):
            raise ValueError(f"app frame type must start with 'app:{self.app_id}:'")
        if self._hooks.broadcast is not None:
            await self._hooks.broadcast(frame)

    async def broadcast_message(self, thread_id: str, bot_id: str, message: Any) -> None:
        """The shell's own ``message`` frame for a message the app persisted
        with ``ctx.db.add_message`` — so open chats show it live, exactly as a
        typed message would appear."""
        body = message.model_dump() if hasattr(message, "model_dump") else message
        if self._hooks.broadcast is not None:
            await self._hooks.broadcast({"type": "message", "thread_id": thread_id,
                                         "bot_id": bot_id, "message": body})

    def dispatch_turn(self, thread_id: str, bot_id: str, text: str) -> bool:
        """Start ``bot_id``'s agent turn for ``text`` in ``thread_id``
        (fire-and-forget). False = nothing to dispatch to (no such bot, no
        agent backend); the caller's own writes stand either way."""
        if self._hooks.dispatch_turn is None:
            return False
        return bool(self._hooks.dispatch_turn(thread_id, bot_id, text))


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #

@dataclass
class LoadedApp:
    manifest: Manifest
    module: Any = None
    ctx: AppContext | None = None
    mounted: bool = False
    prefixes: tuple[str, ...] = ()
    error: str | None = None
    routes: list = field(default_factory=list, repr=False)


_registry: dict[str, LoadedApp] = {}


def get(app_id: str) -> LoadedApp | None:
    return _registry.get(app_id)


def loaded() -> list[LoadedApp]:
    """Every app with a valid manifest, mounted or not, in rail order."""
    return sorted(_registry.values(), key=lambda la: (la.manifest.order, la.manifest.id))


def mounted() -> list[LoadedApp]:
    return [la for la in loaded() if la.mounted]


def app_ids() -> set[str]:
    return set(_registry)


def enabled(app_id: str) -> bool:
    """The tools.yaml switch; no row = on."""
    t = tools.get_tool(app_id)
    if t is not None and t.kind == "app":
        return t.enabled
    return True


def hook_version(la: LoadedApp) -> str | None:
    if not la.manifest.thread_hook:
        return None
    try:
        st = (la.manifest.static_dir / "thread.js").stat()
    except OSError:
        return None
    return hashlib.sha1(f"{st.st_mtime_ns}:{st.st_size}".encode()).hexdigest()[:10]


# --------------------------------------------------------------------------- #
# Path classification (used by main's auth gate)
# --------------------------------------------------------------------------- #

def _app_for_path(path: str) -> tuple[str, str | None] | None:
    """(kind, app_id) for an app-shaped path, else None. kind: api|static|legacy.
    An app id of None means the path is app-shaped but names no app."""
    for base, kind in ((API_PREFIX, "api"), (STATIC_PREFIX, "static")):
        if path == base or path.startswith(base + "/"):
            seg = path[len(base) + 1:].split("/", 1)[0] if path != base else ""
            return kind, (seg if seg in _registry else None)
    for la in _registry.values():
        p = la.manifest.legacy_prefix
        if la.mounted and p and p in la.prefixes and (path == p or path.startswith(p + "/")):
            return "legacy", la.manifest.id
    return None


def is_inbound(method: str, path: str) -> bool:
    """A MOUNTED app's API (either prefix) is on the machine-inbound surface:
    an on-box agent reaches it without a session, and each route's
    require_access / require_operator decides from there."""
    hit = _app_for_path(path)
    if hit is None or hit[0] == "static" or hit[1] is None:
        return False
    la = _registry.get(hit[1])
    return bool(la and la.mounted)


def decoy_blocked(method: str, path: str) -> bool | None:
    """Safe-Mode rule for app paths: None = not an app path. Blocked unless the
    app is ``safe: true`` and the method only reads."""
    hit = _app_for_path(path)
    if hit is None:
        return None
    la = _registry.get(hit[1]) if hit[1] else None
    if la is not None and la.manifest.safe and method in ("GET", "HEAD"):
        return False
    return True


# --------------------------------------------------------------------------- #
# Loading and mounting
# --------------------------------------------------------------------------- #

def _module_name(app_id: str) -> str:
    return MODULE_PREFIX + app_id.replace("-", "_")


def _drop_modules(name: str) -> None:
    for k in [k for k in sys.modules if k == name or k.startswith(name + ".")]:
        sys.modules.pop(k, None)


def _import_backend(m: Manifest):
    """Import ``backend.py`` as the package ``dispatch_app_<id>`` whose search
    path is the app directory, so ``from . import helper`` finds siblings."""
    name = _module_name(m.id)
    path = m.dir / BACKEND
    if not path.is_file():
        raise ImportError(f"{BACKEND} not found")
    spec = importlib.util.spec_from_file_location(
        name, path, submodule_search_locations=[str(m.dir)])
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
    except BaseException:
        _drop_modules(name)
        raise
    return mod


def _gate_for(app_id: str) -> Callable[[Request], None]:
    def gate(request: Request) -> None:
        la = _registry.get(app_id)
        safe_read = bool(la and la.manifest.safe) and request.method in ("GET", "HEAD")
        if _is_decoy(request):
            if not safe_read:
                raise AppDecoy
        elif not (_is_machine(request) or tools._is_operator(request) or safe_read):
            raise AppDecoy
        if la is None or not la.mounted or not enabled(app_id):
            raise HTTPException(404, NOT_FOUND["detail"])
    return gate


def _route_paths(app: FastAPI) -> list[str]:
    return [p for p in (getattr(r, "path", None) for r in app.router.routes) if p]


def _legacy_collides(app: FastAPI, prefix: str, own: list) -> bool:
    for r in app.router.routes:
        if any(r is o for o in own):
            continue
        p = getattr(r, "path", "") or ""
        if p == prefix or p.startswith((prefix + "/", prefix + "{")):
            return True
    return any(la.manifest.legacy_prefix == prefix and la.mounted and prefix in la.prefixes
               for la in _registry.values())


async def _decoy_handler(request: Request, exc: AppDecoy):
    return JSONResponse(DECOY_BODY, status_code=403)


def _wire(app: FastAPI) -> None:
    if getattr(app.state, "dispatch_apps_wired", False):
        return
    app.add_exception_handler(AppDecoy, _decoy_handler)
    app.include_router(static_router)
    app.state.dispatch_apps_wired = True


def _bot_ids_lower() -> set[str]:
    with contextlib.suppress(Exception):
        return {b.id.lower() for b in config.load_bots()}
    return set()


def _conflict(m: Manifest) -> str | None:
    if m.id in tools.BUILTINS:
        return "id is a builtin tool id"
    own_bot = m.bot.id.lower() if m.bot else None
    if m.id.lower() in _bot_ids_lower() and m.id.lower() != own_bot:
        return "id is already another bot's id"
    for t in tools.load_tools():
        if t.id == m.id and t.kind != "app":
            return f"id is already a {t.kind} tool in tools.yaml"
    return None


def _make_context(m: Manifest, hooks: Hooks) -> AppContext:
    return AppContext(
        app_id=m.id, config=config,
        require_operator=require_operator, require_access=require_access,
        bot_id=m.bot.id if m.bot else None,
        env={n: os.environ[n] for n in m.env if n in os.environ},
        log=logging.getLogger(f"local-chat.app.{m.id}"),
        _hooks=hooks)


def load_one(app: FastAPI, m: Manifest, hooks: Hooks) -> LoadedApp:
    """Import, build and mount one app. Never raises."""
    la = LoadedApp(manifest=m)
    _registry[m.id] = la
    why = _conflict(m)
    if why:
        la.error = why
        log.error("app %s: refused (%s)", m.id, why)
        return la
    try:
        mod = _import_backend(m)
        build = getattr(mod, "build", None)
        if not callable(build):
            raise TypeError("backend.py defines no build(ctx)")
        ctx = _make_context(m, hooks)
        router = build(ctx)
        if not isinstance(router, APIRouter):
            raise TypeError(f"build(ctx) returned {type(router).__name__}, not an APIRouter")
    except BaseException as e:           # an app must never take the shell down
        if isinstance(e, (KeyboardInterrupt, SystemExit)):
            raise
        _drop_modules(_module_name(m.id))
        la.error = f"{type(e).__name__}: {e}"
        log.exception("app %s: failed to load — app skipped", m.id)
        return la

    dep = [Depends(_gate_for(m.id))]
    before = list(app.router.routes)
    prefixes = [f"{API_PREFIX}/{m.id}"]
    try:
        app.include_router(router, prefix=prefixes[0], dependencies=dep)
        if m.legacy_prefix:
            own = [r for r in app.router.routes if not any(r is b for b in before)]
            if _legacy_collides(app, m.legacy_prefix, own):
                log.error("app %s: api.legacy_prefix %s collides with an existing route "
                          "— mounted at %s only", m.id, m.legacy_prefix, prefixes[0])
            else:
                app.include_router(router, prefix=m.legacy_prefix, dependencies=dep)
                prefixes.append(m.legacy_prefix)
    except Exception as e:
        app.router.routes[:] = [r for r in app.router.routes if any(r is b for b in before)]
        _drop_modules(_module_name(m.id))
        la.error = f"{type(e).__name__}: {e}"
        log.exception("app %s: router could not be mounted — app skipped", m.id)
        return la
    la.routes = [r for r in app.router.routes if not any(r is b for b in before)]
    la.module, la.ctx, la.mounted, la.prefixes = mod, ctx, True, tuple(prefixes)
    log.info("app %s mounted at %s (%s)", m.id, ", ".join(prefixes), m.source)
    return la


def load_all(app: FastAPI, *, hooks: Hooks, repo_dir: Path | None = None) -> list[LoadedApp]:
    """Discover and mount every app not already mounted. Returns the apps this
    call looked at (newly mounted or refused), in load order."""
    _wire(app)
    out: list[LoadedApp] = []
    for m in discover(repo_dir):
        cur = _registry.get(m.id)
        if cur is not None and cur.mounted:
            continue
        out.append(load_one(app, m, hooks))
    return out


def unload(app: FastAPI, app_id: str) -> None:
    """Tests: remove an app's routes, module and registry entry."""
    la = _registry.pop(app_id, None)
    if la is None:
        return
    app.router.routes[:] = [r for r in app.router.routes
                            if not any(r is o for o in la.routes)]
    _drop_modules(_module_name(app_id))


# --------------------------------------------------------------------------- #
# Bot provisioning (startup)
# --------------------------------------------------------------------------- #

def provision_bots() -> list[str]:
    """Append each mounted app's ``bot:`` to the roster if absent — through the
    one serializer (``config._bot_entry`` / ``_write_bots``). A present row is
    never touched: the operator owns it. Returns the ids added."""
    added: list[str] = []
    for la in mounted():
        spec = la.manifest.bot
        if spec is None:
            continue
        bots = config.load_bots()
        if any(b.id.lower() == spec.id.lower() for b in bots):
            continue
        new = config.Bot(id=spec.id, name=spec.name, emoji=spec.emoji,
                         avatar=spec.avatar, agent=spec.agent, visible=spec.visible,
                         order=max((b.order for b in bots), default=-1) + 1)
        config._write_bots([config._bot_entry(b) for b in bots] + [config._bot_entry(new)])
        added.append(spec.id)
        log.info("app %s: added its bot %s to the roster (visible=%s)",
                 la.manifest.id, spec.id, spec.visible)
    return added


# --------------------------------------------------------------------------- #
# Static serving: /apps/<id>/ and /apps/<id>/{path}
# --------------------------------------------------------------------------- #

class _NotFound(Exception):
    pass


def _resolve_under(root: Path, rel: str) -> Path:
    """``rel`` under ``root`` or _NotFound — same guards as tools.resolve_static
    (lexical refusal of dot components, strict resolve, containment after
    symlinks, hidden + secret-pattern names, the built-in deny roots), minus
    the data-dir refusal: a trusted data-dir app's own static tree IS under
    DATA_DIR by design."""
    if "\x00" in rel or "\\" in rel or rel.startswith("/"):
        raise _NotFound
    parts = [p for p in rel.split("/") if p]
    if any(p in (".", "..") or p.startswith(".") for p in parts):
        raise _NotFound
    try:
        base = root.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        raise _NotFound from None
    if not base.is_dir():
        raise _NotFound
    target = base.joinpath(*parts) if parts else base
    if localview._pattern_denied(base, target):
        raise _NotFound
    try:
        resolved = target.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        raise _NotFound from None
    if not localview._under(resolved, base):
        raise _NotFound
    if localview._hidden_below(base, resolved) or localview._pattern_denied(base, resolved):
        raise _NotFound
    if tools._in_deny_roots(resolved):
        raise _NotFound
    return resolved


def _static_file(m: Manifest, rel: str) -> Path | str:
    if LOCALE_RE.match(rel):
        # Locale files live beside static/, not in it (contract layout); the
        # SDK fetches them at /apps/<id>/locales/<lang>.json.
        resolved = _resolve_under(m.dir / "locales", rel.split("/", 1)[1])
    else:
        resolved = _resolve_under(m.static_dir, rel or "index.html")
        if resolved.is_dir():
            if rel and not rel.endswith("/"):
                return "redirect"
            resolved = _resolve_under(m.static_dir, f"{rel.strip('/')}/index.html"
                                      if rel.strip("/") else "index.html")
    if not resolved.is_file():
        raise _NotFound
    try:
        if resolved.stat().st_size > tools.MAX_FILE_BYTES:
            raise _NotFound
    except OSError:
        raise _NotFound from None
    return resolved


def static_headers(resolved_name: str, *, entry: bool) -> dict[str, str]:
    html = localview.viewer_kind(resolved_name) == "html"
    return {
        "Content-Security-Policy": CSP_HTML if html else CSP_OTHER,
        "Cache-Control": CACHE_ENTRY if entry or resolved_name == "index.html" else CACHE_ASSET,
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "same-origin",
        "X-Robots-Tag": "noindex",
    }


static_router = APIRouter()


@static_router.get("/apps/{app_id}", include_in_schema=False)
def app_static_bare(app_id: str):
    return RedirectResponse(f"/apps/{quote(app_id)}/", status_code=307)


@static_router.api_route("/apps/{app_id}/{rel:path}", methods=["GET", "HEAD"],
                         include_in_schema=False)
def app_static(request: Request, app_id: str, rel: str):
    la = _registry.get(app_id)
    safe = bool(la and la.manifest.safe)
    if (_is_decoy(request) or not tools._is_operator(request)) and not safe:
        return JSONResponse(DECOY_BODY, status_code=403)
    if la is None or not la.mounted or not enabled(app_id):
        return JSONResponse(NOT_FOUND, status_code=404)
    try:
        f = _static_file(la.manifest, rel)
    except _NotFound:
        return JSONResponse(NOT_FOUND, status_code=404)
    if f == "redirect":
        return RedirectResponse(request.url.path + "/", status_code=307)
    ctype = localview.content_type(f.name)
    headers = static_headers(f.name, entry=(rel == "" or rel.endswith("/")))
    if ctype == localview.OCTET:
        headers["Content-Disposition"] = f'attachment; filename="{f.name.replace(chr(34), "")}"'
    return FileResponse(f, media_type=ctype, headers=headers)
