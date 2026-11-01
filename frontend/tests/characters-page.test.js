// Settings -> Characters (4.2): Mine | Household tabs, rows drawn from the server's `actions` /
// `badges`, quick buttons, the give-to dialog flow, filter + search, and the wiring that folds
// "My bots" into the Mine tab. Pure helpers first, then the DOM against a stubbed fetch.
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
const K = await import('../static/js/characters-page.js?v=2');
const G = await import('../static/js/give-to.js?v=1');

const ACTIONS = { edit: true, hide: true, pictures: true, lookups: true, voice: true, reset: false, delete: true, stop_sharing: false, share: false, make_copy: false };
const row = (over = {}) => ({
  id: 'atlas', name: 'Atlas', emoji: '🌍', avatar: '', description: 'General chat', kind: 'own', backend_kind: 'direct',
  connection: 'c1', model: 'm1', hidden: false,
  badges: { voice: true, pictures: false, reactions: false, lookups: true, quick_actions: true, managed: false, kit_chats: 0 },
  template: null, overridden: [], give_to: null, copies: [], actions: { ...ACTIONS }, voice_link: { section: 'voice', anchor: 'voices', character: 'atlas' },
  ...over,
});
const ids = (list) => list.map((x) => x.id);

// ------------------------------------------------------------------ pure

test('backend pill: Agent / Direct / App / Older API each carry words and an icon, not colour alone', () => {
  const sprite = read('ui-theme/icons.svg');
  for (const kind of ['agent', 'direct', 'app', 'legacy-api']) {
    const p = K.backendPill({ backend_kind: kind });
    assert.ok(EN.cpage[p.key.split('.')[1]], `${kind} has a label`);
    assert.ok(EN.cpage[p.titleKey.split('.')[1]], `${kind} has a tooltip`);
    assert.ok(sprite.includes(`<symbol id="${p.icon}"`), `${kind} icon ${p.icon} is in the sprite`);
  }
  assert.equal(K.backendPill({ backend_kind: 'agent' }).cls, 'ui-badge--accent');
  assert.notEqual(K.backendPill({ backend_kind: 'agent' }).cls, K.backendPill({ backend_kind: 'direct' }).cls);
});

test('rowBadges: only what is on, plus linked / customised for a copy', () => {
  assert.deepEqual(ids(K.rowBadges(row())), ['voice', 'lookups', 'quick']);
  const copy = row({ kind: 'copy', template: { id: 'atlas', name: 'Atlas' }, overridden: [] });
  assert.ok(ids(K.rowBadges(copy)).includes('linked'));
  const custom = K.rowBadges({ ...copy, overridden: ['name', 'persona'] }).find((b) => b.id === 'custom');
  assert.equal(custom.vars.count, 2);
  assert.ok(!ids(K.rowBadges({ ...copy, overridden: ['name'] })).includes('linked'));
  const sprite = read('ui-theme/icons.svg');
  const every = K.rowBadges(row({ kind: 'copy', overridden: ['x'], badges: { voice: 1, pictures: 1, reactions: 1, lookups: 1, quick_actions: 1 } }));
  for (const b of every) assert.ok(sprite.includes(`<symbol id="${b.icon}"`), b.icon);
});

test('rowQuick: exactly the buttons the row\'s actions allow; labels follow the state', () => {
  assert.deepEqual(ids(K.rowQuick(row())), ['hide', 'pictures', 'lookups', 'voice']);
  const hid = K.rowQuick(row({ hidden: true }))[0];
  assert.equal(hid.key, 'cpage.quick_show');
  assert.deepEqual(hid.patch, { hidden: false });
  const p = K.rowQuick(row({ badges: { pictures: true } })).find((q) => q.id === 'pictures');
  assert.deepEqual(p.patch, { image_jobs: false });
  assert.equal(p.pressed, true);
  assert.equal(p.key, 'cpage.quick_pictures_label', 'the visible label is the feature; the tooltip says what a tap does');
  assert.equal(p.tipKey, 'cpage.quick_pictures_off');
  assert.deepEqual(K.rowQuick(row({ actions: { ...ACTIONS, lookups: false, voice: false } })).map((q) => q.id), ['hide', 'pictures']);
  assert.deepEqual(K.rowQuick(row({ actions: { edit: false } })), [], 'a read-only copy gets no quick buttons');
  assert.equal(K.rowQuick(row()).find((q) => q.id === 'voice').nav, true);
});

test('rowMenu: server actions only; an agent bot gets Delete only when the server says so; read-only reads Details', () => {
  assert.deepEqual(ids(K.rowMenu(row())), ['edit', 'delete']);
  const tpl = row({ kind: 'template', actions: { ...ACTIONS, stop_sharing: true } });
  assert.deepEqual(ids(K.rowMenu(tpl)), ['edit', 'stop_sharing'], 'a template\'s Delete and Stop sharing are one item (L12)');
  const copy = row({ kind: 'copy', actions: { ...ACTIONS, reset: true } });
  assert.deepEqual(ids(K.rowMenu(copy)), ['edit', 'reset', 'delete']);
  assert.deepEqual(ids(K.rowMenu(row({ actions: { ...ACTIONS, share: true } }))), ['edit', 'share', 'delete']);
  assert.deepEqual(ids(K.rowMenu(row({ kind: 'template', actions: { edit: false, make_copy: true } }))), ['edit', 'make_copy']);
  assert.equal(K.rowMenu(row({ actions: { edit: false } }))[0].key, 'cpage.menu_details');
  const agent = row({ kind: 'agent', backend_kind: 'agent', actions: { ...ACTIONS, delete: true } });
  assert.ok(ids(K.rowMenu(agent)).includes('delete'), 'the Owner\'s deliberate delete (actions.delete from the server)');
  assert.ok(!ids(K.rowMenu({ ...agent, actions: { ...ACTIONS, delete: false } })).includes('delete'));
  assert.deepEqual(ids(K.rowMenu(row({ actions: { ...ACTIONS, details: false } }))), ['delete'], 'actions.details false: no Details item');
});

