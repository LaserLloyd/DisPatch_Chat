"""Agent-fired image jobs — the gate, the placeholder, and the four endings.

The feature's whole claim is that a request either becomes a picture or becomes
a visible failure, and never stays "pending" forever. Most of what is below is
there to hold that claim down: the success path, the rig's refusal, the
deadline, and a restart in the middle. The rig itself is stubbed — the real
one is proven separately by a smoke run — but nothing else is: the messages are
persisted through the app's own chokepoint, the bytes go through the app's own
media ingest, and the frames go through the real Safe-Mode redactor.

Run: cd backend && uv run pytest tests/test_image_jobs.py
"""

from __future__ import annotations

import asyncio
import json
import struct
import time
import zlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import auth, config, image_jobs
from app import main as main
from app.database import Database

# A browser stamps these. The image-job routes use them for the same reason the
# reaction fire does: to tell a locked TAB apart from an on-box agent taking
# the machine-to-machine exemption.
BROWSER = {"Sec-Fetch-Site": "same-origin", "Origin": "http://testserver"}

ENDPOINT = "http://image-rig.invalid:8700/mcp"


def _png(size: int = 20_000) -> bytes:
    """A real PNG, comfortably over the 10 KB floor.

    Built by hand rather than with Pillow: this file is about the job
    machinery, and a fixture that needs an image library is a fixture that
    fails for a reason unrelated to anything being tested.
    """
    def chunk(kind: bytes, body: bytes) -> bytes:
        return (struct.pack(">I", len(body)) + kind + body
                + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF))
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0)
    idat = zlib.compress(b"\x00\x00")
    # Pad with a comment chunk so the blob clears MIN_IMAGE_BYTES.
    pad = chunk(b"tEXt", b"pad\x00" + b"p" * size)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + pad
            + chunk(b"IDAT", idat) + chunk(b"IEND", b""))


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated data dir + DB, the feature switched on, and a client factory."""
    for name, sub in [("DATA_DIR", ""), ("CONFIG_PATH", "config.yaml"),
                      ("MEDIA_DIR", "media"), ("FILES_DIR", "files"),
                      ("LOG_DIR", "logs"), ("BACKUP_DIR", "backups")]:
        monkeypatch.setattr(config, name, tmp_path / sub if sub else tmp_path)
    monkeypatch.setattr(main, "MEDIA_DIR", tmp_path / "media")
    monkeypatch.setattr(main, "FILES_DIR", tmp_path / "files")
    monkeypatch.setattr(auth, "SECURITY_PATH", tmp_path / "security.yaml")
    monkeypatch.setattr(auth, "RECOVERY_PATH", tmp_path / "RECOVERY-CODE.txt")
    auth._sessions.clear()
    auth._cache = None
    auth._fail_count = 0
    auth._fail_until = 0.0
    config._invalidate_bots_cache()
    image_jobs.limiter.reset()
    image_jobs.reset_failures()
    # A previous test's client teardown latches main._shutting_down True for
    # the rest of the session (it is a process-lifetime flag, and the process
    # normally only shuts down once). The sweep honours it, so a fresh test
    # needs the fresh-process value back.
    monkeypatch.setattr(main, "_shutting_down", False)

    # The feature is configured, and "main" (the non-safe bot in the shared
    # roster) is the one bot allowed to use it.
    monkeypatch.setattr(main, "SETTINGS", replace(
        config.SETTINGS, clawforge_url=ENDPOINT, clawforge_files_url="",
        image_jobs_enabled=True))
    config.ensure_dirs()
    bots = config.load_bots()          # materialises config.yaml
    for b in bots:
        if b.id == "main":
            b.image_jobs = True
    config._write_bots([config._bot_entry(b) for b in bots])
    config._invalidate_bots_cache()
    assert config.get_bot("main").image_jobs is True

    temp_db = Database(tmp_path / "chats.db")
    monkeypatch.setattr(main, "db", temp_db)
    main._thread_bot.clear()

    clients: list[TestClient] = []

    def make_client(client_addr=("127.0.0.1", 50000)) -> TestClient:
        c = TestClient(main.app, client=client_addr)
        c.__enter__()
        clients.append(c)
        return c

    yield make_client

    for c in clients:
        c.__exit__(None, None, None)


def _unlocked(make_client, pin="4321"):
    auth.set_pin(pin)
    c = make_client()
    assert c.post("/api/auth/unlock", json={"pin": pin}).status_code == 200
    return c


def _thread(c, bot_id="main") -> str:
    r = c.post("/api/threads", json={"bot_id": bot_id})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _fire(c, thread_id, **over):
    body = {"bot_id": "main", "thread_id": thread_id,
            "prompt": "a blue ceramic teapot"}
    body.update(over)
    return c.post("/api/image-jobs", json=body)


def _messages(c, thread_id) -> list[dict]:
    r = c.get(f"/api/threads/{thread_id}/messages")
    assert r.status_code == 200, r.text
    return r.json()["messages"] if isinstance(r.json(), dict) else r.json()


class FakeForge:
    """A stand-in for the image server, driven per test.

    Same three calls the real client exposes, so the worker under test is the
    real worker — only the socket is replaced.
    """

    def __init__(self, *, enqueue=None, poll=None, fetch=None, cancel=None,
                 readiness=None):
        self._enqueue, self._poll, self._fetch = enqueue, poll, fetch
        self._cancel = cancel
        # "Can you render right now?" — a rig that has not been asked answers
        # the way the real client's probe does when it cannot ask: fail open.
        self._readiness = readiness or image_jobs.RigReadiness()
        self.calls: list[str] = []
        # What the worker actually submitted — the callback wiring, the
        # priority band and the spec are only real if they reach the rig.
        self.enqueue_args: list[dict] = []
        self.cancelled: list[str] = []

    async def readiness(self, *, max_age_s=0.0):
        self.calls.append("readiness")
        return self._readiness

    async def enqueue(self, spec, *, callback_url="", callback_token=""):
        self.calls.append("enqueue")
        self.enqueue_args.append({"spec": spec, "callback_url": callback_url,
                                  "callback_token": callback_token})
        if callable(self._enqueue):
            return self._enqueue(spec)
        if isinstance(self._enqueue, Exception):
            raise self._enqueue
        return image_jobs.EnqueueResult(job_id="rig-1")

    async def poll(self, rig_id):
        self.calls.append("poll")
        if isinstance(self._poll, Exception):
            raise self._poll
        if callable(self._poll):
            return self._poll(rig_id)
        return self._poll or image_jobs.PollResult(state="running", done=False)

    async def cancel(self, rig_id):
        self.calls.append("cancel")
        self.cancelled.append(rig_id)
        if isinstance(self._cancel, Exception):
            raise self._cancel
        return {"cancelled": True, "state": "cancelled"}

    async def fetch(self, rel):
        self.calls.append("fetch")
        if isinstance(self._fetch, Exception):
            raise self._fetch
        return self._fetch if self._fetch is not None else _png()


def _use(monkeypatch, forge):
    monkeypatch.setattr(main, "_clawforge", lambda: forge)
    return forge


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #


def test_an_on_box_agent_may_fire(env):
    c = env()
    tid = _thread(c)
    r = _fire(c, tid)
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["state"] == "queued"
    assert body["job_id"] and body["message_id"]


def test_a_locked_tab_is_refused(env):
    unlocked = _unlocked(env)          # sets a PIN, so Safe Mode exists
    tid = _thread(unlocked)
    locked = env()
    r = locked.post("/api/image-jobs",
                    json={"bot_id": "main", "thread_id": tid, "prompt": "x"},
                    headers=BROWSER)
    assert r.status_code == 403
    assert r.json()["detail"] == "Unlock for full access"


def test_an_unknown_thread_is_404_not_a_silent_success(env):
    c = env()
    r = _fire(c, "no-such-thread")
    assert r.status_code == 404
    assert r.json()["detail"] == "Unknown thread"


def test_a_bot_without_the_flag_is_refused(env):
    c = env()
    assert config.get_bot("alpha").image_jobs is False
    tid = _thread(c, "alpha")
    r = _fire(c, tid, bot_id="alpha")
    assert r.status_code == 403
    assert "enabled" in r.json()["detail"].lower()


def test_a_bot_cannot_fire_into_another_bots_thread(env):
    """The placeholder is attributed to the thread's own bot regardless, so a
    mismatch would let one bot put a picture under another's name."""
    c = env()
    tid = _thread(c, "alpha")
    r = _fire(c, tid, bot_id="main")
    assert r.status_code == 403
    assert "another bot" in r.json()["detail"]


def test_the_route_is_503_when_no_image_server_is_configured(env, monkeypatch):
    c = env()
    tid = _thread(c)
    monkeypatch.setattr(main, "SETTINGS", replace(
        main.SETTINGS, clawforge_url=""))
    r = _fire(c, tid)
    assert r.status_code == 503


def test_the_rate_limit_refuses_a_burst(env):
    """Distinct threads, deliberately: coalescing (see the block below) only
    supersedes a bot's own still-QUEUED job in the SAME thread, so a burst
    across separate threads still exhausts the bot-wide limiter exactly as it
    did before coalescing existed."""
    c = env()
    for _ in range(image_jobs.RATE_LIMIT):
        assert _fire(c, _thread(c)).status_code == 202
    r = _fire(c, _thread(c))
    assert r.status_code == 429
    assert "Too many image requests" in r.json()["detail"]


def test_a_bad_prompt_is_422(env):
    c = env()
    tid = _thread(c)
    assert _fire(c, tid, prompt="   ").status_code == 422
    assert _fire(c, tid, ratio="widescreen").status_code == 422


# --------------------------------------------------------------------------- #
# The placeholder
# --------------------------------------------------------------------------- #


def test_the_placeholder_lands_in_the_thread_immediately(env):
    c = env()
    tid = _thread(c)
    body = _fire(c, tid, caption="a teapot").json()

    rows = [m for m in _messages(c, tid) if m["id"] == body["message_id"]]
    assert len(rows) == 1
    row = rows[0]
    assert row["role"] == "assistant"
    assert "Generating an image" in row["content"]
    assert "a teapot" in row["content"]
    meta = row["metadata"]
    assert meta["kind"] == "image_job"
    assert meta["status"] == "queued"
    assert meta["job_id"] == body["job_id"]


def test_the_job_row_survives_and_is_readable(env):
    c = env()
    tid = _thread(c)
    jid = _fire(c, tid).json()["job_id"]
    r = c.get(f"/api/image-jobs/{jid}")
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "queued"
    assert r.json()["thread_id"] == tid
    assert c.get("/api/image-jobs/nope").status_code == 404


def test_status_is_refused_to_a_locked_tab(env):
    unlocked = _unlocked(env)
    tid = _thread(unlocked)
    jid = _fire(unlocked, tid).json()["job_id"]
    locked = env()
    r = locked.get(f"/api/image-jobs/{jid}", headers=BROWSER)
    assert r.status_code == 403


# --------------------------------------------------------------------------- #
# The worker: the four endings
# --------------------------------------------------------------------------- #


def test_success_rewrites_the_same_message_into_the_picture(env, monkeypatch):
    c = env()
    tid = _thread(c)
    body = _fire(c, tid).json()
    mid = body["message_id"]

    forge = _use(monkeypatch, FakeForge(
        poll=image_jobs.PollResult(state="done", done=True,
                                   files_rel="out/a.png")))

    async def drive():
        await main._image_job_sweep()      # queued -> running (enqueue)
        await main._image_job_sweep()      # running -> done   (poll + fetch)

    asyncio.run(drive())

    rows = [m for m in _messages(c, tid) if m["id"] == mid]
    assert len(rows) == 1, "the placeholder must be edited, never duplicated"
    row = rows[0]
    assert row["metadata"]["status"] == "done"
    # Ingested through the app's own media path — that is what gives the
    # picture its lightbox, its origin ledger entry and its Safe-Mode strip.
    assert "[[media:/media/" in row["content"]
    assert "Generating" not in row["content"]
    # The readiness probe comes FIRST: the rig is asked whether a submit would
    # run before one is made (`can_render`, not `comfy.running`).
    assert forge.calls == ["readiness", "enqueue", "poll", "fetch"]


