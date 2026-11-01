"""R1 on claim (integration, 4.2): the first Admin's claim makes every household agent / app bot
that Admin's personal bot, so a Member cannot chat with it. Direct-backend bots stay household
(templates)."""
from __future__ import annotations

from app import accounts, config


def test_a_claim_makes_agent_bots_the_owners_and_nobody_elses(world, adopt_on_claim):
    owner = world.create_owner()
    oid = accounts.get_protected_admin().id
    p = world.person(owner, "Sam", "user", pin="3333")
    sam = world.client(("203.0.113.77", 47100))
    assert world.login(sam, profile_id=p["id"], pin="3333").status_code == 200
    agents = [b for b in config.load_bots() if b.id in ("alpha", "beta", "main")]
    assert agents and all(b.scope == "personal" and b.owner_profile == oid for b in agents)
    assert owner.post("/api/threads", json={"bot_id": "alpha"}).status_code == 200
    assert sam.post("/api/threads", json={"bot_id": "alpha"}).status_code == 404
