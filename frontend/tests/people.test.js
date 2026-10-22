// DisPatch Chat 3.0 -- profiles, roles and policy (frontend half).
//
//   1. js/profiles.js: the pure decisions (role gating, which credential a tile
//      asks for, the 429 countdown, the budgets form <-> API payload, activity
//      rows, usage lines, ws error frames). No DOM.
//   2. js/api.js: the 3.0 calls exist with the contract's names, post the
//      contract's bodies, and surface error.code / fix / retry_after.
//   3. js/signin.js + js/people.js against the real index.html markup.
//   4. Wiring: the admin rule in main.js / index.html, and the service worker.

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { domSkip } from './_require-dom.js';
import * as P from '../static/js/profiles.js';

const HERE = dirname(fileURLToPath(import.meta.url));
const STATIC = join(HERE, '..', 'static');
const read = (f) => readFileSync(join(STATIC, f), 'utf8');
const HTML = read('index.html');
const MAIN = read('js/main.js');
const API_SRC = read('js/api.js');
const SW = read('sw.js');
const EN = JSON.parse(read('locales/en.json'));

const require = createRequire(import.meta.url);
let jsdom = null;
try { jsdom = require('jsdom'); } catch { /* not installed - DOM tests skip */ }
const dom = { skip: domSkip(jsdom ? false : 'jsdom is not installed') };

// ------------------------------------------------------------------ roles

test('normalizeRole trusts the server; anything unknown reads as the lowest type', () => {
  assert.equal(P.normalizeRole({ role: 'user' }), 'user');
  assert.equal(P.normalizeRole({ role: 'admin', protected: true }), 'admin');
  assert.equal(P.normalizeRole({ role: 'basic' }), 'basic');
  assert.equal(P.normalizeRole({ role: 'member' }), 'basic', 'the 3.x words are gone');
  assert.equal(P.normalizeRole({ pin_set: false }), 'basic');
  assert.equal(P.normalizeRole(null), 'basic');
});

test('admin surfaces are for Admins only; a User is signed in but not an admin', () => {
  assert.equal(P.isAdminRole('admin'), true);
  assert.equal(P.isAdminRole('user'), false);
  assert.equal(P.isAdminRole('basic'), false);
  assert.equal(P.isAdminRole(undefined), false);
});

test('canManage mirrors the server: Admins manage everyone but the protected Admin', () => {
  assert.equal(P.canManage('admin', { role: 'admin' }), true);
  assert.equal(P.canManage('admin', { role: 'admin', protected: true }), false);
  assert.equal(P.canManage('admin', 'user'), true);
  assert.equal(P.canManage('admin', { role: 'basic' }), true);
  assert.equal(P.canManage('user', { role: 'basic' }), false);
  assert.equal(P.canManage('basic', { role: 'user' }), false);
});

test('the Admin type is offered to Admins, once the protected Admin has a password', () => {
  assert.deepEqual(P.roleOptions('admin', true), { options: ['admin', 'user', 'basic'], adminBlocked: null });
  assert.deepEqual(P.roleOptions('admin', false), { options: ['user', 'basic'], adminBlocked: 'owner_needs_password' });
  assert.deepEqual(P.roleOptions('user', true), { options: ['user', 'basic'], adminBlocked: 'not_admin' });
});

test('the rail groups Mine and Household, and a household bot the profile copied stands down', () => {
  const bots = [
    { id: 'atlas', name: 'Atlas', scope: 'household' }, { id: 'helper', name: 'Helper', scope: 'household' },
    { id: 'p1', name: 'Atlas', scope: 'personal', cloned_from: 'atlas' }, { id: 'p2', name: 'Mine', scope: 'personal' },
  ];
  const g = P.railGroups(bots);
  assert.deepEqual(g.mine.map((b) => b.id), ['p1', 'p2']);
  assert.deepEqual(g.household.map((b) => b.id), ['helper'], 'one Atlas, not two');
  assert.deepEqual(P.railGroups(bots.slice(0, 2)).household.map((b) => b.id), ['atlas', 'helper']);
  assert.deepEqual(P.railGroups(null), { mine: [], household: [] });
  assert.equal(P.isMineBot({ scope: 'personal' }), true);
  assert.equal(P.isMineBot({ scope: 'household' }), false);
});

// ------------------------------------------------------------- credentials

test('a tile\'s credential kind maps to the inputs on offer', () => {
  assert.deepEqual(P.credentialModes('password'), ['password']);
  assert.deepEqual(P.credentialModes('pin_or_password'), ['password', 'pin']);
  assert.deepEqual(P.credentialModes('pin'), ['pin']);
  assert.deepEqual(P.credentialModes('none'), []);
  assert.deepEqual(P.credentialModes(undefined), []);
});

test('only the secret in use goes into the sign-in options', () => {
  assert.deepEqual(P.credentialOptions('pin', '1234'), { pin: '1234' });
  assert.deepEqual(P.credentialOptions('password', 'hunter22', true), { password: 'hunter22', remember: true });
  assert.deepEqual(P.credentialOptions('pin', ''), {});
});

