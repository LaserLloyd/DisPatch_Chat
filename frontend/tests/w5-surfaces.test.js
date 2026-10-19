// DisPatch Chat 3.0 (W5) -- the frontend half of settings-from-schema, Connections, character
// details + starter packs, the first-run wizard and backups.
//
//   1. w5-api.js: the calls post the contract's bodies, failures carry error.code / fix / errors,
//      a 401 reaches the locked handler.
//   2. The pure decisions of each module (no DOM).
//   3. The DOM half against the real index.html and en.json, with a fake api injected.
//   4. Wiring: tabs, panes, service worker.

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
const SW = read('sw.js');
const EN = JSON.parse(read('locales/en.json'));

const require = createRequire(import.meta.url);
let jsdom = null;
try { jsdom = require('jsdom'); } catch { /* not installed - DOM tests skip */ }
const dom = { skip: domSkip(jsdom ? false : 'jsdom is not installed') };

const fresh = (f) => import(`../static/js/${f}?t=${Math.random()}`);

// ------------------------------------------------------------------ 1. w5-api.js

let calls = [];
let routes = {};
function installFetch(win) {
  calls = [];
  globalThis.fetch = async (u, opts = {}) => {
    const key = `${(opts.method || 'GET').toUpperCase()} ${u}`;
    calls.push({ key, body: opts.body ? JSON.parse(opts.body) : null });
    if (key === 'GET /static/locales/en.json') {
      return { ok: true, status: 200, json: async () => EN, headers: new win.Headers() };
    }
    const h = routes[key];
    const [status, body, headers] = h ? h(opts) : [404, { detail: 'Not Found' }];
    return {
      ok: status >= 200 && status < 300, status, statusText: String(status), headers: new win.Headers(headers || {}),
      json: async () => body, clone() { return this; },
      blob: async () => ({ size: 3 }),
    };
  };
}

async function boot() {
  const win = new jsdom.JSDOM(HTML, { url: 'http://127.0.0.1:18765/', runScripts: 'outside-only' }).window;
  const put = (k, v) => Object.defineProperty(globalThis, k, { value: v, configurable: true, writable: true });
  for (const [k, v] of Object.entries({
    window: win, document: win.document, location: win.location, navigator: win.navigator,
    localStorage: win.localStorage, sessionStorage: win.sessionStorage, Node: win.Node, HTMLElement: win.HTMLElement,
    HTMLInputElement: win.HTMLInputElement,
  })) put(k, v);
  installFetch(win);
  const i18n = await import('../static/js/i18n.js?v=4');
  await i18n.init('en');
  return win;
}

test('w5 calls post the contract bodies', { skip: dom.skip }, async () => {
  const win = await boot();
  routes = {
    'PATCH /api/settings': () => [200, { ok: true, changed: ['system.backup_keep'], restart_required: [] }],
    'POST /api/connections': () => [201, { id: 'x' }],
    'POST /api/connections/discover': () => [200, { found: [] }],
    'POST /api/starter-packs/animal_friends/install': () => [200, { installed: [] }],
    'POST /api/wizard/step/voice': () => [200, {}],
    'POST /api/backups/archive%3Aa.dpbk/restore': () => [200, { staged: true }],
    'PUT /api/plugins/extensions/voice': () => [200, {}],
  };
  const { w5 } = await fresh('w5-api.js');
  await w5.patchSettings({ 'system.backup_keep': 5 }, ['system.port']);
  await w5.createConnection({ type: 'ollama' });
  await w5.discoverConnections({ lan: true });
  await w5.installPack('animal_friends', { connection: 'c1' });
  await w5.wizardStep('voice', 'skipped');
  await w5.restoreBackup('archive:a.dpbk', 'pw');
  await w5.setExtensionEnabled('voice', true, ['http_routes']);
  const by = Object.fromEntries(calls.map((c) => [c.key, c.body]));
  assert.deepEqual(by['PATCH /api/settings'], { set: { 'system.backup_keep': 5 }, reset: ['system.port'] });
  assert.deepEqual(by['POST /api/connections'], { type: 'ollama' });
  assert.deepEqual(by['POST /api/connections/discover'], { lan: true });
  assert.deepEqual(by['POST /api/starter-packs/animal_friends/install'], { connection: 'c1' });
  assert.deepEqual(by['POST /api/wizard/step/voice'], { status: 'skipped' });
  assert.deepEqual(by['POST /api/backups/archive%3Aa.dpbk/restore'], { confirm: true, passphrase: 'pw' });
  assert.deepEqual(by['PUT /api/plugins/extensions/voice'], { enabled: true, approve: ['http_routes'] });
  void win;
});

