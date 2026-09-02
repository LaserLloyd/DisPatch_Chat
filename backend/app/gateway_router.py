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
    ) -> None:
        self._resolve = resolve_thread
        self._deliver = deliver
        self._client = client
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
                      "error_released": 0}

    def _record_seq(self, session_key: str, seq: int) -> None:
        """Advance one session's cursor, evicting the least-recently-used."""
        cur = self._seq.get(session_key)
        self._seq[session_key] = seq if cur is None else max(seq, cur)
        self._seq.move_to_end(session_key)
        while len(self._seq) > SEQ_CURSOR_MAX:
            self._seq.popitem(last=False)

    async def handle(self, event: str, payload: dict) -> None:
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
                                    live=True)

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

    async def _deliver_message(self, session_key: str, thread_id: str,
                               bot_id: str, message: dict,
                               *, live: bool = False) -> Any:
        text = text_of(message)
        if not text.strip():
            return None

        # Truncation: repair BEFORE anything downstream sees it. Detected per
        # BLOCK — a truncated block that is not last leaves the marker buried
        # where an endswith() check on the joined text cannot find it.
        if self._client is not None and self._client.blocks_truncated(message):
            oc = message.get("__openclaw") or {}
            mid = oc.get("id")
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

    async def resync_known(self) -> None:
        """Resync every session we hold a cursor for. The reconnect entry point.

        First connect: `_seq` is empty, so this is a no-op. Reconnect: it holds
        the sessions we were tracking, so we backfill exactly those and nothing
        else — bounded by construction, and the legacy transcript sweep plus
        source_id dedup make any overlap harmless.
        """
        await self.resync(list(self._seq.keys()))

    async def resync(self, session_keys: list[str]) -> None:
        """Backfill sessions after a reconnect.

        Subscriptions die with the connection, so anything emitted while
        disconnected was simply never sent — there is no queue. The cursor is
        the only way to notice, and history is the only way to recover.
        """
        for key in session_keys:
            last = self._seq.get(key)
            if last is None:
                continue
            target = await self._resolve(key)
            if target is None:
                continue
            thread_id, bot_id = target
            # No upper bound: a reconnect has no idea how far the session moved
            # while the socket was down, and `last + 50` was a guess that
            # DISCARDED anything past it. The walk back to the cursor is what
            # bounds the read now.
            await self._backfill(key, thread_id, bot_id, last, None)
