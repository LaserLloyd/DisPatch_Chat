"""Clients tab ("WebBuilder"): a same-origin JSON proxy onto the practice
box's client-pipeline GUI API (``practice/gui/app.py``, docs/GUI-PLAN.md §2),
127.0.0.1:8793, PIN-gated.

Unlike MailForge's NiceGUI dashboard (see ``mailforge_bridge.py``), the
practice GUI backend is a plain FastAPI JSON API with no server-rendered HTML
assets to rewrite — DisPatch builds its OWN native UI (``frontend/static/js/
clients.js``, ported from ``practice/gui/static/app.js``) against this proxy,
so there is nothing but ``/api/*`` JSON (plus a couple of file/multipart
uploads) to forward. That makes a real reverse proxy the natural shape here,
unlike the Emails tab.

The proxy holds ONE practice-GUI session cookie server-side (module-level,
process-wide — this box has a single operator) obtained by POSTing the PIN
from ``practice_pin_file`` to ``/api/login``, and re-logs-in once on a 401.
The PIN itself is read fresh off disk on each login attempt and never sent
to, or logged for, the DisPatch caller.

Every route is full-session only (see ``_require_practice`` in main.py) and
belongs in ``_decoy_blocked``.
"""

from __future__ import annotations

import logging

import httpx

from app.config import Settings

log = logging.getLogger("local-chat.practice")

_TIMEOUT = 30.0

# Process-wide: the practice GUI's own session cookie, once we have one.
_session_cookie: str | None = None


def _read_pin(settings: Settings) -> str | None:
    try:
        pin = settings.practice_pin_file.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return pin or None


async def _login(client: httpx.AsyncClient, settings: Settings) -> str | None:
    pin = _read_pin(settings)
    if pin is None:
        return None
    try:
        resp = await client.post(
            f"{settings.practice_url}/api/login", json={"pin": pin}, timeout=_TIMEOUT,
        )
    except httpx.HTTPError:
        return None
    if resp.status_code != 204:
        return None
    for name, value in resp.cookies.items():
        return f"{name}={value}"
    return None


class UpstreamError(Exception):
    """The practice API could not be reached at all (transport failure)."""


class ProxyResponse:
    __slots__ = ("content", "content_type", "status_code")

    def __init__(self, status_code: int, content: bytes, content_type: str):
        self.status_code = status_code
        self.content = content
        self.content_type = content_type


async def proxy(
    settings: Settings,
    method: str,
    path: str,
    *,
    query: str = "",
    body: bytes | None = None,
    content_type: str | None = None,
) -> ProxyResponse:
    """Forward one request to ``<practice_url>/api/<path>``, logging in (and
    retrying once on 401) with the server-held session. ``path`` must already
    be validated by the caller (main.py) to be a same-scheme relative path —
    this module does not re-derive it from anything user-controlled beyond
    that string."""
    global _session_cookie
    url = f"{settings.practice_url}/api/{path}"
    if query:
        url = f"{url}?{query}"

    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False) as client:
        if _session_cookie is None:
            _session_cookie = await _login(client, settings)

        async def do_request() -> httpx.Response:
            headers: dict[str, str] = {}
            if content_type:
                headers["content-type"] = content_type
            if _session_cookie:
                headers["cookie"] = _session_cookie
            try:
                return await client.request(method, url, content=body, headers=headers)
            except httpx.HTTPError as e:
                raise UpstreamError(str(e)) from e

        resp = await do_request()
        if resp.status_code == 401:
            # Session cookie expired/invalid — log in again once.
            _session_cookie = await _login(client, settings)
            resp = await do_request()

        return ProxyResponse(
            status_code=resp.status_code,
            content=resp.content,
            content_type=resp.headers.get("content-type", "application/json"),
        )


def reset_session() -> None:
    """Test/ops hook: forget the cached session cookie."""
    global _session_cookie
    _session_cookie = None
