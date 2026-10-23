"""Connections (3.0, W5): one record per model server.

Create/update/delete without ever returning a key, the credential reference (secret store or an
environment variable), the SSRF guard, an actionable Test (reachable? key accepted? models?
latency?), StudioForge auto-detection through ``GET /api/version``, discovery, the adoption of the
2.x inline provider blocks (zero breakage: the same key still resolves) and the permissions.
"""
from __future__ import annotations

import socket

import httpx
import pytest
import yaml

from app import config, connections, llm_api, secret_store
from app.connections import ConnectionError_


@pytest.fixture
def owner(world):
    return world.create_owner()


def _mock_http(monkeypatch, handler):
    def factory(read_timeout: float = llm_api.READ_TIMEOUT_S) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=read_timeout,
                                 follow_redirects=False)
    monkeypatch.setattr(llm_api, "http_client", factory)


def _openai_server(models=("m1", "m2"), key="sk-live-1234567890"):
    """A fake OpenAI-compatible server that wants `key` (or none when key is None)."""
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((req.method, req.url.path, req.headers.get("authorization")))
        if req.url.path.endswith("/api/version"):
            return httpx.Response(404, json={"error": "nope"})
        if req.url.path.endswith("/models"):
            if key and req.headers.get("authorization") != f"Bearer {key}":
                return httpx.Response(401, json={"error": {"message": "bad key"}})
            return httpx.Response(200, json={"data": [{"id": m} for m in models]})
        return httpx.Response(404)
    handler.seen = seen                                      # type: ignore[attr-defined]
    return handler


# --------------------------------------------------------------------------- #
# Types and manifests
# --------------------------------------------------------------------------- #


def test_the_thirteen_types_exist_and_fit_the_connection_manifest():
    assert set(connections.TYPES) == {
        "openai_compatible", "studioforge", "lmstudio", "ollama", "openai", "anthropic", "deepseek",
        "xai", "minimax", "openrouter", "groq", "mistral", "together"}
    for t in connections.TYPES.values():
        m = t.manifest()                                    # validates against the frozen schema
        assert m.kind == "llm" and m.requires_permission == "manage_connections"
        assert t.provider in llm_api.PROVIDER_BY_ID, f"{t.id} maps to an unknown llm_api provider"
    assert connections.TYPES["studioforge"].manifest().discover == ["http://127.0.0.1:1234/api/version"]


# --------------------------------------------------------------------------- #
# Storage, credentials
# --------------------------------------------------------------------------- #


