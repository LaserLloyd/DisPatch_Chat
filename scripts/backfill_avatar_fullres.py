#!/usr/bin/env python3
"""Give existing avatar snapshots their missing full-resolution half.

An avatar is a PAIR: a square face crop for the UI, and a full-resolution
original that the lightbox opens. Snapshots taken before that was understood
captured only the face, so clicking one of those thread thumbnails falls back
to the crop — the right picture, the wrong resolution.

The originals are not lost: the avatar pool stores every image as
``<bot>-<label>-face.png`` beside ``<bot>-<label>-full.png``. This matches each
snapshot to its pool entry BY CONTENT — hashing the bytes, never the filename,
because the rotation copies pool files over ``<bot>-face.png`` and the name of
the image a snapshot came from is not recorded anywhere.

    python3 scripts/backfill_avatar_fullres.py --avatar-dir <install>/frontend/static/avatars
    python3 scripts/backfill_avatar_fullres.py --avatar-dir ... --go

Safe to re-run: a snapshot that already has its full-res half is skipped, and
nothing is ever overwritten or deleted.
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
from pathlib import Path


def _digest(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", default=str(Path.home() / ".local/share/local-chat"),
                    help="DisPatch data directory (holds avatar-snapshots/)")
    # No default: the pool lives wherever the install does, and hard-coding one
    # maintainer's path into a public repo is both wrong for everyone else and
    # a private-data leak (the scrub check refuses it, correctly).
    ap.add_argument("--avatar-dir", required=True,
                    help="the avatar POOL — the directory where <name>-face.png "
                         "sits beside <name>-full.png (usually "
                         "<install>/frontend/static/avatars)")
    ap.add_argument("--go", action="store_true", help="actually write (default is a dry run)")
    args = ap.parse_args()

    snap_dir = Path(args.data_dir) / "avatar-snapshots"
    pool = Path(args.avatar_dir)
    if not snap_dir.is_dir():
        print(f"no snapshots at {snap_dir}")
        return 0
    if not pool.is_dir():
        print(f"avatar pool not found at {pool}", file=sys.stderr)
        return 2

    # Index the pool by the CONTENT of each face crop.
    by_face: dict[str, Path] = {}
    for face in pool.glob("*-face.*"):
        full = None
        stem = face.name[: -len(face.suffix)]
        base = stem[: -len("-face")]
        for ext in (face.suffix, ".png", ".jpg", ".jpeg", ".webp"):
            cand = pool / f"{base}-full{ext}"
            if cand.is_file():
                full = cand
                break
        if full is not None:
            by_face[_digest(face)] = full
    print(f"pool: {len(by_face)} face/full pairs")

    snaps = [p for p in snap_dir.glob("*") if p.is_file()
             and not p.name.startswith(".") and "-full." not in p.name]
    matched = skipped = unmatched = 0
    for snap in sorted(snaps):
        target = snap.with_name(f"{snap.stem}-full{snap.suffix}")
        if target.is_file():
            skipped += 1
            continue
        src = by_face.get(_digest(snap))
        if src is None:
            unmatched += 1
            print(f"  no pool match: {snap.name}  (keeps the face; lightbox falls back)")
            continue
        matched += 1
        print(f"  {snap.name}  <-  {src.name}")
        if args.go:
            tmp = target.with_name(f".{target.name}.partial")
            shutil.copyfile(src, tmp)
            tmp.replace(target)

    print(f"\n{matched} to backfill, {skipped} already had one, {unmatched} unmatched")
    if not args.go:
        print("DRY RUN — nothing written. Re-run with --go to apply.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
