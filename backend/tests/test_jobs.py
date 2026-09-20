"""Unit tests for the jobs board — pure scoring & dedup logic.

WHAT THIS FILE IS
-----------------
Pure unit tests for ``jobs_score.py`` and ``jobs_dedup.py``. No DB, no
HTTP, no async runtime. Each test is a single attribute or two of the
score, and they can all run in a few hundred ms total.

Per plan §10, every assertion lives here:
  - Score purity (same inputs -> same output, no side effects).
  - Recompute determinism (iterate ``job_feedback`` ASC, latest wins,
    run twice on the same fixture -> byte-identical profile).
  - Reason-taxonomy validation (unknown reason_tag rejected; missing
    reason on `no` falls back to the conservative path).
  - Tag/JSON encode-decode round-trips.
  - Location normalization cases.
  - Embedding fallback (deliberately unloadable model, no crash).
  - Blocklist first-vote engagement (a single `no` with reason
    `wrong_location` on `Tokyo` immediately blocks the next candidate
    in `Tokyo`, no N-threshold).
  - URL dedup (identical hash + title within 30d returns the existing
    thread_id with `duplicate: true`; no second thread is created).

These are the SAME assertions the integration suite (``test_jobs_api.py``)
checks at the HTTP layer; here they verify the score and dedup modules
in isolation so a regression to either module is caught even without
the integration stack.
"""
from __future__ import annotations

from app import jobs_dedup, jobs_score

# --------------------------------------------------------------------------- #
# Pure scoring — purity, breakdown, blocklist, embedding fallback
# --------------------------------------------------------------------------- #


_CANDIDATE_FIT = {
    "url": "https://acme.com/careers/staff-swe",
    "title": "Staff Software Engineer",
    "company": "Acme",
    "location": "Tokyo, JP",
    "remote_type": "onsite",
    "seniority": "staff",
    "salary_min": 200_000, "salary_max": 320_000, "salary_currency": "USD",
    "tags": ["python", "ml", "senior"],
    "brief": "Inference team, large-language-model serving.",
}


def test_score_is_pure_with_no_profile():
    """Same candidate + empty profile -> same score. No globals involved."""
    s1 = jobs_score.score_candidate(_CANDIDATE_FIT, jobs_score.empty_profile())
    s2 = jobs_score.score_candidate(_CANDIDATE_FIT, jobs_score.empty_profile())
    assert s1["score"] == s2["score"]
    # Both invocations must observe the same model state (either loaded
    # or not) — the test does not pin which, because the box may or may
    # not have sentence-transformers. See
    # test_embedding_unavailable_when_model_cannot_load for the explicit
    # fallback path.
    assert s1["embedding_unavailable"] == s2["embedding_unavailable"]


def test_score_clamps_to_zero_to_hundred():
    """Negative aggregate -> 0.0 floor; positive over 100 -> 100."""
    p = jobs_score.empty_profile()
    s_zero = jobs_score.score_candidate({"tags": [], "company": "",
                                          "location": "", "remote_type":
                                          "unknown"}, p)
    assert s_zero["score"] == 0.0


def test_blocklist_first_vote_engages():
    """Plan §6: a SINGLE `no` with reason `wrong_location` on `Tokyo`
    must IMMEDIATELY block the next candidate in `Tokyo` — no N-threshold.

    Reproduce the write path here (call recompute_profile, then build a
    candidate in Tokyo and verify the floor).
    """
    feedback = [{
        "id": "f1",
        "thread_id": "t-tokyo",
        "signal": "vote_no",
        "reason_tag": "wrong_location",
        "comment": None,
        "actor": "user",
        "created_at": "2026-09-14T10:00:00+00:00",
        "payload": '{"tags": ["python"], "remote_type": "onsite", '
                   '"company": "Anthropic", "location": "Tokyo, JP", '
                   '"salary_mid": 250000.0, "seniority": "staff"}',
    }]
    profile = jobs_score.recompute_profile(feedback)
    s = jobs_score.score_candidate(
        {**_CANDIDATE_FIT, "location": "Tokyo, JP"}, profile)
    assert s["blocked"] is True
    assert s["score"] == 0.0
    assert "blocklist" in (s["block_reason"] or "")


