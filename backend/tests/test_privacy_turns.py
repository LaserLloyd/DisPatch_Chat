"""Privacy matrix, turn entry points and pictures.

Every way a turn can start (WS send / retry / regenerate / edit_rerun, REST thread creation, the
extension host's submit_turn and speculate_turn) must refuse a bot the thread's owner may not use:
a household bot that is not on a Basic profile's list, and another profile's personal bot. A
refused turn never reaches a model (no ``run_agent_turn``, no ``advisor.run_turn``/``speculate``).
Each refusal has a positive control, so a malformed request cannot pass vacuously.
"""
from __future__ import annotations

import time

import privacy_fake_addon as fake
import pytest
from privacy_fixtures import (
    HOUSEHOLD_BOT,
    HOUSEHOLD_OTHER,
    PRIVACY_MARKS,
    build_install,
    drain,
)

from app import accounts, advisor, config, llm_api, main

pytestmark = PRIVACY_MARKS


class Turns:
    def __init__(self):
        self.agent: list = []
        self.model: list = []


@pytest.fixture
def turns(monkeypatch):
    t = Turns()

    async def fake_turn(thread_id, bot_id, text, opts=None):
        t.agent.append((thread_id, bot_id))

    async def fake_run(*a, **kw):
        t.model.append(("run", a[:2]))

    async def fake_spec(*a, **kw):
        t.model.append(("speculate", a[:2]))
        return None
    monkeypatch.setattr(main, "run_agent_turn", fake_turn)
    monkeypatch.setattr(advisor, "run_turn", fake_run)
    monkeypatch.setattr(advisor, "speculate", fake_spec)
    monkeypatch.setattr(llm_api, "run_api_turn", fake_run)
    monkeypatch.setattr(llm_api, "speculate", fake_spec, raising=False)
    return t


def _plant(inst, owner: str, bot_id: str):
    """A thread of ``owner`` on ``bot_id`` with one user message (the API would refuse to make it)."""
    db = inst.world.db

    async def go():
        t = await db.create_thread(bot_id, title="planted", owner_profile_id=inst.pids[owner])
        m = await db.add_message(t.id, "user", "planted question")
        return t.id, m.id
    return inst.run(go)


def _wait(turns, n, ws):
    for _ in range(40):
        if len(turns.agent) >= n:
            return
        drain(ws)
        time.sleep(0.05)


ACTIONS = ("send", "retry", "regenerate", "edit_rerun")


def _frame(action, tid, mid):
    return {"send": {"type": "send", "thread_id": tid, "text": "hello", "client_msg_id": "c1"},
            "retry": {"type": "retry", "thread_id": tid},
            "regenerate": {"type": "regenerate", "thread_id": tid},
            "edit_rerun": {"type": "edit_rerun", "thread_id": tid, "message_id": mid, "content": "changed"}}[action]


@pytest.mark.parametrize("action", ACTIONS)
def test_positive_control_own_thread_starts_a_turn(world, monkeypatch, turns, action):
    inst = build_install(world, monkeypatch, "U1")
    tid, mid = _plant(inst, "U1", HOUSEHOLD_BOT)
    with inst.client("U1").websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json(_frame(action, tid, mid))
        _wait(turns, 1, ws)
    assert turns.agent or turns.model, f"{action}: a turn on an allowed bot never started (harness is vacuous)"


@pytest.mark.parametrize("action", ACTIONS)
def test_basic_profile_cannot_run_a_household_bot_off_its_list(world, monkeypatch, turns, action):
    inst = build_install(world, monkeypatch, "B1")
    tid, mid = _plant(inst, "B1", HOUSEHOLD_OTHER)
    with inst.client("B1").websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json(_frame(action, tid, mid))
        frames = drain(ws)
        time.sleep(0.3)
        frames += drain(ws)
    assert not turns.agent and not turns.model, f"{action}: B1 ran {HOUSEHOLD_OTHER}, which is not on its list"
    assert any(f.get("type") == "error" for f in frames), f"{action}: refused silently (no error frame)"


@pytest.mark.parametrize("action", ACTIONS)
def test_a_personal_bot_of_someone_else_cannot_be_run(world, monkeypatch, turns, action):
    inst = build_install(world, monkeypatch, "U1")
    foreign = inst.get("personal_bot", "A0").ids["bot"]
    tid, mid = _plant(inst, "U1", foreign)
    with inst.client("U1").websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json(_frame(action, tid, mid))
        frames = drain(ws)
        time.sleep(0.3)
        frames += drain(ws)
    assert not turns.agent and not turns.model, f"{action}: ran another profile's personal bot"
    assert any(f.get("type") == "error" for f in frames)


