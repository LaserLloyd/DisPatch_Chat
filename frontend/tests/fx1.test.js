// DisPatch 4.3 FX1: a Basic profile (use_chat, no create_characters) reorders its own bots on a
// phone. Settings -> Characters is offered to every profile that may chat; for Basic it is Mine
// only, read-only except the order (grip + ☰ Move up / Move down) and the Tools & more toggle:
// no Details editor, no New / AI build, no Household tab. The rows' `actions` already say so
// (GET /api/characters/manage, F1a); this file pins the section gate and the page's Basic view.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { domSkip } from './_require-dom.js';
import { SECTIONS, charactersSectionAllowed } from '../static/js/settings-nav.js';

const HERE = dirname(fileURLToPath(import.meta.url));
const STATIC = join(HERE, '..', 'static');
const read = (rel) => readFileSync(join(STATIC, rel), 'utf8');
const EN = JSON.parse(read('locales/en.json'));
const HTML = read('index.html');

const require = createRequire(import.meta.url);
let jsdom = null;
try { jsdom = require('jsdom'); } catch { /* DOM tests skip */ }
const dom = { skip: domSkip(jsdom ? false : 'jsdom is not installed') };

// ------------------------------------------------------------------ the section gate

const canOf = (perms) => (p) => perms.includes('*') || perms.includes(p);

test('gate: Characters is every chatting profile\'s (Basic included), never a profile that cannot chat', () => {
  assert.equal(SECTIONS.find((s) => s.id === 'characters').perm, 'use_chat');
  assert.equal(charactersSectionAllowed(canOf(['use_chat', 'use_voice'])), true, 'Basic');
  assert.equal(charactersSectionAllowed(canOf(['use_chat', 'create_characters'])), true, 'User');
  assert.equal(charactersSectionAllowed(canOf(['manage_characters'])), true, 'household manager');
  assert.equal(charactersSectionAllowed(canOf(['*'])), true, 'open install');
  assert.equal(charactersSectionAllowed(canOf([])), false, 'no permissions');
  assert.equal(charactersSectionAllowed(canOf(['use_voice'])), false);
  assert.equal(charactersSectionAllowed(null), false, 'no checker: nothing');
  assert.equal(charactersSectionAllowed(() => { throw new Error('x'); }), false, 'a failing checker: nothing');
});

test('every new string exists in all eight locales', () => {
  for (const lang of ['ar', 'de', 'en', 'es', 'fr', 'ja', 'pt', 'zh']) {
    const L = JSON.parse(read(`locales/${lang}.json`));
    assert.equal(typeof (L.cpage || {}).lead_mine_order, 'string', `${lang}: cpage.lead_mine_order`);
  }
});

// ------------------------------------------------------------------ the page, as Basic

const store = new Map();
globalThis.localStorage ??= {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
  removeItem: (k) => store.delete(k),
};

const BASIC_ACTIONS = { edit: false, details: false, hide: false, pictures: false, lookups: false, voice: false, reset: false,
  delete: false, stop_sharing: false, share: false, make_copy: false, reactions: false, avatar_pool: false, reorder: true, voice_extras: false };
const ME_BASIC = { id: 'b1', role: 'basic', protected: false, can_manage_household: false, can_create: false };
const row = (over = {}) => ({
  id: 'x', name: 'X', emoji: '', avatar: '', description: '', kind: 'copy', backend_kind: 'direct', hidden: false, order: 0,
  badges: { voice: false, pictures: false, reactions: false, avatar_pool: false, lookups: true, quick_actions: true },
  template: { id: 't', name: 'T' }, overridden: [], give_to: null, copies: [], actions: { ...BASIC_ACTIONS }, voice_link: null, ...over,
});
const basicMine = () => ({ me: ME_BASIC, profiles: [], household: [], mine: [
  row({ id: 'b-atlas', name: 'Atlas', order: 0 }), row({ id: 'b-wikka', name: 'Wikka', order: 1 })] });

const tick = () => new Promise((r) => setTimeout(r, 0));
const settle = async (n = 8) => { for (let i = 0; i < n; i += 1) await tick(); };
let calls = [];
let routes = {};