test('a failed call carries the envelope, the per-key errors and the permission list', { skip: dom.skip }, async () => {
  await boot();
  routes = {
    'PATCH /api/settings': () => [422, { detail: 'Some settings were not accepted; nothing was changed',
      error: { code: 'settings_invalid', message: 'x', fix: 'Fix them', docs: 'settings_invalid' },
      errors: { 'system.backup_keep': 'Snapshots to keep: must be at least 1' } }],
    'PUT /api/plugins/extensions/voice': () => [409, { detail: 'needs approval', error: { code: 'plugin_needs_approval', message: 'm' },
      requested: ['http_routes'], permissions: [{ id: 'http_routes', label: 'Add its own web API' }] }],
  };
  const { w5, errorText } = await fresh('w5-api.js');
  const e1 = await w5.patchSettings({ 'system.backup_keep': 0 }).then(() => null, (e) => e);
  assert.equal(e1.status, 422); assert.equal(e1.code, 'settings_invalid');
  assert.deepEqual(e1.errors, { 'system.backup_keep': 'Snapshots to keep: must be at least 1' });
  assert.match(errorText(e1), /Snapshots to keep: must be at least 1/);
  const e2 = await w5.setExtensionEnabled('voice', true).then(() => null, (e) => e);
  assert.equal(e2.code, 'plugin_needs_approval'); assert.deepEqual(e2.requested, ['http_routes']);
  assert.equal(e2.permissions[0].label, 'Add its own web API');
});

test('a 401 reaches the locked handler, except on /api/auth', { skip: dom.skip }, async () => {
  await boot();
  routes = { 'GET /api/connections': () => [401, { detail: 'Unlock' }], 'POST /api/auth/setup': () => [401, { detail: 'no' }] };
  const { w5, setOnLocked } = await fresh('w5-api.js');
  let n = 0;
  setOnLocked(() => { n += 1; });
  await assert.rejects(w5.connections());
  await assert.rejects(w5.setup({}));
  assert.equal(n, 1);
});

// ------------------------------------------------------------------ 2. pure decisions

const SCHEMA = {
  schema: 1,
  groups: [{ id: 'system', label: 'System', description: 's' }, { id: 'people', label: 'People & Profiles', description: 'p' }],
  settings: [
    { key: 'system.backup_keep', type: 'integer', default: 12, label: 'Snapshots to keep', description: 'How many snapshots.', group: 'system', section: 'Backups', level: 'basic', restart_required: true, min: 1 },
    { key: 'system.host', type: 'string', default: '127.0.0.1', label: 'Listen address', description: 'Informational.', group: 'system', section: 'Network', level: 'advanced' },
    { key: 'people.lock_timeout_seconds', type: 'integer', default: 600, label: 'Auto-lock', description: 'Idle seconds.', group: 'people', section: 'Sessions', level: 'basic' },
    { key: 'system.backup_passphrase', type: 'secret', default: null, label: 'Backup passphrase', description: 'Write only.', group: 'system', section: 'Backups', level: 'advanced', secret: true },
  ],
};
const VALUES = { schema: 1, values: {
  'system.backup_keep': { value: 20, source: 'store', default: 12, is_default: false, restart_pending: true },
  'system.host': { value: '0.0.0.0', source: 'env', env: 'DISPATCH_HOST', default: '127.0.0.1', is_default: false },
  'people.lock_timeout_seconds': { value: 600, source: 'default', default: 600, is_default: true },
  'system.backup_passphrase': { value: null, source: 'store', secret: true, set: true },
}, restart_required: ['system.backup_keep'], unknown: [] };

test('settings model: groups in server order, sections, source and restart flags', async () => {
  const S = await fresh('settings_schema.js').catch(() => null);
  if (!S) return;                                   // needs i18n/util in a DOM-less run: covered below with a DOM
  const model = S.buildModel(SCHEMA, VALUES);
  assert.deepEqual(model.map((g) => g.id), ['system', 'people']);
  const sys = model[0];
  assert.deepEqual(sys.sections.map((s) => s.name), ['Backups', 'Network']);
  const keep = sys.sections[0].rows[0];
  assert.equal(keep.value, 20); assert.equal(keep.restartPending, true); assert.equal(keep.isDefault, false);
  const host = sys.sections[1].rows[0];
  assert.equal(host.source, 'env'); assert.equal(host.env, 'DISPATCH_HOST');
  const secret = sys.sections[0].rows[1];
  assert.equal(secret.secretSet, true);
});

test('settings filter: Basic hides advanced rows; search matches key, label and text', async () => {
  const S = await fresh('settings_schema.js').catch(() => null);
  if (!S) return;
  const rows = S.buildModel(SCHEMA, VALUES).flatMap((g) => g.sections.flatMap((s) => s.rows));
  const basic = rows.filter((r) => S.matches(r, { advanced: false })).map((r) => r.def.key);
  assert.deepEqual(basic, ['system.backup_keep', 'people.lock_timeout_seconds']);
  assert.equal(rows.filter((r) => S.matches(r, { advanced: true })).length, 4);
  assert.deepEqual(rows.filter((r) => S.matches(r, { q: 'snapshots', advanced: true })).map((r) => r.def.key), ['system.backup_keep']);
  assert.deepEqual(rows.filter((r) => S.matches(r, { q: 'idle auto', advanced: true })).map((r) => r.def.key), ['people.lock_timeout_seconds']);
  assert.equal(rows.filter((r) => S.matches(r, { q: 'zzz', advanced: true })).length, 0);
});