test('filterRows: chips narrow by backend / hidden, the query matches name, id, description, model', () => {
  const rows = [
    row({ id: 'a', name: 'Alpha', backend_kind: 'agent' }),
    row({ id: 'b', name: 'Bravo', description: 'writes poems', model: 'gpt-x' }),
    row({ id: 'c', name: 'Charlie', backend_kind: 'app' }),
    row({ id: 'd', name: 'Delta', hidden: true, backend_kind: 'legacy-api' }),
  ];
  assert.deepEqual(ids(K.filterRows(rows, {})), ['a', 'b', 'c', 'd']);
  assert.deepEqual(ids(K.filterRows(rows, { chip: 'agent' })), ['a', 'c']);
  assert.deepEqual(ids(K.filterRows(rows, { chip: 'direct' })), ['b', 'd']);
  assert.deepEqual(ids(K.filterRows(rows, { chip: 'hidden' })), ['d']);
  assert.deepEqual(ids(K.filterRows(rows, { query: 'POEMS' })), ['b']);
  assert.deepEqual(ids(K.filterRows(rows, { query: 'gpt' })), ['b']);
  assert.deepEqual(ids(K.filterRows(rows, { chip: 'direct', query: 'del' })), ['d']);
  assert.deepEqual(ids(K.filterRows(rows, { query: 'zzz' })), []);
});

test('visibleTabs: Household only for a household manager', () => {
  assert.deepEqual(K.visibleTabs({ me: { can_manage_household: true } }), ['mine', 'household']);
  assert.deepEqual(K.visibleTabs({ me: { can_manage_household: false } }), ['mine']);
  assert.deepEqual(K.visibleTabs(null), ['mine']);
});

test('detailPatch: only changed fields, agent bots never get connection / tools', () => {
  const orig = { name: 'Atlas', emoji: '🌍', description: 'x', backend: { connection: 'c1', model: 'm1' }, personality: { text: 'Be kind.' } };
  const r = row();
  assert.deepEqual(K.detailPatch(r, orig, { name: 'Atlas', emoji: '🌍', description: 'x', persona: 'Be kind.', connection: 'c1', model: 'm1', tools: true, hidden: false, image_jobs: false, quick_actions: true }), {});
  assert.deepEqual(K.detailPatch(r, orig, { name: ' Atlas II ', connection: 'c2', model: 'm9', tools: false, hidden: true }),
    { name: 'Atlas II', connection: 'c2', model: 'm9', tools: false, hidden: true });
  assert.deepEqual(K.detailPatch(r, orig, { connection: '' }), { connection: null });
  const agent = row({ backend_kind: 'agent', kind: 'agent' });
  assert.deepEqual(K.detailPatch(agent, orig, { connection: 'c2', tools: false, persona: 'New' }), { persona: 'New' });
});

test('give-to helpers: entitled, toggling one profile, the Everyone chip, same set', () => {
  const profiles = [{ id: 'p1' }, { id: 'p2' }, { id: 'p3' }];
  assert.equal(G.entitled({ give_to: 'everyone' }, 'p1'), true);
  assert.equal(G.entitled({ give_to: ['p2'] }, 'p1'), false);
  assert.deepEqual(G.toggleProfile({ give_to: ['p2'] }, 'p1', true, profiles).sort(), ['p1', 'p2']);
  assert.deepEqual(G.toggleProfile({ give_to: 'everyone' }, 'p2', false, profiles).sort(), ['p1', 'p3'], 'everyone minus one is the others');
  assert.equal(G.toggleProfile({ give_to: 'everyone' }, 'p2', true, profiles), 'everyone');
  assert.equal(G.toggleEveryone({ give_to: ['p1'] }, true, profiles), 'everyone');
  assert.deepEqual(G.toggleEveryone({ give_to: 'everyone' }, false, profiles), ['p1', 'p2', 'p3']);
  assert.equal(G.sameGiveTo(['a', 'b'], ['b', 'a']), true);
  assert.equal(G.sameGiveTo('everyone', ['a']), false);
});

test('changeGiveTo: nothing to change asks nothing; a stale token previews and asks again', { skip: dom.skip }, async () => {
  await boot();
  const r = { id: 'atlas', name: 'Atlas', give_to: ['p1'] };
  const profiles = [{ id: 'p1', name: 'Pat' }, { id: 'p2', name: 'Max' }];
  let asked = 0;
  const ask = async (d) => { asked += 1; assert.match(d.notes.join(' '), /Max/); return 'ok'; };
  assert.equal(await G.changeGiveTo({ row: r, next: ['p1'], profiles, calls: {}, ask }), null);
  let previews = 0;
  let puts = 0;
  const calls = {
    preview: async () => { previews += 1; return { add: ['p2'], remove: [], detach: [], token: `t${previews}` }; },
    put: async (id, g, token) => {
      puts += 1;
      if (puts === 1) { const e = new Error('stale'); e.code = 'give_to_stale'; throw e; }
      assert.equal(token, 't2', 'the second PUT carries the second preview\'s token');
      return { id, give_to: g };
    },
  };
  const out = await G.changeGiveTo({ row: r, next: ['p1', 'p2'], profiles, calls, ask });
  assert.deepEqual(out.give_to, ['p1', 'p2']);
  assert.equal(previews, 2);
  assert.equal(asked, 2);
  // Cancel: no PUT.
  puts = 0;
  const no = await G.changeGiveTo({ row: r, next: ['p1', 'p2'], profiles, calls, ask: async () => null });
  assert.equal(no, null);
  assert.equal(puts, 0);
});

// ------------------------------------------------------------------ DOM

const tick = () => new Promise((r) => setTimeout(r, 0));
const settle = async (n = 8) => { for (let i = 0; i < n; i += 1) await tick(); };
let calls = [];
let routes = {};