def test_create_stores_the_key_in_the_secret_store_only(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    c = connections.create(type="openai", api_key="sk-live-1234567890")  # scrub-ok: test fixture, not a real credential
    assert c.id == "openai" and c.credential_ref == "secret:connection.openai"
    assert secret_store.get("connection.openai") == "sk-live-1234567890"
    assert connections.credential(c) == "sk-live-1234567890"
    import sqlite3
    db = sqlite3.connect(tmp_path / "chats.db")
    assert "sk-live" not in repr(db.execute("SELECT * FROM connections").fetchall())
    db.close()
    assert "sk-live" not in repr(c.public())
    assert c.public()["credential"] == {"kind": "secret", "env": "", "has_key": True}
    again = connections.create(type="openai", api_key="sk-other-1234567890")  # scrub-ok: test fixture, not a real credential
    assert again.id == "openai-2"


def test_an_environment_reference_reads_the_variable_at_use_time(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setenv("HOUSE_OPENAI_KEY", "sk-env-abcdefghij")
    c = connections.create(type="openai", api_key_env="HOUSE_OPENAI_KEY")
    assert c.credential_ref == "env:HOUSE_OPENAI_KEY" and connections.credential(c) == "sk-env-abcdefghij"
    monkeypatch.setenv("HOUSE_OPENAI_KEY", "sk-rotated-abcdefghij")
    assert connections.credential(c) == "sk-rotated-abcdefghij"
    # DisPatch's own variables and non-credential names are refused (a stranger must not read PATH)
    for bad in ("PATH", "DISPATCH_PORT", "HOME_DIR", "x"):
        with pytest.raises(ConnectionError_):
            connections.create(type="openai_compatible", base_url="http://127.0.0.1:9/v1", api_key_env=bad)


def test_a_hosted_type_needs_a_key_and_a_local_one_does_not(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    with pytest.raises(ConnectionError_) as e:
        connections.create(type="anthropic")
    assert e.value.code == "connection_invalid"
    assert connections.create(type="lmstudio").base_url == "http://127.0.0.1:1234/v1"
    with pytest.raises(ConnectionError_):
        connections.create(type="openai_compatible")                    # needs its address
    with pytest.raises(ConnectionError_):
        connections.create(type="nope")


def test_update_replaces_or_clears_the_key_and_resets_the_model_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    c = connections.create(type="studioforge", base_url="http://127.0.0.1:1234/v1", api_key="sf-key-1234567")  # scrub-ok: test fixture, not a real credential
    c.models, c.default_model = ["a"], "a"
    connections._save(c)
    c = connections.update(c.id, {"api_key": "sf-key-new-12345"})
    assert connections.credential(c) == "sf-key-new-12345" and c.models == ["a"]
    c = connections.update(c.id, {"base_url": "http://203.0.113.5:1234/v1"})
    assert c.models == [] and c.base_url == "http://203.0.113.5:1234/v1"      # new server, stale list dropped
    c = connections.update(c.id, {"clear_key": True})
    assert c.credential_ref == "" and secret_store.get("connection.studioforge") is None
    with pytest.raises(ConnectionError_) as e:
        connections.update("ghost", {"name": "x"})
    assert e.value.status == 404


def test_a_connection_in_use_cannot_be_deleted(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    c = connections.create(type="lmstudio")
    config.upsert_bot(config.Bot(id="scribe", name="Scribe", advisor={
        "providers": [{"provider": "lmstudio", "model": "m", "connection": c.id}]}))
    with pytest.raises(ConnectionError_) as e:
        connections.delete(c.id)
    assert e.value.code == "connection_in_use" and "scribe" in str(e.value)
    config.remove_bot("scribe") if len(config.load_bots()) > 1 else None
    config.update_bot("scribe", lambda b: __import__("dataclasses").replace(
        b, advisor={"providers": [{"provider": "lmstudio", "model": "m"}]}))
    connections.delete(c.id)
    assert connections.get(c.id) is None


# --------------------------------------------------------------------------- #
# The SSRF guard
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("url", [
    "http://169.254.169.254/latest/meta-data", "http://169.254.0.7:1234/v1", "http://[fe80::1]:1234/v1",
    "http://metadata.google.internal/computeMetadata/v1", "http://[::ffff:169.254.169.254]/", "http://0.0.0.0:1234/v1",
    "http://100.100.100.200/",  # scrub-ok: a cloud metadata address the guard must refuse
])
def test_link_local_and_metadata_targets_are_refused(url, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    with pytest.raises(ConnectionError_) as e:
        connections.check_target(url)
    assert e.value.code == "connection_blocked_target" and e.value.status == 403


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:1234/v1", "http://localhost:11434/v1", "http://192.0.2.20:1234/v1",
    "http://198.51.100.5:8080", "https://api.openai.com/v1", "http://[::1]:1234/v1",
])
def test_loopback_and_lan_targets_are_allowed(url, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, **k: [(0, 0, 0, "", ("93.184.216.34", 0))])
    assert connections.check_target(url) == url


def test_a_name_that_resolves_to_the_metadata_address_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    connections._resolved_hosts.clear()
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, **k: [(0, 0, 0, "", ("169.254.169.254", 0))])
    with pytest.raises(ConnectionError_) as e:
        connections.check_target("http://innocent.example.net/v1")
    assert e.value.code == "connection_blocked_target" and "169.254.169.254" in str(e.value)


def test_the_admin_can_allow_private_targets(tmp_path, monkeypatch):
    from app import settings_store
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    settings_store.set_many({"connections.allow_private_targets": True})
    assert connections.check_target("http://169.254.169.254/v1") == "http://169.254.169.254/v1"


def test_bad_schemes_and_credentials_in_the_url_are_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    for url in ("file:///etc/passwd", "ftp://x/y", "127.0.0.1:1234", "http://user:pw@127.0.0.1/v1", "http://", ""):
        with pytest.raises(ConnectionError_) as e:
            connections.check_target(url)
        assert e.value.code == "connection_invalid_url", url


# --------------------------------------------------------------------------- #
# Test
# --------------------------------------------------------------------------- #


async def test_a_good_connection_reports_models_and_latency(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    _mock_http(monkeypatch, _openai_server())
    c = connections.create(type="openai_compatible", base_url="http://127.0.0.1:9/v1", api_key="sk-live-1234567890")  # scrub-ok: test fixture, not a real credential
    res = await connections.test_and_record(c)
    assert res["ok"] and res["reachable"] and res["auth_ok"] and res["models_listed"]
    assert res["models"] == ["m1", "m2"] and isinstance(res["latency_ms"], int)
    saved = connections.get(c.id)
    assert saved.models == ["m1", "m2"] and saved.default_model == "m1" and saved.last_test["ok"]


async def test_a_rejected_key_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    _mock_http(monkeypatch, _openai_server())
    res = await connections.run_test(type="openai_compatible", base_url="http://127.0.0.1:9/v1", api_key="wrong")
    assert res["ok"] is False and res["reachable"] is True and res["auth_ok"] is False
    assert res["problem"] == "auth" and "key" in res["message"].lower()


async def test_an_unreachable_server_says_what_to_check(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)

    def boom(req):
        raise httpx.ConnectError("refused")
    _mock_http(monkeypatch, boom)
    res = await connections.run_test(type="lmstudio", base_url="http://127.0.0.1:1234/v1")
    assert res["ok"] is False and res["reachable"] is False and res["problem"] == "unreachable"
    assert "server" in res["fix"].lower()


async def test_a_blocked_or_malformed_address_is_a_result_not_an_exception(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    r1 = await connections.run_test(type="openai_compatible", base_url="http://169.254.169.254/v1")
    r2 = await connections.run_test(type="openai_compatible", base_url="nonsense")
    r3 = await connections.run_test(type="martian")
    assert (r1["problem"], r2["problem"], r3["problem"]) == ("blocked_target", "invalid_url", "invalid_type")


async def test_studioforge_is_recognised_by_its_version_endpoint_and_sent_a_bearer(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((req.url.path, req.headers.get("authorization")))
        if req.url.path == "/api/version":
            return httpx.Response(200, json={"product": "StudioForge", "version": "2.0.1"})
        if req.url.path.endswith("/models"):
            if req.headers.get("authorization") != "Bearer sf-secret-123456":
                return httpx.Response(401, json={"error": "key required"})
            return httpx.Response(200, json={"data": [{"id": "qwen"}]})
        return httpx.Response(404)
    _mock_http(monkeypatch, handler)
    c = connections.create(type="studioforge", api_key="sf-secret-123456")  # scrub-ok: test fixture, not a real credential
    res = await connections.run_test(c)
    assert res["ok"] and res["product"] == "StudioForge" and res["version"] == "2.0.1"
    assert ("/v1/models", "Bearer sf-secret-123456") in seen            # the credential ref's key is sent
    # no key -> StudioForge 2.0 refuses; the result says auth, not "down"
    res = await connections.run_test(type="studioforge", base_url="http://127.0.0.1:1234/v1")
    assert res["problem"] == "auth" and res["reachable"]


async def test_discovery_finds_each_kind_of_server(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)

    def handler(req: httpx.Request) -> httpx.Response:
        host, port, path = req.url.host, req.url.port, req.url.path
        if host != "127.0.0.1":
            raise httpx.ConnectError("no route")
        if port == 1234 and path == "/api/version":
            return httpx.Response(200, json={"product": "StudioForge", "version": "2.0.0"})
        if port == 11434 and path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "llama3"}]})
        if port == 8080 and path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "gguf"}]})
        return httpx.Response(404)
    _mock_http(monkeypatch, handler)
    found = await connections.discover()
    by_type = {f["type"]: f for f in found}
    assert set(by_type) == {"studioforge", "ollama", "openai_compatible"}
    assert by_type["studioforge"]["base_url"] == "http://127.0.0.1:1234/v1"
    assert by_type["ollama"]["models"] == ["llama3"] and by_type["ollama"]["already_added"] is None
    connections.create(type="ollama")
    assert {f["type"]: f for f in await connections.discover()}["ollama"]["already_added"] == "ollama"
    # a link-local host named by a caller is never probed
    probed = []
    _mock_http(monkeypatch, lambda req: probed.append(req.url.host) or httpx.Response(404))
    await connections.discover(hosts=["169.254.169.254"])
    assert "169.254.169.254" not in probed


