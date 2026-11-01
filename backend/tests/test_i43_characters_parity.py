"""4.3 WP-F1a: Characters parity, server side (legacy D.1 + H-2).

The manage row carries ``order``, ``voice``, ``voice_rp`` and ``badges.avatar_pool``; ``actions``
gains ``reactions``, ``avatar_pool``, ``reorder`` and ``voice_extras``; ``PUT
/api/me/characters/order`` reorders the caller's own bots in the slots they already hold (no other
profile's bot moves); PATCH takes ``voice_rp`` on an own bot or a copy (a copy override); and a
give-to / stop-sharing preview token is bound to the principal and the id lists (H-2).
"""
from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import pytest
from agents_world import sibling

from app import accounts, config, extensions, ownership, profiles_service, reactions

PROV = {"providers": [{"provider": "openai_compatible", "model": "m1"}], "tools": True,
        "persona": "You are Tmpl."}
EVE_PW = "-".join(["throwaway", "test", "pass"])


@pytest.fixture
def hh(world):
    owner = world.create_owner(world.client())
    world.person(owner, "Eve", "admin", password=EVE_PW)
    world.person(owner, "Max", "user", pin="5555")
    world.person(owner, "Vic", "user", pin="6666")
    world.person(owner, "Dee", "basic")
    ids = {p.name: p.id for p in accounts.list_profiles()}
    oid = ids["Olive"]
    clients = {"Olive": owner}
    for i, (name, kw) in enumerate((("Eve", {"password": EVE_PW}), ("Max", {"pin": "5555"}),
                                    ("Vic", {"pin": "6666"}))):
        c = sibling(world, owner, (f"203.0.113.{80 + i}", 42000 + i))
        r = world.login(c, profile_id=ids[name], **kw)
        assert r.status_code == 200, (name, r.text)
        clients[name] = c
    dee = sibling(world, owner, ("203.0.113.88", 42088))
    assert dee.post("/api/auth/login", json={"profile_id": ids["Dee"]}).status_code == 200
    clients["Dee"] = dee
    config.upsert_bot(config.Bot(id="tmpl", name="Tmpl", emoji="T", voice_rp="calm",
                                 advisor=json.loads(json.dumps(PROV)), give_to=[ids["Max"], ids["Dee"]]))
    config.upsert_bot(config.Bot(id="bits", name="Bits", scope="personal", owner_profile=oid,
                                 agent="bits", reactions=True))
    config.upsert_bot(config.Bot(id="doxy", name="Doxy", scope="personal", owner_profile=oid,
                                 agent="doxy"))
    for who in ("Max", "Dee"):
        profiles_service.make_copy(accounts.get_profile(ids[who]), "tmpl")
    ownership.forget_all()
    return SimpleNamespace(world=world, ids=ids, oid=oid, **clients)


def _copy(pid, template="tmpl"):
    return next(b for b in config.load_bots() if b.template == template and b.owner_profile == pid)


def _manage(c):
    r = c.get("/api/characters/manage")
    assert r.status_code == 200, r.text
    return r.json()


def _row(rows, bot_id):
    return next(r for r in rows if r["id"] == bot_id)


