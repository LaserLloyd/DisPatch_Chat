// Host-storage notices: "this is kept on the computer that runs DisPatch".
//
// Run: node --test frontend/tests/
//
// Every surface where a person creates or uploads something the server keeps
// carries a red notice in the THEME's danger tokens, and Settings says the same
// about the chats. These tests pin: the module draws a titled <aside role=note>
// in the active language; the four static notices are in index.html; the JS
// surfaces (composer attachments, Reactions, People & Profiles, the wizard's
// Done step, the Restricted "What can the owner see?" panel) call it; every
// locale has real strings for every kind; the style is token-driven; and the
// chats wording says what the code does (a chat belongs to its character).

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
const KINDS = ['upload', 'fileserver', 'images', 'chats', 'chats_restricted'];

const require = createRequire(import.meta.url);
let jsdom = null;
try { jsdom = require('jsdom'); } catch { /* DOM tests skip */ }
const dom = { skip: domSkip(jsdom ? false : 'jsdom is not installed') };

async function boot() {
  const { JSDOM } = jsdom;
  const win = new JSDOM('<!doctype html><html><body></body></html>', { url: 'http://127.0.0.1:18765/' }).window;
  const put = (k, v) => Object.defineProperty(globalThis, k, { value: v, configurable: true, writable: true });
  for (const [k, v] of Object.entries({ window: win, document: win.document, location: win.location,
    navigator: win.navigator, localStorage: win.localStorage, Node: win.Node, HTMLElement: win.HTMLElement })) put(k, v);
  globalThis.fetch = async (u) => (String(u).startsWith('/static/locales/en.json')
    ? { ok: true, status: 200, json: async () => EN, headers: new win.Headers() }
    : { ok: false, status: 404, json: async () => ({}), headers: new win.Headers() });
  const i18n = await import('../static/js/i18n.js?v=4');
  await i18n.init('en');
  return win;
}

test('hostNotice draws a titled note for each kind, in the active language', { skip: dom.skip }, async () => {
  await boot();
  const { hostNotice, HOST_NOTICE_KEYS } = await import('../static/js/host-notice.js');
  assert.deepEqual(Object.keys(HOST_NOTICE_KEYS).sort(), [...KINDS].sort());
  for (const kind of KINDS) {
    const n = hostNotice(kind);
    assert.equal(n.tagName, 'ASIDE');
    assert.equal(n.getAttribute('role'), 'note');
    assert.equal(n.dataset.kind, kind);
    assert.ok(n.classList.contains('host-notice'));
    assert.equal(n.querySelector('.host-notice-title').textContent, EN.host_notice[`${kind}_title`]);
    assert.equal(n.querySelector('.host-notice-body').textContent.trim(), EN.host_notice[`${kind}_body`]);
  }
  assert.ok(hostNotice('upload', { compact: true }).classList.contains('host-notice-compact'));
});

test('every locale has real strings for every kind (no [xx] placeholders), and the same keys as English', () => {
  const want = KINDS.flatMap((k) => [`${k}_title`, `${k}_body`]).sort();
  for (const l of LANGS) {
    const h = LOC[l].host_notice;
    assert.ok(h, `${l}: host_notice section`);
    assert.deepEqual(Object.keys(h).sort(), want, l);
    for (const [k, v] of Object.entries(h)) {
      assert.ok(typeof v === 'string' && v.trim().length > 4, `${l}.${k}`);
      assert.doesNotMatch(v, /^\[[a-z]{2}\]/, `${l}.${k} is translated`);
      if (l !== 'en') assert.notEqual(v, EN.host_notice[k], `${l}.${k} is not the English text`);
    }
  }
});

test('the four static notices are in index.html with their keys', () => {
  const pane = (id) => {
    const i = HTML.indexOf(`id="${id}"`);
    assert.ok(i > 0, id);
    return HTML.slice(i, i + 3000);
  };
  for (const [id, kind] of [['spane-security', 'chats'], ['fileserver-backdrop', 'fileserver'],
    ['drop-backdrop', 'upload'], ['crop-backdrop', 'images']]) {
    const near = pane(id);
    assert.match(near, new RegExp(`<aside class="host-notice" role="note" data-kind="${kind}">`), `${id} has the ${kind} notice`);
    assert.match(near, new RegExp(`data-i18n="host_notice\\.${kind}_title"`));
    assert.match(near, new RegExp(`data-i18n="host_notice\\.${kind}_body"`));
  }
});

test('the JS surfaces call hostNotice with the right kind', () => {
  const calls = {
    'js/main.js': /if \(state\.attachments\.length\) p\.append\(hostNotice\('upload', \{ compact: true \}\)\)/,
    'js/reactions.js': /add\.append\(hostNotice\('images'\)\)/,
    'js/people.js': /hostNotice\('chats'\)/,
    'js/wizard.js': /list, hostNotice\('chats'\), actions\(/,
    'js/signin.js': /hostNotice\('chats_restricted'\)/,
  };
  for (const [f, re] of Object.entries(calls)) {
    const src = read(f);
    assert.match(src, /import \{ hostNotice \} from '\.\/host-notice\.js\?v=1';/, `${f} imports it`);
    assert.match(src, re, f);
  }
  assert.match(read('sw.js'), /'\/static\/js\/host-notice\.js'/, 'precached with the shell');
});

test('the Restricted panel shows the plain chats notice under the summary line', { skip: dom.skip }, async () => {
  const win = await boot();
  win.HTMLDialogElement.prototype.showModal = function showModal() { this.open = true; };
  const { openRestrictedPanel } = await import(`../static/js/signin.js?t=${Math.random()}`);
  await openRestrictedPanel({ fetchActivity: async () => ({ today: {}, limits: {}, summary: [] }) });
  const note = win.document.querySelector('.restricted-panel .host-notice');
  assert.ok(note, 'the notice is in the panel');
  assert.equal(note.dataset.kind, 'chats_restricted');
  assert.match(note.textContent, /not secret/);
});

test('the style is the theme danger tokens, not a fixed red', () => {
  const css = read('app.css');
  const rule = /\n\.host-notice \{([^}]*)\}/.exec(css);
  assert.ok(rule, '.host-notice rule');
  assert.match(rule[1], /color: var\(--danger-text/);
  assert.match(rule[1], /border: 1px solid var\(--danger-border/);
  assert.match(rule[1], /background: var\(--danger-subtle/);
  assert.doesNotMatch(rule[1], /#[0-9a-f]{3,6}\b|\bred\b/i);
});

test('the chats wording matches the code: per character, Owner/Admin read all, not encrypted, backups', () => {
  const body = EN.host_notice.chats_body;
  assert.match(body, /not private and not end-to-end encrypted/);
  assert.match(body, /belongs to its character/);
  assert.match(body, /Owner and Admins can use every character/);
  assert.match(body, /backups/);
  assert.doesNotMatch(EN.restricted.banner, /not what was said/, 'the banner no longer says chats stay unread');
});
