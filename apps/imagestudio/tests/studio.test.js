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

async function page({ nim = false, state = STATE, generate, clear, images, sort = 'oldest', pay = null, extra = {} } = {}) {
  const posted = [];
  const env = setupDom(PAGE, PAGE_URL, {
    'GET /apps/imagestudio/locales/en.json': () => [200, EN],
    'GET /api/apps/imagestudio/state': () => [200, state],
    'GET /api/apps/imagestudio/images': images || (() => [200, { images: ROWS }]),
    'POST /api/apps/imagestudio/generate': generate || (() => [200, { images: [], asked: 1 }]),
    'POST /api/apps/imagestudio/clear': clear || (() => [404, { detail: 'no' }]),
    ...extra,
  });
  env.win.localStorage.setItem('dispatch-lang', 'en');
  if (sort !== null) env.win.localStorage.setItem('dispatch-imagestudio-sort', sort);
  if (pay !== null) env.win.localStorage.setItem('dispatch-imagestudio-pay', pay);
  if (nim) env.win.localStorage.setItem('dispatch-nim', '1');
  Object.defineProperty(env.win, 'parent', { configurable: true, get: () => parentWin });
  const parentWin = { postMessage: (m, o) => posted.push([m, o]) };
  globalThis.__ST_NO_AUTOMOUNT = true;
  const sdk = await import('/static/js/app-sdk.js?v=7');
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
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
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

  await sleep(300);   // one keypress = one job: a later press, not a bounce, starts the next
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
  const sdk = await import('/static/js/app-sdk.js?v=7');
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
  assert.match(env2.doc.querySelector('.st-none').textContent, /Ask an Admin to set one up/);
});

test('a picture the retention setting removed (410) becomes the "image expired" placeholder; a 404 stays as it is', { skip: dom.skip }, async () => {
  const env = await page();
  env.routes[`GET /media/${ID('a')}.png`] = () => [410, {}];
  env.routes[`GET /media/${ID('b')}.jpg`] = () => [404, {}];
  const imgs = [...env.doc.querySelectorAll('.st-tile img')];
  assert.equal(imgs.length, 2);
  for (const img of imgs) img.dispatchEvent(new env.win.Event('error'));
  await flush();
  const ph = env.doc.querySelectorAll('.media-expired');
  assert.equal(ph.length, 1);
  assert.equal(ph[0].getAttribute('aria-label'), EN.expired);
  assert.equal(ph[0].dataset.expiredSrc, `/media/${ID('a')}.png`);
  assert.equal(env.doc.querySelectorAll('.st-tile img').length, 1, 'the 404 picture is left alone');
  assert.ok(EN.expired, 'the app locale carries the placeholder text');
});

test('with no saved preference the grid is newest first; a saved preference still wins', { skip: dom.skip }, async () => {
  const fresh = await page({ sort: null });
  assert.equal(fresh.doc.getElementById('st-sort').value, 'newest');
  assert.deepEqual(tiles(fresh.doc), [ID('b'), ID('a')]);
  const saved = await page({ sort: 'oldest' });
  assert.equal(saved.doc.getElementById('st-sort').value, 'oldest');
  assert.deepEqual(tiles(saved.doc), [ID('a'), ID('b')]);
});

test('Shift+Enter generates too; IME composition, key repeat and a bounce start only one job', { skip: dom.skip }, async () => {
  let release;
  const env = await page({
    generate: () => new Promise((res) => { release = () => res([200, { images: [], asked: 1 }]); }),
  });
  const box = env.doc.getElementById('st-prompt');
  box.value = 'a dog';
  box.dispatchEvent(new env.win.Event('input', { bubbles: true }));
  key(env, box, { key: 'Enter', shiftKey: true, isComposing: true });
  key(env, box, { key: 'Enter', shiftKey: true, repeat: true });
  await flush();
  assert.equal(gens(env).length, 0, 'composition and repeat never fire');
  const ev = key(env, box, { key: 'Enter', shiftKey: true });
  assert.equal(ev.defaultPrevented, true);
  key(env, box, { key: 'Enter', ctrlKey: true });
  key(env, box, { key: 'Enter', metaKey: true });
  await flush();
  assert.equal(gens(env).length, 1, 'one keypress (even a bouncing one) = one job');
  release();
  await flush();
});

