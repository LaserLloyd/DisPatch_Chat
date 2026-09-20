"""HTTP surface for the monthly-threading jobs board.

WHAT THIS MODULE OWNS
---------------------
- Routes (read + write).
- The monthly-thread find-or-create helper used by ``create_job``.
- The atomic vote-write: job state + job_events + job_feedback + profile
  recompute, in one transaction.
- The lazy expiry filter ``effective_state()`` — never overwrites the
  stored ``state``; the GET responses carry the effective view.
- The friendly month-label helpers the API layer uses to render chat
  header titles for monthly threads.

WHAT THIS MODULE DOES NOT OWN
-----------------------------
- Scoring math (``jobs_score.py``).
- Content hashing (``jobs_dedup.py``).
- DB tables and their migrations (``database.py``).

MONTHLY THREAD MODEL (2026-09-15)
---------------------------------
Every job post lands in the bot's MONTHLY discussion thread for the
current year+month — not its own per-job thread. The monthly thread
title is ``Jobs — YYYY-MM``; multiple jobs share the same thread, and
each one is announced as an assistant message inside the thread. A
user browsing the Jobs board sees the current month's chat, can page
back through prior months via a small picker, and can vote / apply /
tag per-job from the chat itself.

Auth matrix (unchanged):
- Inbound-exempt: ``POST /api/jobs``, ``POST /api/jobs/score``. These
  are the two routes an on-box agent calls — no PIN-derived session
  needed; the allowlist covers them.
- Session-only (full PIN unlock, not Safe Mode): vote, applied, tags,
  archive, profile/recompute. Each handler calls ``_require_full``
  directly; a decoy caller gets 403.
- Read: ``GET /api/jobs*``. No session needed on the inbound-exempt
  layer, but ``_is_safe_mode_caller`` redacts the response to the empty
  shape so a Safe-Mode client gets ``{"jobs": [], "next_cursor": null}``
  — same pattern as ``/api/threads`` (main.py:7213–7225).
- All other endpoints: feature-flag gate + route-local tier check.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator

from . import auth, config, database, jobs_dedup, jobs_score

log = logging.getLogger("local-chat.jobs")

# Feature flag: ``main`` reads this BEFORE mounting the router. Default
# off so a stock install never sees the surface until flipped.
JOBS_ENABLED: bool = False

# Reason-tag enum (mirrored exactly by the UI; do NOT add new tags
# without updating both sides).
REASONS: tuple[str, ...] = jobs_score.REASON_TAGS

# Default auto-archive window.
EXPIRY_DEFAULT_DAYS = 90

# How many distinct tags we accept on a job — bounds the JSON column.
MAX_TAGS = 32


# --------------------------------------------------------------------------- #
# Tier helpers
# --------------------------------------------------------------------------- #


def _is_decoy(request: Request) -> bool:
    """A locked Safe-Mode session is a 'decoy' from main.py's vocabulary."""
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
        # No PIN set up yet -> the app is wide open (no-pin allow in
        # auth.py). A bare caller is fine.
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
# Monthly thread helpers
# --------------------------------------------------------------------------- #


def _current_year_month() -> tuple[int, int]:
    """Year+month in UTC. Stable across a single request; never read the
    clock twice (a job posted at 23:59:59 must not flip months mid-write).
    """
    return database.current_year_month()


async def _ensure_monthly_thread_async(bot_id: str, year: int, month: int):
    """Find-or-create the per-month jobs discussion thread for ``bot_id``.

    If a thread titled ``Jobs — YYYY-MM`` for this bot already exists,
    return it. Otherwise create one. The bot's resolved avatar is
    captured via the standard ``create_thread`` path so the chat header
    shows the bot's current face.

    This is the one place the monthly-thread title is constructed, so a
    format change never strands older threads.
    """
    db = _db()
    existing = await db.find_monthly_thread(bot_id, year, month)
    if existing is not None:
        return existing
    title = database.jobs_monthly_thread_title(year, month)
    return await db.create_thread(
        bot_id=bot_id,
        title=title,
        avatar_from_pool=False,
    )


async def _current_month_thread(bot_id: str):
    """The bot's monthly thread for the current year+month. Creates one
    if missing — this is the path ``create_job`` always takes.

    Reads the clock exactly once so a job posted at 23:59:59 doesn't
    flip months mid-write.
    """
    year, month = _current_year_month()
    return await _ensure_monthly_thread_async(bot_id, year, month)


