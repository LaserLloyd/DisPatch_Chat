"""4.3 Fable: the guide pictures and the screenshot allowlist agree.

``frontend/static/guide/my-bots.png`` showed the removed Settings -> My bots roster and has been
unreferenced since the 4.3 Opus review; it is gone from the tree and from
``docs/screenshots/ALLOWED.txt``. Every guide picture on the allowlist exists, every guide
picture in the tree is on the allowlist, and no guide text names a picture that is not there.

Run: cd backend && uv run pytest tests/test_fable_guide_pictures.py
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GUIDE = ROOT / "frontend" / "static" / "guide"


def _allowed() -> set[str]:
    lines = (ROOT / "docs" / "screenshots" / "ALLOWED.txt").read_text(encoding="utf-8").splitlines()
    return {ln.strip()[len("guide/"):] for ln in lines if ln.strip().startswith("guide/")}


def test_my_bots_picture_is_gone():
    assert not (GUIDE / "my-bots.png").exists()
    assert "my-bots.png" not in _allowed()


def test_allowlist_and_tree_agree():
    on_disk = {p.name for p in GUIDE.glob("*.png")}
    assert on_disk == _allowed()


def test_guide_text_names_only_pictures_that_exist():
    on_disk = {p.name for p in GUIDE.glob("*.png")}
    for md in (ROOT / "backend" / "app" / "guide").glob("*.md"):
        for name in re.findall(r"guide/([\w.-]+\.png)", md.read_text(encoding="utf-8")):
            assert name in on_disk, (md.name, name)
