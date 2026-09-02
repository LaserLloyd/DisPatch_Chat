"""The ClawForge client itself: one MCP session reused, re-initialised only
when the rig forgets it, and a breaker that makes a down rig cheap.

The transport is `httpx.MockTransport`, so the request sequence the client
actually puts on the wire is what is asserted — the point of these tests is
the number and shape of round trips, which a FakeForge cannot see.
"""
import asyncio
import json

import httpx
import pytest

from app import image_jobs


class Rig:
    """A scripted MCP image server. Counts handshakes, can vanish."""

    def __init__(self):
        self.sessions: list[str] = []
        self.requests: list[str] = []
        self.down = False
        self.forget = False  # answer tools/call with 404 once, then recover

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("All connection attempts failed", request=request)
        if request.method == "GET":
            return httpx.Response(200, content=b"\x89PNG\r\n\x1a\n" + b"\0" * 20000)
        body = json.loads(request.content)
        method = body.get("method")
        self.requests.append(method)
        if method == "initialize":
            sid = f"sid-{len(self.sessions) + 1}"
            self.sessions.append(sid)
            return httpx.Response(200, headers={"mcp-session-id": sid},
                                  json={"jsonrpc": "2.0", "id": 1, "result": {}})
        if method == "notifications/initialized":
            return httpx.Response(202)
        sid = request.headers.get("mcp-session-id")
        if not sid:
            return httpx.Response(400, json={"error": {"message": "Missing session ID"}})
        if self.forget:
            self.forget = False
            return httpx.Response(404, json={"error": {"message": "Session not found"}})
        if sid != self.sessions[-1]:
            return httpx.Response(404, json={"error": {"message": "Session not found"}})
        tool = body["params"]["name"]
        res = {"content": [{"type": "text", "text": json.dumps(
            {"job_id": "rig-1", "state": "running", "tool": tool})}]}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 2, "result": res})


@pytest.fixture
def rig():
    return Rig()


def _forge(rig, **kw):
    return image_jobs.ClawForge("http://rig.invalid/mcp",
                                transport=httpx.MockTransport(rig.handler), **kw)


def test_one_session_serves_many_calls(rig):
    forge = _forge(rig)

    async def go():
        for _ in range(5):
            await forge.call("get_job", {"job_id": "rig-1"})
        await forge.aclose()
    asyncio.run(go())

    assert len(rig.sessions) == 1, "the handshake must not be repeated per call"
    assert rig.requests.count("tools/call") == 5
    assert forge.handshakes == 1


def test_a_forgotten_session_is_rebuilt_once_and_the_call_repeated(rig):
    forge = _forge(rig)

    async def go():
        await forge.call("get_job", {"job_id": "rig-1"})
        rig.forget = True
        res = await forge.call("get_job", {"job_id": "rig-1"})
        await forge.aclose()
        return res
    res = asyncio.run(go())

    assert len(rig.sessions) == 2 and forge.handshakes == 2
    assert "rig-1" in res["content"][0]["text"], "the repeated call must return the answer"


def test_a_dead_rig_trips_the_breaker_so_the_next_calls_fail_fast(rig, monkeypatch):
    forge = _forge(rig)
    rig.down = True
    attempts = []
    real = rig.handler

    def counting(request):
        attempts.append(request.method)
        return real(request)
    forge.transport = httpx.MockTransport(counting)

    async def go():
        with pytest.raises(image_jobs.ImageJobError) as e1:
            await forge.call("get_job", {"job_id": "a"})
        assert e1.value.retryable
        n = len(attempts)
        # Every further call inside the window: retryable, and NO wire attempt.
        for _ in range(10):
            with pytest.raises(image_jobs.ImageJobError) as e:
                await forge.call("get_job", {"job_id": "b"})
            assert e.value.retryable
            assert "backing off" in e.value.message
        assert len(attempts) == n
        st = forge.status()
        assert st["reachable"] is False and st["last_error"] == "ConnectError"
        # Window over, rig back: the probe succeeds and the breaker clears.
        monkeypatch.setattr(image_jobs, "RIG_BACKOFF_S", 0.0)
        forge._unreachable_until = 0.0
        rig.down = False
        await forge.call("get_job", {"job_id": "c"})
        assert forge.status()["reachable"] is True
        await forge.aclose()
    asyncio.run(go())


def test_the_rig_cannot_name_a_file_outside_its_files_area(rig):
    forge = _forge(rig)

    async def go():
        for bad in ("../../etc/passwd", "a/../../x.png", "http://evil/x.png",
                    "a//b.png"):
            with pytest.raises(image_jobs.ImageJobError) as e:
                await forge.fetch(bad)
            assert "outside" in e.value.message
        data = await forge.fetch("2026/09/02/ok.png")
        assert data.startswith(b"\x89PNG")
        await forge.aclose()
    asyncio.run(go())
