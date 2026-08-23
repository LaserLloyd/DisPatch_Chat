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
    ("POST", "/api/llm/connect", {"provider": "lmstudio", "model": "x"}),
]


@pytest.mark.parametrize("method,path,body", LLM_ROUTES)
def test_sessionless_caller_is_refused_when_a_pin_is_set(llm_env, method, path, body):
    client = llm_env()
    _unlock(client)                       # sets a PIN and hands us a session
    client.cookies.clear()                # …and now we are Safe Mode
    r = client.request(method, path, json=body)
    assert r.status_code == 403
    assert "unlock" in r.text.lower()


@pytest.mark.parametrize("method,path,body", LLM_ROUTES)
def test_decoy_is_blocked_by_the_middleware_too(llm_env, method, path, body):
    """Belt-and-braces: the prefix is in _decoy_blocked, so a limited session is
    turned away one layer before the route's own dependency runs."""
    assert main._decoy_blocked(method, path) is True
    client = llm_env()
    _unlock(client)
    client.cookies.clear()
    assert client.request(method, path, json=body).status_code == 403


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
    r = client.post("/api/llm/connect", json={
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
    client.post("/api/llm/connect", json={
        "provider": "openai", "model": "gpt-4o-mini", "api_key": "sk-one"})
    r = client.post("/api/llm/connect", json={
        "provider": "openai", "model": "gpt-4o", "api_key": "sk-two"})
    assert r.status_code == 200
    assert len(config.load_bots()) == before + 1, "re-running the panel adds no duplicate"
    assert config.get_bot("llm-openai").api["model"] == "gpt-4o"
    assert config.get_bot("llm-openai").api["api_key"] == "sk-two"


def test_reconnecting_without_a_key_keeps_the_stored_one(llm_env):
    """Changing the model must not require re-pasting the credential — the
    panel sends a blank key field when the operator leaves it alone."""
    client = llm_env()
    client.post("/api/llm/connect", json={
        "provider": "openai", "model": "gpt-4o-mini", "api_key": "sk-keep"})
    client.post("/api/llm/connect", json={"provider": "openai", "model": "gpt-4o"})
    assert config.get_bot("llm-openai").api["api_key"] == "sk-keep"


def test_connect_refuses_to_hijack_an_existing_agent_bot(llm_env):
    client = llm_env()
    r = client.post("/api/llm/connect", json={
        "provider": "openai", "model": "gpt-4o-mini", "api_key": "sk-x",
        "bot_id": "alpha"})
    assert r.status_code == 400
    assert "not an API bot" in r.json()["detail"]
    assert config.get_bot("alpha").api is None


def test_connect_requires_a_key_for_a_provider_that_needs_one(llm_env):
    client = llm_env()
    r = client.post("/api/llm/connect", json={"provider": "openai", "model": "gpt-4o"})
    assert r.status_code == 400
    assert "key" in r.json()["detail"].lower()
    assert config.get_bot("llm-openai") is None


def test_connect_accepts_an_env_var_instead_of_a_stored_key(llm_env, monkeypatch):
    monkeypatch.setenv("MY_TEST_KEY", "sk-from-env")
    client = llm_env()
    r = client.post("/api/llm/connect", json={
        "provider": "openai", "model": "gpt-4o", "api_key_env": "MY_TEST_KEY"})
    assert r.status_code == 200
    stored = config.get_bot("llm-openai").api
    assert stored["api_key_env"] == "MY_TEST_KEY"
    assert "api_key" not in stored, "no secret was written to disk at all"
    assert r.json()["bot"]["api"]["has_key"] is False


def test_local_provider_needs_no_key(llm_env):
    client = llm_env()
    r = client.post("/api/llm/connect", json={"provider": "lmstudio", "model": "any"})
    assert r.status_code == 200
    assert config.get_bot("llm-lmstudio").api["base_url"] == "http://127.0.0.1:1234/v1"


def test_public_bot_list_exposes_the_provider_but_no_url_or_key(llm_env):
    client = llm_env()
    client.post("/api/llm/connect", json={
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
    client.post("/api/llm/connect", json={
        "provider": "openai", "model": "gpt-4o", "api_key": "sk-secret"})
    mode = stat.S_IMODE(config.CONFIG_PATH.stat().st_mode)
    assert mode == 0o600, f"config.yaml is {oct(mode)} but now holds credentials"


def test_auth_status_reports_the_api_bot_count(llm_env):
    client = llm_env()
    assert client.get("/api/auth/status").json()["features"]["api_bots"] == 0
    client.post("/api/llm/connect", json={"provider": "lmstudio", "model": "m"})
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
        thread = await db.create_thread("llm-custom")
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
        thread = await db.create_thread("llm-custom")
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
        thread = await db.create_thread("llm-custom")
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
        thread = await db.create_thread("llm-custom")
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
        thread = await db.create_thread("llm-custom")
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
        thread = await db.create_thread("llm-custom")
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
        thread = await db.create_thread("llm-broken")
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
        thread = await db.create_thread("llm-custom")
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
        thread = await db.create_thread("llm-custom")
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
        thread = await db.create_thread("llm-custom")
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
    client.post("/api/llm/connect", json={
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
    client.post("/api/llm/connect", json={"provider": "custom", "model": "m",
                                          "base_url": "http://127.0.0.1:9/v1"})
    assert config.get_bot("llm-custom").name == "Assistant"
    client.post("/api/llm/connect", json={"provider": "anthropic",
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
