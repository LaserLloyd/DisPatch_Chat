"""Apps: trusted add-on packages — manifest, loader, gating, static, bots, tools rows.

Contract: docs/design/2026-09-25-apps.md. An app is REPO code (or a data-dir
package the operator explicitly trusted in tools.yaml) imported into the
server process, so the interesting cases are the boundaries around it: a
manifest that says more than the schema allows, a package that fails to
import, a Safe-Mode caller, a disabled app, a static path that tries to leave
the package, a roster the operator already owns.

Same hermetic style as the rest of tests/: throwaway data dir, nothing touches
the live install. Test packages are written to tmp and mounted into main.app
under ids no real app uses, then unmounted again.
Run: cd backend && uv run pytest -q tests/test_apps_loader.py
"""
from __future__ import annotations

import asyncio
import dataclasses
import os
import sys
import textwrap
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from app import apps_loader, auth, config, main, tools
from app.database import Database

LOOPBACK = ("127.0.0.1", 50000)
REMOTE = ("testclient", 50000)
BROWSER = {"origin": "http://127.0.0.1:8765", "sec-fetch-site": "same-origin"}

FIRST_PARTY_CSP = ("default-src 'self'; script-src 'self'; style-src 'self' "
                   "'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; "
                   "frame-ancestors 'self'")

BACKEND_OK = '''
from fastapi import APIRouter, Depends, Request

router = APIRouter()
CTX = None


def build(ctx):
    global CTX
    CTX = ctx

    @router.get("/ping")
    def ping():
        return {"app": ctx.app_id, "pong": True}

    @router.post("/echo", dependencies=[Depends(ctx.require_access)])
    def echo(body: dict):
        return {"echo": body}

    @router.post("/admin", dependencies=[Depends(ctx.require_operator)])
    def admin():
        return {"ok": True}

    return router
'''


def _write_app(root: Path, app_id: str, manifest: dict | None = None,
               backend: str = BACKEND_OK, static: bool = True) -> Path:
    d = root / app_id
    (d / "static").mkdir(parents=True, exist_ok=True)
    m = {"id": app_id, "title": app_id.title(), "icon": "chart"}
    m.update(manifest or {})
    (d / "app.yaml").write_text(yaml.safe_dump(m, sort_keys=False, allow_unicode=True))
    (d / "backend.py").write_text(textwrap.dedent(backend))
    if static:
        (d / "static" / "index.html").write_text("<h1>app index</h1>")
        (d / "static" / "app.js").write_text("export const x = 1;")
        (d / "static" / "thread.js").write_text("export function mount(){return {unmount(){}}}")
        (d / "static" / "sub").mkdir()
        (d / "static" / "sub" / "deep.css").write_text("h1{color:red}")
        (d / "static" / ".hidden.js").write_text("secret")
        (d / "static" / "prod.env").write_text("TOKEN=x")
        (d / "locales").mkdir()
        (d / "locales" / "en.json").write_text('{"hello": "Hello"}')
    return d


@pytest.fixture
def env(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "CONFIG_PATH", data / "config.yaml")
    monkeypatch.setattr(config, "MEDIA_DIR", data / "media")
    monkeypatch.setattr(config, "FILES_DIR", data / "files")
    monkeypatch.setattr(config, "LOG_DIR", data / "logs")
    monkeypatch.setattr(config, "BACKUP_DIR", data / "backups")
    monkeypatch.setattr(main, "MEDIA_DIR", data / "media")
    monkeypatch.setattr(main, "FILES_DIR", data / "files")
    monkeypatch.setattr(auth, "SECURITY_PATH", data / "security.yaml")
    monkeypatch.setattr(auth, "RECOVERY_PATH", data / "RECOVERY-CODE.txt")
    auth._sessions.clear()
    auth._cache = None
    auth._fail_count = 0
    auth._fail_until = 0.0
    tools._reset_state()
    config._invalidate_bots_cache()
    temp_db = Database(data / "chats.db")
    monkeypatch.setattr(main, "db", temp_db)

    repo = tmp_path / "apps"
    repo.mkdir()
    loaded: list[str] = []
    clients: list[TestClient] = []

    def load(**kw):
        out = apps_loader.load_all(main.app, repo_dir=repo, hooks=main.APP_HOOKS, **kw)
        loaded.extend(la.manifest.id for la in out)
        return out

    def make_client(addr=REMOTE) -> TestClient:
        c = TestClient(main.app, client=addr)
        c.__enter__()
        clients.append(c)
        return c

    yield repo, load, make_client

    for c in clients:
        c.__exit__(None, None, None)
    for app_id in loaded:
        apps_loader.unload(main.app, app_id)
    tools._reset_state()
    asyncio.run(temp_db.close())


