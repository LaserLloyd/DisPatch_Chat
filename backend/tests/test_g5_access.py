"""Gate G5 - access control, exercised from the outside.

With NO session and with a RESTRICTED session, call every mutating route, every
tool/agent/connection-admin route, every settings route and every WebSocket action
directly (the same HTTP client the UI uses, no UI), and assert denial. Then the other
half: a Restricted turn reaches only ``audience=restricted|all`` characters.

The list of routes is not written here - it is read from the running app, so a route
added tomorrow is covered the day it appears (and ``test_route_table.py`` makes sure it
has a declared permission to be tested against).
"""
from __future__ import annotations

import re

import pytest
from fastapi.routing import APIRoute

from app import config, main, route_policy
from app.contracts.permissions import ROLE_PERMISSIONS, Role

RESTRICTED = {p.value for p in ROLE_PERMISSIONS[Role.RESTRICTED]}
LAN = ("192.168.1.50", 40000)

#: Gate-level refusal codes: seeing one of these means the policy engine said no.
GATE_CODES = {"safe_mode", "permission_denied", "route_undeclared", "machine_scope_denied",
              "setup_required", "agents_only", "invalid_api_key"}


def _routes():
    out = []
    for r in main.app.routes:
        if isinstance(r, APIRoute):
            for m in sorted(r.methods - {"HEAD", "OPTIONS"}):
                out.append((m, r.path))
    return sorted(set(out), key=lambda t: (t[1], t[0]))


def _url(template: str) -> str:
    return re.sub(r"\{[^}]+\}", "x", template)


def _call(client, method, template):
    url = _url(template)
    if method == "GET":
        return client.get(url)
    return client.request(method, url, json={})


def _requirement(method, template):
    return route_policy.requirement_for(method, template, _url(template))


def _must_be_denied_to_restricted(method, template) -> bool:
    req = _requirement(method, template)
    if req is None or req.public or req.scoped:
        return False
    if req.authenticated:
        return True
    if req.permission is None:
        return True                       # machine-only
    return req.permission not in RESTRICTED


@pytest.fixture
def clients(world):
    owner = world.create_owner(world.client())
    mia = world.person(owner, "Mia", "restricted")
    kid = world.client(LAN)
    assert world.login(kid, profile_id=mia["id"]).status_code == 200
    return {
        "owner": owner,
        "kid": kid,
        # nobody signed in: a browser tab, and a bare script (no browser headers)
        "anonymous": world.client(LAN),
        "script": world.client(LAN, origin=False),
    }


def test_every_protected_route_denies_no_session_and_a_restricted_session(clients):
    checked, leaks = 0, []
    for method, template in _routes():
        if not _must_be_denied_to_restricted(method, template):
            continue
        for who in ("anonymous", "script", "kid"):
            r = _call(clients[who], method, template)
            checked += 1
            if r.status_code not in (401, 403):
                leaks.append(f"{who}: {method} {template} -> {r.status_code}")
    assert checked > 300
    assert not leaks, "reachable without permission:\n  " + "\n  ".join(leaks)


def test_the_owner_is_not_turned_away_by_the_gate(clients):
    """The other half of the matrix: the same routes, called by the Owner, never come
    back with a gate-level refusal (a handler may still say 404/409/422 - that is the
    handler, not the policy)."""
    wrong = []
    for method, template in _routes():
        req = _requirement(method, template)
        if req is None or req.public or req.permission is None:
            continue
        if template.startswith("/api/auth/") or template in ("/api/files/wipe",):
            continue                      # would sign the owner out / are exercised elsewhere
        r = _call(clients["owner"], method, template)
        code = None
        if r.headers.get("content-type", "").startswith("application/json"):
            body = r.json()
            if isinstance(body, dict) and isinstance(body.get("error"), dict):
                code = body["error"].get("code")
        if r.status_code in (401, 403) and code in GATE_CODES:
            wrong.append(f"{method} {template}: {r.status_code} {code}")
    assert not wrong, "\n  ".join(wrong)


def test_the_denial_names_the_missing_permission(clients):
    r = clients["kid"].put("/api/tools", json={"tools": []})
    assert r.status_code == 403
    body = r.json()
    assert body["detail"] == "Unlock for full access"          # the 2.x wording, unchanged
    assert body["decoy"] is True                               # the 2.x marker, unchanged
    assert body["required_permission"] == "manage_plugins"
    assert body["error"]["code"] == "safe_mode" and body["error"]["fix"]


