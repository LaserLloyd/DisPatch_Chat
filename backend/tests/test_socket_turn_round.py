"""The rest of the socket-turn round: Safe Mode, bubbles, acks, backstops.

Three groups of properties, each with a failure that reports success:

* SAFE MODE. Streaming frames are now delivered to locked devices, so the
  bot-scoping and the redaction that `message` has always had must apply to
  them identically. A frame type nobody scoped is a frame type that leaks.

* ONE BUBBLE PER REPLY. A provisional bubble and its persisted row are two
  views of one message. If the row arrives as a plain `message` frame, or the
  simulated stream replays it a second time, the family reads it twice.

* THE DEAD BACKSTOPS. The file watcher/follower/reconciler/sweep read
  transcripts that OpenClaw 8.1 no longer writes. They did nothing, silently,
  while `_follow_session` alone burned thirty two-second sleeps per turn.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app import auth, config, main
from app.database import Database


@pytest.fixture
def app_client(tmp_path, monkeypatch):
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
    config._invalidate_bots_cache()

    temp_db = Database(tmp_path / "chats.db")
    monkeypatch.setattr(main, "db", temp_db)
    main._ACK_SEEN.clear()
    main._delivered.clear()
    main._provisional_runs.clear()
    main._inflight_runs.clear()
    turns: list[tuple] = []

    async def _fake_turn(thread_id, bot_id, text):
        turns.append((thread_id, bot_id, text))
    monkeypatch.setattr(main, "run_agent_turn", _fake_turn)

    with TestClient(main.app) as client:
        client.agent_turns = turns
        yield client

    asyncio.run(temp_db.close())


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """A real database + funnel with the broadcast captured (no TestClient)."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(config, "AVATAR_DIR", tmp_path / "avatars")
    config._invalidate_bots_cache()
    frames: list[dict] = []

    async def _capture(frame):
        frames.append(frame)

    monkeypatch.setattr(main.manager, "broadcast", _capture)
    db = Database(tmp_path / "chats.db")
    monkeypatch.setattr(main, "db", db)
    main._delivered.clear()
    main._provisional_runs.clear()
    main._canon_memo.clear()
    yield db, frames


# --------------------------------------------------------------------------- #
# Safe Mode
# --------------------------------------------------------------------------- #

def test_streaming_frames_are_scoped_by_bot_exactly_like_a_message():
    """The reason they were excluded was leakage, not the frame type.

    A locked device may watch a SAFE bot's reply arrive as it is typed, and
    must learn nothing at all about any other bot — the same rule `message`
    has always obeyed.
    """
    for kind in ("stream_start", "stream_chunk", "turn_status"):
        assert kind in main._DECOY_FRAME_ALLOW
        unsafe = {"type": kind, "thread_id": "t1", "bot_id": "main",
                  "text": "secret", "phase": "tool:x", "message_id": "run:1"}
        assert main.redact_for_decoy(unsafe) is None, (
            f"{kind} for a non-safe bot must not reach a locked device")
        safe = {**unsafe, "bot_id": "alpha"}
        assert main.redact_for_decoy(safe) is not None


def test_an_unattributed_streaming_frame_fails_closed():
    """Fail-closed is the direction: no bot means no delivery."""
    frame = {"type": "stream_chunk", "thread_id": "unknown-thread",
             "message_id": "run:1", "text": "hi"}
    assert main.redact_for_decoy(frame) is None


def test_a_delta_reaching_a_locked_device_carries_no_path_or_marker():
    """The explicit Safe-Mode requirement, end to end through the redactor."""
    raw = "look [[media:/srv/pics/x.png|c]] :react:morning: at this"
    clean = main._sanitize_delta(raw)
    frame = main.redact_for_decoy({"type": "stream_chunk", "thread_id": "t1",
                                   "bot_id": "alpha", "message_id": "run:1",
                                   "text": clean})
    assert frame is not None
    assert "/srv/pics" not in frame["text"]
    assert ":react:" not in frame["text"]
    assert "[[media:" not in frame["text"]


