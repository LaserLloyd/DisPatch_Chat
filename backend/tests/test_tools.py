"""Tools: the tools.yaml manifest, static serving, refresh, builtin switches.

Contract: docs/design/2026-09-25-tools-plugins.md. Most of this file is
security assertions — a static tool serves bytes off the host's disk, and a
refresh runs a process — so the interesting cases are the refusals: traversal,
symlink escape, dotfiles, secret-shaped names, Safe-Mode callers, a UI that
tries to introduce or change a refresh argv.

Same hermetic style as the rest of tests/: throwaway data dir, nothing touches
the live install. Run: cd backend && uv run pytest -q tests/test_tools.py
"""
from __future__ import annotations

import asyncio
import os
import stat
import sys
import threading
import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app import auth, config, localview, main, tools
from app.database import Database

PY = sys.executable


@pytest.fixture
def tools_env(tmp_path, monkeypatch):
    """Isolated data dir + DB + a scratch site to serve. Yields
    (make_client, site_dir)."""
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
    localview._reset_cache()
    config._invalidate_bots_cache()

    temp_db = Database(data / "chats.db")
    monkeypatch.setattr(main, "db", temp_db)

    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text("<h1>index</h1>")
    (site / "report.html").write_text("<h1>report</h1><link rel=stylesheet href=css/s.css>")
    (site / "css").mkdir()
    (site / "css" / "s.css").write_text("h1{color:red}")
    (site / "data.json").write_text('{"ok": true}')

    clients: list[TestClient] = []

    def make_client(client_addr=("testclient", 50000)) -> TestClient:
        c = TestClient(main.app, client=client_addr)
        c.__enter__()
        clients.append(c)
        return c

    yield make_client, site

    for c in clients:
        c.__exit__(None, None, None)
    tools._reset_state()
    asyncio.run(temp_db.close())


def _manifest(text: str) -> None:
    (config.DATA_DIR / "tools.yaml").write_text(text)
    tools._reset_state()


def _bench(site, **extra) -> str:
    lines = [
        "tools:",
        "  - id: bench",
        "    title: Bench",
        "    icon: '📊'",
        "    kind: static",
        f"    root: {site}",
        "    entry: report.html",
    ]
    for k, v in extra.items():
        lines.append(f"    {k}: {v}")
    return "\n".join(lines) + "\n"


def _unlocked(make_client):
    c = make_client()
    auth.set_pin("1234")
    r = c.post("/api/auth/unlock", json={"pin": "1234"})
    assert r.status_code == 200, r.text
    return c


def _decoy(make_client):
    auth.set_pin("1234")
    return make_client()


# --------------------------------------------------------------------------- #
# Manifest validation
# --------------------------------------------------------------------------- #

def _static(**kw):
    d = {"id": "bench", "title": "Bench", "kind": "static", "root": "/tmp"}
    d.update(kw)
    return d


@pytest.mark.parametrize("entry,field", [
    (_static(id="Bench"), "id"),                     # uppercase
    (_static(id="a" * 41), "id"),                    # too long
    (_static(id="bad_id"), "id"),                    # underscore
    (_static(id=""), "id"),
    ({"id": "bench", "title": "B", "kind": "static"}, "root"),   # missing root
    (_static(root="relative/dir"), "root"),          # relative root
    (_static(root="/"), "root"),                     # whole filesystem
    (_static(colour="red"), "colour"),               # unknown key
    (_static(kind="plugin"), "kind"),
    (_static(entry="../x.html"), "entry"),
    (_static(entry="/abs.html"), "entry"),
    (_static(entry=".hidden.html"), "entry"),
    (_static(enabled="yes"), "enabled"),
    (_static(refresh={"argv": "uv run x"}), "refresh.argv"),     # shell string
    (_static(refresh={"argv": []}), "refresh.argv"),
    (_static(refresh={"argv": ["x"], "timeout_s": 4000}), "refresh.timeout_s"),
    (_static(refresh={"argv": ["x"], "cwd": "rel"}), "refresh.cwd"),
    (_static(refresh={"argv": ["x"], "shell": True}), "refresh.shell"),
    ({"id": "web", "title": "W", "kind": "url", "url": "javascript:alert(1)"}, "url"),
    ({"id": "web", "title": "W", "kind": "url", "url": "ftp://x/"}, "url"),
    ({"id": "web", "title": "W", "kind": "url", "url": "http://x/", "remote_url": "javascript:x"}, "remote_url"),
    ({"id": "web", "title": "W", "kind": "url", "url": "http://x/", "remote_url": 7}, "remote_url"),
    (_static(remote_url="https://x/"), "remote_url"),  # url tools only
    ({"id": "web", "title": "W", "kind": "url", "url": "http://x/", "refresh": {"argv": ["x"]}},
     "refresh"),                                     # refresh is static-only
    ({"id": "deepseek-harness", "kind": "builtin", "enabled": False, "title": "x"}, "title"),
    ({"id": "not-a-builtin", "kind": "builtin"}, "id"),
    (_static(id="mail-panel"), "id"),                # builtin id on a static tool
    (_static(id="main"), "id"),                      # collides with a bot id
])
def test_validation_refuses(tools_env, entry, field):
    with pytest.raises(tools.ToolValidationError) as ei:
        tools.validate_tools([entry], check_fs=False)
    assert ei.value.index == 0
    assert ei.value.field == field, str(ei.value)


def test_validation_duplicate_id_names_second_index(tools_env):
    with pytest.raises(tools.ToolValidationError) as ei:
        tools.validate_tools([_static(), _static()], check_fs=False)
    assert ei.value.index == 1 and ei.value.field == "id"


def test_validation_fs_checks_root_exists(tools_env, tmp_path):
    with pytest.raises(tools.ToolValidationError) as ei:
        tools.validate_tools([_static(root=str(tmp_path / "nope"))], check_fs=True)
    assert ei.value.field == "root"


@pytest.mark.parametrize("sub,msg", [
    (None, "never served"),                    # /etc
    ("", tools.DATA_DIR_MSG),                  # the data dir itself
    ("files", tools.DATA_DIR_MSG),             # anything under it
    ("media/x", tools.DATA_DIR_MSG),
])
def test_validation_refuses_root_in_deny_tree(tools_env, sub, msg):
    if sub is None:
        root = "/etc"
    else:
        root = str(config.DATA_DIR / sub) if sub else str(config.DATA_DIR)
        (config.DATA_DIR / sub).mkdir(parents=True, exist_ok=True)
    with pytest.raises(tools.ToolValidationError) as ei:
        tools.validate_tools([_static(root=root)], check_fs=True)
    assert ei.value.field == "root"
    assert msg in ei.value.message


