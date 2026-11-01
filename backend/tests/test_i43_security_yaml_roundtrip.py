"""4.3 WP-A1 (H-4, #7): every SecurityConfig field survives a write and a reload, and a
fresh install starts without loopback trust.

Parametrised over ``dataclasses.fields(SecurityConfig)``: a field added later and not wired
into ``_TEMPLATE`` / ``_write`` / ``load`` fails here, instead of being erased by the next
PIN change.
"""
from __future__ import annotations

import dataclasses

import pytest

from app import auth


@pytest.fixture(autouse=True)
def _shipped_fresh_install_default(monkeypatch):
    """conftest models upgraded installs; these tests check the shipped default (False)."""
    from app import auth as _auth
    monkeypatch.setattr(_auth, "FRESH_INSTALL_LOOPBACK_TRUST", False)


#: A non-default value for every field. A new field needs a row here (the test below
#: refuses a field it has no value for).
NON_DEFAULT = {
    "pin_hash": "ab" * 32,
    "salt": "cd" * 16,
    "iterations": 123457,
    "lock_timeout": 777,
    "api_token": "t" * 40,
    "recovery_hash": "ef" * 32,
    "recovery_salt": "01" * 16,
    "remember_days": 12,
    "machine_loopback_trust": False,
    "anonymous_access": "none",
    "machine_scopes": ["machine:inject", "machine:threads"],
    "session_timeouts": {"user": 4321, "basic": 1234},
    "lock_target": "default_profile",
    "default_profile": "p-123",
    "trusted_proxies": ["198.51.100.0/24", "192.0.2.10"],
    # 4.3 Fable: the Host allow-list (policy.host_allowed) and its mode.
    "allowed_hosts": ["chat.example.net", "*.example.org"],
    "host_check": "enforce",
}

FIELDS = [f.name for f in dataclasses.fields(auth.SecurityConfig)]


@pytest.fixture
def sec(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "SECURITY_PATH", tmp_path / "security.yaml")
    monkeypatch.setattr(auth, "RECOVERY_PATH", tmp_path / "RECOVERY-CODE.txt")
    monkeypatch.setattr(auth, "TRUSTED_PATH", tmp_path / "trusted-devices.yaml")
    auth._bust_cache()
    yield tmp_path
    auth._bust_cache()


def _default(f: dataclasses.Field):
    if f.default is not dataclasses.MISSING:
        return f.default
    return f.default_factory()


@pytest.mark.parametrize("name", FIELDS)
def test_l_every_field_round_trips(sec, name):
    assert name in NON_DEFAULT, f"SecurityConfig.{name} has no round-trip value: add one"
    f = next(x for x in dataclasses.fields(auth.SecurityConfig) if x.name == name)
    assert NON_DEFAULT[name] != _default(f)
    cfg = auth.SecurityConfig(**NON_DEFAULT)
    auth._write(cfg)
    auth._bust_cache()
    back = auth.load()
    assert getattr(back, name) == NON_DEFAULT[name]
    # And a second write (what a PIN change does) keeps it too.
    auth._write(back)
    auth._bust_cache()
    assert getattr(auth.load(), name) == NON_DEFAULT[name]


def test_m_no_file_means_no_loopback_trust_and_the_first_write_keeps_it(sec):
    assert not auth.SECURITY_PATH.exists()
    assert auth.load().machine_loopback_trust is False
    auth.set_remember_days(5)              # any first write
    assert "machine_loopback_trust: false" in auth.SECURITY_PATH.read_text()
    auth._bust_cache()
    assert auth.load().machine_loopback_trust is False


def test_g_an_upgraded_file_without_the_key_keeps_trust(sec):
    auth.SECURITY_PATH.write_text("remember_device_days: 3\n")
    assert auth.load().machine_loopback_trust is True