test('the keypad stays when the only profile is the protected Admin and the install has a PIN', () => {
  const owner = (credential) => ({ id: 'o', name: 'Ada', role: 'admin', protected: true, credential });
  const kid = { id: 'k', name: 'Kid', role: 'basic', credential: 'none' };
  assert.equal(P.signInMode({ claimed: true, pin_set: true, profiles: [owner('pin_or_password')] }), 'keypad');
  assert.equal(P.signInMode({ claimed: true, pinSet: true, profiles: [owner('pin_or_password')] }), 'keypad', 'state.auth spelling');
  assert.equal(P.signInMode({ claimed: true, profiles: [owner('pin')] }), 'keypad');
  assert.equal(P.signInMode({ claimed: true, pin_set: false, profiles: [owner('pin_or_password')] }), 'tiles', 'a password-only Owner cannot use the keypad');
  assert.equal(P.signInMode({ claimed: true, pin_set: true, profiles: [owner('pin_or_password'), kid] }), 'tiles');
  assert.equal(P.signInMode({ claimed: true, profiles: [] }), 'keypad');
  assert.equal(P.signInMode({ claimed: false, profiles: [owner('pin'), owner('pin')] }), 'keypad');
  assert.equal(P.signInMode({ pin_set: true }), 'keypad');        // a 2.x server sends no profiles
});

test('tiles list the shared default profile first, then the protected Admin, then by type, then by name', () => {
  const names = P.sortTiles([
    { name: 'Zed', role: 'user' }, { name: 'Kid', role: 'basic' }, { name: 'Ann', role: 'user' },
    { name: 'Boss', role: 'admin', protected: true }, { name: 'Ops', role: 'admin' }, { name: 'DisPatch', role: 'user', shared: true },
  ]).map((p) => p.name);
  assert.deepEqual(names, ['DisPatch', 'Boss', 'Ops', 'Ann', 'Zed', 'Kid']);
});

test('avatars: same-origin picture, short glyph, or the initial -- never a foreign URL', () => {
  assert.deepEqual(P.avatarSpec({ name: 'Sam', avatar: '/static/avatars/a.png' }), { kind: 'img', src: '/static/avatars/a.png' });
  assert.deepEqual(P.avatarSpec({ name: 'Sam', avatar: '🦊' }), { kind: 'glyph', text: '🦊' });
  assert.deepEqual(P.avatarSpec({ name: 'sam', avatar: '' }), { kind: 'initial', text: 'S' });
  assert.equal(P.avatarSpec({ name: 'Sam', avatar: '//evil.example/x.png' }).kind, 'initial');
  assert.equal(P.avatarSpec({ name: 'Sam', avatar: 'https://evil.example/x.png' }).kind, 'initial');
  assert.deepEqual(P.avatarSpec(null), { kind: 'initial', text: '?' });
});

// ------------------------------------------------------------ the countdown

test('countdown formatting and remaining seconds', () => {
  assert.equal(P.formatCountdown(75), '1:15');
  assert.equal(P.formatCountdown(9), '0:09');
  assert.equal(P.formatCountdown(-3), '0:00');
  assert.equal(P.remainingSeconds(10_000, 7_400), 3);
  assert.equal(P.remainingSeconds(10_000, 12_000), 0);
});

test('createCountdown ticks once a second, finishes at zero, and stop() cancels', () => {
  let t = 0;
  const timers = [];
  const ticks = [];
  let done = 0;
  const cd = P.createCountdown({
    seconds: 3, now: () => t, onTick: (n) => ticks.push(n), onDone: () => { done++; },
    setTimer: (fn) => { timers.push(fn); return timers.length; }, clearTimer: () => { timers.length = 0; },
  });
  assert.equal(cd.deadline, 3000);
  assert.deepEqual(ticks, [3]);
  for (let i = 0; i < 3; i++) { t += 1000; timers[0] && timers[0](); }
  assert.deepEqual(ticks, [3, 2, 1, 0]);
  assert.equal(done, 1, 'onDone fires exactly once');

  let stopped = 0;
  const cd2 = P.createCountdown({ seconds: 5, now: () => t, setTimer: () => 1, clearTimer: () => { stopped++; } });
  cd2.stop();
  assert.equal(stopped, 1);

  let d3 = 0;
  P.createCountdown({ seconds: 0, onDone: () => { d3++; }, setTimer: () => 1, clearTimer: () => {} });
  assert.equal(d3, 1, 'a zero wait is done at once');
});

// ------------------------------------------------------------------ budgets

test('budgets: the server row -> the form strings', () => {
  const f = P.budgetsToForm({
    budgets: { minutes_per_day: 45, quiet_hours: '20:30-07:00', turns: 100, upload_mb: 50 },
    session_timeout: 1800,
  });
  assert.deepEqual(f, {
    minutes: '45', quietStart: '20:30', quietEnd: '07:00', turns: '100', threads: '', feedback: '',
    uploadMb: '50', sessionMinutes: '30',
  });
  assert.deepEqual(P.budgetsToForm({}), P.EMPTY_BUDGET_FORM);
  assert.equal(P.budgetsToForm({ budgets: { quiet_hours: '7:05-9:00' } }).quietStart, '07:05');
});

test('budgets: the form -> the API payload; an empty box is absent, not zero', () => {
  const r = P.formToBudgets({ minutes: '45', quietStart: '20:30', quietEnd: '07:00', turns: '100', threads: '', feedback: '0', uploadMb: '', sessionMinutes: '30' });
  assert.deepEqual(r.errors, {});
  assert.deepEqual(r.budgets, { minutes_per_day: 45, quiet_hours: '20:30-07:00', turns: 100, feedback: 0 });
  assert.equal(r.session_timeout, 1800);
  assert.deepEqual(P.formToBudgets({}).budgets, {});
});