async function boot() {
  const win = new jsdom.JSDOM(HTML, { url: 'http://127.0.0.1:18765/', runScripts: 'outside-only' }).window;
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
const call = (key) => calls.find((c) => c.key === key);

const PROFILES = [{ id: 'p1', name: 'Max', role: 'user', avatar: '', default_picture: null }, { id: 'p2', name: 'Sam', role: 'basic', avatar: '', default_picture: null }];
const manage = (over = {}) => ({
  me: { id: 'a1', role: 'admin', protected: true, can_manage_household: true, can_create: true },
  profiles: PROFILES,
  mine: [
    row({ id: 'bits', name: 'Bits', kind: 'agent', backend_kind: 'agent', actions: { ...ACTIONS, delete: false } }),
    row({ id: 'poet', name: 'Poet', description: 'writes poems' }),
  ],
  household: [
    row({ id: 'atlas', name: 'Atlas', kind: 'template', give_to: ['p1'], copies: [{ profile_id: 'p1', state: 'has', chats: 2 }], actions: { ...ACTIONS, stop_sharing: true } }),
  ],
  ...over,
});

async function mount(data, extra = {}) {
  const win = await boot();
  routes = { ...routes, 'GET /api/characters/manage': () => [200, data], ...extra };
  const mod = await import(`../static/js/characters-page.js?t=${Math.random()}`);
  const host = win.document.getElementById('chars-page-host');
  const toasts = [];
  const asks = [];
  mod.mountCharactersPage(host, { deps: { toast: (m, bad) => toasts.push([m, !!bad]) }, ask: async (d) => { asks.push(d); return asks.answer || 'ok'; } });
  await settle();
  return { win, host, toasts, asks, mod };
}
const rowEl = (host, id) => host.querySelector(`.cp-row[data-bot="${id}"]`);
const quick = (host, id, q) => rowEl(host, id).querySelector(`[data-q="${q}"]`);

test('Admin: Mine | Household tabs, the pill reads in words, status icons have tooltips + aria-labels', { skip: dom.skip }, async () => {
  const { host } = await mount(manage());
  const tabs = [...host.querySelectorAll('[role="tab"]')];
  assert.deepEqual(tabs.map((b) => b.dataset.tab), ['mine', 'household']);
  assert.equal(tabs[0].getAttribute('aria-selected'), 'true');
  assert.match(tabs[0].textContent, /Mine/);
  assert.ok([...host.querySelectorAll('.cp-row')].every((r) => ['bits', 'poet'].includes(r.dataset.bot)));
  const pill = rowEl(host, 'bits').querySelector('.cp-pill');
  assert.match(pill.textContent, /Agent/);
  assert.equal(pill.dataset.pill, 'agent');
  assert.match(rowEl(host, 'poet').querySelector('.cp-pill').textContent, /Direct/);
  const ico = rowEl(host, 'poet').querySelector('[data-badge="voice"]');
  assert.equal(ico.getAttribute('aria-label'), 'Has a voice');
  assert.equal(ico.title, 'Has a voice');
  // Household tab: the template row, its "given to" tag
  tabs[1].click();
  await settle();
  assert.ok(rowEl(host, 'atlas'));
  assert.match(rowEl(host, 'atlas').textContent, /Given to 1 profile/);
  assert.equal(rowEl(host, 'bits'), null);
});

test('a User sees only Mine: no Household tab, and no quick button the row\'s actions deny', { skip: dom.skip }, async () => {
  const data = manage({
    me: { id: 'u1', role: 'user', protected: false, can_manage_household: false, can_create: true },
    household: [], profiles: [],
    mine: [row({ id: 'copy1', name: 'My Atlas', kind: 'copy', template: { id: 'atlas', name: 'Atlas' }, actions: { ...ACTIONS, reset: true, lookups: false, voice: false } })],
  });
  const { host } = await mount(data);
  assert.equal(host.querySelector('.cp-tabs').hidden, true, 'a single tab is not drawn');
  assert.equal(quick(host, 'copy1', 'lookups'), null);
  assert.equal(quick(host, 'copy1', 'voice'), null);
  assert.ok(quick(host, 'copy1', 'hide'));
  assert.ok(rowEl(host, 'copy1').querySelector('[data-badge="linked"]'));
});

test('a Basic profile\'s managed copy is read-only: no quick buttons, menu says Details, panel says so', { skip: dom.skip }, async () => {
  const data = manage({
    me: { id: 'b1', role: 'basic', protected: false, can_manage_household: false, can_create: false },
    household: [], profiles: [],
    mine: [row({ id: 'c', name: 'Pip', kind: 'copy', template: { id: 'atlas', name: 'Atlas' }, actions: { edit: false, reset: false, delete: false } })],
  });
  const { host } = await mount(data, { 'GET /api/me/characters/c': () => [200, { name: 'Pip', emoji: '🌍', description: '', backend: { kind: 'direct', connection: 'c1', model: 'm' }, personality: { text: 'Hi' } }],
    'GET /api/me/backends': () => [200, { connections: [] }] });
  assert.equal(rowEl(host, 'c').querySelectorAll('.cp-quick .cp-btn').length, 0);
  assert.equal(host.querySelector('#mb-new'), null, 'no New character without can_create');
  rowEl(host, 'c').querySelector('.cp-details-btn').click();
  await settle();
  const panel = rowEl(host, 'c').querySelector('.cp-details');
  assert.match(panel.textContent, /household keeps this bot up to date/);
  assert.ok([...panel.querySelectorAll('input[type="text"], textarea')].every((i) => i.disabled));
  assert.equal(panel.querySelector('.btn-primary'), null, 'no Save');
});

test('quick buttons PATCH the unified route with the contract body and refetch', { skip: dom.skip }, async () => {
  const { host } = await mount(manage(), {
    'PATCH /api/characters/poet': (b) => [200, { id: 'poet', row: row({ id: 'poet', ...b }) }],
  });
  quick(host, 'poet', 'hide').click();
  await settle();
  assert.deepEqual(call('PATCH /api/characters/poet').body, { hidden: true });
  quick(host, 'poet', 'pictures').click();
  await settle();
  assert.deepEqual(calls.filter((c) => c.key === 'PATCH /api/characters/poet')[1].body, { image_jobs: true });
  quick(host, 'poet', 'lookups').click();
  await settle();
  assert.deepEqual(calls.filter((c) => c.key === 'PATCH /api/characters/poet')[2].body, { tools: false });
  assert.ok(calls.filter((c) => c.key === 'GET /api/characters/manage').length >= 4, 'refetched after every write');
});

test('an agent bot\'s menu has no Delete; a refusal shows the server sentence', { skip: dom.skip }, async () => {
  const { host, toasts } = await mount(manage(), { 'PATCH /api/characters/bits': () => [403, { detail: 'nope', error: { code: 'permission_denied', message: 'Not yours.' } }] });
  rowEl(host, 'bits').querySelector('.cp-menu-btn').click();
  const items = [...host.querySelectorAll('.cp-menu [role="menuitem"]')].map((b) => b.textContent);
  assert.deepEqual(items, ['Edit']);
  document.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Escape' }));
  quick(host, 'bits', 'hide').click();
  await settle();
  assert.deepEqual(toasts.at(-1), ['Not yours.', true]);
});

test('filter chips and the search box narrow the list', { skip: dom.skip }, async () => {
  const { host } = await mount(manage());
  const shown = () => [...host.querySelectorAll('.cp-row')].map((r) => r.dataset.bot);
  assert.deepEqual(shown(), ['bits', 'poet']);
  host.querySelector('[data-chip="agent"]').click();
  assert.deepEqual(shown(), ['bits']);
  host.querySelector('[data-chip="direct"]').click();
  assert.deepEqual(shown(), ['poet']);
  host.querySelector('[data-chip="all"]').click();
  const search = host.querySelector('.cp-search');
  search.value = 'poems';
  search.dispatchEvent(new window.Event('input'));
  assert.deepEqual(shown(), ['poet']);
  search.value = 'nothing like this';
  search.dispatchEvent(new window.Event('input'));
  assert.deepEqual(shown(), []);
  assert.match(host.querySelector('.cp-empty').textContent, /No bot matches/);
  assert.equal(host.querySelector('[data-chip="all"]').getAttribute('aria-pressed'), 'true');
});

test('Who can use it: a chip tap previews, the dialog lists who gets / keeps / loses, then PUT carries the token', { skip: dom.skip }, async () => {
  const q = (g) => `GET /api/characters/atlas/give-to/preview?give_to=${encodeURIComponent(JSON.stringify(g))}`;
  const { host, asks } = await mount(manage(), {
    'GET /api/characters/atlas': () => [200, { name: 'Atlas', emoji: '🌍', description: '', backend: { kind: 'direct', connection: 'c1', model: 'm' }, personality: { text: '' } }],
    'GET /api/connections': () => [200, { connections: [{ id: 'c1', name: 'Rig' }] }],
    [q(['p1', 'p2'])]: () => [200, { add: ['p2'], remove: [], detach: [], token: 'T9' }],
    'PUT /api/characters/atlas/give-to': () => [200, { id: 'atlas', give_to: ['p1', 'p2'] }],
    'GET /api/characters/atlas/kit': () => [404, { detail: 'x' }],
  });
  host.querySelector('[data-tab="household"]').click();
  await settle();
  rowEl(host, 'atlas').querySelector('.cp-details-btn').click();
  await settle();
  const chips = rowEl(host, 'atlas').querySelectorAll('.cp-who-chip');
  assert.deepEqual([...chips].map((c) => c.dataset.who), ['everyone', 'p1', 'p2']);
  assert.deepEqual([...chips].map((c) => c.getAttribute('aria-pressed')), ['false', 'true', 'false']);
  assert.match(chips[2].getAttribute('aria-label'), /Sam: no copy/);
  chips[2].click();
  await settle();
  assert.equal(asks.length, 1);
  assert.match(asks[0].title, /Change who can use Atlas\?/);
  assert.deepEqual(asks[0].tally.map((x) => x.n), [1, 0, 0]);
  assert.match(asks[0].notes[0], /Gets a copy: Sam/);
  assert.deepEqual(call('PUT /api/characters/atlas/give-to').body, { give_to: ['p1', 'p2'], token: 'T9' });
  // groups: Identity / Brain / Abilities / Who can use it
  const groups = [...rowEl(host, 'atlas').querySelectorAll('.cp-legend')].map((l) => l.textContent);
  assert.deepEqual(groups, ['Identity', 'How it answers', 'Abilities', 'Who can use it']);
});

test('give_to_stale: the page previews again and asks again before the second PUT', { skip: dom.skip }, async () => {
  let puts = 0;
  const q = `GET /api/characters/atlas/give-to/preview?give_to=${encodeURIComponent(JSON.stringify(['p1', 'p2']))}`;
  let n = 0;
  const { host, asks } = await mount(manage(), {
    'GET /api/characters/atlas': () => [200, { name: 'Atlas', emoji: '', description: '', backend: { kind: 'direct' }, personality: { text: '' } }],
    'GET /api/connections': () => [200, { connections: [] }],
    [q]: () => { n += 1; return [200, { add: ['p2'], remove: [], detach: [], token: `S${n}` }]; },
    'PUT /api/characters/atlas/give-to': () => { puts += 1; return puts === 1 ? [409, { detail: 'stale', error: { code: 'give_to_stale', message: 'stale' } }] : [200, { id: 'atlas', give_to: ['p1', 'p2'] }]; },
    'GET /api/characters/atlas/kit': () => [404, { detail: 'x' }],
  });
  host.querySelector('[data-tab="household"]').click();
  await settle();
  rowEl(host, 'atlas').querySelector('.cp-details-btn').click();
  await settle();
  rowEl(host, 'atlas').querySelector('[data-who="p2"]').click();
  await settle();
  assert.equal(asks.length, 2);
  assert.deepEqual(calls.filter((c) => c.key === 'PUT /api/characters/atlas/give-to').map((c) => c.body.token), ['S1', 'S2']);
});

test('Stop sharing: preview, dialog with the count, then POST with the token', { skip: dom.skip }, async () => {
  const { host, asks } = await mount(manage(), {
    'POST /api/characters/atlas/stop-sharing/preview': () => [200, { detach: ['p1'], token: 'X1' }],
    'POST /api/characters/atlas/stop-sharing': () => [200, { mine: null }],
  });
  host.querySelector('[data-tab="household"]').click();
  await settle();
  rowEl(host, 'atlas').querySelector('.cp-menu-btn').click();
  const item = [...host.querySelectorAll('.cp-menu [role="menuitem"]')].find((b) => b.textContent === 'Stop sharing');
  item.click();
  await settle();
  assert.equal(asks.length, 1);
  assert.match(asks[0].notes[0], /Max/);
  assert.deepEqual(call('POST /api/characters/atlas/stop-sharing').body, { token: 'X1' });
});

test('Share to household (own bot) and Reset (copy) call their contract routes', { skip: dom.skip }, async () => {
  const data = manage();
  data.mine[1].actions.share = true;
  data.mine.push(row({ id: 'cp', name: 'Copy', kind: 'copy', template: { id: 'atlas', name: 'Atlas' }, actions: { ...ACTIONS, reset: true } }));
  const { host } = await mount(data, {
    'POST /api/me/characters/poet/share': () => [200, { template: row({ id: 'poet-t', kind: 'template' }) }],
    'POST /api/me/characters/cp/reset': () => [200, { id: 'cp', posted: 0 }],
  });
  const menu = async (id, label) => {
    rowEl(host, id).querySelector('.cp-menu-btn').click();
    [...host.querySelectorAll('.cp-menu [role="menuitem"]')].find((b) => b.textContent === label).click();
    await settle();
  };
  await menu('poet', 'Share with household');
  assert.ok(call('POST /api/me/characters/poet/share'));
  await menu('cp', 'Reset to household version');
  assert.ok(call('POST /api/me/characters/cp/reset'));
});

test('Details: Save sends only what changed through the unified PATCH', { skip: dom.skip }, async () => {
  const { host } = await mount(manage(), {
    'GET /api/me/characters/poet': () => [200, { name: 'Poet', emoji: '🌍', description: 'writes poems', backend: { kind: 'direct', connection: 'c1', model: 'm1' }, personality: { text: 'Rhyme.' } }],
    'GET /api/me/backends': () => [200, { connections: [{ id: 'c1', name: 'Rig' }] }],
    'GET /api/connections': () => [200, { connections: [{ id: 'c1', name: 'Rig' }] }],
    'PATCH /api/characters/poet': () => [200, { id: 'poet', row: row() }],
  });
  rowEl(host, 'poet').querySelector('.cp-details-btn').click();
  await settle();
  const d = rowEl(host, 'poet').querySelector('.cp-details');
  const name = d.querySelector('input[id$="-name"]');
  name.value = 'Poet II';
  [...d.querySelectorAll('.btn-primary')].find((b) => b.textContent === 'Save details').click();
  await settle();
  assert.deepEqual(call('PATCH /api/characters/poet').body, { name: 'Poet II' });
});

test('Details: a profile that cannot list connections sees its choice locked, and Save never clears it', { skip: dom.skip }, async () => {
  const { host } = await mount(manage(), {
    'GET /api/me/characters/poet': () => [200, { name: 'Poet', emoji: '🌍', description: 'writes poems', backend: { kind: 'direct', connection: 'c1', model: 'm1' }, personality: { text: 'Rhyme.' } }],
    'GET /api/me/backends': () => [200, { may_choose: false, connections: [] }],
    'GET /api/connections': () => [403, { detail: 'no' }],
    'PATCH /api/characters/poet': () => [200, { id: 'poet', row: row() }],
  });
  rowEl(host, 'poet').querySelector('.cp-details-btn').click();
  await settle();
  const d = rowEl(host, 'poet').querySelector('.cp-details');
  const sel = d.querySelector('select');
  assert.equal(sel.disabled, true);
  assert.equal(sel.value, 'c1', 'the current connection stays selected');
  d.querySelector('input[id$="-name"]').value = 'Poet II';
  [...d.querySelectorAll('.btn-primary')].find((b) => b.textContent === 'Save details').click();
  await settle();
  assert.deepEqual(call('PATCH /api/characters/poet').body, { name: 'Poet II' });
});

// ------------------------------------------------------------------ wiring

test('wiring: My bots is the Mine tab (alias), Characters is one section for everyone who may use it', async () => {
  const N = await import('../static/js/settings-nav.js?v=10');
  assert.deepEqual(N.resolveSettingsTarget('mybots'), { id: 'characters', anchor: 'mine' });
  assert.deepEqual(N.resolveSettingsTarget('characters#household'), { id: 'characters', anchor: 'household' });
  assert.ok(!N.SECTION_IDS.includes('mybots'));
  assert.ok(N.RESERVED_TAB_IDS.has('mybots'), 'an add-in may not take the old id');
  const main = read('js/main.js');
  assert.match(main, /mountCharactersPage\(document\.getElementById\('chars-page-host'\), \{ anchor,/);
  assert.match(main, /refreshCharactersPage\(\);/);
  assert.match(main, /const MY_BOTS_SECTION = 'characters#mine';/);
  assert.match(read('sw.js'), /'\/static\/js\/characters-page\.js'/);
  assert.match(read('sw.js'), /'\/static\/js\/give-to\.js'/);
});

test('people.js: the profile editor uses give_to, not the household-bots list', () => {
  const people = read('js/people.js');
  assert.match(people, /giveToPanel\(person,/);
  assert.doesNotMatch(people, /householdPanel\(person, \{ bots, api: ctx\.api, toast: ctx\.toast, save: true \}\)/);
});

test('every new cpage / perms string exists in all eight locales with the same placeholders', () => {
  const isPlural = (v) => v && typeof v === 'object' && typeof v.other === 'string';
  const flat = (o, p = '') => Object.entries(o).flatMap(([k, v]) => (typeof v === 'object' && !isPlural(v) ? flat(v, `${p}${k}.`) : [[`${p}${k}`, v]]));
  const ph = (s) => (String(s).match(/\{\w+\}/g) || []).sort().join();
  const keys = flat({ cpage: EN.cpage });
  keys.push(...['give_hint', 'give_basic_hint', 'give_everyone', 'give_saved'].map((k) => [`perms.${k}`, EN.perms[k]]));
  assert.ok(keys.length > 100);
  for (const lang of ['ar', 'de', 'es', 'fr', 'ja', 'pt', 'zh']) {
    const L = JSON.parse(read(`locales/${lang}.json`));
    for (const [k, en] of keys) {
      const v = k.split('.').reduce((o, p) => (o ? o[p] : undefined), L);
      if (isPlural(en)) {   // a plural form: every category the language has keeps the placeholders; `other` always exists
        assert.ok(isPlural(v), `${lang}: ${k} is a plural form`);
        for (const [cat, str] of Object.entries(v)) assert.ok(String(str).trim() && ph(str) === ph(en.other), `${lang}: ${k}.${cat} placeholders`);
        continue;
      }
      assert.equal(typeof v, 'string', `${lang}: ${k}`);
      assert.ok(v.trim(), `${lang}: ${k} is empty`);
      assert.equal(ph(v), ph(en), `${lang}: ${k} placeholders`);
    }
  }
});

test('remount: the household Add toolbar is still in the page the second time Settings opens (H5)', { skip: dom.skip }, async () => {
  const win = await boot();
  routes = { ...routes, 'GET /api/characters/manage': () => [200, manage()] };
  const mod = await import(`../static/js/characters-page.js?t=${Math.random()}`);
  const host = win.document.getElementById('chars-page-host');
  mod.mountCharactersPage(host, { anchor: 'household', deps: { toast() {} } });
  await settle();
  assert.ok(host.querySelector('#chars-add-toolbar'), 'first open');
  assert.equal(host.querySelector('#chars-add-toolbar').hidden, false);
  mod.unmountCharactersPage();
  mod.mountCharactersPage(host, { anchor: 'household', deps: { toast() {} } });
  await settle();
  const add = host.querySelector('#chars-add-toolbar');
  assert.ok(add, 'second open still has the toolbar');
  assert.equal(add.hidden, false);
  host.querySelector('[data-tab="mine"]').click();
  await settle();
  assert.equal(host.querySelector('#chars-add-toolbar').hidden, true, 'hidden on the Mine tab');
});

test('Get my copy: a row whose actions allow make_copy shows a visible button that POSTs the clone', { skip: dom.skip }, async () => {
  const data = manage({
    me: { id: 'u1', role: 'user', protected: false, can_manage_household: false, can_create: true },
    household: [], profiles: [],
    mine: [row({ id: 'atlas', name: 'Atlas', kind: 'template', give_to: ['u1'], actions: { ...ACTIONS, edit: false, hide: false, pictures: false, lookups: false, voice: false, delete: false, make_copy: true } })],
  });
  const { host } = await mount(data, { 'POST /api/characters/atlas/clone': () => [201, { bot: { id: 'atlas-u1' }, voice_cleared: false }] });
  const btn = rowEl(host, 'atlas').querySelector('[data-q="make_copy"]');
  assert.ok(btn, 'a visible Get my copy button, not only a menu item');
  assert.match(btn.textContent, /Get my copy/);
  btn.click();
  await settle();
  assert.ok(call('POST /api/characters/atlas/clone'));
});

test('a refresh drops an open Details panel whose row changed, and a failed fetch is not cached (M4, L1)', { skip: dom.skip }, async () => {
  let data = manage();
  let fail = true;
  const { host, mod } = await mount(data, {
    'GET /api/characters/manage': () => [200, data],
    'GET /api/me/characters/poet': () => (fail ? [500, { detail: 'boom' }] : [200, { name: 'Poet', emoji: '', description: data.mine[1].description, backend: { kind: 'direct', connection: 'c1', model: 'm1' }, personality: { text: '' } }]),
    'GET /api/me/backends': () => [200, { connections: [] }],
    'GET /api/connections': () => [200, { connections: [] }],
  });
  rowEl(host, 'poet').querySelector('.cp-details-btn').click();
  await settle();
  assert.match(rowEl(host, 'poet').querySelector('.cp-details').textContent, /boom|went wrong|error/i);
  fail = false;
  data = manage(); data.mine[1] = row({ id: 'poet', name: 'Poet', description: 'writes poems' });
  mod.refreshCharactersPage(); await new Promise((r) => setTimeout(r, 250)); await settle();
  assert.ok(rowEl(host, 'poet').querySelector('.cp-details input[id$="-name"]'), 'the failed panel was retried, not cached');
  assert.equal(rowEl(host, 'poet').querySelector('.cp-details input[id$="-description"]').value, 'writes poems');
  data = manage(); data.mine[1] = row({ id: 'poet', name: 'Poet', description: 'changed elsewhere', hidden: true });
  mod.refreshCharactersPage(); await new Promise((r) => setTimeout(r, 250)); await settle();
  assert.equal(rowEl(host, 'poet').querySelector('.cp-details input[id$="-description"]').value, 'changed elsewhere', 'a changed row rebuilds its panel');
  assert.equal(rowEl(host, 'poet').querySelector('.cp-details [data-feature="hidden"] input').checked, true);
});

test('deleting your own copy warns that the household copy can be fetched again', { skip: dom.skip }, async () => {
  const data = manage({ mine: [row({ id: 'cp', name: 'Copy', kind: 'copy', template: { id: 'atlas', name: 'Atlas' }, actions: { ...ACTIONS, delete: true } })] });
  const { host, asks } = await mount(data, { 'DELETE /api/me/characters/cp': () => [200, { removed: 'cp' }] });
  rowEl(host, 'cp').querySelector('.cp-menu-btn').click();
  [...host.querySelectorAll('.cp-menu [role="menuitem"]')].find((b) => /Delete/.test(b.textContent)).click();
  await settle();
  assert.match(asks[0].body, /Get my copy/);
});

// ------------------------------------------------------------------ Fable fix pass (frontend)

test('M1: leaving "everyone" always asks and says new profiles will no longer get the bot', { skip: dom.skip }, async () => {
  await boot();
  const profiles = [{ id: 'p1', name: 'Max' }, { id: 'p2', name: 'Sam' }];
  const r = { id: 'atlas', name: 'Atlas', give_to: 'everyone' };
  assert.equal(G.leavesEveryone(r, { everyone_to_list: true }, 'everyone'), true, 'the server flag wins');
  assert.equal(G.leavesEveryone(r, {}, ['p1']), true, 'and the page works it out when the flag is absent');
  assert.equal(G.leavesEveryone(r, {}, 'everyone'), false);
  assert.equal(G.leavesEveryone({ ...r, give_to: ['p1'] }, {}, ['p1', 'p2']), false);
  const asked = [];
  const calls = { preview: async () => ({ add: [], remove: [], detach: [], token: 'T' }), put: async (id, g) => ({ id, give_to: g }) };
  const out = await G.changeGiveTo({ row: r, next: ['p1', 'p2'], profiles, calls, ask: async (d) => { asked.push(d); return 'ok'; } });
  assert.deepEqual(out.give_to, ['p1', 'p2']);
  assert.equal(asked.length, 1, 'asks even though no copy changes');
  assert.match(asked[0].notes[0], /New profiles will no longer get Atlas\./);
  assert.equal(G.previewDialog(r, { add: ['p2'] }, profiles).notes.some((n) => /no longer get/.test(n)), false, 'a plain change carries no such note');
});

test('M1: the Everyone chip says it includes profiles made later', { skip: dom.skip }, async () => {
  const data = manage();
  data.household[0].give_to = 'everyone';
  const { host } = await mount(data, {
    'GET /api/characters/atlas': () => [200, { name: 'Atlas', emoji: '', description: '', backend: { kind: 'direct', connection: 'c1', model: 'm' }, personality: { text: '' } }],
    'GET /api/connections': () => [200, { connections: [] }],
    'GET /api/characters/atlas/kit': () => [200, { managed: true, copyable: true, shipped: false, chats: [], copies: 1 }],
  });
  host.querySelector('[data-tab="household"]').click(); await settle();
  rowEl(host, 'atlas').querySelector('.cp-details-btn').click(); await settle();
  const chip = rowEl(host, 'atlas').querySelector('[data-who="everyone"]');
  assert.match(chip.textContent, /Everyone \(includes profiles made later\)/);
  assert.match(rowEl(host, 'atlas').querySelector('.cp-who-everyone-hint').textContent, /profiles made later/);
});

test('M2: the Get-my-copy row is a call to action: no template badges, no Details, one button', { skip: dom.skip }, async () => {
  const data = manage({
    me: { id: 'u1', role: 'user', protected: false, can_manage_household: false, can_create: true },
    household: [], profiles: [],
    mine: [row({ id: 'atlas', name: 'Atlas', kind: 'template', give_to: null, hidden: true, actions: { edit: false, hide: false, make_copy: true, details: false } })],
  });
  const { host } = await mount(data, { 'POST /api/characters/atlas/clone': () => [201, { bot: { id: 'x' } }] });
  const li = rowEl(host, 'atlas');
  assert.ok(li.classList.contains('cp-row--cta'));
  assert.equal(li.querySelector('.cp-pill, .cp-kind, .cp-icons, .cp-details-btn, .cp-menu-btn, .cp-hidden-tag'), null);
  assert.match(li.textContent, /deleted your copy/);
  const b = li.querySelector('[data-q="make_copy"]');
  assert.match(b.textContent, /Get my copy/);
  assert.ok(b.classList.contains('cp-cta-btn'));
  assert.equal(li.querySelectorAll('button').length, 1);
});

test('L3: a template has no Hide button, no Hidden tag and no Hide switch in Details', { skip: dom.skip }, async () => {
  const data = manage();
  data.household[0].hidden = true;
  const { host } = await mount(data, {
    'GET /api/characters/atlas': () => [200, { name: 'Atlas', emoji: '', description: '', backend: { kind: 'direct', connection: 'c1', model: 'm' }, personality: { text: '' } }],
    'GET /api/connections': () => [200, { connections: [] }],
    'GET /api/characters/atlas/kit': () => [404, { detail: 'x' }],
  });
  host.querySelector('[data-tab="household"]').click(); await settle();
  const li = rowEl(host, 'atlas');
  assert.equal(li.querySelector('[data-q="hide"]'), null);
  assert.equal(li.querySelector('.cp-hidden-tag'), null);
  assert.ok(!li.classList.contains('cp-row--hidden'));
  li.querySelector('.cp-details-btn').click(); await settle();
  assert.equal(li.querySelector('[data-feature="hidden"]'), null);
});

test('L15: a User\'s own bot explains Hide in its own words, not the household\'s', { skip: dom.skip }, async () => {
  const data = manage();
  const { host } = await mount(data, {
    'GET /api/me/characters/poet': () => [200, { name: 'Poet', emoji: '', description: '', backend: { kind: 'direct', connection: 'c1', model: 'm' }, personality: { text: '' } }],
    'GET /api/me/backends': () => [200, { connections: [] }], 'GET /api/connections': () => [200, { connections: [] }],
  });
  rowEl(host, 'poet').querySelector('.cp-details-btn').click(); await settle();
  const hint = rowEl(host, 'poet').querySelector('[data-feature="hidden"] .dev-row-hint').textContent;
  assert.match(hint, /out of your bot lists/);
  assert.doesNotMatch(hint, /ticked in People/);
});

test('L12: a template\'s Delete is Stop sharing (preview, token), never the raw tallies', { skip: dom.skip }, async () => {
  const data = manage();
  data.household[0].actions = { ...ACTIONS, delete: true, stop_sharing: false };
  const { host, asks } = await mount(data, {
    'POST /api/characters/atlas/stop-sharing/preview': () => [200, { detach: ['p1'], token: 'S1' }],
    'POST /api/characters/atlas/stop-sharing': () => [200, { mine: null }],
  });
  host.querySelector('[data-tab="household"]').click(); await settle();
  rowEl(host, 'atlas').querySelector('.cp-menu-btn').click();
  [...host.querySelectorAll('.cp-menu [role="menuitem"]')].find((b) => /Delete/.test(b.textContent)).click();
  await settle();
  assert.match(asks[0].title, /Stop sharing Atlas\?/);
  assert.doesNotMatch(JSON.stringify(asks[0]), /chats: \d/);
  assert.deepEqual(call('POST /api/characters/atlas/stop-sharing').body, { token: 'S1' });
  assert.equal(call('DELETE /api/characters/atlas'), undefined);
});

test('H1 (frontend half): the Owner deletes an agent bot through the household route, falling back to the personal one', { skip: dom.skip }, async () => {
  const data = manage();
  data.mine[0].actions = { ...ACTIONS, delete: true };
  const { host, asks } = await mount(data, { 'DELETE /api/characters/bits': () => [404, { detail: 'nope', error: { code: 'bot_not_found' } }], 'DELETE /api/me/characters/bits': () => [200, { removed: 'bits' }] });
  rowEl(host, 'bits').querySelector('.cp-menu-btn').click();
  [...host.querySelectorAll('.cp-menu [role="menuitem"]')].find((b) => /Delete/.test(b.textContent)).click();
  await settle();
  assert.match(asks[0].body, /stops answering/);
  assert.ok(call('DELETE /api/characters/bits') && call('DELETE /api/me/characters/bits'));
});

test('H1 (frontend half): only the protected Admin is offered the Agent backend when adding a character', { skip: dom.skip }, async () => {
  await boot();
  const { addBotRow } = await import('../static/js/bots-edit.js?t=a');
  const opts = (ctx) => [...addBotRow({ toast() {}, ...ctx }).querySelectorAll('[data-bm="add-backend"] option')].map((o) => o.value);
  assert.deepEqual(opts({ canCreateAgent: true }), ['direct', 'agent']);
  assert.deepEqual(opts({ canCreateAgent: false }), ['direct']);
  assert.deepEqual(opts({}), ['direct', 'agent'], 'callers that say nothing keep the old form');
  assert.match(read('js/main.js'), /canCreateAgent: !!\(charactersMe\(\) \|\| \{\}\)\.protected/);
});

test('L9: real plurals for profiles, chats and changed settings', { skip: dom.skip }, async () => {
  await boot();
  const { t } = await import('../static/js/i18n.js?v=5');
  assert.equal(t('cpage.given_count', { count: 1 }), 'Given to 1 profile');
  assert.equal(t('cpage.given_count', { count: 2 }), 'Given to 2 profiles');
  assert.equal(t('cpage.copy_chats', { count: 1 }), '1 chat');
  assert.equal(t('cpage.copy_chats', { count: 3 }), '3 chats');
  assert.match(t('cpage.badge_follows_changed', { count: 1, name: 'Atlas' }), /you changed 1 setting\b/);
  assert.doesNotMatch(JSON.stringify(EN.cpage), /\(s\)/);
});

test('M5: People create: templates given to everyone are ticked, fixed, labelled, and never sent in the list', { skip: dom.skip }, async () => {
  await boot();
  const { householdPanel } = await import('../static/js/people-perms.js?t=hp');
  const bots = [{ id: 'atlas', name: 'Atlas', give_to: 'everyone' }, { id: 'wikka', name: 'Wikka', give_to: ['p1'] }];
  const panel = householdPanel({ role: 'basic', household_bots: [] }, { bots, toast() {} });
  const cb = (id) => panel.querySelector(`input[value="${id}"]`);
  assert.equal(cb('atlas').checked, true);
  assert.equal(cb('atlas').disabled, true);
  assert.match(cb('atlas').closest('label').textContent, /given to everyone/);
  assert.equal(cb('wikka').checked, false);
  cb('wikka').checked = true; cb('wikka').dispatchEvent(new window.Event('change'));
  assert.deepEqual(panel.getValue(), ['wikka']);
  assert.doesNotMatch(panel.textContent, /sees every household bot/);
  const user = householdPanel({ role: 'user', household_bots: 'all' }, { bots, toast() {} });
  assert.match(user.textContent, /Only the bots given to everyone/);
});

test('H2 / M7 / L13: Settings CSS: no CSS columns on the pane, the search survives the icon-only nav, "on" quick buttons are filled', () => {
  const css = read('app.css');
  assert.doesNotMatch(css, /column-count/);
  assert.match(css, /#spane-appearance:not\(\.hidden\)[^}]*display: grid; grid-template-columns: minmax\(0, 1fr\) minmax\(0, 1fr\)/);
  const collapsed = css.slice(css.indexOf('@container smodal (max-width: 759px)'), css.indexOf('@container smodal (max-width: 759px)') + 1800);
  assert.doesNotMatch(collapsed, /\.settings-search \{ display: none/);
  assert.match(collapsed, /\.settings-search-input:focus/);
  assert.match(read('profiles.css'), /\.cp-btn\[aria-pressed="true"\] \{[^}]*background: var\(--accent-subtle\)/);
});

test('M3: What\'s new 4.2 uses the labels the UI really has', () => {
  const md = readFileSync(join(HERE, '..', '..', 'backend', 'app', 'guide', 'whats-new-4.2.0.md'), 'utf8');
  for (const label of [EN.cpage.menu_reset, EN.cpage.menu_get_copy, EN.cpage.menu_share, EN.settings.group_bots, EN.cpage.menu_stop_sharing, EN.cpage.group_who, EN.mybots.new, EN.mybots.ai_build, EN.chars.details, EN.cpage.tab_mine, EN.cpage.tab_household, EN.settings.maximize]) {
    assert.ok(md.includes(label), `mentions "${label}"`);
  }
  assert.doesNotMatch(md, /detach|Give to|Reset to default/i);
});

// ------------------------------------------------------------------ 4.3 Fable review (chat agent)

test('Fable 11a: on the shared default profile the lead and the copy note say everyone on it can chat', { skip: dom.skip }, async () => {
  const data = manage({
    me: { id: 'd1', role: 'user', protected: false, can_manage_household: false, can_create: true },
    household: [], profiles: [],
    mine: [row({ id: 'copy1', name: 'Atlas', kind: 'copy', template: { id: 'atlas', name: 'Atlas' } })],
  });
  const win = await boot();
  routes = { ...routes, 'GET /api/characters/manage': () => [200, data],
    'GET /api/me/characters/copy1': () => [200, { name: 'Atlas', emoji: '', description: '', backend: { kind: 'direct', connection: 'c1', model: 'm' }, personality: { text: '' } }],
    'GET /api/connections': () => [200, { connections: [] }], 'GET /api/me/backends': () => [200, { connections: [] }] };
  const mod = await import(`../static/js/characters-page.js?t=${Math.random()}`);
  const host = win.document.getElementById('chars-page-host');
  mod.mountCharactersPage(host, { deps: { toast: () => {}, isShared: () => true } });
  await settle();
  assert.equal(host.querySelector('.cp-lead').textContent, 'Everyone on this shared profile can chat with these.');
  rowEl(host, 'copy1').querySelector('.cp-details-btn').click();
  await settle();
  const txt = rowEl(host, 'copy1').querySelector('.cp-details').textContent;
  assert.match(txt, /The shared profile's copy of Atlas: everyone who uses this profile sees it and its chats\./);
  assert.doesNotMatch(txt, /Nobody else sees it/);
});

test('Fable 11b: a Basic profile is told what it can and cannot do here', { skip: dom.skip }, async () => {
  const data = manage({
    me: { id: 'b1', role: 'basic', protected: false, can_manage_household: false, can_create: false },
    household: [], profiles: [],
    mine: [row({ id: 'c', name: 'Pip', kind: 'copy', template: { id: 'atlas', name: 'Atlas' }, actions: { details: false, reorder: true } })],
  });
  const { host } = await mount(data);
  const note = host.querySelector('.cp-basic-note');
  assert.ok(note && !note.hidden);
  assert.equal(note.textContent, 'Basic profiles can chat with their bots and put them in order. An Admin changes the rest.');
  // A User does not get it.
  const { host: h2 } = await mount(manage({ me: { id: 'u1', role: 'user', can_manage_household: false, can_create: true }, household: [], profiles: [] }));
  assert.ok(!h2.querySelector('.cp-basic-note') || h2.querySelector('.cp-basic-note').hidden);
});

test('Fable 11c/11e: the Household Add reads "New household bot"; the summary for a non-Admin is "Your bots"', () => {
  assert.equal(EN.chars.add, 'New household bot');
  assert.match(HTML, /data-i18n="chars\.add">New household bot</);
  assert.equal(EN.cpage.sum_mine, 'Your bots');
  const MAIN = read('js/main.js');
  assert.match(MAIN, /const key = state\.role === 'admin' \? 'cpage\.sum' : 'cpage\.sum_mine';/);
});

test('Fable 11d: an Admin without a copy of a household bot gets "Get my copy" in its menu', { skip: dom.skip }, async () => {
  const data = manage({ profiles: [...PROFILES, { id: 'a1', name: 'Jo', role: 'admin', avatar: '', default_picture: null }] });
  const { host } = await mount(data, {
    [`GET /api/characters/atlas/give-to/preview?give_to=${encodeURIComponent(JSON.stringify(['p1', 'a1']))}`]: () => [200, { add: ['a1'], remove: [], detach: [], token: 'T1' }],
    'PUT /api/characters/atlas/give-to': () => [200, { id: 'atlas', give_to: ['p1', 'a1'] }],
  });
  host.querySelector('[data-tab="household"]').click();
  await settle();
  rowEl(host, 'atlas').querySelector('.cp-menu-btn').click();
  const item = [...host.querySelectorAll('.cp-menu [data-item]')].find((b) => b.dataset.item === 'get_copy');
  assert.ok(item, 'Get my copy in the menu');
  assert.match(item.textContent, /Get my copy/);
  item.click();
  await settle();
  assert.deepEqual(call('PUT /api/characters/atlas/give-to').body, { give_to: ['p1', 'a1'], token: 'T1' });
  // An Admin who already has a copy is not offered it.
  const has = manage({ household: [row({ id: 'atlas', name: 'Atlas', kind: 'template', give_to: ['a1'], copies: [{ profile_id: 'a1', state: 'has', chats: 0 }], actions: { ...ACTIONS, stop_sharing: true } })] });
  const { host: h2 } = await mount(has);
  h2.querySelector('[data-tab="household"]').click();
  await settle();
  rowEl(h2, 'atlas').querySelector('.cp-menu-btn').click();
  assert.equal([...h2.querySelectorAll('.cp-menu [data-item]')].filter((b) => b.dataset.item === 'get_copy').length, 0);
});
