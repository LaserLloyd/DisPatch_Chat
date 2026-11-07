"""Image Studio x the who-may-make-pictures matrix (Civitai plan WP4).

Profile types x generators, enforced on /state, /generate and /estimate. Every refusal must
leave the provider transport untouched. Civitai and MiniMax calls go to one MockTransport.

Run: cd backend && uv run pytest tests/test_imagestudio_access.py
"""
from __future__ import annotations

import base64
import json

import httpx
import pytest
from privacy_fixtures import _png

from app import accounts, auth, image_access, image_providers, settings_store

BASE = "/api/apps/imagestudio"
AIR = "urn:air:sdxl:checkpoint:civitai:133005@1759168"
CIV_KEY = "civ-test-ACCESS-0123456789"
MM_KEY = "sk-test-ACCESS-0123456789"


class Rig:
    def __init__(self):
        self.requests: list[httpx.Request] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        host = req.url.host
        if host == "orchestration.civitai.com":
            if req.method == "POST" and req.url.params.get("whatif") == "true":
                return httpx.Response(200, json={"cost": {"total": 10}})
            return httpx.Response(200, json={"id": "wf", "status": "succeeded", "steps": [
                {"output": {"images": [{"available": True, "url": "https://blob.civitai.com/a.png"}]}}]})
        if host == "blob.civitai.com":
            return httpx.Response(200, content=_png(), headers={"content-type": "image/png"})
        n = json.loads(req.content).get("n", 1)
        return httpx.Response(200, json={
            "id": "t", "data": {"image_base64": [base64.b64encode(_png()).decode()] * n},
            "metadata": {"success_count": n, "failed_count": 0},
            "base_resp": {"status_code": 0, "status_msg": "success"}})


@pytest.fixture
def studio(world, monkeypatch):
    from agents_world import sibling
    owner = world.create_owner(world.client())
    image_providers.save_config({"provider": "minimax", "api_key": MM_KEY})
    monkeypatch.setenv("CIVITAI_API_KEY", CIV_KEY)
    rig = Rig()
    monkeypatch.setattr(image_providers, "transport", httpx.MockTransport(rig))

    async def no_sleep(_s):
        return None
    monkeypatch.setattr(image_providers, "_sleep", no_sleep)
    out = {"owner": (owner, None), "rig": rig, "world": world}
    admin = world.person(owner, "Ada", "admin", password="pw-pass-1234")  # scrub-ok: throwaway test profile password
    people = (("Ada", admin, None, "pw-pass-1234"), ("Uma", None, "user", "5555"), ("Bea", None, "basic", "6666"))
    for i, (name, made, role, secret) in enumerate(people):
        p = made or world.person(owner, name, role, pin=secret)
        c = sibling(world, owner, (f"203.0.113.{200 + i}", 49300 + i))
        kw = {"password": secret} if made else {"pin": secret}
        assert world.login(c, profile_id=p["id"], **kw).status_code == 200
        out[name] = (c, p["id"])
    accounts.update_profile(out["Bea"][1], grants=["use_images"])
    return out


STANDARD = {p.id for p in image_providers.PRESETS if p.access == "standard"}


def _ids(c):
    """(the provider ids /state lists, the answer); standard ones collapse to "minimax" when all present."""
    r = c.get(BASE + "/state")
    assert r.status_code == 200, r.text
    d = r.json()
    ids = [p["id"] for p in d["providers"]]
    got = set(ids) & STANDARD
    assert got in (set(), STANDARD), got            # standard is all-or-nothing
    return ([i for i in ids if i not in STANDARD] + (["minimax"] if got else []), d)


def _body(**kw):
    b = {"prompt": "a lighthouse at dusk", "ratio": "1:1", "n": 1}
    b.update(kw)
    return b


def _set(**cells):
    settings_store.set_many({image_access.setting_key(*k.split("_", 1)): v for k, v in cells.items()})


def test_default_matrix_per_profile_type(studio):
    for who, expect in (("owner", {"minimax", "civitai"}), ("Ada", {"minimax", "civitai"}),
                        ("Uma", {"minimax"}), ("Bea", set())):
        ids, d = _ids(studio[who][0])
        assert set(ids) == expect, who
        assert d["blocked"] is (not expect), who
        if "civitai" in ids:
            row = next(p for p in d["providers"] if p["id"] == "civitai")
            assert row["estimate"] is True and AIR in row["model_names"] and row["configured"] is True


