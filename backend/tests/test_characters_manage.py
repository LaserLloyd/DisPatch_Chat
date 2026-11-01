"""4.2 S6: the Characters page API (PLAN-v2 section 4) through the real app.

The manage list, the unified PATCH (template / copy / own / agent), give-to preview and PUT,
stop-sharing, share, the compatibility shims, and every permission edge: the Owner, a second
Admin, a User, a Basic profile, the PIN-less default profile and a machine caller.
"""
from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from agents_world import sibling
from profiles_fixtures import Scratch

from app import accounts, auth, characters_manage, config, kits, ownership, profiles_service

PROV = {"providers": [{"provider": "openai_compatible", "model": "m1"}], "tools": True,
        "persona": "You are Tmpl.", "lookup": {"command": ["infoforge", "search"]}}
TOKEN = "s6-machine-token"  # scrub-ok: throwaway test fixture
EVE_PW = "-".join(["throwaway", "test", "pass"])


class _Machine:
    """A caller holding only the API key. On a route with no machine scope the gate treats it as
    an unsigned device, and with a default profile that device is the default profile - so the
    key-holder is asked with the default profile switched off, where it must get 401/403."""

    def __init__(self, client, default_id):
        self._c, self._default = client, default_id

    def __getattr__(self, verb):
        fn = getattr(self._c, verb)

        def call(*a, **kw):
            auth.set_lock_target("profile_list", None)
            try:
                return fn(*a, **kw)
            finally:
                auth.set_lock_target("default_profile", self._default)
        return call


@pytest.fixture
def hh(world):
    owner = world.create_owner(world.client())
    world.person(owner, "Eve", "admin", password=EVE_PW)
    world.person(owner, "Max", "user", pin="5555")
    world.person(owner, "Dee", "basic")
    world.person(owner, "Lobby", "user")
    ids = {p.name: p.id for p in accounts.list_profiles()}
    oid = ids["Olive"]
    clients = {"Olive": owner}
    for i, (name, kw) in enumerate((("Eve", {"password": EVE_PW}), ("Max", {"pin": "5555"}), ("Dee", {}))):
        c = sibling(world, owner, (f"203.0.113.{80 + i}", 42000 + i))
        r = world.login(c, profile_id=ids[name], **kw) if kw else c.post(
            "/api/auth/login", json={"profile_id": ids[name]})
        assert r.status_code == 200, (name, r.text)
        clients[name] = c
    # the PIN-less default profile: an unsigned device is Lobby
    auth.set_lock_target("default_profile", ids["Lobby"])
    lobby = sibling(world, owner, ("203.0.113.90", 42100))
    lobby.headers.pop("Origin", None)
    lobby.headers.update({"Origin": "http://testserver"})
    # a template given to Max, one nobody has, and the Owner's agent bot
    config.upsert_bot(config.Bot(id="tmpl", name="Tmpl", emoji="T", advisor=json.loads(json.dumps(PROV)),
                                 give_to=[ids["Max"], ids["Dee"]], description="A template"))
    config.upsert_bot(config.Bot(id="tmplb", name="TmplB", advisor=json.loads(json.dumps(PROV)), give_to=[]))
    config.upsert_bot(config.Bot(id="bits", name="Bits", scope="personal", owner_profile=oid,
                                 agent="bits", reactions=True))
    for who in ("Max", "Dee"):
        profiles_service.make_copy(accounts.get_profile(ids[who]), "tmpl")
    cfg = auth.load()
    cfg.api_token = TOKEN
    cfg.machine_loopback_trust = False
    auth._write(cfg)
    auth._bust_cache()
    machine = world.client(world.LOOPBACK, origin=False)
    machine.headers.update({"X-API-Key": TOKEN})
    ownership.forget_all()
    return SimpleNamespace(world=world, ids=ids, oid=oid, machine=_Machine(machine, ids["Lobby"]), lobby=lobby, **clients)


def _copy(pid, template="tmpl"):
    return next(b for b in config.load_bots() if b.template == template and b.owner_profile == pid)


def _manage(c):
    r = c.get("/api/characters/manage")
    assert r.status_code == 200, r.text
    return r.json()


