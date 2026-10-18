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

test('normalizeRole trusts the server, and falls back the way 2.x worked', () => {
  assert.equal(P.normalizeRole({ role: 'member' }), 'member');
  assert.equal(P.normalizeRole({ role: 'nonsense', decoy: true }), 'restricted');
  assert.equal(P.normalizeRole({ decoy: true }), 'restricted');
  assert.equal(P.normalizeRole({ pin_set: false }), 'owner');
  assert.equal(P.normalizeRole(null), 'owner');
});

test('admin surfaces are for owner and admin only; a Member is signed in but not an admin', () => {
  assert.equal(P.isAdminRole('owner'), true);
  assert.equal(P.isAdminRole('admin'), true);
  assert.equal(P.isAdminRole('member'), false);
  assert.equal(P.isAdminRole('restricted'), false);
  assert.equal(P.isAdminRole(undefined), false);
});

test('canManage mirrors the server: Owner manages all others, Admin only Member/Restricted', () => {
  assert.equal(P.canManage('owner', 'admin'), true);
  assert.equal(P.canManage('owner', 'owner'), false);
  assert.equal(P.canManage('admin', 'member'), true);
  assert.equal(P.canManage('admin', 'restricted'), true);
  assert.equal(P.canManage('admin', 'admin'), false);
  assert.equal(P.canManage('admin', 'owner'), false);
  assert.equal(P.canManage('member', 'restricted'), false);
  assert.equal(P.canManage('restricted', 'member'), false);
});

