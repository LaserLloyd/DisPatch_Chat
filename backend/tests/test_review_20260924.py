"""Review round 2026-09-24: the defects found in the 2026-09-20 feature wave.

Every test here was run against the code BEFORE its fix: 12 of 14 fail there
(the review's repro file, re-shaped into the repo's own fixtures); the two
that pass on both (`..._restores_only_what_it_removed`,
`..._stores_the_edit_once_the_rewind_succeeded`) are the positive halves of
contracts whose negative halves are the fixes, kept so the fixes cannot
over-correct. What each one pins:

  1. Regenerate supersedes only the rows the bot's TURN wrote. A cron alert,
     an injected brief or a delivered run report after the answer used to be
     deleted for good, and an injected assistant row that happened to be last
     became THE alternate while the real previous answer was lost.
  2. A second regenerate / edit-and-rerun while one is in flight is refused.
     thread.status only becomes "thinking" inside run_agent_turn, after the
     rewind and the deletes, so a double-click used to rewind TWO turns.
  3. Edit-and-rerun stores the edit only once the rewind succeeded. It used to
     commit first, so a refused rewind left the new question over the answer
     to the old one.
  4. An API ("Connect an AI") bot is sent the composed text ONCE. The stored
     row is the raw message and the composed text differs (quote prefix,
     feedback note), so _ensure_trailing_user concatenated the two.
  5. A refusal at the door (AgentRefused) clears its _inflight_runs entry.
  6. The CLI's post-acceptance errors keep the NARROW gateway-down regex:
     "All models failed" with code UNAVAILABLE ran and was billed, so it must
     never read as a retryable outage. The socket's refusal branch keeps the
     wide one.
  7. The thread-list preview is redacted BEFORE it is cut, so a [[media:…]]
     directive straddling the cut cannot leak a URL head to a locked device.
  8. PATCH /api/threads/{id} is all-or-nothing, and a prefs value is a short
     identifier that never starts with "-" (it becomes a CLI argument).
  9. A WS send replayed after a backend restart (its ack lost, _ACK_SEEN gone)
     is stored once: the client id rides on messages.source_id.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app import auth, config, gateway_ws, llm_api, main, openclaw
from app.database import Database

LOOPBACK = ("127.0.0.1", 50000)
BROWSER_HDRS = {"origin": "http://127.0.0.1:8765"}


# --------------------------------------------------------------------------- #
# Fixtures (same shape as test_regenerate_edit.py / test_model_chip.py)
# --------------------------------------------------------------------------- #

@pytest.fixture
def wired(tmp_path, monkeypatch):
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
    main._inflight_runs.clear()
    getattr(main, "_regen_inflight", set()).clear()   # absent pre-fix
    yield db, frames


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
    getattr(main, "_regen_inflight", set()).clear()   # absent pre-fix
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


class Gw:
    """A gateway whose newest user entry moves back after each rewind, so a
    second rewind is visible as a rewind of an EARLIER turn."""

    def __init__(self, entries=(("e1", "q1"), ("e3", "q2")), supports=True):
        self.connected = asyncio.Event()
        self.connected.set()
        self.entries = list(entries)
        self.rewound: list[str] = []
        self._supports = supports

    def supports(self, method):
        return self._supports

    async def history_tail(self, key, *, limit=50):
        return {"messages": [{"role": "assistant", "__openclaw": {"id": "leaf"}}]}

    async def last_user_entry(self, key, *, limit=30):
        await asyncio.sleep(0)
        return self.entries[-1] if self.entries else None

    async def rewind(self, key, eid, *, agent_id=None):
        await asyncio.sleep(0.01)
        self.rewound.append(eid)
        self.entries = [e for e in self.entries if e[0] < eid]
        return {}

    async def switch_branch(self, key, leaf):
        return {}


class FakeWS:
    pass


def _ws_env(monkeypatch, frames):
    monkeypatch.setattr(main.manager, "conn_decoy", lambda w: False)

    async def _allowed(w, b):
        return True

    async def _send(w, f):
        frames.append(f)

    monkeypatch.setattr(main, "_ws_bot_allowed", _allowed)
    monkeypatch.setattr(main.manager, "send", _send)


async def _drain(n=30):
    for _ in range(n):
        await asyncio.sleep(0.01)


# --------------------------------------------------------------------------- #
# 1. Regenerate leaves rows that were not the turn alone
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_regenerate_keeps_rows_that_were_not_the_bots_turn(wired, monkeypatch):
    db, _ = wired
    await db.connect()
    try:
        th = await db.create_thread(bot_id="main")
        u = await db.add_message(th.id, "user", "q")
        await db.add_message(th.id, "assistant", "the real previous answer")
        await db.add_message(th.id, "system", "⚠️ backup finished (cron alert)")
        await db.add_message(th.id, "assistant", "Morning brief: …",
                             metadata={"origin": "inject"})
        await db.add_message(th.id, "assistant", "❌ Run w-1 failed — see report",
                             metadata={"origin": "inject", "delivery_key": "k1"})
        monkeypatch.setattr(main, "_gateway_client", Gw(entries=[("e1", "q")]))

        async def fake_turn(tid, bid, text, opts=None):
            await db.add_message(tid, "assistant", "a (regenerated)")

        monkeypatch.setattr(main, "run_agent_turn", fake_turn)
        main._regen_inflight.add(th.id)
        await main._regenerate_turn(th.id, "main", u)

        rows = await db.dump_messages(th.id)
        texts = [m.content for m in rows]
        assert "⚠️ backup finished (cron alert)" in texts, "system alert row lost"
        assert "Morning brief: …" in texts, "injected brief lost"
        assert "❌ Run w-1 failed — see report" in texts, "delivered run report lost"
        assert "the real previous answer" not in texts, "superseded reply not removed"
        assert texts[-1] == "a (regenerated)"
        alts = [a["content"] for m in rows
                for a in (m.metadata or {}).get("alternates", [])]
        assert alts == ["the real previous answer"], alts
        assert th.id not in main._regen_inflight
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_a_failed_regenerate_restores_only_what_it_removed(wired, monkeypatch):
    """The kept rows must not be counted as 'the new reply' either — the
    restore path used to see the injected brief as a reply and skip the
    restore, so the old answer stayed deleted."""
    db, frames = wired
    await db.connect()
    try:
        th = await db.create_thread(bot_id="main")
        u = await db.add_message(th.id, "user", "q")
        old = await db.add_message(th.id, "assistant", "old answer")
        await db.add_message(th.id, "assistant", "Morning brief: …",
                             metadata={"origin": "inject"})
        monkeypatch.setattr(main, "_gateway_client", Gw(entries=[("e1", "q")]))

        async def nothing(tid, bid, text, opts=None):
            return None

        monkeypatch.setattr(main, "run_agent_turn", nothing)
        await main._regenerate_turn(th.id, "main", u)
        rows = await db.dump_messages(th.id)
        assert [m.id for m in rows if m.content == "old answer"] == [old.id]
        assert any(f.get("type") == "error" and "previous reply is back" in f["message"]
                   for f in frames)
    finally:
        await db.close()


# --------------------------------------------------------------------------- #
# 2. One regenerate at a time per thread
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_a_second_regenerate_is_refused_while_the_first_is_in_flight(wired, monkeypatch):
    db, frames = wired
    await db.connect()
    try:
        th = await db.create_thread(bot_id="main")
        await db.add_message(th.id, "user", "q1")
        await db.add_message(th.id, "assistant", "a1")
        await db.add_message(th.id, "user", "q2")
        await db.add_message(th.id, "assistant", "a2")
        gw = Gw()
        monkeypatch.setattr(main, "_gateway_client", gw)
        _ws_env(monkeypatch, frames)

        async def slow_turn(tid, bid, text, opts=None):
            await asyncio.sleep(0.08)
            await db.add_message(tid, "assistant", "regen")

        monkeypatch.setattr(main, "run_agent_turn", slow_turn)
        ws = FakeWS()
        await main._handle_regenerate(ws, {"thread_id": th.id})
        await asyncio.sleep(0.02)   # the double-click
        await main._handle_regenerate(ws, {"thread_id": th.id})
        await _drain()
        assert gw.rewound == ["e3"], "the second click rewound an EARLIER turn"
        errs = [f["message"] for f in frames if f.get("type") == "error"]
        assert "A reply is already in progress" in errs
        rows = [m.content for m in await db.dump_messages(th.id)]
        assert rows.count("regen") == 1, rows
        assert "a1" in rows
        assert th.id not in main._regen_inflight, "the guard must be released"
    finally:
        await db.close()


# --------------------------------------------------------------------------- #
# 3. Edit-and-rerun: the edit follows the rewind, never precedes it
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_edit_rerun_leaves_the_question_alone_when_the_rewind_is_refused(wired, monkeypatch):
    db, frames = wired
    await db.connect()
    try:
        th = await db.create_thread(bot_id="main")
        u = await db.add_message(th.id, "user", "original question")
        await db.add_message(th.id, "assistant", "answer to original")
        monkeypatch.setattr(main, "_gateway_client", Gw(supports=False))
        _ws_env(monkeypatch, frames)
        called: list = []

        async def turn(*a, **k):
            called.append(a)

        monkeypatch.setattr(main, "run_agent_turn", turn)
        await main._handle_edit_rerun(FakeWS(), {"thread_id": th.id, "message_id": u.id,
                                                 "content": "totally different question"})
        await _drain()
        msg = await db.get_message(u.id)
        assert msg.content == "original question"
        assert not (msg.metadata or {}).get("edited_at")
        assert not called
        assert any(f.get("type") == "error" and "rewind" in f["message"] for f in frames)
        assert th.id not in main._regen_inflight
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_edit_rerun_stores_the_edit_once_the_rewind_succeeded(wired, monkeypatch):
    db, frames = wired
    await db.connect()
    try:
        th = await db.create_thread(bot_id="main")
        u = await db.add_message(th.id, "user", "original question")
        await db.add_message(th.id, "assistant", "answer to original")
        gw = Gw(entries=[("e1", "original question")])
        monkeypatch.setattr(main, "_gateway_client", gw)
        _ws_env(monkeypatch, frames)
        sent: list = []

        async def turn(tid, bid, text, opts=None):
            sent.append(text)
            await db.add_message(tid, "assistant", "answer to the new one")

        monkeypatch.setattr(main, "run_agent_turn", turn)
        await main._handle_edit_rerun(FakeWS(), {"thread_id": th.id, "message_id": u.id,
                                                 "content": "new question"})
        await _drain()
        assert gw.rewound == ["e1"]
        assert sent == ["new question"]
        msg = await db.get_message(u.id)
        assert msg.content == "new question"
        assert msg.metadata["original"] == "original question"
        rows = [m.content for m in await db.dump_messages(th.id)]
        assert rows == ["new question", "answer to the new one"]
    finally:
        await db.close()


# --------------------------------------------------------------------------- #
# 4. API bots: the composed text replaces the raw row, never joins it
# --------------------------------------------------------------------------- #

def test_ensure_trailing_user_replaces_a_raw_row_with_the_composed_text():
    history = [{"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1"},
               {"role": "user", "content": "follow up please"}]
    composed = '[Replying to your own earlier reply: "a1"]\nfollow up please'
    out = llm_api._ensure_trailing_user(history, composed)
    assert out[-1] == {"role": "user", "content": composed}
    assert out[-1]["content"].count("follow up please") == 1
    assert len(out) == 3
    # Unchanged contracts: identical text is a no-op, and a history ending
    # on the assistant gains the user turn.
    assert llm_api._ensure_trailing_user(history, "follow up please") == history
    tail = llm_api._ensure_trailing_user(history[:-1], "x")
    assert tail[-1] == {"role": "user", "content": "x"} and len(tail) == 3


@pytest.mark.asyncio
async def test_an_api_bot_is_sent_the_users_message_once(wired, monkeypatch):
    db, _ = wired
    await db.connect()
    try:
        config.upsert_bot(config.Bot(id="llm-custom", name="T", emoji="x",
                                     api={"provider": "custom",
                                          "base_url": "http://127.0.0.1:9/v1",
                                          "model": "m"}))
        th = await db.create_thread(bot_id="llm-custom")
        await db.add_message(th.id, "user", "first question")
        ans = await db.add_message(th.id, "assistant", "first answer")
        await db.add_message(th.id, "user", "follow up please")
        seen: dict = {}

        async def fake_complete(cfg, history):
            seen["history"] = history
            return llm_api.Reply(text="ok", model="m", provider="custom")

        monkeypatch.setattr(llm_api, "complete", fake_complete)
        await main.run_agent_turn(th.id, "llm-custom", "follow up please",
                                  main.TurnOptions(reply_to=ans.id))
        last = seen["history"][-1]
        assert last["role"] == "user"
        assert last["content"].count("follow up please") == 1, last["content"]
        assert "first answer" in last["content"], "the quote must still be there"
    finally:
        await db.close()


# --------------------------------------------------------------------------- #
# 5. A refused run is not in flight
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_a_refused_run_leaves_no_inflight_entry(wired, monkeypatch):
    import dataclasses

    class FakeClient:
        connected = asyncio.Event()

        async def subscribe_session(self, key):
            return None

        async def call_agent(self, params, *, timeout):
            raise gateway_ws.GatewayRunRefused(
                "{'code': 'INVALID_REQUEST', 'message': 'model x not allowed'}")

    c = FakeClient()
    c.connected.set()
    monkeypatch.setattr(main, "_gateway_client", c)
    monkeypatch.setattr(main, "SETTINGS",
                        dataclasses.replace(main.SETTINGS, turn_transport="1"))
    with pytest.raises(openclaw.AgentRefused):
        await main._dispatch_turn("main", "agent:main:t1", "hi", model="x")
    assert not main._inflight_runs, "a refused run stayed registered"


# --------------------------------------------------------------------------- #
# 6. The two gateway-down regexes
# --------------------------------------------------------------------------- #

def test_cli_post_acceptance_unavailable_is_not_retryable():
    parsed = {"status": "error",
              "summary": "All models failed (3): {'code': 'UNAVAILABLE', "
                         "'message': 'provider overloaded'}"}
    with pytest.raises(openclaw.AgentError) as ei:
        openclaw._parse_reply(parsed, "main", "")
    assert not isinstance(ei.value, openclaw.GatewayUnavailable)
    # Prose that merely mentions draining is not an outage on this path.
    parsed = {"status": "error", "summary": "the queue is draining slowly"}
    with pytest.raises(openclaw.AgentError) as ei:
        openclaw._parse_reply(parsed, "main", "")
    assert not isinstance(ei.value, openclaw.GatewayUnavailable)


def test_the_socket_refusal_branch_keeps_the_wide_signature():
    wide = openclaw._GATEWAY_REFUSAL_DOWN_RE
    assert wide.search("{'code': 'UNAVAILABLE', 'message': 'draining'}")
    assert wide.search("GatewayDrainingError: no")
    assert not wide.search("{'code': 'INVALID_REQUEST', 'message': 'model x'}")
    narrow = openclaw._GATEWAY_DOWN_RE
    assert narrow.search("errorCode=UNAVAILABLE something")
    assert not narrow.search("{'code': 'UNAVAILABLE', 'message': 'All models failed'}")


# --------------------------------------------------------------------------- #
# 7. Preview: redact, then cut
# --------------------------------------------------------------------------- #

def test_thread_list_preview_is_redacted_before_it_is_cut(env):
    client = env()
    auth.set_pin("1234")
    body = "x" * (main.THREAD_PREVIEW_CHARS - 20) + \
        "[[media:https://example.invalid/private/secret-token-abc.png|cap]]"
    r = client.post("/api/inject", json={"bot_id": "alpha", "content": body})
    assert r.status_code == 200, r.text
    locked = env()
    r = locked.get("/api/threads", params={"bot_id": "alpha"}, headers=BROWSER_HDRS)
    assert r.status_code == 200, r.text
    previews = [t["last_message"] for t in r.json()["threads"] if t.get("last_message")]
    assert previews
    for p in previews:
        assert "example.invalid" not in p, p
        assert "[[media" not in p, p
        assert len(p) <= main.THREAD_PREVIEW_CHARS + 1


# --------------------------------------------------------------------------- #
# 8. PATCH thread: all-or-nothing, and bounded prefs values
# --------------------------------------------------------------------------- #

def _unlock(client: TestClient, pin: str = "1234") -> None:
    r = client.post("/api/auth/unlock", json={"pin": pin})
    assert r.status_code == 200, r.text


def test_patch_thread_refused_for_prefs_applies_nothing(env):
    client = env()
    auth.set_pin("1234")
    r = client.post("/api/inject", json={"bot_id": "main", "content": "seed"})
    tid = r.json()["thread_id"]
    before = client.get(f"/api/threads/{tid}").json()["title"]
    r = client.patch(f"/api/threads/{tid}",
                     json={"title": "RENAMED-BY-MACHINE", "pinned": True,
                           "prefs": {"model": "m-1"}})
    assert r.status_code == 403, r.text
    after = client.get(f"/api/threads/{tid}").json()
    assert after["title"] == before, "a refused PATCH half-applied"
    assert not after.get("pinned")


def test_prefs_values_are_short_identifiers(env):
    client = env()
    auth.set_pin("1234")
    _unlock(client)
    tid = client.post("/api/inject", json={"bot_id": "main", "content": "seed"}).json()["thread_id"]
    for bad in ({"model": "--help"}, {"thinking": "x" * 50_000},
                {"model": "a b"}, {"thinking": "-adaptive"}, {"model": ""}):
        r = client.patch(f"/api/threads/{tid}", json={"prefs": bad})
        assert r.status_code == 400, (bad, r.text)
    r = client.patch(f"/api/threads/{tid}",
                     json={"prefs": {"model": "minimax/MiniMax-M3", "thinking": "adaptive"}})
    assert r.status_code == 200, r.text
    r = client.patch(f"/api/threads/{tid}", json={"prefs": {"thinking": None}})
    assert r.status_code == 200, r.text
    assert client.get(f"/api/threads/{tid}").json()["prefs"] == {"model": "minimax/MiniMax-M3"}


# --------------------------------------------------------------------------- #
# 9. WS send replay after the in-memory dedup is gone
# --------------------------------------------------------------------------- #

def test_a_replayed_send_is_stored_once_even_after_ack_seen_is_lost(env, monkeypatch):
    client = env()
    tid = client.post("/api/threads", json={"bot_id": "main"}).json()["id"]
    turns: list = []

    async def no_turn(*a, **k):
        turns.append(a)

    monkeypatch.setattr(main, "run_agent_turn", no_turn)
    frame = {"type": "send", "thread_id": tid, "text": "DUP_TEST",
             "client_msg_id": "c-11111111-2222-3333-4444-555555555555"}
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()   # hello
        ws.send_json(frame)
        acks = []
        while True:
            f = ws.receive_json()
            if f.get("type") == "ack":
                acks.append(f)
                break
        assert acks[0]["status"] == "ok"
    # A backend restart: the in-memory LRU is gone, the rows are not.
    main._ACK_SEEN.clear()
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json(frame)
        while True:
            f = ws.receive_json()
            if f.get("type") == "ack":
                assert f["status"] == "ok"
                break
    msgs = client.get(f"/api/threads/{tid}/messages").json()["messages"]
    assert [m["content"] for m in msgs if m["role"] == "user"] == ["DUP_TEST"]
    assert len(turns) == 1, "the replay must not start a second turn"
