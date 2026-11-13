"""Guard: no tracked file names the private project this repo must not mention."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
# Assembled so this file does not match itself.
PATTERN = re.compile("cl" + r"aw[-_ ]?forge", re.I)


def _tracked() -> list[str]:
    try:
        out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, check=True,
                             capture_output=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        pytest.skip("not a git checkout")
    return [p for p in out.decode("utf-8", "replace").split("\0") if p]


def test_no_tracked_file_names_the_private_project():
    hits: list[str] = []
    for rel in _tracked():
        path = ROOT / rel
        if not path.is_file():
            continue
        try:
            text = path.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if "\0" in text:
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if PATTERN.search(line):
                hits.append(f"{rel}:{n}")
    assert not hits, "private project name in tracked files: " + ", ".join(hits[:30])