def _unlocked(make_client, addr=REMOTE):
    c = make_client(addr)
    auth.set_pin("1234")
    r = c.post("/api/auth/unlock", json={"pin": "1234"})
    assert r.status_code == 200, r.text
    return c


def _manifest_rows(text: str) -> None:
    (config.DATA_DIR / "tools.yaml").write_text(text)
    tools._reset_state()


# --------------------------------------------------------------------------- #
# Manifest validation (closed schema)
# --------------------------------------------------------------------------- #

def _parse(tmp_path, doc: dict, name: str | None = None):
    d = tmp_path / (name or doc.get("id") or "x")
    (d / "static").mkdir(parents=True, exist_ok=True)
    (d / "static" / "thread.js").write_text("")
    return apps_loader.parse_manifest(doc, d, "repo")


def test_manifest_full_example_parses(tmp_path):
    m = _parse(tmp_path, {
        "id": "jobboard", "title": "Job Board", "icon": "clipboard", "safe": False,
        "order": 50, "api": {"legacy_prefix": "/api/jobs"},
        "bot": {"id": "jobboard", "name": "Job Board", "emoji": "🎯",
                "avatar": "jobboard-face.png", "agent": "scout", "visible": False},
        "thread_hook": True, "env": ["JOBS_PROFILE_PATH"],
    })
    assert (m.id, m.title, m.icon, m.safe, m.order) == ("jobboard", "Job Board", "clipboard", False, 50)
    assert m.legacy_prefix == "/api/jobs"
    assert m.bot.id == "jobboard" and m.bot.agent == "scout" and m.bot.visible is False
    assert m.thread_hook is True and m.env == ("JOBS_PROFILE_PATH",)


def test_manifest_defaults(tmp_path):
    m = _parse(tmp_path, {"id": "plain"})
    assert m.title == "plain" and m.safe is False and m.bot is None
    assert m.legacy_prefix is None and m.thread_hook is False and m.env == ()


@pytest.mark.parametrize("doc,field", [
    ({"id": "a", "extra": 1}, "extra"),
    ({"id": "Bad_Id"}, "id"),
    ({"id": "other"}, "id"),                                  # id != directory name
    ({"id": "a", "safe": "yes"}, "safe"),
    ({"id": "a", "order": True}, "order"),
    ({"id": "a", "title": ""}, "title"),
    ({"id": "a", "api": {"legacy_prefix": "/api/x", "more": 1}}, "api.more"),
    ({"id": "a", "api": {"legacy_prefix": "/apis/x"}}, "api.legacy_prefix"),
    ({"id": "a", "api": {"legacy_prefix": "/api/apps/x"}}, "api.legacy_prefix"),
    ({"id": "a", "api": {"legacy_prefix": "/api/../x"}}, "api.legacy_prefix"),
    ({"id": "a", "api": {"legacy_prefix": "/api"}}, "api.legacy_prefix"),
    ({"id": "a", "api": {"legacy_prefix": "/api/tools"}}, "api.legacy_prefix"),   # reserved
    ({"id": "a", "bot": {"id": "a", "colour": "red"}}, "bot.colour"),
    ({"id": "a", "bot": {"id": "no spaces"}}, "bot.id"),
    ({"id": "a", "bot": {"id": "a", "avatar": "../x.png"}}, "bot.avatar"),
    ({"id": "a", "bot": {"id": "a", "visible": "no"}}, "bot.visible"),
    ({"id": "a", "thread_hook": True}, "thread_hook"),        # a hook needs a bot
    ({"id": "a", "env": ["lower"]}, "env"),
    ({"id": "a", "env": "JOBS"}, "env"),
])
def test_manifest_refusals(tmp_path, doc, field):
    with pytest.raises(apps_loader.ManifestError) as ei:
        _parse(tmp_path, doc, name="a")
    assert ei.value.field == field, str(ei.value)


