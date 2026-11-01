// 4.3 Fable (people): the sign-in, People and Account fixes.
//
//   1. Edit profile: a PIN or password typed into the form is saved by the main Save.
//   2. Access for programs: a second Admin sees the key and the trust switch switched off.
//   3. "Forgot PIN?" on the tile screen: pick a profile first; the Owner's form is titled.
//   6. The quick-switch 429 names itself, once.   7. The shared tile says "Shared · no PIN".
//   8. Notifications: no /api/push/vapid call that can only answer 403.
//   9. People list order + why the Owner's row has no Edit.  10. Default profile above the list.
//  11. Reset another profile: their voices by name, no sentence repeating the tiles.
//  4/5. main.js: the Account sign-out button follows the rail; one choice is a plain button.

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
const MAIN = read('js/main.js');
const EN = JSON.parse(read('locales/en.json'));

const require = createRequire(import.meta.url);
let jsdom = null;
try { jsdom = require('jsdom'); } catch { /* not installed - DOM tests skip */ }
const dom = { skip: domSkip(jsdom ? false : 'jsdom is not installed') };

const tick = () => new Promise((r) => setTimeout(r, 0));
const settle = async (n = 8) => { for (let i = 0; i < n; i += 1) await tick(); };
const apiCalls = [];
let apiRoutes = {};

