"""Migration 009_ownership (3.x -> 4.0): every existing chat gets the most specific owner
that could ALREADY read it - nobody gains access - with an exact dry run, a backup that
includes the push subscriptions and the Image Studio ledger, idempotence and a restore.

Run: cd backend && uv run pytest tests/test_migrate_ownership.py
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3

import pytest
import yaml

from app import accounts, auth, characters, config, migrate, policy

PUSH_KEYS = {"remembered": "a" * 64, "legacy": "b" * 64, "dead": "c" * 64, "open": "open:" + "d" * 64}


def _sub(n: int) -> dict:
    return {"endpoint": f"https://fcm.googleapis.com/fcm/send/{n}", "keys": {"p256dh": "A" * 87, "auth": "B" * 22},
            "created": 1, "ua": "test"}


@pytest.fixture
def v3(tmp_path, monkeypatch):
    """A 3.x data directory: steps 001-008 applied, three profiles, a roster that covers
    every assignment rule, chats and files with no owner column, push subscriptions."""
    d = tmp_path / "v3"
    d.mkdir()
    monkeypatch.setattr(config, "DATA_DIR", d)
    monkeypatch.setattr(config, "CONFIG_PATH", d / "config.yaml")
    monkeypatch.setattr(config, "BACKUP_DIR", d / "backups")
    monkeypatch.setattr(auth, "SECURITY_PATH", d / "security.yaml")
    monkeypatch.setattr(auth, "RECOVERY_PATH", d / "RECOVERY-CODE.txt")
    monkeypatch.setattr(auth, "TRUSTED_PATH", d / "trusted-devices.yaml")
    config._invalidate_bots_cache()
    accounts.reset_for_tests()
    auth._cache = None
    auth._trusted = None
    owner = accounts.create_profile("Olive", "owner")
    sam = accounts.create_profile("Sam", "member")
    robin = accounts.create_profile("Robin", "restricted")
    roster = [
        {"id": "cub", "name": "Cub", "safe": True, "hide_unlocked": True, "audience": "restricted"},
        {"id": "pal", "name": "Pal", "safe": True, "hide_unlocked": False, "audience": "all"},
        {"id": "work", "name": "Work", "safe": False, "hide_unlocked": False, "audience": "members"},
        {"id": "solo", "name": "Solo", "safe": False, "audience": "members", "profiles": [sam.id]},
        # Made for Robin only - but Robin is Restricted and the audience is members, so
        # Robin could never open it: the chats stay the Owner's.
        {"id": "rx", "name": "Rx", "safe": False, "audience": "members", "profiles": [robin.id]},
        {"id": "pair", "name": "Pair", "safe": True, "audience": "all", "profiles": [sam.id, robin.id]},
        {"id": "Mixed", "name": "Mixed", "safe": True, "hide_unlocked": True, "audience": "restricted"},
    ]
    (d / "config.yaml").write_text(yaml.safe_dump({"bots": roster}, sort_keys=False))

    from app.database import Database

    async def build():
        db = Database(d / "chats.db")
        await db.connect()
        for i, bot in enumerate(["cub", "cub", "pal", "work", "solo", "solo", "rx", "pair",
                                 "ghost", "mixed"]):
            await db.create_thread(bot, f"t{i}", thread_id=f"t{i}", owner_profile_id=None)
        await db.add_file("a.txt", "a.txt", 1, "text/plain", owner_profile_id=None)
        await db.add_file("b.txt", "b.txt", 1, "text/plain", source="fileserver", owner_profile_id=None)
        await db.close()
    asyncio.run(build())
    migrate.run(take_backup=False)                    # 001-009 recorded ...
    con = sqlite3.connect(d / "chats.db")             # ... then back to the 3.x shape
    con.execute("DROP INDEX IF EXISTS idx_threads_owner_bot_updated")
    con.execute("ALTER TABLE threads DROP COLUMN owner_profile_id")
    con.execute("ALTER TABLE files DROP COLUMN owner_profile_id")
    con.execute("ALTER TABLE profiles DROP COLUMN backends")
    con.execute("DELETE FROM schema_migrations WHERE id = '009_ownership'")
    con.commit()
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.execute("PRAGMA journal_mode=DELETE")
    con.close()
    (d / "trusted-devices.yaml").write_text(yaml.safe_dump({"devices": {
        PUSH_KEYS["remembered"]: {"created": 1.0, "last_seen": 1.0, "ua": "x", "profile_id": sam.id},
        PUSH_KEYS["legacy"]: {"created": 1.0, "last_seen": 1.0, "ua": "x", "profile_id": ""},
    }}))
    (d / "push-subscriptions.json").write_text(json.dumps(
        {"devices": {k: _sub(i) for i, k in enumerate(PUSH_KEYS.values())}}, indent=1, sort_keys=True))
    led = d / "apps-data" / "imagestudio"
    led.mkdir(parents=True)
    (led / "images.jsonl").write_text('{"id": "i1", "prompt": "a lighthouse"}\n')
    accounts.reset_for_tests()
    auth._trusted = None
    return {"dir": d, "owner": owner, "sam": sam, "robin": robin}


def _hashes(d):
    return {str(p.relative_to(d)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(d.rglob("*")) if p.is_file()}


def _owners(d) -> dict[str, str | None]:
    con = sqlite3.connect(d / "chats.db")
    try:
        return dict(con.execute("SELECT id, owner_profile_id FROM threads"))
    finally:
        con.close()


def _text(rep) -> str:
    return "\n".join(rep.lines())


def test_the_dry_run_prints_the_table_and_writes_nothing(v3):
    before = _hashes(v3["dir"])
    rep = migrate.run(dry_run=True)
    assert _hashes(v3["dir"]) == before, "a dry run wrote something"
    assert {a.step for a in rep.actions} == {"009_ownership"}
    text = _text(rep)
    assert "threads.owner_profile_id" in text and "files.owner_profile_id" in text
    assert "profiles.backends" in text
    rows = {line.split("'")[1]: line for line in text.splitlines() if "chat(s) ->" in line}
    assert "2 chat(s) -> Robin (Basic)" in rows["cub"]
    assert "-> Sam (User)" in rows["solo"] and "only Sam may use it" in rows["solo"]
    for bot in ("pal", "work", "rx", "pair", "ghost"):
        assert "-> Olive (Owner)" in rows[bot], rows[bot]
    assert "no longer in the roster" in rows["ghost"]
    assert "-> Robin (Basic)" in rows["mixed"]        # matched case-insensitively
    assert "files: 2 File Server record(s) -> Olive (Owner)" in text
    assert "push subscriptions: 3 get their device's profile, 1 dropped" in text
    assert "'cub' could become a personal character of Robin" in text
    assert "'solo' could become a personal character of Sam" in text
    assert rep.backup is None and rep.applied == []


def test_applying_assigns_by_the_never_widen_rule(v3):
    o, sam, robin = v3["owner"].id, v3["sam"].id, v3["robin"].id
    rep = migrate.run()
    assert rep.applied == ["009_ownership"] and rep.backup is not None
    got = _owners(v3["dir"])
    assert got == {"t0": robin, "t1": robin, "t2": o, "t3": o, "t4": sam, "t5": sam, "t6": o,
                   "t7": o, "t8": o, "t9": robin}
    con = sqlite3.connect(v3["dir"] / "chats.db")
    assert con.execute("SELECT COUNT(*) FROM files WHERE owner_profile_id IS NOT ?", (o,)).fetchone()[0] == 0
    assert "idx_threads_owner_bot_updated" in {r[1] for r in con.execute("PRAGMA index_list(threads)")}
    assert "backends" in {r[1] for r in con.execute("PRAGMA table_info(profiles)")}
    assert con.execute("SELECT 1 FROM schema_migrations WHERE id='009_ownership'").fetchone()
    con.close()
    # Characters are untouched: nothing became personal.
    roster = yaml.safe_load((v3["dir"] / "config.yaml").read_text())["bots"]
    assert all("scope" not in e for e in roster)
    subs = json.loads((v3["dir"] / "push-subscriptions.json").read_text())["devices"]
    assert subs[PUSH_KEYS["remembered"]]["profile_id"] == sam
    assert subs[PUSH_KEYS["legacy"]]["profile_id"] == o
    assert subs[PUSH_KEYS["open"]]["profile_id"] == o
    assert PUSH_KEYS["dead"] not in subs
    # The backup holds the files this step touches besides the usual ones.
    assert (rep.backup / "push-subscriptions.json").exists()
    assert (rep.backup / "apps-data" / "imagestudio" / "images.jsonl").exists()


def test_no_profile_gains_access_to_a_chat_it_could_not_read(v3):
    """The rule, checked directly: every new owner could already use the character."""
    migrate.run()
    accounts.reset_for_tests()
    bots = {b.id.lower(): b for b in config.load_bots()}
    for tid, pid in _owners(v3["dir"]).items():
        prof = accounts.get_profile(pid)
        assert prof is not None
        con = sqlite3.connect(v3["dir"] / "chats.db")
        bot_id = con.execute("SELECT bot_id FROM threads WHERE id=?", (tid,)).fetchone()[0]
        con.close()
        bot = bots.get(bot_id.lower())
        if not prof.protected:
            assert bot is not None and characters.may_use(bot, policy.profile_principal(prof)), (tid, bot_id)


def test_two_restricted_profiles_means_no_guess(v3):
    accounts.create_profile("Kit", "restricted")
    accounts.reset_for_tests()
    migrate.run()
    got = _owners(v3["dir"])
    assert got["t0"] == got["t1"] == v3["owner"].id


def test_running_it_twice_is_a_no_op(v3):
    migrate.run()
    assert migrate.plan().actions == []
    before = _hashes(v3["dir"])
    rep = migrate.run()
    assert rep.applied == [] and rep.backup is None
    assert len(list((v3["dir"] / "backups").iterdir())) == 1
    after = _hashes(v3["dir"])
    assert {k: v for k, v in after.items() if not k.startswith("chats.db")} == \
        {k: v for k, v in before.items() if not k.startswith("chats.db")}


def test_restore_puts_every_file_back(v3):
    d = v3["dir"]
    con = sqlite3.connect(d / "chats.db")
    dump_before = list(con.iterdump())
    con.close()
    files_before = {n: (d / n).read_bytes() for n in
                    ("config.yaml", "trusted-devices.yaml", "push-subscriptions.json",
                     "apps-data/imagestudio/images.jsonl")}
    rep = migrate.run()
    assert (d / "push-subscriptions.json").read_bytes() != files_before["push-subscriptions.json"]
    names = migrate.restore(rep.backup)
    assert {"chats.db", "push-subscriptions.json", "apps-data/imagestudio/images.jsonl"} <= set(names)
    for n, b in files_before.items():
        assert (d / n).read_bytes() == b, n
    con = sqlite3.connect(d / "chats.db")
    assert list(con.iterdump()) == dump_before
    con.close()
    # ... and the upgrade can be run again from there.
    assert migrate.run().applied == ["009_ownership"]


def test_assign_and_all_to_owner_override_the_rule(v3, capsys):
    d = str(v3["dir"])
    assert migrate.main(["--dry-run", "--data-dir", d, "--assign", "work=Sam", "--assign", "cub=Olive"]) == 0
    out = capsys.readouterr().out
    assert "'work'" in out and "-> Sam (User)  (--assign)" in out
    assert migrate.main(["--data-dir", d, "--assign", "work=sam", "--assign", "cub=Olive"]) == 0
    got = _owners(v3["dir"])
    assert got["t3"] == v3["sam"].id and got["t0"] == v3["owner"].id
    assert got["t4"] == v3["sam"].id                          # the rule still covers the rest


def test_all_to_owner(v3):
    assert migrate.main(["--data-dir", str(v3["dir"]), "--all-to-owner"]) == 0
    assert set(_owners(v3["dir"]).values()) == {v3["owner"].id}
    # The overrides shape the first run only; afterwards they are refused, not silently ignored.
    assert migrate.main(["--data-dir", str(v3["dir"]), "--assign", "cub=Robin"]) == 2
    assert set(_owners(v3["dir"]).values()) == {v3["owner"].id}


def test_assign_warns_when_it_would_widen_and_refuses_unknown_names(v3, capsys):
    d = str(v3["dir"])
    assert migrate.main(["--dry-run", "--data-dir", d, "--assign", "work=Robin"]) == 0
    assert "Robin could not use this character before" in capsys.readouterr().out
    assert migrate.main(["--dry-run", "--data-dir", d, "--assign", "work=Nobody"]) == 2
    assert migrate.main(["--dry-run", "--data-dir", d, "--assign", "nope=Sam"]) == 2
    assert migrate.main(["--dry-run", "--data-dir", d, "--assign", "work"]) == 2
    assert migrate.main(["--dry-run", "--data-dir", d, "--assign", "work=Sam", "--all-to-owner"]) == 2


def test_an_install_without_an_owner_leaves_chats_unowned_until_one_exists(tmp_path, monkeypatch):
    d = tmp_path / "open"
    d.mkdir()
    for mod, name, val in ((config, "DATA_DIR", d), (config, "CONFIG_PATH", d / "config.yaml"),
                           (config, "BACKUP_DIR", d / "backups"), (auth, "SECURITY_PATH", d / "security.yaml"),
                           (auth, "TRUSTED_PATH", d / "trusted-devices.yaml")):
        monkeypatch.setattr(mod, name, val)
    config._invalidate_bots_cache()
    accounts.reset_for_tests()
    (d / "config.yaml").write_text(yaml.safe_dump({"bots": [{"id": "pal", "name": "Pal", "safe": True}]}))
    from app.database import Database

    async def build():
        db = Database(d / "chats.db")
        await db.connect()
        await db.create_thread("pal", thread_id="t1", owner_profile_id=None)
        await db.close()
    asyncio.run(build())
    rep = migrate.run(dry_run=True)
    assert "nobody yet (the Owner, once one is created)" in _text(rep)
    migrate.run()
    assert _owners(d) == {"t1": None}
    accounts.create_profile("Olive", "owner")
    assert _owners(d) == {"t1": accounts.get_owner().id}


def test_anonymous_access_none_is_left_alone(v3):
    """R1 records no anonymous-profile setting: security.yaml is not touched by 009."""
    sec = v3["dir"] / "security.yaml"
    sec.write_text(yaml.safe_dump({"anonymous_access": "none", "machine_loopback_trust": True}))
    before = sec.read_bytes()
    migrate.run()
    assert sec.read_bytes() == before
