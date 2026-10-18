// Image Studio — the page (static/studio.js) driven in jsdom: the Ctrl+Enter
// shortcut, the controls a provider does not support staying hidden, results
// opening in the SHELL's lightbox (a postMessage with the whole set, oldest ->
// newest) and No-Image Mode creating no <img> at all.

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { dom, setupDom, flush, appDir } from '../../../frontend/tests/helpers/app-test-env.js';

const APP = appDir('imagestudio');
const EN = JSON.parse(readFileSync(join(APP, 'locales', 'en.json'), 'utf8'));
const PAGE_URL = 'http://127.0.0.1:8765/apps/imagestudio/';
const PAGE = '<!doctype html><html><head></head><body><main id="st-root"></main></body></html>';
const ID = (c) => c.repeat(32);

const PROVIDERS = [
  { id: 'minimax', label: 'MiniMax', shape: 'minimax', configured: true, model: 'image-01', models: ['image-01', 'image-01-live'], price_note: 'Billed per image.', supports: { reference: true, optimizer: true, seed: true, max_n: 9 } },
  { id: 'openai', label: 'OpenAI', shape: 'openai-images', configured: true, model: 'gpt-image-1', models: [], price_note: 'Billed per image.', supports: { reference: false, optimizer: false, seed: false, max_n: 4 } },
  { id: 'xai', label: 'xAI Grok', shape: 'openai-images', configured: false, model: 'grok-imagine-image', models: [], price_note: 'Billed per image.', supports: { reference: false, optimizer: false, seed: false, max_n: 4 } },
];
const STATE = {
  providers: PROVIDERS, default: 'minimax', ratios: ['1:1', '16:9', '9:16'], prompt_max: 1500,
  today: '2026-10-14', today_count: 2, days: [{ day: '2026-10-14', count: 2 }], total: 2,
  prompts: ['an earlier prompt', 'another one'], busy: false,
};
const ROWS = [
  { id: ID('a'), url: `/media/${ID('a')}.png`, prompt: 'first', provider: 'minimax', model: 'image-01', ratio: '1:1', seed: null },
  { id: ID('b'), url: `/media/${ID('b')}.jpg`, prompt: 'second', provider: 'minimax', model: 'image-01', ratio: '16:9', seed: 5 },
];

async function page({ nim = false, state = STATE, generate } = {}) {
  const posted = [];
  const env = setupDom(PAGE, PAGE_URL, {
    'GET /apps/imagestudio/locales/en.json': () => [200, EN],
    'GET /api/apps/imagestudio/state': () => [200, state],
    'GET /api/apps/imagestudio/images': () => [200, { images: ROWS }],
    'POST /api/apps/imagestudio/generate': generate || (() => [200, { images: [], asked: 1 }]),
  });
  env.win.localStorage.setItem('dispatch-lang', 'en');
  if (nim) env.win.localStorage.setItem('dispatch-nim', '1');
  Object.defineProperty(env.win, 'parent', { configurable: true, get: () => parentWin });
  const parentWin = { postMessage: (m, o) => posted.push([m, o]) };
  globalThis.__ST_NO_AUTOMOUNT = true;
  const sdk = await import('/static/js/app-sdk.js?v=3');
  sdk._resetForTest();
  await sdk.ready('en');
  const mod = await import(`/apps/imagestudio/studio.js?t=${Math.random()}`);
  await mod.mount(env.win.document.getElementById('st-root'));
  await flush();
  return { ...env, posted, mod, doc: env.win.document };
}

function key(env, el, init) {
  const ev = new env.win.KeyboardEvent('keydown', { bubbles: true, cancelable: true, ...init });
  el.dispatchEvent(ev);
  return ev;
}
const gens = (env) => env.calls.filter((c) => c.key === 'POST /api/apps/imagestudio/generate');

