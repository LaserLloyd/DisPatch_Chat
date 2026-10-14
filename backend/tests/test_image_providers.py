"""API image providers (2.3.7): OpenAI-shape and MiniMax behind image_jobs.

Every provider call here goes to an httpx MockTransport -- no test reaches a
real (metered) API.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import stat
from dataclasses import replace

import httpx
import pytest

from app import config, image_jobs, image_providers, main
from tests.test_image_jobs import _messages, _png, _say, _thread, _unlocked
from tests.test_image_jobs import env as _jobs_env


@pytest.fixture
def env(_jobs_env, monkeypatch):
    """The image-jobs fixture, but with NO MCP server: only the API path."""
    monkeypatch.setattr(main, "SETTINGS", replace(
        main.SETTINGS, clawforge_url="", image_jobs_enabled=False,
        image_jobs_flag="auto"))
    monkeypatch.setattr(image_providers, "backend", image_providers.ApiImages())
    yield _jobs_env
    monkeypatch.setattr(image_providers, "transport", None)


class Fake:
    """A fake provider: records requests, answers from a handler."""

    def __init__(self, handler):
        self.requests: list[httpx.Request] = []
        self._handler = handler

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._handler(request)

    def body(self, i: int = 0) -> dict:
        return json.loads(self.requests[i].content)


def _install(monkeypatch, handler) -> Fake:
    fake = Fake(handler)
    monkeypatch.setattr(image_providers, "transport", httpx.MockTransport(fake))
    return fake


def _b64png() -> str:
    return base64.b64encode(_png()).decode()


def _openai_ok(request):
    return httpx.Response(200, json={"created": 1, "data": [{"b64_json": _b64png()}]})


def _minimax_ok(request):
    return httpx.Response(200, json={
        "id": "t", "data": {"image_base64": [_b64png()]},
        "metadata": {"success_count": 1, "failed_count": 0},
        "base_resp": {"status_code": 0, "status_msg": "success"}})


def _save(provider, **kw):
    cfg = {"provider": provider, "base_url": "", "model": "", "api_key": "",
           "api_key_env": ""} | kw
    image_providers.save_config(cfg)


def _drive(n=3):
    async def go():
        for _ in range(n):
            await main._image_job_sweep()
            await asyncio.sleep(0.05)
    asyncio.run(go())


# --------------------------------------------------------------------------- #
# Wire shapes
# --------------------------------------------------------------------------- #


def test_openai_shape_request_and_b64_reply(env, monkeypatch):
    fake = _install(monkeypatch, _openai_ok)
    _save("openai", api_key="sk-test")
    res = image_providers.resolve()
    data = asyncio.run(image_providers.generate(
        res, image_jobs.ImageSpec(prompt="a teapot", ratio="16:9")))
    assert data.startswith(b"\x89PNG")
    req = fake.requests[0]
    assert str(req.url) == "https://api.openai.com/v1/images/generations"
    assert req.headers["authorization"] == "Bearer sk-test"
    assert fake.body() == {"model": "gpt-image-1", "prompt": "a teapot", "n": 1,
                           "size": "1536x1024"}


def test_grok_preset_sends_no_size_and_follows_a_url_reply(env, monkeypatch):
    def handler(request):
        if request.url.path.endswith("/images/generations"):
            return httpx.Response(200, json={"data": [{"url": "https://cdn.example/x.png"}]})
        return httpx.Response(200, content=_png())
    fake = _install(monkeypatch, handler)
    _save("xai", api_key="xai-test")
    data = asyncio.run(image_providers.generate(
        image_providers.resolve(), image_jobs.ImageSpec(prompt="a fox", ratio="2:3")))
    assert data.startswith(b"\x89PNG")
    assert str(fake.requests[0].url) == "https://api.x.ai/v1/images/generations"
    assert fake.body() == {"model": "grok-imagine-image", "prompt": "a fox", "n": 1}
    assert str(fake.requests[1].url) == "https://cdn.example/x.png"
    # The key never travels to the download host.
    assert "authorization" not in fake.requests[1].headers


def test_minimax_shape(env, monkeypatch):
    fake = _install(monkeypatch, _minimax_ok)
    _save("minimax", api_key="mm-test")
    data = asyncio.run(image_providers.generate(
        image_providers.resolve(),
        image_jobs.ImageSpec(prompt="p" * 1800, ratio="5:4")))
    assert data.startswith(b"\x89PNG")
    assert str(fake.requests[0].url) == "https://api.minimax.io/v1/image_generation"
    body = fake.body()
    assert body["model"] == "image-01"
    assert len(body["prompt"]) == 1500          # MiniMax's documented cap
    assert body["aspect_ratio"] == "4:3"        # nearest documented ratio
    assert body["response_format"] == "base64" and body["n"] == 1


@pytest.mark.parametrize("code,needle", [(1004, "rejected the API key"),
                                         (1008, "balance"), (1026, "sensitive")])
def test_minimax_status_codes_read_as_sentences(env, monkeypatch, code, needle):
    _install(monkeypatch, lambda r: httpx.Response(200, json={
        "base_resp": {"status_code": code, "status_msg": "x"}}))
    _save("minimax", api_key="mm")
    with pytest.raises(image_jobs.ImageJobError) as e:
        asyncio.run(image_providers.generate(
            image_providers.resolve(), image_jobs.ImageSpec(prompt="x")))
    assert needle in e.value.message


@pytest.mark.parametrize("status,needle", [(401, "rejected the API key"),
                                           (429, "rate-limiting"), (400, "HTTP 400: bad size")])
def test_http_errors_read_as_sentences(env, monkeypatch, status, needle):
    _install(monkeypatch, lambda r: httpx.Response(
        status, json={"error": {"message": "bad size"}}))
    _save("openai", api_key="sk")
    with pytest.raises(image_jobs.ImageJobError) as e:
        asyncio.run(image_providers.generate(
            image_providers.resolve(), image_jobs.ImageSpec(prompt="x")))
    assert needle in e.value.message


def test_bytes_that_are_not_an_image_are_refused(env, monkeypatch):
    _install(monkeypatch, lambda r: httpx.Response(200, json={
        "data": [{"b64_json": base64.b64encode(b'{"error":"nope"}').decode()}]}))
    _save("openai", api_key="sk")
    with pytest.raises(image_jobs.ImageJobError, match="not an image"):
        asyncio.run(image_providers.generate(
            image_providers.resolve(), image_jobs.ImageSpec(prompt="x")))


def test_openai_size_mapping():
    f = image_providers.openai_size
    assert f("", None, None, "gpt-image-1") == "1024x1024"
    assert f("9:16", None, None, "gpt-image-1") == "1024x1536"
    assert f("16:9", None, None, "dall-e-3") == "1792x1024"
    assert f("", 800, 800, "gpt-image-1") == "1024x1024"


# --------------------------------------------------------------------------- #
# Config and keys
# --------------------------------------------------------------------------- #


def test_config_file_is_owner_only_and_the_key_never_comes_back(env):
    c = _unlocked(env)
    r = c.put("/api/llm/image", json={"provider": "minimax", "api_key": "mm-secret"})
    assert r.status_code == 200, r.text
    assert "mm-secret" not in r.text
    assert r.json()["config"]["has_key"] is True
    mode = stat.S_IMODE(os.stat(image_providers.config_path()).st_mode)
    assert mode == 0o600
    # A blank key on re-save keeps the stored one…
    r = c.put("/api/llm/image", json={"provider": "minimax", "model": "image-01"})
    assert image_providers.load_config()["api_key"] == "mm-secret"
    # …but a provider switch does not carry it across.
    r = c.put("/api/llm/image", json={"provider": "openai", "api_key": "sk-2"})
    assert image_providers.load_config()["api_key"] == "sk-2"
    assert "sk-2" not in c.get("/api/llm/image").text


def test_a_config_that_cannot_work_is_refused(env):
    c = _unlocked(env)
    r = c.put("/api/llm/image", json={"provider": "openai"})
    assert r.status_code == 400 and "need an API key" in r.json()["detail"]
    assert c.put("/api/llm/image", json={"provider": "dalle"}).status_code == 422
    r = c.put("/api/llm/image", json={"provider": "clawforge"})
    assert r.status_code == 422          # no MCP server in this fixture


def test_the_env_variable_wins_and_is_name_checked(env, monkeypatch):
    monkeypatch.setenv("MY_IMG_API_KEY", "from-env")
    _save("openai", api_key="stored", api_key_env="MY_IMG_API_KEY")
    res = image_providers.resolve()
    assert (res.api_key, res.key_source) == ("from-env", "env")
    with pytest.raises(ValueError):
        image_providers.merge_submission({"provider": "openai",
                                          "api_key_env": "DISPATCH_PIN"}, {})


def test_a_chat_key_for_the_same_host_is_shared(env):
    bots = config.load_bots()
    atlas = next(b for b in bots if b.id == "Atlas")
    atlas = replace(atlas, advisor={"providers": [
        {"provider": "minimax", "base_url": "https://api.minimax.io/v1",
         "model": "MiniMax-M3", "api_key": "chat-key"}]})
    config.upsert_bot(atlas)
    _save("minimax")
    res = image_providers.resolve()
    assert (res.api_key, res.key_source) == ("chat-key", "shared")
    # A different host never borrows it.
    _save("openai")
    with pytest.raises(image_jobs.ImageJobError, match="need an API key"):
        image_providers.resolve()


def test_routes_are_operator_only(env):
    from app import auth
    auth.set_pin("4321")
    c = env()                     # no session cookie, PIN set
    for method, path in [("get", "/api/llm/image"), ("put", "/api/llm/image"),
                         ("post", "/api/llm/image/test"),
                         ("post", "/api/llm/quicksetup"),
                         ("get", "/api/plugins/catalog"),
                         ("post", "/api/guide/seed?name=set-up-add-ons")]:
        kw = {"json": {}} if method in ("put", "post") else {}
        assert getattr(c, method)(path, **kw).status_code in (401, 403), path
    assert main._decoy_blocked("POST", "/api/guide/seed")
    assert main._decoy_blocked("GET", "/api/llm/image")


def test_the_test_button_makes_one_generation_and_stores_nothing(env, monkeypatch):
    fake = _install(monkeypatch, _minimax_ok)
    c = _unlocked(env)
    r = c.post("/api/llm/image/test", json={"provider": "minimax", "api_key": "mm"})
    assert r.status_code == 200 and r.json()["ok"] is True, r.text
    assert r.json()["preview"].startswith("data:image/png;base64,")
    assert len(fake.requests) == 1
    assert not image_providers.config_path().exists(), "Test must not save"


# --------------------------------------------------------------------------- #
# Routing and the worker
# --------------------------------------------------------------------------- #


def test_choose_backend_rules():
    cb = image_providers.choose_backend
    assert cb("", clawforge=True, cfg={}) == "clawforge"     # pre-2.3.7
    assert cb("wf", clawforge=True, cfg={}) == "clawforge"
    assert cb("", clawforge=False, cfg={}) == ""
    api = {"provider": "minimax"}
    assert cb("", clawforge=True, cfg=api) == "api"
    assert cb("wf", clawforge=True, cfg=api) == "clawforge"  # a workflow keeps the rig
    assert cb("wf", clawforge=False, cfg=api) == "api"
    assert cb("", clawforge=True, cfg={"provider": "none"}) == ""
    assert cb("wf", clawforge=True, cfg={"provider": "none"}) == "clawforge"


def test_an_old_row_without_a_backend_reads_back_as_the_rig():
    blob = json.dumps({"prompt": "x", "workflow": "", "ratio": "", "width": None,
                       "height": None, "negative": "", "caption": "", "priority": 1})
    spec = image_jobs.ImageSpec.from_json(blob)
    assert spec.backend == ""
    assert spec.to_json() == json.dumps(json.loads(blob), sort_keys=True)
    api = image_jobs.ImageSpec(prompt="x", backend="api")
    assert image_jobs.ImageSpec.from_json(api.to_json()).backend == "api"


def test_a_marker_becomes_a_picture_through_the_api(env, monkeypatch):
    fake = _install(monkeypatch, _minimax_ok)
    _save("minimax", api_key="mm")
    c = env()
    tid = _thread(c)
    _say(c, tid, "Here. [[pic:a blue ceramic teapot|Tea]] Enjoy.")
    rows = _messages(c, tid)
    placeholder = rows[-1]
    assert placeholder["metadata"]["kind"] == "image_job"
    _drive()
    row = next(m for m in _messages(c, tid) if m["id"] == placeholder["id"])
    assert row["metadata"]["status"] == "done", row
    assert "[[media:/media/" in row["content"]
    assert len(fake.requests) == 1, "exactly one generation per marker"


def test_a_provider_failure_becomes_a_visible_failure(env, monkeypatch):
    _install(monkeypatch, lambda r: httpx.Response(401, json={}))
    _save("openai", api_key="bad")
    c = env()
    tid = _thread(c)
    _say(c, tid, "[[pic:a teapot]]")
    _drive()
    row = _messages(c, tid)[-1]
    assert row["metadata"]["status"] == "failed"
    assert "rejected the API key" in row["content"]


def test_a_restart_fails_an_api_job_out_loudly(env, monkeypatch):
    _install(monkeypatch, _minimax_ok)
    _save("minimax", api_key="mm")
    c = env()
    tid = _thread(c)
    _say(c, tid, "[[pic:a teapot]]")
    asyncio.run(main._image_job_sweep())          # submitted
    # A new process: the in-memory task is gone.
    monkeypatch.setattr(image_providers, "backend", image_providers.ApiImages())
    _drive(1)
    row = _messages(c, tid)[-1]
    assert row["metadata"]["status"] == "failed"
    assert "restarted" in row["content"]


def test_with_no_provider_and_no_rig_a_marker_is_stripped_and_nothing_starts(env):
    c = env()
    tid = _thread(c)
    _say(c, tid, "Look [[pic:a teapot]] there.")
    rows = _messages(c, tid)
    assert "[[pic" not in rows[0]["content"]
    assert not [m for m in rows if (m.get("metadata") or {}).get("kind") == "image_job"]
    # One quiet sub row says why, instead of the request vanishing.
    (note,) = rows[1:]
    assert note["metadata"].get("sub") is True
    assert "needs an image generator" in note["content"]


def test_health_shows_the_api_provider_without_secrets(env):
    _save("minimax", api_key="mm-secret")
    c = _unlocked(env)
    body = c.get("/api/health").text
    assert "mm-secret" not in body
    assert '"image_api"' in body
