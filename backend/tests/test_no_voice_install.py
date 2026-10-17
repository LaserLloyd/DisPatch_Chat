"""DisPatch without the voice add-on (requirement 7: DisPatch installs and runs
fully without it). Voice chat left this repository in 2.2.0 and is the
separate DisPatch Voice package; these tests prove the app imports, boots and
serves with nothing voice-shaped installed, that a stale voice setting in
config.yaml or tools.yaml costs nothing, and that the pre-2.2 unit line
(`uv run --group voice`) still boots (hardening H1).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import textwrap
import tomllib
from pathlib import Path

import pytest
from _extfake import ext_env, lock, thread_for, unlock
from starlette.websockets import WebSocketDisconnect

from app import extensions, main

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent


def test_no_speech_code_ships_in_dispatch():
    assert not (BACKEND / "app" / "voice").exists()
    for rel in ("frontend/static/drive.js", "frontend/static/voice-worklet.js",
                "frontend/static/voice.css", "frontend/static/js/drive-settings.js",
                "frontend/static/js/voices.js", "frontend/static/js/voice-tools.js"):
        assert not (REPO / rel).exists(), rel
    for f in (BACKEND / "app").rglob("*.py"):
        text = f.read_text(encoding="utf-8")
        assert "from .voice" not in text and "app.voice" not in text, f


def test_pyproject_keeps_an_empty_voice_group():
    doc = tomllib.loads((BACKEND / "pyproject.toml").read_text())
    groups = doc["dependency-groups"]
    assert groups["voice"] == []
    deps = " ".join(doc["project"]["dependencies"] + groups["dev"]).lower()
    for speech in ("onnx-asr", "soundfile", "tokenizers", "pyloudnorm", "huggingface"):
        assert speech not in deps, speech


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is not installed")
def test_uv_run_group_voice_still_boots(tmp_path):
    """The 2.1 unit drop-in runs `uv run --quiet --group voice uvicorn …`. With
    the group gone uv exits 2 before Python starts and the restart loop takes
    the family app down; with the empty group it starts."""
    for name in ("pyproject.toml", "uv.lock"):
        shutil.copy(BACKEND / name, tmp_path / name)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("VIRTUAL_ENV", "UV_PROJECT"))}
    r = subprocess.run(["uv", "run", "--offline", "--quiet", "--group", "voice", "python", "-c",
                        "print('started')"], cwd=tmp_path, env=env, capture_output=True,
                       text=True, timeout=300)
    if r.returncode != 0 and ("offline" in r.stderr.lower() or "network" in r.stderr.lower()):
        pytest.skip("uv's cache cannot build the env offline here: " + r.stderr[-300:])
    assert r.returncode == 0 and "started" in r.stdout, r.stderr[-2000:]
    # The control: without the declaration the same line refuses to start.
    py = (tmp_path / "pyproject.toml").read_text().replace("\nvoice = []\n", "\n")
    (tmp_path / "pyproject.toml").write_text(py)
    r = subprocess.run(["uv", "run", "--offline", "--quiet", "--group", "voice", "python", "-c",
                        "print('started')"], cwd=tmp_path, env=env, capture_output=True,
                       text=True, timeout=300)
    if r.returncode != 0 and "voice" not in r.stderr and "cache" in r.stderr.lower():
        # Re-locking the edited pyproject needs packages this machine's uv cache
        # lacks (a clean CI runner): the control cannot run, and the assertion
        # above — the one that guards the unit line — has already passed.
        pytest.skip("control needs a warm uv cache to re-lock offline: " + r.stderr[-200:])
    assert r.returncode != 0 and "voice" in r.stderr


def test_app_imports_and_routes_without_any_extension(tmp_path):
    """Import only (no lifespan): nothing voice-shaped is mounted, the generic
    host routes are, and the shell is whole.

    The child first makes the add-on ABSENT even when this environment has it
    (DisPatch's suite also runs under `uv run --with-editable DisPatch_Voice`,
    the live box's line): no `dispatch.extensions` entry points, and
    `import dispatch_voice` fails as it does on a box that never installed it.
    Where nothing is installed both shims are no-ops."""
    code = textwrap.dedent("""
        import importlib.metadata as _md
        import sys
        _real_eps = _md.entry_points
        def _eps(**kw):
            if kw.get("group") == "dispatch.extensions":
                return _md.EntryPoints(())
            return _real_eps(**kw)
        _md.entry_points = _eps
        class _NotInstalled:
            def find_spec(self, name, path=None, target=None):
                if name == "dispatch_voice" or name.startswith("dispatch_voice."):
                    raise ModuleNotFoundError(f"No module named {name!r}", name=name)
                return None
        sys.meta_path.insert(0, _NotInstalled())
        from app import main, extensions
        paths = {getattr(r, "path", "") for r in main.app.routes}
        assert "/api/voice/status" not in paths and "/api/voices" not in paths
        assert "/ws/voice/{thread_id}" not in paths
        assert {"/ws/{name}/{thread_id}", "/api/extensions", "/ext/{ext_id}/{rel:path}"} <= paths
        assert "/api/health" in paths and "/ws" in paths
        assert [e.id for e in extensions.registry() if e.state == "ok"] == []
        assert not any(m == "dispatch_voice" or m.startswith("dispatch_voice.") for m in sys.modules)
        print("OK")
    """)
    env = {**os.environ, "DISPATCH_DATA_DIR": str(tmp_path), "DISPATCH_GATEWAY_WS": "0",
           "DISPATCH_MIRROR": "0"}
    env.pop("DISPATCH_EXTENSIONS", None)
    env.pop("DISPATCH_VOICE_PKG", None)
    r = subprocess.run([sys.executable, "-c", code], cwd=BACKEND, env=env,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0 and "OK" in r.stdout, r.stderr[-3000:]


def test_the_running_app_serves_with_voice_absent(ext_env):
    c, _ = ext_env
    assert c.get("/api/health").status_code == 200
    body = c.get("/api/extensions").json()
    assert body["extensions"] == [] and body["api"] == extensions.EXT_API
    assert c.get("/api/auth/status").json()["features"]["extensions"] == 0
    assert c.get("/api/voice/status").status_code == 404
    assert c.get("/api/voices").status_code == 404
    assert c.get("/ext/voice/index.js").status_code == 404
    tid = thread_for(c)
    with pytest.raises(WebSocketDisconnect) as e:
        with c.websocket_connect(f"/ws/voice/{tid}", headers={"origin": "http://testserver"}) as ws:
            ws.receive_text()
    assert e.value.code == 1013 and e.value.reason == "No extension provides voice"
    # Safe Mode (2.3.4) reads the same empty list; a locked device on a
    # non-safe bot's thread is refused before any of that.
    lock(c)
    browser = {"sec-fetch-mode": "cors", "origin": "http://testserver"}
    assert c.get("/api/extensions", headers=browser).json()["extensions"] == []
    assert c.get("/api/extensions").json()["extensions"] == []   # keyless remote = locked view
    assert c.get("/ext/voice/index.js").status_code == 404
    with pytest.raises(WebSocketDisconnect) as e:
        with c.websocket_connect(f"/ws/voice/{tid}", headers={"origin": "http://testserver"}) as ws:
            ws.receive_text()
    assert e.value.code == 1008
    unlock(c)


def test_a_stale_voice_tools_row_does_not_cost_the_tools_rail(ext_env):
    c, _ = ext_env
    from app import config, tools
    (config.DATA_DIR / "tools.yaml").write_text(
        "tools:\n- {id: voice-cloning, kind: builtin, enabled: true}\n"
        "- {id: mail-panel, kind: builtin, enabled: false}\n")
    tools._reset_state()
    rows = {t["id"]: t for t in c.get("/api/tools").json()["tools"]}
    assert "voice-cloning" not in rows
    assert rows["mail-panel"]["enabled"] is False          # the manifest still applies


def test_the_shipped_bots_need_nothing_from_voice(ext_env):
    c, _ = ext_env
    bots = c.get("/api/bots/all").json()["bots"]
    assert all(b["voice"] == "" and b["voice_rp"] == "" for b in bots)
    items = [{"id": b["id"], "order": i, "voice": ""} for i, b in enumerate(bots)]
    assert c.put("/api/bots/order", json={"bots": items}).status_code == 200
    assert main.app.version == "2.4.0"
