"""Backups: scheduled, encrypted, restorable, and the support bundle.

Three kinds of backup sit side by side under ``<data>/backups/`` and are listed together:

``archive``        ``archives/dispatch-<stamp>.dpbk`` - everything you would need on a new computer:
                   chats.db (a consistent SQLite snapshot), the settings and roster files,
                   avatars, the reaction pack, the connection API keys (from the secret store) and,
                   if asked, the media and File Server folders. **Encrypted** (see below).
``snapshot``       ``chats-<stamp>.db`` - the rotating online database copies ``main._make_backup``
                   has always written (plain SQLite, every 6 hours by default).
``premigration``   ``<version>-<stamp>/`` - what ``migrate.py`` copies before it changes anything.
``prerestore``     ``pre-restore-<stamp>/`` - what a restore copies before it replaces anything
                   (4.3: its own kind, kept 3, never counted as a pre-migration copy).

Encryption (``.dpbk``): AES-256-GCM over a gzip'd tar, in 1 MiB chunks, key = scrypt(passphrase, salt).
The header (magic, KDF parameters, salt, nonce prefix) is bound into every chunk's authentication
data together with the chunk number and a "last chunk" flag, so a flipped bit, a reordered chunk and a
truncated file all fail to decrypt. The passphrase is yours (``system.backup_passphrase``) or, if you
set none, a recovery key DisPatch generates and shows once.

Restoring from the running app *stages* the files and applies them at the next start (the database is
open while the server runs); the current files are copied to ``backups/pre-restore-<stamp>/`` first.
Offline: ``python -m app.backups restore <file> --passphrase-file <f>``.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import hashlib
import io
import json
import logging
import os
import re
import secrets as pysecrets
import shutil
import sqlite3
import sys
import tarfile
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from . import __version__, config, fsperm, secret_store

log = logging.getLogger("local-chat.backups")

MAGIC = b"DPBK1\n"
CHUNK = 1 << 20
#: scrypt cost (N=2**15, r=8, p=1 = 32 MiB, ~100 ms). Tests lower it.
SCRYPT_N = 2 ** 15
EXT = ".dpbk"
ARCHIVE_DIRNAME = "archives"
PENDING = ".restore-pending.json"
_RECOVERY_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"

#: Files in the data directory an archive carries (those that exist).
DATA_FILES = (
    "config.yaml", "security.yaml", "trusted-devices.yaml", "tools.yaml", "extensions.yaml",
    "image.yaml", "quick-actions.yaml", "local-viewer.yaml", "reactions.yaml", "reaction-pool.yaml",
    "reaction-prompts.yaml", "push-vapid.json", "push-subscriptions.json", "client-links.json",
    "gateway-mirror.json", "media-origins.json", "voice.yaml", "voice-acks.yaml", "advisor.db",
    "RECOVERY-CODE.txt",
)
DATA_DIRS = ("avatars", "profile-avatars", "reactions/pack", "voices")
MEDIA_DIRS = ("media", "files")
#: top-level names a restore may write; anything else in an archive is refused.
ALLOWED_TOP = frozenset({"chats.db", "MANIFEST.json", "secrets.json", *DATA_FILES,
                         "avatars", "profile-avatars", "reactions", "voices", "media", "files"})


def make_private(path: Path, *, directory: bool = False) -> None:
    """Owner-only, as a fresh write would be: 0600 / 0700 on POSIX, an owner-only ACL on Windows
    (``app/fsperm.py``). Never raises: a filesystem that cannot hold permissions (a FAT share) is logged
    by ``fsperm`` and the restore carries on."""
    with contextlib.suppress(Exception):
        fsperm.restrict(path, directory=directory)


def harden_restored(data: Path, rels: list[str] | tuple[str, ...]) -> None:
    """After a restore wrote ``rels`` (data-directory-relative, ``/``-separated): every top-level file
    (``security.yaml``, ``config.yaml``, ``chats.db``, the VAPID key, ``RECOVERY-CODE.txt`` ...) is made
    owner-only, and so is the secret store's own file (``agents/secrets.json``, the keyring fallback) and its
    folder. Media and avatars below a folder are left to the folder's permissions: there can be thousands."""
    for rel in rels:
        if "/" not in rel:
            make_private(data / rel)
    sec = data / "agents" / "secrets.json"
    if sec.is_file():
        make_private(sec)
        make_private(sec.parent, directory=True)


class BackupError(Exception):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


# --------------------------------------------------------------------------- #
# Paths and ids
# --------------------------------------------------------------------------- #


def backup_root() -> Path:
    return Path(config.BACKUP_DIR)


def archive_dir() -> Path:
    return backup_root() / ARCHIVE_DIRNAME


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%d-%H%M%S")


def recovery_key() -> str:
    """A fresh, readable recovery key: 5 groups of 5 from an alphabet without look-alikes (~120 bits)."""
    return "-".join("".join(pysecrets.choice(_RECOVERY_ALPHABET) for _ in range(5)) for _ in range(5))


PASSPHRASE_REF = "setting.system.backup_passphrase"
#: sha256 of the recovery key DisPatch generated (4.3, N-16): the reveal route shows the stored
#: passphrase only while it is still that generated key, never one a person chose.
GENERATED_REF = "backup.generated_key_sha256"
_RECOVERY_SHAPE = re.compile(r"^[A-HJKMNP-Z2-9]{5}(?:-[A-HJKMNP-Z2-9]{5}){4}$")


def configured_passphrase() -> str | None:
    return secret_store.get(PASSPHRASE_REF)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def store_generated_key(key: str) -> None:
    """Keep a generated recovery key as the install's backup passphrase, and remember that it was
    generated (so the protected Admin may see it again later, after re-entering a credential)."""
    secret_store.put(PASSPHRASE_REF, key)
    secret_store.put(GENERATED_REF, _digest(key))


def generated_key() -> str | None:
    """The stored passphrase when DisPatch generated it, else None (a passphrase somebody chose is
    never shown). A key generated before 4.3 left no record: one that has exactly the recovery-key
    shape and no record saying otherwise counts as generated."""
    pw = configured_passphrase()
    if not pw:
        return None
    mark = secret_store.get(GENERATED_REF)
    if mark:
        return pw if pysecrets.compare_digest(mark, _digest(pw)) else None
    return pw if _RECOVERY_SHAPE.match(pw) else None


# --------------------------------------------------------------------------- #
# Crypto stream
# --------------------------------------------------------------------------- #


