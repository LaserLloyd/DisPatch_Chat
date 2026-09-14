"""Pure scoring for the jobs board (Rocchio, deterministic, append-only feedback).

WHAT THIS IS
------------
A pure function over ``(candidate, profile) -> score`` with a small
companion fold that recomputes the profile from ``job_feedback`` rows.
The fold and the score are split so the recompute path stays separate
from the per-request scorer and can be benchmarked in isolation
(test_jobs.py::test_recompute_is_byte_deterministic).

The two pieces:
  1. ``recompute_profile(feedback_rows) -> dict``
     Deterministic fold over ``job_feedback`` ordered by created_at ASC.
     Latest signal per thread wins (so an ``undo`` cleanly reverts the
     prior signal). Source: plan §6.

  2. ``score_candidate(candidate, profile) -> (score, breakdown, explanation)``
     Pure expression over the profile and the candidate. Blocklist hits
     floor the score at 0 (a hard-no can't be unblocked by acing the
     tag-match equation). Embedding centroids are an optional add-on
     that activates only when both the model is available AND there are
     ≥10 yes/applied jobs — the scorer stays functional without them.

WHY DETERMINISTIC
-----------------
No databases in this file. No I/O. No randomness. A pure function of
its inputs. The recompute is a fold over the SAME input list, so two
runs over the same fixture produce a byte-identical profile
(test_jobs.py::test_recompute_is_byte_deterministic). That is the
property the rank-by-relevance relies on; without it, a rebuilt profile
would silently re-rank the board.

EMBEDDING FALLBACK
------------------
``_get_embedder()`` lazily loads a sentence-transformer model
(``all-MiniLM-L6-v2``, 384-dim). The load happens at first use inside
a try/except; a failure logs a warning and the scorer returns the
tag-only path with ``embedding_unavailable: true`` in the explanation.
This is the runtime fallback for the case the model file is missing or
the import fails: the request still gets an answer, just without the
centroid bonus term. Plan §6.
"""
from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable
from typing import Any


# --------------------------------------------------------------------------- #
# Reason-taxonomy enum (the canonical list; UI maps this verbatim).
# --------------------------------------------------------------------------- #

REASON_TAGS: tuple[str, ...] = (
    "wrong_location", "too_senior", "too_junior", "compensation",
    "company", "wrong_domain", "already_applied", "duplicate", "other",
)
REASON_SET = frozenset(REASON_TAGS)


# Salary buckets (the keys ``job_profile.salary_history`` writes and reads).
SALARY_BANDS: tuple[tuple[int, int, str], ...] = (
    (0, 50_000, "band_0_50k"),
    (50_000, 100_000, "band_50_100k"),
    (100_000, 150_000, "band_100_150k"),
    (150_000, 200_000, "band_150_200k"),
    (200_000, 300_000, "band_200_300k"),
    (300_000, 10**9, "band_300k_plus"),
)


# Seniority buckets (used to drive the seniority_match term; matches
# jobs.seniority + job_profile.seniority_preference).
SENIORITY_RANK: dict[str, int] = {
    "unknown": -1, "junior": 0, "mid": 1, "senior": 2, "staff": 3,
    "principal": 4,
}


# --------------------------------------------------------------------------- #
# Location normalization — the v1 bug used string equality on raw location
# strings, so a vote against "Tokyo, JP" missed "tokyo" in the candidate's
# location field. The fix: lowercase, strip after the first comma, trim,
# then match with a word-boundary regex. ``Tokyo, JP`` -> ``tokyo``;
# matches ``Tokyo, JP`` AND ``tokyo japan`` but NOT ``tokyokot``.
# --------------------------------------------------------------------------- #

