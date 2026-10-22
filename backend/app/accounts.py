"""Profiles, devices, session registry, daily usage and the activity/audit log.

This is the persistence layer under :mod:`app.auth` (credentials and sessions) and
:mod:`app.policy` (who may do what). Everything lives in ``chats.db`` beside the
conversations, so the rotating verified backups and the pre-migration backup cover
accounts too.

Why a synchronous ``sqlite3`` module and not the app's ``aiosqlite`` handle: the
auth gate, the WebSocket handshake and a dozen sync helpers ask "who is this?" on
every request. The tables are tiny, the answers are cached in memory (keyed by the
database path, so a test that points the data dir elsewhere gets a fresh cache), and
writes are rare (a login, a role change) or one small UPSERT (a budget charge). WAL
mode and ``busy_timeout`` make the second connection safe next to the app's own.

The schema is additive only: ``CREATE TABLE IF NOT EXISTS``, nothing dropped. A 2.x
binary run against a migrated database simply ignores these tables.
"""

from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import config

log = logging.getLogger("local-chat.accounts")

ROLES = ("owner", "admin", "member", "restricted")

SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles (
    id             TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    role           TEXT NOT NULL CHECK (role IN ('owner','admin','member','restricted')),
    pin_hash       TEXT,                       -- quick-switch PIN (argon2id, or the legacy pbkdf2 string)
    password_hash  TEXT,                       -- owner/admin credential (argon2id)
    avatar         TEXT NOT NULL DEFAULT '',
    budgets        TEXT NOT NULL DEFAULT '{}', -- JSON: per-day limits and quiet hours
    grants         TEXT NOT NULL DEFAULT '[]', -- JSON: extra permissions (incl. use_tools:<id>)
    denies         TEXT NOT NULL DEFAULT '[]', -- JSON: permissions removed from the role's set
    session_timeout INTEGER,                   -- seconds of inactivity; NULL = role default
    created        TEXT NOT NULL,
    disabled       INTEGER NOT NULL DEFAULT 0,
    epoch          INTEGER NOT NULL DEFAULT 0, -- bumped to invalidate every session of the profile
    backends       TEXT NOT NULL DEFAULT '{}'  -- 4.0: JSON {allowed, default, models}; {} = inherit
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_profiles_name ON profiles(lower(name));
CREATE UNIQUE INDEX IF NOT EXISTS ux_profiles_one_owner ON profiles(role) WHERE role = 'owner';

CREATE TABLE IF NOT EXISTS devices (
    id                 TEXT PRIMARY KEY,       -- sha256(device cookie token)
    label              TEXT NOT NULL DEFAULT '',
    default_profile_id TEXT REFERENCES profiles(id) ON DELETE SET NULL,
    bound_at           TEXT,
    created            TEXT NOT NULL,
    last_seen          TEXT,
    user_agent         TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash  TEXT PRIMARY KEY,              -- sha256(session cookie token)
    profile_id  TEXT NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    device_id   TEXT REFERENCES devices(id) ON DELETE SET NULL,
    created     TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    expires     TEXT,                          -- NULL = idle expiry only
    persistent  INTEGER NOT NULL DEFAULT 0,
    user_agent  TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_sessions_profile ON sessions(profile_id);

CREATE TABLE IF NOT EXISTS usage_daily (
    subject TEXT NOT NULL,                     -- profile id, or device:<id> / ip:<addr> when anonymous
    day     TEXT NOT NULL,                     -- local YYYY-MM-DD
    kind    TEXT NOT NULL,                     -- turn | thread | feedback | upload_bytes | active_seconds
    amount  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (subject, day, kind)
);

CREATE TABLE IF NOT EXISTS activity (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    category   TEXT NOT NULL DEFAULT 'activity',   -- 'activity' | 'audit'
    kind       TEXT NOT NULL,
    profile_id TEXT,                               -- who did it (NULL = anonymous / machine)
    subject    TEXT NOT NULL DEFAULT '',
    detail     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_activity_profile_ts ON activity(profile_id, ts);

-- One row, bumped by every write to ``profiles``. A second process (the ``python -m app users``
-- command line) edits the same file; the running server notices the bump and reloads its profile
-- cache instead of serving a stale password hash until the next restart.
CREATE TABLE IF NOT EXISTS account_rev (
    id  INTEGER PRIMARY KEY CHECK (id = 1),
    rev INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS schema_migrations (
    id         TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL,
    app_version TEXT NOT NULL DEFAULT ''
);
"""

#: ALTERs for columns added to a table after it first shipped (idempotent: a duplicate
#: column is ignored).
_ADDED_COLUMNS = (
    "ALTER TABLE profiles ADD COLUMN backends TEXT NOT NULL DEFAULT '{}'",
)

#: Tables this module owns, in drop-safe (children first) order.
TABLES = ("sessions", "devices", "usage_daily", "activity", "profiles", "account_rev", "schema_migrations")

_lock = threading.RLock()
_ready: set[str] = set()


class AccountError(ValueError):
    """A rejected account operation. ``code`` is a stable slug (errors catalog)."""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def new_id() -> str:
    return uuid.uuid4().hex


# --------------------------------------------------------------------------- #
# Connection and cache
# --------------------------------------------------------------------------- #


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
        con.execute("PRAGMA foreign_keys=ON")
        key = str(path)
        if key not in _ready:
            con.execute("PRAGMA journal_mode=WAL")
            con.executescript(SCHEMA)
            # Columns added after a table first shipped (CREATE TABLE IF NOT EXISTS leaves an
            # existing table as it is). Migration 009_ownership reports this one; adding it
            # here too means no reader ever meets a profiles table without it.
            for ddl in _ADDED_COLUMNS:
                with contextlib.suppress(sqlite3.OperationalError):    # already there
                    con.execute(ddl)
            _ready.add(key)
        yield con
    finally:
        con.close()


@dataclass
class Profile:
    id: str
    name: str
    role: str
    pin_hash: str | None = None
    password_hash: str | None = None
    avatar: str = ""
    budgets: dict = field(default_factory=dict)
    grants: list[str] = field(default_factory=list)
    denies: list[str] = field(default_factory=list)
    session_timeout: int | None = None
    created: str = ""
    disabled: bool = False
    epoch: int = 0
    # 4.0: which AI backends this profile may use - {"allowed": [...], "default": ...,
    # "models": {...}}. {} = inherit the install's (today's behaviour). Stored and read
    # only for now: nothing consults it yet, and it is not in public().
    backends: dict = field(default_factory=dict)

    @property
    def has_pin(self) -> bool:
        return bool(self.pin_hash)

    @property
    def has_password(self) -> bool:
        return bool(self.password_hash)

    def public(self) -> dict:
        """Everything a client may see - never a hash."""
        return {
            "id": self.id, "name": self.name, "role": self.role, "avatar": self.avatar,
            "has_pin": self.has_pin, "has_password": self.has_password,
            "budgets": dict(self.budgets), "grants": list(self.grants),
            "denies": list(self.denies), "session_timeout": self.session_timeout,
            "created": self.created, "disabled": self.disabled,
        }


def _row_profile(r: sqlite3.Row) -> Profile:
    def _j(text, default):
        try:
            v = json.loads(text or "")
        except ValueError:
            return default
        return v if isinstance(v, type(default)) else default
    return Profile(
        id=r["id"], name=r["name"], role=r["role"], pin_hash=r["pin_hash"],
        password_hash=r["password_hash"], avatar=r["avatar"] or "",
        budgets=_j(r["budgets"], {}), grants=[str(x) for x in _j(r["grants"], [])],
        denies=[str(x) for x in _j(r["denies"], [])],
        session_timeout=r["session_timeout"], created=r["created"],
        disabled=bool(r["disabled"]), epoch=int(r["epoch"] or 0),
        backends=_j(r["backends"], {}) if "backends" in r.keys() else {})


#: path -> {id: Profile}. Invalidated by every write in this process.
_profiles: dict[str, dict[str, Profile]] = {}
#: path -> (``account_rev`` when the cache was filled, monotonic time it was last checked).
_rev_seen: dict[str, tuple[int, float]] = {}
#: How often a cached read looks at ``account_rev`` to see whether ANOTHER process wrote.
REV_RECHECK_SECONDS = 1.0


def _read_rev(con: sqlite3.Connection) -> int:
    row = con.execute("SELECT rev FROM account_rev WHERE id = 1").fetchone()
    return int(row[0]) if row else 0


def _bump_rev(con: sqlite3.Connection) -> None:
    con.execute("INSERT INTO account_rev (id, rev) VALUES (1, 1) "
                "ON CONFLICT(id) DO UPDATE SET rev = rev + 1")


def _all(path: Path | None = None) -> dict[str, Profile]:
    path = path or db_path()
    key = str(path)
    with _lock:
        hit = _profiles.get(key)
        seen = _rev_seen.get(key)
        now = time.monotonic()
        if hit is not None and seen is not None and now - seen[1] >= REV_RECHECK_SECONDS:
            with connect(path) as con:
                rev = _read_rev(con)
            if rev != seen[0]:
                hit = None                      # another process changed an account
            else:
                _rev_seen[key] = (rev, now)
        if hit is None:
            with connect(path) as con:
                rev = _read_rev(con)
                rows = con.execute("SELECT * FROM profiles ORDER BY created, name").fetchall()
            hit = {r["id"]: _row_profile(r) for r in rows}
            _profiles[key] = hit
            _rev_seen[key] = (rev, now)
        return hit


def invalidate() -> None:
    with _lock:
        _profiles.clear()
        _rev_seen.clear()


def reset_for_tests() -> None:
    """Forget every cache (the schema-ready set and the profile cache)."""
    with _lock:
        _profiles.clear()
        _rev_seen.clear()
        _ready.clear()


# --------------------------------------------------------------------------- #
# Profiles
# --------------------------------------------------------------------------- #


def list_profiles() -> list[Profile]:
    return list(_all().values())


def get_profile(profile_id: str | None) -> Profile | None:
    return _all().get(profile_id) if profile_id else None


def get_by_name(name: str) -> Profile | None:
    want = (name or "").strip().casefold()
    return next((p for p in _all().values() if p.name.casefold() == want), None) if want else None


def get_owner() -> Profile | None:
    return next((p for p in _all().values() if p.role == "owner"), None)


def has_owner() -> bool:
    return get_owner() is not None


def _clean_name(name: str) -> str:
    n = " ".join((name or "").split())
    if not (1 <= len(n) <= 40):
        raise AccountError("profile_name_invalid", "A profile name needs 1 to 40 characters.")
    return n


def _check_role(role: str) -> str:
    if role not in ROLES:
        raise AccountError("profile_role_invalid", f"Unknown role {role!r}.")
    return role


def create_profile(name: str, role: str, *, pin_hash: str | None = None,
                   password_hash: str | None = None, avatar: str = "",
                   budgets: dict | None = None, grants: list[str] | None = None,
                   denies: list[str] | None = None,
                   session_timeout: int | None = None,
                   profile_id: str | None = None) -> Profile:
    name = _clean_name(name)
    _check_role(role)
    if role == "owner" and has_owner():
        raise AccountError("owner_exists", "This install already has an owner.", 409)
    if get_by_name(name) is not None:
        raise AccountError("profile_name_taken", f"A profile named {name!r} already exists.", 409)
    pid = profile_id or new_id()
    with _lock, connect() as con:
        try:
            con.execute(
                "INSERT INTO profiles (id,name,role,pin_hash,password_hash,avatar,budgets,"
                "grants,denies,session_timeout,created) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (pid, name, role, pin_hash, password_hash, avatar or "",
                 json.dumps(budgets or {}), json.dumps(grants or []),
                 json.dumps(denies or []), session_timeout, now_iso()))
        except sqlite3.IntegrityError as e:
            raise AccountError("profile_conflict", "That profile could not be created "
                               "(duplicate name or second owner).", 409) from e
        if role == "owner":
            _adopt_unowned(con, pid)
        _bump_rev(con)
        invalidate()
    p = get_profile(pid)
    assert p is not None
    return p


def _adopt_unowned(con: sqlite3.Connection, owner_id: str) -> None:
    """The Owner was just created: every thread and file that had no owner yet (made on
    an install with no Owner - first-run guides, an open install's chats) is the Owner's.
    So is a row whose owner no longer exists: "remove the lock" on a PIN-only install
    deletes the Owner row and the next PIN creates a new one with a new id - its chats
    must follow it, not dangle. The tables live in the same chats.db; on a database
    without them this is a no-op."""
    have = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table in ("threads", "files"):
        if table not in have:
            continue
        cols = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        if "owner_profile_id" in cols:
            con.execute(f"UPDATE {table} SET owner_profile_id = ? WHERE owner_profile_id IS NULL "
                        "OR owner_profile_id NOT IN (SELECT id FROM profiles)", (owner_id,))


_UPDATABLE = {"name", "role", "pin_hash", "password_hash", "avatar", "budgets", "grants",
              "denies", "session_timeout", "disabled", "backends"}


def update_profile(profile_id: str, **fields) -> Profile:
    """Patch a profile. Any change to role, credentials or ``disabled`` bumps the epoch
    (every live session of the profile dies) - the caller drops them from memory."""
    cur = get_profile(profile_id)
    if cur is None:
        raise AccountError("profile_not_found", "No such profile.", 404)
    sets: list[str] = []
    args: list = []
    bump = False
    for k, v in fields.items():
        if k not in _UPDATABLE:
            raise AccountError("profile_field_invalid", f"Cannot set {k!r}.")
        if k == "name":
            v = _clean_name(v)
            other = get_by_name(v)
            if other is not None and other.id != profile_id:
                raise AccountError("profile_name_taken", f"A profile named {v!r} already exists.", 409)
        if k == "role":
            _check_role(v)
            if v == "owner" and cur.role != "owner":
                raise AccountError("owner_exists", "Ownership is not transferred through a role change.", 409)
            if cur.role == "owner" and v != "owner":
                raise AccountError("owner_immutable", "The owner's role cannot be changed.", 409)
            bump = bump or v != cur.role
        if k in ("budgets", "backends"):
            if k == "backends" and not isinstance(v or {}, dict):
                raise AccountError("profile_field_invalid", "backends must be an object.")
            v = json.dumps(v or {})
        elif k in ("grants", "denies"):
            v = json.dumps(list(v or []))
        elif k == "disabled":
            if cur.role == "owner" and v:
                raise AccountError("owner_immutable", "The owner cannot be disabled.", 409)
            v = 1 if v else 0
            bump = True
        elif k in ("pin_hash", "password_hash"):
            bump = True
        sets.append(f"{k} = ?")
        args.append(v)
    if not sets:
        return cur
    if bump:
        sets.append("epoch = epoch + 1")
    args.append(profile_id)
    with _lock, connect() as con:
        try:
            con.execute(f"UPDATE profiles SET {', '.join(sets)} WHERE id = ?", args)
        except sqlite3.IntegrityError as e:
            raise AccountError("profile_conflict", "That change conflicts with another profile.", 409) from e
        _bump_rev(con)
        invalidate()
    out = get_profile(profile_id)
    assert out is not None
    return out


def delete_profile(profile_id: str) -> None:
    cur = get_profile(profile_id)
    if cur is None:
        raise AccountError("profile_not_found", "No such profile.", 404)
    if cur.role == "owner":
        raise AccountError("owner_immutable", "The owner cannot be deleted.", 409)
    with _lock, connect() as con:
        con.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))
        _bump_rev(con)
        invalidate()


# --------------------------------------------------------------------------- #
# Devices
# --------------------------------------------------------------------------- #


@dataclass
class Device:
    id: str
    label: str = ""
    default_profile_id: str | None = None
    bound_at: str | None = None
    created: str = ""
    last_seen: str | None = None
    user_agent: str = ""

    def public(self) -> dict:
        return {"id": self.id, "label": self.label,
                "default_profile_id": self.default_profile_id, "bound_at": self.bound_at,
                "created": self.created, "last_seen": self.last_seen}


def _row_device(r: sqlite3.Row) -> Device:
    return Device(id=r["id"], label=r["label"] or "", default_profile_id=r["default_profile_id"],
                  bound_at=r["bound_at"], created=r["created"], last_seen=r["last_seen"],
                  user_agent=r["user_agent"] or "")


#: path -> {device id: default profile id}. A bound device is looked up on every
#: sessionless request, so it must not cost a query.
_bound: dict[str, dict[str, str | None]] = {}


def _bound_map() -> dict[str, str | None]:
    key = str(db_path())
    with _lock:
        hit = _bound.get(key)
        if hit is None:
            with connect() as con:
                rows = con.execute("SELECT id, default_profile_id FROM devices "
                                   "WHERE default_profile_id IS NOT NULL").fetchall()
            hit = {r["id"]: r["default_profile_id"] for r in rows}
            _bound[key] = hit
        return hit


def default_profile_for_device(device_id: str | None) -> Profile | None:
    if not device_id:
        return None
    pid = _bound_map().get(device_id)
    p = get_profile(pid) if pid else None
    return p if p is not None and not p.disabled else None


def ensure_device(device_id: str, user_agent: str = "", label: str = "") -> Device:
    ua = (user_agent or "")[:160]
    with _lock, connect() as con:
        con.execute(
            "INSERT INTO devices (id,label,created,last_seen,user_agent) VALUES (?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET last_seen = excluded.last_seen",
            (device_id, label[:60], now_iso(), now_iso(), ua))
        row = con.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
    return _row_device(row)


def get_device(device_id: str) -> Device | None:
    with connect() as con:
        row = con.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
    return _row_device(row) if row else None


def list_devices() -> list[Device]:
    with connect() as con:
        rows = con.execute("SELECT * FROM devices ORDER BY created").fetchall()
    return [_row_device(r) for r in rows]


def bind_device(device_id: str, profile_id: str | None, label: str | None = None) -> Device:
    """Bind a device to a default profile (``None`` unbinds). Owner/Admin profiles
    cannot be a device default: a bound device opens WITHOUT a credential."""
    if profile_id is not None:
        p = get_profile(profile_id)
        if p is None:
            raise AccountError("profile_not_found", "No such profile.", 404)
        if p.role in ("owner", "admin"):
            raise AccountError(
                "device_bind_role", "A device can only open as a Member or Restricted profile; "
                "Admins and the Owner always sign in with their credential.", 409)
    with _lock, connect() as con:
        if label is not None:
            con.execute("UPDATE devices SET label = ? WHERE id = ?", (label[:60], device_id))
        con.execute("UPDATE devices SET default_profile_id = ?, bound_at = ? WHERE id = ?",
                    (profile_id, now_iso() if profile_id else None, device_id))
        _bound.pop(str(db_path()), None)
    d = get_device(device_id)
    assert d is not None
    return d


def delete_device(device_id: str) -> None:
    with _lock, connect() as con:
        con.execute("DELETE FROM devices WHERE id = ?", (device_id,))
        _bound.pop(str(db_path()), None)


# --------------------------------------------------------------------------- #
# Session registry (the durable record; the live set is in app.auth)
# --------------------------------------------------------------------------- #


def record_session(token_hash: str, profile_id: str, device_id: str | None, *,
                   persistent: bool, user_agent: str = "", expires: str | None = None) -> None:
    with _lock, connect() as con:
        if device_id:
            con.execute("INSERT OR IGNORE INTO devices (id,created,last_seen) VALUES (?,?,?)",
                        (device_id, now_iso(), now_iso()))
        con.execute(
            "INSERT OR REPLACE INTO sessions (token_hash,profile_id,device_id,created,last_seen,"
            "expires,persistent,user_agent) VALUES (?,?,?,?,?,?,?,?)",
            (token_hash, profile_id, device_id, now_iso(), now_iso(), expires,
             1 if persistent else 0, (user_agent or "")[:160]))


def drop_session(token_hash: str) -> None:
    with _lock, connect() as con:
        con.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))


def drop_profile_sessions(profile_id: str) -> int:
    with _lock, connect() as con:
        cur = con.execute("DELETE FROM sessions WHERE profile_id = ?", (profile_id,))
        return cur.rowcount


def drop_all_sessions(*, keep_persistent: bool = False) -> int:
    with _lock, connect() as con:
        sql = "DELETE FROM sessions" + (" WHERE persistent = 0" if keep_persistent else "")
        return con.execute(sql).rowcount


def list_sessions(profile_id: str | None = None) -> list[dict]:
    with connect() as con:
        if profile_id:
            rows = con.execute("SELECT * FROM sessions WHERE profile_id = ? ORDER BY created",
                               (profile_id,)).fetchall()
        else:
            rows = con.execute("SELECT * FROM sessions ORDER BY created").fetchall()
    return [{"profile_id": r["profile_id"], "device_id": r["device_id"], "created": r["created"],
             "last_seen": r["last_seen"], "persistent": bool(r["persistent"]),
             "user_agent": r["user_agent"]} for r in rows]


# --------------------------------------------------------------------------- #
# Daily usage (budgets)
# --------------------------------------------------------------------------- #


def usage_get(subject: str, kind: str, day: str | None = None) -> int:
    with connect() as con:
        row = con.execute("SELECT amount FROM usage_daily WHERE subject=? AND day=? AND kind=?",
                          (subject, day or today(), kind)).fetchone()
    return int(row["amount"]) if row else 0


def usage_add(subject: str, kind: str, amount: int) -> int:
    """Add ``amount`` (negative refunds) unconditionally; returns the new total (floor 0)."""
    with _lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        try:
            row = con.execute("SELECT amount FROM usage_daily WHERE subject=? AND day=? AND kind=?",
                              (subject, today(), kind)).fetchone()
            total = max(0, (int(row["amount"]) if row else 0) + amount)
            con.execute("INSERT OR REPLACE INTO usage_daily (subject,day,kind,amount) VALUES (?,?,?,?)",
                        (subject, today(), kind, total))
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    return total


def usage_charge(subject: str, kind: str, limit: int, amount: int = 1) -> bool:
    """Atomically charge ``amount`` unless that would exceed ``limit``. False = spent."""
    if amount > limit:
        return False
    with _lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        try:
            row = con.execute("SELECT amount FROM usage_daily WHERE subject=? AND day=? AND kind=?",
                              (subject, today(), kind)).fetchone()
            used = int(row["amount"]) if row else 0
            if used + amount > limit:
                con.execute("COMMIT")
                return False
            con.execute(
                "INSERT INTO usage_daily (subject,day,kind,amount) VALUES (?,?,?,?) "
                "ON CONFLICT(subject,day,kind) DO UPDATE SET amount = amount + excluded.amount",
                (subject, today(), kind, amount))
            con.execute("COMMIT")
            return True
        except Exception:
            con.execute("ROLLBACK")
            raise


def usage_today(subject: str) -> dict[str, int]:
    with connect() as con:
        rows = con.execute("SELECT kind, amount FROM usage_daily WHERE subject=? AND day=?",
                           (subject, today())).fetchall()
    return {r["kind"]: int(r["amount"]) for r in rows}


def prune(keep_days: int = 35) -> None:
    cutoff = (datetime.now() - timedelta(days=keep_days)).strftime("%Y-%m-%d")
    cutoff_ts = (datetime.now(UTC) - timedelta(days=90)).isoformat(timespec="seconds")
    with _lock, connect() as con:
        con.execute("DELETE FROM usage_daily WHERE day < ?", (cutoff,))
        con.execute("DELETE FROM activity WHERE ts < ?", (cutoff_ts,))


# --------------------------------------------------------------------------- #
# Activity and audit log
# --------------------------------------------------------------------------- #


def log_activity(kind: str, *, profile_id: str | None = None, subject: str = "",
                 detail: str = "", category: str = "activity") -> None:
    """Best effort: a logging failure must never fail the request it describes."""
    try:
        with _lock, connect() as con:
            con.execute("INSERT INTO activity (ts,category,kind,profile_id,subject,detail) "
                        "VALUES (?,?,?,?,?,?)",
                        (now_iso(), category, kind, profile_id, subject[:80], detail[:300]))
    except Exception:
        log.warning("could not record activity %s", kind, exc_info=True)


def activity_summary(profile_id: str | None = None, days: int = 7) -> dict:
    """Counts per day and kind for one profile (or everyone), newest day first."""
    since = (datetime.now(UTC) - timedelta(days=max(1, days))).isoformat(timespec="seconds")
    where = "category = 'activity' AND ts >= ?"
    args: list = [since]
    if profile_id:
        where += " AND profile_id = ?"
        args.append(profile_id)
    with connect() as con:
        rows = con.execute(
            f"SELECT substr(ts,1,10) AS day, kind, COUNT(*) AS n FROM activity "
            f"WHERE {where} GROUP BY day, kind ORDER BY day DESC", args).fetchall()
    out: dict[str, dict[str, int]] = {}
    for r in rows:
        out.setdefault(r["day"], {})[r["kind"]] = int(r["n"])
    return {"days": [{"day": d, "counts": c} for d, c in out.items()]}


def recent_activity(profile_id: str | None = None, *, category: str = "activity",
                    limit: int = 100) -> list[dict]:
    with connect() as con:
        if profile_id:
            rows = con.execute(
                "SELECT * FROM activity WHERE category=? AND profile_id=? ORDER BY id DESC LIMIT ?",
                (category, profile_id, limit)).fetchall()
        else:
            rows = con.execute("SELECT * FROM activity WHERE category=? ORDER BY id DESC LIMIT ?",
                               (category, limit)).fetchall()
    return [{"ts": r["ts"], "kind": r["kind"], "profile_id": r["profile_id"],
             "subject": r["subject"], "detail": r["detail"]} for r in rows]


def delete_owner_row() -> None:
    """Remove the owner row - only for "remove the lock" on a PIN-only 2.x install."""
    with _lock, connect() as con:
        con.execute("DELETE FROM profiles WHERE role = 'owner'")
        _bump_rev(con)
        invalidate()


def update_profile_hash_silently(profile_id: str, column: str, value: str) -> None:
    """Replace a credential hash with a stronger hash of the SAME secret: no epoch
    bump, so the session that just signed in with it is not killed."""
    if column not in ("pin_hash", "password_hash"):
        raise AccountError("profile_field_invalid", f"Cannot set {column!r}.")
    with _lock, connect() as con:
        con.execute(f"UPDATE profiles SET {column} = ? WHERE id = ?", (value, profile_id))
        _bump_rev(con)
        invalidate()


class UsageMap:
    """A dict-shaped view of today's ``usage_daily`` rows for one kind.

    The 2.x per-IP counters were plain dicts (``main._decoy_upload_used``,
    ``_decoy_action_used``) that tests and tools read and clear. They are now rows
    in SQLite - so they survive a restart and can be keyed by profile - and this
    adapter keeps the dict spelling working: ``m.get(subject, 0)``, ``m[subject] = n``,
    ``m.clear()``. With ``tuple_keys`` the key is ``(subject, action)`` and the kind is
    ``<prefix><action>``."""

    def __init__(self, kind: str, tuple_keys: bool = False) -> None:
        self.kind = kind
        self.tuple_keys = tuple_keys

    def _split(self, key) -> tuple[str, str]:
        if self.tuple_keys:
            subject, action = key
            return str(subject), f"{self.kind}{action}"
        return str(key), self.kind

    def get(self, key, default: int = 0) -> int:
        subject, kind = self._split(key)
        v = usage_get(subject, kind)
        return v if v else default

    def __getitem__(self, key) -> int:
        return self.get(key, 0)

    def __setitem__(self, key, value: int) -> None:
        subject, kind = self._split(key)
        with _lock, connect() as con:
            con.execute("INSERT OR REPLACE INTO usage_daily (subject,day,kind,amount) VALUES (?,?,?,?)",
                        (subject, today(), kind, max(0, int(value))))

    def __contains__(self, key) -> bool:
        subject, kind = self._split(key)
        return usage_get(subject, kind) > 0

    def clear(self) -> None:
        pattern = self.kind + ("%" if self.tuple_keys else "")
        with _lock, connect() as con:
            con.execute("DELETE FROM usage_daily WHERE kind LIKE ? AND day = ?", (pattern, today()))
