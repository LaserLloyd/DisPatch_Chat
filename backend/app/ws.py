"""WebSocket connection manager.

Single user, but possibly several devices/tabs open at once (phone + desktop).
Every server event is broadcast to all connected clients; the frontend decides
what is relevant to the view it currently shows. This keeps every open client
in sync (e.g. a proactive daily message from OpenClaw appears everywhere).

Each connection carries its principal (who it belongs to) and, for a signed-in socket, the
session token. Every outgoing frame passes through :attr:`ConnectionManager.scoper` with
that principal (4.0 ownership and character visibility). A connection whose session has
since lapsed (checked via :attr:`is_session_live`), or a token-less one on an install that
has gained a PIN since, is scoped as if it had NO principal - it keeps only frames about
nothing in particular - until it reconnects.

4.3 (#33): a token-less ``profile`` socket is an unsigned device on the lock-target default
profile; it has no session to lapse. Each broadcast computes a generation key
(``account_rev``, ``security.yaml`` mtime) and, when it moved, resolves ``policy.unsigned()``
once. A default-profile socket whose (kind, profile id, role, permissions) no longer matches
is sent ``locked`` and loses its principal, at the first broadcast after the change - a tab
that only listens no longer keeps the profile's frames until it next speaks.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable

from fastapi import WebSocket


class ConnectionManager:
    def __init__(self) -> None:
        # ws -> {"token": str | None, "lock": asyncio.Lock, "principal": Principal | None}
        self._conns: dict[WebSocket, dict] = {}
        self._lock = asyncio.Lock()
        # Injected at startup (avoids a circular import on main).
        # (frame, principal) -> frame | None. Applied to every live socket: ownership and
        # per-profile character visibility (4.0). A profile must not learn another's
        # chats, or a character it may not use, from a later `bots` / `thread_*` /
        # `message` broadcast.
        self.scoper: Callable[[dict, object], dict | None] | None = None
        self.is_session_live: Callable[[str], bool] | None = None
        # (frame) -> awaitable, run before every fan-out and targeted send (4.0 ownership):
        # it caches the owner of the thread a frame names, so the synchronous scoper can
        # send a thread frame to its owner's sockets only - and drop one it cannot attribute.
        self.prime: Callable[[dict], Awaitable[None]] | None = None
        self.pin_set: Callable[[], bool] | None = None
        # Synchronous observers of every broadcast frame (app/extensions.py:
        # the open-stream snapshot and per-thread frame subscriptions). Called
        # in broadcast order before the fan-out; each must be cheap and must
        # never block — the extensions host only enqueues.
        self._observers: list[Callable[[dict], None]] = []
        # 4.3 (#33): (generation key, policy.unsigned() under it). The key names the data
        # files too, so a manager that outlives one data dir (tests) never reuses another's.
        self._unsigned_cache: tuple[object, object] | None = None
        self._rev_con: tuple[tuple, object] | None = None   # ((path, inode), read-only conn)
        # 4.3 FX2 (#33 promptness): the loop the sockets live on (captured at connect) and the
        # re-check tasks ``nudge`` started (kept so they are not garbage-collected mid-run).
        self._loop: asyncio.AbstractEventLoop | None = None
        self._nudges: set[asyncio.Task] = set()

    def add_observer(self, fn: Callable[[dict], None]) -> None:
        if fn not in self._observers:
            self._observers.append(fn)

    def remove_observer(self, fn: Callable[[dict], None]) -> None:
        if fn in self._observers:
            self._observers.remove(fn)

    def frame_for_token(self, token: str | None, message: dict, principal=None) -> dict | None:
        """The frame a FULL connection opened under ``token`` would receive
        right now — redacted (or None = dropped) the instant that session has
        lapsed, exactly as for a browser tab of the same session.

        Used by the add-on host's per-thread subscriptions, whose thread was owner-checked
        when the subscribing socket opened; with ``principal`` given the per-profile scoper
        runs too (4.0)."""
        meta = {"token": token, "principal": principal}
        return self._frame_for(meta, message, scoped=principal is not None)

    async def connect(self, ws: WebSocket, token: str | None = None, principal=None) -> None:
        await ws.accept()
        self._loop = asyncio.get_running_loop()
        async with self._lock:
            # The per-connection lock serializes every send to this socket, so
            # concurrent broadcasts / direct sends can never interleave a
            # frame's bytes on the wire or reorder frames for one client.
            # ``principal`` is who the socket belongs to (the scoper's input).
            self._conns[ws] = {"token": token, "lock": asyncio.Lock(), "principal": principal}

    def principal_of(self, ws: WebSocket):
        meta = self._conns.get(ws)
        return meta.get("principal") if meta else None

    def set_principal(self, ws: WebSocket, principal) -> None:
        meta = self._conns.get(ws)
        if meta is not None:
            meta["principal"] = principal

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self._conns.pop(ws, None)

    @staticmethod
    def _is_default_socket(meta: dict) -> bool:
        """A token-less ``profile`` principal: the lock-target default profile (4.0)."""
        pr = meta.get("principal")
        return pr is not None and getattr(pr, "kind", "") == "profile" and not meta.get("token")

    def _read_rev(self, path) -> int:
        """``account_rev`` through one kept read-only connection per accounts file (a new
        connection per broadcast cost ~0.4 ms on the event loop). The first read of a path
        goes through ``accounts.connect`` (schema + the live-db guard under pytest)."""
        import sqlite3

        from . import accounts
        try:
            ino = path.stat().st_ino
        except OSError:
            ino = None
        # The inode too: a file swapped in place (a restore) is read afresh.
        key = (str(path), ino)
        held = self._rev_con
        if held is None or held[0] != key or ino is None:
            if held is not None:
                with contextlib.suppress(Exception):
                    held[1].close()
                self._rev_con = None
            with accounts.connect(path) as con:
                rev = accounts._read_rev(con)
            ro = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0,
                                 isolation_level=None, check_same_thread=False)
            self._rev_con = (key, ro)
            return rev
        try:
            row = held[1].execute("SELECT rev FROM account_rev WHERE id = 1").fetchone()
        except Exception:
            with contextlib.suppress(Exception):
                held[1].close()
            self._rev_con = None
            raise
        return int(row[0]) if row else 0

    def _generation_key(self) -> object:
        """(accounts db, ``account_rev``, security.yaml, its mtime). Every write to
        ``profiles`` bumps ``account_rev`` (accounts.py); the lock target lives in
        security.yaml. None when either cannot be read: the caller then re-resolves."""
        from . import accounts, auth
        try:
            path = accounts.db_path()
            rev = self._read_rev(path)
            sec = auth.SECURITY_PATH
            try:
                mtime = sec.stat().st_mtime_ns
            except FileNotFoundError:
                mtime = None
            return (str(path), rev, str(sec), mtime)
        except Exception:
            return None

    def _refresh_unsigned(self) -> None:
        """Re-resolve who an unsigned device is, once per generation (start of a broadcast).
        Cheap when no default-profile socket is open: nothing is read."""
        if not any(self._is_default_socket(m) for m in tuple(self._conns.values())):
            self._unsigned_cache = None         # stale by the time a default socket opens
            return
        key = self._generation_key()
        cached = self._unsigned_cache
        if key is not None and cached is not None and cached[0] == key:
            return
        from . import accounts, policy
        if cached is None or key is None or cached[0] is None or cached[0][:2] != key[:2]:
            # Another process (the ``users`` command line) may have written: accounts'
            # own cache only re-reads account_rev once a second.
            accounts.invalidate()
        try:
            fresh = policy.unsigned()
        except Exception:
            fresh = None                        # fail closed: every default socket lapses
        self._unsigned_cache = (key, fresh)

    def _unsigned_now(self):
        if self._unsigned_cache is None:
            self._refresh_unsigned()
        cached = self._unsigned_cache
        return cached[1] if cached is not None else None

    @staticmethod
    def _identity(pr) -> tuple:
        return (getattr(pr, "kind", None), getattr(pr, "profile_id", None),
                getattr(pr, "role", None), getattr(pr, "permissions", None))

    def _default_lapsed(self, meta: dict) -> bool:
        """A default-profile socket is lapsed when the current lock-target resolution is
        nobody or a different identity (another profile, role or permission set)."""
        fresh = self._unsigned_now()
        if fresh is None:
            return True
        pr = meta.get("principal")
        if self._identity(fresh) != self._identity(pr):
            return True
        if getattr(fresh, "profile", None) is not getattr(pr, "profile", None):
            # Same identity, newer row (e.g. its household list): scope with the fresh one.
            meta["principal"] = fresh
        return False

    def _lapsed(self, meta: dict) -> bool:
        """Has this connection lost the identity it was opened with?"""
        if self._is_default_socket(meta):
            # A profile with no token is the lock-target default profile an unsigned device
            # landed on (4.0): no session to lapse, so it follows policy.unsigned() (4.3).
            return self._default_lapsed(meta)
        # A connection whose session has lapsed loses its principal immediately (don't
        # wait for it to send a frame).
        tok = meta.get("token")
        if tok and self.is_session_live is not None and not self.is_session_live(tok):
            return True
        # A token-less full connection only exists while no PIN is set. If a
        # PIN has been set since the handshake, demote it immediately — it must
        # not keep receiving the open install's frames just because it stayed open.
        if not tok and self.pin_set is not None and self.pin_set():
            return True
        return False

    def _frame_for(self, meta: dict, message: dict, *, targeted: bool = False,
                   scoped: bool = True) -> dict | None:
        """``targeted``: a reply to this socket's own request (``send``), not a broadcast -
        the scoper lets a neutral reply about a thread that does not exist through.

        4.0: the scoper runs for EVERY live connection, including one with no principal
        yet (it then drops anything about a thread, a character or an operator surface:
        Fable amendment 15 - the 3.x manager skipped the scoper for those)."""
        if self._lapsed(meta):
            # Scoped as nobody, whatever ``scoped`` says: frames about a thread, a character
            # or an operator surface are dropped (fail closed when there is no scoper).
            return self.scoper(message, None, targeted=targeted) if self.scoper is not None else None
        if scoped and self.scoper is not None:
            return self.scoper(message, meta.get("principal"), targeted=targeted)  # None -> drop
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
        (The `_ACK_SEEN` entry now also records whether the turn was actually
        scheduled, so a resend finishes the job rather than confirming it.)
        """
        meta = self._conns.get(ws)
        if meta is None:
            return
        await self._prime(message)
        self._refresh_unsigned()
        frame = self._frame_for(meta, message, targeted=True)
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

    async def _prime(self, message: dict) -> None:
        if self.prime is None:
            return
        try:
            await self.prime(message)
        except Exception:      # an unprimed frame is dropped by the scoper (fail closed)
            pass

    def _plan(self, ws: WebSocket, meta: dict, message: dict):
        """The send for one recipient of a broadcast, or None. A default-profile socket that
        lapsed (4.3) gets ``locked`` instead of the frame and loses its principal, exactly
        as the /ws loop does on its next receive; later frames are scoped as nobody."""
        if self._is_default_socket(meta) and self._lapsed(meta):
            meta["principal"] = None
            return self._send_or_reap(ws, meta, {"type": "locked"})
        frame = self._frame_for(meta, message)
        if frame is None:
            return None
        return self._send_or_reap(ws, meta, frame)

    def nudge(self) -> None:
        """4.3 FX2 (#33): a write that can change who an unsigned device is (the default
        profile disabled, given a PIN, its permissions changed; the lock target moved) just
        happened. Re-check the default-profile sockets NOW instead of at the next unrelated
        broadcast (15-20 s+ on a quiet install). Callable from any thread and from sync code
        (the write paths are both); a no-op before any socket connected. Sends nothing but
        ``locked`` to a socket that lapsed - see :meth:`recheck_default`."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None

        def spawn() -> None:
            task = loop.create_task(self.recheck_default())
            self._nudges.add(task)
            task.add_done_callback(self._nudges.discard)
        if running is loop:
            spawn()
        else:
            with contextlib.suppress(RuntimeError):     # the loop closed in between
                loop.call_soon_threadsafe(spawn)

    async def recheck_default(self) -> int:
        """Run A2's lapse check on every default-profile socket without a frame to deliver:
        a lapsed one gets ``locked`` and loses its principal (as :meth:`_plan` does), every
        other socket gets nothing - no profile data ever leaves through this path. Returns
        how many sockets were locked."""
        self._refresh_unsigned()
        async with self._lock:
            targets = [(ws, m) for ws, m in self._conns.items() if self._is_default_socket(m)]
        sends = []
        for ws, meta in targets:
            if self._lapsed(meta):
                meta["principal"] = None
                sends.append(self._send_or_reap(ws, meta, {"type": "locked"}))
        if not sends:
            return 0
        results = await asyncio.gather(*sends)
        dead = [ws for ws in results if ws is not None]
        if dead:
            async with self._lock:
                for ws in dead:
                    self._conns.pop(ws, None)
        return len(sends)

    async def broadcast(self, message: dict) -> None:
        await self._prime(message)
        self._refresh_unsigned()
        for fn in tuple(self._observers):
            try:
                fn(message)
            except Exception:  # an observer must never cost the chat a frame
                pass
        async with self._lock:
            targets = list(self._conns.items())
        sends = []
        for ws, meta in targets:
            plan = self._plan(ws, meta, message)
            if plan is not None:
                sends.append(plan)
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

    async def broadcast_where(self, message: dict,
                              wants: Callable[[object], bool]) -> int:
        """Send ``message`` ONLY to sockets whose principal satisfies ``wants``.

        For frames that are private to a person (agent task events and approval
        requests): the global :meth:`broadcast` is "everyone, scoped", which is the wrong
        default for something only a task's owner may see. A socket with no principal (it
        has not finished its handshake) never matches, a lapsed socket is also dropped by
        the scoper (:meth:`_frame_for`), and the observers - the add-on host's open-stream
        snapshot - are deliberately NOT called: a task frame is not conversation.
        Returns how many sockets were sent to.
        """
        await self._prime(message)
        self._refresh_unsigned()
        async with self._lock:
            targets = list(self._conns.items())
        sends = []
        for ws, meta in targets:
            pr = meta.get("principal")
            if pr is None:
                continue
            try:
                if not wants(pr):
                    continue
            except Exception:
                continue
            plan = self._plan(ws, meta, message)
            if plan is not None:
                sends.append(plan)
        if not sends:
            return 0
        results = await asyncio.gather(*sends)
        dead = [ws for ws in results if ws is not None]
        if dead:
            async with self._lock:
                for ws in dead:
                    self._conns.pop(ws, None)
        return len(sends) - len(dead)

    @property
    def count(self) -> int:
        return len(self._conns)


manager = ConnectionManager()
