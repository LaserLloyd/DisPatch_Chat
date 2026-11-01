"""4.3 WP-A14 (H-3, OPUS-CODE-REVIEW L-6): a fired (spent) reaction picture belongs to the bot
that fired it.

A ready shelf image (``moods-<bot>/``) was already served only to whoever may see its bot. Once
fired it moves to the shared ``spent/`` store, which named no bot, so any signed-in profile that
learnt the id could read another profile's fired picture. The fire now records the firing bot
(``reactions.spent_owner``) and ``GET /api/reactions/{rid}/image`` applies the pool rule to it.
A spent picture fired before 4.3 has no record and is served exactly as before.

Run: cd backend && uv run pytest tests/test_i43_spent_reaction_gate.py --basetemp ~/.cache/cfp/42/tmp-a14
"""
from __future__ import annotations

import json

from test_access_42 import PROV, hh
from test_reactions import _fake_pool_item

from app import accounts, auth, config, main, reactions
from app.contracts.permissions import Role


def _fire_from_pool(hh, bot_id: str) -> str:
    rid = _fake_pool_item(bot_id=bot_id)
    reactions.limiter.reset()
    hh.owner.portal.call(lambda: main.fire_reaction(
        rid, actor=bot_id, actor_kind="agent", bot_id=bot_id, trace=False))
    reactions.limiter.reset()
    r = reactions.get_for_display(rid)
    assert r is not None and r.file.startswith("spent/"), "the fire moved it to spent/"
    return rid


def _default_profile_device(hh):
    """The shared "DisPatch" profile: what an unsigned device on this install lands on."""
    from agents_world import sibling
    d = accounts.create_profile("DisPatch", Role.USER)
    cfg = auth.load()
    cfg.lock_target = "default_profile"
    cfg.default_profile = d.id
    auth._write(cfg)
    auth._bust_cache()
    return sibling(hh.world, hh.owner, ("203.0.113.172", 49102))


def test_a_spent_image_fired_by_the_owners_bot_is_the_owners(hh):
    rid = _fire_from_pool(hh, "main")          # the test roster's Bits: the protected Admin's
    r = reactions.get_for_display(rid)
    assert reactions.spent_owner(r) == "main"
    assert hh.owner.get(f"/api/reactions/{rid}/image").status_code == 200
    assert hh.uma.get(f"/api/reactions/{rid}/image").status_code == 404
    dev = _default_profile_device(hh)
    assert dev.get("/api/auth/status").json().get("authenticated") is True
    assert dev.get(f"/api/reactions/{rid}/image").status_code == 404


def test_a_profiles_own_bot_fired_image_is_that_profiles(hh):
    config.upsert_bot(config.Bot(id="p0umaown000", name="UmaOwn", advisor=dict(PROV), reactions=True,
                                 scope="personal", owner_profile=hh.uid))
    config._invalidate_bots_cache()
    rid = _fire_from_pool(hh, "p0umaown000")
    assert hh.uma.get(f"/api/reactions/{rid}/image").status_code == 200
    assert hh.owner.get(f"/api/reactions/{rid}/image").status_code == 404   # not even an Admin


def test_a_pre_4_3_spent_image_keeps_todays_behaviour(hh):
    """No record (fired before 4.3): every signed-in profile is still served it."""
    rid = _fake_pool_item(bot_id="main")
    hit = reactions._resolve_pool_file(rid, reactions._moods_dir("main"))
    assert reactions._retire(hit[1]) is not None          # spent without the 4.3 record
    r = reactions.get_for_display(rid)
    assert r.file.startswith("spent/") and reactions.spent_owner(r) is None
    assert hh.owner.get(f"/api/reactions/{rid}/image").status_code == 200
    assert hh.uma.get(f"/api/reactions/{rid}/image").status_code == 200


def test_the_ledger_is_a_dotfile_no_image_walker_sees(hh):
    _fire_from_pool(hh, "main")
    ledger = config.REACTIONS_SPENT_DIR / ".owners.jsonl"
    rows = [json.loads(x) for x in ledger.read_text().splitlines()]
    assert rows and rows[-1]["bot"] == "main" and rows[-1]["f"].count("/") == 1
    from app import media_inventory
    assert all(a.path.name != ".owners.jsonl"
               for a in media_inventory.iter_reaction_spent(config.DATA_DIR))
    # a torn last line (crash mid-append) is skipped and does not swallow the next record
    with open(ledger, "a") as fh:
        fh.write('{"f": "default/x.png", "bo')
    rid = _fire_from_pool(hh, "main")
    assert reactions.spent_owner(reactions.get_for_display(rid)) == "main"
    assert hh.uma.get(f"/api/reactions/{rid}/image").status_code == 404


def test_spent_owner_ignores_everything_but_spent_files():
    assert reactions.spent_owner(reactions.Reaction(id="x", name="x", file="moods-main/a/b.png")) is None
    assert reactions.spent_owner(reactions.Reaction(id="x", name="x", file="pack/b.png")) is None
    assert reactions.spent_owner(reactions.Reaction(id="x", name="x", file="")) is None
