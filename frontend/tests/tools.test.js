// Tools rail / pane / Settings — rendered against the REAL index.html markup
// with fixture data shaped exactly like the contract's GET /api/tools
// (docs/design/2026-09-25-tools-plugins.md). The backend is stubbed at fetch().

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { domSkip } from './_require-dom.js';

const HERE = dirname(fileURLToPath(import.meta.url));
const STATIC = join(HERE, '..', 'static');
const FIXTURE = JSON.parse(readFileSync(join(HERE, 'fixtures', 'tools.json'), 'utf8'));
const HTML = readFileSync(join(STATIC, 'index.html'), 'utf8');

const require = createRequire(import.meta.url);
let jsdom = null;
try { jsdom = require('jsdom'); } catch { /* not installed — tests skip */ }
const dom = { skip: domSkip(jsdom ? false : 'jsdom is not installed (see markdown-behaviour.test.js)') };

let calls = [];
let routes = {};

/** A fresh document at `url` + a fetch stub answering from `routes`. */
function setup(url = 'http://127.0.0.1:8765/') {
  const { JSDOM } = jsdom;
  const win = new JSDOM(HTML, { url, runScripts: 'outside-only' }).window;
  globalThis.window = win;
  globalThis.document = win.document;
  globalThis.location = win.location;
  globalThis.history = win.history;
  globalThis.CSS = win.CSS || { escape: (s) => s };
  calls = [];
  routes = {
    'GET /api/tools': () => [200, FIXTURE],
    'GET /api/tools/benchmark/status': () => [200, { id: 'benchmark', enabled: true, kind: 'static', mtime: Date.now() / 1000 - 600, refreshing: false, last_refresh: null }],
    'GET /api/tools/rig-panel/status': () => [200, { id: 'rig-panel', enabled: true, kind: 'url', reachable: true, refreshing: false, last_refresh: null }],
    'GET /api/tools/family-page/status': () => [200, { id: 'family-page', enabled: true, kind: 'static', refreshing: false, last_refresh: null }],
  };
  globalThis.fetch = async (u, opts = {}) => {
    const key = `${(opts.method || 'GET').toUpperCase()} ${u}`;
    calls.push({ key, body: opts.body });
    const h = routes[key];
    const [status, body] = h ? h(opts) : [404, { detail: 'Not Found' }];
    return {
      ok: status >= 200 && status < 300, status, statusText: String(status),
      headers: new win.Headers(), json: async () => body,
    };
  };
  return win;
}

async function freshTools() {
  return await import(`../static/js/tools.js?v=${Math.random()}`);
}

/** Wire tools.js the way main.js does, with recorders instead of the app. */
function wire(mod, over = {}) {
  const rec = { opened: [], closedWith: [], restored: [], sidebar: 0, toasts: [], navigated: [] };
  const state = { decoy: false, selectedBotId: 'main', activeThreadId: 't-1', ...over.state };
  const flags = { harness: true, studioforge: true, mail: true, practice: false, ...over.flags };
  mod.wireTools({
    state,
    openers: {
      harness: () => rec.opened.push('harness'),
      studioforge: () => rec.opened.push('studioforge'),
      mail: () => rec.opened.push('mail'),
      practice: () => rec.opened.push('practice'),
    },
    closeToolPanes: (except) => rec.closedWith.push(except),
    builtinOn: (f) => !state.decoy && !!flags[f],
    builtinDot: (f) => (f === 'harness' ? 'harness-sidedot harness-running' : ''),
    restoreView: (p) => rec.restored.push(p),
    renderSidebar: () => { rec.sidebar += 1; mod.renderToolRail(); },
    isMobile: () => false,
    navigate: (v) => rec.navigated.push(v),
    toast: (m, err) => rec.toasts.push([m, !!err]),
    isBotId: (id) => id === 'main',
  });
  return { rec, state };
}

const tileIds = (win) => [...win.document.querySelectorAll('#tool-list [data-tool]')].map((b) => b.dataset.tool);

