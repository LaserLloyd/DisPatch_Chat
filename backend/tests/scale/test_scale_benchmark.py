"""Synthetic-load benchmark: 20 profiles x 50,000 messages, p95 budgets (BUILD-PLAN-v2 §1.13.2).

Opt-in (marker ``scale``, deselected by default)::

    cd backend && TMPDIR=~/.cache/cfp/tmp/scale uv run pytest -m scale -q -s \\
        --basetemp ~/.cache/cfp/pytest/scale tests/scale

Environment (all optional):

* ``SCALE_PROFILES`` (20) and ``SCALE_MESSAGES`` (50000 per profile): the dataset size; a
  developer can run 5 x 10000 in a minute.
* ``SCALE_DIR`` (``~/.cache/cfp/scale``): where the dataset and the report live. Never /tmp.
* ``SCALE_REGEN=1``: regenerate the pristine dataset even when a matching one is cached.
  Generation is cached by default (``gen-<P>x<M>/dataset.json``); each run works on a
  reflink copy (``run-<label>/``), so the startup migration and every write start from the
  same pre-011 state as a live install.
* ``SCALE_LABEL`` (``run``): the name of the run in ``report.txt``.

Output: a p50/p95/p99 table per operation appended to ``<SCALE_DIR>/report.txt`` and printed.
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import statistics
import subprocess
import threading
import time
from pathlib import Path

import pytest

import scale_data

pytestmark = pytest.mark.scale

# Budgets in milliseconds (p95), measured on this box: CHUWI AuBox Ai365 (Ryzen AI 9 365,
# 30 GB, NVMe, btrfs). Raise one only with evidence (a report showing why).
BUDGET_MS = {
    "hello (first page)": 150,
    "thread list page": 50,
    "thread list page (all characters)": 50,
    "open thread (last 100)": 40,
    "before-cursor page": 40,
    "unread summary": 50,
    "search (3 terms)": 300,
    "search (common word)": 300,
    "send (persist + FTS + broadcast)": 60,
}
#: The nightly maintenance run (WP-SCALE-OPS) on the full dataset, and the longest write stall
#: it may cause a concurrent writer.
MAINTENANCE_BUDGET_S = 600
MAINTENANCE_STALL_MS = 250

PROFILES = int(os.environ.get("SCALE_PROFILES", "20"))
PER_PROFILE = int(os.environ.get("SCALE_MESSAGES", "50000"))
ROOT = Path(os.environ.get("SCALE_DIR", str(Path.home() / ".cache" / "cfp" / "scale")))
LABEL = os.environ.get("SCALE_LABEL", "run")
REPEAT = int(os.environ.get("SCALE_REPEAT", "40"))

#: The 011 indexes/tables a pristine (pre-011) dataset must not have.
_011_INDEXES = ("idx_threads_owner_list", "idx_threads_owner_bot_list", "idx_threads_owner_unread",
                "idx_files_owner_created", "idx_files_stored_name", "idx_image_jobs_message", "idx_sessions_device",
                "idx_activity_category_ts", "idx_task_events_ts", "idx_tasks_finished",
                "idx_job_events_created", "idx_job_feedback_created", "idx_usage_daily_day",
                "idx_transcript_seen_created", "idx_image_jobs_state_updated",
                "idx_sessions_last_seen", "idx_devices_last_seen", "idx_approvals_decided")
_011_TABLES = ("maintenance_runs", "media_refs", "media_expired", "storage_stats", "maintenance_state")


# --------------------------------------------------------------------------- #
# Pointing the app at a scratch data directory
# --------------------------------------------------------------------------- #

def _point_at(mp: pytest.MonkeyPatch, d: Path):
    from app import accounts, auth, config, main
    from app.database import Database
    for name, value in (("DATA_DIR", d), ("CONFIG_PATH", d / "config.yaml"),
                        ("MEDIA_DIR", d / "media"), ("FILES_DIR", d / "files"),
                        ("LOG_DIR", d / "logs"), ("BACKUP_DIR", d / "backups"),
                        ("AVATAR_DIR", d / "avatars")):
        mp.setattr(config, name, value)
    mp.setattr(main, "MEDIA_DIR", d / "media")
    mp.setattr(main, "FILES_DIR", d / "files")
    for name, fname in (("SECURITY_PATH", "security.yaml"), ("RECOVERY_PATH", "RECOVERY-CODE.txt"),
                        ("TRUSTED_PATH", "trusted-devices.yaml"), ("SETUP_TOKEN_PATH", "SETUP-TOKEN.txt")):
        mp.setattr(auth, name, d / fname)
    import dataclasses
    mp.setattr(main, "SETTINGS", dataclasses.replace(main.SETTINGS, backup_interval=0))   # no 1 GB VACUUM INTO mid-run
    mp.delenv("DISPATCH_ALLOW_OPEN_INSTALL", raising=False)
    from app import advisor
    if advisor._hooks is not None:
        mp.setattr(advisor._hooks, "data_dir", d)
    mp.setattr(advisor, "_ledger", None)
    config._invalidate_bots_cache()
    accounts.reset_for_tests()
    auth._cache = None
    auth._trusted = None
    auth._trusted_sig = None
    db = Database(d / "chats.db")
    mp.setattr(main, "db", db)
    main._thread_bot.clear()
    main._media_origins_reset()
    return db


def _client(main, addr=("203.0.113.70", 47000), base=None):
    from fastapi.testclient import TestClient
    c = TestClient(main.app, client=addr)
    if base is not None:
        c.portal = base.portal
    c.headers.update({"Origin": "http://testserver"})
    return c


def _generate(gen: Path, log) -> dict:
    """The pristine dataset: a claimed install with PROFILES profiles, then the bulk fill, in
    the pre-011 shape a live install has (none of 011's tables or indexes, auto_vacuum NONE)."""
    import sqlite3

    from app import accounts, config, main
    if gen.exists():
        shutil.rmtree(gen)
    gen.mkdir(parents=True)
    with pytest.MonkeyPatch.context() as mp:
        _point_at(mp, gen)
        c = _client(main, ("127.0.0.1", 50000))          # setup is allowed from this computer
        with c:
            r = c.post("/api/auth/setup", json={"name": scale_data.ADMIN_NAME,
                                                "password": scale_data.ADMIN_PASSWORD,
                                                "new_pin": scale_data.ADMIN_PIN})
            assert r.status_code == 200, r.text
            for i in range(1, PROFILES):
                accounts.create_profile(f"User{i:02d}", "user")
            direct = {"providers": [{"provider": "custom", "base_url": "http://p.invalid/v1",
                                     "model": "m"}]}
            for b in scale_data.BOTS:
                config.upsert_bot(config.Bot(id=b, name=b.replace("_", " ").title(),
                                             advisor=dict(direct)))
            pids = [p.id for p in sorted(accounts.list_profiles(), key=lambda p: p.created)]
            owner = accounts.get_owner()
            pids.sort(key=lambda p: p != owner.id)           # the protected Admin first
    con = sqlite3.connect(str(gen / "chats.db"), isolation_level=None)
    for name in _011_INDEXES:
        con.execute(f"DROP INDEX IF EXISTS {name}")
    for t in _011_TABLES:
        con.execute(f"DROP TABLE IF EXISTS {t}")
    with contextlib.suppress(sqlite3.OperationalError):
        con.execute("ALTER TABLE profiles DROP COLUMN archive_after_months")
    con.execute("DELETE FROM schema_migrations WHERE id = '011_scale'")
    con.execute("DELETE FROM threads")                      # the first-run guide threads
    con.execute("PRAGMA auto_vacuum = 0")
    con.execute("VACUUM")
    con.close()
    stats = scale_data.bulk_fill(gen / "chats.db", pids, PER_PROFILE, log=log)
    meta = {"profiles": PROFILES, "per_profile": PER_PROFILE, "profile_ids": pids,
            "generated": time.strftime("%Y-%m-%d %H:%M:%S"), **stats}
    (gen / "dataset.json").write_text(json.dumps(meta))
    return meta


def _dataset(log) -> tuple[Path, dict]:
    gen = ROOT / f"gen-{PROFILES}x{PER_PROFILE}"
    meta_file = gen / "dataset.json"
    if os.environ.get("SCALE_REGEN") != "1" and meta_file.is_file():
        meta = json.loads(meta_file.read_text())
        if meta.get("profiles") == PROFILES and meta.get("per_profile") == PER_PROFILE:
            log(f"reusing the cached dataset {gen} (generated {meta['generated']})")
            return gen, meta
    log(f"generating {PROFILES} x {PER_PROFILE} messages into {gen} ...")
    return gen, _generate(gen, log)


def _fresh_copy(gen: Path) -> Path:
    run = ROOT / f"run-{LABEL}"
    if run.exists():
        shutil.rmtree(run)
    # A reflink copy on btrfs (instant); a plain copy elsewhere.
    subprocess.run(["cp", "-a", "--reflink=auto", str(gen), str(run)], check=True)
    return run


# --------------------------------------------------------------------------- #
# Measuring
# --------------------------------------------------------------------------- #

def _pct(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    if not xs:
        return float("nan")
    k = (len(xs) - 1) * q
    lo, hi = int(k), min(len(xs) - 1, int(k) + 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


class Timings:
    def __init__(self):
        self.rows: dict[str, list[float]] = {}
        self.notes: list[str] = []

    def time(self, name: str, fn, n: int = REPEAT):
        out = []
        fn(0)                                           # one warm-up call (page cache, plans)
        for i in range(n):
            t0 = time.perf_counter()
            fn(i)
            out.append((time.perf_counter() - t0) * 1000)
        self.rows[name] = out

    def table(self) -> str:
        lines = [f"{'operation':40s} {'n':>4s} {'p50':>9s} {'p95':>9s} {'p99':>9s} {'budget':>8s}  ok"]
        for name, xs in self.rows.items():
            b = BUDGET_MS.get(name)
            p95 = _pct(xs, 0.95)
            ok = "-" if b is None else ("yes" if p95 < b else "NO")
            lines.append(f"{name:40s} {len(xs):4d} {statistics.median(xs):8.1f}m {p95:8.1f}m "
                         f"{_pct(xs, 0.99):8.1f}m {'' if b is None else b:>7}m  {ok}")
        return "\n".join(lines + self.notes)


def _report(text: str) -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    with (ROOT / "report.txt").open("a", encoding="utf-8") as fh:
        fh.write(text + "\n\n")
    print("\n" + text)


def test_scale_budgets(monkeypatch):
    from app import auth, main

    t_all = time.monotonic()
    gen, meta = _dataset(print)
    run = _fresh_copy(gen)
    db = _point_at(monkeypatch, run)
    pids: list[str] = meta["profile_ids"]
    long_threads: dict[str, list[str]] = meta["long_threads"]
    tm = Timings()

    t0 = time.monotonic()
    base = _client(main)
    base.__enter__()                                  # lifespan: migrations (011 here), startup
    startup_s = time.monotonic() - t0
    try:
        clients = []
        for i, pid in enumerate(pids):
            c = _client(main, (f"203.0.113.{80 + i % 100}", 48000 + i), base)
            c.cookies.set(main.COOKIE_NAME, auth.issue_session(profile_id=pid))
            clients.append(c)
        bot = scale_data.BOTS[0]

        def who(i):
            return clients[i % len(clients)]

        # -- hello (WS handshake + the hello frame) ------------------------- #
        def hello(i):
            with who(i).websocket_connect("/ws") as ws:
                frame = ws.receive_json()
                assert frame["type"] == "hello"
        tm.time("hello (first page)", hello)

        # -- thread lists --------------------------------------------------- #
        sizes = []

        def thread_list(i):
            r = who(i).get(f"/api/threads?bot_id={bot}&limit=50")
            assert r.status_code == 200, r.text
            sizes.append(len(r.json()["threads"]))
        tm.time("thread list page", thread_list)
        tm.notes.append(f"  thread list page returned {statistics.median(sizes):.0f} threads (median)")

        r = clients[0].get("/api/threads?limit=50")
        if r.status_code == 200:
            def thread_list_all(i):
                rr = who(i).get("/api/threads?limit=50")
                assert rr.status_code == 200, rr.text
            tm.time("thread list page (all characters)", thread_list_all)
            nxt = r.json().get("next_cursor")
            if nxt:
                def thread_list_next(i):
                    rr = who(0).get(f"/api/threads?limit=50&cursor={nxt}")
                    assert rr.status_code == 200, rr.text
                tm.time("thread list next page", thread_list_next)
            def archived(i):
                rr = who(i).get("/api/threads?archived=1&limit=50")
                assert rr.status_code == 200, rr.text
            tm.time("archived list page", archived)
        else:
            tm.notes.append(f"  /api/threads without bot_id: {r.status_code} (not supported here)")

        # -- messages ------------------------------------------------------- #
        first_pages: dict[int, str] = {}

        def open_thread(i):
            k = i % len(clients)
            tid = long_threads[pids[k]][i % 10]
            r = who(i).get(f"/api/threads/{tid}/messages?limit=100")
            assert r.status_code == 200, r.text
            msgs = r.json()["messages"]
            assert msgs and r.json()["has_more"]
            first_pages[i] = msgs[0]["id"]
        tm.time("open thread (last 100)", open_thread)

        def before(i):
            k = i % len(clients)
            tid = long_threads[pids[k]][i % 10]
            r = who(i).get(f"/api/threads/{tid}/messages?limit=100&before_id={first_pages[i]}")
            assert r.status_code == 200, r.text
            assert r.json()["messages"]
        tm.time("before-cursor page", before)

        # -- unread --------------------------------------------------------- #
        counts = []

        def unread(i):
            r = who(i).get("/api/unread")
            assert r.status_code == 200, r.text
            counts.append(len(r.json()["unread"]))
        tm.time("unread summary", unread)
        tm.notes.append(f"  unread summary listed {statistics.median(counts):.0f} threads (median)")

        # -- search --------------------------------------------------------- #
        hits = []

        def search3(i):
            q = " ".join(scale_data.SEARCH_TERMS)
            r = who(i).get(f"/api/search?q={q}")
            assert r.status_code == 200, r.text
            hits.append(len(r.json()["results"]))
        tm.time("search (3 terms)", search3)
        tm.notes.append(f"  search (3 terms) returned {statistics.median(hits):.0f} hits (median)")
        hits.clear()

        def search_common(i):
            r = who(i).get(f"/api/search?q={scale_data.COMMON_TERM}")
            assert r.status_code == 200, r.text
            hits.append(len(r.json()["results"]))
        tm.time("search (common word)", search_common)
        tm.notes.append(f"  search (common word) returned {statistics.median(hits):.0f} hits (median)")

        # -- send (persist + FTS + broadcast to an open socket; no model call) ------ #
        def send(i):
            k = i % len(clients)
            tid = long_threads[pids[k]][0]
            r = who(i).post(f"/api/threads/{tid}/messages",
                            json={"role": "user", "content": f"benchmark send {i} kalo misu"})
            assert r.status_code in (200, 201), r.text
        with clients[0].websocket_connect("/ws") as ws:
            ws.receive_json()
            tm.time("send (persist + FTS + broadcast)", send)

        # -- the nightly maintenance run (WP-SCALE-OPS) ---------------------- #
        maint_failed = _maintenance(tm, run)
    finally:
        base.__exit__(None, None, None)
        main._thread_bot.clear()

    size_mb = (run / "chats.db").stat().st_size / 1e6
    tm.notes.insert(0, f"  startup (lifespan incl. migrations) {startup_s:.1f} s; chats.db {size_mb:.0f} MB")
    head = (f"== {LABEL} @ {time.strftime('%Y-%m-%d %H:%M:%S')}: {PROFILES} profiles x {PER_PROFILE} "
            f"messages = {meta['messages']} messages, {meta['threads']} threads; "
            f"total {time.monotonic() - t_all:.0f} s")
    _report(head + "\n" + tm.table())
    failed = [n for n, xs in tm.rows.items() if n in BUDGET_MS and _pct(xs, 0.95) >= BUDGET_MS[n]]
    assert not failed and not maint_failed, f"over budget (p95): {failed} {maint_failed}"


def _maintenance(tm: Timings, run: Path) -> list[str]:
    """``maintenance.run_nightly`` on the full dataset, with a concurrent writer measuring its
    longest stall. Skipped (reported, not failed) while maintenance.py is still a stub."""
    import sqlite3

    from app import maintenance
    stop = threading.Event()
    stalls: list[float] = []

    def writer():
        con = sqlite3.connect(str(run / "chats.db"), timeout=30, isolation_level=None)
        con.execute("PRAGMA busy_timeout=30000")
        while not stop.is_set():
            t0 = time.perf_counter()
            con.execute("INSERT OR REPLACE INTO settings_meta (k, v) VALUES ('scale.writer', ?)",
                        (str(t0),))
            stalls.append((time.perf_counter() - t0) * 1000)
            time.sleep(0.02)
        con.close()
    th = threading.Thread(target=writer, daemon=True)
    th.start()
    t0 = time.monotonic()
    try:
        maintenance.run_nightly()
    except NotImplementedError:
        tm.notes.append("  maintenance.run_nightly: not implemented on this branch (WP-SCALE-OPS)")
        return []
    finally:
        stop.set()
        th.join(timeout=5)
    took = time.monotonic() - t0
    worst = max(stalls) if stalls else 0.0
    tm.notes.append(f"  maintenance.run_nightly: {took:.1f} s (budget {MAINTENANCE_BUDGET_S} s); "
                    f"worst writer stall {worst:.0f} ms (budget {MAINTENANCE_STALL_MS} ms)")
    return ([f"maintenance {took:.0f} s"] if took >= MAINTENANCE_BUDGET_S else []) + (
        [f"writer stall {worst:.0f} ms"] if worst >= MAINTENANCE_STALL_MS else [])
