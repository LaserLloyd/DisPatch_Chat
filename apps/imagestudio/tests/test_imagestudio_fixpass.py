"""Fix-pass tests for the available-models round (mock transport only; no paid call)."""
from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import httpx
import pytest

from app import config, image_jobs, image_providers, model_catalog, settings_store
from tests.test_image_jobs import _png, _unlocked
from tests.test_image_jobs import env as env

BASE = "/api/apps/imagestudio"
KEY = "sk-test-FIXPASS-SECRET-0123456789"   # scrub-ok: test fixture


def _gen(c, **over):
    body = {"prompt": "a silver-haired explorer with a globe", "ratio": "16:9", "n": 1}
    body.update(over)
    return c.post(f"{BASE}/generate", json=body)


def _b64(b):
    return base64.b64encode(b).decode()


class H3Fake:
    def __init__(self):
        self.requests: list[httpx.Request] = []

    def __call__(self, req):
        self.requests.append(req)
        if req.url.path.endswith("/images/generations"):
            n = json.loads(req.content).get("n", 1)
            return httpx.Response(200, json={"data": [
                {"b64_json": _b64(_png(20_000 + i)), "size": "2048x2048"} for i in range(n)]})
        if req.url.path.endswith("/image_generation"):
            n = json.loads(req.content).get("n", 1)
            return httpx.Response(200, json={
                "data": {"image_base64": [_b64(_png(20_000 + i)) for i in range(n)]},
                "metadata": {"success_count": n, "failed_count": 0},
                "base_resp": {"status_code": 0, "status_msg": "success"}})
        return httpx.Response(200, json={"data": []})

    def body(self, i=-1):
        return json.loads(self.requests[i].content)


@pytest.fixture
def fake(monkeypatch, env):
    monkeypatch.setattr(model_catalog, "_last", {})
    for n in ("OPENAI_API_KEY", "XAI_API_KEY", "CIVITAI_API_KEY"):
        monkeypatch.delenv(n, raising=False)
    image_providers.save_config({"provider": "minimax", "api_key": KEY})
    f = H3Fake()
    monkeypatch.setattr(image_providers, "transport", httpx.MockTransport(f))
    yield f
    monkeypatch.setattr(image_providers, "transport", None)


# ---- H1 -------------------------------------------------------------------- #

def test_h3_takes_ten_pictures_and_image01_is_clamped_to_nine(env, fake):
    c = _unlocked(env)
    r = _gen(c, model="MiniMax-H3-Image", n=10)
    assert r.status_code == 200, r.text
    assert fake.body()["n"] == 10 and fake.requests[-1].url.path.endswith("/images/generations")
    r = _gen(c, model="image-01", n=10)
    assert r.status_code == 200, r.text
    assert fake.body()["n"] == 9
    assert _gen(c, model="MiniMax-H3-Image", n=11).status_code == 422


# ---- L9 -------------------------------------------------------------------- #

def test_h3_accepts_a_3000_char_prompt_and_image01_refuses_it(env, fake):
    c = _unlocked(env)
    long = ("a lighthouse on a cliff at dusk, waves, gulls, warm windows. " * 60)[:3000]
    assert _gen(c, model="MiniMax-H3-Image", prompt=long).status_code == 200
    assert len(fake.body()["prompt"]) == len(long.strip())
    r = _gen(c, model="image-01", prompt=long)
    assert r.status_code == 422 and "takes 1500" in r.json()["detail"]


def test_rows_carry_h3_first_in_exact_order(env, fake):
    c = _unlocked(env)
    mm = next(p for p in c.get(BASE + "/state").json()["providers"] if p["id"] == "minimax")
    assert mm["models"][:4] == ["MiniMax-H3-Image", "MiniMax-H3-Image-Highspeed",
                                "image-01", "image-01-live"]
    assert mm["models_checked"] == "2026-11-11"


def test_chat_pic_marker_path_reaches_h3_endpoint(env, monkeypatch):
    import asyncio
    image_providers.save_config({"provider": "minimax", "api_key": KEY, "model": "MiniMax-H3-Image"})
    f = H3Fake()
    monkeypatch.setattr(image_providers, "transport", httpx.MockTransport(f))

    async def go():
        b = image_providers.ApiImages()
        res = await b.enqueue(image_jobs.ImageSpec(prompt="a quiet harbour", backend="api"))
        for _ in range(100):
            p = await b.poll(res.job_id)
            if p.done:
                break
            await asyncio.sleep(0.05)
        return p
    p = asyncio.run(go())
    monkeypatch.setattr(image_providers, "transport", None)
    assert p.state == "done", p
    assert f.requests[0].url.path == "/v1/images/generations"
    assert f.body(0)["disable_prompt_enhanced"] is True and "size" not in f.body(0)


# ---- M2 -------------------------------------------------------------------- #

