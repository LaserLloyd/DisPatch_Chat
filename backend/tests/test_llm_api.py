"""The "Connect an AI" backend: presets, gating, connect, and a whole turn.

Same hermetic style as the rest of tests/: throwaway DB + monkeypatched data
dirs, and — the point of this file — **no network at all**. Every provider call
goes through an ``httpx.MockTransport`` (injected by swapping the module's own
``http_client`` factory) or a stub Anthropic client, so the suite is identical
on a laptop with no keys and a CI box with no internet.

What is worth testing here, in order of how much it would hurt to get wrong:

  1. the routes are admin-only — they write an API key to disk;
  2. a saved key never comes back out of the API;
  3. a turn that fails leaves no thread stuck in 'thinking';
  4. the history window keeps the NEWEST messages, in order.

Run: cd backend && uv run pytest tests/test_llm_api.py.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import types

import httpx
import pytest
from fastapi.testclient import TestClient

from app import auth, config, llm_api, main
from app.database import Database
from app.models import MessageOut

# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def llm_env(tmp_path, monkeypatch):
    """Isolated data dir + DB + auth store. Yields a TestClient factory."""
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
    main._delivered.clear()
    main._thread_bot.clear()

    clients: list[TestClient] = []

    def make_client() -> TestClient:
        c = TestClient(main.app)
        c.__enter__()
        clients.append(c)
        return c

    yield make_client

    for c in clients:
        c.__exit__(None, None, None)
    asyncio.run(temp_db.close())


@pytest.fixture
def db_env(tmp_path, monkeypatch):
    """Just the config paths + a connected Database, for the turn tests."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(config, "MEDIA_DIR", tmp_path / "media")
    monkeypatch.setattr(config, "FILES_DIR", tmp_path / "files")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(main, "MEDIA_DIR", tmp_path / "media")
    config._invalidate_bots_cache()
    temp_db = Database(tmp_path / "chats.db")
    monkeypatch.setattr(main, "db", temp_db)
    main._delivered.clear()
    main._thread_bot.clear()

    async def _open():
        await temp_db.connect()

    asyncio.run(_open())
    yield temp_db
    asyncio.run(temp_db.close())


def _mock_http(monkeypatch, handler):
    """Point llm_api's client factory at an in-process transport."""
    def factory(read_timeout: float = llm_api.READ_TIMEOUT_S) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                 timeout=read_timeout, follow_redirects=False)
    monkeypatch.setattr(llm_api, "http_client", factory)


def _unlock(client: TestClient, pin: str = "1234") -> None:
    client.post("/api/auth/setup", json={"new_pin": pin})


def _api_bot(bot_id: str = "llm-custom", **api) -> None:
    """Write a direct-provider bot straight into config.yaml."""
    cfg = {"provider": "custom", "base_url": "http://127.0.0.1:9/v1",
           "model": "test-model"}
    cfg.update(api)
    config.upsert_bot(config.Bot(id=bot_id, name="Tester", emoji="🔌", api=cfg))


# --------------------------------------------------------------------------- #
# Preset table
# --------------------------------------------------------------------------- #


def test_every_preset_is_well_formed():
    ids = [p["id"] for p in llm_api.providers_public()]
    assert len(ids) == len(set(ids)), "duplicate provider id"
    for p in llm_api.providers_public():
        assert p["label"], f"{p['id']} has no label"
        assert p["kind"] in ("openai", "anthropic")
        # Every preset except the deliberately blank "custom" ships a usable
        # default URL — that is the whole point of a preset.
        if p["id"] != "custom":
            assert p["base_url"].startswith(("http://", "https://")), p["id"]
        assert p["bot_name"], f"{p['id']} has no short bot name"
        if p["key_required"]:
            assert p["key_accepted"], f"{p['id']} needs a key but hides the field"
            # The panel's link reads "Where do I get a key?" — a preset that
            # requires one has to be able to answer that.
            assert p["docs"].startswith("https://"), p["id"]
        else:
            assert not p["docs"], \
                f"{p['id']} needs no key, so the 'where do I get a key' link " \
                "would be nonsense — leave docs empty"


def test_local_presets_need_no_key_and_remote_ones_do():
    by_id = {p["id"]: p for p in llm_api.providers_public()}
    for local in ("lmstudio", "ollama"):
        assert by_id[local]["local"] is True
        assert by_id[local]["key_required"] is False
        assert by_id[local]["key_accepted"] is False
    for remote in ("openai", "anthropic", "deepseek", "groq", "openrouter",
                   "mistral", "xai", "together"):
        assert by_id[remote]["key_required"] is True, remote
        assert by_id[remote]["key_env"], f"{remote} should name its env var"
    # "custom" is an OpenAI-compatible server the operator names themselves.
    assert by_id["custom"]["editable_base_url"] is True
    assert by_id["custom"]["key_required"] is False


def test_presets_never_leak_a_key():
    """The preset payload names environment VARIABLES; there is no field in it
    that COULD hold a value. Pinning the exact field set (rather than grepping
    the JSON for "api_key", which a docs URL legitimately contains) is the
    check that survives someone adding a field later."""
    for p in llm_api.providers_public():
        assert set(p) == {"id", "label", "bot_name", "base_url", "kind",
                          "key_required", "key_accepted", "key_env", "docs",
                          "local", "models", "editable_base_url"}, p["id"]


def test_anthropic_preset_uses_the_official_sdk_shape():
    a = llm_api.PROVIDER_BY_ID["anthropic"]
    assert a.kind == "anthropic"
    assert a.models[0] == "claude-opus-5"
    assert set(a.models) == {"claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"}


# --------------------------------------------------------------------------- #
# Route gating — these routes write an API key to disk
# --------------------------------------------------------------------------- #


LLM_ROUTES = [
    ("GET", "/api/llm/providers", None),
    ("POST", "/api/llm/test", {"provider": "lmstudio"}),
    ("POST", "/api/llm/connect", {"backend": "legacy-api", "provider": "lmstudio", "model": "x"}),
]


@pytest.mark.parametrize("method,path,body", LLM_ROUTES)
def test_sessionless_caller_is_refused_when_a_pin_is_set(llm_env, method, path, body):
    client = llm_env()
    _unlock(client)                       # sets a PIN and hands us a session
    client.cookies.clear()                # …and now we are Safe Mode
    r = client.request(method, path, json=body)
    assert r.status_code in (401, 403)
    assert "unlock" in r.text.lower() or "sign in" in r.text.lower()


@pytest.mark.parametrize("method,path,body", LLM_ROUTES)
def test_decoy_is_blocked_by_the_middleware_too(llm_env, method, path, body):
    """Belt-and-braces: the prefix is in _signed_out_blocked, so a limited session is
    turned away one layer before the route's own dependency runs."""
    assert main._signed_out_blocked(method, path) is True
    client = llm_env()
    _unlock(client)
    client.cookies.clear()
    assert client.request(method, path, json=body).status_code in (401, 403)


@pytest.mark.parametrize("method,path,body", LLM_ROUTES)
def test_open_install_with_no_pin_may_use_them(llm_env, method, path, body,
                                               monkeypatch):
    """No PIN configured = the whole app is open, and this is no exception —
    the same rule the dashboard uses. (Nothing reaches the network: `test`
    fails on an unreachable host, which is still a 200 with ok:false.)"""
    _mock_http(monkeypatch, lambda req: httpx.Response(200, json={"data": []}))
    client = llm_env()
    r = client.request(method, path, json=body)
    assert r.status_code == 200, r.text


# --------------------------------------------------------------------------- #
# The probe
# --------------------------------------------------------------------------- #


