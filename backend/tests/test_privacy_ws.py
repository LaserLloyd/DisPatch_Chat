"""Privacy matrix, WebSockets: six live sockets (A0 A2 U1 U2 B1 D); every thread-scoped frame type
about a U1 thread reaches U1 only; roster, hello, reaction, progress, task and notice frames are
narrowed per socket; an unattributed or unknown-thread frame reaches nobody; ``/ws`` actions on a
foreign thread are refused."""
from __future__ import annotations

import contextlib

import pytest
from privacy_fixtures import (
    HOUSEHOLD_BOT,
    HOUSEHOLD_OTHER,
    PRIVACY_MARKS,
    SENTINEL_RE,
    broadcast,
    build_install,
    drain,
    ids_in,
)

from app import main

pytestmark = PRIVACY_MARKS

SOCKET_ACTORS = ("A0", "A2", "U1", "U2", "B1", "D")

#: Thread-scoped frame types. Unioned with the host's own tuple when CORE exposes one.
THREAD_FRAMES = ["message", "message_update", "stream_done", "thread_update", "thread_created",
                 "thread_deleted", "thinking", "message_deleted", "checklist_update", "stream_start",
                 "stream_chunk", "turn_status", "message_feedback", "messages", "threads_list",
                 "notice", "ack", "error", "reaction", "progress"]
for _name in ("_THREAD_FRAMES", "THREAD_FRAMES"):
    for _mod in (main,):
        THREAD_FRAMES += [t for t in getattr(_mod, _name, ()) if t not in THREAD_FRAMES]


def _frame(ftype: str, tid: str, bot: str, s: str) -> dict:
    f: dict = {"type": ftype, "thread_id": tid, "bot_id": bot, "delta": f"{s}-delta", "content": f"{s}-content"}
    f["message"] = {"id": "mx", "thread_id": tid, "role": "assistant", "content": f"{s}-message"}
    f["thread"] = {"id": tid, "bot_id": bot, "title": f"{s}-title"}
    f["messages"] = [f["message"]]
    f["threads"] = [f["thread"]]
    f["text"] = f"{s}-text"
    return f


@contextlib.contextmanager
def sockets(inst, actors=SOCKET_ACTORS):
    with contextlib.ExitStack() as st:
        out = {}
        for a in actors:
            ws = st.enter_context(inst.client(a).websocket_connect("/ws"))
            out[a] = (ws, ws.receive_json())          # hello
        yield out


def _others_empty(received: dict, who: str, marker: str, label: str):
    bad = [a for a, frames in received.items() if a != who and marker in str(frames)]
    assert not bad, f"{label}: {bad} received a frame carrying {marker}"


@pytest.mark.parametrize("ftype", sorted(set(THREAD_FRAMES)))
def test_thread_frame_reaches_only_the_owner(world, monkeypatch, ftype):
    inst = build_install(world, monkeypatch, "U1")
    th = inst.get("thread", "U1")
    with sockets(inst) as socks:
        s = inst.sent("thread", "U1")
        main._thread_bot[th.ids["thread"]] = HOUSEHOLD_BOT
        broadcast(inst, _frame(ftype, th.ids["thread"], HOUSEHOLD_BOT, s))
        got = {a: drain(ws) for a, (ws, _) in socks.items()}
    _others_empty(got, "U1", s, f"{ftype} frame")


def test_hello_lists_only_own_threads_bots_and_profile(world, monkeypatch):
    inst = build_install(world, monkeypatch, "U1")
    personal = {o: r.ids["bot"] for (k, o), r in inst.res.items() if k == "personal_bot"}
    with sockets(inst) as socks:
        for a, (_, hello) in socks.items():
            ids = {b["id"] for b in hello.get("bots", [])}
            foreign = {b for o, b in personal.items() if o != a}
            assert not (ids & foreign), f"{a}: hello lists another profile's personal bot"
            others = [s for s in SENTINEL_RE.findall(str(hello)) if s[1] != a]
            assert not others, f"{a}: hello carries {others}"
            if a == "B1":
                assert ids == {HOUSEHOLD_BOT}, f"B1 hello lists {sorted(ids)}"


def test_bots_broadcast_is_narrowed_per_socket(world, monkeypatch):
    inst = build_install(world, monkeypatch, "U1")
    personal = {o: r for (k, o), r in inst.res.items() if k == "personal_bot"}
    roster = [{"id": HOUSEHOLD_BOT, "name": "Household One"}, {"id": HOUSEHOLD_OTHER, "name": "Household Two"}] + [
        {"id": r.ids["bot"], "name": f"{r.sentinel}-name"} for r in personal.values()]
    with sockets(inst) as socks:
        broadcast(inst, {"type": "bots", "bots": roster})
        got = {a: drain(ws) for a, (ws, _) in socks.items()}
    for a, frames in got.items():
        ids = ids_in(frames, "bots")
        foreign = {r.ids["bot"] for o, r in personal.items() if o != a}
        assert not (ids & foreign), f"{a}: bots frame lists another profile's personal bot"
        assert not [s for s in SENTINEL_RE.findall(str(frames)) if s[1] != a], f"{a}: bots frame leaks a name"
        if a == "B1":
            assert HOUSEHOLD_OTHER not in ids, "B1 learns about a bot that is not on its list"


