"""The four traps a small model falls into on the machine surface.

Every one of these is the same shape: the operation half-works, or works and
then appears to fail, and a Gemma-class agent cannot tell a phantom failure
from a real one — so it retries a send that already landed, or cycles reaction
names, or decides a bot does not exist.

  1. Thread ids resolved case-insensitively on the WRITE paths but exact-match
     on read/verify/fire. The gateway hands agents LOWERCASED ids in session
     keys (`agent:scout:daily-scout-…`), so an agent could post successfully and
     then 404 on the dispatch skill's own "verify, then stop" step.
  2. `POST /api/reactions/fire` at a wrong-case thread 404'd, and the skill's
     refusal table reads 404 as "bad reaction id".
  3. `GET /api/bots` showed a sessionless machine only the SAFE subset, so a
     roster lookup concluded the non-safe bots do not exist.
  4. `POST /api/threads/{id}/messages` silently sliced content at 65536 and
     returned 200, while the same body to /api/inject 422s.

Widening lookup is not widening permission: every test here also pins that a
sessionless BROWSER still gets exactly the Safe-Mode answer it always did.

Same hermetic style as test_inbound_thread_mgmt.py. Run: cd backend && uv run pytest.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app import auth, config, main
from app.database import Database
from app.models import MESSAGE_MAX_CHARS

LOOPBACK = ("127.0.0.1", 50000)
BROWSER_HDRS = {"origin": "http://127.0.0.1:8765"}   # browser-shaped request


@pytest.fixture
def env(tmp_path, monkeypatch):
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


# --------------------------------------------------------------------------- #
# 1 + 2. Case-insensitive on every thread-taking path, not only the writes
# --------------------------------------------------------------------------- #

def _seed_mixed(client) -> tuple[str, str]:
    """A thread whose real id differs in case from the form agents are handed.

    Mirrors the live shape rather than faking it: a bot id with a capital in it
    (`Scout`, say) gives `daily-Scout-<date>`, while the gateway lowercases
    whole session keys, so the agent reads its own thread back as
    `daily-scout-<date>`. Returns (canonical, lowercased).
    """
    mixed_bot = config.Bot(id="MixedCase", name="Mixed", emoji="\N{TEST TUBE}",
                           order=99, visible=True, safe=False)
    roster = [*config.load_bots(), mixed_bot]
    config._write_bots([config._bot_entry(b) for b in roster])
    config._invalidate_bots_cache()

    r = client.post("/api/inject", json={"bot_id": "MixedCase", "content": "seed"})
    assert r.status_code == 200, r.text
    tid = r.json()["thread_id"]
    assert tid != tid.lower(), f"expected a mixed-case thread id, got {tid!r}"
    return tid, tid.lower()


def test_read_and_verify_paths_accept_the_lowercased_id(env):
    client = env()
    auth.set_pin("1234")
    mixed, lowered = _seed_mixed(client)
    assert mixed != lowered

    # The write path always worked…
    assert client.post("/api/inject",
                       json={"thread_id": lowered, "content": "hi"}).status_code == 200
    # …and now so does everything an agent uses to CONFIRM the write.
    assert client.get(f"/api/threads/{lowered}").status_code == 200
    r = client.get(f"/api/threads/{lowered}/messages")
    assert r.status_code == 200, r.text
    assert any(m["content"] == "hi" for m in r.json()["messages"])
    assert client.post(f"/api/threads/{lowered}/read").status_code == 200
    assert client.patch(f"/api/threads/{lowered}",
                        json={"title": "renamed"}).status_code == 200
    assert client.post(f"/api/threads/{lowered}/messages",
                       json={"content": "second"}).status_code == 200

    # The canonical id is what is returned and stored — resolving must not
    # fork a second row under the lowercased spelling.
    assert client.get(f"/api/threads/{mixed}").json()["id"] == mixed
    ids = [t["id"] for t in client.get("/api/threads",
                                       params={"bot_id": "MixedCase"}).json()["threads"]]
    assert ids.count(mixed) == 1 and lowered not in ids

    # Delete last, so the rest of the test had a thread to work on.
    assert client.delete(f"/api/threads/{lowered}").status_code == 200


def test_an_id_that_matches_no_row_is_still_404(env):
    """Widening lookup must not turn a real miss into a success."""
    client = env()
    auth.set_pin("1234")
    for call in (lambda: client.get("/api/threads/no-such-thread"),
                 lambda: client.get("/api/threads/no-such-thread/messages"),
                 lambda: client.post("/api/threads/no-such-thread/read"),
                 lambda: client.patch("/api/threads/no-such-thread",
                                      json={"title": "x"}),
                 lambda: client.delete("/api/threads/no-such-thread")):
        assert call().status_code == 404


def test_fire_resolves_the_thread_before_refusing(env):
    client = env()
    auth.set_pin("1234")
    mixed, lowered = _seed_mixed(client)

    # A wrong-case thread must NOT come back as 404 "Unknown thread" — that is
    # the code the skill's table reads as "bad reaction id", which sends the
    # model cycling reaction names instead of fixing the thread.
    r = client.post("/api/reactions/fire",
                    json={"reaction": "definitely-not-a-real-reaction",
                          "thread_id": lowered, "bot_id": "MixedCase",
                          "actor_kind": "agent"})
    assert r.status_code != 404 or "Unknown thread" not in r.text

    # A genuinely unknown thread still refuses loudly.
    r = client.post("/api/reactions/fire",
                    json={"reaction": "anything", "thread_id": "no-such-thread",
                          "bot_id": "main", "actor_kind": "agent"})
    assert r.status_code == 404
    assert "Unknown thread" in r.text


# --------------------------------------------------------------------------- #
# 3. GET /api/bots — the machine sees the roster, the locked tab does not
# --------------------------------------------------------------------------- #

def test_machine_gets_the_true_roster_but_a_locked_tab_does_not(env):
    client = env()
    auth.set_pin("1234")

    machine = client.get("/api/bots")
    assert machine.status_code == 200, machine.text
    machine_ids = {b["id"] for b in machine.json()["bots"]}

    browser = client.get("/api/bots", headers=BROWSER_HDRS)
    assert browser.status_code == 200, browser.text
    browser_ids = {b["id"] for b in browser.json()["bots"]}

    # Safe Mode is a strict subset, and the machine sees at least one bot the
    # locked tab does not — otherwise this test proves nothing about either.
    assert browser_ids <= machine_ids
    assert machine_ids - browser_ids, (machine_ids, browser_ids)
    assert all(b["safe"] for b in browser.json()["bots"])


def test_bots_roster_is_not_a_remote_freebie(env):
    client = env()
    auth.set_pin("1234")
    remote = env(("192.0.2.99", 50000))

    # Keyless remote GET degrades to the decoy view — same carve-out the other
    # dual-use GETs take — never to the full roster.
    r = remote.get("/api/bots")
    assert r.status_code == 200
    assert all(b["safe"] for b in r.json()["bots"])

    cfg = auth.load()
    cfg.api_token = "sekrit-token"
    auth._write(cfg)
    auth._bust_cache()
    keyed = remote.get("/api/bots", headers={"X-API-Key": "sekrit-token"})
    assert keyed.status_code == 200
    assert {b["id"] for b in keyed.json()["bots"]} == {
        b["id"] for b in client.get("/api/bots").json()["bots"]}


# --------------------------------------------------------------------------- #
# 4. Oversize refuses instead of silently losing the tail
# --------------------------------------------------------------------------- #

def test_oversize_message_is_refused_not_truncated(env):
    client = env()
    auth.set_pin("1234")
    tid = client.post("/api/inject",
                      json={"bot_id": "main", "content": "seed"}).json()["thread_id"]

    too_long = "x" * (MESSAGE_MAX_CHARS + 1)
    r = client.post(f"/api/threads/{tid}/messages", json={"content": too_long})
    assert r.status_code == 422, r.status_code
    assert "too long" in r.text

    # /api/inject refuses the same body, so the two agree.
    assert client.post("/api/inject",
                       json={"thread_id": tid, "content": too_long}).status_code == 422

    # Nothing was persisted by either attempt.
    msgs = client.get(f"/api/threads/{tid}/messages").json()["messages"]
    assert all(len(m["content"]) <= MESSAGE_MAX_CHARS for m in msgs)
    assert not any(m["content"].startswith("xxxx") for m in msgs)

    # A message exactly AT the ceiling still posts.
    ok = client.post(f"/api/threads/{tid}/messages",
                     json={"content": "y" * MESSAGE_MAX_CHARS})
    assert ok.status_code == 200, ok.status_code


# --------------------------------------------------------------------------- #
# The gateway WS counters are readable without a journal grep
# --------------------------------------------------------------------------- #

def test_gateway_ws_counters_are_surfaced_in_detailed_health(env):
    client = env()
    body = client.get("/api/health").json()      # no PIN set -> detailed
    assert "gateway_ws" in body
    stats = body["gateway_ws"]
    # None when the transport is off, which is the shipped default; when it IS
    # on, truncation_unrepaired must be visible — it is the stop-the-line
    # signal that replies are being delivered cut short.
    if stats is not None:
        assert "truncation_unrepaired" in stats
        assert "since" in stats and "mode" in stats
