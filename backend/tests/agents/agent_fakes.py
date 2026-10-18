"""Hand-written fakes for the agent suite. No network beyond loopback, no SDK.

* FakeGateway   - an asyncio websockets server on an ephemeral loopback port that
                  speaks the OpenClaw frames the EXISTING client code expects
                  (app/gateway_ws.py, app/gateway_router.py, app/openclaw.py).
                  Frame shapes are taken from those modules, not from upstream
                  docs (RESEARCH.md R-OC3: upstream schema unverified). The one
                  assumption beyond them: a ``session.tool`` terminal phase
                  named "end" - the repo only handles "start".
* FakeSdkTransport - an in-process SdkTransport that emits the wire dicts the
                  worker would send. Message shapes are DERIVED FROM DOCS
                  (RESEARCH.md F6e system/init + api_retry, R-SESS session_id,
                  R-COST ResultMessage.total_cost_usd / subtypes); no recording
                  of a real run exists because no key was available.
* FakeSdk       - a stand-in for the ``claude_agent_sdk`` module, for testing the
                  worker (option filtering, can_use_tool, message conversion).
"""
from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, ClassVar

FAKE_API_KEY = "sk-ant-api03-FAKEFAKEFAKE0123456789abcdefSECRET"
FAKE_GATEWAY_TOKEN = "gw-token-FAKE-0123456789abcdef"
TRUNCATED_FULL = "B" * 9000          # what chat.message.get returns for a truncated projection
TRUNCATED_PROJECTION = "B" * 50 + "...(truncated)..."


def scenario_of(message: str) -> str:
    """Which scripted scenario a prompt asks for (shared by every fake)."""
    if message.startswith("scenario:"):
        return message.split(":", 1)[1].strip()
    return "resume"


# --------------------------------------------------------------------------- #
# OpenClaw gateway
# --------------------------------------------------------------------------- #

GATEWAY_METHODS = ["sessions.subscribe", "sessions.messages.subscribe", "chat.history",
                   "chat.message.get", "chat.abort", "agent", "agent.wait"]
GATEWAY_EVENTS = ["session.message", "chat", "session.tool"]


