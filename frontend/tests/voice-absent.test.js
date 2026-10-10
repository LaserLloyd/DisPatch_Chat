// Settings → Voice while the voice add-on is not running (js/voice-absent.js,
// SPEC §7.2): which state the page is in, what it says in each, that the
// install commands follow the moving `v1` tag of the add-on's repository, and
// that every string it needs exists in all eight locales.

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { domSkip } from './_require-dom.js';

const require = createRequire(import.meta.url);
let jsdom = null;
try { jsdom = require('jsdom'); } catch { /* not installed — DOM tests skip */ }
const dom = { skip: domSkip(jsdom ? false : 'jsdom is not installed') };

const HERE = dirname(fileURLToPath(import.meta.url));
const STATIC = join(HERE, '..', 'static');
const LANGS = ['en', 'ar', 'de', 'es', 'fr', 'ja', 'pt', 'zh'];
const SRC = readFileSync(join(STATIC, 'js', 'voice-absent.js'), 'utf8');

const load = async () => import(`../static/js/voice-absent.js?t=${Math.random()}`);

test('the install line follows the add-on repository and the moving v1 tag', async () => {
  const m = await load();
  assert.equal(m.VOICE_REPO_URL, 'https://github.com/LaserLloyd/DisPatch_Voice');
  assert.equal(m.VOICE_TAG, 'v1');
  assert.equal(m.COMMANDS.install,
    'uv pip install --python .venv/bin/python "dispatch-voice @ git+https://github.com/LaserLloyd/DisPatch_Voice@v1"');
  assert.match(m.COMMANDS.upgrade, /--upgrade "dispatch-voice @ git\+https:\/\/github\.com\/LaserLloyd\/DisPatch_Voice@v1"$/);
  assert.equal(m.COMMANDS.models, '.venv/bin/dispatch-voice fetch-models');
  // The uv-run variant keeps the two unit flags the live box depends on.
  assert.match(m.COMMANDS.uvRun, /--offline --with-editable .* --ws-max-size 262144$/);
  assert.equal(m.COMMANDS.dockerModels, 'docker compose exec app dispatch-voice fetch-models');
  assert.match(m.VOICE_GUIDE_URL, /docs\/extensions\.md#voice$/);
  // An exact pin belongs in the docs' reproducible section only.
  assert.doesNotMatch(Object.values(m.COMMANDS).join('\n'), /@v1\.\d/);
});

test('voiceState: absent, ok, failed, incompatible, disabled, client failure', async () => {
  const { voiceState } = await load();
  assert.deepEqual(voiceState([], 'entry_points'), { state: 'absent', ext: null, mode: 'entry_points' });
  assert.equal(voiceState(null, null).state, 'absent');
  const ok = { id: 'voice', state: 'ok' };
  assert.equal(voiceState([ok], 'env').state, 'ok');
  assert.equal(voiceState([ok], 'env', { detail: 'x' }).state, 'client_failed');
  assert.equal(voiceState([{ id: 'voice', state: 'failed' }]).state, 'failed');
  assert.equal(voiceState([{ id: 'voice', state: 'incompatible' }]).state, 'incompatible');
  assert.equal(voiceState([{ id: 'voice', state: 'disabled' }], 'off').state, 'disabled');
  // A duplicate that failed beside a healthy one is not the story.
  assert.equal(voiceState([{ id: 'voice', state: 'failed' }, ok]).state, 'ok');
  // Another extension is not voice.
  assert.equal(voiceState([{ id: 'other', state: 'ok' }]).state, 'absent');
});

test('every voice_absent key the page uses exists in all eight locales', () => {
  const used = [...new Set([...SRC.matchAll(/t\('(voice_absent\.[a-z_]+)'/g)].map((m) => m[1]))];
  assert.ok(used.length >= 30, `found only ${used.length} keys`);
  for (const lang of LANGS) {
    const d = JSON.parse(readFileSync(join(STATIC, 'locales', `${lang}.json`), 'utf8'));
    for (const key of used) {
      const v = key.split('.').reduce((n, k) => (n ? n[k] : undefined), d);
      assert.equal(typeof v, 'string', `${lang}: ${key}`);
      assert.ok(v.trim(), `${lang}: ${key} is empty`);
    }
    // The contract's keys (CONTRACTS §E.3), whatever the page uses today.
    for (const k of ['title', 'lead', 'state_incompatible', 'state_failed', 'state_disabled', 'step_install',
      'step_models', 'step_restart', 'optional_kokoro', 'links_repo', 'links_guide', 'docker_note',
      'bare_metal_note', 'could_not_load', 'check_again', 'uv_sync_note', 'english_only', 'latest_release']) {
      assert.equal(typeof d.voice_absent[k], 'string', `${lang}: voice_absent.${k}`);
    }
    // Commands are never translated: a translated string must not carry one.
    assert.doesNotMatch(JSON.stringify(d.voice_absent), /pip install|fetch-models/);
    // The user-facing name is "Voice chat" — never "Drive mode".
    assert.doesNotMatch(JSON.stringify(d.voice_absent), /Drive|Fahrmodus|conduite|conducción|condução|ドライブ|驾驶|القيادة/);
  }
});

function withDom(fn) {
  return async () => {
    const win = new jsdom.JSDOM('<!doctype html><html><body><div id="root"></div></body></html>').window;
    const keep = { document: globalThis.document, window: globalThis.window, fetch: globalThis.fetch };
    globalThis.document = win.document;
    globalThis.window = win;
    try {
      const en = JSON.parse(readFileSync(join(STATIC, 'locales', 'en.json'), 'utf8'));
      const i18n = await import('../static/js/i18n.js?v=4');
      globalThis.fetch = async () => ({ ok: true, json: async () => en });
      await i18n.init('en');
      await fn(win, win.document.getElementById('root'));
    } finally { Object.assign(globalThis, keep); }
  };
}

test('absent: what it is, three steps with copyable commands, Check again', dom, withDom(async (win, root) => {
  const m = await load();
  let checks = 0;
  m.renderVoiceAbsent(root, m.voiceState([], 'entry_points'), { onCheckAgain: () => { checks += 1; } });
  const text = root.textContent;
  assert.match(text, /Voice chat is an add-on/);
  assert.match(text, /English only for now/);
  assert.match(text, /1\. Install the latest 1\.x release into DisPatch/);
  assert.match(text, /2\. Fetch the speech models/);
  assert.match(text, /3\. Restart DisPatch, then check again/);
  assert.match(text, /uv sync/);
  const cmds = [...root.querySelectorAll('pre.ext-cmd code')].map((c) => c.textContent);
  assert.ok(cmds.includes(m.COMMANDS.install) && cmds.includes(m.COMMANDS.models));
  // Commands read left-to-right even under Arabic: the row (command + its
  // Copy button) carries dir="ltr", so the button stays at the line's end.
  for (const pre of root.querySelectorAll('pre.ext-cmd')) {
    assert.equal(pre.closest('.ext-cmd-row').getAttribute('dir'), 'ltr');
  }
  // Same components as the rest of the Settings modal.
  assert.ok(root.querySelectorAll('section.dev-group > .dev-group-body > .dev-row').length >= 5);
  assert.equal(root.querySelectorAll('details.ext-details').length, 3);
  const btn = root.querySelector('button.ext-check-again');
  assert.ok(btn.classList.contains('btn-primary'));
  btn.click();
  assert.equal(checks, 1);
  const links = [...root.querySelectorAll('a.ext-link')];
  assert.equal(links[0].href, m.VOICE_REPO_URL);
  for (const a of links) assert.equal(a.getAttribute('rel'), 'noopener noreferrer');
  assert.equal(root.dataset.voiceState, 'absent');
  // No markup is ever parsed from a string: the backticked `uv sync` is a <code>.
  assert.ok([...root.querySelectorAll('.dev-row-hint code')].some((c) => c.textContent === 'uv sync'));
}));

test('absent + busy / not found yet', dom, withDom(async (win, root) => {
  const m = await load();
  m.renderVoiceAbsent(root, m.voiceState([], null), { busy: true });
  const b = root.querySelector('button.ext-check-again');
  assert.equal(b.disabled, true);
  assert.equal(b.textContent, 'Checking…');
  m.renderVoiceAbsent(root, m.voiceState([], null), { notYet: true });
  assert.match(root.textContent, /Still not running/);
}));

test('failed: the server detail, the fix, and nothing else to install', dom, withDom(async (win, root) => {
  const m = await load();
  const ext = { id: 'voice', name: 'DisPatch Voice', version: '1.0.0', state: 'failed',
    detail: "ModuleNotFoundError: No module named 'onnxruntime'" };
  m.renderVoiceAbsent(root, m.voiceState([ext]), {});
  assert.match(root.textContent, /DisPatch Voice 1\.0\.0 is installed but could not start/);
  assert.match(root.textContent, /onnxruntime/);
  assert.match(root.textContent, /full error is in the server log/);
  assert.equal(root.querySelectorAll('pre.ext-cmd').length, 0);
  assert.ok(root.querySelector('button.ext-check-again'));
}));

test('incompatible: which side to update', dom, withDom(async (win, root) => {
  const m = await load();
  const old = { id: 'voice', name: 'Old Voice', version: '0.1.0', state: 'incompatible',
    detail: 'Old Voice 0.1.0 needs DisPatch extension API 0..0; this DisPatch has 1. Update the add-on.' };
  m.renderVoiceAbsent(root, m.voiceState([old]), {});
  assert.match(root.textContent, /does not fit this DisPatch/);
  assert.equal(root.querySelector('pre.ext-cmd code').textContent, m.COMMANDS.upgrade);
  const fut = { ...old, name: 'Future Voice', detail: '… this DisPatch has 1. Update DisPatch.' };
  m.renderVoiceAbsent(root, m.voiceState([fut]), {});
  assert.match(root.textContent, /Update DisPatch to its latest release/);
  assert.equal(root.querySelectorAll('pre.ext-cmd').length, 0);
  // A browser-side handshake mismatch (server ok, JS EXT.api excludes the
  // shell) gets the same which-side copy, not "could not load" (review L5).
  const ok = { id: 'voice', name: 'DisPatch Voice', version: '1.0.0', state: 'ok' };
  const cf = { state: 'incompatible',
    detail: 'DisPatch Voice 1.0.0 needs DisPatch extension API 0..0; this DisPatch has 1. Update the add-on.' };
  const info = m.voiceState([ok], 'entry_points', cf);
  assert.equal(info.state, 'incompatible');
  m.renderVoiceAbsent(root, info, { clientDetail: cf.detail });
  assert.match(root.textContent, /does not fit this DisPatch/);
  assert.doesNotMatch(root.textContent, /could not load/);
  assert.equal(root.querySelector('pre.ext-cmd code').textContent, m.COMMANDS.upgrade);
  // A missing EXT handshake is still "could not load".
  assert.equal(m.voiceState([ok], 'entry_points', { state: 'incompatible', detail: 'no EXT handshake' }).state,
    'client_failed');
}));

test('disabled, and installed-but-cannot-load-here', dom, withDom(async (win, root) => {
  const m = await load();
  m.renderVoiceAbsent(root, m.voiceState([{ id: 'voice', state: 'disabled' }], 'off'), {});
  assert.match(root.textContent, /Extensions are turned off on this server/);
  assert.match(root.textContent, /dispatch_voice:EXTENSION/);
  m.renderVoiceAbsent(root, m.voiceState([], 'off'), {});
  assert.match(root.textContent, /turned off/);
  assert.match(root.textContent, /1\. Install/);
  m.renderVoiceAbsent(root, m.voiceState([{ id: 'voice', state: 'ok' }], 'env', { detail: 'x' }),
    { clientDetail: 'Failed to fetch dynamically imported module' });
  assert.match(root.textContent, /could not load/);
  assert.match(root.textContent, /Failed to fetch/);
}));

test('every locale renders the page without a missing key', dom, withDom(async (win, root) => {
  const i18n = await import('../static/js/i18n.js?v=4');
  const m = await load();
  for (const lang of LANGS) {
    const d = JSON.parse(readFileSync(join(STATIC, 'locales', `${lang}.json`), 'utf8'));
    const en = JSON.parse(readFileSync(join(STATIC, 'locales', 'en.json'), 'utf8'));
    globalThis.fetch = async (url) => ({ ok: true, json: async () => (String(url).includes('/en.json') ? en : d) });
    await i18n.setLocale(lang);
    const warn = console.warn;
    const missing = [];
    console.warn = (msg) => { if (String(msg).includes('missing key')) missing.push(msg); };
    try {
      for (const st of [[], [{ id: 'voice', state: 'failed', detail: 'x' }],
        [{ id: 'voice', state: 'incompatible', detail: 'Update the add-on.' }],
        [{ id: 'voice', state: 'disabled' }]]) {
        m.renderVoiceAbsent(root, m.voiceState(st, 'entry_points'), {});
        assert.ok(root.textContent.length > 40, lang);
      }
    } finally { console.warn = warn; }
    assert.deepEqual(missing, [], lang);
  }
  await i18n.setLocale('en');
}));