async def test_discovery_recognises_an_owner_protected_studioforge_and_says_it_needs_a_key(tmp_path, monkeypatch):
    """System test 2026-10-18: StudioForge 2.0 in Docker answers 401 on /api/version, so Find servers
    returned nothing. /health stays public and carries a shape only StudioForge reports."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    health = {"status": "ok", "version": "2.0.0", "instance": "primary", "boot": {"phase": "ready"},
              "loaded_models": [], "can_serve": True, "engine": {"ok": True}}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host != "127.0.0.1" or req.url.port != 1234:
            raise httpx.ConnectError("no route")
        if req.url.path == "/health":
            return httpx.Response(200, json=health)
        return httpx.Response(401, json={"error": {"code": "invalid_api_key"}})
    _mock_http(monkeypatch, handler)
    found = await connections.discover()
    assert [(f["type"], f["product"], f["version"], f["needs_key"]) for f in found] == [
        ("studioforge", "StudioForge", "2.0.0", True)]
    # a plain /health that only says ok is not enough to be called StudioForge
    _mock_http(monkeypatch, lambda req: httpx.Response(200, json={"status": "ok"})
               if req.url.path == "/health" and req.url.port == 1234 and req.url.host == "127.0.0.1"
               else httpx.Response(404))
    assert await connections.discover() == []
    # with a key the real /api/version answer wins and no key is requested
    def keyed(req: httpx.Request) -> httpx.Response:
        if req.url.host == "127.0.0.1" and req.url.port == 1234 and req.url.path == "/api/version":
            ok = req.headers.get("authorization") == "Bearer sf-i-test-key"
            return httpx.Response(200, json={"product": "StudioForge", "version": "2.0.0"}) if ok                 else httpx.Response(401)
        raise httpx.ConnectError("no route")
    _mock_http(monkeypatch, keyed)
    ident = await connections.identify("http://127.0.0.1:1234/v1", key="sf-i-test-key")  # scrub-ok: test fixture
    assert ident and ident["product"] == "StudioForge" and not ident.get("needs_key")


# --------------------------------------------------------------------------- #
# Adopting the 2.x inline blocks, and resolving through a connection
# --------------------------------------------------------------------------- #


def _roster():
    return [
        {"id": "a", "name": "A", "api": {"provider": "openai", "api_key": "sk-inline-aaaaaaaa", "model": "gpt-4o"}},
        {"id": "b", "name": "B", "advisor": {"tools": True, "providers": [
            {"provider": "openai", "api_key": "sk-inline-aaaaaaaa", "model": "gpt-4o-mini"},
            {"provider": "lmstudio", "base_url": "http://127.0.0.1:1234/v1", "model": "local"}]}},
        {"id": "c", "name": "C", "advisor": {"providers": [
            {"provider": "deepseek", "api_key_env": "DEEPSEEK_API_KEY", "model": "deepseek-chat"}]}},
        {"id": "d", "name": "D"},
    ]


def test_adoption_reuses_one_connection_per_server_and_moves_only_the_key(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    roster = _roster()
    plan = connections.adopt_inline(roster)
    assert plan.linked == 4 and len(plan.created) == 3                  # openai (shared), lmstudio, deepseek
    a, b = roster[0]["api"], roster[1]["advisor"]["providers"]
    assert a["connection"] == b[0]["connection"] != b[1]["connection"]
    for blk in (a, b[0]):
        assert "api_key" not in blk and blk["provider"] == "openai"     # inline stays readable, minus the key
    assert a["model"] == "gpt-4o" and b[0]["model"] == "gpt-4o-mini"    # the model stays per character
    # ZERO BREAKAGE: the same key and the same resolved provider come out the other end
    assert llm_api.resolve_key(a) == "sk-inline-aaaaaaaa" == llm_api.resolve_key(b[0])
    res = llm_api.resolve_api(a, "A", "a")
    assert (res.provider.id, res.base_url, res.api_key, res.model) == (
        "openai", "https://api.openai.com/v1", "sk-inline-aaaaaaaa", "gpt-4o")
    # the env-var provider keeps its variable, by reference
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-env-123456789")
    ds = roster[2]["advisor"]["providers"][0]
    assert connections.get(ds["connection"]).credential_ref == "env:DEEPSEEK_API_KEY"
    assert llm_api.resolve_key(ds) == "ds-env-123456789"
    # and it is idempotent
    again = connections.adopt_inline(roster)
    assert again.created == [] and again.linked == 0


def test_adoption_never_names_a_connection_after_its_address(tmp_path, monkeypatch):
    """A connection's name reaches non-Admin screens; the address must not be in it."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    roster = [
        {"id": "a", "api": {"provider": "custom", "base_url": "http://192.0.2.7:1234/v1", "model": "m"}},
        {"id": "b", "api": {"provider": "custom", "base_url": "http://192.0.2.6:4321/v1", "model": "m"}},
    ]
    dry = connections.adopt_inline([dict(r, api=dict(r["api"])) for r in roster], dry_run=True)
    assert not any("192.0.2" in a.split("->")[0] for a in dry.actions), dry.actions
    plan = connections.adopt_inline(roster)
    assert len(plan.created) == 2
    names = [connections.get(cid).name for cid in plan.created]
    assert all("192.0.2" not in n and "1234" not in n for n in names), names
    assert len(set(names)) == 2, names                               # still told apart
    labels = connections.neutral_labels(connections.list_all())
    assert sorted(labels.values()) == ["OpenAI-compatible server 1", "OpenAI-compatible server 2"]