@pytest.mark.parametrize("actor", ("A0", "A2", "U1", "U2"))
def test_creating_a_thread_with_someone_elses_personal_bot_is_refused(world, monkeypatch, actor):
    inst = build_install(world, monkeypatch, actor)
    victim = "U1" if actor != "U1" else "A0"
    bot = inst.get("personal_bot", victim).ids["bot"]
    r = inst.client(actor).post("/api/threads", json={"bot_id": bot})
    assert r.status_code in (400, 403, 404), f"{actor} created a thread on {victim}'s personal bot: {r.status_code}"
    r = inst.client("B1").post("/api/threads", json={"bot_id": HOUSEHOLD_OTHER})
    assert r.status_code in (400, 403, 404), f"B1 created a thread on a bot off its list: {r.status_code}"
    r = inst.client("B1").post("/api/threads", json={"bot_id": HOUSEHOLD_BOT})
    assert r.status_code == 200, f"B1 could not start a chat with its own household bot: {r.status_code}"


@pytest.fixture
def addon(world):
    fake.install(main.app)
    yield fake.STATE
    fake.uninstall(main.app)


def test_extension_turn_entry_points(world, monkeypatch, turns, addon):
    inst = build_install(world, monkeypatch, "U1")
    ok_tid, _ = _plant(inst, "U1", HOUSEHOLD_BOT)
    bad_basic, _ = _plant(inst, "B1", HOUSEHOLD_OTHER)
    bad_personal, _ = _plant(inst, "U1", inst.get("personal_bot", "A0").ids["bot"])
    ctx = addon.ctx

    def call(fn):
        try:
            return inst.run(fn)
        except Exception as e:                 # a refusal may raise
            return e

    n0 = len(turns.agent)
    assert not isinstance(call(lambda: ctx.submit_turn(ok_tid, "spoken words")), Exception)
    assert len(turns.agent) > n0, "positive control: submit_turn on an allowed bot must start a turn"
    for label, tid in (("household bot off the list", bad_basic), ("another profile's personal bot", bad_personal)):
        before = (len(turns.agent), len(turns.model))
        res = call(lambda tid=tid: ctx.submit_turn(tid, "spoken words"))
        assert (len(turns.agent), len(turns.model)) == before, f"submit_turn ran a turn: {label}"
        assert res is None or isinstance(res, Exception), f"submit_turn accepted: {label} ({res!r})"
        res = call(lambda tid=tid: ctx.speculate_turn(tid, "spoken words"))
        assert (len(turns.agent), len(turns.model)) == before, f"speculate_turn started a model call: {label}"
        assert res is None or isinstance(res, Exception)


def _pic_jobs(inst, owner):
    tid = inst.pic_threads[owner]
    return inst.run(lambda: inst.world.db.open_image_jobs_for_thread(tid))


@pytest.mark.parametrize("order", [("B1", "U1"), ("U1", "B1")])
def test_pic_marker_is_dropped_without_use_images(world, monkeypatch, order):
    from app import image_providers
    inst = build_install(world, monkeypatch, "U1")
    # B1 loses its use_images grant for this test; U1 (a User) keeps the default.
    accounts.update_profile(inst.pids["B1"], grants=["dispatch_agents"])
    for bot in (HOUSEHOLD_BOT, HOUSEHOLD_OTHER):
        b = config.get_bot(bot)
        config.upsert_bot(config.Bot(**{**b.__dict__, "image_jobs": True}))
    config._invalidate_bots_cache()
    image_providers.save_config({"provider": "minimax", "api_key": "sk-test-MATRIX-0123456789"})
    monkeypatch.setattr(main, "_image_jobs_configured", lambda: True)
    monkeypatch.setattr(main, "_wake_image_jobs", lambda: None)
    monkeypatch.setattr(main, "_choose_image_backend", lambda workflow: "api")
    # One thread per owner, each on its own bot, so the per-bot coalescing and rate limit of
    # the picture pipeline cannot make one owner's result depend on the other's.
    inst.pic_threads = {"B1": _plant(inst, "B1", HOUSEHOLD_BOT)[0], "U1": _plant(inst, "U1", HOUSEHOLD_OTHER)[0]}
    text = "Here you go [[pic:a red fox in snow|fox]]"
    results = {}
    for owner in order:
        tid = inst.pic_threads[owner]
        from app import image_jobs
        image_jobs.limiter.reset()          # the per-bot rate limit must not decide this test
        before = len(_pic_jobs(inst, owner))
        msg = inst.run(lambda tid=tid: main._deliver_assistant_text(tid, text))
        content = getattr(msg, "content", "") or ""
        for _ in range(30):                      # the job row is written by a background task
            if len(_pic_jobs(inst, owner)) > before:
                break
            time.sleep(0.1)
        results[owner] = (content, len(_pic_jobs(inst, owner)) - before)
    b1_text, b1_jobs = results["B1"]
    assert "[[pic:" not in b1_text.lower() and b1_jobs == 0, f"B1 (no use_images) got a picture: {results['B1']}"
    u1_text, u1_jobs = results["U1"]
    assert u1_jobs > 0 or u1_text != b1_text, f"positive control: U1's marker was treated like B1's: {results['U1']}"
