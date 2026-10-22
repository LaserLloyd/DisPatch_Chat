"""WebSocket frames for agent tasks, and who may receive them.

Three frame types, server to client only (there is no task action on ``/ws``: the browser
acts over HTTP, which is gated and audited per route):

``task.event``     one normalized agent event (``app/agents/events.py``) for one task, with a
                   small summary of the task row so a list can repaint without a fetch
``task.approval``  an approval request opened, or answered (``state`` pending / allow / deny /
                   expired); the card in the timeline and the toast use it
``task.update``    the task row changed outside the event stream (created, released from a
                   hold, dismissed, purged)

Audience (the whole of the privacy rule for these frames):

* an Owner or Admin (and the open, unclaimed install) sees every task;
* a profile sees its OWN tasks, and only while it still holds ``dispatch_agents``;
* a Restricted socket, a machine and a socket that has not finished its handshake NEVER
  receive them - three independent layers: the audience predicate below, the redactor in
  ``main.redact_for_decoy`` (these types are in ``_DECOY_FRAME_DENY``), and the fact that
  the frames bypass the observers that feed add-ons (``ConnectionManager.broadcast_where``).

Sequence numbers (``event.seq``) are the replay cursor: a client that sees a gap refetches
``GET /api/tasks/{id}/events?after_seq=<last seen>``.
"""

from __future__ import annotations

import logging
from typing import Any

from .contracts.permissions import Permission
from .ws import manager

log = logging.getLogger("local-chat.agents.ws")

FRAME_EVENT = "task.event"
FRAME_APPROVAL = "task.approval"
FRAME_UPDATE = "task.update"
FRAME_TYPES = frozenset({FRAME_EVENT, FRAME_APPROVAL, FRAME_UPDATE})


def may_see_task(principal: Any, owner: str) -> bool:
    """May ``principal`` see a task dispatched by profile ``owner``?

    The single definition, used by the routes and by the frame audience.
    """
    if principal is None:
        return False
    kind = getattr(principal, "kind", "")
    if kind == "open":
        return True
    if kind != "profile" or getattr(principal, "is_restricted", True):
        return False
    if principal.is_admin:
        return True
    return bool(owner) and getattr(principal, "profile_id", None) == owner \
        and principal.can(Permission.DISPATCH_AGENTS)


def summary(task: dict) -> dict:
    """The part of a task row that rides along on every frame."""
    return {k: task.get(k) for k in (
        "state", "reason", "hold", "cost_usd", "tokens", "turns", "resumable", "started_at",
        "finished_at")}


def event_frame(task: dict, event: dict) -> dict:
    return {"type": FRAME_EVENT, "task_id": task["task_id"], "agent_id": task.get("agent_id"),
            "event": event, "task": summary(task)}


def approval_frame(task: dict, approval: dict, state: str) -> dict:
    return {"type": FRAME_APPROVAL, "task_id": task["task_id"], "request_id": approval["request_id"],
            "state": state, "tool": approval.get("tool"), "args_summary": approval.get("args_summary"),
            "scopes": approval.get("scopes"), "decided_by": approval.get("decided_by"),
            "agent_name": task.get("agent_name")}


def update_frame(task: dict) -> dict:
    return {"type": FRAME_UPDATE, "task_id": task["task_id"], "agent_id": task.get("agent_id"),
            "task": summary(task)}


async def publish(frame: dict, owner: str) -> int:
    """Send ``frame`` to every socket allowed to see a task owned by ``owner``."""
    try:
        return await manager.broadcast_where(frame, lambda pr: may_see_task(pr, owner))
    except Exception:                         # a frame is a courtesy on top of the database row
        log.exception("task frame not delivered")
        return 0
