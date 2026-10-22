"""4.0 ownership, the data model (R1): every creation path stamps an owner, and the owner
never reaches a client. Nothing here is ENFORCEMENT - who may read what is unchanged.

Run: cd backend && uv run pytest tests/test_ownership_model.py
"""
from __future__ import annotations

import asyncio
import io
import json
import sqlite3

import pytest

from app import accounts, config, ownership

LAN = ("203.0.113.70", 42000)


def _owner_of(world, table: str, row_id: str):
    con = sqlite3.connect(world.tmp / "chats.db")
    try:
        row = con.execute(f"SELECT owner_profile_id FROM {table} WHERE id = ?", (row_id,)).fetchone()
    finally:
        con.close()
    assert row is not None, f"no {table} row {row_id}"
    return row[0]


def _drain(ws) -> list[dict]:
    ws.send_json({"type": "ping"})
    got = []
    while True:
        f = ws.receive_json()
        if f.get("type") == "pong":
            return got
        got.append(f)


@pytest.fixture
def people(world):
    owner = world.create_owner(world.client())
    world.person(owner, "Sam", "member", pin="5555")
    world.person(owner, "Robin", "restricted")
    from agents_world import sibling
    member = sibling(world, owner, LAN)
    assert world.login(member, name="Sam", pin="5555").status_code == 200
    ids = {p.name: p.id for p in accounts.list_profiles()}
    return {"owner": owner, "member": member, "ids": ids}


# --------------------------------------------------------------------------- #
# Stamping
# --------------------------------------------------------------------------- #


def test_rest_and_ws_thread_creation_stamp_the_callers_profile(world, people):
    ids = people["ids"]
    t_owner = people["owner"].post("/api/threads", json={"bot_id": "alpha"}).json()
    t_member = people["member"].post("/api/threads", json={"bot_id": "alpha"}).json()
    assert _owner_of(world, "threads", t_owner["id"]) == ids["Olive"]
    assert _owner_of(world, "threads", t_member["id"]) == ids["Sam"]
    with people["member"].websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"type": "create_thread", "bot_id": "beta"})
        frames = _drain(ws)
    created = [f for f in frames if f.get("type") == "thread_created"]
    assert created, frames
    assert _owner_of(world, "threads", created[0]["thread"]["id"]) == ids["Sam"]


def test_machine_posts_go_to_the_owners_daily_thread_with_the_cron_id(world, people):
    ids = people["ids"]
    machine = world.client(world.LOOPBACK, origin=False)
    r = machine.post("/api/inject", json={"bot_id": "alpha", "content": "morning"})
    assert r.status_code == 200, r.text
    tid = r.json()["thread_id"]
    assert tid.startswith("daily-alpha-") and len(tid) == len("daily-alpha-2026-10-21")
    assert _owner_of(world, "threads", tid) == ids["Olive"]
    r = machine.post("/api/daily", json={"bot_id": "beta"})
    assert r.status_code == 200, r.text
    tid = r.json()["thread"]["id"]
    assert _owner_of(world, "threads", tid) == ids["Olive"]


def test_a_non_owner_daily_thread_gets_its_own_id(world, people):
    ids = people["ids"]

    async def go():
        a, _ = await world.db.find_or_create_daily_thread("alpha", date="2026-01-02", owner=ids["Olive"])
        b, created = await world.db.find_or_create_daily_thread("alpha", date="2026-01-02", owner=ids["Sam"])
        c, again = await world.db.find_or_create_daily_thread("alpha", date="2026-01-02", owner=ids["Sam"])
        n, _ = await world.db.find_or_create_daily_thread("beta", date="2026-01-02", owner=None)
        return a, b, created, c, again, n
    a, b, created, c, again, n = people["owner"].portal.call(go)
    assert a.id == "daily-alpha-2026-01-02"
    assert b.id == f"daily-alpha-2026-01-02-{ids['Sam'][:8]}" and created
    assert c.id == b.id and not again
    assert n.id == "daily-beta-2026-01-02"
    assert _owner_of(world, "threads", b.id) == ids["Sam"]


def test_mirror_threads_belong_to_the_owner(world, people):
    ids = people["ids"]
    bot = config.get_bot("main")
    tid = people["owner"].portal.call(world.main._mirror_create_thread, bot, "main", "x", [])
    assert tid == "gw-main-main"
    assert _owner_of(world, "threads", tid) == ids["Olive"]


