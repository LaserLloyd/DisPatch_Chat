"""Data migrations, with a backup first and a dry run that tells the truth.

``python -m app.migrate --dry-run`` prints what 4.0 would change in your data
directory, touching nothing. Without ``--dry-run`` it takes the backup and applies
the same steps - and so does the server when it starts, so an upgrade needs no
manual step (the dry run is for people who want to see first).

Steps (each idempotent; ``schema_migrations`` in chats.db records what ran):

``001_accounts``       create the profiles / devices / sessions / usage / activity
                       tables (additive: a 2.x binary still runs on the result)
``002_owner_from_pin`` an install with a PIN gets an Owner profile built from the
                       existing PIN hash. No password yet: the first admin action
                       asks for one (``needs_password``)
``003_audience``       every character in config.yaml gets ``audience``
                       (all / members / restricted), computed from the legacy
                       ``safe`` + ``hide_unlocked`` flags, which stay readable
``004_security_keys``  security.yaml gains the machine / session-timeout keys, written
                       explicitly so the owner can see and change them
``005_settings``       the settings tables (chats.db) are created and filled from security.yaml and
                       tools.yaml; keys the schema does not know are carried into
                       ``settings_unknown`` and reported, never dropped
``006_connections``    every character's inline provider block (``api:`` / ``advisor.providers``)
                       gets a Connection: the API key moves into the secret store, the character
                       keeps ``connection: <id>`` beside its (still readable) inline provider,
                       address and model
``007_plugin_approvals`` add-ons and apps that are running at the upgrade are approved (the
                       permissions they request are recorded); a plugin added later needs approval
                       before it can be switched on
``008_agents``         create the agents / tasks / task_events / approvals / spend-ledger
                       tables (additive, like 001: a 3.0 binary without agents ignores them)
``009_ownership``      (4.0) every conversation and File Server record gets an owner
                       profile (``owner_profile_id``), profiles get ``backends`` ({} =
                       inherit). The rule is that NOBODY GAINS ACCESS: a chat goes to the
                       most specific profile that could already read it -
                       a character only one (non-Owner) profile may use -> that profile;
                       a character made for Restricted profiles, on an install with exactly
                       one Restricted profile -> that profile; everything else -> the Owner.
                       ``--assign <character>=<profile>`` and ``--all-to-owner`` override it.
                       Files -> the Owner. Characters are NOT changed (the dry run lists the
                       ones that could become someone's personal character). Push
                       subscriptions record their device's profile; ones whose device is no
                       longer remembered (dead already) are dropped. Additive: a 3.x binary
                       ignores the new columns

Backup: before the first applied step, ``<data>/backups/<version>-<UTC stamp>/`` gets a
consistent copy of chats.db (the SQLite backup API, WAL-safe), config.yaml, security.yaml,
trusted-devices.yaml, push-subscriptions.json and the Image Studio ledger
(apps-data/imagestudio/images.jsonl). The newest five are kept. ``--restore <dir>`` puts
them back.
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import re
import shutil
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import yaml

from . import accounts, agents_store, auth, config, connections, settings_store, tool_manifests

log = logging.getLogger("local-chat.migrate")

TARGET_VERSION = "4.0.0"
KEEP_BACKUPS = 5
#: ``<version>-YYYYMMDD-HHMMSS``; the container entrypoint writes the same shape with the
#: previous image version in front (``3.0.0-b1-...``, ``dev-...``), kept in one list here.
_BACKUP_DIR_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+-]*-\d{8}-\d{6}$")


@dataclass
class Action:
    step: str
    text: str


@dataclass
class Report:
    actions: list[Action] = field(default_factory=list)
    applied: list[str] = field(default_factory=list)
    backup: Path | None = None
    dry_run: bool = False
    ownership: OwnershipPlan | None = None

    @property
    def pending(self) -> bool:
        return bool(self.actions)

    def lines(self) -> list[str]:
        out = [f"[{a.step}] {a.text}" for a in self.actions]
        if not out:
            out = [f"nothing to migrate: this data directory is already on {TARGET_VERSION[:3]}"]
        return out


# --------------------------------------------------------------------------- #
# Inspecting state WITHOUT changing it (the dry run must be exact and silent)
# --------------------------------------------------------------------------- #


def _db_file() -> Path:
    return accounts.db_path()


def _ro_uri(path: Path) -> str:
    return path.resolve().as_uri() + "?mode=ro"


def _table_names(path: Path) -> set[str]:
    if not path.exists():
        return set()
    con = sqlite3.connect(_ro_uri(path), uri=True)
    try:
        return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        con.close()


def _applied_ids(path: Path) -> set[str]:
    if "schema_migrations" not in _table_names(path):
        return set()
    con = sqlite3.connect(_ro_uri(path), uri=True)
    try:
        return {r[0] for r in con.execute("SELECT id FROM schema_migrations")}
    finally:
        con.close()


def _has_owner_row(path: Path) -> bool:
    if "profiles" not in _table_names(path):
        return False
    con = sqlite3.connect(_ro_uri(path), uri=True)
    try:
        return con.execute("SELECT 1 FROM profiles WHERE role='owner'").fetchone() is not None
    finally:
        con.close()


def _raw_roster() -> list[dict] | None:
    p = Path(config.CONFIG_PATH)
    if not p.exists():
        return None
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return None
    ents = raw.get("bots") if isinstance(raw, dict) else None
    return [e for e in ents if isinstance(e, dict) and "id" in e] if isinstance(ents, list) else None


def _audience_of(entry: dict) -> str:
    safe = bool(entry.get("safe", False))
    hide = bool(entry.get("hide_unlocked", False))
    if entry.get("visible") is not None and not bool(entry.get("visible")):
        safe, hide = False, True
    return "restricted" if safe and hide else "all" if safe else "members"


class MigrateError(ValueError):
    """A migration option that cannot be honoured (an unknown character or profile)."""


def plan(*, assign: dict[str, str] | None = None, all_to_owner: bool = False) -> Report:
    """What ``run`` would do, computed from the files as they are. Writes nothing.

    ``assign`` / ``all_to_owner`` are the 009_ownership overrides (``--assign``,
    ``--all-to-owner``)."""
    rep = Report(dry_run=True)
    dbp = _db_file()
    have = _table_names(dbp)
    done = _applied_ids(dbp)
    missing = [t for t in accounts.TABLES if t not in have]
    if "001_accounts" not in done and missing:
        rep.actions.append(Action("001_accounts", "create tables: " + ", ".join(missing)))

    sec = Path(auth.SECURITY_PATH)
    raw_sec = None
    if sec.exists():
        # Read the file raw: auth.load() applies a plaintext `pin:` directive and
        # backfills the recovery code, i.e. it WRITES - a dry run must not.
        try:
            parsed = yaml.safe_load(sec.read_text(encoding="utf-8"))
            raw_sec = parsed if isinstance(parsed, dict) else None
        except (OSError, yaml.YAMLError):
            raw_sec = None
    if raw_sec is not None and "002_owner_from_pin" not in done:
        has_pin = bool((raw_sec.get("pin_hash") and raw_sec.get("salt"))
                       or str(raw_sec.get("pin") or "").strip())
        if has_pin and not _has_owner_row(dbp):
            rep.actions.append(Action(
                "002_owner_from_pin",
                "create the Owner profile from the existing PIN (no password yet - you will be "
                "asked to set one the first time you do something that needs an Admin)"))

    roster = _raw_roster()
    if roster is not None and "003_audience" not in done:
        todo = [e for e in roster if "audience" not in e]
        if todo:
            for e in todo:
                rep.actions.append(Action(
                    "003_audience",
                    f"character {e['id']!r}: safe={bool(e.get('safe', False))} "
                    f"hide_unlocked={bool(e.get('hide_unlocked', False))} -> audience={_audience_of(e)}"))

    if raw_sec is not None and "004_security_keys" not in done:
        if "machine_loopback_trust" not in raw_sec:
            rep.actions.append(Action(
                "004_security_keys",
                "write machine_loopback_trust: true, machine_scopes and per-role session "
                "timeouts into security.yaml (defaults keep today's behaviour)"))

    if "005_settings" not in done:
        imp = settings_store.import_legacy(dry_run=True)
        missing_s = [t for t in settings_store.TABLES if t not in have]
        if missing_s:
            rep.actions.append(Action("005_settings", "create tables: " + ", ".join(missing_s)))
        for line in imp.lines():
            rep.actions.append(Action("005_settings", line))

    if "006_connections" not in done and roster is not None:
        plan6 = connections.adopt_inline([_copy_entry(e) for e in roster], dry_run=True)
        if plan6.linked or plan6.actions:
            if "connections" not in have:
                rep.actions.append(Action("006_connections", "create table: connections"))
            for line in plan6.actions:
                rep.actions.append(Action("006_connections", line))

    if "007_plugin_approvals" not in done:
        running = [r for r in tool_manifests.list_manifests()
                   if r["manifest"]["permissions_requested"] and not r["approved"]
                   and r["state"] not in ("failed", "incompatible") and r["enabled"]]
        if "plugin_approvals" not in have and (running or tool_manifests.list_manifests()):
            rep.actions.append(Action("007_plugin_approvals", "create table: plugin_approvals"))
        for r in running:
            rep.actions.append(Action(
                "007_plugin_approvals",
                f"approve {r['manifest']['kind']} {r['manifest']['id']!r}, already running "
                f"(it asks for: {', '.join(r['manifest']['permissions_requested'])})"))

    if "008_agents" not in done:
        absent = [t for t in agents_store.TABLES if t not in have]
        if absent:
            rep.actions.append(Action(
                "008_agents", "create tables: " + ", ".join(absent)
                + " (agent dispatch; nothing existing is altered)"))

    if "009_ownership" not in done:
        p9 = ownership_plan(assign=assign, all_to_owner=all_to_owner,
                            owner_pending="002_owner_from_pin" in {a.step for a in rep.actions})
        rep.actions.extend(Action("009_ownership", line) for line in p9.lines())
        rep.ownership = p9
    return rep


# --------------------------------------------------------------------------- #
# 009_ownership: who each existing conversation belongs to
# --------------------------------------------------------------------------- #


@dataclass
class Assignment:
    bot_id: str                  # as stored in threads.bot_id
    chats: int
    target: str | None           # profile id (None: no Owner yet - adopted when one is created)
    target_name: str
    reason: str


@dataclass
class OwnershipPlan:
    add_columns: list[str] = field(default_factory=list)
    add_index: bool = False
    assignments: list[Assignment] = field(default_factory=list)
    files: int = 0
    files_target: str | None = None
    owner_name: str = "the Owner"
    candidates: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    push_map: dict[str, str] = field(default_factory=dict)       # key -> profile id
    push_drop: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        out: list[str] = []
        if self.add_columns:
            out.append("add columns: " + ", ".join(self.add_columns)
                       + (" (+ index threads(owner_profile_id, bot_id, updated_at))"
                          if self.add_index else ""))
        width = max((len(a.bot_id) for a in self.assignments), default=0)
        for a in self.assignments:
            out.append(f"character {a.bot_id!r:<{width + 2}} {a.chats:>5} chat(s) -> "
                       f"{a.target_name}  ({a.reason})")
        if self.files:
            out.append(f"files: {self.files} File Server record(s) -> {self.owner_name}")
        if self.push_map or self.push_drop:
            out.append(f"push subscriptions: {len(self.push_map)} get their device's profile, "
                       f"{len(self.push_drop)} dropped (device no longer remembered)")
        out.extend(self.candidates)
        out.extend(self.notes)
        return out

    @property
    def pending(self) -> bool:
        return bool(self.add_columns or self.add_index or self.assignments or self.files
                    or self.push_map or self.push_drop)


#: The thread columns 009 reads and indexes.
_THREAD_COLS = frozenset({"id", "bot_id", "updated_at"})


def _columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}


def _ro_profiles(path: Path) -> list[accounts.Profile]:
    if "profiles" not in _table_names(path):
        return []
    con = sqlite3.connect(_ro_uri(path), uri=True)
    con.row_factory = sqlite3.Row
    try:
        return [accounts._row_profile(r) for r in con.execute("SELECT * FROM profiles")]
    finally:
        con.close()


def _bot_of(entry: dict) -> config.Bot:
    """A roster entry read exactly as config.load_bots reads the fields access depends on
    (audience over the legacy flags, the profile list) - without load_bots' side effects."""
    return config.Bot(
        id=str(entry["id"]), name=str(entry.get("name") or entry["id"]),
        profiles=[str(x) for x in (entry.get("profiles") or []) if isinstance(x, str)]
        if isinstance(entry.get("profiles"), list) else [],
        **config.reconcile_flags(entry.get("audience"), config.fold_visible(
            entry.get("visible"), bool(entry.get("safe", False)),
            bool(entry.get("hide_unlocked", False)))))


