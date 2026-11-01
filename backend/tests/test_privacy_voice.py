"""Privacy matrix, voice clones through a fake add-on (BUILD-PLAN-v2 section 1.12).

The fake add-on (tests/privacy_fake_addon.py) enforces the ownership table with ONLY what the host
hands it, so this judges the host half: the Caller it supplies, the coarse route gate, the thread
socket owner check (1008), ``voice_allowed``, ``count`` and ``purge``."""
from __future__ import annotations

import contextlib

import privacy_fake_addon as fake
import pytest
from privacy_fixtures import (
    HOUSEHOLD_BOT,
    PRIVACY_MARKS,
    PROFILE_ACTORS,
    SENTINEL_RE,
    actor_permissions,
    build_install,
)
from starlette.websockets import WebSocketDisconnect

from app import ext_profile_hooks, main

pytestmark = PRIVACY_MARKS

CLONE_OWNERS = ("A0", "U1", "U2")


@pytest.fixture
def addon(world):
    fake.install(main.app)
    yield fake.STATE
    fake.uninstall(main.app)


def _clones(inst):
    out = {}
    for owner in CLONE_OWNERS:
        s = f"SENTINEL-voice_clone-{owner}-{inst.tag}"
        fake.add_clone(f"clone-{owner}", f"{s}-name", inst.pids[owner], f"RIFF{s}-ref".encode())
        out[owner] = s
    fake.add_clone("clone-house", "Household Voice", "household", b"RIFFhousehold-ref")
    return out


@pytest.mark.parametrize("actor", PROFILE_ACTORS)
def test_picker_lists_household_and_own_clones_only(world, monkeypatch, addon, actor):
    inst = build_install(world, monkeypatch, actor)
    sent = _clones(inst)
    r = inst.client(actor).get("/api/voice/profiles")
    assert r.status_code == 200, f"{actor}: GET /api/voice/profiles -> {r.status_code}"
    shown = {p["id"] for p in r.json()["profiles"]}
    assert "clone-house" in shown
    for owner in CLONE_OWNERS:
        if owner == actor:
            assert f"clone-{owner}" in shown, f"{actor} cannot see its own clone"
        else:
            assert f"clone-{owner}" not in shown, f"{actor} is offered {owner}'s clone"
            assert sent[owner] not in r.text


@pytest.mark.parametrize("actor", PROFILE_ACTORS)
def test_foreign_clone_routes_are_404_and_leak_nothing(world, monkeypatch, addon, actor):
    inst = build_install(world, monkeypatch, actor)
    sent = _clones(inst)
    c = inst.client(actor)
    for owner in CLONE_OWNERS:
        if owner == actor:
            continue
        cid = f"clone-{owner}"
        for method, path, kw in (("GET", f"/api/voice/profiles/{cid}", {}),
                                 ("GET", f"/api/voice/profiles/{cid}/files/reference.wav", {}),
                                 ("PATCH", f"/api/voice/profiles/{cid}", {"json": {"name": "stolen"}}),
                                 ("DELETE", f"/api/voice/profiles/{cid}", {})):
            r = c.request(method, path, **kw)
            assert r.status_code in (403, 404), f"{actor} {method} {owner}'s clone: {r.status_code}"
            assert sent[owner] not in r.text
        assert cid in fake.STATE.clones and fake.STATE.clones[cid]["name"].endswith("-name")


def test_the_hosts_caller_is_right_for_every_principal(world, monkeypatch, addon):
    inst = build_install(world, monkeypatch, "A0")
    for actor in PROFILE_ACTORS:
        r = inst.client(actor).get("/api/voice/status")
        assert r.status_code == 200, f"{actor}: /api/voice/status -> {r.status_code}"
        who = r.json()["caller"]
        assert who is not None and who["profile_id"] == inst.pids[actor], f"{actor}: wrong caller {who}"
        assert who["role"] == {"A0": "admin", "A2": "admin", "B1": "basic"}.get(actor, "user")
        assert who["voice_lab"] == ("voice_lab" in actor_permissions(actor)), f"{actor}: voice_lab mismatch"
        personal = {r2.ids["bot"] for (k, o), r2 in inst.res.items() if k == "personal_bot" and o != actor}
        assert not (set(r.json()["bots"]) & personal), f"{actor}: bots_visible lists another profile's bot"
    own = inst.get("personal_bot", "U1").ids["bot"]
    assert own in inst.client("U1").get("/api/voice/status").json()["bots"]
    assert "voice_lab" not in set(actor_permissions("D")), "default profile must not hold voice_lab"


