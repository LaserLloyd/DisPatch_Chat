"""Thread management as a machine surface (added 2026-08-14).

OPENCLAW.md has always documented list/read/create/rename/pin/archive/delete +
mark-read + unread as available to on-box agents, but only the POST-message
half was actually inbound-exempt — every agent following the doc got a 403
(the same doc/code drift the avatar routes went through). These tests pin the
widened surface AND that a sessionless BROWSER still gets exactly the
Safe-Mode behavior it always had: the exemption is for machines.

Same hermetic style as test_auth_gate.py. Run: cd backend && uv run pytest.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app import auth, config, main
from app.database import Database

LOOPBACK = ("127.0.0.1", 50000)
BROWSER_HDRS = {"origin": "http://127.0.0.1:8765"}   # browser-shaped request


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated data dir; yields a client factory keyed by peer address."""
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

    clients: list[TestClient] = []

    def make(client_addr=LOOPBACK) -> TestClient:
        c = TestClient(main.app, client=client_addr)
        c.__enter__()
        clients.append(c)
        return c

    yield make
    for c in clients:
        c.__exit__(None, None, None)
    asyncio.run(temp_db.close())


def _seed_thread(client, bot_id="main", content="seed message") -> str:
    r = client.post("/api/inject", json={"bot_id": bot_id, "content": content})
    assert r.status_code == 200, r.text
    return r.json()["thread_id"]


# --------------------------------------------------------------------------- #
# Machine callers (loopback curl shape: no Origin, no Sec-Fetch, no session)
# --------------------------------------------------------------------------- #

def test_machine_can_list_and_read_nonsafe_bot_with_pin(env):
    client = env()
    auth.set_pin("1234")
    tid = _seed_thread(client, content="unredacted body")

    r = client.get("/api/threads", params={"bot_id": "main"})
    assert r.status_code == 200, r.text
    assert any(t["id"] == tid for t in r.json()["threads"])

    r = client.get(f"/api/threads/{tid}/messages")
    assert r.status_code == 200, r.text
    assert r.json()["messages"][-1]["content"] == "unredacted body"  # no redaction

    assert client.get(f"/api/threads/{tid}").status_code == 200
    assert client.get("/api/unread").status_code == 200


def test_machine_can_manage_threads_with_pin(env):
    client = env()
    auth.set_pin("1234")
    tid = _seed_thread(client)

    assert client.patch(f"/api/threads/{tid}",
                        json={"title": "renamed"}).status_code == 200
    assert client.patch(f"/api/threads/{tid}",
                        json={"pinned": True}).status_code == 200
    assert client.post(f"/api/threads/{tid}/read").status_code == 200

    mid = client.get(f"/api/threads/{tid}/messages").json()["messages"][-1]["id"]
    assert client.delete(f"/api/messages/{mid}").status_code == 200

    created = client.post("/api/threads", json={"bot_id": "main", "title": "t"})
    assert created.status_code == 200
    assert client.delete(f"/api/threads/{created.json()['id']}").status_code == 200


def test_remote_machine_needs_api_token(env):
    loop = env()
    auth.set_pin("1234")
    tid = _seed_thread(loop)

    remote = env(("192.0.2.99", 50000))
    # Keyless remote GET degrades to the decoy view (a plain-HTTP locked tab
    # is indistinguishable from this shape — see the auth_gate carve-out), and
    # decoy + non-safe bot = 403. Keyless remote MUTATIONS stay 401.
    r = remote.get("/api/threads", params={"bot_id": "main"})
    assert r.status_code == 403
    assert remote.patch(f"/api/threads/{tid}",
                        json={"title": "x"}).status_code == 401

    cfg = auth.load()
    cfg.api_token = "sekrit-token"
    auth._write(cfg)
    auth._bust_cache()
    r = remote.get("/api/threads", params={"bot_id": "main"},
                   headers={"X-API-Key": "sekrit-token"})
    assert r.status_code == 200
    assert any(t["id"] == tid for t in r.json()["threads"])


# --------------------------------------------------------------------------- #
# Browser callers: the exemption must NOT leak to a sessionless tab
# --------------------------------------------------------------------------- #