// ---------------------------------------------------------------- Delete by age

const DEL_STATE = { ...STATE, can_clear: true };
const delBtn = (doc) => doc.getElementById('st-delete');

test('no Delete button for a profile that may not delete', { skip: dom.skip }, async () => {
  const { doc } = await page({ state: { ...STATE, can_clear: false } });
  assert.ok(!delBtn(doc) || delBtn(doc).hidden, 'no Delete button');
});

test('Delete asks for a scope, counts on the server, confirms with the number, then deletes by that scope', { skip: dom.skip }, async () => {
  let rows = ROWS;
  const counts = { hour: 14, day: 1, all: 2 };
  const env = await page({
    state: DEL_STATE,
    images: () => [200, { images: rows }],
    clear: (opts) => {
      const b = JSON.parse(opts.body);
      if (b.dry_run) return [200, { count: counts[b.since], in_chat: 0, dry_run: true }];
      rows = [];
      return [200, { deleted: counts[b.since], files: 1, in_chat: 0 }];
    },
  });
  const { doc, win } = env;
  assert.ok(delBtn(doc), 'a Delete button sits by the results');
  delBtn(doc).click();
  await flush();
  const dlg = doc.querySelector('.st-dlg');
  const scope = dlg.querySelector('select');
  assert.deepEqual([...scope.options].map((o) => o.value), ['hour', 'day', 'all']);
  assert.deepEqual([...scope.options].map((o) => o.textContent), ['Past hour', 'Past day', 'All my pictures']);
  assert.deepEqual(clears(env)[0].body, { since: 'hour', dry_run: true });
  assert.match(dlg.textContent, /Delete 14 pictures from the past hour\?/);
  assert.equal(clears(env).length, 1, 'nothing is deleted before the confirmation');
  scope.value = 'day';
  scope.dispatchEvent(new win.Event('change', { bubbles: true }));
  await flush();
  assert.match(dlg.textContent, /Delete 1 picture from the past day\?/);
  scope.value = 'all';
  scope.dispatchEvent(new win.Event('change', { bubbles: true }));
  await flush();
  assert.match(dlg.textContent, /Delete all 2 of your pictures\?/);
  dlg.querySelector('.st-danger').click();
  await flush(30);
  assert.deepEqual(clears(env).at(-1).body, { since: 'all', expect: 2 }, 'the confirmed count rides along; no client clock');
  assert.equal(doc.querySelectorAll('.st-tile').length, 0);
  assert.match(doc.querySelector('.st-status').textContent, /Pictures deleted: 2/);
});

test('Delete with nothing in range is disabled', { skip: dom.skip }, async () => {
  const env = await page({ state: DEL_STATE, clear: () => [200, { count: 0, in_chat: 0, dry_run: true }] });
  delBtn(env.doc).click();
  await flush();
  assert.match(env.doc.querySelector('.st-dlg').textContent, /No pictures in that range/);
  assert.equal(env.doc.querySelector('.st-dlg .st-danger').disabled, true);
});

test('Delete: a 409 re-counts and asks again, Cancel deletes nothing', { skip: dom.skip }, async () => {
  let dry = 0;
  const env = await page({
    state: DEL_STATE,
    clear: (opts) => {
      const b = JSON.parse(opts.body);
      if (b.dry_run) { dry += 1; return [200, { count: dry === 1 ? 1 : 3, in_chat: 0, dry_run: true }]; }
      return [409, { detail: 'x', error: { code: 'imagestudio_pictures_changed', message: 'x' } }];
    },
  });
  const { doc } = env;
  delBtn(doc).click();
  await flush();
  doc.querySelector('.st-dlg .st-danger').click();
  await flush(30);
  assert.equal(dry, 2);
  assert.match(doc.querySelector('.st-dlg').textContent, /Delete 3 pictures/);
  [...doc.querySelectorAll('.st-dlg .st-btn')].find((b) => b.textContent === 'Cancel').click();
  await flush();
  assert.equal(clears(env).filter((c) => !c.body.dry_run).length, 1, 'only the refused attempt');
  assert.equal(doc.querySelectorAll('.st-tile').length, 2);
});