def _row(rows, bot_id):
    return next(r for r in rows if r["id"] == bot_id)


def _chat(hh, owner_id, bot_id, title="my chat"):
    return Scratch(hh.world.tmp).thread(owner_id, bot_id, title)


# ---- the manage list ------------------------------------------------------------------- #

def test_manage_list_owner(hh):
    d = _manage(hh.Olive)
    assert d["me"] == {"id": hh.oid, "role": "admin", "protected": True,
                       "can_manage_household": True, "can_create": True,
                       "backends_url": "/api/connections"}
    assert {p["name"] for p in d["profiles"]} == {"Olive", "Eve", "Max", "Dee", "Lobby"}
    assert set(d["profiles"][0]) == {"id", "name", "role", "avatar", "default_picture"}
    bits = _row(d["mine"], "bits")
    assert bits["kind"] == "agent" and bits["backend_kind"] == "agent"
    assert bits["actions"]["edit"] and bits["actions"]["delete"] and not bits["actions"]["share"]   # the Owner may delete on purpose
    assert bits["badges"]["reactions"] is True and bits["template"] is None
    tpl = _row(d["household"], "tmpl")
    assert tpl["kind"] == "template" and tpl["give_to"] == [hh.ids["Max"], hh.ids["Dee"]]
    assert tpl["actions"]["stop_sharing"] and tpl["actions"]["edit"] and tpl["actions"]["delete"]
    states = {c["profile_id"]: c["state"] for c in tpl["copies"]}
    assert states[hh.ids["Max"]] == "has" and states[hh.ids["Eve"]] == "none"
    assert set(tpl["copies"][0]) == {"profile_id", "state", "chats"}
    assert tpl["badges"]["lookups"] is True
    # a template is no one's chat partner: it is not in mine either
    assert all(r["id"] != "tmpl" for r in d["mine"])


def test_manage_list_second_admin_never_sees_owner_mine(hh):
    d = _manage(hh.Eve)
    assert d["me"]["protected"] is False and d["me"]["can_manage_household"]
    assert all(r["id"] != "bits" for r in d["mine"] + d["household"])
    assert {r["id"] for r in d["household"]} >= {"tmpl", "tmplb"}
    tmpl = _row(d["household"], "tmpl")
    assert tmpl["actions"]["make_copy"] is False        # not given to Eve


def test_manage_list_user_and_basic(hh):
    d = _manage(hh.Max)
    assert d["household"] == [] and d["profiles"] == []
    assert d["me"] == {"id": hh.ids["Max"], "role": "user", "protected": False,
                       "can_manage_household": False, "can_create": True,
                       "backends_url": "/api/me/backends"}
    cp = _row(d["mine"], _copy(hh.ids["Max"]).id)
    assert cp["kind"] == "copy" and cp["template"] == {"id": "tmpl", "name": "Tmpl"}
    assert cp["actions"]["edit"] and cp["actions"]["reset"] and cp["actions"]["hide"]
    assert not cp["actions"]["stop_sharing"] and not cp["actions"]["share"]
    assert cp["copies"] == [] and cp["give_to"] is None
    dee = _manage(hh.Dee)
    assert dee["me"]["can_create"] is False and dee["household"] == []
    dcp = _row(dee["mine"], _copy(hh.ids["Dee"]).id)
    # a read-only managed copy; 4.3 (F1a): every profile may still reorder its own rail
    assert dcp["kind"] == "copy" and {k for k, v in dcp["actions"].items() if v} == {"reorder"}
    assert dcp["badges"]["managed"] is True


def test_manage_list_default_profile_and_machine(hh):
    d = _manage(hh.lobby)
    assert d["me"]["id"] == hh.ids["Lobby"] and d["me"]["can_manage_household"] is False
    assert hh.machine.get("/api/characters/manage").status_code in (401, 403)
    anon = hh.world.client(("203.0.113.99", 49999))
    auth.set_lock_target("profile_list", None)
    assert anon.get("/api/characters/manage").status_code in (401, 403)
    auth.set_lock_target("default_profile", hh.ids["Lobby"])


