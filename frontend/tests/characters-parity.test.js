// DisPatch 4.3 (F1b, legacy D.1): the Characters page does everything the old roster did.
//   - Mine in `order`; reorder by a grip (pointer events: mouse, pen, touch) and by ☰ Move up /
//     Move down (keyboard, screen readers: "Moved X to position N"), PUT /api/me/characters/order;
//   - Details -> Abilities: Reactions + Avatar pool switches when the row's actions allow them;
//   - the voice add-on's `ext.bots.rowExtra` control in Details, saving only voice / voice_rp;
//   - ☰ Show in Tools & more only / Show on the rail again (menubots.js, this device);
//   - the rail drag for every signed-in profile through the new route;
//   - parity: a direct copy's Details still has connection, model, description, personality.
// HTTP is mocked (F1a builds the server half in parallel).
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { domSkip } from './_require-dom.js';

const HERE = dirname(fileURLToPath(import.meta.url));
const STATIC = join(HERE, '..', 'static');
const read = (rel) => readFileSync(join(STATIC, rel), 'utf8');
const EN = JSON.parse(read('locales/en.json'));
const HTML = read('index.html');
const MAIN = read('js/main.js');

const require = createRequire(import.meta.url);
let jsdom = null;
try { jsdom = require('jsdom'); } catch { /* DOM tests skip */ }
const dom = { skip: domSkip(jsdom ? false : 'jsdom is not installed') };

const store = new Map();
globalThis.localStorage ??= {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
  removeItem: (k) => store.delete(k),
};
const K = await import('../static/js/characters-page.js?v=parity');

const ACTIONS = { edit: true, hide: true, pictures: true, lookups: true, voice: false, reset: false, delete: true, stop_sharing: false, share: false, make_copy: false };
const row = (over = {}) => ({
  id: 'atlas', name: 'Atlas', emoji: '🌍', avatar: '', description: 'General chat', kind: 'own', backend_kind: 'direct',
  connection: 'c1', model: 'm1', hidden: false, order: 0, voice: '', voice_rp: '',
  badges: { voice: false, pictures: false, reactions: false, avatar_pool: false, lookups: true, quick_actions: true },
  template: null, overridden: [], give_to: null, copies: [], actions: { ...ACTIONS }, voice_link: null,
  ...over,
});
const R = { ...ACTIONS, reorder: true };
const ids = (list) => list.map((x) => x.id);

// ------------------------------------------------------------------ pure

test('sortByOrder: Mine follows `order`; rows without one keep the server order after the rest', () => {
  const rows = [row({ id: 'c', order: 2 }), row({ id: 'x', order: undefined }), row({ id: 'a', order: 0 }), row({ id: 'b', order: 1 })];
  assert.deepEqual(ids(K.sortByOrder(rows)), ['a', 'b', 'c', 'x']);
  assert.deepEqual(ids(K.sortByOrder([row({ id: 'p', order: 1 }), row({ id: 'q', order: 1 })])), ['p', 'q'], 'stable');
});

test('reorderPlan: moves among the reorderable rows only; other rows keep their slots', () => {
  const rows = [row({ id: 'a', actions: R }), row({ id: 'get', kind: 'template', actions: { make_copy: true } }), row({ id: 'b', actions: R }), row({ id: 'c', actions: R })];
  const down = K.reorderPlan(rows, 'a', 1);
  assert.deepEqual(down.ids, ['b', 'a', 'c']);
  assert.deepEqual(ids(down.rows), ['b', 'get', 'a', 'c'], 'the Get-my-copy row stays where it was');
  assert.equal(down.position, 2);
  assert.equal(K.reorderPlan(rows, 'a', 0), null, 'no move, no write');
  assert.equal(K.reorderPlan(rows, 'get', 0), null, 'a row without actions.reorder never moves');
  assert.deepEqual(K.reorderPlan(rows, 'c', 0).ids, ['c', 'a', 'b']);
  assert.deepEqual(K.reorderIds(rows), ['a', 'b', 'c']);
});