test('budgets: bad input is reported by field and nothing is silently coerced', () => {
  assert.equal(P.formToBudgets({ minutes: '0' }).errors.minutes, 'range_1_1440');
  assert.equal(P.formToBudgets({ minutes: '1441' }).errors.minutes, 'range_1_1440');
  assert.equal(P.formToBudgets({ minutes: '4.5' }).errors.minutes, 'range_1_1440');
  assert.equal(P.formToBudgets({ turns: '-1' }).errors.turns, 'range_0_100000');
  assert.equal(P.formToBudgets({ uploadMb: '100001' }).errors.uploadMb, 'range_0_100000');
  assert.equal(P.formToBudgets({ quietStart: '20:00' }).errors.quiet, 'both_times');
  assert.equal(P.formToBudgets({ quietStart: '20:00', quietEnd: '20:00' }).errors.quiet, 'same_times');
  assert.equal(P.formToBudgets({ sessionMinutes: '0' }).errors.sessionMinutes, 'range_1_43200');
  assert.equal(P.formToBudgets({ sessionMinutes: '43201' }).errors.sessionMinutes, 'range_1_43200');
});

test('quiet hours parse and format symmetrically', () => {
  assert.deepEqual(P.parseQuietHours('20:30-07:00'), { start: '20:30', end: '07:00' });
  assert.deepEqual(P.parseQuietHours('garbage'), { start: '', end: '' });
  assert.equal(P.formatQuietHours('20:30', '07:00'), '20:30-07:00');
  assert.equal(P.formatQuietHours('20:30', ''), '');
});

test('createPayload: a PIN is optional on User and Basic, an Admin needs a password', () => {
  assert.deepEqual(P.createPayload({ name: ' Sam ', role: 'user', pin: '1234' }).body, { name: 'Sam', role: 'user', pin: '1234' });
  assert.equal(P.createPayload({ name: 'Sam', role: 'user', pin: '12' }).errors.pin, 'min_4');
  assert.deepEqual(P.createPayload({ name: 'Sam', role: 'user' }).errors, {});
  assert.deepEqual(P.createPayload({ name: 'Kid', role: 'basic' }).errors, {});
  assert.equal(P.createPayload({ name: 'Kid', role: 'basic', pin: '1' }).errors.pin, 'min_4');
  assert.deepEqual(P.createPayload({ name: 'Kid', role: 'basic', householdBots: ['atlas'] }).body, { name: 'Kid', role: 'basic', household_bots: ['atlas'] });
  assert.equal(P.createPayload({ name: 'Ops', role: 'admin', password: 'short' }).errors.password, 'min_8');
  assert.deepEqual(P.createPayload({ name: 'Ops', role: 'admin', password: 'longenough' }).body, { name: 'Ops', role: 'admin', password: 'longenough' });
  assert.equal(P.createPayload({ name: '  ', role: 'user', pin: '1234' }).errors.name, 'required');
  assert.equal(P.createPayload({ name: 'Kid', role: 'basic', sessionMinutes: '10' }).body.session_timeout, 600);
});

test('patchPayload sends only what changed', () => {
  const person = { id: 'p1', name: 'Kid', role: 'basic', avatar: '', disabled: false, session_timeout: 600 };
  const same = P.budgetsToForm(person);
  assert.deepEqual(P.patchPayload(person, { ...same, name: 'Kid', role: 'basic', avatar: '', disabled: false }).patch, {});
  const changed = P.patchPayload(person, { ...same, name: 'Kiddo', role: 'user', avatar: '🦊', disabled: true });
  assert.deepEqual(changed.patch, { name: 'Kiddo', role: 'user', avatar: '🦊', disabled: true });
  assert.equal(P.patchPayload({ ...person, role: 'admin', protected: true }, { ...same, name: 'Kid', role: 'user' }).patch.role, undefined, 'the protected Admin\'s role is never patched');
});

// ----------------------------------------------------------------- activity

test('activity rows: newest first, six columns, zero-filled, at most 7 days', () => {
  const days = Array.from({ length: 9 }, (_, i) => ({ day: `2026-10-${String(10 + i).padStart(2, '0')}`, counts: i === 8 ? { turn: 4, upload: 1, odd_kind: 9 } : {} }));
  const rows = P.activityRows({ days }, 7);
  assert.equal(rows.length, 7);
  assert.equal(rows[0].day, '2026-10-18');
  assert.deepEqual(rows[0].counts, { login: 0, turn: 4, thread: 0, upload: 1, feedback: 0, limit_hit: 0 });
  assert.equal(rows[0].total, 5, 'unknown kinds are not summed into the visible columns');
  assert.deepEqual(P.activityRows(null), []);
  const totals = P.activityTotals(rows);
  assert.equal(totals.turn, 4);
});

// ------------------------------------------------------------ policy frames

test('ws error frames: a permission refusal is told apart from the rest', () => {
  assert.equal(P.wsErrorKind({ code: 'permission_denied' }).kind, 'denied');
  assert.equal(P.wsErrorKind({ message: 'boom' }).kind, 'plain');
  assert.equal(P.wsErrorKind(null).kind, 'plain');
});

// --------------------------------------------------------------------- api.js

const NAMES = ['me', 'login', 'switchProfile', 'setupOwner', 'changePassword', 'apiTokenInfo', 'rotateApiToken',
  'people', 'createPerson', 'patchPerson', 'deletePerson', 'setPersonPin', 'clearPersonPin', 'setPersonPassword',
  'signOutPerson', 'devices', 'forgetDevice', 'peopleActivity', 'peopleAudit',
  'rolePreview', 'changeRole', 'setHouseholdBots', 'deletePreview'];

test('api.js no longer binds a device to a profile (4.0: the route is gone)', () => {
  assert.doesNotMatch(API_SRC, /bindThisDevice|devices\/this/);
});

test('api.js declares every 3.0 call the backend parity test greps for', () => {
  for (const n of NAMES) assert.match(API_SRC, new RegExp(`\\b${n}:\\s`), n);
});

test('and keeps the 2.x auth calls', () => {
  for (const n of ['authStatus', 'unlock', 'lock', 'setupPin', 'recover', 'rememberConfig', 'forgetDevices']) {
    assert.match(API_SRC, new RegExp(`\\b${n}:\\s`), n);
  }
});

