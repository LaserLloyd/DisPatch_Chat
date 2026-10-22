"""Task frames over the WebSocket: who receives them, and who never does (G17, policy part).

``task.event`` / ``task.approval`` / ``task.update`` reach the owner of a task and an
Owner/Admin - nobody else. A Restricted socket, a signed-out socket and a Member who may not
dispatch receive none of them, whatever else is broadcast; another dispatching Member sees
only their own.
"""
from __future__ import annotations

import pytest
from agents_world import aw, make_agent, spec, wait_state

from app import agents_ws, main


def _drain(ws, until_type=None, until=None, limit=200):
    """Read frames until ``until(frame)`` is true (or the sentinel pong); returns what was seen."""
    seen = []
    for _ in range(limit):
        f = ws.receive_json()
        seen.append(f)
        if until is not None and until(f):
            break
        if until_type and f.get("type") == until_type:
            break
    return seen


def _ping(ws):
    """Frames that arrive before a ping is answered (everything already sent on this socket)."""
    ws.send_json({"type": "ping"})
    return _drain(ws, until_type="pong")[:-1]


def _types(frames):
    return [f["type"] for f in frames]


def _result_of(task_id):
    return lambda f: (f.get("type") == "task.event" and f.get("task_id") == task_id
                      and f["event"]["type"] == "result")


@pytest.mark.timeout(60)
def test_the_owner_of_a_task_and_the_owner_account_receive_its_frames_and_nobody_else(aw):
    a = make_agent(aw.owner, published=["*"])
    with aw.owner.websocket_connect("/ws") as op, aw.eve.websocket_connect("/ws") as eve, \
            aw.fay.websocket_connect("/ws") as fay, aw.max.websocket_connect("/ws") as max_, \
            aw.kid.websocket_connect("/ws") as kid, aw.anonymous.websocket_connect("/ws") as anon:
        for s in (op, eve, fay, max_, kid, anon):
            assert s.receive_json()["type"] == "hello"
        tid = aw.eve.post("/api/tasks", json={"agent_id": a["id"], "prompt": "scenario:approval"}).json()["task"]["id"]
        # Eve's own socket: the timeline, then the approval card (she may not answer it)
        got = _drain(eve, until=lambda f: f.get("type") == "task.approval" and f["state"] == "pending")
        assert {f["task_id"] for f in got if "task_id" in f} == {tid}
        pending = got[-1]
        assert pending["tool"] == "Bash" and pending["scopes"] == ["once"]
        assert "echo hi" in pending["args_summary"]
        # the operator's socket sees the same card, and answers it
        seen_op = _drain(op, until=lambda f: f.get("type") == "task.approval" and f["state"] == "pending")
        assert seen_op[-1]["request_id"] == pending["request_id"]
        assert aw.owner.post(f"/api/approvals/{pending['request_id']}", json={"decision": "allow"}).status_code == 200
        rest = _drain(eve, until=_result_of(tid))
        assert any(f["type"] == "task.approval" and f["state"] == "allow" for f in rest)
        events = [f["event"] for f in got + rest if f["type"] == "task.event"]
        seqs = [e["seq"] for e in events]
        assert seqs == sorted(set(seqs)) and seqs[0] == 0              # in order, no repeats
        assert [e["type"] for e in events][-1] == "result"
        last = [f for f in rest if f["type"] == "task.event"][-1]
        assert last["task"]["state"] == "succeeded"
        # operator got the result too
        assert any(_result_of(tid)(f) for f in _drain(op, until=_result_of(tid)))
        # Everyone else heard nothing at all about it
        for name, sock in (("fay", fay), ("max", max_), ("kid", kid), ("anonymous", anon)):
            leaked = [f for f in _ping(sock) if str(f.get("type", "")).startswith("task.")]
            assert leaked == [], f"{name} received task frames: {leaked[:2]}"


