"""RV-auth (LOW): /api/auth/status still decides `setup_required` with the pre-4.3 peer-only
test, so a loopback peer under a foreign Host (the case #2 closed) is told setup is NOT
required, while every other route answers setup_required."""
from __future__ import annotations

import pytest

from app import policy


@pytest.fixture
def strict(monkeypatch):
    monkeypatch.delenv("DISPATCH_ALLOW_OPEN_INSTALL", raising=False)


def test_status_agrees_with_the_gate_for_a_foreign_host(world, strict):
    c = world.client(("127.0.0.1", 41031), origin=False)
    assert not policy.claimed()
    gate = c.get("/api/settings", headers={"Host": "evil.example"})
    assert gate.json()["error"]["code"] == "setup_required"
    st = c.get("/api/auth/status", headers={"Host": "evil.example"}).json()
    assert st["setup_required"] is True, st
