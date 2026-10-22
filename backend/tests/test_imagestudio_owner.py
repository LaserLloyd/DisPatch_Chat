"""Image Studio per owner (4.0, BUILD-PLAN-v2 §1.11 / WP-DEL): every ledger row carries the
profile that made it, and ``/state``, ``/images``, ``/clear`` and a ``result`` reference read
only the caller's own rows - Admins included. Recent prompts are ``prompts-<profile>.json``.
A row from before rows had an owner is the protected Admin's. A Basic profile without
``use_images`` gets the app's 403; with it granted, it makes and sees its own pictures.

Run: cd backend && uv run pytest tests/test_imagestudio_owner.py
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx
import pytest
from privacy_fixtures import _png

from app import accounts, config, image_providers

BASE = "/api/apps/imagestudio"


@pytest.fixture
def studio(world, monkeypatch):
    owner = world.create_owner(world.client())
    image_providers.save_config({"provider": "minimax", "api_key": "sk-test-OWNER-0123456789"})
    body = _png()

    def handler(request):
        n = json.loads(request.content).get("n", 1)
        return httpx.Response(200, json={
            "id": "t", "data": {"image_base64": [base64.b64encode(body).decode()] * n},
            "metadata": {"success_count": n, "failed_count": 0},
            "base_resp": {"status_code": 0, "status_msg": "success"}})
    monkeypatch.setattr(image_providers, "transport", httpx.MockTransport(handler))
    from agents_world import sibling
    out = {"owner": owner, "world": world}
    for i, (name, role) in enumerate((("Uma", "user"), ("Una", "user"), ("Bea", "basic"))):
        pin = f"{i + 5}{i + 5}{i + 5}{i + 5}"
        p = world.person(owner, name, role, pin=pin)
        c = sibling(world, owner, (f"203.0.113.{190 + i}", 49200 + i))
        assert world.login(c, profile_id=p["id"], pin=pin).status_code == 200
        out[name] = (c, p["id"])
    return out


def _make(c, prompt: str) -> dict:
    r = c.post(f"{BASE}/generate", json={"prompt": prompt, "ratio": "1:1", "n": 1})
    assert r.status_code == 200, r.text
    return r.json()["images"][0]


def _ledger() -> list[dict]:
    p = Path(config.DATA_DIR) / "apps-data" / "imagestudio" / "images.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]


def test_rows_carry_their_owner_and_lists_are_per_owner(studio):
    uma, uma_id = studio["Uma"]
    una, _ = studio["Una"]
    mine = _make(uma, "SENT-UMA-PIC lighthouse")
    _make(una, "SENT-UNA-PIC harbour")
    assert any(r["id"] == mine["id"] and r["owner_profile_id"] == uma_id for r in _ledger())
    for path in ("/images", "/state"):
        r = uma.get(BASE + path)
        assert r.status_code == 200
        assert "SENT-UMA-PIC" in r.text and "SENT-UNA-PIC" not in r.text, path
    assert "owner_profile_id" not in uma.get(BASE + "/images").text
    # the protected Admin sees neither
    r = studio["owner"].get(BASE + "/images")
    assert "SENT-UMA-PIC" not in r.text and "SENT-UNA-PIC" not in r.text
    assert studio["owner"].get(BASE + "/state").json()["total"] == 0


def test_clear_reaches_only_the_callers_pictures(studio):
    uma, _ = studio["Uma"]
    una, _ = studio["Una"]
    theirs = _make(una, "SENT-UNA-KEEP")
    first = _make(uma, "SENT-UMA-1")
    _make(uma, "SENT-UMA-2")
    # someone else's picture id is "already gone" for this caller
    r = uma.post(f"{BASE}/clear", json={"id": theirs["id"], "order": "oldest", "dry_run": True})
    assert r.status_code == 404
    r = uma.post(f"{BASE}/clear", json={"id": first["id"], "order": "oldest", "dry_run": True})
    assert r.status_code == 200 and r.json()["count"] == 2
    r = uma.post(f"{BASE}/clear", json={"id": first["id"], "order": "oldest", "expect": 2})
    assert r.status_code == 200 and r.json()["deleted"] == 2
    assert "SENT-UNA-KEEP" in una.get(BASE + "/images").text
    assert (Path(config.MEDIA_DIR) / theirs["url"].rsplit("/", 1)[1]).is_file()


def test_a_result_reference_must_be_the_callers_own(studio):
    uma, _ = studio["Uma"]
    una, _ = studio["Una"]
    theirs = _make(una, "SENT-UNA-REF")
    r = uma.post(f"{BASE}/generate", json={"prompt": "a copy", "ratio": "1:1", "n": 1,
                                           "reference": {"kind": "result", "id": theirs["id"]}})
    assert r.status_code in (404, 422)


def test_recent_prompts_are_per_profile(studio):
    uma, uma_id = studio["Uma"]
    una, _ = studio["Una"]
    _make(uma, "SENT-UMA-PROMPT")
    assert "SENT-UMA-PROMPT" in json.dumps(uma.get(BASE + "/state").json()["prompts"])
    assert "SENT-UMA-PROMPT" not in json.dumps(una.get(BASE + "/state").json()["prompts"])
    assert (Path(config.DATA_DIR) / "apps-data" / "imagestudio" / f"prompts-{uma_id}.json").is_file()


def test_a_row_without_an_owner_is_the_protected_admins(studio):
    d = Path(config.DATA_DIR) / "apps-data" / "imagestudio"
    d.mkdir(parents=True, exist_ok=True)
    with (d / "images.jsonl").open("a") as f:
        f.write(json.dumps({"id": "a" * 32, "url": "/media/legacy.png", "prompt": "SENT-LEGACY",
                            "day": "2026-01-01"}) + "\n")
    uma, _ = studio["Uma"]
    assert "SENT-LEGACY" in studio["owner"].get(BASE + "/images").text
    assert "SENT-LEGACY" not in uma.get(BASE + "/images").text


def test_basic_needs_use_images_and_then_sees_only_its_own(studio):
    bea, bea_id = studio["Bea"]
    assert bea.get(BASE + "/state").status_code == 403
    assert bea.post(f"{BASE}/generate", json={"prompt": "x", "ratio": "1:1"}).status_code == 403
    accounts.update_profile(bea_id, grants=["use_images"])
    uma, _ = studio["Uma"]
    _make(uma, "SENT-UMA-B")
    r = bea.get(BASE + "/state")
    assert r.status_code == 200, r.text
    _make(bea, "SENT-BEA-PIC")
    r = bea.get(BASE + "/images")
    assert "SENT-BEA-PIC" in r.text and "SENT-UMA-B" not in r.text
