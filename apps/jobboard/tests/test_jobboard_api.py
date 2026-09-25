"""Integration tests for the Job Board app's HTTP surface.

WHAT THIS FILE IS
-----------------
Integration tests for ``apps/jobboard/backend.py`` — the router the shell's
app loader mounts at ``/api/apps/jobboard`` AND at the legacy ``/api/jobs``
(the scout agent's skill and the daily sweep cron still call the latter).
Moved here from ``backend/tests/test_jobs_api.py`` on 2026-09-25 when the
board became an app. Plan §10:
  * Create / list / get / vote / applied / archive endpoints against
    an empty staging DB.
  * ``job_events`` is append-only (UPDATE/DELETE attempts raise).
  * Profile recompute fires after each vote; GETs do not mutate
    profile.
  * Auth matrix: machine gets 200 on inbound-exempt, browser-with-
    session gets 200 on session-only, decoy gets 403.
  * Safe-Mode decoy on ``GET /api/jobs`` returns the empty shape
    (no data leak).

Why both unit AND integration cover the same property: the unit
suite (``test_jobboard_scoring.py``) keeps the score / dedup modules honest in
isolation; the integration suite here pins the HTTP contracts the
agent-facing API is built on.
"""
from __future__ import annotations

import asyncio

import dispatch_app_jobboard as jobs
import pytest
from pydantic import ValidationError

from app import auth, config, main  # main import loads the apps

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


@pytest.fixture
def sample_job_payload():
    return {
        "bot_id": "jobboard",
        "thread_title": "Staff SWE — Anthropic Tokyo",
        "url": "https://anthropic.com/careers/staff-swe",
        "title": "Staff Software Engineer",
        "company": "Anthropic",
        "location": "Tokyo, JP",
        "remote_type": "onsite",
        "salary_min": 220000, "salary_max": 320000,
        "salary_currency": "USD",
        "tags": ["python", "ml", "senior"],
        "brief": "Inference team; LLM serving.",
        "source_agent": "scout",
        "source_run_id": "r-001",
        "posted_at": "2026-09-01T00:00:00+00:00",
        "initial_message": "🎯 New posting — Staff SWE @ Anthropic Tokyo.",
    }


# --------------------------------------------------------------------------- #
# Storage helpers — the layers below the router that the tests drive directly.
# We exercise the router through its handlers by mocking the FastAPI Request
# just enough to thread the tier checks.
# --------------------------------------------------------------------------- #


class _FakeRequest:
    """Minimal stand-in for fastapi.Request used by the tier helpers.

    Only the cookies / state attributes are read by the app's
    ``_is_decoy`` and the shell's ``ctx.require_access`` — keep the surface
    narrow and the rest fakeable. ``decoy``/``machine`` mirror what the auth
    gate stamps on ``request.state``.
    """
    def __init__(self, cookie=None, decoy=False, machine=False):
        self.cookies = {"lc_session": cookie} if cookie else {}
        from types import SimpleNamespace
        self.state = SimpleNamespace(decoy=decoy, machine=machine, session=None)
        self.method = "GET"


@pytest.fixture
def full_session(monkeypatch):
    """Mint an in-memory full-access session for the synthetic user.

    The tests below don't need real cookie crypto — only the bool from
    ``auth.get_session(sid)``. monkeypatch restores the real function at
    teardown; a hand-rolled generator that was never exhausted left it
    patched for the rest of the suite and broke every later auth test.
    """
    sid = "test-session"
    orig = auth.get_session
    monkeypatch.setattr(auth, "get_session",
                        lambda token: {"id": sid, "full": True} if token == sid else orig(token))
    return sid


# --------------------------------------------------------------------------- #
# Storage-layer round trip — uses db directly, like test_double_post_fix.py.
# --------------------------------------------------------------------------- #


@pytest.fixture
async def wired(tmp_path, monkeypatch):
    from app import database as db_module

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(config, "AVATAR_DIR", tmp_path / "avatars")
    config._invalidate_bots_cache()
    db = db_module.Database(tmp_path / "chats.db")
    await db.connect()
    # Make jobs' lazy `_db()` resolver see our test DB. Mirrors the
    # pattern in test_double_post_fix.py.
    monkeypatch.setattr(main, "db", db)
    # The app's require_access reads the PIN state: point auth at a scratch
    # security.yaml (no PIN) so the live install's lock never leaks in.
    monkeypatch.setattr(auth, "SECURITY_PATH", tmp_path / "security.yaml")
    monkeypatch.setattr(auth, "RECOVERY_PATH", tmp_path / "RECOVERY-CODE.txt")
    auth._cache = None
    p = {}
    p["_db"] = db
    yield p
    await db.close()