def test_validation_refuses_data_dir_via_symlink(tools_env, tmp_path):
    link = tmp_path / "innocent"
    link.symlink_to(config.DATA_DIR)
    with pytest.raises(tools.ToolValidationError) as ei:
        tools.validate_tools([_static(root=str(link))], check_fs=False)
    assert ei.value.message == tools.DATA_DIR_MSG


def test_hand_edited_safe_tool_at_data_dir_serves_nothing(tools_env, monkeypatch):
    """tools.yaml is read with check_fs=False, so validation alone does not
    stop a hand edit. A `safe: true` tool rooted at the data dir would hand
    files/, media/ and tools.yaml to a cookieless Safe-Mode device — the
    resolver refuses it at serve time too."""
    make_client, site = tools_env
    data = config.DATA_DIR
    (data / "files").mkdir(exist_ok=True)
    (data / "files" / "family.txt").write_text("private")
    (data / "index.html").write_text("<h1>data</h1>")
    # Bypass validation: the dataclass is what load_tools would hand back if a
    # future change loosened the check — serve-time must hold on its own.
    t = tools.Tool(id="leak", kind="static", title="Leak", safe=True, root=str(data),
                   entry="index.html")
    for rel in ("index.html", "files/family.txt", "tools.yaml"):
        with pytest.raises(tools.ToolNotFound):
            tools.resolve_static(t, rel)
    # And through HTTP, as Safe Mode, with the manifest written by hand...
    _manifest(f"tools:\n  - id: leak\n    title: L\n    kind: static\n"
              f"    root: {data}\n    safe: true\n")
    # The data-dir rule is a shape rule, so even the check_fs=False read refuses
    # the file (fail closed: no tools until it is fixed)...
    assert tools.load_tools() == []
    c = _decoy(make_client)
    for rel in ("", "files/family.txt", "tools.yaml"):
        r = c.get(f"/tools/leak/{rel}")
        assert r.status_code in (403, 404), (rel, r.status_code)
        assert "private" not in r.text and "tools:" not in r.text
    # ...and if a loosened loader did hand the tool through, the Safe-Mode
    # route still serves nothing (the resolver is the last line).
    monkeypatch.setattr(tools, "load_tools", lambda: [t])
    for rel in ("", "files/family.txt", "tools.yaml"):
        r = c.get(f"/tools/leak/{rel}")
        assert r.status_code == 404, (rel, r.status_code)
        assert "private" not in r.text and "tools:" not in r.text


def test_validation_accepts_the_spec_example(tools_env, tmp_path):
    out = tools.validate_tools([
        _static(root=str(tmp_path), entry="report.html",
                refresh={"argv": ["uv", "run", "x"], "cwd": str(tmp_path), "timeout_s": 600}),
        {"id": "sf-web", "title": "SF", "icon": "🎛️", "kind": "url", "url": "http://192.0.2.5:8080/"},
        {"id": "deepseek-harness", "kind": "builtin", "enabled": False},
    ], check_fs=True)
    assert [t.id for t in out] == ["bench", "sf-web", "deepseek-harness"]
    assert out[0].refresh.timeout_s == 600
    assert out[0].enabled is True and out[0].safe is False
    assert out[2].enabled is False


def test_absent_manifest_is_empty(tools_env):
    assert tools.load_tools() == []


def test_malformed_manifest_is_empty_and_app_stays_up(tools_env):
    make_client, site = tools_env
    _manifest("tools: [ {id: bench, kind: static\n  :::")
    assert tools.load_tools() == []
    _manifest("tools:\n  - id: BAD\n    kind: static\n")    # parses, fails validation
    assert tools.load_tools() == []
    c = make_client()
    r = c.get("/api/tools")
    assert r.status_code == 200
    # Only what does not come from tools.yaml: the builtins and the repo's apps.
    assert all(t["kind"] in ("builtin", "app") for t in r.json()["tools"])


def test_manifest_reload_on_mtime_change(tools_env):
    make_client, site = tools_env
    _manifest(_bench(site))
    assert [t.id for t in tools.load_tools()] == ["bench"]
    p = config.DATA_DIR / "tools.yaml"
    p.write_text(_bench(site).replace("id: bench", "id: bench2"))
    os.utime(p, (time.time() + 5, time.time() + 5))
    assert [t.id for t in tools.load_tools()] == ["bench2"]


# --------------------------------------------------------------------------- #
# Static serving (no PIN: the app is open)
# --------------------------------------------------------------------------- #

def test_static_entry_and_nested_path(tools_env):
    make_client, site = tools_env
    _manifest(_bench(site))
    c = make_client()
    r = c.get("/tools/bench/")
    assert r.status_code == 200 and "report" in r.text
    assert r.headers["content-type"].startswith("text/html")
    r = c.get("/tools/bench/css/s.css")
    assert r.status_code == 200 and "color:red" in r.text
    assert r.headers["content-type"].startswith("text/css")
    r = c.get("/tools/bench/index.html")
    assert r.status_code == 200 and "index" in r.text


def test_static_bare_id_redirects_to_slash(tools_env):
    make_client, site = tools_env
    _manifest(_bench(site))
    c = make_client()
    r = c.get("/tools/bench", follow_redirects=False)
    assert r.status_code in (307, 308)
    assert r.headers["location"].endswith("/tools/bench/")


def test_static_csp_and_cache_headers(tools_env):
    make_client, site = tools_env
    _manifest(_bench(site))
    c = make_client()
    r = c.get("/tools/bench/")
    csp = r.headers["content-security-policy"]
    assert "sandbox allow-scripts allow-forms allow-popups" in csp
    assert "allow-same-origin" not in csp
    assert "frame-ancestors 'self'" in csp
    assert "x-frame-options" not in {k.lower() for k in r.headers}
    assert r.headers["cache-control"] == "private, max-age=0, must-revalidate"
    assert r.headers["x-content-type-options"] == "nosniff"
    r = c.get("/tools/bench/css/s.css")
    assert "sandbox" in r.headers["content-security-policy"]
    assert r.headers["cache-control"] == "private, max-age=0, must-revalidate"
    # Refusals carry it too (set by main's header middleware).
    r = c.get("/tools/bench/nope.css")
    assert r.status_code == 404
    assert r.headers["cache-control"] == "private, max-age=0, must-revalidate"


