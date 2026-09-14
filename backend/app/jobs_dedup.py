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
from typing import Any
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

    Returns ``{duplicate, existing_thread_id?, last_seen?, repost_of?}``.
    The handler short-circuits to the existing thread when ``duplicate``
    is True and tags the new post as ``repost_of=existing_thread_id``
    when only ``repost_of`` is set (same URL, different title/company
    → within 90 days).

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
        return {"duplicate": False, "repost_of": r["thread_id"], "hash": h}

    return {"duplicate": False, "hash": h}


def remember_hash(profile: dict, thread_id: str, hash_str: str) -> dict:
    """Return a NEW profile dict with the hash appended, capped at 500.

    The profile's ``duplicate_hashes`` column stores a JSON array of
    {hash, thread_id, seen_at} entries; this helper appends one and
    trims the oldest when the list exceeds ``HASH_LIST_MAX``. Returns
    the modified column as a JSON string so the caller can write it
    straight back. Pure function — never mutates the input.
    """
    import json
    raw = profile.get("duplicate_hashes")
    entries = []
    if isinstance(raw, str) and raw:
        try:
            entries = json.loads(raw)
        except (TypeError, ValueError):
            entries = []
    elif isinstance(raw, list):
        entries = list(raw)
    entries.append({
        "hash": hash_str,
        "thread_id": thread_id,
        "seen_at": datetime.now(UTC).isoformat(),
    })
    if len(entries) > HASH_LIST_MAX:
        entries = entries[-HASH_LIST_MAX:]
    profile = dict(profile)
    profile["duplicate_hashes"] = json.dumps(entries)
    return profile


def normalized_for_log(record: dict) -> dict:
    """Pretty-print the most useful bits of a job row for an
    audit-log entry. Returns a NEW dict; no I/O."""
    return {
        "thread_id": record.get("thread_id"),
        "title": record.get("title"),
        "company": record.get("company"),
        "state": record.get("state"),
        "url": record.get("url"),
    }


# Alias for tests so they can patch the embedding model loader without
# importing the heavier module surface.
def _ensure_json_default(obj: Any) -> Any:
    """Fallback for json.dumps when the caller forgot to parse first."""
    try:
        import json
        return json.loads(obj) if isinstance(obj, str) else obj
    except (TypeError, ValueError):
        return obj
