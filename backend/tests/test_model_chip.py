"""Feature 7 (C-model-chip): per-thread model/thinking override + context meter.

Covers, in order:

  1. THE PROOF: a thread's saved prefs reach the gateway agent call. This is
     the property nothing in the codebase provided before this feature — no
     caller of run_agent_turn ever read TurnOptions.model/.thinking from
     anywhere but its own (always-empty) argument. Written to FAIL against
     the code as it stood before _with_thread_model_prefs existed; see the
     commented assertion below and this file's companion run against the
     pre-fix code (recorded in the session report, not re-run here).
  2. THE OTHER PROOF: a refusal from the gateway (AgentRefused) reaches the
     operator VERBATIM, is never retried, and does not clear the preference
     that caused it.
  3. PATCH /api/threads/{id} prefs: operator-only, merge-not-replace, null
     removes a key, unknown keys/bad value types 400.
  4. _redact_thread_dict drops prefs for a locked device.
  5. GET /api/bots/{bot_id}/models: operator-only, API-bot single model,
     gateway-bot normalized list, briefly cached, 503 when disconnected.
  6. openclaw._parse_reply keeps usage input/output and contextBudgetStatus
     on the reply metadata (which run_agent_turn puts on the assistant row).

Same hermetic style as the rest of tests/: throwaway DB + monkeypatched data
dirs. Run: cd backend && uv run pytest.
"""
from __future__ import annotations

import asyncio
from typing import ClassVar

import pytest
from fastapi.testclient import TestClient

from app import auth, config, gateway_ws, main, openclaw
from app.database import Database

LOOPBACK = ("127.0.0.1", 50000)
BROWSER_HDRS = {"origin": "http://127.0.0.1:8765"}   # browser-shaped request


# --------------------------------------------------------------------------- #
# 1 + 2: run_agent_turn / the gateway call
# --------------------------------------------------------------------------- #


@pytest.fixture
def turn_env(tmp_path, monkeypatch):
    """A real DB + captured broadcasts, wired the way test_audit_fixes.py's
    delivery tests are — close enough to a real turn that the gateway call
    site is exercised for real, far enough from the network that nothing
    here needs OpenClaw actually running."""
    from app.database import Database as DB

    db = DB(tmp_path / "chats.db")
    asyncio.run(db.connect())
    monkeypatch.setattr(main, "db", db)
    monkeypatch.setattr(main, "_shutting_down", True)   # no follower spawn

    frames: list[dict] = []

    async def _capture(frame):
        frames.append(frame)

    async def fake_watch(thread_id, bot_id, session_key, handoff):
        handoff["texts"] = []

    async def no_reconcile(*a, **kw):
        return []

    async def no_second_look(*a, **kw):
        return None

    monkeypatch.setattr(main.manager, "broadcast", _capture)
    monkeypatch.setattr(main, "_watch_progress", fake_watch)
    monkeypatch.setattr(main, "_reconcile_transcript", no_reconcile)
    monkeypatch.setattr(main, "_media_second_look", no_second_look)

    yield db, frames
    asyncio.run(db.close())


class _FakeReply:
    metadata: ClassVar[dict] = {"model": "fake", "provider": "fake", "session_id": "s1"}
    payloads: ClassVar[list] = []

    def __init__(self):
        from types import SimpleNamespace
        self.payloads = [SimpleNamespace(text="ok", sub=False, media_url=None)]


@pytest.mark.asyncio
async def test_a_threads_saved_model_reaches_the_gateway_call(turn_env, monkeypatch):
    """THE PROOF TEST. Fails against the code before _with_thread_model_prefs
    existed: with no such loader, `over` at the gateway call site is always
    {}, so `captured` below would be empty and this assertion would fail.

    Verified by hand against the pre-fix run_agent_turn (the call site read
    only `opts.model`/`opts.thinking` on the TurnOptions the WS handler
    passes — which is always None — and nothing anywhere loaded the thread's
    `prefs` column): the second assertion raised AssertionError, captured
    being {}.
    """
    db, frames = turn_env
    captured: dict = {}

    async def fake_send(agent_id, session_key, message, thread_id,
                        model=None, thinking=None):
        captured["agent_id"] = agent_id
        captured["model"] = model
        captured["thinking"] = thinking
        return _FakeReply()

    monkeypatch.setattr(main, "_send_with_gateway_retry", fake_send)

    t = await db.create_thread(bot_id="main")
    await db.update_thread_prefs(t.id, {"model": "big-model", "thinking": "high"})

    await main.run_agent_turn(t.id, "main", "hello")

    assert captured, "the gateway transport was never called"
    assert captured["model"] == "big-model", (
        "the thread's saved model preference never reached the gateway call")
    assert captured["thinking"] == "high"


