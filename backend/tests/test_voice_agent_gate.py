"""One agent rule for every turn entry point (security review 2026-10-21, H3).

A typed send on /ws refuses an agent-backed character (its turns run an agent on the host)
to a profile without ``dispatch_agents`` (main._ws_bot_allowed). The extension paths did
not: a Member could open the voice socket on an agent bot's thread and every spoken or
typed voice turn ran the agent. Now:

* the thread socket refuses such a caller with 1008 + a reason (the add-on stops, says why);
* ``ctx.submit_turn`` refuses a turn on an agent bot while such a caller has a socket on the
  thread (defence in depth: a character switched to an agent backend mid-call);
* a /ws resend of a stored-but-never-run message passes the same gate as a fresh send;
* an app's ``ctx.dispatch_turn`` applies it to the person whose request asked.

Owner/Admin voice on agent bots, and Members on direct/advisor bots, are unchanged.
Run: cd backend && uv run pytest tests/test_voice_agent_gate.py
"""
from __future__ import annotations

import json

import pytest
from _extfake import KNOBS, ext_env, install, thread_for
from starlette.websockets import WebSocketDisconnect

from app import accounts, apps_loader, config, extensions, main, openclaw, policy

ORIGIN = {"origin": "http://testserver"}
ADV = {"providers": [{"provider": "custom", "base_url": "http://p/v1", "model": "m"}]}


def _household(c):
    from fastapi.testclient import TestClient
    r = c.post("/api/auth/setup", json={"name": "Olive", "password": "owner-pass1", "new_pin": "2468"})
    assert r.status_code == 200, r.text
    made = []

    def client(addr):
        k = TestClient(main.app, client=addr)
        k.__enter__()
        k.headers.update({"Origin": "http://testserver"})
        made.append(k)
        return k
    return client, made


def _member(c, client, addr=("203.0.113.120", 45001), **body):
    r = c.post("/api/people", json={"name": "Max", "role": "member", "pin": "5555", **body})
    assert r.status_code == 201, r.text
    k = client(addr)
    assert k.post("/api/auth/login", json={"name": "Max", "pin": "5555"}).status_code == 200
    return r.json(), k


def _owned_thread(c, owner_id: str, bot_id: str) -> str:
    """4.0: a User without dispatch_agents cannot start a chat with an agent character at
    all; the thread is made for it directly (as one that existed before the grant changed)."""
    from app import main as m
    t = c.portal.call(lambda: m.db.create_thread(bot_id=bot_id, owner_profile_id=owner_id))
    return t.id


def _refused(k, path):
    with pytest.raises(WebSocketDisconnect) as e:
        with k.websocket_connect(path, headers=ORIGIN) as ws:
            ws.receive_text()
            ws.receive_text()
    return e.value.code, e.value.reason


@pytest.fixture
def hh(ext_env):
    c, _ = ext_env
    extensions._ext_callers.clear(); extensions._ext_last_closed.clear()
    extensions._locked_threads.clear()
    config.upsert_bot(config.Bot(id="adv", name="Sage", advisor=dict(ADV)))
    install()
    client, made = _household(c)
    try:
        yield c, client
    finally:
        for k in made:
            k.__exit__(None, None, None)
        extensions._ext_callers.clear(); extensions._ext_last_closed.clear()


async def _rows(tid):
    rows, _ = await main.db.list_messages(tid, limit=50)
    return rows


# --------------------------------------------------------------------------- #
# The voice socket
# --------------------------------------------------------------------------- #

def test_a_member_voice_socket_on_an_agent_bot_is_refused_with_a_reason(hh):
    c, client = hh
    KNOBS.handler_mode = "submit"
    prof, member = _member(c, client)
    tid = _owned_thread(c, prof["id"], "main")        # an agent-backed character
    code, reason = _refused(member, f"/ws/voice/{tid}")
    assert code == 1008 and "may not run agents" in reason, (code, reason)
    assert KNOBS.sockets == []                        # the extension never saw the socket
    assert c.turns == []
    assert c.portal.call(_rows, tid) == []


def test_a_member_voice_turn_on_an_advisor_bot_still_runs(hh):
    c, client = hh
    KNOBS.handler_mode = "submit"
    _, member = _member(c, client)
    tid = thread_for(member, "adv")
    with member.websocket_connect(f"/ws/voice/{tid}", headers=ORIGIN) as ws:
        assert json.loads(ws.receive_text())["type"] == "ready"
        ws.send_text("hello there")                   # spoken or typed: the same submit
        assert json.loads(ws.receive_text())["id"]
    assert [t[1] for t in c.turns] == ["adv"]


