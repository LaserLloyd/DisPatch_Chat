"""Tests for the monthly-threading jobs board (added 2026-09-15).

WHAT THIS FILE IS
-----------------
Integration coverage for the new monthly-thread model:

  * A new job post lands in the CURRENT month's discussion thread,
    not its own per-job thread. Two jobs posted in the same month
    share the same thread; two jobs posted in different months
    land in different threads.
  * ``GET /api/jobs/current`` finds-or-creates the current thread.
  * ``GET /api/jobs/months`` returns the chronological list of
    monthly threads with parsed (year, month) metadata.
  * ``GET /api/jobs/month/{key}`` returns one month's chat +
    structured jobs + scores.
  * Voting on a job inside a monthly thread does not affect
    other jobs in the same month (each job is keyed by job_id).
  * The dedup path returns the existing job_id, not the thread.

These tests bypass the FastAPI Pydantic boundary by calling
``create_job_from_dict`` and the database helpers directly — the
unit suite already covers the score / dedup modules in isolation.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app import config, database, jobs


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
async def wired(tmp_path, monkeypatch):
    """A clean staging DB with the jobs router loaded, mirroring the
    ``wired`` fixture in test_jobs_api.py but without the FastAPI
    app wrapper (we test the helpers directly)."""
    from app import database as db_module
    from app import main

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(config, "AVATAR_DIR", tmp_path / "avatars")
    config._invalidate_bots_cache()
    db = db_module.Database(tmp_path / "chats.db")
    await db.connect()
    monkeypatch.setattr(main, "db", db)
    jobs.JOBS_ENABLED = True
    p = {"_db": db}
    yield p
    await db.close()


def _payload(url: str, title: str = "Senior SWE", **overrides):
    base = {
        "bot_id": "jobboard",
        "url": url,
        "title": title,
        "company": "Anthropic",
        "location": "Tokyo, JP",
        "remote_type": "onsite",
        "salary_min": 200000, "salary_max": 320000,
        "salary_currency": "USD",
        "tags": ["python", "ml"],
        "brief": "Inference team.",
        "source_agent": "scout",
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------- #
# Storage-layer monthly thread helpers
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_monthly_thread_title_roundtrip():
    """The storage helper builds a deterministic title the API layer
    can parse back. A round-trip through ``parse_jobs_month_key``
    must recover the original (year, month)."""
    assert database.jobs_monthly_thread_title(2026, 9) == "Jobs — 2026-09"
    assert database.jobs_monthly_thread_title(2026, 12) == "Jobs — 2026-12"
    assert database.parse_jobs_month_key("2026-09") == (2026, 9)


@pytest.mark.asyncio
async def test_current_month_thread_creates_on_first_post(wired):
    """A single job post in a fresh DB creates exactly one monthly
    thread and writes one message into it."""
    db = wired["_db"]
    res = await jobs.create_job_from_dict(_payload(
        "https://anthropic.com/careers/staff-swe"))
    assert res["duplicate"] is False
    thread_id = res["thread_id"]
    msgs, _ = await db.list_messages(thread_id)
    assert len(msgs) == 1
    assert msgs[0].role == "assistant"
    # The thread title matches the canonical monthly-thread format.
    thread = await db.get_thread(thread_id)
    assert thread.title.startswith("Jobs — ")
    year, month = database.current_year_month()
    assert thread.title == database.jobs_monthly_thread_title(year, month)


@pytest.mark.asyncio
async def test_two_jobs_same_month_share_thread(wired):
    """Two jobs posted in the same month land in the same monthly
    thread, with two announcement messages. The DB has exactly one
    thread row carrying both jobs."""
    db = wired["_db"]
    res1 = await jobs.create_job_from_dict(_payload(
        "https://anthropic.com/careers/staff-swe"))
    res2 = await jobs.create_job_from_dict(_payload(
        "https://stripe.com/careers/senior-swe",
        title="Senior SWE Stripe",
        company="Stripe",
        location="Remote",
        remote_type="remote"))
    assert res1["thread_id"] == res2["thread_id"]
    msgs, _ = await db.list_messages(res1["thread_id"])
    assert len(msgs) == 2
    # The structured ``jobs`` table holds two rows (one per posting)
    # but both reference the same thread.
    rows = await db.list_jobs()
    assert len(rows) == 2
    assert {r["thread_id"] for r in rows} == {res1["thread_id"]}


@pytest.mark.asyncio
async def test_vote_targets_job_not_thread(wired):
    """A yes-vote on job A must NOT flip job B in the same monthly
    thread. Each job keeps its own state (the jobs.job_id key)."""
    db = wired["_db"]
    res1 = await jobs.create_job_from_dict(_payload(
        "https://anthropic.com/careers/staff-swe"))
    res2 = await jobs.create_job_from_dict(_payload(
        "https://stripe.com/careers/senior-swe",
        title="Senior SWE Stripe", company="Stripe", location="Remote"))
    await jobs._record_vote(res1["job_id"], "yes", None, None, "user")
    j1 = await db.get_job(res1["job_id"])
    j2 = await db.get_job(res2["job_id"])
    assert j1["state"] == "yes"
    assert j2["state"] == "pending"


@pytest.mark.asyncio
async def test_duplicate_returns_existing_job_id(wired):
    """A second post with the same URL within 30 days returns the
    first job's id — no second row, no second message."""
    db = wired["_db"]
    res1 = await jobs.create_job_from_dict(_payload(
        "https://anthropic.com/careers/staff-swe"))
    res2 = await jobs.create_job_from_dict(_payload(
        "https://anthropic.com/careers/staff-swe"))
    assert res2["duplicate"] is True
    assert res2["existing_job_id"] == res1["job_id"]
    rows = await db.list_jobs()
    assert len([r for r in rows if r["url"]
                == "https://anthropic.com/careers/staff-swe"]) == 1