def test_manifest_thread_hook_without_file_is_refused(tmp_path):
    d = tmp_path / "a"
    (d / "static").mkdir(parents=True)
    with pytest.raises(apps_loader.ManifestError) as ei:
        apps_loader.parse_manifest({"id": "a", "bot": {"id": "a"}, "thread_hook": True}, d, "repo")
    assert ei.value.field == "thread_hook"


# --------------------------------------------------------------------------- #
# Discovery + import isolation
# --------------------------------------------------------------------------- #

def test_discover_orders_by_order_then_id_and_skips_broken(env, caplog):
    repo, load, _ = env
    _write_app(repo, "zeta-test", {"order": 10})
    _write_app(repo, "alpha-test", {"order": 20})
    _write_app(repo, "beta-test", {"order": 10})
    (repo / "broken-test").mkdir()
    (repo / "broken-test" / "app.yaml").write_text("id: broken-test\nnope: [\n")
    (repo / "notes").mkdir()                            # no app.yaml → not an app
    found = apps_loader.discover(repo_dir=repo)
    assert [m.id for m in found] == ["beta-test", "zeta-test", "alpha-test"]
    assert "broken-test" in caplog.text


def test_import_failure_is_isolated(env, caplog):
    repo, load, make_client = env
    _write_app(repo, "good-test")
    _write_app(repo, "boom-test", backend="raise RuntimeError('import boom')\n")
    _write_app(repo, "norouter-test", backend="def build(ctx):\n    return 42\n")
    _write_app(repo, "nobuild-test", backend="x = 1\n")
    out = load()
    by = {la.manifest.id: la for la in out}
    assert by["good-test"].mounted is True
    for bad in ("boom-test", "norouter-test", "nobuild-test"):
        assert by[bad].mounted is False and by[bad].error
        assert f"dispatch_app_{bad.replace('-', '_')}" not in sys.modules
    assert "import boom" in caplog.text
    c = make_client()
    assert c.get("/api/apps/good-test/ping").json() == {"app": "good-test", "pong": True}
    assert c.get("/api/apps/boom-test/ping").status_code == 404


def test_module_is_imported_under_its_app_name_as_a_package(env):
    repo, load, _ = env
    d = _write_app(repo, "pkg-test", backend='''
        from fastapi import APIRouter
        from . import helper
        def build(ctx):
            r = APIRouter()
            @r.get("/v")
            def v():
                return {"v": helper.VALUE}
            return r
    ''')
    (d / "helper.py").write_text("VALUE = 7\n")
    (la,) = load()
    assert la.module.__name__ == "dispatch_app_pkg_test"
    assert sys.modules["dispatch_app_pkg_test.helper"].VALUE == 7


def test_load_is_idempotent(env):
    repo, load, make_client = env
    _write_app(repo, "twice-test")
    load()
    n = len(main.app.router.routes)
    load()
    assert len(main.app.router.routes) == n


# --------------------------------------------------------------------------- #
# Mounting: /api/apps/<id> AND the legacy prefix
# --------------------------------------------------------------------------- #

def test_mounted_at_both_prefixes(env):
    repo, load, make_client = env
    _write_app(repo, "dual-test", {"api": {"legacy_prefix": "/api/dualtest"}})
    load()
    c = make_client()
    a = c.get("/api/apps/dual-test/ping")
    b = c.get("/api/dualtest/ping")
    assert a.status_code == b.status_code == 200
    assert a.json() == b.json() == {"app": "dual-test", "pong": True}
    assert c.post("/api/dualtest/echo", json={"k": 1}).json() == {"echo": {"k": 1}}


