"""Civitai image provider (WP1): async workflow, estimate, model resolution, fences.

All HTTP goes to an httpx MockTransport; no test reaches Civitai.
"""
from __future__ import annotations

import json
import re

import httpx
import pytest

from app import config, image_jobs, image_providers as ip, secret_store
from tests.test_image_jobs import _png

BASE = "https://orchestration.civitai.com"
WF = f"{BASE}/v2/consumer/workflows"
KEY = "civ-test-0123456789"
AIR = "urn:air:sdxl:checkpoint:civitai:133005@1759168"


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setenv("DISPATCH_SECRET_STORE", "file")
    secret_store.reset_for_tests()
    monkeypatch.setenv("CIVITAI_API_KEY", KEY)
    clock = {"t": 0.0}
    monkeypatch.setattr(ip, "_monotonic", lambda: clock["t"])

    async def fake_sleep(s):
        clock["t"] += max(s, 0.5)
    monkeypatch.setattr(ip, "_sleep", fake_sleep)
    yield clock
    monkeypatch.setattr(ip, "transport", None)
    secret_store.reset_for_tests()


class Fake:
    def __init__(self, handler):
        self.requests: list[httpx.Request] = []
        self._h = handler
        ip.transport = httpx.MockTransport(self._call)

    def _call(self, req):
        self.requests.append(req)
        return self._h(req)

    def of(self, method):
        return [r for r in self.requests if r.method == method]


def res():
    return ip.resolve({"provider": "civitai"}, studio=True)


def spec(ratio="1:1", prompt="a cat"):
    return image_jobs.ImageSpec(prompt=prompt, ratio=ratio)


def wf(status="succeeded", url="https://blob.civitai.com/a.png", **extra):
    doc = {"id": "wf1", "status": status,
           "steps": [{"output": {"images": [{"available": True, "url": url}]}}]}
    doc.update(extra)
    return doc


def blob_ok(req):
    return httpx.Response(200, content=_png(), headers={"content-type": "image/png"})


def router(post, get=None, put=None, blob=blob_ok):
    def h(req):
        if req.url.host == "blob.civitai.com":
            return blob(req)
        if req.method == "POST":
            return post(req)
        if req.method == "PUT":
            return (put or (lambda r: httpx.Response(200, json={})))(req)
        return get(req)
    return h


# 1 ---------------------------------------------------------------- body
def test_body_exact_16x9_and_clamp():
    body = ip.civitai_body(res(), spec("16:9"), 6, 42, external_id="dp-abc_1")
    assert body == {
        "steps": [{"$type": "imageGen", "input": {
            "engine": "sdcpp", "ecosystem": "sdxl", "operation": "createImage",
            ip.CIVITAI_MODEL_FIELD: AIR, "prompt": "a cat",
            "negativePrompt": ip.CIVITAI_NEGATIVE, "width": 1344, "height": 768,
            "cfgScale": 7, "steps": 25, "quantity": 4, "seed": 42}}],
        "allowMatureContent": True, "externalId": "dp-abc_1"}
    assert "seed" not in ip.civitai_body(res(), spec(), 1, None, external_id="x")["steps"][0]["input"]


def test_submit_has_fresh_valid_external_id_and_bearer():
    f = Fake(router(lambda r: httpx.Response(200, json=wf())))
    import asyncio
    asyncio.run(ip.generate_batch(res(), spec(), n=2, seed=7))
    post = f.of("POST")[0]
    assert post.url.params["wait"] == "55"
    assert post.headers["authorization"] == f"Bearer {KEY}"
    body = json.loads(post.content)
    assert re.fullmatch(r"[A-Za-z0-9_-]+", body["externalId"])
    assert body["steps"][0]["input"]["quantity"] == 2
    assert body["steps"][0]["input"]["seed"] == 7


# 2 ---------------------------------------------------------------- success
async def test_success_blob_has_no_auth():
    f = Fake(router(lambda r: httpx.Response(200, json=wf())))
    out = await ip.generate_batch(res(), spec())
    assert len(out) == 1 and out[0].startswith(b"\x89PNG")
    blob = [r for r in f.requests if r.url.host == "blob.civitai.com"][0]
    assert "authorization" not in blob.headers


