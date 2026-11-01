#!/usr/bin/env python3
"""OpenClaw gateway smoke test for DisPatch (issue #25, G16).

Drives DisPatch's native gateway transport (``DISPATCH_GATEWAY_WS=1``, app/gateway_ws.py +
app/gateway_router.py) end to end on a SCRATCH instance: a signed-in tab opens a chat with
an agent bot, sends one message over /ws, and the reply must come back through the gateway
socket - streamed (``chat`` deltas), persisted ONCE (``session.message`` + the run result
must not double it), with the run's metadata.

Two modes:

* default: a **fake gateway** (``FakeGateway``, a websockets server on loopback) that
  speaks the subset of the protocol DisPatch uses: ``connect`` (token checked, the hello
  advertises the methods/events ``_assert_capabilities`` insists on), ``sessions.*``
  subscriptions, ``chat.history``, and ``agent`` (accepted receipt -> ``chat`` deltas ->
  ``session.message`` -> ``chat`` final -> the result). Scenarios: ``reply`` (the happy
  path, checked for streaming and exactly-once delivery) and ``refused`` (the gateway
  refuses the run at the door: the tab must get an error, the thread no reply, the
  server keeps serving). Runs anywhere; this is what CI runs.

* ``--live``: **the coordinator's one optional live step.** No fake: the scratch DisPatch
  connects to the real gateway (``--gateway-url``, default ws://127.0.0.1:18789) with the
  token resolved by DisPatch's own ``gateway_ws.gateway_token()`` (never printed, passed to
  the child in its environment only). Its one agent bot is ``ds_flash`` and the thread is
  new, so the session key is fresh. It costs ONE agent turn. Refuses to run when the
  maintenance marker (``~/.local/state/openclaw/maintenance``) exists or the gateway port
  does not answer. Never run it from CI or from a builder.

Usage::

    cd backend && uv run python ../tests/smoke/openclaw_smoke.py --port 18860 \
        --data ~/.cache/dispatch-e2e/smoke --fresh            # fake gateway
    cd backend && uv run python ../tests/smoke/openclaw_smoke.py --live --port 18861 \
        --data ~/.cache/dispatch-e2e/smoke-live --fresh       # coordinator only

Exit 0 = every check passed; 1 = a check failed; 2 = the harness could not run
(3 = ``--live`` refused: marker present or gateway not answering).
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import socket
import sys
import threading
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "e2e"))
import _scratch
from _scratch import DEFAULT_DATA_ROOT, Scratch

FAKE_TOKEN = "e2e-fake-gateway-token"
FAKE_REPLY = "Smoke reply from the fake gateway: streamed, then persisted once."
GATEWAY_METHODS = ["agent", "agent.wait", "chat.history", "chat.message.get", "chat.abort",
                   "sessions.subscribe", "sessions.messages.subscribe", "sessions.abort", "models.list",
                   "sessions.usage"]
GATEWAY_EVENTS = ["session.message", "chat", "session.tool", "agent", "tick"]
MAINTENANCE_MARKER = Path.home() / ".local" / "state" / "openclaw" / "maintenance"


# --------------------------------------------------------------------------- #
# The fake gateway
# --------------------------------------------------------------------------- #

class FakeGateway:
    """A loopback websockets server playing the OpenClaw gateway (protocol 4 subset)."""

    def __init__(self, *, token: str = FAKE_TOKEN, reply: str = FAKE_REPLY):
        self.token = token
        self.reply = reply
        self.mode = "reply"                  # reply | refused
        self.port = 0
        self.connects = 0
        self.bad_tokens = 0
        self.agent_calls: list[dict] = []
        self.requests: list[str] = []
        self._seq: dict[str, int] = {}
        self._tasks: set[asyncio.Task] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server = None
        self._ready = threading.Event()
        self._t = threading.Thread(target=self._run, name="fake-gateway", daemon=True)

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}"

    def start(self) -> FakeGateway:
        self._t.start()
        if not self._ready.wait(15):
            raise RuntimeError("the fake gateway did not start")
        return self

    def stop(self) -> None:
        if self._loop and self._server:
            self._loop.call_soon_threadsafe(self._server.close)
        self._t.join(timeout=5)

    def _run(self) -> None:
        from websockets.asyncio.server import serve

        async def main():
            self._loop = asyncio.get_running_loop()
            async with serve(self._handler, "127.0.0.1", 0, max_size=2 ** 24) as server:
                self._server = server
                self.port = next(iter(server.sockets)).getsockname()[1]
                self._ready.set()
                await server.wait_closed()

        with contextlib.suppress(Exception):
            asyncio.run(main())

    async def _handler(self, ws) -> None:
        send = lambda frame: ws.send(json.dumps(frame))  # noqa: E731
        await send({"type": "event", "event": "connect.challenge", "payload": {"nonce": uuid.uuid4().hex}})
        first = json.loads(await ws.recv())
        if first.get("method") != "connect":
            await ws.close(code=1008)
            return
        if ((first.get("params") or {}).get("auth") or {}).get("token") != self.token:
            self.bad_tokens += 1
            await send({"type": "res", "id": first.get("id"), "ok": False,
                        "error": {"code": "UNAUTHORIZED", "message": "bad token"}})
            await ws.close(code=1008)
            return
        self.connects += 1
        await send({"type": "res", "id": first["id"], "ok": True, "payload": {
            "protocol": 4, "server": {"version": "fake-gateway-e2e"},
            "features": {"methods": GATEWAY_METHODS, "events": GATEWAY_EVENTS},
            "policy": {"tickIntervalMs": 15000},
            "auth": {"scopes": ["operator.read", "operator.write"]}}})
        ticker = asyncio.create_task(self._tick(ws))
        try:
            async for raw in ws:
                frame = json.loads(raw)
                if frame.get("type") != "req":
                    continue
                method, rid, params = frame.get("method"), frame.get("id"), frame.get("params") or {}
                self.requests.append(method)
                if method == "agent":
                    task = asyncio.create_task(self._agent(ws, rid, params))
                    self._tasks.add(task)
                    task.add_done_callback(self._tasks.discard)
                elif method == "chat.history":
                    await send({"type": "res", "id": rid, "ok": True, "payload": {"messages": []}})
                elif method == "agent.wait":
                    await send({"type": "res", "id": rid, "ok": True, "payload": {"status": "ok"}})
                elif method == "models.list":
                    await send({"type": "res", "id": rid, "ok": True, "payload": {"models": []}})
                else:
                    await send({"type": "res", "id": rid, "ok": True, "payload": {}})
        except Exception:
            pass
        finally:
            ticker.cancel()

    async def _tick(self, ws) -> None:
        with contextlib.suppress(Exception):
            while True:
                await asyncio.sleep(5)
                await ws.send(json.dumps({"type": "event", "event": "tick", "payload": {"ts": int(time.time() * 1000)}}))

    async def _agent(self, ws, rid: str, params: dict) -> None:
        send = lambda frame: ws.send(json.dumps(frame))  # noqa: E731
        self.agent_calls.append({k: params.get(k) for k in ("agentId", "sessionKey", "deliver", "idempotencyKey")})
        if self.mode == "refused":
            await send({"type": "res", "id": rid, "ok": False,
                        "error": {"code": "INVALID_REQUEST", "message": "fake gateway: this run is refused (smoke test)"}})
            return
        run_id = params.get("idempotencyKey") or uuid.uuid4().hex
        skey = params.get("sessionKey") or ""
        await send({"type": "res", "id": rid, "ok": True, "payload": {"status": "accepted", "runId": run_id}})
        await asyncio.sleep(0.2)
        words = self.reply.split(" ")
        half = " ".join(words[: len(words) // 2])
        await send({"type": "event", "event": "chat", "payload": {
            "runId": run_id, "sessionKey": skey, "state": "delta",
            "message": {"role": "assistant", "content": [{"type": "text", "text": half}]}}})
        await asyncio.sleep(0.3)
        await send({"type": "event", "event": "chat", "payload": {
            "runId": run_id, "sessionKey": skey, "state": "delta",
            "deltaText": self.reply[len(half):]}})
        await asyncio.sleep(0.2)
        seq = self._seq.get(skey, 0) + 2               # the user row took one seq
        self._seq[skey] = seq
        ts = int(time.time() * 1000)
        msg = {"role": "assistant", "content": [{"type": "text", "text": self.reply}],
               "timestamp": ts, "stopReason": "stop", "__openclaw": {"id": uuid.uuid4().hex[:8]}}
        await send({"type": "event", "event": "session.message",
                    "payload": {"sessionKey": skey, "messageSeq": seq, "message": msg}})
        await send({"type": "event", "event": "chat", "payload": {
            "runId": run_id, "sessionKey": skey, "state": "final"}})
        await send({"type": "res", "id": rid, "ok": True, "payload": {
            "runId": run_id, "status": "ok",
            "result": {"payloads": [{"text": self.reply}],
                       "meta": {"durationMs": 700, "stopReason": "stop",
                                "agentMeta": {"model": "fake-model", "provider": "fake",
                                              "sessionId": "fake-session",
                                              "usage": {"input": 12, "output": 14, "total": 26}}}}}})


# --------------------------------------------------------------------------- #
# The DisPatch side
# --------------------------------------------------------------------------- #

async def one_turn(base: str, cookie: str, thread_id: str, text: str, *, timeout: float) -> list[dict]:
    """Send `text` on /ws as the signed-in tab; collect frames until the turn settles."""
    import websockets
    frames: list[dict] = []
    async with websockets.connect(base.replace("http", "ws", 1) + "/ws",
                                  additional_headers={"Cookie": cookie, "Origin": base},
                                  max_size=2 ** 24, open_timeout=20) as ws:
        while json.loads(await asyncio.wait_for(ws.recv(), 20)).get("type") != "hello":
            pass
        await ws.send(json.dumps({"type": "send", "thread_id": thread_id, "text": text,
                                  "client_msg_id": uuid.uuid4().hex}))
        t_end = time.monotonic() + timeout
        quiet_after_done = None
        while time.monotonic() < t_end:
            try:
                raw = await asyncio.wait_for(ws.recv(), 1.0)
            except TimeoutError:
                if quiet_after_done and time.monotonic() > quiet_after_done:
                    break
                continue
            f = json.loads(raw)
            frames.append(f)
            t = f.get("type") or ""
            if t in ("error",) or (t in ("typing", "turn_done", "done") and not f.get("typing", True)) \
                    or t == "turn_end":
                quiet_after_done = time.monotonic() + 2.5
            if t == "message" and (f.get("message") or {}).get("role") == "assistant":
                quiet_after_done = time.monotonic() + 2.5
    return frames


def assistant_rows(api, thread_id: str) -> list[dict]:
    out = api.ok("GET", f"/api/threads/{thread_id}/messages")
    rows = out.get("messages", out) if isinstance(out, dict) else out
    return [m for m in rows if m.get("role") == "assistant"]


def check(results: list, name: str, ok: bool, detail: str = "") -> bool:
    results.append({"check": name, "ok": bool(ok), "detail": detail})
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + (f"  ({detail})" if detail else ""), flush=True)
    return ok


def run(args) -> int:
    results: list[dict] = []
    gw = None
    env = {}
    if args.live:
        if MAINTENANCE_MARKER.exists():
            print(f"--live refused: the maintenance marker {MAINTENANCE_MARKER} exists; record 'not run'")
            return 3
        host, port = args.gateway_url.split("//", 1)[1].split("/")[0].rsplit(":", 1)
        with socket.socket() as s:
            s.settimeout(3)
            if s.connect_ex((host, int(port))) != 0:
                print(f"--live refused: nothing answers on {args.gateway_url}; record 'not run'")
                return 3
        sys.path.insert(0, str(_scratch.BACKEND))
        from app import gateway_ws  # the one sanctioned token reader (never printed)
        token = gateway_ws.gateway_token()
        if not token:
            print("--live refused: no gateway token could be resolved", file=sys.stderr)
            return 3
        env["OPENCLAW_GATEWAY_TOKEN"] = token
        gateway_url = args.gateway_url
        bots = [{"id": "ds_flash", "name": "Flash (smoke)", "emoji": "⚡", "agent": "ds_flash"}]
    else:
        gw = FakeGateway().start()
        gateway_url = gw.url
        bots = [{"id": "main", "name": "Nova", "emoji": "⚡", "agent": "main"}]
    s = Scratch(args.port or _scratch.free_port(), args.data, fresh=args.fresh, fake_voice=False,
                gateway_url=gateway_url, env=env, agent_bots=bots)
    try:
        try:
            s.start()
            s.seed()
        except Exception as e:
            print(f"the scratch instance did not start: {e}", file=sys.stderr)
            return 2
        owner = s.api("Owner")
        bot_id = bots[0]["id"]
        deadline = time.monotonic() + 30
        connected = False
        while time.monotonic() < deadline and not connected:
            code, status = owner.call("GET", "/api/health")
            connected = bool(code == 200 and isinstance(status, dict)
                             and ((status.get("gateway_ws") or {}).get("connected")))
            if not connected:
                time.sleep(0.5)
        check(results, "/api/health reports the gateway socket connected", connected)
        if gw is not None:
            check(results, "DisPatch connected to the fake gateway with the token", gw.connects >= 1,
                  f"connects={gw.connects} bad_tokens={gw.bad_tokens}")
            check(results, "it subscribed to sessions", "sessions.subscribe" in gw.requests, str(sorted(set(gw.requests))))
        thread = owner.ok("POST", "/api/threads", {"bot_id": bot_id, "title": "Gateway smoke"})
        tid = thread["id"]
        prompt = ("Reply with the single word PONG." if args.live else "Smoke test: say something.")
        t0 = time.monotonic()
        frames = asyncio.run(one_turn(s.base, owner.cookie_header(), tid, prompt, timeout=args.timeout))
        took = time.monotonic() - t0
        rows = assistant_rows(owner, tid)
        types = [f.get("type") for f in frames]
        if args.live:
            texts = [r.get("content") or "" for r in rows]
            check(results, "one assistant reply in the thread", len(rows) == 1, f"{len(rows)} row(s), {took:.1f}s")
            check(results, "the reply has text and is not an error bubble",
                  bool(rows) and bool(texts[0].strip()) and not (rows[0].get("metadata") or {}).get("error"),
                  (texts[0][:80] if texts else ""))
        else:
            check(results, "the agent run reached the gateway",
                  len(gw.agent_calls) == 1 and gw.agent_calls[0]["agentId"] == "main"
                  and gw.agent_calls[0]["deliver"] is False,
                  json.dumps(gw.agent_calls)[:200])
            skey = gw.agent_calls[0]["sessionKey"] if gw.agent_calls else ""
            check(results, "the session key names the bot and the thread", skey == f"agent:main:{tid}", skey)
            half = FAKE_REPLY.split(" ")[: len(FAKE_REPLY.split(" ")) // 2]
            streamed = [f for f in frames if f.get("type") not in ("message",)
                        and " ".join(half) in json.dumps(f) and FAKE_REPLY not in json.dumps(f)]
            check(results, "the reply streamed to the tab before it was final", bool(streamed),
                  f"frame types: {sorted({t for t in types if t})}")
            exact = [r for r in rows if (r.get("content") or "").strip() == FAKE_REPLY]
            check(results, "the reply is persisted exactly once", len(exact) == 1 and len(rows) == 1,
                  f"{len(exact)} exact, {len(rows)} assistant row(s)")
            # Scenario 2: refused at the door.
            gw.mode = "refused"
            t2 = owner.ok("POST", "/api/threads", {"bot_id": bot_id, "title": "Gateway smoke (refused)"})["id"]
            frames2 = asyncio.run(one_turn(s.base, owner.cookie_header(), t2, "Smoke test: refused run.",
                                           timeout=min(args.timeout, 40)))
            rows2 = assistant_rows(owner, t2)
            shown = [f for f in frames2 if "refused" in json.dumps(f).lower() or f.get("type") == "error"]
            check(results, "a refused run tells the tab", bool(shown) or any(
                (r.get("metadata") or {}).get("error") for r in rows2),
                  f"frames {sorted({f.get('type') for f in frames2 if f.get('type')})}")
            check(results, "a refused run leaves no normal reply",
                  not any((r.get("content") or "").strip() == FAKE_REPLY for r in rows2))
            code, _ = owner.call("GET", "/api/health")
            check(results, "the server still answers after the refusal", code == 200)
    finally:
        s.stop()
        if s.stub is not None:
            s.stub.stop()
        if gw is not None:
            gw.stop()
    out = Path(args.out) if args.out else Path(args.data).parent / (Path(args.data).name + "-smoke.json")
    out.write_text(json.dumps({"mode": "live" if args.live else "fake", "checks": results,
                               "when": time.strftime("%Y-%m-%dT%H:%M:%S%z")}, indent=2))
    bad = [r for r in results if not r["ok"]]
    print(f"{len(results) - len(bad)}/{len(results)} checks passed ({'live' if args.live else 'fake gateway'}) -> {out}")
    return 1 if bad else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="DisPatch <-> OpenClaw gateway smoke test")
    ap.add_argument("--live", action="store_true",
                    help="the REAL gateway (coordinator only; one agent turn; refuses under the maintenance marker)")
    ap.add_argument("--gateway-url", default=os.environ.get("E2E_LIVE_GATEWAY_URL") or "ws://127.0.0.1:18789")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--data", default=str(DEFAULT_DATA_ROOT / "smoke"))
    ap.add_argument("--fresh", action="store_true")
    ap.add_argument("--timeout", type=float, default=90.0, help="seconds to wait for the turn")
    ap.add_argument("--out")
    return run(ap.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
