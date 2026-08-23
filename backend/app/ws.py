"""WebSocket connection manager.

Single user, but possibly several devices/tabs open at once (phone + desktop).
Every server event is broadcast to all connected clients; the frontend decides
what is relevant to the view it currently shows. This keeps every open client
in sync (e.g. a proactive daily message from OpenClaw appears everywhere).

Each connection carries a ``decoy`` flag and (for full connections) the session
token. Outgoing frames are passed through :attr:`ConnectionManager.redactor`
for any connection that is effectively Safe Mode — either flagged decoy, OR a
full connection whose session has since expired (checked via
:attr:`is_session_live`). So a Safe-Mode / lapsed client never receives
image/media payloads, even on a broadcast that a full client also receives.
The redactor may return ``None`` to mean "drop this frame for Safe Mode".
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from fastapi import WebSocket


class ConnectionManager:
    def __init__(self) -> None:
        # ws -> {"decoy": bool, "token": str | None, "lock": asyncio.Lock}
        self._conns: dict[WebSocket, dict] = {}
        self._lock = asyncio.Lock()
        # Injected at startup (avoids a circular import on main).
        self.redactor: Callable[[dict], dict | None] | None = None
        self.is_session_live: Callable[[str], bool] | None = None
        self.pin_set: Callable[[], bool] | None = None

    async def connect(self, ws: WebSocket, decoy: bool = False, token: str | None = None) -> None:
        await ws.accept()
        async with self._lock:
            # The per-connection lock serializes every send to this socket, so
            # concurrent broadcasts / direct sends can never interleave a
            # frame's bytes on the wire or reorder frames for one client.
            self._conns[ws] = {"decoy": decoy, "token": token, "lock": asyncio.Lock()}

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self._conns.pop(ws, None)

    def _is_safe(self, meta: dict) -> bool:
        if meta.get("decoy"):
            return True
        # A full connection whose session has lapsed is treated as Safe Mode
        # immediately (don't wait for it to send a frame).
        tok = meta.get("token")
        if tok and self.is_session_live is not None and not self.is_session_live(tok):
            return True
        # A token-less full connection only exists while no PIN is set. If a
        # PIN has been set since the handshake, demote it immediately — it must
        # not keep receiving unredacted frames just because it stayed open.
        if not tok and self.pin_set is not None and self.pin_set():
            return True
        return False

    def _frame_for(self, meta: dict, message: dict) -> dict | None:
        if self._is_safe(meta) and self.redactor is not None:
            return self.redactor(message)   # may be None -> drop
        return message

    async def send(self, ws: WebSocket, message: dict) -> None:
        """Bounded targeted send. Mirrors broadcast()'s reaping.

        This used to await ``send_json`` under the connection lock with no
        timeout. A backgrounded phone tab that stops reading (TCP zero-window)
        keeps its socket open, so the await parked indefinitely — and because
        ``_handle_send`` acks the client BEFORE scheduling the agent turn, the
        turn was never scheduled at all. The message was already recorded in
        ``_ACK_SEEN``, so the client's reconnect-and-resend was answered with a
        bare re-ack and the reply was lost for good. Bounding it here means a
        stalled client is dropped instead of taking the turn down with it.
        """
        meta = self._conns.get(ws)
        if meta is None:
            return
        frame = self._frame_for(meta, message)
        if frame is None:
            return
        stalled = await self._send_or_reap(ws, meta, frame)
        if stalled is not None:
            await self.disconnect(stalled)

    async def _send_or_reap(self, ws: WebSocket, meta: dict, frame: dict) -> WebSocket | None:
        """One bounded, per-connection-serialized send. Returns the socket when
        it should be dropped (stalled or failed), else None."""

        async def _locked_send() -> None:
            async with meta["lock"]:
                await ws.send_json(frame)

        try:
            # A client that stops reading (half-dead TCP) fills its write
            # buffer and would park send_json forever. Bounded wait — covering
            # the lock too, so a send queued behind an already-stalled one
            # can't wait unboundedly — and a stalled client is treated as dead.
            await asyncio.wait_for(_locked_send(), timeout=5.0)
            return None
        except Exception:
            return ws

    async def broadcast(self, message: dict) -> None:
        async with self._lock:
            targets = list(self._conns.items())
        sends = []
        for ws, meta in targets:
            frame = self._frame_for(meta, message)
            if frame is None:
                continue
            sends.append(self._send_or_reap(ws, meta, frame))
        if not sends:
            return
        # Concurrent fan-out: one stalled client must not delay delivery to
        # every other (healthy) client behind its 5s timeout. Per-connection
        # ordering is preserved twice over — each connection gets ONE frame per
        # broadcast, the manager awaits the whole gather before returning (so
        # sequential broadcasts stay sequential per client), and the meta lock
        # serializes any cross-task overlap on the same socket.
        results = await asyncio.gather(*sends)
        dead = [ws for ws in results if ws is not None]
        if dead:
            async with self._lock:
                for ws in dead:
                    self._conns.pop(ws, None)

    def conn_decoy(self, ws: WebSocket) -> bool:
        """Is this connection effectively Safe Mode (decoy or lapsed session)?"""
        meta = self._conns.get(ws)
        return self._is_safe(meta) if meta is not None else True

    @property
    def count(self) -> int:
        return len(self._conns)


manager = ConnectionManager()
