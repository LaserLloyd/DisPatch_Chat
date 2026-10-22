"""Profile lifecycle: counts, Reset to default, User <-> Basic, personal bots, copies, Atlas.

4.0 chat-first profiles (BUILD-PLAN-v2 §1.3). One synchronous module behind one lock
(:data:`LOCK`): every operation here changes several stores at once (chats.db threads and
files, config.yaml personal bots, avatar files, voice-acks entries, the Image Studio
ledger, the advisor ledger, agent tasks, voice cursors), and two of them racing for the
same profile must not interleave.

Counts are numbers only, never names or content: an Admin sees "3 bots, 41 chats" for a
profile whose chats they can never open.

The three functions that delete for good - :func:`wipe_profile_content`, :func:`change_role`
and :func:`reset_profile` - are small on purpose and are what the final review reads first.

What a wipe removes, and what it keeps
--------------------------------------
Removes (everything keyed by the profile or by its threads):

* its threads - messages, their search index, image jobs, job-board rows and
  ``transcript_seen`` go with them by foreign key;
* the picture files those messages show (``/media/<name>``) and the avatar snapshots only
  those threads used, each deleted only when no surviving message / thread still names it;
* its personal bots: the config.yaml entry, the avatar files, the ``voice-acks.yaml`` entry;
* its ``source='chat'`` files (rows and blobs);
* its Image Studio ledger rows and their pictures, and ``prompts-<profile>.json``;
* the advisor-ledger requests, agent tasks (with events and approvals) and voice-chat
  cursors that belong to those threads.

Keeps: ``source='fileserver'`` files, push subscriptions, audit rows, cloned voices (Reset
keeps them; profile deletion and User -> Basic purge them through
``ext_profile_hooks.purge_profile_data``), and OpenClaw sessions of agent-bot threads (the
gateway owns those; they are not DisPatch's to delete).

Deleted rows are zeroed (``secure_delete``), the search index is merged and the WAL is
truncated, so the data directory holds no readable trace of the content afterwards.
Backup snapshots taken earlier (``<data>/backups``) are not rewritten and keep their copy
until they rotate out.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import io
import json
import logging
import os
import re
import secrets
import sqlite3
import tempfile
import threading
import time
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import (
    accounts,
    avatar_snapshots,
    backends_policy,
    characters,
    config,
    ext_profile_hooks,
    firstrun,
    ownership,
)
from .contracts.permissions import (
    ROLE_DEFAULTS,
    ROLE_GRANTABLE,
    Permission,
    Role,
    normalise_role,
)

log = logging.getLogger("local-chat.profiles")

#: Held for the whole of every operation below.
LOCK = threading.RLock()

#: How long a preview token (Reset, downgrade, delete) stays valid.
TOKEN_TTL_SECONDS = 300

#: Threads deleted per transaction. Small on purpose: the running app shares this file and
#: waits at most its busy timeout for a writer, so a big Reset is many short commits (and is
#: safe to repeat if it is interrupted - it only ever deletes).
_DELETE_BATCH = 25

_NAME_RE = re.compile(r"/media/([A-Za-z0-9][A-Za-z0-9._-]{0,127})")
_SNAP_RE = re.compile(r"\A[0-9a-f]{8,64}\.(?:png|jpe?g|webp|gif|svg)\Z")
_PID_RE = re.compile(r"\A[A-Za-z0-9_-]{1,64}\Z")
MAX_AVATAR_BYTES = 25 * 1024 * 1024
MAX_AVATAR_PIXELS = 64_000_000


class ProfileServiceError(ValueError):
    """A refused lifecycle operation. ``code`` is a stable slug (errors catalog); ``status``
    the HTTP status the API layer answers with (409 = a stale preview token)."""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class ContentCounts:
    """What a Reset / downgrade / delete would remove (or did remove)."""

    bots: int = 0           # personal bots (scope: personal, owner_profile == the profile)
    chats: int = 0          # threads owned by the profile
    pictures: int = 0       # chat images + Image Studio results owned by the profile
    voices: int = 0         # cloned voices the profile owns (ext_profile_hooks.count_profile_data)

    def as_dict(self) -> dict[str, int]:
        return {"bots": self.bots, "chats": self.chats, "pictures": self.pictures,
                "voices": self.voices}


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def _profile(profile_id: str) -> accounts.Profile:
    prof = accounts.get_profile(profile_id)
    if prof is None:
        raise ProfileServiceError("profile_not_found", "No such profile.", 404)
    return prof


def _can_own_bots(profile: accounts.Profile) -> bool:
    return profile.role in (Role.ADMIN, Role.USER)


def _require_owner_of_bots(profile: accounts.Profile) -> None:
    if not _can_own_bots(profile):
        raise ProfileServiceError(
            "basic_no_bots", "A Basic profile uses the bots an Admin gives it; it has none of its own.", 422)


def _safe_pid(profile_id: str) -> str:
    if not isinstance(profile_id, str) or not _PID_RE.match(profile_id):
        raise ProfileServiceError("profile_not_found", "No such profile.", 404)
    return profile_id


@contextlib.contextmanager
def _chats(*, secure: bool = False) -> Iterator[sqlite3.Connection]:
    """A short-lived connection to chats.db. ``secure`` zeroes deleted content in place."""
    path = accounts.db_path()
    con = sqlite3.connect(str(path), timeout=30.0, isolation_level=None)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA busy_timeout=30000")
        con.execute("PRAGMA foreign_keys=ON")
        if secure:
            con.execute("PRAGMA secure_delete=ON")
        yield con
    finally:
        con.close()


def _try(con: sqlite3.Connection, sql: str, params: Iterable = ()) -> sqlite3.Cursor | None:
    """Run ``sql``; None when a table of an optional subsystem is not there."""
    try:
        return con.execute(sql, tuple(params))
    except sqlite3.OperationalError as e:
        if "no such table" in str(e) or "no such column" in str(e):
            return None
        raise


def _chunks(seq: list, n: int) -> Iterator[list]:
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _marks(n: int) -> str:
    return ",".join("?" * n)


def _unlink(path: Path) -> bool:
    try:
        path.unlink()
        return True
    except OSError:
        return False


def _media_names(*texts: str | None) -> set[str]:
    out: set[str] = set()
    for t in texts:
        if t and "/media/" in t:
            out.update(_NAME_RE.findall(t))
    return out


# --------------------------------------------------------------------------- #
# Preview tokens
# --------------------------------------------------------------------------- #

#: Per-process key: a token dies with the process, which is fine for a five-minute token.
_TOKEN_KEY = secrets.token_bytes(32)
_consumed: dict[str, float] = {}


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _mint(kind: str, profile_id: str, counts: Mapping[str, int], extra: Mapping[str, Any] | None = None) -> str:
    """A signed, expiring token that binds ``kind``, the profile, what the confirmation said
    would go (``counts``) and ``extra`` (the role being moved to)."""
    body = {"k": kind, "p": profile_id, "c": dict(counts), "x": dict(extra or {}),
            "e": int(time.time()) + TOKEN_TTL_SECONDS, "n": secrets.token_hex(8)}
    raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    return f"{_b64(raw)}.{_b64(hmac.new(_TOKEN_KEY, raw, hashlib.sha256).digest())}"


def _spend(kind: str, token: str | None, profile_id: str, counts: Mapping[str, int],
           extra: Mapping[str, Any] | None, code: str) -> None:
    """Accept ``token`` once, or raise a 409 ``code``. Every way of being wrong looks the same
    (malformed, forged, expired, another profile, another action, already used, or the counts
    changed since the preview): the person is asked to look at the numbers again."""
    stale = ProfileServiceError(code, "What would be deleted has changed. Look at the numbers again "
                                      "before confirming.", 409)
    if not token or token.count(".") != 1:
        raise stale
    raw_b64, sig_b64 = token.split(".")
    try:
        raw, sig = _unb64(raw_b64), _unb64(sig_b64)
        body = json.loads(raw)
    except (ValueError, TypeError):
        raise stale from None
    good = hmac.new(_TOKEN_KEY, raw, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, good) or not isinstance(body, dict):
        raise stale
    now = time.time()
    for k in [k for k, exp in _consumed.items() if exp < now]:
        _consumed.pop(k, None)
    nonce = str(body.get("n"))
    if (body.get("k") != kind or body.get("p") != profile_id or int(body.get("e", 0)) < now
            or nonce in _consumed or body.get("c") != dict(counts) or body.get("x") != dict(extra or {})):
        raise stale
    _consumed[nonce] = float(body["e"])


# --------------------------------------------------------------------------- #
# Finding a profile's content
# --------------------------------------------------------------------------- #


def _owner_clause(profile_id: str) -> tuple[str, list]:
    """SQL for "rows this profile owns": a NULL owner reads as the protected Admin's."""
    if profile_id == ownership.protected_admin_id():
        return "(owner_profile_id = ? OR owner_profile_id IS NULL)", [profile_id]
    return "owner_profile_id = ?", [profile_id]


