// Settings -> Plugins (js/plugins.js) rendered against the REAL index.html
// markup, with the backend stubbed at fetch() (the shapes are the contract in
// docs/design/platform-2-3.md section 3), plus the tab order and the removal
// of the old Tools tab.

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
const MAIN = readFileSync(join(STATIC, 'js', 'main.js'), 'utf8');
const EN = JSON.parse(readFileSync(join(STATIC, 'locales', 'en.json'), 'utf8'));

const require = createRequire(import.meta.url);
let jsdom = null;
try { jsdom = require('jsdom'); } catch { /* not installed - DOM tests skip */ }
const dom = { skip: domSkip(jsdom ? false : 'jsdom is not installed') };

// ---------------------------------------------------------------- tab order

const TAB_ORDER = ['bots', 'theme', 'device', 'voice', 'plugins', 'ai', 'reactions', 'health', 'people', 'security'];

test('the tab strip reads Bots, Theme, Device, Voice, Plugins, AI models, Reactions, Health, People, Security', () => {
  const tabs = [...HTML.matchAll(/<button class="settings-tab[^"]*" id="stab-([a-z]+)"/g)].map((m) => m[1]);
  assert.deepEqual(tabs, TAB_ORDER);
});

test('the panes follow the same order as the tabs', () => {
  const panes = [...HTML.matchAll(/<section class="settings-pane[^"]*" id="spane-([a-z]+)"/g)].map((m) => m[1]);
  assert.deepEqual(panes, TAB_ORDER);
});

test('every tab button points at its pane and the pane back at the tab', () => {
  for (const id of TAB_ORDER) {
    assert.match(HTML, new RegExp(`id="stab-${id}"[^>]*aria-controls="spane-${id}"`), id);
    assert.match(HTML, new RegExp(`id="spane-${id}"[^>]*aria-labelledby="stab-${id}"`), id);
  }
});

test('main.js lists the tabs in the same order and the admin rule is unchanged', () => {
  const m = /const SETTINGS_TABS = \[([^\]]*)\];/.exec(MAIN);
  assert.deepEqual([...m[1].matchAll(/'([a-z]+)'/g)].map((x) => x[1]), TAB_ORDER);
  const a = /const ADMIN_TABS = \[([^\]]*)\];/.exec(MAIN);
  assert.deepEqual([...a[1].matchAll(/'([a-z]+)'/g)].map((x) => x[1]).sort(),
    ['ai', 'health', 'people', 'plugins', 'reactions', 'voice']);
  // The same tabs are admin-only in the markup (Bots, Theme, Device and
  // Security stay visible to a Restricted session, exactly as before; People &
  // Profiles joined in 3.0 and shows for owner/admin only -- canAdmin()).
  const adminInMarkup = [...HTML.matchAll(/class="settings-tab admin-only[^"]*" id="stab-([a-z]+)"/g)].map((x) => x[1]).sort();
  assert.deepEqual(adminInMarkup, ['ai', 'health', 'people', 'plugins', 'reactions', 'voice']);
});

test('the first-run card still opens the AI models tab', () => {
  assert.match(MAIN, /openSettingsTab\('ai'\);/);
});

test('the old Tools tab and its locale key are gone', () => {
  assert.doesNotMatch(HTML, /stab-tools|spane-tools/);
  assert.equal(EN.tools.settings_tab, undefined);
});

// ---------------------------------------------------------------- the pane

const DATA = () => ({
  extensions: [
    { id: 'voice', name: 'DisPatch Voice', version: '1.1.0', state: 'ok', enabled: true,
      restart_pending: false, source: 'entry point (dispatch-voice 1.1.0)', detail: '' },
    { id: 'stats', name: 'Stats', version: '0.3.0', state: 'failed', enabled: true,
      restart_pending: false, source: 'entry point', detail: 'ImportError: no module named numpy' },
    { id: 'old', name: 'Old', version: '', state: 'disabled', enabled: false,
      restart_pending: false, source: 'entry point', detail: 'Switched off in Settings' },
  ],
  tools: [],
  livetools: { enabled: true, names: ['web_search', 'weather', 'calculator'] },
  voice: { installed: true, version: '1.1.0', install_cmd: 'x' },
});

let calls = [];
let routes = {};

async function setup(over = {}) {
  const { JSDOM } = jsdom;
  const win = new JSDOM(HTML, { url: 'http://127.0.0.1:8765/', runScripts: 'outside-only' }).window;
  const put = (k, v) => Object.defineProperty(globalThis, k, { value: v, configurable: true, writable: true });
  put('window', win);
  put('document', win.document);
  put('location', win.location);
  put('navigator', win.navigator);
  put('localStorage', win.localStorage);
  put('Node', win.Node);
  put('HTMLElement', win.HTMLElement);
  put('HTMLInputElement', win.HTMLInputElement);
  calls = [];
  routes = {
    'GET /static/locales/en.json': () => [200, EN],
    'GET /api/plugins': () => [200, over.data || DATA()],
    'PUT /api/plugins/extensions/voice': () => [200, { id: 'voice', enabled: false, restart_required: true }],
    'PUT /api/plugins/extensions/stats': () => [200, { id: 'stats', enabled: false, restart_required: true }],
    'PUT /api/plugins/livetools': () => [200, { enabled: false, names: [], restart_required: false }],
    ...(over.routes || {}),
  };
  globalThis.fetch = async (u, opts = {}) => {
    const key = `${(opts.method || 'GET').toUpperCase()} ${u}`;
    calls.push({ key, body: opts.body ? JSON.parse(opts.body) : null });
    const h = routes[key];
    const [status, body] = h ? h(opts) : [404, { detail: 'Not Found' }];
    return {
      ok: status >= 200 && status < 300, status, statusText: String(status),
      headers: new win.Headers(), json: async () => body,
    };
  };
  const i18n = await import('../static/js/i18n.js?v=4');
  await i18n.init('en');
  const mod = await import(`../static/js/plugins.js?t=${Math.random()}`);
  const rec = { toasts: [], tools: [], tabs: [] };
  const deps = {
    mountTools: async (host) => { rec.tools.push(host); host.append(win.document.createTextNode('TOOLS-TABLE')); },
    toast: (m, err) => rec.toasts.push([m, !!err]),
    openTab: (id) => rec.tabs.push(id),
  };
  const pane = win.document.getElementById('spane-plugins');
  return { win, mod, rec, deps, pane };
}

const tick = () => new Promise((r) => setTimeout(r, 0));

test('Plugins pane: four groups - Works with DisPatch, Add-ons, Apps & tools, Built-in lookups', { skip: dom.skip }, async () => {
  const { mod, pane, deps, rec } = await setup();
  await mod.mountPluginsSettings(pane, deps);
  const titles = [...pane.querySelectorAll('.dev-group-title')].map((h) => h.textContent);
  assert.deepEqual(titles, ['Works with DisPatch', 'Add-ons', 'Apps & tools', 'Built-in lookups']);
  // The Tools table got its own host inside the second group.
  assert.equal(rec.tools.length, 1);
  assert.equal(rec.tools[0].id, 'plugins-tools-host');
  assert.ok(pane.querySelector('#plugins-tools .tools-pane').contains(rec.tools[0]));
  assert.match(rec.tools[0].textContent, /TOOLS-TABLE/);
});

test('Add-ons: one row per extension with name, version, state and an install-wide switch', { skip: dom.skip }, async () => {
  const { mod, pane, deps } = await setup();
  await mod.mountPluginsSettings(pane, deps);
  const rows = [...pane.querySelectorAll('#plugins-addons-body > [data-ext]')];
  assert.deepEqual(rows.map((r) => r.dataset.ext), ['voice', 'stats', 'old']);
  const voice = rows[0];
  assert.match(voice.querySelector('strong').textContent, /DisPatch Voice v1\.1\.0/);
  assert.equal(voice.querySelector('.plugins-state').textContent, 'Running');
  assert.equal(voice.querySelector('input[type=checkbox]').checked, true);
  assert.equal(rows[1].querySelector('.plugins-state').textContent, 'Failed to start');
  assert.equal(rows[1].querySelector('.ext-detail').textContent, 'ImportError: no module named numpy');
  assert.equal(rows[2].querySelector('.plugins-state').textContent, 'Switched off');
  assert.equal(rows[2].querySelector('input[type=checkbox]').checked, false);
  assert.equal(rows[2].querySelector('strong').textContent, 'Old', 'no version, no stray "v"');
  // Every switch is labelled for screen readers.
  for (const r of rows) assert.ok(r.querySelector('input').getAttribute('aria-label'));
  // The installed voice add-on is a normal row: no "not installed" row.
  assert.equal(pane.querySelector('.plugins-voice-missing'), null);
});

test('an add-on switch PUTs {enabled} and shows the restart hint', { skip: dom.skip }, async () => {
  const { mod, pane, deps } = await setup();
  await mod.mountPluginsSettings(pane, deps);
  const row = pane.querySelector('[data-ext="voice"]');
  const hint = row.querySelector('.plugins-restart');
  assert.equal(hint.hidden, true);
  const cb = row.querySelector('input');
  cb.checked = false;
  cb.dispatchEvent(new globalThis.window.Event('change'));
  await tick(); await tick();
  const put = calls.find((c) => c.key === 'PUT /api/plugins/extensions/voice');
  assert.deepEqual(put.body, { enabled: false });
  assert.equal(hint.hidden, false);
  assert.equal(hint.textContent, 'Takes effect after DisPatch restarts.');
  assert.equal(cb.disabled, false);
  // Back to where it started: nothing to restart.
  routes['PUT /api/plugins/extensions/voice'] = () => [200, { id: 'voice', enabled: true, restart_required: true }];
  cb.checked = true;
  cb.dispatchEvent(new globalThis.window.Event('change'));
  await tick(); await tick();
  assert.deepEqual(calls.at(-1).body, { enabled: true });
  assert.equal(hint.hidden, true);
});

test('a failed save puts the switch back and says why', { skip: dom.skip }, async () => {
  const { mod, pane, deps, rec } = await setup({
    routes: { 'PUT /api/plugins/extensions/voice': () => [403, { detail: 'Unlock for full access' }] },
  });
  await mod.mountPluginsSettings(pane, deps);
  const cb = pane.querySelector('[data-ext="voice"] input');
  cb.checked = false;
  cb.dispatchEvent(new globalThis.window.Event('change'));
  await tick(); await tick();
  assert.equal(cb.checked, true, 'reverted');
  assert.equal(rec.toasts.length, 1);
  assert.equal(rec.toasts[0][1], true);
  assert.match(rec.toasts[0][0], /Could not save: .*Unlock for full access/);
  assert.doesNotMatch(rec.toasts[0][0], /^403/);
});

test('voice add-on not installed: a row says so and shows the install command from voice-absent.js', { skip: dom.skip }, async () => {
  const data = DATA();
  data.extensions = [];
  data.voice = { installed: false, install_cmd: 'ignored - the frontend constants are the source' };
  const { mod, pane, deps, rec } = await setup({ data });
  await mod.mountPluginsSettings(pane, deps);
  const row = pane.querySelector('.plugins-voice-missing');
  assert.ok(row);
  assert.equal(row.querySelector('strong').textContent, 'Voice chat add-on: not installed');
  const va = await import('../static/js/voice-absent.js?v=1');
  assert.equal(row.querySelector('.ext-cmd code').textContent, va.COMMANDS.install);
  assert.equal(row.querySelector('.ext-cmd code').textContent,
    'uv pip install --python .venv/bin/python "dispatch-voice @ git+https://github.com/LaserLloyd/DisPatch_Voice@v1"');
  row.querySelector('.plugins-voice-open').click();
  assert.deepEqual(rec.tabs, ['voice']);
});

test('Built-in lookups: the switch PUTs /api/plugins/livetools and the tool names are listed', { skip: dom.skip }, async () => {
  const { mod, pane, deps, rec } = await setup();
  await mod.mountPluginsSettings(pane, deps);
  const body = pane.querySelector('#plugins-lookups-body');
  assert.deepEqual([...body.querySelectorAll('.plugins-chip')].map((c) => c.textContent),
    ['web_search', 'weather', 'calculator']);
  assert.match(body.textContent, /Offered to bots whose settings turn them on\./);
  const cb = body.querySelector('input[type=checkbox]');
  assert.equal(cb.checked, true);
  cb.checked = false;
  cb.dispatchEvent(new globalThis.window.Event('change'));
  await tick(); await tick();
  assert.deepEqual(calls.find((c) => c.key === 'PUT /api/plugins/livetools').body, { enabled: false });
  assert.deepEqual(rec.toasts.at(-1), ['Built-in lookups are off for every bot.', false]);
});

test('lookups switch: a refusal reverts it', { skip: dom.skip }, async () => {
  const { mod, pane, deps, rec } = await setup({
    routes: { 'PUT /api/plugins/livetools': () => [409, { detail: 'tools.yaml cannot be read' }] },
  });
  await mod.mountPluginsSettings(pane, deps);
  const cb = pane.querySelector('#plugins-lookups-body input');
  cb.checked = false;
  cb.dispatchEvent(new globalThis.window.Event('change'));
  await tick(); await tick();
  assert.equal(cb.checked, true);
  assert.equal(rec.toasts.at(-1)[1], true);
});

test('a failed load shows the error with Retry, and still gives the Tools table its host', { skip: dom.skip }, async () => {
  const { mod, pane, deps, rec } = await setup({ routes: { 'GET /api/plugins': () => [500, { detail: 'boom' }] } });
  await mod.mountPluginsSettings(pane, deps);
  const alert = pane.querySelector('[role=alert]');
  assert.match(alert.textContent, /Could not load plugins: .*boom/);
  assert.equal(pane.querySelector('#plugins-lookups'), null, 'no lookups group without data');
  assert.equal(rec.tools.length, 1);
  routes['GET /api/plugins'] = () => [200, DATA()];
  alert.querySelector('button').click();
  await tick(); await tick(); await tick();
  assert.ok(pane.querySelector('[data-ext="voice"]'));
});

test('a newer mount supersedes a slower, older one', { skip: dom.skip }, async () => {
  const { mod, pane, deps, rec } = await setup();
  const first = mod.mountPluginsSettings(pane, deps);
  const second = mod.mountPluginsSettings(pane, deps);
  await Promise.all([first, second]);
  assert.equal(pane.querySelectorAll('#plugins-addons').length, 1);
  assert.equal(rec.tools.length, 1, 'only the newest mount builds the Tools table');
});

test('the api client sends the contract shapes', { skip: dom.skip }, async () => {
  await setup({ routes: { 'PUT /api/plugins/extensions/my-addon': () => [200, { restart_required: true }] } });
  calls.length = 0;                      // drop the locale fetch
  const { api } = await import('../static/js/api.js?v=32');
  await api.plugins();
  await api.setPluginExtension('my-addon', false);
  await api.setPluginLivetools(true);
  assert.deepEqual(calls.map((c) => c.key), [
    'GET /api/plugins', 'PUT /api/plugins/extensions/my-addon', 'PUT /api/plugins/livetools']);
  assert.deepEqual(calls[1].body, { enabled: false });
  assert.deepEqual(calls[2].body, { enabled: true });
});

test('plugins.js and bots-edit.js are in the service worker shell and use no innerHTML', () => {
  const sw = readFileSync(join(STATIC, 'sw.js'), 'utf8');
  for (const f of ['plugins.js', 'bots-edit.js']) {
    assert.ok(sw.includes(`'/static/js/${f}'`), f);
    assert.doesNotMatch(readFileSync(join(STATIC, 'js', f), 'utf8'), /innerHTML|outerHTML|insertAdjacentHTML/, f);
  }
});

test('plugins and bots_edit locale keys exist in all eight locales with the same placeholders', () => {
  for (const lang of ['en', 'ar', 'de', 'es', 'fr', 'ja', 'pt', 'zh']) {
    const d = JSON.parse(readFileSync(join(STATIC, 'locales', `${lang}.json`), 'utf8'));
    for (const ns of ['plugins', 'bots_edit']) {
      assert.deepEqual(Object.keys(d[ns]), Object.keys(EN[ns]), `${lang}.${ns}`);
      for (const [k, v] of Object.entries(EN[ns])) {
        const ph = (s) => [...s.matchAll(/\{(\w+)\}/g)].map((m) => m[1]).sort();
        assert.deepEqual(ph(d[ns][k]), ph(v), `${lang}.${ns}.${k}`);
      }
    }
  }
});

test('the namespaces sit right before security, after the others', () => {
  const keys = Object.keys(EN);
  const i = keys.indexOf('plugins');
  assert.deepEqual(keys.slice(i - 1, i + 3), [keys[i - 1], 'plugins', 'bots_edit', 'security']);
});

test('switching off a failed add-on shows the restart hint too: it stays loaded until a restart', { skip: dom.skip }, async () => {
  const { mod, pane, deps } = await setup();
  await mod.mountPluginsSettings(pane, deps);
  const row = pane.querySelector('[data-ext="stats"]');
  const hint = row.querySelector('.plugins-restart');
  assert.equal(hint.hidden, true, 'nothing pending at first');
  const cb = row.querySelector('input');
  cb.checked = false;
  cb.dispatchEvent(new globalThis.window.Event('change'));
  await tick(); await tick();
  assert.deepEqual(calls.at(-1).body, { enabled: false });
  assert.equal(hint.hidden, false, 'the server rule: off + not yet unloaded = restart pending');
  // And the mirror image: a row the operator switched off earlier (state
  // "disabled") switched back on is pending until the restart loads it.
  const old = pane.querySelector('[data-ext="old"]');
  routes['PUT /api/plugins/extensions/old'] = () => [200, { id: 'old', enabled: true, restart_required: true }];
  const ocb = old.querySelector('input');
  ocb.checked = true;
  ocb.dispatchEvent(new globalThis.window.Event('change'));
  await tick(); await tick();
  assert.equal(old.querySelector('.plugins-restart').hidden, false);
});
