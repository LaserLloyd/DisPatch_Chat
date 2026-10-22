"""Which household connections a profile's PERSONAL bots may run on (BUILD-PLAN-v2 §1.10).

``profiles.backends`` holds ``{"allowed": [<connection id>, ...]}`` and nothing else in 4.0
(no per-profile default or model map; household bots ignore it entirely). Keys stay
household-wide; only Admins see them.

* ``{}`` (the default) means "every household connection".
* ``{"allowed": [...]}`` is an explicit list. An empty list is allowed on purpose: it
  switches personal bots off the network for that profile (they can still be made, but not
  pointed at any connection).

:func:`resolve_connection` is the one place that turns "what the editor asked for" into the
connection a personal bot will use, so create, patch and copy cannot disagree.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from . import connections


class BackendsError(ValueError):
    """``profiles.backends`` failed validation; ``code`` is a stable slug."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _known_ids(known_connections: Iterable[str] | None) -> set[str]:
    if known_connections is not None:
        return {str(c) for c in known_connections}
    return {c.id for c in connections.list_all()}


def validate_backends(value: Any, *, known_connections: Iterable[str] | None = None) -> dict:
    """Normalise a ``backends`` value from the People API: ``{}`` (inherit: every household
    connection) or ``{"allowed": [ids]}`` with each id a known connection
    (``known_connections``, default the connections table). Anything else ->
    :class:`BackendsError`. Returns the dict to store."""
    if value is None or value == {}:
        return {}
    if not isinstance(value, dict):
        raise BackendsError("backends_invalid", "backends must be an object.")
    extra = set(value) - {"allowed"}
    if extra:
        raise BackendsError("backends_invalid",
                            f"backends only takes 'allowed' (got {sorted(extra)[0]!r}).")
    allowed = value.get("allowed")
    if not isinstance(allowed, (list, tuple)) or not all(isinstance(x, str) for x in allowed):
        raise BackendsError("backends_invalid", "backends.allowed must be a list of connection ids.")
    ids = list(dict.fromkeys(x.strip() for x in allowed if x.strip()))
    known = _known_ids(known_connections)
    bad = [i for i in ids if i not in known]
    if bad:
        raise BackendsError("backends_unknown_connection", f"Unknown connection {bad[0]!r}.")
    return {"allowed": ids}


def allowed_backends(profile: Any) -> list[str] | None:
    """The connection ids ``profile``'s personal bots may choose; ``None`` = every household
    connection (``backends`` is ``{}``)."""
    raw = getattr(profile, "backends", None) or {}
    if not isinstance(raw, dict) or "allowed" not in raw:
        return None
    allowed = raw.get("allowed")
    if not isinstance(allowed, (list, tuple)):
        return None
    return [str(x) for x in allowed if isinstance(x, str)]


def permits(profile: Any, connection_id: str) -> bool:
    """May ``profile``'s personal bots use ``connection_id``?"""
    allowed = allowed_backends(profile)
    return allowed is None or connection_id in allowed


def resolve_connection(profile: Any, wanted: str | None = None, *,
                       fallback_ok: bool = False) -> str:
    """The connection id a personal bot of ``profile`` should use.

    ``wanted`` is the editor's pick (or the source bot's connection when copying). A pick the
    profile may not use raises :class:`BackendsError` ``backend_not_allowed`` - unless
    ``fallback_ok`` (a copy: fall back instead of failing). With no pick: the install's
    default connection when allowed, else the first allowed one that exists, else ``""`` (no
    connection yet - the bot answers with the "not connected" notice until one exists)."""
    if wanted:
        if connections.get(wanted) is None:
            raise BackendsError("connection_not_found", "Unknown connection.")
        if permits(profile, wanted):
            return wanted
        if not fallback_ok:
            raise BackendsError("backend_not_allowed",
                                "This profile may not use that connection.")
    default = connections.default_id()
    if default and permits(profile, default):
        return default
    allowed = allowed_backends(profile)
    for cid in allowed or []:
        if connections.get(cid) is not None:
            return cid
    return ""