def test_legacy_prefix_colliding_with_a_shell_route_is_refused(env, caplog):
    repo, load, make_client = env
    _write_app(repo, "clash-test", {"api": {"legacy_prefix": "/api/threads"}})
    (la,) = load()
    assert la.mounted is True and la.prefixes == ("/api/apps/clash-test",)
    assert "legacy_prefix" in caplog.text
    c = make_client()
    assert c.get("/api/apps/clash-test/ping").status_code == 200
    # The legacy mount never happened: the shell's own routes answer there.
    assert "pong" not in c.get("/api/threads/ping").text


def test_app_id_that_is_someone_elses_bot_is_refused(env):
    repo, load, _ = env
    config._write_bots([config._bot_entry(config.Bot(id="taken-test", name="T"))])
    _write_app(repo, "taken-test")
    (la,) = load()
    assert la.mounted is False and "bot" in la.error


def test_app_id_may_equal_its_own_bot_id(env):
    repo, load, _ = env
    config._write_bots([config._bot_entry(config.Bot(id="own-test", name="Own"))])
    _write_app(repo, "own-test", {"bot": {"id": "own-test", "name": "Own"}})
    (la,) = load()
    assert la.mounted is True


# --------------------------------------------------------------------------- #
# Gating: decoy matrix, disabled, safe apps, machine callers
# --------------------------------------------------------------------------- #

def test_decoy_blocked_rules():
    for method, path in (("GET", "/api/apps"), ("GET", "/api/apps/x"),
                         ("GET", "/api/apps/x/y"), ("POST", "/api/apps/x/y"),
                         ("GET", "/apps/x/"), ("GET", "/apps/x/app.js"),
                         ("HEAD", "/apps/x/"), ("GET", "/apps"),
                         ("GET", "/api/jobs"), ("POST", "/api/jobs/find"),
                         ("GET", "/api/apps/jobboard/months")):
        assert main._decoy_blocked(method, path) is True, (method, path)
    # Not app paths at all — untouched by the app rule.
    assert main._decoy_blocked("GET", "/api/appsettings") is False
    assert main._decoy_blocked("GET", "/applesauce") is False


def test_safe_mode_browser_gets_decoy_403_everywhere(env):
    repo, load, make_client = env
    _write_app(repo, "priv-test", {"api": {"legacy_prefix": "/api/privtest"}})
    load()
    c = make_client(LOOPBACK)
    auth.set_pin("1234")
    for method, path in (("GET", "/api/apps/priv-test/ping"),
                         ("POST", "/api/apps/priv-test/echo"),
                         ("GET", "/api/privtest/ping"),
                         ("GET", "/apps/priv-test/"),
                         ("GET", "/apps/priv-test/app.js"),
                         ("GET", "/apps/nosuch/"),
                         ("GET", "/api/apps/nosuch/x")):
        r = c.request(method, path, headers=BROWSER)
        assert r.status_code == 403, (method, path, r.status_code)
        assert r.json().get("decoy") is True, (method, path, r.text)


def test_remote_machine_without_token_is_refused(env):
    repo, load, make_client = env
    _write_app(repo, "rem-test")
    load()
    c = make_client(REMOTE)
    auth.set_pin("1234")
    assert c.get("/api/apps/rem-test/ping").status_code == 401
    assert c.get("/apps/rem-test/").status_code == 403           # static: not a machine surface


def test_loopback_machine_gets_require_access_but_not_require_operator(env):
    repo, load, make_client = env
    _write_app(repo, "mach-test", {"api": {"legacy_prefix": "/api/machtest"}})
    load()
    c = make_client(LOOPBACK)
    auth.set_pin("1234")
    assert c.get("/api/apps/mach-test/ping").status_code == 200
    assert c.post("/api/apps/mach-test/echo", json={"a": 1}).json() == {"echo": {"a": 1}}
    assert c.post("/api/machtest/echo", json={"a": 2}).status_code == 200
    r = c.post("/api/apps/mach-test/admin")
    assert r.status_code == 403 and r.json().get("decoy") is True


def test_operator_session_reaches_everything(env):
    repo, load, make_client = env
    _write_app(repo, "op-test")
    load()
    c = _unlocked(make_client)
    assert c.post("/api/apps/op-test/admin").json() == {"ok": True}
    assert c.get("/apps/op-test/").status_code == 200


