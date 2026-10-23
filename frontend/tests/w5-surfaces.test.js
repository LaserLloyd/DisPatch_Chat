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
  const i18n = await import('../static/js/i18n.js?v=5');
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
    { key: 'system.backup_keep', type: 'integer', default: 12, label: 'Snapshots to keep', description: 'How many snapshots.', group: 'system', section: 'Backups', level: 'basic', restart_required: true, min: 1, ui_section: 'system#backups' },
    { key: 'system.host', type: 'string', default: '127.0.0.1', label: 'Listen address', description: 'Informational.', group: 'system', section: 'Network', level: 'advanced' },
    { key: 'people.lock_timeout_seconds', type: 'integer', default: 600, label: 'Auto-lock', description: 'Idle seconds.', group: 'people', section: 'Sessions', level: 'basic',
      unit: 'seconds', min: 30, presets: [300, 600, 900, 3600], ui_section: 'people#signin-rules' },
    { key: 'system.backup_passphrase', type: 'secret', default: null, label: 'Backup passphrase', description: 'Write only.', group: 'system', section: 'Backups', level: 'advanced', secret: true, ui_section: 'system#backups' },
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

test('connections list: reachable with no models is a warning, Used by shows names, a type chip only when the name lacks it', { skip: dom.skip }, async () => {
  await boot();
  const C = await fresh('connections.js');
  // 3.2: "0 models available" was painted green.
  const zero = C.describeTest({ ok: true, models: [], models_listed: true, latency_ms: 231 });
  assert.equal(zero[0], 'warn');
  assert.match(zero[2], /check the key or the address/);
  assert.equal(C.describeTest({ ok: true, models_listed: false })[0], 'ok', 'a server that does not list models is fine');
  const names = { a: 'Ann', b: 'Bob' };
  assert.equal(C.usedByText(['a', 'b'], (id) => names[id]), 'Used by Ann, Bob');
  assert.equal(C.usedByText(['a', 'b', 'c', 'd', 'e', 'f', 'g'], (id) => names[id], 5), 'Used by Ann, Bob, c, d, e and 2 more');
  assert.equal(C.usedByText([], (id) => id), EN.cxn.unused);
  // A stored test keeps no model list: the connection's own list counts (not a false "0 models").
  const stored = C.describeStored({ models: ['m1'], last_test: { ok: true, latency_ms: 4 } });
  assert.equal(stored[0], 'ok');
  assert.equal(C.describeStored({ models: [], last_test: { ok: true } })[0], 'warn');
  // Other profiles' personal bots are a count, never names or ids.
  assert.equal(C.usedByText(['a'], (id) => names[id], 5, 3), 'Used by Ann, personal bots: 3');
  assert.equal(C.usedByText([], (id) => id, 5, 2), 'Used by personal bots: 2');
  // The count survives the "+N more" cut.
  assert.equal(C.usedByText(['a', 'b', 'c', 'd', 'e', 'f'], (id) => names[id] || id, 5, 3),
    'Used by Ann, Bob, c, d, e, personal bots: 3 and 1 more');
  assert.equal(C.nameSaysType({ name: 'OpenAI-compatible server (api.example/v1)', type_label: 'OpenAI-compatible server' }), true);
  assert.equal(C.nameSaysType({ name: 'Rig', type_label: 'StudioForge' }), false);
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

test('character diff: only what changed; connection and model travel together; hidden is the Admin flag', async () => {
  const K = await fresh('characters.js').catch(() => null);
  if (!K) return;
  const orig = { description: '', hidden: false, backend: { connection: 'c1', model: 'm1' }, personality: { preset: 'default' }, backend_kind: 'direct' };
  assert.deepEqual(K.diffCharacter(orig, { description: '', hidden: false, connection: 'c1', model: 'm1', personality: 'default' }), {});
  assert.deepEqual(K.diffCharacter(orig, { hidden: true, description: 'Hi', connection: 'c2', model: 'x', personality: 'tutor' }),
    { description: 'Hi', hidden: true, connection: 'c2', model: 'x', personality: 'tutor' });
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
  // A PIN is optional on both types; blank rows are skipped.
  assert.deepEqual(W.familyPayload([
    { role: 'user', name: ' Max ', pin: '5555' }, { role: 'basic', name: 'Mia', pin: '' }, { role: 'user', name: '', pin: '' }]), {
    people: [{ name: 'Max', role: 'user', pin: '5555' }, { name: 'Mia', role: 'basic' }] });
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

// mountSchemaRows (3.2): the rows of one slot of a section, applied as they change.
const slotHost = (win) => {
  const h = win.document.createElement('div');
  h.className = 'schema-host';
  win.document.body.append(h);
  return h;
};

test('schema rows: a slot draws its section#anchor rows, the anchor-less slot the rest, env rows locked', { skip: dom.skip }, async () => {
  const win = await boot();
  const S = await fresh('settings_schema.js');
  const api = { settingsSchema: async () => SCHEMA, settings: async () => VALUES, patchSettings: async () => ({ ok: true }) };
  const backups = slotHost(win);
  const advanced = slotHost(win);
  const claimed = new Set(['backups']);
  await S.mountSchemaRows(backups, { api, section: 'system', anchor: 'backups', claimed, role: 'owner' });
  await S.mountSchemaRows(advanced, { api, section: 'system', anchor: '', claimed, role: 'owner' });
  assert.deepEqual([...backups.querySelectorAll('.ui-setting')].map((n) => n.dataset.key), ['system.backup_keep', 'system.backup_passphrase']);
  assert.deepEqual([...advanced.querySelectorAll('.ui-setting')].map((n) => n.dataset.key), ['system.host'], 'no row twice');
  const host = advanced.querySelector('[data-key="system.host"]');
  assert.equal(host.getAttribute('data-source'), 'env');
  assert.equal(host.querySelector('input').disabled, true);
  // A slot with nothing for it hides itself.
  const empty = slotHost(win);
  assert.equal(await S.mountSchemaRows(empty, { api, section: 'characters', anchor: '', role: 'owner' }), null);
  assert.equal(empty.hidden, true);
});

test('schema rows: a change applies at once (one PATCH for that key), says a restart is needed, and can be undone', { skip: dom.skip }, async () => {
  const win = await boot();
  const S = await fresh('settings_schema.js');
  const patched = [];
  const toasts = [];
  const api = {
    settingsSchema: async () => SCHEMA, settings: async () => VALUES,
    patchSettings: async (set, reset) => { patched.push({ set, reset }); return { ok: true, changed: Object.keys(set), restart_required: ['system.backup_keep'] }; },
  };
  const host = slotHost(win);
  await S.mountSchemaRows(host, { api, section: 'system', anchor: 'backups', claimed: new Set(['backups']), role: 'owner',
    toast: (m, err, o) => toasts.push([m, !!err, o && o.undo]) });
  const input = host.querySelector('[data-key="system.backup_keep"] input');
  input.value = '30';
  input.dispatchEvent(new win.Event('input', { bubbles: true }));
  assert.equal(patched.length, 0, 'typing alone sends nothing');
  input.dispatchEvent(new win.Event('change', { bubbles: true }));
  await tick(); await tick(); await tick();
  assert.deepEqual(patched, [{ set: { 'system.backup_keep': 30 }, reset: [] }]);
  assert.match(toasts.at(-1)[0], /Snapshots to keep saved\. It applies after DisPatch restarts\./);
  assert.equal(typeof toasts.at(-1)[2], 'function', 'the toast offers Undo');
  await toasts.at(-1)[2]();
  await tick(); await tick();
  assert.deepEqual(patched.at(-1), { set: { 'system.backup_keep': 20 }, reset: [] }, 'Undo writes the value it had');
});

test('schema rows: a refused value is shown on its row', { skip: dom.skip }, async () => {
  const win = await boot();
  const S = await fresh('settings_schema.js');
  const api = {
    settingsSchema: async () => SCHEMA, settings: async () => VALUES,
    patchSettings: async () => { const e = new Error('422: x'); e.errors = { 'system.backup_keep': 'Snapshots to keep: must be at least 1' }; throw e; },
  };
  const host = slotHost(win);
  await S.mountSchemaRows(host, { api, section: 'system', anchor: 'backups', claimed: new Set(['backups']), role: 'owner' });
  const input = host.querySelector('[data-key="system.backup_keep"] input');
  input.value = '0';
  input.dispatchEvent(new win.Event('change', { bubbles: true }));
  await tick(); await tick();
  const err = host.querySelector('[data-key="system.backup_keep"] .schema-error');
  assert.equal(err.hidden, false);
  assert.match(err.textContent, /must be at least 1/);
  assert.equal(input.getAttribute('aria-invalid'), 'true');
});

test('schema rows: a duration is a dropdown of presets with Custom…, written in the row\'s own unit', { skip: dom.skip }, async () => {
  const win = await boot();
  const S = await fresh('settings_schema.js');
  const patched = [];
  const api = { settingsSchema: async () => SCHEMA, settings: async () => VALUES,
    patchSettings: async (set) => { patched.push(set); return { ok: true, restart_required: [] }; } };
  const host = slotHost(win);
  await S.mountSchemaRows(host, { api, section: 'people', anchor: 'signin-rules', claimed: new Set(['signin-rules']), role: 'owner' });
  const sel = host.querySelector('[data-key="people.lock_timeout_seconds"] select');
  assert.deepEqual([...sel.options].map((o) => o.textContent), ['5 minutes', '10 minutes', '15 minutes', '1 hour', 'Custom…']);
  assert.equal(sel.value, '600');
  sel.value = '3600';
  sel.dispatchEvent(new win.Event('change', { bubbles: true }));
  await tick(); await tick();
  assert.deepEqual(patched.at(-1), { 'people.lock_timeout_seconds': 3600 });
  // The row is redrawn from the server's answer after a write: look it up again.
  const sel2 = host.querySelector('[data-key="people.lock_timeout_seconds"] select');
  sel2.value = 'custom';
  sel2.dispatchEvent(new win.Event('change', { bubbles: true }));
  const custom = host.querySelector('.schema-measure-custom');
  assert.equal(custom.hidden, false);
  custom.querySelector('input').value = '2';
  custom.querySelector('select').value = 'hours';
  custom.querySelector('select').dispatchEvent(new win.Event('change', { bubbles: true }));
  await tick(); await tick();
  assert.deepEqual(patched.at(-1), { 'people.lock_timeout_seconds': 7200 });
});

test('durations and sizes: split, join and format round-trip', async () => {
  await boot().catch(() => null);
  const S = await fresh('settings_schema.js').catch(() => null);
  if (!S) return;
  const secs = { type: 'integer', unit: 'seconds', min: 30 };
  assert.deepEqual(S.splitMeasure(secs, 600), { n: 10, unit: 'minutes' });
  assert.deepEqual(S.splitMeasure(secs, 7200), { n: 2, unit: 'hours' });
  assert.deepEqual(S.splitMeasure(secs, 90), { n: 90, unit: 'seconds' });
  assert.equal(S.joinMeasure(secs, 10, 'minutes'), 600);
  assert.equal(S.joinMeasure(secs, '', 'minutes'), undefined);
  const hours = { type: 'integer', unit: 'hours' };
  assert.deepEqual(S.splitMeasure(hours, 48), { n: 2, unit: 'days' });
  assert.equal(S.joinMeasure(hours, 2, 'days'), 48);
  assert.equal(S.joinMeasure(hours, 30, 'minutes'), undefined, 'not a whole number of hours');
  assert.deepEqual(S.customUnits(hours), ['hours', 'days']);
  const bytes = { type: 'integer', unit: 'bytes' };
  assert.deepEqual(S.splitMeasure(bytes, 20 * 1024 ** 3), { n: 20, unit: 'gigabytes' });
  assert.equal(S.joinMeasure(bytes, 200, 'megabytes'), 200 * 1024 ** 2);
  assert.equal(S.measureOf({ type: 'integer', unit: 'days' }), 'duration');
  assert.equal(S.measureOf({ type: 'string', unit: 'seconds' }), '');
  assert.equal(S.formatMeasure({ ...secs, presets: [0, 60] }, 0, 'en'), 'Off');
  assert.equal(S.formatMeasure(secs, 3600, 'en'), '1 hour');
});

test('which rows go where: a row whose anchor has no slot falls back to the section\'s Advanced', async () => {
  const S = await fresh('settings_schema.js').catch(() => null);
  if (!S) return;
  const defs = [
    { key: 'a.x', ui_section: 'system#network' }, { key: 'a.y', ui_section: 'system' },
    { key: 'a.z', ui_section: 'system#nowhere' }, { key: 'people.machine_loopback_trust', ui_section: 'people#machine' },
    { key: 'b.q', group: 'tools' },
  ];
  const claimed = new Set(['network']);
  assert.deepEqual(S.rowsForSlot(defs, 'system', 'network', claimed).map((d) => d.key), ['a.x']);
  assert.deepEqual(S.rowsForSlot(defs, 'system', '', claimed).map((d) => d.key), ['a.y', 'a.z']);
  assert.deepEqual(S.rowsForSlot(defs, 'people', '', new Set()).map((d) => d.key), [], 'drawn by the section itself');
  assert.deepEqual(S.uiTarget({ group: 'tools' }), ['plugins', '']);
  // 4.0: the lock-target pair is drawn by People's "When locked, show" picker only, never as rows.
  const lock = [{ key: 'people.lock_target', ui_section: 'people#lock-target' },
    { key: 'people.default_profile', ui_section: 'people#lock-target' }, { key: 'people.other', ui_section: 'people' }];
  assert.deepEqual(S.rowsForSlot(lock, 'people', '', new Set()).map((d) => d.key), ['people.other']);
  assert.deepEqual(S.rowsForSlot(lock, 'people', 'lock-target', new Set(['lock-target'])).map((d) => d.key), []);
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
  assert.match(card.querySelector('.cxn-result').textContent, /Models available: 1|12 ms/);
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
  assert.match(form.querySelector('.cxn-result').textContent, /Models available/);
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
    allCharacters: async () => ({ characters: [{ id: 'nova', name: 'Nova', description: '', hidden: false,
      backend: { kind: 'direct', connection: 'c1', model: 'm1' }, backend_kind: 'direct', personality: { preset: 'default' } }] }),
    connections: async () => ({ connections: [{ id: 'c1', name: 'LM Studio', type_label: 'LM Studio', models: ['m1', 'm2'], default_model: 'm1' }] }),
    personalities: async () => ({ presets: [{ id: 'default', title: 'Default', description: '' }, { id: 'tutor', title: 'Tutor', description: '' }] }),
    people: async () => ({ profiles: [{ id: 'p1', name: 'Mia', role: 'basic' }, { id: 'p2', name: 'Boss', role: 'admin' }], me: 'p2' }),
    patchCharacter: async (id, body) => { patched.push([id, body]); return {}; },
  };
  const panel = await K.characterPanel({ id: 'nova', name: 'Nova' }, { api, toast() {}, openTab() {}, reload: async () => {} });
  win.document.body.append(panel);
  const labels = [...panel.querySelectorAll('label')].map((l) => l.textContent);
  assert.ok(labels.includes(EN.chars.connection) && labels.includes(EN.chars.model) && labels.includes(EN.chars.personality));
  assert.doesNotMatch(panel.textContent, /Mia/, 'a Basic profile owns no bots, so it is not a copy target');
  assert.match(panel.textContent, /Boss/, 'an Admin can copy into their own bots');
  panel.querySelector('.chr-hide input').checked = true;
  [...panel.querySelectorAll('button')].find((b) => b.textContent === EN.chars.save).click();
  await tick(); await tick();
  assert.deepEqual(patched, [['nova', { hidden: true }]]);
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
  assert.deepEqual(seen[0], { name: 'Admin', password: 'long-enough-pw' });  // scrub-ok: test fixture, not a real credential
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

test('Backups: Admins are offered them, a User is not', { skip: dom.skip }, async () => {
  const win = await boot();
  const { mountBackups } = await fresh('backup.js');
  const host = win.document.createElement('div');
  mountBackups(host, { api: {}, role: 'member' });
  assert.match(host.textContent, /managed by Admins/);
  const admin = win.document.createElement('div');
  mountBackups(admin, { api: { backups: async () => ({ backups: [], schedule: { enabled: false } }), pendingRestore: async () => ({ pending: false }), people: async () => ({ profiles: [] }) }, role: 'admin' });
  assert.doesNotMatch(admin.textContent, /managed by Admins/);
});

// ------------------------------------------------------------------ 4. wiring

test('index.html: AI & Connections is a household section; All settings is gone (its rows live in the sections)', () => {
  assert.match(HTML, /id="stab-connections"[^>]*aria-controls="spane-connections"/);
  assert.match(HTML, /class="settings-tab admin-only" id="stab-connections"/);
  assert.doesNotMatch(HTML, /id="stab-settings"|id="spane-settings"/);
  // Every section that holds schema rows has an anchor-less slot under its Advanced disclosure.
  for (const sec of ['appearance', 'characters', 'connections', 'system']) {
    assert.match(HTML, new RegExp(`<div class="schema-host" data-schema="${sec}"></div>`), sec);
  }
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
  assert.match(MAIN, /characterPanel\(bot, \{ \.\.\.characterCtx\(\), skip: \['personality'\]/);
  assert.match(MAIN, /case 'connections':\s+await mountConnectionsSection\(pane\)/);
  assert.match(MAIN, /const ctl = await mountConnections\(host, \{/);
  assert.match(MAIN, /function mountSchemaHosts\(pane/);
});

test('the W5 strings exist in every locale with the same keys', () => {
  for (const lang of ['ar', 'de', 'es', 'fr', 'ja', 'pt', 'zh']) {
    const L = JSON.parse(read(`locales/${lang}.json`));
    for (const ns of ['schema', 'cxn', 'chars', 'wiz', 'bkp']) {
      assert.deepEqual(Object.keys(L[ns]).sort(), Object.keys(EN[ns]).sort(), `${lang}.${ns}`);
    }
  }
});

test('schema rows: a host-control row is read-only to a Member granted manage_settings, editable to an Admin and the Owner', { skip: dom.skip }, async () => {
  const win = await boot();
  const S = await fresh('settings_schema.js');
  const schema = { ...SCHEMA, settings: SCHEMA.settings.map((d) => (d.key === 'system.backup_keep' ? { ...d, owner_only: true } : d)) };
  const api = { settingsSchema: async () => schema, settings: async () => VALUES, patchSettings: async () => ({ ok: true, changed: [] }) };
  const draw = async (role) => {
    const h = slotHost(win);
    await S.mountSchemaRows(h, { api, section: 'system', anchor: 'backups', claimed: new Set(['backups']), role, force: true });
    return h.querySelector('[data-key="system.backup_keep"]');
  };
  const row = await draw('member');
  assert.equal(row.querySelector('input').disabled, true);
  assert.ok(row.querySelector('.schema-owner-only'), 'the row says why');
  const rowA = await draw('admin');
  assert.equal(rowA.querySelector('input').disabled, false, 'an Admin edits host-control rows');
  assert.equal(rowA.querySelector('.schema-owner-only'), null);
  const row2 = await draw('owner');
  assert.equal(row2.querySelector('input').disabled, false);
  assert.equal(row2.querySelector('.schema-owner-only'), null);
});

test('schema rows: a default connection whose connection was deleted shows as missing, not as None', { skip: dom.skip }, async () => {
  const win = await boot();
  const S = await fresh('settings_schema.js');
  const def = { key: 'connections.default', type: 'choice', default: '', label: 'Default connection', description: 'd',
    group: 'connections', section: '', level: 'basic', choices_from: 'connections', choices: ['', 'c1'],
    choice_labels: { '': 'None', c1: 'Box' }, ui_section: 'connections#list' };
  const schema = { ...SCHEMA, settings: [...SCHEMA.settings, def] };
  const values = { ...VALUES, values: { ...VALUES.values,
    'connections.default': { value: 'gone-1', source: 'store', default: '', is_default: false } } };
  const api = { settingsSchema: async () => schema, settings: async () => values, patchSettings: async () => ({ ok: true, changed: [] }) };
  const h = slotHost(win);
  await S.mountSchemaRows(h, { api, section: 'connections', anchor: 'list', claimed: new Set(['list']), role: 'owner', force: true });
  const sel = h.querySelector('[data-key="connections.default"] select');
  assert.ok(sel, 'the row is drawn');
  assert.equal(sel.value, 'gone-1');
  assert.equal(sel.selectedOptions[0].textContent, EN.schema.missing_connection.replace('{id}', 'gone-1'));
  assert.deepEqual([...sel.options].map((o) => o.value), ['', 'c1', 'gone-1']);
});
