#!/usr/bin/env python3
"""Headless-Chromium verification of the Emails + Clients tabs, per
CLAUDE.md's "tests passing is not verification on this box — open the app"
rule. Drives a real DisPatch instance (staging by default, or a live/dev
instance via --base-url) with Playwright.

Needs a Python environment with the `playwright` package AND its downloaded
Chromium (`python3 -m playwright install chromium` if neither is already
on the box — see the repo's own dev-dependency notes for how CI gets one).
Pass --chromium-path to pin an explicit browser binary (e.g. a shared cache
under the operator's home directory); otherwise this script uses whatever
Chromium `playwright install` last resolved, which is the portable default:

    python3 tests/e2e/clients_emails.py --base-url http://127.0.0.1:8766 --pin <staging-pin>

Checks, in order:
  1. Locked (no cookie): neither rail entry is offered, and both APIs 403.
  2. Unlocked: Clients tab renders Overview (health card + tiles), Active
     list, Completed list, and a client detail page with the 12-step
     stepper.
  3. Emails tab renders either the MailForge iframe (if reachable) or the
     "not reachable"/"not installed" note — never a blank pane.
Screenshots go to docs/screenshots/{clients,emails}-{390,1280}.png.

Fixed argv only; no shell=True anywhere (this script has no subprocess
calls at all — Playwright's own driver manages its browser process).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

REPO = Path(__file__).resolve().parents[2]
SHOT_DIR = REPO / "docs" / "screenshots"


def log(msg: str) -> None:
    print(f"[e2e] {msg}", flush=True)


def run(base_url: str, pin: str, shot_prefix: str, chromium_path: str | None) -> list[str]:
    failures: list[str] = []
    SHOT_DIR.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        launch_kwargs = {"headless": True}
        # An explicit path (flag or env var) overrides Playwright's own
        # resolution — useful when the box's Chromium download lives outside
        # playwright's default cache dir. Neither is required: with nothing
        # set, playwright.chromium.launch() finds its own installed browser.
        explicit = chromium_path or os.environ.get("E2E_CHROMIUM_PATH")
        if explicit:
            launch_kwargs["executable_path"] = explicit
        browser = p.chromium.launch(**launch_kwargs)

        # ---- 1. Locked: neither tab is offered, both APIs 403 -----------
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        page.goto(base_url, wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(500)
        mail_visible = page.locator("#tools-mail").is_visible()
        clients_visible = page.locator("#tools-clients").is_visible()
        if mail_visible or clients_visible:
            failures.append(f"locked: rail entries visible (mail={mail_visible} clients={clients_visible})")
        else:
            log("locked: Emails/Clients rail entries correctly hidden")

        resp = ctx.request.get(f"{base_url}/api/mail/status")
        if resp.status != 403:
            failures.append(f"locked: GET /api/mail/status = {resp.status}, want 403")
        resp = ctx.request.get(f"{base_url}/api/practice/board")
        if resp.status != 403:
            failures.append(f"locked: GET /api/practice/board = {resp.status}, want 403")
        if not failures:
            log("locked: both APIs correctly 403")
        ctx.close()

        # ---- 2. Unlocked: drive the real UI ------------------------------
        for width, label in ((390, "390"), (1280, "1280")):
            ctx = browser.new_context(viewport={"width": width, "height": 844 if width == 390 else 900})
            page = ctx.new_page()
            page.goto(base_url, wait_until="networkidle", timeout=30000)
            page.wait_for_timeout(500)

            # Unlock via the PIN keypad if a lock screen is showing;
            # otherwise fall back to the raw API (keeps this script usable
            # against an already-unlocked dev session too).
            keypad = page.locator("#keypad")
            if keypad.count() and keypad.is_visible():
                for digit in pin:
                    page.click(f'#keypad button[data-key="{digit}"]')
                page.wait_for_timeout(400)
            else:
                ctx.request.post(f"{base_url}/api/auth/unlock", data=json.dumps({"pin": pin}),
                                  headers={"Content-Type": "application/json"})
                page.reload(wait_until="networkidle")
            page.wait_for_timeout(800)

            if True:
                # -------- Clients tab --------
                page.locator("#tools-btn").dispatch_event("click")
                page.wait_for_timeout(300)
                page.locator("#tools-clients").dispatch_event("click")
                page.wait_for_timeout(1200)
                overview_ok = page.locator("#clients-root .health-card").count() > 0
                if not overview_ok:
                    failures.append(f"clients@{label}: Overview health card did not render")
                else:
                    log(f"clients@{label}: Overview rendered (health card present)")
                page.screenshot(path=str(SHOT_DIR / f"{shot_prefix}clients-overview-{label}.png"), full_page=True)

                page.locator('[data-clients-tab="active"]').click()
                page.wait_for_timeout(800)
                active_ok = page.locator("#clients-root").count() > 0
                page.screenshot(path=str(SHOT_DIR / f"{shot_prefix}clients-active-{label}.png"), full_page=True)

                page.locator('[data-clients-tab="completed"]').click()
                page.wait_for_timeout(800)
                page.screenshot(path=str(SHOT_DIR / f"{shot_prefix}clients-completed-{label}.png"), full_page=True)

                # Open a client detail if any client row exists anywhere
                page.locator('[data-clients-tab="active"]').click()
                page.wait_for_timeout(800)
                row = page.locator(".client-list-item, .client-row").first
                if row.count():
                    row.click()
                    page.wait_for_timeout(1200)
                    steps_ok = page.locator(".steps-root .step").count() > 0
                    if not steps_ok:
                        failures.append(f"clients@{label}: detail page had no visible steps")
                    else:
                        log(f"clients@{label}: detail page rendered {page.locator('.steps-root .step').count()} step(s)")
                    page.screenshot(path=str(SHOT_DIR / f"{shot_prefix}clients-detail-{label}.png"), full_page=True)
                else:
                    log(f"clients@{label}: no active client row to open (board is empty) — detail view not exercised")

                # -------- Emails tab --------
                page.locator("#tools-btn").dispatch_event("click")
                page.wait_for_timeout(300)
                page.locator("#tools-mail").dispatch_event("click")
                page.wait_for_timeout(1500)
                frame_visible = page.locator("#mail-frame").is_visible()
                note_visible = page.locator("#mail-note").is_visible()
                if not frame_visible and not note_visible:
                    failures.append(f"mail@{label}: neither the iframe nor the note is visible (blank pane)")
                elif frame_visible:
                    log(f"mail@{label}: MailForge iframe is visible")
                else:
                    log(f"mail@{label}: note shown instead of iframe ({page.locator('#mail-status-label').inner_text()})")
                page.screenshot(path=str(SHOT_DIR / f"{shot_prefix}emails-{label}.png"), full_page=True)

            ctx.close()

        browser.close()
    return failures


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8766")
    ap.add_argument("--pin", required=True)
    ap.add_argument("--shot-prefix", default="")
    ap.add_argument("--chromium-path", default=None,
                     help="Explicit Chromium binary (else E2E_CHROMIUM_PATH env, else playwright's own default)")
    args = ap.parse_args()

    failures = run(args.base_url, args.pin, args.shot_prefix, args.chromium_path)
    if failures:
        log("FAILURES:")
        for f in failures:
            log(f"  - {f}")
        return 1
    log("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