async function mount(data, extra = {}, deps = {}) {
  const win = new jsdom.JSDOM(HTML, { url: 'http://127.0.0.1:18871/', runScripts: 'outside-only' }).window;
  const put = (k, v) => Object.defineProperty(globalThis, k, { value: v, configurable: true, writable: true });
  put('window', win); put('document', win.document); put('location', win.location);
  put('navigator', win.navigator); put('localStorage', win.localStorage);
  put('Node', win.Node); put('HTMLElement', win.HTMLElement); put('HTMLInputElement', win.HTMLInputElement);
  calls = [];
  routes = { 'GET /static/locales/en.json': () => [200, EN], 'GET /api/characters/manage': () => [200, data], ...extra };
  globalThis.fetch = async (u, opts = {}) => {
    const key = `${(opts.method || 'GET').toUpperCase()} ${u}`;
    const body = typeof opts.body === 'string' ? JSON.parse(opts.body) : null;
    calls.push({ key, body });
    const h = routes[key];
    const [status, out] = h ? h(body) : [404, { detail: 'Not Found' }];
    return { ok: status >= 200 && status < 300, status, statusText: String(status), headers: new win.Headers(), json: async () => out, clone() { return this; } };
  };
  const i18n = await import('../static/js/i18n.js?v=5');
  await i18n.init('en');
  const mod = await import(`../static/js/characters-page.js?t=${Math.random()}`);
  const host = win.document.getElementById('chars-page-host');
  mod.mountCharactersPage(host, { deps: { toast: () => {}, ...deps }, ask: async () => 'ok' });
  await settle();
  return { win, host, mod };
}
const rowEl = (host, id) => host.querySelector(`.cp-row[data-bot="${id}"]`);
const menuIds = (host, id) => {
  rowEl(host, id).querySelector('.cp-menu-btn').click();
  const m = rowEl(host, id).querySelector('.cp-menu');
  const out = m ? [...m.querySelectorAll('[data-item]')].map((b) => b.dataset.item) : [];
  rowEl(host, id).querySelector('.cp-menu-btn').click();
  return out;
};

test('Basic: Mine only, nothing to edit or create; the lead says how to change the order', { skip: dom.skip }, async () => {
  const parked = new Set();
  const { host } = await mount(basicMine(), {}, { isMenuBot: (id) => parked.has(id), toggleMenuBot: (id) => { if (parked.has(id)) parked.delete(id); else parked.add(id); return parked.has(id); } });
  assert.deepEqual([...host.querySelectorAll('.cp-row')].map((r) => r.dataset.bot), ['b-atlas', 'b-wikka']);
  assert.equal(host.querySelector('.cp-tabs').hidden, true, 'no Household tab');
  assert.equal(host.querySelector('#cp-tab-household'), null);
  assert.equal(host.querySelector('#mb-new'), null, 'no New');
  assert.equal(host.querySelector('#mb-build'), null, 'no AI build');
  assert.equal(host.querySelector('.cp-details-btn'), null, 'no Details editor');
  assert.equal(host.querySelectorAll('.cp-quick button').length, 0, 'no quick toggles');
  assert.equal(host.querySelectorAll('.cp-row .cp-handle').length, 2, 'a grip on each row');
  assert.equal(host.querySelector('.cp-lead').textContent, EN.cpage.lead_mine_order);
  assert.deepEqual(menuIds(host, 'b-atlas'), ['move_down', 'park']);
  assert.deepEqual(menuIds(host, 'b-wikka'), ['move_up', 'park']);
});

test('Basic: Move down PUTs its own order; the park item toggles the injected toggleMenuBot', { skip: dom.skip }, async () => {
  const parked = new Set();
  const data = basicMine();
  const { host } = await mount(data, { 'PUT /api/me/characters/order': (b) => [200, { ok: true, order: b.ids }] },
    { isMenuBot: (id) => parked.has(id), toggleMenuBot: (id) => { parked.add(id); return true; } });
  rowEl(host, 'b-atlas').querySelector('.cp-menu-btn').click();
  rowEl(host, 'b-atlas').querySelector('[data-item="move_down"]').click();
  await settle();
  assert.deepEqual(calls.filter((c) => c.key === 'PUT /api/me/characters/order')[0].body, { ids: ['b-wikka', 'b-atlas'] });
  rowEl(host, 'b-wikka').querySelector('.cp-menu-btn').click();
  rowEl(host, 'b-wikka').querySelector('[data-item="park"]').click();
  await settle();
  assert.ok(parked.has('b-wikka'));
  assert.ok(!calls.some((c) => c.key.startsWith('PATCH ')), 'nothing else is written');
});

test('a profile that may edit keeps the old lead (User / Owner unchanged)', { skip: dom.skip }, async () => {
  const data = basicMine();
  data.me = { ...ME_BASIC, role: 'user', can_create: true };
  data.mine = data.mine.map((r) => ({ ...r, actions: { ...BASIC_ACTIONS, edit: true, details: true } }));
  const { host } = await mount(data);
  assert.equal(host.querySelector('.cp-lead').textContent, EN.cpage.lead_mine);
  assert.ok(host.querySelector('.cp-details-btn'));
});
