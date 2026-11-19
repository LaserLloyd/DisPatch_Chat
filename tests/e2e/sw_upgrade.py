#!/usr/bin/env python3
"""Service-worker upgrade path, warm reopen and offline start (first-load round).

    cd backend && uv run --with playwright python ../tests/e2e/sw_upgrade.py \\
        --base-static /var/tmp/x/frontend/static [--data /var/tmp/dp-sw-scratch]

1. A device runs the PREVIOUS release (``--base-static``, e.g. ``git archive <base>
   frontend/static``): signed in, its worker installed and in charge.
2. The server switches to this checkout's tree (a deploy). The next open must boot the
   NEW code (its main.js ?v=), and the new worker must take over with the new CACHE.
3. A warm reopen must load no startup module from the network.
4. Offline, the app shell must still start (from the worker's cache).

Exit 0 = every check passed; prints one line per check.
"""
from __future__ import annotations

import argparse
import re
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _scratch import REPO, Scratch, find_headless_shell  # noqa: E402
from first_load import INIT, Proxy  # noqa: E402

NEW_STATIC = REPO / "frontend" / "static"


def settle(page, timeout=120):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if page.evaluate("""async () => { const r = await navigator.serviceWorker.getRegistration('/');
                return !!(r && r.active && r.active.state === 'activated' && !r.installing && !r.waiting); }"""):
            return True
        page.wait_for_timeout(250)
    return False


def booted(page, timeout=60_000):
    page.wait_for_function("() => window.__bootDone === true || !!document.querySelector('.signin-overlay:not(.hidden), #signin:not(.hidden)')",
                           timeout=timeout)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-static", required=True)
    ap.add_argument("--data", default="/var/tmp/dp-sw-scratch")
    ap.add_argument("--port", type=int, default=18862)
    ap.add_argument("--proxy-port", type=int, default=18863)
    args = ap.parse_args()
    from playwright.sync_api import sync_playwright

    new_cache = re.search(r"const CACHE = '([^']+)'", (NEW_STATIC / "sw.js").read_text()).group(1)
    new_main = re.search(r'/static/js/main\.js\?v=(\d+)', (NEW_STATIC / "index.html").read_text()).group(1)
    fails = []

    def check(name, ok, detail=""):
        print(("PASS " if ok else "FAIL ") + name + (f" ({detail})" if detail else ""))
        if not ok:
            fails.append(name)

    s = Scratch(args.port, Path(args.data), fresh=True, fake_voice=False, static_dir=args.base_static)
    s.start()
    seed = s.seed()
    proxy = Proxy(args.proxy_port, args.port).start()
    base = f"http://127.0.0.1:{args.proxy_port}"
    udd = tempfile.mkdtemp(prefix="udd-sw-", dir="/var/tmp")
    try:
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(udd, executable_path=find_headless_shell(),
                                                       headless=True, service_workers="allow")
            ctx.add_init_script(INIT)
            o = seed["profiles"]["Owner"]
            assert ctx.request.post(base + "/api/auth/login",
                                    data={"profile_id": o["id"], "pin": o["pin"], "remember": True}).ok
            page = ctx.new_page()
            page.goto(base + "/")
            booted(page)
            check("old release installs its worker", settle(page))
            old_cache = page.evaluate("async () => (await caches.keys()).join(',')")
            page.close()

            s.restart(static_dir=None)                     # the deploy
            page = ctx.new_page()
            page.goto(base + "/")
            booted(page)
            ran = page.evaluate("() => [...document.scripts].map(s => s.src).find(u => u.includes('/js/main.js'))")
            check("the first open after the deploy runs the new main.js", f"main.js?v={new_main}" in (ran or ""), ran or "")
            keys = ""
            end = time.monotonic() + 90
            while time.monotonic() < end:
                keys = page.evaluate("async () => (await caches.keys()).join(',')")
                if keys == new_cache:
                    break
                page.wait_for_timeout(500)
            check("the new worker takes over", settle(page) and page.evaluate(
                "async () => (await navigator.serviceWorker.getRegistration('/')).active.scriptURL.endsWith('/static/sw.js')"))
            check("only the new CACHE remains", keys == new_cache, f"{old_cache} -> {keys}")
            page.close()

            proxy.reset_counts()
            page = ctx.new_page()
            page.goto(base + "/")
            booted(page)
            page.wait_for_timeout(1000)
            js = [r for r in proxy.requests if r.startswith("/static/js/")]
            check("a warm reopen loads no module from the network", not js, f"{len(js)} module requests")
            ctrl = page.evaluate("() => !!navigator.serviceWorker.controller")
            check("the warm page is controlled", ctrl)
            page.close()

            ctx.set_offline(True)
            page = ctx.new_page()
            try:
                page.goto(base + "/", timeout=30_000)
                page.wait_for_function("() => !!window.__perf && document.readyState === 'complete'", timeout=30_000)
                mods = page.evaluate("() => performance.getEntriesByType('resource').filter(e => e.name.includes('/static/js/')).length")
                check("offline: the shell and its modules load from the worker", mods > 50, f"{mods} modules")
                page.wait_for_timeout(3000)
                err = page.evaluate("() => document.getElementById('boot-veil') ? 'veil up' : 'veil gone'")
                print(f"info offline boot state: {err}")
            except Exception as exc:                    # noqa: BLE001
                check("offline: the shell loads", False, str(exc)[:120])
            ctx.close()
    finally:
        s.stop()
    print("ALL PASS" if not fails else f"{len(fails)} FAILED: {', '.join(fails)}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