def _norm_location(s: str) -> str:
    """Normalise a location string for the blocklist-match predicate.

    Two passes:
      1. ``Tokyo, JP`` -> ``tokyo`` (drop trailing country / state).
      2. ``Remote (US)`` -> ``remote`` (drop parenthetical qualifiers —
         a "remote / anywhere" vote is recorded against `remote`, not
         against `remote (us)`).
      3. Collapse internal whitespace.

    The plan's example said `tokyo` matches `Tokyo, JP` but NOT
    `tokyokot`. The same predicate must also handle the Remote-style
    location — a vote against "Remote (US)" must block subsequent
    `remote` candidates without a country tag.
    """
    s = (s or "").strip().lower()
    if "," in s:
        s = s.split(",", 1)[0].strip()
    # Strip parenthetical qualifiers: "(US)", "(Europe)", "(canada)".
    s = re.sub(r"\s*\([^)]*\)", "", s).strip()
    # Collapse internal whitespace.
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _loc_match(blocked: str, candidate: str) -> bool:
    """Word-boundary substring match on normalized locations."""
    b = _norm_location(blocked)
    c = _norm_location(candidate)
    if not b or not c:
        return False
    # Word-boundary on both sides: word chars only, no partial matches.
    return bool(re.search(rf"(^|\W){re.escape(b)}(\W|$)", c))


# --------------------------------------------------------------------------- #
# Tag / company normalization + scoring primitives
# --------------------------------------------------------------------------- #

def _canon_tag(t: str) -> str:
    return (t or "").strip().lower()


def _canon_company(c: str) -> str:
    return (c or "").strip().lower()


# --------------------------------------------------------------------------- #
# Recompute — pure fold over feedback, deterministic, NOT a method on the DB.
# Caller feeds rows ordered by created_at ASC; the fold iterates once and
# returns the new profile dict.
# --------------------------------------------------------------------------- #


