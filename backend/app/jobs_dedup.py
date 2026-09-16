"""URL+title+company content hash and repost detection for the jobs board.

WHAT THIS IS
------------
A small utility the POST /api/jobs handler calls BEFORE the insert. Plan
§7 — close the "URL dedup / repost flow" gap. Two responsibilities:

  1. Compute a stable content hash for ``(url, title, company)`` so a
     repost of the same role (same URL, same title, same company) within
     30 days is detected as a duplicate and short-circuits to the
     existing thread.
  2. Detect a ``repost`` — same URL within 90 days but with title or
     company changed — and emit the relationship the agent should link
     to.

Both run in O(1) work per call plus a 30-day window scan in ``jobs``,
which is small because jobs are recent.

NORMALISATION
-------------
URL: lowercase, drop scheme, drop query/fragment, strip trailing
     slashes, collapse ``www.`` prefix. Same job posted with tracking
     params should hash equal.
TITLE: lowercase, collapse whitespace, strip ``(req id)`` /
      ``(rXYZ)`` suffixes that recruiting systems append.
COMPANY: lowercase, strip whitespace.

The hash function is exposed so the tests can verify that
``hash(url1, title, co)`` == ``hash(url2, title, co)`` when url1 and
url2 are the same after normalization.
"""
from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit, urlunsplit

# Window lengths (days). The two are deliberately different: 30-day dedup,
# 90-day repost. Plan §7.
DEDUP_WINDOW_DAYS = 30
REPOST_WINDOW_DAYS = 90
HASH_LIST_MAX = 500  # rolling window in job_profile.duplicate_hashes


def _norm_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    parts = urlsplit(url)
    host = (parts.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = (parts.path or "").rstrip("/")
    # Drop the query and fragment — tracking params are not identity.
    return urlunsplit(("", host, path, "", ""))


_TITLE_SUFFIX_RE = re.compile(
    r"\s*[\(\[]\s*(?:req(?:uisition)?\.?\s*id\.?\s*[:\-]?\s*|r[:\-]?\s*)([a-z0-9_-]+)\s*[\)\]]",
    re.IGNORECASE,
)


def _norm_title(title: str) -> str:
    t = (title or "").strip().lower()
    t = _TITLE_SUFFIX_RE.sub("", t)
    # Collapse all whitespace to single spaces.
    return re.sub(r"\s+", " ", t).strip()


def _norm_company(company: str) -> str:
    return (company or "").strip().lower()


def content_hash(url: str, title: str, company: str) -> str:
    """Stable SHA-256 over the normalised tuple. ``str`` of length 64.

    The output is hex-encoded so a plain string compare in tests is
    cheap and short. Returns the hash of the THREE-PART NORMALISED
    representation joined by ``"|"`` — a separator with no meaning in
    any of the three fields (no URL contains '|', and titles / company
    names with '|' are vanishingly rare).
    """
    n = f"{_norm_url(url)}|{_norm_title(title)}|{_norm_company(company)}"
    return hashlib.sha256(n.encode("utf-8")).hexdigest()


async def check_duplicate(db, url: str, title: str, company: str) -> dict:
    """Look for an existing job row whose content hash matches and was
    created in the last 30 days.

    Returns ``{duplicate, existing_job_id?, existing_thread_id?,
    last_seen?, repost_of?}``. The handler short-circuits to the existing
    job when ``duplicate`` is True and tags the new post as
    ``repost_of=existing_job_id`` when only ``repost_of`` is set (same
    URL, different title/company → within 90 days).

    The monthly-threading model (2026-09-15) keys everything off
    ``job_id``, not ``thread_id`` — multiple jobs share the same thread
    so the dedup must hand back the per-job id, otherwise a duplicate
    repost of job A would short-circuit to job B in the same month.

    The args ``url``, ``title``, ``company`` are the candidate's values.
    ``db`` is the Database instance.
    """
    h = content_hash(url, title, company)
    cutoff = (datetime.now(UTC) - timedelta(days=DEDUP_WINDOW_DAYS)).isoformat()

    rows = await db.list_jobs(limit=1000)
    same_hash = [r for r in rows
                 if (r.get("first_seen") or r.get("created_at") or "") >= cutoff
                 and content_hash(r.get("url", ""), r.get("title", ""),
                                  r.get("company", ""))
                     == h]
    if same_hash:
        existing = same_hash[0]
        return {"duplicate": True,
                "existing_job_id": existing["job_id"],
                "existing_thread_id": existing["thread_id"],
                "last_seen": existing.get("last_seen")
                              or existing.get("created_at"),
                "hash": h}

    # Repost: same URL within 90 days, different (title|company).
    repost_cutoff = (
        datetime.now(UTC) - timedelta(days=REPOST_WINDOW_DAYS)).isoformat()
    norm_u = _norm_url(url)
    for r in rows:
        if not ((r.get("first_seen") or r.get("created_at") or "")
                >= repost_cutoff):
            continue
        if _norm_url(r.get("url", "")) != norm_u:
            continue
        # Same URL — repost of a different title/company, link them.
        return {"duplicate": False, "repost_of": r["job_id"], "hash": h}

    return {"duplicate": False, "hash": h}