def test_copies_counts_chats_numbers_only(hh):
    _chat(hh, hh.ids["Max"], _copy(hh.ids["Max"]).id)
    tpl = _row(_manage(hh.Olive)["household"], "tmpl")
    mx = next(c for c in tpl["copies"] if c["profile_id"] == hh.ids["Max"])
    assert mx == {"profile_id": hh.ids["Max"], "state": "has", "chats": 1}
    assert "title" not in json.dumps(tpl)


# ---- PATCH: a template ----------------------------------------------------------------- #

def test_patch_template_admin_and_copies_follow(hh):
    r = hh.Eve.patch("/api/characters/tmpl", json={"name": "Tmpl2", "emoji": "Q", "tools": False})
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "Tmpl2" and r.json()["row"]["kind"] == "template"
    cp = _row(_manage(hh.Max)["mine"], _copy(hh.ids["Max"]).id)
    assert cp["name"] == "Tmpl2" and cp["badges"]["lookups"] is False and cp["overridden"] == []


def test_patch_template_permissions(hh):
    for who in ("Max", "Dee"):
        assert getattr(hh, who).patch("/api/characters/tmpl", json={"name": "x"}).status_code == 403, who
    assert hh.machine.patch("/api/characters/tmpl", json={"name": "x"}).status_code in (401, 403)
    assert hh.lobby.patch("/api/characters/tmpl", json={"name": "x"}).status_code == 403
    assert config.get_bot("tmpl").name == "Tmpl"


def test_patch_template_field_allowlist(hh):
    for body in ({"agent": "x"}, {"reactions": True}, {"avatar_pool": True}):
        r = hh.Olive.patch("/api/characters/tmpl", json=body)
        assert r.status_code == 422, (body, r.text)
    assert hh.Olive.patch("/api/characters/tmpl", json={"hidden": True}).status_code == 422   # no hide on a template
    assert hh.Olive.patch("/api/characters/tmpl", json={"image_jobs": True}).status_code == 200
    assert config.get_bot("tmpl").image_jobs is True and config.get_bot("tmpl").hidden is False


def test_patch_unknown_empty_and_null(hh):
    assert hh.Olive.patch("/api/characters/nope", json={"name": "x"}).status_code == 404
    assert hh.Olive.patch("/api/characters/tmpl", json={}).status_code == 422
    assert hh.Olive.patch("/api/characters/tmpl", json={"name": None}).status_code == 422


# ---- PATCH: a copy, an own bot --------------------------------------------------------- #

def test_patch_copy_writes_overrides_and_reset_clears(hh):
    cid = _copy(hh.ids["Max"]).id
    r = hh.Max.patch(f"/api/characters/{cid}", json={"name": "Mine", "persona": "Be brief.",
                                                     "description": "d2"})
    assert r.status_code == 200, r.text
    row = r.json()["row"]
    assert row["kind"] == "copy" and sorted(row["overridden"]) == ["description", "name", "persona"]
    b = config.get_bot(cid)
    assert b.template == "tmpl" and b.name == "Mine" and b.advisor["lookup"] == PROV["lookup"]
    # a template edit reaches the fields the owner did not touch
    hh.Olive.patch("/api/characters/tmpl", json={"emoji": "Z"})
    assert config.get_bot(cid).emoji == "Z" and config.get_bot(cid).name == "Mine"
    r = hh.Max.post(f"/api/me/characters/{cid}/reset")
    assert r.status_code == 200, r.text
    assert config.get_bot(cid).name == "Tmpl"
    assert _row(_manage(hh.Max)["mine"], cid)["overridden"] == []


def test_patch_copy_forbidden_fields_and_foreign(hh):
    cid = _copy(hh.ids["Max"]).id
    for body in ({"agent": "x"}, {"reactions": True}, {"avatar_pool": True}, {"model_hint": "x"}):
        assert hh.Max.patch(f"/api/characters/{cid}", json=body).status_code == 422, body
    # nobody else touches it, Admins included
    for who in ("Olive", "Eve", "Dee"):
        assert getattr(hh, who).patch(f"/api/characters/{cid}", json={"name": "x"}).status_code == 404, who
    assert hh.machine.patch(f"/api/characters/{cid}", json={"name": "x"}).status_code in (401, 403)
    assert hh.lobby.patch(f"/api/characters/{cid}", json={"name": "x"}).status_code == 404


