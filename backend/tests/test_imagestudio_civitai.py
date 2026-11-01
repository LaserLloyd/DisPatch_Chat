"""Image Studio x Civitai (WP4): generate, model resolution, estimate, hard lines, key hygiene.

All HTTP goes to one httpx MockTransport; nothing reaches Civitai.

Run: cd backend && uv run pytest tests/test_imagestudio_civitai.py
"""
from __future__ import annotations

import json

import httpx
import pytest
from privacy_fixtures import _png

from app import image_providers

BASE = "/api/apps/imagestudio"
AIR = "urn:air:sdxl:checkpoint:civitai:133005@1759168"
KEY = "civ-test-STUDIO-0123456789"


class Rig:
    def __init__(self):
        self.requests: list[httpx.Request] = []

    def orch(self):
        return [r for r in self.requests if r.url.host == "orchestration.civitai.com"]

    def __call__(self, req):
        self.requests.append(req)
        h = req.url.host
        if h == "orchestration.civitai.com":
            if req.url.params.get("whatif") == "true":
                return httpx.Response(200, json={"cost": {"total": 10}})
            return httpx.Response(200, json={"id": "wf", "status": "succeeded", "steps": [
                {"output": {"images": [{"available": True, "url": "https://blob.civitai.com/a.png"}]}}]})
        if h == "blob.civitai.com":
            return httpx.Response(200, content=_png(), headers={"content-type": "image/png"})
        if h == "civitai.com" and req.url.path == "/api/v1/model-versions/1759168":
            return httpx.Response(200, json={"air": AIR})
        return httpx.Response(404)


@pytest.fixture
def studio(world, monkeypatch):
    owner = world.create_owner(world.client())
    monkeypatch.setenv("CIVITAI_API_KEY", KEY)
    rig = Rig()
    monkeypatch.setattr(image_providers, "transport", httpx.MockTransport(rig))
    return owner, rig


def _gen(c, **kw):
    b = {"prompt": "a silver lighthouse", "ratio": "1:1", "n": 1, "provider": "civitai"}
    b.update(kw)
    return c.post(f"{BASE}/generate", json=b)


def test_generate_stores_rows_with_provider_and_air(studio):
    c, rig = studio
    r = _gen(c)
    assert r.status_code == 200, r.text
    row = r.json()["images"][0]
    assert row["provider"] == "civitai" and row["model"] == AIR
    sent = json.loads(rig.orch()[0].content)
    assert sent["steps"][0]["input"][image_providers.CIVITAI_MODEL_FIELD] == AIR
    assert KEY not in r.text


def test_a_pasted_model_link_resolves(studio):
    c, rig = studio
    r = _gen(c, model="https://civitai.com/models/133005?modelVersionId=1759168")
    assert r.status_code == 200, r.text
    assert r.json()["images"][0]["model"] == AIR


def test_a_lora_or_junk_is_422_before_any_generation(studio):
    c, rig = studio
    for bad in ("urn:air:sdxl:lora:civitai:1@55", "junk", "https://example.com/models/1"):
        assert _gen(c, model=bad).status_code == 422, bad
    assert rig.orch() == []


def test_hard_line_prompt_is_422_on_generate_and_estimate_with_no_calls(studio):
    c, rig = studio
    from app import image_guard
    bad = None
    for cand in ("a nude child", "naked minor", "sexual photo of Taylor Swift", "nude photo of Elon Musk"):
        try:
            image_guard.check_prompt(cand)
        except image_guard.ImageGuardError:
            bad = cand
            break
    assert bad, "no hard-line sample tripped the guard"
    assert _gen(c, prompt=bad).status_code == 422
    assert c.post(f"{BASE}/estimate", json={"prompt": bad, "provider": "civitai"}).status_code == 422
    assert rig.requests == []


def test_estimate_is_free_and_leaves_no_trace(studio, world):
    from app import config
    c, rig = studio
    r = c.post(f"{BASE}/estimate", json={"prompt": "a cat", "provider": "civitai", "n": 2})
    assert r.status_code == 200 and r.json() == {"buzz": 10, "n": 2}
    reqs = rig.orch()
    assert len(reqs) == 1 and reqs[0].url.params["whatif"] == "true"
    assert "externalId" not in json.loads(reqs[0].content)
    assert c.get(BASE + "/images").json()["images"] == []
    assert not list((config.MEDIA_DIR).glob("*")) if config.MEDIA_DIR.exists() else True
    assert c.get(BASE + "/state").json()["total"] == 0


def test_state_configured_follows_the_key(studio, monkeypatch):
    c, _rig = studio
    row = next(p for p in c.get(BASE + "/state").json()["providers"] if p["id"] == "civitai")
    assert row["configured"] is True and row["supports"]["seed"] is True and row["supports"]["max_n"] == 4
    monkeypatch.delenv("CIVITAI_API_KEY")
    row = next(p for p in c.get(BASE + "/state").json()["providers"] if p["id"] == "civitai")
    assert row["configured"] is False
    assert _gen(c).status_code == 409


def test_estimate_refuses_a_provider_without_one(studio):
    c, rig = studio
    image_providers.save_config({"provider": "minimax", "api_key": "sk-test-X-0123456789"})
    r = c.post(f"{BASE}/estimate", json={"prompt": "a cat", "provider": "minimax"})
    assert r.status_code == 422 and rig.requests == []
