"""4.4 lock, resume and "keep this device unlocked" (docs/design/lock-resume.md).

Part 1: a restart no longer signs anybody out; a revoked session never comes back.
Part 2: a lock HOLDS the session (nobody on every route) until its credential resumes it,
and the locked device learns one bit (resumable) and never whose session it is.
"""
from __future__ import annotations

import time

import pytest

from app import accounts, auth, settings_store

OWNER_PIN = "2468"


@pytest.fixture
def owner(world):
    return world.create_owner()


def _restart():
    """A server restart: in-memory sessions and caches gone, disk intact."""
    auth._sessions.clear()
    auth._trusted = None
    auth._cache = None
    auth._buckets.clear()


def _sess(profile_id):
    return next(s for s in auth._sessions.values() if s.profile_id == profile_id)


def _idle(profile_id, seconds):
    _sess(profile_id).last_seen -= seconds


def _st(c):
    return c.get("/api/auth/status").json()


def _mia(world, owner, **kw):
    return world.person(owner, "Mia", "member", pin="1357", **kw)


# ----------------------------------------------------------------- Part 1: restarts


def test_a_plain_session_survives_a_restart(world, owner):
    mia = _mia(world, owner)
    c = world.client()
    assert world.login(c, profile_id=mia["id"], pin="1357").status_code == 200
    _restart()
    st = _st(c)
    assert st["signed_in"] is True and st["profile"]["id"] == mia["id"]


def test_a_restart_keeps_the_idle_clock(world, owner):
    assert settings_store.set_many({"people.session_timeout_user": 3600}).ok
    mia = _mia(world, owner)
    c = world.client()
    world.login(c, profile_id=mia["id"], pin="1357")
    th = auth._token_hash(_sess(mia["id"]).token)
    old = auth._iso(time.time() - 4000)
    accounts.update_session_row(th, last_seen=old)
    _restart()
    st = _st(c)
    assert st["signed_in"] is False and st["resumable"] is True   # idle -> held, not live


@pytest.mark.parametrize("how", ["pin_change", "sign_out_everywhere", "disable"])
def test_a_revoked_session_never_comes_back_after_a_restart(world, owner, how):
    mia = _mia(world, owner)
    c = world.client()
    world.login(c, profile_id=mia["id"], pin="1357")
    if how == "pin_change":
        accounts.update_profile(mia["id"], pin_hash=auth.credentials.hash_secret("9999"))
    elif how == "sign_out_everywhere":
        auth.revoke_profile(mia["id"])
    else:
        accounts.update_profile(mia["id"], disabled=True)
    _restart()
    assert _st(c)["signed_in"] is False
    assert _st(c)["resumable"] is False


def test_a_pre_44_row_is_not_reminted(world, owner):
    mia = _mia(world, owner)
    c = world.client()
    world.login(c, profile_id=mia["id"], pin="1357")
    th = auth._token_hash(_sess(mia["id"]).token)
    with accounts.connect() as con:
        con.execute("UPDATE sessions SET epoch = NULL WHERE token_hash = ?", (th,))
    _restart()
    assert _st(c)["signed_in"] is False


def test_lockdown_drops_the_rows_too(world, owner):
    c = world.client()
    world.login(c, profile_id=accounts.get_owner().id, pin=OWNER_PIN)
    auth.SECURITY_PATH.write_text("pin: [unclosed\n\t:::")
    auth._bust_cache()
    auth.load()
    assert accounts.list_sessions() == []


# ----------------------------------------------------------------- Part 2: hold / resume


def test_lock_holds_and_resume_brings_the_same_session_back(world, owner):
    mia = _mia(world, owner)
    c = world.client()
    world.login(c, profile_id=mia["id"], pin="1357")
    tok = _sess(mia["id"]).token
    assert c.post("/api/auth/lock").json()["ok"] is True
    st = _st(c)
    assert st["signed_in"] is False and st["resumable"] is True
    # nothing of hers answers while held
    assert c.get("/api/me/characters").status_code in (401, 403)
    r = c.post("/api/auth/resume", json={"secret": "1357"})
    assert r.status_code == 200 and r.json()["ok"] is True
    st = _st(c)
    assert st["signed_in"] is True and st["profile"]["id"] == mia["id"]
    assert _sess(mia["id"]).token == tok                  # the same session, not a new one


def test_the_locked_status_never_names_the_held_profile(world, owner):
    world.person(owner, "Zebedee", "member", pin="1357")
    c = world.client()
    zid = accounts.get_by_name("Zebedee").id
    world.login(c, profile_id=zid, pin="1357")
    c.post("/api/auth/lock")
    st = _st(c)
    st.pop("profiles", None)          # the public tile list names everyone, as before
    raw = repr(st)
    assert "Zebedee" not in raw and zid not in raw
    bad = c.post("/api/auth/resume", json={"secret": "0000"})
    assert "Zebedee" not in bad.text and zid not in bad.text


