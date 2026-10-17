// The extension loader (js/extensions.js) and the shell an extension is
// handed (js/ext-sdk.js) — CONTRACTS §E.1.
//
// The loader is pure: every effect is a dependency, so its state machine is
// driven here with a fake fetch and a fake import — the order (locales → css →
// import → handshake → mount), lock → unmount, an incompatible add-on, a
// failed import, a lock that lands mid-mount. The DOM half — pane ownership
// in both orders, header slot, shell teardown — runs under jsdom.

import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
import { domSkip } from './_require-dom.js';
import { createLoader, createPaneHost, createAddinTabs, ADDIN_TAB_ID } from '../static/js/extensions.js';

const require = createRequire(import.meta.url);
let jsdom = null;
try { jsdom = require('jsdom'); } catch { /* not installed — DOM tests skip */ }
const dom = { skip: domSkip(jsdom ? false : 'jsdom is not installed') };

const VOICE = {
  id: 'voice', name: 'DisPatch Voice', version: '1.0.0', api: [1, 1], state: 'ok',
  source: 'entry point (dispatch-voice 1.0.0)', assets_version: 'abc123', entry: 'index.js',
  css: ['voice.css'], locales: true, tools: ['voice-lab'],
};

function harness({ list = [VOICE], features = { extensions: 1 }, modules = {}, locked = false } = {}) {
  const log = [];
  const st = { locked, features, lang: 'de', handles: [] };
  const deps = {
    EXT_API: 1,
    async fetchJson(path) {
      log.push(`fetch ${path}`);
      if (path === '/api/extensions') return { extensions: list, mode: 'entry_points', api: 1 };
      if (path.includes('/locales/en.json')) return { drive: { title: 'Voice chat' } };
      if (path.includes('/locales/de.json')) return { drive: { title: 'Sprachchat' } };
      throw Object.assign(new Error('404'), { status: 404 });
    },
    async importModule(url) {
      log.push(`import ${url}`);
      const m = modules[url.split('?')[0]];
      if (!m) throw new Error(`Failed to fetch dynamically imported module: ${url}`);
      return typeof m === 'function' ? m() : m;
    },
    async loadStyle(href) { log.push(`css ${href}`); },
    mergeBundle(id, b) { log.push(`merge ${id} ${b.lang} ${b.dict && b.dict.drive.title} ${b.en && b.en.drive.title}`); },
    removeBundle(id) { log.push(`unmerge ${id}`); },
    lang: () => st.lang,
    features: () => st.features,
    isLocked: () => st.locked,
    shellFor: (id) => ({ id, marker: 'shell' }),
    disposeExtension(id) { log.push(`dispose ${id}`); },
    emitLocale(l) { log.push(`emitLocale ${l}`); },
    onChange() { log.push('changed'); },
  };
  return { deps, log, st, loader: createLoader(deps) };
}

function voiceModule(extra = {}) {
  const calls = [];
  return {
    calls,
    mod: {
      EXT: { id: 'voice', api: [1, 1], version: '1.0.0' },
      async mount(shell, opts) {
        calls.push(['mount', shell.id, opts.assetsVersion]);
        return { unmount() { calls.push(['unmount']); }, onLocale(l) { calls.push(['onLocale', l]); } };
      },
      ...extra,
    },
  };
}

test('mount order: locales → css → import → handshake → mount, with the assets hash', async () => {
  const v = voiceModule();
  const h = harness({ modules: { '/ext/voice/index.js': v.mod } });
  await h.loader.refresh();
  const steps = h.log.filter((l) => !l.startsWith('changed'));
  assert.deepEqual(steps, [
    'fetch /api/extensions',
    'fetch /ext/voice/locales/en.json?v=abc123',
    'fetch /ext/voice/locales/de.json?v=abc123',
    'merge voice de Sprachchat Voice chat',
    'css /ext/voice/voice.css?v=abc123',
    'import /ext/voice/index.js?v=abc123',
  ]);
  assert.deepEqual(v.calls, [['mount', 'voice', 'abc123']]);
  assert.ok(h.loader.isMounted('voice'));
  assert.equal(h.loader.snapshot().client.get('voice').state, 'mounted');
  // Idempotent: a second refresh fetches the list again but never remounts.
  await h.loader.refresh();
  assert.equal(v.calls.length, 1);
});

