// Settings -> AI models (js/llm.js) in 2.3.0: the panel connects a BOT — the
// "Bot to connect" picker lists the direct-API bots from /api/bots/all with
// "New bot…" last, the Lookups switch rides along, and Save posts
// {backend: "direct", target, tools, ...} to /api/llm/connect. Rendered against
// the REAL index.html markup under jsdom with fetch() stubbed.

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { domSkip } from './_require-dom.js';

const HERE = dirname(fileURLToPath(import.meta.url));
const STATIC = join(HERE, '..', 'static');
const HTML = readFileSync(join(STATIC, 'index.html'), 'utf8');
const EN = JSON.parse(readFileSync(join(STATIC, 'locales', 'en.json'), 'utf8'));

const require = createRequire(import.meta.url);
let jsdom = null;
try { jsdom = require('jsdom'); } catch { /* not installed - DOM tests skip */ }
const dom = { skip: domSkip(jsdom ? false : 'jsdom is not installed') };

const PROVIDERS = () => ({
  providers: [
    { id: 'lmstudio', label: 'LM Studio', base_url: 'http://127.0.0.1:1234/v1', local: true, key_accepted: false, models: [] },
    { id: 'minimax', label: 'MiniMax', base_url: 'https://api.minimax.io/v1', local: false, key_accepted: true, key_env: 'MINIMAX_API_KEY', models: ['MiniMax-M3'] },
  ],
  connected: [],
});
const BOTS = () => ({
  bots: [
    { id: 'atlas', name: 'Atlas', backend: 'direct', tools: true, connected: false, provider: '', model: '' },
    { id: 'sage', name: 'Sage', backend: 'direct', tools: false, connected: true, provider: 'minimax', model: 'MiniMax-M3' },
    { id: 'nova', name: 'Nova', backend: 'agent', tools: false, connected: false },
  ],
});

let calls = [];

async function setup() {
  const { JSDOM } = jsdom;
  const win = new JSDOM(HTML, { url: 'http://127.0.0.1:8765/', runScripts: 'outside-only' }).window;
  const put = (k, v) => Object.defineProperty(globalThis, k, { value: v, configurable: true, writable: true });
  for (const k of ['window', 'document', 'location', 'navigator', 'localStorage', 'Node', 'HTMLElement', 'HTMLInputElement']) put(k, win[k]);
  calls = [];
  const routes = {
    'GET /static/locales/en.json': () => [200, EN],
    'GET /api/llm/providers': () => [200, PROVIDERS()],
    'GET /api/bots/all': () => [200, BOTS()],
    'POST /api/llm/connect': (o) => [200, { id: 'atlas', name: 'Atlas', connected: true, bot: { id: 'atlas', name: 'Atlas', connected: true } }],
  };
  globalThis.fetch = async (u, opts = {}) => {
    const key = `${(opts.method || 'GET').toUpperCase()} ${u}`;
    calls.push({ key, body: opts.body ? JSON.parse(opts.body) : null });
    const h = routes[key];
    const [status, body] = h ? h(opts) : [404, { detail: 'Not Found' }];
    return { ok: status >= 200 && status < 300, status, statusText: String(status), headers: new win.Headers(), json: async () => body };
  };
  const i18n = await import('../static/js/i18n.js?v=4');
  await i18n.init('en');
  const mod = await import(`../static/js/llm.js?t=${Math.random()}`);
  const rec = { toasts: [], connected: [] };
  mod.initLlmPanel({ toast: (m, e) => rec.toasts.push([m, !!e]), onConnected: async (b) => rec.connected.push(b), isDecoy: () => false, settingsTabActive: () => true });
  await mod.activateLlmPanel({});
  return { win, mod, rec, doc: win.document };
}

const tick = () => new Promise((r) => setTimeout(r, 0));

test('the picker lists the direct-API bots (never an agent bot) with New bot… last, defaulting to the unconnected one', dom, async () => {
  const { doc } = await setup();
  const opts = [...doc.getElementById('llm-target').options].map((o) => [o.value, o.textContent]);
  assert.deepEqual(opts.map((o) => o[0]), ['atlas', 'sage', '__new__']);
  assert.match(opts[0][1], /Atlas — not connected yet/);
  assert.match(opts[1][1], /Sage — connected \(MiniMax, MiniMax-M3\)/);
  assert.equal(doc.getElementById('llm-target').value, 'atlas');
  assert.equal(doc.getElementById('llm-tools').checked, true, 'Atlas has lookups on');
});

