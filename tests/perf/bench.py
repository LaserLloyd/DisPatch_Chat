#!/usr/bin/env python3
"""DisPatch performance numbers against a scratch instance (issue #25, G14).

Measures p50 / p95 / max (milliseconds) for:

  signin            POST /api/auth/login (a User's PIN; the real argon2id cost)
  hello             /ws handshake -> the first frame ("hello": rail bots + first thread page)
  threads_page      GET /api/threads (page one) and the next page by cursor
  thread_messages   GET /api/threads/<id>/messages on a thread with --messages rows
  roster_bots       GET /api/bots
  roster_manage     GET /api/characters/manage (Settings -> Characters)
  fanout_20         POST /api/inject (machine key) -> the message frame on ALL of 20 open
                    sockets of the thread's owner (time to the LAST socket)

on an install seeded with --threads threads (default 500) over --bots bots (default 50 =
the seeded roster plus household direct bots). Numbers go to --out (default
``<data>-perf.json``) with thresholds that are WARNINGS only: the exit code is 0 unless
the harness itself fails (2), so a slow CI runner never turns a pull request red.

Usage::

    cd backend && uv run python ../tests/perf/bench.py --port 18859 \
        --data ~/.cache/dispatch-e2e/perf --fresh --out ~/.cache/dispatch-e2e/perf.json

Standard library + ``websockets`` (already a backend dependency); run it with the
backend's interpreter.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import platform
import statistics
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "e2e"))
import _scratch
from _scratch import DEFAULT_DATA_ROOT, Scratch

#: p95 thresholds in ms. Warnings only.
THRESHOLDS = {
    "signin": 1500, "hello": 500, "threads_page": 300, "threads_page_next": 300,
    "thread_messages": 300, "roster_bots": 300, "roster_manage": 500, "fanout_20": 500,
}


def stats(samples: list[float]) -> dict:
    s = sorted(samples)
    if not s:
        return {"n": 0}
    p95 = s[min(len(s) - 1, max(0, round(0.95 * len(s)) - 1))]
    return {"n": len(s), "p50": round(statistics.median(s), 2), "p95": round(p95, 2),
            "max": round(s[-1], 2), "min": round(s[0], 2)}


def timed(fn, n: int) -> list[float]:
    out = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        out.append((time.perf_counter() - t0) * 1000)
    return out


def seed_volume(s: Scratch, *, threads: int, messages: int) -> dict:
    """Owner threads spread over every bot the Owner may chat with; one long thread."""
    owner = s.api("Owner")
    bots = owner.ok("GET", "/api/bots")
    bots = bots if isinstance(bots, list) else bots.get("bots", [])
    usable = [b["id"] for b in bots if not b.get("hidden")]
    have = owner.ok("GET", "/api/threads?limit=1")
    marker = s.data / ".perf-volume"
    if not marker.exists():
        made = 0
        i = 0
        while made < threads:
            bid = usable[i % len(usable)]
            i += 1
            code, _ = owner.call("POST", "/api/threads", {"bot_id": bid, "title": f"Perf thread {made:03d}"})
            if code == 200:
                made += 1
            elif i > threads * 3:
                raise RuntimeError(f"could not create threads (last {code})")
        marker.write_text(json.dumps({"threads": made}))
    page = owner.ok("GET", "/api/threads?limit=50")
    long_tid = page["threads"][0]["id"]
    key_api = s.machine()
    long_marker = s.data / ".perf-long-thread"
    if not long_marker.exists():
        for k in range(messages):
            key_api.ok("POST", "/api/inject", {"thread_id": long_tid, "role": "assistant",
                                               "content": f"Perf message {k:04d}: test data, nothing private."})
        long_marker.write_text(long_tid)
    long_tid = long_marker.read_text().strip()
    return {"bots_visible_to_owner": len(usable), "first_page": len(page["threads"]),
            "long_thread": long_tid, "has_more": bool(page.get("next_cursor")), "_probe": bool(have)}


async def ws_hello(base: str, cookie: str, n: int) -> list[float]:
    import websockets
    url = base.replace("http", "ws", 1) + "/ws"
    out = []
    for _ in range(n):
        t0 = time.perf_counter()
        async with websockets.connect(url, additional_headers={"Cookie": cookie, "Origin": base},
                                      max_size=2 ** 24, open_timeout=20) as ws:
            while True:
                frame = json.loads(await asyncio.wait_for(ws.recv(), 20))
                if frame.get("type") == "hello":
                    break
        out.append((time.perf_counter() - t0) * 1000)
    return out


async def fanout(base: str, cookie: str, key: str, thread_id: str, *, sockets: int, rounds: int) -> list[float]:
    import urllib.request

    import websockets
    url = base.replace("http", "ws", 1) + "/ws"
    conns = [await websockets.connect(url, additional_headers={"Cookie": cookie, "Origin": base},
                                      max_size=2 ** 24, open_timeout=20) for _ in range(sockets)]
    try:
        for ws in conns:                               # drain the hello
            while json.loads(await asyncio.wait_for(ws.recv(), 20)).get("type") != "hello":
                pass
        out = []
        for _ in range(rounds):
            mark = f"fanout-{uuid.uuid4().hex[:10]}"

            async def wait_one(ws, mark=mark):
                while True:
                    raw = await asyncio.wait_for(ws.recv(), 20)
                    if mark in raw:
                        return time.perf_counter()

            waiters = [asyncio.create_task(wait_one(ws)) for ws in conns]
            body = json.dumps({"thread_id": thread_id, "role": "assistant", "content": mark}).encode()
            req = urllib.request.Request(base + "/api/inject", data=body, method="POST",
                                         headers={"Content-Type": "application/json", "X-API-Key": key})
            t0 = time.perf_counter()
            await asyncio.to_thread(lambda r=req: urllib.request.urlopen(r, timeout=20).read())
            arrivals = await asyncio.gather(*waiters)
            out.append((max(arrivals) - t0) * 1000)
        return out
    finally:
        for ws in conns:
            await ws.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="DisPatch p50/p95 numbers on a scratch instance")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--data", default=str(DEFAULT_DATA_ROOT / "perf"))
    ap.add_argument("--fresh", action="store_true")
    ap.add_argument("--threads", type=int, default=500)
    ap.add_argument("--bots", type=int, default=50, help="total bots on the install (seeded roster + extras)")
    ap.add_argument("--messages", type=int, default=300, help="rows in the long thread")
    ap.add_argument("-n", type=int, default=20, help="samples per HTTP/WS metric")
    ap.add_argument("--rounds", type=int, default=15, help="fan-out rounds")
    ap.add_argument("--sockets", type=int, default=20)
    ap.add_argument("--out", help="perf JSON (default <data>-perf.json)")
    args = ap.parse_args(argv)
    port = args.port or _scratch.free_port()
    roster_now = 2 + len(_scratch.AGENT_BOTS) + 1        # atlas + jobboard + agents + Owner's Atlas copy
    extra = max(0, args.bots - roster_now)
    t_all = time.perf_counter()
    try:
        s = Scratch(port, args.data, fresh=args.fresh, fake_voice=False, extra_bots=extra).start()
    except Exception as e:
        print(f"the scratch instance did not start: {e}", file=sys.stderr)
        return 2
    try:
        seed = s.seed()
        vol = seed_volume(s, threads=args.threads, messages=args.messages)
        user, owner = s.api(), s.api("Owner")
        u = seed["profiles"]["User"]
        res: dict[str, list[float]] = {}
        res["signin"] = timed(lambda: user.ok("POST", "/api/auth/login",
                                              {"profile_id": u["id"], "pin": u["pin"]}), args.n)
        res["threads_page"] = timed(lambda: owner.ok("GET", "/api/threads?limit=50"), args.n)
        cur = owner.ok("GET", "/api/threads?limit=50").get("next_cursor")
        if cur:
            from urllib.parse import quote
            res["threads_page_next"] = timed(lambda: owner.ok("GET", f"/api/threads?limit=50&cursor={quote(cur)}"), args.n)
        res["thread_messages"] = timed(lambda: owner.ok("GET", f"/api/threads/{vol['long_thread']}/messages"), args.n)
        res["roster_bots"] = timed(lambda: owner.ok("GET", "/api/bots"), args.n)
        res["roster_manage"] = timed(lambda: owner.ok("GET", "/api/characters/manage"), args.n)
        cookie = owner.cookie_header()
        res["hello"] = asyncio.run(ws_hello(s.base, cookie, args.n))
        res["fanout_20"] = asyncio.run(fanout(s.base, cookie, seed["machine_key"], vol["long_thread"],
                                              sockets=args.sockets, rounds=args.rounds))
        bots_total = len(owner.ok("GET", "/api/characters/manage").get("mine", [])) + \
            len(owner.ok("GET", "/api/characters/manage").get("household", []))
    finally:
        s.stop()
        if s.stub is not None:
            s.stub.stop()
    metrics = {k: stats(v) for k, v in res.items()}
    warnings = [f"{k}: p95 {m['p95']} ms > {THRESHOLDS[k]} ms" for k, m in metrics.items()
                if k in THRESHOLDS and m.get("p95", 0) > THRESHOLDS[k]]
    doc = {
        "when": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "host": {"python": platform.python_version(), "machine": platform.machine(), "system": platform.system()},
        "install": {"threads_owner": args.threads, "bots_requested": args.bots, "characters_rows_owner": bots_total,
                    "bots_visible_to_owner": vol["bots_visible_to_owner"], "long_thread_messages": args.messages,
                    "fanout_sockets": args.sockets},
        "unit": "ms", "metrics": metrics, "thresholds_p95_ms": THRESHOLDS,
        "warnings": warnings, "note": "thresholds are warnings only; numbers from a scratch instance on loopback",
        "elapsed_s": round(time.perf_counter() - t_all, 1),
    }
    out = Path(args.out) if args.out else Path(args.data).parent / (Path(args.data).name + "-perf.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=2))
    for k, m in metrics.items():
        flag = "  WARN" if k in THRESHOLDS and m.get("p95", 0) > THRESHOLDS[k] else ""
        print(f"{k:<18} n={m['n']:<3} p50={m['p50']:>8} ms  p95={m['p95']:>8} ms  max={m['max']:>8} ms{flag}")
    print(f"-> {out}" + (f"  ({len(warnings)} warning(s))" if warnings else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
