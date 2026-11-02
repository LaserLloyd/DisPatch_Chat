"""Image Studio x Civitai: Buzz type, Buzz balance, model browser.

All HTTP goes to one httpx MockTransport; nothing reaches Civitai and no Buzz is spent.

Run: cd backend && uv run pytest tests/test_imagestudio_civitai_browse.py
"""
from __future__ import annotations

import base64
import json

import httpx
import pytest
from privacy_fixtures import _png

from app import image_access, image_jobs, image_providers, settings_store
from app.image_providers import civitai_body, civitai_top_models

BASE = "/api/apps/imagestudio"
AIR = "urn:air:sdxl:checkpoint:civitai:133005@1759168"
CIV_KEY = "civ-test-BROWSE-0123456789"
MM_KEY = "sk-test-BROWSE-0123456789"
SCOPE_BODY = {"error": {"json": {"message": "Your API key does not have the required scope for this action",
                                 "code": -32003, "data": {"code": "FORBIDDEN", "httpStatus": 403}}}}


def _item(mid, vid, name="Cool XL", *, gen=True, nsfw=False, base="Illustrious", dl=286000, likes=900):
    return {"id": mid, "name": name, "nsfw": nsfw, "supportsGeneration": gen,
            "stats": {"downloadCount": dl, "thumbsUpCount": likes},
            "modelVersions": [{"id": vid, "baseModel": base, "supportsGeneration": False}]}


class Rig:
    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.balance = lambda req: httpx.Response(
            200, json={"result": {"data": {"json": {"yellow": 1234, "blue": 56}}}})
        self.models = lambda req: httpx.Response(200, json={"items": [_item(11, 22)], "metadata": {}})

    def on(self, host, path=None):
        return [r for r in self.requests if r.url.host == host and (path is None or r.url.path == path)]

    def orch(self):
        return self.on("orchestration.civitai.com")

    def __call__(self, req):
        self.requests.append(req)
        host = req.url.host
        if host == "orchestration.civitai.com":
            if req.method == "POST" and req.url.params.get("whatif") == "true":
                return httpx.Response(200, json={"cost": {"total": 10}})
            return httpx.Response(200, json={"id": "wf", "status": "succeeded", "steps": [
                {"output": {"images": [{"available": True, "url": "https://blob.civitai.com/a.png"}]}}]})
        if host == "blob.civitai.com":
            return httpx.Response(200, content=_png(), headers={"content-type": "image/png"})
        if host == "civitai.com" and req.url.path == "/api/trpc/buzz.getBuzzAccount":
            return self.balance(req)
        if host == "civitai.com" and req.url.path == "/api/v1/models":
            return self.models(req)
        if host == "civitai.com" and req.url.path == "/api/v1/model-versions/1759168":
            return httpx.Response(200, json={"air": AIR})
        if "minimax" in host or "api" in host:
            n = json.loads(req.content).get("n", 1)
            return httpx.Response(200, json={
                "id": "t", "data": {"image_base64": [base64.b64encode(_png()).decode()] * n},
                "metadata": {"success_count": n, "failed_count": 0},
                "base_resp": {"status_code": 0, "status_msg": "success"}})
        return httpx.Response(404)


@pytest.fixture
def studio(world, monkeypatch):
    from agents_world import sibling
    owner = world.create_owner(world.client())
    image_providers.save_config({"provider": "minimax", "api_key": MM_KEY})
    monkeypatch.setenv("CIVITAI_API_KEY", CIV_KEY)
    rig = Rig()
    monkeypatch.setattr(image_providers, "transport", httpx.MockTransport(rig))
    image_providers._browse_cache.clear()
    import sys

    from app import apps_loader
    isb = sys.modules[apps_loader._module_name("imagestudio")]
    isb._balance_last.clear()

    async def no_sleep(_s):
        return None
    monkeypatch.setattr(image_providers, "_sleep", no_sleep)
    out = {"owner": owner, "rig": rig, "isb": isb}
    uma = world.person(owner, "Uma", "user", pin="5555")
    c = sibling(world, owner, ("203.0.113.210", 49310))
    assert world.login(c, profile_id=uma["id"], pin="5555").status_code == 200
    out["Uma"] = c
    yield out
    image_providers._browse_cache.clear()


def _body(**kw):
    b = {"prompt": "a lighthouse at dusk", "ratio": "1:1", "n": 1, "provider": "civitai"}
    b.update(kw)
    return b


# ---------------------------------------------------------------- currency
def _res():
    return image_providers.resolve({"provider": "civitai"}, studio=True)


def test_body_has_currencies_only_when_set(monkeypatch):
    monkeypatch.setenv("CIVITAI_API_KEY", CIV_KEY)
    spec = image_jobs.ImageSpec(prompt="a cat", ratio="1:1")
    assert "currencies" not in civitai_body(_res(), spec, 1, None, external_id=None)
    assert civitai_body(_res(), spec, 1, None, external_id=None, currency="yellow")["currencies"] == ["yellow"]
    assert civitai_body(_res(), spec, 1, None, external_id="x", currency="blue")["currencies"] == ["blue"]
    with pytest.raises(ValueError):
        civitai_body(_res(), spec, 1, None, external_id=None, currency="green")