test('it mounts: provider default, prompt counter, recent prompts, per-day count, past results oldest first', { skip: dom.skip }, async () => {
  const env = await page();
  const { doc } = env;
  assert.equal(doc.getElementById('st-provider').value, 'minimax');
  assert.equal(doc.querySelector('.st-counter').textContent, '0 / 1,500');
  assert.deepEqual([...doc.querySelectorAll('.st-recent')].map((b) => b.textContent), ['an earlier prompt', 'another one']);
  assert.equal(doc.querySelector('.st-today').textContent, 'Today: 2');
  assert.deepEqual([...doc.querySelectorAll('.st-tile')].map((t) => t.dataset.id), [ID('a'), ID('b')]);
  const xai = [...doc.querySelectorAll('#st-provider option')].find((o) => o.value === 'xai');
  assert.equal(xai.disabled, true, 'a provider with no key cannot be chosen');
  assert.match(doc.querySelector('.st-cost').textContent, /metered|Billed per image/);
  assert.equal(gens(env).length, 0, 'nothing runs automatically');
});

test('clicking a recent prompt fills the box and updates the counter', { skip: dom.skip }, async () => {
  const { doc } = await page();
  doc.querySelector('.st-recent').click();
  assert.equal(doc.getElementById('st-prompt').value, 'an earlier prompt');
  assert.equal(doc.querySelector('.st-counter').textContent, '17 / 1,500');
});

test('Ctrl+Enter generates exactly like the button; plain Enter only types; the tooltip names the shortcut', { skip: dom.skip }, async () => {
  const env = await page({
    generate: () => [200, { images: [{ ...ROWS[0], id: ID('c'), url: `/media/${ID('c')}.png`, prompt: 'a cat' }], asked: 1 }],
  });
  const { doc } = env;
  const box = doc.getElementById('st-prompt');
  const go = doc.getElementById('st-generate');
  assert.match(go.title, /Ctrl\+Enter|⌘\+Enter/);

  // Disabled (empty prompt): the shortcut does nothing.
  assert.equal(go.disabled, true);
  key(env, box, { key: 'Enter', ctrlKey: true });
  await flush();
  assert.equal(gens(env).length, 0, 'disabled button = disabled shortcut');

  box.value = 'a cat';
  box.dispatchEvent(new env.win.Event('input', { bubbles: true }));
  assert.equal(go.disabled, false);

  const plain = key(env, box, { key: 'Enter' });
  await flush();
  assert.equal(gens(env).length, 0, 'plain Enter does not generate');
  assert.equal(plain.defaultPrevented, false, 'plain Enter stays a newline');

  const ev = key(env, box, { key: 'Enter', ctrlKey: true });
  assert.equal(ev.defaultPrevented, true);
  await flush();
  assert.equal(gens(env).length, 1);
  assert.equal(gens(env)[0].body.prompt, 'a cat');
  assert.equal(doc.querySelectorAll('.st-tile').length, 3, 'the new picture joins the grid');

  const meta = key(env, box, { key: 'Enter', metaKey: true });
  assert.equal(meta.defaultPrevented, true, 'Cmd+Enter is the same shortcut');
  await flush();
  assert.equal(gens(env).length, 2);
});

test('an over-long prompt disables Generate and the shortcut', { skip: dom.skip }, async () => {
  const env = await page();
  const box = env.doc.getElementById('st-prompt');
  box.value = 'x'.repeat(1501);
  box.dispatchEvent(new env.win.Event('input', { bubbles: true }));
  assert.equal(env.doc.getElementById('st-generate').disabled, true);
  assert.ok(env.doc.querySelector('.st-counter').classList.contains('st-over'));
  key(env, box, { key: 'Enter', ctrlKey: true });
  await flush();
  assert.equal(gens(env).length, 0);
});

test('controls a provider does not support are hidden; switching provider shows them again', { skip: dom.skip }, async () => {
  const { doc, win } = await page();
  const hidden = (id) => doc.getElementById(id).closest('.st-field, .st-check, .st-ref').hidden;
  assert.equal(doc.querySelector('.st-ref').hidden, false);
  assert.equal(doc.getElementById('st-optimizer').closest('.st-check').hidden, false);
  const sel = doc.getElementById('st-provider');
  sel.value = 'openai';
  sel.dispatchEvent(new win.Event('change', { bubbles: true }));
  assert.equal(doc.querySelector('.st-ref').hidden, true, 'no reference for an OpenAI-shape provider');
  assert.equal(doc.getElementById('st-optimizer').closest('.st-check').hidden, true);
  assert.equal(doc.getElementById('st-seed').closest('.st-field').hidden, true);
  assert.equal(doc.getElementById('st-count').max, '4');
  assert.equal(hidden('st-count'), false);
  sel.value = 'minimax';
  sel.dispatchEvent(new win.Event('change', { bubbles: true }));
  assert.equal(doc.querySelector('.st-ref').hidden, false);
  assert.equal(doc.getElementById('st-count').max, '9');
});

