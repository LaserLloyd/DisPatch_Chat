// 4.3 Fable: the Host allow-list in Settings > System > Network.
//
// Two schema rows (people.host_check, people.allowed_hosts) are host tier (HOST_KEYS mirrors
// backend settings_api.HOST_SETTING_KEYS), and under the mode row the Owner sees a small card
// built from GET /api/system/host-check: the mode in plain words, the addresses this DisPatch
// always answers to, and the unknown ones seen so far, each with an Allow button.

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { domSkip } from './_require-dom.js';

const HERE = dirname(fileURLToPath(import.meta.url));
const STATIC = join(HERE, '..', 'static');
const read = (f) => readFileSync(join(STATIC, f), 'utf8');
const HTML = read('index.html');
const EN = JSON.parse(read('locales/en.json'));

const require = createRequire(import.meta.url);
let jsdom = null;
try { jsdom = require('jsdom'); } catch { /* DOM tests skip */ }
const dom = { skip: domSkip(jsdom ? false : 'jsdom is not installed') };
const fresh = (f) => import(`../static/js/${f}?t=${Math.random()}`);

async function boot() {
  const win = new jsdom.JSDOM(HTML, { url: 'http://127.0.0.1:18880/', runScripts: 'outside-only' }).window;
  const put = (k, v) => Object.defineProperty(globalThis, k, { value: v, configurable: true, writable: true });
  for (const [k, v] of Object.entries({
    window: win, document: win.document, location: win.location, navigator: win.navigator,
    localStorage: win.localStorage, sessionStorage: win.sessionStorage, Node: win.Node, HTMLElement: win.HTMLElement,
    HTMLInputElement: win.HTMLInputElement,
  })) put(k, v);
  globalThis.fetch = async (u) => {
    if (u === '/static/locales/en.json') return { ok: true, status: 200, json: async () => EN, headers: new win.Headers() };
    return { ok: false, status: 404, json: async () => ({}), headers: new win.Headers() };
  };
  const i18n = await import('../static/js/i18n.js?v=5');
  await i18n.init('en');
  return win;
}

const SCHEMA = {
  schema: 1,
  groups: [{ id: 'people', label: 'People & Profiles', description: 'p' }],
  settings: [
    { key: 'people.host_check', type: 'choice', default: 'log', label: 'Unknown addresses', description: 'd', group: 'people',
      section: 'Network', level: 'basic', choices: ['log', 'enforce'],
      choice_labels: { log: 'Allow and list them', enforce: 'Block them' }, owner_only: true, ui_section: 'system#network' },
    { key: 'people.allowed_hosts', type: 'list', default: [], label: 'Other addresses for DisPatch', description: 'd',
      group: 'people', section: 'Network', level: 'basic', owner_only: true, ui_section: 'system#network' },
  ],
};

function makeApi(state) {
  return {
    settingsSchema: async () => SCHEMA,
    settings: async () => ({ schema: 1, values: {
      'people.host_check': { value: state.mode, source: 'store' },
      'people.allowed_hosts': { value: state.allowed, source: state.allowed.length ? 'store' : 'default' },
    } }),
    patchSettings: async (set) => {
      state.patches.push(set);
      if (set['people.allowed_hosts']) state.allowed = set['people.allowed_hosts'];
      if (set['people.host_check']) state.mode = set['people.host_check'];
      return { ok: true, changed: Object.keys(set) };
    },
    hostCheck: async () => {
      state.reports += 1;
      return { mode: state.mode, mode_from_env: false, answers_to: ['chatbox', 'localhost', '198.51.100.37'],
        unexpected: state.unexpected.filter((u) => !state.allowed.includes(u.name)) };
    },
  };
}

const slot = (win) => {
  const h = win.document.createElement('div');
  h.className = 'schema-host';
  win.document.body.append(h);
  return h;
};

test('HOST_KEYS carries the two Host-guard settings', async () => {
  const S = await fresh('settings_schema.js').catch(() => null);
  if (!S) return;
  assert.ok(S.HOST_KEYS.has('people.host_check'));
  assert.ok(S.HOST_KEYS.has('people.allowed_hosts'));
});

