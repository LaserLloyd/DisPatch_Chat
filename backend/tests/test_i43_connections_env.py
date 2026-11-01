"""4.3 A3 (#31, R-2): only the main Admin (the protected Admin, the Owner) may bind a SERVER
environment variable as a connection key, or move a saved key to another server.

An Admin who is not the Owner can still do every household thing with a connection: add one
with a pasted key, rename it, change its model. What changes is the host-level part: naming an
environment variable of the DisPatch process (its value then goes to whatever server the
connection points at), carrying a stored or environment key to an address of the caller's
choosing, and lowering the permission a connection needs.
"""
from __future__ import annotations

import httpx
import pytest

from app import config, connections, llm_api

ENV = "ANTHROPIC_API_KEY"
ENV_VALUE = "sk-env-value-0123456789"            # scrub-ok: test fixture, not a real credential
STORED = "sk-stored-0123456789"                  # scrub-ok: test fixture, not a real credential


@pytest.fixture
def pair(world, monkeypatch):
    """(owner, admin): the protected Admin and a second, non-protected Admin."""
    monkeypatch.setenv(ENV, ENV_VALUE)
    owner = world.create_owner()
    world.person(owner, "Ada", "admin", password="admin-pass1")
    admin = world.client(("203.0.113.60", 40100))
    assert world.login(admin, name="Ada", password="admin-pass1").status_code == 200
    return owner, admin


def _code(r) -> str:
    return (r.json().get("error") or {}).get("code", "")


def _mock_http(monkeypatch):
    seen: list[tuple[str, str | None]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((str(req.url), req.headers.get("authorization") or req.headers.get("x-api-key")))
        if req.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "m1"}]})
        return httpx.Response(404)

    def factory(read_timeout: float = llm_api.READ_TIMEOUT_S) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=read_timeout,
                                 follow_redirects=False)
    monkeypatch.setattr(llm_api, "http_client", factory)
    return seen


def _leaked(seen, secret: str) -> bool:
    return any(h and secret in h for _u, h in seen)


# (a) + (b) ------------------------------------------------------------------ #

def test_a_non_protected_admin_cannot_bind_an_env_variable_on_create(pair):
    owner, admin = pair
    r = admin.post("/api/connections", json={"type": "anthropic", "api_key_env": ENV})
    assert r.status_code == 403, r.text
    assert _code(r) == "env_key_owner_only"
    assert connections.list_all() == []                       # refused before anything was written
    # (b) the protected Admin may
    r = owner.post("/api/connections", json={"type": "anthropic", "api_key_env": ENV})
    assert r.status_code == 201, r.text
    assert r.json()["credential"] == {"kind": "env", "env": ENV, "has_key": True}


def test_a_non_protected_admin_still_adds_a_connection_with_a_pasted_key(pair):
    _owner, admin = pair
    r = admin.post("/api/connections", json={"type": "openai", "api_key": STORED})
    assert r.status_code == 201, r.text


def test_a_non_protected_admin_cannot_bind_an_env_variable_through_llm_connect(pair):
    owner, admin = pair
    body = {"provider": "anthropic", "model": "claude-x", "api_key_env": ENV, "target": "envbot"}
    r = admin.post("/api/llm/connect", json=body)
    assert r.status_code == 403, r.text
    assert _code(r) == "env_key_owner_only"
    assert config.get_bot("envbot") is None and connections.list_all() == []
    r = admin.post("/api/llm/connect", json={**body, "backend": "legacy-api", "bot_id": "envlegacy"})
    assert r.status_code == 403 and _code(r) == "env_key_owner_only"
    assert config.get_bot("envlegacy") is None
    # (b) the protected Admin may
    r = owner.post("/api/llm/connect", json=body)
    assert r.status_code == 200, r.text
    (conn,) = connections.list_all()
    assert conn.credential_ref == "env:" + ENV


def test_a_non_protected_admin_cannot_bind_an_env_variable_through_quicksetup(pair, monkeypatch):
    owner, admin = pair
    monkeypatch.setenv("OPENAI_API_KEY", ENV_VALUE)
    body = {"provider": "openai", "api_key_env": "OPENAI_API_KEY", "target": "quick", "test": False}
    r = admin.post("/api/llm/quicksetup", json=body)
    assert r.status_code == 403 and _code(r) == "env_key_owner_only"
    assert config.get_bot("quick") is None
    r = owner.post("/api/llm/quicksetup", json=body)
    assert r.status_code == 200, r.text