def test_probe_lists_models(llm_env, monkeypatch):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"data": [{"id": "b-model"}, {"id": "a-model"}]})

    _mock_http(monkeypatch, handler)
    client = llm_env()
    r = client.post("/api/llm/test", json={
        "provider": "custom", "base_url": "http://127.0.0.1:9999/v1",
        "api_key": "sk-test"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["models"] == ["a-model", "b-model"], "models are sorted + de-duped"
    assert seen["url"] == "http://127.0.0.1:9999/v1/models"
    assert seen["auth"] == "Bearer sk-test"


def test_probe_reports_a_rejected_key_without_raising(llm_env, monkeypatch):
    _mock_http(monkeypatch, lambda req: httpx.Response(
        401, json={"error": {"message": "Incorrect API key provided"}}))
    client = llm_env()
    r = client.post("/api/llm/test", json={"provider": "openai", "api_key": "sk-bad"})
    assert r.status_code == 200, "a provider-side failure is data, not an HTTP error"
    body = r.json()
    assert body["ok"] is False
    assert "key" in body["error"].lower()
    assert "Incorrect API key provided" in body["error"], \
        "the provider's own sentence has to survive to the operator"


def test_probe_falls_back_to_the_chat_endpoint_when_models_is_missing(
        llm_env, monkeypatch):
    """A server with no /models is common. Reporting it unreachable would be a
    lie — prove the endpoint that matters instead."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/models"):
            return httpx.Response(404, json={"error": "no such route"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]})

    _mock_http(monkeypatch, handler)
    client = llm_env()
    r = client.post("/api/llm/test", json={
        "provider": "custom", "base_url": "http://127.0.0.1:9999/v1",
        "model": "local-model"})
    assert r.json() == {"ok": True, "models": []}
    assert calls == ["/v1/models", "/v1/chat/completions"]


@pytest.mark.parametrize("bad", [
    "file:///etc/passwd",
    "ftp://example.com/v1",
    "127.0.0.1:1234/v1",          # urlparse reads "127.0.0.1" as the SCHEME
    "gopher://example.com",
])
def test_probe_rejects_non_http_schemes(llm_env, bad):
    """An operator pointing at any HOST is by design. A non-http SCHEME is not:
    file:// would have the server read its own filesystem."""
    client = llm_env()
    r = client.post("/api/llm/test", json={"provider": "custom", "base_url": bad})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "http" in body["error"].lower()


def test_normalize_base_url_rejects_non_http_directly():
    for bad in ("file:///tmp", "ws://x/v1", "", "   "):
        with pytest.raises(llm_api.ApiError):
            llm_api.normalize_base_url(bad)
    assert llm_api.normalize_base_url("http://h:1/v1/") == "http://h:1/v1"


def test_probe_rejects_an_unknown_provider(llm_env):
    client = llm_env()
    r = client.post("/api/llm/test", json={"provider": "not-a-provider"})
    assert r.json()["ok"] is False
    assert "not-a-provider" in r.json()["error"]


# --------------------------------------------------------------------------- #
# Connect: create, update, and never hand the key back
# --------------------------------------------------------------------------- #


def test_connect_creates_a_bot_that_round_trips_through_load_bots(llm_env):
    client = llm_env()
    r = client.post("/api/llm/connect", json={"backend": "legacy-api",
        "provider": "openai", "model": "gpt-4o-mini", "api_key": "sk-secret-123",
        "name": "Nova", "system_prompt": "Be brief."})
    assert r.status_code == 200, r.text
    bot = r.json()["bot"]
    assert bot["id"] == "llm-openai"
    assert bot["name"] == "Nova"   # the Advanced override wins
    assert bot["safe"] is False, "a new provider is never exposed to Safe Mode"
    assert bot["reactions"] is False
    assert bot["api"]["has_key"] is True
    assert "api_key" not in json.dumps(bot), "the key must never come back out"
    assert "sk-secret-123" not in r.text

    loaded = config.get_bot("llm-openai")
    assert loaded is not None
    assert loaded.api["api_key"] == "sk-secret-123", "…but it IS persisted"
    assert loaded.api["model"] == "gpt-4o-mini"
    assert loaded.api["system_prompt"] == "Be brief."
    assert loaded.model_hint == "gpt-4o-mini", "the model pill has a value from the start"


def test_connect_updates_the_same_bot_instead_of_making_a_second_one(llm_env):
    client = llm_env()
    before = len(config.load_bots())
    client.post("/api/llm/connect", json={"backend": "legacy-api",
        "provider": "openai", "model": "gpt-4o-mini", "api_key": "sk-one"})
    r = client.post("/api/llm/connect", json={"backend": "legacy-api",
        "provider": "openai", "model": "gpt-4o", "api_key": "sk-two"})
    assert r.status_code == 200
    assert len(config.load_bots()) == before + 1, "re-running the panel adds no duplicate"
    assert config.get_bot("llm-openai").api["model"] == "gpt-4o"
    assert config.get_bot("llm-openai").api["api_key"] == "sk-two"


def test_reconnecting_without_a_key_keeps_the_stored_one(llm_env):
    """Changing the model must not require re-pasting the credential — the
    panel sends a blank key field when the operator leaves it alone."""
    client = llm_env()
    client.post("/api/llm/connect", json={"backend": "legacy-api",
        "provider": "openai", "model": "gpt-4o-mini", "api_key": "sk-keep"})
    client.post("/api/llm/connect", json={"backend": "legacy-api", "provider": "openai", "model": "gpt-4o"})
    assert config.get_bot("llm-openai").api["api_key"] == "sk-keep"


def test_connect_refuses_to_hijack_an_existing_agent_bot(llm_env):
    client = llm_env()
    r = client.post("/api/llm/connect", json={"backend": "legacy-api",
        "provider": "openai", "model": "gpt-4o-mini", "api_key": "sk-x",
        "bot_id": "alpha"})
    assert r.status_code == 400
    assert "not an API bot" in r.json()["detail"]
    assert config.get_bot("alpha").api is None


def test_connect_requires_a_key_for_a_provider_that_needs_one(llm_env):
    client = llm_env()
    r = client.post("/api/llm/connect", json={"backend": "legacy-api", "provider": "openai", "model": "gpt-4o"})
    assert r.status_code == 400
    assert "key" in r.json()["detail"].lower()
    assert config.get_bot("llm-openai") is None


def test_connect_accepts_an_env_var_instead_of_a_stored_key(llm_env, monkeypatch):
    monkeypatch.setenv("MY_TEST_KEY", "sk-from-env")
    client = llm_env()
    r = client.post("/api/llm/connect", json={"backend": "legacy-api",
        "provider": "openai", "model": "gpt-4o", "api_key_env": "MY_TEST_KEY"})
    assert r.status_code == 200
    stored = config.get_bot("llm-openai").api
    assert stored["api_key_env"] == "MY_TEST_KEY"
    assert "api_key" not in stored, "no secret was written to disk at all"
    assert r.json()["bot"]["api"]["has_key"] is False


def test_local_provider_needs_no_key(llm_env):
    client = llm_env()
    r = client.post("/api/llm/connect", json={"backend": "legacy-api", "provider": "lmstudio", "model": "any"})
    assert r.status_code == 200
    assert config.get_bot("llm-lmstudio").api["base_url"] == "http://127.0.0.1:1234/v1"


def test_public_bot_list_exposes_the_provider_but_no_url_or_key(llm_env):
    client = llm_env()
    client.post("/api/llm/connect", json={"backend": "legacy-api",
        "provider": "openai", "model": "gpt-4o", "api_key": "sk-secret-abc"})
    body = client.get("/api/bots").text
    assert "sk-secret-abc" not in body
    assert "api.openai.com" not in body, "Safe Mode has no business knowing the URL"
    entry = next(b for b in client.get("/api/bots").json()["bots"]
                 if b["id"] == "llm-openai")
    assert entry["api_provider"] == "openai"


def test_config_yaml_is_owner_only_once_it_can_hold_a_key(llm_env):
    import stat
    client = llm_env()
    client.post("/api/llm/connect", json={"backend": "legacy-api",
        "provider": "openai", "model": "gpt-4o", "api_key": "sk-secret"})
    mode = stat.S_IMODE(config.CONFIG_PATH.stat().st_mode)
    assert mode == 0o600, f"config.yaml is {oct(mode)} but now holds credentials"


def test_auth_status_reports_the_api_bot_count(llm_env):
    client = llm_env()
    assert client.get("/api/auth/status").json()["features"]["api_bots"] == 0
    client.post("/api/llm/connect", json={"backend": "legacy-api", "provider": "lmstudio", "model": "m"})
    assert client.get("/api/auth/status").json()["features"]["api_bots"] == 1


# --------------------------------------------------------------------------- #
# Key resolution
# --------------------------------------------------------------------------- #


def test_env_var_wins_over_a_stored_key(monkeypatch):
    monkeypatch.setenv("PICKED", "from-env")
    assert llm_api.resolve_key({"api_key": "on-disk", "api_key_env": "PICKED"}) == "from-env"


def test_stored_key_is_used_when_the_env_var_is_absent_or_blank(monkeypatch):
    monkeypatch.delenv("MISSING_VAR", raising=False)
    assert llm_api.resolve_key({"api_key": "on-disk", "api_key_env": "MISSING_VAR"}) == "on-disk"
    monkeypatch.setenv("BLANK_VAR", "   ")
    assert llm_api.resolve_key({"api_key": "on-disk", "api_key_env": "BLANK_VAR"}) == "on-disk"


# --------------------------------------------------------------------------- #
# History windowing
# --------------------------------------------------------------------------- #


def _msg(role: str, content: str, **meta) -> MessageOut:
    return MessageOut(id=f"m{content[:4]}", thread_id="t", role=role,
                      content=content, created_at="2026-08-04T00:00:00",
                      metadata=meta or None)


def test_history_keeps_order_and_maps_roles():
    rows = [_msg("user", "one"), _msg("assistant", "two"), _msg("user", "three")]
    assert llm_api.build_history(rows) == [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "two"},
        {"role": "user", "content": "three"},
    ]


def test_history_drops_system_rows_and_collapsed_working_output():
    rows = [
        _msg("user", "hello"),
        _msg("system", "a notice"),
        _msg("assistant", "tool chatter", sub=True),
        _msg("assistant", "the answer"),
    ]
    assert llm_api.build_history(rows) == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "the answer"},
    ]