# --------------------------------------------------------------------------- #
# One bubble per reply
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_a_persisted_row_closes_the_bubble_it_was_streamed_into(wired):
    db, frames = wired
    await db.connect()
    try:
        thread = await db.create_thread(bot_id="main")
        main._open_provisional(thread.id, "run:abc")
        msg = await main._deliver_assistant_text(thread.id, "the finished reply")
        assert msg is not None
        done = [f for f in frames if f.get("type") == "stream_done"]
        assert done, "the row must REPLACE the bubble, not land beneath it"
        assert done[0]["provisional_id"] == "run:abc"
        assert done[0]["message"]["id"] == msg.id
        assert not [f for f in frames if f.get("type") == "message"], (
            "a plain message frame would leave the client holding both copies")
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_a_reply_with_no_bubble_still_arrives_as_a_plain_message(wired):
    db, frames = wired
    await db.connect()
    try:
        thread = await db.create_thread(bot_id="main")
        await main._deliver_assistant_text(thread.id, "no stream ran for this")
        assert [f["type"] for f in frames if f["type"] in
                ("message", "stream_done")] == ["message"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_the_simulated_stream_is_skipped_when_the_real_one_ran(wired):
    """Replaying a reply that was already typed out rewinds it and types it
    again — one animation per reply, whichever road it came down."""
    db, frames = wired
    await db.connect()
    try:
        thread = await db.create_thread(bot_id="main")
        long_reply = "The sea is grey and the sky is grey. " * 20
        main._open_provisional(thread.id, "run:xyz")
        await main._deliver_assistant_text(thread.id, long_reply, stream=True)
        assert not [f for f in frames if f["type"] == "stream_chunk"], (
            "the live deltas already painted this reply")
        assert [f["type"] for f in frames].count("stream_done") == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_the_simulated_stream_still_runs_on_the_fallback_path(wired):
    """With no live stream there is nothing on screen yet — animate it."""
    db, frames = wired
    await db.connect()
    try:
        thread = await db.create_thread(bot_id="main")
        await main._deliver_assistant_text(
            thread.id, "The sea is grey and the sky is grey. " * 20,
            stream=True)
        assert [f for f in frames if f["type"] == "stream_chunk"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_a_streamed_reply_moves_the_thread_list(wired):
    """The one message type that matters most did not update the sidebar:
    the streamed branch returned before _broadcast_thread_update."""
    db, frames = wired
    await db.connect()
    try:
        thread = await db.create_thread(bot_id="main")
        await main._deliver_assistant_text(
            thread.id, "The sea is grey and the sky is grey. " * 20,
            stream=True)
        assert [f for f in frames if f["type"] == "thread_update"], (
            "a thread's preview, unread dot and ordering must move for the "
            "agent's final reply")
    finally:
        await db.close()


def test_only_the_first_claimant_retires_a_bubble():
    main._provisional_runs.clear()
    main._open_provisional("t1", "run:1")
    assert main._close_provisional("t1", "run:1") is True
    assert main._close_provisional("t1", "run:1") is False, (
        "a second stream_done for a bubble that already has its message "
        "would blank it out")


def _row(role="assistant", metadata=None):
    return main.MessageOut(id="m1", thread_id="t1", role=role, content="x",
                           created_at=main.now_iso(), metadata=metadata)


def test_a_sub_row_landing_mid_stream_does_not_claim_the_bubble():
    """A reaction-fire notice / tool sub row persisted while the reply is
    still streaming used to take the provisional: the half-painted reply
    turned into a collapsed 'working…' line and the real reply then landed
    as a second row (staging, 2026-09-02)."""
    main._provisional_runs.clear()
    main._open_provisional("t1", "run:1")
    frame = main._landing_frame("t1", "main", _row(metadata={"sub": True}))
    assert frame["type"] == "message"
    assert main._provisional_open("t1", "run:1"), "bubble must stay claimable"
    frame = main._landing_frame("t1", "main", _row(role="user"))
    assert frame["type"] == "message", "a user echo is never the reply"
    assert main._provisional_open("t1", "run:1")
    frame = main._landing_frame("t1", "main", _row())
    assert frame["type"] == "stream_done" and frame["provisional_id"] == "run:1"
    assert not main._provisional_open("t1", "run:1")


# --------------------------------------------------------------------------- #
# The abridged twin: repair must never lose both copies
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_a_failed_upgrade_leaves_the_family_the_copy_they_already_read(
        wired, monkeypatch):
    """Deleting first meant every failure in between lost BOTH copies."""
    db, frames = wired
    await db.connect()
    try:
        thread = await db.create_thread(bot_id="main")
        abridged = "A" * 200
        await db.add_message(thread.id, "assistant", abridged)
        main._delivered.clear()

        async def _boom(*a, **kw):
            raise RuntimeError("database closed mid-upgrade")

        monkeypatch.setattr(main, "_persist_and_broadcast_message", _boom)
        with pytest.raises(RuntimeError):
            await main._deliver_assistant_text(thread.id, abridged + " and the rest")
        rows = await db.dump_messages(thread.id)
        assert [r.content for r in rows] == [abridged], (
            "the reply the family had already read must survive a failed "
            "repair of a duplicate")
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_a_successful_upgrade_still_leaves_exactly_one_copy(wired):
    db, frames = wired
    await db.connect()
    try:
        thread = await db.create_thread(bot_id="main")
        abridged = "B" * 200
        await db.add_message(thread.id, "assistant", abridged)
        main._delivered.clear()
        await main._deliver_assistant_text(thread.id, abridged + " and the rest")
        rows = [r for r in await db.dump_messages(thread.id)
                if r.role == "assistant"]
        assert len(rows) == 1 and rows[0].content.endswith("and the rest")
        assert [f for f in frames if f["type"] == "message_deleted"]
    finally:
        await db.close()


def test_twin_comparison_does_not_recanonicalize_canonical_text():
    """Both call sites hand this canonical strings; it was re-running half a
    dozen regex passes over them, once per candidate, per delivered message."""
    calls = {"n": 0}
    real = main._canon_msg

    def counted(s):
        calls["n"] += 1
        return real(s)

    main._canon_memo.clear()
    orig = main._canon_msg
    try:
        main._canon_msg = counted
        a, b = real("first message here"), real("second message here")
        main._is_twin(a, b)
        first = calls["n"]
        for _ in range(20):
            main._is_twin(a, b)
        assert calls["n"] == first, (
            "repeated comparisons must be memo hits, not 40 more regex passes")
    finally:
        main._canon_msg = orig
        main._canon_memo.clear()


# --------------------------------------------------------------------------- #
# The send-ack must mean "the turn ran", not "the row exists"
# --------------------------------------------------------------------------- #

def test_a_resend_of_a_stored_but_undispatched_message_runs_the_turn(app_client):
    """The failure ws.py documents: the ack was recorded before the dispatch,
    so a resend was answered with a bare re-ack and the bot never replied."""
    tid = app_client.post("/api/threads", json={"bot_id": "main"}).json()["id"]
    main._ACK_SEEN.clear()
    app_client.agent_turns.clear()
    # Exactly the state a stall between the ack and the dispatch leaves behind.
    main._ack_mark("cm-1", thread_id=tid, bot_id="main", text="are you there?")
    with app_client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"type": "send", "thread_id": tid, "text": "are you there?",
                      "client_msg_id": "cm-1"})
        for _ in range(6):
            if app_client.agent_turns:
                break
            ws.send_json({"type": "ping"})
            ws.receive_json()
    assert app_client.agent_turns == [(tid, "main", "are you there?")], (
        "a resend must finish the job, not confirm a turn that never started")


