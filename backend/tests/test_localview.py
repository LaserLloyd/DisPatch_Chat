"""Local Viewer: the roots allowlist, the deny list, and the header contract.

Every test here is a security assertion. The viewer serves arbitrary bytes off
the host's disk to a browser, so the interesting cases are all the ones where
it must REFUSE: traversal, symlinks out of a root, dotfiles below a root, the
built-in secret paths, and a machine caller holding an API key (this feature is
browser-only on purpose — see _is_inbound in main.py, which deliberately does
not list these routes).

Same hermetic style as the rest of tests/: throwaway data dir, nothing touches
the live install. Run: cd backend && uv run pytest -q tests/test_localview.py
"""
from __future__ import annotations

import asyncio
import os
import time

import pytest
from fastapi.testclient import TestClient

from app import auth, config, localview, main
from app.database import Database


@pytest.fixture
def view_env(tmp_path, monkeypatch):
    """Isolated data dir + DB + a scratch tree to serve, with the viewer's
    config cache dropped. Yields (make_client, sandbox_root)."""
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)     # the no-writes-outside-tmp fixture makes it too
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
    localview._reset_cache()
    monkeypatch.delenv("DISPATCH_VIEWER_ROOTS", raising=False)
    monkeypatch.delenv("LOCAL_CHAT_VIEWER_ROOTS", raising=False)
    config._invalidate_bots_cache()

    temp_db = Database(data / "chats.db")
    monkeypatch.setattr(main, "db", temp_db)

    site = tmp_path / "site"
    site.mkdir()

    clients: list[TestClient] = []

    def make_client(client_addr=("testclient", 50000)) -> TestClient:
        c = TestClient(main.app, client=client_addr)
        c.__enter__()
        clients.append(c)
        return c

    yield make_client, site

    for c in clients:
        c.__exit__(None, None, None)
    localview._reset_cache()
    asyncio.run(temp_db.close())


def _write_config(roots, *, show_hidden=False, deny=None, max_text_bytes=None):
    """Write local-viewer.yaml straight into the (monkeypatched) data dir."""
    lines = ["roots:"]
    lines += [f"  - {r}" for r in roots] or ["  []"]
    if deny:
        lines.append("deny:")
        lines += [f"  - {d}" for d in deny]
    lines.append(f"show_hidden: {'true' if show_hidden else 'false'}")
    if max_text_bytes is not None:
        lines.append(f"max_text_bytes: {max_text_bytes}")
    (config.DATA_DIR / "local-viewer.yaml").write_text("\n".join(lines) + "\n")
    localview._reset_cache()


def _unlocked(make_client):
    """A client holding a full session (PIN set, then unlocked)."""
    c = make_client()
    auth.set_pin("1234")
    r = c.post("/api/auth/unlock", json={"pin": "1234"})
    assert r.status_code == 200, r.text
    return c


# --------------------------------------------------------------------------- #
# Feature off
# --------------------------------------------------------------------------- #

def test_off_when_no_roots_configured(view_env):
    make_client, _ = view_env
    c = _unlocked(make_client)
    for path in ("/api/local/stat?path=/tmp", "/api/local/ls?path=/tmp",
                 "/local/file/tmp/x.txt"):
        r = c.get(path)
        assert r.status_code == 404, path
        assert "add a root in Settings" in r.json()["detail"], path


def test_roots_endpoint_reports_disabled(view_env):
    make_client, _ = view_env
    c = _unlocked(make_client)
    body = c.get("/api/local/roots").json()
    assert body["enabled"] is False
    assert body["roots"] == []
    assert body["show_hidden"] is False


def test_env_seeds_roots_when_yaml_absent(view_env, monkeypatch):
    make_client, site = view_env
    (site / "a.txt").write_text("hello")
    monkeypatch.setenv("DISPATCH_VIEWER_ROOTS", os.pathsep.join([str(site), "/nope"]))
    localview._reset_cache()
    c = _unlocked(make_client)
    body = c.get("/api/local/roots").json()
    assert body["enabled"] is True
    assert [r["path"] for r in body["roots"]][0].endswith("site")
    assert c.get(f"/api/local/stat?path={site}/a.txt").status_code == 200


# --------------------------------------------------------------------------- #
# The gate: decoy 403, machine caller refused, unlocked 200
# --------------------------------------------------------------------------- #

VIEWER_ROUTES = [
    ("GET", "/local/file/tmp/x.txt"),
    ("GET", "/api/local/stat?path=/tmp"),
    ("GET", "/api/local/ls?path=/tmp"),
    ("GET", "/api/local/roots"),
    ("PUT", "/api/local/config"),
]


