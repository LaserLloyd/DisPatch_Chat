"""POST /api/llm/connect (backend "direct") goes through a household Connection (4.0).

The 3.x route wrote an inline ``advisor.providers`` entry with the API key into config.yaml.
Personal bots and copies cannot use an inline block, so on an install set up only that way a
User's bots stayed unconnected. Now Connect finds or creates the Connection (the key in the
secret store) and points the bot at it, the way the setup wizard and the Connections page do.
The request and response shapes are unchanged.
"""
from __future__ import annotations

from app import characters, config, connections, firstrun


def _personal_atlases(pid: str) -> list[config.Bot]:
    return [b for b in characters.personal_bots(pid) if b.pack == firstrun.ATLAS_PACK]


def test_connect_creates_one_household_connection_and_reuses_it(world):
    owner = world.create_owner()
    r = owner.post("/api/llm/connect", json={"provider": "openai", "model": "gpt-4o",
                                             "api_key": "sk-test-first", "target": "helper_a",
                                             "name": "Helper A"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "sk-test-first" not in r.text
    assert body["id"] == "helper_a" and body["bot"]["id"] == "helper_a" and body["connected"] is True
    cid = config.get_bot("helper_a").advisor["providers"][0]["connection"]
    assert [c.id for c in connections.list_all()] == [cid]
    assert connections.default_id() == cid                 # the first one is the default
    assert "sk-test-first" not in (world.tmp / "config.yaml").read_text()
    # The same key for a second bot: the same connection, not a second copy of the key.
    r = owner.post("/api/llm/connect", json={"provider": "openai", "model": "gpt-4o-mini",
                                             "api_key": "sk-test-first", "target": "helper_b"})
    assert r.status_code == 200, r.text
    assert config.get_bot("helper_b").advisor["providers"][0]["connection"] == cid
    # A re-save from the panel sends no key: the bot keeps its connection.
    r = owner.post("/api/llm/connect", json={"provider": "openai", "model": "gpt-4o",
                                             "target": "helper_b"})
    assert r.status_code == 200, r.text
    assert config.get_bot("helper_b").advisor["providers"][0]["connection"] == cid
    # Another key is another account: a second connection.
    owner.post("/api/llm/connect", json={"provider": "openai", "model": "gpt-4o",
                                         "api_key": "sk-test-other", "target": "helper_c"})
    other = config.get_bot("helper_c").advisor["providers"][0]["connection"]
    assert other != cid and len(connections.list_all()) == 2
    assert connections.credential(connections.get(other)) == "sk-test-other"


def test_a_users_own_atlas_is_connected_by_connect(world):
    owner = world.create_owner()
    uma = world.person(owner, "Uma", "user", pin="2222")
    (atlas,) = _personal_atlases(uma["id"])                # made at creation, no connection yet
    assert not characters.connection_of(atlas)
    r = owner.post("/api/llm/connect", json={"provider": "openai", "model": "gpt-4o",
                                             "api_key": "sk-test-house", "target": "helper_a"})
    assert r.status_code == 200, r.text
    (atlas,) = _personal_atlases(uma["id"])
    cid = characters.connection_of(atlas)
    assert cid and cid == connections.default_id()
    assert connections.credential(connections.get(cid)) == "sk-test-house"


def test_connect_cannot_reach_a_personal_bot(world):
    owner = world.create_owner()
    uma = world.person(owner, "Uma", "user", pin="2222")
    (atlas,) = _personal_atlases(uma["id"])
    r = owner.post("/api/llm/connect", json={"provider": "openai", "model": "gpt-4o",
                                             "api_key": "sk-test-x", "target": atlas.id})
    assert r.status_code == 404
    assert config.get_bot(atlas.id).advisor == atlas.advisor          # untouched


def test_a_needed_key_is_still_required(world):
    owner = world.create_owner()
    r = owner.post("/api/llm/connect", json={"provider": "openai", "model": "gpt-4o",
                                             "target": "helper_a"})
    assert r.status_code == 400 and "needs an API key" in r.text
    assert connections.list_all() == []