def _could_use(bot: config.Bot, prof: accounts.Profile) -> bool:
    from . import characters, policy
    return characters.may_use(bot, policy.profile_principal(prof))


def _default_target(bot: config.Bot | None, by_id: dict[str, accounts.Profile],
                    anon: accounts.Profile | None) -> tuple[accounts.Profile | None, str]:
    """(profile, reason) by the never-widen rule; (None, reason) = the Owner."""
    if bot is None:
        return None, "character no longer in the roster"
    if len(bot.profiles) == 1:
        p = by_id.get(bot.profiles[0])
        if p is not None and p.role != "owner" and not p.disabled and _could_use(bot, p):
            return p, f"only {p.name} may use it"
    if bot.audience == "restricted" and anon is not None and _could_use(bot, anon):
        return anon, f"made for Restricted profiles; {anon.name} is the only Restricted profile"
    return None, "default"


def _resolve_profile(ref: str, profiles: list[accounts.Profile]) -> accounts.Profile:
    want = ref.strip()
    hit = next((p for p in profiles if p.id == want), None) \
        or next((p for p in profiles if p.name.casefold() == want.casefold()), None)
    if hit is None and len(want) >= 6:
        pre = [p for p in profiles if p.id.startswith(want)]
        hit = pre[0] if len(pre) == 1 else None
    if hit is None:
        raise MigrateError(f"--assign: no profile {ref!r} (use its name or id)")
    return hit


