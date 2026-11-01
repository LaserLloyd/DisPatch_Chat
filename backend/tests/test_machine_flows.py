"""Machine callers under 4.0 ownership (BUILD-PLAN-v2 §1.7): a machine READS only the
protected Admin's chats and WRITES (inject) into any existing thread; daily threads and
mirrors stay the protected Admin's; inject is idempotent on ``delivery_key``; a keyless call
is 401 once loopback is not trusted.

Run: cd backend && uv run pytest tests/test_machine_flows.py
"""
from __future__ import annotations

import pytest
import yaml

from app import accounts, auth, config, ownership

PROV = {"providers": [{"provider": "openai", "model": "m", "base_url": "http://127.0.0.1:9/v1"}]}
TOKEN = "machine-test-token-1"  # scrub-ok: throwaway test fixture


@pytest.fixture
def house(world):
    owner = world.create_owner(world.client())
    doc = yaml.safe_load(config.CONFIG_PATH.read_text()) or {}
    doc.setdefault("bots", []).append({"id": "chatty", "name": "Chatty", "advisor": PROV})
    config.CONFIG_PATH.write_text(yaml.safe_dump(doc))
    config._invalidate_bots_cache()
    p = world.person(owner, "Uma", "user", pin="2222")
    from agents_world import sibling
    uma = sibling(world, owner, ("203.0.113.160", 49000))
    assert world.login(uma, profile_id=p["id"], pin="2222").status_code == 200
    cfg = auth.load()
    cfg.api_token = TOKEN
    cfg.machine_loopback_trust = False
    auth._write(cfg)
    auth._bust_cache()
    machine = world.client(world.LOOPBACK, origin=False)
    machine.headers.update({"X-API-Key": TOKEN})
    keyless = world.client(world.LOOPBACK, origin=False)
    ownership.forget_all()
    return {"owner": owner, "uma": uma, "machine": machine, "keyless": keyless,
            "uma_id": p["id"], "oid": accounts.get_protected_admin().id, "world": world}


def test_keyless_machine_calls_are_401(house):
    r = house["keyless"].post("/api/inject", json={"bot_id": "chatty", "content": "x"})
    assert r.status_code == 401


def test_inject_and_daily_land_in_the_protected_admins_threads(house):
    r = house["machine"].post("/api/inject", json={"bot_id": "chatty", "content": "morning"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["duplicate"] is False and body["created"] is True
    tid = body["thread_id"]
    assert tid.startswith("daily-chatty-")
    assert house["owner"].get(f"/api/threads/{tid}").status_code == 200
    assert house["uma"].get(f"/api/threads/{tid}").status_code == 404
    r = house["machine"].post("/api/daily", json={"bot_id": "chatty"})
    assert r.status_code == 200 and r.json()["thread"]["id"] == tid


def test_inject_writes_into_any_existing_thread_but_reads_only_the_admins(house):
    t = house["uma"].post("/api/threads", json={"bot_id": "chatty", "title": "SENTINEL-UMA"}).json()
    r = house["machine"].post("/api/inject", json={"thread_id": t["id"], "content": "result"})
    assert r.status_code == 200, r.text
    assert "SENTINEL-UMA" not in r.text
    # the reading surface stays the protected Admin's only
    assert house["machine"].get(f"/api/threads/{t['id']}").status_code == 404
    lst = house["machine"].get("/api/threads", params={"bot_id": "chatty"})
    assert lst.status_code == 200 and "SENTINEL-UMA" not in lst.text


def test_inject_is_idempotent_on_delivery_key(house):
    body = {"bot_id": "chatty", "content": "the report", "delivery_key": "run-42"}
    first = house["machine"].post("/api/inject", json=body)
    second = house["machine"].post("/api/inject", json=body)
    assert first.status_code == 200 and second.status_code == 200, second.text
    assert second.json()["duplicate"] is True
    assert second.json()["message"]["id"] == first.json()["message"]["id"]
    tid = first.json()["thread_id"]
    msgs = house["owner"].get(f"/api/threads/{tid}/messages").json()["messages"]
    assert sum(1 for m in msgs if m["content"] == "the report") == 1
    bad = house["machine"].post("/api/inject", json={**body, "delivery_key": "has space"})
    assert bad.status_code == 422


def test_a_reused_key_in_a_thread_the_machine_cannot_read_echoes_nothing(house):
    t = house["uma"].post("/api/threads", json={"bot_id": "chatty"}).json()
    body = {"thread_id": t["id"], "content": "SENTINEL-PRIVATE", "delivery_key": "k-uma"}
    assert house["machine"].post("/api/inject", json=body).status_code == 200
    again = house["machine"].post("/api/inject", json=body)
    # INT-1 (BUILD-PLAN-v2 §1.7: inject returns `duplicate`): the retry into the same thread is
    # flagged a duplicate, with no message - nothing of the User's thread is echoed.
    assert again.status_code == 200 and again.json()["duplicate"] is True
    assert again.json()["message"] is None and "SENTINEL-PRIVATE" not in again.text
    # The same key aimed at another thread is someone else's delivery: refused, no echo.
    other = house["machine"].post("/api/inject", json={**body, "thread_id": "daily-elsewhere"})
    assert other.status_code == 409 and "SENTINEL-PRIVATE" not in other.text


def test_image_jobs_and_reactions_only_on_threads_the_machine_may_write(house, monkeypatch):
    monkeypatch.setattr(house["world"].main, "_image_jobs_configured", lambda: True)
    t = house["uma"].post("/api/threads", json={"bot_id": "chatty"}).json()
    r = house["machine"].post("/api/image-jobs", json={"thread_id": t["id"], "bot_id": "chatty", "prompt": "a cat"})
    assert r.status_code in (404, 403), r.text


def test_mirror_threads_are_the_protected_admins(house):
    w = house["world"]
    bot = config.get_bot("chatty")
    tid = house["owner"].portal.call(w.main._mirror_create_thread, bot, "chatty", "x", [])
    assert house["owner"].get(f"/api/threads/{tid}").status_code == 200
    assert house["uma"].get(f"/api/threads/{tid}").status_code == 404


def test_the_digest_reads_only_the_corpus_profiles_threads(house):
    w = house["world"]
    house["owner"].post("/api/threads", json={"bot_id": "chatty", "title": "admin-chat"})
    house["uma"].post("/api/threads", json={"bot_id": "chatty", "title": "uma-chat"})
    got = house["owner"].portal.call(w.main._advisor_threads_since, "chatty", "2000-01-01")
    titles = {t for _, t in got}
    assert "admin-chat" in titles and "uma-chat" not in titles