def test_estimate_and_generate_pass_the_currency(studio):
    c, rig = studio["owner"], studio["rig"]
    r = c.post(f"{BASE}/estimate", json=_body(currency="yellow"))
    assert r.status_code == 200, r.text
    assert json.loads(rig.orch()[0].content)["currencies"] == ["yellow"]
    r = c.post(f"{BASE}/generate", json=_body(currency="blue"))
    assert r.status_code == 200, r.text
    sent = json.loads(rig.orch()[1].content)
    assert sent["currencies"] == ["blue"]
    assert r.json()["images"][0]["currency"] == "blue"
    # nothing chosen: no field sent, nothing recorded
    r = c.post(f"{BASE}/generate", json=_body())
    assert "currencies" not in json.loads(rig.orch()[2].content)
    assert r.json()["images"][0]["currency"] is None


def test_a_bad_currency_is_422_before_any_call(studio):
    c, rig = studio["owner"], studio["rig"]
    for path in ("generate", "estimate"):
        r = c.post(f"{BASE}/{path}", json=_body(currency="green"))
        assert r.status_code == 422, path
    assert rig.orch() == []


def test_other_providers_ignore_a_currency(studio):
    c, rig = studio["owner"], studio["rig"]
    r = c.post(f"{BASE}/generate", json=_body(provider="minimax", currency="yellow"))
    assert r.status_code == 200, r.text
    assert rig.orch() == []
    assert r.json()["images"][0]["currency"] is None


def test_state_lists_the_currencies(studio):
    row = next(p for p in studio["owner"].get(BASE + "/state").json()["providers"] if p["id"] == "civitai")
    assert row["currencies"] == ["blue", "yellow"] and row["browse"] is True


# ---------------------------------------------------------------- balance
def test_balance_is_parsed_and_key_only_goes_to_civitai_com(studio):
    c, rig = studio["owner"], studio["rig"]
    r = c.get(BASE + "/civitai/balance")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["balances"] == {"yellow": 1234, "blue": 56} and d["checked"]
    assert CIV_KEY not in r.text
    req = rig.on("civitai.com", "/api/trpc/buzz.getBuzzAccount")[0]
    assert req.method == "GET" and req.headers["authorization"] == f"Bearer {CIV_KEY}"
    for other in rig.requests:
        if other is req:
            continue
        assert CIV_KEY not in str(other.url)
    assert CIV_KEY not in str(req.url) and not req.content


def test_balance_drops_junk_keys_and_values(studio):
    rig = studio["rig"]
    rig.balance = lambda req: httpx.Response(200, json={"result": {"data": {"json": {
        "yellow": 10, "blue": 2.9, "Green": 5, "x" * 40: 3, "red": True, "pink": "7",
        "bad key": 1, "nan": None, "list": [1], "gold": {"a": 1}}}}})
    r = studio["owner"].get(BASE + "/civitai/balance")
    assert r.json()["balances"] == {"yellow": 10, "blue": 2}


def test_balance_scope_missing_is_a_200_with_help(studio):
    studio["rig"].balance = lambda req: httpx.Response(403, json=SCOPE_BODY)
    r = studio["owner"].get(BASE + "/civitai/balance")
    assert r.status_code == 200
    assert r.json() == {"balances": None, "scope_missing": True,
                        "help_url": "https://civitai.com/user/account"}


def test_balance_server_error_is_502(studio):
    studio["rig"].balance = lambda req: httpx.Response(500, json={"detail": "boom"})
    r = studio["owner"].get(BASE + "/civitai/balance")
    assert r.status_code == 502


def test_balance_plain_403_without_scope_words_is_502(studio):
    studio["rig"].balance = lambda req: httpx.Response(403, json={"error": "nope"})
    assert studio["owner"].get(BASE + "/civitai/balance").status_code == 502


def test_balance_disallowed_profile_is_403_and_never_calls(studio):
    rig = studio["rig"]
    uma = studio["Uma"]
    n0 = len(rig.requests)
    assert uma.get(BASE + "/civitai/balance").status_code == 403
    assert len(rig.requests) == n0
    settings_store.set_many({image_access.setting_key("civitai", "user"): True})
    assert uma.get(BASE + "/civitai/balance").status_code == 200


def test_balance_without_a_key_is_409(studio, monkeypatch):
    monkeypatch.delenv("CIVITAI_API_KEY")
    r = studio["owner"].get(BASE + "/civitai/balance")
    assert r.status_code == 409 and studio["rig"].requests == []