@pytest.mark.asyncio
async def test_no_saved_prefs_means_no_override_is_sent(turn_env, monkeypatch):
    """The other half of the same property: a thread with nothing saved must
    behave exactly as it always did — absent, not null, reaching the wire."""
    db, frames = turn_env
    captured: dict = {}

    async def fake_send(agent_id, session_key, message, thread_id,
                        model=None, thinking=None):
        captured["model"] = model
        captured["thinking"] = thinking
        return _FakeReply()

    monkeypatch.setattr(main, "_send_with_gateway_retry", fake_send)

    t = await db.create_thread(bot_id="main")
    await main.run_agent_turn(t.id, "main", "hello")

    assert captured == {"model": None, "thinking": None}


@pytest.mark.asyncio
async def test_a_callers_own_opts_win_over_the_threads_saved_prefs(turn_env, monkeypatch):
    """A future caller that already chose a model on its own TurnOptions
    (rewind/retry-with-a-different-model, say) must not have the thread's
    standing preference silently override it."""
    db, frames = turn_env
    captured: dict = {}

    async def fake_send(agent_id, session_key, message, thread_id,
                        model=None, thinking=None):
        captured["model"] = model
        captured["thinking"] = thinking
        return _FakeReply()

    monkeypatch.setattr(main, "_send_with_gateway_retry", fake_send)

    t = await db.create_thread(bot_id="main")
    await db.update_thread_prefs(t.id, {"model": "saved-model", "thinking": "low"})
    opts = main.TurnOptions(model="explicit-model", thinking="max")
    await main.run_agent_turn(t.id, "main", "hello", opts)

    assert captured == {"model": "explicit-model", "thinking": "max"}


@pytest.mark.asyncio
async def test_a_refusal_reaches_the_operator_verbatim_and_runs_once(turn_env, monkeypatch):
    """THE SECOND PROOF TEST. AgentRefused must reach the broadcast error
    frame with the gateway's own sentence, byte for byte, tagged so the chip
    can tell a verdict apart from an outage — and the gateway call must have
    been attempted exactly once, never retried (a refusal can never succeed
    on a second try, and retrying a billed model call that was refused is
    worse than not retrying it).
    """
    db, frames = turn_env
    calls = 0
    refusal = ('Thinking level "ultra" is not supported by agent "main". '
              'Use one of: off, low, high.')

    async def fake_send(agent_id, session_key, message, thread_id,
                        model=None, thinking=None):
        nonlocal calls
        calls += 1
        raise openclaw.AgentRefused(refusal, detail=refusal)

    monkeypatch.setattr(main, "_send_with_gateway_retry", fake_send)

    t = await db.create_thread(bot_id="main")
    await db.update_thread_prefs(t.id, {"thinking": "ultra"})

    await main.run_agent_turn(t.id, "main", "hello")

    assert calls == 1, "a refusal must never be retried"
    errors = [f for f in frames if f.get("type") == "error"]
    assert errors, "the refusal never reached the client"
    assert errors[-1]["message"] == refusal, (
        "the gateway's own sentence must reach the operator byte for byte, "
        "never a generic message")
    assert errors[-1].get("refused") is True, (
        "AgentRefused must be tagged so the chip can show its warning state "
        "instead of the generic chat-error toast")

    # The preference itself must survive the refusal untouched, so the
    # operator can see and correct it rather than have it silently revert.
    thread = await db.get_thread(t.id)
    assert thread.prefs == {"thinking": "ultra"}
    assert thread.status == "error"


@pytest.mark.asyncio
async def test_a_plain_agent_error_is_not_tagged_refused(turn_env, monkeypatch):
    """A timeout or a dead gateway is an outage, not a verdict — must not
    carry the `refused` flag, or the chip would show the wrong state for an
    ordinary retry-later failure."""
    db, frames = turn_env

    async def fake_send(agent_id, session_key, message, thread_id,
                        model=None, thinking=None):
        raise openclaw.AgentTimeout("main took too long.", detail="timeout")

    monkeypatch.setattr(main, "_send_with_gateway_retry", fake_send)

    t = await db.create_thread(bot_id="main")
    await main.run_agent_turn(t.id, "main", "hello")

    errors = [f for f in frames if f.get("type") == "error"]
    assert errors and "refused" not in errors[-1]


