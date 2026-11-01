"""4.2 access (PLAN-v2 §3, slice S2) and reaction scoping (S2b).

``characters.may_use`` split into ``may_chat`` / ``entitled`` / ``may_manage``; the turn gate (R1:
an agent or app bot may be personal only to the protected Admin, R2: nobody chats with a
template); inject / daily by bot id; thread-create on a template; list filters; reactions reach
only the profile that owns the chat.

Run: cd backend && uv run pytest tests/test_access_42.py --basetemp ~/.cache/cfp/42/tmp-s2
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app import accounts, auth, characters, config, main, ownership, policy
from app.contracts.permissions import Role

PROV = {"providers": [{"provider": "openai", "model": "m", "base_url": "http://127.0.0.1:9/v1"}]}
TOKEN = "access42-machine-token"  # scrub-ok: throwaway test fixture


# --------------------------------------------------------------------------- #
# Pure rules
# --------------------------------------------------------------------------- #


@pytest.fixture
def house(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.yaml")
    accounts.reset_for_tests()
    owner = accounts.create_profile("Owner", Role.ADMIN, protected=True)
    admin2 = accounts.create_profile("Ann", Role.ADMIN)
    uma = accounts.create_profile("Uma", Role.USER)
    basic = accounts.create_profile("Bo", Role.BASIC)
    off = accounts.create_profile("Off", Role.USER)
    accounts.update_profile(off.id, disabled=True)
    config.upsert_bot(config.Bot(id="tmpl", name="T", advisor=dict(PROV), give_to="everyone"))
    config.upsert_bot(config.Bot(id="tmpl2", name="T2", advisor=dict(PROV),
                                 give_to=[uma.id, basic.id, off.id]))
    config.upsert_bot(config.new_copy(config.get_bot("tmpl"), id="p0owner0copy",
                                      owner_profile=owner.id))
    config.upsert_bot(config.new_copy(config.get_bot("tmpl"), id="p0uma00copy",
                                      owner_profile=uma.id))
    # agent bots, personal to the protected Admin (the 4.2 shape) and one hand-edited onto Uma
    config.upsert_bot(config.Bot(id="bits", name="Bits", scope="personal", owner_profile=owner.id))
    config.upsert_bot(config.Bot(id="umaagent", name="UA", scope="personal",
                                 owner_profile=uma.id))
    config.upsert_bot(config.Bot(id="legacy1", name="L1", advisor=dict(PROV)))   # pre-012 household
    config._invalidate_bots_cache()
    yield SimpleNamespace(owner=owner, admin2=admin2, uma=uma, basic=basic, off=off)
    accounts.reset_for_tests()
    config._invalidate_bots_cache()


def P(prof):
    return policy.profile_principal(accounts.get_profile(prof.id))


def chat(bot_id, pr):
    return characters.may_chat(config.get_bot(bot_id), pr)


def test_may_use_is_may_chat():
    assert characters.may_use is characters.may_chat


def test_nobody_chats_with_a_template_admins_and_machines_included(house):
    for who in (house.owner, house.admin2, house.uma, house.basic):
        assert not chat("tmpl", P(who)) and not chat("tmpl2", P(who)), who.name
    assert not chat("tmpl", policy.machine_principal("api_token"))
    # an unclaimed install has no copies yet: it is one big Owner
    assert chat("tmpl", policy.open_principal())


def test_everyone_chats_with_their_own_copy_only(house):
    assert chat("p0owner0copy", P(house.owner)) and chat("p0uma00copy", P(house.uma))
    assert not chat("p0uma00copy", P(house.owner))          # never an Admin
    assert not chat("p0owner0copy", P(house.uma))
    assert not chat("p0owner0copy", P(house.admin2))
    m = policy.machine_principal("api_token")
    assert chat("p0owner0copy", m) and not chat("p0uma00copy", m)


def test_r1_agent_bots_are_personal_only_to_the_protected_admin(house):
    assert chat("bits", P(house.owner))
    assert chat("bits", policy.machine_principal("api_token"))
    for who in (house.admin2, house.uma, house.basic):
        assert not chat("bits", P(who)), who.name
    # a hand-edited roster that hands an agent bot to a User still does not let anyone chat
    assert not chat("umaagent", P(house.uma))
    assert not chat("umaagent", P(house.owner))
    assert not chat("umaagent", policy.machine_principal("api_token"))


def test_pre_012_household_bots_keep_their_old_rules(house):
    assert chat("legacy1", P(house.uma)) and chat("legacy1", P(house.owner))
    assert chat("legacy1", policy.machine_principal("api_token"))


def test_entitled_follows_give_to_for_every_role_but_not_disabled_profiles(house):
    t, t2 = config.get_bot("tmpl"), config.get_bot("tmpl2")
    for who in (house.owner, house.admin2, house.uma, house.basic):
        assert characters.entitled(accounts.get_profile(who.id), t)
    assert characters.entitled(accounts.get_profile(house.uma.id), t2)
    assert characters.entitled(accounts.get_profile(house.basic.id), t2)      # Basic included
    assert not characters.entitled(accounts.get_profile(house.owner.id), t2)
    assert not characters.entitled(accounts.get_profile(house.off.id), t2)    # disabled
    assert not characters.entitled(accounts.get_profile(house.off.id), t)
    # only a template has entitlements
    assert not characters.entitled(accounts.get_profile(house.uma.id), config.get_bot("legacy1"))
    assert not characters.entitled(accounts.get_profile(house.uma.id), None)
    assert not characters.entitled(None, t)


def test_may_manage(house):
    t = config.get_bot("tmpl")
    assert characters.may_manage(t, P(house.owner)) and characters.may_manage(t, P(house.admin2))
    assert not characters.may_manage(t, P(house.uma)) and not characters.may_manage(t, P(house.basic))
    cp = config.get_bot("p0uma00copy")
    assert characters.may_manage(cp, P(house.uma))
    assert not characters.may_manage(cp, P(house.owner))                       # not even the Owner
    assert not characters.may_manage(cp, policy.machine_principal("api_token"))
    assert not characters.may_manage(None, P(house.owner))


def test_copy_of_and_machine_resolution(house):
    assert characters.copy_of("tmpl", house.uma.id).id == "p0uma00copy"
    assert characters.copy_of("tmpl", house.admin2.id) is None
    assert characters.copy_of("tmpl", None) is None
    assert characters.resolve_for_machine(config.get_bot("tmpl")).id == "p0owner0copy"
    assert characters.resolve_for_machine(config.get_bot("tmpl2")) is None     # Owner has no copy
    assert characters.resolve_for_machine(config.get_bot("bits")).id == "bits"
    assert characters.resolve_for_machine(None) is None


def test_lists_never_offer_a_template_for_chat(house):
    for who in (house.owner, house.admin2, house.uma, house.basic):
        ids = {b.id for b in characters.visible_bots(P(who))}
        assert not ids & {"tmpl", "tmpl2"}, who.name
        ids = {b.id for b in characters.usable_bots(P(who))}
        assert not ids & {"tmpl", "tmpl2"}, who.name
    mine = {"p0uma00copy", "p0owner0copy", "bits", "umaagent"}      # (the test roster adds its own)
    assert {b.id for b in characters.visible_bots(P(house.uma))} & mine == {"p0uma00copy"}
    assert {b.id for b in characters.visible_bots(P(house.owner))} & mine == {"p0owner0copy", "bits"}


def test_admin_template_views(house):
    assert {b.id for b in characters.template_bots(P(house.owner))} == {"tmpl", "tmpl2"}
    assert {b.id for b in characters.template_bots(P(house.admin2))} == {"tmpl", "tmpl2"}
    assert characters.template_bots(P(house.uma)) == []
    assert characters.template_bots(P(house.basic)) == []
    assert characters.template_bots(None) == []
    t = config.get_bot("tmpl")
    assert characters.may_view_item(t, P(house.admin2))
    # a template given to Uma shows its picture to her ("Get my copy" row); one given to nobody does not
    assert characters.may_view_item(t, P(house.uma))
    config.upsert_bot(config.Bot(id="tmpl_none", name="TN", advisor=dict(PROV), give_to=[]))
    assert not characters.may_view_item(config.get_bot("tmpl_none"), P(house.uma))
    assert characters.may_list_manage(t, P(house.admin2)) and not characters.may_list_manage(t, P(house.uma))
    assert not characters.may_list_manage(config.get_bot("p0uma00copy"), P(house.owner))


# --------------------------------------------------------------------------- #
# HTTP / turn gate / machine routes
# --------------------------------------------------------------------------- #


@pytest.fixture
def hh(world):
    """Owner (Olive), Uma (User), a machine; templates, copies and an Owner agent bot."""
    owner = world.create_owner(world.client())
    p = world.person(owner, "Uma", "user", pin="2222")
    from agents_world import sibling
    uma = sibling(world, owner, ("203.0.113.170", 49100))
    assert world.login(uma, profile_id=p["id"], pin="2222").status_code == 200
    oid = accounts.get_protected_admin().id
    config.upsert_bot(config.Bot(id="tmpl", name="Tmpl", advisor=dict(PROV), give_to="everyone"))
    config.upsert_bot(config.Bot(id="tmplnocopy", name="TN", advisor=dict(PROV), give_to=[]))
    config.upsert_bot(config.new_copy(config.get_bot("tmpl"), id="p0owner0copy", owner_profile=oid))
    config.upsert_bot(config.new_copy(config.get_bot("tmpl"), id="p0uma00copy", owner_profile=p["id"]))
    # the 4.2 shape of Bits / Doxy / jobboard: agent bots personal to the protected Admin
    for bid in ("bits", "Doxy", "jobboard"):
        config.upsert_bot(config.Bot(id=bid, name=bid, scope="personal", owner_profile=oid))
    # the test roster's own "main" (agent, reactions on) is the Owner's too
    config.update_bot("main", lambda b: __import__("dataclasses").replace(
        b, scope="personal", owner_profile=oid))
    cfg = auth.load()
    cfg.api_token = TOKEN
    cfg.machine_loopback_trust = False
    auth._write(cfg)
    auth._bust_cache()
    machine = world.client(world.LOOPBACK, origin=False)
    machine.headers.update({"X-API-Key": TOKEN})
    ownership.forget_all()
    return SimpleNamespace(owner=owner, uma=uma, machine=machine, oid=oid, uid=p["id"], world=world)


def test_crons_post_into_a_protected_admin_personal_agent_bot(hh):
    for bid in ("main", "bits", "Doxy", "jobboard"):
        r = hh.machine.post("/api/inject", json={"bot_id": bid, "content": f"morning {bid}"})
        assert r.status_code == 200, (bid, r.text)
        tid = r.json()["thread_id"]
        assert tid.startswith(f"daily-{bid}-")                       # the cron-compatible id
        got = hh.owner.get(f"/api/threads/{tid}")
        assert got.status_code == 200 and got.json()["bot_id"] == bid
        assert hh.uma.get(f"/api/threads/{tid}").status_code == 404
        d = hh.machine.post("/api/daily", json={"bot_id": bid})
        assert d.status_code == 200 and d.json()["thread"]["id"] == tid


def test_machine_post_by_template_id_goes_to_the_owners_copy(hh):
    r = hh.machine.post("/api/inject", json={"bot_id": "tmpl", "content": "hello"})
    assert r.status_code == 200, r.text
    tid = r.json()["thread_id"]
    assert hh.owner.get(f"/api/threads/{tid}").json()["bot_id"] == "p0owner0copy"
    d = hh.machine.post("/api/daily", json={"bot_id": "tmpl"})
    assert d.status_code == 200 and d.json()["thread"]["bot_id"] == "p0owner0copy"
    assert d.json()["thread"]["id"] == tid


def test_machine_post_by_template_id_without_a_copy_is_409(hh):
    for path, body in (("/api/inject", {"bot_id": "tmplnocopy", "content": "x"}),
                       ("/api/daily", {"bot_id": "tmplnocopy"})):
        r = hh.machine.post(path, json=body)
        assert r.status_code == 409, r.text
        assert r.json()["error"]["code"] == "template_has_no_copy"
        assert r.json()["template_id"] == "tmplnocopy"


def test_machine_cannot_post_into_another_profiles_personal_bot(hh):
    for path, body in (("/api/inject", {"bot_id": "p0uma00copy", "content": "x"}),
                       ("/api/daily", {"bot_id": "p0uma00copy"})):
        r = hh.machine.post(path, json=body)
        assert r.status_code == 400, r.text           # the same answer as an unknown bot_id


def test_thread_create_on_a_template_is_refused_and_names_the_callers_copy(hh):
    r = hh.uma.post("/api/threads", json={"bot_id": "tmpl"})
    assert r.status_code == 403, r.text
    body = r.json()
    assert body["error"]["code"] == "template_not_chattable" and body["copy_id"] == "p0uma00copy"
    r = hh.owner.post("/api/threads", json={"bot_id": "tmpl"})        # an Admin too
    assert r.status_code == 403 and r.json()["copy_id"] == "p0owner0copy"
    r = hh.owner.post("/api/threads", json={"bot_id": "tmplnocopy"})  # no copy: no copy_id
    assert r.status_code == 403 and "copy_id" not in r.json()
    r = hh.machine.post("/api/threads", json={"bot_id": "tmpl"})      # a machine's copy is the Owner's
    assert r.status_code == 403 and r.json()["copy_id"] == "p0owner0copy"


def test_thread_create_on_the_callers_own_copy_and_agent_bot_works(hh):
    r = hh.uma.post("/api/threads", json={"bot_id": "p0uma00copy"})
    assert r.status_code == 200, r.text
    assert hh.uma.post("/api/threads", json={"bot_id": "p0owner0copy"}).status_code == 404
    r = hh.owner.post("/api/threads", json={"bot_id": "bits"})
    assert r.status_code == 200, r.text
    assert hh.uma.post("/api/threads", json={"bot_id": "bits"}).status_code == 404


def test_http_lists_hide_templates_from_chat_and_show_them_to_managers(hh):
    for c, want_copy in ((hh.owner, "p0owner0copy"), (hh.uma, "p0uma00copy")):
        ids = {b["id"] for b in c.get("/api/bots").json()["bots"]}
        assert want_copy in ids and "tmpl" not in ids and "tmplnocopy" not in ids
        ids = {b["id"] for b in c.get("/api/characters").json()["characters"]}
        assert want_copy in ids and not ids & {"tmpl", "tmplnocopy"}
        with c.websocket_connect("/ws") as ws:
            hello = ws.receive_json()
            hids = {b["id"] for b in hello["bots"]}
            assert want_copy in hids and not hids & {"tmpl", "tmplnocopy"}
    # the manager views
    assert {"tmpl", "tmplnocopy"} <= {b["id"] for b in hh.owner.get("/api/characters/all").json()["characters"]}
    assert {"tmpl", "tmplnocopy"} <= {b["id"] for b in hh.owner.get("/api/bots/all").json()["bots"]}
    r = hh.uma.get("/api/bots/all")
    if r.status_code == 200:                                   # a User may not manage; if it can read
        assert not {b["id"] for b in r.json()["bots"]} & {"tmpl", "tmplnocopy"}
    # a template's picture is a manager's to see, nobody else's
    assert hh.uma.get("/api/bots/tmpl/avatar").status_code in (403, 404)
    assert hh.owner.get("/api/bots/tmpl/avatar").status_code in (200, 404)
    assert hh.owner.get("/api/bots/tmpl/avatar").status_code != 403


def test_the_bots_frame_is_narrowed_per_socket(hh):
    rows = [{"id": i} for i in ("tmpl", "p0owner0copy", "p0uma00copy", "bits")]
    with hh.owner.websocket_connect("/ws") as o_ws, hh.uma.websocket_connect("/ws") as u_ws:
        o_ws.receive_json(); u_ws.receive_json()
        hh.owner.portal.call(lambda: main.manager.broadcast({"type": "bots", "bots": rows}))
        got = {}
        for name, ws in (("o", o_ws), ("u", u_ws)):
            ws.send_json({"type": "ping"})
            while True:
                f = ws.receive_json()
                if f.get("type") == "pong":
                    break
                if f.get("type") == "bots":
                    got[name] = {b["id"] for b in f["bots"]}
    assert got["o"] == {"p0owner0copy", "bits"}
    assert got["u"] == {"p0uma00copy"}


class _Reached(Exception):
    pass


def _run_turn(hh, bot_id, monkeypatch):
    """Run the turn entry point on a thread of ``bot_id``; return (error codes sent, reached)."""
    sent: list[dict] = []

    async def fake_broadcast(frame, *a, **k):
        sent.append(frame)

    async def reached(*a, **k):
        raise _Reached

    monkeypatch.setattr(main.manager, "broadcast", fake_broadcast)
    monkeypatch.setattr(main, "_compose_agent_text", reached)

    async def go():
        try:
            await main._run_agent_turn("t-x", bot_id, "hi")
        except _Reached:
            return True
        return False

    got = hh.owner.portal.call(go)
    return [f.get("code") for f in sent if f.get("type") == "error"], got


def test_turn_gate_lets_the_protected_admins_agent_bot_answer(hh, monkeypatch):
    codes, reached = _run_turn(hh, "bits", monkeypatch)      # BL-1
    assert reached and not codes


def test_turn_gate_refuses_a_template(hh, monkeypatch):
    codes, reached = _run_turn(hh, "tmpl", monkeypatch)
    assert not reached and codes == ["template_not_chattable"]


def test_turn_gate_refuses_a_personal_agent_bot_of_anyone_else(hh, monkeypatch):
    config.upsert_bot(config.Bot(id="umaagent", name="UA", scope="personal", owner_profile=hh.uid))
    codes, reached = _run_turn(hh, "umaagent", monkeypatch)
    assert not reached and codes == ["agent_owner_only"]


def test_turn_gate_still_answers_a_personal_direct_copy(hh, monkeypatch):
    # the direct path goes on to the model call; it must get past the gate
    async def boom(*a, **k):
        raise _Reached
    monkeypatch.setattr(main.advisor, "run_turn", boom, raising=False)
    codes, reached = _run_turn(hh, "p0uma00copy", monkeypatch)
    assert reached and not codes


def test_owner_agent_bot_over_the_socket(hh):
    # /ws: the Owner may start a chat with Bits; Uma may not see it at all
    tid = hh.owner.post("/api/threads", json={"bot_id": "bits"}).json()["id"]
    assert hh.owner.get(f"/api/threads/{tid}").status_code == 200
    assert hh.uma.get(f"/api/threads/{tid}").status_code == 404


def test_extension_agent_gate_agrees_with_may_chat(hh):
    from app import extensions
    bits = config.get_bot("bits")
    owner_pr = policy.profile_principal(accounts.get_profile(hh.oid))
    assert characters.may_chat(bits, owner_pr)
    # a profile without dispatch_agents is not "only missing the permission" on someone else's
    # personal agent bot: that stays a plain refusal
    uma_pr = policy.profile_principal(accounts.get_profile(hh.uid))
    assert not extensions._only_agents_missing(bits, uma_pr)


# --------------------------------------------------------------------------- #
# S2b: reactions reach only the profile that owns the chat
# --------------------------------------------------------------------------- #


def _drain(ws) -> list[dict]:
    ws.send_json({"type": "ping"})
    got = []
    while True:
        f = ws.receive_json()
        if f.get("type") == "pong":
            return got
        got.append(f)


def _reactions(frames):
    return [f for f in frames if f.get("type") == "reaction"]


def _thread(hh, owner_id, bot_id="main"):
    t = hh.owner.portal.call(lambda: main.db.create_thread(bot_id=bot_id, owner_profile_id=owner_id))
    ownership.remember(t.id, owner_id)
    return t.id


def _fire(hh, **kw):
    from app import reactions
    reactions.limiter.reset()
    r = hh.machine.post("/api/reactions/fire", json={"reaction": "nice", "actor_kind": "agent",
                                                     "bot_id": "main", **kw})
    assert r.status_code == 200, r.text
    from app import reactions
    reactions.limiter.reset()
    return r.json()["event"]


def test_a_reaction_on_an_owner_thread_never_reaches_another_profiles_socket(hh):
    tid = _thread(hh, hh.oid)
    with hh.owner.websocket_connect("/ws") as o_ws, hh.uma.websocket_connect("/ws") as u_ws:
        o_ws.receive_json(); u_ws.receive_json()
        ev = _fire(hh, thread_id=tid)
        o, u = _drain(o_ws), _drain(u_ws)
    assert [f["event_id"] for f in _reactions(o)] == [ev["event_id"]]
    assert _reactions(u) == []
    # nor the trace row that embeds the picture
    assert "reacted" not in str(u) and ev["image_url"] not in str(u)


def test_a_reaction_on_a_profiles_own_thread_reaches_that_profile_only(hh):
    config.upsert_bot(config.Bot(id="p0umaown000", name="UmaOwn", advisor=dict(PROV), reactions=True,
                                 scope="personal", owner_profile=hh.uid))
    tid = _thread(hh, hh.uid, bot_id="p0umaown000")
    with hh.owner.websocket_connect("/ws") as o_ws, hh.uma.websocket_connect("/ws") as u_ws:
        o_ws.receive_json(); u_ws.receive_json()
        from app import reactions
        reactions.limiter.reset()
        # not the machine route (a machine writes only the Owner's chats): the server's own
        # marker / autopilot path, which is how a profile's bot fires into that profile's chat
        ev = hh.owner.portal.call(lambda: main.fire_reaction(
            "nice", actor="Uma bot", actor_kind="agent", thread_id=tid, bot_id="p0umaown000"))
        o, u = _drain(o_ws), _drain(u_ws)
    assert [f["event_id"] for f in _reactions(u)] == [ev["event_id"]]
    assert _reactions(o) == []


def test_the_autopilot_reaction_is_scoped_like_any_other(hh):
    tid = _thread(hh, hh.oid)
    from app import reactions
    with hh.owner.websocket_connect("/ws") as o_ws, hh.uma.websocket_connect("/ws") as u_ws:
        o_ws.receive_json(); u_ws.receive_json()
        reactions.limiter.reset()
        hh.owner.portal.call(lambda: main._fire_autopilot_reaction("nice", tid, "main", "Nova"))
        o, u = _drain(o_ws), _drain(u_ws)
    assert len(_reactions(o)) == 1 and _reactions(u) == []
    assert "reacted" not in str(u)


def test_a_marker_reaction_is_scoped_like_any_other(hh):
    tid = _thread(hh, hh.oid)
    from app import reactions
    with hh.owner.websocket_connect("/ws") as o_ws, hh.uma.websocket_connect("/ws") as u_ws:
        o_ws.receive_json(); u_ws.receive_json()
        reactions.limiter.reset()
        hh.owner.portal.call(lambda: main._fire_marker_reactions(["nice"], tid, "main"))
        o, u = _drain(o_ws), _drain(u_ws)
    assert len(_reactions(o)) == 1 and _reactions(u) == []


def test_an_untargeted_pop_goes_to_the_bots_owner_only(hh):
    """No thread: the pop used to be 'every screen'; now it is the firing bot's owner's."""
    with hh.owner.websocket_connect("/ws") as o_ws, hh.uma.websocket_connect("/ws") as u_ws:
        o_ws.receive_json(); u_ws.receive_json()
        _fire(hh)                                               # Bits: the protected Admin's
        o, u = _drain(o_ws), _drain(u_ws)
        assert len(_reactions(o)) == 1 and _reactions(u) == []
        config.upsert_bot(config.Bot(id="p0umaown000", name="UmaOwn", advisor=dict(PROV),
                                     reactions=True, scope="personal", owner_profile=hh.uid))
        _fire(hh, bot_id="p0umaown000")                         # Uma's own: Uma's only
        o, u = _drain(o_ws), _drain(u_ws)
    assert len(_reactions(u)) == 1 and _reactions(o) == []


def test_the_scoper_drops_a_reaction_for_the_default_profile_principal(hh):
    """An unsigned device is the lock-target profile's principal: same rule as a signed one."""
    default = accounts.create_profile("DisPatch", Role.USER)
    pr = policy.profile_principal(accounts.get_profile(default.id))
    ownership.remember("th-owner", hh.oid)
    frame = {"type": "reaction", "thread_id": "th-owner", "bot_id": "main", "event_id": "e"}
    assert main.scope_frame_for_principal(frame, pr) is None
    assert main.scope_frame_for_principal({**frame, "thread_id": None}, pr) is None
    owner_pr = policy.profile_principal(accounts.get_profile(hh.oid))
    assert main.scope_frame_for_principal(frame, owner_pr) is not None


