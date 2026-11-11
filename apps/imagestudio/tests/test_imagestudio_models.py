"""Image Studio's model list: catalog rows and POST /models/refresh (mock transport only)."""
from __future__ import annotations

import json

import httpx
import pytest

from app import image_providers, model_catalog

BASE = "/api/apps/imagestudio"
KEY = "sk-studio-models-SECRET-0123456789"   # scrub-ok: test fixture


@pytest.fixture(autouse=True)
def _iso(monkeypatch):
    monkeypatch.setattr(model_catalog, "_last", {})
    seen: list[httpx.Request] = []

    def handler(req):
        seen.append(req)
        if req.url.host.endswith("x.ai"):
            return httpx.Response(200, json={"models": [{"id": "grok-imagine-image"}]})
        return httpx.Response(200, json={"data": []})
    monkeypatch.setattr(image_providers, "transport", httpx.MockTransport(handler))
    for n in ("OPENAI_API_KEY", "XAI_API_KEY", "CIVITAI_API_KEY"):
        monkeypatch.delenv(n, raising=False)
    monkeypatch.setenv("MINIMAX_API_KEY", KEY)
    yield seen


def _minimax(c):
    s = c.get(BASE + "/state").json()
    return s, next(p for p in s["providers"] if p["id"] == "minimax")


def test_rows_carry_h3_and_source_fields(world):
    owner = world.create_owner()
    s, mm = _minimax(owner)
    assert mm["models"][:4] == ["MiniMax-H3-Image", "MiniMax-H3-Image-Highspeed",
                                "image-01", "image-01-live"]
    assert mm["model"] == "image-01" and "image-01" in mm["models"]
    assert mm["models_source"] == "documented" and mm["models_note"]
    sup = mm["supports_by_model"]
    assert sup["MiniMax-H3-Image"]["seed"] is False and sup["MiniMax-H3-Image"]["reference"] is False
    assert sup["MiniMax-H3-Image"]["max_n"] == 10
    assert sup["image-01"]["seed"] is True and sup["image-01"]["max_n"] == 9
    assert mm["prompt_max_by_model"]["image-01"] == 1500
    assert mm["prompt_max_by_model"]["MiniMax-H3-Image"] == 4000
    assert mm["prompt_max"] == 1500
    assert s["can_refresh_models"] is True
    assert KEY not in json.dumps(s)


def test_refresh_is_403_without_manage_connections(world, _iso):
    owner = world.create_owner()
    world.person(owner, "Nia", "member", pin="6666", denies=["manage_connections"])
    nia = world.client()
    assert world.login(nia, name="Nia", pin="6666").status_code == 200
    assert nia.get(BASE + "/state").json()["can_refresh_models"] is False
    r = nia.post(BASE + "/models/refresh")
    assert r.status_code == 403 and _iso == []


def test_refresh_ok_for_owner_and_never_leaks_the_key(world, _iso, monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", KEY)
    owner = world.create_owner()
    r = owner.post(BASE + "/models/refresh")
    assert r.status_code == 200, r.text
    body = r.json()
    assert KEY not in r.text
    assert {"providers", "default", "results"} <= set(body)
    assert body["results"]["xai"]["count"] >= 1 and body["results"]["xai"]["source"] == "endpoint"
    assert body["results"]["minimax"]["source"] == "documented"
    assert any(req.url.host.endswith("x.ai") for req in _iso)
