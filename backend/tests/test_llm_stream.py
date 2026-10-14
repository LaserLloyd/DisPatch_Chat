"""Real streaming + one retry for the direct-API ("Connect an AI") path.

`llm_api.stream_chat` is shared by `api:` bots and the advisor. Everything
goes through an ``httpx.MockTransport`` (via the module's `http_client`
factory) — no network.

Run: cd backend && uv run pytest tests/test_llm_stream.py
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app import config, llm_api, main
from tests.test_llm_api import _api_bot, _mock_http, db_env


def _sse(*pieces: str, model: str = "stream-model", usage: int | None = None) -> bytes:
    out = []
    for p in pieces:
        out.append("data: " + json.dumps(
            {"model": model, "choices": [{"delta": {"content": p}}]}) + "\n\n")
    tail: dict = {"model": model, "choices": [{"delta": {}, "finish_reason": "stop"}]}
    if usage is not None:
        tail["usage"] = {"total_tokens": usage}
    out.append("data: " + json.dumps(tail) + "\n\ndata: [DONE]\n\n")
    return "".join(out).encode()


def _sse_response(body: bytes) -> httpx.Response:
    return httpx.Response(200, headers={"content-type": "text/event-stream"},
                          content=body)


class _BreaksAfterFirstChunk(httpx.AsyncByteStream):
    """An SSE body that delivers one token and then drops the connection."""

    async def __aiter__(self):
        yield ("data: " + json.dumps({"choices": [{"delta": {"content": "Hel"}}]})
               + "\n\n").encode()
        raise httpx.ReadError("connection reset")


def _cfg(provider: str = "custom", **api) -> llm_api.Resolved:
    block = {"provider": provider, "base_url": "http://fake.test/v1",
             "model": "m", "api_key": "k", **api}
    return llm_api.resolve_api(block, "Tester")


@pytest.fixture
def no_sleep(monkeypatch):
    slept: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(s, *a, **kw):
        slept.append(s)
        await real_sleep(0)

    monkeypatch.setattr(llm_api.asyncio, "sleep", fake_sleep)
    return slept


def _stream(cfg, **kw):
    seen: list[str] = []

    async def on_text(raw):
        seen.append(raw)

    async def go():
        return await llm_api.stream_chat(
            cfg, [{"role": "system", "content": "s"},
                  {"role": "user", "content": "hi"}], on_text, **kw)
    return asyncio.run(go()), seen


# --------------------------------------------------------------------------- #
# Streaming
# --------------------------------------------------------------------------- #


def test_deltas_arrive_cumulatively_and_the_body_asks_for_a_stream(monkeypatch):
    bodies = []

    def handler(req):
        bodies.append(json.loads(req.content))
        return _sse_response(_sse("Hel", "lo ", "there", usage=17))

    _mock_http(monkeypatch, handler)
    res, seen = _stream(_cfg())
    assert seen == ["Hel", "Hello ", "Hello there"]
    assert (res.text, res.model, res.tokens, res.finish_reason, res.streamed) == (
        "Hello there", "stream-model", 17, "stop", True)
    assert bodies[0]["stream"] is True and bodies[0]["model"] == "m"


def test_a_server_that_ignores_stream_still_answers(monkeypatch):
    _mock_http(monkeypatch, lambda req: httpx.Response(200, json={
        "model": "whole", "choices": [{"message": {"content": "All at once."}}]}))
    res, seen = _stream(_cfg())
    assert (res.text, res.model, res.streamed) == ("All at once.", "whole", False)
    assert seen == []


def test_preset_extra_body_is_sent_and_the_bots_own_wins(monkeypatch):
    bodies = []
    _mock_http(monkeypatch, lambda req: (bodies.append(json.loads(req.content))
                                         or _sse_response(_sse("ok"))))
    _stream(_cfg("minimax"))
    assert bodies[0]["thinking"] == {"type": "disabled"}
    assert bodies[0]["reasoning_split"] is True
    _stream(_cfg("minimax"), extra_body={"reasoning_split": False, "x": 1})
    assert bodies[1]["reasoning_split"] is False and bodies[1]["x"] == 1
    assert bodies[1]["thinking"] == {"type": "disabled"}


def test_the_minimax_preset_is_a_plain_preset():
    p = llm_api.PROVIDER_BY_ID["minimax"]
    assert p.kind == "openai" and p.key_required and p.models[0] == "MiniMax-M3"
    # The public table (the setup panel) gets no new field.
    pub = next(x for x in llm_api.providers_public() if x["id"] == "minimax")
    assert "extra_body" not in pub


# --------------------------------------------------------------------------- #
# One retry, never after the first token
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("status", [429, 502, 503, 504])
def test_a_transient_status_is_retried_once(monkeypatch, no_sleep, status):
    calls = []

    def handler(req):
        calls.append(req)
        if len(calls) == 1:
            return httpx.Response(status, headers={"retry-after": "2"},
                                  json={"error": {"message": "busy"}})
        return _sse_response(_sse("second ", "time"))

    _mock_http(monkeypatch, handler)
    res, _ = _stream(_cfg())
    assert res.text == "second time" and len(calls) == 2
    assert no_sleep == [2.0], "Retry-After is honoured"


def test_retry_without_retry_after_uses_the_short_default(monkeypatch, no_sleep):
    calls = []
    _mock_http(monkeypatch, lambda req: (calls.append(1) or (
        httpx.Response(503, text="overloaded") if len(calls) == 1
        else _sse_response(_sse("ok")))))
    _stream(_cfg())
    assert no_sleep == [llm_api.RETRY_DEFAULT_DELAY_S]


def test_only_one_retry(monkeypatch, no_sleep):
    calls = []
    _mock_http(monkeypatch, lambda req: (calls.append(1)
                                         or httpx.Response(429, json={"error": "slow down"})))
    with pytest.raises(llm_api.StreamFailed) as e:
        _stream(_cfg())
    assert len(calls) == 2
    assert "rate-limiting" in e.value.error.message and e.value.started is False


def test_a_long_retry_after_is_honoured_by_not_retrying(monkeypatch, no_sleep):
    calls = []
    _mock_http(monkeypatch, lambda req: (calls.append(1) or httpx.Response(
        429, headers={"retry-after": "60"}, json={"error": "later"})))
    with pytest.raises(llm_api.StreamFailed):
        _stream(_cfg())
    assert len(calls) == 1 and no_sleep == []


@pytest.mark.parametrize("status", [400, 401, 404, 500])
def test_other_statuses_are_not_retried(monkeypatch, no_sleep, status):
    calls = []
    _mock_http(monkeypatch, lambda req: (calls.append(1)
                                         or httpx.Response(status, json={"error": "no"})))
    with pytest.raises(llm_api.StreamFailed):
        _stream(_cfg())
    assert len(calls) == 1


def test_no_retry_after_the_first_token(monkeypatch, no_sleep):
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              stream=_BreaksAfterFirstChunk())

    _mock_http(monkeypatch, handler)
    seen: list[str] = []

    async def on_text(raw):
        seen.append(raw)

    async def go():
        await llm_api.stream_chat(_cfg(), [{"role": "user", "content": "hi"}], on_text)

    with pytest.raises(llm_api.StreamFailed) as e:
        asyncio.run(go())
    assert len(calls) == 1 and seen == ["Hel"]
    assert e.value.started is True and no_sleep == []


def test_the_advisor_uses_the_shared_stream():
    from app import advisor
    assert advisor._stream_openai is llm_api.stream_openai
    assert advisor.StreamFailed is llm_api.StreamFailed
    assert issubclass(advisor._Emitter, llm_api.StreamEmitter)


# --------------------------------------------------------------------------- #
# A whole api: turn streams for real through main.py's bubble protocol
# --------------------------------------------------------------------------- #


def test_api_turn_streams_into_a_bubble_that_the_row_claims(db_env, monkeypatch):
    db = db_env
    _api_bot()
    frames: list[dict] = []

    async def capture(frame):
        frames.append(frame)

    monkeypatch.setattr(main.manager, "broadcast", capture)
    monkeypatch.setattr(llm_api, "STREAM_EMIT_INTERVAL_S", 0.0)
    _mock_http(monkeypatch, lambda req: _sse_response(
        _sse("Streaming ", "for ", "real.", model="live-model", usage=9)))

    async def go():
        thread = await db.create_thread("llm-custom")
        await db.add_message(thread.id, "user", "go")
        await llm_api.run_api_turn(thread.id, "llm-custom", "go")
        return (await db.list_messages(thread.id, 50))[0], await db.get_thread(thread.id)

    msgs, thread = asyncio.run(go())
    reply = [m for m in msgs if m.role == "assistant"]
    assert len(reply) == 1 and reply[0].content == "Streaming for real."
    assert reply[0].metadata["model"] == "live-model"
    assert reply[0].metadata["tokens"] == 9
    kinds = [f["type"] for f in frames]
    start = next(f for f in frames if f["type"] == "stream_start")
    chunks = [f["text"] for f in frames if f["type"] == "stream_chunk"
              and f["message_id"] == start["message_id"]]
    assert "".join(chunks) == "Streaming for real."
    assert len(chunks) >= 2, "deltas went out as they arrived"
    done = [f for f in frames if f["type"] == "stream_done"]
    assert len(done) == 1 and done[0]["provisional_id"] == start["message_id"]
    assert done[0]["message"]["content"] == "Streaming for real."
    assert kinds.index("stream_start") < kinds.index("stream_done")
    assert thread.status == "idle"


def test_api_turn_that_fails_mid_stream_retires_its_bubble(db_env, monkeypatch):
    db = db_env
    _api_bot()
    frames: list[dict] = []

    async def capture(frame):
        frames.append(frame)

    monkeypatch.setattr(main.manager, "broadcast", capture)
    _mock_http(monkeypatch, lambda req: httpx.Response(
        200, headers={"content-type": "text/event-stream"},
        stream=_BreaksAfterFirstChunk()))

    async def go():
        thread = await db.create_thread("llm-custom")
        await db.add_message(thread.id, "user", "go")
        await llm_api.run_api_turn(thread.id, "llm-custom", "go")
        return (await db.list_messages(thread.id, 50))[0], await db.get_thread(thread.id)

    msgs, thread = asyncio.run(go())
    assert [m for m in msgs if m.role == "assistant"] == []
    assert thread.status == "error"
    start = next(f for f in frames if f["type"] == "stream_start")
    done = [f for f in frames if f["type"] == "stream_done"]
    assert len(done) == 1 and done[0]["message"] is None
    assert done[0]["provisional_id"] == start["message_id"]
    assert any(f["type"] == "error" for f in frames)
    assert main._provisional_runs.get(start["thread_id"]) is None
    config._invalidate_bots_cache()


# --------------------------------------------------------------------------- #
# Tool calls (advisor live tools, 2026-10-13)
# --------------------------------------------------------------------------- #


def test_tool_call_fragments_merge_into_whole_calls():
    whole = [{"index": 0, "id": "a", "type": "function",
              "function": {"name": "weather", "arguments": '{"location":"Tokyo"}'}}]
    assert llm_api.merge_tool_call_deltas(whole) == [
        {"id": "a", "name": "weather", "arguments": '{"location":"Tokyo"}'}]
    # llama.cpp style: the arguments arrive a few characters at a time.
    frags = [{"index": 0, "id": "b", "function": {"name": "weather", "arguments": ""}},
             {"index": 0, "function": {"arguments": '{"loc'}},
             {"index": 0, "function": {"arguments": 'ation":"Tokyo"}'}},
             {"index": 1, "id": "c", "function": {"name": "calculator",
                                                  "arguments": '{"expression":"1+1"}'}}]
    assert llm_api.merge_tool_call_deltas(frags) == [
        {"id": "b", "name": "weather", "arguments": '{"location":"Tokyo"}'},
        {"id": "c", "name": "calculator", "arguments": '{"expression":"1+1"}'}]
    # No index at all: a new id starts a call, an id-less fragment continues it.
    noidx = [{"id": "x", "function": {"name": "weather", "arguments": '{"a"'}},
             {"function": {"arguments": ':1}'}},
             {"id": "y", "function": {"name": "clock", "arguments": "{}"}}]
    assert [c["arguments"] for c in llm_api.merge_tool_call_deltas(noidx)] == ['{"a":1}', "{}"]
    assert llm_api.merge_tool_call_deltas([{"function": {"arguments": "{}"}}])[0]["id"] == "call_0"


def test_a_streamed_tool_call_comes_back_on_the_result(monkeypatch):
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        chunks = [
            {"choices": [{"delta": {"content": "</think>\n\n", "role": "assistant",
                                    "tool_calls": [{"id": "c1", "type": "function", "index": 0,
                                                    "function": {"name": "weather",
                                                                 "arguments": '{"location":"Tokyo"}'}}]}}]},
            {"choices": [{"finish_reason": "tool_calls", "index": 0, "delta": {"role": "assistant"}}]},
        ]
        body = "".join("data: " + json.dumps(c) + "\n\n" for c in chunks) + "data: [DONE]\n\n"
        return _sse_response(body.encode())
    _mock_http(monkeypatch, handler)
    schemas = [{"type": "function", "function": {"name": "weather", "parameters": {}}}]
    res = asyncio.run(llm_api.stream_chat(_cfg(), [{"role": "user", "content": "hi"}],
                                          tools=schemas))
    assert res.tool_calls == [{"id": "c1", "name": "weather", "arguments": '{"location":"Tokyo"}'}]
    assert res.text == "</think>\n\n" and res.finish_reason == "tool_calls"
    assert bodies[0]["tools"] == schemas and bodies[0]["tool_choice"] == "auto"
    # Without tools the body carries neither key — the old request unchanged.
    asyncio.run(llm_api.stream_chat(_cfg(), [{"role": "user", "content": "hi"}]))
    assert "tools" not in bodies[1] and "tool_choice" not in bodies[1]


def test_a_non_streamed_reply_with_tool_calls_is_read_too(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {
            "role": "assistant", "content": None,
            "tool_calls": [{"id": "z", "type": "function",
                            "function": {"name": "calculator",
                                         "arguments": {"expression": "2*3"}}}]},
            "finish_reason": "tool_calls"}]})
    _mock_http(monkeypatch, handler)
    res = asyncio.run(llm_api.stream_chat(_cfg(), [{"role": "user", "content": "hi"}],
                                          tools=[{"type": "function",
                                                  "function": {"name": "calculator"}}]))
    assert res.streamed is False
    assert res.tool_calls == [{"id": "z", "name": "calculator",
                               "arguments": '{"expression": "2*3"}'}]