def test_adoption_dry_run_creates_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    roster = _roster()
    plan = connections.adopt_inline(roster, dry_run=True)
    assert plan.actions and not (tmp_path / "chats.db").exists()
    assert "connection" not in roster[0]["api"] and roster[0]["api"]["api_key"] == "sk-inline-aaaaaaaa"


def test_a_deleted_connection_falls_back_to_the_inline_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    blk = {"provider": "lmstudio", "base_url": "http://127.0.0.1:1234/v1", "model": "m", "connection": "gone"}
    assert llm_api.resolve_api(blk, "X").base_url == "http://127.0.0.1:1234/v1"
    with pytest.raises(llm_api.ApiError):
        llm_api.resolve_api({"provider": "lmstudio", "model": "m", "connection": "gone"}, "X")


def test_an_address_that_became_blocked_stops_the_turn(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    c = connections.create(type="lmstudio")
    c.base_url = "http://169.254.169.254/v1"                            # edited behind the API's back
    connections._save(c)
    with pytest.raises(llm_api.ApiError):
        llm_api.resolve_api({"provider": "lmstudio", "model": "m", "connection": c.id}, "X")


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #


def test_the_http_round_trip_never_leaks_a_key(world, owner):
    r = owner.post("/api/connections", json={"type": "openai", "api_key": "sk-live-1234567890", "name": "My OpenAI"})
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    for url in ("/api/connections", f"/api/connections/{cid}"):
        body = owner.get(url).text
        assert "sk-live-1234567890" not in body
    got = owner.get(f"/api/connections/{cid}").json()
    assert got["credential"]["has_key"] is True and got["type_label"] == "OpenAI" and got["in_use_by"] == []
    assert owner.patch(f"/api/connections/{cid}", json={"name": "Renamed", "default_model": "gpt-4o"}).json()["name"] == "Renamed"
    assert owner.patch(f"/api/connections/{cid}", json={}).status_code == 422
    assert owner.get("/api/connections").json()["types"][0]["id"] == "openai_compatible"
    assert owner.delete(f"/api/connections/{cid}").status_code == 200
    assert owner.get(f"/api/connections/{cid}").status_code == 404


def test_the_http_errors_carry_catalogued_codes(world, owner):
    r = owner.post("/api/connections", json={"type": "openai_compatible", "base_url": "http://169.254.169.254/v1"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "connection_blocked_target"
    r = owner.post("/api/connections", json={"type": "anthropic"})
    assert r.status_code == 422 or r.status_code == 400
    assert r.json()["error"]["code"] == "connection_invalid"
    assert owner.post("/api/connections", json={"type": "lmstudio", "id": "same"}).status_code == 201
    r = owner.post("/api/connections", json={"type": "lmstudio", "id": "same"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "connection_exists"
    r = owner.post("/api/connections", json={"type": "lmstudio", "bogus": 1})
    assert r.status_code == 422


def test_test_endpoint_borrows_the_saved_key_for_the_same_server_only(world, owner, monkeypatch):
    handler = _openai_server(key="sk-live-1234567890")
    _mock_http(monkeypatch, handler)
    cid = owner.post("/api/connections", json={"type": "openai_compatible", "base_url": "http://127.0.0.1:9/v1",
                                               "api_key": "sk-live-1234567890"}).json()["id"]
    ok = owner.post("/api/connections/test", json={"type": "openai_compatible", "base_url": "http://127.0.0.1:9/v1",
                                                   "use_connection": cid}).json()
    assert ok["ok"] is True
    other = owner.post("/api/connections/test", json={"type": "openai_compatible", "base_url": "http://127.0.0.1:7/v1",
                                                      "use_connection": cid}).json()
    assert other["ok"] is False and other["auth_ok"] is False          # a different address never gets the key
    saved = owner.post(f"/api/connections/{cid}/test").json()
    assert saved["ok"] and saved["connection"]["models"] == ["m1", "m2"]


def test_only_manage_connections_may_touch_connections(world, owner):
    max_ = world.person(owner, "Max", "member", pin="5555")
    member = world.client(("203.0.113.52", 40002))
    world.login(member, profile_id=max_["id"], pin="5555")
    stranger = world.client(("203.0.113.51", 40001))
    for c in (member, stranger):
        assert c.get("/api/connections").status_code in (401, 403)
        assert c.post("/api/connections", json={"type": "lmstudio"}).status_code in (401, 403)
        assert c.post("/api/connections/discover").status_code in (401, 403)
        assert c.post("/api/connections/test", json={"type": "lmstudio"}).status_code in (401, 403)
    # an Admin holds the permission
    world.person(owner, "Ada", "admin", password="admin-pass1")
    admin = world.client()
    assert world.login(admin, name="Ada", password="admin-pass1").status_code == 200
    assert admin.get("/api/connections").status_code == 200


def test_adopt_endpoint_links_a_legacy_character_without_breaking_it(world, owner):
    config.upsert_bot(config.Bot(id="legacy", name="Legacy", advisor={
        "providers": [{"provider": "openai", "model": "gpt-4o", "api_key": "sk-legacy-123456789"}]}))
    r = owner.post("/api/connections/adopt")
    assert r.status_code == 200 and r.json()["linked"] == 1
    raw = yaml.safe_load(config.CONFIG_PATH.read_text(encoding="utf-8"))
    entry = next(e for e in raw["bots"] if e["id"] == "legacy")["advisor"]["providers"][0]
    assert "api_key" not in entry and entry["connection"] and entry["model"] == "gpt-4o"
    assert llm_api.resolve_key(entry) == "sk-legacy-123456789"
    assert "sk-legacy" not in config.CONFIG_PATH.read_text(encoding="utf-8")
    assert owner.post("/api/connections/adopt").json()["linked"] == 0


# --------------------------------------------------------------------------- #
# Embeddings borrow the key of the connection for the same server (ops 2026-10-19)
# --------------------------------------------------------------------------- #


def test_advisor_embeddings_use_the_key_of_the_connection_for_the_same_server(tmp_path, monkeypatch):
    """The rig's StudioForge began requiring a key; the chat fallback's connection had it,
    the un-migrated `embeddings:` block did not, and every embedding came back 401."""
    from app import advisor
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    connections.create(type="openai_compatible", base_url="http://192.0.2.5:1234/v1",
                       api_key="sf-rig-key-1234567")  # scrub-ok: test fixture, not a real credential
    connections.create(type="openai_compatible", base_url="http://192.0.2.9:1234/v1",
                       api_key="sf-other-key-1234567")  # scrub-ok: test fixture, not a real credential
    cfg = advisor._embed_config({"base_url": "http://192.0.2.5:1234/v1", "model": "embed-8b"})
    assert cfg is not None and cfg.api_key == "sf-rig-key-1234567"
    # same origin, other path: still that server's key
    cfg = advisor._embed_config({"base_url": "http://192.0.2.5:1234/v1/", "model": "embed-8b"})
    assert cfg.api_key == "sf-rig-key-1234567"
    # an inline key wins; an unknown server gets none
    assert advisor._embed_config({"base_url": "http://192.0.2.5:1234/v1", "model": "e",
                                  "api_key": "inline-123456"}).api_key == "inline-123456"
    assert advisor._embed_config({"base_url": "http://192.0.2.7:1234/v1", "model": "e"}).api_key == ""
