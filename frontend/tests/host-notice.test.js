// "Stored on the host computer" and the sign-in notice (js/host-notice.js).
//
// Run: node --test frontend/tests/
//
// Two homes: the permanent notice in Settings -> Account, and a sign-in popup
// that comes up after every sign-in (once per sign-in, per tab). These tests pin:
// the popup shows once per sign-in and not again on a reload of the tab; a press
// anywhere, the corner ✕, "Got it" and Escape dismiss it; a press still reaches
// its target; it is not shown while the app is not ready (lock screen, setup,
// wizard); the shared-profile line and the PIN-reset notice; there is no
// permanent strip; main.js arms it at every sign-in and lock; every locale has
// real strings; the style is token-driven with danger only as an accent.

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
const HTML = read('index.html');
const CSS = read('app.css');
const MAIN = read('js/main.js');
const LANGS = ['en', 'ar', 'de', 'es', 'fr', 'ja', 'pt', 'zh'];
const LOC = Object.fromEntries(LANGS.map((l) => [l, JSON.parse(read(`locales/${l}.json`))]));
const EN = LOC.en;
const KEYS = ['title', 'files_body', 'chats_body', 'card_title', 'card_files', 'card_chats', 'card_more', 'card_ok',
  'shared', 'pin_reset'];
// Added in English in all eight locales (Foundation); WP-LOC translates them, then they leave this set.
// 4.0: card_chats was reworded at INT-1 and files_body / chats_body at INT-2 (chats and files are
// private to each profile now); WP-LOC translates them with the other two.
const PENDING_TRANSLATION = new Set(['shared', 'pin_reset', 'card_chats', 'files_body', 'chats_body']);

const require = createRequire(import.meta.url);
let jsdom = null;
try { jsdom = require('jsdom'); } catch { /* DOM tests skip */ }
const dom = { skip: domSkip(jsdom ? false : 'jsdom is not installed') };

async function boot() {
  const { JSDOM } = jsdom;
  const win = new JSDOM('<!doctype html><html><body><main id="app"><button id="other">x</button></main></body></html>', { url: 'http://127.0.0.1:18765/' }).window;
  const put = (k, v) => Object.defineProperty(globalThis, k, { value: v, configurable: true, writable: true });
  for (const [k, v] of Object.entries({ window: win, document: win.document, location: win.location,
    navigator: win.navigator, localStorage: win.localStorage, sessionStorage: win.sessionStorage,
    Node: win.Node, HTMLElement: win.HTMLElement })) put(k, v);
  globalThis.fetch = async (u) => (String(u).startsWith('/static/locales/en.json')
    ? { ok: true, status: 200, json: async () => EN, headers: new win.Headers() }
    : { ok: false, status: 404, json: async () => ({}), headers: new win.Headers() });
  const i18n = await import('../static/js/i18n.js?v=4');
  await i18n.init('en');
  return { win, mod: await load() };
}
// A fresh module instance over the same window: the same tab, reloaded.
const load = () => import(`../static/js/host-notice.js?t=${Math.random()}`);
const card = (win) => win.document.querySelector('.privacy-card');
const key = (win, k) => win.document.dispatchEvent(new win.KeyboardEvent('keydown', { key: k, bubbles: true, cancelable: true }));
const press = (win, el) => el.dispatchEvent(new win.Event('pointerdown', { bubbles: true }));

test('the sign-in popup shows once per sign-in; "Got it" dismisses it; a reload of the tab does not show it again', { skip: dom.skip }, async () => {
  const { win, mod } = await boot();
  const c = mod.maybeShowSignInNotice({ who: 'user:p1' });
  assert.ok(c && card(win), 'shown');
  assert.equal(c.getAttribute('role'), 'dialog');
  assert.equal(c.getAttribute('aria-modal'), 'false', 'a note, not a focus trap');
  assert.equal(c.querySelector('h2').textContent, EN.host_notice.card_title);
  assert.deepEqual([...c.querySelectorAll('li')].map((li) => li.textContent), [EN.host_notice.card_files, EN.host_notice.card_chats]);
  const ok = c.querySelector('.privacy-card-ok');
  assert.equal(ok.textContent, EN.host_notice.card_ok);
  assert.equal(win.document.activeElement, ok);
  assert.equal(mod.maybeShowSignInNotice({ who: 'user:p1' }), null, 'never twice on one page');
  ok.click();
  assert.equal(card(win), null);
  const reloaded = await load();
  assert.equal(reloaded.maybeShowSignInNotice({ who: 'user:p1' }), null, 'remembered for this tab');
});

