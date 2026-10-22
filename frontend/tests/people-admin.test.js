// 4.0 People & Access (WP-FE-PEOPLE): types Admin / User / Basic, the permission toggles, the
// lock-target fence, household bots, backends, "When locked, show", the dialogs with counts
// (downgrade, reset, delete) and the household Characters panel (Hide, Copy to a profile).
//
//   1. pure helpers (people-perms.js, people.js, characters.js): no DOM
//   2. the panes against the real index.html markup, with fetch mocked per route

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { domSkip } from './_require-dom.js';
import * as PP from '../static/js/people-perms.js';
import * as PE from '../static/js/people.js';
import * as CH from '../static/js/characters.js';

const HERE = dirname(fileURLToPath(import.meta.url));
const STATIC = join(HERE, '..', 'static');
const read = (f) => readFileSync(join(STATIC, f), 'utf8');
const HTML = read('index.html');
const EN = JSON.parse(read('locales/en.json'));

const require = createRequire(import.meta.url);
let jsdom = null;
try { jsdom = require('jsdom'); } catch { /* not installed - DOM tests skip */ }
const dom = { skip: domSkip(jsdom ? false : 'jsdom is not installed') };

// ------------------------------------------------------------------ the role table

const TABLES = PP.permTables({
  role_permissions: {
    admin: [], basic: ['use_chat', 'use_voice', 'attach_files', 'use_lookups', 'manage_history', 'use_notifications', 'files_upload'],
    user: ['use_chat', 'use_voice', 'attach_files', 'use_lookups', 'manage_history', 'use_notifications', 'files_upload',
      'files_read', 'use_images', 'voice_lab', 'create_characters', 'choose_backend'],
  },
  role_grantable: {
    admin: [], user: ['dispatch_agents', 'approve_agent_actions'],
    basic: ['use_images', 'voice_lab', 'dispatch_agents', 'approve_agent_actions'],
  },
  role_never: { admin: [], user: ['manage_users'], basic: ['files_read', 'create_characters', 'choose_backend', 'manage_users'] },
});
const user = (extra = {}) => ({ id: 'u1', name: 'Riley', role: 'user', grants: [], denies: [], ...extra });
const basic = (extra = {}) => ({ id: 'b1', name: 'Sam', role: 'basic', grants: [], denies: [], ...extra });

test('permission rows: defaults are on, grantable are off, never-items are not offered', () => {
  const rows = PP.permissionRows(basic(), TABLES);
  const by = Object.fromEntries(rows.map((r) => [r.perm, r]));
  assert.equal(by.use_chat.on, true);
  assert.equal(by.use_images.on, false, 'Basic: image generation is off but switchable');
  assert.equal(by.dispatch_agents.on, false);
  assert.equal(by.files_read, undefined, 'files_read is never for Basic');
  assert.equal(by.create_characters, undefined);
  assert.equal(by.manage_users, undefined);
  assert.equal(PP.permissionRows({ role: 'admin' }, TABLES).length, 0, 'an Admin has nothing to switch');
});

test('permission rows follow the overrides, and say what differs from the default', () => {
  const rows = PP.permissionRows(user({ denies: ['use_images'], grants: ['dispatch_agents'] }), TABLES);
  const by = Object.fromEntries(rows.map((r) => [r.perm, r]));
  assert.deepEqual([by.use_images.on, by.use_images.changed], [false, true]);
  assert.deepEqual([by.dispatch_agents.on, by.dispatch_agents.changed], [true, true]);
  assert.deepEqual([by.use_chat.on, by.use_chat.changed], [true, false]);
  assert.equal(by.dispatch_agents.agent, true);
});

test('the lock-target fence switches agents and voice cloning off whatever the grants say', () => {
  const rows = PP.permissionRows(user({ grants: ['dispatch_agents'] }), TABLES, { lockTarget: true });
  const by = Object.fromEntries(rows.map((r) => [r.perm, r]));
  for (const p of ['dispatch_agents', 'approve_agent_actions', 'voice_lab']) {
    assert.deepEqual([by[p].fenced, by[p].on], [true, false], p);
  }
  assert.equal(by.use_images.fenced, false);
  assert.deepEqual([...PP.LOCK_TARGET_FENCE].sort(), ['approve_agent_actions', 'dispatch_agents', 'voice_lab']);
});

test('withToggle: a default goes into denies, a grantable one into grants; back is a removal', () => {
  assert.deepEqual(PP.withToggle(user(), 'use_images', false, TABLES), { grants: [], denies: ['use_images'] });
  assert.deepEqual(PP.withToggle(user({ denies: ['use_images'] }), 'use_images', true, TABLES), { grants: [], denies: [] });
  assert.deepEqual(PP.withToggle(user(), 'dispatch_agents', true, TABLES), { grants: ['dispatch_agents'], denies: [] });
  assert.deepEqual(PP.withToggle(user({ grants: ['dispatch_agents'] }), 'dispatch_agents', false, TABLES), { grants: [], denies: [] });
  assert.equal(PP.hasOverrides(user()), false);
  assert.equal(PP.hasOverrides(user({ denies: ['x'] })), true);
});

test('household value: only a User may have "all"; a Basic profile is always a list', () => {
  assert.equal(PP.householdValue('user', 'all', new Set(['a'])), 'all');
  assert.deepEqual(PP.householdValue('user', 'pick', new Set(['a'])), ['a']);
  assert.deepEqual(PP.householdValue('basic', 'all', new Set(['a'])), ['a']);
});