test('settings patch: only real changes; a reset beats nothing and loses to an edit', async () => {
  const S = await fresh('settings_schema.js').catch(() => null);
  if (!S) return;
  const model = S.buildModel(SCHEMA, VALUES);
  const edits = new Map([['system.backup_keep', 20], ['people.lock_timeout_seconds', 900], ['system.backup_passphrase', 'new-pass']]);
  const resets = new Set(['system.backup_keep', 'system.host', 'people.lock_timeout_seconds']);
  const { set, reset } = S.buildPatch(edits, resets, model);
  assert.deepEqual(set, { 'people.lock_timeout_seconds': 900, 'system.backup_passphrase': 'new-pass' });   // 20 == stored: dropped
  assert.deepEqual(reset, ['system.backup_keep', 'system.host']);          // reset of an edited key is just the edit
  assert.deepEqual(S.buildPatch(new Map(), new Set(['people.lock_timeout_seconds']), model).reset, []);     // already default
});

test('parseInput types what was typed', async () => {
  const S = await fresh('settings_schema.js').catch(() => null);
  if (!S) return;
  assert.equal(S.parseInput({ type: 'integer' }, ' 42 '), 42);
  assert.equal(S.parseInput({ type: 'integer' }, '4.5'), undefined);
  assert.equal(S.parseInput({ type: 'number' }, '4.5'), 4.5);
  assert.equal(S.parseInput({ type: 'number' }, ''), undefined);
  assert.equal(S.parseInput({ type: 'boolean' }, '', true), true);
  assert.equal(S.parseInput({ type: 'string' }, 'x y'), 'x y');
});

test('device defaults apply only where the device has no choice of its own', async () => {
  const S = await fresh('settings_schema.js').catch(() => null);
  if (!S) return;
  const d = { theme: 'forest', language: 'de', avatar_style: 'minimal' };
  assert.deepEqual(S.deviceDefaultsPlan(d, {}).map((p) => p[0]), ['theme', 'language', 'avatar_style']);
  assert.deepEqual(S.deviceDefaultsPlan(d, { theme: 'glacier', language: 'en' }).map((p) => p[0]), ['avatar_style']);
});

test('connection test results become plain sentences', async () => {
  const C = await fresh('connections.js').catch(() => null);
  if (!C) return;
  assert.equal(C.describeTest(null)[0], 'idle');
  const ok = C.describeTest({ ok: true, models: ['a', 'b'], models_listed: true, latency_ms: 40, product: 'StudioForge', version: '2.0.1' });
  assert.equal(ok[0], 'ok'); assert.match(ok[2], /40/); assert.match(ok[2], /StudioForge 2\.0\.1/);
  assert.equal(C.describeTest({ ok: false, problem: 'auth', fix: 'Check the key' })[0], 'bad');
  assert.equal(C.describeTest({ ok: false, problem: 'auth', fix: 'Check the key' })[2], 'Check the key');
  assert.equal(C.describeTest({ ok: false, problem: 'no_model_list' })[0], 'warn');
  assert.equal(C.describeTest({ ok: false, problem: 'blocked_target', message: 'm' })[0], 'bad');
});

test('connection form payload: a blank key keeps the stored one; clear and env are explicit', async () => {
  const C = await fresh('connections.js').catch(() => null);
  if (!C) return;
  const base = { type: 'openai', name: ' Mine ', base_url: ' https://api.openai.com/v1 ', api_key: '', api_key_env: '', default_model: 'gpt-4o', capabilities: ['chat'] };
  assert.deepEqual(C.formPayload(base), { type: 'openai', name: 'Mine', base_url: 'https://api.openai.com/v1', default_model: 'gpt-4o', capabilities: ['chat'] });
  assert.equal(C.formPayload({ ...base, api_key: ' sk-1 ' }, { editing: true }).api_key, 'sk-1');
  assert.equal(C.formPayload({ ...base, api_key_env: 'OPENAI_API_KEY' }).api_key_env, 'OPENAI_API_KEY');
  const edit = C.formPayload({ ...base, clearKey: true }, { editing: true });
  assert.equal(edit.clear_key, true); assert.equal('type' in edit, false);
  assert.equal('clear_key' in C.formPayload({ ...base, clearKey: true }), false);          // never on create
  assert.deepEqual(C.formDefaults({ id: 'ollama', label: 'Ollama', base_url: 'http://127.0.0.1:11434/v1', key: 'none', capabilities: ['chat'] }).needsKey, false);
});

