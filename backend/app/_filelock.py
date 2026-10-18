"""Cross-platform advisory file locks (POSIX ``fcntl`` / Windows ``msvcrt``).

``import fcntl`` made the whole backend un-importable on native Windows. The
two places that need a lock (the single-instance claim in ``main.py`` and the
dsh settings rewrite in ``harness.py``) go through this module instead.

Semantics are deliberately the narrowest common denominator:

* ``try_lock_exclusive`` is non-blocking and raises ``OSError`` with
  ``errno.EACCES`` / ``errno.EAGAIN`` when another *process* holds the lock.
* The lock is released when the file object is closed or the process dies.
* On POSIX it is a ``lockf`` record lock (owned by the process, so several app
  instances in ONE interpreter - the test suite - may share a data dir). On
  Windows ``LockFileEx`` byte-range locks are owned by the handle, so a
  per-process registry reproduces the same "one process per data dir" rule.
"""

from __future__ import annotations

import errno
import os
import sys
import threading
from typing import IO, Any

IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:  # pragma: no cover - exercised on Windows only
    import msvcrt
else:
    import fcntl

# A byte far past any real data so reads of the pid text stay possible.
_WIN_LOCK_OFFSET = 0x7FFFFFF0

_held: dict[str, int] = {}     # Windows: resolved path -> owning pid
_held_guard = threading.Lock()


def _contended(exc: OSError) -> OSError:
    """Normalise "someone else holds it" to ``errno.EACCES``."""
    if exc.errno in (errno.EACCES, errno.EAGAIN):
        return exc
    # msvcrt reports contention as EACCES (13) or EDEADLOCK (36).
    if IS_WINDOWS and exc.errno in (errno.EDEADLK, 36):
        return OSError(errno.EACCES, "lock is held by another process")
    return exc


def try_lock_exclusive(fh: IO[Any], key: str | os.PathLike[str] | None = None) -> None:
    """Take a non-blocking exclusive lock on ``fh``. Raises ``OSError`` if contended."""
    if not IS_WINDOWS:
        fcntl.lockf(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return
    # pragma: no cover - Windows branch
    k = os.path.realpath(os.fspath(key if key is not None else fh.name))
    with _held_guard:
        if _held.get(k) == os.getpid():
            return                       # this process already owns it (same rule as lockf)
        try:
            fh.seek(_WIN_LOCK_OFFSET)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as e:
            raise _contended(e) from e
        finally:
            fh.seek(0)
        _held[k] = os.getpid()


def lock_exclusive(fd: int) -> None:
    """Blocking exclusive lock on a raw descriptor (released by :func:`unlock`)."""
    if not IS_WINDOWS:
        fcntl.flock(fd, fcntl.LOCK_EX)
        return
    # pragma: no cover - Windows branch
    os.lseek(fd, _WIN_LOCK_OFFSET, os.SEEK_SET)
    try:
        msvcrt.locking(fd, msvcrt.LK_LOCK, 1)   # retries ~10 s, then raises
    finally:
        os.lseek(fd, 0, os.SEEK_SET)


def unlock(fd: int) -> None:
    """Release a lock taken with :func:`lock_exclusive`."""
    if not IS_WINDOWS:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return
    # pragma: no cover - Windows branch
    os.lseek(fd, _WIN_LOCK_OFFSET, os.SEEK_SET)
    try:
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    finally:
        os.lseek(fd, 0, os.SEEK_SET)


def current_uid() -> int | None:
    """The effective uid, or ``None`` where the concept does not exist (Windows)."""
    getuid = getattr(os, "getuid", None)
    return getuid() if getuid else None
