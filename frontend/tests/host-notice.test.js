// "Stored on the host computer" (js/host-notice.js).
//
// Run: node --test frontend/tests/
//
// Two homes and only two (2026-10-19: per-surface callouts were too loud):
// the permanent notice in Settings -> Security, and a one-time card on a
// device's first normal page load. These tests pin: the Security notice and its
// keys; the card shows once, "Got it" / Escape / a click outside dismiss it, the
// dismissal persists per device, a version bump shows it again, it is not shown
// while the app is not ready (lock screen, setup, wizard); every locale has real
// strings; the style is token-driven with danger only as an accent; and the
// other surfaces carry no callout.

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
const LANGS = ['en', 'ar', 'de', 'es', 'fr', 'ja', 'pt', 'zh'];
const LOC = Object.fromEntries(LANGS.map((l) => [l, JSON.parse(read(`locales/${l}.json`))]));
const EN = LOC.en;
const KEYS = ['title', 'files_body', 'chats_body', 'card_title', 'card_files', 'card_chats', 'card_more', 'card_ok'];

const require = createRequire(import.meta.url);
let jsdom = null;
try { jsdom = require('jsdom'); } catch { /* DOM tests skip */ }
const dom = { skip: domSkip(jsdom ? false : 'jsdom is not installed') };

async function boot() {
  const { JSDOM } = jsdom;
  const win = new JSDOM('<!doctype html><html><body><main id="app"><button id="other">x</button></main></body></html>', { url: 'http://127.0.0.1:18765/' }).window;
  const put = (k, v) => Object.defineProperty(globalThis, k, { value: v, configurable: true, writable: true });
  for (const [k, v] of Object.entries({ window: win, document: win.document, location: win.location,
    navigator: win.navigator, localStorage: win.localStorage, Node: win.Node, HTMLElement: win.HTMLElement })) put(k, v);
  globalThis.fetch = async (u) => (String(u).startsWith('/static/locales/en.json')
    ? { ok: true, status: 200, json: async () => EN, headers: new win.Headers() }
    : { ok: false, status: 404, json: async () => ({}), headers: new win.Headers() });
  const i18n = await import('../static/js/i18n.js?v=4');
  await i18n.init('en');
  const mod = await import(`../static/js/host-notice.js?t=${Math.random()}`);
  return { win, mod };
}
const card = (win) => win.document.querySelector('.privacy-card');
const key = (win, k) => win.document.dispatchEvent(new win.KeyboardEvent('keydown', { key: k, bubbles: true, cancelable: true }));

test('the first normal page load shows the card once; "Got it" dismisses it for good on this device', { skip: dom.skip }, async () => {
  const { win, mod } = await boot();
  const c = mod.maybeShowPrivacyCard();
  assert.ok(c && card(win), 'shown');
  assert.equal(c.getAttribute('role'), 'dialog');
  assert.equal(c.getAttribute('aria-modal'), 'false', 'a note, not a focus trap');
  assert.equal(c.querySelector('h2').textContent, EN.host_notice.card_title);
  assert.deepEqual([...c.querySelectorAll('li')].map((li) => li.textContent), [EN.host_notice.card_files, EN.host_notice.card_chats]);
  const ok = c.querySelector('.privacy-card-ok');
  assert.equal(ok.textContent, 'Got it');
  assert.equal(win.document.activeElement, ok);
  assert.equal(mod.maybeShowPrivacyCard(), null, 'never twice on one page');
  ok.click();
  assert.equal(card(win), null);
  assert.equal(win.localStorage.getItem(mod.PRIVACY_ACK_KEY), String(mod.PRIVACY_ACK_VERSION));
  mod._resetForTest();
  assert.equal(mod.maybeShowPrivacyCard(), null, 'remembered on this device');
});

test('Escape or a click outside dismisses it too', { skip: dom.skip }, async () => {
  let { win, mod } = await boot();
  mod.maybeShowPrivacyCard();
  key(win, 'Escape');
  assert.equal(card(win), null);
  assert.ok(mod.privacyAcked());
  ({ win, mod } = await boot());
  mod.maybeShowPrivacyCard();
  win.document.getElementById('other').dispatchEvent(new win.Event('pointerdown', { bubbles: true }));
  assert.equal(card(win), null);
  assert.ok(mod.privacyAcked());
});