def test_wrong_secret_and_nothing_to_resume_answer_the_same(world, owner):
    mia = _mia(world, owner)
    c = world.client()
    world.login(c, profile_id=mia["id"], pin="1357")
    c.post("/api/auth/lock")
    wrong = c.post("/api/auth/resume", json={"secret": "0000"})
    stranger = world.client(("203.0.113.77", 1))
    none = stranger.post("/api/auth/resume", json={"secret": "0000"})
    assert wrong.status_code == none.status_code == 401
    assert wrong.json() == none.json()
    assert _st(c)["resumable"] is True                     # a wrong try leaves the hold


def test_five_wrong_resumes_drop_the_hold_and_never_touch_the_profile_bucket(world, owner):
    mia = _mia(world, owner)
    c = world.client()
    world.login(c, profile_id=mia["id"], pin="1357")
    c.post("/api/auth/lock")
    for _ in range(auth.RESUME_MAX_FAILS):
        r = c.post("/api/auth/resume", json={"secret": "0000"})
        assert r.status_code in (401, 429)
    auth._buckets.clear()                       # the cooldown passed
    assert _st(c)["resumable"] is False
    assert c.post("/api/auth/resume", json={"secret": "1357"}).status_code == 401
    # Her own tile still signs in at once: the resume attempts never charged her bucket.
    assert world.login(c, profile_id=mia["id"], pin="1357").status_code == 200


def test_tile_failures_do_not_change_the_resume_offer(world, owner):
    mia = _mia(world, owner)
    c = world.client()
    world.login(c, profile_id=mia["id"], pin="1357")
    c.post("/api/auth/lock")
    for _ in range(4):
        world.login(c, profile_id=mia["id"], pin="0000")
    assert _st(c)["resumable"] is True


def test_a_pinless_hold_is_never_offered(world, owner):
    kid = world.person(owner, "Pip", "member")
    c = world.client()
    world.login(c, profile_id=kid["id"])
    c.post("/api/auth/lock")
    assert _st(c)["resumable"] is False
    assert c.post("/api/auth/resume", json={"secret": ""}).status_code == 401


def test_the_owner_resumes_with_pin_or_password(world, owner):
    c = world.client()
    oid = accounts.get_owner().id
    world.login(c, profile_id=oid, pin=OWNER_PIN)
    c.post("/api/auth/lock")
    assert c.post("/api/auth/resume", json={"secret": "owner-pass1"}).status_code == 200
    c.post("/api/auth/lock")
    assert c.post("/api/auth/resume", json={"secret": OWNER_PIN}).status_code == 200


def test_another_profile_cannot_resume_the_first(world, owner):
    mia = _mia(world, owner)
    world.person(owner, "Ben", "member", pin="8642")
    c = world.client()
    world.login(c, profile_id=mia["id"], pin="1357")
    c.post("/api/auth/lock")
    assert c.post("/api/auth/resume", json={"secret": "8642"}).status_code == 401
    # Ben signs in normally: Mia's hold is replaced and gone for good.
    assert world.login(c, name="Ben", pin="8642").status_code == 200
    c.post("/api/auth/lock")
    assert c.post("/api/auth/resume", json={"secret": "1357"}).status_code == 401
    assert c.post("/api/auth/resume", json={"secret": "8642"}).status_code == 200


def test_a_hold_runs_out(world, owner):
    mia = _mia(world, owner)
    c = world.client()
    world.login(c, profile_id=mia["id"], pin="1357")
    c.post("/api/auth/lock")
    _sess(mia["id"]).locked_at -= auth.HOLD_SECONDS + 5
    assert _st(c)["resumable"] is False
    assert c.post("/api/auth/resume", json={"secret": "1357"}).status_code == 401


def test_idle_is_held_not_dropped(world, owner):
    assert settings_store.set_many({"people.session_timeout_user": 3600}).ok
    mia = _mia(world, owner)
    c = world.client()
    world.login(c, profile_id=mia["id"], pin="1357")
    _idle(mia["id"], 3700)
    assert _st(c)["signed_in"] is False and _st(c)["resumable"] is True


def test_a_held_session_is_no_use_and_no_privilege(world, owner):
    auth.set_remember_days(30)
    mia = _mia(world, owner)
    c = world.client()
    c.post("/api/auth/login", json={"profile_id": mia["id"], "pin": "1357", "remember": True})
    tok = _sess(mia["id"]).token
    c.post("/api/auth/lock")
    before = _sess(mia["id"]).last_seen
    auth.touch_session(tok)
    assert _sess(mia["id"]).last_seen == before
    assert auth.remembered_for(mia["id"], session_token=tok) is False


