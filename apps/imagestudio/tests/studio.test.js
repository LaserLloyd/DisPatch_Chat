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

async function page({ nim = false, state = STATE, generate, clear, images, sort } = {}) {
  const posted = [];
  const env = setupDom(PAGE, PAGE_URL, {
    'GET /apps/imagestudio/locales/en.json': () => [200, EN],
    'GET /api/apps/imagestudio/state': () => [200, state],
    'GET /api/apps/imagestudio/images': images || (() => [200, { images: ROWS }]),
    'POST /api/apps/imagestudio/generate': generate || (() => [200, { images: [], asked: 1 }]),
    'POST /api/apps/imagestudio/clear': clear || (() => [404, { detail: 'no' }]),
  });
  env.win.localStorage.setItem('dispatch-lang', 'en');
  if (sort) env.win.localStorage.setItem('dispatch-imagestudio-sort', sort);
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

test('a 403 shows a locked note, never an empty studio', { skip: dom.skip }, async () => {
  const env = setupDom(PAGE, PAGE_URL, {
    'GET /apps/imagestudio/locales/en.json': () => [200, EN],
    'GET /api/apps/imagestudio/state': () => [403, { detail: 'Permission denied' }],
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

// ---------------------------------------------------------------- sort, clear, notice

const tiles = (doc) => [...doc.querySelectorAll('.st-tile')].map((t) => t.dataset.id);
const clears = (env) => env.calls.filter((c) => c.key === 'POST /api/apps/imagestudio/clear');

test('the sort control reorders the grid, is remembered per device, and the lightbox follows it', { skip: dom.skip }, async () => {
  const env = await page();
  const { doc, win } = env;
  const sel = doc.getElementById('st-sort');
  assert.ok(sel, 'there is a sort control');
  assert.equal(sel.value, 'oldest');
  assert.deepEqual([...sel.options].map((o) => o.textContent), ['Oldest first', 'Newest first']);
  assert.match(sel.closest('label').textContent, /^Sort:/);
  assert.deepEqual(tiles(doc), [ID('a'), ID('b')]);
  sel.value = 'newest';
  sel.dispatchEvent(new win.Event('change', { bubbles: true }));
  assert.deepEqual(tiles(doc), [ID('b'), ID('a')]);
  assert.equal(win.localStorage.getItem('dispatch-imagestudio-sort'), 'newest');
  doc.querySelectorAll('.st-open')[0].click();
  const msg = env.posted.map(([m]) => m).find((m) => m.type === 'dispatch:lightbox');
  assert.equal(msg.index, 0);
  assert.deepEqual(msg.items.map((i) => i.src), [`/media/${ID('b')}.jpg`, `/media/${ID('a')}.png`]);
});

test('a remembered sort is used on the next visit', { skip: dom.skip }, async () => {
  const { doc } = await page({ sort: 'newest' });
  assert.equal(doc.getElementById('st-sort').value, 'newest');
  assert.deepEqual(tiles(doc), [ID('b'), ID('a')]);
});

test('"Clear from here down" counts first, asks in a danger dialog, then clears in the shown order', { skip: dom.skip }, async () => {
  let rows = ROWS;
  const env = await page({
    state: { ...STATE, can_clear: true },
    images: () => [200, { images: rows }],
    clear: (opts) => {
      const b = JSON.parse(opts.body);
      if (b.dry_run) return [200, { count: 2, in_chat: 1, dry_run: true }];
      rows = [];
      return [200, { deleted: 2, files: 1, in_chat: 1 }];
    },
  });
  const { doc, win } = env;
  const sel = doc.getElementById('st-sort');
  sel.value = 'newest';
  sel.dispatchEvent(new win.Event('change', { bubbles: true }));
  const btn = doc.querySelectorAll('.st-tile')[0].querySelector('.st-icon-danger');
  assert.ok(btn, 'each tile has the clear action');
  assert.equal(btn.getAttribute('aria-label'), 'Clear from here down');
  btn.click();
  await flush();
  assert.deepEqual(clears(env)[0].body, { id: ID('b'), order: 'newest', dry_run: true });
  const dlg = doc.querySelector('.st-dlg');
  assert.ok(dlg, 'a confirmation opens');
  assert.match(dlg.textContent, /every older one/);
  assert.match(dlg.textContent, /Pictures to delete: 2/);
  assert.match(dlg.textContent, /still shown in a chat: 1/);
  assert.equal(clears(env).length, 1, 'nothing is deleted before the confirmation');
  dlg.querySelector('.st-danger').click();
  await flush();
  assert.deepEqual(clears(env)[1].body, { id: ID('b'), order: 'newest', expect: 2 }, 'the confirmed count rides along');
  assert.equal(doc.querySelectorAll('.st-tile').length, 0, 'the grid reloads from the server');
  assert.match(doc.querySelector('.st-status').textContent, /Pictures deleted: 2/);
});

test('cancelling the confirmation deletes nothing', { skip: dom.skip }, async () => {
  const env = await page({ state: { ...STATE, can_clear: true }, clear: () => [200, { count: 1, in_chat: 0, dry_run: true }] });
  env.doc.querySelectorAll('.st-tile')[1].querySelector('.st-icon-danger').click();
  await flush();
  assert.match(env.doc.querySelector('.st-dlg').textContent, /every newer one/);
  [...env.doc.querySelectorAll('.st-dlg .st-btn')].find((b) => b.textContent === 'Cancel').click();
  await flush();
  assert.equal(clears(env).length, 1);
  assert.deepEqual(clears(env)[0].body, { id: ID('b'), order: 'oldest', dry_run: true });
  assert.equal(env.doc.querySelectorAll('.st-tile').length, 2);
});

test('a profile that may not delete gets no clear action', { skip: dom.skip }, async () => {
  const { doc } = await page({ state: { ...STATE, can_clear: false } });
  assert.equal(doc.querySelectorAll('.st-icon-danger').length, 0);
});

test('no host-storage callout on the page (it lives in Settings and the first-load card)', { skip: dom.skip }, async () => {
  const { doc } = await page();
  assert.equal(doc.querySelector('.st-privacy, .host-notice'), null);
});

test('a 409 (pictures changed since the count) deletes nothing, re-counts and asks again', { skip: dom.skip }, async () => {
  let dry = 0;
  const env = await page({
    state: { ...STATE, can_clear: true },
    clear: (opts) => {
      const b = JSON.parse(opts.body);
      if (b.dry_run) { dry += 1; return [200, { count: dry === 1 ? 1 : 2, in_chat: 0, dry_run: true }]; }
      return b.expect === 2 ? [200, { deleted: 2, files: 2, in_chat: 0 }] : [409, { detail: 'The pictures changed since you confirmed; count again', error: { code: 'imagestudio_pictures_changed', message: 'The pictures changed since you confirmed; count again' } }];
    },
  });
  const { doc } = env;
  doc.querySelectorAll('.st-tile')[1].querySelector('.st-icon-danger').click();
  await flush();
  assert.match(doc.querySelector('.st-dlg').textContent, /Pictures to delete: 1/);
  doc.querySelector('.st-dlg .st-danger').click();
  await flush(30);
  assert.equal(dry, 2, 'counted again');
  assert.match(doc.querySelector('.st-dlg').textContent, /Pictures to delete: 2/, 'and asked again with the new number');
  assert.match(doc.querySelector('.st-status').textContent, /changed/);
  doc.querySelector('.st-dlg .st-danger').click();
  await flush(30);
  assert.deepEqual(clears(env).filter((c) => !c.body.dry_run).map((c) => c.body.expect), [1, 2]);
});

test('the other 409 (a generation is running) shows the server message and does not re-count', { skip: dom.skip }, async () => {
  let dry = 0;
  const msg = 'A generation is running; try again when it is done';
  const env = await page({
    state: { ...STATE, can_clear: true },
    clear: (opts) => {
      const b = JSON.parse(opts.body);
      if (b.dry_run) { dry += 1; return [200, { count: 1, in_chat: 0, dry_run: true }]; }
      return [409, { detail: msg, error: { code: 'imagestudio_generation_running_clear', message: msg } }];
    },
  });
  const { doc } = env;
  doc.querySelectorAll('.st-tile')[1].querySelector('.st-icon-danger').click();
  await flush();
  doc.querySelector('.st-dlg .st-danger').click();
  await flush(30);
  assert.equal(dry, 1, 'no second count');
  assert.match(doc.querySelector('.st-status').textContent, /generation is running/);
  assert.equal(doc.querySelector('.st-dlg .st-danger').disabled, false, 'the dialog stays, its button usable again');
});

test('a recount whose picture list fails says so instead of throwing', { skip: dom.skip }, async () => {
  let imagesCalls = 0;
  const env = await page({
    state: { ...STATE, can_clear: true },
    images: () => { imagesCalls += 1; return imagesCalls === 1 ? [200, { images: ROWS }] : [500, { detail: 'disk gone' }]; },
    clear: (opts) => {
      const b = JSON.parse(opts.body);
      if (b.dry_run) return [200, { count: 1, in_chat: 0, dry_run: true }];
      return [409, { detail: 'changed', error: { code: 'imagestudio_pictures_changed', message: 'changed' } }];
    },
  });
  const { doc } = env;
  doc.querySelectorAll('.st-tile')[1].querySelector('.st-icon-danger').click();
  await flush();
  doc.querySelector('.st-dlg .st-danger').click();
  await flush(30);
  assert.match(doc.querySelector('.st-status').textContent, /disk gone/);
});

test('no provider yet: a profile that may set one up gets a button that opens Settings there; others are told who can', { skip: dom.skip }, async () => {
  const none = { ...STATE, default: '', providers: PROVIDERS.map((p) => ({ ...p, configured: false })) };
  const env = await page({ state: { ...none, can_configure: true } });
  const btn = env.doc.querySelector('.st-none button');
  assert.ok(btn && !btn.hidden, 'the Set up button is shown');
  assert.equal(btn.textContent, 'Set up image generation');
  assert.equal(env.doc.querySelector('.st-none .st-muted').hidden, true);
  btn.click();
  assert.deepEqual(env.posted.at(-1)[0], { type: 'dispatch:open-settings', target: 'connections#images' });
  assert.equal(env.posted.at(-1)[1], 'http://127.0.0.1:8765');
  const env2 = await page({ state: { ...none, can_configure: false } });
  assert.equal(env2.doc.querySelector('.st-none button').hidden, true, 'no button that could only lead nowhere');
  assert.equal(env2.doc.querySelector('.st-none .st-muted').hidden, false);
  assert.match(env2.doc.querySelector('.st-none').textContent, /Ask the Owner or an Admin/);
});
