"""Locked-out recovery: the pieces that only someone with access to the machine can reach.

Two doors, both closed to the network on purpose:

* ``python -m app owner reset`` / ``python -m app users passwd <owner>`` (``app/cli.py``) run in
  a terminal on the host, or inside the container with ``docker compose exec app ...``.
* A **sentinel file** or an **environment variable**, read exactly once while the server starts
  (:func:`apply_startup_reset`, called from the lifespan and from nowhere else). No request
  handler reads either of them, no setting can set them (the variable is read from the process
  environment only, never through :func:`app.config.env`, which would also consult the settings
  store that an admin can write over the network), and the sentinel is deleted before it is acted
  on, so it cannot replay.

The sentinel is ``<data dir>/RESET_LOGIN``. Its first line is one of:

=====================  ==============================================================
(empty)                Clear the owner's password and PIN. The install is unclaimed
                       again: a new one-time setup token is logged and written to
                       ``SETUP-TOKEN.txt``, and the owner sets a new password in the
                       setup wizard.
``name``               The same, after checking ``name`` is the owner's name.
``name:newpassword``   Set the owner's password right now (the owner is created when
                       there is none). Everything after the FIRST colon is the password.
=====================  ==============================================================

``DISPATCH_RESET_OWNER=<anything but 0/false/off/empty>`` does the first row once per value: the
value is remembered in ``<data dir>/.reset-owner-applied``, so leaving it in a ``.env`` file does
not wipe the owner on every restart. Use a different value to use it again. The environment is
no place for a password, so it never sets one.

What is reset: the owner's password, PIN and recovery code, the owner's sessions and remembered
devices. What is kept: every chat, every other profile (and their PINs), the roster, settings,
connections and the owner's profile row itself (its id, name, picture, history).
"""

from __future__ import annotations

import codecs
import contextlib
import hashlib
import logging
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

from . import accounts, auth, config, credentials
from .contracts.permissions import Role

log = logging.getLogger("local-chat.recovery")

SENTINEL_NAME = "RESET_LOGIN"
#: Windows editors append ``.txt`` without asking; it is accepted too.
SENTINEL_NAMES = (SENTINEL_NAME, SENTINEL_NAME + ".txt")
ENV_NAME = "DISPATCH_RESET_OWNER"
APPLIED_NAME = ".reset-owner-applied"
#: The sentinel is a line, not a document; anything bigger is not ours.
SENTINEL_MAX_BYTES = 1024
_OFF = frozenset({"", "0", "false", "no", "off"})


class RecoveryError(Exception):
    """A recovery action that was refused; the message is safe to show."""


@dataclass
class OwnerReset:
    owner_name: str | None       # None: there was no owner row to clear
    had_password: bool
    had_pin: bool
    setup_token: str


def sentinel_path() -> Path:
    return Path(config.DATA_DIR) / SENTINEL_NAME


def sentinel_paths() -> list[Path]:
    return [Path(config.DATA_DIR) / n for n in SENTINEL_NAMES]


def applied_path() -> Path:
    return Path(config.DATA_DIR) / APPLIED_NAME


# --------------------------------------------------------------------------- #
# The two actions
# --------------------------------------------------------------------------- #


def reset_owner(*, via: str) -> OwnerReset:
    """Clear the owner's credentials and hand back a fresh setup token.

    The owner row stays (its id keeps the audit history and device bindings); the install is
    unclaimed until the setup wizard gives it a password or PIN again."""
    owner = auth.ensure_owner_for_pin()
    cfg = auth.load()
    had_pin = cfg.pin_set
    had_password = bool(owner and owner.has_password)
    if owner is not None:
        accounts.update_profile(owner.id, password_hash=None, pin_hash=None)
    if cfg.pin_set:
        auth.clear_pin()                 # security.yaml's PIN, the recovery code, the owner's sessions
    auth._revoke_owner_sessions()        # also sessions minted before profiles existed
    token = auth.write_setup_token_file()
    accounts.log_activity("owner_reset", profile_id=owner.id if owner else None,
                          subject=via, category="audit")
    return OwnerReset(owner.name if owner else None, had_password, had_pin, token)


