"""2.4.2: one visibility model — "Show locked" and "Show unlocked".

Each bot carries two independent flags, set in Settings -> Bots:

* ``safe``          = Show locked. The Safe-Mode gate, unchanged in meaning: a
                      locked device lists and reaches ONLY these bots.
* ``hide_unlocked`` = NOT Show unlocked (2.4.0's flag). A display preference
                      on unlocked devices; the server still serves the bot to
                      an unlocked session so its threads open by URL.

Neither on = the bot shows nowhere, but still exists and stays editable.

The old ``visible`` master switch is folded into the two: ``visible: false``
reads as "neither" (safe off, hide_unlocked on), and ``visible`` survives only
as a derived, read-only field on the wire (listed on SOME device), so older
clients and the voice add-on keep working.

Run: cd backend && uv run pytest tests/test_bot_visibility.py
"""
from __future__ import annotations

import pytest

from app import config, extensions, main
from tests.test_platform_bots import env

PIN = "1234"


def _unlocked(env):
    c = env(real_defaults=True)
    assert c.post("/api/auth/setup", json={"new_pin": PIN}).status_code == 200
    return c


def _ids(client):
    return {b["id"] for b in client.get("/api/bots").json()["bots"]}


def _write(env, body: str) -> None:
    (env.tmp_path / "config.yaml").write_text(body)
    config._invalidate_bots_cache()


ROSTER = """bots:
- {id: atlas, name: Atlas, safe: true}
- {id: kid, name: Kid, safe: true, hide_unlocked: true}
- {id: main, name: Bits}
- {id: ghost, name: Ghost, visible: false}
- {id: oldsafe, name: Old safe, visible: false, safe: true}
"""


# --------------------------------------------------------------------------- #
# The model
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("safe,hide,listed", [
    (True, False, True), (True, True, True), (False, False, True), (False, True, False)])
def test_visible_is_derived_from_the_two_flags(safe, hide, listed):
    b = config.Bot(id="x", name="X", safe=safe, hide_unlocked=hide)
    assert b.visible is listed
    assert b.to_dict()["visible"] is listed


def test_visible_false_in_config_yaml_migrates_to_neither(env):
    env(real_defaults=True)
    _write(env, ROSTER)
    bots = {b.id: b for b in config.load_bots()}
    assert (bots["ghost"].safe, bots["ghost"].hide_unlocked) == (False, True)
    # `visible: false` + `safe: true` was shown nowhere before; it must not
    # start showing on locked devices now.
    assert (bots["oldsafe"].safe, bots["oldsafe"].hide_unlocked) == (False, True)
    # Everything else keeps what it shows today.
    assert (bots["atlas"].safe, bots["atlas"].hide_unlocked) == (True, False)
    assert (bots["kid"].safe, bots["kid"].hide_unlocked) == (True, True)
    assert (bots["main"].safe, bots["main"].hide_unlocked) == (False, False)


def test_the_writer_drops_the_old_key_and_a_reload_round_trips(env):
    env(real_defaults=True)
    _write(env, ROSTER)
    config.save_bot_order([{"id": b.id, "order": i} for i, b in enumerate(config.load_bots())])
    text = (env.tmp_path / "config.yaml").read_text(encoding="utf-8")
    assert "visible" not in text
    bots = {b.id: b for b in config.load_bots()}
    assert (bots["ghost"].safe, bots["ghost"].hide_unlocked) == (False, True)
    assert (bots["kid"].safe, bots["kid"].hide_unlocked) == (True, True)


# --------------------------------------------------------------------------- #
# Who sees what
# --------------------------------------------------------------------------- #

def test_extension_locked_gate_follows_safe_alone():
    assert extensions.locked_may_see(config.Bot(id="a", name="A", safe=True, hide_unlocked=True))
    assert not extensions.locked_may_see(config.Bot(id="b", name="B", hide_unlocked=True))
    assert not extensions.locked_may_see(config.Bot(id="c", name="C"))


# --------------------------------------------------------------------------- #
# Writers: the two flags, and the old `visible` key from older clients
# --------------------------------------------------------------------------- #

def test_the_bot_manager_save_sets_each_combination(env):
    c = _unlocked(env)
    for safe, hide in [(False, True), (True, True), (False, False), (True, False)]:
        r = c.put("/api/bots/order", json={"bots": [
            {"id": "atlas", "order": 0, "safe": safe, "hide_unlocked": hide}]})
        assert r.status_code == 200, r.text
        row = r.json()["bots"][0]
        assert (row["safe"], row["hide_unlocked"], row["visible"]) == (safe, hide, safe or not hide)


def test_an_old_client_visible_false_means_neither_and_true_changes_nothing(env):
    c = _unlocked(env)
    c.put("/api/bots/order", json={"bots": [{"id": "atlas", "order": 0, "visible": False}]})
    a = config.get_bot("atlas")
    assert (a.safe, a.hide_unlocked) == (False, True)
    # The rail's reorder used to send visible: true for every bot it held; that
    # must never un-hide a bot.
    c.put("/api/bots/order", json={"bots": [{"id": "atlas", "order": 0, "visible": True}]})
    a = config.get_bot("atlas")
    assert (a.safe, a.hide_unlocked) == (False, True)


def test_patch_and_create_fold_visible_false(env):
    c = _unlocked(env)
    r = c.patch("/api/bots/atlas", json={"visible": False})
    assert r.status_code == 200, r.text
    assert (r.json()["safe"], r.json()["hide_unlocked"]) == (False, True)
    r = c.post("/api/bots", json={"id": "n", "name": "N", "safe": True, "visible": False})
    assert r.status_code == 201, r.text
    assert (r.json()["safe"], r.json()["hide_unlocked"]) == (False, True)
    c.cookies.clear()
    assert c.get("/api/bots").status_code == 401           # 4.0: signed out = nobody


def test_a_neither_bots_thread_is_reachable_unlocked_only(env):
    c = _unlocked(env)
    _write(env, ROSTER)
    r = c.post("/api/threads", json={"bot_id": "ghost", "title": "old chat"})
    assert r.status_code in (200, 201), r.text
    tid = r.json()["id"]
    assert c.get(f"/api/threads/{tid}/messages").status_code == 200
    c.cookies.clear()
    assert c.get(f"/api/threads/{tid}/messages").status_code in (401, 403, 404)
