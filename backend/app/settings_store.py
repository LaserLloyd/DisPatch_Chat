"""The settings store: one place that holds every value in ``settings_schema``.

``chats.db`` gets three small tables (created on first use, additive, so a 2.x binary run
against a migrated database simply ignores them):

``settings``          key, JSON value, origin (``user`` / ``imported`` / ``plugin:<ns>``), updated, actor
``settings_unknown``  keys found in the old files that the schema does not know - kept, reported,
                      never dropped (secret-looking values are recorded as ``***``, not copied)
``settings_meta``     the schema version and when the import ran

Resolution, highest first: **process environment** (``source: env``, read-only here), the
**stored value** (``store``), the **default**. Three kinds of binding:

``store``      the value lives in the table; the old readers see it through ``config.env``
               (see ``config.py``), so a change reaches them without an environment variable
``file:<x>``   the setting's home is still a file other code reads (``security.yaml``,
               ``tools.yaml``). Writes go to that file first, the table mirrors them, reads ask the
               file, so a hand edit is never overridden by a stale row
``secret``     write-only; goes to ``secret_store`` and is never returned

``writer(namespace)`` is what an add-on uses to move its own settings in::

    store_writer = settings_store.writer("voice")
    store_writer("voice.speed", 1.1)       # or store_writer("speed", 1.1) - same key

The import (``import_legacy``) is idempotent and has a dry run that writes nothing.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import sqlite3
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import config, secret_store, settings_schema
from .settings_schema import SettingDef, SettingError

log = logging.getLogger("local-chat.settings")

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL,                       -- JSON
    origin  TEXT NOT NULL DEFAULT 'user',        -- user | imported | plugin:<namespace>
    updated TEXT NOT NULL,
    actor   TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS settings_unknown (
    origin     TEXT NOT NULL,                    -- the file it was found in
    key        TEXT NOT NULL,
    value      TEXT NOT NULL DEFAULT '',         -- JSON, or '"***"' for a secret-looking key
    first_seen TEXT NOT NULL,
    PRIMARY KEY (origin, key)
);
CREATE TABLE IF NOT EXISTS settings_meta (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
"""

TABLES = ("settings", "settings_unknown", "settings_meta")

_lock = threading.RLock()
_ready: set[str] = set()
#: key -> the effective value when this process first looked (restart_pending baseline)
_boot: dict[str, Any] = {}
MISSING = object()


def db_path() -> Path:
    return Path(config.DATA_DIR) / "chats.db"