// ---------------------------------------------------------------- several at once

function holdGenerate() {
  const held = [];
  const fn = (opts) => new Promise((res) => held.push({ opts, ok: (images) => res([200, { images, asked: 1 }]), fail: (code, detail) => res([code, { detail }]) }));
  return { held, fn };
}
const pending = (doc) => [...doc.querySelectorAll('.st-tile.st-pending')];
const fill = (env, prompt) => {
  const box = env.doc.getElementById('st-prompt');
  box.value = prompt;
  box.dispatchEvent(new env.win.Event('input', { bubbles: true }));
};
const newRow = (c) => ({ ...ROWS[0], id: ID(c), url: `/media/${ID(c)}.png`, prompt: `pic ${c}` });

test('Generate stays usable while earlier ones run: each request gets its own dots tile, newest first', { skip: dom.skip }, async () => {
  const h = holdGenerate();
  const env = await page({ sort: 'newest', generate: h.fn });
  const { doc } = env;
  const go = doc.getElementById('st-generate');
  fill(env, 'first cat');
  go.click();
  await flush();
  assert.equal(go.disabled, false, 'the button is still usable');
  fill(env, 'second cat');
  go.click();
  await flush();
  fill(env, 'third cat');
  go.click();
  await flush();
  assert.equal(h.held.length, 3, 'three requests overlap');
  const tilesNow = pending(doc);
  assert.equal(tilesNow.length, 3);
  assert.ok(tilesNow.every((t) => t.querySelectorAll('.st-dots span').length === 3), 'bouncing dots');
  assert.match(tilesNow[0].textContent, /third cat/, 'newest at the top');
  assert.equal(doc.querySelector('.st-grid').firstElementChild, tilesNow[0]);
  // The middle one lands: only its tile is swapped for a picture.
  h.held[1].ok([newRow('c')]);
  await flush(30);
  assert.equal(pending(doc).length, 2);
  assert.ok(tiles(doc).includes(ID('c')));
  h.held[0].ok([newRow('d')]); h.held[2].ok([newRow('e')]);
  await flush(30);
  assert.equal(pending(doc).length, 0);
  assert.equal(tiles(doc).length, 5);
});

test('with oldest first the placeholders sit at the end', { skip: dom.skip }, async () => {
  const h = holdGenerate();
  const env = await page({ sort: 'oldest', generate: h.fn });
  fill(env, 'x');
  env.doc.getElementById('st-generate').click();
  await flush();
  assert.equal(env.doc.querySelector('.st-grid').lastElementChild.classList.contains('st-pending'), true);
});

test('a failed job keeps its tile with the error, Retry re-sends it, Dismiss removes it', { skip: dom.skip }, async () => {
  const h = holdGenerate();
  const env = await page({ generate: h.fn });
  const { doc } = env;
  fill(env, 'doomed');
  doc.getElementById('st-generate').click();
  await flush();
  h.held[0].fail(429, '4 pictures are already being made; wait for one to finish');
  await flush(30);
  const tile = doc.querySelector('.st-tile.st-pending');
  assert.ok(tile.classList.contains('st-failed'));
  assert.match(tile.textContent, /already being made/);
  assert.equal(tile.querySelectorAll('.st-dots').length, 0, 'no dots once it failed');
  [...tile.querySelectorAll('button')].find((b) => b.textContent === 'Retry').click();
  await flush();
  assert.equal(h.held.length, 2);
  assert.equal(JSON.parse(h.held[1].opts.body).prompt, 'doomed');
  assert.equal(doc.querySelectorAll('.st-pending .st-dots').length, 1, 'running again');
  h.held[1].fail(502, 'provider said no');
  await flush(30);
  [...doc.querySelector('.st-tile.st-pending').querySelectorAll('button')].find((b) => b.textContent === 'Dismiss').click();
  assert.equal(pending(doc).length, 0);
});

test('the page stops at the profile cap before asking the server', { skip: dom.skip }, async () => {
  const h = holdGenerate();
  const env = await page({ generate: h.fn, state: { ...STATE, max_in_flight: 2, in_flight: 0 } });
  fill(env, 'p');
  const go = env.doc.getElementById('st-generate');
  go.click(); await flush(); go.click(); await flush(); go.click(); await flush();
  assert.equal(h.held.length, 2);
  assert.match(env.doc.querySelector('.st-status').textContent, /2 pictures are already being made/);
});

