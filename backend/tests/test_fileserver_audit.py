"""File-Server / upload / media-serving audit regression tests.

Covers the abuse surface flagged in review ("gets used a lot"): concurrent
multi-file uploads, filename collisions + traversal + weird names, zero-byte
files, the one-way-drop 403 gate across verbs, MIME/XSS defang on served
files, chat-media Range support, and the Safe-Mode upload quota.

Hermetic style mirrors test_auth_gate.py: throwaway DB + monkeypatched data
dirs, nothing touches the live data dir or ~/.openclaw.
Run: cd backend && uv run pytest tests/test_fileserver_audit.py -q

Tests marked xfail(strict=False) expose real findings from the 2026-07-08
audit and should flip to pass once the referenced fix lands.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import os
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app import auth, config, main
from app.database import Database


@pytest.fixture
def fs_env(tmp_path, monkeypatch):
    """Isolated data dir + DB; yields a TestClient factory (mirrors gate_env)."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(config, "MEDIA_DIR", tmp_path / "media")
    monkeypatch.setattr(config, "FILES_DIR", tmp_path / "files")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(main, "MEDIA_DIR", tmp_path / "media")
    monkeypatch.setattr(main, "FILES_DIR", tmp_path / "files")
    monkeypatch.setattr(auth, "SECURITY_PATH", tmp_path / "security.yaml")
    monkeypatch.setattr(auth, "RECOVERY_PATH", tmp_path / "RECOVERY-CODE.txt")
    for d in (tmp_path / "media", tmp_path / "files", tmp_path / "logs",
              tmp_path / "backups"):
        d.mkdir(parents=True, exist_ok=True)
    auth._sessions.clear()
    auth._cache = None
    auth._fail_count = 0
    auth._fail_until = 0.0
    # reset the in-memory decoy quota accounting between tests
    main._decoy_upload_used.clear()
    main._decoy_upload_day = ""
    config._invalidate_bots_cache()

    temp_db = Database(tmp_path / "chats.db")
    monkeypatch.setattr(main, "db", temp_db)
    main._ACK_SEEN.clear()
    main._delivered.clear()
    main._thread_bot.clear()

    clients: list[TestClient] = []

    def make_client(client_addr=("127.0.0.1", 50000)) -> TestClient:
        c = TestClient(main.app, client=client_addr)
        c.__enter__()
        clients.append(c)
        return c

    yield make_client, tmp_path

    for c in clients:
        c.__exit__(None, None, None)
    asyncio.run(temp_db.close())


# --------------------------------------------------------------------------- #
# Correctness under load
# --------------------------------------------------------------------------- #