test('a click inside the card does not dismiss it; not ready = not shown (and shown later)', { skip: dom.skip }, async () => {
  const { win, mod } = await boot();
  assert.equal(mod.maybeShowPrivacyCard({ ready: () => false }), null, 'lock screen / setup / wizard up');
  assert.equal(mod.privacyAcked(), false);
  const c = mod.maybeShowPrivacyCard({ ready: () => true });
  c.querySelector('li').dispatchEvent(new win.Event('pointerdown', { bubbles: true }));
  assert.ok(card(win));
});

test('a newer wording version shows it again on a device that dismissed an older one', { skip: dom.skip }, async () => {
  const { win, mod } = await boot();
  win.localStorage.setItem(mod.PRIVACY_ACK_KEY, String(mod.PRIVACY_ACK_VERSION - 1));
  assert.equal(mod.privacyAcked(), false);
  assert.ok(mod.maybeShowPrivacyCard());
  win.localStorage.setItem(mod.PRIVACY_ACK_KEY, 'junk');
  assert.equal(mod.privacyAcked(), false);
});

test('Settings -> Security holds the full notice (files and chats), with its keys', () => {
  const i = HTML.indexOf('id="spane-security"');
  const pane = HTML.slice(i, i + 3000);
  assert.match(pane, /<aside class="host-notice" role="note" id="sec-host-notice">/);
  for (const k of ['title', 'files_body', 'chats_body']) assert.match(pane, new RegExp(`data-i18n="host_notice\\.${k}"`));
});

test('no callout on the create/upload surfaces any more', () => {
  assert.equal((HTML.match(/class="host-notice"/g) || []).length, 1, 'only the Security one');
  for (const f of ['js/main.js', 'js/reactions.js', 'js/people.js', 'js/wizard.js', 'js/signin.js', 'js/chatdrop.js']) {
    assert.doesNotMatch(read(f), /hostNotice\(/, f);
  }
  const studio = readFileSync(join(STATIC, '..', '..', 'apps', 'imagestudio', 'static', 'studio.js'), 'utf8');
  assert.doesNotMatch(studio, /st-privacy/);
});

test('main.js shows the card after boot, never over the wizard, lock or setup screens', () => {
  const main = read('js/main.js');
  assert.match(main, /import \{ maybeShowPrivacyCard \} from '\.\/host-notice\.js\?v=1';/);
  assert.match(main, /if \(!wizard\) showPrivacyCardWhenReady\(\);/);
  assert.match(main, /!document\.querySelector\('\.wizard-backdrop'\)/);
  assert.match(main, /dom\['lock-screen'\]\.classList\.contains\('hidden'\)/);
  assert.match(read('js/privacy.js'), /'dispatch-privacy-ack'/);
  assert.match(read('sw.js'), /'\/static\/js\/host-notice\.js'/);
});

test('every locale has real strings for every key', () => {
  for (const l of LANGS) {
    const h = LOC[l].host_notice;
    assert.deepEqual(Object.keys(h).sort(), [...KEYS].sort(), l);
    for (const [k, v] of Object.entries(h)) {
      assert.ok(typeof v === 'string' && v.trim().length > 1, `${l}.${k}`);
      assert.doesNotMatch(v, /^\[[a-z]{2}\]/, `${l}.${k} is translated`);
      if (l !== 'en' && k !== 'card_ok') assert.notEqual(v, EN.host_notice[k], `${l}.${k} is not the English text`);
    }
  }
});

test('style: theme tokens, danger only as the accent edge and title', () => {
  const css = read('app.css');
  const note = /\n\.host-notice \{([^}]*)\}/.exec(css)[1];
  assert.match(note, /border-inline-start: 4px solid var\(--danger/);
  assert.match(note, /background: var\(--bg-tertiary\)/, 'a neutral surface, not a red block');
  assert.doesNotMatch(note, /danger-subtle/);
  const cardCss = /\n\.privacy-card \{([^}]*)\}/.exec(css)[1];
  assert.match(cardCss, /border-inline-start: 4px solid var\(--danger/);
  assert.match(cardCss, /background: var\(--bg-primary\)/);
  for (const r of [note, cardCss]) assert.doesNotMatch(r, /#[0-9a-f]{3,6}\b|\bred\b/i);
});

test('the chats wording matches the code: per character, Owner/Admin read all, not encrypted, backups', () => {
  const body = EN.host_notice.chats_body;
  assert.match(body, /not private and not end-to-end encrypted/);
  assert.match(body, /belongs to its character/);
  assert.match(body, /Owner and Admins can use every character/);
  assert.match(body, /backups/);
  assert.doesNotMatch(EN.restricted.banner, /not what was said/, 'the banner no longer says chats stay unread');
});
