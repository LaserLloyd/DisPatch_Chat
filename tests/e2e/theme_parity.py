#!/usr/bin/env python3
"""Screenshot every palette on a fixed set of surfaces, for before/after diffs.

The move onto the shared theme package (ui-theme.css / ui-theme.js) promises
that the core palettes look the SAME afterwards, apart from two documented
deltas (Purple adopts the contract Purple; Paper's --text-muted is slightly
darker). Token-level parity is pinned by
frontend/tests/fixtures/palette-parity-2026-09.json; this script is the other
half — it opens the real app and captures the same surfaces, the same way, so
two runs (one on the BEFORE tree, one on the AFTER tree) can be diffed pixel by
pixel.

Everything that could make two runs differ for reasons that are not the theme
is pinned: DPR 1, reduced motion, animations disabled at capture, caret hidden,
a fixed wall clock (so "2 days ago" and time-of-day scene backdrops agree),
`dispatch-lang=en`, and the palette stamped into localStorage before any page
script runs. Point both runs at IDENTICAL seeded data dirs (copies of one
template) or the diff measures the data, not the theme.

Surfaces (each at 1280x800 and 390x844):

    chat       an open thread scrolled to its TOP: assistant markdown (em,
               strong, quote, mark, inline code, callouts) and a user message
    chat-end   the same thread at its natural bottom: table, fenced code,
               task list, the composer
    theme      Settings -> Theme (the palette gallery)
    device     Settings -> Device
    dashboard  Settings -> Health (the host dashboard) — its numbers are LIVE
               host stats, so expect small diffs in the figures themselves
    lock       the PIN keypad
    minimal    the chat surface with minimal avatars (dispatch-avatar-style)
    dialog     a themed ui-dialog (the in-app confirm/prompt), built with the
               app's own markup and classes so it is painted by app.css
    cmdk       the command palette (Ctrl+K)

A surface that cannot be reached is logged as SKIP with the reason and the run
continues; the exit code is 1 only if nothing at all was captured.

    python3 tests/e2e/theme_parity.py --port 8812 --out /tmp/shots/before

Refuses port 8765 (the family app): it unlocks, opens Settings and types into
the command palette. Screenshots of a running instance show its data, so the
default output directory is gitignored — see mobile_layout.py for why that
matters. Fixed argv only; no subprocess calls at all.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

REPO = Path(__file__).resolve().parents[2]
DEFAULT_OUT = REPO / ".screenshots-local" / "theme-parity"
PALETTES = ["glacier", "midnight-gold", "forest", "paper", "daylight", "purple"]
# The four opt-in themes the shared theme package adds; pass --palettes all.
OPT_IN = ["electric-yellow", "laserlloyd", "laserlloyd-light", "night-red"]
SURFACES = ["chat", "chat-end", "theme", "device", "dashboard", "lock", "minimal", "dialog", "cmdk"]
VIEWPORTS = [(1280, 800), (390, 844)]
# 2026-09-23 03:00 UTC. Anything after the seeded data works; it only has to be
# the SAME in both runs.
FIXED_CLOCK_MS = 1790132400000
DEFAULT_PIN = "246810"


def log(msg: str) -> None:
    print(f"[parity] {msg}", flush=True)


def init_script(palette: str, extra: dict[str, str]) -> str:
    store = {"dispatch-palette": palette, "dispatch-lang": "en", **extra}
    return (
        "(() => { try { const s = " + json.dumps(store) + ";"
        " for (const k in s) localStorage.setItem(k, s[k]); } catch (e) {} })();"
    )


def wait_ready(page) -> None:
    page.wait_for_function(
        "() => { const v = document.getElementById('boot-veil');"
        " return !v || v.classList.contains('gone') && getComputedStyle(v).opacity === '0'; }",
        timeout=20000,
    )
    page.evaluate("() => document.fonts.ready.then(() => true)")
    page.wait_for_timeout(400)


def unlock_via_api(page, base: str, pin: str) -> bool:
    r = page.request.post(base + "/api/auth/unlock", data={"pin": pin})
    return r.ok


def pick_thread(page, thread_id: str, mobile: bool) -> bool:
    if mobile:
        tab = page.query_selector('.mobile-tabs button[data-view="bots"]')
        if tab:
            tab.click()
            page.wait_for_timeout(300)
    bot = page.query_selector('#bot-list .bot-btn[data-id="quick"]') or \
        page.query_selector("#bot-list .bot-btn")
    if bot is None:
        return False
    bot.click()
    page.wait_for_timeout(800)
    row = page.query_selector(f'#threads .thread-item[data-id="{thread_id}"]') if thread_id \
        else page.query_selector("#threads .thread-item[data-id]")
    if row is None:
        return False
    row.click()
    page.wait_for_timeout(1200)
    return page.query_selector("#messages .msg, #messages .message, #messages [data-id]") is not None


def open_settings(page, tab: str) -> bool:
    btn = page.query_selector("#manage-bots")
    if btn is None:
        return False
    btn.click()
    page.wait_for_timeout(500)
    t = page.query_selector(f"#stab-{tab}")
    if t is None or not t.is_visible():
        return False
    t.click()
    page.wait_for_timeout(1200 if tab == "health" else 600)
    return page.query_selector(f"#spane-{tab}:not(.hidden)") is not None


def open_lock(page) -> bool:
    for sel in ("#unlock-btn", "#manage-bots"):
        btn = page.query_selector(f"{sel}:not(.hidden)")
        if btn and btn.is_visible():
            btn.click()
            page.wait_for_timeout(400)
            break
    if page.query_selector("#lock-screen:not(.hidden)") is None:
        more = page.query_selector("#comp-more")
        if more and more.is_visible():
            more.click()
            page.wait_for_timeout(400)
    return page.query_selector("#lock-screen:not(.hidden)") is not None


DIALOG_JS = """
() => {
  const dlg = document.createElement('dialog');
  dlg.className = 'ui-dialog';
  dlg.innerHTML = '<div class="ui-dialog-body">'
    + '<h3 class="ui-dialog-title">Delete this chat?</h3>'
    + '<p class="ui-dialog-msg">The conversation and its attachments are removed for everyone.</p>'
    + '<input class="ui-dialog-input" type="text" value="Theme parity sample">'
    + '<div class="ui-dialog-foot">'
    + '<button class="ui-dialog-btn">Cancel</button>'
    + '<button class="ui-dialog-btn primary">Rename</button>'
    + '<button class="ui-dialog-btn primary danger">Delete</button>'
    + '</div></div>';
  document.body.append(dlg);
  dlg.showModal();
  dlg.querySelector('.ui-dialog-btn.primary').focus();
  return true;
}
"""


# Pictures a theme does not paint: avatars (letter avatars carry a per-bot
# hue), media, and the emoji glyphs that stand in for icons. A colour audit
# (e.g. "a night theme lights no blue") masks these rectangles out.
MASK_JS = """
() => {
  const sel = 'img, video, canvas, picture, .letter-avatar, .bot-avatar, .msg-avatar,'
    + ' .thread-avatar, .ch-avatar, .tl-avatar, .stab-glyph, .empty-emoji, [data-emoji]';
  return Array.from(document.querySelectorAll(sel)).map((n) => n.getBoundingClientRect())
    .filter((r) => r.width > 0 && r.height > 0)
    .map((r) => [Math.floor(r.left), Math.floor(r.top), Math.ceil(r.right), Math.ceil(r.bottom)]);
}
"""


def shoot(page, path: Path) -> list:
    path.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(path), animations="disabled", caret="hide")
    return page.evaluate(MASK_JS)


def run(args) -> int:
    base = f"http://127.0.0.1:{args.port}"
    ids = json.loads(Path(args.ids).read_text()) if args.ids else None
    if ids is None:
        log("no --ids file: the chat surfaces open the first thread of the first bot")
    out = Path(args.out)
    palettes = PALETTES + OPT_IN if args.palettes == "all" else \
        [p.strip() for p in args.palettes.split(",") if p.strip()]
    surfaces = [s.strip() for s in args.surfaces.split(",") if s.strip()]
    captured, skipped, errors = [], [], []
    masks: dict[str, list] = {}

    with sync_playwright() as p:
        launch = {"headless": True}
        explicit = args.chromium_path or os.environ.get("E2E_CHROMIUM_PATH")
        if explicit:
            launch["executable_path"] = explicit
        browser = p.chromium.launch(**launch)

        for palette in palettes:
            for (vw, vh) in VIEWPORTS:
                mobile = vw < 700
                for surface in surfaces:
                    tag = f"{palette}/{surface}-{vw}"
                    extra = {"dispatch-avatar-style": "minimal"} if surface == "minimal" else {}
                    ctx = browser.new_context(
                        viewport={"width": vw, "height": vh},
                        device_scale_factor=1,
                        is_mobile=mobile,
                        has_touch=mobile,
                        reduced_motion="reduce",
                        color_scheme="dark",
                        locale="en-US",
                        timezone_id="UTC",
                    )
                    ctx.clock.set_fixed_time(FIXED_CLOCK_MS)
                    ctx.add_init_script(init_script(palette, extra))
                    page = ctx.new_page()
                    page.on("pageerror", lambda e, tag=tag: errors.append(f"{tag}: {e}"))
                    try:
                        if surface != "lock" and not unlock_via_api(page, base, args.pin):
                            skipped.append(f"{tag}: unlock failed")
                            log(f"SKIP {tag}: unlock failed")
                            continue
                        page.goto(base + "/", wait_until="domcontentloaded", timeout=20000)
                        wait_ready(page)
                        # The theme runtime REMOVES data-palette for the base
                        # theme (Purple), so ask it when it is there.
                        stamped = page.evaluate(
                            "() => (window.UITheme && window.UITheme.current())"
                            " || document.documentElement.dataset.palette")
                        if stamped != palette:
                            skipped.append(f"{tag}: html carries data-palette={stamped!r}")
                            log(f"SKIP {tag}: palette not applied ({stamped!r})")
                            continue
                        ok, why = True, ""
                        if surface in ("chat", "chat-end", "minimal"):
                            tid = (ids or {}).get("sample", "")
                            ok = pick_thread(page, tid, mobile)
                            why = "thread did not open"
                            if ok and surface == "chat":
                                page.evaluate("() => { const m = document.getElementById('messages');"
                                              " if (m) m.scrollTop = 0; }")
                                page.wait_for_timeout(400)
                        elif surface in ("theme", "device"):
                            ok = open_settings(page, surface)
                            why = f"Settings -> {surface} not reachable"
                        elif surface == "dashboard":
                            ok = open_settings(page, "health")
                            why = "Settings -> Health not reachable"
                        elif surface == "lock":
                            ok = open_lock(page)
                            why = "lock screen did not open"
                        elif surface == "dialog":
                            tid = (ids or {}).get("sample", "")
                            pick_thread(page, tid, mobile)
                            ok = page.evaluate(DIALOG_JS)
                            page.wait_for_timeout(300)
                        elif surface == "cmdk":
                            tid = (ids or {}).get("sample", "")
                            pick_thread(page, tid, mobile)
                            page.keyboard.press("Control+k")
                            page.wait_for_timeout(500)
                            ok = page.evaluate("() => !!document.getElementById('cmdk')?.open")
                            why = "Ctrl+K did not open the palette"
                        if not ok:
                            skipped.append(f"{tag}: {why}")
                            log(f"SKIP {tag}: {why}")
                            continue
                        page.wait_for_timeout(250)
                        masks[tag] = shoot(page, out / palette / f"{surface}-{vw}.png")
                        captured.append(tag)
                    except Exception as exc:  # one surface never sinks the run
                        skipped.append(f"{tag}: {exc}")
                        log(f"SKIP {tag}: {exc}")
                    finally:
                        ctx.close()
            log(f"{palette}: done ({len(captured)} captured so far)")
        browser.close()

    report = {"captured": captured, "skipped": skipped, "page_errors": errors, "masks": masks}
    (out).mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    log(f"{len(captured)} captured, {len(skipped)} skipped, {len(errors)} page errors -> {out}")
    return 0 if captured else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, required=True, help="a SCRATCH instance on loopback")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="output root: <out>/<palette>/<surface>-<vw>.png")
    ap.add_argument("--palettes", default=",".join(PALETTES), help="comma list, or \"all\" for all ten (default: the six core palettes)")
    ap.add_argument("--surfaces", default=",".join(SURFACES), help="comma list (default: all)")
    ap.add_argument("--ids", default=None, help='JSON file {"sample": <thread id>} from the seeding step')
    ap.add_argument("--pin", default=DEFAULT_PIN, help="the scratch instance's PIN (never a real one)")
    ap.add_argument("--chromium-path", default=None)
    args = ap.parse_args()
    if args.port == 8765:
        print("refusing to drive :8765 — that is the family app")
        return 2
    # Probe first: a dead port would otherwise produce 96 identical SKIPs.
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{args.port}/api/auth/status", timeout=5).read()
    except Exception as exc:
        print(f"no DisPatch answering on 127.0.0.1:{args.port}: {exc}")
        return 2
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
