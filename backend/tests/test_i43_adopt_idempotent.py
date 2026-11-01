"""4.3 A3 (#17 N-14 006, #6 N-17).

Migration step 006 (``migrate._apply_connections``) gives every inline provider block a
connection and THEN writes the roster. A crash between the two left config.yaml with the inline
key and no ``connection``, so the next start created a second connection holding the same key.
``connections.adopt_inline`` now reuses a connection with the same type, address and key
fingerprint, the stored-secret kind included.

The secret file (agents/secrets.json) is private on every platform: ``fsperm.ensure_private``
follows each write (on Windows a chmod only toggles the read-only bit).
"""
from __future__ import annotations

import os
import stat
import sys

import pytest
import yaml

from app import config, connections, fsperm, llm_api, migrate, secret_store
from app.agents import secrets as agent_secrets

KEY = "sk-adopt-0123456789"          # scrub-ok: test fixture, not a real credential


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setenv("DISPATCH_SECRET_STORE", "file")
    secret_store.reset_for_tests()
    config._invalidate_bots_cache()
    entries = [
        {"id": "one", "name": "One", "advisor": {"providers": [
            {"provider": "openai", "model": "gpt-4o", "api_key": KEY}]}},
        {"id": "two", "name": "Two", "api": {"provider": "openai", "model": "gpt-4o-mini", "api_key": KEY}},
    ]
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"bots": entries}), encoding="utf-8")
    yield tmp_path
    secret_store.reset_for_tests()


def test_a_crash_between_create_and_roster_write_does_not_duplicate(data, monkeypatch):
    real = config._write_bots
    calls = {"n": 0}

    def flaky(entries):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("simulated crash after the connections were created")
        return real(entries)
    monkeypatch.setattr(config, "_write_bots", flaky)
    with pytest.raises(OSError):
        migrate._apply_connections()
    assert len(connections.list_all()) == 1                # created before the crash
    migrate._apply_connections()                           # the next start
    conns = connections.list_all()
    assert len(conns) == 1, [c.id for c in conns]
    raw = yaml.safe_load(config.CONFIG_PATH.read_text(encoding="utf-8"))
    for e in raw["bots"]:
        (block,) = connections.blocks_of(e)
        assert block["connection"] == conns[0].id and "api_key" not in block
        assert llm_api.resolve_key(block) == KEY
    assert KEY not in config.CONFIG_PATH.read_text(encoding="utf-8")


def test_another_key_on_the_same_server_is_still_another_connection(data):
    connections.create(type="openai", api_key="sk-other-0123456789", conn_id="openai")  # scrub-ok: test fixture, not a real credential
    raw = yaml.safe_load(config.CONFIG_PATH.read_text(encoding="utf-8"))
    plan = connections.adopt_inline(raw["bots"])
    assert len(plan.created) == 1 and len(connections.list_all()) == 2
    # and the same key again (a second run) reuses the new one
    roster = [{"id": "three", "api": {"provider": "openai", "model": "m", "api_key": KEY}}]
    again = connections.adopt_inline(roster)
    assert again.created == [] and roster[0]["api"]["connection"] == plan.created[0]


def test_the_dry_run_reports_reuse_of_a_stored_key(data):
    connections.create(type="openai", api_key=KEY, conn_id="openai")
    raw = yaml.safe_load(config.CONFIG_PATH.read_text(encoding="utf-8"))
    plan = connections.adopt_inline(raw["bots"], dry_run=True)
    assert not any("create connection" in a for a in plan.actions), plan.actions
    assert len(connections.list_all()) == 1


# ---- (e) the secret file is private ---------------------------------------- #

def test_the_secret_file_is_private_and_ensure_private_follows_each_write(tmp_path, monkeypatch):
    calls: list[str] = []
    real = fsperm.ensure_private

    def spy(path, **kw):
        calls.append(str(path))
        return real(path, **kw)
    monkeypatch.setattr(fsperm, "ensure_private", spy)
    p = tmp_path / "agents" / "secrets.json"
    store = agent_secrets.FileSecretProvider(p)
    store.set("connection.x", "value-1")
    assert calls == [str(p)]
    store.delete("connection.x")
    assert calls == [str(p), str(p)]
    if sys.platform != "win32":
        assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    # a file that was widened by hand is narrowed again on the next write
    if sys.platform != "win32":
        os.chmod(p, 0o644)
        store.set("connection.y", "value-2")
        assert stat.S_IMODE(os.stat(p).st_mode) == 0o600


def test_the_secret_store_writes_through_the_private_file(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setenv("DISPATCH_SECRET_STORE", "file")
    secret_store.reset_for_tests()
    calls: list[str] = []
    monkeypatch.setattr(fsperm, "ensure_private", lambda path, **kw: calls.append(str(path)) or True)
    secret_store.put("connection.z", "value-3")
    assert calls == [str(tmp_path / "agents" / "secrets.json")]
    secret_store.reset_for_tests()
