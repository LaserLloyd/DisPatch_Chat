"""The property the persisted outbox is built on top of.

The client now keeps unacked `send` frames in IndexedDB and replays them
unchanged after a reload — the same `client_msg_id`, the same text, on a
brand-new socket. That is only safe because the server treats the id as the
identity of the message rather than of the connection it arrived on.

`test_socket_turn_round.py` already pins the TURN half of this: a resend is
never dispatched twice. This file pins the half the outbox actually leans on
and nothing asserted before — the resend must not leave a SECOND COPY of the
message in the thread. A client that replays into a server without this would
double-post the family's messages on every reload, and would do it silently:
both copies are real rows, both look sent, nothing errors.

Honest about what this is: it verifies an existing guarantee rather than fixing
a bug. It passes against the pre-change server, and it is here so that a future
change to the ack path cannot quietly take the guarantee away while the client
is relying on it.
"""
from __future__ import annotations

import asyncio

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


def _drain(ws, kind: str = "pong") -> list[dict]:
    """Send a ping and collect everything up to its pong — the handler's work
    is queued behind our frame, so the pong is the marker that it is done."""
    ws.send_json({"type": "ping"})
    seen: list[dict] = []
    while True:
        frame = ws.receive_json()
        if frame.get("type") == kind:
            return seen
        seen.append(frame)


def test_a_replayed_outbox_frame_is_acked_but_not_stored_twice(app_client):
    tid = app_client.post("/api/threads", json={"bot_id": "main"}).json()["id"]
    frame = {
        "type": "send",
        "thread_id": tid,
        "text": "did this go through?",
        "client_msg_id": "c-outbox-1",
    }

    # First socket: the message is typed and sent. Then the tab goes away with
    # the frame still in the outbox — an ack that never made it back, a tunnel
    # that dropped, a phone that was put in a pocket.
    with app_client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json(dict(frame))
        _drain(ws)

    # Second socket: a fresh boot replays the persisted frame UNCHANGED. Same
    # id, same text — this is exactly what resendPendingSends() puts on the
    # wire, and nothing about it says "this is a replay".
    with app_client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json(dict(frame))
        acks = [f for f in _drain(ws) if f.get("type") == "ack"]

    assert acks and acks[0]["status"] == "ok", (
        "a replayed frame must be acked, or the client keeps it queued forever "
        "and retries it on every reconnect")

    msgs = app_client.get(f"/api/threads/{tid}/messages").json()["messages"]
    users = [m for m in msgs if m["role"] == "user"]
    assert len(users) == 1, (
        "the replay stored a SECOND copy of the message. The persisted outbox "
        "replays frames unchanged and relies on client_msg_id being the "
        "message's identity; without that, every reload double-posts.")
    assert users[0]["content"] == "did this go through?"

    assert len(app_client.agent_turns) == 1, (
        "the replay started a second turn for one message")


def test_two_different_ids_with_the_same_text_are_two_messages(app_client):
    """The other direction, so the dedup above cannot be 'passing' by matching
    on text. Sending the same words twice on purpose is an ordinary thing to do
    and must still produce two messages."""
    tid = app_client.post("/api/threads", json={"bot_id": "main"}).json()["id"]
    with app_client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"type": "send", "thread_id": tid, "text": "ping",
                      "client_msg_id": "c-a"})
        _drain(ws)
        ws.send_json({"type": "send", "thread_id": tid, "text": "ping",
                      "client_msg_id": "c-b"})
        _drain(ws)

    msgs = app_client.get(f"/api/threads/{tid}/messages").json()["messages"]
    users = [m for m in msgs if m["role"] == "user"]
    assert len(users) == 2, "dedup swallowed a genuine repeat of the same text"