@pytest.mark.parametrize("method,path", VIEWER_ROUTES)
def test_decoy_gets_403(view_env, method, path):
    make_client, site = view_env
    _write_config([str(site)])
    c = make_client()
    auth.set_pin("1234")                     # PIN set, never unlocked -> decoy
    r = c.request(method, path, json={} if method == "PUT" else None)
    assert r.status_code == 403, f"{method} {path} -> {r.status_code}"


@pytest.mark.parametrize("method,path", VIEWER_ROUTES)
def test_api_key_does_not_open_the_viewer(view_env, method, path):
    """Browser-only feature: the routes are NOT in _is_inbound, so a machine
    caller with a valid api_token is still Safe Mode here."""
    make_client, site = view_env
    _write_config([str(site)])
    c = make_client()
    auth.set_pin("1234")
    cfg = auth.load()
    cfg.api_token = "tok-abc"
    auth._write(cfg)
    auth._bust_cache()
    r = c.request(method, path, headers={"X-API-Key": "tok-abc"},
                  json={} if method == "PUT" else None)
    assert r.status_code == 403, f"{method} {path} -> {r.status_code}"


def test_loopback_machine_caller_also_refused(view_env):
    """An on-box agent on 127.0.0.1 is exempt from the API key on the inbound
    surface — but these routes are not on it, so it stays Safe Mode."""
    make_client, site = view_env
    _write_config([str(site)])
    c = make_client(("127.0.0.1", 51111))
    auth.set_pin("1234")
    assert c.get("/api/local/roots").status_code == 403


def test_unlocked_gets_200(view_env):
    make_client, site = view_env
    (site / "a.txt").write_text("hello")
    _write_config([str(site)])
    c = _unlocked(make_client)
    assert c.get("/api/local/roots").status_code == 200
    assert c.get(f"/api/local/stat?path={site}/a.txt").status_code == 200


def test_no_pin_at_all_is_open(view_env):
    """The app with no lock configured is fully open — same rule as everywhere."""
    make_client, site = view_env
    (site / "a.txt").write_text("hello")
    _write_config([str(site)])
    c = make_client()
    assert c.get("/api/local/roots").status_code == 200


# --------------------------------------------------------------------------- #
# Traversal / symlinks / hidden
# --------------------------------------------------------------------------- #

def test_traversal_out_of_root_denied(view_env, tmp_path):
    make_client, site = view_env
    (tmp_path / "outside.txt").write_text("secret")
    _write_config([str(site)])
    c = _unlocked(make_client)
    for path in (f"{site}/../outside.txt",
                 f"{site}/%2e%2e/outside.txt",
                 f"{site}//../outside.txt"):
        r = c.get(f"/api/local/stat?path={path}")
        assert r.status_code in (403, 404), path
        if r.status_code == 403:
            assert r.json()["detail"] == "Not served by the local viewer"


def test_resolve_refuses_traversal_directly(view_env, tmp_path):
    """Straight at resolve(), because httpx normalises `..` out of a URL before
    it ever reaches us — so the route-level test above cannot prove this."""
    _make_client, site = view_env
    (tmp_path / "outside.txt").write_text("secret")
    _write_config([str(site)])
    cfg = localview.load()
    for raw in (f"{site}/../outside.txt", f"{site}/./../outside.txt",
                f"{site}//../outside.txt", "../etc/passwd"):
        with pytest.raises(localview.ViewerError) as ei:
            localview.resolve(raw, cfg)
        assert ei.value.status == 403, raw
        assert ei.value.detail == "Not served by the local viewer"


def test_traversal_on_file_route_denied(view_env, tmp_path):
    make_client, site = view_env
    (tmp_path / "outside.txt").write_text("secret")
    _write_config([str(site)])
    c = _unlocked(make_client)
    rel = str(site).lstrip("/")
    r = c.get(f"/local/file/{rel}/../outside.txt", follow_redirects=False)
    assert r.status_code in (403, 404)
    assert "secret" not in r.text


def test_symlink_escaping_root_denied(view_env, tmp_path):
    make_client, site = view_env
    (tmp_path / "outside.txt").write_text("secret")
    (site / "escape.txt").symlink_to(tmp_path / "outside.txt")
    _write_config([str(site)])
    c = _unlocked(make_client)
    r = c.get(f"/api/local/stat?path={site}/escape.txt")
    assert r.status_code == 403
    assert r.json()["detail"] == "Not served by the local viewer"


