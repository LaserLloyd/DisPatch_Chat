"""Regenerate that actually regenerates, and an edit that admits it edited.

Two defects, one mechanism.

THE REGENERATE DEFECT. `_handle_retry` re-sent the last user message into the
same gateway session without rewinding it, so the agent answered a second time
with its own previous reply still in front of it. That is "ask again while
looking at your old answer" — the model's strongest prior is the text it just
wrote, which is why a regenerate so reliably produced the same reply in
different words. The fix is `sessions.rewind` BEFORE the run, and the order is
the whole feature: rewinding after the turn would be a no-op with extra steps.

THE EDIT DEFECT (a new surface, not a regression). A transcript that can be
rewritten in place and shows no sign of it is a transcript that lies. Hiding a
row from the model's context and leaving it visible would be the same class of
lie one layer down — which is exactly why hide is offered for API bots only,
where DisPatch owns the history it sends.

Also pinned here: the failure path. A regenerate that loses the old answer AND
fails to get a new one is the worst outcome available, so the superseded rows
go back verbatim — same ids, same timestamps, same metadata.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app import auth, config, llm_api, main, openclaw
from app.database import Database

# --------------------------------------------------------------------------- #
# A gateway that records the ORDER of what it was asked to do
# --------------------------------------------------------------------------- #

class RecordingGateway:
    """Enough GatewayClient surface for a regenerate, with a call log.

    The log is the point. Every assertion in the proof test is about what
    happened before what, and a mock that only records *that* rewind was
    called would pass against a rewind issued after the turn — which is the
    bug, not the fix.
    """

    def __init__(self, *, methods=("sessions.rewind", "sessions.branches.switch"),
                 connected=True, editor_text="what is the capital of France?",
                 rewind_raises: Exception | None = None,
                 entry: tuple[str, str] | None = None):
        self.calls: list[str] = []
        self.connected = asyncio.Event()
        if connected:
            self.connected.set()
        self._methods = set(methods)
        self._editor_text = editor_text
        self._rewind_raises = rewind_raises
        self._entry = entry
        self.rewound: list[tuple[str, str]] = []
        self.switched: list[tuple[str, str]] = []

    def supports(self, method: str) -> bool:
        return method in self._methods

    async def history_tail(self, session_key: str, *, limit: int = 50) -> dict:
        self.calls.append("chat.history")
        return {"messages": [{"role": "assistant", "__openclaw": {"id": "e9"}}]}

    async def last_user_entry(self, session_key: str, *, limit: int = 30):
        self.calls.append("sessions.lastUserEntry")
        if self._entry is not None:
            return self._entry
        return ("e7", self._editor_text)

    async def rewind(self, session_key: str, entry_id: str, *, agent_id=None):
        self.calls.append("sessions.rewind")
        if self._rewind_raises is not None:
            raise self._rewind_raises
        self.rewound.append((session_key, entry_id))
        return {"editorText": self._editor_text}

    async def switch_branch(self, session_key: str, leaf_entry_id: str):
        self.calls.append("sessions.branches.switch")
        self.switched.append((session_key, leaf_entry_id))
        return {"ok": True}


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """Real database, captured broadcast, no HTTP layer."""
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
    yield db, frames


async def _seed(db, *, bot_id="main", reply="Paris, of course."):
    """A thread with one question and one answer, the shape regenerate acts on."""
    thread = await db.create_thread(bot_id=bot_id)
    user = await db.add_message(thread.id, "user", "what is the capital of France?")
    assistant = await db.add_message(thread.id, "assistant", reply,
                                     metadata={"model": "old-model"})
    return thread, user, assistant


# --------------------------------------------------------------------------- #
# THE PROOF. Rewind happens, and it happens FIRST.
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_regenerate_rewinds_the_session_before_it_runs_the_turn(
        wired, monkeypatch):
    """The defect, stated as an ordering: `sessions.rewind` then `agent`.

    Run against the pre-change code this dies on
    `module 'app.main' has no attribute '_regenerate_turn'` — there was no
    regenerate at all, only `retry`, which re-ran the turn inside the untouched
    session. And the assertion that survives a careless fix is the last one:
    rewinding AFTER the run reads the same in a call log that only records
    *whether* rewind happened, and is a no-op with extra steps.
    """
    db, frames = wired
    await db.connect()
    try:
        thread, user, _old = await _seed(db)
        gw = RecordingGateway()
        monkeypatch.setattr(main, "_gateway_client", gw)

        async def _fake_turn(thread_id, bot_id, text, opts=None):
            gw.calls.append("agent")
            await db.add_message(thread_id, "assistant", "Paris.",
                                 metadata={"model": "new-model"})

        monkeypatch.setattr(main, "run_agent_turn", _fake_turn)

        await main._regenerate_turn(thread.id, "main", user)

        assert "sessions.rewind" in gw.calls, (
            "regenerate must cut the session back — re-asking inside the same "
            "session is the bug: the agent answers again while reading its own "
            "previous reply")
        assert "agent" in gw.calls, "the turn must still run"
        assert gw.calls.index("sessions.rewind") < gw.calls.index("agent"), (
            "a rewind issued after the turn is a no-op with extra steps")
        assert gw.rewound == [(openclaw.session_key_for("main", thread.id), "e7")]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_the_superseded_reply_is_deleted_and_the_new_one_pages_back_to_it(
        wired, monkeypatch):
    db, frames = wired
    await db.connect()
    try:
        thread, user, old = await _seed(db)
        gw = RecordingGateway()
        monkeypatch.setattr(main, "_gateway_client", gw)

        async def _fake_turn(thread_id, bot_id, text, opts=None):
            await db.add_message(thread_id, "assistant", "Paris.",
                                 metadata={"model": "new-model"})

        monkeypatch.setattr(main, "run_agent_turn", _fake_turn)
        await main._regenerate_turn(thread.id, "main", user)

        rows = [m for m in await db.dump_messages(thread.id) if m.role == "assistant"]
        assert [r.content for r in rows] == ["Paris."], (
            "the old reply must be gone from the thread, not sitting above the new one")
        alts = (rows[0].metadata or {}).get("alternates") or []
        assert [a["content"] for a in alts] == ["Paris, of course."]
        assert alts[0]["id"] == old.id and alts[0]["model"] == "old-model"
        assert any(f["type"] == "message_deleted" and f["message_id"] == old.id
                   for f in frames), "open clients must be told the row went away"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_alternates_keep_the_last_eight_and_evict_the_oldest(
        wired, monkeypatch):
    """A thread regenerated forty times must not carry forty copies of itself."""
    db, frames = wired
    await db.connect()
    try:
        thread, user, _ = await _seed(db, reply="answer 0")
        gw = RecordingGateway()
        monkeypatch.setattr(main, "_gateway_client", gw)
        counter = {"n": 0}

        async def _fake_turn(thread_id, bot_id, text, opts=None):
            counter["n"] += 1
            await db.add_message(thread_id, "assistant", f"answer {counter['n']}")

        monkeypatch.setattr(main, "run_agent_turn", _fake_turn)
        for _ in range(12):
            await main._regenerate_turn(thread.id, "main", user)

        rows = [m for m in await db.dump_messages(thread.id) if m.role == "assistant"]
        assert len(rows) == 1
        alts = (rows[0].metadata or {}).get("alternates") or []
        assert len(alts) == main.ALTERNATES_CAP
        assert [a["content"] for a in alts] == [f"answer {i}" for i in range(4, 12)]
        assert all("alternates" not in a for a in alts), (
            "an alternate records the reply, not the whole history under it — "
            "nesting would double the blob on every regenerate")
    finally:
        await db.close()


# --------------------------------------------------------------------------- #
# The failure path: never lose both answers
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_a_failed_regenerate_puts_the_old_reply_back_verbatim(
        wired, monkeypatch):
    db, frames = wired
    await db.connect()
    try:
        thread, user, old = await _seed(db)
        gw = RecordingGateway()
        monkeypatch.setattr(main, "_gateway_client", gw)

        async def _fake_turn(thread_id, bot_id, text, opts=None):
            # What run_agent_turn does on an AgentError: broadcasts, persists
            # nothing.
            raise openclaw.AgentError("gateway said no", "detail")

        monkeypatch.setattr(main, "run_agent_turn", _fake_turn)
        await main._regenerate_turn(thread.id, "main", user)

        rows = [m for m in await db.dump_messages(thread.id) if m.role == "assistant"]
        assert len(rows) == 1, "the answer the family had already read must come back"
        back = rows[0]
        assert back.id == old.id, "same row, not a copy with a new id"
        assert back.content == old.content
        assert back.created_at == old.created_at, (
            "a restored reply stamped `now` files itself at the bottom of a "
            "conversation it was never at the bottom of")
        assert (back.metadata or {}).get("model") == "old-model"
        assert gw.switched == [(openclaw.session_key_for("main", thread.id), "e9")], (
            "the gateway's own view has to go back too, or the session is "
            "rewound past a reply DisPatch is showing")
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_a_gateway_that_refuses_to_rewind_changes_nothing(
        wired, monkeypatch):
    """Deleting without rewinding is the original bug plus data loss."""
    db, frames = wired
    await db.connect()
    try:
        thread, user, old = await _seed(db)
        gw = RecordingGateway(rewind_raises=RuntimeError("run in progress"))
        monkeypatch.setattr(main, "_gateway_client", gw)
        ran = {"n": 0}

        async def _fake_turn(thread_id, bot_id, text, opts=None):
            ran["n"] += 1

        monkeypatch.setattr(main, "run_agent_turn", _fake_turn)
        await main._regenerate_turn(thread.id, "main", user)

        rows = [m for m in await db.dump_messages(thread.id) if m.role == "assistant"]
        assert [r.id for r in rows] == [old.id]
        assert ran["n"] == 0, "no turn may run against a session that was not cut back"
        assert any(f["type"] == "error" for f in frames), "and the refusal must be visible"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_an_older_gateway_says_so_instead_of_doing_the_broken_thing(
        wired, monkeypatch):
    db, frames = wired
    await db.connect()
    try:
        thread, user, old = await _seed(db)
        gw = RecordingGateway(methods=())        # hello advertised no rewind
        monkeypatch.setattr(main, "_gateway_client", gw)

        async def _fake_turn(thread_id, bot_id, text, opts=None):
            raise AssertionError("must not run")

        monkeypatch.setattr(main, "run_agent_turn", _fake_turn)
        await main._regenerate_turn(thread.id, "main", user)

        assert "sessions.rewind" not in gw.calls
        rows = await db.dump_messages(thread.id)
        assert [m.id for m in rows] == [user.id, old.id]
        errs = [f for f in frames if f["type"] == "error"]
        assert errs and "Retry" in errs[-1]["message"], (
            "name the thing that still works instead of failing silently")
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_an_editor_text_mismatch_warns_and_proceeds(wired, monkeypatch, caplog):
    """The stored row and the gateway's copy legitimately differ (doc refs,
    quotes). Refusing on every composed prompt would make regenerate useless."""
    db, frames = wired
    await db.connect()
    try:
        thread, user, _ = await _seed(db)
        gw = RecordingGateway(entry=("e7", "what is the capital of France?\n\n[file]"))
        monkeypatch.setattr(main, "_gateway_client", gw)

        async def _fake_turn(thread_id, bot_id, text, opts=None):
            await db.add_message(thread_id, "assistant", "Paris.")

        monkeypatch.setattr(main, "run_agent_turn", _fake_turn)
        with caplog.at_level("WARNING"):
            await main._regenerate_turn(thread.id, "main", user)
        assert gw.rewound, "a mismatch is a warning, not a refusal"
        assert any("regenerate" in r.message for r in caplog.records)
    finally:
        await db.close()


# --------------------------------------------------------------------------- #
# An API bot: deleting the trailing rows IS the rewind
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_an_api_bot_regenerates_with_no_gateway_call_at_all(
        wired, monkeypatch, tmp_path):
    db, frames = wired
    await db.connect()
    try:
        class _ApiBot:
            id = "apibot"
            agent_id = "apibot"

            def __init__(self):
                self.api = {"provider": "openai", "model": "gpt", "api_key": "k"}

        monkeypatch.setattr(config, "get_bot",
                            lambda bid: _ApiBot() if bid == "apibot" else None)
        thread, user, old = await _seed(db, bot_id="apibot")
        gw = RecordingGateway()
        monkeypatch.setattr(main, "_gateway_client", gw)

        seen: dict = {}

        async def _fake_turn(thread_id, bot_id, text, opts=None):
            seen["rows"] = await db.dump_messages(thread_id)
            await db.add_message(thread_id, "assistant", "Paris.")

        monkeypatch.setattr(main, "run_agent_turn", _fake_turn)
        await main._regenerate_turn(thread.id, "apibot", user)

        assert gw.calls == [], "an API bot has no session to rewind"
        assert [m.role for m in seen["rows"]] == ["user"], (
            "the trailing assistant row must be gone BEFORE the turn rebuilds "
            "its history from these rows — that deletion is the rewind")
    finally:
        await db.close()


# --------------------------------------------------------------------------- #
# Editing
# --------------------------------------------------------------------------- #

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
    auth._fail_count = 0
    auth._fail_until = 0.0
    config._invalidate_bots_cache()
    temp_db = Database(tmp_path / "chats.db")
    monkeypatch.setattr(main, "db", temp_db)
    main._ACK_SEEN.clear()
    main._delivered.clear()
    main._thread_bot.clear()
    with TestClient(main.app) as c:
        yield c
    asyncio.run(temp_db.close())


def _seed_http(client, bot_id="main", content="hello there"):
    """A thread whose newest row is a USER message — what an edit acts on."""
    tid = client.post("/api/threads", json={"bot_id": bot_id}).json()["id"]
    r = client.post(f"/api/threads/{tid}/messages",
                    json={"role": "user", "content": content})
    assert r.status_code == 200, r.text
    msgs = client.get(f"/api/threads/{tid}/messages").json()["messages"]
    return tid, msgs[-1]["id"]


def _settle(ws, probe, timeout=40):
    """Give a handler's spawned task a chance to run.

    The WS handlers dispatch their work with `create_task`, so the frame having
    been accepted says nothing about the task having run — asserting straight
    after the send passes or fails on scheduler luck.
    """
    for _ in range(timeout):
        if probe():
            return True
        ws.send_json({"type": "ping"})
        while ws.receive_json().get("type") != "pong":
            pass
    return probe()


def test_the_first_edit_keeps_the_original_and_marks_the_row(client):
    tid, mid = _seed_http(client)
    r = client.patch(f"/api/messages/{mid}", json={"content": "hello, world"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["content"] == "hello, world"
    assert body["metadata"]["original"] == "hello there"
    assert body["metadata"]["edit_count"] == 1
    assert body["metadata"]["edited_at"]

    r = client.patch(f"/api/messages/{mid}", json={"content": "hello again"})
    body = r.json()
    assert body["metadata"]["original"] == "hello there", (
        "the SECOND edit must not overwrite the original with the first "
        "edit's text — 'show original' would then show a draft nobody wrote")
    assert body["metadata"]["edit_count"] == 2


def test_hiding_a_row_is_the_absence_of_the_flag_when_it_is_undone(client):
    tid, mid = _seed_http(client)
    body = client.patch(f"/api/messages/{mid}", json={"hidden": True}).json()
    assert body["metadata"]["hidden"] is True
    body = client.patch(f"/api/messages/{mid}", json={"hidden": False}).json()
    assert "hidden" not in (body["metadata"] or {}), (
        "un-hidden must read identically to never-hidden, not as a stored False")


def test_an_edit_is_rejected_in_safe_mode(client):
    """VIEW + SEND. A locked tablet rewriting what somebody said is the worst
    version of this feature."""
    tid, mid = _seed_http(client, bot_id="alpha")
    auth.set_pin("4321")
    auth._cache = None
    r = client.patch(f"/api/messages/{mid}", json={"content": "not me"},
                     headers={"origin": "http://127.0.0.1:8765"})
    assert r.status_code == 403
    msgs = client.get(f"/api/threads/{tid}/messages",
                      headers={"origin": "http://127.0.0.1:8765"}).json()["messages"]
    assert msgs[-1]["content"] == "hello there"


def test_an_empty_edit_is_refused(client):
    tid, mid = _seed_http(client)
    assert client.patch(f"/api/messages/{mid}", json={"content": "   "}).status_code == 400
    assert client.patch(f"/api/messages/{mid}", json={}).status_code == 400
    assert client.patch(f"/api/messages/{mid}",
                        json={"hidden": "yes"}).status_code == 400


def test_a_hidden_row_leaves_the_model_context_but_stays_in_the_thread():
    """The point of the flag. Dropping it from the transcript instead would be
    the same lie one layer down."""
    class Row:
        def __init__(self, role, content, metadata=None):
            self.role, self.content, self.metadata = role, content, metadata

    rows = [
        Row("user", "first question"),
        Row("assistant", "a wrong answer", {"hidden": True}),
        Row("user", "second question"),
    ]
    history = llm_api.build_history(rows)
    assert [h["content"] for h in history] == ["first question\n\nsecond question"]
    assert all("wrong answer" not in h["content"] for h in history)


# --------------------------------------------------------------------------- #
# Safe Mode: regenerate is unlocked-only, and `retry` is untouched
#
# This is a deliberate departure from the plan, which made regenerate a plain
# mutation and therefore a 403 for locked devices. Regenerate rewinds an
# agent's session and deletes rows, so it stays unlocked-only — but plain
# retry is something the family's tablets can do TODAY, and taking it away to
# ship a nicer button for the operator would be a downgrade for everyone else.
# --------------------------------------------------------------------------- #

def test_the_regenerate_frame_is_wired_and_reaches_the_rewind(client, monkeypatch):
    tid, mid = _seed_http(client)
    seen: list = []

    async def _fake(thread_id, bot_id, user_msg, *, text=None,
                    expect_text=None):
        seen.append((thread_id, bot_id, user_msg.id, text))

    monkeypatch.setattr(main, "_regenerate_turn", _fake)
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["decoy"] is False
        ws.send_json({"type": "regenerate", "thread_id": tid})
        _settle(ws, lambda: seen)
    assert [s[0] for s in seen] == [tid]


def test_regenerate_is_refused_on_a_locked_device(client, monkeypatch):
    tid, mid = _seed_http(client, bot_id="alpha")
    called: list = []

    async def _fake(thread_id, bot_id, user_msg, *, text=None,
                    expect_text=None):
        called.append(thread_id)

    monkeypatch.setattr(main, "_regenerate_turn", _fake)
    auth.set_pin("112233")
    client.cookies.clear()
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["decoy"] is True
        ws.send_json({"type": "regenerate", "thread_id": tid})
        frame = ws.receive_json()
    assert frame["type"] == "error" and "Unlock" in frame["message"]
    assert called == [], "a locked tablet must not rewind an agent's session"


def test_edit_rerun_is_refused_on_a_locked_device(client, monkeypatch):
    tid, mid = _seed_http(client, bot_id="alpha")
    called: list = []

    async def _fake(thread_id, bot_id, user_msg, *, text=None,
                    expect_text=None):
        called.append(thread_id)

    monkeypatch.setattr(main, "_regenerate_turn", _fake)
    auth.set_pin("332211")
    client.cookies.clear()
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["decoy"] is True
        ws.send_json({"type": "edit_rerun", "thread_id": tid,
                      "message_id": mid, "content": "rewritten"})
        frame = ws.receive_json()
    assert frame["type"] == "error" and "Unlock" in frame["message"]
    assert called == []
    msgs = client.get(f"/api/threads/{tid}/messages",
                      headers={"origin": "http://127.0.0.1:8765"}).json()["messages"]
    assert msgs[-1]["content"] == "hello there", "and nothing was rewritten"


def test_plain_retry_still_works_for_a_locked_device(client, monkeypatch):
    """Nothing is taken away from the family. `retry` is the frame they have
    always had: same behaviour, same daily quota, still no rewind."""
    tid, mid = _seed_http(client, bot_id="alpha")
    turns: list = []

    async def _fake_turn(thread_id, bot_id, text, opts=None):
        turns.append((thread_id, bot_id, text))

    monkeypatch.setattr(main, "run_agent_turn", _fake_turn)
    auth.set_pin("445566")
    client.cookies.clear()
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["decoy"] is True
        ws.send_json({"type": "retry", "thread_id": tid})
        _settle(ws, lambda: turns)
    assert [t[0] for t in turns] == [tid]


def test_edit_rerun_refuses_anything_but_the_newest_user_message(client, monkeypatch):
    """A v1 limit, and not timidity: a rewind addresses a session by the entry
    id of a user turn, and the newest one is the only turn DisPatch can
    identify unambiguously."""
    tid, first = _seed_http(client)
    client.post(f"/api/threads/{tid}/messages",
                json={"role": "user", "content": "and another thing"})
    called: list = []

    async def _fake(thread_id, bot_id, user_msg, *, text=None,
                    expect_text=None):
        called.append(thread_id)

    monkeypatch.setattr(main, "_regenerate_turn", _fake)
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"type": "edit_rerun", "thread_id": tid,
                      "message_id": first, "content": "changed my mind"})
        frame = ws.receive_json()
    assert frame["type"] == "error" and "newest" in frame["message"]
    assert called == []


def test_edit_rerun_hands_the_edit_to_the_regenerate_path(client, monkeypatch):
    """The handler no longer stores the edit itself (2026-09-24 review): a
    refused rewind used to leave the new question over the answer to the old
    one. It passes the OLD row plus `edit_to`, and _regenerate_turn stores
    the edit only once the rewind has succeeded — see
    test_review_20260924.py for both halves of that contract."""
    tid, mid = _seed_http(client)
    seen: list = []

    async def _fake(thread_id, bot_id, user_msg, *, text=None,
                    expect_text=None, edit_to=None):
        seen.append((user_msg.content, text, expect_text, edit_to))

    monkeypatch.setattr(main, "_regenerate_turn", _fake)
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"type": "edit_rerun", "thread_id": tid,
                      "message_id": mid, "content": "hello, world"})
        _settle(ws, lambda: seen)
    assert seen == [("hello there", "hello, world", "hello there", "hello, world")]
    msgs = client.get(f"/api/threads/{tid}/messages").json()["messages"]
    assert msgs[-1]["content"] == "hello there", (
        "the handler must not store the edit before the rewind has succeeded")


@pytest.mark.asyncio
async def test_a_rerun_compares_against_the_text_the_gateway_actually_holds(
        wired, monkeypatch, caplog):
    """An edit-and-rerun sends the NEW wording, but the gateway's copy of that
    turn is still the OLD one. Checking the new text against it would warn on
    every single edit, which is how a real warning gets trained out of people."""
    db, frames = wired
    await db.connect()
    try:
        thread, user, _ = await _seed(db)
        gw = RecordingGateway()          # editorText = the original question
        monkeypatch.setattr(main, "_gateway_client", gw)

        async def _fake_turn(thread_id, bot_id, text, opts=None):
            await db.add_message(thread_id, "assistant", "Paris.")

        monkeypatch.setattr(main, "run_agent_turn", _fake_turn)
        await db.update_message_content(
            user.id, "what is the capital city of France?")
        edited = await db.get_message(user.id)
        with caplog.at_level("WARNING"):
            await main._regenerate_turn(
                thread.id, "main", edited,
                text="what is the capital city of France?",
                expect_text="what is the capital of France?")
        assert gw.rewound, "the rewind must still happen"
        assert not [r for r in caplog.records if "regenerate:" in r.message], (
            "no mismatch warning: the comparison is against the pre-edit text")
    finally:
        await db.close()
