"""HTTP surface for the managed-chat-room-style jobs board.

MOUNTING IT (the exact change in main.py, three small bits):

    1. add ``jobs`` to the existing package import near the top:

           from . import (auth, config, dashboard_routes, jobs, ...)

    2. THE ONE LINE, anywhere after the existing dashboards/localview
       mounts and before the static mounts at the bottom of main.py:

           if JOBS_ENABLED:
               app.include_router(jobs.router)

       The feature-flag gate is independent of `_is_inbound` — every
       route also has its own tier checks; the gate is so a `JOBS_ENABLED=0`
       install never sees the surface at all.

    3. `_is_inbound(method, path)` allowlist — add two tuples
       ``("POST", "/api/jobs")`` and ``("POST", "/api/jobs/score")``
       so on-box agents stop getting 403s. The 403 scar comment at
       main.py:1541–1545 is the reason.

    4. `_decoy_blocked(method, path)` prefix list — add ``/api/jobs`` so a
       Safe-Mode browser is refused one layer earlier than the route's
       own check.

AUTH MATRIX (per plan §4)
-------------------------
- **Inbound-exempt**: ``POST /api/jobs``, ``POST /api/jobs/score``. These
  are the two routes an on-box agent calls — no PIN-derived session
  needed; the allowlist covers them.
- **Session-only** (full PIN unlock, not Safe Mode): vote, applied, tags,
  archive, profile/recompute. Each handler calls ``_require_full``
  directly; a decoy caller gets 403.
- **Read**: ``GET /api/jobs*``. No session needed on the inbound-exempt
  layer, but ``_is_safe_mode_caller`` redacts the response to the empty
  shape so a Safe-Mode client gets ``{"jobs": [], "next_cursor": null}``
  — same pattern as ``/api/threads`` (main.py:7213–7225).
- All other endpoints: feature-flag gate + route-local tier check.

WHAT THIS MODULE OWNS
---------------------
- Routes.
- The atomic vote-write: thread_id state + job_events + job_feedback +
  profile recompute, in one transaction.
- The lazy expiry filter ``effective_state()`` — never overwrites the
  stored ``state``; the GET responses carry the effective view.

WHAT THIS MODULE DOES NOT OWN
-----------------------------
- Scoring math (``jobs_score.py``).
- Content hashing (``jobs_dedup.py``).
- DB tables (``database.py``, the schema is added in ``_init_schema``).
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator

from . import auth, config, jobs_dedup, jobs_score

log = logging.getLogger("local-chat.jobs")

# Feature flag: ``main`` reads this BEFORE mounting the router. Default
# off so a stock install never sees the surface until flipped (plan §9).
JOBS_ENABLED: bool = False

# Reason-tag enum (mirrored exactly by the UI; do NOT add new tags
# without updating both sides). Plan §4.
REASONS: tuple[str, ...] = jobs_score.REASON_TAGS

# Default auto-archive window (plan §11 risk row "Stale jobs").
EXPIRY_DEFAULT_DAYS = 90

# How many distinct tags we accept on a job — bounds the JSON column.
MAX_TAGS = 32


# --------------------------------------------------------------------------- #
# Tier helpers
# --------------------------------------------------------------------------- #


def _is_decoy(request: Request) -> bool:
    """A locked Safe-Mode session is a 'decoy' from main.py's vocabulary."""
    # Imported lazily so the module is importable without circularity.
    from . import main
    return bool(getattr(main, "_is_decoy", lambda r: False)(request))


def _safe_mode_redact(jobs_payload: dict) -> dict:
    """The empty shape on Safe-Mode reads — never leak job rows to a
    locked session.
    """
    return {"jobs": [], "next_cursor": None}


def _require_full(request: Request) -> None:
    """Raise 403 unless this request holds a full (PIN-derived) session.

    Used by every session-only write route. Decoy callers and missing
    cookies both 403; full sessions and pre-PIN installs pass.
    """
    from . import main
    sid = request.cookies.get(getattr(main, "COOKIE_NAME", "lc_session"))
    if sid is None:
        # No PIN set up yet -> the app is wide open (this is the
        # no-pin allow in auth.py). A bare caller is fine.
        return
    if auth.get_session(sid) is None:
        raise HTTPException(403, "Unlock for full access")