@pytest.mark.timeout(60)
def test_another_dispatching_member_gets_only_their_own_frames(aw):
    a = make_agent(aw.owner, published=["*"])
    with aw.eve.websocket_connect("/ws") as eve, aw.fay.websocket_connect("/ws") as fay:
        eve.receive_json(), fay.receive_json()
        t_fay = aw.fay.post("/api/tasks", json={"agent_id": a["id"], "prompt": "scenario:simple"}).json()["task"]["id"]
        _drain(fay, until=_result_of(t_fay))
        wait_state(aw.fay, t_fay, "succeeded")
        assert [f for f in _ping(eve) if str(f.get("type", "")).startswith("task.")] == []


@pytest.mark.timeout(60)
def test_a_restricted_socket_receives_nothing_even_if_a_frame_is_forced_at_it(aw):
    a = make_agent(aw.owner, published=["*"])
    tid = aw.owner.post("/api/tasks", json={"agent_id": a["id"], "prompt": "scenario:simple"}).json()["task"]["id"]
    wait_state(aw.owner, tid, "succeeded")
    task = {"task_id": tid, "agent_id": a["id"], "state": "succeeded"}
    frames = [agents_ws.event_frame(task, {"seq": 99, "type": "text_delta", "text": "secret"}),
              agents_ws.approval_frame(task, {"request_id": "r", "tool": "Bash"}, "pending"),
              agents_ws.update_frame(task)]
    with aw.kid.websocket_connect("/ws") as kid, aw.anonymous.websocket_connect("/ws") as anon:
        kid.receive_json(), anon.receive_json()
        for fr in frames:
            # (1) to everyone: the redactor drops it for the Restricted socket
            aw.owner.portal.call(main.manager.broadcast, fr)
            # (2) to exactly the people who "own" it - the audience predicate still excludes them
            aw.owner.portal.call(agents_ws.publish, fr, aw.eve_id)
            # (3) even an audience that would match a MEMBER owner id cannot reach a Restricted socket
            aw.owner.portal.call(main.manager.broadcast_where, fr, lambda pr: True)
        for sock in (kid, anon):
            assert [f for f in _ping(sock) if str(f.get("type", "")).startswith("task.")] == []


def test_the_new_frames_are_explicitly_denied_to_locked_sockets():
    assert agents_ws.FRAME_TYPES == {"task.event", "task.approval", "task.update"}
    assert agents_ws.FRAME_TYPES <= main._DECOY_FRAME_DENY
    assert not (agents_ws.FRAME_TYPES & main._DECOY_FRAME_ALLOW)
    for t in agents_ws.FRAME_TYPES:
        assert main.redact_for_decoy({"type": t, "task_id": "x", "bot_id": "alpha"}) is None


def test_who_may_see_a_task_is_one_rule():
    from app.contracts.permissions import Permission

    class P:  # a stand-in with the attributes may_see_task reads
        def __init__(self, kind="profile", restricted=False, operator=False, pid=None, perms=()):
            self.kind, self.is_restricted, self.is_admin, self.profile_id = kind, restricted, operator, pid
            self._perms = set(perms)

        def can(self, p):
            return (p.value if isinstance(p, Permission) else p) in self._perms

    assert agents_ws.may_see_task(P(kind="open"), "anyone")
    assert agents_ws.may_see_task(P(operator=True), "anyone")
    assert agents_ws.may_see_task(P(pid="a", perms={"dispatch_agents"}), "a")
    assert not agents_ws.may_see_task(P(pid="a", perms={"dispatch_agents"}), "b")
    assert not agents_ws.may_see_task(P(pid="a"), "a"), "the grant was revoked: the old frames stop at once"
    assert not agents_ws.may_see_task(P(pid="a", restricted=True, perms={"dispatch_agents"}), "a")
    assert not agents_ws.may_see_task(P(kind="machine"), "a")
    assert not agents_ws.may_see_task(P(kind="anonymous", restricted=True), "")
    assert not agents_ws.may_see_task(None, "a")
