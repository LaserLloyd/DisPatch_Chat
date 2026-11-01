// The Add-a-pack dialog (4.4, js/pack-dialog.js) and the Characters tabs bar + pack cards
// (js/characters-page.js). Pure selection logic first, then the dialog and the page in jsdom.
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
const P = await import('../static/js/pack-dialog.js?v=1');

const PACK = {
  id: 'animal_friends', name: 'Animal Friends', description: 'Eight gentle animal friends.',
  installed: false, partly_installed: true,
  characters: [
    { id: 'kids_owl', name: 'Oscar the Owl', emoji: '🦉', description: 'A wise owl.', picture: '/api/starter-packs/animal_friends/pictures/kids_owl', installed: true },
    { id: 'kids_bee', name: 'Bizzy the Bee', emoji: '🐝', description: 'A busy bee.', picture: '/api/starter-packs/animal_friends/pictures/kids_bee', installed: false },
    { id: 'kids_frog', name: 'Freddie the Frog', emoji: '🐸', description: 'A bouncy frog.', picture: '', installed: false },
    { id: 'kids_pip', name: 'Pip', emoji: '🐧', description: '', picture: '', installed: false },
  ],
};

// ------------------------------------------------------------------ pure

test('addableIds: the bots not added yet, in pack order', () => {
  assert.deepEqual(P.addableIds(PACK), ['kids_bee', 'kids_frog', 'kids_pip']);
  assert.deepEqual(P.addableIds({}), []);
});

test('selectionState / selectAll / toggleOne: installed bots never count or toggle', () => {
  let sel = new Set();
  assert.deepEqual(P.selectionState(PACK, sel), { addable: 3, count: 0, all: false, some: false, canAddSelected: false, canAddAll: true });
  sel = P.toggleOne(PACK, sel, 'kids_owl', true);          // already added: ignored
  assert.equal(sel.size, 0);
  sel = P.toggleOne(PACK, sel, 'kids_bee', true);
  sel = P.toggleOne(PACK, sel, 'kids_frog', true);
  assert.deepEqual(P.selectionState(PACK, sel), { addable: 3, count: 2, all: false, some: true, canAddSelected: true, canAddAll: true });
  sel = P.selectAll(PACK, true);
  assert.deepEqual([...sel], ['kids_bee', 'kids_frog', 'kids_pip']);
  assert.equal(P.selectionState(PACK, sel).all, true);
  assert.equal(P.selectAll(PACK, false).size, 0);
  const done = { ...PACK, characters: PACK.characters.map((c) => ({ ...c, installed: true })) };
  const s = P.selectionState(done, new Set(['kids_bee']));
  assert.equal(s.count, 0); assert.equal(s.canAddAll, false); assert.equal(s.all, false);
});

test('installBody: `only` is the ticked bots or every addable one, never empty; the connection rides along', () => {
  const sel = new Set(['kids_frog', 'kids_owl']);
  assert.deepEqual(P.installBody(PACK, sel, 'selected', 'c1'), { only: ['kids_frog'], connection: 'c1' });
  assert.deepEqual(P.installBody(PACK, sel, 'all', ''), { only: ['kids_bee', 'kids_frog', 'kids_pip'] });
  assert.equal(P.installBody(PACK, new Set(), 'selected', 'c1'), null);
  const done = { ...PACK, characters: PACK.characters.map((c) => ({ ...c, installed: true })) };
  assert.equal(P.installBody(done, sel, 'all', 'c1'), null, 'an empty only would install the whole pack');
});

