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

from app import avatar_pool, config, image_jobs, pool_guard, reactions
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


# --------------------------------------------------------------------------- #
# Backend-down vs VRAM-short (2026-09-02)
#
# On 2026-09-02 the rig's ComfyUI was down from ~00:50 to ~15:00 JST. DisPatch
# spent fourteen hourly cycles finding that out one wasted `generate_image`
# call at a time, recorded every one of them as a generic "refused", and
# alerted with a text that GUESSED between "VRAM contention or rig down". Six
# more refusals the same afternoon were the opposite error: the guard demanded
# the 10 GB ComfyUI wants when SELECTING a card, from a ComfyUI that had
# already selected one and was holding 16 GB of warm weights on it.
# --------------------------------------------------------------------------- #


def _cli_status(running=True, selected=3, last_error=""):
    return {"comfy": {"running": running, "gpu_selected": selected,
                      "last_error": last_error}}


def _fake_readiness(can_render=True, reason="unknown", detail=""):
    """A canned `RigReadiness`, for monkeypatching `pool_guard.rig_readiness`.

    The gate (12.6) reads `can_render` only — never `comfy.running` — so this
    is the one seam the "backend down" tests below need; `_image_cli_status`
    keeps supplying `gpu_selected` for the headroom math, which is unrelated.
    """
    return image_jobs.RigReadiness(can_render=can_render, reason=reason,
                                   detail=detail)


def test_backend_state_reads_the_image_cli(monkeypatch):
    monkeypatch.setattr(pool_guard, "_image_cli_status",
                        lambda: _cli_status(running=False, last_error="boom"))
    st = pool_guard.image_backend_state()
    assert st["known"] is True
    assert st["running"] is False
    assert st["error"] == "boom"


def test_backend_state_unknown_without_a_cli(monkeypatch):
    monkeypatch.setattr(pool_guard, "_image_cli_status", lambda: None)
    st = pool_guard.image_backend_state()
    assert st == {"known": False, "running": None, "gpu_selected": None,
                  "error": "", "auto_start": False}


def test_backend_state_tolerates_a_status_without_the_running_field():
    # The pre-2026-09 status shape. Absent must never read as "down".
    st = pool_guard.image_backend_state({"comfy": {"gpu_selected": 1}})
    assert st["known"] is True and st["running"] is None
    assert st["gpu_selected"] == 1


def test_guard_blocks_and_names_a_backend_the_rig_says_cannot_render(monkeypatch):
    _patched_rig(monkeypatch, {0: 25.0, 1: 25.0}, selected=1)
    monkeypatch.setattr(
        pool_guard, "rig_readiness",
        lambda: _fake_readiness(can_render=False, reason="circuit_open",
                                detail="crash-loop breaker tripped"))
    verdict = pool_guard.free_vram_before_mint()
    # Plenty of VRAM — and still a refusal, because the rig itself said no.
    assert verdict["ok"] is False
    assert verdict["backend"] == "down"
    assert "crash-loop breaker tripped" in verdict["reason"]


def test_guard_blocks_when_the_rig_reports_no_suitable_gpu(monkeypatch):
    _patched_rig(monkeypatch, {0: 25.0}, selected=0)
    monkeypatch.setattr(
        pool_guard, "rig_readiness",
        lambda: _fake_readiness(can_render=False, reason="no_suitable_gpu"))
    verdict = pool_guard.free_vram_before_mint()
    assert verdict["ok"] is False and verdict["backend"] == "down"
    assert "no_suitable_gpu" in verdict["reason"]


def test_running_backend_is_not_held_to_the_selection_threshold(monkeypatch):
    """The regression that cost six refills: warm weights are not "short"."""
    _patched_rig(monkeypatch, {0: 7.5, 3: 4.2}, selected=3)
    monkeypatch.setattr(pool_guard, "_image_cli_status",
                        lambda: _cli_status(running=True, selected=3))
    verdict = pool_guard.free_vram_before_mint()
    assert verdict["ok"] is True
    assert verdict["backend"] == "up"
    assert "headroom-ok" in verdict["reason"]


def test_a_running_backend_with_no_room_at_all_still_blocks(monkeypatch):
    _patched_rig(monkeypatch, {3: 0.4}, selected=3)
    monkeypatch.setattr(pool_guard, "_image_cli_status",
                        lambda: _cli_status(running=True, selected=3))
    verdict = pool_guard.free_vram_before_mint()
    assert verdict["ok"] is False
    assert verdict["backend"] == "up"          # up, just full