const apiCalls = [];
let apiRoutes = {};
function fakeFetch(win) {
  return async (u, opts = {}) => {
    const key = `${(opts.method || 'GET').toUpperCase()} ${u}`;
    apiCalls.push({ key, body: opts.body ? JSON.parse(opts.body) : null });
    const h = apiRoutes[key];
    const [status, body, headers] = h ? h(opts) : [404, { detail: 'Not Found' }];
    return {
      ok: status >= 200 && status < 300, status, statusText: String(status),
      headers: new win.Headers(headers || {}), json: async () => body,
    };
  };
}

async function loadApi() {
  const { JSDOM } = jsdom;
  const win = new JSDOM('<!doctype html><body></body>', { url: 'http://127.0.0.1:18765/' }).window;
  globalThis.fetch = fakeFetch(win);
  apiCalls.length = 0;
  return import(`../static/js/api.js?t=${Math.random()}`);
}

test('loginBody sends only the secret in use, and only the id or the name', () => {
  // pure: no DOM needed
  return import(`../static/js/api.js?t=${Math.random()}`).then(({ loginBody }) => {
    assert.deepEqual(loginBody('p1', { pin: '1234' }), { profile_id: 'p1', pin: '1234' });
    assert.deepEqual(loginBody({ name: 'Sam' }, { password: 'pw', remember: true }), { name: 'Sam', password: 'pw', remember: true });
    assert.deepEqual(loginBody('p1'), { profile_id: 'p1' });
    assert.deepEqual(loginBody('p1', { pin: '', password: undefined }), { profile_id: 'p1' });
  });
});

test('errorExtras reads the 3.0 envelope, retry_after and the Retry-After header', () => {
  return import(`../static/js/api.js?t=${Math.random()}`).then(({ errorExtras }) => {
    assert.deepEqual(errorExtras({ detail: 'x', error: { code: 'password_required', message: 'm', fix: 'Set one', docs: '/d' } }),
      { code: 'password_required', fix: 'Set one', docs: '/d', userMessage: 'm' });
    assert.equal(errorExtras({ detail: 'x', retry_after: 12.2 }).retryAfter, 13);
    assert.equal(errorExtras({ detail: 'x' }, { get: (h) => (h === 'Retry-After' ? '7' : null) }).retryAfter, 7);
    assert.equal(errorExtras({ detail: 'x', required_permission: 'manage_people' }).requiredPermission, 'manage_people');
    assert.deepEqual(errorExtras({ detail: 'plain' }), {});
    assert.deepEqual(errorExtras(null), {});
  });
});

test('api.login / switchProfile / setupOwner / changePassword post the contract bodies', { skip: dom.skip }, async () => {
  const { api } = await loadApi();
  apiRoutes = {
    'POST /api/auth/login': () => [200, { ok: true, role: 'user' }],
    'POST /api/auth/switch': () => [200, { ok: true, role: 'admin' }],
    'POST /api/auth/setup': () => [200, { ok: true, claimed: true }],
    'POST /api/auth/password': () => [200, { ok: true }],
    'POST /api/auth/api-token': () => [200, { ok: true, api_token: 'k' }],
    'GET /api/people/activity?profile_id=p1&days=7': () => [200, { summary: { days: [] } }],
    'GET /api/people/activity': () => [200, { summary: { days: [] } }],
    'GET /api/people/p1/role/preview?role=basic': () => [200, { counts: {}, token: 'tk', deletes: true }],
    'POST /api/people/p1/role': () => [200, {}],
    'PUT /api/people/p1/household-bots': () => [200, {}],
    'GET /api/people/p1/delete/preview': () => [200, { counts: {}, token: 'tk2' }],
    'POST /api/people/p1/delete': () => [200, {}],
  };
  await api.login('p1', { pin: '1234', remember: true });
  await api.switchProfile('p2', { password: 'pw' });
  await api.setupOwner({ name: 'Boss', password: 'longenough', setupToken: 'tok' });
  await api.setupOwner({ password: 'longenough', newPin: '4321' });
  await api.changePassword({ currentPin: '4321', newPassword: 'newpassword' });
  await api.changePassword({ current: 'old', newPassword: 'newpassword' });
  await api.rotateApiToken();
  await api.setMachineAccess({ machineLoopbackTrust: false });
  await api.peopleActivity({ profileId: 'p1', days: 7 });
  await api.peopleActivity();
  await api.rolePreview('p1', 'basic');
  await api.changeRole('p1', { role: 'basic', confirm_token: 'tk' });
  await api.setHouseholdBots('p1', ['b1']);
  await api.deletePreview('p1');
  await api.deletePerson('p1', 'tk2');
  const bodies = apiCalls.map((c) => [c.key, c.body]);
  assert.deepEqual(bodies, [
    ['POST /api/auth/login', { profile_id: 'p1', pin: '1234', remember: true }],
    ['POST /api/auth/switch', { profile_id: 'p2', password: 'pw' }],
    ['POST /api/auth/setup', { name: 'Boss', password: 'longenough', setup_token: 'tok' }],
    ['POST /api/auth/setup', { password: 'longenough', new_pin: '4321' }],
    ['POST /api/auth/password', { current_pin: '4321', new: 'newpassword' }],
    ['POST /api/auth/password', { current: 'old', new: 'newpassword' }],
    ['POST /api/auth/api-token', { rotate: true }],
    ['POST /api/auth/api-token', { machine_loopback_trust: false }],
    ['GET /api/people/activity?profile_id=p1&days=7', null],
    ['GET /api/people/activity', null],
    ['GET /api/people/p1/role/preview?role=basic', null],
    ['POST /api/people/p1/role', { role: 'basic', confirm_token: 'tk' }],
    ['PUT /api/people/p1/household-bots', { household_bots: ['b1'] }],
    ['GET /api/people/p1/delete/preview', null],
    ['POST /api/people/p1/delete', { confirm_token: 'tk2' }],
  ]);
});