test('rail: builtins first (feature-gated), then enabled static/url tools', { skip: dom.skip }, async () => {
  const win = setup();
  const mod = await freshTools();
  wire(mod);
  await mod.loadTools();
  const list = win.document.getElementById('tool-list');
  assert.equal(list.hidden, false);
  // studioforge-panel: enabled:false in the manifest. clients-panel: its
  // feature probe says off. old-report: enabled:false.
  assert.deepEqual(tileIds(win), ['deepseek-harness', 'mail-panel', 'benchmark', 'rig-panel', 'family-page']);
  assert.ok(list.querySelector('.tool-list-head'), 'the group has its heading');
  const bench = list.querySelector('[data-tool="benchmark"]');
  assert.equal(bench.querySelector('.tool-avatar').textContent, '📊');
  assert.equal(bench.getAttribute('aria-label'), 'Benchmark Board');
  // A builtin carries its status dot; a generic tool has none.
  assert.ok(list.querySelector('[data-tool="deepseek-harness"] .bot-status-dot.harness-running'));
  assert.equal(bench.querySelector('.bot-status-dot'), null);
  assert.equal(mod.railToolDot('deepseek-harness').classList.contains('harness-running'), true);
});

test('rail: a backend without /api/tools still shows the builtins', { skip: dom.skip }, async () => {
  const win = setup();
  delete routes['GET /api/tools'];
  const mod = await freshTools();
  wire(mod);
  await mod.loadTools();
  assert.deepEqual(tileIds(win), ['deepseek-harness', 'studioforge-panel', 'mail-panel']);
});

test('Safe Mode: only safe static/url tools, never a builtin', { skip: dom.skip }, async () => {
  const win = setup();
  const mod = await freshTools();
  wire(mod, { state: { decoy: true, selectedBotId: null } });
  await mod.loadTools();
  assert.deepEqual(tileIds(win), ['family-page']);
  // And no refresh control, even if the tool had one.
  mod.openTool('family-page');
  assert.ok(win.document.getElementById('tool-refresh').classList.contains('hidden'));
  // Deep links are unlocked-only.
  win.location.hash = '#tool=family-page';
  assert.equal(mod.openFromHash(), false);
});

test('opening a static tool: full-page, strict sandbox, served path, refresh shown', { skip: dom.skip }, async () => {
  const win = setup();
  const mod = await freshTools();
  const { rec, state } = wire(mod);
  await mod.loadTools();
  win.document.querySelector('#tool-list [data-tool="benchmark"]').click();

  const doc = win.document;
  assert.ok(doc.body.classList.contains('tool-full'), 'body.tool-full must be set');
  assert.equal(doc.getElementById('tool-view').classList.contains('hidden'), false);
  const frame = doc.getElementById('tool-frame');
  assert.equal(frame.getAttribute('src'), '/tools/benchmark/');
  assert.equal(frame.getAttribute('sandbox'), 'allow-scripts allow-forms allow-popups');
  assert.equal(doc.getElementById('tool-title').textContent, 'Benchmark Board');
  assert.equal(doc.getElementById('tool-open').getAttribute('href'), '/tools/benchmark/');
  assert.equal(doc.getElementById('tool-refresh').classList.contains('hidden'), false);
  assert.deepEqual(rec.closedWith, ['tool'], 'the builtin panes are closed first');
  assert.equal(state.selectedBotId, 'benchmark');
  assert.ok(doc.querySelector('#tool-list [data-tool="benchmark"]').classList.contains('active'));
  assert.equal(win.location.hash, '#tool=benchmark');
  // Status lands asynchronously: "updated … ago".
  await new Promise((r) => setTimeout(r, 0));
  await new Promise((r) => setTimeout(r, 0));
  assert.notEqual(doc.getElementById('tool-updated').textContent, '');

  // ✕ restores the selection the tool replaced.
  doc.getElementById('tool-close').click();
  assert.equal(doc.body.classList.contains('tool-full'), false);
  assert.ok(doc.getElementById('tool-view').classList.contains('hidden'));
  assert.equal(frame.hasAttribute('src'), false, 'the frame is unloaded on close');
  assert.deepEqual(rec.restored, [{ botId: 'main', threadId: 't-1' }]);
  assert.equal(win.location.hash, '');
});