test('rowMenu: Move up / down and the Tools & more toggle come from the page context, never on a template', () => {
  assert.deepEqual(ids(K.rowMenu(row(), {})), ['edit', 'delete'], 'no context, no new items (old callers unchanged)');
  assert.deepEqual(ids(K.rowMenu(row(), { up: true, down: true, parked: false })), ['edit', 'move_up', 'move_down', 'park', 'delete']);
  assert.deepEqual(ids(K.rowMenu(row(), { parked: true })), ['edit', 'unpark', 'delete']);
  assert.ok(!ids(K.rowMenu(row({ kind: 'template' }), { parked: false })).includes('park'));
  const sprite = read('ui-theme/icons.svg');
  for (const it of K.rowMenu(row(), { up: true, down: true, parked: false }).filter((m) => m.icon)) {
    assert.ok(sprite.includes(`<symbol id="${it.icon}"`), `${it.id}: ${it.icon} is in the sprite`);
    assert.ok(EN.cpage[it.key.split('.')[1]], `${it.id} has a label`);
  }
  assert.ok(sprite.includes('<symbol id="grip-vertical"') && sprite.includes('<symbol id="wrench"'));
});

test('extrasPatch: only the add-on fields, only strings', () => {
  assert.deepEqual(K.extrasPatch({ voice: 'x', name: 'y', voice_rp: 'z', hidden: true }), { voice: 'x', voice_rp: 'z' });
  assert.deepEqual(K.extrasPatch({ voice: 3, persona: 'p' }), {});
  assert.deepEqual(K.extrasPatch(null), {});
});

test('detailPatch: Reactions and Avatar pool join the PATCH only when they changed', () => {
  const r = row({ badges: { reactions: false, avatar_pool: true } });
  assert.deepEqual(K.detailPatch(r, {}, { reactions: true, avatar_pool: true }), { reactions: true });
  assert.deepEqual(K.detailPatch(r, {}, { avatar_pool: false }), { avatar_pool: false });
  assert.deepEqual(K.detailPatch(r, {}, {}), {});
});

test('rail drag: every signed-in profile drags its own bots; an unclaimed install keeps the Admin-only household order', () => {
  const mine = { id: 'm', scope: 'personal' };
  const house = { id: 'h', scope: 'household' };
  const user = { claimed: true, signedIn: true, profile: { id: 'u1' }, admin: false };
  assert.equal(K.railDragAllowed(mine, user), true, 'a User reorders their own copies');
  assert.equal(K.railDragAllowed(house, user), false);
  assert.equal(K.railDragAllowed(mine, { claimed: true, signedIn: false, profile: null, admin: false }), false, 'signed out: no drag');
  assert.equal(K.railDragAllowed(house, { claimed: true, signedIn: true, profile: { id: 'a' }, admin: true }), false, 'a signed-in Admin moves only their own bots');
  assert.equal(K.railDragAllowed(house, { claimed: false, admin: true }), true, 'unclaimed: the household order, Admin');
  assert.equal(K.railDragAllowed(house, { claimed: false, admin: false }), false, 'the old refusal holds: unclaimed non-Admin');
});

test('the rail drag for a User calls PUT /api/me/characters/order with their own bots, never the household route', async () => {
  const seen = [];
  const call = async (method, url, o) => { seen.push([method, url, o && o.json]); return { ok: true }; };
  let legacy = 0;
  const saveOrder = async () => { legacy += 1; };
  const bots = [{ id: 'b', scope: 'personal' }, { id: 'x', scope: 'household' }, { id: 'a', scope: 'personal' }];
  await K.saveRailOrder(bots, { claimed: true, signedIn: true, profile: { id: 'u1' }, admin: false }, { call, saveOrder });
  assert.deepEqual(seen, [['PUT', '/api/me/characters/order', { ids: ['b', 'a'] }]]);
  assert.equal(legacy, 0);
  // A signed-in Admin: the same new route.
  seen.length = 0;
  await K.saveRailOrder(bots, { claimed: true, signedIn: true, profile: { id: 'a1' }, admin: true }, { call, saveOrder });
  assert.equal(seen[0][1], '/api/me/characters/order');
  assert.equal(legacy, 0);
  // Unclaimed: the old household route, with the whole roster.
  seen.length = 0;
  let payload = null;
  await K.saveRailOrder(bots, { claimed: false, admin: true }, { call, saveOrder: async (p) => { payload = p; } });
  assert.deepEqual(payload, [{ id: 'b', order: 0 }, { id: 'x', order: 1 }, { id: 'a', order: 2 }]);
  assert.equal(seen.length, 0);
});

