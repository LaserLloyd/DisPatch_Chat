"""Review fixes for the agent-rule round (security review of fix/voice-agent-gate, 2026-10-22).

1. ctx.dispatch_turn's caller is recorded by an ASYNC dependency on every app route (a
   plain ``def`` dependency runs in a worker thread and its ContextVar never reached the
   route), and no recorded caller fails closed.
2. An unknown character (removed from config.yaml) counts as agent-backed, and its turn is
   refused outright instead of going to the gateway under the old id.
4. The boot sweep's missing share is taken against the rows that existed before this
   boot's adoption; a failed check is reported too.
5. The voice caller registry re-checks dispatch_agents on every submit; an Owner's turn is
   affected only while a no-dispatch socket is actually open.
6. Retry, edit & re-run and the speculative start carry dispatch off for such a caller.
Run: cd backend && uv run pytest tests/test_voice_agent_gate_review.py
"""
from __future__ import annotations

import asyncio
import json
import textwrap

import pytest
from _extfake import KNOBS, ext_env, install, thread_for
from test_advisor import _hooks, _run_with_reply, adv_env

from app import accounts, advisor, apps_loader, config, extensions, main, openclaw, policy

ORIGIN = {"origin": "http://testserver"}
ADV = {"providers": [{"provider": "custom", "base_url": "http://p/v1", "model": "m"}],
       "research_agent": "worker", "action_agent": "lead"}


def _clear_registry():
    for name in ("_ext_callers", "_ext_last_closed", "_nodispatch_threads"):
        getattr(extensions, name, {}).clear()


# --------------------------------------------------------------------------- #
# 1. Apps: a Depends-gated route, over real HTTP
# --------------------------------------------------------------------------- #

APP_BACKEND = '''
from fastapi import APIRouter, Depends

router = APIRouter()


def build(ctx):
    @router.post("/go", dependencies=[Depends(ctx.require_access)])
    async def go(body: dict):
        return {"dispatched": ctx.dispatch_turn(body["thread_id"], body["bot_id"], "x")}

    return router
'''


@pytest.fixture
def app_world(world, tmp_path, monkeypatch):
    import yaml
    repo = tmp_path / "apps-repo"
    d = repo / "turnprobe"
    (d / "static").mkdir(parents=True)
    (d / "static" / "index.html").write_text("<h1>x</h1>")
    (d / "app.yaml").write_text(yaml.safe_dump({"id": "turnprobe", "title": "Turn probe",
                                                "icon": "chart"}))
    (d / "backend.py").write_text(textwrap.dedent(APP_BACKEND))
    turns = []

    async def fake_turn(thread_id, bot_id, text, opts=None):
        turns.append((bot_id, opts))

    monkeypatch.setattr(main, "run_agent_turn", fake_turn)
    monkeypatch.setattr(openclaw, "cli_available", lambda: True)
    config.upsert_bot(config.Bot(id="adv", name="Sage", advisor=dict(ADV)))
    apps_loader.load_all(main.app, repo_dir=repo, hooks=main.APP_HOOKS)
    try:
        yield world, turns
    finally:
        apps_loader.unload(main.app, "turnprobe")


def test_an_app_route_gated_with_depends_applies_the_agent_rule(app_world):
    world, turns = app_world
    owner = world.create_owner(world.client())
    world.person(owner, "Max", "member", pin="5555")
    member = world.client(("203.0.113.140", 47001))
    assert world.login(member, name="Max", pin="5555").status_code == 200
    tid = owner.post("/api/threads", json={"bot_id": "main"}).json()["id"]

    # 4.0: app routes need use_tools, which is Admin-only now, so a Member (User) is refused
    # before the agent rule is reached (it never dispatches either way).
    r = member.post("/api/apps/turnprobe/go", json={"thread_id": tid, "bot_id": "main"})
    assert r.status_code == 403, r.text
    assert turns == []

    r = owner.post("/api/apps/turnprobe/go", json={"thread_id": tid, "bot_id": "main"})
    assert r.json()["dispatched"] is True and [t[0] for t in turns] == ["main"]

    machine = world.client(world.LOOPBACK, origin=False)             # an on-box cron
    r = machine.post("/api/apps/turnprobe/go", json={"thread_id": tid, "bot_id": "main"})
    assert r.status_code == 200, r.text
    assert r.json()["dispatched"] is True and len(turns) == 2

    r = member.post("/api/apps/turnprobe/go", json={"thread_id": tid, "bot_id": "adv"})
    assert r.status_code == 403 and len(turns) == 2


def test_an_app_turn_with_no_recorded_caller_fails_closed(world, monkeypatch):
    turns = []

    async def fake_turn(thread_id, bot_id, text, opts=None):
        turns.append((bot_id, opts))

    monkeypatch.setattr(main, "run_agent_turn", fake_turn)
    monkeypatch.setattr(openclaw, "cli_available", lambda: True)
    config.upsert_bot(config.Bot(id="adv", name="Sage", advisor=dict(ADV)))

    async def go(bot_id):
        token = apps_loader._caller.set(None)
        try:
            return main._app_dispatch_turn("t", bot_id, "x")
        finally:
            apps_loader._caller.reset(token)

    c = world.client()
    assert c.portal.call(go, "main") is False
    assert c.portal.call(go, "adv") is True
    c.portal.call(asyncio.sleep, 0.05)
    assert [b for b, _ in turns] == ["adv"] and turns[0][1].dispatch is False