def test_selection_threshold_still_applies_when_nothing_is_running(monkeypatch):
    # No ComfyUI yet: it has to CHOOSE a card, and 10 GB is what it wants.
    _patched_rig(monkeypatch, {0: 4.2}, selected=None)
    monkeypatch.setattr(pool_guard, "_image_cli_status", lambda: None)
    verdict = pool_guard.free_vram_before_mint()
    assert verdict["ok"] is False
    assert "4.2 GB" in verdict["reason"] and "10.0" in verdict["reason"]


def test_explicit_min_free_gb_is_never_overridden(monkeypatch):
    _patched_rig(monkeypatch, {3: 4.2}, selected=3)
    monkeypatch.setattr(pool_guard, "_image_cli_status",
                        lambda: _cli_status(running=True, selected=3))
    assert pool_guard.free_vram_before_mint(min_free_gb=20.0)["ok"] is False


# --- classifier ------------------------------------------------------------ #


def test_classify_uses_the_structured_code_first():
    assert pool_guard.classify_cli_failure(
        "anything at all", "backend_unavailable") == pool_guard.KIND_BACKEND_DOWN
    assert pool_guard.classify_cli_failure(
        "ConnectError: all connection attempts failed",
        "insufficient_vram") == "refused"


def test_classify_recognises_the_rigs_connect_error_prose():
    # The exact string 54 of the 60 failures on 2026-09-02 carried.
    msg = ("Error executing tool generate_image: Generation failed: "
           "ConnectError: All connection attempts failed")
    assert pool_guard.classify_cli_failure(msg) == pool_guard.KIND_BACKEND_DOWN


def test_classify_defaults_to_refused():
    assert pool_guard.classify_cli_failure(
        "Not enough free VRAM, short by 15.9 GB") == "refused"
    assert pool_guard.classify_cli_failure("") == "refused"


def test_stats_break_the_count_down_by_kind():
    pool_guard.note_refill_failure(pool_guard.KIND_BACKEND_DOWN, "down", actor="main")
    pool_guard.note_refill_failure(pool_guard.KIND_BACKEND_DOWN, "down", actor="beta")
    pool_guard.note_refill_failure("vram-short", "4.2 < 10.0", actor="alpha")
    stats = pool_guard.refill_failure_stats()
    assert stats["failures_24h"] == 3
    assert stats["by_kind"] == {pool_guard.KIND_BACKEND_DOWN: 2, "vram-short": 1}


def test_a_trailing_newline_does_not_pass_an_id_as_a_path_component():
    # `^…$` + .match() accepts "main\n"; the id becomes a directory name.
    from app import avatar_pool, reactions
    for rx in (reactions.ID_RE, reactions.MOOD_DIR_RE, reactions.BOT_ID_RE,
               avatar_pool._BOT_ID_RE):
        assert rx.match("main")
        assert not rx.match("main\n")
        assert not rx.match("ma/in")


# --------------------------------------------------------------------------- #
# Idle-unload is not an outage (2026-09-03)
#
# ClawForge stops ComfyUI after `idle_unload.minutes` of quiet and starts it
# again on the next render. On an hourly picture cadence that means
# `comfy.running` is false for most of every hour, and the pools -- which look
# every 300 s -- spent 2026-09-03 reading a normal idle state as a dead
# backend: 70 refill rounds skipped while the rig rendered on demand the whole
# time (proven with a live 62 s render taken while running was false).
# --------------------------------------------------------------------------- #


def _idle_status(running=False, last_error="", selected=1, idle_minutes=10):
    """A status from a host that manages ComfyUI's lifecycle itself."""
    return {"comfy": {"running": running, "gpu_selected": selected,
                      "last_error": last_error},
            "idle_unload": {"minutes": idle_minutes, "active_jobs": 0,
                            "idle_s": 725, "models_unloaded": True}}


def test_backend_state_reports_that_the_host_starts_it_on_demand():
    st = pool_guard.image_backend_state(_idle_status())
    assert st["known"] is True
    assert st["running"] is False
    assert st["auto_start"] is True


def test_backend_state_without_idle_unload_does_not_claim_auto_start():
    st = pool_guard.image_backend_state({"comfy": {"running": False}})
    assert st["auto_start"] is False


def test_a_disabled_idle_unload_is_not_auto_start():
    """minutes: 0 means the host is NOT going to bring it back by itself."""
    st = pool_guard.image_backend_state(_idle_status(idle_minutes=0))
    assert st["auto_start"] is False