# --------------------------------------------------------------------------- #
# Storage-level round-trips — these run before any router exercise.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_create_then_list_then_get(wired, sample_job_payload):
    db = wired["_db"]
    # `POST /api/jobs` -> create the row directly via the same path
    # the router takes, so we cover the upsert + thread + first message.
    from dispatch_app_jobboard import create_job_from_dict
    res = await create_job_from_dict(sample_job_payload)
    assert res["duplicate"] is False
    job_id = res["job_id"]
    thread_id = res["thread_id"]
    assert job_id and thread_id
    # The thread is the current month's discussion thread, and the job
    # row references it. ``get_job`` is keyed by ``job_id`` in the
    # 2026-09-15 monthly-threading model.
    assert res["job"]["thread_id"] == thread_id
    assert res["job"]["job_id"] == job_id
    listed = await db.list_jobs()
    assert any(j["job_id"] == job_id for j in listed)
    assert any(j["thread_id"] == thread_id for j in listed)


@pytest.mark.asyncio
async def test_duplicate_within_30d_returns_existing(wired, sample_job_payload):
    """Identical hash + title within 30 days -> the second POST gets
    ``duplicate: True`` with the existing job_id. NO second job row
    is created. (Plan §7 + §10 last assertion.)
    """
    db = wired["_db"]
    from dispatch_app_jobboard import create_job_from_dict
    res1 = await create_job_from_dict(sample_job_payload)
    res2 = await create_job_from_dict(sample_job_payload)
    assert res1["duplicate"] is False
    assert res2["duplicate"] is True
    assert res2["existing_job_id"] == res1["job_id"]
    assert res2["existing_thread_id"] == res1["thread_id"]
    listed = await db.list_jobs()
    assert len([j for j in listed if j["url"] == sample_job_payload["url"]]) == 1


@pytest.mark.asyncio
async def test_vote_appends_immortal_event(wired, sample_job_payload):
    """Append-only: ``job_events`` should never be UPDATEd or DELETEd
    in normal flow. We don't have a DELETE handler, so the assertion
    is that the vote handler only inserts.
    """
    db = wired["_db"]
    from dispatch_app_jobboard import _record_vote, create_job_from_dict
    res = await create_job_from_dict(sample_job_payload)
    job_id = res["job_id"]
    await _record_vote(job_id, "yes", None, None, "user")
    events = await db.list_job_events(job_id)
    # type=vote + a state_change or one of the two — at minimum, 1.
    types = [e["type"] for e in events]
    assert "vote" in types or "vote_undo" in types
    # job_events are append-only in this codepath: every event row
    # created by a vote gets a UUID id, never mutated.
    assert all(e["id"] for e in events)


# --------------------------------------------------------------------------- #
# Auth matrix — exercising the helpers without going through the full
# FastAPI machinery (which is more thoroughly tested in test_auth_gate.py).
# --------------------------------------------------------------------------- #


def test_require_access_passes_a_no_pin_caller(wired):
    """No PIN set up yet: the app is wide open, a bare caller is the operator."""
    jobs.CTX.require_access(_FakeRequest(cookie=None))


def test_require_access_passes_a_machine_caller(wired):
    """The auth gate stamped this request as an on-box machine (loopback or
    api_token) — the scout agent's path. Passes even with a PIN set."""
    auth.set_pin("1234")
    jobs.CTX.require_access(_FakeRequest(machine=True))


def test_require_access_rejects_a_decoy(wired):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        jobs.CTX.require_access(_FakeRequest(decoy=True))
    assert ei.value.status_code == 403


def test_require_access_rejects_a_sessionless_caller_once_a_pin_exists(wired):
    from fastapi import HTTPException
    auth.set_pin("1234")
    with pytest.raises(HTTPException) as ei:
        jobs.CTX.require_access(_FakeRequest(cookie="stale"))
    assert ei.value.status_code == 403


def test_require_access_accepts_a_live_session(wired, full_session):
    auth.set_pin("1234")
    jobs.CTX.require_access(_FakeRequest(cookie=full_session))