# --------------------------------------------------------------------------- #
# 2. Unknown characters
# --------------------------------------------------------------------------- #

def test_an_unknown_character_counts_as_agent_backed():
    assert extensions.runs_agent(None) is True
    member = policy.Principal(kind="profile", role="member", profile_id="p", name="Max",
                              permissions=frozenset({"use_chat"}))
    assert extensions.may_dispatch(member, None) is False


def test_a_turn_on_an_unknown_character_is_refused(world, monkeypatch):
    frames = []

    async def capture(frame):
        frames.append(frame)

    async def boom(*a, **kw):
        raise AssertionError("must not compose a turn for an unknown character")

    monkeypatch.setattr(main.manager, "broadcast", capture)
    monkeypatch.setattr(main, "_compose_agent_text", boom)
    c = world.client()
    c.portal.call(main._run_agent_turn, "t-ghost", "ghost-bot", "hello")
    assert any(f.get("code") == "unknown_character" and f.get("thread_id") == "t-ghost"
               for f in frames), frames


# --------------------------------------------------------------------------- #
# 4. Boot sweep
# --------------------------------------------------------------------------- #

@pytest.fixture
def sweep(world):
    c = world.client()
    d = world.tmp / "files"
    d.mkdir(parents=True, exist_ok=True)
    main.FILES_DIR = d
    yield c, d
    main._file_sweep_skipped = None


def _rows(c, n, prefix, blobs_dir=None):
    async def go():
        for i in range(n):
            await main.db.add_file(f"{prefix}{i}.pdf", f"{prefix}{i}.bin", 1, None,
                                   owner_profile_id=None)
            if blobs_dir is not None:
                (blobs_dir / f"{prefix}{i}.bin").write_bytes(b"x")
    c.portal.call(go)


def test_adopted_blobs_do_not_dilute_the_missing_share(sweep):
    """12 of 30 records missing (40%) - a storage fault - while 100 untracked blobs get
    adopted this boot: 12 of 130 (9%) must not make it look like housekeeping."""
    c, d = sweep
    _rows(c, 18, "ok", d)
    _rows(c, 12, "ghost")
    for i in range(100):
        (d / f"drop{i}.bin").write_bytes(b"y")
    c.portal.call(main._sweep_orphan_blobs)
    names = {r["stored_name"] for r in c.portal.call(main.db.list_files)}
    assert sum(n.startswith("ghost") for n in names) == 12          # nothing purged
    assert main._file_sweep_skipped == {"reason": "mass_missing", "missing": 12, "total": 30}


def test_a_failed_check_is_reported(sweep, monkeypatch):
    c, _d = sweep

    async def broken(*a, **kw):
        raise RuntimeError("disk")

    monkeypatch.setattr(main.db, "files_missing_from", broken)
    c.portal.call(main._sweep_orphan_blobs)
    assert main._file_sweep_skipped == {"reason": "check_failed"}


# --------------------------------------------------------------------------- #
# 5 + 6. Voice: re-checked per submit; retry / edit & re-run / speculation
# --------------------------------------------------------------------------- #

@pytest.fixture
def hh(ext_env):
    from fastapi.testclient import TestClient
    c, _ = ext_env
    _clear_registry()
    config.upsert_bot(config.Bot(id="adv", name="Sage", advisor=dict(ADV)))
    install()
    r = c.post("/api/auth/setup", json={"name": "Olive", "password": "owner-pass1", "new_pin": "2468"})
    assert r.status_code == 200, r.text
    made = []

    def member(grants=None, addr=("203.0.113.150", 48001)):
        body = {"name": "Max", "role": "member", "pin": "5555"}
        if grants:
            body["grants"] = grants
        r = c.post("/api/people", json=body)
        assert r.status_code == 201, r.text
        k = TestClient(main.app, client=addr)
        k.__enter__()
        k.headers.update({"Origin": "http://testserver"})
        made.append(k)
        assert k.post("/api/auth/login", json={"name": "Max", "pin": "5555"}).status_code == 200
        return k, r.json()["id"]
    try:
        yield c, member
    finally:
        for k in made:
            k.__exit__(None, None, None)
        _clear_registry()


def _dispatch(turn) -> bool:
    return True if turn[3] is None else turn[3].dispatch


def test_a_mid_call_revocation_takes_effect_on_the_next_submit(hh):
    c, member = hh
    KNOBS.handler_mode = "hold"
    k, pid = member(grants=["dispatch_agents"])
    tid = thread_for(k, "adv")
    with k.websocket_connect(f"/ws/voice/{tid}", headers=ORIGIN) as ws:
        assert json.loads(ws.receive_text())["type"] == "ready"
        assert c.portal.call(lambda: KNOBS.ctx.submit_turn(tid, "one"))
        accounts.update_profile(pid, grants=[])                     # the Owner revokes it
        assert c.portal.call(lambda: KNOBS.ctx.submit_turn(tid, "two"))
    assert [_dispatch(t) for t in c.turns] == [True, False]


