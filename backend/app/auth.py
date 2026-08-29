"""PIN lock, "Safe Mode" (the default), and session management.

The app is single-user on a trusted network. Once a PIN is set, the DEFAULT
state is a stripped-down **Safe Mode**: anyone can view it with no PIN, but it is
limited to the safe-flagged bots and chat media is withheld at the server (only
those bots' own avatars are shown). Entering the PIN (from Settings) issues a
full-access session cookie; that session auto-expires after a window of
inactivity (default 10 min), dropping back to Safe Mode. The browser arms the
same idle timer, and a "Lock" button returns to Safe Mode on demand.

So there is no blocking gate: a request that holds a valid full session gets
everything; any other request (no/expired session) is served Safe Mode.

Optionally ("remember_device_days" > 0 in security.yaml), the unlock screen
offers to REMEMBER the device: the session then survives restarts and never
idle-locks, for a sliding window of that many days. Only the SHA-256 of each
remembered token is persisted (``trusted-devices.yaml``); the token itself
lives solely in that device's cookie. Locking a remembered device forgets it,
and any PIN change forgets all of them. Deleting trusted-devices.yaml (or
setting the days to 0) is the recovery/kill switch.

Everything lives in one human-editable YAML file (``security.yaml``) in the data
dir, which is a recovery path: set ``pin:`` to reset, set it to ``""`` to remove
the lock, or delete the file. A findable RECOVERY-CODE.txt is the other path.

No third-party crypto: PBKDF2-HMAC-SHA256 from the stdlib, constant-time compare
via :func:`hmac.compare_digest`.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import logging
import os
import secrets
import tempfile
import threading
import time
from dataclasses import dataclass

import yaml

from .config import DATA_DIR, ensure_dirs

log = logging.getLogger("local-chat.auth")

SECURITY_PATH = DATA_DIR / "security.yaml"
RECOVERY_PATH = DATA_DIR / "RECOVERY-CODE.txt"
TRUSTED_PATH = DATA_DIR / "trusted-devices.yaml"

# Cost factor for the PIN KDF. 200,000 is the shipped floor and the only value
# a real deployment ever uses.
#
# ENV-ONLY override, deliberately. The test suite calls set_pin() 53 times
# (414 hashes after parametrisation) at ~47ms each — 19.4 seconds, 44% of the
# suite's remaining runtime, spent proving nothing except that PBKDF2 is slow,
# which is its job. Making it configurable turns that into ~0.
#
# It is read from the environment and NOT from security.yaml on purpose: a
# config file the app itself writes must never be able to weaken the KDF that
# protects it. Two tests assert the shipped default is still >= 200,000 so
# nobody can quietly lower it (see tests/test_auth_gate.py).
PBKDF2_ITERATIONS = int(os.environ.get("DISPATCH_PBKDF2_ITERATIONS", "200000"))
DEFAULT_LOCK_TIMEOUT = 600          # seconds of inactivity before auto-lock
MIN_PIN_LENGTH = 4

# Remembered ("keep this device unlocked") devices.
DEFAULT_REMEMBER_DAYS = 30          # what the UI toggle enables
_MAX_TRUSTED = 10                   # oldest evicted beyond this
_TRUSTED_TOUCH_MIN = 3600.0         # persist last_seen at most this often (s)

# Recovery code charset: unambiguous (no O/0, I/1/L) so it's easy to read off
# the file and type back in. ~30^8 ≈ 6.5e11 combinations.
_RECOVERY_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"

# Brute-force throttle: once there have been this many consecutive failures,
# each further attempt is refused for a cooldown that grows with the count.
_FAIL_LOCKOUT_AFTER = 5
_FAIL_BASE_COOLDOWN = 5.0           # seconds (doubles per extra failure, capped)

# Reentrant: load() may call revoke_all() while already holding the lock.
_lock = threading.RLock()

# token -> _Session. In memory only: a server restart logs everyone out, which
# is the safe default for a lock.
_sessions: dict[str, _Session] = {}
_fail_count = 0
_fail_until = 0.0

_cache: tuple[float, SecurityConfig] | None = None

# sha256(token) hex -> {"created": epoch, "last_seen": epoch, "ua": str}.
# Lazy-loaded from TRUSTED_PATH; None = not loaded yet.
_trusted: dict[str, dict] | None = None


_TEMPLATE = """\
# DisPatch Chat — security & lock settings.
#
# RESET / RECOVERY (no restart needed; picked up on the next request):
#   • Forgot the PIN or locked out?  set   pin: "1357"   -> becomes the new PIN
#   • Remove the lock entirely?      set   pin: ""        (empty quotes)
#   • Or simply delete this file     -> removes the lock entirely
#
# The PIN is NEVER stored in plaintext — only the salted PBKDF2 hash below.
# Quote numeric PINs (pin: "0042") so leading zeros are preserved.

