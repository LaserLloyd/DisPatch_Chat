"""First-run seed: the two explainer threads a brand-new install opens to.

A fresh clone should not open on an empty chat. On the very first start with an
EMPTY database, two threads are created on the default bot, Atlas:

* "Meet Atlas"         -- who he is, where to put the key, what he can do;
* "How DisPatch works" -- the tour: bots, Connect an AI, Safe Mode, add-ons.

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
"""

from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from . import config

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
    ("How DisPatch works", "how-dispatch-works.md"),
)


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
    """Seed the two threads when this is a first run. Returns the thread ids
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
            thread = await db.create_thread(BOT_ID, title=title)
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