def _own(c, name):
    r = c.post("/api/me/characters", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _ids():
    return [b.id for b in config.load_bots()]


@pytest.fixture
def rp_validator(monkeypatch):
    """A voice add-on stand-in: voice_rp accepts only a few style names."""
    real = extensions.field_validator

    def _rp(value: str) -> None:
        if value not in ("", "calm", "bright", "dry"):
            raise ValueError("Unknown voice style")

    monkeypatch.setattr(extensions, "field_validator",
                        lambda f: _rp if f == "voice_rp" else real(f))


# ---- the manage row ---------------------------------------------------------------------- #

def test_copy_row_carries_the_new_fields_and_reorder(hh):
    cid = _copy(hh.ids["Max"]).id
    row = _row(_manage(hh.Max)["mine"], cid)
    assert row["kind"] == "copy"
    assert isinstance(row["order"], int)
    assert row["voice"] == "" and row["voice_rp"] == "calm"
    assert row["badges"]["avatar_pool"] is False
    a = row["actions"]
    assert a["reorder"] is True and a["voice_extras"] is True
    assert a["reactions"] is False and a["avatar_pool"] is False     # a copy has no pools


def test_owner_agent_row_has_reactions_and_pool_switches(hh):
    d = _manage(hh.Olive)
    bits = _row(d["mine"], "bits")
    assert bits["actions"]["reactions"] is True and bits["actions"]["avatar_pool"] is True
    assert bits["actions"]["reorder"] is True and bits["actions"]["voice_extras"] is True
    assert bits["badges"]["avatar_pool"] is False
    tpl = _row(d["household"], "tmpl")
    assert tpl["actions"]["reorder"] is False                        # nobody chats with a template
    assert tpl["actions"]["reactions"] is False and tpl["actions"]["avatar_pool"] is False
    assert tpl["actions"]["voice_extras"] is True


def test_reactions_switch_needs_something_to_fire(hh):
    """Doxy has reactions off. With no pack entry and no pool image nothing could fire, so no
    switch; a ready pool image or a pack entry (what ``reactions.get`` resolves) makes it
    offerable. Bits (reactions on) always keeps the switch, so it can be turned off."""
    seeded = config.REACTIONS_PATH.read_text(encoding="utf-8")
    assert reactions.load().reactions                                 # the boot seeds a starter pack
    assert _row(_manage(hh.Olive)["mine"], "doxy")["actions"]["reactions"] is True
    config.REACTIONS_PATH.unlink()
    reactions.invalidate()
    assert _row(_manage(hh.Olive)["mine"], "doxy")["actions"]["reactions"] is False
    assert not config.REACTIONS_PATH.exists()                         # the GET seeded nothing
    assert _row(_manage(hh.Olive)["mine"], "bits")["actions"]["reactions"] is True
    mood = reactions._moods_dir("doxy") / "happy"
    mood.mkdir(parents=True)
    (mood / "one.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    assert _row(_manage(hh.Olive)["mine"], "doxy")["actions"]["reactions"] is True
    (mood / "one.png").unlink()
    config.REACTIONS_PATH.write_text(seeded, encoding="utf-8")
    reactions.invalidate()
    assert _row(_manage(hh.Olive)["mine"], "doxy")["actions"]["reactions"] is True


def test_other_profiles_never_get_reorder_on_rows_they_see(hh):
    d = _manage(hh.Eve)
    assert all(not r["actions"]["reorder"] for r in d["household"])


# ---- PUT /api/me/characters/order ---------------------------------------------------------- #

def test_user_reorders_own_bots_only(hh):
    cid = _copy(hh.ids["Max"]).id
    a, b = _own(hh.Max, "A"), _own(hh.Max, "B")
    before = _ids()
    r = hh.Max.put("/api/me/characters/order", json={"ids": [b, cid]})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "order": [b, cid, a]}            # unlisted keep their order, after
    after = _ids()
    assert [x for x in after if x in (cid, a, b)] == [b, cid, a]
    mine_slots = [i for i, x in enumerate(before) if x in (cid, a, b)]
    assert [i for i, x in enumerate(after) if x in (cid, a, b)] == mine_slots
    assert [x for x in after if x not in (cid, a, b)] == [x for x in before if x not in (cid, a, b)]
    assert [x["id"] for x in _manage(hh.Max)["mine"] if x["id"] in (cid, a, b)] == [b, cid, a]


def test_foreign_unknown_and_duplicate_ids_are_refused(hh):
    cid = _copy(hh.ids["Max"]).id
    eve_own = _own(hh.Eve, "EveBot")
    before = _ids()
    for bad in ("bits", "tmpl", eve_own, "nope"):
        r = hh.Max.put("/api/me/characters/order", json={"ids": [cid, bad]})
        assert r.status_code == 404 and r.json()["error"]["code"] == "bot_not_found", (bad, r.text)
    r = hh.Max.put("/api/me/characters/order", json={"ids": [cid, cid]})
    assert r.status_code == 422 and r.json()["error"]["code"] == "order_invalid", r.text
    assert _ids() == before


def test_owner_reorders_agent_bots_and_a_user_cannot(hh):
    r = hh.Olive.put("/api/me/characters/order", json={"ids": ["doxy", "bits"]})
    assert r.status_code == 200, r.text
    assert r.json()["order"][:2] == ["doxy", "bits"]
    assert [x for x in _ids() if x in ("bits", "doxy")] == ["doxy", "bits"]
    r = hh.Max.put("/api/me/characters/order", json={"ids": ["bits"]})
    assert r.status_code == 404
    r = hh.Eve.put("/api/me/characters/order", json={"ids": ["bits"]})   # a second Admin too
    assert r.status_code == 404


def test_interleaved_profiles_only_the_callers_slots_change(hh):
    m1, v1, m2, v2, m3 = (_own(hh.Max, "m1"), _own(hh.Vic, "v1"), _own(hh.Max, "m2"),
                          _own(hh.Vic, "v2"), _own(hh.Max, "m3"))
    cid = _copy(hh.ids["Max"]).id
    before = _ids()
    r = hh.Max.put("/api/me/characters/order", json={"ids": [m3, m1, m2, cid]})
    assert r.status_code == 200, r.text
    expect, it = [], iter([m3, m1, m2, cid])
    max_ids = {m1, m2, m3, cid}
    for x in before:
        expect.append(next(it) if x in max_ids else x)
    assert _ids() == expect
    assert [x for x in _ids() if x in (v1, v2)] == [v1, v2]


def test_concurrent_reorders_by_two_profiles_both_hold(hh):
    mx = [_own(hh.Max, f"m{i}") for i in range(4)]
    vx = [_own(hh.Vic, f"v{i}") for i in range(4)]
    mcopy = _copy(hh.ids["Max"]).id
    before = _ids()
    want_m = [*reversed(mx), mcopy]
    want_v = [vx[2], vx[0], vx[3], vx[1]]
    gate = threading.Barrier(2)
    errs: list = []

    def run(ids, pid):
        try:
            gate.wait()
            for _ in range(10):
                config.reorder_subset(list(reversed(ids)), pid)
                config.reorder_subset(ids, pid)
        except Exception as e:                    # pragma: no cover - reported below
            errs.append(e)

    ts = [threading.Thread(target=run, args=(want_m, hh.ids["Max"])),
          threading.Thread(target=run, args=(want_v, hh.ids["Vic"]))]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errs
    after = _ids()
    assert [x for x in after if x in want_m] == want_m
    assert [x for x in after if x in want_v] == want_v
    mine_m = set(want_m) | {mcopy}
    assert [i for i, x in enumerate(after) if x in mine_m] == [i for i, x in enumerate(before) if x in mine_m]
    assert [i for i, x in enumerate(after) if x in want_v] == [i for i, x in enumerate(before) if x in want_v]
    others = set(want_m) | set(want_v)
    assert [x for x in after if x not in others] == [x for x in before if x not in others]


def test_reorder_broadcast_reaches_only_the_caller(hh, monkeypatch):
    from app.ws import manager
    seen: list = []

    async def capture(message, wants):
        seen.append((message, wants))
        return 0

    monkeypatch.setattr(manager, "broadcast_where", capture)
    a = _own(hh.Max, "A")
    r = hh.Max.put("/api/me/characters/order", json={"ids": [a]})
    assert r.status_code == 200, r.text
    assert len(seen) == 1 and seen[0][0]["type"] == "bots"
    wants = seen[0][1]
    assert wants(SimpleNamespace(profile_id=hh.ids["Max"], protected=False))
    assert not wants(SimpleNamespace(profile_id=hh.ids["Vic"], protected=False))
    assert not wants(SimpleNamespace(profile_id=hh.oid, protected=True))


def test_basic_profile_reorders_its_read_only_copies(hh):
    """A Basic profile cannot edit its managed copies, but the order of its own rail is its own:
    reorder is use_chat (the only true action on such a row)."""
    dcp = _copy(hh.ids["Dee"]).id
    row = _row(_manage(hh.Dee)["mine"], dcp)
    assert {k for k, v in row["actions"].items() if v} == {"reorder"}
    assert hh.Dee.put("/api/me/characters/order", json={"ids": [dcp]}).status_code == 200
    assert hh.Dee.put("/api/me/characters/order", json={"ids": [_copy(hh.ids["Max"]).id]}).status_code == 404


def test_reorder_body_validation(hh):
    assert hh.Max.put("/api/me/characters/order", json={}).status_code == 422
    assert hh.Max.put("/api/me/characters/order", json={"ids": "x"}).status_code == 422
    assert hh.Max.put("/api/me/characters/order", json={"ids": [], "x": 1}).status_code == 422
    r = hh.Max.put("/api/me/characters/order", json={"ids": []})
    assert r.status_code == 200 and r.json()["ok"] is True


# ---- PATCH voice_rp ------------------------------------------------------------------------ #

def test_patch_voice_rp_on_copy_and_own(hh, rp_validator):
    cid = _copy(hh.ids["Max"]).id
    r = hh.Max.patch(f"/api/characters/{cid}", json={"voice_rp": "bright"})
    assert r.status_code == 200, r.text
    assert r.json()["row"]["voice_rp"] == "bright" and "voice_rp" in r.json()["row"]["overridden"]
    assert config.get_bot(cid).voice_rp == "bright"
    r = hh.Max.patch(f"/api/characters/{cid}", json={"voice_rp": "shouty"})
    assert r.status_code == 422, r.text
    assert config.get_bot(cid).voice_rp == "bright"
    own = _own(hh.Max, "Own")
    assert hh.Max.patch(f"/api/characters/{own}", json={"voice_rp": "dry"}).status_code == 200
    assert config.get_bot(own).voice_rp == "dry"
    assert hh.Max.patch(f"/api/characters/{own}", json={"voice_rp": "nope"}).status_code == 422
    # still nobody else's
    assert hh.Vic.patch(f"/api/characters/{cid}", json={"voice_rp": "dry"}).status_code == 404


def test_template_voice_rp_change_keeps_a_copys_own_value(hh, rp_validator):
    cid = _copy(hh.ids["Max"]).id
    assert hh.Max.patch(f"/api/characters/{cid}", json={"voice_rp": "bright"}).status_code == 200
    r = hh.Olive.patch("/api/characters/tmpl", json={"voice_rp": "dry"})
    assert r.status_code == 200, r.text
    assert config.get_bot(cid).voice_rp == "bright"
    # back to the template's value: it follows again
    assert hh.Max.patch(f"/api/characters/{cid}", json={"voice_rp": "dry"}).status_code == 200
    assert "voice_rp" not in _row(_manage(hh.Max)["mine"], cid)["overridden"]
    assert hh.Olive.patch("/api/characters/tmpl", json={"voice_rp": "calm"}).status_code == 200
    assert config.get_bot(cid).voice_rp == "calm"


def test_patch_voice_rp_without_the_addon_keeps_the_old_refusal(hh):
    """No add-on: only "" or the current value may be saved (check_extension_field)."""
    cid = _copy(hh.ids["Max"]).id
    r = hh.Max.patch(f"/api/characters/{cid}", json={"voice_rp": "bright"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "voice_addon_missing", r.text
    assert hh.Max.patch(f"/api/characters/{cid}", json={"voice_rp": "calm"}).status_code == 200


# ---- H-2: preview tokens are bound to the principal and the id lists ------------------------ #

def _preview(c, bot, given):
    return c.get(f"/api/characters/{bot}/give-to/preview", params={"give_to": json.dumps(given)})


def test_give_to_token_spent_by_another_admin_is_stale(hh):
    given = [hh.ids["Max"], hh.ids["Dee"], hh.ids["Eve"]]
    tok = _preview(hh.Eve, "tmpl", given).json()["token"]
    r = hh.Olive.put("/api/characters/tmpl/give-to", json={"give_to": given, "token": tok})
    assert r.status_code == 409 and r.json()["error"]["code"] == "give_to_stale", r.text
    assert hh.ids["Eve"] not in config.get_bot("tmpl").give_to
    r = hh.Eve.put("/api/characters/tmpl/give-to", json={"give_to": given, "token": tok})
    assert r.status_code == 200, r.text
    # single use still holds
    r = hh.Eve.put("/api/characters/tmpl/give-to", json={"give_to": given, "token": tok})
    assert r.status_code == 409


def test_stop_sharing_token_spent_by_another_admin_is_stale(hh):
    tok = hh.Eve.post("/api/characters/tmpl/stop-sharing/preview").json()["token"]
    r = hh.Olive.post("/api/characters/tmpl/stop-sharing", json={"token": tok})
    assert r.status_code == 409 and r.json()["error"]["code"] == "stop_sharing_stale", r.text
    assert config.get_bot("tmpl") is not None
    assert hh.Eve.post("/api/characters/tmpl/stop-sharing", json={"token": tok}).status_code == 200


def test_give_to_token_binds_the_id_lists_not_only_the_counts(hh, monkeypatch):
    """Same counts, different people: the confirmation the Admin saw no longer holds."""
    given = [hh.ids["Eve"]]
    tok = _preview(hh.Eve, "tmpl", given).json()["token"]
    real = profiles_service._plan

    def swapped(tpl, g):
        plan = real(tpl, g)
        return {k: ([hh.ids["Vic"]] * len(v) if k == "add" else v) for k, v in plan.items()}

    monkeypatch.setattr(profiles_service, "_plan", swapped)
    r = hh.Eve.put("/api/characters/tmpl/give-to", json={"give_to": given, "token": tok})
    assert r.status_code == 409 and r.json()["error"]["code"] == "give_to_stale", r.text


def test_mint_binds_the_actor():
    tok = profiles_service._mint("give_to", "tmpl", {"add": 1}, {"ids": ["a"]}, actor="p1")
    with pytest.raises(profiles_service.ProfileServiceError):
        profiles_service._spend("give_to", tok, "tmpl", {"add": 1}, {"ids": ["a"]}, "give_to_stale",
                                actor="p2")
    profiles_service._spend("give_to", tok, "tmpl", {"add": 1}, {"ids": ["a"]}, "give_to_stale",
                            actor="p1")
