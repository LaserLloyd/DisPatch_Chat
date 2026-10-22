// Settings -> Bots inline editing (js/bots-edit.js): the PATCH / POST / DELETE
// request shapes of docs/design/platform-2-3.md section 3, driven through the
// REAL api client against a stubbed fetch, with real English strings.

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
  const i18n = await import('../static/js/i18n.js?v=4');
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

const DIRECT = { id: 'atlas', name: 'Atlas', emoji: '🌍', model_hint: '', backend: 'direct', tools: true, connected: true };

test('backend kinds: the four badges, and an old server row (no backend) reads as an agent', { skip: dom.skip }, async () => {
  const { mod } = await setup();
  const label = (b) => mod.backendBadge(b).textContent;
  assert.equal(label({ backend: 'direct' }), 'Direct API');
  assert.equal(label({ backend: 'legacy-api' }), 'Legacy API');
  assert.equal(label({ backend: 'agent' }), 'Agent');
  assert.equal(label({ backend: 'app' }), 'App');
  assert.equal(label({}), 'Agent');
  assert.equal(mod.backendBadge({ backend: 'direct' }).dataset.backend, 'direct');
});

test('"Not connected: choose a connection" only for a direct-API bot with connected === false', { skip: dom.skip }, async () => {
  const { mod, ctx, rec, win } = await setup();
  assert.equal(mod.notConnectedLink({ ...DIRECT, connected: true }, ctx), null);
  assert.equal(mod.notConnectedLink({ ...DIRECT }, ctx), null);
  assert.equal(mod.notConnectedLink({ backend: 'agent', connected: false }, ctx), null);
  assert.equal(mod.notConnectedLink({ backend: 'legacy-api', connected: false }, ctx), null);
  const link = mod.notConnectedLink({ ...DIRECT, connected: false }, ctx);
  assert.equal(link.textContent, 'Not connected: choose a connection');
  fire(win, link);
  assert.equal(rec.ai, 1);
});

test('Lookups pill: direct-API bots only; a click PATCHes {tools} and reports the row', { skip: dom.skip }, async () => {
  const { mod, ctx, rec, win } = await setup({
    'PATCH /api/bots/atlas': () => [200, { ...DIRECT, tools: false }],
  });
  assert.equal(mod.lookupsPill({ backend: 'agent' }, ctx), null);
  assert.equal(mod.lookupsPill({ backend: 'legacy-api' }, ctx), null);
  const pill = mod.lookupsPill(DIRECT, ctx);
  assert.equal(pill.textContent, 'Lookups');
  assert.equal(pill.getAttribute('aria-pressed'), 'true');
  assert.ok(pill.classList.contains('on'));
  fire(win, pill);
  await tick();
  assert.deepEqual(calls, [{ key: 'PATCH /api/bots/atlas', body: { tools: false } }]);
  assert.equal(pill.getAttribute('aria-pressed'), 'false');
  assert.ok(!pill.classList.contains('on'));
  assert.equal(rec.patched[0][0], 'atlas');
  assert.equal(rec.patched[0][1].tools, false);
  // tools missing on the row reads as on (the default for a direct bot)
  assert.equal(mod.lookupsPill({ backend: 'direct', id: 'x' }, ctx).getAttribute('aria-pressed'), 'true');
});

test('Lookups pill: a refusal leaves it where it was and says why', { skip: dom.skip }, async () => {
  const { mod, ctx, rec, win } = await setup({ 'PATCH /api/bots/atlas': () => [422, { detail: 'nope' }] });
  const pill = mod.lookupsPill(DIRECT, ctx);
  fire(win, pill);
  await tick();
  assert.equal(pill.getAttribute('aria-pressed'), 'true');
  assert.equal(rec.patched.length, 0);
  assert.match(rec.toasts[0][0], /Could not save: nope/);
  assert.equal(rec.toasts[0][1], true);
});

