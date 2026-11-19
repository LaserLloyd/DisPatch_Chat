"""Fix-pass tests for the available-models round (mock transport only; no paid call)."""
from __future__ import annotations

import base64
import json
import re
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


# ---- device-local draft: the page keys it by this ---------------------------- #

def test_state_carries_a_stable_per_profile_key(world):
    owner = world.create_owner()
    world.person(owner, "Ada", "admin", password="pw-pass-1234")  # scrub-ok: throwaway test password
    ada = world.client()
    assert world.login(ada, name="Ada", password="pw-pass-1234").status_code == 200  # scrub-ok: throwaway test password
    k_owner = owner.get(BASE + "/state").json()["profile_key"]
    k_ada = ada.get(BASE + "/state").json()["profile_key"]
    assert re.fullmatch(r"[0-9a-f]{16}", k_owner) and re.fullmatch(r"[0-9a-f]{16}", k_ada)
    assert k_owner != k_ada, "two profiles must never share a draft"
    assert owner.get(BASE + "/state").json()["profile_key"] == k_owner, "stable across calls"
    assert ada.get(BASE + "/state").json()["profile_key"] == k_ada


def test_profile_key_for_no_owner_is_fixed_and_hides_the_id(world):
    import dispatch_app_imagestudio as b
    assert b._profile_key(None) == "install" and b._profile_key("") == "install"
    k = b._profile_key("abc123")
    assert "abc123" not in k and k == b._profile_key("abc123") and len(k) == 16


# ---- the last generation: GET /last, per profile ----------------------------- #

def _server_world(world, monkeypatch):
    from dataclasses import replace

    import dispatch_app_imagestudio as studio
    from app import image_access
    monkeypatch.setattr(studio, "SETTINGS", replace(studio.SETTINGS, image_server_url="http://rig.example/mcp"))

    async def render(prompt, workflow, ratio):
        return [_png(21_000)]

    monkeypatch.setattr(studio, "_render_server", render)
    owner = world.create_owner()
    ada_id = world.person(owner, "Ada", "admin", password="pw-pass-1234")["id"]  # scrub-ok: throwaway test password
    settings_store.set_many({image_access.SERVER_PROFILES_KEY: [ada_id]})
    ada = world.client()
    assert world.login(ada, name="Ada", password="pw-pass-1234").status_code == 200  # scrub-ok: throwaway test password
    return owner, ada


def test_last_generation_is_recorded_with_every_form_setting(world, monkeypatch):
    owner, _ = _server_world(world, monkeypatch)
    assert owner.get(BASE + "/last").json() == {"last": None}
    r = owner.post(f"{BASE}/generate", json={"prompt": "a lighthouse at dusk", "provider": "workflows",
                                             "model": "wf-b", "ratio": "9:16", "n": 1, "count": 3})
    assert r.status_code == 200, r.text
    last = owner.get(BASE + "/last").json()["last"]
    assert {k: last[k] for k in ("provider", "model", "prompt", "ratio", "n", "seed", "optimizer", "currency")} == {
        "provider": "workflows", "model": "wf-b", "prompt": "a lighthouse at dusk", "ratio": "9:16",
        "n": 3, "seed": None, "optimizer": False, "currency": ""}
    assert last["v"] == 1 and last["at"] > 0
    assert "reference" not in last


def test_one_profile_never_reads_another_profiles_last_generation(world, monkeypatch):
    owner, ada = _server_world(world, monkeypatch)
    assert owner.post(f"{BASE}/generate", json={"prompt": "owner only", "provider": "workflows",
                                                "model": "wf-a", "ratio": "1:1", "n": 1}).status_code == 200
    assert ada.get(BASE + "/last").json() == {"last": None}, "Ada sees nothing of the Owner's"
    assert ada.post(f"{BASE}/generate", json={"prompt": "ada only", "provider": "workflows",
                                              "model": "wf-c", "ratio": "1:1", "n": 1}).status_code == 200
    assert ada.get(BASE + "/last").json()["last"]["prompt"] == "ada only"
    assert owner.get(BASE + "/last").json()["last"]["prompt"] == "owner only"
    # no query parameter can point at another profile's record
    assert ada.get(BASE + "/last", params={"owner": "x", "profile_id": "y"}).json()["last"]["prompt"] == "ada only"