test('every person route encodes its id as ONE path segment', { skip: dom.skip }, async () => {
  const { api } = await loadApi();
  apiRoutes = {};
  const evil = 'a/../b?x=1';
  const enc = encodeURIComponent(evil);
  for (const fn of [() => api.patchPerson(evil, {}), () => api.deletePerson(evil, 't'), () => api.setPersonPin(evil, '1234'),
    () => api.clearPersonPin(evil), () => api.setPersonPassword(evil, 'longenough'), () => api.signOutPerson(evil),
    () => api.forgetDevice(evil), () => api.rolePreview(evil, 'basic'), () => api.changeRole(evil, { role: 'user' }),
    () => api.setHouseholdBots(evil, 'all'), () => api.deletePreview(evil)]) {
    await fn().catch(() => {});
  }
  for (const c of apiCalls) assert.ok(c.key.includes(enc), c.key);
  assert.equal(apiCalls.length, 11);
});

test('a failed call carries code, fix and retryAfter without changing err.message', { skip: dom.skip }, async () => {
  const { api } = await loadApi();
  apiRoutes = {
    'POST /api/auth/login': () => [429, { detail: 'Too many attempts - wait 31s.', retry_after: 31, error: { code: 'too_many_attempts', message: 'm' } }],
    'POST /api/people': () => [409, { detail: 'Set your own password first', error: { code: 'password_required', message: 'm', fix: 'Settings > Security', docs: '/docs/x' } }],
  };
  await assert.rejects(api.login('p1', { pin: '1' }), (e) => {
    assert.equal(e.status, 429);
    assert.equal(e.code, 'too_many_attempts');
    assert.equal(e.retryAfter, 31);
    assert.match(e.message, /^429: Too many attempts/);
    return true;
  });
  await assert.rejects(api.createPerson({ name: 'A', role: 'admin', password: 'longenough' }), (e) => {
    assert.equal(e.code, 'password_required');
    assert.equal(e.fix, 'Settings > Security');
    assert.equal(e.docs, '/docs/x');
    assert.match(e.message, /^409: Set your own password first/);
    return true;
  });
});

test('signin_required reaches its hook and does not trip onLocked; a plain 401 does', { skip: dom.skip }, async () => {
  const mod = await loadApi();
  const seen = [];
  mod.setOnSignInRequired(() => seen.push(['signin']));
  mod.setOnLocked(() => seen.push(['locked']));
  apiRoutes = {
    'GET /api/threads?bot_id=b': () => [401, { detail: 'Sign in', error: { code: 'signin_required', message: 'x' } }],
    'GET /api/files': () => [401, { detail: 'expired' }],
  };
  await assert.rejects(mod.api.threads('b'), (e) => e.signInRequired === true && e.code === 'signin_required');
  await assert.rejects(mod.api.files());
  assert.deepEqual(seen, [['signin'], ['locked']]);
  assert.equal(typeof mod.setOnPolicyBlock, 'undefined', 'the time-limit hook went with the limits');
});

// ------------------------------------------------------------- DOM: signin.js

const tick = () => new Promise((r) => setTimeout(r, 0));

async function boot(url = 'http://127.0.0.1:18765/') {
  const { JSDOM } = jsdom;
  const win = new JSDOM(HTML, { url, runScripts: 'outside-only' }).window;
  const put = (k, v) => Object.defineProperty(globalThis, k, { value: v, configurable: true, writable: true });
  put('window', win);
  put('document', win.document);
  put('location', win.location);
  put('navigator', win.navigator);
  put('localStorage', win.localStorage);
  put('Node', win.Node);
  put('HTMLElement', win.HTMLElement);
  put('HTMLInputElement', win.HTMLInputElement);
  apiCalls.length = 0;
  globalThis.fetch = async (u, opts = {}) => {
    const key = `${(opts.method || 'GET').toUpperCase()} ${u}`;
    apiCalls.push({ key, body: opts.body ? JSON.parse(opts.body) : null });
    if (key === 'GET /static/locales/en.json') return { ok: true, status: 200, json: async () => EN, headers: new win.Headers() };
    const h = apiRoutes[key];
    const [status, body] = h ? h(opts) : [404, { detail: 'Not Found' }];
    return { ok: status >= 200 && status < 300, status, statusText: String(status), headers: new win.Headers(), json: async () => body };
  };
  const i18n = await import('../static/js/i18n.js?v=4');
  await i18n.init('en');
  return win;
}

const PROFILES = [
  { id: 'o1', name: 'Boss', role: 'admin', protected: true, avatar: '', credential: 'pin_or_password' },
  { id: 'a1', name: 'Ops', role: 'admin', avatar: '', credential: 'password' },
  { id: 'm1', name: 'Sam', role: 'user', avatar: '🦊', credential: 'pin' },
  { id: 'r1', name: 'Kid', role: 'basic', avatar: '', credential: 'none' },
];

