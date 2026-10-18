"""Second-review fixes (Opus, 2026-10-18), one test per finding.

O-1  a Member socket received operator frames (harness, pool telemetry, agent progress) and
     reaction / progress frames about characters made for someone else
O-2  per-profile character visibility held on the roster but not on the thread REST routes:
     /api/unread listed a hidden character's thread ids, and a Member could rename / mark read /
     delete in that thread, or post into it - as the character or the system - over REST
O-3  an Admin could keep an Owner-approved stdio MCP server or remote secret-bearing server on
     an agent and rewrite its command or address (the check compared feature NAMES only)
O-4  an Admin could repoint a connection at another host and keep its write-only key
O-5  the voice add-on's thread socket ignored "unclaimed = this computer only",
     anonymous_access=none and the character's profiles list
O-6  a staged restore went ahead when the copy of the current data failed
O-7  erasing the only profile on a character's list made the character everyone's
O-8  the setup-token throttle was asked only after the token had been compared
O-9  a Restricted profile's quiet hours / minutes per day were enforced on HTTP only, not on
     the WebSocket the chat runs over

Run: cd backend && uv run pytest tests/test_review_opus_20261018.py
"""
from __future__ import annotations

import dataclasses
import json
import sqlite3

import pytest
import yaml
from _extfake import ext_env  # the fixture
from agents_world import aw  # the fixture
from starlette.websockets import WebSocketDisconnect

from app import accounts, auth, backups, config, main, policy

LAN = ("203.0.113.60", 41000)
ORIGIN = {"origin": "http://testserver"}


# --------------------------------------------------------------------------- #
# A household: alpha is made for Mia only, beta for Max only (as in test_ws_profile_visibility)
# --------------------------------------------------------------------------- #


@pytest.fixture
def house(world):
    from agents_world import sibling
    owner = world.create_owner(world.client())
    mia = world.person(owner, "Mia", "restricted")
    world.person(owner, "Max", "member", pin="5555")
    member = sibling(world, owner, ("203.0.113.61", 41001))
    assert world.login(member, name="Max", pin="5555").status_code == 200
    max_id = next(p["id"] for p in owner.get("/api/people").json()["profiles"] if p["name"] == "Max")
    assert owner.patch("/api/characters/alpha", json={"profiles": [mia["id"]]}).status_code == 200
    assert owner.patch("/api/characters/beta", json={"profiles": [max_id]}).status_code == 200
    return {"owner": owner, "member": member, "max_id": max_id, "mia_id": mia["id"]}


# O-1 ------------------------------------------------------------------------ #

def test_a_member_socket_gets_no_operator_frames_and_no_hidden_character_items(house):
    scope = main.scope_frame_for_principal
    member = policy.profile_principal(accounts.get_profile(house["max_id"]))
    owner = policy.profile_principal(accounts.get_owner())
    operator_frames = [{"type": "harness_state", "jobs": {"running": 1}},
                       {"type": "harness_sessions", "sessions": []},
                       {"type": "avatar_pool", "pools": {"alpha": {"dir": "/data/avatars/alpha"}}},
                       {"type": "reaction_pool", "pools": {}},
                       {"type": "progress", "thread_id": "t-beta", "item": {"kind": "tool", "args": "/etc"}}]
    main._thread_bot.update({"t-alpha": "alpha", "t-beta": "beta"})
    for f in operator_frames:
        assert scope(f, member) is None, f["type"]           # a Member holds none of these
        assert scope(f, owner) == f, f["type"]              # the Owner still gets every one
    # A Member granted the permission behind a frame gets it - for its own characters only.
    granted = dataclasses.replace(member, permissions=member.permissions | {
        "dispatch_agents", "manage_characters"})
    assert scope({"type": "avatar_pool", "pools": {}}, granted) is not None
    assert scope({"type": "progress", "thread_id": "t-beta", "item": {}}, granted) is not None
    assert scope({"type": "progress", "thread_id": "t-alpha", "item": {}}, granted) is None
    # A reaction overlay names its bot: alpha's is not Max's to see, beta's is.
    assert scope({"type": "reaction", "bot_id": "alpha", "safe": True}, member) is None
    assert scope({"type": "reaction", "bot_id": "beta", "safe": True}, member) is not None


# O-2 ------------------------------------------------------------------------ #

