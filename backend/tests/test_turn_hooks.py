"""The turn hooks main lends an extension (app/main.py `_ext_*`, CONTRACTS
§B.1/§B.2): submit_turn (persist + broadcast before the task, instructions
placement, adoption of a speculative run), speculate_turn, abort_turn with
keep_partial, retract_message, replies_after, note_heard and
subscribe_frames (per-thread, bounded, snapshot on subscribe, redaction).

The direct backends' own halves (system slot, speculate(), the cancel rule)
are T2's and tested in test_advisor.py / test_llm_api.py; here they are
doubles, so these tests pin only what the HOST does.
"""
from __future__ import annotations

import asyncio

import pytest
import yaml
from _extfake import KNOBS, ext_env, install, thread_for, unlock

from app import advisor, auth, config, extensions, llm_api, main

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def add_bots() -> None:
    """An API bot and an advisor bot beside the test roster."""
    config.load_bots()
    doc = yaml.safe_load(config.CONFIG_PATH.read_text())
    doc["bots"] += [
        {"id": "swift", "name": "Swift", "order": 20,
         "api": {"provider": "openai", "base_url": "http://127.0.0.1:9/v1", "model": "m"}},
        {"id": "sage", "name": "Sage", "order": 21,
         "advisor": {"providers": [{"provider": "openai", "base_url": "http://127.0.0.1:9/v1",
                                    "model": "m"}]}},
    ]
    config.CONFIG_PATH.write_text(yaml.safe_dump(doc))
    config._invalidate_bots_cache()


class FakeRun:
    def __init__(self, head_id, text=""):
        self.text, self.done, self.head_id, self.prompt_chars = text, False, head_id, 1234
        self.discarded = 0

    async def discard(self):
        self.discarded += 1
        self.done = True


def frames_of(c, fn):
    """Run fn on the app loop while recording every broadcast frame."""
    seen = []
    obs = seen.append
    main.manager.add_observer(obs)
    try:
        out = c.portal.call(fn)
    finally:
        main.manager.remove_observer(obs)
    return out, seen


def ctx():
    return KNOBS.ctx


# --------------------------------------------------------------------------- #
# submit_turn
# --------------------------------------------------------------------------- #


def test_submit_persists_tags_and_broadcasts_before_the_task(ext_env):
    c, _ = ext_env
    install()
    tid = thread_for(c)
    order = []
    real_create = asyncio.create_task

    async def go():
        def spy(coro, **kw):
            order.append("task")
            return real_create(coro, **kw)
        asyncio.create_task = spy
        try:
            return await ctx().submit_turn(tid, "what is on my calendar", metadata={"voice": True},
                                           instructions=extensions.VoiceInstructions(trailer="[voice chat]"),
                                           thinking="off")
        finally:
            asyncio.create_task = real_create

    def watch(frame):
        if frame.get("type") == "message":
            order.append("message")

    main.manager.add_observer(watch)
    try:
        mid = c.portal.call(go)
    finally:
        main.manager.remove_observer(watch)
    assert order[:2] == ["message", "task"]
    rows = c.get(f"/api/threads/{tid}/messages").json()
    rows = rows["messages"] if isinstance(rows, dict) else rows
    user = next(m for m in rows if m["id"] == mid)
    assert user["content"] == "what is on my calendar" and user["metadata"] == {"voice": True}
    for _ in range(50):
        if c.turns:
            break
        c.portal.call(asyncio.sleep, 0.01)
    (t_id, bot_id, text, opts) = c.turns[0]
    assert (t_id, bot_id, text) == (tid, "main", "what is on my calendar")
    assert opts.user_msg_id == mid and opts.thinking == "off"
    assert opts.voice.trailer == "[voice chat]" and opts.prefetched is None


def test_submit_strips_directives_and_returns_none_for_nothing(ext_env):
    c, _ = ext_env
    install()
    tid = thread_for(c)
    assert c.portal.call(lambda: ctx().submit_turn(tid, "   ")) is None
    assert c.portal.call(lambda: ctx().submit_turn(tid, "[[media:/etc/hosts|x]] :react:happy:")) is None
    mid = c.portal.call(lambda: ctx().submit_turn(tid, "hello [[doc:abc|notes]] there"))
    msg = c.portal.call(lambda: main.db.get_message(mid))
    assert "[[" not in msg.content and msg.content.startswith("hello")


