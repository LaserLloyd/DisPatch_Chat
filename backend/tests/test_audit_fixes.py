"""Regression tests for the 2026-07 audit fixes (backend).

Covers: the WS send-ack protocol (ok / rejected / duplicate-resend dedup),
per-message WS handler error isolation, backup corrupt-snapshot rotation, and
the extended /api/health payload. Same hermetic style as the rest of tests/:
throwaway DB + monkeypatched data dirs, nothing touches the live stack.
Run: cd backend && uv run pytest.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import auth, config, main
from app.database import Database


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    """TestClient with full isolation (mirrors test_comfy_routes.route_client)."""
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
    # Agent turns are stubbed — these tests exercise the WS protocol only.
    turns: list[tuple] = []

    async def _fake_turn(thread_id, bot_id, text):
        turns.append((thread_id, bot_id, text))
    monkeypatch.setattr(main, "run_agent_turn", _fake_turn)

    with TestClient(main.app) as client:
        client.agent_turns = turns
        yield client

    asyncio.run(temp_db.close())


def _mk_thread(client: TestClient, bot_id: str = "main") -> str:
    r = client.post("/api/threads", json={"bot_id": bot_id})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _recv_until(ws, mtype: str, tries: int = 10) -> dict:
    for _ in range(tries):
        frame = ws.receive_json()
        if frame.get("type") == mtype:
            return frame
    raise AssertionError(f"no {mtype!r} frame received")


# --------------------------------------------------------------------------- #
# WS send-ack protocol
# --------------------------------------------------------------------------- #


def test_ws_send_ack_ok_and_echo_carries_client_msg_id(app_client):
    tid = _mk_thread(app_client)
    with app_client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "hello"
        ws.send_json({"type": "send", "thread_id": tid, "text": "hello there",
                      "client_msg_id": "c-abc"})
        ack = _recv_until(ws, "ack")
        assert ack == {"type": "ack", "client_msg_id": "c-abc", "status": "ok"}
        # The persisted-user-message broadcast carries the id (secondary clear).
        msg = _recv_until(ws, "message")
        assert msg["client_msg_id"] == "c-abc"
        assert msg["message"]["content"] == "hello there"
        # Drain the thread_update so the handler has fully completed before the
        # TestClient close cancels the server-side session task.
        _recv_until(ws, "thread_update")
    r = app_client.get(f"/api/threads/{tid}/messages")
    contents = [m["content"] for m in r.json()["messages"]]
    assert contents == ["hello there"]
    for _ in range(40):                    # the turn task runs on the app loop
        if app_client.agent_turns:
            break
        time.sleep(0.05)
    assert app_client.agent_turns and app_client.agent_turns[0][0] == tid


def test_ws_send_ack_rejected_thread_not_found(app_client):
    with app_client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "hello"
        ws.send_json({"type": "send", "thread_id": "nope", "text": "hi",
                      "client_msg_id": "c-rej"})
        ack = _recv_until(ws, "ack")
        assert ack["status"] == "rejected"
        assert ack["client_msg_id"] == "c-rej"
        assert ack["reason"] == "Thread not found"
    assert app_client.agent_turns == []


def test_ws_send_duplicate_resend_acks_ok_but_persists_once(app_client):
    tid = _mk_thread(app_client)
    with app_client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "hello"
        frame = {"type": "send", "thread_id": tid, "text": "only once",
                 "client_msg_id": "c-dup"}
        ws.send_json(frame)
        assert _recv_until(ws, "ack")["status"] == "ok"
        _recv_until(ws, "message")
        # Reconnect resend: same frame, same id — ack ok again, no second row.
        ws.send_json(frame)
        ack2 = _recv_until(ws, "ack")
        assert ack2 == {"type": "ack", "client_msg_id": "c-dup", "status": "ok"}
    r = app_client.get(f"/api/threads/{tid}/messages")
    assert [m["content"] for m in r.json()["messages"]] == ["only once"]
    for _ in range(40):
        if app_client.agent_turns:
            break
        time.sleep(0.05)
    assert len(app_client.agent_turns) == 1


def test_ws_send_without_client_msg_id_gets_no_ack(app_client):
    """Backwards compatible: an old client (no client_msg_id) sees no ack frame."""
    tid = _mk_thread(app_client)
    with app_client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "hello"
        ws.send_json({"type": "send", "thread_id": tid, "text": "legacy"})
        msg = _recv_until(ws, "message")     # would raise if an ack came instead
        assert "client_msg_id" not in msg


# --------------------------------------------------------------------------- #
# WS handler error isolation
# --------------------------------------------------------------------------- #


def test_ws_handler_exception_sends_neutral_error_and_keeps_socket(app_client, monkeypatch):
    async def _boom(ws, data):
        raise RuntimeError("secret internal detail /srv/private/leak.sql")
    monkeypatch.setitem(main.WS_HANDLERS, "boom", _boom)
    with app_client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "hello"
        ws.send_json({"type": "boom", "thread_id": None})
        err = _recv_until(ws, "error")
        assert err["message"] == "Server error — please retry."
        assert "secret" not in str(err) and "leak.sql" not in str(err)
        # The connection survived the exception.
        ws.send_json({"type": "ping"})
        assert _recv_until(ws, "pong")["type"] == "pong"


# --------------------------------------------------------------------------- #
# Backup rotation: corrupt snapshots never evict known-good ones
# --------------------------------------------------------------------------- #


async def test_backup_corrupt_snapshot_renamed_and_good_one_pinned(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path / "backups")
    config.BACKUP_DIR.mkdir(parents=True)
    db = Database(tmp_path / "chats.db")
    await db.connect()
    try:
        monkeypatch.setattr(main, "db", db)
        monkeypatch.setattr(main, "_last_good_backup", None)
        monkeypatch.setattr(main, "_last_backup_ok", None)
        monkeypatch.setattr(main, "_last_backup_at", None)

        good = await main._make_backup()
        assert good is not None and good.exists()
        assert main._last_backup_ok is True
        assert main._last_good_backup == good

        # Live DB "corrupt": every subsequent snapshot fails verification.
        async def _bad_backup(dest: Path):
            dest.write_bytes(b"this is not a sqlite database")
        monkeypatch.setattr(db, "backup_to", _bad_backup)
        time.sleep(1.1)                    # distinct per-second snapshot stamp
        bad = await main._make_backup()
        assert bad is None
        assert main._last_backup_ok is False
        assert main._last_backup_at is not None
        # Failed snapshot renamed aside; the good one untouched.
        assert good.exists(), "verified-good snapshot was destroyed"
        assert list(config.BACKUP_DIR.glob("chats-*.db")) == [good]
        assert len(list(config.BACKUP_DIR.glob("chats-*.db.corrupt"))) == 1
    finally:
        await db.close()


def test_prune_preserves_pinned_good_snapshot_and_caps_corrupt(tmp_path, monkeypatch):
    bdir = tmp_path / "backups"
    bdir.mkdir(parents=True)
    monkeypatch.setattr(config, "BACKUP_DIR", bdir)
    monkeypatch.setattr(main, "SETTINGS", replace(config.SETTINGS, backup_keep=2))
    snaps = []
    for i in range(5):
        p = bdir / f"chats-2026070{i}-000000.db"
        p.write_bytes(b"x")
        snaps.append(p)
    for i in range(4):
        (bdir / f"chats-2026060{i}-000000.db.corrupt").write_bytes(b"x")
    # Pin the OLDEST snapshot as the last verified-good one.
    monkeypatch.setattr(main, "_last_good_backup", snaps[0])

    main._prune_backups()

    remaining = sorted(bdir.glob("chats-*.db"))
    # keep=2 newest + the pinned good one survive; middle ones pruned.
    assert remaining == [snaps[0], snaps[3], snaps[4]]
    corrupt = sorted(bdir.glob("chats-*.db.corrupt"))
    assert len(corrupt) == 2, "corrupt snapshots must be capped at 2"


# --------------------------------------------------------------------------- #
# Crash-recovery import: a lost reply repeating earlier text is still recovered
# --------------------------------------------------------------------------- #


async def test_crashed_turn_import_recovers_repeated_reply(tmp_path, monkeypatch, monkeypatch_root):
    import json as _json

    from app import openclaw

    db = Database(tmp_path / "chats.db")
    await db.connect()
    try:
        monkeypatch.setattr(main, "db", db)
        main._delivered.clear()

        async def _noop(_frame):
            return None
        monkeypatch.setattr(main.manager, "broadcast", _noop)
        openclaw.OPENCLAW_AGENTS_DIR = monkeypatch_root

        th = await db.create_thread(bot_id="main")
        key = openclaw.session_key_for("main", th.id)
        # Transcript: two turns whose replies are TEXTUALLY IDENTICAL ("Done.").
        sdir = monkeypatch_root / "main" / "sessions"
        sdir.mkdir(parents=True)
        (sdir / "sessions.json").write_text(_json.dumps({key: {"sessionId": "sid-c"}}))
        lines = []
        for _ in range(2):
            lines.append({"type": "message", "message": {"role": "user",
                          "content": [{"type": "text", "text": "status?"}]}})
            lines.append({"type": "message", "message": {"role": "assistant",
                          "content": [{"type": "text", "text": "Done."}]}})
        (sdir / "sid-c.jsonl").write_text("\n".join(_json.dumps(x) for x in lines) + "\n")

        # DB state at crash time: turn 1 fully delivered, turn 2's reply LOST.
        await db.add_message(th.id, "user", "status?")
        await db.add_message(th.id, "assistant", "Done.")
        await db.add_message(th.id, "user", "status?")

        # Whole-history dedup (manual /api/recover sweep) skips the repeat…
        assert await main._import_transcript_messages(th.id, "main") == 0
        # …but crash recovery restricts dedup to the stranded turn and recovers it.
        n = await main._import_transcript_messages(th.id, "main", crashed_turn=True)
        assert n == 1, "crashed turn's repeated reply was not recovered"
        msgs = await db.dump_messages(th.id)
        assert [m.content for m in msgs] == ["status?", "Done.", "status?", "Done."]
        # Idempotent: a second crash-recovery pass adds nothing (the reply is
        # now in the trailing assistant run).
        main._delivered.clear()
        assert await main._import_transcript_messages(th.id, "main", crashed_turn=True) == 0
    finally:
        await db.close()


# --------------------------------------------------------------------------- #
# /api/health degraded-state surfacing
# --------------------------------------------------------------------------- #


def test_health_surfaces_integrity_backup_and_gateway(app_client, monkeypatch):
    monkeypatch.setattr(main, "_last_backup_ok", True)
    monkeypatch.setattr(main, "_last_backup_at", "2026-07-07T06:00:00")
    # Prime the probe cache (ts far in the future ⇒ cache hit, no real socket).
    monkeypatch.setattr(main, "_gateway_probe", {"ts": 1e18, "ok": True})
    r = app_client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["db_integrity_ok"] is True          # set by startup recovery
    assert body["last_backup_ok"] is True
    assert body["last_backup_at"] == "2026-07-07T06:00:00"
    assert body["gateway_ok"] is True


@pytest.mark.asyncio
async def test_thread_preview_skips_tool_warning_sub_messages(tmp_path):
    """The chat-list preview must show what the AGENT SAID, not runtime narration.

    A turn whose last stored row is a failed tool call ("⚠️ 🛠️ Exec failed: …")
    is persisted as a `sub` message and collapsed in the bubble view. The
    thread-list preview took the newest row unconditionally, so those threads
    advertised an internal error while the real reply sat one row above it.
    Reproduced against the live database: 5 of 100 threads previewed a tool
    warning.
    """
    from app.database import Database

    db = Database(tmp_path / "t.db")
    await db.connect()
    try:
        t = await db.create_thread(bot_id="main", title="preview test")
        await db.add_message(thread_id=t.id, role="user", content="do the thing")
        await db.add_message(thread_id=t.id, role="assistant",
                             content="Here is the real answer.")
        await db.add_message(
            thread_id=t.id, role="assistant",
            content="⚠️ 🛠️ Exec failed: `show ~/nope.md` (exit 1)",
            metadata={"sub": True})

        got = await db.get_thread(t.id)
        assert got is not None
        assert got.last_message == "Here is the real answer.", (
            f"preview leaked runtime narration: {got.last_message!r}")

        listed = await db.all_threads()
        mine = [x for x in listed if x.id == t.id]
        assert mine and mine[0].last_message == "Here is the real answer."

        # The sub row is still STORED — this is a display rule, not deletion.
        msgs = await db.dump_messages(t.id)
        assert any("Exec failed" in (m.content or "") for m in msgs)
    finally:
        await db.close()


def test_health_withholds_recon_from_an_unauthenticated_caller(app_client, monkeypatch):
    """/api/health is un-gated on purpose (the container healthcheck reads it
    before any PIN exists), but the absolute data dir, the gateway's
    reachability and the client count are operator detail — free recon on a
    0.0.0.0-bound service."""
    monkeypatch.setattr(main, "_gateway_probe", {"ts": 1e18, "ok": True})
    auth.set_pin("1234")                     # PIN set, never unlocked

    body = app_client.get("/api/health").json()
    # What the Docker HEALTHCHECK reads must still be there.
    assert body["status"] == "ok"
    assert body["db_integrity_ok"] is True
    for leak in ("data_dir", "gateway_ok", "clients", "openclaw_available",
                 "last_backup_at"):
        assert leak not in body, f"{leak} leaked to an unauthenticated caller"

    app_client.post("/api/auth/unlock", json={"pin": "1234"})
    full = app_client.get("/api/health").json()
    assert full["data_dir"] and full["gateway_ok"] is True
    assert "clients" in full
