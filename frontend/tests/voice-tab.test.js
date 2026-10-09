// Settings → Voice and the voice tools (2026-10-10: voice is optional).
//
// Settings → Voice opens with an on/off switch (server-wide, off on a fresh
// install) and holds the everyday settings only. Drive mode's advanced pieces
// are opt-in tools: Voice cloning, Built-in voices, Voice lab
// (js/voice-tools.js), drawn by js/tools.js like any builtin and opened by
// main.js into #voice-tool-view.
//
// Three halves: the pure state functions (voiceTabState, setupSteps,
// cloningState, builtinState), the wiring pins in index.html and main.js,
// and the DOM builders under jsdom (the tab in each state, each tool
// installed and not installed, the stock voice row).

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { domSkip } from './_require-dom.js';

import {
  voiceTabState, setupSteps, mountDriveSettings, voiceOptions, voiceName, engineLine,
} from '../static/js/drive-settings.js';
import {
  cloningState, builtinState, mountVoiceTool, unmountVoiceTool, VOICE_TOOLS, INSTALL, fmtMs, builtinPlugin,
} from '../static/js/voice-tools.js';
import { createVoicesPane, isStock } from '../static/js/voices.js';

const require = createRequire(import.meta.url);
const HERE = dirname(fileURLToPath(import.meta.url));
const STATIC = join(HERE, '..', 'static');
const read = (p) => readFileSync(join(STATIC, p), 'utf8');
const HTML = read('index.html');
const MARKUP = HTML.replace(/<!--[\s\S]*?-->/g, '');
const MAIN = read('js/main.js');
const TOOLS = read('js/tools.js');

function fnBody(src, name) {
  const start = src.search(new RegExp(`(?:async\\s+)?function\\s+${name}\\s*\\(`));
  assert.ok(start >= 0, `${name} must be defined`);
  const end = src.indexOf('\n}\n', start);
  assert.ok(end > start, `${name} must have a body`);
  return src.slice(start, end);
}

/** The markup of the <section id="spane-…"> pane, up to its closing tag. */
function pane(id) {
  const start = MARKUP.indexOf(`id="spane-${id}"`);
  assert.ok(start >= 0, `spane-${id} must exist`);
  const next = MARKUP.indexOf('<section class="settings-pane', start + 1);
  return MARKUP.slice(start, next < 0 ? undefined : next);
}

const STOCK = 'stock-host';

// ------------------------------------------------------- voiceTabState ---

test('voiceTabState: no status → none (no tab)', () => {
  for (const v of [null, undefined, 'nope', 3]) assert.equal(voiceTabState(v), 'none');
});

test('voiceTabState: the switch decides off / setup / on', () => {
  // A fresh install: everything present, switch off → off (not on).
  assert.equal(voiceTabState({ enabled: false, switch: false, forced: null, missing: [] }), 'off');
  assert.equal(voiceTabState({ enabled: false, switch: false, missing: ['python package numpy'] }), 'off');
  // Switched on, something missing → setup.
  assert.equal(voiceTabState({ enabled: false, switch: true, missing: ['model silero_vad.onnx'] }), 'setup');
  // Switched on and ready → on.
  assert.equal(voiceTabState({ enabled: true, switch: true, missing: [] }), 'on');
});

test('voiceTabState: DISPATCH_VOICE overrides', () => {
  assert.equal(voiceTabState({ enabled: false, switch: false, forced: 'off', missing: [] }), 'forced');
  assert.equal(voiceTabState({ enabled: true, switch: true, forced: 'on', missing: [] }), 'on');
  // Forced on but a model is missing: the tab still says what is missing.
  assert.equal(voiceTabState({ enabled: true, switch: true, forced: 'on', missing: ['model silero_vad.onnx'] }), 'setup');
});

// ---------------------------------------------------------- setupSteps ---