def test_trailer_goes_last_for_gateway_and_never_to_direct_backends(ext_env):
    c, _ = ext_env
    tid = thread_for(c)
    opts = main.TurnOptions(voice=extensions.VoiceInstructions(system="SYS", trailer="[voice chat] short."))
    gw = c.portal.call(lambda: main._compose_agent_text(tid, "hello", opts))
    assert gw == "hello\n\n[voice chat] short."
    direct = c.portal.call(lambda: main._compose_agent_text(tid, "hello", opts, gateway=False))
    assert direct == "hello"
    assert "SYS" not in gw


def test_run_agent_turn_hands_voice_and_prefetched_to_direct_backends(ext_env, monkeypatch):
    c, _ = ext_env
    add_bots()
    calls = []

    async def api_turn(thread_id, bot_id, text, *, voice=None, prefetched=None):
        calls.append(("api", text, voice, prefetched))

    async def adv_turn(thread_id, bot_id, text, *, voice=None, prefetched=None):
        calls.append(("advisor", text, voice, prefetched))

    monkeypatch.setattr(llm_api, "run_api_turn", api_turn)
    monkeypatch.setattr(advisor, "run_turn", adv_turn)
    main._ACCEPTS.clear()
    vi = extensions.VoiceInstructions(system="SPOKEN", trailer="[voice chat]")
    run = FakeRun(None)
    tid_api = thread_for(c, "swift")
    tid_adv = thread_for(c, "sage")
    c.portal.call(lambda: _REAL(tid_api, "swift", "hi", main.TurnOptions(voice=vi, prefetched=run)))
    c.portal.call(lambda: _REAL(tid_adv, "sage", "hi", main.TurnOptions(voice=vi)))
    c.portal.call(lambda: _REAL(tid_adv, "sage", "plain"))
    # The clean transcript (no trailer) and the instructions in their own slot.
    assert calls[0] == ("api", "hi", vi, run)
    assert calls[1] == ("advisor", "hi", vi, None)
    assert calls[2] == ("advisor", "plain", None, None)
    main._ACCEPTS.clear()


#: main.run_agent_turn as defined in the module (the ext_env fixture replaces
#: the attribute with a recorder; this module was imported before that).
_REAL = main.run_agent_turn


def test_direct_backend_without_the_kwargs_still_runs_the_turn(ext_env, monkeypatch, caplog):
    c, _ = ext_env
    add_bots()
    calls = []

    async def old_api_turn(thread_id, bot_id, text):
        calls.append(text)

    monkeypatch.setattr(llm_api, "run_api_turn", old_api_turn)
    main._ACCEPTS.clear()
    tid = thread_for(c, "swift")
    vi = extensions.VoiceInstructions(system="S")
    c.portal.call(lambda: _REAL(tid, "swift", "hi", main.TurnOptions(voice=vi)))
    assert calls == ["hi"]
    assert "takes no voice=" in caplog.text
    main._ACCEPTS.clear()


# --------------------------------------------------------------------------- #
# speculate_turn + adoption
# --------------------------------------------------------------------------- #


def test_speculation_is_direct_only_and_persists_nothing(ext_env, monkeypatch):
    c, _ = ext_env
    add_bots()
    install()
    made = []

    async def spec(thread_id, bot_id, text, *, voice=None):
        rows, _ = await main.db.list_messages(thread_id, limit=1)
        r = FakeRun(rows[-1].id if rows else None)
        made.append((bot_id, text, voice, r))
        return r

    monkeypatch.setattr(llm_api, "speculate", spec, raising=False)
    monkeypatch.setattr(advisor, "speculate", spec, raising=False)
    gw = thread_for(c, "main")
    assert c.portal.call(lambda: ctx().speculate_turn(gw, "hello")) is None
    tid = thread_for(c, "swift")
    before = c.portal.call(lambda: main.db.dump_messages(tid))
    vi = extensions.VoiceInstructions(system="S")
    run1, frames = frames_of(c, lambda: ctx().speculate_turn(tid, "what's the weather", instructions=vi))
    assert isinstance(run1, FakeRun) and made[0][:3] == ("swift", "what's the weather", vi)
    assert [f for f in frames if f.get("thread_id") == tid] == []
    assert c.portal.call(lambda: main.db.dump_messages(tid)) == before
    # A second call discards the first.
    run2 = c.portal.call(lambda: ctx().speculate_turn(tid, "what's the weather like"))
    assert run1.discarded == 1 and run2 is not run1
    assert main._speculations[tid] is run2
    adv = thread_for(c, "sage")
    assert isinstance(c.portal.call(lambda: ctx().speculate_turn(adv, "hi")), FakeRun)