test('the default profile can be a User or a Basic profile that is not disabled, never an Admin', () => {
  const rows = [{ id: 'a', role: 'admin' }, { id: 'u', role: 'user' }, { id: 'd', role: 'basic', disabled: true }, { id: 'b', role: 'basic' }];
  assert.deepEqual(PP.defaultCandidates(rows).map((p) => p.id), ['u', 'b']);
  assert.deepEqual(PP.lockState([{ id: 'u' }, { id: 'b', lock_target: true }]), { target: 'default_profile', profileId: 'b' });
  assert.deepEqual(PP.lockState([{ id: 'u' }]), { target: 'profile_list', profileId: '' });
});

test('createBody: PINs and passwords are optional on every type; a password is for an Admin only', () => {
  assert.deepEqual(PE.createBody({ name: ' Pat ', role: 'user' }).body, { name: 'Pat', role: 'user' });
  assert.deepEqual(PE.createBody({ name: 'Pat', role: 'basic', householdBots: ['atlas'] }).body,
    { name: 'Pat', role: 'basic', household_bots: ['atlas'] });
  assert.equal(PE.createBody({ name: 'Pat', role: 'admin', householdBots: 'all' }).body.household_bots, undefined,
    'an Admin has no household list');
  assert.deepEqual(PE.createBody({ name: 'Pat', role: 'user', pin: '12' }).errors, { pin: 'min_4' });
  assert.deepEqual(PE.createBody({ name: 'Pat', role: 'user', password: 'longenough' }).errors, { password: 'role' });
  assert.deepEqual(PE.createBody({ name: 'Pat', role: 'admin', password: 'short' }).errors, { password: 'min_8' });
  assert.deepEqual(PE.createBody({ name: '', role: 'user' }).errors, { name: 'required' });
  assert.equal(PE.createBody({ name: 'Pat', role: 'user', sessionMinutes: '90' }).body.session_timeout, 5400);
});

test('patchBody sends only what changed, and never the type, the budgets or the limits', () => {
  const p = { name: 'Riley', avatar: '', session_timeout: 900, disabled: false };
  assert.deepEqual(PE.patchBody(p, { name: 'Riley', avatar: '', sessionMinutes: '15', disabled: false }).patch, {});
  assert.deepEqual(PE.patchBody(p, { name: 'Rye', avatar: '🦊', sessionMinutes: '30', disabled: true }).patch,
    { name: 'Rye', avatar: '🦊', session_timeout: 1800, disabled: true });
  assert.deepEqual(PE.patchBody(p, { name: 'Riley', role: 'basic', budgets: { turns: 5 } }).patch, {});
});

test('who may be edited: every Admin manages everyone but THE protected Admin, which only edits itself', () => {
  const prot = { id: 'a1', role: 'admin', protected: true };
  const other = { id: 'u1', role: 'user' };
  assert.equal(PE.canEditRow('admin', other, 'a1'), true);
  assert.equal(PE.canEditRow('admin', prot, 'a2'), false);
  assert.equal(PE.canEditRow('admin', prot, 'a1'), true);
  assert.equal(PE.canEditRow('user', other, 'u1'), false);
});

test('characters: hidden reads the 4.0 flag, then the 3.x one; diff sends hidden only when it changed', () => {
  assert.equal(CH.isHidden({ hidden: true }), true);
  assert.equal(CH.isHidden({ flags: { hidden: true } }), true);
  assert.equal(CH.isHidden({ hide_unlocked: true }), false, 'the 3.x flag is inert: only hidden hides');
  assert.equal(CH.isHidden({ audience: 'restricted', safe: true }), false, 'a legacy audience hides nothing');
  const orig = { description: '', hidden: false, backend: { connection: 'c1', model: 'm1' }, personality: { preset: 'default' } };
  assert.deepEqual(CH.diffCharacter(orig, { description: '', hidden: false }), {});
  assert.deepEqual(CH.diffCharacter(orig, { hidden: true, description: 'Hi' }), { hidden: true, description: 'Hi' });
  assert.deepEqual(CH.diffCharacter(orig, { connection: '', model: '' }), { connection: null });
});

test('characters: agent bots are not copyable; copies go to profiles that own bots', () => {
  assert.equal(CH.isAgentBot({ backend_kind: 'agent' }), true);
  assert.equal(CH.isAgentBot({ backend: { kind: 'agent' } }), true);
  assert.equal(CH.isAgentBot({ backend_kind: 'direct' }), false);
  assert.deepEqual(CH.copyTargets([{ id: 'a', role: 'admin' }, { id: 'u', role: 'user' }, { id: 'b', role: 'basic' },
    { id: 'd', role: 'user', disabled: true }]).map((p) => p.id), ['a', 'u']);
});

// ------------------------------------------------------------------ DOM

const tick = () => new Promise((r) => setTimeout(r, 0));
const settle = async (n = 6) => { for (let i = 0; i < n; i += 1) await tick(); };
const apiCalls = [];
let apiRoutes = {};

async function boot() {
  const { JSDOM } = jsdom;
  const win = new JSDOM(HTML, { url: 'http://127.0.0.1:18765/', runScripts: 'outside-only' }).window;
  const put = (k, v) => Object.defineProperty(globalThis, k, { value: v, configurable: true, writable: true });
  put('window', win);
  put('document', win.document);
  put('location', win.location);
  put('navigator', win.navigator);
  put('localStorage', win.localStorage);
  put('Node', win.Node);
  put('HTMLElement', win.HTMLElement);
  put('HTMLInputElement', win.HTMLInputElement);
  put('CustomEvent', win.CustomEvent);
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
  const i18n = await import('../static/js/i18n.js?v=4');
  await i18n.init('en');
  return win;
}

const P = (over) => ({ avatar: '', has_pin: true, has_password: false, budgets: {}, grants: [], denies: [], session_timeout: null,
  disabled: false, household_bots: 'all', no_credential: false, lock_target: false, ...over });