def test_pool_pictures_and_frames_belong_to_the_bots_owner(hh):
    """Second Admin / User never learn Bits' pool: list, prompts, refill, image, frames."""
    from app import reactions
    rid = None
    from test_reactions import _fake_pool_item
    rid = _fake_pool_item()
    # `main` is the Owner's; the test pool item lives on the default reaction bot's shelf
    listing = hh.owner.get("/api/reactions").json()["reactions"]
    assert any(r["id"] == rid for r in listing)
    assert not any(r["id"] == rid for r in hh.uma.get("/api/reactions").json()["reactions"])
    assert hh.machine.get("/api/reactions").status_code in (200, 401, 403)
    # the picture itself
    assert hh.owner.get(f"/api/reactions/{rid}/image").status_code == 200
    assert hh.uma.get(f"/api/reactions/{rid}/image").status_code == 404
    # pool admin routes need manage_characters; a profile without it is refused first, and a
    # second Admin is refused by the bot rule
    p = hh.world.person(hh.owner, "Ann", "admin", pin="3333")
    from agents_world import sibling
    ann = sibling(hh.world, hh.owner, ("203.0.113.171", 49101))
    assert hh.world.login(ann, profile_id=p["id"], pin="3333").status_code == 200
    assert not any(r["id"] == rid for r in ann.get("/api/reactions").json()["reactions"])
    for call in (lambda c: c.get("/api/reactions/pool?bot_id=main"),
                 lambda c: c.get("/api/reactions/prompts?bot_id=main"),
                 lambda c: c.post("/api/reactions/pool/refill?bot_id=main")):
        assert call(ann).status_code == 404
    assert hh.owner.get("/api/reactions/pool?bot_id=main").status_code == 200
    # a pool frame carries only the pools the socket may see
    frame = {"type": "reaction_pool", "pools": {"main": {"bot_id": "main"},
                                                "p0uma00copy": {"bot_id": "p0uma00copy"}},
             "pool": {"bot_id": "main"}}
    ann_pr = policy.profile_principal(accounts.get_profile(p["id"]))
    assert main.scope_frame_for_principal(frame, ann_pr) is None
    owner_pr = policy.profile_principal(accounts.get_profile(hh.oid))
    got = main.scope_frame_for_principal(frame, owner_pr)
    assert set(got["pools"]) == {"main"} and got["pool"]["bot_id"] == "main"
    assert reactions.pool_owner(reactions.get_for_display(rid)) is not None