def test_speculation_refused_while_a_turn_holds_the_thread(ext_env, monkeypatch):
    c, _ = ext_env
    add_bots()
    install()

    async def spec(thread_id, bot_id, text, *, voice=None):
        return FakeRun(None)

    monkeypatch.setattr(llm_api, "speculate", spec, raising=False)
    tid = thread_for(c, "swift")

    async def go():
        hold = asyncio.Event()

        async def holder():
            main._turn_holder[tid] = asyncio.current_task()
            await hold.wait()

        t = asyncio.create_task(holder())
        await asyncio.sleep(0)
        out = await ctx().speculate_turn(tid, "hi")
        hold.set()
        await t
        return out

    assert c.portal.call(go) is None


def test_adoption_when_the_head_is_unchanged(ext_env, monkeypatch):
    c, _ = ext_env
    add_bots()
    install()
    tid = thread_for(c, "swift")

    async def spec(thread_id, bot_id, text, *, voice=None):
        rows, _ = await main.db.list_messages(thread_id, limit=1)
        return FakeRun(rows[-1].id if rows else None, text="It is")

    monkeypatch.setattr(llm_api, "speculate", spec, raising=False)
    run = c.portal.call(lambda: ctx().speculate_turn(tid, "weather?"))
    c.portal.call(lambda: ctx().submit_turn(tid, "weather?", metadata={"voice": True}, prefetched=run))
    for _ in range(50):
        if c.turns:
            break
        c.portal.call(asyncio.sleep, 0.01)
    assert c.turns[-1][3].prefetched is run and run.discarded == 0
    assert tid not in main._speculations
    assert main._ext_counters["speculation_adopted"] == 1


def test_stale_head_is_discarded_and_counted(ext_env, monkeypatch):
    c, _ = ext_env
    add_bots()
    install()
    tid = thread_for(c, "swift")

    async def spec(thread_id, bot_id, text, *, voice=None):
        rows, _ = await main.db.list_messages(thread_id, limit=1)
        return FakeRun(rows[-1].id if rows else None)

    monkeypatch.setattr(llm_api, "speculate", spec, raising=False)
    run = c.portal.call(lambda: ctx().speculate_turn(tid, "weather?"))
    # Something landed in the thread after the prompt was built.
    c.portal.call(lambda: main.db.add_message(tid, "assistant", "an unrelated report"))
    c.portal.call(lambda: ctx().submit_turn(tid, "weather?", metadata={"voice": True}, prefetched=run))
    for _ in range(50):
        if c.turns:
            break
        c.portal.call(asyncio.sleep, 0.01)
    assert c.turns[-1][3].prefetched is None and run.discarded == 1
    assert main._ext_counters["speculation_stale"] == 1
    assert ctx().counters["speculation_stale"] == 1


def test_a_run_that_is_not_the_live_one_is_never_adopted(ext_env):
    c, _ = ext_env
    add_bots()
    install()
    tid = thread_for(c, "swift")
    stray = FakeRun(None)
    c.portal.call(lambda: ctx().submit_turn(tid, "hi", metadata={"voice": True}, prefetched=stray))
    for _ in range(50):
        if c.turns:
            break
        c.portal.call(asyncio.sleep, 0.01)
    assert c.turns[-1][3].prefetched is None and stray.discarded == 1


# --------------------------------------------------------------------------- #
# abort_turn
# --------------------------------------------------------------------------- #


