"""Shared helpers for the agent-dispatch API suites (G17, policy part).

Everything runs through the REAL app (the ``world`` fixture of tests/conftest.py): the gate,
the policy engine, SQLite, the WebSocket manager and the task manager are all live. Only
the backend is fake - ``app.agents.fake.FakeBackend``, registered into the live manager -
so no gateway, no model and no network is ever touched.
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from app import agents_service
from app.agents.fake import FakeBackend

LAN = ("203.0.113.50", 40000)
TERMINAL = ("succeeded", "failed", "cancelled")


class FakeOpenClaw(FakeBackend):
    """The scripted fake wearing the OpenClaw backend's name (for the unified-dispatch flag)."""

    name = "openclaw"


def spec(**over):
    base = {"backend": "fake", "tool_allowlist": ["Read", "Bash"],
            "approval_policy": {"mode": "interactive"}}
    base.update(over)
    return base


def wait_state(client, task_id, states, timeout=8.0):
    """Poll the API until the task reaches one of ``states``; returns the task dict."""
    want = (states,) if isinstance(states, str) else tuple(states)
    end = time.monotonic() + timeout
    last = None
    while time.monotonic() < end:
        r = client.get(f"/api/tasks/{task_id}")
        assert r.status_code == 200, r.text
        last = r.json()["task"]
        if last["state"] in want:
            return last
        time.sleep(0.03)
    raise AssertionError(f"task {task_id} stayed {last and last['state']!r}, wanted {want}")


def event_types(client, task_id):
    return [e["type"] for e in client.get(f"/api/tasks/{task_id}/events").json()["events"]]


def sibling(world, base, addr, *, origin=True):
    """Another caller (own cookies, own peer address) that shares ``base``'s event loop.

    ``world.client()`` enters a new TestClient, which runs the app's lifespan - and a whole
    event loop - of its own. The task manager lives on ONE loop, as it does in production, so
    the extra callers ride the owner's portal instead.
    """
    from fastapi.testclient import TestClient
    c = TestClient(world.main.app, client=addr)
    c.portal = base.portal
    if origin:
        c.headers.update({"Origin": "http://testserver"})
    return c


class Notices:
    """Captures what the manager would post into a chat and push to a phone."""

    def __init__(self):
        self.pushes: list[tuple[str, str, str]] = []
        self.posts: list[tuple[str, str, dict]] = []


@pytest.fixture
def aw(world, monkeypatch):
    """An installation with an Owner, a Restricted child, two Members (one allowed to
    dispatch), and a fake backend in the live task manager."""
    owner = world.create_owner()
    mgr = agents_service.get_manager()
    mgr.register_backend("fake", FakeBackend())
    notes = Notices()
    # main._shutting_down is set by the first lifespan to end in this process and never
    # reset (production starts once); the manager reads it before notifying.
    monkeypatch.setattr(world.main, "_shutting_down", False)
    monkeypatch.setattr(mgr.hooks, "push", lambda kind, ref, title: notes.pushes.append((kind, ref, title)))

    async def deliver(thread_id, text, **kw):
        notes.posts.append((thread_id, text, kw.get("metadata") or {}))
        return SimpleNamespace(id=f"msg-{len(notes.posts)}")
    monkeypatch.setattr(mgr.hooks, "deliver", deliver)

    def caller(i):
        return sibling(world, owner, (f"203.0.113.{50 + i}", 40000 + i))

    mia = world.person(owner, "Mia", "restricted")
    kid = caller(0)
    assert world.login(kid, profile_id=mia["id"]).status_code == 200
    world.person(owner, "Max", "member", pin="5555")                         # no dispatch grant
    eve = world.person(owner, "Eve", "member", pin="6666", grants=["dispatch_agents"])
    eve_c = caller(1)
    assert world.login(eve_c, name="Eve", pin="6666").status_code == 200
    max_c = caller(2)
    assert world.login(max_c, name="Max", pin="5555").status_code == 200
    world.person(owner, "Fay", "member", pin="7777", grants=["dispatch_agents"])
    fay_c = caller(3)
    assert world.login(fay_c, name="Fay", pin="7777").status_code == 200
    ns = SimpleNamespace(world=world, owner=owner, kid=kid, eve=eve_c, eve_id=eve["id"], max=max_c,
                         fay=fay_c, mgr=mgr, notes=notes, anonymous=caller(4), caller=caller)
    yield ns


def make_agent(client, name="Helper", published=None, **over):
    body = {"name": name, "spec": over.pop("spec", spec()), "published_to": published or []}
    body.update(over)
    r = client.post("/api/agents", json=body)
    assert r.status_code == 201, r.text
    return r.json()