def test_static_head_works(tools_env):
    make_client, site = tools_env
    _manifest(_bench(site))
    c = make_client()
    r = c.head("/tools/bench/css/s.css")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/css")
    assert r.content == b""
    assert c.head("/tools/bench/nope.css").status_code == 404


@pytest.mark.parametrize("rel", [
    "%2e%2e/secret.txt",
    "css/%2e%2e/%2e%2e/secret.txt",
    "..%2fsecret.txt",
    ".hidden.html",
    "sub/.git/config",
    ".env",
    "prod.env",
    "server.pem",
    "id_rsa",
    "my-secret-notes.txt",
    "chats.db",
    "security.yaml",
    "trusted-devices.yaml",
    "nope.html",
    "escape.txt",          # symlink out of the root
    "escdir/x.txt",        # symlinked dir out of the root
])
def test_static_refusals_are_uniform_404(tools_env, tmp_path, rel):
    make_client, site = tools_env
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x.txt").write_text("outside")
    (tmp_path / "secret.txt").write_text("secret")
    (site / ".hidden.html").write_text("h")
    (site / "sub" / ".git").mkdir(parents=True)
    (site / "sub" / ".git" / "config").write_text("c")
    for n in (".env", "prod.env", "server.pem", "id_rsa", "my-secret-notes.txt", "chats.db",
              "security.yaml", "trusted-devices.yaml"):
        (site / n).write_text("s")
    (site / "escape.txt").symlink_to(outside / "x.txt")
    (site / "escdir").symlink_to(outside)
    _manifest(_bench(site))
    c = make_client()
    r = c.get(f"/tools/bench/{rel}")
    assert r.status_code == 404, (rel, r.status_code, r.text[:200])
    assert "secret" not in r.text and "outside" not in r.text
    assert r.json() == {"detail": "Not found"}


def test_static_resolver_refuses_dotdot_directly(tools_env):
    make_client, site = tools_env
    _manifest(_bench(site))
    tool = tools.get_tool("bench")
    for rel in ("../x", "a/../../x", "/etc/passwd", "a\\b", "a\x00b"):
        with pytest.raises(tools.ToolNotFound):
            tools.resolve_static(tool, rel)


def test_static_unknown_disabled_and_url_tools_404(tools_env):
    make_client, site = tools_env
    _manifest(_bench(site, enabled="false") +
              "  - id: web\n    title: W\n    kind: url\n    url: http://example.invalid/\n")
    c = make_client()
    assert c.get("/tools/bench/").status_code == 404
    assert c.get("/tools/web/").status_code == 404
    assert c.get("/tools/nope/").status_code == 404


def test_static_unknown_type_downloads(tools_env):
    make_client, site = tools_env
    (site / "blob.bin").write_bytes(b"\x00\x01")
    _manifest(_bench(site))
    r = make_client().get("/tools/bench/blob.bin")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/octet-stream"
    assert "attachment" in r.headers["content-disposition"]


def test_static_size_cap(tools_env, monkeypatch):
    make_client, site = tools_env
    monkeypatch.setattr(tools, "MAX_FILE_BYTES", 10)
    (site / "big.txt").write_text("x" * 11)
    _manifest(_bench(site))
    assert make_client().get("/tools/bench/big.txt").status_code == 404


# --------------------------------------------------------------------------- #
# With a PIN: the operator's framed page gets a cookieless ticket URL
# --------------------------------------------------------------------------- #

def test_pin_operator_entry_redirects_to_ticket_and_subresources_work(tools_env):
    make_client, site = tools_env
    _manifest(_bench(site))
    c = _unlocked(make_client)
    r = c.get("/tools/bench/", follow_redirects=False)
    assert r.status_code == 307
    loc = r.headers["location"]
    assert loc.startswith(tools.TICKET_PREFIX)
    r = c.get(loc)
    assert r.status_code == 200 and "report" in r.text
    assert "sandbox allow-scripts" in r.headers["content-security-policy"]
    # A sandboxed (opaque-origin) page's subresources carry NO cookie — the
    # ticket path is what authenticates them.
    base = loc.rsplit("/", 1)[0]
    bare = make_client()
    r = bare.get(base + "/css/s.css")
    assert r.status_code == 200 and "color:red" in r.text
    r = bare.get(base + "/data.json")
    assert r.headers.get("access-control-allow-origin") == "*"
    # Every guard still applies below the ticket.
    (site / ".env").write_text("SECRET")
    assert bare.get(base + "/.env").status_code == 404
    assert bare.get(base + "/%2e%2e/x").status_code == 404
    # A made-up ticket or one presented by another client is refused.
    assert bare.get(tools.TICKET_PREFIX + "nope/report.html").status_code == 404
    other = make_client(("203.0.113.9", 1))
    assert other.get(base + "/css/s.css").status_code == 404


def test_pin_ticket_only_for_framed_loads(tools_env):
    """Sec-Fetch-Dest iframe/frame (or absent) → ticket redirect; document (a
    top-level "open in new tab") → served directly on the cookie, no ticket."""
    make_client, site = tools_env
    _manifest(_bench(site))
    c = _unlocked(make_client)
    for dest in ("iframe", "frame"):
        r = c.get("/tools/bench/", headers={"Sec-Fetch-Dest": dest}, follow_redirects=False)
        assert r.status_code == 307, dest
        assert r.headers["location"].startswith(tools.TICKET_PREFIX)
    n = len(tools._tickets)
    r = c.get("/tools/bench/", headers={"Sec-Fetch-Dest": "document"}, follow_redirects=False)
    assert r.status_code == 200 and "report" in r.text
    assert len(tools._tickets) == n                  # nothing minted
    # HEAD on a ticketed page works too.
    r = c.get("/tools/bench/", headers={"Sec-Fetch-Dest": "iframe"}, follow_redirects=False)
    assert c.head(r.headers["location"]).status_code == 200


