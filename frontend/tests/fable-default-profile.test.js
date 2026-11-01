// 4.3 Fable (the owner's decision): the shared default profile never has a PIN or password.
// Settings -> People: the default profile's row says so in one sentence, and its editor offers no
// PIN field and no Save PIN; a refusal that still reaches a stale page reads as the server's one
// sentence. Settings -> Account on the default profile: no PIN control, the same sentence.

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
const EN = JSON.parse(read('locales/en.json'));
const SENTENCE = 'Shared default profile: no PIN. To give it one, pick another default profile first.';
const SERVER = 'The shared default profile cannot have a PIN or password. Pick another default profile in Settings > People first.';

const require = createRequire(import.meta.url);
let jsdom = null;
try { jsdom = require('jsdom'); } catch { /* not installed - DOM tests skip */ }
const skip = domSkip(jsdom ? false : 'jsdom is not installed');

const tick = () => new Promise((r) => setTimeout(r, 0));
const settle = async (n = 6) => { for (let i = 0; i < n; i += 1) await tick(); };
let apiRoutes = {};
const apiCalls = [];

async function boot() {
  const { JSDOM } = jsdom;
  const win = new JSDOM(HTML, { url: 'http://127.0.0.1:18885/', runScripts: 'outside-only' }).window;
  const put = (k, v) => Object.defineProperty(globalThis, k, { value: v, configurable: true, writable: true });
  for (const [k, v] of Object.entries({ window: win, document: win.document, location: win.location, navigator: win.navigator,
    localStorage: win.localStorage, Node: win.Node, HTMLElement: win.HTMLElement, HTMLInputElement: win.HTMLInputElement,
    CustomEvent: win.CustomEvent })) put(k, v);
  apiCalls.length = 0;
  globalThis.fetch = async (u, opts = {}) => {
    const key = `${(opts.method || 'GET').toUpperCase()} ${u}`;
    apiCalls.push(key);
    if (key === 'GET /static/locales/en.json') return { ok: true, status: 200, json: async () => EN, headers: new win.Headers() };
    const h = apiRoutes[key];
    const [status, body] = h ? h(opts) : [404, { detail: 'Not Found' }];
    return { ok: status >= 200 && status < 300, status, statusText: String(status), headers: new win.Headers(),
      json: async () => body, clone() { return this; } };
  };
  const i18n = await import('../static/js/i18n.js?v=5');
  await i18n.init('en');
  return win;
}

const P = (over) => ({ avatar: '', has_pin: false, has_password: false, budgets: {}, grants: [], denies: [], session_timeout: null,
  disabled: false, household_bots: 'all', no_credential: false, lock_target: false, ...over });
const PEOPLE = () => ({
  profiles: [
    P({ id: 'a1', name: 'Boss', role: 'admin', protected: true, has_password: true, has_pin: true }),
    P({ id: 'd1', name: 'DisPatch', role: 'user', lock_target: true }),
    P({ id: 'u1', name: 'Riley', role: 'user', has_pin: true }),
  ],
  me: 'a1', roles: ['basic', 'user', 'admin'],
  role_permissions: { admin: [], user: ['use_chat'], basic: ['use_chat'] },
  role_grantable: { admin: [], user: [], basic: [] },
  role_never: { admin: [], user: [], basic: [] },
  can_create_admin: true, owner_has_password: true, disclosure: 'x',
});

