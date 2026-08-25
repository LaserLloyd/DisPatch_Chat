"""Why generation is unavailable — and why that must never read as "rig down".

Regression cover for the 2026-08-24 outage: a deploy replaced a hardcoded image
CLI default with an env-only ``DISPATCH_IMAGE_CLI`` and nothing on the host set
it. Both pools then generated 0 forever **in total silence**, while the only
operator-facing message said "ClawForge is unreachable" — pointing every
investigation at a rig that was perfectly healthy.

So these tests pin three separate promises:
  * the *reason* generation is impossible is reported, not just the fact;
  * a pool that cannot generate says so LOUDLY, once, in the log;
  * a rig somebody else has leased is never minted onto (or evicted from).

Run: cd backend && uv run pytest tests/test_image_cli_diagnosis.py
"""
from __future__ import annotations

import json
import logging
import urllib.request

import pytest

from app import avatar_pool, config, pool_guard, reactions

# --------------------------------------------------------------------------- #
# image_cli_state — unset vs missing vs ok
# --------------------------------------------------------------------------- #


def test_state_unset_when_nothing_configured(monkeypatch):
    monkeypatch.setattr(reactions, "_IMAGE_CLI", "")
    assert reactions.image_cli_state() == reactions.IMAGE_CLI_UNSET
    assert reactions.image_cli_available() is False


def test_state_missing_when_configured_but_absent(monkeypatch, tmp_path):
    """The distinction that did not exist before: configured-but-broken is a
    DIFFERENT problem from never-configured, and needs a different fix."""
    monkeypatch.setattr(reactions, "_IMAGE_CLI", str(tmp_path / "nope"))
    assert reactions.image_cli_state() == reactions.IMAGE_CLI_MISSING
    assert reactions.image_cli_available() is False


def test_state_ok_for_a_real_executable(monkeypatch, tmp_path):
    exe = tmp_path / "fake-cli"
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(0o755)
    monkeypatch.setattr(reactions, "_IMAGE_CLI", str(exe))
    assert reactions.image_cli_state() == reactions.IMAGE_CLI_OK
    assert reactions.image_cli_available() is True


def test_error_strings_distinguish_the_two_faults(monkeypatch, tmp_path):
    monkeypatch.setattr(reactions, "_IMAGE_CLI", "")
    unset = reactions.image_cli_error()
    monkeypatch.setattr(reactions, "_IMAGE_CLI", str(tmp_path / "nope"))
    missing = reactions.image_cli_error()
    assert unset == "image-cli-unset"
    assert missing.startswith("image-cli-missing")
    assert unset != missing
    # Both must still be recognised as image-CLI faults, so a later generic
    # "generation-failed" cannot overwrite the specific cause.
    assert reactions._is_image_cli_error(unset)
    assert reactions._is_image_cli_error(missing)
    assert not reactions._is_image_cli_error("rig-vram-short (2.0 GB)")


# --------------------------------------------------------------------------- #
# The pools report the reason, and say it out loud
# --------------------------------------------------------------------------- #


@pytest.fixture
def rx_dry(tmp_path, monkeypatch):
    """A reaction pool on a throwaway data dir, with NO image CLI configured."""
    for name, sub in [("DATA_DIR", ""), ("CONFIG_PATH", "config.yaml"),
                      ("MEDIA_DIR", "media"), ("FILES_DIR", "files"),
                      ("LOG_DIR", "logs"), ("BACKUP_DIR", "backups")]:
        monkeypatch.setattr(config, name, tmp_path / sub if sub else tmp_path)
    config.ensure_dirs()
    reactions.invalidate()
    reactions._pool_cache.clear()
    reactions._bank_cache.clear()
    reactions.seed_starter_pack()
    monkeypatch.setattr(reactions, "_IMAGE_CLI", "")
    yield tmp_path


def test_reaction_refill_records_the_specific_reason(rx_dry, caplog):
    """Before the fix this stored the catch-all "image-cli-unavailable", which
    is exactly the ambiguity that sent people to inspect the image host."""
    with caplog.at_level(logging.ERROR, logger="local-chat.reactions"):
        assert reactions.pool_refill(bot_id="main") == 0
    st = reactions.pool_load("main")
    assert st.last_error == "image-cli-unset"
    assert "DISPATCH_IMAGE_CLI" in caplog.text
    # The message must actively deny the wrong conclusion.
    assert "NOT an image-host outage" in caplog.text


def test_reaction_refill_does_not_spam_the_log(rx_dry, caplog):
    """The pool loop runs every 300s; the alert logs on the TRANSITION only."""
    with caplog.at_level(logging.ERROR, logger="local-chat.reactions"):
        reactions.pool_refill(bot_id="main")
        assert "DISPATCH_IMAGE_CLI" in caplog.text     # the transition alerts
        caplog.clear()                                  # caplog keeps records
        reactions.pool_refill(bot_id="main")
    assert "DISPATCH_IMAGE_CLI" not in caplog.text     # the steady state is quiet


