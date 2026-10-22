"""Image Studio's HTTP surface (apps/imagestudio/backend.py).

Every provider call goes to an httpx MockTransport; nothing here reaches a real
(metered) API. The key used is a fake and the tests assert it is never in any
response.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json

import httpx
import pytest
from app import config, image_providers
from tests.test_image_jobs import _png, _say, _thread, _unlocked
from tests.test_image_jobs import env as env

KEY = "sk-test-STUDIO-SECRET-0123456789"
BASE = "/api/apps/imagestudio"
ROUTES_GET = ["/state", "/images", "/chat-images", "/targets"]


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _jpeg() -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), (200, 30, 30)).save(buf, "JPEG", quality=95)
    return buf.getvalue() + b"\x00" * 12_000          # clear the 10 KB floor


class Fake:
    def __init__(self, handler=None):
        self.requests: list[httpx.Request] = []
        self.handler = handler or self.ok

    def ok(self, request):
        body = json.loads(request.content)
        n = body.get("n", 1)
        return httpx.Response(200, json={
            "id": "t", "data": {"image_base64": [_b64(_png(20_000 + i)) for i in range(n)]},
            "metadata": {"success_count": n, "failed_count": 0},
            "base_resp": {"status_code": 0, "status_msg": "success"}})

    def __call__(self, request):
        self.requests.append(request)
        return self.handler(request)

    def body(self, i=0):
        return json.loads(self.requests[i].content)


@pytest.fixture
def provider(monkeypatch, env):
    image_providers.save_config({"provider": "minimax", "api_key": KEY})
    fake = Fake()
    monkeypatch.setattr(image_providers, "transport", httpx.MockTransport(fake))
    yield fake
    monkeypatch.setattr(image_providers, "transport", None)


def _gen(c, **over):
    body = {"prompt": "a silver-haired explorer with a globe", "ratio": "16:9", "n": 1}
    body.update(over)
    return c.post(f"{BASE}/generate", json=body)


# ---------------------------------------------------------------- security

def test_cookieless_and_forged_callers_get_the_403_everywhere(env, provider):
    from app import auth
    auth.set_pin("4321")
    for cookies in ({}, {"lc_session": "forged-cookie"}):
        c = env()
        for k, v in cookies.items():
            c.cookies.set(k, v)
        for path in ROUTES_GET:
            r = c.get(BASE + path)
            assert r.status_code in (401, 403), (path, r.status_code)
            assert "images" not in r.json()
        r = _gen(c)
        assert r.status_code in (401, 403)
        assert provider.requests == [], "a refused caller never reaches the provider"


def test_the_key_never_appears_in_any_response(env, provider):
    c = _unlocked(env)
    texts = [c.get(BASE + p).text for p in ROUTES_GET]
    texts.append(_gen(c, n=2).text)
    texts.append(c.get(BASE + "/images").text)
    for t in texts:
        assert KEY not in t
    assert KEY not in json.dumps(c.get("/api/health").json()) if c.get("/api/health").status_code == 200 else True


def test_the_app_is_operator_only_in_the_manifest():
    from pathlib import Path

    import yaml
    m = yaml.safe_load((Path(__file__).resolve().parents[1] / "app.yaml").read_text())
    assert m["id"] == "imagestudio" and m["safe"] is False


# ---------------------------------------------------------------- state

def test_state_lists_providers_and_defaults_to_the_configured_one(env, provider):
    c = _unlocked(env)
    s = c.get(BASE + "/state").json()
    assert s["default"] == "minimax"
    by = {p["id"]: p for p in s["providers"]}
    assert by["minimax"]["configured"] is True and by["minimax"]["supports"]["reference"] is True
    assert by["minimax"]["supports"]["max_n"] == 9
    assert by["openai"]["configured"] is False
    assert by["openai"]["supports"]["reference"] is False
    assert s["prompt_max"] == 1500 and s["today_count"] == 0 and s["prompts"] == []
    assert "1:1" in s["ratios"]


def test_no_configured_provider_means_an_empty_default(env, monkeypatch):
    for name in ("MINIMAX_API_KEY", "OPENAI_API_KEY", "XAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    c = _unlocked(env)
    s = c.get(BASE + "/state").json()
    assert s["default"] == ""
    r = _gen(c)
    assert r.status_code == 409 and "API key" in r.json()["detail"]


# ---------------------------------------------------------------- generate

def test_generate_sends_one_provider_request_for_n_pictures_and_stores_them(env, provider):
    c = _unlocked(env)
    r = _gen(c, n=3, seed=77, optimizer=True)
    assert r.status_code == 200, r.text
    out = r.json()
    assert len(out["images"]) == 3 and out["asked"] == 3
    assert len(provider.requests) == 1, "one accepted request = one provider call"
    body = provider.body()
    assert body["n"] == 3 and body["seed"] == 77 and body["prompt_optimizer"] is True
    assert body["aspect_ratio"] == "16:9" and body["model"] == "image-01"
    assert provider.requests[0].headers["authorization"] == f"Bearer {KEY}"
    media = config.MEDIA_DIR
    for im in out["images"]:
        assert im["url"].startswith("/media/")
        assert (media / im["url"].rsplit("/", 1)[1]).is_file()
        assert im["prompt"].startswith("a silver-haired")
        assert im["seed"] == 77 and im["provider"] == "minimax"


def test_results_are_listed_newest_last_with_a_per_day_count_and_recent_prompts(env, provider):
    c = _unlocked(env)
    a = _gen(c, prompt="first prompt").json()["images"][0]
    b = _gen(c, prompt="second prompt", n=2).json()["images"]
    rows = c.get(BASE + "/images").json()["images"]
    assert [r["id"] for r in rows] == [a["id"], b[0]["id"], b[1]["id"]]
    s = c.get(BASE + "/state").json()
    assert s["today_count"] == 3 and s["total"] == 3
    assert s["days"][0]["count"] == 3
    assert s["prompts"] == ["second prompt", "first prompt"]
    # The prompt is recorded once per request; repeating it moves it to the top.
    _gen(c, prompt="first prompt")
    assert c.get(BASE + "/state").json()["prompts"][0] == "first prompt"


def test_unsupported_options_are_not_sent_to_an_openai_shape_provider(env, monkeypatch):
    image_providers.save_config({"provider": "openai", "api_key": KEY})

    def ok(request):
        return httpx.Response(200, json={"data": [{"b64_json": _b64(_png())}, {"b64_json": _b64(_png(21_000))}]})
    fake = Fake(ok)
    monkeypatch.setattr(image_providers, "transport", httpx.MockTransport(fake))
    c = _unlocked(env)
    r = _gen(c, n=2, seed=5, optimizer=True)
    assert r.status_code == 200, r.text
    body = fake.body()
    assert body["n"] == 2 and "seed" not in body and "prompt_optimizer" not in body
    assert len(r.json()["images"]) == 2
    assert r.json()["images"][0]["seed"] is None
    # ... and a reference is refused outright rather than silently dropped.
    ref = {"kind": "data_url", "data": "data:image/jpeg;base64," + _b64(_jpeg())}
    assert _gen(c, reference=ref).status_code == 422
    monkeypatch.setattr(image_providers, "transport", None)


def test_n_is_capped_at_what_the_provider_allows(env, monkeypatch):
    image_providers.save_config({"provider": "openai", "api_key": KEY})
    fake = Fake(lambda r: httpx.Response(200, json={"data": [{"b64_json": _b64(_png())}]}))
    monkeypatch.setattr(image_providers, "transport", httpx.MockTransport(fake))
    c = _unlocked(env)
    _gen(c, n=9)
    assert fake.body()["n"] == 4
    monkeypatch.setattr(image_providers, "transport", None)


def test_prompt_and_count_are_validated(env, provider):
    c = _unlocked(env)
    assert _gen(c, prompt="x" * 1501).status_code == 422
    assert _gen(c, prompt="   ").status_code == 422
    assert _gen(c, n=10).status_code == 422
    assert _gen(c, n=0).status_code == 422
    assert _gen(c, seed=-1).status_code == 422
    assert _gen(c, model="bad model name!").status_code == 422
    assert _gen(c, provider="nonesuch").status_code == 422
    # A ratio ImageSpec would reject was a 500 (ValueError escaped the route).
    for ratio in ("wide", "16/9", "1:1:1", "Z" * 5000, "100:1"):
        r = _gen(c, ratio=ratio)
        assert r.status_code == 422, (ratio, r.status_code, r.text)
    assert provider.requests == []


def test_a_provider_error_becomes_a_readable_502_and_stores_nothing(env, provider):
    provider.handler = lambda r: httpx.Response(200, json={"base_resp": {"status_code": 1004, "status_msg": "auth"}})
    c = _unlocked(env)
    r = _gen(c)
    assert r.status_code == 502 and "rejected the API key" in r.json()["detail"]
    assert c.get(BASE + "/images").json()["images"] == []
    assert KEY not in r.text
    provider.handler = lambda r: httpx.Response(429, json={"error": {"message": "slow down"}})
    assert "rate-limiting" in _gen(c).json()["detail"]


def test_fewer_pictures_than_asked_is_reported_not_hidden(env, provider):
    def short(request):
        return httpx.Response(200, json={"data": {"image_base64": [_b64(_png())]},
                                         "base_resp": {"status_code": 0}})
    provider.handler = short
    c = _unlocked(env)
    out = _gen(c, n=4).json()
    assert out["asked"] == 4 and len(out["images"]) == 1


def test_a_second_generate_while_one_runs_is_refused(env, provider):
    from dispatch_app_imagestudio import _lock
    c = _unlocked(env)

    async def hold():
        await _lock.acquire()
    loop = asyncio.new_event_loop()
    try:
        # Simulate "busy" on the module lock itself.
        loop.run_until_complete(hold())
        r = _gen(c)
        assert r.status_code == 409 and "already running" in r.json()["detail"]
        assert provider.requests == []
    finally:
        _lock.release()
        loop.close()


# ---------------------------------------------------------------- reference

def test_reference_by_result_id_goes_out_as_subject_reference_character(env, provider):
    c = _unlocked(env)
    first = _gen(c).json()["images"][0]
    r = _gen(c, reference={"kind": "result", "id": first["id"]})
    assert r.status_code == 200, r.text
    ref = provider.body(1)["subject_reference"]
    assert ref[0]["type"] == "character"
    assert ref[0]["image_file"].startswith("data:image/png;base64,")
    assert r.json()["images"][0]["reference"] is True
    assert _gen(c, reference={"kind": "result", "id": "0" * 32}).status_code == 404


def test_reference_from_a_chat_image_and_an_upload(env, provider):
    c = _unlocked(env)
    tid = _thread(c)
    name = "chatpic.jpg"
    (config.MEDIA_DIR).mkdir(parents=True, exist_ok=True)
    (config.MEDIA_DIR / name).write_bytes(_jpeg())
    r = _gen(c, reference={"kind": "media", "url": f"/media/{name}"})
    assert r.status_code == 200, r.text
    assert provider.body(0)["subject_reference"][0]["image_file"].startswith("data:image/jpeg;base64,")
    up = _gen(c, reference={"kind": "data_url", "data": "data:image/jpeg;base64," + _b64(_jpeg())})
    assert up.status_code == 200
    assert tid  # the thread fixture is unused beyond proving the session works


def test_a_webp_reference_is_converted_and_junk_or_traversal_is_refused(env, provider):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (32, 32), (0, 255, 0)).save(buf, "WEBP")
    c = _unlocked(env)
    ok = _gen(c, reference={"kind": "data_url", "data": "data:image/webp;base64," + _b64(buf.getvalue())})
    assert ok.status_code == 200, ok.text
    assert provider.body(0)["subject_reference"][0]["image_file"].startswith("data:image/png;base64,")
    assert _gen(c, reference={"kind": "data_url", "data": "data:image/png;base64," + _b64(b"not an image")}).status_code == 422
    assert _gen(c, reference={"kind": "data_url", "data": "data:text/plain;base64,AAAA"}).status_code == 422
    for bad in ("/media/../security.yaml", "/media/a/b.png", "https://x/y.png", "/etc/passwd", "/media/"):
        assert _gen(c, reference={"kind": "media", "url": bad}).status_code in (404, 422), bad
    assert _gen(c, reference={"kind": "what"}).status_code == 422
    assert len(provider.requests) == 1, "only the good reference reached the provider"


# ---------------------------------------------------------------- chat / avatar plumbing

def test_chat_images_and_targets_list_what_the_picker_needs(env, provider):
    c = _unlocked(env)
    tid = _thread(c)
    (config.MEDIA_DIR).mkdir(parents=True, exist_ok=True)
    (config.MEDIA_DIR / "pic1.png").write_bytes(_png())
    _say(c, tid, "look [[media:/media/pic1.png|cap]]")
    imgs = c.get(BASE + "/chat-images").json()["images"]
    assert [i["url"] for i in imgs] == ["/media/pic1.png"]
    assert imgs[0]["thread_id"] == tid
    t = c.get(BASE + "/targets").json()
    assert any(x["id"] == tid for x in t["threads"])
    assert any(b["id"] == "main" for b in t["bots"])


def test_send_to_chat_is_the_ordinary_inject_path(env, provider):
    c = _unlocked(env)
    tid = _thread(c)
    img = _gen(c).json()["images"][0]
    for role in ("assistant", "system"):
        r = c.post("/api/inject", json={"thread_id": tid, "role": role, "content": "made in the studio",
                                        "media_url": img["url"], "metadata": {"source": "image-studio", "image_id": img["id"]}})
        assert r.status_code == 200, r.text
        assert r.json()["message"]["media_url"] == img["url"]
    msgs = c.get(f"/api/threads/{tid}/messages").json()
    msgs = msgs["messages"] if isinstance(msgs, dict) else msgs
    assert [m["role"] for m in msgs if m.get("media_url") == img["url"]] == ["assistant", "system"]


def test_the_seed_prompts_start_the_history_and_then_become_ordinary_history(env, provider):
    c = _unlocked(env)
    from dispatch_app_imagestudio import CTX
    (CTX.data_dir / "seed-prompts.json").write_text(json.dumps(["seeded one", "seeded two"]))
    assert c.get(BASE + "/state").json()["prompts"] == ["seeded one", "seeded two"]
    _gen(c, prompt="mine")
    assert c.get(BASE + "/state").json()["prompts"] == ["mine", "seeded one", "seeded two"], \
        "the seeds become ordinary history, newest prompt first"