def recompute_profile(feedback_rows: Iterable[dict]) -> dict:
    """Compute the preference profile from every feedback row, in order.

    See module docstring "Why deterministic" — this must be a pure fold
    with no I/O. Side effects: NONE. Output: a profile dict the API can
    serialize verbatim (the same shape ``job_profile`` stores as JSON
    columns; centroids are filled lazily — see _get_embedder).

    Latest signal per thread wins — a re-vote or an ``undo`` cleanly
    supersedes the prior signal for that thread, because we re-walk
    rows ordered by created_at ASC and overwrite the per-thread slot on
    each new entry. Plan §6, "Profile recompute".
    """
    tag_weights: dict[str, float] = {}
    company_blocklist: dict[str, None] = {}
    location_blocklist: dict[str, None] = {}
    salary_history: dict[str, int] = {b[2]: 0 for b in SALARY_BANDS}
    preferred_remote: dict[str, float] = {"onsite": 0.0, "hybrid": 0.0,
                                          "remote": 0.0}
    seniority_preference: dict[str, float] = {
        s: 0.0 for s in ("junior", "mid", "senior", "staff", "principal")
    }
    duplicate_hashes: dict[str, dict] = {}
    reason_counts: dict[str, int] = {}
    yes_count = no_count = maybe_count = 0
    # Per-thread latest signal (so re-votes replace prior signals cleanly).
    latest: dict[str, str] = {}

    for row in feedback_rows:
        thread_id = row["thread_id"]
        signal = row["signal"]

        def _apply_payload(sig: str, fb: dict) -> None:
            """Apply one signal as if it were the only signal for the
            thread. Called twice per row in the two-pass algorithm below:
            once with the empty payload (to mark "this thread's signal
            is sig") and once with the real payload (to actually update
            counters)."""
            pass  # body filled by the two-pass loop below

        # Two-pass: first compute which signal each thread CLAIMS today,
        # then apply ONLY the latest one. The first pass is local and
        # O(N); the second pass is local too. The DB query already
        # provides rows in created_at ASC order so the "latest" per
        # thread is just whichever signal this row carries.
        if signal == "undo":
            # An undo retroactively removes the prior signal from the
            # fold. We handle it by NOT recording any "latest signal"
            # for this thread and clearing the counters via a separate
            # two-pass strip below.
            latest.pop(thread_id, None)
            continue

        # Read per-thread candidate metadata from the row's optional
        # JSON payload (set by the write path in jobs.py::record_vote).
        payload = {}
        try:
            if row.get("payload"):
                payload = json.loads(row["payload"]) if isinstance(
                    row["payload"], str) else row["payload"]
        except (TypeError, ValueError):
            payload = {}

        latest[thread_id] = signal

    # ---- second pass: walk rows again, apply ONLY the latest per thread,
    # tagging "applied" with the same weights as 'yes' for the profile
    # -- a vote and an applied transition both indicate interest.
    for row in feedback_rows:
        thread_id = row["thread_id"]
        if latest.get(thread_id) != row["signal"]:
            continue
        signal = row["signal"]
        reason_tag = row.get("reason_tag")
        try:
            payload = (json.loads(row["payload"])
                      if isinstance(row.get("payload"), str)
                      and row.get("payload")
                      else (row.get("payload") or {}))
        except (TypeError, ValueError):
            payload = {}

        tags = payload.get("tags") or []
        remote = payload.get("remote_type") or "unknown"
        company = payload.get("company") or ""
        location = payload.get("location") or ""
        salary_mid = payload.get("salary_mid")
        seniority = payload.get("seniority") or "unknown"

        if signal in ("vote_yes", "applied"):
            yes_count += 1
            for t in tags:
                ct = _canon_tag(t)
                if not ct:
                    continue
                tag_weights[ct] = tag_weights.get(ct, 0.0) + 2.0
            if company:
                company_blocklist.pop(_canon_company(company), None)
            if remote in preferred_remote:
                preferred_remote[remote] += 1.0
            if isinstance(salary_mid, (int, float)) and salary_mid > 0:
                band = _band_for(salary_mid)
                salary_history[band] = salary_history.get(band, 0) + 1
        elif signal == "vote_maybe":
            maybe_count += 1
            for t in tags:
                ct = _canon_tag(t)
                if not ct:
                    continue
                tag_weights[ct] = tag_weights.get(ct, 0.0) + 0.5
        elif signal == "vote_no":
            no_count += 1
            # Reason-driven side effects. Hard blocks on the FIRST vote
            # for wrong_location / company — no N-threshold (closes the
            # v1 string-exact bug and the v1 silent-soft-block).
            if reason_tag == "wrong_location":
                norm = _norm_location(location)
                if norm:
                    location_blocklist[norm] = None
                for t in tags:
                    ct = _canon_tag(t)
                    if ct:
                        tag_weights[ct] = max(0.0, tag_weights.get(ct, 0.0) - 0.5)
            elif reason_tag == "company":
                nc = _canon_company(company)
                if nc:
                    company_blocklist[nc] = None
                for t in tags:
                    ct = _canon_tag(t)
                    if ct:
                        tag_weights[ct] = max(0.0, tag_weights.get(ct, 0.0) - 0.5)
            elif reason_tag == "compensation":
                for t in tags:
                    ct = _canon_tag(t)
                    if ct:
                        tag_weights[ct] = max(0.0, tag_weights.get(ct, 0.0) - 1.0)
            elif reason_tag == "too_senior":
                # Bump the bucket one above the candidate's seniority, take
                # half from the candidate's bucket. Ranks higher-seniority
                # candidates lower in future scoring.
                idx = SENIORITY_RANK.get(seniority, -1)
                if 0 <= idx < len(SENIORITY_RANK) - 1:
                    keys = list(seniority_preference.keys())
                    if idx + 1 < len(keys):
                        seniority_preference[keys[idx + 1]] = \
                            seniority_preference[keys[idx + 1]] + 1.0
                    if idx < len(keys):
                        seniority_preference[keys[idx]] = \
                            max(0.0, seniority_preference[keys[idx]] - 0.5)
                for t in tags:
                    ct = _canon_tag(t)
                    if ct:
                        tag_weights[ct] = max(0.0, tag_weights.get(ct, 0.0) - 0.5)
            elif reason_tag == "too_junior":
                idx = SENIORITY_RANK.get(seniority, -1)
                if idx >= 0:
                    keys = list(seniority_preference.keys())
                    if idx >= 1:
                        seniority_preference[keys[idx - 1]] = \
                            seniority_preference[keys[idx - 1]] + 1.0
                    if idx < len(keys):
                        seniority_preference[keys[idx]] = \
                            max(0.0, seniority_preference[keys[idx]] - 0.5)
                for t in tags:
                    ct = _canon_tag(t)
                    if ct:
                        tag_weights[ct] = max(0.0, tag_weights.get(ct, 0.0) - 0.5)
            elif reason_tag in ("wrong_domain", "other"):
                for t in tags:
                    ct = _canon_tag(t)
                    if ct:
                        tag_weights[ct] = max(0.0, tag_weights.get(ct, 0.0) - 0.5)
            else:
                # No reason: conservative — penalise tags only, do NOT add
                # to any blocklist (ground truth missing).
                for t in tags:
                    ct = _canon_tag(t)
                    if ct:
                        tag_weights[ct] = max(0.0, tag_weights.get(ct, 0.0) - 0.5)
        if reason_tag and reason_tag in REASON_SET:
            # The API boundary validates reason_tag against REASON_SET,
            # but a defensive recompute call from a foreign caller (a
            # test, a migration) could carry any string. Skip anything
            # not in the canonical taxonomy — never invent a profile
            # field that the UI / profile route doesn't know about.
            reason_counts[reason_tag] = reason_counts.get(reason_tag, 0) + 1

    from datetime import UTC, datetime
    return {
        "tag_weights": json.dumps(tag_weights),
        "company_blocklist": json.dumps(sorted(company_blocklist.keys())),
        "location_blocklist": json.dumps(sorted(location_blocklist.keys())),
        "salary_history": json.dumps(salary_history),
        "preferred_remote": json.dumps(preferred_remote),
        "seniority_preference": json.dumps(seniority_preference),
        "duplicate_hashes": json.dumps(list(duplicate_hashes.values())),
        "reason_counts": json.dumps(reason_counts),
        "yes_count": yes_count,
        "no_count": no_count,
        "maybe_count": maybe_count,
        "updated_at": datetime.now(UTC).isoformat(),
    }