def _push_plan(profiles: list[accounts.Profile], owner: accounts.Profile | None,
               dbp: Path) -> tuple[dict[str, str], list[str]]:
    """Which profile each push subscription's device is signed in as. A remembered device's
    key is its session-token hash: trusted-devices.yaml (and the sessions table) name the
    profile; an empty profile there is a session from before accounts = the Owner. A key no
    remembered device has any more is a subscription the next send prunes anyway: dropped."""
    from . import push
    path = Path(config.DATA_DIR) / push.SUBS_FILE
    if not path.exists():
        return {}, []
    subs = push._load_subs()
    trusted: dict = {}
    tp = Path(auth.TRUSTED_PATH)
    if tp.exists():
        try:
            raw = yaml.safe_load(tp.read_text(encoding="utf-8")) or {}
            trusted = raw.get("devices") if isinstance(raw, dict) and isinstance(raw.get("devices"), dict) else {}
        except (OSError, yaml.YAMLError):
            trusted = {}
    sessions: dict[str, str] = {}
    if "sessions" in _table_names(dbp):
        con = sqlite3.connect(_ro_uri(dbp), uri=True)
        try:
            sessions = {r[0]: r[1] for r in con.execute("SELECT token_hash, profile_id FROM sessions")}
        finally:
            con.close()
    known = {p.id for p in profiles}
    mapped: dict[str, str] = {}
    drop: list[str] = []
    for key, sub in subs.items():
        if sub.get("profile_id") in known:
            continue                                    # already recorded
        if key.startswith("open:"):                    # no PIN: everyone is the Owner
            if owner is not None:
                mapped[key] = owner.id
            continue
        if key not in trusted:
            drop.append(key)
            continue
        pid = str((trusted.get(key) or {}).get("profile_id") or "") or sessions.get(key) or ""
        if not pid:
            pid = owner.id if owner is not None else ""
        if pid in known:
            mapped[key] = pid
        elif not pid and owner is None:
            continue                                    # no accounts at all yet: leave it
        else:
            drop.append(key)
    return mapped, drop