test('main.js wiring: the rail uses railDragAllowed + saveRailOrder; the page gets the add-on slot and the park toggle', () => {
  assert.match(MAIN, /draggable: railDragAllowed\(bot, railAuth\(\)\) \? 'true' : 'false'/);
  assert.match(MAIN, /if \(railDragAllowed\(bot, railAuth\(\)\)\) \{\s+btn\.addEventListener\('dragstart'/);
  const persist = MAIN.slice(MAIN.indexOf('async function persistSidebarOrder('), MAIN.indexOf('function repaintPreserving('));
  assert.match(persist, /saveRailOrder\(state\.bots, railAuth\(\)/);
  const code = persist.split('\n').filter((l) => !l.trim().startsWith('//')).join('\n');
  assert.equal((code.match(/api\.saveOrder/g) || []).length, 1, 'api.saveOrder only as the unclaimed fallback');
  assert.match(code, /saveOrder: \(payload\) => api\.saveOrder\(payload\)/);
  assert.match(MAIN, /mountCharactersPage\(document\.getElementById\('chars-page-host'\), \{ anchor, deps: \{ openTab: \(tab\) => setSettingsTab\(tab\), botRowExtrasFor, isMenuBot, toggleMenuBot, renderSidebar,\s+isShared: /);
  assert.match(MAIN, /init === initCharactersPage \? \{ \.\.\.profileDeps, botRowExtrasFor, isMenuBot, toggleMenuBot, renderSidebar \}/);
  const hook = MAIN.slice(MAIN.indexOf('function botRowExtra('), MAIN.indexOf('function botRowExtrasFor('));
  assert.equal((hook.match(/refreshCharactersPage\(\{ extras: true \}\);/g) || []).length, 2, 'register and dispose both redraw the page');
});

test('every new string exists in all eight locales with the same placeholders', () => {
  const keys = ['cpage.menu_move_up', 'cpage.menu_move_down', 'cpage.moved', 'cpage.reorder_hint', 'cpage.drag_tip', 'cpage.menu_park', 'cpage.menu_unpark', 'cpage.parked_tag', 'cpage.parked_tip', 'cpage.parked_done', 'cpage.unparked_done', 'cpage.switch_reactions', 'cpage.switch_reactions_hint', 'cpage.switch_pool', 'cpage.switch_pool_hint', 'cpage.voice_extras', 'cpage.voice_extras_hint', 'cpage.voice_saved'];
  const ph = (s) => (String(s).match(/\{\w+\}/g) || []).sort().join();
  assert.ok(keys.length >= 15);
  for (const lang of ['ar', 'de', 'en', 'es', 'fr', 'ja', 'pt', 'zh']) {
    const L = JSON.parse(read(`locales/${lang}.json`));
    for (const k of keys) {
      const v = k.split('.').reduce((o, p) => (o ? o[p] : undefined), L);
      const en = k.split('.').reduce((o, p) => (o ? o[p] : undefined), EN);
      assert.equal(typeof v, 'string', `${lang}: ${k}`);
      assert.equal(ph(v), ph(en), `${lang}: ${k} placeholders`);
    }
  }
});

// ------------------------------------------------------------------ DOM

const tick = () => new Promise((r) => setTimeout(r, 0));
const settle = async (n = 8) => { for (let i = 0; i < n; i += 1) await tick(); };
const wait = async () => { await new Promise((r) => setTimeout(r, 200)); await settle(); };
let calls = [];
let routes = {};

async function boot() {
  const win = new jsdom.JSDOM(HTML, { url: 'http://127.0.0.1:18885/', runScripts: 'outside-only' }).window;
  const put = (k, v) => Object.defineProperty(globalThis, k, { value: v, configurable: true, writable: true });
  put('window', win); put('document', win.document); put('location', win.location);
  put('navigator', win.navigator); put('localStorage', win.localStorage);
  put('Node', win.Node); put('HTMLElement', win.HTMLElement); put('HTMLInputElement', win.HTMLInputElement);
  calls = [];
  globalThis.fetch = async (u, opts = {}) => {
    const key = `${(opts.method || 'GET').toUpperCase()} ${u}`;
    const body = typeof opts.body === 'string' ? JSON.parse(opts.body) : null;
    calls.push({ key, body });
    const h = routes[key];
    const [status, out] = h ? h(body) : [404, { detail: 'Not Found' }];
    return { ok: status >= 200 && status < 300, status, statusText: String(status), headers: new win.Headers(), json: async () => out, clone() { return this; } };
  };
  const i18n = await import('../static/js/i18n.js?v=5');
  routes['GET /static/locales/en.json'] = () => [200, EN];
  await i18n.init('en');
  return win;
}
const callsTo = (key) => calls.filter((c) => c.key === key);

const ME_USER = { id: 'u1', role: 'user', protected: false, can_manage_household: false, can_create: true };
const ME_OWNER = { id: 'a1', role: 'admin', protected: true, can_manage_household: true, can_create: true };
const DETAIL = (name) => [200, { name, emoji: '', description: 'd', backend: { kind: 'direct', connection: 'c1', model: 'm1' }, personality: { text: 'Kind.' } }];

async function mount(data, extra = {}, deps = {}) {
  const win = await boot();
  routes = { ...routes, 'GET /api/characters/manage': () => [200, data], ...extra };
  const mod = await import(`../static/js/characters-page.js?t=${Math.random()}`);
  const host = win.document.getElementById('chars-page-host');
  const toasts = [];
  mod.mountCharactersPage(host, { deps: { toast: (m, bad) => toasts.push([m, !!bad]), ...deps }, ask: async () => 'ok' });
  await settle();
  return { win, host, toasts, mod };
}
const rowEl = (host, id) => host.querySelector(`.cp-row[data-bot="${id}"]`);
const openMenu = (host, id) => { rowEl(host, id).querySelector('.cp-menu-btn').click(); return rowEl(host, id).querySelector('.cp-menu'); };
const menuItem = (host, id, item) => {
  const m = openMenu(host, id);
  const it = m && m.querySelector(`[data-item="${item}"]`);
  if (!it) rowEl(host, id).querySelector('.cp-menu-btn').click();   // close it again
  return it;
};
const openDetails = async (host, id) => { rowEl(host, id).querySelector('.cp-details-btn').click(); await settle(); return rowEl(host, id).querySelector('.cp-details'); };

const userMine = () => ({
  me: ME_USER, profiles: [], household: [],
  mine: [
    row({ id: 'c3', name: 'Cleo', order: 2, actions: R }),
    row({ id: 'c1', name: 'Ada', order: 0, actions: R }),
    row({ id: 'c2', name: 'Bo', order: 1, actions: R }),
  ],
});

test('Mine is drawn in `order`', { skip: dom.skip }, async () => {
  const { host } = await mount(userMine());
  assert.deepEqual([...host.querySelectorAll('.cp-row')].map((r) => r.dataset.bot), ['c1', 'c2', 'c3']);
});

test('reorder: Move up / Move down only with actions.reorder; Move down PUTs the right ids and announces the place', { skip: dom.skip }, async () => {
  let data = userMine();
  const { host } = await mount(data, {
    'GET /api/characters/manage': () => [200, data],
    'PUT /api/me/characters/order': (b) => [200, { ok: true, order: b.ids }],
  });
  assert.equal(menuItem(host, 'c1', 'move_up'), null, 'the first row has no Move up');
  const down = menuItem(host, 'c1', 'move_down');
  assert.ok(down);
  assert.match(down.textContent, /Move down/);
  assert.ok(down.querySelector('svg use'), 'an icon from the sprite');
  down.click();
  await settle();
  assert.deepEqual(callsTo('PUT /api/me/characters/order')[0].body, { ids: ['c2', 'c1', 'c3'] });
  assert.deepEqual([...host.querySelectorAll('.cp-row')].map((r) => r.dataset.bot), ['c2', 'c1', 'c3'], 'drawn at once');
  const live = host.querySelector('.cp-live');
  assert.equal(live.getAttribute('aria-live'), 'polite');
  assert.equal(live.textContent, 'Moved Ada to position 2');
  assert.equal(document.activeElement, rowEl(host, 'c1').querySelector('.cp-menu-btn'), 'focus stays on the moved row');
  // Move up from the last row.
  const up = menuItem(host, 'c3', 'move_up');
  up.click();
  await settle();
  assert.deepEqual(callsTo('PUT /api/me/characters/order')[1].body.ids, ['c2', 'c3', 'c1']);

  // Without actions.reorder (a server that does not allow it): no items, no grip, no hint.
  data = userMine(); data.mine = data.mine.map((r) => ({ ...r, actions: ACTIONS }));
  const again = await mount(data);
  assert.equal(menuItem(again.host, 'c1', 'move_down'), null);
  assert.equal(again.host.querySelector('.cp-handle'), null);
  assert.equal(again.host.querySelector('.cp-reorder-hint').hidden, true);
});

test('reorder: with a filter or a search on, no Move items or grip, and a hint says how to reorder', { skip: dom.skip }, async () => {
  const { host } = await mount(userMine());
  assert.ok(host.querySelector('.cp-row .cp-handle'));
  const search = host.querySelector('.cp-search');
  search.value = 'a';
  search.dispatchEvent(new window.Event('input'));
  await settle();
  const hint = host.querySelector('.cp-reorder-hint');
  assert.equal(hint.hidden, false);
  assert.equal(hint.textContent, 'Clear the filter to reorder');
  assert.equal(host.querySelector('.cp-handle'), null);
  assert.equal(menuItem(host, 'c1', 'move_down'), null);
});

test('reorder: dragging the grip (pointer events: works with touch) PUTs the new order', { skip: dom.skip }, async () => {
  const { host, win } = await mount(userMine(), { 'PUT /api/me/characters/order': (b) => [200, { ok: true, order: b.ids }] });
  const rows = [...host.querySelectorAll('.cp-row')];
  rows.forEach((r, i) => { r.getBoundingClientRect = () => ({ top: i * 60, height: 60, bottom: i * 60 + 60, left: 0, right: 300, width: 300 }); });
  const handle = rowEl(host, 'c1').querySelector('.cp-handle');
  assert.equal(handle.getAttribute('aria-hidden'), 'true', 'keyboard users have Move up / down instead');
  assert.match(handle.title, /^Drag to change the order \(or use Move up and Move down/, 'the tooltip names the keyboard way too');
  assert.ok(handle.querySelector('svg use'));
  const ev = (type, y) => new win.MouseEvent(type, { bubbles: true, clientY: y, button: 0 });
  handle.dispatchEvent(ev('pointerdown', 30));
  assert.ok(rowEl(host, 'c1').classList.contains('cp-row--dragging'));
  handle.dispatchEvent(ev('pointermove', 170));   // past Bo (60-120) and Cleo's middle (150)
  assert.ok(rowEl(host, 'c3').classList.contains('cp-drop-after'));
  handle.dispatchEvent(ev('pointerup', 170));
  await settle();
  assert.deepEqual(callsTo('PUT /api/me/characters/order')[0].body, { ids: ['c2', 'c3', 'c1'] });
  assert.match(host.querySelector('.cp-live').textContent, /Moved Ada to position 3/);
  assert.equal(host.querySelector('.cp-row--dragging'), null);
  // A cancelled drag (the phone scrolled, a call came in) writes nothing.
  const h2 = rowEl(host, 'c2').querySelector('.cp-handle');
  h2.dispatchEvent(ev('pointerdown', 10)); h2.dispatchEvent(ev('pointermove', 170)); h2.dispatchEvent(ev('pointercancel', 170));
  await settle();
  assert.equal(callsTo('PUT /api/me/characters/order').length, 1);
});

test('Details -> Abilities: the Reactions and Avatar pool switches when the actions allow them; Reactions PATCHes {reactions:true}', { skip: dom.skip }, async () => {
  const data = {
    me: ME_OWNER, profiles: [], household: [],
    mine: [
      row({ id: 'bits', name: 'Bits', kind: 'agent', backend_kind: 'agent', actions: { ...ACTIONS, reactions: true, avatar_pool: true, delete: false } }),
      row({ id: 'copy', name: 'Copy', kind: 'copy', template: { id: 't', name: 'T' }, actions: { ...ACTIONS, reactions: false, avatar_pool: false } }),
    ],
  };
  const { host } = await mount(data, {
    'GET /api/me/characters/bits': () => [200, { name: 'Bits', emoji: '🌍', description: 'General chat', backend: { kind: 'agent' }, personality: { text: '' } }],
    'GET /api/me/characters/copy': DETAIL.bind(null, 'Copy'),
    'GET /api/me/backends': () => [200, { connections: [{ id: 'c1', name: 'Rig' }] }],
    'GET /api/connections': () => [200, { connections: [{ id: 'c1', name: 'Rig' }] }],
    'PATCH /api/characters/bits': () => [200, { id: 'bits' }],
  });
  const d = await openDetails(host, 'bits');
  const react = d.querySelector('[data-feature="reactions"] input[role="switch"]');
  const pool = d.querySelector('[data-feature="avatar_pool"] input[role="switch"]');
  assert.ok(react && pool);
  assert.match(d.querySelector('[data-feature="reactions"] label').textContent, /Reactions/);
  assert.match(d.querySelector('[data-feature="avatar_pool"] label').textContent, /New picture each chat/);
  react.checked = true;
  [...d.querySelectorAll('.btn-primary')].find((b) => b.textContent === 'Save details').click();
  await settle();
  assert.deepEqual(callsTo('PATCH /api/characters/bits')[0].body, { reactions: true });
  const c = await openDetails(host, 'copy');
  assert.equal(c.querySelector('[data-feature="reactions"]'), null, 'a copy has no Reactions switch');
  assert.equal(c.querySelector('[data-feature="avatar_pool"]'), null);
});

test('the add-on slot: a rowExtra node renders in Details; its onChange PATCHes {voice} and drops {name}', { skip: dom.skip }, async () => {
  const data = { me: ME_USER, profiles: [], household: [], mine: [row({ id: 'c1', name: 'Ada', voice: 'v0', actions: { ...R, voice_extras: true } }), row({ id: 'c2', name: 'Bo', actions: R })] };
  let change = null;
  const seen = [];
  const botRowExtrasFor = (bot, onChange) => {
    seen.push(bot); change = onChange;
    return [Object.assign(document.createElement('button'), { className: 'fake-voice-pill', textContent: 'Voice: v0' })];
  };
  const { host, toasts } = await mount(data, {
    'GET /api/me/characters/c1': DETAIL.bind(null, 'Ada'),
    'GET /api/me/characters/c2': DETAIL.bind(null, 'Bo'),
    'GET /api/me/backends': () => [200, { connections: [] }],
    'PATCH /api/characters/c1': () => [200, { id: 'c1' }],
  }, { botRowExtrasFor });
  const d = await openDetails(host, 'c1');
  const slot = d.querySelector('[data-feature="extras"]');
  assert.ok(slot && slot.querySelector('.fake-voice-pill'), 'the add-on control is in Details -> Abilities');
  assert.match(slot.textContent, /Voice/);
  assert.equal(seen[0].id, 'c1');
  assert.equal(seen[0].voice, 'v0', 'the add-on sees the saved voice');
  change({ voice: 'x', name: 'y' });
  await settle();
  assert.deepEqual(callsTo('PATCH /api/characters/c1').map((c) => c.body), [{ voice: 'x' }]);
  assert.ok(toasts.some(([m]) => m === 'Voice saved for Ada'));
  change({ name: 'y' });
  await settle();
  assert.equal(callsTo('PATCH /api/characters/c1').length, 1, 'nothing but voice / voice_rp is ever sent');
  // A row without actions.voice_extras gets no slot.
  const d2 = await openDetails(host, 'c2');
  assert.equal(d2.querySelector('[data-feature="extras"]'), null);
});

test('refreshCharactersPage({ extras: true }) draws an open panel again (the add-on came after the page)', { skip: dom.skip }, async () => {
  const data = { me: ME_USER, profiles: [], household: [], mine: [row({ id: 'c1', name: 'Ada', actions: { ...R, voice_extras: true } })] };
  const extras = [];
  const botRowExtrasFor = () => extras.map(() => document.createElement('span'));
  const { host, mod } = await mount(data, { 'GET /api/me/characters/c1': DETAIL.bind(null, 'Ada'), 'GET /api/me/backends': () => [200, { connections: [] }] }, { botRowExtrasFor });
  let d = await openDetails(host, 'c1');
  assert.equal(d.querySelector('[data-feature="extras"]'), null);
  extras.push(1);
  mod.refreshCharactersPage();
  await wait();
  assert.equal(rowEl(host, 'c1').querySelector('[data-feature="extras"]'), null, 'a plain refresh keeps the panel');
  mod.refreshCharactersPage({ extras: true });
  await wait();
  d = rowEl(host, 'c1').querySelector('.cp-details');
  assert.ok(d.querySelector('[data-feature="extras"]'));
});

test('park: ☰ Show in Tools & more only toggles the injected toggleMenuBot; the row says so; templates have no such item', { skip: dom.skip }, async () => {
  const parked = new Set();
  const toggled = [];
  let rails = 0;
  const deps = {
    isMenuBot: (id) => parked.has(id),
    toggleMenuBot: (id) => { toggled.push(id); if (parked.has(id)) parked.delete(id); else parked.add(id); return parked.has(id); },
    renderSidebar: () => { rails += 1; },
  };
  const data = {
    me: ME_OWNER, profiles: [],
    mine: [row({ id: 'doxy', name: 'Doxy', kind: 'agent', backend_kind: 'agent', actions: R })],
    household: [row({ id: 'tpl', name: 'Tpl', kind: 'template', give_to: [], actions: { ...ACTIONS, stop_sharing: true } })],
  };
  const { host, toasts } = await mount(data, {}, deps);
  const park = menuItem(host, 'doxy', 'park');
  assert.ok(park);
  assert.match(park.textContent, /Show in Tools & more only/);
  assert.ok(park.querySelector('svg use'));
  park.click();
  await settle();
  assert.deepEqual(toggled, ['doxy']);
  assert.equal(rails, 1, 'the rail repaints');
  assert.ok(toasts.some(([m]) => /Doxy now shows in Tools & more/.test(m)));
  const tag = rowEl(host, 'doxy').querySelector('.cp-parked-tag');
  assert.ok(tag);
  assert.equal(tag.textContent, 'In Tools & more');
  const unpark = menuItem(host, 'doxy', 'unpark');
  assert.match(unpark.textContent, /Back to the bar/);
  unpark.click();
  await settle();
  assert.deepEqual(toggled, ['doxy', 'doxy']);
  assert.equal(rowEl(host, 'doxy').querySelector('.cp-parked-tag'), null);
  host.querySelector('[data-tab="household"]').click();
  await settle();
  assert.equal(menuItem(host, 'tpl', 'park'), null, 'a template is on nobody\'s rail');
});

test('parity: a direct copy\'s Details still has connection, model, description and personality', { skip: dom.skip }, async () => {
  const data = { me: ME_USER, profiles: [], household: [], mine: [row({ id: 'cp', name: 'Atlas', kind: 'copy', template: { id: 'atlas', name: 'Atlas' }, actions: R })] };
  const { host } = await mount(data, {
    'GET /api/me/characters/cp': () => [200, { name: 'Atlas', emoji: '🌍', description: 'General chat', backend: { kind: 'direct', connection: 'c1', model: 'm1' }, personality: { text: 'Curious.' } }],
    'GET /api/me/backends': () => [200, { connections: [{ id: 'c1', name: 'Rig' }] }],
  });
  const d = await openDetails(host, 'cp');
  const conn = d.querySelector('select[id$="-conn"]');
  assert.ok(conn, 'connection');
  assert.equal(conn.value, 'c1');
  assert.equal(d.querySelector('input[id$="-model"]').value, 'm1', 'model');
  assert.equal(d.querySelector('input[id$="-description"]').value, 'General chat', 'description');
  assert.equal(d.querySelector('textarea[id$="-persona"]').value, 'Curious.', 'personality');
  for (const input of d.querySelectorAll('input, select, textarea')) {
    assert.ok(d.querySelector(`label[for="${input.id}"]`), `${input.id} has a label`);
  }
});

test('CSS: the grip works by touch (touch-action: none) and the new rules are cp-* only', () => {
  const css = read('profiles.css');
  assert.match(css, /\.cp-handle \{[^}]*touch-action: none;/);
  assert.match(css, /\.cp-handle \{[^}]*min-height: 44px;/, 'a finger-sized target at 390 px');
  assert.match(css, /\.cp-drop-before \{/);
  assert.match(css, /\.cp-parked-tag \{/);
});
