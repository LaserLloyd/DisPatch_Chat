// 4.3 Fable (session): the idle sign-out, where a sign-out lands, and the cards that tell a
// person who they are on a shared device. Each test failed before its fix.
//
//  1. The idle timer arms with the CALLER's own timeout (status.idle_timeout_seconds), not the
//     Admin lock_timeout; a person reading (no sends) tells the server with an "active" frame.
//  2. An idle sign-out, a Sign out to <default> or a lapse lands on the HOME screen of the
//     default profile with no chat open, and a banner that stays until dismissed says why.
//  3. The first-visit card on the shared profile says how to switch; the chip's tooltip says
//     "You're on {name} (shared profile)".
//  4. "Set a password" is a once-per-device card with a button, never a toast.
//  5. "Signed in as ..." is short-lived and a phone toast sits above the tab bar.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { domSkip } from './_require-dom.js';

const HERE = dirname(fileURLToPath(import.meta.url));
const STATIC = join(HERE, '..', 'static');
const read = (f) => readFileSync(join(STATIC, f), 'utf8');
const MAIN = read('js/main.js');
const CSS = read('app.css');
const EN = JSON.parse(read('locales/en.json'));
const LANGS = ['en', 'ar', 'de', 'es', 'fr', 'ja', 'pt', 'zh'];

const require = createRequire(import.meta.url);
let jsdom = null;
try { jsdom = require('jsdom'); } catch { /* DOM tests skip */ }
const dom = { skip: domSkip(jsdom ? false : 'jsdom is not installed') };

async function boot() {
  const { JSDOM } = jsdom;
  const win = new JSDOM('<!doctype html><html><body><main id="app"><button id="other">x</button></main></body></html>',
    { url: 'http://127.0.0.1:18845/' }).window;
  const put = (k, v) => Object.defineProperty(globalThis, k, { value: v, configurable: true, writable: true });
  for (const [k, v] of Object.entries({ window: win, document: win.document, location: win.location,
    navigator: win.navigator, localStorage: win.localStorage, sessionStorage: win.sessionStorage,
    Node: win.Node, HTMLElement: win.HTMLElement })) put(k, v);
  globalThis.fetch = async (u) => (String(u).startsWith('/static/locales/en.json')
    ? { ok: true, status: 200, json: async () => EN, headers: new win.Headers() }
    : { ok: false, status: 404, json: async () => ({}), headers: new win.Headers() });
  const i18n = await import('../static/js/i18n.js?v=5');
  await i18n.init('en');
  return { win, mod: await import(`../static/js/host-notice.js?t=${Math.random()}`) };
}

/** A function's source, from its declaration to the next top-level declaration. */
function fn(name) {
  const start = MAIN.search(new RegExp(`\\n(?:async )?function ${name}\\(`));
  assert.ok(start > 0, `${name} exists`);
  const rest = MAIN.slice(start + 1);
  const end = rest.search(/\n(?:async function|function|const|let|\/\/ ----|\/\*\*)\s/);
  return rest.slice(0, end > 0 ? end : undefined);
}

// ---- 1. the idle timer -------------------------------------------------------------------

test('the idle timer arms with the status idle_timeout_seconds, not the Admin lock_timeout', () => {
  assert.match(fn('applyAuthStatus'), /idleTimeout: s\.idle_timeout_seconds \|\| 0,/);
  const arm = fn('armIdleTimer');
  assert.match(arm, /idleSeconds\(\)/);
  assert.doesNotMatch(arm, /state\.auth\.lockTimeout \|\| 600/);
  assert.match(MAIN, /const idleSeconds = \(\) => state\.auth\.idleTimeout > 0 \? state\.auth\.idleTimeout : \(state\.auth\.lockTimeout \|\| 600\);/);
  assert.match(arm, /setTimeout\(\(\) => lockNow\('idle'\), ms\)/);
});

test('a person reading tells the server (an "active" frame, throttled); the timer re-arms', () => {
  const bump = fn('bumpIdle');
  assert.match(bump, /lastUse = now;/);
  assert.match(bump, /socket\.send\(\{ type: 'active' \}\)/);
});

// ---- 2. landing ---------------------------------------------------------------------------