// ---------------------------------------------------------------- the prompt stays with its picture

test('the prompt is captured when Generate is pressed: later typing never relabels a running job', { skip: dom.skip }, async () => {
  const h = holdGenerate();
  const env = await page({ generate: h.fn });
  const { doc } = env;
  fill(env, 'a red fox');
  doc.getElementById('st-generate').click();
  await flush();
  fill(env, 'a blue whale');                       // typed while the first one renders
  assert.equal(JSON.parse(h.held[0].opts.body).prompt, 'a red fox');
  assert.match(pending(doc)[0].textContent, /a red fox/);
  h.held[0].ok([{ ...newRow('f'), prompt: 'a red fox' }]);
  await flush(30);
  const tile = doc.querySelector(`.st-tile[data-id="${ID('f')}"]`);
  assert.match(tile.querySelector('.st-tileprompt').textContent, /a red fox/);
});

test('overlapping jobs finishing out of order keep their own prompts', { skip: dom.skip }, async () => {
  const h = holdGenerate();
  const env = await page({ generate: h.fn });
  const go = env.doc.getElementById('st-generate');
  fill(env, 'one'); go.click(); await flush();
  fill(env, 'two'); go.click(); await flush();
  h.held[1].ok([{ ...newRow('2'), prompt: 'two' }]);
  await flush(30);
  h.held[0].ok([{ ...newRow('1'), prompt: 'one' }]);
  await flush(30);
  const prompt = (c) => env.doc.querySelector(`.st-tile[data-id="${ID(c)}"] .st-tileprompt`).textContent;
  assert.equal(prompt('1'), 'one');
  assert.equal(prompt('2'), 'two');
});

test('sending a picture to a chat carries its prompt, model and seed so Details can show them', { skip: dom.skip }, async () => {
  const env = await page({
    state: { ...STATE, can_send: true },
    extra: {
      'GET /api/apps/imagestudio/targets': () => [200, { threads: [{ id: 't1', bot: 'Bits', title: 'x' }] }],
      'POST /api/inject': () => [200, { ok: true }],
    },
  });
  const { doc } = env;
  doc.querySelectorAll('.st-tile')[1].querySelectorAll('.st-actions .st-icon')[3].click();   // oldest first: tile 1 = b
  await flush();
  doc.querySelector('.st-dlg .st-primary').click();
  await flush(30);
  const call = env.calls.find((c) => c.key === 'POST /api/inject');
  assert.ok(call, 'it posted');
  assert.deepEqual(call.body.metadata, {
    source: 'image-studio', image_id: ID('b'), prompt: 'second', model: 'minimax · image-01', seed: 5,
  });
});

// ---- 4.3 Fable review (chat agent)

test('Fable: a failed job shows its reason once (on its tile), not again in the status line', { skip: dom.skip }, async () => {
  const h = holdGenerate();
  const env = await page({ generate: h.fn });
  const { doc } = env;
  fill(env, 'doomed');
  doc.getElementById('st-generate').click();
  await flush();
  h.held[0].fail(502, 'The picture server answered: not found');
  await flush(30);
  const hits = (doc.body.textContent.match(/answered: not found/g) || []).length;
  assert.equal(hits, 1);
});

test('Fable: a provider with no price note gets no "metered provider" note', { skip: dom.skip }, async () => {
  const custom = { id: 'custom', label: 'Custom (OpenAI-compatible)', shape: 'openai-images', configured: true, model: 'x', models: [], price_note: '', supports: { reference: false, optimizer: false, seed: false, max_n: 4 } };
  const env = await page({ state: { ...STATE, providers: [custom], default: 'custom' } });
  assert.doesNotMatch(env.doc.body.textContent, /metered/);
});

test('Fable: "no bots" points to Settings → Characters', () => {
  assert.match(EN['avatar.no_bots'], /Settings → Characters/);
  assert.doesNotMatch(EN['avatar.no_bots'], /My bots/);
});