test('sign-in: one tile per profile, with name and role label', { skip: dom.skip }, async () => {
  const win = await boot();
  const { renderSignIn } = await import(`../static/js/signin.js?t=${Math.random()}`);
  const host = win.document.getElementById('lock-tiles');
  renderSignIn(host, { profiles: PROFILES, onSubmit: async () => {} });
  const tiles = [...host.querySelectorAll('.signin-tile')];
  assert.deepEqual(tiles.map((b) => b.querySelector('.signin-name').textContent), ['Boss', 'Ops', 'Sam', 'Kid']);
  assert.deepEqual(tiles.map((b) => b.querySelector('.signin-role').textContent), ['Admin', 'Admin', 'User', 'Basic']);
  assert.ok(tiles.every((b) => b.tagName === 'BUTTON' && b.type === 'button'), 'keyboard operable');
});

test('sign-in: a "none" tile signs in at once; a password tile asks for the password', { skip: dom.skip }, async () => {
  const win = await boot();
  const { renderSignIn } = await import(`../static/js/signin.js?t=${Math.random()}`);
  const host = win.document.getElementById('lock-tiles');
  const sent = [];
  renderSignIn(host, { profiles: PROFILES, onSubmit: async (p, o) => { sent.push([p.id, o]); } });
  host.querySelector('[data-profile="r1"]').click();
  await tick();
  assert.deepEqual(sent, [['r1', { remember: false }]]);

  host.querySelector('[data-profile="a1"]').click();
  const input = host.querySelector('#signin-secret');
  assert.ok(input, 'a credential field appeared');
  assert.equal(input.type, 'password');
  assert.equal(host.querySelector('label[for="signin-secret"]').textContent, 'Password for Ops');
  assert.equal(host.querySelector('.signin-toggle'), null, 'an Admin has no PIN to switch to');
  input.value = 'hunter-22';
  host.querySelector('form').dispatchEvent(new win.Event('submit', { cancelable: true }));
  await tick();
  assert.deepEqual(sent[1], ['a1', { password: 'hunter-22', remember: false }]);
});

test('sign-in: the Owner tile offers password or PIN, and Back returns to the tiles', { skip: dom.skip }, async () => {
  const win = await boot();
  const { renderSignIn } = await import(`../static/js/signin.js?t=${Math.random()}`);
  const host = win.document.getElementById('lock-tiles');
  const sent = [];
  renderSignIn(host, { profiles: PROFILES, onSubmit: async (p, o) => { sent.push(o); } });
  host.querySelector('[data-profile="o1"]').click();
  const toggle = host.querySelector('.signin-toggle');
  assert.equal(toggle.textContent, 'Use PIN instead');
  toggle.click();
  assert.equal(host.querySelector('#signin-secret').getAttribute('inputmode'), 'numeric');
  host.querySelector('#signin-secret').value = '4321';
  host.querySelector('form').dispatchEvent(new win.Event('submit', { cancelable: true }));
  await tick();
  assert.deepEqual(sent, [{ pin: '4321', remember: false }]);
  host.querySelector('.signin-back').click();
  assert.equal(host.querySelectorAll('.signin-tile').length, 4);
});

test('sign-in: a wrong credential shows the message and clears the field', { skip: dom.skip }, async () => {
  const win = await boot();
  const { renderSignIn } = await import(`../static/js/signin.js?t=${Math.random()}`);
  const host = win.document.getElementById('lock-tiles');
  renderSignIn(host, {
    profiles: PROFILES,
    onSubmit: async () => { const e = new Error('401: Incorrect PIN or password'); e.status = 401; e.code = 'incorrect_credential'; throw e; },
  });
  host.querySelector('[data-profile="m1"]').click();
  const input = host.querySelector('#signin-secret');
  input.value = '0000';
  host.querySelector('form').dispatchEvent(new win.Event('submit', { cancelable: true }));
  await tick();
  const err = host.querySelector('#signin-cred-error');
  assert.equal(err.hidden, false);
  assert.equal(err.textContent, 'That PIN or password is not right.');
  assert.equal(input.value, '');
  assert.equal(err.getAttribute('role'), 'alert');
});

test('sign-in: a 429 disables submit and counts down', { skip: dom.skip }, async () => {
  const win = await boot();
  const { renderSignIn } = await import(`../static/js/signin.js?t=${Math.random()}`);
  const host = win.document.getElementById('lock-tiles');
  renderSignIn(host, {
    profiles: PROFILES,
    onSubmit: async () => { const e = new Error('429: slow down'); e.status = 429; e.retryAfter = 90; throw e; },
  });
  host.querySelector('[data-profile="m1"]').click();
  host.querySelector('#signin-secret').value = '1234';
  host.querySelector('form').dispatchEvent(new win.Event('submit', { cancelable: true }));
  await tick();
  const go = host.querySelector('.signin-go');
  assert.equal(go.disabled, true, 'submit is disabled while throttled');
  assert.match(host.querySelector('.signin-wait').textContent, /1:30/);
  assert.match(host.querySelector('#signin-cred-error').textContent, /90 seconds/);
});

test('setup: the token field exists only for a network install, and the form validates before sending', { skip: dom.skip }, async () => {
  const win = await boot();
  const { renderSetup } = await import(`../static/js/signin.js?t=${Math.random()}`);
  const sent = [];
  const remote = renderSetup({ setupRequired: true, onSubmit: async (v) => { sent.push(v); } });
  win.document.body.append(remote.form);
  assert.ok(remote.form.querySelector('#setup-token'));
  assert.equal(remote.form.querySelector('label[for="setup-token"]').textContent, 'Setup token');
  const submit = () => remote.form.dispatchEvent(new win.Event('submit', { cancelable: true }));
  submit();
  assert.equal(sent.length, 0);
  assert.equal(remote.form.querySelector('.setup-error').textContent, 'The password needs at least 8 characters.');
  remote.form.querySelector('#setup-password').value = 'longenough';
  remote.form.querySelector('#setup-confirm').value = 'different1';
  submit();
  assert.equal(remote.form.querySelector('.setup-error').textContent, 'The two passwords do not match.');
  remote.form.querySelector('#setup-confirm').value = 'longenough';
  submit();
  assert.equal(remote.form.querySelector('.setup-error').textContent, 'Enter the setup token from the server log.');
  remote.form.querySelector('#setup-token').value = ' tok123 ';
  remote.form.querySelector('#setup-pin').value = '4321';
  submit();
  await tick();
  assert.deepEqual(sent, [{ name: 'Admin', password: 'longenough', newPin: '4321', setupToken: 'tok123' }]);

  const local = renderSetup({ setupRequired: false, onSubmit: async () => {}, idPrefix: 'loc' });
  assert.equal(local.form.querySelector('#loc-token'), null, 'no token field on the box itself');
});