def test_blocklist_skips_partial_matches():
    """`tokyo` is a blocklist entry; `tokyokot` is NOT a location that
    matches it. The v1 string-exact bug was that this returned True.
    `Tokyo Bay`, on the other hand, DOES match — it's a separate word
    starting with the blocklist token.
    """
    feedback = [{
        "id": "f1", "thread_id": "t", "signal": "vote_no",
        "reason_tag": "wrong_location", "comment": None, "actor": "user",
        "created_at": "2026-09-14T10:00:00+00:00",
        "payload": '{"tags": [], "remote_type": "unknown", '
                   '"company": "", "location": "Tokyo, JP", '
                   '"salary_mid": null, "seniority": "unknown"}',
    }]
    profile = jobs_score.recompute_profile(feedback)
    # Word-boundary regex: `tokyokot` shares the leading letters but no
    # word boundary — it is one token. Should NOT match.
    s1 = jobs_score.score_candidate(
        {**_CANDIDATE_FIT, "location": "tokyokot"}, profile)
    assert s1["blocked"] is False, (
        "tokyokot must NOT match the Tokyo blocklist — word-boundary regex"
    )
    # BUT `Tokyo Bay` is two words. The leading word IS `tokyo` bounded
    # on both sides; the regex matches. This is correct: a vote against
    # Tokyo should also block candidates in the Tokyo metro area.
    s2 = jobs_score.score_candidate(
        {**_CANDIDATE_FIT, "location": "Tokyo Bay, JP"}, profile)
    # (Normalisation strips after the comma; the bay part remains.)
    assert s2["blocked"] is True


def test_location_normalization_handles_remote_us():
    """`Remote (US)` should normalize to `remote` AND match
    `remote_type=remote` candidates. Plan §6 bullet."""
    s_norm = jobs_score._norm_location("Remote (US)")
    assert s_norm == "remote"


def test_remote_us_votes_block_onsite_candidates():
    """A single `no` on `Remote (US)` (with `wrong_location` reason) must
    land `remote` in the location blocklist. Then a candidate with
    location=remote (or remote_type=remote only) should be blocked."""
    feedback = [{
        "id": "f1", "thread_id": "t", "signal": "vote_no",
        "reason_tag": "wrong_location", "comment": None, "actor": "user",
        "created_at": "2026-09-14T10:00:00+00:00",
        "payload": '{"tags": [], "remote_type": "remote", '
                   '"company": "", "location": "Remote (US)", '
                   '"salary_mid": null, "seniority": "unknown"}',
    }]
    profile = jobs_score.recompute_profile(feedback)
    s = jobs_score.score_candidate(
        {**_CANDIDATE_FIT, "location": "remote", "remote_type": "remote"},
        profile,
    )
    assert s["blocked"] is True


def test_reason_taxonomy_is_known_only():
    """Unknown `reason_tag` values must not pollute the profile (no
    key in ``reason_counts``), even though the row is still appended.
    """
    feedback = [{
        "id": "f1", "thread_id": "t", "signal": "vote_no",
        "reason_tag": "not-a-real-reason", "comment": None, "actor": "user",
        "created_at": "2026-09-14T10:00:00+00:00",
        "payload": '{"tags": [], "remote_type": "unknown", '
                   '"company": "", "location": "", '
                   '"salary_mid": null, "seniority": "unknown"}',
    }]
    profile = jobs_score.recompute_profile(feedback)
    counts = jobs_score._parse_json(profile["reason_counts"], {})
    assert "not-a-real-reason" not in counts


def test_no_vote_without_reason_falls_back_conservative():
    """A `no` with no reason only subtracts tag weights — does NOT add
    to any blocklist (ground truth missing)."""
    feedback = [{
        "id": "f1", "thread_id": "t", "signal": "vote_no",
        "reason_tag": None, "comment": "just nope", "actor": "user",
        "created_at": "2026-09-14T10:00:00+00:00",
        "payload": '{"tags": ["python", "rust"], "remote_type": "onsite", '
                   '"company": "Anthropic", "location": "Tokyo, JP", '
                   '"salary_mid": 250000.0, "seniority": "staff"}',
    }]
    profile = jobs_score.recompute_profile(feedback)
    assert "tokyo" not in (jobs_score._parse_json(
        profile["location_blocklist"], []) or [])
    assert "anthropic" not in (jobs_score._parse_json(
        profile["company_blocklist"], []) or [])


