"""Which household connections a profile's PERSONAL bots may run on (BUILD-PLAN-v2 §1.10).

``profiles.backends`` holds ``{"allowed": [<connection id>, ...]}`` and nothing else in 4.0
(no per-profile default or model map; household bots ignore it entirely). Keys stay
household-wide; only Admins see them.

Status (Foundation): signatures only; WP-PROF (with its BACK half) writes the bodies.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


class BackendsError(ValueError):
    """``profiles.backends`` failed validation; ``code`` is a stable slug."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def validate_backends(value: Any, *, known_connections: Iterable[str] | None = None) -> dict:
    """Normalise a ``backends`` value from the People API: ``{}`` (inherit: every household
    connection) or ``{"allowed": [ids]}`` with each id a known connection
    (``known_connections``, default the connections table). Anything else ->
    :class:`BackendsError`. Returns the dict to store."""
    raise NotImplementedError("backends_policy.validate_backends: WP-PROF")


def allowed_backends(profile: Any) -> list[str] | None:
    """The connection ids ``profile``'s personal bots may choose; ``None`` = every household
    connection (``backends`` is ``{}``)."""
    raise NotImplementedError("backends_policy.allowed_backends: WP-PROF")