def _select_threads(con: sqlite3.Connection, profile_id: str, bot_ids: list[str] | None = None) -> list[str]:
    """Fill the temp table ``_wipe_threads`` with the profile's thread ids (only those with
    ``bot_ids`` when given) and return them."""
    con.execute("CREATE TEMP TABLE IF NOT EXISTS _wipe_threads (id TEXT PRIMARY KEY)")
    con.execute("DELETE FROM _wipe_threads")
    where, params = _owner_clause(profile_id)
    if bot_ids is not None:
        if not bot_ids:
            return []
        where += f" AND bot_id IN ({_marks(len(bot_ids))})"
        params += list(bot_ids)
    con.execute(f"INSERT INTO _wipe_threads SELECT id FROM threads WHERE {where}", params)
    return [r[0] for r in con.execute("SELECT id FROM _wipe_threads")]


def _thread_media(con: sqlite3.Connection) -> set[str]:
    """Every ``/media/<name>`` the scoped threads' messages and image jobs show."""
    names: set[str] = set()
    for r in con.execute(
            "SELECT m.media_url, m.content, m.metadata FROM messages m "
            "JOIN _wipe_threads w ON w.id = m.thread_id "
            "WHERE m.media_url IS NOT NULL OR instr(m.content, '/media/') > 0 "
            "OR instr(m.metadata, '/media/') > 0"):
        names |= _media_names(r[0], r[1], r[2])
    cur = _try(con, "SELECT j.media_url FROM image_jobs j JOIN _wipe_threads w ON w.id = j.thread_id "
                    "WHERE j.media_url IS NOT NULL")
    for r in (cur or ()):
        names |= _media_names(r[0])
    return names


def _surviving_media(con: sqlite3.Connection, names: set[str]) -> set[str]:
    """Which of ``names`` a message or image job that is STILL there shows. One pass over the
    rows that mention a media file at all."""
    if not names:
        return set()
    found: set[str] = set()
    for r in con.execute(
            "SELECT media_url, content, metadata FROM messages "
            "WHERE media_url IS NOT NULL OR instr(content, '/media/') > 0 "
            "OR instr(metadata, '/media/') > 0"):
        found |= _media_names(r[0], r[1], r[2]) & names
        if found == names:
            return found
    cur = _try(con, "SELECT media_url FROM image_jobs WHERE media_url IS NOT NULL")
    for r in (cur or ()):
        found |= _media_names(r[0]) & names
    return found


def _ledger_path() -> Path:
    return Path(config.DATA_DIR) / "apps-data" / "imagestudio" / "images.jsonl"


def _ledger_owner_matches(row: dict, profile_id: str) -> bool:
    """A ledger row is a profile's when it says so; a row from before rows had owners is the
    protected Admin's."""
    owner = row.get("owner_profile_id") or row.get("owner")
    if owner:
        return owner == profile_id
    return profile_id == ownership.protected_admin_id()