test('a new sign-in shows it again: armSignInNotice forgets the tab, and takes down a card left on screen', { skip: dom.skip }, async () => {
  const { win, mod } = await boot();
  mod.maybeShowSignInNotice({ who: 'user:p1' });
  mod.armSignInNotice();
  assert.equal(card(win), null, 'the popup from the previous sign-in is gone');
  assert.ok(mod.maybeShowSignInNotice({ who: 'user:p1' }), 'shown for the new sign-in');
});

test('a press anywhere dismisses it, and the press still reaches its target', { skip: dom.skip }, async () => {
  const { win, mod } = await boot();
  mod.maybeShowSignInNotice({ who: 'user:p1' });
  let clicks = 0;
  win.document.getElementById('other').addEventListener('click', () => { clicks++; });
  press(win, win.document.getElementById('other'));
  assert.equal(card(win), null, 'a press outside dismisses it');
  win.document.getElementById('other').click();
  assert.equal(clicks, 1, 'the click still does what it was aimed at');

  mod._resetForTest();
  mod.maybeShowSignInNotice({ who: 'user:p1' });
  press(win, card(win).querySelector('li'));
  assert.equal(card(win), null, 'a press inside dismisses it too');
});

test('the corner ✕ and Escape dismiss it', { skip: dom.skip }, async () => {
  let { win, mod } = await boot();
  const c = mod.maybeShowSignInNotice({ who: 'user:p1' });
  const x = c.querySelector('.privacy-card-close');
  assert.equal(x.getAttribute('aria-label'), EN.common.close, 'the close button is named "Close"');
  x.click();
  assert.equal(card(win), null);
  ({ win, mod } = await boot());
  mod.maybeShowSignInNotice({ who: 'user:p1' });
  key(win, 'Escape');
  assert.equal(card(win), null);
});

test('not ready (lock screen, setup or wizard up) = not shown, and shown once it is ready', { skip: dom.skip }, async () => {
  const { win, mod } = await boot();
  assert.equal(mod.maybeShowSignInNotice({ who: 'user:p1', ready: () => false }), null);
  assert.equal(win.sessionStorage.getItem('dispatch-signin-notice'), null, 'not used up while not shown');
  assert.ok(mod.maybeShowSignInNotice({ who: 'user:p1', ready: () => true }));
});

test('the shared-profile line and the PIN-reset notice show when given; other notice kinds do not', { skip: dom.skip }, async () => {
  const { win, mod } = await boot();
  const c = mod.maybeShowSignInNotice({
    who: 'user:dp', shared: true,
    notices: [{ kind: 'pin_reset', by: 'Admin', at: '2026-10-22 18:00' }, { kind: 'something_else', by: 'x', at: 'y' }],
  });
  assert.ok(c.querySelector('.privacy-card-shared'), 'shared line');
  assert.equal(c.querySelector('.privacy-card-shared').textContent, EN.host_notice.shared);
  const reset = c.querySelector('.privacy-card-notice');
  assert.equal(reset.textContent, EN.host_notice.pin_reset.replace('{by}', 'Admin').replace('{at}', '2026-10-22 18:00'));
  assert.equal(c.querySelectorAll('.privacy-card-notice').length, 1, 'only pin_reset is shown');
  mod._resetForTest();
  const plain = mod.maybeShowSignInNotice({ who: 'user:p2' });
  assert.equal(plain.querySelector('.privacy-card-shared'), null);
  assert.equal(plain.querySelector('.privacy-card-notice'), null);
});