def set_owner_password(name: str, password: str, *, via: str) -> tuple[accounts.Profile, bool]:
    """Set the owner's password, creating the owner when there is none.

    ``name`` has to be the owner's name when one exists. Returns ``(profile, created)``."""
    err = credentials.check_password(password)
    if err:
        raise RecoveryError(err)
    owner = auth.ensure_owner_for_pin()
    created = False
    try:
        if owner is None:
            owner = accounts.create_profile(name, Role.ADMIN, protected=True,
                                            password_hash=credentials.hash_secret(password))
            created = True
        else:
            if owner.name.casefold() != name.strip().casefold():
                raise RecoveryError(
                    f"{SENTINEL_NAME} names {name!r} but this install's owner is {owner.name!r}. "
                    f"Put {owner.name}:<new password> in the file.")
            auth.set_profile_password(owner.id, password)
    except accounts.AccountError as e:
        raise RecoveryError(str(e)) from e
    auth.burn_setup_token()              # the install is claimed; a pending token is void
    auth._revoke_owner_sessions()
    owner = accounts.get_owner() or owner
    accounts.log_activity("owner_password_reset", profile_id=owner.id, subject=via, category="audit")
    return owner, created


# --------------------------------------------------------------------------- #
# Start-up hook
# --------------------------------------------------------------------------- #


def _discard(path: Path, why: str) -> None:
    log.warning("login recovery: %s %s; deleted without being used", path.name, why)
    with contextlib.suppress(OSError):
        path.unlink()


def _read_sentinel(path: Path) -> str | None:
    """The sentinel's first line after the file has been shredded and removed, or None when
    it must not be acted on (and has been dealt with).

    Everything is decided on ONE descriptor (opened without following a symlink) so the file
    that was checked is the file that was read; the name is only used to delete it."""
    try:
        st = path.lstat()
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode):
        log.warning("login recovery: %s is not a plain file; ignored (remove it)", path.name)
        return None
    flags = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
    writable = True
    try:
        fd = os.open(path, os.O_RDWR | flags)
    except OSError:
        writable = False
        try:
            fd = os.open(path, os.O_RDONLY | flags)
        except OSError as e:
            log.warning("login recovery: cannot open %s (%s); left in place", path.name, e)
            return None
    refuse: str | None = None
    raw = b""
    try:
        fst = os.fstat(fd)
        if not stat.S_ISREG(fst.st_mode):
            log.warning("login recovery: %s is not a plain file; ignored (remove it)", path.name)
            return None
        if sys.platform != "win32" and (fst.st_mode & stat.S_IWOTH or fst.st_uid not in (0, os.geteuid())):
            refuse = "is world-writable or owned by another user"
        elif sys.platform != "win32" and fst.st_nlink != 1:
            refuse = "has more than one hard link"
        elif fst.st_size > SENTINEL_MAX_BYTES:
            refuse = f"is larger than {SENTINEL_MAX_BYTES} bytes"
        else:
            raw = os.read(fd, SENTINEL_MAX_BYTES + 1)
            if len(raw) > SENTINEL_MAX_BYTES:
                refuse = f"is larger than {SENTINEL_MAX_BYTES} bytes"
            elif writable:
                # Consume FIRST. If the file cannot be removed (a read-only mount) it is not acted
                # on: a sentinel that survives would replay on every start.
                with contextlib.suppress(OSError):
                    os.lseek(fd, 0, os.SEEK_SET)
                    os.write(fd, bytes(len(raw)))
                    os.fsync(fd)
    finally:
        os.close(fd)                     # closed before any delete: Windows cannot unlink an open file
    if refuse is not None:
        _discard(path, refuse)
        return None
    try:
        path.unlink()
    except OSError as e:
        log.warning("login recovery: cannot delete %s (%s); NOT acting on it", path.name, e)
        return None
    try:
        # Windows PowerShell 5 writes UTF-16 with a BOM for `>` and Out-File; Notepad may add a
        # UTF-8 BOM. Accept all three.
        text = raw.decode("utf-16") if raw[:2] in (codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE) \
            else raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        log.warning("login recovery: %s is not UTF-8 text; deleted without being used", path.name)
        return None
    return text.split("\n", 1)[0].rstrip("\r")