def test_files_uploaded_belong_to_the_uploader_and_adopted_blobs_to_the_owner(world, people):
    ids = people["ids"]
    r = people["member"].post("/api/upload", files={"file": ("notes.txt", io.BytesIO(b"hi"), "text/plain")})
    assert r.status_code == 200, r.text
    assert _owner_of(world, "files", r.json()["id"]) == ids["Sam"]
    r = people["owner"].post("/api/files", files={"file": ("a.bin", io.BytesIO(b"x" * 10), "application/x")})
    assert r.status_code == 200, r.text
    assert _owner_of(world, "files", r.json()["id"]) == ids["Olive"]
    r = people["member"].post("/api/drop", files={"file": ("d.txt", io.BytesIO(b"drop"), "text/plain")})
    assert r.status_code == 200, r.text
    assert _owner_of(world, "files", r.json()["id"]) == ids["Sam"]
    # An agent writes a blob straight into the File Server directory: adopted as the Owner's.
    (config.FILES_DIR).mkdir(parents=True, exist_ok=True)
    (world.main.FILES_DIR / "agent-report.txt").write_text("from an agent")
    people["owner"].portal.call(world.main._sweep_orphan_blobs)
    con = sqlite3.connect(world.tmp / "chats.db")
    row = con.execute("SELECT owner_profile_id FROM files WHERE stored_name = 'agent-report.txt'").fetchone()
    con.close()
    assert row is not None and row[0] == ids["Olive"]


def test_first_run_threads_wait_for_the_owner_and_are_adopted_when_one_appears(world):
    c = world.client()

    async def make():
        return await world.db.create_thread("alpha", title="guide", owner_profile_id=ownership.owner_id())
    t = c.portal.call(make)
    assert ownership.owner_id() is None
    assert _owner_of(world, "threads", t.id) is None
    world.create_owner(c)
    assert _owner_of(world, "threads", t.id) == accounts.get_owner().id


def test_owner_creation_also_adopts_rows_whose_owner_no_longer_exists(world):
    c = world.client()

    async def make():
        return await world.db.create_thread("alpha", owner_profile_id="gone-profile-id")
    t = c.portal.call(make)
    world.create_owner(c)
    assert _owner_of(world, "threads", t.id) == accounts.get_owner().id


def test_an_unsigned_device_stamps_the_single_restricted_profile_else_the_owner(world):
    owner = world.create_owner(world.client())
    rows = accounts.list_profiles()
    assert ownership.anonymous_target(rows) == accounts.get_owner().id     # no Restricted profile
    world.person(owner, "Robin", "restricted")
    robin = accounts.get_by_name("Robin")
    assert ownership.anonymous_target() == robin.id
    assert ownership.for_principal(world.main.policy.anonymous()) == robin.id
    world.person(owner, "Kit", "restricted")
    assert ownership.anonymous_target() == accounts.get_owner().id         # two: never guess
    assert ownership.anonymous_target(designated=robin.id) == robin.id
    assert ownership.for_principal(world.main.policy.machine_principal("loopback")) == accounts.get_owner().id
    assert ownership.for_principal(world.main.policy.open_principal()) == accounts.get_owner().id


def test_every_create_call_in_the_app_passes_an_owner():
    """A grep gate: a new creation path cannot forget the owner (the keyword is required,
    and this names the call sites so a reviewer sees each one)."""
    import ast
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    sources = list((root / "app").rglob("*.py")) + list((root.parent / "apps").rglob("backend.py"))
    want = {"create_thread": "owner_profile_id", "add_file": "owner_profile_id",
            "adopt_file": "owner_profile_id", "find_or_create_daily_thread": "owner"}
    seen = 0
    for path in sources:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in want):
                seen += 1
                kws = {k.arg for k in node.keywords}
                assert want[node.func.attr] in kws, f"{path.name}:{node.lineno} {node.func.attr} without an owner"
    assert seen >= 14


# --------------------------------------------------------------------------- #
# The owner is server-side only
# --------------------------------------------------------------------------- #


def test_thread_out_never_dumps_the_owner():
    from app.models import ThreadOut
    t = ThreadOut(id="t", bot_id="b", created_at="x", updated_at="x", owner_profile_id="p-secret")
    assert t.owner_profile_id == "p-secret"
    assert "owner_profile_id" not in t.model_dump()
    assert "p-secret" not in t.model_dump_json()


def test_no_client_payload_carries_an_owner_id(world, people):
    ids = people["ids"]
    owner, member = people["owner"], people["member"]
    bodies: list[str] = []
    t = owner.post("/api/threads", json={"bot_id": "alpha", "title": "x"})
    bodies.append(t.text)
    tid = t.json()["id"]
    up = owner.post("/api/files", files={"file": ("a.txt", io.BytesIO(b"x"), "text/plain")})
    bodies.append(up.text)
    for c in (owner, member):
        for path in ("/api/threads?bot_id=alpha", f"/api/threads/{tid}", "/api/files",
                     "/api/unread", "/api/export", "/api/search?q=x", "/api/bots", "/api/people",
                     "/api/auth/me"):
            r = c.get(path)
            bodies.append(r.text)
    with owner.websocket_connect("/ws") as ws:
        bodies.append(json.dumps(ws.receive_json()))
        ws.send_json({"type": "get_threads", "bot_id": "alpha"})
        ws.send_json({"type": "create_thread", "bot_id": "alpha"})
        bodies.extend(json.dumps(f) for f in _drain(ws))
    blob = "\n".join(bodies)
    assert "threads_list" in blob and "thread_created" in blob
    assert "owner_profile_id" not in blob
    assert '"backends"' not in blob
    # The Member's own id is legitimately in /api/auth/me and /api/people; the Owner's
    # threads must not carry it, and nothing above carries a key named for ownership.
    assert ids["Olive"] not in t.text and ids["Olive"] not in up.text


