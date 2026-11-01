import pytest

from tests.test_i43_host_tier import _install


@pytest.mark.parametrize("key,val", [("tools.practice_url", "https://attacker.example"),
                                     ("tools.mail_remote_url", "https://attacker.example")])
def test_a_second_admin_cannot_repoint_where_host_secrets_go(world, key, val):
    _base, ann, _ = _install(world)
    r = ann.patch("/api/settings", json={"set": {key: val}})
    assert r.status_code == 403, r.text