def test_idle_unloaded_backend_does_not_block_the_round(monkeypatch):
    """THE 2026-09-03 regression: 70 blocked refills on a healthy rig."""
    _patched_rig(monkeypatch, {0: 25.0, 1: 25.0}, selected=1)
    monkeypatch.setattr(pool_guard, "_image_cli_status", lambda: _idle_status())
    verdict = pool_guard.free_vram_before_mint()
    assert verdict["ok"] is True
    assert verdict["backend"] != "down"


def test_a_stopped_backend_the_rig_says_it_can_still_render_does_not_block(
        monkeypatch):
    """`can_render` is the ONLY gate (12.6) — `comfy.running: false` alone,

    with the rig itself saying a job would run (``can_render: True``,
    e.g. reason ``startable``), must not block: the next job just starts it.
    """
    _patched_rig(monkeypatch, {0: 25.0}, selected=0)
    monkeypatch.setattr(pool_guard, "_image_cli_status",
                        lambda: _cli_status(running=False))
    monkeypatch.setattr(pool_guard, "rig_readiness",
                        lambda: _fake_readiness(can_render=True,
                                                reason="startable"))
    verdict = pool_guard.free_vram_before_mint()
    assert verdict["ok"] is True and verdict["backend"] != "down"


def test_a_rig_that_says_the_backend_cannot_be_started_blocks(monkeypatch):
    """`can_render: False` blocks regardless of `comfy.running`/idle-unload."""
    _patched_rig(monkeypatch, {0: 25.0}, selected=0)
    monkeypatch.setattr(pool_guard, "_image_cli_status",
                        lambda: _cli_status(running=False))
    monkeypatch.setattr(
        pool_guard, "rig_readiness",
        lambda: _fake_readiness(can_render=False,
                                reason="unmanaged_backend_down"))
    verdict = pool_guard.free_vram_before_mint()
    assert verdict["ok"] is False and verdict["backend"] == "down"


def test_no_configured_rig_client_fails_open_on_the_can_render_gate(
        monkeypatch):
    """No `DISPATCH_CLAWFORGE_URL` → `rig_readiness()` is None → the gate

    does not fire at all (the headroom check downstream still can).
    """
    _patched_rig(monkeypatch, {0: 25.0}, selected=0)
    monkeypatch.setattr(pool_guard, "_image_cli_status",
                        lambda: _idle_status(last_error="CUDA error: no device"))
    monkeypatch.setattr(pool_guard, "rig_readiness", lambda: None)
    verdict = pool_guard.free_vram_before_mint()
    assert verdict["backend"] != "down"


# --------------------------------------------------------------------------- #
# Only a `benchmark` lease is a stand-down (rig contract §12.5, G24 2026-09-10)
#
# `kind` is the ONLY signal. A `render`/`agent` lease is ordinary contention
# for a card — most commonly ClawForge's own near-permanent idle hold on its
# own ComfyUI card (`holder=clawforge2 kind=render state=idle`, re-asked
# every 60s) — and must never earn the same block a real benchmark does.
# This also retires the old self-holder exclusion (2026-09-03): a `render`
# lease is never a stand-down whether it is ours or somebody else's, so
# there is nothing left here that needs to know who "we" are.
# --------------------------------------------------------------------------- #


def _leases(*entries):
    return {"leases": list(entries)}


def test_a_render_lease_is_never_a_blocker_even_when_it_is_our_own(monkeypatch):
    """THE 2026-09-10 regression: 20 `leased` refusals/day against ClawForge's
    own permanent idle hold on its ComfyUI card."""
    monkeypatch.setattr(pool_guard, "lease_url", lambda: "http://rig/api/leases")
    monkeypatch.setattr(pool_guard, "_fetch_leases",
                        lambda url: _leases({"holder": "clawforge2",
                                             "kind": "render",
                                             "state": "idle"}))
    assert pool_guard.rig_lease_holder() is None


def test_a_render_lease_is_never_a_blocker_from_a_third_party_either(
        monkeypatch):
    monkeypatch.setattr(pool_guard, "lease_url", lambda: "http://rig/api/leases")
    monkeypatch.setattr(pool_guard, "_fetch_leases",
                        lambda url: _leases({"holder": "somebody-else",
                                             "kind": "render",
                                             "state": "active"}))
    assert pool_guard.rig_lease_holder() is None


def test_an_agent_lease_does_not_block(monkeypatch):
    monkeypatch.setattr(pool_guard, "lease_url", lambda: "http://rig/api/leases")
    monkeypatch.setattr(pool_guard, "_fetch_leases",
                        lambda url: _leases({"holder": "bits", "kind": "agent",
                                             "state": "active"}))
    assert pool_guard.rig_lease_holder() is None