def test_unsigned_on_a_profile_list_install_has_no_caller_and_no_writes(world, monkeypatch, addon):
    inst = build_install(world, monkeypatch, "N")
    _clones(inst)
    c = inst.client("N")
    for method, path, kw in (("GET", "/api/voice/profiles", {}), ("GET", "/api/voice/status", {}),
                             ("POST", "/api/voice/profiles", {"json": {"name": "x"}}),
                             ("DELETE", "/api/voice/profiles/clone-house", {})):
        r = c.request(method, path, **kw)
        assert r.status_code in (401, 403), f"{method} {path}: {r.status_code}"
    assert "clone-house" in fake.STATE.clones


@pytest.mark.parametrize("actor,expect_ok", [("U1", True), ("A0", True), ("B1", False), ("D", False)])
def test_creating_a_clone_needs_voice_lab_and_the_default_profile_never_has_it(world, monkeypatch, addon,
                                                                               actor, expect_ok):
    inst = build_install(world, monkeypatch, actor)
    r = inst.client(actor).post("/api/voice/profiles", json={"name": "mine"})
    if expect_ok:
        assert r.status_code == 200, f"{actor}: {r.status_code} {r.text[:100]}"
        assert r.json()["owner"] == inst.pids[actor]
    else:
        assert r.status_code in (401, 403), f"{actor} created a clone ({r.status_code})"
        assert not fake.STATE.clones


def test_voice_allowed_follows_the_owner_rule(world, monkeypatch, addon):
    inst = build_install(world, monkeypatch, "A0")
    _clones(inst)
    p = inst.pids
    assert ext_profile_hooks.voice_owned_clones_active() is True
    assert ext_profile_hooks.voice_allowed("clone-U1", p["U1"]) is True
    assert ext_profile_hooks.voice_allowed("clone-U1", p["U2"]) is False
    assert ext_profile_hooks.voice_allowed("clone-U1", p["A0"]) is False, "an Admin's bot may not wear a User's clone"
    assert ext_profile_hooks.voice_allowed("clone-U1", None) is False, "a household bot may not wear a personal clone"
    assert ext_profile_hooks.voice_allowed("clone-house", p["U2"]) is True
    assert ext_profile_hooks.voice_allowed("builtin:af_heart", None) is True


def test_count_and_purge_touch_only_that_profile(world, monkeypatch, addon):
    inst = build_install(world, monkeypatch, "A0")
    _clones(inst)
    assert ext_profile_hooks.count_profile_data(inst.pids["U1"]).get("voices") == 1
    assert ext_profile_hooks.purge_profile_data(inst.pids["U1"]) == 1
    assert "clone-U1" not in fake.STATE.clones
    assert {"clone-U2", "clone-A0", "clone-house"} <= set(fake.STATE.clones)


def _connect(inst, actor, tid):
    return inst.client(actor).websocket_connect(f"/ws/voice/{tid}")


@pytest.mark.parametrize("actor", ("A0", "A2", "U2", "B1", "D", "N"))
def test_voice_socket_refuses_a_non_owner_with_1008(world, monkeypatch, addon, actor):
    inst = build_install(world, monkeypatch, actor)
    tid = inst.get("thread", "U1").ids["thread"]
    try:
        with _connect(inst, actor, tid) as ws:
            first = ws.receive_text()          # the fake handler sends "ready" at once if it is let in
        pytest.fail(f"{actor} was let into U1's voice socket: {first[:80]}")
    except WebSocketDisconnect as e:
        assert e.code == 1008, f"{actor}: closed with {e.code}"


def test_voice_socket_accepts_the_owner_and_reports_its_profile(world, monkeypatch, addon):
    import json
    inst = build_install(world, monkeypatch, "U1")
    tid = inst.get("thread", "U1").ids["thread"]
    with _connect(inst, "U1", tid) as ws:
        ready = json.loads(ws.receive_text())
    assert ready["profile_id"] == inst.pids["U1"]
