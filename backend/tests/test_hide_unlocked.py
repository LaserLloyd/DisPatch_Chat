"""2.4.0: `hide_unlocked` — a bot kept off the UNLOCKED rail while locked
(Safe-Mode) devices still list it.

The use case is the kids' science animals: they belong on the family tablet,
not on the owner's own screen. The flag is a display preference, NOT a gate:

* the server still lists the bot to every tier that may see it (the client
  drops it from the unlocked rail, and a device can opt back in), so a thread
  of a hidden bot stays reachable by URL and the Bot Manager can edit it;
* Safe Mode is decided by `safe` exactly as before — `hide_unlocked` never
  makes a non-safe bot visible to a locked device, and never hides a safe one;
* only an unlocked session can change it (the bot-management gate).

Run: cd backend && uv run pytest tests/test_hide_unlocked.py
"""
from __future__ import annotations

import pytest

from app import advisor, config
from tests.test_platform_bots import env

PIN = "1234"


def _unlocked(env):
    c = env(real_defaults=True)
    assert c.post("/api/auth/setup", json={"new_pin": PIN}).status_code == 200
    return c


def _ids(client):
    return [b["id"] for b in client.get("/api/bots").json()["bots"]]


def test_the_flag_defaults_off_and_rides_on_the_public_record():
    b = config.Bot(id="x", name="X")
    assert b.hide_unlocked is False
    assert b.to_dict()["hide_unlocked"] is False
    assert config.Bot(id="y", name="Y", hide_unlocked=True).to_dict()["hide_unlocked"] is True


def test_patch_sets_and_clears_it_and_it_persists(env):
    c = _unlocked(env)
    r = c.patch("/api/bots/atlas", json={"hide_unlocked": True})
    assert r.status_code == 200, r.text
    assert r.json()["hide_unlocked"] is True
    config._invalidate_bots_cache()
    assert config.get_bot("atlas").hide_unlocked is True
    assert "hide_unlocked: true" in (env.tmp_path / "config.yaml").read_text()
    # Leaving it out of a later PATCH leaves it alone.
    c.patch("/api/bots/atlas", json={"name": "Globe"})
    assert config.get_bot("atlas").hide_unlocked is True
    assert c.patch("/api/bots/atlas", json={"hide_unlocked": False}).json()["hide_unlocked"] is False


def test_the_bot_manager_save_carries_it_both_ways(env):
    c = _unlocked(env)
    r = c.put("/api/bots/order", json={"bots": [{"id": "atlas", "order": 0, "hide_unlocked": True}]})
    assert r.status_code == 200, r.text
    assert r.json()["bots"][0]["hide_unlocked"] is True
    # A save that does not mention it keeps it (the reorder from the rail).
    c.put("/api/bots/order", json={"bots": [{"id": "atlas", "order": 0, "visible": True}]})
    assert config.get_bot("atlas").hide_unlocked is True
    c.put("/api/bots/order", json={"bots": [{"id": "atlas", "order": 0, "hide_unlocked": False}]})
    assert config.get_bot("atlas").hide_unlocked is False


def test_create_can_set_it(env):
    c = _unlocked(env)
    r = c.post("/api/bots", json={"id": "kid", "name": "Kid", "safe": True,
                                  "hide_unlocked": True})
    assert r.status_code == 201, r.text
    assert r.json()["hide_unlocked"] is True and r.json()["safe"] is True


@pytest.mark.parametrize("method,path,body", [
    ("PATCH", "/api/bots/atlas", {"hide_unlocked": True}),
    ("PUT", "/api/bots/order", {"bots": [{"id": "atlas", "order": 0, "hide_unlocked": True}]}),
])
def test_a_locked_device_cannot_change_it(env, method, path, body):
    c = _unlocked(env)
    c.cookies.clear()
    r = c.request(method, path, json=body)
    assert r.status_code in (401, 403), r.text
    assert config.get_bot("atlas").hide_unlocked is False


def test_a_null_is_refused_rather_than_guessed(env):
    c = _unlocked(env)
    assert c.patch("/api/bots/atlas", json={"hide_unlocked": None}).status_code == 422


# --------------------------------------------------------------------------- #
# The locked prompt of a bot with nothing to lock (the kids' animals)
# --------------------------------------------------------------------------- #

def _cfg(**kw) -> advisor.AdvisorConfig:
    base = {"bot_id": "kid", "name": "Lulu", "owner": "the child", "persona": "You are {name}.",
                "providers": [{"provider": "custom", "model": "m"}], "knowledge_dir": None,
                "profile_fallback": [], "embeddings": None, "research_agent": "", "action_agent": "",
                "agents": [], "dispatch_timeout_s": 60, "retrieval_chars": 1000, "history_chars": 1000,
                "safe": True, "locked": True}
    base.update(kw)
    return advisor.AdvisorConfig(**base)


def test_a_locked_turn_with_no_agents_and_no_tools_gets_no_safe_mode_paragraph():
    system = advisor.build_system_prompt(_cfg(), "", [], "Saturday")
    assert system.startswith("You are Lulu.")
    assert "Safe Mode" not in system and "marker" not in system
    assert "About the child" not in system


def test_a_locked_turn_with_agents_still_gets_it():
    system = advisor.build_system_prompt(_cfg(agents=["worker"], research_agent="worker"),
                                         "", [], "Saturday")
    assert advisor.LOCKED_RULES.split("{")[0].strip() in system