# 3 ---------------------------------------------------------------- polling
async def test_202_then_poll_then_done():
    states = iter(["processing", "succeeded"])
    f = Fake(router(lambda r: httpx.Response(202, json=wf("processing", url="")),
                    lambda r: httpx.Response(200, json=wf(next(states)))))
    out = await ip.generate_batch(res(), spec())
    assert out
    assert len(f.of("POST")) == 1
    gets = [r for r in f.of("GET") if r.url.host != "blob.civitai.com"]
    assert len(gets) == 2 and gets[0].url.params["wait"] == "30"


# 4 ---------------------------------------------------------------- deadline
async def test_deadline_cancels():
    f = Fake(router(lambda r: httpx.Response(202, json=wf("processing")),
                    lambda r: httpx.Response(200, json=wf("processing"))))
    with pytest.raises(image_jobs.ImageJobError, match="took too long"):
        await ip.generate_batch(res(), spec())
    puts = f.of("PUT")
    assert len(puts) == 1 and json.loads(puts[0].content) == {"status": "canceled"}


# 4b --------------------------------------------- giving up for another reason
async def test_poll_error_cancels_the_workflow():
    f = Fake(router(lambda r: httpx.Response(202, json=wf("processing")),
                    lambda r: httpx.Response(500, json={"detail": "boom"})))
    with pytest.raises(image_jobs.ImageJobError):
        await ip.generate_batch(res(), spec())
    puts = f.of("PUT")
    assert len(puts) == 1 and puts[0].url.path.endswith("/workflows/wf1")


