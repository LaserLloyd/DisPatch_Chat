"""Per-task workspaces: layout, traversal and symlink escapes, worktrees (G17)."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from app.agents.workspace import (
    WorkspaceError,
    check_task_id,
    cleanup_workspace,
    existing_workspace,
    prepare_workspace,
    resolve_inside,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "agents"


def git(*args, cwd):
    subprocess.run(["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t", *args], cwd=cwd,
                   check=True, capture_output=True)


def make_repo(tmp_path: Path, src: Path | None = None) -> Path:
    repo = tmp_path / "repo"
    if src is not None:
        shutil.copytree(src, repo)
    else:
        repo.mkdir()
        (repo / "a.txt").write_text("a")
    git("init", "-q", cwd=repo)
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "init", cwd=repo)
    return repo


def test_a_scratch_workspace_lives_under_the_data_dir(agent_data):
    ws = prepare_workspace(agent_data, "task-1")
    assert ws.kind == "scratch" and ws.root.is_dir()
    assert ws.base == agent_data / "agents" / "task-1"
    assert ws.config_dir == ws.base / ".claude" and ws.home == ws.base / "home"
    assert ws.root.resolve().is_relative_to(agent_data.resolve())


@pytest.mark.parametrize("bad", ["", "../x", "a/b", "a\\b", "..", ".hidden", "x" * 65, "a b", "a\x00b", "C:evil"])
def test_task_ids_cannot_walk_out(bad):
    with pytest.raises(WorkspaceError):
        check_task_id(bad)


def test_a_workspace_is_not_created_twice(agent_data):
    prepare_workspace(agent_data, "t")
    with pytest.raises(WorkspaceError, match="already exists"):
        prepare_workspace(agent_data, "t")


@pytest.mark.parametrize("rel", ["../escape.txt", "a/../../escape.txt", "..\\escape.txt", "/etc/passwd",
                                 "\\windows\\system32", "C:\\Windows\\win.ini", "C:win.ini", "", "a\x00b"])
def test_resolve_inside_rejects_traversal_and_absolute_paths(agent_data, rel):
    ws = prepare_workspace(agent_data, "t")
    with pytest.raises(WorkspaceError):
        resolve_inside(ws.root, rel)


def test_resolve_inside_accepts_normal_relative_paths(agent_data):
    ws = prepare_workspace(agent_data, "t")
    p = resolve_inside(ws.root, "out/report.md")
    assert p == ws.root.resolve() / "out" / "report.md"


def _link_dir(link: Path, target: Path) -> bool:
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        pass
    if sys.platform == "win32":                 # a junction needs no privilege
        cp = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
        return cp.returncode == 0
    return False


def test_a_symlink_or_junction_pointing_outside_is_rejected(agent_data, tmp_path):
    ws = prepare_workspace(agent_data, "t")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("s")
    if not _link_dir(ws.root / "link", outside):
        pytest.skip("this account cannot create symlinks or junctions")
    with pytest.raises(WorkspaceError, match="outside"):
        resolve_inside(ws.root, "link/secret.txt")
    with pytest.raises(WorkspaceError, match="outside"):
        resolve_inside(ws.root, "link")


def test_a_symlinked_file_pointing_outside_is_rejected(agent_data, tmp_path):
    ws = prepare_workspace(agent_data, "t")
    secret = tmp_path / "secret.txt"
    secret.write_text("s")
    try:
        os.symlink(secret, ws.root / "f.txt")
    except (OSError, NotImplementedError):
        pytest.skip("this account cannot create file symlinks")
    with pytest.raises(WorkspaceError, match="outside"):
        resolve_inside(ws.root, "f.txt")


def test_a_repo_task_gets_a_detached_worktree(agent_data, tmp_path):
    repo = make_repo(tmp_path)
    ws = prepare_workspace(agent_data, "wt1", mode="worktree", repo_path=repo)
    assert ws.kind == "worktree" and (ws.root / "a.txt").read_text() == "a"
    listing = subprocess.run(["git", "worktree", "list"], cwd=repo, capture_output=True, text=True).stdout
    assert "wt1" in listing.replace("\\", "/")
    again = existing_workspace(agent_data, "wt1")
    assert again.kind == "worktree" and again.root == ws.root
    cleanup_workspace(ws)
    assert not ws.base.exists()
    listing = subprocess.run(["git", "worktree", "list"], cwd=repo, capture_output=True, text=True).stdout
    assert "wt1" not in listing, "the repo forgot the removed worktree"


def test_worktree_mode_needs_a_real_repo(agent_data, tmp_path):
    with pytest.raises(WorkspaceError):
        prepare_workspace(agent_data, "x", mode="worktree", repo_path=None)
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(WorkspaceError, match="not a git repository"):
        prepare_workspace(agent_data, "y", mode="worktree", repo_path=plain)


def test_repo_hooks_do_not_run_while_the_worktree_is_created(agent_data, tmp_path):
    repo = make_repo(tmp_path)
    marker = tmp_path / "HOOK_RAN"
    hook = repo / ".git" / "hooks" / "post-checkout"
    hook.write_text(f"#!/bin/sh\necho x > '{marker.as_posix()}'\n")
    hook.chmod(0o755)
    prepare_workspace(agent_data, "wt2", mode="worktree", repo_path=repo)
    assert not marker.exists(), "an untrusted repo's own hook must not execute"


def test_existing_workspace_requires_one(agent_data):
    with pytest.raises(WorkspaceError):
        existing_workspace(agent_data, "nothing")
    ws = prepare_workspace(agent_data, "keep")
    assert existing_workspace(agent_data, "keep").root == ws.root


def test_cleanup_removes_a_scratch_workspace(agent_data):
    ws = prepare_workspace(agent_data, "gone")
    (ws.root / "f.txt").write_text("x")
    cleanup_workspace(ws)
    assert not ws.base.exists()