def test_no_existence_oracle_outside_roots(view_env, tmp_path):
    """Outside every root the answer is 403 whether or not the path exists;
    a 404 is only ever given inside a root for a name the caller could read."""
    make_client, site = view_env
    (tmp_path / "outside.txt").write_text("secret")
    _write_config([str(site)])
    c = _unlocked(make_client)
    for raw in (tmp_path / "outside.txt", tmp_path / "never-there.txt",
                "/etc/passwd", "/etc/never-there"):
        r = c.get(f"/api/local/stat?path={raw}")
        assert r.status_code == 403, raw
        assert r.json()["detail"] == "Not served by the local viewer"
    # a denied *name* inside the root is 403 even when it does not exist
    assert c.get(f"/api/local/stat?path={site}/ghost.env").status_code == 403
    # an ordinary missing name inside the root is the one honest 404
    assert c.get(f"/api/local/stat?path={site}/ghost.txt").status_code == 404


def test_symlink_inside_root_allowed(view_env):
    make_client, site = view_env
    (site / "real.txt").write_text("ok")
    (site / "link.txt").symlink_to(site / "real.txt")
    _write_config([str(site)])
    c = _unlocked(make_client)
    r = c.get(f"/api/local/stat?path={site}/link.txt")
    assert r.status_code == 200
    assert r.json()["viewer"] == "text"


def test_hidden_component_below_root_refused(view_env):
    make_client, site = view_env
    (site / ".secret").mkdir()
    (site / ".secret" / "x.txt").write_text("shh")
    (site / ".dotfile").write_text("shh")
    _write_config([str(site)])
    c = _unlocked(make_client)
    assert c.get(f"/api/local/stat?path={site}/.secret/x.txt").status_code == 403
    assert c.get(f"/api/local/stat?path={site}/.dotfile").status_code == 403


def test_hidden_component_allowed_with_show_hidden(view_env):
    make_client, site = view_env
    (site / ".dotfile").write_text("shh")
    _write_config([str(site)], show_hidden=True)
    c = _unlocked(make_client)
    assert c.get(f"/api/local/stat?path={site}/.dotfile").status_code == 200


def test_hidden_component_above_root_allowed(view_env, tmp_path):
    """`~/.agent/workspace` as a root is legal: the dot is above the root."""
    make_client, _ = view_env
    hidden_root = tmp_path / ".hiddenroot" / "workspace"
    hidden_root.mkdir(parents=True)
    (hidden_root / "a.txt").write_text("ok")
    _write_config([str(hidden_root)])
    c = _unlocked(make_client)
    assert c.get(f"/api/local/stat?path={hidden_root}/a.txt").status_code == 200


def test_tilde_expands_only_as_first_segment(view_env, tmp_path):
    make_client, site = view_env
    weird = site / "~"
    weird.mkdir()
    (weird / "a.txt").write_text("ok")
    _write_config([str(site)])
    c = _unlocked(make_client)
    # A literal "~" DIRECTORY below the root is a normal path component.
    r = c.get(f"/api/local/stat?path={site}/~/a.txt")
    assert r.status_code == 200
    # And a leading "~" means $HOME, which is not under the root here.
    assert c.get("/api/local/stat?path=~/anything").status_code in (403, 404)


# --------------------------------------------------------------------------- #
# Built-in deny list
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("name", [
    "creds.env", ".env", ".env.local", "server.pem", "server.key",
    "cert.p12", "cert.pfx", "id_rsa", "vault.kdbx", "notes.gpg",
    "key.asc", "known_hosts", "authorized_keys", "cache.sqlite", "data.db",
])
def test_denied_filename_patterns(view_env, name):
    make_client, site = view_env
    (site / name).write_text("secret")
    _write_config([str(site)])
    c = _unlocked(make_client)
    r = c.get(f"/api/local/stat?path={site}/{name}")
    assert r.status_code == 403, name
    assert r.json()["detail"] == "Not served by the local viewer"


def test_denied_builtin_roots_even_if_rooted_there(view_env):
    """Configuring /etc as a root does not make /etc readable."""
    make_client, _ = view_env
    _write_config(["/etc"])
    c = _unlocked(make_client)
    assert c.get("/api/local/stat?path=/etc/hosts").status_code == 403


def test_data_dir_secrets_denied(view_env):
    make_client, _ = view_env
    data = config.DATA_DIR
    (data / "security.yaml").write_text("pin_hash: x\n")
    (data / "trusted-devices.yaml").write_text("[]\n")
    (data / "RECOVERY-CODE.txt").write_text("LC-AAAA-BBBB\n")
    (data / "chats.db-wal").write_text("x")
    (data / "backups").mkdir(exist_ok=True)
    (data / "backups" / "snap.db").write_text("x")
    _write_config([str(data)])
    c = _unlocked(make_client)
    for name in ("security.yaml", "trusted-devices.yaml", "RECOVERY-CODE.txt",
                 "chats.db-wal", "local-viewer.yaml", "backups/snap.db"):
        r = c.get(f"/api/local/stat?path={data}/{name}")
        assert r.status_code == 403, name