test('nothing is requested when the status says no extension is ok', async () => {
  const h = harness({ features: { extensions: 0 } });
  await h.loader.refresh();
  assert.ok(!h.log.some((l) => l.startsWith('fetch')), 'a fresh install costs no request');
  assert.deepEqual(h.loader.snapshot().list, []);
  // …but the Voice tab (force) still reads the states (failed / disabled / …).
  await h.loader.refresh({ force: true });
  assert.ok(h.log.includes('fetch /api/extensions'));
  // Later auth refreshes (applyAuthChrome → refresh) stay request-free.
  const fetches = () => h.log.filter((l) => l === 'fetch /api/extensions').length;
  await h.loader.refresh();
  await h.loader.refresh();
  assert.equal(fetches(), 1, 'an unforced refresh after a forced read must not fetch');
});

test('a locked device mounts the add-on too, told it is locked (2.3.4)', async () => {
  const seen = [];
  const v = voiceModule({
    async mount(shell, opts) { seen.push(opts); return { unmount() { seen.push('unmount'); } }; },
  });
  const h = harness({ modules: { '/ext/voice/index.js': v.mod }, locked: true });
  await h.loader.refresh();
  assert.ok(h.log.includes('fetch /api/extensions'));
  assert.ok(h.loader.isMounted('voice'));
  assert.deepEqual(seen, [{ assetsVersion: 'abc123', locked: true }]);
  assert.equal(h.loader.tier(), 'locked');
});

test('crossing the lock boundary remounts in the new tier', async () => {
  const seen = [];
  const v = voiceModule({
    async mount(shell, opts) { seen.push(opts.locked); return { unmount() { seen.push('unmount'); } }; },
  });
  const h = harness({ modules: { '/ext/voice/index.js': v.mod } });
  await h.loader.refresh();
  assert.equal(h.loader.tier(), 'unlocked');
  h.st.locked = true;
  await h.loader.refresh();
  assert.deepEqual(seen, [false, 'unmount', true]);
  h.st.locked = false;
  await h.loader.refresh();
  assert.deepEqual(seen, [false, 'unmount', true, 'unmount', false]);
});

test('lock → unmount, dispose everything it registered, drop its strings', async () => {
  const v = voiceModule();
  const h = harness({ modules: { '/ext/voice/index.js': v.mod } });
  await h.loader.refresh();
  h.st.locked = true;
  h.loader.unmountAll();
  assert.deepEqual(v.calls.at(-1), ['unmount']);
  assert.ok(h.log.includes('dispose voice') && h.log.includes('unmerge voice'));
  assert.ok(!h.loader.isMounted('voice'));
  assert.equal(h.loader.snapshot().list, null);
});

test('a lock that lands while the module is loading wins: nothing stays mounted', async () => {
  let release;
  const gate = new Promise((r) => { release = r; });
  const v = voiceModule();
  const h = harness({ modules: { '/ext/voice/index.js': async () => { await gate; return v.mod; } } });
  const p = h.loader.refresh();
  await new Promise((r) => setTimeout(r, 5));
  h.st.locked = true;
  h.loader.unmountAll();
  release();
  await p;
  assert.deepEqual(v.calls, [], 'mount() never ran');
  assert.ok(!h.loader.isMounted('voice'));
});

test('an incompatible add-on (JS handshake) is not mounted and says why', async () => {
  const v = voiceModule({ EXT: { id: 'voice', api: [2, 3], version: '3.0.0' } });
  const h = harness({ modules: { '/ext/voice/index.js': v.mod } });
  await h.loader.refresh();
  assert.deepEqual(v.calls, []);
  const c = h.loader.snapshot().client.get('voice');
  assert.equal(c.state, 'incompatible');
  assert.match(c.detail, /2\.\.3; this DisPatch has 1/);
  assert.ok(h.log.includes('unmerge voice'), 'its strings are dropped again');
});

