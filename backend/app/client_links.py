"""Clients tab: DisPatch-local "site links" overlay.

The practice box (proxied at ``/api/practice/*``, see ``practice_bridge.py``)
already hands back a live preview URL once one has been issued — it's the
``url`` field on each entry of ``client["previews"]["tokens"]`` in the
``GET /api/practice/clients/{id}`` response, and ``clients.js`` renders those
directly. What the practice box does NOT keep is a durable "this client's
real site is now live at X" fact: once the operator hands a build off (their own
domain, a client's own host, a shared-hosting account, whatever), the practice
box's job for that client is basically done, and nothing upstream tracks
where it ended up.

This module is that missing fact, kept entirely on the DisPatch side so it
outlives the practice box's own state: one small JSON file in the DisPatch
data dir, keyed by client id, holding a live URL, a repo/notes free-text
field (a local path or a remote, this box has both — never assumed to be a
clickable link), and free-text notes. Same shape as ``main._media_origins*``:
an in-process cache, guarded by one lock, atomic ``os.replace`` on write so a
crash mid-write reads as the previous good file rather than a torn one.

Every route that reaches this module sits behind the SAME gate as the rest
of the Clients tab (``main._require_practice`` — full session only, never
Safe Mode); this module holds no gate of its own on purpose, so there is
exactly one place that decision is made.
"""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config

FILE_NAME = "client-links.json"

_MAX_URL_LEN = 2000
_MAX_TEXT_LEN = 4000
_URL_RE = re.compile(r"^https?://", re.IGNORECASE)

_lock = threading.Lock()
_cache: dict[str, dict[str, Any]] | None = None


class InvalidLink(ValueError):
    """A submitted field failed validation. The message is safe to show the
    caller verbatim — it never echoes the offending value, so it can't be
    used to smuggle a stored XSS payload back out through an error toast."""


def _path() -> Path:
    return config.DATA_DIR / FILE_NAME


def reset_cache() -> None:
    """Test hook: forget the in-process cache after DATA_DIR changes."""
    global _cache
    with _lock:
        _cache = None


def _load() -> dict[str, dict[str, Any]]:
    global _cache
    if _cache is None:
        try:
            data = json.loads(_path().read_text(encoding="utf-8"))
            _cache = {str(k): v for k, v in data.items()} if isinstance(data, dict) else {}
        except (OSError, ValueError):
            _cache = {}
    return _cache


def _write(data: dict[str, dict[str, Any]]) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)          # atomic: a torn write is never picked up


def get(client_id: str) -> dict[str, Any]:
    """Always returns the full shape (``None`` for anything unset)."""
    with _lock:
        row = _load().get(str(client_id)) or {}
    return {
        "live_url": row.get("live_url") or None,
        "repo": row.get("repo") or None,
        "notes": row.get("notes") or None,
        "updated_at": row.get("updated_at") or None,
    }


def _clean_url(value: Any, field: str) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    if len(s) > _MAX_URL_LEN:
        raise InvalidLink(f"{field} is too long.")
    if not _URL_RE.match(s):
        raise InvalidLink(f"{field} must start with http:// or https://.")
    return s


def _clean_text(value: Any, field: str) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    if len(s) > _MAX_TEXT_LEN:
        raise InvalidLink(f"{field} is too long.")
    return s


def set_link(client_id: str, *, live_url: Any = None, repo: Any = None,
             notes: Any = None) -> dict[str, Any]:
    """Validate and persist. The panel always submits the whole form, so a
    field left blank clears it rather than leaving the old value behind —
    there is no partial-update path here, unlike ``PATCH``-style routes
    elsewhere in this app.

    Raises ``InvalidLink`` on a bad ``live_url`` (must be http/https) or an
    oversized field; the caller turns that into a 400.
    """
    row = {
        "live_url": _clean_url(live_url, "Live URL"),
        # A repo may be a local path (``~/Projects/example-site``) as often
        # as a URL on this box — free text, never validated or linkified.
        "repo": _clean_text(repo, "Repo"),
        "notes": _clean_text(notes, "Notes"),
    }
    global _cache
    with _lock:
        # Edit a COPY and only adopt it once it is on disk: a failed write
        # (disk full, permissions) must leave memory agreeing with the file,
        # not serving a value that the next restart silently loses.
        data = dict(_load())
        if not any(row.values()):
            data.pop(str(client_id), None)
        else:
            row["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            data[str(client_id)] = row
        _write(data)
        _cache = data
    return get(client_id)