def test_the_thread_list_preview_follows_the_rewrite(env, monkeypatch):
    """The list shows `last_message`; a rewrite that only pushed the bubble
    left every tab's list reading "Generating an image…" until a reload."""
    c = env()
    tid = _thread(c)
    _fire(c, tid)
    _use(monkeypatch, FakeForge(
        poll=image_jobs.PollResult(state="done", done=True, files_rel="out/a.png")))
    frames: list[dict] = []

    async def fake_broadcast(frame, *a, **k):
        frames.append(frame)
    monkeypatch.setattr(main.manager, "broadcast", fake_broadcast)

    async def drive():
        await main._image_job_sweep()
        await main._image_job_sweep()
    asyncio.run(drive())

    ups = [f for f in frames if f["type"] == "thread_update" and f["thread"]["id"] == tid]
    assert ups, "no thread_update after the rewrite"
    assert "[[media:/media/" in (ups[-1]["thread"]["last_message"] or "")
    order = [f["type"] for f in frames]
    assert order.index("message_update") < len(order) - 1 - order[::-1].index("thread_update")


def test_a_rig_refusal_becomes_a_visible_failure(env, monkeypatch):
    c = env()
    tid = _thread(c)
    mid = _fire(c, tid).json()["message_id"]
    _use(monkeypatch, FakeForge(
        enqueue=image_jobs.ImageJobError("Not enough free VRAM — short by 15.9 GB")))

    asyncio.run(main._image_job_sweep())

    row = [m for m in _messages(c, tid) if m["id"] == mid][0]
    assert row["metadata"]["status"] == "failed"
    assert row["content"].startswith("⚠️ image failed:")
    assert "free VRAM" in row["content"]
    assert image_jobs.failure_stats()["failures_24h"] == 1


def test_bytes_that_are_not_an_image_fail_rather_than_render_broken(env, monkeypatch):
    c = env()
    tid = _thread(c)
    mid = _fire(c, tid).json()["message_id"]
    _use(monkeypatch, FakeForge(
        poll=image_jobs.PollResult(state="done", done=True, files_rel="out/a.png"),
        fetch=image_jobs.ImageJobError(
            "the rig returned something that is not an image (only 28 bytes)")))

    async def drive():
        await main._image_job_sweep()
        await main._image_job_sweep()

    asyncio.run(drive())
    row = [m for m in _messages(c, tid) if m["id"] == mid][0]
    assert row["metadata"]["status"] == "failed"
    assert "not an image" in row["content"]


def test_a_job_that_never_finishes_times_out_loudly(env, monkeypatch):
    c = env()
    tid = _thread(c)
    body = _fire(c, tid).json()
    # Backdate the row past the deadline: the render is still "running" as far
    # as the rig is concerned, and this is the case where pending-forever would
    # otherwise be the outcome.
    old = (datetime.now() - timedelta(seconds=image_jobs.DEADLINE_S + 60)).isoformat()

    async def drive():
        await main.db.db.execute(
            "UPDATE image_jobs SET created_at = ? WHERE id = ?", (old, body["job_id"]))
        await main.db.db.commit()
        await main._image_job_sweep()

    _use(monkeypatch, FakeForge())
    asyncio.run(drive())

    row = [m for m in _messages(c, tid) if m["id"] == body["message_id"]][0]
    assert row["metadata"]["status"] == "failed"
    assert "timed out" in row["content"]


def test_a_transport_blip_is_retried_rather_than_failed(env, monkeypatch):
    c = env()
    tid = _thread(c)
    mid = _fire(c, tid).json()["message_id"]
    _use(monkeypatch, FakeForge(
        enqueue=image_jobs.ImageJobError("image server unreachable: ConnectError",
                                         retryable=True)))

    asyncio.run(main._image_job_sweep())

    row = [m for m in _messages(c, tid) if m["id"] == mid][0]
    assert row["metadata"]["status"] == "queued", (
        "a rig restart must not turn every in-flight job into a ⚠️ line")


# --------------------------------------------------------------------------- #
# Restart
# --------------------------------------------------------------------------- #


def test_a_restart_fails_out_a_job_the_rig_never_accepted(env):
    c = env()
    tid = _thread(c)
    body = _fire(c, tid).json()

    # Still `queued`: the placeholder exists, the rig has nothing. There is
    # nothing to resume, so it must fail visibly instead of waiting for a
    # deadline that starts ticking again in the new process.
    asyncio.run(main._resume_image_jobs())

    row = [m for m in _messages(c, tid) if m["id"] == body["message_id"]][0]
    assert row["metadata"]["status"] == "failed"
    assert "restart" in row["content"]


def test_a_restart_resumes_a_render_the_rig_still_holds(env, monkeypatch):
    c = env()
    tid = _thread(c)
    body = _fire(c, tid).json()

    async def drive():
        # Pretend the previous process got as far as handing it to the rig.
        await main.db.update_image_job(body["job_id"], state=image_jobs.RUNNING,
                                       rig_job_id="rig-1")
        await main._resume_image_jobs()
        await main._image_job_sweep()

    _use(monkeypatch, FakeForge(
        poll=image_jobs.PollResult(state="done", done=True, files_rel="out/a.png")))
    asyncio.run(drive())

    row = [m for m in _messages(c, tid) if m["id"] == body["message_id"]][0]
    assert row["metadata"]["status"] == "done"
    assert "[[media:/media/" in row["content"]


def test_switching_the_feature_off_fails_out_what_is_open(env, monkeypatch):
    c = env()
    tid = _thread(c)
    body = _fire(c, tid).json()
    monkeypatch.setattr(main, "SETTINGS", replace(
        main.SETTINGS, image_jobs_enabled=False))

    asyncio.run(main._resume_image_jobs())

    row = [m for m in _messages(c, tid) if m["id"] == body["message_id"]][0]
    assert row["metadata"]["status"] == "failed"
    assert "switched off" in row["content"]


# --------------------------------------------------------------------------- #
# Safe Mode / the new WS frame
# --------------------------------------------------------------------------- #


def test_a_message_update_from_a_non_safe_bot_is_dropped_for_a_locked_device(env):
    frame = {"type": "message_update", "thread_id": "t", "bot_id": "main",
             "message_id": "m",
             "message": {"id": "m", "thread_id": "t", "role": "assistant",
                         "content": "[[media:/media/a.png]]", "created_at": "x"}}
    assert main.redact_for_decoy(frame) is None


def test_a_message_update_from_a_safe_bot_keeps_the_row_but_loses_the_picture(env):
    frame = {"type": "message_update", "thread_id": "t", "bot_id": "alpha",
             "message_id": "m",
             "message": {"id": "m", "thread_id": "t", "role": "assistant",
                         "content": "[[media:/media/a.png]]", "created_at": "x",
                         "media_url": "/media/a.png"}}
    out = main.redact_for_decoy(frame)
    assert out is not None, (
        "a locked device that saw the placeholder must see it stop pending")
    assert out["message"]["media_url"] is None
    assert "/media/a.png" not in out["message"]["content"]


def test_the_frame_type_is_allowed_deliberately(env):
    assert "message_update" in main._DECOY_FRAME_ALLOW


def test_the_routes_are_off_the_safe_mode_surface(env):
    assert main._decoy_blocked("POST", "/api/image-jobs")
    assert main._decoy_blocked("GET", "/api/image-jobs/abc")
    assert main._is_inbound("POST", "/api/image-jobs")
    assert main._is_inbound("GET", "/api/image-jobs/abc")


# --------------------------------------------------------------------------- #
# The module's own rules
# --------------------------------------------------------------------------- #


def test_validate_image_rejects_the_failures_this_pipeline_actually_has():
    assert image_jobs.validate_image(_png())[0] is True
    assert image_jobs.validate_image(b'{"error":"no vram"}')[0] is False
    assert image_jobs.validate_image(b"\x89PNG\r\n\x1a\n" + b"x" * 100)[0] is False
    assert image_jobs.validate_image(b"")[0] is False


def test_the_spec_round_trips_and_refuses_nonsense():
    spec = image_jobs.ImageSpec(prompt="a teapot", workflow="wf", ratio="3:2",
                                caption="cap")
    again = image_jobs.ImageSpec.from_json(spec.to_json())
    assert (again.prompt, again.workflow, again.ratio, again.caption) == \
           ("a teapot", "wf", "3:2", "cap")
    with pytest.raises(ValueError):
        image_jobs.ImageSpec(prompt="")
    with pytest.raises(ValueError):
        image_jobs.ImageSpec(prompt="x", ratio="huge")
    with pytest.raises(ValueError):
        image_jobs.ImageSpec(prompt="x", width=512)      # height missing


def test_auto_mode_follows_whether_a_server_is_configured():
    assert config._image_jobs_default("auto", "") is False
    assert config._image_jobs_default("auto", ENDPOINT) is True
    assert config._image_jobs_default("0", ENDPOINT) is False
    assert config._image_jobs_default("1", "") is True


def test_the_files_url_is_derived_from_the_mcp_endpoint():
    forge = image_jobs.ClawForge("http://rig.invalid:8700/mcp")
    assert forge.files_url == "http://rig.invalid:8700/files/"
    explicit = image_jobs.ClawForge("http://rig.invalid:8700/mcp",
                                    files_url="http://elsewhere/f/")
    assert explicit.files_url == "http://elsewhere/f/"


def test_an_sse_framed_reply_parses_like_a_plain_one():
    plain = image_jobs._sse_json('{"result": {"ok": true}}')
    streamed = image_jobs._sse_json(
        'event: message\ndata: {"result": {"ok": true}}\n\n')
    assert plain == streamed == {"result": {"ok": True}}
    assert image_jobs._sse_json("garbage") == {}


def test_the_job_row_only_accepts_columns_it_has(env):
    async def drive():
        with pytest.raises(KeyError):
            await main.db.update_image_job("x", state_typo="done")

    asyncio.run(drive())


def test_a_tool_error_surfaces_the_rigs_own_sentence():
    """The rig's refusal text is the most useful string in the whole feature —
    it says WHY. Flattening it to "no image returned" is what made the
    predecessor pipeline undebuggable."""
    forge = image_jobs.ClawForge(ENDPOINT)
    res = {"isError": True,
           "content": [{"type": "text",
                        "text": "[insufficient_vram] Not enough free VRAM"}]}

    async def drive():
        forge.call = lambda *a, **k: _done(res)
        with pytest.raises(image_jobs.ImageJobError) as e:
            await forge._tool_json("generate_image", {})
        assert "Not enough free VRAM" in e.value.message

    async def _done(v):
        return v

    asyncio.run(drive())


def test_health_reports_the_failure_count(env):
    c = env()
    image_jobs.note_failure("j1", "boom")
    body = c.get("/api/health").json()
    assert body["image_job_failures_24h"] == 1
    # Count only — the prompt and the rig's error text stay off this route.
    assert "boom" not in json.dumps(body)


# --------------------------------------------------------------------------- #
# The inline marker
#
# `POST /api/image-jobs` is the explicit path; `[[pic:…]]` is the one a model
# takes on its own. What is tested here is the pair of properties the feature
# rests on: the marker never reaches a bubble, and it only creates a render
# when every guard says so.
# --------------------------------------------------------------------------- #


def _say(c, thread_id, content, role="assistant"):
    r = c.post("/api/inject", json={"thread_id": thread_id, "role": role,
                                    "content": content})
    assert r.status_code == 200, r.text
    return r.json()


def _placeholders(c, thread_id) -> list[dict]:
    return [m for m in _messages(c, thread_id)
            if (m.get("metadata") or {}).get("kind") == "image_job"]


def _job_of(c, row) -> dict:
    r = c.get(f"/api/image-jobs/{row['metadata']['job_id']}")
    assert r.status_code == 200, r.text
    return r.json()