def test_reaction_status_exposes_image_cli_state(rx_dry):
    """What a watchdog reads to name the real cause instead of guessing."""
    status = reactions.pool_status("main")
    assert status["available"] is False
    assert status["image_cli"] == reactions.IMAGE_CLI_UNSET


@pytest.fixture
def avatar_dry(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(config, "AVATAR_DIR", tmp_path / "avatars")
    config.ensure_dirs()
    avatar_pool._state_cache.clear()
    avatar_pool._bank_cache.clear()
    monkeypatch.setattr(reactions, "_IMAGE_CLI", "")
    yield tmp_path


def test_avatar_refill_records_the_specific_reason(avatar_dry, caplog):
    with caplog.at_level(logging.ERROR, logger="local-chat.reactions"):
        assert avatar_pool.refill(bot_id="main") == 0
    st = avatar_pool.load_state("main")
    assert st.last_error == "image-cli-unset"
    assert "DISPATCH_IMAGE_CLI" in caplog.text


def test_avatar_status_exposes_image_cli_state(avatar_dry):
    status = avatar_pool.status("main")
    assert status["available"] is False
    assert status["image_cli"] == reactions.IMAGE_CLI_UNSET


# --------------------------------------------------------------------------- #
# GPU leases — a booked rig is off-limits
# --------------------------------------------------------------------------- #


class _FakeResponse:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode()

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_leases(monkeypatch, payload):
    monkeypatch.setattr(pool_guard, "lease_url", lambda: "http://rig.invalid/api/leases")
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda url, timeout=None: _FakeResponse(payload))


def test_lease_holder_is_reported(monkeypatch):
    _fake_leases(monkeypatch, {"leases": [{"holder": "gauntlet"}], "count": 1})
    assert pool_guard.rig_lease_holder() == "gauntlet"


def test_no_lease_reads_as_unleased(monkeypatch):
    _fake_leases(monkeypatch, {"leases": [], "count": 0})
    assert pool_guard.rig_lease_holder() is None


def test_lease_view_failure_fails_open(monkeypatch):
    """An unreadable lease endpoint must not become an outage for the pools."""
    monkeypatch.setattr(pool_guard, "lease_url", lambda: "http://rig.invalid/api/leases")

    def _boom(url, timeout=None):
        raise OSError("unreachable")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    assert pool_guard.rig_lease_holder() is None


def test_unset_lease_url_keeps_old_behaviour(monkeypatch):
    monkeypatch.setattr(pool_guard, "lease_url", lambda: None)
    assert pool_guard.rig_lease_holder() is None


def test_leased_rig_blocks_the_mint_without_touching_a_gpu(monkeypatch):
    """The load-bearing one: DISPATCH_POOL_FREE_VRAM=1 lets the guard UNLOAD
    models, so a top-up during a benchmark lease could evict the very model the
    lease exists to protect. The lease is checked BEFORE the headroom probe --
    a leased rig is off-limits however much VRAM happens to be free."""
    monkeypatch.setattr(pool_guard, "rig_lease_holder", lambda: "gauntlet")
    probed: list[str] = []
    monkeypatch.setattr(pool_guard, "mint_gpu_free_gb",
                        lambda: probed.append("probe") or (999.0, "gpu0"))
    unloaded: list[str] = []
    monkeypatch.setattr(pool_guard, "_unload_model",
                        lambda mid: unloaded.append(mid) or True)

    out = pool_guard.free_vram_before_mint()

    assert out["ok"] is False
    assert "leased" in out["reason"] and "gauntlet" in out["reason"]
    assert probed == [], "a leased rig must not even be probed"
    assert unloaded == [], "a leased rig must never be evicted"


# --------------------------------------------------------------------------- #
# Where the avatar roster lives — the rule external tools must mirror
# --------------------------------------------------------------------------- #


def test_data_dir_wins_when_both_exist(tmp_path):
    """The migrated steady state. `dispatch-avatar-rotate` hardcoded the legacy
    path and so wrote nowhere once the roster moved — rotation stopped dead."""
    legacy = tmp_path / "code" / "avatars"
    data = tmp_path / "data" / "avatars"
    legacy.mkdir(parents=True)
    data.mkdir(parents=True)
    assert config.resolve_avatar_dir(legacy, data) == data


def test_legacy_wins_only_while_the_data_dir_is_absent(tmp_path):
    """An install that has not migrated keeps its faces, with no migration step."""
    legacy = tmp_path / "code" / "avatars"
    data = tmp_path / "data" / "avatars"
    legacy.mkdir(parents=True)
    (legacy / "main-face.png").write_bytes(b"x")
    assert config.resolve_avatar_dir(legacy, data) == legacy


def test_data_dir_is_the_default_on_a_fresh_install(tmp_path):
    """The legacy dir EXISTS in every checkout (.gitkeep), so existence alone
    must never win -- that would send a fresh install into the code tree."""
    legacy = tmp_path / "code" / "avatars"
    data = tmp_path / "data" / "avatars"
    legacy.mkdir(parents=True)
    (legacy / ".gitkeep").write_bytes(b"")
    assert config.resolve_avatar_dir(legacy, data) == data
    assert config.legacy_avatars_in_use(legacy) is False
