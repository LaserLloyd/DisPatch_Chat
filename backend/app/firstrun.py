"""First-run guides: what a brand-new install opens to.

A fresh clone should not open on an empty chat. Since 4.2 the household Atlas is a **template**
nobody chats with, so nothing is posted on it. The **claim** (the first Admin is created, see
``profiles_service.on_profile_created(first_admin=True)``) gives that Admin a copy of Atlas, the
kit engine posts the Atlas kit chats to it (``app/kits.py``) and :func:`post_claim_guides` adds
the one first-run chat the kit does not carry:

* "Set up add-ons" -- the recommended order for everything optional (2.3.7), following
  app/catalog.yaml. ``POST /api/guide/seed?name=...`` posts any guide on an existing install.

The rules, in order of how much damage getting them wrong would do:

* **Never seed an install that has threads.** Any thread (archived ones count) means somebody
  already lives here; an upgrade is not a first run. :func:`seed_if_first_run` writes the flag
  then, so a later claim never posts.
* **Once.** ``<data>/.firstrun-seeded`` is written after the post, so deleting every thread
  later does not bring the tour back.
* **The normal write path.** The rows go through the same funnel as any assistant reply
  (``main._deliver_assistant_text`` -> ``_prepare_persist``), so ``[[media:...]]`` markers,
  images and sanitising behave identically. ``metadata.seeded`` marks them.

Pictures in the guide are plain Markdown, ``![alt](/static/guide/<file>.png)``, served from
``frontend/static/guide/`` (same-origin, shipped in the repo).

``DISPATCH_FIRSTRUN=0`` switches the seed off entirely.

Atlas copies (4.2)
------------------
Every profile entitled to the Atlas template has its own copy (``profiles_service.make_copy``;
a live copy that follows the template). :func:`copy_atlas_avatar` is the one piece a copy is
made from here.
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
    """Post ONE guide thread now (an existing install never auto-seeds), on the Atlas copy that
    talks to ``profile_id`` (the caller; None = the protected Admin, for a machine caller) and in
    that profile's chats.

    Raises KeyError for an unknown guide or no Atlas copy for that profile, GuideExists when the
    guide was already posted there (unless `force`). Returns the new thread id."""
    from . import accounts
    title, fname = GUIDES[name]
    pid = profile_id or ownership.protected_admin_id()
    prof = accounts.get_profile(pid) if pid else None
    bot_id = _atlas_of(prof) if prof is not None else None
    if bot_id is None:
        raise KeyError("atlas")
    source = f"guide:{bot_id}:{fname}"
    # What 4.0/4.1 posted for the main Admin on the household Atlas (now re-pointed to its copy).
    seen_before = [source] + ([f"firstrun:{fname}", f"guide:{BOT_ID}:{pid}:{fname}"]
                              if getattr(prof, "protected", False) else [])
    for seen in seen_before:
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
    """The startup half of the first-run guides. Posts nothing (the household Atlas is a template
    nobody chats with; :func:`post_claim_guides` posts when the install is claimed). What it does
    is protect an existing install: with threads present the flag is written, so a claim never
    posts there. Returns the thread ids created (always empty). Never raises."""
    if os.environ.get("DISPATCH_FIRSTRUN", "1").strip().lower() in ("0", "false", "no", "off"):
        return []
    try:
        if flag_path().exists():
            return []
        cur = await db.db.execute("SELECT COUNT(*) FROM threads")
        row = await cur.fetchone()
        if row and int(row[0]) > 0:
            _write_flag()                  # an existing install: never, ever
    except Exception:
        log.exception("first-run check failed (ignored)")
    return []


#: The first-run chats a claim posts beyond the Atlas kit's (the kit carries the other tours).
CLAIM_GUIDES: tuple[tuple[str, str], ...] = (("Set up add-ons", "set-up-add-ons.md"),)


async def post_claim_guides(db: Any, deliver: Callable[..., Awaitable[Any]]) -> list[str]:
    """The claim hook: once the first Admin has its Atlas copy, post the first-run chats the kit
    does not carry, in its chats, and write the flag. Does nothing on an install that was already
    in use (``<data>/.firstrun-seeded`` exists), without a protected Admin or Atlas copy yet (the
    next sync tries again). Never raises."""
    from . import accounts
    if not guides_enabled() or flag_path().exists():
        return []
    try:
        prof = accounts.get_protected_admin()
        bot_id = _atlas_of(prof) if prof is not None else None
        if prof is None or bot_id is None:
            return []
        made: list[str] = []
        for title, fname in reversed(CLAIM_GUIDES):
            source = f"firstrun:{fname}"
            text = _read_guide(fname)
            if not text or await db.source_id_seen(source):
                continue
            thread = await db.create_thread(bot_id, title=title, owner_profile_id=prof.id)
            made.append(thread.id)
            await deliver(thread.id, text, metadata={"seeded": True}, source_id=source,
                          dedup_recent_window=False)
        _write_flag()
        if made:
            log.info("claim: posted %d first-run chat(s) on %s", len(made), bot_id)
        return made
    except Exception:
        log.exception("first-run chats failed (ignored)")
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
# Every personal Atlas opens with the "how to use DisPatch" chats. Since 4.1 those are the
# chats of the shipped Atlas kit (app/kits.py): the kit engine posts them to every copy
# through the normal delivery funnel. 4.0 queued them in <data>/.atlas-guides-pending.json and
# posted them as ``guide:<bot>:<file>``; the kit engine reads both once, when it upgrades an
# install (kits._migrate), so nothing is posted twice.
#
# After an update, every Admin and User profile gets one "What's new" thread on its Atlas copy, from
# ``guide/whats-new-<version>.md``. ``<data>/.release-notes-seen`` holds the last version
# announced; a fresh install records its version without posting anything.

from .contracts.permissions import Role  # noqa: E402

#: 4.0's guide queue and its one-off backfill flag (read by the 4.1 upgrade only).
PENDING_NAME = ".atlas-guides-pending.json"
BACKFILL_FLAG = ".atlas-guides-backfilled"
RELEASE_FLAG = ".release-notes-seen"


def _switched_on(var: str) -> bool:
    return os.environ.get(var, "1").strip().lower() not in ("0", "false", "no", "off")


def guides_enabled() -> bool:
    """The kit chats (the Atlas guides among them) are posted at all."""
    return _switched_on("DISPATCH_FIRSTRUN") and _switched_on("DISPATCH_ATLAS_GUIDES")


async def count_threads(db: Any) -> int:
    cur = await db.db.execute("SELECT COUNT(*) FROM threads")
    row = await cur.fetchone()
    return int(row[0]) if row else 0


def release_notes_file(version: str) -> Path:
    return GUIDE_DIR / f"whats-new-{version}.md"


def _atlas_of(profile: Any) -> str | None:
    """The Atlas that talks to ``profile``: its own copy of the Atlas template (None when it has
    none: nobody chats with the template)."""
    own = [b for b in config.load_bots() if b.scope == "personal" and b.owner_profile == profile.id
           and (b.template == BOT_ID or (not b.template and b.pack == ATLAS_PACK))]
    own.sort(key=lambda b: (b.template != BOT_ID, b.order))
    return own[0].id if own else None


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