def test_a_marker_in_a_reply_starts_a_job(env):
    c = env()
    tid = _thread(c)
    _say(c, tid, "Here you go. [[pic:a blue ceramic teapot]] Hope it fits.")

    rows = _messages(c, tid)
    assert len(rows) == 2, "the reply, then the placeholder it earned"
    reply, placeholder = rows[0], rows[1]
    assert "[[pic:" not in reply["content"]
    assert reply["content"] == "Here you go. Hope it fits."
    assert placeholder["metadata"]["kind"] == "image_job"
    assert placeholder["metadata"]["status"] == "queued"
    assert _job_of(c, placeholder)["prompt"] == "a blue ceramic teapot"
    assert _job_of(c, placeholder)["thread_id"] == tid


def test_a_marker_carries_an_optional_caption(env):
    c = env()
    tid = _thread(c)
    _say(c, tid, "[[pic:a blue ceramic teapot|Tea, at last]] There.")

    placeholder = _placeholders(c, tid)[0]
    assert placeholder["metadata"]["caption"] == "Tea, at last"
    assert "Tea, at last" in placeholder["content"]
    assert _job_of(c, placeholder)["prompt"] == "a blue ceramic teapot"


def test_a_reply_that_is_only_a_marker_still_starts_the_job(env):
    """An all-marker message persists no empty bubble — but the request in it
    is a request, not something to discard."""
    c = env()
    tid = _thread(c)
    _say(c, tid, "[[pic:a blue ceramic teapot]]")

    rows = _messages(c, tid)
    assert len(rows) == 1 and rows[0]["metadata"]["kind"] == "image_job"


def test_a_quoted_marker_is_text_and_fires_nothing(env):
    """A marker inside a code span or a fence is an agent DESCRIBING the
    syntax. Firing it both corrupts the sentence and spends rig time on
    documentation — the same bug the reaction markers were taught first."""
    c = env()
    tid = _thread(c)
    _say(c, tid, "Write `[[pic:a teapot]]` in a reply.\n\n"
                 "```\n[[pic:another teapot]]\n```")

    rows = _messages(c, tid)
    assert len(rows) == 1, "no placeholder — nothing was actually asked for"
    assert "`[[pic:a teapot]]`" in rows[0]["content"]
    assert "[[pic:another teapot]]" in rows[0]["content"]


def test_a_user_message_has_its_marker_stripped_and_fires_nothing(env):
    """Marker syntax is stripped on EVERY path; only an assistant's fires."""
    c = env()
    tid = _thread(c)
    _say(c, tid, "draw me [[pic:a teapot]] please", role="user")

    rows = _messages(c, tid)
    assert len(rows) == 1
    assert "[[pic:" not in rows[0]["content"]
    assert rows[0]["content"] == "draw me please"


def test_a_stale_replay_strips_its_marker_without_rendering(env):
    """Replaying HISTORY has no side effects. A live reply that took the
    scenic route (the follower, seconds later) still counts."""
    c = env()
    tid = _thread(c)

    def at(age_s):
        return (datetime.now(UTC) - timedelta(seconds=age_s)).isoformat()

    async def replay(age_s):
        await main._persist_and_broadcast_message(
            tid, "assistant", f"done [[pic:a teapot {age_s}]]",
            metadata={"followup": True}, created_at=at(age_s))

    asyncio.run(replay(main.REACTION_REPLAY_FRESH_S + 60))
    assert _placeholders(c, tid) == []
    assert "[[pic:" not in _messages(c, tid)[0]["content"]

    asyncio.run(replay(5))
    assert len(_placeholders(c, tid)) == 1


def test_a_bot_without_the_flag_gets_the_marker_stripped_only(env):
    c = env()
    assert config.get_bot("alpha").image_jobs is False
    tid = _thread(c, "alpha")
    _say(c, tid, "sure [[pic:a teapot]]")

    rows = _messages(c, tid)
    assert len(rows) == 1 and "[[pic:" not in rows[0]["content"]


def test_markers_are_stripped_when_no_image_server_is_configured(env,
                                                                 monkeypatch):
    c = env()
    tid = _thread(c)
    monkeypatch.setattr(main, "SETTINGS", replace(main.SETTINGS,
                                                  clawforge_url=""))
    _say(c, tid, "sure [[pic:a teapot]]")

    rows = _messages(c, tid)
    assert len(rows) == 1 and "[[pic:" not in rows[0]["content"]


def test_at_most_two_markers_per_message_render(env):
    c = env()
    tid = _thread(c)
    _say(c, tid, "[[pic:one]] [[pic:two]] [[pic:three]] [[pic:four]] done")

    placeholders = _placeholders(c, tid)
    assert len(placeholders) == 2
    assert [_job_of(c, p)["prompt"] for p in placeholders] == ["one", "two"]
    assert "[[pic:" not in _messages(c, tid)[0]["content"]


def test_the_rate_limiter_refuses_a_marker_without_eating_the_reply(env):
    # Distinct threads for the burst — coalescing only supersedes a bot's own
    # still-QUEUED job in the SAME thread (see the block below), so filling
    # the bot-wide limiter still needs independent threads.
    c = env()
    for _ in range(image_jobs.RATE_LIMIT):
        assert _fire(c, _thread(c)).status_code == 202
    tid = _thread(c)
    _say(c, tid, "one more [[pic:a teapot]]")

    assert len(_placeholders(c, tid)) == 0, "the limiter refused this one"
    body = [m for m in _messages(c, tid)
            if not (m.get("metadata") or {}).get("sub")][-1]
    assert "one more" in body["content"]
    # ...and the refusal is VISIBLE, in a collapsed sub row of its own. It used
    # to be a journal line, which nobody — human or agent — could tell apart
    # from a reply that never asked for a picture.
    assert any(m["content"].startswith("⚠️ No picture")
               for m in _messages(c, tid) if (m.get("metadata") or {}).get("sub"))


def test_an_unusable_marker_is_dropped_not_fatal(env):
    """ImageSpec is the validator; a marker it refuses costs the reply
    nothing.

    An EMPTY prompt is unusable and is dropped by the extractor. An over-long
    one is not: it is clamped, because truncating a 2,500-character styled
    prompt renders a picture and refusing it renders nothing.
    """
    c = env()
    tid = _thread(c)
    _say(c, tid, "[[pic:]] and [[pic:" + "x" * 3000 + "]] anyway")

    placeholders = _placeholders(c, tid)
    assert len(placeholders) == 1
    assert len(_job_of(c, placeholders[0])["prompt"]) == image_jobs.MAX_PROMPT_CHARS
    assert _messages(c, tid)[0]["content"] == "and anyway"


def test_a_placeholder_never_spawns_a_job_of_its_own(env, monkeypatch):
    """The placeholder text is server-written, but the guard is structural:
    a receipt for a render must not be able to start renders."""
    c = env()
    tid = _thread(c)
    monkeypatch.setattr(main, "_image_job_pending_text",
                        lambda spec: "🖼️ Generating… [[pic:recursion]]")
    _fire(c, tid)

    assert len(_placeholders(c, tid)) == 1
    assert "[[pic:" not in _placeholders(c, tid)[0]["content"]


# --------------------------------------------------------------------------- #
# Per-bot default workflow
# --------------------------------------------------------------------------- #


def _set_workflow(name: str, bot_id: str = "main") -> None:
    bots = config.load_bots()
    for b in bots:
        if b.id == bot_id:
            b.image_workflow = name
    config._write_bots([config._bot_entry(b) for b in bots])
    config._invalidate_bots_cache()
    assert config.get_bot(bot_id).image_workflow == name


def test_the_bots_default_workflow_lands_in_a_marker_job(env):
    c = env()
    _set_workflow("krea2")
    tid = _thread(c)
    _say(c, tid, "[[pic:a teapot]]")

    assert _job_of(c, _placeholders(c, tid)[0])["workflow"] == "krea2"


def test_the_bots_default_workflow_fills_an_endpoint_request(env):
    c = env()
    _set_workflow("krea2")
    tid = _thread(c)

    body = _fire(c, tid).json()
    assert c.get(f"/api/image-jobs/{body['job_id']}").json()["workflow"] == "krea2"

    # An explicit workflow still wins — the default only fills a gap.
    body = _fire(c, tid, workflow="z-image-turbo").json()
    assert c.get(
        f"/api/image-jobs/{body['job_id']}").json()["workflow"] == "z-image-turbo"


def test_the_workflow_survives_a_roster_write(env):
    """_bot_entry has dropped a field three times; image_workflow is only
    useful if it is still there after the next avatar upload."""
    env()
    _set_workflow("krea2")
    config.save_bot_avatar("main", "new-face.png")
    config._invalidate_bots_cache()
    assert config.get_bot("main").image_workflow == "krea2"


# --------------------------------------------------------------------------- #
# Dedup
# --------------------------------------------------------------------------- #


def test_the_dedup_key_ignores_the_marker(env):
    """EVERY transform persisting applies has to appear in _canon_msg. When
    the `:react:` strip did not, one reply re-posted five times in a morning."""
    assert (main._canon_msg("Here you go. [[pic:a teapot]] Hope it fits.")
            == main._canon_msg("Here you go. Hope it fits."))
    assert (main._canon_msg("done [[pic:a teapot|cap]]")
            == main._canon_msg("done"))
    # A quoted marker is text, and text is part of the key.
    assert (main._canon_msg("say `[[pic:x]]`")
            != main._canon_msg("say ``"))


# --------------------------------------------------------------------------- #
# The rig's completion callback
#
# A callback is an OPTIMISATION over the sweep: it makes a terminal transition
# arrive in a second instead of within five. What is tested here is that it
# cannot be anything more than that — the body is discarded, the token is the
# only credential, and a browser never takes this path.
# --------------------------------------------------------------------------- #


CALLBACK_BASE = "http://192.0.2.37:8765"


def _armed(monkeypatch):
    monkeypatch.setattr(main, "SETTINGS",
                        replace(main.SETTINGS, callback_base=CALLBACK_BASE))


async def _token_of(job_id: str) -> str:
    row = await main.db.get_image_job(job_id)
    return row["callback_token"]


def _callback(c, job_id, token, body=None):
    headers = {} if token is None else {"X-ClawForge-Token": token}
    return c.post(f"/api/image-jobs/{job_id}/callback",
                  json=body if body is not None else {"state": "done"},
                  headers=headers)


def test_the_submit_call_carries_the_callback_url_and_token(env, monkeypatch):
    c = env()
    _armed(monkeypatch)
    tid = _thread(c)
    jid = _fire(c, tid).json()["job_id"]
    forge = _use(monkeypatch, FakeForge())

    asyncio.run(main._image_job_sweep())

    sent = forge.enqueue_args[0]
    assert sent["callback_url"] == f"{CALLBACK_BASE}/api/image-jobs/{jid}/callback"
    assert sent["callback_token"] == asyncio.run(_token_of(jid))


def test_no_callback_base_means_pure_polling(env, monkeypatch):
    """The default is the behaviour that existed before callbacks: the rig is
    told nothing, and the sweep is the only thing that finishes a job."""
    c = env()
    tid = _thread(c)
    _fire(c, tid)
    forge = _use(monkeypatch, FakeForge())

    asyncio.run(main._image_job_sweep())

    assert forge.enqueue_args[0]["callback_url"] == ""


def test_a_valid_callback_advances_exactly_that_job(env, monkeypatch):
    c = env()
    _armed(monkeypatch)
    tid = _thread(c)
    first = _fire(c, tid).json()
    # Advance `first` all the way to REAL progress before firing `second`:
    # coalescing supersedes not only a QUEUED job but a RUNNING one the rig
    # has not actually started rendering yet (see the block below) — only
    # once the rig has reported real step/percent progress is a job safe from
    # a later marker in the same thread, and this test wants two genuinely
    # independent open jobs.
    _use(monkeypatch, FakeForge())
    asyncio.run(main._image_job_sweep())          # QUEUED -> RUNNING (enqueue)
    _use(monkeypatch, FakeForge(poll=image_jobs.PollResult(
        state="running", done=False,
        progress={"step": 1, "steps": 10, "percent": 10.0})))
    asyncio.run(main._image_job_sweep())          # rig reports real progress
    second = _fire(c, tid).json()

    forge = _use(monkeypatch, FakeForge(
        poll=image_jobs.PollResult(state="done", done=True,
                                   files_rel="out/a.png")))
    # Only the first one's callback arrives; `second` is never swept.
    r = _callback(c, first["job_id"], asyncio.run(_token_of(first["job_id"])))

    assert r.status_code == 200, r.text
    assert r.json()["state"] == "done"
    assert forge.calls == ["poll", "fetch"], "one job's worth of work, no sweep"
    rows = {m["id"]: m for m in _messages(c, tid)}
    assert rows[first["message_id"]]["metadata"]["status"] == "done"
    assert rows[second["message_id"]]["metadata"]["status"] == "queued"


