"""pool_guard tests — the VRAM guard, refusal tally and backoff.

Hermetic: GPU-CLI/image-CLI calls are monkeypatched; no real host is touched,
no live data dirs. The two integration tests prove the refill entrypoints
(pool_refill / avatar refill) actually route through the guard and stop
before spending a single rig call when the mint GPU is short.
Run: cd backend && uv run pytest tests/test_pool_guard.py
"""
from __future__ import annotations

import time

import pytest

from app import avatar_pool, config, pool_guard, reactions
from app.reactions import pool_refill

# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _reset_tally():
    pool_guard._REFILL_FAILURES.clear()
    pool_guard._consecutive = 0
    yield
    pool_guard._REFILL_FAILURES.clear()
    pool_guard._consecutive = 0


def _status(gpu_free_gb: dict[int, float], loaded: list[dict] | None = None):
    return {
        "gpus": [{"index": i, "free_bytes": gb * 1e9}
                 for i, gb in gpu_free_gb.items()],
        "loaded": loaded or [],
    }


def _patched_rig(monkeypatch, gpu_free_gb, loaded=None, selected=None):
    """Point the guard's GPU-CLI/image-CLI seams at a fake host."""
    status = _status(gpu_free_gb, loaded)
    monkeypatch.setattr(pool_guard, "gpu_status", lambda: status)
    if selected is None:
        monkeypatch.setattr(pool_guard, "_image_cli_status", lambda: None)
    else:
        monkeypatch.setattr(pool_guard, "_image_cli_status",
                            lambda: {"comfy": {"gpu_selected": selected}})  # rig-side ComfyUI, via the image CLI
    monkeypatch.setattr(pool_guard, "_model_pinned", lambda mid: False)
    unloaded: list[str] = []
    monkeypatch.setattr(pool_guard, "_unload_model",
                        lambda mid: (unloaded.append(mid), True)[1])
    return unloaded


@pytest.fixture
def rx_env(tmp_path, monkeypatch):
    """Minimal reaction-pool env: throwaway data dir + a seeded starter pack."""
    for name, sub in [("DATA_DIR", ""), ("CONFIG_PATH", "config.yaml"),
                      ("MEDIA_DIR", "media"), ("FILES_DIR", "files"),
                      ("LOG_DIR", "logs"), ("BACKUP_DIR", "backups")]:
        monkeypatch.setattr(config, name, tmp_path / sub if sub else tmp_path)
    config.ensure_dirs()
    reactions.invalidate()
    reactions._pool_cache.clear()
    reactions._bank_cache.clear()
    reactions.seed_starter_pack()
    # The default pool config is enabled; force a due batch + one mood deficit.
    st = reactions.pool_load("main")
    st.config.refresh_hour = 0
    st.batch_date = ""
    reactions.pool_save(st, "main")
    monkeypatch.setattr(reactions, "image_cli_available", lambda: True)
    monkeypatch.setattr(reactions, "bank_categories", lambda *a, **k: ["happy"])
    monkeypatch.setattr(reactions, "pool_deficits",
                        lambda *a, **k: {"happy": 3})
    yield tmp_path


# --------------------------------------------------------------------------- #
# mint_gpu_free_gb — which GPU the next mint lands on
# --------------------------------------------------------------------------- #


def test_mint_target_prefers_image_cli_selected_gpu(monkeypatch):
    _patched_rig(monkeypatch, {0: 3.2, 1: 3.8, 2: 25.3, 3: 25.1}, selected=1)
    free, which = pool_guard.mint_gpu_free_gb()
    assert which == "selected:gpu1"
    assert abs(free - 3.8) < 0.01


def test_mint_target_falls_back_to_best_gpu(monkeypatch):
    # image CLI status unreadable → guard picks the best free GPU, not None
    _patched_rig(monkeypatch, {0: 3.2, 1: 3.8, 2: 25.3, 3: 25.1}, selected=None)
    free, which = pool_guard.mint_gpu_free_gb()
    assert which == "best:gpu2"
    assert abs(free - 25.3) < 0.01


