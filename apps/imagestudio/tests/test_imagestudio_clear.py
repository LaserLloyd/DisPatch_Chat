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


def _seed(n: int, owner: str | None = None) -> list[dict]:
    """``n`` ledger rows, oldest first, each with its file in the media store (``owner``: the
    rows' ``owner_profile_id``; none = a 3.x row, which is the protected Admin's)."""
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
        if owner:
            rows[-1]["owner_profile_id"] = owner
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


def test_the_order_is_required_never_guessed(world):
    owner = world.create_owner()
    rows = _seed(3)
    r = owner.post(BASE + "/clear", json={"id": rows[0]["id"]})   # no order
    assert r.status_code == 422, r.text
    assert len(_ids(owner)) == 3 and all(_file(x) for x in rows)


def test_the_confirmed_count_must_still_hold(world):
    owner = world.create_owner()
    rows = _seed(3)
    plan = owner.post(BASE + "/clear", json={"id": rows[0]["id"], "order": "oldest", "dry_run": True}).json()
    assert plan["count"] == 3
    _seed(1)                                  # another tab made a picture after the count
    r = owner.post(BASE + "/clear", json={"id": rows[0]["id"], "order": "oldest", "expect": plan["count"]})
    assert r.status_code == 409, r.text
    assert len(_ids(owner)) == 4 and all(_file(x) for x in rows), "nothing was deleted"
    plan = owner.post(BASE + "/clear", json={"id": rows[0]["id"], "order": "oldest", "dry_run": True}).json()
    r = owner.post(BASE + "/clear", json={"id": rows[0]["id"], "order": "oldest", "expect": plan["count"]})
    assert r.status_code == 200 and r.json()["deleted"] == 4


def test_a_cross_origin_request_is_refused(world):
    owner = world.create_owner()
    rows = _seed(2)
    r = owner.post(BASE + "/clear", json={"id": rows[0]["id"], "order": "oldest"},
                   headers={"Origin": "http://evil.example"})
    assert r.status_code == 403, r.text
    assert r.json()["error"]["code"] == "csrf_origin"
    assert len(_ids(owner)) == 2 and all(_file(x) for x in rows)


def test_a_machine_caller_is_refused(world):
    owner = world.create_owner()
    rows = _seed(2)
    for c in (world.client(origin=False), world.client(world.LAN, origin=False)):
        r = c.post(BASE + "/clear", json={"id": rows[0]["id"], "order": "oldest"})
        assert r.status_code in (401, 403), (r.status_code, r.text)
    assert len(_ids(owner)) == 2 and all(_file(x) for x in rows)


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
    assert owner.post(BASE + "/clear", json={"id": "0" * 32, "order": "oldest"}).status_code == 404
    assert owner.post(BASE + "/clear", json={"id": "x" * 32, "order": "oldest"}).status_code == 422
    assert owner.post(BASE + "/clear", json={"id": rows[0]["id"], "order": "random"}).status_code == 422
    assert _file(rows[0])


def test_a_user_may_clear_their_own_but_not_without_manage_history(world):
    owner = world.create_owner()
    max_p = world.person(owner, "Max", "member", pin="5555")
    nia_p = world.person(owner, "Nia", "member", pin="6666", denies=["manage_history"])
    rows = _seed(3, nia_p["id"])
    nia = world.client()
    assert world.login(nia, name="Nia", pin="6666").status_code == 200
    assert nia.get(BASE + "/state").json()["can_clear"] is False, "the page hides the action too"
    assert owner.get(BASE + "/state").json()["can_clear"] is True
    r = nia.post(BASE + "/clear", json={"id": rows[2]["id"], "order": "newest"})
    assert r.status_code == 403
    assert len(_ids(nia)) == 3 and all(_file(x) for x in rows)
    mine = _seed(2, max_p["id"])
    max_ = world.client()
    assert world.login(max_, name="Max", pin="5555").status_code == 200
    # 4.0: another profile's picture is "already gone" for this caller (its own ledger)
    assert max_.post(BASE + "/clear", json={"id": rows[0]["id"], "order": "newest"}).status_code == 404
    assert max_.post(BASE + "/clear", json={"id": mine[0]["id"], "order": "newest"}).status_code == 200
    assert len(_ids(max_)) == 1 and len(_ids(nia)) == 3


def test_basic_and_signed_out_callers_never_reach_it(world):
    owner = world.create_owner()
    world.person(owner, "Mia", "basic")
    rows = _seed(2)
    bea = world.client()
    assert world.login(bea, name="Mia").status_code == 200
    assert bea.post(BASE + "/clear", json={"id": rows[1]["id"], "order": "newest"}).status_code == 403
    assert world.client().post(BASE + "/clear", json={"id": rows[1]["id"], "order": "newest"}).status_code in (401, 403)
    assert len(_ids(owner)) == 2 and all(_file(x) for x in rows)