def ownership_plan(*, assign: dict[str, str] | None = None, all_to_owner: bool = False,
                   owner_pending: bool = False) -> OwnershipPlan:
    """Read-only: what 009_ownership would do to the data directory as it is now."""
    out = OwnershipPlan()
    dbp = _db_file()
    have = _table_names(dbp)
    profiles = _ro_profiles(dbp)
    by_id = {p.id: p for p in profiles}
    owner = next((p for p in profiles if p.role == "owner"), None)
    out.owner_name = (f"{owner.name} (Owner)" if owner is not None else
                      "the Owner" if owner_pending else
                      "nobody yet (the Owner, once one is created)")
    restricted = [p for p in profiles if p.role == "restricted" and not p.disabled]
    anon = restricted[0] if len(restricted) == 1 else None

    roster = _raw_roster() or []
    entries = {str(e["id"]): e for e in roster}
    lower = {k.lower(): k for k in entries}

    def entry_for(bot_id: str) -> dict | None:
        return entries.get(bot_id) or entries.get(lower.get(bot_id.lower(), ""))

    overrides: dict[str, accounts.Profile] = {}
    for bot_ref, prof_ref in (assign or {}).items():
        overrides[bot_ref.lower()] = _resolve_profile(prof_ref, profiles)

    counts: list[tuple[str, int]] = []
    nfiles = 0
    if dbp.exists() and have:
        con = sqlite3.connect(_ro_uri(dbp), uri=True)
        try:
            if "threads" in have:
                cols = _columns(con, "threads")
                if "owner_profile_id" not in cols:
                    out.add_columns.append("threads.owner_profile_id")
                where = " WHERE owner_profile_id IS NULL" if "owner_profile_id" in cols else ""
                if _THREAD_COLS <= cols:           # anything else is not a DisPatch threads table
                    counts = [(r[0], int(r[1])) for r in con.execute(
                        f"SELECT bot_id, COUNT(*) FROM threads{where} GROUP BY bot_id ORDER BY bot_id")]
                    idx = {r[1] for r in con.execute("PRAGMA index_list(threads)")}
                    out.add_index = "idx_threads_owner_bot_updated" not in idx
            if "files" in have:
                cols = _columns(con, "files")
                if "owner_profile_id" not in cols:
                    out.add_columns.append("files.owner_profile_id")
                where = " WHERE owner_profile_id IS NULL" if "owner_profile_id" in cols else ""
                nfiles = int(con.execute(f"SELECT COUNT(*) FROM files{where}").fetchone()[0])
            if "profiles" in have and "backends" not in _columns(con, "profiles"):
                out.add_columns.append("profiles.backends")
        finally:
            con.close()

    unknown = [b for b in overrides if b not in {c[0].lower() for c in counts} and b not in lower]
    if unknown:
        raise MigrateError(f"--assign: no character {unknown[0]!r} in the roster or the chats")

    for bot_id, n in counts:
        e = entry_for(bot_id)
        bot = _bot_of(e) if e is not None else None
        if bot_id.lower() in overrides:
            p = overrides[bot_id.lower()]
            note = "" if (p.role == "owner" or bot is None or _could_use(bot, p)) else \
                f"; NOTE: {p.name} could not use this character before"
            target, reason = p, f"--assign{note}"
        elif all_to_owner:
            target, reason = None, "--all-to-owner"
        else:
            target, reason = _default_target(bot, by_id, anon)
        if target is not None and target.role == "owner":
            target = None
        tid = target.id if target is not None else (owner.id if owner is not None else None)
        name = (f"{target.name} ({target.role.capitalize()})" if target is not None
                else out.owner_name)
        out.assignments.append(Assignment(bot_id, n, tid, name, reason))
    if nfiles:
        out.files = nfiles
        out.files_target = owner.id if owner is not None else None

    if not all_to_owner:
        for e in roster:
            bot = _bot_of(e)
            p, _ = _default_target(bot, by_id, anon)
            if p is not None:
                out.candidates.append(
                    f"character {bot.id!r} could become a personal character of {p.name} "
                    "(not converted: characters stay household)")

    out.push_map, out.push_drop = _push_plan(profiles, owner, dbp)
    if not out.pending:
        return OwnershipPlan()                      # nothing to do: report nothing at all
    return out