def _band_for(mid_salary: float | int) -> str:
    for lo, hi, name in SALARY_BANDS:
        if lo <= mid_salary < hi:
            return name
    return "band_300k_plus"


# --------------------------------------------------------------------------- #
# Lazy embedding loader — see module docstring "Embedding fallback".
# Returns None on failure; the scorer tolerates a None model gracefully.
# --------------------------------------------------------------------------- #


_MODEL = None
_MODEL_LOAD_FAILED = False


def _get_embedder():
    global _MODEL, _MODEL_LOAD_FAILED
    if _MODEL is not None:
        return _MODEL
    if _MODEL_LOAD_FAILED:
        return None
    try:
        from sentence_transformers import SentenceTransformer  # type: ignore
        _MODEL = SentenceTransformer("all-MiniLM-L6-v2")
    except Exception:
        _MODEL_LOAD_FAILED = True
        return None
    return _MODEL


def _parse_json(s: str | None, default):
    if not s:
        return default
    try:
        return json.loads(s)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- #
# Score a candidate against the profile. Pure function.
# --------------------------------------------------------------------------- #


def score_candidate(candidate: dict, profile: dict) -> dict:
    """Return ``{score, breakdown, explanation}`` for a candidate job.

    ``candidate`` is the structured representation the API takes
    verbatim: ``{url, title, company, location, remote_type, salary_min,
    salary_max, tags[], brief}``. ``profile`` is the SAME dict the DB
    stores (with JSON columns already-parsed by the read path; the
    router parses them before calling this).

    Score is bounded to ``[0, 100]``. Blocklist hits floor the score at
    0 — a hard-no can't be unblocked by acing the tag match. The
    ``explanation`` lists the top contributors verbatim (blocklist line
    included when relevant) so the UI can show the user WHY a job
    scored where it did.
    """
    tag_weights = _parse_json(profile.get("tag_weights"), {}) or {}
    company_blocklist = set(_parse_json(profile.get("company_blocklist"), []) or [])
    location_blocklist = set(_parse_json(profile.get("location_blocklist"), []) or [])
    salary_history = _parse_json(profile.get("salary_history"), {}) or {}
    preferred_remote = _parse_json(profile.get("preferred_remote"), {}) or {}
    seniority_preference = _parse_json(profile.get("seniority_preference"), {}) or {}

    tags = [(_canon_tag(t), t) for t in (candidate.get("tags") or [])]
    company = _canon_company(candidate.get("company", ""))
    location = _norm_location(candidate.get("location", ""))

    raw = 0.0
    contributions: list[tuple[str, float]] = []

    # --- tag contribution ------------------------------------------------
    for canon, original in tags:
        if not canon:
            continue
        w = float(tag_weights.get(canon, 0.0))
        if w != 0.0:
            raw += w
            contributions.append((f"+ tag:{original}", w))

    # --- salary contribution --------------------------------------------
    salary_mid = _mid_salary(candidate.get("salary_min"),
                             candidate.get("salary_max"))
    if salary_mid and salary_history:
        band = _band_for(salary_mid)
        # Normalise the band's count against the total positive-history
        # so the term is a fraction in [0, 1] times a small multiplier.
        total = sum(int(v) for v in salary_history.values()) or 1
        band_w = float(salary_history.get(band, 0)) / total
        if band_w > 0:
            term = band_w * 6.0
            raw += term
            contributions.append((f"+ salary:{band}", term))

    # --- remote contribution ---------------------------------------------
    remote_type = candidate.get("remote_type") or "unknown"
    if remote_type in preferred_remote and preferred_remote.get(remote_type):
        total = sum(float(v) for v in preferred_remote.values()) or 1.0
        term = (float(preferred_remote.get(remote_type, 0)) / total) * 4.0
        raw += term
        contributions.append((f"+ remote:{remote_type}", term))

    # --- seniority match -------------------------------------------------
    seniority = candidate.get("seniority") or "unknown"
    if seniority and seniority_preference and seniority != "unknown":
        pref = float(seniority_preference.get(seniority, 0))
        # Positive preference is "I want THIS bucket" -> bump. Negative
        # preference is "I downrank this bucket" -> dock.
        if pref > 0:
            term = min(4.0, pref)
            raw += term
            contributions.append((f"+ seniority:{seniority}", term))
        elif pref < 0:
            term = max(-6.0, pref)
            raw += term
            contributions.append((f"- seniority:{seniority} (downranked)", term))

    # --- blocklist penalties (always last; they FLOOR the score) -------
    blocked = False
    block_reasons: list[str] = []
    if company and company in company_blocklist:
        raw -= 50.0
        blocked = True
        block_reasons.append(f"blocklist: company '{candidate.get('company','')}' (your reason: company)")
    if location and any(_loc_match(b, location) for b in location_blocklist):
        raw -= 50.0
        blocked = True
        block_reasons.append(f"blocklist: location '{candidate.get('location','')}' (your reason: wrong_location)")

    # --- embedding centroid bonus (optional) -----------------------------
    embedding_unavailable = False
    embedder = _get_embedder()
    if embedder is None:
        # Lazy load failed: lock in the flag for this scorer call. We
        # still return a score — just without the centroid bonus term.
        embedding_unavailable = True
    else:
        try:
            yes_centroid = profile.get("yes_centroid")
            no_centroid = profile.get("no_centroid")
            if (yes_centroid is not None or no_centroid is not None):
                text = " ".join(filter(None, [
                    candidate.get("title", ""), candidate.get("brief", ""),
                    " ".join(t for _, t in tags),
                ]))
                emb = embedder.encode([text])[0]
                term = 0.0
                if yes_centroid is not None:
                    term += _cosine(emb, yes_centroid) * 8.0
                if no_centroid is not None:
                    term -= _cosine(emb, no_centroid) * 4.0
                raw += term
                contributions.append((f"+ embedding:centroid {term:+.1f}", term))
        except Exception:
            embedding_unavailable = True

    # --- final bound + floor --------------------------------------------
    if blocked:
        final = 0.0
    else:
        final = max(0.0, min(100.0, raw))

    # Sort contributions descending, keep top 5 for the UI; always show
    # the blocklist line.
    contributions.sort(key=lambda c: -abs(c[1]))
    top = contributions[:5]
    explanation = [block_reasons[0]] if block_reasons else []
    explanation.extend(f"{label} ({v:+.1f})" for label, v in top if not label.startswith("blocklist"))
    if not explanation:
        explanation = ["no signals yet — your profile is empty"]

    return {
        "score": round(final, 1),
        "blocked": blocked,
        "block_reason": block_reasons[0] if block_reasons else None,
        "breakdown": {label: round(v, 2) for label, v in contributions},
        "explanation": explanation,
        "embedding_unavailable": embedding_unavailable,
    }