def test_state_says_who_may_set_up_a_provider(world):
    """The empty state's "Set up image generation" opens Settings; only a profile
    that may change connections is offered it (3.2)."""
    owner = world.create_owner()
    world.person(owner, "Max", "member", pin="5555")
    max_ = world.client()
    assert world.login(max_, name="Max", pin="5555").status_code == 200
    assert owner.get(BASE + "/state").json()["can_configure"] is True
    assert max_.get(BASE + "/state").json()["can_configure"] is False


# ---- Delete by age: POST /clear {since: all|hour|day} (the window is the server's clock) ----

def _age(rows, minutes_ago_list):
    """Re-stamp ``rows`` (and the ledger) so row i was made minutes_ago_list[i] minutes ago."""
    from datetime import UTC, datetime, timedelta
    led = config.DATA_DIR / "apps-data" / "imagestudio" / "images.jsonl"
    stamp = {r["id"]: (datetime.now(UTC) - timedelta(minutes=m)).isoformat()
             for r, m in zip(rows, minutes_ago_list, strict=True)}
    out = []
    for line in led.read_text().splitlines():
        row = json.loads(line)
        if row["id"] in stamp:
            row["created_at"] = stamp[row["id"]]
        out.append(json.dumps(row))
    led.write_text("\n".join(out) + "\n")


def test_since_hour_removes_only_the_recent_ones(world):
    owner = world.create_owner()
    rows = _seed(4)
    _age(rows, [3000, 90, 30, 5])
    r = owner.post(BASE + "/clear", json={"since": "hour", "dry_run": True})
    assert r.status_code == 200 and r.json()["count"] == 2
    r = owner.post(BASE + "/clear", json={"since": "hour", "expect": 2})
    assert r.status_code == 200 and r.json()["deleted"] == 2 and r.json()["files"] == 2
    assert _ids(owner) == [rows[0]["id"], rows[1]["id"]]
    assert [_file(x) for x in rows] == [True, True, False, False]


def test_since_day_and_all(world):
    owner = world.create_owner()
    rows = _seed(4)
    _age(rows, [3000, 1000, 30, 5])
    assert owner.post(BASE + "/clear", json={"since": "day", "dry_run": True}).json()["count"] == 3
    assert owner.post(BASE + "/clear", json={"since": "day"}).json()["deleted"] == 3
    assert _ids(owner) == [rows[0]["id"]]
    assert owner.post(BASE + "/clear", json={"since": "all"}).json()["deleted"] == 1
    assert _ids(owner) == [] and not any(_file(x) for x in rows)


def test_since_never_touches_another_profiles_pictures(world):
    owner = world.create_owner()
    max_p = world.person(owner, "Max", "member", pin="5555")
    mine = _seed(2, max_p["id"])
    theirs = _seed(3)                        # the Owner's (unstamped 3.x rows)
    _age(mine + theirs, [5, 6, 7, 8, 9])
    max_ = world.client()
    assert world.login(max_, name="Max", pin="5555").status_code == 200
    plan = max_.post(BASE + "/clear", json={"since": "all", "dry_run": True}).json()
    assert plan["count"] == 2
    assert max_.post(BASE + "/clear", json={"since": "all", "expect": 2}).json()["deleted"] == 2
    assert _ids(max_) == [] and len(_ids(owner)) == 3
    assert [_file(x) for x in mine + theirs] == [False, False, True, True, True]


def test_since_keeps_a_chat_picture_file(world):
    owner = world.create_owner()
    rows = _seed(2)
    tid = owner.post("/api/threads", json={"bot_id": "main"}).json()["id"]
    assert owner.post("/api/inject", json={"thread_id": tid, "role": "assistant", "content": "x",
                                           "media_url": rows[0]["url"]}).status_code == 200
    r = owner.post(BASE + "/clear", json={"since": "all"})
    assert r.json() == {"deleted": 2, "files": 1, "in_chat": 1}
    assert [_file(x) for x in rows] == [True, False]


def test_since_expect_mismatch_and_bad_input(world):
    owner = world.create_owner()
    rows = _seed(3)
    assert owner.post(BASE + "/clear", json={"since": "all", "expect": 2}).status_code == 409
    assert owner.post(BASE + "/clear", json={"since": "week"}).status_code == 422
    assert owner.post(BASE + "/clear", json={}).status_code == 422
    assert owner.post(BASE + "/clear", json={"since": "all", "id": rows[0]["id"], "order": "newest"}).status_code == 422
    assert len(_ids(owner)) == 3 and all(_file(x) for x in rows)


def test_since_needs_manage_history_and_a_session(world):
    owner = world.create_owner()
    world.person(owner, "Nia", "member", pin="6666", denies=["manage_history"])
    rows = _seed(2)
    nia = world.client()
    assert world.login(nia, name="Nia", pin="6666").status_code == 200
    assert nia.post(BASE + "/clear", json={"since": "all"}).status_code == 403
    assert world.client(origin=False).post(BASE + "/clear", json={"since": "all"}).status_code in (401, 403)
    assert len(_ids(owner)) == 2 and all(_file(x) for x in rows)