const AIR = 'urn:air:sdxl:checkpoint:civitai:133005@1759168';
const CIVITAI = { id: 'civitai', label: 'Civitai', shape: 'civitai', configured: true, model: AIR, models: [AIR], model_names: { [AIR]: 'Juggernaut XL' }, price_note: 'Billed in Buzz.', estimate: true, supports: { reference: false, optimizer: false, seed: true, max_n: 4 } };
const CSTATE = { ...STATE, providers: [PROVIDERS[0], CIVITAI], default: 'civitai' };

test('civitai: model names label the datalist, seed shown, optimizer/reference hidden, count max 4', { skip: dom.skip }, async () => {
  const env = await page({ state: CSTATE });
  const { doc } = env;
  assert.equal(doc.querySelector('#st-models option, datalist option').getAttribute('label'), 'Juggernaut XL');
  assert.equal(doc.getElementById('st-count').max, '4');
  assert.equal(doc.getElementById('st-seed').closest('[hidden]'), null);
  assert.equal(doc.getElementById('st-model').placeholder, EN['model.civitai_placeholder']);
});

test('civitai: the estimate line follows one debounced call per change and a failure never blocks Generate', { skip: dom.skip }, async () => {
  let fail = false;
  const env = await page({ state: CSTATE, extra: { 'POST /api/apps/imagestudio/estimate': () => (fail === 'detail' ? [422, { detail: 'Paste a Civitai checkpoint link or AIR' }] : fail ? [502, {}] : [200, { buzz: 14, n: 1 }]) } });
  const { doc, win } = env;
  const est = () => env.calls.filter((c) => c.key === 'POST /api/apps/imagestudio/estimate').length;
  const prompt = doc.getElementById('st-prompt');
  const type = (v) => { prompt.value = v; prompt.dispatchEvent(new win.Event('input', { bubbles: true })); };
  type('a'); type('a f'); type('a fox');
  await sleep(750); await flush();
  assert.equal(est(), 1, 'typing bursts collapse to one call');
  assert.match(doc.querySelector('.st-estimate').textContent, /About 14 Buzz for 1 picture/);
  type('a fox'); await sleep(700);
  assert.equal(est(), 1, 'same form, no new call');
  fail = true; type('a fox again'); await sleep(750); await flush();
  assert.equal(doc.querySelector('.st-estimate').textContent, EN['cost.estimate_failed']);
  assert.equal(doc.getElementById('st-generate').disabled, false);
  // A refusal with a sentence (a bad model, a refused prompt) shows the server's own words.
  fail = 'detail'; type('a fox once more'); await sleep(750); await flush();
  assert.equal(doc.querySelector('.st-estimate').textContent, 'Paste a Civitai checkpoint link or AIR');
  assert.equal(doc.getElementById('st-generate').disabled, false);
});

const ROW1 = { air: 'urn:air:sdxl:checkpoint:civitai:11@22', name: 'Cool <b>XL</b>', base: 'Illustrious', downloads: 286000, likes: 9, nsfw: true, url: 'https://civitai.com/models/11' };
const ROW2 = { air: 'urn:air:sdxl:checkpoint:civitai:12@23', name: 'Plain Model', base: 'Pony', downloads: 1, likes: 1, nsfw: false, url: 'https://civitai.com/models/12' };
const BAL = () => [200, { balances: { blue: 56, yellow: 1234 }, checked: '2026-11-02T10:00:00+00:00' }];