def test_an_other_lease_does_not_block(monkeypatch):
    monkeypatch.setattr(pool_guard, "lease_url", lambda: "http://rig/api/leases")
    monkeypatch.setattr(pool_guard, "_fetch_leases",
                        lambda url: _leases({"holder": "somebody",
                                             "kind": "other",
                                             "state": "active"}))
    assert pool_guard.rig_lease_holder() is None


def test_a_released_benchmark_lease_does_not_block(monkeypatch):
    """holder stays populated after release; state is the live part."""
    monkeypatch.setattr(pool_guard, "lease_url", lambda: "http://rig/api/leases")
    monkeypatch.setattr(pool_guard, "_fetch_leases",
                        lambda url: _leases({"holder": "bench-runner",
                                             "kind": "benchmark",
                                             "state": "none"}))
    assert pool_guard.rig_lease_holder() is None


def test_a_real_benchmark_lease_still_blocks(monkeypatch):
    monkeypatch.setattr(pool_guard, "lease_url", lambda: "http://rig/api/leases")
    monkeypatch.setattr(pool_guard, "_fetch_leases",
                        lambda url: _leases({"holder": "bench-runner",
                                             "kind": "benchmark",
                                             "state": "active"}))
    assert pool_guard.rig_lease_holder() == "bench-runner"


def test_a_benchmark_lease_without_a_state_field_still_blocks(monkeypatch):
    """A lease book that predates `state` must keep failing safe."""
    monkeypatch.setattr(pool_guard, "lease_url", lambda: "http://rig/api/leases")
    monkeypatch.setattr(pool_guard, "_fetch_leases",
                        lambda url: _leases({"holder": "bench-runner",
                                             "kind": "benchmark"}))
    assert pool_guard.rig_lease_holder() == "bench-runner"


def test_a_lease_with_no_kind_at_all_does_not_block(monkeypatch):
    """A lease book that predates `kind` fails the same way `kind` itself
    does when it is unrecognised: not one of the closed vocabulary's
    stand-down kinds, so not a blocker (§12.5: branch on `kind` and ONLY on
    `kind`)."""
    monkeypatch.setattr(pool_guard, "lease_url", lambda: "http://rig/api/leases")
    monkeypatch.setattr(pool_guard, "_fetch_leases",
                        lambda url: _leases({"holder": "bench-runner",
                                             "state": "active"}))
    assert pool_guard.rig_lease_holder() is None


def test_self_holder_is_read_from_the_image_hosts_own_lease_view():
    """`image_self_lease_holder()` itself still works — it is just no longer
    called by `rig_lease_holder()`, which reads `kind` instead."""
    status = {"comfy": {"lease": {"holder": "clawforge2", "state": "none"}}}
    assert pool_guard.image_self_lease_holder(status) == "clawforge2"


# --------------------------------------------------------------------------- #
# A leased rig is reported as leased, not as a VRAM shortage (2026-09-03)
#
# free_vram_before_mint returns backend "leased", but both refill callers only
# special-cased "down" -- so 35 lease blocks were filed under `vram-short` on a
# rig with 23 GB free, in /api/health and in every pool's last_error.
# --------------------------------------------------------------------------- #


def test_reaction_refill_names_a_lease_as_a_lease(rx_env, monkeypatch):
    monkeypatch.setattr(pool_guard, "free_vram_before_mint",
                        lambda **kw: {"ok": False, "backend": "leased",
                                      "reason": "rig leased by bench-runner"})
    monkeypatch.setattr(reactions, "_pool_generate_one",
                        lambda *a, **k: pytest.fail("must not mint"))
    assert pool_refill(bot_id="main") == 0
    st = reactions.pool_load("main")
    assert st.last_error.startswith("rig-leased")
    assert "vram" not in st.last_error.lower()
    assert pool_guard.refill_failure_stats()["by_kind"] == {"leased": 1}


def test_avatar_refill_names_a_lease_as_a_lease(avatar_env, monkeypatch):
    monkeypatch.setattr(pool_guard, "free_vram_before_mint",
                        lambda **kw: {"ok": False, "backend": "leased",
                                      "reason": "rig leased by bench-runner"})
    monkeypatch.setattr(avatar_pool, "generate_pair",
                        lambda *a, **k: pytest.fail("must not mint"))
    assert avatar_pool.refill(bot_id="main") == 0
    st = avatar_pool.load_state("main")
    assert st.last_error.startswith("rig-leased")
    assert "vram" not in st.last_error.lower()
    assert pool_guard.refill_failure_stats()["by_kind"] == {"leased": 1}
