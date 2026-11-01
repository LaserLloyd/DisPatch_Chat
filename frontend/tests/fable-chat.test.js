// 4.3 Fable review, chat/Characters/agents/Image Studio fixes. Each test was seen red first.
//
// Run: node --test frontend/tests/fable-chat.test.js

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const JS = join(HERE, '..', 'static', 'js');
const read = (p) => readFileSync(join(JS, p), 'utf8');
const MAIN = read('main.js');
const EN = JSON.parse(readFileSync(join(HERE, '..', 'static', 'locales', 'en.json'), 'utf8'));

test('B1: a toast never shows the HTTP status prefix ("409: Hello" -> "Hello")', async () => {
  const util = await import('../static/js/util.js');
  assert.equal(util.toastText('409: Hello'), 'Hello');
  assert.equal(util.toastText('Hello 409: x'), 'Hello 409: x');
  assert.equal(util.errorText({ message: '403: No.', fix: 'Ask an Admin.' }), 'No. Ask an Admin.');
  const fn = MAIN.slice(MAIN.indexOf('function toast('), MAIN.indexOf('function toast(') + 600);
  assert.match(fn, /msg = toastText\(msg\)/);
  // The per-module copies are gone from the files that only fed toasts.
  for (const f of ['agents.js', 'tasks.js']) assert.doesNotMatch(read(f), /replace\(\/\^\\d\{3\}/, f);
});

// ------------------------------------------------------------------ DOM harness (agents pane)
import { createRequire } from 'node:module';
import { domSkip } from './_require-dom.js';

const require = createRequire(import.meta.url);
let jsdom = null;
try { jsdom = require('jsdom'); } catch { /* DOM tests skip */ }
const dom = { skip: domSkip(jsdom ? false : 'jsdom is not installed') };
const tick = (ms = 0) => new Promise((r) => setTimeout(r, ms));

async function boot(html = '<div id="host"></div>') {
  const { JSDOM } = jsdom;
  const win = new JSDOM(`<!doctype html><body>${html}</body>`, { url: 'http://127.0.0.1:18850/', pretendToBeVisual: true }).window;
  const put = (k, v) => Object.defineProperty(globalThis, k, { value: v, configurable: true, writable: true });
  for (const k of ['window', 'document', 'location', 'navigator', 'localStorage', 'Node', 'HTMLElement']) put(k, k === 'window' ? win : win[k]);
  put('requestAnimationFrame', (fn) => setTimeout(fn, 0));
  put('cancelAnimationFrame', (id) => clearTimeout(id));
  globalThis.fetch = async (u) => {
    if (String(u).endsWith('/static/locales/en.json')) return { ok: true, status: 200, json: async () => EN, headers: new win.Headers() };
    return { ok: false, status: 404, json: async () => ({}), headers: new win.Headers() };
  };
  const i18n = await import('../static/js/i18n.js?v=5');
  await i18n.init('en');
  return win;
}

function agentsApi({ agents = [], routes = null } = {}) {
  const rec = (ret) => async () => (typeof ret === 'function' ? ret() : ret);
  return {
    me: rec({ role: 'admin', protected: true, permissions: ['manage_agents', 'dispatch_agents'] }),
    agents: rec({ agents, profiles: [] }),
    agentBackends: rec({ backends: [
      { name: 'openclaw', label: 'OpenClaw', available: true, capabilities: {} },
      { name: 'claude_sdk', label: 'Claude Agent', available: false, capabilities: {} },
      { name: 'cli', label: 'Command-line agent', available: true, capabilities: {} },
      { name: 'managed', label: 'Managed', available: true, capabilities: {} },
    ] }),
    claudeCredentials: rec({ route: 'api_key', routes: ['api_key', 'bedrock'], api_key_set: false, api_key_source: null, provider_env: '' }),
    agentSettings: rec({ retention_days: 30, max_concurrent: 4, default_max_usd: 2, default_max_turns: 60, default_max_usd_per_day: 10, approval_timeout_s: 1800 }),
    agentUsage: rec({ day: '2026-10-31', estimate: true, spend: [] }),
    agentSecrets: rec({ secrets: [] }),
    claudeRoutes: rec(routes || { current: 'api_key', routes: [
      { route: 'api_key', available: true, missing: [] },
      { route: 'bedrock', available: false, missing: ['AWS_REGION'] },
    ] }),
    cliPresets: rec({ presets: [] }),
  };
}

async function mountAgents(opts) {
  const win = await boot();
  const A = await import('../static/js/agents.js?v=4');
  const host = win.document.getElementById('host');
  await A.mountAgentsSettings(host, { api: agentsApi(opts), isOwner: true, toast: () => {}, confirm: async () => true });
  await tick(5);
  return { win, host };
}

test('B2: the agent editor\'s backend note follows the Backend select', { skip: dom.skip }, async () => {
  const { win, host } = await mountAgents();
  host.querySelector('#agents-new').click();
  const form = host.querySelector('.agents-form');
  const sel = [...form.querySelectorAll('select')].find((s) => [...s.options].some((o) => o.value === 'cli'));
  const note = () => sel.parentElement.querySelector('.people-field-hint');
  sel.value = 'openclaw'; sel.dispatchEvent(new win.Event('change'));
  assert.equal(note().textContent, EN.agents.backend_note_openclaw);
  sel.value = 'cli'; sel.dispatchEvent(new win.Event('change'));
  assert.equal(note().textContent, EN.agents.backend_note_cli);
  sel.value = 'managed'; sel.dispatchEvent(new win.Event('change'));
  assert.equal(note().textContent, EN.agents.backend_note_managed);
});

test('B3: the MCP "Type" select says what http means', { skip: dom.skip }, async () => {
  const { host } = await mountAgents();
  host.querySelector('#agents-new').click();
  const add = [...host.querySelectorAll('.agents-form button')].find((b) => b.textContent === EN.agents.add_mcp);
  add.click();
  const opt = host.querySelector('.agents-mcp-row option[value="http"]');
  assert.equal(opt.textContent, 'A web address (http)');
});

test('B4: no agents = a styled empty box holding the New agent button; route names sit in a fold', { skip: dom.skip }, async () => {
  const { host } = await mountAgents();
  const box = host.querySelector('.agents-empty');
  assert.ok(box, 'empty-state box');
  assert.ok(box.querySelector('#agents-new'), 'button inside the box');
  assert.doesNotMatch(host.textContent, /null/);
  const needs = host.querySelector('.agents-route-needs');
  assert.ok(needs.closest('details.agents-route-details'), 'raw names only inside Show details');
});

test('B6: "Archive chats older than" says "1 month" (not "1 months") and "Never" for off', { skip: dom.skip }, async () => {
  const win = await boot();
  const i18n = await import('../static/js/i18n.js?v=5');
  const { el } = await import('../static/js/util.js');
  const P = await import('../static/js/thread-pager.js');
  const host = win.document.getElementById('host');
  const api = { myArchive: async () => ({ months: 0, would_archive: 1 }) };
  P.renderArchiveControl(host, { api, t: i18n.t, el, toast: () => {} });
  await tick(5);
  const texts = [...host.querySelectorAll('option')].map((o) => o.textContent);
  assert.equal(texts[0], 'Never');
  assert.ok(texts.includes('1 month'), texts.join('|'));
  assert.ok(texts.some((x) => /^\d+ months$/.test(x)));
  assert.equal(i18n.t('archive.would', { count: 1 }), '1 chat would be archived now.');
});

test('B7: deleting a chat asks with a "Delete" button, not "Confirm"', () => {
  assert.match(MAIN, /uiConfirm\(t\('chat\.delete_confirm'\), \{ danger: true, okText: t\('common\.delete'\) \}\)/);
});

test('B8: the lightbox controls show for about 2 s when it opens, then fade as before', async () => {
  const { createChrome } = await import('../static/js/lightbox-nav.js');
  const timers = [];
  const seen = [];
  const c = createChrome({ delay: 700, onChange: (v) => seen.push(v), setTimer: (fn, ms) => { timers.push({ fn, ms }); return timers.length; }, clearTimer: () => {} });
  c.open();
  assert.equal(c.visible, true);
  assert.equal(timers.at(-1).ms, 2000);
  timers.at(-1).fn();
  assert.equal(c.visible, false);
  assert.deepEqual(seen, [true, false]);
  // main.js opens the lightbox with the controls up.
  const at = MAIN.indexOf('document.body.append(lb);');
  assert.match(MAIN.slice(at, at + 400), /chrome\.open\(\)/);
});

test('B9: the phone "More" button on a message is ⋯ (like the header), and Copy/More are at least 40 px on touch', () => {
  const fn = MAIN.slice(MAIN.indexOf('function moreMsgBtn('), MAIN.indexOf('function moreMsgBtn(') + 500);
  assert.match(fn, /spriteIcon\('ellipsis'/);
  assert.doesNotMatch(fn, /RAIL_ICONS\.menu/);
  const css = readFileSync(join(HERE, '..', 'static', 'app.css'), 'utf8');
  const m = css.match(/@media \(hover: none\), \(max-width: 768px\) \{\s*\.msg-icon-btn \{ width: (\d+)px; height: (\d+)px; \}/);
  assert.ok(m, 'a touch/narrow size rule for .msg-icon-btn');
  assert.ok(Number(m[1]) >= 40 && Number(m[2]) >= 40);
  assert.doesNotMatch(css, /\.msg-icon-btn \{ width: 36px; height: 30px; \}/);
});

test('B10: the thread-list header says "Pick a bot"; a bot\'s model id is shown to Admins only', () => {
  assert.equal(EN.threads.pick_bot, 'Pick a bot');
  const html = readFileSync(join(HERE, '..', 'static', 'index.html'), 'utf8');
  assert.match(html, /data-i18n="threads\.pick_bot">Pick a bot</);
  assert.match(MAIN, /function botModelLine\(bot\) \{\s*return bot && bot\.model_hint && state\.role === 'admin' \? bot\.model_hint : '';/);
  assert.match(MAIN, /dom\['tl-model'\]\.textContent = botModelLine\(bot\);/);
  assert.match(MAIN, /sub: botModelLine\(b\), group: t\('cmdk\.group_bots'\)/);
  assert.doesNotMatch(MAIN, /wbot && wbot\.model_hint \?/);
});

test('B12: the Settings Agents card says schedules are on the Tasks page, and opens it', { skip: dom.skip }, async () => {
  const win = await boot();
  const A = await import('../static/js/agents.js?v=4');
  const host = win.document.getElementById('host');
  let opened = 0;
  await A.mountAgentsSettings(host, { api: agentsApi(), isOwner: true, toast: () => {}, confirm: async () => true, openTasks: () => { opened += 1; } });
  await tick(5);
  const link = host.querySelector('.agents-tasks-link');
  assert.ok(link);
  assert.equal(link.textContent, 'Schedules are on the Tasks page');
  link.click();
  assert.equal(opened, 1);
  assert.match(MAIN, /openTasks: \(\) => \{ closeBotManager\(\); tasksUi\.openPage\(\); \}/);
  assert.match(MAIN, /openSettings: \(target\) => openSettingsTab\(target\)/);
});

test('B13: the coding-agent pane subtitle shows no internal address', () => {
  assert.equal(EN.harness.subtitle, 'Coding agent');
  assert.doesNotMatch(MAIN, /t\('harness\.subtitle', \{ port/);
  const html = readFileSync(join(HERE, '..', 'static', 'index.html'), 'utf8');
  assert.match(html, /data-i18n="harness\.subtitle">Coding agent</);
});

test('B14: a visible paperclip beside ⚡ opens the file picker; ⚡ keeps its Upload entry', async () => {
  const html = readFileSync(join(HERE, '..', 'static', 'index.html'), 'utf8');
  const btn = html.match(/<button[^>]*id="attach-btn"[^>]*>.*?<\/button>/s)[0];
  assert.match(btn, /icons\.svg#paperclip/);
  assert.match(btn, /data-i18n-attr="title=composer\.attach;aria-label=composer\.attach"/);
  assert.match(btn, /title="Attach a file"/);
  assert.equal(EN.composer.attach, 'Attach a file');
  // It sits right before the ⚡ wrapper in the composer row.
  assert.ok(html.indexOf('id="attach-btn"') < html.indexOf('id="quick-btn"'));
  const css = readFileSync(join(HERE, '..', 'static', 'app.css'), 'utf8');
  assert.doesNotMatch(css, /\.attach-btn\.qa-folded \{ display: none/);
  const qa = read('quick-actions.js');
  assert.doesNotMatch(qa, /classList\.toggle\('qa-folded'/, 'the paperclip no longer folds away');
  assert.match(MAIN, /dom\['attach-btn'\]\.addEventListener\('click', \(\) => dom\['file-input'\]\.click\(\)\);/);
  assert.match(MAIN, /setQuickUpload\(/);
});