def _install_holding_turn(monkeypatch, *, persist_partial: str | None):
    """run_agent_turn double that holds the thread like a real direct turn
    and follows the cancel rule the way T2's backends do."""
    state = {"cancelled": 0}

    async def turn(thread_id, bot_id, text, opts=None):
        main._turn_holder[thread_id] = asyncio.current_task()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            state["cancelled"] += 1
            if persist_partial and llm_api.keep_partial_requested(thread_id):
                row = await main.db.add_message(
                    thread_id, "assistant", persist_partial,
                    metadata={"voice_heard": {"interrupted": True}})
                llm_api.note_partial_row(thread_id, row.id)
            raise

    monkeypatch.setattr(main, "run_agent_turn", turn)
    return state


pytestmark_cancel = pytest.mark.skipif(not hasattr(llm_api, "cancel_direct_turn"),
                                       reason="the direct backends' cancel rule (T2) is not here")


@pytestmark_cancel
def test_abort_keep_partial_returns_the_row_the_backend_persisted(ext_env, monkeypatch):
    c, _ = ext_env
    add_bots()
    install()
    state = _install_holding_turn(monkeypatch, persist_partial="The dentist is at three")
    tid = thread_for(c, "sage")

    async def go():
        await ctx().submit_turn(tid, "when is the dentist", metadata={"voice": True})
        await asyncio.sleep(0.05)
        return await ctx().abort_turn(tid, keep_partial=True)

    row_id = c.portal.call(go)
    assert state["cancelled"] == 1 and row_id
    msg = c.portal.call(lambda: main.db.get_message(row_id))
    assert msg.content == "The dentist is at three"
    assert msg.metadata["voice_heard"] == {"interrupted": True}


@pytestmark_cancel
def test_abort_without_keep_partial_persists_nothing(ext_env, monkeypatch):
    c, _ = ext_env
    add_bots()
    install()
    state = _install_holding_turn(monkeypatch, persist_partial="should not land")
    tid = thread_for(c, "swift")

    async def go():
        await ctx().submit_turn(tid, "x", metadata={"voice": True})
        await asyncio.sleep(0.05)
        out = await ctx().abort_turn(tid, keep_partial=False)
        return out, [m.content for m in await main.db.dump_messages(tid)]

    out, contents = c.portal.call(go)
    assert out is None and state["cancelled"] == 1 and "should not land" not in contents


def test_abort_cancels_queued_turns_and_the_live_speculation(ext_env, monkeypatch):
    c, _ = ext_env
    add_bots()
    install()
    tid = thread_for(c, "swift")
    run = FakeRun(None)
    main._speculations[tid] = run
    started = []

    async def queued(thread_id, bot_id, text, opts=None):
        started.append(text)
        await asyncio.sleep(30)

    monkeypatch.setattr(main, "run_agent_turn", queued)

    async def go():
        await ctx().submit_turn(tid, "one", metadata={"voice": True})
        await ctx().submit_turn(tid, "two", metadata={"voice": True})
        tasks = [t for t, _ in main._ext_turns[tid]]
        out = await ctx().abort_turn(tid)
        await asyncio.sleep(0.01)
        return out, [t.cancelled() for t in tasks]

    out, cancelled = c.portal.call(go)
    assert out is None and all(cancelled)            # nothing held the lock: all queued
    assert run.discarded == 1 and tid not in main._speculations


def test_abort_on_a_gateway_turn_goes_through_the_gateway(ext_env, monkeypatch):
    c, _ = ext_env
    install()
    tid = thread_for(c, "main")
    aborted = []

    async def fake_abort(thread_id):
        aborted.append(thread_id)
        return {"ok": True}

    monkeypatch.setattr(main, "_abort_thread_turn", fake_abort)
    assert c.portal.call(lambda: ctx().abort_turn(tid, keep_partial=False)) is None
    assert aborted == [tid]


def _foreign_holder(c, tid, *, persist_partial: str | None = None):
    """A turn someone TYPED into the thread (not started by the extension):
    holds the thread the way run_agent_turn does."""
    state = {"cancelled": 0}

    async def typed():
        main._turn_holder[tid] = asyncio.current_task()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            state["cancelled"] += 1
            if persist_partial and llm_api.keep_partial_requested(tid):
                row = await main.db.add_message(
                    tid, "assistant", persist_partial,
                    metadata={"voice_heard": {"interrupted": True}})
                llm_api.note_partial_row(tid, row.id)
            raise

    return state, typed