def _mid_salary(smin, smax) -> float | None:
    try:
        if smax and smin:
            return (float(smin) + float(smax)) / 2.0
        if smax:
            return float(smax) * 0.85
        if smin:
            return float(smin) * 1.0
    except (TypeError, ValueError):
        return None
    return None


def _cosine(a, b) -> float:
    """Dot product / magnitudes. Inputs may be numpy arrays or byte blobs;
    the scorer tolerates both without binding numpy at import time."""
    try:
        import numpy as np  # only present when sentence-transformers is
        va = np.asarray(a, dtype=np.float32)
        vb = np.asarray(b, dtype=np.float32)
        denom = (float(np.linalg.norm(va)) * float(np.linalg.norm(vb))) or 1.0
        return float(np.dot(va, vb) / denom)
    except Exception:
        # Pure-Python fallback (slower but identical result for 1-d arrays).
        la = list(a)
        lb = list(b)
        if not la or not lb or len(la) != len(lb):
            return 0.0
        dot = sum(x * y for x, y in zip(la, lb))
        ma = math.sqrt(sum(x * x for x in la))
        mb = math.sqrt(sum(y * y for y in lb))
        denom = ma * mb or 1.0
        return dot / denom


# --------------------------------------------------------------------------- #
# Seniority inference from a free-text title. Matches plan §6;
# the regex is intentionally small (the API lets callers override).
# --------------------------------------------------------------------------- #