def test_generate_and_estimate_follow_the_matrix(studio):
    rig = studio["rig"]
    for who, civ, std in (("owner", 200, 200), ("Ada", 200, 200), ("Uma", 403, 200), ("Bea", 403, 403)):
        c = studio[who][0]
        n0 = len(rig.requests)
        r = c.post(f"{BASE}/estimate", json=_body(provider="civitai"))
        assert r.status_code == civ, (who, r.text)
        if civ == 200:
            assert r.json() == {"buzz": 10, "n": 1}
        r = c.post(f"{BASE}/generate", json=_body(provider="civitai"))
        assert r.status_code == civ, (who, r.text)
        if civ == 403:
            assert len(rig.requests) == n0, f"{who}: a refusal must not reach any provider"
        r = c.post(f"{BASE}/generate", json=_body(provider="minimax"))
        assert r.status_code == std, (who, r.text)
        r = c.post(f"{BASE}/estimate", json=_body(provider="minimax"))
        assert r.status_code == (422 if std == 200 else 403), (who, r.text)


def test_each_switch_changes_exactly_its_cells(studio):
    rig = studio["rig"]
    uma = studio["Uma"][0]
    _set(civitai_user=True)
    assert "civitai" in _ids(uma)[0]
    assert uma.post(f"{BASE}/generate", json=_body(provider="civitai")).status_code == 200
    _set(standard_user=False)
    ids, d = _ids(uma)
    assert ids == ["civitai"] and d["blocked"] is False
    n0 = len(rig.requests)
    assert uma.post(f"{BASE}/generate", json=_body(provider="minimax")).status_code == 403
    assert len(rig.requests) == n0
    _set(civitai_user=False)
    ids, d = _ids(uma)
    assert ids == [] and d["blocked"] is True
    bea = studio["Bea"][0]
    _set(standard_basic=True)
    assert _ids(bea)[0] == ["minimax"]
    assert bea.post(f"{BASE}/generate", json=_body(provider="minimax")).status_code == 200
    _set(standard_admin=False, civitai_admin=False)
    assert _ids(studio["Ada"][0])[0] == []
    assert sorted(_ids(studio["owner"][0])[0]) == ["civitai", "minimax"]     # the Owner is never a row


def test_basic_without_use_images_gets_the_apps_403_even_when_allowed(studio):
    _set(standard_basic=True, civitai_basic=True)
    accounts.update_profile(studio["Bea"][1], grants=[])
    bea = studio["Bea"][0]
    assert bea.get(BASE + "/state").status_code == 403
    assert bea.post(f"{BASE}/generate", json=_body()).status_code == 403
    assert bea.post(f"{BASE}/estimate", json=_body(provider="civitai")).status_code == 403


def test_the_lock_target_profile_never_gets_civitai(studio):
    _set(civitai_user=True)
    uma_id = studio["Uma"][1]
    cfg = auth.load()
    cfg.default_profile = uma_id
    cfg.lock_target = "default_profile"
    auth._write(cfg)
    auth._bust_cache()
    uma = studio["Uma"][0]
    rig = studio["rig"]
    n0 = len(rig.requests)
    r = uma.get(BASE + "/state")
    if r.status_code == 200:
        assert "civitai" not in [p["id"] for p in r.json()["providers"]]
    r = uma.post(f"{BASE}/generate", json=_body(provider="civitai"))
    assert r.status_code == 403
    assert uma.post(f"{BASE}/estimate", json=_body(provider="civitai")).status_code == 403
    assert len(rig.requests) == n0


def test_refusals_cover_hard_lines_models_and_keys(studio):
    rig = studio["rig"]
    owner = studio["owner"][0]
    lora = "urn:air:sdxl:lora:civitai:1@55"
    n0 = len(rig.requests)
    assert owner.post(f"{BASE}/generate", json=_body(provider="civitai", model=lora)).status_code == 422
    assert owner.post(f"{BASE}/estimate", json=_body(provider="civitai", model="junk")).status_code == 422
    assert len(rig.requests) == n0, "a refused model never reaches Civitai"
    r = owner.post(f"{BASE}/generate", json=_body(provider="nope"))
    assert r.status_code == 422
    # every response stays free of both keys
    for r in (owner.get(BASE + "/state"),
              owner.post(f"{BASE}/estimate", json=_body(provider="civitai")),
              owner.post(f"{BASE}/generate", json=_body(provider="civitai", model=AIR))):
        assert CIV_KEY not in r.text and MM_KEY not in r.text
        assert r.status_code == 200, r.text
    rows = owner.get(BASE + "/images").json()["images"]
    assert rows[-1]["provider"] == "civitai" and rows[-1]["model"] == AIR


def test_xai_row_offers_both_grok_image_models(studio):
    """2026-11-07: xAI has two image models at different prices; the picker lists both,
    names the dearer one, and the cheaper one stays the default."""
    d = studio["owner"][0].get(BASE + "/state").json()
    row = next(p for p in d["providers"] if p["id"] == "xai")
    assert row["models"] == ["grok-imagine-image", "grok-imagine-image-2.0"]
    assert row["model"] == "grok-imagine-image"
    assert "2x" in row["model_names"]["grok-imagine-image-2.0"]
    assert "twice" in row["price_note"]