class FakeGateway:
    def __init__(self, *, protocol: int | None = 4, token: str = FAKE_GATEWAY_TOKEN,
                 connect_close: tuple[int, str] | None = None,
                 connect_error: dict | None = None, methods: list[str] | None = None,
                 events: list[str] | None = None) -> None:
        self.protocol = protocol
        self.token = token
        self.connect_close = connect_close
        self.connect_error = connect_error
        self.methods = GATEWAY_METHODS if methods is None else methods
        self.events = GATEWAY_EVENTS if events is None else events
        self.requests: list[dict] = []
        self.url = ""
        self._server: Any = None
        self._abort: dict[str, asyncio.Event] = {}
        self._tasks: set[asyncio.Task] = set()
        self.connections = 0
        self.fail_connect_after_probe = False

    async def start(self) -> str:
        from websockets.asyncio.server import serve
        self._server = await serve(self._handler, "127.0.0.1", 0)
        port = self._server.sockets[0].getsockname()[1]
        assert port not in (1234, 1235, 1236, 8080, 8700, 8288, 7865, 18789)
        self.url = f"ws://127.0.0.1:{port}"
        return self.url

    async def stop(self) -> None:
        for t in list(self._tasks):
            t.cancel()
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()

    def methods_called(self) -> list[str]:
        return [r.get("method") for r in self.requests]

    # -- plumbing --------------------------------------------------------------- #

    async def _handler(self, ws: Any) -> None:
        self.connections += 1
        await ws.send(json.dumps({"type": "event", "event": "connect.challenge", "payload": {}}))
        try:
            async for raw in ws:
                frame = json.loads(raw)
                if frame.get("type") != "req":
                    continue
                self.requests.append(frame)
                await self._on_req(ws, frame)
        except Exception:
            return

    @staticmethod
    async def _res(ws: Any, req: dict, payload: dict | None = None, *, ok: bool = True,
                   error: dict | None = None) -> None:
        frame = {"type": "res", "id": req["id"], "ok": ok}
        if ok:
            frame["payload"] = payload or {}
        else:
            frame["error"] = error or {"code": "ERROR", "message": "error"}
        await ws.send(json.dumps(frame))

    @staticmethod
    async def _event(ws: Any, event: str, payload: dict) -> None:
        await ws.send(json.dumps({"type": "event", "event": event, "payload": payload}))

    async def _on_req(self, ws: Any, req: dict) -> None:
        method = req.get("method")
        params = req.get("params") or {}
        if method == "connect":
            if self.connect_close is not None:
                await ws.close(code=self.connect_close[0], reason=self.connect_close[1])
                return
            if self.connect_error is not None:
                await self._res(ws, req, ok=False, error=self.connect_error)
                return
            if (params.get("auth") or {}).get("token") != self.token:
                await self._res(ws, req, ok=False,
                                error={"code": "UNAUTHORIZED", "message": "gateway token mismatch"})
                return
            hello = {"features": {"methods": self.methods, "events": self.events},
                     "policy": {"tickIntervalMs": 30000}, "auth": {"scopes": ["operator.read"]}}
            if self.protocol is not None:
                hello["protocol"] = self.protocol
            await self._res(ws, req, hello)
        elif method in ("sessions.subscribe", "sessions.messages.subscribe", "chat.history"):
            await self._res(ws, req, {"messages": []} if method == "chat.history" else {})
        elif method == "chat.message.get":
            await self._res(ws, req, {"message": {"role": "assistant", "content": [
                {"type": "text", "text": TRUNCATED_FULL}]}})
        elif method in ("chat.abort", "sessions.abort"):
            key = (params.get("sessionKey") or params.get("key") or "").lower()
            self._abort.setdefault(key, asyncio.Event()).set()
            await self._res(ws, req, {"aborted": True})
        elif method == "agent":
            t = asyncio.create_task(self._run_agent(ws, req))
            self._tasks.add(t)
            t.add_done_callback(self._tasks.discard)
        else:
            await self._res(ws, req, ok=False, error={"code": "UNKNOWN_METHOD", "message": str(method)})

    # -- the scripted agent ------------------------------------------------------- #

    async def _run_agent(self, ws: Any, req: dict) -> None:
        p = req["params"]
        key, run_id = p["sessionKey"], p["idempotencyKey"]
        scenario = scenario_of(p["message"])
        sess_id = "oc-" + hashlib.sha1(key.lower().encode()).hexdigest()[:10]
        self._abort.setdefault(key.lower(), asyncio.Event()).clear()

        if scenario == "error_unavailable":
            await self._res(ws, req, ok=False, error={"code": "UNAVAILABLE", "message": "gateway is draining"})
            return
        if scenario == "error_refused":
            await self._res(ws, req, ok=False,
                            error={"code": "INVALID_REQUEST", "message": "model not allowed: gpt-x"})
            return
        await self._res(ws, req, {"status": "accepted", "runId": run_id})

        async def delta(text: str) -> None:
            await self._event(ws, "chat", {"runId": run_id, "sessionKey": key, "state": "delta",
                                           "message": {"content": [{"type": "text", "text": text}]}})

        usage = {"total": 120, "input": 100, "output": 20}
        text = "Hello world"
        aborted = False
        if scenario == "simple":
            await delta("Hello")
            await delta("Hello world")
        elif scenario == "tools":
            await self._event(ws, "session.tool", {"runId": run_id, "sessionKey": key, "stream": "tool",
                                                   "data": {"phase": "start", "name": "Read",
                                                            "toolCallId": "tc1", "args": {"file_path": "notes.txt"}}})
            await self._event(ws, "session.tool", {"runId": run_id, "sessionKey": key, "stream": "tool",
                                                   "data": {"phase": "end", "name": "Read",
                                                            "toolCallId": "tc1", "result": "file contents"}})
            text = "Read it."
            await delta(text)
        elif scenario == "long":
            await delta("working")
            await delta("working on")
            ev = self._abort.setdefault(key.lower(), asyncio.Event())
            try:
                await asyncio.wait_for(ev.wait(), timeout=20)
                aborted = True
            except TimeoutError:
                pass
            text = "working on"
            if aborted:
                await self._event(ws, "chat", {"runId": run_id, "sessionKey": key, "state": "aborted"})
        elif scenario == "budget":
            usage = {"total": 5000, "input": 4000, "output": 1000}
            text = "expensive"
            await delta(text)
        elif scenario == "resume":
            text = "resumed"
            await delta(text)
        elif scenario == "truncated":
            text = TRUNCATED_FULL
            await self._event(ws, "session.message", {
                "sessionKey": key, "messageSeq": 2,
                "message": {"role": "assistant", "content": [{"type": "text", "text": TRUNCATED_PROJECTION}],
                            "__openclaw": {"id": "m1"}}})
        elif scenario == "leaky":
            text = f"the token is {self.token}"
            await delta(text)
        elif scenario == "no_deltas":
            text = "only the transcript event"
        else:
            await delta(text)

        if scenario not in ("truncated",):
            await self._event(ws, "session.message", {
                "sessionKey": key, "messageSeq": 3,
                "message": {"role": "assistant", "content": [{"type": "text", "text": text}],
                            "__openclaw": {"id": "m2"}}})
        await self._res(ws, req, {
            "runId": run_id, "status": "ok", "summary": "completed",
            "result": {"payloads": [] if aborted else [{"text": text}],
                       "meta": {"durationMs": 5, "aborted": aborted,
                                "agentMeta": {"sessionId": sess_id, "usage": usage,
                                              "model": "fake-model", "provider": "fake"}}}})