def _apply_ownership(p9: OwnershipPlan) -> None:
    """Carry out an :class:`OwnershipPlan` (computed after 002 created the Owner)."""
    from .database import OWNER_INDEX_DDL
    dbp = _db_file()
    have = _table_names(dbp)
    with accounts.connect() as con:                 # also adds profiles.backends (accounts)
        con.execute("BEGIN IMMEDIATE")
        try:
            for table in ("threads", "files"):
                if table in have and "owner_profile_id" not in _columns(con, table):
                    con.execute(f"ALTER TABLE {table} ADD COLUMN owner_profile_id TEXT")
            if "threads" in have and _THREAD_COLS <= _columns(con, "threads"):
                con.execute(OWNER_INDEX_DDL)
            for a in p9.assignments:
                if a.target:
                    con.execute("UPDATE threads SET owner_profile_id = ? "
                                "WHERE bot_id = ? AND owner_profile_id IS NULL", (a.target, a.bot_id))
            if p9.files_target and "files" in have:
                con.execute("UPDATE files SET owner_profile_id = ? WHERE owner_profile_id IS NULL",
                            (p9.files_target,))
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise
    if p9.push_map or p9.push_drop:
        from . import push
        with push._lock:
            subs = push._load_subs()
            for k, pid in p9.push_map.items():
                if k in subs:
                    subs[k]["profile_id"] = pid
            for k in p9.push_drop:
                subs.pop(k, None)
            push._save_subs(subs)


