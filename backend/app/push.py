"""Web Push: a ping to a device's lock screen, never the message itself.

What a push carries is ``{kind, thread_id, title}`` — "Nova replied", "Image
ready", "Morning brief posted". No message text, no picture, no preview: the
phone's lock screen is readable by whoever picks the phone up, and the locked
(Safe Mode) tier exists precisely so that what a bot said stays behind the PIN.
Tapping the notification opens the app on that thread, where the normal
session rules decide what is shown.

Who gets one
------------
Only a REMEMBERED device (auth.py, "keep this device unlocked"). A
subscription is stored under the SHA-256 of that device's session token — the
same key ``trusted-devices.yaml`` uses — and before every send the key is
checked against the trusted store. A device that locks, is forgotten, expires
or loses its trust in a PIN change therefore stops receiving pushes at once,
and its subscription is deleted the next time anything is sent. An install
with no PIN at all (fully open) has no device identity; its subscriptions are
keyed by endpoint and live only for as long as no PIN is set.

Files (both 0600, in the data dir)
----------------------------------
``push-vapid.json``          the VAPID keypair, generated once.
``push-subscriptions.json``  ``{"devices": {key: {endpoint, keys, created, ua}}}``.

The push service answering 404/410 means the browser dropped the
subscription; it is deleted. Anything else is logged and kept.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import os
import re
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from . import config

log = logging.getLogger("dispatch.push")

VAPID_FILE = "push-vapid.json"
SUBS_FILE = "push-subscriptions.json"

#: The kinds a ping may be. Titles are built by the caller from names only.
KINDS = frozenset({"reply", "image", "post"})
TITLE_MAX = 80
MAX_DEVICES = 50
#: How long the push service may hold a ping for a phone that is offline.
TTL_S = 6 * 3600
SEND_TIMEOUT_S = 10.0
#: Two pings of the same kind for the same thread inside this window collapse
#: to one (an image job finishing right after the reply that asked for it).
DEBOUNCE_S = 3.0

#: The browsers' push services. An endpoint is a URL the SERVER will POST to,
#: supplied by a client, so it is held to this list rather than allowed to
#: point anywhere (a loopback or LAN address would make this a request
#: forger). ``DISPATCH_PUSH_EXTRA_HOSTS`` (comma-separated exact hosts) adds
#: to it — for a self-hosted push service, or a test.
PUSH_HOST_SUFFIXES = (
    ".googleapis.com",            # Chrome, Edge on Android, Samsung, Brave
    ".push.services.mozilla.com",  # Firefox
    ".push.apple.com",            # Safari / iOS home-screen apps
    ".notify.windows.com",        # Edge on Windows
)
#: RFC 8292 `sub`: a contact for the push service operator. py_vapid only
#: accepts a mailto: one; the default names nobody. Set DISPATCH_PUSH_SUBJECT
#: (mailto:you@…) to give the push services a real contact.
DEFAULT_SUBJECT = "mailto:push@example.com"

_B64URL_RE = re.compile(r"^[A-Za-z0-9_-]+={0,2}$")

_lock = threading.Lock()
_last_sent: dict[tuple[str, str], float] = {}

def _nobody(key: str) -> bool:
    return False


#: Set by main.py: is this subscription key still a device allowed to receive?
#: Until it is, nobody is (fail closed).
_device_alive: Callable[[str], bool] = _nobody


def bind(device_alive: Callable[[str], bool]) -> None:
    global _device_alive
    _device_alive = device_alive


class PushError(ValueError):
    """A subscription the server refuses to store (message is user-facing)."""


# --------------------------------------------------------------------------- #
# Files
# --------------------------------------------------------------------------- #


def _path(name: str) -> Path:
    return Path(config.DATA_DIR) / name


def _write_private(path: Path, data: dict) -> None:
    """Atomic, owner-only JSON write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=1, sort_keys=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _load_vapid() -> dict:
    """The keypair, generated on first use. Never logged, never served whole."""
    path = _path(VAPID_FILE)
    with _lock:
        try:
            data = json.loads(path.read_text())
            if isinstance(data, dict) and data.get("private_pem") and data.get("public_key"):
                return data
            log.error("%s is malformed — generating a new VAPID keypair "
                      "(existing subscriptions will stop working)", path)
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            log.error("%s unreadable (%s) — generating a new VAPID keypair", path, e)
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec

        key = ec.generate_private_key(ec.SECP256R1())
        pem = key.private_bytes(serialization.Encoding.PEM,
                                serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode("ascii")
        public = key.public_key().public_bytes(serialization.Encoding.X962,
                                               serialization.PublicFormat.UncompressedPoint)
        data = {"private_pem": pem, "public_key": _b64url(public),
                "created": int(time.time())}
        _write_private(path, data)
        # A new key invalidates every subscription made with the old one.
        with contextlib.suppress(OSError):
            _path(SUBS_FILE).unlink()
        return data


def vapid_public_key() -> str:
    """The applicationServerKey a browser subscribes with (public; safe to serve)."""
    return str(_load_vapid()["public_key"])


def _subject() -> str:
    sub = (config.env("PUSH_SUBJECT") or "").strip()
    return sub if sub.startswith("mailto:") and "@" in sub else DEFAULT_SUBJECT


def _load_subs() -> dict[str, dict]:
    path = _path(SUBS_FILE)
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        log.error("%s unreadable (%s) — treating as empty", path, e)
        return {}
    devices = data.get("devices") if isinstance(data, dict) else None
    return {k: v for k, v in (devices or {}).items()
            if isinstance(k, str) and isinstance(v, dict)}


def _save_subs(subs: dict[str, dict]) -> None:
    _write_private(_path(SUBS_FILE), {"devices": subs})


def has_subscriptions() -> bool:
    return bool(_load_subs())


# --------------------------------------------------------------------------- #
# Subscriptions
# --------------------------------------------------------------------------- #


def _extra_hosts() -> set[str]:
    return {h.strip().lower() for h in
            (os.environ.get("DISPATCH_PUSH_EXTRA_HOSTS") or "").split(",") if h.strip()}


def _allowed_host(host: str) -> bool:
    host = host.lower().rstrip(".")
    return host in _extra_hosts() or any(host.endswith(s) for s in PUSH_HOST_SUFFIXES)


def validate(sub: Any) -> dict:
    """The part of a PushSubscription.toJSON() we keep, or PushError."""
    if not isinstance(sub, dict):
        raise PushError("Not a push subscription.")
    endpoint = sub.get("endpoint")
    keys = sub.get("keys") if isinstance(sub.get("keys"), dict) else {}
    if not isinstance(endpoint, str) or len(endpoint) > 1024:
        raise PushError("The subscription has no usable endpoint.")
    u = urlparse(endpoint)
    host = (u.hostname or "").lower()
    # Plain http only for a host the operator listed by name (a test server).
    if u.scheme != "https" and not (u.scheme == "http" and host in _extra_hosts()):
        raise PushError("The push endpoint must be https.")
    if not host or not _allowed_host(host):
        raise PushError("That push service is not one this app knows.")
    p256dh, auth_secret = keys.get("p256dh"), keys.get("auth")
    for v, lo, hi in ((p256dh, 80, 100), (auth_secret, 16, 32)):
        if not isinstance(v, str) or not (lo <= len(v) <= hi) or not _B64URL_RE.match(v):
            raise PushError("The subscription keys are malformed.")
    return {"endpoint": endpoint, "keys": {"p256dh": p256dh, "auth": auth_secret}}


def open_key(endpoint: str) -> str:
    """Subscription key on an install with no PIN (no device identity)."""
    return "open:" + hashlib.sha256(endpoint.encode()).hexdigest()


def subscribe(key: str, sub: Any, *, ua: str = "") -> None:
    clean = validate(sub)
    with _lock:
        subs = _load_subs()
        # One device, one subscription; and one endpoint, one device (a
        # browser that re-subscribes under a new identity moves its endpoint).
        for k in [k for k, v in subs.items() if v.get("endpoint") == clean["endpoint"]]:
            subs.pop(k, None)
        subs[key] = {**clean, "created": int(time.time()), "ua": str(ua)[:160]}
        while len(subs) > MAX_DEVICES:
            subs.pop(min(subs, key=lambda k: subs[k].get("created", 0)))
        _save_subs(subs)


def unsubscribe(key: str) -> bool:
    with _lock:
        subs = _load_subs()
        if subs.pop(key, None) is None:
            return False
        _save_subs(subs)
        return True


def is_subscribed(key: str) -> bool:
    return key in _load_subs()


def prune() -> int:
    """Delete subscriptions whose device is no longer allowed. Returns count."""
    with _lock:
        subs = _load_subs()
        dead = [k for k in subs if not _alive(k)]
        for k in dead:
            subs.pop(k, None)
        if dead:
            _save_subs(subs)
        return len(dead)


def _alive(key: str) -> bool:
    try:
        return bool(_device_alive(key))
    except Exception:                       # fail closed
        log.exception("push: device check failed for a subscription")
        return False


# --------------------------------------------------------------------------- #
# Sending
# --------------------------------------------------------------------------- #


def _send(sub: dict, data: str, *, topic: str) -> None:
    """One push (blocking). The test seam: tests replace this function."""
    from py_vapid import Vapid02
    from pywebpush import webpush

    vapid = _load_vapid()
    webpush(sub, data=data,
            vapid_private_key=Vapid02.from_pem(vapid["private_pem"].encode("ascii")),
            vapid_claims={"sub": _subject()},
            ttl=TTL_S, timeout=SEND_TIMEOUT_S,
            headers={"Urgency": "normal", "Topic": topic})


def _status_of(exc: BaseException) -> int | None:
    resp = getattr(exc, "response", None)
    status = getattr(resp, "status_code", None) if resp is not None else None
    return status if isinstance(status, int) else None


def payload(kind: str, thread_id: str, title: str) -> dict:
    """The whole of what leaves the box. No content, ever."""
    return {"kind": kind, "thread_id": str(thread_id)[:200],
            "title": " ".join(str(title).split())[:TITLE_MAX]}


async def notify(kind: str, thread_id: str, title: str) -> int:
    """Ping every live device. Returns how many pushes were accepted.

    Never raises: a push is a courtesy on top of a message that has already
    landed, and its failure must not become the turn's failure.
    """
    if kind not in KINDS or not thread_id:
        return 0
    try:
        subs = _load_subs()
        if not subs:
            return 0
        now = time.monotonic()
        last = _last_sent.get((kind, thread_id))
        if last is not None and now - last < DEBOUNCE_S:
            return 0
        _last_sent[(kind, thread_id)] = now
        if len(_last_sent) > 2048:
            _last_sent.clear()
        body = json.dumps(payload(kind, thread_id, title), separators=(",", ":"))
        # Topic: a newer ping for the same thread replaces one still queued
        # at the push service (<= 32 url-safe chars, RFC 8030 §5.4).
        topic = _b64url(hashlib.sha256(f"{kind}:{thread_id}".encode()).digest())[:32]
        sent, gone = 0, []
        for key, sub in subs.items():
            if not _alive(key):
                gone.append(key)
                continue
            try:
                await asyncio.to_thread(_send, sub, body, topic=topic)
                sent += 1
            except Exception as e:
                status = _status_of(e)
                if status in (404, 410):
                    gone.append(key)
                    log.info("push: subscription expired (HTTP %s) — removed", status)
                else:
                    log.warning("push: send failed (%s%s): %s", type(e).__name__,
                                f", HTTP {status}" if status else "", str(e)[:200])
        if gone:
            with _lock:
                fresh = _load_subs()
                for k in gone:
                    # Only if it is still the same subscription we tried.
                    if k in fresh and fresh[k].get("endpoint") == subs[k].get("endpoint"):
                        fresh.pop(k, None)
                _save_subs(fresh)
        return sent
    except Exception:
        log.exception("push: notify failed")
        return 0