def test_pin_decoy_cannot_reach_nonsafe_tool(tools_env):
    make_client, site = tools_env
    _manifest(_bench(site))
    c = _decoy(make_client)
    for p in ("/tools/bench/", "/tools/bench/css/s.css", "/tools/nope/",
              "/api/tools/bench/status"):
        r = c.get(p)
        assert r.status_code == 403, (p, r.status_code)
        assert "report" not in r.text


# --------------------------------------------------------------------------- #
# GET /api/tools + Safe Mode
# --------------------------------------------------------------------------- #

def test_api_tools_operator_view(tools_env):
    make_client, site = tools_env
    _manifest(_bench(site, refresh="{argv: [echo, hi], timeout_s: 5}"))
    c = make_client()                       # no PIN → open app → operator view
    body = c.get("/api/tools").json()
    assert body["path"] == str(config.DATA_DIR / "tools.yaml")
    ids = [t["id"] for t in body["tools"]]
    assert ids[:4] == ["deepseek-harness", "studioforge-panel", "mail-panel", "clients-panel"]
    bench = next(t for t in body["tools"] if t["id"] == "bench")
    assert bench["root"] == str(site) and bench["entry"] == "report.html"
    assert bench["has_refresh"] is True
    assert bench["refresh"]["argv"] == ["echo", "hi"]
    feats = {t["id"]: t.get("builtin_feature") for t in body["tools"] if t["kind"] == "builtin"}
    assert feats == {"deepseek-harness": "harness", "studioforge-panel": "studioforge",
                     "mail-panel": "mail", "clients-panel": "practice"}


def test_safe_tool_visible_in_safe_mode_stripped(tools_env):
    make_client, site = tools_env
    _manifest(_bench(site, refresh="{argv: [echo, hi]}")
              + f"  - id: pub\n    title: Pub\n    kind: static\n    root: {site}\n    safe: true\n"
              + f"    refresh: {{argv: [echo, x], cwd: {site}}}\n"
              + "  - id: pubweb\n    title: PW\n    kind: url\n    url: http://example.invalid/\n"
              + "    safe: true\n"
              + f"  - id: offsafe\n    title: O\n    kind: static\n    root: {site}\n"
              + "    safe: true\n    enabled: false\n")
    c = _decoy(make_client)
    r = c.get("/api/tools")
    assert r.status_code == 200
    body = r.json()
    assert "path" not in body
    assert [t["id"] for t in body["tools"]] == ["pub", "pubweb"]
    for t in body["tools"]:
        for banned in ("root", "refresh", "cwd", "argv"):
            assert banned not in t
        assert t["has_refresh"] is False
    assert body["tools"][1]["url"] == "http://example.invalid/"
    # The safe static tool is served to the Safe-Mode browser.
    r = c.get("/tools/pub/")
    assert r.status_code == 200 and "index" in r.text
    assert c.get("/tools/pub/css/s.css").status_code == 200
    st = c.get("/api/tools/pub/status")
    assert st.status_code == 200 and "last_refresh" in st.json()
    # ...but never its refresh or the write path.
    assert c.post("/api/tools/pub/refresh").status_code == 403
    assert c.put("/api/tools", json={"tools": []}).status_code == 403
    assert c.get("/tools/offsafe/").status_code == 403


def test_auth_status_features_tools(tools_env):
    make_client, site = tools_env
    c = _unlocked(make_client)
    assert c.get("/api/auth/status").json()["features"]["tools"] is True


# --------------------------------------------------------------------------- #
# Builtin switches
# --------------------------------------------------------------------------- #

def test_builtin_enabled_lookup(tools_env):
    make_client, site = tools_env
    assert tools.builtin_enabled("deepseek-harness") is None
    _manifest("tools:\n  - {id: deepseek-harness, kind: builtin, enabled: false}\n"
              "  - {id: mail-panel, kind: builtin}\n")
    assert tools.builtin_enabled("deepseek-harness") is False
    assert tools.builtin_enabled("mail-panel") is True
    assert tools.builtin_enabled("clients-panel") is None


def test_builtin_disabled_turns_harness_off(tools_env, monkeypatch):
    make_client, site = tools_env
    monkeypatch.setattr(main, "SETTINGS", replace(config.SETTINGS, harness_enabled=True))
    c = _unlocked(make_client)
    assert c.get("/api/auth/status").json()["features"]["harness"] is True
    before = c.get("/api/harness/status").status_code
    assert before != 404
    _manifest("tools:\n  - {id: deepseek-harness, kind: builtin, enabled: false}\n")
    assert c.get("/api/harness/status").status_code == 404
    assert c.get("/api/auth/status").json()["features"]["harness"] is False
    row = next(t for t in c.get("/api/tools").json()["tools"] if t["id"] == "deepseek-harness")
    assert row["enabled"] is False and row["available"] is True


def test_builtin_disabled_harness_is_silent_on_the_socket(tools_env, monkeypatch):
    """A pane tools.yaml switched off must not keep pushing its WS frames."""
    frames: list[dict] = []

    class FakeManager:
        async def broadcast(self, frame):
            frames.append(frame)

    monkeypatch.setattr(main, "manager", FakeManager())
    monkeypatch.setattr(main, "_shutting_down", False)

    async def fire():
        main._harness_state_changed({"running": False})
        main._harness_sessions_changed([])
        await asyncio.sleep(0.05)

    asyncio.run(fire())
    assert [f["type"] for f in frames] == ["harness_state", "harness_sessions"]
    frames.clear()
    _manifest("tools:\n  - {id: deepseek-harness, kind: builtin, enabled: false}\n")
    asyncio.run(fire())
    assert frames == []


def test_builtin_disabled_studioforge_leaves_the_shell_csp(tools_env, monkeypatch):
    monkeypatch.setattr(main, "SETTINGS", replace(
        config.SETTINGS, studioforge_enabled=True, studioforge_url="http://198.51.100.7:8080"))
    assert main._studioforge_origin() == "http://198.51.100.7:8080"
    _manifest("tools:\n  - {id: studioforge-panel, kind: builtin, enabled: false}\n")
    assert main._studioforge_origin() == ""
    html = tools_env[0]().get("/").text
    assert "198.51.100.7" not in html