test('civitai: the Pay with select and Buzz line show for Civitai only; the choice is remembered and sent', { skip: dom.skip }, async () => {
  const env = await page({ state: CSTATE, extra: {
    'GET /api/apps/imagestudio/civitai/balance': BAL,
    'POST /api/apps/imagestudio/estimate': () => [200, { buzz: 14, n: 1 }],
    'POST /api/apps/imagestudio/generate': () => [200, { images: [{ ...ROWS[0], id: ID('c'), url: `/media/${ID('c')}.png`, provider: 'civitai', currency: 'yellow' }], asked: 1 }],
  } });
  const { doc, win } = env;
  const pay = doc.getElementById('st-pay');
  assert.equal(pay.closest('.st-field').hidden, false);
  assert.deepEqual([...pay.options].map((o) => o.textContent), ['Civitai decides', 'Blue Buzz', 'Yellow Buzz']);
  assert.equal(doc.querySelector('.st-buzz-text').textContent, 'Buzz: 1,234 yellow · 56 blue');
  const prompt = doc.getElementById('st-prompt');
  prompt.value = 'a fox'; prompt.dispatchEvent(new win.Event('input', { bubbles: true }));
  await sleep(750); await flush();
  const est = () => env.calls.filter((c) => c.key === 'POST /api/apps/imagestudio/estimate');
  assert.equal(est().length, 1);
  pay.value = 'yellow'; pay.dispatchEvent(new win.Event('change', { bubbles: true }));
  await sleep(750); await flush();
  assert.equal(est().length, 2, 'changing the Buzz type re-estimates');
  assert.equal(est()[1].body.currency, 'yellow');
  assert.equal(win.localStorage.getItem('dispatch-imagestudio-pay'), 'yellow');
  doc.getElementById('st-generate').click();
  await flush();
  assert.equal(gens(env)[0].body.currency, 'yellow');
  const balances = () => env.calls.filter((c) => c.key === 'GET /api/apps/imagestudio/civitai/balance').length;
  assert.equal(balances(), 2, 'the balance is read again after a Civitai picture');
  // Another provider: none of it shows.
  doc.getElementById('st-provider').value = 'minimax';
  doc.getElementById('st-provider').dispatchEvent(new win.Event('change', { bubbles: true }));
  assert.equal(pay.closest('.st-field').hidden, true);
  assert.equal(doc.getElementById('st-browse').hidden, true);
  assert.equal(doc.querySelector('.st-buzz').hidden, true);
});

test('civitai: a saved Buzz type is selected on load; nothing about Buzz shows for a non-Civitai default', { skip: dom.skip }, async () => {
  const env = await page({ state: CSTATE, extra: { 'GET /api/apps/imagestudio/civitai/balance': BAL } });
  assert.equal(env.doc.getElementById('st-pay').value, '');
  const env2 = await page({ state: CSTATE, pay: 'blue', extra: { 'GET /api/apps/imagestudio/civitai/balance': BAL } });
  assert.equal(env2.doc.getElementById('st-pay').value, 'blue');
  const env3 = await page();
  assert.equal(env3.calls.filter((c) => c.key.includes('/civitai/')).length, 0, 'MiniMax never asks about Civitai');
  assert.equal(env3.doc.querySelector('.st-buzz').hidden, true);
});

test('civitai: Refresh asks again; a key without the scope gets one plain sentence and a link', { skip: dom.skip }, async () => {
  let scope = false;
  const env = await page({ state: { ...CSTATE, can_configure: true }, extra: {
    'GET /api/apps/imagestudio/civitai/balance': () => (scope ? [200, { balances: null, scope_missing: true, help_url: 'https://civitai.com/user/account' }] : BAL()),
  } });
  const { doc } = env;
  const n = () => env.calls.filter((c) => c.key === 'GET /api/apps/imagestudio/civitai/balance').length;
  assert.equal(n(), 1, 'loaded once when Civitai is the provider');
  assert.equal(doc.querySelector('.st-buzz-scope').hidden, true);
  scope = true;
  doc.getElementById('st-buzz-refresh').click();
  await flush();
  assert.equal(n(), 2);
  const note = doc.querySelector('.st-buzz-scope');
  assert.equal(note.hidden, false);
  assert.equal(note.textContent, EN['buzz.scope']);
  const link = doc.querySelector('.st-buzz-links a');
  assert.equal(link.hidden, false);
  assert.equal(link.getAttribute('href'), 'https://civitai.com/user/account');
  assert.equal(link.getAttribute('target'), '_blank');
  assert.match(link.getAttribute('rel'), /noopener/);
  assert.equal(doc.querySelector('.st-buzz-links button').hidden, false, 'the Owner can jump to Settings');
  assert.equal(doc.querySelector('.st-buzz-text').textContent, '');
});