def test_a_non_protected_admin_cannot_move_a_bots_inline_key_to_another_server(pair):
    owner, admin = pair
    config.upsert_bot(config.Bot(id="inline", name="Inline", api={
        "provider": "custom", "base_url": "http://127.0.0.1:9/v1", "model": "m",
        "api_key": STORED}))
    body = {"backend": "legacy-api", "bot_id": "inline", "provider": "custom",
            "base_url": "http://198.51.100.7:8080/v1", "model": "m"}
    r = admin.post("/api/llm/connect", json=body)
    assert r.status_code == 403, r.text
    assert config.get_bot("inline").api["base_url"] == "http://127.0.0.1:9/v1"
    # the same server, re-saved without a key: still fine for any Admin
    r = admin.post("/api/llm/connect", json={**body, "base_url": "http://127.0.0.1:9/v1"})
    assert r.status_code == 200, r.text
    # the Owner may move it
    r = owner.post("/api/llm/connect", json=body)
    assert r.status_code == 200, r.text


# (f) ------------------------------------------------------------------------ #

def test_an_admin_may_rename_an_env_keyed_connection_but_not_move_a_keyed_one(pair):
    owner, admin = pair
    env_id = owner.post("/api/connections", json={"type": "anthropic", "api_key_env": ENV}).json()["id"]
    key_id = owner.post("/api/connections", json={
        "type": "openai_compatible", "base_url": "http://127.0.0.1:9/v1", "api_key": STORED}).json()["id"]
    # harmless edits stay allowed, also when the form re-sends the same env name
    assert admin.patch(f"/api/connections/{env_id}", json={"name": "Claude"}).status_code == 200
    assert admin.patch(f"/api/connections/{env_id}", json={"default_model": "claude-x"}).status_code == 200
    r = admin.patch(f"/api/connections/{env_id}", json={"name": "Claude 2", "api_key_env": ENV})
    assert r.status_code == 200, r.text
    # naming ANOTHER variable is a new binding
    r = admin.patch(f"/api/connections/{env_id}", json={"api_key_env": "OTHER_API_KEY"})
    assert r.status_code == 403 and _code(r) == "env_key_owner_only"
    r = admin.patch(f"/api/connections/{key_id}", json={"api_key_env": "OTHER_API_KEY"})
    assert r.status_code == 403 and _code(r) == "env_key_owner_only"
    assert connections.get(key_id).credential_ref.startswith("secret:")
    # moving a keyed connection's address
    r = admin.patch(f"/api/connections/{key_id}", json={"base_url": "http://198.51.100.7:8080/v1"})
    assert r.status_code == 403, r.text
    # also to another port of the same host (another program there would get the key)
    r = admin.patch(f"/api/connections/{key_id}", json={"base_url": "http://127.0.0.1:7/v1"})
    assert r.status_code == 403, r.text
    r = admin.patch(f"/api/connections/{env_id}", json={"base_url": "https://198.51.100.7/v1"})
    assert r.status_code == 403, r.text
    assert connections.get(key_id).base_url == "http://127.0.0.1:9/v1"
    # with a new key pasted nothing is carried over: allowed
    r = admin.patch(f"/api/connections/{key_id}", json={"base_url": "http://127.0.0.1:7/v1",
                                                         "api_key": "sk-new-0123456789"})
    assert r.status_code == 200, r.text
    # the protected Admin may move it
    r = owner.patch(f"/api/connections/{key_id}", json={"base_url": "http://198.51.100.7:8080/v1"})
    assert r.status_code == 200, r.text
    r = owner.patch(f"/api/connections/{env_id}", json={"api_key_env": "OTHER_API_KEY"})
    assert r.status_code == 200, r.text


def test_an_admin_may_move_a_connection_that_holds_no_key(pair):
    owner, admin = pair
    cid = owner.post("/api/connections", json={"type": "lmstudio"}).json()["id"]
    r = admin.patch(f"/api/connections/{cid}", json={"base_url": "http://127.0.0.1:5555/v1"})
    assert r.status_code == 200, r.text


def test_only_the_protected_admin_changes_a_connections_required_permission(pair):
    owner, admin = pair
    cid = owner.post("/api/connections", json={"type": "lmstudio"}).json()["id"]
    r = admin.patch(f"/api/connections/{cid}", json={"required_permission": "admin_system"})
    assert r.status_code == 403, r.text
    r = owner.patch(f"/api/connections/{cid}", json={"required_permission": "admin_system"})
    assert r.status_code == 200, r.text


# POST /api/connections/test and /api/llm/test: a key never goes to a caller-chosen address -- #

