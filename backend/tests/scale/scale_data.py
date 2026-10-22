"""The synthetic install the scale benchmark runs against (BUILD-PLAN-v2 §1.13.2).

``build(root, profiles, per_profile)`` makes a complete DisPatch data directory under ``root``:
a claimed install (one protected Admin plus Users), four household direct-API bots, and
``profiles x per_profile`` messages across about ``per_profile / 10`` threads per profile,
weighted like real use (a handful of very long threads, many short ones), with realistic
content lengths, 3% ``media_url`` rows, ``metadata`` on most rows, owner stamps, some pinned,
archived and unread threads, and the FTS index populated (by the app's own triggers).

It is generated once and reused: ``root/dataset.json`` records the parameters, and a later run
with the same parameters skips generation (``SCALE_REGEN=1`` forces it). Never under /tmp.
"""
from __future__ import annotations

import json
import random
import sqlite3
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

#: Neutral fixture names only.
BOTS = ("pal_a", "pal_b", "pal_c", "pal_d")
BOT_WEIGHTS = (50, 25, 15, 10)
ADMIN_NAME = "Admin0"
ADMIN_PASSWORD = "scale-admin-pass-1"  # scrub-ok: throwaway fixture credential for a scratch install
ADMIN_PIN = "2468"

#: Common English words (searching ``the`` is the "very common word" case) plus a synthetic
#: vocabulary drawn with a Zipf-like weight, so mid-frequency terms exist for the 3-term search.
COMMON = ("the", "and", "to", "of", "a", "in", "is", "it", "you", "that", "for", "on", "with",
          "this", "be", "are", "was", "have", "not", "but", "what", "can", "we", "your", "just")
SYLL = ("ka", "lo", "mi", "ra", "te", "su", "no", "ve", "di", "ga", "pe", "zu", "ri", "ma", "so",
        "ten", "bar", "lin", "dor", "mek", "sal", "vin", "tor", "pul", "rex")
#: The three-term search (mid-frequency synthetic words; present in the vocabulary).
SEARCH_TERMS = ("kalo", "misu", "ravedi")
COMMON_TERM = "the"


def _vocab(rng: random.Random, n: int = 6000) -> list[str]:
    words = set(SEARCH_TERMS)
    while len(words) < n:
        words.add("".join(rng.choice(SYLL) for _ in range(rng.choice((2, 2, 3, 3, 4)))))
    return list(SEARCH_TERMS) + sorted(words - set(SEARCH_TERMS))


class _Text:
    def __init__(self, rng: random.Random):
        self.rng = rng
        vocab = _vocab(rng)
        # Zipf-ish: weight 1/(rank+8); the search terms sit at ranks ~40-60 (mid frequency).
        self.words = vocab[3:43] + list(SEARCH_TERMS) + vocab[43:]
        self.weights = [1.0 / (i + 8) for i in range(len(self.words))]

    def sentence(self, n_words: int) -> str:
        rng = self.rng
        out = []
        syn = rng.choices(self.words, weights=self.weights, k=n_words)
        for w in syn:
            out.append(rng.choice(COMMON) if rng.random() < 0.35 else w)
        return " ".join(out)

    def message(self, role: str) -> str:
        rng = self.rng
        if role == "user":
            n = max(3, int(rng.lognormvariate(3.0, 0.8)))          # ~20 words, long tail
        else:
            n = max(5, int(rng.lognormvariate(4.3, 0.9)))          # ~75 words, long tail
        n = min(n, 1500)
        return self.sentence(n).capitalize() + "."


