"""First-run seed: the explainer threads a brand-new install opens to.

A fresh clone should not open on an empty chat. On the very first start with an
EMPTY database, three threads are created on the default bot, Atlas:

* "Meet Atlas"         -- who she is, where to put the key, what she can do;
* "How DisPatch works" -- the tour: bots, Connect an AI, profiles, add-ons;
* "Set up add-ons"     -- the recommended order for everything optional
  (2.3.7), following app/catalog.yaml. ``POST /api/guide/seed?name=…`` posts
  any guide on an existing install.

Each holds ONE assistant message whose text is the Markdown file of the same
name under ``app/guide/``. A reply in either thread simply goes to Atlas.

The rules, in order of how much damage getting them wrong would do:

* **Never seed an install that has threads.** Any thread (archived ones count)
  means somebody already lives here; an upgrade is not a first run.
* **Once.** ``<data>/.firstrun-seeded`` is written after the attempt (and also
  when the install turned out not to be a first run), so deleting every thread
  later does not bring the tour back. A crash half-way leaves a thread, which
  by the first rule also stops a second attempt.
* **Only for the shipped bot.** No exact-id ``atlas`` on the roster (an install
  with its own config.yaml, a test roster) means nothing to seed.
* **The normal write path.** The rows go through the same funnel as any
  assistant reply (``main._deliver_assistant_text`` -> ``_prepare_persist``), so
  ``[[media:...]]`` markers, images and sanitising behave identically.
  ``metadata.seeded`` marks them.

Pictures in the guide are plain Markdown, ``![alt](/static/guide/<file>.png)``,
served from ``frontend/static/guide/`` (same-origin, shipped in the repo).

``DISPATCH_FIRSTRUN=0`` switches the seed off entirely.

Atlas copies (4.0)
------------------
Every Admin and User profile has its own Atlas, a personal copy of the shipped direct-API bot
(``profiles_service.spawn_atlas``). The first Admin of a fresh install is the one exception: it
sees the household Atlas this module's seed talks to. :func:`atlas_template` and
:func:`copy_atlas_avatar` are the two pieces a copy is made from.
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from . import config, ownership

log = logging.getLogger("local-chat.firstrun")

BOT_ID = "atlas"
FLAG_NAME = ".firstrun-seeded"
GUIDE_DIR = Path(__file__).resolve().parent / "guide"
AVATAR_SRC_DIR = Path(__file__).resolve().parent / "default_avatars"
#: The shipped avatar set, keyed by the bot whose `avatar` names its face file.
DEFAULT_AVATARS: tuple[str, ...] = ("atlas-face.png", "atlas-full.png", "atlas-thumb.png")

#: (thread title, guide file) in the order they should appear in the list.
#: Created in REVERSE so the first one is the newest and sits on top.
THREADS: tuple[tuple[str, str], ...] = (
    ("Meet Atlas", "meet-atlas.md"),
    ("Using DisPatch", "using-dispatch.md"),
    ("How DisPatch works", "how-dispatch-works.md"),
    ("Set up add-ons", "set-up-add-ons.md"),
)

#: Guide name (the file stem) -> (title, file), for seeding one on demand.
GUIDES: dict[str, tuple[str, str]] = {f.removesuffix(".md"): (t, f) for t, f in THREADS}


class GuideExists(Exception):
    """That guide was already posted on this install."""


async def seed_one(db, deliver, name: str, *, force: bool = False, profile_id: str | None = None) -> str:
    """Post ONE guide thread now (an existing install never auto-seeds), on the Atlas that talks
    to ``profile_id`` (the caller; None = the protected Admin, for a machine caller) and in that
    profile's chats.

    Raises KeyError for an unknown guide or no Atlas for that profile, GuideExists when the
    guide was already posted there (unless `force`). Returns the new thread id."""
    from . import accounts
    title, fname = GUIDES[name]
    pid = profile_id or ownership.owner_id()
    prof = accounts.get_profile(pid) if pid else None
    if prof is None and not pid:
        # An open install (nobody owns it yet): the household Atlas, adopted by the first Admin.
        bot_id = BOT_ID if config.get_bot(BOT_ID) is not None else None
    else:
        bot_id = _atlas_of(prof) if prof is not None else None
    if bot_id is None:
        raise KeyError("atlas")
    house = bot_id == BOT_ID
    # The household Atlas in the protected Admin's chats (or an open install's) is where the
    # first-run seed put these.
    legacy = house and (prof is None or getattr(prof, "protected", False))
    sources = [f"firstrun:{fname}"] if legacy else \
        [f"guide:{bot_id}:{pid}:{fname}" if house else f"guide:{bot_id}:{fname}"]
    source = sources[-1]
    for seen in sources:
        if await db.source_id_seen(seen):
            if not force:
                raise GuideExists(name)
            source = f"{source}:{int(time.time())}"
            break
    text = _read_guide(fname)
    if not text:
        raise KeyError(name)
    thread = await db.create_thread(bot_id, title=title, owner_profile_id=pid)
    await deliver(thread.id, text, metadata={"seeded": True}, source_id=source,
                  dedup_recent_window=False)
    log.info("guide %s posted on demand (thread %s)", name, thread.id)
    return thread.id


def flag_path() -> Path:
    return config.DATA_DIR / FLAG_NAME


def _write_flag() -> None:
    try:
        flag_path().parent.mkdir(parents=True, exist_ok=True)
        flag_path().write_text("seeded\n", encoding="utf-8")
    except OSError:
        log.warning("could not write %s", flag_path(), exc_info=True)


def _read_guide(name: str) -> str:
    try:
        return (GUIDE_DIR / name).read_text(encoding="utf-8").strip()
    except OSError:
        log.warning("first-run guide %s is missing", name)
        return ""


def install_default_avatars() -> list[str]:
    """Copy the shipped avatar set into ``config.AVATAR_DIR``. Returns the file
    names written.

    Independent of the first-run flag: a missing file is the only condition,
    so a deleted avatar comes back and a fresh install always has one. An
    existing file is NEVER overwritten, and when the face file is already
    there the whole set is left alone (an install with its own Atlas picture
    must not end up with our full-size image beside its face). Each file is
    written to a temp name and renamed. Never raises: a missing source or an
    unwritable directory is a log line.
    """
    written: list[str] = []
    try:
        dest_dir = Path(config.AVATAR_DIR)
        if (dest_dir / DEFAULT_AVATARS[0]).exists():
            return written
        dest_dir.mkdir(parents=True, exist_ok=True)
        for name in DEFAULT_AVATARS:
            dest = dest_dir / name
            if dest.exists():
                continue
            tmp = dest.with_name(f".{name}.partial")
            try:
                tmp.write_bytes((AVATAR_SRC_DIR / name).read_bytes())
                tmp.replace(dest)
                written.append(name)
            except OSError:
                log.warning("could not install default avatar %s", name, exc_info=True)
                tmp.unlink(missing_ok=True)
    except Exception:
        log.warning("default avatar install failed (ignored)", exc_info=True)
    return written


async def seed_if_first_run(
    db: Any,
    deliver: Callable[..., Awaitable[Any]],
) -> list[str]:
    """Seed the guide threads when this is a first run. Returns the thread ids
    created (empty = nothing seeded). Never raises: a failed seed is logged and
    the app starts normally."""
    if os.environ.get("DISPATCH_FIRSTRUN", "1").strip().lower() in ("0", "false", "no", "off"):
        return []
    try:
        if flag_path().exists():
            return []
        cur = await db.db.execute("SELECT COUNT(*) FROM threads")
        row = await cur.fetchone()
        if row and int(row[0]) > 0:
            _write_flag()                  # an existing install: never, ever
            return []
        bot = next((b for b in config.load_bots() if b.id == BOT_ID), None)
        if bot is None:
            _write_flag()
            return []
        made: list[str] = []
        for title, fname in reversed(THREADS):
            text = _read_guide(fname)
            if not text:
                continue
            # The Owner's - or, on a brand-new install, nobody's until the Owner
            # is created (accounts.create_profile adopts unowned rows then).
            thread = await db.create_thread(BOT_ID, title=title,
                                            owner_profile_id=ownership.owner_id())
            made.append(thread.id)
            await deliver(thread.id, text, metadata={"seeded": True},
                          source_id=f"firstrun:{fname}",
                          dedup_recent_window=False)
        if made:
            log.info("first run: seeded %d guide thread(s) on %s", len(made), BOT_ID)
            _write_flag()
        return made
    except Exception:
        log.exception("first-run seed failed (ignored)")
        return []


#: The display name of the default (lock-target) profile a fresh install creates.
DEFAULT_PROFILE_NAME = "DisPatch"

ATLAS_NAME = "Atlas"
#: ``Bot.pack`` of every personal Atlas copy, so "does this profile have its Atlas yet?" has an
#: answer that survives a rename.
ATLAS_PACK = "atlas"


def atlas_template() -> config.Bot:
    """The shipped Atlas as a fresh object, stripped to what a personal copy keeps: name, emoji,
    quick actions, the lookups switch ON, no provider (the connection is chosen when it is
    copied), no knowledge folder, no pools, no image jobs, no voice."""
    shipped = next((b for b in config.shipped_default_bots() if b.id == BOT_ID), None)
    # Built from the few stable fields, not copied with `replace`: the shipped roster entry
    # carries household-only settings (flags, pools) that a personal copy must never inherit.
    return config.Bot(
        id=BOT_ID, name=ATLAS_NAME, emoji=(shipped.emoji if shipped else "") or "\U0001F30D",
        model_hint="Direct API", pack=ATLAS_PACK,
        advisor={"providers": [], "tools": True})


def copy_atlas_avatar(bot_id: str) -> str:
    """Write the shipped Atlas picture (face, full, thumb) for ``bot_id`` into the avatar
    directory and return the face file name ("" when the source set is not there).
    Each file goes through a temp name and a rename; an existing file is replaced."""
    dest_dir = Path(config.AVATAR_DIR)
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        for role in ("face", "full", "thumb"):
            src = AVATAR_SRC_DIR / f"atlas-{role}.png"
            dest = dest_dir / f"{bot_id}-{role}.png"
            tmp = dest.with_name(f".{dest.name}.partial")
            shutil.copyfile(src, tmp)
            tmp.replace(dest)
    except OSError:
        log.warning("could not copy the Atlas picture for %s", bot_id, exc_info=True)
        return ""
    return f"{bot_id}-face.png"


# --------------------------------------------------------------------------- #
# Guides for every Atlas copy, and the "What's new" thread after an update (4.0)
# --------------------------------------------------------------------------- #
#
# Every personal Atlas opens with the "how to use DisPatch" chats: that is what it is made for.
# spawn_atlas() is synchronous (people routes, the CLI, Reset) and has no database handle, so it
# only queues the copy here (<data>/.atlas-guides-pending.json); the server posts the threads
# through the normal delivery funnel as soon as it can - at once when it is running
# (bind_server), else on its next start. The thread source id ``guide:<bot>:<file>`` makes a
# repeat a no-op, so a queue entry can never post the same guide twice.
#
# After an update, every Admin and User profile gets one "What's new" thread on its Atlas (the
# protected Admin without a copy of its own: the household Atlas), from
# ``guide/whats-new-<version>.md``. ``<data>/.release-notes-seen`` holds the last version
# announced; a fresh install records its version without posting anything.

import contextlib  # noqa: E402
import json  # noqa: E402
import threading  # noqa: E402

from .contracts.permissions import Role  # noqa: E402

PENDING_NAME = ".atlas-guides-pending.json"
BACKFILL_FLAG = ".atlas-guides-backfilled"
RELEASE_FLAG = ".release-notes-seen"

#: What a personal Atlas opens with (title, file), first = newest = top of the list.
PROFILE_GUIDES: tuple[tuple[str, str], ...] = (
    ("Meet Atlas", "meet-your-atlas.md"),
    ("Using DisPatch", "using-dispatch.md"),
)
#: An Admin's Atlas also gets the setup tour.
ADMIN_GUIDES: tuple[tuple[str, str], ...] = (
    ("How DisPatch works", "how-dispatch-works.md"),
)

_pending_lock = threading.Lock()
_server: dict[str, Any] = {}


def _switched_on(var: str) -> bool:
    return os.environ.get(var, "1").strip().lower() not in ("0", "false", "no", "off")


def guides_enabled() -> bool:
    return _switched_on("DISPATCH_FIRSTRUN") and _switched_on("DISPATCH_ATLAS_GUIDES")


def _pending_path() -> Path:
    return config.DATA_DIR / PENDING_NAME


def _read_pending() -> list[dict[str, str]]:
    try:
        rows = json.loads(_pending_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [r for r in rows if isinstance(r, dict) and r.get("profile") and r.get("bot")] \
        if isinstance(rows, list) else []


def _write_pending(rows: list[dict[str, str]]) -> None:
    path = _pending_path()
    try:
        if not rows:
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.partial")
        tmp.write_text(json.dumps(rows), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        log.warning("could not write %s", path, exc_info=True)


def request_guides(profile_id: str, bot_id: str) -> None:
    """Queue the guide threads for a new personal Atlas. Never raises; safe from any thread."""
    if not guides_enabled():
        return
    try:
        with _pending_lock:
            rows = _read_pending()
            if not any(r["profile"] == profile_id and r["bot"] == bot_id for r in rows):
                rows.append({"profile": profile_id, "bot": bot_id})
                _write_pending(rows)
        loop = _server.get("loop")
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(_schedule_drain)
    except Exception:
        log.warning("could not queue the Atlas guides", exc_info=True)


def bind_server(loop: Any, db: Any, deliver: Callable[..., Awaitable[Any]]) -> None:
    """The running server: queued guides are posted straight away from now on."""
    _server.update(loop=loop, db=db, deliver=deliver)


def unbind_server() -> None:
    _server.clear()


def _schedule_drain() -> None:
    import asyncio
    db, deliver = _server.get("db"), _server.get("deliver")
    if db is None or deliver is None:
        return
    task = asyncio.ensure_future(drain_guides(db, deliver))
    _server.setdefault("tasks", set()).add(task)
    task.add_done_callback(_server["tasks"].discard)


def _guides_for(profile: Any) -> tuple[tuple[str, str], ...]:
    return PROFILE_GUIDES + (ADMIN_GUIDES if profile.role is Role.ADMIN else ())


async def _post_guides(db: Any, deliver: Callable[..., Awaitable[Any]], profile: Any,
                       bot_id: str) -> list[str]:
    made: list[str] = []
    for title, fname in reversed(_guides_for(profile)):
        source = f"guide:{bot_id}:{fname}"
        if await db.source_id_seen(source):
            continue
        text = _read_guide(fname)
        if not text:
            continue
        thread = await db.create_thread(bot_id, title=title, owner_profile_id=profile.id)
        try:
            await deliver(thread.id, text, metadata={"seeded": True}, source_id=source,
                          dedup_recent_window=False)
        except BaseException:
            # No empty guide chat left behind: a later try posts it whole.
            with contextlib.suppress(Exception):
                await db.delete_thread(thread.id)
            raise
        made.append(thread.id)
    return made


_drain_lock: Any = None


async def drain_guides(db: Any, deliver: Callable[..., Awaitable[Any]]) -> list[str]:
    """Post every queued guide set whose Atlas and profile still exist. Returns the thread ids
    made. Never raises."""
    import asyncio

    from . import accounts
    global _drain_lock
    if _drain_lock is None:
        _drain_lock = asyncio.Lock()
    made: list[str] = []
    async with _drain_lock:
        with _pending_lock:
            rows = _read_pending()
        done: list[dict[str, str]] = []
        retry: list[dict[str, Any]] = []
        for row in rows:
            try:
                prof = accounts.get_profile(row["profile"])
                bot = config.get_bot(row["bot"])
                if (prof is not None and bot is not None and bot.scope == "personal"
                        and bot.owner_profile == prof.id and prof.role is not Role.BASIC):
                    made += await _post_guides(db, deliver, prof, bot.id)
                done.append(row)
            except Exception:
                log.warning("could not post the Atlas guides for one profile", exc_info=True)
                done.append(row)
                # Posted guides are skipped next time (their source ids), so a later try only
                # adds what is missing; a row that keeps failing is given up after 3 tries.
                tries = int(row.get("tries") or 0) + 1
                if tries < 3:
                    retry.append({**row, "tries": tries})
        with _pending_lock:
            _write_pending([r for r in _read_pending() if r not in done] + retry)
    if made:
        log.info("posted %d Atlas guide thread(s)", len(made))
    return made


def backfill_guides_once() -> int:
    """4.0 upgrade: queue the guides for every personal Atlas made before they existed (once
    per install; the source ids keep a repeat harmless). Returns how many were queued."""
    flag = config.DATA_DIR / BACKFILL_FLAG
    if not guides_enabled() or flag.exists():
        return 0
    n = 0
    for b in config.load_bots():
        if b.scope == "personal" and b.pack == ATLAS_PACK and b.owner_profile:
            request_guides(b.owner_profile, b.id)
            n += 1
    try:
        flag.write_text("done\n", encoding="utf-8")
    except OSError:
        log.warning("could not write %s", flag, exc_info=True)
    return n


async def count_threads(db: Any) -> int:
    cur = await db.db.execute("SELECT COUNT(*) FROM threads")
    row = await cur.fetchone()
    return int(row[0]) if row else 0


HOUSE_FLAG = ".atlas-house-guides"
#: What the main Admin's household Atlas gets on an upgrade (the 4.0 tour; Meet Atlas and
#: Set up add-ons are the first-run ones and did not change in substance).
HOUSE_GUIDES: tuple[tuple[str, str], ...] = (
    ("Using DisPatch", "using-dispatch.md"),
    ("How DisPatch works", "how-dispatch-works.md"),
)


async def backfill_house_guides(db: Any, deliver: Callable[..., Awaitable[Any]]) -> list[str]:
    """Once per install: the protected Admin who still talks to the household Atlas (no copy
    of its own) gets the current guides there, in its own chats - an upgrade from 3.x left it
    only the old ones. A guide already posted by the first-run seed (or by this, before) is
    skipped. Never raises."""
    from . import accounts
    flag = config.DATA_DIR / HOUSE_FLAG
    if not guides_enabled() or flag.exists():
        return []
    made: list[str] = []
    try:
        prof = accounts.get_protected_admin()
        if prof is None:
            return []                       # an open install: the first-run seed covers it
        if _atlas_of(prof) == BOT_ID:
            for title, fname in reversed(HOUSE_GUIDES):
                source = f"guide:{BOT_ID}:{prof.id}:{fname}"
                if await db.source_id_seen(f"firstrun:{fname}") or await db.source_id_seen(source):
                    continue
                text = _read_guide(fname)
                if not text:
                    continue
                thread = await db.create_thread(BOT_ID, title=title, owner_profile_id=prof.id)
                await deliver(thread.id, text, metadata={"seeded": True}, source_id=source,
                              dedup_recent_window=False)
                made.append(thread.id)
        flag.write_text("done\n", encoding="utf-8")
    except Exception:
        log.warning("could not post the main Admin's guides", exc_info=True)
    return made


def release_notes_file(version: str) -> Path:
    return GUIDE_DIR / f"whats-new-{version}.md"


def _atlas_of(profile: Any) -> str | None:
    """The Atlas that talks to ``profile``: its own copy, else the household one when the
    profile may use it (the protected Admin, a profile made before 4.0 that has no copy yet,
    one that deleted its copy)."""
    own = next((b for b in config.load_bots() if b.scope == "personal"
                and b.owner_profile == profile.id and b.pack == ATLAS_PACK), None)
    if own is not None:
        return own.id
    house = config.get_bot(BOT_ID)
    if house is None or house.scope == "personal":
        return None
    if getattr(profile, "protected", False):
        return house.id
    from . import characters, policy
    try:
        principal = policy.profile_principal(profile)
    except Exception:
        return None
    return house.id if characters.may_use(house, principal) else None


async def post_release_notes(db: Any, deliver: Callable[..., Awaitable[Any]],
                             version: str | None = None, *, fresh: bool | None = None) -> list[str]:
    """After an update: one "What's new" thread per Admin and User profile, on its Atlas.
    ``fresh``: the install had no chats before this start (the caller counts BEFORE the
    first-run seed and the guides, so What's new can be posted last, on top); None = count now.
    Returns the thread ids made. Never raises."""
    from . import __version__, accounts
    version = version or __version__
    flag = config.DATA_DIR / RELEASE_FLAG
    if not _switched_on("DISPATCH_RELEASE_NOTES"):
        return []
    try:
        try:
            seen = flag.read_text(encoding="utf-8").strip()
        except OSError:
            seen = ""
        if seen == version:
            return []
        made: list[str] = []
        notes = _read_guide(release_notes_file(version).name) \
            if release_notes_file(version).is_file() else ""
        if fresh is None:
            fresh = await count_threads(db) == 0
        if notes and not fresh:
            # "4.0" for a x.y.0 release; a patch release with notes of its own names itself.
            parts = version.split(".")
            shown = ".".join(parts[:2]) if len(parts) < 3 or parts[2] in ("0", "") else version
            title = f"What's new in DisPatch {shown}"
            for prof in accounts.list_profiles():
                if prof.role is Role.BASIC or getattr(prof, "disabled", False):
                    continue
                bot_id = _atlas_of(prof)
                if bot_id is None:
                    continue
                source = f"release:{version}:{prof.id}"
                if await db.source_id_seen(source):
                    continue
                thread = await db.create_thread(bot_id, title=title, owner_profile_id=prof.id)
                await deliver(thread.id, notes, metadata={"seeded": True, "release": version},
                              source_id=source, dedup_recent_window=False)
                made.append(thread.id)
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.write_text(version + "\n", encoding="utf-8")
        if made:
            log.info("posted the %s release notes in %d profile(s)", version, len(made))
        return made
    except Exception:
        log.exception("release notes failed (ignored)")
        return []
