#!/usr/bin/env python3
"""First-load timing harness: cold, after-an-update and warm loads, phone and desktop.

Starts a scratch DisPatch from this checkout (``_scratch.Scratch``), seeds a realistic
amount of test data (household bots, a few hundred daily threads with messages, a few
pictures), puts a throttling TCP proxy in front of it and drives headless Chromium
through the proxy. The proxy (not CDP network emulation) does the shaping because
CDP emulation does not reach a service worker's own fetches, and the worker is half
of what this measures. It also counts requests and bytes on the wire.

    cd backend && uv run --with playwright python ../tests/e2e/first_load.py \\
        --data /var/tmp/dp-perf-scratch [--port 18860] [--runs 3] [--json out.json]

Profiles:
  phone   390x844, CPU x4, 150 ms RTT, 1.6 Mbit/s down / 750 kbit/s up (Lighthouse "slow 4G")
  desktop 1440x900, no throttling (loopback)

Scenarios (one persistent browser profile per run, so the service worker and the HTTP
cache behave as on a real device):
  cold    empty profile, no worker
  warm    the same profile, reopened
  update  the server switched to a copy of the static tree whose sw.js CACHE and
          main.js ?v= are bumped (what every deploy does), then reopened

Caveat stated in docs/design/first-load.md: the proxy speaks HTTP/1.1 (6 connections
per host); a phone on the Tailscale Serve origin gets HTTP/2. Request COUNT therefore
costs more here than on the phone; bytes cost the same.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _scratch import REPO, Scratch, find_headless_shell  # noqa: E402

STATIC = REPO / "frontend" / "static"

PROFILES = {
    "phone": {"viewport": {"width": 390, "height": 844}, "mobile": True, "cpu": 4,
              "rtt_ms": 150, "down_bps": 1_600_000 // 8, "up_bps": 750_000 // 8},
    "desktop": {"viewport": {"width": 1440, "height": 900}, "mobile": False, "cpu": 1,
                "rtt_ms": 0, "down_bps": 0, "up_bps": 0},
}

# --------------------------------------------------------------------------- #
# Throttling proxy
# --------------------------------------------------------------------------- #


class _Bucket:
    """A shared byte-rate limiter (one per direction: the link is one pipe)."""

    def __init__(self, rate: int):
        self.rate = rate
        self.t = time.monotonic()

    async def take(self, n: int) -> None:
        if not self.rate:
            return
        now = time.monotonic()
        self.t = max(self.t, now) + n / self.rate
        delay = self.t - now
        if delay > 0:
            await asyncio.sleep(delay)


class Proxy:
    def __init__(self, listen: int, target: int):
        self.listen, self.target = listen, target
        self.reset_shape(0, 0, 0)
        self.reset_counts()
        self.loop: asyncio.AbstractEventLoop | None = None

    def reset_shape(self, rtt_ms: int, down: int, up: int) -> None:
        self.half = rtt_ms / 2000
        self.down, self.up = _Bucket(down), _Bucket(up)

    def reset_counts(self) -> None:
        self.requests: list[str] = []
        self.bytes_down = 0
        self.conns = 0

    async def _pipe(self, r, w, bucket, upstream: bool):
        """Bandwidth is a shared serialisation delay; latency is a DELAY LINE (each chunk
        is delivered `half` an RTT after it was sent, pipelined), not a per-chunk pause."""
        q: asyncio.Queue = asyncio.Queue()

        async def deliver():
            try:
                while True:
                    at, data = await q.get()
                    if data is None:
                        break
                    wait = at - time.monotonic()
                    if wait > 0:
                        await asyncio.sleep(wait)
                    w.write(data)
                    await w.drain()
            except (ConnectionError, OSError):
                pass
            finally:
                try:
                    w.close()
                except Exception:
                    pass

        out = asyncio.ensure_future(deliver())
        try:
            while True:
                data = await r.read(16384)
                if not data:
                    break
                if upstream:
                    for m in re.finditer(rb"(?:^|\r\n)(GET|POST|PUT|PATCH|DELETE|HEAD) (\S+) HTTP/1\.1", data):
                        self.requests.append(m.group(2).decode("latin-1"))
                else:
                    self.bytes_down += len(data)
                await bucket.take(len(data))
                q.put_nowait((time.monotonic() + self.half, data))
        except (ConnectionError, OSError):
            pass
        finally:
            q.put_nowait((0, None))
            await out

    async def _client(self, cr, cw):
        self.conns += 1
        if self.half:
            await asyncio.sleep(self.half * 2)   # the TCP handshake round trip
        try:
            sr, sw = await asyncio.open_connection("127.0.0.1", self.target)
        except OSError:
            cw.close()
            return
        await asyncio.gather(self._pipe(cr, sw, self.up, True), self._pipe(sr, cw, self.down, False))

    def start(self) -> Proxy:
        ready = threading.Event()

        def run():
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            srv = self.loop.run_until_complete(asyncio.start_server(self._client, "127.0.0.1", self.listen))
            ready.set()
            self.loop.run_until_complete(srv.serve_forever())

        threading.Thread(target=run, daemon=True).start()
        ready.wait(10)
        return self

# --------------------------------------------------------------------------- #
# Seeding (test data only; shapes, not anybody's content)
# --------------------------------------------------------------------------- #

LOREM = ("The quick brown fox jumps over the lazy dog. " * 6).strip()
MD_BLOCK = ("Here is a summary:\n\n- first point about the plan\n- second point, with **bold**\n"
            "- third point with `code`\n\n```python\nfor i in range(3):\n    print(i)\n```\n\n" + LOREM)


def seed_threads(s: Scratch, *, dates: int, per_thread: int) -> int:
    marker = s.data / ".perf-seeded"
    if marker.exists():
        return int(marker.read_text())
    m = s.machine()
    bots = [b["id"] for b in s.api("Owner").ok("GET", "/api/bots")["bots"]]
    pic = s.data / "perf-pic.png"
    shutil.copy(STATIC / "icon-512.png", pic)
    n = 0
    for bi, bot in enumerate(bots):
        for d in range(dates):
            day = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400 * (d + 1)))
            for k in range(per_thread):
                role = "user" if k % 2 == 0 else "assistant"
                body = MD_BLOCK if k % 3 == 1 else f"Message {k} for day {day}. {LOREM[:120]}"
                if k == 1 and d % 10 == 0:
                    body += f"\n\n[[media:{pic}|a picture]]"
                code, _ = m.call("POST", "/api/inject", {"bot_id": bot, "date": day, "role": role,
                                                         "content": body})
                if code != 200 and k == 0:
                    break
            n += 1
    marker.write_text(str(n))
    return n

# --------------------------------------------------------------------------- #
# Browser
# --------------------------------------------------------------------------- #

INIT = r"""
(() => {
  const T = window.__perf = { ws_open: null, ws_first: null, ws_hello: null, longtasks: [], boot: null };
  const W = window.WebSocket;
  window.WebSocket = function (...a) {
    const s = new W(...a);
    s.addEventListener('open', () => { if (T.ws_open == null) T.ws_open = performance.now(); });
    s.addEventListener('message', (ev) => {
      if (T.ws_first == null) T.ws_first = performance.now();
      if (T.ws_hello == null && typeof ev.data === 'string' && ev.data.includes('"hello"')) T.ws_hello = performance.now();
    });
    return s;
  };
  window.WebSocket.prototype = W.prototype;
  Object.assign(window.WebSocket, { CONNECTING: 0, OPEN: 1, CLOSING: 2, CLOSED: 3 });
  try {
    new PerformanceObserver((l) => { for (const e of l.getEntries()) T.longtasks.push(e.duration); })
      .observe({ type: 'longtask', buffered: true });
  } catch (e) {}
  const iv = setInterval(() => { if (window.__bootDone && T.boot == null) { T.boot = performance.now(); clearInterval(iv); } }, 10);
})();
"""

COLLECT = r"""
() => {
  const T = window.__perf || {};
  const paint = {};
  for (const e of performance.getEntriesByType('paint')) paint[e.name] = e.startTime;
  const nav = performance.getEntriesByType('navigation')[0] || {};
  const res = performance.getEntriesByType('resource');
  return {
    fcp: paint['first-contentful-paint'] ?? null,
    boot: T.boot, ws_open: T.ws_open, ws_first: T.ws_first, ws_hello: T.ws_hello,
    dcl: nav.domContentLoadedEventEnd ?? null,
    longtasks_ms: Math.round((T.longtasks || []).reduce((a, b) => a + b, 0)),
    longtask_max: Math.round(Math.max(0, ...(T.longtasks || []))),
    resources: res.length,
    controlled: !!navigator.serviceWorker && !!navigator.serviceWorker.controller,
  };
}
"""


def one_load(ctx, proxy: Proxy, base: str, prof: dict) -> dict:
    page = ctx.new_page()
    cdp = ctx.new_cdp_session(page)
    if prof["cpu"] > 1:
        cdp.send("Emulation.setCPUThrottlingRate", {"rate": prof["cpu"]})
    proxy.reset_counts()
    t0 = time.monotonic()
    page.goto(base + "/", wait_until="commit")
    page.wait_for_function("() => window.__perf && window.__perf.boot != null", timeout=120_000)
    try:
        page.wait_for_function("() => window.__perf.ws_hello != null", timeout=60_000)
    except Exception:
        pass
    out = page.evaluate(COLLECT)
    # First data a person asks for: tap the first bot, wait for its chat list.
    t1 = time.monotonic()
    page.evaluate("() => document.querySelector('#bot-list .bot-btn').click()")
    page.wait_for_function(
        "() => document.querySelectorAll('#threads .thread-item, #threads [data-thread-id], #threads li').length > 0",
        timeout=60_000)
    out["open_bot_ms"] = round((time.monotonic() - t1) * 1000)
    out["wall_boot_ms"] = round((t1 - t0) * 1000)
    # Let the worker finish installing (on a phone that is the background precache),
    # so the next load is the one a person gets when they open the app again later.
    t2 = time.monotonic()
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        settled = page.evaluate(
            """async () => { if (!navigator.serviceWorker) return true;
                 const r = await navigator.serviceWorker.getRegistration('/');
                 return !!(r && r.active && r.active.state === 'activated'
                           && !r.installing && !r.waiting); }""")
        if settled:
            break
        page.wait_for_timeout(250)
    out["sw_settle_ms"] = round((time.monotonic() - t2) * 1000)
    page.wait_for_timeout(500)
    out["requests"] = len(proxy.requests)
    out["kbytes"] = round(proxy.bytes_down / 1024)
    out["conns"] = proxy.conns
    out["req_list"] = list(proxy.requests)
    page.close()
    return out


def make_update_tree(dst: Path) -> Path:
    """A copy of the static tree as the next deploy would leave it: CACHE+1, main.js ?v+1."""
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(STATIC, dst, symlinks=True,
                    ignore=shutil.ignore_patterns("avatars"))
    sw = (dst / "sw.js").read_text()
    sw = re.sub(r"const CACHE = 'local-chat-v(\d+)'", lambda m: f"const CACHE = 'local-chat-v{int(m.group(1)) + 1}'", sw, count=1)
    html = (dst / "index.html").read_text()
    # The generated version table, as startup-graph.mjs would rewrite it.
    sw = re.sub(r"('/static/js/main\.js': ')(\d+)'", lambda m: f"{m.group(1)}{int(m.group(2)) + 1}'", sw)
    (dst / "sw.js").write_text(sw)
    html = re.sub(r'(src="/static/js/main\.js\?v=)(\d+)', lambda m: m.group(1) + str(int(m.group(2)) + 1), html)
    (dst / "index.html").write_text(html)
    (dst / "js" / "main.js").write_text((dst / "js" / "main.js").read_text() + "\n// perf: next deploy\n")
    return dst


def run(args) -> dict:
    from playwright.sync_api import sync_playwright

    data = Path(args.data)
    s = Scratch(args.port, data, fresh=args.fresh, fake_voice=False, extra_bots=args.bots)
    s.start()
    s.seed()
    n = seed_threads(s, dates=args.dates, per_thread=args.per_thread)
    print(f"seeded threads: {n}", file=sys.stderr)
    proxy = Proxy(args.proxy_port, args.port).start()
    base = f"http://127.0.0.1:{args.proxy_port}"
    owner = s.seed()["profiles"]["Owner"]
    upd = make_update_tree(Path(args.work) / "static-next")
    results: dict = {}
    exe = find_headless_shell()
    try:
        with sync_playwright() as p:
            for pname in args.profiles:
                prof = PROFILES[pname]
                for r in range(args.runs):
                    if s.static_dir:
                        s.restart(static_dir=None)
                    udd = Path(tempfile.mkdtemp(prefix="udd-", dir=args.work))
                    ctx = p.chromium.launch_persistent_context(
                        str(udd), executable_path=exe, headless=True,
                        viewport=prof["viewport"], is_mobile=prof["mobile"], has_touch=prof["mobile"],
                        device_scale_factor=3 if prof["mobile"] else 1, service_workers="allow")
                    ctx.add_init_script(INIT)
                    # A remembered sign-in, as on the owner's phone (no network shaping yet).
                    proxy.reset_shape(0, 0, 0)
                    body = {"profile_id": owner["id"], "pin": owner["pin"], "remember": True}
                    resp = ctx.request.post(base + "/api/auth/login", data=body)
                    assert resp.ok, resp.text()
                    proxy.reset_shape(prof["rtt_ms"], prof["down_bps"], prof["up_bps"])
                    for scen in ("cold", "warm", "update"):
                        if scen == "update":
                            proxy.reset_shape(0, 0, 0)
                            s.restart(static_dir=upd)
                            proxy.reset_shape(prof["rtt_ms"], prof["down_bps"], prof["up_bps"])
                        res = one_load(ctx, proxy, base, prof)
                        results.setdefault(pname, {}).setdefault(scen, []).append(res)
                        print(pname, scen, r, {k: (round(v) if isinstance(v, float) else v)
                                               for k, v in res.items() if k != "req_list"}, file=sys.stderr)
                    ctx.close()
                    shutil.rmtree(udd, ignore_errors=True)
    finally:
        s.stop()
    return results


def summarise(results: dict) -> dict:
    out: dict = {}
    keys = ("sw_settle_ms", "fcp", "boot", "ws_open", "ws_hello", "open_bot_ms", "requests", "kbytes", "longtasks_ms", "longtask_max")
    for pname, scens in results.items():
        for scen, runs in scens.items():
            row = {}
            for k in keys:
                vals = [r[k] for r in runs if r.get(k) is not None]
                row[k] = round(statistics.median(vals)) if vals else None
            out[f"{pname}/{scen}"] = row
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="/var/tmp/dp-perf-scratch")
    ap.add_argument("--work", default="/var/tmp/dp-perf-work")
    ap.add_argument("--port", type=int, default=18860)
    ap.add_argument("--proxy-port", type=int, default=18861)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--bots", type=int, default=8)
    ap.add_argument("--dates", type=int, default=30)
    ap.add_argument("--per-thread", type=int, default=6)
    ap.add_argument("--profiles", nargs="+", default=["phone", "desktop"])
    ap.add_argument("--fresh", action="store_true")
    ap.add_argument("--json")
    args = ap.parse_args()
    Path(args.work).mkdir(parents=True, exist_ok=True)
    results = run(args)
    summary = summarise(results)
    print(json.dumps(summary, indent=1))
    if args.json:
        Path(args.json).write_text(json.dumps({"summary": summary, "runs": results}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