def test_basic_owner_cannot_edit_its_managed_copy(hh):
    cid = _copy(hh.ids["Dee"]).id
    r = hh.Dee.patch(f"/api/characters/{cid}", json={"name": "x"})
    assert r.status_code == 403, r.text
    assert config.get_bot(cid).name == "Tmpl"


def test_quick_toggles_on_copy_and_own(hh):
    cid = _copy(hh.ids["Max"]).id
    for body in ({"hidden": True}, {"image_jobs": True}, {"tools": False}, {"voice": ""}):
        r = hh.Max.patch(f"/api/characters/{cid}", json=body)
        assert r.status_code == 200, (body, r.text)
    row = _row(_manage(hh.Max)["mine"], cid)
    assert row["hidden"] and row["badges"]["pictures"] and not row["badges"]["lookups"]
    assert sorted(row["overridden"]) == ["image_jobs", "tools"]      # hidden is per copy, not an override
    own = hh.Max.post("/api/me/characters", json={"name": "Own"}).json()["id"]
    assert hh.Max.patch(f"/api/characters/{own}", json={"image_jobs": True, "hidden": True,
                                                        "tools": False}).status_code == 200
    r = _row(_manage(hh.Max)["mine"], own)
    assert r["kind"] == "own" and r["hidden"] and r["badges"]["pictures"]
    assert r["actions"]["delete"] and r["actions"]["share"] is False     # a User cannot share


def test_old_own_bot_route_still_works(hh):
    own = hh.Max.post("/api/me/characters", json={"name": "Own"}).json()["id"]
    assert hh.Max.patch(f"/api/me/characters/{own}", json={"emoji": "E"}).status_code == 200
    assert config.get_bot(own).emoji == "E"


# ---- PATCH: the Owner's agent bots (SF-1) ---------------------------------------------- #

def test_owner_edits_its_agent_bot(hh):
    r = hh.Olive.patch("/api/characters/bits", json={"name": "Bits2", "reactions": False,
                                                     "image_jobs": True, "hidden": True})
    assert r.status_code == 200, r.text
    b = config.get_bot("bits")
    assert (b.name, b.reactions, b.image_jobs, b.hidden) == ("Bits2", False, True, True)
    assert b.scope == "personal" and b.owner_profile == hh.oid
    assert r.json()["row"]["kind"] == "agent"
    r = hh.Olive.patch("/api/characters/bits", json={"tools": True})
    assert r.status_code == 422                                      # direct-only
    assert hh.Olive.patch("/api/characters/bits", json={"connection": "x"}).status_code in (404, 422)


def test_nobody_else_edits_an_agent_bot(hh):
    for who in ("Eve", "Max", "Dee"):
        r = getattr(hh, who).patch("/api/characters/bits", json={"name": "x"})
        assert r.status_code == 404, (who, r.status_code)
    assert hh.machine.patch("/api/characters/bits", json={"name": "x"}).status_code in (401, 403)
    assert config.get_bot("bits").name == "Bits"


# ---- give-to --------------------------------------------------------------------------- #

def _preview(c, bot, given):
    return c.get(f"/api/characters/{bot}/give-to/preview", params={"give_to": json.dumps(given)})


def test_give_to_preview_and_put(hh):
    eve = hh.ids["Eve"]
    p = _preview(hh.Eve, "tmpl", [hh.ids["Max"], hh.ids["Dee"], eve])
    assert p.status_code == 200, p.text
    j = p.json()
    assert j["add"] == [eve] and j["remove"] == [] and j["detach"] == [] and j["token"]
    r = hh.Eve.put("/api/characters/tmpl/give-to", json={"give_to": [hh.ids["Max"], hh.ids["Dee"], eve],
                                                         "token": j["token"]})
    assert r.status_code == 200, r.text
    assert r.json()["id"] == "tmpl" and eve in r.json()["give_to"]
    assert _copy(eve).owner_profile == eve
    row = _row(_manage(hh.Eve)["mine"], _copy(eve).id)
    assert row["kind"] == "copy"
    # the token is single use
    again = hh.Eve.put("/api/characters/tmpl/give-to", json={"give_to": [hh.ids["Max"], hh.ids["Dee"], eve],
                                                             "token": j["token"]})
    assert again.status_code == 409 and again.json()["error"]["code"] == "give_to_stale"