def test_mint_target_fails_open_when_gpu_cli_unreadable(monkeypatch):
    monkeypatch.setattr(pool_guard, "gpu_status", lambda: None)
    assert pool_guard.mint_gpu_free_gb() is None


# --------------------------------------------------------------------------- #
# free_vram_before_mint
# --------------------------------------------------------------------------- #


def test_guard_headroom_ok_unloads_nothing(monkeypatch):
    _patched_rig(monkeypatch, {0: 12.0, 1: 25.0}, selected=1,
                 loaded=[{"model_id": "bench", "active_requests": 0}])
    verdict = pool_guard.free_vram_before_mint()
    assert verdict["ok"] is True
    assert verdict["unloaded"] == []
    assert "headroom-ok" in verdict["reason"]


def test_guard_short_unloads_nompinned_and_skips_pinned_active(monkeypatch):
    loaded = [
        {"model_id": "bench", "active_requests": 0},   # free to unload
        {"model_id": "pinned-one", "active_requests": 0},  # pinned → keep
        {"model_id": "busy-one", "active_requests": 2},    # in use → keep
    ]
    _patched_rig(monkeypatch, {0: 3.2, 1: 3.8}, selected=1, loaded=loaded)
    # bench unloads and frees 8 GB on GPU 1 → headroom OK afterwards
    monkeypatch.setattr(pool_guard, "_model_pinned",
                        lambda mid: mid == "pinned-one")
    real_status = {"gpus": [{"index": 0, "free_bytes": 3.2e9},
                            {"index": 1, "free_bytes": 3.8e9}],
                   "loaded": loaded}
    state = {"loaded": loaded}

    def _status_after_unload():
        if "bench" not in [m["model_id"] for m in state.get("loaded", [])]:
            return {"gpus": [{"index": 0, "free_bytes": 3.2e9},
                             {"index": 1, "free_bytes": 11.8e9}],
                    "loaded": state.get("loaded", [])}
        return real_status

    def _fake_unload(mid):
        state.setdefault("loaded", loaded)
        state["loaded"] = [m for m in state["loaded"] if m["model_id"] != mid]
        return True

    monkeypatch.setattr(pool_guard, "gpu_status", _status_after_unload)
    monkeypatch.setattr(pool_guard, "_unload_model", _fake_unload)
    monkeypatch.setenv("LOCAL_CHAT_POOL_FREE_VRAM", "1")

    verdict = pool_guard.free_vram_before_mint()
    assert verdict["ok"] is True
    assert verdict["unloaded"] == ["bench"]
    assert verdict["skipped_pinned"] == ["pinned-one"]
    assert verdict["skipped_active"] == ["busy-one"]


def test_guard_still_short_after_unload_blocks(monkeypatch):
    loaded = [{"model_id": "bench", "active_requests": 0}]
    _patched_rig(monkeypatch, {0: 3.2, 1: 3.8}, selected=1, loaded=loaded)
    monkeypatch.setattr(pool_guard, "gpu_status",
                        lambda: _status({0: 3.2, 1: 4.2}))  # unload freed nothing
    monkeypatch.setenv("LOCAL_CHAT_POOL_FREE_VRAM", "1")
    verdict = pool_guard.free_vram_before_mint()
    assert verdict["ok"] is False
    assert "STILL SHORT" in verdict["reason"]


def test_guard_dry_run_never_touches_the_rig(monkeypatch):
    loaded = [{"model_id": "bench", "active_requests": 0}]
    _patched_rig(monkeypatch, {0: 3.2, 1: 3.8}, selected=1, loaded=loaded)
    unloaded: list[str] = []
    monkeypatch.setattr(pool_guard, "_unload_model",
                        lambda mid: (unloaded.append(mid), True)[1])
    monkeypatch.setattr(pool_guard, "gpu_status",
                        lambda: _status({0: 3.2, 1: 3.8}, loaded))
    verdict = pool_guard.free_vram_before_mint(dry_run=True)
    assert unloaded == []                       # dry run → zero rig calls
    assert verdict["unloaded"] == ["bench"]     # ...but it reports intent
    assert "DRY-RUN" in verdict["reason"]