# --------------------------------------------------------------------------- #
# Claude SDK transport (wire dicts)
# --------------------------------------------------------------------------- #

def _asst(*blocks: dict, parent: str | None = None, error: str | None = None) -> dict:
    return {"kind": "assistant", "content": list(blocks), "parent_tool_use_id": parent,
            "model": "fake-model", "error": error}


def _text(t: str) -> dict:
    return {"type": "text", "text": t}


def _tool_use(call_id: str, name: str, inp: dict) -> dict:
    return {"type": "tool_use", "id": call_id, "name": name, "input": inp}


def _tool_result(call_id: str, content: str, is_error: bool = False, parent: str | None = None) -> dict:
    return {"kind": "user", "content": [{"type": "tool_result", "tool_use_id": call_id,
                                         "content": content, "is_error": is_error}],
            "parent_tool_use_id": parent}


def _result(sid: str, *, subtype: str = "success", is_error: bool = False, cost: float = 0.01,
            turns: int = 1, text: str | None = "", usage: tuple[int, int] = (100, 20)) -> dict:
    return {"kind": "result", "subtype": subtype, "is_error": is_error, "num_turns": turns,
            "session_id": sid, "total_cost_usd": cost, "duration_ms": 5, "result": text,
            "usage": {"input_tokens": usage[0], "output_tokens": usage[1]}}