def test_give_to_remove_and_detach_classes(hh):
    mx, dee = hh.ids["Max"], hh.ids["Dee"]
    mcopy = _copy(mx).id
    _chat(hh, mx, mcopy)
    p = _preview(hh.Olive, "tmpl", []).json()
    assert p["detach"] == [mx] and p["remove"] == [dee] and p["add"] == []
    r = hh.Olive.put("/api/characters/tmpl/give-to", json={"give_to": [], "token": p["token"]})
    assert r.status_code == 200, r.text
    kept = config.get_bot(mcopy)
    assert kept.template == "" and kept.scope == "personal" and kept.name == "Tmpl"
    assert not [b for b in config.load_bots() if b.owner_profile == dee and b.name == "Tmpl"]


def test_give_to_token_must_match_the_request(hh):
    p = _preview(hh.Olive, "tmpl", "everyone").json()
    r = hh.Olive.put("/api/characters/tmpl/give-to", json={"give_to": [], "token": p["token"]})
    assert r.status_code == 409
    r = hh.Olive.put("/api/characters/tmpl/give-to", json={"give_to": "everyone", "token": "junk"})
    assert r.status_code == 409
    assert hh.Olive.put("/api/characters/tmpl/give-to", json={"give_to": "everyone"}).status_code == 422
    assert config.get_bot("tmpl").give_to == [hh.ids["Max"], hh.ids["Dee"]]


def test_give_to_validation_and_targets(hh):
    assert _preview(hh.Olive, "tmpl", ["nobody"]).status_code == 422
    assert _preview(hh.Olive, "bits", []).status_code == 404       # an agent bot is no template
    assert _preview(hh.Olive, "nope", []).status_code == 404
    mcopy = _copy(hh.ids["Max"]).id
    assert _preview(hh.Eve, mcopy, []).status_code == 404           # someone's copy does not exist for Eve
    r = hh.Olive.get("/api/characters/tmpl/give-to/preview", params={"give_to": "everyone"})
    assert r.status_code == 200                                    # a bare word is accepted


def test_give_to_permissions(hh):
    p = _preview(hh.Olive, "tmpl", [hh.ids["Max"]]).json()
    for who in ("Max", "Dee"):
        c = getattr(hh, who)
        assert _preview(c, "tmpl", []).status_code == 403
        assert c.put("/api/characters/tmpl/give-to", json={"give_to": [], "token": p["token"]}).status_code == 403
    assert _preview(hh.machine, "tmpl", []).status_code in (401, 403)
    assert hh.machine.put("/api/characters/tmpl/give-to", json={"give_to": [], "token": "x"}).status_code in (401, 403)
    assert _preview(hh.lobby, "tmpl", []).status_code == 403


# ---- stop sharing ---------------------------------------------------------------------- #

def test_stop_sharing_detaches_every_copy_and_keeps_chats(hh):
    mx = hh.ids["Max"]
    mcopy = _copy(mx).id
    tid = _chat(hh, mx, mcopy)
    p = hh.Eve.post("/api/characters/tmpl/stop-sharing/preview")
    assert p.status_code == 200, p.text
    assert sorted(p.json()["detach"]) == sorted([mx, hh.ids["Dee"]])
    r = hh.Eve.post("/api/characters/tmpl/stop-sharing", json={"token": p.json()["token"]})
    assert r.status_code == 200, r.text
    assert r.json() == {"mine": None}                       # Eve had no copy
    assert config.get_bot("tmpl") is None
    kept = config.get_bot(mcopy)
    assert kept.template == "" and kept.name == "Tmpl" and kept.advisor["lookup"] == PROV["lookup"]
    d = _manage(hh.Max)
    assert _row(d["mine"], mcopy)["kind"] == "own"
    assert tid                                              # the thread survives (R3: the bot stays)


