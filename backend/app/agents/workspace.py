"""Per-task workspaces under the data dir.

Layout (everything below ``<data>/agents/<task_id>/``)::

    work/     the agent's cwd: an empty scratch dir, or a git worktree of the repo
    .claude/  CLAUDE_CONFIG_DIR for the Claude backend (RESEARCH.md section 5 item 10)
    home/     a private HOME, so the user's own ~/.claude.json is never read
              (R-ISO: it is read regardless of setting_sources)

Rules:

* The task id is a strict token, so it can never walk out of ``agents/``.
* A task that targets a repo gets a *detached git worktree* (brief; RESEARCH.md
  section 5 item 19: the product, not the SDK, removes it afterwards).
* The repo's own hooks are neutralised while the worktree is created
  (``core.hooksPath`` -> empty dir): the repo is untrusted input.
* :func:`resolve_inside` is the only way agent-supplied relative paths (artifact
  names, uploads) are turned into real paths; it refuses absolute paths,
  ``..``, Windows drive/ADS tricks and anything that resolves outside the root
  once symlinks and junctions are followed (G17: workspace escapes blocked).
"""
from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Literal

TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


class WorkspaceError(Exception):
    """A path escape, a bad task id, or a repo that cannot be checked out."""


@dataclass(frozen=True)
class Workspace:
    task_id: str
    base: Path
    root: Path                 # the agent's cwd
    config_dir: Path           # CLAUDE_CONFIG_DIR
    home: Path
    kind: Literal["scratch", "worktree"]
    repo: Path | None = None


def check_task_id(task_id: str) -> str:
    if not isinstance(task_id, str) or not TASK_ID_RE.match(task_id):
        raise WorkspaceError(f"invalid task id {task_id!r} (letters, digits, '_' and '-' only)")
    return task_id


def task_base(data_dir: Path, task_id: str) -> Path:
    check_task_id(task_id)
    return Path(data_dir) / "agents" / task_id


def _git(args: list[str], *, cwd: Path | None = None, hooks_dir: Path | None = None) -> str:
    cmd = ["git"]
    if hooks_dir is not None:
        cmd += ["-c", f"core.hooksPath={hooks_dir}"]
    cmd += args
    try:
        cp = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=120,
                            check=False, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise WorkspaceError(f"git failed to run: {e}") from e
    if cp.returncode != 0:
        raise WorkspaceError(f"git {' '.join(args[:3])} failed: {(cp.stderr or cp.stdout).strip()[:300]}")
    return cp.stdout


def prepare_workspace(data_dir: Path, task_id: str, *, mode: str = "scratch",
                      repo_path: Path | None = None, ref: str = "HEAD") -> Workspace:
    """Create the task's directories (and its worktree, if it targets a repo)."""
    base = task_base(data_dir, task_id)
    root, cfg, home = base / "work", base / ".claude", base / "home"
    if mode not in ("scratch", "worktree"):
        raise WorkspaceError(f"unknown workspace mode {mode!r}")
    if mode == "worktree" and repo_path is None:
        raise WorkspaceError("workspace mode 'worktree' needs a repo path")
    if root.exists():
        raise WorkspaceError(f"workspace for task {task_id!r} already exists")
    base.mkdir(parents=True, exist_ok=True)
    cfg.mkdir(exist_ok=True)
    home.mkdir(exist_ok=True)
    if mode == "scratch":
        root.mkdir()
        return Workspace(task_id, base, root, cfg, home, "scratch")

    repo = Path(repo_path).resolve(strict=True)
    if not (repo / ".git").exists():
        raise WorkspaceError(f"{repo} is not a git repository")
    nohooks = base / "nohooks"
    nohooks.mkdir(exist_ok=True)
    _git(["worktree", "add", "--detach", str(root), ref], cwd=repo, hooks_dir=nohooks)
    return Workspace(task_id, base, root, cfg, home, "worktree", repo)


def existing_workspace(data_dir: Path, task_id: str) -> Workspace:
    """Re-open the workspace of a task that already has one (restart / resume)."""
    base = task_base(data_dir, task_id)
    root = base / "work"
    if not root.is_dir():
        raise WorkspaceError(f"task {task_id!r} has no workspace")
    kind: Literal["scratch", "worktree"] = "worktree" if (root / ".git").exists() else "scratch"
    repo: Path | None = None
    if kind == "worktree":
        # A worktree's .git is a file: "gitdir: <repo>/.git/worktrees/<name>".
        try:
            gitdir = (root / ".git").read_text(encoding="utf-8").split(":", 1)[1].strip()
            repo = Path(gitdir).parents[2]
        except (OSError, IndexError, ValueError):
            repo = None
    (base / ".claude").mkdir(exist_ok=True)
    (base / "home").mkdir(exist_ok=True)
    return Workspace(task_id, base, root, base / ".claude", base / "home", kind, repo)


def resolve_inside(root: Path, relative: str | os.PathLike[str]) -> Path:
    """``root / relative`` as a real path, or WorkspaceError if it would leave ``root``."""
    rel = os.fspath(relative)
    if not rel or "\x00" in rel:
        raise WorkspaceError("empty or NUL-containing path")
    win = PureWindowsPath(rel)
    if os.path.isabs(rel) or win.is_absolute() or win.drive or rel[:1] in ("/", "\\"):
        raise WorkspaceError(f"absolute path not allowed: {rel!r}")
    parts = [p for p in re.split(r"[\\/]+", rel) if p]
    if ".." in parts:
        raise WorkspaceError(f"'..' not allowed in {rel!r}")
    if os.name == "nt" and any(":" in p for p in parts):
        raise WorkspaceError(f"drive or stream syntax not allowed in {rel!r}")
    root_real = Path(root).resolve(strict=True)
    cand = (root_real / Path(*parts)).resolve(strict=False) if parts else root_real
    try:
        cand.relative_to(root_real)
    except ValueError:
        raise WorkspaceError(f"{rel!r} resolves outside the workspace") from None
    return cand


def _rm_readonly(func, path, _exc):             # pragma: no cover - Windows git objects
    os.chmod(path, stat.S_IWRITE)
    func(path)


def cleanup_workspace(ws: Workspace) -> None:
    """Remove the worktree (properly, so the repo forgets it) and the task dir."""
    if ws.kind == "worktree" and ws.repo is not None and ws.root.exists():
        try:
            _git(["worktree", "remove", "--force", str(ws.root)], cwd=ws.repo, hooks_dir=ws.base / "nohooks")
        except WorkspaceError:
            pass        # fall through to deleting the directory; `git worktree prune` mops up
        try:
            _git(["worktree", "prune"], cwd=ws.repo, hooks_dir=ws.base / "nohooks")
        except WorkspaceError:
            pass
    if ws.base.exists():
        shutil.rmtree(ws.base, onexc=_rm_readonly)