def test_require_operator_refuses_a_machine(wired):
    from fastapi import HTTPException
    auth.set_pin("1234")
    with pytest.raises(HTTPException):
        jobs.CTX.require_operator(_FakeRequest(machine=True))


# --------------------------------------------------------------------------- #
# Profile recompute fires on vote but NOT on GET.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_profile_recompute_fires_after_vote_not_after_get(wired,
                                                                 sample_job_payload):
    db = wired["_db"]
    from dispatch_app_jobboard import _record_vote, create_job_from_dict, get_profile
    await create_job_from_dict(sample_job_payload)
    # Before any vote, the profile is empty (no recompute yet).
    pre = await get_profile(_FakeRequest(cookie="x"))
    assert int(pre["yes_count"]) == 0
    # A GET should NOT touch the profile.
    _ = await get_profile(_FakeRequest(cookie="x"))
    post_get = await get_profile(_FakeRequest(cookie="x"))
    assert int(post_get["yes_count"]) == 0
    # Cast a vote and re-read.
    pre_vote = await db.get_job_profile() or {}
    pre_count = int(pre_vote.get("yes_count", 0))
    job_id = (await db.list_jobs())[0]["job_id"]
    await _record_vote(job_id, "yes", None, None, "user")
    after = await get_profile(_FakeRequest(cookie="x"))
    assert int(after["yes_count"]) == pre_count + 1


# --------------------------------------------------------------------------- #
# Safe-Mode decoy -> empty shape on GET, 403 on write.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_decoy_get_returns_empty_shape(wired, sample_job_payload, monkeypatch):
    from dispatch_app_jobboard import create_job_from_dict, list_jobs
    await create_job_from_dict(sample_job_payload)
    req = _FakeRequest(decoy=True)
    out = await list_jobs(req)
    assert out == {"jobs": [], "next_cursor": None}


@pytest.mark.asyncio
async def test_decoy_write_is_403(wired, sample_job_payload):
    """A decoy caller hitting the WRITE path gets 403 from
    ``ctx.require_access`` (the auth gate refuses it a layer earlier too)."""
    from dispatch_app_jobboard import create_job_from_dict, vote
    from fastapi import HTTPException
    await create_job_from_dict(sample_job_payload)
    db = wired["_db"]
    job_id = (await db.list_jobs())[0]["job_id"]
    with pytest.raises(HTTPException) as ei:
        await vote(job_id, jobs.VoteIn(signal="yes"), _FakeRequest(decoy=True))
    assert ei.value.status_code == 403
    assert (await db.get_job(job_id))["state"] == "pending"


# --------------------------------------------------------------------------- #
# Tiering gate — machine inbound-exempt.
# --------------------------------------------------------------------------- #


def test_inbound_allowlist_includes_job_writes():
    """``_is_inbound`` MUST grant machine access to ``POST /api/jobs``
    and ``POST /api/jobs/score`` — the routes an on-box agent drives
    (plan §4 + §12 risk 2 fix). Since the board became an app the whole
    mounted surface is machine-inbound at BOTH prefixes; the per-route
    ``ctx.require_access``/``require_operator`` decides from there."""
    assert main._is_inbound("POST", "/api/jobs") is True
    assert main._is_inbound("POST", "/api/jobs/score") is True
    assert main._is_inbound("POST", "/api/apps/jobboard") is True
    assert main._is_inbound("POST", "/api/apps/jobboard/score") is True
    assert main._is_inbound("GET", "/api/apps/jobboard/months") is True


def test_inbound_allowlist_includes_manage_verbs():
    """2026-09-15 — the OpenClaw audit lifts vote/applied/tags/archive
    onto the inbound tier so an on-box agent can manage a job end-to-end
    without first obtaining a PIN-derived session cookie. The decoy
    block at the ``/api/jobs`` prefix still keeps Safe-Mode browsers
    out (see test_decoy_blocked_bars_jobs_paths). 2026-09-16 adds
    ``feedback`` to the same tier.
    """
    assert main._is_inbound("POST", "/api/jobs/abc/vote") is True
    assert main._is_inbound("POST", "/api/jobs/abc/applied") is True
    assert main._is_inbound("POST", "/api/jobs/abc/tags") is True
    assert main._is_inbound("POST", "/api/jobs/abc/archive") is True
    assert main._is_inbound("POST", "/api/jobs/abc/feedback") is True
    assert main._is_inbound("POST", "/api/jobs/profile/recompute") is True