async function boot() {
  const { JSDOM } = jsdom;
  const win = new JSDOM(HTML, { url: 'http://127.0.0.1:18840/', runScripts: 'outside-only' }).window;
  const put = (k, v) => Object.defineProperty(globalThis, k, { value: v, configurable: true, writable: true });
  for (const [k, v] of Object.entries({ window: win, document: win.document, location: win.location,
    navigator: win.navigator, localStorage: win.localStorage, Node: win.Node, HTMLElement: win.HTMLElement,
    HTMLInputElement: win.HTMLInputElement, CustomEvent: win.CustomEvent })) put(k, v);
  apiCalls.length = 0;
  globalThis.fetch = async (u, opts = {}) => {
    const key = `${(opts.method || 'GET').toUpperCase()} ${u}`;
    apiCalls.push({ key, body: opts.body ? JSON.parse(opts.body) : null });
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

const P = (over) => ({ avatar: '', has_pin: true, has_password: false, grants: [], denies: [], session_timeout: null,
  disabled: false, household_bots: 'all', no_credential: false, lock_target: false, ...over });
const PEOPLE = (me = 'a1') => ({
  profiles: [
    P({ id: 'd1', name: 'DisPatch', role: 'user', has_pin: false, lock_target: true }),
    P({ id: 'b1', name: 'Sam', role: 'basic' }),
    P({ id: 'u1', name: 'Riley', role: 'user' }),
    P({ id: 'a2', name: 'Ops', role: 'admin', has_password: true }),
    P({ id: 'a1', name: 'Boss', role: 'admin', protected: true, has_password: true }),
  ],
  me, roles: ['basic', 'user', 'admin'],
  role_permissions: { admin: [], user: ['use_chat'], basic: ['use_chat'] },
  role_grantable: { admin: [], user: [], basic: [] },
  role_never: { admin: [], user: [], basic: [] },
  can_create_admin: true, owner_has_password: true, disclosure: '',
});

function routes(over = {}) {
  apiRoutes = {
    'GET /api/people': () => [200, over.people || PEOPLE()],
    'GET /api/people/devices': () => [200, { devices: [], this_device: null }],
    'GET /api/people/activity?days=7': () => [200, { summary: { days: [] }, recent: [], today: {} }],
    'GET /api/people/audit': () => [200, { entries: [] }],
    'GET /api/auth/api-token': () => [200, { has_token: true, machine_loopback_trust: false, machine_scopes: [], all_scopes: [] }],
    'GET /api/characters/all': () => [200, { characters: [] }],
    'GET /api/connections': () => [200, { connections: [] }],
    'POST /api/people/u1/pin': () => [200, { ok: true }],
    'POST /api/people/a2/password': () => [200, { ok: true }],
    'PATCH /api/people/u1': () => [200, { ok: true }],
    ...(over.routes || {}),
  };
}

async function mount(over = {}) {
  const win = await boot();
  routes(over);
  const mod = await import(`../static/js/people.js?t=${Math.random()}`);
  const perms = await import(`../static/js/people-perms.js?t=${Math.random()}`);
  const rec = { toasts: [], confirms: [] };
  const deps = { role: 'admin', toast: (m, e) => rec.toasts.push([m, !!e]),
    confirm: async (m) => { rec.confirms.push(m); return true; }, ...(over.deps || {}) };
  const pane = win.document.getElementById('spane-people');
  await mod.mountPeopleSettings(pane, deps);
  await perms.renderPeoplePerms(pane, deps);
  await settle();
  return { win, pane, rec, mod };
}

const btn = (root, text) => [...root.querySelectorAll('button')].find((b) => b.textContent.trim() === text);
const labelled = (root, text) => {
  const l = [...root.querySelectorAll('label')].find((x) => x.textContent.trim() === text);
  assert.ok(l, `no label "${text}"`);
  return root.querySelector(`#${l.getAttribute('for')}`);
};
const edit = async (pane, id) => { pane.querySelector(`[data-person="${id}"] button`).click(); await settle(); return pane.querySelector('#people-editor'); };
const call = (key) => apiCalls.find((c) => c.key === key);

// ---- 1 -------------------------------------------------------------------------------------

test('Edit profile: Save also saves a PIN typed into the form (it used to be thrown away)', { skip: dom.skip }, async () => {
  const { pane } = await mount();
  const ed = await edit(pane, 'u1');
  labelled(ed, EN.people.pin_change).value = '4826';
  apiCalls.length = 0;
  btn(ed, EN.common.save).click();
  await settle();
  assert.deepEqual(call('POST /api/people/u1/pin')?.body, { pin: '4826' });
});

test('Edit profile: a too-short PIN is refused by Save, not dropped', { skip: dom.skip }, async () => {
  const { pane } = await mount();
  const ed = await edit(pane, 'u1');
  labelled(ed, EN.people.pin_change).value = '12';
  apiCalls.length = 0;
  btn(ed, EN.common.save).click();
  await settle();
  assert.equal(call('POST /api/people/u1/pin'), undefined);
  assert.equal(ed.querySelector('.people-editor-error').textContent, EN.people.err_pin_short);
});

test('Edit profile: Save saves a typed password for an Admin', { skip: dom.skip }, async () => {
  const { pane } = await mount();
  const ed = await edit(pane, 'a2');
  labelled(ed, EN.people.password_change).value = 'longer-pass-1';
  apiCalls.length = 0;
  btn(ed, EN.common.save).click();
  await settle();
  assert.deepEqual(call('POST /api/people/a2/password')?.body, { password: 'longer-pass-1' });   // scrub-ok: a test fixture typed into a mocked form
});

// ---- 2 -------------------------------------------------------------------------------------

test('Access for programs: a second Admin sees the key and the trust switch switched off, with the reason', { skip: dom.skip }, async () => {
  const { pane } = await mount({ people: PEOPLE('a2') });
  const g = pane.querySelector('#people-machine');
  assert.ok(g);
  const rotate = g.querySelector('.dev-row button');
  assert.equal(rotate.disabled, true);
  assert.equal(g.querySelector('input[type="checkbox"]').disabled, true);
  assert.match(g.textContent, new RegExp(EN.agents.only_owner.replace(/[()]/g, '\\$&')));
});

test('Access for programs: the Owner still has both', { skip: dom.skip }, async () => {
  const { pane } = await mount();
  const g = pane.querySelector('#people-machine');
  assert.equal(g.querySelector('.dev-row button').disabled, false);
  assert.equal(g.querySelector('input[type="checkbox"]').disabled, false);
  assert.equal(g.querySelector('.people-owner-only'), null);
});

// ---- 9 -------------------------------------------------------------------------------------

test('People list: Owner, Admins, Users, Basic; the shared default profile last in its group', { skip: dom.skip }, async () => {
  const { pane } = await mount();
  const ids = [...pane.querySelectorAll('#people-list [data-person]')].map((r) => r.dataset.person);
  assert.deepEqual(ids, ['a1', 'a2', 'u1', 'd1', 'b1']);
});

test('People list: a second Admin is told why the Owner row has no Edit', { skip: dom.skip }, async () => {
  const { pane } = await mount({ people: PEOPLE('a2') });
  const row = pane.querySelector('[data-person="a1"]');
  assert.equal(row.querySelector('button'), null);
  assert.equal(row.querySelector('.people-owner-note').textContent, EN.people.protected_note);
  const own = (await mount()).pane.querySelector('[data-person="a1"]');
  assert.equal(own.querySelector('.people-owner-note'), null, 'the Owner edits itself: no note');
});

// ---- 10 ------------------------------------------------------------------------------------

test('Default profile: above the list, with a lead line; one profile is named, not a one-item list', { skip: dom.skip }, async () => {
  const { pane } = await mount();
  const kids = [...pane.children];
  const lock = kids.findIndex((n) => n.classList.contains('people-lock-host'));
  const list = kids.findIndex((n) => n.classList.contains('people-list-host'));
  assert.ok(lock >= 0 && lock < list, 'the group comes before the person list');
  const g = pane.querySelector('#people-lock');
  assert.equal(g.querySelector('.pe-lock-lead').textContent, EN.people.lock_lead.replace('{name}', 'DisPatch'));
  assert.equal(g.querySelector('select').hidden, true, 'one candidate: no dropdown');
  assert.match(g.querySelector('.pe-lock-default-label').textContent, /The DisPatch profile/);
});

test('Default profile: two candidates keep the dropdown; the list setting reads as the sign-in screen', { skip: dom.skip }, async () => {
  const p = PEOPLE(); p.profiles[1].has_pin = false; p.profiles[0].lock_target = false;
  const { pane } = await mount({ people: p });
  const g = pane.querySelector('#people-lock');
  assert.equal(g.querySelector('select').hidden, false);
  assert.equal(g.querySelector('.pe-lock-lead').textContent, EN.people.lock_lead_list);
});

// ---- 11 ------------------------------------------------------------------------------------

test('Reset another profile: their voices by name; the tiles, not a sentence, carry the counts', { skip: dom.skip }, async () => {
  const { win, pane } = await mount({ routes: {
    'GET /api/people/u1/reset/preview': () => [200, { counts: { bots: 1, chats: 4, pictures: 2, files: 5, voices: 1 }, token: 'rt', keeps_voices: true, outcome: 'atlas' }],
  } });
  const ed = await edit(pane, 'u1');
  btn(ed, EN.people.reset_button).click();
  await settle();
  const d = win.document.querySelector('.pe-dialog');
  assert.match(d.textContent, /Riley's cloned voices are kept\./);
  assert.doesNotMatch(d.textContent, /Your cloned voices/);
  assert.ok(!d.textContent.includes(EN.reset.confirm_counts.slice(0, 12)), 'no counts sentence');
  assert.equal(d.querySelectorAll('.pe-tally li').length, 4);
  btn(d, EN.people.dialog_cancel).click();
});

// ---- 3, 6, 7 (signin.js) ------------------------------------------------------------------

const TILES = [
  { id: 'o1', name: 'Boss', role: 'admin', protected: true, avatar: '', credential: 'pin_or_password' },
  { id: 'm1', name: 'Sam', role: 'user', avatar: '', credential: 'pin' },
  { id: 'd1', name: 'DisPatch', role: 'user', avatar: '', credential: 'none', shared: true },
];

test('recoverPlan: no tile picked asks for the profile first; another profile asks an Admin; else the Owner', async () => {
  const { recoverPlan } = await import('../static/js/signin.js');
  assert.equal(recoverPlan({ tiles: true, picked: null }), 'pick_first');
  assert.equal(recoverPlan({ tiles: true, picked: { id: 'm1' } }), 'ask_admin');
  assert.equal(recoverPlan({ tiles: true, picked: { id: 'o1', protected: true } }), 'owner');
  assert.equal(recoverPlan({ tiles: false, picked: null }), 'owner', 'the keypad is the Owner PIN');
});

test('sign-in tiles: the shared profile says "Shared · no PIN"; the Owner wears the Owner badge', { skip: dom.skip }, async () => {
  const win = await boot();
  const { renderSignIn } = await import(`../static/js/signin.js?t=${Math.random()}`);
  const host = win.document.getElementById('lock-tiles');
  renderSignIn(host, { profiles: TILES, onSubmit: async () => {} });
  const tile = (id) => host.querySelector(`[data-profile="${id}"]`);
  assert.equal(tile('d1').querySelector('.signin-role').textContent, EN.signin.role_shared);
  assert.equal(tile('d1').querySelectorAll('.signin-badge').length, 0, 'no second "Shared" badge');
  assert.equal(tile('o1').querySelector('.signin-badge').textContent, EN.people.protected);
  assert.equal(tile('m1').querySelector('.signin-role').textContent, 'User');
});

test('sign-in: the quick-switch 429 names itself, once, inline', { skip: dom.skip }, async () => {
  const win = await boot();
  const { renderSignIn } = await import(`../static/js/signin.js?t=${Math.random()}`);
  const host = win.document.getElementById('lock-tiles');
  renderSignIn(host, { profiles: TILES, onSubmit: async () => {
    const e = new Error('429: Too many attempts'); e.status = 429; e.retryAfter = 560; e.code = 'too_many_attempts'; throw e; } });
  host.querySelector('[data-profile="d1"]').click();
  await settle();
  const err = host.querySelector('#signin-tiles-error');
  assert.equal(err.hidden, false);
  assert.equal(err.textContent, EN.signin.pick_wait.replace('{time}', '9:20'));
  assert.doesNotMatch(host.textContent, /Too many attempts/);
});

test('sign-in: "Forgot PIN?" with no tile picked shows the note under the tiles; picking clears it', { skip: dom.skip }, async () => {
  const win = await boot();
  const { renderSignIn } = await import(`../static/js/signin.js?t=${Math.random()}`);
  const host = win.document.getElementById('lock-tiles');
  const ctl = renderSignIn(host, { profiles: TILES, onSubmit: async () => {} });
  ctl.notice(EN.lock.recover_pick_first);
  assert.equal(host.querySelector('#signin-tiles-error').textContent, 'Tap your profile first, then Forgot PIN?');
  host.querySelector('[data-profile="m1"]').click();
  assert.equal(host.querySelector('#signin-tiles-error'), null);
});

test('main.js: "Forgot PIN?" follows recoverPlan and hides the PIN form while the code form is up', () => {
  const fn = MAIN.slice(MAIN.indexOf('function showRecover()'), MAIN.indexOf('// POST /api/auth/recover'));
  assert.match(fn, /recoverPlan\(\{ tiles: lockMode === 'tiles', picked: p \}\)/);
  assert.match(fn, /t\('lock\.recover_pick_first'\)/);
  assert.match(fn, /t\('lock\.recover_owner_title'\)/);
  assert.match(fn, /setPinFormAside\(true\)/);
  assert.match(fn, /function hideRecover\(\) \{[\s\S]*setPinFormAside\(false\)/);
  assert.match(fn, /'lock-tiles', 'lock-dots', 'keypad', 'lock-submit', 'lock-forgot'/);
});

// ---- 4, 5 (main.js) ------------------------------------------------------------------------

test('main.js: Settings -> Account sign-out button = the rail button (same name and rule, arrow glyph)', () => {
  const fn = MAIN.slice(MAIN.indexOf('function paintAccountLockButton('), MAIN.indexOf('function paintAccountLockButton(') + 700);
  assert.match(fn, /classList\.toggle\('hidden', !lockOffered\(\)\)/);
  assert.match(fn, /text\.textContent = label/);
  assert.match(MAIN, /paintAccountLockButton\(home, label\)/);
  assert.doesNotMatch(MAIN, /dom\['bm-lock'\]\.classList\.toggle\('hidden', !canLock\)/);
  const b = HTML.slice(HTML.indexOf('id="bm-lock"'), HTML.indexOf('</button>', HTML.indexOf('id="bm-lock"')));
  assert.match(b, /bm-home-glyph/);
  assert.match(b, /bm-lock-label/);
  assert.doesNotMatch(b, /data-i18n="settings\.lock_now"/, 'the label is painted, not reset by i18n');
});

test('main.js: one thing to change in Account is a plain button, not a one-item list', () => {
  const fn = MAIN.slice(MAIN.indexOf('function paintAccountChoices()'), MAIN.indexOf('function openAccountForm('));
  assert.match(fn, /const single = choices\.length === 1;/);
  assert.match(fn, /sel\.hidden = single;/);
  assert.match(fn, /openAccountForm\(one\.dataset\.choice/);
});

// ---- 8 (push.js) ---------------------------------------------------------------------------

function pushWin() {
  const win = new jsdom.JSDOM('<!doctype html><html><body></body></html>').window;
  const calls = [];
  Object.defineProperty(win, 'isSecureContext', { value: true });
  win.Notification = { permission: 'default', requestPermission: async () => 'granted' };
  win.PushManager = function PushManager() {};
  Object.defineProperty(win.navigator, 'serviceWorker', {
    value: { ready: Promise.resolve({ pushManager: { getSubscription: async () => null } }) } });
  win.fetch = async (url) => { calls.push(url); return { ok: false, status: 403, statusText: '', json: async () => ({ detail: 'remember' }) }; };
  return { win, calls };
}

test('push: a session known not to be remembered asks nothing of the server and says who can fix it', { skip: dom.skip }, async () => {
  const { pushRow } = await import('../static/js/push.js');
  const tt = (k) => `[${k}]`;
  const { win, calls } = pushWin();
  const row = pushRow(tt, { win, remembered: false, rememberDays: 0 });
  await settle();
  assert.deepEqual(calls, [], 'no GET /api/push/vapid that can only answer 403');
  assert.equal(row.querySelector('#push-hint').textContent, '[push.needs_remember_off]');
  assert.equal(row.querySelector('#push-toggle').disabled, true);
  const on = pushWin();
  const row2 = pushRow(tt, { win: on.win, remembered: false, rememberDays: 30 });
  assert.equal(row2.querySelector('#push-hint').textContent, '[push.needs_remember]');
  assert.equal(pushRow(tt, { win: on.win, allowed: false }), null, 'no permission: no card');
});