def test_rest_rename_of_a_household_bot_does_not_reach_a_basic_socket(world, monkeypatch):
    inst = build_install(world, monkeypatch, "U1")
    with sockets(inst) as socks:
        r = inst.client("A0").patch(f"/api/bots/{HOUSEHOLD_OTHER}", json={"name": "Renamed By Matrix"})
        assert r.status_code == 200, r.text
        got = {a: drain(ws) for a, (ws, _) in socks.items()}
    assert "Renamed By Matrix" not in str(got["B1"])


def test_task_and_progress_frames(world, monkeypatch):
    from app import agents_ws
    inst = build_install(world, monkeypatch, "U1")
    tk = inst.get("task", "U1")
    s = tk.sentinel
    with sockets(inst) as socks:
        for ftype in sorted(agents_ws.FRAME_TYPES):
            frame = {"type": ftype, "task_id": tk.ids["task"], "agent_name": f"{s}-agent",
                     "args_summary": f"{s}-args", "event": {"text": f"{s}-event"}, "task": {"state": "running"},
                     "request_id": "r1", "state": "pending"}
            inst.run(lambda frame=frame: agents_ws.publish(frame, inst.pids["U1"]))
        got = {a: drain(ws) for a, (ws, _) in socks.items()}
    _others_empty(got, "U1", s, "task frames")
    assert s in str(got["U1"]), "the owner (who holds dispatch_agents) must receive its own task frames"


def test_unattributed_and_unknown_thread_frames_reach_nobody(world, monkeypatch):
    inst = build_install(world, monkeypatch, "U1")
    s = "SENTINEL-frame-A0-deadbeef"
    with sockets(inst) as socks:
        broadcast(inst, {"type": "message", "message": {"id": "z", "role": "assistant", "content": s}})
        broadcast(inst, {"type": "message", "thread_id": "no-such-thread",
                         "message": {"id": "y", "role": "assistant", "content": s}})
        broadcast(inst, {"type": "thread_update", "thread": {"id": "no-such-thread", "title": s}})
        got = {a: drain(ws) for a, (ws, _) in socks.items()}
    bad = [a for a, f in got.items() if s in str(f)]
    assert not bad, f"{bad} received a frame that names no known thread"


def test_a_missing_principal_drops_thread_and_bot_scoped_frames():
    f_thread = {"type": "message", "thread_id": "t", "message": {"content": "x"}}
    f_bot = {"type": "bots", "bots": [{"id": "p000000000000"}]}
    assert main.scope_frame_for_principal(f_thread, None) is None
    assert main.scope_frame_for_principal(f_bot, None) in (None, {**f_bot, "bots": []})


FOREIGN_ACTIONS = [
    ("get_messages", lambda t, m: {"type": "get_messages", "thread_id": t}),
    ("archive_thread", lambda t, m: {"type": "archive_thread", "thread_id": t}),
    ("abort", lambda t, m: {"type": "abort", "thread_id": t}),
    ("send", lambda t, m: {"type": "send", "thread_id": t, "text": "hi", "client_msg_id": "cm1"}),
    ("retry", lambda t, m: {"type": "retry", "thread_id": t}),
    ("regenerate", lambda t, m: {"type": "regenerate", "thread_id": t}),
    ("edit_rerun", lambda t, m: {"type": "edit_rerun", "thread_id": t, "message_id": m, "content": "edited"}),
]


@pytest.mark.parametrize("name,make", FOREIGN_ACTIONS, ids=[a for a, _ in FOREIGN_ACTIONS])
@pytest.mark.parametrize("actor", ("A0", "A2", "U2", "B1", "D"))
def test_ws_action_on_a_foreign_thread_is_refused(world, monkeypatch, actor, name, make):
    inst = build_install(world, monkeypatch, actor)
    victim = "U1"
    th = inst.get("thread", victim)
    turns = []

    async def fake_turn(thread_id, bot_id, text, opts=None):
        turns.append(thread_id)
    monkeypatch.setattr(main, "run_agent_turn", fake_turn)
    with sockets(inst, (actor,)) as socks:
        ws, _ = socks[actor]
        ws.send_json(make(th.ids["thread"], th.ids["m_user"]))
        frames = drain(ws)
    text = str(frames)
    assert th.sentinel not in text, f"{actor}: {name} on {victim}'s thread returned its content"
    assert not [f for f in frames if f.get("type") in ("messages", "stream_start", "thinking", "message")
                and f.get("thread_id") == th.ids["thread"]], f"{actor}: {name} produced thread frames"
    assert not turns, f"{actor}: {name} started a turn on {victim}'s thread"
    t = inst.run(lambda: world.db.get_thread(th.ids["thread"]))
    assert t is not None and t.title == th.intact["title"] and not t.is_archived, "foreign thread was changed"