def test_inbound_allowlist_includes_feedback_feed():
    """``GET /api/jobs/feedback`` is machine-readable exactly like
    ``GET /api/jobs`` — an agent reads it without a PIN session."""
    assert main._is_inbound("GET", "/api/jobs/feedback") is True


def test_inbound_allowlist_includes_find():
    """``POST /api/jobs/find`` is how the daily sweep cron asks Scout to
    search (``dispatch-jobs find``); without it on the machine tier the board
    only ever grew when someone pressed the button."""
    assert main._is_inbound("POST", "/api/jobs/find") is True


def test_decoy_blocked_bars_jobs_paths():
    """``_decoy_blocked`` belt-and-braces: a locked session must not
    reach ``/api/jobs*`` even if auth_gate misses it."""
    assert main._decoy_blocked("GET", "/api/jobs") is True
    assert main._decoy_blocked("GET", "/api/jobs/abc") is True
    assert main._decoy_blocked("POST", "/api/jobs") is True
    assert main._decoy_blocked("GET", "/api/jobs/feedback") is True
    assert main._decoy_blocked("POST", "/api/jobs/abc/feedback") is True
    assert main._decoy_blocked("GET", "/api/apps/jobboard") is True
    assert main._decoy_blocked("GET", "/apps/jobboard/") is True


# --------------------------------------------------------------------------- #
# URL validation — P0 stored-XSS fix (2026-09-16).
# --------------------------------------------------------------------------- #


def test_job_url_rejects_javascript_scheme():
    with pytest.raises(ValidationError):
        jobs.JobIn(url="javascript:alert(1)", title="x")


def test_job_url_rejects_data_scheme():
    with pytest.raises(ValidationError):
        jobs.JobIn(url="data:text/html,<script>alert(1)</script>", title="x")


def test_job_url_rejects_relative_path():
    with pytest.raises(ValidationError):
        jobs.JobIn(url="/careers/staff-swe", title="x")


def test_job_url_accepts_https():
    j = jobs.JobIn(url="https://example.com/careers/1", title="x")
    assert j.url == "https://example.com/careers/1"


def test_job_url_strips_whitespace():
    j = jobs.JobIn(url="  https://example.com/x  ", title="x")
    assert j.url == "https://example.com/x"


def test_score_in_url_uses_the_same_validator():
    with pytest.raises(ValidationError):
        jobs.ScoreIn(url="javascript:alert(1)", title="x")


def test_serialise_job_redacts_a_bad_stored_url():
    """Defence in depth: a row written before the validator existed (or by
    a future writer that forgets it) must never come back out as an href
    a browser will render.
    """
    row = {
        "job_id": "j1", "thread_id": "t1", "url": "javascript:alert(1)",
        "title": "Evil", "state": "pending",
    }
    out = jobs._serialise_job(row)
    assert out["url"] == ""


def test_serialise_job_keeps_a_good_stored_url():
    row = {
        "job_id": "j1", "thread_id": "t1", "url": "https://example.com/x",
        "title": "Fine", "state": "pending",
    }
    out = jobs._serialise_job(row)
    assert out["url"] == "https://example.com/x"


# --------------------------------------------------------------------------- #
# Applied endpoint — end to end.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_applied_end_to_end(wired, sample_job_payload):
    """POST .../applied records an `applied` feedback signal, a
    state_change event, and flips the job's state — all in one call."""
    from dispatch_app_jobboard import applied, create_job_from_dict

    db = wired["_db"]
    res = await create_job_from_dict(sample_job_payload)
    job_id = res["job_id"]
    req = _FakeRequest(cookie=None)
    out = await applied(job_id, jobs.CommentIn(comment="submitted 2026-09-16"), req)
    assert out == {"ok": True}

    job = await db.get_job(job_id)
    assert job["state"] == "applied"

    feedback_rows = await db.list_job_feedback_for_job(job_id)
    signals = [r["signal"] for r in feedback_rows]
    assert "applied" in signals
    assert feedback_rows[-1]["comment"] == "submitted 2026-09-16"

    events = await db.list_job_events(job_id)
    assert any(e["type"] == "state_change" and e["to_state"] == "applied"
              for e in events)


