"""The settings schema and store (3.0, W5): one description of every setting, one store.

Covers the schema's own invariants, validation, the store (defaults, env override, restart
tracking, secrets, file-bound adapters), the import of today's files (idempotent, dry-run that
writes nothing, unknown keys carried and reported), the namespace writer an add-on uses, and the
HTTP surface with its permissions.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import yaml

from app import auth, config, settings_schema, settings_store
from app.settings_schema import SettingError


@pytest.fixture
def owner(world):
    return world.create_owner()


# --------------------------------------------------------------------------- #
# The schema itself
# --------------------------------------------------------------------------- #


def test_every_definition_is_well_formed():
    seen_env: dict[str, str] = {}
    for d in settings_schema.CORE_SETTINGS:
        assert settings_schema.KEY_RE.match(d.key), d.key
        assert d.key.split(".", 1)[0] == d.group
        assert d.label and d.description and d.description.endswith((".", ")")), d.key
        if d.choices:
            assert d.default in d.choices or d.choices_from, f"{d.key}: default not among the choices"
        # the default must itself pass validation (a schema that rejects its own default is broken)
        if d.default is not None and not d.secret:
            assert settings_schema.coerce(d, d.default) == d.default, d.key
        for name in d.env_names():
            assert name not in seen_env, f"{name} is claimed by {seen_env[name]} and {d.key}"
            seen_env[name] = d.key
        if d.env and d.env.startswith("DISPATCH_"):
            assert d.env_names()[1] == "LOCAL_CHAT_" + d.env[len("DISPATCH_"):]
        assert d.binding == "store" or d.binding in settings_store.ADAPTERS


def test_groups_follow_the_brief_order():
    assert [g[1] for g in settings_schema.GROUPS] == [
        "Characters", "Connections", "Voice", "Tools & Modules", "People & Profiles", "Appearance", "System"]
    assert {d.group for d in settings_schema.CORE_SETTINGS} <= set(settings_schema.GROUP_IDS)


def test_schema_document_is_versioned_and_complete():
    doc = settings_schema.schema_document()
    assert doc["schema"] == settings_schema.SCHEMA_VERSION == 1
    keys = {s["key"] for s in doc["settings"]}
    assert {"system.backup_keep", "people.lock_timeout_seconds", "tools.livetools_enabled",
            "characters.default_audience", "connections.allow_private_targets"} <= keys
    first = next(s for s in doc["settings"] if s["key"] == "system.backup_keep")
    assert first["env_names"] == ["DISPATCH_BACKUP_KEEP", "LOCAL_CHAT_BACKUP_KEEP"]
    assert first["restart_required"] is True and first["level"] == "basic"


@pytest.mark.parametrize("key,raw,ok", [
    ("system.backup_keep", 5, True), ("system.backup_keep", "5", True), ("system.backup_keep", 0, False),
    ("system.backup_keep", 1.5, False), ("system.backup_keep", True, False), ("system.backup_keep", "x", False),
    ("system.behind_tls", "yes", True), ("system.behind_tls", "maybe", False),
    ("system.network_exposure", "lan", True), ("system.network_exposure", "world", False),
    ("system.public_url", "https://chat.example.net", True), ("system.public_url", "ftp://x", False),
    ("system.public_url", "https://u:p@example.net", False), ("system.public_url", "", True),
    ("people.lock_timeout_seconds", 600, True), ("people.lock_timeout_seconds", 5, False),
    ("tools.mirror_kinds", "webchat,main", True),
])
def test_coerce_accepts_and_refuses(key, raw, ok):
    d = settings_schema.get_def(key)
    if ok:
        settings_schema.coerce(d, raw)
    else:
        with pytest.raises(SettingError):
            settings_schema.coerce(d, raw)


def test_a_plugin_fragment_lives_under_tools_id():
    defs = settings_schema.from_fragment(
        [{"key": "region", "type": "choice", "label": "Region", "choices": ["eu", "us"], "default": "eu"},
         {"key": "token", "type": "secret", "label": "Token", "env": "WEATHER_TOKEN"}],
        namespace="tools.weather", owner="tool:weather")
    settings_schema.register(defs, "tool:weather")
    assert settings_schema.get_def("tools.weather.region").default == "eu"
    assert settings_schema.get_def("tools.weather.token").secret is True
    with pytest.raises(ValueError):
        settings_schema.register(settings_schema.from_fragment(
            [{"key": "x", "type": "string", "label": "x"}], namespace="tools.other", owner="tool:weather"),
            "tool:weather")


# --------------------------------------------------------------------------- #
# The store
# --------------------------------------------------------------------------- #


def test_defaults_then_a_stored_value(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    r = settings_store.resolve("system.backup_keep")
    assert (r.value, r.source) == (12, "default")
    res = settings_store.set_many({"system.backup_keep": 20})
    assert res.ok and res.changed == ["system.backup_keep"]
    r = settings_store.resolve("system.backup_keep")
    assert (r.value, r.source) == (20, "store")
    # reset returns it to the default and removes the row
    settings_store.set_many({}, reset=["system.backup_keep"])
    assert settings_store.resolve("system.backup_keep").source == "default"


def test_a_stored_value_reaches_the_old_environment_readers(tmp_path, monkeypatch):
    """config.env() answers from the store when no environment variable is set."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.delenv("DISPATCH_BACKUP_KEEP", raising=False)
    monkeypatch.delenv("LOCAL_CHAT_BACKUP_KEEP", raising=False)
    assert config.env("BACKUP_KEEP", "12") == "12"
    settings_store.set_many({"system.backup_keep": 31})
    assert config.env("BACKUP_KEEP", "12") == "31"
    settings_store.set_many({"tools.mirror_enabled": False})
    assert config.env("MIRROR", "1") == "0"
    # an empty string means "unset": the caller's default applies, not ""
    settings_store.set_many({"system.log_file": ""})
    assert config.env("LOG_FILE", "dflt") == "dflt"