def test_last_generation_needs_a_signed_in_profile_that_may_use_studio(world, monkeypatch):
    _server_world(world, monkeypatch)
    anon = world.client()
    assert anon.get(BASE + "/last").status_code in (401, 403)


def test_a_refused_generate_does_not_replace_the_last_generation(world, monkeypatch):
    import dispatch_app_imagestudio as studio
    owner, _ = _server_world(world, monkeypatch)
    assert owner.post(f"{BASE}/generate", json={"prompt": "kept", "provider": "workflows",
                                                "model": "wf-a", "ratio": "1:1", "n": 1}).status_code == 200
    monkeypatch.setattr(studio, "_take_slot", lambda owner: "too many at once")
    assert owner.post(f"{BASE}/generate", json={"prompt": "refused", "provider": "workflows",
                                                "model": "wf-b", "ratio": "1:1", "n": 1}).status_code == 429
    assert owner.get(BASE + "/last").json()["last"]["prompt"] == "kept"


# ---- the form draft: GET/PUT/DELETE /draft, per profile ---------------------- #

DRAFT = {"v": 1, "provider": "minimax", "model": "image-01", "prompt": "a lighthouse", "ratio": "3:2",
         "seed": "7", "n": 2, "optimizer": True}


def test_draft_round_trip_and_reset(world, monkeypatch):
    owner, _ = _server_world(world, monkeypatch)
    assert owner.get(BASE + "/draft").json() == {"draft": None}
    assert owner.put(BASE + "/draft", json=DRAFT).status_code == 200
    got = owner.get(BASE + "/draft").json()["draft"]
    assert {k: v for k, v in got.items() if k != "rev"} == DRAFT
    assert owner.delete(BASE + "/draft").status_code == 200
    assert owner.get(BASE + "/draft").json() == {"draft": None}


def test_a_stale_save_cannot_overwrite_a_newer_one_or_a_reset(world, monkeypatch):
    owner, _ = _server_world(world, monkeypatch)
    assert owner.put(BASE + "/draft", json={**DRAFT, "prompt": "newer", "rev": 200}).status_code == 200
    assert owner.put(BASE + "/draft", json={**DRAFT, "prompt": "older", "rev": 100}).status_code == 200
    assert owner.get(BASE + "/draft").json()["draft"]["prompt"] == "newer", "the older save lost"
    assert owner.delete(BASE + "/draft", params={"rev": 300}).status_code == 200
    assert owner.put(BASE + "/draft", json={**DRAFT, "prompt": "crossed", "rev": 250}).status_code == 200
    assert owner.get(BASE + "/draft").json() == {"draft": None}, "a save that crossed a Reset does not bring the form back"
    assert owner.put(BASE + "/draft", json={**DRAFT, "prompt": "after", "rev": 400}).status_code == 200
    assert owner.get(BASE + "/draft").json()["draft"]["prompt"] == "after"


def test_a_page_left_open_across_a_profile_switch_cannot_write_the_new_profiles_draft(world, monkeypatch):
    owner, ada = _server_world(world, monkeypatch)
    ada_key = ada.get(BASE + "/state").json()["profile_key"]
    owner_key = owner.get(BASE + "/state").json()["profile_key"]
    assert ada_key != owner_key
    # the page that believes it is the Owner's, now talking with Ada's session
    assert ada.put(BASE + "/draft", params={"pk": owner_key}, json=DRAFT).status_code == 409
    assert ada.delete(BASE + "/draft", params={"pk": owner_key}).status_code == 409
    assert ada.get(BASE + "/draft").json() == {"draft": None}
    assert ada.put(BASE + "/draft", params={"pk": ada_key}, json=DRAFT).status_code == 200