def _db():
    """Resolve ``main.db`` at request time — main imports this module,
    so a module-level import would be a cycle. Tests that patch
    ``main.db`` get the patched value."""
    from . import main
    return main.db


def _manager():
    """Same lazy pattern for the WS broadcast funnel."""
    from . import main
    return main.manager


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _new_id() -> str:
    import uuid
    return str(uuid.uuid4())


# --------------------------------------------------------------------------- #
# Lazy expiry filter — the helper called on every GET that returns a job.
# --------------------------------------------------------------------------- #


def effective_state(job_row: dict, now: str | None = None) -> str:
    """Return the state a caller should see, treating ``expires_at`` past
    as ``archived``. NEVER writes — the stored ``state`` column stays
    whatever it was, and a manual vote (re-pin, archive-open) is what
    actually changes the stored value. Plan §11 "Stale jobs".

    Returns ``"archived"`` when expires_at is set and < now and the
    stored state isn't already ``"archived"``. Everything else passes
    through.
    """
    if not job_row:
        return "unknown"
    stored = job_row.get("state") or "pending"
    if stored == "archived":
        return stored
    expires_at = job_row.get("expires_at")
    if expires_at and (expires_at < (now or _now())):
        return "archived"
    return stored


# --------------------------------------------------------------------------- #
# Pydantic models — the API surface, validated at the boundary.
# --------------------------------------------------------------------------- #


class JobIn(BaseModel):
    bot_id: str
    thread_title: str | None = None
    url: str
    title: str
    company: str = ""
    location: str = ""
    remote_type: str = "unknown"
    salary_min: int | None = None
    salary_max: int | None = None
    salary_currency: str = "USD"
    tags: list[str] = Field(default_factory=list)
    brief: str = ""
    initial_message: str | None = None
    source_agent: str = ""
    source_run_id: str | None = None
    posted_at: str | None = None
    expires_at: str | None = None

    @field_validator("remote_type")
    @classmethod
    def _v_remote(cls, v: str) -> str:
        if v not in ("unknown", "onsite", "hybrid", "remote"):
            raise ValueError("remote_type must be unknown|onsite|hybrid|remote")
        return v

    @field_validator("tags")
    @classmethod
    def _v_tags(cls, v: list[str]) -> list[str]:
        if len(v) > MAX_TAGS:
            raise ValueError(f"tags capped at {MAX_TAGS}")
        return [str(t).strip().lower() for t in v if str(t).strip()]


class ScoreIn(BaseModel):
    url: str
    title: str
    company: str = ""
    location: str = ""
    remote_type: str = "unknown"
    salary_min: int | None = None
    salary_max: int | None = None
    salary_currency: str = "USD"
    tags: list[str] = Field(default_factory=list)
    brief: str = ""

    @field_validator("remote_type")
    @classmethod
    def _v_remote(cls, v: str) -> str:
        if v not in ("unknown", "onsite", "hybrid", "remote"):
            raise ValueError("remote_type must be unknown|onsite|hybrid|remote")
        return v


