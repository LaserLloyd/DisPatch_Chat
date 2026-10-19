"""Image Studio's "Clear from here down" (POST /api/apps/imagestudio/clear).

The ledger and the media files are seeded directly (no provider call): what is
under test is which rows leave, in which order, which files are unlinked, that a
picture still shown in a chat keeps its file, and who may do it.
"""
from __future__ import annotations

import json
import uuid

from app import config

BASE = "/api/apps/imagestudio"


def _seed(n: int) -> list[dict]:
    """``n`` ledger rows, oldest first, each with its file in the media store."""
    led_dir = config.DATA_DIR / "apps-data" / "imagestudio"
    led_dir.mkdir(parents=True, exist_ok=True)
    config.MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    rows = []
    for i in range(n):
        iid = uuid.uuid4().hex
        name = f"{iid}.png"
        (config.MEDIA_DIR / name).write_bytes(b"\x89PNG" + bytes([i]) * 64)
        rows.append({"id": iid, "url": f"/media/{name}", "prompt": f"picture {i}",
                     "provider": "minimax", "model": "image-01", "ratio": "1:1",
                     "seed": None, "optimizer": False, "reference": False, "batch": "b",
                     "created_at": f"2026-10-19T10:00:{i:02d}+00:00", "day": "2026-10-19"})
    with (led_dir / "images.jsonl").open("a", encoding="utf-8") as f:
        f.write("".join(json.dumps(r) + "\n" for r in rows))
    return rows


def _file(row) -> bool:
    return (config.MEDIA_DIR / row["url"].rsplit("/", 1)[1]).is_file()


def _ids(c) -> list[str]:
    r = c.get(BASE + "/images")
    assert r.status_code == 200, r.text
    return [x["id"] for x in r.json()["images"]]


def test_newest_first_clears_the_picture_and_every_older_one(world):
    owner = world.create_owner()
    rows = _seed(5)
    r = owner.post(BASE + "/clear", json={"id": rows[2]["id"], "order": "newest"})
    assert r.status_code == 200, r.text
    assert r.json()["deleted"] == 3 and r.json()["files"] == 3
    assert _ids(owner) == [rows[3]["id"], rows[4]["id"]]
    assert [_file(x) for x in rows] == [False, False, False, True, True]


def test_oldest_first_clears_the_picture_and_every_newer_one(world):
    owner = world.create_owner()
    rows = _seed(5)
    r = owner.post(BASE + "/clear", json={"id": rows[1]["id"], "order": "oldest"})
    assert r.status_code == 200, r.text
    assert r.json()["deleted"] == 4
    assert _ids(owner) == [rows[0]["id"]]
    assert [_file(x) for x in rows] == [True, False, False, False, False]


def test_newest_is_the_default_order(world):
    owner = world.create_owner()
    rows = _seed(3)
    assert owner.post(BASE + "/clear", json={"id": rows[0]["id"]}).json()["deleted"] == 1
    assert _ids(owner) == [rows[1]["id"], rows[2]["id"]]


def test_a_dry_run_counts_and_deletes_nothing(world):
    owner = world.create_owner()
    rows = _seed(4)
    r = owner.post(BASE + "/clear", json={"id": rows[3]["id"], "order": "newest", "dry_run": True})
    assert r.status_code == 200 and r.json()["count"] == 4 and r.json()["dry_run"] is True
    assert len(_ids(owner)) == 4 and all(_file(x) for x in rows)


def test_a_picture_sent_to_a_chat_keeps_its_file(world):
    owner = world.create_owner()
    rows = _seed(3)
    tid = owner.post("/api/threads", json={"bot_id": "main"}).json()["id"]
    r = owner.post("/api/inject", json={"thread_id": tid, "role": "assistant", "content": "look",
                                        "media_url": rows[0]["url"]})
    assert r.status_code == 200, r.text
    r = owner.post("/api/inject", json={"thread_id": tid, "role": "assistant",
                                        "content": f"inline ![x]({rows[1]['url']})"})
    assert r.status_code == 200, r.text
    r = owner.post(BASE + "/clear", json={"id": rows[2]["id"], "order": "newest"})
    assert r.status_code == 200, r.text
    assert r.json() == {"deleted": 3, "files": 1, "in_chat": 2}
    assert _ids(owner) == []
    assert [_file(x) for x in rows] == [True, True, False]


def test_unknown_ids_and_bad_orders_are_refused(world):
    owner = world.create_owner()
    rows = _seed(1)
    assert owner.post(BASE + "/clear", json={"id": "0" * 32}).status_code == 404
    assert owner.post(BASE + "/clear", json={"id": "x" * 32}).status_code == 422
    assert owner.post(BASE + "/clear", json={"id": rows[0]["id"], "order": "random"}).status_code == 422
    assert _file(rows[0])


def test_a_member_may_clear_but_not_without_manage_history(world):
    owner = world.create_owner()
    world.person(owner, "Max", "member", pin="5555")
    world.person(owner, "Nia", "member", pin="6666", denies=["manage_history"])
    rows = _seed(3)
    nia = world.client()
    assert world.login(nia, name="Nia", pin="6666").status_code == 200
    assert nia.get(BASE + "/state").json()["can_clear"] is False, "the page hides the action too"
    assert owner.get(BASE + "/state").json()["can_clear"] is True
    r = nia.post(BASE + "/clear", json={"id": rows[2]["id"]})
    assert r.status_code == 403
    assert len(_ids(owner)) == 3 and all(_file(x) for x in rows)
    max_ = world.client()
    assert world.login(max_, name="Max", pin="5555").status_code == 200
    assert max_.post(BASE + "/clear", json={"id": rows[0]["id"]}).status_code == 200
    assert len(_ids(owner)) == 2


def test_restricted_and_signed_out_callers_never_reach_it(world):
    owner = world.create_owner()
    world.person(owner, "Mia", "restricted")
    rows = _seed(2)
    kid = world.client()
    assert world.login(kid, name="Mia").status_code == 200
    assert kid.post(BASE + "/clear", json={"id": rows[1]["id"]}).status_code == 403
    assert world.client().post(BASE + "/clear", json={"id": rows[1]["id"]}).status_code in (401, 403)
    assert len(_ids(owner)) == 2 and all(_file(x) for x in rows)