test('edit: the pencil opens name / emoji / model fields and Save PATCHes only what changed', { skip: dom.skip }, async () => {
  const { mod, ctx, rec, win } = await setup({
    'PATCH /api/bots/atlas': () => [200, { ...DIRECT, name: 'Atlas 2', model_hint: 'MiniMax M3' }],
  });
  const c = mod.rowControls(DIRECT, ctx);
  const panel = c.panel;
  assert.ok(panel.classList.contains('hidden'));
  fire(win, c.edit);
  assert.ok(!panel.classList.contains('hidden'));
  const f = (k) => panel.querySelector(`[data-bm="${k}"]`);
  assert.equal(f('name').value, 'Atlas');
  assert.equal(f('emoji').value, '🌍');
  f('name').value = '  Atlas 2 ';
  f('model_hint').value = 'MiniMax M3';
  fire(win, f('edit-save'));
  await tick();
  assert.deepEqual(calls, [{ key: 'PATCH /api/bots/atlas', body: { name: 'Atlas 2', model_hint: 'MiniMax M3' } }]);
  assert.deepEqual(rec.toasts.at(-1), ['Saved.', false]);
  assert.equal(rec.patched[0][1].name, 'Atlas 2');
});

test('edit: nothing changed closes without a request; an empty name is refused client-side', { skip: dom.skip }, async () => {
  const { mod, ctx, rec, win } = await setup();
  const c = mod.rowControls(DIRECT, ctx);
  fire(win, c.edit);
  fire(win, c.panel.querySelector('[data-bm="edit-save"]'));
  await tick();
  assert.equal(calls.length, 0);
  assert.ok(c.panel.classList.contains('hidden'));
  fire(win, c.edit);
  c.panel.querySelector('[data-bm="name"]').value = '   ';
  fire(win, c.panel.querySelector('[data-bm="edit-save"]'));
  await tick();
  assert.equal(calls.length, 0);
  assert.deepEqual(rec.toasts.at(-1), ['Give the bot a name.', true]);
});

test('edit: Cancel throws the edits away', { skip: dom.skip }, async () => {
  const { mod, ctx, win } = await setup();
  const c = mod.rowControls(DIRECT, ctx);
  fire(win, c.edit);
  const name = c.panel.querySelector('[data-bm="name"]');
  name.value = 'Something else';
  fire(win, [...c.panel.querySelectorAll('button')].find((b) => b.textContent === 'Cancel'));
  assert.ok(c.panel.classList.contains('hidden'));
  fire(win, c.edit);
  assert.equal(name.value, 'Atlas');
  assert.equal(calls.length, 0);
});

test('remove: confirm first; Cancel sends nothing; OK DELETEs and reports', { skip: dom.skip }, async () => {
  const { mod, ctx, rec, win } = await setup({ 'DELETE /api/bots/atlas': () => [200, { removed: 'atlas' }] });
  const btn = mod.removeButton(DIRECT, ctx);
  rec.answer = false;
  fire(win, btn);
  await tick();
  assert.equal(calls.length, 0);
  assert.equal(rec.confirms.length, 1);
  assert.match(rec.confirms[0][0], /Remove Atlas\? Its chats stay/);
  assert.equal(rec.confirms[0][1].danger, true);
  rec.answer = true;
  fire(win, btn);
  await tick();
  assert.deepEqual(calls, [{ key: 'DELETE /api/bots/atlas', body: null }]);
  assert.deepEqual(rec.removed, ['atlas']);
  assert.deepEqual(rec.toasts.at(-1), ['Atlas removed.', false]);
});

test('remove: an app-owned bot answers 409 and gets its own message; the bot stays', { skip: dom.skip }, async () => {
  const { mod, ctx, rec, win } = await setup({ 'DELETE /api/bots/jobboard': () => [409, { detail: 'owned by app' }] });
  const btn = mod.removeButton({ id: 'jobboard', name: 'Job Board', backend: 'app' }, ctx);
  fire(win, btn);
  await tick();
  assert.deepEqual(rec.removed, []);
  assert.deepEqual(rec.toasts.at(-1), ['Job Board comes with an app and cannot be removed here.', true]);
  assert.equal(btn.disabled, false, 'can be retried');
});

test('remove: any other failure shows the server text without the status prefix', { skip: dom.skip }, async () => {
  const { mod, ctx, rec, win } = await setup({ 'DELETE /api/bots/atlas': () => [404, { detail: 'No such bot' }] });
  fire(win, mod.removeButton(DIRECT, ctx));
  await tick();
  assert.deepEqual(rec.toasts.at(-1), ['Could not remove: No such bot', true]);
});