@contextlib.contextmanager
def connect(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    path = path or db_path()
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
        _boot.clear()
    config.invalidate_stored_env()
    settings_schema.reset_plugins_for_tests()


def _now() -> str:
    from datetime import UTC, datetime
    return datetime.now(UTC).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# File-bound adapters
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Adapter:
    """How a file-bound setting is read and written. ``read`` returns MISSING when the file does
    not say (the default then applies); ``write`` persists a coerced value."""
    read: Callable[[SettingDef], Any]
    write: Callable[[SettingDef, Any], None]


def _sec_name(d: SettingDef) -> str:
    return d.key.split(".", 1)[1]


def _security_read(d: SettingDef) -> Any:
    from . import auth
    cfg = auth.load()
    n = _sec_name(d)
    return {
        "lock_timeout_seconds": cfg.lock_timeout,
        "remember_device_days": cfg.remember_days,
        "machine_loopback_trust": cfg.machine_loopback_trust,
        "anonymous_access": cfg.anonymous_access,
        # 4.0: the dict is keyed by Role value; the setting keys keep their 3.x names until
        # WP-CORE renames them (session_timeout_user / _basic).
        "session_timeout_member": cfg.session_timeouts.get("user"),
        "session_timeout_restricted": cfg.session_timeouts.get("basic"),
    }.get(n, MISSING)


def _security_write(d: SettingDef, value: Any) -> None:
    from . import auth
    n = _sec_name(d)
    if n == "remember_device_days":
        auth.set_remember_days(int(value))          # also forgets devices when it goes to 0
        return
    with auth._lock:
        cfg = auth.load()
        if n == "lock_timeout_seconds":
            cfg.lock_timeout = int(value)
        elif n == "machine_loopback_trust":
            cfg.machine_loopback_trust = bool(value)
        elif n == "anonymous_access":
            cfg.anonymous_access = str(value)
        elif n in ("session_timeout_member", "session_timeout_restricted"):
            role = "user" if n == "session_timeout_member" else "basic"
            cfg.session_timeouts = {**cfg.session_timeouts, role: int(value)}
        else:                                       # pragma: no cover - schema/adapter drift
            raise SettingError(d.key, f"{d.label}: no writer for this setting")
        auth._write(cfg)
        auth._bust_cache()


def _tools_read(d: SettingDef) -> Any:
    from . import tools
    return tools.livetools_enabled() if _sec_name(d) == "livetools_enabled" else MISSING


def _tools_write(d: SettingDef, value: Any) -> None:
    from . import tools
    try:
        tools.set_livetools_enabled(bool(value))
    except ValueError as e:
        raise SettingError(d.key, f"tools.yaml cannot be read, fix it first ({e})") from e


ADAPTERS: dict[str, Adapter] = {
    "file:security": Adapter(_security_read, _security_write),
    "file:tools": Adapter(_tools_read, _tools_write),
}


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #


@dataclass
class Resolved:
    key: str
    value: Any
    source: str                                   # env | store | default
    default: Any
    env_name: str | None = None
    restart_pending: bool = False
    secret_set: bool | None = None
    invalid_env: str | None = None

    def public(self, d: SettingDef | None = None) -> dict:
        out = {"value": None if self.secret_set is not None else self.value, "source": self.source,
               "default": self.default, "is_default": self.source == "default",
               "restart_pending": self.restart_pending}
        if self.env_name:
            out["env"] = self.env_name
        if self.secret_set is not None:
            out["secret"] = True
            out["set"] = self.secret_set
        if self.invalid_env:
            out["warning"] = self.invalid_env
        return out


def _env_hit(d: SettingDef) -> tuple[str, str] | None:
    for name in d.env_names():
        if name in os.environ:
            return name, os.environ[name]
    return None


def _row(key: str) -> sqlite3.Row | None:
    con = _ro_connect()
    if con is None:
        return None
    try:
        return con.execute("SELECT * FROM settings WHERE key=?", (key,)).fetchone()
    except sqlite3.Error:
        return None                       # no table yet: nothing stored
    finally:
        con.close()


def _secret_ref(d: SettingDef) -> str:
    return f"setting.{d.key}"


def resolve(key: str) -> Resolved:
    d = settings_schema.get_def(key)
    if d is None:
        raise KeyError(key)
    hit = _env_hit(d)
    if d.secret:
        set_ = bool(hit and hit[1]) or secret_store.exists(_secret_ref(d))
        return Resolved(key, None, "env" if hit else ("store" if set_ else "default"), None,
                        env_name=hit[0] if hit else None, secret_set=set_)
    value: Any = MISSING
    source = "default"
    invalid = None
    env_name = None
    if hit is not None:
        env_name = hit[0]
        try:
            value = settings_schema.coerce(d, hit[1] if hit[1] != "" else None, strict=False)
            source = "env"
        except SettingError as e:
            invalid = f"{hit[0]} is set to a value DisPatch cannot use ({e}); using the stored value or default"
    if value is MISSING and d.binding in ADAPTERS:
        try:
            v = ADAPTERS[d.binding].read(d)
        except Exception:
            log.exception("reading %s from its file failed", key)
            v = MISSING
        if v is not MISSING and v is not None:
            value, source = v, "store"
    if value is MISSING:
        row = _row(key)
        if row is not None:
            try:
                value = settings_schema.coerce(d, json.loads(row["value"]), strict=False)
                source = "store"
            except (ValueError, SettingError):
                value = MISSING
    if value is MISSING:
        value, source = d.default, "default"
    with _lock:
        _boot.setdefault(key, value)
        pending = d.restart_required and _boot[key] != value
    return Resolved(key, value, source, d.default, env_name=env_name,
                    restart_pending=pending, invalid_env=invalid)


def get(key: str, default: Any = MISSING) -> Any:
    """The effective value. For a key the schema does not know (a namespace writer's), the stored
    JSON value, or ``default``."""
    if settings_schema.get_def(key) is not None:
        return resolve(key).value
    row = _row(key)
    if row is not None:
        with contextlib.suppress(ValueError):
            return json.loads(row["value"])
    if default is MISSING:
        raise KeyError(key)
    return default


def all_resolved(defs: list[SettingDef] | None = None) -> dict[str, Resolved]:
    return {d.key: resolve(d.key) for d in (defs or settings_schema.all_defs())}


def extra_values() -> dict[str, Any]:
    """Stored values whose key the schema does not define (a namespace writer's)."""
    out: dict[str, Any] = {}
    con = _ro_connect()
    if con is None:
        return out
    try:
        for r in con.execute("SELECT key, value FROM settings ORDER BY key"):
            if settings_schema.get_def(r["key"]) is None:
                with contextlib.suppress(ValueError):
                    out[r["key"]] = json.loads(r["value"])
    except sqlite3.Error:
        pass
    finally:
        con.close()
    return out


def unknown() -> list[dict]:
    con = _ro_connect()
    if con is None:
        return []
    try:
        return [{"origin": r["origin"], "key": r["key"], "value": _loads(r["value"]),
                 "first_seen": r["first_seen"]}
                for r in con.execute("SELECT * FROM settings_unknown ORDER BY origin, key")]
    except sqlite3.Error:
        return []
    finally:
        con.close()


def _loads(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return text


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #


@dataclass
class ChangeResult:
    changed: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    restart_required: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _put_row(key: str, value: Any, origin: str, actor: str = "") -> None:
    with connect() as con:
        con.execute(
            "INSERT INTO settings (key, value, origin, updated, actor) VALUES (?,?,?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, origin=excluded.origin, "
            "updated=excluded.updated, actor=excluded.actor",
            (key, json.dumps(value, ensure_ascii=False), origin, _now(), actor))


def _del_row(key: str) -> None:
    with connect() as con:
        con.execute("DELETE FROM settings WHERE key=?", (key,))


def set_many(changes: dict[str, Any], *, actor: str = "", reset: list[str] | None = None,
             owner: bool = True) -> ChangeResult:
    """Validate everything, then apply. ALL-or-nothing on validation: one bad value rejects the
    whole request so a form is never half-saved. ``reset`` returns keys to their default.
    ``owner`` is whether the caller may change ``owner_only`` settings (server code: yes; the API passes
    whether the caller holds ``admin_system``)."""
    res = ChangeResult()
    reset = list(reset or [])
    # A choice over the connections may hold an id whose connection was deleted (it was
    # free text in schema 1); writing that same value back stays allowed.
    current = {}
    for k in changes:
        d = settings_schema.get_def(k)
        if d is not None and d.choices_from == "connections":
            with contextlib.suppress(Exception):
                current[k] = resolve(k).value
    coerced, errs = settings_schema.validate_all(changes, current=current)
    res.errors.update(errs)
    for k in reset:
        if settings_schema.get_def(k) is None:
            res.errors[k] = "unknown setting"
    for k in {*coerced, *reset}:
        d = settings_schema.get_def(k)
        if d is None:
            continue
        if d.owner_only and not owner:
            res.errors[k] = f"{d.label}: only the Owner or an Admin can change this"
            continue
        hit = _env_hit(d)
        if hit is not None:
            res.errors[k] = (f"{d.label} is set by the environment variable {hit[0]}; "
                             "change it there (or unset it) and restart")
    if res.errors:
        return res
    with _lock:
        for k in reset:
            d = settings_schema.get_def(k)
            assert d is not None
            _apply(d, d.default, actor, reset=True)
            res.changed.append(k)
        for k, v in coerced.items():
            d = settings_schema.get_def(k)
            assert d is not None
            resolve(k)                                  # take the restart baseline BEFORE the change
            _apply(d, v, actor)
            res.changed.append(k)
    config.invalidate_stored_env()
    for k in res.changed:
        d = settings_schema.get_def(k)
        if d is not None and d.restart_required and resolve(k).restart_pending:
            res.restart_required.append(k)
    return res


def _apply(d: SettingDef, value: Any, actor: str, *, reset: bool = False) -> None:
    if d.secret:
        ref = _secret_ref(d)
        if reset or value in (None, ""):
            secret_store.delete(ref)
        else:
            secret_store.put(ref, str(value))
        return
    if d.binding in ADAPTERS:
        ADAPTERS[d.binding].write(d, value)
    if reset:
        _del_row(d.key)
    else:
        _put_row(d.key, value, "user", actor)


def writer(namespace: str) -> Callable[[str, Any], None]:
    """``(key, value) -> None`` that stores values under ``namespace.`` for an add-on.

    W6b's ``migrate_voice_yaml(store_writer)`` calls this with fully qualified keys
    (``voice.speed``). A key without the prefix gets it (``speed`` -> ``voice.speed``). A key the
    schema defines is validated like a PATCH (and refused while an environment variable sets
    it); a key it does not define is stored as given - the add-on owns that namespace. Raises
    ``SettingError`` for a refused value, ``ValueError`` for a key outside the namespace."""
    ns = namespace.strip().strip(".")
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}(\.[a-z][a-z0-9_-]{0,31})*", ns):
        raise ValueError(f"bad settings namespace {namespace!r}")

    def write(key: str, value: Any) -> None:
        full = key if key == ns or key.startswith(ns + ".") else f"{ns}.{key}"
        if not settings_schema.KEY_RE.match(full) and not re.fullmatch(r"[a-z][a-z0-9_.-]{1,95}", full):
            raise ValueError(f"bad settings key {key!r}")
        d = settings_schema.get_def(full)
        if d is not None:
            r = set_many({full: value}, actor=f"plugin:{ns}")
            if r.errors:
                raise SettingError(full, next(iter(r.errors.values())))
            return
        with _lock:
            _put_row(full, value, f"plugin:{ns}", f"plugin:{ns}")
        config.invalidate_stored_env()

    write.namespace = ns                                  # type: ignore[attr-defined]
    return write


# --------------------------------------------------------------------------- #
# Import of today's values (idempotent, dry-run capable)
# --------------------------------------------------------------------------- #

_SECRETISH = re.compile(r"(key|token|secret|password|passwd|pin|hash|salt|credential|cookie)", re.I)

#: security.yaml keys that are credentials / managed state, not settings.
_SECURITY_INTERNAL = frozenset({"pin", "pin_hash", "salt", "iterations", "recovery_hash",
                                "recovery_salt", "api_token"})
_SECURITY_MAPPED = frozenset({"lock_timeout_seconds", "remember_device_days", "machine_loopback_trust",
                              "anonymous_access", "session_timeout_member",
                              "session_timeout_restricted"})


@dataclass
class ImportReport:
    imported: list[dict] = field(default_factory=list)       # {key, value, from}
    unknown: list[dict] = field(default_factory=list)        # {origin, key, value}
    already: bool = False
    dry_run: bool = False

    def lines(self) -> list[str]:
        out = [f"import setting {i['key']} = {i['value']!r} (from {i['from']})" for i in self.imported]
        out += [f"carry over unrecognised key {u['origin']}:{u['key']} into settings_unknown"
                for u in self.unknown]
        return out


def _read_yaml(path: Path) -> dict | None:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError, UnicodeDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def _scan(data_dir: Path) -> tuple[list[dict], list[dict]]:
    """(values found for schema keys, unrecognised keys) from the old files. Reads only."""
    imported: list[dict] = []
    unknown_: list[dict] = []

    def unk(origin: str, key: str, value: Any) -> None:
        shown = "***" if _SECRETISH.search(key) else value
        unknown_.append({"origin": origin, "key": key, "value": shown})

    sec = _read_yaml(data_dir / "security.yaml")
    if sec is not None:
        for k, v in sec.items():
            if k in _SECURITY_INTERNAL:
                continue
            key = f"people.{k}"
            d = settings_schema.get_def(key)
            if k in _SECURITY_MAPPED and d is not None and v is not None:
                try:
                    imported.append({"key": key, "value": settings_schema.coerce(d, v),
                                     "from": "security.yaml"})
                except SettingError:
                    unk("security.yaml", k, v)
            elif k in _SECURITY_MAPPED:
                continue
            elif k == "session_timeouts":
                continue
            else:
                unk("security.yaml", str(k), v)
    tl = _read_yaml(data_dir / "tools.yaml")
    if tl is not None:
        for k, v in tl.items():
            if k == "tools":
                continue
            if k == "livetools" and isinstance(v, dict):
                for sub, val in v.items():
                    if sub == "enabled" and isinstance(val, bool):
                        imported.append({"key": "tools.livetools_enabled", "value": val,
                                         "from": "tools.yaml"})
                    else:
                        unk("tools.yaml", f"livetools.{sub}", val)
                continue
            unk("tools.yaml", str(k), v)
    cfg = _read_yaml(data_dir / "config.yaml")
    if cfg is not None:
        for k, v in cfg.items():
            if k != "bots":
                unk("config.yaml", str(k), v)
    return imported, unknown_


def import_legacy(*, dry_run: bool = False) -> ImportReport:
    """Fill the store from the old files. Only keys with no stored value are imported, so
    running it twice (or after the owner changed something) changes nothing."""
    data_dir = Path(config.DATA_DIR)
    imported, unknown_ = _scan(data_dir)
    rep = ImportReport(dry_run=dry_run)
    try:
        have = {r["key"] for r in _rows_ro()}
        have_unk = {(r["origin"], r["key"]) for r in _unknown_ro()}
        meta = _meta_ro()
    except sqlite3.Error:
        have, have_unk, meta = set(), set(), {}
    rep.imported = [i for i in imported if i["key"] not in have]
    rep.unknown = [u for u in unknown_ if (u["origin"], u["key"]) not in have_unk]
    rep.already = "imported_at" in meta and not rep.imported and not rep.unknown
    if dry_run:
        return rep
    with _lock, connect() as con:
        for i in rep.imported:
            con.execute("INSERT OR IGNORE INTO settings (key, value, origin, updated) VALUES (?,?,?,?)",
                        (i["key"], json.dumps(i["value"]), "imported", _now()))
        for u in rep.unknown:
            con.execute("INSERT OR IGNORE INTO settings_unknown (origin, key, value, first_seen) "
                        "VALUES (?,?,?,?)", (u["origin"], u["key"],
                                             json.dumps(u["value"], default=str), _now()))
        con.execute("INSERT OR REPLACE INTO settings_meta (k, v) VALUES ('schema', ?)",
                    (str(settings_schema.SCHEMA_VERSION),))
        con.execute("INSERT OR IGNORE INTO settings_meta (k, v) VALUES ('imported_at', ?)", (_now(),))
    config.invalidate_stored_env()
    return rep


def _ro_connect() -> sqlite3.Connection | None:
    p = db_path()
    if not p.is_file():
        return None
    con = sqlite3.connect(p.resolve().as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _rows_ro() -> list[sqlite3.Row]:
    con = _ro_connect()
    if con is None:
        return []
    try:
        return con.execute("SELECT key FROM settings").fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()


def _unknown_ro() -> list[sqlite3.Row]:
    con = _ro_connect()
    if con is None:
        return []
    try:
        return con.execute("SELECT origin, key FROM settings_unknown").fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()


def _meta_ro() -> dict[str, str]:
    con = _ro_connect()
    if con is None:
        return {}
    try:
        return {r["k"]: r["v"] for r in con.execute("SELECT k, v FROM settings_meta")}
    except sqlite3.Error:
        return {}
    finally:
        con.close()


# --------------------------------------------------------------------------- #
# Documents for the API
# --------------------------------------------------------------------------- #


def values_document(*, group: str | None = None, level: str | None = None,
                    q: str | None = None) -> dict:
    defs = settings_schema.all_defs()
    needle = (q or "").strip().lower()

    def keep(d: SettingDef) -> bool:
        if group and d.group != group:
            return False
        if level and d.level != level:
            return False
        if needle and needle not in " ".join([d.key, d.label, d.description, d.section]).lower():
            return False
        return True

    shown = [d for d in defs if keep(d)]
    values = {d.key: resolve(d.key).public(d) for d in shown}
    return {
        "schema": settings_schema.SCHEMA_VERSION,
        "values": values,
        "restart_required": sorted(k for k, v in values.items() if v["restart_pending"]),
        "unknown": unknown(),
        "extra": extra_values() if not (group or needle) else {},
    }