def test_user_deny_entry_refused(view_env):
    make_client, site = view_env
    (site / "private").mkdir()
    (site / "private" / "x.txt").write_text("nope")
    (site / "ok.txt").write_text("yes")
    _write_config([str(site)], deny=[str(site / "private")])
    c = _unlocked(make_client)
    assert c.get(f"/api/local/stat?path={site}/private/x.txt").status_code == 403
    assert c.get(f"/api/local/stat?path={site}/ok.txt").status_code == 200


# --------------------------------------------------------------------------- #
# stat / ls
# --------------------------------------------------------------------------- #

def test_stat_file_shape(view_env):
    make_client, site = view_env
    (site / "readme.md").write_text("# hi\n")
    _write_config([str(site)])
    c = _unlocked(make_client)
    b = c.get(f"/api/local/stat?path={site}/readme.md").json()
    assert b["ok"] is True
    assert b["name"] == "readme.md"
    assert b["kind"] == "file"
    assert b["ext"] == "md"
    assert b["viewer"] == "markdown"
    assert b["mime"] == "text/plain; charset=utf-8"
    assert b["size"] == 5
    assert b["text_ok"] is True
    assert b["url"].startswith("/local/file/")
    assert isinstance(b["mtime"], float)


def test_stat_dir_shape(view_env):
    make_client, site = view_env
    (site / "index.html").write_text("<h1>hi</h1>")
    _write_config([str(site)])
    c = _unlocked(make_client)
    b = c.get(f"/api/local/stat?path={site}").json()
    assert b["kind"] == "dir"
    assert b["viewer"] == "listing"
    assert b["has_index"] is True


def test_stat_text_ok_false_over_cap(view_env):
    make_client, site = view_env
    (site / "big.txt").write_text("x" * 100)
    _write_config([str(site)], max_text_bytes=10)
    c = _unlocked(make_client)
    assert c.get(f"/api/local/stat?path={site}/big.txt").json()["text_ok"] is False


def test_stat_missing_is_404(view_env):
    make_client, site = view_env
    _write_config([str(site)])
    c = _unlocked(make_client)
    r = c.get(f"/api/local/stat?path={site}/nope.txt")
    assert r.status_code == 404
    assert r.json()["detail"] == "Not found"


def test_ls_sorted_and_omits_hidden_and_denied(view_env):
    make_client, site = view_env
    (site / "Bravo").mkdir()
    (site / "alpha").mkdir()
    (site / "zeta.txt").write_text("z")
    (site / "Apple.txt").write_text("a")
    (site / ".hidden").write_text("h")
    (site / "id_rsa").write_text("k")
    _write_config([str(site)])
    c = _unlocked(make_client)
    b = c.get(f"/api/local/ls?path={site}").json()
    names = [e["name"] for e in b["entries"]]
    assert names == ["alpha", "Bravo", "Apple.txt", "zeta.txt"]
    assert b["truncated"] is False
    assert b["parent"] is None            # this IS a root


def test_ls_parent_below_root(view_env):
    make_client, site = view_env
    (site / "sub").mkdir()
    _write_config([str(site)])
    c = _unlocked(make_client)
    b = c.get(f"/api/local/ls?path={site}/sub").json()
    assert b["parent"] is not None
    assert b["parent"].endswith("site")


def test_ls_cap(view_env):
    make_client, site = view_env
    for i in range(2100):
        (site / f"f{i:05d}.txt").write_text("x")
    _write_config([str(site)])
    c = _unlocked(make_client)
    b = c.get(f"/api/local/ls?path={site}").json()
    assert len(b["entries"]) == 2000
    assert b["truncated"] is True


def test_ls_on_a_file_is_400(view_env):
    make_client, site = view_env
    (site / "a.txt").write_text("x")
    _write_config([str(site)])
    c = _unlocked(make_client)
    assert c.get(f"/api/local/ls?path={site}/a.txt").status_code == 400


# --------------------------------------------------------------------------- #
# The file route
# --------------------------------------------------------------------------- #

def _file_url(p) -> str:
    return "/local/file/" + str(p).lstrip("/")


def test_serves_file_bytes(view_env):
    make_client, site = view_env
    (site / "a.txt").write_text("hello world")
    _write_config([str(site)])
    c = _unlocked(make_client)
    r = c.get(_file_url(site / "a.txt"))
    assert r.status_code == 200
    assert r.text == "hello world"
    assert r.headers["content-type"] == "text/plain; charset=utf-8"