def test_the_callback_body_is_ignored(env, monkeypatch):
    """The rig posts what get_job would return, and trusting it would let a
    forged POST write a picture URL into a family thread. We re-poll instead."""
    c = env()
    _armed(monkeypatch)
    tid = _thread(c)
    body = _fire(c, tid).json()
    _use(monkeypatch, FakeForge(
        poll=image_jobs.PollResult(state="running", done=False)))
    asyncio.run(main._image_job_sweep())

    r = _callback(c, body["job_id"], asyncio.run(_token_of(body["job_id"])),
                  body={"state": "done", "files_rel": ["out/evil.png"],
                        "error": "attacker text", "media_url": "/media/evil.png"})

    assert r.status_code == 200
    # The rig still says "running", so that is what the job is.
    assert r.json()["state"] == "running"
    row = [m for m in _messages(c, tid) if m["id"] == body["message_id"]][0]
    assert "Generating an image" in row["content"]
    assert "attacker text" not in json.dumps(row)
    assert "evil" not in json.dumps(row)


def test_a_callback_with_a_wrong_or_missing_token_is_refused(env, monkeypatch):
    c = env()
    _armed(monkeypatch)
    tid = _thread(c)
    jid = _fire(c, tid).json()["job_id"]
    forge = _use(monkeypatch, FakeForge())
    asyncio.run(main._image_job_sweep())
    forge.calls.clear()

    assert _callback(c, jid, "not-the-token").status_code == 403
    assert _callback(c, jid, "").status_code == 403
    assert _callback(c, jid, None).status_code == 403
    assert forge.calls == [], "a refused callback must not reach the rig"


def test_an_unknown_job_is_404(env, monkeypatch):
    c = env()
    _armed(monkeypatch)
    assert _callback(c, "no-such-job", "anything").status_code == 404


def test_a_browser_shaped_callback_is_refused(env, monkeypatch):
    """The credential is a per-job token held by the rig, so a locked TAB must
    get Safe Mode's answer rather than a path into the worker."""
    unlocked = _unlocked(env)
    _armed(monkeypatch)
    tid = _thread(unlocked)
    jid = _fire(unlocked, tid).json()["job_id"]
    token = asyncio.run(_token_of(jid))

    locked = env()
    r = locked.post(f"/api/image-jobs/{jid}/callback", json={},
                    headers={**BROWSER, "X-ClawForge-Token": token})
    assert r.status_code == 403


def test_a_callback_for_a_finished_job_is_benign(env, monkeypatch):
    """The rig retries up to three times; a retry after success must not be an
    error, and must not re-run the worker on a terminal row."""
    c = env()
    _armed(monkeypatch)
    tid = _thread(c)
    jid = _fire(c, tid).json()["job_id"]
    forge = _use(monkeypatch, FakeForge(
        poll=image_jobs.PollResult(state="done", done=True,
                                   files_rel="out/a.png")))
    asyncio.run(main._image_job_sweep())
    asyncio.run(main._image_job_sweep())
    forge.calls.clear()

    r = _callback(c, jid, asyncio.run(_token_of(jid)))
    assert r.status_code == 200
    assert r.json()["state"] == "done"
    assert forge.calls == []


def test_the_callback_route_is_on_the_machine_surface_only(env):
    assert main._is_inbound("POST", "/api/image-jobs/abc/callback")
    assert main._decoy_blocked("POST", "/api/image-jobs/abc/callback")


def test_the_status_route_never_hands_out_the_callback_token(env, monkeypatch):
    """It is readable by every on-box agent; the token is the one thing on the
    row that must stay between DisPatch and the rig."""
    c = env()
    _armed(monkeypatch)
    tid = _thread(c)
    jid = _fire(c, tid).json()["job_id"]
    token = asyncio.run(_token_of(jid))
    assert token
    assert token not in c.get(f"/api/image-jobs/{jid}").text


# --------------------------------------------------------------------------- #
# Cancelled — the third terminal ending
# --------------------------------------------------------------------------- #


def test_a_cancelled_render_is_its_own_ending_not_a_failure(env, monkeypatch):
    """An operator pressing Interrupt on the rig produces this WITHOUT DisPatch
    asking, and a worker that only knows "failed" would wait out the deadline."""
    c = env()
    tid = _thread(c)
    body = _fire(c, tid).json()
    _use(monkeypatch, FakeForge(
        poll=image_jobs.PollResult(state="cancelled", done=True,
                                   error="Cancelled by an operator")))

    async def drive():
        await main._image_job_sweep()
        await main._image_job_sweep()

    asyncio.run(drive())

    row = [m for m in _messages(c, tid) if m["id"] == body["message_id"]][0]
    assert row["metadata"]["status"] == "cancelled"
    assert row["content"].startswith("✋")
    assert "cancelled on the rig" in row["content"]
    assert "Cancelled by an operator" in row["content"]
    assert c.get(f"/api/image-jobs/{body['job_id']}").json()["state"] == "cancelled"
    # Not a rig fault, so it does not move the number an operator watches.
    assert image_jobs.failure_stats()["failures_24h"] == 0


def test_a_cancelled_job_is_closed_and_never_swept_again(env, monkeypatch):
    c = env()
    tid = _thread(c)
    _fire(c, tid)
    forge = _use(monkeypatch, FakeForge(
        poll=image_jobs.PollResult(state="cancelled", done=True, error="stopped")))

    async def drive():
        await main._image_job_sweep()
        await main._image_job_sweep()
        forge.calls.clear()
        await main._image_job_sweep()

    asyncio.run(drive())
    assert forge.calls == []


def test_the_deadline_withdraws_the_render_from_the_rig(env, monkeypatch):
    c = env()
    tid = _thread(c)
    body = _fire(c, tid).json()
    forge = _use(monkeypatch, FakeForge())
    old = (datetime.now() - timedelta(seconds=image_jobs.DEADLINE_S + 60)).isoformat()

    async def drive():
        await main._image_job_sweep()          # queued -> running
        await main.db.db.execute(
            "UPDATE image_jobs SET created_at = ? WHERE id = ?",
            (old, body["job_id"]))
        await main.db.db.commit()
        await main._image_job_sweep()

    asyncio.run(drive())

    assert forge.cancelled == ["rig-1"]
    row = [m for m in _messages(c, tid) if m["id"] == body["message_id"]][0]
    assert row["metadata"]["status"] == "failed"
    assert "timed out" in row["content"]


def test_a_cancel_the_rig_refuses_does_not_stop_the_job_failing(env, monkeypatch):
    """Cancelling is best-effort by contract: the local ending is what makes a
    stuck job impossible, and it must not depend on the rig answering."""
    c = env()
    tid = _thread(c)
    body = _fire(c, tid).json()
    _use(monkeypatch, FakeForge(
        cancel=image_jobs.ImageJobError("image server unreachable: ConnectError",
                                        retryable=True)))
    old = (datetime.now() - timedelta(seconds=image_jobs.DEADLINE_S + 60)).isoformat()

    async def drive():
        await main._image_job_sweep()
        await main.db.db.execute(
            "UPDATE image_jobs SET created_at = ? WHERE id = ?",
            (old, body["job_id"]))
        await main.db.db.commit()
        await main._image_job_sweep()

    asyncio.run(drive())
    row = [m for m in _messages(c, tid) if m["id"] == body["message_id"]][0]
    assert row["metadata"]["status"] == "failed"


def test_deleting_the_placeholder_withdraws_the_render(env, monkeypatch):
    c = env()
    tid = _thread(c)
    body = _fire(c, tid).json()
    forge = _use(monkeypatch, FakeForge())
    asyncio.run(main._image_job_sweep())          # queued -> running

    assert c.delete(f"/api/messages/{body['message_id']}").status_code == 200
    assert forge.cancelled == ["rig-1"]
    assert asyncio.run(main.db.get_image_job(body["job_id"])) is None


def test_deleting_the_thread_withdraws_every_open_render(env, monkeypatch):
    c = env()
    tid = _thread(c)
    forge = _use(monkeypatch, FakeForge(
        enqueue=lambda spec: image_jobs.EnqueueResult(job_id=f"rig-{spec.prompt}")))
    _fire(c, tid, prompt="a blue ceramic teapot")
    asyncio.run(main._image_job_sweep())          # -> RUNNING, so it survives
    _fire(c, tid, prompt="a second one")          # coalescing (still QUEUED)
    asyncio.run(main._image_job_sweep())          # -> RUNNING too

    assert c.delete(f"/api/threads/{tid}?hard=true").status_code == 200
    assert len(forge.cancelled) == 2


# --------------------------------------------------------------------------- #
# Progress
# --------------------------------------------------------------------------- #


def test_progress_reaches_the_card_and_merges_into_the_metadata(env, monkeypatch):
    """update_message_metadata replaces wholesale, so a progress write that
    patched only its own key would drop the prompt off the placeholder."""
    c = env()
    tid = _thread(c)
    body = _fire(c, tid, caption="a teapot").json()
    _use(monkeypatch, FakeForge(
        poll=image_jobs.PollResult(
            state="running", done=False,
            progress={"step": 15, "steps": 32, "percent": 46.9})))

    async def drive():
        await main._image_job_sweep()
        await main._image_job_sweep()

    asyncio.run(drive())

    meta = [m for m in _messages(c, tid)
            if m["id"] == body["message_id"]][0]["metadata"]
    assert meta["progress"] == {"step": 15, "steps": 32, "percent": 46.9}
    assert meta["kind"] == "image_job" and meta["status"] == "running"
    assert meta["caption"] == "a teapot"
    assert meta["prompt"] == "a blue ceramic teapot"
    assert c.get(f"/api/image-jobs/{body['job_id']}").json()["progress"]["step"] == 15


def test_progress_is_broadcast_when_it_changes_and_not_when_it_does_not(env,
                                                                       monkeypatch):
    """Every write here is a frame to every open device, and the poll runs
    every few seconds for up to ten minutes."""
    c = env()
    tid = _thread(c)
    _fire(c, tid)
    steps = iter([{"step": 1, "steps": 32, "percent": 3.1},
                  {"step": 1, "steps": 32, "percent": 3.1},
                  {"step": 8, "steps": 32, "percent": 25.0}])
    _use(monkeypatch, FakeForge(
        poll=lambda rig_id: image_jobs.PollResult(
            state="running", done=False, progress=next(steps))))

    frames: list[dict] = []

    async def fake_broadcast(frame):
        frames.append(frame)

    monkeypatch.setattr(main.manager, "broadcast", fake_broadcast)

    async def drive():
        for _ in range(4):
            await main._image_job_sweep()

    asyncio.run(drive())

    updates = [f for f in frames if f["type"] == "message_update"]
    assert len(updates) == 2, "one frame per CHANGE, not one per poll"
    assert updates[-1]["message"]["metadata"]["progress"]["percent"] == 25.0


def test_a_finished_job_does_not_keep_a_stale_percentage(env, monkeypatch):
    c = env()
    tid = _thread(c)
    body = _fire(c, tid).json()
    polls = iter([
        image_jobs.PollResult(state="running", done=False,
                              progress={"step": 15, "steps": 32, "percent": 46.9}),
        image_jobs.PollResult(state="done", done=True, files_rel="out/a.png"),
    ])
    _use(monkeypatch, FakeForge(poll=lambda rig_id: next(polls)))

    async def drive():
        for _ in range(3):
            await main._image_job_sweep()

    asyncio.run(drive())

    meta = [m for m in _messages(c, tid)
            if m["id"] == body["message_id"]][0]["metadata"]
    assert meta["status"] == "done"
    assert "progress" not in meta