// ------------------------------------------------------------- DOM: people.js

const PEOPLE = () => ({
  profiles: [
    { id: 'o1', name: 'Boss', role: 'owner', avatar: '', has_pin: true, has_password: true, budgets: {}, grants: [], denies: [], session_timeout: null, disabled: false },
    { id: 'a1', name: 'Ops', role: 'admin', avatar: '', has_pin: false, has_password: true, budgets: {}, grants: [], denies: [], session_timeout: null, disabled: false },
    { id: 'm1', name: 'Sam', role: 'member', avatar: '🦊', has_pin: true, has_password: false, budgets: {}, grants: [], denies: [], session_timeout: null, disabled: false },
    { id: 'r1', name: 'Kid', role: 'restricted', avatar: '', has_pin: false, has_password: false,
      budgets: { minutes_per_day: 45, quiet_hours: '20:30-07:00' }, grants: [], denies: [], session_timeout: 900, disabled: true },
  ],
  me: { id: 'o1', role: 'owner' },
  roles: ['owner', 'admin', 'member', 'restricted'],
  role_permissions: {},
  can_create_admin: true,
  owner_has_password: true,
  disclosure: 'x',
});

function peopleRoutes(over = {}) {
  apiRoutes = {
    'GET /api/people': () => [200, over.people || PEOPLE()],
    'GET /api/people/devices': () => [200, { devices: [
      { id: 'd1', label: 'Hall tablet', default_profile_id: 'r1', last_seen: '2026-10-17T10:00:00Z', this_device: true },
      { id: 'd2', label: '', default_profile_id: null, last_seen: null, this_device: false },
    ], this_device: 'd1' }],
    'GET /api/people/activity?days=7': () => [200, { summary: { days: [{ day: '2026-10-17', counts: { turn: 3, login: 1 } }] }, recent: [], today: {} }],
    'GET /api/people/audit': () => [200, { entries: [{ ts: '2026-10-17T10:00:00Z', kind: 'profile_created', profile_id: 'o1', subject: 'r1', detail: '' }] }],
    'GET /api/auth/api-token': () => [200, { has_token: true, machine_loopback_trust: true, machine_scopes: ['read'], all_scopes: ['read'] }],
    'POST /api/auth/api-token': () => [200, { ok: true, machine_loopback_trust: true, api_token: 'dp_secret_key' }],
    'POST /api/people': () => [201, { id: 'new' }],
    'PATCH /api/people/r1': () => [200, {}],
    'GET /api/people/m1/delete/preview': () => [200, { counts: { bots: 1, chats: 2, pictures: 0, voices: 0 }, token: 'del-tok' }],
    'POST /api/people/m1/delete': () => [200, { ok: true }],
    'POST /api/people/m1/sign-out': () => [200, { ok: true }],
    ...(over.routes || {}),
  };
}

async function mountPeople(role = 'owner', over = {}) {
  const win = await boot();
  peopleRoutes(over);
  const mod = await import(`../static/js/people.js?t=${Math.random()}`);
  const rec = { toasts: [], confirms: [] };
  const deps = {
    role,
    toast: (m, err) => rec.toasts.push([m, !!err]),
    confirm: async (m) => { rec.confirms.push(m); return true; },
    ...(over.deps || {}),
  };
  const pane = win.document.getElementById('spane-people');
  await mod.mountPeopleSettings(pane, deps);
  await tick();
  return { win, pane, rec };
}

test('People tab: This device lists remembered devices with Forget only (4.0: no binding)', { skip: dom.skip }, async () => {
  const { pane } = await mountPeople('owner');
  assert.equal(pane.querySelector('#people-device-select'), null);
  const devs = [...pane.querySelectorAll('.people-device')];
  assert.equal(devs.length, 2);
  assert.match(devs[0].textContent, /Hall tablet \(this device\)/);
  for (const d of devs) {
    assert.doesNotMatch(d.textContent, /Unbind|Opens as|Asks who/);
    assert.match(d.textContent, /Forget/);
  }
});

test('People tab: Machine access shows the rotated key once, with a Copy button', { skip: dom.skip }, async () => {
  const { pane, rec } = await mountPeople('owner');
  const group = pane.querySelector('#people-machine');
  assert.ok(group);
  assert.match(group.textContent, /A key is set\./);
  const trust = group.querySelector('input[type="checkbox"]');
  assert.equal(trust.checked, true);
  assert.match(group.textContent, /Off: they must send the API key/);
  [...group.querySelectorAll('button')].find((b) => b.textContent === 'Rotate key').click();
  await tick(); await tick();
  assert.equal(rec.confirms.length, 1, 'rotating asks first');
  const keyBox = group.querySelector('.people-key');
  assert.equal(keyBox.hidden, false);
  assert.equal(keyBox.querySelector('input').value, 'dp_secret_key');
  assert.ok([...keyBox.querySelectorAll('button')].some((b) => b.textContent === 'Copy'));
  assert.match(keyBox.textContent, /shown only once/);
  [...keyBox.querySelectorAll('button')].find((b) => b.textContent === 'Done').click();
  assert.equal(keyBox.hidden, true);
  assert.equal(keyBox.querySelector('input'), null, 'the key is gone from the page');
});