test('the Admin role is offered only to the Owner, and only once the Owner has a password', () => {
  assert.deepEqual(P.roleOptions('owner', true), { options: ['admin', 'member', 'restricted'], adminBlocked: null });
  assert.deepEqual(P.roleOptions('owner', false), { options: ['member', 'restricted'], adminBlocked: 'owner_needs_password' });
  assert.deepEqual(P.roleOptions('admin', true), { options: ['member', 'restricted'], adminBlocked: 'not_owner' });
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

test('the keypad stays when the only profile is the Owner and the install has a PIN', () => {
  const owner = (credential) => ({ id: 'o', name: 'Owner', role: 'owner', credential });
  const kid = { id: 'k', name: 'Kid', role: 'restricted', credential: 'none' };
  assert.equal(P.signInMode({ claimed: true, pin_set: true, profiles: [owner('pin_or_password')] }), 'keypad');
  assert.equal(P.signInMode({ claimed: true, pinSet: true, profiles: [owner('pin_or_password')] }), 'keypad', 'state.auth spelling');
  assert.equal(P.signInMode({ claimed: true, profiles: [owner('pin')] }), 'keypad');
  assert.equal(P.signInMode({ claimed: true, pin_set: false, profiles: [owner('pin_or_password')] }), 'tiles', 'a password-only Owner cannot use the keypad');
  assert.equal(P.signInMode({ claimed: true, pin_set: true, profiles: [owner('pin_or_password'), kid] }), 'tiles');
  assert.equal(P.signInMode({ claimed: true, profiles: [] }), 'keypad');
  assert.equal(P.signInMode({ claimed: false, profiles: [owner('pin'), owner('pin')] }), 'keypad');
  assert.equal(P.signInMode({ pin_set: true }), 'keypad');        // a 2.x server sends no profiles
});

test('tiles list the Owner first, then by role, then by name', () => {
  const names = P.sortTiles([
    { name: 'Zed', role: 'member' }, { name: 'Kid', role: 'restricted' },
    { name: 'Ann', role: 'member' }, { name: 'Boss', role: 'owner' }, { name: 'Ops', role: 'admin' },
  ]).map((p) => p.name);
  assert.deepEqual(names, ['Boss', 'Ops', 'Ann', 'Zed', 'Kid']);
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

test('createPayload: each role needs its own credential', () => {
  assert.deepEqual(P.createPayload({ name: ' Sam ', role: 'member', pin: '1234' }).body, { name: 'Sam', role: 'member', pin: '1234' });
  assert.equal(P.createPayload({ name: 'Sam', role: 'member', pin: '12' }).errors.pin, 'min_4');
  assert.equal(P.createPayload({ name: 'Sam', role: 'member' }).errors.pin, 'min_4');
  assert.deepEqual(P.createPayload({ name: 'Kid', role: 'restricted' }).errors, {});
  assert.equal(P.createPayload({ name: 'Kid', role: 'restricted', pin: '1' }).errors.pin, 'min_4');
  assert.equal(P.createPayload({ name: 'Ops', role: 'admin', password: 'short' }).errors.password, 'min_8');
  assert.deepEqual(P.createPayload({ name: 'Ops', role: 'admin', password: 'longenough' }).body, { name: 'Ops', role: 'admin', password: 'longenough' });
  assert.equal(P.createPayload({ name: '  ', role: 'member', pin: '1234' }).errors.name, 'required');
  const withBudgets = P.createPayload({ name: 'Kid', role: 'restricted', minutes: '30', sessionMinutes: '10' }).body;
  assert.deepEqual(withBudgets.budgets, { minutes_per_day: 30 });
  assert.equal(withBudgets.session_timeout, 600);
});

test('patchPayload sends only what changed', () => {
  const person = { id: 'p1', name: 'Kid', role: 'restricted', avatar: '', disabled: false, budgets: { minutes_per_day: 30 }, session_timeout: 600 };
  const same = P.budgetsToForm(person);
  assert.deepEqual(P.patchPayload(person, { ...same, name: 'Kid', role: 'restricted', avatar: '', disabled: false }).patch, {});
  const changed = P.patchPayload(person, { ...same, name: 'Kiddo', role: 'member', avatar: '🦊', minutes: '', disabled: true });
  assert.deepEqual(changed.patch, { name: 'Kiddo', role: 'member', avatar: '🦊', budgets: {}, disabled: true });
  assert.equal(P.patchPayload({ ...person, role: 'owner' }, { ...same, name: 'Kid', role: 'member' }).patch.role, undefined, 'the Owner\'s role is never patched');
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

test('usage lines: today against the limits, over-limit flagged', () => {
  const lines = P.usageLines(
    { active_seconds: 1800, 'action:turn': 100, 'action:thread': 1, upload_bytes: 5 * 1024 * 1024 },
    { minutes_per_day: 45, turns: 100, upload_mb: 50 });
  const by = Object.fromEntries(lines.map((l) => [l.key, l]));
  assert.deepEqual([by.minutes.used, by.minutes.limit, by.minutes.over], [30, 45, false]);
  assert.deepEqual([by.turns.used, by.turns.limit, by.turns.over], [100, 100, true]);
  assert.deepEqual([by.threads.used, by.threads.limit], [1, null]);
  assert.equal(by.upload_mb.used, 5);
  assert.equal(P.usageLines(null, null).every((l) => l.used === 0 && l.limit === null && !l.over), true);
});

// ------------------------------------------------------------ policy frames

test('ws error frames: policy refusals become the notice, the rest stay plain', () => {
  assert.deepEqual(P.wsErrorKind({ type: 'error', code: 'time_limit' }), { kind: 'notice', code: 'time_limit' });
  assert.deepEqual(P.wsErrorKind({ error: { code: 'quiet_hours' } }), { kind: 'notice', code: 'quiet_hours' });
  assert.equal(P.wsErrorKind({ code: 'safe_mode' }).kind, 'restricted');
  assert.equal(P.wsErrorKind({ code: 'permission_denied' }).kind, 'denied');
  assert.equal(P.wsErrorKind({ message: 'boom' }).kind, 'plain');
  assert.equal(P.wsErrorKind(null).kind, 'plain');
  assert.equal(P.blockNoticeKey('time_limit'), 'notice.time_limit');
  assert.equal(P.blockNoticeKey('permission_denied'), null);
});

test('only Members and Restricted profiles can be bound to a device', () => {
  const ids = P.bindableProfiles([
    { id: 'o', role: 'owner' }, { id: 'a', role: 'admin' }, { id: 'm', role: 'member' },
    { id: 'r', role: 'restricted' }, { id: 'x', role: 'member', disabled: true },
  ]).map((p) => p.id);
  assert.deepEqual(ids, ['m', 'r']);
});

test('a character\'s audience follows the two switches, then the server field', () => {
  assert.equal(P.audienceOf({ safe: true, hide_unlocked: true }), 'restricted');
  assert.equal(P.audienceOf({ safe: true, hide_unlocked: false }), 'all');
  assert.equal(P.audienceOf({ safe: false, hide_unlocked: false }), 'members');
  assert.equal(P.audienceOf({ safe: false, audience: 'all' }), 'members', 'a click before Save wins over the stale field');
  assert.equal(P.audienceOf({ audience: 'restricted' }), 'restricted');
  assert.equal(P.audienceOf({}), 'members');
});

// --------------------------------------------------------------------- api.js

const NAMES = ['me', 'login', 'switchProfile', 'setupOwner', 'changePassword', 'apiTokenInfo', 'rotateApiToken',
  'people', 'createPerson', 'patchPerson', 'deletePerson', 'setPersonPin', 'clearPersonPin', 'setPersonPassword',
  'signOutPerson', 'devices', 'bindThisDevice', 'forgetDevice', 'peopleActivity', 'peopleAudit', 'myActivity'];

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
    'POST /api/auth/login': () => [200, { ok: true, role: 'member' }],
    'POST /api/auth/switch': () => [200, { ok: true, role: 'admin' }],
    'POST /api/auth/setup': () => [200, { ok: true, claimed: true }],
    'POST /api/auth/password': () => [200, { ok: true }],
    'POST /api/auth/api-token': () => [200, { ok: true, api_token: 'k' }],
    'GET /api/people/activity?profile_id=p1&days=7': () => [200, { summary: { days: [] } }],
    'GET /api/people/activity': () => [200, { summary: { days: [] } }],
    'GET /api/me/activity?days=7': () => [200, {}],
    'PUT /api/people/devices/this': () => [200, {}],
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
  await api.myActivity(7);
  await api.bindThisDevice('kid');
  await api.bindThisDevice(null);
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
    ['GET /api/me/activity?days=7', null],
    ['PUT /api/people/devices/this', { profile_id: 'kid' }],
    ['PUT /api/people/devices/this', { profile_id: null }],
  ]);
});

test('every person route encodes its id as ONE path segment', { skip: dom.skip }, async () => {
  const { api } = await loadApi();
  apiRoutes = {};
  const evil = 'a/../b?x=1';
  const enc = encodeURIComponent(evil);
  for (const fn of [() => api.patchPerson(evil, {}), () => api.deletePerson(evil), () => api.setPersonPin(evil, '1234'),
    () => api.clearPersonPin(evil), () => api.setPersonPassword(evil, 'longenough'), () => api.signOutPerson(evil),
    () => api.forgetDevice(evil)]) {
    await fn().catch(() => {});
  }
  for (const c of apiCalls) assert.ok(c.key.includes(enc), c.key);
  assert.equal(apiCalls.length, 7);
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

test('time_limit / quiet_hours / signin_required reach their hooks; signin_required does not trip onLocked', { skip: dom.skip }, async () => {
  const mod = await loadApi();
  const seen = [];
  mod.setOnPolicyBlock((code) => seen.push(['block', code]));
  mod.setOnSignInRequired(() => seen.push(['signin']));
  mod.setOnLocked(() => seen.push(['locked']));
  apiRoutes = {
    'GET /api/bots': () => [403, { detail: 'No', error: { code: 'time_limit', message: 'x' } }],
    'GET /api/threads?bot_id=b': () => [401, { detail: 'Sign in', error: { code: 'signin_required', message: 'x' } }],
    'GET /api/files': () => [401, { detail: 'expired' }],
  };
  await assert.rejects(mod.api.bots());
  await assert.rejects(mod.api.threads('b'), (e) => e.signInRequired === true && e.code === 'signin_required');
  await assert.rejects(mod.api.files());
  assert.deepEqual(seen, [['block', 'time_limit'], ['signin'], ['locked']]);
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
  { id: 'o1', name: 'Boss', role: 'owner', avatar: '', credential: 'pin_or_password' },
  { id: 'a1', name: 'Ops', role: 'admin', avatar: '', credential: 'password' },
  { id: 'm1', name: 'Sam', role: 'member', avatar: '🦊', credential: 'pin' },
  { id: 'r1', name: 'Kid', role: 'restricted', avatar: '', credential: 'none' },
];

test('sign-in: one tile per profile, with name and role label', { skip: dom.skip }, async () => {
  const win = await boot();
  const { renderSignIn } = await import(`../static/js/signin.js?t=${Math.random()}`);
  const host = win.document.getElementById('lock-tiles');
  renderSignIn(host, { profiles: PROFILES, onSubmit: async () => {} });
  const tiles = [...host.querySelectorAll('.signin-tile')];
  assert.deepEqual(tiles.map((b) => b.querySelector('.signin-name').textContent), ['Boss', 'Ops', 'Sam', 'Kid']);
  assert.deepEqual(tiles.map((b) => b.querySelector('.signin-role').textContent), ['Owner', 'Admin', 'Member', 'Restricted']);
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
  assert.deepEqual(sent, [{ name: 'Owner', password: 'longenough', newPin: '4321', setupToken: 'tok123' }]);

  const local = renderSetup({ setupRequired: false, onSubmit: async () => {}, idPrefix: 'loc' });
  assert.equal(local.form.querySelector('#loc-token'), null, 'no token field on the box itself');
});

test('restricted banner: slim, non-dismissible, with the disclosure and a link to the panel', { skip: dom.skip }, async () => {
  const win = await boot();
  const { paintRestrictedBanner } = await import(`../static/js/signin.js?t=${Math.random()}`);
  const host = win.document.getElementById('restricted-banner');
  let opened = 0;
  paintRestrictedBanner(host, { show: true, onMore: () => { opened++; } });
  assert.equal(host.hidden, false);
  assert.equal(host.querySelector('.restricted-text').textContent,
    'The owner can see a summary of this profile\'s activity (how many chats and uploads, not what was said).');
  assert.equal(host.querySelectorAll('button').length, 1, 'only the link; there is no close button');
  host.querySelector('.restricted-more').click();
  assert.equal(opened, 1);
  paintRestrictedBanner(host, { show: false });
  assert.equal(host.hidden, true);
  assert.equal(host.childNodes.length, 0);
});

test('access notice: time_limit and quiet_hours each get their own words', { skip: dom.skip }, async () => {
  const win = await boot();
  const { paintAccessNotice } = await import(`../static/js/signin.js?t=${Math.random()}`);
  const host = win.document.getElementById('access-notice');
  paintAccessNotice(host, 'time_limit', { onRetry: () => {}, onSwitch: () => {} });
  assert.equal(host.querySelector('#access-title').textContent, 'Today\'s time is used up');
  assert.equal(host.querySelectorAll('button').length, 2);
  paintAccessNotice(host, 'quiet_hours', { onRetry: () => {}, onSwitch: null });
  assert.equal(host.querySelector('#access-title').textContent, 'Not available right now');
  assert.equal(host.querySelectorAll('button').length, 1, 'no Switch button when there is nobody to switch to');
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
    'PUT /api/people/devices/this': () => [200, {}],
    'POST /api/people': () => [201, { id: 'new' }],
    'PATCH /api/people/r1': () => [200, {}],
    'DELETE /api/people/m1': () => [200, { ok: true }],
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
  };
  const pane = win.document.getElementById('spane-people');
  await mod.mountPeopleSettings(pane, deps);
  await tick();
  return { win, pane, rec };
}

test('People tab: a row per profile with role badge and PIN / password / disabled indicators', { skip: dom.skip }, async () => {
  const { pane } = await mountPeople('owner');
  const rows = [...pane.querySelectorAll('.people-row')];
  assert.equal(rows.length, 4);
  const text = (r) => r.textContent.replace(/\s+/g, ' ');
  assert.match(text(rows[0]), /Boss.*Owner.*PIN set.*Password set/);
  assert.match(text(rows[2]), /Sam.*Member.*PIN set/);
  assert.doesNotMatch(text(rows[2]), /password/i, 'only the Owner and Admins have a password');
  assert.match(text(rows[3]), /Kid.*Restricted.*No PIN.*Disabled/);
  // The Owner row has no Edit button (nobody edits the Owner here); the others do.
  assert.equal(rows[0].querySelector('button'), null);
  assert.ok(rows[1].querySelector('button[aria-label="Edit Ops"]'));
});

test('People tab: an Admin manages Members and Restricted, not other Admins, and has no Machine access', { skip: dom.skip }, async () => {
  const { pane } = await mountPeople('admin');
  const editable = [...pane.querySelectorAll('.people-row')].filter((r) => r.querySelector('button')).map((r) => r.dataset.person);
  assert.deepEqual(editable, ['m1', 'r1']);
  assert.equal(pane.querySelector('#people-machine'), null);
  pane.querySelector('#people-add').click();
  const roles = [...pane.querySelectorAll('#people-editor select option')].map((o) => o.value);
  assert.deepEqual(roles, ['member', 'restricted'], 'only the Owner can add an Admin');
});

test('People tab: the Owner sees the Admin role only when they have a password', { skip: dom.skip }, async () => {
  const withPw = await mountPeople('owner');
  withPw.pane.querySelector('#people-add').click();
  assert.deepEqual([...withPw.pane.querySelectorAll('#people-editor select option')].map((o) => o.value), ['admin', 'member', 'restricted']);
  const p = PEOPLE(); p.owner_has_password = false;
  const noPw = await mountPeople('owner', { people: p });
  noPw.pane.querySelector('#people-add').click();
  assert.deepEqual([...noPw.pane.querySelectorAll('#people-editor select option')].map((o) => o.value), ['member', 'restricted']);
  assert.match(noPw.pane.querySelector('.people-admin-note').textContent, /set your own password/);
});

test('People tab: creating a Member posts the PIN and budgets from the form', { skip: dom.skip }, async () => {
  const { pane, win } = await mountPeople('owner');
  pane.querySelector('#people-add').click();
  const ed = pane.querySelector('#people-editor');
  const byLabel = (txt) => { const l = [...ed.querySelectorAll('label')].find((x) => x.textContent === txt); return ed.querySelector(`#${l.getAttribute('for')}`); };
  byLabel('Name').value = 'Pat';
  byLabel('PIN').value = '2468';
  byLabel('Minutes per day').value = '60';
  byLabel('From').value = '21:00';
  byLabel('Until').value = '07:30';
  apiCalls.length = 0;
  [...ed.querySelectorAll('button')].find((b) => b.textContent === 'Add person').click();
  await tick(); await tick();
  const post = apiCalls.find((c) => c.key === 'POST /api/people');
  assert.deepEqual(post.body, { name: 'Pat', role: 'member', pin: '2468', budgets: { minutes_per_day: 60, quiet_hours: '21:00-07:30' } });
  void win;
});

test('People tab: a bad budget is refused in the form, before any request', { skip: dom.skip }, async () => {
  const { pane } = await mountPeople('owner');
  pane.querySelector('#people-add').click();
  const ed = pane.querySelector('#people-editor');
  const byLabel = (txt) => { const l = [...ed.querySelectorAll('label')].find((x) => x.textContent === txt); return ed.querySelector(`#${l.getAttribute('for')}`); };
  byLabel('Name').value = 'Pat';
  byLabel('PIN').value = '2468';
  byLabel('Minutes per day').value = '5000';
  apiCalls.length = 0;
  [...ed.querySelectorAll('button')].find((b) => b.textContent === 'Add person').click();
  await tick();
  assert.equal(apiCalls.some((c) => c.key === 'POST /api/people'), false);
  assert.equal(ed.querySelector('.people-error').textContent, 'Minutes per day must be a whole number from 1 to 1440.');
});

test('People tab: editing sends only the change, and Delete asks first', { skip: dom.skip }, async () => {
  const { pane, rec } = await mountPeople('owner');
  pane.querySelector('[data-person="r1"] button').click();
  const ed = pane.querySelector('#people-editor');
  const field = (txt) => { const l = [...ed.querySelectorAll('label')].find((x) => x.textContent === txt); return ed.querySelector(`#${l.getAttribute('for')}`); };
  assert.equal(field('Minutes per day').value, '45');
  assert.equal(field('From').value, '20:30');
  assert.equal(field('Sign out after (minutes)').value, '15');
  assert.equal(ed.querySelector('.people-disabled-row input').checked, true);
  field('Minutes per day').value = '30';
  ed.querySelector('.people-disabled-row input').checked = false;
  apiCalls.length = 0;
  [...ed.querySelectorAll('button')].find((b) => b.textContent === 'Save').click();
  await tick(); await tick();
  const patch = apiCalls.find((c) => c.key === 'PATCH /api/people/r1');
  assert.deepEqual(patch.body, { budgets: { minutes_per_day: 30, quiet_hours: '20:30-07:00' }, disabled: false });

  pane.querySelector('[data-person="m1"] button').click();
  apiCalls.length = 0;
  [...pane.querySelectorAll('#people-editor button')].find((b) => b.textContent === 'Delete').click();
  await tick(); await tick();
  assert.equal(rec.confirms.length, 1);
  assert.match(rec.confirms[0], /Delete Sam\?/);
  assert.ok(apiCalls.some((c) => c.key === 'DELETE /api/people/m1'));
});

test('People tab: This device lists only Members and Restricted and binds through PUT /api/people/devices/this', { skip: dom.skip }, async () => {
  const p = PEOPLE(); p.profiles[3].disabled = false;     // a disabled profile cannot be bound
  const { pane } = await mountPeople('owner', { people: p });
  const select = pane.querySelector('#people-device-select');
  assert.deepEqual([...select.options].map((o) => o.textContent), ['Nobody (ask who\'s using it)', 'Sam (Member)', 'Kid (Restricted)']);
  assert.equal(select.value, 'r1', 'this browser is currently bound to Kid');
  apiCalls.length = 0;
  select.value = 'm1';
  select.dispatchEvent(new (pane.ownerDocument.defaultView.Event)('change'));
  await tick();
  assert.deepEqual(apiCalls[0], { key: 'PUT /api/people/devices/this', body: { profile_id: 'm1' } });
  const devs = [...pane.querySelectorAll('.people-device')];
  assert.equal(devs.length, 2);
  assert.match(devs[0].textContent, /Hall tablet \(this device\).*Opens as Kid/);
  assert.match(devs[0].textContent, /Unbind/);
  assert.match(devs[1].textContent, /Unnamed device.*Asks who is using it/);
  assert.doesNotMatch(devs[1].textContent, /Unbind/);
});

test('People tab: activity is a real table with a caption and counts only', { skip: dom.skip }, async () => {
  const { pane } = await mountPeople('owner');
  const table = pane.querySelector('.people-table');
  assert.ok(table.querySelector('caption'));
  assert.deepEqual([...table.querySelectorAll('thead th')].map((h) => h.textContent),
    ['Day', 'Sign-ins', 'Messages', 'New chats', 'Uploads', 'Feedback', 'Limit hits']);
  assert.ok([...table.querySelectorAll('th[scope="col"]')].length >= 7);
  assert.match(table.querySelector('tbody tr').textContent, /2026-10-17/);
  assert.match(pane.querySelector('.people-audit').textContent, /Profile created/);
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

test('index.html: the People tab is admin-only, before Security, with its pane', () => {
  assert.match(HTML, /class="settings-tab admin-only hidden" id="stab-people"[^>]*aria-controls="spane-people"/);
  assert.match(HTML, /id="spane-people"[^>]*aria-labelledby="stab-people"/);
  assert.ok(HTML.indexOf('id="stab-people"') < HTML.indexOf('id="stab-security"'));
  assert.match(HTML, /data-i18n="people\.tab"/);
});

test('main.js: admin tabs follow canAdmin(), not merely "not Restricted"', () => {
  assert.match(MAIN, /const canAdmin = \(\) => !state\.decoy && isAdminRole\(state\.role\);/);
  assert.match(MAIN, /b\.classList\.toggle\('hidden', !canAdmin\(\)\)/);
  assert.match(MAIN, /const ADMIN_TABS = \['reactions', 'health', 'ai', 'plugins', 'voice', 'people', 'agents'\];/);
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
  assert.match(SW, /const CACHE = 'local-chat-v168'/);
});

test('the retired terms are gone from the strings a user reads', () => {
  const flat = JSON.stringify(EN);
  assert.doesNotMatch(flat, /Safe[- ]Mode/i);
  assert.doesNotMatch(flat, /locked \(Safe/i);
  assert.equal(EN.role.restricted, 'Restricted');
  assert.equal(EN.reactions.safe_label, 'Restricted');
  assert.equal(EN.restricted.banner,
    'The owner can see a summary of this profile\'s activity (how many chats and uploads, not what was said).');
});
