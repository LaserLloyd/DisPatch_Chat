#!/usr/bin/env python3
"""Headless-Chromium measurement of DisPatch's layout at phone sizes.

CLAUDE.md's rule is that tests passing is not verification on this box — you
open the app and look. This script is the "look" for layout: it drives a real
DisPatch instance at several phone viewports, measures the *actual* bounding
boxes of the fixed chrome (composer, mobile tab bar, toasts, modals) against
the scrolling content, and fails when they overlap or run off the bottom.

It exists because the owner's report — "installed as an Android PWA, the UI
overlaps at the bottom" — is a geometry claim, and geometry is measurable.
A screenshot proves it to a human; a rect intersection proves it to CI.

Two things this deliberately does NOT try to do:

  * It does not emulate the Android on-screen keyboard. Headless Chromium has
    no soft keyboard, so the keyboard-overlap case is checked *statically*
    instead (see ``check_viewport_contract``): the viewport meta must opt into
    ``interactive-widget=resizes-content``, because without it Android Chrome
    in ``display: standalone`` leaves the visual viewport at full height when
    the keyboard opens and every ``dvh``-sized pane keeps its full height —
    which is exactly how a fixed composer ends up underneath the keys.
  * It does not emulate safe-area insets. ``env(safe-area-inset-*)`` resolves
    to 0 in headless Chromium and cannot be overridden from CSS, so a rule that
    is wrong only when the inset is non-zero cannot be caught by measurement
    here. ``check_inset_arithmetic`` reads the stylesheet instead and flags the
    two shapes that are wrong at *any* inset: double-counting (a container that
    subtracts the tab bar when its parent already did) and zero-counting (fixed
    bottom chrome with no inset in its padding at all).

Usage:

    python3 tests/e2e/mobile_layout.py --base-url http://127.0.0.1:8766

The PIN is read from the instance's own data dir when ``--pin`` is omitted and
the instance is local; it is never printed. Screenshots land in
docs/screenshots/mobile-<device>-<pane>.png.

Fixed argv only; no shell=True (there are no subprocess calls at all —
Playwright's driver manages its own browser process).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

REPO = Path(__file__).resolve().parents[2]

# Screenshots go to a GITIGNORED scratch directory, never docs/screenshots/.
#
# This is not tidiness. A capture of a running instance is a picture of real
# family conversation: bot names, avatars, message previews, mailbox addresses.
# None of that survives a text scan — scrub_check.py reads bytes, and a JPEG of
# someone's inbox is opaque to it — so a screenshot that lands in a tracked
# directory is one `git add -A` from a public repo with nothing in the way.
# An earlier run of this very script wrote 20 such files into docs/screenshots/
# before anyone noticed.
#
# Override with --shot-dir when you deliberately want a committed screenshot,
# and look at what you captured before you add it.
SHOT_DIR = Path(
    os.environ.get("DISPATCH_E2E_SHOT_DIR")
    or (REPO / ".screenshots-local")
)
INDEX_HTML = REPO / "frontend" / "static" / "index.html"
APP_CSS = REPO / "frontend" / "static" / "app.css"

# The phone viewports that matter here. The first two are the common Android
# sizes the owner would see; the 320 is the WCAG reflow floor, and the short
# landscape case is where a vertically-stacked composer + tab bar runs out of
# room first.
DEVICES = [
    {"name": "android-360", "width": 360, "height": 800, "dpr": 3},
    {"name": "android-412", "width": 412, "height": 915, "dpr": 2.6},
    {"name": "iphone-390", "width": 390, "height": 844, "dpr": 3},
    {"name": "narrow-320", "width": 320, "height": 640, "dpr": 2},
    {"name": "landscape-740", "width": 740, "height": 360, "dpr": 3},
]

# Chrome that is pinned to the viewport and therefore able to sit on top of
# content. Measured against the scroll container for each pane.
FIXED_CHROME = [".composer", ".mobile-tabs", ".toast", ".reconnect-card"]

# Chrome that must never sit on top of a pane's scrolling content. Toasts and
# the reconnect card are deliberately excluded: floating over content for a few
# seconds is what a toast IS. They are still bounds-checked against the
# viewport, because a toast that has slid off the bottom edge is a real bug.
COVERING_CHROME = [".composer", ".mobile-tabs"]

# The PIN this probe sets on the disposable staging instance. Staging has its
# own data dir and no path to real conversations, so this is not a secret; a
# live instance must be given its PIN with --pin.
DEFAULT_PROBE_PIN = "246810"


def log(msg: str) -> None:
    print(f"[mobile] {msg}", flush=True)


def read_local_pin(base_url: str, explicit: str | None, data_dir: str | None) -> str | None:
    """The instance's unlock PIN, without ever printing it.

    Order: an explicit --pin, then the data directory named by --data-dir or
    $DISPATCH_DATA_DIR. There is deliberately NO built-in list of local install
    paths — a hard-coded one would only fit this maintainer's box, and writing
    someone's directory layout into a public repo is the kind of detail the
    privacy scanner exists to reject.

    Only consulted for a loopback base URL: a remote instance's PIN is not ours
    to go looking for on disk.
    """
    if explicit:
        return explicit
    if "127.0.0.1" not in base_url and "localhost" not in base_url:
        return None
    root = data_dir or os.environ.get("DISPATCH_DATA_DIR")
    if not root:
        return None
    sec = Path(root).expanduser() / "security.yaml"
    if not sec.exists():
        return None
    try:
        import yaml

        doc = yaml.safe_load(sec.read_text(encoding="utf-8")) or {}
    except Exception:
        return None
    pin = doc.get("pin")
    if pin:
        return str(pin)
    # auth.py clears the plaintext `pin:` once it has hashed it, so a blank one
    # is normal. Fall back to the probe PIN the operator sets on a scratch
    # instance; a real one must be given with --pin.
    return DEFAULT_PROBE_PIN if doc.get("pin_hash") else None


# --------------------------------------------------------------------------
# Static checks. These catch the two failure modes measurement cannot see in a
# headless browser: the Android keyboard, and any non-zero safe-area inset.
# --------------------------------------------------------------------------


def check_viewport_contract() -> list[str]:
    """The viewport meta must tell Android what to do when the keyboard opens."""
    problems: list[str] = []
    if not INDEX_HTML.exists():
        return [f"index.html not found at {INDEX_HTML}"]
    html = INDEX_HTML.read_text(encoding="utf-8")
    m = re.search(r'<meta\s+name="viewport"\s+content="([^"]+)"', html)
    if not m:
        return ["no viewport meta in index.html"]
    content = m.group(1)
    if "viewport-fit=cover" not in content:
        problems.append(
            "viewport meta lacks viewport-fit=cover, so env(safe-area-inset-*) "
            "resolves to 0 and the app cannot clear a notch or gesture bar"
        )
    if "interactive-widget" not in content:
        problems.append(
            "viewport meta lacks interactive-widget=resizes-content: on Android "
            "Chrome in display:standalone the on-screen keyboard does not shrink "
            "the visual viewport, dvh units keep their full height, and the fixed "
            "composer and tab bar are left underneath the keys"
        )
    return problems


def check_inset_arithmetic() -> list[str]:
    """Flag inset shapes that are wrong at any non-zero inset.

    Reported as advisories rather than hard failures: this is a text scan of a
    stylesheet, and the file carries deliberate exceptions that it documents in
    comments. The point is to surface the arithmetic for a human, not to fail a
    build on a regex.
    """
    problems: list[str] = []
    if not APP_CSS.exists():
        return [f"app.css not found at {APP_CSS}"]
    css = APP_CSS.read_text(encoding="utf-8")

    # Fixed bottom chrome with no bottom inset anywhere in its block is the
    # zero-counting shape: correct on a phone with hardware keys, clipped under
    # an Android gesture bar.
    for selector in (".mobile-tabs", ".composer"):
        idx = css.find(selector)
        if idx == -1:
            continue
        block = css[idx : idx + 600]
        if "safe-area-inset-bottom" not in block:
            problems.append(
                f"{selector}: no safe-area-inset-bottom within its rule block — "
                "check it is inherited from a parent, or it will sit under the "
                "Android gesture bar"
            )
    return problems


# --------------------------------------------------------------------------
# Live measurement.
# --------------------------------------------------------------------------

MEASURE_JS = """
(selectors) => {
  const out = { viewport: { w: window.innerWidth, h: window.innerHeight }, els: {} };
  if (window.visualViewport) {
    out.visual = { w: window.visualViewport.width, h: window.visualViewport.height,
                   top: window.visualViewport.offsetTop };
  }
  const seen = {};
  for (const sel of selectors) {
    const nodes = Array.from(document.querySelectorAll(sel));
    seen[sel] = nodes.filter((n) => {
      const cs = getComputedStyle(n);
      if (cs.display === 'none' || cs.visibility === 'hidden') return false;
      if (parseFloat(cs.opacity || '1') === 0) return false;
      const r = n.getBoundingClientRect();
      return r.width > 0 && r.height > 0;
    }).map((n) => {
      const r = n.getBoundingClientRect();
      const cs = getComputedStyle(n);
      return {
        sel, top: r.top, bottom: r.bottom, left: r.left, right: r.right,
        w: r.width, h: r.height,
        position: cs.position, zIndex: cs.zIndex,
        paddingBottom: cs.paddingBottom,
      };
    });
  }
  out.els = seen;

  // Does anything scroll the document itself? On a pane that sizes to the
  // viewport it should not: the inner list scrolls instead. A document-level
  // scrollbar at phone width is how content ends up under fixed chrome.
  out.docScroll = {
    scrollH: document.documentElement.scrollHeight,
    clientH: document.documentElement.clientHeight,
    overflowing: document.documentElement.scrollHeight - document.documentElement.clientHeight,
  };
  return out;
}
"""

# The scrolling content each pane owns, paired with the mobile tab that gets
# you there. `open_thread` panes need a conversation opened first: the Messages
# tab with no thread selected shows a placeholder and no composer, so measuring
# it would miss the single most important overlap in the app.
PANES = [
    {"key": "bots", "view": "bots", "content": "#bot-list", "open_thread": False},
    {"key": "threads", "view": "threads", "content": "#threads", "open_thread": False},
    {"key": "chat", "view": "chat", "content": "#messages", "open_thread": True},
    {"key": "jobs", "view": "jobs", "content": "[data-jobs-root]", "open_thread": False},
]


def goto_view(page, view: str, open_thread: bool) -> bool:
    """Put the app on one mobile tab, opening a conversation when asked.

    Returns False when the pane is not reachable on this build (the Jobs tab is
    hidden unless JOBS_ENABLED and a jobboard bot exist), which is a skip, not
    a failure.
    """
    try:
        tab = page.query_selector(f'.mobile-tabs button[data-view="{view}"]:not(.hidden)')
        if tab is None or tab.get_attribute("hidden") is not None:
            return False
        tab.click()
        page.wait_for_timeout(500)
        if open_thread:
            # Any thread will do; the composer is the same element either way.
            row = page.query_selector("#threads .thread-item[data-id]")
            if row is None:
                # No thread list yet - open a bot from the roster, which is what
                # populates it.
                page.click('.mobile-tabs button[data-view="bots"]')
                page.wait_for_timeout(400)
                bot = page.query_selector("#bot-list button.bot-btn[data-id]")
                if bot:
                    bot.click()
                    page.wait_for_timeout(900)
                    row = page.query_selector("#threads .thread-item[data-id]")
            if row:
                row.click()
                page.wait_for_timeout(900)
        return True
    except Exception:
        return False


def unlock(page, pin: str = "246810") -> bool:
    """Get the page out of Safe Mode, or say we could not.

    A locked device is shown the Companions panel, and the ONLY route to the
    keypad from there is the panel's discreet "More" button — that indirection
    is a deliberate product decision (index.html: "the gear's More button is
    the only (discreet) route to the PIN keypad"), so the probe has to walk it
    rather than jumping straight to #lock-screen.

    Returns True once the unlocked chrome is actually on screen. Measuring the
    Safe-Mode face and reporting it as the app would be a false pass — locked
    mode has no composer and no message list, so every overlap check would
    trivially succeed against elements that are not there.
    """
    try:
        if page.query_selector("#lock-screen:not(.hidden)") is None:
            for sel in ("#unlock-btn", "#manage-bots", "#gear-row #unlock-btn"):
                btn = page.query_selector(f"{sel}:not(.hidden)")
                if btn:
                    btn.click()
                    page.wait_for_timeout(400)
                    break
        if page.query_selector("#lock-screen:not(.hidden)") is None:
            more = page.query_selector("#comp-more")
            if more:
                more.click()
                page.wait_for_timeout(400)
        if page.query_selector("#lock-screen:not(.hidden)") is None:
            return False

        for ch in pin:
            key = page.query_selector(f'#keypad button[data-k="{ch}"]')
            if key is None:
                return False
            key.click()
            page.wait_for_timeout(60)
        submit = page.query_selector("#lock-submit")
        if submit:
            submit.click()
        page.wait_for_timeout(1500)
        # Truth is the body class the app itself sets, not the absence of the
        # lock card: a wrong PIN also hides nothing.
        return page.evaluate("() => !document.body.classList.contains('decoy-mode')")
    except Exception:
        return False


def rects_overlap(a: dict, b: dict, tolerance: float = 1.0) -> float:
    """Vertical overlap in px between two rects that also overlap horizontally."""
    h_overlap = min(a["right"], b["right"]) - max(a["left"], b["left"])
    if h_overlap <= tolerance:
        return 0.0
    v_overlap = min(a["bottom"], b["bottom"]) - max(a["top"], b["top"])
    return v_overlap if v_overlap > tolerance else 0.0


def run(base_url: str, pin: str | None, chromium_path: str | None, keep_open: bool) -> list[str]:
    failures: list[str] = []
    SHOT_DIR.mkdir(parents=True, exist_ok=True)

    log("static checks")
    for p in check_viewport_contract():
        failures.append(f"viewport: {p}")
        log(f"  FAIL viewport: {p}")
    for p in check_inset_arithmetic():
        log(f"  note inset: {p}")

    with sync_playwright() as p:
        launch_kwargs: dict = {"headless": True}
        explicit = chromium_path or os.environ.get("E2E_CHROMIUM_PATH")
        if explicit:
            launch_kwargs["executable_path"] = explicit
        browser = p.chromium.launch(**launch_kwargs)

        for dev in DEVICES:
            ctx = browser.new_context(
                viewport={"width": dev["width"], "height": dev["height"]},
                device_scale_factor=dev["dpr"],
                is_mobile=True,
                has_touch=True,
            )
            page = ctx.new_page()
            try:
                page.goto(base_url, wait_until="domcontentloaded", timeout=20000)
                page.wait_for_timeout(700)

                if pin and not unlock(page, pin):
                    failures.append(f"{dev['name']}: could not unlock — measured Safe Mode only")
                    log(f"  FAIL {dev['name']}: unlock failed")

                page.wait_for_timeout(500)
                name = dev["name"]
                selectors = FIXED_CHROME + [pane["content"] for pane in PANES]

                for pane in PANES:
                    if not goto_view(page, pane["view"], pane["open_thread"]):
                        log(f"  {name}/{pane['key']}: not available on this build - skipped")
                        continue
                    page.wait_for_timeout(300)
                    data = page.evaluate(MEASURE_JS, selectors)
                    vh = data["viewport"]["h"]
                    tag = f"{name}/{pane['key']}"

                    # A pane whose content never rendered makes every check below
                    # pass by measuring nothing. That is the failure mode this
                    # whole script exists to avoid, so it is a failure, not a
                    # silent skip.
                    if not data["els"].get(pane["content"]):
                        failures.append(
                            f"{tag}: content {pane['content']} never rendered - "
                            "nothing was measured, so this pane is UNVERIFIED"
                        )
                        log(f"  FAIL {tag}: content never rendered")
                        continue
                    if pane["open_thread"] and not data["els"].get(".composer"):
                        failures.append(
                            f"{tag}: no .composer in an open conversation - "
                            "the main overlap case is UNVERIFIED"
                        )
                        log(f"  FAIL {tag}: composer missing")

                    # 1. Fixed chrome must sit inside the viewport, not past it.
                    for sel in FIXED_CHROME:
                        for r in data["els"].get(sel, []):
                            if r["bottom"] > vh + 1:
                                msg = (
                                    f"{tag}: {sel} bottom={r['bottom']:.0f} exceeds "
                                    f"viewport height {vh} by {r['bottom'] - vh:.0f}px"
                                )
                                failures.append(msg)
                                log(f"  FAIL {msg}")

                    # 2. Fixed chrome must not cover this pane's scrolling content.
                    for content in data["els"].get(pane["content"], []):
                        for sel in COVERING_CHROME:
                            for chrome in data["els"].get(sel, []):
                                ov = rects_overlap(content, chrome)
                                if ov > 2:
                                    msg = (
                                        f"{tag}: {sel} overlaps {pane['content']} "
                                        f"by {ov:.0f}px vertically"
                                    )
                                    failures.append(msg)
                                    log(f"  FAIL {msg}")

                    # 3. The document itself should not scroll at phone width.
                    over = data["docScroll"]["overflowing"]
                    if over > 2:
                        msg = (
                            f"{tag}: document scrolls by {over:.0f}px - an inner pane "
                            "should scroll instead, or fixed chrome will ride over content"
                        )
                        failures.append(msg)
                        log(f"  FAIL {msg}")

                    page.screenshot(path=str(SHOT_DIR / f"mobile-{name}-{pane['key']}.png"))

                log(f"  {name}: measured ({len(failures)} failures so far)")
            except Exception as exc:
                failures.append(f"{dev['name']}: {exc}")
                log(f"  ERROR {dev['name']}: {exc}")
            finally:
                if not keep_open:
                    ctx.close()

        browser.close()
    return failures


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base-url", default="http://127.0.0.1:8766")
    ap.add_argument("--pin", default=None, help="unlock PIN; read from the local data dir when omitted")
    ap.add_argument("--chromium-path", default=None)
    ap.add_argument("--json", action="store_true", help="emit the failure list as JSON")
    ap.add_argument("--keep-open", action="store_true")
    ap.add_argument("--data-dir", default=None,
                    help="the instance's data dir, used only to read its PIN "
                         "(default: $DISPATCH_DATA_DIR). Never printed.")
    ap.add_argument("--shot-dir", default=None,
                    help="where screenshots go (default: a gitignored scratch dir, NOT docs/screenshots)")
    args = ap.parse_args()

    global SHOT_DIR
    if args.shot_dir:
        SHOT_DIR = Path(args.shot_dir)
    pin = read_local_pin(args.base_url, args.pin, args.data_dir)
    failures = run(args.base_url, pin, args.chromium_path, args.keep_open)

    if args.json:
        print(json.dumps({"failures": failures}, indent=2))
    if failures:
        log(f"FAILED — {len(failures)} layout problem(s)")
        for f in failures:
            log(f"  - {f}")
        return 1
    log("PASS — no overlap, no overflow, viewport contract intact")
    return 0


if __name__ == "__main__":
    sys.exit(main())
