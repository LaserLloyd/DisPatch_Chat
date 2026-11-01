"""Tools as manifests (3.0, W5): the back-compat shim over extensions, apps and tools.yaml rows,
the approval gate on enabling a plugin, and settings fragments under ``tools.<id>.``."""
from __future__ import annotations

import sys

import pytest
import yaml
from _extfake import ext_env, install

from app import accounts, apps_loader, config, migrate, settings_schema, settings_store, tool_manifests, tools
from app.contracts.manifests import ToolPluginManifest

ORIGIN = {"origin": "http://testserver"}


def _by_id():
    return {r["manifest"]["id"]: r for r in tool_manifests.list_manifests()}


def test_the_shipped_apps_become_valid_manifests(world):
    rows = _by_id()
    assert {"jobboard", "imagestudio"} <= set(rows)
    for r in rows.values():
        ToolPluginManifest.model_validate(r["manifest"])           # every output obeys the frozen schema
    img = rows["imagestudio"]["manifest"]
    assert img["kind"] == "app" and img["permission"] == "use_images" and img["restricted_ok"] is False
    assert img["publisher"] == "DisPatch" and img["entry"] == "apps/imagestudio/backend.py"
    assert {"http_routes", "ui", "run_code"} <= set(img["permissions_requested"])
    jb = rows["jobboard"]["manifest"]
    assert "character" in jb["permissions_requested"]


def test_an_extension_manifest_lists_what_it_registered(ext_env):
    c, _ = ext_env
    install()
    row = _by_id()["voice"]
    m = row["manifest"]
    assert m["kind"] == "extension" and m["name"] == "Fake Voice" and m["version"] == "9.9.9"
    assert m["entry"] == "dispatch.extensions:voice" and m["permission"] == "use_voice"
    assert {"http_routes", "websocket", "ui", "character_fields", "background_work", "panes"} <= set(
        m["permissions_requested"])
    assert [p["id"] for p in row["permissions"]] == m["permissions_requested"]
    assert all(p["label"] for p in row["permissions"])


def test_tools_yaml_rows_are_manifests_too(world, tmp_path_factory):
    root = tmp_path_factory.mktemp("page")          # outside the data directory, which is never served
    (root / "index.html").write_text("<p>hi</p>")
    rows = [{"id": "bench", "title": "Bench", "icon": "b", "kind": "url", "url": "https://bench.example.net/",
             "enabled": True, "safe": False}]
    if sys.platform != "win32":          # tools.yaml wants POSIX absolute roots (a known Windows gap)
        rows.insert(0, {"id": "notes", "title": "Notes", "icon": "n", "kind": "static", "root": str(root),
                        "entry": "index.html", "enabled": True, "safe": True})
    (config.DATA_DIR / "tools.yaml").write_text(yaml.safe_dump({"tools": rows}))
    tools._drop_cache()
    by = _by_id()
    if sys.platform != "win32":
        n = by["notes"]["manifest"]
        # restricted_ok stays in the published schema (deprecated) but DisPatch writes False.
        assert (n["kind"], n["restricted_ok"], n["permissions_requested"]) == ("static", False, ["ui"])
    b = by["bench"]["manifest"]
    assert b["kind"] == "url" and b["network"] == ["bench.example.net"]
    assert b["permissions_requested"] == ["network:bench.example.net"]
    assert tool_manifests.describe_permission("network:bench.example.net") == "Contact bench.example.net"


def test_the_manifests_route_needs_manage_plugins(world):
    owner = world.create_owner()
    r = owner.get("/api/plugins/manifests")
    assert r.status_code == 200 and "permission_labels" in r.json()
    assert {x["manifest"]["id"] for x in r.json()["manifests"]} >= {"jobboard", "imagestudio"}
    stranger = world.client(("203.0.113.80", 43001))
    assert stranger.get("/api/plugins/manifests").status_code in (401, 403)


# --------------------------------------------------------------------------- #
# Approval
# --------------------------------------------------------------------------- #