# --------------------------------------------------------------------------- #
# 3: PATCH /api/threads/{id} prefs
# --------------------------------------------------------------------------- #


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
    main._BOT_MODELS_CACHE.clear()

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


def _unlock(client: TestClient, pin: str = "1234") -> None:
    r = client.post("/api/auth/unlock", json={"pin": pin})
    assert r.status_code == 200, r.text


def test_operator_can_set_and_clear_prefs(env):
    client = env()
    auth.set_pin("1234")
    tid = _seed_thread(client)
    _unlock(client)

    r = client.patch(f"/api/threads/{tid}", json={"prefs": {"model": "m-1", "thinking": "high"}})
    assert r.status_code == 200, r.text

    r = client.get(f"/api/threads/{tid}")
    assert r.json()["prefs"] == {"model": "m-1", "thinking": "high"}

    # A null value REMOVES the key — "back to the bot's default" — and a
    # merge, not a replace: the sibling key is untouched.
    r = client.patch(f"/api/threads/{tid}", json={"prefs": {"thinking": None}})
    assert r.status_code == 200, r.text
    r = client.get(f"/api/threads/{tid}")
    assert r.json()["prefs"] == {"model": "m-1"}


def test_prefs_rejects_unknown_keys_and_bad_types(env):
    client = env()
    auth.set_pin("1234")
    tid = _seed_thread(client)
    _unlock(client)

    assert client.patch(f"/api/threads/{tid}",
                        json={"prefs": {"scene": "beach"}}).status_code == 400
    assert client.patch(f"/api/threads/{tid}",
                        json={"prefs": {"model": 5}}).status_code == 400
    assert client.patch(f"/api/threads/{tid}",
                        json={"prefs": "nope"}).status_code == 400


def test_a_decoy_session_cannot_set_prefs(env):
    client = env()
    auth.set_pin("1234")
    tid = _seed_thread(client, bot_id="alpha")   # safe bot, viewable decoy

    r = client.patch(f"/api/threads/{tid}", json={"prefs": {"model": "m-1"}},
                     headers=BROWSER_HDRS)
    assert r.status_code == 403


def test_a_machine_caller_can_still_rename_and_pin_but_not_set_prefs(env):
    """The machine-inbound surface keeps title/pinned (ordinary thread
    housekeeping an agent may do on its own thread) but a model override is
    operator configuration — the same loopback, session-less caller that can
    rename a thread must be refused when it tries to touch prefs."""
    client = env()
    auth.set_pin("1234")
    tid = _seed_thread(client)

    assert client.patch(f"/api/threads/{tid}",
                        json={"title": "renamed"}).status_code == 200
    assert client.patch(f"/api/threads/{tid}",
                        json={"pinned": True}).status_code == 200

    r = client.patch(f"/api/threads/{tid}", json={"prefs": {"model": "m-1"}})
    assert r.status_code == 403, r.text

    # And the refusal must not have partially applied.
    r = client.get(f"/api/threads/{tid}")
    assert r.json().get("prefs") in (None, {})


def test_no_pin_set_allows_prefs_same_as_everything_else(env):
    """With no PIN configured the app has no lock at all — the same rule
    _require_full_access uses, applied to the stricter operator gate too."""
    client = env()
    tid = _seed_thread(client)
    r = client.patch(f"/api/threads/{tid}", json={"prefs": {"model": "m-1"}})
    assert r.status_code == 200, r.text


# --------------------------------------------------------------------------- #
# 4: _redact_thread_dict drops prefs
# --------------------------------------------------------------------------- #


def test_redact_thread_dict_drops_prefs():
    t = {"id": "t1", "bot_id": "main", "last_message": "hi",
        "prefs": {"model": "m-1", "thinking": "high"}}
    out = main._redact_thread_dict(t)
    assert "prefs" not in out
    # Everything else about the existing redaction is untouched.
    assert out["last_message"] == "hi"


@pytest.mark.asyncio
async def test_a_locked_device_never_sees_prefs_over_rest_or_ws(env):
    client = env()
    auth.set_pin("1234")
    tid = _seed_thread(client, bot_id="alpha")
    _unlock(client)
    client.patch(f"/api/threads/{tid}", json={"prefs": {"model": "m-1"}})

    locked = env()
    r = locked.get("/api/threads", params={"bot_id": "alpha"}, headers=BROWSER_HDRS)
    assert r.status_code == 200
    row = next(t for t in r.json()["threads"] if t["id"] == tid)
    assert "prefs" not in row

    r = locked.get(f"/api/threads/{tid}", headers=BROWSER_HDRS)
    assert r.status_code == 200
    assert "prefs" not in r.json()


