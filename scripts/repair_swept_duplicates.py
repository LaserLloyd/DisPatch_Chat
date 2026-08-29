#!/usr/bin/env python3
"""Remove the duplicate replies the gap sweep posted before it was fixed.

The sweep decides "is this transcript item already in the chat?" by comparing
canonical text, and that comparison had drifted from what persisting actually
stores in two ways: reaction markers were stripped on the way in but not in the
key, and a media directive's rewritten path was bridged by hashing the file's
bytes — which stops bridging the moment the agent deletes its scratch file. Any
reply hitting either case read as missing on EVERY pass, so it was re-posted
every ten minutes, and each re-post found fewer of its source pictures alive:

    07:17  Five renders, all cooked and served hot ⚡  [[media:…]] ×5
    07:38  Five renders, all cooked and served hot ⚡  🖼️ ×3  [[media:…]] ×2
    07:58  Five renders, all cooked and served hot ⚡  🖼️ ×5
    08:08  …again.

The code no longer does this. This removes what it already did.

A row is deleted only when ALL of:
  * it was written by the sweep or a recovery pass (metadata.followup /
    .recovered) — never anything a person or a live agent turn produced;
  * an EARLIER row in the same thread says the same thing, comparing prose with
    every picture reference removed, so a copy whose images degraded to
    "(image unavailable)" still matches the intact original it duplicates;
  * it is not the only copy — the earliest row always survives, and a row that
    still carries a working picture the survivor lacks is kept.

Where the surviving copy is the DEGRADED one — the live paths missed the reply
and a sweep delivered it first, pictures already broken — and a later duplicate
still has them, the survivor is healed with that content before the duplicates
go. One copy, at the time it was actually said, with its pictures.

Dry run by default. --go takes a VACUUM INTO backup first.

    python3 scripts/repair_swept_duplicates.py                  # show
    python3 scripts/repair_swept_duplicates.py --go             # apply
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

DEFAULT_DB = Path.home() / ".local/share/local-chat/chats.db"

# Two deliveries of one text this close together are the same turn's redundant
# paths, never a person saying the same thing twice.
TWIN_WINDOW_S = 10

_MEDIA_DIRECTIVE_RE = re.compile(r"\[\[media:([^\]|]+)(\|[^\]]*)?\]\]")
_MEDIA_UNAVAILABLE_RE = re.compile(r"🖼️\s*\*\(image unavailable:.*?\)\*")
_REACT_RE = re.compile(r":react:[a-z0-9][a-z0-9._-]{0,47}:", re.IGNORECASE)


def presence_key(s: str) -> str:
    """The prose of a message, with every picture reference and marker gone.

    Deliberately blunt: the whole point is that an intact copy and a degraded
    copy of one reply must compare equal.
    """
    s = _MEDIA_DIRECTIVE_RE.sub(" ", s or "")
    s = _MEDIA_UNAVAILABLE_RE.sub(" ", s)
    s = _REACT_RE.sub(" ", s)
    return " ".join(s.split())


MEDIA_DIR = DEFAULT_DB.parent / "media"


def pictures(s: str, media_dir: Path | None = None) -> set[str]:
    """The pictures a message carries, identified BY CONTENT.

    Not by URL: each re-import ingested the same file again under a fresh uuid,
    so two rows holding the identical picture name it differently. Comparing
    URLs made a duplicate look like it held something the original lacked, and
    the rows it was supposed to remove survived. Unreadable files fall back to
    their path, which at worst keeps a row that could have gone.
    """
    d = media_dir or MEDIA_DIR
    out = set()
    for m in _MEDIA_DIRECTIVE_RE.finditer(s or ""):
        ref = m.group(1).strip()
        try:
            data = (d / ref[len("/media/"):]).read_bytes() if ref.startswith("/media/") \
                else Path(ref).expanduser().read_bytes()
            out.add(hashlib.sha256(data).hexdigest())
        except OSError:
            out.add(ref)
    return out


def swept(metadata: str | None) -> bool:
    try:
        meta = json.loads(metadata or "{}")
    except ValueError:
        return False
    return isinstance(meta, dict) and bool(meta.get("followup") or meta.get("recovered"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--media-dir", type=Path, default=None,
                    help="served media store (default: <db dir>/media)")
    ap.add_argument("--go", action="store_true", help="apply (default: dry run)")
    args = ap.parse_args()

    if not args.db.is_file():
        print(f"no database at {args.db}", file=sys.stderr)
        return 2

    global MEDIA_DIR
    MEDIA_DIR = args.media_dir or (args.db.parent / "media")

    con = sqlite3.connect(str(args.db))
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT id, thread_id, content, metadata, created_at FROM messages "
        "WHERE role = 'assistant' ORDER BY thread_id, created_at, rowid").fetchall()

    groups: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for r in rows:
        key = presence_key(r["content"])
        if key:                                         # media-only: never guess
            groups.setdefault((r["thread_id"], key), []).append(r)

    # Byte-identical twins from the SAME turn. The funnel's own invariant is
    # that one text delivered twice inside a turn is one message — these exist
    # because the key drifted (or, for a few, because an earlier run of this
    # script healed a survivor without removing the donor it copied from).
    # Narrow on purpose: identical bytes, same thread, seconds apart. Unlike
    # the rule below it does not require the row to be sweep-written, because
    # a heal artifact and a reconciler copy carry no such mark.
    twins: list[sqlite3.Row] = []
    for i in range(1, len(rows)):
        a, b = rows[i - 1], rows[i]
        if a["thread_id"] != b["thread_id"] or a["content"] != b["content"]:
            continue
        try:
            gap = (datetime.fromisoformat(b["created_at"])
                   - datetime.fromisoformat(a["created_at"])).total_seconds()
        except (TypeError, ValueError):
            continue
        if 0 <= gap <= TWIN_WINDOW_S:
            twins.append(b)
    twin_ids = {r["id"] for r in twins}

    doomed: list[sqlite3.Row] = []
    heals: list[tuple[sqlite3.Row, sqlite3.Row]] = []   # (survivor, donor)
    for group in groups.values():
        first, rest = group[0], group[1:]
        richest = max(group, key=lambda r: len(pictures(r["content"])))
        # The survivor is the EARLIEST row, always — its identity never moves.
        # What moves is the content it is compared by: after a heal it holds
        # the donor's pictures, so the donor (and every other swept copy) no
        # longer "has a picture the survivor lacks" and can go. Rebinding
        # `first` to the donor here instead — an earlier version did — made the
        # id check below protect the DONOR, which then outlived its own heal:
        # one healed copy plus one identical donor copy, in every healed thread.
        survivor_content = first["content"]
        if richest is not first and pictures(richest["content"]) - pictures(first["content"]):
            heals.append((first, richest))
            survivor_content = richest["content"]       # what the survivor BECOMES
        surv_pics = pictures(survivor_content)
        for r in rest:                                  # rest never holds the survivor
            if not swept(r["metadata"]):
                continue                                # a person / live turn
            if pictures(r["content"]) - surv_pics:
                continue                                # holds a picture the survivor lacks
            doomed.append(r)

    # Merge, keeping order and not double-listing a row both rules caught.
    seen_ids = {r["id"] for r in doomed}
    doomed += [r for r in twins if r["id"] not in seen_ids]

    if not doomed and not heals:
        print("nothing to repair — no duplicates found")
        return 0

    by_thread: dict[str, int] = {}
    degraded = 0
    for r in doomed:
        by_thread[r["thread_id"]] = by_thread.get(r["thread_id"], 0) + 1
        if "image unavailable" in (r["content"] or ""):
            degraded += 1
    print(f"{len(doomed)} duplicate row(s) across {len(by_thread)} thread(s); "
          f"{degraded} of them have degraded pictures, "
          f"{len(twin_ids & {r['id'] for r in doomed})} are same-turn byte-identical twins")
    for r in doomed[:20]:
        print(f"  {r['created_at'][:19]}  {r['thread_id'][:18]:18}  "
              f"{' '.join((r['content'] or '').split())[:64]}")
    if len(doomed) > 20:
        print(f"  … and {len(doomed) - 20} more")
    for survivor, donor in heals:
        print(f"heal {survivor['created_at'][:19]}  "
              f"+{len(pictures(donor['content']))} picture(s)  "
              f"{' '.join((survivor['content'] or '').split())[:50]}")

    if not args.go:
        print("\ndry run — re-run with --go to apply")
        return 0

    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    backup = args.db.parent / "backups" / f"chats-before-dedupe-repair-{stamp}.db"
    backup.parent.mkdir(parents=True, exist_ok=True)
    con.execute("VACUUM INTO ?", (str(backup),))
    print(f"backed up to {backup}")

    for survivor, donor in heals:
        con.execute("UPDATE messages SET content = ? WHERE id = ?",
                    (donor["content"], survivor["id"]))
    con.executemany("DELETE FROM messages WHERE id = ?", [(r["id"],) for r in doomed])
    con.commit()
    print(f"healed {len(heals)} row(s), deleted {len(doomed)} row(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
