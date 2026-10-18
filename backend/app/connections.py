"""Connections: one record per model server DisPatch talks to.

2.x kept the provider, URL and API key INSIDE every character (``api:`` / ``advisor.providers``
in ``config.yaml``), so the same key was pasted per character and "which server does Atlas use?"
had no answer you could point at. 3.0 gives each server one record:

``id``                a stable slug (``openai``, ``studioforge``, ``lmstudio-2``)
``type``              openai_compatible | studioforge | lmstudio | ollama | openai | anthropic |
                      deepseek | xai | minimax | openrouter | groq | mistral | together
``base_url``          validated (http/https, no credentials in the URL) and SSRF-checked
``credential_ref``    ``secret:<name>`` (the secret store - keyring or a 0600 file), ``env:<NAME>``
                      (read from the environment at use time) or empty. The key itself is never
                      returned by any route and never written to ``config.yaml``
``capabilities``      chat, vision, images, embeddings, tools, agent
``required_permission``  what a profile needs to change the connection (``manage_connections``)
``models`` + ``models_checked_at``  the model list the last Test returned (a cache, not a source)

A character refers to a connection with ``connection: <id>`` inside its provider entry. The inline
``provider`` / ``base_url`` / ``model`` stay readable for one release (so a 2.x ``config.yaml`` and
anything that still reads it keep working); only the API key moves into the secret store.

Safety: a target URL that is link-local (169.254.0.0/16, fe80::/10) or a cloud metadata name is
refused unless the admin sets ``connections.allow_private_targets``. Loopback and ordinary LAN
addresses are allowed - a model server on this computer or the next shelf is the point.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import logging
import os
import re
import socket
import sqlite3
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from . import config, secret_store, settings_store
from .contracts.manifests import ConnectionManifest

log = logging.getLogger("local-chat.connections")

ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
CAPABILITIES = ("chat", "vision", "images", "embeddings", "tools", "agent")
_ENV_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_ENV_SUFFIX = re.compile(r"(?:_KEY|_TOKEN|_SECRET|_PASSWORD|_CREDENTIALS?)$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS connections (
    id                  TEXT PRIMARY KEY,
    name                TEXT NOT NULL,
    type                TEXT NOT NULL,
    base_url            TEXT NOT NULL,
    credential_ref      TEXT NOT NULL DEFAULT '',
    capabilities        TEXT NOT NULL DEFAULT '[]',     -- JSON list
    required_permission TEXT NOT NULL DEFAULT 'manage_connections',
    default_model       TEXT NOT NULL DEFAULT '',
    models              TEXT NOT NULL DEFAULT '[]',     -- JSON list (cache of the last Test)
    models_checked_at   TEXT,
    last_test           TEXT NOT NULL DEFAULT '{}',     -- JSON: the last Test's verdict
    created             TEXT NOT NULL,
    updated             TEXT NOT NULL
);
"""

TABLES = ("connections",)
_lock = threading.RLock()
_ready: set[str] = set()


class ConnectionError_(Exception):
    """A rejected connection operation. ``code`` is a catalogued error code."""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


# --------------------------------------------------------------------------- #
# Types
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ConnType:
    id: str
    label: str
    provider: str                    # the llm_api.PROVIDERS id that speaks to it
    protocol: str                    # openai | anthropic | lmstudio | ollama | custom
    base_url: str
    key: str = "optional"            # required | optional | none
    key_env: str = ""
    local: bool = False
    editable_base_url: bool = False
    capabilities: tuple[str, ...] = ("chat", "tools")
    docs: str = ""
    models: tuple[str, ...] = ()
    discover: tuple[str, ...] = ()   # probe URLs for auto-detection

    def public(self) -> dict:
        return {"id": self.id, "label": self.label, "protocol": self.protocol,
                "base_url": self.base_url, "key": self.key, "key_env": self.key_env,
                "local": self.local, "editable_base_url": self.editable_base_url,
                "capabilities": list(self.capabilities), "docs": self.docs,
                "models": list(self.models)}

    def manifest(self) -> ConnectionManifest:
        """The type as the frozen Connection manifest (contracts/manifests)."""
        return ConnectionManifest.model_validate({
            "id": self.id.replace("_", "-"), "name": self.label, "kind": "llm",
            "description": f"{self.label} ({self.protocol} protocol)",
            "base_url": self.base_url or None,
            "auth": "none" if self.key == "none" else "api_key",
            "api_key_env": self.key_env or None,
            "protocol": self.protocol if self.protocol in (
                "openai", "anthropic", "lmstudio", "ollama") else "custom",
            "discover": list(self.discover), "capabilities": list(self.capabilities)})