test('character diff: only what changed; connection and model travel together', async () => {
  const K = await fresh('characters.js').catch(() => null);
  if (!K) return;
  const orig = { audience: 'members', description: '', profiles: ['a'], backend: { connection: 'c1', model: 'm1' }, personality: { preset: 'default' }, backend_kind: 'direct' };
  assert.deepEqual(K.diffCharacter(orig, { audience: 'members', description: '', profiles: ['a'], connection: 'c1', model: 'm1', personality: 'default' }), {});
  assert.deepEqual(K.diffCharacter(orig, { audience: 'restricted', description: 'Hi', profiles: [], connection: 'c2', model: 'x', personality: 'tutor' }),
    { description: 'Hi', audience: 'restricted', profiles: [], connection: 'c2', model: 'x', personality: 'tutor' });
  assert.deepEqual(K.diffCharacter(orig, { connection: '', model: '' }), { connection: null });
  assert.deepEqual(K.diffCharacter(orig, { connection: 'c1', model: 'm2' }), { model: 'm2' });
  assert.deepEqual(K.diffCharacter(orig, { personality: 'custom' }), {});
  assert.equal(K.takesConnection({ backend_kind: 'agent' }), false);
  assert.equal(K.takesConnection(orig), true);
  assert.equal(K.voiceHash('otto'), '#voice/voices/otto');
});

test('wizard: where it starts, the next step, and the bodies it sends', async () => {
  const W = await fresh('wizard.js').catch(() => null);
  if (!W) return;
  assert.equal(W.startStep(null), 'owner');
  assert.equal(W.startStep({ claimed: false }), 'owner');
  assert.equal(W.startStep({ claimed: true, steps: [{ id: 'connection', done: false }] }), 'connection');
  assert.equal(W.startStep({ claimed: true, steps: [{ id: 'connection', done: true }, { id: 'network', done: false }] }), 'voice');
  assert.equal(W.startStep({ claimed: true, steps: [{ id: 'connection', done: true }, { id: 'network', done: true }] }), 'done');
  assert.equal(W.nextStep('owner', {}), 'connection');
  assert.equal(W.nextStep('network', {}), 'done');
  assert.equal(W.nextStep('connection', { steps: [{ id: 'voice', done: true }, { id: 'family', done: false }] }, { skipDone: true }), 'family');
  assert.deepEqual(W.ownerPayload({ name: ' Olive ', password: 'pw', pin: '', token: ' t ' }), { name: 'Olive', password: 'pw', setup_token: 't' });
  assert.equal(W.ownerError({ password: '', pin: '' }), 'wiz.owner_need_one');
  assert.equal(W.ownerError({ password: 'short', pin: '', confirm: 'short' }), 'wiz.owner_pw_short');
  assert.equal(W.ownerError({ password: 'long-enough', pin: '', confirm: 'other' }), 'wiz.owner_pw_mismatch');
  assert.equal(W.ownerError({ password: '', pin: '12', confirm: '' }), 'wiz.owner_pin_short');
  assert.equal(W.ownerError({ password: 'long-enough', pin: '1234', confirm: 'long-enough' }), '');
  assert.deepEqual(W.familyPayload([
    { role: 'member', name: ' Max ', pin: '5555' }, { role: 'restricted', name: 'Mia', pin: '9999' }, { role: 'member', name: '', pin: '' }],
  { animals: true, assistants: false }), {
    people: [{ name: 'Max', role: 'member', pin: '5555' }, { name: 'Mia', role: 'restricted' }],
    install_animal_friends: true, install_assistants: false });
});

test('backup: what each row offers, and the schedule sentence', async () => {
  const B = await fresh('backup.js').catch(() => null);
  if (!B) return;
  assert.deepEqual(B.actionsFor({ kind: 'archive', restorable: true }), { verify: true, restore: true, remove: true, needsPassphrase: true });
  assert.deepEqual(B.actionsFor({ kind: 'premigration', restorable: true }), { verify: false, restore: true, remove: false, needsPassphrase: false });
  assert.deepEqual(B.actionsFor({ kind: 'snapshot' }), { verify: false, restore: true, remove: true, needsPassphrase: false });
});

// ------------------------------------------------------------------ 3. DOM

const tick = () => new Promise((r) => setTimeout(r, 0));

