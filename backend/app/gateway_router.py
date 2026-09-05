"""Turn gateway session events into DisPatch messages.

This is the replacement for transcript tailing. The gateway emits
``session.message`` from the transcript-WRITE path — the same source of truth
the old file follower read, pushed the moment it is written, for every session,
with no window and no polling.

WHAT IT DOES NOT DO
-------------------
It does not sanitize, strip reaction markers, ingest media, decide Safe Mode, or
classify speech versus working output. All of that stays in
``_deliver_assistant_text``, untouched, so swapping the transport cannot quietly
change what a message MEANS. This module's entire job is: which thread, what
text, exactly once, in order.

THE THREE THINGS IT MUST GET RIGHT
----------------------------------
1. IDENTITY. Every message carries ``__openclaw.id`` — stable across the live
   event and a later ``chat.history`` read. That becomes ``source_id``, so a
   backfill that overlaps what was already delivered is a no-op rather than a
   replay that dumps dozens of duplicated messages into a thread at once.

2. TRUNCATION. The event's text is projected through an 8000-character cap that
   has no config knob. The old CLI path had no cap. Anything ending in the
   truncation marker is refetched in full before it goes anywhere — otherwise
   long replies silently stop mid-sentence with no error and no failing test.

3. GAPS. The event is best-effort (``dropIfSlow``) with no frame sequence, so
   the only loss signal is ``messageSeq``, an absolute per-session counter. A
   skip triggers a backfill rather than a shrug.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

log = logging.getLogger("local-chat.gateway_router")

# How many per-session cursors to keep. Bounded because the map is keyed by
# a gateway session id, of which a long-lived install accumulates any number.
SEQ_CURSOR_MAX = 4096

# How long a live `stopReason == "error"` assistant message is HELD before it is
# delivered. The gateway emits one of these the moment a turn errors — and for
# a context overflow it then auto-compacts, retries the prompt in the SAME run
# and deletes the synthetic error from its own transcript. Observed in practice:
# the large majority of "The agent run failed before producing a reply."
# placeholders are followed by a real reply 40-100 s later (compaction time),
# so persisting one on arrival turns a transient into a permanent failure
# bubble. A later assistant message in the same session cancels the hold; a
# turn that really died still shows its failure, just this much later.
ERROR_PLACEHOLDER_GRACE_S = 180.0

# Backfill window. `chat.history` is a paged tail read, so ONE call can only
# ever cover its own page — a gap wider than that used to be permanently
# unrecoverable by this transport while stats["backfilled"] read healthy.
# Pages are walked backwards until the cursor is reached, bounded by
# BACKFILL_MAX_MESSAGES so a corrupt cursor cannot ask for the whole history.
BACKFILL_PAGE = 100
BACKFILL_MAX_MESSAGES = 2000

# How long a detected gap waits before it is chased, and why it waits at all.
#
# MOST GAPS ARE NOT LOSS. `messageSeq` counts every transcript row, but the
# gateway only emits `session.message` for rows its display projection keeps —
# and a toolResult row is folded into the assistant message that called the
# tool, so it is counted and never emitted. Measured on this box: every one of
# the 32 "gaps" in a day was a jump of exactly 2 across a tool call, on a
# session that had lost nothing. Chasing each one inline cost a `chat.history`
# round trip ON THE PATH THE REPLY TRAVELS, and a tool-heavy turn produces one
# per tool call.
#
# So the gap is recorded, the reply goes out immediately, and the repair runs
# behind it — coalesced, because a turn that called eight tools produced eight
# "gaps" that one read covers. Every backfilled message carries a stable
# source_id, so overlapping the live delivery is a no-op by construction.
GAP_BACKFILL_DELAY_S = 6.0

# Roles the gateway emits. Anything else is bucketed, because
# stats["dropped_role_<role>"] minted a NEW key from a wire-supplied string and
# then served it in /api/health — unbounded growth from remote input.
KNOWN_ROLES = ("user", "assistant", "system", "tool", "toolResult")

# --- live deltas ----------------------------------------------------------- #
#
# `chat` events are the model's output as it is produced. They are sent
# `dropIfSlow`, carry no per-frame sequence, and are explicitly a DISPLAY
# channel: the transcript event (`session.message`) remains the only thing
# anything is ever persisted from. A dropped delta must therefore be able to
# cost nothing, which is why the text a client receives is derived from the
# CUMULATIVE `message.content[].text` snapshot on every event rather than from
# `deltaText`. Accumulating deltaText makes one dropped frame corrupt the rest
# of the reply, silently, with no way to notice.

# Smallest gap between two outgoing chunk frames for one run. The gateway
# already paces itself, but a fast local model still out-runs what a phone can
# usefully paint, and each chunk is a fan-out to every connected device. The
# last chunk is always flushed, so coalescing costs latency, never text.
CHUNK_MIN_INTERVAL_S = 0.1

# How long a run's provisional bubble survives its own terminal event without a
# persisted row arriving. After this the client is told the placeholder will
# never be filled, rather than being left with a bubble that streams and then
# hangs there for ever.
RUN_SETTLE_S = 10.0

# How many run records to keep. Bounded for the same reason the seq cursors
# are: the subscription is a firehose, and most runs on this box are not ours.
RUN_CACHE_MAX = 512


class _ChatRun:
    """One in-flight run's live-delta state.

    `target` is the resolved (thread_id, bot_id) or None for a run belonging to
    a session DisPatch does not own — cached either way, because resolving hits
    the database and a foreign session emits a delta several times a second.
    """

    __slots__ = (
        "flush",
        "pending",
        "replace",
        "sent",
        "settle",
        "started",
        "target",
    )

    def __init__(self, target: tuple[str, str] | None) -> None:
        self.target = target
        self.sent: str = ""          # sanitized cumulative text already sent
        self.pending: str | None = None
        self.replace: bool = False   # the gateway said this is not an extension
        self.flush: asyncio.Task | None = None
        self.started: bool = False   # stream_start has been broadcast
        self.settle: asyncio.Task | None = None


def text_of(message: dict) -> str:
    """The visible text of a gateway message.

    ``content`` is a list of typed blocks; only ``text`` blocks are speech.
    Reading ``message["text"]`` (which does not exist) yielded empty strings —
    a silent total loss that looks exactly like an agent saying nothing.
    """
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [b.get("text") or "" for b in content
                 if isinstance(b, dict) and b.get("type") == "text"]
        return "\n\n".join(p for p in parts if p.strip())
    txt = message.get("text")
    return txt if isinstance(txt, str) else ""


def source_id_of(message: dict, session_key: str = "") -> str | None:
    """Stable identity, or None.

    Same value on the live event and on a `chat.history` re-read, which is what
    makes backfill idempotent.

    SCOPED BY SESSION, DELIBERATELY. ``__openclaw.id`` is 8 hex characters — 32
    bits. A few thousand messages without a collision proves nothing here,
    because the birthday bound is what matters and it is not reassuring: at
    50,000 messages the chance of at least one collision is 25%, and at 100,000
    it is 69%. A chat database only ever grows.

    A collision here does not raise, log, or fail a test. ``source_id_seen()``
    returns True and the reply is dropped on the floor — the exact failure this
    module exists to end, reintroduced inside the mechanism meant to prevent it.

    The gateway only guarantees the id is unique WITHIN a session, so that is
    the scope we use. The session key is on the live event (``sessionKey``) and
    is the argument we pass to ``chat.history``, so the value stays identical
    across both paths and backfill stays idempotent.
    """
    oc = message.get("__openclaw")
    if isinstance(oc, dict):
        mid = oc.get("id")
        if isinstance(mid, str) and mid:
            return f"gw:{session_key}:{mid}" if session_key else f"gw:{mid}"
    return None


def created_at_of(message: dict) -> str | None:
    """The message's own time, so a late delivery lands where it belongs.

    A backfilled answer stamped "now" sorts to the bottom of a conversation
    that happened yesterday.
    """
    ts = message.get("timestamp")
    if isinstance(ts, (int, float)) and ts > 0:
        try:
            return datetime.fromtimestamp(ts / 1000, tz=UTC).isoformat()
        except (ValueError, OSError, OverflowError):
            return None
    return None


class SessionRouter:
    """Routes ``session.message`` events into a delivery callable.

    `resolve_thread(session_key)` returns `(thread_id, bot_id)` or None for a
    session DisPatch does not track — the subscription is a firehose over every
    session on the box, so most events are legitimately not ours.

    `deliver(thread_id, text, source_id, created_at, bot_id, live)` is the
    existing funnel. Nothing here bypasses it. ``live`` is False for backfilled
    messages — a replay, which the funnel dedups and de-fangs differently.
    """

    def __init__(
        self,
        resolve_thread: Callable[[str], Awaitable[tuple[str, str] | None]],
        deliver: Callable[..., Awaitable[Any]],
        *,
        client: Any = None,
        broadcast: Callable[[dict], Awaitable[None]] | None = None,
        sanitize: Callable[[str], str] | None = None,
        open_stream: Callable[[str, str], None] | None = None,
        close_stream: Callable[[str, str], bool] | None = None,
        stream_open: Callable[[str, str], bool] | None = None,
    ) -> None:
        self._resolve = resolve_thread
        self._deliver = deliver
        self._client = client
        # Live-delta wiring. All four are optional: with none of them the
        # router behaves exactly as it did before deltas existed, which is what
        # keeps the transcript path (the only path anything is persisted from)
        # provably unaffected by this feature.
        #
        # `sanitize` is NOT a nicety. Delta text is raw model output — it still
        # carries `[[media:/abs/path]]`, `[[doc:…]]`, `[[pic:…]]` and
        # `:react:…:` markers, and the internal-context scaffolding the persist
        # chokepoint removes. Broadcasting it unsanitized would put absolute
        # paths and internal markers on a LOCKED family device, which the
        # persisted copy has never done.
        self._broadcast = broadcast
        self._sanitize = sanitize or (lambda t: t)
        self._open_stream = open_stream
        self._close_stream = close_stream
        self._stream_open = stream_open
        # runId -> _ChatRun, LRU-bounded (see _run_for).
        self._runs: OrderedDict[str, _ChatRun] = OrderedDict()
        # Off-consumer repairs (truncation refetches). Held so the garbage
        # collector cannot drop a task mid-flight and lose the message.
        self._detached: set[asyncio.Task] = set()
        # sessionKey -> the newest `deltaCursor` the gateway gave us. A forward
        # catch-up from a cursor replays exactly what was missed; the backward
        # page walk is the fallback for when the cursor is too old to serve.
        self._delta_cursor: OrderedDict[str, str] = OrderedDict()
        # sessionKey -> last messageSeq we processed. The gap detector.
        # sessionKey -> highest messageSeq seen. LRU-bounded (see _record_seq).
        self._seq: OrderedDict[str, int] = OrderedDict()
        # sessionKey -> the held error placeholder (see ERROR_PLACEHOLDER_GRACE_S).
        self._held_errors: dict[str, asyncio.Task] = {}
        # sessionKey -> (thread_id, bot_id, lowest missed seq): gaps noticed but
        # not yet chased. One task per session drains this (see _schedule_gap).
        self._gap_pending: dict[str, tuple[str, str, int]] = {}
        self._gap_tasks: dict[str, asyncio.Task] = {}
        self.stats = {"delivered": 0, "delivered_live": 0,
                      "delivered_backfill": 0, "skipped_not_ours": 0,
                      "gaps": 0, "refetched": 0, "backfilled": 0,
                      "truncation_unrepaired": 0, "backfill_incomplete": 0,
                      "gaps_empty": 0, "gap_backfills_run": 0,
                      "backfill_attempts": 0, "deduped": 0,
                      "error_held": 0, "error_suppressed": 0,
                      "error_released": 0,
                      "streams_started": 0, "chunks_sent": 0,
                      "streams_orphaned": 0, "status_frames": 0,
                      "late_chunks_dropped": 0,
                      "refetch_pending": 0, "refetch_failed": 0,
                      "forward_catchups": 0, "cursor_resets": 0}

    def _record_seq(self, session_key: str, seq: int) -> None:
        """Advance one session's cursor, evicting the least-recently-used."""
        cur = self._seq.get(session_key)
        self._seq[session_key] = seq if cur is None else max(seq, cur)
        self._seq.move_to_end(session_key)
        while len(self._seq) > SEQ_CURSOR_MAX:
            self._seq.popitem(last=False)

    async def handle(self, event: str, payload: dict) -> None:
        # Live, display-only families first. They never persist and never
        # touch the seq cursor — a `chat` delta is the same words the
        # transcript event will carry moments later, and treating it as a
        # second source of truth is how you get every reply twice.
        if event == "chat":
            await self._handle_chat(payload)
            return
        if event in ("session.tool", "agent"):
            await self._handle_tool(payload)
            return
        if event != "session.message":
            return
        session_key = payload.get("sessionKey") or ""
        message = payload.get("message") or {}

        # THE CURSOR MOVES FOR EVERY ROLE, BECAUSE messageSeq COUNTS EVERY ROLE.
        # It is an absolute per-session counter over user and toolResult rows
        # too — measured live: assistant 2, toolResult 3, toolResult 4,
        # assistant 5. Advancing it only on assistant messages made every
        # ordinary tool-using turn look like a 3-message gap, so the detector
        # fired constantly and a REAL loss was indistinguishable from the noise
        # it generated itself.
        seq = payload.get("messageSeq")
        last: int | None = None
        if isinstance(seq, int):
            last = self._seq.get(session_key)

        # Resolve BEFORE filtering by role, so a dropped message on a thread we
        # own is counted. Dropping first meant the user half of every mirrored
        # conversation vanished leaving no trace in the stats at all.
        target = await self._resolve(session_key)
        if target is None:
            self.stats["skipped_not_ours"] += 1
            log.debug("gateway-ws skip key=%s", session_key)
            return
        thread_id, bot_id = target

        # Only NOW is the cursor recorded. Every session on the gateway used to
        # get an entry, including the ones that resolve to nothing, so the map
        # grew for the life of the process and resync() re-walked sessions that
        # were never ours. It is also LRU-bounded, because "sessions we own"
        # has no natural ceiling either.
        if isinstance(seq, int):
            self._record_seq(session_key, seq)

        # Gap check BEFORE the role filter, not after. messageSeq counts every
        # role, so the common shape "assistant N delivered, assistant N+1
        # dropped, toolResult N+2 arrives" would advance the cursor past the
        # loss on the tool row and early-return before ever checking — the next
        # assistant then sees no gap and the dropped reply is lost silently.
        # Detecting here backfills the missing assistant message even when the
        # message in hand is one we are about to drop.
        if last is not None and isinstance(seq, int) and seq > last + 1:
            self.stats["gaps"] += 1
            log.info("session %s jumped %d -> %d; queued a backfill",
                     session_key, last, seq)
            # NOT awaited. A gap repair used to run inline, ahead of the very
            # message that revealed it, so every tool call put a chat.history
            # round trip in front of the reply — and because the repair's range
            # INCLUDED that message, the reply was then delivered as a replay
            # (live=False -> `followup`, whole-thread dedup, reaction markers
            # held to the replay rule) and its real live delivery deduped away.
            # The reply is the urgent thing; the repair is not.
            self._schedule_gap(session_key, thread_id, bot_id, last)

        if message.get("role") != "assistant":
            # The user's own message is already in the thread — DisPatch put it
            # there before dispatching the turn. Re-delivering it would show
            # the family their own question a second time, below the answer.
            # (Mirror-kind threads are the exception, and this counter is how
            # many rows a cutover would lose there.)
            role = message.get("role")
            if role not in KNOWN_ROLES:
                role = "other"
            k = f"dropped_role_{role}"
            self.stats[k] = self.stats.get(k, 0) + 1
            return

        if message.get("stopReason") == "error":
            self._hold_error(session_key, thread_id, bot_id, message)
            return
        # Any later assistant message in the session — a tool call of the
        # retried prompt counts — means the run recovered and the held error
        # was the gateway's transient, not the reply.
        held = self._held_errors.pop(session_key, None)
        if held is not None and not held.done():
            held.cancel()
            self.stats["error_suppressed"] += 1
            log.info("gateway-ws suppressed transient error placeholder "
                     "session=%s (run continued)", session_key)
        await self._deliver_message(session_key, thread_id, bot_id, message,
                                    live=True, defer_refetch=True)

    # -- live deltas ------------------------------------------------------- #

    def provisional_id(self, run_id: str) -> str:
        """The message id a client uses for a bubble that has no row yet."""
        return f"run:{run_id}"

    async def _run_for(self, run_id: str, session_key: str) -> _ChatRun | None:
        """The cached state for one run, resolving its thread exactly once.

        The negative answer is cached too. Most runs on this gateway are not
        DisPatch conversations, and a foreign run emits several deltas a
        second — re-resolving each one would put a database read on the event
        loop for every frame of every session on the box.
        """
        run = self._runs.get(run_id)
        if run is None:
            run = _ChatRun(await self._resolve(session_key))
            self._runs[run_id] = run
            while len(self._runs) > RUN_CACHE_MAX:
                _, evicted = self._runs.popitem(last=False)
                self._cancel_run_tasks(evicted)
        self._runs.move_to_end(run_id)
        return run

    @staticmethod
    def _cancel_run_tasks(run: _ChatRun) -> None:
        for task in (run.flush, run.settle):
            if task is not None and not task.done():
                task.cancel()

    async def _emit(self, frame: dict) -> None:
        if self._broadcast is None:
            return
        try:
            await self._broadcast(frame)
        except Exception:
            # A display frame that cannot be sent must never take the event
            # pump down with it — the transcript path rides the same pump.
            log.exception("gateway-ws could not broadcast %s", frame.get("type"))

    async def _handle_chat(self, payload: dict) -> None:
        run_id = payload.get("runId")
        session_key = payload.get("sessionKey") or ""
        if not isinstance(run_id, str) or not run_id or not session_key:
            return
        state = payload.get("state")
        if state not in ("delta", "status", "final", "aborted", "error"):
            return
        run = await self._run_for(run_id, session_key)
        if run is None or run.target is None:
            return
        thread_id, bot_id = run.target
        if state == "status":
            phase = payload.get("phase")
            if isinstance(phase, str) and phase:
                self.stats["status_frames"] += 1
                await self._emit({"type": "turn_status", "thread_id": thread_id,
                                  "bot_id": bot_id, "run_id": run_id,
                                  "phase": phase})
            return
        if state == "delta":
            message = payload.get("message")
            text = text_of(message) if isinstance(message, dict) else ""
            if payload.get("replace"):
                run.replace = True
            await self._queue_chunk(run_id, run, text)
            return
        # Terminal. Flush whatever is buffered, then give the transcript event
        # a bounded moment to land the real row before the bubble is retired.
        await self._flush_chunk(run_id, run, force=True)
        await self._arm_settle(run_id, run, state)

    async def _queue_chunk(self, run_id: str, run: _ChatRun, text: str) -> None:
        """Record the newest cumulative text; send it now or shortly."""
        run.pending = text
        if run.flush is not None and not run.flush.done():
            return                      # a flush is already scheduled
        await self._flush_chunk(run_id, run)

    async def _flush_chunk(self, run_id: str, run: _ChatRun,
                           *, force: bool = False) -> None:
        if run.pending is None:
            return
        pending, run.pending = run.pending, None
        # SANITIZE THE CUMULATIVE TEXT, THEN DIFF. Doing it the other way round
        # — sanitizing each chunk — leaks any directive that straddles two
        # chunks, which is precisely why streaming frames were kept away from
        # Safe Mode until now.
        clean = self._sanitize(pending)
        replace, run.replace = run.replace, False
        if clean == run.sent:
            return
        if not replace and clean.startswith(run.sent):
            chunk = clean[len(run.sent):]
            frame_extra: dict = {}
        else:
            chunk = clean
            frame_extra = {"replace": True}
        run.sent = clean
        if not chunk:
            return
        thread_id, bot_id = run.target            # type: ignore[misc]
        prov = self.provisional_id(run_id)
        if (run.started and self._stream_open is not None
                and not self._stream_open(thread_id, prov)):
            # The persisted row already landed and its stream_done swapped the
            # bubble for the real message. The run's own terminal event still
            # flushes what it holds (the final message text, which can differ
            # from the delta stream by a stripped marker), and that chunk would
            # RE-CREATE the bubble the client just retired — seen on staging as
            # a second, cursor-bearing copy of the whole reply under the real
            # one. Nothing after the row is worth painting; retire the run.
            self.stats["late_chunks_dropped"] += 1
            self._runs.pop(run_id, None)
            self._cancel_run_tasks(run)
            return
        if not run.started:
            run.started = True
            self.stats["streams_started"] += 1
            if self._open_stream is not None:
                self._open_stream(thread_id, prov)
            await self._emit({"type": "stream_start", "thread_id": thread_id,
                              "bot_id": bot_id, "message_id": prov})
        self.stats["chunks_sent"] += 1
        await self._emit({"type": "stream_chunk", "thread_id": thread_id,
                          "bot_id": bot_id, "message_id": prov,
                          "text": chunk, **frame_extra})
        if not force:
            run.flush = asyncio.create_task(self._flush_later(run_id))

    async def _flush_later(self, run_id: str) -> None:
        """Hold the next chunk for one interval, then send whatever arrived."""
        try:
            await asyncio.sleep(CHUNK_MIN_INTERVAL_S)
        except asyncio.CancelledError:
            return
        run = self._runs.get(run_id)
        if run is None:
            return
        run.flush = None
        try:
            await self._flush_chunk(run_id, run)
        except Exception:
            log.exception("gateway-ws chunk flush failed for run %s", run_id)

    async def _arm_settle(self, run_id: str, run: _ChatRun, state: str) -> None:
        """After a run ends, retire its provisional bubble if no row lands."""
        if not run.started:
            self._runs.pop(run_id, None)
            self._cancel_run_tasks(run)
            return
        if run.settle is not None and not run.settle.done():
            return
        run.settle = asyncio.create_task(self._settle(run_id, state))

    async def _settle(self, run_id: str, state: str) -> None:
        # An aborted or errored run is retired at once: there is no row coming,
        # and ten seconds of a bubble that has visibly stopped is ten seconds
        # of the client believing more text is on the way.
        if state == "final":
            try:
                await asyncio.sleep(RUN_SETTLE_S)
            except asyncio.CancelledError:
                return
        run = self._runs.pop(run_id, None)
        if run is None or run.target is None:
            return
        # Only the flush timer. `run.settle` IS this task — cancelling it here
        # cancels the coroutine mid-retirement, and the bubble it was about to
        # close stays open for ever.
        run.settle = None
        self._cancel_run_tasks(run)
        thread_id, bot_id = run.target
        prov = self.provisional_id(run_id)
        # close_stream returns False when the persist chokepoint already
        # claimed this provisional id — the row landed and sent its own
        # stream_done, so there is nothing to retire.
        if self._close_stream is not None and not self._close_stream(thread_id, prov):
            return
        self.stats["streams_orphaned"] += 1
        log.info("gateway-ws run %s ended (%s) with no persisted row; "
                 "retiring the provisional bubble", run_id, state)
        await self._emit({"type": "stream_done", "thread_id": thread_id,
                          "bot_id": bot_id, "message_id": prov,
                          "message": None, "provisional_id": prov})

    async def _handle_tool(self, payload: dict) -> None:
        """Turn a tool lifecycle event into a display-only phase line."""
        if payload.get("stream") != "tool":
            return
        data = payload.get("data")
        if not isinstance(data, dict) or data.get("phase") != "start":
            return
        name = data.get("name") or data.get("tool") or data.get("toolName")
        run_id = payload.get("runId")
        session_key = payload.get("sessionKey") or ""
        if not isinstance(name, str) or not name or not session_key:
            return
        if not isinstance(run_id, str) or not run_id:
            return
        run = await self._run_for(run_id, session_key)
        if run is None or run.target is None:
            return
        thread_id, bot_id = run.target
        self.stats["status_frames"] += 1
        await self._emit({"type": "turn_status", "thread_id": thread_id,
                          "bot_id": bot_id, "run_id": run_id,
                          "phase": f"tool:{name}"})

    async def open_provisional(self, run_id: str, session_key: str,
                               text: str) -> None:
        """Re-open a run's bubble after a reconnect, from its buffered text.

        The client has been disconnected from the delta stream, so what it has
        on screen is whatever arrived before the socket dropped — which may be
        nothing, or may be stale. A `replace` carrying the whole buffer is the
        only shape that is correct from either starting point.
        """
        run = await self._run_for(run_id, session_key)
        if run is None or run.target is None or not text.strip():
            return
        run.sent = ""
        run.replace = True
        run.pending = text
        await self._flush_chunk(run_id, run, force=True)

    def cancel_streams(self) -> None:
        """Drop every run's timers. Shutdown only."""
        for run in self._runs.values():
            self._cancel_run_tasks(run)
        self._runs.clear()

    def _schedule_gap(self, session_key: str, thread_id: str, bot_id: str,
                      last: int) -> None:
        """Remember a gap and make sure something will chase it.

        Coalescing is the point: a turn that calls eight tools reports eight
        gaps, all inside the same few seconds, all covered by one read. The
        LOWEST missed seq wins, so the one read still spans everything.
        """
        prev = self._gap_pending.get(session_key)
        low = last if prev is None else min(prev[2], last)
        self._gap_pending[session_key] = (thread_id, bot_id, low)
        task = self._gap_tasks.get(session_key)
        if task is not None and not task.done():
            return
        self._gap_tasks[session_key] = asyncio.create_task(
            self._drain_gap(session_key))

    async def _run_gap(self, session_key: str) -> None:
        """Chase whatever is queued for one session, if anything still is."""
        pending = self._gap_pending.pop(session_key, None)
        if pending is None:
            return
        thread_id, bot_id, last = pending
        self.stats["gap_backfills_run"] += 1
        before = self.stats["backfilled"]
        await self._backfill(session_key, thread_id, bot_id, last, None)
        if self.stats["backfilled"] == before:
            # The overwhelmingly common case: the missing seqs were rows the
            # gateway never emits (a toolResult is folded into the assistant
            # message that called the tool, counted by messageSeq and never
            # sent). Counted separately so `gaps` climbing stops reading as
            # replies going missing — a counter that cries loss on every tool
            # call trains an operator to ignore the one time it means it.
            self.stats["gaps_empty"] += 1

    async def _drain_gap(self, session_key: str) -> None:
        """Wait out the settling delay, then chase the session's queued gap."""
        try:
            await asyncio.sleep(GAP_BACKFILL_DELAY_S)
        except asyncio.CancelledError:
            # flush_gaps() cancels the sleep and runs the repair itself; the
            # queue entry stays put so nothing is dropped.
            self._gap_tasks.pop(session_key, None)
            raise
        try:
            await self._run_gap(session_key)
        except Exception:
            log.exception("gap backfill failed for %s", session_key)
        finally:
            self._gap_tasks.pop(session_key, None)
            # A gap recorded while the repair was running must not sit unchased.
            still = self._gap_pending.get(session_key)
            if still is not None:
                self._schedule_gap(session_key, *still)

    async def flush_gaps(self) -> None:
        """Run every queued gap repair immediately.

        The repair is deliberately deferred (see GAP_BACKFILL_DELAY_S), which
        makes "did the gap get repaired?" untestable and un-drainable without
        this. Used by the tests and by shutdown, where a pending repair would
        otherwise be cancelled with the loop and simply never happen.
        """
        for key, task in list(self._gap_tasks.items()):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
            self._gap_tasks.pop(key, None)
        for key in list(self._gap_pending):
            try:
                await self._run_gap(key)
            except Exception:
                log.exception("gap backfill failed for %s", key)

    def _hold_error(self, session_key: str, thread_id: str, bot_id: str,
                    message: dict) -> None:
        """Park an error-stopped message; deliver it only if the run stays dead.

        One hold per session: a second error while one is pending replaces it
        (the gateway's overflow recovery can emit several in a row — only the
        last one can be the turn's real end).
        """
        prev = self._held_errors.pop(session_key, None)
        if prev is not None and not prev.done():
            prev.cancel()
        self.stats["error_held"] += 1
        log.info("gateway-ws holding error placeholder session=%s for %.0fs",
                 session_key, ERROR_PLACEHOLDER_GRACE_S)
        task = asyncio.create_task(
            self._release_error(session_key, thread_id, bot_id, message))
        self._held_errors[session_key] = task

    async def _release_error(self, session_key: str, thread_id: str,
                             bot_id: str, message: dict) -> None:
        try:
            await asyncio.sleep(ERROR_PLACEHOLDER_GRACE_S)
        except asyncio.CancelledError:
            return
        self._held_errors.pop(session_key, None)
        self.stats["error_released"] += 1
        try:
            await self._deliver_message(session_key, thread_id, bot_id,
                                        message, live=True)
        except Exception:
            log.exception("gateway-ws error placeholder delivery failed "
                          "session=%s", session_key)

    def _spawn(self, coro) -> None:
        """Run a coroutine detached, holding a reference so it is not GC'd."""
        task = asyncio.create_task(coro)
        self._detached.add(task)
        task.add_done_callback(self._detached.discard)

    async def drain_detached(self) -> None:
        """Wait for the off-consumer repairs. Tests and shutdown."""
        while self._detached:
            await asyncio.gather(*list(self._detached), return_exceptions=True)

    async def _deliver_refetched(self, session_key: str, thread_id: str,
                                 bot_id: str, message: dict, mid: str,
                                 projected: str, live: bool) -> None:
        """Refetch a body the projection cut short, then deliver it."""
        try:
            full = await self._client.full_text(session_key, mid)
        except Exception:
            full = None
        if full:
            self.stats["refetched"] += 1
            text = full
        else:
            self.stats["truncation_unrepaired"] += 1
            self.stats["refetch_failed"] += 1
            log.error("UNREPAIRED TRUNCATION session=%s id=%s len=%d",
                      session_key, mid, len(projected))
            text = projected
        await self._deliver_and_count(session_key, thread_id, bot_id, message,
                                      text, live)

    async def _deliver_message(self, session_key: str, thread_id: str,
                               bot_id: str, message: dict,
                               *, live: bool = False,
                               defer_refetch: bool = False) -> Any:
        text = text_of(message)
        if not text.strip():
            return None

        # Truncation: repair BEFORE anything downstream sees it. Detected per
        # BLOCK — a truncated block that is not last leaves the marker buried
        # where an endswith() check on the joined text cannot find it.
        if self._client is not None and self._client.blocks_truncated(message):
            oc = message.get("__openclaw") or {}
            mid = oc.get("id")
            if defer_refetch and mid:
                # OFF THE SINGLE CONSUMER. `full_text` is a round trip to the
                # gateway; awaiting it here stops the one task that drains the
                # event queue for EVERY session, so one long reply on one
                # thread stalls delivery everywhere and fills the queue (which
                # is what `dropped_local` counts). One task per truncated
                # message instead: only these — a rare case — lose their place
                # in the session's order, and delivery is idempotent by
                # source_id, so nothing can be duplicated by the reordering.
                self.stats["refetch_pending"] += 1
                self._spawn(self._deliver_refetched(
                    session_key, thread_id, bot_id, message, mid, text, live))
                return None
            full = await self._client.full_text(session_key, mid) if mid else None
            if full:
                self.stats["refetched"] += 1
                text = full
            else:
                # LOUD. A silent failure here delivers a reply that stops
                # mid-sentence, with no error, no log and no failing test —
                # and a clean "refetched: 0" reads as good news rather than as
                # a repair path that never worked.
                self.stats["truncation_unrepaired"] += 1
                log.error("UNREPAIRED TRUNCATION session=%s id=%s len=%d",
                          session_key, mid, len(text))

        return await self._deliver_and_count(session_key, thread_id, bot_id,
                                             message, text, live)

    async def _deliver_and_count(self, session_key: str, thread_id: str,
                                 bot_id: str, message: dict, text: str,
                                 live: bool) -> Any:
        # `live` travels with the message: a backfilled reply is history being
        # replayed, and the deliverer needs to know — it may sit behind later
        # turns (trailing-run dedup cannot see it) and its `:react:` markers
        # were already spent when the block first happened.
        landed = await self._deliver(
            thread_id, text,
            source_id=source_id_of(message, session_key),
            created_at=created_at_of(message),
            bot_id=bot_id, live=live,
        )
        # The funnel returns the persisted row, or None when it recognised the
        # message as one the thread already has. COUNT THE LANDINGS, not the
        # attempts: "delivered_backfill: 27" meant 27 offers, most of which the
        # funnel threw away as duplicates, and it read as 27 replies that only
        # a repair had rescued. A counter that reports work it did not do is
        # the same failure as a repair that reports success by silence.
        if landed is None:
            self.stats["deduped"] += 1
        else:
            self.stats["delivered_live" if live else "delivered_backfill"] += 1
            self.stats["delivered"] += 1
        return landed

    async def _backfill(self, session_key: str, thread_id: str, bot_id: str,
                        last: int, now: int | None) -> None:
        """Fetch what the gap swallowed.

        Safe to overrun: every message carries a stable id, so re-delivering
        one already stored is a no-op. That is the property the old
        content-based dedup lacked, and why a re-read used to replay the day.
        """
        if self._client is None:
            return

        # WALK BACK TO THE CURSOR, don't guess a window. `chat.history` is a
        # paged tail read; one call with limit=min(gap+5, 100) covered a gap of
        # at most ~95 messages, and anything wider was silently unrecoverable
        # by this transport — with stats["backfilled"] reading healthy, because
        # what it counts is what was delivered, not what was missed.
        pages: list[list[dict]] = []
        fetched = 0
        oldest_seq: int | None = None
        while fetched < BACKFILL_MAX_MESSAGES:
            limit = min(BACKFILL_PAGE, BACKFILL_MAX_MESSAGES - fetched)
            try:
                page = await self._client.history(
                    session_key, limit=limit, offset=fetched)
            except Exception:
                log.warning("backfill failed for %s", session_key, exc_info=True)
                break
            page = [m for m in page if isinstance(m, dict)]
            if not page:
                break
            pages.append(page)
            fetched += len(page)
            seqs = [s for s in ((m.get("__openclaw") or {}).get("seq")
                                for m in page) if isinstance(s, int)]
            if seqs:
                low = min(seqs)
                oldest_seq = low if oldest_seq is None else min(oldest_seq, low)
            # Reached back past the cursor: the gap is fully covered.
            if oldest_seq is not None and oldest_seq <= last + 1:
                break
            if len(page) < limit:
                break                            # the session has no more

        if oldest_seq is None or oldest_seq > last + 1:
            # VISIBLE, not silent. The gap is real, it was not covered, and the
            # delivered counters cannot show that on their own.
            self.stats["backfill_incomplete"] += 1
            log.error("INCOMPLETE BACKFILL session=%s gap=%d..%s reached=%s "
                      "after %d messages", session_key, last,
                      "live" if now is None else now, oldest_seq, fetched)

        # Pages come newest-first; deliver oldest-first so a thread reads in
        # order.
        for m in [m for page in reversed(pages) for m in page]:
            if m.get("role") != "assistant":
                continue
            # ONLY WHAT THE GAP SWALLOWED. history() returns a window, not the
            # gap, so delivering all of it re-posts messages already shown.
            # Identity dedup does not save us here: no row persisted before the
            # migration has a source_id, and the content fallback only compares
            # the trailing assistant run. That combination is exactly how 49
            # messages once landed in the family chat in one second.
            mseq = (m.get("__openclaw") or {}).get("seq")
            # `now` is EXCLUSIVE. When a live message reveals a gap, that
            # message is delivered live moments later; including it here made
            # the backfill win the race and the reply landed as a replay.
            if isinstance(mseq, int) and (
                    mseq <= last or (now is not None and mseq >= now)):
                continue
            self.stats["backfill_attempts"] += 1
            if await self._deliver_message(session_key, thread_id, bot_id, m):
                self.stats["backfilled"] += 1

    def _record_cursor(self, session_key: str, cursor: Any) -> None:
        if not isinstance(cursor, str) or not cursor:
            return
        self._delta_cursor[session_key] = cursor
        self._delta_cursor.move_to_end(session_key)
        while len(self._delta_cursor) > SEQ_CURSOR_MAX:
            self._delta_cursor.popitem(last=False)

    def cursor_for(self, session_key: str) -> str | None:
        return self._delta_cursor.get(session_key)

    async def _replay(self, session_key: str, thread_id: str, bot_id: str,
                      messages: Any) -> None:
        """Deliver a page of history as a replay (never live)."""
        if not isinstance(messages, list):
            return
        for m in messages:
            if not isinstance(m, dict) or m.get("role") != "assistant":
                continue
            seq = (m.get("__openclaw") or {}).get("seq")
            if isinstance(seq, int):
                self._record_seq(session_key, seq)
            self.stats["backfill_attempts"] += 1
            if await self._deliver_message(session_key, thread_id, bot_id, m):
                self.stats["backfilled"] += 1

    async def _reopen_inflight(self, session_key: str, envelope: dict) -> None:
        """Put a still-running turn's bubble back on screen after a reconnect.

        Without this the client shows a bubble that stopped mid-sentence when
        the socket dropped and never moves again — the run is alive on the
        gateway and its remaining deltas went to a subscription that no longer
        existed.
        """
        run = envelope.get("inFlightRun")
        if not isinstance(run, dict):
            return
        run_id, text = run.get("runId"), run.get("text")
        if not isinstance(run_id, str) or not run_id:
            return
        await self.open_provisional(run_id, session_key,
                                    text if isinstance(text, str) else "")

    async def catch_up(self, session_key: str, *, force: bool = False) -> None:
        """Recover one session after a gap in the socket.

        THREE ROADS, cheapest first. A `deltaCursor` gets a bounded forward
        replay of exactly what was missed. A `reset` (the cursor is too old to
        serve) or a session with only a seq cursor falls back to the backward
        page walk. A session with NEITHER — which is every session whose turn
        was dispatched but whose first transcript event never arrived, i.e.
        precisely the runs a disconnect strands — used to be skipped entirely
        and is now given a bounded tail read.
        """
        if self._client is None:
            return
        target = await self._resolve(session_key)
        if target is None:
            return
        thread_id, bot_id = target
        cursor = self._delta_cursor.get(session_key)
        if cursor:
            try:
                res = await self._client.history_from_cursor(session_key, cursor)
            except Exception:
                log.warning("forward catch-up failed for %s", session_key,
                            exc_info=True)
                res = {}
            if res.get("kind") == "delta":
                self.stats["forward_catchups"] += 1
                self._record_cursor(session_key, res.get("deltaCursor"))
                await self._replay(session_key, thread_id, bot_id,
                                   res.get("messages"))
                await self._reopen_inflight(session_key, res)
                return
            if res.get("kind") == "reset":
                # Not an error: the cursor is simply older than the window the
                # gateway keeps. Falling back is the documented recovery, and
                # returning here would make a reset a silent no-repair.
                self.stats["cursor_resets"] += 1
                force = True
        last = self._seq.get(session_key)
        if last is not None:
            # No upper bound: a reconnect has no idea how far the session moved
            # while the socket was down, and `last + 50` was a guess that
            # DISCARDED anything past it. The walk back to the cursor is what
            # bounds the read now.
            await self._backfill(session_key, thread_id, bot_id, last, None)
        elif not force:
            return
        try:
            res = await self._client.history_tail(session_key,
                                                  limit=BACKFILL_PAGE)
        except Exception:
            log.warning("tail catch-up failed for %s", session_key,
                        exc_info=True)
            return
        self._record_cursor(session_key, res.get("deltaCursor"))
        if last is None:
            await self._replay(session_key, thread_id, bot_id,
                               res.get("messages"))
        await self._reopen_inflight(session_key, res)

    async def resync_known(self) -> None:
        """Resync every session we hold a cursor for. The reconnect entry point.

        First connect: both cursor maps are empty, so this is a no-op.
        Reconnect: they hold the sessions we were tracking, so we recover
        exactly those and nothing else — bounded by construction, and
        source_id dedup makes any overlap harmless.
        """
        keys = list(self._seq.keys())
        keys += [k for k in self._delta_cursor if k not in self._seq]
        await self.resync(keys)

    async def resync(self, session_keys: list[str], *,
                     force: bool = False) -> None:
        """Backfill sessions after a reconnect.

        Subscriptions die with the connection, so anything emitted while
        disconnected was simply never sent — there is no queue. History is the
        only way to recover it.

        ``force`` covers a session we have never received an event for: a turn
        accepted just before the socket dropped has no seq cursor at all, so
        the cursor-less skip meant the one case a reconnect exists for was the
        one it declined to repair.
        """
        for key in session_keys:
            try:
                await self.catch_up(key, force=force)
            except Exception:
                log.exception("resync failed for %s", key)
