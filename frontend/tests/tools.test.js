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

/** Wire tools.js the way main.js does, with recorders instead of the app.
 *  `over.mobile` picks the layout: true (the default here) = the phone Bots
 *  page, one tile per tool — what most of these cases inspect; false = the
 *  desktop rail, ONE Tools button with a popup (the "desktop rail" cases). */
function wire(mod, over = {}) {
  const rec = { opened: [], closedWith: [], restored: [], sidebar: 0, toasts: [], navigated: [], threads: [] };
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
    openThread: (id, opts) => rec.threads.push([id, opts]),
    closeToolPanes: (except) => rec.closedWith.push(except),
    builtinOn: (f) => !state.decoy && !!flags[f],
    builtinDot: (f) => (f === 'harness' ? 'harness-sidedot harness-running' : ''),
    restoreView: (p) => rec.restored.push(p),
    renderSidebar: () => { rec.sidebar += 1; mod.renderToolRail(); },
    isMobile: () => (over.mobile === undefined ? true : !!over.mobile),
    groupState: () => (over.groupState === undefined ? 'running' : over.groupState),
    navigate: (v) => rec.navigated.push(v),
    toast: (m, err) => rec.toasts.push([m, !!err]),
    isBotId: (id) => id === 'main',
  });
  return { rec, state };
}

// The phone layout's tiles (wire()'s default).
const tileIds = (win) => [...win.document.querySelectorAll('#tool-list [data-tool]')].map((b) => b.dataset.tool);

test('rail: builtins first (feature-gated), then apps, then enabled static/url tools', { skip: dom.skip }, async () => {
  const win = setup();
  const mod = await freshTools();
  wire(mod);
  await mod.loadTools();
  const list = win.document.getElementById('tool-list');
  assert.equal(list.hidden, false);
  // studioforge-panel: enabled:false in the manifest. clients-panel: its
  // feature probe says off. old-report: enabled:false.
  assert.deepEqual(tileIds(win), ['deepseek-harness', 'mail-panel', 'jobboard', 'benchmark', 'rig-panel', 'family-page']);
  assert.ok(list.querySelector('.tool-list-head'), 'the group has its heading');
  const bench = list.querySelector('[data-tool="benchmark"]');
  // 📊 is a chrome emoji: the tile draws the theme's chart line icon instead.
  assert.ok(bench.querySelector('.tool-avatar.tool-avatar-icon svg.rail-icon'));
  assert.equal(bench.getAttribute('aria-label'), 'Benchmark Board');
  // A builtin carries its status dot; a generic tool has none.
  assert.ok(list.querySelector('[data-tool="deepseek-harness"] .bot-status-dot.harness-running'));
  assert.equal(bench.querySelector('.bot-status-dot'), null);
  assert.equal(mod.railToolDot('deepseek-harness').classList.contains('harness-running'), true);
});

