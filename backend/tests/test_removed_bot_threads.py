"""A chat whose character was deleted later (4.0): its owner can still open, read, rename and
delete it by id, nobody else can, and it is in no rail list (there is no character to list it
under). Nothing errors on the way."""
from __future__ import annotations

from app import config


def test_a_chat_of_a_deleted_character_stays_its_owners_and_never_errors(world):
    owner = world.create_owner(world.client())
    config.upsert_bot(config.Bot(id="gone_bot", name="Gone", advisor={"providers": []}))
    tid = owner.post("/api/threads", json={"bot_id": "gone_bot", "title": "old chat"}).json()["id"]
    config.remove_bot("gone_bot")
    assert owner.get(f"/api/threads/{tid}").status_code == 200
    r = owner.get(f"/api/threads/{tid}/messages")
    assert r.status_code == 200, r.text
    assert owner.patch(f"/api/threads/{tid}", json={"title": "renamed"}).status_code == 200
    assert all(t["id"] != tid for t in owner.get("/api/threads").json()["threads"])
    other = world.client()
    world.person(owner, "Max", "user", pin="1212")
    assert world.login(other, name="Max", pin="1212").status_code == 200
    assert other.get(f"/api/threads/{tid}/messages").status_code == 404
    assert owner.delete(f"/api/threads/{tid}", params={"hard": "true"}).status_code == 200
    assert owner.get(f"/api/threads/{tid}").status_code == 404
