"""Migration 010_profiles (4.0 chat-first profiles, BUILD-PLAN-v2 §1.6): skeleton.

Foundation registered the step (``migrate.plan_profiles`` / ``migrate.apply_profiles``, the
``--default-profile`` flag and its "refused after 010 ran" rule) and wrote the neutral
fixture builders below. WP-MIG implements the step and adds its tests here:

* the dry run writes nothing (sha256 of the data dir before/after) and prints, per profile,
  old stored role -> new role -> household list, the capabilities the default profile
  gains, the bot flag changes and the security.yaml diff;
* apply on shape (a) gives the expected roles / lists / ``hidden`` / ``lock_target``;
* idempotent; ``--restore`` round trip; ``--default-profile`` refused after 010;
* the auto-run rule never raises (shapes b, c, d);
* sessions, devices and remembered devices survive with their rows intact;
* shape (e): a character ``profiles`` list folds into those profiles' ``household_bots``.

Shapes (every name neutral):
  a  live-shaped: Admin "Owner" with a PIN, a PIN-less Restricted "Playground", three pack
     bots (pack_otter, pack_heron, pack_newt: audience restricted, safe, hide_unlocked), an
     agent bot ``helper``, two direct bots, chats per owner, ``anonymous_access: none``
  b  Owner + Admin + Member + two Restricted profiles
  c  an open install: no Owner, no PIN
  d  already migrated (010 recorded)
  e  a character with a ``profiles`` list
"""
from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest
import yaml

from app import accounts, auth, config, migrate

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
        migrate.run(take_backup=False)
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
    migrate.run(take_backup=False)                         # 001-009 recorded, 010 not (yet)
    if shape == "d":
        con = sqlite3.connect(d / "chats.db")
        con.execute("INSERT OR IGNORE INTO schema_migrations (id, applied_at, app_version) "
                    "VALUES ('010_profiles', '2026-10-22T00:00:00+00:00', '4.0.0')")
        con.commit()
        con.close()
    accounts.reset_for_tests()
    auth._cache = None
    return out


@pytest.mark.parametrize("shape", ["a", "b", "c", "d", "e"])
def test_every_shape_builds_and_plans_without_raising(shape, tmp_path, monkeypatch):
    built = build_shape(shape, tmp_path, monkeypatch)
    rep = migrate.plan()
    assert all(a.step != "010_profiles" for a in rep.actions)   # Foundation: the stub plans nothing
    assert migrate.plan_profiles() == []
    if shape in ("a", "e"):
        assert accounts.get_profile(built["Playground"]).role.value == "basic"


def test_the_stub_never_records_010(tmp_path, monkeypatch):
    build_shape("a", tmp_path, monkeypatch)
    migrate.run(take_backup=False)
    assert "010_profiles" not in migrate._applied_ids(migrate._db_file())


def test_default_profile_is_refused_once_010_has_run(tmp_path, monkeypatch, capsys):
    built = build_shape("d", tmp_path, monkeypatch)
    rc = migrate.main(["--data-dir", str(built["dir"]), "--dry-run", "--default-profile", "Playground"])
    assert rc == 2
    assert "010_profiles has already run" in capsys.readouterr().err


def test_default_profile_is_accepted_before_010(tmp_path, monkeypatch, capsys):
    built = build_shape("a", tmp_path, monkeypatch)
    rc = migrate.main(["--data-dir", str(built["dir"]), "--dry-run", "--default-profile", "Playground"])
    assert rc == 0, capsys.readouterr().err
