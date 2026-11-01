"""4.3 Fable: a Host allow-list against DNS rebinding on a claimed install.

A rebinding page (``evil.example`` re-pointed at this machine's LAN / tailnet / loopback
address) reaches DisPatch with ITS OWN name in the Host header. On a claimed install it would
land on the PIN-less default profile and pass CSRF (its Origin equals its own Host). The gate
in ``main._HostAllowList`` compares the Host's name with what this machine answers to
(``policy.host_allowed``): ``log`` (default) counts and logs a stranger, ``enforce`` refuses it
with 421 (a WebSocket is closed 1008).

Run: cd backend && uv run pytest tests/test_fable_host_allowlist.py
"""
from __future__ import annotations

import pytest
from starlette.websockets import WebSocketDisconnect

from app import auth, policy, settings_api, settings_schema

EVIL = "evil.example"


@pytest.fixture(autouse=True)
def _facts(monkeypatch):
    """A deterministic machine: hostname ``chatbox``, a LAN and a tailnet address."""
    monkeypatch.setattr(policy, "_hostnames", lambda: {"chatbox", "chatbox.lan"})
    monkeypatch.setattr(policy, "_interface_addresses",
                        lambda: {"198.51.100.37", "203.0.113.44", "2001:db8:1::1", "127.0.0.1", "::1"})
    for name in ("DISPATCH_HOST_CHECK", "DISPATCH_ALLOWED_HOSTS", "DISPATCH_PUBLIC_URL",
                 "DISPATCH_CALLBACK_BASE", "DISPATCH_TRUSTED_PROXIES", "LOCAL_CHAT_PUBLIC_URL",
                 "LOCAL_CHAT_CALLBACK_BASE", "LOCAL_CHAT_HOST_CHECK", "LOCAL_CHAT_ALLOWED_HOSTS"):
        monkeypatch.delenv(name, raising=False)
    policy.reset_host_check_state()
    policy.refresh_host_facts()
    yield
    policy.reset_host_check_state()


@pytest.fixture
def sec(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "SECURITY_PATH", tmp_path / "security.yaml")
    monkeypatch.setattr(auth, "RECOVERY_PATH", tmp_path / "RECOVERY-CODE.txt")
    monkeypatch.setattr(auth, "TRUSTED_PATH", tmp_path / "trusted-devices.yaml")
    auth._bust_cache()
    yield tmp_path
    auth._bust_cache()


def _set_security(**fields):
    with auth._lock:
        cfg = auth.load()
        for k, v in fields.items():
            setattr(cfg, k, v)
        auth._write(cfg)
    auth._bust_cache()


# --------------------------------------------------------------------------- #
# (a) the allowed set
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("host", [
    "localhost", "LOCALHOST:8765", "127.0.0.1", "127.0.0.1:8765", "127.9.9.9", "[::1]", "[::1]:8765",
    "::1", "[::ffff:127.0.0.1]:8765",
    "chatbox", "chatbox:8765", "ChatBox.lan", "chatbox.local", "chatbox.local:8765",
    "chatbox.example.ts.net", "chatbox.example.ts.net:443",  # scrub-ok: Tailscale's own domain suffix
    "198.51.100.37", "198.51.100.37:8765", "203.0.113.44:8765",
    "[2001:db8:1::1]:8765", "[2001:DB8:1:0::1]",
])
def test_names_this_machine_answers_to(sec, host):
    assert policy.host_allowed(host), host


@pytest.mark.parametrize("host", [
    EVIL, f"{EVIL}:8765", "chatbox.evil.example", "evil-chatbox.example.ts.net", "chatbox.ts.net",  # scrub-ok: Tailscale's own domain suffix
    "other.example.ts.net", "127.0.0.1.nip.io", "198.51.100.38", "[2001:db8:1::2]",  # scrub-ok: Tailscale's own domain suffix
    "localhost.evil.example", "", None, "chatbox.local.evil.example",
])
def test_strangers(sec, host):
    assert not policy.host_allowed(host), host


