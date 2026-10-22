"""Privacy matrix, lifecycle: after Reset or a User -> Basic downgrade, NOTHING of the profile's
content is left anywhere in the data directory (sentinel grep over every file, SQLite free pages
included), while every other profile's content is untouched."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from privacy_fixtures import HOUSEHOLD_BOT, PRIVACY_MARKS, build_install

from app import profiles_service

pytestmark = PRIVACY_MARKS

WIPED_KINDS = ("thread", "file", "task", "image_job", "advisor_request", "personal_bot", "studio")


def _compact(root: Path) -> None:
    """DELETE leaves bytes on free pages; compact every SQLite file so the grep sees only live data."""
    for p in root.rglob("*"):
        if p.is_file() and p.suffix in (".db", ".sqlite", ".sqlite3"):
            try:
                con = sqlite3.connect(p, timeout=5)
                con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                con.execute("VACUUM")
                con.close()
            except sqlite3.Error:
                pass


def _grep(root: Path, needles: list[str]) -> dict[str, list[str]]:
    hits: dict[str, list[str]] = {}
    raw = [n.encode() for n in needles]
    for p in root.rglob("*"):
        if not p.is_file() or p.stat().st_size > 64 * 1024 * 1024:
            continue
        try:
            data = p.read_bytes()
        except OSError:
            continue
        for n, b in zip(needles, raw, strict=True):
            if b in data:
                hits.setdefault(n, []).append(str(p.relative_to(root)))
    return hits


def _sentinels(inst, owner):
    return [r.sentinel for (k, o), r in inst.res.items() if o == owner and k in WIPED_KINDS]


def _check(inst, victim: str, keep: str):
    inst.run(lambda: inst.world.db.close()) if False else None
    _compact(inst.world.tmp)
    gone = _grep(inst.world.tmp, _sentinels(inst, victim))
    assert not gone, f"{victim}'s content survived: {gone}"
    kept = _grep(inst.world.tmp, _sentinels(inst, keep))
    missing = [s for s in _sentinels(inst, keep) if s not in kept]
    assert not missing, f"{keep}'s content was damaged: {missing}"


def test_reset_leaves_no_trace_and_touches_nobody_else(world, monkeypatch):
    inst = build_install(world, monkeypatch, "A0")
    victim = "U1"
    pid = inst.pids[victim]
    prev = profiles_service.reset_preview(pid)
    assert prev["counts"]["chats"] >= 1 and prev["counts"]["bots"] >= 1
    profiles_service.reset_profile(pid, prev["token"])
    _check(inst, victim, "A0")


def test_reset_with_a_stale_token_changes_nothing(world, monkeypatch):
    inst = build_install(world, monkeypatch, "A0")
    pid = inst.pids["U1"]
    prev = profiles_service.reset_preview(pid)
    inst.run(lambda: world.db.create_thread(HOUSEHOLD_BOT, title="newer", owner_profile_id=pid))
    with pytest.raises(profiles_service.ProfileServiceError) as e:
        profiles_service.reset_profile(pid, prev["token"])
    assert e.value.status == 409
    assert _grep(inst.world.tmp, [inst.sent("thread", "U1")]), "a refused reset must not delete anything"


def test_user_to_basic_downgrade_wipes_private_content(world, monkeypatch):
    inst = build_install(world, monkeypatch, "A0")
    victim = "U1"
    pid = inst.pids[victim]
    prev = profiles_service.change_role_preview(pid, "basic")
    profiles_service.change_role(pid, "basic", confirm_token=prev["token"], household_bots=[HOUSEHOLD_BOT])
    _check(inst, victim, "A0")
    assert [b for b in __import__("app.config", fromlist=["x"]).load_bots() if b.owner_profile == pid] == []


def test_basic_to_user_keeps_chats(world, monkeypatch):
    inst = build_install(world, monkeypatch, "A0")
    pid = inst.pids["B1"]
    profiles_service.change_role(pid, "user")
    assert _grep(inst.world.tmp, [inst.sent("thread", "B1")]), "Basic -> User must keep the profile's chats"