@pytest.mark.parametrize("tool_id,feature,flag,path", [
    ("studioforge-panel", "studioforge", "studioforge_enabled", "/api/studioforge/status"),
    ("mail-panel", "mail", "mail_enabled", "/api/mail/status"),
    ("clients-panel", "practice", "practice_enabled", "/api/practice/status"),
])
def test_builtin_disabled_turns_others_off(tools_env, monkeypatch, tool_id, feature, flag, path):
    make_client, site = tools_env
    monkeypatch.setattr(main, "SETTINGS", replace(
        config.SETTINGS, **{flag: True, "studioforge_url": "http://127.0.0.1:9/"}))
    c = _unlocked(make_client)
    assert c.get("/api/auth/status").json()["features"][feature] is True
    _manifest(f"tools:\n  - {{id: {tool_id}, kind: builtin, enabled: false}}\n")
    assert c.get("/api/auth/status").json()["features"][feature] is False
    assert c.get(path).status_code == 404


# --------------------------------------------------------------------------- #
# Refresh
# --------------------------------------------------------------------------- #

def _refresh_manifest(site, code: str, timeout: int = 30) -> None:
    import json
    argv = json.dumps([PY, "-c", code])
    _manifest(_bench(site, refresh=f"{{argv: {argv}, cwd: {site}, timeout_s: {timeout}}}"))


def test_refresh_runs_fixed_argv(tools_env):
    make_client, site = tools_env
    _refresh_manifest(site, "import pathlib; pathlib.Path('report.html').write_text('<b>new</b>');"
                            " print('done')")
    c = make_client()
    r = c.post("/api/tools/bench/refresh")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["rc"] == 0 and "done" in body["stdout_tail"]
    assert set(body) >= {"rc", "seconds", "stdout_tail", "stderr_tail"}
    assert "new" in c.get("/tools/bench/").text
    st = c.get("/api/tools/bench/status").json()
    assert st["last_refresh"]["rc"] == 0 and st["refreshing"] is False
    assert st["mtime"] is not None


def test_refresh_env_is_scrubbed(tools_env, monkeypatch):
    make_client, site = tools_env
    monkeypatch.setenv("FOO_TOKEN", "t0k")
    monkeypatch.setenv("BAR_API_KEY", "k3y")
    monkeypatch.setenv("BAZ_SECRET", "s3c")
    monkeypatch.setenv("MCP_PIN", "1111")
    monkeypatch.setenv("HARMLESS_VALUE", "ok")
    capab = ("SSH_AUTH_SOCK", "XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS",
             "WAYLAND_DISPLAY", "DISPLAY")
    for k in capab:
        monkeypatch.setenv(k, "live")
    names = ("FOO_TOKEN", "BAR_API_KEY", "BAZ_SECRET", "MCP_PIN", "HARMLESS_VALUE", *capab)
    _refresh_manifest(site, "import os; print(sorted(k for k in os.environ if k in "
                            f"{names!r}))")
    out = make_client().post("/api/tools/bench/refresh").json()["stdout_tail"]
    assert "HARMLESS_VALUE" in out
    for k in ("FOO_TOKEN", "BAR_API_KEY", "BAZ_SECRET", "MCP_PIN", *capab):
        assert f"'{k}'" not in out, k
    env = tools.scrubbed_env()
    assert not set(capab) & set(env)


def test_refresh_timeout_is_rc_124(tools_env):
    make_client, site = tools_env
    _refresh_manifest(site, "import time; time.sleep(30)", timeout=1)
    t0 = time.monotonic()
    body = make_client().post("/api/tools/bench/refresh").json()
    assert body["rc"] == 124
    assert time.monotonic() - t0 < 15


def test_refresh_409_while_running(tools_env):
    make_client, site = tools_env
    _refresh_manifest(site, "import time; time.sleep(2)")
    c = make_client()
    results = {}

    def first():
        results["first"] = c.post("/api/tools/bench/refresh").status_code

    th = threading.Thread(target=first)
    th.start()
    deadline = time.monotonic() + 5
    while not tools.is_refreshing("bench") and time.monotonic() < deadline:
        time.sleep(0.02)
    assert c.get("/api/tools/bench/status").json()["refreshing"] is True
    assert c.post("/api/tools/bench/refresh").status_code == 409
    th.join()
    assert results["first"] == 200


def test_refresh_404_without_refresh_block(tools_env):
    make_client, site = tools_env
    _manifest(_bench(site))
    c = make_client()
    assert c.post("/api/tools/bench/refresh").status_code == 404
    assert c.post("/api/tools/nope/refresh").status_code == 404


def test_refresh_missing_binary_reports_rc(tools_env):
    make_client, site = tools_env
    _manifest(_bench(site, refresh="{argv: [/nonexistent/binary-xyz]}"))
    body = make_client().post("/api/tools/bench/refresh").json()
    assert body["rc"] == 127


# --------------------------------------------------------------------------- #
# PUT /api/tools
# --------------------------------------------------------------------------- #

def test_put_writes_atomically_0600(tools_env):
    make_client, site = tools_env
    c = _unlocked(make_client)
    r = c.put("/api/tools", json={"tools": [
        {"id": "bench", "title": "Bench", "icon": "📊", "kind": "static",
         "root": str(site), "entry": "report.html"},
        {"id": "deepseek-harness", "kind": "builtin", "enabled": False},
    ]})
    assert r.status_code == 200, r.text
    p = config.DATA_DIR / "tools.yaml"
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert [t.id for t in tools.load_tools()] == ["bench", "deepseek-harness"]
    assert any(t["id"] == "bench" for t in r.json()["tools"])
    assert tools.builtin_enabled("deepseek-harness") is False


def test_put_roundtrips_get_output(tools_env):
    """The Settings tab may send back what GET gave it (ToolOut rows, builtin
    decoration included). Output-only keys are dropped, not refused."""
    make_client, site = tools_env
    _manifest(_bench(site, refresh="{argv: [echo, hi]}"))
    c = _unlocked(make_client)
    rows = c.get("/api/tools").json()["tools"]
    for row in rows:
        if row["id"] == "deepseek-harness":
            row["enabled"] = False
    r = c.put("/api/tools", json={"tools": rows})
    assert r.status_code == 200, r.text
    assert tools.builtin_enabled("deepseek-harness") is False
    assert tools.get_tool("bench").refresh.argv == ("echo", "hi")