def test_minimax_only_setup_refresh_reports_no_error(world, monkeypatch):
    for n in ("OPENAI_API_KEY", "XAI_API_KEY", "CIVITAI_API_KEY"):
        monkeypatch.delenv(n, raising=False)
    monkeypatch.setenv("MINIMAX_API_KEY", KEY)
    monkeypatch.setattr(model_catalog, "_last", {})
    seen = []

    def h(req):
        seen.append(req)
        return httpx.Response(200, json={"data": []})
    monkeypatch.setattr(image_providers, "transport", httpx.MockTransport(h))
    owner = world.create_owner()
    body = owner.post(BASE + "/models/refresh").json()
    assert set(body["results"]) == {"minimax"}
    assert body["results"]["minimax"]["error"] is None
    assert {r.url.host for r in seen} == {"api.minimax.io"}


# ---- L9: a denied profile --------------------------------------------------- #

def test_refresh_respects_the_access_matrix_for_a_denied_profile(world, monkeypatch):
    for n in ("OPENAI_API_KEY", "XAI_API_KEY"):
        monkeypatch.delenv(n, raising=False)
    monkeypatch.setenv("MINIMAX_API_KEY", KEY)
    monkeypatch.setenv("CIVITAI_API_KEY", KEY)
    monkeypatch.setattr(model_catalog, "_last", {})
    seen = []

    def h(req):
        seen.append(req)
        return httpx.Response(200, json={"data": []})
    monkeypatch.setattr(image_providers, "transport", httpx.MockTransport(h))
    owner = world.create_owner()
    ids = {p["id"] for p in owner.get(BASE + "/state").json()["providers"]}
    assert "civitai" in ids
    world.person(owner, "Ada", "admin", password="pw-pass-1234")  # scrub-ok: throwaway test password
    settings_store.set_many({"connections.image_access.civitai_admin": False})
    ada = world.client()
    assert world.login(ada, name="Ada", password="pw-pass-1234").status_code == 200  # scrub-ok: throwaway test password
    state = ada.get(BASE + "/state").json()
    assert "civitai" not in {p["id"] for p in state["providers"]} and state["can_refresh_models"]
    seen.clear()
    body = ada.post(BASE + "/models/refresh").json()
    assert "civitai" not in body["results"] and "civitai" not in {p["id"] for p in body["providers"]}
    assert not any("civitai" in r.url.host for r in seen)


# ---- L9: borrowed key ------------------------------------------------------- #

def test_a_borrowed_chat_key_goes_only_to_the_same_origin(world, monkeypatch):
    for n in ("OPENAI_API_KEY", "XAI_API_KEY", "CIVITAI_API_KEY", "MINIMAX_API_KEY"):
        monkeypatch.delenv(n, raising=False)
    monkeypatch.setattr(model_catalog, "_last", {})
    bot = SimpleNamespace(api=None, advisor={"providers": [{
        "provider": "openai", "base_url": "https://pics.example.org/v1", "api_key": KEY}]})
    monkeypatch.setattr(config, "load_bots", lambda: [bot])
    seen = []

    def h(req):
        seen.append(req)
        return httpx.Response(200, json={"data": [{"id": "pic-1"}]})
    monkeypatch.setattr(image_providers, "transport", httpx.MockTransport(h))
    image_providers.save_config({"provider": "custom", "base_url": "https://pics.example.org/v1",
                                 "model": "pic-1"})
    e = __import__("asyncio").run(model_catalog.refresh_image("custom"))
    assert e["source"] == "endpoint" and seen[0].headers["authorization"] == f"Bearer {KEY}"
    # a different origin (scheme/port/host) never borrows it
    for url in ("http://pics.example.org/v1", "https://pics.example.org:8443/v1",
                "https://other.example.org/v1"):
        seen.clear()
        monkeypatch.setattr(model_catalog, "_last", {})
        image_providers.save_config({"provider": "custom", "base_url": url, "model": "pic-1"})
        __import__("asyncio").run(model_catalog.refresh_image("custom"))
        assert all("authorization" not in r.headers for r in seen), url


# ---- L-6: a listed id Generate would refuse is not offered -------------------- #

def test_l6_ids_generate_would_refuse_are_not_in_the_datalist(env, monkeypatch):
    monkeypatch.setattr(model_catalog, "_last", {})
    image_providers.save_config({"provider": "minimax", "api_key": KEY})
    long_id = "m" * 120
    model_catalog._store("image", "custom", {
        "models": [{"id": "ok-model", "kind": "image"}, {"id": long_id, "kind": "image"},
                   {"id": "a@b", "kind": "image"}],
        "source": "endpoint", "checked_at": "2026-11-11T00:00:00+00:00", "error": None, "note": ""})
    c = _unlocked(env)
    row = next(p for p in c.get(BASE + "/state").json()["providers"] if p["id"] == "custom")
    assert "ok-model" in row["models"] and long_id not in row["models"] and "a@b" not in row["models"]
