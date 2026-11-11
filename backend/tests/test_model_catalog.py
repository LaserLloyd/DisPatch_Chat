"""Model catalog (WP1): classify, bounds, documented fallback, key origin, cooldown, routes."""
from __future__ import annotations

import asyncio

import httpx
import pytest

from app import config, connections, image_providers, llm_api, model_catalog

KEY = "sk-catalog-0123456789"        # scrub-ok: test fixture, not a real credential


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(model_catalog, "_last", {})
    monkeypatch.setattr(image_providers, "transport", None)
    for n in ("MINIMAX_API_KEY", "OPENAI_API_KEY", "XAI_API_KEY", "CIVITAI_API_KEY"):
        monkeypatch.delenv(n, raising=False)


def _transport(monkeypatch, handler):
    seen: list[httpx.Request] = []

    def h(req):
        seen.append(req)
        return handler(req)
    monkeypatch.setattr(image_providers, "transport", httpx.MockTransport(h))
    return seen


def run(coro):
    return asyncio.run(coro)


def test_classify():
    assert model_catalog.classify("gpt-image-1") == "image"
    assert model_catalog.classify("dall-e-3") == "image"
    assert model_catalog.classify("grok-imagine-image") == "image"
    assert model_catalog.classify("text-embedding-3-small") == "embedding"
    assert model_catalog.classify("gpt-4o") == "chat"


