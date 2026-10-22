"""Privacy matrix, advisor corpus: the household advisor's corpus belongs to ``corpus_profile``
(default: the protected Admin). A User's turn on that bot - through ``run_turn`` AND ``speculate`` -
must carry no corpus sentinel in the request the provider receives; the Admin's own turn must."""
from __future__ import annotations

import asyncio

import pytest
from privacy_fixtures import HOUSEHOLD_ADVISOR, PRIVACY_MARKS, build_install

from app import advisor

pytestmark = PRIVACY_MARKS


@pytest.fixture
def captured(monkeypatch):
    seen: list[list[dict]] = []

    async def fake_stream(res, entry, messages, on_text):
        seen.append(messages)
        await on_text("fine.")
        return "fine."
    monkeypatch.setattr(advisor, "_stream_openai", fake_stream)
    advisor._indexes.clear()
    return seen


def _thread(inst, owner):
    db = inst.world.db

    async def go():
        t = await db.create_thread(HOUSEHOLD_ADVISOR, title="adv", owner_profile_id=inst.pids[owner])
        await db.add_message(t.id, "user", "What do you know about rooftop solar?")
        return t.id
    return inst.run(go)


def _prompt(messages) -> str:
    return "\n".join(str(m.get("content")) for m in messages)


def _run(inst, coro_fn):
    inst.run(coro_fn)


@pytest.mark.parametrize("owner,expect", [("U1", False), ("B1", False), ("D", False), ("A2", False), ("A0", True)])
def test_run_turn_corpus(world, monkeypatch, captured, owner, expect):
    inst = build_install(world, monkeypatch, "A0")
    corpus = inst.sent("corpus", "A0")
    tid = _thread(inst, owner)

    async def go():
        await advisor.run_turn(tid, HOUSEHOLD_ADVISOR, "What do you know about rooftop solar?")
        while advisor._tasks:
            await asyncio.gather(*list(advisor._tasks))
    _run(inst, go)
    assert captured, "the provider was never called (harness is vacuous)"
    assert (corpus in _prompt(captured[-1])) is expect, \
        f"{owner}'s run_turn prompt {'lacks' if expect else 'contains'} the Admin's corpus"


@pytest.mark.parametrize("owner,expect", [("U1", False), ("B1", False), ("D", False), ("A0", True)])
def test_speculate_corpus(world, monkeypatch, captured, owner, expect):
    inst = build_install(world, monkeypatch, "A0")
    corpus = inst.sent("corpus", "A0")
    tid = _thread(inst, owner)

    async def go():
        run = await advisor.speculate(tid, HOUSEHOLD_ADVISOR, "What do you know about rooftop solar?")
        await asyncio.wait({run._task}, timeout=5)
        return run
    run = inst.run(go)
    assert captured, "the provider was never called (harness is vacuous)"
    assert (corpus in _prompt(captured[-1])) is expect, \
        f"{owner}'s speculate prompt {'lacks' if expect else 'contains'} the Admin's corpus"
    assert (corpus in _prompt(run.prompt.messages)) is expect


def test_advisor_status_shows_nobody_elses_requests(world, monkeypatch):
    inst = build_install(world, monkeypatch, "A0")
    for actor in ("U1", "U2", "B1", "A2"):
        r = inst.client(actor).get("/api/advisor/status")
        for owner in ("A0", "U1", "B1", "D"):
            if owner != actor:
                assert inst.sent("advisor_request", owner) not in r.text, f"{actor} sees {owner}'s advisor brief"