const PEOPLE = () => ({
  profiles: [
    P({ id: 'a1', name: 'Boss', role: 'admin', protected: true, has_password: true }),
    P({ id: 'a2', name: 'Ops', role: 'admin', has_pin: false, has_password: false, no_credential: true }),
    P({ id: 'u1', name: 'Riley', role: 'user', avatar: '🦊' }),
    P({ id: 'b1', name: 'Sam', role: 'basic', has_pin: false, household_bots: ['atlas'] }),
  ],
  me: 'a1', roles: ['basic', 'user', 'admin'],
  role_permissions: { admin: [], user: [...TABLES.defaults.user], basic: [...TABLES.defaults.basic] },
  role_grantable: { admin: [], user: [...TABLES.grantable.user], basic: [...TABLES.grantable.basic] },
  role_never: { admin: [], user: ['manage_users'], basic: [...TABLES.never.basic] },
  can_create_admin: true, owner_has_password: true,
  disclosure: 'An Admin sees how much happens in a profile, not what. Chats are private to their profile.',
});
const CHARS = () => ({ characters: [
  { id: 'atlas', name: 'Atlas', emoji: '🌍', backend_kind: 'direct', backend: { kind: 'direct', connection: 'c1', model: 'm1' }, hidden: false,
    description: '', personality: { preset: 'default' }, flags: { hidden: false } },
  { id: 'runner', name: 'Runner', emoji: '🏃', backend_kind: 'agent', backend: { kind: 'agent', agent: 'helper' }, hidden: true,
    description: '', personality: {}, flags: { hidden: true } },
] });

function routes(over = {}) {
  apiRoutes = {
    'GET /api/people': () => [200, over.people || PEOPLE()],
    'GET /api/people/devices': () => [200, { devices: [
      { id: 'd1', label: 'Hall tablet', last_seen: '2026-10-17T10:00:00Z', this_device: true },
      { id: 'd2', label: '', last_seen: null, this_device: false }], this_device: 'd1' }],
    'GET /api/people/activity?days=7': () => [200, { summary: { days: [{ day: '2026-10-17', counts: { turn: 3, login: 1 } }] }, recent: [], today: {} }],
    'GET /api/people/audit': () => [200, { entries: [{ ts: '2026-10-17T10:00:00Z', kind: 'profile_created', profile_id: 'a1', subject: 'u1', detail: '' }] }],
    'GET /api/auth/api-token': () => [200, { has_token: true, machine_loopback_trust: false, machine_scopes: ['machine:inject'], all_scopes: [] }],
    'POST /api/auth/api-token': () => [200, { ok: true, api_token: 'dp_secret_key' }],
    'GET /api/characters/all': () => [200, over.chars || CHARS()],
    'GET /api/connections': () => [200, { connections: [{ id: 'c1', name: 'Rig', type_label: 'OpenAI-compatible', models: ['m1', 'm2'] },
      { id: 'c2', name: 'Cloud', type_label: 'MiniMax', models: [] }] }],
    'GET /api/characters/personas': () => [200, { presets: [{ id: 'default', title: 'Default', description: '' }] }],
    ...(over.routes || {}),
  };
}

async function mount(over = {}) {
  const win = await boot();
  routes(over);
  const mod = await import(`../static/js/people.js?t=${Math.random()}`);
  const perms = await import(`../static/js/people-perms.js?t=${Math.random()}`);
  const rec = { toasts: [], confirms: [] };
  const deps = {
    role: 'admin',
    toast: (m, err) => rec.toasts.push([m, !!err]),
    confirm: async (m) => { rec.confirms.push(m); return over.confirmResult !== false; },
    ...(over.deps || {}),
  };
  const pane = win.document.getElementById('spane-people');
  await mod.mountPeopleSettings(pane, deps);
  await perms.renderPeoplePerms(pane, deps);
  await settle();
  return { win, pane, rec, mod, perms };
}

const labelled = (root, text) => {
  const l = [...root.querySelectorAll('label')].find((x) => x.textContent.trim() === text);
  assert.ok(l, `no label "${text}"`);
  return root.querySelector(`#${l.getAttribute('for')}`);
};
const btn = (root, text) => [...root.querySelectorAll('button')].find((b) => b.textContent.trim() === text);
const edit = async (pane, id) => { pane.querySelector(`[data-person="${id}"] button`).click(); await settle(); return pane.querySelector('#people-editor'); };
const call = (key) => apiCalls.find((c) => c.key === key);

test('People list: type badge, Protected, Lock screen, and a warning on an Admin with no PIN or password', { skip: dom.skip }, async () => {
  const p = PEOPLE(); p.profiles[2].lock_target = true;
  const { pane } = await mount({ people: p });
  const text = (id) => pane.querySelector(`[data-person="${id}"]`).textContent.replace(/\s+/g, ' ');
  assert.match(text('a1'), /Boss.*Admin.*Protected.*you.*PIN set.*Password set/);
  assert.match(text('u1'), /Riley.*User.*Lock screen.*PIN set/);
  assert.match(text('b1'), /Sam.*Basic.*No PIN/);
  assert.match(text('a2'), /Ops.*Admin.*No PIN.*No password.*no PIN or password/i);
  assert.doesNotMatch(pane.querySelector('#people-people').textContent, /Restricted|Member|Owner/);
  assert.ok(pane.querySelector('.people-disclosure').textContent.includes('Chats are private'), "the server's disclosure, not a stale string");
});

