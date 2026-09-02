"""Regressions for the 2026-09-02 hardening round (proxy markers, quota key,
doc-ref path guard, media-ingest deny roots, second lock on read routes)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app import main


def _req(**headers):
    return SimpleNamespace(headers={k.lower(): v for k, v in headers.items()})


def test_every_known_proxy_header_marks_the_request_as_proxied():
    assert not main._proxied_request(_req())
    for h in main._PROXY_MARKER_HEADERS:
        assert main._proxied_request(_req(**{h: "x"})), h
    # Serve's identity headers alone (no X-Forwarded-*) used to slip through.
    assert main._proxied_request(_req(**{"tailscale-user-login": "a@b"}))
    assert main._proxied_request(_req(**{"x-real-ip": "203.0.113.9"}))


def test_quota_key_takes_the_hop_the_proxy_appended_not_the_clients_claim():
    proxy = "127.0.0.1"
    got = main._quota_ip({"x-forwarded-for": "8.8.8.8, 1.1.1.1, 192.0.2.77"}, proxy)
    assert got == "192.0.2.77"


def test_media_ingest_refuses_secret_trees(tmp_path, monkeypatch):
    secret = tmp_path / "ssh"
    secret.mkdir()
    pic = secret / "id.png"
    pic.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    monkeypatch.setattr(main, "_INGEST_DENY_ROOTS", (secret.resolve(),))
    assert main._ingest_local_file(str(pic)) is None
    # A file under the same name outside the deny root is still ingested.
    monkeypatch.setattr(main, "MEDIA_DIR", tmp_path / "media")
    (tmp_path / "media").mkdir()
    ok = tmp_path / "ok.png"
    ok.write_bytes(pic.read_bytes())
    assert (main._ingest_local_file(str(ok)) or "").startswith("/media/")


def test_ingest_deny_roots_cover_the_obvious_secret_stores():
    names = {p.name for p in main._INGEST_DENY_ROOTS}
    assert {".ssh", ".gnupg", "secrets", "agents", "etc", "proc"} <= names


async def test_doc_ref_with_a_traversing_stored_name_is_not_read(tmp_path, monkeypatch):
    """A file row whose stored_name escapes FILES_DIR must not be opened."""
    files_dir = tmp_path / "files"
    files_dir.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("SECRET CONTENT")
    monkeypatch.setattr(main, "FILES_DIR", files_dir)

    async def fake_get_file(file_id):
        return {"id": file_id, "name": "x.txt", "stored_name": "../outside.txt",
                "mime": "text/plain", "size": 14}
    monkeypatch.setattr(main.db, "get_file", fake_get_file)
    out = await main._resolve_doc_refs("[[doc:abc|x.txt]]")
    assert "SECRET CONTENT" not in out
    assert "x.txt" in out


async def test_read_routes_refuse_a_decoy_caller_without_the_middleware(tmp_path, monkeypatch):
    """The middleware matrix in test_auth_gate pins the 403; this pins the
    in-route check so a future route-table edit cannot silently uncover it."""
    from app import auth
    monkeypatch.setattr(auth, "SECURITY_PATH", tmp_path / "security.yaml")
    auth._cache = None
    auth._sessions.clear()
    auth.set_pin("1234")
    req = SimpleNamespace(headers={}, cookies={}, state=SimpleNamespace(),
                          client=SimpleNamespace(host="testclient"))
    for fn, args in ((main.get_all_bots, ()), (main.put_bot_order, ({"order": []},))):
        with pytest.raises(Exception) as ei:
            await fn(req, *args) if args else await fn(req)
        assert getattr(ei.value, "status_code", None) == 403, fn.__name__