def test_dir_without_slash_redirects(view_env):
    make_client, site = view_env
    (site / "sub").mkdir()
    (site / "sub" / "index.html").write_text("<h1>hi</h1>")
    _write_config([str(site)])
    c = _unlocked(make_client)
    r = c.get(_file_url(site / "sub"), follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"].endswith("/sub/")


def test_dir_with_slash_serves_index(view_env):
    make_client, site = view_env
    (site / "index.html").write_text("<h1>hi</h1>")
    _write_config([str(site)])
    c = _unlocked(make_client)
    r = c.get(_file_url(site) + "/")
    assert r.status_code == 200
    assert r.headers["content-type"] == "text/html; charset=utf-8"
    assert "<h1>hi</h1>" in r.text


def test_dir_without_index_is_404_no_index(view_env):
    make_client, site = view_env
    (site / "sub").mkdir()
    _write_config([str(site)])
    c = _unlocked(make_client)
    r = c.get(_file_url(site / "sub") + "/")
    assert r.status_code == 404
    assert r.json()["detail"] == "no index"
    # ...and the listing still works.
    assert c.get(f"/api/local/ls?path={site}/sub").status_code == 200


def test_range_request(view_env):
    make_client, site = view_env
    (site / "a.bin").write_bytes(b"0123456789")
    _write_config([str(site)])
    c = _unlocked(make_client)
    r = c.get(_file_url(site / "a.bin"), headers={"Range": "bytes=2-4"})
    assert r.status_code == 206
    assert r.content == b"234"


@pytest.mark.parametrize("name,ctype", [
    ("a.html", "text/html; charset=utf-8"),
    ("a.htm", "text/html; charset=utf-8"),
    ("a.css", "text/css; charset=utf-8"),
    ("a.js", "text/javascript; charset=utf-8"),
    ("a.mjs", "text/javascript; charset=utf-8"),
    ("a.json", "application/json; charset=utf-8"),
    ("a.svg", "image/svg+xml"),
    ("a.md", "text/plain; charset=utf-8"),
    ("a.log", "text/plain; charset=utf-8"),
    ("a.py", "text/plain; charset=utf-8"),
    ("a.png", "image/png"),
    ("a.mp4", "video/mp4"),
    ("a.mp3", "audio/mpeg"),
    ("a.pdf", "application/pdf"),
])
def test_content_type_table(view_env, name, ctype):
    make_client, site = view_env
    (site / name).write_bytes(b"x")
    _write_config([str(site)])
    c = _unlocked(make_client)
    r = c.get(_file_url(site / name))
    assert r.status_code == 200, name
    assert r.headers["content-type"] == ctype, name


def test_unknown_extension_is_attachment(view_env):
    make_client, site = view_env
    (site / "a.wat").write_bytes(b"x")
    _write_config([str(site)])
    c = _unlocked(make_client)
    r = c.get(_file_url(site / "a.wat"))
    assert r.headers["content-type"].startswith("application/octet-stream")
    assert r.headers["content-disposition"].startswith("attachment")


# --------------------------------------------------------------------------- #
# Headers
# --------------------------------------------------------------------------- #

def test_html_gets_sandbox_csp(view_env):
    make_client, site = view_env
    (site / "a.html").write_text("<h1>hi</h1>")
    _write_config([str(site)])
    c = _unlocked(make_client)
    r = c.get(_file_url(site / "a.html"))
    csp = r.headers["content-security-policy"]
    assert csp == ("sandbox allow-scripts allow-forms allow-popups "
                   "allow-modals allow-downloads; frame-ancestors 'self'")
    assert "allow-same-origin" not in csp
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["cache-control"] == "private, max-age=0, must-revalidate"
    assert r.headers["x-robots-tag"] == "noindex"


def test_non_html_gets_locked_csp(view_env):
    make_client, site = view_env
    (site / "a.png").write_bytes(b"x")
    _write_config([str(site)])
    c = _unlocked(make_client)
    r = c.get(_file_url(site / "a.png"))
    assert r.headers["content-security-policy"] == "default-src 'none'; sandbox; frame-ancestors 'self'"
    assert r.headers["x-robots-tag"] == "noindex"


def test_api_local_json_gets_locked_csp(view_env):
    make_client, site = view_env
    _write_config([str(site)])
    c = _unlocked(make_client)
    r = c.get("/api/local/roots")
    assert r.headers["content-security-policy"] == "default-src 'none'; sandbox; frame-ancestors 'self'"


def test_app_html_gets_frame_ancestors(view_env):
    make_client, _ = view_env
    c = make_client()
    r = c.get("/")
    assert r.headers.get("content-security-policy") == "frame-ancestors 'self'"


# --------------------------------------------------------------------------- #
# PUT /api/local/config
# --------------------------------------------------------------------------- #

def test_put_config_writes_and_reloads(view_env, tmp_path):
    make_client, site = view_env
    (site / "a.txt").write_text("x")
    c = _unlocked(make_client)
    r = c.put("/api/local/config", json={"roots": [str(site)], "show_hidden": True})
    assert r.status_code == 200, r.text
    assert r.json()["enabled"] is True
    assert r.json()["show_hidden"] is True
    written = config.DATA_DIR / "local-viewer.yaml"
    assert written.exists()
    assert (written.stat().st_mode & 0o777) == 0o600
    # ...and the change is live without a restart.
    assert c.get(f"/api/local/stat?path={site}/a.txt").status_code == 200


def test_put_config_rejects_bad_roots(view_env, tmp_path):
    make_client, site = view_env
    (site / "afile.txt").write_text("x")
    c = _unlocked(make_client)
    for bad in [str(tmp_path / "does-not-exist"), str(site / "afile.txt"), "/etc",
                "relative/path", ""]:
        r = c.put("/api/local/config", json={"roots": [bad], "show_hidden": False})
        assert r.status_code == 400, bad
    assert not (config.DATA_DIR / "local-viewer.yaml").exists()


def test_put_config_empty_roots_turns_it_off(view_env):
    make_client, site = view_env
    _write_config([str(site)])
    c = _unlocked(make_client)
    r = c.put("/api/local/config", json={"roots": [], "show_hidden": False})
    assert r.status_code == 200
    assert r.json()["enabled"] is False
    assert c.get("/api/local/stat?path=/tmp").status_code == 404


def test_put_config_preserves_deny_and_cap(view_env):
    make_client, site = view_env
    (site / "private").mkdir()
    _write_config([str(site)], deny=[str(site / "private")], max_text_bytes=99)
    c = _unlocked(make_client)
    c.put("/api/local/config", json={"roots": [str(site)], "show_hidden": False})
    cfg = localview.load()
    assert cfg.max_text_bytes == 99
    assert str(site / "private") in [str(d) for d in cfg.deny]


# --------------------------------------------------------------------------- #
# Fail-closed config + the denial counter
# --------------------------------------------------------------------------- #

def test_malformed_yaml_fails_closed(view_env):
    make_client, site = view_env
    (config.DATA_DIR / "local-viewer.yaml").write_text("roots: [unclosed\n")
    localview._reset_cache()
    c = _unlocked(make_client)
    r = c.get(f"/api/local/stat?path={site}")
    assert r.status_code == 404
    assert "add a root in Settings" in r.json()["detail"]


def test_denial_counter_and_health(view_env):
    make_client, site = view_env
    (site / "id_rsa").write_text("k")
    _write_config([str(site)])
    localview._DENIALS.clear()
    c = _unlocked(make_client)
    assert c.get(f"/api/local/stat?path={site}/id_rsa").status_code == 403
    assert localview.denial_stats()["denials_24h"] >= 1
    assert c.get("/api/health").json()["viewer_denied_24h"] >= 1


# --------------------------------------------------------------------------- #
# The viewer classifier (pure)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("name,kind", [
    ("a.png", "image"), ("a.jpg", "image"), ("a.avif", "image"),
    ("a.svg", "svg"), ("a.mp4", "video"), ("a.mkv", "video"),
    ("a.mp3", "audio"), ("a.opus", "audio"), ("a.pdf", "pdf"),
    ("a.md", "markdown"), ("a.markdown", "markdown"),
    ("a.html", "html"), ("a.htm", "html"), ("a.xhtml", "html"),
    ("a.txt", "text"), ("a.py", "text"), ("a.yaml", "text"), ("a.rs", "text"),
    ("a.zip", "download"), ("a", "download"),
])
def test_viewer_classifier(name, kind):
    assert localview.viewer_kind(name) == kind


