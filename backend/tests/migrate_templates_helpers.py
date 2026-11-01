"""A data directory in the live 4.1 shape (tests/fixtures/live-4.1-shape) for the 012_templates
tests: the redacted roster, three profiles with the fixture's ids, threads per (bot, owner) from
thread-counts.json, the kit state, the pictures, an advisor ledger. Migrations 001-011 are
applied (on the empty directory), 012 is not."""
from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
from pathlib import Path

import yaml

from app import accounts, auth, config, firstrun, migrate

FIX = Path(__file__).parent / "fixtures" / "live-4.1-shape"
OWNER = "c30db2d188b5c09aa9a6768ca6ab6cd6"
DISPATCH = "1643f014f0507dbcdcceac1214d0c5e4"
KIDS = "8f78572fc3dff747bd6eaf8f0b384867"
KIDS_BOTS = ["kid_lobster", "kid_fruitbat", "kid_octopus", "kid_bee", "kid_owl", "kid_frog",
             "kid_tortoise", "kid_penguin"]
AGENTS = ["main", "trainer", "Doxy", "alpha", "jobboard", "DS_Flash", "reviewer"]
RETIRED = {"Charley": 3, "DS_Brain": 1, "beta": 9, "review": 1}
def _png() -> bytes:
    import io

    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (40, 90, 160)).save(buf, "PNG")
    return buf.getvalue()


PNG = _png()


def point_at(d: Path, monkeypatch) -> None:
    d.mkdir(parents=True, exist_ok=True)
    for name, rel in (("DATA_DIR", ""), ("CONFIG_PATH", "config.yaml"), ("BACKUP_DIR", "backups"),
                      ("DB_PATH", "chats.db"), ("MEDIA_DIR", "media"), ("FILES_DIR", "files"),
                      ("LOG_DIR", "logs"), ("AVATAR_DIR", "avatars")):
        monkeypatch.setattr(config, name, d / rel if rel else d)
    monkeypatch.setattr(auth, "SECURITY_PATH", d / "security.yaml")
    monkeypatch.setattr(auth, "RECOVERY_PATH", d / "RECOVERY-CODE.txt")
    monkeypatch.setattr(auth, "TRUSTED_PATH", d / "trusted-devices.yaml")
    config._invalidate_bots_cache()
    accounts.reset_for_tests()
    auth._cache = None
    auth._trusted = None


def _threads(d: Path, rows: list[tuple[str, str | None, str, list[tuple[str, str]]]]) -> None:
    from app.database import Database

    async def build():
        db = Database(d / "chats.db")
        await db.connect()
        for tid, owner, bot, msgs in rows:
            await db.create_thread(bot, f"t-{tid}", thread_id=tid, owner_profile_id=owner)
            for i, (role, src) in enumerate(msgs):
                await db.add_message(tid, role, f"m{i}", source_id=src or None)
        await db.close()
    asyncio.run(build())


