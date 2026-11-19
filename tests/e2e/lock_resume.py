#!/usr/bin/env python3
"""Headless check of lock / resume / keep-unlocked (docs/design/lock-resume.md).

Drives a scratch DisPatch (tests/e2e/_scratch.py) in chrome-headless-shell at desktop and
phone width. Each check prints PASS/FAIL; exit 1 on any failure.

    cd backend && uv run --with playwright python ../tests/e2e/lock_resume.py \
        --port 18861 --data /var/tmp/dp-lock-scratch

The idle timeouts are set to 60 s on the scratch install, so the run takes ~10 minutes.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _scratch import Scratch, find_headless_shell  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

IDLE = 60
RESULTS: list[tuple[str, bool, str]] = []
CANARY = "ZEBRA-CANARY-7731"


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail else ""), flush=True)
    return bool(ok)


def set_security(s: Scratch, **kv) -> None:
    p = Path(s.data) / "security.yaml"
    text = p.read_text()
    for k, v in kv.items():
        if re.search(rf"(?m)^{k}:", text):
            text = re.sub(rf"(?m)^{k}:.*$", f"{k}: {v}", text)
        else:
            text += f"\n{k}: {v}\n"
    p.write_text(text)


class Dev:
    def __init__(self, browser, base, seed, *, phone=False):
        self.base, self.seed = base, seed
        kw = {"viewport": {"width": 390, "height": 844}, "is_mobile": True, "has_touch": True,
              "device_scale_factor": 2} if phone else {"viewport": {"width": 1280, "height": 860}}
        self.ctx = browser.new_context(**kw)
        self.page = self.ctx.new_page()
        self.phone = phone

    def status(self) -> dict:
        return self.page.evaluate("() => fetch('/api/auth/status').then(r => r.json())")

    def me_id(self):
        j = self.page.evaluate("() => fetch('/api/auth/me').then(r => r.json())")
        return (j.get("profile") or {}).get("id") if j.get("signed_in", True) else None

    def signed_in_as(self, role) -> bool:
        st = self.status()
        return bool(st.get("signed_in")) and (st.get("profile") or {}).get("id") == self.seed["profiles"][role]["id"]

    def open(self):
        self.page.goto(self.base + "/")
        self.page.wait_for_function("() => document.readyState === 'complete'")
        self.page.wait_for_timeout(1500)

    def cards(self) -> int:
        return self.page.locator(".privacy-card:visible").count()

    def dismiss(self):
        for _ in range(4):
            loc = self.page.locator(".privacy-card-later:visible, .privacy-card-ok:visible, .privacy-card-close:visible")
            if loc.count() == 0:
                break
            loc.first.click()
            self.page.wait_for_timeout(200)

    def sign_in(self, role, *, remember=False, via_split_others=False):
        pg = self.page
        want = self.seed["profiles"][role]
        if via_split_others and pg.locator("#lock-split:not([hidden]) .lks-others").count():
            pg.locator(".lks-others").click()
        elif not pg.locator("#lock-screen:not(.hidden) .signin-tile").count():
            if pg.locator("#lock-split:not([hidden])").count():
                pg.locator(".lks-others").click()
            else:
                self.dismiss()
                self.goto_switch()
                if pg.locator("#lock-split:not([hidden])").count():
                    pg.locator(".lks-others").click()
        pg.wait_for_selector(f".signin-tile[data-profile='{want['id']}']", state="visible")
        if remember and pg.locator("#lock-remember-row:not(.hidden)").count():
            pg.locator("#lock-remember").check()
        pg.locator(f".signin-tile[data-profile='{want['id']}']").click()
        if want["pin"]:
            pg.wait_for_selector("#signin-secret", state="visible")
            pw = pg.locator("#signin-secret").get_attribute("autocomplete") == "current-password"
            pg.fill("#signin-secret", want.get("password") if pw and want.get("password") else want["pin"])
            pg.locator(".signin-go").click()
        pg.wait_for_function(
            "(id) => fetch('/api/auth/me').then(r => r.json()).then(j => j.profile && j.profile.id === id)",
            arg=want["id"], timeout=15000)
        pg.wait_for_selector("#lock-screen", state="hidden")
        pg.wait_for_timeout(1200)

    def goto_switch(self):
        pg = self.page
        # The landing banner / profile chip: openSwitcher(). Use the function the buttons call.
        if pg.locator("#switch-profile:visible").count():
            pg.locator("#switch-profile").click()
        else:
            pg.locator("#profile-chip").click()
            pg.wait_for_timeout(200)
            pg.get_by_role("menuitem").filter(has_text=re.compile("Switch|profile", re.I)).first.click()
        pg.wait_for_timeout(500)

    def lock(self):
        pg = self.page
        if pg.locator("#lock-now:visible").count():
            pg.locator("#lock-now").click()
        elif pg.locator("#bm-lock:visible").count():
            pg.locator("#bm-lock").click()
        else:
            pg.locator("#profile-chip").click()
            pg.wait_for_timeout(200)
            pg.get_by_role("menuitem").filter(has_text=re.compile("Lock", re.I)).first.click()
        pg.wait_for_timeout(2000)

    def split_shown(self) -> bool:
        return self.page.locator("#lock-screen.lock-mode-split:not(.hidden) #lock-split:not([hidden]) .lks-bottom").count() == 1

    def pad(self, digits: str):
        for d in digits:
            self.page.locator(f"#lock-split button[data-k='{d}']").click()
        self.page.locator(".lks-submit").click()
        self.page.wait_for_timeout(2500)

    def held_leaks(self, role) -> list[str]:
        """Anything of the held profile in DOM / storage."""
        pid = self.seed["profiles"][role]["id"]
        role = self.seed["profiles"][role].get("name", role)
        html = self.page.content()
        storage = self.page.evaluate("""() => { const o = {};
            for (let i = 0; i < localStorage.length; i++) { const k = localStorage.key(i); o[k] = localStorage.getItem(k); }
            for (let i = 0; i < sessionStorage.length; i++) { const k = sessionStorage.key(i); o['s:' + k] = sessionStorage.getItem(k); }
            return JSON.stringify(o); }""")
        out = []
        for label, hay in (("dom", html), ("storage", storage)):
            if CANARY in hay:
                out.append(f"{label}:canary")
            if pid in hay:
                out.append(f"{label}:profile-id")
            if re.search(rf"\b{re.escape(role)}\b", re.sub(r"<[^>]+>", " ", hay) if label == "dom" else hay):
                out.append(f"{label}:name '{role}'")
        return out

    def close(self):
        self.ctx.close()


def send_canary(d: Dev):
    """Put recognisable text on screen: a chat message to the default bot."""
    pg = d.page
    box = pg.locator("textarea#input")
    try:
        if not box.is_visible():
            pg.locator(".bot-btn:visible").first.click(timeout=4000)
            pg.wait_for_timeout(1200)
        box.fill(CANARY, timeout=4000)
        box.press("Enter")
        pg.wait_for_timeout(2500)
    except Exception as e:  # noqa: BLE001
        print("  (no composer:", type(e).__name__, ")")


def run(args) -> int:
    data = Path(args.data)
    with Scratch(args.port, data, fresh=True) as s:
        seed = s.seed()
        set_security(s, lock_timeout_seconds=IDLE, session_timeout_user=IDLE, session_timeout_basic=IDLE,
                     remember_device_days=30, lock_target="default_profile")
        s.restart()
        owner_api = s.api("Owner")
        # A PIN-less profile (not the default) and a password on Admin.
        owner_api.call("POST", "/api/people", {"name": "Pinless", "role": "user"})
        people = {p["name"]: p for p in owner_api.ok("GET", "/api/people")["profiles"]}
        seed["profiles"]["Pinless"] = {"id": people["Pinless"]["id"], "role": "user", "pin": ""}
        # A name no UI string contains, so a leak of it is unambiguous.
        owner_api.ok("PATCH", f"/api/people/{seed['profiles']['User']['id']}", {"name": "Quokka"})
        seed["profiles"]["User"]["name"] = "Quokka"
        adm = s.api("Admin")
        code, out = adm.call("POST", "/api/auth/password", {"current_pin": seed["profiles"]["Admin"]["pin"], "new": "correct horse 42"})
        print("  admin password set:", code)
        seed["profiles"]["Admin"]["password"] = "correct horse 42"
        chrome = find_headless_shell()
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, **({"executable_path": chrome} if chrome else {}))
            for phone in (False, True):
                W = "phone" if phone else "desktop"
                # ---- first sign-in shows notices; lock -> split, no leaks; pad resumes, no notices
                d = Dev(browser, s.base, seed, phone=phone)
                d.open()
                d.sign_in("User")
                check(f"{W}: notice card on a first sign-in", d.cards() >= 1, f"cards={d.cards()}")
                d.dismiss()
                send_canary(d)
                d.lock()
                check(f"{W}: lock shows the split screen", d.split_shown())
                leaks = d.held_leaks("User")
                check(f"{W}: locked DOM/storage holds nothing of the held profile", not leaks, ",".join(leaks))
                st = d.status()
                other = Dev(browser, s.base, seed, phone=phone)
                other.open()
                st0 = other.status()
                other.close()
                diff = sorted(k for k in set(st) | set(st0) if st.get(k) != st0.get(k))
                check(f"{W}: status is one bit (same as a fresh browser but for resumable)",
                      st.get("resumable") is True and diff == ["resumable"], f"diff={diff}")
                d.pad(seed["profiles"]["User"]["pin"])
                check(f"{W}: right PIN on the pad resumes", d.signed_in_as("User"))
                check(f"{W}: no notice cards on resume", d.cards() == 0, f"cards={d.cards()}")
                # ---- Go to main, then back to the split from Switch profile, still resumes
                d.lock()
                d.page.locator("#lks-main").click()
                d.page.wait_for_timeout(800)
                check(f"{W}: Go to main closes the lock screen", not d.split_shown()
                      and d.page.locator("#lock-screen.hidden").count() == 1)
                d.goto_switch()
                check(f"{W}: Switch profile brings the split back", d.split_shown())
                d.pad(seed["profiles"]["User"]["pin"])
                check(f"{W}: ... and the same profile still resumes", d.signed_in_as("User"))
                # ---- keyboard only
                d.lock()
                d.page.wait_for_timeout(300)
                focused = d.page.evaluate("() => document.activeElement && document.activeElement.id")
                d.page.keyboard.type(seed["profiles"]["User"]["pin"])
                d.page.keyboard.press("Enter")
                d.page.wait_for_timeout(2500)
                check(f"{W}: keyboard-only resume (focus starts in the field)", focused == "lks-field" and d.signed_in_as("User"),
                      f"focus={focused}")
                # ---- wrong PINs: each goes to main; after 5 the resume is no longer offered
                d.lock()
                went_main = True
                for i in range(5):
                    if not d.split_shown():
                        d.goto_switch()
                    if not d.split_shown():
                        break
                    d.pad("0000")
                    went_main &= (not d.split_shown()) and not d.signed_in_as("User")
                st = d.status()
                check(f"{W}: wrong PIN goes to main each time", went_main)
                check(f"{W}: 5 wrong PINs: resume no longer offered", st.get("resumable") is False)
                d.goto_switch()
                check(f"{W}: after lockout Switch profile shows normal tiles",
                      not d.split_shown() and d.page.locator(".signin-tile:visible").count() > 0)
                d.page.keyboard.press("Escape")
                d.page.wait_for_timeout(15000)   # the resume bucket's short cooldown
                # ---- a second profile cannot resume the first
                d.sign_in("User", via_split_others=False)
                d.dismiss()
                d.lock()
                d.sign_in("Admin", via_split_others=True)
                d.dismiss()
                st_a = d.status()
                d.lock()
                d.pad(seed["profiles"]["User"]["pin"])
                check(f"{W}: another profile's PIN does not resume the held one", not d.signed_in_as("User")
                      and not d.signed_in_as("Admin"))
                # ---- password profile resumes with the password typed into the same field
                d.page.wait_for_timeout(25000)   # the wrong try above re-armed the short cooldown
                d.goto_switch()
                if d.split_shown():
                    d.page.locator("#lks-field").fill("correct horse 42")
                    d.page.locator(".lks-submit").click()
                    d.page.wait_for_timeout(2500)
                    check(f"{W}: password profile resumes with its password", d.signed_in_as("Admin"))
                else:
                    check(f"{W}: password profile resumes with its password", False,
                          "no split (resume dropped by the earlier wrong PIN count?)")
                d.dismiss()
                # ---- PIN-less profile: never offered
                d.sign_in("Pinless", via_split_others=False)
                d.dismiss()
                d.lock()
                check(f"{W}: PIN-less profile lock: no resume offered", not d.split_shown() and d.status().get("resumable") is False)
                d.close()

                # ---- restart keeps sessions (remembered and not)
                d = Dev(browser, s.base, seed, phone=phone)
                d.open()
                d.sign_in("Owner", remember=True)
                d.dismiss()
                st = d.status()
                check(f"{W}: Owner remembered device is keep-unlocked by default",
                      st.get("remembered") and st.get("keep_unlocked") is True, json.dumps({k: st.get(k) for k in ("remembered", "keep_unlocked")}))
                d2 = Dev(browser, s.base, seed, phone=phone)
                d2.open()
                d2.sign_in("User")
                d2.dismiss()
                s.restart()
                d.open()
                d2.open()
                check(f"{W}: restart keeps the remembered device signed in", d.signed_in_as("Owner"))
                check(f"{W}: restart keeps a plain session signed in", d2.signed_in_as("User"))
                d2.close()

                # ---- idle: keep-unlocked device stays in past idle, through reload/restart/reopen
                if phone:
                    d.page.wait_for_timeout((IDLE + 20) * 1000)
                    check(f"{W}: keep-unlocked: still in past the idle timeout", d.signed_in_as("Owner")
                          and d.page.locator("#lock-screen.hidden").count() == 1)
                    d.page.reload(); d.page.wait_for_timeout(1500)
                    check(f"{W}: keep-unlocked: reload -> straight in", d.signed_in_as("Owner")
                          and d.page.locator("#lock-screen.hidden").count() == 1)
                    s.restart()
                    d.open()
                    check(f"{W}: keep-unlocked: server restart -> straight in", d.signed_in_as("Owner")
                          and d.page.locator("#lock-screen.hidden").count() == 1)
                    d.page.close(); d.page = d.ctx.new_page(); d.open()
                    check(f"{W}: keep-unlocked: reopen -> straight in", d.signed_in_as("Owner")
                          and d.page.locator("#lock-screen.hidden").count() == 1)
                    # turn it off in Settings -> This device
                    d.page.evaluate("() => { location.hash = '#settings/device'; }")
                    d.page.wait_for_timeout(1500)
                    box = d.page.locator("#keep-unlocked")
                    present = box.count() == 1
                    if present:
                        box.scroll_into_view_if_needed()
                        box.uncheck()
                        d.page.wait_for_timeout(1500)
                    st = d.status()
                    check(f"{W}: Settings -> This device turns keep-unlocked off", present and st.get("keep_unlocked") is False)
                    d.page.keyboard.press("Escape"); d.page.wait_for_timeout(300)
                    d.page.wait_for_timeout((IDLE + 25) * 1000)
                    check(f"{W}: keep-unlocked off: idle lock applies (split shown)", d.split_shown() and not d.signed_in_as("Owner"))
                    d.pad(seed["profiles"]["Owner"]["pin"])
                    d.page.wait_for_timeout(500)
                    # manual Lock always works and shows the split
                    d.lock()
                    check(f"{W}: manual Lock shows the split screen", d.split_shown())
                    d.close()
                    # Restricted profile cannot turn it on
                    b = Dev(browser, s.base, seed, phone=True)
                    b.open()
                    b.sign_in("Basic", remember=True)
                    b.dismiss()
                    st = b.status()
                    r = b.page.evaluate("() => fetch('/api/auth/keep-unlocked', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({on:true})}).then(r => r.status)")
                    b.page.evaluate("() => { location.hash = '#settings/device'; }")
                    b.page.wait_for_timeout(1500)
                    check(f"{W}: Restricted profile cannot turn keep-unlocked on",
                          st.get("keep_unlocked") is False and st.get("keep_unlocked_allowed") is False and r == 403
                          and b.page.locator("#keep-unlocked").count() == 0, f"status={r}")
                    b.close()
                else:
                    d.close()
                    # Tool window activity past the idle timeout (non-remembered User).
                    d = Dev(browser, s.base, seed, phone=False)
                    d.open()
                    d.sign_in("User")
                    d.dismiss()
                    tools = d.page.evaluate("() => fetch('/api/tools').then(r => r.json())")
                    items = tools.get("tools") if isinstance(tools, dict) else tools
                    app = next((t for t in (items or []) if t.get("kind") in ("app", "static")), None)
                    if not app:
                        check(f"{W}: typing in a tool window past idle does not lock", False, "no app/static tool on scratch")
                    else:
                        d.page.evaluate("(id) => { location.hash = '#tool=' + id; }", app["id"])
                        d.page.wait_for_timeout(3000)
                        frames = [f for f in d.page.frames if f != d.page.main_frame]
                        # Real input only (the shell ignores a script's dispatchEvent): click into
                        # the tool's frame once, then type there with the keyboard.
                        fel = d.page.locator("iframe:visible").first
                        box = fel.bounding_box()
                        d.page.mouse.click(box["x"] + box["width"] / 2, box["y"] + min(200, box["height"] / 2))
                        d.page.mouse.move(5, 5)        # leave the frame: pointerenter will not fire again
                        end = time.time() + IDLE + 25
                        typed = 0
                        while time.time() < end:
                            d.page.keyboard.press("Shift")
                            typed += 1
                            d.page.wait_for_timeout(4000)
                        in_frame = d.page.evaluate("() => document.activeElement && document.activeElement.tagName")
                        check(f"{W}: typing in a tool window past idle does not lock",
                              typed > 0 and d.signed_in_as("User") and not d.split_shown(),
                              f"tool={app['id']} kind={app.get('kind')} frames={len(frames)} keys={typed} focus={in_frame}")
                        # and without activity it does lock (to the split)
                        d.page.evaluate("() => { location.hash = ''; }")
                        d.page.wait_for_timeout((IDLE + 25) * 1000)
                        check(f"{W}: no activity: idle lock to the split screen", d.split_shown() and not d.signed_in_as("User"))
                    d.close()
            browser.close()
    fails = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} checks passed")
    return 1 if fails else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=18861)
    ap.add_argument("--data", default="/var/tmp/dp-lock-scratch")
    sys.exit(run(ap.parse_args()))
