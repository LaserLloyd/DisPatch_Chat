"""4.3 Fable (the owner's decision): the shared default profile never has a PIN or password.

Every unsigned device and every lock lands on the default profile. It can already never BE an
Admin or a profile with a credential (``auth.check_lock_target``), and never be promoted to
Admin (``profiles_service._refuse_admin_lock_target``). This closes the last way round: putting
a credential ON the current default profile. One guard in ``accounts.update_profile`` (the
lowest writer of ``pin_hash`` / ``password_hash``) refuses it for every route; the routes answer
409 ``default_profile_no_credential`` with a plain sentence. Invariant I12 reports a default
profile that is disabled, an Admin or has a credential anyway (a hand edit, a restored backup).

Run: cd backend && uv run pytest tests/test_fable_default_profile_credential.py
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import accounts, auth, invariants

CODE = "default_profile_no_credential"
BOSS_PW = "boss-pass-123"   # scrub-ok: throwaway test fixture
SENTENCE = ("The shared default profile cannot have a PIN or password. "
            "Pick another default profile in Settings > People first.")


@pytest.fixture
def lobby(world):
    owner = world.create_owner(world.client())
    p = world.person(owner, "Lobby", "user")
    auth.set_lock_target("default_profile", p["id"])
    return {"owner": owner, "pid": p["id"], "world": world}


def _refused(r):
    assert r.status_code == 409, r.text
    body = r.json()
    assert body["error"]["code"] == CODE, body
    assert SENTENCE in json.dumps(body), body


def _clean(pid):
    p = accounts.get_profile(pid)
    assert p.has_pin is False and p.has_password is False


def test_the_owner_cannot_set_a_pin_on_the_default_profile(lobby):
    _refused(lobby["owner"].post(f"/api/people/{lobby['pid']}/pin", json={"pin": "8642"}))
    _clean(lobby["pid"])


def test_a_second_admin_cannot_set_a_pin_on_the_default_profile(lobby):
    world = lobby["world"]
    world.person(lobby["owner"], "Boss", "admin", password=BOSS_PW)
    boss = world.client(("203.0.113.71", 41000))
    assert world.login(boss, name="Boss", password=BOSS_PW).status_code == 200
    _refused(boss.post(f"/api/people/{lobby['pid']}/pin", json={"pin": "8642"}))
    _clean(lobby["pid"])


def test_no_password_on_the_default_profile(lobby):
    _refused(lobby["owner"].post(f"/api/people/{lobby['pid']}/password",
                                 json={"password": "lobby-pass-123"}))
    _clean(lobby["pid"])


def test_the_default_profile_cannot_set_its_own_pin(lobby):
    from agents_world import sibling
    guest = sibling(lobby["world"], lobby["owner"], ("203.0.113.120", 45000))   # unsigned -> Lobby
    me = guest.get("/api/auth/me").json()
    assert me["profile"]["id"] == lobby["pid"]
    _refused(guest.post("/api/auth/pin", json={"current": "", "new": "9999"}))
    _clean(lobby["pid"])


def test_the_one_guard_holds_below_every_route(lobby):
    """No route can bypass it: the auth setters and the accounts layer itself refuse."""
    pid = lobby["pid"]
    for call in (lambda: auth.set_profile_pin(pid, "8642"),
                 lambda: accounts.update_profile(pid, pin_hash="x" * 40),
                 lambda: accounts.update_profile(pid, password_hash="x" * 40)):
        with pytest.raises(accounts.AccountError) as ei:
            call()
        assert ei.value.code == CODE and ei.value.status == 409
    _clean(pid)
    # Clearing stays allowed (it can only make the profile safer).
    accounts.update_profile(pid, pin_hash=None, password_hash=None)


def test_moving_the_default_away_then_setting_a_pin_works(lobby):
    other = lobby["world"].person(lobby["owner"], "Hall", "user")
    auth.set_lock_target("default_profile", other["id"])
    r = lobby["owner"].post(f"/api/people/{lobby['pid']}/pin", json={"pin": "8642"})
    assert r.status_code == 200, r.text
    assert accounts.get_profile(lobby["pid"]).has_pin is True
    # The profile list as the landing (no default profile in use) allows it too.
    auth.set_lock_target("profile_list", None)
    assert lobby["owner"].post(f"/api/people/{other['id']}/pin", json={"pin": "1357"}).status_code == 200


def test_a_pinned_profile_still_cannot_be_chosen_as_the_default(lobby):
    """The reverse race keeps its old 422 (check_lock_target), and nothing is written."""
    pinned = lobby["world"].person(lobby["owner"], "Kit", "basic", pin="1357")
    with pytest.raises(accounts.AccountError) as ei:
        auth.set_lock_target("default_profile", pinned["id"])
    assert ei.value.code == "lock_target_invalid" and ei.value.status == 422
    r = lobby["owner"].patch("/api/settings", json={"set": {"people.default_profile": pinned["id"]}})
    assert r.status_code == 422, r.text
    assert auth.load().default_profile == lobby["pid"]


def test_i12_is_clean_on_a_seeded_install_and_fires_on_a_corrupted_row(lobby):
    assert "I12" in invariants.INVARIANTS
    assert not [v for v in invariants.check() if v.code == "I12"]
    body = lobby["owner"].get("/api/health/invariants").json()
    assert body["by_invariant"]["I12"] == 0, body
    # A hand edit straight into the accounts database (the guard is above it).
    with accounts.connect() as con:
        con.execute("UPDATE profiles SET pin_hash = ? WHERE id = ?", ("corrupt", lobby["pid"]))
    accounts.invalidate()
    vs = [v for v in invariants.check() if v.code == "I12"]
    assert [v.subject for v in vs] == [lobby["pid"]], vs
    body = lobby["owner"].get("/api/health/invariants").json()
    assert body["ok"] is False and body["by_invariant"]["I12"] == 1, body


def test_i12_seeded_worlds():
    from types import SimpleNamespace as P

    from app.contracts.permissions import Role

    def prof(pid, role=Role.USER, pin=False, pw=False, disabled=False):
        return P(id=pid, role=role, has_pin=pin, has_password=pw, disabled=disabled, protected=False,
                 household_bots="all")

    def run(profiles, target="default_profile", default="d"):
        w = invariants.World(profiles=profiles,
                             extra={"lock_target": {"target": target, "default_profile": default}})
        return [v for v in invariants.check(w) if v.code == "I12"]

    assert run([prof("d")]) == []
    assert run([prof("d", pin=True)], target="profile_list") == []        # not in use
    assert len(run([prof("d", pin=True)])) == 1
    assert len(run([prof("d", pw=True)])) == 1
    assert len(run([prof("d", role=Role.ADMIN)])) == 1
    assert len(run([prof("d", disabled=True)])) == 1
    assert len(run([])) == 1                                              # names a missing profile


def test_the_error_code_is_in_the_catalog():
    cat = json.loads((Path(__file__).resolve().parents[1] / "app/contracts/error_catalog.json")
                     .read_text(encoding="utf-8"))
    rows = cat if isinstance(cat, list) else cat.get("errors", cat)
    row = next(r for r in rows if r["code"] == CODE)
    assert row["status"] == 409 and row["message"] == SENTENCE
