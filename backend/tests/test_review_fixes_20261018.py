"""Fixes from the 3.0.0 independent review (2026-10-18).

1. A profile cannot grant itself permissions: a Member holding ``manage_users`` could PATCH
   its own ``grants`` (host control included), bind its device to any Member's profile and
   forget anyone's device. ``admin_system`` is Owner-only for every role, in the matrix and
   in the API. (4.0: ``manage_users`` is in a User's "never" column, so the grant itself is
   refused now; the checks below keep the escalation closed from both ends.)
2. A connection's redirects get the same SSRF guard as the typed address.
Run: cd backend && uv run pytest tests/test_review_fixes_20261018.py
"""
from __future__ import annotations

import dataclasses

import httpx
import pytest

from app import accounts, connections, llm_api, policy

LAN = ("203.0.113.70", 42000)


@pytest.fixture
def family(world):
    owner = world.create_owner(world.client())
    r = owner.post("/api/people", json={"name": "Max", "role": "member", "pin": "5555",
                                        "grants": ["manage_users"]})
    assert r.status_code == 422 and r.json()["error"]["code"] == "permissions_invalid", r.text
    maxp = world.person(owner, "Max", "member", pin="5555")
    eve = world.person(owner, "Eve", "member", pin="6666")
    max_c = world.client(LAN)
    assert world.login(max_c, name="Max", pin="5555").status_code == 200
    assert "manage_users" not in max_c.get("/api/auth/me").json()["permissions"]
    return {"owner": owner, "max": max_c, "max_id": maxp["id"], "eve_id": eve["id"]}


def test_a_member_cannot_grant_itself_anything(family):
    r = family["max"].patch(f"/api/people/{family['max_id']}",
                            json={"grants": ["admin_system", "manage_settings", "dispatch_agents"]})
    assert r.status_code == 403, r.text
    assert r.json()["error"]["code"] == "permission_denied"
    for body in ({"denies": []}, {"budgets": {"turns": 99999}}, {"session_timeout": 999999}):
        assert family["max"].patch(f"/api/people/{family['max_id']}", json=body).status_code == 403, body
    me = family["max"].get("/api/auth/me").json()["permissions"]
    assert "admin_system" not in me and "manage_settings" not in me
    assert family["max"].get("/api/dashboard").status_code == 403


def test_host_control_cannot_be_granted_below_admin(family):
    r = family["owner"].patch(f"/api/people/{family['max_id']}", json={"grants": ["admin_system"]})
    assert r.status_code == 422 and r.json()["error"]["code"] == "permissions_invalid"
    # And a grant that reaches the database some other way is still not effective.
    prof = accounts.get_profile(family["max_id"])
    forged = dataclasses.replace(prof, grants=["admin_system", "manage_settings"])
    perms = policy.effective_permissions(forged)
    assert "admin_system" not in perms and "manage_settings" not in perms   # 4.0: never for a User
    admin = dataclasses.replace(prof, role="admin", grants=[])
    assert "admin_system" in policy.effective_permissions(admin)   # an Admin holds it by role


def test_device_binding_is_gone_and_a_member_cannot_forget_other_devices(family, world):
    # 4.0: the bind route is gone for everyone.
    assert family["max"].put("/api/people/devices/this", json={"profile_id": family["eve_id"]}).status_code in (403, 404, 405)
    assert family["owner"].put("/api/people/devices/this", json={"profile_id": None}).status_code in (404, 405)
    from app import accounts
    accounts.ensure_device("e" * 64)
    assert family["max"].delete(f"/api/people/devices/{'e' * 64}").status_code == 403
    assert family["owner"].delete(f"/api/people/devices/{'e' * 64}").status_code == 200


def test_the_owner_and_an_admin_still_manage_members(family):
    owner = family["owner"]
    assert owner.patch(f"/api/people/{family['eve_id']}", json={"grants": ["dispatch_agents"],
                                                                 "budgets": {"turns": 5}}).status_code == 200


# --------------------------------------------------------------------------- #
# Redirects are guarded like the typed address
# --------------------------------------------------------------------------- #


def test_a_redirect_to_a_blocked_address_is_refused_and_never_fetched(world, monkeypatch):
    world.create_owner()
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.url.host)
        if req.url.host == "provider.invalid":
            return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data/"})
        return httpx.Response(200, json={"data": [{"id": "leaked"}]})

    def factory(read_timeout: float = llm_api.READ_TIMEOUT_S) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=read_timeout,
                                 follow_redirects=False)
    monkeypatch.setattr(llm_api, "http_client", factory)
    import asyncio
    out = asyncio.run(connections.run_test(type="openai_compatible", base_url="http://provider.invalid/v1",
                                           api_key="k"))
    assert out["ok"] is False, out
    assert "blocked" in (out["message"] + out["fix"]).lower(), out
    assert "leaked" not in str(out)
    assert "169.254.169.254" not in seen