def test_disabled_app_is_404_for_the_operator_and_403_for_safe_mode(env):
    repo, load, make_client = env
    _write_app(repo, "off-test", {"api": {"legacy_prefix": "/api/offtest"}})
    load()
    _manifest_rows("tools:\n  - {id: off-test, kind: app, enabled: false}\n")
    c = make_client(LOOPBACK)
    for path in ("/api/apps/off-test/ping", "/api/offtest/ping", "/apps/off-test/"):
        assert c.get(path).status_code == 404, path          # no PIN → operator
    auth.set_pin("1234")
    assert c.get("/api/apps/off-test/ping", headers=BROWSER).status_code == 403
    # A machine caller is refused too: the switch turns the FEATURE off.
    assert c.get("/api/apps/off-test/ping").status_code == 404
    _manifest_rows("tools:\n  - {id: off-test, kind: app, enabled: true}\n")
    assert c.get("/api/apps/off-test/ping").status_code == 200


def test_safe_app_get_is_allowed_in_safe_mode_but_mutations_are_not(env):
    repo, load, make_client = env
    _write_app(repo, "pub-test", {"safe": True})
    load()
    assert main._decoy_blocked("GET", "/api/apps/pub-test/ping") is False
    assert main._decoy_blocked("GET", "/apps/pub-test/") is False
    assert main._decoy_blocked("POST", "/api/apps/pub-test/echo") is True
    c = make_client(LOOPBACK)
    auth.set_pin("1234")
    assert c.get("/api/apps/pub-test/ping", headers=BROWSER).status_code == 200
    assert c.get("/apps/pub-test/", headers=BROWSER).status_code == 200
    r = c.post("/api/apps/pub-test/echo", json={}, headers=BROWSER)
    assert r.status_code == 403 and r.json()["decoy"] is True


# --------------------------------------------------------------------------- #
# Static serving
# --------------------------------------------------------------------------- #

def test_static_entry_nested_headers_and_csp(env):
    repo, load, make_client = env
    _write_app(repo, "st-test")
    load()
    c = make_client()
    r = c.get("/apps/st-test/")
    assert r.status_code == 200 and "app index" in r.text
    assert r.headers["content-security-policy"] == FIRST_PARTY_CSP
    assert "sandbox" not in r.headers["content-security-policy"]
    assert "no-cache" in r.headers["cache-control"]
    assert r.headers["x-content-type-options"] == "nosniff"
    js = c.get("/apps/st-test/app.js")
    assert js.status_code == 200 and js.headers["content-type"].startswith("text/javascript")
    assert js.headers["cache-control"] == "private, max-age=0, must-revalidate"
    assert c.get("/apps/st-test/sub/deep.css").text == "h1{color:red}"
    assert c.head("/apps/st-test/app.js").status_code == 200
    assert c.get("/apps/st-test/index.html").headers["content-security-policy"] == FIRST_PARTY_CSP
    bare = c.get("/apps/st-test", follow_redirects=False)
    assert bare.status_code == 307 and bare.headers["location"] == "/apps/st-test/"


def test_static_locales_come_from_the_package_locales_dir(env):
    repo, load, make_client = env
    _write_app(repo, "loc-test")
    load()
    c = make_client()
    r = c.get("/apps/loc-test/locales/en.json")
    assert r.status_code == 200 and r.json() == {"hello": "Hello"}
    assert c.get("/apps/loc-test/locales/xx.json").status_code == 404


def test_static_refusals_are_a_uniform_404(env, tmp_path):
    repo, load, make_client = env
    d = _write_app(repo, "ref-test")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside")
    os.symlink(outside, d / "static" / "link.txt")
    (d / "static" / "id_rsa").write_text("KEY")
    load()
    c = make_client()
    for rel in ("..%2Fbackend.py", "../backend.py", "%2E%2E/app.yaml", ".hidden.js",
                "prod.env", "id_rsa", "link.txt", "nope.js", "sub/../../app.yaml"):
        r = c.get(f"/apps/ref-test/{rel}")
        assert r.status_code == 404, (rel, r.status_code, r.text[:80])
        assert "outside" not in r.text and "KEY" not in r.text