test('a module claiming another id is refused by the handshake', async () => {
  const v = voiceModule({ EXT: { id: 'other', api: [1, 1] } });
  const h = harness({ modules: { '/ext/voice/index.js': v.mod } });
  await h.loader.refresh();
  assert.equal(h.loader.snapshot().client.get('voice').state, 'incompatible');
});

test('a failed import or a throwing mount is `failed`, never fatal, and torn down', async () => {
  const h = harness({ modules: {} });
  await h.loader.refresh();
  const c = h.loader.snapshot().client.get('voice');
  assert.equal(c.state, 'failed');
  assert.match(c.detail, /dynamically imported module/);
  assert.ok(h.log.includes('dispose voice'));
  const bad = harness({ modules: { '/ext/voice/index.js': { EXT: { id: 'voice', api: [1, 1] },
    async mount() { throw new Error('mic init broke'); } } } });
  await bad.loader.refresh();
  assert.equal(bad.loader.snapshot().client.get('voice').detail, 'mic init broke');
  // A failed extension is not retried on every refresh — only when asked.
  const before = bad.log.filter((l) => l.startsWith('import')).length;
  await bad.loader.refresh();
  assert.equal(bad.log.filter((l) => l.startsWith('import')).length, before);
  await bad.loader.refresh({ force: true });
  assert.equal(bad.log.filter((l) => l.startsWith('import')).length, before + 1);
});

test('extensions that are not ok are listed but never imported', async () => {
  const list = [{ ...VOICE, state: 'failed', entry: null, detail: 'ModuleNotFoundError: onnxruntime' },
    { id: 'other', state: 'disabled' }];
  const h = harness({ list });
  await h.loader.refresh({ force: true });
  assert.ok(!h.log.some((l) => l.startsWith('import')));
  assert.equal(h.loader.snapshot().list.length, 2);
});

test('language change: bundles re-merged first, then the add-on is told', async () => {
  const v = voiceModule();
  const h = harness({ modules: { '/ext/voice/index.js': v.mod } });
  await h.loader.refresh();
  h.st.lang = 'en';
  h.log.length = 0;
  await h.loader.onLanguage('en');
  assert.deepEqual(h.log, ['fetch /ext/voice/locales/en.json?v=abc123',
    'merge voice en Voice chat Voice chat', 'emitLocale en']);
  assert.deepEqual(v.calls.at(-1), ['onLocale', 'en']);
});

// --------------------------------------------------------------------------
// DOM: pane ownership (A16) in both orders, and the shell's teardown.
// --------------------------------------------------------------------------

function withDom(fn) {
  return async () => {
    const win = new jsdom.JSDOM('<!doctype html><html><body><div id="pane"></div><span id="slot" hidden></span></body></html>').window;
    const keep = { document: globalThis.document, window: globalThis.window, Node: globalThis.Node, HTMLElement: globalThis.HTMLElement };
    globalThis.document = win.document;
    globalThis.window = win;
    globalThis.Node = win.Node;
    globalThis.HTMLElement = win.HTMLElement;
    try { await fn(win); } finally { Object.assign(globalThis, keep); }
  };
}

test('pane host: absent page until claimed; dispose gives it back', dom, withDom(async (win) => {
  const root = win.document.getElementById('pane');
  let absent = 0;
  const host = createPaneHost(root, (r) => { absent += 1; r.textContent = 'ABSENT'; });
  host.show();
  assert.equal(root.textContent, 'ABSENT');
  const seen = [];
  const dispose = host.claim('voice', { render: (r) => { r.textContent = 'ADDON'; }, onShow: () => seen.push('show'), onHide: () => seen.push('hide') });
  assert.equal(root.textContent, 'ADDON');
  assert.deepEqual(seen, ['show'], 'the tab is on screen, so the add-on hears onShow');
  host.showAbsent();                       // an onChange while it owns the pane
  assert.equal(root.textContent, 'ADDON', 'the absent renderer never overwrites the owner');
  dispose();
  assert.equal(root.textContent, 'ABSENT');
  assert.deepEqual(seen, ['show', 'hide']);
  assert.throws(() => host.claim('voice', {}), /render/);
  assert.ok(absent >= 2);
}));