def test_history_window_keeps_the_NEWEST_messages():
    rows = [_msg("user", "x" * 100), _msg("assistant", "y" * 100),
            _msg("user", "z" * 100)]
    out = llm_api.build_history(rows, budget=150)
    # Budget stops the walk before the oldest turn is admitted; whatever
    # survives is a suffix of the conversation, still oldest→newest.
    assert out[-1]["content"] == "z" * 100
    assert sum(len(t["content"]) for t in out) <= 250
    assert [t["content"] for t in out] == [m.content for m in rows][-len(out):]


def test_history_always_admits_at_least_the_last_message():
    """A single message longer than the whole budget must still be sent —
    dropping it would send an empty conversation."""
    out = llm_api.build_history([_msg("user", "q" * 5000)], budget=100)
    assert out == [{"role": "user", "content": "q" * 5000}]


def test_history_merges_consecutive_same_role_turns():
    """Anthropic rejects them outright; merging is the portable answer."""
    rows = [_msg("user", "a"), _msg("user", "b"), _msg("assistant", "c")]
    assert llm_api.build_history(rows) == [
        {"role": "user", "content": "a\n\nb"},
        {"role": "assistant", "content": "c"},
    ]


def test_history_never_starts_with_an_assistant_turn():
    rows = [_msg("assistant", "unprompted"), _msg("user", "hi")]
    assert llm_api.build_history(rows) == [{"role": "user", "content": "hi"}]


def test_history_ignores_blank_rows():
    rows = [_msg("user", "hi"), _msg("assistant", "   "), _msg("user", "still here")]
    assert llm_api.build_history(rows) == [{"role": "user", "content": "hi\n\nstill here"}]


# --------------------------------------------------------------------------- #
# Anthropic
# --------------------------------------------------------------------------- #


class _FakeAnthropic:
    """Stands in for AsyncAnthropic: records the call, returns a canned reply."""

    def __init__(self, resp=None, error: Exception | None = None):
        self._resp = resp
        self._error = error
        self.seen: dict = {}
        self.messages = types.SimpleNamespace(create=self._create)
        self.models = types.SimpleNamespace(list=self._list)

    async def _create(self, **kwargs):
        self.seen = kwargs
        if self._error:
            raise self._error
        return self._resp

    async def _list(self):
        return types.SimpleNamespace(data=[types.SimpleNamespace(id="claude-opus-5")])

    async def close(self):
        pass


def _block(kind: str, text: str = ""):
    return types.SimpleNamespace(type=kind, text=text)


def _anthropic_reply(blocks, stop_reason="end_turn", model="claude-opus-5"):
    return types.SimpleNamespace(
        content=blocks, stop_reason=stop_reason, model=model,
        usage=types.SimpleNamespace(input_tokens=10, output_tokens=5))


def test_anthropic_refusal_is_surfaced_not_silently_empty(monkeypatch):
    """stop_reason == "refusal" is checked BEFORE the content blocks: they may
    be empty, and a blank bubble would look like a broken app."""
    fake = _FakeAnthropic(_anthropic_reply([], stop_reason="refusal"))
    monkeypatch.setattr(llm_api, "anthropic_client", lambda *a, **k: fake)
    cfg = llm_api.Resolved(
        provider=llm_api.PROVIDER_BY_ID["anthropic"],
        base_url="https://api.anthropic.com", model="claude-opus-5",
        api_key="sk-ant-x", system_prompt="be nice", max_history_chars=1000)
    with pytest.raises(llm_api.ApiError) as e:
        asyncio.run(llm_api.complete(cfg, [{"role": "user", "content": "hi"}]))
    assert "declined" in e.value.message.lower()
    assert "refusal" in e.value.detail.lower()


def test_anthropic_reads_only_text_blocks(monkeypatch):
    fake = _FakeAnthropic(_anthropic_reply(
        [_block("thinking"), _block("text", "Hello "), _block("text", "there.")]))
    monkeypatch.setattr(llm_api, "anthropic_client", lambda *a, **k: fake)
    cfg = llm_api.Resolved(
        provider=llm_api.PROVIDER_BY_ID["anthropic"],
        base_url="https://api.anthropic.com", model="claude-opus-5",
        api_key="sk-ant-x", system_prompt="be nice", max_history_chars=1000)
    reply = asyncio.run(llm_api.complete(cfg, [{"role": "user", "content": "hi"}]))
    assert reply.text == "Hello there."
    assert reply.model == "claude-opus-5"
    assert reply.tokens == 15
    # The system prompt travels as its own parameter, never as a message.
    assert fake.seen["system"] == "be nice"
    assert fake.seen["max_tokens"] == llm_api.DEFAULT_MAX_TOKENS
    assert fake.seen["messages"] == [{"role": "user", "content": "hi"}]


def test_anthropic_status_errors_map_to_an_actionable_message(monkeypatch):
    err = Exception("boom")
    err.status_code = 401
    fake = _FakeAnthropic(error=err)
    monkeypatch.setattr(llm_api, "anthropic_client", lambda *a, **k: fake)
    cfg = llm_api.Resolved(
        provider=llm_api.PROVIDER_BY_ID["anthropic"],
        base_url="https://api.anthropic.com", model="claude-opus-5",
        api_key="sk-ant-bad", system_prompt="s", max_history_chars=1000)
    with pytest.raises(llm_api.ApiError) as e:
        asyncio.run(llm_api.complete(cfg, [{"role": "user", "content": "hi"}]))
    assert "key" in e.value.message.lower()


# --------------------------------------------------------------------------- #
# A whole turn, through main.py's real delivery plumbing
# --------------------------------------------------------------------------- #


def _run_turn(db, thread_id, bot_id, text):
    async def go():
        await llm_api.run_api_turn(thread_id, bot_id, text)
    asyncio.run(go())


def test_run_api_turn_persists_the_reply_and_clears_thinking(db_env, monkeypatch):
    db = db_env
    _api_bot()
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        seen["path"] = request.url.path
        return httpx.Response(200, json={
            "model": "test-model-v9",
            "choices": [{"message": {"content": "Hello from the provider."}}],
            "usage": {"total_tokens": 42},
        })

    _mock_http(monkeypatch, handler)

    async def go():
        thread = await db.create_thread("llm-custom", owner_profile_id=None)
        await db.add_message(thread.id, "user", "are you there?")
        await llm_api.run_api_turn(thread.id, "llm-custom", "are you there?")
        msgs, _ = await db.list_messages(thread.id, 50)
        fresh = await db.get_thread(thread.id)
        return msgs, fresh

    msgs, thread = asyncio.run(go())

    assert seen["path"] == "/v1/chat/completions"
    assert seen["body"]["model"] == "test-model"
    assert seen["body"]["messages"][0]["role"] == "system"
    assert seen["body"]["messages"][-1] == {"role": "user", "content": "are you there?"}

    assistant = [m for m in msgs if m.role == "assistant"]
    assert len(assistant) == 1
    assert assistant[0].content == "Hello from the provider."
    # The model pill reads metadata.model, so the REAL model has to be there.
    assert assistant[0].metadata["model"] == "test-model-v9"
    assert assistant[0].metadata["provider"] == "custom"
    assert assistant[0].metadata["tokens"] == 42
    assert thread.status == "idle", "the thinking state must be cleared"


def test_run_api_turn_uses_the_configured_system_prompt(db_env, monkeypatch):
    db = db_env
    _api_bot(system_prompt="You are a pirate.")
    seen: dict = {}
    _mock_http(monkeypatch, lambda req: (
        seen.update(body=json.loads(req.content))
        or httpx.Response(200, json={"choices": [{"message": {"content": "Arr."}}]})))

    async def go():
        thread = await db.create_thread("llm-custom", owner_profile_id=None)
        await db.add_message(thread.id, "user", "hi")
        await llm_api.run_api_turn(thread.id, "llm-custom", "hi")

    asyncio.run(go())
    assert seen["body"]["messages"][0]["content"] == "You are a pirate."


def test_run_api_turn_defaults_the_system_prompt_to_the_bots_name(db_env, monkeypatch):
    db = db_env
    _api_bot()
    seen: dict = {}
    _mock_http(monkeypatch, lambda req: (
        seen.update(body=json.loads(req.content))
        or httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})))

    async def go():
        thread = await db.create_thread("llm-custom", owner_profile_id=None)
        await db.add_message(thread.id, "user", "hi")
        await llm_api.run_api_turn(thread.id, "llm-custom", "hi")

    asyncio.run(go())
    system = seen["body"]["messages"][0]["content"]
    assert "Tester" in system and "DisPatch" in system


