"""Release wiring: what a deploy carries, and what the app survives without.

2.1.0 review, finding C1: the deploy allowlist globbed backend/app/*.py
non-recursively, so a new subpackage would not have been deployed — and
main.py imported it at module load, which would have taken the whole app
down. These tests pin that every runtime file is on the allowlist. (Since
2.2.0 voice is a separate add-on; tests/test_no_voice_install.py proves the
app without it.)
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _allow():
    spec = importlib.util.spec_from_file_location(
        "sync_from_live", REPO / "scripts" / "sync_from_live.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.ALLOW


def _covered(rel: str, allow) -> bool:
    p = Path(rel)
    for _src, dst, pattern in allow:
        try:
            sub = p.relative_to(dst)
        except ValueError:
            continue
        if sub.match(pattern) and (len(sub.parts) == 1 or "/" in pattern):
            return True
    return False


def test_every_backend_module_is_on_the_allowlist():
    allow = _allow()
    app_dir = REPO / "backend" / "app"
    missing = [f.relative_to(REPO).as_posix() for f in app_dir.rglob("*.py")
               if "__pycache__" not in f.parts
               and not _covered(f.relative_to(REPO).as_posix(), allow)]
    assert not missing, f"runtime modules a deploy would not carry: {missing}"


def test_voice_rows_left_the_allowlist_with_voice():
    """2.2.0: voice chat is the DisPatch Voice add-on. Its 2.1 files must not be
    on the allowlist — a 2.1 ghost tree left in an install (SPEC §11 step 6b)
    would otherwise sync straight back into the repo — while the shell's new
    extension modules travel like every other js/ module."""
    allow = _allow()
    for rel in ("frontend/static/drive.js", "frontend/static/voice-worklet.js",
                "frontend/static/voice.css", "backend/app/voice/routes.py"):
        assert not _covered(rel, allow), rel
    for name in ("extensions.js", "ext-sdk.js", "voice-absent.js"):
        assert (REPO / "frontend" / "static" / "js" / name).is_file(), name
        assert _covered(f"frontend/static/js/{name}", allow), name
    assert _covered("backend/app/extensions.py", allow)


def test_release_notes_cover_drive_mode():
    """M8: the 2.1.0 notes name what shipped then, including the optional voice stack."""
    changelog = (REPO / "CHANGELOG.md").read_text()
    section = changelog.split("## [2.1.0]", 1)[1].split("\n## [", 1)[0]
    for needle in ("Drive mode", "/ws/voice", "/api/voices", "--group voice",
                   "GPL-3.0", "DISPATCH_VOICE", "docs/voice-drive-mode.md"):
        assert needle in section, needle


def test_release_notes_cover_the_voice_split():
    """2.2.0: the notes and the README say where voice went and how to get it
    back, and keep the operator-facing traps (the empty group, the unit flags)."""
    changelog = (REPO / "CHANGELOG.md").read_text()
    section = changelog.split("## [2.2.0]", 1)[1].split("\n## [", 1)[0]
    for needle in ("DisPatch Voice", "docs/extensions.md", "/api/extensions", "/ext/",
                   "DISPATCH_EXTENSIONS", "--group voice", "--offline", "--ws-max-size 262144",
                   "Voice chat", "dormant"):
        assert needle in section, needle
    readme = (REPO / "README.md").read_text()
    whats_new = readme.split("## What's new in 2.2", 1)[1].split("\n## ", 1)[0]
    assert "DisPatch Voice" in whats_new and "docs/extensions.md" in whats_new
    assert "Drive mode" not in whats_new


def test_every_runtime_data_file_is_on_the_allowlist():
    """2.3.6: app/personas.py reads app/personas/*.md at runtime (like the
    first-run guide). A preset the deploy did not carry would simply vanish
    from the Bots tab, so every such file is pinned here."""
    allow = _allow()
    app_dir = REPO / "backend" / "app"
    files = [*app_dir.glob("personas/*.md"), *app_dir.glob("guide/*.md")]
    assert any(f.parent.name == "personas" for f in files)
    missing = [f.relative_to(REPO).as_posix() for f in files
               if not _covered(f.relative_to(REPO).as_posix(), allow)]
    assert not missing, f"runtime data a deploy would not carry: {missing}"