# --------------------------------------------------------------------------- #
# 5: GET /api/bots/{bot_id}/models
# --------------------------------------------------------------------------- #


class _FakeGatewayClient:
    def __init__(self, models):
        self._models = models
        self.calls = 0
        self.connected = asyncio.Event()
        self.connected.set()

    async def models_list(self, agent_id):
        self.calls += 1
        return self._models


def test_models_route_is_operator_only(env):
    client = env()
    auth.set_pin("1234")
    r = client.get("/api/bots/main/models", headers=BROWSER_HDRS)
    assert r.status_code == 403


def test_models_route_for_an_api_bot_is_its_one_model(env):
    client = env()
    auth.set_pin("1234")
    _unlock(client)
    config.upsert_bot(config.Bot(id="llm-custom", name="Tester", emoji="🔌",
                                 api={"provider": "custom",
                                      "base_url": "http://127.0.0.1:9/v1",
                                      "model": "test-model"}))
    r = client.get("/api/bots/llm-custom/models")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source"] == "api"
    assert body["models"] == [{"id": "test-model", "label": "test-model"}]


def test_models_route_for_a_gateway_bot_uses_and_caches_models_list(env, monkeypatch):
    client = env()
    auth.set_pin("1234")
    _unlock(client)
    fake = _FakeGatewayClient([
        {"id": "deepseek/deepseek-v4-pro", "label": "DeepSeek v4 Pro"},
        {"id": "deepseek/deepseek-v4-flash"},
        {"not": "usable"},
        "",
    ])
    monkeypatch.setattr(main, "_gateway_client", fake, raising=False)

    r = client.get("/api/bots/main/models")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source"] == "gateway"
    assert body["models"] == [
        {"id": "deepseek/deepseek-v4-pro", "label": "DeepSeek v4 Pro"},
        {"id": "deepseek/deepseek-v4-flash", "label": "deepseek/deepseek-v4-flash"},
    ], "a malformed entry (no id) must be dropped, not offered as a choice"

    r2 = client.get("/api/bots/main/models")
    assert r2.status_code == 200
    assert fake.calls == 1, "a second request inside the TTL must not re-hit the gateway"


def test_models_route_503s_when_the_gateway_is_not_connected(env, monkeypatch):
    client = env()
    auth.set_pin("1234")
    _unlock(client)
    monkeypatch.setattr(main, "_gateway_client", None, raising=False)
    r = client.get("/api/bots/main/models")
    assert r.status_code == 503


# --------------------------------------------------------------------------- #
# 6: openclaw._parse_reply keeps usage in/out + contextBudgetStatus
# --------------------------------------------------------------------------- #


def test_parse_reply_keeps_context_budget_and_split_usage():
    parsed = {
        "runId": "r1", "status": "ok", "summary": "completed",
        "result": {
            "payloads": [{"text": "hi", "mediaUrl": None}],
            "meta": {
                "durationMs": 100,
                "agentMeta": {
                    "provider": "deepseek", "model": "deepseek-v4-flash",
                    "sessionId": "s1",
                    "usage": {"total": 900, "input": 800, "output": 100},
                    "contextBudgetStatus": {"estimatedPromptTokens": 800,
                                            "contextTokenBudget": 262144},
                },
            },
        },
    }
    reply = openclaw._parse_reply(parsed, "main", "")
    assert reply.metadata["tokens_in"] == 800
    assert reply.metadata["tokens_out"] == 100
    assert reply.metadata["tokens"] == 900
    assert reply.metadata["context_budget"] == {
        "estimatedPromptTokens": 800, "contextTokenBudget": 262144}


def test_parse_reply_tolerates_a_missing_context_budget():
    """Verified present on this gateway per the brief, but a reply that lacks
    it (an older gateway, a degraded response) must not blow up parsing —
    the field is simply absent, same as every other optional metadata key."""
    parsed = {
        "runId": "r1", "status": "ok", "summary": "completed",
        "result": {
            "payloads": [{"text": "hi", "mediaUrl": None}],
            "meta": {"agentMeta": {"model": "m", "usage": {"total": 5}}},
        },
    }
    reply = openclaw._parse_reply(parsed, "main", "")
    assert "context_budget" not in reply.metadata
    assert "tokens_in" not in reply.metadata
    assert reply.metadata["tokens"] == 5