def test_concurrent_uploads_no_collision(fs_env):
    """20 parallel uploads incl. same-name files each get a distinct blob +
    DB record; no lost writes, no stored_name collision."""
    make_client, tmp_path = fs_env
    client = make_client()

    def up(i):
        name = "collide.txt" if i % 2 == 0 else f"uniq_{i}.bin"
        return client.post(
            "/api/files",
            files={"file": (name, b"x" * 4096, "application/octet-stream")},
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as ex:
        results = list(ex.map(up, range(20)))

    assert all(r.status_code == 200 for r in results)
    stored = [r.json()["stored_name"] for r in results]
    assert len(set(stored)) == 20, "stored_name collision under concurrency"
    listed = client.get("/api/files").json()["files"]
    assert len(listed) == 20
    # every DB record's blob exists on disk
    for rec in listed:
        assert (tmp_path / "files" / rec["stored_name"]).is_file()


def test_zero_byte_upload(fs_env):
    make_client, _ = fs_env
    client = make_client()
    r = client.post("/api/files", files={"file": ("empty.bin", b"", "application/octet-stream")})
    assert r.status_code == 200
    assert r.json()["size"] == 0


@pytest.mark.parametrize("filename", [
    "../../../etc/passwd",
    "..\\..\\windows\\system32\\x.dll",
    "/abs/path/evil.sh",
    "CON", "NUL.txt",
    "  spaces  .txt",
    "naïve_文件.pdf",
    "A" * 400 + ".bin",
    ".hidden",
])
def test_upload_filename_no_traversal(fs_env, filename):
    """No filename can escape FILES_DIR; stored blob is always a flat uuid."""
    make_client, tmp_path = fs_env
    client = make_client()
    r = client.post("/api/files", files={"file": (filename, b"data", "application/octet-stream")})
    assert r.status_code == 200
    stored = r.json()["stored_name"]
    p = (tmp_path / "files" / stored).resolve()
    assert p.parent == (tmp_path / "files").resolve(), f"blob escaped dir: {p}"
    assert p.is_file()
    # only the files dir itself should exist; no traversal-created subdirs
    subdirs = [x for x in (tmp_path / "files").iterdir() if x.is_dir()]
    assert subdirs == [], f"unexpected dirs created: {subdirs}"


# --------------------------------------------------------------------------- #
# One-way-drop 403 gate (intentional) holds across verbs/Range/HEAD
# --------------------------------------------------------------------------- #

def _upload_fileserver(client):
    r = client.post("/api/files", files={"file": ("doc.bin", b"secret-bytes", "application/octet-stream")})
    assert r.status_code == 200
    return r.json()["id"]


@pytest.mark.parametrize("verb,suffix,headers", [
    ("GET", "/download", {}),
    ("GET", "/raw", {}),
    ("HEAD", "/download", {}),
    ("GET", "/download", {"Range": "bytes=0-3"}),
    ("OPTIONS", "/download", {}),
])
def test_decoy_fileserver_read_blocked(fs_env, verb, suffix, headers):
    """Safe Mode (PIN set, no session) can never pull a fileserver blob.

    One client: upload while no PIN is set (open), then set a PIN. The same
    client carries no session cookie, so it is now a decoy — no second
    TestClient (which would bind the shared DB to a second event loop)."""
    make_client, _ = fs_env
    client = make_client()
    fid = _upload_fileserver(client)    # upload while open (no PIN yet)
    auth.set_pin("1234")                # cookieless client is now a decoy
    r = client.request(verb, f"/api/files/{fid}{suffix}", headers=headers)
    assert r.status_code == 403, f"{verb} {suffix} leaked: {r.status_code}"


def test_unlocked_session_can_download_fileserver_blob(fs_env):
    """The one-way drop is a SAFE-MODE rule, not a global one: an unlocked
    (full-session) operator gets view + download — deliberate, README-advertised
    behavior. Pinned here so a docs-driven 'fix' can't quietly delete the
    feature (DP-AUTH-6: the docs, not the code, were wrong)."""
    make_client, _ = fs_env
    client = make_client()
    fid = _upload_fileserver(client)    # upload while open (no PIN yet)
    auth.set_pin("1234")
    assert client.get(f"/api/files/{fid}/download").status_code == 403  # decoy
    assert client.post("/api/auth/unlock", json={"pin": "1234"}).status_code == 200
    r = client.get(f"/api/files/{fid}/download")
    assert r.status_code == 200, r.text
    assert r.content == b"secret-bytes"
    # /raw stays 403 even unlocked for non-previewable MIME types ("Preview
    # not available") — that's a preview policy, not the one-way gate.
    assert client.get(f"/api/files/{fid}/raw").status_code == 403


# --------------------------------------------------------------------------- #
# MIME / stored-XSS defang on served files
# --------------------------------------------------------------------------- #

def test_html_fileserver_raw_defanged(fs_env):
    make_client, _ = fs_env
    client = make_client()
    r = client.post("/api/files", files={
        "file": ("x.html", b"<script>alert(1)</script>", "text/html")})
    fid = r.json()["id"]
    raw = client.get(f"/api/files/{fid}/raw")
    assert raw.status_code == 200
    assert raw.headers["content-type"].startswith("text/plain")
    assert "sandbox" in raw.headers.get("content-security-policy", "")
    assert raw.headers.get("x-content-type-options") == "nosniff"


def test_html_as_png_spoof_not_executable(fs_env):
    """A .png-labelled HTML blob still carries nosniff + CSP so it can't run."""
    make_client, _ = fs_env
    client = make_client()
    r = client.post("/api/files", files={
        "file": ("evil.png", b"<html><script>alert(1)</script></html>", "image/png")})
    fid = r.json()["id"]
    raw = client.get(f"/api/files/{fid}/raw")
    assert raw.headers.get("x-content-type-options") == "nosniff"
    assert "sandbox" in raw.headers.get("content-security-policy", "")


def test_download_is_attachment_octet_stream(fs_env):
    make_client, _ = fs_env
    client = make_client()
    r = client.post("/api/files", files={"file": ("report.pdf", b"%PDF-1.4", "application/pdf")})
    fid = r.json()["id"]
    dl = client.get(f"/api/files/{fid}/download")
    assert dl.headers["content-type"] == "application/octet-stream"
    assert "attachment" in dl.headers.get("content-disposition", "")


# --------------------------------------------------------------------------- #
# Chat-media serving: Range support
# --------------------------------------------------------------------------- #

def test_media_serving_supports_range(fs_env):
    """FileResponse-served media honours Range (large-media seeking).

    Exercised via /api/files/<id>/raw for an image (a FileResponse code path
    reading the monkeypatched FILES_DIR). The live /media StaticFiles mount
    also returns 206 + Accept-Ranges (verified out-of-band); it can't be
    tested here because the mount binds to the import-time MEDIA_DIR."""
    make_client, _ = fs_env
    client = make_client()
    body = bytes(range(256)) * 8   # 2048 bytes
    up = client.post("/api/files", files={"file": ("pic.png", body, "image/png")})
    fid = up.json()["id"]
    r = client.get(f"/api/files/{fid}/raw", headers={"Range": "bytes=0-15"})
    assert r.status_code == 206
    assert r.headers.get("accept-ranges") == "bytes"
    assert r.headers["content-range"].endswith("/2048")
    assert len(r.content) == 16


# --------------------------------------------------------------------------- #
# Safe-Mode upload quota
# --------------------------------------------------------------------------- #

def test_decoy_quota_enforced_sequentially(fs_env, monkeypatch):
    """Serial decoy uploads stop at the daily byte budget (this DOES work)."""
    make_client, _ = fs_env
    monkeypatch.setattr(main, "DECOY_UPLOAD_QUOTA", 3 * 1024 * 1024)  # 3MB
    auth.set_pin("1234")
    client = make_client()
    chunk = b"z" * (1024 * 1024)   # 1MB each
    codes = [client.post("/api/upload",
                         files={"file": (f"s{i}.bin", chunk, "application/octet-stream")}
                         ).status_code for i in range(6)]
    assert 429 in codes, f"quota never tripped serially: {codes}"


@pytest.mark.asyncio
async def test_decoy_quota_not_bypassable_by_concurrency(fs_env, monkeypatch):
    """FINDING F2 (FIXED): two decoy uploads from the same client that actually
    INTERLEAVE (each yields the loop between chunks) must not both admit a full
    quota's worth. The fix charges a shared live counter per-chunk and refunds
    on failure, so combined committed bytes stay within the budget.

    TestClient serialises real threads, so we drive `_stream_upload` directly
    with two fake UploadFiles that await between chunks — a faithful model of
    the concurrency the live server sees. Pre-fix this over-committed to ~4MB;
    post-fix one upload wins and the other is rejected 429."""
    _, tmp_path = fs_env
    monkeypatch.setattr(main, "DECOY_UPLOAD_QUOTA", 3 * 1024 * 1024)  # 3MB
    monkeypatch.setattr(main, "FILES_TOTAL_MAX", 0)   # isolate: only test the daily quota
    main._decoy_upload_used.clear()
    main._decoy_upload_day = ""

    from types import SimpleNamespace

    class FakeUpload:
        def __init__(self, total, chunk):
            self.rem, self.chunk = total, chunk

        async def read(self, _n):
            await asyncio.sleep(0)        # yield → let the peer interleave
            if self.rem <= 0:
                return b""
            take = min(self.chunk, self.rem)
            self.rem -= take
            return b"z" * take

    req = SimpleNamespace(state=SimpleNamespace(decoy=True),
                          client=SimpleNamespace(host="1.2.3.4"))  # same client
    two_mb, half_mb = 2 * 1024 * 1024, 512 * 1024

    async def one(name):
        return await main._stream_upload(
            FakeUpload(two_mb, half_mb), tmp_path / "files", name,
            max_size=100 * 1024 * 1024, request=req, quota=True)

    results = await asyncio.gather(one("a.bin"), one("b.bin"),
                                   return_exceptions=True)
    rejected = [r for r in results if isinstance(r, main.HTTPException)]
    assert any(r.status_code == 429 for r in rejected), (
        f"quota bypassed by concurrency: {results}")
    # After failed uploads refund, the shared counter must never exceed budget.
    used = main._decoy_upload_used.get("1.2.3.4", 0)
    assert used <= main.DECOY_UPLOAD_QUOTA, f"overcommitted: {used} > quota"


# --------------------------------------------------------------------------- #
# F3: server-wide storage cap
# --------------------------------------------------------------------------- #

def test_total_storage_cap_enforced(fs_env, monkeypatch):
    """Uploads are refused (507) once the server-wide byte ceiling is reached."""
    make_client, tmp_path = fs_env
    monkeypatch.setattr(main, "FILES_TOTAL_MAX", 6 * 1024)   # 6KB total
    client = make_client()
    r1 = client.post("/api/files", files={"file": ("a.bin", b"x" * 4096, "application/octet-stream")})
    assert r1.status_code == 200
    # Second 4KB would push total to 8KB > 6KB cap → rejected, no torn blob left.
    r2 = client.post("/api/files", files={"file": ("b.bin", b"x" * 4096, "application/octet-stream")})
    assert r2.status_code == 507, r2.text
    assert list((tmp_path / "files").glob("*.part")) == []
    assert len(client.get("/api/files").json()["files"]) == 1


# --------------------------------------------------------------------------- #
# F5: atomic write (.part) + startup orphan sweep
# --------------------------------------------------------------------------- #

def test_no_part_file_left_after_success(fs_env):
    make_client, tmp_path = fs_env
    client = make_client()
    client.post("/api/files", files={"file": ("ok.bin", b"data" * 100, "application/octet-stream")})
    assert list((tmp_path / "files").glob("*.part")) == [], "leftover .part after success"


def test_failed_upload_leaves_no_part(fs_env, monkeypatch):
    """A per-file-cap rejection must not leave a partial or .part behind."""
    make_client, tmp_path = fs_env
    monkeypatch.setattr(main, "FILE_UPLOAD_MAX", 1024)   # 1KB cap
    client = make_client()
    r = client.post("/api/files", files={"file": ("big.bin", b"x" * 8192, "application/octet-stream")})
    assert r.status_code == 413
    assert list((tmp_path / "files").iterdir()) == [], "residue after failed upload"
    assert client.get("/api/files").json()["files"] == []


@pytest.mark.asyncio
async def test_operator_routes_deny_a_decoy_in_the_handler_itself(fs_env):
    """These routes used to be protected ONLY by the `_decoy_blocked` prefix
    tuple in the middleware — their security was a string list in another
    function. Each now re-derives it, so calling the handler directly (a route
    remounted at a new path, a refactored middleware) still refuses. /api/files
    /wipe empties the whole File Server, so it is the sharpest of them."""
    from types import SimpleNamespace

    auth.set_pin("1234")
    decoy = SimpleNamespace(state=SimpleNamespace(decoy=True, session=None,
                                                  machine=False),
                            cookies={})
    calls = [
        main.file_list(decoy),
        main.file_delete("x", decoy),
        main.file_wipe(decoy, {"before": "2026-01-01T00:00:00"}),
        main.search_messages(decoy, q="secret"),
        main.export_all(decoy),
        main.recover_transcript(decoy, {"all": True}),
        main.openclaw_sessions(decoy, bot_id="main"),
        main.openclaw_transcript(decoy, bot_id="main", thread_id="t"),
        main.openclaw_import_session(decoy, {"bot_id": "main", "session_key": "k"}),
    ]
    for call in calls:
        with pytest.raises(main.HTTPException) as e:
            await call
        assert e.value.status_code == 403, call


@pytest.mark.asyncio
async def test_client_disconnect_leaves_no_part_and_refunds_quota(fs_env, monkeypatch):
    """The cleanup used to live in `except HTTPException` / `except OSError`,
    and the two exceptions this path actually sees are neither: Starlette's
    ClientDisconnect (a phone walking out of range) and CancelledError. Both
    left the .part on disk forever AND left the decoy's day charged."""
    _, tmp_path = fs_env
    monkeypatch.setattr(main, "FILES_TOTAL_MAX", 0)
    main._decoy_upload_used.clear()
    main._decoy_upload_day = ""

    from types import SimpleNamespace

    class Disconnecting:
        def __init__(self):
            self.calls = 0

        async def read(self, _n):
            self.calls += 1
            if self.calls == 1:
                return b"z" * (256 * 1024)
            raise RuntimeError("client disconnected")   # not HTTPException/OSError

    req = SimpleNamespace(state=SimpleNamespace(decoy=True),
                          client=SimpleNamespace(host="9.9.9.9"))
    with pytest.raises(RuntimeError):
        await main._stream_upload(Disconnecting(), tmp_path / "files", "gone.bin",
                                  max_size=100 * 1024 * 1024, request=req, quota=True)
    assert list((tmp_path / "files").glob("*.part")) == [], "leaked .part file"
    assert main._decoy_upload_used.get("9.9.9.9", 0) == 0, "quota was not refunded"


@pytest.mark.asyncio
async def test_orphan_sweep_adopts_untracked_blob(fs_env):
    """An agent-dropped blob must SURVIVE the boot sweep and gain a DB row.

    On-box agents write straight into FILES_DIR (a scheduled agent pipeline
    drops dated images there daily) and reference the path; those files never
    get a row. The sweep used to delete them, and the nightly ~02:30
    backup-quiesce restart therefore wiped the day's files on every boot.
    Stale .part temp files still die.
    """
    _, tmp_path = fs_env
    files_dir = tmp_path / "files"
    files_dir.mkdir(parents=True, exist_ok=True)
    orphan = files_dir / "influencer-2026-08-27.jpg"
    orphan.write_bytes(b"untracked" * 10)
    old_mtime = 1_700_000_000
    os.utime(orphan, (old_mtime, old_mtime))
    stray_part = files_dir / "half.bin.part"
    stray_part.write_bytes(b"partial")
    # main.db is set by the fixture but not connected; the sweep lists files via
    # a connected DB, so connect it here for the duration of the sweep.
    await main.db.connect()
    try:
        await main._sweep_orphan_blobs()
        rows = await main.db.list_files()
        total = await main.db.total_file_bytes()
    finally:
        await main.db.close()

    assert orphan.exists(), "agent-dropped blob was deleted by the sweep"
    assert not stray_part.exists(), "stale .part not swept"
    assert len(rows) == 1, "untracked blob was not adopted"
    row = rows[0]
    assert row["name"] == orphan.name
    assert row["stored_name"] == orphan.name
    assert row["size"] == orphan.stat().st_size
    assert row["mime"] == "image/jpeg"
    assert row["source"] == "fileserver"
    # created_at comes from the blob's mtime, not "now", so retention is honest.
    assert row["created_at"].startswith(
        datetime.fromtimestamp(old_mtime, UTC).isoformat()[:10]
    )
    # Adopted bytes count toward the server-wide storage cap.
    assert total == orphan.stat().st_size


@pytest.mark.asyncio
async def test_orphan_sweep_is_idempotent(fs_env):
    """A second boot must not adopt the same blob twice."""
    _, tmp_path = fs_env
    files_dir = tmp_path / "files"
    files_dir.mkdir(parents=True, exist_ok=True)
    (files_dir / "drop.txt").write_bytes(b"hello")
    await main.db.connect()
    try:
        await main._sweep_orphan_blobs()
        await main._sweep_orphan_blobs()
        rows = await main.db.list_files()
    finally:
        await main.db.close()
    assert len(rows) == 1, f"adopted twice: {rows}"


# --------------------------------------------------------------------------- #
# F6: stored original filename length cap
# --------------------------------------------------------------------------- #

def test_long_filename_capped(fs_env):
    make_client, _ = fs_env
    client = make_client()
    long_name = "A" * 400 + ".bin"
    r = client.post("/api/files", files={"file": (long_name, b"data", "application/octet-stream")})
    assert r.status_code == 200
    assert len(r.json()["name"]) <= 255


# --------------------------------------------------------------------------- #
# Boot sweep: phantom records (BOX-05)
# --------------------------------------------------------------------------- #


def test_boot_sweep_purges_records_with_no_blob(fs_env):
    """The File Server listing must not be full of rows whose download 404s.

    The live box had 43 records and an EMPTY blob directory: every entry dead,
    and every agent told to "read it off disk" chasing a path that could not
    exist. The sweep used to only log a warning.
    """
    make_client, tmp = fs_env
    make_client()
    (tmp / "files" / "real.bin").write_bytes(b"blob")

    async def _seed():
        kept = await main.db.add_file("Real.pdf", "real.bin", 4, "application/pdf")
        await main.db.add_file("Ghost.pdf", "ghost.bin", 999, "application/pdf")
        return kept
    kept = asyncio.run(_seed())

    asyncio.run(main._sweep_orphan_blobs())

    rows = asyncio.run(main.db.list_files())
    assert [r["id"] for r in rows] == [kept["id"]]


def test_boot_sweep_refuses_to_purge_when_the_blob_dir_is_gone(fs_env):
    """If FILES_DIR itself does not resolve (unmounted share, broken symlink —
    it IS a symlink on this box) then EVERY blob looks absent. Deleting the
    whole table on a mount hiccup is the one unrecoverable outcome here, so the
    sweep must skip rather than purge."""
    make_client, tmp = fs_env
    make_client()

    async def _seed():
        await main.db.add_file("A.pdf", "a.bin", 1, "application/pdf")
        await main.db.add_file("B.pdf", "b.bin", 2, "application/pdf")
    asyncio.run(_seed())

    # Point at a path that does not exist — the "mount went away" shape.
    main.FILES_DIR = tmp / "files-unmounted"
    try:
        asyncio.run(main._sweep_orphan_blobs())
        assert len(asyncio.run(main.db.list_files())) == 2      # nothing deleted
    finally:
        main.FILES_DIR = tmp / "files"