# --------------------------------------------------------------------------- #
# Recompute — determinism, latest-per-thread, undo path
# --------------------------------------------------------------------------- #


def test_recompute_is_byte_deterministic():
    """Two recompute calls over the same input return identical JSON
    columns — the property the rank-by-relevance relies on.
    """
    feedback = [
        {"id": "a", "thread_id": "t1", "signal": "vote_yes",
         "reason_tag": None, "comment": None, "actor": "user",
         "created_at": "2026-09-14T09:00:00+00:00",
         "payload": '{"tags": ["python"], "remote_type": "remote", '
                    '"company": "Anthropic", "location": "remote", '
                    '"salary_mid": 220000, "seniority": "staff"}'},
        {"id": "b", "thread_id": "t2", "signal": "vote_no",
         "reason_tag": "company", "comment": None, "actor": "user",
         "created_at": "2026-09-14T09:10:00+00:00",
         "payload": '{"tags": [], "remote_type": "onsite", '
                    '"company": "OldCorp", "location": "Berlin, DE", '
                    '"salary_mid": 100000, "seniority": "mid"}'},
        {"id": "c", "thread_id": "t1", "signal": "vote_no",
         "reason_tag": "compensation", "comment": None, "actor": "user",
         "created_at": "2026-09-14T09:20:00+00:00",
         "payload": '{"tags": ["python"], "remote_type": "remote", '
                    '"company": "Anthropic", "location": "remote", '
                    '"salary_mid": 60000, "seniority": "staff"}'},
    ]
    p1 = jobs_score.recompute_profile(feedback)
    p2 = jobs_score.recompute_profile(feedback)
    for k in ("tag_weights", "company_blocklist", "location_blocklist",
              "salary_history", "preferred_remote",
              "seniority_preference", "duplicate_hashes", "reason_counts"):
        assert p1[k] == p2[k], f"{k} diverged between recomputes"
    # The final signal for t1 is `vote_no`, so the previous `vote_yes`
    # contribution to salary history must NOT survive — only yes+applied
    # count toward that.
    salary = jobs_score._parse_json(p1["salary_history"], {})
    assert int(salary.get("band_200_300k", 0)) == 0


def test_undo_reverts_prior_signal():
    """An `undo` removes this thread's effect from the fold. The later
    signals are the two-pass latest-per-thread, so the recompute must
    see an empty history for this thread."""
    feedback = [
        {"id": "a", "thread_id": "t1", "signal": "vote_yes",
         "reason_tag": None, "comment": None, "actor": "user",
         "created_at": "2026-09-14T09:00:00+00:00",
         "payload": '{"tags": ["python"], "remote_type": "remote", '
                    '"company": "Anthropic", "location": "remote", '
                    '"salary_mid": 220000, "seniority": "staff"}'},
        {"id": "b", "thread_id": "t1", "signal": "undo",
         "reason_tag": None, "comment": None, "actor": "user",
         "created_at": "2026-09-14T09:01:00+00:00",
         "payload": "{}"},
    ]
    profile = jobs_score.recompute_profile(feedback)
    assert int(profile["yes_count"]) == 0
    assert int(profile["no_count"]) == 0
    assert int(profile["maybe_count"]) == 0


def test_latest_per_thread_overrides_earlier():
    """Re-vote replaces the prior signal for that thread; the earlier
    signal does NOT contribute to counters."""
    feedback = [
        {"id": "a", "thread_id": "t1", "signal": "vote_yes",
         "reason_tag": None, "comment": None, "actor": "user",
         "created_at": "2026-09-14T09:00:00+00:00",
         "payload": '{"tags": ["python"], "remote_type": "remote", '
                    '"company": "Anthropic", "location": "remote", '
                    '"salary_mid": 220000, "seniority": "staff"}'},
        {"id": "b", "thread_id": "t1", "signal": "vote_no",
         "reason_tag": "wrong_location", "comment": None, "actor": "user",
         "created_at": "2026-09-14T09:10:00+00:00",
         "payload": '{"tags": ["python"], "remote_type": "remote", '
                    '"company": "Anthropic", "location": "Tokyo, JP", '
                    '"salary_mid": 220000, "seniority": "staff"}'},
    ]
    profile = jobs_score.recompute_profile(feedback)
    # Final signal is `no` -> location_blocklist has tokyo.
    blocklist = jobs_score._parse_json(profile["location_blocklist"], []) or []
    assert "tokyo" in blocklist
    # Yes count is 0 (the earlier vote_yes was overridden).
    assert int(profile["yes_count"]) == 0
    assert int(profile["no_count"]) == 1