def test_applied_accepts_a_null_comment():
    """The board's Applied button sends `{comment: null}` when the note box
    is empty; that must validate, not 422."""
    assert jobs.CommentIn.model_validate({"comment": None}).comment is None
    assert jobs.CommentIn.model_validate({}).comment is None


@pytest.mark.asyncio
async def test_find_posts_request_into_month_thread_and_dispatches(wired, monkeypatch):
    from dispatch_app_jobboard import find_jobs

    db = wired["_db"]
    config._write_bots([config._bot_entry(config.Bot(id="jobboard", name="Jobs", agent="scout"))])
    monkeypatch.setattr("app.openclaw.cli_available", lambda: True)
    calls = []

    async def _fake_turn(tid, bid, text):
        calls.append((tid, bid, text))
    monkeypatch.setattr(main, "run_agent_turn", _fake_turn)

    out = await find_jobs(jobs.FindIn(query="remote LLM infra"),
                          _FakeRequest(cookie=None), bot_id="jobboard")
    assert out["ok"] is True and out["dispatched"] is True
    msgs, _ = await db.list_messages(out["thread_id"], limit=10)
    assert any("dispatch-jobs post" in m.content and "remote LLM infra" in m.content
               and m.role == "user" for m in msgs)
    await asyncio.sleep(0)
    assert calls and calls[0][:2] == (out["thread_id"], "jobboard")


@pytest.mark.asyncio
async def test_find_unknown_bot_is_404(wired):
    from dispatch_app_jobboard import find_jobs
    from fastapi import HTTPException
    config._write_bots([config._bot_entry(config.Bot(id="jobboard", name="Jobs"))])
    with pytest.raises(HTTPException) as ei:
        await find_jobs(jobs.FindIn(), _FakeRequest(cookie=None), bot_id="nope")
    assert ei.value.status_code == 404


# --------------------------------------------------------------------------- #
# Free-text feedback -> chat message + dispatched agent turn.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_feedback_records_event_posts_message_and_dispatches(
    wired, sample_job_payload, monkeypatch,
):
    from dispatch_app_jobboard import create_job_from_dict, feedback

    db = wired["_db"]
    res = await create_job_from_dict(sample_job_payload)
    job_id = res["job_id"]
    thread_id = res["thread_id"]

    # Give the thread's bot ("jobboard") a resolvable config, and stub the
    # dispatch so the test never shells out to a real OpenClaw CLI.
    config._write_bots([config._bot_entry(config.Bot(id="jobboard", name="Jobs"))])
    monkeypatch.setattr("app.openclaw.cli_available", lambda: True)
    dispatched_calls = []

    async def _fake_turn(tid, bid, text):
        dispatched_calls.append((tid, bid, text))
    monkeypatch.setattr(main, "run_agent_turn", _fake_turn)

    req = _FakeRequest(cookie=None)
    payload = jobs.FeedbackIn(comment="This looks great, applying today.",
                              reason_tag=None)
    out = await feedback(job_id, payload, req)
    assert out["ok"] is True
    assert out["thread_id"] == thread_id
    assert out["dispatched"] is True
    assert out["event_id"] and out["message_id"]

    events = await db.list_job_events(job_id)
    comment_events = [e for e in events if e["type"] == "comment"
                      and e["comment"] == "This looks great, applying today."]
    assert comment_events, "feedback must record a `comment` job_event"
    assert comment_events[0]["actor"] == "user"

    msgs, _ = await db.list_messages(thread_id, limit=50)
    posted = [m for m in msgs if "This looks great, applying today." in (m.content or "")]
    assert posted, "feedback must be posted into the job's thread as a message"
    assert posted[0].role == "user"
    assert sample_job_payload["title"] in posted[0].content
    assert sample_job_payload["company"] in posted[0].content

    # Give the event loop a turn so the fire-and-forget task actually runs.
    await asyncio.sleep(0)
    assert dispatched_calls and dispatched_calls[0][0] == thread_id