async function fullStack(win, modBody) {
  const sdk = await import(`../static/js/ext-sdk.js?t=${Math.random()}`);
  const root = win.document.getElementById('pane');
  const host = createPaneHost(root, (r) => { r.textContent = 'ABSENT'; });
  sdk.installShell({ settingsPane: (owner, id, spec) => host.claim(owner, spec),
    addHeaderAction: (owner, node) => { win.document.getElementById('slot').append(node); return () => node.remove(); } });
  const deps = {
    EXT_API: sdk.EXT_API,
    fetchJson: async (p) => (p === '/api/extensions' ? { extensions: [{ ...VOICE, locales: false, css: [] }] } : {}),
    importModule: async () => modBody(sdk),
    loadStyle: async () => {}, mergeBundle() {}, removeBundle() {},
    lang: () => 'en', features: () => ({ extensions: 1 }), isLocked: () => false,
    shellFor: (id) => sdk.createShell(id), disposeExtension: sdk.disposeExtension,
    emitLocale: sdk.emitLocale, onChange: () => host.showAbsent(),
  };
  host.show();
  return { sdk, root, host, loader: createLoader(deps) };
}

test('pane ownership: pane taken, THEN mount fails → the absent page is back', dom, withDom(async (win) => {
  const s = await fullStack(win, () => ({
    EXT: { id: 'voice', api: [1, 1] },
    async mount(shell) {
      shell.settings.pane('voice', { render: (r) => { r.textContent = 'ADDON'; } });
      shell.chatHeader.addAction(win.document.createElement('button'));
      throw new Error('late failure');
    },
  }));
  await s.loader.refresh();
  assert.equal(s.root.textContent, 'ABSENT');
  assert.equal(win.document.getElementById('slot').childElementCount, 0, 'its header button is gone too');
  assert.equal(s.loader.snapshot().client.get('voice').state, 'failed');
}));

test('pane ownership: mount fails BEFORE taking the pane → the absent page stays', dom, withDom(async (win) => {
  const s = await fullStack(win, () => ({
    EXT: { id: 'voice', api: [1, 1] },
    async mount() { throw new Error('early failure'); },
  }));
  await s.loader.refresh();
  assert.equal(s.root.textContent, 'ABSENT');
  assert.equal(s.loader.snapshot().client.get('voice').state, 'failed');
}));

test('pane ownership: a healthy add-on owns the pane until the lock unmounts it', dom, withDom(async (win) => {
  const s = await fullStack(win, () => ({
    EXT: { id: 'voice', api: [1, 1] },
    async mount(shell) {
      shell.settings.pane('voice', { render: (r) => { r.textContent = 'ADDON'; } });
      return { unmount() {} };
    },
  }));
  await s.loader.refresh();
  assert.equal(s.root.textContent, 'ADDON');
  s.loader.unmountAll();
  assert.equal(s.root.textContent, 'ABSENT', 'everything it registered was disposed');
}));

test('the shell object is frozen and carries the contract members', dom, withDom(async () => {
  const sdk = await import(`../static/js/ext-sdk.js?t=${Math.random()}`);
  const shell = sdk.createShell('voice');
  assert.ok(Object.isFrozen(shell));
  assert.equal(shell.EXT_API, 1);
  assert.equal(shell.version, '2.4.2');
  for (const k of ['el', 'railIcon', 'RAIL_ICONS', 'loadStyle', 'acquireInert', 'releaseInert', 'isMobile',
    'onLocale', 'onLocked', 'onThread', 'onThreadChange', 'api', 'activeThread', 'bots', 'botById', 'allBots',
    'isLocked', 'language', 'features', 'threadFace', 'toast', 'confirm', 'prompt', 'navigate']) {
    assert.equal(typeof shell[k] === 'function' || typeof shell[k] === 'object', true, k);
  }
  for (const k of ['t', 'tHtml', 'has', 'applyDom', 'list', 'n', 'lang']) assert.equal(typeof shell.i18n[k], 'function', k);
  assert.equal(typeof shell.chatHeader.addAction, 'function');
  assert.equal(typeof shell.settings.pane, 'function');
  assert.equal(typeof shell.settings.open, 'function');
  assert.equal(typeof shell.settings.close, 'function', 'amendment T7a-2');
  assert.equal(typeof shell.bots.rowExtra, 'function');
  assert.equal(typeof shell.tools.register, 'function');
  assert.equal(typeof shell.tools.openPane, 'function');
  assert.equal(typeof shell.history.push, 'function');
  assert.throws(() => { shell.EXT_API = 2; });
  // Listeners registered through an extension's shell go with it.
  const seen = [];
  shell.onLocked(() => seen.push('locked'));
  sdk.emitLocked();
  sdk.disposeExtension('voice');
  sdk.emitLocked();
  assert.deepEqual(seen, ['locked']);
}));

