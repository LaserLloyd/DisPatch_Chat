"""Tools as manifests: one description of everything you can install, whatever it is.

DisPatch has four ways to add a tool - a directory served as a page (``tools.yaml`` ``static``), a
URL framed in the rail (``url``), a trusted package under ``apps/<id>/`` (``app.yaml``) and a
separately installed add-on (the ``dispatch.extensions`` entry point, which may register
model-callable tools with ``register_live_tools``). 2.x kept four vocabularies. This module is the
**back-compat shim**: it reads each of them as it is and produces the one
``contracts/manifests/tool-plugin`` document - ``id, name, version, publisher, entry, permission
(the one a profile needs to use it), permissions_requested, settings fragment, min_app_version`` -
without changing any of the four loaders.

What it adds:

* ``GET /api/plugins`` gains ``manifests`` (with the permissions each asks for);
* **approval**: a plugin that was not running when 3.0 first started must be approved - the person
  with ``manage_plugins`` sees the permissions it requests and confirms them - before the Plugins
  switch will turn it on (``check_enable``). Plugins already running at the upgrade are
  grandfathered (``source: upgrade``). Loading itself is the extension host's business and is not
  changed here;
* **settings fragments**: an app's ``env:`` names, and any ``settings`` a plugin declares, appear in
  Settings under ``tools.<id>.`` (``settings_schema.register``); an app reads them through
  ``ctx.env`` exactly as before, whether the value comes from the environment or from Settings.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from . import __version__, apps_loader, config, extensions, settings_schema, tools
from .contracts.manifests import ToolPluginManifest

log = logging.getLogger("local-chat.toolmanifests")

SCHEMA = """
CREATE TABLE IF NOT EXISTS plugin_approvals (
    plugin_id   TEXT PRIMARY KEY,
    permissions TEXT NOT NULL DEFAULT '[]',   -- JSON: what was approved
    approved_by TEXT NOT NULL DEFAULT '',
    approved_at TEXT NOT NULL,
    source      TEXT NOT NULL DEFAULT 'user'  -- user | upgrade
);
"""
TABLES = ("plugin_approvals",)
_lock = threading.RLock()
_ready: set[str] = set()

#: Plain-language text for each requested-permission id (the UI shows these).
PERMISSION_LABELS: dict[str, str] = {
    "http_routes": "Add its own web API to this server",
    "websocket": "Open live (WebSocket) connections",
    "ui": "Add its own pages and scripts to the app",
    "live_tools": "Give characters tools they can call (lookups, actions)",
    "panes": "Add tiles to the left rail",
    "character_fields": "Store its own settings on each character (e.g. a voice)",
    "background_work": "Run work at start-up and shutdown",
    "large_uploads": "Accept large uploads",
    "run_code": "Run its own code inside this server (a trusted app package)",
    "character": "Add a character to the roster",
    "thread_hook": "Add a panel inside that character's chats",
}


class PluginError(Exception):
    def __init__(self, code: str, message: str, status: int = 400, **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.extra = extra


def describe_permission(pid: str) -> str:
    if pid.startswith("network:"):
        return f"Contact {pid[8:]}"
    if pid.startswith("env:"):
        return f"Read the setting {pid[4:]}"
    return PERMISSION_LABELS.get(pid, pid)


# --------------------------------------------------------------------------- #
# Storage (approvals)
# --------------------------------------------------------------------------- #


@contextlib.contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    path = Path(config.DATA_DIR) / "chats.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), timeout=5.0, isolation_level=None)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA busy_timeout=5000")
        key = str(path)
        if key not in _ready:
            con.execute("PRAGMA journal_mode=WAL")
            con.executescript(SCHEMA)
            _ready.add(key)
        yield con
    finally:
        con.close()


def reset_schema_cache() -> None:
    """Forget which database files already have this module's tables (after a restore swapped the
    file under us)."""
    with _lock:
        _ready.clear()


def reset_for_tests() -> None:
    with _lock:
        _ready.clear()


def _now() -> str:
    from datetime import UTC, datetime
    return datetime.now(UTC).isoformat(timespec="seconds")


def approvals() -> dict[str, dict]:
    """plugin id -> its approval. Read-only: never creates the table (the migration's dry run calls
    this and must write nothing)."""
    p = Path(config.DATA_DIR) / "chats.db"
    if not p.is_file():
        return {}
    try:
        con = sqlite3.connect(p.resolve().as_uri() + "?mode=ro", uri=True, timeout=2.0)
        con.row_factory = sqlite3.Row
        try:
            return {r["plugin_id"]: {"permissions": json.loads(r["permissions"] or "[]"),
                                     "approved_by": r["approved_by"], "approved_at": r["approved_at"],
                                     "source": r["source"]}
                    for r in con.execute("SELECT * FROM plugin_approvals")}
        finally:
            con.close()
    except (sqlite3.Error, ValueError):
        return {}


def approve(plugin_id: str, permissions: list[str], by: str = "", source: str = "user") -> None:
    with _lock, connect() as con:
        con.execute("INSERT INTO plugin_approvals (plugin_id, permissions, approved_by, approved_at, source) "
                    "VALUES (?,?,?,?,?) ON CONFLICT(plugin_id) DO UPDATE SET permissions=excluded.permissions, "
                    "approved_by=excluded.approved_by, approved_at=excluded.approved_at, source=excluded.source",
                    (plugin_id, json.dumps(sorted(permissions)), by, _now(), source))


def grandfather_running() -> int:
    """Approve (``source: upgrade``) every plugin that is running now and has no approval row. Run
    once by migration step 007 so an upgrade never switches a working add-on off."""
    have = approvals()
    off = extensions.user_disabled()
    n = 0
    for row in list_manifests():
        pid = row["manifest"]["id"]
        if pid in have or row["state"] in ("failed", "incompatible") or pid in off:
            continue
        approve(pid, row["manifest"]["permissions_requested"], by="", source="upgrade")
        n += 1
    return n


# --------------------------------------------------------------------------- #
# The shim: four sources -> one manifest
# --------------------------------------------------------------------------- #

_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+([-+][0-9A-Za-z.-]+)?$")


def _semver(text: str, default: str = "0.0.0") -> str:
    m = re.match(r"^(\d+)(?:\.(\d+))?(?:\.(\d+))?", (text or "").strip())
    if not m:
        return default
    return f"{m.group(1)}.{m.group(2) or 0}.{m.group(3) or 0}"


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9_-]+", "-", (text or "").lower()).strip("-_")[:48]
    return s if re.match(r"^[a-z0-9]", s or "") else ("x" + s)[:48]


def _extension_manifest(e: extensions.Extension) -> dict:
    req: list[str] = []
    if e.routes or e.prefixes:
        req.append("http_routes")
    if e.websockets:
        req.append("websocket")
    if e.static_dir is not None or e.entry:
        req.append("ui")
    if e.live_tools:
        req.append("live_tools")
    if e.tools:
        req.append("panes")
    if e.validators:
        req.append("character_fields")
    if e.startup or e.shutdown:
        req.append("background_work")
    if e.body_limits:
        req.append("large_uploads")
    return {
        "id": _slug(e.id), "name": (e.name or e.id)[:80], "version": _semver(e.version),
        "kind": "extension", "description": (e.detail or "")[:400] if e.state != "ok" else "",
        "entry": f"dispatch.extensions:{e.id}", "permission": "use_voice" if e.id == "voice" else "use_tools",
        "restricted_ok": False, "api_version": int(e.api[0]) if e.api and e.api[0] else 1,
        "network": [], "license": "MIT", "settings": [],
        "publisher": "", "permissions_requested": req, "min_app_version": None,
    }


def _app_manifest(m: apps_loader.Manifest) -> dict:
    req = ["http_routes", "ui", "run_code"]
    if m.bot is not None:
        req.append("character")
    if m.thread_hook:
        req.append("thread_hook")
    req += [f"env:{n}" for n in m.env]
    fragment = [_env_field(n) for n in m.env]
    return {
        "id": m.id, "name": m.title[:80], "version": _semver(__version__) if m.source == "repo" else "0.0.0",
        "kind": "app", "description": "", "entry": f"apps/{m.id}/backend.py", "permission": m.permission,
        "restricted_ok": bool(m.safe), "api_version": 1, "network": [], "license": "MIT",
        "settings": fragment, "publisher": "DisPatch" if m.source == "repo" else "",
        "permissions_requested": req, "min_app_version": None,
    }


def _env_field(name: str) -> dict:
    secretish = bool(re.search(r"(KEY|TOKEN|SECRET|PASSWORD)$", name))
    return {"key": name.lower()[:48], "type": "secret" if secretish else "string",
            "label": name, "description": f"Passed to the app as the environment variable {name}.",
            "default": None, "choices": [], "advanced": True, "env": name}


def _tool_row_manifest(t: tools.Tool) -> dict | None:
    if t.kind == "static":
        return {"id": _slug(t.id), "name": (t.title or t.id)[:80], "version": "0.0.0", "kind": "static",
                "description": "", "entry": t.entry, "permission": "use_tools", "restricted_ok": bool(t.safe),
                "api_version": 1, "network": [], "license": "MIT", "settings": [], "publisher": "",
                "permissions_requested": ["ui"], "min_app_version": None}
    if t.kind == "url":
        from urllib.parse import urlparse
        host = urlparse(t.url or "").hostname or ""
        return {"id": _slug(t.id), "name": (t.title or t.id)[:80], "version": "0.0.0", "kind": "url",
                "description": "", "entry": t.url, "permission": "use_tools", "restricted_ok": bool(t.safe),
                "api_version": 1, "network": [host] if host else [], "license": "MIT", "settings": [],
                "publisher": "", "permissions_requested": [f"network:{host}"] if host else [],
                "min_app_version": None}
    return None


def _validated(raw: dict, origin: str) -> dict | None:
    try:
        return ToolPluginManifest.model_validate(raw).model_dump(by_alias=True)
    except ValidationError as e:
        log.warning("plugin %s (%s) does not fit the tool manifest: %s", raw.get("id"), origin, e.errors()[:1])
        return None


def list_manifests() -> list[dict]:
    """``[{"manifest": <tool-plugin manifest>, "state", "enabled", "source", "approved", ...}]``
    for every extension, app and tools.yaml page/URL row."""
    out: list[dict] = []
    off = extensions.user_disabled()
    appr = approvals()

    def add(raw: dict | None, origin: str, *, state: str, enabled: bool, source: str) -> None:
        if raw is None:
            return
        m = _validated(raw, origin)
        if m is None:
            return
        a = appr.get(m["id"])
        needs = (not a) and bool(m["permissions_requested"]) and m["kind"] in ("extension", "app")
        out.append({"manifest": m, "state": state, "enabled": enabled, "source": source,
                    "approved": a is not None, "needs_approval": needs,
                    "approval": a, "permissions": [{"id": p, "label": describe_permission(p)}
                                                   for p in m["permissions_requested"]]})

    for e in extensions.registry():
        add(_extension_manifest(e), f"extension {e.id}", state=e.state, enabled=e.id not in off,
            source=e.source or "entry point")
    try:
        discovered = apps_loader.discover()
    except Exception:
        log.exception("app discovery failed while listing manifests")
        discovered = []
    mounted = {la.manifest.id for la in apps_loader.mounted()}
    for m in discovered:
        add(_app_manifest(m), f"app {m.id}", state="ok" if m.id in mounted else "off", enabled=m.id in mounted,
            source=m.source)
    try:
        rows = tools.load_tools()
    except Exception:
        rows = []
    for t in rows:
        add(_tool_row_manifest(t), f"tool {t.id}", state="ok", enabled=bool(t.enabled), source="tools.yaml")
    return out


def get_manifest(plugin_id: str) -> dict | None:
    return next((r for r in list_manifests() if r["manifest"]["id"] == plugin_id), None)


def check_enable(plugin_id: str, approve_list: list[str] | None) -> None:
    """PluginError ``plugin_needs_approval`` (409) when turning ``plugin_id`` ON requires a
    confirmation it has not had. ``approve_list`` must equal what the plugin requests NOW."""
    row = get_manifest(plugin_id)
    if row is None or not row["manifest"]["permissions_requested"] or row["approved"]:
        return
    req = sorted(row["manifest"]["permissions_requested"])
    if approve_list is not None and sorted(approve_list) == req:
        return
    raise PluginError(
        "plugin_needs_approval",
        f"{row['manifest']['name']} asks to: " + "; ".join(describe_permission(p) for p in req)
        + ". Review that and enable it again, confirming the permissions.", 409,
        requested=req, permissions=row["permissions"])


# --------------------------------------------------------------------------- #
# Settings fragments -> the schema store
# --------------------------------------------------------------------------- #


def register_fragments() -> int:
    """Put every plugin's settings fragment into the schema under ``tools.<id>.``. Idempotent."""
    n = 0
    for row in list_manifests():
        m = row["manifest"]
        if not m["settings"]:
            continue
        owner = f"tool:{m['id']}"
        try:
            settings_schema.unregister(owner)
            defs = settings_schema.from_fragment(m["settings"], namespace=f"tools.{m['id']}", owner=owner)
            settings_schema.register(defs, owner)
            n += len(defs)
        except (ValueError, ValidationError) as e:
            log.warning("settings of plugin %s were not registered: %s", m["id"], e)
    if n:
        config.invalidate_stored_env()
    return n


def app_env(m: apps_loader.Manifest) -> dict[str, str]:
    """The ``ctx.env`` an app gets: each variable it declared, from the process environment or,
    failing that, from Settings (``tools.<app>.<name>``)."""
    import os
    if m.env:
        with contextlib.suppress(Exception):
            owner = f"tool:{m.id}"
            if not any(k.startswith(f"tools.{m.id}.") for k in settings_schema._plugin_keys.get(owner, ())):
                settings_schema.register(
                    settings_schema.from_fragment([_env_field(n) for n in m.env],
                                                  namespace=f"tools.{m.id}", owner=owner), owner)
                config.invalidate_stored_env()
    from . import secret_store
    out: dict[str, str] = {}
    for n in m.env:
        v = os.environ.get(n)
        if v is None:
            v = config.env_raw(n, "") or secret_store.get(f"setting.tools.{m.id}.{n.lower()[:48]}") or None
        if v is not None:
            out[n] = v
    return out