def test_a_member_is_stopped_where_a_restricted_profile_is_and_a_bit_earlier(world, clients):
    owner = clients["owner"]
    world.person(owner, "Max", "member", pin="5555")
    member = world.client(LAN)
    assert world.login(member, name="Max", pin="5555").status_code == 200
    assert member.get("/api/bots").status_code == 200
    assert member.get("/api/tools").status_code == 200
    r = member.put("/api/tools", json={"tools": []})
    assert r.status_code == 403 and r.json()["error"]["code"] == "permission_denied"
    assert r.json()["required_permission"] == "manage_plugins"
    for path in ("/api/bots/all", "/api/llm/providers", "/api/people", "/api/dashboard",
                 "/api/harness/status", "/api/export", "/api/search?q=x"):
        assert member.get(path).status_code == 403, path
    # Dispatching an agent is not a Member permission; the Owner can grant it per profile.
    from app import accounts
    assert "dispatch_agents" not in main.policy.effective_permissions(accounts.get_by_name("Max"))


# --------------------------------------------------------------------------- #
# Scoped routes: reachable, but only for what a Restricted caller may see
# --------------------------------------------------------------------------- #


def _roster(client):
    return {b["id"]: b for b in client.get("/api/bots").json()["bots"]}


def test_a_restricted_roster_is_the_audience_not_the_whole_roster(clients):
    everyone = _roster(clients["owner"])
    seen = _roster(clients["kid"])
    assert set(seen) and set(seen) < set(everyone)
    assert all(b["audience"] in ("all", "restricted") for b in seen.values())
    assert all(b["audience"] == "members" for k, b in everyone.items() if k not in seen)
    assert _roster(clients["anonymous"]).keys() == seen.keys()


def test_a_restricted_caller_cannot_reach_a_members_character_by_id(world, clients):
    hidden = next(b for b in config.load_bots() if b.audience == "members")
    shown = next(b for b in config.load_bots() if b.audience == "all")
    kid = clients["kid"]
    assert kid.get(f"/api/bots/{hidden.id}/avatar").status_code == 403
    assert kid.get(f"/api/bots/{hidden.id}/avatar/thumb").status_code == 403
    assert kid.get(f"/api/bots/{hidden.id}/avatar/history").status_code == 403
    assert kid.post("/api/threads", json={"bot_id": hidden.id}).status_code == 403
    assert kid.post("/api/threads", json={"bot_id": shown.id}).status_code == 200
    owner_thread = clients["owner"].post("/api/threads", json={"bot_id": hidden.id}).json()["id"]
    assert kid.get(f"/api/threads/{owner_thread}").status_code == 403
    assert kid.get(f"/api/threads/{owner_thread}/messages").status_code == 403
    assert kid.get(f"/api/threads?bot_id={hidden.id}").status_code == 403
    assert owner_thread in {t["id"] for t in clients["owner"].get(
        f"/api/threads?bot_id={hidden.id}").json()["threads"]}


def test_a_restricted_tool_list_is_only_the_safe_tools(world, clients, tmp_path_factory):
    from app import tools
    page = tmp_path_factory.mktemp("site")        # outside the data dir, which a tool may not serve
    (page / "index.html").write_text("<p>hi</p>")
    root = page.as_posix()
    manifest = [
        "tools:",
        f"  - {{id: kids, title: Kids, kind: static, root: '{root}', entry: index.html, safe: true}}",
        f"  - {{id: grown, title: Grownups, kind: static, root: '{root}', entry: index.html, safe: false}}",
    ]
    (config.DATA_DIR / "tools.yaml").write_text("\n".join(manifest) + "\n")
    tools._reset_state()
    ids = {t["id"] for t in clients["kid"].get("/api/tools").json()["tools"]}
    assert "kids" in ids and "grown" not in ids
    assert clients["kid"].get("/tools/kids/index.html").status_code == 200
    assert clients["kid"].get("/tools/grown/index.html").status_code == 403
    assert clients["kid"].get("/api/tools/grown/status").status_code == 403
    # A Member sees both (no host paths); the Owner sees the paths too.
    owner_ids = {t["id"]: t for t in clients["owner"].get("/api/tools").json()["tools"]}
    assert {"kids", "grown"} <= set(owner_ids) and "root" in owner_ids["grown"]


# --------------------------------------------------------------------------- #
# WebSocket actions
# --------------------------------------------------------------------------- #


def _ws_exchange(client, frames):
    """Send ``frames`` on /ws and return every frame received up to a ping sentinel."""
    got = []
    with client.websocket_connect("/ws") as ws:
        hello = ws.receive_json()
        for f in frames:
            ws.send_json(f)
        ws.send_json({"type": "ping"})
        while True:
            f = ws.receive_json()
            if f.get("type") == "pong":
                break
            got.append(f)
    return hello, got


