"""The extension host (app/extensions.py): discovery, handshake, rollback,
/api/extensions, /ext/ static serving, include_router rules, tools rows
(incl. dormant rows), bot-field validators and lifecycle hooks.

The add-on itself is never installed here (DisPatch's CI stays add-on-free —
requirement 7's proof); tests/_extfake.py plays its part.
"""
from __future__ import annotations

import logging
import os
import stat
import threading
from pathlib import Path

import pytest
from _extfake import KNOBS, ext_env, install, lock, thread_for, unlock

from app import auth, config, extensions, main, tools

# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #


def _fake_dist(site: Path, name: str = "fakevoice", version: str = "1.2.3",
               ep: str = "voice = _extfake:EXTENSION") -> None:
    d = site / f"{name}-{version}.dist-info"
    d.mkdir(parents=True)
    (d / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n")
    (d / "entry_points.txt").write_text(f"[dispatch.extensions]\n{ep}\n")


def _only_site(monkeypatch, site: Path) -> None:
    """Discovery sees ONLY the entry points of the fake dist under `site`.

    These tests must hold in DisPatch's environment with or without the real
    add-on installed (the live box runs `uv run --with-editable DisPatch_Voice`,
    so a real `voice` entry point is normally there): without this filter the
    real one shows up beside the fake and the counts are off by one."""
    real = extensions._entry_points

    def mine() -> list:
        return [ep for ep in real()
                if str(site) in str(getattr(getattr(ep, "dist", None), "_path", ""))]
    monkeypatch.setattr(extensions, "_entry_points", mine)


def test_entry_point_discovery_names_the_dist_never_a_path(tmp_path, monkeypatch):
    _fake_dist(tmp_path / "site")
    monkeypatch.syspath_prepend(str(tmp_path / "site"))
    monkeypatch.delenv("DISPATCH_EXTENSIONS", raising=False)
    monkeypatch.delenv("DISPATCH_VOICE_PKG", raising=False)
    mode, cands, disabled = extensions.discover()
    assert mode == "entry_points" and not disabled
    mine = [c for c in cands if c.target == "_extfake:EXTENSION"]
    assert len(mine) == 1
    assert mine[0].ep_name == "voice"
    assert mine[0].source == "entry point (fakevoice 1.2.3)"
    assert "/" not in mine[0].source


def test_off_imports_nothing_and_lists_installed_as_disabled(tmp_path, monkeypatch):
    _fake_dist(tmp_path / "site")
    monkeypatch.syspath_prepend(str(tmp_path / "site"))
    _only_site(monkeypatch, tmp_path / "site")
    monkeypatch.setenv("DISPATCH_EXTENSIONS", "off")
    mode, cands, disabled = extensions.discover()
    assert mode == "off" and cands == []
    row = next(d for d in disabled if d.id == "voice")
    assert row.state == "disabled" and row.name == "fakevoice" and row.version == "1.2.3"


def test_explicit_list_loads_only_those(tmp_path, monkeypatch):
    _fake_dist(tmp_path / "site")
    monkeypatch.syspath_prepend(str(tmp_path / "site"))
    _only_site(monkeypatch, tmp_path / "site")
    monkeypatch.setenv("DISPATCH_EXTENSIONS", " _extfake:OTHER ")
    mode, cands, disabled = extensions.discover()
    assert mode == "env"
    assert [c.target for c in cands] == ["_extfake:OTHER"]
    assert [d.id for d in disabled if d.id == "voice"] == ["voice"]


def test_legacy_voice_pkg_is_honoured_once_and_logged(tmp_path, monkeypatch, caplog):
    # A 2.1 box: the add-on is NOT installed as a package (no entry point).
    monkeypatch.setattr(extensions, "_entry_points", list)
    monkeypatch.delenv("DISPATCH_EXTENSIONS", raising=False)
    monkeypatch.setenv("DISPATCH_VOICE_PKG", str(tmp_path))
    monkeypatch.setattr("sys.path", list(os.sys.path))
    with caplog.at_level(logging.WARNING, logger="local-chat.ext"):
        mode, cands, _ = extensions.discover()
    assert str(tmp_path) in os.sys.path
    legacy = [c for c in cands if c.target == "dispatch_voice:EXTENSION"]
    assert legacy and legacy[0].source == "DISPATCH_VOICE_PKG (deprecated)"
    assert "deprecated" in caplog.text


def test_legacy_voice_pkg_beside_an_installed_add_on_loads_it_once(tmp_path, monkeypatch):
    """Mid-migration: the unit still sets DISPATCH_VOICE_PKG and the add-on is
    also installed (`--with-editable`). The entry point wins; the legacy path
    adds no second candidate for the same target."""
    _fake_dist(tmp_path / "site", ep="voice = dispatch_voice:EXTENSION")
    monkeypatch.syspath_prepend(str(tmp_path / "site"))
    _only_site(monkeypatch, tmp_path / "site")
    monkeypatch.delenv("DISPATCH_EXTENSIONS", raising=False)
    monkeypatch.setenv("DISPATCH_VOICE_PKG", str(tmp_path))
    monkeypatch.setattr("sys.path", list(os.sys.path))
    mode, cands, _ = extensions.discover()
    voice = [c for c in cands if c.target == "dispatch_voice:EXTENSION"]
    assert mode == "entry_points" and len(voice) == 1
    assert voice[0].source == "entry point (fakevoice 1.2.3)"


# --------------------------------------------------------------------------- #
# Handshake states and rollback
# --------------------------------------------------------------------------- #


def test_ok_extension_mounts_and_logs(ext_env, caplog):
    with caplog.at_level(logging.INFO, logger="local-chat.ext"):
        ext = install()
    assert ext.state == "ok" and ext.detail == ""
    assert "ext voice: Fake Voice 9.9.9 ok (api 1)" in caplog.text
    paths = {getattr(r, "path", "") for r in main.app.routes}
    assert "/api/voice/status" in paths


@pytest.mark.parametrize("target,side", [("_extfake:INCOMPATIBLE_NEW", "Update DisPatch."),
                                         ("_extfake:INCOMPATIBLE_OLD", "Update the add-on.")])
def test_incompatible_says_which_side_to_update(ext_env, target, side):
    ext = install(target)
    assert ext.state == "incompatible"
    assert ext.detail.endswith(side)
    assert "this DisPatch has 1" in ext.detail
    assert KNOBS.ctx is None                      # setup never ran


@pytest.mark.parametrize("target", ["_extfake:NOT_A_DICT", "_extfake:BAD_ID",
                                    "no_such_module_anywhere:EXTENSION", "_extfake:MISSING"])
def test_bad_metadata_or_import_is_failed_not_fatal(ext_env, target):
    ext = install(target)
    assert ext.state == "failed" and ext.detail


def test_raising_setup_rolls_back_every_route_and_tool(ext_env):
    KNOBS.raise_in_setup = RuntimeError("model dir missing")
    KNOBS.raise_after_router = True
    before = {getattr(r, "path", "") for r in main.app.routes}
    ext = install()
    assert ext.state == "failed" and ext.detail == "RuntimeError: model dir missing"
    assert {getattr(r, "path", "") for r in main.app.routes} == before
    assert "voice-lab" not in tools.BUILTINS
    assert extensions.websocket_spec("voice") is None
    assert not extensions.owns_path("/api/voice/status")


@pytest.mark.parametrize("knob,value", [("bad_prefix", "/api/other"), ("bad_prefix", "/api/voicex"),
                                        ("ws_route_in_router", True), ("async_setup", True)])
def test_router_rules_fail_setup(ext_env, knob, value):
    setattr(KNOBS, knob, value)
    before = {getattr(r, "path", "") for r in main.app.routes}
    ext = install()
    assert ext.state == "failed", ext.detail
    assert {getattr(r, "path", "") for r in main.app.routes} == before


def test_include_router_with_a_websocket_route_is_a_value_error(ext_env):
    from fastapi import APIRouter
    install()
    ctx = KNOBS.ctx
    ctx._sealed = False                           # re-open to call it directly
    r = APIRouter()

    @r.websocket("/ws")
    async def w(ws):                              # pragma: no cover
        pass

    with pytest.raises(ValueError, match="thread_websocket"):
        ctx.include_router(r, prefix="/api/voice/x")


def test_include_router_refuses_a_mount_or_plain_route(ext_env, tmp_path):
    """Review T1 L3: a Mount / starlette Route takes no `dependencies`, so the
    operator gate would never run on it — refused like a websocket route."""
    from fastapi import APIRouter
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route
    from starlette.staticfiles import StaticFiles
    install()
    ctx = KNOBS.ctx
    ctx._sealed = False
    before = {getattr(r, "path", "") for r in main.app.routes}
    r = APIRouter()
    r.mount("/files", StaticFiles(directory=str(tmp_path)))
    with pytest.raises(ValueError, match="only FastAPI API routes"):
        ctx.include_router(r, prefix="/api/voice/x")
    r2 = APIRouter()
    r2.routes.append(Route("/plain", lambda req: PlainTextResponse("x")))
    with pytest.raises(ValueError, match="only FastAPI API routes"):
        ctx.include_router(r2, prefix="/api/voice/y")
    assert {getattr(r, "path", "") for r in main.app.routes} == before


def test_duplicate_id_second_is_refused(ext_env):
    first = install()
    second = install()
    assert first.state == "ok" and second.state == "failed"
    assert "already provides 'voice'" in second.detail
    assert extensions.get("voice") is first


def test_context_is_sealed_after_setup(ext_env):
    from fastapi import APIRouter
    install()
    with pytest.raises(RuntimeError, match="only allowed inside setup"):
        KNOBS.ctx.include_router(APIRouter(), prefix="/api/voice/late")


def test_context_paths_and_state_dir(ext_env):
    _c, tmp = ext_env
    install()
    p = KNOBS.ctx.paths
    assert p.voice_yaml == tmp / "voice.yaml" and p.acks_yaml == tmp / "voice-acks.yaml"
    assert p.voices_dir == tmp / "voices" and p.cache_dir == tmp / "voice-cache"
    assert p.state_dir == tmp / "ext" / "voice" and p.state_dir.is_dir()
    assert stat.S_IMODE(p.state_dir.stat().st_mode) == 0o700
    assert not hasattr(KNOBS.ctx, "data_dir") and not hasattr(KNOBS.ctx, "config")


def test_context_bots_are_views_without_provider_blocks(ext_env):
    install()
    v = KNOBS.ctx.bots.get("main")
    assert v.id == "main" and v.backend == "gateway" and v.safe is False
    assert not hasattr(v, "api") and not hasattr(v, "advisor")
    assert KNOBS.ctx.bots.get("nope") is None
    assert {b.id for b in KNOBS.ctx.bots.all()} >= {"alpha", "main"}


def test_env_carries_only_declared_names(ext_env, monkeypatch):
    monkeypatch.setenv("FAKE_EXT_ENV", "1")
    monkeypatch.setenv("SOMETHING_ELSE", "x")
    install()
    assert KNOBS.ctx.env == {"FAKE_EXT_ENV": "1"}


# --------------------------------------------------------------------------- #
# GET /api/extensions
# --------------------------------------------------------------------------- #

ROW_KEYS = {"id", "name", "version", "api", "state", "source", "assets_version",
            "entry", "css", "locales", "tools"}


def test_api_extensions_shape_has_no_paths(ext_env):
    c, tmp = ext_env
    install()
    body = c.get("/api/extensions").json()
    assert body["api"] == 1 and body["mode"] in ("entry_points", "env", "off")
    (row,) = body["extensions"]
    assert set(row) == ROW_KEYS | {"detail"}
    assert row["state"] == "ok" and row["api"] == [1, 1] and row["entry"] == "index.js"
    assert row["css"] == ["voice.css"] and row["locales"] is True
    assert row["tools"] == ["voice-lab", "voice-cloning"]
    assert len(row["assets_version"]) == 16
    text = str(body)
    assert str(tmp) not in text and "_extfake:" not in text


def test_api_extensions_detail_scrubs_home_and_caps(ext_env, monkeypatch):
    c, _ = ext_env
    home = str(Path.home())
    KNOBS.raise_in_setup = FileNotFoundError(f"{home}/models/parakeet missing " + "x" * 400)
    install()
    row = c.get("/api/extensions").json()["extensions"][0]
    assert row["state"] == "failed"
    assert home not in row["detail"] and "~/models/parakeet" in row["detail"]
    assert len(row["detail"]) <= 200


def test_api_extensions_safe_mode_403_and_machine_without_detail(ext_env):
    c, _ = ext_env
    install()
    lock(c)
    # A browser with no session (Safe Mode): the uniform decoy 403.
    r = c.get("/api/extensions", headers={"sec-fetch-mode": "cors", "origin": "http://testserver"})
    assert r.status_code == 403
    # A sessionless on-box machine (TestClient is not loopback, so it presents
    # the api token the way a remote agent would).
    cfg = auth.load()
    cfg.api_token = "tok-ext-123"
    auth._write(cfg)
    auth._bust_cache()
    r = c.get("/api/extensions", headers={"x-api-key": "tok-ext-123"})
    assert r.status_code == 200, r.text
    assert "detail" not in r.json()["extensions"][0]
    # …but a machine never reaches an extension's own routes.
    assert c.get("/api/voice/status", headers={"x-api-key": "tok-ext-123"}).status_code == 403
    assert c.get("/api/extensions", headers={"x-api-key": "wrong"}).status_code == 401


def test_api_extensions_operator_gets_detail(ext_env):
    c, _ = ext_env
    install()
    unlock(c)
    row = c.get("/api/extensions").json()["extensions"][0]
    assert row["detail"] == ""


def test_auth_status_counts_ok_extensions(ext_env):
    c, _ = ext_env
    assert c.get("/api/auth/status").json()["features"]["extensions"] == 0
    install()
    assert c.get("/api/auth/status").json()["features"]["extensions"] == 1
    lock(c)
    assert "extensions" not in (c.get("/api/auth/status").json().get("features") or {})


# --------------------------------------------------------------------------- #
# /ext/<id>/<path>
# --------------------------------------------------------------------------- #


def test_ext_static_serves_allowlisted_types_with_headers(ext_env):
    c, _ = ext_env
    install()
    r = c.get("/ext/voice/index.js")
    assert r.status_code == 200 and "EXT" in r.text
    assert r.headers["content-type"].startswith("text/javascript")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["content-security-policy"] == "default-src 'none'; frame-ancestors 'self'"
    assert r.headers["x-robots-tag"] == "noindex"
    assert r.headers["cache-control"] == "no-cache"
    etag = r.headers["etag"]
    assert etag.startswith('"') and len(etag) > 10
    assert c.get("/ext/voice/index.js", headers={"if-none-match": etag}).status_code == 304
    assert c.get("/ext/voice/voice.css").headers["content-type"].startswith("text/css")
    assert c.get("/ext/voice/locales/en.json").json() == {"drive": {"title": "Voice chat"}}
    assert c.head("/ext/voice/index.js").status_code == 200


def test_ext_static_caching_keys_on_the_assets_hash(ext_env):
    c, _ = ext_env
    install()
    v = c.get("/api/extensions").json()["extensions"][0]["assets_version"]
    r = c.get(f"/ext/voice/index.js?v={v}")
    assert r.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert c.get("/ext/voice/index.js?v=0000").headers["cache-control"] == "no-cache"
    # An edit changes the hash (an editable checkout is never served stale).
    (KNOBS.static_dir / "index.js").write_text("export const EXT = { id: 'voice', api: [1, 1] }; // 2\n")
    v2 = c.get("/api/extensions").json()["extensions"][0]["assets_version"]
    assert v2 != v
    assert c.get(f"/ext/voice/index.js?v={v}").headers["cache-control"] == "no-cache"


@pytest.mark.parametrize("rel", ["voices.html", "icon.svg", "notes.xml", "helper.py",
                                 ".hidden.js", "../static/index.js", "locales/../index.js/x",
                                 "nope.js", "locales", ""])
def test_ext_static_refuses_documents_traversal_and_unknowns(ext_env, rel):
    c, _ = ext_env
    install()
    assert c.get(f"/ext/voice/{rel}").status_code == 404


def test_ext_static_unknown_or_unhealthy_extension_404(ext_env):
    c, _ = ext_env
    assert c.get("/ext/voice/index.js").status_code == 404
    KNOBS.raise_in_setup = RuntimeError("x")
    install()
    assert c.get("/ext/voice/index.js").status_code == 404


def test_ext_static_locked_device_403(ext_env):
    c, _ = ext_env
    install()
    lock(c)
    r = c.get("/ext/voice/index.js")
    assert r.status_code == 403 and "index.js" not in r.text
    unlock(c)
    assert c.get("/ext/voice/index.js").status_code == 200


def test_serve_static_rejects_a_non_js_entry(ext_env, tmp_path):
    install()
    ctx = KNOBS.ctx
    ctx._sealed = False
    ctx._ext.static_dir = None
    with pytest.raises(ValueError, match="JavaScript"):
        ctx.serve_static(KNOBS.static_dir, entry="voice.css")
    with pytest.raises(ValueError, match="servable"):
        ctx.serve_static(KNOBS.static_dir, entry="voices.html")


# --------------------------------------------------------------------------- #
# Extension routes: operator only, Safe-Mode block list, never machine-inbound,
# body limits.
# --------------------------------------------------------------------------- #


def test_extension_routes_are_operator_only(ext_env):
    c, _ = ext_env
    install()
    assert c.get("/api/voice/status").status_code == 200
    lock(c)
    assert c.get("/api/voice/status").status_code == 403
    assert main._decoy_blocked("GET", "/api/voice/status")
    assert main._decoy_blocked("GET", "/api/extensions")
    assert main._decoy_blocked("GET", "/ext/voice/index.js")
    assert not main._is_inbound("GET", "/api/voice/status")
    assert main._is_inbound("GET", "/api/extensions")
    unlock(c)
    assert c.get("/api/voice/status").status_code == 200


def test_other_extension_prefix_joins_the_block_list(ext_env):
    install("_extfake:OTHER")
    assert extensions.owns_path("/api/other/ping")
    assert main._decoy_blocked("GET", "/api/other/ping")
    assert not main._is_inbound("GET", "/api/other/ping")


def test_body_limits_refuse_before_the_body_is_read(ext_env):
    c, _ = ext_env
    install()
    assert main._upload_ceiling("/api/voice/upload") == 1000
    assert c.post("/api/voice/upload", content=b"x" * 900).json() == {"bytes": 900}
    assert c.post("/api/voice/upload", content=b"x" * 1001).status_code == 413
    assert main._upload_ceiling("/api/voice/elsewhere") is None


def test_counters_are_exposed_to_the_extension(ext_env):
    c, _ = ext_env
    install()
    main._ext_counters["speculation_stale"] += 2
    assert c.get("/api/voice/status").json()["counters"]["speculation_stale"] == 2


# --------------------------------------------------------------------------- #
# Tools rows: registered, probed, dormant when the extension is gone
# --------------------------------------------------------------------------- #


def _manifest(text: str) -> None:
    (config.DATA_DIR / "tools.yaml").write_text(text)
    tools._reset_state()


def test_registered_tools_list_with_their_probe(ext_env):
    c, _ = ext_env
    install()
    rows = {t["id"]: t for t in c.get("/api/tools").json()["tools"]}
    assert rows["voice-lab"]["builtin_feature"] == "voice_lab"
    assert rows["voice-lab"]["available"] is True and rows["voice-lab"]["enabled"] is False
    assert rows["voice-cloning"]["available"] is False
    _manifest("tools:\n- {id: voice-lab, kind: builtin, enabled: true}\n")
    rows = {t["id"]: t for t in c.get("/api/tools").json()["tools"]}
    assert rows["voice-lab"]["enabled"] is True


def test_dormant_rows_survive_load_and_save_without_the_extension(ext_env, tmp_path_factory,
                                                                 caplog):
    c, _ = ext_env
    site = tmp_path_factory.mktemp("site-static")    # outside the data dir
    (site / "index.html").write_text("<p>hi</p>")
    _manifest("tools:\n"
              "- {id: voice-cloning, kind: builtin, enabled: true}\n"
              "- {id: voice-lab, kind: builtin, enabled: false, open: window}\n"
              f"- {{id: bench, title: Bench, kind: static, root: {site}}}\n")
    with caplog.at_level(logging.INFO, logger="local-chat.tools"):
        loaded = tools.load_tools()
    # The manifest is NOT invalidated: the static tool is still there.
    assert [t.id for t in loaded] == ["voice-cloning", "voice-lab", "bench"]
    assert "tool voice-cloning: no extension provides it; row kept" in caplog.text
    body = c.get("/api/tools").json()["tools"]
    ids = [t["id"] for t in body]
    assert "bench" in ids and "voice-cloning" not in ids and "voice-lab" not in ids
    # A Settings save sends back what it was given; the dormant rows stay.
    r = c.put("/api/tools", json={"tools": body})
    assert r.status_code == 200, r.text
    text = (config.DATA_DIR / "tools.yaml").read_text()
    assert "voice-cloning" in text and "voice-lab" in text and "bench" in text
    tools._reset_state()
    assert {t.id for t in tools.load_tools()} >= {"voice-cloning", "voice-lab", "bench"}


def test_put_refuses_a_new_unknown_builtin(ext_env):
    c, _ = ext_env
    r = c.put("/api/tools", json={"tools": [{"id": "voice-lab", "kind": "builtin", "enabled": True}]})
    assert r.status_code == 422 and "not a builtin id" in r.text


def test_dormant_row_wakes_when_the_extension_returns(ext_env):
    c, _ = ext_env
    _manifest("tools:\n- {id: voice-lab, kind: builtin, enabled: true}\n")
    assert all(t["id"] != "voice-lab" for t in c.get("/api/tools").json()["tools"])
    install()
    row = next(t for t in c.get("/api/tools").json()["tools"] if t["id"] == "voice-lab")
    assert row["enabled"] is True and row["available"] is True


def test_extension_tool_ids_cannot_take_a_core_builtin(ext_env):
    install()
    ctx = KNOBS.ctx
    ctx._sealed = False
    from _extfake import Tool
    with pytest.raises(ValueError, match="already taken"):
        ctx.register_tools([Tool("mail-panel", "x_mail", "Mail", "x", True, lambda: True)])


# --------------------------------------------------------------------------- #
# Bot fields DisPatch stores for extensions
# --------------------------------------------------------------------------- #


def _order(c, **fields):
    bots = c.get("/api/bots/all").json()["bots"]
    items = [{"id": b["id"], "order": i} for i, b in enumerate(bots)]
    for it in items:
        if it["id"] == "main":
            it.update(fields)
    return c.put("/api/bots/order", json={"bots": items})


def test_validators_decide_voice_and_voice_rp(ext_env):
    c, _ = ext_env
    install()
    r = _order(c, voice="stock-host", voice_rp="dialogue")
    assert r.status_code == 200, r.text
    main_row = next(b for b in r.json()["bots"] if b["id"] == "main")
    assert main_row["voice"] == "stock-host" and main_row["voice_rp"] == "dialogue"
    r = _order(c, voice="grandma")
    assert r.status_code == 422 and "Unknown voice: grandma" in r.text
    r = _order(c, voice_rp="mumble")
    assert r.status_code == 422 and "role-play" in r.text
    allb = {b["id"]: b for b in c.get("/api/bots/all").json()["bots"]}
    assert allb["main"]["voice"] == "stock-host" and allb["main"]["voice_rp"] == "dialogue"
    # The public roster (and the `bots` broadcast built from it) never carry them.
    pub = c.get("/api/bots").json()["bots"]
    assert all("voice" not in b and "voice_rp" not in b for b in pub)


def test_without_the_extension_only_empty_or_unchanged_values_save(ext_env):
    c, _ = ext_env
    cfg = config.CONFIG_PATH.read_text() if config.CONFIG_PATH.exists() else None
    config.load_bots()
    import yaml
    doc = yaml.safe_load(config.CONFIG_PATH.read_text())
    for b in doc["bots"]:
        if b["id"] == "main":
            b["voice"] = "grandma"
    config.CONFIG_PATH.write_text(yaml.safe_dump(doc))
    config._invalidate_bots_cache()
    assert _order(c, voice="grandma").status_code == 200        # unchanged: kept
    assert _order(c, voice="someone-else").status_code == 422
    assert "needs the voice add-on" in _order(c, voice_rp="dialogue").text
    assert _order(c).status_code == 200                           # absent key: untouched
    assert config.get_bot("main").voice == "grandma"
    assert _order(c, voice="").status_code == 200
    assert config.get_bot("main").voice == ""
    del cfg


def test_set_bot_field_validator_refuses_unknown_fields(ext_env):
    install()
    ctx = KNOBS.ctx
    ctx._sealed = False
    with pytest.raises(ValueError, match="stores no bot field"):
        ctx.set_bot_field_validator("api", lambda v: None)


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #


def test_startup_hooks_run_thread_ones_off_loop_and_survive_a_failure(ext_env, caplog):
    c, _ = ext_env
    install()
    with caplog.at_level(logging.ERROR, logger="local-chat.ext"):
        c.portal.call(extensions.run_startup)
    for _ in range(200):
        if KNOBS.startup_threads:
            break
        threading.Event().wait(0.01)
    assert KNOBS.startup_threads and KNOBS.startup_threads[0] == "ext-voice-startup"
    assert KNOBS.startup_calls == ["plain"]
    assert "an on_startup hook failed" in caplog.text


def test_shutdown_hooks_run(ext_env):
    c, _ = ext_env
    install()
    c.portal.call(extensions.run_shutdown)
    assert KNOBS.shutdown_calls == ["down"]


def test_the_thread_websocket_and_unclaimed_routes_exist_without_extensions():
    paths = {getattr(r, "path", "") for r in main.app.routes}
    assert {"/ws/{name}/{thread_id}", "/api/extensions", "/ext/{ext_id}/{rel:path}"} <= paths
    assert thread_for  # imported helper used by sibling modules