def test_balance_second_call_inside_five_seconds_is_429(studio):
    isb = studio["isb"]
    assert isb.BALANCE_MIN_GAP_S == 5.0
    c = studio["owner"]
    assert c.get(BASE + "/civitai/balance").status_code == 200
    assert c.get(BASE + "/civitai/balance").status_code == 429
    assert len(studio["rig"].on("civitai.com", "/api/trpc/buzz.getBuzzAccount")) == 1
    for k in isb._balance_last:                      # six seconds pass
        isb._balance_last[k] -= 6.0
    assert c.get(BASE + "/civitai/balance").status_code == 200


# ---------------------------------------------------------------- models
def test_models_build_the_air_and_drop_bad_rows(studio):
    rig = studio["rig"]
    rig.models = lambda req: httpx.Response(200, json={"items": [
        _item(11, 22, "  Good   Model  ", nsfw=True, base="Pony"),
        _item(12, 23, "Not generatable", gen=False),
        _item("x", 5), _item(13, "y"), _item(14, 0), _item(15, 5, name=""),
        {"id": 16, "name": "No versions", "supportsGeneration": True, "modelVersions": []},
        "junk", None, _item(17, 30, "L" * 200),
    ], "metadata": {}})
    r = studio["owner"].get(BASE + "/civitai/models?sort=week")
    assert r.status_code == 200, r.text
    rows = r.json()["models"]
    assert [x["air"] for x in rows] == [
        "urn:air:sdxl:checkpoint:civitai:11@22", "urn:air:sdxl:checkpoint:civitai:17@30"]
    assert all(image_providers._AIR_RE.fullmatch(x["air"]) for x in rows)
    assert rows[0] == {"air": "urn:air:sdxl:checkpoint:civitai:11@22", "name": "Good Model", "base": "Pony",
                       "downloads": 286000, "likes": 900, "nsfw": True, "url": "https://civitai.com/models/11"}
    assert len(rows[1]["name"]) == 80


def test_models_request_is_public_and_shaped(studio):
    rig = studio["rig"]
    studio["owner"].get(BASE + "/civitai/models?sort=rated")
    req = rig.on("civitai.com", "/api/v1/models")[0]
    assert "authorization" not in req.headers and CIV_KEY not in str(req.url)
    q = req.url.params
    assert q["sort"] == "Highest Rated" and q["period"] == "Month" and q["types"] == "Checkpoint"
    assert q.get_list("baseModels") == ["SDXL 1.0", "Pony", "Illustrious"] and q["limit"] == "40"


def test_models_cache_hit_makes_no_second_request_and_fresh_does(studio):
    rig, c = studio["rig"], studio["owner"]
    assert c.get(BASE + "/civitai/models?sort=week").status_code == 200
    assert c.get(BASE + "/civitai/models?sort=week").status_code == 200
    assert len(rig.on("civitai.com", "/api/v1/models")) == 1
    assert c.get(BASE + "/civitai/models?sort=month").status_code == 200       # another sort: its own cache
    assert len(rig.on("civitai.com", "/api/v1/models")) == 2
    assert c.get(BASE + "/civitai/models?sort=week&fresh=true").status_code == 200
    assert len(rig.on("civitai.com", "/api/v1/models")) == 3


async def test_models_cache_expires_after_fifteen_minutes(monkeypatch):
    t = {"v": 100.0}
    monkeypatch.setattr(image_providers, "_monotonic", lambda: t["v"])
    image_providers._browse_cache.clear()
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(200, json={"items": [_item(1, 2)]})
    monkeypatch.setattr(image_providers, "transport", httpx.MockTransport(handler))
    await civitai_top_models("new")
    t["v"] += 14 * 60
    await civitai_top_models("new")
    assert len(calls) == 1
    t["v"] += 2 * 60
    await civitai_top_models("new")
    assert len(calls) == 2
    image_providers._browse_cache.clear()


def test_models_bad_sort_is_422_and_makes_no_call(studio):
    r = studio["owner"].get(BASE + "/civitai/models?sort=bogus")
    assert r.status_code == 422 and studio["rig"].requests == []


def test_models_disallowed_profile_is_403(studio):
    rig = studio["rig"]
    assert studio["Uma"].get(BASE + "/civitai/models?sort=week").status_code == 403
    assert rig.requests == []


def test_models_civitai_failure_is_502_and_is_not_cached(studio):
    rig, c = studio["rig"], studio["owner"]
    rig.models = lambda req: httpx.Response(503, json={})
    assert c.get(BASE + "/civitai/models?sort=week").status_code == 502
    rig.models = lambda req: httpx.Response(200, json={"items": [_item(1, 2)]})
    assert c.get(BASE + "/civitai/models?sort=week").status_code == 200


def test_a_picked_air_generates(studio):
    c = studio["owner"]
    studio["rig"].models = lambda req: httpx.Response(200, json={"items": [_item(133005, 1759168)]})
    air = c.get(BASE + "/civitai/models?sort=week").json()["models"][0]["air"]
    assert air == AIR
    assert c.post(f"{BASE}/generate", json=_body(model=air)).status_code == 200