test('People list: the protected Admin edits only itself; others get Edit', { skip: dom.skip }, async () => {
  const { pane } = await mount({ deps: { role: 'admin' } });
  assert.ok(pane.querySelector('[data-person="a1"] button'), 'its own row');
  const other = PEOPLE(); other.me = 'a2';
  const m2 = await mount({ people: other });
  assert.equal(m2.pane.querySelector('[data-person="a1"] button'), null, 'another Admin cannot edit the protected one');
  assert.ok(m2.pane.querySelector('[data-person="u1"] button'));
});

test('create a User: no budgets, an optional PIN, a household picker with "All household bots"', { skip: dom.skip }, async () => {
  const { pane } = await mount({ routes: { 'POST /api/people': () => [201, { id: 'n1' }] } });
  pane.querySelector('#people-add').click();
  await settle();
  const ed = pane.querySelector('#people-editor');
  assert.deepEqual([...ed.querySelectorAll('.people-type option')].slice(0, 3).map((o) => o.value), ['user', 'basic', 'admin']);
  assert.doesNotMatch(ed.textContent, /Daily limits|Quiet hours|Minutes per day/);
  assert.ok(ed.querySelector('.pe-household'), 'household picker');
  assert.match(ed.querySelector('.pe-household').textContent, /All household bots/);
  labelled(ed, 'Name').value = 'Pat';
  apiCalls.length = 0;
  btn(ed, 'Add person').click();
  await settle();
  assert.deepEqual(call('POST /api/people').body, { name: 'Pat', role: 'user', household_bots: 'all' });
});

test('create a Basic profile: it needs picks, and the picks are sent', { skip: dom.skip }, async () => {
  const { pane } = await mount({ routes: { 'POST /api/people': () => [201, { id: 'n1' }] } });
  pane.querySelector('#people-add').click();
  await settle();
  const ed = pane.querySelector('#people-editor');
  const type = ed.querySelector('.people-type');
  type.value = 'basic';
  type.dispatchEvent(new window.Event('change'));
  await settle();
  assert.doesNotMatch(ed.querySelector('.pe-household').textContent, /All household bots/, 'a Basic profile is a list, never "all"');
  labelled(ed, 'Name').value = 'Pat';
  const atlas = [...ed.querySelectorAll('.pe-household-list input')].find((i) => i.value === 'atlas');
  atlas.checked = true;
  atlas.dispatchEvent(new window.Event('change'));
  apiCalls.length = 0;
  btn(ed, 'Add person').click();
  await settle();
  assert.deepEqual(call('POST /api/people').body, { name: 'Pat', role: 'basic', household_bots: ['atlas'] });
});

test('create an Admin: no household picker, and a warning while it has neither PIN nor password', { skip: dom.skip }, async () => {
  const { pane } = await mount();
  pane.querySelector('#people-add').click();
  await settle();
  const ed = pane.querySelector('#people-editor');
  const type = ed.querySelector('.people-type');
  type.value = 'admin';
  type.dispatchEvent(new window.Event('change'));
  await settle();
  assert.equal(ed.querySelector('.pe-household'), null);
  const warn = [...ed.querySelectorAll('.people-warn')].find((n) => /no PIN or password/i.test(n.textContent));
  assert.equal(warn.hidden, false);
  const pin = labelled(ed, 'PIN (optional)');
  pin.value = '2468';
  pin.dispatchEvent(new window.Event('input'));
  assert.equal(warn.hidden, true);
});

test('permissions: never-items are not offered; switching one off sends denies only', { skip: dom.skip }, async () => {
  const { pane } = await mount({ routes: { 'PATCH /api/people/u1': (o) => [200, { grants: [], denies: JSON.parse(o.body).denies }] } });
  const ed = await edit(pane, 'u1');
  const perms = [...ed.querySelectorAll('.pe-perm')].map((r) => r.dataset.perm);
  assert.ok(perms.includes('use_images') && perms.includes('dispatch_agents'));
  assert.ok(!perms.includes('manage_users') && !perms.includes('use_tools'), perms.join());
  apiCalls.length = 0;
  const sw = ed.querySelector('[data-perm="use_images"] input');
  sw.checked = false;
  sw.dispatchEvent(new window.Event('change'));
  await settle();
  assert.deepEqual(call('PATCH /api/people/u1').body, { grants: [], denies: ['use_images'] });
  assert.ok(ed.querySelector('.pe-defaults'), 'Back to the defaults for User appears');
  assert.match(ed.querySelector('.pe-defaults').textContent, /defaults for User/);
});