def test_a_member_cannot_reach_a_hidden_characters_thread_over_rest(house):
    owner, member = house["owner"], house["member"]
    r = owner.post("/api/threads", json={"bot_id": "alpha"})
    assert r.status_code == 200, r.text
    tid = r.json()["id"]
    r = owner.post(f"/api/threads/{tid}/messages", json={"role": "assistant", "content": "Mia's secret"})
    assert r.status_code == 200, r.text
    mid = r.json()["id"]
    # The Owner's unread list has it; the Member's does not (a thread id is the key to the rest).
    assert tid in {u["thread_id"] for u in owner.get("/api/unread").json()["unread"]}
    assert tid not in {u["thread_id"] for u in member.get("/api/unread").json()["unread"]}
    # Every thread/message route refuses the Member (they used to check only Restricted callers).
    assert member.patch(f"/api/threads/{tid}", json={"title": "pwned"}).status_code == 403
    assert member.post(f"/api/threads/{tid}/read").status_code == 403
    assert member.patch(f"/api/messages/{mid}", json={"content": "rewritten"}).status_code == 403
    assert member.delete(f"/api/messages/{mid}").status_code == 403
    assert member.post(f"/api/threads/{tid}/messages", json={"role": "user", "content": "hi"}).status_code == 403
    assert owner.get(f"/api/threads/{tid}").json().get("title") != "pwned"


def test_a_member_may_not_post_as_a_character_or_the_system(house):
    owner, member = house["owner"], house["member"]
    tid = owner.post("/api/threads", json={"bot_id": "beta"}).json()["id"]      # Max's own character
    for role in ("assistant", "system"):
        r = member.post(f"/api/threads/{tid}/messages", json={"role": role, "content": "forged"})
        assert r.status_code == 403, (role, r.text)
    assert member.post(f"/api/threads/{tid}/messages", json={"role": "user", "content": "mine"}).status_code == 200
    # The Owner (and an agent) still may.
    assert owner.post(f"/api/threads/{tid}/messages", json={"role": "assistant", "content": "ok"}).status_code == 200


# O-3 ------------------------------------------------------------------------ #

def test_an_admin_cannot_rewrite_what_the_owner_approved_on_an_agent(aw):
    from agents_world import spec
    aw.world.person(aw.owner, "Ada", "admin", password="admin-pass1")
    admin = aw.world.client(("203.0.113.62", 1))
    assert aw.world.login(admin, name="Ada", password="admin-pass1").status_code == 200
    stdio = {"x": {"transport": "stdio", "command": "node", "args": ["server.js"]}}
    remote = {"r": {"transport": "http", "url": "https://mcp.example/mcp",
                    "header_secrets": {"Authorization": "STUDIOFORGE_MCP_KEY_RO"}}}
    r = aw.owner.post("/api/agents", json={"name": "Approved", "spec": spec(
        tool_allowlist=["Read"], mcp_servers={**stdio, **remote})})
    assert r.status_code == 201, r.text
    aid = r.json()["agent"]["id"] if "agent" in r.json() else r.json()["id"]

    def patch(servers):
        return admin.patch(f"/api/agents/{aid}", json={"spec": spec(tool_allowlist=["Read"], mcp_servers=servers)})

    # Same name, new command line: code execution on the host -> the Owner's call.
    r = patch({"x": {"transport": "stdio", "command": "sh", "args": ["-c", "id"]}, **remote})
    assert r.status_code == 403 and r.json()["error"]["code"] == "agent_needs_owner", r.text
    # Same name, the stored secret sent somewhere else -> the Owner's call.
    r = patch({**stdio, "r": {**remote["r"], "url": "https://attacker.example/mcp"}})
    assert r.status_code == 403 and r.json()["error"]["code"] == "agent_needs_owner", r.text
    # Leaving the approved grants exactly as they are, or removing one, is fine.
    assert patch({**stdio, **remote}).status_code == 200
    assert patch(stdio).status_code == 200


# O-4 ------------------------------------------------------------------------ #

def test_an_admin_cannot_move_a_saved_key_to_another_host(world):
    owner = world.create_owner(world.client())
    world.person(owner, "Ada", "admin", password="admin-pass1")
    admin = world.client(("203.0.113.63", 1))
    assert world.login(admin, name="Ada", password="admin-pass1").status_code == 200
    r = owner.post("/api/connections", json={"type": "openai_compatible", "name": "Box",
                                              "base_url": "http://127.0.0.1:9/v1",
                                              "api_key": "sk-house-key-0123456789"})  # scrub-ok: test fixture
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    r = admin.patch(f"/api/connections/{cid}", json={"base_url": "http://203.0.113.99:9/v1"})
    assert r.status_code == 403, r.text
    # Same host (another port), a fresh key, or no key: nothing is carried to a stranger.
    assert admin.patch(f"/api/connections/{cid}", json={"base_url": "http://127.0.0.1:10/v1"}).status_code == 200
    assert admin.patch(f"/api/connections/{cid}", json={"base_url": "http://203.0.113.99:9/v1",
                                                        "api_key": "sk-admins-own-0123456789"}).status_code == 200  # scrub-ok
    # The Owner may move it.
    assert owner.patch(f"/api/connections/{cid}", json={"base_url": "http://198.51.100.7:9/v1"}).status_code == 200


