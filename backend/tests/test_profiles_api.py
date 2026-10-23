"""The profile lifecycle routes (4.0 chat-first profiles), through the real app.

The gate, the policy engine, SQLite and the config store are all live (the ``world`` fixture).
"""
from __future__ import annotations

import io
import json

import pytest
from agents_world import sibling

from app import accounts, config, profiles_service
from app.contracts.permissions import Role

LAN = ("203.0.113.50", 40000)
EVE_PW = "-".join(["throwaway", "test", "pass"])


@pytest.fixture
def team(world):
    """An Admin (the protected one), a second Admin, a User with a PIN, and a Basic profile, each
    signed in on their own client. A direct household bot ``otter`` exists."""
    owner = world.create_owner()
    world.person(owner, "Eve", "admin", password=EVE_PW)
    world.person(owner, "Max", "user", pin="5555")
    world.person(owner, "Dee", "basic")
    ids = {p.name: p.id for p in accounts.list_profiles()}
    clients = {"Olive": owner}
    for i, (name, kw) in enumerate((("Eve", {"password": EVE_PW}), ("Max", {"pin": "5555"}), ("Dee", {}))):
        c = sibling(world, owner, (f"203.0.113.{60 + i}", 41000 + i))
        r = world.login(c, profile_id=ids[name], **kw) if kw else c.post("/api/auth/login", json={"profile_id": ids[name]})
        assert r.status_code == 200, (name, r.text)
        clients[name] = c
    r = owner.post("/api/characters", json={"name": "Otter", "backend": "direct", "id": "otter"})
    assert r.status_code == 201, r.text
    # People-API creation runs profiles_service.on_profile_created: the second Admin and the User
    # start with their own Atlas, the Basic profile with nothing (SPEC: every new profile gets one).
    for name, want in (("Eve", 1), ("Max", 1), ("Dee", 0)):
        assert profiles_service.has_atlas(ids[name]) is bool(want), name
        assert profiles_service.content_counts(ids[name])["bots"] == want, name
    return {"ids": ids, **clients, "world": world}


def _bot(client, name="Mine", **extra) -> dict:
    r = client.post("/api/me/characters", json={"name": name, **extra})
    assert r.status_code == 201, r.text
    return r.json()