@pytest.mark.parametrize("response,expect", [
    (httpx.Response(401, json={"error": {"message": "bad key"}}), "key"),
    (httpx.Response(404, json={"error": {"message": "no model"}}), "model"),
    (httpx.Response(500, text="upstream exploded"), "error"),
])
def test_run_api_turn_error_paths_never_hang_the_thread(db_env, monkeypatch,
                                                        response, expect):
    """Every failure mode must set 'error', broadcast, and stop the spinner.
    A thread stuck in 'thinking' is the one outcome worse than an error."""
    db = db_env
    _api_bot()
    _mock_http(monkeypatch, lambda req: response)
    frames: list[dict] = []

    async def capture(frame):
        frames.append(frame)

    monkeypatch.setattr(main.manager, "broadcast", capture)

    async def go():
        thread = await db.create_thread("llm-custom", owner_profile_id=None)
        await db.add_message(thread.id, "user", "hi")
        await llm_api.run_api_turn(thread.id, "llm-custom", "hi")
        msgs, _ = await db.list_messages(thread.id, 50)
        return await db.get_thread(thread.id), msgs

    thread, msgs = asyncio.run(go())
    assert thread.status == "error"
    assert not [m for m in msgs if m.role == "assistant"], "no half-reply was posted"

    kinds = [f["type"] for f in frames]
    assert "error" in kinds
    err = next(f for f in frames if f["type"] == "error")
    assert expect in (err["message"] + " " + err["detail"]).lower()
    # The spinner is stopped exactly once, and last.
    stops = [f for f in frames if f["type"] == "thinking" and f["status"] == "stopped"]
    assert len(stops) == 1
    assert frames.index(stops[0]) > kinds.index("error")


def test_run_api_turn_reports_an_unreachable_provider(db_env, monkeypatch):
    db = db_env
    _api_bot()

    def boom(request):
        raise httpx.ConnectError("connection refused")

    _mock_http(monkeypatch, boom)
    frames: list[dict] = []

    async def capture(frame):
        frames.append(frame)

    monkeypatch.setattr(main.manager, "broadcast", capture)

    async def go():
        thread = await db.create_thread("llm-custom", owner_profile_id=None)
        await db.add_message(thread.id, "user", "hi")
        await llm_api.run_api_turn(thread.id, "llm-custom", "hi")
        return await db.get_thread(thread.id)

    thread = asyncio.run(go())
    assert thread.status == "error"
    err = next(f for f in frames if f["type"] == "error")
    assert "reach" in err["message"].lower()


def test_run_api_turn_reports_an_empty_reply_instead_of_an_empty_bubble(
        db_env, monkeypatch):
    db = db_env
    _api_bot()
    _mock_http(monkeypatch, lambda req: httpx.Response(200, json={
        "choices": [{"message": {"content": ""}, "finish_reason": "length"}]}))
    frames: list[dict] = []

    async def capture(frame):
        frames.append(frame)

    monkeypatch.setattr(main.manager, "broadcast", capture)

    async def go():
        thread = await db.create_thread("llm-custom", owner_profile_id=None)
        await db.add_message(thread.id, "user", "hi")
        await llm_api.run_api_turn(thread.id, "llm-custom", "hi")
        msgs, _ = await db.list_messages(thread.id, 50)
        return msgs

    msgs = asyncio.run(go())
    assert not [m for m in msgs if m.role == "assistant"]
    err = next(f for f in frames if f["type"] == "error")
    assert "length" in err["detail"]


def test_run_api_turn_reports_a_misconfigured_bot(db_env, monkeypatch):
    """A model that was never set must fail with a sentence about the model,
    not a provider 400 an operator cannot decode."""
    db = db_env
    config.upsert_bot(config.Bot(id="llm-broken", name="Broken",
                                 api={"provider": "custom",
                                      "base_url": "http://127.0.0.1:9/v1"}))
    frames: list[dict] = []

    async def capture(frame):
        frames.append(frame)

    monkeypatch.setattr(main.manager, "broadcast", capture)

    async def go():
        thread = await db.create_thread("llm-broken", owner_profile_id=None)
        await db.add_message(thread.id, "user", "hi")
        await llm_api.run_api_turn(thread.id, "llm-broken", "hi")
        return await db.get_thread(thread.id)

    thread = asyncio.run(go())
    assert thread.status == "error"
    err = next(f for f in frames if f["type"] == "error")
    assert "model" in err["message"].lower()


def test_run_api_turn_sends_prior_context(db_env, monkeypatch):
    db = db_env
    _api_bot()
    seen: dict = {}
    _mock_http(monkeypatch, lambda req: (
        seen.update(body=json.loads(req.content))
        or httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})))

    async def go():
        thread = await db.create_thread("llm-custom", owner_profile_id=None)
        await db.add_message(thread.id, "user", "my name is Sam")
        await db.add_message(thread.id, "assistant", "Nice to meet you, Sam.")
        await db.add_message(thread.id, "user", "what is my name?")
        await llm_api.run_api_turn(thread.id, "llm-custom", "what is my name?")

    asyncio.run(go())
    turns = seen["body"]["messages"][1:]   # drop the system prompt
    assert turns == [
        {"role": "user", "content": "my name is Sam"},
        {"role": "assistant", "content": "Nice to meet you, Sam."},
        {"role": "user", "content": "what is my name?"},
    ]


def test_main_routes_an_api_bot_away_from_the_agent_cli(db_env, monkeypatch):
    """run_agent_turn is the single entry point; a bot with an `api` block must
    never reach openclaw.send_to_agent."""
    db = db_env
    _api_bot()

    async def explode(*a, **k):
        raise AssertionError("the agent CLI was called for an API bot")

    monkeypatch.setattr(main.openclaw, "send_to_agent", explode)
    _mock_http(monkeypatch, lambda req: httpx.Response(
        200, json={"choices": [{"message": {"content": "direct reply"}}]}))

    async def go():
        thread = await db.create_thread("llm-custom", owner_profile_id=None)
        await db.add_message(thread.id, "user", "hi")
        await main.run_agent_turn(thread.id, "llm-custom", "hi")
        msgs, _ = await db.list_messages(thread.id, 50)
        return msgs

    msgs = asyncio.run(go())
    assert [m.content for m in msgs if m.role == "assistant"] == ["direct reply"]


def test_reaction_markers_are_still_stripped_on_the_api_path(db_env, monkeypatch):
    """The reply goes through the SAME persist chokepoint as an agent's, so a
    `:react:` marker can never reach a chat bubble on this path either."""
    db = db_env
    _api_bot()
    _mock_http(monkeypatch, lambda req: httpx.Response(200, json={
        "choices": [{"message": {"content": "Sure thing. :react:nope:"}}]}))

    async def go():
        thread = await db.create_thread("llm-custom", owner_profile_id=None)
        await db.add_message(thread.id, "user", "hi")
        await llm_api.run_api_turn(thread.id, "llm-custom", "hi")
        msgs, _ = await db.list_messages(thread.id, 50)
        return msgs

    msgs = asyncio.run(go())
    reply = next(m for m in msgs if m.role == "assistant")
    assert ":react:" not in reply.content
    assert reply.content.startswith("Sure thing.")


# --------------------------------------------------------------------------- #
# config.yaml round-trip
# --------------------------------------------------------------------------- #


def test_other_writers_preserve_the_api_block(llm_env):
    """The recurring foot-gun in config.py: a writer that spells the field list
    out by hand drops whatever it forgot. `api` is the field whose loss costs
    the operator their key."""
    client = llm_env()
    client.post("/api/llm/connect", json={"backend": "legacy-api",
        "provider": "openai", "model": "gpt-4o", "api_key": "sk-precious"})
    # Reorder through the Bot Manager…
    config.save_bot_order([{"id": "llm-openai", "order": 0},
                           {"id": "alpha", "order": 1}])
    assert config.get_bot("llm-openai").api["api_key"] == "sk-precious"
    # …and set an avatar on an unrelated bot.
    config.save_bot_avatar("alpha", "new.png")
    assert config.get_bot("llm-openai").api["api_key"] == "sk-precious"
    assert config.get_bot("llm-openai").api["model"] == "gpt-4o"


def test_a_roster_with_no_api_bots_keeps_the_file_it_always_had(llm_env):
    """`api` is omitted rather than written as null, so an install that never
    touches this feature sees no change in config.yaml."""
    config.load_bots()
    assert "api:" not in config.CONFIG_PATH.read_text()


def test_the_default_bot_name_is_short_enough_for_a_sidebar(llm_env):
    """The pick-list label ("Custom (OpenAI-compatible)") is written for a
    <select>. Using it as the character's name wraps to three lines in the bot
    rail, so presets carry a separate short name."""
    client = llm_env()
    client.post("/api/llm/connect", json={"backend": "legacy-api", "provider": "custom", "model": "m",
                                          "base_url": "http://127.0.0.1:9/v1"})
    assert config.get_bot("llm-custom").name == "Assistant"
    client.post("/api/llm/connect", json={"backend": "legacy-api", "provider": "anthropic",
                                          "model": "claude-opus-5",
                                          "api_key": "sk-ant-x"})
    assert config.get_bot("llm-anthropic").name == "Claude"


# --------------------------------------------------------------------------- #
# Redirects must not carry the operator's credential to a new origin
# --------------------------------------------------------------------------- #