def _derive(passphrase: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return Scrypt(salt=salt, length=32, n=n, r=r, p=p).derive(passphrase.encode("utf-8"))


def _aad(header_hash: bytes, counter: int, final: bool) -> bytes:
    return header_hash + counter.to_bytes(4, "big") + (b"\x01" if final else b"\x00")


class _EncWriter(io.RawIOBase):
    """File-like that encrypts what is written to it in CHUNK-sized AES-GCM records."""

    def __init__(self, fh, key: bytes, header_hash: bytes, nonce_prefix: bytes) -> None:
        super().__init__()
        self._fh = fh
        self._gcm = AESGCM(key)
        self._hh = header_hash
        self._np = nonce_prefix
        self._buf = bytearray()
        self._pending: bytes | None = None
        self._n = 0

    def writable(self) -> bool:
        return True

    def _emit(self, data: bytes, final: bool) -> None:
        nonce = self._np + self._n.to_bytes(4, "big")
        ct = self._gcm.encrypt(nonce, data, _aad(self._hh, self._n, final))
        self._fh.write(len(ct).to_bytes(4, "big"))
        self._fh.write(ct)
        self._n += 1

    def write(self, b) -> int:                      # type: ignore[override]
        self._buf += b
        while len(self._buf) >= CHUNK:
            chunk = bytes(self._buf[:CHUNK])
            del self._buf[:CHUNK]
            if self._pending is not None:
                self._emit(self._pending, False)
            self._pending = chunk
        return len(b)

    def close(self) -> None:
        if not self.closed and self._fh.closed:
            super().close()                       # an aborted write: nothing left to flush into
            return
        if not self.closed:
            if self._buf or self._pending is None:
                if self._pending is not None:
                    self._emit(self._pending, False)
                self._pending = bytes(self._buf)
                self._buf.clear()
            assert self._pending is not None
            self._emit(self._pending, True)
            self._pending = None
        super().close()


class _DecReader(io.RawIOBase):
    """File-like over the decrypted stream; raises BackupError on a wrong key or damaged file."""

    def __init__(self, fh, key: bytes, header_hash: bytes, nonce_prefix: bytes) -> None:
        super().__init__()
        self._fh = fh
        self._gcm = AESGCM(key)
        self._hh = header_hash
        self._np = nonce_prefix
        self._n = 0
        self._buf = b""
        self._done = False

    def readable(self) -> bool:
        return True

    def _next(self) -> None:
        raw_len = self._fh.read(4)
        if len(raw_len) < 4:
            raise BackupError("backup_corrupt", "The backup file is cut short.")
        ln = int.from_bytes(raw_len, "big")
        if ln < 16 or ln > CHUNK + 64:
            raise BackupError("backup_corrupt", "The backup file is damaged.")
        ct = self._fh.read(ln)
        if len(ct) < ln:
            raise BackupError("backup_corrupt", "The backup file is cut short.")
        final = not self._fh.read(1)
        nonce = self._np + self._n.to_bytes(4, "big")
        try:
            self._buf = self._gcm.decrypt(nonce, ct, _aad(self._hh, self._n, final))
        except InvalidTag:
            # Would it have decrypted if this were NOT the last chunk? Then the file was cut short.
            with contextlib.suppress(InvalidTag):
                self._gcm.decrypt(nonce, ct, _aad(self._hh, self._n, not final))
                raise BackupError("backup_corrupt", "The backup file is cut short.") from None
            if self._n == 0:
                raise BackupError("backup_wrong_key", "The passphrase is wrong, or this is not a DisPatch backup.") from None
            raise BackupError("backup_corrupt", "The backup file is damaged or incomplete.") from None
        if not final:
            self._fh.seek(-1, os.SEEK_CUR)
        self._n += 1
        self._done = final

    def readinto(self, b) -> int:                   # type: ignore[override]
        if not self._buf:
            if self._done:
                return 0
            self._next()
        n = min(len(b), len(self._buf))
        b[:n] = self._buf[:n]
        self._buf = self._buf[n:]
        return n


def _open_decrypt(path: Path, passphrase: str):
    """(file handle, reader) for ``path``; the caller closes the handle."""
    fh = open(path, "rb")
    try:
        if fh.read(len(MAGIC)) != MAGIC:
            raise BackupError("backup_corrupt", "This is not a DisPatch backup file.")
        hlen = int.from_bytes(fh.read(2), "big")
        raw = fh.read(hlen)
        try:
            hdr = json.loads(raw)
            salt = base64.b64decode(hdr["salt"])
            nonce_prefix = base64.b64decode(hdr["nonce"])
            kdf = hdr["kdf"]
            key = _derive(passphrase, salt, int(kdf["n"]), int(kdf["r"]), int(kdf["p"]))
        except (ValueError, KeyError, TypeError):
            raise BackupError("backup_corrupt", "The backup header is unreadable.") from None
        return fh, _DecReader(fh, key, hashlib.sha256(raw).digest(), nonce_prefix), hdr
    except BaseException:
        fh.close()
        raise


# --------------------------------------------------------------------------- #
# Creating an archive
# --------------------------------------------------------------------------- #


def _sqlite_snapshot(src: Path, dst: Path) -> None:
    s = sqlite3.connect(src.resolve().as_uri() + "?mode=ro", uri=True)
    d = sqlite3.connect(str(dst))
    try:
        s.backup(d)
    finally:
        d.close()
        s.close()


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def _walk(root: Path, rel: str) -> list[tuple[Path, str]]:
    base = root / rel
    if not base.is_dir():
        return []
    out = []
    for p in sorted(base.rglob("*")):
        if p.is_file() and not p.is_symlink():
            out.append((p, p.relative_to(root).as_posix()))
    return out


# --- the secret stores (4.3, issue #35) -------------------------------------------------------
#
# Two stores hold credentials. ``secret_store`` (connection keys, secret settings, anything a
# later feature adds such as an OIDC client secret) and the agents' provider (``agents/secrets.py``:
# the Anthropic key, the gateway token, ``MCP_*`` / ``STUDIOFORGE_*`` header secrets). Both fall
# back to ONE file, ``<data>/agents/secrets.json``; with an OS keyring they use two keyring
# services. An archive carries every name either store holds, each name once, in one flat
# ``secrets.json`` ({name: value}, the format older versions read); a restore sends each name back
# to the store its reader asks (``_is_agent_name``).

#: Never inside an archive: the archive's own key, and the record that it was generated.
_NOT_ARCHIVED = frozenset({PASSPHRASE_REF, GENERATED_REF})
_AGENT_CONSTANTS = ("ANTHROPIC_API_KEY", "OPENCLAW_GATEWAY_TOKEN")


def _secrets_file() -> Path:
    return Path(config.DATA_DIR) / "agents" / "secrets.json"


def _file_names() -> set[str]:
    """Every name in the shared 0600 file (the store of both, without a keyring)."""
    try:
        doc = json.loads(_secrets_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return {str(k) for k in doc} if isinstance(doc, dict) else set()


def _is_agent_name(name: str) -> bool:
    """A name the agents' provider owns: the two constants and the ``MCP_`` / ``STUDIOFORGE_``
    names ``agents_api`` lets a person set. Everything else is ``secret_store``'s."""
    if name in _AGENT_CONSTANTS:
        return True
    return bool(re.fullmatch(r"[A-Z][A-Z0-9_]{2,63}", name)) and name.startswith(("MCP_", "STUDIOFORGE_"))


def _agent_store():
    """The agents' secret store WITHOUT the environment (an operator's ``ANTHROPIC_API_KEY`` in a
    unit file is not the install's to copy): the running task manager's own store when there is
    one, else what ``agents.secrets.default_provider`` would build. ``DISPATCH_SECRET_STORE=file``
    (containers, the test suite) means the file and never a keyring."""
    from .agents.secrets import ChainSecretProvider, EnvSecretProvider, FileSecretProvider
    if secret_store._mode() == "file":
        return FileSecretProvider(_secrets_file())
    with contextlib.suppress(Exception):
        from . import agents_service
        if agents_service.manager is not None:
            sp = agents_service.manager.secrets
            ps = [p for p in getattr(sp, "providers", [sp]) if not isinstance(p, EnvSecretProvider)]
            if ps:
                return ChainSecretProvider(ps)
    from .agents.secrets import default_provider
    sp = default_provider(Path(config.DATA_DIR))
    return ChainSecretProvider([p for p in getattr(sp, "providers", [sp])
                                if not isinstance(p, EnvSecretProvider)])


def _store_for(name: str):
    return _agent_store() if _is_agent_name(name) else secret_store.provider()


def _known_store_names() -> set[str]:
    """Names ``secret_store`` is known to hold even when a keyring hides them from a listing:
    every connection's ``secret:`` reference and every secret setting."""
    out: set[str] = set()
    with contextlib.suppress(Exception):
        from . import connections
        for c in connections.list_all():
            if c.credential_ref.startswith("secret:"):
                out.add(c.credential_ref[7:])
    with contextlib.suppress(Exception):
        from . import settings_schema
        out.update(f"setting.{d.key}" for d in settings_schema.all_defs() if d.secret)
    return out


def _known_agent_names() -> set[str]:
    """The agents' names: the two constants and every header secret an agent definition names."""
    out = set(_AGENT_CONSTANTS)
    with contextlib.suppress(Exception):
        from . import agents_store
        for a in agents_store.list_agents():
            for srv in ((a.get("spec") or {}).get("mcp_servers") or {}).values():
                out.update(str(v) for v in (srv.get("header_secrets") or {}).values())
    return out


def _secrets_document() -> dict[str, str]:
    """Every secret either store holds, by name, for the archive (encrypted like everything else).
    Generic: the shared file is listed whole, so a name no code here knows (a later feature's)
    travels too; names a keyring hides are found through what refers to them."""
    out: dict[str, str] = {}
    names = _file_names() | _known_store_names() | _known_agent_names()
    for name in sorted(names - _NOT_ARCHIVED):
        try:
            v = _store_for(name).get(name)
        except Exception:
            log.warning("could not read the secret %s for the backup", name)
            continue
        if v:
            out[name] = v
    return out


def _referenced_names(db: Path) -> set[str]:
    """Secret names the data in ``db`` refers to: connections' ``secret:`` references and the
    agents' header secrets. Read-only; a missing table or file is simply nothing."""
    out: set[str] = set()
    if not db.is_file():
        return out
    try:
        con = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error:
        return out
    try:
        with contextlib.suppress(sqlite3.Error):
            for (ref,) in con.execute("SELECT credential_ref FROM connections"):
                if isinstance(ref, str) and ref.startswith("secret:") and len(ref) > 7:
                    out.add(ref[7:])
        with contextlib.suppress(sqlite3.Error):
            for (spec,) in con.execute("SELECT spec FROM agents"):
                with contextlib.suppress(ValueError, TypeError, AttributeError):
                    for srv in ((json.loads(spec) or {}).get("mcp_servers") or {}).values():
                        out.update(str(v) for v in (srv.get("header_secrets") or {}).values())
    finally:
        con.close()
    return out - _NOT_ARCHIVED


def _missing(names: set[str] | list[str]) -> list[str]:
    """The names neither store has a value for now (each asked of its own store)."""
    out = []
    for n in sorted(set(names)):
        try:
            if not _store_for(n).get(n):
                out.append(n)
        except Exception:
            out.append(n)
    return out


@contextlib.contextmanager
def encrypted_tar(path: Path, passphrase: str, *, created: str = ""):
    """Open ``path`` for writing as an encrypted archive and yield the ``tarfile`` to add members to.
    The file is complete (final chunk written) when the ``with`` block exits cleanly."""
    salt = os.urandom(16)
    nonce_prefix = os.urandom(8)
    kdf = {"name": "scrypt", "n": SCRYPT_N, "r": 8, "p": 1}
    header = json.dumps({"format": 1, "kdf": kdf, "salt": base64.b64encode(salt).decode(),
                         "nonce": base64.b64encode(nonce_prefix).decode(),
                         "created": created or datetime.now(UTC).isoformat(timespec="seconds")},
                        separators=(",", ":")).encode()
    key = _derive(passphrase, salt, kdf["n"], kdf["r"], kdf["p"])
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(MAGIC)
        fh.write(len(header).to_bytes(2, "big"))
        fh.write(header)
        writer = _EncWriter(fh, key, hashlib.sha256(header).digest(), nonce_prefix)
        with tarfile.open(fileobj=writer, mode="w|gz") as tf:
            yield tf
        writer.close()


def create_archive(passphrase: str, *, include_media: bool = False, dest_dir: Path | None = None) -> dict:
    """Write one encrypted archive and return its listing row. Blocking: run it in a thread."""
    if not passphrase or len(passphrase) < 8:
        raise BackupError("backup_passphrase_required", "A backup passphrase of at least 8 characters is required.")
    data = Path(config.DATA_DIR)
    dest = Path(dest_dir) if dest_dir else archive_dir()
    dest.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(dest, 0o700)
    stamp = _stamp()
    out = dest / f"dispatch-{stamp}{EXT}"
    n = 2
    while out.exists():
        out = dest / f"dispatch-{stamp}-{n}{EXT}"
        n += 1
    tmp = out.with_suffix(out.suffix + ".part")
    with tempfile.TemporaryDirectory(prefix="dispatch-bk-", dir=str(dest)) as td:
        work = Path(td)
        files: list[tuple[Path, str]] = []
        dbp = data / "chats.db"
        if dbp.is_file():
            snap = work / "chats.db"
            _sqlite_snapshot(dbp, snap)
            files.append((snap, "chats.db"))
        for name in DATA_FILES:
            p = data / name
            if p.is_file():
                files.append((p, name))
        for d in DATA_DIRS + (MEDIA_DIRS if include_media else ()):
            files += _walk(data, d)
        sec = _secrets_document()
        if sec:
            sp = work / "secrets.json"
            sp.write_text(json.dumps(sec), encoding="utf-8")
            files.append((sp, "secrets.json"))
        manifest = {
            "format": 1, "app_version": __version__, "created": datetime.now(UTC).isoformat(timespec="seconds"),
            "include_media": bool(include_media),
            "files": [{"path": rel, "size": p.stat().st_size,
                       "sha256": _sha256(p) if rel in ("chats.db", "secrets.json") or p.stat().st_size < 64 * 1024 ** 2 else None}
                      for p, rel in files],
        }
        try:
            with encrypted_tar(tmp, passphrase, created=manifest["created"]) as tf:
                raw = json.dumps(manifest, indent=1).encode()
                ti = tarfile.TarInfo("MANIFEST.json")
                ti.size = len(raw)
                ti.mtime = int(datetime.now(UTC).timestamp())
                tf.addfile(ti, io.BytesIO(raw))
                for p, rel in files:
                    ti = tf.gettarinfo(str(p), arcname=rel)
                    ti.uid = ti.gid = 0
                    ti.uname = ti.gname = ""
                    ti.mode = 0o600
                    with open(p, "rb") as src:
                        tf.addfile(ti, src)
            os.replace(tmp, out)
        except BaseException:
            with contextlib.suppress(OSError):
                tmp.unlink()
            raise
    with contextlib.suppress(OSError):
        os.chmod(out, 0o600)
    log.info("encrypted backup written: %s (%d bytes, %d files)", out.name, out.stat().st_size, len(files))
    return _archive_row(out)


def prune_archives(keep: int) -> int:
    d = archive_dir()
    if not d.is_dir():
        return 0
    arcs = sorted((p for p in d.iterdir() if p.is_file() and p.suffix == EXT), key=lambda p: p.name)
    gone = arcs[:-keep] if keep > 0 else []
    for p in gone:
        with contextlib.suppress(OSError):
            p.unlink()
    return len(gone)


# --------------------------------------------------------------------------- #
# Snapshot tiers (4.0 scale and retention, BUILD-PLAN-v2 1.13.3)
# --------------------------------------------------------------------------- #
#
# The rotating ``chats-<stamp>.db`` snapshots are written every 6 hours and trimmed by
# ``main._prune_backups`` to the newest N. At 1-2 GB each that is too much disk for too little
# history, so the daily maintenance task replaces "keep 12" with tiers:
#
#   recent   the newest ``keep_recent`` snapshots (default 8 = two days at 6-hourly)
#   daily    the last snapshot of each of the newest ``keep_daily`` days (default 7)
#   weekly   the last snapshot of each of the newest ``keep_weekly`` ISO weeks older than the
#            oldest kept day (default 4)
#
# never fewer than ``MIN_SNAPSHOTS`` in total. A daily or weekly keeper is hard-linked into
# ``backups/tiers/{daily,weekly}/`` (same bytes, no extra disk), which is what protects it from
# ``main._prune_backups``'s newest-N rule; the root copy of everything that is not "recent" is then
# dropped. ``list_backups`` and ``resolve_id`` see the tier folders, so a daily or weekly keeper can
# be restored like any snapshot.

MIN_SNAPSHOTS = 4
TIER_DIRNAME = "tiers"
TIERS = ("daily", "weekly")
_SNAP_STAMP = re.compile(r"^chats-(\d{8})-(\d{6})\.db$")


def tier_dir(tier: str) -> Path:
    return backup_root() / TIER_DIRNAME / tier


def _snap_time(name: str) -> datetime | None:
    m = _SNAP_STAMP.match(name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")  # noqa: DTZ007  (the rotation stamps local time)
    except ValueError:
        return None


def plan_snapshot_tiers(names: list[str], *, keep_recent: int = 8, keep_daily: int = 7,
                        keep_weekly: int = 4, min_total: int = MIN_SNAPSHOTS) -> dict[str, list[str]]:
    """Which snapshot names each tier keeps. Pure: ``names`` are ``chats-YYYYMMDD-HHMMSS.db``
    (anything else is ignored). Returns ``{"recent", "daily", "weekly", "keep", "drop"}``; the
    lists are newest first."""
    stamped = sorted(((t, n) for n in names if (t := _snap_time(n)) is not None), reverse=True)
    ordered = [n for _t, n in stamped]
    recent = ordered[:max(0, keep_recent)]
    by_day: dict[str, str] = {}
    for t, n in stamped:                      # newest first: the first seen of a day is its last
        by_day.setdefault(t.strftime("%Y-%m-%d"), n)
    days = sorted(by_day, reverse=True)[:max(0, keep_daily)]
    daily = [by_day[d] for d in days]
    oldest_day = days[-1] if days else None
    by_week: dict[str, str] = {}
    for t, n in stamped:
        if oldest_day is not None and t.strftime("%Y-%m-%d") >= oldest_day:
            continue
        iso = t.isocalendar()
        by_week.setdefault(f"{iso[0]}-{iso[1]:02d}", n)
    weeks = sorted(by_week, reverse=True)[:max(0, keep_weekly)]
    weekly = [by_week[w] for w in weeks]
    keep = list(dict.fromkeys(recent + daily + weekly))
    if len(keep) < min_total:
        for n in ordered:
            if n not in keep:
                keep.append(n)
            if len(keep) >= min_total:
                break
    keep_set = set(keep)
    return {"recent": recent, "daily": daily, "weekly": weekly,
            "keep": [n for n in ordered if n in keep_set],
            "drop": [n for n in ordered if n not in keep_set]}


def _link_or_copy(src: Path, dst: Path) -> None:
    if dst.exists():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".part")
    with contextlib.suppress(OSError):
        tmp.unlink()
    try:
        os.link(src, tmp)
    except OSError:
        shutil.copy2(src, tmp)
    os.replace(tmp, dst)


def _usable_snapshot(p: Path) -> bool:
    try:
        if p.stat().st_size <= 0:
            return False
        with open(p, "rb") as fh:
            return fh.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


def prune_snapshot_tiers(*, keep_recent: int = 8, keep_daily: int = 7, keep_weekly: int = 4,
                         dry_run: bool = False) -> dict[str, Any]:
    """Apply :func:`plan_snapshot_tiers` to ``backups/``: link the daily and weekly keepers into
    ``backups/tiers/``, drop the root copies that are no longer "recent" and every tier file the
    plan no longer keeps. Only the rotation's own stamped files are ever touched. A snapshot that is
    empty or not a database never counts as a keeper (a failed ``VACUUM INTO`` leaves one)."""
    root = backup_root()
    out: dict[str, Any] = {"removed": 0, "bytes": 0, "kept": 0, "plan": {}}
    if not root.is_dir():
        return out
    names: dict[str, Path] = {}
    for tier_name in ("", *TIERS):
        d = root if not tier_name else tier_dir(tier_name)
        if not d.is_dir():
            continue
        for p in d.iterdir():
            if p.is_file() and _SNAP_RE.match(p.name) and p.name not in names and _usable_snapshot(p):
                names[p.name] = p
    plan = plan_snapshot_tiers(list(names), keep_recent=keep_recent, keep_daily=keep_daily,
                               keep_weekly=keep_weekly)
    out["plan"] = plan
    out["kept"] = len(plan["keep"])
    if dry_run:
        out["removed"] = len(plan["drop"])
        out["bytes"] = sum(names[n].stat().st_size for n in plan["drop"] if n in names)
        return out
    want = {"daily": set(plan["daily"]) - set(plan["recent"]),
            "weekly": (set(plan["weekly"]) | set()) - set(plan["recent"]) - set(plan["daily"])}
    # link first (a keeper must exist in its tier before any root copy goes)
    for tier, keepers in want.items():
        for n in keepers:
            _link_or_copy(names[n], tier_dir(tier) / n)
    recent = set(plan["recent"])
    drop = set(plan["drop"])

    def _unlink(p: Path) -> None:
        try:
            st = p.stat()
            p.unlink()
        except OSError:
            return
        if st.st_nlink <= 1:                       # the last link: the bytes are really freed
            out["bytes"] += st.st_size

    # a tier file the plan no longer keeps in that tier (never the only copy of a kept snapshot)
    for tier in TIERS:
        d = tier_dir(tier)
        if not d.is_dir():
            continue
        for p in d.iterdir():
            if not (p.is_file() and _SNAP_RE.match(p.name)) or p.name in want[tier]:
                continue
            if p.name in drop or (p.name in recent and (root / p.name).exists()):
                _unlink(p)
    # a root file that is not "recent": its tier link exists, or the plan drops it
    for p in root.iterdir():
        if not (p.is_file() and _SNAP_RE.match(p.name)) or p.name in recent:
            continue
        if p.name in drop or any((tier_dir(t) / p.name).exists() for t in TIERS):
            _unlink(p)
    out["removed"] = sum(1 for n in drop
                         if not (root / n).exists() and not any((tier_dir(t) / n).exists() for t in TIERS))
    return out


def prune_premigration(keep: int = 3, *, dry_run: bool = False) -> dict[str, int]:
    """Keep the newest ``keep`` ``<version>-<stamp>/`` pre-migration copies (``migrate.KEEP_BACKUPS``
    is 3; at the size a year-old database reaches, three is enough)."""
    root = backup_root()
    rows = {"removed": 0, "bytes": 0}
    if not root.is_dir():
        return rows
    dirs = sorted((p for p in root.iterdir() if p.is_dir() and _PRE_RE.match(p.name)),
                  key=lambda p: p.stat().st_mtime)
    for p in dirs[:-keep] if keep > 0 else dirs:
        size = sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
        rows["bytes"] += size
        rows["removed"] += 1
        if not dry_run:
            shutil.rmtree(p, ignore_errors=True)
    return rows


#: Pre-restore copies kept (4.3, #36). The newest is never removed, whatever ``keep`` says.
KEEP_PRERESTORE = 3


def prune_prerestore(keep: int = KEEP_PRERESTORE, *, dry_run: bool = False) -> dict[str, int]:
    """Keep the newest ``keep`` ``pre-restore-<stamp>/`` copies (at least one: the newest is the
    way back from the last restore). Run by the daily maintenance task, never by a restore, so a
    restore never deletes anything while it makes its own copy."""
    root = backup_root()
    rows = {"removed": 0, "bytes": 0}
    if not root.is_dir():
        return rows
    dirs = sorted((p for p in root.iterdir() if p.is_dir() and _PRERESTORE_RE.match(p.name)),
                  key=lambda p: p.name)          # the stamp sorts by time
    for p in dirs[:-max(1, keep)]:
        rows["bytes"] += sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
        rows["removed"] += 1
        if not dry_run:
            shutil.rmtree(p, ignore_errors=True)
    return rows


def backup_in_progress() -> bool:
    """True while a backup is being written: an unfinished ``.part`` archive or work folder, or a
    snapshot file touched in the last two minutes that is not yet a database."""
    root = backup_root()
    now = datetime.now(UTC).timestamp()
    try:
        for d in (root, archive_dir()):
            if not d.is_dir():
                continue
            for p in d.iterdir():
                name = p.name
                if name.endswith(".part") or name.startswith("dispatch-bk-"):
                    if now - p.stat().st_mtime < 6 * 3600:
                        return True
                elif p.is_file() and _SNAP_RE.match(name) and now - p.stat().st_mtime < 120:
                    if not _usable_snapshot(p):
                        return True
    except OSError:
        return False
    return False


# --------------------------------------------------------------------------- #
# Reading an archive
# --------------------------------------------------------------------------- #


def _safe_member(name: str) -> str | None:
    """The normalised relative path of a tar member, or None when it must not be extracted."""
    if not name or name.startswith(("/", "\\")) or "\x00" in name or re.match(r"^[A-Za-z]:", name):
        return None
    parts = [p for p in name.replace("\\", "/").split("/") if p not in ("", ".")]
    if not parts or ".." in parts or parts[0] not in ALLOWED_TOP:
        return None
    return "/".join(parts)


def read_archive(path: Path, passphrase: str, *, extract_to: Path | None = None) -> dict:
    """Decrypt and verify ``path``. Returns the manifest plus ``verified`` (every checksum matched).
    With ``extract_to``, regular files are written there (path-checked; links and devices refused)."""
    fh, reader, hdr = _open_decrypt(path, passphrase)
    manifest: dict = {}
    seen: dict[str, str] = {}
    try:
        with tarfile.open(fileobj=io.BufferedReader(reader), mode="r|gz") as tf:
            for m in tf:
                rel = _safe_member(m.name)
                if rel is None:
                    raise BackupError("backup_corrupt", f"The backup contains an unsafe path ({m.name[:40]!r}).")
                if not m.isreg():
                    if m.isdir():
                        continue
                    raise BackupError("backup_corrupt", "The backup contains something other than files.")
                src = tf.extractfile(m)
                assert src is not None
                if rel == "MANIFEST.json":
                    manifest = json.loads(src.read().decode("utf-8"))
                    continue
                h = hashlib.sha256()
                target = None
                if extract_to is not None:
                    target = Path(extract_to) / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                with contextlib.ExitStack() as st:
                    out = st.enter_context(os.fdopen(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb"))                         if target is not None else None
                    for blk in iter(lambda s=src: s.read(1 << 20), b""):
                        h.update(blk)
                        if out is not None:
                            out.write(blk)
                seen[rel] = h.hexdigest()
    except BackupError:
        raise
    except (tarfile.TarError, EOFError, OSError, ValueError) as e:
        raise BackupError("backup_corrupt", f"The backup could not be read ({type(e).__name__}).") from None
    finally:
        fh.close()
    if not manifest:
        raise BackupError("backup_corrupt", "The backup has no manifest.")
    mismatched = [f["path"] for f in manifest.get("files", [])
                  if f.get("sha256") and seen.get(f["path"]) != f["sha256"]]
    missing = [f["path"] for f in manifest.get("files", []) if f["path"] not in seen]
    if mismatched or missing:
        raise BackupError("backup_corrupt", "The backup failed its checksums: "
                          + ", ".join((mismatched + missing)[:3]))
    manifest["verified"] = True
    return manifest


# --------------------------------------------------------------------------- #
# Listing
# --------------------------------------------------------------------------- #

_SNAP_RE = re.compile(r"^chats-\d{8}-\d{6}\.db$")
#: ``<version>-YYYYMMDD-HHMMSS``. The version is whatever wrote the copy: app/migrate.py's
#: ``3.0.0``, or the container entrypoint's previous image version (``3.0.0-b1``, ``dev``,
#: ``unrecorded``), so anything up to the stamp is accepted - except ``pre-restore-``, which is
#: a restore's own copy (4.3, #36; the same exclusion is in ``migrate._BACKUP_DIR_RE``).
_PRE_RE = re.compile(r"^(?!pre-restore-)[0-9A-Za-z][0-9A-Za-z.+-]*-\d{8}-\d{6}$")
_PRERESTORE_RE = re.compile(r"^pre-restore-\d{8}-\d{6}(?:-\d+)?$")


def _archive_row(p: Path) -> dict:
    st = p.stat()
    return {"id": f"archive:{p.name}", "kind": "archive", "name": p.name, "size": st.st_size,
            "created": datetime.fromtimestamp(st.st_mtime, UTC).isoformat(timespec="seconds"),
            "encrypted": True, "restorable": True}


def list_backups() -> list[dict]:
    rows: list[dict] = []
    d = archive_dir()
    if d.is_dir():
        rows += [_archive_row(p) for p in d.iterdir() if p.is_file() and p.suffix == EXT]
    root = backup_root()
    if root.is_dir():
        for p in root.iterdir():
            st = p.stat()
            created = datetime.fromtimestamp(st.st_mtime, UTC).isoformat(timespec="seconds")
            if p.is_file() and _SNAP_RE.match(p.name):
                rows.append({"id": f"snapshot:{p.name}", "kind": "snapshot", "name": p.name,
                             "size": st.st_size, "created": created, "encrypted": False, "restorable": True,
                             "tier": "recent"})
            elif p.is_dir() and _PRE_RE.match(p.name):
                size = sum(f.stat().st_size for f in p.iterdir() if f.is_file())
                rows.append({"id": f"premigration:{p.name}", "kind": "premigration", "name": p.name,
                             "size": size, "created": created, "encrypted": False, "restorable": True})
            elif p.is_dir() and _PRERESTORE_RE.match(p.name):
                size = sum(f.stat().st_size for f in p.iterdir() if f.is_file())
                rows.append({"id": f"prerestore:{p.name}", "kind": "prerestore", "name": p.name,
                             "size": size, "created": created, "encrypted": False, "restorable": True})
        seen = {r["name"] for r in rows if r["kind"] == "snapshot"}
        for tier in TIERS:
            d = tier_dir(tier)
            if not d.is_dir():
                continue
            for p in d.iterdir():
                if p.is_file() and _SNAP_RE.match(p.name) and p.name not in seen:
                    st = p.stat()
                    seen.add(p.name)
                    rows.append({"id": f"snapshot:{p.name}", "kind": "snapshot", "name": p.name,
                                 "size": st.st_size, "encrypted": False, "restorable": True, "tier": tier,
                                 "created": datetime.fromtimestamp(st.st_mtime, UTC).isoformat(timespec="seconds")})
    rows.sort(key=lambda r: r["created"], reverse=True)
    return rows


def resolve_id(backup_id: str) -> tuple[str, Path]:
    kind, _, name = (backup_id or "").partition(":")
    if (kind not in ("archive", "snapshot", "premigration", "prerestore") or not name or "/" in name
            or "\\" in name or ".." in name):
        raise BackupError("backup_not_found", "Unknown backup", 404)
    base = {"archive": archive_dir(), "snapshot": backup_root(), "premigration": backup_root(),
            "prerestore": backup_root()}[kind]
    p = base / name
    if kind == "snapshot" and not p.exists():       # a daily or weekly keeper lives in backups/tiers/
        for tier in TIERS:
            if (tier_dir(tier) / name).is_file():
                p = tier_dir(tier) / name
                break
    ok = {"archive": p.is_file() and p.suffix == EXT,
          "snapshot": p.is_file() and bool(_SNAP_RE.match(name)),
          "premigration": p.is_dir() and bool(_PRE_RE.match(name)),
          "prerestore": p.is_dir() and bool(_PRERESTORE_RE.match(name))}[kind]
    if not ok:
        raise BackupError("backup_not_found", "Unknown backup", 404)
    return kind, p


# --------------------------------------------------------------------------- #
# Restore: stage now, apply at the next start
# --------------------------------------------------------------------------- #


def pending_path() -> Path:
    return Path(config.DATA_DIR) / PENDING


def stage_restore(backup_id: str, passphrase: str | None = None) -> dict:
    kind, p = resolve_id(backup_id)
    data = Path(config.DATA_DIR)
    stage = data / "restore-staging" / _stamp()
    if stage.exists():
        shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True)
    make_private(stage.parent, directory=True)      # decrypted secrets wait here until the next start
    make_private(stage, directory=True)
    try:
        if kind == "archive":
            pw = passphrase or configured_passphrase()
            if not pw:
                raise BackupError("backup_passphrase_required",
                                  "Enter the backup's passphrase (or recovery key).")
            manifest = read_archive(p, pw, extract_to=stage)
            what = [f["path"] for f in manifest.get("files", [])]
        elif kind == "snapshot":
            shutil.copy2(p, stage / "chats.db")
            what = ["chats.db"]
        else:
            what = []
            for f in p.iterdir():
                if f.is_file() and f.name in ALLOWED_TOP and f.name != "secrets.json":
                    shutil.copy2(f, stage / f.name)
                    what.append(f.name)
            # A pre-migration / pre-restore copy holds the secret file itself ({name: value}, the
            # archive's own format): its names go back through the stores like an archive's.
            sec = p / "agents" / "secrets.json"
            if sec.is_file():
                shutil.copy2(sec, stage / "secrets.json")
                make_private(stage / "secrets.json")
                what.append("agents/secrets.json")
        if kind != "archive":
            _quick_check(stage / "chats.db") if (stage / "chats.db").exists() else None
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    marker = {"staging": str(stage), "source": backup_id, "staged_at": datetime.now(UTC).isoformat(timespec="seconds"),
              "files": what}
    pending_path().write_text(json.dumps(marker), encoding="utf-8")
    return {"staged": True, "restart_required": True, "source": backup_id, "files": what,
            "re_enter": _re_enter_preview(stage)}


def _staged_secret_names(stage: Path) -> set[str]:
    try:
        doc = json.loads((stage / "secrets.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return {str(k) for k, v in doc.items() if v} if isinstance(doc, dict) else set()


def _re_enter_preview(stage: Path) -> list[str]:
    """What the restore will not be able to put back: secrets the restored data refers to that
    neither the backup nor this install holds. (Only a restored ``chats.db`` changes what is
    referred to; a backup without one leaves the references as they are.)"""
    with contextlib.suppress(Exception):
        refs = _referenced_names(stage / "chats.db")
        return _missing(refs - _staged_secret_names(stage))
    return []


LAST_RESTORE = ".last-restore.json"


def last_restore() -> dict | None:
    """The last applied restore (``applied_at``, ``source``, ``previous``) and the secrets that
    still need entering again (``re_enter``, re-checked now: a name drops off once it is set)."""
    try:
        doc = json.loads((Path(config.DATA_DIR) / LAST_RESTORE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict):
        return None
    doc["re_enter"] = _missing([str(n) for n in doc.get("re_enter") or []])
    return doc


def _quick_check(db: Path) -> None:
    try:
        c = sqlite3.connect(str(db))
        try:
            r = c.execute("PRAGMA quick_check").fetchone()
        finally:
            c.close()
    except sqlite3.Error:
        r = None
    if not r or str(r[0]).lower() != "ok":
        raise BackupError("backup_corrupt", "The database inside the backup failed its integrity check.")


def cancel_pending() -> bool:
    mp = pending_path()
    if not mp.exists():
        return False
    with contextlib.suppress(Exception):
        st = json.loads(mp.read_text(encoding="utf-8")).get("staging")
        if st and Path(st).is_dir():
            shutil.rmtree(st, ignore_errors=True)
    mp.unlink(missing_ok=True)
    return True


def apply_pending_restore() -> dict | None:
    """Apply a staged restore (if any). Called before the database is opened at start-up, and by the
    offline CLI. Returns what was done, or None when nothing was staged."""
    mp = pending_path()
    if not mp.exists():
        return None
    data = Path(config.DATA_DIR)
    try:
        marker = json.loads(mp.read_text(encoding="utf-8"))
        stage = Path(marker["staging"])
        if not stage.is_dir() or data not in stage.parents:
            raise ValueError("the staging folder is missing")
    except (OSError, ValueError, KeyError) as e:
        log.error("a restore was staged but cannot be applied (%s); leaving it for you to look at", e)
        return {"applied": False, "error": str(e)}
    pre = backup_root() / f"pre-restore-{_stamp()}"
    # The copy of what is about to be replaced is the only way back from a wrong restore, so a
    # failure here (disk full, an unreadable database) STOPS the restore: the staged files and the
    # marker stay, nothing is overwritten, and the next start tries again. It used to be
    # suppressed, and the restore then replaced the live chats.db with no copy left anywhere.
    try:
        pre.mkdir(parents=True, exist_ok=True)
        make_private(pre, directory=True)
        cur = data / "chats.db"
        if cur.is_file():
            _sqlite_snapshot(cur, pre / "chats.db")
            make_private(pre / "chats.db")
        for name in DATA_FILES:
            p = data / name
            if p.is_file():
                shutil.copy2(p, pre / name)
                make_private(pre / name)
        sec_now = _secrets_file()
        if sec_now.is_file():                     # the keys the restore is about to overwrite
            (pre / "agents").mkdir(exist_ok=True)
            make_private(pre / "agents", directory=True)
            shutil.copy2(sec_now, pre / "agents" / "secrets.json")
            make_private(pre / "agents" / "secrets.json")
    except (OSError, sqlite3.Error) as e:
        log.error("a restore is staged but the copy of the current data to %s failed (%s); NOT restoring. "
                  "Free some space or fix the error and restart, or cancel it with DELETE /api/backups/pending",
                  pre, e)
        return {"applied": False, "error": f"could not save the current data first: {e}"}
    applied: list[str] = []
    for src in sorted(p for p in stage.rglob("*") if p.is_file()):
        rel = src.relative_to(stage).as_posix()
        if rel in ("MANIFEST.json", "secrets.json"):
            continue
        dst = data / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if rel == "chats.db":
            for suffix in ("-wal", "-shm"):
                with contextlib.suppress(OSError):
                    Path(str(dst) + suffix).unlink()
        tmp = dst.with_name(dst.name + ".restore-new")
        shutil.copy2(src, tmp)
        if "/" not in rel:
            make_private(tmp)           # before it is published: no moment when it is world-readable
        os.replace(tmp, dst)
        applied.append(rel)
    failed = _put_back_secrets(stage / "secrets.json")
    harden_restored(data, applied)
    shutil.rmtree(stage, ignore_errors=True)
    mp.unlink(missing_ok=True)
    with contextlib.suppress(Exception):
        config._invalidate_bots_cache()
        from . import accounts, connections, settings_store, tool_manifests
        accounts.reset_for_tests()
        settings_store.reset_schema_cache()
        connections.reset_schema_cache()
        tool_manifests.reset_schema_cache()
        config.invalidate_stored_env()
    re_enter = sorted(set(failed) | set(_missing(_referenced_names(data / "chats.db"))))
    with contextlib.suppress(OSError):
        doc = {"applied_at": datetime.now(UTC).isoformat(timespec="seconds"),
               "source": str(marker.get("source") or ""), "previous": pre.name, "re_enter": re_enter}
        (data / LAST_RESTORE).write_text(json.dumps(doc), encoding="utf-8")
        make_private(data / LAST_RESTORE)
    if re_enter:
        log.warning("restore applied; re-enter these secrets (the backup could not put them back): %s",
                    ", ".join(re_enter))
    log.warning("restore applied (%d files); the previous state is in %s", len(applied), pre)
    return {"applied": True, "files": applied, "previous": str(pre), "re_enter": re_enter}


def _put_back_secrets(sec: Path) -> list[str]:
    """Write every name of a backup's ``secrets.json`` into the store that owns it. Returns the
    names that did not stick (a locked keyring, a read-only store): the person re-enters them."""
    if not sec.is_file():
        return []
    try:
        doc = json.loads(sec.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.error("the backup's secrets could not be read; re-enter the keys")
        return ["secrets.json"]
    if not isinstance(doc, dict):
        return []
    failed: list[str] = []
    agents = None
    for name, value in doc.items():
        name, value = str(name), str(value)
        if not value or name in _NOT_ARCHIVED:
            continue
        try:
            if _is_agent_name(name):
                agents = agents or _agent_store()
                agents.set(name, value)
                if agents.get(name) != value:
                    raise RuntimeError("the agents' secret store did not keep the value")
                from .agents.secrets import GLOBAL_REDACTOR
                GLOBAL_REDACTOR.register(value)
            else:
                secret_store.put(name, value)
        except Exception as e:
            log.error("could not restore the secret %s (%s); re-enter it", name, type(e).__name__)
            failed.append(name)
    return failed


# --------------------------------------------------------------------------- #
# Schedule
# --------------------------------------------------------------------------- #


def last_archive_age_hours() -> float | None:
    rows = [r for r in list_backups() if r["kind"] == "archive"]
    if not rows:
        return None
    newest = max(rows, key=lambda r: r["created"])
    return (datetime.now(UTC) - datetime.fromisoformat(newest["created"])).total_seconds() / 3600


def run_scheduled_once() -> dict | None:
    """Make an encrypted archive if the schedule says one is due. Returns the row or None."""
    from . import settings_store
    if not settings_store.get("system.encrypted_backup_enabled", False):
        return None
    age = last_archive_age_hours()
    interval = float(settings_store.get("system.encrypted_backup_interval_hours", 24))
    if age is not None and age < interval:
        return None
    pw = configured_passphrase()
    if not pw:
        pw = recovery_key()
        store_generated_key(pw)
        log.warning("no backup passphrase was set, so a recovery key was generated and saved in this install's secret "
                    "store as the backup passphrase. It is not shown anywhere unasked: the main Admin can show it "
                    "once in Settings > System > Backups after re-entering the PIN or password, or set your own "
                    "passphrase there to be able to read these archives on another computer")
    row = create_archive(pw, include_media=bool(settings_store.get("system.encrypted_backup_include_media", False)))
    prune_archives(int(settings_store.get("system.encrypted_backup_keep", 7)))
    return row


async def scheduled_loop() -> None:
    """Started by the app at boot. Checks every ten minutes; never raises."""
    try:
        await asyncio.sleep(180)
        while True:
            try:
                await asyncio.to_thread(run_scheduled_once)
            except Exception:
                log.exception("scheduled backup failed; will retry")
            await asyncio.sleep(600)
    except asyncio.CancelledError:
        pass


# --------------------------------------------------------------------------- #
# Support bundle
# --------------------------------------------------------------------------- #

_REDACT_PATTERNS = [
    (re.compile(r"sk-[A-Za-z0-9_\-]{8,}"), "***"),
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=\-]{8,}"), r"\1 ***"),
    (re.compile(r"(?i)\b(api[_-]?key|token|secret|password|passwd|pin|passphrase|cookie|authorization|session|recovery)"
                r"(\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;&}]+)"), r"\1\2***"),
    (re.compile(r"(?i)\b(lc_session|dispatch_device|set-cookie)\s*[=:]\s*[^\s;]+"), r"\1=***"),
    (re.compile(r"\b[A-Fa-f0-9]{32,}\b"), "***"),
    (re.compile(r"\b[A-Za-z0-9_\-]{40,}\b"), "***"),
    (re.compile(r"[A-Z2-9]{5}(?:-[A-Z2-9]{5}){4}"), "***"),
]


def redact_text(text: str) -> str:
    """Scrub secrets from free text: key shapes, bearer tokens, ``name=value`` for credential-looking
    names, long hex/base64 runs, recovery keys, and every secret value this process has registered."""
    from .agents.secrets import GLOBAL_REDACTOR
    out = GLOBAL_REDACTOR.redact(text or "")
    for rx, repl in _REDACT_PATTERNS:
        out = rx.sub(repl, out)
    return out


_SAFE_KEYS = frozenset({"secret", "pinned", "is_pinned", "set", "persona_kind", "persona_custom"})
_KEYISH = re.compile(r"(key|token|secret|password|passwd|pin|hash|salt|passphrase|cookie|credential|persona|prompt|system_prompt)", re.I)


def redact_obj(obj: Any, *, include_prompts: bool = False) -> Any:
    """Redact a JSON-able structure: values under credential-looking keys are replaced wholesale,
    every string is scrubbed, and (unless ``include_prompts``) prompt/persona text is dropped."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            ks = str(k)
            if (_KEYISH.search(ks) and not ks.endswith(("_set", "_len", "_env", "_kind"))
                    and not ks.startswith("has_") and ks not in _SAFE_KEYS):
                if re.search(r"persona|prompt", ks, re.I):
                    if include_prompts:
                        out[ks] = redact_obj(v, include_prompts=True)
                    else:
                        out[ks] = "(omitted - prompts are excluded by default)" if v else v
                else:
                    out[ks] = "***" if v else v
            else:
                out[ks] = redact_obj(v, include_prompts=include_prompts)
        return out
    if isinstance(obj, list):
        return [redact_obj(v, include_prompts=include_prompts) for v in obj]
    if isinstance(obj, str):
        return redact_text(obj)
    return obj


def _tail(path: Path, lines: int = 2000) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 512 * 1024))
            data = f.read().decode("utf-8", "replace")
    except OSError:
        return ""
    return "\n".join(data.splitlines()[-lines:])


def _ring_tail(lines: int) -> str:
    """The in-memory log ring (``app/logbuffer.py``, 4.3 #18) when there is no log file: DisPatch
    logs to stderr / the journal, so the file is usually absent. Empty when the ring is absent."""
    try:
        from . import logbuffer  # type: ignore[attr-defined]
    except ImportError:
        return ""
    try:
        try:
            got = logbuffer.tail(lines)
        except TypeError:
            got = logbuffer.tail()
    except Exception:
        log.exception("could not read the in-memory log for the support bundle")
        return ""
    if isinstance(got, str):
        text = got
    else:
        text = "\n".join(str(x) for x in (got or []))
    return "\n".join(text.splitlines()[-lines:])


def build_support_bundle(*, include_prompts: bool = False, log_lines: int = 2000) -> bytes:
    """A zip of what a maintainer needs to help - versions, settings (secrets redacted), connection
    and character summaries (no keys, no prompts unless asked), health, the tail of the log (scrubbed).
    Never contains conversations, files, media or credentials."""
    import platform

    from . import accounts, connections, settings_store
    now = datetime.now(UTC).isoformat(timespec="seconds")
    docs: dict[str, Any] = {}
    docs["about.json"] = {"app_version": __version__, "created": now, "python": sys.version.split()[0],
                          "platform": platform.platform(),
                          "include_prompts": include_prompts,
                          "note": "No conversations, uploads, keys, PINs, passwords or tokens are included."}
    docs["settings.json"] = settings_store.values_document()
    docs["connections.json"] = [
        {k: v for k, v in c.public(connections.used_by(c.id)).items() if k != "credential"}
        | {"credential_kind": c.credential_kind(), "has_key": bool(connections.credential(c))}
        for c in connections.list_all()]
    from . import characters
    chars = []
    for b in config.load_bots():
        chars.append({"id": b.id, "name": b.name, "scope": b.scope,
                      "hidden": b.hidden,
                      "backend": characters.backend_kind(b),
                      "connection": characters.connection_of(b) or None,
                      "model": characters.model_of(b), "reactions": b.reactions, "pack": b.pack,
                      "persona": personas_text(b) if include_prompts else "(omitted)"})
    docs["characters.json"] = chars
    docs["profiles.json"] = [{"id": p.id, "role": p.role, "disabled": p.disabled, "has_pin": p.has_pin,
                              "has_password": p.has_password} for p in accounts.list_profiles()]
    try:
        docs["backups.json"] = [{k: r[k] for k in ("kind", "name", "size", "created", "encrypted")}
                                for r in list_backups()]
    except Exception:
        docs["backups.json"] = []
    logp = Path(config.env("LOG_FILE", "") or (Path(config.LOG_DIR) / "dispatch.log"))
    log_text = redact_text(_tail(logp, log_lines) or _ring_tail(log_lines))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, doc in docs.items():
            z.writestr(name, json.dumps(redact_obj(doc, include_prompts=include_prompts), indent=1,
                                        ensure_ascii=False, default=str))
        z.writestr("log-tail.txt", log_text or "(no log file)")
        z.writestr("README.txt", "DisPatch support bundle. Review it before you share it. "
                                 "Secrets were scrubbed automatically, but you know your data best.\n")
    return buf.getvalue()


def personas_text(b) -> str:
    from . import personas
    return personas.current(b)[0]


# --------------------------------------------------------------------------- #
# CLI (offline restore)
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.backups", description="DisPatch backups (server stopped).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pc = sub.add_parser("create", help="write an encrypted archive")
    pc.add_argument("--passphrase-file", required=True)
    pc.add_argument("--include-media", action="store_true")
    pr = sub.add_parser("restore", help="restore an archive into the data directory")
    pr.add_argument("file")
    pr.add_argument("--passphrase-file", required=True)
    sub.add_parser("list", help="list backups")
    ap.add_argument("--data-dir")
    args = ap.parse_args(argv)
    if args.data_dir:
        from . import migrate
        migrate._repoint(Path(args.data_dir))
        config.BACKUP_DIR = Path(args.data_dir) / "backups"
    if args.cmd == "list":
        for r in list_backups():
            print(f"{r['id']}\t{r['size']}\t{r['created']}")
        return 0
    pw = Path(args.passphrase_file).read_text(encoding="utf-8").strip()
    if args.cmd == "create":
        print(create_archive(pw, include_media=args.include_media)["name"])
        return 0
    p = Path(args.file)
    stage = Path(config.DATA_DIR) / "restore-staging" / _stamp()
    stage.mkdir(parents=True)
    read_archive(p, pw, extract_to=stage)
    pending_path().write_text(json.dumps({"staging": str(stage), "source": p.name, "files": []}), encoding="utf-8")
    print(json.dumps(apply_pending_restore(), indent=1))
    return 0


if __name__ == "__main__":      # pragma: no cover
    sys.exit(main())