def _types() -> dict[str, ConnType]:
    def t(*a: Any, **k: Any) -> ConnType:
        return ConnType(*a, **k)
    full = ("chat", "vision", "embeddings", "tools")
    rows = [
        t("openai_compatible", "OpenAI-compatible server", "custom", "openai", "",
          editable_base_url=True, capabilities=("chat", "tools"),
          docs="Any server that speaks the OpenAI chat API: vLLM, llama.cpp, LiteLLM, TabbyAPI..."),
        t("studioforge", "StudioForge", "llamacpp", "openai", "http://127.0.0.1:1234/v1",
          key="optional", local=True, editable_base_url=True, capabilities=full,
          docs="https://github.com/LaserLloyd/StudioForge",
          discover=("http://127.0.0.1:1234/api/version",)),
        t("lmstudio", "LM Studio", "lmstudio", "lmstudio", "http://127.0.0.1:1234/v1",
          key="none", local=True, editable_base_url=True, capabilities=full,
          discover=("http://127.0.0.1:1234/api/v0/models",)),
        t("ollama", "Ollama", "ollama", "ollama", "http://127.0.0.1:11434/v1",
          key="none", local=True, editable_base_url=True, capabilities=full,
          discover=("http://127.0.0.1:11434/api/tags",)),
        t("openai", "OpenAI", "openai", "openai", "https://api.openai.com/v1", key="required",
          key_env="OPENAI_API_KEY", docs="https://platform.openai.com/api-keys",
          capabilities=("chat", "vision", "images", "embeddings", "tools"),
          models=("gpt-4o-mini", "gpt-4o")),
        t("anthropic", "Anthropic (Claude)", "anthropic", "anthropic", "https://api.anthropic.com",
          key="required", key_env="ANTHROPIC_API_KEY", docs="https://console.anthropic.com/settings/keys",
          capabilities=("chat", "vision", "tools"),
          models=("claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5")),
        t("deepseek", "DeepSeek", "deepseek", "openai", "https://api.deepseek.com/v1", key="required",
          key_env="DEEPSEEK_API_KEY", docs="https://platform.deepseek.com/api_keys",
          models=("deepseek-chat", "deepseek-reasoner")),
        t("xai", "xAI (Grok)", "xai", "openai", "https://api.x.ai/v1", key="required",
          key_env="XAI_API_KEY", docs="https://console.x.ai/",
          capabilities=("chat", "vision", "images", "tools"), models=("grok-4.7",)),
        t("minimax", "MiniMax", "minimax", "openai", "https://api.minimax.io/v1", key="required",
          key_env="MINIMAX_API_KEY", docs="https://platform.minimax.io/",
          capabilities=("chat", "images", "tools"), models=("MiniMax-M3",)),
        t("openrouter", "OpenRouter", "openrouter", "openai", "https://openrouter.ai/api/v1",
          key="required", key_env="OPENROUTER_API_KEY", docs="https://openrouter.ai/keys",
          capabilities=full),
        t("groq", "Groq", "groq", "openai", "https://api.groq.com/openai/v1", key="required",
          key_env="GROQ_API_KEY", docs="https://console.groq.com/keys"),
        t("mistral", "Mistral", "mistral", "openai", "https://api.mistral.ai/v1", key="required",
          key_env="MISTRAL_API_KEY", docs="https://console.mistral.ai/api-keys", capabilities=full,
          models=("mistral-large-latest", "mistral-small-latest")),
        t("together", "Together AI", "together", "openai", "https://api.together.xyz/v1",
          key="required", key_env="TOGETHER_API_KEY",
          docs="https://api.together.xyz/settings/api-keys",
          capabilities=("chat", "vision", "images", "embeddings", "tools")),
    ]
    return {r.id: r for r in rows}


TYPES: dict[str, ConnType] = _types()
#: llm_api provider id -> connection type id (the inverse of ConnType.provider; "llamacpp" is a
#: local OpenAI-style server, which StudioForge is the best-known example of).
TYPE_BY_PROVIDER: dict[str, str] = {t.provider: t.id for t in TYPES.values()}


def types_public() -> list[dict]:
    return [t.public() for t in TYPES.values()]


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #


def db_path() -> Path:
    return Path(config.DATA_DIR) / "chats.db"


@contextlib.contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    path = db_path()
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
        _resolved_hosts.clear()


def _now() -> str:
    from datetime import UTC, datetime
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class Connection:
    id: str
    name: str
    type: str
    base_url: str
    credential_ref: str = ""
    capabilities: list[str] = field(default_factory=list)
    required_permission: str = "manage_connections"
    default_model: str = ""
    models: list[str] = field(default_factory=list)
    models_checked_at: str | None = None
    last_test: dict = field(default_factory=dict)
    created: str = ""
    updated: str = ""

    @property
    def ctype(self) -> ConnType:
        return TYPES[self.type]

    def credential_kind(self) -> str:
        if self.credential_ref.startswith("secret:"):
            return "secret"
        if self.credential_ref.startswith("env:"):
            return "env"
        return "none"

    def public(self, in_use_by: list[str] | None = None) -> dict:
        kind = self.credential_kind()
        return {
            "id": self.id, "name": self.name, "type": self.type, "type_label": self.ctype.label,
            "base_url": self.base_url,
            "credential": {"kind": kind, "env": self.credential_ref[4:] if kind == "env" else "",
                           "has_key": bool(credential(self))},
            "capabilities": list(self.capabilities), "required_permission": self.required_permission,
            "default_model": self.default_model, "models": list(self.models),
            "models_checked_at": self.models_checked_at, "last_test": dict(self.last_test),
            "created": self.created, "updated": self.updated,
            "in_use_by": list(in_use_by or []),
        }


