"""Recurring agent tasks (4.3, #13): the rule maths, the fire checks and the 30-second loop.

A schedule (``schedule_store``) says: every so often, run agent X as profile P with prompt Y,
and (optionally) post the card and the result into P's own chat T. The loop
(:func:`run_loop`, started in the app's lifespan by WP-INT) calls :meth:`Scheduler.tick` every
30 s. A tick fires every enabled row whose ``next_run`` is due, at most
``agents.max_concurrent`` new tasks per tick (the rest stay due for the next tick).

Every fire re-checks, as of NOW, what the routes checked when the schedule was saved - a
schedule never runs on stale permission:

1. the owner profile still exists                (no: the schedule is switched OFF, not run)
2. it is not disabled                             (no: skipped + a notice)
3. it still holds ``dispatch_agents``             (no: skipped + a notice)
4. the agent still exists                         (no: switched off + a notice)
5. the owner may still use it (published, enabled, permission; ``agents_api._usable_agent``)
                                                  (no: skipped + a notice)
6. the chat, if one is named, still exists, is still the owner's own and its character is one
   the owner may use                              (no: switched off + a notice)
7. the previous run of this schedule is not still going (no: this occurrence is skipped)

then it calls the normal create-task path (``TaskManager.dispatch``), so budgets, the queue,
the assigned card and the notifications are exactly those of ``POST /api/tasks``.

Missed runs while the app was down collapse into ONE catch-up run: a due row fires once and
its next run is computed from now, never from the missed slot.

Rules (``parse_rule``): ``every_minutes`` (>= 15), ``daily_at`` ``"HH:MM"`` and
``weekly_day_at`` ``{"day": "mon".."sun", "at": "HH:MM"}``, read in the schedule's IANA zone.
Across a DST change a wall-clock rule fires once per day: a time inside a spring-forward gap
runs at the shifted instant (02:30 becomes 03:30), an ambiguous fall-back time runs at its
first occurrence.

A "notice" is: the row's ``status``/``status_detail`` (what the UI shows), an audit row in the
activity log, a line in the schedule's chat when that chat still exists, a WebSocket frame to
the owner and a content-free push. The chat line and the push are sent only when the
status/detail CHANGES, so a schedule skipped every 15 minutes does not spam its owner.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import schedule_store

log = logging.getLogger("local-chat.agents.scheduler")

TICK_S = 30.0
MIN_EVERY_MINUTES = 15
MAX_EVERY_MINUTES = 31 * 24 * 60
MAX_PER_PROFILE = 20
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


# --------------------------------------------------------------------------- #
# Rules and time
# --------------------------------------------------------------------------- #


class RuleError(ValueError):
    """A rule or time zone the scheduler cannot run."""


def utc_iso(dt: datetime) -> str:
    return dt.astimezone(UTC).replace(microsecond=0).isoformat()


def parse_iso(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def zone(tz: str | None) -> ZoneInfo:
    name = (tz or "UTC").strip()
    if not name or len(name) > 64 or ".." in name or name.startswith("/"):
        raise RuleError(f"unknown time zone {tz!r}")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise RuleError(f"unknown time zone {tz!r}") from None


def _hhmm(v: Any, what: str) -> tuple[int, int]:
    m = _HHMM.match(str(v or "").strip())
    if not m:
        raise RuleError(f"{what} must be a time like 08:30")
    return int(m.group(1)), int(m.group(2))


def parse_rule(rule: Any) -> dict[str, Any]:
    """The normalized rule, or :class:`RuleError`. Exactly one kind per rule."""
    if not isinstance(rule, dict) or len(rule) != 1:
        raise RuleError("rule must be exactly one of every_minutes, daily_at, weekly_day_at")
    (kind, val), = rule.items()
    if kind == "every_minutes":
        if isinstance(val, bool) or not isinstance(val, int):
            raise RuleError("every_minutes must be a whole number")
        if val < MIN_EVERY_MINUTES:
            raise RuleError(f"every_minutes must be at least {MIN_EVERY_MINUTES}")
        if val > MAX_EVERY_MINUTES:
            raise RuleError(f"every_minutes must be at most {MAX_EVERY_MINUTES}")
        return {"every_minutes": val}
    if kind == "daily_at":
        h, m = _hhmm(val, "daily_at")
        return {"daily_at": f"{h:02d}:{m:02d}"}
    if kind == "weekly_day_at":
        if not isinstance(val, dict) or set(val) != {"day", "at"}:
            raise RuleError('weekly_day_at must be {"day": "mon".."sun", "at": "HH:MM"}')
        day = str(val["day"]).strip().lower()[:3]
        if day not in DAYS:
            raise RuleError("weekly_day_at.day must be one of " + ", ".join(DAYS))
        h, m = _hhmm(val["at"], "weekly_day_at.at")
        return {"weekly_day_at": {"day": day, "at": f"{h:02d}:{m:02d}"}}
    raise RuleError("rule must be exactly one of every_minutes, daily_at, weekly_day_at")


def _wall(day: date, hh: int, mm: int, z: ZoneInfo) -> datetime:
    """``day hh:mm`` on the wall clock of ``z``, as UTC. ``fold=0``: a time in a spring-forward
    gap maps to the shifted instant (02:30 -> 03:30), an ambiguous one to its first occurrence."""
    return datetime.combine(day, time(hh, mm)).replace(tzinfo=z, fold=0).astimezone(UTC)


def next_after(rule: dict[str, Any], tz: str | None, after: datetime) -> datetime:
    """The first fire time strictly after ``after`` (UTC, whole seconds)."""
    after = after.astimezone(UTC).replace(microsecond=0)
    rule = parse_rule(rule)
    if "every_minutes" in rule:
        return after + timedelta(minutes=rule["every_minutes"])
    z = zone(tz)
    if "daily_at" in rule:
        hh, mm = _hhmm(rule["daily_at"], "daily_at")
        weekday = None
    else:
        w = rule["weekly_day_at"]
        hh, mm = _hhmm(w["at"], "weekly_day_at.at")
        weekday = DAYS.index(w["day"])
    start = after.astimezone(z).date() - timedelta(days=1)
    for i in range(10):
        day = start + timedelta(days=i)
        if weekday is not None and day.weekday() != weekday:
            continue
        cand = _wall(day, hh, mm, z)
        if cand > after:
            return cand
    raise RuleError("no next run")                      # pragma: no cover - unreachable


def upcoming(rule: dict[str, Any], tz: str | None, after: datetime, n: int = 3) -> list[str]:
    out, t = [], after
    for _ in range(n):
        t = next_after(rule, tz, t)
        out.append(utc_iso(t))
    return out


def describe(row: dict[str, Any], *, owner_view: bool) -> dict[str, Any]:
    """What the API returns. Another profile's prompt and chat are never shown (an Admin sees
    that the schedule exists, whose it is and when it runs - not what it asks or where it posts)."""
    out = {
        "id": row["id"], "agent_id": row["agent_id"], "owner_profile": row["owner_profile"],
        "rule": row["rule"], "tz": row["tz"], "enabled": bool(row["enabled"]),
        "next_run": row["next_run"], "last_run": row["last_run"], "status": row["status"],
        "status_detail": row["status_detail"], "created_at": row["created_at"],
        "updated_at": row["updated_at"], "own": owner_view,
    }
    if owner_view:
        out.update(prompt=row["prompt"], thread_id=row["thread_id"], last_task_id=row["last_task_id"])
        try:
            if row["enabled"]:
                out["upcoming"] = upcoming(row["rule"], row["tz"], datetime.now(UTC))
        except RuleError:
            out["upcoming"] = []
    else:
        out.update(prompt=None, thread_id=None, last_task_id=None,
                   prompt_chars=len(row["prompt"] or ""))
    return out


# --------------------------------------------------------------------------- #
# The tick
# --------------------------------------------------------------------------- #

TERMINAL = ("succeeded", "failed", "cancelled")


def _now() -> datetime:
    return datetime.now(UTC)


def _default_manager():
    from .. import agents_service
    try:
        return agents_service.get_manager()
    except RuntimeError:
        return None


class Scheduler:
    """One per app. ``clock`` and ``manager`` are injectable for the tests."""

    def __init__(self, *, clock: Callable[[], datetime] = _now,
                 manager: Callable[[], Any] = _default_manager) -> None:
        self._clock = clock
        self._manager = manager
        self._lock = asyncio.Lock()

    async def tick(self, now: datetime | None = None) -> list[dict[str, Any]]:
        """Fire what is due. Returns one ``{"id", "outcome", ...}`` per row looked at."""
        mgr = self._manager()
        if mgr is None:
            return []
        from .. import agents_store
        async with self._lock:
            now = (now or self._clock()).astimezone(UTC).replace(microsecond=0)
            limit = max(1, int(agents_store.get_setting("agents.max_concurrent") or 4))
            started, out = 0, []
            for row in await asyncio.to_thread(schedule_store.due, utc_iso(now), 200):
                if started >= limit:
                    break                   # still due: the next tick picks it up
                try:
                    res = await self._fire(mgr, row, now)
                except Exception:
                    log.exception("schedule %s: fire failed", row["id"])
                    res = {"id": row["id"], "outcome": "error"}
                    self._advance(row, now)
                if res.get("outcome") == "started":
                    started += 1
                out.append(res)
            return out

    # -- one row ---------------------------------------------------------------- #

    def _advance(self, row: dict[str, Any], now: datetime, **fields: Any) -> bool:
        """Write the next run (and ``fields``). False when the rule no longer parses and the
        row was switched off instead."""
        try:
            nxt = utc_iso(next_after(row["rule"], row["tz"], now))
        except RuleError:
            fields.update(enabled=False, status="disabled",
                          status_detail="Its rule or time zone is no longer valid.")
            nxt = None
        schedule_store.update(row["id"], next_run=nxt, **fields)
        return nxt is not None

    async def _fire(self, mgr: Any, row: dict[str, Any], now: datetime) -> dict[str, Any]:
        from fastapi import HTTPException

        from .. import accounts, agents_api, agents_service, characters, ownership, policy
        from .. import agents_store as store
        from ..contracts.permissions import Permission
        from .interface import AgentBackendError

        sid, owner = row["id"], row["owner_profile"]
        profile = accounts.get_profile(owner)
        if profile is None:
            schedule_store.update(sid, enabled=False, next_run=None, status="disabled",
                                  status_detail="The profile it ran as was deleted.")
            accounts.log_activity("agent_schedule_disabled", subject=sid,
                                  detail="owner profile deleted", category="audit")
            return {"id": sid, "outcome": "disabled", "why": "owner_deleted"}
        if profile.disabled:
            return await self._skip(mgr, row, now, "Its profile is switched off.", "profile_disabled")
        pr = policy.profile_principal(profile, via="schedule")
        if not pr.can(Permission.DISPATCH_AGENTS):
            return await self._skip(mgr, row, now,
                                    "Its profile may no longer use agents (dispatch_agents).",
                                    "no_dispatch")
        agent = store.get_agent(row["agent_id"])
        if agent is None:
            return await self._disable(mgr, row, now, "Its agent was deleted.", "agent_deleted")
        try:
            agents_api._usable_agent(pr, agent["id"])
        except HTTPException:
            return await self._skip(mgr, row, now, "Its profile may no longer use this agent.",
                                    "agent_not_usable")
        if agent["disabled"]:
            return await self._skip(mgr, row, now, "Its agent is switched off.", "agent_disabled")
        tid = row["thread_id"]
        if tid:
            info = None
            if mgr.hooks.thread_info is not None:
                with contextlib.suppress(Exception):
                    info = await mgr.hooks.thread_info(tid)
            if info is None:
                return await self._disable(mgr, row, now, "Its chat was deleted.", "thread_deleted",
                                           post=False)
            if not ownership.may_see_thread(pr, info.get("owner_profile_id"), write=True) \
                    or not characters.may_use_id(info.get("bot_id"), pr):
                return await self._disable(mgr, row, now, "Its chat is no longer one of its profile's own.",
                                           "thread_not_owned", post=False)
        last = row["last_task_id"]
        if last:
            prev = store.get_task(last)
            if prev is not None and prev["state"] not in TERMINAL:
                self._advance(row, now, status="skipped",
                              status_detail="The previous run was still going.")
                return {"id": sid, "outcome": "skipped", "why": "previous_running"}
        # Claim the slot BEFORE the task exists (4.3 Fable): a crash between the dispatch and a
        # later next_run write would fire this occurrence again on restart. A crash right here
        # loses one occurrence instead - at most once, never twice.
        still_on = self._advance(row, now)
        try:
            task = await mgr.dispatch(agent=agent, prompt=row["prompt"],
                                      owner=agents_service.principal_key(pr),
                                      origin_thread_id=tid or None)
        except agents_service.BudgetExhausted as e:
            return await self._skip(mgr, row, now, f"Not run: {e.message}", "budget")
        except AgentBackendError as e:
            return await self._skip(mgr, row, now, f"Not run: {e.message}", "backend")
        except Exception as e:                       # a spec that no longer validates, etc.
            log.warning("schedule %s: dispatch refused: %s", sid, e)
            return await self._skip(mgr, row, now, "Not run: the agent could not be started.", "dispatch")
        done: dict[str, Any] = {"last_run": utc_iso(now), "last_task_id": task["task_id"]}
        if still_on:
            done.update(status="ok", status_detail="")     # else keep the "disabled" it just got
        schedule_store.update(sid, **done)
        accounts.log_activity("agent_schedule_fired", profile_id=owner, subject=sid,
                              detail=f"task={task['task_id']}", category="audit")
        return {"id": sid, "outcome": "started", "task_id": task["task_id"]}

    async def _skip(self, mgr: Any, row: dict[str, Any], now: datetime, text: str,
                    why: str) -> dict[str, Any]:
        changed = (row["status"], row["status_detail"]) != ("skipped", text)
        self._advance(row, now, status="skipped", status_detail=text)
        await self._notice(mgr, row, text, "skipped", changed=changed)
        return {"id": row["id"], "outcome": "skipped", "why": why}

    async def _disable(self, mgr: Any, row: dict[str, Any], now: datetime, text: str, why: str,
                       *, post: bool = True) -> dict[str, Any]:
        schedule_store.update(row["id"], enabled=False, next_run=None, status="disabled",
                              status_detail=text)
        await self._notice(mgr, row, text, "disabled", changed=True, post=post)
        return {"id": row["id"], "outcome": "disabled", "why": why}

    async def _notice(self, mgr: Any, row: dict[str, Any], text: str, status: str, *,
                      changed: bool, post: bool = True) -> None:
        from .. import accounts
        from .. import agents_ws as ws_frames
        sid, owner = row["id"], row["owner_profile"]
        accounts.log_activity(f"agent_schedule_{status}", profile_id=owner, subject=sid,
                              detail=text, category="audit")
        if not changed:
            return
        with contextlib.suppress(Exception):
            await ws_frames.publish({"type": "schedule_notice", "schedule_id": sid, "status": status,
                                     "detail": text}, owner)
        hooks = mgr.hooks
        if hooks.shutting_down():
            return
        if hooks.push is not None:
            with contextlib.suppress(Exception):
                hooks.push("schedule", f"schedule:{sid}", "A schedule needs your attention")
        tid = row["thread_id"]
        if post and tid and await mgr._thread_postable(tid):
            try:
                await hooks.deliver(
                    tid, f"Scheduled task not run: {text}",
                    metadata={"notice": {"level": "warn"}, "schedule": {"id": sid, "status": status}},
                    source_id=f"schedule:{sid}:{status}:{row['next_run']}", provisional=False,
                    dedup_recent_window=False)
            except Exception:
                log.warning("could not post a schedule notice for %s", sid, exc_info=True)


# --------------------------------------------------------------------------- #
# The loop (started by the app's lifespan - WP-INT wires it)
# --------------------------------------------------------------------------- #

_scheduler: Scheduler | None = None


def get_scheduler() -> Scheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = Scheduler()
    return _scheduler


async def run_loop(interval_s: float = TICK_S, *, scheduler: Scheduler | None = None) -> None:
    """Tick every ``interval_s`` until cancelled. A failing tick is logged, never fatal.
    Cancel the task to stop it; the cancellation propagates out of the sleep or the tick
    (an in-flight ``dispatch`` is the manager's own and finishes or fails on its own terms)."""
    sch = scheduler or get_scheduler()
    while True:
        try:
            await sch.tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("schedule tick failed")
        await asyncio.sleep(interval_s)