test('every sign-out path lands on Home: no bot selected, so no chat opens and no composer waits', () => {
  const lock = fn('lockToTarget');
  const reboot = lock.indexOf('await reboot()');
  const clear = lock.indexOf('landHome();');
  assert.ok(clear > 0 && clear < reboot, 'the selection is dropped before the rebuild');
  const rec = fn('reconcileIdentity');
  assert.ok(rec.indexOf('landHome();') > 0
    && rec.indexOf('landHome();') < rec.indexOf('await reboot()'), 'a lapse seen on reconnect too');
  const home = fn('landHome');
  assert.match(home, /state\.selectedBotId = null;/);
  assert.match(home, /dom\['tl-botname'\]\.textContent = t\('app\.name'\);/, 'the old bot name leaves the list header');
  // The old landing toast is gone: a banner that stays.
  assert.doesNotMatch(lock, /toast\(t\('lock\.landed'/);
  assert.match(lock, /showLandingBanner\(/);
});

test('each path names its reason: idle timer, the button, a server lapse, the default profile going away', () => {
  assert.match(fn('lockNow'), /async function lockNow\(reason = 'manual'\)/);
  const hl = fn('handleLocked');
  assert.match(hl, /'default'/);
  assert.match(hl, /'lapsed'/);
  assert.match(hl, /'idle'/);
  const lock = fn('lockToTarget');
  for (const r of ['default_disabled', 'default_changed']) assert.ok(lock.includes(`'${r}'`), r);
});

test('landing sentences: idle, manual, shared profile off, shared profile changed', { skip: dom.skip }, async () => {
  const { mod } = await boot();
  assert.equal(mod.landingText('idle', { name: 'DisPatch', seconds: 3600 }),
    "You were signed out after 1 hour without use. You're on DisPatch, the shared profile: everyone can read its chats. Tap Switch profile to use your own.");
  assert.match(mod.landingText('idle', { name: 'DisPatch', seconds: 600 }), /after 10 minutes without use/);
  assert.equal(mod.landingText('manual', { name: 'DisPatch' }), "You're on DisPatch now. To use your own profile, tap Switch profile.");
  assert.equal(mod.landingText('lapsed', { name: 'DisPatch' }), mod.landingText('manual', { name: 'DisPatch' }));
  assert.equal(mod.landingText('default_disabled', { name: 'DisPatch' }), 'The shared DisPatch profile was turned off. Pick your profile.');
  assert.equal(mod.landingText('default_changed', {}), 'An Admin changed the shared profile. Sign in again.');
});

test('the landing banner stays until dismissed, is keyboard reachable and offers Switch profile', { skip: dom.skip }, async () => {
  const { win, mod } = await boot();
  let switched = 0;
  const b = mod.showLandingBanner({ text: 'hello', onSwitch: () => { switched++; } });
  assert.ok(b.isConnected);
  assert.equal(b.getAttribute('role'), 'status');
  assert.equal(b.querySelector('.landing-banner-text').textContent, 'hello');
  // A press elsewhere does NOT take it down (it is not the 4-second toast, nor the sign-in card).
  win.document.getElementById('other').dispatchEvent(new win.Event('pointerdown', { bubbles: true }));
  assert.ok(b.isConnected, 'still there after a press elsewhere');
  const buttons = [...b.querySelectorAll('button')];
  assert.ok(buttons.every((x) => x.tabIndex >= 0), 'every control is reachable with Tab');
  const close = b.querySelector('.privacy-card-close');
  assert.equal(close.getAttribute('aria-label'), EN.common.close);
  assert.equal(close.title, EN.common.close);
  const sw = [...b.querySelectorAll('button')].find((x) => x.textContent === EN.chip.switch);
  assert.ok(sw, 'a Switch profile button');
  sw.click();
  assert.equal(switched, 1);
  assert.ok(!b.isConnected, 'switching dismisses it');
  const b2 = mod.showLandingBanner({ text: 'again' });
  b2.querySelector('.landing-banner-text').focus?.();
  b2.dispatchEvent(new win.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  assert.ok(!b2.isConnected, 'Escape inside it dismisses it');
  const b3 = mod.showLandingBanner({ text: 'x' });
  b3.querySelector('.privacy-card-close').click();
  assert.ok(!b3.isConnected, 'the ✕ dismisses it');
  // It sits in the top stack, over the sign-in screen when that is where we landed.
  assert.ok(b3.parentNode === null);
  const b4 = mod.showLandingBanner({ text: 'y' });
  assert.ok(b4.parentNode.classList.contains('notice-stack'));
  mod.showLandingBanner({ text: 'z' });
  assert.equal(win.document.querySelectorAll('.landing-banner').length, 1, 'one at a time');
});

test('the banner and the stack are styled at the top, clear of the phone\'s bottom row', () => {
  assert.match(CSS, /\.notice-stack \{[^}]*position: fixed;[^}]*top: /);
  assert.match(CSS, /\.notice-stack \{[^}]*z-index: 410;/);
  assert.match(CSS, /\.notice-stack > \.privacy-card \{[^}]*position: relative;/);
});

// ---- 3. who am I on the shared profile -------------------------------------------------

test('the first-visit card on the shared profile says where Switch profile is', { skip: dom.skip }, async () => {
  const { mod } = await boot();
  const c = mod.maybeShowSignInNotice({ who: 'user:d', shared: true, switchHint: true });
  const txt = c.textContent;
  assert.ok(txt.includes(EN.host_notice.shared));
  assert.ok(txt.includes("Tap Switch profile (the last button at the bottom of the bar) to use your own profile."));
  mod._resetForTest();
  const own = mod.maybeShowSignInNotice({ who: 'user:p', shared: false, switchHint: true });
  assert.ok(!own.textContent.includes(EN.host_notice.shared_switch), 'not on your own profile');
  assert.match(MAIN, /switchHint: !!\(p && p\.shared\) && !!dom\['switch-profile'\] && !dom\['switch-profile'\]\.classList\.contains\('hidden'\),/);
});

test('the chip\'s tooltip and name say "You\'re on {name} (shared profile)" on the default profile', () => {
  assert.equal(EN.chip.shared_title, "You're on {name} (shared profile)");
  const chrome = fn('paintProfileChrome');
  assert.match(chrome, /if \(p\.shared\) \{ const tip = t\('chip\.shared_title', \{ name: p\.name \}\); chip\.title = tip; chip\.setAttribute\('aria-label', tip\); \}/);
});

// ---- 4. the password card -----------------------------------------------------------------

test('"Set a password" is a once-per-device card with a Set password button, never a toast', { skip: dom.skip }, async () => {
  const { win, mod } = await boot();
  let opened = 0;
  assert.equal(mod.maybeShowPasswordCard({ ready: () => false, onSet: () => {} }), null, 'never over the sign-in screen');
  const c = mod.maybeShowPasswordCard({ ready: () => true, onSet: () => { opened++; } });
  assert.ok(c && c.isConnected);
  assert.equal(c.querySelector('h2').textContent, EN.password.prompt_title);
  const set = [...c.querySelectorAll('button')].find((b) => b.textContent === EN.password.save);
  set.click();
  assert.equal(opened, 1);
  assert.ok(!c.isConnected);
  assert.equal(mod.maybeShowPasswordCard({ ready: () => true, onSet: () => {} }), null, 'once per device');
  const reloaded = await import(`../static/js/host-notice.js?t=${Math.random()}`);
  assert.equal(reloaded.maybeShowPasswordCard({ ready: () => true, onSet: () => {} }), null, 'a reload remembers it');
  win.localStorage.clear();
  const c2 = reloaded.maybeShowPasswordCard({ ready: () => true, onSet: () => {} });
  const later = [...c2.querySelectorAll('button')].find((b) => b.textContent === EN.setup.not_now);
  later.click();
  assert.ok(!c2.isConnected, 'Not now dismisses it');
  assert.doesNotMatch(MAIN, /toast\(t\('password\.toast'\)\)/);
  assert.match(MAIN, /showPasswordCardWhenReady\(\)/);
});

// ---- 5. toasts ----------------------------------------------------------------------------

test('"Signed in as ..." lasts 2.5 s; toast() takes a lifetime', () => {
  assert.match(MAIN, /function toast\(msg, isError = false, \{ undo = null, ms = 0 \} = \{\}\)/);
  assert.match(MAIN, /toast\(name \? t\('signin\.welcome', \{ name \}\)[^;]*, false, \{ ms: 2500 \}\);/);
});

test('on a phone a toast sits above the tab bar', () => {
  const phone = CSS.indexOf('.toast { bottom: calc(168px + env(safe-area-inset-bottom)); }');
  assert.ok(phone > 0);
  const media = CSS.lastIndexOf('@media', phone);
  assert.match(CSS.slice(media, media + 40), /max-width: 768px/);
});

// ---- locales --------------------------------------------------------------------------------

test('every new key is in all eight locales', () => {
  const keys = ['lock.idle_landed', 'lock.default_disabled', 'lock.default_changed', 'host_notice.shared_switch', 'chip.shared_title'];
  for (const lang of LANGS) {
    const d = JSON.parse(read(`locales/${lang}.json`));
    for (const k of keys) {
      const v = k.split('.').reduce((o, p) => (o ? o[p] : undefined), d);
      assert.ok(typeof v === 'string' && v.length, `${lang}: ${k}`);
    }
  }
  assert.equal(EN.lock.landed, "You're on {name} now. To use your own profile, tap Switch profile.");
});
