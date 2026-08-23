"""Regression tests for config.py's bot-registry writers.

save_bot_avatar() must be a surgical read-modify-write of ONE bot's avatar
field. Until 2026-08-01 its payload omitted `reactions`, so ANY avatar upload
silently reset every bot's reactions capability to the compiled default —
discarding an admin's Bot-Manager choice, and able to re-enable a capability
that had been deliberately switched off.

Hermetic style mirrors the other suites: config paths monkeypatched to a tmp
dir, nothing touches the live data dir.
Run: cd backend && uv run pytest.
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from app import config


@pytest.fixture
def cfg_env(tmp_path, monkeypatch):
    """Isolated config.yaml + data dirs (no app instance needed)."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(config, "MEDIA_DIR", tmp_path / "media")
    monkeypatch.setattr(config, "FILES_DIR", tmp_path / "files")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path / "backups")
    config._invalidate_bots_cache()
    yield
    config._invalidate_bots_cache()


def test_save_bot_avatar_preserves_another_bots_reactions_flag(cfg_env):
    # Flip a NON-default flag through the Bot Manager path (alpha ships off)…
    config.save_bot_order([{"id": "alpha", "order": 0, "reactions": True}])
    assert config.get_bot("alpha").reactions is True
    # …then replace an UNRELATED bot's avatar.
    updated = config.save_bot_avatar("main", "main-face.png")
    assert updated is not None and updated.avatar == "main-face.png"
    assert config.get_bot("alpha").reactions is True, (
        "avatar upload reset another bot's reactions capability")


def test_save_bot_avatar_preserves_a_deliberate_reactions_off(cfg_env):
    # Nova ("main") ships reactions-enabled; turn it OFF deliberately…
    config.save_bot_order([{"id": "main", "order": 0, "reactions": False}])
    assert config.get_bot("main").reactions is False
    # …an avatar upload for that same bot must not silently re-arm it.
    config.save_bot_avatar("main", "main-face.png")
    assert config.get_bot("main").reactions is False, (
        "avatar upload re-enabled a deliberately disabled capability")


def test_save_bot_avatar_changes_only_the_target_avatar_field(cfg_env):
    """Field-by-field: the ONLY difference after save_bot_avatar is the target
    bot's avatar filename — every other bot and every other field survives."""
    before = {b.id: b for b in config.load_bots()}
    config.save_bot_avatar("main", "new-face.png")
    after = {b.id: b for b in config.load_bots()}
    assert set(after) == set(before)
    for bid, b in after.items():
        expected = replace(before[bid], avatar="new-face.png") if bid == "main" \
            else before[bid]
        assert b == expected, f"unrelated field changed for {bid!r}"


def test_save_bot_avatar_unknown_bot_is_a_noop(cfg_env):
    before = {b.id: b for b in config.load_bots()}
    assert config.save_bot_avatar("nope", "x.png") is None
    assert {b.id: b for b in config.load_bots()} == before


# --------------------------------------------------------------------------- #
# config.yaml may hold provider API keys — write it like a secret
# --------------------------------------------------------------------------- #


def test_config_is_written_atomically_at_0600(cfg_env):
    """It used to be write_text() then chmod: world-readable for the length of
    the write, and a crash mid-write truncated the roster."""
    import os

    config._write_default_config()
    assert config.CONFIG_PATH.stat().st_mode & 0o777 == 0o600
    assert not [p for p in config.CONFIG_PATH.parent.iterdir()
                if p.name.startswith(".config.yaml.")], "temp file left behind"

    before = config.CONFIG_PATH.read_bytes()
    real_replace = os.replace

    def boom(src, dst):
        raise OSError("disk full")

    try:
        os.replace = boom
        with pytest.raises(OSError):
            config._write_default_config()
    finally:
        os.replace = real_replace

    assert config.CONFIG_PATH.read_bytes() == before, "a failed write ate the roster"
    assert not [p for p in config.CONFIG_PATH.parent.iterdir()
                if p.name.startswith(".config.yaml.")], "temp file left behind"