# --------------------------------------------------------------------------- #
# Adversarial review round (2026-09-03)
# --------------------------------------------------------------------------- #

def test_index_symlink_is_revalidated(view_env, tmp_path):
    """A directory's index.html goes through the same gate as any path: a
    symlink named index.html must not become a read primitive (served as html)."""
    make_client, site = view_env
    _write_config([str(site)])
    loot = tmp_path / "outside" / "loot.html"
    loot.parent.mkdir(exist_ok=True)
    loot.write_text("<b>SECRET OUTSIDE ROOT</b>")
    pub = site / "pub"
    pub.mkdir()
    (pub / "index.html").symlink_to(loot)
    etc = site / "pub2"
    etc.mkdir()
    (etc / "index.html").symlink_to("/etc/hostname")
    c = _unlocked(make_client)
    for d in (pub, etc):
        r = c.get(f"/local/file{d}/")
        assert r.status_code == 404, (d, r.status_code, r.text[:80])
        assert "SECRET" not in r.text
        st = c.get(f"/api/local/stat?path={d}").json()
        assert st["has_index"] is False
    # a real index still works
    real = site / "ok"
    real.mkdir()
    (real / "index.html").write_text("<p>fine</p>")
    r = c.get(f"/local/file{real}/")
    assert r.status_code == 200 and "fine" in r.text