# Plaintext PIN to (re)set. Normally null. If you type a value here it is hashed
# on the next load and this line is blanked back to null automatically.
pin: {pin}

# --- managed by the app; readable, but avoid hand-editing these ---
pin_hash: {pin_hash}
salt: {salt}
iterations: {iterations}
recovery_hash: {recovery_hash}
recovery_salt: {recovery_salt}

# Auto-lock back to Safe Mode after this many seconds with no activity.
lock_timeout_seconds: {lock_timeout}

# "Keep this device unlocked": 0 disables the offer. When > 0, the unlock
# screen offers to remember the device for this many days (sliding — renewed
# by use). Remembered devices skip the PIN across restarts and never
# idle-lock. Only hashed tokens are stored, in trusted-devices.yaml next to
# this file — delete that file (or set this to 0) to forget every device.
# Changing the PIN also forgets them all.
remember_device_days: {remember_days}

# API key for OpenClaw's inbound endpoints (/api/inject, /api/daily,
# POST /api/threads/<id>/messages). Loopback (on-box agents/crons) is always
# exempt. Remote (LAN/tailnet) callers must send  X-API-Key: <value>; with
# api_token null, remote calls to these endpoints are REFUSED outright.
api_token: {api_token}
"""


@dataclass
class SecurityConfig:
    pin_hash: str | None = None
    salt: str | None = None
    iterations: int = PBKDF2_ITERATIONS
    lock_timeout: int = DEFAULT_LOCK_TIMEOUT
    api_token: str | None = None
    recovery_hash: str | None = None
    recovery_salt: str | None = None
    remember_days: int = 0

    @property
    def pin_set(self) -> bool:
        return bool(self.pin_hash and self.salt)


@dataclass
class _Session:
    """A full-access session. Its mere existence (and not being expired) is what
    grants full access; Safe Mode is simply the absence of one.

    ``persistent`` sessions belong to a remembered device: they are exempt from
    the idle timeout and are validated against the trusted-device store (which
    also lets them be re-minted from the cookie after a restart)."""
    token: str
    last_seen: float        # time.monotonic()
    persistent: bool = False


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def _clean(v) -> str | None:
    """Normalise a YAML scalar to a non-empty string or None."""
    if v is None:
        return None
    s = str(v).strip()
    if s == "" or s.lower() == "null":
        return None
    return s


def _pos_int(v, default: int) -> int:
    try:
        n = int(v)
        return n if n > 0 else default
    except (TypeError, ValueError):
        return default


def _nonneg_int(v, default: int) -> int:
    try:
        n = int(v)
        return n if n >= 0 else default
    except (TypeError, ValueError):
        return default


def _y(v) -> str:
    """Render a Python scalar as a YAML value for the template."""
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    s = str(v).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def _hash_pin(pin: str, salt_hex: str, iterations: int) -> str:
    dk = hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), bytes.fromhex(salt_hex), iterations)
    return dk.hex()


def _write(cfg: SecurityConfig, plaintext_pin: str | None = None) -> None:
    ensure_dirs()
    text = _TEMPLATE.format(
        pin=_y(plaintext_pin),
        pin_hash=_y(cfg.pin_hash),
        salt=_y(cfg.salt),
        iterations=cfg.iterations,
        recovery_hash=_y(cfg.recovery_hash),
        recovery_salt=_y(cfg.recovery_salt),
        lock_timeout=cfg.lock_timeout,
        remember_days=cfg.remember_days,
        api_token=_y(cfg.api_token),
    )
    # Atomic + 0600: the file holds the
    # api_token and PIN hash, and a crash mid-write (or a concurrent reader
    # during a PIN change) must never observe a truncated/partial file — an
    # unparseable security.yaml now fails CLOSED (see load()).
    SECURITY_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(SECURITY_PATH.parent), prefix=".security.yaml.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, SECURITY_PATH)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def _gen_recovery_code() -> str:
    raw = "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(8))
    return f"LC-{raw[:4]}-{raw[4:]}"


def _norm_code(code: str) -> str:
    """Normalise a recovery code for comparison: alnum only, upper-cased."""
    return "".join(ch for ch in str(code).upper() if ch.isalnum())


def _ensure_recovery(cfg: SecurityConfig, rotate: bool) -> None:
    """Make sure a recovery code exists, writing it to a findable text file.

    Called whenever a PIN is set. The plaintext code lives only in
    RECOVERY-CODE.txt (and the startup log) — the config stores just its hash.
    """
    if not (rotate or not cfg.recovery_hash):
        return
    code = _gen_recovery_code()
    salt = secrets.token_hex(16)
    cfg.recovery_salt = salt
    cfg.recovery_hash = _hash_pin(_norm_code(code), salt, cfg.iterations)
    try:
        ensure_dirs()
        RECOVERY_PATH.write_text(
            "DisPatch Chat — PIN RECOVERY CODE\n"
            "=================================\n\n"
            "Forgot your PIN? On the lock screen tap \"Forgot PIN?\" and enter:\n\n"
            f"    {code}\n\n"
            "That resets the lock so you can set a new PIN (or run without one).\n"
            "Keep this file private — anyone with this code can reset the PIN.\n"
            "It is regenerated each time you change the PIN.\n"
        )
        # Owner-only: the code bypasses the PIN, so keep it off any group/other
        # read (write_text honours umask, which is not guaranteed to be 0077).
        os.chmod(RECOVERY_PATH, 0o600)
    except OSError as e:
        log.warning("could not write recovery file %s: %s", RECOVERY_PATH, e)
    # Never log the code itself — journald persists across reboots and is swept
    # into log/support bundles. Point the operator at the 0600 file instead.
    log.warning("PIN recovery code regenerated → %s", RECOVERY_PATH)


def _drop_recovery(cfg: SecurityConfig) -> None:
    cfg.recovery_hash = None
    cfg.recovery_salt = None
    with contextlib.suppress(OSError):
        RECOVERY_PATH.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# Config load / persistence
# --------------------------------------------------------------------------- #

# Sentinel used when security.yaml EXISTS but cannot be read/parsed. It never
# equals a hex PBKDF2 digest, so pin_set is True but no PIN (and no recovery
# code) can ever verify — the app stays in Safe Mode until the file is fixed.
_LOCKDOWN_HASH = "!unreadable-security-yaml!"


def _lockdown_config() -> SecurityConfig:
    """FAIL-CLOSED config: locked, with no way to mint a full session."""
    return SecurityConfig(pin_hash=_LOCKDOWN_HASH, salt="00" * 16)


def load() -> SecurityConfig:
    """Read security.yaml (cached by mtime), applying any plaintext `pin:` set.

    No file means "no lock" — and we do NOT create one until a PIN is actually
    set, keeping the app zero-config until the user opts in.
    """
    global _cache
    with _lock:
        if not SECURITY_PATH.exists():
            _cache = None
            return SecurityConfig()

        try:
            mtime = SECURITY_PATH.stat().st_mtime
        except OSError:
            mtime = 0.0
        if _cache and _cache[0] == mtime:
            return _cache[1]

        try:
            loaded = yaml.safe_load(SECURITY_PATH.read_text())
            if not isinstance(loaded, dict):
                # An empty/scalar file is just as suspect as invalid YAML (a
                # truncated write, a botched hand-edit): fail closed the same
                # way. Removing the lock is done by DELETING the file.
                raise yaml.YAMLError(f"expected a mapping, got {type(loaded).__name__}")
            raw: dict = loaded
        except (yaml.YAMLError, OSError) as e:
            # FAIL CLOSED. The file exists, so a lock was (very likely)
            # configured — treating a corrupt/unreadable file as "no lock"
            # would silently serve everything unlocked on a LAN-reachable
            # port. Stay locked with no unlockable PIN until it's fixed.
            log.error("security.yaml exists but cannot be read/parsed (%s) — "
                      "FAILING CLOSED: app stays locked in Safe Mode. Fix or "
                      "delete %s to recover.", e, SECURITY_PATH)
            # Fail closed means fail closed for sessions ALREADY MINTED too.
            # Lockdown made it impossible to unlock from here on, but every
            # full session handed out before the file went bad kept working —
            # so the state entered because the security config can no longer be
            # trusted still had unlocked devices walking around in it. The
            # plaintext-PIN path above has always cleared both; this one now
            # matches it.
            _sessions.clear()
            _wipe_trusted()
            cfg = _lockdown_config()
            _cache = (mtime, cfg)
            return cfg

        cfg = SecurityConfig(
            pin_hash=_clean(raw.get("pin_hash")),
            salt=_clean(raw.get("salt")),
            iterations=_pos_int(raw.get("iterations"), PBKDF2_ITERATIONS),
            lock_timeout=_pos_int(raw.get("lock_timeout_seconds"), DEFAULT_LOCK_TIMEOUT),
            api_token=_clean(raw.get("api_token")),
            recovery_hash=_clean(raw.get("recovery_hash")),
            recovery_salt=_clean(raw.get("recovery_salt")),
            remember_days=_nonneg_int(raw.get("remember_device_days"), 0),
        )

        # Apply a plaintext `pin:` recovery/set directive, then blank it out.
        plain = raw.get("pin")
        dirty = False
        if plain is not None:
            plain = str(plain)
            if plain == "":
                cfg.pin_hash = None          # explicit reset: remove the lock
                cfg.salt = None
                _drop_recovery(cfg)
                log.info("security.yaml: PIN cleared via plaintext reset")
            else:
                salt = secrets.token_hex(16)
                cfg.pin_hash = _hash_pin(plain, salt, cfg.iterations)
                cfg.salt = salt
                _ensure_recovery(cfg, rotate=True)
                log.info("security.yaml: PIN (re)set via plaintext directive")
            _sessions.clear()                # any change to the PIN logs out all
            _wipe_trusted()                  # …and forgets every remembered device
            dirty = True
        # Backfill a recovery code for a PIN set before this feature existed.
        elif cfg.pin_set and not cfg.recovery_hash:
            _ensure_recovery(cfg, rotate=True)
            dirty = True

        if dirty:
            _write(cfg, plaintext_pin=None)
            try:
                mtime = SECURITY_PATH.stat().st_mtime
            except OSError:
                pass

        _cache = (mtime, cfg)
        return cfg


def _bust_cache() -> None:
    global _cache
    _cache = None


def is_pin_set() -> bool:
    return load().pin_set


def lock_timeout() -> int:
    return load().lock_timeout


def set_pin(new_pin: str) -> None:
    """Set/replace the real PIN, rotate the recovery code, log out all sessions."""
    with _lock:
        cfg = load()
        salt = secrets.token_hex(16)
        cfg.pin_hash = _hash_pin(new_pin, salt, cfg.iterations)
        cfg.salt = salt
        _ensure_recovery(cfg, rotate=True)
        _write(cfg)
        _bust_cache()
        _sessions.clear()
        _wipe_trusted()


def clear_pin() -> None:
    """Remove the PIN entirely (lock disabled) and drop the recovery code."""
    with _lock:
        cfg = load()
        cfg.pin_hash = None
        cfg.salt = None
        _drop_recovery(cfg)
        _write(cfg)
        _bust_cache()
        _sessions.clear()
        _wipe_trusted()


def verify_recovery(code: str) -> bool:
    cfg = load()
    if not (cfg.recovery_hash and cfg.recovery_salt):
        return False
    cand = _hash_pin(_norm_code(code), cfg.recovery_salt, cfg.iterations)
    return hmac.compare_digest(cand, cfg.recovery_hash)


def verify_pin(pin: str) -> bool:
    """True if `pin` matches the configured PIN (constant-time)."""
    cfg = load()
    if not cfg.pin_set or not (cfg.salt and cfg.pin_hash):
        return False
    cand = _hash_pin(pin, cfg.salt, cfg.iterations)
    return hmac.compare_digest(cand, cfg.pin_hash)


def verify_api_token(token: str) -> bool:
    cfg = load()
    if not (cfg.api_token and token):
        return False
    try:
        return hmac.compare_digest(token, cfg.api_token)
    except TypeError:        # non-ASCII header value
        return False


# --------------------------------------------------------------------------- #
# Trusted ("remembered") devices
# --------------------------------------------------------------------------- #


def _token_hash(token: str) -> str:
    # Plain SHA-256 (no KDF needed): the token is 256 bits of urandom, so the
    # hash only has to stop a leaked trusted-devices.yaml from minting sessions.
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _trusted_store() -> dict[str, dict]:
    """Lazy-load the trusted-device store. A corrupt/unreadable file FAILS
    CLOSED the convenient way: no device is trusted, PIN entry still works."""
    global _trusted
    with _lock:
        if _trusted is not None:
            return _trusted
        _trusted = {}
        if TRUSTED_PATH.exists():
            try:
                raw = yaml.safe_load(TRUSTED_PATH.read_text())
                if isinstance(raw, dict) and isinstance(raw.get("devices"), dict):
                    for h, meta in raw["devices"].items():
                        if isinstance(h, str) and isinstance(meta, dict):
                            _trusted[h] = {
                                "created": float(meta.get("created") or 0.0),
                                "last_seen": float(meta.get("last_seen") or 0.0),
                                "ua": str(meta.get("ua") or "")[:160],
                            }
                else:
                    log.error("trusted-devices.yaml is not the expected mapping "
                              "— ignoring it (devices must re-enter the PIN)")
            except (yaml.YAMLError, OSError, TypeError, ValueError) as e:
                log.error("trusted-devices.yaml unreadable (%s) — ignoring it "
                          "(devices must re-enter the PIN)", e)
        return _trusted


def _save_trusted() -> None:
    """Atomic 0600 write, mirroring security.yaml's discipline."""
    store = _trusted_store()
    text = (
        "# DisPatch Chat — remembered (\"keep this device unlocked\") devices.\n"
        "# Only SHA-256 hashes of the device tokens live here — the tokens\n"
        "# themselves exist only in each device's cookie. DELETE THIS FILE to\n"
        "# forget every remembered device at once (picked up immediately).\n"
        + yaml.safe_dump({"devices": store}, sort_keys=True)
    )
    TRUSTED_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(TRUSTED_PATH.parent), prefix=".trusted-devices.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, TRUSTED_PATH)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def _wipe_trusted() -> None:
    global _trusted
    with _lock:
        _trusted = {}
        with contextlib.suppress(OSError):
            TRUSTED_PATH.unlink(missing_ok=True)


