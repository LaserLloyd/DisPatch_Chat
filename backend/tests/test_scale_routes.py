"""The paged route shapes of 4.0 scale (BUILD-PLAN-v2 §1.13.1) and the expired-image answer
(§1.13.6), through the real app: thread pages and cursors, the hello first page, message
pages, 410 for an expired image, the owner-scoped search index, write-time media_refs, and the
thread-cache prune. A profile only ever reaches its own pages, cursors and hits."""
from __future__ import annotations

import sqlite3

import pytest
from agents_world import sibling

from app import config, database, main

BOT = "pal"


def _direct_bot() -> None:
    config.upsert_bot(config.Bot(id=BOT, name="Pal", advisor={"providers": [
        {"provider": "custom", "base_url": "http://p.invalid/v1", "model": "m"}]}))
    config._invalidate_bots_cache()


def _seed(world, pid: str | None, n: int, *, bot: str = BOT, prefix: str = "t") -> list[str]:
    async def go():
        ids = []
        for i in range(n):
            t = await world.db.create_thread(bot, f"{prefix}{i}", owner_profile_id=pid)
            await world.db.add_message(t.id, "user", f"{prefix} note {i} lantern")
            ids.append(t.id)
        return ids
    return world.base.portal.call(go)


@pytest.fixture
def two(world):
    """An Admin and a User, each with 7 threads with the same character."""
    owner = world.create_owner()
    world.base = owner
    _direct_bot()
    uma = world.person(owner, "Uma", "user", pin="2222")
    u = sibling(world, owner, ("203.0.113.21", 41003))
    assert world.login(u, profile_id=uma["id"], pin="2222").status_code == 200
    from app import accounts
    admin_id = accounts.get_protected_admin().id
    a_ids = _seed(world, admin_id, 7, prefix="adm")
    u_ids = _seed(world, uma["id"], 7, prefix="uma")
    return world, owner, u, admin_id, uma["id"], a_ids, u_ids