def test_config_refuses_root_and_proc_self_root(view_env):
    make_client, site = view_env
    c = _unlocked(make_client)
    for bad in ("/", "/proc/self/root", "/proc/self/cwd"):
        r = c.put("/api/local/config", json={"roots": [bad]})
        assert r.status_code == 400, (bad, r.status_code)
    r = c.put("/api/local/config", json={"roots": [str(site)]})
    assert r.status_code == 200 and r.json()["enabled"] is True


@pytest.mark.parametrize("name", [
    "foo.env.bak", "secret.pem.orig", "id_rsa~", "notes.sqlite3", "app.db-wal",
    "app.db-shm", "credentials", "credentials.json", ".netrc", "site.crt",
    "wallet.keystore", "office.ovpn", "keys.env.1", "prod.env.example",
])
def test_deny_patterns_cover_backup_siblings(view_env, name):
    make_client, site = view_env
    _write_config([str(site)])
    (site / name).write_text("x")
    c = _unlocked(make_client)
    assert c.get(f"/api/local/stat?path={site}/{name}").status_code == 403, name
    assert c.get(f"/local/file{site}/{name}").status_code == 403, name
    names = {e["name"] for e in c.get(f"/api/local/ls?path={site}").json()["entries"]}
    assert name not in names


def test_nul_byte_is_a_403_not_a_500(view_env):
    make_client, site = view_env
    _write_config([str(site)])
    c = _unlocked(make_client)
    r = c.get("/api/local/stat", params={"path": f"{site}/a\x00.txt"})
    assert r.status_code == 403
    r = c.get(f"/local/file{site}/a%00.txt")
    assert r.status_code == 403


def test_roots_body_reports_symlink_target(view_env, tmp_path):
    make_client, site = view_env
    link = tmp_path / "sitelink"
    link.symlink_to(site)
    _write_config([str(link)])
    c = _unlocked(make_client)
    rows = c.get("/api/local/roots").json()["roots"]
    assert rows[0]["exists"] is True
    assert rows[0]["serves"].endswith(site.name)


# --------------------------------------------------------------------------- #
# Frame tickets (2026-09-03): a sandboxed frame sends no cookie
# --------------------------------------------------------------------------- #

def _site_with_assets(site):
    (site / "css").mkdir()
    (site / "css" / "style.css").write_text("h1{color:red}")
    (site / "index.html").write_text('<link rel="stylesheet" href="css/style.css"><h1>hi</h1>')
    (site / "sub").mkdir()
    (site / "sub" / "page.html").write_text("<h2>two</h2>")
    (site / "other").mkdir()
    (site / "other" / "private.txt").write_text("not for the page")


def test_stat_mints_frame_url_only_for_framed_content(view_env):
    make_client, site = view_env
    _write_config([str(site)])
    _site_with_assets(site)
    (site / "notes.txt").write_text("plain")
    c = _unlocked(make_client)
    html = c.get(f"/api/local/stat?path={site}/index.html").json()
    assert html["frame_url"].startswith("/local/view/") and html["frame_url"].endswith("/index.html")
    folder = c.get(f"/api/local/stat?path={site}").json()
    assert folder["frame_url"].startswith("/local/view/") and folder["frame_url"].endswith("/")
    assert c.get(f"/api/local/stat?path={site}/notes.txt").json()["frame_url"] is None
    assert c.get(f"/api/local/stat?path={site}/other").json()["frame_url"] is None  # no index


