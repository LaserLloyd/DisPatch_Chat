"""The voice retrieval budget (C-§B.2 `EmbedConfig.voice_timeout_s`, feasibility C1).

The advisor's query embedding comes from an embedder on the rig that unloads
after ten idle minutes and takes ~1.7 s to load again. A typed turn can wait
for it (``timeout_s``, 2 s); a voice turn gives it 0.3 s and then searches by
keywords only. These tests run against a real loopback HTTP embedder with the
stub delays the harness uses (0 / 1.7 / 2.5 s) and measure wall time.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from voice_stub_llm import StubLLM

from app import advisor, config, knowledge, llm_api

#: Scheduling slack on top of the 0.3 s budget (a loaded CI box).
SLACK_S = 0.2


def _corpus(tmp_path: Path) -> Path:
    root = tmp_path / "kb"
    (root / "research").mkdir(parents=True)
    (root / "_profile.md").write_text("Sam likes hiking and sourdough.\n")
    (root / "research" / "2026-10-01-solar.md").write_text(
        "---\ntitle: Rooftop solar\ncreated: 2026-10-01\n---\n\n"
        "Rooftop solar panels pay back in about eight years in this climate.\n")
    (root / "notes.md").write_text("# Bread\n\nSourdough starter needs feeding daily.\n")
    return root


def _index(tmp_path: Path, base_url: str) -> knowledge.KnowledgeIndex:
    idx = knowledge.KnowledgeIndex(_corpus(tmp_path), tmp_path / "k.db",
                                   knowledge.EmbedConfig(base_url, "emb", timeout_s=2.0))
    idx.scan_sync()
    return idx


def test_the_contract_numbers():
    cfg = knowledge.EmbedConfig("http://x/v1", "emb")
    assert (cfg.timeout_s, cfg.voice_timeout_s) == (2.0, 0.3)
    assert knowledge.VOICE_TIMEOUT_S == 0.3


@pytest.mark.parametrize("delay", [1.7, 2.5])
def test_a_cold_embedder_costs_a_voice_turn_at_most_the_budget(tmp_path, delay):
    with StubLLM(embed_delay_s=delay) as stub:
        idx = _index(tmp_path, stub.base_url)

        async def go():
            t0 = time.monotonic()
            hits = await idx.search("how long until solar pays back?", voice=True)
            return hits, time.monotonic() - t0

        hits, took = asyncio.run(go())
        assert took < 0.3 + SLACK_S, f"voice search took {took:.2f}s"
        # Keywords still answer.
        assert hits and hits[0].title == "Rooftop solar"
        assert "voice budget" in idx.last_error
        assert len(stub.embed_requests()) == 1


def test_a_typed_turn_still_waits_for_the_embedder(tmp_path):
    with StubLLM(embed_delay_s=1.7) as stub:
        idx = _index(tmp_path, stub.base_url)
        seen = {}
        real = idx._vector_ids

        def spy(qvec, k):
            seen["qvec"] = qvec
            return real(qvec, k)

        idx._vector_ids = spy                     # type: ignore[method-assign]

        async def go():
            t0 = time.monotonic()
            await idx.search("solar payback")
            return time.monotonic() - t0

        took = asyncio.run(go())
        assert took >= 1.6
        assert seen.get("qvec"), "the typed turn used the vector"


def test_a_warm_embedder_is_used_on_a_voice_turn(tmp_path):
    with StubLLM(embed_delay_s=0.0) as stub:
        idx = _index(tmp_path, stub.base_url)
        seen = {}
        real = idx._vector_ids

        def spy(qvec, k):
            seen["qvec"] = qvec
            return real(qvec, k)

        idx._vector_ids = spy                     # type: ignore[method-assign]
        asyncio.run(idx.search("solar payback", voice=True))
        assert seen.get("qvec"), "a fast embedder is not skipped on voice turns"


def test_a_warming_embedder_is_not_asked_twice(tmp_path):
    """The over-budget request carries on (it is what warms the embedder);
    the next voice turn does not queue a second one behind it."""
    with StubLLM(embed_delay_s=1.0) as stub:
        idx = _index(tmp_path, stub.base_url)

        async def go():
            await idx.search("solar", voice=True)
            t0 = time.monotonic()
            await idx.search("sourdough", voice=True)
            second = time.monotonic() - t0
            assert idx._warming is not None
            await asyncio.wait({idx._warming}, timeout=3)   # let it finish cleanly
            return second

        second = asyncio.run(go())
        assert second < 0.1
        assert len(stub.embed_requests()) == 1


def test_a_failing_embedder_degrades_to_keywords_on_voice(tmp_path, monkeypatch):
    idx = knowledge.KnowledgeIndex(_corpus(tmp_path), tmp_path / "k.db",
                                   knowledge.EmbedConfig("http://x/v1", "emb"))
    idx.scan_sync()

    async def boom(texts, timeout):
        raise RuntimeError("embeddings HTTP 400: bad model")

    monkeypatch.setattr(idx, "_embed_texts", boom)
    hits = asyncio.run(idx.search("solar payback", voice=True))
    assert hits and hits[0].title == "Rooftop solar"
    assert "bad model" in idx.last_error


def test_the_advisor_reads_a_voice_budget_from_config():
    cfg = advisor._embed_config({"base_url": "http://x/v1", "model": "e",
                                 "voice_timeout_s": 0.5})
    assert cfg is not None and cfg.voice_timeout_s == 0.5
    assert advisor._embed_config({"base_url": "http://x/v1", "model": "e"}).voice_timeout_s == 0.3


# --------------------------------------------------------------------------- #
# The whole advisor turn: the provider call starts within the budget
# --------------------------------------------------------------------------- #


@pytest.fixture
def stub_advisor(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.yaml")
    config._invalidate_bots_cache()
    monkeypatch.setattr(advisor, "_ledger", None)
    advisor._indexes.clear()
    old = advisor._hooks
    with StubLLM(delay_s=0.0, words_per_s=400, embed_delay_s=1.7) as stub:
        config.upsert_bot(config.Bot(id="adv", name="Sage", advisor={
            "owner": "Sam", "knowledge_dir": str(_corpus(tmp_path)),
            "providers": [{"provider": "custom", "base_url": stub.base_url, "model": "m"}],
            "embeddings": {"base_url": stub.base_url, "model": "emb"}}))

        async def noop(*a, **kw):
            return None

        async def deliver(thread_id, text, **kw):
            return SimpleNamespace(id="row1", content=text)

        async def list_messages(tid, limit):
            return ([SimpleNamespace(id="u1", role="user", content="Is solar worth it?",
                                     metadata=None)], False)

        advisor.bind(advisor.Hooks(
            list_messages=list_messages, deliver=deliver, set_status=noop,
            broadcast=noop, thread_update=noop, open_stream=lambda t, p: None,
            close_stream=lambda t, p: True, sanitize_delta=lambda s: s,
            ask_agent=noop, threads_since=noop, data_dir=tmp_path))
        yield stub
    advisor._hooks = old
    advisor._indexes.clear()


def _time_to_provider(stub, *, voice) -> float:
    async def go():
        t0 = time.monotonic()
        await advisor.run_turn("t1", "adv", "Is solar worth it?", voice=voice)
        return t0

    t0 = asyncio.run(go())
    chat = stub.chat_requests()[-1]
    return chat["t"] - t0


def test_a_voice_turn_reaches_the_provider_within_the_budget(stub_advisor):
    stub = stub_advisor
    # Warm the index (first scan) outside the measurement.
    asyncio.run(advisor.index_for(advisor.AdvisorConfig.from_bot(config.get_bot("adv"))).scan())
    voice = SimpleNamespace(system="## Spoken replies\nShort.", trailer="")
    took = _time_to_provider(stub, voice=voice)
    assert took < 0.3 + SLACK_S + 0.15, f"voice turn reached the provider after {took:.2f}s"


def test_a_typed_turn_with_the_same_embedder_waits_for_it(stub_advisor):
    stub = stub_advisor
    asyncio.run(advisor.index_for(advisor.AdvisorConfig.from_bot(config.get_bot("adv"))).scan())
    took = _time_to_provider(stub, voice=None)
    assert took >= 1.6


def test_llm_api_voice_helpers_read_by_attribute():
    assert llm_api.voice_system_text(None) == ""
    assert llm_api.voice_system_text(SimpleNamespace(system="  x  ", trailer="t")) == "x"
    assert llm_api.voice_system_text(SimpleNamespace(trailer="t")) == ""