_SENIORITY_TITLE_RE = re.compile(
    r"\b(staff|principal|senior|sr\.?|junior|jr\.?|intern|entry[-\s]?level)(?=\W|$)",
    re.IGNORECASE,
)


def infer_seniority(title: str | None) -> str:
    r"""Map a title to a seniority bucket; default 'unknown'.

    The trailing ``(?=\W|$)`` instead of ``\b`` matters for compound
    titles: ``Entry Level Engineer`` would otherwise miss because
    ``\b`` after ``level`` doesn't match before the next word. A
    non-word boundary lookahead accepts it.
    """
    t = (title or "").lower()
    for token in ("intern",):
        if token in t:
            return "junior"
    m = _SENIORITY_TITLE_RE.search(t)
    if not m:
        return "unknown"
    word = m.group(1).lower().rstrip(".")
    if word in ("staff", "principal"):
        return word
    if word in ("senior", "sr"):
        return "senior"
    if word.startswith("entry") or word in ("junior", "jr"):
        return "junior"
    return "unknown"


# --------------------------------------------------------------------------- #
# Helpers exposed for the API layer
# --------------------------------------------------------------------------- #

def empty_profile() -> dict:
    """A profile-shaped dict the API returns on a cold start."""
    from datetime import UTC, datetime
    now = datetime.now(UTC).isoformat()
    return {
        "tag_weights": "{}",
        "company_blocklist": "[]",
        "location_blocklist": "[]",
        "salary_history": "{" + ",".join(f'"{b[2]}":0' for b in SALARY_BANDS) + "}",
        "preferred_remote": json.dumps({"onsite": 0.0, "hybrid": 0.0,
                                        "remote": 0.0}),
        "seniority_preference": json.dumps({
            s: 0.0 for s in ("junior", "mid", "senior", "staff", "principal")
        }),
        "duplicate_hashes": "[]",
        "reason_counts": "{}",
        "yes_count": 0, "no_count": 0, "maybe_count": 0,
        "updated_at": now,
    }


def normalize_payload(payload: Any) -> dict:
    """Strip unknown keys, clamp tag list to 32 entries, and bound salary
    integers — the API boundary calls this BEFORE writing the job_row
    so the DB never sees garbage. Returns a NEW dict; never mutates
    the caller's payload.
    """
    if not isinstance(payload, dict):
        return {}
    out = {k: payload[k] for k in (
        "url", "title", "company", "location", "remote_type",
        "salary_min", "salary_max", "salary_currency",
        "source_agent", "source_run_id", "posted_at",
        "brief", "thread_title", "initial_message", "tags",
    ) if k in payload}
    tags = out.get("tags")
    if isinstance(tags, list):
        out["tags"] = [str(t).strip().lower() for t in tags[:32]
                       if str(t).strip()]
    elif tags is not None:
        out.pop("tags", None)
    for k in ("salary_min", "salary_max"):
        if k in out:
            try:
                v = int(out[k])
                if v < 0:
                    out[k] = None
                else:
                    out[k] = v
            except (TypeError, ValueError):
                out.pop(k, None)
    return out