def _row(r: sqlite3.Row) -> Connection:
    def j(text: str, default: Any) -> Any:
        try:
            v = json.loads(text or "")
        except ValueError:
            return default
        return v if isinstance(v, type(default)) else default
    return Connection(
        id=r["id"], name=r["name"], type=r["type"], base_url=r["base_url"],
        credential_ref=r["credential_ref"] or "", capabilities=[str(x) for x in j(r["capabilities"], [])],
        required_permission=r["required_permission"] or "manage_connections",
        default_model=r["default_model"] or "", models=[str(x) for x in j(r["models"], [])],
        models_checked_at=r["models_checked_at"], last_test=j(r["last_test"], {}),
        created=r["created"], updated=r["updated"])


def list_all() -> list[Connection]:
    with connect() as con:
        return [_row(r) for r in con.execute("SELECT * FROM connections ORDER BY created, id")]


def list_all_ro() -> list[Connection]:
    """Like :func:`list_all` but read-only: never creates the table or the file (the migration's
    dry run must write nothing)."""
    p = db_path()
    if not p.is_file():
        return []
    try:
        con = sqlite3.connect(p.resolve().as_uri() + "?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            return [_row(r) for r in con.execute("SELECT * FROM connections ORDER BY created, id")]
        finally:
            con.close()
    except sqlite3.Error:
        return []


def get(conn_id: str | None) -> Connection | None:
    if not conn_id:
        return None
    with connect() as con:
        r = con.execute("SELECT * FROM connections WHERE id=?", (conn_id,)).fetchone()
    return _row(r) if r else None


def exists_any() -> bool:
    try:
        with connect() as con:
            return con.execute("SELECT 1 FROM connections LIMIT 1").fetchone() is not None
    except sqlite3.Error:
        return False


def _save(c: Connection) -> None:
    with _lock, connect() as con:
        con.execute(
            "INSERT INTO connections (id,name,type,base_url,credential_ref,capabilities,"
            "required_permission,default_model,models,models_checked_at,last_test,created,updated) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,"
            "type=excluded.type,base_url=excluded.base_url,credential_ref=excluded.credential_ref,"
            "capabilities=excluded.capabilities,required_permission=excluded.required_permission,"
            "default_model=excluded.default_model,models=excluded.models,"
            "models_checked_at=excluded.models_checked_at,last_test=excluded.last_test,"
            "updated=excluded.updated",
            (c.id, c.name, c.type, c.base_url, c.credential_ref, json.dumps(c.capabilities),
             c.required_permission, c.default_model, json.dumps(c.models), c.models_checked_at,
             json.dumps(c.last_test), c.created, c.updated))


# --------------------------------------------------------------------------- #
# URL validation and the SSRF guard
# --------------------------------------------------------------------------- #

_METADATA_NAMES = frozenset({"metadata", "metadata.google.internal", "metadata.goog",
                             "instance-data", "instance-data.ec2.internal"})
_METADATA_IPS = frozenset({"169.254.169.254", "169.254.170.2", "100.100.100.200",
                           "fd00:ec2::254", "192.0.0.192"})
_resolved_hosts: dict[str, tuple[float, list[str]]] = {}
_DNS_TTL_S = 60.0


def allow_private_targets() -> bool:
    try:
        return bool(settings_store.get("connections.allow_private_targets", False))
    except Exception:
        return False


def _blocked_ip(ip: ipaddress._BaseAddress) -> str | None:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if str(ip) in _METADATA_IPS:
        return "a cloud metadata address"
    if ip.is_link_local:
        return "a link-local address"
    if ip.is_unspecified:
        return "the unspecified address (0.0.0.0); use 127.0.0.1 for this computer"
    if ip.is_multicast:
        return "a multicast address"
    return None


def _resolve_all(host: str, port: int) -> list[str]:
    now = time.monotonic()
    hit = _resolved_hosts.get(host)
    if hit and now - hit[0] < _DNS_TTL_S:
        return hit[1]
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        addrs = sorted({i[4][0].split("%", 1)[0] for i in infos})
    except (socket.gaierror, OSError):
        addrs = []
    _resolved_hosts[host] = (now, addrs)
    return addrs


def check_target(url: str, *, allow_private: bool | None = None) -> str:
    """The normalised URL, or ConnectionError_ ``connection_invalid_url`` /
    ``connection_blocked_target``. Resolves the host: EVERY address it resolves to is checked, so a
    public-looking name that points at the metadata service is refused too. A name that does not
    resolve at all is accepted (the Test will say so); the check repeats at use time."""
    u = (url or "").strip().rstrip("/")
    try:
        p = urlparse(u)
        port = p.port
    except ValueError:
        raise ConnectionError_("connection_invalid_url", "That is not a valid address.") from None
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ConnectionError_(
            "connection_invalid_url",
            "The address must start with http:// or https:// and name a host, "
            "e.g. http://127.0.0.1:1234/v1")
    if p.username or p.password:
        raise ConnectionError_("connection_invalid_url",
                               "Put the key in the key field, not in the address.")
    allow = allow_private_targets() if allow_private is None else allow_private
    if allow:
        return u
    host = p.hostname.lower().rstrip(".")
    if host in _METADATA_NAMES:
        raise ConnectionError_(
            "connection_blocked_target",
            f"{host} is a cloud metadata name and is blocked. If this is really your model server, an "
            "admin can allow it with the setting connections.allow_private_targets.", 403)
    addrs: list[str]
    try:
        addrs = [str(ipaddress.ip_address(host))]
    except ValueError:
        addrs = _resolve_all(host, port or (443 if p.scheme == "https" else 80))
    for a in addrs:
        try:
            why = _blocked_ip(ipaddress.ip_address(a))
        except ValueError:
            continue
        if why:
            raise ConnectionError_(
                "connection_blocked_target",
                f"{host} resolves to {a}, {why}, which is blocked so DisPatch cannot be used to reach "
                "internal services. If this really is your model server, an admin can allow it with "
                "the setting connections.allow_private_targets.", 403)
    return u


# --------------------------------------------------------------------------- #
# Credentials
# --------------------------------------------------------------------------- #


def credential(c: Connection) -> str:
    """The API key for ``c`` ("" when it has none). ``env:NAME`` is read from the process
    environment, and only for a name that looks like a credential and is not DisPatch's own."""
    ref = c.credential_ref
    if ref.startswith("secret:"):
        return (secret_store.get(ref[7:]) or "").strip()
    if ref.startswith("env:"):
        name = ref[4:]
        if env_name_ok(name):
            return os.environ.get(name, "").strip()
    return ""


def env_name_ok(name: str) -> bool:
    return bool(_ENV_RE.fullmatch(name or "") and _ENV_SUFFIX.search(name)
                and not name.startswith(("DISPATCH_", "LOCAL_CHAT_")))


def _secret_name(conn_id: str) -> str:
    return f"connection.{conn_id}"


# --------------------------------------------------------------------------- #
# Create / update / delete
# --------------------------------------------------------------------------- #


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9_-]+", "-", (text or "").lower()).strip("-_")[:40]
    return s or "connection"