def test_a_junk_progress_block_is_dropped_rather_than_shown():
    assert image_jobs._progress({"progress": "soon"}) is None
    assert image_jobs._progress({}) is None
    assert image_jobs._progress({"progress": {"percent": "46.9"}}) is None
    assert image_jobs._progress(
        {"progress": {"step": 15, "steps": 32, "percent": 146.9}}) == {
            "step": 15, "steps": 32, "percent": 100.0}


# --------------------------------------------------------------------------- #
# Priority
# --------------------------------------------------------------------------- #


def test_the_chat_path_submits_at_the_interactive_band(env, monkeypatch):
    """Everything through a thread has a placeholder somebody is looking at."""
    c = env()
    tid = _thread(c)
    forge = _use(monkeypatch, FakeForge())
    _fire(c, tid)
    asyncio.run(main._image_job_sweep())          # -> RUNNING, survives coalescing
    _say(c, tid, "[[pic:a teapot]]")
    asyncio.run(main._image_job_sweep())

    assert [a["spec"].priority for a in forge.enqueue_args] == [1, 1]


def test_the_endpoint_accepts_a_band_and_refuses_the_rest(env, monkeypatch):
    c = env()
    tid = _thread(c)
    assert _fire(c, tid, priority=3).status_code == 202
    assert _fire(c, tid, priority=0).status_code == 422
    assert _fire(c, tid, priority=4).status_code == 422
    forge = _use(monkeypatch, FakeForge())

    asyncio.run(main._image_job_sweep())
    assert forge.enqueue_args[0]["spec"].priority == 3


def test_the_band_survives_the_stored_spec():
    spec = image_jobs.ImageSpec(prompt="a teapot", priority=3)
    assert image_jobs.ImageSpec.from_json(spec.to_json()).priority == 3
    # A row written before priority existed reads back as the chat band.
    assert image_jobs.ImageSpec.from_json('{"prompt": "x"}').priority == 1
    for bad in (0, 4, "high", None):
        with pytest.raises(ValueError):
            image_jobs.ImageSpec(prompt="x", priority=bad)


def test_the_band_reaches_the_rig_arguments():
    """The spec is only worth validating if it lands in the tool call."""
    forge = image_jobs.ClawForge(ENDPOINT)
    seen: dict = {}

    async def fake_tool_json(tool, args, **kw):
        seen.update(args)
        return {"job_id": "rig-1", "seeds": [2975651872]}

    async def drive():
        forge._tool_json = fake_tool_json
        res = await forge.enqueue(
            image_jobs.ImageSpec(prompt="a teapot", priority=3),
            callback_url="http://host/cb", callback_token="tok")
        assert res.seed == 2975651872

    asyncio.run(drive())
    assert seen["priority"] == 3
    assert seen["callback_url"] == "http://host/cb"
    assert seen["callback_token"] == "tok"
    assert seen["wait"] is False


# --------------------------------------------------------------------------- #
# Structured error codes
# --------------------------------------------------------------------------- #


def _refusal(code: str) -> dict:
    return {"isError": True,
            "content": [{"type": "text",
                         "text": f"Error executing tool generate_image: "
                                 f"[{code}] the rig's own sentence"}],
            "structuredContent": {"error": {"code": code,
                                            "message": f"[{code}] …",
                                            "tool": "generate_image"}}}


@pytest.mark.parametrize("code,retryable", [
    ("insufficient_vram", True),
    ("backend_unavailable", True),
    ("captioner_unavailable", True),
    ("insufficient_compute_cap", False),
    ("comfy_rejected", False),
    ("workflow_not_found", False),
    ("invalid_priority", False),
    ("", False),
])
def test_the_structured_code_decides_retry_not_the_prose(code, retryable):
    """The text carries the SDK's own "Error executing tool …: " prefix in
    front of the [code], which is why matching it never worked."""
    forge = image_jobs.ClawForge(ENDPOINT)
    res = _refusal(code) if code else {"isError": True,
                                       "content": [{"type": "text", "text": "boom"}]}

    async def drive():
        async def fake_call(*a, **k):
            return res
        forge.call = fake_call
        with pytest.raises(image_jobs.ImageJobError) as e:
            await forge._tool_json("generate_image", {})
        assert e.value.retryable is retryable
        assert e.value.code == code
        if code:
            assert "the rig's own sentence" in e.value.message

    asyncio.run(drive())


def test_a_retryable_refusal_waits_and_a_terminal_one_does_not(env, monkeypatch):
    c = env()
    tid = _thread(c)
    vram = _fire(c, tid).json()
    _use(monkeypatch, FakeForge(enqueue=image_jobs.ImageJobError(
        "[insufficient_vram] Not enough free VRAM — short by 15.9 GB",
        code="insufficient_vram", retryable=True)))
    asyncio.run(main._image_job_sweep())
    row = [m for m in _messages(c, tid) if m["id"] == vram["message_id"]][0]
    assert row["metadata"]["status"] == "queued"
    # ...and the pending line says something a human can read. It used to take
    # the rig's sentence and keep the text after its last colon, which on a
    # live run produced "Waiting for the image rig (llama-server.exe)".
    assert "the GPUs are busy" in row["content"]

    missing = _fire(c, tid).json()
    _use(monkeypatch, FakeForge(enqueue=image_jobs.ImageJobError(
        "[workflow_not_found] no such workflow", code="workflow_not_found")))
    asyncio.run(main._image_job_sweep())
    row = [m for m in _messages(c, tid) if m["id"] == missing["message_id"]][0]
    assert row["metadata"]["status"] == "failed"
    assert "no such workflow" in row["content"]


# --------------------------------------------------------------------------- #
# Seeds
# --------------------------------------------------------------------------- #


def test_the_seed_is_stored_at_enqueue_and_exposed(env, monkeypatch):
    """`seeds` is on the wait:false reply, populated before the graph is even
    submitted — it is what makes "again, but…" possible later."""
    c = env()
    tid = _thread(c)
    body = _fire(c, tid).json()
    _use(monkeypatch, FakeForge(
        enqueue=lambda spec: image_jobs.EnqueueResult(job_id="rig-1",
                                                      seed=2975651872)))

    asyncio.run(main._image_job_sweep())

    assert c.get(f"/api/image-jobs/{body['job_id']}").json()["seed"] == 2975651872


def test_the_finished_picture_carries_its_seed_in_metadata_only(env, monkeypatch):
    c = env()
    tid = _thread(c)
    body = _fire(c, tid).json()
    _use(monkeypatch, FakeForge(
        enqueue=lambda spec: image_jobs.EnqueueResult(job_id="rig-1",
                                                      seed=2975651872),
        poll=image_jobs.PollResult(state="done", done=True,
                                   files_rel="out/a.png")))

    async def drive():
        await main._image_job_sweep()
        await main._image_job_sweep()

    asyncio.run(drive())

    row = [m for m in _messages(c, tid) if m["id"] == body["message_id"]][0]
    assert row["metadata"]["seed"] == 2975651872
    assert "2975651872" not in row["content"], "machinery, not something to read"


def test_the_seed_is_read_off_the_poll_when_the_enqueue_had_none():
    assert image_jobs._first_seed({"seeds": [7]}) == 7
    assert image_jobs._first_seed({"seed": 7}) == 7
    assert image_jobs._first_seed({"seeds": []}) is None
    assert image_jobs._first_seed({"seeds": ["7"]}) is None


def test_an_unreachable_rig_is_named_on_the_placeholder_then_cleared(env, monkeypatch):
    c = env()
    tid = _thread(c)
    mid = _fire(c, tid).json()["message_id"]
    forge = _use(monkeypatch, FakeForge(
        enqueue=image_jobs.ImageJobError(
            "image server unreachable: ConnectError (backing off)", retryable=True)))

    asyncio.run(main._image_job_sweep())
    asyncio.run(main._image_job_sweep())
    row = [m for m in _messages(c, tid) if m["id"] == mid][0]
    assert row["metadata"]["status"] == "queued"
    assert "Waiting for the image rig" in row["content"]
    assert "ConnectError" in row["content"]
    assert row["metadata"]["progress"]["waiting"] == "ConnectError"

    # Rig back: accepted, then the first poll drops the waiting wording.
    forge._enqueue = None
    asyncio.run(main._image_job_sweep())
    asyncio.run(main._image_job_sweep())
    row = [m for m in _messages(c, tid) if m["id"] == mid][0]
    assert row["metadata"]["status"] == "running"
    assert row["content"].startswith("🖼️ Generating an image…"), row["content"]
    assert "waiting" not in (row["metadata"].get("progress") or {})


def test_health_reports_whether_the_rig_is_reachable(env, monkeypatch):
    c = _unlocked(env)
    body = c.get("/api/health?detailed=1").json()
    assert "image_rig" in body
    assert body["image_rig"].get("reachable") in (True, False, None)


def test_a_submit_wakes_the_worker_instead_of_waiting_for_the_tick(env, monkeypatch):
    main._image_job_wake = None
    c = env()
    tid = _thread(c)
    assert _fire(c, tid).status_code == 202
    assert main._image_job_wake is not None and main._image_job_wake.is_set()


def test_the_sweep_advances_jobs_concurrently_not_in_series(env, monkeypatch):
    # Distinct threads: same-thread requests now coalesce onto the newest one
    # (see the block below), so three jobs genuinely open together need three
    # threads.
    c = env()
    for i in range(3):
        assert _fire(c, _thread(c), prompt=f"teapot {i}").status_code == 202
    active = {"now": 0, "peak": 0}

    class SlowForge(FakeForge):
        async def enqueue(self, spec, **kw):
            active["now"] += 1
            active["peak"] = max(active["peak"], active["now"])
            await asyncio.sleep(0.05)
            active["now"] -= 1
            return await super().enqueue(spec, **kw)
    _use(monkeypatch, SlowForge())

    t0 = time.monotonic()
    asyncio.run(main._image_job_sweep())
    assert active["peak"] == 3, "three open jobs must be in flight together"
    assert time.monotonic() - t0 < 0.15, "a serial sweep would take 3× as long"


def test_a_callback_and_the_sweep_cannot_advance_one_job_twice(env, monkeypatch):
    c = env()
    tid = _thread(c)
    _fire(c, tid)
    forge = _use(monkeypatch, FakeForge())

    async def race():
        job = (await main.db.open_image_jobs())[0]
        await asyncio.gather(main._advance_image_job(dict(job)),
                             main._advance_image_job(dict(job)))
    asyncio.run(race())
    assert forge.calls.count("enqueue") == 1, "the second advancer must yield"


# --------------------------------------------------------------------------- #
# A rig somebody else has booked (ClawForge2 2.2.1, 2026-09-04)
#
# `[gpu_leased]` is not a shortage and not a fault: the GPUs are promised to
# another holder. The distinction that matters to a reader is the lease KIND —
# a render or an agent lease clears in minutes and is worth waiting out, a
# benchmark holds every card for hours and is one to stand down from.
# --------------------------------------------------------------------------- #


def _leased(kind: str, holder: str = "bench-runner",
            retry_after_s: int = 300) -> image_jobs.ImageJobError:
    """The refusal as the rig raises it, built through the real parser so the
    payload shape is what is under test — not a hand-set flag."""
    res = {"isError": True,
           "content": [{"type": "text", "text":
                        f"Error executing tool generate_image: [gpu_leased] "
                        f"CUDA 3 is leased to {holder}-judge [{kind}]"}],
           "structuredContent": {"error": {
               "code": "gpu_leased", "tool": "generate_image",
               "message": "[gpu_leased] …",
               "lease": {"holder": f"{holder}-judge", "holder_family": holder,
                         "kind": kind, "expires_at": 1788500360.9,
                         "retry_after_s": retry_after_s}}}}
    forge = image_jobs.ClawForge(ENDPOINT)

    async def drive():
        async def fake_call(*a, **k):
            return res
        forge.call = fake_call
        with pytest.raises(image_jobs.ImageJobError) as e:
            await forge._tool_json("generate_image", {})
        return e.value
    return asyncio.run(drive())