@pytestmark_cancel
def test_interrupt_stops_a_direct_turn_the_extension_did_not_start(ext_env):
    """Review T1 L1: the session speaks a typed turn's reply (frames are per
    thread); "stop" must stop the model too and return the heard row."""
    c, _ = ext_env
    add_bots()
    install()
    tid = thread_for(c, "sage")
    state, typed = _foreign_holder(c, tid, persist_partial="Typed reply so far")

    async def go():
        t = asyncio.create_task(typed())
        await asyncio.sleep(0.05)
        row_id = await ctx().abort_turn(tid, keep_partial=True)
        await asyncio.sleep(0.01)
        return row_id, t.done()

    row_id, done = c.portal.call(go)
    assert state["cancelled"] == 1 and done and row_id
    msg = c.portal.call(lambda: main.db.get_message(row_id))
    assert msg.content == "Typed reply so far"


def test_a_merge_never_touches_a_turn_it_does_not_own(ext_env, monkeypatch):
    c, _ = ext_env
    add_bots()
    install()
    tid = thread_for(c, "swift")
    state, typed = _foreign_holder(c, tid)
    gtid = thread_for(c, "main")
    gstate, gtyped = _foreign_holder(c, gtid)
    aborted = []

    async def fake_abort(thread_id):
        aborted.append(thread_id)
        return {"ok": True}

    monkeypatch.setattr(main, "_abort_thread_turn", fake_abort)

    async def go():
        t = asyncio.create_task(typed())
        g = asyncio.create_task(gtyped())
        await asyncio.sleep(0.05)
        out = (await ctx().abort_turn(tid, keep_partial=False),
               await ctx().abort_turn(gtid, keep_partial=False))
        alive = not t.done() and not g.done()
        counts = (state["cancelled"], gstate["cancelled"])
        t.cancel()
        g.cancel()
        return out, alive, counts

    out, alive, counts = c.portal.call(go)
    assert out == (None, None) and alive and counts == (0, 0)
    assert aborted == []


def test_interrupt_aborts_a_foreign_gateway_turn(ext_env, monkeypatch):
    c, _ = ext_env
    install()
    tid = thread_for(c, "main")
    _state, typed = _foreign_holder(c, tid)
    aborted = []

    async def fake_abort(thread_id):
        aborted.append(thread_id)
        return {"ok": True}

    monkeypatch.setattr(main, "_abort_thread_turn", fake_abort)

    async def go():
        t = asyncio.create_task(typed())
        await asyncio.sleep(0.05)
        out = await ctx().abort_turn(tid, keep_partial=True)
        t.cancel()
        return out

    assert c.portal.call(go) is None and aborted == [tid]


def test_gateway_abort_answers_within_its_bound(ext_env, monkeypatch):
    """Review T1 L6: a gateway turn slow to wind down costs the caller at most
    _ABORT_GATEWAY_SETTLE_S, not 5 s."""
    import time
    c, _ = ext_env
    install()
    tid = thread_for(c, "main")

    async def stubborn(thread_id, bot_id, text, opts=None):
        main._turn_holder[thread_id] = asyncio.current_task()
        await asyncio.sleep(10)

    async def fake_abort(thread_id):
        return {"ok": True}

    monkeypatch.setattr(main, "run_agent_turn", stubborn)
    monkeypatch.setattr(main, "_abort_thread_turn", fake_abort)

    async def go():
        await ctx().submit_turn(tid, "x", metadata={"voice": True})
        await asyncio.sleep(0.05)
        t0 = time.monotonic()
        out = await ctx().abort_turn(tid, keep_partial=True)
        took = time.monotonic() - t0
        for t, _ in list(main._ext_turns.get(tid, [])):
            t.cancel()
        return out, took

    out, took = c.portal.call(go)
    assert out is None and took < main._ABORT_GATEWAY_SETTLE_S + 0.5


# --------------------------------------------------------------------------- #
# retract_message / replies_after
# --------------------------------------------------------------------------- #


