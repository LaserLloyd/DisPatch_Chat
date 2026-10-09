"""Release wiring: what a deploy carries, and what the app survives without.

2.1.0 review, finding C1: the deploy allowlist globbed backend/app/*.py
non-recursively, so the new app/voice/ subpackage would not have been
deployed — and main.py imported it at module load, which would have taken the
whole app down. These tests pin both halves: every runtime file is on the
allowlist, and a missing voice package costs Drive mode, not the app.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import textwrap
from pathlib import Path

from app import main

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


def test_drive_mode_static_files_are_on_the_allowlist():
    """The static-root files Drive mode loads (main.js imports drive.js, which
    loads the worklet and the stylesheet) must travel with a deploy."""
    allow = _allow()
    for name in ("drive.js", "voice-worklet.js", "voice.css"):
        assert (REPO / "frontend" / "static" / name).is_file()
        assert _covered(f"frontend/static/{name}", allow), name


def test_app_imports_and_serves_without_the_voice_package(tmp_path):
    """Drive mode is optional: with app.voice unimportable the app still
    loads, the voice socket answers 1013 and /api/voice simply is not there.
    (Import only — no lifespan, so nothing starts and nothing is written.)"""
    code = textwrap.dedent("""
        import sys
        sys.modules["app.voice.routes"] = None      # import of it now fails
        from app import main
        assert main._voice_serve is None and main._voice_mount_http is None
        paths = {getattr(r, "path", "") for r in main.app.routes}
        assert "/ws/voice/{thread_id}" in paths          # answers 1013
        assert "/api/voice/status" not in paths
        assert "/api/health" in paths and "/ws" in paths  # the app itself is whole
        print("OK")
    """)
    env = {**os.environ, "DISPATCH_DATA_DIR": str(tmp_path), "DISPATCH_VOICE": "0",
           "DISPATCH_GATEWAY_WS": "0", "DISPATCH_MIRROR": "0"}
    r = subprocess.run([sys.executable, "-c", code], cwd=REPO / "backend", env=env,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0 and "OK" in r.stdout, r.stderr[-3000:]


def test_voice_clip_uploads_have_a_ceiling():
    """M5: the Voices panel's uploads are refused by declared size before the
    body is spooled."""
    for path in ("/api/voices", "/api/voices/analyse"):
        assert main._upload_ceiling(path) == main.VOICE_CLIP_MAX + main._MULTIPART_SLACK
    assert main.VOICE_CLIP_MAX == 20 * 1024 * 1024


def test_release_notes_cover_drive_mode():
    """M8: the 2.1.0 notes name what ships, including the optional voice stack."""
    changelog = (REPO / "CHANGELOG.md").read_text()
    section = changelog.split("## [2.1.0]", 1)[1].split("\n## [", 1)[0]
    for needle in ("Drive mode", "/ws/voice", "/api/voices", "--group voice",
                   "GPL-3.0", "DISPATCH_VOICE", "docs/voice-drive-mode.md"):
        assert needle in section, needle
    readme = (REPO / "README.md").read_text()
    whats_new = readme.split("## What's new in 2.1", 1)[1].split("\n## ", 1)[0]
    assert "Drive mode" in whats_new and "docs/voice-drive-mode.md" in whats_new
