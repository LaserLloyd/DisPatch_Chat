"""SQLite persistence layer (async via aiosqlite).

A single shared connection is used: aiosqlite runs each connection on its own
worker thread and serialises operations through a queue, so one connection is
both safe and simplest for a single-user app. WAL mode keeps reads snappy.

Timestamps are stored as ISO-8601 UTC strings with a trailing offset so the
browser's `new Date(...)` parses them unambiguously (avoids the classic
"SQLite datetime('now') parsed as local time" bug).
"""

from __future__ import annotations

import asyncio
import contextlib
import json as _json
import logging
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

from . import avatar_pool, avatar_snapshots, config
from .models import MessageOut, ThreadOut

log = logging.getLogger("local-chat.database")

SCHEMA = """
CREATE TABLE IF NOT EXISTS threads (
    id          TEXT PRIMARY KEY,
    bot_id      TEXT NOT NULL,
    title       TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'idle',
    is_archived INTEGER NOT NULL DEFAULT 0,
    is_pinned   INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_threads_bot_updated ON threads(bot_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_threads_updated ON threads(updated_at DESC);

CREATE TABLE IF NOT EXISTS messages (
    id          TEXT PRIMARY KEY,
    thread_id   TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    media_url   TEXT,
    created_at  TEXT NOT NULL,
    metadata    TEXT
);

CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(thread_id, created_at);

-- One row per pool-enabled bot: the local date whose "daily face" slot has
-- been taken. The FIRST eligible thread of a day wears the bot's current
-- (daily) avatar; claiming is an UPDATE guarded on the stored date, so two
-- concurrent creations cannot both take the slot. See create_thread.
CREATE TABLE IF NOT EXISTS avatar_pool_daily (
    bot_id     TEXT PRIMARY KEY,
    used_date  TEXT NOT NULL DEFAULT ''
);

-- One row per transcript item the gap sweep has already accounted for.
--
-- The sweep's job is "deliver what the live paths missed", and it decided that
-- by comparing canonical text. Canonical text is a heuristic that has now
-- drifted from what persisting stores TWICE (rewritten media paths, stripped
-- reaction markers), and each drift turned the backstop into a duplicate
-- machine — the same reply re-posted every ten minutes for an hour. Identity
-- does not drift: an item this sweep has already looked at is not a gap,
-- whatever the text comparison thinks.
CREATE TABLE IF NOT EXISTS transcript_seen (
    item_id    TEXT PRIMARY KEY,
    thread_id  TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_transcript_seen_thread ON transcript_seen(thread_id);

CREATE TABLE IF NOT EXISTS files (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    stored_name TEXT NOT NULL,
    size        INTEGER NOT NULL DEFAULT 0,
    mime        TEXT,
    created_at  TEXT NOT NULL,
    source      TEXT NOT NULL DEFAULT 'chat'
);

CREATE INDEX IF NOT EXISTS idx_files_created ON files(created_at DESC);

-- Agent-fired image jobs (app/image_jobs.py). One row per request, created in
-- the same breath as the placeholder message it will eventually rewrite.
--
-- The row is the truth, not the worker's memory: a restart mid-render reads
-- this table back, resumes anything the rig still holds a job id for, and
-- fails out the rest. Without it a crash would leave "image pending…" in a
-- thread with nothing left alive to ever change it — the one outcome this
-- feature is not allowed to produce.
--
-- ON DELETE CASCADE from messages, not threads: deleting the placeholder
-- deletes the job, because the job exists only to edit that message.
CREATE TABLE IF NOT EXISTS image_jobs (
    id          TEXT PRIMARY KEY,
    message_id  TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    thread_id   TEXT NOT NULL,
    bot_id      TEXT NOT NULL,
    spec        TEXT NOT NULL,          -- ImageSpec.to_json()
    state       TEXT NOT NULL,          -- queued | running | done | failed | cancelled
    rig_job_id  TEXT,                   -- the image server's own id, once it has one
    error       TEXT,
    media_url   TEXT,
    -- Per-job bearer token for POST /api/image-jobs/<id>/callback. NULL when
    -- no callback origin is configured, and a NULL token can never match: the
    -- route refuses before comparing, so "callbacks off" is not a hole.
    callback_token TEXT,
    -- The rig's last reported {step, steps, percent}, as JSON. Advisory only —
    -- the poll remains the sole authority on state.
    progress    TEXT,
    -- The seed the rig chose, known at enqueue. Kept so "again, but…" can
    -- reproduce (or deliberately not reproduce) a picture later.
    seed        INTEGER,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_image_jobs_state ON image_jobs(state, created_at);

-- --------------------------------------------------------------------------- #
--  Jobs board (added 2026-09-14 — see plan §3)
--
--  Additive tables only — the bot id `jobboard` owns the surface, the same
--  `threads` + `messages` rows carry every job's chat, and these tables just
--  pin structured metadata + feedback to the thread_id. Drop the four tables
--  and the feature is gone.
--
--  Schema notes:
--  * `jobs.seniority` lets the recommender rank against the profile's
--    seniority preference at score time without re-parsing titles.
--  * `jobs.tags` is a JSON-encoded list (32-tag cap, validated at the API
--    boundary). A separate `job_events` row logs every tag mutation so the
--    feedback loop can attribute changes (no UPDATE on `jobs.tags` in
--    isolation — that would silently swallow history).
--  * `job_feedback` is append-only: signals are added, never rewritten.
--    Profile recompute is a fold over this table ordered by created_at ASC;
--    the latest signal per thread wins (so an `undo` cleanly reverts the
--    prior signal). Source: plan §6.
--  * `job_profile` is a SINGLETON (id=1, CHECK). Recompute writes back into
--    the same row — the score function reads its current value. The
--    embedding centroids (yes_centroid, no_centroid) are NULL until ≥10
--    yes/applied feedbacks exist; the scorer falls back to tag-only mode
--    while the model is unavailable or the threshold isn't met.
--  * PRAGMA foreign_keys=ON is set in connect() above (line 174), so
--    ON DELETE CASCADE from threads will fire and tear down a job's
--    rows when a thread is removed.
-- --------------------------------------------------------------------------- #

CREATE TABLE IF NOT EXISTS jobs (
    thread_id        TEXT PRIMARY KEY REFERENCES threads(id) ON DELETE CASCADE,
    url              TEXT NOT NULL,
    title            TEXT NOT NULL,
    company          TEXT NOT NULL DEFAULT '',
    location         TEXT NOT NULL DEFAULT '',
    remote_type      TEXT NOT NULL DEFAULT 'unknown',  -- onsite|hybrid|remote|unknown
    seniority        TEXT NOT NULL DEFAULT 'unknown', -- unknown|junior|mid|senior|staff|principal
    salary_min       INTEGER,
    salary_max       INTEGER,
    salary_currency  TEXT NOT NULL DEFAULT 'USD',
    tags             TEXT NOT NULL DEFAULT '[]',       -- JSON array
    source_agent     TEXT NOT NULL DEFAULT '',
    source_run_id    TEXT,
    posted_at        TEXT,                            -- when the role was originally posted
    first_seen       TEXT NOT NULL,                   -- immutable: when WE first saw it
    last_seen        TEXT NOT NULL,                   -- refreshed on each repost detection
    brief            TEXT NOT NULL DEFAULT '',        -- LLM analysis
    state            TEXT NOT NULL DEFAULT 'pending', -- pending|yes|no|maybe|applied|archived|duplicate
    duplicate_of     TEXT,                            -- thread_id of canonical when state=duplicate
    expires_at       TEXT,                            -- optional auto-archive deadline (lazy, in-GET)
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs(company);
CREATE INDEX IF NOT EXISTS idx_jobs_updated ON jobs(updated_at DESC);

CREATE TABLE IF NOT EXISTS job_events (
    id          TEXT PRIMARY KEY,
    thread_id   TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    type        TEXT NOT NULL,       -- state_change|tag_added|tag_removed|comment|vote|repost|duplicate_detected|expired
    from_state  TEXT,
    to_state    TEXT,
    reason_tag  TEXT,                -- e.g. 'wrong_location','too_senior','company','compensation'
    comment     TEXT,
    actor       TEXT NOT NULL,       -- 'user:<id>' | 'agent:<id>' | 'system'
    payload     TEXT,                -- JSON, free-form
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_job_events_thread ON job_events(thread_id, created_at);

CREATE TABLE IF NOT EXISTS job_feedback (
    id          TEXT PRIMARY KEY,
    thread_id   TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    signal      TEXT NOT NULL,       -- 'vote_yes'|'vote_no'|'vote_maybe'|'applied'|'undo'
    reason_tag  TEXT,
    comment     TEXT,
    actor       TEXT NOT NULL,
    -- JSON snapshot of the candidate fields the recompute fold needs
    -- (tags, remote_type, company, location, salary_mid, seniority).
    -- Written by ``add_job_feedback`` so the recompute is a pure fold over
    -- this table — no joining back to ``jobs`` for the per-vote context.
    -- Added 2026-09-14 (was missing on first cut; the blocklist stayed
    -- empty because the recompute read ``payload=None`` for every row).
    payload     TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_job_feedback_thread ON job_feedback(thread_id, created_at);

CREATE TABLE IF NOT EXISTS job_profile (
    id              INTEGER PRIMARY KEY DEFAULT 1,   -- singleton; CHECK at the end
    tag_weights     TEXT NOT NULL DEFAULT '{}',      -- JSON: {tag: weight}
    company_blocklist TEXT NOT NULL DEFAULT '[]',    -- JSON array (normalized lowercase)
    location_blocklist TEXT NOT NULL DEFAULT '[]',   -- JSON array (normalized: "tokyo, jp" -> "tokyo")
    salary_history  TEXT NOT NULL DEFAULT '{}',      -- JSON: {band_0_50k: n, band_50_100k: n, ...}
    preferred_remote TEXT NOT NULL DEFAULT '{}',     -- JSON: {onsite: w, hybrid: w, remote: w}
    seniority_preference TEXT NOT NULL DEFAULT '{}', -- JSON: {junior: n, mid: n, senior: n, staff: n, principal: n}
    yes_centroid    BLOB,                            -- 384-dim float32; NULL until >=10 yes/applied
    no_centroid     BLOB,                            -- same shape
    duplicate_hashes TEXT NOT NULL DEFAULT '[]',     -- JSON: [{hash, thread_id, seen_at}] (rolling 500)
    reason_counts   TEXT NOT NULL DEFAULT '{}',      -- JSON: {reason_tag: count}
    yes_count       INTEGER NOT NULL DEFAULT 0,
    no_count        INTEGER NOT NULL DEFAULT 0,
    maybe_count     INTEGER NOT NULL DEFAULT 0,
    updated_at      TEXT NOT NULL,
    CHECK (id = 1)
);
"""


