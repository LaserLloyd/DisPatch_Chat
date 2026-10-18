"""Time limits and quiet hours for Restricted profiles.

A profile's ``budgets`` JSON may carry::

    {"minutes_per_day": 45, "quiet_hours": "20:30-07:00",
     "turns": 100, "threads": 20, "feedback": 50, "upload_mb": 100}

``minutes_per_day`` and ``quiet_hours`` are enforced here, in the auth gate; the other
four are daily *counts* charged by the handlers (``main._budget_charge``) and persisted
in ``usage_daily`` - they replace the 2.x per-IP counters, which lived in memory,
reset on restart and were shared by every device behind one address.

Active time is the sum of the gaps between a profile's consecutive requests, each gap
capped at 60 s, so a tab left open overnight counts for nothing.
"""

from __future__ import annotations

import re
import threading
import time
from datetime import datetime

from . import accounts, policy

_GAP_CAP = 60.0
_FLUSH_EVERY = 10.0

_lock = threading.Lock()
_last: dict[str, float] = {}
_pending: dict[str, float] = {}

_WINDOW_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$")


def parse_window(text: str) -> tuple[int, int] | None:
    """``"20:30-07:00"`` -> (minutes since midnight start, end); None if malformed."""
    m = _WINDOW_RE.match(text or "")
    if not m:
        return None
    a, b = int(m[1]) * 60 + int(m[2]), int(m[3]) * 60 + int(m[4])
    if not (0 <= a < 1440 and 0 <= b < 1440):
        return None
    return a, b


def in_quiet_hours(window: str, now: datetime | None = None) -> bool:
    w = parse_window(window)
    if w is None:
        return False
    start, end = w
    now = now or datetime.now()
    cur = now.hour * 60 + now.minute
    if start == end:
        return False
    return start <= cur < end if start < end else (cur >= start or cur < end)


def used_seconds(profile_id: str) -> int:
    with _lock:
        pending = _pending.get(profile_id, 0.0)
    return accounts.usage_get(profile_id, "active_seconds") + int(pending)


def check(principal: policy.Principal) -> policy.Decision | None:
    """A refusal when this profile is outside its quiet hours or out of time today."""
    prof = principal.profile
    if prof is None:
        return None
    b = prof.budgets or {}
    quiet = b.get("quiet_hours")
    if isinstance(quiet, str) and quiet and in_quiet_hours(quiet):
        return policy.Decision(False, 403, "quiet_hours", "This profile is not available right now.")
    minutes = b.get("minutes_per_day")
    if isinstance(minutes, (int, float)) and minutes > 0 and used_seconds(prof.id) >= minutes * 60:
        return policy.Decision(False, 403, "time_limit", "Today's time for this profile is used up.")
    return None


def note_active(principal: policy.Principal) -> None:
    """Credit the time since this profile's previous request (capped)."""
    prof = principal.profile
    if prof is None or not (prof.budgets or {}).get("minutes_per_day"):
        return
    now = time.monotonic()
    flush = 0
    with _lock:
        prev = _last.get(prof.id)
        _last[prof.id] = now
        if prev is None:
            return
        gap = min(now - prev, _GAP_CAP)
        pend = _pending.get(prof.id, 0.0) + gap
        if pend >= _FLUSH_EVERY:
            flush = int(pend)
            pend -= flush
        _pending[prof.id] = pend
    if flush:
        accounts.usage_add(prof.id, "active_seconds", flush)


def reset() -> None:
    with _lock:
        _last.clear()
        _pending.clear()