test('onThread (T7a-2) and onThreadChange are one channel, disposed with the extension', dom, withDom(async () => {
  const sdk = await import(`../static/js/ext-sdk.js?t=${Math.random()}`);
  const shell = sdk.createShell('voice');
  const seen = [];
  shell.onThread((th) => seen.push(['a', th && th.id]));
  shell.onThreadChange((th) => seen.push(['b', th && th.id]));
  sdk.emitThreadChange({ id: 't1' });
  sdk.emitThreadChange(null);
  sdk.disposeExtension('voice');
  sdk.emitThreadChange({ id: 't2' });
  assert.deepEqual(seen, [['a', 't1'], ['b', 't1'], ['a', null], ['b', null]]);
}));

test('main.js implements the optional T7a-2 members: closeSettings, openPane onClose, stacked Settings history', () => {
  const src = readFileSync(new URL('../static/js/main.js', import.meta.url), 'utf8');
  assert.match(src, /closeSettings: \(\) => \{ if \(settingsOpen\(\)\) closeBotManager\(\); \}/);
  assert.match(src, /function openToolPane\(owner, \{ id, title, subtitle, iconPaths, render, onClose \}/);
  assert.match(src, /if \(entry\.onClose\) \{\s*try \{ entry\.onClose\(\); \}/);
  // Settings over an overlay pushes its own entry only when an extension's
  // entry is active, and pops it when Settings closes any other way.
  assert.match(src, /historyPush\(null, 'settings', async \(\) => \{/);
  assert.match(src, /extHistory\.some\(\(x\) => x\.active && x\.owner\)/);
  assert.match(src, /new MutationObserver\(syncSettingsHistory\)/);
});

test('activity() (2.2.0, amendment FINAL-FIX-3) re-arms the shell idle lock and is a no-op without one', dom, withDom(async () => {
  const sdk = await import(`../static/js/ext-sdk.js?t=${Math.random()}`);
  const shell = sdk.createShell('voice');
  assert.equal(typeof shell.activity, 'function');
  assert.doesNotThrow(() => shell.activity(), 'no implementation installed yet: nothing happens');
  let n = 0;
  sdk.installShell({ activity: () => { n += 1; } });
  shell.activity();
  shell.activity();
  assert.equal(n, 2);
  // main.js wires it to the idle timer, with bumpIdle's own rules.
  const src = readFileSync(new URL('../static/js/main.js', import.meta.url), 'utf8');
  assert.match(src, /activity: \(\) => bumpIdle\(\),/);
}));

// --------------------------------------------------------------------------
// 2.3: add-in Settings tabs — shell.settings.pane(id) with a new id.
// --------------------------------------------------------------------------

const TABS_HTML = `<!doctype html><html><body>
  <div id="settings-tabs">
    <button id="stab-bots" data-tab="bots"></button><button id="stab-voice" data-tab="voice"></button>
    <button id="stab-plugins" data-tab="plugins"></button><button id="stab-ai" data-tab="ai"></button>
  </div>
  <section id="spane-bots"></section><section id="spane-voice"></section>
  <section id="spane-plugins"></section><section id="spane-ai"></section>
</body></html>`;

function tabsEnv(over = {}) {
  const win = new jsdom.JSDOM(TABS_HTML).window;
  const doc = win.document;
  const log = [];
  const tabs = createAddinTabs({
    doc,
    tablist: doc.getElementById('settings-tabs'),
    beforeTab: doc.getElementById('stab-plugins'),
    beforePane: doc.getElementById('spane-plugins'),
    reserved: new Set(['bots', 'voice', 'plugins', 'ai']),
    t: (k) => `T(${k})`,
    isLocked: () => false,
    onAdd: (id, o) => log.push(['add', id, o.public]),
    onRemove: (id) => log.push(['remove', id]),
    onTab: (id) => log.push(['tab', id]),
    ...over,
  });
  return { win, doc, tabs, log };
}
const tabOrder = (doc) => [...doc.querySelectorAll('#settings-tabs > button')].map((b) => b.dataset.tab);
const paneOrder = (doc) => [...doc.querySelectorAll('section')].map((s) => s.id.replace('spane-', ''));

test('add-in tab ids: lower-case letters, digits, hyphen, 2-31 characters', () => {
  for (const ok of ['ab', 'stats', 'my-addon', 'a1', 'x'.padEnd(31, 'y')]) assert.ok(ADDIN_TAB_ID.test(ok), ok);
  for (const bad of ['a', '', '1abc', 'Abc', 'a_b', 'a b', 'x'.padEnd(32, 'y'), '-ab', 'a.b']) assert.ok(!ADDIN_TAB_ID.test(bad), bad);
});

test('a new pane id creates a tab and a pane after Voice, before Plugins, titled from the spec', dom, () => {
  const { doc, tabs, log } = tabsEnv();
  const seen = [];
  tabs.claim('stats-addon', 'stats', {
    title: 'Stats', glyph: '📊', render: (r) => { r.textContent = 'HELLO'; },
    onShow: () => seen.push('show'), onHide: () => seen.push('hide'),
  });
  assert.deepEqual(tabOrder(doc), ['bots', 'voice', 'stats', 'plugins', 'ai']);
  assert.deepEqual(paneOrder(doc), ['bots', 'voice', 'stats', 'plugins', 'ai']);
  const btn = doc.getElementById('stab-stats');
  assert.equal(btn.getAttribute('role'), 'tab');
  assert.equal(btn.getAttribute('aria-controls'), 'spane-stats');
  assert.equal(btn.querySelector('.stab-label').textContent, 'Stats');
  assert.equal(btn.querySelector('.stab-glyph').textContent, '📊');
  assert.ok(btn.classList.contains('admin-only'), 'admin-only by default');
  const pane = doc.getElementById('spane-stats');
  assert.equal(pane.getAttribute('role'), 'tabpanel');
  assert.equal(pane.getAttribute('aria-labelledby'), 'stab-stats');
  assert.ok(pane.classList.contains('hidden'));
  assert.equal(pane.textContent, 'HELLO');
  assert.deepEqual(log, [['add', 'stats', false]]);
  btn.click();
  assert.deepEqual(log.at(-1), ['tab', 'stats']);
  // The pane host fires the add-on's show/hide as the tab comes and goes.
  tabs.host('stats').show();
  tabs.host('stats').hide();
  assert.deepEqual(seen, ['show', 'hide']);
});

test('titleKey is looked up in the i18n bundle and tagged data-i18n for language changes', dom, () => {
  const { doc, tabs } = tabsEnv();
  tabs.claim('a-b', 'stats', { titleKey: 'stats.tab', render() {} });
  const label = doc.querySelector('#stab-stats .stab-label');
  assert.equal(label.textContent, 'T(stats.tab)');
  assert.equal(label.getAttribute('data-i18n'), 'stats.tab');
});

test('public: true drops admin-only; a locked session starts an admin tab hidden', dom, () => {
  const { doc, tabs, log } = tabsEnv({ isLocked: () => true });
  tabs.claim('x1', 'secret', { title: 'S', render() {} });
  tabs.claim('x2', 'open-tab', { title: 'O', public: true, render() {} });
  const secret = doc.getElementById('stab-secret');
  const open = doc.getElementById('stab-open-tab');
  assert.ok(secret.classList.contains('admin-only') && secret.classList.contains('hidden'));
  assert.ok(!open.classList.contains('admin-only') && !open.classList.contains('hidden'));
  assert.deepEqual(log.filter((e) => e[0] === 'add'), [['add', 'secret', false], ['add', 'open-tab', true]]);
});

test('dispose removes the tab and the pane and tells the shell', dom, () => {
  const { doc, tabs, log } = tabsEnv();
  const dispose = tabs.claim('o', 'stats', { title: 'S', render() {} });
  assert.ok(tabs.has('stats'));
  dispose();
  assert.equal(doc.getElementById('stab-stats'), null);
  assert.equal(doc.getElementById('spane-stats'), null);
  assert.ok(!tabs.has('stats'));
  assert.deepEqual(log.at(-1), ['remove', 'stats']);
  dispose();                                   // idempotent
  assert.equal(log.filter((e) => e[0] === 'remove').length, 1);
  // The id is free again.
  tabs.claim('o2', 'stats', { title: 'S2', render() {} });
  assert.equal(doc.querySelector('#stab-stats .stab-label').textContent, 'S2');
});

test('invalid, built-in and taken ids are refused, and nothing is left behind', dom, () => {
  const { doc, tabs, log } = tabsEnv();
  assert.throws(() => tabs.claim('o', 'Bad Id', { render() {} }), /not a valid tab id/);
  assert.throws(() => tabs.claim('o', 'x', { render() {} }), /not a valid tab id/);
  assert.throws(() => tabs.claim('o', 'bots', { render() {} }), /built-in tab/);
  assert.throws(() => tabs.claim('o', 'ai', { render() {} }), /built-in tab/);
  assert.throws(() => tabs.claim('o', 'stats', {}), /render/);
  tabs.claim('o', 'stats', { title: 'S', render() {} });
  assert.throws(() => tabs.claim('someone-else', 'stats', { render() {} }), /taken/);
  assert.deepEqual(tabOrder(doc), ['bots', 'voice', 'stats', 'plugins', 'ai']);
  assert.equal(log.filter((e) => e[0] === 'add').length, 1);
});

test('the same owner claiming its tab again re-renders into the existing pane', dom, () => {
  const { doc, tabs } = tabsEnv();
  tabs.claim('o', 'stats', { title: 'S', render: (r) => { r.textContent = 'ONE'; } });
  tabs.claim('o', 'stats', { title: 'S', render: (r) => { r.textContent = 'TWO'; } });
  assert.equal(doc.querySelectorAll('#stab-stats').length, 1);
  assert.equal(doc.getElementById('spane-stats').textContent, 'TWO');
});

test('main.js routes an unknown shell.settings.pane id to the add-in tabs and keeps its lists in step', () => {
  const src = readFileSync(new URL('../static/js/main.js', import.meta.url), 'utf8');
  assert.match(src, /return addinTabs\.claim\(owner, id, spec\);/);
  // A claim before wireSettingsTabs built the add-in tab host is a real error
  // for the loader to record, never a TypeError on null.
  assert.match(src, /if \(!addinTabs\) throw new Error\(`settings\.pane: "\$\{id\}" claimed before the Settings tabs exist`\);/);
  assert.match(src, /SETTINGS_TABS\.splice\(at < 0 \? SETTINGS_TABS\.length : at, 0, id\)/);
  assert.match(src, /if \(!isPublic\) ADMIN_TABS\.push\(id\);/);
  assert.match(src, /reserved: CORE_SETTINGS_TABS/);
  // The strip is walked live by the keyboard handler, so add-in tabs get arrows for free.
  assert.match(src, /list\.querySelectorAll\('\.settings-tab'\)/);
});

test('the SDK documents the new pane ids', () => {
  const sdk = readFileSync(new URL('../static/js/ext-sdk.js', import.meta.url), 'utf8');
  assert.match(sdk, /\[a-z\]\[a-z0-9-\]\{1,30\}/);
  assert.match(sdk, /titleKey/);
});