test('civitai: a balance failure shows the server sentence; a profile that cannot configure gets no Settings link', { skip: dom.skip }, async () => {
  const env = await page({ state: CSTATE, extra: { 'GET /api/apps/imagestudio/civitai/balance': () => [429, { detail: 'Wait a few seconds before checking the Buzz balance again' }] } });
  assert.equal(env.doc.querySelector('.st-buzz-text').textContent, 'Wait a few seconds before checking the Buzz balance again');
  const env2 = await page({ state: CSTATE, extra: { 'GET /api/apps/imagestudio/civitai/balance': () => [200, { balances: null, scope_missing: true, help_url: 'https://civitai.com/user/account' }] } });
  assert.equal(env2.doc.querySelector('.st-buzz-links button').hidden, true);
});

test('civitai Browse: opens a list, a row fills the Model box with the AIR and its name, Refresh bypasses the cache, text is never markup', { skip: dom.skip }, async () => {
  const env = await page({ state: CSTATE, extra: {
    'GET /api/apps/imagestudio/civitai/balance': BAL,
    'POST /api/apps/imagestudio/estimate': () => [200, { buzz: 14, n: 1 }],
    'GET /api/apps/imagestudio/civitai/models': () => [200, { models: [ROW1, ROW2, { air: 'junk', name: 'Bad' }], sort: 'week' }],
  } });
  const { doc, win } = env;
  const calls = () => env.calls.filter((c) => c.key.startsWith('GET /api/apps/imagestudio/civitai/models'));
  const btn = doc.getElementById('st-browse');
  const panel = doc.getElementById('st-browse-panel');
  assert.equal(btn.hidden, false);
  assert.equal(panel.hidden, true);
  btn.click(); await flush();
  assert.equal(panel.hidden, false);
  assert.equal(btn.getAttribute('aria-expanded'), 'true');
  assert.equal(calls().length, 1);
  const rows = [...doc.querySelectorAll('.st-browse-row')];
  assert.equal(rows.length, 2, 'a malformed row never reaches the list');
  assert.equal(rows[0].querySelector('.st-browse-name').textContent, 'Cool <b>XL</b> · Illustrious · 286k downloads');
  assert.equal(rows[0].querySelector('b'), null, 'names are text, not markup');
  assert.equal(rows[0].querySelector('.st-badge').textContent, '18+');
  assert.equal(rows[1].querySelector('.st-badge'), null);
  assert.equal(rows[1].querySelector('.st-browse-name').textContent, 'Plain Model · Pony · 1 download');
  assert.equal(rows[0].tagName, 'BUTTON');
  doc.getElementById('st-browse-sort').value = 'rated';
  doc.getElementById('st-browse-sort').dispatchEvent(new win.Event('change', { bubbles: true }));
  await flush();
  assert.match(calls()[0].url, /sort=week$/);
  assert.match(calls()[1].url, /sort=rated$/);
  assert.equal(calls().length, 2);
  [...doc.querySelectorAll('#st-browse-panel .st-row .st-btn')].find((b) => b.textContent === 'Refresh').click();
  await flush();
  assert.equal(calls().length, 3);
  assert.match(calls()[2].url, /sort=rated&fresh=true$/);
  doc.querySelectorAll('.st-browse-row')[0].click();
  await flush(); await sleep(700); await flush();
  const model = doc.getElementById('st-model');
  assert.equal(model.value, ROW1.air);
  assert.equal(panel.hidden, true, 'the panel closes after a pick');
  assert.ok([...doc.querySelectorAll('#st-models option')].some((o) => o.value === ROW1.air && o.getAttribute('label') === ROW1.name));
  const prompt = doc.getElementById('st-prompt');
  prompt.value = 'a fox'; prompt.dispatchEvent(new win.Event('input', { bubbles: true }));
  await sleep(750); await flush();
  const est = env.calls.filter((c) => c.key === 'POST /api/apps/imagestudio/estimate');
  assert.equal(est.at(-1).body.model, ROW1.air, 'the estimate follows the picked model');
});