def test_put_cannot_introduce_or_change_refresh(tools_env):
    make_client, site = tools_env
    _manifest(_bench(site, refresh="{argv: [echo, hi]}"))
    c = _unlocked(make_client)
    base = {"id": "bench", "title": "Bench", "kind": "static", "root": str(site)}
    r = c.put("/api/tools", json={"tools": [{**base, "refresh": {"argv": ["rm", "-rf", "/"]}}]})
    assert r.status_code == 422
    assert r.json()["field"] == "refresh" and r.json()["index"] == 0
    r = c.put("/api/tools", json={"tools": [
        {**base, "id": "new", "refresh": {"argv": ["echo"]}}]})
    assert r.status_code == 422 and r.json()["field"] == "refresh"
    # Omitted = keep the stored block (the UI never sees argv as editable).
    r = c.put("/api/tools", json={"tools": [{**base, "title": "Renamed"}]})
    assert r.status_code == 200, r.text
    t = tools.get_tool("bench")
    assert t.title == "Renamed" and t.refresh.argv == ("echo", "hi")
    # Sent back identical = fine.
    r = c.put("/api/tools", json={"tools": [{**base, "refresh": {"argv": ["echo", "hi"]}}]})
    assert r.status_code == 200, r.text


def test_put_validation_422_names_index_and_field(tools_env):
    make_client, site = tools_env
    c = _unlocked(make_client)
    r = c.put("/api/tools", json={"tools": [
        {"id": "ok", "title": "O", "kind": "static", "root": str(site)},
        {"id": "bad", "title": "B", "kind": "static", "root": "relative"},
    ]})
    assert r.status_code == 422
    assert r.json()["index"] == 1 and r.json()["field"] == "root"
    assert not (config.DATA_DIR / "tools.yaml").exists()


def test_put_requires_operator_session(tools_env):
    make_client, site = tools_env
    auth.set_pin("1234")
    c = make_client()
    assert c.put("/api/tools", json={"tools": []}).status_code == 403
    # A loopback on-box machine caller is not the operator either.
    lo = make_client(("127.0.0.1", 5555))
    assert lo.put("/api/tools", json={"tools": []}).status_code == 403
    assert lo.post("/api/tools/bench/refresh").status_code == 403


# --------------------------------------------------------------------------- #
# Status
# --------------------------------------------------------------------------- #

def test_status_url_tool_reports_reachable(tools_env, monkeypatch):
    make_client, site = tools_env
    _manifest("tools:\n  - id: web\n    title: W\n    kind: url\n    url: http://example.invalid/\n")
    calls = []

    async def fake_probe(url):
        calls.append(url)
        return True

    monkeypatch.setattr(tools, "_probe_url", fake_probe)
    st = make_client().get("/api/tools/web/status").json()
    assert st["reachable"] is True and st["kind"] == "url"
    assert calls == ["http://example.invalid/"]


def test_status_unknown_tool_404(tools_env):
    make_client, site = tools_env
    assert make_client().get("/api/tools/nope/status").status_code == 404


# --------------------------------------------------------------------------- #
# URL probe: body-less, no redirects, bounded in total
# --------------------------------------------------------------------------- #