def test_ticket_url_serves_subresources_without_a_cookie(view_env):
    """The whole point: with a PIN set, a cookieless fetch of the page's CSS
    through the ticket succeeds, while the same file via /local/file is 403."""
    make_client, site = view_env
    _write_config([str(site)])
    _site_with_assets(site)
    c = _unlocked(make_client)
    frame = c.get(f"/api/local/stat?path={site}/index.html").json()["frame_url"]
    base = frame.rsplit("/", 1)[0] + "/"
    bare = make_client()                      # same host, no session cookie
    assert bare.get(f"/local/file{site}/css/style.css").status_code == 403
    r = bare.get(base + "css/style.css")
    assert r.status_code == 200 and "color:red" in r.text
    assert r.headers["access-control-allow-origin"] == "*"
    assert r.headers["content-security-policy"].startswith("default-src 'none'")
    page = bare.get(frame)
    assert page.status_code == 200 and "sandbox allow-scripts" in page.headers["content-security-policy"]
    assert "allow-same-origin" not in page.headers["content-security-policy"]
    # Folder ticket: slash form serves the index, bare form 307s to it.
    fr = c.get(f"/api/local/stat?path={site}").json()["frame_url"]
    assert "<h1>hi</h1>" in bare.get(fr).text
    r = bare.get(fr + "sub", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"].endswith("/sub/")


def test_ticket_fails_closed(view_env):
    make_client, site = view_env
    _write_config([str(site)])
    _site_with_assets(site)
    c = _unlocked(make_client)
    frame = c.get(f"/api/local/stat?path={site}/sub/page.html").json()["frame_url"]
    base = frame.rsplit("/", 1)[0] + "/"
    bare = make_client()
    # Unknown ticket, even with no PIN gate in the way, is a 403.
    assert bare.get("/local/view/nope/index.html").status_code == 403
    assert bare.get("/local/view/nope/").status_code == 403
    # Scope is the page's OWN directory: parent and sibling trees are refused
    # with the uniform message, and `..` gets no existence oracle. Sent
    # percent-encoded because the HTTP client (like a browser) collapses a
    # literal `..` before the request leaves — Starlette decodes these to `..`
    # so the SERVER's lexical check is what is under test.
    for rel in ("%2e%2e/index.html", "%2e%2e/other/private.txt", "%2e%2e/%2e%2e/etc/passwd",
                "%2e%2e/nope-does-not-exist", "%2e%2e", ""):
        r = bare.get(base + rel)
        if rel == "":
            assert r.status_code == 404      # the sub folder has no index
        else:
            assert r.status_code == 403, rel
            assert r.json()["detail"] == localview.DENIED_DETAIL
    # Missing file inside the scope is an honest 404.
    assert bare.get(base + "missing.png").status_code == 404
    # Deny patterns still apply inside the scope.
    (site / "sub" / ".env").write_text("KEY=1")
    assert bare.get(base + ".env").status_code == 403
    # A symlink inside the scope that points outside it is refused.
    (site / "sub" / "esc").symlink_to(site / "other")
    assert bare.get(base + "esc/private.txt").status_code == 403


def test_ticket_is_client_bound_and_expires(view_env, monkeypatch):
    make_client, site = view_env
    _write_config([str(site)])
    _site_with_assets(site)
    c = _unlocked(make_client)
    frame = c.get(f"/api/local/stat?path={site}/index.html").json()["frame_url"]
    other = make_client(("203.0.113.7", 4000))
    assert other.get(frame).status_code == 403
    same = make_client()
    assert same.get(frame).status_code == 200
    # Expiry: push the clock past the sliding window.
    t0 = time.monotonic()
    monkeypatch.setattr(localview.time, "monotonic", lambda: t0 + localview.TICKET_TTL_S + 1)
    assert same.get(frame).status_code == 403
    assert not localview._tickets                   # and it was dropped


def test_ticket_table_is_bounded(view_env):
    make_client, site = view_env
    _write_config([str(site)])
    _site_with_assets(site)
    c = _unlocked(make_client)
    first = c.get(f"/api/local/stat?path={site}/index.html").json()["frame_url"]
    for _ in range(localview.TICKET_CAP + 5):
        c.get(f"/api/local/stat?path={site}/index.html")
    assert len(localview._tickets) <= localview.TICKET_CAP
    assert make_client().get(first).status_code == 403   # evicted, oldest first


def test_ticket_route_is_not_machine_inbound(view_env):
    """An api_token holder still cannot mint tickets (stat is browser-only),
    and a ticket URL is not on the inbound allowlist either."""
    make_client, site = view_env
    _write_config([str(site)])
    _site_with_assets(site)
    assert not main._is_inbound("GET", "/local/view/x/index.html")
    auth.set_pin("1234")
    remote = make_client(("198.51.100.9", 5000))
    r = remote.get(f"/api/local/stat?path={site}/index.html",
                   headers={"X-API-Key": auth.load().api_token or "x"})
    assert r.status_code == 403