test('permissions: agent bots carry the plain warning and need a confirmation to switch on', { skip: dom.skip }, async () => {
  const { pane, rec } = await mount({ routes: { 'PATCH /api/people/u1': (o) => [200, { grants: JSON.parse(o.body).grants, denies: [] }] } });
  const ed = await edit(pane, 'u1');
  assert.match(ed.querySelector('[data-perm="dispatch_agents"]').textContent,
    /Agent bots can run commands on this computer, including reading anyone's chats\./);
  apiCalls.length = 0;
  const sw = ed.querySelector('[data-perm="dispatch_agents"] input');
  sw.checked = true;
  sw.dispatchEvent(new window.Event('change'));
  await settle();
  assert.equal(rec.confirms.length, 1);
  assert.match(rec.confirms[0], /Riley.*run commands on this computer/);
  assert.deepEqual(call('PATCH /api/people/u1').body, { grants: ['dispatch_agents'], denies: [] });
});

test('permissions: declining the agent warning changes nothing', { skip: dom.skip }, async () => {
  const { pane } = await mount({ confirmResult: false });
  const ed = await edit(pane, 'u1');
  apiCalls.length = 0;
  const sw = ed.querySelector('[data-perm="dispatch_agents"] input');
  sw.checked = true;
  sw.dispatchEvent(new window.Event('change'));
  await settle();
  assert.equal(sw.checked, false);
  assert.equal(apiCalls.some((c) => c.key.startsWith('PATCH')), false);
});

test('permissions: a refusal from the server puts the switch back and shows its sentence', { skip: dom.skip }, async () => {
  const { pane, rec } = await mount({ routes: { 'PATCH /api/people/u1': () => [422, { detail: 'Not for that profile.', error: { code: 'permissions_invalid' } }] } });
  const ed = await edit(pane, 'u1');
  const sw = ed.querySelector('[data-perm="use_images"] input');
  sw.checked = false;
  sw.dispatchEvent(new window.Event('change'));
  await settle();
  assert.equal(sw.checked, true);
  assert.deepEqual(rec.toasts.at(-1), ['Not for that profile.', true]);
});

test('the lock-target profile: agents and voice cloning are shown locked, with the reason once', { skip: dom.skip }, async () => {
  const p = PEOPLE(); p.profiles[2].lock_target = true;
  const { pane } = await mount({ people: p });
  const ed = await edit(pane, 'u1');
  for (const perm of ['dispatch_agents', 'approve_agent_actions', 'voice_lab']) {
    const row = ed.querySelector(`[data-perm="${perm}"]`);
    assert.equal(row.querySelector('input').disabled, true, perm);
    assert.equal(row.querySelector('input').checked, false, perm);
    assert.match(row.textContent, /Locked/, perm);
  }
  assert.equal(ed.querySelector('[data-perm="use_images"] input').disabled, false);
  const notes = ed.querySelectorAll('.pe-fence-note');
  assert.equal(notes.length, 1);
  assert.match(notes[0].textContent, /can't be switched on for Riley/);
});

test('an Admin profile shows no toggles, and no household picker', { skip: dom.skip }, async () => {
  const { pane } = await mount();
  const ed = await edit(pane, 'a2');
  assert.equal(ed.querySelectorAll('.pe-perm').length, 0);
  assert.equal(ed.querySelector('.pe-household'), null);
  assert.match(ed.querySelector('.pe-perms').textContent, /Admins can do everything/);
});

test('your own profile (and the protected Admin) edits name and picture and can reset, nothing else', { skip: dom.skip }, async () => {
  const { pane } = await mount();
  const ed = await edit(pane, 'a1');
  assert.equal(ed.querySelector('.pe-perms'), null);
  assert.ok(btn(ed, 'Reset this profile…'));
  assert.equal(btn(ed, 'Delete'), undefined, 'the first Admin cannot be deleted');
  assert.equal(ed.querySelector('.people-type').disabled, true, 'its type is fixed');
});

test('household bots: a User picks from the list and saves through the household route', { skip: dom.skip }, async () => {
  const { pane } = await mount({ routes: { 'PUT /api/people/u1/household-bots': () => [200, { household_bots: ['atlas'] }] } });
  const ed = await edit(pane, 'u1');
  const hh = ed.querySelector('.pe-household');
  const pick = [...hh.querySelectorAll('input[type="radio"]')].find((r) => r.value === 'pick');
  pick.checked = true;
  pick.dispatchEvent(new window.Event('change'));
  const atlas = [...hh.querySelectorAll('.pe-household-list input')].find((i) => i.value === 'atlas');
  atlas.checked = true;
  atlas.dispatchEvent(new window.Event('change'));
  assert.match(hh.textContent, /agent bot: also needs agent bots switched on/, 'agent bots are marked');
  apiCalls.length = 0;
  btn(hh, 'Save household bots').click();
  await settle();
  assert.deepEqual(call('PUT /api/people/u1/household-bots').body, { household_bots: ['atlas'] });
});

test('household bots: a Basic profile starts from its list and has no "All" option', { skip: dom.skip }, async () => {
  const { pane } = await mount();
  const ed = await edit(pane, 'b1');
  const hh = ed.querySelector('.pe-household');
  assert.doesNotMatch(hh.textContent, /All household bots/);
  const atlas = [...hh.querySelectorAll('.pe-household-list input')].find((i) => i.value === 'atlas');
  assert.equal(atlas.checked, true);
});

test('backends: every connection by default, or a pick, saved through the backends route', { skip: dom.skip }, async () => {
  const { pane } = await mount({ routes: { 'PUT /api/people/u1/backends': () => [200, { id: 'u1', backends: { allowed: ['c1'] } }] } });
  const ed = await edit(pane, 'u1');
  const be = ed.querySelector('.pe-backends');
  assert.ok(be);
  assert.equal(be.querySelector('input[type="radio"]').checked, true);
  const only = [...be.querySelectorAll('input[type="radio"]')][1];
  only.checked = true;
  only.dispatchEvent(new window.Event('change'));
  const c1 = [...be.querySelectorAll('.dev-group-body input')].find((i) => i.value === 'c1');
  c1.checked = true;
  c1.dispatchEvent(new window.Event('change'));
  apiCalls.length = 0;
  btn(be, 'Save backends').click();
  await settle();
  assert.deepEqual(call('PUT /api/people/u1/backends').body, { allowed: ['c1'] });
  const basicEd = await edit(pane, 'b1');
  assert.equal(basicEd.querySelector('.pe-backends'), null, 'a Basic profile has no bots of its own to place');
});

const dialog = () => document.querySelector('.pe-dialog');

test('downgrade User to Basic: a dialog with the counts including File Server files and cloned voices; Cancel changes nothing', { skip: dom.skip }, async () => {
  const { pane } = await mount({ routes: {
    'GET /api/people/u1/role/preview?role=basic': () => [200, { counts: { bots: 2, chats: 7, pictures: 1, files: 4, voices: 3 }, token: 'tok', deletes: true }],
    'POST /api/people/u1/role': () => [200, { id: 'u1', role: 'basic' }],
  } });
  const ed = await edit(pane, 'u1');
  const type = ed.querySelector('.people-type');
  type.value = 'basic';
  btn(ed, 'Save').click();
  await settle();
  const d = dialog();
  assert.ok(d, 'the dialog');
  assert.equal(d.getAttribute('role'), 'alertdialog');
  assert.match(d.querySelector('.pe-dialog-title').textContent, /Make Riley a Basic profile\?/);
  assert.match(d.textContent, /deletes 2 bots, 7 chats, 4 File Server files and 3 cloned voices/);
  assert.deepEqual([...d.querySelectorAll('.pe-tally li')].map((li) => li.textContent), ['2bots', '7chats', '1pictures', '4File Server files', '3cloned voices']);
  assert.equal(document.activeElement.textContent, 'Cancel', 'the safe choice has focus');
  btn(d, 'Cancel').click();
  await settle();
  assert.equal(dialog(), null);
  assert.equal(apiCalls.some((c) => c.key === 'POST /api/people/u1/role'), false);
  btn(ed, 'Save').click();
  await settle();
  btn(dialog(), 'Make Basic').click();
  await settle();
  assert.deepEqual(call('POST /api/people/u1/role').body, { role: 'basic', confirm_token: 'tok' });
});

test('the dialog closes on Escape and on a click outside', { skip: dom.skip }, async () => {
  const { win, perms } = await mount();
  const p1 = perms.askDialog({ title: 'x', okText: 'Go' });
  win.document.dispatchEvent(new win.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  assert.equal(await p1, null);
  assert.equal(dialog(), null);
  const p2 = perms.askDialog({ title: 'y', okText: 'Go', altText: 'Other' });
  btn(dialog(), 'Other').click();
  assert.equal(await p2, 'alt');
});

test('Basic to User keeps the chats: no dialog, no token', { skip: dom.skip }, async () => {
  const { pane } = await mount({ routes: {
    'GET /api/people/b1/role/preview?role=user': () => [200, { counts: {}, token: '', deletes: false }],
    'POST /api/people/b1/role': () => [200, { id: 'b1', role: 'user' }],
  } });
  const ed = await edit(pane, 'b1');
  const type = ed.querySelector('.people-type');
  type.value = 'user';
  type.dispatchEvent(new window.Event('change'));
  assert.match(ed.textContent, /keeps its chats/);
  btn(ed, 'Save').click();
  await settle();
  assert.equal(dialog(), null);
  assert.deepEqual(call('POST /api/people/b1/role').body, { role: 'user' });
});

test('reset another profile: counts in the dialog, the outcome, then the token goes back', { skip: dom.skip }, async () => {
  const { pane } = await mount({ routes: {
    'GET /api/people/u1/reset/preview': () => [200, { counts: { bots: 1, chats: 4, pictures: 2, files: 5, voices: 1 }, token: 'rt', keeps_voices: true, outcome: 'atlas' }],
    'POST /api/people/u1/reset': () => [200, { bots: 1, chats: 4 }],
  } });
  const ed = await edit(pane, 'u1');
  btn(ed, 'Reset this profile…').click();
  await settle();
  const d = dialog();
  assert.match(d.querySelector('.pe-dialog-title').textContent, /Reset Riley\?/);
  assert.match(d.textContent, /1 bots, 4 chats, 2 pictures and 5 File Server files/);
  assert.match(d.textContent, /Riley gets a fresh Atlas afterwards/);
  assert.match(d.textContent, /cloned voices are kept/);
  btn(d, 'Delete and reset').click();
  await settle();
  assert.deepEqual(call('POST /api/people/u1/reset').body, { token: 'rt' });
});

test('reset a Basic profile: it goes back to the household bots; a stale token is explained', { skip: dom.skip }, async () => {
  const { pane } = await mount({ routes: {
    'GET /api/people/b1/reset/preview': () => [200, { counts: { bots: 0, chats: 1, pictures: 0, voices: 0 }, token: 'rt', outcome: 'household' }],
    'POST /api/people/b1/reset': () => [409, { detail: 'stale', error: { code: 'reset_stale' } }],
  } });
  const ed = await edit(pane, 'b1');
  btn(ed, 'Reset this profile…').click();
  await settle();
  assert.match(dialog().textContent, /household bots you picked/);
  btn(dialog(), 'Delete and reset').click();
  await settle();
  assert.match(ed.querySelector('.people-editor-error').textContent, /Something changed since you opened this/);
});

test('delete: the dialog shows what goes with the profile, and the token confirms those numbers', { skip: dom.skip }, async () => {
  const { pane } = await mount({ routes: {
    'GET /api/people/u1/delete/preview': () => [200, { counts: { bots: 3, chats: 9, pictures: 0, files: 6, voices: 2 }, token: 'dt' }],
    'POST /api/people/u1/delete': () => [200, { ok: true }],
  } });
  const ed = await edit(pane, 'u1');
  btn(ed, 'Delete').click();
  await settle();
  assert.match(dialog().querySelector('.pe-dialog-title').textContent, /Delete Riley\?/);
  assert.deepEqual([...dialog().querySelectorAll('.pe-tally-n')].map((n) => n.textContent), ['3', '9', '0', '6', '2']);
  btn(dialog(), 'Delete profile').click();
  await settle();
  assert.equal(call('POST /api/people/u1/delete').body.confirm_token, 'dt');
  assert.equal(apiCalls.some((c) => c.key === 'DELETE /api/people/u1'), false, 'the 3.x DELETE answers 409');
});

test('When locked, show: the default profile or the profile list; an Admin is never offered', { skip: dom.skip }, async () => {
  const { pane } = await mount({ routes: { 'PATCH /api/settings': () => [200, { ok: true }] } });
  const g = pane.querySelector('#people-lock');
  assert.ok(g, 'the group is mounted by renderPeoplePerms');
  assert.match(g.textContent, /When locked, show/);
  assert.deepEqual([...g.querySelectorAll('select option')].map((o) => o.textContent), ['Riley', 'Sam']);
  assert.equal(g.querySelector('input[value="profile_list"]').checked, true);
  const sel = g.querySelector('select');
  sel.value = 'b1';
  apiCalls.length = 0;
  sel.dispatchEvent(new window.Event('change'));
  await settle();
  const patch = call('PATCH /api/settings');
  assert.deepEqual(Object.entries(patch.body.set), [['people.default_profile', 'b1'], ['people.lock_target', 'default_profile']],
    'the profile is named before it becomes the target');
});

test('When locked, show: switching to the default profile asks first and says what is switched off', { skip: dom.skip }, async () => {
  const { pane, rec } = await mount({ routes: { 'PATCH /api/settings': () => [200, { ok: true }] } });
  const g = pane.querySelector('#people-lock');
  const sel = g.querySelector('select');
  sel.value = 'u1';
  sel.dispatchEvent(new window.Event('change'));
  await settle();
  assert.match(rec.confirms[0], /Agent bots and voice cloning will be switched off for Riley/);
});

test('When locked, show: a refusal puts the choice back; back to the list sends profile_list', { skip: dom.skip }, async () => {
  const p = PEOPLE(); p.profiles[2].lock_target = true;
  const { pane } = await mount({ people: p, routes: { 'PATCH /api/settings': () => [422, { detail: 'Pick an existing, enabled profile.' }] } });
  const g = pane.querySelector('#people-lock');
  assert.equal(g.querySelector('input[value="default_profile"]').checked, true);
  assert.match(g.textContent, /The Riley profile/);
  assert.match(g.querySelector('.pe-fence').textContent, /can't be switched on for Riley/);
  g.querySelector('input[value="profile_list"]').checked = true;
  g.querySelector('input[value="profile_list"]').dispatchEvent(new window.Event('change'));
  await settle();
  assert.equal(g.querySelector('input[value="default_profile"]').checked, true, 'refused: back where it was');
  assert.match(g.querySelector('.people-error').textContent, /Pick an existing/);
});

test('When locked, show: with only Admins there is no default profile to choose', { skip: dom.skip }, async () => {
  const p = PEOPLE(); p.profiles = p.profiles.slice(0, 2);
  const { pane } = await mount({ people: p });
  const g = pane.querySelector('#people-lock');
  assert.equal(g.querySelector('input[value="default_profile"]').disabled, true);
  assert.match(g.textContent, /Add a User or Basic profile first/);
});

test('Devices: remembered devices with Forget only; the stale "open this browser as" text is gone', { skip: dom.skip }, async () => {
  const { pane } = await mount();
  assert.equal(pane.querySelector('#people-device-select'), null);
  const devs = [...pane.querySelectorAll('.people-device')];
  assert.equal(devs.length, 2);
  assert.match(devs[0].textContent, /Hall tablet \(this device\)/);
  for (const d of devs) assert.doesNotMatch(d.textContent, /Unbind|Opens as|Asks who/);
  const body = pane.querySelector('#people-devices').textContent;
  assert.doesNotMatch(body, /Choose who this browser opens as|Restricted profile can be used/);
});

test('Activity: a real table with counts only, and no "Limit hits" column (there are no limits)', { skip: dom.skip }, async () => {
  const { pane } = await mount();
  const table = pane.querySelector('.people-table');
  assert.deepEqual([...table.querySelectorAll('thead th')].map((h) => h.textContent),
    ['Day', 'Sign-ins', 'Messages', 'New chats', 'Uploads', 'Feedback']);
  assert.match(pane.querySelector('.people-audit').textContent, /Profile created/);
});

test('Machine access shows the rotated key once, with a Copy button', { skip: dom.skip }, async () => {
  const { pane, rec } = await mount();
  const group = pane.querySelector('#people-machine');
  assert.ok(group);
  btn(group, 'Rotate key').click();
  await settle();
  assert.equal(rec.confirms.length, 1, 'rotating asks first');
  const keyBox = group.querySelector('.people-key');
  assert.equal(keyBox.querySelector('input').value, 'dp_secret_key');
  btn(keyBox, 'Done').click();
  assert.equal(keyBox.querySelector('input'), null, 'the key is gone from the page');
});

test('the pane keeps the caller\'s groups after Devices and Activity last but the extras', { skip: dom.skip }, async () => {
  const mk = (id) => { const n = document.createElement('section'); n.id = id; return n; };
  const { pane } = await mount({ deps: { extraGroups: () => ({ afterDevices: [mk('x-rules')], end: [mk('x-adv')] }), collapseActivity: true } });
  const order = [...pane.children].map((n) => n.id || n.dataset.anchor || n.className);
  const at = (x) => order.findIndex((o) => String(o).includes(x));
  assert.ok(at('people-lock-host') > at('people') && at('people-lock-host') > at('people-editor-host'), order.join(' | '));
  assert.ok(at('device') < at('x-rules') && at('x-rules') < at('machine') && at('machine') < at('activity'), order.join(' | '));
  assert.equal(order.at(-1), 'x-adv');
  assert.ok(!pane.querySelector('details.people-activity-sub').open);
});

test('every control in the People pane has a label', { skip: dom.skip }, async () => {
  const { pane } = await mount();
  const ed = await edit(pane, 'u1');
  for (const c of pane.querySelectorAll('input:not([type="hidden"]), select')) {
    const labelled = c.getAttribute('aria-label') || (c.id && pane.querySelector(`label[for="${c.id}"]`)) || c.closest('label');
    assert.ok(labelled, `unlabelled control: ${c.outerHTML.slice(0, 90)}`);
  }
  void ed;
});

// ------------------------------------------------------------------ Characters

const charCtx = (over = {}) => {
  const rec = { toasts: [] };
  return { rec, ctx: { toast: (m, err) => rec.toasts.push([m, !!err]), openTab() {}, reload: async () => {}, noClose: true, skip: ['personality'], ...over } };
};

test('Characters: the details panel has Hide from lists and Copy to a profile; no "who can see it" select', { skip: dom.skip }, async () => {
  await boot(); routes();
  const { ctx } = charCtx();
  const panel = await CH.characterPanel({ id: 'atlas', name: 'Atlas' }, ctx);
  assert.ok(panel.querySelector('.chr-hide input[role="switch"]'));
  assert.match(panel.textContent, /Hide from lists/);
  assert.match(panel.textContent, /Copy to a profile/);
  assert.doesNotMatch(panel.textContent, /Who it is for|Restricted|Members and up/);
  assert.equal(panel.querySelector('.chr-copy select').textContent.includes('Sam'), false, 'a Basic profile owns no bots');
  assert.deepEqual([...panel.querySelectorAll('.chr-copy option')].map((o) => o.textContent), ['Boss', 'Ops', 'Riley']);
  assert.doesNotMatch(panel.textContent, /null/);
});

test('Characters: Hide saves the hidden flag through the character route', { skip: dom.skip }, async () => {
  await boot(); routes({ routes: { 'PATCH /api/characters/atlas': () => [200, {}] } });
  const { ctx } = charCtx();
  const panel = await CH.characterPanel({ id: 'atlas', name: 'Atlas' }, ctx);
  const sw = panel.querySelector('.chr-hide input');
  sw.checked = true;
  apiCalls.length = 0;
  btn(panel, 'Save details').click();
  await settle();
  assert.deepEqual(call('PATCH /api/characters/atlas').body, { hidden: true });
});

test('Characters: Copy goes to the chosen profile; the Admin is told they will not see the copy', { skip: dom.skip }, async () => {
  await boot(); routes({ routes: { 'POST /api/characters/atlas/clone-to/u1': () => [201, { bot_id: 'p1', voice_cleared: true }] } });
  const { ctx, rec } = charCtx();
  const panel = await CH.characterPanel({ id: 'atlas', name: 'Atlas' }, ctx);
  const sel = panel.querySelector('.chr-copy select');
  sel.value = 'u1';
  sel.dispatchEvent(new window.Event('change'));
  assert.match(panel.querySelector('.chr-copy [role="note"]').textContent, /won't be able to see or edit Riley's copy/);
  apiCalls.length = 0;
  btn(panel.querySelector('.chr-copy'), 'Copy').click();
  await settle();
  assert.ok(call('POST /api/characters/atlas/clone-to/u1'));
  assert.match(rec.toasts.at(-1)[0], /Copied to Riley\..*voice wasn't copied/);
});

test('Characters: an agent bot cannot be copied, and says so', { skip: dom.skip }, async () => {
  await boot(); routes();
  const { ctx } = charCtx();
  const panel = await CH.characterPanel({ id: 'runner', name: 'Runner' }, ctx);
  assert.match(panel.querySelector('.chr-copy').textContent, /Agent bots can't be copied/);
  assert.equal(panel.querySelector('.chr-copy select'), null);
});

test('Characters: delete shows what stays behind and offers Hide instead', { skip: dom.skip }, async () => {
  await boot(); routes({ routes: {
    'GET /api/characters/atlas/delete-preview': () => [200, { chats: 12, profiles: 3 }],
    'PATCH /api/characters/atlas': () => [200, {}],
    'DELETE /api/characters/atlas': () => [200, { removed: 'atlas' }],
  } });
  const { ctx } = charCtx();
  let p = CH.removeCharacter({ id: 'atlas', name: 'Atlas', hidden: false }, ctx);
  await settle();
  assert.match(dialog().textContent, /12 chats in 3 profiles stay/);
  btn(dialog(), 'Hide instead').click();
  assert.equal(await p, 'hidden');
  assert.deepEqual(call('PATCH /api/characters/atlas').body, { hidden: true });
  apiCalls.length = 0;
  p = CH.removeCharacter({ id: 'atlas', name: 'Atlas', hidden: false }, ctx);
  await settle();
  btn(dialog(), 'Delete bot').click();
  assert.equal(await p, 'deleted');
  assert.ok(call('DELETE /api/characters/atlas'));
  // an already hidden bot has no "Hide instead"
  p = CH.removeCharacter({ id: 'atlas', name: 'Atlas', hidden: true }, ctx);
  await settle();
  assert.equal(btn(dialog(), 'Hide instead'), undefined);
  btn(dialog(), 'Cancel').click();
  assert.equal(await p, null);
});

// ------------------------------------------------------------------ source guards

test('the three files carry no 3.x role words, quiet hours, budgets or device binding', () => {
  for (const f of ['js/people.js', 'js/people-perms.js', 'js/characters.js']) {
    const src = read(f).replace(/\/\/.*$/gm, '').replace(/\/\*[\s\S]*?\*\//g, '');
    assert.doesNotMatch(src, /'restricted'|'member'|quiet|budget|bindableProfiles|device_intro|\bdecoy\b|anonymous/i, f);
  }
});