test('every packs.* key the code uses is in all eight locales with the same placeholders', () => {
  const src = read('js/pack-dialog.js') + read('js/characters-page.js');
  const used = [...new Set([...src.matchAll(/'(packs\.[a-z_]+|cpage\.tab_[a-z_]+)'/g)].map((m) => m[1]))];
  assert.ok(used.length >= 14, used.join());
  const ph = (s) => (String(s).match(/\{\w+\}/g) || []).sort().join();
  const get = (o, k) => k.split('.').reduce((x, p) => (x ? x[p] : undefined), o);
  for (const lang of ['en', 'ar', 'de', 'es', 'fr', 'ja', 'pt', 'zh']) {
    const L = JSON.parse(read(`locales/${lang}.json`));
    for (const k of [...used, 'chars.pack_done']) {
      const v = get(L, k);
      const en = get(EN, k);
      assert.ok(v, `${lang}: ${k}`);
      if (typeof en === 'object') {
        assert.equal(typeof v, 'object', `${lang}: ${k} is a plural`);
        for (const [cat, s] of Object.entries(v)) assert.equal(ph(s), ph(en.other), `${lang}: ${k}.${cat}`);
      } else {
        assert.equal(ph(v), ph(en), `${lang}: ${k}`);
      }
    }
  }
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
  routes = {};
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
const key = (win, k, shift = false) => win.document.dispatchEvent(new win.KeyboardEvent('keydown', { key: k, shiftKey: shift, bubbles: true }));

test('the dialog: rows with pictures, Added bots ticked + disabled, Add selected (N) sends `only`', { skip: dom.skip }, async () => {
  const win = await boot();
  const D = await import(`../static/js/pack-dialog.js?t=${Math.random()}`);
  const installs = [];
  const toasts = [];
  const p = D.packDialog(PACK, {
    connections: [{ id: 'c1', name: 'Home AI' }, { id: 'c2', name: 'Other' }],
    install: async (id, body) => { installs.push([id, body]); return { installed: body.only }; },
    toast: (m, bad) => toasts.push([m, !!bad]),
  });
  const box = win.document.querySelector('.pk-dialog');
  assert.equal(box.getAttribute('role'), 'dialog');
  assert.equal(box.getAttribute('aria-modal'), 'true');
  assert.equal(win.document.getElementById(box.getAttribute('aria-labelledby')).textContent, 'Animal Friends');
  const rows = [...box.querySelectorAll('.pk-row')];
  assert.deepEqual(rows.map((r) => r.dataset.id), ['kids_owl', 'kids_bee', 'kids_frog', 'kids_pip']);
  const owl = rows[0].querySelector('input');
  assert.equal(owl.checked, true); assert.equal(owl.disabled, true);
  assert.match(rows[0].querySelector('.pk-added').textContent, /Added/);
  assert.equal(rows[1].querySelector('img.pk-pic').getAttribute('src'), PACK.characters[1].picture);
  assert.match(rows[2].querySelector('.pk-emoji').textContent, /🐸/);
  assert.match(rows[1].textContent, /A busy bee\./);
  const addSel = box.querySelector('.pk-add-selected');
  assert.equal(addSel.textContent, 'Add selected (0)');
  assert.equal(addSel.disabled, true);
  assert.equal(box.querySelector('select').value, 'c1', 'the first connection is picked, as before');
  assert.equal(win.document.activeElement, rows[1].querySelector('input'), 'focus starts on the first bot to tick');
  for (const r of rows.slice(1, 3)) { const cb = r.querySelector('input'); cb.checked = true; cb.dispatchEvent(new win.Event('change')); }
  assert.equal(addSel.textContent, 'Add selected (2)');
  assert.equal(box.querySelector('#' + box.querySelector('.pk-all input').id).indeterminate, true);
  box.querySelector('select').value = 'c2';
  addSel.click();
  const r = await p;
  assert.deepEqual(installs, [['animal_friends', { only: ['kids_bee', 'kids_frog'], connection: 'c2' }]]);
  assert.deepEqual(r, { installed: ['kids_bee', 'kids_frog'] });
  assert.deepEqual(toasts, [['2 bots added.', false]]);
  assert.equal(win.document.querySelector('.pk-dialog'), null, 'closed');
});

test('the dialog: Select all, Add all, Esc / Cancel, the focus trap and a refused install', { skip: dom.skip }, async () => {
  const win = await boot();
  const D = await import(`../static/js/pack-dialog.js?t=${Math.random()}`);
  // Esc cancels, nothing installed
  let installs = 0;
  let p = D.packDialog(PACK, { install: async () => { installs += 1; return { installed: [] }; } });
  key(win, 'Escape');
  assert.equal(await p, null);
  // Cancel button
  p = D.packDialog(PACK, { install: async () => { installs += 1; return {}; } });
  win.document.querySelector('.pk-cancel').click();
  assert.equal(await p, null);
  assert.equal(installs, 0);
  // Select all ticks the addable ones; the focus trap wraps
  const bodies = [];
  let fail = true;
  const toasts = [];
  p = D.packDialog(PACK, { install: async (id, b) => { bodies.push(b); if (fail) { fail = false; const e = new Error('No connection'); throw e; } return { installed: b.only }; },
    toast: (m, bad) => toasts.push([m, bad]) });
  const box = win.document.querySelector('.pk-dialog');
  const all = box.querySelector('.pk-all input');
  all.checked = true; all.dispatchEvent(new win.Event('change'));
  assert.deepEqual([...box.querySelectorAll('.pk-check')].map((c) => c.checked), [true, true, true, true]);
  assert.equal(box.querySelector('.pk-add-selected').textContent, 'Add selected (3)');
  const focusables = [...box.querySelectorAll('button, input, select')].filter((x) => !x.disabled);
  focusables[focusables.length - 1].focus();
  key(win, 'Tab');
  assert.equal(win.document.activeElement, focusables[0], 'Tab from the last control goes to the first');
  key(win, 'Tab', true);
  assert.equal(win.document.activeElement, focusables[focusables.length - 1], 'Shift+Tab from the first goes to the last');
  // a refused install keeps the dialog open with the reason
  box.querySelector('.pk-add-all').click();
  await settle();
  assert.ok(win.document.querySelector('.pk-dialog'), 'still open');
  assert.match(box.querySelector('.pk-status').textContent, /No connection/);
  assert.deepEqual(toasts[0], ['No connection', true]);
  box.querySelector('.pk-add-all').click();
  const r = await p;
  assert.deepEqual(bodies[1], { only: ['kids_bee', 'kids_frog', 'kids_pip'] });
  assert.deepEqual(r.installed, ['kids_bee', 'kids_frog', 'kids_pip']);
});

test('the dialog: a fully added pack offers nothing to add', { skip: dom.skip }, async () => {
  const win = await boot();
  const D = await import(`../static/js/pack-dialog.js?t=${Math.random()}`);
  const done = { ...PACK, characters: PACK.characters.map((c) => ({ ...c, installed: true })) };
  const p = D.packDialog(done, { install: async () => ({}) });
  const box = win.document.querySelector('.pk-dialog');
  assert.equal(box.querySelector('.pk-add-selected').disabled, true);
  assert.equal(box.querySelector('.pk-add-all').disabled, true);
  assert.equal(box.querySelector('.pk-all'), null);
  assert.match(box.textContent, /already added/);
  assert.equal(win.document.activeElement, box.querySelector('.pk-cancel'));
  key(win, 'Escape');
  assert.equal(await p, null);
});

// ------------------------------------------------------------------ the Characters page

const row = (over = {}) => ({
  id: 'atlas', name: 'Atlas', emoji: '🌍', avatar: '', description: '', kind: 'own', backend_kind: 'direct',
  connection: 'c1', model: 'm1', hidden: false, badges: {}, template: null, overridden: [], give_to: null, copies: [],
  actions: { edit: true }, voice_link: null, ...over,
});
const manage = (me) => ({
  me, profiles: [],
  mine: [row({ id: 'poet', name: 'Poet' }), row({ id: 'nova', name: 'Nova' })],
  household: [row({ id: 'atlas', kind: 'template', give_to: [] })],
});
const ADMIN = { id: 'a1', role: 'admin', protected: true, can_manage_household: true, can_create: true };

async function mountPage(data, extra = {}) {
  const win = await boot();
  routes = { ...routes, 'GET /api/characters/manage': () => [200, data], ...extra };
  const mod = await import(`../static/js/characters-page.js?t=${Math.random()}`);
  const host = win.document.getElementById('chars-page-host');
  const toasts = [];
  mod.mountCharactersPage(host, { anchor: 'mine', deps: { toast: (m, bad) => toasts.push([m, !!bad]) } });
  await settle();
  return { win, host, toasts };
}

test('tabs: a full-width tab bar - icon, label, count, a hint - with ARIA and arrow keys', { skip: dom.skip }, async () => {
  const { win, host } = await mountPage(manage(ADMIN));
  const bar = host.querySelector('.cp-tabs');
  assert.equal(bar.getAttribute('role'), 'tablist');
  assert.ok(bar.getAttribute('aria-label'));
  const tabs = [...bar.querySelectorAll('[role="tab"]')];
  assert.deepEqual(tabs.map((b) => b.dataset.tab), ['mine', 'household']);
  assert.ok(tabs.every((b) => b.classList.contains('cp-tab') && b.querySelector('svg.rail-icon')));
  assert.equal(tabs[0].querySelector('.cp-tab-label').textContent, 'Mine');
  assert.equal(tabs[0].querySelector('.cp-count').textContent, '2');
  assert.equal(tabs[0].querySelector('.cp-count').title, '2 bots');
  assert.equal(tabs[1].querySelector('.cp-count').title, '1 bot');
  assert.equal(tabs[0].querySelector('.cp-tab-hint').textContent, EN.cpage.tab_mine_hint);
  assert.equal(tabs[1].querySelector('.cp-tab-hint').textContent, EN.cpage.tab_household_hint);
  assert.equal(tabs[0].getAttribute('aria-selected'), 'true');
  assert.equal(tabs[0].getAttribute('tabindex'), '0');
  assert.equal(tabs[1].getAttribute('tabindex'), '-1');
  assert.equal(tabs[0].getAttribute('aria-controls'), 'cp-panel');
  assert.equal(host.querySelector('#cp-panel').getAttribute('aria-labelledby'), 'cp-tab-mine');
  tabs[0].dispatchEvent(new win.KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }));
  await settle();
  const now = host.querySelector('[data-tab="household"]');
  assert.equal(now.getAttribute('aria-selected'), 'true');
  assert.equal(win.document.activeElement, now);
  // the CSS draws it as a bar with a filled selected tab, in theme tokens
  const css = read('profiles.css');
  assert.match(css, /\.cp-tabs \.ui-tab\.cp-tab\[aria-selected="true"\] \{ background: var\(--selected\);[^}]*color: var\(--on-selected\)/);
  assert.match(css, /\.cp-tabs \{ display: grid;/);
});

test('Household: pack cards for manage_characters open the dialog and install; never for a User', { skip: dom.skip }, async () => {
  const packs = { packs: [PACK, { id: 'assistants', name: 'Assistants', description: 'Four helpers.', installed: false, partly_installed: false,
    characters: [{ id: 'sage', name: 'Sage', emoji: '🧠', installed: false }] }] };
  const { win, host } = await mountPage(manage(ADMIN), {
    'GET /api/starter-packs': () => [200, packs],
    'GET /api/connections': () => [200, { connections: [{ id: 'c1', name: 'Home AI' }] }],
    'POST /api/starter-packs/animal_friends/install': (b) => [200, { pack: 'animal_friends', installed: b.only, skipped: [] }],
  });
  assert.ok(host.querySelector('.cp-packs').hidden, 'not on Mine');
  assert.ok(!calls.some((c) => c.key === 'GET /api/starter-packs'), 'not fetched for Mine');
  host.querySelector('[data-tab="household"]').click();
  await settle();
  const box = host.querySelector('.cp-packs');
  assert.equal(box.hidden, false);
  const cards = [...box.querySelectorAll('.cp-pack-card')];
  assert.deepEqual(cards.map((c) => c.dataset.pack), ['animal_friends', 'assistants']);
  assert.match(cards[0].textContent, /Animal Friends/);
  assert.match(cards[0].textContent, /1 of 4 added/);
  assert.match(cards[1].textContent, /1 bot/);
  cards[0].click();
  await settle();
  const dlg = win.document.querySelector('.pk-dialog');
  assert.ok(dlg, 'the card opens the dialog');
  const cb = dlg.querySelector('.pk-row[data-id="kids_pip"] input');
  cb.checked = true; cb.dispatchEvent(new win.Event('change'));
  dlg.querySelector('.pk-add-selected').click();
  await settle(12);
  const post = calls.find((c) => c.key === 'POST /api/starter-packs/animal_friends/install');
  assert.deepEqual(post.body, { only: ['kids_pip'], connection: 'c1' });
  assert.ok(calls.filter((c) => c.key === 'GET /api/characters/manage').length >= 2, 'the page reloads');
  assert.ok(calls.filter((c) => c.key === 'GET /api/starter-packs').length >= 2, 'the packs reload');

  // A User: no Household tab, no pack fetch, no cards
  const user = await mountPage(manage({ id: 'u1', role: 'user', can_manage_household: false, can_create: true }), {
    'GET /api/starter-packs': () => [200, packs],
  });
  assert.equal(user.host.querySelector('.cp-packs').hidden, true);
  assert.ok(!calls.some((c) => c.key === 'GET /api/starter-packs'));
});

test('main.js: the Add menu opens the pack dialog; the old inline sheet is gone', () => {
  const main = read('js/main.js');
  assert.match(main, /import \{ packDialog \} from '\.\/pack-dialog\.js\?v=1';/);
  assert.match(main, /await packDialog\(pk, \{/);
  assert.doesNotMatch(main, /chr-pack-sheet|chars-pack-conn/);
  assert.match(read('js/characters-page.js'), /from '\.\/pack-dialog\.js\?v=1'/);
  assert.ok(read('sw.js').includes("'/static/js/pack-dialog.js'"));
});