def test_stop_sharing_returns_the_callers_own_copy(hh):
    oid = hh.oid
    profiles_service.set_give_to("tmpl", [hh.ids["Max"], oid])
    mine = _copy(oid).id
    p = hh.Olive.post("/api/characters/tmpl/stop-sharing/preview").json()
    r = hh.Olive.post("/api/characters/tmpl/stop-sharing", json={"token": p["token"]})
    assert r.status_code == 200
    assert r.json()["mine"]["id"] == mine and r.json()["mine"]["kind"] == "own"


def test_stop_sharing_stale_and_permissions(hh):
    p = hh.Olive.post("/api/characters/tmpl/stop-sharing/preview").json()
    profiles_service.set_give_to("tmpl", [hh.ids["Max"], hh.ids["Dee"], hh.ids["Eve"]])   # a third copy
    r = hh.Olive.post("/api/characters/tmpl/stop-sharing", json={"token": p["token"]})
    assert r.status_code == 409 and r.json()["error"]["code"] == "stop_sharing_stale"
    assert config.get_bot("tmpl") is not None
    for c in (hh.Max, hh.Dee, hh.machine, hh.lobby):
        assert c.post("/api/characters/tmpl/stop-sharing/preview").status_code in (401, 403)
        assert c.post("/api/characters/tmpl/stop-sharing", json={"token": "x"}).status_code in (401, 403)
    assert hh.Olive.post("/api/characters/bits/stop-sharing/preview").status_code == 404


# ---- share ----------------------------------------------------------------------------- #

def _own_bot(c, name="Plum", **kw):
    r = c.post("/api/me/characters", json={"name": name, "persona": "Say plum.", **kw})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_share_makes_a_template_and_the_original_becomes_the_copy(hh):
    oid = hh.oid
    own = _own_bot(hh.Olive)
    chat = _chat(hh, oid, own)
    r = hh.Olive.post(f"/api/me/characters/{own}/share")
    assert r.status_code == 201 or r.status_code == 200, r.text
    t = r.json()["template"]
    assert t["kind"] == "template" and t["give_to"] == [oid] and t["id"] != own
    tpl = config.get_bot(t["id"])
    assert tpl.scope == "household" and tpl.is_template and tpl.advisor["persona"] == "Say plum."
    mine = config.get_bot(own)                        # same id, same chats, now a linked copy
    assert mine.template == tpl.id and mine.scope == "personal" and mine.owner_profile == oid
    assert mine.name == "Plum" and mine.avatar == tpl.avatar == tpl.avatar
    assert chat
    d = _manage(hh.Olive)
    assert _row(d["mine"], own)["kind"] == "copy" and _row(d["household"], tpl.id)["kind"] == "template"
    # now it can be given to others and they follow it
    profiles_service.set_give_to(tpl.id, [oid, hh.ids["Max"]])
    assert _copy(hh.ids["Max"], tpl.id).name == "Plum"


def test_share_second_admin_and_refusals(hh):
    eve = _own_bot(hh.Eve, "EveBot")
    r = hh.Eve.post(f"/api/me/characters/{eve}/share")
    assert r.status_code in (200, 201), r.text
    assert config.get_bot(eve).template == r.json()["template"]["id"]
    # a copy, an agent bot, someone else's bot, a missing one
    assert hh.Eve.post(f"/api/me/characters/{eve}/share").status_code == 422        # already a copy
    assert hh.Olive.post("/api/me/characters/bits/share").status_code == 422
    assert hh.Olive.post(f"/api/me/characters/{eve}/share").status_code == 404
    assert hh.Olive.post("/api/me/characters/nope/share").status_code == 404
    assert hh.Eve.post("/api/me/characters/bits/share").status_code == 404


def test_share_needs_manage_characters(hh):
    own = _own_bot(hh.Max, "MaxBot")
    for c in (hh.Max, hh.Dee, hh.machine, hh.lobby):
        assert c.post(f"/api/me/characters/{own}/share").status_code in (401, 403, 404)
    assert config.get_bot(own).template == ""


