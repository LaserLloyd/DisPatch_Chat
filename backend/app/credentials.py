"""Secret hashing for profile PINs and passwords.

argon2id (RFC 9106) through ``cryptography``'s ``Argon2id`` KDF - already a
dependency of ``pywebpush``, so no new package. Hashes are stored as PHC strings::

    $argon2id$v=19$m=19456,t=2,p=1$<salt b64>$<hash b64>  # scrub-ok: hash FORMAT, no secret

The parameters are the OWASP minimum for argon2id (19 MiB, 2 passes, 1 lane); they
travel inside the string, so raising them later only needs :func:`needs_rehash`.

The install's *legacy* PIN (security.yaml, PBKDF2-HMAC-SHA256) is also understood:
``pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>`` verifies, and  # scrub-ok: hash FORMAT, no secret
:func:`needs_rehash` reports it so a profile's PIN is upgraded to argon2id the next
time it is entered correctly.

Environment (tests only): ``DISPATCH_ARGON2_MEMORY_KIB`` / ``DISPATCH_ARGON2_PASSES``
lower the cost. They are read at call time and never from a file the app writes.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets

from cryptography.hazmat.primitives.kdf.argon2 import Argon2id

DEFAULT_MEMORY_KIB = 19_456
DEFAULT_PASSES = 2
DEFAULT_LANES = 1
_HASH_LEN = 32

#: Floors for the *accepted* parameters when verifying: a stored hash that claims a
#: trivially cheap cost is treated as unverifiable rather than trusted.
_MIN_MEMORY_KIB = 8
_MIN_PASSES = 1

MIN_PIN_LENGTH = 4
MIN_PASSWORD_LENGTH = 8


def _b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.b64decode(text + "=" * (-len(text) % 4))


def _cost() -> tuple[int, int, int]:
    mem = int(os.environ.get("DISPATCH_ARGON2_MEMORY_KIB", DEFAULT_MEMORY_KIB))
    passes = int(os.environ.get("DISPATCH_ARGON2_PASSES", DEFAULT_PASSES))
    return max(mem, _MIN_MEMORY_KIB), max(passes, _MIN_PASSES), DEFAULT_LANES


def hash_secret(secret: str) -> str:
    """argon2id PHC string for ``secret`` (a PIN or a password)."""
    mem, passes, lanes = _cost()
    salt = secrets.token_bytes(16)
    raw = Argon2id(salt=salt, length=_HASH_LEN, iterations=passes, lanes=lanes,
                   memory_cost=mem).derive(secret.encode("utf-8"))
    return f"$argon2id$v=19$m={mem},t={passes},p={lanes}${_b64e(salt)}${_b64e(raw)}"  # scrub-ok: hash FORMAT, no secret


def legacy_pbkdf2(secret: str, salt_hex: str, iterations: int) -> str:
    """The legacy digest, hex. Single definition shared with ``auth``."""
    return hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"),
                               bytes.fromhex(salt_hex), iterations).hex()


def legacy_string(pin_hash: str, salt_hex: str, iterations: int) -> str:
    """security.yaml's PIN fields in the self-describing profile format."""
    return f"pbkdf2_sha256${int(iterations)}${salt_hex}${pin_hash}"  # scrub-ok: hash FORMAT, no secret


def _parse_argon2(stored: str) -> tuple[int, int, int, bytes, bytes] | None:
    parts = stored.split("$")
    # ['', 'argon2id', 'v=19', 'm=..,t=..,p=..', salt, hash]
    if len(parts) != 6 or parts[1] != "argon2id":
        return None
    try:
        params = dict(kv.split("=", 1) for kv in parts[3].split(","))
        return (int(params["m"]), int(params["t"]), int(params["p"]),
                _b64d(parts[4]), _b64d(parts[5]))
    except (KeyError, ValueError):
        return None


def verify_secret(secret: str, stored: str | None) -> bool:
    """Constant-time check of ``secret`` against a stored hash of either format."""
    if not stored or not isinstance(secret, str):
        return False
    if stored.startswith("$argon2id$"):  # scrub-ok: hash FORMAT, no secret
        parsed = _parse_argon2(stored)
        if parsed is None:
            return False
        mem, passes, lanes, salt, want = parsed
        if mem < _MIN_MEMORY_KIB or passes < _MIN_PASSES or lanes < 1 or not want:
            return False
        try:
            got = Argon2id(salt=salt, length=len(want), iterations=passes, lanes=lanes,
                           memory_cost=mem).derive(secret.encode("utf-8"))
        except Exception:
            return False
        return hmac.compare_digest(got, want)
    if stored.startswith("pbkdf2_sha256$"):  # scrub-ok: hash FORMAT, no secret
        try:
            _, iters, salt_hex, want = stored.split("$")
            got = legacy_pbkdf2(secret, salt_hex, int(iters))
        except ValueError:
            return False
        return hmac.compare_digest(got, want)
    return False


def needs_rehash(stored: str | None) -> bool:
    """True when ``stored`` should be replaced by a fresh argon2id hash."""
    if not stored:
        return False
    if not stored.startswith("$argon2id$"):  # scrub-ok: hash FORMAT, no secret
        return True
    parsed = _parse_argon2(stored)
    if parsed is None:
        return True
    mem, passes, lanes, _, _ = parsed
    want_mem, want_passes, want_lanes = _cost()
    return (mem, passes, lanes) != (want_mem, want_passes, want_lanes)


def check_pin(pin: str) -> str | None:
    """None when ``pin`` is acceptable, else a human-readable reason."""
    if len(pin) < MIN_PIN_LENGTH:
        return f"PIN must be at least {MIN_PIN_LENGTH} characters"
    if len(pin) > 128:
        return "PIN is too long"
    return None


def check_password(password: str) -> str | None:
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Password must be at least {MIN_PASSWORD_LENGTH} characters"
    if len(password) > 256:
        return "Password is too long"
    return None