test('url tool: loopback address loads only on the host, with allow-same-origin', { skip: dom.skip }, async () => {
  let win = setup('http://192.0.2.10:8765/');
  let mod = await freshTools();
  wire(mod);
  await mod.loadTools();
  mod.openTool('rig-panel');
  let frame = win.document.getElementById('tool-frame');
  assert.ok(frame.classList.contains('hidden'), 'off-host: no frame');
  assert.equal(frame.hasAttribute('src'), false);
  assert.equal(win.document.getElementById('tool-note').classList.contains('hidden'), false);

  win = setup('http://127.0.0.1:8765/');
  mod = await freshTools();
  wire(mod);
  await mod.loadTools();
  mod.openTool('rig-panel');
  frame = win.document.getElementById('tool-frame');
  assert.equal(frame.getAttribute('src'), 'http://127.0.0.1:8080/');
  assert.equal(frame.getAttribute('sandbox'), 'allow-scripts allow-forms allow-popups allow-same-origin');
});

test('builtin tiles call their own opener; selectBot-style ids are tool ids', { skip: dom.skip }, async () => {
  const win = setup();
  const mod = await freshTools();
  const { rec } = wire(mod);
  await mod.loadTools();
  win.document.querySelector('#tool-list [data-tool="deepseek-harness"]').click();
  assert.deepEqual(rec.opened, ['harness']);
  assert.equal(mod.isToolId('mail-panel'), true);
  assert.equal(mod.isToolId('benchmark'), true);
  assert.equal(mod.isToolId('main'), false);
  // A builtin switched off in tools.yaml does not open.
  assert.equal(mod.openTool('studioforge-panel'), false);
});

test('refresh: rc≠0 shows the stderr tail collapsed; rc 0 reloads the frame', { skip: dom.skip }, async () => {
  const win = setup();
  const mod = await freshTools();
  const { rec } = wire(mod);
  await mod.loadTools();
  mod.openTool('benchmark');
  const doc = win.document;
  const tick = () => new Promise((r) => setTimeout(r, 0));

  routes['POST /api/tools/benchmark/refresh'] = () => [200, { rc: 2, seconds: 1.2, stdout_tail: '', stderr_tail: 'Traceback: boom' }];
  doc.getElementById('tool-refresh').click();
  assert.equal(doc.getElementById('tool-refresh').disabled, true, 'disabled while running');
  for (let i = 0; i < 4; i++) await tick();
  const box = doc.getElementById('tool-error');
  assert.equal(box.classList.contains('hidden'), false);
  assert.equal(box.open, false, 'collapsed by default');
  assert.equal(doc.getElementById('tool-error-text').textContent, 'Traceback: boom');
  assert.equal(doc.getElementById('tool-refresh').disabled, false);

  routes['POST /api/tools/benchmark/refresh'] = () => [200, { rc: 0, seconds: 3, stdout_tail: 'ok', stderr_tail: '' }];
  doc.getElementById('tool-refresh').click();
  for (let i = 0; i < 4; i++) await tick();
  assert.ok(box.classList.contains('hidden'), 'a good run clears the old error');
  assert.equal(doc.getElementById('tool-frame').getAttribute('src'), '/tools/benchmark/');
  assert.ok(rec.toasts.some(([, err]) => !err), 'a success toast');
  assert.ok(calls.filter((c) => c.key === 'POST /api/tools/benchmark/refresh').length === 2);
});

test('serializeTools sends only schema fields; builtins only id/kind/enabled', { skip: dom.skip }, async () => {
  setup();
  const mod = await freshTools();
  const out = mod.serializeTools(FIXTURE.tools);
  assert.deepEqual(out[0], { id: 'deepseek-harness', kind: 'builtin', enabled: true });
  assert.deepEqual(out[1], { id: 'studioforge-panel', kind: 'builtin', enabled: false });
  const bench = out.find((x) => x.id === 'benchmark');
  assert.deepEqual(Object.keys(bench).sort(), ['enabled', 'entry', 'icon', 'id', 'kind', 'root', 'safe', 'title']);
  assert.equal('has_refresh' in bench, false);
  assert.equal('builtin_feature' in bench, false);
});

