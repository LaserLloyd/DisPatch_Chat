// Settings -> Characters -> Reactions (js/reactions.js mountManager), 3.2: the
// pack is a grid of thumbnails; a tile opens one edit sheet whose fields save
// when they change; Behaviour sits first (closed) and applies on change. Real
// index.html under jsdom, the API stubbed at fetch().

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

const require = createRequire(import.meta.url);
let jsdom = null;
try { jsdom = require('jsdom'); } catch { /* not installed - DOM tests skip */ }
const dom = { skip: domSkip(jsdom ? false : 'jsdom is not installed') };

const PACK = () => ({
  reactions: [
    { id: 'wow', name: 'Wow', aliases: ['omg'], safe: true, source: 'pack', image_url: '/api/reactions/wow/image' },
    { id: 'nope', name: 'Nope', aliases: [], safe: false, source: 'custom', image_url: '/api/reactions/nope/image' },
  ],
  settings: { enabled: true, default_duration_ms: 10000, cooldown_ms: 0, queue_max: 3 },
  can_manage: true, can_generate: false, pool: { remaining: 4, per_mood: 20, available: false, image_cli: 'unset' },
  reaction_bots: [],
});
const tick = () => new Promise((r) => setTimeout(r, 0));

async function setup() {
  const win = new jsdom.JSDOM(HTML, { url: 'http://127.0.0.1:8765/', runScripts: 'outside-only' }).window;
  const put = (k, v) => Object.defineProperty(globalThis, k, { value: v, configurable: true, writable: true });
  for (const k of ['window', 'document', 'location', 'navigator', 'localStorage', 'Node', 'HTMLElement', 'HTMLInputElement']) put(k, win[k]);
  const calls = [];
  globalThis.fetch = async (u, opts = {}) => {
    const key = `${(opts.method || 'GET').toUpperCase()} ${u}`;
    calls.push({ key, body: typeof opts.body === 'string' ? JSON.parse(opts.body) : null });
    let res = [404, { detail: 'Not Found' }];
    if (key === 'GET /static/locales/en.json') res = [200, EN];
    else if (key === 'GET /api/reactions') res = [200, PACK()];
    else if (key.startsWith('PATCH /api/reactions/')) res = [200, { ok: true }];
    else if (key === 'PUT /api/reactions/settings') res = [200, { settings: { ...PACK().settings, ...JSON.parse(opts.body).values } }];
    return { ok: res[0] < 300, status: res[0], statusText: String(res[0]), headers: new win.Headers(), json: async () => res[1] };
  };
  const i18n = await import('../static/js/i18n.js?v=4');
  await i18n.init('en');
  const mod = await import(`../static/js/reactions.js?t=${Math.random()}`);
  const toasts = [];
  mod.initReactions({ toast: (m) => toasts.push(m), onChanged() {}, confirm: async () => true });
  const host = win.document.getElementById('chars-reactions-host');
  await mod.mountManager(host);
  return { win, mod, host, calls, toasts };
}

test('the pack is a grid of tiles with an R badge for the Restricted-visible ones; no row has its own Save', { skip: dom.skip }, async () => {
  const { host } = await setup();
  const tiles = [...host.querySelectorAll('.rx-tile')];
  assert.deepEqual(tiles.map((t) => t.dataset.reaction), ['wow', 'nope']);
  assert.equal(tiles[0].querySelector('.rx-tile-badge').textContent, 'R');
  assert.equal(tiles[1].querySelector('.rx-tile-badge'), null);
  assert.equal(host.querySelectorAll('.rx-row-save').length, 0);
  assert.equal(host.querySelector('.rx-sheet'), null, 'no sheet until a tile is chosen');
  // Behaviour comes first and starts closed.
  const first = host.querySelector('.rx-body').firstElementChild;
  assert.equal(first.tagName, 'DETAILS');
  assert.equal(first.open, false);
  assert.equal(first.querySelector('summary').textContent, EN.reactions.behaviour_heading);
});

test('a tile opens its edit sheet; a field saves when it changes', { skip: dom.skip }, async () => {
  const { win, host, calls } = await setup();
  host.querySelector('.rx-tile[data-reaction="nope"]').click();
  await tick();
  const sheet = host.querySelector('.rx-sheet');
  assert.ok(sheet);
  assert.equal(host.querySelector('.rx-tile[data-reaction="nope"]').getAttribute('aria-expanded'), 'true');
  const name = sheet.querySelector('#rx-sheet-name');
  name.value = 'Nah';
  name.dispatchEvent(new win.Event('change'));
  await tick(); await tick();
  const patch = calls.find((c) => c.key === 'PATCH /api/reactions/nope');
  assert.deepEqual(patch.body, { name: 'Nah', aliases: [], safe: false });
  // Escape closes the sheet.
  const again = host.querySelector('.rx-sheet');
  again.dispatchEvent(new win.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  assert.equal(host.querySelector('.rx-sheet'), null);
});

test('behaviour applies on change (no Save button)', { skip: dom.skip }, async () => {
  const { win, host, calls } = await setup();
  const fold = host.querySelector('.rx-body > details');
  const nums = fold.querySelectorAll('input[type=number]');
  nums[2].value = '5';
  nums[2].dispatchEvent(new win.Event('change'));
  await tick(); await tick();
  const put = calls.find((c) => c.key === 'PUT /api/reactions/settings');
  assert.equal(put.body.values.queue_max, 5);
  assert.equal([...fold.querySelectorAll('button')].some((b) => b.textContent === 'Save behaviour'), false);
});