def test_a_normal_resend_is_still_never_dispatched_twice(app_client):
    tid = app_client.post("/api/threads", json={"bot_id": "main"}).json()["id"]
    main._ACK_SEEN.clear()
    app_client.agent_turns.clear()
    with app_client.websocket_connect("/ws") as ws:
        ws.receive_json()
        for _ in range(2):
            ws.send_json({"type": "send", "thread_id": tid, "text": "hello",
                          "client_msg_id": "cm-2"})
            ws.send_json({"type": "ping"})
            while ws.receive_json().get("type") != "pong":
                pass
    assert len(app_client.agent_turns) == 1


# --------------------------------------------------------------------------- #
# Abort
# --------------------------------------------------------------------------- #

class _AbortClient:
    def __init__(self):
        self.connected = asyncio.Event()
        self.connected.set()
        self.aborted: list[tuple] = []

    async def abort_run(self, session_key, run_id=None):
        self.aborted.append((session_key, run_id))
        return {"ok": True}


def test_abort_is_refused_in_safe_mode(app_client, monkeypatch):
    """Safe Mode is view and send. A locked tablet that could abort a turn
    could silence any conversation in the house."""
    tid = app_client.post("/api/threads", json={"bot_id": "alpha"}).json()["id"]
    auth.set_pin("135790")
    app_client.cookies.clear()
    monkeypatch.setattr(main, "_gateway_client", _AbortClient(), raising=False)
    assert app_client.post(f"/api/threads/{tid}/abort").status_code == 403


