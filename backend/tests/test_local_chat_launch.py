"""scripts/local-chat-launch: start DisPatch with the add-on overlay, or WITHOUT it
when the overlay cannot be resolved, imported, or crashes on import — never a
restart loop that keeps the family app down (hardening H2, FR-hardening L4)."""
from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

LAUNCHER = Path(__file__).resolve().parents[2] / "scripts" / "local-chat-launch"


def fake_uv(tmp_path: Path, probe: str) -> Path:
    """A stand-in `uv`: the probe run (`… python -c import …`) exits as told —
    ok, fail, or crash (SIGSEGV) — and records its argv."""
    log = tmp_path / "argv.log"
    action = {"ok": "sys.exit(0)", "fail": "sys.exit(1)",
              "crash": "os.kill(os.getpid(), signal.SIGSEGV)"}[probe]
    uv = tmp_path / "uv"
    uv.write_text(
        f"#!{sys.executable}\n"
        "import os, signal, sys\n"
        f"open({str(log)!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
        "if '-c' in sys.argv:\n"
        f"    {action}\n"
        "sys.exit(0)\n")
    uv.chmod(uv.stat().st_mode | stat.S_IEXEC)
    return uv


def launch(tmp_path: Path, probe: str, *extra: str) -> tuple[subprocess.CompletedProcess, list[str]]:
    uv = fake_uv(tmp_path, probe)
    r = subprocess.run([sys.executable, str(LAUNCHER), "--uv", str(uv), "--with-editable", "/opt/voice-addon",
                        *extra, "--dry-run", "--", "uvicorn", "app.main:app", "--port", "1"],
                       capture_output=True, text=True, timeout=60, env={**os.environ})
    logged = (tmp_path / "argv.log").read_text().splitlines() if (tmp_path / "argv.log").exists() else []
    return r, logged


def test_a_working_overlay_is_used(tmp_path):
    r, logged = launch(tmp_path, "ok")
    assert r.returncode == 0, r.stderr
    assert "--with-editable /opt/voice-addon uvicorn app.main:app" in r.stdout
    # The probe imports the host package and the native libraries, offline.
    assert logged and "--offline" in logged[0]
    assert "import dispatch_voice.host, numpy, onnxruntime" in logged[0]


def test_a_failing_overlay_starts_dispatch_without_it(tmp_path):
    r, _ = launch(tmp_path, "fail")
    assert r.returncode == 0
    assert "--with-editable" not in r.stdout and "uvicorn app.main:app" in r.stdout
    assert "WITHOUT it" in r.stderr


def test_a_probe_that_crashes_natively_is_caught(tmp_path):
    """A broken native wheel kills the probe process, never DisPatch."""
    r, _ = launch(tmp_path, "crash")
    assert r.returncode == 0
    assert "--with-editable" not in r.stdout
    assert "killed by signal" in r.stderr


def test_the_probe_list_is_validated(tmp_path):
    r, _ = launch(tmp_path, "ok", "--probe", "os; import shutil")
    assert r.returncode == 2 and "module names" in r.stderr