test('All settings: renders ForgeUI rows from the schema, locks env rows, saves only changes', { skip: dom.skip }, async () => {
  const win = await boot();
  const { mountSettingsSchema } = await fresh('settings_schema.js');
  const patched = [];
  const api = {
    settingsSchema: async () => SCHEMA,
    settings: async () => VALUES,
    patchSettings: async (set, reset) => { patched.push({ set, reset }); return { ok: true, changed: Object.keys(set), restart_required: ['system.backup_keep'] }; },
    backups: async () => ({ backups: [], schedule: { enabled: false } }),
    pendingRestore: async () => ({ pending: false }),
    people: async () => ({ profiles: [] }),
  };
  const pane = win.document.getElementById('spane-settings');
  const toasts = [];
  const ctl = await mountSettingsSchema(pane, { api, toast: (m) => toasts.push(m), role: 'owner', group: 'system' });
  const rows = [...pane.querySelectorAll('.ui-setting')];
  assert.ok(rows.length >= 1);
  const keep = pane.querySelector('[data-key="system.backup_keep"]');
  assert.ok(keep, 'the Basic row is drawn');
  assert.equal(pane.querySelector('[data-key="system.host"]'), null, 'advanced rows start hidden');
  assert.ok(pane.querySelector('.schema-tab[aria-selected="true"]'));
  // Advanced shows the env-locked row with a disabled control
  [...pane.querySelectorAll('button')].find((b) => b.textContent === EN.schema.show_advanced).click();
  const host = pane.querySelector('[data-key="system.host"]');
  assert.equal(host.getAttribute('data-source'), 'env');
  assert.equal(host.querySelector('input').disabled, true);
  // edit one, save: only that key is sent
  const input = pane.querySelector('[data-key="system.backup_keep"] input');
  input.value = '30';
  input.dispatchEvent(new win.Event('input', { bubbles: true }));
  const save = [...pane.querySelectorAll('.schema-savebar button')].find((b) => /Save 1 change/.test(b.textContent));
  assert.ok(save && !save.disabled);
  save.click();
  await tick(); await tick();
  assert.deepEqual(patched, [{ set: { 'system.backup_keep': 30 }, reset: [] }]);
  assert.match(pane.querySelector('.schema-savemsg').textContent, /needs a restart/);
  void ctl;
});

test('All settings: a refused value is shown on its row and nothing else changes', { skip: dom.skip }, async () => {
  const win = await boot();
  const { mountSettingsSchema } = await fresh('settings_schema.js');
  const api = {
    settingsSchema: async () => SCHEMA, settings: async () => VALUES,
    patchSettings: async () => { const e = new Error('422: x'); e.errors = { 'system.backup_keep': 'Snapshots to keep: must be at least 1' }; throw e; },
    backups: async () => ({ backups: [], schedule: {} }), pendingRestore: async () => ({ pending: false }), people: async () => ({ profiles: [] }),
  };
  const pane = win.document.getElementById('spane-settings');
  await mountSettingsSchema(pane, { api, toast() {}, role: 'owner', group: 'system' });
  const input = pane.querySelector('[data-key="system.backup_keep"] input');
  input.value = '0';
  input.dispatchEvent(new win.Event('input', { bubbles: true }));
  [...pane.querySelectorAll('.schema-savebar button')].find((b) => /Save/.test(b.textContent) && !b.disabled).click();
  await tick(); await tick();
  const err = pane.querySelector('[data-key="system.backup_keep"] .schema-error');
  assert.equal(err.hidden, false);
  assert.match(err.textContent, /must be at least 1/);
  assert.equal(pane.querySelector('[data-key="system.backup_keep"] input').getAttribute('aria-invalid'), 'true');
});

const CONN_TYPES = [
  { id: 'openai_compatible', label: 'OpenAI-compatible server', base_url: '', key: 'optional', editable_base_url: true, capabilities: ['chat'] },
  { id: 'lmstudio', label: 'LM Studio', base_url: 'http://127.0.0.1:1234/v1', key: 'none', capabilities: ['chat', 'tools'] },
  { id: 'openai', label: 'OpenAI', base_url: 'https://api.openai.com/v1', key: 'required', key_env: 'OPENAI_API_KEY', capabilities: ['chat'] },
];