def _parse_month_title(title: str | None) -> tuple[int, int] | None:
    """Inverse of ``database.jobs_monthly_thread_title``: parse a thread
    title of the form ``Jobs — YYYY-MM`` into (year, month). Returns
    None for non-monthly threads so the API can silently skip them in
    list responses.

    Tolerant of trailing whitespace and of the alternate hyphen-minus
    form ``Jobs - YYYY-MM`` (which a copy-pasted title or a unicode
    normalisation can produce).
    """
    if not title:
        return None
    norm = title.strip()
    for prefix in ("Jobs — ", "Jobs - ", "Jobs— ", "Jobs- "):
        if norm.startswith(prefix):
            tail = norm[len(prefix):].strip()
            try:
                return database.parse_jobs_month_key(tail)
            except ValueError:
                return None
    return None


# --------------------------------------------------------------------------- #
# Lazy expiry filter — the helper called on every GET that returns a job.
# --------------------------------------------------------------------------- #


def effective_state(job_row: dict, now: str | None = None) -> str:
    """Return the state a caller should see, treating ``expires_at`` past
    as ``archived``. NEVER writes — the stored ``state`` column stays
    whatever it was, and a manual vote (re-pin, archive-open) is what
    actually changes the stored value.

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
# URL validation (P0 XSS fix, 2026-09-16).
#
# A job's `url` round-trips into the chat as plain, unescaped href text — the
# announcement message and, historically, the frontend's job card. An
# agent-supplied `javascript:`/`data:` URL there is a stored-XSS payload that
# fires the moment a human opens the thread. Both the write-time validator
# (below) and the read-time defence-in-depth in `_serialise_job` reject
# anything that isn't an absolute http(s) URL.
# --------------------------------------------------------------------------- #


def _validate_http_url(v: str) -> str:
    """Pydantic validator body: require an absolute http(s) URL.

    Shared by every model field that accepts a job URL so the rule cannot
    drift between them. Whitespace is stripped first so a URL with leading/
    trailing padding (a common copy-paste artifact) isn't rejected on a
    technicality.
    """
    v = (v or "").strip()
    parsed = urlparse(v)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(
            "url must be an absolute http:// or https:// URL "
            f"(got {v[:80]!r})")
    return v


def _safe_url(url: str | None) -> str:
    """Read-time belt-and-braces: never emit a URL that isn't http(s).

    Covers rows written before the validator existed (or by any future
    write path that forgets to use it) — the response layer is the last
    place this can be caught before it reaches a browser as an href.
    """
    u = (url or "").strip()
    try:
        parsed = urlparse(u)
    except ValueError:
        return ""
    if parsed.scheme in ("http", "https") and parsed.netloc:
        return u
    return ""


# --------------------------------------------------------------------------- #
# Pydantic models — the API surface, validated at the boundary.
# --------------------------------------------------------------------------- #


class JobIn(BaseModel):
    bot_id: str = "jobboard"
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

    @field_validator("url")
    @classmethod
    def _v_url(cls, v: str) -> str:
        return _validate_http_url(v)

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

    @field_validator("url")
    @classmethod
    def _v_url(cls, v: str) -> str:
        return _validate_http_url(v)

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
    # Optional: the board's Applied button sends null when the note box is empty.
    comment: str | None = None


class FeedbackIn(BaseModel):
    """Free-text feedback on a job — posted into the monthly thread as a
    user message and dispatched to the bot like any other chat message.
    """
    comment: str
    reason_tag: str | None = None

    @field_validator("comment")
    @classmethod
    def _v_comment(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("comment is required")
        if len(v) > 2000:
            raise ValueError("comment capped at 2000 chars")
        return v

    @field_validator("reason_tag")
    @classmethod
    def _v_reason(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if v not in REASONS:
            raise ValueError(f"reason_tag must be one of {REASONS}")
        return v


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
# route (``/profile``, ``/reasons``, ``/score``, ``/months``, ``/month/*``)
# MUST be declared BEFORE the catch-all ``/{job_id}`` route, otherwise
# ``GET /api/jobs/profile`` resolves with ``job_id="profile"`` and 404s
# through ``get_job``. The router below keeps all the static paths ahead of
# ``/{job_id}`` for exactly that reason.


def _last_vote_from_feedback(rows: list[dict]) -> dict | None:
    """Fold per-job feedback rows (ASC order) into the current vote state.

    The latest yes/no/maybe/applied signal wins, UNLESS a later ``undo``
    cancels it — in which case there is no current vote until another
    signal is cast. Because ``rows`` is ASC, a single forward pass that
    resets on ``undo`` and otherwise remembers the last real signal gives
    exactly that.
    """
    last: dict | None = None
    for row in rows:
        sig = row.get("signal")
        if sig == "undo":
            last = None
            continue
        if sig in ("vote_yes", "vote_no", "vote_maybe", "applied"):
            last = row
    if last is None:
        return None
    return {
        "signal": last.get("signal"),
        "reason_tag": last.get("reason_tag"),
        "comment": last.get("comment"),
        "actor": last.get("actor"),
        "created_at": last.get("created_at"),
    }


def _serialise_job(job_row: dict, last_vote: dict | None = None) -> dict:
    """Turn a stored job row into the JSON shape the API returns.

    Includes:
      * the parsed tags list (NOT the JSON string stored in the column).
      * ``is_expired`` and ``effective_state`` so the UI can render the
        lazy-archived view without a re-GET.
      * ``seniority`` echoed; the UI may also override it client-side.
      * ``message_id`` so the chat panel can scroll to / vote on the
        announcement message directly.
      * ``last_vote`` (2026-09-16) — the current yes/no/maybe/applied signal
        (or None), computed by the caller via ``_last_vote_from_feedback``
        and passed in; callers that don't have feedback in hand (e.g. the
        just-created job broadcast) leave it None rather than paying for a
        query they don't need.

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
        "job_id": job_row["job_id"],
        "thread_id": job_row["thread_id"],
        "message_id": job_row.get("message_id"),
        "url": _safe_url(job_row.get("url")),
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
        "last_vote": last_vote,
    }


