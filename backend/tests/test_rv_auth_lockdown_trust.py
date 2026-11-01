"""RV-auth: a corrupt security.yaml is FAIL CLOSED for browsers, but its lockdown config keeps
SecurityConfig's default machine_loopback_trust=True and machine_scopes=None, so every keyless
on-box process becomes a machine with every default scope - even on an install whose file said
machine_loopback_trust: false (R1.5 / 4.3 #7)."""
from __future__ import annotations

import pytest

from app import auth


@pytest.fixture(autouse=True)
def _shipped(monkeypatch):
    monkeypatch.setattr(auth, "FRESH_INSTALL_LOOPBACK_TRUST", False)


def test_lockdown_does_not_grant_loopback_machine_trust(world):
    owner = world.create_owner()
    owner.post("/api/auth/api-token", json={"rotate": True})
    assert auth.load().machine_loopback_trust is False
    local = world.client(("127.0.0.1", 41019), origin=False)
    body = {"bot_id": "alpha", "role": "user", "content": "hi"}
    assert local.post("/api/inject", json=body).status_code == 401        # control
    auth.SECURITY_PATH.write_text("pin_hash: [unclosed\n")                 # corrupt file
    auth._bust_cache()
    cfg = auth.load()
    assert cfg.pin_hash == auth._LOCKDOWN_HASH
    assert local.post("/api/inject", json=body).status_code == 401