def test_one_profile_never_reads_or_writes_another_profiles_draft(world, monkeypatch):
    owner, ada = _server_world(world, monkeypatch)
    assert owner.put(BASE + "/draft", json=DRAFT).status_code == 200
    assert ada.get(BASE + "/draft").json() == {"draft": None}
    assert ada.put(BASE + "/draft", json={**DRAFT, "prompt": "ada's"},
                   params={"owner": "x", "profile_id": "y"}).status_code == 200
    assert ada.get(BASE + "/draft").json()["draft"]["prompt"] == "ada's"
    assert owner.get(BASE + "/draft").json()["draft"]["prompt"] == "a lighthouse"
    assert ada.delete(BASE + "/draft").status_code == 200
    assert owner.get(BASE + "/draft").json()["draft"]["prompt"] == "a lighthouse"


def test_draft_needs_a_signed_in_profile_and_drops_data_urls(world, monkeypatch):
    owner, _ = _server_world(world, monkeypatch)
    anon = world.client()
    for call in (lambda: anon.get(BASE + "/draft"), lambda: anon.put(BASE + "/draft", json=DRAFT),
                 lambda: anon.delete(BASE + "/draft")):
        assert call().status_code in (401, 403)
    owner.put(BASE + "/draft", json={**DRAFT, "model": "data:image/png;base64,AAAA", "extra": "ignored"})
    got = owner.get(BASE + "/draft").json()["draft"]
    assert got["model"] == "" and "extra" not in got


def test_draft_is_bounded(world, monkeypatch):
    owner, _ = _server_world(world, monkeypatch)
    assert owner.put(BASE + "/draft", json={**DRAFT, "prompt": "x" * 50_000}).status_code == 422
    assert owner.put(BASE + "/draft", json={**DRAFT, "n": 99}).status_code == 422


# ---- the daily spend cap on a paid provider ---------------------------------- #

def _caps(**kw):
    settings_store.set_many({f"connections.image_spend.{k}": v for k, v in kw.items()})


def test_paid_job_over_the_cap_is_refused_before_any_provider_call(env, fake):
    from app import image_spend
    image_spend.reset_for_tests()
    _caps(profile_usd_per_day=0.05, install_usd_per_day=0)
    c = _unlocked(env)
    ok = _gen(c, model="MiniMax-H3-Image", n=2)                       # $0.04 of $0.05
    assert ok.status_code == 200, ok.text
    sent = len(fake.requests)
    r = _gen(c, model="MiniMax-H3-Image", n=1)                        # $0.06 > $0.05
    assert r.status_code == 429, r.text
    assert "$0.05" in r.json()["detail"] and "midnight" in r.json()["detail"]
    assert len(fake.requests) == sent, "no provider call was made for the refused job"
    assert c.get(BASE + "/last").json()["last"]["n"] == 2, "a refused job is not the last generation"
    st = c.get(BASE + "/state").json()["spend"]
    line = next(ln for ln in st["lines"] if ln["unit"] == "usd")
    assert line["used"] == pytest.approx(0.04) and line["cap"] == 0.05


def test_a_failed_paid_job_gives_its_reservation_back(env, fake, monkeypatch):
    from app import image_spend
    image_spend.reset_for_tests()
    _caps(profile_usd_per_day=0.05, install_usd_per_day=0)
    c = _unlocked(env)
    monkeypatch.setattr(image_providers, "transport", httpx.MockTransport(lambda req: httpx.Response(500, json={})))
    assert _gen(c, model="MiniMax-H3-Image", n=2).status_code == 502
    st = c.get(BASE + "/state").json()["spend"]
    assert next(ln for ln in st["lines"] if ln["unit"] == "usd")["used"] == 0
    monkeypatch.setattr(image_providers, "transport", httpx.MockTransport(fake))
    assert _gen(c, model="MiniMax-H3-Image", n=2).status_code == 200      # the whole cap is free again


def test_eighty_percent_notice_rides_the_response_and_state(env, fake):
    from app import image_spend
    image_spend.reset_for_tests()
    _caps(profile_usd_per_day=0.05, install_usd_per_day=0)
    c = _unlocked(env)
    assert "spend_notice" not in _gen(c, model="MiniMax-H3-Image", n=1).json()
    r = _gen(c, model="MiniMax-H3-Image", n=1).json()                 # $0.04 = 80%
    assert r["spend_notice"].startswith("80%")
    assert c.get(BASE + "/state").json()["spend"]["notice"].startswith("80%")