def now_iso() -> str:
    """Current UTC time as an unambiguous ISO-8601 string."""
    return datetime.now(UTC).isoformat()


def local_date() -> str:
    """Today's date (YYYY-MM-DD) in the host's local timezone."""
    return datetime.now().strftime("%Y-%m-%d")


def new_id() -> str:
    return str(uuid.uuid4())


# Delimiters SQLite's snippet() wraps around each full-text match. The frontend
# splits on them to build <mark> elements. Private Use Area code points: they
# have no glyph, no width and — crucially — no bidi semantics. See the comment
# at the query for why the previous U+2068/U+2069 pair had to go.
# Distinct from markdown.js's own U+E300/U+E301 placeholder tokens.
FTS_MARK_OPEN = "\ue000"
FTS_MARK_CLOSE = "\ue001"


class Database:
    def __init__(self, path: Path):
        self.path = path
        self._db: aiosqlite.Connection | None = None

    # -- lifecycle ---------------------------------------------------------- #

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None -> autocommit: each statement commits on its own.
        # Critical because a single connection is shared across many coroutines;
        # without this, one coroutine's commit() could publish another's
        # half-finished (uncommitted) multi-statement write.
        self._db = await aiosqlite.connect(str(self.path), isolation_level=None)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL;")
        await self._db.execute("PRAGMA foreign_keys=ON;")
        await self._db.execute("PRAGMA busy_timeout=5000;")
        # Durability: the app's overriding requirement is "never lose a message".
        # WAL's default synchronous=NORMAL can silently roll back the most-recent
        # committed transactions on a power loss / kernel panic — exactly the data
        # this app cannot lose. FULL fsyncs the WAL after each commit; the extra
        # cost is irrelevant at human/agent write rates (one writer, low volume).
        await self._db.execute("PRAGMA synchronous=FULL;")
        await self._db.executescript(SCHEMA)
        await self._db.commit()
        # Idempotent migrations: add columns to existing databases.
        for ddl in (
            "ALTER TABLE threads ADD COLUMN is_pinned INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE threads ADD COLUMN last_read_at TEXT",
            # File Server vs chat-attachment provenance. 'fileserver' uploads are
            # upload-only from the browser (download/raw served to loopback agents
            # only); 'chat' attachments stay viewable so posted media renders.
            "ALTER TABLE files ADD COLUMN source TEXT NOT NULL DEFAULT 'chat'",
            # What the bot's avatar looked like when this thread started. NULL
            # on every pre-existing thread, which is exactly right: they fall
            # back to the live avatar, i.e. today's behaviour. See
            # app/avatar_snapshots.py.
            "ALTER TABLE threads ADD COLUMN avatar_snapshot TEXT",
            # Was this thread's avatar pinned EXPLICITLY (someone gave this one
            # conversation its own picture), or captured automatically at
            # creation? The daily rotation re-pins unused (message-less) threads
            # to the new face — but an explicit pin on a not-yet-used thread must
            # survive that, or "give ONE conversation its own picture" is a lie
            # for any thread created before its first message (daily threads are
            # pre-created, so that window is routine).
            "ALTER TABLE threads ADD COLUMN avatar_pinned INTEGER NOT NULL DEFAULT 0",
            # Stable identity for a message that came from somewhere with one —
            # "<sessionId>:<seq>" for a transcript entry. Content-based dedup
            # only ever compared the trailing assistant run, so a re-scan from
            # offset 0 replayed everything above the last user message: 49 rows
            # in one second, 41 of them byte-identical, plus a re-fired
            # reaction. Identity makes the re-scan a no-op instead.
            # NULL for anything with no source identity (/api/inject, manual
            # import) — and SQLite treats every NULL as distinct, so the unique
            # index constrains only rows that actually have one.
            "ALTER TABLE messages ADD COLUMN source_id TEXT",
            # Image-job fields added with the rig's callback/progress/seed
            # round. All three are NULL on pre-existing rows, which is the
            # correct reading: no callback was armed, no progress was reported,
            # no seed was recorded.
            "ALTER TABLE image_jobs ADD COLUMN callback_token TEXT",
            "ALTER TABLE image_jobs ADD COLUMN progress TEXT",
            "ALTER TABLE image_jobs ADD COLUMN seed INTEGER",
        ):
            try:
                await self._db.execute(ddl)
                await self._db.commit()
            except Exception:
                pass  # column already present
        try:
            await self._db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_source_id "
                "ON messages(source_id) WHERE source_id IS NOT NULL")
            await self._db.commit()
        except Exception:
            # A pre-existing duplicate would fail the index build. Dedup is a
            # belt here, not the braces — the insert path also checks — so a
            # database with historic duplicates still starts.
            log.warning("could not create the source_id unique index", exc_info=True)
        await self._setup_fts()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Database not connected")
        return self._db

    # -- full-text search (FTS5) ------------------------------------------- #

    fts_ok: bool = False

    async def _setup_fts(self) -> None:
        """Build a contentless FTS5 index mirroring messages.content + triggers.

        FTS5 is compiled into CPython's bundled SQLite on virtually every build,
        but we degrade gracefully (search falls back to LIKE) if it isn't, so the
        app never fails to start over search.
        """
        try:
            await self.db.executescript(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts
                    USING fts5(content, content='messages', content_rowid='rowid');
                CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
                    INSERT INTO messages_fts(rowid, content) VALUES (new.rowid, new.content);
                END;
                CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
                    INSERT INTO messages_fts(messages_fts, rowid, content)
                        VALUES('delete', old.rowid, old.content);
                END;
                CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
                    INSERT INTO messages_fts(messages_fts, rowid, content)
                        VALUES('delete', old.rowid, old.content);
                    INSERT INTO messages_fts(rowid, content) VALUES (new.rowid, new.content);
                END;
                """
            )
            # Rebuild backfills the index from the content table; idempotent and
            # cheap to re-run at startup for a single-user DB.
            await self.db.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
            await self.db.commit()
            self.fts_ok = True
        except Exception:
            self.fts_ok = False
            # If FTS5 is unavailable but trigger DDL from a prior (fts-capable)
            # run persisted, those triggers reference a now-missing messages_fts
            # and would make EVERY message INSERT/UPDATE/DELETE fail. Drop them so
            # writes keep working and search simply falls back to LIKE.
            with contextlib.suppress(Exception):
                await self.db.executescript(
                    "DROP TRIGGER IF EXISTS messages_ai;"
                    "DROP TRIGGER IF EXISTS messages_au;"
                    "DROP TRIGGER IF EXISTS messages_ad;"
                )
                await self.db.commit()

    @staticmethod
    def _fts_query(q: str) -> str:
        """Turn arbitrary user text into a safe FTS5 MATCH expression.

        Each word becomes a quoted prefix term ANDed together; quoting avoids
        FTS5 syntax errors on punctuation, and the trailing * gives prefix match.
        """
        import re as _re
        terms = _re.findall(r"\w+", q or "")
        return " ".join(f'"{t}"*' for t in terms)

    async def search_messages(
        self, query: str, bot_ids: list[str] | None = None, limit: int = 60
    ) -> list[dict]:
        """Search message content across all threads. Returns newest-first hits
        with a short snippet. Uses FTS5 when available, else a LIKE scan."""
        q = (query or "").strip()
        if not q:
            return []
        limit = max(1, min(200, limit))
        rows: list[Any] = []
        fts_failed = False
        if self.fts_ok:
            match = self._fts_query(q)
            if not match:
                return []
            sql = (
                "SELECT m.id, m.thread_id, m.role, m.created_at, t.bot_id, t.title, "
                # Match delimiters are Private Use Area code points, chosen
                # because they have NO rendering or layout semantics of their
                # own. The previous markers were U+2068/U+2069 (bidi isolates):
                # invisible, but functional — they change directional handling
                # of the run they enclose, which is a live problem once the UI
                # renders right-to-left languages, and they are exactly the
                # characters "trojan source" scanners flag. PUA code points are
                # inert and effectively absent from real message text.
                f"       snippet(messages_fts, 0, '{FTS_MARK_OPEN}', '{FTS_MARK_CLOSE}', '…', 12) AS snippet "
                "FROM messages_fts f "
                "JOIN messages m ON m.rowid = f.rowid "
                "JOIN threads t ON t.id = m.thread_id "
                "WHERE messages_fts MATCH ? "
            )
            params: list[Any] = [match]
            if bot_ids:
                sql += f"AND t.bot_id IN ({','.join('?' * len(bot_ids))}) "
                params += list(bot_ids)
            sql += "ORDER BY bm25(messages_fts), m.created_at DESC LIMIT ?"
            params.append(limit)
            try:
                cur = await self.db.execute(sql, params)
                rows = await cur.fetchall()
            except Exception:
                rows = []
                fts_failed = True      # runtime FTS error → use the LIKE fallback
        if not rows and (not self.fts_ok or fts_failed):
            sql = (
                "SELECT m.id, m.thread_id, m.role, m.created_at, t.bot_id, t.title, "
                "       substr(m.content, 1, 160) AS snippet "
                "FROM messages m JOIN threads t ON t.id = m.thread_id "
                "WHERE m.content LIKE ? "
            )
            params = [f"%{q}%"]
            if bot_ids:
                sql += f"AND t.bot_id IN ({','.join('?' * len(bot_ids))}) "
                params += list(bot_ids)
            sql += "ORDER BY m.created_at DESC LIMIT ?"
            params.append(limit)
            cur = await self.db.execute(sql, params)
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # -- durability / backup ----------------------------------------------- #

    async def checkpoint(self, mode: str = "TRUNCATE") -> None:
        """Fold the WAL back into the main DB file (and bound its growth).

        TRUNCATE leaves a self-complete main file so a filesystem snapshot isn't
        stale; safe to call periodically and on shutdown. The PRAGMA returns a
        row (busy, log, checkpointed) — we MUST consume it, or the cursor stays
        open and a following VACUUM fails with 'SQL statements in progress'."""
        with contextlib.suppress(Exception):
            cur = await self.db.execute(f"PRAGMA wal_checkpoint({mode});")
            await cur.fetchall()
            await cur.close()

    async def backup_to(self, dest: Path) -> Path:
        """Write a consistent online snapshot of the DB to `dest` (WAL-safe).

        Uses VACUUM INTO, which does not require exclusive access and yields a
        defragmented single-file copy that opens instantly and is easy to verify.
        """
        dest.parent.mkdir(parents=True, exist_ok=True)
        await self.checkpoint("PASSIVE")
        # VACUUM INTO needs autocommit (we run isolation_level=None) and a path
        # with no existing file.
        if dest.exists():
            dest.unlink()

        # ON ITS OWN CONNECTION, DELIBERATELY.
        #
        # VACUUM refuses to run while ANY statement is active on the same
        # connection, and this one is shared with every live query in the app.
        # The boot snapshot fires 60s after start, straight into startup
        # recovery and the gap sweep, and lost the race:
        #
        #   sqlite3.OperationalError: cannot VACUUM - SQL statements in progress
        #
        # It then does not retry until the next interval — six hours later —
        # while /api/health reports last_backup_ok:false the whole time. The
        # family's backup should not depend on the app being momentarily idle.
        #
        # A private connection has no statements but its own, so the race
        # cannot happen. In a thread because VACUUM is synchronous and can take
        # seconds on a large database — on the event loop it would stall every
        # open chat.
        def _vacuum_into(src: str, out: str) -> None:
            con = sqlite3.connect(src, timeout=30, isolation_level=None)
            try:
                con.execute("PRAGMA busy_timeout=30000")
                con.execute("VACUUM INTO ?", (out,))
            finally:
                con.close()

        await asyncio.to_thread(_vacuum_into, str(self.path), str(dest))
        return dest

    async def integrity_ok(self) -> bool:
        """Fast structural check (quick_check). True iff the DB reports 'ok'."""
        try:
            cur = await self.db.execute("PRAGMA quick_check;")
            row = await cur.fetchone()
            await cur.fetchall()      # drain + close so no statement stays "in progress"
            await cur.close()
            return bool(row) and str(row[0]).lower() == "ok"
        except Exception:
            return False

    async def reset_inflight_threads(self) -> list[dict]:
        """Clear any thread stuck in status='thinking' (no turn survives a restart).

        Returns the affected threads ([{id, bot_id}]) so the caller can reconcile
        each one's transcript for a reply that landed just before the crash."""
        cur = await self.db.execute(
            "SELECT id, bot_id FROM threads WHERE status = 'thinking'"
        )
        rows = [dict(r) for r in await cur.fetchall()]
        if rows:
            await self.db.execute(
                "UPDATE threads SET status = 'idle' WHERE status = 'thinking'"
            )
            await self.db.commit()
        return rows

    async def clear_stale_error_threads(self) -> list[dict]:
        """Reset status='error' to 'idle' on boot.

        `error` is per-TURN state: it is set when an agent turn fails, and the
        client is told at the same moment by a WS `error` frame. The turn cannot
        resume across a restart any more than a 'thinking' one can, so an
        `error` still on a thread at boot describes a turn that ended long ago —
        on this box, seven of them, the oldest from 2026-07-29. It is stale
        state, not history: the failure itself lives in the thread's messages
        and in the log.

        Returns the affected threads so the caller can log and broadcast."""
        cur = await self.db.execute(
            "SELECT id, bot_id FROM threads WHERE status = 'error'"
        )
        rows = [dict(r) for r in await cur.fetchall()]
        if rows:
            await self.db.execute(
                "UPDATE threads SET status = 'idle' WHERE status = 'error'"
            )
            await self.db.commit()
        return rows

    async def delete_files_missing_from(self, files_dir) -> list[dict]:
        """Delete file rows whose blob is genuinely absent from ``files_dir``.

        A row with no blob is not a record, it is a trap: /api/files lists it,
        the File Server shows it, its download 404s, and an agent told to "read
        it off disk" fails on a path that was never going to exist. Its `size`
        also counts against the server-wide storage cap, so phantoms shrink the
        real budget.

        SAFETY: the caller checks that ``files_dir`` resolves to a real
        directory FIRST. If it does not (an unmounted share, a broken symlink)
        every blob looks absent and this would delete the whole table — so that
        case must skip the purge entirely, not run it. Here we only ever test
        one specific file at a time, and delete by primary key.
        """
        rows = await self.list_files()
        gone = [r for r in rows if not (files_dir / r["stored_name"]).is_file()]
        for r in gone:
            await self.db.execute("DELETE FROM files WHERE id = ?", (r["id"],))
        if gone:
            await self.db.commit()
        return gone

    # -- export (full, unpaginated dump) ----------------------------------- #

    async def all_threads(self, include_archived: bool = True) -> list[ThreadOut]:
        sql = (
            "SELECT t.*, "
            f"  {self._LAST_MESSAGE_SQL}, "
            "  (SELECT COUNT(*) FROM messages m WHERE m.thread_id = t.id) AS message_count, "
            f"  {self._UNREAD_SQL} "
            "FROM threads t "
        )
        if not include_archived:
            sql += "WHERE t.is_archived = 0 "
        sql += "ORDER BY t.bot_id, t.created_at"
        cur = await self.db.execute(sql)
        return [self._thread_from_row(r) for r in await cur.fetchall()]

    async def dump_messages(self, thread_id: str) -> list[MessageOut]:
        """Every message in a thread, oldest→newest, no limit (for export)."""
        cur = await self.db.execute(
            "SELECT * FROM messages WHERE thread_id = ? ORDER BY created_at, rowid",
            (thread_id,),
        )
        return [self._message_from_row(r) for r in await cur.fetchall()]

    async def count_messages(self) -> int:
        cur = await self.db.execute("SELECT COUNT(*) AS n FROM messages")
        row = await cur.fetchone()
        return int(row["n"]) if row else 0

    # -- threads ------------------------------------------------------------ #

    async def create_thread(
        self,
        bot_id: str,
        title: str | None = None,
        thread_id: str | None = None,
        status: str = "idle",
        avatar_from_pool: bool = False,
    ) -> ThreadOut:
        """``avatar_from_pool`` marks an ELIGIBLE creation path: user-created
        threads and the daily rollover draw from the bot's avatar pool (when
        it has one), while mirror/import/drop threads keep the plain capture —
        they arrive in bursts and must never drain the pool."""
        tid = thread_id or new_id()
        ts = now_iso()
        snap: str | None = None
        pinned = 0
        bot = config.get_bot(bot_id)
        if avatar_from_pool and bot is not None and getattr(bot, "avatar_pool", False):
            # First eligible thread of the day wears the CURRENT (daily) face —
            # the claim below is race-guarded, so exactly one creation takes
            # the slot. Everyone after it draws a one-shot pair from the pool.
            if not await self._claim_daily_face(bot_id):
                snap, explicit = await asyncio.to_thread(
                    avatar_pool.draw_snapshot_for_thread, bot)
                if snap and explicit:
                    # A pool draw is this thread's OWN picture: without the
                    # explicit pin, the daily rotation's re-pin sweep of
                    # message-less threads would overwrite it hours later.
                    pinned = 1
        if snap is None:
            # Freeze the bot's current face. Best-effort by design: a snapshot
            # is a decoration, so anything that goes wrong yields None and the
            # thread renders with the live avatar — the pre-feature behaviour.
            #
            # Off the event loop: snapshot_id stats/reads/hashes the face and
            # copies the -full sibling (up to a few MB), and every OTHER caller
            # already wraps it in to_thread. Inline here it blocked the loop on
            # every thread INSERT.
            snap = await asyncio.to_thread(avatar_snapshots.snapshot_id, bot)
        await self.db.execute(
            "INSERT INTO threads (id, bot_id, title, created_at, updated_at, status, is_archived, is_pinned, avatar_snapshot, avatar_pinned) "
            "VALUES (?, ?, ?, ?, ?, ?, 0, 0, ?, ?)",
            (tid, bot_id, title, ts, ts, status, snap, pinned),
        )
        await self.db.commit()
        return ThreadOut(
            id=tid, bot_id=bot_id, title=title, created_at=ts, updated_at=ts,
            status=status, is_archived=False, is_pinned=False, last_message=None, message_count=0,
            avatar_snapshot=snap,
            # Must be set here too: this path builds the model directly and does
            # not go through _thread_from_row, so a thread reported "" for its
            # avatar at the exact moment it was created — the one response the
            # client uses to render the new row.
            avatar_url=avatar_snapshots.url_for(tid, snap),
        )

    async def _claim_daily_face(self, bot_id: str) -> bool:
        """Take today's daily-face slot for a bot. True exactly once per day.

        The guarded UPDATE is the claim: after the first winner sets
        ``used_date`` to today, every later attempt's WHERE fails (rowcount
        0), so concurrent creations cannot both wear the daily face. A date
        rollover makes the stored date stale and the next claim succeeds.
        """
        today = local_date()
        await self.db.execute(
            "INSERT OR IGNORE INTO avatar_pool_daily (bot_id, used_date) VALUES (?, '')",
            (bot_id,),
        )
        cur = await self.db.execute(
            "UPDATE avatar_pool_daily SET used_date = ? WHERE bot_id = ? AND used_date != ?",
            (today, bot_id, today),
        )
        await self.db.commit()
        return cur.rowcount > 0

    # Oldest not-yet-read non-user message — powers the unread indicators
    # (dot when set; the frontend turns it red once it's >24h old).
    # The thread-list preview line. It must show what the AGENT SAID, not what
    # the runtime narrated: a turn that ends in a failed tool call stores a
    # "sub" message ("⚠️ 🛠️ Exec failed: `show ~/notes/status.md` (exit 1)"),
    # and picking the newest row regardless put that string on the thread row —
    # so the chat list advertised an internal error instead of the reply that
    # was sitting right underneath it. Sub messages are already collapsed in the
    # bubble view for exactly this reason; the preview simply never got the memo.
    #
    # COALESCE(...) NOT IN (1, 'true') rather than a plain != : metadata is
    # absent on most rows, and JSON true has surfaced as both 1 and 'true'
    # depending on how the row was written.
    _LAST_MESSAGE_SQL = """
                   (SELECT m.content FROM messages m WHERE m.thread_id = t.id
                      AND COALESCE(json_extract(m.metadata, '$.sub'), 0) NOT IN (1, 'true')
                      ORDER BY m.created_at DESC, m.rowid DESC LIMIT 1) AS last_message
    """

    # An unread dot must mean "there is something here to READ".
    #
    # This counted any row with role != 'user', knowing nothing about
    # metadata.sub — while _LAST_MESSAGE_SQL four lines above deliberately
    # excludes it. So a turn whose only output was collapsed working output
    # (a failed tool call, a demoted narration payload) lit the dot on every
    # device; the family opened the thread and found nothing new. The same for
    # a reaction trace, which is a picture's footprint rather than speech.
    #
    # This is the mirror image of the losses fixed elsewhere: there, a message
    # existed and nobody saw it; here, nobody saw anything and the app insisted
    # there was something. Both are the indicator disagreeing with reality, and
    # the same rule fixes both — count what a person can actually read.
    _UNREAD_SQL = """
                   (SELECT MIN(m.created_at) FROM messages m WHERE m.thread_id = t.id
                      AND m.role != 'user'
                      AND COALESCE(json_extract(m.metadata, '$.sub'), 0) NOT IN (1, 'true')
                      AND COALESCE(json_extract(m.metadata, '$.kind'), '') != 'reaction'
                      AND TRIM(COALESCE(m.content, '')) != ''
                      AND m.created_at > COALESCE(t.last_read_at, '')) AS unread_since
    """

    async def get_thread(self, thread_id: str) -> ThreadOut | None:
        cur = await self.db.execute(
            f"""
            SELECT t.*,
                   {self._LAST_MESSAGE_SQL},
                   (SELECT COUNT(*) FROM messages m WHERE m.thread_id = t.id) AS message_count,
                   {self._UNREAD_SQL}
            FROM threads t WHERE t.id = ?
            """,
            (thread_id,),
        )
        row = await cur.fetchone()
        return self._thread_from_row(row) if row else None

    async def resolve_thread_id(self, thread_id: str) -> str | None:
        """Case-insensitive thread-id lookup, returning the canonical id.

        The OpenClaw gateway lowercases whole session keys, so a thread id
        recovered from one (e.g. "daily-scout-…") may differ in case from the
        real row ("daily-Scout-…")."""
        cur = await self.db.execute(
            "SELECT id FROM threads WHERE id = ? COLLATE NOCASE LIMIT 1",
            (thread_id,),
        )
        row = await cur.fetchone()
        return row[0] if row else None

    async def list_threads(self, bot_id: str, include_archived: bool = False) -> list[ThreadOut]:
        sql = f"""
            SELECT t.*,
                   {self._LAST_MESSAGE_SQL},
                   (SELECT COUNT(*) FROM messages m WHERE m.thread_id = t.id) AS message_count,
                   {self._UNREAD_SQL}
            FROM threads t
            WHERE t.bot_id = ?
        """
        if not include_archived:
            sql += " AND t.is_archived = 0"
        sql += " ORDER BY t.is_pinned DESC, t.updated_at DESC, t.rowid DESC"
        cur = await self.db.execute(sql, (bot_id,))
        rows = await cur.fetchall()
        return [self._thread_from_row(r) for r in rows]

    async def update_thread_status(self, thread_id: str, status: str) -> None:
        await self.db.execute(
            "UPDATE threads SET status = ? WHERE id = ?", (status, thread_id)
        )
        await self.db.commit()

    async def set_thread_avatar(self, thread_id: str, snapshot_id: str,
                                explicit: bool = False) -> bool:
        """Re-pin one thread's avatar snapshot. Returns False for a missing thread.

        `explicit=True` marks the pin as deliberate (the thread-avatar endpoint),
        so the daily rotation's re-pin of unused threads leaves it alone.
        """
        cur = await self.db.execute(
            "UPDATE threads SET avatar_snapshot = ?, avatar_pinned = ? WHERE id = ?",
            (snapshot_id, 1 if explicit else 0, thread_id),
        )
        await self.db.commit()
        return cur.rowcount > 0

    async def repin_unused_thread_avatars(self, bot_id: str, snapshot_id: str) -> list[str]:
        """Point every UNUSED thread of a bot at the (new) current avatar.

        A thread wears the face it started under — but a thread with no
        messages hasn't started: it was pre-created (the daily rollover does
        this, and so do agents) and pinning it to the face that happened to be
        current at INSERT time is how "today's chat wears yesterday's picture"
        happens when the avatar changes in between. The first message freezes
        the face; this never touches a thread that has one.

        Returns the ids that changed so the caller can broadcast them.
        """
        cur = await self.db.execute(
            """
            SELECT id FROM threads
            WHERE bot_id = ?
              AND COALESCE(avatar_snapshot, '') != ?
              AND COALESCE(avatar_pinned, 0) = 0
              AND NOT EXISTS (SELECT 1 FROM messages m WHERE m.thread_id = threads.id)
            """,
            (bot_id, snapshot_id),
        )
        ids = [r[0] for r in await cur.fetchall()]
        if ids:
            await self.db.executemany(
                "UPDATE threads SET avatar_snapshot = ? WHERE id = ?",
                [(snapshot_id, tid) for tid in ids],
            )
            await self.db.commit()
        return ids

    async def touch_thread(self, thread_id: str) -> None:
        await self.db.execute(
            "UPDATE threads SET updated_at = ? WHERE id = ?", (now_iso(), thread_id)
        )
        await self.db.commit()

    async def set_title_if_empty(self, thread_id: str, title: str) -> bool:
        """Set the title only if it's currently NULL/empty. Returns True if set."""
        cur = await self.db.execute(
            "UPDATE threads SET title = ? WHERE id = ? AND (title IS NULL OR title = '')",
            (title, thread_id),
        )
        await self.db.commit()
        return cur.rowcount > 0

    async def rename_thread(self, thread_id: str, title: str) -> None:
        await self.db.execute(
            "UPDATE threads SET title = ? WHERE id = ?", (title, thread_id)
        )
        await self.db.commit()

    async def archive_thread(self, thread_id: str) -> None:
        await self.db.execute(
            "UPDATE threads SET is_archived = 1 WHERE id = ?", (thread_id,)
        )
        await self.db.commit()

    async def delete_thread(self, thread_id: str) -> None:
        await self.db.execute("DELETE FROM threads WHERE id = ?", (thread_id,))
        await self.db.commit()

    async def pin_thread(self, thread_id: str, pinned: bool) -> None:
        await self.db.execute(
            "UPDATE threads SET is_pinned = ? WHERE id = ?", (1 if pinned else 0, thread_id)
        )
        await self.db.commit()

    async def mark_thread_read(self, thread_id: str) -> None:
        await self.db.execute(
            "UPDATE threads SET last_read_at = ? WHERE id = ?", (now_iso(), thread_id)
        )
        await self.db.commit()

    async def unread_summary(self) -> list[dict]:
        """All threads with unread bot messages: [{thread_id, bot_id, unread_since}]."""
        cur = await self.db.execute(
            f"""
            SELECT t.id AS thread_id, t.bot_id,
                   {self._UNREAD_SQL}
            FROM threads t
            WHERE t.is_archived = 0
            """
        )
        rows = await cur.fetchall()
        return [
            {"thread_id": r["thread_id"], "bot_id": r["bot_id"], "unread_since": r["unread_since"]}
            for r in rows if r["unread_since"]
        ]

    async def find_or_create_daily_thread(
        self, bot_id: str, date: str | None = None, title: str | None = None
    ) -> tuple[ThreadOut, bool]:
        """Find-or-create a stable daily thread for a bot.

        Daily threads use a deterministic id (`daily-<bot>-<date>`) so OpenClaw
        cron/timers can post to "today" repeatedly without spawning duplicates.
        Returns (thread, created).
        """
        d = date or local_date()
        tid = f"daily-{bot_id}-{d}"
        existing = await self.get_thread(tid)
        if existing:
            return existing, False
        try:
            # The daily rollover is an ELIGIBLE creation: normally it runs
            # right after the 02:00 rotation and therefore takes the day's
            # daily-face slot; a same-day re-creation (the daily cleanup can
            # delete an unused daily thread) draws from the pool instead.
            thread = await self.create_thread(
                bot_id=bot_id, title=title or f"Daily · {d}", thread_id=tid,
                avatar_from_pool=True,
            )
            return thread, True
        except sqlite3.IntegrityError:
            # Concurrent create (e.g. two cron pushes hitting the same daily id
            # at once) — the other writer won the race. Return its thread.
            existing = await self.get_thread(tid)
            if existing:
                return existing, False
            raise

    # -- messages ----------------------------------------------------------- #

    async def transcript_items_seen(self, thread_id: str) -> set[str]:
        """Every transcript item already accounted for in this thread.

        Read once per sweep rather than per item: a long thread has hundreds of
        items and the sweep runs over every recent thread on a timer.
        """
        cur = await self.db.execute(
            "SELECT item_id FROM transcript_seen WHERE thread_id = ?", (thread_id,))
        return {r[0] for r in await cur.fetchall()}

    async def mark_transcript_items(self, thread_id: str, item_ids: list[str]) -> None:
        """Record items as considered — delivered OR found already present.

        Both outcomes mean the same thing to a gap-filler: this item is in the
        chat, stop looking at it. Written after the items are processed, so a
        failed pass simply reconsiders them next time.
        """
        if not item_ids:
            return
        ts = now_iso()
        await self.db.executemany(
            "INSERT OR IGNORE INTO transcript_seen (item_id, thread_id, created_at) "
            "VALUES (?, ?, ?)", [(i, thread_id, ts) for i in item_ids])
        await self.db.commit()

    async def source_id_seen(self, source_id: str) -> bool:
        """Has a message with this source identity already been stored?

        The primary dedup. Content comparison could only ever look at the
        trailing assistant run — a re-scan from offset 0 therefore replayed
        everything above the last user message into the family chat. Identity
        does not care how far back it was, or whether the text repeats a
        legitimate earlier reply ("Done.", "No new items today.") that content
        matching would have wrongly swallowed.
        """
        if not source_id:
            return False
        cur = await self.db.execute(
            "SELECT 1 FROM messages WHERE source_id = ? LIMIT 1", (source_id,))
        return await cur.fetchone() is not None

    async def add_message(
        self,
        thread_id: str,
        role: str,
        content: str,
        media_url: str | None = None,
        metadata: dict[str, Any] | None = None,
        msg_id: str | None = None,
        created_at: str | None = None,
        source_id: str | None = None,
    ) -> MessageOut:
        mid = msg_id or new_id()
        # A RECOVERED message keeps its original time. The gap sweep imports
        # answers a live path missed — sometimes days later — and stamping them
        # "now" files a Monday reply at the bottom of Monday's thread dated
        # Wednesday, out of order inside its own conversation. Observed the
        # first time the sweep ran: 41 messages from three different days all
        # landed at once, timestamped today.
        ts = created_at or now_iso()
        meta_json = _json.dumps(metadata) if metadata else None
        await self.db.execute(
            "INSERT INTO messages (id, thread_id, role, content, media_url, created_at, metadata, source_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (mid, thread_id, role, content, media_url, ts, meta_json, source_id),
        )
        # Bump the thread's updated_at so it sorts to the top of the list —
        # but only FORWARD. A recovered message keeps its original created_at
        # (above), and an unconditional write here let one backfilled old
        # answer shove a thread with today's activity back days: off the top
        # of the list, and out of the gap sweep's 48-hour recency window,
        # which silently exempted exactly the threads being repaired from
        # further repair.
        await self.db.execute(
            "UPDATE threads SET updated_at = MAX(COALESCE(updated_at, ''), ?) "
            "WHERE id = ?", (ts, thread_id)
        )
        await self.db.commit()
        return MessageOut(
            id=mid, thread_id=thread_id, role=role, content=content,
            media_url=media_url, created_at=ts, metadata=metadata,
        )

    async def get_message(self, msg_id: str) -> MessageOut | None:
        cur = await self.db.execute("SELECT * FROM messages WHERE id = ?", (msg_id,))
        row = await cur.fetchone()
        return self._message_from_row(row) if row else None

    async def delete_message(self, msg_id: str) -> None:
        await self.db.execute("DELETE FROM messages WHERE id = ?", (msg_id,))
        await self.db.commit()

    async def update_message_content(self, msg_id: str, content: str) -> None:
        await self.db.execute(
            "UPDATE messages SET content = ? WHERE id = ?", (content, msg_id))
        await self.db.commit()

    async def update_message_metadata(self, msg_id: str,
                                      metadata: dict[str, Any] | None) -> None:
        """Replace a message's metadata blob (callers merge first)."""
        await self.db.execute(
            "UPDATE messages SET metadata = ? WHERE id = ?",
            (_json.dumps(metadata) if metadata else None, msg_id))
        await self.db.commit()

    # ----------------------------------------------------------------- #
    # Image jobs
    #
    # Deliberately thin: no ORM, no state machine in SQL. The worker owns the
    # transitions; these four calls just make them durable. Every write stamps
    # `updated_at`, which is what the resume path uses to tell a job that is
    # merely slow from one whose worker died.
    # ----------------------------------------------------------------- #

    async def add_image_job(self, job_id: str, message_id: str, thread_id: str,
                            bot_id: str, spec: str, *,
                            state: str = "queued",
                            callback_token: str | None = None) -> dict:
        ts = now_iso()
        await self.db.execute(
            "INSERT INTO image_jobs (id, message_id, thread_id, bot_id, spec, "
            "state, callback_token, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (job_id, message_id, thread_id, bot_id, spec, state,
             callback_token, ts, ts))
        await self.db.commit()
        return {"id": job_id, "message_id": message_id, "thread_id": thread_id,
                "bot_id": bot_id, "spec": spec, "state": state,
                "rig_job_id": None, "error": None, "media_url": None,
                "callback_token": callback_token, "progress": None,
                "seed": None, "created_at": ts, "updated_at": ts}

    async def update_image_job(self, job_id: str, *, require_open: bool = False,
                               **fields: Any) -> int:
        """Patch a job row. Only the columns named are touched.

        A whitelist rather than an f-string over ``fields``: this is the one
        place a caller's dict reaches SQL, and a typo'd key should be a loud
        KeyError here rather than a silently-ignored update that leaves a job
        stuck in `running` forever.

        ``require_open`` makes the write a COMPARE-AND-SET on the state, and
        the returned rowcount says whether it landed. Every write that ENDS a
        job must use it, because two coroutines can hold the same row: the
        deadline sweep and a delivery that is still inside `fetch()`. Without
        the predicate the loser overwrites the winner, and both orderings are
        wrong — a delivered picture rewritten into "⚠️ timed out", or a
        terminal row un-terminaled. It also covers the row simply being GONE
        (deleting the placeholder cascades the job away), which is how a
        delivery into a deleted thread used to write a file, rewrite nothing
        and log "delivered".
        """
        allowed = ("state", "rig_job_id", "error", "media_url", "progress",
                   "seed")
        sets, values = [], []
        for k, v in fields.items():
            if k not in allowed:
                raise KeyError(f"image_jobs has no updatable column {k!r}")
            sets.append(f"{k} = ?")
            values.append(v)
        if not sets:
            return 0
        sets.append("updated_at = ?")
        values.extend([now_iso(), job_id])
        where = "id = ?"
        if require_open:
            where += " AND state IN ('queued', 'running')"
        cur = await self.db.execute(
            f"UPDATE image_jobs SET {', '.join(sets)} WHERE {where}", values)
        await self.db.commit()
        return cur.rowcount or 0

    async def get_image_job(self, job_id: str) -> dict | None:
        cur = await self.db.execute(
            "SELECT * FROM image_jobs WHERE id = ?", (job_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def open_image_jobs(self) -> list[dict]:
        """Every job that has not reached a terminal state, oldest first."""
        cur = await self.db.execute(
            "SELECT * FROM image_jobs WHERE state IN ('queued', 'running') "
            "ORDER BY created_at")
        return [dict(r) for r in await cur.fetchall()]

    async def open_image_jobs_for_thread(self, thread_id: str) -> list[dict]:
        """Open jobs belonging to one thread — what a delete has to withdraw.

        Deleting the thread cascades the placeholder away, so nothing is left
        to rewrite; the render on the rig, however, keeps burning a GPU unless
        somebody says otherwise.
        """
        cur = await self.db.execute(
            "SELECT * FROM image_jobs WHERE thread_id = ? "
            "AND state IN ('queued', 'running') ORDER BY created_at",
            (thread_id,))
        return [dict(r) for r in await cur.fetchall()]

    async def open_image_jobs_for_message(self, message_id: str) -> list[dict]:
        """Open jobs whose placeholder is this message. Same reasoning."""
        cur = await self.db.execute(
            "SELECT * FROM image_jobs WHERE message_id = ? "
            "AND state IN ('queued', 'running')", (message_id,))
        return [dict(r) for r in await cur.fetchall()]

    async def get_last_user_message(self, thread_id: str) -> MessageOut | None:
        cur = await self.db.execute(
            "SELECT * FROM messages WHERE thread_id = ? AND role = 'user' "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (thread_id,),
        )
        row = await cur.fetchone()
        return self._message_from_row(row) if row else None

    async def has_assistant_message(self, thread_id: str) -> bool:
        """Whether the thread already holds any assistant reply (cheap probe —
        the reaction autopilot uses it to spot a day-thread's opening reply)."""
        cur = await self.db.execute(
            "SELECT 1 FROM messages WHERE thread_id = ? AND role = 'assistant' LIMIT 1",
            (thread_id,))
        return await cur.fetchone() is not None

    async def list_messages(
        self, thread_id: str, limit: int = 200, before_id: str | None = None
    ) -> tuple[list[MessageOut], bool]:
        """Return up to `limit` messages oldest→newest, with a has_more flag.

        When `before_id` is given, returns the page of messages immediately
        preceding that message (for scroll-back pagination).
        """
        params: list[Any] = [thread_id]
        sql = "SELECT * FROM messages WHERE thread_id = ?"
        if before_id:
            # Only apply the cursor if it resolves to a real row IN THIS THREAD;
            # otherwise a stale/foreign before_id makes the row-value comparison
            # NULL (empty page) or anchors on another thread's timeline.
            cur = await self.db.execute(
                "SELECT 1 FROM messages WHERE id = ? AND thread_id = ?",
                (before_id, thread_id),
            )
            anchor = await cur.fetchone()
            if anchor:
                sql += (
                    " AND (created_at, rowid) < "
                    "(SELECT created_at, rowid FROM messages WHERE id = ?)"
                )
                params.append(before_id)
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        params.append(limit + 1)

        cur = await self.db.execute(sql, params)
        rows = await cur.fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        msgs = [self._message_from_row(r) for r in reversed(rows)]
        return msgs, has_more

    # -- file server -------------------------------------------------------- #

    async def add_file(
        self, name: str, stored_name: str, size: int, mime: str | None,
        source: str = "chat",
    ) -> dict:
        fid = new_id()
        ts = now_iso()
        await self.db.execute(
            "INSERT INTO files (id, name, stored_name, size, mime, created_at, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (fid, name, stored_name, size, mime, ts, source),
        )
        await self.db.commit()
        return {"id": fid, "name": name, "stored_name": stored_name,
                "size": size, "mime": mime, "created_at": ts, "source": source}

    async def adopt_file(
        self, name: str, stored_name: str, size: int, mime: str | None,
        created_at: str, source: str = "fileserver",
    ) -> dict:
        """Insert a row for a blob that is ALREADY on disk.

        Same as add_file() but the caller supplies created_at (the blob's mtime)
        instead of "now", so an adopted file keeps its real age — retention
        (delete_files_before) and the listing order stay honest.
        """
        fid = new_id()
        await self.db.execute(
            "INSERT INTO files (id, name, stored_name, size, mime, created_at, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (fid, name, stored_name, size, mime, created_at, source),
        )
        await self.db.commit()
        return {"id": fid, "name": name, "stored_name": stored_name,
                "size": size, "mime": mime, "created_at": created_at,
                "source": source}

    async def list_files(self) -> list[dict]:
        cur = await self.db.execute(
            "SELECT * FROM files ORDER BY created_at DESC, rowid DESC"
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def total_file_bytes(self) -> int:
        """Sum of all recorded blob sizes (chat + fileserver). Backs the
        server-wide storage cap so uploads can't fill the disk unbounded."""
        cur = await self.db.execute("SELECT COALESCE(SUM(size), 0) FROM files")
        row = await cur.fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    async def get_file(self, file_id: str) -> dict | None:
        cur = await self.db.execute("SELECT * FROM files WHERE id = ?", (file_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def delete_file(self, file_id: str) -> dict | None:
        f = await self.get_file(file_id)
        if f:
            await self.db.execute("DELETE FROM files WHERE id = ?", (file_id,))
            await self.db.commit()
        return f

    async def delete_files_before(self, cutoff_iso: str) -> list[dict]:
        """Delete all files with created_at <= cutoff (inclusive). Returns them
        so the caller can remove the blobs from disk."""
        cur = await self.db.execute(
            "SELECT * FROM files WHERE created_at <= ?", (cutoff_iso,)
        )
        rows = [dict(r) for r in await cur.fetchall()]
        if rows:
            await self.db.execute(
                "DELETE FROM files WHERE created_at <= ?", (cutoff_iso,)
            )
            await self.db.commit()
        return rows

    # ----------------------------------------------------------------- #
    # Jobs board (added 2026-09-14)
    #
    # Storage for the additive jobs / job_events / job_feedback /
    # job_profile tables. All four are simple — the write paths are the
    # only complicated thing (atomic vote + event + feedback + recompute),
    # which lives in jobs.py, not here.
    # ----------------------------------------------------------------- #

    async def get_job(self, thread_id: str) -> dict | None:
        cur = await self.db.execute(
            "SELECT * FROM jobs WHERE thread_id = ?", (thread_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def upsert_job(self, job: dict) -> None:
        """Insert or replace the structured job row.

        Tags are stored JSON-encoded at the boundary; callers pass either a
        list or a string (already-encoded). The caller is responsible for
        the 32-tag cap and tag-trim rules — this is a dumb shelf.
        """
        tags = job.get("tags") or []
        if isinstance(tags, list):
            import json as _json
            tags_json = _json.dumps(tags)
        else:
            tags_json = tags
        await self.db.execute(
            "INSERT OR REPLACE INTO jobs ("
            "thread_id, url, title, company, location, remote_type, seniority,"
            "salary_min, salary_max, salary_currency, tags, source_agent,"
            "source_run_id, posted_at, first_seen, last_seen, brief, state,"
            "duplicate_of, expires_at, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (job["thread_id"], job["url"], job["title"],
             job.get("company", ""), job.get("location", ""),
             job.get("remote_type", "unknown"),
             job.get("seniority", "unknown"),
             job.get("salary_min"), job.get("salary_max"),
             job.get("salary_currency", "USD"), tags_json,
             job.get("source_agent", ""), job.get("source_run_id"),
             job.get("posted_at"), job["first_seen"], job["last_seen"],
             job.get("brief", ""), job.get("state", "pending"),
             job.get("duplicate_of"), job.get("expires_at"),
             job["created_at"], job["updated_at"]),
        )
        await self.db.commit()

    async def list_jobs(self, *, state: str | None = None,
                        source_agent: str | None = None,
                        tag: str | None = None,
                        limit: int = 200) -> list[dict]:
        """List job rows. Filtering is intentionally minimal in the storage
        layer — the router does full-text + score-based selection on top of
        this set."""
        sql = "SELECT * FROM jobs WHERE 1=1"
        params: list[Any] = []
        if state:
            sql += " AND state = ?"
            params.append(state)
        if source_agent:
            sql += " AND source_agent = ?"
            params.append(source_agent)
        if tag:
            sql += " AND tags LIKE ?"
            params.append(f'%"{tag}"%')
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        cur = await self.db.execute(sql, params)
        return [dict(r) for r in await cur.fetchall()]

    async def update_job_state(self, thread_id: str, state: str,
                               duplicate_of: str | None = None) -> None:
        await self.db.execute(
            "UPDATE jobs SET state = ?, duplicate_of = ?, updated_at = ? "
            "WHERE thread_id = ?",
            (state, duplicate_of, now_iso(), thread_id),
        )
        await self.db.commit()

    async def update_job_tags(self, thread_id: str, tags: list[str]) -> None:
        import json as _json
        await self.db.execute(
            "UPDATE jobs SET tags = ?, updated_at = ? WHERE thread_id = ?",
            (_json.dumps(tags), now_iso(), thread_id),
        )
        await self.db.commit()

    async def touch_job_seen(self, thread_id: str) -> None:
        await self.db.execute(
            "UPDATE jobs SET last_seen = ? WHERE thread_id = ?",
            (now_iso(), thread_id))
        await self.db.commit()

    async def add_job_event(self, event: dict) -> None:
        await self.db.execute(
            "INSERT INTO job_events (id, thread_id, type, from_state, to_state,"
            " reason_tag, comment, actor, payload, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (event["id"], event["thread_id"], event["type"],
             event.get("from_state"), event.get("to_state"),
             event.get("reason_tag"), event.get("comment"),
             event["actor"], event.get("payload"),
             event["created_at"]),
        )
        await self.db.commit()

    async def list_job_events(self, thread_id: str, limit: int = 50) -> list[dict]:
        cur = await self.db.execute(
            "SELECT * FROM job_events WHERE thread_id = ?"
            " ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (thread_id, limit))
        return [dict(r) for r in await cur.fetchall()]

    async def add_job_feedback(self, feedback: dict) -> None:
        await self.db.execute(
            "INSERT INTO job_feedback (id, thread_id, signal, reason_tag, "
            "comment, actor, payload, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (feedback["id"], feedback["thread_id"], feedback["signal"],
             feedback.get("reason_tag"), feedback.get("comment"),
             feedback["actor"], feedback.get("payload"),
             feedback["created_at"]),
        )
        await self.db.commit()

    async def list_job_feedback(
        self, *, thread_id: str | None = None, since: str | None = None,
        limit: int = 10000,
    ) -> list[dict]:
        """Return feedback rows in created_at ASC order.

        Profile recompute depends on ASC ordering — the fold iterates rows
        oldest-first, latest per thread wins, see jobs_score.py. Limit is
        generous so a full recompute over a long history stays single-pass.
        """
        sql = "SELECT * FROM job_feedback WHERE 1=1"
        params: list[Any] = []
        if thread_id:
            sql += " AND thread_id = ?"
            params.append(thread_id)
        if since:
            sql += " AND created_at >= ?"
            params.append(since)
        sql += " ORDER BY created_at ASC LIMIT ?"
        params.append(limit)
        cur = await self.db.execute(sql, params)
        return [dict(r) for r in await cur.fetchall()]

    async def get_job_profile(self) -> dict | None:
        cur = await self.db.execute(
            "SELECT * FROM job_profile WHERE id = 1")
        row = await cur.fetchone()
        if row is None:
            # Singleton may not exist yet on a fresh DB. The recompute
            # path will write one the first time it runs; the scoring
            # path treats absence as the empty defaults.
            return None
        return dict(row)

    async def write_job_profile(self, profile: dict) -> None:
        """Replace the singleton profile row."""
        await self.db.execute(
            "INSERT OR REPLACE INTO job_profile ("
            "id, tag_weights, company_blocklist, location_blocklist,"
            " salary_history, preferred_remote, seniority_preference,"
            "yes_centroid, no_centroid, duplicate_hashes, reason_counts,"
            "yes_count, no_count, maybe_count, updated_at)"
            " VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (profile["tag_weights"], profile["company_blocklist"],
             profile["location_blocklist"], profile["salary_history"],
             profile["preferred_remote"], profile["seniority_preference"],
             profile.get("yes_centroid"), profile.get("no_centroid"),
             profile["duplicate_hashes"], profile["reason_counts"],
             profile["yes_count"], profile["no_count"],
             profile["maybe_count"], profile["updated_at"]),
        )
        await self.db.commit()

    async def recent_job_duplicate_hashes(self, since: str) -> list[dict]:
        """The rolling dedup window — returns {hash, thread_id, seen_at}
        entries from the profile, filtered to those seen in the last
        `since`. The hash list itself is parsed by jobs_dedup.py; here we
        only return the raw row so the caller doesn't re-hit the disk.
        """
        profile = await self.get_job_profile()
        if not profile:
            return []
        raw = profile.get("duplicate_hashes") or "[]"
        import json as _json
        try:
            entries = _json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError):
            return []
        return [e for e in entries if isinstance(e, dict)
                and e.get("seen_at", "") >= since]

    # -- row mappers -------------------------------------------------------- #

    @staticmethod
    def _thread_from_row(row: aiosqlite.Row) -> ThreadOut:
        keys = row.keys()
        # None for threads that predate the feature — they render the live
        # avatar, which is what they have always done.
        snap = row["avatar_snapshot"] if "avatar_snapshot" in keys else None
        return ThreadOut(
            id=row["id"],
            bot_id=row["bot_id"],
            title=row["title"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            status=row["status"],
            is_archived=bool(row["is_archived"]),
            is_pinned=bool(row["is_pinned"]) if "is_pinned" in keys else False,
            last_message=row["last_message"] if "last_message" in keys else None,
            message_count=row["message_count"] if "message_count" in keys else 0,
            unread_since=row["unread_since"] if "unread_since" in keys else None,
            avatar_snapshot=snap,
            avatar_url=avatar_snapshots.url_for(row["id"], snap),
        )

    @staticmethod
    def _message_from_row(row: aiosqlite.Row) -> MessageOut:
        meta = None
        if row["metadata"]:
            try:
                meta = _json.loads(row["metadata"])
            except (ValueError, TypeError):
                meta = None
        return MessageOut(
            id=row["id"],
            thread_id=row["thread_id"],
            role=row["role"],
            content=row["content"],
            media_url=row["media_url"],
            created_at=row["created_at"],
            metadata=meta,
            source_id=row["source_id"] if "source_id" in row.keys() else None,
        )
