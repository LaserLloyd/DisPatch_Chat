"""Tests for the ComfyUI workflow manager (/api/comfy/workflows*).

Hermetic: the workflows + snapshot dirs are monkeypatched to tmp dirs — the
live ComfyUI tree is never touched. Same fixture style as
test_comfy_routes.py. Run: cd backend && uv run pytest.
"""
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app import auth, config, main
from app import comfy_service as cs
from app.database import Database

WF = {"nodes": [{"id": 1, "type": "KSampler"}], "version": 1}
WF_BODY = json.dumps(WF).encode()


@pytest.fixture
def wf_dirs(tmp_path, monkeypatch):
    """Isolated workflow + snapshot dirs (what config's env overrides do)."""
    wdir = tmp_path / "workflows"
    bdir = tmp_path / "workflow-backups"
    wdir.mkdir(parents=True)
    monkeypatch.setattr(config, "COMFY_WORKFLOWS_DIR", wdir)
    monkeypatch.setattr(config, "COMFY_WORKFLOW_BACKUP_DIR", bdir)
    return wdir, bdir


@pytest.fixture
def wf_client(tmp_path, monkeypatch, wf_dirs):
    """Full-isolation TestClient with the comfy feature forced on."""
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
    auth._sessions.clear()
    auth._cache = None
    auth._fail_count = 0
    auth._fail_until = 0.0
    config._invalidate_bots_cache()

    temp_db = Database(tmp_path / "chats.db")
    monkeypatch.setattr(main, "db", temp_db)
    monkeypatch.setattr(main, "SETTINGS", replace(config.SETTINGS, comfy_enabled=True))

    with TestClient(main.app) as client:
        yield client

    asyncio.run(temp_db.close())


# --------------------------------------------------------------------------- #
# Round trip: save → list → download
# --------------------------------------------------------------------------- #


def test_save_list_download_round_trip(wf_client, wf_dirs):
    wdir, _ = wf_dirs
    r = wf_client.post("/api/comfy/workflows/My Flow.json", content=WF_BODY)
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "name": "My Flow.json"}
    assert (wdir / "My Flow.json").read_bytes() == WF_BODY

    r = wf_client.get("/api/comfy/workflows")
    assert r.status_code == 200
    body = r.json()
    assert body["dir"] == str(wdir)
    assert body["last_backup_epoch"] is None
    names = [w["name"] for w in body["workflows"]]
    assert names == ["My Flow.json"]
    assert body["workflows"][0]["size"] == len(WF_BODY)
    assert body["workflows"][0]["modified_epoch"] > 0

    r = wf_client.get("/api/comfy/workflows/My Flow.json")
    assert r.status_code == 200
    assert r.content == WF_BODY
    assert "attachment" in r.headers.get("content-disposition", "")


def test_list_is_sorted_newest_first(wf_client, wf_dirs):
    wdir, _ = wf_dirs
    old = wdir / "old.json"
    new = wdir / "new.json"
    old.write_bytes(WF_BODY)
    new.write_bytes(WF_BODY)
    os.utime(old, (1_000_000, 1_000_000))
    r = wf_client.get("/api/comfy/workflows")
    assert [w["name"] for w in r.json()["workflows"]] == ["new.json", "old.json"]


def test_download_missing_404(wf_client, wf_dirs):
    assert wf_client.get("/api/comfy/workflows/nope.json").status_code == 404


# --------------------------------------------------------------------------- #
# Overwrite creates a single rolling .bak
# --------------------------------------------------------------------------- #


def test_overwrite_creates_rolling_bak(wf_client, wf_dirs):
    wdir, _ = wf_dirs
    v1 = json.dumps({"v": 1}).encode()
    v2 = json.dumps({"v": 2}).encode()
    v3 = json.dumps({"v": 3}).encode()
    assert wf_client.post("/api/comfy/workflows/flow.json", content=v1).status_code == 200
    assert not (wdir / "flow.bak.json").exists()      # first save: no .bak
    assert wf_client.post("/api/comfy/workflows/flow.json", content=v2).status_code == 200
    assert (wdir / "flow.bak.json").read_bytes() == v1
    assert wf_client.post("/api/comfy/workflows/flow.json", content=v3).status_code == 200
    assert (wdir / "flow.bak.json").read_bytes() == v2   # rolling: only the last
    assert (wdir / "flow.json").read_bytes() == v3


# --------------------------------------------------------------------------- #
# Validation: path safety, size cap, JSON check
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("bad", [
    "../evil.json",
    "..%2Fevil.json",
    "/etc/passwd.json",
    "a/b.json",
    "..json",
    ".hidden.json",
    ".json",
    "evil.txt",
    "evil",
    "",
])
def test_bad_names_rejected(wf_dirs, bad):
    with pytest.raises(cs.WorkflowError) as ei:
        cs.workflow_path(bad)
    assert ei.value.status == 400


def test_symlink_escape_rejected(wf_dirs, tmp_path):
    wdir, _ = wf_dirs
    outside = tmp_path / "outside.json"
    outside.write_bytes(WF_BODY)
    (wdir / "link.json").symlink_to(outside)
    with pytest.raises(cs.WorkflowError):
        cs.workflow_path("link.json")


def test_route_rejects_non_json_name_and_bad_body(wf_client, wf_dirs):
    assert wf_client.post("/api/comfy/workflows/evil.txt", content=WF_BODY).status_code == 400
    r = wf_client.post("/api/comfy/workflows/x.json", content=b"{not json")
    assert r.status_code == 422
    wdir, _ = wf_dirs
    assert not (wdir / "x.json").exists()