def test_retract_only_touches_rows_carrying_the_key(ext_env):
    c, _ = ext_env
    install()
    tid = thread_for(c)

    async def go():
        typed = await main.db.add_message(tid, "user", "typed")
        spoken = await main.db.add_message(tid, "user", "spoken words", metadata={"voice": True})
        await main.db.set_title_if_empty(tid, "spoken words")
        for mid in (typed.id, spoken.id):
            await ctx().retract_message(tid, mid)
        thread = await main.db.get_thread(tid)
        return [m.content for m in await main.db.dump_messages(tid)], thread.title

    left, title = c.portal.call(go)
    assert left == ["typed"] and not title


def test_replies_after_skips_sub_rows_and_foreign_ids(ext_env):
    c, _ = ext_env
    install()
    tid = thread_for(c)
    other = thread_for(c)

    async def go():
        u = await main.db.add_message(tid, "user", "q")
        await main.db.add_message(tid, "assistant", "working…", metadata={"sub": True})
        a = await main.db.add_message(tid, "assistant", "answer")
        o = await main.db.add_message(other, "user", "elsewhere")
        return (await ctx().replies_after(tid, u.id), a.id,
                await ctx().replies_after(tid, o.id), await ctx().replies_after(tid, "nope"))

    replies, aid, foreign, unknown = c.portal.call(go)
    assert replies == [(aid, "answer")] and foreign is None and unknown is None


# --------------------------------------------------------------------------- #
# note_heard
# --------------------------------------------------------------------------- #


def test_note_heard_merges_caps_and_broadcasts_message_update(ext_env):
    c, _ = ext_env
    install()
    tid = thread_for(c)
    content = "The dentist is at three on Tuesday. Bring the insurance card."

    async def setup():
        u = await main.db.add_message(tid, "user", "q")
        a = await main.db.add_message(tid, "assistant", content,
                                      metadata={"voice_heard": {"interrupted": True}, "model": "m"})
        return u.id, a.id

    uid, aid = c.portal.call(setup)
    ok, frames = frames_of(c, lambda: ctx().note_heard(
        tid, aid, heard_upto=36, heard_text="h" * 300, unheard_text="u" * 300))
    assert ok is True
    msg = c.portal.call(lambda: main.db.get_message(aid))
    vh = msg.metadata["voice_heard"]
    assert vh == {"interrupted": True, "upto": 36, "heard": "h" * 160, "unheard": "u" * 240}
    assert msg.metadata["model"] == "m"
    upd = [f for f in frames if f["type"] == "message_update"]
    assert upd and upd[0]["message_id"] == aid and upd[0]["thread_id"] == tid
    assert upd[0]["message"]["metadata"]["voice_heard"]["upto"] == 36
    # Clamped to the content; refused for unknown, provisional and user rows.
    assert c.portal.call(lambda: ctx().note_heard(tid, aid, heard_upto=10_000, heard_text="",
                                                  unheard_text="")) is True
    assert c.portal.call(lambda: main.db.get_message(aid)).metadata["voice_heard"]["upto"] == len(content)
    for bad in ("nope", "run:advisor-abc", uid):
        assert c.portal.call(lambda bad=bad: ctx().note_heard(
            tid, bad, heard_upto=1, heard_text="", unheard_text="")) is False


# --------------------------------------------------------------------------- #
# subscribe_frames
# --------------------------------------------------------------------------- #


def _sub(c, tid, token=None):
    got = []
    unsub = c.portal.call(lambda: ctx().subscribe_frames(tid, got.append, token=token))
    return got, unsub


def _settle(c):
    c.portal.call(asyncio.sleep, 0.02)


def test_frames_for_one_thread_in_order_and_never_inside_broadcast(ext_env):
    c, _ = ext_env
    install()
    tid = thread_for(c)
    other = thread_for(c)
    inside = {"now": False, "violations": 0}
    got = []

    def on_frame(f):
        if inside["now"]:
            inside["violations"] += 1
        got.append(f)

    unsub = c.portal.call(lambda: ctx().subscribe_frames(tid, on_frame, token=None))

    async def go():
        for i in range(5):
            inside["now"] = True
            await main.manager.broadcast({"type": "stream_chunk", "thread_id": tid, "bot_id": "main",
                                          "message_id": "m1", "text": str(i)})
            inside["now"] = False
        await main.manager.broadcast({"type": "progress", "thread_id": tid,
                                      "item": {"kind": "tool", "name": "brave_search"}})
        await main.manager.broadcast({"type": "message", "thread_id": other, "bot_id": "main",
                                      "message": {"id": "x"}})
        await asyncio.sleep(0.02)

    c.portal.call(go)
    assert [f.get("text") for f in got[:5]] == ["0", "1", "2", "3", "4"]
    assert got[5]["type"] == "progress" and got[5]["item"]["kind"] == "tool"
    assert all(f["thread_id"] == tid for f in got) and inside["violations"] == 0
    c.portal.call(unsub)
    c.portal.call(lambda: main.manager.broadcast({"type": "thinking", "thread_id": tid,
                                                  "bot_id": "main", "status": "started"}))
    _settle(c)
    assert len(got) == 6


