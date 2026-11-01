#!/usr/bin/env python3
"""Role-matrix e2e + axe-core + icon/label inventory (+ the service-worker upgrade path).

What it does, per seeded profile (Owner = protected Admin, Admin, User, Basic and the
PIN-less default "DisPatch"), per viewport (1280x800 desktop, 390x844 phone) and per
language (``en``; ``ar`` = RTL, phone only by default):

  signin       land on the app; sign in through the profile tiles + PIN form (the default
               profile lands signed in with no PIN)
  chat         open the Atlas chat, send a message, see the fake model's reply stream in
  settings-*   open Settings and every section this role may see
  characters-* Characters -> Mine, Household (when the role has it), Details of a row
  voice        (Owner, fake voice add-on) Details -> the add-on's Voice picker saves `voice`
  close        Settings closes with #bm-close and the backdrop is hidden again
  tools        the Tools menu, and the Image Studio entry in it

Each screen: axe-core (injected from frontend/node_modules/axe-core/axe.min.js through an
init script, so the page's CSP is untouched) must report no ``serious``/``critical``
violation that is not in the allow-list (``tests/e2e/axe-allowlist.json``, every row with
a reason); no uncaught page error and no console error outside the allow-list; a
screenshot (test data only); and every visible control goes into the icon/label
inventory (role x screen: accessible name, visible text, title, icon-only flags).

The scratch instance is started here (``_scratch.Scratch``: own port, own data dir, fake
LLM, fake voice add-on) unless ``--base-url`` + ``--seed`` point at one already running.

Usage::

    cd backend && uv run --with playwright python ../tests/e2e/roles_axe.py \
        --port 18856 --data ~/.cache/dispatch-e2e/run --fresh \
        --shots ~/.cache/dispatch-e2e/shots --inventory ~/.cache/dispatch-e2e/icon-inventory.md
    # quick: --roles Owner,User --widths 1280 --langs en
    # the SW upgrade path (base tree = `git archive <base> frontend/static`):
    ... roles_axe.py --sw-upgrade --base-commit 1d0dbb0 --port 18857 --data ~/.cache/dispatch-e2e/sw

Exit 0 = every step passed; 1 = a step failed or axe found a serious/critical issue;
2 = the harness itself could not run (no browser, no axe-core, server would not start).
The browser is the Playwright chromium-headless-shell already installed
(``_scratch.find_headless_shell``); nothing is downloaded.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _scratch
from _scratch import DEFAULT_DATA_ROOT, REPO, ROLE_ORDER, STUB_REPLY, Scratch

#: axe-core from the frontend's devDependencies; DISPATCH_E2E_AXE points at another copy
#: (a worktree whose node_modules is a shared symlink without it).
AXE_JS = Path(os.environ.get("DISPATCH_E2E_AXE")
              or (REPO / "frontend" / "node_modules" / "axe-core" / "axe.min.js"))
ALLOWLIST = Path(__file__).resolve().parent / "axe-allowlist.json"
GATE_IMPACTS = ("serious", "critical")
VIEWPORTS = {1280: {"width": 1280, "height": 800}, 390: {"width": 390, "height": 844}}

# The controls the inventory lists (the clickable surface a person has to understand).
_CONTROLS = ("button, a[href], [role=button], [role=tab], [role=menuitem], [role=menuitemradio],"
             " [role=menuitemcheckbox], [role=link], [role=switch], summary, select,"
             " input[type=button], input[type=submit], input[type=checkbox], input[type=radio]")

_INVENTORY_JS = """
(sel) => {
  const vis = (e) => { const r = e.getBoundingClientRect(); const cs = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none'
      && !e.closest('[hidden], [aria-hidden=true], .hidden, [inert]'); };
  let tree = null;
  try { if (window.axe && axe.setup) tree = axe.setup(document); } catch (e) { tree = null; }
  // The accessible name, in the order the accname spec reads it for these controls:
  // aria-labelledby, aria-label, a <label for>, the content (text, img alt, svg title),
  // then title / value. axe-core's own computation is used when it answers; its
  // virtual-tree call returns '' outside axe.run on some builds, so this is the fallback.
  const ownName = (e) => {
    const lb = e.getAttribute('aria-labelledby');
    if (lb) {
      const t = lb.split(/\\s+/).map((id) => (document.getElementById(id) || {}).textContent || '').join(' ').trim();
      if (t) return t;
    }
    const al = (e.getAttribute('aria-label') || '').trim();
    if (al) return al;
    if (e.id) {
      const lab = document.querySelector(`label[for="${CSS.escape(e.id)}"]`);
      if (lab && lab.textContent.trim()) return lab.textContent.trim();
    }
    const wrap = e.closest('label');
    if (wrap && wrap !== e && wrap.textContent.trim()) return wrap.textContent.trim();
    if (e.tagName !== 'SELECT') {
      const txt = (e.innerText || '').trim();
      if (txt) return txt;
      const alts = [...e.querySelectorAll('img[alt], svg title')].map((x) => (x.getAttribute('alt') || x.textContent || '').trim()).filter(Boolean);
      if (alts.length) return alts.join(' ');
    }
    return (e.getAttribute('title') || (e.tagName === 'INPUT' ? e.value : '') || '').trim();
  };
  const accName = (e) => {
    try {
      if (tree && axe.utils && axe.commons) {
        const vn = axe.utils.getNodeFromTree(e);
        const n = vn ? axe.commons.text.accessibleTextVirtual(vn) : '';
        if (n && n.trim()) return n;
      }
    } catch (err) { /* fall through */ }
    return ownName(e);
  };
  const hint = (e) => {
    let s = e.tagName.toLowerCase();
    if (e.id) return s + '#' + e.id;
    const cls = [...e.classList].filter((c) => !/^(active|hidden|open|is-|focus)/.test(c)).slice(0, 3);
    if (cls.length) s += '.' + cls.join('.');
    for (const k of ['tab', 'feature', 'q', 'section', 'tool', 'action', 'role']) {
      if (e.dataset && e.dataset[k]) { s += `[data-${k}=${e.dataset[k]}]`; break; }
    }
    return s;
  };
  const out = [];
  const seen = new Set();
  for (const e of document.querySelectorAll(sel)) {
    if (!vis(e) || seen.has(e)) continue;
    seen.add(e);
    const text = (e.tagName === 'SELECT' ? '' : (e.innerText || '')).replace(/\\s+/g, ' ').trim();
    const name = accName(e).replace(/\\s+/g, ' ').trim();
    const title = (e.getAttribute('title') || '').trim();
    const iconOnly = !/[\\p{L}\\p{N}]/u.test(text) && !['SELECT', 'INPUT'].includes(e.tagName);
    out.push({ el: hint(e), name, text: text.slice(0, 80), title, icon_only: iconOnly,
               no_tooltip: iconOnly && !title, unlabeled: !name });
  }
  try { if (tree && axe.teardown) axe.teardown(); } catch (e) { /* ignore */ }
  return out;
}
"""


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #

@dataclass
class Step:
    role: str
    width: int
    lang: str
    name: str
    ok: bool = True
    error: str = ""
    axe_blocking: list = field(default_factory=list)
    axe_allowed: list = field(default_factory=list)
    axe_other: int = 0
    console: list = field(default_factory=list)
    shot: str = ""
    ms: int = 0
    #: False when the FLOW broke (a control missing, a wait timed out, a wrong answer), as
    #: opposed to a step whose flow worked but whose screens carry axe or console findings.
    flow_ok: bool = True
    note: str = ""

    def fail(self, why: str, *, flow: bool = False) -> None:
        self.ok = False
        if flow:
            self.flow_ok = False
        self.error = (self.error + "; " if self.error else "") + why


@dataclass
class Run:
    steps: list[Step] = field(default_factory=list)
    inventory: dict = field(default_factory=dict)       # (role, screen) -> {el|name: row}
    allow: dict = field(default_factory=dict)

    @property
    def failed(self) -> list[Step]:
        return [s for s in self.steps if not s.ok]


def load_allowlist(path: Path) -> dict:
    if not path.exists():
        return {"axe": [], "console": []}
    doc = json.loads(path.read_text(encoding="utf-8"))
    for row in doc.get("axe", []) + doc.get("console", []) + doc.get("http", []):
        if not str(row.get("reason") or "").strip():
            raise SystemExit(f"{path}: every allow-list row needs a reason: {row}")
    return {"axe": doc.get("axe", []), "console": doc.get("console", []), "http": doc.get("http", [])}


def _axe_allowed(allow: dict, rule: str, target: str) -> str | None:
    for row in allow.get("axe", []):
        if row.get("rule") == rule and re.search(row.get("target") or ".*", target):
            return row["reason"]
    return None


def _console_allowed(allow: dict, text: str) -> bool:
    return any(re.search(row["pattern"], text) for row in allow.get("console", []) + allow.get("http", []))


# --------------------------------------------------------------------------- #
# The driver
# --------------------------------------------------------------------------- #

class Driver:
    def __init__(self, browser, base: str, seed: dict, run: Run, args, *, role: str, width: int,
                 lang: str, axe_source: str, static_tag: str = ""):
        self.base, self.seed, self.run, self.args = base, seed, run, args
        self.role, self.width, self.lang = role, width, lang
        vp = VIEWPORTS[width]
        phone = width < 700
        self.ctx = browser.new_context(viewport=vp, locale=("ar" if lang == "ar" else "en-US"),
                                       is_mobile=phone, has_touch=phone,
                                       device_scale_factor=2 if phone else 1)
        self.ctx.add_init_script(script=axe_source)
        self.ctx.add_init_script(script=(
            f"try {{ localStorage.setItem('dispatch-lang', {json.dumps(lang)}); }} catch (e) {{}}"))
        self.page = self.ctx.new_page()
        self.page.set_default_timeout(args.timeout * 1000)
        self._console: list[str] = []
        self.page.on("console", lambda m: self._console.append(f"console.{m.type}: {m.text[:300]}")
                     if m.type == "error" else None)
        self.page.on("pageerror", lambda e: self._console.append(f"pageerror: {str(e)[:300]}"))
        self.page.on("response", lambda r: self._console.append(f"http {r.status}: {r.request.method} "
                                                                f"{r.url.replace(self.base, '')}")
                     if r.status >= 400 else None)

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.ctx.close()

    # ---- helpers ---------------------------------------------------------
    def visible(self, sel: str) -> bool:
        try:
            return self.page.locator(sel).first.is_visible()
        except Exception:
            return False

    def click_if(self, sel: str) -> bool:
        if self.visible(sel):
            self.page.locator(sel).first.click()
            return True
        return False

    def settle(self, ms: int = 350) -> None:
        self.page.wait_for_timeout(ms)

    def me(self) -> dict:
        return self.page.evaluate("fetch('/api/auth/me', {credentials: 'same-origin'}).then(r => r.json())")

    # ---- one screen ------------------------------------------------------
    def check(self, step: Step, screen: str) -> None:
        """axe + console + screenshot + inventory for the screen as it is now."""
        mark = len(self._console)
        self.settle_toast()
        try:
            res = self.page.evaluate(
                "async () => { const r = await axe.run(document, { resultTypes: ['violations'] });"
                " return r.violations.map(v => ({ id: v.id, impact: v.impact, help: v.help,"
                " nodes: v.nodes.map(n => ({ target: n.target.join(' '), html: n.html.slice(0, 160),"
                " impact: n.impact })) })); }")
        except Exception as e:
            step.fail(f"axe did not run: {e}")
            res = []
        # axe-core itself sets inline styles while it measures (color-contrast); under the app's
        # CSP (style-src 'self') Chromium logs each as a console error. Those come from the probe,
        # not the app: drop the CSP inline-style lines logged while axe ran, keep everything else.
        self.page.wait_for_timeout(60)
        self._console[mark:] = [m for m in self._console[mark:]
                                if "Applying inline style violates the following Content Security Policy" not in m]
        n_before = len(step.axe_blocking)
        for v in res:
            for n in v["nodes"]:
                imp = n.get("impact") or v.get("impact") or ""
                row = {"screen": screen, "rule": v["id"], "impact": imp, "target": n["target"],
                       "help": v["help"], "html": n["html"]}
                if imp in GATE_IMPACTS:
                    why = _axe_allowed(self.run.allow, v["id"], n["target"])
                    if why:
                        step.axe_allowed.append({**row, "reason": why})
                    else:
                        step.axe_blocking.append(row)
                else:
                    step.axe_other += 1
        new_blocking = len(step.axe_blocking) - n_before
        if new_blocking:
            step.fail(f"{new_blocking} serious/critical axe node(s) on {screen}")
        msgs, self._console[:] = list(self._console), []
        bad = [m for m in msgs if not _console_allowed(self.run.allow, m)]
        step.console += msgs
        # Chromium's "Failed to load resource" console line repeats an HTTP answer that is
        # judged on its own line ("http <code>: <method> <path>"), with the URL: 5xx always
        # fails, 4xx fails unless the allow-list names it (an expected refusal).
        hard = [m for m in bad if m.startswith(("pageerror", "http ")) or (m.startswith("console.error") and "Failed to load resource" not in m)]
        if hard:
            step.fail(f"console/http errors on {screen}: {hard[:3]}")
        if self.args.shots:
            name = f"{self.args.shot_prefix}{self.role.lower()}-{self.width}-{self.lang}-{screen}.png"
            path = Path(self.args.shots) / name
            with contextlib.suppress(Exception):
                self.page.screenshot(path=str(path))
                step.shot = str(path)
        if self.lang == "en" or self.args.inventory_all_langs:
            try:
                rows = self.page.evaluate(_INVENTORY_JS, _CONTROLS)
            except Exception:
                rows = []
            bucket = self.run.inventory.setdefault((self.role, screen), {})
            for r in rows:
                k = (r["el"], r["name"], r["text"])
                if k in bucket:
                    bucket[k]["widths"].add(self.width)
                else:
                    bucket[k] = {**r, "widths": {self.width}}

    def _step(self, name: str, fn) -> Step:
        st = Step(self.role, self.width, self.lang, name)
        t0 = time.monotonic()
        try:
            fn(st)
        except Exception as e:
            st.fail(f"{type(e).__name__}: {str(e).splitlines()[0][:300]}", flow=True)
            st.console += self._console           # what the page said before it broke
            pe = [m for m in self._console if m.startswith("pageerror")]
            if pe:
                st.error += f"; first page error: {pe[0][:200]}"
            self._console[:] = []
            if self.args.verbose:
                traceback.print_exc()
            if self.args.shots:
                with contextlib.suppress(Exception):
                    p = Path(self.args.shots) / (f"{self.args.shot_prefix}{self.role.lower()}-{self.width}-"
                                                 f"{self.lang}-{name}-FAILED.png")
                    self.page.screenshot(path=str(p))
                    st.shot = str(p)
        st.ms = int((time.monotonic() - t0) * 1000)
        self.run.steps.append(st)
        mark = ("ok  " if not st.note else "n/a ") if st.ok else ("FAIL" if not st.flow_ok else "FIND")
        print(f"  {mark} {self.role:<8} {self.width:>4} {self.lang} {name:<26} {st.ms:>6} ms"
              + ("" if st.ok else f"  {st.error}"), flush=True)
        return st

    # ---- the flows -------------------------------------------------------
    def settle_toast(self) -> None:
        """A toast mid-fade (opacity between 0 and 1 for .25 s) is what axe's colour-contrast
        check sees as a low-contrast element; wait until it is fully shown or fully hidden."""
        with contextlib.suppress(Exception):
            self.page.wait_for_function(
                "() => { const t = document.getElementById('toast'); if (!t) return true;"
                " const cs = getComputedStyle(t); return cs.visibility === 'hidden' || Number(cs.opacity) === 1; }",
                timeout=3000)

    def dismiss_cards(self) -> None:
        # The sign-in card (Got it), the landing banner (Got it) and the once-per-device
        # password card (Not now) share the top stack; a person dismisses each with one tap.
        for _ in range(4):
            if not self.click_if(".privacy-card-ok, .privacy-card-later"):
                break
            self.settle(200)

    def do_signin(self, st: Step) -> None:
        pg = self.page
        pg.goto(self.base + "/")
        pg.wait_for_function("() => document.readyState === 'complete'")
        pg.wait_for_selector("#switch-profile, #lock-screen:not(.hidden) .signin-tile", state="visible")
        self.settle(500)
        self.dismiss_cards()
        want = self.seed["profiles"][self.role]
        me = self.me()
        if (me.get("profile") or {}).get("id") == want["id"]:
            self.check(st, "landing")
            return                                   # the default profile lands signed in
        if not self.visible("#lock-screen .signin-tile"):
            pg.locator("#switch-profile").click()
        pg.wait_for_selector(f".signin-tile[data-profile='{want['id']}']", state="visible")
        self.settle(250)
        self.check(st, "signin-tiles")
        pg.locator(f".signin-tile[data-profile='{want['id']}']").click()
        if want["pin"]:
            pg.wait_for_selector("#signin-secret", state="visible")
            self.check(st, "signin-pin")
            pg.fill("#signin-secret", want["pin"])
            pg.locator(".signin-go").click()
        pg.wait_for_function(
            "(id) => fetch('/api/auth/me').then(r => r.json()).then(j => j.profile && j.profile.id === id)",
            arg=want["id"])
        pg.wait_for_selector("#lock-screen", state="hidden")
        self.settle(600)
        self.dismiss_cards()
        self.check(st, "home")

    def do_chat(self, st: Step) -> None:
        pg = self.page
        atlas = pg.locator("#bot-list .bot-btn[aria-label='Atlas']")
        if atlas.count() == 0:
            atlas = pg.locator(".bot-btn", has_text="Atlas")
        if self.width < 700 and not atlas.first.is_visible():
            self.click_if(".mobile-tabs .tab:first-child, #mobile-tabs .tab:first-child")
        atlas.first.click()
        self.settle(500)
        # A fresh chat with the bot: the thread list's + (desktop) or the phone's New chat tab.
        if not (self.click_if("#threadlist #new-chat") or self.click_if("#tab-new-chat")):
            raise AssertionError("no New chat control is visible")
        pg.wait_for_selector("#input:not([disabled])", state="visible")
        self.settle(300)
        self.check(st, "chat-empty")
        msg = f"e2e hello from {self.role} {self.width} {self.lang}"
        pg.fill("#input", msg)
        pg.locator("#send").click()
        pg.wait_for_function("(t) => (document.getElementById('messages') || {}).innerText?.includes(t)",
                             arg=STUB_REPLY.split(".")[0], timeout=self.args.timeout * 1000)
        self.settle(400)
        self.check(st, "chat")

    def open_settings(self) -> None:
        pg = self.page
        if self.width < 700 and not self.visible("#manage-bots"):
            self.click_if("#mobile-tabs .tab:first-child")
        if not self.visible("#botmanager"):
            pg.locator("#manage-bots").click()
        pg.wait_for_selector("#botmanager", state="visible")
        self.settle(400)

    def tabs(self) -> list[str]:
        return self.page.evaluate(
            "() => [...document.querySelectorAll('#settings-tabs [id^=stab-]')]"
            ".filter(e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0 && !e.hidden; })"
            ".map(e => e.id)")

    def to_section_list(self) -> None:
        # Phone: Settings is a drill-in list; Back returns to it.
        if self.width < 700 and self.visible("#settings-back"):
            self.page.locator("#settings-back").click()
            self.settle(300)

    def do_settings(self, st: Step) -> None:
        self.open_settings()
        self.check(st, "settings")
        seen = self.tabs()
        if not seen:
            raise AssertionError("Settings shows no section")
        expect_admin = self.seed["profiles"][self.role]["role"] == "admin"
        has_admin = any(t in seen for t in ("stab-people", "stab-system", "stab-connections"))
        if expect_admin != has_admin:
            st.fail(f"admin sections {'missing' if expect_admin else 'visible'} for {self.role}: {seen}", flow=True)
        for tab in seen:
            self.to_section_list()
            self.page.locator(f"#{tab}").click()
            pane = "#spane-" + tab[len("stab-"):]
            with contextlib.suppress(Exception):
                self.page.wait_for_selector(pane, state="visible", timeout=5000)
            self.settle(500)
            self.check(st, "settings-" + tab[len("stab-"):])
        self.to_section_list()

    def do_characters(self, st: Step) -> None:
        pg = self.page
        self.open_settings()
        self.to_section_list()
        if not self.visible("#stab-characters"):
            st.console.append(f"note: Settings offers no Characters section to {self.role}")
            st.note = "absent"
            return
        pg.locator("#stab-characters").click()
        pg.wait_for_selector("#spane-characters", state="visible")
        self.settle(600)
        if self.visible("#cp-tab-mine"):
            pg.locator("#cp-tab-mine").click()
            self.settle(400)
        self.check(st, "characters-mine")
        if self.visible("#cp-tab-household"):
            pg.locator("#cp-tab-household").click()
            self.settle(500)
            self.check(st, "characters-household")
            pg.locator("#cp-tab-mine").click()
            self.settle(300)
        if self.visible("#spane-characters .cp-details-btn"):
            pg.locator("#spane-characters .cp-details-btn").first.click()
            self.settle(700)
            self.check(st, "characters-details")

    def do_voice(self, st: Step) -> None:
        """The fake add-on's bots.rowExtra picker saves `voice` (Owner, on Atlas)."""
        pg = self.page
        self.open_settings()
        self.to_section_list()
        pg.locator("#stab-characters").click()
        pg.wait_for_selector("#spane-characters", state="visible")
        self.settle(500)
        row = pg.locator("#spane-characters .cp-row", has_text="Atlas").first
        if not row.locator(".e2e-voice-select").first.is_visible():
            row.locator(".cp-details-btn").click()
        sel = row.locator(".e2e-voice-select").first
        try:
            sel.wait_for(state="attached", timeout=8000)
        except Exception:
            raise AssertionError("the add-on's bots.rowExtra picker is not rendered in Characters -> Details "
                                 "(4.3 F1b ports it there; before F1b only the old roster drew it)") from None
        sel.scroll_into_view_if_needed()
        bot_id = row.get_attribute("data-bot")
        sel.select_option("builtin:e2e_alto")
        pg.wait_for_function(
            "(id) => fetch('/api/characters/manage').then(r => r.json()).then(d =>"
            " [...(d.mine || []), ...(d.household || [])].some(b => b.id === id && b.voice === 'builtin:e2e_alto'))",
            arg=bot_id, timeout=10000)
        self.check(st, "characters-voice")
        sel.select_option("")
        self.settle(800)

    def do_close(self, st: Step) -> None:
        pg = self.page
        self.open_settings()
        close = pg.locator("#bm-close")
        if close.count() == 0:
            raise AssertionError("#bm-close is missing")
        if not close.is_visible():
            self.to_section_list()
        close.click()
        pg.wait_for_function("() => document.getElementById('botmanager-backdrop').classList.contains('hidden')",
                             timeout=5000)
        self.settle(300)
        self.check(st, "settings-closed")

    def do_tools(self, st: Step) -> None:
        pg = self.page
        if self.visible("#botmanager"):              # an earlier step left Settings open
            pg.keyboard.press("Escape")
            self.settle(300)
        if self.width < 700 and not self.visible("#tools-menu-btn"):
            self.click_if("#mobile-tabs .tab:first-child")
        pg.locator("#tools-menu-btn").click()
        pg.wait_for_selector("#tools-menu", state="visible")
        self.settle(400)
        self.check(st, "tools-menu")
        studio = pg.locator("#tools-menu .tools-item", has_text=re.compile("Image Studio|استوديو"))
        if studio.count() and studio.first.is_visible():
            studio.first.click()
            self.settle(1500)
            self.check(st, "image-studio")
            pg.keyboard.press("Escape")
            self.settle(300)
        else:
            pg.keyboard.press("Escape")
            st.console.append("note: no Image Studio entry for this role")

    def all(self) -> None:
        signin = self.step("signin", self.do_signin)
        if not signin.flow_ok:
            return
        self.step("chat", self.do_chat)
        self.step("settings", self.do_settings)
        self.step("characters", self.do_characters)
        if self.role == "Owner" and self.seed.get("fake_voice") and self.lang == "en":
            self.step("voice-picker", self.do_voice)
        self.step("settings-close", self.do_close)
        self.step("tools", self.do_tools)

    def step(self, name: str, fn) -> Step:
        if name in self.args.skip_steps:
            print(f"  skip {self.role:<8} {self.width:>4} {self.lang} {name}", flush=True)
            return Step(self.role, self.width, self.lang, name)
        return self._step(name, fn)


