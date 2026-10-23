// Settings -> AI & Connections (js/llm.js) in 3.2: the per-bot "Bot to connect"
// form is gone (a server is a connection; a character picks one in its Details).
// What stays: the one-key quick setup, open as the empty state and closed once a
// connection exists, the image-generation block, and the first-run card that
// opens this section on the quick setup. Rendered against the REAL index.html
// markup under jsdom with fetch() stubbed.

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

let calls = [];

async function setup({ quickOpen } = {}) {
  const { JSDOM } = jsdom;
  const win = new JSDOM(HTML, { url: 'http://127.0.0.1:8765/', runScripts: 'outside-only' }).window;
  const put = (k, v) => Object.defineProperty(globalThis, k, { value: v, configurable: true, writable: true });
  for (const k of ['window', 'document', 'location', 'navigator', 'localStorage', 'Node', 'HTMLElement', 'HTMLInputElement']) put(k, win[k]);
  calls = [];
  const routes = {
    'GET /static/locales/en.json': () => [200, EN],
    'GET /api/llm/quicksetup': () => [200, { plans: [{ id: 'minimax', label: 'MiniMax', chat_model: 'MiniMax-M3', images: true, embeddings: false, docs: '' }] }],
    'GET /api/llm/image': () => [200, { presets: [], config: { provider: '' }, clawforge: false }],
  };
  globalThis.fetch = async (u, opts = {}) => {
    const key = `${(opts.method || 'GET').toUpperCase()} ${u}`;
    calls.push({ key, body: opts.body ? JSON.parse(opts.body) : null });
    const h = routes[key];
    const [status, body] = h ? h(opts) : [404, { detail: 'Not Found' }];
    return { ok: status >= 200 && status < 300, status, statusText: String(status), headers: new win.Headers(), json: async () => body };
  };
  const i18n = await import('../static/js/i18n.js?v=5');
  await i18n.init('en');
  const mod = await import(`../static/js/llm.js?t=${Math.random()}`);
  const rec = { tabs: [] };
  mod.initLlmPanel({ toast: () => {}, isDecoy: () => false, settingsTabActive: (t) => t === 'connections',
    openSettingsTab: (t) => rec.tabs.push(t) });
  await mod.activateLlmPanel({ quickOpen });
  return { win, mod, rec, doc: win.document };
}

test('the per-bot connect form is gone from the markup; the section has the quick setup and image hosts', () => {
  for (const id of ['llm-target', 'llm-provider', 'llm-base-url', 'llm-key', 'llm-model', 'llm-save', 'spane-ai']) {
    assert.doesNotMatch(HTML, new RegExp(`id="${id}"`), id);
  }
  const pane = HTML.slice(HTML.indexOf('id="spane-connections"'), HTML.indexOf('id="spane-people"'));
  assert.match(pane, /id="llm-quick-host"[^>]*data-anchor="quick-setup"/);
  assert.match(pane, /id="llm-image-host"[^>]*data-anchor="images"/);
});

test('quick setup is open as the empty state (no connection yet) and closed once there is one', dom, async () => {
  const empty = await setup({ quickOpen: true });
  assert.equal(empty.doc.getElementById('qs-box').open, true);
  const some = await setup({ quickOpen: false });
  assert.equal(some.doc.getElementById('qs-box').open, false);
  assert.ok(some.doc.getElementById('img-section'), 'image generation is drawn too');
});

test('the old entry point opens AI & Connections on the quick setup; the first-run card has one door', dom, async () => {
  const { mod, rec } = await setup();
  await mod.openLlmPanel({});
  assert.deepEqual(rec.tabs, ['connections#quick-setup']);
  const card = mod.firstRunCard(() => {});
  assert.equal(card.querySelectorAll('button').length, 1);
  assert.equal(mod.llmPanelOpen(), true);
});

test('slugForBot still derives a safe id from a name', dom, async () => {
  const { mod } = await setup();
  assert.equal(mod.slugForBot('Ada Lovelace!'), 'ada-lovelace');
  assert.equal(mod.slugForBot('   '), 'assistant');
});