def test_a_new_copy_appears_in_its_owners_bots_frame_and_no_ones_else(hh):
    """Integration (S2 scoping x S3 copy engine): sharing a template makes a copy through
    ``set_give_to``; the roster frame that follows reaches the owner's socket with the copy and
    every other socket without it."""
    import time

    from app import profiles_service
    config.upsert_bot(config.Bot(id="tmplshare", name="Share", advisor=dict(PROV), give_to=[]))

    def drain(ws):
        ws.send_json({"type": "ping"})
        frames = []
        while True:
            f = ws.receive_json()
            if f.get("type") == "pong":
                return frames
            if f.get("type") == "bots":
                frames.append({b["id"] for b in f["bots"]})

    with hh.owner.websocket_connect("/ws") as o_ws, hh.uma.websocket_connect("/ws") as u_ws:
        o_ws.receive_json(); u_ws.receive_json()
        profiles_service.set_give_to("tmplshare", [hh.uid])
        copy = profiles_service.copy_of(hh.uid, "tmplshare")
        assert copy is not None and copy.owner_profile == hh.uid
        u_seen, o_seen = [], []
        for _ in range(50):
            u_seen += drain(u_ws)
            o_seen += drain(o_ws)
            if any(copy.id in ids for ids in u_seen):
                break
            time.sleep(0.1)
        o_seen += drain(o_ws)
    assert any(copy.id in ids for ids in u_seen), "the owner never heard of its new copy"
    assert not any(copy.id in ids or "tmplshare" in ids for ids in o_seen)
    assert not any("tmplshare" in ids for ids in u_seen)
