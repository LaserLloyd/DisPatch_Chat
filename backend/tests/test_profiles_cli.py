"""``python -m app.profiles`` (4.0 chat-first profiles)."""
from __future__ import annotations

import pytest
from profiles_fixtures import scratch

from app import accounts, config, profiles, profiles_service
from app.contracts.permissions import Role


def run(s, *argv) -> int:
    return profiles.main(["--data-dir", str(s.root), *argv])


def test_counts(scratch, capsys):
    s = scratch
    s.admin()
    u = s.profile("Bram")
    s.thread(u.id)
    assert run(s, "counts", "--profile", "Bram") == 0
    assert "1 chats" in capsys.readouterr().out


def test_spawn_atlas_for_an_existing_profile_once(scratch, capsys):
    s = scratch
    s.connection()
    s.admin()
    u = s.profile("Bram")
    assert run(s, "spawn-atlas", "--profile", "Bram") == 0
    assert profiles_service.has_atlas(u.id)
    assert run(s, "spawn-atlas", "--profile", "Bram") == 1
    assert "already has an Atlas" in capsys.readouterr().err
    assert len(s.personal(u.id)) == 1
    assert run(s, "spawn-atlas", "--profile", "Bram", "--force") == 0
    assert len(s.personal(u.id)) == 2


def test_spawn_atlas_refuses_a_basic_profile_and_an_unknown_name(scratch, capsys):
    s = scratch
    s.admin()
    s.profile("Dee", Role.BASIC)
    assert run(s, "spawn-atlas", "--profile", "Dee") == 1
    assert run(s, "spawn-atlas", "--profile", "Nobody") == 1
    assert "No profile named" in capsys.readouterr().err


def test_create_a_user_and_a_basic_profile(scratch):
    s = scratch
    s.connection()
    s.admin()
    assert run(s, "create", "Science Group", "--role", "user") == 0
    u = accounts.get_by_name("Science Group")
    assert u.role is Role.USER and not u.has_pin and profiles_service.has_atlas(u.id)
    assert run(s, "create", "Little Group", "--role", "basic", "--household", "pack_otter,pack_heron") == 0
    b = accounts.get_by_name("Little Group")
    assert b.role is Role.BASIC and b.household_bots == ["pack_otter", "pack_heron"] and not s.personal(b.id)
    assert run(s, "create", "Little Group", "--role", "basic") == 1                    # duplicate name
    assert run(s, "create", "X", "--role", "user", "--household", "pack_otter") == 1   # a list is for Basic


def test_ensure_default(scratch, capsys):
    s = scratch
    s.connection()
    s.admin()
    assert run(s, "ensure-default") == 0
    assert "lands on it" in capsys.readouterr().out
    d = accounts.get_by_name("DisPatch")
    assert profiles_service.lock_target_profile_id() == d.id and config.get_bot(s.personal(d.id)[0].id)


def test_a_directory_without_a_database_is_refused(scratch, tmp_path_factory, capsys):
    # (uses the scratch fixture only so its monkeypatching restores the paths the CLI repoints)
    empty = tmp_path_factory.mktemp("empty")
    assert profiles.main(["--data-dir", str(empty), "ensure-default"]) == 1
    assert "no DisPatch database" in capsys.readouterr().err