class _ProbeServer:
    """A tiny local HTTP server. mode: 'head405' (405 on HEAD, 200 + a large
    body on GET), 'slow' (accepts, then dribbles one header byte a second)."""

    def __init__(self, mode):
        import socket
        self.mode = mode
        self.requests: list[str] = []
        self.body_bytes_sent = 0
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.stop = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        self.sock.settimeout(0.2)
        while not self.stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                continue
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        with conn:
            conn.settimeout(10)
            try:
                req = conn.recv(4096).decode("latin-1")
            except OSError:
                return
            method = req.split(" ", 1)[0]
            self.requests.append(method)
            try:
                if self.mode == "slow":
                    for ch in b"HTTP/1.1 200 OK\r\n":
                        if self.stop.is_set():
                            return
                        conn.sendall(bytes([ch]))
                        time.sleep(1.0)
                    return
                if method == "HEAD":
                    # One request per connection: say so, or the client may
                    # reuse a socket this handler is about to close.
                    conn.sendall(b"HTTP/1.1 405 Method Not Allowed\r\n"
                             b"Content-Length: 0\r\nConnection: close\r\n\r\n")
                    return
                size = 50 * 1024 * 1024
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/octet-stream\r\n"
                             + f"Content-Length: {size}\r\n\r\n".encode())
                chunk = b"x" * 65536
                for _ in range(size // len(chunk)):
                    conn.sendall(chunk)
                    self.body_bytes_sent += len(chunk)
            except OSError:
                return

    def close(self):
        self.stop.set()
        self.sock.close()


def test_probe_head_405_falls_back_without_reading_body():
    srv = _ProbeServer("head405")
    try:
        t0 = time.monotonic()
        ok = asyncio.run(tools._probe_url(f"http://127.0.0.1:{srv.port}/"))
        assert ok is True
        assert srv.requests[:2] == ["HEAD", "GET"]
        assert time.monotonic() - t0 < 4.5
        # The GET stream was closed after the headers: nowhere near the 50 MB
        # body was pushed (the socket buffer absorbs a little before blocking).
        time.sleep(0.2)
        assert srv.body_bytes_sent < 20 * 1024 * 1024
    finally:
        srv.close()


def test_probe_is_bounded_in_total_time():
    """httpx's 3 s timeout is per operation; a server trickling a byte a second
    never trips it. The overall guard does."""
    srv = _ProbeServer("slow")
    try:
        t0 = time.monotonic()
        ok = asyncio.run(tools._probe_url(f"http://127.0.0.1:{srv.port}/"))
        took = time.monotonic() - t0
        assert ok is False
        assert took < tools.PROBE_TOTAL_S + 1.0, took
    finally:
        srv.close()


def test_probe_never_follows_redirects(monkeypatch):
    seen = []

    class FakeClient:
        def __init__(self, *a, **kw):
            seen.append(kw)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def head(self, url):
            class R:
                status_code = 302
            return R()

    monkeypatch.setattr(tools.httpx, "AsyncClient", FakeClient)
    assert asyncio.run(tools._probe_url("http://example.invalid/")) is True
    assert seen[0]["follow_redirects"] is False


def test_builtin_row_enabled_is_the_switch_not_availability(tools_env, monkeypatch):
    """A Settings save round-trips `enabled`; it must be the manifest switch, so an
    unavailable feature never gets `enabled: false` written into tools.yaml."""
    make_client, site = tools_env
    monkeypatch.setattr(main, "SETTINGS", replace(config.SETTINGS, harness_enabled=False))
    c = _unlocked(make_client)
    row = next(t for t in c.get("/api/tools").json()["tools"] if t["id"] == "deepseek-harness")
    assert row["available"] is False and row["enabled"] is True
    rows = c.get("/api/tools").json()["tools"]
    assert c.put("/api/tools", json={"tools": rows}).status_code == 200
    path = config.DATA_DIR / "tools.yaml"
    text = path.read_text() if path.exists() else ""
    assert "enabled: false" not in text


# --------------------------------------------------------------------------- #
# The Job Board was a builtin tool for one day (2026-09-25) and is now an APP
# (apps/jobboard/). A `builtin` row naming it is refused like any other
# unknown builtin; its switch is an `app` row. See tests/test_apps_loader.py.
# --------------------------------------------------------------------------- #

def test_jobboard_builtin_row_is_refused(tools_env):
    with pytest.raises(tools.ToolValidationError):
        tools.validate_tools([{"id": "jobboard", "kind": "builtin", "enabled": False}])


def test_jobs_stay_decoy_blocked(tools_env):
    """The Safe-Mode matrix did not change: /api/jobs is still barred for a
    locked browser, and the builtin never lists for Safe Mode."""
    make_client, site = tools_env
    assert main._decoy_blocked("GET", "/api/jobs") is True
    assert main._decoy_blocked("GET", "/api/jobs/months") is True
    c = _decoy(make_client)
    ids = [t["id"] for t in c.get("/api/tools").json()["tools"]]
    assert "jobboard" not in ids


# --------------------------------------------------------------------------- #
# url tools: the optional off-host address (remote_url)
# --------------------------------------------------------------------------- #

def test_url_tool_remote_url_round_trips(tools_env):
    make_client, _site = tools_env
    c = _unlocked(make_client)
    r = c.put("/api/tools", json={"tools": [
        {"id": "rig", "title": "Rig", "kind": "url", "url": "http://127.0.0.1:3080/",
         "remote_url": "https://host.tailnet.example:8452/"},
        {"id": "plain", "title": "Plain", "kind": "url", "url": "https://example.org/"},
    ]})
    assert r.status_code == 200, r.text
    t = tools.get_tool("rig")
    assert t.remote_url == "https://host.tailnet.example:8452/"
    assert t.to_manifest()["remote_url"] == "https://host.tailnet.example:8452/"
    assert "remote_url" not in tools.get_tool("plain").to_manifest(), "absent stays absent in the file"
    rows = {x["id"]: x for x in c.get("/api/tools").json()["tools"]}
    assert rows["rig"]["remote_url"] == "https://host.tailnet.example:8452/"
    assert rows["plain"]["remote_url"] is None
    # GET's rows go straight back (null remote_url included).
    assert c.put("/api/tools", json={"tools": list(rows.values())}).status_code == 200
    assert tools.get_tool("rig").remote_url == "https://host.tailnet.example:8452/"


# --------------------------------------------------------------------------- #
# `open`: frame (default) or window — a tool that cannot be embedded launches
# in its own tab instead of drawing an empty pane (2026-09-26). StudioForge's
# panel sends `X-Frame-Options: DENY` + `frame-ancestors 'none'`, so its
# builtin defaults to `window`.
# --------------------------------------------------------------------------- #

SF_URL = "http://198.51.100.7:8080/"


def _sf_on(monkeypatch, **extra):
    monkeypatch.setattr(main, "SETTINGS", replace(
        config.SETTINGS, studioforge_enabled=True, studioforge_url=SF_URL, **extra))


def test_open_field_validation(tools_env):
    ok = tools.validate_tools([
        {"id": "web", "title": "W", "kind": "url", "url": "https://example.org/", "open": "window"},
        {"id": "web2", "title": "W2", "kind": "url", "url": "https://example.org/"},
        {"id": "studioforge-panel", "kind": "builtin"},
        {"id": "deepseek-harness", "kind": "builtin", "open": "window"},
        {"id": "mail-panel", "kind": "builtin", "open": "frame"},
    ], check_fs=False)
    by = {t.id: t for t in ok}
    assert by["web"].open == "window"
    assert by["web2"].open == "frame"
    assert by["studioforge-panel"].open == "window", "StudioForge defaults to a new window"
    assert by["deepseek-harness"].open == "window"
    assert by["mail-panel"].open == "frame"


@pytest.mark.parametrize("row, field", [
    ({"id": "web", "title": "W", "kind": "url", "url": "https://e.org/", "open": "popup"}, "open"),
    ({"id": "web", "title": "W", "kind": "url", "url": "https://e.org/", "open": True}, "open"),
    # Native panes have no page of their own to put in a window.
    ({"id": "clients-panel", "kind": "builtin", "open": "window"}, "open"),
    ({"id": "mail-panel", "kind": "builtin", "open": "window"}, "open"),
    # An app is our own code in an unsandboxed frame with a message bridge.
    ({"id": "someapp", "kind": "app", "open": "window"}, "open"),
])
def test_open_field_refused(tools_env, row, field):
    with pytest.raises(tools.ToolValidationError) as ei:
        tools.validate_tools([row], check_fs=False)
    assert ei.value.field == field


def test_open_default_is_not_written_to_the_file(tools_env):
    t = tools.validate_tools([
        {"id": "studioforge-panel", "kind": "builtin", "open": "window"},
        {"id": "web", "title": "W", "kind": "url", "url": "https://e.org/", "open": "frame"},
        {"id": "win", "title": "X", "kind": "url", "url": "https://e.org/", "open": "window"},
        {"id": "deepseek-harness", "kind": "builtin", "open": "window"},
    ], check_fs=False)
    m = {x.id: x.to_manifest() for x in t}
    assert "open" not in m["studioforge-panel"]
    assert "open" not in m["web"]
    assert m["win"]["open"] == "window"
    assert m["deepseek-harness"]["open"] == "window"
    # An operator who wants StudioForge framed after all can say so.
    sf = tools.validate_tools([{"id": "studioforge-panel", "kind": "builtin", "open": "frame"}],
                              check_fs=False)[0]
    assert sf.to_manifest()["open"] == "frame"


def test_api_studioforge_row_opens_in_a_window_with_its_address(tools_env, monkeypatch):
    make_client, _site = tools_env
    _sf_on(monkeypatch, studioforge_remote_url="https://host.tailnet.example:8452")
    c = _unlocked(make_client)
    rows = {x["id"]: x for x in c.get("/api/tools").json()["tools"]}
    sf = rows["studioforge-panel"]
    assert sf["open"] == "window"
    assert sf["open_url"] == SF_URL
    assert sf["open_remote_url"] == "https://host.tailnet.example:8452"
    assert rows["clients-panel"]["open"] == "frame"
    assert "open_url" not in rows["clients-panel"]


def test_api_builtin_row_has_no_address_when_unavailable(tools_env, monkeypatch):
    make_client, _site = tools_env
    monkeypatch.setattr(main, "SETTINGS", replace(
        config.SETTINGS, studioforge_enabled=False, studioforge_url=SF_URL))
    c = _unlocked(make_client)
    sf = next(x for x in c.get("/api/tools").json()["tools"] if x["id"] == "studioforge-panel")
    assert sf["open_url"] is None


def test_api_open_round_trips_and_builtin_open_is_writable(tools_env, monkeypatch):
    make_client, _site = tools_env
    _sf_on(monkeypatch)
    c = _unlocked(make_client)
    r = c.put("/api/tools", json={"tools": [
        {"id": "win", "title": "Win", "kind": "url", "url": "https://e.org/", "open": "window"},
        {"id": "studioforge-panel", "kind": "builtin", "enabled": True, "open": "frame"},
    ]})
    assert r.status_code == 200, r.text
    assert tools.get_tool("win").open == "window"
    assert tools.get_tool("studioforge-panel").open == "frame"
    rows = c.get("/api/tools").json()["tools"]
    assert next(x for x in rows if x["id"] == "studioforge-panel")["open"] == "frame"
    # GET's rows go straight back, output-only address fields included.
    assert c.put("/api/tools", json={"tools": rows}).status_code == 200
    assert tools.get_tool("win").open == "window"
    assert tools.get_tool("studioforge-panel").open == "frame"
    # Defaults stay out of the file after a plain round trip.
    c.put("/api/tools", json={"tools": [
        {"id": "studioforge-panel", "kind": "builtin", "enabled": True, "open": "window"}]})
    assert "open" not in (config.DATA_DIR / "tools.yaml").read_text()


def test_safe_mode_row_carries_open(tools_env):
    make_client, _site = tools_env
    _manifest("tools:\n  - {id: web, title: W, kind: url, url: 'https://e.org/', safe: true, open: window}\n")
    c = _decoy(make_client)
    rows = c.get("/api/tools").json()["tools"]
    assert rows == [r for r in rows if r["id"] == "web"] and rows[0]["open"] == "window"


@pytest.mark.parametrize("headers, expected", [
    ({}, True),
    ({"X-Frame-Options": "DENY"}, False),
    ({"X-Frame-Options": "SAMEORIGIN"}, False),
    ({"Content-Security-Policy": "frame-ancestors 'none'"}, False),
    ({"Content-Security-Policy": "frame-ancestors *"}, True),
    ({"X-Frame-Options": "DENY",
      "Content-Security-Policy": "default-src 'self'; frame-ancestors *"}, True),
])
def test_tools_framable_matches_main(headers, expected):
    import httpx
    h = httpx.Headers(headers)
    assert tools.framable(h) is expected
    assert main._framable(h) is expected


class _XfoServer:
    """StudioForge's exact shape: HEAD → 405, GET → 200 with
    X-Frame-Options: DENY and CSP frame-ancestors 'none'."""

    def __init__(self, deny=True):
        import socket
        self.deny = deny
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.stop = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        self.sock.settimeout(0.2)
        while not self.stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                continue
            with conn:
                try:
                    method = conn.recv(4096).decode("latin-1").split(" ", 1)[0]
                    hdr = (b"X-Frame-Options: DENY\r\n"
                           b"Content-Security-Policy: frame-ancestors 'none'\r\n") if self.deny else b""
                    if method == "HEAD":
                        conn.sendall(b"HTTP/1.1 405 Method Not Allowed\r\n" + hdr
                                     + b"Content-Length: 0\r\nConnection: close\r\n\r\n")
                    else:
                        conn.sendall(b"HTTP/1.1 200 OK\r\n" + hdr
                                     + b"Content-Length: 2\r\nConnection: close\r\n\r\nok")
                except OSError:
                    pass

    def close(self):
        self.stop.set()
        self.sock.close()


@pytest.mark.parametrize("deny, expected", [(True, False), (False, True)])
def test_status_url_tool_reports_framable(tools_env, deny, expected):
    make_client, _site = tools_env
    srv = _XfoServer(deny=deny)
    try:
        _manifest(f"tools:\n  - {{id: web, title: W, kind: url, url: 'http://127.0.0.1:{srv.port}/'}}\n")
        st = make_client().get("/api/tools/web/status").json()
        assert st["reachable"] is True
        assert st["framable"] is expected
        assert st["open"] == "frame"
    finally:
        srv.close()


def test_status_framable_unknown_when_unreachable(tools_env, monkeypatch):
    make_client, _site = tools_env
    _manifest("tools:\n  - {id: web, title: W, kind: url, url: 'http://example.invalid/'}\n")

    async def down(url):
        return False

    monkeypatch.setattr(tools, "_probe_url", down)
    st = make_client().get("/api/tools/web/status").json()
    assert st["reachable"] is False and st["framable"] is None


def test_api_harness_row_addresses(tools_env, monkeypatch):
    make_client, _site = tools_env
    monkeypatch.setattr(main, "SETTINGS", replace(
        config.SETTINGS, harness_enabled=True, harness_port=3999,
        harness_remote_url="https://host.tailnet.example:8453"))
    c = _unlocked(make_client)
    h = next(x for x in c.get("/api/tools").json()["tools"] if x["id"] == "deepseek-harness")
    assert h["open"] == "frame", "the Harness pane (service control, jobs) stays the default"
    assert h["open_url"] == "http://127.0.0.1:3999/"
    assert h["open_remote_url"] == "https://host.tailnet.example:8453"


def test_builtin_open_url_never_reaches_safe_mode(tools_env, monkeypatch):
    make_client, _site = tools_env
    _sf_on(monkeypatch)
    c = _decoy(make_client)
    body = c.get("/api/tools").text
    assert "198.51.100.7" not in body and "open_url" not in body
    assert c.get("/api/tools/studioforge-panel/status").status_code == 403
