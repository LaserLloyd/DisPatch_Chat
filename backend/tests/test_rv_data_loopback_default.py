from app import auth, settings_schema, settings_store


def test_schema_default_matches_fresh_install(monkeypatch):
    monkeypatch.setattr(auth, "FRESH_INSTALL_LOOPBACK_TRUST", False)   # the shipped value
    d = settings_schema.get_def("people.machine_loopback_trust")
    assert d.default is auth.FRESH_INSTALL_LOOPBACK_TRUST


def test_reset_never_turns_loopback_trust_on(world, monkeypatch):
    monkeypatch.setattr(auth, "FRESH_INSTALL_LOOPBACK_TRUST", False)
    world.create_owner()
    settings_store.set_many({"people.machine_loopback_trust": False})
    assert auth.load().machine_loopback_trust is False
    settings_store.set_many({}, reset=["people.machine_loopback_trust"])
    assert auth.load().machine_loopback_trust is False, "Reset to default wrote trust ON"
