"""Privacy matrix, machine callers and push routing.

A machine (api key) WRITES into any existing thread but READS only the protected Admin's. A
machine holding only ``machine:inject`` reaches nothing else. Push pings go only to the devices
of the profile that owns the thread."""
from __future__ import annotations

import json

import pytest
from privacy_fixtures import PRIVACY_MARKS, SENTINEL_RE, build_install

from app import main, push

pytestmark = PRIVACY_MARKS


def test_default_scope_machine_reads_only_the_protected_admin(world, monkeypatch):
    inst = build_install(world, monkeypatch, "M")
    m = inst.client("M")
    a0 = inst.get("thread", "A0")
    r = m.get(f"/api/threads/{a0.ids['thread']}")
    assert r.status_code == 200 and a0.sentinel in r.text, f"machine cannot read the Admin's thread: {r.status_code}"
    r = m.get(f"/api/threads/{a0.ids['thread']}/messages")
    assert r.status_code == 200 and a0.sentinel in r.text
    for owner in ("U1", "B1", "D"):
        th = inst.get("thread", owner)
        for path in (f"/api/threads/{th.ids['thread']}", f"/api/threads/{th.ids['thread']}/messages"):
            r = m.get(path)
            assert r.status_code in (403, 404) and th.sentinel not in r.text, f"machine read {owner}: {path} {r.status_code}"
    # listings show the Admin's threads only
    r = m.get("/api/threads", params={"bot_id": "h_direct"})
    assert r.status_code == 200
    found = {x[1] for x in SENTINEL_RE.findall(r.text)}
    assert found <= {"A0"}, f"machine thread list shows {found}"
    r = m.get("/api/files")
    found = {x[1] for x in SENTINEL_RE.findall(r.text)}
    assert found <= {"A0"}, f"machine file list shows {found}"


@pytest.mark.parametrize("actor", ("M", "Mi"))
def test_inject_into_any_thread_without_echo(world, monkeypatch, actor):
    inst = build_install(world, monkeypatch, actor)
    c = inst.client(actor)
    for owner in ("A0", "U1", "B1", "D"):
        th = inst.get("thread", owner)
        r = c.post("/api/inject", json={"thread_id": th.ids["thread"], "content": f"hello-{owner}"})
        assert r.status_code == 200, f"{actor}: inject into {owner}'s thread: {r.status_code} {r.text[:120]}"
        assert th.sentinel not in r.text, f"{actor}: the inject reply echoed {owner}'s thread content"
        rows = inst.run(lambda th=th: world.db.list_messages(th.ids["thread"], 50))[0]
        assert any(m.content == f"hello-{owner}" for m in rows)
    assert c.post("/api/inject", json={"thread_id": "no-such-thread", "content": "x"}).status_code == 404


def test_inject_only_machine_reaches_nothing_else(world, monkeypatch):
    inst = build_install(world, monkeypatch, "Mi")
    a0 = inst.get("thread", "A0")
    for path in (f"/api/threads/{a0.ids['thread']}", "/api/files", "/api/bots", "/api/threads?bot_id=h_direct",
                 "/api/advisor/status"):
        r = inst.client("Mi").get(path)
        assert r.status_code in (401, 403, 404), f"{path}: {r.status_code}"
        assert a0.sentinel not in r.text


def test_inject_delivery_key_is_idempotent(world, monkeypatch):
    inst = build_install(world, monkeypatch, "M")
    th = inst.get("thread", "U1")
    body = {"thread_id": th.ids["thread"], "content": "once only", "delivery_key": "run-123"}
    first = inst.client("M").post("/api/inject", json=body)
    second = inst.client("M").post("/api/inject", json=body)
    assert first.status_code == second.status_code == 200
    assert second.json().get("duplicate") is True, f"second delivery was not flagged: {second.text[:200]}"
    rows = inst.run(lambda: world.db.list_messages(th.ids["thread"], 50))[0]
    assert sum(1 for m in rows if m.content == "once only") == 1


# ---- push -------------------------------------------------------------------------------------- #

@pytest.fixture
def pushes(monkeypatch):
    out: list[dict] = []

    def fake_send(sub, data, *, topic):
        out.append({"endpoint": sub["endpoint"], "data": json.loads(data)})
    monkeypatch.setattr(push, "_send", fake_send)
    monkeypatch.setattr(push, "_alive", lambda key: True)
    monkeypatch.setattr(push, "DEBOUNCE_S", 0.0)
    monkeypatch.setattr(main, "_shutting_down", False)
    push._last_sent.clear()
    return out


def _subscribe_all(inst):
    eps = {}
    for key in ("A0", "A2", "U1", "U2", "B1", "D"):
        ep = f"https://fcm.googleapis.com/fcm/send/{key}-{inst.tag}"
        sub = {"endpoint": ep, "keys": {"p256dh": "B" + "A" * 86, "auth": "c" * 22}}
        push.subscribe(f"dev-{key}", sub, profile_id=inst.pids[key])
        eps[key] = ep
    return eps


@pytest.mark.parametrize("owner", ("A0", "U1", "B1", "D"))
def test_notify_reaches_only_the_owners_devices(world, monkeypatch, pushes, owner):
    inst = build_install(world, monkeypatch, "A0")
    eps = _subscribe_all(inst)
    th = inst.get("thread", owner)
    try:
        n = inst.run(lambda: push.notify("reply", th.ids["thread"], th.sentinel, owner=inst.pids[owner]))
    except TypeError as e:
        pytest.fail(f"push.notify has no owner= routing yet: {e}")
    got = {p["endpoint"] for p in pushes}
    assert got == {eps[owner]}, f"ping for {owner}'s thread went to {sorted(got)} (n={n})"


def test_machine_inject_with_notify_pings_only_the_threads_owner(world, monkeypatch, pushes):
    inst = build_install(world, monkeypatch, "M")
    eps = _subscribe_all(inst)
    th = inst.get("thread", "U1")
    r = inst.client("M").post("/api/inject", json={"thread_id": th.ids["thread"], "content": "ping me",
                                                   "notify": True})
    assert r.status_code == 200, r.text
    import time
    time.sleep(0.5)
    got = {p["endpoint"] for p in pushes}
    assert got <= {eps["U1"]}, f"inject notify reached {sorted(got - {eps['U1']})}"
