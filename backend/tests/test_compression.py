"""Responses are compressed where it helps and nowhere it breaks (C20).

The thread list for a busy bot was ~242 KB per poll, uncompressed, because
every row carried the full body of its last message; static JS/CSS went out
uncompressed too. Fine on loopback, slow on a phone over the tailnet.

Pinned here: JSON is gzipped; pictures/video and Range (206) responses are
NOT (recompressing a PNG wastes CPU, and gzipping a byte range breaks video
seeking); the thread-list preview is bounded.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app import auth, config, main
from app.database import Database

LOOPBACK = ("127.0.0.1", 50000)
GZ = {"Accept-Encoding": "gzip"}


@pytest.fixture
def client(tmp_path, monkeypatch):
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
    config._invalidate_bots_cache()
    temp_db = Database(tmp_path / "chats.db")
    monkeypatch.setattr(main, "db", temp_db)
    main._delivered.clear()
    main._thread_bot.clear()
    with TestClient(main.app, client=LOOPBACK) as c:
        yield c
    asyncio.run(temp_db.close())


def _seed_long_threads(client, n=8):
    body = "word " * 4000   # ~20 KB per last message
    for _ in range(n):
        r = client.post("/api/threads", json={"bot_id": "main"})
        assert r.status_code == 200, r.text
        tid = r.json()["id"]
        r = client.post("/api/inject", json={"thread_id": tid, "content": body})
        assert r.status_code == 200, r.text


def test_thread_list_is_gzipped_and_previews_are_bounded(client):
    _seed_long_threads(client)
    r = client.get("/api/threads?bot_id=main", headers=GZ)
    assert r.status_code == 200
    assert r.headers.get("content-encoding") == "gzip"
    threads = r.json()["threads"]
    assert threads
    for t in threads:
        if t.get("last_message"):
            assert len(t["last_message"]) <= main.THREAD_PREVIEW_CHARS + 1


def test_no_gzip_without_accept_encoding(client):
    _seed_long_threads(client, n=2)
    r = client.get("/api/threads?bot_id=main",
                   headers={"Accept-Encoding": "identity"})
    assert "content-encoding" not in r.headers


def test_images_and_ranges_are_never_gzipped(client, tmp_path):
    media = tmp_path / "media"
    media.mkdir(exist_ok=True)
    (media / "big.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 50_000)
    (media / "clip.mp4").write_bytes(b"\0" * 50_000)
    assert main._gzip_skips(200, "image/png")
    assert main._gzip_skips(200, "video/mp4")
    assert main._gzip_skips(206, "application/json")
    assert not main._gzip_skips(200, "application/json")
    assert not main._gzip_skips(200, "text/javascript; charset=utf-8")
    r = client.get("/static/js/main.js", headers=GZ)
    assert r.status_code == 200
    assert r.headers.get("content-encoding") == "gzip"
    r = client.get("/static/js/main.js", headers={**GZ, "Range": "bytes=0-99"})
    assert r.status_code == 206
    assert "content-encoding" not in r.headers
    assert len(r.content) == 100