class FakeSdkTransport:
    """In-process SdkTransport. One instance = one 'worker'."""

    def __init__(self, *, session_id: str = "sdk-sess-1") -> None:
        self.session_id = session_id
        self._alive = False
        self._q: asyncio.Queue = asyncio.Queue()
        self.plan: Any = None
        self.queries: list[str] = []
        self.interrupts = 0
        self.closed = False
        self._interrupt = asyncio.Event()
        self._steer: asyncio.Queue = asyncio.Queue()
        self._perm: dict[str, asyncio.Future] = {}
        self.permission_answers: list[tuple[str, bool, str | None]] = []
        self._tasks: set[asyncio.Task] = set()
        self._running = False
        self.fail_start: Exception | None = None
        self.die_after_next_result = False

    @property
    def alive(self) -> bool:
        return self._alive

    async def start(self, plan: Any) -> None:
        if self.fail_start is not None:
            raise self.fail_start
        self.plan = plan
        resume = plan.options.get("resume")
        if resume:
            self.session_id = resume
        self._alive = True

    async def messages(self):
        while True:
            item = await self._q.get()
            if item is None:
                return
            yield item

    def _put(self, m: dict) -> None:
        self._q.put_nowait(m)

    async def query(self, prompt: str) -> None:
        self.queries.append(prompt)
        if self._running:                       # a steer message during a turn
            self._steer.put_nowait(prompt)
            return
        self._interrupt.clear()
        self._running = True
        t = asyncio.create_task(self._script(prompt))
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    async def interrupt(self) -> None:
        self.interrupts += 1
        self._interrupt.set()

    async def answer_permission(self, request_id: str, allow: bool, message: str | None = None) -> None:
        self.permission_answers.append((request_id, allow, message))
        fut = self._perm.get(request_id)
        if fut is not None and not fut.done():
            fut.set_result(allow)

    async def close(self) -> None:
        self._alive = False
        self.closed = True
        for t in list(self._tasks):
            t.cancel()
        self._q.put_nowait(None)

    def kill(self) -> None:
        """Simulate the worker dying (no result, stream ends)."""
        self._alive = False
        self._q.put_nowait(None)

    # -- scripts ------------------------------------------------------------------ #

    async def _script(self, prompt: str) -> None:
        sid = self.session_id
        sc = scenario_of(prompt)
        try:
            self._put({"kind": "system", "subtype": "init", "data": {"session_id": sid}})
            await getattr(self, f"_sc_{sc}", self._sc_simple)(sid)
        finally:
            self._running = False
            if self.die_after_next_result:
                self.kill()

    async def _sc_simple(self, sid: str) -> None:
        self._put({"kind": "stream_event", "event": {"type": "message_start"}, "parent_tool_use_id": None})
        for piece in ("Hello", " world"):
            self._put({"kind": "stream_event", "parent_tool_use_id": None,
                       "event": {"type": "content_block_delta",
                                 "delta": {"type": "text_delta", "text": piece}}})
        self._put(_asst(_text("Hello world")))
        self._put(_result(sid, text="Hello world"))

    async def _sc_tools(self, sid: str) -> None:
        self._put(_asst(_tool_use("c1", "Read", {"file_path": "notes.txt"})))
        self._put(_tool_result("c1", "file contents"))
        self._put(_asst(_text("Read it.")))
        self._put(_result(sid, text="Read it."))

    async def _sc_approval(self, sid: str) -> None:
        self._put(_asst(_tool_use("c2", "Bash", {"command": "echo hi"})))
        fut = asyncio.get_running_loop().create_future()
        self._perm["r1"] = fut
        self._put({"kind": "permission_request", "request_id": "r1", "tool_name": "Bash",
                   "input": {"command": "echo hi"}, "tool_use_id": "c2"})
        allowed = await fut
        self._put(_tool_result("c2", "hi") if allowed else _tool_result("c2", "Denied", is_error=True))
        self._put(_asst(_text("Done.")))
        self._put(_result(sid, text="Done."))

    async def _sc_subagent(self, sid: str) -> None:
        self._put(_asst(_tool_use("s1", "Agent", {"subagent_type": "researcher", "description": "look things up"})))
        self._put(_asst(_text("child says hi"), parent="s1"))
        self._put(_tool_result("s1", "child finished"))
        self._put(_asst(_text("All done.")))
        self._put(_result(sid, text="All done."))

    async def _sc_retry(self, sid: str) -> None:
        self._put({"kind": "system", "subtype": "api_retry",
                   "data": {"attempt": 1, "max_retries": 3, "retry_delay_ms": 500, "error_status": 429,
                            "error": "rate limited"}})
        self._put(_asst(_text("recovered")))
        self._put(_result(sid, text="recovered"))

    async def _sc_long(self, sid: str) -> None:
        self._put(_asst(_text("working on it")))
        waiters = {asyncio.create_task(self._interrupt.wait()): "interrupt",
                   asyncio.create_task(self._steer.get()): "steer"}
        done, pending = await asyncio.wait(waiters, timeout=20, return_when=asyncio.FIRST_COMPLETED)
        for p in pending:
            p.cancel()
        kinds = [waiters[d] for d in done]
        if "interrupt" in kinds:
            # What the CLI does after client.interrupt(): ends the turn with an error result.
            self._put(_result(sid, subtype="error_during_execution", is_error=True, cost=0.002, text=""))
        elif "steer" in kinds:
            msg = next(d.result() for d in done if waiters[d] == "steer")
            self._put(_asst(_text(f"steered: {msg}")))
            self._put(_result(sid, text="steered"))

    async def _sc_budget(self, sid: str) -> None:
        self._put(_asst(_text("expensive")))
        self._put(_result(sid, subtype="error_max_budget_usd", is_error=True, cost=0.06,
                          text="", usage=(4000, 1000)))

    async def _sc_budget_soft(self, sid: str) -> None:
        """The SDK overshoots its own cap and still reports success (R-COST)."""
        self._put(_asst(_text("expensive")))
        self._put(_result(sid, cost=0.10, usage=(4000, 1000), text="expensive"))

    async def _sc_leaky(self, sid: str) -> None:
        """A tool and the model both echo the API key (an env dump, a pasted traceback)."""
        self._put(_asst(_tool_use("c5", "Read", {"file_path": f"{FAKE_API_KEY}.txt"})))
        self._put(_tool_result("c5", f"ANTHROPIC_API_KEY={FAKE_API_KEY}"))
        self._put(_asst(_text(f"the key is {FAKE_API_KEY}")))
        self._put(_result(sid, text=f"done {FAKE_API_KEY}"))

    async def _sc_error_unavailable(self, sid: str) -> None:
        self._put({"kind": "worker_error", "code": "cli_not_found", "message": "the CLI could not be found"})

    async def _sc_error_refused(self, sid: str) -> None:
        self._put(_asst(_text("bad request"), error="invalid_request"))
        self._put(_result(sid, subtype="error_during_execution", is_error=True, cost=0.0, text="bad request"))

    async def _sc_error_auth(self, sid: str) -> None:
        self._put(_asst(_text("auth"), error="authentication_failed"))
        self._put(_result(sid, subtype="error_during_execution", is_error=True, cost=0.0, text="auth"))

    async def _sc_mcp_error(self, sid: str) -> None:
        self._put({"kind": "system", "subtype": "init",
                   "data": {"session_id": sid, "mcp_server_errors": ["evil: failed to start"]}})

    async def _sc_resume(self, sid: str) -> None:
        self._put(_asst(_text("resumed")))
        self._put(_result(sid, text="resumed"))

    async def _sc_write_file(self, sid: str) -> None:
        self._put(_asst(_tool_use("c9", "Write", {"file_path": "out/report.md"})))
        self._put(_asst(_tool_use("c10", "Write", {"file_path": "../../escape.txt"})))
        self._put(_result(sid, text="wrote"))