def test_a_late_subscriber_gets_a_snapshot_of_the_open_stream(ext_env):
    c, _ = ext_env
    install()
    tid = thread_for(c)
    full = "The dentist is at three on Tuesday. Bring the insurance card."

    async def stream_part():
        await main.manager.broadcast({"type": "stream_start", "thread_id": tid, "bot_id": "main",
                                      "message_id": "run:advisor-1"})
        for i in range(0, 40, 10):
            await main.manager.broadcast({"type": "stream_chunk", "thread_id": tid, "bot_id": "main",
                                          "message_id": "run:advisor-1", "text": full[i:i + 10]})

    c.portal.call(stream_part)
    got, unsub = _sub(c, tid)

    async def stream_rest():
        await main.manager.broadcast({"type": "stream_chunk", "thread_id": tid, "bot_id": "main",
                                      "message_id": "run:advisor-1", "text": full[40:]})
        await main.manager.broadcast({"type": "stream_done", "thread_id": tid, "bot_id": "main",
                                      "message_id": "m9", "provisional_id": "run:advisor-1",
                                      "message": {"id": "m9", "role": "assistant", "content": full}})
        await asyncio.sleep(0.02)

    c.portal.call(stream_rest)
    assert got[0] == {"type": "stream_start", "thread_id": tid, "bot_id": "main",
                      "message_id": "run:advisor-1"}
    assert got[1] == {"type": "stream_chunk", "thread_id": tid, "bot_id": "main",
                      "message_id": "run:advisor-1", "text": full[:40], "replace": True}
    assert got[2]["text"] == full[40:] and "replace" not in got[2]
    assert got[1]["text"] + got[2]["text"] == full
    assert got[3]["type"] == "stream_done"
    # Closed: a later subscriber gets no snapshot for it.
    got2, unsub2 = _sub(c, tid)
    _settle(c)
    assert got2 == []
    c.portal.call(unsub)
    c.portal.call(unsub2)


def test_queue_is_bounded_and_drops_the_oldest(ext_env, caplog):
    c, _ = ext_env
    install()
    tid = thread_for(c)
    got = []
    unsub = c.portal.call(lambda: ctx().subscribe_frames(tid, got.append, token=None))

    async def flood():
        # Synchronous puts (as from many broadcasts before the pump runs).
        for i in range(300):
            extensions._observe({"type": "stream_chunk", "thread_id": tid, "bot_id": "main",
                                 "message_id": "m", "text": str(i)})
        await asyncio.sleep(0.05)

    c.portal.call(flood)
    assert len(got) == extensions.FRAME_QUEUE_MAX
    assert got[0]["text"] == str(300 - extensions.FRAME_QUEUE_MAX) and got[-1]["text"] == "299"
    assert "frame queue for thread" in caplog.text
    c.portal.call(unsub)


def test_frames_are_redacted_the_instant_the_session_lapses(ext_env):
    c, _ = ext_env
    install()
    tid = thread_for(c, "main")                      # a non-safe bot
    unlock(c)
    token = next(iter(auth._sessions))
    got, unsub = _sub(c, tid, token=token)

    def chunk(text):
        return {"type": "stream_chunk", "thread_id": tid, "bot_id": "main",
                "message_id": "m1", "text": text}

    c.portal.call(lambda: main.manager.broadcast(chunk("visible")))
    _settle(c)
    auth._sessions.clear()                           # the session lapses
    c.portal.call(lambda: main.manager.broadcast(chunk("secret")))
    _settle(c)
    assert [f["text"] for f in got] == ["visible"]    # a non-safe bot's frame is dropped
    c.portal.call(unsub)