test('tiles: the bot tile\'s shape, the theme\'s line icons, typed emoji kept', { skip: dom.skip }, async () => {
  const win = setup();
  const mod = await freshTools();
  wire(mod, { flags: { practice: true } });
  await mod.loadTools();
  const tile = (id) => win.document.querySelector(`#tool-list [data-tool="${id}"]`);
  for (const id of ['deepseek-harness', 'mail-panel', 'clients-panel', 'jobboard', 'benchmark', 'rig-panel', 'family-page']) {
    const b = tile(id);
    assert.ok(b, id);
    // Same classes a bot tile carries, so every bot rule (size, radius, hover,
    // .active ring, dot corner, Minimal rows, phone grid) applies unchanged.
    assert.ok(b.classList.contains('bot-btn') && b.classList.contains('tool-btn'), id);
    const av = b.querySelector('.bot-avatar.tool-avatar');
    assert.ok(av, id + ' has a bot-shaped tile');
    assert.equal(av.getAttribute('aria-hidden'), 'true');
    // No hard-coded service hues or the old accent tile any more.
    assert.equal(av.className.match(/terminal-avatar|harness-avatar|studioforge-avatar/), null, id);
    assert.ok(b.querySelector('.bot-name-tip') && b.querySelector('.bot-name-label'), id + ' has the tip and label');
  }
  // Builtins: line icons (currentColor SVG), never text. So is an app whose
  // manifest names an icon word (`clipboard` is the contract's own example).
  for (const id of ['deepseek-harness', 'mail-panel', 'clients-panel', 'jobboard']) {
    assert.ok(tile(id).querySelector('.tool-avatar-icon svg.rail-icon'), id);
  }
  assert.equal(mod.toolIconName({ kind: 'app', icon: 'clipboard' }), 'jobs');
  assert.equal(tile('jobboard').getAttribute('aria-label'), 'Job Board', 'an app tile is named by its title');
  assert.equal(tile('jobboard').querySelector('.bot-status-dot'), null, 'an app has no service dot');
  // 🎛️ (with its U+FE0F) is the sliders icon; 🏠 is the person's own emoji.
  assert.ok(tile('rig-panel').querySelector('.tool-avatar-icon svg'));
  const fam = tile('family-page').querySelector('.tool-avatar');
  assert.ok(fam.classList.contains('tool-avatar-glyph'));
  assert.equal(fam.textContent, '🏠');
  // A RAIL_ICONS name works as an icon too; an unknown word is just text.
  assert.equal(mod.toolIconName({ kind: 'static', icon: 'chart' }), 'chart');
  assert.equal(mod.toolIconName({ kind: 'static', icon: 'nope' }), null);
  assert.equal(mod.toolIconName({ kind: 'static', icon: '' }), null);
  assert.equal(mod.toolGlyph({ kind: 'static' }).textContent, '🧩', 'no icon at all → the default glyph');
  // The active tile is marked the way a bot tile is.
  mod.openTool('benchmark');
  assert.ok(tile('benchmark').classList.contains('active'));
  assert.equal(tile('benchmark').getAttribute('aria-current'), 'page');
  // The pane header shows the same glyph as the tile.
  assert.ok(win.document.querySelector('#tool-icon svg.rail-icon'));
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

test('✕ on a generic tool opened over a builtin pane restores the last real chat', { skip: dom.skip }, async () => {
  const win = setup();
  const mod = await freshTools();
  const { rec, state } = wire(mod);
  // The builtin openers are main.js's; mimic one: rememberPrev() BEFORE the
  // selection moves to the builtin's id.
  rec.opened.length = 0;
  const openHarness = () => { mod.rememberPrev(); state.selectedBotId = 'deepseek-harness'; state.activeThreadId = null; };
  await mod.loadTools();
  openHarness();
  assert.equal(mod.openTool('benchmark'), true);
  win.document.getElementById('tool-close').click();
  assert.deepEqual(rec.restored, [{ botId: 'main', threadId: 't-1' }],
    'without rememberPrev the restore target was the builtin (= no bot selected)');

  // Generic → builtin → generic keeps the original chat too: the builtin
  // closing the generic pane with restore:false must not drop it.
  rec.restored.length = 0;
  state.selectedBotId = 'main'; state.activeThreadId = 't-2';
  mod.openTool('benchmark');
  mod.closeTool({ restore: false });          // what closeToolPanes('harness') does
  openHarness();                              // selection is still 'benchmark' → no-op
  mod.openTool('rig-panel');
  win.document.getElementById('tool-close').click();
  assert.deepEqual(rec.restored, [{ botId: 'main', threadId: 't-2' }]);
});

test('closing a tool navigates its frame to about:blank before dropping src', { skip: dom.skip }, async () => {
  const win = setup();
  const mod = await freshTools();
  wire(mod);
  await mod.loadTools();
  mod.openTool('benchmark');
  const frame = win.document.getElementById('tool-frame');
  const seen = [];
  const proto = Object.getPrototypeOf(frame);
  const desc = Object.getOwnPropertyDescriptor(proto, 'src')
    || Object.getOwnPropertyDescriptor(win.HTMLIFrameElement.prototype, 'src');
  Object.defineProperty(frame, 'src', {
    configurable: true,
    get() { return desc.get.call(this); },
    set(v) { seen.push(['set', v]); desc.set.call(this, v); },
  });
  const rm = frame.removeAttribute.bind(frame);
  frame.removeAttribute = (n) => { seen.push(['remove', n]); rm(n); };
  mod.closeTool();
  assert.deepEqual(seen, [['set', 'about:blank'], ['remove', 'src']]);
  assert.equal(frame.hasAttribute('src'), false);
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

// =============================================================================
// Apps (docs/design/2026-09-25-apps.md): a trusted package framed WITHOUT a
// sandbox from /apps/<id>/, talking to the shell only through postMessage.
// =============================================================================

/** A message event as the browser would deliver it to the shell window. */
function appMessage(win, data, { origin = win.location.origin, source } = {}) {
  const src = source === undefined ? win.document.getElementById('tool-frame').contentWindow : source;
  const ev = new win.MessageEvent('message', { data, origin });
  // jsdom validates MessageEventInit.source; define it directly instead.
  Object.defineProperty(ev, 'source', { value: src });
  win.dispatchEvent(ev);
}

test('an app opens full-page in the tool pane, framed from /apps/<id>/ with NO sandbox', { skip: dom.skip }, async () => {
  const win = setup();
  const mod = await freshTools();
  const { rec, state } = wire(mod);
  await mod.loadTools();
  win.document.querySelector('#tool-list [data-tool="jobboard"]').click();
  const doc = win.document;
  const frame = doc.getElementById('tool-frame');
  assert.ok(doc.body.classList.contains('tool-full'));
  assert.equal(frame.getAttribute('src'), '/apps/jobboard/');
  assert.equal(frame.hasAttribute('sandbox'), false, 'an app frame has no sandbox attribute at all');
  assert.equal(doc.getElementById('tool-title').textContent, 'Job Board');
  assert.ok(doc.getElementById('tool-refresh').classList.contains('hidden'), 'apps have no refresh');
  assert.equal(win.location.hash, '#tool=jobboard', 'deep link');
  assert.equal(state.selectedBotId, 'jobboard');
  assert.deepEqual(rec.closedWith, ['tool']);

  // The next tool that is NOT an app gets its sandbox back before its src.
  mod.openTool('benchmark');
  assert.equal(frame.getAttribute('sandbox'), 'allow-scripts allow-forms allow-popups');
  assert.equal(frame.getAttribute('src'), '/tools/benchmark/');

  // Deep link straight to the app.
  mod.closeTool();
  win.location.hash = '#tool=jobboard';
  assert.equal(mod.openFromHash(), true);
  assert.equal(frame.getAttribute('src'), '/apps/jobboard/');
  assert.equal(frame.hasAttribute('sandbox'), false);
});

test('an app entry that points outside /apps/<id>/ is ignored', { skip: dom.skip }, async () => {
  const win = setup();
  routes['GET /api/tools'] = () => [200, { ...FIXTURE, tools: FIXTURE.tools.map((x) => (x.id === 'jobboard' ? { ...x, entry: 'https://evil.example/' } : x)) }];
  const mod = await freshTools();
  wire(mod);
  await mod.loadTools();
  mod.openTool('jobboard');
  assert.equal(win.document.getElementById('tool-frame').getAttribute('src'), '/apps/jobboard/',
    'an unsandboxed frame only ever holds our own origin');
});

test('Safe Mode: a non-safe app has no tile, no pane and owns no bot', { skip: dom.skip }, async () => {
  const win = setup();
  const mod = await freshTools();
  wire(mod, { state: { decoy: true, selectedBotId: null } });
  await mod.loadTools();
  assert.equal(tileIds(win).includes('jobboard'), false);
  assert.equal(mod.openTool('jobboard'), false);
  assert.equal(mod.appForBot('jobboard'), null);
});

test('appForBot: the enabled app that owns a roster bot (case-insensitive)', { skip: dom.skip }, async () => {
  setup();
  const mod = await freshTools();
  wire(mod);
  await mod.loadTools();
  assert.equal(mod.appForBot('jobboard').id, 'jobboard');
  assert.equal(mod.appForBot('JobBoard').thread_hook, '/apps/jobboard/thread.js');
  assert.equal(mod.appForBot('main'), null);
  routes['GET /api/tools'] = () => [200, { ...FIXTURE, tools: FIXTURE.tools.map((x) => (x.id === 'jobboard' ? { ...x, enabled: false } : x)) }];
  await mod.loadTools();
  assert.equal(mod.appForBot('jobboard'), null, 'a disabled app hooks nothing');
  assert.equal(mod.openTool('jobboard'), false);
});

test('app messages: only our origin AND the frame on screen are obeyed', { skip: dom.skip }, async () => {
  const win = setup();
  const mod = await freshTools();
  const { rec } = wire(mod);
  await mod.loadTools();
  mod.openTool('jobboard');
  const frame = win.document.getElementById('tool-frame');

  // Wrong origin: ignored.
  appMessage(win, { type: 'dispatch:toast', text: 'x' }, { origin: 'http://evil.example' });
  // Right origin, wrong source (some other window): ignored.
  appMessage(win, { type: 'dispatch:toast', text: 'y' }, { source: win });
  // No source at all: ignored.
  appMessage(win, { type: 'dispatch:close' }, { source: null });
  assert.deepEqual(rec.toasts, []);
  assert.equal(mod.toolOpen(), 'jobboard');

  // The real thing.
  appMessage(win, { type: 'dispatch:toast', text: 'Saved', error: false });
  appMessage(win, { type: 'dispatch:toast', text: 'Broke', error: true });
  assert.deepEqual(rec.toasts, [['Saved', false], ['Broke', true]]);
  appMessage(win, { type: 'dispatch:set-title', text: 'Job Board — March' });
  assert.equal(win.document.getElementById('tool-title').textContent, 'Job Board — March');

  // open-thread: close the pane (restoring the chat it covered), then open
  // the thread with the app's bot and the hint for its hook.
  appMessage(win, { type: 'dispatch:open-thread', threadId: 'thr-2026-09', hint: { job_id: 'j-1' } });
  assert.equal(mod.toolOpen(), null, 'the app pane is closed');
  assert.equal(win.document.body.classList.contains('tool-full'), false);
  assert.deepEqual(rec.restored, [{ botId: 'main', threadId: 't-1' }], 'rememberPrev semantics');
  assert.deepEqual(rec.threads, [['thr-2026-09', { botId: 'jobboard', hint: { job_id: 'j-1' }, appId: 'jobboard' }]]);
  assert.equal(frame.hasAttribute('src'), false, 'the app document is unloaded');

  // With the pane closed, a late message from that frame does nothing.
  appMessage(win, { type: 'dispatch:open-thread', threadId: 'thr-x' });
  assert.equal(rec.threads.length, 1);

  // A malformed thread id is refused; `close` closes.
  mod.openTool('jobboard');
  appMessage(win, { type: 'dispatch:open-thread', threadId: '../../etc' });
  assert.equal(rec.threads.length, 1);
  assert.equal(mod.toolOpen(), 'jobboard');
  appMessage(win, { type: 'dispatch:close' });
  assert.equal(mod.toolOpen(), null);
});

test('a static tool cannot drive the shell through the app channel', { skip: dom.skip }, async () => {
  const win = setup();
  const mod = await freshTools();
  const { rec } = wire(mod);
  await mod.loadTools();
  mod.openTool('benchmark');
  appMessage(win, { type: 'dispatch:open-thread', threadId: 't-9' });
  appMessage(win, { type: 'dispatch:close' });
  assert.deepEqual(rec.threads, []);
  assert.equal(mod.toolOpen(), 'benchmark');
});

test('the shell posts theme + language to the app, and forwards only its own frames', { skip: dom.skip }, async () => {
  const win = setup();
  const mod = await freshTools();
  wire(mod);
  await mod.loadTools();
  win.document.documentElement.setAttribute('data-palette', 'paper');
  win.document.documentElement.setAttribute('data-theme', 'light');
  win.document.documentElement.setAttribute('lang', 'de');
  mod.openTool('jobboard');
  const frame = win.document.getElementById('tool-frame');
  const posted = [];
  frame.contentWindow.postMessage = (msg, origin) => posted.push([msg, origin]);
  mod.syncAppFrame();
  assert.deepEqual(posted.map(([m]) => m.type), ['dispatch:theme', 'dispatch:lang']);
  assert.equal(posted[0][0].palette, 'paper');
  assert.equal(posted[0][0].theme, 'light');
  assert.equal(posted[1][0].lang, 'de');
  assert.ok(posted.every(([, o]) => o === win.location.origin), 'targetOrigin is always our own');

  posted.length = 0;
  assert.equal(mod.forwardAppFrame({ type: 'app:jobboard:job_updated', job: { job_id: 'j-1' } }), true);
  assert.equal(mod.forwardAppFrame({ type: 'app:other:thing' }), false, 'another app’s frame is not ours to see');
  assert.equal(mod.forwardAppFrame({ type: 'message' }), false);
  assert.deepEqual(posted.map(([m]) => m.type), ['dispatch:frame']);
  assert.equal(posted[0][0].frame.type, 'app:jobboard:job_updated');

  // Nothing is posted to a static tool's frame.
  mod.openTool('benchmark');
  frame.contentWindow.postMessage = () => { throw new Error('must not post to a static tool'); };
  mod.syncAppFrame();
  assert.equal(mod.forwardAppFrame({ type: 'app:jobboard:job_updated' }), false);
});

test('Settings → Tools: an app is a row with a switch, no remove, no editable fields', { skip: dom.skip }, async () => {
  const win = setup();
  const mod = await freshTools();
  wire(mod);
  const pane = win.document.getElementById('spane-tools');
  await mod.mountToolsSettings(pane);
  const rows = [...pane.querySelectorAll('.tools-row')];
  const appRow = rows.find((r) => r.querySelector('.tools-id')?.textContent === 'jobboard');
  assert.ok(appRow, 'the app has a row');
  assert.ok(rows.indexOf(appRow) > 0 && rows.indexOf(appRow) < rows.findIndex((r) => r.querySelector('.tools-id')?.textContent === 'benchmark'),
    'apps sit between the builtins and the static/url tools');
  assert.equal(appRow.querySelector('.tools-remove'), null, 'the package is on disk: no remove');
  assert.equal(appRow.querySelector('input.tools-input'), null, 'title/icon come from app.yaml');
  assert.ok(appRow.querySelector('input[type="checkbox"]'), 'the enabled switch');
  assert.match(appRow.querySelector('.tools-loc').textContent, /^\/apps\/jobboard\/$/);
  // What goes over the wire for an app: id/kind/enabled (+ trusted if set).
  const out = mod.serializeTools([{ ...FIXTURE.tools.find((x) => x.id === 'jobboard'), enabled: false }]);
  assert.deepEqual(out, [{ id: 'jobboard', kind: 'app', enabled: false }]);
  assert.deepEqual(mod.serializeTools([{ id: 'addon', kind: 'app', enabled: true, trusted: true, title: 'x', bot_id: 'y' }]),
    [{ id: 'addon', kind: 'app', enabled: true, trusted: true }]);
});

test('an app whose backend did not mount says so in the pane', { skip: dom.skip }, async () => {
  const win = setup();
  routes['GET /api/tools/jobboard/status'] = () => [200, { id: 'jobboard', kind: 'app', enabled: true, mounted: false }];
  const mod = await freshTools();
  wire(mod);
  await mod.loadTools();
  mod.openTool('jobboard');
  await new Promise((r) => setTimeout(r, 0));
  await new Promise((r) => setTimeout(r, 0));
  assert.equal(win.document.getElementById('tool-error').classList.contains('hidden'), false);
});

// =============================================================================
// Desktop rail: ONE Tools button + a popup menu (the phone keeps the tiles).
// =============================================================================

const menuRows = (win) => [...win.document.querySelectorAll('#tools-menu [role="menuitem"]')];
const rowKeys = (win) => menuRows(win).map((r) => r.dataset.tool || ('bot:' + r.dataset.bot));
const key = (win, target, k) => target.dispatchEvent(new win.KeyboardEvent('keydown', { key: k, bubbles: true, cancelable: true }));
const pointerClick = (win, target) => target.dispatchEvent(new win.MouseEvent('click', { bubbles: true, cancelable: true, detail: 1 }));

test('desktop rail: one Tools button, no tiles; its popup lists every enabled tool', { skip: dom.skip }, async () => {
  const win = setup();
  const mod = await freshTools();
  wire(mod, { mobile: false });
  await mod.loadTools();
  const doc = win.document;
  const list = doc.getElementById('tool-list');
  assert.equal(list.hidden, false);
  assert.ok(list.classList.contains('tool-list-compact'));
  assert.deepEqual(tileIds(win), [], 'no per-tool tiles on the desktop rail');
  assert.equal(list.querySelector('.tool-list-head'), null, 'the button names itself — no heading');
  const btns = list.querySelectorAll('button');
  assert.equal(btns.length, 1, 'exactly one rail button');
  const btn = btns[0];
  assert.equal(btn.id, 'tools-menu-btn');
  assert.ok(btn.classList.contains('bot-btn') && btn.classList.contains('tool-btn'), 'bot-tile geometry');
  assert.equal(btn.getAttribute('aria-haspopup'), 'menu');
  assert.equal(btn.getAttribute('aria-controls'), 'tools-menu');
  assert.equal(btn.getAttribute('aria-expanded'), 'false');
  assert.equal(btn.getAttribute('aria-label'), 'Tools — open the tools menu');
  assert.equal(btn.querySelector('.bot-name-tip').textContent, 'Tools');
  assert.ok(btn.querySelector('.tool-avatar-icon svg.rail-icon'), 'a line icon, not a glyph');
  assert.ok(btn.querySelector('.bot-status-dot.tools-sidedot.running'), 'worst-of dot from groupState');
  const menu = doc.getElementById('tools-menu');
  assert.equal(menu.hidden, true);

  pointerClick(win, btn);
  assert.equal(menu.hidden, false);
  assert.equal(btn.getAttribute('aria-expanded'), 'true');
  assert.equal(menu.getAttribute('role'), 'menu');
  assert.deepEqual(rowKeys(win), ['deepseek-harness', 'mail-panel', 'jobboard', 'benchmark', 'rig-panel', 'family-page']);
  for (const r of menuRows(win)) {
    assert.ok(r.querySelector('.tool-avatar'), r.dataset.tool + ' has its icon');
    assert.ok(r.querySelector('.tools-item-label').textContent, r.dataset.tool + ' has its title');
  }
  assert.ok(doc.querySelector('#tools-menu [data-tool="deepseek-harness"] .bot-status-dot.harness-running'), 'builtin row dot');
  assert.equal(doc.querySelector('#tools-menu [data-tool="benchmark"] .bot-status-dot'), null);
  assert.equal(mod.railToolDot('deepseek-harness').classList.contains('harness-running'), true, 'main.js can patch the row dot');
  assert.equal(doc.getElementById('tools-bots-sep').hidden, true, 'no parked bots → no rule');
  // Positioned through the CSSOM, never a style="" attribute string built by hand.
  assert.match(menu.style.left, /px$/);

  // A second click on the button closes it.
  pointerClick(win, btn);
  assert.equal(menu.hidden, true);
  assert.equal(btn.getAttribute('aria-expanded'), 'false');
});

test('desktop rail: a row opens its tool and closes the popup; the button is active while it is open', { skip: dom.skip }, async () => {
  const win = setup();
  const mod = await freshTools();
  const { rec, state } = wire(mod, { mobile: false });
  await mod.loadTools();
  const doc = win.document;
  pointerClick(win, doc.getElementById('tools-menu-btn'));
  doc.querySelector('#tools-menu [data-tool="deepseek-harness"]').click();
  assert.deepEqual(rec.opened, ['harness'], 'a builtin row calls its own opener');
  assert.equal(doc.getElementById('tools-menu').hidden, true);

  pointerClick(win, doc.getElementById('tools-menu-btn'));
  doc.querySelector('#tools-menu [data-tool="benchmark"]').click();
  assert.equal(state.selectedBotId, 'benchmark');
  assert.ok(doc.body.classList.contains('tool-full'));
  assert.equal(doc.getElementById('tools-menu').hidden, true);
  assert.ok(doc.getElementById('tools-menu-btn').classList.contains('active'), 'active while a tool pane is open');
  pointerClick(win, doc.getElementById('tools-menu-btn'));
  assert.ok(doc.querySelector('#tools-menu [data-tool="benchmark"]').classList.contains('active'));
  doc.getElementById('tool-close').click();
  // main.js's restoreView puts the chat back and repaints the rail; mimic it.
  state.selectedBotId = 'main';
  mod.renderToolRail();
  assert.equal(doc.getElementById('tools-menu-btn').classList.contains('active'), false);
});

test('desktop rail: parked bots follow the tools under a separator', { skip: dom.skip }, async () => {
  const win = setup();
  const mod = await freshTools();
  const opened = [];
  wire(mod, { mobile: false });
  mod.setParkedBots([
    { id: 'parked-1', name: 'Parked Pal', avatar: () => win.document.createElement('span'), dot: 'unread', active: false, open: () => opened.push('parked-1') },
  ]);
  await mod.loadTools();
  const doc = win.document;
  pointerClick(win, doc.getElementById('tools-menu-btn'));
  assert.deepEqual(rowKeys(win).slice(-2), ['family-page', 'bot:parked-1'], 'bots come after the tools');
  assert.equal(doc.getElementById('tools-bots-sep').hidden, false);
  assert.ok(doc.querySelector('#tools-bots [data-bot="parked-1"] .bot-status-dot.unread'));
  doc.querySelector('#tools-bots [data-bot="parked-1"]').click();
  assert.deepEqual(opened, ['parked-1']);
  assert.equal(doc.getElementById('tools-menu').hidden, true);
});

test('desktop rail: keyboard — Enter/arrows open, arrows move, Esc closes and returns focus', { skip: dom.skip }, async () => {
  const win = setup();
  const mod = await freshTools();
  wire(mod, { mobile: false });
  await mod.loadTools();
  const doc = win.document;
  const btn = doc.getElementById('tools-menu-btn');
  btn.focus();
  btn.click();                      // keyboard activation: detail 0 → first row
  assert.equal(doc.getElementById('tools-menu').hidden, false);
  const rows = menuRows(win);
  assert.equal(doc.activeElement, rows[0]);
  assert.ok(rows.every((r) => r.getAttribute('tabindex') === '-1'), 'roving focus, rows out of the tab order');
  key(win, rows[0], 'ArrowDown');
  assert.equal(doc.activeElement, rows[1]);
  key(win, rows[1], 'ArrowUp');
  key(win, rows[0], 'ArrowUp');
  assert.equal(doc.activeElement, rows[rows.length - 1], 'ArrowUp wraps to the last row');
  key(win, doc.activeElement, 'Home');
  assert.equal(doc.activeElement, rows[0]);
  key(win, doc.activeElement, 'Escape');
  assert.equal(doc.getElementById('tools-menu').hidden, true);
  assert.equal(doc.activeElement, doc.getElementById('tools-menu-btn'), 'focus back on the button');
  // Opened by a pointer, focus stays on the button — Escape still closes.
  pointerClick(win, doc.getElementById('tools-menu-btn'));
  assert.equal(doc.getElementById('tools-menu').hidden, false);
  key(win, doc.getElementById('tools-menu-btn'), 'Escape');
  assert.equal(doc.getElementById('tools-menu').hidden, true);
  // ArrowUp on the button opens on the last row.
  key(win, doc.getElementById('tools-menu-btn'), 'ArrowUp');
  assert.equal(doc.getElementById('tools-menu').hidden, false);
  assert.equal(doc.activeElement.dataset.tool, 'family-page');
});

test('desktop rail: a click outside closes the popup; a repaint keeps it open', { skip: dom.skip }, async () => {
  const win = setup();
  const mod = await freshTools();
  wire(mod, { mobile: false });
  await mod.loadTools();
  const doc = win.document;
  pointerClick(win, doc.getElementById('tools-menu-btn'));
  mod.renderToolRail();             // every sidebar repaint does this
  assert.equal(doc.getElementById('tools-menu').hidden, false, 'survives a repaint');
  assert.equal(doc.getElementById('tools-menu-btn').getAttribute('aria-expanded'), 'true');
  pointerClick(win, doc.querySelector('#tools-menu [data-tool="benchmark"]').parentElement);   // inside: stays
  assert.equal(doc.getElementById('tools-menu').hidden, false);
  pointerClick(win, doc.getElementById('threads'));
  assert.equal(doc.getElementById('tools-menu').hidden, true);
  assert.equal(doc.getElementById('tools-menu-btn').getAttribute('aria-expanded'), 'false');
});

test('desktop rail: the dot is worst-of, and the button hides with nothing to show', { skip: dom.skip }, async () => {
  let win = setup();
  let mod = await freshTools();
  wire(mod, { mobile: false, groupState: 'error' });
  await mod.loadTools();
  assert.ok(win.document.querySelector('#tools-menu-btn .bot-status-dot.tools-sidedot.error'));

  // Safe Mode: parked bots are ignored and only safe tools count.
  win = setup();
  routes['GET /api/tools'] = () => [200, { tools: [] }];
  mod = await freshTools();
  wire(mod, { mobile: false, state: { decoy: true, selectedBotId: null } });
  mod.setParkedBots([{ id: 'parked-1', name: 'Parked Pal', dot: '', open: () => {} }]);
  await mod.loadTools();
  assert.equal(win.document.getElementById('tool-list').hidden, true);
  assert.equal(win.document.getElementById('tools-menu-btn'), null);
});

test('phone Bots page: tiles stay; parked bots get one "More bots" tile with only their rows', { skip: dom.skip }, async () => {
  const win = setup();
  const mod = await freshTools();
  wire(mod, { mobile: true });
  mod.setParkedBots([{ id: 'parked-1', name: 'Parked Pal', dot: '', open: () => {} }]);
  await mod.loadTools();
  const doc = win.document;
  const list = doc.getElementById('tool-list');
  assert.equal(list.classList.contains('tool-list-compact'), false);
  assert.ok(list.querySelector('.tool-list-head'));
  assert.deepEqual(tileIds(win), ['deepseek-harness', 'mail-panel', 'jobboard', 'benchmark', 'rig-panel', 'family-page']);
  const more = doc.getElementById('tools-menu-btn');
  assert.equal(more.dataset.menu, 'bots');
  assert.equal(more.querySelector('.bot-name-label').textContent, 'More bots');
  pointerClick(win, more);
  assert.deepEqual(rowKeys(win), ['bot:parked-1'], 'no tool rows on the phone — the tiles are right there');
  assert.equal(doc.getElementById('tools-bots-sep').hidden, true);
});

test('crossing the breakpoint re-lays the rail out (tiles ⇄ one button)', { skip: dom.skip }, async () => {
  const win = setup();
  const mod = await freshTools();
  let mobile = false;
  // Wired by hand: this case needs a viewport it can switch.
  const d = {
    state: { decoy: false, selectedBotId: 'main', activeThreadId: 't-1' },
    openers: {}, closeToolPanes: () => {}, builtinOn: () => true, builtinDot: () => '',
    groupState: () => '', restoreView: () => {}, renderSidebar: () => mod.renderToolRail(),
    isMobile: () => mobile, navigate: () => {}, toast: () => {}, isBotId: () => false,
  };
  mod.wireTools(d);
  await mod.loadTools();
  assert.ok(win.document.getElementById('tools-menu-btn'));
  pointerClick(win, win.document.getElementById('tools-menu-btn'));
  mobile = true;
  win.dispatchEvent(new win.Event('resize'));
  assert.equal(win.document.getElementById('tools-menu').hidden, true, 'a resize closes the popup');
  assert.ok(tileIds(win).length > 0, 'phone: tiles again');
  mobile = false;
  win.dispatchEvent(new win.Event('resize'));
  assert.deepEqual(tileIds(win), []);
});