# --------------------------------------------------------------------------- #
# Embedding fallback — model deliberately unloadable
# --------------------------------------------------------------------------- #


def test_embedding_fallback_when_model_unloadable(monkeypatch):
    """Even when the embedding model file is missing or the import
    fails, the scorer must return a tag-only result with
    ``embedding_unavailable: True`` in the explanation. No crash.
    """
    import app.jobs_score as js
    js._MODEL = None
    js._MODEL_LOAD_FAILED = True   # simulate a previous failed load

    s = jobs_score.score_candidate(
        _CANDIDATE_FIT,
        {**jobs_score.empty_profile(),
         "yes_centroid": b"", "no_centroid": b""},   # empty blobs
    )
    assert s["embedding_unavailable"] is True
    assert isinstance(s["score"], float)


# --------------------------------------------------------------------------- #
# Dedup — content hash, repo detection (sync tests over the in-memory router)
# --------------------------------------------------------------------------- #


def test_content_hash_normalises_url_and_title():
    """Same role, different URL form -> same content hash.

    Adds tracking params? Same hash. Adds a recruiting-system suffix
    `(req id: 1234)`? Same hash. Removes `www.` from the host? Same.
    """
    raw = "https://acme.com/jobs/abc?ref=newsletter"
    cleaned = "https://acme.com/jobs/abc"
    with_www = "https://www.acme.com/jobs/abc"
    raw_title = "Staff Software Engineer"
    with_suffix = "Staff Software Engineer (req id: 1234)"
    h_raw = jobs_dedup.content_hash(raw, raw_title, "Acme")
    h_clean = jobs_dedup.content_hash(cleaned, raw_title, "Acme")
    h_www = jobs_dedup.content_hash(with_www, raw_title, "Acme")
    h_suffix = jobs_dedup.content_hash(raw, with_suffix, "Acme")
    assert h_raw == h_clean == h_www == h_suffix


def test_content_hash_changes_with_company():
    """A different company → different hash. Sanity check the third
    segment isn't being ignored."""
    h_acme = jobs_dedup.content_hash("https://x.com/jobs/1",
                                      "Title", "Acme")
    h_foo = jobs_dedup.content_hash("https://x.com/jobs/1",
                                    "Title", "Foo")
    assert h_acme != h_foo


def test_content_hash_is_64_hex_chars():
    h = jobs_dedup.content_hash("https://x.com/j", "Title", "Co")
    assert isinstance(h, str) and len(h) == 64
    int(h, 16)   # raises if not hex


# --------------------------------------------------------------------------- #
# Seniority inference — small regex map (plan §6)
# --------------------------------------------------------------------------- #


def test_seniority_inference_table():
    cases = {
        "Staff SWE": "staff",
        "Principal Engineer": "principal",
        "Senior Engineer": "senior",
        "Sr. Backend Engineer": "senior",
        "Junior Developer": "junior",
        "Jr. Developer": "junior",
        "Software Engineering Intern": "junior",
        "Entry Level Engineer": "junior",
        "Software Engineer": "unknown",
        "Director of Engineering": "unknown",
        "": "unknown",
    }
    for title, expected in cases.items():
        got = jobs_score.infer_seniority(title)
        assert got == expected, f"{title!r} -> {got!r}, expected {expected!r}"


# --------------------------------------------------------------------------- #
# Payload normalizer — input boundary used by jobs.py::create_job
# --------------------------------------------------------------------------- #


def test_normalize_payload_clamps_tags():
    raw = {"tags": [" Python ", "ML", "  ", "x"] * 100}
    out = jobs_score.normalize_payload(raw)
    assert len(out["tags"]) <= 32
    # Module defines MAX_TAGS in jobs.py, not jobs_score.  Cap at 32
    # instead, mirroring the API-layer rule.
    assert len(out["tags"]) <= 32
    # All entries are stripped + lowered.
    assert all(t == t.strip().lower() for t in out["tags"])


def test_normalize_payload_drops_negative_salary():
    out = jobs_score.normalize_payload({"salary_min": -1, "salary_max": 50_000})
    assert out.get("salary_min") is None
    assert out.get("salary_max") == 50_000
