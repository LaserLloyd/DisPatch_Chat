"""Foundation contracts for the 4.0 wave packages (BUILD-PLAN-v2 §2.2).

Pins what the parallel packages build against: module and function NAMES and SIGNATURES
(not stub behaviour, which the packages replace), the one-time wiring in main.py, the
add-on host hooks, ``policy.unsigned`` and the lock-target fence. A package that has to
change a signature changes it here too and says so in its report.
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from app import (
    accounts,
    auth,
    backends_policy,
    ext_profile_hooks,
    extensions,
    maintenance,
    ownership,
    policy,
    profiles_service,
)
from app.contracts.permissions import Role

MAIN = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text(encoding="utf-8")


def _params(fn) -> list[str]:
    return list(inspect.signature(fn).parameters)


def test_profiles_service_signatures():
    want = {
        "content_counts": ["profile_id"],
        "wipe_profile_content": ["profile_id"],
        "spawn_atlas": ["profile_id"],
        "reset_preview": ["profile_id"],
        "reset_profile": ["profile_id", "token"],
        "change_role_preview": ["profile_id", "new_role"],
        "change_role": ["profile_id", "new_role", "confirm_token", "household_bots"],
        "create_personal": ["profile_id", "fields"],
        "clone_to_own": ["bot_id", "principal"],
        "clone_to_profile": ["bot_id", "profile_id", "actor"],
        "on_profile_created": ["profile", "first_admin"],
    }
    for name, params in want.items():
        assert _params(getattr(profiles_service, name)) == params, name
    assert profiles_service.ContentCounts().as_dict() == {"bots": 0, "chats": 0, "pictures": 0, "voices": 0}
    assert profiles_service.TOKEN_TTL_SECONDS == 300
    assert issubclass(profiles_service.ProfileServiceError, ValueError)


def test_backends_and_maintenance_signatures():
    assert _params(backends_policy.validate_backends) == ["value", "known_connections"]
    assert _params(backends_policy.allowed_backends) == ["profile"]
    assert _params(maintenance.start) == ["app"]
    assert _params(maintenance.stop) == ["app"]
    assert inspect.iscoroutinefunction(maintenance.stop)
    assert _params(maintenance.run_nightly) == ["now", "steps"]
    maintenance.start(object())                     # a no-op today, and it must never raise


def test_main_wires_each_new_router_and_the_maintenance_task_exactly_once():
    for router in ("profiles_api", "bot_builder_api", "maintenance_api"):
        assert MAIN.count(f"app.include_router({router}.router)") == 1, router
    assert MAIN.count("maintenance.start(app)") == 1
    assert MAIN.count("await maintenance.stop()") == 1
    assert '_accepts(advisor.run_turn, "owner_profile_id")' in MAIN


def test_ownership_pure_names():
    for name in ("may_see_thread", "readable_owner_ids", "owner_filter_sql", "require_thread",
                 "require_message", "remember", "cached_owner", "is_known", "forget_all",
                 "frame_thread_id", "prime_for_frame", "corpus_profile_id", "protected_admin_id",
                 "effective_owner", "for_principal", "for_request"):
        assert callable(getattr(ownership, name)), name
    assert not hasattr(ownership, "_anonymous_reads")


# --------------------------------------------------------------------------- #
# The add-on host hooks (voice-host contract)
# --------------------------------------------------------------------------- #


def test_the_extension_context_offers_the_four_hooks():
    for name in ("caller", "bots_visible", "register_voice_access", "register_profile_data"):
        assert callable(getattr(extensions.ExtensionContext, name)), name
    assert extensions.EXT_API == 1
    fields = {f.name for f in __import__("dataclasses").fields(extensions.ThreadSocket)}
    assert "profile_id" in fields
    bv = {f.name for f in __import__("dataclasses").fields(extensions.BotView)}
    assert {"scope", "owner_profile"} <= bv


def test_hook_signatures():
    h = ext_profile_hooks
    assert _params(h.caller) == ["request"]
    assert _params(h.caller_of) == ["principal"]
    assert _params(h.bots_visible) == ["request"]
    assert _params(h.register_voice_access) == ["ext_id", "fn"]
    assert _params(h.register_profile_data) == ["ext_id", "count", "purge"]
    assert _params(h.voice_allowed) == ["voice_id", "bot_owner"]
    assert _params(h.voice_owned_clones_active) == []
    assert _params(h.count_profile_data) == ["profile_id"]
    assert _params(h.purge_profile_data) == ["profile_id"]
    assert [f for f in ("profile_id", "is_admin", "role")
            if f in {x.name for x in __import__("dataclasses").fields(h.Caller)}] == \
        ["profile_id", "is_admin", "role"]


def test_registries_record_and_forget():
    h = ext_profile_hooks
    h.reset_for_tests()
    h.register_voice_access("voice", lambda v, o: True)
    h.register_profile_data("voice", count=lambda p: {"voices": 1}, purge=lambda p: 1)
    assert set(h.registered_voice_access()) == {"voice"}
    assert set(h.registered_profile_data()) == {"voice"}
    with pytest.raises(TypeError):
        h.register_voice_access("x", "not callable")
    h.forget("voice")
    assert not h.registered_voice_access() and not h.registered_profile_data()


def test_caller_mapping():
    owner = accounts.create_profile("Owner", Role.ADMIN, protected=True)
    usr = accounts.create_profile("Uma", Role.USER)
    c = ext_profile_hooks.caller_of(policy.profile_principal(usr))
    assert (c.profile_id, c.is_admin, c.role) == (usr.id, False, "user")
    assert c.can("voice_lab") and not c.can("manage_plugins")
    a = ext_profile_hooks.caller_of(policy.profile_principal(owner))
    assert a.is_admin and a.can("manage_plugins")
    o = ext_profile_hooks.caller_of(policy.open_principal())
    assert o.is_admin and o.profile_id is None and o.can("anything")
    assert ext_profile_hooks.caller_of(None) is None
    assert ext_profile_hooks.caller_of(policy.machine_principal("api_token")) is None
    assert ext_profile_hooks.caller_of(policy.anonymous()) is None
    assert ext_profile_hooks.is_household_voice("") and ext_profile_hooks.is_household_voice("builtin:af")
    assert not ext_profile_hooks.is_household_voice("26-10-abcdef")


# --------------------------------------------------------------------------- #
# Sign-in fields, policy.unsigned, the lock-target fence
# --------------------------------------------------------------------------- #


def _lock(target: str, pid: str | None) -> None:
    cfg = auth.load()
    cfg.lock_target, cfg.default_profile = target, pid
    auth._write(cfg)
    auth._bust_cache()


def test_security_yaml_carries_lock_target_and_default_profile():
    cfg = auth.SecurityConfig()
    assert (cfg.lock_target, cfg.default_profile) == ("profile_list", None)
    pg = accounts.create_profile("Playground", Role.USER)
    _lock("default_profile", pg.id)
    auth._cache = None
    cfg = auth.load()
    assert (cfg.lock_target, cfg.default_profile) == ("default_profile", pg.id)
    text = auth.SECURITY_PATH.read_text()
    assert "\nlock_target: " in text and "\ndefault_profile: " in text and "people:" not in text
    _lock("nonsense", None)
    auth._cache = None
    assert auth.load().lock_target == "profile_list"


def test_unsigned_is_the_default_profile_or_nobody():
    auth.set_pin("2468")
    pg = accounts.create_profile("Playground", Role.USER)
    assert policy.unsigned() is None                        # profile_list (the default)
    _lock("default_profile", pg.id)
    pr = policy.unsigned()
    assert pr is not None and (pr.kind, pr.profile_id, pr.via, pr.role) == ("profile", pg.id, "default", "user")
    accounts.update_profile(pg.id, disabled=True)
    assert policy.unsigned() is None
    _lock("default_profile", "gone")
    assert policy.unsigned() is None


def test_the_lock_target_fence_applies_to_the_default_profile_only():
    pg = accounts.create_profile("Playground", Role.USER, grants=["dispatch_agents"])
    other = accounts.create_profile("Uma", Role.USER, grants=["dispatch_agents"])
    fence = {"dispatch_agents", "approve_agent_actions", "voice_lab"}
    assert "voice_lab" in policy.effective_permissions(pg)
    _lock("default_profile", pg.id)
    assert policy.is_lock_target(pg) and not policy.is_lock_target(other)
    assert not (policy.effective_permissions(pg) & fence)
    assert {"dispatch_agents", "voice_lab"} <= policy.effective_permissions(other)
    _lock("profile_list", pg.id)                            # the fence follows lock_target
    assert "voice_lab" in policy.effective_permissions(pg)