test('Connections: cards, Test result, add form with a Test before Save', { skip: dom.skip }, async () => {
  const win = await boot();
  const { mountConnections } = await fresh('connections.js');
  const created = [];
  const state = [{ id: 'lmstudio', name: 'LM Studio', type: 'lmstudio', type_label: 'LM Studio', base_url: 'http://127.0.0.1:1234/v1',
    credential: { kind: 'none', has_key: false }, capabilities: ['chat'], default_model: '', models: [], last_test: {}, in_use_by: ['nova'] }];
  const api = {
    connections: async () => ({ connections: state, types: CONN_TYPES, default: 'lmstudio' }),
    testConnection: async (id) => ({ ok: true, models: ['m1'], models_listed: true, latency_ms: 12, connection: { ...state[0], last_test: { ok: true } } }),
    testConnectionForm: async (b) => ({ ok: true, models: ['gpt-4o'], models_listed: true, latency_ms: 80, sent: b }),
    createConnection: async (b) => { created.push(b); state.push({ ...state[0], id: 'openai', name: b.name, type: 'openai', in_use_by: [] }); return { id: 'openai' }; },
    discoverConnections: async () => ({ found: [{ type: 'ollama', product: 'Ollama', base_url: 'http://127.0.0.1:11434/v1', models: ['llama3'], already_added: null }] }),
    adoptConnections: async () => ({ linked: 0 }),
  };
  const pane = win.document.getElementById('spane-connections');
  await mountConnections(pane, { api, toast() {} });
  const card = pane.querySelector('[data-connection="lmstudio"]');
  assert.ok(card);
  assert.match(card.textContent, /Used by nova/);
  assert.match(card.textContent, /Default/);
  [...card.querySelectorAll('button')].find((b) => b.textContent === 'Test').click();
  await tick(); await tick();
  assert.match(card.querySelector('.cxn-result').textContent, /1 models available|12 ms/);
  // add form
  [...pane.querySelectorAll('.cxn-bar button')].find((b) => b.textContent === EN.cxn.add).click();
  const form = pane.querySelector('.cxn-form');
  assert.ok(form);
  const type = form.querySelector('select');
  type.value = 'openai';
  type.dispatchEvent(new win.Event('change', { bubbles: true }));
  const url = [...form.querySelectorAll('input[type=url]')][0];
  assert.equal(url.value, 'https://api.openai.com/v1');
  const key = form.querySelector('input[type=password]');
  key.value = 'sk-test';
  [...form.querySelectorAll('button')].find((b) => b.textContent === 'Test').click();
  await tick(); await tick();
  assert.match(form.querySelector('.cxn-result').textContent, /models available/);
  [...form.querySelectorAll('button')].find((b) => b.textContent === EN.cxn.add_save).click();
  await tick(); await tick();
  assert.equal(created.length, 1);
  assert.equal(created[0].type, 'openai'); assert.equal(created[0].api_key, 'sk-test');
  assert.ok(pane.querySelector('[data-connection="openai"]'), 'the list reloads after Save');
  // discovery
  [...pane.querySelectorAll('.cxn-bar button')].find((b) => b.textContent === EN.cxn.find).click();
  await tick(); await tick();
  assert.match(pane.querySelector('.cxn-found').textContent, /Ollama/);
});

test('Characters: the Details panel loads, shows only what applies, and saves a diff', { skip: dom.skip }, async () => {
  const win = await boot();
  const K = await fresh('characters.js');
  const patched = [];
  const api = {
    allCharacters: async () => ({ characters: [{ id: 'nova', name: 'Nova', audience: 'members', description: '', profiles: [],
      backend: { kind: 'direct', connection: 'c1', model: 'm1' }, backend_kind: 'direct', personality: { preset: 'default' } }] }),
    connections: async () => ({ connections: [{ id: 'c1', name: 'LM Studio', type_label: 'LM Studio', models: ['m1', 'm2'], default_model: 'm1' }] }),
    personalities: async () => ({ presets: [{ id: 'default', title: 'Default', description: '' }, { id: 'tutor', title: 'Tutor', description: '' }] }),
    people: async () => ({ profiles: [{ id: 'p1', name: 'Mia', role: 'restricted' }, { id: 'p2', name: 'Boss', role: 'owner' }] }),
    patchCharacter: async (id, body) => { patched.push([id, body]); return {}; },
  };
  const panel = await K.characterPanel({ id: 'nova', name: 'Nova' }, { api, toast() {}, openTab() {}, reload: async () => {} });
  win.document.body.append(panel);
  const labels = [...panel.querySelectorAll('label')].map((l) => l.textContent);
  assert.ok(labels.includes(EN.chars.connection) && labels.includes(EN.chars.model) && labels.includes(EN.chars.personality));
  assert.match(panel.textContent, /Mia/, 'Restricted profile listed for visibility');
  assert.doesNotMatch(panel.textContent, /Boss/, 'the owner always sees everything: not offered');
  const sel = [...panel.querySelectorAll('select')];
  sel.find((s) => [...s.options].some((o) => o.value === 'restricted')).value = 'restricted';
  panel.querySelector('input[type=checkbox][value="p1"]').checked = true;
  [...panel.querySelectorAll('button')].find((b) => b.textContent === EN.chars.save).click();
  await tick(); await tick();
  assert.deepEqual(patched, [['nova', { audience: 'restricted', profiles: ['p1'] }]]);
});