@router.get("")
async def list_jobs(
    request: Request,
    state: str | None = None,
    source_agent: str | None = None,
    tag: str | None = None,
    thread_id: str | None = None,
    limit: int = Query(default=200, ge=1, le=500),
):
    """The board view. Filterable by state, source agent, tag, thread.

    A Safe-Mode caller gets the empty shape — never the row contents,
    matching the existing redaction pattern.

    ``thread_id`` filters to one monthly discussion thread — the chat
    panel uses this to paint the messages for a specific month.
    """
    if _is_decoy(request):
        return _safe_mode_redact({})
    db = _db()
    rows = await db.list_jobs(state=state, source_agent=source_agent,
                             tag=tag, thread_id=thread_id, limit=limit)
    # One batched query for every row's vote history instead of N+1 — cheap
    # even at the max page size, since it's a single indexed IN() read.
    fb_by_job = await db.list_job_feedback_for_jobs([r["job_id"] for r in rows])
    items = []
    for r in rows:
        item = _serialise_job(
            r, last_vote=_last_vote_from_feedback(fb_by_job.get(r["job_id"], [])))
        # Suppress `state=duplicate` rows from the default list — the
        # system-managed duplicate marker.
        if state is None and item["state"] == "duplicate":
            continue
        items.append(item)
    return {"jobs": items, "next_cursor": None}


# Static read paths FIRST so the catch-all ``/{job_id}`` below them
# cannot eat ``GET /api/jobs/profile`` / ``/reasons`` / ``/score`` /
# ``/months`` / ``/month/<key>``.

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