def test_idle_lock_is_refused_while_the_session_is_in_use(world, owner):
    mia = _mia(world, owner)
    c = world.client()
    world.login(c, profile_id=mia["id"], pin="1357")
    r = c.post("/api/auth/lock", json={"reason": "idle"})
    assert r.json()["ok"] is False and r.json()["active"] is True
    assert _st(c)["signed_in"] is True
    # a manual lock is never refused
    assert c.post("/api/auth/lock", json={"reason": "manual"}).json()["ok"] is True
    assert _st(c)["signed_in"] is False


# ----------------------------------------------------------------- keep this device unlocked


def test_an_owner_remembered_device_keeps_itself_unlocked_by_default(world, owner):
    auth.set_remember_days(30)
    c = world.client()
    oid = accounts.get_owner().id
    c.post("/api/auth/login", json={"profile_id": oid, "pin": OWNER_PIN, "remember": True})
    st = _st(c)
    assert st["keep_unlocked"] is True and st["idle_timeout_seconds"] == 0
    _idle(oid, 10 ** 6)
    _restart()
    st = _st(c)
    assert st["signed_in"] is True and st["keep_unlocked"] is True


def test_turning_keep_unlocked_off_brings_the_idle_lock_back(world, owner):
    auth.set_remember_days(30)
    c = world.client()
    oid = accounts.get_owner().id
    c.post("/api/auth/login", json={"profile_id": oid, "pin": OWNER_PIN, "remember": True})
    r = c.post("/api/auth/keep-unlocked", json={"on": False})
    assert r.status_code == 200 and r.json()["keep_unlocked"] is False
    st = _st(c)
    assert st["keep_unlocked"] is False and st["idle_timeout_seconds"] > 0 and st["remembered"] is True
    _restart()
    assert _st(c)["keep_unlocked"] is False
    _idle(oid, 10 ** 4)
    assert _st(c)["signed_in"] is False and _st(c)["resumable"] is True


def test_basic_never_keeps_a_device_unlocked(world, owner):
    auth.set_remember_days(30)
    kid = world.person(owner, "Kid", "basic", pin="1111")
    c = world.client()
    c.post("/api/auth/login", json={"profile_id": kid["id"], "pin": "1111", "remember": True})
    st = _st(c)
    assert st["remembered"] is True and st["keep_unlocked"] is False
    assert st["keep_unlocked_allowed"] is False and st["idle_timeout_seconds"] > 0
    assert c.post("/api/auth/keep-unlocked", json={"on": True}).status_code == 403


def test_turning_it_on_for_a_plain_session_needs_the_credential(world, owner):
    auth.set_remember_days(30)
    mia = _mia(world, owner)
    c = world.client()
    world.login(c, profile_id=mia["id"], pin="1357")
    _sess(mia["id"]).issued -= auth.FRESH_SIGNIN_SECONDS + 1       # not right after sign-in
    assert c.post("/api/auth/keep-unlocked", json={"on": True}).status_code == 401
    assert c.post("/api/auth/keep-unlocked", json={"on": True, "secret": "0000"}).status_code == 401
    r = c.post("/api/auth/keep-unlocked", json={"on": True, "secret": "1357"})
    assert r.status_code == 200 and r.json() == {"ok": True, "keep_unlocked": True, "remembered": True}
    assert "Max-Age" in r.headers.get("set-cookie", "")


def test_remember_off_install_wide_exempts_nobody(world, owner):
    auth.set_remember_days(30)
    c = world.client()
    oid = accounts.get_owner().id
    c.post("/api/auth/login", json={"profile_id": oid, "pin": OWNER_PIN, "remember": True})
    auth.set_remember_days(0)
    assert _st(c)["keep_unlocked"] is False


def test_status_slides_a_remembered_cookie(world, owner):
    auth.set_remember_days(30)
    c = world.client()
    oid = accounts.get_owner().id
    c.post("/api/auth/login", json={"profile_id": oid, "pin": OWNER_PIN, "remember": True})
    r = c.get("/api/auth/status")
    assert f"Max-Age={30 * 86400}" in r.headers.get("set-cookie", "")


def test_status_does_not_set_a_cookie_for_a_plain_session(world, owner):
    mia = _mia(world, owner)
    c = world.client()
    world.login(c, profile_id=mia["id"], pin="1357")
    assert "set-cookie" not in c.get("/api/auth/status").headers