test('Wizard: owner step validates before it calls the server', { skip: dom.skip }, async () => {
  const win = await boot();
  const W = await fresh('wizard.js');
  const seen = [];
  const api = {
    wizard: async () => ({ needed: true, claimed: false, steps: [] }),
    setup: async (b) => { seen.push(b); return { ok: true }; },
    wizardDetect: async () => ({ found: [] }),
  };
  const ctl = await W.maybeStartWizard({ api, toast() {} });
  assert.ok(ctl, 'a fresh install gets the wizard');
  const dlg = win.document.querySelector('.wizard');
  assert.equal(dlg.getAttribute('aria-modal'), 'true');
  assert.match(dlg.querySelector('.wizard-steps').textContent, /Your account/);
  [...dlg.querySelectorAll('button')].find((b) => b.textContent === EN.wiz.owner_create).click();
  await tick();
  assert.match(dlg.querySelector('.lock-error').textContent, /password or a PIN/);
  assert.equal(seen.length, 0);
  const pw = dlg.querySelectorAll('input[type=password]');
  pw[0].value = 'long-enough-pw'; pw[1].value = 'long-enough-pw';
  [...dlg.querySelectorAll('button')].find((b) => b.textContent === EN.wiz.owner_create).click();
  await tick(); await tick();
  assert.equal(seen.length, 1);
  assert.deepEqual(seen[0], { name: 'Owner', password: 'long-enough-pw' });  // scrub-ok: test fixture, not a real credential
  // "Set up later" dismisses for the session and the wizard is not offered again
  [...dlg.querySelectorAll('button')].find((b) => b.textContent === EN.wiz.later).click();
  assert.equal(win.document.querySelector('.wizard'), null);
  assert.equal(await W.maybeStartWizard({ api, toast() {} }), null);
});

test('Wizard: nothing opens when the install is already set up', { skip: dom.skip }, async () => {
  await boot();
  const W = await fresh('wizard.js');
  assert.equal(await W.maybeStartWizard({ api: { wizard: async () => ({ needed: false, claimed: true }) } }), null);
  assert.equal(await W.maybeStartWizard({ api: { wizard: async () => { throw new Error('boom'); } } }), null);
});

test('Backups: a recovery key is shown once; restore asks first and stages', { skip: dom.skip }, async () => {
  const win = await boot();
  const { mountBackups } = await fresh('backup.js');
  const asked = [];
  let listing = [{ id: 'archive:a.dpbk', kind: 'archive', name: 'a.dpbk', size: 2048, created: '2026-10-18T10:00:00+00:00', encrypted: true, restorable: true },
    { id: 'premigration:3.0.0-x', kind: 'premigration', name: '3.0.0-x', size: 10, created: '2026-10-17T10:00:00+00:00', encrypted: false, restorable: true }];
  const staged = [];
  const api = {
    backups: async () => ({ backups: listing, schedule: { enabled: true, interval_hours: 24, keep: 7, last_archive_age_hours: 3 } }),
    pendingRestore: async () => ({ pending: false }),
    createBackup: async () => { listing = [{ ...listing[0], id: 'archive:b.dpbk', name: 'b.dpbk' }, ...listing]; return { backup: listing[0], recovery_key: 'ABCDE-FGHJK-MNPQR-STUVW-XYZ23' }; },
    restoreBackup: async (id, pw) => { staged.push([id, pw]); return { staged: true, message: 'Staged' }; },
    verifyBackup: async () => ({ ok: true, files: 5 }),
    deleteBackup: async () => ({}), people: async () => ({ profiles: [] }),
  };
  const host = win.document.createElement('div');
  win.document.body.append(host);
  mountBackups(host, { api, toast() {}, confirm: async (m, o) => { asked.push([m, o]); return true; }, role: 'owner' });
  await tick(); await tick();
  assert.match(host.textContent, /every 24 hours/);
  const rows = [...host.querySelectorAll('.bkp-row')];
  assert.equal(rows.length, 2);
  assert.equal(rows[1].textContent.includes(EN.bkp.delete), false, 'a pre-upgrade copy cannot be deleted from here');
  // back up now -> the key panel
  [...host.querySelectorAll('button')].find((b) => b.textContent === EN.bkp.now).click();
  await tick(); await tick(); await tick();
  const key = host.querySelector('.bkp-key-input');
  assert.equal(key.value, 'ABCDE-FGHJK-MNPQR-STUVW-XYZ23');
  [...host.querySelectorAll('button')].find((b) => b.textContent === EN.bkp.key_saved).click();
  assert.equal(host.querySelector('.bkp-key').hidden, true);
  assert.equal(host.querySelector('.bkp-key').textContent, '');
  // restore asks first
  host.querySelector('input[type=password]').value = 'my-passphrase';
  [...host.querySelectorAll('.bkp-row')][0].querySelectorAll('button').forEach((b) => { if (b.textContent === EN.bkp.restore) b.click(); });
  await tick(); await tick();
  assert.equal(asked.length, 1); assert.equal(asked[0][1].danger, true);
  assert.deepEqual(staged, [['archive:b.dpbk', 'my-passphrase']]);
});