def test_the_image_server_and_a_custom_host_are_never_counted(world, monkeypatch):
    from app import image_spend
    image_spend.reset_for_tests()
    _caps(profile_usd_per_day=0.0001, install_usd_per_day=0.0001, profile_pictures_per_day=1)
    owner, _ = _server_world(world, monkeypatch)
    for _ in range(3):
        assert owner.post(f"{BASE}/generate", json={"prompt": "free", "provider": "workflows",
                                                    "model": "wf-a", "ratio": "1:1", "n": 1}).status_code == 200
    assert next(ln for ln in owner.get(BASE + "/state").json()["spend"]["lines"] if ln["unit"] == "usd")["used"] == 0


def test_parallel_paid_jobs_cannot_overshoot(env, fake):
    import threading

    from app import image_spend
    image_spend.reset_for_tests()
    _caps(profile_usd_per_day=0.10, install_usd_per_day=0)
    c = _unlocked(env)
    codes = []

    def go():
        codes.append(_gen(c, model="MiniMax-H3-Image", n=1).status_code)

    ts = [threading.Thread(target=go) for _ in range(9)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert codes.count(200) == 5 and codes.count(429) == 4, codes     # $0.02 each, $0.10 cap
    assert len(fake.requests) == 5


def test_spend_overview_is_for_admins_only(world, monkeypatch):
    owner, ada = _server_world(world, monkeypatch)
    assert owner.get("/api/image-spend").status_code == 200
    body = owner.get("/api/image-spend").json()
    assert {"day", "resets_at", "install", "profiles"} <= set(body)
    anon = world.client()
    assert anon.get("/api/image-spend").status_code in (401, 403)


def test_chat_pictures_from_an_api_provider_share_the_cap(env, fake, monkeypatch):
    import asyncio

    from app import image_spend
    image_spend.reset_for_tests()
    _caps(profile_usd_per_day=0.006, install_usd_per_day=0)           # image-01 is $0.0035: one fits
    spec = image_jobs.ImageSpec(prompt="a chat picture", ratio="1:1", backend="api")

    async def go():
        be = image_providers.ApiImages()
        first = await be.enqueue(spec, owner="p1")
        await be._runs[first.job_id].task
        before = len(fake.requests)
        with pytest.raises(image_jobs.ImageJobError) as e:
            await be.enqueue(spec, owner="p1")
        assert "midnight" in e.value.message and len(fake.requests) == before
        await be.enqueue(spec, owner="p2")                            # another profile's own cap
        # a failing picture gives its share back
        monkeypatch.setattr(image_providers, "transport", httpx.MockTransport(lambda r: httpx.Response(500, json={})))
        _caps(profile_usd_per_day=0.004)
        bad = await be.enqueue(spec, owner="p3")
        await be._runs[bad.job_id].task
        again = await be.enqueue(spec, owner="p3")
        await be._runs[again.job_id].task

    asyncio.run(go())


def test_a_timeout_after_the_call_keeps_the_reservation_but_a_refusal_gives_it_back(env, fake, monkeypatch):
    from app import image_spend
    image_spend.reset_for_tests()
    _caps(profile_usd_per_day=0.05, install_usd_per_day=0)
    c = _unlocked(env)

    def timeout(req):
        raise httpx.ReadTimeout("slow", request=req)

    monkeypatch.setattr(image_providers, "transport", httpx.MockTransport(timeout))
    assert _gen(c, model="MiniMax-H3-Image", n=1).status_code == 502
    used = next(ln for ln in c.get(BASE + "/state").json()["spend"]["lines"] if ln["unit"] == "usd")["used"]
    assert used == pytest.approx(0.02), "the provider may have billed a call that timed out: it stays counted"
    monkeypatch.setattr(image_providers, "transport", httpx.MockTransport(lambda r: httpx.Response(401, json={})))
    assert _gen(c, model="MiniMax-H3-Image", n=1).status_code == 502
    used = next(ln for ln in c.get(BASE + "/state").json()["spend"]["lines"] if ln["unit"] == "usd")["used"]
    assert used == pytest.approx(0.02), "a refused request bought nothing: its share went back"
