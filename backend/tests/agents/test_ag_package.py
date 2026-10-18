"""The package stays self-contained: importable without app.main or any SDK, and the SDK
stays an exactly-pinned OPTIONAL extra (RESEARCH.md section 1 item 6, section 5 item 6)."""
from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]


def test_the_package_imports_without_app_main_or_an_sdk_or_a_websocket_client():
    code = (
        "import sys\n"
        "import app.agents\n"
        "from app.agents import (branding, budget, claude_sdk_adapter, cli_adapter, events, fake,\n"
        "                        interface, openclaw_adapter, secrets, workspace)\n"
        "bad = [m for m in ('app.main', 'claude_agent_sdk', 'keyring', 'websockets', 'app.config',\n"
        "                   'app.openclaw', 'app.gateway_ws') if m in sys.modules]\n"
        "assert not bad, bad\n"
        "print('ok')\n"
    )
    cp = subprocess.run([sys.executable, "-P", "-c", code], cwd=BACKEND, capture_output=True, text=True,
                        env={**__import__("os").environ, "PYTHONPATH": str(BACKEND)}, timeout=60)
    assert cp.returncode == 0 and cp.stdout.strip() == "ok", cp.stderr


def test_the_claude_sdk_is_an_exactly_pinned_optional_extra_not_a_default_dependency():
    meta = tomllib.loads((BACKEND / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert meta["optional-dependencies"]["agents-claude"] == ["claude-agent-sdk==0.2.168"]
    assert not any("claude-agent-sdk" in d for d in meta["dependencies"])


def test_the_adapter_pin_constant_matches_the_extra():
    from app.agents.claude_sdk_adapter import SDK_PIN
    assert SDK_PIN == "0.2.168"
