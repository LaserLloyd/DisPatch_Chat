"""The first-run wizard (3.0, W5), driven entirely through the API, as a script or a test would.

Six steps: owner account (POST /api/auth/setup), first connection (detect + Test), voice (a link),
family (profiles + Animal Friends), network exposure, done.
"""
from __future__ import annotations

import httpx
import pytest

from app import config, connections, llm_api, settings_store


def _mock_http(monkeypatch, handler):
    def factory(read_timeout: float = llm_api.READ_TIMEOUT_S) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=read_timeout,
                                 follow_redirects=False)
    monkeypatch.setattr(llm_api, "http_client", factory)


def _lm_studio_on_loopback(monkeypatch):
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host != "127.0.0.1":
            raise httpx.ConnectError("no route")
        if req.url.port == 1234 and req.url.path == "/api/v0/models":
            return httpx.Response(200, json={"data": [{"id": "qwen2.5-7b"}]})
        if req.url.port == 1234 and req.url.path in ("/v1/models",):
            return httpx.Response(200, json={"data": [{"id": "qwen2.5-7b"}]})
        return httpx.Response(404)
    _mock_http(monkeypatch, handler)


def test_a_fresh_install_needs_the_wizard_and_says_only_that_to_a_stranger(world, monkeypatch):
    monkeypatch.delenv("DISPATCH_ALLOW_OPEN_INSTALL")          # the secure default: a stranger is not the owner
    stranger = world.client(("203.0.113.90", 44001))
    st = stranger.get("/api/wizard").json()
    assert st == {"needed": True, "claimed": False}