def test_the_owner_voice_turn_on_an_agent_bot_still_runs(hh):
    c, _client = hh
    KNOBS.handler_mode = "submit"
    tid = thread_for(c, "main")
    with c.websocket_connect(f"/ws/voice/{tid}", headers=ORIGIN) as ws:
        assert json.loads(ws.receive_text())["type"] == "ready"
        ws.send_text("what's on today")
        assert json.loads(ws.receive_text())["id"]
    assert [t[1] for t in c.turns] == ["main"]


def test_a_member_granted_dispatch_agents_may_use_voice_on_an_agent_bot(hh):
    c, client = hh
    KNOBS.handler_mode = "submit"
    _, member = _member(c, client, grants=["dispatch_agents"])
    tid = thread_for(member, "main")
    with member.websocket_connect(f"/ws/voice/{tid}", headers=ORIGIN) as ws:
        assert json.loads(ws.receive_text())["type"] == "ready"
        ws.send_text("go")
        assert json.loads(ws.receive_text())["id"]
    assert [t[1] for t in c.turns] == ["main"]


def test_submit_turn_refuses_an_agent_turn_while_a_member_socket_is_open(hh):
    """The socket was opened on an advisor bot; the character is then switched to the agent
    backend. The extension's submit (no caller of its own) must still be refused - and in
    the grace window after the call ended, too."""
    c, client = hh
    KNOBS.handler_mode = "hold"
    _, member = _member(c, client)
    tid = thread_for(member, "adv")
    with member.websocket_connect(f"/ws/voice/{tid}", headers=ORIGIN) as ws:
        assert json.loads(ws.receive_text())["type"] == "ready"
        config.upsert_bot(config.Bot(id="adv", name="Sage"))           # now an agent bot
        with pytest.raises(PermissionError, match="may not run agents"):
            c.portal.call(lambda: KNOBS.ctx.submit_turn(tid, "run it", metadata={"voice": True}))
    with pytest.raises(PermissionError, match="may not run agents"):
        c.portal.call(lambda: KNOBS.ctx.submit_turn(tid, "after hang-up"))
    assert c.turns == []
    assert c.portal.call(_rows, tid) == []            # refused = no row either


def test_an_owner_extension_turn_on_an_agent_bot_is_not_refused(hh):
    c, _client = hh
    KNOBS.handler_mode = "hold"
    tid = thread_for(c, "main")
    with c.websocket_connect(f"/ws/voice/{tid}", headers=ORIGIN) as ws:
        assert json.loads(ws.receive_text())["type"] == "ready"
        assert c.portal.call(lambda: KNOBS.ctx.submit_turn(tid, "hi"))
    assert [t[1] for t in c.turns] == ["main"]


# --------------------------------------------------------------------------- #
# /ws: the resend of a stored-but-never-run message
# --------------------------------------------------------------------------- #

def test_a_resend_replay_passes_the_same_agent_gate(hh):
    c, client = hh
    prof, member = _member(c, client)
    tid = _owned_thread(c, prof["id"], "main")
    cmid = "c-00000000-0000-0000-0000-000000000001"
    main._ACK_SEEN[cmid] = {"thread_id": tid, "bot_id": "main", "text": "x", "scheduled": False}
    with member.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "hello"
        ws.send_json({"type": "send", "thread_id": tid, "text": "x", "client_msg_id": cmid})
        seen = []
        for _ in range(6):
            f = ws.receive_json()
            seen.append(f)
            if f.get("type") == "error":
                break
    assert any(f.get("code") == "permission_denied" for f in seen), seen
    assert c.turns == []


# --------------------------------------------------------------------------- #
# Apps: ctx.dispatch_turn
# --------------------------------------------------------------------------- #

def test_an_app_turn_on_an_agent_bot_needs_dispatch_agents(hh, monkeypatch):
    c, client = hh
    monkeypatch.setattr(openclaw, "cli_available", lambda: True)
    prof, _member_client = _member(c, client)
    max_pol = policy.Policy(policy.profile_principal(accounts.get_profile(prof["id"])))
    tid = thread_for(c, "main")

    def attempt(pol, bot_id="main"):
        async def go():
            token = apps_loader._caller.set(pol)
            try:
                return main._app_dispatch_turn(tid, bot_id, "x")
            finally:
                apps_loader._caller.reset(token)
        return c.portal.call(go)

    assert attempt(max_pol) is False                              # a Member, agent bot
    assert attempt(max_pol, "adv") is True                        # a Member, advisor bot
    assert attempt(policy.Policy(policy.machine_principal("test"))) is True   # a cron
    assert attempt(None) is False                                 # no recorded caller: fail closed