def _redirect_probe(monkeypatch, location: str):
    """Follow one redirect to ``location``; return every request seen."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(307, headers={"location": location})
        return httpx.Response(200, json={"ok": True})

    _mock_http(monkeypatch, handler)

    async def _go():
        async with llm_api.http_client() as client:
            return await llm_api._request(
                client, "POST", "https://api.example.com/v1/chat",
                headers={"Accept": "application/json",
                         "Authorization": "Bearer sk-secret",
                         "X-Api-Key": "sk-secret"},
                json_body={"hi": 1})

    status, _body = asyncio.run(_go())
    assert status == 200
    return seen


def test_redirect_to_another_host_drops_the_credential(monkeypatch):
    seen = _redirect_probe(monkeypatch, "https://evil.example.net/v1/chat")
    assert seen[0].headers.get("authorization") == "Bearer sk-secret"
    assert "authorization" not in seen[1].headers
    assert "x-api-key" not in seen[1].headers
    assert seen[1].headers.get("accept") == "application/json"


@pytest.mark.parametrize("location", [
    "http://api.example.com/v1/chat",       # scheme change
    "https://api.example.com:8443/v1/chat",  # port change
])
def test_redirect_across_scheme_or_port_drops_the_credential(monkeypatch, location):
    seen = _redirect_probe(monkeypatch, location)
    assert "authorization" not in seen[1].headers


def test_same_origin_redirect_keeps_the_credential(monkeypatch):
    seen = _redirect_probe(monkeypatch, "https://api.example.com/v2/chat")
    assert seen[1].headers.get("authorization") == "Bearer sk-secret"
    assert seen[1].headers.get("x-api-key") == "sk-secret"


# --------------------------------------------------------------------------- #
# Re-saving a bot must not erase what the panel never sees
# --------------------------------------------------------------------------- #

def test_resaving_a_bot_without_a_key_keeps_the_stored_one(db_env):
    """A saved key never comes back out of the API, so the panel re-submits an
    empty one. `upsert_bot` replaces the entry wholesale, so rebuilding the
    `api` block from the submission alone DELETED the key — editing a bot's
    name broke its ability to answer, and the only symptom was a 401 on the
    next turn."""
    first = llm_api.connect(llm_api.ConnectSpec(
        provider="openai", model="gpt-4o-mini", api_key="sk-stored",
        bot_id="llm-openai", name="First",
        system_prompt="be brief", max_history_chars=4000))
    assert first.api["api_key"] == "sk-stored"

    again = llm_api.connect(llm_api.ConnectSpec(
        provider="openai", model="gpt-4o-mini", api_key="",
        bot_id="llm-openai", name="Renamed"))

    assert again.name == "Renamed"
    assert again.api["api_key"] == "sk-stored"
    assert again.api["system_prompt"] == "be brief"
    assert again.api["max_history_chars"] == 4000
    assert config.get_bot("llm-openai").api["api_key"] == "sk-stored"


def test_a_new_key_still_replaces_the_stored_one(db_env):
    llm_api.connect(llm_api.ConnectSpec(
        provider="openai", model="gpt-4o-mini", api_key="sk-old",
        bot_id="llm-openai"))
    bot = llm_api.connect(llm_api.ConnectSpec(
        provider="openai", model="gpt-4o-mini", api_key="sk-new",
        bot_id="llm-openai"))
    assert bot.api["api_key"] == "sk-new"


def test_switching_provider_does_not_carry_the_old_key_across(db_env):
    llm_api.connect(llm_api.ConnectSpec(
        provider="openai", model="gpt-4o-mini", api_key="sk-openai",
        bot_id="llm-shared"))
    with pytest.raises(llm_api.ApiError):
        llm_api.connect(llm_api.ConnectSpec(
            provider="deepseek", model="deepseek-chat", api_key="",
            bot_id="llm-shared"))


# --------------------------------------------------------------------------- #
# Block-list content
# --------------------------------------------------------------------------- #

def test_block_list_content_is_read_not_crashed_on():
    """`.strip()` on a list raises AttributeError, which escapes the ApiError
    ladder and reaches the family as "Something went wrong." — with the reply
    sitting there intact."""
    msg = {"content": [{"type": "text", "text": "hello "},
                       {"type": "image_url", "image_url": {"url": "x"}},
                       {"type": "text", "text": "world"}]}
    assert llm_api._openai_message_text(msg) == "hello world"


def test_string_content_is_unchanged():
    assert llm_api._openai_message_text({"content": " hi "}) == "hi"
    assert llm_api._openai_message_text({"content": None}) == ""


# --------------------------------------------------------------------------- #
# The timeout an error quotes must be the one that expired
# --------------------------------------------------------------------------- #

def test_a_probe_timeout_quotes_the_probe_timeout():
    err = llm_api._transport_error(
        httpx.ReadTimeout("slow"), "http://x/v1/models", local=True,
        read_timeout=llm_api.PROBE_READ_TIMEOUT_S)
    assert f"{int(llm_api.PROBE_READ_TIMEOUT_S)}s" in err.detail
    assert f"{int(llm_api.READ_TIMEOUT_S)}s" not in err.detail


def test_a_turn_timeout_still_quotes_the_turn_timeout():
    err = llm_api._transport_error(
        httpx.ReadTimeout("slow"), "http://x/v1/chat/completions", local=True)
    assert f"{int(llm_api.READ_TIMEOUT_S)}s" in err.detail


def test_the_default_prompt_has_no_content_framing_whatever_the_safe_flag():
    """4.0: no Restricted tier, so no tier-dependent framing - the 3.x ``safe`` flag changes
    nothing about what a bot is told."""
    api = {"provider": "custom", "base_url": "http://127.0.0.1:9/v1", "model": "m"}
    safe = llm_api.resolve(config.Bot(id="s", name="S", safe=True, api=api))
    private = llm_api.resolve(config.Bot(id="p", name="S", safe=False, api=api))
    assert safe.system_prompt == private.system_prompt
    assert "family" not in safe.system_prompt.lower()
    # An operator's own prompt always wins, on either tier.
    own = llm_api.resolve(config.Bot(id="o", name="O", safe=True,
                                     api={**api, "system_prompt": "Be terse."}))
    assert own.system_prompt == "Be terse."


# --------------------------------------------------------------------------- #
# Repeated replies (T2 investigation: "an API bot only answered the first
# message on a scratch stub")
# --------------------------------------------------------------------------- #


def test_a_repeated_reply_in_a_later_turn_still_lands(db_env, monkeypatch):
    """Two turns, the provider says the same thing twice ("Sure." to two
    different requests a minute apart). Each turn is ONE delivery with no
    redundant transport behind it, so the content dedup that exists for the
    gateway's twin deliveries must not eat the second reply: it did (the
    300 s recent window), and the family saw a turn that never answered."""
    db = db_env
    _api_bot()
    _mock_http(monkeypatch, lambda req: httpx.Response(
        200, json={"choices": [{"message": {"content": "Sure, I can do that."}}]}))

    async def go():
        thread = await db.create_thread("llm-custom", owner_profile_id=None)
        for ask in ("turn the porch light on", "and the kitchen one"):
            await db.add_message(thread.id, "user", ask)
            main._forget_thread_delivery(thread.id)     # as run_agent_turn does
            await llm_api.run_api_turn(thread.id, "llm-custom", ask)
        msgs, _ = await db.list_messages(thread.id, 50)
        return msgs

    msgs = asyncio.run(go())
    assert [m.content for m in msgs if m.role == "assistant"] == [
        "Sure, I can do that.", "Sure, I can do that."]


# --------------------------------------------------------------------------- #
# Voice chat (C-§B.1/B.2): system slot, speculation + adoption, the cancel
# rule, heard truncation
# --------------------------------------------------------------------------- #

import time as _time  # noqa: E402
from types import SimpleNamespace as _NS  # noqa: E402

from voice_stub_llm import StubLLM  # noqa: E402

VOICE = _NS(system="## Spoken replies\nAt most two short sentences.",
            trailer="[voice chat] (app note …)")


def _voice_env(monkeypatch, db, frames=None, *, merge=True):
    """Real main.py delivery funnel; frames captured; optional merge hook."""
    frames = [] if frames is None else frames

    async def capture(frame):
        frames.append(frame)

    monkeypatch.setattr(main.manager, "broadcast", capture)
    hooks = llm_api._hooks
    assert hooks is not None
    monkeypatch.setattr(llm_api, "_hooks", llm_api.Hooks(
        list_messages=hooks.list_messages, deliver=hooks.deliver,
        set_status=hooks.set_status, broadcast=hooks.broadcast,
        thread_update=hooks.thread_update,
        merge_metadata=(lambda mid, patch: db.merge_message_metadata(mid, patch))
        if merge else None))
    return frames


def test_voice_section_goes_in_the_system_prompt_and_the_user_turn_stays_clean(
        db_env, monkeypatch):
    db = db_env
    _api_bot(system_prompt="You are Captain Hook, a pirate.")
    seen: dict = {}
    _mock_http(monkeypatch, lambda req: (
        seen.update(body=json.loads(req.content))
        or httpx.Response(200, json={"choices": [{"message": {"content": "Arr."}}]})))

    async def go():
        thread = await db.create_thread("llm-custom", owner_profile_id=None)
        await db.add_message(thread.id, "user", "what's the weather")
        await llm_api.run_api_turn(thread.id, "llm-custom", "what's the weather", voice=VOICE)

    asyncio.run(go())
    msgs = seen["body"]["messages"]
    assert msgs[0]["content"] == ("You are Captain Hook, a pirate.\n\n"
                                  "## Spoken replies\nAt most two short sentences.")
    assert msgs[-1] == {"role": "user", "content": "what's the weather"}
    assert "voice chat" not in json.dumps(msgs[1:])        # never the trailer


def test_no_voice_means_no_change_to_the_prompt(db_env, monkeypatch):
    db = db_env
    _api_bot(system_prompt="Be terse.")
    seen: dict = {}
    _mock_http(monkeypatch, lambda req: (
        seen.update(body=json.loads(req.content))
        or httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})))

    async def go():
        thread = await db.create_thread("llm-custom", owner_profile_id=None)
        await db.add_message(thread.id, "user", "hi")
        await llm_api.run_api_turn(thread.id, "llm-custom", "hi",
                                   voice=_NS(system="", trailer="x"))

    asyncio.run(go())
    assert seen["body"]["messages"][0]["content"] == "Be terse."


def _rows(*spec):
    return [MessageOut(id=f"m{i}", thread_id="t", role=r, content=c, created_at="x",
                       metadata=meta or {})
            for i, (r, c, meta) in enumerate(spec)]


def test_history_truncates_the_newest_interrupted_reply_to_what_was_heard():
    reply = "You have two things this afternoon: a dentist at three, and a school call."
    upto = reply.index(" and a school")
    heard = {"voice_heard": {"upto": upto, "interrupted": True,
                             "heard": reply[:upto], "unheard": reply[upto:]}}
    rows = _rows(("user", "what's on today", None), ("assistant", reply, heard),
                 ("user", "skip that, weather?", None))
    hist = llm_api.build_history(rows, truncate_heard=True)
    assert hist[1] == {"role": "assistant",
                       "content": "You have two things this afternoon: a dentist at three, —"}
    # Off by default (every non-voice caller), and the stored row is untouched.
    assert llm_api.build_history(rows)[1]["content"] == reply
    assert rows[1].content == reply


def test_history_truncation_is_for_the_next_prompt_only():
    reply = "First reply that was cut off half way through the second clause."
    heard = {"voice_heard": {"upto": 11, "interrupted": True}}
    rows = _rows(("user", "a", None), ("assistant", reply, heard),
                 ("user", "b", None), ("assistant", "A later reply.", None),
                 ("user", "c", None))
    assert llm_api.build_history(rows, truncate_heard=True)[1]["content"] == reply


@pytest.mark.parametrize("meta,expect", [
    ({"voice_heard": {"interrupted": True}}, None),          # no upto yet
    ({"voice_heard": {"upto": "12"}}, None),                 # wrong type
    ({"voice_heard": {"upto": True}}, None),                 # a bool is not an offset
    ({"voice_heard": {"upto": -1}}, None),
    ({"voice_heard": {"upto": 999}}, "full"),                # past the end: all heard
    ({"voice_heard": {"upto": 0}}, "—"),                     # nothing heard
])
def test_history_truncation_tolerates_odd_metadata(meta, expect):
    reply = "Some reply text."
    rows = _rows(("user", "a", None), ("assistant", reply, meta))
    got = llm_api.build_history(rows, truncate_heard=True)[1]["content"]
    assert got == (reply if expect in (None, "full") else expect)


def test_append_user_turn_never_replaces_an_earlier_message():
    hist = [{"role": "user", "content": "an unanswered question"}]
    assert llm_api.append_user_turn(hist, "the spoken one") == [
        {"role": "user", "content": "an unanswered question\n\nthe spoken one"}]
    assert llm_api.append_user_turn([], "x") == [{"role": "user", "content": "x"}]
    assert llm_api.append_user_turn(hist, "  ") == hist


def test_speculate_persists_and_broadcasts_nothing_then_adoption_reuses_the_call(
        db_env, monkeypatch):
    db = db_env
    _api_bot()
    calls: list[dict] = []
    _mock_http(monkeypatch, lambda req: (
        calls.append(json.loads(req.content))
        or httpx.Response(200, json={"choices": [{"message": {"content": "On it."}}]})))
    frames = _voice_env(monkeypatch, db)

    async def go():
        thread = await db.create_thread("llm-custom", owner_profile_id=None)
        await db.add_message(thread.id, "user", "earlier question")
        head = (await db.list_messages(thread.id, 5))[0][-1].id
        run = await llm_api.speculate(thread.id, "llm-custom", "lights on", voice=VOICE)
        await asyncio.wait({run._task}, timeout=2)
        snap = (list(frames), (await db.list_messages(thread.id, 50))[0],
                (await db.get_thread(thread.id)).status)
        # The real turn: the user's row lands, then the run is adopted.
        await db.add_message(thread.id, "user", "lights on")
        await llm_api.run_api_turn(thread.id, "llm-custom", "lights on", prefetched=run)
        msgs, _ = await db.list_messages(thread.id, 50)
        return run, head, snap, msgs

    run, head, (frames_before, rows_before, status_before), msgs = asyncio.run(go())
    assert run.head_id == head and run.done and run.text == "On it."
    assert run.prompt_chars > len("lights on")
    assert frames_before == [] and len(rows_before) == 1 and status_before != "thinking"
    # One provider call in total; its prompt had the voice section and the
    # spoken text as a NEW user turn after the unanswered one.
    assert len(calls) == 1
    assert calls[0]["messages"][0]["content"].endswith("At most two short sentences.")
    assert calls[0]["messages"][-1] == {
        "role": "user", "content": "earlier question\n\nlights on"}
    assert [m.content for m in msgs if m.role == "assistant"] == ["On it."]


def test_a_foreign_or_dead_run_is_discarded_and_the_turn_calls_afresh(db_env, monkeypatch):
    db = db_env
    _api_bot()
    calls: list[int] = []
    _mock_http(monkeypatch, lambda req: (
        calls.append(1)
        or httpx.Response(200, json={"choices": [{"message": {"content": f"r{len(calls)}"}}]})))
    _voice_env(monkeypatch, db)

    async def go():
        t1 = await db.create_thread("llm-custom", owner_profile_id=None)
        t2 = await db.create_thread("llm-custom", owner_profile_id=None)
        await db.add_message(t1.id, "user", "x")
        await db.add_message(t2.id, "user", "y")
        foreign = await llm_api.speculate(t1.id, "llm-custom", "x")
        await asyncio.wait({foreign._task}, timeout=2)
        await llm_api.run_api_turn(t2.id, "llm-custom", "y", prefetched=foreign)
        other = _NS(discarded=False)

        async def discard():
            other.discarded = True
        other.discard = discard
        await llm_api.run_api_turn(t2.id, "llm-custom", "y", prefetched=other)
        return foreign, other, (await db.list_messages(t2.id, 50))[0]

    foreign, other, msgs = asyncio.run(go())
    assert foreign.discarded and other.discarded
    assert len(calls) == 3                       # the speculation + two fresh calls
    assert [m.content for m in msgs if m.role == "assistant"] == ["r2", "r3"]


def test_a_failed_speculation_is_not_adopted(db_env, monkeypatch):
    db = db_env
    _api_bot()
    answers = iter([httpx.Response(500, text="boom"),
                    httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})])
    _mock_http(monkeypatch, lambda req: next(answers))
    _voice_env(monkeypatch, db)

    async def go():
        thread = await db.create_thread("llm-custom", owner_profile_id=None)
        await db.add_message(thread.id, "user", "x")
        run = await llm_api.speculate(thread.id, "llm-custom", "x")
        await asyncio.wait({run._task}, timeout=2)
        assert run.error is not None and not run.usable()
        await llm_api.run_api_turn(thread.id, "llm-custom", "x", prefetched=run)
        return (await db.list_messages(thread.id, 50))[0], await db.get_thread(thread.id)

    msgs, thread = asyncio.run(go())
    assert [m.content for m in msgs if m.role == "assistant"] == ["ok"]
    assert thread.status == "idle"


def test_discard_closes_the_provider_call_within_a_second(db_env, monkeypatch):
    db = db_env
    with StubLLM(delay_s=1.5) as stub:
        _api_bot(base_url=stub.base_url)
        _voice_env(monkeypatch, db)

        async def go():
            thread = await db.create_thread("llm-custom", owner_profile_id=None)
            await db.add_message(thread.id, "user", "x")
            run = await llm_api.speculate(thread.id, "llm-custom", "x")
            for _ in range(100):
                if stub.chat_requests():
                    break
                await asyncio.sleep(0.01)
            t0 = _time.monotonic()
            await run.discard()
            took = _time.monotonic() - t0
            # Checked INSIDE the loop: asyncio.run() cancels leftover tasks
            # on exit, which would make a discard that never closed anything
            # look closed afterwards.
            cancelled = run._task.cancelled()
            await run.discard()                      # idempotent
            # The server notices the hang-up when it tries to answer (1.5 s).
            deadline = _time.monotonic() + 3
            while not stub.chat_requests()[0]["aborted"] and _time.monotonic() < deadline:
                await asyncio.sleep(0.05)
            return run, took, cancelled, (await db.list_messages(thread.id, 50))[0]

        run, took, cancelled, msgs = asyncio.run(go())
        assert took < 1.0 and cancelled
        assert not run.usable() and run.text == ""
        assert [m for m in msgs if m.role == "assistant"] == []
        assert stub.chat_requests()[0]["aborted"], "the server saw the hang-up"


def test_an_unadopted_run_expires(db_env, monkeypatch):
    db = db_env
    monkeypatch.setattr(llm_api, "SPECULATION_TTL_S", 0.2)
    with StubLLM(delay_s=3.0) as stub:
        _api_bot(base_url=stub.base_url)
        _voice_env(monkeypatch, db)

        async def go():
            thread = await db.create_thread("llm-custom", owner_profile_id=None)
            run = await llm_api.speculate(thread.id, "llm-custom", "x")
            await asyncio.sleep(0.6)
            return run, run._task.cancelled()

        run, cancelled = asyncio.run(go())
        assert run.discarded and cancelled


def _long_reply() -> str:
    return " ".join(f"Sentence number {i} of a long spoken reply." for i in range(12))


def test_cancel_keep_partial_mid_delivery_reports_the_row(db_env, monkeypatch):
    """An API reply is persisted whole and then simulated-streamed; an
    interruption during that stream finishes the delivery and returns the row,
    stamped `voice_heard {interrupted: true}` (F.6)."""
    db = db_env
    _api_bot()
    reply = _long_reply()
    _mock_http(monkeypatch, lambda req: httpx.Response(
        200, json={"choices": [{"message": {"content": reply}}]}))
    frames = _voice_env(monkeypatch, db)

    async def go():
        thread = await db.create_thread("llm-custom", owner_profile_id=None)
        await db.add_message(thread.id, "user", "talk")
        task = asyncio.create_task(llm_api.run_api_turn(thread.id, "llm-custom", "talk"))
        for _ in range(300):
            if any(f["type"] == "stream_chunk" for f in frames):
                break
            await asyncio.sleep(0.01)
        row_id = await llm_api.cancel_direct_turn(task, thread.id, keep_partial=True)
        msgs, _ = await db.list_messages(thread.id, 50)
        return row_id, msgs, task, await db.get_thread(thread.id)

    row_id, msgs, task, thread = asyncio.run(go())
    assistant = [m for m in msgs if m.role == "assistant"]
    assert len(assistant) == 1 and assistant[0].id == row_id
    assert assistant[0].content == reply
    assert assistant[0].metadata["voice_heard"] == {"interrupted": True}
    assert task.cancelled() and thread.status == "idle"
    assert any(f["type"] == "stream_done" and f["message_id"] == row_id for f in frames)
    assert llm_api._cancel_policy == {}


@pytest.mark.parametrize("keep", [True, False])
def test_cancel_before_the_reply_exists_persists_nothing(db_env, monkeypatch, keep):
    db = db_env
    with StubLLM(delay_s=3.0) as stub:
        _api_bot(base_url=stub.base_url)
        _voice_env(monkeypatch, db)

        async def go():
            thread = await db.create_thread("llm-custom", owner_profile_id=None)
            await db.add_message(thread.id, "user", "x")
            task = asyncio.create_task(llm_api.run_api_turn(thread.id, "llm-custom", "x"))
            for _ in range(200):
                if stub.chat_requests():
                    break
                await asyncio.sleep(0.01)
            row_id = await llm_api.cancel_direct_turn(task, thread.id, keep_partial=keep)
            return row_id, (await db.list_messages(thread.id, 50))[0], \
                await db.get_thread(thread.id)

        row_id, msgs, thread = asyncio.run(go())
        assert row_id is None
        assert [m for m in msgs if m.role == "assistant"] == []
        assert thread.status == "idle"


def test_cancel_of_a_finished_turn_is_a_no_op(db_env, monkeypatch):
    db = db_env
    _api_bot()
    _mock_http(monkeypatch, lambda req: httpx.Response(
        200, json={"choices": [{"message": {"content": "done"}}]}))
    _voice_env(monkeypatch, db)

    async def go():
        thread = await db.create_thread("llm-custom", owner_profile_id=None)
        await db.add_message(thread.id, "user", "x")
        task = asyncio.create_task(llm_api.run_api_turn(thread.id, "llm-custom", "x"))
        await task
        return await llm_api.cancel_direct_turn(task, thread.id, keep_partial=True)

    assert asyncio.run(go()) is None


def test_an_api_turn_after_an_interruption_sees_only_what_was_heard(db_env, monkeypatch):
    db = db_env
    _api_bot()
    seen: dict = {}
    _mock_http(monkeypatch, lambda req: (
        seen.update(body=json.loads(req.content))
        or httpx.Response(200, json={"choices": [{"message": {"content": "Sunny."}}]})))
    reply = "You have two things this afternoon. The dentist is at three, and the school call."
    upto = reply.index(" The dentist")

    async def go():
        thread = await db.create_thread("llm-custom", owner_profile_id=None)
        await db.add_message(thread.id, "user", "what's on")
        await db.add_message(thread.id, "assistant", reply, metadata={"voice_heard": {
            "upto": upto, "interrupted": True, "heard": reply[:upto],
            "unheard": reply[upto:]}})
        await db.add_message(thread.id, "user", "weather?")
        await llm_api.run_api_turn(thread.id, "llm-custom", "weather?", voice=VOICE)

    asyncio.run(go())
    turns = seen["body"]["messages"][1:]
    assert turns[1] == {"role": "assistant", "content": "You have two things this afternoon. —"}


# -- review T2 fixes: repeated aborts, merge after landing, the stamp frame -- #


def _wait_for(frames, kind, n=300):
    async def go():
        for _ in range(n):
            if any(f["type"] == kind for f in frames):
                return True
            await asyncio.sleep(0.01)
        return False
    return go()


@pytest.mark.parametrize("second_keep", [True, False])
def test_a_second_cancel_waits_for_the_first_instead_of_cancelling_again(
        db_env, monkeypatch, second_keep):
    """Barge-in then hang-up a moment later (review T2 #1): the second call must
    not cancel the turn again (that landed inside its cleanup and stranded the
    thread in 'thinking'); it waits for the same outcome, keeps the first
    call's flag, and the turn task is cancelled exactly once."""
    db = db_env
    _api_bot()
    reply = _long_reply()
    _mock_http(monkeypatch, lambda req: httpx.Response(
        200, json={"choices": [{"message": {"content": reply}}]}))
    frames = _voice_env(monkeypatch, db)

    async def go():
        thread = await db.create_thread("llm-custom", owner_profile_id=None)
        await db.add_message(thread.id, "user", "talk")
        task = asyncio.create_task(llm_api.run_api_turn(thread.id, "llm-custom", "talk"))
        assert await _wait_for(frames, "stream_chunk")
        cancels = 0
        real_cancel = task.cancel

        def counting_cancel(*a, **kw):
            nonlocal cancels
            cancels += 1
            return real_cancel(*a, **kw)

        task.cancel = counting_cancel        # type: ignore[method-assign]
        first = asyncio.create_task(
            llm_api.cancel_direct_turn(task, thread.id, keep_partial=True))
        await asyncio.sleep(0.005)
        second = asyncio.create_task(
            llm_api.cancel_direct_turn(task, thread.id, keep_partial=second_keep))
        r1, r2 = await asyncio.gather(first, second)
        msgs, _ = await db.list_messages(thread.id, 50)
        return r1, r2, cancels, msgs, task, await db.get_thread(thread.id)

    r1, r2, cancels, msgs, task, thread = asyncio.run(go())
    assistant = [m for m in msgs if m.role == "assistant"]
    assert cancels == 1
    assert len(assistant) == 1 and r1 == assistant[0].id
    assert r2 == r1                      # the first call's keep_partial holds
    assert assistant[0].metadata["voice_heard"] == {"interrupted": True}
    assert task.cancelled() and thread.status == "idle"
    assert [f["status"] for f in frames if f["type"] == "thinking"][-1] == "stopped"
    assert len([f for f in frames if f["type"] == "stream_done"]) == 1
    assert llm_api._cancel_policy == {}


def test_the_interrupted_stamp_reaches_every_screen(db_env, monkeypatch):
    """review T2 #4: the `voice_heard {interrupted}` stamp on a landed API row
    goes out as a message_update, not only into the database."""
    db = db_env
    _api_bot()
    reply = _long_reply()
    _mock_http(monkeypatch, lambda req: httpx.Response(
        200, json={"choices": [{"message": {"content": reply}}]}))
    frames = _voice_env(monkeypatch, db)

    async def go():
        thread = await db.create_thread("llm-custom", owner_profile_id=None)
        await db.add_message(thread.id, "user", "talk")
        task = asyncio.create_task(llm_api.run_api_turn(thread.id, "llm-custom", "talk"))
        assert await _wait_for(frames, "stream_chunk")
        return await llm_api.cancel_direct_turn(task, thread.id, keep_partial=True)

    row_id = asyncio.run(go())
    updates = [f for f in frames if f["type"] == "message_update"]
    assert len(updates) == 1 and updates[0]["message_id"] == row_id
    assert updates[0]["bot_id"] == "llm-custom"
    assert updates[0]["message"]["metadata"]["voice_heard"] == {"interrupted": True}


def test_a_merge_waits_for_a_reply_that_was_already_landing(db_env, monkeypatch):
    """Amendment T2-4: keep_partial=False cannot un-write a reply whose
    delivery had begun, so the turn waits for it — it is in the thread, with
    no interrupted stamp, before abort returns (and a retraction of its
    question then finds it). None is still the answer."""
    db = db_env
    _api_bot()
    reply = _long_reply()
    _mock_http(monkeypatch, lambda req: httpx.Response(
        200, json={"choices": [{"message": {"content": reply}}]}))
    frames = _voice_env(monkeypatch, db)

    async def go():
        thread = await db.create_thread("llm-custom", owner_profile_id=None)
        await db.add_message(thread.id, "user", "talk")
        task = asyncio.create_task(llm_api.run_api_turn(thread.id, "llm-custom", "talk"))
        assert await _wait_for(frames, "stream_chunk")
        got = await llm_api.cancel_direct_turn(task, thread.id, keep_partial=False)
        done_at_return = any(f["type"] == "stream_done" for f in frames)
        msgs, _ = await db.list_messages(thread.id, 50)
        return got, done_at_return, msgs, task, await db.get_thread(thread.id)

    got, done_at_return, msgs, task, thread = asyncio.run(go())
    assistant = [m for m in msgs if m.role == "assistant"]
    assert got is None
    assert done_at_return                                # landed before the return
    assert len(assistant) == 1 and "voice_heard" not in (assistant[0].metadata or {})
    assert not [f for f in frames if f["type"] == "message_update"]
    assert task.cancelled() and thread.status == "idle"


def _ext_env(monkeypatch, db, frames):
    async def capture(frame):
        frames.append(frame)
    monkeypatch.setattr(main.manager, "broadcast", capture)
    main._ext_turns.clear()
    main._speculations.clear()


@pytest.mark.parametrize("phase", ["landing", "landed"])
def test_retracting_a_merged_question_takes_its_api_answer_with_it(
        db_env, monkeypatch, phase):
    """review T2 #3 end to end through the extension seam: the voice session's
    merge (abort keep_partial=False, then retract the first user row) leaves
    no assistant row answering the deleted question — whether the reply was
    still being simulated-streamed or had fully landed and the turn ended."""
    db = db_env
    _api_bot()
    reply = _long_reply() if phase == "landing" else "Short answer."
    _mock_http(monkeypatch, lambda req: httpx.Response(
        200, json={"choices": [{"message": {"content": reply}}]}))
    frames: list[dict] = []
    _ext_env(monkeypatch, db, frames)

    async def go():
        thread = await db.create_thread("llm-custom", owner_profile_id=None)
        uid = await main._ext_submit_turn("voice", thread.id, "what's on")
        if phase == "landing":
            assert await _wait_for(frames, "stream_chunk")
        else:
            for _ in range(300):
                if (await db.get_thread(thread.id)).status == "idle" and any(
                        f["type"] in ("message", "stream_done")
                        and (f.get("message") or {}).get("role") == "assistant"
                        for f in frames):
                    break
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.05)
        assert await main._ext_abort_turn(thread.id, keep_partial=False) is None
        await main._ext_retract(thread.id, uid)
        await asyncio.sleep(0.1)
        msgs, _ = await db.list_messages(thread.id, 50)
        return uid, msgs, await db.get_thread(thread.id)

    uid, msgs, thread = asyncio.run(go())
    assert msgs == []
    deleted = {f["message_id"] for f in frames if f["type"] == "message_deleted"}
    assert uid in deleted and len(deleted) == 2
    assert thread.status == "idle"