def test_the_whole_wizard_by_api(world, monkeypatch):
    _lm_studio_on_loopback(monkeypatch)
    config.upsert_bot(config.Bot(id="helper", name="Helper", advisor={"providers": [], "tools": True}))
    c = world.client()
    # 1. owner (W4's setup flow, allowed from this computer)
    assert c.post("/api/auth/setup", json={"name": "Olive", "password": "owner-pass1", "new_pin": "2468"}).status_code == 200
    st = c.get("/api/wizard").json()
    assert st["needed"] is True and st["claimed"] is True and st["next"] == "connection"
    by = {s["id"]: s for s in st["steps"]}
    assert by["owner"]["done"] and not by["connection"]["done"] and by["voice"]["optional"] and by["family"]["optional"]
    # 2. detect, then add and Test the first connection; the default character is pointed at it
    found = c.post("/api/wizard/detect", json={}).json()["found"]
    assert [f["type"] for f in found] == ["lmstudio"] and found[0]["models"] == ["qwen2.5-7b"]
    r = c.post("/api/wizard/connection", json={"type": "lmstudio", "base_url": found[0]["base_url"], "model": "qwen2.5-7b"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["test"]["ok"] is True and body["connection"]["models"] == ["qwen2.5-7b"]
    assert body["assigned"] == {"character": "helper", "model": "qwen2.5-7b"}
    assert settings_store.get("connections.default") == body["connection"]["id"]
    bot = config.get_bot(body["assigned"]["character"])
    assert bot.advisor["providers"][0]["connection"] == body["connection"]["id"]
    st = c.get("/api/wizard").json()
    assert {s["id"]: s for s in st["steps"]}["connection"]["done"] is True
    # 3. voice is optional: the state says whether the add-on is there, skipping is allowed
    assert c.post("/api/wizard/step/voice", json={"status": "skipped"}).json() == {"step": "voice", "status": "skipped"}
    assert c.post("/api/wizard/step/connection", json={"status": "skipped"}).status_code == 409
    assert c.post("/api/wizard/step/nope", json={}).status_code == 404
    # 4. family: a member, a child, and the Animal Friends pack
    fam = c.post("/api/wizard/family", json={
        "people": [{"name": "Max", "role": "member", "pin": "5555"}, {"name": "Mia", "role": "restricted"}],
        "install_animal_friends": True})
    assert fam.status_code == 200, fam.text
    fb = fam.json()
    assert [p["role"] for p in fb["created"]] == ["user", "basic"] and len(fb["restricted"]) == 1
    assert fb["packs"]["animal_friends"]["installed"] == ["otto", "hazel", "hoot", "pip", "maple", "bruno"]
    otto = config.get_bot("otto")
    assert otto.audience == "restricted" and otto.advisor["providers"][0]["connection"] == body["connection"]["id"]
    # 5. network: local-only by default
    net = c.post("/api/wizard/network", json={"exposure": "local"}).json()
    assert net["exposure"] == "local" and not net["mismatch"] and settings_store.get("system.network_exposure") == "local"
    # 6. done
    fin = c.post("/api/wizard/finish").json()
    assert fin["finished"] is True
    assert {s["id"] for s in fin["next_steps"]} <= {"backup", "recovery"}
    after = world.client(("203.0.113.91", 44002)).get("/api/wizard").json()
    assert after == {"needed": False, "claimed": True}


def test_lan_exposure_needs_an_owner_and_carries_the_tls_note(world):
    c = world.client()
    r = c.post("/api/wizard/network", json={"exposure": "lan"})
    assert r.status_code in (401, 403, 409)                    # not signed in / no owner yet: refused
    world.create_owner(c)
    net = c.post("/api/wizard/network", json={"exposure": "lan", "public_url": "https://chat.example.net",
                                              "behind_tls": True}).json()
    assert net["exposure"] == "lan"
    text = " ".join(net["notes"])
    assert "0.0.0.0" in text and "HTTPS" in text and "remote-access" in text
    assert net["mismatch"] is True and "cannot reach" in net["mismatch_note"]       # still listening on loopback
    assert settings_store.get("system.public_url") == "https://chat.example.net"
    assert settings_store.get("system.behind_tls") is True
    bad = c.post("/api/wizard/network", json={"exposure": "lan", "public_url": "ftp://nope"})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "settings_invalid"
    assert c.post("/api/wizard/network", json={"exposure": "world"}).status_code == 422


def test_a_failing_test_is_reported_and_nothing_is_assigned(world, monkeypatch):
    def refuse(req):
        raise httpx.ConnectError("refused")
    _mock_http(monkeypatch, refuse)
    c = world.create_owner()
    r = c.post("/api/wizard/connection", json={"type": "ollama"}).json()
    assert r["test"]["ok"] is False and r["test"]["problem"] == "unreachable" and r["assigned"] is None
    assert settings_store.get("connections.default") == ""
    assert connections.get(r["connection"]["id"]) is not None            # kept so it can be fixed and re-tested


def test_an_upgraded_install_that_already_works_never_sees_the_wizard(world):
    c = world.create_owner()
    config.upsert_bot(config.Bot(id="old", name="Old", advisor={
        "providers": [{"provider": "openai", "model": "gpt-4o", "api_key": "sk-old-0123456789"}]}))
    assert c.get("/api/wizard").json()["needed"] is False


def test_the_wizard_routes_follow_the_permissions(world):
    owner = world.create_owner()
    world.person(owner, "Max", "member", pin="5555")
    members = world.client(("203.0.113.92", 44003))
    people = {p["name"]: p for p in owner.get("/api/people").json()["profiles"]}
    world.login(members, profile_id=people["Max"]["id"], pin="5555")
    for c in (members, world.client(("203.0.113.93", 44004))):
        assert c.post("/api/wizard/detect", json={}).status_code == 403
        assert c.post("/api/wizard/connection", json={"type": "ollama"}).status_code == 403
        assert c.post("/api/wizard/family", json={"people": []}).status_code == 403
        assert c.post("/api/wizard/network", json={"exposure": "local"}).status_code == 403
        assert c.post("/api/wizard/finish").status_code == 403
        assert c.get("/api/wizard").json().keys() == {"needed", "claimed"}      # no checklist for them
    assert "steps" in owner.get("/api/wizard").json()


def test_the_wizard_state_is_stored_under_its_own_namespace(world):
    c = world.create_owner()
    c.post("/api/wizard/step/voice", json={"status": "skipped"})
    st = settings_store.get("wizard.state")
    assert st["skipped"] == ["voice"]
    assert "wizard.state" in settings_store.extra_values()


def test_family_input_is_validated(world):
    c = world.create_owner()
    assert c.post("/api/wizard/family", json={"people": [{"name": "Max", "role": "member", "pin": "1"}]}).status_code == 400
    assert c.post("/api/wizard/family", json={"people": [{"name": "Max", "role": "admin"}]}).status_code == 422
    c.post("/api/wizard/family", json={"people": [{"name": "Mia", "role": "restricted"}]})
    dup = c.post("/api/wizard/family", json={"people": [{"name": "mia", "role": "restricted"}]})
    assert dup.status_code == 409


def test_in_docker_the_published_address_decides_so_local_is_not_a_mismatch(world, monkeypatch):
    """The image binds 0.0.0.0 inside the container by design; `-p 127.0.0.1:8765:8765` /
    BIND_ADDR is what "this computer only" means there, and the app cannot see it."""
    from dataclasses import replace
    monkeypatch.setattr(config, "SETTINGS", replace(config.SETTINGS, host="0.0.0.0"))
    c = world.create_owner()
    net = c.post("/api/wizard/network", json={"exposure": "local"}).json()
    assert net["mismatch"] is True and "wider" in net["mismatch_note"]        # bare metal: a real mismatch
    monkeypatch.setenv("DISPATCH_IN_DOCKER", "1")
    net = c.post("/api/wizard/network", json={"exposure": "local"}).json()
    assert net["mismatch"] is False and net["mismatch_note"] == ""
    assert any("BIND_ADDR" in n for n in net["notes"])
    net = c.post("/api/wizard/network", json={"exposure": "lan"}).json()
    assert net["mismatch"] is False


@pytest.mark.parametrize("path", ["/api/wizard/detect", "/api/wizard/finish"])
def test_posting_junk_never_crashes(world, path):
    c = world.create_owner()
    r = c.post(path, content=b"not json", headers={"content-type": "application/json"})
    assert r.status_code in (200, 409, 422)


# connections.default may hold the id of a connection that has since been deleted (it
# reads back as itself since 3.2, so Settings can show it as missing). Every reader
# treats such a dangling id as "no default".


def _dangling_default():
    gone = connections.create(type="openai_compatible", name="Gone", base_url="http://127.0.0.1:9/v1")
    assert settings_store.set_many({"connections.default": gone.id}).ok
    connections.delete(gone.id)
    assert settings_store.get("connections.default") == gone.id
    return gone.id


def test_a_wizard_connection_becomes_the_default_when_the_stored_one_is_gone(world, monkeypatch):
    _lm_studio_on_loopback(monkeypatch)
    c = world.create_owner()
    gone = _dangling_default()
    found = c.post("/api/wizard/detect", json={}).json()["found"]
    r = c.post("/api/wizard/connection", json={"type": "lmstudio", "base_url": found[0]["base_url"], "model": "qwen2.5-7b"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["test"]["ok"] is True
    assert settings_store.get("connections.default") == body["connection"]["id"] != gone
    assert c.get("/api/connections").json()["default"] == body["connection"]["id"]


def test_a_starter_pack_ignores_a_dangling_default_connection(world):
    c = world.create_owner()
    _dangling_default()
    assert c.get("/api/connections").json()["default"] == ""
    fam = c.post("/api/wizard/family", json={"people": [], "install_animal_friends": True})
    assert fam.status_code == 200, fam.text
    assert fam.json()["packs"]["animal_friends"]["installed"]
    otto = config.get_bot("otto")
    assert not any((p or {}).get("connection") for p in (otto.advisor or {}).get("providers", []))