async function mount(extraRoutes = {}) {
  const win = await boot();
  apiRoutes = {
    'GET /api/people': () => [200, PEOPLE()],
    'GET /api/people/devices': () => [200, { devices: [], this_device: '' }],
    'GET /api/people/activity?days=7': () => [200, { summary: { days: [] }, recent: [], today: {} }],
    'GET /api/people/audit': () => [200, { entries: [] }],
    'GET /api/auth/api-token': () => [200, { has_token: false, machine_loopback_trust: false, machine_scopes: [], all_scopes: [] }],
    'GET /api/characters/all': () => [200, { characters: [] }],
    'GET /api/connections': () => [200, { connections: [] }],
    ...extraRoutes,
  };
  const mod = await import(`../static/js/people.js?t=${Math.random()}`);
  const perms = await import(`../static/js/people-perms.js?t=${Math.random()}`);
  const rec = { toasts: [] };
  const deps = { role: 'admin', toast: (m, err) => rec.toasts.push([m, !!err]), confirm: async () => true };
  const pane = win.document.getElementById('spane-people');
  await mod.mountPeopleSettings(pane, deps);
  await perms.renderPeoplePerms(pane, deps);
  await settle();
  return { win, pane, rec };
}

const btn = (root, text) => [...root.querySelectorAll('button')].find((b) => b.textContent.trim() === text);
const edit = async (pane, id) => { pane.querySelector(`[data-person="${id}"] button`).click(); await settle(); return pane.querySelector('#people-editor'); };

test('the new string is in every locale, and Account on the default profile says the same', () => {
  for (const loc of ['ar', 'de', 'en', 'es', 'fr', 'ja', 'pt', 'zh']) {
    const L = JSON.parse(read(`locales/${loc}.json`));
    assert.equal(typeof L.people.default_no_pin, 'string', loc);
  }
  assert.equal(EN.people.default_no_pin, SENTENCE);
  assert.ok(EN.acct.status_shared.startsWith(SENTENCE.replace(' pick another', ' an Admin picks another').replace(/ first\.$/, '')),
    EN.acct.status_shared);
  // Account: the default (shared) profile gets no PIN choice at all; the status line carries the sentence.
  const main = read('js/main.js');
  assert.match(main, /\} else if \(!\(a\.profile && a\.profile\.shared\)\) \{/);
  assert.match(main, /else if \(a\.profile && a\.profile\.shared\) key = 'acct\.status_shared';/);
});

test('People: the default profile row says it has no PIN, other rows do not', { skip }, async () => {
  const { pane } = await mount();
  const row = (id) => pane.querySelector(`[data-person="${id}"]`).textContent.replace(/\s+/g, ' ');
  assert.ok(row('d1').includes(SENTENCE), row('d1'));
  assert.ok(!row('u1').includes(SENTENCE));
  assert.ok(!row('a1').includes(SENTENCE));
});

test('People: the default profile editor offers no PIN field and no Save PIN; another profile does', { skip }, async () => {
  const { pane } = await mount();
  let ed = await edit(pane, 'd1');
  assert.equal(btn(ed, EN.people.pin_save), undefined, 'no Save PIN on the default profile');
  assert.equal(btn(ed, EN.people.password_save), undefined);
  const pinLabel = [...ed.querySelectorAll('label')].find((l) => l.textContent.trim() === EN.people.pin_change);
  assert.ok(!pinLabel || pinLabel.closest('[hidden]') || pinLabel.parentElement.hidden, 'the PIN field is hidden');
  assert.ok(ed.textContent.includes(SENTENCE));
  ed = await edit(pane, 'u1');
  assert.ok(btn(ed, EN.people.pin_save), 'Save PIN on a profile that is not the default');
  assert.ok(!ed.textContent.includes(SENTENCE));
});

test('People: a refusal reaching a stale page reads as the server sentence, once', { skip }, async () => {
  const { pane } = await mount({
    'POST /api/people/u1/pin': () => [409, { detail: SERVER, error: { code: 'default_profile_no_credential', message: SERVER,
      fix: 'Pick another default profile in Settings > People first.' } }],
  });
  const ed = await edit(pane, 'u1');
  const pin = [...ed.querySelectorAll('input[type="password"]')][0];
  pin.value = '8642';
  btn(ed, EN.people.pin_save).click();
  await settle();
  const err = ed.querySelector('.people-editor-error');
  assert.equal(err.hidden, false);
  assert.equal(err.textContent, SERVER);
});