test('the Owner sees the card: mode, known addresses, unknown ones with Allow', { skip: dom.skip }, async () => {
  const win = await boot();
  const S = await fresh('settings_schema.js');
  const state = { mode: 'log', allowed: ['chat.home.arpa'], patches: [], reports: 0,
    unexpected: [{ name: 'evil.example', count: 3, last_seen: 1761900000 }, { name: '', count: 1, last_seen: 1761900001 }] };
  const host = slot(win);
  const toasts = [];
  await S.mountSchemaRows(host, { api: makeApi(state), section: 'system', anchor: 'network', claimed: new Set(['network']),
    role: 'admin', protected: true, force: true, toast: (m) => toasts.push(m) });
  const card = host.querySelector('.schema-host-check');
  assert.ok(card, 'the card is drawn');
  const row = host.querySelector('[data-key="people.host_check"]');
  assert.equal(row.nextElementSibling, card, 'right under the mode row');
  assert.match(card.textContent, new RegExp(EN.hostcheck.title));
  assert.match(card.textContent, new RegExp(EN.hostcheck.mode_log));
  assert.match(card.textContent, /198\.51\.100\.37/);
  const items = [...card.querySelectorAll('li.schema-host-check__item')];
  assert.equal(items.length, 2);
  assert.match(items[0].textContent, /evil\.example/);
  assert.match(items[1].textContent, new RegExp(EN.hostcheck.no_name.replace(/[()]/g, '\\$&')));
  assert.equal(items[1].querySelector('button'), null, 'a request with no address cannot be allowed');
  const allow = items[0].querySelector('button');
  assert.equal(allow.textContent, EN.hostcheck.allow);
  assert.equal(allow.getAttribute('aria-label'), EN.hostcheck.allow_label.replace('{name}', 'evil.example'));
  allow.click();
  for (let i = 0; i < 20 && !state.patches.length; i += 1) await new Promise((r) => setTimeout(r, 5));
  for (let i = 0; i < 20 && !toasts.length; i += 1) await new Promise((r) => setTimeout(r, 5));
  assert.deepEqual(state.patches[0], { 'people.allowed_hosts': ['chat.home.arpa', 'evil.example'] });
  assert.ok(toasts.some((m) => m.includes('evil.example')));
  const card2 = host.querySelector('.schema-host-check');
  assert.ok(card2 && !/evil\.example/.test([...card2.querySelectorAll('li')].map((l) => l.textContent).join(' ')),
    'the list is redrawn without the allowed name');
});

test('enforce mode and an empty list say so in plain words', { skip: dom.skip }, async () => {
  const win = await boot();
  const S = await fresh('settings_schema.js');
  const state = { mode: 'enforce', allowed: [], patches: [], reports: 0, unexpected: [] };
  const host = slot(win);
  await S.mountSchemaRows(host, { api: makeApi(state), section: 'system', anchor: 'network', claimed: new Set(['network']),
    role: 'admin', protected: true, force: true });
  const card = host.querySelector('.schema-host-check');
  assert.match(card.textContent, new RegExp(EN.hostcheck.mode_enforce));
  assert.match(card.textContent, new RegExp(EN.hostcheck.unknown_none));
});

test('a second Admin gets the locked rows and no card (the report is the Owner\'s)', { skip: dom.skip }, async () => {
  const win = await boot();
  const S = await fresh('settings_schema.js');
  const state = { mode: 'log', allowed: [], patches: [], reports: 0, unexpected: [] };
  const host = slot(win);
  await S.mountSchemaRows(host, { api: makeApi(state), section: 'system', anchor: 'network', claimed: new Set(['network']),
    role: 'admin', protected: false, force: true });
  assert.equal(host.querySelector('.schema-host-check'), null);
  assert.equal(state.reports, 0, 'not even fetched');
  assert.equal(host.querySelector('[data-key="people.host_check"] select').disabled, true);
});

test('a report that fails to load leaves a short note, never a broken page', { skip: dom.skip }, async () => {
  const win = await boot();
  const S = await fresh('settings_schema.js');
  const state = { mode: 'log', allowed: [], patches: [], reports: 0, unexpected: [] };
  const api = { ...makeApi(state), hostCheck: async () => { throw new Error('boom'); } };
  const host = slot(win);
  await S.mountSchemaRows(host, { api, section: 'system', anchor: 'network', claimed: new Set(['network']),
    role: 'admin', protected: true, force: true });
  assert.match(host.querySelector('.schema-host-check').textContent, new RegExp(EN.hostcheck.unavailable));
});

test('every hostcheck key is in all eight locales', () => {
  const dir = join(STATIC, 'locales');
  for (const f of readdirSync(dir).filter((x) => x.endsWith('.json'))) {
    const L = JSON.parse(readFileSync(join(dir, f), 'utf8'));
    for (const k of Object.keys(EN.hostcheck)) assert.ok(L.hostcheck && L.hostcheck[k], `${f}: hostcheck.${k}`);
  }
});