def new_id(base: str) -> str:
    taken = {c.id for c in list_all()}
    base = _slug(base)
    if base not in taken:
        return base
    n = 2
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"


def _clean_caps(caps: Any, default: tuple[str, ...]) -> list[str]:
    if caps is None:
        return list(default)
    if not isinstance(caps, list) or not all(isinstance(x, str) for x in caps):
        raise ConnectionError_("connection_invalid", "capabilities must be a list of names")
    bad = [c for c in caps if c not in CAPABILITIES]
    if bad:
        raise ConnectionError_("connection_invalid",
                               f"Unknown capability {bad[0]!r}; choose from {', '.join(CAPABILITIES)}.")
    return list(dict.fromkeys(caps))


def create(*, type: str, name: str = "", base_url: str = "", api_key: str = "",
           api_key_env: str = "", capabilities: list[str] | None = None,
           default_model: str = "", conn_id: str = "", required_permission: str = "manage_connections",
           allow_private: bool | None = None, key_optional: bool = False) -> Connection:
    ct = TYPES.get(type)
    if ct is None:
        raise ConnectionError_("connection_invalid",
                               f"Unknown connection type {type!r}; choose from {', '.join(TYPES)}.")
    url = check_target(base_url or ct.base_url, allow_private=allow_private)
    if ct.id == "openai_compatible" and not (base_url or "").strip():
        raise ConnectionError_("connection_invalid", "An OpenAI-compatible connection needs its address.")
    cid = (conn_id or "").strip().lower()
    if cid:
        if not ID_RE.match(cid):
            raise ConnectionError_("connection_invalid",
                                   "The id must be 1-48 characters of a-z, 0-9, '-' and '_'.")
        if get(cid) is not None:
            raise ConnectionError_("connection_exists", f"A connection with the id {cid!r} already exists.", 409)
    else:
        cid = new_id(ct.id if not name else name)
    ref = ""
    key = (api_key or "").strip()
    env_name = (api_key_env or "").strip()
    if env_name and not env_name_ok(env_name):
        raise ConnectionError_(
            "connection_invalid",
            "The key variable must look like a credential (end in _KEY, _TOKEN, _SECRET or _PASSWORD) and "
            "not be one of DisPatch's own.")
    if ct.key == "required" and not key and not key_optional and not (
            env_name and os.environ.get(env_name, "").strip()):
        raise ConnectionError_("connection_invalid",
                               f"{ct.label} needs an API key. Paste one, or name an environment variable that holds it.")
    now = _now()
    c = Connection(
        id=cid, name=(name or ct.label).strip()[:80] or ct.label, type=ct.id, base_url=url,
        capabilities=_clean_caps(capabilities, ct.capabilities),
        required_permission=required_permission, default_model=(default_model or "").strip()[:200],
        created=now, updated=now)
    if key:
        secret_store.put(_secret_name(cid), key)
        ref = "secret:" + _secret_name(cid)
    elif env_name:
        ref = "env:" + env_name
    c.credential_ref = ref
    _save(c)
    return c


