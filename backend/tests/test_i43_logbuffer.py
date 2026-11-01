"""4.3 WP-A1 (#18): the in-memory log ring, the dashboard's fallback to it, and the setup
banner's published address."""
from __future__ import annotations

import asyncio
import logging

import pytest

from app import dashboard, logbuffer, main


@pytest.fixture(autouse=True)
def _shipped_fresh_install_default(monkeypatch):
    """conftest models upgraded installs; these tests check the shipped default (False)."""
    from app import auth as _auth
    monkeypatch.setattr(_auth, "FRESH_INSTALL_LOOPBACK_TRUST", False)


@pytest.fixture
def ring():
    h = logbuffer.install()
    yield h
    logging.getLogger().removeHandler(h)


def test_i_tail_holds_a_line_logged_after_install(ring):
    assert logbuffer.install() is ring                  # idempotent: one handler
    assert sum(1 for x in logging.getLogger().handlers if x is ring) == 1
    logging.getLogger("dispatch.test").warning("ring-probe-%s", "4711")
    assert "ring-probe-4711" in logbuffer.tail()
    assert logbuffer.tail(1).endswith("ring-probe-4711")


def test_the_ring_is_bounded():
    h = logbuffer.RingHandler(maxlen=5)
    log = logging.getLogger("dispatch.test.bounded")
    log.addHandler(h)
    log.propagate = False
    try:
        for i in range(12):
            log.warning("line %d", i)
    finally:
        log.removeHandler(h)
        log.propagate = True
    assert len(h.lines) == 5 and h.lines[-1].endswith("line 11")
    assert logbuffer.MAX_LINES == 2000


def test_the_dashboard_reads_the_ring_redacted(ring, world, monkeypatch):
    monkeypatch.delenv("DISPATCH_LOG_FILE", raising=False)
    monkeypatch.delenv("LOCAL_CHAT_LOG_FILE", raising=False)
    planted = "sk-ant-api03-" + "A" * 40
    logging.getLogger("dispatch.test").warning("leaky line api_key=%s", planted)
    body = asyncio.run(dashboard.tail_log(50))
    assert body["available"] is True and body["source"] == "memory"
    text = "\n".join(body["lines"])
    assert "leaky line" in text and planted not in text


def test_the_dashboard_still_prefers_the_file(world, monkeypatch, tmp_path, ring):
    f = tmp_path / "dispatch.log"
    f.write_text("from the file\n")
    monkeypatch.setenv("DISPATCH_LOG_FILE", str(f))
    body = asyncio.run(dashboard.tail_log(10))
    assert body["lines"] == ["from the file"] and body.get("source") != "memory"


def test_lifespan_installs_the_ring(world):
    world.client()
    assert logbuffer.installed()


def test_i_banner_shows_the_published_port(world, monkeypatch, caplog):
    monkeypatch.delenv("DISPATCH_ALLOW_OPEN_INSTALL", raising=False)
    monkeypatch.setenv("DISPATCH_PUBLIC_PORT", "9999")
    monkeypatch.delenv("DISPATCH_PUBLIC_URL", raising=False)
    with caplog.at_level(logging.INFO):
        main._warn_if_wide_open()
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "http://localhost:9999" in text
    caplog.clear()
    monkeypatch.setenv("DISPATCH_PUBLIC_URL", "https://chat.example.net/")
    with caplog.at_level(logging.INFO):
        main._warn_if_wide_open()
    assert "https://chat.example.net and create it" in "\n".join(r.getMessage() for r in caplog.records)


def test_startup_logs_the_posture(world, monkeypatch, caplog):
    from app import auth
    auth.set_pin("2468")
    with caplog.at_level(logging.INFO):
        main._warn_if_wide_open()
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "security posture: claimed=True machine_loopback_trust=False" in text
    assert "any process on this computer" not in text
    cfg = auth.load()
    cfg.machine_loopback_trust = True
    auth._write(cfg)
    auth._bust_cache()
    caplog.clear()
    with caplog.at_level(logging.INFO):
        main._warn_if_wide_open()
    assert "any process on this computer is a machine caller without a key" in \
        "\n".join(r.getMessage() for r in caplog.records)
