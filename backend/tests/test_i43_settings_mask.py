"""4.3 A3 (#6, N-17): ``GET /api/settings`` never echoes the value of a key the schema does not
know. A namespace writer (a plugin, the voice add-on) may store anything under its own prefix,
a token included; the Settings page only needs to know that it is there."""
from __future__ import annotations

from app import settings_store


def test_an_unknown_keys_value_goes_out_masked(world):
    owner = world.create_owner()
    settings_store.writer("foo")("foo.token", "abc")
    settings_store.writer("foo")("foo.flags", {"x": 1})
    r = owner.get("/api/settings")
    assert r.status_code == 200, r.text
    extra = r.json()["extra"]
    assert extra["foo.token"] == {"masked": True, "type": "string", "length": 3}
    assert extra["foo.flags"]["masked"] is True and extra["foo.flags"]["type"] == "object"
    assert '"abc"' not in r.text
    # the value itself is still there for the code that owns the namespace
    assert settings_store.get("foo.token") == "abc"


def test_a_carried_over_unknown_value_goes_out_masked(world):
    owner = world.create_owner()
    with settings_store.connect() as con:
        con.execute("INSERT INTO settings_unknown (origin, key, value, first_seen) VALUES (?,?,?,?)",
                    ("security.yaml", "old_thing", '"plain-old-value"', "2026-01-01T00:00:00+00:00"))
    r = owner.get("/api/settings")
    assert "plain-old-value" not in r.text
    (row,) = [u for u in r.json()["unknown"] if u["key"] == "old_thing"]
    assert row["value"] == {"masked": True, "type": "string", "length": len("plain-old-value")}


def test_a_secret_settings_shape_is_unchanged(world):
    owner = world.create_owner()
    assert owner.patch("/api/settings", json={"set": {"system.backup_passphrase": "hunter2-hunter2"}}).status_code == 200
    r = owner.get("/api/settings")
    v = r.json()["values"]["system.backup_passphrase"]
    assert v["value"] is None and v["secret"] is True and v["set"] is True and "masked" not in v
    assert "hunter2" not in r.text
    # a known, non-secret value is still shown as it is
    assert "masked" not in r.json()["values"]["appearance.default_theme"]