def test_static_of_an_unmounted_app_is_404(env):
    repo, load, make_client = env
    _write_app(repo, "dead-test", backend="raise ImportError('x')\n")
    load()
    assert make_client().get("/apps/dead-test/").status_code == 404


# --------------------------------------------------------------------------- #
# Data-dir apps: loaded only when tools.yaml trusts them
# --------------------------------------------------------------------------- #

def test_data_dir_app_needs_trusted(env):
    repo, load, make_client = env
    ddir = config.DATA_DIR / "apps"
    ddir.mkdir()
    _write_app(ddir, "dd-test")
    assert "dd-test" not in [m.id for m in apps_loader.discover(repo_dir=repo)]
    _manifest_rows("tools:\n  - {id: dd-test, kind: app, enabled: true}\n")
    assert "dd-test" not in [m.id for m in apps_loader.discover(repo_dir=repo)]
    _manifest_rows("tools:\n  - {id: dd-test, kind: app, trusted: true}\n")
    found = {m.id: m for m in apps_loader.discover(repo_dir=repo)}
    assert found["dd-test"].source == "data"
    load()
    assert make_client().get("/api/apps/dd-test/ping").json()["pong"] is True
    # Its static root lives under DATA_DIR and is still served (trusted code).
    assert make_client().get("/apps/dd-test/").status_code == 200


def test_data_dir_app_cannot_shadow_a_repo_app(env, caplog):
    repo, load, _ = env
    _write_app(repo, "shadow-test")
    _write_app(config.DATA_DIR / "apps", "shadow-test", {"title": "Evil"})
    _manifest_rows("tools:\n  - {id: shadow-test, kind: app, trusted: true}\n")
    found = [m for m in apps_loader.discover(repo_dir=repo) if m.id == "shadow-test"]
    assert len(found) == 1 and found[0].source == "repo"
    assert "shadow" in caplog.text


# --------------------------------------------------------------------------- #
# AppContext
# --------------------------------------------------------------------------- #

def test_app_context_fields(env):
    names = [f.name for f in dataclasses.fields(apps_loader.AppContext)]
    for want in ("app_id", "config", "require_operator", "require_access", "bot_id",
                 "env", "log"):
        assert want in names
    # Behaviour rather than data: methods on the context.
    for method in ("broadcast", "broadcast_message", "dispatch_turn"):
        assert callable(getattr(apps_loader.AppContext, method))
    assert isinstance(apps_loader.AppContext.data_dir, property)
    assert isinstance(apps_loader.AppContext.db, property)


def test_app_context_data_dir_env_and_db(env, monkeypatch):
    repo, load, _ = env
    monkeypatch.setenv("CTX_TEST_ALLOWED", "yes")
    monkeypatch.setenv("CTX_TEST_SECRET", "no")
    _write_app(repo, "ctx-test", {"env": ["CTX_TEST_ALLOWED", "CTX_TEST_MISSING"],
                                  "bot": {"id": "ctxbot-test"}})
    (la,) = load()
    ctx = la.ctx
    assert ctx.app_id == "ctx-test" and ctx.bot_id == "ctxbot-test"
    assert ctx.env == {"CTX_TEST_ALLOWED": "yes"}
    assert ctx.config is config
    assert ctx.db is main.db
    d = ctx.data_dir
    assert d == config.DATA_DIR / "apps-data" / "ctx-test" and d.is_dir()
    assert (d.stat().st_mode & 0o777) == 0o700
    assert ctx.log.name.endswith("ctx-test")


def test_broadcast_requires_the_app_prefix_and_safe_mode_drops_it(env, monkeypatch):
    repo, load, _ = env
    _write_app(repo, "bc-test")
    (la,) = load()
    sent = []

    async def fake(frame):
        sent.append(frame)
    monkeypatch.setattr(main.manager, "broadcast", fake)
    asyncio.run(la.ctx.broadcast({"type": "app:bc-test:changed", "x": 1}))
    assert sent == [{"type": "app:bc-test:changed", "x": 1}]
    for bad in ({"type": "message"}, {"type": "app:other:x"}, {"x": 1}):
        with pytest.raises(ValueError):
            asyncio.run(la.ctx.broadcast(bad))
    assert main.redact_for_decoy({"type": "app:bc-test:changed", "x": 1}) is None
    assert main.redact_for_decoy({"type": "app:jobboard:job_updated", "job": {}}) is None


