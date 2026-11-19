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


# First-load round (docs/design/first-load.md): a CURRENT ?v= URL is immutable, so a
# phone reopening the app does not revalidate ~70 modules once the browser's heuristic
# freshness has run out. "Current" = sw.js's generated VERSIONS table.
import re  # noqa: E402
from pathlib import Path  # noqa: E402

_STATIC = Path(__file__).resolve().parents[2] / "frontend" / "static"
_SW = (_STATIC / "sw.js").read_text(encoding="utf-8")


def _current(path: str) -> str:
    return re.search(rf"'{re.escape(path)}': '(\d+)'", _SW).group(1)


@pytest.fixture
def settled(monkeypatch):
    from app import main
    monkeypatch.setattr(main, "_deploy_settling", lambda: False)
    return main


@pytest.mark.parametrize("path", ["/static/js/i18n.js", "/static/app.css", "/static/js/main.js"])
def test_current_versioned_app_files_are_immutable(world, settled, path):
    r = world.create_owner().get(f"{path}?v={_current(path)}")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "public, max-age=31536000, immutable"


@pytest.mark.parametrize("path", ["/static/js/i18n.js", "/static/app.css"])
def test_an_old_or_other_version_is_never_pinned(world, settled, path):
    """An old tab or a rollback asks for a ?v= the server answers with today's bytes."""
    other = str(int(_current(path)) + 1000)
    r = world.create_owner().get(f"{path}?v={other}")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-cache"


def test_nothing_is_pinned_while_a_deploy_settles(world, monkeypatch):
    from app import main
    monkeypatch.setattr(main, "_deploy_settling", lambda: True)
    r = world.create_owner().get(f"/static/js/i18n.js?v={_current('/static/js/i18n.js')}")
    assert r.headers["cache-control"] == "no-cache"


@pytest.mark.parametrize("path", ["/static/js/i18n.js", "/static/sw.js?v=3",
                                  "/static/locales/en.json?v=2", "/static/js/i18n.js?v=abc",
                                  "/static/js/nope.js?v=5", "/static/icon-192.png?v=3"])
def test_unversioned_or_special_files_are_not_immutable(world, settled, path):
    r = world.create_owner().get(path)
    assert "immutable" not in r.headers.get("cache-control", "")


def test_the_version_table_parses(settled):
    table = settled._static_versions()
    assert table.get("/static/js/main.js") and table.get("/static/app.css")