def test_right_after_a_sign_in_the_offer_needs_no_second_credential(world, owner):
    auth.set_remember_days(30)
    mia = _mia(world, owner)
    c = world.client()
    world.login(c, profile_id=mia["id"], pin="1357")
    r = c.post("/api/auth/keep-unlocked", json={"on": True})
    assert r.status_code == 200 and r.json()["remembered"] is True
    _restart()
    st = _st(c)
    assert st["signed_in"] is True and st["keep_unlocked"] is True


def test_a_reminted_session_is_never_fresh(world, owner):
    auth.set_remember_days(30)
    mia = _mia(world, owner)
    c = world.client()
    world.login(c, profile_id=mia["id"], pin="1357")
    _restart()
    assert c.post("/api/auth/keep-unlocked", json={"on": True}).status_code == 401


# ----------------------------------------------------------------- 4.4 security review


def test_a_remembered_device_turned_off_needs_the_credential_to_turn_it_back_on(world, owner):
    auth.set_remember_days(30)
    c = world.client()
    oid = accounts.get_owner().id
    c.post("/api/auth/login", json={"profile_id": oid, "pin": OWNER_PIN, "remember": True})
    assert c.post("/api/auth/keep-unlocked", json={"on": False}).json()["keep_unlocked"] is False
    mine = next(s for s in auth._sessions.values() if s.profile_id == oid and s.persistent)
    mine.issued -= auth.FRESH_SIGNIN_SECONDS + 1          # someone picks the phone up later
    assert c.post("/api/auth/keep-unlocked", json={"on": True}).status_code == 401
    assert _st(c)["keep_unlocked"] is False
    r = c.post("/api/auth/keep-unlocked", json={"on": True, "secret": OWNER_PIN})
    assert r.status_code == 200 and r.json()["keep_unlocked"] is True


def test_asking_without_a_secret_is_not_a_failed_attempt(world, owner):
    auth.set_remember_days(30)
    mia = _mia(world, owner)
    c = world.client()
    world.login(c, profile_id=mia["id"], pin="1357")
    _sess(mia["id"]).issued -= auth.FRESH_SIGNIN_SECONDS + 1
    for _ in range(8):
        assert c.post("/api/auth/keep-unlocked", json={"on": True}).status_code == 401
    assert not auth._buckets                      # nothing charged anywhere
    assert world.login(world.client(), profile_id=mia["id"], pin="1357").status_code == 200


def test_wrong_resumes_are_counted_across_a_restart(world, owner):
    mia = _mia(world, owner)
    c = world.client()
    world.login(c, profile_id=mia["id"], pin="1357")
    c.post("/api/auth/lock")
    for _ in range(auth.RESUME_MAX_FAILS - 1):
        c.post("/api/auth/resume", json={"secret": "0000"})
    _restart()                                   # buckets and memory gone
    assert _st(c)["resumable"] is True
    c.post("/api/auth/resume", json={"secret": "0000"})
    auth._buckets.clear()
    assert _st(c)["resumable"] is False
    assert c.post("/api/auth/resume", json={"secret": "1357"}).status_code == 401


def test_a_lock_that_cannot_be_written_ends_the_session(world, owner, monkeypatch):
    mia = _mia(world, owner)
    c = world.client()
    world.login(c, profile_id=mia["id"], pin="1357")

    def boom(*a, **k):
        raise RuntimeError("database is locked")
    with monkeypatch.context() as m:
        m.setattr(accounts, "update_session_row", boom)
        c.post("/api/auth/lock")
    assert not any(s.profile_id == mia["id"] for s in auth._sessions.values())
    _restart()
    st = _st(c)
    assert st["signed_in"] is False and st["resumable"] is False


def test_sign_out_everywhere_holds_even_if_the_row_delete_is_lost(world, owner, monkeypatch):
    mia = _mia(world, owner)
    c = world.client()
    world.login(c, profile_id=mia["id"], pin="1357")
    with monkeypatch.context() as m:
        m.setattr(accounts, "drop_profile_sessions", lambda pid: 0)
        auth.revoke_profile(mia["id"])
    assert accounts.list_sessions(mia["id"])     # the row is still there ...
    _restart()
    assert _st(c)["signed_in"] is False           # ... and still never comes back


def test_a_restart_keeps_the_idle_clock_of_a_remembered_device_that_locks(world, owner):
    auth.set_remember_days(30)
    assert settings_store.set_many({"people.session_timeout_user": 3600}).ok
    mia = _mia(world, owner)
    c = world.client()
    c.post("/api/auth/login", json={"profile_id": mia["id"], "pin": "1357", "remember": True})
    assert c.post("/api/auth/keep-unlocked", json={"on": False}).status_code == 200
    th = auth._token_hash(_sess(mia["id"]).token)
    accounts.update_session_row(th, last_seen=auth._iso(time.time() - 4000))
    _restart()
    st = _st(c)
    assert st["signed_in"] is False and st["resumable"] is True
