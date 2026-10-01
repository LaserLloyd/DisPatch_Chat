#!/usr/bin/env python3
"""Headless-Chromium check of the 2026-09 feature wave, end to end.

The contribution guide's rule is that tests passing is not verification — you
open the app and look. This is the "look" for the wave that added regenerate
with alternates, edit-and-rerun, quote/reply, the per-thread model and thinking
override, the mood-driven header face, scene backdrops, drafts with a persisted
outbox, and thumbs feedback.

It exists because that wave was built as five parallel groups and merged by
hand, and three of the merges were in ``main.js``: the import block, the
thread-switch branch, and the composer's input handler. All three are
load-or-don't — a bad import specifier is a module-level throw and a blank page,
and every unit test in the repo still passes on a blank page, because none of
them load the real shell. Only a browser catches that.

Two conventions here are load-bearing rather than stylistic:

  * **Rows are addressed by ``data-id``, never by index.** The thread list sorts
    by ``updated_at``, so "the second row" is whichever thread was touched
    second-most recently. An index-based click reads the wrong conversation and
    reports a leak that is really the probe looking in the wrong place — which
    is exactly what happened the first time this was written.
  * **Picking a bot is part of every cold open.** DisPatch has never persisted
    the rail selection, so a reload lands on "Pick a bot to see your chats" with
    an empty thread list. That is the app behaving correctly, not a failure.

Point it at a SCRATCH instance, not the family app: it creates threads and
types into the composer.

    python3 tests/e2e/feature_wave.py --base-url http://127.0.0.1:8767

No screenshots, by design — see mobile_layout.py for why a capture of a running
instance is something the text scrubber cannot read.

Fixed argv only; no shell=True (there are no subprocess calls at all —
Playwright's driver manages its own browser process).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request

from playwright.sync_api import sync_playwright

DRAFT = "an unsent draft"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base-url", default="http://127.0.0.1:8767",
                    help="a scratch DisPatch, NOT the family app on :8765")
    ap.add_argument("--bot", default="quick",
                    help="roster id to drive; 'quick' is the repo's placeholder bot")
    ap.add_argument("--chromium-path", default=None,
                    help="overrides Playwright's own resolution (or E2E_CHROMIUM_PATH)")
    args = ap.parse_args()
    base = args.base_url.rstrip("/")

    if base.endswith(":8765"):
        print("refusing to drive :8765 — this probe writes threads and drafts")
        return 2

    def api(path, payload=None):
        req = urllib.request.Request(
            base + path,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={"Content-Type": "application/json"},
            method="POST" if payload is not None else "GET")
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read() or b"null")

    passed, failed = [], []

    def check(ok, label, detail=""):
        (passed if ok else failed).append(
            f"{'PASS' if ok else 'FAIL'}  {label}{(' — ' + detail) if detail else ''}")

    drafted = api("/api/threads", {"title": "Drafted thread", "bot_id": args.bot})["id"]
    other = api("/api/threads", {"title": "Other thread", "bot_id": args.bot})["id"]
    api("/api/inject", {"thread_id": drafted, "bot_id": args.bot,
                        "content": "Hello from the feature-wave probe."})

    with sync_playwright() as p:
        launch = {"headless": True}
        explicit = args.chromium_path or os.environ.get("E2E_CHROMIUM_PATH")
        if explicit:
            launch["executable_path"] = explicit
        browser = p.chromium.launch(**launch)
        page = browser.new_context(viewport={"width": 1280, "height": 900}).new_page()

        errors, bad_http = [], []
        page.on("pageerror", lambda e: errors.append("pageerror: " + str(e)))
        page.on("console", lambda m: errors.append("console.error: " + m.text)
                if m.type == "error" else None)
        page.on("response", lambda r: bad_http.append(f"{r.status} {r.url}")
                if r.status >= 400 and "/static/" in r.url else None)

        def open_thread(tid):
            page.locator("#bot-list .bot-btn").first.click()
            page.wait_for_timeout(1000)
            page.locator(f'#threads .thread-item[data-id="{tid}"]').click()
            page.wait_for_timeout(900)

        def composer():
            return page.evaluate("() => (document.getElementById('input')||{}).value || ''")

        page.goto(base + "/", wait_until="networkidle")
        page.wait_for_timeout(1500)

        # The shell came up. Everything below is meaningless without this.
        check(not errors, "boots with no JS errors", " | ".join(errors[:3]))
        check(not bad_http, "every static module resolved", " | ".join(bad_http[:3]))
        check(page.evaluate("() => { const v = document.getElementById('boot-veil');"
                            " return !v || getComputedStyle(v).opacity === '0' || v.hidden; }"),
              "boot veil cleared")

        served = page.evaluate(
            "() => performance.getEntriesByType('resource').map(e => e.name)"
            ".filter(n => /store\\.js|modelchip\\.js/.test(n))")
        check(any("store.js" in n for n in served), "store.js loaded")
        check(any("modelchip.js" in n for n in served), "modelchip.js loaded")

        open_thread(drafted)
        check(page.locator("#threads .thread-item").count() >= 2, "thread list rendered")
        check(page.locator("#input").is_visible(), "composer visible in a thread")
        check(page.locator("#ch-modelchip").count() > 0, "per-thread model chip present")
        check(page.locator("#mp-thinking-row").count() > 0, "thinking-level picker present")

        # Drafts, written through the merged input handler and read back after a
        # reload — the half that only IndexedDB can answer.
        page.locator("#input").click()
        page.locator("#input").type(DRAFT, delay=15)
        page.wait_for_timeout(1500)   # the save is debounced
        page.reload(wait_until="networkidle")
        page.wait_for_timeout(1800)
        open_thread(drafted)
        check(DRAFT in composer(), "draft survives a reload", repr(composer()[:40]))
        check(page.locator(f'.thread-item[data-id="{drafted}"] .thread-draft').count() > 0,
              "the drafted row shows a Draft label")

        # The merged thread-switch branch: it clears the composer for the thread
        # being opened and restores that thread's own draft, so the text must not
        # follow you out and must still be there when you come back.
        page.locator(f'#threads .thread-item[data-id="{other}"]').click()
        page.wait_for_timeout(900)
        check(DRAFT not in composer(), "a draft does not leak into another thread",
              repr(composer()[:40]))
        page.locator(f'#threads .thread-item[data-id="{drafted}"]').click()
        page.wait_for_timeout(900)
        check(DRAFT in composer(), "switching back restores the draft", repr(composer()[:40]))

        check(not errors, "no JS errors after driving the UI", " | ".join(errors[:3]))
        browser.close()

    for line in passed + failed:
        print("[wave] " + line)
    print(f"[wave] {'PASS' if not failed else 'FAIL'} — {len(passed)} passed, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
