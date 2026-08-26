"""Per-thread avatar snapshots — what the bot looked like when a chat started.

Avatars can rotate — a bot's face may be swapped daily from a pool — so a
thread list rendered against the CURRENT avatar shows the same picture on every
row regardless of when the conversation happened. Snapshotting restores the
sense of time: a chat from last Tuesday keeps last Tuesday's face.

CONTENT-ADDRESSED, NOT COPY-PER-THREAD. The obvious implementation writes
`thread_<id>_avatar.png` for every thread, which duplicates one image per
thread: with daily rotation a few months of chatting produces several threads
per distinct face, so the same bytes are written over and over and the store
grows without bound as threads accumulate. Hashing the bytes instead means one
file per DISTINCT image, shared by every thread that started while it was
current — roughly one file per rotation rather than one per thread, and the
ratio only improves the more you chat.

It also keeps the app independent of HOW an avatar changes. Nothing here knows
about rotation scripts, pools or schedules — it hashes whatever the bot's
picture is at the moment a thread is created. Any bot, any install.
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from . import config

log = logging.getLogger("local-chat.avatar_snapshots")

# Same allowlist the media routes use. A snapshot is served to browsers, so the
# extension has to stay in a set we are willing to hand back.
_ALLOWED_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"}

# Enough hex to make a collision irrelevant while keeping filenames readable.
_HASH_CHARS = 16

# A snapshot is a decoration; a hostile or broken avatar file must never be able
# to fill the disk through the thread-creation path. The face is small (a 512px
# crop); the full is the original, so it gets a larger but still bounded cap —
# unbounded, a 64MP upload re-encoded to PNG could land ~30MB in the store twice.
MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024
MAX_FULL_BYTES = 25 * 1024 * 1024


def _dir() -> Path:
    return config.DATA_DIR / "avatar-snapshots"


def snapshot_id(bot) -> str | None:
    """Capture the bot's current avatar, returning the id to store on a thread.

    Returns None whenever there is nothing worth capturing — no avatar, an
    unreadable file, an unexpected extension, an implausibly large image. A
    thread with no snapshot simply falls back to the live avatar, which is the
    behaviour every pre-existing thread already has, so failing here costs a
    decoration and never a thread.
    """
    name = getattr(bot, "avatar", "") if bot else ""
    if not name:
        return None
    src = config.AVATAR_DIR / name
    suffix = src.suffix.lower()
    if suffix not in _ALLOWED_SUFFIXES:
        return None
    try:
        if src.stat().st_size > MAX_SNAPSHOT_BYTES:
            log.warning("avatar too large to snapshot: %s", name)
            return None
        data = src.read_bytes()
    except OSError:
        return None
    if not data:
        return None

    digest = hashlib.sha256(data).hexdigest()[:_HASH_CHARS]
    sid = f"{digest}{suffix}"
    dest = _dir() / sid
    if not dest.exists():
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            # Write beside the target and rename: a reader must never see a
            # half-written PNG, and two threads created in the same second must
            # not interleave into one file.
            _atomic_write(dest, data)
        except OSError:
            log.warning("could not write avatar snapshot for %s", name, exc_info=True)
            return None

    # AN AVATAR IS A PAIR. Uploads save a full-resolution original next to the
    # square face crop (<stem>-full.<ext> beside <stem>-face.<ext>), the daily
    # rotation moves BOTH, and the lightbox serves the full one. Capturing only
    # the face made a restored avatar show the correct thumbnail and somebody
    # ELSE'S full-resolution image when you clicked it — the previous avatar's
    # -full file was simply left in place. Snapshot the sibling too.
    #
    # This runs even when the face was ALREADY captured: an early return there
    # meant a snapshot first taken while the -full sibling was missing could
    # never acquire one, however many times the pair was later seen intact. The
    # pair now completes itself on the next capture of the same image.
    full_dest = _dir() / _full_name(sid)
    if not full_dest.exists():
        full_src = _full_sibling(src)
        if full_src is not None:
            try:
                if full_src.stat().st_size > MAX_FULL_BYTES:
                    log.warning("full-res avatar too large to snapshot: %s", full_src.name)
                    return sid       # keep the face; lightbox falls back
                _atomic_write(full_dest, full_src.read_bytes())
            except OSError:
                # A missing full-res is survivable — path_for_full returns None
                # and callers fall back to the face. A WRONG one is not.
                log.warning("could not snapshot the full-res avatar for %s", name)
    return sid


def snapshot_pair(face_data: bytes, full_data: bytes | None,
                  suffix: str = ".png") -> str | None:
    """Capture an EXPLICIT face/full pair, returning the id to pin on a thread.

    This is how a single thread gets its own picture (distinct from the bot's
    current avatar): the caller supplies both halves and the pair lands in the
    same content-addressed store the thread-creation capture uses — a picture
    pinned to one thread and an identical daily capture share bytes.

    Keyed by hash(face + full), NOT the face alone: an explicit re-pin can send
    an identical face crop with a DIFFERENT full-resolution image (two threads,
    same headshot, different scene behind it). Keying on the face only made the
    second pin resolve to the FIRST full — the little/big mismatch this whole
    system exists to prevent, reappearing by construction. Hashing both halves
    gives distinct pairs distinct ids while still deduplicating identical pairs.
    """
    sfx = suffix.lower()
    if sfx not in _ALLOWED_SUFFIXES or not face_data:
        return None
    if len(face_data) > MAX_SNAPSHOT_BYTES or (full_data and len(full_data) > MAX_FULL_BYTES):
        return None
    h = hashlib.sha256(face_data)
    h.update(b"\x00")                    # domain separator: face || full
    if full_data:
        h.update(full_data)
    digest = h.hexdigest()[:_HASH_CHARS]
    sid = f"{digest}{sfx}"
    try:
        d = _dir()
        d.mkdir(parents=True, exist_ok=True)
        dest = d / sid
        if not dest.exists():
            _atomic_write(dest, face_data)
        full_dest = d / _full_name(sid)
        if full_data and not full_dest.exists():
            _atomic_write(full_dest, full_data)
    except OSError:
        log.warning("could not write explicit avatar snapshot", exc_info=True)
        return None
    return sid


def _atomic_write(dest: Path, data: bytes) -> None:
    tmp = dest.with_name(f".{dest.name}.partial")
    tmp.write_bytes(data)
    tmp.replace(dest)


def _full_name(sid: str) -> str:
    """`<hash>.png` -> `<hash>-full.png`. Derived, so one column still keys both."""
    p = Path(sid)
    return f"{p.stem}-full{p.suffix}"


def _full_sibling(face: Path) -> Path | None:
    """The full-resolution original beside a face crop, if there is one.

    Upload writes `<id>-full.png` next to `<id>-face.png`; the rotation copies a
    pool pair into `main-face.png` / `main-full.png`. Both shapes end in
    `-face`, so one rule covers them.
    """
    stem = face.stem
    if stem.endswith("-face"):
        stem = stem[: -len("-face")]
    # The face's own suffix wins outright — a pair written together shares one.
    same = face.with_name(f"{stem}-full{face.suffix}")
    if same.is_file():
        return same
    # Cross-extension leftovers (an old .png beside a new .jpg pair) are stale
    # by definition; if several exist, the newest write is the least wrong. The
    # upload and restore paths now delete these, so this is a transition rule,
    # not a load-bearing one.
    others = [face.with_name(f"{stem}-full{ext}")
              for ext in (".png", ".jpg", ".jpeg", ".webp") if ext != face.suffix]
    others = [c for c in others if c.is_file()]
    if not others:
        return None
    return max(others, key=lambda c: c.stat().st_mtime)


def path_for_full(sid: str) -> Path | None:
    """The full-resolution half of a snapshot, or None when it has none."""
    if not sid:
        return None
    face = path_for(sid)
    if face is None:
        return None                     # validates sid before we build from it
    cand = _dir() / _full_name(Path(sid).name)
    return cand if cand.is_file() else None


def path_for(sid: str) -> Path | None:
    """Resolve a snapshot id to a file, or None if it is not a real one.

    The id reaches this from a URL, so it is untrusted: anything with a path
    separator, a parent reference, or an unexpected extension is refused before
    it touches the filesystem, and the resolved path is re-checked against the
    snapshot directory afterwards.
    """
    if not sid or "/" in sid or "\\" in sid or ".." in sid:
        return None
    if Path(sid).suffix.lower() not in _ALLOWED_SUFFIXES:
        return None
    root = _dir().resolve()
    try:
        p = (root / sid).resolve()
        p.relative_to(root)
    except (OSError, ValueError):
        return None
    return p if p.is_file() else None


def url_for(thread_id: str, sid: str | None) -> str:
    """Public URL for a thread's snapshot, or "" when it has none.

    Keyed by THREAD, not by hash. The content-addressed id says nothing about
    which bot the picture belongs to, so a hash-keyed URL could not be gated
    against a Safe-Mode session without reverse-mapping it — and the obvious
    "is any referencing thread safe?" test leaks a non-safe bot's face as soon
    as one safe bot shares the same image. Going through the thread means the
    route reuses the rule that already governs the thread itself.

    The bytes behind it are immutable, so the response caches forever; the URL
    carries the snapshot's own hash prefix (`?s=`) so it CHANGES whenever the
    thread's pin does — without it, a re-pinned thread kept showing the old
    picture out of the browser's immutable cache for up to a year.
    """
    if not sid:
        return ""
    return f"/api/threads/{thread_id}/avatar?s={Path(sid).stem[:8]}"


def prune(keep: set[str]) -> int:
    """Delete snapshots no thread references any more. Returns the count.

    An EMPTY keep set is refused rather than obeyed. Every caller derives
    `keep` by reading the thread table, so "keep nothing" means either a store
    that legitimately has nothing to protect (deleting is then a no-op anyway)
    or a caller that failed to read the threads -- and in the second case the
    literal instruction is "delete the entire store". A prune that deletes
    everything is never the right answer to a question nobody asked; the one
    time this ran with a keep-set of one it took 117 snapshots with it.
    """
    root = _dir()
    if not root.is_dir():
        return 0
    if not keep:
        log.warning("refusing to prune avatar snapshots: empty keep set")
        return 0
    removed = 0
    for f in root.iterdir():
        if f.name.startswith(".") or f.name in keep:
            continue
        try:
            f.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def history(bot_id: str, threads: list) -> list[dict]:
    """Distinct past avatars for a bot, newest first.

    Built from the thread snapshots, which is the only place the app records
    what an avatar USED to be — the rotation overwrites main-face.png in place,
    so without this the previous image is unrecoverable from the app's own
    state.

    Each entry carries the earliest and latest thread that started under it, so
    an agent (or a human) can say "the one from last Tuesday" and mean
    something. Deduplicated by snapshot id: one entry per distinct picture, not
    one per thread.
    """
    seen: dict[str, dict] = {}
    for t in threads:
        sid = getattr(t, "avatar_snapshot", None)
        if not sid or getattr(t, "bot_id", None) != bot_id:
            continue
        created = getattr(t, "created_at", "") or ""
        e = seen.get(sid)
        if e is None:
            seen[sid] = {"id": sid, "first_seen": created, "last_seen": created,
                         "thread_count": 1}
        else:
            e["thread_count"] += 1
            if created and created < e["first_seen"]:
                e["first_seen"] = created
            if created > e["last_seen"]:
                e["last_seen"] = created
    out = [e for e in seen.values() if path_for(e["id"]) is not None]
    out.sort(key=lambda e: e["last_seen"], reverse=True)
    return out


def missing_blobs(threads: list) -> list[tuple[str, str]]:
    """(thread_id, snapshot_id) for every thread whose snapshot file is gone.

    A thread pins a snapshot id in SQLite while the bytes live on disk, so the
    two can drift: a restore that misses the store, a stray delete, a prune fed
    a bad keep set. The visible result is a broken thumbnail, and nothing
    anywhere said why -- the serving route just 404s and the frontend quietly
    falls back to the live avatar. This turns that drift into something a log
    line and /api/health can report.
    """
    out: list[tuple[str, str]] = []
    for t in threads:
        sid = getattr(t, "avatar_snapshot", None)
        if sid and path_for(sid) is None:
            out.append((getattr(t, "id", "?"), sid))
    return out
