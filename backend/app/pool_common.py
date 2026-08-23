"""Shared one-shot pool mechanics.

Two features keep a bank of images on hand and burn each on use: reaction
images (reactions.py — single files keyed by mood) and avatar pools
(avatar_pool.py — face/full pairs per bot). Their layouts differ, but the
load-bearing mechanics are identical, and this module is the single
implementation of them:

* the FILESYSTEM is the manifest — a file in a ready directory is drawable,
  hand-dropping one requires no registry write;
* an atomic ``os.replace`` into a spent directory IS the one-shot lock —
  of two racing consumers exactly one wins, the loser's rename finds no
  source and reports the loss;
* new items are staged as ``<name>.part`` and published with an atomic
  rename, so a half-written image can never be drawn;
* a nightly top-up is DUE while ``batch_date`` has not been stamped today
  and the local clock has passed the refresh hour — stamping only on a
  completed fill is what makes a rig that was off keep retrying.

Policy stays with the callers: what counts as an item (file vs pair), draw
keys, targets and deficits are the feature's business, not this module's.
"""
from __future__ import annotations

import logging
import os
import shutil
import time
import uuid
from collections.abc import Iterable
from pathlib import Path

log = logging.getLogger("local-chat.pool")

# How old a stranded `*.part` staging file must be before a sweep may unlink
# it. Generous on purpose: generation copies into `<name>.part` and atomically
# renames, on a worker thread — a day-long guard can never race an in-flight
# generate, while still cleaning up after a crash between the two steps.
PART_STALE_S = 24 * 3600


def retire(src: Path, dst_dir: Path) -> Path | None:
    """Move one ready blob into a spent directory — the one-shot lock itself.

    ``os.replace`` is atomic on the same filesystem, so of two racing retirees
    of the same source exactly one wins; the loser's replace raises (source
    gone) and returns None. The blob keeps its name, so anything that resolves
    it by name keeps working from the spent side.
    """
    dst = dst_dir / src.name
    try:
        dst_dir.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            # A same-named spent blob already exists (uuid stems make this
            # near-impossible). Never overwrite it — spent blobs are kept for
            # good — park the newcomer under a suffixed name instead.
            dst = dst_dir / f"{src.stem}-{uuid.uuid4().hex[:6]}{src.suffix}"
        os.replace(src, dst)
    except FileNotFoundError:
        return None                       # lost the consume race — one winner
    except OSError as e:
        log.warning("could not retire pool image %s: %s", src.name, e)
        return None
    try:
        os.utime(dst)                     # spent_at = mtime, stamped at burn
    except OSError:
        pass
    return dst


def publish_copy(src: Path, dest: Path) -> bool:
    """Copy ``src`` to ``dest`` via ``.part`` + atomic rename.

    The rename is the instant the item becomes drawable — a reader can never
    see a half-copied file under the final name.
    """
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        shutil.copyfile(src, part)
        os.replace(part, dest)
    except OSError as e:
        log.warning("could not publish pool image %s: %s", dest.name, e)
        return False
    return True


def image_files(d: Path, exts: set[str]) -> list[Path]:
    """The stock inside one directory: plain image files only.

    Dotfiles and non-image suffixes (which covers ``*.part`` staging) are
    invisible, so a crash mid-generate or a stray .DS_Store can never be
    drawn. Symlinks are refused — serve paths do containment checks that
    would reject their targets anyway, so refusing to draw them fails closed.
    """
    out: list[Path] = []
    try:
        entries = sorted(d.iterdir())
    except OSError:
        return []
    for p in entries:
        if p.name.startswith(".") or p.suffix.lower() not in exts:
            continue
        try:
            if not p.is_file() or p.is_symlink():
                continue
        except OSError:
            continue
        out.append(p)
    return out


def sweep_parts(dirs: Iterable[Path], max_age_s: int = PART_STALE_S) -> int:
    """Unlink ``*.part`` staging stranded by a crash between a generation's
    copy and its atomic rename. Returns the number removed.

    The age guard keeps this from racing an in-flight generate: a fresh .part
    is a rig mid-copy, not wreckage. Nothing else is ever touched.
    """
    removed = 0
    now = time.time()
    for d in dirs:
        try:
            entries = list(d.iterdir())
        except OSError:
            continue
        for p in entries:
            if not p.name.endswith(".part"):
                continue
            try:
                if not p.is_file() or p.is_symlink():
                    continue             # only ever unlink our own staging
                if now - p.stat().st_mtime < max_age_s:
                    continue
                p.unlink()
                removed += 1
            except OSError:
                continue
    return removed


def daily_due(enabled: bool, batch_date: str, refresh_hour: int) -> bool:
    """True once tonight's refill window has opened and it hasn't completed.

    ``batch_date`` is stamped only when a nightly fill actually reaches its
    targets (the caller's business), so a rig that was off at the refresh
    hour keeps the window open and retries every cycle.
    """
    if not enabled:
        return False
    now = time.localtime()
    today = time.strftime("%Y-%m-%d", now)
    return batch_date != today and now.tm_hour >= refresh_hour
