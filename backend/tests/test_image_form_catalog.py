"""GET /api/llm/image carries the model catalog for every preset (WP4), never a key."""
from __future__ import annotations

import pytest

from app import config, image_providers, model_catalog

KEY = "sk-formcat-0123456789"        # scrub-ok: test fixture, not a real credential


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setenv("MINIMAX_API_KEY", KEY)


def test_image_get_has_catalog_per_preset(world):
    owner = world.create_owner()
    r = owner.get("/api/llm/image")
    assert r.status_code == 200, r.text
    j = r.json()
    cat = j["catalog"]
    assert set(cat) == {p.id for p in image_providers.DEFAULT_PRESETS}
    mm = cat["minimax"]
    assert "MiniMax-H3-Image" in mm["models"] and mm["source"] == "documented"
    assert mm["names"] and "note" in mm
    assert j["can_refresh_models"] is True
    assert KEY not in r.text


def test_catalog_matches_model_catalog(world):
    owner = world.create_owner()
    cat = owner.get("/api/llm/image").json()["catalog"]
    for pid, e in cat.items():
        assert e == model_catalog.image_models(pid)