test('setupSteps: each kind of missing piece maps to the command that fixes it, in order', () => {
  const cmds = (m) => setupSteps(m).map((s) => s.cmd);
  assert.deepEqual(cmds([]), []);
  assert.deepEqual(cmds(['python package numpy', 'python package onnx-asr', 'model silero_vad.onnx', 'model parakeet-tdt-0.6b-v2']),
    ['uv sync --group voice', 'uv run python -m app.voice.download']);
  const GIT = 'uv run --group voice --with "dispatch-voice @ git+https://github.com/LaserLloyd/DisPatch_Voice"';
  assert.deepEqual(cmds(['dispatch-voice models (run `dispatch-voice fetch-models`)']), [`${GIT} dispatch-voice fetch-models`]);
  const tts = setupSteps(['a text-to-speech engine: install dispatch-voice (MIT) and run …']);
  assert.deepEqual(tts.map((s) => s.where), ['backend', 'start']);
  assert.deepEqual(tts.map((s) => s.cmd), [`${GIT} dispatch-voice fetch-models`, `${GIT} uvicorn app.main:app`]);
  assert.ok(!tts.some((s) => /DISPATCH_VOICE_PKG/.test(s.cmd)), 'an installed package needs no PKG path');
  assert.deepEqual(cmds(['MINIMAX_API_KEY (DISPATCH_VOICE_TTS=minimax)']), ['MINIMAX_API_KEY=…']);
  // Something it does not know: no invented command (the tab points at the docs).
  assert.deepEqual(cmds(["TTS engine 'x' is not installed"]), []);
});

// ----------------------------------------------------------- voice tools ---

test('cloningState / builtinState read the status plugins block', () => {
  assert.equal(cloningState(null), 'missing');
  assert.equal(cloningState({ plugins: { cloning: { installed: false } } }), 'missing');
  assert.equal(cloningState({ plugins: { cloning: { installed: true, ready: false } } }), 'models');
  assert.equal(cloningState({ plugins: { cloning: { installed: true, ready: true } } }), 'ready');
  assert.equal(builtinState(null), 'missing');
  assert.equal(builtinState({ plugins: { builtin: { installed: false, package: false } } }), 'missing');
  assert.equal(builtinState({ plugins: { builtin: { installed: false, package: true } } }), 'not-enabled');
  assert.equal(builtinState({ plugins: { builtin: { installed: true, missing: ['model kokoro'] } } }), 'models');
  assert.equal(builtinState({ plugins: { builtin: { installed: true, missing: [] } } }), 'ready');
});