test('Settings → Tools: table, add, toggle, PUT, inline 422', { skip: dom.skip }, async () => {
  const win = setup();
  const mod = await freshTools();
  wire(mod);
  const doc = win.document;
  // Real English strings for this case, so the hint's {path} is checkable.
  const en = JSON.parse(readFileSync(join(STATIC, 'locales', 'en.json'), 'utf8'));
  routes['GET /static/locales/en.json'] = () => [200, en];
  const i18n = await import('../static/js/i18n.js?v=3');
  await i18n.init('en');
  const pane = doc.getElementById('spane-tools');
  await mod.mountToolsSettings(pane);

  const rows = [...pane.querySelectorAll('.tools-row')];
  assert.equal(rows.length, FIXTURE.tools.length);
  // Builtins first, and they have no remove button and no editable title.
  assert.equal(rows[0].querySelector('.tools-remove'), null);
  assert.equal(rows[0].querySelector('input.tools-input'), null);
  const benchRow = rows.find((r) => r.querySelector('.tools-id')?.textContent === 'benchmark');
  assert.ok(benchRow.querySelector('.tools-remove'));
  assert.match(benchRow.querySelector('.tools-loc').textContent, /report\.html/);
  assert.ok(pane.textContent.includes(FIXTURE.path), 'the hint names the manifest path');

  // Add a url tool.
  pane.querySelector('#tools-add').click();
  pane.querySelector('#tools-add-kind').value = 'url';
  pane.querySelector('#tools-add-kind').dispatchEvent(new win.Event('change'));
  pane.querySelector('#tools-add-id').value = 'Bad Id';
  pane.querySelector('#tools-add-title').value = 'Grafana';
  pane.querySelector('.tools-add-form').dispatchEvent(new win.Event('submit', { cancelable: true }));
  assert.equal(doc.getElementById('tools-settings-error').classList.contains('hidden'), false, 'bad id refused inline');
  pane.querySelector('#tools-add-id').value = 'grafana';
  pane.querySelector('#tools-add-url').value = 'https://grafana.example/';
  pane.querySelector('.tools-add-form').dispatchEvent(new win.Event('submit', { cancelable: true }));
  assert.equal(pane.querySelectorAll('.tools-row').length, FIXTURE.tools.length + 1);
  assert.equal(mod.toolsSettingsDirty(), true);

  // A 422 from the server lands inline and marks the row.
  // The backend's shape (backend/app/tools.py _err422).
  routes['PUT /api/tools'] = () => [422, { detail: 'tools[4].root: root does not exist', index: 4, field: 'root', message: 'root does not exist' }];
  assert.equal(await mod.saveToolsSettings(), false);
  const err = doc.getElementById('tools-settings-error');
  assert.equal(err.classList.contains('hidden'), false);
  assert.match(err.textContent, /root does not exist/);
  assert.ok(pane.querySelector('.tools-row.invalid'), 'the offending row is marked');

  // Success: PUT body carries the new tool, the rail re-renders.
  let sent = null;
  routes['PUT /api/tools'] = (opts) => {
    sent = JSON.parse(opts.body);
    return [200, { tools: FIXTURE.tools.concat([{ id: 'grafana', title: 'Grafana', icon: '', kind: 'url', enabled: true, safe: false, url: 'https://grafana.example/', has_refresh: false }]) }];
  };
  assert.equal(await mod.saveToolsSettings(), true);
  assert.ok(sent.tools.some((x) => x.id === 'grafana' && x.url === 'https://grafana.example/' && x.kind === 'url'));
  assert.equal(sent.tools[0].kind, 'builtin', 'builtins first on the wire');
  assert.equal(mod.toolsSettingsDirty(), false);
  assert.ok(tileIds(win).includes('grafana'), 'the rail shows the saved tool');
});