test('picking a connected bot pre-fills its provider, model, saved-key placeholder and Lookups state', dom, async () => {
  const { doc } = await setup();
  const sel = doc.getElementById('llm-target');
  sel.value = 'sage'; sel.dispatchEvent(new doc.defaultView.Event('change'));
  await tick();
  assert.equal(doc.getElementById('llm-provider').value, 'minimax');
  assert.equal(doc.getElementById('llm-tools').checked, false);
  assert.equal(doc.getElementById('llm-key').placeholder, EN.llm.api_key_saved);
  assert.equal(doc.getElementById('llm-name').value, 'Sage');
});

test('Save posts backend=direct, the target and the Lookups switch; the panel then opens the bot', dom, async () => {
  const { doc, rec } = await setup();
  doc.getElementById('llm-provider').value = 'minimax';
  doc.getElementById('llm-provider').dispatchEvent(new doc.defaultView.Event('change'));
  doc.getElementById('llm-model-text').value = 'MiniMax-M3';
  doc.getElementById('llm-key').value = 'sk-test';
  doc.getElementById('llm-tools').checked = false;
  doc.getElementById('llm-save').click();
  for (let i = 0; i < 5; i++) await tick();
  const post = calls.find((c) => c.key === 'POST /api/llm/connect');
  assert.ok(post, 'connect was posted');
  assert.equal(post.body.backend, 'direct');
  assert.equal(post.body.target, 'atlas');
  assert.equal(post.body.tools, false);
  assert.equal(post.body.provider, 'minimax');
  assert.equal(post.body.api_key, 'sk-test');
  assert.deepEqual(rec.connected.map((b) => b.id), ['atlas']);
});

test('New bot… needs a name and derives the id from it', dom, async () => {
  const { doc, mod } = await setup();
  const sel = doc.getElementById('llm-target');
  sel.value = '__new__'; sel.dispatchEvent(new doc.defaultView.Event('change'));
  await tick();
  assert.equal(doc.getElementById('llm-advanced').open, true, 'Advanced opens for the name');
  doc.getElementById('llm-model-text').value = 'x';
  doc.getElementById('llm-name').value = '';
  doc.getElementById('llm-save').click();
  for (let i = 0; i < 5; i++) await tick();
  assert.equal(calls.some((c) => c.key === 'POST /api/llm/connect'), false);
  assert.equal(doc.getElementById('llm-result').textContent, EN.llm.need_name);
  doc.getElementById('llm-name').value = 'Study Buddy!';
  doc.getElementById('llm-save').click();
  for (let i = 0; i < 5; i++) await tick();
  const post = calls.find((c) => c.key === 'POST /api/llm/connect');
  assert.equal(post.body.target, 'study-buddy');
  assert.equal(post.body.name, 'Study Buddy!');
  assert.equal(mod.slugForBot('  Ünïcode name  '), 'n-code-name');
  assert.equal(mod.slugForBot(''), 'assistant');
});

test('Test connection names the picked bot (target) and the env name, so a connected bot re-tests on its saved key', dom, async () => {
  const { doc } = await setup();
  const sel = doc.getElementById('llm-target');
  sel.value = 'sage'; sel.dispatchEvent(new doc.defaultView.Event('change'));
  await tick();
  // The key field is blank on purpose: a saved key never comes back out of the
  // API, and the server falls back to the stored key of `target` for the same
  // provider (main._stored_probe_key) — which it cannot do without `target`.
  assert.equal(doc.getElementById('llm-key').value, '');
  doc.getElementById('llm-key-env').value = 'MINIMAX_API_KEY';
  doc.getElementById('llm-test').click();
  for (let i = 0; i < 5; i++) await tick();
  const probe = calls.find((c) => c.key === 'POST /api/llm/test');
  assert.ok(probe, 'the probe was posted');
  assert.equal(probe.body.provider, 'minimax');
  assert.equal(probe.body.target, 'sage');
  assert.equal(probe.body.api_key, '');
  assert.equal(probe.body.api_key_env, 'MINIMAX_API_KEY');
  // New bot…: nothing stored to fall back on, so no target is named.
  sel.value = '__new__'; sel.dispatchEvent(new doc.defaultView.Event('change'));
  await tick();
  doc.getElementById('llm-test').click();
  for (let i = 0; i < 5; i++) await tick();
  const again = calls.filter((c) => c.key === 'POST /api/llm/test').at(-1);
  assert.equal(again.body.target, '');
});