def _read_ledger() -> list[tuple[str, dict | None]]:
    """(raw line, parsed row or None) for each non-blank line of the Image Studio ledger."""
    out: list[tuple[str, dict | None]] = []
    try:
        with _ledger_path().open(encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\n")
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    row = None
                out.append((line, row if isinstance(row, dict) else None))
    except OSError:
        pass
    return out


def _owned_ledger_media(profile_id: str) -> set[str]:
    names: set[str] = set()
    for _line, row in _read_ledger():
        if row and _ledger_owner_matches(row, profile_id):
            names |= _media_names(str(row.get("url") or ""))
    return names


def _ledger_media_of_others(profile_id: str) -> set[str]:
    names: set[str] = set()
    for _line, row in _read_ledger():
        if row and not _ledger_owner_matches(row, profile_id):
            names |= _media_names(str(row.get("url") or ""))
    return names


def _drop_owned_ledger(profile_id: str) -> int:
    """Rewrite the ledger without ``profile_id``'s rows (lines that do not parse are kept
    untouched). Returns how many rows went."""
    lines = _read_ledger()
    keep = [line for line, row in lines if not (row and _ledger_owner_matches(row, profile_id))]
    gone = len(lines) - len(keep)
    if gone:
        path = _ledger_path()
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".images.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write("".join(line + "\n" for line in keep))
            with contextlib.suppress(OSError):
                os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except BaseException:
            _unlink(Path(tmp))
            raise
    return gone


# --------------------------------------------------------------------------- #
# Counts
# --------------------------------------------------------------------------- #


def content_counts(profile_id: str) -> dict[str, int]:
    """``{"bots", "chats", "pictures", "voices"}`` for ``profile_id`` (counts only)."""
    _safe_pid(profile_id)
    with LOCK:
        bots = len(characters.personal_bots(profile_id))
        with _chats() as con:
            ids = _select_threads(con, profile_id)
            names = _thread_media(con) if ids else set()
        names |= _owned_ledger_media(profile_id)
        voices = 0
        try:
            voices = int(ext_profile_hooks.count_profile_data(profile_id).get("voices", 0) or 0)
        except Exception:
            log.warning("could not count cloned voices", exc_info=True)
        return ContentCounts(bots=bots, chats=len(ids), pictures=len(names), voices=voices).as_dict()


def open_image_jobs(profile_id: str) -> list[dict]:
    """The profile's image jobs that are still running on the rig. Read them BEFORE a wipe and
    withdraw them afterwards (the rows are cascaded away with their messages, so only the
    rig-side job is left to cancel)."""
    _safe_pid(profile_id)
    with LOCK, _chats() as con:
        _select_threads(con, profile_id)
        cur = _try(con, "SELECT j.* FROM image_jobs j JOIN _wipe_threads w ON w.id = j.thread_id "
                        "WHERE j.state IN ('queued','running')")
        return [dict(r) for r in (cur or ())]


# --------------------------------------------------------------------------- #
# The wipe
# --------------------------------------------------------------------------- #


def _delete_threads(con: sqlite3.Connection, ids: list[str]) -> None:
    for chunk in _chunks(ids, _DELETE_BATCH):
        con.execute("BEGIN IMMEDIATE")
        try:
            con.execute(f"DELETE FROM threads WHERE id IN ({_marks(len(chunk))})", chunk)
            con.execute("COMMIT")
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                con.execute("ROLLBACK")
            raise


def _delete_tasks(con: sqlite3.Connection, profile_id: str | None) -> int:
    """Agent tasks owned by ``profile_id`` or started from a scoped thread, with their events
    and approvals. Runs while ``_wipe_threads`` still lists the threads."""
    sql = "SELECT task_id FROM tasks WHERE origin_thread_id IN (SELECT id FROM _wipe_threads)"
    params: list = []
    if profile_id:
        sql += " OR owner = ?"
        params.append(profile_id)
    cur = _try(con, sql, params)
    ids = [r[0] for r in (cur or ())]
    for chunk in _chunks(ids, 200):
        con.execute("BEGIN IMMEDIATE")
        try:
            for table in ("task_events", "approvals", "tasks"):
                _try(con, f"DELETE FROM {table} WHERE task_id IN ({_marks(len(chunk))})", chunk)
            con.execute("COMMIT")
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                con.execute("ROLLBACK")
            raise
    return len(ids)


def _delete_chat_files(con: sqlite3.Connection, profile_id: str) -> int:
    """The profile's ``source='chat'`` attachments (rows and blobs). File Server drops
    (``source='fileserver'``) are kept: they are the profile's files, not its conversations."""
    where, params = _owner_clause(profile_id)
    rows = [(r["id"], r["stored_name"]) for r in
            con.execute(f"SELECT id, stored_name FROM files WHERE source = 'chat' AND {where}", params)]
    for chunk in _chunks(rows, 200):
        con.execute("BEGIN IMMEDIATE")
        try:
            con.execute(f"DELETE FROM files WHERE id IN ({_marks(len(chunk))})", [c[0] for c in chunk])
            con.execute("COMMIT")
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                con.execute("ROLLBACK")
            raise
    files_dir = Path(config.FILES_DIR)
    for _fid, stored in rows:
        if stored and Path(stored).name == stored:
            _unlink(files_dir / stored)
    return len(rows)


def _advisor_db_paths() -> list[Path]:
    """Every advisor ledger file to clean: ``<data>/advisor.db`` and the file
    ``advisor.ledger()`` opens (the data dir the advisor was bound with). In production the two
    are the same file; INT-1: reconstructing the path from ``config.DATA_DIR`` alone missed
    the ledger whenever they differ, so both are cleaned."""
    paths = [Path(config.DATA_DIR) / "advisor.db"]
    try:
        from . import advisor
        if advisor._hooks is not None:
            bound = Path(advisor.ledger().path)
            if bound.resolve() != paths[0].resolve():
                paths.append(bound)
    except Exception:  # an unbound advisor (CLI, tests): the data-dir file only
        pass
    return paths


def _delete_advisor_requests(thread_ids: list[str], bot_ids: list[str]) -> None:
    for path in _advisor_db_paths():
        _delete_advisor_requests_in(path, thread_ids, bot_ids)


def _delete_advisor_requests_in(path: Path, thread_ids: list[str], bot_ids: list[str]) -> None:
    if not path.is_file() or not (thread_ids or bot_ids):
        return
    con = sqlite3.connect(str(path), timeout=30.0, isolation_level=None)
    try:
        con.execute("PRAGMA busy_timeout=30000")
        con.execute("PRAGMA secure_delete=ON")
        for column, ids in (("thread_id", thread_ids), ("bot_id", bot_ids)):
            for chunk in _chunks(ids, 400):
                _try(con, f"DELETE FROM requests WHERE {column} IN ({_marks(len(chunk))})", chunk)
        with contextlib.suppress(sqlite3.Error):
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        con.close()


def _delete_voice_cursors(thread_ids: list[str]) -> None:
    """Voice chats keep a small resume cursor per thread (``<data>/ext/<id>/cursor.sqlite``,
    at most a few hundred rows, ten-minute life); drop the ones for deleted threads."""
    if not thread_ids:
        return
    wanted = set(thread_ids)
    for path in sorted((Path(config.DATA_DIR) / "ext").glob("*/cursor.sqlite")):
        try:
            con = sqlite3.connect(str(path), timeout=5.0, isolation_level=None)
        except sqlite3.Error:
            continue
        try:
            con.execute("PRAGMA busy_timeout=5000")
            con.execute("PRAGMA secure_delete=ON")
            rows = _try(con, "SELECT k FROM cursor")
            doomed = [r[0] for r in (rows or ()) if any(t in str(r[0]) for t in wanted)]
            for chunk in _chunks(doomed, 200):
                con.execute(f"DELETE FROM cursor WHERE k IN ({_marks(len(chunk))})", chunk)
            with contextlib.suppress(sqlite3.Error):
                con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            log.warning("could not clear voice cursors in %s", path.name, exc_info=True)
        finally:
            con.close()


def _delete_snapshots(con: sqlite3.Connection, candidates: set[str]) -> None:
    """Avatar snapshots are content-addressed and shared by every thread that started while
    that picture was current: delete one only when no remaining thread names it."""
    if not candidates:
        return
    live: set[str] = set()
    for chunk in _chunks(sorted(candidates), 400):
        live |= {r[0] for r in con.execute(
            f"SELECT DISTINCT avatar_snapshot FROM threads WHERE avatar_snapshot IN ({_marks(len(chunk))})", chunk)}
    folder = avatar_snapshots._dir()
    for sid in candidates - live:
        if _SNAP_RE.match(sid):
            for name in (sid, avatar_snapshots._full_name(sid), avatar_snapshots._thumb_name(sid)):
                _unlink(folder / name)


def _scrub(con: sqlite3.Connection) -> None:
    """Leave no readable trace in the database file: merge the search index (deleted entries
    live on in its segments until then), release freed pages and truncate the WAL."""
    for sql in ("INSERT INTO messages_fts(messages_fts) VALUES('optimize')",
                "PRAGMA incremental_vacuum", "PRAGMA wal_checkpoint(TRUNCATE)"):
        with contextlib.suppress(sqlite3.Error):
            con.execute(sql)


def _delete_conversation_data(profile_id: str, bot_ids: list[str] | None, *, whole_profile: bool,
                              extra_media: set[str] | None = None) -> tuple[int, int]:
    """Delete the threads in scope and everything hanging off them. Returns
    ``(threads deleted, pictures deleted)``. Caller holds LOCK."""
    media_deleted = 0
    with _chats(secure=True) as con:
        ids = _select_threads(con, profile_id, bot_ids)
        names = _thread_media(con) if ids else set()
        names |= extra_media or set()
        snaps: set[str] = set()
        for chunk in _chunks(ids, 400):
            snaps |= {r[0] for r in con.execute(
                f"SELECT DISTINCT avatar_snapshot FROM threads WHERE id IN ({_marks(len(chunk))}) "
                "AND avatar_snapshot IS NOT NULL", chunk)}
        _delete_tasks(con, profile_id if whole_profile else None)
        if whole_profile:
            _delete_chat_files(con, profile_id)
        _delete_threads(con, ids)
        # Files go after the rows: what is still shown anywhere stays.
        # A picture the Image Studio ledger still lists stays too (all rows when only one bot's
        # chats go; other profiles' rows when the whole profile does).
        ledger_keep = _ledger_media_of_others(profile_id)
        if not whole_profile:
            ledger_keep |= _owned_ledger_media(profile_id)
        keep = _surviving_media(con, names) | (ledger_keep & names)
        media_dir = Path(config.MEDIA_DIR)
        for name in names - keep:
            if _NAME_RE.fullmatch("/media/" + name) and _unlink(media_dir / name):
                media_deleted += 1
        _delete_snapshots(con, snaps)
        _scrub(con)
        con.execute("DROP TABLE IF EXISTS _wipe_threads")
    _delete_advisor_requests(ids, bot_ids or [])
    _delete_voice_cursors(ids)
    ownership.forget_all()
    return len(ids), media_deleted


def wipe_profile_content(profile_id: str) -> dict[str, int]:
    """Delete the profile's threads (messages, image_jobs, thread media and avatar snapshots
    cascade), personal bots (config entries, avatar files, voice-acks entries),
    ``source='chat'`` files, Image Studio ledger rows and ``prompts-<pid>.json``,
    advisor-ledger requests, agents_store tasks and voice cursors. Keeps
    ``source='fileserver'`` files and push subscriptions; a ``/media/`` file is deleted only
    when no surviving message references it. Does NOT purge cloned voices (Reset keeps
    them; callers that must, call ``ext_profile_hooks.purge_profile_data``).
    Returns what was deleted, in the :class:`ContentCounts` keys.

    Safe to run again after a failure: it only ever deletes, and every step looks at what is
    left. The profile row, its sign-in and its permissions are not touched."""
    _safe_pid(profile_id)
    with LOCK:
        bots = characters.personal_bots(profile_id)
        ledger_names = _owned_ledger_media(profile_id)
        chats, pictures = _delete_conversation_data(profile_id, None, whole_profile=True,
                                                    extra_media=ledger_names)
        # Ledger rows leave AFTER their pictures were judged against the remaining messages.
        _drop_owned_ledger(profile_id)
        prompts = Path(config.DATA_DIR) / "apps-data" / "imagestudio" / f"prompts-{profile_id}.json"
        _unlink(prompts)
        # Bots last: their chats are already gone, so nothing points at a vanished bot.
        removed = []
        for b in bots:
            try:
                config.remove_bot(b.id)
                removed.append(b)
            except ValueError:
                log.warning("kept the last bot on the roster; it is not removed", exc_info=True)
        characters.drop_bot_assets(removed)
        return ContentCounts(bots=len(removed), chats=chats, pictures=pictures, voices=0).as_dict()


# --------------------------------------------------------------------------- #
# Atlas
# --------------------------------------------------------------------------- #


def spawn_atlas(profile_id: str) -> str:
    """Copy the shipped Atlas template into ``profile_id`` as a personal bot with an opaque
    ``p<12hex>`` id (direct backend on the household default connection, no knowledge_dir,
    image_jobs off, tools on, the Atlas avatar copied). Returns the new bot id."""
    _safe_pid(profile_id)
    with LOCK:
        prof = _profile(profile_id)
        _require_owner_of_bots(prof)
        tmpl = firstrun.atlas_template()
        household = config.get_bot(firstrun.BOT_ID)
        connection, model = None, None
        try:
            cid = backends_policy.resolve_connection(prof)
        except backends_policy.BackendsError:
            cid = ""
        if cid:
            connection = cid
            # The household Atlas already runs a model on this connection: use the same one.
            if household is not None and characters.connection_of(household) == cid:
                model = characters.model_of(household) or None
        bot = characters.create_personal(
            prof, name=tmpl.name, emoji=tmpl.emoji, tools=True, pack=firstrun.ATLAS_PACK,
            connection=connection, model=model, soft_connection=True, fallback_ok=True)
        face = firstrun.copy_atlas_avatar(bot.id)
        if face:
            config.save_bot_avatar(bot.id, face)
        return bot.id


def on_profile_created(profile: Any, *, first_admin: bool = False) -> None:
    """Called once after a profile is created: Admin and User profiles get their own Atlas
    (:func:`spawn_atlas`), except the first Admin of a fresh install (``first_admin``), who
    sees the household Atlas. Basic profiles get nothing. A failure is logged, never raised:
    the profile exists and Reset can make the Atlas later."""
    if first_admin or getattr(profile, "role", None) is Role.BASIC:
        return
    try:
        spawn_atlas(profile.id)
    except Exception:
        log.warning("could not make the starter bot for a new profile", exc_info=True)


def household_bot_usage(bot_id: str) -> dict[str, int]:
    """``{"chats", "profiles"}``: how many chats use the household bot ``bot_id`` and how many
    profiles those chats belong to (numbers only; nobody's titles or content)."""
    with _chats() as con:
        row = con.execute(
            "SELECT COUNT(*), COUNT(DISTINCT COALESCE(owner_profile_id, '')) FROM threads WHERE bot_id = ?",
            (bot_id,)).fetchone()
    return {"chats": int(row[0]), "profiles": int(row[1])}


def has_atlas(profile_id: str) -> bool:
    return any(b.pack == firstrun.ATLAS_PACK for b in characters.personal_bots(profile_id))


def connect_unconnected_atlases(connection_id: str, model: str | None = None) -> int:
    """Point every personal Atlas that has no connection yet (made before the install had one)
    at ``connection_id`` - for the profiles whose backends allow it. Returns how many."""
    n = 0
    with LOCK:
        for b in config.load_bots():
            if b.scope != "personal" or b.pack != firstrun.ATLAS_PACK or characters.connection_of(b):
                continue
            owner = accounts.get_profile(b.owner_profile)
            if owner is None or not backends_policy.permits(owner, connection_id):
                continue
            try:
                config.update_bot(b.id, lambda cur: characters.with_connection(cur, connection_id, model))
                n += 1
            except characters.CharacterError:
                log.info("left a personal Atlas unconnected: the connection offers no model", exc_info=True)
    return n


# --------------------------------------------------------------------------- #
# Reset to default
# --------------------------------------------------------------------------- #


def reset_preview(profile_id: str) -> dict[str, Any]:
    """``{"counts": {...}, "token": str, "expires_in": TOKEN_TTL_SECONDS}``. The token binds
    the profile id and the counts. ``outcome`` says what the profile is left with: a fresh
    ``atlas`` (Admin, User) or its assigned ``household`` bots (Basic); cloned voices are
    always kept (``keeps_voices``)."""
    _safe_pid(profile_id)
    with LOCK:
        prof = _profile(profile_id)
        counts = content_counts(profile_id)
        return {"counts": counts, "token": _mint("reset", profile_id, counts),
                "expires_in": TOKEN_TTL_SECONDS, "keeps_voices": True,
                "outcome": "atlas" if _can_own_bots(prof) else "household"}


def reset_profile(profile_id: str, token: str) -> dict[str, int]:
    """Reset to default: check ``token`` (stale or mismatched counts ->
    ``ProfileServiceError("reset_stale", ..., 409)``), wipe, then Admin/User -> a fresh
    Atlas, Basic -> its assigned household list stays. Returns the deleted counts. Cloned
    voices are kept."""
    _safe_pid(profile_id)
    with LOCK:
        prof = _profile(profile_id)
        _spend("reset", token, profile_id, content_counts(profile_id), None, "reset_stale")
        deleted = wipe_profile_content(profile_id)
        if _can_own_bots(prof):
            spawn_atlas(profile_id)
        return deleted


# --------------------------------------------------------------------------- #
# Role changes
# --------------------------------------------------------------------------- #


def _loses_private_content(old: Role, new: Role) -> bool:
    """Moving to Basic from a role that owns bots deletes them and every chat."""
    return new is Role.BASIC and old is not Role.BASIC


def change_role_preview(profile_id: str, new_role: str) -> dict[str, Any]:
    """For User -> Basic: ``{"counts": {... incl. voices}, "token": str, "expires_in": ...}``;
    for any other change ``{"counts": {}, "token": "", ...}`` (nothing is deleted)."""
    _safe_pid(profile_id)
    with LOCK:
        prof = _profile(profile_id)
        new = _normalise(new_role)
        if prof.protected and new is not Role.ADMIN:
            raise ProfileServiceError("owner_immutable", "The first Admin cannot be demoted.", 409)
        if not _loses_private_content(prof.role, new):
            return {"counts": {}, "token": "", "expires_in": TOKEN_TTL_SECONDS, "deletes": False}
        counts = content_counts(profile_id)
        return {"counts": counts, "token": _mint("role", profile_id, counts, {"to": new.value}),
                "expires_in": TOKEN_TTL_SECONDS, "deletes": True}


def _normalise(role: str) -> Role:
    try:
        return normalise_role(role)
    except ValueError:
        raise ProfileServiceError("profile_role_invalid", f"Unknown role {role!r}.", 422) from None


def basic_default_household() -> list[str]:
    """The household bots a new Basic profile gets when nobody picked: every household bot a
    Basic profile can use (direct ones; an agent bot also needs a permission an Admin must
    grant on purpose), frozen as an explicit list so bots added later are not handed out."""
    return [b.id for b in config.load_bots()
            if b.scope == "household" and characters.backend_kind(b) == "direct"]


def _household_choice(profile: accounts.Profile,
                      wanted: list[str] | str | None) -> list[str]:
    """The explicit household list a new Basic profile gets: the Admin's pick, else what it
    already had, else (it could use "all") a frozen snapshot of the bots a Basic profile can
    actually use - direct ones, so later additions are not handed out silently."""
    household = [b for b in config.load_bots() if b.scope == "household"]
    known = {b.id for b in household}
    if wanted is None or wanted == "all":
        if wanted is None and isinstance(profile.household_bots, list):
            wanted = list(profile.household_bots)
        else:
            return basic_default_household()
    if not isinstance(wanted, (list, tuple)) or not all(isinstance(x, str) for x in wanted):
        raise ProfileServiceError("household_invalid", "household_bots must be a list of bot ids.", 422)
    bad = [x for x in wanted if x not in known]
    if bad:
        raise ProfileServiceError("household_invalid", f"Unknown household bot {bad[0]!r}.", 422)
    return list(dict.fromkeys(wanted))


def _trim_overrides(profile: accounts.Profile, role: Role) -> tuple[list[str], list[str]]:
    """The profile's grants/denies cut to what ``role`` can have: a grant must be in the
    grantable column, a deny must name something the role has or can be given."""
    grantable = {p.value for p in ROLE_GRANTABLE[role]}
    known = grantable | {p.value for p in ROLE_DEFAULTS[role]}
    return ([g for g in profile.grants if g in grantable], [d for d in profile.denies if d in known])


def change_role(profile_id: str, new_role: str, *, confirm_token: str | None = None,
                household_bots: list[str] | str | None = None) -> Any:
    """Change a profile's role. User -> Basic needs ``confirm_token`` from
    :func:`change_role_preview`, wipes the content, purges cloned voices, resets the
    household list to ``household_bots`` (Admin-chosen) and trims overrides to the Basic
    table. Basic -> User changes the role only (chats kept). Returns the updated
    ``accounts.Profile``. The protected Admin's role never changes."""
    _safe_pid(profile_id)
    with LOCK:
        prof = _profile(profile_id)
        new = _normalise(new_role)
        if prof.protected:
            if new is not Role.ADMIN:
                raise ProfileServiceError("owner_immutable", "The first Admin cannot be demoted.", 409)
            return prof
        if new is prof.role:
            return prof
        if not _loses_private_content(prof.role, new):
            updated = accounts.update_profile(profile_id, role=new)
            _revoke(profile_id)
            return updated
        # Moving to Basic. Resolve everything that can fail BEFORE deleting anything.
        chosen = _household_choice(prof, household_bots)
        grants, denies = _trim_overrides(prof, Role.BASIC)
        _spend("role", confirm_token, profile_id, content_counts(profile_id), {"to": new.value},
               "role_change_stale")
        wipe_profile_content(profile_id)
        ext_profile_hooks.purge_profile_data(profile_id)
        updated = accounts.update_profile(profile_id, role=Role.BASIC, household_bots=chosen,
                                          grants=grants, denies=denies)
        _revoke(profile_id)
        return updated


def _revoke(profile_id: str) -> None:
    """Sign the profile out everywhere (a role change bumps its epoch; this drops the live
    sessions from memory, as the People route does)."""
    from . import auth
    with contextlib.suppress(Exception):
        auth.revoke_profile(profile_id)


# --------------------------------------------------------------------------- #
# What an Admin lets a profile use
# --------------------------------------------------------------------------- #


def set_household_bots(profile_id: str, value: list[str] | str) -> accounts.Profile:
    """The household bots ``profile_id`` may use: ``"all"`` (User) or an explicit list. A Basic
    profile always has a list (it gets only the bots an Admin adds); an Admin uses every
    household bot, so there is nothing to set."""
    _safe_pid(profile_id)
    with LOCK:
        prof = _profile(profile_id)
        if prof.role is Role.ADMIN:
            raise ProfileServiceError("household_invalid", "An Admin uses every household bot.", 422)
        if value == "all":
            if prof.role is Role.BASIC:
                raise ProfileServiceError(
                    "household_invalid", "A Basic profile gets an explicit list of bots, not all of them.", 422)
            chosen: list[str] | str = "all"
        else:
            chosen = _household_choice(prof, value)
        return accounts.update_profile(profile_id, household_bots=chosen)


def set_backends(profile_id: str, allowed: list[str] | None) -> accounts.Profile:
    """The connections ``profile_id``'s own bots may run on (None = every household
    connection). Household bots are not affected."""
    _safe_pid(profile_id)
    with LOCK:
        prof = _profile(profile_id)
        try:
            value = backends_policy.validate_backends(None if allowed is None else {"allowed": allowed})
        except backends_policy.BackendsError as e:
            raise ProfileServiceError(e.code, str(e), 422) from e
        return accounts.update_profile(prof.id, backends=value)


# --------------------------------------------------------------------------- #
# Deleting a profile
# --------------------------------------------------------------------------- #


def delete_preview(profile_id: str) -> dict[str, Any]:
    """The counts a deletion would remove, with a token that binds them."""
    _safe_pid(profile_id)
    with LOCK:
        prof = _profile(profile_id)
        if prof.protected:
            raise ProfileServiceError("owner_immutable", "The first Admin cannot be deleted.", 409)
        counts = content_counts(profile_id)
        return {"counts": counts, "token": _mint("delete", profile_id, counts),
                "expires_in": TOKEN_TTL_SECONDS}


def purge_deleted_profile(profile_id: str) -> dict[str, int]:
    """Everything that belongs to a profile that is going away, except its row: the content
    (:func:`wipe_profile_content`), its cloned voices (the add-ons' purge hook), its File
    Server drops and its push subscriptions. The People route calls this, then
    ``accounts.delete_profile``. Returns the content counts that were deleted."""
    _safe_pid(profile_id)
    with LOCK:
        deleted = wipe_profile_content(profile_id)
        try:
            deleted["voices"] = int(ext_profile_hooks.purge_profile_data(profile_id) or 0)
        except Exception:
            log.warning("a voice add-on could not purge a deleted profile's clones", exc_info=True)
        with _chats(secure=True) as con:
            where, params = _owner_clause(profile_id)
            rows = [(r["id"], r["stored_name"]) for r in
                    con.execute(f"SELECT id, stored_name FROM files WHERE {where}", params)]
            for chunk in _chunks(rows, 200):
                con.execute(f"DELETE FROM files WHERE id IN ({_marks(len(chunk))})", [c[0] for c in chunk])
            for _fid, stored in rows:
                if stored and Path(stored).name == stored:
                    _unlink(Path(config.FILES_DIR) / stored)
            _scrub(con)
        _drop_push_subscriptions(profile_id)
        return deleted


def _drop_push_subscriptions(profile_id: str) -> None:
    from . import push
    try:
        for key, sub in list(push._load_subs().items()):
            if sub.get("profile_id") == profile_id:
                push.unsubscribe(key)
    except Exception:
        log.warning("could not drop a deleted profile's push subscriptions", exc_info=True)


def delete_profile(profile_id: str, confirm_token: str) -> dict[str, int]:
    """Delete a profile for good: confirm with :func:`delete_preview`'s token, purge its
    content, voices, files and push subscriptions, sign it out, move the lock target off it
    (back to the profile list) and remove the row. The protected Admin cannot be deleted."""
    _safe_pid(profile_id)
    with LOCK:
        prof = _profile(profile_id)
        if prof.protected:
            raise ProfileServiceError("owner_immutable", "The first Admin cannot be deleted.", 409)
        _spend("delete", confirm_token, profile_id, content_counts(profile_id), None, "delete_stale")
        _revoke(profile_id)
        deleted = purge_deleted_profile(profile_id)
        if lock_target_profile_id() == profile_id:
            set_lock_target(None)
        accounts.delete_profile(profile_id)
        return deleted


# --------------------------------------------------------------------------- #
# The lock target (security.yaml)
# --------------------------------------------------------------------------- #


def lock_target_profile_id() -> str | None:
    """The default profile when the lock lands on it, else None."""
    from . import auth
    cfg = auth.load()
    if cfg.lock_target == "default_profile" and cfg.default_profile:
        return cfg.default_profile
    return None


def set_lock_target(profile_id: str | None) -> None:
    """Make ``profile_id`` the default profile the lock lands on, or None for the profile
    list. Goes through ``auth.set_lock_target``, which validates the target (exists, enabled,
    not an Admin) and strips stored fenced grants; ``policy`` enforces the fence either way."""
    from . import auth
    if profile_id:
        auth.set_lock_target("default_profile", profile_id)
    else:
        auth.set_lock_target("profile_list", None)


def ensure_default_profile(*, name: str = firstrun.DEFAULT_PROFILE_NAME) -> accounts.Profile:
    """The fresh-install default profile: a User named "DisPatch" with no PIN and its own Atlas,
    made the lock target. Idempotent: an existing profile of that name is reused untouched
    (apart from a missing Atlas), and the lock target is only set when the profile is created
    here, so an Admin's later choice is never overridden."""
    with LOCK:
        prof = accounts.get_by_name(name)
        created = prof is None
        if prof is None:
            prof = accounts.create_profile(name, Role.USER)
        if created and lock_target_profile_id() is None:
            set_lock_target(prof.id)
        if created or (prof.role is Role.USER and not has_atlas(prof.id)):
            on_profile_created(prof)
        return prof


# --------------------------------------------------------------------------- #
# Personal bots (the owner's surface)
# --------------------------------------------------------------------------- #


def _may_choose_backend(profile: accounts.Profile) -> bool:
    from . import policy
    return Permission.CHOOSE_BACKEND.value in policy.effective_permissions(profile)


def list_personal(profile_id: str) -> list[dict]:
    """The profile's own bots as the My bots list shows them, each with its chat count."""
    _safe_pid(profile_id)
    with LOCK:
        bots = characters.personal_bots(profile_id)
        counts: dict[str, int] = {}
        if bots:
            with _chats() as con:
                where, params = _owner_clause(profile_id)
                for r in con.execute(
                        f"SELECT bot_id, COUNT(*) FROM threads WHERE {where} GROUP BY bot_id", params):
                    counts[r[0]] = r[1]
        return [{**characters.character_out(b), "chats": counts.get(b.id, 0)} for b in bots]


def create_personal(profile_id: str, fields: Mapping[str, Any]) -> Any:
    """Create a personal bot for ``profile_id`` from the editor's fields (name, emoji,
    personality, voice, backend). Direct backend only; ``voice`` checked with
    ``ext_profile_hooks.voice_allowed(voice, profile_id)``; backend within
    ``backends_policy.allowed_backends(profile)``. Returns the new ``config.Bot``."""
    _safe_pid(profile_id)
    with LOCK:
        prof = _profile(profile_id)
        _require_owner_of_bots(prof)
        allowed = {"name", "emoji", "description", "color", "quick_actions", "tools", "persona",
                   "personality", "voice", "connection", "model"}
        extra = sorted(set(fields) - allowed)
        if extra:
            raise ProfileServiceError("character_invalid", f"Cannot set {extra[0]!r} on your own bot.", 422)
        try:
            return characters.create_personal(prof, may_choose_backend=_may_choose_backend(prof), **dict(fields))
        except characters.CharacterError as e:
            raise ProfileServiceError(e.code, str(e), e.status) from e


def update_personal(profile_id: str, bot_id: str, changes: Mapping[str, Any]) -> Any:
    _safe_pid(profile_id)
    with LOCK:
        prof = _profile(profile_id)
        _require_owner_of_bots(prof)
        try:
            return characters.patch_personal(bot_id, prof, dict(changes),
                                             may_choose_backend=_may_choose_backend(prof))
        except characters.CharacterError as e:
            raise ProfileServiceError(e.code, str(e), e.status) from e


def delete_personal(profile_id: str, bot_id: str) -> dict[str, int]:
    """Delete one of the profile's own bots and the chats with it (the editor's confirmation
    says "also deletes your N chats with <name>"). Returns ``{"chats", "pictures"}``."""
    _safe_pid(profile_id)
    with LOCK:
        try:
            bot = characters.get_personal(bot_id, profile_id)
        except characters.CharacterError as e:
            raise ProfileServiceError(e.code, str(e), e.status) from e
        chats, pictures = _delete_conversation_data(profile_id, [bot.id], whole_profile=False)
        try:
            characters.remove_personal(bot.id, profile_id)
        except characters.CharacterError as e:
            raise ProfileServiceError(e.code, str(e), e.status) from e
        return {"chats": chats, "pictures": pictures}


def set_personal_avatar(profile_id: str, bot_id: str, raw: bytes) -> Any:
    """Replace one of the profile's own bots' picture: a 512 px square face, a 128 px thumb and
    the full image, written as ``<id>-face.png`` / ``-thumb.png`` / ``-full.png``."""
    _safe_pid(profile_id)
    with LOCK:
        try:
            characters.get_personal(bot_id, profile_id)
        except characters.CharacterError as e:
            raise ProfileServiceError(e.code, str(e), e.status) from e
        if not raw:
            raise ProfileServiceError("avatar_invalid", "No picture was sent.", 422)
        if len(raw) > MAX_AVATAR_BYTES:
            raise ProfileServiceError("avatar_too_large", "The picture is too large (25 MB at most).", 413)
        try:
            from PIL import Image, ImageOps
            with Image.open(io.BytesIO(raw)) as im:
                if im.width * im.height > MAX_AVATAR_PIXELS:
                    raise ProfileServiceError("avatar_too_large", "The picture has too many pixels.", 413)
                im = ImageOps.exif_transpose(im)
                im = im.convert("RGBA" if "A" in im.getbands() else "RGB")
                side = min(im.size)
                left, top = (im.width - side) // 2, (im.height - side) // 2
                square = im.crop((left, top, left + side, top + side))
                face = square.resize((512, 512), Image.LANCZOS)
                thumb = square.resize((128, 128), Image.LANCZOS)
                full = im.copy()
        except ProfileServiceError:
            raise
        except Exception:
            raise ProfileServiceError("avatar_invalid", "That file is not a picture DisPatch can read.", 422) from None
        folder = Path(config.AVATAR_DIR)
        folder.mkdir(parents=True, exist_ok=True)
        for role, img in (("full", full), ("face", face), ("thumb", thumb)):
            name = f"{bot_id}-{role}.png"
            tmp = folder / f".{name}.partial"
            img.save(tmp, "PNG")
            tmp.replace(folder / name)
        for role in characters.AVATAR_ROLES:
            for ext in characters.AVATAR_EXTS:
                if ext != ".png":
                    _unlink(folder / f"{bot_id}-{role}{ext}")
        saved = config.save_bot_avatar(bot_id, f"{bot_id}-face.png")
        if saved is None:
            raise ProfileServiceError("bot_not_found", "Unknown bot", 404)
        return saved


# --------------------------------------------------------------------------- #
# Copies
# --------------------------------------------------------------------------- #


def _copy_source(bot_id: str) -> config.Bot:
    """The household bot to copy. Agent bots (and apps and legacy API bots) cannot be copied:
    an agent can run commands on this computer, and an ``api`` block carries a key."""
    src = config.get_bot(bot_id)
    if src is None or src.scope != "household":
        raise ProfileServiceError("bot_not_found", "Unknown bot", 404)
    if characters.backend_kind(src) != "direct":
        raise ProfileServiceError("not_copyable", "This bot runs on an agent and cannot be copied.", 422)
    return src


def _copy_avatar_files(src: config.Bot, new_id: str) -> str:
    """Copy the source's picture (face, and full/thumb beside it when they exist) under the
    new id; returns the new face file name ("" when the source has none)."""
    if not src.avatar:
        return ""
    folder = Path(config.AVATAR_DIR)
    face = folder / src.avatar
    if not face.is_file():
        return ""
    ext = face.suffix.lower()
    stem = face.name[:-len(face.suffix)]
    out_face = f"{new_id}-face{ext}"
    pairs = [(face, out_face)]
    if stem.endswith("-face"):
        for role in ("full", "thumb"):
            sib = folder / f"{stem[:-5]}-{role}{ext}"
            if sib.is_file():
                pairs.append((sib, f"{new_id}-{role}{ext}"))
    try:
        for source, name in pairs:
            tmp = folder / f".{name}.partial"
            tmp.write_bytes(source.read_bytes())
            tmp.replace(folder / name)
    except OSError:
        log.warning("could not copy a bot's picture", exc_info=True)
        return ""
    return out_face


def _copy_into(profile: accounts.Profile, src: config.Bot) -> tuple[config.Bot, bool]:
    """Make the personal copy of household bot ``src`` for ``profile``. Copies the setup only
    (name, emoji, description, colour, picture, personality, voice, backend); never chats,
    knowledge folder, lookups configuration, agents, image jobs, reactions or pools."""
    from . import personas
    text, custom = personas.current(src)
    voice = (src.voice or "").strip()
    voice_ok = True
    if voice:
        # The copy keeps the source's voice only if this profile may use it (a built-in, the
        # stock voice, a household clone or its own clone) and the voice add-on still knows it.
        try:
            characters.check_extension_field("voice", "", voice)
            voice_ok = bool(ext_profile_hooks.voice_allowed(voice, profile.id))
        except characters.CharacterError:
            voice_ok = False
    new = characters.create_personal(
        profile, name=src.name, emoji=src.emoji, description=src.description, color=src.color,
        persona=text if custom else None, voice=voice if voice_ok else "",
        connection=characters.connection_of(src) or None, model=characters.model_of(src) or None,
        tools=True, quick_actions=src.quick_actions, cloned_from=src.id,
        may_choose_backend=True, fallback_ok=True, soft_connection=True)
    face = _copy_avatar_files(src, new.id)
    if face:
        saved = config.save_bot_avatar(new.id, face)
        new = saved or new
    return new, bool(voice) and not voice_ok


def clone_to_own(bot_id: str, principal: Any) -> dict[str, Any]:
    """A User copies a household bot into their own profile. Setup fields only (name, emoji,
    picture, personality, voice, backend); agent bots -> ``ProfileServiceError(
    "not_copyable", ..., 422)``. Returns ``{"bot": <character row>, "voice_cleared": bool}``."""
    pid = getattr(principal, "profile_id", None)
    if not pid:
        raise ProfileServiceError("permission_denied", "Sign in to copy a bot.", 403)
    with LOCK:
        prof = _profile(pid)
        _require_owner_of_bots(prof)
        src = _copy_source(bot_id)
        if not characters.may_use(src, principal):
            raise ProfileServiceError("bot_not_found", "Unknown bot", 404)
        try:
            new, cleared = _copy_into(prof, src)
        except characters.CharacterError as e:
            raise ProfileServiceError(e.code, str(e), e.status) from e
        return {"bot": characters.character_out(new), "voice_cleared": cleared}


def clone_to_profile(bot_id: str, profile_id: str, *, actor: Any) -> dict[str, Any]:
    """An Admin copies a household bot into ``profile_id`` (which then owns it; the Admin
    cannot see or edit the copy). Same field allowlist and voice rule as
    :func:`clone_to_own`. Returns ``{"bot_id": str, "voice_cleared": bool}`` (no row: the
    Admin may not read it)."""
    if not getattr(actor, "is_admin", False):
        raise ProfileServiceError("permission_denied", "Only an Admin copies a bot into another profile.", 403)
    _safe_pid(profile_id)
    with LOCK:
        prof = _profile(profile_id)
        _require_owner_of_bots(prof)
        src = _copy_source(bot_id)
        try:
            new, cleared = _copy_into(prof, src)
        except characters.CharacterError as e:
            raise ProfileServiceError(e.code, str(e), e.status) from e
        return {"bot_id": new.id, "voice_cleared": cleared}


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #


def export_chunks(profile_id: str) -> Iterator[str]:
    """The profile's own data as JSON, in pieces (``/api/me/export``): its chats with their
    messages, and the setup of its own bots (never keys). Pictures are named by URL, not
    embedded. The caller streams it; nothing here holds the whole thing in memory."""
    _safe_pid(profile_id)
    prof = _profile(profile_id)
    head = {"exported_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "profile": {"id": prof.id, "name": prof.name},
            "bots": [{k: b.get(k) for k in ("id", "name", "emoji", "description", "voice", "personality")}
                     for b in list_personal(profile_id)]}
    yield json.dumps(head, ensure_ascii=False)[:-1] + ', "threads": ['
    first = True
    with _chats() as con:
        where, params = _owner_clause(profile_id)
        threads = con.execute(
            f"SELECT id, bot_id, title, created_at, updated_at FROM threads WHERE {where} "
            "ORDER BY updated_at DESC", params).fetchall()
        for t in threads:
            msgs = [{"role": m["role"], "content": m["content"], "media_url": m["media_url"],
                     "created_at": m["created_at"]} for m in con.execute(
                "SELECT role, content, media_url, created_at FROM messages WHERE thread_id = ? "
                "ORDER BY created_at, rowid", (t["id"],))]
            item = {"id": t["id"], "bot_id": t["bot_id"], "title": t["title"],
                    "created_at": t["created_at"], "updated_at": t["updated_at"], "messages": msgs}
            yield ("" if first else ",") + json.dumps(item, ensure_ascii=False)
            first = False
    yield "]}"