def build(root: Path, monkeypatch, *, claimed: bool = True) -> Path:
    d = root / "data"
    point_at(d, monkeypatch)
    profs = {p["id"]: p for p in json.loads((FIX / "profiles.json").read_text())}
    if claimed:
        accounts.create_profile("Owner", "admin", protected=True, profile_id=OWNER)
    accounts.create_profile("DisPatch", "user", profile_id=DISPATCH,
                            household_bots=profs[DISPATCH]["household_bots"])
    accounts.create_profile(profs[KIDS]["name"], "user", profile_id=KIDS,
                            household_bots=profs[KIDS]["household_bots"])
    migrate.run(take_backup=False)              # 001-012 on the empty directory ...
    con = sqlite3.connect(d / "chats.db")
    con.execute("DELETE FROM schema_migrations WHERE id = '012_templates'")    # ... 012 not yet
    con.commit()
    con.close()
    shutil.copy(FIX / "roster.yaml", d / "config.yaml")
    av = d / "avatars"
    av.mkdir(exist_ok=True)
    for b in yaml.safe_load((FIX / "roster.yaml").read_text())["bots"]:
        if b.get("avatar"):
            (av / b["avatar"]).write_bytes(PNG)
    # the 4.1 Atlas copies: DisPatch's still holds the shipped picture, the kids' own was changed
    shutil.copyfile(firstrun.AVATAR_SRC_DIR / "atlas-face.png", av / "pe2aa88a6cb9e-face.png")
    (av / "pe63d316c0739-face.png").write_bytes(PNG + b"own")
    kd = d / "bot-kits"
    kd.mkdir(exist_ok=True)
    state = json.loads((FIX / "bot-kits-state.json").read_text())
    (kd / "state.json").write_text(json.dumps(state))
    counts = json.loads((FIX / "thread-counts.json").read_text())
    rows: list[tuple[str, str | None, str, list[tuple[str, str]]]] = []
    for pid, per in counts.items():
        for bot, n in per.items():
            for i in range(n):
                tid = f"{pid[:4]}-{bot}-{i}"
                msgs: list[tuple[str, str]] = []
                if pid == OWNER and bot == "atlas" and i == 0:
                    msgs = [("assistant", "guide:atlas:meet-your-atlas.md")]
                if pid == OWNER and bot == "atlas" and i == 1:
                    msgs = [("assistant", "guide:atlas:" + OWNER + ":using-dispatch.md:r2")]
                if pid == OWNER and bot == "atlas" and i == 2:
                    msgs = [("assistant", f"release:4.1.0:{OWNER}")]
                owner = None if (pid == OWNER and i % 4 == 3) else pid       # NULL owner = the Admin
                rows.append((tid, owner, bot, msgs))
    _threads(d, rows)
    con = sqlite3.connect(d / "chats.db")
    con.execute("INSERT INTO image_jobs (id, message_id, thread_id, bot_id, spec, state, created_at, "
                "updated_at) SELECT 'j1', m.id, m.thread_id, 'atlas', '{}', 'done', 'x', 'x' "
                "FROM messages m WHERE m.thread_id = ? LIMIT 1", (f"{OWNER[:4]}-atlas-0",))
    con.commit()
    con.close()
    adv = sqlite3.connect(d / "advisor.db")
    adv.execute("CREATE TABLE requests(id TEXT PRIMARY KEY, bot_id TEXT NOT NULL, thread_id TEXT NOT NULL, "
                "kind TEXT, agent TEXT, brief TEXT, state TEXT, created_at TEXT, updated_at TEXT)")
    adv.execute("INSERT INTO requests VALUES ('r1','wikka',?, 'k','a','b','done','x','x')",
                (f"{OWNER[:4]}-wikka-0",))
    adv.commit()
    adv.close()
    accounts.reset_for_tests()
    config._invalidate_bots_cache()
    return d


def roster(d: Path) -> dict[str, dict]:
    return {e["id"]: e for e in yaml.safe_load((d / "config.yaml").read_text())["bots"]}


def thread_counts(d: Path) -> dict[tuple[str, str], int]:
    con = sqlite3.connect(d / "chats.db")
    try:
        out: dict[tuple[str, str], int] = {}
        for bot, owner, n in con.execute(
                "SELECT bot_id, COALESCE(owner_profile_id, ?), COUNT(*) FROM threads GROUP BY 1, 2", (OWNER,)):
            out[(bot, owner)] = n
        return out
    finally:
        con.close()


def snapshot(d: Path) -> dict:
    """Everything 012 may touch, comparable."""
    con = sqlite3.connect(d / "chats.db")
    try:
        threads = sorted(con.execute("SELECT id, bot_id, owner_profile_id FROM threads"))
        jobs = sorted(con.execute("SELECT id, bot_id FROM image_jobs"))
        hb = sorted(con.execute("SELECT id, household_bots FROM profiles"))
    finally:
        con.close()
    adv = sqlite3.connect(d / "advisor.db")
    try:
        reqs = sorted(adv.execute("SELECT id, bot_id FROM requests"))
    finally:
        adv.close()
    return {"config": (d / "config.yaml").read_bytes(), "state": (d / "bot-kits" / "state.json").read_bytes(),
            "threads": threads, "jobs": jobs, "hb": hb, "reqs": reqs,
            "avatars": sorted(p.name for p in (d / "avatars").iterdir())}