# --------------------------------------------------------------------------- #
# Bot provisioning (append-only)
# --------------------------------------------------------------------------- #

def test_bot_provisioning_appends_once_and_never_edits(env, caplog):
    import logging
    caplog.set_level(logging.INFO, logger="local-chat.apps")
    repo, load, _ = env
    _write_app(repo, "prov-test", {"bot": {"id": "provbot", "name": "Prov", "emoji": "🎯",
                                           "avatar": "prov-face.png", "agent": "scout"}})
    _write_app(repo, "kept-test", {"bot": {"id": "keptbot", "name": "From Manifest"}})
    config._write_bots([config._bot_entry(b) for b in config.load_bots()]
                       + [config._bot_entry(config.Bot(id="keptbot", name="Operator's",
                                                       emoji="🦊", visible=True))])
    load()
    added = apps_loader.provision_bots()
    # (The repo's own Job Board is mounted too and brings `jobboard`.)
    assert sorted(added) == ["jobboard", "provbot"]
    bots = {b.id: b for b in config.load_bots()}
    p = bots["provbot"]
    assert (p.name, p.emoji, p.avatar, p.agent, p.visible) == (
        "Prov", "🎯", "prov-face.png", "scout", False)
    k = bots["keptbot"]
    assert (k.name, k.emoji, k.visible) == ("Operator's", "🦊", True)
    assert apps_loader.provision_bots() == []
    ids = [b.id for b in config.load_bots()]
    assert ids.count("provbot") == 1
    assert caplog.text.count("provbot") == 1


def test_startup_provisions_the_jobboard_bot(env):
    _, _, make_client = env
    make_client()                                   # lifespan runs provisioning
    jb = config.get_bot("jobboard")
    assert jb is not None and jb.agent == "scout" and jb.visible is False


# --------------------------------------------------------------------------- #
# /api/tools app rows, status, PUT
# --------------------------------------------------------------------------- #

def test_api_tools_lists_app_rows_in_order(env, tmp_path):
    repo, load, make_client = env
    _write_app(repo, "late-test", {"order": 90, "title": "Late"})
    _write_app(repo, "early-test", {"order": 1, "title": "Early",
                                    "bot": {"id": "earlybot"}, "thread_hook": True})
    load()
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text("x")
    _manifest_rows(f"tools:\n  - {{id: bench, title: B, kind: static, root: {site}}}\n")
    c = make_client()
    rows = c.get("/api/tools").json()["tools"]
    kinds = [r["kind"] for r in rows]
    first_app = kinds.index("app")
    assert all(k == "builtin" for k in kinds[:first_app])
    assert kinds[-1] == "static"
    app_ids = [r["id"] for r in rows if r["kind"] == "app"]
    assert app_ids.index("early-test") < app_ids.index("jobboard") < app_ids.index("late-test")
    early = next(r for r in rows if r["id"] == "early-test")
    assert {k: early[k] for k in ("id", "title", "icon", "kind", "enabled", "safe", "order",
                                  "has_refresh", "bot_id", "thread_hook", "entry")} == {
        "id": "early-test", "title": "Early", "icon": "chart", "kind": "app", "enabled": True,
        "safe": False, "order": 1, "has_refresh": False, "bot_id": "earlybot",
        "thread_hook": "/apps/early-test/thread.js", "entry": "/apps/early-test/"}
    late = next(r for r in rows if r["id"] == "late-test")
    assert late["thread_hook"] is None and "bot_id" not in late
    assert "jobboard" not in [r["id"] for r in rows if r["kind"] == "builtin"]