def _copy_entry(e: dict) -> dict:
    import copy
    return copy.deepcopy(e)


# --------------------------------------------------------------------------- #
# Backup and restore
# --------------------------------------------------------------------------- #


def _has_user_data() -> bool:
    return any(p.exists() for p in (_db_file(), Path(config.CONFIG_PATH), Path(auth.SECURITY_PATH)))


#: Where the push subscriptions and the Image Studio ledger sit, relative to the data dir
#: (and inside a backup directory).
PUSH_SUBS_REL = "push-subscriptions.json"
IMAGE_LEDGER_REL = "apps-data/imagestudio/images.jsonl"


def _backed_up_files() -> list[tuple[str, Path]]:
    """(name inside the backup, live path) for every file besides chats.db."""
    data = Path(config.DATA_DIR)
    return [("config.yaml", Path(config.CONFIG_PATH)), ("security.yaml", Path(auth.SECURITY_PATH)),
            ("trusted-devices.yaml", Path(auth.TRUSTED_PATH)),
            (PUSH_SUBS_REL, data / PUSH_SUBS_REL), (IMAGE_LEDGER_REL, data / IMAGE_LEDGER_REL)]


def backup(version: str = TARGET_VERSION) -> Path:
    """A consistent copy of everything the migration may touch."""
    root = Path(config.BACKUP_DIR)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    dest = root / f"{version}-{stamp}"
    dest.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(Exception):
        from . import fsperm
        fsperm.restrict(dest, directory=True)
    dbp = _db_file()
    if dbp.exists():
        src = sqlite3.connect(_ro_uri(dbp), uri=True)
        dst = sqlite3.connect(str(dest / "chats.db"))
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
    for rel, p in _backed_up_files():
        if p.exists():
            (dest / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dest / rel)
            with contextlib.suppress(OSError):
                (dest / rel).chmod(0o600)
    kept = sorted((d for d in root.iterdir() if d.is_dir() and _BACKUP_DIR_RE.match(d.name)),
                  key=lambda d: d.name[-15:])            # by stamp, whatever version is in front
    for old in kept[:-KEEP_BACKUPS]:
        shutil.rmtree(old, ignore_errors=True)
    return dest