def test_file_records_keep_their_old_shape(world, people):
    up = people["owner"].post("/api/files", files={"file": ("a.txt", io.BytesIO(b"x"), "text/plain")}).json()
    assert set(up) == {"id", "name", "stored_name", "size", "mime", "created_at", "source"}

    async def look():
        return (await world.db.list_files(), await world.db.get_file(up["id"]),
                await world.db.file_owner(up["id"]))
    rows, one, own = people["owner"].portal.call(look)
    assert all("owner_profile_id" not in r for r in rows) and "owner_profile_id" not in one
    assert own == people["ids"]["Olive"]


# --------------------------------------------------------------------------- #
# Schema round trips: character fields, profiles.backends
# --------------------------------------------------------------------------- #


def test_character_scope_fields_round_trip_and_are_not_written_by_default(world):
    config.load_bots()
    raw = config.CONFIG_PATH.read_text()
    assert "scope" not in raw and "owner_profile" not in raw and "cloned_from" not in raw
    b = config.Bot(id="copy1", name="Copy", scope="personal", owner_profile="p1", cloned_from="alpha")
    config._write_bots([config._bot_entry(x) for x in config.load_bots()] + [config._bot_entry(b)])
    got = config.get_bot("copy1")
    assert (got.scope, got.owner_profile, got.cloned_from) == ("personal", "p1", "alpha")
    assert config.get_bot("alpha").scope == "household"
    # Neither field reaches the public record yet.
    assert "scope" not in got.to_dict() and "owner_profile" not in got.to_dict()
    assert config.parse_scope("Personal") == "personal" and config.parse_scope("other") == "household"


def test_profile_backends_are_stored_and_read_but_not_public(world):
    world.create_owner(world.client())
    p = accounts.create_profile("Sam", "member")
    assert p.backends == {}
    want = {"allowed": ["conn-1", "agents"], "default": "conn-1", "models": {"conn-1": "m"}}
    q = accounts.update_profile(p.id, backends=want)
    assert q.backends == want and q.epoch == p.epoch       # not a credential change
    accounts.invalidate()
    assert accounts.get_profile(p.id).backends == want
    assert "backends" not in q.public()
    with pytest.raises(accounts.AccountError):
        accounts.update_profile(p.id, backends=["not", "a", "dict"])


def test_an_older_profiles_table_gains_the_backends_column(tmp_path):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript(accounts.SCHEMA)
    con.execute("ALTER TABLE profiles DROP COLUMN backends")     # the 3.x shape
    cols = {r[1] for r in con.execute("PRAGMA table_info(profiles)")}
    con.close()
    assert "backends" not in cols
    accounts.reset_for_tests()
    with accounts.connect(path) as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(profiles)")}
    assert "backends" in cols
    accounts.reset_for_tests()


def test_push_subscriptions_record_the_profile(world, monkeypatch):
    from app import push
    push.subscribe("k1", {"endpoint": "https://fcm.googleapis.com/x",
                          "keys": {"p256dh": "A" * 87, "auth": "B" * 22}}, profile_id="p-1")
    assert push._load_subs()["k1"]["profile_id"] == "p-1"
    push.subscribe("k2", {"endpoint": "https://fcm.googleapis.com/y",
                          "keys": {"p256dh": "A" * 87, "auth": "B" * 22}})
    assert "profile_id" not in push._load_subs()["k2"]


def test_the_owner_index_exists(world):
    world.client()
    con = sqlite3.connect(world.tmp / "chats.db")
    idx = {r[1] for r in con.execute("PRAGMA index_list(threads)")}
    cols = {r[1] for r in con.execute("PRAGMA table_info(files)")}
    con.close()
    assert "idx_threads_owner_bot_updated" in idx and "owner_profile_id" in cols


def test_database_without_the_columns_is_upgraded_in_place(tmp_path):
    from app.database import Database
    path = tmp_path / "c.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, bot_id TEXT NOT NULL, title TEXT, "
                "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'idle', "
                "is_archived INTEGER NOT NULL DEFAULT 0)")
    con.execute("INSERT INTO threads (id, bot_id, created_at, updated_at) VALUES ('t', 'alpha', 'x', 'x')")
    con.commit()
    con.close()

    async def go():
        db = Database(path)
        await db.connect()
        try:
            t = await db.get_thread("t")
            return t, await db.thread_owner("t")
        finally:
            await db.close()
    t, own = asyncio.run(go())
    assert t is not None and own is None and t.owner_profile_id is None
