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


# First-load round (docs/design/first-load.md): a ?v= URL is immutable, so a phone
# reopening the app does not revalidate ~70 modules once the browser's heuristic
# freshness has run out, and the service worker's install reuses the page's copies.
@pytest.mark.parametrize("path", ["/static/js/i18n.js?v=5", "/static/app.css?v=1",
                                  "/static/js/main.js?v=12&x=1"])
def test_versioned_app_files_are_immutable(world, path):
    r = world.create_owner().get(path)
    assert r.status_code == 200
    assert r.headers["cache-control"] == "public, max-age=31536000, immutable"


@pytest.mark.parametrize("path", ["/static/js/i18n.js", "/static/sw.js?v=3",
                                  "/static/locales/en.json?v=2", "/static/js/i18n.js?v=abc",
                                  "/static/js/nope.js?v=5"])
def test_unversioned_or_special_files_are_not_immutable(world, path):
    r = world.create_owner().get(path)
    assert "immutable" not in r.headers.get("cache-control", "")