# --------------------------------------------------------------------------- #
# A stand-in for the claude_agent_sdk module (worker tests)
# --------------------------------------------------------------------------- #

@dataclass
class ClaudeAgentOptions:
    cwd: str | None = None
    setting_sources: list | None = None
    permission_mode: str | None = None
    allowed_tools: list = field(default_factory=list)
    disallowed_tools: list = field(default_factory=list)
    mcp_servers: dict = field(default_factory=dict)
    extra_args: dict = field(default_factory=dict)
    env: dict = field(default_factory=dict)
    include_partial_messages: bool = False
    max_turns: int | None = None
    max_budget_usd: float | None = None
    model: str | None = None
    system_prompt: str | None = None
    resume: str | None = None
    can_use_tool: Any = None
    hooks: Any = None


@dataclass
class PermissionResultAllow:
    behavior: str = "allow"


@dataclass
class PermissionResultDeny:
    message: str = ""
    behavior: str = "deny"


@dataclass
class HookMatcher:
    matcher: Any = None
    hooks: list = field(default_factory=list)


@dataclass
class TextBlock:
    text: str


@dataclass
class ThinkingBlock:
    thinking: str = "secret reasoning"


@dataclass
class ToolUseBlock:
    id: str
    name: str
    input: dict


@dataclass
class ToolResultBlock:
    tool_use_id: str
    content: Any = None
    is_error: bool = False


