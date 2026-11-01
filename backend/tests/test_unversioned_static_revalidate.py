"""Files served without a ?v= must revalidate (4.2.0: a warm phone kept an old
en.json, so new labels read as humanized keys - "Pill agent", "F all")."""
from __future__ import annotations

import pytest


@pytest.mark.parametrize("path", ["/static/locales/en.json", "/static/locales/ja.json",
                                  "/static/guide/phone-chats.png"])
def test_unversioned_static_files_revalidate(world, path):
    owner = world.create_owner()
    r = owner.get(path)
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-cache, must-revalidate"


def test_versioned_assets_stay_cacheable(world):
    owner = world.create_owner()
    r = owner.get("/static/js/i18n.js?v=5")
    assert r.status_code == 200
    assert "no-cache" not in r.headers.get("cache-control", "")