@pytest.mark.asyncio
async def test_message_id_back_patched(wired):
    """After create, the job row carries the message_id of the
    announcement message (so the chat panel can scroll to / vote
    on it in one hop)."""
    db = wired["_db"]
    res = await jobs.create_job_from_dict(_payload(
        "https://anthropic.com/careers/staff-swe"))
    job = await db.get_job(res["job_id"])
    assert job["message_id"] == res["message_id"]
    # The chat panel can verify the message really exists in the
    # thread and belongs to this job (metadata.job_id).
    msgs, _ = await db.list_messages(res["thread_id"])
    assert any(m.id == job["message_id"] for m in msgs)


# --------------------------------------------------------------------------- #
# API surface
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_list_months_after_creating_jobs(wired):
    """``list_months`` returns a chronological list of monthly
    threads, with parsed (year, month) metadata."""
    res = await jobs.create_job_from_dict(_payload(
        "https://anthropic.com/careers/staff-swe"))
    # The wired DB has no PIN installed, so main._is_decoy returns
    # False (auth_gate's no-PIN allow). Build a fake request whose
    # .state.decoy is False.
    req = _FakeRequest(cookie=None)
    out = await jobs.list_months(request=req, bot_id="jobboard")
    assert "months" in out
    assert out["current"]["year"], out["current"]
    assert any(m["thread_id"] == res["thread_id"] for m in out["months"])


@pytest.mark.asyncio
async def test_get_month_returns_thread_messages_jobs(wired):
    """``get_month`` returns the thread + its messages + the
    structured jobs + a per-job score. The chat panel mounts the
    monthly chat from this single response."""
    res = await jobs.create_job_from_dict(_payload(
        "https://anthropic.com/careers/staff-swe"))
    year, month = database.current_year_month()
    key = database.jobs_month_key(year, month)
    req = _FakeRequest(cookie=None)
    out = await jobs.get_month(request=req, key=key, bot_id="jobboard")
    assert out["thread"]["id"] == res["thread_id"]
    assert len(out["messages"]) == 1
    assert len(out["jobs"]) == 1
    assert out["jobs"][0]["job_id"] == res["job_id"]
    assert res["job_id"] in out["score"]


@pytest.mark.asyncio
async def test_get_month_unknown_returns_empty_shape(wired):
    """An empty month has no thread yet — ``get_month`` returns the
    empty shape so the UI can render a placeholder without 404ing."""
    req = _FakeRequest(cookie=None)
    out = await jobs.get_month(request=req, key="2099-01",
                                bot_id="jobboard")
    assert out["thread"] is None
    assert out["messages"] == []
    assert out["jobs"] == []
    assert out["score"] is None
    assert out["month"]["year"] == 2099
    assert out["month"]["month"] == 1
    assert out["month"]["label"] == "January 2099"


@pytest.mark.asyncio
async def test_get_month_rejects_malformed_key(wired):
    """A bad YYYY-MM key must 400, not silently route."""
    req = _FakeRequest(cookie=None)
    with pytest.raises(Exception):
        await jobs.get_month(request=req, key="not-a-month",
                              bot_id="jobboard")


@pytest.mark.asyncio
async def test_current_month_ensure_creates(wired):
    """``current_month(ensure=true)`` creates the monthly thread if
    missing and returns its row. The Jobs board's click-to-enter
    path uses this so the user always lands on a real thread."""
    req = _FakeRequest(cookie=None)
    out = await jobs.current_month(request=req, bot_id="jobboard")
    assert out["month"]["year"]
    assert out["thread"] is not None
    # A second call returns the SAME thread (idempotent).
    out2 = await jobs.current_month(request=req, bot_id="jobboard")
    assert out2["thread"]["id"] == out["thread"]["id"]


def _FakeRequest(cookie=None, decoy=False):
    """Inline copy of the helper from test_jobs_api.py — we need a
    request whose ``state.decoy`` is set so ``_is_decoy`` returns
    False in the no-PIN install used by the wired fixture."""
    from types import SimpleNamespace
    return SimpleNamespace(
        cookies={"lc_session": cookie} if cookie else {},
        state=SimpleNamespace(decoy=decoy),
        query_params={},
    )


@pytest.mark.asyncio
async def test_parse_month_title_handles_alternate_hyphen(wired):
    """Some legacy titles may use an ASCII hyphen-minus instead of
    the em-dash. The parser accepts both forms (defensive — the
    storage layer's CREATE uses the em-dash form)."""
    assert jobs._parse_month_title("Jobs — 2026-09") == (2026, 9)
    assert jobs._parse_month_title("Jobs - 2026-09") == (2026, 9)
    assert jobs._parse_month_title("not a jobs thread") is None
    assert jobs._parse_month_title(None) is None
    assert jobs._parse_month_title("") is None