test('People & Access: the caller\'s groups go after This device; Activity & audit is closed, last but the extras', { skip: 'people.js moved to Admin/User/Basic: this test is rewritten with it by WP-FE-PEOPLE' }, async () => {
  const groups = (doc) => {
    const mk = (id) => { const n = doc.createElement('section'); n.id = id; return n; };
    return { afterDevices: [mk('x-rules'), mk('x-limits')], end: [mk('x-adv')] };
  };
  let win = null;
  const { pane } = await mountPeople('owner', { deps: { extraGroups: () => groups(globalThis.document), collapseActivity: true } });
  win = pane.ownerDocument;
  const order = [...pane.children].map((n) => n.id || n.dataset.anchor || n.className);
  const at = (x) => order.findIndex((o) => String(o).includes(x));
  assert.ok(at('device') < at('x-rules') && at('x-rules') < at('x-limits'), order.join(' | '));
  assert.ok(at('x-limits') < at('machine') && at('machine') < at('activity'), order.join(' | '));
  assert.equal(order.at(-1), 'x-adv');
  const act = pane.querySelector('details.people-activity-sub');
  assert.ok(act && !act.open, 'Activity & audit starts closed');
  assert.equal(act.dataset.anchor, 'activity');
  assert.ok(act.querySelector('#people-activity'), 'the activity group lives inside it');
  // The anchors the settings search jumps to.
  for (const a of ['people', 'device', 'machine']) assert.ok(pane.querySelector(`[data-anchor="${a}"]`), a);
  void win;
});

test('People & Access: the activity filter is a themed select', { skip: dom.skip }, async () => {
  const { pane } = await mountPeople('owner');
  assert.ok(pane.querySelector('#people-activity-who').classList.contains('ui-select'));
});

test('People tab: every control has a label (no placeholder-only inputs)', { skip: dom.skip }, async () => {
  const { pane } = await mountPeople('owner');
  pane.querySelector('#people-add').click();
  for (const c of pane.querySelectorAll('input:not([type="hidden"]), select')) {
    const labelled = c.getAttribute('aria-label')
      || (c.id && pane.querySelector(`label[for="${c.id}"]`))
      || c.closest('label');
    assert.ok(labelled, `unlabelled control: ${c.outerHTML.slice(0, 80)}`);
  }
});

// ------------------------------------------------------------------- wiring

test('index.html: People & Access is a household (admin-only) section after Account, with its pane', () => {
  assert.match(HTML, /class="settings-tab admin-only" id="stab-people"[^>]*aria-controls="spane-people"/);
  assert.match(HTML, /id="spane-people"[^>]*aria-labelledby="stab-people"/);
  assert.ok(HTML.indexOf('id="stab-account"') < HTML.indexOf('id="stab-people"'));
  assert.match(HTML, /data-i18n="settings\.sec_people"/);
});

test('main.js: admin tabs follow canAdmin()', () => {
  assert.match(MAIN, /const canAdmin = \(\) => isAdminRole\(state\.role\);/);
  // !canAdmin() hides every admin tab. OWNER_TABS is empty since Admins hold admin_system
  // (2026-10-19), and Voice left the list: a Member who holds use_voice gets it too.
  assert.match(MAIN, /b\.classList\.toggle\('hidden', !canAdmin\(\) \|\| \(OWNER_TABS\.includes\(tab\) && !isHostRole\(\)\)\)/);
  assert.match(MAIN, /const ADMIN_TABS = SECTIONS\.filter\(\(s\) => s\.perm === 'admin'\)\.map\(\(s\) => s\.id\);/);
  assert.match(MAIN, /case 'people':\s+await mountPeopleSettings/);
});

test('main.js: the sign-in gate does not toast, does not drop to Restricted, and does not start the app', () => {
  assert.match(MAIN, /if \(isError && \(gateBusy \|\| gateOverlayOpen\(\)\)\) return;/);
  assert.match(MAIN, /if \(gateActive\(\)\) \{ enterGate\(\); return; \}[^\n]*\n\s+await startApp\(\);/);
  assert.match(MAIN, /setOnSignInRequired\(\(\) => signInRequired\(\)\);/);
  // Escape / backdrop must not close a gate.
  assert.match(MAIN, /if \(lockCtx\.gate && !force\) return;/);
});

test('ws.js: a 1008 close is not retried in a loop', async () => {
  const src = read('js/ws.js');
  assert.match(src, /ev\.code === 1008/);
  assert.match(src, /this\.onSignInRequired\(\);\s+return;/);
});

test('sw.js: the three new modules are precached and the cache name moved on', () => {
  for (const f of ['profiles', 'signin', 'people']) assert.ok(SW.includes(`'/static/js/${f}.js'`), f);
  assert.match(SW, /const CACHE = 'local-chat-v183'/);
});

test('the retired terms are gone from the strings a user reads', () => {
  const flat = JSON.stringify(EN);
  assert.doesNotMatch(flat, /Safe[- ]Mode/i);
  assert.doesNotMatch(flat, /locked \(Safe/i);
  assert.deepEqual([EN.role.admin, EN.role.user, EN.role.basic], ['Admin', 'User', 'Basic']);
  for (const gone of ['owner', 'member', 'restricted']) assert.equal(EN.role[gone], undefined, `role.${gone}`);
  for (const ns of ['restricted', 'companions', 'drop']) assert.equal(EN[ns], undefined, `the ${ns} namespace left with its screen`);
});