test('civitai Browse: a failure and an empty list say so in words', { skip: dom.skip }, async () => {
  let mode = 'fail';
  const env = await page({ state: CSTATE, extra: {
    'GET /api/apps/imagestudio/civitai/balance': BAL,
    'GET /api/apps/imagestudio/civitai/models': () => (mode === 'fail' ? [502, {}] : [200, { models: [], sort: 'week' }]),
  } });
  const { doc } = env;
  doc.getElementById('st-browse').click(); await flush();
  const msg = doc.querySelector('#st-browse-panel .st-muted[role="status"]');
  assert.equal(msg.textContent, EN['browse.failed']);
  mode = 'empty';
  [...doc.querySelectorAll('#st-browse-panel .st-row .st-btn')].find((b) => b.textContent === 'Refresh').click();
  await flush();
  assert.equal(msg.textContent, EN['browse.empty']);
});

test('blocked state shows the not-allowed text and no setup button', { skip: dom.skip }, async () => {
  const env = await page({ state: { ...STATE, providers: [], default: null, blocked: true, can_configure: true } });
  const none = env.doc.querySelector('.st-none');
  assert.equal(none.hidden, false);
  assert.match(none.textContent, /can't use the image generators/);
  assert.equal(none.querySelector('button').hidden, true);
});

const H3_PROVIDERS = PROVIDERS.map((p) => (p.id !== 'minimax' ? p : {
  ...p, models: ['MiniMax-H3-Image', 'image-01'], models_source: 'documented', models_checked_at: '2026-11-11T05:00:00+00:00', label: 'MiniMax',
  prompt_max_by_model: { 'image-01': 1500, 'MiniMax-H3-Image': 4000 },
  supports_by_model: {
    'image-01': { reference: true, optimizer: true, seed: true, max_n: 9 },
    'MiniMax-H3-Image': { reference: false, optimizer: true, seed: false, max_n: 10 },
  },
}));

test('the refresh button shows only with can_refresh_models; the note names the source', { skip: dom.skip }, async () => {
  const no = await page({ state: { ...STATE, providers: H3_PROVIDERS } });
  assert.equal(no.doc.getElementById('st-model-refresh').hidden, true);
  assert.match(no.doc.querySelector('.st-modelnote').textContent, /From MiniMax's docs \(checked 2026-11-11\)/);
  const yes = await page({ state: { ...STATE, providers: H3_PROVIDERS, can_refresh_models: true } });
  assert.equal(yes.doc.getElementById('st-model-refresh').hidden, false);
});

test('picking an H3 model lifts the prompt limit and hides seed and reference', { skip: dom.skip }, async () => {
  const { doc, win } = await page({ state: { ...STATE, providers: H3_PROVIDERS } });
  assert.equal(doc.querySelector('.st-counter').textContent, '0 / 1,500');
  const m = doc.getElementById('st-model');
  m.value = 'MiniMax-H3-Image';
  m.dispatchEvent(new win.Event('input', { bubbles: true }));
  assert.equal(doc.querySelector('.st-counter').textContent, '0 / 4,000');
  assert.equal(doc.getElementById('st-seed').closest('.st-field').hidden, true);
  assert.equal(doc.getElementById('st-count').max, '10');
});

test('Refresh swaps in the new provider rows, keeps the selection and says what changed', { skip: dom.skip }, async () => {
  const fresh = H3_PROVIDERS.map((p) => (p.id === 'minimax' ? { ...p, models: ['MiniMax-H3-Image', 'image-01', 'image-01-live'] } : p));
  const env = await page({
    state: { ...STATE, providers: H3_PROVIDERS, can_refresh_models: true },
    extra: { 'POST /api/apps/imagestudio/models/refresh': () => [200, {
      providers: fresh, default: 'minimax', results: { minimax: { source: 'endpoint', count: 3, error: null, cached: false } } }] },
  });
  const { doc } = env;
  doc.getElementById('st-model').value = 'MiniMax-H3-Image';
  doc.getElementById('st-model-refresh').click();
  await flush(); await flush();
  assert.equal(env.calls.filter((c) => c.key === 'POST /api/apps/imagestudio/models/refresh').length, 1);
  assert.equal(doc.getElementById('st-model').value, 'MiniMax-H3-Image');
  assert.equal(doc.getElementById('st-provider').value, 'minimax');
  assert.equal(doc.querySelectorAll('#st-models option').length, 3);
  assert.match(doc.querySelector('.st-status').textContent, /Updated: 3 models for MiniMax/);
  assert.equal(doc.getElementById('st-model-refresh').disabled, false);
});