# --------------------------------------------------------------------------- #
# The SW upgrade path
# --------------------------------------------------------------------------- #

def _module_refs(static: Path) -> dict[str, str]:
    """path -> the ?v= the tree's own importers/index.html use for it ('' = unversioned)."""
    refs: dict[str, str] = {}
    pat = re.compile(r"""(?:import|from|href=|src=)\s*\(?\s*['"]([^'"]+?\.(?:js|css))\?v=([0-9A-Za-z._-]+)['"]""")
    for f in list(static.rglob("*.js")) + list(static.rglob("*.html")):
        if "vendor" in f.parts:
            continue
        for m in pat.finditer(f.read_text(encoding="utf-8", errors="replace")):
            name = m.group(1).split("/")[-1]
            refs.setdefault(name, m.group(2))
    return refs


def sw_upgrade(args, browser_type, axe_source: str) -> int:
    """A persistent profile runs the base tree, the server switches to the RC tree, ONE
    reload: every module the page loads must be the RC's bytes, the locale the RC's."""
    base_static = Path(args.base_static) if args.base_static else _scratch.export_static(
        args.base_commit, Path(args.data).parent / (Path(args.data).name + "-base-tree"))
    rc_static = Path(args.static).resolve() if args.static else (REPO / "frontend" / "static")
    base_h, rc_h = _scratch.tree_hashes(base_static), _scratch.tree_hashes(rc_static)
    changed = sorted(p for p in rc_h if base_h.get(p) != rc_h[p])
    print(f"SW upgrade: base {base_static} -> RC {rc_static}: {len(changed)} changed file(s)")
    profile_dir = Path(args.data).parent / (Path(args.data).name + "-chrome-profile")
    if profile_dir.exists():
        import shutil
        shutil.rmtree(profile_dir)
    problems: list[str] = []
    report: dict[str, Any] = {"changed": changed}
    vp = VIEWPORTS[args.sw_width]
    phone = args.sw_width < 700
    with Scratch(args.port, args.data, static_dir=base_static, fresh=args.fresh,
                 fake_voice=not args.no_voice) as s:
        seed = s.seed()
        ctx = browser_type.launch_persistent_context(
            str(profile_dir), executable_path=_scratch.find_headless_shell(), headless=True,
            viewport=vp, is_mobile=phone, has_touch=phone, service_workers="allow")
        try:
            ctx.add_init_script(script=axe_source)
            pg = ctx.pages[0] if ctx.pages else ctx.new_page()
            pg.set_default_timeout(args.timeout * 1000)
            pg.goto(s.base + "/")
            # Owner, remembered: the session must survive the restart like a real phone's.
            pg.evaluate("""(b) => fetch('/api/auth/login', {method: 'POST', headers: {'Content-Type':
                'application/json'}, body: JSON.stringify(b)}).then(r => r.status)""",
                        {"profile_id": seed["profiles"]["Owner"]["id"], "pin": seed["profiles"]["Owner"]["pin"],
                         "remember": True})
            pg.reload()
            pg.wait_for_function("() => navigator.serviceWorker && navigator.serviceWorker.controller", timeout=30000)
            pg.locator("#manage-bots").click()           # lazy Settings modules load too
            pg.wait_for_timeout(1500)
            cache_before = pg.evaluate("caches.keys()")
            report["cache_before"] = cache_before
            s.restart(static_dir=rc_static if args.static else None)
            pg.reload()
            pg.wait_for_function("() => document.readyState === 'complete'")
            pg.wait_for_timeout(2500)
            with contextlib.suppress(Exception):
                pg.locator("#manage-bots").click()
            pg.wait_for_timeout(1500)
            loaded = pg.evaluate(
                "() => performance.getEntriesByType('resource').map(e => e.name)"
                ".filter(u => u.includes('/static/'))")
            report["cache_after"] = pg.evaluate("caches.keys()")
            checked = 0
            for url in sorted(set(loaded)):
                path = url.split("/static/", 1)[1].split("?", 1)[0]
                if path not in rc_h:
                    continue
                got = pg.evaluate("""async (u) => { const r = await fetch(u, {cache: 'force-cache'});
                    const b = new Uint8Array(await r.arrayBuffer());
                    const h = await crypto.subtle.digest('SHA-256', b);
                    return [...new Uint8Array(h)].map(x => x.toString(16).padStart(2, '0')).join(''); }""", url)
                checked += 1
                if got != rc_h[path]:
                    problems.append(f"stale after one reload: {url.replace(s.base, '')}"
                                    f" (base={'same' if got == base_h.get(path) else 'other'})")
            # The locale the page asks for (i18n.js fetches with cache: 'no-cache').
            for lang in ("en",):
                got = pg.evaluate("""async (u) => { const r = await fetch(u, {cache: 'no-cache'});
                    const b = new Uint8Array(await r.arrayBuffer());
                    const h = await crypto.subtle.digest('SHA-256', b);
                    return [...new Uint8Array(h)].map(x => x.toString(16).padStart(2, '0')).join(''); }""",
                                  f"/static/locales/{lang}.json")
                if got != rc_h.get(f"locales/{lang}.json"):
                    problems.append(f"locales/{lang}.json is not the RC's after one reload")
            humanized = pg.evaluate("""() => [...document.querySelectorAll('[data-i18n]')]
                .filter(e => e.offsetParent && e.textContent.trim() === e.getAttribute('data-i18n'))
                .map(e => e.getAttribute('data-i18n')).slice(0, 10)""")
            if humanized:
                problems.append(f"raw i18n keys shown: {humanized}")
            report.update({"loaded_static": len(set(loaded)), "checked": checked, "problems": problems})
            if args.shots:
                pg.screenshot(path=str(Path(args.shots) / f"{args.shot_prefix}sw-upgrade-after-reload.png"))
        finally:
            ctx.close()
    out = Path(args.out) if args.out else Path(args.data).parent / (Path(args.data).name + "-sw-upgrade.json")
    out.write_text(json.dumps(report, indent=2))
    print(f"SW upgrade: {report.get('checked', 0)} loaded file(s) compared, {len(problems)} problem(s) -> {out}")
    for p in problems:
        print("  FAIL", p)
    return 1 if problems else 0