def test_guard_fails_open_when_gpu_cli_missing(monkeypatch):
    monkeypatch.setattr(pool_guard, "gpu_status", lambda: None)
    verdict = pool_guard.free_vram_before_mint()
    assert verdict["ok"] is True
    assert verdict["reason"] == "gpu-cli-unavailable"


# --------------------------------------------------------------------------- #
# Refusal tally + backoff
# --------------------------------------------------------------------------- #


def test_refusal_tally_and_success_reset():
    assert pool_guard.consecutive_failures() == 0
    pool_guard.note_refill_failure("refused", "GPU 1: 3.1 GB free", actor="main")
    pool_guard.note_refill_failure("rig-error", "timed out")
    assert pool_guard.consecutive_failures() == 2
    stats = pool_guard.refill_failure_stats()
    assert stats["failures_24h"] == 2
    assert stats["consecutive"] == 2
    assert stats["recent"][-1]["kind"] == "rig-error"
    pool_guard.note_refill_success()
    assert pool_guard.consecutive_failures() == 0


def test_backoff_doubles_and_caps():
    assert pool_guard.backoff_s(0) == 300
    assert pool_guard.backoff_s(1) == 300
    assert pool_guard.backoff_s(2) == 600
    assert pool_guard.backoff_s(3) == 1200
    assert pool_guard.backoff_s(4) == 2400
    assert pool_guard.backoff_s(5) == 3600
    assert pool_guard.backoff_s(99) == 3600


# --------------------------------------------------------------------------- #
# reactions.pool_refill — guard routing
# --------------------------------------------------------------------------- #


def test_pool_refill_blocks_before_minting_when_vram_short(rx_env, monkeypatch):
    """The core guard path: rig short → refill returns 0 and _pool_generate_one
    is NEVER called (no refused-call hammer)."""
    called: list[str] = []
    monkeypatch.setattr(pool_guard, "free_vram_before_mint",
                        lambda **kw: {"ok": False, "reason": "STILL SHORT (3.1 GB)"})
    monkeypatch.setattr(reactions, "_pool_generate_one",
                        lambda *a, **k: called.append("mint") or "x")
    assert pool_refill(bot_id="main") == 0
    assert called == []
    st = reactions.pool_load("main")
    assert st.last_error.startswith("rig-vram-short")
    assert pool_guard.consecutive_failures() >= 1


def test_pool_refill_breaks_on_rig_refusal(rx_env, monkeypatch):
    """Refusal (rig-side) ends the round after ONE call instead of continuing
    to burn the whole 50-call plan."""
    monkeypatch.setattr(pool_guard, "free_vram_before_mint",
                        lambda **kw: {"ok": True})
    calls: list[str] = []

    def _refuse(cfg, category, *, bot_id=None):
        calls.append(category)
        reactions._LAST_GEN_RIG_REFUSED = True
        pool_guard.note_refill_failure("refused", "fake refusal", actor=bot_id)
        return None

    monkeypatch.setattr(reactions, "_pool_generate_one", _refuse)
    assert pool_refill(bot_id="main") == 0
    assert calls == ["happy"]          # one refusal, then break — not 3
    assert pool_guard.consecutive_failures() >= 1


def test_pool_refill_continues_on_local_failure(rx_env, monkeypatch):
    """A LOCAL failure (compose/store) only skips that mood — the round still
    tries the rest of the plan."""
    monkeypatch.setattr(pool_guard, "free_vram_before_mint",
                        lambda **kw: {"ok": True})
    calls: list[str] = []

    def _local_fail(cfg, category, *, bot_id=None):
        calls.append(category)
        reactions._LAST_GEN_RIG_REFUSED = False
        return None

    monkeypatch.setattr(reactions, "_pool_generate_one", _local_fail)
    assert pool_refill(bot_id="main") == 0
    assert calls == ["happy", "happy", "happy"]   # all three attempted
    assert pool_guard.consecutive_failures() == 0  # local failures don't tally


