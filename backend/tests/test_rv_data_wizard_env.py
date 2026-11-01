from app import connections
from tests.test_i43_connections_env import ENV, ENV_VALUE, _code, _leaked, _mock_http, pair


def test_wizard_connection_cannot_bind_env_for_a_non_protected_admin(pair, monkeypatch):
    seen = _mock_http(monkeypatch)
    _owner, admin = pair
    r = admin.post("/api/wizard/connection",
                   json={"type": "openai_compatible", "base_url": "https://attacker.example/v1",
                         "api_key_env": ENV})
    assert not _leaked(seen, ENV_VALUE), seen
    assert r.status_code == 403, r.text
    assert _code(r) == "env_key_owner_only"
    assert connections.list_all() == []
