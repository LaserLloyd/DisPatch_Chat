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
import zlib
from dataclasses import replace
from datetime import datetime, timedelta

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

    def __init__(self, *, enqueue=None, poll=None, fetch=None):
        self._enqueue, self._poll, self._fetch = enqueue, poll, fetch
        self.calls: list[str] = []

    async def enqueue(self, spec):
        self.calls.append("enqueue")
        if callable(self._enqueue):
            return self._enqueue(spec)
        if isinstance(self._enqueue, Exception):
            raise self._enqueue
        return image_jobs.EnqueueResult(job_id="rig-1")

    async def poll(self, rig_id):
        self.calls.append("poll")
        if isinstance(self._poll, Exception):
            raise self._poll
        return self._poll or image_jobs.PollResult(state="running", done=False)

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
    c = env()
    tid = _thread(c)
    for _ in range(image_jobs.RATE_LIMIT):
        assert _fire(c, tid).status_code == 202
    r = _fire(c, tid)
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
    assert forge.calls == ["enqueue", "poll", "fetch"]


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