def test_the_owner_is_unaffected_once_the_members_socket_closed(hh):
    c, member = hh
    KNOBS.handler_mode = "hold"
    k, _pid = member()
    tid = thread_for(k, "adv")
    with k.websocket_connect(f"/ws/voice/{tid}", headers=ORIGIN) as ws:
        assert json.loads(ws.receive_text())["type"] == "ready"
    # 4.0: the Member's thread is the Member's; the Owner talks in its own one.
    own = thread_for(c, "adv")
    with c.websocket_connect(f"/ws/voice/{own}", headers=ORIGIN) as ws:
        assert json.loads(ws.receive_text())["type"] == "ready"
        assert c.portal.call(lambda: KNOBS.ctx.submit_turn(own, "owner speaking"))
    assert [_dispatch(t) for t in c.turns] == [True]


def test_the_members_hang_up_turn_still_runs_with_dispatch_off(hh):
    c, member = hh
    KNOBS.handler_mode = "hold"
    k, _pid = member()
    tid = thread_for(k, "adv")
    with k.websocket_connect(f"/ws/voice/{tid}", headers=ORIGIN) as ws:
        assert json.loads(ws.receive_text())["type"] == "ready"
    assert c.portal.call(lambda: KNOBS.ctx.submit_turn(tid, "committed on hang-up"))
    assert [_dispatch(t) for t in c.turns] == [False]


def _ws_send(k, frame):
    with k.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "hello"
        ws.send_json(frame)
        for _ in range(8):
            f = ws.receive_json()
            if f.get("type") in ("ack", "error"):
                return f


def _wait(c, cond):
    for _ in range(150):
        if cond():
            return
        c.portal.call(asyncio.sleep, 0.02)


def test_a_member_retry_on_an_advisor_runs_with_dispatch_off(hh):
    c, member = hh
    k, _pid = member()
    tid = thread_for(k, "adv")
    _ws_send(k, {"type": "send", "thread_id": tid, "text": "q", "client_msg_id": "c-q"})
    _wait(c, lambda: len(c.turns) >= 1)
    with k.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "hello"
        ws.send_json({"type": "retry", "thread_id": tid})
        _wait(c, lambda: len(c.turns) >= 2)
    assert len(c.turns) == 2 and _dispatch(c.turns[1]) is False


def test_a_member_edit_rerun_on_an_advisor_runs_with_dispatch_off(hh, monkeypatch):
    c, member = hh
    k, _pid = member()
    tid = thread_for(k, "adv")
    _ws_send(k, {"type": "send", "thread_id": tid, "text": "q", "client_msg_id": "c-q2"})
    _wait(c, lambda: len(c.turns) >= 1)
    rows, _ = c.portal.call(main.db.list_messages, tid, 10)
    mid = [m for m in rows if m.role == "user"][-1].id
    seen = []

    async def fake_regen(thread_id, bot_id, user_msg, **kw):
        seen.append(kw)

    monkeypatch.setattr(main, "_regenerate_turn", fake_regen)
    with k.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "hello"
        ws.send_json({"type": "edit_rerun", "thread_id": tid, "message_id": mid, "content": "q!"})
        _wait(c, lambda: seen)
    assert seen and seen[0].get("dispatch") is False and seen[0].get("edit_to") == "q!"


def test_a_member_speculative_start_is_built_with_dispatch_off(hh, monkeypatch):
    c, member = hh
    KNOBS.handler_mode = "hold"
    calls = []

    async def fake_speculate(thread_id, bot_id, text, **kw):
        calls.append(kw)
        return None

    monkeypatch.setattr(main.advisor, "speculate", fake_speculate)
    k, _pid = member()
    tid = thread_for(k, "adv")
    with k.websocket_connect(f"/ws/voice/{tid}", headers=ORIGIN) as ws:
        assert json.loads(ws.receive_text())["type"] == "ready"
        c.portal.call(lambda: KNOBS.ctx.speculate_turn(tid, "maybe"))
    otid = thread_for(c, "adv")
    with c.websocket_connect(f"/ws/voice/{otid}", headers=ORIGIN) as ws:
        assert json.loads(ws.receive_text())["type"] == "ready"
        c.portal.call(lambda: KNOBS.ctx.speculate_turn(otid, "maybe"))
    assert calls[0].get("dispatch") is False and "dispatch" not in calls[1]


def test_speculate_with_dispatch_off_offers_no_markers(adv_env, monkeypatch):
    tmp_path, _ = adv_env
    advisor.bind(_hooks(tmp_path, [], []))
    seen = _run_with_reply(monkeypatch, "Sure.")

    async def go(dispatch):
        run = await advisor.speculate("t1", "adv", "Is rooftop solar worth it?", dispatch=dispatch)
        await run.result()
        return seen["system"]

    on = asyncio.run(go(True))
    off = asyncio.run(go(False))
    assert "[[research:" in on
    assert "[[research:" not in off and advisor.NO_DISPATCH_RULES in off