def _trusted_entry(token: str, days: int) -> dict | None:
    """The live store entry for `token`, or None (expired ones are dropped)."""
    store = _trusted_store()
    e = store.get(_token_hash(token))
    if e is None:
        return None
    if (time.time() - e["last_seen"]) > days * 86400:
        store.pop(_token_hash(token), None)
        with contextlib.suppress(OSError):
            _save_trusted()
        return None
    return e


def trusted_count() -> int:
    with _lock:
        return len(_trusted_store())


def remember_days() -> int:
    return load().remember_days


def set_remember_days(days: int) -> None:
    """Persist the feature toggle. Turning it OFF also forgets every device —
    the natural reading of "stop keeping devices unlocked"."""
    days = max(0, min(int(days), 365))
    with _lock:
        cfg = load()
        cfg.remember_days = days
        _write(cfg)
        _bust_cache()
        if days == 0:
            _wipe_trusted()


def forget_all_trusted(keep_session_token: str | None = None) -> None:
    """Forget every remembered device. Other devices' persistent sessions die
    outright (that is the point — think "my phone went missing"); the CALLER's
    session (keep_session_token) survives but is demoted to a normal
    idle-expiring one, so forgetting doesn't lock the device you're holding."""
    with _lock:
        _wipe_trusted()
        for t in [t for t, s in _sessions.items() if s.persistent]:
            if t == keep_session_token:
                _sessions[t].persistent = False
                _sessions[t].last_seen = time.monotonic()
            else:
                _sessions.pop(t, None)