def test_a_lease_refusal_carries_its_holder_and_kind_off_the_payload():
    err = _leased("benchmark")
    assert err.code == image_jobs.GPU_LEASED
    assert err.facts["holder_family"] == "bench-runner"
    assert err.facts["kind"] == "benchmark"
    assert err.retry_after_s == 300
    assert err.reservation == "bench-runner (benchmark)"
    # A benchmark is not waited out; a render lease is.
    assert err.retryable is False
    assert _leased("render").retryable is True


def test_a_benchmark_lease_ends_the_job_honestly_instead_of_spinning(env,
                                                                    monkeypatch):
    """The reader is told the rig is booked — not shown a ⚠️ that reads as if
    the picture went wrong, and not left with a spinner for the full deadline
    when the answer will not change for hours."""
    c = env()
    tid = _thread(c)
    before = main.image_jobs.failure_stats()["failures_24h"]
    fired = _fire(c, tid).json()
    _use(monkeypatch, FakeForge(enqueue=_leased("benchmark")))
    asyncio.run(main._image_job_sweep())

    row = [m for m in _messages(c, tid) if m["id"] == fired["message_id"]][0]
    assert row["metadata"]["status"] == "failed"
    assert "reserved by bench-runner (benchmark)" in row["content"]
    assert "⚠️" not in row["content"], "a booked rig is not a failed render"
    assert "ask again" in row["content"]
    # And it does NOT move the health counter: that number is for a rig going
    # wrong, and a night of benchmarking would otherwise light it up.
    assert main.image_jobs.failure_stats()["failures_24h"] == before
    # A cron caller must be able to tell this ending from a failed render
    # without matching on prose.
    job = c.get(f"/api/image-jobs/{fired['job_id']}").json()
    assert job["progress"]["reserved"] == "bench-runner (benchmark)"


def test_a_render_lease_waits_and_says_who_has_the_rig(env, monkeypatch):
    c = env()
    tid = _thread(c)
    fired = _fire(c, tid).json()
    _use(monkeypatch, FakeForge(enqueue=_leased("render", holder="doxy-pics")))
    asyncio.run(main._image_job_sweep())

    row = [m for m in _messages(c, tid) if m["id"] == fired["message_id"]][0]
    assert row["metadata"]["status"] == "queued", "still pending, not failed"
    assert "reserved by doxy-pics (render)" in row["content"]


def test_a_client_quota_refusal_waits_for_a_slot(env, monkeypatch):
    """Our own tag is over its active-render quota. A refusal, not a fault —
    and one that says something a reader can understand."""
    c = env()
    tid = _thread(c)
    fired = _fire(c, tid).json()
    _use(monkeypatch, FakeForge(enqueue=image_jobs.ImageJobError(
        "[client_quota] dispatch already has 3 renders in flight",
        code="client_quota", retryable=True, retry_after_s=42.0)))
    asyncio.run(main._image_job_sweep())

    row = [m for m in _messages(c, tid) if m["id"] == fired["message_id"]][0]
    assert row["metadata"]["status"] == "queued"
    assert "free slot" in row["content"]


def test_a_stalled_render_is_resubmitted_exactly_once(env, monkeypatch):
    """`[stalled]` means the job moved and then went quiet — a wedged sampler,
    not a graph that cannot run. Worth one more go, and only one: a rig that
    stalls every attempt must still end visibly."""
    c = env()
    tid = _thread(c)
    fired = _fire(c, tid).json()
    stalled = image_jobs.PollResult(
        state="failed", done=True,
        error="[stalled] no progress for 240s", code="stalled")
    forge = _use(monkeypatch, FakeForge(poll=stalled))

    asyncio.run(main._image_job_sweep())      # queued -> running
    asyncio.run(main._image_job_sweep())      # poll -> stalled -> requeued
    row = [m for m in _messages(c, tid) if m["id"] == fired["message_id"]][0]
    assert row["metadata"]["status"] == "queued", "put back, not failed"

    asyncio.run(main._image_job_sweep())      # queued -> running (2nd submit)
    asyncio.run(main._image_job_sweep())      # poll -> stalled -> now it fails
    row = [m for m in _messages(c, tid) if m["id"] == fired["message_id"]][0]
    assert row["metadata"]["status"] == "failed"
    assert "no progress" in row["content"]
    assert forge.calls.count("enqueue") == 2, "exactly one extra attempt"


# --------------------------------------------------------------------------- #
# `can_render`: asking the rig whether a submit would run at all
# --------------------------------------------------------------------------- #


def test_a_leased_rig_is_not_submitted_into_at_all(env, monkeypatch):
    """The pre-flight and the submit refusal must reach the same verdict —
    otherwise the pre-flight parks a job the submit would have ended."""
    c = env()
    tid = _thread(c)
    fired = _fire(c, tid).json()
    forge = _use(monkeypatch, FakeForge(readiness=image_jobs.RigReadiness(
        can_render=False, reason="cards_leased",
        detail="CUDA [0,1,2,3] are leased to bench-runner-judge [benchmark]",
        lease_holder="bench-runner", lease_kind="benchmark",
        checked_at=1.0)))
    asyncio.run(main._image_job_sweep())

    row = [m for m in _messages(c, tid) if m["id"] == fired["message_id"]][0]
    assert row["metadata"]["status"] == "failed"
    assert "reserved by bench-runner (benchmark)" in row["content"]
    assert "enqueue" not in forge.calls, "no submit into somebody's reservation"
    # The rig's own sentence names its internal ComfyUI host: journal, not chat.
    assert "CUDA" not in row["content"]


def test_a_rig_that_cannot_start_waits_without_burning_a_submit(env, monkeypatch):
    c = env()
    tid = _thread(c)
    fired = _fire(c, tid).json()
    forge = _use(monkeypatch, FakeForge(readiness=image_jobs.RigReadiness(
        can_render=False, reason="circuit_open",
        detail="crash-loop circuit tripped", checked_at=1.0)))
    asyncio.run(main._image_job_sweep())

    row = [m for m in _messages(c, tid) if m["id"] == fired["message_id"]][0]
    assert row["metadata"]["status"] == "queued", "pending, not failed"
    assert "circuit_open" in row["content"]
    assert "enqueue" not in forge.calls


@pytest.mark.parametrize("reason", ["startable", "backend_reachable",
                                    "something_new", "unknown"])
def test_everything_else_submits_exactly_as_before(env, monkeypatch, reason):
    """`startable` is a cold start, not an outage; an unfamiliar reason is
    inconclusive. Both submit — the rig's refusal remains the authority."""
    c = env()
    tid = _thread(c)
    _fire(c, tid)
    forge = _use(monkeypatch, FakeForge(readiness=image_jobs.RigReadiness(
        can_render=(reason in ("startable", "backend_reachable", "unknown")),
        reason=reason, checked_at=1.0)))
    asyncio.run(main._image_job_sweep())
    assert "enqueue" in forge.calls


# --------------------------------------------------------------------------- #
# Review round, 2026-09-04 — the endings that used to overwrite each other,
# the caption that deleted its own picture, and the markers that vanished.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("caption", ["nice ]bracket", "5] of 6", "]x", "a]]b",
                                     "[[media:/etc/passwd]]"])
def test_a_caption_with_a_bracket_still_delivers_its_picture(env, monkeypatch,
                                                             caption):
    """The one that cost a real render: `[[media:…|<caption>]]` cannot express
    a `]` inside the caption, so the finished directive failed to match, the
    delivery test failed, and the code DELETED the validated image and told the
    family "the image could not be stored" — a correct picture destroyed, and
    the blame put on the disk."""
    c = env()
    tid = _thread(c)
    fired = _fire(c, tid, caption=caption).json()
    _use(monkeypatch, FakeForge(
        poll=image_jobs.PollResult(state="done", done=True,
                                   files_rel="out/a.png")))

    async def drive():
        await main._image_job_sweep()
        await main._image_job_sweep()
    asyncio.run(drive())

    row = [m for m in _messages(c, tid) if m["id"] == fired["message_id"]][0]
    assert row["metadata"]["status"] == "done", row["content"]
    assert "[[media:/media/" in row["content"]
    assert "could not be stored" not in row["content"]
    # And the file is still on disk, not unlinked by the failure branch.
    assert list((main.MEDIA_DIR).glob("*.png")), "the picture was deleted"


def test_the_deadline_cannot_overwrite_a_picture_that_landed(env, monkeypatch):
    """A fetch may span the deadline (the fetch timeout is 180 s of the 600).
    Whoever ends the job first wins; the loser must not rewrite the message."""
    c = env()
    tid = _thread(c)
    fired = _fire(c, tid).json()
    forge = _use(monkeypatch, FakeForge(
        poll=image_jobs.PollResult(state="done", done=True,
                                   files_rel="out/a.png")))

    async def drive():
        await main._image_job_sweep()          # queued -> running
        await main._image_job_sweep()          # poll + fetch -> delivered
        # Now the deadline comes round on a row the sweep had already read.
        job = await main.db.get_image_job(fired["job_id"])
        stale = dict(job)
        stale["created_at"] = "1999-01-01T00:00:00+00:00"
        await main._advance_image_job(stale)
    asyncio.run(drive())

    row = [m for m in _messages(c, tid) if m["id"] == fired["message_id"]][0]
    assert row["metadata"]["status"] == "done", "the picture was overwritten"
    assert "[[media:/media/" in row["content"]
    assert "timed out" not in row["content"]
    assert main.image_jobs.failure_stats()["failures_24h"] == 0
    assert forge.calls.count("fetch") == 1


def test_a_picture_finishing_into_a_deleted_thread_is_discarded_not_logged(
        env, monkeypatch):
    """Deleting the placeholder cascades the job row away. The delivery used to
    write the file, rewrite a message that no longer existed, and log
    "delivered"."""
    c = env()
    tid = _thread(c)
    fired = _fire(c, tid).json()
    _use(monkeypatch, FakeForge(
        poll=image_jobs.PollResult(state="done", done=True,
                                   files_rel="out/a.png")))

    async def drive():
        await main._image_job_sweep()          # queued -> running
        job = dict(await main.db.get_image_job(fired["job_id"]))
        await main.db.delete_message(fired["message_id"])
        await main._advance_image_job(job)     # delivers into nothing
    asyncio.run(drive())

    assert asyncio.run(main.db.get_image_job(fired["job_id"])) is None
    assert not list(main.MEDIA_DIR.glob("*.png")), \
        "the bytes must not be kept when there is nothing to deliver into"


def test_a_rig_that_forgot_the_job_says_so_in_english(env, monkeypatch):
    """ClawForge2 only remembers job ids for the life of its own process, and
    it gets restarted. What the family used to get was the rig's developer
    sentence about which tools mint job ids, clipped mid-word."""
    c = env()
    tid = _thread(c)
    fired = _fire(c, tid).json()

    forge = image_jobs.ClawForge(ENDPOINT)

    async def fake_call(tool, args, **kw):
        if tool == "get_job":
            return {"isError": True, "content": [{"type": "text", "text":
                    "Error executing tool get_job: No job called 'rig-1'. Job "
                    "ids come from generate_image / generate_portrait / "
                    "crop_to_face and are only remembered for this server "
                    "session — nothing has been generated yet."}],
                    "structuredContent": {"error": {"code": "tool_error"}}}
        return {"content": [{"type": "text",
                             "text": json.dumps({"job_id": "rig-1"})}]}
    forge.call = fake_call
    forge.readiness = lambda **kw: _ready()
    monkeypatch.setattr(main, "_clawforge", lambda: forge)

    async def drive():
        await main._image_job_sweep()
        await main._image_job_sweep()
    asyncio.run(drive())

    row = [m for m in _messages(c, tid) if m["id"] == fired["message_id"]][0]
    assert row["metadata"]["status"] == "failed"
    assert row["content"] == ("⚠️ image failed: the image server restarted and "
                              "lost this render")
    assert "generate_portrait" not in row["content"]
    assert "Error executing tool" not in row["content"]


async def _ready():
    return image_jobs.RigReadiness()