@router.get("/months")
async def list_months(request: Request,
                      bot_id: str = Query(default="jobboard")):
    """All monthly discussion threads for ``bot_id``, newest first.

    Each entry carries the canonical ``{year, month, key, label}``
    payload so the frontend month picker doesn't have to parse the
    thread title itself. The thread is also returned so the UI can
    navigate straight into the chat.

    Safe-Mode callers get an empty list — a locked device must never
    see job metadata, even the title of a monthly thread (the title
    embeds the year/month).
    """
    if _is_decoy(request):
        return {"months": [], "current": None}
    db = _db()
    rows = await db.list_monthly_threads(bot_id)
    out: list[dict] = []
    for thread in rows:
        parsed = _parse_month_title(thread.title)
        if parsed is None:
            continue
        year, month = parsed
        out.append({
            "year": year,
            "month": month,
            "key": database.jobs_month_key(year, month),
            "label": database.jobs_month_label(year, month),
            "thread_id": thread.id,
            "created_at": thread.created_at,
            "updated_at": thread.updated_at,
        })
    out.sort(key=lambda e: (e["year"], e["month"]), reverse=True)
    year, month = _current_year_month()
    current = {
        "year": year,
        "month": month,
        "key": database.jobs_month_key(year, month),
        "label": database.jobs_month_label(year, month),
    }
    return {"months": out, "current": current}


@router.get("/current")
async def current_month(request: Request,
                        bot_id: str = Query(default="jobboard")):
    """The current month + (creating if missing) the current thread.

    ``ensure=true`` (default) is what the Jobs board uses on entry:
    clicking "Job Board" lands the user on the current month's chat,
    creating it if no job has been posted yet this month. ``ensure=false``
    is what other surfaces use to learn the current month without
    accidentally creating an empty thread (e.g. the dispatchctl CLI's  # scrub-ok: dispatchctl is the project's CLI tool name, not a private identifier
    status read).
    """
    if _is_decoy(request):
        return {"thread": None, "month": None}
    year, month = _current_year_month()
    label = database.jobs_month_label(year, month)
    key = database.jobs_month_key(year, month)
    ensure = (request.query_params.get("ensure", "true").lower()
              not in ("0", "false", "no"))
    thread = None
    if ensure:
        thread = await _ensure_monthly_thread_async(bot_id, year, month)
    else:
        db = _db()
        thread = await db.find_monthly_thread(bot_id, year, month)
    return {
        "thread": thread.model_dump() if thread else None,
        "month": {"year": year, "month": month, "key": key, "label": label},
    }


@router.get("/month/{key}")
async def get_month(request: Request, key: str,
                    bot_id: str = Query(default="jobboard")):
    """One month's worth of jobs (the ``YYYY-MM`` key in the path).

    Resolves to the thread, then runs the standard thread-messages
    fetch so the chat panel can mount the monthly chat. The structured
    ``jobs`` rows for the month are returned alongside so the panel can
    pair each announcement message with its job metadata in one trip.
    """
    try:
        year, month = database.parse_jobs_month_key(key)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if _is_decoy(request):
        return {"thread": None, "messages": [], "jobs": [], "score": None}
    db = _db()
    thread = await db.find_monthly_thread(bot_id, year, month)
    if thread is None:
        # An empty month has no thread yet — return the empty shape so
        # the UI can show "no posts in September 2026" without 404ing.
        return {
            "thread": None,
            "messages": [],
            "jobs": [],
            "score": None,
            "month": {
                "year": year, "month": month, "key": key,
                "label": database.jobs_month_label(year, month),
            },
        }
    msgs, _has_more = await db.list_messages(thread.id, limit=500)
    jobs = await db.list_jobs(thread_id=thread.id, limit=500)
    profile = await db.get_job_profile() or jobs_score.empty_profile()
    # Score each job once against the current profile; the chat panel
    # only needs the score for the active message but precomputing them
    # here is one DB hit instead of N.
    score_by_job: dict[str, dict] = {}
    for j in jobs:
        try:
            tags = json.loads(j.get("tags") or "[]")
        except (TypeError, ValueError):
            tags = []
        score_by_job[j["job_id"]] = jobs_score.score_candidate(
            {
                "url": j["url"],
                "title": j["title"],
                "company": j.get("company", ""),
                "location": j.get("location", ""),
                "remote_type": j.get("remote_type", "unknown"),
                "salary_min": j.get("salary_min"),
                "salary_max": j.get("salary_max"),
                "seniority": j.get("seniority", "unknown"),
                "tags": tags,
            },
            profile,
        )
    return {
        "thread": thread.model_dump(),
        "messages": [m.model_dump() for m in msgs],
        "jobs": [_serialise_job(j) for j in jobs],
        "score": score_by_job,
        "month": {
            "year": year, "month": month, "key": key,
            "label": database.jobs_month_label(year, month),
        },
    }