def test_every_websocket_action_is_gated(clients, monkeypatch):
    turns = []

    async def fake_turn(thread_id, bot_id, text, opts=None):
        turns.append((thread_id, bot_id, opts))

    monkeypatch.setattr(main, "run_agent_turn", fake_turn)
    for who in ("kid", "anonymous", "script"):
        for action, req in route_policy.WS_ACTION_PERMISSIONS.items():
            if req.public or req.permission in RESTRICTED:
                continue
            _, frames = _ws_exchange(clients[who], [{"type": action, "thread_id": "x", "text": "hi"}])
            errs = [f for f in frames if f.get("type") == "error"]
            assert errs, f"{who}: ws action {action!r} was not refused"
            assert errs[0]["code"] in ("safe_mode", "permission_denied"), (who, action, errs[0])
    assert turns == []


def test_a_restricted_turn_reaches_only_restricted_audience_characters(world, clients, monkeypatch):
    """G5: a Restricted turn never reaches a Members character, and the one it may
    reach runs LOCKED (no tools, no dispatch)."""
    turns = []

    async def fake_turn(thread_id, bot_id, text, opts=None):
        turns.append((thread_id, bot_id, opts))

    monkeypatch.setattr(main, "run_agent_turn", fake_turn)
    members_bot = next(b.id for b in config.load_bots() if b.audience == "members")
    kids_bot = next(b.id for b in config.load_bots() if b.audience in ("all", "restricted"))
    owner = clients["owner"]
    t_members = owner.post("/api/threads", json={"bot_id": members_bot}).json()["id"]
    t_kids = owner.post("/api/threads", json={"bot_id": kids_bot}).json()["id"]
    for who in ("kid", "anonymous"):
        hello, frames = _ws_exchange(clients[who], [
            {"type": "send", "thread_id": t_members, "text": "hello", "client_msg_id": "m1"},
            {"type": "send", "thread_id": t_kids, "text": "hello", "client_msg_id": "m2"},
        ])
        assert hello["decoy"] is True and hello["role"] == "restricted"
        assert {b["id"] for b in hello["bots"]} == set(_roster(clients["kid"]))
        errors = [f for f in frames if f.get("type") == "error"]
        assert errors and "Unlock" in errors[0]["message"]
        acks = [f for f in frames if f.get("type") == "ack"]
        assert {a.get("client_msg_id"): a.get("status") for a in acks} == {"m1": "rejected", "m2": "ok"}
    assert {t[0] for t in turns} == {t_kids}
    assert all(t[2] is not None and t[2].locked is True for t in turns)
    # Nothing was written into the members thread.
    msgs = clients["owner"].get(f"/api/threads/{t_members}/messages").json()["messages"]
    assert msgs == []


def test_frames_about_a_members_character_never_reach_a_restricted_socket(world, clients):
    members_bot = next(b.id for b in config.load_bots() if b.audience == "members")
    tid = clients["owner"].post("/api/threads", json={"bot_id": members_bot}).json()["id"]
    main._thread_bot[tid] = members_bot
    with clients["kid"].websocket_connect("/ws") as ws:
        ws.receive_json()
        clients["kid"].portal.call(main.manager.broadcast, {
            "type": "message", "thread_id": tid, "bot_id": members_bot,
            "message": {"id": "z", "content": "secret", "role": "assistant"}})
        ws.send_json({"type": "ping"})
        seen = []
        while True:
            f = ws.receive_json()
            if f.get("type") == "pong":
                break
            seen.append(f)
    assert seen == []


# --------------------------------------------------------------------------- #
# A Restricted voice turn (the add-on's socket)
# --------------------------------------------------------------------------- #

from _extfake import KNOBS, ext_env, install, thread_for  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402


def test_a_restricted_voice_socket_reaches_only_restricted_audience_characters(ext_env):
    c, _ = ext_env
    install()
    KNOBS.handler_mode = "hold"
    # An owner, a Restricted profile, and the profile's own session.
    assert c.post("/api/auth/setup", json={"name": "Olive", "password": "owner-password-1"}).status_code == 200
    mia = c.post("/api/people", json={"name": "Mia", "role": "restricted"}).json()
    members_tid = thread_for(c, next(b.id for b in config.load_bots() if b.audience == "members"))
    kids_tid = thread_for(c, next(b.id for b in config.load_bots() if b.audience in ("all", "restricted")))
    c.cookies.clear()
    assert c.post("/api/auth/login", json={"profile_id": mia["id"]}).status_code == 200
    origin = {"origin": "http://testserver"}
    with pytest.raises(WebSocketDisconnect) as e, c.websocket_connect(
            f"/ws/voice/{members_tid}", headers=origin) as ws:
        ws.receive_text()
    assert e.value.code == 1008
    with c.websocket_connect(f"/ws/voice/{kids_tid}", headers=origin) as ws:
        ws.receive_text()
    sock = KNOBS.sockets[-1]
    assert sock.locked is True, "a Restricted profile's voice turn must run locked"