def _thread_sizes(rng: random.Random, total: int) -> list[int]:
    """About total/10 threads: 10 long ones carry ~40% of the messages, the rest are short."""
    n_threads = max(12, total // 10)
    long_n = 10
    long_share = int(total * 0.4)
    sizes = [long_share // long_n] * long_n
    rest = total - sum(sizes)
    short_n = n_threads - long_n
    short = [1 + int(rng.expovariate(1 / 5)) for _ in range(short_n)]
    scale = rest / max(1, sum(short))
    short = [max(1, round(s * scale)) for s in short]
    diff = rest - sum(short)
    i = 0
    while diff != 0 and short:
        j = i % len(short)
        if diff > 0:
            short[j] += 1
            diff -= 1
        elif short[j] > 1:
            short[j] -= 1
            diff += 1
        i += 1
    return sizes + short


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def bulk_fill(db_path: Path, profile_ids: list[str], per_profile: int, *, seed: int = 20261023,
              log=print) -> dict:
    """Insert the threads and messages straight into chats.db (the app's own triggers fill FTS)."""
    rng = random.Random(seed)
    text = _Text(rng)
    con = sqlite3.connect(str(db_path), isolation_level=None)
    con.execute("PRAGMA synchronous=OFF")
    con.execute("PRAGMA foreign_keys=OFF")
    now = datetime(2026, 10, 22, tzinfo=UTC)
    span = timedelta(days=3 * 365)
    stats = {"threads": 0, "messages": 0, "long_threads": {}}
    t0 = time.monotonic()
    for pi, pid in enumerate(profile_ids):
        sizes = _thread_sizes(rng, per_profile)
        rng.shuffle(sizes)
        threads, msgs = [], []
        long_ids = []
        for size in sizes:
            tid = str(uuid.uuid4())
            bot = rng.choices(BOTS, weights=BOT_WEIGHTS)[0]
            start = now - span * rng.random() ** 0.7
            # A long thread runs for months; a short one for minutes to days.
            dur = timedelta(days=rng.uniform(60, 400)) if size > 200 else timedelta(
                minutes=rng.uniform(1, 60 * 24 * 3))
            end = min(now, start + dur)
            step = (end - start) / max(1, size)
            ts = start
            for k in range(size):
                ts = ts + step * rng.uniform(0.2, 1.8) if k else ts
                role = "user" if k % 2 == 0 else "assistant"
                r = rng.random()
                meta = None
                if r < 0.10:
                    meta = '{"sub": true}'
                elif r < 0.45:
                    meta = '{"model": "m-1", "elapsed": 1.2}'
                media = f"/media/{uuid.uuid4().hex}.png" if rng.random() < 0.03 else None
                msgs.append((str(uuid.uuid4()), tid, role, text.message(role), media, _iso(ts), meta))
            last = ts
            read = rng.random()
            if read < 0.85:
                last_read = _iso(last + timedelta(seconds=5))
            elif read < 0.95:
                last_read = _iso(start + (last - start) / 2)
            else:
                last_read = None
            threads.append((tid, bot, text.sentence(4).title(), _iso(start), _iso(last), "idle",
                            1 if rng.random() < 0.05 else 0, 0, last_read, pid))
            long_ids.append((size, tid))
        # A few pinned threads per profile (pinned and not archived).
        for idx in rng.sample(range(len(threads)), 5):
            t = list(threads[idx])
            t[6], t[7] = 0, 1
            threads[idx] = tuple(t)
        con.execute("BEGIN")
        con.executemany(
            "INSERT INTO threads (id, bot_id, title, created_at, updated_at, status, is_archived, "
            "is_pinned, last_read_at, owner_profile_id) VALUES (?,?,?,?,?,?,?,?,?,?)", threads)
        con.executemany(
            "INSERT INTO messages (id, thread_id, role, content, media_url, created_at, metadata) "
            "VALUES (?,?,?,?,?,?,?)", msgs)
        con.execute("COMMIT")
        stats["threads"] += len(threads)
        stats["messages"] += len(msgs)
        stats["long_threads"][pid] = [tid for _n, tid in sorted(long_ids, reverse=True)[:10]]
        log(f"  profile {pi + 1}/{len(profile_ids)}: {len(threads)} threads, {len(msgs)} messages "
            f"({time.monotonic() - t0:.0f} s)")
    con.execute("PRAGMA foreign_keys=ON")
    # No ANALYZE: a live install has no sqlite_stat1 until the maintenance task's PRAGMA
    # optimize runs, so the plans measured here are the ones it gets without statistics.
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.close()
    stats["seconds"] = round(time.monotonic() - t0, 1)
    return stats