test('bot ids and values are encoded into the path, never concatenated raw', { skip: dom.skip }, async () => {
  const { mod, ctx, win } = await setup({ 'DELETE /api/bots/a%2Fb%3F': () => [200, {}] });
  fire(win, mod.removeButton({ id: 'a/b?', name: 'x', backend: 'agent' }, ctx));
  await tick();
  assert.equal(calls[0].key, 'DELETE /api/bots/a%2Fb%3F');
});

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
  assert.match(node.querySelector('.bm-add-hint').textContent, /OpenClaw gateway/);
  fire(win, q('add-confirm'));
  await tick();
  assert.equal(calls.length, 0, 'no agent id yet');
  assert.match(rec.toasts.at(-1)[0], /Enter the agent's id/);
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

test('api.js carries the three bot calls', async () => {
  const src = readFileSync(join(STATIC, 'js', 'api.js'), 'utf8');
  assert.match(src, /addBot: \(body\) => j\('\/api\/bots', \{ method: 'POST'/);
  assert.match(src, /patchBot: \(id, patch\) => j\(`\/api\/bots\/\$\{encodeURIComponent\(id\)\}`, \{ method: 'PATCH'/);
  assert.match(src, /deleteBot: \(id\) => j\(`\/api\/bots\/\$\{encodeURIComponent\(id\)\}`, \{ method: 'DELETE' \}\)/);
});

test('main.js builds every Characters row: Hidden tag, Details, the ⋯ menu, drag and ▲/▼, instant save', () => {
  assert.match(MAIN, /import \{ rowControls, addBotRow, backendKind \} from '\.\/bots-edit\.js\?v=\d+';/);
  assert.match(MAIN, /const ctl = rowControls\(bot, botEditCtx\(\)\);/);
  // Rename / Personality / Remove are the ⋯ menu's, using the same controls bots-edit.js builds.
  assert.match(MAIN, /run: \(\) => ctl\.edit\.click\(\)/);
  assert.match(MAIN, /run: \(\) => ctl\.persona\.click\(\)/);
  // Remove goes through characters.js (delete-preview counts, Delete or Hide instead).
  assert.match(MAIN, /import \{ characterPanel, isHidden, removeCharacter \} from '\.\/characters\.js\?v=\d+';/);
  assert.match(MAIN, /const done = await removeCharacter\(bot, \{ toast \}\);/);
  assert.match(MAIN, /if \(done === 'deleted'\) await botEditCtx\(\)\.onRemoved\(bot\.id\);/);
  assert.doesNotMatch(MAIN, /bm-who|whoCanSee|WHO_OPTIONS|audienceLabel/);
  // Parking a character in Tools & more (this device only) moved from the ⌥ pill into the ⋯ menu.
  assert.match(MAIN, /label: t\(parked \? 'chars\.menu_unpark' : 'chars\.menu_park'\),\s+run: \(\) => \{ toggleMenuBot\(bot\.id\); renderSidebar\(\); renderBotManager\(\); \},/);
  // Add is a menu: a blank character, or a starter pack.
  assert.match(MAIN, /showAddCharacter\(\)/);
  assert.match(MAIN, /const row = addBotRow\(\{/);
  for (const part of ['moveBtn(-1), moveBtn(1)', 'pickAvatarFile(bot.id)', 'wireDrag(row);', 'avatar_pool: !!b.avatar_pool',
    "sw('chars.feat_reactions', 'reactions'", "sw('chars.feat_pool', 'avatar_pool'", "sw('chars.feat_pictures', 'image_jobs'"]) {
    assert.ok(MAIN.includes(part), part);
  }
  // Every change saves at once (no Save button, no unsaved-changes guard).
  assert.doesNotMatch(MAIN, /bmDirty|saveBotManager|'bm-done'/);
  assert.match(MAIN, /async function doSaveRoster\(/);
  // A removed bot drops out of both the roster and the sidebar list.
  assert.match(MAIN, /bmBots = bmBots\.filter\(\(x\) => x\.id !== id\);/);
  assert.match(MAIN, /state\.bots = state\.bots\.filter\(\(x\) => x\.id !== id\);/);
  assert.match(MAIN, /openAi: \(\) => setSettingsTab\('connections'\)/);
});