def test_retraction_spares_rows_that_are_not_the_direct_answer(db_env, monkeypatch):
    """Only the direct backend's own reply rows (api:/advisor:<uuid>, the
    advisor's keep-partial row) go with a retracted question: a request card,
    a gateway row, a source-less row and anything after the next user row
    stay."""
    db = db_env
    _api_bot()
    frames: list[dict] = []
    _ext_env(monkeypatch, db, frames)

    async def go():
        thread = await db.create_thread("llm-custom", owner_profile_id=None)
        tid = thread.id
        q = await db.add_message(tid, "user", "first", metadata={"voice": True})
        gone_a = await db.add_message(tid, "assistant", "a", source_id=llm_api.reply_source_id("api"))
        gone_b = await db.add_message(tid, "assistant", "b",
                                      source_id=llm_api.reply_source_id("advisor"))
        gone_c = await db.add_message(tid, "assistant", "c",
                                      source_id="advisor:partial:run:advisor-abc123")
        card = await db.add_message(tid, "assistant", "card",
                                    source_id="advisor:3f2a9c:card")
        gw = await db.add_message(tid, "assistant", "gw", source_id="gw:agent:main:x:1")
        plain = await db.add_message(tid, "assistant", "plain")
        await db.add_message(tid, "user", "second")
        later = await db.add_message(tid, "assistant", "later",
                                     source_id=llm_api.reply_source_id("api"))
        await main._ext_retract(tid, q.id)
        left = [m.id for m in (await db.list_messages(tid, 50))[0]]
        return left, {gone_a.id, gone_b.id, gone_c.id, q.id}, [card.id, gw.id, plain.id,
                                                              later.id]

    left, gone, kept = asyncio.run(go())
    assert not gone & set(left)
    assert all(k in left for k in kept)
    assert {f["message_id"] for f in frames if f["type"] == "message_deleted"} == gone