def test_openai_bounds_and_filter(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    ids = [{"id": f"gpt-image-{i}"} for i in range(700)] + [{"id": "bad id!"}, {"id": "gpt-4o"}, {"id": 5}]
    _transport(monkeypatch, lambda r: httpx.Response(200, json={"data": ids}))
    e = run(model_catalog.refresh_image("openai"))
    assert e["source"] == "endpoint" and len(e["models"]) == model_catalog.MAX_IDS
    assert all(m["kind"] == "image" and " " not in m["id"] for m in e["models"])
    assert (config.DATA_DIR / "model-catalog.json").stat().st_mode & 0o777 == 0o600


def test_oversize_response_falls_back_documented(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    _transport(monkeypatch, lambda r: httpx.Response(200, content=b"x" * (model_catalog.MAX_BYTES + 10)))
    e = run(model_catalog.refresh_image("openai"))
    assert e["source"] == "documented" and e["error"]
    assert {m["id"] for m in e["models"]} == {"gpt-image-1", "dall-e-3"}


def test_redirect_not_followed(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    seen = _transport(monkeypatch, lambda r: httpx.Response(302, headers={"location": "http://evil.example/x"}))
    e = run(model_catalog.refresh_image("openai"))
    assert e["source"] == "documented" and len(seen) == 1


def test_minimax_documented_with_note(monkeypatch):
    monkeypatch.setenv("MINIMAX_API_KEY", KEY)
    seen = _transport(monkeypatch, lambda r: httpx.Response(200, json={"data": [{"id": "MiniMax-M3.1-Flash"}]}))
    e = run(model_catalog.refresh_image("minimax"))
    assert e["source"] == "documented" and "no image models" in e["note"]
    assert [m["id"] for m in e["models"]][0] == "MiniMax-H3-Image"
    assert seen[0].url.path == "/v1/models"
    im = model_catalog.image_models("minimax")
    assert im["models"][:2] == ["MiniMax-H3-Image", "MiniMax-H3-Image-Highspeed"]
    assert im["names"]["MiniMax-H3-Image"].startswith("MiniMax H3 Image")


def test_unconfigured_is_documented_with_error():
    e = run(model_catalog.refresh_image("minimax"))
    assert e["source"] == "documented" and e["error"] and e["models"]


def test_civitai_no_call(monkeypatch):
    seen = _transport(monkeypatch, lambda r: httpx.Response(500))
    e = run(model_catalog.refresh_image("civitai"))
    assert not seen and e["source"] == "documented" and "Browse" in e["note"]


def test_xai_endpoint_models_shape(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", KEY)
    seen = _transport(monkeypatch, lambda r: httpx.Response(200, json={"models": [
        {"id": "grok-imagine-image", "object": "model"}, {"id": "grok-imagine-image-2.0"}]}))
    e = run(model_catalog.refresh_image("xai"))
    assert e["source"] == "endpoint" and [m["id"] for m in e["models"]] == [
        "grok-imagine-image", "grok-imagine-image-2.0"]
    assert seen[0].url.path == "/v1/image-generation-models"


def test_xai_fallback_to_models_filtered(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", KEY)

    def h(r):
        if r.url.path.endswith("image-generation-models"):
            return httpx.Response(404)
        return httpx.Response(200, json={"data": [{"id": "grok-4"}, {"id": "grok-imagine-image"}]})
    seen = _transport(monkeypatch, h)
    e = run(model_catalog.refresh_image("xai"))
    assert [m["id"] for m in e["models"]] == ["grok-imagine-image"] and len(seen) == 2


def test_key_only_goes_to_resolved_origin(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    seen = _transport(monkeypatch, lambda r: httpx.Response(200, json={"data": []}))
    run(model_catalog.refresh_image("openai"))
    assert seen and all(r.url.host == "api.openai.com" for r in seen)
    assert seen[0].headers["authorization"] == f"Bearer {KEY}"
    assert KEY not in (config.DATA_DIR / "model-catalog.json").read_text()


def test_cooldown(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    seen = _transport(monkeypatch, lambda r: httpx.Response(200, json={"data": [{"id": "gpt-image-1"}]}))
    a = run(model_catalog.refresh_image("openai"))
    b = run(model_catalog.refresh_image("openai"))
    assert len(seen) == 1 and "cached" not in a and b["cached"] is True
    assert b["models"] == a["models"]


def _mock_chat(monkeypatch, models=("m1", "m2")):
    seen = []

    def handler(req):
        seen.append(req)
        if req.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": m} for m in models]})
        return httpx.Response(404)

    def factory(read_timeout: float = llm_api.READ_TIMEOUT_S):
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=read_timeout,
                                 follow_redirects=False)
    monkeypatch.setattr(llm_api, "http_client", factory)
    return seen


def test_connection_refresh_updates_column_and_catalog(monkeypatch):
    _mock_chat(monkeypatch, ("gpt-4o", "text-embedding-3-small"))
    c = connections.create(type="openai", api_key=KEY)
    e = run(model_catalog.refresh_connection(c))
    assert e["source"] == "endpoint"
    assert {m["id"]: m["kind"] for m in e["models"]} == {"gpt-4o": "chat", "text-embedding-3-small": "embedding"}
    assert connections.get(c.id).models == ["gpt-4o", "text-embedding-3-small"]
    assert model_catalog.catalog()["connections"][c.id]["models"] == e["models"]


# ---- routes -------------------------------------------------------------- #

@pytest.fixture
def clients(world):
    owner = world.create_owner()
    world.person(owner, "Mia", "user", pin="1357")
    member = world.client(("203.0.113.61", 40101))
    assert world.login(member, name="Mia", pin="1357").status_code == 200
    return owner, member


def test_routes_forbidden_for_non_managers(clients):
    owner, member = clients
    c = connections.create(type="openai", api_key=KEY)
    for r in (member.get("/api/connections/models"),
              member.post("/api/connections/models/refresh"),
              member.post(f"/api/connections/{c.id}/models/refresh")):
        assert r.status_code == 403, r.text


def test_routes_for_owner(clients, monkeypatch):
    owner, _ = clients
    _mock_chat(monkeypatch)
    _transport(monkeypatch, lambda r: httpx.Response(200, json={"data": []}))
    c = connections.create(type="openai", api_key=KEY)
    r = owner.get("/api/connections/models")
    assert r.status_code == 200 and set(r.json()) == {"version", "connections", "image"}
    r = owner.post(f"/api/connections/{c.id}/models/refresh")
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["connection"]["models"] == ["m1", "m2"] and j["catalog"]["source"] == "endpoint"
    assert KEY not in r.text
    assert owner.post("/api/connections/nope/models/refresh").status_code == 404
    r = owner.post("/api/connections/models/refresh")
    assert r.status_code == 200, r.text
    j = r.json()
    assert c.id in j["connections"] and set(j["image"]) == set(image_providers.PRESET_BY_ID)
    assert KEY not in r.text
    assert owner.get("/api/connections/models").json()["image"]["minimax"]["source"] == "documented"
