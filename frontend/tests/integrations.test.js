// js/integrations.js (2.3.7): the one-key checklist, the image-generation
// status line and the "Works with DisPatch" catalog cards.

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
const LLM = readFileSync(join(STATIC, 'js', 'llm.js'), 'utf8');
const PLUGINS = readFileSync(join(STATIC, 'js', 'plugins.js'), 'utf8');

const require = createRequire(import.meta.url);
let jsdom = null;
try { jsdom = require('jsdom'); } catch { /* DOM tests skip */ }
const dom = { skip: domSkip(jsdom ? false : 'jsdom is not installed') };

let fetched = [];

async function setup(routes = {}) {
  const { JSDOM } = jsdom;
  const win = new JSDOM(HTML, { url: 'http://127.0.0.1:8765/' }).window;
  const put = (k, v) => Object.defineProperty(globalThis, k, { value: v, configurable: true, writable: true });
  for (const k of ['window', 'document', 'location', 'navigator', 'localStorage', 'Node', 'HTMLElement']) {
    put(k, k === 'window' ? win : win[k]);
  }
  fetched = [];
  globalThis.fetch = async (u, opts = {}) => {
    const key = `${(opts.method || 'GET').toUpperCase()} ${u}`;
    fetched.push({ key, body: opts.body ? JSON.parse(opts.body) : null });
    const h = key === 'GET /static/locales/en.json' ? () => [200, EN] : routes[key];
    const [status, body] = h ? h() : [404, { detail: 'Not Found' }];
    return {
      ok: status >= 200 && status < 300, status, statusText: String(status),
      headers: new win.Headers(), json: async () => body,
    };
  };
  const i18n = await import('../static/js/i18n.js?v=5');
  await i18n.init('en');
  const mod = await import(`../static/js/integrations.js?t=${Math.random()}`);
  return { mod, win };
}

test('AI & Connections has hosts for the one-key box and image generation', () => {
  const ai = HTML.slice(HTML.indexOf('id="spane-connections"'), HTML.indexOf('id="spane-people"'));
  assert.match(ai, /id="llm-quick-host"/);
  assert.match(ai, /id="llm-image-host"/);
  assert.match(LLM, /mountQuickSetup\(\$\('llm-quick-host'\)/);
  assert.match(LLM, /mountImageSection\(\$\('llm-image-host'\)/);
  assert.match(PLUGINS, /catalogGroup\(/);
});

test('every integrations.* key the module uses exists in en.json', () => {
  const src = readFileSync(join(STATIC, 'js', 'integrations.js'), 'utf8');
  for (const m of src.matchAll(/'integrations\.([a-z_]+)'/g)) {
    assert.ok(EN.integrations[m[1]], m[1]);
  }
});

test('catalog cards link only what the server marked as a repo', { skip: dom.skip }, async () => {
  const { mod } = await setup();
  const pub = mod.catalogCard({ id: 'studioforge', name: 'StudioForge', summary: 's',
    repo: 'https://github.com/LaserLloyd/StudioForge', status: 'not_configured',
    connects: 'c', docs: 'https://example/doc#studioforge' });
  assert.equal(pub.querySelector('.cat-status').textContent, 'Not set up');
  assert.deepEqual([...pub.querySelectorAll('a')].map((a) => a.getAttribute('href')),
    ['https://github.com/LaserLloyd/StudioForge', 'https://example/doc#studioforge']);
  const priv = mod.catalogCard({ id: 'speechforge', name: 'SpeechForge', summary: 's', repo: '',
    repo_note: 'Not public yet.', status: 'connected', docs: '' });
  assert.equal(priv.querySelectorAll('a').length, 0);
  assert.match(priv.textContent, /Not public yet\./);
  assert.equal(priv.dataset.status, 'connected');
});

test('the checklist marks each capability', { skip: dom.skip }, async () => {
  const { mod } = await setup();
  const ul = document.createElement('ul');
  mod.renderChecklist(ul, [
    { id: 'chat', label: 'Chat', status: 'ok', detail: 'Atlas on MiniMax-M3' },
    { id: 'embeddings', label: 'Knowledge search', status: 'absent', detail: 'not offered' },
    { id: 'images', label: 'Image generation', status: 'failed', detail: 'HTTP 403' },
  ]);
  // ok / failed are ThemeForge sprite icons now (check, x); absent stays a dash.
  const marks = [...ul.querySelectorAll('.qs-mark')];
  assert.deepEqual(marks.map((n) => n.textContent), ['', '—', '']);
  assert.ok(marks[0].querySelector('use[href$="#check"]'));
  assert.ok(marks[2].querySelector('use[href$="#x"]'));
  assert.equal(ul.querySelector('[data-cap="images"]').dataset.status, 'failed');
});

test('image status line says where pictures come from', { skip: dom.skip }, async () => {
  const { mod } = await setup();
  const presets = [{ id: 'minimax', label: 'MiniMax', model: 'image-01' }];
  assert.match(mod.imageStatusText({ presets, clawforge: false,
    config: { provider: 'minimax', key_source: 'shared' } }), /MiniMax \(image-01\).*shared/);
  assert.match(mod.imageStatusText({ presets, clawforge: true, config: { provider: '' } }), /image server/);
  assert.match(mod.imageStatusText({ presets, clawforge: false, config: { provider: '' } }), /off/);
});

test('one-key setup posts the key once and paints the checklist', { skip: dom.skip }, async () => {
  const { mod } = await setup({
    'GET /api/llm/quicksetup': () => [200, { plans: [{ id: 'minimax', label: 'MiniMax', chat_model: 'MiniMax-M3', images: true, embeddings: false, docs: '' }] }],
    'POST /api/llm/quicksetup': () => [200, { rows: [{ id: 'chat', label: 'Chat', status: 'ok', detail: '' }], bot: null }],
  });
  const host = document.createElement('div');
  document.body.append(host);
  await mod.mountQuickSetup(host);
  host.querySelector('#qs-key').value = 'k-123';
  host.querySelector('#qs-go').click();
  await new Promise((r) => setTimeout(r, 20));
  const post = fetched.find((f) => f.key === 'POST /api/llm/quicksetup');
  assert.deepEqual(post.body, { provider: 'minimax', api_key: 'k-123' });
  assert.equal(host.querySelector('#qs-key').value, '', 'the key is cleared after sending');
  assert.equal(host.querySelectorAll('.qs-row').length, 1);
});

test('the checklist survives the AI tab re-mounting after a connect', { skip: dom.skip }, async () => {
  const { mod } = await setup({
    'GET /api/llm/quicksetup': () => [200, { plans: [{ id: 'minimax', label: 'MiniMax', chat_model: 'MiniMax-M3', images: true, embeddings: false, docs: '' }] }],
    'POST /api/llm/quicksetup': () => [200, { rows: [
      { id: 'chat', label: 'Chat', status: 'ok', detail: '' },
      { id: 'images', label: 'Image generation', status: 'ok', detail: '' }], bot: null }],
  });
  const host = document.createElement('div');
  document.body.append(host);
  await mod.mountQuickSetup(host);
  host.querySelector('#qs-key').value = 'k';
  host.querySelector('#qs-go').click();
  await new Promise((r) => setTimeout(r, 20));
  await mod.mountQuickSetup(host);                 // what a roster push does
  assert.equal(host.querySelectorAll('.qs-row').length, 2);
  assert.match(host.querySelector('#qs-result').textContent, /Atlas is ready/);
});