test('a non-Admin is told what an Admin can see: in the popup (by code, translated) and under the Account notice', { skip: dom.skip }, async () => {
  const { win, mod } = await boot();
  const told = EN.people.disclosure_activity;
  const c = mod.maybeShowSignInNotice({ who: 'user:p1', disclosure: { disclosure_code: 'activity_summary', disclosure: 'server text' } });
  assert.equal(c.querySelector('.privacy-card-disclosure').textContent, told);
  mod._resetForTest();
  const unknown = mod.maybeShowSignInNotice({ who: 'user:p2', disclosure: { disclosure_code: 'newer_code', disclosure: 'server text' } });
  assert.equal(unknown.querySelector('.privacy-card-disclosure').textContent, 'server text', 'an unknown code falls back');
  mod._resetForTest();
  const sharedOne = mod.maybeShowSignInNotice({ who: 'user:dp', shared: true, disclosure: { disclosure_code: 'activity_summary' } });
  assert.equal(sharedOne.querySelector('.privacy-card-disclosure'), null, 'the shared profile: only the shared line');
  assert.ok(sharedOne.querySelector('.privacy-card-shared'));
  mod._resetForTest();
  const admin = mod.maybeShowSignInNotice({ who: 'admin:p3', disclosure: null });
  assert.equal(admin.querySelector('.privacy-card-disclosure'), null, 'an Admin is told nothing');
  const p = win.document.createElement('p');
  p.hidden = true;
  mod.paintDisclosure(p, { disclosure_code: 'activity_summary' });
  assert.equal(p.hidden, false);
  assert.equal(p.textContent, told);
  assert.equal(p.getAttribute('data-i18n'), 'people.disclosure_activity', 're-translated on a language change');
  mod.paintDisclosure(p, null);
  assert.equal(p.hidden, true);
  assert.equal(p.getAttribute('data-i18n'), null);
  mod.paintDisclosure(null, null);   // no node: no throw
});

test('Settings -> Account has the disclosure line, hidden until the auth state fills it', () => {
  const i = HTML.indexOf('id="sec-host-notice"');
  const aside = HTML.slice(i, HTML.indexOf('</aside>', i));
  assert.match(aside, /<p class="host-notice-body host-notice-disclosure" id="host-notice-disclosure" hidden><\/p>/);
  assert.match(MAIN, /paintDisclosure\(document\.getElementById\('host-notice-disclosure'\),\n\s+state\.auth\.profile && state\.auth\.profile\.shared \? null : state\.auth\.disclosure\);/);
  assert.match(MAIN, /disclosure: \(s\.disclosure_code \|\| s\.disclosure\)/);
});

test('the popup never mentions the Restricted tier, which is gone', { skip: dom.skip }, async () => {
  const { mod } = await boot();
  const c = mod.maybeShowSignInNotice({ who: 'restricted:x' });
  assert.doesNotMatch(c.textContent, /Restricted|restricted/);
});

test('no permanent strip: index.html and app.css carry no restricted banner, and main.js paints none', () => {
  assert.doesNotMatch(HTML, /id="restricted-banner"|class="restricted-banner"/);
  assert.doesNotMatch(CSS, /restricted-banner|has-restricted-banner|--rb-h/);
  assert.doesNotMatch(MAIN, /restricted-banner|paintRestrictedBanner|has-restricted-banner|--rb-h/);
});

test('the popup is a top-centre note with a neutral surface and danger only as the accent', () => {
  const cardCss = /\n\.privacy-card \{([^}]*)\}/.exec(CSS)[1];
  assert.match(cardCss, /top: 16px; left: 50%/);
  assert.match(cardCss, /border-inline-start: 4px solid var\(--danger/);
  assert.match(cardCss, /background: var\(--bg-primary\)/);
  assert.doesNotMatch(cardCss, /#[0-9a-f]{3,6}\b|\bred\b/i);
  const note = /\n\.host-notice \{([^}]*)\}/.exec(CSS)[1];
  assert.match(note, /background: var\(--bg-tertiary\)/, 'a neutral surface, not a red block');
  assert.doesNotMatch(note, /danger-subtle/);
});