def test_sessionless_browser_still_safe_mode(env):
    client = env()
    auth.set_pin("1234")
    tid = _seed_thread(client)                        # main = non-safe

    # Non-safe bot: view refused, exactly as before the widening.
    r = client.get("/api/threads", params={"bot_id": "main"},
                   headers=BROWSER_HDRS)
    assert r.status_code == 403
    assert client.get(f"/api/threads/{tid}/messages",
                      headers=BROWSER_HDRS).status_code == 403

    # Mutations refused outright.
    assert client.patch(f"/api/threads/{tid}", json={"title": "x"},
                        headers=BROWSER_HDRS).status_code == 403
    assert client.delete(f"/api/threads/{tid}",
                         headers=BROWSER_HDRS).status_code == 403

    # Safe bot: view allowed, media redacted — the Safe-Mode contract.
    stid = _seed_thread(client, bot_id="alpha",
                        content="pic [[media:/media/x.png|cap]]")
    r = client.get(f"/api/threads/{stid}/messages", headers=BROWSER_HDRS)
    assert r.status_code == 200
    assert "[[media:" not in (r.json()["messages"][-1]["content"] or "")

    # Unread summary filters to safe bots for the sessionless tab.
    unread = client.get("/api/unread", headers=BROWSER_HDRS)
    assert unread.status_code == 200
    assert all(u["bot_id"] in ("alpha", "beta", "Atlas")
               for u in unread.json()["unread"])


def test_plain_http_remote_tab_keeps_decoy_view(env):
    """A locked tab on a plain-HTTP origin (LAN IP) sends NO Sec-Fetch-* and
    no Origin on same-origin GETs — indistinguishable from a remote machine.
    Its keyless GETs must degrade to the decoy view (what it had before the
    widening), like the long-standing GET /api/reactions carve-out; keyless
    mutations from that shape stay fail-closed at 401."""
    loop = env()
    auth.set_pin("1234")
    _seed_thread(loop, content="private main body")           # non-safe
    stid = _seed_thread(loop, bot_id="alpha",
                        content="pic [[media:/media/x.png|cap]]")

    remote = env(("192.0.2.50", 50000))                     # bare GET shape
    # Safe bot: 200 with redaction — the locked family view.
    r = remote.get("/api/threads", params={"bot_id": "alpha"})
    assert r.status_code == 200, r.text
    r = remote.get(f"/api/threads/{stid}/messages")
    assert r.status_code == 200
    assert "[[media:" not in (r.json()["messages"][-1]["content"] or "")
    # Non-safe bot: refused as decoy (403), not an API-key demand.
    assert remote.get("/api/threads",
                      params={"bot_id": "main"}).status_code == 403
    # Unread summary filtered to safe bots.
    u = remote.get("/api/unread")
    assert u.status_code == 200
    assert all(x["bot_id"] in ("alpha", "beta", "Atlas")
               for x in u.json()["unread"])
    # Keyless remote mutations stay fail-closed.
    assert remote.patch(f"/api/threads/{stid}",
                        json={"title": "x"}).status_code == 401
    assert remote.delete(f"/api/threads/{stid}").status_code == 401


def test_no_pin_everything_open_unchanged(env):
    client = env()
    tid = _seed_thread(client)
    assert client.get("/api/threads",
                      params={"bot_id": "main"}).status_code == 200
    assert client.get(f"/api/threads/{tid}/messages").status_code == 200


# --------------------------------------------------------------------------- #
# File Server list as a machine surface (added 2026-08-14 evening)
# --------------------------------------------------------------------------- #

def test_machine_can_list_files_with_pin(env):
    """An on-box agent maps an upload to its blob via GET /api/files
    (name → stored_name → FILES_DIR/<stored_name>). Before this joined the
    inbound surface, every agent following that path got a 403 and fell back
    to guessing at uuid filenames on disk."""
    client = env()
    auth.set_pin("1234")
    # /api/upload is the sanctioned sessionless upload (quota-capped): a
    # document lands as a File Server record, which is exactly what an agent
    # then needs to find again.
    r = client.post("/api/upload", files={"file": ("notes.txt", b"hello", "text/plain")})
    assert r.status_code == 200, r.text
    r = client.get("/api/files")
    assert r.status_code == 200, r.text
    names = [f["name"] for f in r.json()["files"]]
    assert "notes.txt" in names
    assert all("stored_name" in f for f in r.json()["files"])


def test_sessionless_browser_still_cannot_list_files(env):
    client = env()
    auth.set_pin("1234")
    r = client.get("/api/files", headers=BROWSER_HDRS)
    assert r.status_code == 403


def test_remote_machine_needs_token_for_files_list(env):
    client = env(client_addr=("192.0.2.99", 40000))
    auth.set_pin("1234")
    r = client.get("/api/files")
    assert r.status_code == 401
