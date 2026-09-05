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
        self.ids: list = []
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
                                  json={"jsonrpc": "2.0", "id": body.get("id"),
                                        "result": {}})
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
        # ECHOES the request id, as a JSON-RPC server must: the client now
        # refuses an answer carrying somebody else's id (see _check_rpc_id).
        self.ids.append(body.get("id"))
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body.get("id"),
                                         "result": res})


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


# --------------------------------------------------------------------------- #
# `can_render`, and the payload contract (ClawForge2 2.2.1, 2026-09-04)
#
# The shapes below are trimmed copies of what the live rig answered while this
# was written, not inventions: `comfy.running: false` with `can_render: true`
# is the state that made `comfy.running` the wrong gate, and it is the state
# the rig was actually in.
# --------------------------------------------------------------------------- #

#: What `comfy_status` looks like on a rig that is idle-unloaded but fine.
LIVE_STATUS = {
    "schema_version": 1,
    "comfy": {"running": False, "backend_origin": "none",
              "backoff": {"circuit_open": False, "retry_in_s": 0.0}},
    "reachable": True,
    "leases": {"ours": {"held": True, "holder": "clawforge2"}, "foreign": [],
               "source": "studioforge", "error": None},
    "can_render": True,
    "can_render_reason": "backend_reachable",
    "can_render_detail": "ComfyUI is answering on http://127.0.0.1:8288.",
    "workflow_names": ["anima", "krea2"],
}

#: The same call with somebody else's benchmark standing on every card.
LEASED_STATUS = {
    **LIVE_STATUS,
    "can_render": False,
    "can_render_reason": "cards_leased",
    "can_render_detail": "CUDA [0, 1, 2, 3] are leased to bench-runner-judge "
                         "[benchmark] for another 7170s.",
    "leases": {"ours": {"held": False, "holder": ""},
               "foreign": [{"id": "72ddee86", "holder": "bench-runner-judge",
                            "holder_family": "bench-runner",
                            "kind": "benchmark", "devices": [0, 1, 2, 3],
                            "expires_in_s": 7169.8}],
               "source": "studioforge", "error": None},
}


def _answering(payload):
    """A forge whose every tool call returns `payload`, counting the calls."""
    forge = image_jobs.ClawForge("http://rig.invalid/mcp")
    calls: list[str] = []

    async def fake_call(tool, args, **kw):
        calls.append(tool)
        return {"content": [{"type": "text", "text": json.dumps(payload)}]}
    forge.call = fake_call
    return forge, calls


def test_an_idle_unloaded_rig_can_still_render():
    """The whole reason `can_render` replaced `comfy.running`: ClawForge frees
    ComfyUI's models when idle and starts the backend on the next job, so
    `running: false` is routinely a rig that renders fine."""
    forge, calls = _answering(LIVE_STATUS)
    ready = asyncio.run(forge.readiness())
    assert LIVE_STATUS["comfy"]["running"] is False
    assert ready.can_render and ready.reason == "backend_reachable"
    assert not ready.blocked and not ready.stand_down
    assert calls == ["comfy_status"]
    assert forge.schema_version == 1


def test_a_foreign_benchmark_lease_is_read_off_the_status_block():
    forge, _ = _answering(LEASED_STATUS)
    ready = asyncio.run(forge.readiness())
    assert ready.blocked and ready.leased and ready.stand_down
    assert ready.reservation == "bench-runner (benchmark)"


def test_the_readiness_probe_does_not_ask_for_the_job_history():
    """`recent_jobs` is opt-in since 2.2.1 and was ~73% of the response. This
    call asks a three-field question and must not drag it back."""
    forge = image_jobs.ClawForge("http://rig.invalid/mcp")
    seen: list[dict] = []

    async def fake_call(tool, args, **kw):
        seen.append(args)
        return {"content": [{"type": "text", "text": json.dumps(LIVE_STATUS)}]}
    forge.call = fake_call
    asyncio.run(forge.readiness())
    assert seen == [{}], "no include_recent_jobs, no other knobs"


def test_the_readiness_answer_is_cached_across_a_sweep():
    forge, calls = _answering(LIVE_STATUS)

    async def go():
        for _ in range(5):
            await forge.readiness()
        await forge.readiness(max_age_s=0.0)   # explicit refresh
    asyncio.run(go())
    assert calls == ["comfy_status", "comfy_status"]


@pytest.mark.parametrize("payload", [
    {},                                             # a rig with no can_render
    {"can_render": "yes", "can_render_reason": "backend_reachable"},
    {"can_render": False},                          # no reason given
])
def test_an_unreadable_answer_fails_open(payload):
    """Every inconclusive probe leaves the submit going ahead. The rig's own
    refusal is the authority; a parse failure here must never become an outage."""
    forge, _ = _answering(payload)
    ready = asyncio.run(forge.readiness())
    assert ready.can_render and not ready.blocked


def test_a_probe_that_cannot_be_made_leaves_the_previous_answer_standing():
    forge = image_jobs.ClawForge("http://rig.invalid/mcp")

    async def go():
        forge._readiness = image_jobs.RigReadiness(
            can_render=False, reason="cards_leased", checked_at=1.0)

        async def boom(*a, **k):
            raise image_jobs.ImageJobError("unreachable", retryable=True)
        forge.call = boom
        return await forge.readiness(max_age_s=0.0)
    ready = asyncio.run(go())
    assert ready.reason == "cards_leased", "a blip does not erase what we knew"