# --------------------------------------------------------------------------- #
# Inventory output
# --------------------------------------------------------------------------- #

def write_inventory(run: Run, path: Path, meta: dict) -> None:
    rows_all = [r for b in run.inventory.values() for r in b.values()]
    unl = [r for r in rows_all if r["unlabeled"]]
    tip = [r for r in rows_all if r["no_tooltip"] and not r["unlabeled"]]
    lines = [
        "# Icon / label inventory (DisPatch e2e)",
        "",
        f"Generated by `tests/e2e/roles_axe.py` on {time.strftime('%Y-%m-%d %H:%M')} against "
        f"{meta.get('tree', 'this checkout')}; widths {meta.get('widths')}, language en. Test data only.",
        "",
        "Columns: **control** (tag#id or tag.classes[data-x]), **accessible name** (what a screen reader says, "
        "computed by axe-core), **visible text**, **title** (the hover tooltip), **flag**:",
        "`UNLABELED` = no accessible name at all; `icon, no tooltip` = no visible text and no title "
        "(a sighted person sees only a glyph); `icon + tooltip` = glyph with a hover title.",
        "",
        f"**Totals:** {len(rows_all)} control rows, **{len(unl)} unlabeled**, "
        f"**{len(tip)} icon-only without a tooltip**.",
        "",
    ]
    for role in ROLE_ORDER:
        screens = [k for k in run.inventory if k[0] == role]
        if not screens:
            continue
        lines += [f"## {role}", ""]
        for key in screens:
            rows = list(run.inventory[key].values())
            nu = sum(r["unlabeled"] for r in rows)
            nt = sum(r["no_tooltip"] and not r["unlabeled"] for r in rows)
            lines += [f"### {role} / {key[1]}  ({len(rows)} controls, {nu} unlabeled, {nt} icon-only without tooltip)",
                      "", "| control | accessible name | visible text | title | flag | widths |",
                      "|---|---|---|---|---|---|"]
            for r in sorted(rows, key=lambda r: (not r["unlabeled"], not r["no_tooltip"], r["el"])):
                flag = ("UNLABELED" if r["unlabeled"] else "icon, no tooltip" if r["no_tooltip"]
                        else "icon + tooltip" if r["icon_only"] else "")
                esc = lambda s: str(s).replace("|", "\\|").replace("\n", " ")  # noqa: E731
                lines.append(f"| `{esc(r['el'])}` | {esc(r['name'])} | {esc(r['text'])} | {esc(r['title'])} | "
                             f"{flag} | {','.join(str(w) for w in sorted(r['widths']))} |")
            lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="DisPatch role-matrix e2e + axe + icon inventory")
    ap.add_argument("--port", type=int, default=0, help="scratch port (default: first free in 18856-18869)")
    ap.add_argument("--data", default=str(DEFAULT_DATA_ROOT / "e2e"), help="scratch data dir")
    ap.add_argument("--fresh", action="store_true", help="delete the scratch data dir first")
    ap.add_argument("--static", help="serve this frontend/static tree (e.g. a planted-regression copy)")
    ap.add_argument("--base-url", help="use an instance already running (needs --seed)")
    ap.add_argument("--seed", help="e2e-seed.json of that instance")
    ap.add_argument("--no-voice", action="store_true", help="no fake voice add-on")
    ap.add_argument("--roles", default=",".join(ROLE_ORDER))
    ap.add_argument("--widths", default="1280,390")
    ap.add_argument("--langs", default="en,ar")
    ap.add_argument("--ar-widths", default="390", help="widths the ar (RTL) pass runs at")
    ap.add_argument("--shots", help="screenshot dir (test data only)")
    ap.add_argument("--shot-prefix", default="")
    ap.add_argument("--inventory", help="write the icon/label inventory markdown here")
    ap.add_argument("--inventory-all-langs", action="store_true")
    ap.add_argument("--allowlist", default=str(ALLOWLIST))
    ap.add_argument("--out", help="results JSON (default: <data>-results.json)")
    ap.add_argument("--timeout", type=float, default=30.0, help="seconds per wait")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--skip", default="", help="comma-separated step names to skip (e.g. voice-picker)")
    ap.add_argument("--sw-upgrade", action="store_true", help="run the service-worker upgrade-path check only")
    ap.add_argument("--base-commit", default="1d0dbb0", help="base tree for --sw-upgrade")
    ap.add_argument("--base-static", help="an already exported base frontend/static tree")
    ap.add_argument("--sw-width", type=int, default=1280, choices=sorted(VIEWPORTS))
    args = ap.parse_args(argv)
    args.skip_steps = {x.strip() for x in args.skip.split(",") if x.strip()}

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("python Playwright is missing: run with `uv run --with playwright`", file=sys.stderr)
        return 2
    if not AXE_JS.exists():
        print(f"axe-core is missing ({AXE_JS}): `cd frontend && npm ci`", file=sys.stderr)
        return 2
    exe = _scratch.find_headless_shell()
    if not exe:
        print("no chromium-headless-shell under ~/.cache/ms-playwright (set DISPATCH_E2E_CHROME)", file=sys.stderr)
        return 2
    axe_source = AXE_JS.read_text(encoding="utf-8")
    if args.shots:
        Path(args.shots).mkdir(parents=True, exist_ok=True)
    args.port = args.port or _scratch.free_port()

    with sync_playwright() as p:
        if args.sw_upgrade:
            return sw_upgrade(args, p.chromium, axe_source)
        run = Run(allow=load_allowlist(Path(args.allowlist)))
        browser = p.chromium.launch(executable_path=exe)
        scratch = None
        try:
            if args.base_url:
                if not args.seed:
                    ap.error("--base-url needs --seed")
                base, seed = args.base_url.rstrip("/"), json.loads(Path(args.seed).read_text())
            else:
                scratch = Scratch(args.port, args.data, static_dir=args.static, fresh=args.fresh,
                                  fake_voice=not args.no_voice).start()
                seed, base = scratch.seed(), scratch.base
            roles = [r.strip() for r in args.roles.split(",") if r.strip()]
            widths = [int(w) for w in args.widths.split(",") if w.strip()]
            ar_widths = [int(w) for w in args.ar_widths.split(",") if w.strip()]
            langs = [x.strip() for x in args.langs.split(",") if x.strip()]
            for role in roles:
                if role not in seed["profiles"]:
                    raise SystemExit(f"unknown role {role!r}: {list(seed['profiles'])}")
                for lang in langs:
                    for width in (widths if lang == "en" else [w for w in widths if w in ar_widths] or ar_widths):
                        d = Driver(browser, base, seed, run, args, role=role, width=width, lang=lang,
                                   axe_source=axe_source)
                        try:
                            d.all()
                        finally:
                            d.close()
        finally:
            browser.close()
            if scratch is not None:
                scratch.stop()
                if scratch.stub is not None:
                    scratch.stub.stop()
    out = Path(args.out) if args.out else Path(args.data).parent / (Path(args.data).name + "-results.json")
    out.write_text(json.dumps({
        "steps": [s.__dict__ for s in run.steps],
        "failed": len(run.failed), "total": len(run.steps),
        "flow_broken": [f"{s.role}/{s.width}/{s.lang}/{s.name}" for s in run.steps if not s.flow_ok],
        "axe_blocking": sum(len(s.axe_blocking) for s in run.steps),
        "axe_allowed": sum(len(s.axe_allowed) for s in run.steps),
    }, indent=2, default=list))
    if args.inventory:
        write_inventory(run, Path(args.inventory), {
            "widths": args.widths, "tree": args.static or "this checkout"})
    blocking = {}
    for s in run.steps:
        for v in s.axe_blocking:
            blocking.setdefault((v["rule"], v["impact"]), set()).add(v["target"])
    flow_broken = [s for s in run.steps if not s.flow_ok]
    print(f"\n{len(run.steps) - len(run.failed)}/{len(run.steps)} steps passed; "
          f"{len(flow_broken)} with a broken flow (FAIL), {len(run.failed) - len(flow_broken)} with findings only (FIND); "
          f"{sum(len(s.axe_blocking) for s in run.steps)} blocking axe node(s); results {out}")
    for (rule, imp), targets in sorted(blocking.items()):
        print(f"  axe {imp:<8} {rule:<28} {len(targets)} target(s): {sorted(targets)[:4]}")
    return 1 if run.failed else 0


if __name__ == "__main__":
    sys.exit(main())