def test_oversize_rejected(wf_client, wf_dirs):
    big = b'{"pad": "' + b"x" * cs.WORKFLOW_MAX_BYTES + b'"}'
    r = wf_client.post("/api/comfy/workflows/big.json", content=big)
    assert r.status_code == 413
    wdir, _ = wf_dirs
    assert not (wdir / "big.json").exists()


# --------------------------------------------------------------------------- #
# Delete → .trash (with cap)
# --------------------------------------------------------------------------- #


def test_delete_moves_to_trash(wf_client, wf_dirs):
    wdir, _ = wf_dirs
    (wdir / "gone.json").write_bytes(WF_BODY)
    r = wf_client.request("DELETE", "/api/comfy/workflows/gone.json")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "trashed": True}
    assert not (wdir / "gone.json").exists()
    assert (wdir / ".trash" / "gone.json").read_bytes() == WF_BODY
    # Deleting again: it's gone from the live dir.
    assert wf_client.request("DELETE", "/api/comfy/workflows/gone.json").status_code == 404
    # Trash contents never show up in the list.
    assert wf_client.get("/api/comfy/workflows").json()["workflows"] == []


def test_trash_collision_suffix_and_prune(wf_dirs):
    wdir, _ = wf_dirs
    # Collision: same name trashed twice → second gets an epoch suffix.
    for _ in range(2):
        (wdir / "dup.json").write_bytes(WF_BODY)
        cs.trash_workflow("dup.json")
    trash = wdir / ".trash"
    assert len(list(trash.iterdir())) == 2
    # Cap: trash many more; at most WORKFLOW_TRASH_KEEP files survive.
    for i in range(cs.WORKFLOW_TRASH_KEEP + 5):
        (wdir / f"wf-{i:02d}.json").write_bytes(WF_BODY)
        cs.trash_workflow(f"wf-{i:02d}.json")
    files = list(trash.iterdir())
    assert len(files) == cs.WORKFLOW_TRASH_KEEP
    # The most recently trashed file is always kept.
    assert trash / f"wf-{cs.WORKFLOW_TRASH_KEEP + 4:02d}.json" in files


# --------------------------------------------------------------------------- #
# Snapshots: route, rotation, auto-hook
# --------------------------------------------------------------------------- #


def test_backup_route_snapshots_and_reports_epoch(wf_client, wf_dirs):
    wdir, bdir = wf_dirs
    (wdir / "a.json").write_bytes(WF_BODY)
    (wdir / "b.json").write_bytes(WF_BODY)
    r = wf_client.post("/api/comfy/workflows/backup")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["count"] == 2
    snap = bdir / body["snapshot"]
    assert sorted(p.name for p in snap.iterdir()) == ["a.json", "b.json"]
    epoch = wf_client.get("/api/comfy/workflows").json()["last_backup_epoch"]
    assert isinstance(epoch, int) and epoch > 0


def test_snapshot_rotation_keeps_20(wf_dirs):
    wdir, bdir = wf_dirs
    (wdir / "a.json").write_bytes(WF_BODY)
    for _ in range(cs.WORKFLOW_SNAPSHOT_KEEP + 3):
        cs.snapshot_workflows()
    dirs = [p for p in bdir.iterdir() if p.is_dir()]
    assert len(dirs) == cs.WORKFLOW_SNAPSHOT_KEEP


def test_maybe_snapshot_only_when_newer(wf_dirs):
    wdir, bdir = wf_dirs
    assert cs.maybe_snapshot_workflows() is None          # nothing to back up
    (wdir / "a.json").write_bytes(WF_BODY)
    first = cs.maybe_snapshot_workflows()
    assert first is not None and first["count"] == 1
    assert cs.maybe_snapshot_workflows() is None          # nothing changed
    # Touch the workflow into the future → a new snapshot is due.
    import time as _t
    future = _t.time() + 60
    os.utime(wdir / "a.json", (future, future))
    second = cs.maybe_snapshot_workflows()
    assert second is not None and second["snapshot"] != first["snapshot"]


# --------------------------------------------------------------------------- #
# Gating: decoy 403 on every new route; feature-off 404
# --------------------------------------------------------------------------- #

WORKFLOW_ROUTES = [
    ("GET", "/api/comfy/workflows", None),
    ("GET", "/api/comfy/workflows/a.json", None),
    ("POST", "/api/comfy/workflows/a.json", WF_BODY),
    ("DELETE", "/api/comfy/workflows/a.json", None),
    ("POST", "/api/comfy/workflows/backup", None),
]


@pytest.mark.parametrize("method,path,body", WORKFLOW_ROUTES)
def test_decoy_gets_403_on_every_workflow_route(wf_client, method, path, body):
    auth.set_pin("1234")     # PIN set, this client never unlocks → decoy
    r = wf_client.request(method, path, content=body)
    assert r.status_code == 403, f"{method} {path} -> {r.status_code}"


@pytest.mark.parametrize("method,path,body", WORKFLOW_ROUTES)
def test_feature_disabled_404s_workflow_routes(wf_client, monkeypatch, method, path, body):
    monkeypatch.setattr(main, "SETTINGS", replace(config.SETTINGS, comfy_enabled=False))
    r = wf_client.request(method, path, content=body)
    assert r.status_code == 404, f"{method} {path} -> {r.status_code}"