# --------------------------------------------------------------------------- #
# Sessions
# --------------------------------------------------------------------------- #


def issue_session(persistent: bool = False, user_agent: str = "") -> str:
    """Issue a full-access session token.

    With ``persistent=True`` (and the feature enabled) the device is also
    remembered: its token hash is persisted so the session survives restarts
    and skips the idle timeout, for `remember_days` sliding days."""
    token = secrets.token_urlsafe(32)
    with _lock:
        persistent = persistent and load().remember_days > 0   # defensive gate
        _sessions[token] = _Session(token=token, last_seen=time.monotonic(),
                                    persistent=persistent)
        if persistent:
            store = _trusted_store()
            now = time.time()
            store[_token_hash(token)] = {
                "created": now, "last_seen": now, "ua": str(user_agent)[:160]}
            # Cap the store: evict the least-recently-seen extras.
            while len(store) > _MAX_TRUSTED:
                oldest = min(store, key=lambda h: store[h]["last_seen"])
                store.pop(oldest)
            try:
                _save_trusted()
            except OSError as e:
                # Can't persist → don't pretend the device is remembered.
                log.error("could not write %s (%s) — session stays "
                          "non-persistent", TRUSTED_PATH, e)
                _sessions[token].persistent = False
    return token


def get_session(token: str | None) -> _Session | None:
    """Return a live session, or None if missing/expired. Expired ones are dropped.

    Remembered devices: a persistent session is validated against the trusted
    store instead of the idle timeout, and an unknown token that IS in the
    store is re-minted (that's how a cookie survives a server restart). With
    the feature disabled (remember_days == 0 — including the fail-closed
    lockdown config) persistent sessions are demoted to normal idle-expiring
    ones and the store is not consulted."""
    if not token:
        return None
    timeout = lock_timeout()
    with _lock:
        s = _sessions.get(token)
        if s is not None and not s.persistent:
            if (time.monotonic() - s.last_seen) > timeout:
                _sessions.pop(token, None)
                return None
            return s

        days = load().remember_days
        if s is not None:                      # persistent
            if days <= 0:
                s.persistent = False           # feature off → normal session
                return s
            if _trusted_entry(token, days) is None:
                _sessions.pop(token, None)     # trust revoked/expired
                return None
            return s

        # Unknown token (e.g. after a restart): honour it only via the store.
        if days <= 0 or _trusted_entry(token, days) is None:
            return None
        s = _Session(token=token, last_seen=time.monotonic(), persistent=True)
        _sessions[token] = s
        return s


