"""Migration 010_profiles (4.0 chat-first profiles, BUILD-PLAN-v2 §1.6).

Minimal and additive (no table rebuild, no revert, no thread reassignment, no device-binding
clearing). What is checked here:

* the dry run writes nothing (sha256 of the data dir before/after) and prints, per profile,
  old stored role -> new role -> household list, the capabilities the default profile
  gains, the bot flag changes and the security.yaml diff;
* apply on shape (a) gives the expected roles / lists / ``hidden`` / ``lock_target``;
* idempotent; ``--restore`` round trip; ``--default-profile`` refused after 010 (and for a
  profile that can't be the default);
* the auto-run rule never raises (shapes b, c, d, e) and never fires on a later start;
* sessions, devices and remembered devices survive with their rows intact;
* shape (e): a character ``profiles`` list folds into those profiles' ``household_bots``.

Shapes (every name neutral):
  a  live-shaped: Admin "Owner" with a PIN, a PIN-less Restricted "Playground", three pack
     bots (pack_otter, pack_heron, pack_newt: audience restricted, safe, hide_unlocked), an
     agent bot ``helper``, two direct bots, chats per owner, ``anonymous_access: none``
  b  Owner + Admin + Member + two Restricted profiles
  c  an open install: no Owner, no PIN
  d  already migrated (010 recorded)
  e  a character with a ``profiles`` list (``anonymous_access: restricted``)

Run: cd backend && uv run pytest tests/test_migrate_profiles.py
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from app import accounts, auth, config, migrate
from app.contracts.permissions import Role

PACK = ["pack_otter", "pack_heron", "pack_newt"]


def _point_at(d: Path, monkeypatch) -> None:
    d.mkdir(parents=True, exist_ok=True)
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


def _roster(extra: list[dict] | None = None) -> list[dict]:
    pack = [{"id": b, "name": b.split("_")[1].title(), "safe": True, "hide_unlocked": True,
             "audience": "restricted", "quick_actions": False,
             "advisor": {"providers": [{"provider": "openai_compatible", "model": "m"}]}} for b in PACK]
    return [
        *pack,
        {"id": "helper", "name": "Helper", "audience": "members"},                     # agent (gateway)
        {"id": "guide", "name": "Guide", "audience": "all", "safe": True,
         "advisor": {"providers": [{"provider": "openai_compatible", "model": "m"}]}},  # direct
        {"id": "notes", "name": "Notes", "audience": "members",
         "advisor": {"providers": [{"provider": "openai_compatible", "model": "m"}]}},  # direct
        *(extra or []),
    ]


def _threads(d: Path, rows: list[tuple[str, str | None]]) -> None:
    from app.database import Database

    async def build():
        db = Database(d / "chats.db")
        await db.connect()
        for i, (bot, owner) in enumerate(rows):
            await db.create_thread(bot, f"t{i}", thread_id=f"t{i}", owner_profile_id=owner)
        await db.close()
    asyncio.run(build())


def _security(**fields) -> None:
    cfg = auth.load()
    for k, v in fields.items():
        setattr(cfg, k, v)
    auth._write(cfg)
    auth._bust_cache()


def _run_without_010(monkeypatch) -> None:
    """Steps 001-009 only: the 3.x shape 010 starts from."""
    with monkeypatch.context() as m:
        m.setattr(migrate, "profiles_plan", lambda **_: SimpleNamespace(lines=list))
        migrate.run(take_backup=False)


def build_shape(shape: str, root: Path, monkeypatch) -> dict:
    """A 3.x-shaped data directory (009 applied, 010 not) for ``shape`` a..e. Returns the
    profile ids by name and the data dir."""
    d = root / f"shape-{shape}"
    _point_at(d, monkeypatch)
    out: dict = {"dir": d}
    extra: list[dict] = []
    if shape == "c":
        (d / "config.yaml").write_text(yaml.safe_dump({"bots": _roster()}, sort_keys=False))
        _threads(d, [("guide", None)])
        _run_without_010(monkeypatch)
        return out
    auth.set_pin("2468")                                   # creates and mirrors the Owner
    owner = accounts.get_protected_admin()
    out["Owner"] = owner.id
    if shape in ("a", "d", "e"):
        out["Playground"] = accounts.create_profile("Playground", "restricted").id
    if shape == "b":
        out["Ann"] = accounts.create_profile("Ann", "admin").id
        out["Uma"] = accounts.create_profile("Uma", "member").id
        out["Bo"] = accounts.create_profile("Bo", "restricted").id
        out["Cy"] = accounts.create_profile("Cy", "restricted").id
    if shape == "e":
        extra = [{"id": "story", "name": "Story", "audience": "restricted", "safe": True,
                  "profiles": [out["Playground"]],
                  "advisor": {"providers": [{"provider": "openai_compatible", "model": "m"}]}}]
    (d / "config.yaml").write_text(yaml.safe_dump({"bots": _roster(extra)}, sort_keys=False))
    rows = [("helper", owner.id), ("guide", owner.id), ("notes", owner.id)]
    if "Playground" in out:
        rows += [(b, out["Playground"]) for b in PACK]
    _threads(d, rows)
    _security(anonymous_access="none" if shape == "a" else "restricted")
    _run_without_010(monkeypatch)                          # 001-009 recorded, 010 not (yet)
    if shape == "d":
        con = sqlite3.connect(d / "chats.db")
        con.execute("INSERT OR IGNORE INTO schema_migrations (id, applied_at, app_version) "
                    "VALUES ('010_profiles', '2026-10-22T00:00:00+00:00', '4.0.0')")
        con.commit()
        con.close()
    accounts.reset_for_tests()
    auth._cache = None
    return out


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _hashes(d: Path) -> dict[str, str]:
    return {str(p.relative_to(d)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(d.rglob("*")) if p.is_file()}


def _text(rep) -> str:
    return "\n".join(rep.lines())


def _mine(rep) -> list[str]:
    return [a.text for a in rep.actions if a.step == "010_profiles"]


def _roster_on_disk(d: Path) -> dict[str, dict]:
    return {e["id"]: e for e in yaml.safe_load((d / "config.yaml").read_text())["bots"]}


def _sec(d: Path) -> dict:
    return yaml.safe_load((d / "security.yaml").read_text())


def _stored(d: Path) -> dict[str, tuple[str, str]]:
    """id -> (raw role column, raw household_bots column)."""
    con = sqlite3.connect(d / "chats.db")
    try:
        return {r[0]: (r[1], r[2]) for r in con.execute("SELECT id, role, household_bots FROM profiles")}
    finally:
        con.close()


def _recorded(d: Path) -> bool:
    con = sqlite3.connect(d / "chats.db")
    try:
        return con.execute("SELECT 1 FROM schema_migrations WHERE id='010_profiles'").fetchone() is not None
    finally:
        con.close()


def _extras(d: Path, playground: str) -> None:
    """The files 010 adds to the backup set."""
    (d / "quick-actions.yaml").write_text("hidden: [proof]\n")
    (d / "voice-acks.yaml").write_text("guide: [ok]\n")
    st = d / "apps-data" / "imagestudio"
    st.mkdir(parents=True, exist_ok=True)
    (st / "prompts.json").write_text('["a lighthouse"]')
    (st / f"prompts-{playground}.json").write_text('["a red kite"]')


# --------------------------------------------------------------------------- #
# Planning never raises
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("shape", ["a", "b", "c", "d", "e"])
def test_every_shape_builds_and_plans_without_raising(shape, tmp_path, monkeypatch):
    built = build_shape(shape, tmp_path, monkeypatch)
    rep = migrate.plan()
    if shape == "d":
        assert _mine(rep) == []
    else:
        assert _mine(rep), "010 is pending until it is recorded"
    if shape in ("a", "e"):
        assert accounts.get_profile(built["Playground"]).role is Role.BASIC
    assert not _recorded(built["dir"]) or shape == "d"


# --------------------------------------------------------------------------- #
# Shape (a): the dry run, then apply
# --------------------------------------------------------------------------- #


def test_the_dry_run_on_the_live_shape_prints_the_table_and_writes_nothing(tmp_path, monkeypatch, capsys):
    built = build_shape("a", tmp_path, monkeypatch)
    d = built["dir"]
    _extras(d, built["Playground"])
    before = _hashes(d)
    assert migrate.main(["--data-dir", str(d), "--dry-run", "--default-profile", "Playground"]) == 0
    assert _hashes(d) == before, "a dry run wrote something"
    out = capsys.readouterr().out
    assert out.startswith("DRY RUN - nothing was changed.")
    lines = [ln for ln in out.splitlines() if ln.startswith("[010_profiles]")]
    text = "\n".join(lines)
    owner_row = next(ln for ln in lines if "profile 'Owner'" in ln)
    assert "owner -> admin (protected) -> household: every household bot (Admin)" in owner_row
    play_row = next(ln for ln in lines if "profile 'Playground'" in ln)
    assert ("restricted -> user -> household: pack_otter, pack_heron, pack_newt, guide"
            in play_row) and "[default profile]" in play_row
    assert "helper" not in play_row and "notes" not in play_row
    assert "default profile: Playground (--default-profile)" in text
    gains = next(ln for ln in lines if "Playground gains:" in ln)
    for perm in ("create_characters", "choose_backend", "files_read", "use_images"):
        assert perm in gains, perm
    gained = gains.split("gains: ", 1)[1].split(";", 1)[0]
    for perm in ("voice_lab", "dispatch_agents", "approve_agent_actions"):
        assert perm not in gained, perm                          # the lock-target fence
    for bot in PACK:
        assert f"character '{bot}': hide_unlocked -> hidden" in text
    assert "character 'guide': hide_unlocked" not in text
    assert "security.yaml: lock_target: profile_list -> default_profile" in text
    assert f"security.yaml: default_profile: (unset) -> {built['Playground']}" in text


def test_the_dry_run_without_a_default_says_why(tmp_path, monkeypatch):
    built = build_shape("a", tmp_path, monkeypatch)
    before = _hashes(built["dir"])
    rep = migrate.run(dry_run=True)
    assert _hashes(built["dir"]) == before
    text = _text(rep)
    assert "no default profile (anonymous_access: none" in text
    assert "restricted -> basic -> household: pack_otter, pack_heron, pack_newt, guide" in text
    assert "security.yaml: lock_target" not in text             # profile_list already written
    assert rep.backup is None and rep.applied == []


def test_apply_on_the_live_shape_with_a_default_profile(tmp_path, monkeypatch):
    built = build_shape("a", tmp_path, monkeypatch)
    d, owner, play = built["dir"], built["Owner"], built["Playground"]
    _extras(d, play)
    assert migrate.main(["--data-dir", str(d), "--default-profile", "Playground"]) == 0
    assert _recorded(d)
    stored = _stored(d)
    assert stored[owner] == ("owner", '"all"')
    assert stored[play][0] == "member"
    assert json.loads(stored[play][1]) == [*PACK, "guide"]
    accounts.reset_for_tests()
    p = accounts.get_profile(play)
    assert p.role is Role.USER and p.household_bots == [*PACK, "guide"]
    assert accounts.get_protected_admin().id == owner
    sec = _sec(d)
    assert sec["lock_target"] == "default_profile" and sec["default_profile"] == play
    assert sec["anonymous_access"] == "none"                   # left as it was
    roster = _roster_on_disk(d)
    for bot in PACK:
        e = roster[bot]
        assert e["hidden"] is True
        assert e["safe"] is True and e["audience"] == "restricted"   # inert, kept
    for bot in ("helper", "guide", "notes"):
        assert "hidden" not in roster[bot]
    # Chats keep their owners (no reassignment).
    con = sqlite3.connect(d / "chats.db")
    owners = {r[0]: r[1] for r in con.execute("SELECT bot_id, owner_profile_id FROM threads")}
    con.close()
    assert owners == {"helper": owner, "guide": owner, "notes": owner,
                      **dict.fromkeys(PACK, play)}


def test_apply_on_the_live_shape_without_a_flag_keeps_the_profile_list(tmp_path, monkeypatch):
    built = build_shape("a", tmp_path, monkeypatch)
    d, play = built["dir"], built["Playground"]
    rep = migrate.run()
    assert "010_profiles" in rep.applied
    stored = _stored(d)
    assert stored[play][0] == "restricted"
    assert json.loads(stored[play][1]) == [*PACK, "guide"]
    sec = _sec(d)
    assert sec["lock_target"] == "profile_list" and sec.get("default_profile") is None
    assert all(_roster_on_disk(d)[b]["hidden"] is True for b in PACK)


def test_backup_holds_the_010_files_and_restore_round_trips(tmp_path, monkeypatch):
    built = build_shape("a", tmp_path, monkeypatch)
    d, play = built["dir"], built["Playground"]
    _extras(d, play)
    con = sqlite3.connect(d / "chats.db")
    dump_before = list(con.iterdump())
    con.close()
    names = ("config.yaml", "security.yaml", "quick-actions.yaml", "voice-acks.yaml",
             "apps-data/imagestudio/prompts.json", f"apps-data/imagestudio/prompts-{play}.json")
    files_before = {n: (d / n).read_bytes() for n in names}
    rep = migrate.run(default_profile="Playground")
    assert rep.backup is not None
    for n in names:
        assert (rep.backup / n).read_bytes() == files_before[n], n
    assert (d / "config.yaml").read_bytes() != files_before["config.yaml"]
    assert (d / "security.yaml").read_bytes() != files_before["security.yaml"]
    # Something the profile code would change later, then a restore puts it all back.
    (d / f"apps-data/imagestudio/prompts-{play}.json").write_text("[]")
    (d / "voice-acks.yaml").write_text("{}\n")
    restored = migrate.restore(rep.backup)
    assert set(names) | {"chats.db"} <= set(restored)
    for n, b in files_before.items():
        assert (d / n).read_bytes() == b, n
    con = sqlite3.connect(d / "chats.db")
    assert list(con.iterdump()) == dump_before
    con.close()
    # ... and 010 runs again from there.
    assert migrate.main(["--data-dir", str(d), "--default-profile", play]) == 0
    assert _stored(d)[play][0] == "member"


def test_running_it_twice_is_a_no_op(tmp_path, monkeypatch):
    built = build_shape("a", tmp_path, monkeypatch)
    d = built["dir"]
    migrate.run(default_profile="Playground")
    assert _mine(migrate.plan()) == []
    before = _hashes(d)
    rep = migrate.run()
    assert rep.applied == [] and rep.backup is None
    after = _hashes(d)
    assert {k: v for k, v in after.items() if not k.startswith("chats.db")} == \
        {k: v for k, v in before.items() if not k.startswith("chats.db")}
    assert len(list((d / "backups").iterdir())) == 1


def test_sessions_devices_and_remembered_devices_survive(tmp_path, monkeypatch):
    built = build_shape("a", tmp_path, monkeypatch)
    d, owner, play = built["dir"], built["Owner"], built["Playground"]
    now = "2026-10-01T00:00:00+00:00"
    con = sqlite3.connect(d / "chats.db")
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("INSERT INTO devices (id,label,default_profile_id,bound_at,created,last_seen,user_agent) "
                "VALUES ('dev-play','tablet',?,?,?,?,'ua')", (play, now, now, now))
    con.execute("INSERT INTO devices (id,label,created,last_seen,user_agent) "
                "VALUES ('dev-own','desk',?,?,'ua')", (now, now))
    for tok, pid, dev in (("s1" * 32, play, "dev-play"), ("s2" * 32, owner, "dev-own")):
        con.execute("INSERT INTO sessions (token_hash,profile_id,device_id,created,last_seen,"
                    "expires,persistent,user_agent) VALUES (?,?,?,?,?,NULL,1,'ua')",
                    (tok, pid, dev, now, now))
    con.commit()
    con.close()
    (d / "trusted-devices.yaml").write_text(yaml.safe_dump({"devices": {
        "e" * 64: {"created": 1.0, "last_seen": 1.0, "ua": "x", "profile_id": play},
        "f" * 64: {"created": 1.0, "last_seen": 1.0, "ua": "x", "profile_id": owner}}}))

    def snap():
        c = sqlite3.connect(d / "chats.db")
        try:
            return (c.execute("SELECT * FROM sessions ORDER BY token_hash").fetchall(),
                    c.execute("SELECT * FROM devices ORDER BY id").fetchall(),
                    c.execute("SELECT id, epoch FROM profiles ORDER BY id").fetchall())
        finally:
            c.close()
    rows_before = snap()
    trusted_before = (d / "trusted-devices.yaml").read_bytes()
    assert migrate.main(["--data-dir", str(d), "--default-profile", "Playground"]) == 0
    assert snap() == rows_before                 # no rebuild, no cascade, no epoch bump
    assert (d / "trusted-devices.yaml").read_bytes() == trusted_before
    assert len(rows_before[0]) == 2 and len(rows_before[1]) == 2


# --------------------------------------------------------------------------- #
# --default-profile
# --------------------------------------------------------------------------- #


def test_default_profile_is_refused_once_010_has_run(tmp_path, monkeypatch, capsys):
    built = build_shape("d", tmp_path, monkeypatch)
    rc = migrate.main(["--data-dir", str(built["dir"]), "--dry-run", "--default-profile", "Playground"])
    assert rc == 2
    assert "010_profiles has already run" in capsys.readouterr().err


def test_default_profile_is_refused_after_a_plain_run(tmp_path, monkeypatch, capsys):
    built = build_shape("a", tmp_path, monkeypatch)
    assert migrate.main(["--data-dir", str(built["dir"])]) == 0
    capsys.readouterr()
    assert migrate.main(["--data-dir", str(built["dir"]), "--default-profile", "Playground"]) == 2
    assert "010_profiles has already run" in capsys.readouterr().err


def test_default_profile_is_accepted_before_010(tmp_path, monkeypatch, capsys):
    built = build_shape("a", tmp_path, monkeypatch)
    rc = migrate.main(["--data-dir", str(built["dir"]), "--dry-run", "--default-profile", "Playground"])
    assert rc == 0, capsys.readouterr().err
    rc = migrate.main(["--data-dir", str(built["dir"]), "--dry-run", "--default-profile",
                       built["Playground"]])
    assert rc == 0, capsys.readouterr().err


@pytest.mark.parametrize("ref, why", [("Nobody", "no profile"), ("Owner", "is an Admin"),
                                      ("Ann", "is an Admin"), ("Pinned", "has a PIN")])
def test_default_profile_that_cannot_be_one_is_refused(ref, why, tmp_path, monkeypatch, capsys):
    built = build_shape("b", tmp_path, monkeypatch)
    pinned = accounts.create_profile("Pinned", "restricted", pin_hash="x" * 20)
    assert pinned.has_pin
    before = _hashes(built["dir"])
    assert migrate.main(["--data-dir", str(built["dir"]), "--default-profile", ref]) == 2
    assert why in capsys.readouterr().err
    assert _hashes(built["dir"]) == before
    assert not _recorded(built["dir"])


def test_a_member_can_be_the_default_profile_and_keeps_all(tmp_path, monkeypatch):
    built = build_shape("b", tmp_path, monkeypatch)
    d = built["dir"]
    assert migrate.main(["--data-dir", str(d), "--default-profile", "Uma"]) == 0
    assert _stored(d)[built["Uma"]] == ("member", '"all"')
    assert _sec(d)["default_profile"] == built["Uma"]


# --------------------------------------------------------------------------- #
# The auto-run rule (server start: no flag) never raises
# --------------------------------------------------------------------------- #


def test_one_pinless_restricted_with_restricted_access_becomes_the_default(tmp_path, monkeypatch):
    built = build_shape("e", tmp_path, monkeypatch)
    d, play = built["dir"], built["Playground"]
    rep = migrate.run()
    assert "010_profiles" in rep.applied
    assert "the only PIN-less Restricted profile" in _text(rep)
    stored = _stored(d)
    assert stored[play][0] == "member"
    # (e): the character's profile list is folded in, then cleared.
    assert json.loads(stored[play][1]) == [*PACK, "guide", "story"]
    roster = _roster_on_disk(d)
    assert "profiles" not in roster["story"] and roster["story"]["hidden"] is True
    sec = _sec(d)
    assert sec["lock_target"] == "default_profile" and sec["default_profile"] == play


def test_two_restricted_profiles_means_no_default(tmp_path, monkeypatch):
    built = build_shape("b", tmp_path, monkeypatch)
    d = built["dir"]
    rep = migrate.run()
    assert "2 PIN-less Restricted profiles, so no guess" in _text(rep)
    stored = _stored(d)
    assert stored[built["Bo"]][0] == stored[built["Cy"]][0] == "restricted"
    assert json.loads(stored[built["Bo"]][1]) == [*PACK, "guide"]
    assert stored[built["Uma"]] == ("member", '"all"')
    assert stored[built["Ann"]] == ("admin", '"all"')
    assert _sec(d)["lock_target"] == "profile_list"


def test_a_pin_on_the_only_restricted_profile_means_no_default(tmp_path, monkeypatch):
    built = build_shape("e", tmp_path, monkeypatch)
    accounts.update_profile(built["Playground"], pin_hash="x" * 20)
    accounts.reset_for_tests()
    rep = migrate.run()
    assert "no PIN-less Restricted profile" in _text(rep)
    assert _stored(built["dir"])[built["Playground"]][0] == "restricted"
    assert _sec(built["dir"])["lock_target"] == "profile_list"


def test_an_open_install_gets_no_security_file_and_is_recorded(tmp_path, monkeypatch):
    built = build_shape("c", tmp_path, monkeypatch)
    d = built["dir"]
    assert not (d / "security.yaml").exists()
    rep = migrate.run()
    assert "010_profiles" in rep.applied and _recorded(d)
    assert "no security.yaml: an open install" in _text(rep)
    assert not (d / "security.yaml").exists()
    assert all(_roster_on_disk(d)[b]["hidden"] is True for b in PACK)


def test_an_open_install_refuses_an_explicit_default(tmp_path, monkeypatch, capsys):
    built = build_shape("c", tmp_path, monkeypatch)
    accounts.create_profile("Kit", "restricted")
    assert migrate.main(["--data-dir", str(built["dir"]), "--default-profile", "Kit"]) == 2
    assert "open install" in capsys.readouterr().err


def test_already_migrated_has_nothing_to_do(tmp_path, monkeypatch):
    built = build_shape("d", tmp_path, monkeypatch)
    before = _hashes(built["dir"])
    rep = migrate.run()
    assert rep.applied == [] and rep.backup is None
    assert _stored(built["dir"])[built["Playground"]] == ("restricted", '"all"')
    assert {k: v for k, v in _hashes(built["dir"]).items() if not k.startswith("chats.db")} == \
        {k: v for k, v in before.items() if not k.startswith("chats.db")}


def test_a_fresh_install_records_010_so_the_rule_never_fires_later(tmp_path, monkeypatch):
    """On a 4.0 install an Admin may make a PIN-less Basic profile with lock_target
    profile_list; a restart must not promote it to the default profile."""
    d = tmp_path / "fresh"
    _point_at(d, monkeypatch)
    rep = migrate.run()
    assert "010_profiles" in rep.applied and rep.backup is None
    assert _recorded(d)
    auth.set_pin("2468")
    kid = accounts.create_profile("Kit", Role.BASIC)
    _security(anonymous_access="restricted", lock_target="profile_list")
    assert _mine(migrate.plan()) == []
    migrate.run()
    assert accounts.get_profile(kid.id).role is Role.BASIC
    assert _sec(d)["lock_target"] == "profile_list"


def test_ambiguous_or_broken_inputs_never_raise(tmp_path, monkeypatch):
    built = build_shape("b", tmp_path, monkeypatch)
    d = built["dir"]
    (d / "config.yaml").write_text("bots: [this is: not valid")      # unreadable roster
    sec = _sec(d)
    sec["lock_target"] = "default_profile"
    sec["default_profile"] = "no-such-profile"
    (d / "security.yaml").write_text(yaml.safe_dump(sec))
    auth._cache = None
    rep = migrate.run()
    assert "010_profiles" in rep.applied
    assert "security.yaml names a default profile that can't be one" in _text(rep)
    assert _sec(d)["lock_target"] == "profile_list" and _sec(d).get("default_profile") is None
    assert json.loads(_stored(d)[built["Bo"]][1]) == []


# --------------------------------------------------------------------------- #
# Character profile lists (shape e and friends)
# --------------------------------------------------------------------------- #


def test_profile_lists_fold_without_widening_a_basic_profile(tmp_path, monkeypatch):
    built = build_shape("b", tmp_path, monkeypatch)
    d, bo, cy, uma = built["dir"], built["Bo"], built["Cy"], built["Uma"]
    roster = _roster([
        {"id": "story", "name": "Story", "audience": "all", "safe": True, "profiles": [bo],
         "advisor": {"providers": [{"provider": "openai_compatible", "model": "m"}]}},
        {"id": "desk", "name": "Desk", "audience": "members", "profiles": [cy, uma],
         "advisor": {"providers": [{"provider": "openai_compatible", "model": "m"}]}},
        {"id": "sprite", "name": "Sprite", "audience": "all", "safe": True, "profiles": [bo]},  # agent
    ])
    (d / "config.yaml").write_text(yaml.safe_dump({"bots": roster}, sort_keys=False))
    config._invalidate_bots_cache()
    text = _text(migrate.run(dry_run=True))
    assert "character 'story': profile list [Bo] folded into household_bots, then cleared" in text
    assert "character 'desk': profile list [Cy, Uma] folded into household_bots" in text
    assert "NOTE: character 'desk' lists Cy, who could not use it before" in text
    assert "NOTE: character 'story' was limited to its profile list; with the list cleared, Uma" in text
    migrate.run()
    stored = _stored(d)
    assert json.loads(stored[bo][1]) == [*PACK, "guide", "story"]      # sprite: an agent bot
    assert json.loads(stored[cy][1]) == [*PACK, "guide"]               # desk: never reachable
    assert stored[uma] == ("member", '"all"')
    on_disk = _roster_on_disk(d)
    assert all("profiles" not in on_disk[b] for b in ("story", "desk", "sprite"))


def test_unknown_keys_in_the_roster_survive(tmp_path, monkeypatch):
    built = build_shape("a", tmp_path, monkeypatch)
    d = built["dir"]
    raw = yaml.safe_load((d / "config.yaml").read_text())
    raw["bots"][0]["addon_field"] = {"kept": True}
    (d / "config.yaml").write_text(yaml.safe_dump(raw, sort_keys=False))
    migrate.run()
    assert _roster_on_disk(d)[PACK[0]]["addon_field"] == {"kept": True}