def test_retract_of_a_typed_row_deletes_nothing(db_env, monkeypatch):
    db = db_env
    _api_bot()
    frames: list[dict] = []
    _ext_env(monkeypatch, db, frames)

    async def go():
        thread = await db.create_thread("llm-custom", owner_profile_id=None)
        q = await db.add_message(thread.id, "user", "typed")
        await db.add_message(thread.id, "assistant", "a",
                             source_id=llm_api.reply_source_id("api"))
        await main._ext_retract(thread.id, q.id)
        return len((await db.list_messages(thread.id, 50))[0])

    assert asyncio.run(go()) == 2
    assert frames == []


def test_a_speculation_is_tracked_and_its_failure_is_not_a_dead_task(
        db_env, monkeypatch, caplog):
    """review T2 #6: the provider call joins the app's task set (shutdown
    order) through a shepherd, and a failed call — the adopting turn's to
    report — is never logged as a background task dying."""
    db = db_env
    _api_bot()
    _mock_http(monkeypatch, lambda req: httpx.Response(500, json={"error": "boom"}))
    tracked: list[asyncio.Task] = []
    _voice_env(monkeypatch, db)
    monkeypatch.setattr(llm_api, "_hooks", dataclasses.replace(
        llm_api._hooks, track=lambda t: (tracked.append(t), main._track(t))))

    async def go():
        thread = await db.create_thread("llm-custom", owner_profile_id=None)
        run = await llm_api.speculate(thread.id, "llm-custom", "hi")
        await asyncio.wait({run._task})
        await asyncio.wait(set(tracked), timeout=1)
        return run

    with caplog.at_level("ERROR"):
        run = asyncio.run(go())
    assert len(tracked) == 1 and tracked[0].done() and not tracked[0].cancelled()
    assert run.error is not None and not run.usable()
    assert "died" not in caplog.text


