"""Locked-side one-way drop (`POST /api/drop`).

The drop is the ONLY upload entry point a Safe-Mode device gets on its own, so
these tests pin the two halves of its contract:

  * it works without a session (that is the whole point), and
  * it stays one-way — the sender can never read back what it just sent.

Same hermetic style as the rest of tests/: throwaway DB + monkeypatched data
dirs, nothing touches the live data dir or ~/.openclaw.
Run: cd backend && uv run pytest tests/test_drop.py
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app import auth, config, main
from app.database import Database


@pytest.fixture
def drop_env(tmp_path, monkeypatch):
    """Isolated data dir + DB, PIN set (so unauthenticated == Safe Mode)."""
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
    main._thread_bot.clear()
    main._decoy_upload_used.clear()

    with TestClient(main.app, client=("testclient", 50000)) as client:
        auth.set_pin("1234")
        auth._bust_cache()
        # 4.0: an unsigned device is the lock-target default profile (or nobody, who can
        # do nothing): the drop is that shared profile's.
        from app import accounts
        lobby = accounts.create_profile("Lobby", "user")
        auth.set_lock_target("default_profile", lobby.id)
        yield client
    asyncio.run(temp_db.close())


def _drop(client, name="report.pdf", body=b"hello", **data):
    return client.post("/api/drop", files={"file": (name, body, "application/pdf")},
                       data=data)


def _unlock(client):
    r = client.post("/api/auth/unlock", json={"pin": "1234"})
    assert r.status_code == 200, r.text
    return r


# --------------------------------------------------------------------------- #
# The point of the feature: a locked device can send.
# --------------------------------------------------------------------------- #

def test_locked_device_can_drop_without_a_session(drop_env):
    r = _drop(drop_env)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "report.pdf"
    assert body["size"] == 5
    assert body["notice"] is True          # posted into a safe bot's daily thread
    assert body["thread_id"]


def test_drop_posts_a_notice_naming_the_file(drop_env):
    r = _drop(drop_env, name="taxes.pdf", body=b"x" * 2048)
    tid = r.json()["thread_id"]
    _unlock(drop_env)                       # read it back as the operator
    msgs = drop_env.get(f"/api/threads/{tid}/messages").json()["messages"]
    notice = [m for m in msgs if (m.get("metadata") or {}).get("kind") == "drop"]
    assert len(notice) == 1
    content = notice[0]["content"]
    assert "taxes.pdf" in content
    assert "2.0 KB" in content
    assert notice[0]["role"] == "system"
    # Text-only: a drop is never inline chat media.
    assert not notice[0]["media_url"]


def test_the_notice_lands_in_a_chat_the_sender_cannot_read(drop_env):
    """4.0: the notice (with its [[doc:]] link) goes to the protected Admin's daily thread;
    the sender - here the shared default profile - cannot open that chat (404)."""
    tid = _drop(drop_env, name="secret.pdf").json()["thread_id"]
    r = drop_env.get(f"/api/threads/{tid}/messages", headers={"origin": "http://testserver"})
    assert r.status_code == 404
    _unlock(drop_env)
    full = drop_env.get(f"/api/threads/{tid}/messages").json()["messages"]
    full_notice = [m for m in full if (m.get("metadata") or {}).get("kind") == "drop"][0]
    assert "[[doc:" in full_notice["content"]


# --------------------------------------------------------------------------- #
# One-way: sending must not grant reading.
# --------------------------------------------------------------------------- #

def test_sender_cannot_read_back_what_it_dropped(drop_env):
    fid = _drop(drop_env).json()["id"]
    # The blobs stay unreadable outright (403). The LIST joined the machine
    # surface 2026-08-14 — for this remote keyless sender that means 401 from
    # the API-key check instead of the old decoy 403: refused either way, and
    # still one-way (browser-403 / remote-401 / loopback-agents-only pinned in
    # test_inbound_thread_mgmt.py).
    for path in (f"/api/files/{fid}/download", f"/api/files/{fid}/raw"):
        assert drop_env.get(path).status_code == 403, path
    assert drop_env.get("/api/files").status_code == 401


def test_dropped_file_is_tagged_fileserver_not_chat(drop_env):
    """`source='fileserver'` keeps `_block_fileserver_read` barring the blob even
    if the /api/files* path gate were ever loosened."""
    fid = _drop(drop_env).json()["id"]
    _unlock(drop_env)
    files = drop_env.get("/api/files").json()["files"]
    rec = [f for f in files if f["id"] == fid][0]
    assert rec["source"] == "fileserver"


# --------------------------------------------------------------------------- #
# Thread targeting: a locked caller may not write into a non-safe conversation.
# --------------------------------------------------------------------------- #

def test_locked_caller_cannot_target_an_unsafe_bots_thread(drop_env):
    _unlock(drop_env)
    unsafe = drop_env.post("/api/threads", json={"bot_id": "main"}).json()
    assert unsafe["bot_id"] == "main"
    drop_env.post("/api/auth/lock")

    landed = _drop(drop_env, thread_id=unsafe["id"]).json()["thread_id"]
    # Silently redirected to the default safe thread rather than honoured.
    assert landed != unsafe["id"]

    _unlock(drop_env)
    msgs = drop_env.get(f"/api/threads/{unsafe['id']}/messages").json()["messages"]
    assert not [m for m in msgs if (m.get("metadata") or {}).get("kind") == "drop"]


def test_drop_honours_a_thread_the_sender_owns(drop_env):
    import yaml
    doc = yaml.safe_load(config.CONFIG_PATH.read_text()) or {}
    doc.setdefault("bots", []).append({"id": "chatty", "name": "Chatty", "safe": True, "advisor": {
        "providers": [{"provider": "openai", "model": "m", "base_url": "http://127.0.0.1:9/v1"}]}})
    config.CONFIG_PATH.write_text(yaml.safe_dump(doc))
    config._invalidate_bots_cache()
    mine = drop_env.post("/api/threads", json={"bot_id": "chatty"},
                         headers={"origin": "http://testserver"}).json()
    assert _drop(drop_env, thread_id=mine["id"]).json()["thread_id"] == mine["id"]


def test_unknown_thread_id_falls_back_rather_than_failing(drop_env):
    body = _drop(drop_env, thread_id="does-not-exist").json()
    assert body["notice"] is True
    assert body["thread_id"]


# --------------------------------------------------------------------------- #
# Availability guards.
# --------------------------------------------------------------------------- #

def test_oversized_drop_is_refused(drop_env, monkeypatch):
    monkeypatch.setattr(main, "UPLOAD_MAX_DOC", 1024)
    r = _drop(drop_env, body=b"x" * 4096)
    assert r.status_code == 413


def test_a_failed_drop_stores_nothing(drop_env, monkeypatch):
    monkeypatch.setattr(main, "UPLOAD_MAX_DOC", 1024)
    _drop(drop_env, body=b"x" * 4096)
    _unlock(drop_env)
    assert drop_env.get("/api/files").json()["files"] == []
    assert not list((config.FILES_DIR).glob("*")) or \
        not [p for p in config.FILES_DIR.glob("*") if p.suffix != ".part"]


def test_filename_is_not_a_path_traversal(drop_env):
    r = _drop(drop_env, name="../../../etc/passwd")
    assert r.status_code == 200
    assert r.json()["name"] == "passwd"
    # Nothing escaped the store.
    assert not (config.FILES_DIR.parent / "passwd").exists()