def touch_session(token: str | None) -> None:
    """Slide the inactivity window forward (call on genuine activity only)."""
    if not token:
        return
    with _lock:
        s = _sessions.get(token)
        if s is None:
            return
        s.last_seen = time.monotonic()
        if s.persistent:
            # Slide the day-scale window too, throttled to ~hourly writes.
            e = _trusted_store().get(_token_hash(token))
            if e is not None and (time.time() - e["last_seen"]) > _TRUSTED_TOUCH_MIN:
                e["last_seen"] = time.time()
                with contextlib.suppress(OSError):
                    _save_trusted()


def revoke(token: str | None) -> None:
    """Kill a session. For a remembered device this also revokes the trust —
    "Lock" on a remembered device means "stop remembering it"."""
    if not token:
        return
    with _lock:
        _sessions.pop(token, None)
        store = _trusted_store()
        if store.pop(_token_hash(token), None) is not None:
            with contextlib.suppress(OSError):
                _save_trusted()


def revoke_all() -> None:
    with _lock:
        _sessions.clear()


def purge_expired() -> int:
    timeout = lock_timeout()
    now = time.monotonic()
    with _lock:
        dead = [t for t, s in _sessions.items()
                if not s.persistent and (now - s.last_seen) > timeout]
        for t in dead:
            _sessions.pop(t, None)
        # Prune remembered devices that fell off their sliding window (lazy
        # backstop — get_session drops them on contact anyway).
        days = load().remember_days
        if days > 0:
            store = _trusted_store()
            wall = time.time()
            stale = [h for h, e in store.items()
                     if (wall - e["last_seen"]) > days * 86400]
            for h in stale:
                store.pop(h, None)
            if stale:
                with contextlib.suppress(OSError):
                    _save_trusted()
    return len(dead)


# --------------------------------------------------------------------------- #
# Brute-force throttle (PIN entry)
# --------------------------------------------------------------------------- #


def throttle_wait() -> float:
    """Seconds the caller must wait before another unlock attempt (0 if clear)."""
    with _lock:
        if _fail_count >= _FAIL_LOCKOUT_AFTER and time.monotonic() < _fail_until:
            return _fail_until - time.monotonic()
        return 0.0


def register_failure() -> None:
    global _fail_count, _fail_until
    with _lock:
        _fail_count += 1
        if _fail_count >= _FAIL_LOCKOUT_AFTER:
            over = _fail_count - _FAIL_LOCKOUT_AFTER
            cooldown = _FAIL_BASE_COOLDOWN * (2 ** min(over, 6))   # cap ~5 min
            _fail_until = time.monotonic() + cooldown


def register_success() -> None:
    global _fail_count, _fail_until
    with _lock:
        _fail_count = 0
        _fail_until = 0.0