def _apply_sentinel() -> bool:
    """Act on ``<data>/RESET_LOGIN`` if there is one. True when a sentinel was present."""
    found = [p for p in sentinel_paths() if p.exists() or p.is_symlink()]
    for path in found[1:]:
        # Two files are two answers to "what should happen"; act on the first, delete the rest.
        _discard(path, f"was found next to {found[0].name}")
    if found:
        _apply_one(found[0])
    return bool(found)


def _apply_one(path: Path) -> None:
    line = _read_sentinel(path)
    if line is None:
        return
    name, colon, password = line.partition(":")
    name = name.strip()
    password = password.rstrip()         # an editor's trailing space or tab is not part of it
    via = f"{SENTINEL_NAME} file"
    try:
        if colon and password:
            if not name:
                raise RecoveryError(f"{SENTINEL_NAME} has a password but no name before the colon")
            owner, created = set_owner_password(name, password, via=via)
            log.warning("login recovery: %s consumed; %s owner %r with the password from the file",
                        SENTINEL_NAME, "created" if created else "reset the password of", owner.name)
            return
        current = auth.ensure_owner_for_pin()
        if name and current is not None and current.name.casefold() != name.casefold():
            raise RecoveryError(f"{SENTINEL_NAME} names {name!r} but this install's owner is "
                                f"{current.name!r}; nothing was reset")
        res = reset_owner(via=via)
        log.warning("login recovery: %s consumed; owner %s sign-in cleared, setup is open again "
                    "(token in the banner below and in %s)", SENTINEL_NAME,
                    repr(res.owner_name) if res.owner_name else "(none existed)", auth.SETUP_TOKEN_PATH.name)
    except RecoveryError as e:
        log.warning("login recovery: %s consumed but NOT applied: %s", SENTINEL_NAME, e)
    except Exception:
        log.exception("login recovery: %s consumed but failed", SENTINEL_NAME)


def _env_fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _apply_env() -> None:
    value = os.environ.get(ENV_NAME, "").strip()
    if value.lower() in _OFF:
        return
    # The value is a "run it again" nonce, not a secret, but people do type odd things into
    # environment files: it is never logged, and the marker keeps only its hash.
    fingerprint = _env_fingerprint(value)
    marker = applied_path()
    with contextlib.suppress(OSError):
        if marker.read_text(encoding="utf-8").strip() == fingerprint:
            log.warning("login recovery: %s was already applied on an earlier start and is "
                        "ignored; remove it from the environment (a different value runs it again)",
                        ENV_NAME)
            return
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(fingerprint + "\n", encoding="utf-8")
    except OSError as e:
        log.warning("login recovery: cannot record %s in %s (%s); NOT acting on it",
                    ENV_NAME, marker.name, e)
        return
    try:
        res = reset_owner(via=f"{ENV_NAME} variable")
        log.warning("login recovery: %s applied; owner %s sign-in cleared, setup is open again "
                    "(token in the banner below and in %s). Remove %s from the environment.",
                    ENV_NAME, repr(res.owner_name) if res.owner_name else "(none existed)",
                    auth.SETUP_TOKEN_PATH.name, ENV_NAME)
    except Exception:
        log.exception("login recovery: %s failed", ENV_NAME)


def apply_startup_reset() -> None:
    """Run the sentinel / environment recovery. Called ONCE, from the server's start-up, after
    migrations and before the "no owner yet" banner. Never raises: a broken recovery file must
    not keep the chat down."""
    try:
        if not _apply_sentinel():        # a sentinel wins; the variable waits for the next start
            _apply_env()
    except Exception:
        log.exception("login recovery failed")
    finally:
        with contextlib.suppress(Exception):
            auth.discard_stale_setup_token()