def update(conn_id: str, patch: dict, *, allow_private: bool | None = None) -> Connection:
    c = get(conn_id)
    if c is None:
        raise ConnectionError_("connection_not_found", "Unknown connection", 404)
    if "name" in patch and str(patch["name"]).strip():
        c.name = str(patch["name"]).strip()[:80]
    if "base_url" in patch:
        c.base_url = check_target(str(patch["base_url"] or c.ctype.base_url), allow_private=allow_private)
        c.models, c.models_checked_at, c.last_test = [], None, {}
    if "capabilities" in patch:
        c.capabilities = _clean_caps(patch["capabilities"], c.ctype.capabilities)
    if "default_model" in patch:
        c.default_model = str(patch["default_model"] or "").strip()[:200]
    if "required_permission" in patch:
        from .contracts.permissions import is_valid_permission
        rp = str(patch["required_permission"] or "")
        if not is_valid_permission(rp):
            raise ConnectionError_("connection_invalid", f"{rp!r} is not a known permission.")
        c.required_permission = rp
    # The key: a non-empty api_key replaces it; clear_key removes it; api_key_env points at a variable.
    key = str(patch.get("api_key") or "").strip()
    env_name = str(patch.get("api_key_env") or "").strip()
    if key:
        secret_store.put(_secret_name(c.id), key)
        c.credential_ref = "secret:" + _secret_name(c.id)
    elif env_name:
        if not env_name_ok(env_name):
            raise ConnectionError_(
                "connection_invalid",
                "The key variable must look like a credential (end in _KEY, _TOKEN, _SECRET or "
                "_PASSWORD) and not be one of DisPatch's own.")
        if c.credential_ref.startswith("secret:"):
            secret_store.delete(c.credential_ref[7:])
        c.credential_ref = "env:" + env_name
    elif patch.get("clear_key"):
        if c.credential_ref.startswith("secret:"):
            secret_store.delete(c.credential_ref[7:])
        c.credential_ref = ""
    c.updated = _now()
    _save(c)
    return c


def delete(conn_id: str) -> None:
    c = get(conn_id)
    if c is None:
        raise ConnectionError_("connection_not_found", "Unknown connection", 404)
    users = used_by(conn_id)
    if users:
        raise ConnectionError_(
            "connection_in_use",
            f"{c.name} is used by {', '.join(users)}. Point those characters at another connection first.", 409)
    if c.credential_ref.startswith("secret:"):
        secret_store.delete(c.credential_ref[7:])
    with _lock, connect() as con:
        con.execute("DELETE FROM connections WHERE id=?", (conn_id,))


def blocks_of(entry: dict) -> list[dict]:
    """The provider dicts of one raw ``config.yaml`` bot entry (``api`` plus every
    ``advisor.providers`` item). The same dict objects, so a caller can edit them in place."""
    out: list[dict] = []
    if isinstance(entry.get("api"), dict):
        out.append(entry["api"])
    adv = entry.get("advisor")
    if isinstance(adv, dict):
        out += [p for p in (adv.get("providers") or []) if isinstance(p, dict)]
    return out


def used_by(conn_id: str) -> list[str]:
    """Ids of the characters whose provider entries refer to ``conn_id``."""
    users = []
    for b in config.load_bots():
        entries = ([b.api] if b.api else []) + [
            p for p in (b.advisor or {}).get("providers") or [] if isinstance(p, dict)]
        if any(str(e.get("connection") or "") == conn_id for e in entries):
            users.append(b.id)
    return users


# --------------------------------------------------------------------------- #
# Resolution (llm_api calls this)
# --------------------------------------------------------------------------- #


def effective_block(block: dict) -> dict:
    """``block`` (a provider entry) with its ``connection`` expanded into the inline fields the
    rest of llm_api reads: ``provider``, ``base_url``, ``api_key``, ``model``. Returns ``block``
    itself when it has no ``connection``. Raises llm_api.ApiError for a connection that was
    deleted (and has no inline fallback) or whose address is no longer allowed."""
    ref = str(block.get("connection") or "").strip()
    if not ref:
        return block
    from .llm_api import ApiError
    c = get(ref)
    if c is None:
        if block.get("provider") and block.get("base_url"):
            log.warning("connection %r is gone; falling back to the inline settings", ref)
            return {k: v for k, v in block.items() if k != "connection"}
        raise ApiError("This character's connection was deleted",
                       f"It points at the connection {ref!r}, which no longer exists. Choose another "
                       "in Settings > Characters.")
    try:
        check_target(c.base_url)
    except ConnectionError_ as e:
        raise ApiError("This connection's address is not allowed", str(e)) from e
    out = {k: v for k, v in block.items() if k not in ("connection", "api_key", "api_key_env")}
    out["provider"] = c.ctype.provider
    out["base_url"] = c.base_url
    key = credential(c)
    if key:
        out["api_key"] = key
    if not str(out.get("model") or "").strip() and c.default_model:
        out["model"] = c.default_model
    return out


