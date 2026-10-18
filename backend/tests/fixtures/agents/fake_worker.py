"""A real worker PROCESS that speaks the adapter's stdio protocol over a FAKE SDK.

Used by tests/agents/test_ag_claude_worker.py to exercise SubprocessTransport (pipes,
process group, graceful close) without the claude-agent-sdk package or any network.
It records what it was given to <cwd>/worker_report.json at exit.
"""
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
BACKEND = HERE.parents[3]
sys.path[:0] = [str(BACKEND), str(BACKEND / "tests" / "agents")]

from agent_fakes import ClaudeSDKClient, fake_sdk_module  # noqa: E402

from app.agents.claude_sdk_adapter import _stdio_main  # noqa: E402

sdk = fake_sdk_module()
code = 1
try:
    code = _stdio_main(sdk)
finally:
    report = {
        "env_keys": sorted(os.environ),
        "home": os.environ.get("HOME"),
        "cwd": os.getcwd(),
        "path0": sys.path[0],
        "clients": [
            {"setting_sources": c.options.setting_sources, "permission_mode": c.options.permission_mode,
             "env_keys": sorted(c.options.env), "queries": c.queries, "disconnected": c.disconnected,
             "has_callback": c.options.can_use_tool is not None, "has_hooks": c.options.hooks is not None}
            for c in ClaudeSDKClient.instances
        ],
    }
    (Path.cwd() / "worker_report.json").write_text(json.dumps(report), encoding="utf-8")
sys.exit(code)