test('Backups: the Owner and Admins are offered them, a Member is not', { skip: dom.skip }, async () => {
  const win = await boot();
  const { mountBackups } = await fresh('backup.js');
  const host = win.document.createElement('div');
  mountBackups(host, { api: {}, role: 'member' });
  assert.match(host.textContent, /managed by the Owner and Admins/);
  const admin = win.document.createElement('div');
  mountBackups(admin, { api: { backups: async () => ({ backups: [], schedule: { enabled: false } }), pendingRestore: async () => ({ pending: false }), people: async () => ({ profiles: [] }) }, role: 'admin' });
  assert.doesNotMatch(admin.textContent, /managed by the Owner and Admins/);
});

// ------------------------------------------------------------------ 4. wiring

test('index.html: the two new tabs and panes exist, hidden for non-admins like the others', () => {
  assert.match(HTML, /id="stab-connections"[^>]*aria-controls="spane-connections"/);
  assert.match(HTML, /id="stab-settings"[^>]*aria-controls="spane-settings"/);
  assert.match(HTML, /class="settings-tab admin-only hidden" id="stab-connections"/);
  assert.match(HTML, /class="settings-tab admin-only hidden" id="stab-settings"/);
  assert.match(HTML, /<link rel="stylesheet" href="\/static\/w5\.css\?v=\d+"/);
});

test('main.js: hooks the new modules, registers the locked handler and starts the wizard after boot', () => {
  for (const m of ['w5-api', 'connections', 'settings_schema', 'characters', 'wizard']) {
    assert.match(MAIN, new RegExp(`from './${m}\\.js\\?v=\\d+'`), m);
    assert.ok(SW.includes(`'/static/js/${m}.js'`), `${m} precached`);
  }
  assert.ok(SW.includes("'/static/w5.css'"));
  assert.match(MAIN, /w5SetOnLocked\(\(\) => handleLocked\(\)\);/);
  assert.match(MAIN, /await refreshAuthAndBoot\(\);\s+[\s\S]{0,400}applyDeviceDefaults\(\);\s+(?:const wizard = await )?maybeStartWizard\(/);
  assert.match(MAIN, /decorateCharacterRows\(list, bmBots, characterCtx\(\)\)/);
  assert.match(MAIN, /case 'connections':[\s\S]{0,200}mountConnections\(pane/);
  assert.match(MAIN, /case 'settings':[\s\S]{0,200}mountSettingsSchema\(pane/);
});

test('the new strings exist in every locale with the same keys (English in en, [xx] elsewhere)', () => {
  for (const lang of ['ar', 'de', 'es', 'fr', 'ja', 'pt', 'zh']) {
    const L = JSON.parse(read(`locales/${lang}.json`));
    for (const ns of ['schema', 'cxn', 'chars', 'wiz', 'bkp']) {
      assert.deepEqual(Object.keys(L[ns]).sort(), Object.keys(EN[ns]).sort(), `${lang}.${ns}`);
    }
    assert.equal(L.settings.tab_connections, `[${lang}] ${EN.settings.tab_connections}`);
  }
});

test('All settings: a host-control row is read-only to a Member granted manage_settings, editable to an Admin and the Owner', { skip: dom.skip }, async () => {
  const win = await boot();
  const { mountSettingsSchema } = await fresh('settings_schema.js');
  const schema = { ...SCHEMA, settings: SCHEMA.settings.map((d) => (d.key === 'system.backup_keep' ? { ...d, owner_only: true } : d)) };
  const api = {
    settingsSchema: async () => schema, settings: async () => VALUES, patchSettings: async () => ({ ok: true, changed: [] }),
    backups: async () => ({ backups: [], schedule: { enabled: false } }), pendingRestore: async () => ({ pending: false }),
    people: async () => ({ profiles: [] }),
  };
  const pane = win.document.getElementById('spane-settings');
  await mountSettingsSchema(pane, { api, toast: () => {}, role: 'member', group: 'system' });
  const row = pane.querySelector('[data-key="system.backup_keep"]');
  assert.equal(row.querySelector('input').disabled, true);
  assert.ok(row.querySelector('.schema-owner-only'), 'the row says why');
  pane.textContent = '';
  await mountSettingsSchema(pane, { api, toast: () => {}, role: 'admin', group: 'system' });
  const rowA = pane.querySelector('[data-key="system.backup_keep"]');
  assert.equal(rowA.querySelector('input').disabled, false, 'an Admin edits host-control rows');
  assert.equal(rowA.querySelector('.schema-owner-only'), null);
  pane.textContent = '';
  await mountSettingsSchema(pane, { api, toast: () => {}, role: 'owner', group: 'system' });
  const row2 = pane.querySelector('[data-key="system.backup_keep"]');
  assert.equal(row2.querySelector('input').disabled, false);
  assert.equal(row2.querySelector('.schema-owner-only'), null);
});
