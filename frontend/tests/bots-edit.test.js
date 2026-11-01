// Settings -> Characters -> Add (js/bots-edit.js): the POST request shape of
// docs/design/platform-2-3.md section 3, driven through the REAL api client against a
// stubbed fetch, with real English strings. (4.3: the row editing it once carried went with
// the old roster; the Characters page edits rows.)

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

let calls = [];
let routes = {};

async function setup(extra = {}) {
  const win = new jsdom.JSDOM(HTML, { url: 'http://127.0.0.1:8765/', runScripts: 'outside-only' }).window;
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
  routes = { 'GET /static/locales/en.json': () => [200, EN], ...extra };
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
  const i18n = await import('../static/js/i18n.js?v=5');
  await i18n.init('en');
  calls.length = 0;
  const mod = await import(`../static/js/bots-edit.js?t=${Math.random()}`);
  const rec = { toasts: [], patched: [], removed: [], added: [], ai: 0, confirms: [], answer: true };
  const ctx = {
    toast: (m, err) => rec.toasts.push([m, !!err]),
    confirm: async (m, o) => { rec.confirms.push([m, o]); return rec.answer; },
    openAi: () => { rec.ai += 1; },
    onPatched: (id, row) => rec.patched.push([id, row]),
    onRemoved: (id) => rec.removed.push(id),
    onAdded: (row) => rec.added.push(row),
  };
  return { win, mod, rec, ctx };
}

const tick = async () => { for (let i = 0; i < 4; i++) await new Promise((r) => setTimeout(r, 0)); };
const fire = (win, node, type = 'click') => node.dispatchEvent(new win.Event(type, { bubbles: true }));

test('add bot: a direct-API bot POSTs {name, emoji, backend}', { skip: dom.skip }, async () => {
  const row = { id: 'helper', name: 'Helper', backend: 'direct', tools: true, connected: false, visible: true };
  const { mod, ctx, rec, win } = await setup({ 'POST /api/bots': () => [201, row] });
  const node = mod.addBotRow(ctx);
  const q = (k) => node.querySelector(`[data-bm="${k}"]`);
  assert.ok(q('add-form').classList.contains('hidden'));
  fire(win, q('add-open'));
  assert.ok(!q('add-form').classList.contains('hidden'));
  assert.ok(q('add-open').classList.contains('hidden'));
  // The agent id field is hidden for a direct-API bot.
  assert.ok(q('add-agent').closest('label').classList.contains('hidden'));
  q('add-name').value = ' Helper ';
  q('add-emoji').value = '🛟';
  fire(win, q('add-confirm'));
  await tick();
  assert.deepEqual(calls, [{ key: 'POST /api/bots', body: { name: 'Helper', backend: 'direct', emoji: '🛟' } }]);
  assert.deepEqual(rec.added, [row]);
  assert.deepEqual(rec.toasts.at(-1), ['Helper added.', false]);
});

test('add bot: an agent bot needs a valid agent id, and sends it', { skip: dom.skip }, async () => {
  const row = { id: 'scribe', name: 'Scribe', backend: 'agent' };
  const { mod, ctx, rec, win } = await setup({ 'POST /api/bots': () => [201, row] });
  const node = mod.addBotRow(ctx);
  const q = (k) => node.querySelector(`[data-bm="${k}"]`);
  fire(win, q('add-open'));
  q('add-name').value = 'Scribe';
  q('add-backend').value = 'agent';
  fire(win, q('add-backend'), 'change');
  assert.ok(!q('add-agent').closest('label').classList.contains('hidden'));
  assert.match(node.querySelector('.bm-add-hint').textContent, /OpenClaw server/);
  fire(win, q('add-confirm'));
  await tick();
  assert.equal(calls.length, 0, 'no agent id yet');
  assert.match(rec.toasts.at(-1)[0], /Enter the agent's name/);
  q('add-agent').value = 'not valid!';
  fire(win, q('add-confirm'));
  await tick();
  assert.equal(calls.length, 0);
  q('add-agent').value = 'scribe_2';
  fire(win, q('add-confirm'));
  await tick();
  assert.deepEqual(calls, [{ key: 'POST /api/bots', body: { name: 'Scribe', backend: 'agent', agent: 'scribe_2' } }]);
  assert.deepEqual(rec.added, [row]);
});

test('add bot: a name is required; a server refusal is shown and nothing is added', { skip: dom.skip }, async () => {
  const { mod, ctx, rec, win } = await setup({ 'POST /api/bots': () => [409, { detail: 'id taken' }] });
  const node = mod.addBotRow(ctx);
  const q = (k) => node.querySelector(`[data-bm="${k}"]`);
  fire(win, q('add-open'));
  fire(win, q('add-confirm'));
  await tick();
  assert.equal(calls.length, 0);
  assert.deepEqual(rec.toasts.at(-1), ['Give the bot a name.', true]);
  q('add-name').value = 'Dup';
  fire(win, q('add-confirm'));
  await tick();
  assert.deepEqual(rec.added, []);
  assert.deepEqual(rec.toasts.at(-1), ['Could not add the bot: id taken', true]);
});

test('api.js carries the Add call', async () => {
  const src = readFileSync(join(STATIC, 'js', 'api.js'), 'utf8');
  assert.match(src, /addBot: \(body\) => j\('\/api\/bots', \{ method: 'POST'/);
});

test('main.js: Settings -> Characters -> Add opens this form; the rows are the Characters page\'s', () => {
  assert.match(MAIN, /import \{ addBotRow \} from '\.\/bots-edit\.js\?v=\d+';/);
  assert.match(MAIN, /showAddCharacter\(\)/);
  assert.match(MAIN, /const row = addBotRow\(\{/);
  assert.match(MAIN, /onAdded: \(r\) => \{ onCharacterAdded\(r\); host\.replaceChildren\(\); \},/);
  // A new character joins the rail at once and the page refetches.
  assert.match(MAIN, /function onCharacterAdded\(row\) \{[\s\S]{0,200}state\.bots\.push\(row\)[\s\S]{0,80}refreshCharactersPage\(\);/);
  assert.doesNotMatch(MAIN, /bmDirty|saveBotManager|'bm-done'|rowControls|doSaveRoster/);
});