def test_api_tools_status_for_an_app(env):
    repo, load, make_client = env
    _write_app(repo, "stat-test")
    load()
    c = make_client()
    assert c.get("/api/tools/stat-test/status").json() == {
        "id": "stat-test", "kind": "app", "enabled": True, "mounted": True}
    _manifest_rows("tools:\n  - {id: stat-test, kind: app, enabled: false}\n")
    assert c.get("/api/tools/stat-test/status").json()["enabled"] is False
    auth.set_pin("1234")
    assert c.get("/api/tools/stat-test/status", headers=BROWSER).status_code == 403


def test_safe_mode_lists_only_safe_apps(env):
    repo, load, make_client = env
    _write_app(repo, "open-test", {"safe": True})
    _write_app(repo, "closed-test")
    load()
    c = make_client(LOOPBACK)
    auth.set_pin("1234")
    ids = [t["id"] for t in c.get("/api/tools", headers=BROWSER).json()["tools"]]
    assert "open-test" in ids and "closed-test" not in ids and "jobboard" not in ids


def test_put_round_trips_app_rows_and_refuses_extra_fields(env):
    repo, load, make_client = env
    _write_app(repo, "put-test", {"title": "Put"})
    load()
    c = make_client()
    rows = c.get("/api/tools").json()["tools"]
    row = next(r for r in rows if r["id"] == "put-test")
    # The Settings tab sends back what it was given, with the switch flipped.
    row["enabled"] = False
    r = c.put("/api/tools", json={"tools": rows})
    assert r.status_code == 200, r.text
    stored = yaml.safe_load((config.DATA_DIR / "tools.yaml").read_text())["tools"]
    app_row = next(t for t in stored if t["id"] == "put-test")
    assert app_row == {"id": "put-test", "kind": "app", "enabled": False}
    for extra in ({"root": "/tmp"}, {"url": "http://x/"}, {"refresh": {"argv": ["x"]}},
                  {"title": "Renamed"}, {"order": 3}):
        bad = [{"id": "put-test", "kind": "app", "enabled": True, **extra}]
        r = c.put("/api/tools", json={"tools": bad})
        assert r.status_code == 422, (extra, r.text)
    r = c.put("/api/tools", json={"tools": [{"id": "put-test", "kind": "app",
                                               "trusted": True}]})
    assert r.status_code == 422 and r.json()["field"] == "trusted"
    r = c.put("/api/tools", json={"tools": [{"id": "ghost-test", "kind": "app"}]})
    assert r.status_code == 422 and r.json()["field"] == "id"


def test_put_keeps_trusted_and_unlisted_app_rows(env):
    repo, load, make_client = env
    _manifest_rows("tools:\n  - {id: far-test, kind: app, enabled: true, trusted: true}\n")
    c = make_client()
    r = c.put("/api/tools", json={"tools": []})
    assert r.status_code == 200, r.text
    stored = yaml.safe_load((config.DATA_DIR / "tools.yaml").read_text())["tools"]
    assert {"id": "far-test", "kind": "app", "enabled": True, "trusted": True} in stored


def test_static_tool_may_not_take_an_app_id(env, tmp_path):
    repo, load, _ = env
    _write_app(repo, "mine-test")
    load()
    with pytest.raises(tools.ToolValidationError) as ei:
        tools.validate_tools([{"id": "mine-test", "kind": "static", "root": str(tmp_path)}])
    assert ei.value.field == "id"
    with pytest.raises(tools.ToolValidationError):
        tools.validate_tools([{"id": "mine-test", "kind": "app", "title": "x"}])
    ok = tools.validate_tools([{"id": "mine-test", "kind": "app", "enabled": False,
                                "trusted": False}])
    assert ok[0].kind == "app" and ok[0].enabled is False


def test_jobboard_is_no_longer_a_builtin():
    assert "jobboard" not in tools.BUILTINS
    assert "jobs" not in tools.FEATURE_TO_ID
    src = Path(main.__file__).read_text(encoding="utf-8")
    for gone in ("_JOBS_MOUNTED", "JOBS_ENABLED", "jobs_available", "_require_jobs_switch",
                 "from . import jobs", "jobs.router"):
        assert gone not in src, gone
    assert not (Path(main.__file__).parent / "jobs.py").exists()