async def test_cancelled_request_cancels_the_workflow(monkeypatch):
    import asyncio
    f = Fake(router(lambda r: httpx.Response(202, json=wf("processing")),
                    lambda r: httpx.Response(200, json=wf("processing"))))

    async def cancelled(_s):
        raise asyncio.CancelledError
    monkeypatch.setattr(ip, "_sleep", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await ip.generate_batch(res(), spec())
    assert len(f.of("PUT")) == 1


async def test_a_strange_workflow_id_is_refused():
    f = Fake(router(lambda r: httpx.Response(202, json=dict(wf("processing"), id="../../x?y"))))
    with pytest.raises(image_jobs.ImageJobError, match="workflow id"):
        await ip.generate_batch(res(), spec())
    assert not f.of("GET") and not f.of("PUT")


# 5 ---------------------------------------------------------------- terminal failures
@pytest.mark.parametrize("doc,msg", [
    (wf("failed", reason="blocked"), "refused this prompt"),
    (wf("expired"), "gave up on the picture"),
    (wf("canceled"), "out of Buzz"),
    (wf("failed", reason="gpu fell over"), "could not make the picture: gpu fell over"),
])
async def test_terminal_failures(doc, msg):
    Fake(router(lambda r: httpx.Response(200, json=doc)))
    with pytest.raises(image_jobs.ImageJobError, match=msg):
        await ip.generate_batch(res(), spec())


async def test_image_level_block():
    doc = wf()
    doc["steps"][0]["output"]["images"][0]["blockedReason"] = "nsfw"
    Fake(router(lambda r: httpx.Response(200, json=doc)))
    with pytest.raises(image_jobs.ImageJobError, match="refused this prompt"):
        await ip.generate_batch(res(), spec())


async def test_workflow_level_blocked_reason_is_moderation_whatever_it_says():
    # The real verdict arrives as a blockedReason KEY; its value is a code, not the word "blocked".
    Fake(router(lambda r: httpx.Response(200, json=wf("failed", blockedReason="sexual-minor"))))
    with pytest.raises(image_jobs.ImageJobError, match="refused this prompt"):
        await ip.generate_batch(res(), spec())


async def test_one_blocked_picture_keeps_the_rest_of_the_batch():
    # Buzz was spent on all four; one moderated picture must not throw away the other three.
    doc = wf()
    imgs = doc["steps"][0]["output"]["images"]
    imgs.append({"available": True, "url": "https://blob.civitai.com/b.png"})
    imgs.append({"available": False, "blockedReason": "nsfw"})
    f = Fake(router(lambda r: httpx.Response(200, json=doc)))
    blobs = await ip.generate_batch(res(), spec(), n=3)
    assert len(blobs) == 2
    assert [r.url.path for r in f.of("GET") if r.url.host == "blob.civitai.com"] == ["/a.png", "/b.png"]


# 6 ---------------------------------------------------------------- http errors
async def test_http_errors():
    Fake(router(lambda r: httpx.Response(
        400, json={"title": "Bad", "detail": "model not found"})))
    with pytest.raises(image_jobs.ImageJobError, match="model not found"):
        await ip.generate_batch(res(), spec())
    Fake(router(lambda r: httpx.Response(401, json={})))
    with pytest.raises(image_jobs.ImageJobError, match="rejected the API key"):
        await ip.generate_batch(res(), spec())
    Fake(router(lambda r: httpx.Response(429, json={})))
    with pytest.raises(image_jobs.ImageJobError, match="rate-limiting"):
        await ip.generate_batch(res(), spec())


# 7 ---------------------------------------------------------------- expired blob
async def test_expired_blob_refetches_once():
    seen = {"n": 0}

    def blob(req):
        seen["n"] += 1
        if "old" in req.url.path:
            return httpx.Response(403)
        return blob_ok(req)
    f = Fake(router(lambda r: httpx.Response(200, json=wf(url="https://blob.civitai.com/old.png")),
                    lambda r: httpx.Response(200, json=wf(url="https://blob.civitai.com/new.png")),
                    blob=blob))
    out = await ip.generate_batch(res(), spec())
    assert out and len([r for r in f.of("GET") if r.url.host != "blob.civitai.com"]) == 1


async def test_expired_blob_twice():
    Fake(router(lambda r: httpx.Response(200, json=wf()),
                lambda r: httpx.Response(200, json=wf()),
                blob=lambda r: httpx.Response(403)))
    with pytest.raises(image_jobs.ImageJobError, match="link expired"):
        await ip.generate_batch(res(), spec())


# 8 ---------------------------------------------------------------- transport retry
async def test_transport_error_retries_same_external_id():
    calls = []

    def post(req):
        calls.append(json.loads(req.content)["externalId"])
        if len(calls) == 1:
            raise httpx.ConnectError("boom")
        return httpx.Response(200, json=wf())
    Fake(router(post))
    assert await ip.generate_batch(res(), spec())
    assert len(calls) == 2 and calls[0] == calls[1]


async def test_transport_error_twice():
    def post(req):
        raise httpx.ConnectTimeout("slow")
    f = Fake(router(post))
    with pytest.raises(image_jobs.ImageJobError, match="could not be reached"):
        await ip.generate_batch(res(), spec())
    assert len(f.of("POST")) == 2


# 9 ---------------------------------------------------------------- blob hygiene
async def test_blob_too_big_not_image_and_http(monkeypatch):
    monkeypatch.setattr(image_jobs, "FETCH_MAX_BYTES", 100)
    Fake(router(lambda r: httpx.Response(200, json=wf())))
    with pytest.raises(image_jobs.ImageJobError, match="implausibly large"):
        await ip.generate_batch(res(), spec())


async def test_blob_not_image_and_http_scheme():
    Fake(router(lambda r: httpx.Response(200, json=wf()),
                blob=lambda r: httpx.Response(200, content=b"x" * 20000)))
    with pytest.raises(image_jobs.ImageJobError, match="not an image"):
        await ip.generate_batch(res(), spec())
    Fake(router(lambda r: httpx.Response(200, json=wf(url="http://blob.civitai.com/a.png"))))
    with pytest.raises(image_jobs.ImageJobError, match="non-https"):
        await ip.generate_batch(res(), spec())


# 10 --------------------------------------------------------------- estimate
async def test_estimate():
    f = Fake(lambda r: httpx.Response(200, json={"cost": {"total": 10.4}}))
    assert await ip.estimate_civitai(res(), spec(), n=2) == 10
    req = f.requests[0]
    assert req.url.params["whatif"] == "true"
    assert "externalId" not in json.loads(req.content)
    assert len(f.requests) == 1
    Fake(lambda r: httpx.Response(400, json={"detail": "nope"}))
    with pytest.raises(image_jobs.ImageJobError, match="nope"):
        await ip.estimate_civitai(res(), spec())


# 11 --------------------------------------------------------------- civitai_model
async def test_model_air_untouched_no_http():
    f = Fake(lambda r: httpx.Response(500))
    assert await ip.civitai_model(f" {AIR} ") == AIR
    assert f.requests == []


async def test_model_resolution():
    def h(req):
        p = req.url.path
        if req.url.host != "civitai.com":
            return httpx.Response(500)
        if p == "/api/v1/model-versions/1759168":
            return httpx.Response(200, json={"air": AIR})
        if p == "/api/v1/models/133005":
            return httpx.Response(200, json={"modelVersions": [{"id": 1759168}]})
        if p == "/api/v1/model-versions/55":
            return httpx.Response(200, json={"air": "urn:air:sdxl:lora:civitai:1@55"})
        return httpx.Response(404)
    Fake(h)
    assert await ip.civitai_model("1759168") == AIR
    assert await ip.civitai_model("https://civitai.com/models/133005/juggernaut-xl") == AIR
    assert await ip.civitai_model("https://civitai.red/models/133005?modelVersionId=1759168") == AIR
    for bad in ("55", "urn:air:sdxl:lora:civitai:1@55", "urn:air:sd1:checkpoint:civitai:1@2",
                "https://example.com/models/133005", "junk", ""):
        with pytest.raises(ValueError):
            await ip.civitai_model(bad)


# 12 --------------------------------------------------------------- fences
def test_fences(monkeypatch):
    assert ip.api_selected({"provider": "civitai"}) is False
    assert ip.api_selected({"provider": "minimax"}) is True
    assert ip.choose_backend("", server=False, cfg={"provider": "civitai"}) == ""
    with pytest.raises(ValueError, match="Image Studio only"):
        ip.merge_submission({"provider": "civitai"}, {})
    with pytest.raises(image_jobs.ImageJobError, match="Image Studio only"):
        ip.resolve({"provider": "civitai", "api_key_env": "CIVITAI_API_KEY"})
    assert ip.resolve({"provider": "civitai"}, studio=True).key_source == "env"
    assert "civitai" not in {p.id for p in ip.DEFAULT_PRESETS}
    pub = ip.PRESET_BY_ID["civitai"].public()
    assert pub["studio_only"] is True and pub["access"] == "civitai"


def test_key_sources_and_no_borrowing(monkeypatch):
    monkeypatch.delenv("CIVITAI_API_KEY")
    with pytest.raises(image_jobs.ImageJobError, match="needs an API key"):
        ip.resolve({"provider": "civitai", "api_key": "stolen"}, studio=True)
    secret_store.put(ip.CIVITAI_KEY_REF, "civ-stored-key-1")
    r = ip.resolve({"provider": "civitai", "api_key": "stolen",
                    "base_url": "https://evil.example"}, studio=True)
    assert r.key_source == "stored" and r.api_key == "civ-stored-key-1"
    assert r.base_url == BASE

    def boom(*a):
        raise AssertionError("shared_chat_key consulted")
    monkeypatch.setattr(ip, "shared_chat_key", boom)
    ip.resolve({"provider": "civitai"}, studio=True)


def test_supports_and_max_batch():
    r = res()
    assert ip.max_batch(r) == 4
    assert ip.supports(r) == {"reference": False, "optimizer": False,
                              "seed": True, "max_n": 4}


def test_size_nearest():
    assert ip.civitai_size("1:1") == (1024, 1024)
    assert ip.civitai_size("9:16") == (768, 1344)
    assert ip.civitai_size("bogus") == (1024, 1024)


# live 2026-11-02 ------------------------------------------------- blob redirect
# Civitai's picture URL answers 301 with a RELATIVE Location on the same host;
# the downloader used to read the empty 301 body as the picture ("empty response").
async def test_blob_relative_301_is_followed():
    def blob(req):
        if req.url.path == "/a.png":
            return httpx.Response(301, headers={"location": "/signed/a.png?sig=1"})
        return blob_ok(req)
    f = Fake(router(lambda r: httpx.Response(200, json=wf()), blob=blob))
    out = await ip.generate_batch(res(), spec())
    assert out[0].startswith(b"\x89PNG")
    hops = [r for r in f.requests if r.url.host == "blob.civitai.com"]
    assert [r.url.path for r in hops] == ["/a.png", "/signed/a.png"]
    assert all("authorization" not in r.headers for r in hops)


async def test_blob_redirect_to_plain_http_is_refused():
    def blob(req):
        return httpx.Response(302, headers={"location": "http://127.0.0.1/x.png"})
    Fake(router(lambda r: httpx.Response(200, json=wf()), blob=blob))
    with pytest.raises(image_jobs.ImageJobError, match="non-https"):
        await ip.generate_batch(res(), spec())


async def test_blob_redirect_loop_is_bounded():
    def blob(req):
        return httpx.Response(301, headers={"location": "/again.png"})
    f = Fake(router(lambda r: httpx.Response(200, json=wf()), blob=blob))
    with pytest.raises(image_jobs.ImageJobError, match="redirect"):
        await ip.generate_batch(res(), spec())
    assert len([r for r in f.requests if r.url.host == "blob.civitai.com"]) <= ip.DOWNLOAD_MAX_REDIRECTS + 1