def test_a_new_plugin_must_be_approved_before_it_can_be_switched_on(ext_env):
    c, _ = ext_env
    install()
    assert c.put("/api/plugins/extensions/voice", json={"enabled": False}, headers=ORIGIN).status_code == 200
    row = _by_id()["voice"]
    assert row["approved"] is False and row["needs_approval"] is True
    r = c.put("/api/plugins/extensions/voice", json={"enabled": True}, headers=ORIGIN)
    assert r.status_code == 409 and r.json()["error"]["code"] == "plugin_needs_approval"
    requested = r.json()["requested"]
    assert "http_routes" in requested and r.json()["permissions"][0]["label"]
    # a stale or partial approval is refused: you must confirm exactly what it asks for NOW
    r = c.put("/api/plugins/extensions/voice", json={"enabled": True, "approve": requested[:1]}, headers=ORIGIN)
    assert r.status_code == 409
    r = c.put("/api/plugins/extensions/voice", json={"enabled": True, "approve": requested}, headers=ORIGIN)
    assert r.status_code == 200 and r.json()["enabled"] is True
    row = _by_id()["voice"]
    assert row["approved"] and row["approval"]["source"] == "user"
    # approved once: later toggles need no confirmation
    assert c.put("/api/plugins/extensions/voice", json={"enabled": False}, headers=ORIGIN).status_code == 200
    assert c.put("/api/plugins/extensions/voice", json={"enabled": True}, headers=ORIGIN).status_code == 200
    # switching OFF never needs approval, and a malformed approve list is a 422
    assert c.put("/api/plugins/extensions/voice", json={"enabled": True, "approve": "yes"}, headers=ORIGIN).status_code == 422


def test_what_was_running_at_the_upgrade_is_grandfathered(ext_env):
    c, _ = ext_env
    install()
    assert _by_id()["voice"]["approved"] is False
    assert tool_manifests.grandfather_running() >= 1
    row = _by_id()["voice"]
    assert row["approved"] and row["approval"]["source"] == "upgrade"
    assert c.put("/api/plugins/extensions/voice", json={"enabled": False}, headers=ORIGIN).status_code == 200
    assert c.put("/api/plugins/extensions/voice", json={"enabled": True}, headers=ORIGIN).status_code == 200


def test_the_migration_plan_names_what_it_will_approve(ext_env):
    install()
    with accounts.connect() as con:                    # as on a 2.x install that has not been migrated yet
        con.execute("DELETE FROM schema_migrations WHERE id='007_plugin_approvals'")
    rep = migrate.plan()
    text = "\n".join(rep.lines())
    assert "007_plugin_approvals" in {a.step for a in rep.actions}
    assert "approve extension 'voice'" in text and "http_routes" in text


# --------------------------------------------------------------------------- #
# Settings fragments
# --------------------------------------------------------------------------- #


def test_an_apps_env_names_become_settings_under_tools_id(world, monkeypatch):
    tool_manifests.register_fragments()
    d = settings_schema.get_def("tools.imagestudio.openai_api_key") if False else None
    # imagestudio declares no env; a synthetic app does
    m = apps_loader.Manifest(id="weather", title="Weather", icon="w", safe=False, order=1, legacy_prefix=None,
                             bot=None, thread_hook=False, env=("WEATHER_API_KEY", "WEATHER_REGION"),
                             dir=config.DATA_DIR, source="data")
    monkeypatch.delenv("WEATHER_API_KEY", raising=False)
    monkeypatch.delenv("WEATHER_REGION", raising=False)
    assert tool_manifests.app_env(m) == {}                                   # nothing set anywhere
    d = settings_schema.get_def("tools.weather.weather_api_key")
    assert d is not None and d.secret and d.env == "WEATHER_API_KEY" and d.level == "advanced"
    assert settings_schema.get_def("tools.weather.weather_region").type == "string"
    # a value saved in Settings reaches the app through ctx.env, a secret through the secret store
    settings_store.set_many({"tools.weather.weather_region": "EU", "tools.weather.weather_api_key": "k-123456789"})
    assert tool_manifests.app_env(m) == {"WEATHER_REGION": "EU", "WEATHER_API_KEY": "k-123456789"}
    # the process environment still wins
    monkeypatch.setenv("WEATHER_REGION", "US")
    assert tool_manifests.app_env(m)["WEATHER_REGION"] == "US"
    assert settings_store.resolve("tools.weather.weather_region").source == "env"
    # the plugin's settings appear in the schema the Settings page renders
    keys = {s["key"] for s in settings_schema.schema_document(settings_schema.all_defs())["settings"]}
    assert "tools.weather.weather_api_key" in keys


def test_settings_survive_the_registry_being_cleared(world):
    """The registry is process-wide and rebuilt on demand; a stored value stays in the table."""
    settings_schema.register(settings_schema.from_fragment(
        [{"key": "region", "type": "string", "label": "Region"}], namespace="tools.weather", owner="tool:weather"),
        "tool:weather")
    settings_store.set_many({"tools.weather.region": "EU"})
    settings_schema.unregister("tool:weather")
    assert settings_store.get("tools.weather.region") == "EU"                  # an unknown key still reads
    assert "tools.weather.region" in settings_store.extra_values()
    with pytest.raises(KeyError):
        settings_store.resolve("tools.weather.region")