def test_pool_refill_success_resets_tally(rx_env, monkeypatch):
    monkeypatch.setattr(pool_guard, "free_vram_before_mint",
                        lambda **kw: {"ok": True})
    pool_guard.note_refill_failure("refused", "old badness")
    calls: list[str] = []

    def _ok(cfg, category, *, bot_id=None):
        calls.append(category)
        reactions._LAST_GEN_RIG_REFUSED = False
        pool_guard.note_refill_success()
        return f"pool-{category}-{len(calls)}"

    monkeypatch.setattr(reactions, "_pool_generate_one", _ok)
    assert pool_refill(bot_id="main") == 3
    assert pool_guard.consecutive_failures() == 0   # success resets the streak


# --------------------------------------------------------------------------- #
# avatar_pool.refill — guard routing
# --------------------------------------------------------------------------- #


@pytest.fixture
def avatar_env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(config, "AVATAR_DIR", tmp_path / "avatars")
    config.ensure_dirs()
    avatar_pool._state_cache.clear()
    avatar_pool._bank_cache.clear()
    monkeypatch.setattr(avatar_pool, "image_cli_available", lambda: True)
    monkeypatch.setattr(avatar_pool, "deficit", lambda *a, **k: 2)
    yield tmp_path


def test_avatar_refill_blocks_before_minting_when_vram_short(avatar_env, monkeypatch):
    called: list[str] = []
    monkeypatch.setattr(pool_guard, "free_vram_before_mint",
                        lambda **kw: {"ok": False, "reason": "STILL SHORT (2.0 GB)"})
    monkeypatch.setattr(avatar_pool, "generate_pair",
                        lambda *a, **k: called.append("pair") or "x")
    assert avatar_pool.refill(bot_id="main") == 0
    assert called == []
    st = avatar_pool.load_state("main")
    assert st.last_error.startswith("rig-vram-short")


def test_avatar_refill_happy_path_no_guard_blocks(avatar_env, monkeypatch):
    monkeypatch.setattr(pool_guard, "free_vram_before_mint",
                        lambda **kw: {"ok": True})
    made = {"n": 0}

    def _pair(bot_id):
        made["n"] += 1
        return f"pair-{made['n']}"

    monkeypatch.setattr(avatar_pool, "generate_pair", _pair)
    assert avatar_pool.refill(bot_id="main") == 2
    assert made["n"] == 2


# --------------------------------------------------------------------------- #
# Unloading somebody else's model is opt-in
# --------------------------------------------------------------------------- #


def test_unloading_is_off_by_default(monkeypatch):
    """The image host's GPUs are usually shared with a working LLM. Evicting it
    for a decorative image is not a decision this feature makes on its own."""
    monkeypatch.delenv("LOCAL_CHAT_POOL_FREE_VRAM", raising=False)
    loaded = [{"model_id": "someone-elses-model", "active_requests": 0}]
    _patched_rig(monkeypatch, {0: 3.2, 1: 3.8}, selected=1, loaded=loaded)
    unloaded: list[str] = []
    monkeypatch.setattr(pool_guard, "_unload_model",
                        lambda mid: unloaded.append(mid) or True)

    verdict = pool_guard.free_vram_before_mint()
    assert unloaded == [], "a shared host's model was evicted without opt-in"
    # The useful half still works: short means "do not mint this round".
    assert verdict["ok"] is False
    assert "unloading is off" in verdict["reason"]


def test_headroom_ok_never_mentions_the_knob(monkeypatch):
    monkeypatch.delenv("LOCAL_CHAT_POOL_FREE_VRAM", raising=False)
    _patched_rig(monkeypatch, {0: 40.0}, selected=0, loaded=[])
    verdict = pool_guard.free_vram_before_mint()
    assert verdict["ok"] is True and verdict["unloaded"] == []


@pytest.mark.parametrize("value,expected", [
    (None, False), ("", False), ("0", False), ("false", False), ("off", False),
    ("1", True), ("true", True), ("yes", True),
])
def test_unload_enabled_reads_the_knob(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("LOCAL_CHAT_POOL_FREE_VRAM", raising=False)
    else:
        monkeypatch.setenv("LOCAL_CHAT_POOL_FREE_VRAM", value)
    assert pool_guard.unload_enabled() is expected
