"""bot_id case-forgiveness on the agent-facing REST boundary.

The gateway lowercases whole session keys, so agents echo lowercase bot ids
(`scout`, `ai_swift`) back at endpoints whose roster ids are mixed-case.
Thread *resolution* was made case-insensitive for exactly this reason; these
tests pin the same rule for bot_id — and, critically, that the CANONICAL id is
what gets persisted, so a lowercased id can never fork `daily-scout-…` off
`daily-Scout-…`.

Same hermetic style as the rest of tests/: throwaway DB + monkeypatched data
dirs. Run: cd backend && uv run pytest.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app import auth, config, main
from app.database import Database

LOOPBACK = ("127.0.0.1", 50000)


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Loopback TestClient over an isolated data dir (inbound-exempt caller)."""
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
    main._ACK_SEEN.clear()
    main._delivered.clear()
    main._thread_bot.clear()

    with TestClient(main.app, client=LOOPBACK) as c:
        yield c
    asyncio.run(temp_db.close())


# --------------------------------------------------------------------------- #
# The resolver itself
# --------------------------------------------------------------------------- #

def test_resolve_bot_exact_and_case_insensitive(client):
    assert config.resolve_bot("Scout").id == "Scout"
    assert config.resolve_bot("scout").id == "Scout"
    assert config.resolve_bot("AI_SWIFT").id == "AI_Swift"
    assert config.resolve_bot("atlas").id == "Atlas"
    assert config.resolve_bot("main").id == "main"
    assert config.resolve_bot("no-such-bot") is None
    assert config.resolve_bot(None) is None
    assert config.resolve_bot("") is None


# --------------------------------------------------------------------------- #
# /api/inject and /api/daily: the canonical id creates the daily thread
# --------------------------------------------------------------------------- #

def test_inject_lowercase_bot_id_does_not_fork_daily_thread(client):
    r1 = client.post("/api/inject", json={"bot_id": "scout", "content": "one"})
    assert r1.status_code == 200, r1.text
    tid = r1.json()["thread_id"]
    assert tid.startswith("daily-Scout-"), tid

    r2 = client.post("/api/inject", json={"bot_id": "Scout", "content": "two"})
    assert r2.status_code == 200
    assert r2.json()["thread_id"] == tid           # same thread, no fork


def test_daily_lowercase_bot_id_resolves_canonical(client):
    r = client.post("/api/daily", json={"bot_id": "ai_swift"})
    assert r.status_code == 200, r.text
    thread = r.json()["thread"]
    assert thread["bot_id"] == "AI_Swift"
    assert thread["id"].startswith("daily-AI_Swift-")


def test_inject_unknown_bot_id_still_400(client):
    r = client.post("/api/inject", json={"bot_id": "no-such-bot", "content": "x"})
    assert r.status_code == 400


# --------------------------------------------------------------------------- #
# REST thread create/list: canonical id persisted, lowercase filter finds it
# --------------------------------------------------------------------------- #

def test_create_thread_rest_persists_canonical_bot_id(client):
    r = client.post("/api/threads", json={"bot_id": "atlas", "title": "case test"})
    assert r.status_code == 200, r.text
    assert r.json()["bot_id"] == "Atlas"

    listed = client.get("/api/threads", params={"bot_id": "atlas"})
    assert listed.status_code == 200
    ids = [t["id"] for t in listed.json()["threads"]]
    assert r.json()["id"] in ids


# --------------------------------------------------------------------------- #
# The `text` alias + the empty-bubble trap
#
# `content` defaults to "", so a caller sending `text` (every other chat
# transport's field name) used to persist an EMPTY message: the agent believed
# it posted, the family saw a blank bubble. Now `text` is accepted as an alias
# and a message with no content AND no media is refused out loud.
# --------------------------------------------------------------------------- #

def test_inject_text_alias_is_accepted(client):
    r = client.post("/api/inject", json={"bot_id": "main", "text": "aliased"})
    assert r.status_code == 200, r.text
    assert r.json()["message"]["content"] == "aliased"


def test_inject_explicit_content_beats_text(client):
    r = client.post("/api/inject",
                    json={"bot_id": "main", "content": "real", "text": "decoy"})
    assert r.status_code == 200
    assert r.json()["message"]["content"] == "real"


def test_inject_empty_message_is_refused(client):
    r = client.post("/api/inject", json={"bot_id": "main"})
    assert r.status_code == 422
    assert "content" in r.text

    # media-only injects stay legal — content is optional WITH media_url
    r = client.post("/api/inject",
                    json={"bot_id": "main", "media_url": "/media/x.png"})
    assert r.status_code == 200, r.text


def test_thread_messages_rest_text_alias_and_empty_refusal(client):
    tid = client.post("/api/inject",
                      json={"bot_id": "main", "content": "seed"}).json()["thread_id"]

    r = client.post(f"/api/threads/{tid}/messages", json={"text": "via alias"})
    assert r.status_code == 200, r.text
    assert r.json()["content"] == "via alias"

    r = client.post(f"/api/threads/{tid}/messages", json={})
    assert r.status_code == 422
    assert "content" in r.text