test('main.js arms the notice at every sign-in and at lock, and shows it through one function', () => {
  assert.match(MAIN, /import \{ maybeShowSignInNotice, armSignInNotice, paintDisclosure \} from '\.\/host-notice\.js\?v=3';/);
  assert.doesNotMatch(MAIN, /maybeShowPrivacyCard|showPrivacyCardWhenReady/);
  // PIN unlock, profile sign-in, first Owner's setup, and the lock itself.
  assert.match(MAIN, /armSignInNotice\(\);[^\n]*\n\s*\/\/ 3\.0: the install PIN signs in/);
  assert.match(MAIN, /if \(r && r\.profile && !state\.auth\.profile\) state\.auth\.profile = r\.profile;\n  armSignInNotice\(\);/);
  assert.match(MAIN, /armSignInNotice\(\);\n  if \(wasStarted\) await reboot\(\); else \{ await startApp\(\); showSignInNoticeWhenReady\(\); \}/);
  assert.match(MAIN, /async function lockToTarget\(\{ announce = false \} = \{\}\) \{\n  if \(lockBusy\) return;[^\n]*\n  lockBusy = true;\n  try \{\n    armSignInNotice\(\);/);
  assert.match(MAIN, /if \(!wizard\) showSignInNoticeWhenReady\(\);/);
  assert.match(MAIN, /onClosed: \(\) => showSignInNoticeWhenReady\(\)/);
  const fn = MAIN.slice(MAIN.indexOf('function showSignInNoticeWhenReady'), MAIN.indexOf('function showSignInNoticeWhenReady') + 600);
  assert.match(fn, /!document\.querySelector\('\.wizard-backdrop'\)/);
  assert.match(fn, /dom\['lock-screen'\]\.classList\.contains\('hidden'\)/);
  assert.match(fn, /shared: !!\(p && p\.shared\)/);
  assert.match(fn, /notices: state\.auth\.notices/);
  assert.match(fn, /disclosure: state\.auth\.disclosure/);
  assert.doesNotMatch(MAIN.slice(MAIN.indexOf('setInterval(() => {'), MAIN.indexOf('setInterval(() => {') + 400), /showSignInNoticeWhenReady/, 'never from the minute tick');
  assert.match(read('sw.js'), /'\/static\/js\/host-notice\.js'/);
});

test('every locale has real strings for every key', () => {
  for (const l of LANGS) {
    const h = LOC[l].host_notice;
    assert.deepEqual(Object.keys(h).sort(), [...KEYS].sort(), l);
    for (const [k, v] of Object.entries(h)) {
      assert.ok(typeof v === 'string' && v.trim().length > 1, `${l}.${k}`);
      assert.doesNotMatch(v, /^\[[a-z]{2}\]/, `${l}.${k} is translated`);
      if (l !== 'en' && k !== 'card_ok' && !PENDING_TRANSLATION.has(k)) {
        assert.notEqual(v, EN.host_notice[k], `${l}.${k} is not the English text`);
      }
    }
  }
});

test('the card text says chats are private to their profile, and that the backups can be read', () => {
  const body = EN.host_notice.card_chats;
  assert.match(body, /private to that profile/);
  assert.match(body, /shared DisPatch profile/);
  assert.match(body, /backups/);
  assert.doesNotMatch(body, /same character/, 'the character rule is gone');
});

test('Settings -> Account holds the full notice (files and chats), with its keys, first', () => {
  const i = HTML.indexOf('id="spane-account"');
  const pane = HTML.slice(i, HTML.indexOf('id="spane-voice"'));
  for (const k of ['title', 'files_body', 'chats_body']) assert.match(pane, new RegExp(`data-i18n="host_notice\\.${k}"`));
  assert.match(pane, /<aside class="host-notice" role="note" id="sec-host-notice" data-anchor="notice">/);
  assert.ok(pane.indexOf('id="dev-group-pictures"') < pane.indexOf('id="sec-host-notice"'));
  assert.equal(EN.host_notice.card_more, 'The details are in Settings → Account.');
});

test('the markup fallback of the Settings notice says what en.json says', () => {
  const unesc = (x) => x.replace(/&#39;/g, "'").replace(/&amp;/g, '&').replace(/\s+/g, ' ').trim();
  for (const k of ['title', 'files_body', 'chats_body']) {
    const m = HTML.match(new RegExp(`data-i18n="host_notice\\.${k}">([^<]*)<`));
    assert.ok(m, k);
    assert.equal(unesc(m[1]), EN.host_notice[k], `index.html host_notice.${k} matches en.json`);
  }
});

test('no callout on the create/upload surfaces', () => {
  assert.equal((HTML.match(/class="host-notice"/g) || []).length, 1, 'only the Settings one');
  for (const f of ['js/main.js', 'js/reactions.js', 'js/people.js', 'js/wizard.js', 'js/signin.js', 'js/chatdrop.js']) {
    assert.doesNotMatch(read(f), /hostNotice\(/, f);
  }
});

test('the wizard reports its closing ("Set up later" too), so the popup follows it at once', { skip: dom.skip }, async () => {
  const { win } = await boot();
  win.sessionStorage.clear();
  const W = await import(`../static/js/wizard.js?t=${Math.random()}`);
  let closed = 0;
  W.openWizard({ needed: true, claimed: true, steps: [] }, { api: {}, onClosed: () => { closed++; } });
  const later = [...win.document.querySelectorAll('.wizard button')].find((b) => b.textContent === EN.wiz.later);
  assert.ok(later, 'the Set up later button');
  later.click();
  assert.equal(closed, 1);
  assert.equal(win.document.querySelector('.wizard-backdrop'), null);
});