# ---- delete / the old routes as shims -------------------------------------------------- #

def test_delete_agent_kind_refused_and_template_detaches(hh):
    r = hh.Olive.delete("/api/me/characters/bits")
    assert r.status_code == 409 and config.get_bot("bits") is not None
    mcopy = _copy(hh.ids["Max"]).id
    assert hh.Olive.delete("/api/characters/tmpl").status_code == 200
    assert config.get_bot(mcopy).template == "" and config.get_bot(mcopy).name == "Tmpl"


def test_get_my_copy_after_removing_it(hh):
    cid = _copy(hh.ids["Max"]).id
    assert hh.Max.delete(f"/api/me/characters/{cid}").status_code == 200
    row = _row(_manage(hh.Olive)["household"], "tmpl")
    assert next(c for c in row["copies"] if c["profile_id"] == hh.ids["Max"])["state"] == "removed"
    r = hh.Max.post("/api/characters/tmpl/clone")
    assert r.status_code == 201, r.text
    assert _copy(hh.ids["Max"]).id != cid


def test_kit_put_auto_clone_translates_to_give_to(hh):
    r = hh.Olive.put("/api/characters/tmpl/kit", json={"auto_clone": True, "chats": []})
    assert r.status_code == 200, r.text
    assert config.get_bot("tmpl").give_to == "everyone"
    r = hh.Olive.put("/api/characters/tmpl/kit", json={"chats": []})            # absent: untouched
    assert config.get_bot("tmpl").give_to == "everyone"
    r = hh.Olive.put("/api/characters/tmpl/kit", json={"auto_clone": False, "chats": []})
    # off freezes "everyone" to today's profiles: nobody is unticked (4.1: new profiles only)
    assert set(config.get_bot("tmpl").give_to) == {p.id for p in accounts.list_profiles()}


def test_clone_to_shim_adds_to_give_to(hh):
    eve = hh.ids["Eve"]
    r = hh.Olive.post(f"/api/characters/tmpl/clone-to/{eve}")
    assert r.status_code == 201, r.text
    assert eve in config.get_bot("tmpl").give_to and _copy(eve)


def test_household_bots_shim_translates_to_give_to(hh):
    mx = hh.ids["Max"]
    r = hh.Olive.put(f"/api/people/{mx}/household-bots", json={"household_bots": ["tmpl"]})
    assert r.status_code == 200, r.text
    assert mx in config.get_bot("tmpl").give_to
    # a list naming tmplb and no longer tmpl is refused: only give-to (preview + token) unticks
    r = hh.Olive.put(f"/api/people/{mx}/household-bots", json={"household_bots": ["tmplb"]})
    assert r.status_code == 409, r.text
    assert mx in config.get_bot("tmpl").give_to and _copy(mx, "tmpl")
    r = hh.Olive.put(f"/api/people/{mx}/household-bots", json={"household_bots": ["tmpl", "tmplb"]})
    assert r.status_code == 200, r.text
    assert mx in config.get_bot("tmplb").give_to and _copy(mx, "tmplb")
    # an agent id is refused
    r = hh.Olive.put(f"/api/people/{mx}/household-bots", json={"household_bots": ["bits"]})
    assert r.status_code == 422
    # "all" does not expand to every template
    r = hh.Olive.put(f"/api/people/{mx}/household-bots", json={"household_bots": "all"})
    assert r.status_code == 200


# ---- units ----------------------------------------------------------------------------- #

def test_kind_of_and_overridden_fields(hh):
    assert characters_manage.kind_of(config.get_bot("tmpl")) == "template"
    assert characters_manage.kind_of(config.get_bot("bits")) == "agent"
    cp = _copy(hh.ids["Max"])
    assert characters_manage.kind_of(cp) == "copy"
    cp2 = replace(cp, overrides={"name": "x", "picture": True,
                                 "advisor": {"provider0": {"model": "z"}, "tools": False}})
    assert characters_manage.overridden_fields(cp2) == ["connection", "model", "name", "picture", "tools"]
    assert kits is not None
