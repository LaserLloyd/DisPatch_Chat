"""Backups: scheduled, encrypted, restorable, and the support bundle.

Three kinds of backup sit side by side under ``<data>/backups/`` and are listed together:

``archive``        ``archives/dispatch-<stamp>.dpbk`` - everything you would need on a new computer:
                   chats.db (a consistent SQLite snapshot), the settings and roster files,
                   avatars, the reaction pack, the connection API keys (from the secret store) and,
                   if asked, the media and File Server folders. **Encrypted** (see below).
``snapshot``       ``chats-<stamp>.db`` - the rotating online database copies ``main._make_backup``
                   has always written (plain SQLite, every 6 hours by default).
``premigration``   ``<version>-<stamp>/`` - what ``migrate.py`` copies before it changes anything.

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

from . import __version__, config, secret_store

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
DATA_DIRS = ("avatars", "reactions/pack", "voices")
MEDIA_DIRS = ("media", "files")
#: top-level names a restore may write; anything else in an archive is refused.
ALLOWED_TOP = frozenset({"chats.db", "MANIFEST.json", "secrets.json", *DATA_FILES,
                         "avatars", "reactions", "voices", "media", "files"})


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


def configured_passphrase() -> str | None:
    return secret_store.get("setting.system.backup_passphrase")


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


def _secrets_document() -> dict[str, str]:
    """Connection API keys (and secret settings) by reference, for the archive. Read from the secret
    store so a restore on another computer has them; encrypted like everything else."""
    out: dict[str, str] = {}
    try:
        from . import connections, settings_schema
        for c in connections.list_all():
            if c.credential_ref.startswith("secret:"):
                v = secret_store.get(c.credential_ref[7:])
                if v:
                    out[c.credential_ref[7:]] = v
        for d in settings_schema.all_defs():
            if d.secret and d.key != "system.backup_passphrase":
                v = secret_store.get(f"setting.{d.key}")
                if v:
                    out[f"setting.{d.key}"] = v
    except Exception:
        log.exception("could not collect secrets for the backup")
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
                    out = st.enter_context(open(target, "wb")) if target is not None else None
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
#: ``unrecorded``), so anything up to the stamp is accepted.
_PRE_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+-]*-\d{8}-\d{6}$")


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
                             "size": st.st_size, "created": created, "encrypted": False, "restorable": True})
            elif p.is_dir() and _PRE_RE.match(p.name):
                size = sum(f.stat().st_size for f in p.iterdir() if f.is_file())
                rows.append({"id": f"premigration:{p.name}", "kind": "premigration", "name": p.name,
                             "size": size, "created": created, "encrypted": False, "restorable": True})
    rows.sort(key=lambda r: r["created"], reverse=True)
    return rows


def resolve_id(backup_id: str) -> tuple[str, Path]:
    kind, _, name = (backup_id or "").partition(":")
    if kind not in ("archive", "snapshot", "premigration") or not name or "/" in name or "\\" in name or ".." in name:
        raise BackupError("backup_not_found", "Unknown backup", 404)
    base = {"archive": archive_dir(), "snapshot": backup_root(), "premigration": backup_root()}[kind]
    p = base / name
    ok = {"archive": p.is_file() and p.suffix == EXT,
          "snapshot": p.is_file() and bool(_SNAP_RE.match(name)),
          "premigration": p.is_dir() and bool(_PRE_RE.match(name))}[kind]
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
                if f.is_file() and f.name in ALLOWED_TOP:
                    shutil.copy2(f, stage / f.name)
                    what.append(f.name)
        if kind != "archive":
            _quick_check(stage / "chats.db") if (stage / "chats.db").exists() else None
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    marker = {"staging": str(stage), "source": backup_id, "staged_at": datetime.now(UTC).isoformat(timespec="seconds"),
              "files": what}
    pending_path().write_text(json.dumps(marker), encoding="utf-8")
    return {"staged": True, "restart_required": True, "source": backup_id, "files": what}


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
    pre.mkdir(parents=True, exist_ok=True)
    cur = data / "chats.db"
    if cur.is_file():
        with contextlib.suppress(Exception):
            _sqlite_snapshot(cur, pre / "chats.db")
    for name in DATA_FILES:
        p = data / name
        if p.is_file():
            with contextlib.suppress(OSError):
                shutil.copy2(p, pre / name)
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
        os.replace(tmp, dst)
        applied.append(rel)
    sec = stage / "secrets.json"
    if sec.is_file():
        try:
            for ref, value in json.loads(sec.read_text(encoding="utf-8")).items():
                secret_store.put(str(ref), str(value))
        except Exception:
            log.exception("restoring secrets failed; re-enter the connection keys")
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
    log.warning("restore applied (%d files); the previous state is in %s", len(applied), pre)
    return {"applied": True, "files": applied, "previous": str(pre)}


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
        secret_store.put("setting.system.backup_passphrase", pw)
        log.warning("a backup recovery key was generated and stored; read it in Settings > System > Backups")
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
        chars.append({"id": b.id, "name": b.name, "audience": b.audience,
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
    log_text = redact_text(_tail(logp, log_lines))
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
