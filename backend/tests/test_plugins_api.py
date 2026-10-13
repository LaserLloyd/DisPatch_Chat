"""Settings -> Plugins (app/plugins_api.py): the aggregate, the two switches,
their yaml persistence, the operator gate, and the `disabled` extension state."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from _extfake import ext_env, install, lock, unlock  # noqa: F401

from app import config, extensions, livetools, plugins_api, tools

REPO = Path(__file__).resolve().parents[2]
ORIGIN = {"origin": "http://testserver"}


def test_aggregate_shape_without_extensions(ext_env):  # noqa: F811
    c, _ = ext_env
    r = c.get("/api/plugins")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"extensions", "tools", "livetools", "voice"}
    assert body["extensions"] == []
    assert body["livetools"] == {"enabled": True, "names": list(livetools.TOOL_NAMES)}
    assert body["voice"]["installed"] is False
    assert body["voice"]["install_cmd"] == plugins_api.VOICE_INSTALL_CMD
    assert "version" not in body["voice"]
    # `tools` is exactly what /api/tools lists for the operator.
    assert body["tools"] == c.get("/api/tools").json()["tools"]


def test_aggregate_lists_an_installed_extension(ext_env):  # noqa: F811
    c, _ = ext_env
    install()
    body = c.get("/api/plugins").json()
    row = next(e for e in body["extensions"] if e["id"] == "voice")
    assert row["state"] == "ok" and row["enabled"] is True
    assert row["name"] == "Fake Voice" and row["version"] == "9.9.9"
    assert body["voice"]["installed"] is True and body["voice"]["version"] == "9.9.9"
    # No path or module:attr leaks into the row.
    assert set(row) == {"id", "name", "version", "state", "enabled", "restart_pending",
                        "source", "detail"}
    assert row["restart_pending"] is False


def test_extension_switch_writes_extensions_yaml(ext_env):  # noqa: F811
    c, tmp = ext_env
    install()
    r = c.put("/api/plugins/extensions/voice", json={"enabled": False}, headers=ORIGIN)
    assert r.status_code == 200, r.text
    assert r.json() == {"id": "voice", "enabled": False, "restart_required": True}
    assert yaml.safe_load((tmp / "extensions.yaml").read_text()) == {"disabled": ["voice"]}
    assert (tmp / "extensions.yaml").stat().st_mode & 0o777 == 0o600
    # The row shows the choice at once; the state follows at the next start.
    row = next(e for e in c.get("/api/plugins").json()["extensions"] if e["id"] == "voice")
    assert row["enabled"] is False and row["state"] == "ok" and row["restart_pending"] is True
    c.put("/api/plugins/extensions/voice", json={"enabled": True}, headers=ORIGIN)
    assert yaml.safe_load((tmp / "extensions.yaml").read_text()) == {"disabled": []}


def test_extension_switch_rejects_unknown_id_and_bad_bodies(ext_env):  # noqa: F811
    c, _ = ext_env
    install()
    assert c.put("/api/plugins/extensions/nope", json={"enabled": False},
                 headers=ORIGIN).status_code == 404
    for body in ({}, {"enabled": "yes"}, {"enabled": False, "x": 1}, {"enabled": 1}):
        r = c.put("/api/plugins/extensions/voice", json=body, headers=ORIGIN)
        assert r.status_code == 422, body


def test_listed_extension_loads_as_disabled_at_next_discovery(ext_env, monkeypatch):  # noqa: F811
    c, tmp = ext_env
    (tmp / "extensions.yaml").write_text("disabled: [voice]\n")

    class EP:
        name = "voice"
        value = "_extfake:EXTENSION"
        dist = None
    monkeypatch.setattr(extensions, "_entry_points", lambda: [EP()])
    monkeypatch.delenv("DISPATCH_EXTENSIONS", raising=False)
    monkeypatch.delenv("DISPATCH_VOICE_PKG", raising=False)
    mode, cands, off = extensions.discover()
    assert mode == "entry_points" and cands == []
    assert [(e.id, e.state) for e in off] == [("voice", "disabled")]
    assert "Settings" in off[0].detail
    # Not listed -> loads as before.
    (tmp / "extensions.yaml").write_text("disabled: []\n")
    _, cands, off = extensions.discover()
    assert [x.target for x in cands] == ["_extfake:EXTENSION"] and off == []


def test_unreadable_extensions_yaml_switches_nothing_off(ext_env):  # noqa: F811
    _, tmp = ext_env
    (tmp / "extensions.yaml").write_text("disabled: [unclosed\n")
    assert extensions.user_disabled() == set()
    (tmp / "extensions.yaml").write_text("- a\n- b\n")
    assert extensions.user_disabled() == set()


def test_livetools_switch_persists_and_is_read_by_the_hook(ext_env):  # noqa: F811
    c, tmp = ext_env
    assert tools.livetools_enabled() is True
    r = c.put("/api/plugins/livetools", json={"enabled": False}, headers=ORIGIN)
    assert r.status_code == 200, r.text
    assert r.json()["enabled"] is False and r.json()["restart_required"] is False
    assert yaml.safe_load((tmp / "tools.yaml").read_text())["livetools"] == {"enabled": False}
    assert tools.livetools_enabled() is False          # effective at once
    assert c.get("/api/plugins").json()["livetools"]["enabled"] is False
    c.put("/api/plugins/livetools", json={"enabled": True}, headers=ORIGIN)
    assert tools.livetools_enabled() is True


def test_livetools_switch_survives_a_tools_save_and_keeps_tool_rows(ext_env):  # noqa: F811
    c, tmp = ext_env
    (tmp / "tools.yaml").write_text(yaml.safe_dump({"tools": [
        {"id": "docs", "kind": "url", "title": "Docs", "url": "https://example.org/", "enabled": True}]}))
    c.put("/api/plugins/livetools", json={"enabled": False}, headers=ORIGIN)
    doc = yaml.safe_load((tmp / "tools.yaml").read_text())
    assert [t["id"] for t in doc["tools"]] == ["docs"] and doc["livetools"] == {"enabled": False}
    # The Tools table's own PUT rewrites the file; the switch must stay.
    rows = c.get("/api/tools").json()["tools"]
    keep = [r for r in rows if r["id"] == "docs"]
    r = c.put("/api/tools", json={"tools": keep}, headers=ORIGIN)
    assert r.status_code == 200, r.text
    assert yaml.safe_load((tmp / "tools.yaml").read_text())["livetools"] == {"enabled": False}
    assert tools.livetools_enabled() is False


def test_livetools_bad_block_is_a_malformed_manifest_but_hook_stays_on(ext_env):  # noqa: F811
    _, tmp = ext_env
    (tmp / "tools.yaml").write_text("livetools: {enabled: maybe}\n")
    assert tools.load_tools() == []
    assert tools.livetools_enabled() is True


def test_livetools_switch_refuses_to_overwrite_an_unreadable_file(ext_env):  # noqa: F811
    c, tmp = ext_env
    (tmp / "tools.yaml").write_text("tools: [unclosed\n")
    r = c.put("/api/plugins/livetools", json={"enabled": False}, headers=ORIGIN)
    assert r.status_code == 409
    assert (tmp / "tools.yaml").read_text() == "tools: [unclosed\n"


@pytest.mark.parametrize("method,path,body", [
    ("GET", "/api/plugins", None),
    ("PUT", "/api/plugins/extensions/voice", {"enabled": False}),
    ("PUT", "/api/plugins/livetools", {"enabled": False}),
])
def test_routes_are_operator_only(ext_env, method, path, body):  # noqa: F811
    c, tmp = ext_env
    install()
    lock(c)                                           # PIN set, no session = Safe Mode
    r = c.request(method, path, json=body, headers=ORIGIN)
    assert r.status_code == 403, (path, r.text)
    assert not (tmp / "extensions.yaml").exists()
    unlock(c)
    r = c.request(method, path, json=body, headers=ORIGIN)
    assert r.status_code == 200, (path, r.text)


def test_cross_origin_write_is_refused(ext_env):  # noqa: F811
    c, tmp = ext_env
    r = c.put("/api/plugins/livetools", json={"enabled": False},
              headers={"origin": "http://evil.example"})
    assert r.status_code == 403
    assert not (tmp / "tools.yaml").exists()


def test_voice_install_command_matches_the_frontend_constants():
    about = (REPO / "frontend/static/js/about.js").read_text()
    absent = (REPO / "frontend/static/js/voice-absent.js").read_text()
    repo = re.search(r"VOICE_REPO_URL\s*=\s*'([^']+)'", about).group(1)
    tag = re.search(r"VOICE_TAG\s*=\s*'([^']+)'", absent).group(1)
    assert plugins_api.VOICE_REPO_URL == repo and plugins_api.VOICE_TAG == tag


def test_a_switch_never_overwrites_an_off_list_it_cannot_read(ext_env):  # noqa: F811
    """Review 2.3.0 LOW-4: a hand-edit typo in extensions.yaml must not be turned
    into a file that re-enables every other add-on."""
    c, _ = ext_env
    install()
    path = config.DATA_DIR / extensions.USER_OFF_FILE
    path.write_text("disabled: [voice\n")                     # not YAML
    r = c.put("/api/plugins/extensions/voice", json={"enabled": False})
    assert r.status_code == 409, r.text
    assert path.read_text() == "disabled: [voice\n", "untouched"
    path.write_text("disabled: [other]\nextra: 1\n")          # a shape we do not own
    assert c.put("/api/plugins/extensions/voice", json={"enabled": False}).status_code == 409
    path.write_text("disabled: [other]\n")
    assert c.put("/api/plugins/extensions/voice", json={"enabled": False}).status_code == 200
    assert sorted(extensions.user_disabled()) == ["other", "voice"], "the earlier id is kept"