test('install steps: dispatch-voice here; the built-in plugin describes itself (no hard-coded plugin)', () => {
  assert.ok(INSTALL.cloning.some((s) => /git\+https:\/\/github\.com\/LaserLloyd\/DisPatch_Voice" dispatch-voice fetch-models$/.test(s.cmd)));
  assert.ok(INSTALL.cloning.some((s) => /DisPatch_Voice" uvicorn app\.main:app$/.test(s.cmd)));
  assert.doesNotMatch(read('js/voice-tools.js').replace(/\/\/.*$/gm, ''), /kokoro/i);
  const st = { plugins: { builtin: { installed: true, name: 'acme-voices', licence: 'MIT',
    models: [{ where: 'plugin', cmd: 'acme fetch' }],
    suggested: { name: 'dispatch-voice-kokoro', licence: 'GPL-3.0-or-later', install: [{ where: 'env', cmd: 'X=1' }], enable: [] } } } };
  const p = builtinPlugin(st);
  assert.deepEqual([p.name, p.licence, p.models[0].cmd], ['acme-voices', 'MIT', 'acme fetch'], 'a registered provider names itself');
  st.plugins.builtin.installed = false; st.plugins.builtin.name = null; st.plugins.builtin.licence = null;
  assert.deepEqual([builtinPlugin(st).name, builtinPlugin(st).licence], ['dispatch-voice-kokoro', 'GPL-3.0-or-later']);
});

test('fmtMs: SI units, no locale', () => {
  assert.equal(fmtMs(420.4), '420 ms');
  assert.equal(fmtMs(1340), '1.3 s');
  assert.equal(fmtMs(12500), '13 s');
  assert.equal(fmtMs(null), '–');
});

test('VOICE_TOOLS ids match the server builtins and the tools.js rows', () => {
  assert.deepEqual(VOICE_TOOLS, { 'voice-cloning': 'voice_cloning', 'voice-builtin': 'voice_builtin', 'voice-lab': 'voice_lab' });
  for (const [id, feat] of Object.entries(VOICE_TOOLS)) {
    assert.match(TOOLS, new RegExp(`\\{ id: '${id}', feature: '${feat}',[^}]*optIn: true \\}`), `${id} is an opt-in builtin in tools.js`);
  }
  // Before /api/tools answers, an opt-in builtin is off (never shown by default).
  assert.match(fnBody(TOOLS, 'allTools'), /enabled: !b\.optIn/);
});

// ------------------------------------------------- follow-ups (split) ---

test('voice labels: the stock voice reads "Stock voice", a missing default gets a placeholder', () => {
  const st = { stock_voice: STOCK, profiles: [{ id: 'alex', name: 'Alex' }, { id: STOCK, name: 'Stock voice (synthetic)' }], builtin_voices: [] };
  // The localised label (drive.voice_stock), not the package's English name.
  assert.notEqual(voiceName(st, STOCK), 'Stock voice (synthetic)');
  assert.equal(voiceName(st, 'alex'), 'Alex');
  const opts = voiceOptions(st, { current: '', placeholder: true });
  assert.equal(opts[0].value, '', 'default "" → a neutral first entry');
  assert.equal(opts.find((o) => o.value === STOCK).group, 'stock');
  // With a default chosen there is no placeholder.
  assert.ok(!voiceOptions(st, { current: 'alex', placeholder: true }).some((o) => o.value === ''));
  assert.ok(isStock({ id: STOCK }, STOCK) && !isStock({ id: 'alex' }, STOCK) && !isStock({ id: STOCK }, null));
});

test('engineLine tells cloned+fallback, cloned only, stock only, built-in and cloud apart', () => {
  const base = { smart_turn: true, stt: 'parakeet', stock_voice: STOCK, voices_ui: '/api/voices/ui' };
  const lines = {
    clonedFallback: engineLine({ ...base, tts: 'auto', profiles: [{ id: 'a' }], builtin_voices: [{ id: 'b' }] }),
    clonedOnly: engineLine({ ...base, tts: 'auto', profiles: [{ id: 'a' }, { id: STOCK }], builtin_voices: [] }),
    stockOnly: engineLine({ ...base, tts: 'auto', profiles: [{ id: STOCK }], builtin_voices: [] }),
    builtin: engineLine({ tts: 'kokoro', smart_turn: true, stt: 'parakeet', profiles: [], builtin_voices: [{ id: 'b' }] }),
    cloud: engineLine({ ...base, tts: 'minimax', profiles: [] }),
  };
  assert.equal(new Set(Object.values(lines)).size, 5, JSON.stringify(lines, null, 1));
  for (const l of Object.values(lines)) assert.equal(l.split(' · ').length, 3);
});

// --------------------------------------------------------------- markup ---

test('the Voice tab sits right after Device, starts hidden, and owns its pane', () => {
  const tabs = [...MARKUP.matchAll(/class="settings-tab[^"]*" id="stab-([a-z]+)"/g)].map((m) => m[1]);
  const i = tabs.indexOf('device');
  assert.ok(i >= 0);
  assert.equal(tabs[i + 1], 'voice', `tab order: ${tabs.join(', ')}`);
  assert.match(MARKUP, /<button class="settings-tab hidden" id="stab-voice" role="tab" data-tab="voice" aria-selected="false" aria-controls="spane-voice" tabindex="-1">/);
  assert.match(MARKUP, /<section class="settings-pane hidden" id="spane-voice" role="tabpanel" aria-labelledby="stab-voice"/);
  assert.match(pane('voice'), /id="dev-group-voice"/);
  assert.doesNotMatch(pane('device'), /dev-group-voice|drive-settings/);
});

test('the voice tool pane: one hidden terminal-view with a back button, a title and a body', () => {
  assert.match(MARKUP, /<div class="terminal-view voice-tool-view hidden" id="voice-tool-view">/);
  for (const id of ['voice-tool-back', 'voice-tool-icon', 'voice-tool-title', 'voice-tool-subtitle', 'voice-tool-root']) {
    assert.match(MARKUP, new RegExp(`id="${id}"`), id);
    assert.match(MAIN, new RegExp(`'${id}'`), `${id} is in main.js's dom list`);
  }
  assert.match(read('app.css'), /\.voice-tool-body \{ display: block; overflow-y: auto; \}/);
});

// --------------------------------------------------------------- main.js ---

test('the Voice tab shows for any voice status (the switch lives there), never locked', () => {
  const body = fnBody(MAIN, 'applyVoiceTab');
  assert.match(body, /const show = !state\.decoy && voiceTab !== 'none';/);
  assert.match(body, /if \(settingsTab === 'voice'\) setSettingsTab\(state\.decoy \? 'bots' : 'device'\);/);
  const probe = fnBody(MAIN, 'probeVoice');
  assert.match(probe, /if \(state\.decoy\) \{\s*voiceOn = false;\s*voiceTab = 'none';/);
  // Drive needs voice ENABLED (switch on and ready, or forced on).
  assert.match(fnBody(MAIN, 'applyVoiceStatus'), /voiceOn = !state\.decoy && !!\(st && st\.enabled\);/);
  // Turning voice off closes an open drive.
  assert.match(fnBody(MAIN, 'setVoiceEnabled'), /if \(!on && drive && drive\.close\) drive\.close\(\);/);
});

test('"Manage voices" only when the Voice cloning tool is on AND installed', () => {
  const body = fnBody(MAIN, 'cloningToolReady');
  assert.match(body, /findTool\('voice-cloning'\)/);
  assert.match(body, /row\.enabled === true/);
  assert.match(body, /voiceSt\.plugins\.cloning\.ready/);
  assert.match(fnBody(MAIN, 'renderDriveSettings'), /cloning: cloningToolReady\(\),/);
  // The old in-tab Voices manager is gone from main.js.
  assert.doesNotMatch(MAIN, /createVoicesPane|voicesPane/);
});

test('voice tools: never in Safe Mode, on only with their tools.yaml row, opened like Clients', () => {
  const on = fnBody(MAIN, 'voiceToolOn');
  assert.match(on, /if \(state\.decoy\) return false;/);
  assert.match(on, /row && row\.enabled === true/);
  assert.match(fnBody(MAIN, 'builtinOn'), /if \(VOICE_FEATURES\.has\(feature\)\) return voiceToolOn\(feature\);/);
  const open = fnBody(MAIN, 'openVoiceToolView');
  assert.match(open, /if \(state\.decoy \|\| !voiceToolOn\(VOICE_TOOLS\[id\]\)\) return;/);
  assert.match(open, /closeToolPanes\('voicetool'\)/);
  assert.match(open, /document\.body\.classList\.add\('tool-full'\)/);
  assert.match(fnBody(MAIN, 'closeToolPanes'), /if \(voiceToolOpen && except !== 'voicetool'\) closeVoiceToolView\(\);/);
  assert.match(fnBody(MAIN, 'closeVoiceToolView'), /unmountVoiceTool\(\);/);
  for (const f of ['voice_cloning', 'voice_builtin', 'voice_lab']) {
    assert.match(MAIN, new RegExp(`${f}: \\(\\) => openVoiceToolView\\('${f.replace('_', '-')}'\\)`));
  }
});

test('"All Drive settings" opens Settings on the Voice tab', () => {
  const body = fnBody(MAIN, 'openDriveSettings');
  assert.match(body, /if \(state\.decoy\) return;/);
  assert.match(body, /openBotManager\('voice'\)/);
});

test('no shipped string sends people to Settings › Device for Drive mode; voice-off names the Voice tab', () => {
  for (const loc of ['en', 'de', 'es', 'fr', 'ja', 'pt', 'zh', 'ar']) {
    const d = JSON.parse(read(`locales/${loc}.json`));
    const device = d.settings.tab_voice && d.settings.tab_device;
    for (const [k, v] of Object.entries(d.drive)) {
      assert.ok(!String(v).includes(`› ${device} ›`), `${loc}:drive.${k} still points at Settings › ${device}`);
    }
    assert.ok(d.drive.err_unavailable.includes(d.settings.tab_voice), `${loc}: err_unavailable names the Voice tab`);
    assert.ok(d.drive.err_voice_off.includes(d.settings.tab_voice), `${loc}: err_voice_off names the Voice tab`);
    assert.equal(d.voices.back, d.settings.tab_voice, `${loc}: the Voices manager's back button names the tab it returns to`);
    // The licence is stated wherever the plugin is described, and it is the
    // PROVIDER's (or the suggested plugin's), never a string baked into the
    // app: a permissive provider must not be labelled GPL.
    for (const k of ['vtools.builtin_about', 'voices.builtin_plugin']) {
      const [ns, key] = k.split('.');
      assert.match(d[ns][key], /\{name\}/, `${loc}:${k} names the plugin from the server`);
      assert.match(d[ns][key], /\{licence\}/, `${loc}:${k} names its licence from the server`);
      assert.doesNotMatch(d[ns][key], /GPL|Kokoro|kokoro/, `${loc}:${k} hard-codes a plugin`);
    }
    for (const k of ['cloning_name', 'builtin_name', 'lab_name']) assert.ok(d.vtools[k], `${loc}: vtools.${k}`);
  }
});

// ------------------------------------------------------------- jsdom ---

let jsdom = null;
try { jsdom = require('jsdom'); } catch { /* not installed — tests skip */ }
const dom = { skip: domSkip(jsdom ? false : 'jsdom is not installed (see markdown-behaviour.test.js)') };

/** Run `fn(doc, host)` with a jsdom document as the global one. */
async function withDom(fn, { fetch = null } = {}) {
  const { JSDOM } = jsdom;
  const win = new JSDOM('<!doctype html><div id="host"><p>old</p></div>', { url: 'http://localhost/' }).window;
  const saved = { document: globalThis.document, fetch: globalThis.fetch, localStorage: globalThis.localStorage };
  globalThis.document = win.document;
  if (fetch) globalThis.fetch = fetch;
  Object.defineProperty(globalThis, 'localStorage', { value: win.localStorage, configurable: true, writable: true });
  try {
    return await fn(win.document, win.document.getElementById('host'));
  } finally {
    globalThis.document = saved.document;
    globalThis.fetch = saved.fetch;
    Object.defineProperty(globalThis, 'localStorage', { value: saved.localStorage, configurable: true, writable: true });
  }
}

const groupTitles = (host) => [...host.querySelectorAll('section.dev-group > h3.dev-group-title')].map((h) => h.textContent);

test('Voice tab, fresh install (off, nothing missing): the switch alone', dom, () => withDom((doc, host) => {
  let asked = null;
  mountDriveSettings(host, {
    status: { enabled: false, switch: false, forced: null, missing: [], profiles: [], builtin_voices: [] },
    setEnabled: async (v) => { asked = v; return true; },
  });
  const sw = host.querySelector('#drive-switch-input');
  assert.ok(sw && sw.type === 'checkbox' && !sw.checked && !sw.disabled);
  assert.equal(host.querySelectorAll('section.dev-group').length, 1);
  assert.ok(!host.querySelector('.drive-seg'), 'no Listening/Replies controls while off');
  assert.ok(!host.querySelector('details'), 'no Advanced group at all');
  assert.ok(!host.querySelector('.voice-cmd'), 'nothing to run');
  assert.equal(host.textContent.includes('old'), false);
  sw.checked = true;
  sw.dispatchEvent(new doc.defaultView.Event('change'));
  assert.equal(asked, true, 'the switch saves through ctx.setEnabled');
}));

test('Voice tab, off with packages missing: the switch, the missing list and the commands', dom, () => withDom((doc, host) => {
  mountDriveSettings(host, {
    status: { enabled: false, switch: false, missing: ['python package numpy', 'model silero_vad.onnx'] },
  });
  assert.ok(host.querySelector('#drive-switch-input'));
  assert.match(host.querySelector('.drive-missing').textContent, /numpy/);
  const cmds = [...host.querySelectorAll('.voice-cmd code')].map((c) => c.textContent);
  assert.deepEqual(cmds, ['uv sync --group voice', 'uv run python -m app.voice.download']);
}));

test('Voice tab, forced: off hides the switch; on shows it locked', dom, () => withDom((doc, host) => {
  mountDriveSettings(host, { status: { enabled: false, switch: false, forced: 'off', missing: [] } });
  assert.equal(host.querySelector('input[type="checkbox"]'), null);
  assert.equal(host.querySelectorAll('section.dev-group').length, 1);
  mountDriveSettings(host, { status: { enabled: true, switch: true, forced: 'on', missing: [], profiles: [], builtin_voices: [] } });
  const sw = host.querySelector('#drive-switch-input');
  assert.ok(sw.checked && sw.disabled);
}));

test('Voice tab, on: switch + Voice, Listening, Replies, This device; no Advanced, no Voices manager', dom, () => withDom((doc, host) => {
  const st = {
    enabled: true, switch: true, missing: [], default_voice: STOCK, stock_voice: STOCK,
    profiles: [{ id: STOCK, name: 'Stock voice (synthetic)' }], builtin_voices: [],
  };
  mountDriveSettings(host, { status: st });
  assert.equal(host.querySelectorAll('section.dev-group').length, 5, groupTitles(host).join(' | '));
  assert.ok(!host.querySelector('details.drive-advanced'));
  assert.ok(!host.querySelector('#dev-voices-open'), 'Manage voices only with the cloning tool');
  assert.ok(!host.querySelector('#drive-speed'), 'no speed row without built-in voices');
  assert.ok(host.querySelector('.drive-note'), 'the stock-only note');
  // With the tool on and installed: Manage voices opens it.
  let opened = false;
  mountDriveSettings(host, { status: st, cloning: true, openCloning: () => { opened = true; } });
  host.querySelector('#dev-voices-open').click();
  assert.equal(opened, true);
}));

test('Voice tab, on: speed only with built-in voices; "" default shows a placeholder', dom, () => withDom((doc, host) => {
  mountDriveSettings(host, {
    status: {
      enabled: true, switch: true, missing: [], default_voice: '', speed: 1.1,
      profiles: [{ id: 'alex', name: 'Alex' }], builtin_voices: [{ id: 'builtin:af_heart', name: 'Heart' }],
    },
  });
  assert.ok(host.querySelector('#drive-speed'));
  const sel = host.querySelector('select.drive-select');
  assert.equal(sel.value, '');
  assert.equal(sel.options[0].value, '');
  assert.equal(host.querySelector('.drive-note'), null, 'a voice of your own: no note');
}));

test('Voice tab, on: no voice at all says so instead of an empty select', dom, () => withDom((doc, host) => {
  mountDriveSettings(host, { status: { enabled: true, switch: true, missing: [], default_voice: '', profiles: [], builtin_voices: [] } });
  assert.ok(host.querySelector('#drive-no-voice'));
}));

/** A fetch that answers the voice API from a table: path → body (or a status). */
function fakeFetch(routes) {
  return async (url) => {
    const path = String(url).replace(/^https?:\/\/[^/]+/, '').split('?')[0];
    const hit = routes[path];
    if (hit === undefined) return { ok: false, status: 404, statusText: 'Not found', json: async () => ({ detail: 'Not found' }) };
    return { ok: true, status: 200, json: async () => (typeof hit === 'function' ? hit() : hit) };
  };
}

const settle = () => new Promise((r) => setTimeout(r, 20));

test('Voice cloning tool: not installed → how to install; installed → the Voices manager', dom, () => withDom(async (doc, host) => {
  const status = { enabled: true, switch: true, stock_voice: STOCK, builtin_voices: [], plugins: { cloning: { installed: false, ready: false } } };
  globalThis.fetch = fakeFetch({ '/api/voice/status': status });
  await mountVoiceTool(host, 'voice-cloning', {});
  assert.ok(host.querySelector('#vtool-install-group'));
  const code = [...host.querySelectorAll('.voice-cmd code')].map((c) => c.textContent).join('\n');
  assert.match(code, /dispatch-voice @ git\+https:\/\/github\.com\/LaserLloyd\/DisPatch_Voice" dispatch-voice fetch-models/);
  assert.doesNotMatch(code, /DISPATCH_VOICE_PKG/);
  unmountVoiceTool();

  status.plugins.cloning = { installed: true, ready: true };
  globalThis.fetch = fakeFetch({
    '/api/voice/status': status,
    '/api/voices': { voices: [{ id: STOCK, name: 'Stock voice (synthetic)', consent: { kind: 'synthetic' } }, { id: 'alex', name: 'Alex', consent: { kind: 'self' } }], default: 'alex' },
  });
  await mountVoiceTool(host, 'voice-cloning', {});
  await settle();
  assert.ok(host.querySelector('.voices-pane'));
  assert.equal(host.querySelector('.voices-back'), null, 'embedded: the tool header is the way back');
  const rows = [...host.querySelectorAll('.voices-row')];
  assert.deepEqual(rows.map((r) => r.dataset.id), ['alex', STOCK], 'the stock voice last');
  const stock = rows[1];
  assert.ok(stock.querySelector('.voices-stock'), 'the stock voice has a Stock badge');
  const buttons = [...stock.querySelectorAll('button')].map((b) => b.className);
  assert.ok(!buttons.some((c) => c.includes('voices-delete')), 'no Delete on the stock voice');
  assert.equal(stock.querySelectorAll('button').length, 2, 'Play + Set as default only (no Rename/Delete)');
  assert.ok(rows[0].querySelector('.voices-delete'), 'your own voice keeps Delete');
  // The built-in section's empty state points at the optional plugin.
  assert.match(host.querySelector('#voices-builtin').textContent, /builtin plugin|Builtin plugin/i);
  unmountVoiceTool();
}, { fetch: fakeFetch({}) }));

test('Built-in voices tool: plugin absent / installed-not-enabled / ready', dom, () => withDom(async (doc, host) => {
  const SUG = { name: 'dispatch-voice-kokoro', licence: 'GPL-3.0-or-later',
    install: [{ where: 'plugin', cmd: 'uv run dispatch-voice-kokoro fetch-models' }, { where: 'env', cmd: 'DISPATCH_VOICE_EXTENSIONS=dispatch_voice_kokoro:setup' }],
    enable: [{ where: 'env', cmd: 'DISPATCH_VOICE_EXTENSIONS=dispatch_voice_kokoro:setup' }] };
  const status = { enabled: false, switch: false, default_voice: 'builtin:am_michael', plugins: { builtin: { installed: false, package: false, missing: [], suggested: SUG } } };
  globalThis.fetch = fakeFetch({ '/api/voice/status': () => status });
  await mountVoiceTool(host, 'voice-builtin', {});
  let code = [...host.querySelectorAll('.voice-cmd code')].map((c) => c.textContent).join('\n');
  assert.match(code, /dispatch-voice-kokoro fetch-models/);
  assert.match(code, /DISPATCH_VOICE_EXTENSIONS=dispatch_voice_kokoro:setup/);

  status.plugins.builtin = { installed: false, package: true, missing: [], suggested: SUG };
  await mountVoiceTool(host, 'voice-builtin', {});
  code = [...host.querySelectorAll('.voice-cmd code')].map((c) => c.textContent);
  assert.deepEqual(code, ['DISPATCH_VOICE_EXTENSIONS=dispatch_voice_kokoro:setup']);

  status.plugins.builtin = { installed: true, package: true, missing: [], suggested: SUG, name: 'dispatch-voice-kokoro', licence: 'GPL-3.0-or-later' };
  status.builtin_voices = [{ id: 'builtin:af_heart', name: 'Heart', lang: 'en-us' }, { id: 'builtin:am_michael', name: 'Michael', lang: 'en-us' }];
  await mountVoiceTool(host, 'voice-builtin', {});
  const rows = [...host.querySelectorAll('.vtool-voice-row')];
  assert.deepEqual(rows.map((r) => r.dataset.voice), ['builtin:af_heart', 'builtin:am_michael']);
  assert.ok(rows[1].querySelector('.voices-default'), 'the default carries the badge');
  assert.ok(rows.every((r) => r.querySelector('.drive-preview').disabled), 'no samples while voice is off');
  assert.ok(host.querySelector('#vtool-voice-off'), 'and says how to turn it on');
  unmountVoiceTool();
}, { fetch: fakeFetch({}) }));

test('Voice lab: status, timing sliders in the server ranges, show-timings, test needs voice', dom, () => withDom(async (doc, host) => {
  const status = { enabled: false, switch: false, profiles: [], builtin_voices: [] };
  const lab = {
    switch: false, forced: null, enabled: false, ready: true, missing: [], stt: 'parakeet', smart_turn: true,
    tts: 'auto', tts_resolved: 'chatterbox', fallback: null, engines: ['chatterbox'], loaded: false, warm: 'idle',
    minimax: { selected: false, key_set: false, model: 'speech-2.8-turbo', voice: 'Wise_Woman' },
    hq: { configured: true, host: 'rig:8000', model: 'qwen3-tts', timeout_s: 4 },
    thinking: 'off', thinking_levels: ['off', 'low', 'medium'],
    timings: {
      merge_window_s: { value: 1.8, default: 1.8, min: 0.5, max: 4 },
      hard_silence_s: { value: 1.4, default: 1.4, min: 0.8, max: 3 },
    },
  };
  globalThis.fetch = fakeFetch({ '/api/voice/status': status, '/api/voice/lab': lab });
  await mountVoiceTool(host, 'voice-lab', {});
  const merge = host.querySelector('#vtool-merge-input');
  assert.deepEqual([merge.min, merge.max, merge.value], ['0.5', '4', '1.8']);
  const hard = host.querySelector('#vtool-hard-input');
  assert.deepEqual([hard.min, hard.max, hard.value], ['0.8', '3', '1.4']);
  assert.ok(host.querySelector('#vtool-thinking select'));
  assert.ok(host.querySelector('#vtool-reset'));
  const timings = host.querySelector('#vtool-show-timings input[type="checkbox"]');
  assert.ok(timings && !timings.checked);
  timings.checked = true;
  timings.dispatchEvent(new doc.defaultView.Event('change'));
  assert.equal(JSON.parse(globalThis.localStorage.getItem('dispatch-drive')).showTimings, true);
  assert.equal(host.querySelector('#vtool-test-btn').disabled, true, 'no test while voice is off');
  // Cloud and rig engines are status only here: no input to type a key or
  // an address into (they stay in the server's environment).
  assert.equal(host.querySelector('#vtool-hq input, #vtool-minimax input'), null);
  unmountVoiceTool();
  lab.enabled = true; lab.switch = true; status.enabled = true;
  await mountVoiceTool(host, 'voice-lab', {});
  assert.equal(host.querySelector('#vtool-test-btn').disabled, false);
  unmountVoiceTool();
}, { fetch: fakeFetch({}) }));

test('an unknown tool id mounts nothing', dom, () => withDom(async (doc, host) => {
  await mountVoiceTool(host, 'voice-nope', {});
  assert.equal(host.textContent, 'old');
}));

test('createVoicesPane without embedded still takes over its pane with a back row', dom, () => withDom(async (doc, host) => {
  globalThis.fetch = fakeFetch({ '/api/voices': { voices: [] }, '/api/voice/status': { builtin_voices: [] } });
  const p = createVoicesPane(host, { toast() {}, onChanged() {} });
  p.open();
  await settle();
  assert.ok(host.querySelector('.voices-back'));
  assert.equal(host.querySelector('p').hidden, true, 'the pane\'s own rows hide underneath');
  p.close();
  assert.equal(host.querySelector('p').hidden, false);
}, { fetch: fakeFetch({}) }));
