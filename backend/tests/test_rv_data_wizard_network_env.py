import pytest

from app import settings_store


@pytest.mark.parametrize("body", [{"exposure": "local"},
                                  {"exposure": "lan", "public_url": "https://chat.example.net", "behind_tls": True}])
def test_wizard_network_with_public_url_from_env(world, monkeypatch, body):
    monkeypatch.setenv("DISPATCH_PUBLIC_URL", "https://chat.example.net")
    c = world.client()
    world.create_owner(c)
    r = c.post("/api/wizard/network", json=body)
    assert r.status_code == 200, r.text
    assert settings_store.get("system.network_exposure") == body["exposure"]
    assert settings_store.get("system.public_url") == "https://chat.example.net"