def restore(src: Path) -> list[str]:
    """Put a pre-migration backup back. The server must be stopped."""
    src = Path(src)
    if not src.is_dir():
        raise FileNotFoundError(f"{src} is not a backup directory")
    restored = []
    for name, target in (("chats.db", _db_file()), *_backed_up_files()):
        f = src / name
        if f.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            for suffix in ("-wal", "-shm"):
                with contextlib.suppress(OSError):
                    Path(str(target) + suffix).unlink()
            shutil.copy2(f, target)
            # A copy keeps the backup's mode (on Windows: the folder's ACL). Put it back to owner-only, the
            # same as a fresh write: security.yaml has the PIN hash, config.yaml may carry keys, chats.db the chats.
            from . import backups
            backups.make_private(Path(target))
            restored.append(name)
    accounts.reset_for_tests()
    settings_store.reset_schema_cache()
    connections.reset_schema_cache()
    tool_manifests.reset_schema_cache()
    auth._cache = None
    return restored


# --------------------------------------------------------------------------- #
# Applying
# --------------------------------------------------------------------------- #


def _record(step: str) -> None:
    with accounts.connect() as con:
        con.execute("INSERT OR IGNORE INTO schema_migrations (id, applied_at, app_version) VALUES (?,?,?)",
                    (step, accounts.now_iso(), TARGET_VERSION))


def _apply_audience() -> None:
    """Write ``audience`` on every raw entry and save the roster AS IT IS (like 006): a key
    this version does not know, or one an add-on owns, survives the upgrade. Re-serialising
    through ``config._bot_entry`` would have dropped it."""
    roster = _raw_roster()
    if roster is None:
        return
    for e in roster:
        if "audience" not in e:
            e["audience"] = _audience_of(e)
    config._write_bots(roster)


def _apply_connections() -> None:
    """Give every inline provider block a connection, writing the ROSTER AS IT IS (raw entries,
    so a key this version does not know survives) with the API keys moved to the secret store."""
    roster = _raw_roster()
    if roster is None:
        return
    plan6 = connections.adopt_inline(roster)
    if plan6.linked:
        config._write_bots(roster)
        log.info("connections: %d provider block(s) now refer to a connection (%d created)",
                 plan6.linked, len(plan6.created))


