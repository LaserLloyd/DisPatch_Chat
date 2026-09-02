"""A live connection to the OpenClaw gateway.

WHY THIS EXISTS
---------------
DisPatch used to learn what an agent said by TAILING TRANSCRIPT FILES: a
watcher during the turn, a follower after it, a mirror poller for other
sessions, plus dedup and a sweep to paper over the gaps. Roughly 400 lines
whose only job was to compensate for polling a file.

It did not work, and could not. A delegated turn writes zero transcript bytes
while it waits on a subagent, so the follower's silence window expired during
exactly the case it existed for — measured at nine seconds short, losing
seventeen assistant blocks including the finished answer. And every re-read
path needed dedup that only compared the trailing assistant run, so a re-scan
replayed the day into the family chat.

The gateway emits ``session.message`` from the transcript-WRITE path — the same
source of truth the follower was reading, pushed at the moment it is written,
for every session, with no window and no polling. One subscription replaces the
watcher, the follower and the mirror.

WHAT THIS MODULE IS AND IS NOT
------------------------------
It is a transport: connect, authenticate, subscribe, hand frames to a callback,
reconnect, and never lose its place. It deliberately knows nothing about
sanitizers, reaction markers, Safe Mode or threads — that is product behaviour
and it stays in the delivery funnel, unchanged, so this migration cannot
quietly alter what a message means.

TWO HAZARDS THIS MODULE OWNS
----------------------------
1. TRUNCATION. ``session.message`` projects text through an 8000-character cap
   with no config knob, appending a truncation marker. The CLI path DisPatch
   used before had no such cap, so a naive switch starts silently cutting long
   replies mid-sentence — no error, no log, and the person who notices is a
   family member. Any projected body ending in the marker is refetched in full
   by id before it goes anywhere near the chat.

2. LOSS. The event is sent best-effort (``dropIfSlow``) and carries no
   frame-level sequence, so the generic gap detector does not apply. The only
   loss signal is ``messageSeq``, an absolute per-session counter. It is
   persisted per session; a skip or a reconnect triggers a backfill rather than
   a shrug.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

log = logging.getLogger("local-chat.gateway_ws")

# The wire protocol this client was written against. The gateway closes with
# 1002/PROTOCOL_MISMATCH if it cannot serve it — a loud failure, which is the
# one advantage this unexported interface has over reading files, where a
# format change would simply have looked like silence.
PROTOCOL = 4

# The gateway truncates projected message text at this many characters and
# appends the marker below. Both are hard-coded upstream with no config knob.
PROJECTION_MAX_CHARS = 8000
TRUNCATION_MARKER = "...(truncated)..."

CONNECT_TIMEOUT_S = 10.0
# Largest single gateway frame we will buffer. Was unlimited (`max_size=None`),
# which makes one hostile or broken frame an out-of-memory rather than a
# dropped connection. Replies run to tens of thousands of characters and a
# media listing is bigger again, so the ceiling is deliberately generous — it
# is a backstop, not a business rule.
MAX_FRAME_BYTES = 64 * 1024 * 1024
REQUEST_TIMEOUT_S = 30.0
RECONNECT_MIN_S = 1.0
RECONNECT_MAX_S = 60.0
# How long a connection must SURVIVE before its success counts. A gateway that
# accepts the socket, completes the handshake and then closes is "successful"
# by the old test, so the backoff reset to 1s and DisPatch hammered a degraded
# gateway at 1 Hz — connect, handshake, subscribe, resync, drop, forever — with
# the backoff that exists to prevent exactly that never getting off the floor.
HEALTHY_CONNECTION_S = 30.0


class GatewayDisconnected(ConnectionError):
    """The connection went away with requests still outstanding."""


class GatewayRunRefused(RuntimeError):
    """The gateway never ACCEPTED the run, so it never started.

    The distinction is the whole point: an `agent` request is answered twice —
    an acceptance receipt, then the finished turn. A failure before the
    acceptance means no model was called and nothing was written, so the caller
    may retry. A failure after it means a turn is (or was) underway and a retry
    would double-run it.
    """


class GatewayRunTimeout(TimeoutError):
    """We stopped waiting for a run that the gateway had already accepted."""


# How long to wait for the acceptance receipt on an `agent` request. The
# gateway answers this in milliseconds on loopback (measured: 24 ms); a long
# wait here means the gateway is not taking work, which is exactly the
# condition the caller retries.
AGENT_ACCEPT_TIMEOUT_S = 30.0


def gateway_url() -> str:
    return os.environ.get("OPENCLAW_GATEWAY_URL") or "ws://127.0.0.1:18789"


def gateway_token(config_path: Path | None = None) -> str | None:
    """The gateway's shared secret.

    Read at CONNECT time, never at import: the token can be rotated while
    DisPatch is running, and a value captured at import would turn that into a
    permanent authentication failure that survives every reconnect.

    Env wins over the config file so an operator can override without editing
    a file the gateway also writes.
    """
    env = os.environ.get("OPENCLAW_GATEWAY_TOKEN")
    if env:
        return env
    p = config_path or (Path.home() / ".openclaw" / "openclaw.json")
    try:
        cfg = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    gw = cfg.get("gateway") or {}
    auth = gw.get("auth") or {}
    tok = auth.get("token")
    return tok if isinstance(tok, str) and tok else None


class _AgentCall:
    """One in-flight turn dispatch: its acceptance, and its finished result."""

    __slots__ = ("final", "accepted", "accepted_payload")

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.final: asyncio.Future = loop.create_future()
        # An Event, not a flag: the caller must be able to WAIT for the receipt
        # without polling, and must stop waiting for it the moment it lands
        # rather than sitting out the whole acceptance window.
        self.accepted = asyncio.Event()
        self.accepted_payload: dict | None = None


class GatewayClient:
    """One connection, kept alive.

    Usage::

        client = GatewayClient(on_event=handler)
        await client.start()

    `on_event(event_name, payload)` is awaited for every subscribed event. It
    must not raise; an exception there would otherwise kill the reader and
    silently stop delivery, which is the failure mode this whole module exists
    to remove.
    """

    def __init__(
        self,
        on_event: Callable[[str, dict], Awaitable[None]],
        *,
        url: str | None = None,
        token: str | None = None,
        on_connect: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._on_event = on_event
        self._on_connect = on_connect
        self._url = url or gateway_url()
        self._token = token
        self._ws: Any = None
        self._task: asyncio.Task | None = None
        self._pump: asyncio.Task | None = None
        self._pending: dict[str, asyncio.Future] = {}
        # `agent` requests answer TWICE on one id (accepted, then final), so
        # they cannot share the single-response map above without the
        # acceptance resolving the call and the real answer arriving to nobody.
        self._agent_calls: dict[str, _AgentCall] = {}
        self._stop = asyncio.Event()
        self.hello: dict | None = None
        self.connected = asyncio.Event()
        # THE READER MUST NEVER AWAIT THE HANDLER.
        #
        # `call()` resolves its future from the reader loop. A handler that
        # calls back into the gateway — full_text() for a truncated reply,
        # history() for a gap — waits for a response frame that only the
        # blocked reader could deliver. Demonstrated, not theorised: the
        # response is already in the stream and is consumed after the request
        # times out 30s later. Both hazard handlers were dead on arrival, and
        # each attempt blinded the socket for the whole timeout.
        #
        # One queue, one consumer: ordering is preserved (a task per event
        # would not), memory is bounded, and a full queue is COUNTED rather
        # than silently dropped.
        self._events: asyncio.Queue = asyncio.Queue(maxsize=512)
        self.dropped_local = 0

    # -- lifecycle --------------------------------------------------------- #

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._run_forever())

    async def stop(self) -> None:
        self._stop.set()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
        if self._pump is not None:
            self._pump.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._pump
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def _run_forever(self) -> None:
        """Reconnect for ever, with backoff.

        Every reconnect MUST re-subscribe: subscriptions live on the connection
        and are dropped when it closes. A client that reconnects and forgets to
        re-subscribe looks perfectly healthy and receives nothing — the exact
        shape of failure that made the old design so hard to notice.
        """
        delay = RECONNECT_MIN_S
        while not self._stop.is_set():
            started = asyncio.get_event_loop().time()
            try:
                await self._connect_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("gateway connection failed (%s); retrying in %.0fs",
                            e, delay)
            finally:
                self.connected.clear()
                # A DEAD SOCKET MUST NOT LOOK LIKE A LIVE ONE. `call()` reads
                # self._ws, and nothing used to clear it or fail the futures
                # waiting on the reader that just exited — so a refetch
                # outstanding at disconnect stalled the single event consumer
                # for the whole 30s request timeout while the queue filled.
                self._ws = None
                self._fail_pending(GatewayDisconnected(
                    "gateway connection lost before the response arrived"))
            # Only a connection that STAYED UP counts as good (see
            # HEALTHY_CONNECTION_S).
            if asyncio.get_event_loop().time() - started >= HEALTHY_CONNECTION_S:
                delay = RECONNECT_MIN_S
            if self._stop.is_set():
                return
            await asyncio.sleep(delay)
            delay = min(delay * 2, RECONNECT_MAX_S)

    async def _connect_once(self) -> None:
        import websockets  # imported late: optional dep

        token = self._token or gateway_token()
        if not token:
            raise RuntimeError(
                "no gateway token (set OPENCLAW_GATEWAY_TOKEN or "
                "gateway.auth.token) — the gateway has NO loopback exemption")

        # No Origin header: an Origin marks the caller as a browser and routes
        # it into the Control-UI origin checks instead of the local-backend path.
        async with websockets.connect(
            self._url, open_timeout=CONNECT_TIMEOUT_S, max_size=MAX_FRAME_BYTES,
            origin=None,
        ) as ws:
            self._ws = ws
            hello = await self._handshake(ws, token)
            self.hello = hello
            self._assert_capabilities(hello)
            self.connected.set()
            log.info("gateway connected: protocol=%s scopes=%s",
                     hello.get("protocol"), (hello.get("auth") or {}).get("scopes"))
            if self._pump is None or self._pump.done():
                self._pump = asyncio.create_task(self._pump_events())
            # THE READER STARTS FIRST. subscribe_sessions() is a `call()`, and a
            # call waits for a response frame that only the reader can deliver —
            # so subscribing before the reader is running deadlocks for the full
            # request timeout and then reconnects, forever. (Observed: connect
            # 16:36:26, failure 16:36:56, reconnect 16:36:57.) This is the same
            # trap as awaiting a handler inside the reader, one level up.
            reader = asyncio.create_task(self._reader(ws))
            try:
                # RE-SUBSCRIBE ON EVERY CONNECT, from inside the client: a
                # caller that subscribes once after start() cannot survive a
                # reconnect, because subscriptions live on the socket. The
                # failure mode is a client that is connected, logs healthily,
                # and receives nothing.
                if self._on_connect is not None:
                    await self._on_connect()
                await reader
            finally:
                reader.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await reader

    async def _handshake(self, ws: Any, token: str) -> dict:
        """connect must be the FIRST request frame, or the socket is closed.

        The server also sends an unsolicited `connect.challenge` on connect;
        it matters only for device-keypair clients, so it is consumed and
        ignored rather than waited for.
        """
        req_id = uuid.uuid4().hex
        await ws.send(json.dumps({
            "type": "req", "id": req_id, "method": "connect",
            "params": {
                "minProtocol": PROTOCOL, "maxProtocol": PROTOCOL,
                "role": "operator",
                # Scopes must be REQUESTED. Omitting them yields a handshake
                # that succeeds and is completely inert — connected, subscribed
                # to nothing, receiving nothing.
                "scopes": ["operator.read", "operator.write"],
                "auth": {"token": token},
                # This exact identity + mode over clean loopback is what skips
                # device pairing. Any other pairing lands in an approval queue
                # and the connection is useless until a human intervenes.
                "client": {
                    "id": "gateway-client", "mode": "backend",
                    "version": "dispatch-chat", "platform": "linux",
                },
            },
        }))
        deadline = asyncio.get_event_loop().time() + CONNECT_TIMEOUT_S
        while asyncio.get_event_loop().time() < deadline:
            raw = await asyncio.wait_for(ws.recv(), timeout=CONNECT_TIMEOUT_S)
            frame = json.loads(raw)
            if not isinstance(frame, dict):
                continue
            if frame.get("type") == "event":
                continue                          # connect.challenge — ignored
            if frame.get("type") == "res" and frame.get("id") == req_id:
                if not frame.get("ok"):
                    raise RuntimeError(f"gateway refused connect: {frame.get('error')}")
                return frame.get("payload") or {}
        raise TimeoutError("gateway did not answer connect")

    @staticmethod
    def _assert_capabilities(hello: dict) -> None:
        """Fail LOUDLY if the gateway cannot do what we depend on.

        The hello frame lists every method and event the server supports. This
        is the compensation for building on an unexported interface: an
        `openclaw update` that removes something breaks at startup with a clear
        message, instead of degrading into the silent no-delivery that the file
        follower used to produce.

        AN EMPTY LIST IS A FAILURE, NOT A PASS. The check used to read
        `n not in methods` guarded by `methods and` — so if `features.methods`
        were renamed, moved or dropped, `methods` came back empty, every term
        was skipped and the guard reported all-present. The one update most
        likely to break us (the one that reshapes the hello frame) was the one
        it was guaranteed to wave through: a tripwire that disarms itself
        exactly when it is needed. Absent or empty now fails, naming what was
        expected so the message says more than "something moved".
        """
        feats = hello.get("features") or {}
        methods = set(feats.get("methods") or [])
        events = set(feats.get("events") or [])
        need_m = {"sessions.subscribe", "chat.history", "chat.message.get"}
        need_e = {"session.message"}
        blank = [name for name, got in (("features.methods", methods),
                                        ("features.events", events)) if not got]
        if blank:
            raise RuntimeError(
                "gateway hello frame advertises no " + " or ".join(blank)
                + " — cannot verify the capabilities DisPatch depends on ("
                + ", ".join(sorted(need_m | need_e))
                + "); the protocol changed and delivery would silently stop")
        missing = sorted((need_m - methods) | (need_e - events))
        if missing:
            raise RuntimeError(
                "gateway is missing capabilities DisPatch depends on: "
                + ", ".join(missing)
                + " — the protocol changed; delivery would silently stop")

    # -- request / response ------------------------------------------------ #

    def _fail_pending(self, exc: BaseException) -> None:
        """Resolve every outstanding request, now, with the reason."""
        pending, self._pending = self._pending, {}
        for fut in pending.values():
            if not fut.done():
                fut.set_exception(exc)
        # Turn dispatches too, or a reply that can never arrive holds the
        # thread lock until the agent timeout — the socket is gone, the future
        # it was waiting on is only ours to resolve.
        agent_calls, self._agent_calls = self._agent_calls, {}
        for call in agent_calls.values():
            if not call.final.done():
                call.final.set_exception(exc)

    async def call(self, method: str, params: dict | None = None) -> dict:
        ws = self._ws
        if ws is None:
            raise GatewayDisconnected("not connected")
        req_id = uuid.uuid4().hex
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[req_id] = fut
        try:
            await ws.send(json.dumps(
                {"type": "req", "id": req_id, "method": method,
                 **({"params": params} if params is not None else {})}))
            return await asyncio.wait_for(fut, timeout=REQUEST_TIMEOUT_S)
        finally:
            self._pending.pop(req_id, None)

    def _handle_agent_res(self, frame: dict) -> None:
        """One `agent` request, two responses.

        The first carries ``status: "accepted"`` and means only that the
        gateway took the work; the turn's result comes later on the SAME id.
        Resolving on the first frame — which is what the generic single-shot
        `call()` does — returns an empty receipt as if it were the reply, and
        the real answer then arrives for a request nobody is waiting on.
        """
        call = self._agent_calls.get(frame.get("id"))
        if call is None or call.final.done():
            return
        payload = frame.get("payload") or {}
        if frame.get("ok") and payload.get("status") == "accepted":
            call.accepted_payload = payload
            call.accepted.set()
            return
        if frame.get("ok"):
            call.final.set_result(payload)
        else:
            call.final.set_exception(RuntimeError(str(frame.get("error"))))

    async def call_agent(self, params: dict, *, timeout: float) -> dict:
        """Dispatch an agent turn and wait for its result.

        This is the same request the `openclaw agent` CLI makes — the CLI is a
        Node process that boots, reads the config, opens its own socket to this
        very gateway and sends exactly this frame. Measured on this box, that
        wrapper costs 2.3 s before the run even starts, on every single turn,
        while a socket to the gateway is already open and idle two feet away.

        Two waits, not one, because the two failures mean opposite things to a
        retrying caller:

        - ``GatewayRunRefused`` — no acceptance receipt, so nothing ran. Safe
          to retry, and the only class the caller's backoff should retry.
        - ``GatewayRunTimeout`` / ``GatewayDisconnected`` — accepted, so a turn
          is (or was) underway on the gateway. Retrying double-runs a billed
          model call; the reply, if it lands, arrives over the session
          subscription like any other.
        """
        ws = self._ws
        if ws is None:
            raise GatewayRunRefused("not connected to the gateway")
        req_id = uuid.uuid4().hex
        call = _AgentCall(asyncio.get_event_loop())
        self._agent_calls[req_id] = call
        try:
            await ws.send(json.dumps(
                {"type": "req", "id": req_id, "method": "agent", "params": params}))
            accepted = asyncio.ensure_future(call.accepted.wait())
            try:
                done, _ = await asyncio.wait(
                    {accepted, call.final},
                    timeout=min(AGENT_ACCEPT_TIMEOUT_S, timeout),
                    return_when=asyncio.FIRST_COMPLETED)
            finally:
                accepted.cancel()
            if not call.accepted.is_set():
                if call.final.done():
                    # Answered without ever accepting: a refusal at the door.
                    exc = call.final.exception()
                    raise GatewayRunRefused(
                        str(exc) if exc else "the gateway refused the run")
                raise GatewayRunRefused(
                    "the gateway did not accept the run within "
                    f"{min(AGENT_ACCEPT_TIMEOUT_S, timeout):.0f}s")
            if call.final.done():
                return call.final.result()
            try:
                return await asyncio.wait_for(call.final, timeout=timeout)
            except TimeoutError:
                raise GatewayRunTimeout(
                    f"the run did not finish within {timeout:.0f}s") from None
        finally:
            self._agent_calls.pop(req_id, None)

    async def _reader(self, ws: Any) -> None:
        async for raw in ws:
            try:
                frame = json.loads(raw)
            except json.JSONDecodeError:
                continue
            # Valid JSON is not necessarily a frame: `[]`, `"hi"` and `null`
            # all parse, and `.get` on them raised AttributeError out of the
            # reader — which ends the socket and costs a full reconnect (and,
            # with it, the subscription) over one malformed line.
            if not isinstance(frame, dict):
                log.warning("ignoring a non-object gateway frame (%s)",
                            type(frame).__name__)
                continue
            kind = frame.get("type")
            if kind == "res" and frame.get("id") in self._agent_calls:
                self._handle_agent_res(frame)
                continue
            if kind == "res":
                fut = self._pending.get(frame.get("id"))
                if fut is not None and not fut.done():
                    if frame.get("ok"):
                        fut.set_result(frame.get("payload") or {})
                    else:
                        fut.set_exception(RuntimeError(str(frame.get("error"))))
            elif kind == "event":
                try:
                    self._events.put_nowait(
                        (frame.get("event") or "", frame.get("payload") or {}))
                except asyncio.QueueFull:
                    # Visible loss beats silent loss. If this ever prints, the
                    # consumer is slower than the firehose and the size is the
                    # thing to argue about — not whether messages went missing.
                    self.dropped_local += 1
                    log.error("event queue full; dropped %s (total %d)",
                              frame.get("event"), self.dropped_local)

    async def _pump_events(self) -> None:
        """Drain the queue, one at a time, off the reader's critical path.

        Single consumer on purpose: `session.message` order within a session is
        the product behaviour, and concurrent handlers would shuffle it.
        """
        while True:
            event, payload = await self._events.get()
            try:
                await self._on_event(event, payload)
            except Exception:
                # A handler that raises must not take the pump down with it —
                # that would stop delivery for every session at once.
                log.exception("gateway event handler failed: %s", event)

    # -- the two hazards --------------------------------------------------- #

    @staticmethod
    def looks_truncated(text: str) -> bool:
        return bool(text) and text.rstrip().endswith(TRUNCATION_MARKER)

    @staticmethod
    def blocks_truncated(message: dict) -> bool:
        """True if ANY content block was cut short.

        The gateway truncates per block, and text_of joins blocks with a blank
        line — so a truncated block that is not the last one leaves the marker
        buried mid-string, where an endswith() check cannot see it.

        Only `text` blocks count, because only those are what text_of reads.
        Scanning every block type meant a marker in a tool_use argument or a
        thinking block ordered a refetch that could not change the delivered
        text, and each one incremented stats["refetched"] — a repair counter
        reporting work it did not do.
        """
        content = message.get("content")
        if isinstance(content, str):
            return GatewayClient.looks_truncated(content)
        if isinstance(content, list):
            return any(TRUNCATION_MARKER in (b.get("text") or "")
                       for b in content
                       if isinstance(b, dict) and b.get("type") == "text")
        return False

    async def full_text(self, session_key: str, message_id: str) -> str | None:
        """Refetch a message body the projection cut short.

        Without this, a long reply silently loses everything past 8000
        characters and looks exactly like a normal message that happened to
        stop. There is no error and nothing to test against downstream, which
        is why it is handled here at the only point that can still tell.
        """
        try:
            res = await self.call("chat.message.get", {
                "sessionKey": session_key, "messageId": message_id,
                "maxChars": 1_000_000,
            })
        except Exception:
            log.warning("could not refetch %s in full; keeping the projection",
                        message_id, exc_info=True)
            return None
        # `ok:false` carries a reason (not_visible / oversized). Folding that
        # into the same None as a transport error hides a refusal as a miss.
        if res.get("ok") is False:
            log.warning("refetch of %s unavailable: %s", message_id,
                        res.get("unavailableReason"))
            return None
        msg = res.get("message") or res
        if not isinstance(msg, dict):
            return None
        # A real assistant message has NO "text" key; `content` is a list of
        # typed blocks. Reading .get("text") returned None for every message
        # ever refetched, so repair reported success-by-silence and the
        # truncated projection went out anyway.
        from .gateway_router import text_of
        return text_of(msg) or None

    async def history(self, session_key: str, *, limit: int = 50,
                      offset: int = 0) -> list[dict]:
        """Backfill after a gap or a reconnect.

        `offset` is ALWAYS passed, even as 0. Omitting it switches the gateway
        to a tail read whose sequence numbers are window-relative rather than
        absolute — the same message reports different numbers on different
        calls, which would make the gap detector's cursor meaningless.
        """
        res = await self.call("chat.history", {
            "sessionKey": session_key, "limit": limit, "offset": offset})
        msgs = res.get("messages")
        return msgs if isinstance(msgs, list) else []

    async def subscribe_sessions(self) -> None:
        """One subscription; every session's messages.

        This single call is what replaces the watcher, the follower AND the
        mirror poller.
        """
        await self.call("sessions.subscribe", None)
        log.info("subscribed to session events")