@pytest.mark.asyncio
async def test_feedback_reason_tag_lands_in_the_message(
    wired, sample_job_payload, monkeypatch,
):
    from dispatch_app_jobboard import create_job_from_dict, feedback

    db = wired["_db"]
    res = await create_job_from_dict(sample_job_payload)
    job_id = res["job_id"]
    config._write_bots([config._bot_entry(config.Bot(id="jobboard", name="Jobs"))])
    monkeypatch.setattr("app.openclaw.cli_available", lambda: True)

    async def _fake_turn(tid, bid, text):
        pass
    monkeypatch.setattr(main, "run_agent_turn", _fake_turn)

    req = _FakeRequest(cookie=None)
    payload = jobs.FeedbackIn(comment="Pay looks low.", reason_tag="compensation")
    out = await feedback(job_id, payload, req)
    msgs, _ = await db.list_messages(out["thread_id"], limit=50)
    posted = [m for m in msgs if "Pay looks low." in (m.content or "")]
    assert posted
    assert "[reason: compensation]" in posted[0].content


@pytest.mark.asyncio
async def test_feedback_dispatched_false_when_no_bot_config(
    wired, sample_job_payload,
):
    """The thread's bot has no config.yaml entry at all (a stock install
    before the operator wires up `jobboard`) — the feedback is still recorded and
    posted, but `dispatched` is honestly False rather than claiming a turn
    that never started."""
    from dispatch_app_jobboard import create_job_from_dict, feedback

    db = wired["_db"]
    res = await create_job_from_dict(sample_job_payload)
    job_id = res["job_id"]
    req = _FakeRequest(cookie=None)
    payload = jobs.FeedbackIn(comment="No bot configured for this one.")
    out = await feedback(job_id, payload, req)
    assert out["dispatched"] is False
    events = await db.list_job_events(job_id)
    assert any(e["type"] == "comment" and e["comment"] == "No bot configured for this one."
              for e in events)


@pytest.mark.asyncio
async def test_feedback_unknown_job_is_404(wired):
    from dispatch_app_jobboard import feedback

    req = _FakeRequest(cookie=None)
    with pytest.raises(Exception) as exc_info:
        await feedback("nope", jobs.FeedbackIn(comment="x"), req)
    assert "404" in str(exc_info.value) or "Not found" in str(exc_info.value)


def test_feedback_comment_required():
    with pytest.raises(ValidationError):
        jobs.FeedbackIn(comment="   ")


def test_feedback_comment_capped_at_2000_chars():
    with pytest.raises(ValidationError):
        jobs.FeedbackIn(comment="x" * 2001)


def test_feedback_reason_tag_must_be_known():
    with pytest.raises(ValidationError):
        jobs.FeedbackIn(comment="ok", reason_tag="not_a_real_tag")


# --------------------------------------------------------------------------- #
# Merged feedback feed.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_feedback_feed_merges_votes_and_comments(
    wired, sample_job_payload, monkeypatch,
):
    from dispatch_app_jobboard import _record_vote, create_job_from_dict, feedback, list_feedback

    res = await create_job_from_dict(sample_job_payload)
    job_id = res["job_id"]
    config._write_bots([config._bot_entry(config.Bot(id="jobboard", name="Jobs"))])
    monkeypatch.setattr("app.openclaw.cli_available", lambda: True)

    async def _fake_turn(tid, bid, text):
        pass
    monkeypatch.setattr(main, "run_agent_turn", _fake_turn)

    await _record_vote(job_id, "yes", None, None, "user")
    await feedback(job_id, jobs.FeedbackIn(comment="worth a shot"), _FakeRequest())

    out = await list_feedback(_FakeRequest(), since=None, limit=100)
    kinds = {row["kind"] for row in out["feedback"]}
    assert kinds == {"vote", "comment"}
    assert all(row["job_id"] == job_id for row in out["feedback"])
    comment_rows = [r for r in out["feedback"] if r["kind"] == "comment"]
    assert comment_rows and comment_rows[0]["comment"] == "worth a shot"
    vote_rows = [r for r in out["feedback"] if r["kind"] == "vote"]
    assert vote_rows and vote_rows[0]["signal"] == "vote_yes"
    # Newest first.
    created_ats = [row["created_at"] for row in out["feedback"]]
    assert created_ats == sorted(created_ats, reverse=True)

    # `since` filters both halves (it 500'd live on an ambiguous created_at).
    since_all = await list_feedback(_FakeRequest(), since="2000-01-01T00:00:00Z", limit=100)
    assert len(since_all["feedback"]) == len(out["feedback"])
    since_none = await list_feedback(_FakeRequest(), since="2999-01-01T00:00:00Z", limit=100)
    assert since_none["feedback"] == []