def credential_for_block(block: dict) -> str | None:
    """The key a provider entry resolves to through its connection, or None when it has no
    connection (the caller then reads the inline key)."""
    ref = str(block.get("connection") or "").strip()
    if not ref:
        return None
    c = get(ref)
    if c is None:
        return None
    return credential(c)


# --------------------------------------------------------------------------- #
# Test
# --------------------------------------------------------------------------- #


def _models_from(payload: Any) -> list[str]:
    data = payload.get("data") if isinstance(payload, dict) else payload
    if isinstance(data, dict):
        data = data.get("models") or data.get("data")
    ids: list[str] = []
    for item in data if isinstance(data, list) else []:
        mid = item.get("id") if isinstance(item, dict) else item
        if isinstance(mid, str) and mid.strip():
            ids.append(mid.strip())
    return sorted(set(ids))


def _origin(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


async def identify(base_url: str, *, timeout: float = 2.5, key: str = "") -> dict | None:
    """What is listening at ``base_url``'s origin? ``{type, product, version, models}`` or None.
    StudioForge is recognised by ``GET /api/version`` -> ``product == "StudioForge"``."""
    from . import llm_api
    origin = _origin(base_url)
    headers = llm_api._auth_headers(key)
    async with llm_api.http_client(timeout) as client:
        async def get_json(path: str) -> Any:
            try:
                status, raw = await llm_api._request(client, "GET", origin + path, headers=headers)
            except (httpx.HTTPError, llm_api.ApiError):
                return None
            if status != 200:
                return None
            try:
                return json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                return None
        v = await get_json("/api/version")
        if isinstance(v, dict) and str(v.get("product", "")).lower() == "studioforge":
            return {"type": "studioforge", "product": "StudioForge", "version": str(v.get("version") or "")}
        lm = await get_json("/api/v0/models")
        if isinstance(lm, dict) and isinstance(lm.get("data"), list):
            return {"type": "lmstudio", "product": "LM Studio", "version": "",
                    "models": _models_from(lm)}
        ol = await get_json("/api/tags")
        if isinstance(ol, dict) and isinstance(ol.get("models"), list):
            return {"type": "ollama", "product": "Ollama",
                    "version": str((await get_json("/api/version") or {}).get("version") or "")
                    if isinstance(v, dict) else "",
                    "models": sorted({str(m.get("name")) for m in ol["models"] if isinstance(m, dict) and m.get("name")})}
        om = await get_json("/v1/models")
        if isinstance(om, dict) and isinstance(om.get("data"), list):
            return {"type": "openai_compatible", "product": "OpenAI-compatible server", "version": "",
                    "models": _models_from(om)}
    return None


async def run_test(c: Connection | None = None, *, type: str = "", base_url: str = "", api_key: str = "",
                   model: str = "") -> dict:
    """Probe a connection (saved, or the unsaved form) and say what is wrong in plain words.

    ``{ok, reachable, auth_ok, models_listed, models, latency_ms, product, version, problem,
    message, fix}``. Never raises for a server-side failure; a malformed or blocked address is
    reported the same way (``problem``: invalid_url | blocked_target)."""
    from . import llm_api
    ct = c.ctype if c is not None else TYPES.get(type)
    out: dict[str, Any] = {"ok": False, "reachable": False, "auth_ok": None, "models_listed": False,
                           "models": [], "latency_ms": None, "product": None, "version": None,
                           "problem": None, "message": "", "fix": ""}
    if ct is None:
        out.update(problem="invalid_type", message="Unknown connection type.",
                   fix=f"Choose one of: {', '.join(TYPES)}.")
        return out
    url_in = base_url or (c.base_url if c is not None else ct.base_url)
    try:
        url = await asyncio.to_thread(check_target, url_in)
    except ConnectionError_ as e:
        out.update(problem="blocked_target" if e.code == "connection_blocked_target" else "invalid_url",
                   message=str(e), fix="Fix the address and test again.")
        return out
    key = api_key.strip() if api_key else (credential(c) if c is not None else "")
    mdl = model or (c.default_model if c is not None else "")
    t0 = time.perf_counter()
    try:
        if ct.protocol == "anthropic":
            models = await llm_api._probe_anthropic(key, url, mdl)
            out["product"] = "Anthropic"
        else:
            models = await llm_api._probe_openai(url, key, mdl, local=ct.local)
    except llm_api.ApiError as e:
        out["latency_ms"] = int((time.perf_counter() - t0) * 1000)
        out["message"], out["fix"] = e.message, e.detail
        low = (e.message + " " + e.detail).lower()
        if "rejected the api key" in low or ("api key" in low and "required" in low):
            out.update(reachable=True, auth_ok=False, problem="auth")
        elif "could not reach" in low or "took too long" in low or "connection to the provider failed" in low:
            out.update(problem="unreachable")
            out["fix"] = _unreachable_hint(ct, url, e.detail)
        elif "no model list" in low:
            out.update(reachable=True, problem="no_model_list")
        else:
            out.update(reachable=True, problem="provider_error")
        return out
    except Exception as e:
        log.warning("connection test failed unexpectedly: %r", e)
        out.update(problem="error", message=f"{type(e).__name__}: {str(e)[:200]}",
                   fix="Check the address and try again.")
        return out
    out["latency_ms"] = int((time.perf_counter() - t0) * 1000)
    out.update(ok=True, reachable=True, auth_ok=True, models=models, models_listed=bool(models))
    if not models:
        out["message"] = "Connected, but the server did not list its models."
        out["fix"] = "Type the model name when you assign this connection to a character."
    else:
        out["message"] = f"Connected. {len(models)} model{'s' if len(models) != 1 else ''} available."
    if ct.local or ct.id == "openai_compatible":
        with contextlib.suppress(Exception):
            ident = await identify(url, key=key)
            if ident:
                out["product"], out["version"] = ident["product"], ident.get("version") or None
                out["detected_type"] = ident["type"]
                if ct.id != ident["type"] and ct.id in ("studioforge", "lmstudio", "ollama"):
                    out["message"] += f" Note: this looks like {ident['product']}, not {ct.label}."
    return out


def _unreachable_hint(ct: ConnType, url: str, detail: str) -> str:
    host = urlparse(url).hostname or ""
    hints = [detail]
    if ct.local and host in ("127.0.0.1", "localhost", "::1") and Path("/.dockerenv").exists():
        hints.append("DisPatch is running in a container, where 127.0.0.1 is the container itself. "
                     "Use host.docker.internal (or the host's LAN address) instead.")
    elif ct.local:
        hints.append("Check the server is started, the port is right, and that it listens on an address "
                     "this computer can reach (a server bound to 127.0.0.1 on another machine cannot be "
                     "reached).")
    return " ".join(h for h in hints if h)


async def test_and_record(c: Connection) -> dict:
    res = await run_test(c)
    c = get(c.id) or c
    c.last_test = {k: res[k] for k in ("ok", "reachable", "auth_ok", "models_listed", "latency_ms",
                                       "problem", "message") if k in res} | {"at": _now()}
    if res.get("ok") and res.get("models"):
        c.models, c.models_checked_at = res["models"], _now()
        if not c.default_model:
            c.default_model = res["models"][0]
    _save(c)
    return res


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #

_DISCOVER_PORTS = (1234, 8080, 11434)


def _lan_hosts() -> list[str]:
    """The /24 neighbours of this computer's LAN address (private ranges only)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("10.255.255.255", 1))          # no packet is sent for a UDP connect
            me = s.getsockname()[0]
        finally:
            s.close()
        ip = ipaddress.ip_address(me)
    except OSError:
        return []
    if not ip.is_private or ip.is_loopback:
        return []
    net = ipaddress.ip_network(f"{ip}/24", strict=False)
    return [str(h) for h in net.hosts() if str(h) != str(ip)]


async def discover(*, hosts: list[str] | None = None, lan: bool = False,
                   allow_private: bool | None = None) -> list[dict]:
    """Find StudioForge, LM Studio, Ollama and OpenAI-style servers on loopback, the container
    host, any ``hosts`` named, and (``lan=True``) this computer's /24. Short timeouts, bounded
    concurrency; every candidate passes the SSRF guard. Returns one row per server found."""
    cand: list[str] = ["127.0.0.1"]
    if Path("/.dockerenv").exists() or os.environ.get("DISPATCH_IN_DOCKER"):
        cand.append("host.docker.internal")
    for h in hosts or []:
        h = str(h).strip()
        if h and re.fullmatch(r"[A-Za-z0-9._:-]{1,253}", h):
            cand.append(h)
    if lan:
        cand += await asyncio.to_thread(_lan_hosts)
    cand = list(dict.fromkeys(cand))
    existing = {(c.base_url.rstrip("/")): c.id for c in list_all()}
    sem = asyncio.Semaphore(48)
    found: dict[str, dict] = {}

    async def probe(host: str, port: int) -> None:
        h = f"[{host}]" if ":" in host and not host.startswith("[") else host
        base = f"http://{h}:{port}"
        try:
            await asyncio.to_thread(check_target, base, allow_private=allow_private)
        except ConnectionError_:
            return
        async with sem:
            ident = await identify(base, timeout=1.2 if host not in ("127.0.0.1", "host.docker.internal") else 2.0)
        if not ident:
            return
        api = base + "/v1"
        ct = TYPES[ident["type"]]
        found[api] = {"type": ident["type"], "label": ct.label, "product": ident["product"],
                      "version": ident.get("version") or "", "base_url": api, "host": host, "port": port,
                      "models": (ident.get("models") or [])[:50],
                      "already_added": existing.get(api)}

    await asyncio.gather(*(probe(h, p) for h in cand for p in _DISCOVER_PORTS))
    order = {"studioforge": 0, "lmstudio": 1, "ollama": 2, "openai_compatible": 3}
    return sorted(found.values(), key=lambda r: (order.get(r["type"], 9), r["host"] != "127.0.0.1", r["base_url"]))


# --------------------------------------------------------------------------- #
# Adoption of the 2.x inline blocks (migration step 006)
# --------------------------------------------------------------------------- #


@dataclass
class AdoptPlan:
    actions: list[str] = field(default_factory=list)
    created: list[str] = field(default_factory=list)
    linked: int = 0


def _signature(provider_id: str, block: dict) -> tuple[str, str, str]:
    from . import llm_api
    prov = llm_api.PROVIDER_BY_ID.get(provider_id)
    base = (str(block.get("base_url") or "") or (prov.base_url if prov else "")).rstrip("/")
    key = str(block.get("api_key") or "").strip()
    env_name = str(block.get("api_key_env") or "").strip()
    if key:
        import hashlib
        return provider_id, base, "k:" + hashlib.sha256(key.encode()).hexdigest()[:16]
    return provider_id, base, ("e:" + env_name) if env_name else ""


def adopt_inline(roster: list[dict], *, dry_run: bool = False) -> AdoptPlan:
    """Give every inline provider block in ``roster`` (raw config.yaml entries) a connection.

    Idempotent: a block that already has ``connection`` is left alone; a second block with the
    same provider, address and key reuses the first one's connection. The API key moves into the
    secret store (read back before it is removed from the file); ``provider`` / ``base_url`` /
    ``model`` and everything else stay inline and readable. ``roster`` is edited in place and the
    caller writes it. With ``dry_run`` nothing is created and nothing is edited."""
    from . import llm_api
    plan = AdoptPlan()
    sigs: dict[tuple[str, str, str], str] = {}
    for c in (list_all_ro() if dry_run else list_all()):
        # Existing connections are matched on type + address + key reference only when the
        # reference is an env name or no key (a stored secret cannot be fingerprint-matched).
        env_name = c.credential_ref[4:] if c.credential_ref.startswith("env:") else ""
        sigs[(c.ctype.provider, c.base_url.rstrip("/"), ("e:" + env_name) if env_name else "")] = c.id
    planned: dict[tuple[str, str, str], str] = {}
    for entry in roster:
        for block in blocks_of(entry):
            if str(block.get("connection") or "").strip():
                continue
            pid = str(block.get("provider") or "").strip()
            prov = llm_api.PROVIDER_BY_ID.get(pid)
            if prov is None or pid not in TYPE_BY_PROVIDER:
                plan.actions.append(f"character {entry.get('id')!r}: provider {pid!r} is unknown - left as it is")
                continue
            sig = _signature(pid, block)
            cid = sigs.get(sig) or planned.get(sig)
            if cid is None:
                ct = TYPES[TYPE_BY_PROVIDER[pid]]
                label = ct.label + (f" ({sig[1].split('//')[-1]})" if ct.editable_base_url else "")
                if dry_run:
                    cid = "<new>"
                    planned[sig] = cid
                    plan.actions.append(
                        f"character {entry.get('id')!r}: create connection {label!r} -> {sig[1]}"
                        f"{' (key moves to the secret store)' if sig[2].startswith('k:') else ''}")
                else:
                    try:
                        key = str(block.get("api_key") or "").strip()
                        env_name = str(block.get("api_key_env") or "").strip()
                        conn = create(
                            type=ct.id, name=label, base_url=sig[1] or ct.base_url, api_key=key,
                            api_key_env=env_name if env_name_ok(env_name) else "",
                            default_model=str(block.get("model") or ""), allow_private=True,
                            conn_id=new_id(ct.id), key_optional=True)
                    except ConnectionError_ as e:
                        plan.actions.append(f"character {entry.get('id')!r}: not adopted ({e})")
                        continue
                    sigs[sig] = cid = conn.id
                    plan.created.append(cid)
            else:
                plan.actions.append(f"character {entry.get('id')!r}: reuse connection {cid}")
            if dry_run:
                plan.linked += 1
                continue
            block["connection"] = cid
            conn = get(cid)
            if conn is not None and conn.credential_ref.startswith("secret:") and block.get("api_key"):
                # verified by secret_store.put's read-back when the connection was created
                if credential(conn) == str(block["api_key"]).strip():
                    del block["api_key"]
            plan.linked += 1
    return plan
