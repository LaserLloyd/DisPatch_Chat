"""Fixtures for the agent-dispatch suite.

This suite is HERMETIC and does not import ``app.main`` - it must run on Windows,
where ``import fcntl`` in main.py stops the rest of the backend from importing
(until W4 lands). The parent ``tests/conftest.py`` has autouse fixtures that
import ``app.main`` and render avatars; they are overridden below with no-ops,
which is allowed because a fixture of the same name in a nearer conftest wins.
Nothing here reads or writes the real data dir: every path comes from tmp_path,
and the package takes ``data_dir`` explicitly.
"""
from __future__ import annotations

import os

import pytest

# --- neutralise the parent conftest's app.main-dependent autouse fixtures ------- #


@pytest.fixture(scope="session")
def _starter_cards():
    return []


@pytest.fixture(autouse=True)
def _test_roster():
    yield


@pytest.fixture(autouse=True)
def _reuse_starter_cards():
    yield


@pytest.fixture(autouse=True)
def _cheap_kdf():
    yield


@pytest.fixture(autouse=True)
def _no_writes_outside_tmp():
    yield


@pytest.fixture(autouse=True)
def _pin_the_autopilot_clock():
    yield


@pytest.fixture(autouse=True)
def _no_hard_exit_on_failed_startup():
    yield


@pytest.fixture(autouse=True)
def _no_real_systemctl():
    yield


# --- this suite's own guards ---------------------------------------------------- #

_LIVE_PORTS = ("1234", "1235", "1236", "8080", "8700", "8288", "7865", "18789")


@pytest.fixture(autouse=True)
def _agents_hermetic(monkeypatch):
    """No ambient credentials and no real gateway/live-port target, ever."""
    for var in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
                "OPENCLAW_GATEWAY_TOKEN", "OPENCLAW_GATEWAY_URL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AGENT_LIVE_TESTS", "off")
    # Anything that resolves the default OpenClaw gateway URL must not find the real one.
    monkeypatch.setenv("OPENCLAW_GATEWAY_URL", "ws://127.0.0.1:9/never-used")
    assert os.environ["AGENT_LIVE_TESTS"] == "off"
    yield


@pytest.fixture
def agent_data(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    return d