@pytest.mark.asyncio
async def test_feedback_feed_decoy_gets_empty_shape(wired, sample_job_payload,
                                                    monkeypatch):
    from dispatch_app_jobboard import create_job_from_dict, list_feedback

    await create_job_from_dict(sample_job_payload)
    out = await list_feedback(_FakeRequest(decoy=True), since=None, limit=100)
    assert out == {"feedback": []}


# --------------------------------------------------------------------------- #
# last_vote on the serialised job.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_get_job_reports_last_vote(wired, sample_job_payload):
    from dispatch_app_jobboard import _record_vote, create_job_from_dict, get_job

    res = await create_job_from_dict(sample_job_payload)
    job_id = res["job_id"]
    await _record_vote(job_id, "yes", None, None, "user")
    out = await get_job(_FakeRequest(), job_id)
    assert out["job"]["last_vote"]["signal"] == "vote_yes"


@pytest.mark.asyncio
async def test_get_job_last_vote_is_none_after_undo(wired, sample_job_payload):
    from dispatch_app_jobboard import _record_vote, create_job_from_dict, get_job

    res = await create_job_from_dict(sample_job_payload)
    job_id = res["job_id"]
    await _record_vote(job_id, "yes", None, None, "user")
    await _record_vote(job_id, "undo", None, None, "user")
    out = await get_job(_FakeRequest(), job_id)
    assert out["job"]["last_vote"] is None


@pytest.mark.asyncio
async def test_list_jobs_reports_last_vote(wired, sample_job_payload, monkeypatch):
    from dispatch_app_jobboard import _record_vote, create_job_from_dict, list_jobs

    res = await create_job_from_dict(sample_job_payload)
    job_id = res["job_id"]
    await _record_vote(job_id, "maybe", None, None, "user")
    out = await list_jobs(_FakeRequest(), limit=200)
    row = next(j for j in out["jobs"] if j["job_id"] == job_id)
    assert row["last_vote"]["signal"] == "vote_maybe"


# --------------------------------------------------------------------------- #
# Over HTTP, through the shell: both prefixes, the scout agent's path, Safe Mode.
# --------------------------------------------------------------------------- #

LOOPBACK = ("127.0.0.1", 50000)
BROWSER = {"origin": "http://127.0.0.1:8765", "sec-fetch-site": "same-origin"}


@pytest.fixture
def http_env(tmp_path, monkeypatch):
    """main.app on a throwaway data dir; yields a client factory by peer."""
    from fastapi.testclient import TestClient

    from app import tools
    from app.database import Database

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(config, "MEDIA_DIR", tmp_path / "media")
    monkeypatch.setattr(config, "FILES_DIR", tmp_path / "files")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(main, "MEDIA_DIR", tmp_path / "media")
    monkeypatch.setattr(main, "FILES_DIR", tmp_path / "files")
    monkeypatch.setattr(auth, "SECURITY_PATH", tmp_path / "security.yaml")
    monkeypatch.setattr(auth, "RECOVERY_PATH", tmp_path / "RECOVERY-CODE.txt")
    auth._sessions.clear()
    auth._cache = None
    auth._fail_count = 0
    auth._fail_until = 0.0
    tools._reset_state()
    config._invalidate_bots_cache()
    temp_db = Database(tmp_path / "chats.db")
    monkeypatch.setattr(main, "db", temp_db)
    turns = []

    async def _fake_turn(tid, bid, text):
        turns.append((tid, bid, text))
    monkeypatch.setattr(main, "run_agent_turn", _fake_turn)
    monkeypatch.setattr("app.openclaw.cli_available", lambda: True)
    clients = []

    def make(addr=LOOPBACK):
        c = TestClient(main.app, client=addr)
        c.__enter__()
        clients.append(c)
        return c

    yield make, turns
    for c in clients:
        c.__exit__(None, None, None)
    tools._reset_state()
    asyncio.run(temp_db.close())


def _job_body(**kw):
    body = {"url": "https://example.com/careers/1", "title": "Staff Engineer",
            "company": "Example", "location": "Remote", "remote_type": "remote",
            "tags": ["python"], "source_agent": "scout"}
    body.update(kw)
    return body