@dataclass
class AssistantMessage:
    content: list
    model: str = "fake"
    parent_tool_use_id: str | None = None
    error: str | None = None


@dataclass
class UserMessage:
    content: Any
    parent_tool_use_id: str | None = None


@dataclass
class SystemMessage:
    subtype: str
    data: dict


@dataclass
class ResultMessage:
    subtype: str
    duration_ms: int
    is_error: bool
    num_turns: int
    session_id: str
    total_cost_usd: float | None = None
    usage: dict | None = None
    result: str | None = None


@dataclass
class StreamEvent:
    event: dict
    parent_tool_use_id: str | None = None


class ClaudeSDKClient:
    """Streaming-mode client: remembers its options; a query() runs a tiny script."""

    instances: ClassVar[list[ClaudeSDKClient]] = []

    def __init__(self, options: ClaudeAgentOptions) -> None:
        self.options = options
        self.queries: list[str] = []
        self.interrupted = 0
        self.connected = False
        self.disconnected = False
        self._q: asyncio.Queue = asyncio.Queue()
        self._tasks: set[asyncio.Task] = set()
        ClaudeSDKClient.instances.append(self)

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.disconnected = True

    async def receive_messages(self):
        while True:
            m = await self._q.get()
            yield m

    async def interrupt(self) -> None:
        self.interrupted += 1
        self._q.put_nowait(ResultMessage("error_during_execution", 1, True, 1, "w-sess", 0.0, {}, ""))

    async def query(self, prompt: str) -> None:
        self.queries.append(prompt)
        t = asyncio.create_task(self._turn(prompt))
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    async def _turn(self, prompt: str) -> None:
        self._q.put_nowait(SystemMessage("init", {"session_id": "w-sess"}))
        if prompt == "needs-tool":
            cb = self.options.can_use_tool
            res = await cb("Bash", {"command": "ls"}, type("Ctx", (), {"tool_use_id": "tu1"})())
            ok = isinstance(res, PermissionResultAllow)
            self._q.put_nowait(AssistantMessage([ThinkingBlock(), ToolUseBlock("tu1", "Bash", {"command": "ls"})]))
            self._q.put_nowait(UserMessage([ToolResultBlock("tu1", "out" if ok else "Denied", not ok)]))
        self._q.put_nowait(StreamEvent({"type": "message_start"}))
        self._q.put_nowait(AssistantMessage([TextBlock("hi")]))
        self._q.put_nowait(ResultMessage("success", 3, False, 1, "w-sess", 0.01,
                                         {"input_tokens": 5, "output_tokens": 2}, "hi"))


def fake_sdk_module() -> Any:
    ClaudeSDKClient.instances = []
    return type("FakeSdkModule", (), {
        "ClaudeAgentOptions": ClaudeAgentOptions, "ClaudeSDKClient": ClaudeSDKClient,
        "PermissionResultAllow": PermissionResultAllow, "PermissionResultDeny": PermissionResultDeny,
        "HookMatcher": HookMatcher})


__all__ = [
    "FAKE_API_KEY",
    "FAKE_GATEWAY_TOKEN",
    "FakeGateway",
    "FakeSdkTransport",
    "dataclasses",
    "fake_sdk_module",
    "scenario_of",
]