def _page(c, **params):
    r = c.get("/api/threads", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def test_thread_pages_follow_the_cursor_with_pinned_first_and_no_duplicates(two):
    world, owner, u, admin_id, uma_id, a_ids, u_ids = two
    assert owner.patch(f"/api/threads/{a_ids[0]}", json={"pinned": True}).status_code == 200
    seen: list[str] = []
    body = _page(owner, bot_id=BOT, limit=3)
    assert body["threads"][0]["id"] == a_ids[0] and body["threads"][0]["is_pinned"]
    assert len(body["threads"]) == 4                      # the pinned one + 3
    seen += [t["id"] for t in body["threads"]]
    while body["next_cursor"]:
        body = _page(owner, bot_id=BOT, limit=3, cursor=body["next_cursor"])
        assert not any(t["is_pinned"] for t in body["threads"])
        seen += [t["id"] for t in body["threads"]]
    assert sorted(seen) == sorted(a_ids) and len(seen) == len(set(seen))
    # Every character at once (no bot_id) gives the same set.
    allc = _page(owner, limit=50)
    assert {t["id"] for t in allc["threads"]} == set(a_ids) and allc["next_cursor"] is None
    # 3.x clients: no limit = page one of 50, which on a small install is the whole list.
    old = owner.get("/api/threads", params={"bot_id": BOT}).json()
    assert {t["id"] for t in old["threads"]} == set(a_ids)


def test_a_cursor_never_reaches_another_profiles_threads(two):
    world, owner, u, admin_id, uma_id, a_ids, u_ids = two
    cur = _page(owner, bot_id=BOT, limit=2)["next_cursor"]
    # Uma replays the Admin's cursor: she gets HER threads older than that point, never his.
    body = _page(u, bot_id=BOT, limit=50, cursor=cur)
    assert not {t["id"] for t in body["threads"]} & set(a_ids)
    assert {t["id"] for t in _page(u, limit=50)["threads"]} == set(u_ids)
    assert owner.get("/api/threads", params={"cursor": "not-a-cursor"}).status_code == 400


def test_archived_view_and_include_archived(two):
    world, owner, u, admin_id, uma_id, a_ids, u_ids = two
    assert owner.delete(f"/api/threads/{a_ids[1]}").status_code == 200       # soft = archive
    rail = {t["id"] for t in _page(owner, limit=50)["threads"]}
    arch = {t["id"] for t in _page(owner, archived="true", limit=50)["threads"]}
    both = {t["id"] for t in _page(owner, bot_id=BOT, include_archived="true", limit=50)["threads"]}
    assert a_ids[1] not in rail and arch == {a_ids[1]} and both == set(a_ids)


def test_hello_carries_the_first_page_of_own_threads(two):
    world, owner, u, admin_id, uma_id, a_ids, u_ids = two
    with u.websocket_connect("/ws") as ws:
        hello = ws.receive_json()
    assert hello["type"] == "hello"
    assert {t["id"] for t in hello["threads"]} == set(u_ids) and hello["next_cursor"] is None
    for t in hello["threads"]:
        assert "owner_profile_id" not in t


def test_message_pages_take_before_and_default_to_100(two):
    world, owner, u, admin_id, uma_id, a_ids, u_ids = two
    tid = a_ids[2]

    async def more():
        for i in range(130):
            await world.db.add_message(tid, "assistant", f"m{i}")
    owner.portal.call(more)
    r = owner.get(f"/api/threads/{tid}/messages").json()
    assert len(r["messages"]) == 100 and r["has_more"]
    oldest = r["messages"][0]["id"]
    r2 = owner.get(f"/api/threads/{tid}/messages", params={"before": oldest}).json()
    r3 = owner.get(f"/api/threads/{tid}/messages", params={"before_id": oldest}).json()
    assert r2 == r3 and len(r2["messages"]) == 31 and not r2["has_more"]
    assert u.get(f"/api/threads/{tid}/messages").status_code == 404     # not hers


def test_an_expired_image_answers_410_not_404(two, monkeypatch):
    world, owner, u, admin_id, uma_id, a_ids, u_ids = two
    # /media is mounted at import time on the live directory: point it at this test's.
    from starlette.staticfiles import StaticFiles
    config.MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    for route in main.app.routes:
        if getattr(route, "name", "") == "media":
            monkeypatch.setattr(route, "app", StaticFiles(directory=str(config.MEDIA_DIR)))
    con = sqlite3.connect(world.tmp / "chats.db")
    con.execute("INSERT INTO media_expired (name, expired_at, kind) VALUES ('gone.png', '2026-01-01', 'chat_pic')")
    con.commit()
    con.close()
    r = owner.get("/media/gone.png")
    assert r.status_code == 410 and r.headers.get("X-Image-Expired") == "1"
    assert owner.get("/media/never-was.png").status_code == 404
    r = owner.get("/api/media", params={"path": str(config.MEDIA_DIR / "gone.png")})
    assert r.status_code == 410 and r.headers.get("X-Image-Expired") == "1"


def test_search_is_owner_scoped_inside_the_index(two):
    world, owner, u, admin_id, uma_id, a_ids, u_ids = two
    hits = u.get("/api/search", params={"q": "lantern"}).json()["results"]
    assert {h["thread_id"] for h in hits} == set(u_ids)
    hits = owner.get("/api/search", params={"q": "lantern"}).json()["results"]
    assert {h["thread_id"] for h in hits} == set(a_ids)
    assert all("" in h["snippet"] for h in hits)        # snippets still marked
    # Every message carries its thread's owner, and the index files it under that owner.
    con = sqlite3.connect(world.tmp / "chats.db")
    stray = con.execute("SELECT COUNT(*) FROM messages m JOIN threads t ON t.id = m.thread_id "
                        "WHERE m.owner_profile_id IS NOT t.owner_profile_id").fetchone()[0]
    con.close()
    assert stray == 0


def test_an_adopted_chat_moves_its_search_entries_with_it(world):
    """Threads made before the first Admin exist (owner NULL) are adopted when it is created;
    the threads_owner_au trigger moves their messages' owner, and the FTS entries follow."""
    c = world.client()
    _direct_bot()

    async def make():
        t = await world.db.create_thread(BOT, "early", owner_profile_id=None)
        await world.db.add_message(t.id, "user", "early lighthouse words")
        return t.id
    tid = c.portal.call(make)
    owner = world.create_owner(c)
    hits = owner.get("/api/search", params={"q": "lighthouse"}).json()["results"]
    assert [h["thread_id"] for h in hits] == [tid]
    con = sqlite3.connect(world.tmp / "chats.db")
    (own,) = con.execute("SELECT owner_profile_id FROM messages WHERE thread_id = ?", (tid,)).fetchone()
    con.close()
    assert own is not None


def test_media_refs_are_recorded_when_a_message_is_written(two):
    world, owner, u, admin_id, uma_id, a_ids, u_ids = two

    async def go():
        m = await world.db.add_message(a_ids[3], "assistant", "see [[media:/media/p1.png|x]]",
                                       media_url="/media/p2.webp")
        await world.db.update_message_content(m.id, "now /media/p3.png too")
        await world.db.merge_message_metadata(m.id, {"image": "/media/p4.png"})
    owner.portal.call(go)
    con = sqlite3.connect(world.tmp / "chats.db")
    names = {r[0] for r in con.execute("SELECT name FROM media_refs")}
    con.close()
    assert {"p1.png", "p2.webp", "p3.png", "p4.png"} <= names


def test_the_thread_cache_forgets_deleted_threads(two):
    world, owner, u, admin_id, uma_id, a_ids, u_ids = two
    main._thread_bot[a_ids[4]] = BOT
    main._thread_bot["no-such-thread"] = BOT
    assert owner.portal.call(main._prune_thread_bot) == 1
    assert "no-such-thread" not in main._thread_bot and main._thread_bot[a_ids[4]] == BOT


def test_the_3x_search_index_is_upgraded_in_place(tmp_path):
    """A 3.x database (no messages.owner_profile_id, FTS on content alone) becomes the
    owner-scoped shape: owners copied, index rebuilt, old triggers replaced."""
    p = tmp_path / "old.db"
    con = sqlite3.connect(p, isolation_level=None)
    con.executescript("""
        CREATE TABLE threads (id TEXT PRIMARY KEY, bot_id TEXT, owner_profile_id TEXT);
        CREATE TABLE messages (id TEXT PRIMARY KEY, thread_id TEXT, content TEXT);
        CREATE VIRTUAL TABLE messages_fts USING fts5(content, content='messages', content_rowid='rowid');
        CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN
            INSERT INTO messages_fts(rowid, content) VALUES (new.rowid, new.content); END;
        INSERT INTO threads VALUES ('a', 'x', 'pa'), ('b', 'x', 'pb');
        INSERT INTO messages VALUES ('1', 'a', 'harbour lights'), ('2', 'b', 'harbour walls');
    """)
    assert database.fts_needs_upgrade(con)
    assert database.ensure_owner_fts(con) == "upgraded"
    assert not database.fts_needs_upgrade(con)
    assert database.ensure_owner_fts(con) == "ok"                 # idempotent, no rebuild
    hits = con.execute("SELECT rowid FROM messages_fts WHERE messages_fts MATCH ?",
                       ['content : ("harbour"*) AND ' + database.fts_owner_term("pa")]).fetchall()
    assert hits == [(1,)]
    con.execute("INSERT INTO messages (id, thread_id, content, owner_profile_id) VALUES ('3', 'a', 'harbour fog', 'pa')")
    con.execute("UPDATE threads SET owner_profile_id = 'pc' WHERE id = 'a'")
    moved = con.execute("SELECT rowid FROM messages_fts WHERE messages_fts MATCH ?",
                        ['content : ("harbour"*) AND ' + database.fts_owner_term("pc")]).fetchall()
    assert sorted(moved) == [(1,), (3,)]
    # A lost row is healed on the next start (not a rebuild on every start, as 3.x did).
    con.execute("INSERT INTO messages_fts(messages_fts, rowid, content, owner_profile_id) "
                "VALUES ('delete', 3, 'harbour fog', 'pc')")
    assert database.ensure_owner_fts(con) == "healed"
    con.close()


def test_the_lifespan_opens_the_database_of_the_current_data_dir(tmp_path, monkeypatch):
    """The import-time Database follows config.DATA_DIR into the lifespan: a test (or any
    process) that moved the data directory never opens the old path's chats.db."""
    from fastapi.testclient import TestClient

    from app.database import Database
    d = tmp_path / "moved"
    d.mkdir()
    monkeypatch.setattr(config, "DATA_DIR", d)
    monkeypatch.setattr(config, "CONFIG_PATH", d / "config.yaml")
    fresh = Database(tmp_path / "elsewhere" / "chats.db")      # stands in for the import path
    monkeypatch.setattr(main, "db", fresh)
    monkeypatch.setattr(main, "_IMPORT_DB", fresh)
    with TestClient(main.app):
        assert main.db.path == d / "chats.db"
    assert not (tmp_path / "elsewhere").exists()


def test_retired_snapshots_and_reaction_images_answer_410(two):
    world, owner, u, admin_id, uma_id, a_ids, u_ids = two
    tid = a_ids[5]

    async def pin():
        await world.db.set_thread_avatar(tid, "0123abcd.png", explicit=True)
    owner.portal.call(pin)
    assert owner.get(f"/api/threads/{tid}/avatar").status_code == 404      # lost, not retired
    con = sqlite3.connect(world.tmp / "chats.db")
    con.execute("INSERT INTO media_expired (name, expired_at, kind) VALUES "
                "('avatar-snapshots/0123abcd.png', '2026-01-01', 'avatar_snapshot')")
    con.commit()
    con.close()
    r = owner.get(f"/api/threads/{tid}/avatar")
    assert r.status_code == 410 and r.headers.get("X-Image-Expired") == "1"
    assert u.get(f"/api/threads/{tid}/avatar").status_code == 404           # not hers: no hint
    from types import SimpleNamespace
    assert main._reaction_rel_names(SimpleNamespace(file="spent/happy/x1.png")) == [
        "reactions/spent/happy/x1.png"]
    assert main._reaction_rel_names(SimpleNamespace(file="../../etc/passwd")) == []