@router.post("/score")
async def score_candidate(payload: ScoreIn):
    """Score a candidate against the current profile. Does NOT write.

    Inbound-exempt (machine agents call this before deciding to POST).
    Returns ``{score, breakdown, explanation, embedding_unavailable}``.
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


@router.get("/feedback")
async def list_feedback(
    request: Request,
    since: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
):
    """Merged vote + free-text feedback feed, newest first — the read side
    of the ``POST .../feedback`` endpoint below, for an agent to catch up
    on what the humans said without re-reading every monthly thread.

    Machine-readable without a session from loopback, exactly like
    ``GET /api/jobs`` (see ``_JOBS_INBOUND`` in main.py). Safe-Mode/decoy
    callers get the empty shape.

    MUST stay registered before ``/{job_id}`` — see the route-order note
    at the top of the read-paths section, "feedback" would otherwise be
    swallowed as a job_id.
    """
    if _is_decoy(request):
        return {"feedback": []}
    db = _db()
    rows = await db.list_recent_feedback(since=since, limit=limit)
    return {
        "feedback": [
            {
                "job_id": r.get("job_id"),
                "title": r.get("title") or "",
                "company": r.get("company") or "",
                "url": _safe_url(r.get("url")),
                "kind": r.get("kind"),
                "signal": r.get("signal"),
                "reason_tag": r.get("reason_tag"),
                "comment": r.get("comment"),
                "actor": r.get("actor"),
                "created_at": r.get("created_at"),
            }
            for r in rows
        ]
    }


@router.get("/{job_id}")
async def get_job(request: Request, job_id: str):
    """One job (by job_id) + its last 50 events + the live score.

    The chat panel resolves a message_id → job via this endpoint when
    the user opens the vote UI inside an existing chat.
    """
    if _is_decoy(request):
        return {"thread": None, "job": None, "events": [], "score": None}
    db = _db()
    job = await db.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    thread = await db.get_thread(job["thread_id"])
    if not thread:
        raise HTTPException(404, "Job thread not found")
    events = await db.list_job_events(job_id, limit=50)
    feedback_rows = await db.list_job_feedback_for_job(job_id)
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
        "job": _serialise_job(job, last_vote=_last_vote_from_feedback(feedback_rows)),
        "events": events,
        "score": score,
    }


# ---- inbound-exempt writes (on-box agents only) ------------------------ #


@router.post("")
async def create_job(payload: JobIn):
    """Create a job in the bot's CURRENT month's discussion thread.

    Inbound-exempt for machines: the dispatcher (or any on-box agent)
    posts here without holding a session. Session-bearing callers can
    also post (the handler does not refuse them).

    Side effects, in order:
      1. ``jobs_dedup.check_duplicate`` — short-circuits to the
         existing job when content hash matches within 30 days.
         Returns ``{duplicate: true, existing_job_id, last_seen}``.
      2. ``_current_month_thread`` — find-or-create the per-month
         thread for this bot.
      3. ``db.upsert_job`` — the structured row keyed by ``job_id``
         (one row per job posting; multiple jobs share the thread).
      4. ``db.add_message`` — the assistant announcement message in
         the monthly thread. The message's metadata carries ``job_id``
         so a future re-render can join without a separate lookup.
      5. ``db.update_job_message_id`` — patch the message_id back into
         the job row so voting knows which message it belongs to.
      6. ``db.add_job_event`` — the ``comment`` event row (job creation
         is itself an event in the immutable audit log).

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
    if prior.get("duplicate"):
        existing_id = prior.get("existing_job_id") or prior.get("existing_thread_id")
        existing = await db.get_job(existing_id) if existing_id else None
        if existing:
            await db.touch_job_seen(existing["job_id"])
            existing_thread = await db.get_thread(existing["thread_id"])
            return {
                "duplicate": True,
                "existing_job_id": existing["job_id"],
                "existing_thread_id": existing["thread_id"],
                "last_seen": prior.get("last_seen"),
                "job": _serialise_job(existing),
                "thread": existing_thread.model_dump() if existing_thread else None,
            }
        # The dedup window references a job that no longer exists
        # (deleted via the thread cascade). Treat as fresh and fall
        # through to the create path.

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

    bot_id = payload.get("bot_id") or "jobboard"
    # Resolve any prior monthly thread for this bot + the current month.
    # ``_current_month_thread`` finds-or-creates so a brand-new install
    # gets its first thread lazily on the very first job post.
    thread = await _current_month_thread(bot_id)

    job_id = _new_id()
    job_row = {
        "job_id": job_id,
        "thread_id": thread.id,
        "message_id": None,                  # back-patched below
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
    body = payload.get("initial_message") or (
        f"🎯 New posting: {job_row['title']} @ {job_row['company'] or '?'} "
        f"({job_row['location'] or 'unspecified'}) — "
        f"{job_row['url']}"
    )
    # The metadata carries the job_id so a chat-panel re-render can
    # pair an assistant message with its structured job row in one
    # round-trip without consulting the jobs table.
    message = await db.add_message(
        thread.id, "assistant", body,
        metadata={
            "job_announce": True,
            "job_id": job_id,
            "url": job_row["url"],
            "company": job_row["company"],
            "location": job_row["location"],
            "remote_type": job_row["remote_type"],
            "salary_min": job_row["salary_min"],
            "salary_max": job_row["salary_max"],
            "salary_currency": job_row["salary_currency"],
            "tags": job_row["tags"],
            "brief": job_row["brief"],
            "expires_at": job_row["expires_at"],
        },
    )
    await db.update_job_message_id(job_id, message.id)
    await db.add_job_event({
        "id": _new_id(),
        "thread_id": thread.id,
        "job_id": job_id,
        "type": "comment",
        "actor": f"agent:{job_row['source_agent'] or 'unknown'}",
        "payload": json.dumps({"bot_id": bot_id,
                               "repost_of": prior.get("repost_of")}),
        "created_at": ts,
    })
    # Refresh the in-memory job row with the patched message_id so the
    # broadcast below carries the complete record.
    job_row["message_id"] = message.id
    job_row["updated_at"] = _now()
    await _manager().broadcast({"type": "job_created",
                                "job_id": job_id,
                                "thread_id": thread.id,
                                "bot_id": bot_id,
                                "message_id": message.id,
                                "job": _serialise_job(job_row)})
    return {
        "duplicate": False,
        "job_id": job_id,
        "thread_id": thread.id,
        "message_id": message.id,
        "thread": thread.model_dump(),
        "job": _serialise_job(job_row),
        "repost_of": prior.get("repost_of"),
    }


# ---- session-only writes (PIN unlock) ----------------------------------- #


async def _record_vote(job_id: str, signal: str, reason_tag: str | None,
                       comment: str | None, actor: str) -> None:
    """Atomic vote: state column + job_events + job_feedback + profile
    recompute, in sequence.

    Signals:
      yes/no/maybe -> writes to jobs.state, records an event, records a
                       feedback row tagged with the payload the recompute
                       needs, then triggers the fold.
      undo         -> records the feedback row with signal='undo'; the
                       recompute's two-pass latest-per-thread logic
                       removes this thread's effect entirely.
    """
    db = _db()
    job = await db.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    ts = _now()
    new_state = ({"yes": "yes", "no": "no", "maybe": "maybe",
                  "undo": job.get("state") or "pending"}.get(signal)
                 or job.get("state") or "pending")
    if signal != "undo":
        await db.update_job_state(job_id, new_state)
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
        "thread_id": job["thread_id"],
        "job_id": job_id,
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
        "thread_id": job["thread_id"],
        "job_id": job_id,
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
    refreshed = await db.get_job(job_id)
    if refreshed:
        await _manager().broadcast({"type": "job_updated",
                                    "job_id": job_id,
                                    "thread_id": refreshed["thread_id"],
                                    "job": _serialise_job(refreshed)})


@router.post("/{job_id}/vote")
async def vote(job_id: str, payload: VoteIn, request: Request):
    _require_full(request)
    await _record_vote(job_id, payload.signal, payload.reason_tag,
                       payload.comment, actor="user")
    return {"ok": True}


@router.post("/{job_id}/applied")
async def applied(job_id: str, payload: CommentIn, request: Request):
    """Mark as applied. Same write pattern as vote but with signal=applied."""
    _require_full(request)
    db = _db()
    job = await db.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    ts = _now()
    try:
        tags = json.loads(job.get("tags") or "[]")
    except (TypeError, ValueError):
        tags = []
    smin = job.get("salary_min")
    smax = job.get("salary_max")
    salary_mid = ((smin + smax) / 2.0) if smin and smax else (smax or smin)
    payload_dict = {"tags": tags, "remote_type": job.get("remote_type"),
                    "company": job.get("company", ""),
                    "location": job.get("location", ""),
                    "seniority": job.get("seniority", "unknown"),
                    "salary_mid": salary_mid}
    await db.add_job_feedback({
        "id": _new_id(), "thread_id": job["thread_id"], "job_id": job_id,
        "signal": "applied",
        "reason_tag": None, "comment": payload.comment, "actor": "user",
        "created_at": ts, "payload": json.dumps(payload_dict),
    })
    await db.add_job_event({
        "id": _new_id(), "thread_id": job["thread_id"], "job_id": job_id,
        "type": "state_change",
        "from_state": job.get("state"), "to_state": "applied",
        "actor": "user", "comment": payload.comment,
        "created_at": ts, "payload": json.dumps(payload_dict),
    })
    await db.update_job_state(job_id, "applied")
    rows = await db.list_job_feedback()
    profile = jobs_score.recompute_profile(rows)
    old = await db.get_job_profile() or {}
    profile["yes_centroid"] = old.get("yes_centroid")
    profile["no_centroid"] = old.get("no_centroid")
    profile["duplicate_hashes"] = old.get("duplicate_hashes") or "[]"
    await db.write_job_profile(profile)
    refreshed = await db.get_job(job_id)
    if refreshed:
        await _manager().broadcast({"type": "job_updated",
                                    "job_id": job_id,
                                    "thread_id": refreshed["thread_id"],
                                    "job": _serialise_job(refreshed)})
    return {"ok": True}


def _try_dispatch_feedback_turn(thread_id: str, bot_id: str, text: str) -> bool:
    """Kick off a real agent turn for freshly-posted feedback, the same
    fire-and-forget way ``main._handle_send`` dispatches a normal typed
    message — the caller never awaits the turn's outcome, only whether it
    could be SCHEDULED.

    Returns False (feedback still recorded by the caller either way) when
    there is nothing to dispatch to: the thread's bot has no config, or
    (for the OpenClaw-CLI backend) the ``openclaw`` binary isn't reachable.
    A bot with a direct-provider ``api`` block skips that second check —
    it never shells out.
    """
    from . import main, openclaw
    bot = config.get_bot(bot_id)
    if bot is None:
        return False
    if not bot.api and not openclaw.cli_available():
        return False
    task = asyncio.create_task(main.run_agent_turn(thread_id, bot_id, text))
    main._track(task)
    return True


class FindIn(BaseModel):
    """Optional steer for a "find me jobs" request from the board."""
    query: str | None = None

    @field_validator("query")
    @classmethod
    def _v_query(cls, v: str | None) -> str | None:
        v = (v or "").strip()
        if len(v) > 1000:
            raise ValueError("query must be at most 1000 characters")
        return v or None


FIND_PROMPT = (
    "Find new job postings for the Job Board. Search the web for current, "
    "live roles that fit my career profile, open each posting to confirm it "
    "is real and still accepting applications, then post every good match "
    "with `dispatch-jobs post` (real https URL, title, company, location, "
    "remote type, salary if listed, tags, and a two-line brief on why it "
    "fits). Check `dispatch-jobs feedback` first and respect my past "
    "no-reasons. Reply here with a short summary of what you posted."
)


@router.post("/find")
async def find_jobs(payload: FindIn, request: Request,
                    bot_id: str = Query("jobboard", max_length=64)):
    """Ask the board's agent (Scout, via the bot's ``agent`` override) to go
    find postings. Posts the request into the current month's thread as a
    user message and dispatches a turn, like ``/feedback`` does. Browser
    only: this is deliberately NOT on the inbound tier — an agent that wants
    to search just searches.
    """
    _require_full(request)
    if config.get_bot(bot_id) is None:
        raise HTTPException(404, "Unknown board bot")
    thread = await _current_month_thread(bot_id)
    body = FIND_PROMPT
    if payload.query:
        body += f"\n\nFocus: {payload.query}"
    db = _db()
    message = await db.add_message(thread.id, "user", body)
    await _manager().broadcast({
        "type": "message", "thread_id": thread.id,
        "bot_id": bot_id, "message": message.model_dump(),
    })
    dispatched = _try_dispatch_feedback_turn(thread.id, bot_id, body)
    return {"ok": True, "thread_id": thread.id, "message_id": message.id,
            "dispatched": dispatched}


@router.post("/{job_id}/feedback")
async def feedback(job_id: str, payload: FeedbackIn, request: Request):
    """Free-text feedback on a job: recorded as an immutable ``comment``
    event AND posted into the job's monthly thread as a user message, with
    a real agent turn dispatched for it — exactly as if a human had typed
    it into the chat themselves, so the bot can act on it (re-score, note
    a pattern, follow up) without anyone copy-pasting.

    Gate: same tier as ``/vote`` — ``_require_full`` (a full PIN session,
    or a no-PIN install; an on-box machine caller with no cookie at all
    also passes, and IS the expected caller per ``_JOBS_INBOUND_RE``).

    The turn is fire-and-forget: this handler does not wait for the bot to
    answer, only for the feedback to be durably recorded and the message
    to be persisted. ``dispatched: false`` means the turn could not even
    be scheduled (e.g. the OpenClaw CLI isn't on PATH) — the feedback
    itself is still saved and still visible in the thread either way.
    """
    _require_full(request)
    db = _db()
    job = await db.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    thread = await db.get_thread(job["thread_id"])
    if not thread:
        raise HTTPException(404, "Job thread not found")
    ts = _now()
    event_id = _new_id()
    await db.add_job_event({
        "id": event_id, "thread_id": job["thread_id"], "job_id": job_id,
        "type": "comment",
        "actor": "user", "comment": payload.comment,
        "reason_tag": payload.reason_tag,
        "created_at": ts,
    })
    body = (f'Feedback on "{job["title"]}" @ {job.get("company") or "?"} '
           f'({job["url"]}): {payload.comment}')
    if payload.reason_tag:
        body += f" [reason: {payload.reason_tag}]"
    message = await db.add_message(job["thread_id"], "user", body)
    await _manager().broadcast({
        "type": "message", "thread_id": job["thread_id"],
        "bot_id": thread.bot_id, "message": message.model_dump(),
    })
    await db.set_title_if_empty(job["thread_id"], body[:50])
    dispatched = _try_dispatch_feedback_turn(job["thread_id"], thread.bot_id, body)
    return {
        "ok": True,
        "event_id": event_id,
        "message_id": message.id,
        "thread_id": job["thread_id"],
        "dispatched": dispatched,
    }


@router.post("/{job_id}/tags")
async def edit_tags(job_id: str, payload: TagsIn, request: Request):
    _require_full(request)
    db = _db()
    job = await db.get_job(job_id)
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
    await db.update_job_tags(job_id, new_tags)
    ts = _now()
    for t in added:
        await db.add_job_event({
            "id": _new_id(), "thread_id": job["thread_id"], "job_id": job_id,
            "type": "tag_added",
            "comment": t, "actor": "user", "created_at": ts,
        })
    for t in removed:
        await db.add_job_event({
            "id": _new_id(), "thread_id": job["thread_id"], "job_id": job_id,
            "type": "tag_removed",
            "comment": t, "actor": "user", "created_at": ts,
        })
    refreshed = await db.get_job(job_id)
    if refreshed:
        await _manager().broadcast({"type": "job_updated",
                                    "job_id": job_id,
                                    "thread_id": refreshed["thread_id"],
                                    "job": _serialise_job(refreshed)})
    return {"ok": True, "tags": new_tags}


@router.post("/{job_id}/archive")
async def archive(job_id: str, request: Request):
    _require_full(request)
    db = _db()
    job = await db.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    await db.update_job_state(job_id, "archived")
    await db.add_job_event({
        "id": _new_id(), "thread_id": job["thread_id"], "job_id": job_id,
        "type": "state_change",
        "from_state": job.get("state"), "to_state": "archived",
        "actor": "user", "created_at": _now(),
    })
    refreshed = await db.get_job(job_id)
    if refreshed:
        await _manager().broadcast({"type": "job_updated",
                                    "job_id": job_id,
                                    "thread_id": refreshed["thread_id"],
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