def test_the_environment_wins_and_is_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    settings_store.set_many({"system.backup_keep": 20})
    monkeypatch.setenv("LOCAL_CHAT_BACKUP_KEEP", "3")           # the legacy spelling still works
    r = settings_store.resolve("system.backup_keep")
    assert (r.value, r.source, r.env_name) == (3, "env", "LOCAL_CHAT_BACKUP_KEEP")
    res = settings_store.set_many({"system.backup_keep": 9})
    assert not res.ok and "LOCAL_CHAT_BACKUP_KEEP" in res.errors["system.backup_keep"]
    assert config.env("BACKUP_KEEP", "12") == "3"
    # a malformed variable is reported, not obeyed
    monkeypatch.setenv("LOCAL_CHAT_BACKUP_KEEP", "many")
    r = settings_store.resolve("system.backup_keep")
    assert r.source == "store" and r.value == 20 and r.invalid_env


def test_validation_is_all_or_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    res = settings_store.set_many({"system.backup_keep": 7, "system.network_exposure": "nope", "x.y": 1})
    assert set(res.errors) == {"system.network_exposure", "x.y"}
    assert settings_store.resolve("system.backup_keep").source == "default"       # nothing applied


def test_restart_pending_is_tracked_from_the_value_at_first_look(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    assert settings_store.resolve("system.backup_keep").restart_pending is False
    res = settings_store.set_many({"system.backup_keep": 4})
    assert res.restart_required == ["system.backup_keep"]
    assert settings_store.resolve("system.backup_keep").restart_pending is True
    settings_store.set_many({"system.backup_keep": 12})                          # back to what is running
    assert settings_store.resolve("system.backup_keep").restart_pending is False
    # a setting that applies at once is never "pending"
    assert settings_store.set_many({"people.restricted_daily_turns": 5}).restart_required == ["people.restricted_daily_turns"]
    assert settings_store.set_many({"characters.greeting": True}).restart_required == []


def test_a_secret_goes_to_the_secret_store_and_is_never_returned(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    settings_store.set_many({"system.backup_passphrase": "correct horse battery"})
    r = settings_store.resolve("system.backup_passphrase")
    assert r.secret_set is True and r.value is None
    assert r.public()["value"] is None and r.public()["set"] is True
    with settings_store.connect() as con:
        stored = con.execute("SELECT count(*) FROM settings WHERE key='system.backup_passphrase'").fetchone()[0]
    assert stored == 0                                   # not in the settings table
    from app import secret_store
    assert secret_store.get("setting.system.backup_passphrase") == "correct horse battery"
    settings_store.set_many({"system.backup_passphrase": ""})
    assert settings_store.resolve("system.backup_passphrase").secret_set is False


def test_a_file_bound_setting_writes_through_to_its_file(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(auth, "SECURITY_PATH", tmp_path / "security.yaml")
    monkeypatch.setattr(auth, "RECOVERY_PATH", tmp_path / "RECOVERY-CODE.txt")
    monkeypatch.setattr(auth, "TRUSTED_PATH", tmp_path / "trusted-devices.yaml")
    auth._cache = None
    assert settings_store.resolve("people.lock_timeout_seconds").value == 600
    assert settings_store.set_many({"people.lock_timeout_seconds": 900,
                                    "people.session_timeout_member": 1200}).ok
    raw = yaml.safe_load((tmp_path / "security.yaml").read_text())
    assert raw["lock_timeout_seconds"] == 900 and raw["session_timeout_member"] == 1200
    assert auth.lock_timeout() == 900                       # the code that reads the file sees it
    # a hand edit of the file is what the store reports afterwards (the file is the home)
    raw["lock_timeout_seconds"] = 1500
    (tmp_path / "security.yaml").write_text(yaml.safe_dump(raw))
    auth._cache = None
    assert settings_store.resolve("people.lock_timeout_seconds").value == 1500


def test_livetools_switch_is_the_tools_yaml_switch(tmp_path, monkeypatch):
    from app import tools
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    assert settings_store.resolve("tools.livetools_enabled").value is True
    assert settings_store.set_many({"tools.livetools_enabled": False}).ok
    assert tools.livetools_enabled() is False
    assert yaml.safe_load((tmp_path / "tools.yaml").read_text())["livetools"] == {"enabled": False}


# --------------------------------------------------------------------------- #
# Importing today's files
# --------------------------------------------------------------------------- #


def _legacy_files(d: Path) -> None:
    (d / "security.yaml").write_text(yaml.safe_dump({
        "pin_hash": "abc", "salt": "def", "lock_timeout_seconds": 777, "remember_device_days": 14,
        "api_token": "tok-secret-value", "machine_loopback_trust": False, "quirky_new_flag": "hello",
        "my_custom_token": "super-secret"}))
    (d / "tools.yaml").write_text(yaml.safe_dump({
        "tools": [], "livetools": {"enabled": False, "mystery": 3}}))
    (d / "config.yaml").write_text(yaml.safe_dump({"bots": [], "theme_hint": "dark"}))


def test_import_carries_values_and_never_drops_an_unknown_key(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    _legacy_files(tmp_path)
    rep = settings_store.import_legacy()
    got = {i["key"]: i["value"] for i in rep.imported}
    assert got["people.lock_timeout_seconds"] == 777 and got["people.remember_device_days"] == 14
    assert got["people.machine_loopback_trust"] is False and got["tools.livetools_enabled"] is False
    unknown = {(u["origin"], u["key"]): u["value"] for u in settings_store.unknown()}
    assert ("security.yaml", "quirky_new_flag") in unknown and unknown[("security.yaml", "quirky_new_flag")] == "hello"
    assert ("tools.yaml", "livetools.mystery") in unknown and ("config.yaml", "theme_hint") in unknown
    # credentials and secret-looking keys are NEVER copied: managed state is skipped, the rest masked
    assert not any(k[1] in ("pin_hash", "salt", "api_token") for k in unknown)
    assert unknown[("security.yaml", "my_custom_token")] == "***"
    db = sqlite3.connect(tmp_path / "chats.db")
    dump = repr(db.execute("SELECT * FROM settings_unknown").fetchall()) + repr(db.execute("SELECT * FROM settings").fetchall())
    db.close()
    assert "super-secret" not in dump and "tok-secret-value" not in dump


def test_import_is_idempotent_and_never_overwrites_a_chosen_value(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    _legacy_files(tmp_path)
    settings_store.import_legacy()
    settings_store.set_many({"tools.livetools_enabled": True})        # the owner changes his mind
    again = settings_store.import_legacy()
    assert again.imported == [] and again.unknown == [] and again.already
    assert settings_store.get("tools.livetools_enabled") is True


def test_the_dry_run_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    _legacy_files(tmp_path)
    rep = settings_store.import_legacy(dry_run=True)
    assert rep.imported and rep.unknown and any("lock_timeout" in line for line in rep.lines())
    assert not (tmp_path / "chats.db").exists()


# --------------------------------------------------------------------------- #
# The namespace writer (the voice add-on's migrate_voice_yaml calls this)
# --------------------------------------------------------------------------- #


def test_writer_accepts_qualified_and_bare_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    w = settings_store.writer("voice")
    w("voice.speed", 1.25)                       # the form W6b's migration uses
    w("rt.power_hold", True)                     # bare: the namespace is added
    assert settings_store.get("voice.speed") == 1.25
    assert settings_store.get("voice.rt.power_hold") is True
    assert settings_store.get("voice.nope", "dflt") == "dflt"
    assert settings_store.extra_values() == {"voice.speed": 1.25, "voice.rt.power_hold": True}
    with pytest.raises(ValueError):
        settings_store.writer("Bad Name")


def test_writer_validates_a_key_the_schema_knows(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    settings_schema.register(settings_schema.from_fragment(
        [{"key": "speed", "type": "number", "label": "Speed"}], namespace="voice", owner="voice"), "voice")
    w = settings_store.writer("voice")
    w("speed", 1.5)
    assert settings_store.resolve("voice.speed").value == 1.5
    with pytest.raises(SettingError):
        w("voice.speed", "fast")


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #


def test_get_schema_and_values(world, owner):
    sch = owner.get("/api/settings/schema").json()
    assert sch["schema"] == 1 and {g["id"] for g in sch["groups"]} >= {"system", "people", "tools"}
    doc = owner.get("/api/settings").json()
    v = doc["values"]["system.backup_interval"]
    assert v["value"] == 21600 and v["source"] == "default" and v["is_default"] is True
    assert "system.backup_passphrase" in doc["values"] and doc["values"]["system.backup_passphrase"]["secret"] is True
    # filters
    only_people = owner.get("/api/settings", params={"group": "people"}).json()["values"]
    assert only_people and all(k.startswith("people.") for k in only_people)
    found = owner.get("/api/settings", params={"q": "snapshot"}).json()["values"]
    assert "system.backup_interval" in found
    assert owner.get("/api/settings", params={"group": "nope"}).status_code == 422
    adv = owner.get("/api/settings", params={"level": "advanced"}).json()["values"]
    assert "system.host" in adv and "system.backup_keep" not in adv


def test_patch_set_reset_and_per_key_errors(world, owner):
    r = owner.patch("/api/settings", json={"set": {"system.backup_keep": 8, "characters.greeting": True}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body["changed"]) == {"system.backup_keep", "characters.greeting"}
    assert body["restart_required"] == ["system.backup_keep"]
    assert owner.get("/api/settings").json()["values"]["system.backup_keep"]["value"] == 8
    r = owner.patch("/api/settings", json={"reset": ["system.backup_keep"]})
    assert r.json()["values"]["system.backup_keep"]["is_default"] is True
    bad = owner.patch("/api/settings", json={"set": {"system.backup_keep": 0, "system.nope": 1}})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "settings_invalid"
    assert set(bad.json()["errors"]) == {"system.backup_keep", "system.nope"}
    assert owner.patch("/api/settings", json={}).status_code == 422
    assert owner.patch("/api/settings", json={"bogus": 1}).status_code == 422
    assert owner.patch("/api/settings", json={"set": {"system.backup_keep": 3}, "reset": ["system.backup_keep"]}
                       ).status_code == 422


def test_an_environment_variable_is_reported_and_blocks_the_change(world, owner, monkeypatch):
    monkeypatch.setenv("DISPATCH_BACKUP_KEEP", "2")
    v = owner.get("/api/settings").json()["values"]["system.backup_keep"]
    assert v["source"] == "env" and v["value"] == 2 and v["env"] == "DISPATCH_BACKUP_KEEP"
    r = owner.patch("/api/settings", json={"set": {"system.backup_keep": 9}})
    assert r.status_code == 422 and "DISPATCH_BACKUP_KEEP" in r.json()["errors"]["system.backup_keep"]


def test_secrets_never_appear_in_any_settings_response(world, owner):
    owner.patch("/api/settings", json={"set": {"system.backup_passphrase": "hunter2-hunter2"}})
    for url in ("/api/settings", "/api/settings/schema"):
        assert "hunter2" not in owner.get(url).text
    assert owner.get("/api/settings").json()["values"]["system.backup_passphrase"]["set"] is True


def test_settings_need_manage_settings(world, owner):
    mia = world.person(owner, "Mia", "restricted")
    kid = world.client(("203.0.113.50", 40000))
    world.login(kid, profile_id=mia["id"])
    stranger = world.client(("203.0.113.51", 40001))
    max_ = world.person(owner, "Max", "member", pin="5555")
    member = world.client(("203.0.113.52", 40002))
    world.login(member, profile_id=max_["id"], pin="5555")
    for c in (kid, stranger, member):
        assert c.get("/api/settings").status_code == 403
        assert c.get("/api/settings/schema").status_code == 403
        assert c.patch("/api/settings", json={"set": {"system.backup_keep": 3}}).status_code == 403
    # the one public route carries no secrets and works for a stranger
    d = stranger.get("/api/settings/defaults")
    assert d.status_code == 200 and set(d.json()) == {"theme", "language", "avatar_style"}


def test_a_settings_change_is_audited_by_key_not_value(world, owner):
    owner.patch("/api/settings", json={"set": {"system.public_url": "https://secret-host.example.net"}})
    audit = owner.get("/api/people/audit").json()
    text = str(audit)
    assert "settings.change" in text and "system.public_url" in text and "secret-host" not in text
