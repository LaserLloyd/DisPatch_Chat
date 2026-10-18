"""Data migrations, with a backup first and a dry run that tells the truth.

``python -m app.migrate --dry-run`` prints what 3.0 would change in your data
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
``007_plugin_approvals`` add-ons and apps that are running at the upgrade are approved (the
                       permissions they request are recorded); a plugin added later needs approval
                       before it can be switched on
``006_connections``    every character's inline provider block (``api:`` / ``advisor.providers``)
                       gets a Connection: the API key moves into the secret store, the character
                       keeps ``connection: <id>`` beside its (still readable) inline provider,
                       address and model

Backup: before the first applied step, ``<data>/backups/<version>-<UTC stamp>/`` gets a
consistent copy of chats.db (the SQLite backup API, WAL-safe), config.yaml, security.yaml
and trusted-devices.yaml. The newest five are kept. ``--restore <dir>`` puts them back.
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

from . import accounts, auth, config, connections, settings_store, tool_manifests

log = logging.getLogger("local-chat.migrate")

TARGET_VERSION = "3.0.0"
KEEP_BACKUPS = 5
_BACKUP_DIR_RE = re.compile(r"^\d+\.\d+\.\d+-\d{8}-\d{6}$")


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

    @property
    def pending(self) -> bool:
        return bool(self.actions)

    def lines(self) -> list[str]:
        out = [f"[{a.step}] {a.text}" for a in self.actions]
        if not out:
            out = ["nothing to migrate: this data directory is already on 3.0"]
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


def plan() -> Report:
    """What ``run`` would do, computed from the files as they are. Writes nothing."""
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
    return rep


def _copy_entry(e: dict) -> dict:
    import copy
    return copy.deepcopy(e)


# --------------------------------------------------------------------------- #
# Backup and restore
# --------------------------------------------------------------------------- #


def _has_user_data() -> bool:
    return any(p.exists() for p in (_db_file(), Path(config.CONFIG_PATH), Path(auth.SECURITY_PATH)))


def backup(version: str = TARGET_VERSION) -> Path:
    """A consistent copy of everything the migration may touch."""
    root = Path(config.BACKUP_DIR)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    dest = root / f"{version}-{stamp}"
    dest.mkdir(parents=True, exist_ok=True)
    dbp = _db_file()
    if dbp.exists():
        src = sqlite3.connect(_ro_uri(dbp), uri=True)
        dst = sqlite3.connect(str(dest / "chats.db"))
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
    for p in (Path(config.CONFIG_PATH), Path(auth.SECURITY_PATH), Path(auth.TRUSTED_PATH)):
        if p.exists():
            shutil.copy2(p, dest / p.name)
            with contextlib.suppress(OSError):
                (dest / p.name).chmod(0o600)
    kept = sorted((d for d in root.iterdir() if d.is_dir() and _BACKUP_DIR_RE.match(d.name)),
                  key=lambda d: d.name)
    for old in kept[:-KEEP_BACKUPS]:
        shutil.rmtree(old, ignore_errors=True)
    return dest


def restore(src: Path) -> list[str]:
    """Put a pre-migration backup back. The server must be stopped."""
    src = Path(src)
    if not src.is_dir():
        raise FileNotFoundError(f"{src} is not a backup directory")
    restored = []
    for name, target in (("chats.db", _db_file()), ("config.yaml", Path(config.CONFIG_PATH)),
                         ("security.yaml", Path(auth.SECURITY_PATH)),
                         ("trusted-devices.yaml", Path(auth.TRUSTED_PATH))):
        f = src / name
        if f.exists():
            for suffix in ("-wal", "-shm"):
                with contextlib.suppress(OSError):
                    Path(str(target) + suffix).unlink()
            shutil.copy2(f, target)
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
    bots = config.load_bots()
    config._write_bots([config._bot_entry(b) for b in bots])


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


def run(*, dry_run: bool = False, take_backup: bool = True) -> Report:
    """Apply (or, with ``dry_run``, just describe) every pending step."""
    if not dry_run:
        # A restore staged from the app (Settings > System > Backups) is applied now, before the
        # database is opened and before anything below looks at the files.
        from . import backups
        backups.apply_pending_restore()
    rep = plan()
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
    rep.applied = sorted(steps)
    return rep


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.migrate",
                                 description="Migrate a DisPatch data directory to 3.0.")
    ap.add_argument("--data-dir", help="the data directory (default: DISPATCH_DATA_DIR / the usual one)")
    ap.add_argument("--dry-run", action="store_true", help="print what would change; write nothing")
    ap.add_argument("--no-backup", action="store_true", help="skip the pre-migration backup (not advised)")
    ap.add_argument("--restore", metavar="BACKUP_DIR", help="restore a pre-migration backup, then exit")
    args = ap.parse_args(argv)
    if args.data_dir:
        _repoint(Path(args.data_dir))
    if args.restore:
        names = restore(Path(args.restore))
        print("restored: " + (", ".join(names) or "nothing"))
        return 0
    rep = run(dry_run=args.dry_run, take_backup=not args.no_backup)
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
    config._invalidate_bots_cache()
    accounts.reset_for_tests()
    auth._cache = None


if __name__ == "__main__":      # pragma: no cover
    sys.exit(main())
