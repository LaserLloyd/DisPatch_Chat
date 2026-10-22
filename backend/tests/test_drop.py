"""The File Server drop (`POST /api/drop`, ``files_upload``).

4.0 pins:

  * every role may drop (an unsigned device too: it is the lock-target default profile);
  * the file is the dropper's (``source='fileserver'``): a Basic dropper can never read
    it back (no ``files_read``), and nobody else can either;
  * the chat notice lands only in a chat the dropper owns - the protected Admin's drop
    falls back to its daily thread, anyone else's falls back to no notice at all.

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
    """Isolated data dir + DB, PIN set (so unauthenticated == the default profile)."""
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
# Every role may drop; the notice follows the dropper.
# --------------------------------------------------------------------------- #

def test_an_unsigned_device_can_drop_as_the_default_profile(drop_env):
    r = _drop(drop_env)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "report.pdf"
    assert body["size"] == 5
    # Not the protected Admin's file, and no chat of its own named: no notice anywhere.
    assert body["notice"] is False and body["thread_id"] is None


def test_drop_posts_a_notice_naming_the_file(drop_env):
    _unlock(drop_env)                       # the protected Admin drops
    r = _drop(drop_env, name="taxes.pdf", body=b"x" * 2048)
    tid = r.json()["thread_id"]
    assert tid
    msgs = drop_env.get(f"/api/threads/{tid}/messages").json()["messages"]
    notice = [m for m in msgs if (m.get("metadata") or {}).get("kind") == "drop"]
    assert len(notice) == 1
    content = notice[0]["content"]
    assert "taxes.pdf" in content
    assert "2.0 KB" in content
    assert notice[0]["role"] == "system"
    # Text-only: a drop is never inline chat media.
    assert not notice[0]["media_url"]


def test_nobodys_file_name_lands_in_someone_elses_chat(drop_env):
    """4.0: the default profile's drop never posts into the protected Admin's chats (the file
    name is the dropper's business): named or not, no notice there."""
    _unlock(drop_env)
    admins = drop_env.post("/api/threads", json={"bot_id": "main"}).json()["id"]
    drop_env.post("/api/auth/lock")
    body = _drop(drop_env, name="secret.pdf", thread_id=admins).json()
    assert body["thread_id"] is None and body["notice"] is False
    _unlock(drop_env)
    msgs = drop_env.get(f"/api/threads/{admins}/messages").json()["messages"]
    assert not [m for m in msgs if (m.get("metadata") or {}).get("kind") == "drop"]


# --------------------------------------------------------------------------- #
# One-way: sending must not grant reading.
# --------------------------------------------------------------------------- #

def test_a_basic_dropper_cannot_read_back_what_it_dropped(drop_env):
    from app import accounts
    lobby = next(p for p in accounts.list_profiles() if p.name == "Lobby")
    accounts.update_profile(lobby.id, role="basic")      # an Admin made the default profile Basic
    fid = _drop(drop_env).json()["id"]
    for path in (f"/api/files/{fid}/download", f"/api/files/{fid}/raw"):
        assert drop_env.get(path, headers={"origin": "http://testserver"}).status_code == 403, path
    # The LIST is on the machine surface: this keyless remote caller is refused by the
    # API-key check (401) - or, as a browser, by files_read (403).
    assert drop_env.get("/api/files").status_code in (401, 403)
    # ...and the protected Admin cannot open it either: it is the dropper's file.
    _unlock(drop_env)
    assert drop_env.get(f"/api/files/{fid}/download").status_code == 404


def test_dropped_file_is_tagged_fileserver_not_chat(drop_env):
    """`source='fileserver'` is what makes `_file_access` ask for files_read."""
    _unlock(drop_env)
    fid = _drop(drop_env).json()["id"]
    files = drop_env.get("/api/files").json()["files"]
    rec = [f for f in files if f["id"] == fid][0]
    assert rec["source"] == "fileserver"


# --------------------------------------------------------------------------- #
# Thread targeting: only a chat the dropper owns.
# --------------------------------------------------------------------------- #

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
    _unlock(drop_env)                       # the protected Admin: its daily thread
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