def test_shutdown_cancels_a_tracked_speculation_in_order(db_env, monkeypatch):
    db = db_env
    tracked: list[asyncio.Task] = []
    with StubLLM(delay_s=3.0) as stub:
        _api_bot(base_url=stub.base_url)
        _voice_env(monkeypatch, db)
        monkeypatch.setattr(llm_api, "_hooks", dataclasses.replace(
            llm_api._hooks, track=tracked.append))

        async def go():
            thread = await db.create_thread("llm-custom", owner_profile_id=None)
            run = await llm_api.speculate(thread.id, "llm-custom", "hi")
            for _ in range(200):
                if stub.chat_requests():
                    break
                await asyncio.sleep(0.01)
            tracked[0].cancel()                  # what shutdown does to _background
            await asyncio.wait(set(tracked), timeout=2)
            return run

        run = asyncio.run(go())
        assert run._task.cancelled() and not run.usable()


def test_an_adopting_turn_leaves_feedback_notes_pending(db_env, monkeypatch):
    """review T2 #5: the adopted prompt never carried the notes, so they must
    not be marked delivered by the turn that adopts it."""
    db = db_env
    _api_bot()

    async def go():
        thread = await db.create_thread("llm-custom", owner_profile_id=None)
        rated = await db.add_message(thread.id, "assistant", "An earlier answer.",
                                     metadata={"feedback": {"vote": "down",
                                                            "pending": True}})
        adopted = await main._compose_agent_text(
            thread.id, "hi", main.TurnOptions(prefetched=object()), gateway=False)
        still = (await db.get_message(rated.id)).metadata["feedback"]["pending"]
        fresh = await main._compose_agent_text(
            thread.id, "hi", main.TurnOptions(), gateway=False)
        after = (await db.get_message(rated.id)).metadata["feedback"]["pending"]
        return adopted, still, fresh, after

    adopted, still, fresh, after = asyncio.run(go())
    assert adopted == "hi" and still is True
    assert "[[feedback]]" in fresh and after is False