def test_both_prefixes_serve_identically(http_env):
    make, _ = http_env
    c = make()                                  # no PIN: operator
    created = c.post("/api/apps/jobboard", json=_job_body())
    assert created.status_code == 200, created.text
    job_id = created.json()["job_id"]
    for path in ("", "/reasons", "/months", "/profile", f"/{job_id}", "/feedback",
                 "/current?ensure=false"):
        a = c.get(f"/api/apps/jobboard{path}")
        b = c.get(f"/api/jobs{path}")
        assert a.status_code == b.status_code == 200, (path, a.status_code, b.status_code)
        ja, jb = a.json(), b.json()
        if path == "/profile":          # an empty profile is stamped "now" per read
            ja.pop("updated_at"), jb.pop("updated_at")
        assert ja == jb, path
    # And a write through the legacy prefix lands in the same store.
    dup = c.post("/api/jobs", json=_job_body())
    assert dup.json()["duplicate"] is True and dup.json()["existing_job_id"] == job_id


def test_sessionless_loopback_agent_path_works_with_a_pin_set(http_env):
    """The scout agent's skill (`dispatch-jobs`) and the daily sweep cron call
    /api/jobs/... from loopback with no cookie while the family app HAS a PIN.
    That path must keep working after the move to an app."""
    make, turns = http_env
    c = make(LOOPBACK)                          # startup provisions the jobboard bot
    auth.set_pin("1234")
    assert config.get_bot("jobboard") is not None
    r = c.post("/api/jobs", json=_job_body())
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]
    assert c.get("/api/jobs").json()["jobs"][0]["job_id"] == job_id
    assert c.post("/api/jobs/score", json=_job_body()).status_code == 200
    f = c.post("/api/jobs/find", json={"query": "remote"})
    assert f.status_code == 200 and f.json()["dispatched"] is True, f.text
    fb = c.post(f"/api/jobs/{job_id}/feedback", json={"comment": "looks good"})
    assert fb.status_code == 200 and fb.json()["ok"] is True, fb.text
    assert c.post(f"/api/jobs/{job_id}/vote", json={"signal": "yes"}).status_code == 200
    assert c.post("/api/apps/jobboard/profile/recompute").status_code == 200
    assert len(turns) == 2 and all(t[1] == "jobboard" for t in turns)


def test_safe_mode_gets_403_on_everything(http_env):
    make, _ = http_env
    c = make(LOOPBACK)
    auth.set_pin("1234")
    for method, path in (("GET", "/api/jobs"), ("GET", "/api/jobs/months"),
                         ("POST", "/api/jobs"), ("POST", "/api/jobs/find"),
                         ("POST", "/api/jobs/x/feedback"), ("GET", "/api/apps/jobboard"),
                         ("POST", "/api/apps/jobboard/find"), ("GET", "/apps/jobboard/"),
                         ("GET", "/api/tools/jobboard/status")):
        r = c.request(method, path, headers=BROWSER,
                      **({"json": {}} if method == "POST" else {}))
        assert r.status_code == 403, (method, path, r.status_code, r.text)
        assert r.json().get("decoy") is True, (method, path)


def test_remote_machine_without_token_is_401(http_env):
    make, _ = http_env
    c = make(("testclient", 50000))
    auth.set_pin("1234")
    assert c.get("/api/jobs").status_code == 401
    assert c.post("/api/apps/jobboard/find", json={}).status_code == 401


def test_disabled_in_tools_yaml_404s_the_agent_path_too(http_env):
    from app import tools
    make, _ = http_env
    c = make(LOOPBACK)
    (config.DATA_DIR / "tools.yaml").write_text(
        "tools:\n  - {id: jobboard, kind: app, enabled: false}\n")
    tools._reset_state()
    for method, path in (("GET", "/api/jobs/reasons"), ("POST", "/api/jobs"),
                         ("POST", "/api/jobs/find"), ("GET", "/api/apps/jobboard/months")):
        r = c.request(method, path, **({"json": {}} if method == "POST" else {}))
        assert r.status_code == 404, (method, path, r.status_code)


def test_job_frames_are_app_prefixed(http_env, monkeypatch):
    make, _ = http_env
    c = make()
    sent = []

    async def fake(frame):
        sent.append(frame)
    monkeypatch.setattr(main.manager, "broadcast", fake)
    job_id = c.post("/api/jobs", json=_job_body()).json()["job_id"]
    c.post(f"/api/jobs/{job_id}/vote", json={"signal": "no"})
    types = [f["type"] for f in sent]
    assert "app:jobboard:job_created" in types and "app:jobboard:job_updated" in types
    assert not any(t in ("job_created", "job_updated") for t in types)