def test_abort_resolves_the_session_key_and_the_named_run(app_client, monkeypatch):
    tid = app_client.post("/api/threads", json={"bot_id": "main"}).json()["id"]
    fake = _AbortClient()
    monkeypatch.setattr(main, "_gateway_client", fake, raising=False)
    main._inflight_runs.clear()
    main._inflight_register(main._InflightRun(
        "run-9", f"agent:main:{tid}", tid, "main", time.time()))
    r = app_client.post(f"/api/threads/{tid}/abort")
    main._inflight_runs.clear()
    assert r.status_code == 200, r.text
    assert fake.aborted == [(f"agent:main:{tid}", "run-9")], (
        "scoping to the run keeps a queued followup from being cancelled too")


def test_abort_over_the_websocket_is_refused_in_safe_mode(app_client, monkeypatch):
    tid = app_client.post("/api/threads", json={"bot_id": "alpha"}).json()["id"]
    auth.set_pin("246802")
    app_client.cookies.clear()
    monkeypatch.setattr(main, "_gateway_client", _AbortClient(), raising=False)
    with app_client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["decoy"] is True
        ws.send_json({"type": "abort", "thread_id": tid})
        frame = ws.receive_json()
    assert frame["type"] == "error" and "Unlock" in frame["message"]


def test_abort_without_a_gateway_says_so_rather_than_pretending(app_client,
                                                                monkeypatch):
    tid = app_client.post("/api/threads", json={"bot_id": "main"}).json()["id"]
    monkeypatch.setattr(main, "_gateway_client", None, raising=False)
    assert app_client.post(f"/api/threads/{tid}/abort").status_code == 503


# --------------------------------------------------------------------------- #
# In-flight runs
# --------------------------------------------------------------------------- #

class _RecoverClient:
    def __init__(self):
        self.waited: list[str] = []

    async def agent_wait(self, run_id, *, timeout_ms):
        self.waited.append(run_id)
        return {"state": "final"}


class _RecoverRouter:
    def __init__(self):
        self.caught: list[tuple] = []

    async def catch_up(self, key, *, force=False):
        self.caught.append((key, force))


@pytest.mark.asyncio
async def test_a_reconnect_chases_every_run_it_lost_the_socket_on():
    """`agent.wait` says the run FINISHED. It finished into a subscription that
    no longer existed, which is the whole reason we are here — so the backfill
    runs whatever the wait answers."""
    main._inflight_runs.clear()
    main._inflight_register(main._InflightRun(
        "r1", "agent:main:t1", "t1", "main", time.time()))
    client, router = _RecoverClient(), _RecoverRouter()
    await main._recover_inflight_runs(client, router)
    main._inflight_runs.clear()
    assert client.waited == ["r1"]
    assert router.caught == [("agent:main:t1", True)], (
        "force, because a run stranded before its first transcript event has "
        "no seq cursor and used to be skipped")