def test_the_test_route_never_carries_a_borrowed_key_to_the_types_default(pair, monkeypatch):
    owner, admin = pair
    seen = _mock_http(monkeypatch)
    cid = owner.post("/api/connections", json={
        "type": "openai", "base_url": "http://127.0.0.1:9/v1", "api_key": STORED}).json()["id"]
    # No base_url in the form: the key is tried against the connection's own address only.
    for who in (admin, owner):
        seen.clear()
        r = who.post("/api/connections/test", json={"type": "openai", "use_connection": cid})
        assert r.status_code == 200, r.text
        assert all(u.startswith("http://127.0.0.1:9/") for u, _h in seen), seen
    # A different address never gets the key, for anybody (the old rule holds).
    for who in (admin, owner):
        seen.clear()
        who.post("/api/connections/test", json={"type": "openai", "base_url": "http://127.0.0.1:7/v1",
                                                "use_connection": cid})
        assert not _leaked(seen, STORED)


def test_llm_test_reads_a_server_variable_for_the_protected_admin_only(pair, monkeypatch):
    owner, admin = pair
    seen = _mock_http(monkeypatch)
    body = {"provider": "custom", "base_url": "http://198.51.100.7:8080/v1",
            "api_key_env": ENV, "model": "m"}
    assert admin.post("/api/llm/test", json=body).status_code == 200
    assert not _leaked(seen, ENV_VALUE)
    seen.clear()
    assert owner.post("/api/llm/test", json=body).status_code == 200
    assert _leaked(seen, ENV_VALUE)


def test_llm_test_borrows_a_bots_key_for_its_own_server_only(pair, monkeypatch):
    owner, admin = pair
    seen = _mock_http(monkeypatch)
    config.upsert_bot(config.Bot(id="inline", name="Inline", api={
        "provider": "custom", "base_url": "http://127.0.0.1:9/v1", "model": "m",
        "api_key": STORED}))
    away = {"provider": "custom", "base_url": "http://198.51.100.7:8080/v1",
            "target": "inline", "model": "m"}
    admin.post("/api/llm/test", json=away)
    assert not _leaked(seen, STORED)
    seen.clear()
    admin.post("/api/llm/test", json={**away, "base_url": "http://127.0.0.1:9/v1"})
    assert _leaked(seen, STORED)                               # its own server: the re-test works


# INT-1: the image-generation provider (PUT /api/llm/image, POST /api/llm/image/test) --------- #

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


@pytest.fixture
def fake_image(monkeypatch):
    from app import image_providers
    sent = []

    async def generate(res, spec):
        sent.append((res.base_url, res.api_key))
        return PNG
    monkeypatch.setattr(image_providers, "generate", generate)
    return sent


def _img(base="http://img-good.example/v1", **kw):
    return {"provider": "custom", "base_url": base, "model": "m", **kw}


def test_image_provider_env_variable_is_the_protected_admins(pair, fake_image):
    owner, admin = pair
    for path, call in (("/api/llm/image", admin.put), ("/api/llm/image/test", admin.post)):
        r = call(path, json=_img(api_key_env=ENV))
        assert r.status_code == 403 and _code(r) == "env_key_owner_only", (path, r.text)
    assert not any(k == ENV_VALUE for _b, k in fake_image), "the variable's value left the box"
    assert owner.put("/api/llm/image", json=_img(api_key_env=ENV)).status_code == 200
    r = owner.post("/api/llm/image/test", json=_img(api_key_env=ENV))
    assert r.status_code == 200 and r.json()["ok"] is True, r.text
    # a second Admin re-saving the Owner's form (same variable, same server) is fine
    assert admin.put("/api/llm/image", json=_img(api_key_env=ENV, model="m2")).status_code == 200
    # ...but may not point it at another server
    for path, call in (("/api/llm/image", admin.put), ("/api/llm/image/test", admin.post)):
        r = call(path, json=_img(base="http://img-evil.example/v1", api_key_env=ENV))
        assert r.status_code == 403, (path, r.text)
    assert not any("img-evil" in b for b, _k in fake_image)


def test_image_provider_stored_key_never_follows_a_second_admin_to_another_server(pair, fake_image):
    owner, admin = pair
    assert owner.put("/api/llm/image", json=_img(api_key=STORED)).status_code == 200
    for path, call in (("/api/llm/image", admin.put), ("/api/llm/image/test", admin.post)):
        r = call(path, json=_img(base="http://img-evil.example/v1"))
        assert r.status_code == 403 and _code(r) == "permission_denied", (path, r.text)
    assert not any(k == STORED for _b, k in fake_image)
    # same server: keeps the key (a model change), and the Test uses it there
    assert admin.put("/api/llm/image", json=_img(model="m3")).status_code == 200
    assert admin.post("/api/llm/image/test", json=_img()).json()["ok"] is True
    assert fake_image[-1] == ("http://img-good.example/v1", STORED)
    # a pasted key may go anywhere; the Owner may move the saved one
    assert admin.put("/api/llm/image", json=_img(base="http://img-new.example/v1",
                                                  api_key="pasted-0123")).status_code == 200
    assert owner.put("/api/llm/image", json=_img(base="http://img-other.example/v1")).status_code == 200
