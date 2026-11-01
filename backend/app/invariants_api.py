"""``GET /api/health/invariants`` - the 4.2 data invariants (app/invariants.py), as JSON.

An Admin (``admin_system``) or a machine holding ``machine:health`` may read it. The body is
ids and counts, never a profile's name, a bot's name or a chat. box-smoke reads it twice a day
through ``dispatch-api``; the scope must be in the live ``machine_scopes`` (see CHANGELOG 4.2).
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter

from . import invariants, kits, problem

log = logging.getLogger("local-chat.invariants")
router = APIRouter()


@router.get("/api/health/invariants", responses=problem.MACHINE)
async def health_invariants():
    pending = bool(kits._server.get("dirty")) or bool(
        (kits._server.get("task") is not None) and not kits._server["task"].done())
    try:
        return await asyncio.to_thread(invariants.report, sync_pending=pending)
    except Exception:
        log.warning("the invariant report failed", exc_info=True)
        return {"ok": False, "count": 0, "by_invariant": {}, "invariants": dict(invariants.INVARIANTS),
                "sync_pending": pending, "violations": [], "error": "the check itself failed; see the log"}