def test_a_lost_submit_answer_is_not_sent_twice(env, monkeypatch):
    """`generate_image` has no idempotency key. A read timeout after the rig
    accepted the job used to leave the row QUEUED, and the sweep resubmitted —
    up to ten real renders for one placeholder, nine of them unpollable."""
    c = env()
    tid = _thread(c)
    fired = _fire(c, tid).json()
    forge = _use(monkeypatch, FakeForge(enqueue=image_jobs.ImageJobError(
        "image server unreachable: ReadTimeout", retryable=True,
        uncertain=True)))

    async def drive():
        await main._image_job_sweep()
        await main._image_job_sweep()
    asyncio.run(drive())

    assert forge.calls.count("enqueue") == 1, "asked twice for one picture"
    row = [m for m in _messages(c, tid) if m["id"] == fired["message_id"]][0]
    assert row["metadata"]["status"] == "failed"
    assert "not asked twice" in row["content"]


def test_a_connect_failure_is_still_retried(env, monkeypatch):
    """The other half of the same rule: a connect failure never left the box,
    so repeating it cannot have doubled anything."""
    c = env()
    tid = _thread(c)
    fired = _fire(c, tid).json()
    forge = _use(monkeypatch, FakeForge(enqueue=image_jobs.ImageJobError(
        "image server unreachable: ConnectError", retryable=True)))

    async def drive():
        await main._image_job_sweep()
        await main._image_job_sweep()
    asyncio.run(drive())

    assert forge.calls.count("enqueue") == 2
    row = [m for m in _messages(c, tid) if m["id"] == fired["message_id"]][0]
    assert row["metadata"]["status"] == "queued"


def test_an_expired_job_is_withdrawn_from_the_rig_at_boot(env, monkeypatch):
    """A restart during renders used to fail them locally and leave them
    burning GPUs nobody was waiting on."""
    c = env()
    tid = _thread(c)
    fired = _fire(c, tid).json()
    forge = _use(monkeypatch, FakeForge())

    async def drive():
        await main._image_job_sweep()          # queued -> running (rig-1)
        await main.db.update_image_job(
            fired["job_id"], progress=None)
        await main.db.db.execute(
            "UPDATE image_jobs SET created_at = ? WHERE id = ?",
            ("1999-01-01T00:00:00+00:00", fired["job_id"]))
        await main.db.db.commit()
        await main._resume_image_jobs()
    asyncio.run(drive())

    assert forge.cancelled == ["rig-1"], "the render was left running"


# --------------------------------------------------------------------------- #
# Markers that produce nothing must not produce silence either
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("text,expected", [
    ("here you go [[pic:a blue teapot", "here you go"),
    ("[[pic:a very\nlong prompt]]", "long prompt]]"),
    ("before [[pic:x\nafter", "before\nafter"),
])
def test_an_unterminated_marker_never_reaches_a_bubble(env, text, expected):
    """The worst outcome available: internal syntax on the family's screens,
    on the locked tablets too, and no picture."""
    c = env()
    tid = _thread(c)
    _say(c, tid, text)
    body = [m for m in _messages(c, tid) if not (m.get("metadata") or {}).get("sub")][-1]
    assert "[[pic:" not in body["content"]
    assert expected in body["content"]


def test_a_documented_marker_in_backticks_still_survives(env):
    """The skill tells agents to show the syntax in backticks. That is code,
    it fires nothing, and it must not be eaten by the remnant strip."""
    c = env()
    tid = _thread(c)
    _say(c, tid, "write it like `[[pic:a cat]]` in your reply")
    body = _messages(c, tid)[-1]
    assert "`[[pic:a cat]]`" in body["content"]
    assert not _placeholders(c, tid)


def test_a_spaced_marker_renders_rather_than_printing_itself(env):
    c = env()
    tid = _thread(c)
    _say(c, tid, "look [[ pic:a blue teapot]]")
    rows = _placeholders(c, tid)
    assert len(rows) == 1, "a shape models actually produce"
    assert "[[" not in [m for m in _messages(c, tid)
                        if m["id"] != rows[0]["id"]][-1]["content"]


def test_strip_and_extract_still_agree_byte_for_byte():
    """The dedup key depends on this equality; when it slipped once, a reply
    was re-posted five times."""
    for text in ("a [[pic:x]] b", "here you go [[pic:unterminated",
                 "`[[pic:doc]]`", "[[ pic:spaced]]", "[[pic:a]]\n[[pic:b",
                 "plain text", "```\n[[pic:in a fence]]\n```"):
        assert (image_jobs.strip_pic_markers(text)
                == image_jobs.extract_pic_markers(text)[0]), text


def test_a_dropped_marker_leaves_a_visible_trace_and_a_counter(env):
    """A marker that earns no picture used to be a journal line and nothing
    else — indistinguishable, to everyone including the agent, from a reply
    that never asked for one."""
    c = env()
    image_jobs.reset_marker_drops()
    # Exhaust the bot-wide limiter across distinct threads (coalescing only
    # protects a bot's own still-QUEUED job in the SAME thread — see the block
    # below), then ask for one more in a fresh thread.
    for _ in range(image_jobs.RATE_LIMIT):
        assert _fire(c, _thread(c)).status_code == 202
    tid = _thread(c)
    _say(c, tid, "[[pic:one more teapot]]")
    subs = [m for m in _messages(c, tid)
            if (m.get("metadata") or {}).get("sub")
            and m["content"].startswith("⚠️ No picture")]
    assert subs, "the refusal was invisible"
    assert "Too many image requests" in subs[0]["content"]
    assert image_jobs.marker_drop_stats()["drops_24h"] >= 1
    assert c.get("/api/health").json()["image_marker_drops_24h"] >= 1


def test_an_over_length_prompt_is_clamped_rather_than_dropped(env):
    """Truncating a 2,500-character styled prompt renders a picture; refusing
    it renders nothing, and the marker is already stripped by then."""
    c = env()
    tid = _thread(c)
    _say(c, tid, f"[[pic:{'a beautifully detailed teapot, ' * 120}]]")
    rows = _placeholders(c, tid)
    assert len(rows) == 1
    job = _job_of(c, rows[0])
    assert len(job["prompt"]) == image_jobs.MAX_PROMPT_CHARS


def test_a_locked_device_never_sees_an_image_job_row_at_all(env):
    """Locked DisPatch is a family-safe afterthought: rather than hand-craft a
    degraded card, an image-job placeholder (at any stage of its life) simply
    does not exist for a locked viewer."""
    for status, extra in [
        ("queued", {"prompt": "a very private prompt", "caption": "",
                    "workflow": "anima", "seed": 12345}),
        ("failed", {"error": "CUDA 3 is leased to someone"}),
        ("cancelled", {"error": "superseded by a newer request"}),
        ("done", {"media_url": "/media/x.png"}),
    ]:
        redacted = main._redact_message_dict({
            "id": "m1", "content": f"anything, status={status}",
            "media_url": None,
            "metadata": {"kind": "image_job", "status": status, **extra}})
        assert redacted is None, f"a {status} image-job row leaked to Safe Mode"


def test_an_image_job_frame_is_dropped_not_delivered_with_a_null_message(env):
    frame = {"type": "message_update", "thread_id": "t", "bot_id": "main",
             "message_id": "m",
             "message": {"id": "m", "thread_id": "t", "role": "assistant",
                         "content": "🖼️ Generating an image…",
                         "metadata": {"kind": "image_job", "status": "queued",
                                      "job_id": "j1"}}}
    assert main.redact_for_decoy(frame) is None


def test_an_image_job_row_is_absent_from_a_redacted_message_list(env):
    frame = {"type": "messages", "messages": [
        {"id": "m1", "role": "assistant", "content": "hello",
         "metadata": None},
        {"id": "m2", "role": "assistant", "content": "🖼️ Generating…",
         "metadata": {"kind": "image_job", "status": "queued", "job_id": "j1"}},
    ]}
    out = main.redact_for_decoy(frame)
    assert [m["id"] for m in out["messages"]] == ["m1"]


# --------------------------------------------------------------------------- #
# Per-bot identity injection (`image_identity_source`)
#
# A companion whose look is curated (Doxy) should write the SCENE ONLY — the
# server prepends her canonical identity block before the prompt ever reaches
# the rig. See app/image_jobs.identity_prompt, which parses the exact same
# "## Canonical base prompt" file+format the `doxy-pics` CLI does.
# --------------------------------------------------------------------------- #

IDENTITY_PROMPT = (
    "athletic curvy dog-girl kemono, floppy dog ears, wagging tail, "
    "auburn hair, warm brown eyes, anime illustration, soft studio lighting"
)


def _write_identity_file(tmp_path, prompt: str = IDENTITY_PROMPT,
                         name: str = "identity.md") -> str:
    path = tmp_path / name
    path.write_text(
        "# Test Bot — Canonical Visual Identity\n\n"
        "## Identity spec\n\n- some bullet points here\n\n"
        "## Canonical base prompt\n\n```\n" + prompt + "\n```\n\n"
        "## Generation & delivery\n\nsome trailing prose nobody parses.\n")
    return str(path)


def _set_identity_source(bot_id: str, source: str) -> None:
    bots = config.load_bots()
    for b in bots:
        if b.id == bot_id:
            b.image_identity_source = source
    config._write_bots([config._bot_entry(b) for b in bots])
    config._invalidate_bots_cache()
    assert config.get_bot(bot_id).image_identity_source == source


def test_identity_injection_prepends_the_canonical_prompt(env, tmp_path):
    c = env()
    _set_identity_source("main", _write_identity_file(tmp_path))
    tid = _thread(c)
    _say(c, tid, "[[pic:kneeling by the window, morning light]]")

    job = _job_of(c, _placeholders(c, tid)[0])
    assert job["prompt"].startswith(IDENTITY_PROMPT)
    assert job["prompt"] == f"{IDENTITY_PROMPT}, kneeling by the window, morning light"


def test_identity_injection_keeps_the_identity_out_of_the_visible_line(env,
                                                                       tmp_path):
    """The canonical identity paragraph must never reach a chat bubble — only
    the scene the bot actually wrote does, on a locked device too."""
    c = env()
    _set_identity_source("main", _write_identity_file(tmp_path))
    tid = _thread(c)
    _say(c, tid, "[[pic:kneeling by the window, morning light]]")

    placeholder = _placeholders(c, tid)[0]
    assert IDENTITY_PROMPT not in placeholder["content"]
    assert "kneeling by the window, morning light" in placeholder["content"]
    assert placeholder["metadata"]["prompt"].startswith(IDENTITY_PROMPT), (
        "the STORED spec is the full rig-bound prompt")
    # ...and a locked device is redacted down to the card either way.
    redacted = main._redact_message_dict(placeholder)
    assert IDENTITY_PROMPT not in json.dumps(redacted)


def test_a_bots_own_caption_survives_identity_injection(env, tmp_path):
    c = env()
    _set_identity_source("main", _write_identity_file(tmp_path))
    tid = _thread(c)
    _say(c, tid, "[[pic:kneeling by the window|Good morning]]")

    placeholder = _placeholders(c, tid)[0]
    assert "Good morning" in placeholder["content"]
    assert IDENTITY_PROMPT not in placeholder["content"]
    assert _job_of(c, placeholder)["prompt"] == f"{IDENTITY_PROMPT}, kneeling by the window"


def test_a_bot_without_the_flag_gets_no_identity_injection(env):
    """Every other bot's behaviour is byte-identical to before this
    feature existed: no `image_identity_source` means no injection at all."""
    c = env()
    assert config.get_bot("main").image_identity_source == ""
    tid = _thread(c)
    _say(c, tid, "[[pic:a blue ceramic teapot]]")

    job = _job_of(c, _placeholders(c, tid)[0])
    assert job["prompt"] == "a blue ceramic teapot"


def test_a_missing_identity_file_drops_the_marker_not_the_reply(env, tmp_path):
    """A bot that opted into identity injection is refusing to guess its own
    look, the same way `doxy-pics die()`s on a missing file — but the marker
    is dropped, not the reply it arrived in."""
    c = env()
    _set_identity_source("main", str(tmp_path / "does-not-exist.md"))
    tid = _thread(c)
    _say(c, tid, "sure [[pic:a teapot]]")

    rows = _messages(c, tid)
    assert "[[pic:" not in rows[0]["content"]
    assert _placeholders(c, tid) == []
    assert any(m["content"].startswith("⚠️ No picture")
               for m in _messages(c, tid) if (m.get("metadata") or {}).get("sub"))