# O-5 ------------------------------------------------------------------------ #

def _ws_refused(c, path) -> int:
    with pytest.raises(WebSocketDisconnect) as e:
        with c.websocket_connect(path, headers=ORIGIN) as ws:
            ws.receive_text()
            ws.receive_text()
    return e.value.code


def _set_bot(bot_id: str, **fields) -> None:
    doc = yaml.safe_load(config.CONFIG_PATH.read_text())
    for b in doc["bots"]:
        if b["id"] == bot_id:
            b.update(fields)
    config.CONFIG_PATH.write_text(yaml.safe_dump(doc))
    config._invalidate_bots_cache()


def test_the_voice_socket_of_an_unclaimed_install_is_closed_to_the_network(ext_env, monkeypatch):
    from _extfake import KNOBS, install, thread_for
    c, _ = ext_env
    install()
    tid = thread_for(c, "alpha")
    monkeypatch.delenv("DISPATCH_ALLOW_OPEN_INSTALL", raising=False)   # the secure default
    assert not policy.claimed()
    assert _ws_refused(c, f"/ws/voice/{tid}") == 1008                   # the TestClient is not loopback
    assert KNOBS.sockets == []


def test_the_voice_socket_honours_anonymous_access_none(ext_env):
    from _extfake import KNOBS, install, lock, thread_for
    c, _ = ext_env
    install()
    tid = thread_for(c, "alpha")                                        # a safe bot
    lock(c)
    cfg = auth.load()
    cfg.anonymous_access = "none"
    auth._write(cfg)
    auth._bust_cache()
    assert _ws_refused(c, f"/ws/voice/{tid}") == 1008
    assert KNOBS.sockets == []


def test_the_voice_socket_honours_the_characters_profiles_list(ext_env):
    from _extfake import KNOBS, install, lock, thread_for
    c, _ = ext_env
    install()
    tid = thread_for(c, "alpha")                                        # a safe bot ...
    _set_bot("alpha", profiles=["someone-else"])                        # ... made for one profile
    lock(c)
    assert _ws_refused(c, f"/ws/voice/{tid}") == 1008                   # not for an anonymous device
    assert KNOBS.sockets == []


# O-6 ------------------------------------------------------------------------ #

def test_a_restore_stops_when_the_current_data_cannot_be_saved_first(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    config._invalidate_bots_cache()
    accounts.reset_for_tests()

    def rows():
        c = sqlite3.connect(tmp_path / "chats.db")
        try:
            return c.execute("SELECT id FROM threads ORDER BY id").fetchall()
        finally:
            c.close()

    con = sqlite3.connect(tmp_path / "chats.db")
    con.execute("CREATE TABLE threads (id TEXT PRIMARY KEY)")
    con.execute("INSERT INTO threads VALUES ('t1')")
    con.commit()
    con.close()
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"bots": [{"id": "a", "name": "A"}]}), encoding="utf-8")
    row = backups.create_archive("correct horse battery staple")
    con = sqlite3.connect(tmp_path / "chats.db")
    con.execute("INSERT INTO threads VALUES ('t2')")       # written after the backup
    con.commit()
    con.close()
    backups.stage_restore(f"archive:{row['name']}", "correct horse battery staple")

    def disk_full(src, dst):
        raise sqlite3.OperationalError("database or disk is full")

    real_snapshot = backups._sqlite_snapshot
    monkeypatch.setattr(backups, "_sqlite_snapshot", disk_full)
    out = backups.apply_pending_restore()
    assert out["applied"] is False and "could not save the current data" in out["error"]
    assert rows() == [("t1",), ("t2",)]                    # nothing was overwritten
    assert backups.pending_path().exists()                 # still staged: the next start retries
    staged = json.loads(backups.pending_path().read_text(encoding="utf-8"))["staging"]
    monkeypatch.setattr(backups, "_sqlite_snapshot", real_snapshot)      # the disk has room again
    assert backups.apply_pending_restore()["applied"] is True
    assert rows() == [("t1",)]
    assert staged and not backups.pending_path().exists()