def _chat(client, bot_id) -> str:
    r = client.post("/api/threads", json={"bot_id": bot_id, "title": "a chat"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _reset(client, path="/api/me/reset", preview="/api/me/reset/preview"):
    p = client.get(preview)
    assert p.status_code == 200, p.text
    return client.post(path, json={"token": p.json()["token"]})


# ---- who may call ---------------------------------------------------------- #

def test_the_routes_need_a_signed_in_profile(team):
    stranger = team["world"].client(("203.0.113.99", 49999))
    for method, path in (("GET", "/api/me/reset/preview"), ("POST", "/api/me/reset"), ("GET", "/api/me/characters"),
                         ("POST", "/api/me/characters"), ("GET", "/api/me/export"),
                         ("POST", "/api/characters/otter/clone")):
        r = stranger.request(method, path, json={"token": "x", "name": "x"} if method == "POST" else None)
        assert r.status_code in (401, 403), (path, r.status_code)


def test_a_basic_profile_may_reset_and_export_but_has_no_bots_of_its_own(team):
    dee = team["Dee"]
    assert dee.get("/api/me/reset/preview").status_code == 200
    assert dee.get("/api/me/export").status_code == 200
    assert dee.get("/api/me/characters").status_code == 403
    assert dee.post("/api/me/characters", json={"name": "x"}).status_code == 403
    assert dee.post("/api/characters/otter/clone").status_code == 403


def test_people_routes_are_for_admins(team):
    max_id = team["ids"]["Max"]
    for who in ("Max", "Dee"):
        c = team[who]
        assert c.get(f"/api/people/{max_id}/reset/preview").status_code == 403
        assert c.post(f"/api/people/{max_id}/role", json={"role": "basic"}).status_code == 403
        assert c.post(f"/api/characters/otter/clone-to/{max_id}").status_code == 403


# ---- Reset ----------------------------------------------------------------- #

def test_reset_by_the_profile_itself(team):
    max_ = team["Max"]
    bot = _bot(max_)
    _chat(max_, bot["id"])
    p = max_.get("/api/me/reset/preview").json()
    # bots = the one made here plus the Atlas the profile got when it was created
    assert p["counts"] == {"bots": 2, "chats": 1, "pictures": 0, "voices": 0, "files": 0} and p["outcome"] == "atlas"
    r = max_.post("/api/me/reset", json={"token": p["token"]})
    assert r.status_code == 200, r.text
    assert r.json()["deleted"]["chats"] == 1 and r.json()["keeps_voices"] is True
    mine = max_.get("/api/me/characters").json()["characters"]
    assert len(mine) == 1 and mine[0]["name"] == "Atlas" and mine[0]["id"] != bot["id"]
    assert max_.get(f"/api/me/characters/{bot['id']}").status_code == 404
    # the audit row names ids and counts, never a name
    rows = team["Olive"].get("/api/people/audit").json()
    text = json.dumps(rows)
    assert "profile.reset" in text and "Max" not in text.split("profile.reset", 1)[1][:200]


def test_a_stale_or_forged_token_is_a_409_and_deletes_nothing(team):
    max_ = team["Max"]
    bot = _bot(max_)
    _chat(max_, bot["id"])
    token = max_.get("/api/me/reset/preview").json()["token"]
    _chat(max_, bot["id"])
    for t in (token, "junk", "a.b"):
        r = max_.post("/api/me/reset", json={"token": t})
        assert r.status_code == 409
        assert "has changed" in r.json()["detail"]
    assert profiles_service.content_counts(team["ids"]["Max"])["chats"] == 2
    assert max_.post("/api/me/reset", json={}).status_code == 422


def test_a_basic_profile_resets_to_its_assigned_household_bots(team):
    dee = team["Dee"]
    r = _reset(dee)
    assert r.status_code == 200 and r.json()["deleted"]["bots"] == 0


def test_an_admin_resets_a_profile_from_people_without_seeing_content(team):
    max_ = team["Max"]
    bot = _bot(max_, "SENT-API-secret-bot")
    _chat(max_, bot["id"])
    eve = team["Eve"]
    pid = team["ids"]["Max"]
    p = eve.get(f"/api/people/{pid}/reset/preview")
    assert p.status_code == 200
    assert "SENT-API" not in p.text and set(p.json()["counts"]) == {"bots", "chats", "pictures", "voices", "files"}
    r = eve.post(f"/api/people/{pid}/reset", json={"token": p.json()["token"]})
    assert r.status_code == 200 and r.json()["deleted"]["chats"] == 1
    assert "SENT-API" not in r.text


def test_the_protected_admin_is_out_of_reach_of_other_admins_but_can_reset_itself(team):
    olive = team["ids"]["Olive"]
    eve = team["Eve"]
    assert eve.get(f"/api/people/{olive}/reset/preview").status_code == 403
    assert eve.post(f"/api/people/{olive}/reset", json={"token": "x"}).status_code == 403
    assert _reset(team["Olive"]).status_code == 200
    assert team["Olive"].get(f"/api/people/{olive}/reset/preview").status_code == 200       # itself, from People


def test_an_unknown_profile_is_404(team):
    assert team["Eve"].get("/api/people/nobody/reset/preview").status_code == 404


# ---- My bots ---------------------------------------------------------------- #

def test_my_bots_crud(team):
    max_ = team["Max"]
    made = _bot(max_, "Mine", persona="Short answers.", emoji="🦊")
    assert made["id"].startswith("p") and made["name"] == "Mine" and made["personality"]["text"] == "Short answers."
    listed = max_.get("/api/me/characters").json()["characters"]
    assert sorted(b["name"] for b in listed) == ["Atlas", "Mine"] and made["id"] in {b["id"] for b in listed}
    assert max_.get(f"/api/me/characters/{made['id']}").json()["emoji"] == "🦊"
    r = max_.patch(f"/api/me/characters/{made['id']}", json={"name": "Renamed", "persona": ""})
    assert r.status_code == 200 and r.json()["name"] == "Renamed"
    tid = _chat(max_, made["id"])
    d = max_.delete(f"/api/me/characters/{made['id']}")
    assert d.status_code == 200 and d.json()["deleted"]["chats"] == 1
    assert max_.get(f"/api/threads/{tid}").status_code == 404
    assert [b["name"] for b in max_.get("/api/me/characters").json()["characters"]] == ["Atlas"]


def test_the_editor_only_sends_what_a_personal_bot_may_have(team):
    max_ = team["Max"]
    for field, value in (("agent", "helper"), ("audience", "all"), ("reactions", True), ("id", "mine")):
        assert max_.post("/api/me/characters", json={"name": "x", field: value}).status_code == 422
    bot = _bot(max_)
    assert max_.patch(f"/api/me/characters/{bot['id']}", json={}).status_code == 422
    assert max_.patch(f"/api/me/characters/{bot['id']}", json={"safe": True}).status_code == 422
    assert max_.patch(f"/api/me/characters/{bot['id']}", json={"name": None}).status_code == 422


def test_nobody_else_can_reach_a_personal_bot_not_even_an_admin(team):
    bot = _bot(team["Max"], "Private")
    for who in ("Olive", "Eve"):
        c = team[who]
        assert c.get(f"/api/me/characters/{bot['id']}").status_code == 404
        assert c.patch(f"/api/me/characters/{bot['id']}", json={"name": "x"}).status_code == 404
        assert c.delete(f"/api/me/characters/{bot['id']}").status_code == 404
        assert c.get(f"/api/characters/{bot['id']}").status_code == 404                # the household surface
        assert c.patch(f"/api/characters/{bot['id']}", json={"name": "x"}).status_code == 404
        assert c.delete(f"/api/characters/{bot['id']}").status_code == 404
        assert c.get(f"/api/characters/{bot['id']}/delete-preview").status_code == 404
        assert bot["id"] not in [b["id"] for b in c.get("/api/characters/all").json()["characters"]]
        order = c.put("/api/characters/order", json={"bots": [{"id": bot["id"], "order": 0}]})
        assert order.status_code == 404
    assert config.get_bot(bot["id"]).name == "Private"


def test_the_household_list_an_admin_sees_has_no_personal_bots_in_it(team):
    _bot(team["Max"], "Private")
    r = team["Olive"].put("/api/characters/order", json={"bots": [{"id": "otter", "order": 0}]})
    assert r.status_code == 200
    assert all(c["id"] in {b.id for b in config.load_bots() if b.scope == "household"}
               for c in r.json()["characters"])


def test_avatar_upload(team):
    from PIL import Image
    max_ = team["Max"]
    bot = _bot(max_)
    buf = io.BytesIO()
    Image.new("RGB", (300, 300), (10, 90, 200)).save(buf, "PNG")
    r = max_.post(f"/api/me/characters/{bot['id']}/avatar", files={"file": ("a.png", buf.getvalue(), "image/png")})
    assert r.status_code == 200 and "avatar_url" in r.json()
    assert config.get_bot(bot["id"]).avatar == f"{bot['id']}-face.png"
    bad = max_.post(f"/api/me/characters/{bot['id']}/avatar", files={"file": ("a.txt", b"hello", "text/plain")})
    assert bad.status_code == 400
    corrupt = max_.post(f"/api/me/characters/{bot['id']}/avatar", files={"file": ("a.png", b"nope", "image/png")})
    assert corrupt.status_code == 422
    other = team["Eve"].post(f"/api/me/characters/{bot['id']}/avatar", files={"file": ("a.png", buf.getvalue(), "image/png")})
    assert other.status_code == 404


# ---- copies ----------------------------------------------------------------- #

def test_a_user_copies_a_household_bot(team):
    r = team["Max"].post("/api/characters/otter/clone")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["voice_cleared"] is False and body["bot"]["id"].startswith("p")
    assert config.get_bot(body["bot"]["id"]).cloned_from == "otter"
    agent = team["Max"].post("/api/characters/alpha/clone")                 # an agent bot
    assert agent.status_code == 422 and "agent" in agent.json()["detail"]
    assert team["Max"].post("/api/characters/nope/clone").status_code == 404


def test_an_admin_copies_into_a_profile_and_gets_back_only_an_id(team):
    max_id = team["ids"]["Max"]
    r = team["Eve"].post(f"/api/characters/otter/clone-to/{max_id}")
    assert r.status_code == 201 and set(r.json()) == {"bot_id", "voice_cleared"}
    copy = config.get_bot(r.json()["bot_id"])
    assert copy.owner_profile == max_id
    assert team["Eve"].get(f"/api/me/characters/{copy.id}").status_code == 404
    assert team["Max"].get(f"/api/me/characters/{copy.id}").status_code == 200
    assert team["Eve"].post(f"/api/characters/otter/clone-to/{team['ids']['Dee']}").status_code == 422


# ---- delete preview for a household bot -------------------------------------- #

def test_deleting_a_household_bot_shows_counts_only(team):
    _chat(team["Max"], "otter")
    _chat(team["Olive"], "otter")
    r = team["Olive"].get("/api/characters/otter/delete-preview")
    assert r.status_code == 200 and r.json() == {"chats": 2, "profiles": 2}


def test_the_hide_switch(team):
    r = team["Olive"].patch("/api/characters/otter", json={"hidden": True})
    assert r.status_code == 200 and r.json()["flags"]["hidden"] is True
    assert team["Olive"].patch("/api/characters/otter", json={"hidden": False}).json()["flags"]["hidden"] is False
    made = team["Olive"].post("/api/characters", json={"name": "Quiet", "hidden": True}).json()
    assert made["flags"]["hidden"] is True


# ---- export ------------------------------------------------------------------ #

def test_export_is_the_callers_own_data_only(team):
    max_, eve = team["Max"], team["Eve"]
    bot = _bot(max_, "Mine")
    tid = _chat(max_, bot["id"])
    _chat(eve, "otter")
    r = max_.get("/api/me/export")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    doc = r.json()
    assert doc["profile"]["id"] == team["ids"]["Max"]
    assert [t["id"] for t in doc["threads"]] == [tid] and sorted(b["name"] for b in doc["bots"]) == ["Atlas", "Mine"]
    assert "api_key" not in r.text


# ---- role change and deletion -------------------------------------------------- #

def test_user_to_basic_by_an_admin_needs_the_confirmation_and_signs_the_person_out(team):
    max_id = team["ids"]["Max"]
    bot = _bot(team["Max"])
    _chat(team["Max"], bot["id"])
    eve = team["Eve"]
    p = eve.get(f"/api/people/{max_id}/role/preview", params={"role": "basic"})
    assert p.status_code == 200 and p.json()["counts"]["chats"] == 1 and p.json()["deletes"] is True
    assert eve.post(f"/api/people/{max_id}/role", json={"role": "basic"}).status_code == 409
    r = eve.post(f"/api/people/{max_id}/role", json={"role": "basic", "confirm_token": p.json()["token"],
                                                      "household_bots": ["otter"]})
    assert r.status_code == 200, r.text
    assert r.json()["role"] == "basic" and r.json()["household_bots"] == ["otter"]
    assert accounts.get_profile(max_id).role is Role.BASIC
    assert profiles_service.content_counts(max_id) == {"bots": 0, "chats": 0, "pictures": 0, "voices": 0, "files": 0}
    assert team["Max"].get("/api/me/characters").status_code in (401, 403)         # signed out


def test_basic_to_user_keeps_the_chats(team):
    dee_id = team["ids"]["Dee"]
    r = team["Eve"].post(f"/api/people/{dee_id}/role", json={"role": "user"})
    assert r.status_code == 200 and r.json()["role"] == "user"


def test_role_rules(team):
    olive, eve = team["ids"]["Olive"], team["ids"]["Eve"]
    assert team["Eve"].post(f"/api/people/{olive}/role", json={"role": "user"}).status_code == 403     # protected Admin
    assert team["Eve"].post(f"/api/people/{eve}/role", json={"role": "user"}).status_code == 409       # yourself
    assert team["Olive"].post(f"/api/people/{olive}/role", json={"role": "user"}).status_code == 409
    assert team["Olive"].post(f"/api/people/{eve}/role", json={"role": "wizard"}).status_code == 422


def test_deleting_a_profile_removes_its_content_first(team):
    max_id = team["ids"]["Max"]
    bot = _bot(team["Max"], "SENT-DEL-bot")
    _chat(team["Max"], bot["id"])
    eve = team["Eve"]
    p = eve.get(f"/api/people/{max_id}/delete/preview")
    assert p.status_code == 200 and p.json()["counts"]["bots"] == 2          # SENT-DEL-bot + its Atlas
    assert eve.post(f"/api/people/{max_id}/delete", json={"confirm_token": "nope"}).status_code == 409
    r = eve.post(f"/api/people/{max_id}/delete", json={"confirm_token": p.json()["token"]})
    assert r.status_code == 200, r.text
    assert accounts.get_profile(max_id) is None
    assert not [b for b in config.load_bots() if b.owner_profile == max_id]
    assert "SENT-DEL" not in (team["world"].tmp / "config.yaml").read_text()


def test_the_protected_admin_and_yourself_cannot_be_deleted(team):
    olive = team["ids"]["Olive"]
    assert team["Eve"].get(f"/api/people/{olive}/delete/preview").status_code == 403
    assert team["Olive"].get(f"/api/people/{olive}/delete/preview").status_code == 409
    eve = team["ids"]["Eve"]
    p = team["Eve"].get(f"/api/people/{eve}/delete/preview").json()
    assert team["Eve"].post(f"/api/people/{eve}/delete", json={"confirm_token": p["token"]}).status_code == 409


# ---- what an Admin lets a profile use ------------------------------------------ #

def test_the_household_list_of_a_user_and_a_basic_profile(team):
    eve, max_id, dee_id = team["Eve"], team["ids"]["Max"], team["ids"]["Dee"]
    r = eve.put(f"/api/people/{max_id}/household-bots", json={"household_bots": ["otter"]})
    assert r.status_code == 200 and r.json()["household_bots"] == ["otter"]
    assert eve.put(f"/api/people/{max_id}/household-bots", json={"household_bots": "all"}).json()["household_bots"] == "all"
    assert eve.put(f"/api/people/{dee_id}/household-bots", json={"household_bots": ["otter"]}).status_code == 200
    for body in ({"household_bots": "all"}, {"household_bots": ["no-such-bot"]}, {"household_bots": "some"}, {}):
        assert eve.put(f"/api/people/{dee_id}/household-bots", json=body).status_code == 422
    bot = _bot(team["Max"], "Private")
    assert eve.put(f"/api/people/{max_id}/household-bots", json={"household_bots": [bot["id"]]}).status_code == 422
    assert eve.put(f"/api/people/{team['ids']['Eve']}/household-bots", json={"household_bots": "all"}).status_code == 422
    assert team["Max"].put(f"/api/people/{dee_id}/household-bots", json={"household_bots": ["otter"]}).status_code == 403


def test_the_connections_a_profiles_own_bots_may_use(team):
    conn = team["Olive"].post("/api/connections", json={"type": "openai_compatible", "name": "Main",
                                                        "base_url": "http://127.0.0.1:9/v1"})
    assert conn.status_code in (200, 201), conn.text
    cid = conn.json()["id"]
    max_id = team["ids"]["Max"]
    r = team["Eve"].put(f"/api/people/{max_id}/backends", json={"allowed": [cid]})
    assert r.status_code == 200 and r.json()["backends"] == {"allowed": [cid]}
    assert accounts.get_profile(max_id).backends == {"allowed": [cid]}
    assert team["Eve"].put(f"/api/people/{max_id}/backends", json={"allowed": ["nope"]}).status_code == 422
    assert team["Eve"].put(f"/api/people/{max_id}/backends", json={"allowed": None}).json()["backends"] == {}
    assert team["Eve"].put(f"/api/people/{max_id}/backends", json={"extra": 1}).status_code == 422


def test_the_connections_page_names_household_users_and_only_counts_personal_bots(team):
    """The Admin's Connections card says who uses a connection: household characters by name,
    other profiles' personal bots only as a number (never their names or opaque ids)."""
    from app import config, connections
    c = connections.create(type="openai", name="Shared", api_key="sk-probe-c-0123456789",  # scrub-ok: test fixture
                           default_model="m-c")
    config.upsert_bot(config.Bot(id="house_one", name="House One",
                                 advisor={"providers": [{"connection": c.id, "model": "m-c"}]}))
    mine = _bot(team["Max"], "Secret Name", connection=c.id, model="m-c")
    row = team["Olive"].get(f"/api/connections/{c.id}").json()
    assert row["in_use_by"] == ["house_one"] and row["in_use_personal"] == 1
    listed = {x["id"]: x for x in team["Olive"].get("/api/connections").json()["connections"]}[c.id]
    assert listed["in_use_by"] == ["house_one"] and listed["in_use_personal"] == 1
    text = json.dumps(listed)
    assert mine["id"] not in text and "Secret Name" not in text
    gone = team["Olive"].delete(f"/api/connections/{c.id}")
    assert gone.status_code == 409
    assert "House One" in gone.text and "1 personal bot" in gone.text and mine["id"] not in gone.text


# ---- What the owner's bot editor may read (INT-2 lead decision) -------------------------- #

def test_a_user_reads_the_presets_and_only_its_allowed_backends(team):
    from app import connections
    a = connections.create(type="openai", name="Probe A", api_key="sk-probe-a-0123456789",  # scrub-ok: test fixture
                           default_model="m-a")
    b = connections.create(type="openai", name="Probe B", api_key="sk-probe-b-0123456789",  # scrub-ok: test fixture
                           default_model="m-b")
    max_, dee = team["Max"], team["Dee"]
    presets = max_.get("/api/me/personas")
    assert presets.status_code == 200
    assert presets.json() == team["Olive"].get("/api/characters/personas").json()
    assert max_.get("/api/characters/personas").status_code == 403       # the Admin manager stays theirs
    body = max_.get("/api/me/backends").json()
    assert body["may_choose"] is True
    assert {r["id"] for r in body["connections"]} == {a.id, b.id}
    # Never the Admin's own name for it (a name may carry an address): the kind plus a number.
    assert sorted(r["name"] for r in body["connections"]) == ["OpenAI 1", "OpenAI 2"]
    olive = team["Olive"].get("/api/me/backends")                       # an Admin's editor: real names
    assert olive.status_code == 200 and olive.json()["may_choose"] is True, olive.text
    assert sorted(r["name"] for r in olive.json()["connections"]) == ["Probe A", "Probe B"]
    text = json.dumps(body)
    for leak in ("sk-probe", "base_url", "credential", "api.openai.com", "in_use_by", "Probe"):
        assert leak not in text, leak
    # The Admin narrows the profile to one connection: only that one is listed.
    r = team["Olive"].put(f"/api/people/{team['ids']['Max']}/backends", json={"allowed": [b.id]})
    assert r.status_code == 200, r.text
    assert [r["id"] for r in max_.get("/api/me/backends").json()["connections"]] == [b.id]
    people = {p["id"]: p for p in team["Olive"].get("/api/people").json()["profiles"]}
    assert people[team["ids"]["Max"]]["backends"] == {"allowed": [b.id]}      # Profile.public()
    assert people[team["ids"]["Dee"]]["backends"] == {}
    # Without choose_backend: an empty list, never an error.
    accounts.update_profile(team["ids"]["Max"], denies=["choose_backend"])
    assert max_.get("/api/me/backends").json() == {"may_choose": False, "default": "", "connections": []}
    # A Basic profile has no bots of its own: both are refused.
    assert dee.get("/api/me/backends").status_code == 403
    assert dee.get("/api/me/personas").status_code == 403


def test_the_roster_rows_carry_scope_and_cloned_from(team):
    """The rail groups Mine / Household and hides the household original of a personal copy
    (FE-CORE railGroups), so every roster row says what it is and where it came from - and the
    automatic Atlas says it is a copy of the household ``atlas``."""
    rows = team["Max"].get("/api/bots").json()["bots"]
    mine = [b for b in rows if b["scope"] == "personal"]
    assert [b["cloned_from"] for b in mine] == ["atlas"]
    assert all("scope" in b and "cloned_from" in b for b in rows)
    assert all(b["scope"] == "household" for b in rows if b not in mine)


def test_a_new_bot_uses_a_household_connection_when_none_is_the_default(team):
    """A connection made from Settings (not the wizard) sets no install default; a User's new
    bot must still land on a working connection instead of "not connected"."""
    from app import connections, settings_store
    c = connections.create(type="openai", name="Probe C", api_key="sk-probe-c-0123456789",  # scrub-ok: test fixture
                           default_model="m-c")
    assert not settings_store.get("connections.default", "")
    bot = _bot(team["Max"], "Wired")
    assert bot["backend"]["connection"] == c.id