def test_stale_in_flight_entries_do_not_accumulate_for_ever():
    main._inflight_runs.clear()
    main._inflight_register(main._InflightRun(
        "old", "agent:main:t1", "t1", "main",
        time.time() - main.INFLIGHT_TTL_S - 1))
    main._inflight_register(main._InflightRun(
        "new", "agent:main:t2", "t2", "main", time.time()))
    assert set(main._inflight_runs) == {"new"}
    main._inflight_runs.clear()


# --------------------------------------------------------------------------- #
# The mirror-state read that ran on every firehose event
# --------------------------------------------------------------------------- #

def test_mirror_state_is_not_re_parsed_for_every_gateway_event(tmp_path,
                                                               monkeypatch):
    """`_gateway_resolve_thread` reads this for EVERY event on a firehose that
    covers every session on the box — a synchronous read plus a JSON parse on
    the event loop, several times a second, to answer one membership test."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    main._mirror_state_cache.update({"path": None, "mtime": None, "data": None})
    main._save_mirror_state({"version": 1, "sessions": {"a|b": {"status": "muted"}}})
    reads = {"n": 0}
    real = type(tmp_path).read_text

    def counted(self, *a, **kw):
        if self.name == "gateway-mirror.json":
            reads["n"] += 1
        return real(self, *a, **kw)

    monkeypatch.setattr(type(tmp_path), "read_text", counted)
    for _ in range(50):
        assert main._load_mirror_state()["sessions"]["a|b"]["status"] == "muted"
    assert reads["n"] == 0, f"re-parsed {reads['n']} times"


def test_a_changed_mirror_state_file_is_still_picked_up(tmp_path, monkeypatch):
    """A cache that cannot notice a change is a cache that hides one."""
    import json

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    main._mirror_state_cache.update({"path": None, "mtime": None, "data": None})
    path = main._mirror_state_path()
    path.write_text(json.dumps({"version": 1, "sessions": {"x|y": {"status": "ok"}}}))
    assert "x|y" in main._load_mirror_state()["sessions"]
    time.sleep(0.01)
    path.write_text(json.dumps({"version": 1, "sessions": {"z|w": {"status": "muted"}}}))
    assert "z|w" in main._load_mirror_state()["sessions"]


# --------------------------------------------------------------------------- #
# The transcript backstops that no longer exist
# --------------------------------------------------------------------------- #

def test_a_host_with_no_transcripts_is_probed_once_and_said_out_loud(
        tmp_path, monkeypatch, caplog):
    """OpenClaw 8.1 moved sessions into sqlite. On such a host the watcher,
    follower, reconciler and sweep do nothing — silently, and at a cost."""
    monkeypatch.setattr(main.openclaw, "OPENCLAW_AGENTS_DIR", tmp_path / "agents")
    main._transcript_backstop.update({"probed": False, "available": True})
    with caplog.at_level("WARNING"):
        assert main._transcript_files_available() is False
        assert main._transcript_files_available() is False
    assert sum("session transcripts" in r.message for r in caplog.records) == 1, (
        "once per boot, not once per turn")
    assert main._transcript_backstop_state() == "unavailable"


def test_a_host_that_still_has_transcripts_keeps_its_backstops(tmp_path,
                                                               monkeypatch):
    """The code is not removed. A host with the files is still served by it."""
    root = tmp_path / "agents" / "main" / "sessions"
    root.mkdir(parents=True)
    (root / "sessions.json").write_text("{}")
    monkeypatch.setattr(main.openclaw, "OPENCLAW_AGENTS_DIR", tmp_path / "agents")
    main._transcript_backstop.update({"probed": False, "available": True})
    assert main._transcript_files_available() is True
    assert main._transcript_backstop_state() == "available"


@pytest.mark.asyncio
async def test_the_follower_does_not_sleep_spin_on_a_host_with_no_files(
        tmp_path, monkeypatch):
    """`_follow_session` polled 30 times at 2s for a file that cannot exist —
    a minute of a background task per turn, achieving nothing."""
    monkeypatch.setattr(main.openclaw, "OPENCLAW_AGENTS_DIR", tmp_path / "agents")
    main._transcript_backstop.update({"probed": False, "available": True})
    monkeypatch.setattr(main, "SETTINGS", replace(main.SETTINGS, gateway_ws="1"))
    monkeypatch.setattr(main, "_gateway_router", object(), raising=False)

    slept: list[float] = []

    async def _no_sleep(d):
        slept.append(d)

    monkeypatch.setattr(main.asyncio, "sleep", _no_sleep)
    await main._follow_session("t1", "main", "agent:main:t1")
    assert slept == []
    assert await main._reconcile_transcript("t1", "main", "agent:main:t1", {}) == []


@pytest.mark.asyncio
async def test_the_backstops_still_run_when_the_socket_is_not_delivering(
        tmp_path, monkeypatch):
    """Both halves matter. With no socket, a host with no transcripts has no
    delivery path at all and must not quietly stand down as well."""
    monkeypatch.setattr(main.openclaw, "OPENCLAW_AGENTS_DIR", tmp_path / "agents")
    main._transcript_backstop.update({"probed": False, "available": True})
    monkeypatch.setattr(main, "SETTINGS", replace(main.SETTINGS, gateway_ws=""))
    monkeypatch.setattr(main, "_gateway_router", None, raising=False)
    assert main._transcript_paths_dead() is False


# --------------------------------------------------------------------------- #
# The typing indicator must stop when the bot stops talking
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_the_indicator_stops_before_the_media_repair_pass(wired,
                                                                monkeypatch):
    """The second look is a repair over messages the family has ALREADY READ.

    Running it before the thread went idle held the typing indicator up for
    its whole duration — measured at 276 ms of the bot visibly still thinking
    after it had finished speaking.
    """
    from app import openclaw

    db, frames = wired
    await db.connect()
    try:
        thread = await db.create_thread(bot_id="main")
        monkeypatch.setattr(main, "SETTINGS",
                            replace(main.SETTINGS, gateway_ws="1"))
        monkeypatch.setattr(main, "_gateway_router", object(), raising=False)
        main._transcript_backstop.update({"probed": True, "available": False})

        async def _reply(bot_id, session_key, message, thread_id):
            return openclaw.AgentReply(
                payloads=[openclaw.AgentPayload(text="all done")],
                metadata={"model": "m"})

        monkeypatch.setattr(main, "_send_with_gateway_retry", _reply)

        seen_at_second_look: list[list[dict]] = []

        async def _second_look(*a, **kw):
            seen_at_second_look.append(list(frames))

        monkeypatch.setattr(main, "_media_second_look", _second_look)
        await main.run_agent_turn(thread.id, "main", "hello")

        assert seen_at_second_look, "the repair pass must still run"
        stopped = [f for f in seen_at_second_look[0]
                   if f.get("type") == "thinking" and f.get("status") == "stopped"]
        assert stopped, (
            "the indicator was still up while a post-delivery repair ran")
    finally:
        await db.close()


# --------------------------------------------------------------------------- #
# What an operator can see
# --------------------------------------------------------------------------- #

def test_health_reports_the_things_that_only_fail_silently(app_client,
                                                           monkeypatch):
    """Each of these had NO surface at all: in-flight runs that never clear,
    events the local queue dropped, and file backstops that do nothing."""
    class _Router:
        def __init__(self):
            self.stats = {"delivered": 0}

    class _Client:
        def __init__(self):
            self.connected = asyncio.Event()
            self.dropped_local = 4
            self.tick_closes = 2

    monkeypatch.setattr(main, "_gateway_router", _Router(), raising=False)
    monkeypatch.setattr(main, "_gateway_client", _Client(), raising=False)
    main._inflight_runs.clear()
    main._inflight_register(main._InflightRun(
        "r1", "agent:main:t1", "t1", "main", time.time()))
    stats = main._gateway_ws_stats()
    main._inflight_runs.clear()
    assert stats["inflight_runs"] == 1
    assert stats["dropped_local"] == 4
    assert stats["tick_closes"] == 2
    assert stats["transcript_backstop"] in ("available", "unavailable", "unprobed")
