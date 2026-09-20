"""Emails tab: embed MailForge's own NiceGUI dashboard.

MailForge's UI (``~/mailforge/src/mailforge/ui/app.py``) binds 127.0.0.1 on a
random high port persisted at ``<mail_data_dir>/ui_port`` and enforces its own
Host-header guard (any request whose Host is not exactly
``127.0.0.1:<port>``/``localhost:<port>`` is refused with 403) plus a
``/launch?k=<ui_launcher_key>`` handoff that mints its own signed session
cookie for the browser.

**Why an iframe-to-the-launch-URL instead of a same-origin reverse proxy:**
NiceGUI serves its own assets and socket.io endpoint from absolute,
root-rooted paths (``/_nicegui/<ver>/static/...``, ``/socket.io/...``) rather
than paths relative to the page. Confirmed live against the running
``mailforge.service`` on 2026-09-19: ``curl 127.0.0.1:<port>/`` returns
``href="/_nicegui/3.16.0/static/..."`` etc. Proxying that under a DisPatch
sub-path (``/mail/*``) would 404 every asset and break the socket.io upgrade,
because the browser would still ask ``/_nicegui/...`` and ``/socket.io/...``
at DisPatch's own origin, not the proxy's mount point — NiceGUI would need to
be told its ``root_path`` (a change to MailForge's own ``ui.run()`` wiring)
to emit sub-path-relative URLs, which is out of scope for a same-evening
integration. So this module follows the pattern already shipped for the
DeepSeek Harness pane (``app/harness.py`` + the ``harness-frame`` iframe):
DisPatch returns the (loopback-only, unlocked-session-only) launch URL, and
the browser iframes MailForge directly. The one-time launcher key therefore
does appear in the iframe's own address (never in DisPatch's page URL, never
in DisPatch's history) — the same trade DisPatch already made for dsh. What
reaches the browser after that first navigation is MailForge's own signed
session cookie, scoped to MailForge's origin.

Every route here is full-session only (see ``_require_mail`` in main.py) and
belongs in ``_decoy_blocked`` — a Safe-Mode / no-PIN caller must never learn
the port, let alone the key.
"""

from __future__ import annotations

import time

import httpx

from app.config import Settings

_PROBE_TTL = 15.0
_PROBE_TIMEOUT = 3.0


def installed(settings: Settings) -> bool:
    """MailForge's UI runtime files are present on disk."""
    d = settings.mail_data_dir
    return (d / "ui_port").exists() and (d / "ui_launcher_key").exists()


def read_port(settings: Settings) -> int | None:
    p = settings.mail_data_dir / "ui_port"
    try:
        return int(p.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _read_launcher_key(settings: Settings) -> str | None:
    p = settings.mail_data_dir / "ui_launcher_key"
    try:
        key = p.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return key or None


def base_url(settings: Settings) -> str | None:
    port = read_port(settings)
    if port is None:
        return None
    return f"http://127.0.0.1:{port}"


def launch_url(settings: Settings) -> str | None:
    """The one-time ``/launch?k=`` URL, or None if MailForge's UI has never
    run (no persisted port/key yet). Never logged, never cached across a
    request — read fresh off disk each call so a rotated key is honoured."""
    base = base_url(settings)
    key = _read_launcher_key(settings)
    if base is None or not key:
        return None
    return f"{base}/launch?k={key}"


_probe_cache: dict[str, tuple[float, bool]] = {}


async def reachable(settings: Settings) -> bool:
    """Plain GET of the UI root, cached briefly. Any HTTP response (even a
    403 from the Host guard, since we GET the loopback host it expects)
    counts as reachable; only a transport failure means the service is down."""
    base = base_url(settings)
    if base is None:
        return False
    cached = _probe_cache.get(base)
    now = time.time()
    if cached and now - cached[0] < _PROBE_TTL:
        return cached[1]
    ok = False
    try:
        async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT, follow_redirects=False) as client:
            await client.get(base + "/")
        ok = True
    except Exception:
        ok = False
    _probe_cache[base] = (now, ok)
    return ok