def run(*, dry_run: bool = False, take_backup: bool = True,
        assign: dict[str, str] | None = None, all_to_owner: bool = False) -> Report:
    """Apply (or, with ``dry_run``, just describe) every pending step."""
    if not dry_run:
        # A restore staged from the app (Settings > System > Backups) is applied now, before the
        # database is opened and before anything below looks at the files.
        from . import backups
        backups.apply_pending_restore()
    rep = plan(assign=assign, all_to_owner=all_to_owner)
    rep.dry_run = dry_run
    if dry_run:
        return rep
    # Even when nothing is pending, make sure the schema exists and is recorded.
    if rep.actions and take_backup and _has_user_data():
        rep.backup = backup()
        log.info("pre-migration backup written to %s", rep.backup)
    steps = {a.step for a in rep.actions}
    accounts.reset_for_tests()                       # re-read the files as they are now
    with accounts.connect():                         # creates the tables
        pass
    _record("001_accounts")
    if "002_owner_from_pin" in steps:
        auth.ensure_owner_for_pin()
    _record("002_owner_from_pin")
    if "003_audience" in steps:
        _apply_audience()
    _record("003_audience")
    if "004_security_keys" in steps:
        auth._write(auth.load())
        auth._bust_cache()
    _record("004_security_keys")
    if "005_settings" in steps:
        settings_store.import_legacy()
    _record("005_settings")
    if "006_connections" in steps:
        _apply_connections()
    _record("006_connections")
    if "007_plugin_approvals" in steps:
        tool_manifests.grandfather_running()
    _record("007_plugin_approvals")
    if "008_agents" in steps:
        agents_store.reset_for_tests()
        with agents_store.connect():                 # creates the tables
            pass
    _record("008_agents")
    if "009_ownership" in steps:
        # Re-planned now: 002 may just have created the Owner the chats default to.
        _apply_ownership(ownership_plan(assign=assign, all_to_owner=all_to_owner))
    _record("009_ownership")
    rep.applied = sorted(steps)
    return rep


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.migrate",
                                 description="Migrate a DisPatch data directory to 4.0.")
    ap.add_argument("--data-dir", help="the data directory (default: DISPATCH_DATA_DIR / the usual one)")
    ap.add_argument("--dry-run", action="store_true", help="print what would change; write nothing")
    ap.add_argument("--no-backup", action="store_true", help="skip the pre-migration backup (not advised)")
    ap.add_argument("--restore", metavar="BACKUP_DIR", help="restore a pre-migration backup, then exit")
    ap.add_argument("--assign", action="append", default=[], metavar="CHARACTER=PROFILE",
                    help="009_ownership: give every existing chat with CHARACTER to PROFILE "
                         "(a name or id); repeatable")
    ap.add_argument("--all-to-owner", action="store_true",
                    help="009_ownership: give every existing chat to the Owner")
    args = ap.parse_args(argv)
    if args.data_dir:
        _repoint(Path(args.data_dir))
    if args.restore:
        names = restore(Path(args.restore))
        print("restored: " + (", ".join(names) or "nothing"))
        return 0
    assign: dict[str, str] = {}
    for item in args.assign:
        bot, sep, prof = item.partition("=")
        if not sep or not bot.strip() or not prof.strip():
            print(f"--assign wants CHARACTER=PROFILE, got {item!r}", file=sys.stderr)
            return 2
        assign[bot.strip()] = prof.strip()
    if assign and args.all_to_owner:
        print("--assign and --all-to-owner cannot be combined", file=sys.stderr)
        return 2
    if (assign or args.all_to_owner) and "009_ownership" in _applied_ids(_db_file()):
        print("009_ownership has already run on this data directory; --assign and --all-to-owner "
              "only shape that first run (restore its backup to run it again)", file=sys.stderr)
        return 2
    try:
        rep = run(dry_run=args.dry_run, take_backup=not args.no_backup,
                  assign=assign or None, all_to_owner=args.all_to_owner)
    except MigrateError as e:
        print(str(e), file=sys.stderr)
        return 2
    print(("DRY RUN - nothing was changed.\n" if args.dry_run else "") + "\n".join(rep.lines()))
    if rep.backup:
        print(f"backup: {rep.backup}")
    return 0


def _repoint(data_dir: Path) -> None:
    """Point every module-level path at ``data_dir`` (the CLI's --data-dir)."""
    config.DATA_DIR = data_dir
    config.CONFIG_PATH = data_dir / "config.yaml"
    config.BACKUP_DIR = data_dir / "backups"
    auth.SECURITY_PATH = data_dir / "security.yaml"
    auth.RECOVERY_PATH = data_dir / "RECOVERY-CODE.txt"
    auth.TRUSTED_PATH = data_dir / "trusted-devices.yaml"
    auth.SETUP_TOKEN_PATH = data_dir / "SETUP-TOKEN.txt"
    auth._trusted = None
    config._invalidate_bots_cache()
    accounts.reset_for_tests()
    auth._cache = None


if __name__ == "__main__":      # pragma: no cover
    sys.exit(main())