def test_identity_injection_also_applies_to_the_explicit_endpoint(env, tmp_path):
    """`/api/image-jobs` cannot be used to bypass server-side identity
    injection and post an off-model picture under an identity-bound bot."""
    c = env()
    _set_identity_source("main", _write_identity_file(tmp_path))
    tid = _thread(c)
    body = _fire(c, tid, prompt="kneeling by the window").json()
    job = c.get(f"/api/image-jobs/{body['job_id']}").json()
    assert job["prompt"] == f"{IDENTITY_PROMPT}, kneeling by the window"


def test_the_identity_parser_matches_doxy_pics_byte_for_byte():
    """image_jobs.identity_prompt must stay the SAME parser doxy-pics uses —
    one file, one regex, one 40-char floor — never a second, diverging copy."""
    import re as _re
    doxy_pics_src = Path.home().joinpath(".local/bin/doxy-pics").read_text()
    m = _re.search(r'PROMPT_BLOCK_RE = re\.compile\(\s*(r".*?")', doxy_pics_src,
                   _re.S)
    if not m:
        pytest.skip("doxy-pics not present on this box")
    assert eval(m.group(1)) == image_jobs._IDENTITY_BLOCK_RE.pattern


def test_identity_injection_for_a_doxy_style_bot(env, tmp_path):
    """The real-world case this feature exists for: a companion whose look is
    curated writes the scene only, and the server prepends her canonical
    identity block, her curated workflow, and her curated aspect ratio — the
    same three things live `doxy-pics` config carries for her
    (`anima-Bits`, `aspect_ratio: "2:3"`)."""
    c = env()
    _set_identity_source("main", _write_identity_file(tmp_path))
    _set_workflow("anima-Bits")
    bots = config.load_bots()
    for b in bots:
        if b.id == "main":
            b.image_ratio = "2:3"
    config._write_bots([config._bot_entry(b) for b in bots])
    config._invalidate_bots_cache()

    tid = _thread(c)
    _say(c, tid, "[[pic:kneeling by the window, morning light]]")

    job = _job_of(c, _placeholders(c, tid)[0])
    assert job["prompt"] == f"{IDENTITY_PROMPT}, kneeling by the window, morning light"
    assert job["workflow"] == "anima-Bits"
    assert c.get(f"/api/image-jobs/{job['job_id']}").json()["prompt"].startswith(
        IDENTITY_PROMPT)
    # The ratio isn't echoed by the status route today; the stored spec is
    # the ground truth for what actually reaches the rig.
    stored_spec = image_jobs.ImageSpec.from_json(
        asyncio.run(main.db.get_image_job(job["job_id"]))["spec"])
    assert stored_spec.ratio == "2:3"


def test_no_bot_gets_identity_injection_without_explicit_opt_in(env):
    """Every bot in the roster without an explicit opt-in must be
    byte-identical to before this feature existed: `image_identity_source`
    defaults to empty for every shipped and test bot, with no exceptions."""
    for b in config.load_bots():
        assert b.image_identity_source == "", (
            f"{b.id} carries a default image_identity_source — "
            "identity injection must be an explicit opt-in")


def test_a_captionless_marker_never_leaks_the_composed_prompt_as_caption(env,
                                                                         tmp_path):
    """A bot that writes no caption of its own must never have the composed
    (identity + scene) prompt stand in for one — the visible line is always
    the scene, at most, never the character's canonical description."""
    c = env()
    _set_identity_source("main", _write_identity_file(tmp_path))
    tid = _thread(c)
    _say(c, tid, "[[pic:stretching after a long day]]")

    placeholder = _placeholders(c, tid)[0]
    assert placeholder["metadata"]["caption"] == "stretching after a long day"
    assert IDENTITY_PROMPT not in placeholder["metadata"]["caption"]
    assert IDENTITY_PROMPT not in placeholder["content"]
    assert placeholder["metadata"]["prompt"] == (
        f"{IDENTITY_PROMPT}, stretching after a long day")


# --------------------------------------------------------------------------- #
# Coalescing — newest scene wins
#
# The rate limiter used to refuse a burst outright and stop there. Now a new
# `[[pic:…]]` for a bot with an unfinished render in this thread quietly
# supersedes it instead: fast chat yields fewer pictures, never late or
# refused ones. "Unfinished" is not "still QUEUED" — `enqueue()` answers in
# about a second, so a row is RUNNING almost immediately; the real backlog on
# a serial rig is a RUNNING row the RIG has not actually started rendering
# yet (see `_job_unstarted_on_rig`), and that is what gets superseded too.
# --------------------------------------------------------------------------- #


def test_a_second_marker_supersedes_the_first_still_queued_job(env):
    c = env()
    tid = _thread(c)
    _say(c, tid, "[[pic:scene one]]")
    first = _placeholders(c, tid)[0]
    _say(c, tid, "[[pic:scene two]]")

    placeholders = _placeholders(c, tid)
    assert len(placeholders) == 2
    first_after = [m for m in placeholders if m["id"] == first["id"]][0]
    # A distinct MESSAGE status, not `cancelled`: imagejobs.js renders any
    # recognised non-pending status as a failure card, and a supersede is
    # normal operation, not a failure. The underlying job row still ends
    # CANCELLED for bookkeeping — see the terminal-state test below.
    assert first_after["metadata"]["status"] == "superseded"
    assert first_after["metadata"].get("sub") is True, (
        "rendered as a collapsed one-line trace, like a reaction sub row")
    assert "superseded" in first_after["content"]
    second = [m for m in placeholders if m["id"] != first["id"]][0]
    assert second["metadata"]["status"] == "queued"
    assert _job_of(c, second)["prompt"] == "scene two"


def test_superseding_does_not_emit_a_refusal_or_drop_row(env):
    """"Silent" means no REFUSAL — the superseded placeholder itself is
    expected to collapse to a sub row (see the test above); what must NOT
    appear is a "Too many image requests" / "No picture" drop."""
    c = env()
    image_jobs.reset_marker_drops()
    tid = _thread(c)
    _say(c, tid, "[[pic:scene one]]")
    _say(c, tid, "[[pic:scene two]]")
    _say(c, tid, "[[pic:scene three]]")

    drops = [m for m in _messages(c, tid)
             if m["content"].startswith("⚠️ No picture")]
    assert drops == [], "superseding must be silent — no drop row, no refusal"
    assert image_jobs.marker_drop_stats()["drops_24h"] == 0


def test_superseding_refunds_the_rate_limiter(env):
    """Coalescing a stale request must not cost the bot capacity it would
    otherwise have had for a genuinely new one — this fires well past
    RATE_LIMIT in one thread and none of it is refused."""
    c = env()
    tid = _thread(c)
    n = image_jobs.RATE_LIMIT + 3
    for i in range(n):
        _say(c, tid, f"[[pic:scene {i}]]")

    assert len(_placeholders(c, tid)) == n
    assert not any(m["content"].startswith("⚠️ No picture")
                   for m in _messages(c, tid))


def test_a_rig_queued_running_job_is_superseded_too(env, monkeypatch):
    """The real backlog on a serial rig: `enqueue()` answers in ~1s, so a job
    is RUNNING almost immediately in OUR db while the RIG itself may still be
    sitting on it behind other work. A RUNNING job with no real progress yet
    is exactly as supersedable as one still locally QUEUED."""
    c = env()
    tid = _thread(c)
    _say(c, tid, "[[pic:scene one]]")
    first = _placeholders(c, tid)[0]
    _use(monkeypatch, FakeForge())
    asyncio.run(main._image_job_sweep())          # -> RUNNING, never polled
    job = asyncio.run(main.db.get_image_job(first["metadata"]["job_id"]))
    assert job["state"] == "running"

    _say(c, tid, "[[pic:scene two]]")

    job = asyncio.run(main.db.get_image_job(first["metadata"]["job_id"]))
    assert job["state"] == "cancelled"
    rows = {m["id"]: m for m in _messages(c, tid)}
    assert rows[first["id"]]["metadata"]["status"] == "superseded"


def test_a_running_job_with_real_progress_is_left_to_finish(env, monkeypatch):
    """Only once the RIG has reported real step/percent progress has a job
    actually spent GPU time — cancelling it then would waste that time, so
    it is left alone."""
    c = env()
    tid = _thread(c)
    _say(c, tid, "[[pic:scene one]]")
    first = _placeholders(c, tid)[0]
    _use(monkeypatch, FakeForge())
    asyncio.run(main._image_job_sweep())          # -> RUNNING
    _use(monkeypatch, FakeForge(poll=image_jobs.PollResult(
        state="running", done=False,
        progress={"step": 3, "steps": 20, "percent": 15.0})))
    asyncio.run(main._image_job_sweep())          # rig reports real progress
    _say(c, tid, "[[pic:scene two]]")

    rows = {m["id"]: m for m in _messages(c, tid)}
    assert rows[first["id"]]["metadata"]["status"] == "running", (
        "message metadata is written at creation and only rewritten on a "
        "terminal ending or a progress update — a protected job gets neither")
    assert len(_placeholders(c, tid)) == 2
    job = asyncio.run(main.db.get_image_job(first["metadata"]["job_id"]))
    assert job["state"] == "running"


def test_a_rig_reported_queued_state_is_superseded(env, monkeypatch):
    """A rig that explicitly says a job is still `queued` on ITS side (not
    merely unpolled) is just as supersedable — the rig_state string itself is
    the strongest signal, ahead of the no-progress default."""
    c = env()
    tid = _thread(c)
    _say(c, tid, "[[pic:scene one]]")
    first = _placeholders(c, tid)[0]
    _use(monkeypatch, FakeForge())
    asyncio.run(main._image_job_sweep())          # -> RUNNING, never polled
    _use(monkeypatch, FakeForge(
        poll=image_jobs.PollResult(state="queued", done=False)))
    asyncio.run(main._image_job_sweep())          # rig says: still queued there
    job = asyncio.run(main.db.get_image_job(first["metadata"]["job_id"]))
    assert json.loads(job["progress"])["rig_state"] == "queued"

    _say(c, tid, "[[pic:scene two]]")

    job = asyncio.run(main.db.get_image_job(first["metadata"]["job_id"]))
    assert job["state"] == "cancelled"


def test_a_superseded_placeholder_reaches_a_terminal_state(env):
    c = env()
    tid = _thread(c)
    _say(c, tid, "[[pic:scene one]]")
    first = _placeholders(c, tid)[0]
    _say(c, tid, "[[pic:scene two]]")

    row = [m for m in _messages(c, tid) if m["id"] == first["id"]][0]
    # The MESSAGE says "superseded"; the underlying job row still ends
    # CANCELLED — a real terminal state, not a status nothing recognises.
    assert row["metadata"]["status"] == "superseded"
    job = asyncio.run(main.db.get_image_job(first["metadata"]["job_id"]))
    assert job["state"] in image_jobs.TERMINAL_STATES
    assert job["state"] == image_jobs.CANCELLED
    assert job["error"] == "superseded by a newer request"


def test_coalescing_does_not_cross_threads(env):
    c = env()
    tid_a, tid_b = _thread(c), _thread(c)
    _say(c, tid_a, "[[pic:scene one]]")
    a_first = _placeholders(c, tid_a)[0]
    _say(c, tid_b, "[[pic:scene two]]")

    assert _placeholders(c, tid_a)[0]["id"] == a_first["id"]
    assert _placeholders(c, tid_a)[0]["metadata"]["status"] == "queued"


def test_coalescing_via_the_explicit_endpoint_too(env):
    """The same coalesce rule applies to `/api/image-jobs`, not only markers —
    one chokepoint (`_supersede_stale_pic_jobs`), two entry points."""
    c = env()
    tid = _thread(c)
    first = _fire(c, tid, prompt="scene one").json()
    second = _fire(c, tid, prompt="scene two").json()

    first_job = c.get(f"/api/image-jobs/{first['job_id']}").json()
    second_job = c.get(f"/api/image-jobs/{second['job_id']}").json()
    assert first_job["state"] == "cancelled"
    assert second_job["state"] == "queued"
