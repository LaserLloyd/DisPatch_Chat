"""The voice host contract, host side (4.0, BUILD-PLAN-v2 §1.12, ~/.cache/cfp/voice-host-
contract.md): a fake add-on exercising every hook, and the legacy behaviour when an add-on
has not declared per-profile clones.

Run: cd backend && uv run pytest tests/test_ext_profile_hooks.py
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
import yaml

from app import accounts, auth, config, policy, route_policy
from app import ext_profile_hooks as hooks
from app.contracts.permissions import Role

PROV = {"providers": [{"provider": "openai", "model": "m", "base_url": "http://127.0.0.1:9/v1"}]}


@pytest.fixture
def house(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(auth, "SECURITY_PATH", tmp_path / "security.yaml")
    auth._bust_cache()
    accounts.reset_for_tests()
    hooks.reset_for_tests()
    owner = accounts.create_profile("Owner", Role.ADMIN, protected=True)
    a = accounts.create_profile("Ann", Role.USER)
    b = accounts.create_profile("Ben", Role.USER)
    basic = accounts.create_profile("Bo", Role.BASIC, household_bots=["house"])
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"bots": [
        {"id": "house", "name": "House", "advisor": PROV},
        {"id": "pa00000000000", "name": "AnnsOwn", "advisor": PROV, "scope": "personal",
         "owner_profile": a.id},
    ]}))
    config._invalidate_bots_cache()
    yield SimpleNamespace(owner=owner, a=a, b=b, basic=basic)
    hooks.reset_for_tests()
    accounts.reset_for_tests()
    config._invalidate_bots_cache()
    auth._bust_cache()


def _req(principal):
    from app.policy import Policy
    return SimpleNamespace(state=SimpleNamespace(policy=Policy(principal)), cookies={})


def P(prof):
    return policy.profile_principal(accounts.get_profile(prof.id))


class FakeVoiceAddon:
    """Keeps clones as {id: owner}; owner "household" for a household clone."""

    def __init__(self):
        self.clones = {"hh-1": "household"}

    def access(self, voice_id, bot_owner):
        owner = self.clones.get(voice_id)
        if owner is None:
            return False
        return owner == "household" or owner == bot_owner

    def count(self, pid):
        return {"voices": sum(1 for o in self.clones.values() if o == pid)}

    def purge(self, pid):
        gone = [k for k, o in self.clones.items() if o == pid]
        for k in gone:
            del self.clones[k]
        return len(gone)


def test_caller(house):
    c = hooks.caller(_req(P(house.a)))
    assert c.profile_id == house.a.id and c.role == "user" and not c.is_admin
    assert c.can("voice_lab") and c.can("use_voice") and not c.can("manage_plugins")
    assert hooks.caller(_req(P(house.owner))).is_admin
    assert not hooks.caller(_req(P(house.basic))).can("voice_lab")
    assert hooks.caller(_req(policy.machine_principal("api_token"))) is None
    assert hooks.caller(_req(policy.nobody())) is None
    o = hooks.caller(_req(policy.open_principal()))
    assert o.is_admin and o.can("anything")


def test_the_default_profile_is_a_caller_that_cannot_clone(house):
    auth.set_lock_target("default_profile", house.b.id)
    c = hooks.caller(_req(policy.unsigned()))
    assert c is not None and c.profile_id == house.b.id and c.role == "user"
    assert c.can("use_voice") and not c.can("voice_lab")


def test_bots_visible_is_per_caller(house):
    def ids(pr):
        return {b.id for b in hooks.bots_visible(_req(pr))}
    assert ids(P(house.a)) == {"house", "pa00000000000"}
    assert ids(P(house.b)) == {"house"}
    assert ids(P(house.owner)) == {"house"}                 # Admins included
    assert ids(P(house.basic)) == {"house"}
    assert ids(policy.nobody()) == set()
    views = hooks.bots_visible(_req(P(house.a)))
    own = next(v for v in views if v.id == "pa00000000000")
    assert own.scope == "personal" and own.owner_profile == house.a.id


def test_legacy_without_a_declaration(house):
    assert hooks.voice_owned_clones_active() is False
    assert hooks.voice_allowed("anything", house.a.id) is True
    assert hooks.count_profile_data(house.a.id) == {}
    assert hooks.purge_profile_data(house.a.id) == 0
    # the 3.x table: three reads use_voice, everything else manage_plugins
    req = route_policy.extension_requirement
    assert req("GET", "/api/voice/profiles").permission == "use_voice"
    assert req("POST", "/api/voice/profiles").permission == "manage_plugins"
    assert req("GET", "/api/voice/profiles/x1").permission == "manage_plugins"
    assert req("POST", "/api/voice/profiles/x1/say").permission == "manage_plugins"


def test_a_declaring_addon_gets_the_voice_lab_tier_and_its_rules_are_asked(house):
    addon = FakeVoiceAddon()
    hooks.register_voice_access("voice", addon.access)
    hooks.register_profile_data("voice", count=addon.count, purge=addon.purge)
    addon.clones["a-1"] = house.a.id
    addon.clones["a-2"] = house.a.id
    assert hooks.voice_owned_clones_active() is True
    req = route_policy.extension_requirement
    for m, p in (("GET", "/api/voice/status"), ("GET", "/api/voice/preview"),
                 ("GET", "/api/voice/profiles"), ("GET", "/api/voice/profiles/a-1"),
                 ("GET", "/api/voice/profiles/a-1/files/reference.wav")):
        assert req(m, p).permission == "use_voice", (m, p)
    for m, p in (("POST", "/api/voice/profiles"), ("POST", "/api/voice/profiles/analyse"),
                 ("PATCH", "/api/voice/profiles/a-1"), ("DELETE", "/api/voice/profiles/a-1"),
                 ("POST", "/api/voice/profiles/a-1/say")):
        assert req(m, p).permission == "voice_lab", (m, p)
    for m, p in (("POST", "/api/voice/lab"), ("POST", "/api/voice/lab/test"),
                 ("PUT", "/api/voice/settings"), ("POST", "/api/voice/acks/rebuild")):
        assert req(m, p).permission == "manage_plugins", (m, p)
    # a Basic profile (voice, no voice_lab) reads but cannot clone; the default profile neither
    assert policy.authorize(P(house.basic), req("GET", "/api/voice/profiles")).allowed
    assert not policy.authorize(P(house.basic), req("POST", "/api/voice/profiles")).allowed
    assert policy.authorize(P(house.a), req("POST", "/api/voice/profiles")).allowed
    # voice_allowed: built-ins always; a personal clone only for its owner's bots
    assert hooks.voice_allowed("", None) and hooks.voice_allowed("builtin:af_x", house.b.id)
    assert hooks.voice_allowed("hh-1", None) and hooks.voice_allowed("hh-1", house.b.id)
    assert hooks.voice_allowed("a-1", house.a.id)
    assert not hooks.voice_allowed("a-1", house.b.id) and not hooks.voice_allowed("a-1", None)
    assert hooks.count_profile_data(house.a.id) == {"voices": 2}
    assert hooks.purge_profile_data(house.a.id) == 2
    assert "a-1" not in addon.clones and "hh-1" in addon.clones
    hooks.forget("voice")
    assert hooks.voice_owned_clones_active() is False


def test_a_raising_rule_is_a_no_and_a_raising_callback_is_skipped(house):
    def bad(*_a):
        raise RuntimeError("boom")
    hooks.register_voice_access("x", bad)
    hooks.register_profile_data("x", count=bad, purge=bad)
    good = FakeVoiceAddon()
    good.clones["a-9"] = house.a.id
    hooks.register_profile_data("y", count=good.count, purge=good.purge)
    assert hooks.voice_allowed("a-9", house.a.id) is False
    assert hooks.count_profile_data(house.a.id) == {"voices": 1}
    assert hooks.purge_profile_data(house.a.id) == 1


def test_thread_sockets_carry_the_principals_profile_id():
    import inspect

    from app import extensions
    assert "profile_id" in extensions.ThreadSocket.__dataclass_fields__
    src = inspect.getsource(extensions.thread_websocket_endpoint)
    assert "profile_id=(principal.profile_id or None)" in src