def test_the_health_block_carries_the_verdict_but_not_the_rigs_prose():
    """/api/health is readable by a locked device, and `can_render_detail` is
    free prose that names the rig's own ComfyUI URL."""
    forge, _ = _answering(LIVE_STATUS)
    asyncio.run(forge.readiness())
    st = forge.status()
    assert st["readiness"]["can_render"] is True
    assert st["readiness"]["reason"] == "backend_reachable"
    assert st["schema_version"] == 1
    assert "8288" not in json.dumps(st) and "detail" not in st["readiness"]


def test_a_schema_version_bump_is_logged_because_a_field_has_moved(caplog):
    forge, _ = _answering(LIVE_STATUS)
    asyncio.run(forge.readiness())
    forge._readiness = None
    forge2, _ = _answering({**LIVE_STATUS, "schema_version": 2})
    forge2.schema_version = 1
    with caplog.at_level("WARNING"):
        asyncio.run(forge2.readiness())
    assert forge2.schema_version == 2
    assert any("schema_version moved 1 -> 2" in r.message for r in caplog.records)


# --------------------------------------------------------------------------- #
# Job failure codes
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("payload,expected", [
    ({"state": "failed", "error": "[stalled] no progress for 240s"}, "stalled"),
    ({"state": "failed", "error": "boom", "error_code": "comfy_rejected"},
     "comfy_rejected"),
    # An explicit code beats the prose, which is only ever the fallback.
    ({"state": "failed", "error": "[stalled] x", "error_code": "other"}, "other"),
    ({"state": "failed", "error": "no code here"}, ""),
    ({"state": "running"}, ""),
])
def test_a_failed_job_reports_the_rigs_code_for_it(payload, expected):
    forge, _ = _answering(payload)
    poll = asyncio.run(forge.poll("rig-1"))
    assert poll.code == expected


# --------------------------------------------------------------------------- #
# JSON-RPC ids — the fix for a live wedge, 2026-09-04
#
# The sweep advances up to _IMAGE_JOB_CONCURRENCY jobs at once over ONE MCP
# session. Every tools/call used to be sent as `"id": 2`, and ClawForge2
# matches an answer to a request by id, so concurrent calls collided: one
# coroutine got another's answer and one connection was closed unread. In
# DisPatch that was a PERMANENT hang — the sweep never returned, /api/health
# reported the image_jobs loop stale, and every open render froze at its last
# percentage until the app was restarted. Reproduced live twice.
# --------------------------------------------------------------------------- #

def test_every_call_carries_a_fresh_rpc_id(rig):
    """Ids must never repeat: the id is the ONLY thing telling two in-flight
    answers on one session apart."""
    forge = _forge(rig)

    async def go():
        await forge.poll("a")
        await forge.poll("b")
        await forge.poll("c")

    asyncio.run(go())
    assert len(rig.ids) == 3
    assert len(set(rig.ids)) == 3, f"ids repeated: {rig.ids}"


def test_concurrent_calls_never_share_an_id(rig):
    """The exact live shape: three calls in flight at once on one session."""
    forge = _forge(rig)

    async def go():
        await asyncio.gather(forge.poll("a"), forge.poll("b"), forge.poll("c"))

    asyncio.run(go())
    assert len(rig.ids) == 3
    assert len(set(rig.ids)) == 3, f"concurrent ids collided: {rig.ids}"


def test_an_answer_with_the_wrong_id_is_refused_not_used(rig):
    """A crossed-over answer is discarded and retried — never acted on.

    This is the one that matters beyond the hang: what a poll's answer carries
    is `files_rel`, so acting on another job's answer puts the wrong picture
    into the wrong family thread.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("method") == "initialize":
            return httpx.Response(200, headers={"mcp-session-id": "sid-1"},
                                  json={"jsonrpc": "2.0", "id": body.get("id"),
                                        "result": {}})
        if body.get("method") == "notifications/initialized":
            return httpx.Response(202)
        # Somebody else's answer, complete with a files_rel we must not use.
        res = {"content": [{"type": "text", "text": json.dumps(
            {"job_id": "someone-else", "state": "done",
             "files_rel": ["not/ours.png"]})}]}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 999999,
                                         "result": res})

    forge = image_jobs.ClawForge("http://rig/mcp",
                                 transport=httpx.MockTransport(handler))
    with pytest.raises(image_jobs.ImageJobError) as e:
        asyncio.run(forge.poll("ours"))
    assert e.value.retryable, "a crossed answer is a race, so it is retryable"
    assert "not/ours.png" not in str(e.value)


def test_a_missing_id_is_tolerated(rig):
    """Guard against the WRONG id, not against a server that omits it."""
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("method") == "initialize":
            return httpx.Response(200, headers={"mcp-session-id": "sid-1"},
                                  json={"result": {}})
        if body.get("method") == "notifications/initialized":
            return httpx.Response(202)
        res = {"content": [{"type": "text", "text": json.dumps(
            {"state": "running"})}]}
        return httpx.Response(200, json={"jsonrpc": "2.0", "result": res})

    forge = image_jobs.ClawForge("http://rig/mcp",
                                 transport=httpx.MockTransport(handler))
    assert asyncio.run(forge.poll("ours")).state == "running"
