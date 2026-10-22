"""The daily maintenance task (4.0 scale and retention, BUILD-PLAN-v2 §1.13.3).

``main.py`` calls :func:`start` once in the lifespan startup and :func:`stop` once at
shutdown (Foundation wired both), so WP-SCALE-OPS never edits ``main.py``.

What it will do (WP-SCALE-OPS): an in-app asyncio loop that runs :func:`run_nightly` once a
day at ``maintenance.time`` (default 04:30 local), skipping (and retrying in 30 min) while
``~/.local/state/openclaw/maintenance`` exists or a backup snapshot runs; a process-wide
lock; its own connection with ``busy_timeout``; bounded batch deletes; every step
try/except'd and recorded in ``maintenance_runs`` (migration 011).

Status (Foundation): :func:`start` and :func:`stop` are no-ops; :func:`run_nightly` raises.
"""

from __future__ import annotations

from typing import Any


def start(app: Any) -> None:
    """Start the daily loop for ``app`` (no-op until WP-SCALE-OPS). Must never raise: a
    failure to schedule maintenance must not stop the server."""
    return None


async def stop(app: Any = None) -> None:
    """Cancel the loop started by :func:`start` and wait for it (no-op for now). ``main``
    calls it from the lifespan shutdown, which has no app handle: keep the task here."""
    return None


def run_nightly(*, now: Any = None, steps: list[str] | None = None) -> dict:
    """Run every maintenance step (or only ``steps``) once and return
    ``{"started", "finished", "steps": [{"step", "rows", "bytes", "ms", "error"}]}``."""
    raise NotImplementedError("maintenance.run_nightly: WP-SCALE-OPS")