def test_interface_addresses_refresh_after_a_minute(sec, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(policy, "_now", lambda: clock[0])
    policy.refresh_host_facts()
    assert not policy.host_allowed("192.0.2.87")
    monkeypatch.setattr(policy, "_interface_addresses", lambda: {"192.0.2.87"})
    clock[0] += 30
    assert not policy.host_allowed("192.0.2.87")          # still the cached set
    clock[0] += 31
    policy.host_allowed("x")                             # a stale read refreshes
    policy._wait_host_refresh()
    assert policy.host_allowed("192.0.2.87")


def test_an_enumerator_error_keeps_what_we_had(sec, monkeypatch):
    def boom():
        raise OSError("no interfaces")
    monkeypatch.setattr(policy, "_interface_addresses", boom)
    policy.refresh_host_facts()
    assert policy.host_allowed("198.51.100.37:8765")


def test_the_real_enumerator_runs_without_a_shell(monkeypatch):
    """Standard library only (no subprocess): on this machine it finds at least loopback."""
    import subprocess

    def no_shell(*a, **k):
        raise AssertionError("never shell out")
    monkeypatch.setattr(subprocess, "run", no_shell)
    monkeypatch.setattr(subprocess, "Popen", no_shell)
    addrs = policy._real_interface_addresses()
    assert isinstance(addrs, set)
    assert all(isinstance(a, str) for a in addrs)


def test_public_url_and_callback_base(sec, monkeypatch):
    assert not policy.host_allowed("chat.example.net")
    monkeypatch.setenv("DISPATCH_PUBLIC_URL", "https://Chat.Example.net/")
    monkeypatch.setenv("DISPATCH_CALLBACK_BASE", "http://[2001:db8::5]:8765")
    assert policy.host_allowed("chat.example.net")
    assert policy.host_allowed("chat.example.net:443")
    assert policy.host_allowed("[2001:db8::5]:8765")
    assert not policy.host_allowed("example.net")


def test_trusted_proxy_literals_only(sec, monkeypatch):
    _set_security(trusted_proxies=["198.51.100.7", "203.0.113.0/24", "2001:db8::9/128", "192.0.2.4/32"])
    assert policy.host_allowed("198.51.100.7")
    assert policy.host_allowed("[2001:db8::9]:8765")
    assert policy.host_allowed("192.0.2.4")
    assert not policy.host_allowed("203.0.113.5")       # a range contributes nothing
    assert not policy.host_allowed("203.0.113.0")
    monkeypatch.setenv("DISPATCH_TRUSTED_PROXIES", "198.51.100.99")
    assert policy.host_allowed("198.51.100.99")


def test_extra_list_and_wildcards_and_env(sec, monkeypatch):
    _set_security(allowed_hosts=["chat.home.arpa", "*.Example.com", "192.0.2.77", "[2001:db8::77]"])
    assert policy.host_allowed("chat.home.arpa:8765")
    assert policy.host_allowed("a.example.com") and policy.host_allowed("a.b.example.com:443")
    assert not policy.host_allowed("example.com")       # *. means a name under it
    assert not policy.host_allowed("evilexample.com")
    assert policy.host_allowed("192.0.2.77")
    assert policy.host_allowed("[2001:db8::77]")
    assert not policy.host_allowed("other.home.arpa")
    monkeypatch.setenv("DISPATCH_ALLOWED_HOSTS", "other.home.arpa, *.lan.example")
    assert policy.host_allowed("other.home.arpa")
    assert policy.host_allowed("box.lan.example")


def test_mode_from_file_and_env(sec, monkeypatch):
    assert policy.host_check_mode() == "log"            # the shipped default
    _set_security(host_check="enforce")
    assert policy.host_check_mode() == "enforce"
    monkeypatch.setenv("DISPATCH_HOST_CHECK", "log")
    assert policy.host_check_mode() == "log"             # the environment wins
    monkeypatch.setenv("DISPATCH_HOST_CHECK", "nonsense")
    assert policy.host_check_mode() == "enforce"         # a typo never loosens the file's choice


def test_security_yaml_round_trip(sec):
    _set_security(host_check="enforce", allowed_hosts=["chat.home.arpa", "*.example.com"])
    back = auth.load()
    assert back.host_check == "enforce"
    assert back.allowed_hosts == ["chat.home.arpa", "*.example.com"]
    text = auth.SECURITY_PATH.read_text()
    assert "host_check:" in text and "allowed_hosts:" in text
    # junk in the file reads as the safe default, never a crash
    auth.SECURITY_PATH.write_text(text.replace('host_check: "enforce"', "host_check: maybe"))
    auth._bust_cache()
    assert auth.load().host_check == "log"


def test_unexpected_names_are_counted_and_capped(sec):
    for _ in range(3):
        policy.note_unexpected_host("Evil.Example:8765")
    policy.note_unexpected_host(None)
    rows = {r["name"]: r for r in policy.unexpected_hosts()}
    assert rows["evil.example"]["count"] == 3 and rows["evil.example"]["last_seen"] > 0
    assert rows[""]["count"] == 1
    for i in range(80):
        policy.note_unexpected_host(f"n{i}.example")
    rows = policy.unexpected_hosts()
    assert len(rows) == 50
    assert rows[0]["name"] == "n79.example"             # most recent first
    policy.note_unexpected_host("x" * 5000)
    assert all(len(r["name"]) <= 255 for r in policy.unexpected_hosts())


def test_unexpected_names_log_once_per_hour(sec, monkeypatch, caplog):
    clock = [5000.0]
    monkeypatch.setattr(policy, "_now", lambda: clock[0])
    with caplog.at_level("WARNING", logger="dispatch.policy"):
        policy.note_unexpected_host(EVIL)
        policy.note_unexpected_host(EVIL)
        clock[0] += 3599
        policy.note_unexpected_host(EVIL)
        assert sum(EVIL in r.getMessage() for r in caplog.records) == 1
        clock[0] += 2
        policy.note_unexpected_host(EVIL)
        assert sum(EVIL in r.getMessage() for r in caplog.records) == 2


# --------------------------------------------------------------------------- #
# (b) the gate over HTTP / WS
# --------------------------------------------------------------------------- #

LOCAL = "localhost:8765"


def _owner(world):
    c = world.client(("127.0.0.1", 42000), origin=False)
    c.headers.update({"Host": LOCAL, "Origin": f"http://{LOCAL}"})
    world.create_owner(c)
    return c


def _ws_close_code(client, path, headers):
    with pytest.raises(WebSocketDisconnect) as ei, client.websocket_connect(path, headers=headers) as ws:
        ws.receive_json()
    return ei.value.code


def test_log_mode_lets_a_stranger_through_and_counts_it(world):
    owner = _owner(world)
    stranger = world.client(("198.51.100.99", 42001), origin=False)
    r = stranger.get("/api/auth/status", headers={"Host": f"{EVIL}:8765"})
    assert r.status_code == 200, r.text
    names = {x["name"]: x["count"] for x in policy.unexpected_hosts()}
    assert names.get(EVIL) == 1
    # the Owner sees it in the report
    rep = owner.get("/api/system/host-check").json()
    assert rep["mode"] == "log"
    assert any(x["name"] == EVIL and x["count"] == 1 for x in rep["unexpected"])
    assert "localhost" in rep["answers_to"] and "198.51.100.37" in rep["answers_to"]
    # an HTTP/1.0-style request without a Host proceeds in log mode and is noted as ""
    r = stranger.get("/api/auth/status", headers={"Host": ""})
    assert r.status_code == 200
    assert "" in {x["name"] for x in policy.unexpected_hosts()}


def test_enforce_refuses_a_stranger_api_html_ws(world, monkeypatch):
    owner = _owner(world)
    monkeypatch.setenv("DISPATCH_HOST_CHECK", "enforce")
    c = world.client(("127.0.0.1", 42002), origin=False)
    r = c.get("/api/auth/status", headers={"Host": f"{EVIL}:8765"})
    assert r.status_code == 421, r.text
    body = r.json()
    assert body["error"]["code"] == "host_not_allowed"
    assert EVIL in body["detail"] and "allowed_hosts" in body["detail"]
    r = c.get("/", headers={"Host": EVIL})
    assert r.status_code == 421 and r.headers["content-type"].startswith("text/html")
    assert EVIL in r.text and "allowed_hosts" in r.text
    r = c.get("/", headers={"Host": "<script>x</script>"})
    assert r.status_code == 421 and "<script>x" not in r.text
    r = c.post("/api/auth/login", headers={"Host": EVIL, "Origin": f"http://{EVIL}"}, json={"pin": "2468"})
    assert r.status_code == 421
    assert _ws_close_code(c, "/ws", {"Host": EVIL, "Origin": f"http://{EVIL}"}) == 1008
    assert _ws_close_code(c, "/ws/voice/x", {"Host": EVIL}) == 1008
    # an empty Host is a stranger too
    assert c.get("/api/auth/status", headers={"Host": ""}).status_code == 421
    # the Owner on localhost is untouched
    assert owner.get("/api/auth/status").status_code == 200
    assert owner.get("/api/system/host-check").json()["mode"] == "enforce"


@pytest.mark.parametrize("host", ["127.0.0.1:8765", "[::1]:8765", "localhost", "198.51.100.37:8765",
                                  "203.0.113.44:8765", "chatbox.example.ts.net", "[2001:db8:1::1]:8765"])  # scrub-ok: Tailscale's own domain suffix
def test_enforce_admits_this_machines_names(world, monkeypatch, host):
    _owner(world)
    monkeypatch.setenv("DISPATCH_HOST_CHECK", "enforce")
    c = world.client(("198.51.100.20", 42003), origin=False)
    assert c.get("/api/auth/status", headers={"Host": host}).status_code == 200, host
    assert policy.unexpected_hosts() == []


def test_enforce_from_security_yaml(world):
    _owner(world)
    _set_security(host_check="enforce")
    c = world.client(("127.0.0.1", 42004), origin=False)
    assert c.get("/api/auth/status", headers={"Host": EVIL}).status_code == 421
    _set_security(host_check="log")
    assert c.get("/api/auth/status", headers={"Host": EVIL}).status_code == 200


def test_machine_key_on_127_is_unaffected(world, monkeypatch):
    owner = _owner(world)
    token = owner.post("/api/auth/api-token", json={"rotate": True}).json()["api_token"]
    monkeypatch.setenv("DISPATCH_HOST_CHECK", "enforce")
    local = world.client(("127.0.0.1", 42005), origin=False)
    local.headers.update({"Host": "127.0.0.1:8765"})
    body = {"bot_id": "alpha", "role": "user", "content": "hi"}
    assert local.post("/api/inject", headers={"X-API-Key": token}, json=body).status_code == 200
    assert local.get("/api/health").status_code == 200
    assert policy.unexpected_hosts() == []


def test_the_unclaimed_gate_is_unchanged_in_log_mode(world, monkeypatch):
    """#2: a foreign Host from loopback on an unclaimed install still gets setup_required, a
    loopback Host still may set up. In enforce mode the stranger is refused earlier (421)."""
    monkeypatch.delenv("DISPATCH_ALLOW_OPEN_INSTALL", raising=False)
    c = world.client(("127.0.0.1", 42006), origin=False)
    r = c.get("/api/settings", headers={"Host": EVIL})
    assert r.status_code == 403 and r.json()["error"]["code"] == "setup_required", r.text
    assert _ws_close_code(c, "/ws", {"Host": EVIL}) == 1008
    monkeypatch.setenv("DISPATCH_HOST_CHECK", "enforce")
    assert c.get("/api/settings", headers={"Host": EVIL}).status_code == 421
    monkeypatch.delenv("DISPATCH_HOST_CHECK")
    r = c.post("/api/auth/setup", headers={"Host": LOCAL, "Origin": f"http://{LOCAL}"},
               json={"name": "Olive", "password": "owner-pass1"})
    assert r.status_code == 200, r.text
    assert policy.claimed()


def test_the_report_route_is_the_owners(world):
    from agents_world import sibling
    owner = _owner(world)
    world.person(owner, "Ann", "admin", password="second-admin-pw1")  # scrub-ok: test fixture
    ann_id = next(p["id"] for p in owner.get("/api/people").json()["profiles"] if p["name"] == "Ann")
    ann = sibling(world, owner, ("127.0.0.1", 42007), origin=False)
    ann.headers.update({"Host": LOCAL, "Origin": f"http://{LOCAL}"})
    assert world.login(ann, profile_id=ann_id, password="second-admin-pw1").status_code == 200  # scrub-ok: throwaway test fixture
    r = ann.get("/api/system/host-check")
    assert r.status_code == 403 and r.json()["error"]["code"] == "host_owner_only"


# --------------------------------------------------------------------------- #
# (c) settings
# --------------------------------------------------------------------------- #


def test_settings_rows_are_host_tier():
    for key in ("people.host_check", "people.allowed_hosts"):
        d = settings_schema.get_def(key)
        assert d is not None and d.owner_only, key
        assert key in settings_api.HOST_SETTING_KEYS
        assert d.binding == "file:security"
    assert settings_schema.get_def("people.host_check").choices == ["log", "enforce"]
    assert settings_schema.get_def("people.host_check").default == "log"


def test_settings_write_and_validate(world):
    owner = _owner(world)
    r = owner.patch("/api/settings", json={"set": {"people.allowed_hosts": "chat.home.arpa, *.example.com"}})
    assert r.status_code == 200, r.text
    assert auth.load().allowed_hosts == ["chat.home.arpa", "*.example.com"]
    r = owner.patch("/api/settings", json={"set": {"people.allowed_hosts": "*"}})
    assert r.status_code in (400, 422), r.text
    r = owner.patch("/api/settings", json={"set": {"people.allowed_hosts": "has space.example"}})
    assert r.status_code in (400, 422), r.text
    assert auth.load().allowed_hosts == ["chat.home.arpa", "*.example.com"]
    r = owner.patch("/api/settings", json={"set": {"people.host_check": "enforce"}})
    assert r.status_code == 200, r.text
    assert auth.load().host_check == "enforce"
    from app import settings_store
    assert settings_store.get("people.host_check") == "enforce"
    assert settings_store.get("people.allowed_hosts") == ["chat.home.arpa", "*.example.com"]
    r = owner.patch("/api/settings", json={"set": {"people.host_check": "off"}})
    assert r.status_code in (400, 422)


def test_error_code_is_catalogued():
    from app.contracts import errors
    spec = errors.get("host_not_allowed")
    assert spec is not None and spec.status == 421
