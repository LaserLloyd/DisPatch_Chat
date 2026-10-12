"""Web Push (app/push.py + the /api/push routes): pings, never content.

What matters, in order:
  1. Safe Mode, machines and plain idle-expiring sessions cannot subscribe;
  2. a subscription belongs to a REMEMBERED device and dies with its trust;
  3. what leaves the box is {kind, thread_id, title} and nothing else;
  4. a 404/410 from the push service deletes the subscription.

`push._send` (the one function that talks to a push service) is replaced in
every test; nothing touches the network. Run: cd backend && uv run pytest
tests/test_push.py
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import stat
import types

import pytest

from app import auth, config, main, push
from tests.test_remember_device import PIN, _enable, _unlock, env  # noqa: F401

FCM = "https://fcm.googleapis.com/fcm/send/abc123"
SUB = {"endpoint": FCM,
       "keys": {"p256dh": "B" + "A" * 86, "auth": "c" * 22}}


@pytest.fixture
def sent(monkeypatch):
    """Capture every push instead of sending it."""
    out: list[dict] = []

    def fake_send(sub, data, *, topic):
        out.append({"endpoint": sub["endpoint"], "data": json.loads(data), "topic": topic})

    monkeypatch.setattr(push, "_send", fake_send)
    monkeypatch.setattr(push, "DEBOUNCE_S", 0.0)
    # A TestClient that exited earlier in the session leaves the module-level
    # shutdown flag set (it is process-wide and never reset in production).
    monkeypatch.setattr(main, "_shutting_down", False)
    push._last_sent.clear()
    main._pushed_replies.clear()
    return out


def _remembered(env):
    client = env()
    auth.set_pin(PIN)
    _enable(30)
    assert _unlock(client, remember=True).status_code == 200
    return client


def _subs() -> dict:
    return push._load_subs()


def _mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


# --------------------------------------------------------------------------- #
# The tier gate
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("method,path", [
    ("get", "/api/push/vapid"),
    ("post", "/api/push/subscribe"),
    ("delete", "/api/push/subscribe"),
])
def test_safe_mode_is_refused(env, method, path):
    client = env()
    auth.set_pin(PIN)
    kw = {"json": SUB} if method == "post" else {}
    r = getattr(client, method)(path, **kw)
    assert r.status_code == 403
    assert not (config.DATA_DIR / push.SUBS_FILE).exists()
    assert not (config.DATA_DIR / push.VAPID_FILE).exists(), "no key minted for a locked caller"


def test_a_session_that_is_not_remembered_is_refused(env):
    client = env()
    auth.set_pin(PIN)
    _enable(30)
    assert _unlock(client, remember=False).status_code == 200
    r = client.post("/api/push/subscribe", json=SUB)
    assert r.status_code == 403 and "Remember this device" in r.json()["detail"]
    assert _subs() == {}


def test_a_loopback_machine_caller_is_refused(env):
    """The inbound machine bypass must not reach push: no session, no device."""
    from fastapi.testclient import TestClient
    auth.set_pin(PIN)
    with TestClient(main.app, client=("127.0.0.1", 40123)) as c:
        assert c.post("/api/push/subscribe", json=SUB).status_code == 403


def test_cross_origin_is_refused(env):
    client = _remembered(env)
    r = client.post("/api/push/subscribe", json=SUB,
                    headers={"origin": "https://evil.example.net"})
    assert r.status_code == 403 and _subs() == {}


# --------------------------------------------------------------------------- #
# Keys and subscriptions
# --------------------------------------------------------------------------- #


def test_vapid_key_is_generated_once_owner_only_and_only_the_public_half_served(env):
    client = _remembered(env)
    r = client.get("/api/push/vapid")
    assert r.status_code == 200
    body = r.json()
    raw = base64.urlsafe_b64decode(body["public_key"] + "==")
    assert len(raw) == 65 and raw[0] == 4, "uncompressed P-256 point"
    assert body["subscribed"] is False
    assert "PRIVATE" not in r.text
    path = config.DATA_DIR / push.VAPID_FILE
    assert _mode(path) == 0o600
    assert client.get("/api/push/vapid").json()["public_key"] == body["public_key"]


def test_subscribe_binds_to_the_remembered_device(env):
    client = _remembered(env)
    assert client.post("/api/push/subscribe", json=SUB).status_code == 200
    subs = _subs()
    token = client.cookies.get(main.COOKIE_NAME)
    assert list(subs) == [auth.device_hash(token)]
    assert subs[auth.device_hash(token)]["endpoint"] == FCM
    assert _mode(config.DATA_DIR / push.SUBS_FILE) == 0o600
    assert client.get("/api/push/vapid").json()["subscribed"] is True
    assert client.delete("/api/push/subscribe").json()["removed"] is True
    assert _subs() == {}


@pytest.mark.parametrize("endpoint", [
    "http://fcm.googleapis.com/fcm/send/x",       # not https
    "https://127.0.0.1/push",                      # loopback: request forgery
    "https://192.0.2.10:8700/push",                # a bare IP
    "https://evil.example.net/push",               # not a push service
    "https://fcm.googleapis.com.evil.net/x",       # suffix trick
])
def test_endpoints_outside_the_push_services_are_refused(env, endpoint):
    client = _remembered(env)
    r = client.post("/api/push/subscribe", json={**SUB, "endpoint": endpoint})
    assert r.status_code == 422 and _subs() == {}


def test_malformed_keys_are_refused(env):
    client = _remembered(env)
    r = client.post("/api/push/subscribe",
                    json={"endpoint": FCM, "keys": {"p256dh": "x", "auth": "y"}})
    assert r.status_code == 422


# --------------------------------------------------------------------------- #
# Sending
# --------------------------------------------------------------------------- #


def test_payload_is_kind_thread_and_title_only(env, sent):
    client = _remembered(env)
    client.post("/api/push/subscribe", json=SUB)
    n = asyncio.run(push.notify("reply", "t-1", "Bits   replied"))
    assert n == 1
    assert sent[0]["data"] == {"kind": "reply", "thread_id": "t-1", "title": "Bits replied"}
    assert len(sent[0]["topic"]) <= 32


def test_a_revoked_device_gets_nothing_and_loses_its_subscription(env, sent):
    client = _remembered(env)
    client.post("/api/push/subscribe", json=SUB)
    assert client.post("/api/auth/lock").status_code == 200     # forgets the device
    assert asyncio.run(push.notify("reply", "t-1", "Bits replied")) == 0
    assert sent == [] and _subs() == {}


def test_a_pin_change_revokes_every_subscription(env, sent):
    client = _remembered(env)
    client.post("/api/push/subscribe", json=SUB)
    auth.set_pin("9876")
    assert asyncio.run(push.notify("image", "t-1", "Image ready")) == 0
    assert sent == [] and _subs() == {}


@pytest.mark.parametrize("status,kept", [(404, False), (410, False), (500, True), (429, True)])
def test_push_service_answers(env, monkeypatch, status, kept):
    client = _remembered(env)
    client.post("/api/push/subscribe", json=SUB)
    monkeypatch.setattr(push, "DEBOUNCE_S", 0.0)

    class WebPushException(Exception):
        pass

    def failing(sub, data, *, topic):
        e = WebPushException("push failed")
        e.response = types.SimpleNamespace(status_code=status)
        raise e

    monkeypatch.setattr(push, "_send", failing)
    assert asyncio.run(push.notify("reply", "t-1", "x")) == 0
    assert bool(_subs()) is kept


def test_open_install_keys_by_endpoint_until_a_pin_is_set(env, sent):
    client = env()                                     # no PIN at all
    assert client.post("/api/push/subscribe", json=SUB).status_code == 200
    assert list(_subs()) == [push.open_key(FCM)]
    assert asyncio.run(push.notify("reply", "t", "x")) == 1
    auth.set_pin(PIN)
    assert asyncio.run(push.notify("reply", "t2", "x")) == 0
    assert _subs() == {}


def test_the_real_sender_is_called_with_vapid_and_no_content(env, monkeypatch):
    """`_send` itself, with pywebpush's webpush() mocked: the VAPID key and
    claims go in, and the data is exactly the ping."""
    client = _remembered(env)
    client.post("/api/push/subscribe", json=SUB)
    calls = []
    import pywebpush
    def fake_webpush(*a, **kw):
        # The VAPID object must actually be able to sign these claims (py_vapid
        # refuses a `sub` that is not mailto:) — a mock that skipped this let a
        # broken default through once.
        kw["vapid_private_key"].sign({**kw["vapid_claims"], "aud": "https://fcm.googleapis.com"})
        calls.append((a, kw))

    monkeypatch.setattr(pywebpush, "webpush", fake_webpush)
    push._last_sent.clear()
    assert asyncio.run(push.notify("post", "t-9", "Morning brief posted")) == 1
    (args, kw), = calls
    assert args[0]["endpoint"] == FCM
    assert json.loads(kw["data"]) == {"kind": "post", "thread_id": "t-9",
                                      "title": "Morning brief posted"}
    assert kw["vapid_claims"]["sub"].startswith("mailto:")
    assert kw["ttl"] > 0 and kw["headers"]["Topic"]


# --------------------------------------------------------------------------- #
# The hooks
# --------------------------------------------------------------------------- #


def _bot_id() -> str:
    return config.load_bots()[0].id


def test_inject_with_notify_pings_without_the_content(env, sent):
    client = _remembered(env)
    client.post("/api/push/subscribe", json=SUB)
    r = client.post("/api/inject", json={"bot_id": _bot_id(),
                                         "content": "SECRET brief body",
                                         "notify": "Morning brief posted"})
    assert r.status_code == 200, r.text
    tid = r.json()["thread_id"]
    for _ in range(50):
        if sent:
            break
        client.portal.call(asyncio.sleep, 0.02)
    assert [s["data"] for s in sent] == [
        {"kind": "post", "thread_id": tid, "title": "Morning brief posted"}]
    assert "SECRET" not in json.dumps(sent)


def test_inject_without_notify_is_silent(env, sent):
    client = _remembered(env)
    client.post("/api/push/subscribe", json=SUB)
    r = client.post("/api/inject", json={"bot_id": _bot_id(), "content": "hello"})
    assert r.status_code == 200
    client.portal.call(asyncio.sleep, 0.1)
    assert sent == []


def test_a_finished_turn_with_a_new_reply_pings_once(env, sent, monkeypatch):
    client = _remembered(env)
    client.post("/api/push/subscribe", json=SUB)
    bot = _bot_id()
    name = config.get_bot(bot).name

    async def fake_turn(thread_id, bot_id, text, opts=None):
        await main._deliver_assistant_text(thread_id, "SECRET answer text")

    monkeypatch.setattr(main, "_run_agent_turn", fake_turn)

    async def go():
        thread = await main.db.create_thread(bot)
        await main.db.add_message(thread.id, "user", "hi")
        await main.run_agent_turn(thread.id, bot, "hi")
        # A second turn that adds nothing must not re-announce the first reply.
        monkeypatch.setattr(main, "_run_agent_turn",
                            lambda *a, **kw: asyncio.sleep(0))
        await main.run_agent_turn(thread.id, bot, "again")
        await asyncio.sleep(0.1)
        return thread.id

    tid = client.portal.call(go)
    assert [s["data"] for s in sent] == [
        {"kind": "reply", "thread_id": tid, "title": f"{name} replied"}]
    assert "SECRET" not in json.dumps(sent)


def test_no_subscriptions_means_no_work(env, monkeypatch):
    client = _remembered(env)
    called = []
    monkeypatch.setattr(push, "_send", lambda *a, **kw: called.append(1))
    main._notify_push("reply", "t", "x")
    client.portal.call(asyncio.sleep, 0.05)
    assert called == []