test('a result opens in the shell lightbox with the whole set, oldest -> newest, at the clicked index', { skip: dom.skip }, async () => {
  const env = await page();
  env.doc.querySelectorAll('.st-open')[1].click();
  const msg = env.posted.map(([m]) => m).find((m) => m.type === 'dispatch:lightbox');
  assert.ok(msg, 'the page asked the shell for its lightbox');
  assert.equal(msg.index, 1);
  assert.deepEqual(msg.items.map((i) => i.src), [`/media/${ID('a')}.png`, `/media/${ID('b')}.jpg`]);
  assert.equal(msg.items[1].caption, 'second');
  assert.ok(env.posted.every(([, origin]) => origin === env.win.location.origin), 'posted to our own origin only');
});

test('"use as reference" sets the reference (by result id, never by sending bytes)', { skip: dom.skip }, async () => {
  const env = await page({ generate: () => [200, { images: [], asked: 1 }] });
  env.doc.querySelectorAll('.st-tile')[1].querySelectorAll('.st-icon')[2].click();
  const box = env.doc.getElementById('st-prompt');
  box.value = 'again';
  box.dispatchEvent(new env.win.Event('input', { bubbles: true }));
  key(env, box, { key: 'Enter', ctrlKey: true });
  await flush();
  assert.deepEqual(gens(env)[0].body.reference, { kind: 'result', id: ID('b') });
  assert.ok(env.doc.querySelector('.st-refprev img'));
});

test('copy prompt puts the prompt on the clipboard', { skip: dom.skip }, async () => {
  const env = await page();
  let copied = '';
  Object.defineProperty(env.win.navigator, 'clipboard', { configurable: true, value: { writeText: async (s) => { copied = s; } } });
  env.doc.querySelectorAll('.st-tile')[0].querySelectorAll('.st-icon')[1].click();
  await flush();
  assert.equal(copied, 'first');
});

test('No-Image Mode: no <img> is created for results, the lightbox is not asked for, nothing is fetched', { skip: dom.skip }, async () => {
  const env = await page({ nim: true });
  assert.equal(env.doc.querySelectorAll('img').length, 0);
  assert.equal(env.doc.querySelectorAll('.st-noimg').length, 2);
  env.doc.querySelectorAll('.st-open')[0].click();
  assert.equal(env.posted.filter(([m]) => m.type === 'dispatch:lightbox').length, 0);
  assert.equal(env.calls.filter((c) => c.url.startsWith('/media/')).length, 0);
});

test('with no configured provider the form is replaced by a plain pointer to Settings', { skip: dom.skip }, async () => {
  const state = { ...STATE, default: '', providers: PROVIDERS.map((p) => ({ ...p, configured: false })) };
  const { doc } = await page({ state });
  assert.equal(doc.querySelector('.st-none').hidden, false);
  assert.equal(doc.querySelector('.st-form').hidden, true);
});

test('the Safe-Mode decoy 403 shows a locked note, never an empty studio', { skip: dom.skip }, async () => {
  const env = setupDom(PAGE, PAGE_URL, {
    'GET /apps/imagestudio/locales/en.json': () => [200, EN],
    'GET /api/apps/imagestudio/state': () => [403, { detail: 'Unlock for full access', decoy: true }],
  });
  env.win.localStorage.setItem('dispatch-lang', 'en');
  globalThis.__ST_NO_AUTOMOUNT = true;
  const sdk = await import('/static/js/app-sdk.js?v=3');
  sdk._resetForTest();
  await sdk.ready('en');
  const mod = await import(`/apps/imagestudio/studio.js?t=${Math.random()}`);
  await mod.mount(env.win.document.getElementById('st-root'));
  assert.equal(env.win.document.querySelector('.st-locked').textContent, EN.locked);
  assert.equal(env.win.document.querySelectorAll('.st-tile').length, 0);
});