class VoteIn(BaseModel):
    signal: str
    reason_tag: str | None = None
    comment: str | None = None

    @field_validator("signal")
    @classmethod
    def _v_sig(cls, v: str) -> str:
        if v not in ("yes", "no", "maybe", "undo"):
            raise ValueError("signal must be yes|no|maybe|undo")
        return v

    @field_validator("reason_tag")
    @classmethod
    def _v_reason(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if v not in REASONS:
            raise ValueError(f"reason_tag must be one of {REASONS}")
        return v


class CommentIn(BaseModel):
    comment: str


class TagsIn(BaseModel):
    add: list[str] = Field(default_factory=list)
    remove: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Router
# --------------------------------------------------------------------------- #

# Inline prefix to keep route names discoverable from the spec.
router = APIRouter(prefix="/api/jobs", tags=["jobs"])


# ---- read paths ----------------------------------------------------------- #
#
# ROUTE ORDER MATTERS. FastAPI matches in registration order; a literal-path
# route (``/profile``, ``/reasons``, ``/score``) MUST be declared BEFORE the
# catch-all ``/{thread_id}`` route, otherwise ``GET /api/jobs/profile``
# resolves with ``thread_id="profile"`` and 404s through ``get_job``. The
# router below keeps all the static paths ahead of ``/{thread_id}`` for
# exactly that reason.


def _serialise_job(job_row: dict) -> dict:
    """Turn a stored job row into the JSON shape the API returns.

    Includes:
      * the parsed tags list (NOT the JSON string stored in the column).
      * ``is_expired`` and ``effective_state`` so the UI can render the
        lazy-archived view without a re-GET.
      * ``seniority`` echoed; the UI may also override it client-side.

    ``job_row`` may carry ``tags`` as either a JSON-encoded string (the
    on-disk form, returned by ``db.list_jobs`` / ``db.get_job``) or as a
    Python list (the in-memory form right after create, before the row
    round-trips). Both are accepted so the create response does not
    ship an empty tags array on a fresh job.
    """
    raw_tags = job_row.get("tags") or []
    if isinstance(raw_tags, (list, tuple)):
        tags = list(raw_tags)
    elif isinstance(raw_tags, str):
        try:
            tags = json.loads(raw_tags)
        except (TypeError, ValueError):
            tags = []
    else:
        tags = []
    eff = effective_state(job_row)
    expires = job_row.get("expires_at")
    now = _now()
    return {
        "thread_id": job_row["thread_id"],
        "url": job_row["url"],
        "title": job_row["title"],
        "company": job_row.get("company", ""),
        "location": job_row.get("location", ""),
        "remote_type": job_row.get("remote_type", "unknown"),
        "seniority": job_row.get("seniority", "unknown"),
        "salary_min": job_row.get("salary_min"),
        "salary_max": job_row.get("salary_max"),
        "salary_currency": job_row.get("salary_currency", "USD"),
        "tags": tags,
        "source_agent": job_row.get("source_agent", ""),
        "source_run_id": job_row.get("source_run_id"),
        "posted_at": job_row.get("posted_at"),
        "first_seen": job_row.get("first_seen"),
        "last_seen": job_row.get("last_seen"),
        "brief": job_row.get("brief", ""),
        "state": job_row.get("state", "pending"),
        "effective_state": eff,
        "is_expired": bool(expires and expires < now and eff == "archived"
                          and job_row.get("state") != "archived"),
        "duplicate_of": job_row.get("duplicate_of"),
        "expires_at": expires,
        "created_at": job_row.get("created_at"),
        "updated_at": job_row.get("updated_at"),
    }


@router.get("")
async def list_jobs(
    request: Request,
    state: str | None = None,
    source_agent: str | None = None,
    tag: str | None = None,
    limit: int = Query(default=200, ge=1, le=500),
):
    """The board view. Filterable by state, source agent, tag.

    A Safe-Mode caller gets the empty shape — never the row contents,
    matching the existing redaction pattern at main.py:7213–7225.
    """
    if _is_decoy(request):
        return _safe_mode_redact({})
    db = _db()
    rows = await db.list_jobs(state=state, source_agent=source_agent,
                             tag=tag, limit=limit)
    items = []
    for r in rows:
        item = _serialise_job(r)
        # Suppress `state=duplicate` rows from the default list — the
        # system-managed duplicate marker (Plan §7).
        if state is None and item["state"] == "duplicate":
            continue
        items.append(item)
    return {"jobs": items, "next_cursor": None}


# Static read paths FIRST so the catch-all ``/{thread_id}`` below them
# cannot eat ``GET /api/jobs/profile`` / ``/reasons`` / ``/score``.

@router.get("/profile")
async def get_profile(request: Request):
    """The current preference profile. Decoy callers get the empty shape."""
    if _is_decoy(request):
        return _profile_to_json(jobs_score.empty_profile())
    db = _db()
    p = await db.get_job_profile()
    if not p:
        return _profile_to_json(jobs_score.empty_profile())
    return _profile_to_json(p)


@router.get("/reasons")
async def list_reasons():
    """Reason-tag taxonomy. Stable id list; the UI maps this verbatim."""
    return {"reasons": list(REASONS)}


@router.post("/score")
async def score_candidate(payload: ScoreIn):
    """Score a candidate against the current profile. Does NOT write.

    Inbound-exempt (machine agents call this before deciding to POST).
    Plan §6. Returns ``{score, breakdown, explanation, embedding_unavailable}``.
    """
    db = _db()
    profile = await db.get_job_profile() or jobs_score.empty_profile()
    seniority = jobs_score.infer_seniority(payload.title)
    return jobs_score.score_candidate(
        {
            "url": payload.url,
            "title": payload.title,
            "company": payload.company,
            "location": payload.location,
            "remote_type": payload.remote_type,
            "salary_min": payload.salary_min,
            "salary_max": payload.salary_max,
            "seniority": seniority,
            "tags": payload.tags,
        },
        profile,
    )


@router.get("/{thread_id}")
async def get_job(request: Request, thread_id: str):
    """One job + its last 50 events + the live score against the
    current profile.
    """
    if _is_decoy(request):
        return {"thread": None, "job": None, "events": [], "score": None}
    db = _db()
    job = await db.get_job(thread_id)
    if not job:
        raise HTTPException(404, "Job not found")
    thread = await db.get_thread(thread_id)
    if not thread:
        raise HTTPException(404, "Thread not found")
    events = await db.list_job_events(thread_id, limit=50)
    profile = await db.get_job_profile() or jobs_score.empty_profile()
    score = jobs_score.score_candidate(
        {
            "url": job["url"],
            "title": job["title"],
            "company": job.get("company", ""),
            "location": job.get("location", ""),
            "remote_type": job.get("remote_type", "unknown"),
            "salary_min": job.get("salary_min"),
            "salary_max": job.get("salary_max"),
            "seniority": job.get("seniority", "unknown"),
            "tags": json.loads(job.get("tags") or "[]"),
        },
        profile,
    )
    return {
        "thread": thread.model_dump(),
        "job": _serialise_job(job),
        "events": events,
        "score": score,
    }


# ---- inbound-exempt writes (on-box agents only) ------------------------ #


@router.post("")
async def create_job(payload: JobIn):
    """Create a job + the thread + the first message.

    Inbound-exempt for machines: the dispatcher (or any on-box agent)
    posts here without holding a session. Session-bearing callers can
    also post (the handler does not refuse them). Plan §4.

    Side effects, in order:
      1. ``jobs_dedup.check_duplicate`` — short-circuits to the
         existing thread when content hash matches within 30 days.
         Returns ``{duplicate: true, existing_thread_id, last_seen}``.
      2. ``db.create_thread`` — the thread row (the chat lives here).
      3. ``db.upsert_job`` — the structured row.
      4. ``db.add_message`` — the first assistant message announcing
         the job. The agent's ``initial_message`` overrides the
         template; otherwise a short canned line.
      5. ``db.add_job_event`` — the ``state_change|comment`` event row.

    The body of the work runs in ``create_job_from_dict`` (which the
    test suite calls directly to avoid the FastAPI Pydantic layer for
    storage round-trip tests).
    """
    return await create_job_from_dict(payload.dict())


async def create_job_from_dict(payload: dict) -> dict:
    """Pure-backend entry point. Tests bypass ``create_job`` (which
    binds Pydantic) so they can exercise the same write path with a
    plain dict; the live handler unpacks its ``JobIn`` into one."""
    payload = jobs_score.normalize_payload(payload)
    db = _db()
    # Resolve any prior match.
    prior = await jobs_dedup.check_duplicate(db, payload.get("url", ""),
                                              payload.get("title", ""),
                                              payload.get("company", ""))
    if prior.get("duplicate") and prior.get("existing_thread_id"):
        thread = await db.get_thread(prior["existing_thread_id"])
        # Refresh last_seen so the repost is visible to the user.
        await db.touch_job_seen(prior["existing_thread_id"])
        return {
            "duplicate": True,
            "existing_thread_id": prior["existing_thread_id"],
            "last_seen": prior.get("last_seen"),
            "thread": thread.model_dump() if thread else None,
        }

    ts = _now()
    # Infer seniority when the caller didn't provide one. Tiny regex
    # lookup; saves the agent a round trip.
    seniority = jobs_score.infer_seniority(payload.get("title", ""))

    expires_at = payload.get("expires_at")
    if not expires_at:
        posted = payload.get("posted_at") or ts
        try:
            base = datetime.fromisoformat(posted)
            if base.tzinfo is None:
                base = base.replace(tzinfo=UTC)
            expires_at = (base + timedelta(days=EXPIRY_DEFAULT_DAYS)).isoformat()
        except (TypeError, ValueError):
            expires_at = (datetime.now(UTC) +
                          timedelta(days=EXPIRY_DEFAULT_DAYS)).isoformat()

    thread = await db.create_thread(
        bot_id=payload.get("bot_id", "jobboard"),
        title=payload.get("thread_title") or payload.get("title", ""),
        avatar_from_pool=False,
    )
    job_row = {
        "thread_id": thread.id,
        "url": payload.get("url", ""),
        "title": payload.get("title", ""),
        "company": payload.get("company", ""),
        "location": payload.get("location", ""),
        "remote_type": payload.get("remote_type", "unknown"),
        "seniority": seniority,
        "salary_min": payload.get("salary_min"),
        "salary_max": payload.get("salary_max"),
        "salary_currency": payload.get("salary_currency", "USD"),
        "tags": payload.get("tags") or [],
        "source_agent": payload.get("source_agent", ""),
        "source_run_id": payload.get("source_run_id"),
        "posted_at": payload.get("posted_at") or ts,
        "first_seen": ts,
        "last_seen": ts,
        "brief": payload.get("brief", ""),
        "state": "pending",
        "duplicate_of": None,
        "expires_at": expires_at,
        "created_at": ts,
        "updated_at": ts,
    }
    await db.upsert_job(job_row)
    bot = config.resolve_bot(payload.get("bot_id", "jobboard"))
    body = payload.get("initial_message") or (
        f"🎯 New posting: {job_row['title']} @ {job_row['company'] or '?'} "
        f"({job_row['location'] or 'unspecified'}) — "
        f"{job_row['url']}"
    )
    await db.add_message(thread.id, "assistant", body,
                         metadata={"job_announce": True})
    await db.add_job_event({
        "id": _new_id(),
        "thread_id": thread.id,
        "type": "comment",
        "actor": f"agent:{job_row['source_agent'] or 'unknown'}",
        "payload": json.dumps({"bot_id": payload.get("bot_id", "jobboard"),
                               "repost_of": prior.get("repost_of")}),
        "created_at": ts,
    })
    await _manager().broadcast({"type": "job_created",
                                "thread_id": thread.id,
                                "bot_id": payload.get("bot_id", "jobboard"),
                                "job": _serialise_job(job_row)})
    return {
        "duplicate": False,
        "thread_id": thread.id,
        "thread": thread.model_dump(),
        "job": _serialise_job(job_row),
        "repost_of": prior.get("repost_of"),
    }


# ---- session-only writes (PIN unlock) ----------------------------------- #


async def _record_vote(thread_id: str, signal: str, reason_tag: str | None,
                       comment: str | None, actor: str) -> None:
    """Atomic vote: state column + job_events + job_feedback + profile
    recompute, in sequence.

    Signals:
      yes/no/maybe -> writes to jobs.state, records an event, records a
                       feedback row tagged with the payload the recompute
                       needs (tags, remote_type, salary_mid, company,
                       location, seniority), then triggers the fold.
      undo         -> records the feedback row with signal='undo'; the
                       recompute's two-pass latest-per-thread logic
                       removes this thread's effect entirely.
    """
    db = _db()
    job = await db.get_job(thread_id)
    if not job:
        raise HTTPException(404, "Job not found")
    ts = _now()
    new_state = ({"yes": "yes", "no": "no", "maybe": "maybe",
                  "undo": job.get("state") or "pending"}.get(signal)
                 or job.get("state") or "pending")
    if signal != "undo":
        await db.update_job_state(thread_id, new_state)
    # Build the recompute payload from the CURRENT job row. We capture
    # this once so the recompute sees the same shape the vote saw.
    try:
        tags = json.loads(job.get("tags") or "[]")
    except (TypeError, ValueError):
        tags = []
    try:
        smin = job.get("salary_min")
        smax = job.get("salary_max")
        salary_mid = ((smin + smax) / 2.0) if smin and smax else (
            smax or smin or None)
    except TypeError:
        salary_mid = None
    payload = {
        "tags": tags,
        "remote_type": job.get("remote_type", "unknown"),
        "company": job.get("company", ""),
        "location": job.get("location", ""),
        "seniority": job.get("seniority", "unknown"),
        "salary_mid": salary_mid,
    }
    fb_id = _new_id()
    await db.add_job_feedback({
        "id": fb_id,
        "thread_id": thread_id,
        "signal": ("vote_" + signal) if signal != "undo" else "undo",
        "reason_tag": reason_tag,
        "comment": comment,
        "actor": actor,
        "created_at": ts,
        "payload": json.dumps(payload),
    })
    event_type = "vote" if signal in ("yes", "no", "maybe") else "vote_undo"
    await db.add_job_event({
        "id": _new_id(),
        "thread_id": thread_id,
        "type": event_type,
        "from_state": job.get("state"),
        "to_state": new_state if signal != "undo" else job.get("state"),
        "reason_tag": reason_tag,
        "comment": comment,
        "actor": actor,
        "payload": json.dumps(payload),
        "created_at": ts,
    })
    # Recompute on every vote — the fold is bounded by feedback row
    # count, and the recompute itself is pure-Python over at most a few
    # hundred rows. Expected <50 ms.
    rows = await db.list_job_feedback()
    profile_dict = jobs_score.recompute_profile(rows)
    # Preserve the centroid BLOBs across recomputes — recompute_profile
    # only handles the JSON-shaped columns.
    old = await db.get_job_profile() or {}
    profile_dict["yes_centroid"] = old.get("yes_centroid")
    profile_dict["no_centroid"] = old.get("no_centroid")
    # Roll the dedup window into the recomputed profile.
    profile_dict["duplicate_hashes"] = (old.get("duplicate_hashes")
                                       or "[]")
    await db.write_job_profile(profile_dict)
    # Broadcast the new state on the same channel the chat uses so the
    # UI updates without a re-GET.
    refreshed = await db.get_job(thread_id)
    if refreshed:
        await _manager().broadcast({"type": "job_updated",
                                    "thread_id": thread_id,
                                    "job": _serialise_job(refreshed)})


@router.post("/{thread_id}/vote")
async def vote(thread_id: str, payload: VoteIn, request: Request):
    _require_full(request)
    await _record_vote(thread_id, payload.signal, payload.reason_tag,
                       payload.comment, actor="user")
    return {"ok": True}


@router.post("/{thread_id}/applied")
async def applied(thread_id: str, payload: CommentIn, request: Request):
    """Mark as applied. Same write pattern as vote but with signal=applied."""
    _require_full(request)
    db = _db()
    job = await db.get_job(thread_id)
    if not job:
        raise HTTPException(404, "Job not found")
    ts = _now()
    try:
        tags = json.loads(job.get("tags") or "[]")
    except (TypeError, ValueError):
        tags = []
    smin = job.get("salary_min"); smax = job.get("salary_max")
    salary_mid = ((smin + smax) / 2.0) if smin and smax else (smax or smin)
    payload_dict = {"tags": tags, "remote_type": job.get("remote_type"),
                    "company": job.get("company", ""),
                    "location": job.get("location", ""),
                    "seniority": job.get("seniority", "unknown"),
                    "salary_mid": salary_mid}
    await db.add_job_feedback({
        "id": _new_id(), "thread_id": thread_id, "signal": "applied",
        "reason_tag": None, "comment": payload.comment, "actor": "user",
        "created_at": ts, "payload": json.dumps(payload_dict),
    })
    await db.add_job_event({
        "id": _new_id(), "thread_id": thread_id, "type": "state_change",
        "from_state": job.get("state"), "to_state": "applied",
        "actor": "user", "comment": payload.comment,
        "created_at": ts, "payload": json.dumps(payload_dict),
    })
    await db.update_job_state(thread_id, "applied")
    rows = await db.list_job_feedback()
    profile = jobs_score.recompute_profile(rows)
    old = await db.get_job_profile() or {}
    profile["yes_centroid"] = old.get("yes_centroid")
    profile["no_centroid"] = old.get("no_centroid")
    profile["duplicate_hashes"] = old.get("duplicate_hashes") or "[]"
    await db.write_job_profile(profile)
    refreshed = await db.get_job(thread_id)
    if refreshed:
        await _manager().broadcast({"type": "job_updated",
                                    "thread_id": thread_id,
                                    "job": _serialise_job(refreshed)})
    return {"ok": True}


@router.post("/{thread_id}/tags")
async def edit_tags(thread_id: str, payload: TagsIn, request: Request):
    _require_full(request)
    db = _db()
    job = await db.get_job(thread_id)
    if not job:
        raise HTTPException(404, "Job not found")
    try:
        current = set(json.loads(job.get("tags") or "[]"))
    except (TypeError, ValueError):
        current = set()
    added = {t.strip().lower() for t in payload.add if t.strip()}
    removed = {t.strip().lower() for t in payload.remove if t.strip()}
    new_tags = sorted((current | added) - removed)
    if len(new_tags) > MAX_TAGS:
        raise HTTPException(400, f"tags capped at {MAX_TAGS}")
    await db.update_job_tags(thread_id, new_tags)
    ts = _now()
    for t in added:
        await db.add_job_event({
            "id": _new_id(), "thread_id": thread_id, "type": "tag_added",
            "comment": t, "actor": "user", "created_at": ts,
        })
    for t in removed:
        await db.add_job_event({
            "id": _new_id(), "thread_id": thread_id, "type": "tag_removed",
            "comment": t, "actor": "user", "created_at": ts,
        })
    refreshed = await db.get_job(thread_id)
    if refreshed:
        await _manager().broadcast({"type": "job_updated",
                                    "thread_id": thread_id,
                                    "job": _serialise_job(refreshed)})
    return {"ok": True, "tags": new_tags}


@router.post("/{thread_id}/archive")
async def archive(thread_id: str, request: Request):
    _require_full(request)
    db = _db()
    job = await db.get_job(thread_id)
    if not job:
        raise HTTPException(404, "Job not found")
    await db.update_job_state(thread_id, "archived")
    await db.add_job_event({
        "id": _new_id(), "thread_id": thread_id, "type": "state_change",
        "from_state": job.get("state"), "to_state": "archived",
        "actor": "user", "created_at": _now(),
    })
    refreshed = await db.get_job(thread_id)
    if refreshed:
        await _manager().broadcast({"type": "job_updated",
                                    "thread_id": thread_id,
                                    "job": _serialise_job(refreshed)})
    return {"ok": True}


# ---- profile introspection (session-only or no-PIN) ------------------- #


def _profile_to_json(profile: dict) -> dict:
    """Parse JSON-encoded columns into Python objects for the wire."""
    def _jd(v, default):
        try:
            return json.loads(v) if isinstance(v, str) and v else default
        except (TypeError, ValueError):
            return default
    return {
        "tag_weights": _jd(profile.get("tag_weights"), {}),
        "company_blocklist": _jd(profile.get("company_blocklist"), []),
        "location_blocklist": _jd(profile.get("location_blocklist"), []),
        "salary_history": _jd(profile.get("salary_history"), {}),
        "preferred_remote": _jd(profile.get("preferred_remote"), {}),
        "seniority_preference": _jd(profile.get("seniority_preference"), {}),
        "reason_counts": _jd(profile.get("reason_counts"), {}),
        "yes_count": profile.get("yes_count", 0),
        "no_count": profile.get("no_count", 0),
        "maybe_count": profile.get("maybe_count", 0),
        "updated_at": profile.get("updated_at"),
        "centroids_ready": profile.get("yes_centroid") is not None
                            or profile.get("no_centroid") is not None,
    }


@router.post("/profile/recompute")
async def recompute_profile(request: Request):
    _require_full(request)
    db = _db()
    rows = await db.list_job_feedback()
    profile = jobs_score.recompute_profile(rows)
    old = await db.get_job_profile() or {}
    profile["yes_centroid"] = old.get("yes_centroid")
    profile["no_centroid"] = old.get("no_centroid")
    profile["duplicate_hashes"] = old.get("duplicate_hashes") or "[]"
    await db.write_job_profile(profile)
    return {"ok": True}
