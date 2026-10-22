"""The vendored ForgeUI bundle (frontend/static/ui-theme/) must not drift from forgeui.json.

Two layers, neither needs the network or the app:

* ``test_vendored_copy_*`` need only the standard library. They prove the folder is the
  bundle it claims to be (every ``files.json`` hash matches, nothing hand-edited, nothing
  extra but the app-owned overlay) and that the picker settings in ``index.html`` come
  from the manifest.
* ``test_forgeui_vendor_check_passes`` runs the real ``forgeui vendor --check`` against the
  installed ``forgeui`` package. CI's lint job installs it from a pinned ThemeForge commit
  and runs the same command; here it SKIPS with a message when the package is absent
  (``pip install`` it to run the drift check locally), or FAILS when ``FORGEUI_REQUIRED``
  is set.

To fix a failure: ``python -m forgeui vendor`` (docs/development.md, "Themes").
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "forgeui.json"


def _manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _dest() -> Path:
    return ROOT / _manifest()["dest"]


def _digest(path: Path) -> str:
    """sha256 over LF line endings, as the bundle's files.json is computed."""
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def test_vendored_copy_matches_its_files_json() -> None:
    dest = _dest()
    listed = json.loads((dest / "files.json").read_text(encoding="utf-8"))["files"]
    problems = [f"missing {rel}" for rel in listed if not (dest / rel).is_file()]
    problems += [
        f"edited {rel}: move the change into the overlay and re-run `python -m forgeui vendor`"
        for rel, want in listed.items()
        if (dest / rel).is_file() and _digest(dest / rel) != want
    ]
    owned = set(listed) | set(_manifest().get("extra_files", {})) | {"files.json"}
    present = [
        p.relative_to(dest).as_posix()
        for p in sorted(dest.rglob("*"))
        if p.is_file() and "__pycache__" not in p.parts
    ]
    problems += [f"unexpected {rel}" for rel in present if rel not in owned]
    assert not problems, problems


def test_vendored_copy_is_the_version_the_manifest_accepts() -> None:
    stamp = (_dest() / "VERSION").read_text(encoding="utf-8").split()
    assert stamp[0] in {"ThemeForge", "ForgeUI"}, stamp
    want = _manifest()["version"]
    assert stamp[1] == want or stamp[1].startswith(want + "."), (stamp, want)
    assert re.fullmatch(r"\d+\.\d+\.\d+", stamp[1]), stamp


def test_overlays_are_app_owned_files_not_appended_into_the_bundle() -> None:
    manifest = _manifest()
    dest = _dest()
    listed = json.loads((dest / "files.json").read_text(encoding="utf-8"))["files"]
    assert manifest["extra_files"], "DisPatch keeps its legacy token aliases in an overlay"
    for target, source in manifest["extra_files"].items():
        assert target not in listed, f"{target} is a bundle file; overlays need their own name"
        assert (dest / target).read_bytes().replace(b"\r\n", b"\n") == (
            ROOT / source
        ).read_bytes().replace(b"\r\n", b"\n"), f"{target} is stale: run `python -m forgeui vendor`"


def test_settings_name_themes_the_bundle_has() -> None:
    settings = _manifest()["settings"]
    registry = json.loads((_dest() / "themes.json").read_text(encoding="utf-8"))["themes"]
    known = {t["slug"] for t in registry}
    assert set(settings["themes"]) <= known
    assert settings["default"] == "auto" or settings["default"] in settings["themes"]
    # The accessibility themes (ForgeUI 1.2 onward) are offered.
    assert {"slate", "high-contrast", "high-contrast-light"} <= set(settings["themes"])


def test_index_html_picker_settings_come_from_the_manifest() -> None:
    """index.html's ui-theme.js data-* attributes are the runtime copy of settings."""
    html = (ROOT / "frontend" / "static" / "index.html").read_text(encoding="utf-8")
    html = re.sub(r"<!--.*?-->", "", html, flags=re.S)
    tag = re.search(r'<script\b[^>]*\bsrc="/static/ui-theme/ui-theme\.js"[^>]*>', html)
    assert tag, "index.html has no <script src=/static/ui-theme/ui-theme.js>"

    def attr(name: str) -> str:
        m = re.search(rf'\b{name}="([^"]*)"', tag.group(0))
        assert m, f"ui-theme.js tag lacks {name}"
        return m.group(1)

    settings = _manifest()["settings"]
    assert attr("data-themes").split(",") == settings["themes"]
    assert attr("data-default") == settings["default"]
    assert attr("data-storage-key") == settings["storage_key"]


def test_forgeui_vendor_check_passes() -> None:
    try:
        from forgeui import vendor
    except ImportError:
        if os.environ.get("FORGEUI_REQUIRED"):
            pytest.fail("FORGEUI_REQUIRED is set but the forgeui package is not installed")
        pytest.skip(
            "forgeui is not installed: the hash/overlay/settings checks above still ran; "
            "pip install it (docs/development.md, 'Themes') to run `forgeui vendor --check` here"
        )
    lines: list[str] = []
    code = vendor.run(MANIFEST, check=True, out=lines.append)
    assert code == 0, "\n".join(lines)
