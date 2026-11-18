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

async function page({ nim = false, state = STATE, generate, clear, images, sort = 'oldest', pay = null, extra = {}, framed = true, store = {} } = {}) {
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
  for (const [k, v] of Object.entries(store)) env.win.localStorage.setItem(k, v);
  const parentWin = { postMessage: (m, o) => posted.push([m, o]) };
  // Framed by the shell: the page posts to it. On its own, window.parent is the
  // window itself and there is nobody to post to.
  if (framed) Object.defineProperty(env.win, 'parent', { configurable: true, get: () => parentWin });
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
/** The model control that is on the page: the dropdown of names, or the text box. */
const modelControl = (doc) => (doc.getElementById('st-model-select').hidden
  ? doc.getElementById('st-model') : doc.getElementById('st-model-select'));
/** Put a model name in the control that is on the page, the way a person would. */
function setModel(doc, win, value) {
  const c = modelControl(doc);
  c.value = value;
  c.dispatchEvent(new win.Event(c.tagName === 'SELECT' ? 'change' : 'input', { bubbles: true }));
}

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

test('the aspect ratios are named, and the ratio rides beside the name', { skip: dom.skip }, async () => {
  const { doc } = await page();
  const sel = doc.getElementById('st-ratio');
  assert.deepEqual([...sel.options].map((o) => o.textContent), ['Square (1:1)', 'Wide (16:9)', 'Portrait tall (9:16)']);
  assert.deepEqual([...sel.options].map((o) => o.value), ['1:1', '16:9', '9:16'], 'the value is still the ratio');
  assert.equal(sel.value, '1:1');
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

test('a page on its own opens the picture in its own dialog (the shell is not there to ask)', { skip: dom.skip }, async () => {
  const env = await page({ framed: false });
  const { doc, win } = env;
  assert.equal(env.posted.filter(([m]) => m.type === 'dispatch:lightbox').length, 0, 'nobody is asked');
  const tile = doc.querySelector('.st-open');
  tile.click();
  await flush();
  const dlg = doc.querySelector('.st-viewerdlg');
  assert.ok(dlg, 'a dialog is open');
  assert.equal(dlg.getAttribute('role'), 'dialog');
  assert.equal(dlg.getAttribute('aria-modal'), 'true');
  const img = dlg.querySelector('img');
  assert.ok(img, 'the picture is there');
  assert.equal(img.getAttribute('src'), ROWS[0].url);
  assert.ok(dlg.querySelector('.st-viewer-close'), 'Close');
  assert.ok(dlg.querySelector('.st-viewer-prev'), 'previous');
  assert.ok(dlg.querySelector('.st-viewer-next'), 'next');
  assert.ok(dlg.querySelector('a[download]'), 'a download link');
  // Stepping goes oldest -> newest, the order the shell's lightbox uses.
  dlg.querySelector('.st-viewer-next').click();
  assert.equal(doc.querySelector('.st-viewerimg').getAttribute('src'), ROWS[1].url);
  // The ends stop: there is nothing older than the first picture.
  doc.querySelector('.st-viewer-prev').click();
  assert.equal(doc.querySelector('.st-viewerimg').getAttribute('src'), ROWS[0].url);
  // Escape closes it, and the focus goes back to the tile.
  const ev = new win.KeyboardEvent('keydown', { key: 'Escape', bubbles: true });
  doc.dispatchEvent(ev);
  assert.equal(doc.querySelector('.st-viewerdlg'), null, 'the dialog is gone');
  assert.equal(doc.activeElement, tile, 'the focus is back on the tile');
});

test('one picture on its own: the viewer has no arrows', { skip: dom.skip }, async () => {
  const env = await page({ framed: false, images: () => [200, { images: [ROWS[0]] }] });
  const { doc } = env;
  doc.querySelector('.st-open').click();
  await flush();
  const dlg = doc.querySelector('.st-viewerdlg');
  assert.ok(dlg, 'a dialog is open');
  assert.equal(dlg.querySelector('.st-viewer-prev').hidden, true, 'no previous with one picture');
  assert.equal(dlg.querySelector('.st-viewer-next').hidden, true, 'and no next');
});

test('No-Image Mode on its own opens nothing and creates no picture', { skip: dom.skip }, async () => {
  const env = await page({ framed: false, nim: true });
  const { doc } = env;
  doc.querySelector('.st-open').click();
  await flush();
  assert.equal(doc.querySelector('.st-viewerdlg'), null, 'nothing opens');
  assert.equal(doc.querySelectorAll('img').length, 0, 'and no picture is fetched');
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

test('civitai: the model names are the options of the dropdown, seed shown, optimizer/reference hidden, count max 4', { skip: dom.skip }, async () => {
  const env = await page({ state: CSTATE });
  const { doc } = env;
  const sel = doc.getElementById('st-model-select');
  assert.equal(sel.hidden, false, 'a short list is a dropdown');
  assert.equal(doc.getElementById('st-model').hidden, true, 'and no text box beside it');
  const opt = sel.querySelector('option');
  assert.equal(opt.value, AIR);
  assert.equal(opt.textContent, 'Juggernaut XL');
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
  const model = modelControl(doc);
  assert.equal(model.value, ROW1.air);
  assert.equal(panel.hidden, true, 'the panel closes after a pick');
  assert.ok([...doc.querySelectorAll('#st-model-select option')].some((o) => o.value === ROW1.air && o.textContent === ROW1.name));
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

test('an H3 model shows the slow-and-billed hint near Generate; image-01 does not', { skip: dom.skip }, async () => {
  const { doc, win } = await page({ state: { ...STATE, providers: H3_PROVIDERS } });
  const hint = doc.querySelector('.st-h3hint');
  assert.equal(hint.hidden, true);
  setModel(doc, win, 'MiniMax-H3-Image');
  assert.equal(hint.hidden, false);
  assert.match(hint.textContent, /^MiniMax H3 pictures can take a few minutes.*never repeated automatically.*billed/);
  setModel(doc, win, 'image-01');
  assert.equal(hint.hidden, true);
});

test('a refresh inside the cooldown says so instead of "Updated"', { skip: dom.skip }, async () => {
  const env = await page({
    state: { ...STATE, providers: H3_PROVIDERS, can_refresh_models: true },
    extra: { 'POST /api/apps/imagestudio/models/refresh': () => [200, {
      providers: H3_PROVIDERS, default: 'minimax', results: { minimax: { source: 'documented', count: 4, error: null, cached: true } } }] },
  });
  env.doc.getElementById('st-model-refresh').click();
  await flush(); await flush();
  assert.match(env.doc.querySelector('.st-status').textContent, /Just refreshed/);
  assert.doesNotMatch(env.doc.querySelector('.st-status').textContent, /Updated/);
});

test('a failed refresh request says so without an empty provider name', { skip: dom.skip }, async () => {
  const env = await page({
    state: { ...STATE, providers: H3_PROVIDERS, can_refresh_models: true },
    extra: { 'POST /api/apps/imagestudio/models/refresh': () => [500, { detail: 'boom' }] },
  });
  env.doc.getElementById('st-model-refresh').click();
  await flush(); await flush();
  assert.match(env.doc.querySelector('.st-status').textContent, /^Couldn't refresh the model lists:/);
  assert.equal(env.doc.getElementById('st-model-refresh').disabled, false, 'the button is usable again');
});

test('picking an H3 model lifts the prompt limit and hides seed and reference', { skip: dom.skip }, async () => {
  const { doc, win } = await page({ state: { ...STATE, providers: H3_PROVIDERS } });
  assert.equal(doc.querySelector('.st-counter').textContent, '0 / 1,500');
  setModel(doc, win, 'MiniMax-H3-Image');
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
  const { doc, win } = env;
  setModel(doc, win, 'MiniMax-H3-Image');
  doc.getElementById('st-model-refresh').click();
  await flush(); await flush();
  assert.equal(env.calls.filter((c) => c.key === 'POST /api/apps/imagestudio/models/refresh').length, 1);
  assert.equal(modelControl(doc).value, 'MiniMax-H3-Image');
  assert.equal(doc.getElementById('st-provider').value, 'minimax');
  assert.equal(doc.querySelectorAll('#st-model-select option').length, 3);
  assert.match(doc.querySelector('.st-status').textContent, /Updated: 3 models for MiniMax/);
  assert.equal(doc.getElementById('st-model-refresh').disabled, false);
});
// ------------------------------------------- the model names and the image server

const WF = { id: 'workflows', label: 'Image server', shape: 'workflows', configured: true, model: '',
  models: [], model_names: {}, gallery: true,
  supports: { reference: false, optimizer: false, seed: false, max_n: 10, batch: false } };
const WSTATE = { ...STATE, providers: [WF], default: 'workflows' };
const WORKFLOWS = { workflows: [{ name: 'wf-a', display_name: 'Workflow A', state: 'ready',
  stale: false, thumb: '/ui/api/image?f=_previews/wf-a.png' }] };

test('a short model list is a dropdown of names; the id rides in the title, and no wall of pills', { skip: dom.skip }, async () => {
  const env = await page({ state: CSTATE });
  const { doc } = env;
  const opt = doc.querySelector('#st-model-select option');
  assert.equal(opt.value, AIR, 'the option carries the model id');
  assert.equal(opt.textContent, 'Juggernaut XL', 'and the display name');
  assert.equal(opt.getAttribute('title'), AIR, 'the id is in the title: it is far too long for the label');
  assert.equal(doc.querySelectorAll('.st-model-pick').length, 0, 'no wall of pills');
  // The name in the dropdown is what Generate sends.
  const prompt = doc.getElementById('st-prompt');
  prompt.value = 'a cat';
  prompt.dispatchEvent(new env.win.Event('input', { bubbles: true }));
  doc.getElementById('st-generate').click();
  await flush();
  assert.equal(gens(env)[0].body.model, AIR);
});

test('the image server: a tile per workflow, and the picture comes through this server', { skip: dom.skip }, async () => {
  const env = await page({ state: WSTATE, extra: { 'GET /api/apps/imagestudio/workflows': () => [200, WORKFLOWS] } });
  const { doc } = env;
  const grid = doc.getElementById('st-workflows');
  assert.equal(grid.hidden, false);
  const tiles = [...grid.querySelectorAll('button')];
  assert.equal(tiles.length, 1);
  const img = tiles[0].querySelector('img');
  assert.ok(img, 'the tile shows the picture it made last');
  assert.equal(img.getAttribute('src'),
    '/api/apps/imagestudio/workflows/thumb?u=%2Fui%2Fapi%2Fimage%3Ff%3D_previews%2Fwf-a.png');
  tiles[0].click();
  assert.equal(doc.getElementById('st-model').value, 'wf-a');
});

test('the image server: No-Image Mode asks for no picture at all', { skip: dom.skip }, async () => {
  const env = await page({ nim: true, state: WSTATE, extra: { 'GET /api/apps/imagestudio/workflows': () => [200, WORKFLOWS] } });
  const { doc } = env;
  const grid = doc.getElementById('st-workflows');
  assert.equal(grid.querySelectorAll('img').length, 0, 'no picture is fetched in No-Image Mode');
  assert.match(grid.textContent, /Workflow A/);
});

test('another provider never shows the workflow grid', { skip: dom.skip }, async () => {
  const env = await page({ state: STATE, extra: { 'GET /api/apps/imagestudio/workflows': () => [200, WORKFLOWS] } });
  const { doc } = env;
  assert.equal(doc.getElementById('st-workflows').hidden, true);
});

// ------------------------------------------- the picker under the Model field

const WROWS = [
  { name: 'wf-alpha', display_name: 'Alpha Portrait', state: 'ready', stale: false, thumb: '/ui/api/image?f=_previews/wf-alpha.png' },
  { name: 'wf-beta', display_name: 'Beta Landscape', state: 'ready', stale: false, thumb: '/ui/api/image?f=_previews/wf-beta.png' },
  { name: 'wf-gamma', display_name: 'Gamma Sketch', state: 'ready', stale: false, thumb: '/ui/api/image?f=_previews/wf-gamma.png' },
];
const WPAGE = { workflows: WROWS };
const wpage = (opts = {}) => page({
  state: WSTATE, extra: { 'GET /api/apps/imagestudio/workflows': () => [200, WPAGE] }, ...opts,
});
const picker = (doc) => doc.getElementById('st-picker');
const wfTiles = (doc) => [...doc.querySelectorAll('#st-workflows .st-wf-tile')];
const shownTiles = (doc) => wfTiles(doc).filter((b) => !b.closest('li').hidden);
const type = (env, input, value) => {
  input.value = value;
  input.dispatchEvent(new env.win.Event('input', { bubbles: true }));
};
// The gallery is a closed section: the small arrow in its header opens it.
const chevron = (doc) => doc.getElementById('st-picker-chevron');
const pickerBody = (doc) => doc.getElementById('st-picker-body');
const pickerHead = (doc) => doc.querySelector('.st-pickerhead');
const openGallery = (doc) => { chevron(doc).click(); return pickerBody(doc); };

test('the image server: ONE picker sits under the Model field and lists the workflows by name', { skip: dom.skip }, async () => {
  const { doc } = await wpage();
  const card = picker(doc);
  assert.equal(card.hidden, false, 'the picker card is there');
  // Under the Model field, not at the bottom of the page: the card comes after
  // the Model field inside the same row.
  const row = card.closest('.st-modelrow');
  assert.ok(row, 'the card lives in the provider/model row');
  const kids = [...row.children];
  const modelField = [...row.querySelectorAll('.st-field')].find((f) => f.querySelector('#st-model'));
  assert.ok(modelField);
  assert.ok(kids.indexOf(card) > kids.indexOf(modelField), 'the picker comes after the Model field');
  // The search box is labelled and the count says how many there are.
  const search = doc.getElementById('st-picker-search');
  assert.equal(search.getAttribute('aria-label'), EN['picker.search_workflows']);
  assert.match(doc.querySelector('.st-picker-count').textContent, /3 of 3/);
  // The workflows are listed by their display names, each with its own tile.
  openGallery(doc);
  assert.deepEqual(wfTiles(doc).map((b) => b.querySelector('.st-wf-name').textContent),
    ['Alpha Portrait', 'Beta Landscape', 'Gamma Sketch']);
  // The gallery is not on the page twice: there is one list of workflows.
  assert.equal(doc.querySelectorAll('#st-workflows').length, 1);
});

test('the image server: typing in the search box narrows the list and the count follows', { skip: dom.skip }, async () => {
  const env = await wpage();
  const { doc } = env;
  openGallery(doc);
  type(env, doc.getElementById('st-picker-search'), 'beta');
  assert.deepEqual(shownTiles(doc).map((b) => b.querySelector('.st-wf-name').textContent), ['Beta Landscape']);
  assert.match(doc.querySelector('.st-picker-count').textContent, /1 of 3/);
  // The display name and the name itself both match.
  type(env, doc.getElementById('st-picker-search'), 'gamma');
  assert.deepEqual(shownTiles(doc).map((b) => b.dataset.name), ['wf-gamma']);
  // Nothing matches: the list says so instead of looking empty.
  type(env, doc.getElementById('st-picker-search'), 'zzz');
  assert.equal(shownTiles(doc).length, 0);
  assert.equal(doc.querySelector('.st-picker-empty').hidden, false);
  assert.match(doc.querySelector('.st-picker-empty').textContent, /zzz/);
  // Escape clears the filter and the whole list is back.
  const search = doc.getElementById('st-picker-search');
  key(env, search, { key: 'Escape' });
  assert.equal(search.value, '');
  assert.equal(shownTiles(doc).length, 3);
  assert.match(doc.querySelector('.st-picker-count').textContent, /3 of 3/);
});

test('the image server: clicking a tile sets the model and marks that tile', { skip: dom.skip }, async () => {
  const { doc } = await wpage();
  openGallery(doc);
  const first = wfTiles(doc)[0];
  first.click();
  assert.equal(doc.getElementById('st-model').value, 'wf-alpha');
  assert.equal(first.getAttribute('aria-pressed'), 'true');
  assert.equal(first.getAttribute('aria-current'), 'true');
  assert.equal(first.querySelector('.st-wf-selected').hidden, false, 'the tile says it is the chosen one');
  assert.equal(wfTiles(doc)[1].getAttribute('aria-pressed'), 'false');
  assert.equal(wfTiles(doc)[1].querySelector('.st-wf-selected').hidden, true);
  // Another tile takes the mark.
  wfTiles(doc)[2].click();
  assert.equal(doc.getElementById('st-model').value, 'wf-gamma');
  assert.equal(wfTiles(doc)[2].getAttribute('aria-pressed'), 'true');
  assert.equal(wfTiles(doc)[0].getAttribute('aria-pressed'), 'false');
});

test('the image server: the Model box stays hand-editable', { skip: dom.skip }, async () => {
  const env = await wpage();
  const { doc, win } = env;
  openGallery(doc);
  const model = doc.getElementById('st-model');
  model.value = 'wf-beta';
  model.dispatchEvent(new win.Event('input', { bubbles: true }));
  assert.equal(model.value, 'wf-beta', 'a name typed by hand is kept');
  assert.equal(wfTiles(doc)[1].getAttribute('aria-pressed'), 'true', 'and the tile for it is marked');
});

test('the image server: a count of three starts three jobs, one picture each', { skip: dom.skip }, async () => {
  const env = await wpage({
    state: { ...WSTATE, max_in_flight: 10 },
    extra: { 'GET /api/apps/imagestudio/workflows': () => [200, WPAGE],
      'POST /api/apps/imagestudio/generate': () => new Promise(() => {}) },
  });
  const { doc, win } = env;
  const count = doc.getElementById('st-count');
  assert.equal(count.closest('.st-field').hidden, false, 'the image server can make more than one');
  assert.equal(count.max, '10');
  count.value = '3';
  count.dispatchEvent(new win.Event('change', { bubbles: true }));
  const prompt = doc.getElementById('st-prompt');
  prompt.value = 'a cat';
  prompt.dispatchEvent(new win.Event('input', { bubbles: true }));
  const model = doc.getElementById('st-model');
  model.value = 'wf-alpha';
  model.dispatchEvent(new win.Event('input', { bubbles: true }));
  doc.getElementById('st-generate').click();
  await flush();
  const jobs = gens(env);
  assert.equal(jobs.length, 3, 'one job per picture');
  assert.deepEqual(jobs.map((j) => j.body.n), [1, 1, 1], 'each job asks for one picture');
  assert.equal(jobs[0].body.model, 'wf-alpha', 'the workflow rides along');
  assert.equal(doc.querySelectorAll('.st-pending').length, 3, 'each job has its own tile');
  assert.match(doc.querySelector('.st-status').textContent, /3 jobs started/);
});

test('the image server: the count stops at the free slots, and says how many did not fit', { skip: dom.skip }, async () => {
  const env = await wpage({
    state: { ...WSTATE, max_in_flight: 2 },
    extra: { 'GET /api/apps/imagestudio/workflows': () => [200, WPAGE],
      'POST /api/apps/imagestudio/generate': () => new Promise(() => {}) },
  });
  const { doc, win } = env;
  const count = doc.getElementById('st-count');
  count.value = '3';
  count.dispatchEvent(new win.Event('change', { bubbles: true }));
  const prompt = doc.getElementById('st-prompt');
  prompt.value = 'a cat';
  prompt.dispatchEvent(new win.Event('input', { bubbles: true }));
  const model = doc.getElementById('st-model');
  model.value = 'wf-alpha';
  model.dispatchEvent(new win.Event('input', { bubbles: true }));
  doc.getElementById('st-generate').click();
  await flush();
  assert.equal(gens(env).length, 2, 'the cap is the cap');
  assert.match(doc.querySelector('.st-status').textContent, /Started 2 of 3 jobs.*1 did not fit/);
});

test('a provider that takes the whole count in one request still sends one request', { skip: dom.skip }, async () => {
  const env = await page();
  const { doc, win } = env;
  const count = doc.getElementById('st-count');
  count.value = '3';
  count.dispatchEvent(new win.Event('change', { bubbles: true }));
  const prompt = doc.getElementById('st-prompt');
  prompt.value = 'a cat';
  prompt.dispatchEvent(new win.Event('input', { bubbles: true }));
  doc.getElementById('st-generate').click();
  await flush();
  const jobs = gens(env);
  assert.equal(jobs.length, 1, 'one request carries the count');
  assert.equal(jobs[0].body.n, 3);
});

test('the image server: no dropdown and no wall of pills (the card lists the names once)', { skip: dom.skip }, async () => {
  const { doc } = await wpage();
  assert.equal(doc.getElementById('st-model-select').hidden, true, 'no dropdown for the image server');
  assert.equal(doc.querySelectorAll('.st-model-pick').length, 0, 'and no wall of pills');
  // The names are in the card, once.
  assert.ok(wfTiles(doc).some((b) => b.dataset.name === 'wf-alpha'));
  assert.equal(doc.getElementById('st-model').hidden, false, 'the Model box stays hand-editable');
});

test('the image server: No-Image Mode asks for no picture in the picker either', { skip: dom.skip }, async () => {
  const env = await wpage({ nim: true });
  const { doc } = env;
  assert.equal(doc.querySelectorAll('img').length, 0, 'no picture is fetched in No-Image Mode');
  assert.match(picker(doc).textContent, /Alpha Portrait/);
});

test('the image server: the arrow keys walk the list and Enter picks', { skip: dom.skip }, async () => {
  const env = await wpage();
  const { doc } = env;
  openGallery(doc);
  const search = doc.getElementById('st-picker-search');
  search.focus();
  key(env, search, { key: 'ArrowDown' });
  assert.equal(doc.activeElement, wfTiles(doc)[0], 'the first tile takes the focus');
  key(env, wfTiles(doc)[0], { key: 'ArrowDown' });
  assert.equal(doc.activeElement, wfTiles(doc)[1]);
  key(env, wfTiles(doc)[1], { key: 'ArrowUp' });
  assert.equal(doc.activeElement, wfTiles(doc)[0]);
  // The focused tile is a button: Enter activates it (a click stands in for it).
  wfTiles(doc)[1].click();
  assert.equal(doc.getElementById('st-model').value, 'wf-beta');
});

test('a provider with a long model list gets the compact chooser, not a wall of pills', { skip: dom.skip }, async () => {
  const many = { id: 'many', label: 'Many', shape: 'openai-images', configured: true, model: 'm-1',
    models: ['m-1', 'm-2', 'm-3', 'm-4', 'm-5', 'm-6', 'm-7', 'm-8', 'm-9'],
    supports: { reference: false, optimizer: false, seed: false, max_n: 4 } };
  const env = await page({ state: { ...STATE, providers: [many], default: 'many' } });
  const { doc } = env;
  assert.equal(doc.getElementById('st-model-select').hidden, true, 'no giant dropdown for a long list');
  assert.equal(doc.querySelectorAll('.st-model-pick').length, 0, 'and no wall of pills');
  const card = picker(doc);
  assert.equal(card.hidden, false, 'the chooser card is there');
  const toggle = doc.getElementById('st-picker-toggle');
  assert.equal(toggle.hidden, false);
  assert.equal(toggle.getAttribute('aria-expanded'), 'false');
  assert.equal(doc.getElementById('st-picker-body').hidden, true, 'the list opens on demand');
  toggle.click();
  assert.equal(toggle.getAttribute('aria-expanded'), 'true');
  assert.equal(doc.getElementById('st-picker-body').hidden, false);
  assert.equal(doc.querySelectorAll('#st-picker-models .st-picker-row').length, 9);
  assert.match(doc.querySelector('.st-picker-count').textContent, /9 of 9/);
  type(env, doc.getElementById('st-picker-search'), 'm-3');
  assert.equal(doc.querySelectorAll('#st-picker-models .st-picker-row').length, 1);
  doc.querySelector('#st-picker-models .st-picker-row').click();
  assert.equal(doc.getElementById('st-model').value, 'm-3');
});

test('the model control follows the provider: a dropdown for a short list, a text box when it names no model', { skip: dom.skip }, async () => {
  const { doc } = await page();
  assert.equal(doc.getElementById('st-model-select').hidden, false, 'two names: a dropdown');
  assert.equal(doc.getElementById('st-model').hidden, true, 'and no text box beside it');
  // A row that names no model at all: a text box, and no empty dropdown.
  const env2 = await page({ state: { ...STATE, providers: [PROVIDERS[1]], default: 'openai' } });
  const doc2 = env2.doc;
  assert.equal(doc2.getElementById('st-model-select').hidden, true);
  assert.equal(doc2.getElementById('st-model').hidden, false);
  assert.equal(doc2.getElementById('st-model-select').options.length, 0, 'no empty dropdown');
});

test('the count field shows only when the provider can make more than one picture', { skip: dom.skip }, async () => {
  const one = { id: 'one', label: 'One', shape: 'openai-images', configured: true, model: 'm', models: ['m'],
    supports: { reference: false, optimizer: false, seed: false, max_n: 1 } };
  const { doc } = await page({ state: { ...STATE, providers: [one], default: 'one' } });
  assert.equal(doc.getElementById('st-count').closest('.st-field').hidden, true, 'one picture: no count');
  const env2 = await page();
  const count = env2.doc.getElementById('st-count');
  assert.equal(count.closest('.st-field').hidden, false);
  assert.equal(count.max, '9', 'the count stops at what the model allows');
});

test('a provider with a short model list gets a dropdown and no card', { skip: dom.skip }, async () => {
  const { doc } = await page();
  const sel = doc.getElementById('st-model-select');
  assert.equal(sel.hidden, false);
  assert.deepEqual([...sel.options].map((o) => o.value), ['image-01', 'image-01-live']);
  assert.equal(doc.getElementById('st-model').hidden, true, 'no text box beside the dropdown');
  assert.equal(picker(doc).hidden, true, 'no card for a short list');
});

test('the image server: switching provider and back leaves the picker list whole', { skip: dom.skip }, async () => {
  const env = await wpage({ state: { ...STATE, providers: [WF, PROVIDERS[0]], default: 'workflows' } });
  const { doc, win } = env;
  const sel = doc.getElementById('st-provider');
  assert.equal(wfTiles(doc).length, 3);
  sel.value = 'minimax';
  sel.dispatchEvent(new win.Event('change', { bubbles: true }));
  assert.equal(wfTiles(doc).length, 0, 'another provider shows no workflows');
  sel.value = 'workflows';
  sel.dispatchEvent(new win.Event('change', { bubbles: true }));
  assert.equal(wfTiles(doc).length, 3, 'the picker is whole again');
  assert.match(doc.querySelector('.st-picker-count').textContent, /3 of 3/);
});

test('a failed workflow load says so in the card, and another provider does not inherit the error', { skip: dom.skip }, async () => {
  const env = await page({
    state: { ...STATE, providers: [WF, PROVIDERS[0]], default: 'workflows' },
    extra: { 'GET /api/apps/imagestudio/workflows': () => [500, { detail: 'the image server said no' }] },
  });
  const { doc, win } = env;
  const note = doc.querySelector('.st-workflows-note');
  assert.equal(note.hidden, false, 'the failure is said in the picker card');
  assert.match(note.textContent, /the image server said no/);
  // Another provider's card must not carry the image server's error.
  const sel = doc.getElementById('st-provider');
  sel.value = 'minimax';
  sel.dispatchEvent(new win.Event('change', { bubbles: true }));
  assert.equal(note.hidden, true, 'the note goes with the image server');
  assert.equal(note.textContent, '', 'and it leaves no text behind');
});


// ------------------------------------------- the gallery as a closed section

test('the image server: the gallery starts closed, behind a small named arrow', { skip: dom.skip }, async () => {
  const { doc } = await wpage();
  const card = picker(doc);
  assert.equal(card.hidden, false, 'the card is there');
  assert.equal(pickerBody(doc).hidden, true, 'the gallery is closed when the page loads');
  const arrow = chevron(doc);
  assert.equal(arrow.hidden, false, 'the arrow is there');
  assert.equal(arrow.tagName, 'BUTTON', 'the arrow is a real button');
  assert.equal(arrow.getAttribute('aria-expanded'), 'false');
  assert.equal(arrow.getAttribute('aria-controls'), 'st-picker-body');
  assert.ok(arrow.getAttribute('aria-label'), 'the arrow carries a name');
  assert.equal(arrow.getAttribute('aria-label'), EN['picker.expand']);
  // The header is a heading, so the section has a name.
  const title = card.querySelector('.st-pickertitle');
  assert.ok(title, 'the header has a title');
  assert.equal(title.tagName, 'H3');
});

test('the image server: the arrow opens and closes the gallery, and aria-expanded follows', { skip: dom.skip }, async () => {
  const { doc } = await wpage();
  const arrow = chevron(doc);
  arrow.click();
  assert.equal(pickerBody(doc).hidden, false, 'the gallery is open');
  assert.equal(arrow.getAttribute('aria-expanded'), 'true');
  assert.equal(arrow.getAttribute('aria-label'), EN['picker.collapse']);
  // The search box and the tiles are there when it is open.
  assert.ok(doc.getElementById('st-picker-search'));
  assert.equal(wfTiles(doc).length, 3);
  arrow.click();
  assert.equal(pickerBody(doc).hidden, true, 'and closed again');
  assert.equal(arrow.getAttribute('aria-expanded'), 'false');
  assert.equal(arrow.getAttribute('aria-label'), EN['picker.expand']);
});

test('the image server: the header says the count and the chosen workflow, closed or open', { skip: dom.skip }, async () => {
  const { doc } = await wpage();
  const head = pickerHead(doc);
  assert.match(head.textContent, /3 of 3/, 'the count is in the header while the card is closed');
  // Pick a workflow, the way a person does: open the gallery, click a tile.
  openGallery(doc);
  wfTiles(doc)[1].click();
  const chosen = doc.getElementById('st-picker-chosen');
  assert.equal(chosen.hidden, false, 'the header says which workflow is chosen');
  assert.match(chosen.textContent, /Beta Landscape/);
  // Close it: the header still says what is chosen and how many there are.
  chevron(doc).click();
  assert.equal(pickerBody(doc).hidden, true);
  assert.match(pickerHead(doc).textContent, /Beta Landscape/);
  assert.match(pickerHead(doc).textContent, /3 of 3/);
});

test('the image server: the search and the tiles still work after the gallery is expanded', { skip: dom.skip }, async () => {
  const env = await wpage();
  const { doc } = env;
  openGallery(doc);
  type(env, doc.getElementById('st-picker-search'), 'gamma');
  assert.deepEqual(shownTiles(doc).map((b) => b.dataset.name), ['wf-gamma']);
  assert.match(doc.querySelector('.st-picker-count').textContent, /1 of 3/);
  shownTiles(doc)[0].click();
  assert.equal(doc.getElementById('st-model').value, 'wf-gamma');
  assert.equal(doc.getElementById('st-picker-chevron').getAttribute('aria-expanded'), 'true',
    'picking a tile does not close the gallery');
});

test('switching provider clears the picker filter and puts the count back', { skip: dom.skip }, async () => {
  const env = await wpage({ state: { ...STATE, providers: [WF, PROVIDERS[0]], default: 'workflows' } });
  const { doc, win } = env;
  openGallery(doc);
  type(env, doc.getElementById('st-picker-search'), 'beta');
  assert.match(doc.querySelector('.st-picker-count').textContent, /1 of 3/);
  const sel = doc.getElementById('st-provider');
  sel.value = 'minimax';
  sel.dispatchEvent(new win.Event('change', { bubbles: true }));
  assert.equal(doc.getElementById('st-picker-search').value, '', 'the filter text does not survive the switch');
  // Back to the image server: the whole list is back and the count says so.
  sel.value = 'workflows';
  sel.dispatchEvent(new win.Event('change', { bubbles: true }));
  assert.equal(doc.getElementById('st-picker-search').value, '');
  assert.match(doc.querySelector('.st-picker-count').textContent, /3 of 3/);
  assert.equal(shownTiles(doc).length, 3, 'and every workflow is listed again');
});

test('a provider with a long model list loses its filter when the provider changes', { skip: dom.skip }, async () => {
  const many = { id: 'many', label: 'Many', shape: 'openai-images', configured: true, model: 'm-1',
    models: ['m-1', 'm-2', 'm-3', 'm-4', 'm-5', 'm-6', 'm-7', 'm-8', 'm-9'],
    supports: { reference: false, optimizer: false, seed: false, max_n: 4 } };
  const env = await page({ state: { ...STATE, providers: [many, PROVIDERS[0]], default: 'many' } });
  const { doc, win } = env;
  doc.getElementById('st-picker-toggle').click();
  type(env, doc.getElementById('st-picker-search'), 'm-3');
  assert.match(doc.querySelector('.st-picker-count').textContent, /1 of 9/);
  const sel = doc.getElementById('st-provider');
  sel.value = 'minimax';
  sel.dispatchEvent(new win.Event('change', { bubbles: true }));
  assert.equal(doc.getElementById('st-picker-search').value, '', 'the filter text does not survive the switch');
  sel.value = 'many';
  sel.dispatchEvent(new win.Event('change', { bubbles: true }));
  assert.match(doc.querySelector('.st-picker-count').textContent, /9 of 9/, 'the count is the whole list again');
});

// ------------------------------------------- Generate: the sticky bar

test('Generate: the button is still found by its id, rides in the sticky bar and still submits', { skip: dom.skip }, async () => {
  const env = await page({ generate: () => [200, { images: [], asked: 1 }] });
  const { doc } = env;
  const go = doc.getElementById('st-generate');
  assert.ok(go, 'the button is still found by id');
  assert.equal(go.className, 'st-btn st-primary st-go', 'and it keeps its classes');
  const bar = go.closest('.st-gobar');
  assert.ok(bar, 'it rides in the bar that sticks to the bottom of the form');
  assert.equal(bar.parentElement.lastElementChild, bar, 'the bar is the last thing in the form');
  assert.match(go.title, /Ctrl\+Enter|⌘\+Enter/, 'the shortcut is still named');
  // And it still submits.
  const box = doc.getElementById('st-prompt');
  box.value = 'a cat';
  box.dispatchEvent(new env.win.Event('input', { bubbles: true }));
  go.click();
  await flush();
  assert.equal(gens(env).length, 1, 'one press, one request');
});

test('the Generate bar sticks to the bottom of the form (and the phone bar is a thumb-sized strip)', () => {
  const css = readFileSync(join(APP, 'static', 'studio.css'), 'utf8');
  const bar = /\.st-gobar \{([^}]*)\}/.exec(css);
  assert.ok(bar, 'the sticky bar has a rule');
  assert.match(bar[1], /position: sticky/);
  assert.match(bar[1], /bottom: 0/);
  assert.match(bar[1], /background: var\(--bg-primary\)/, 'the bar is opaque');
  assert.match(bar[1], /backdrop-filter/, 'and blurred behind');
  assert.match(css, /\.st-gobar \{[^}]*env\(safe-area-inset-bottom/, 'the phone bar keeps clear of the home indicator');
  assert.match(css, /\.st-gobar \.st-go \{[^}]*min-height: 44px/, 'the phone button is a thumb tall');
});

test('the wheel over the controls column scrolls the page when the column is at its end', { skip: dom.skip }, async () => {
  const env = await page();
  const { doc, win } = env;
  const col = doc.querySelector('.st-controls');
  // The column is a scrollport with room to move.
  Object.defineProperty(col, 'scrollHeight', { configurable: true, value: 1000 });
  Object.defineProperty(col, 'clientHeight', { configurable: true, value: 400 });
  let top = 100;
  Object.defineProperty(col, 'scrollTop', { configurable: true, get: () => top, set: (v) => { top = v; } });
  const wheel = (deltaY) => {
    const ev = new win.WheelEvent('wheel', { deltaY, bubbles: true, cancelable: true });
    col.dispatchEvent(ev);
    return ev;
  };
  // The column can still move down: it keeps the wheel.
  assert.equal(wheel(100).defaultPrevented, false, 'the column scrolls itself');
  // At the bottom: the page takes the wheel, so the results move.
  top = 600;
  assert.equal(wheel(100).defaultPrevented, true, 'the page scrolls instead');
  // At the top, the same the other way round.
  top = 0;
  assert.equal(wheel(-100).defaultPrevented, true, 'the page scrolls instead');
  top = 200;
  assert.equal(wheel(-100).defaultPrevented, false, 'the column scrolls itself');
});

test('a wheel over a list inside the controls column scrolls that list, not the page', { skip: dom.skip }, async () => {
  const env = await wpage();
  const { doc, win } = env;
  const col = doc.querySelector('.st-controls');
  // A tall window: the column is not a scrollport at all, so the column's own
  // metrics cannot decide what a wheel over a list inside it means.
  Object.defineProperty(col, 'scrollHeight', { configurable: true, value: 400 });
  Object.defineProperty(col, 'clientHeight', { configurable: true, value: 400 });
  // Where the page stands, as the browser keeps it.
  const pageBox = doc.scrollingElement || doc.documentElement;
  let pageTop = 0;
  Object.defineProperty(pageBox, 'scrollTop', { configurable: true, get: () => pageTop, set: (v) => { pageTop = v; } });
  // A list inside the column, with room to move: the image server's gallery.
  const list = doc.getElementById('st-workflows');
  assert.ok(col.contains(list), 'the gallery is inside the controls column');
  let listTop = 0;
  Object.defineProperty(list, 'scrollHeight', { configurable: true, value: 600 });
  Object.defineProperty(list, 'clientHeight', { configurable: true, value: 300 });
  Object.defineProperty(list, 'scrollTop', { configurable: true, get: () => listTop, set: (v) => { listTop = v; } });
  const wheel = (target, deltaY) => {
    const ev = new win.WheelEvent('wheel', { deltaY, bubbles: true, cancelable: true });
    target.dispatchEvent(ev);
    return ev;
  };
  // The gallery can still scroll down: the wheel belongs to the gallery.
  const over = wheel(list, 100);
  assert.equal(over.defaultPrevented, false, 'the gallery keeps the wheel');
  assert.equal(pageTop, 0, 'and the page did not scroll');
  // The gallery is at its end: the wheel goes on to the page, and the results move.
  listTop = 300;
  const atEnd = wheel(list, 100);
  assert.equal(atEnd.defaultPrevented, true, 'the page takes the wheel');
  assert.equal(pageTop, 100, 'the page really scrolled');
  // The recent prompts are a list of their own, and they scroll as well.
  const recents = doc.querySelector('.st-recents');
  assert.ok(col.contains(recents), 'the recent prompts are inside the controls column');
  let recentTop = 0;
  Object.defineProperty(recents, 'scrollHeight', { configurable: true, value: 400 });
  Object.defineProperty(recents, 'clientHeight', { configurable: true, value: 180 });
  Object.defineProperty(recents, 'scrollTop', { configurable: true, get: () => recentTop, set: (v) => { recentTop = v; } });
  pageTop = 0;
  const overRecents = wheel(recents, 100);
  assert.equal(overRecents.defaultPrevented, false, 'the recent prompts keep the wheel');
  assert.equal(pageTop, 0, 'and the page did not scroll');
});

test('the page itself is never told not to scroll', () => {
  const css = readFileSync(join(APP, 'static', 'studio.css'), 'utf8');
  assert.doesNotMatch(css, /overscroll-behavior/, 'the column does not keep the wheel to itself');
  assert.doesNotMatch(css, /(?:^|[\s,{])(?:html|body)[^{]*\{[^}]*overflow:\s*hidden/, 'the page keeps scrolling');
});

test('the arrow on a phone is a thumb-sized tap target (the phone rule must out-specify the base rule)', () => {
  // The base rule that sizes the arrow sits AFTER the phone media query, so a
  // phone rule with the same classes would lose: the arrow stayed 26x26 on a
  // phone while every other control in that card was a thumb tall.
  const css = readFileSync(join(APP, 'static', 'studio.css'), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');
  const phoneBlock = /@media\s*\(max-width:\s*860px\)\s*\{([\s\S]*?)\n\}/.exec(css);
  assert.ok(phoneBlock, 'the phone media query is there');
  const rules = (chunk) => {
    const out = [];
    const re = /([^{}]+)\{([^{}]*)\}/g;
    let m;
    while ((m = re.exec(chunk))) out.push({ sel: m[1].trim(), body: m[2] });
    return out;
  };
  const phone = rules(phoneBlock[1]).filter((r) => r.sel.endsWith('.st-btn.st-picker-chevron'));
  assert.equal(phone.length, 1, 'the phone media query sizes the arrow once');
  const base = rules(css.replace(phoneBlock[0], '')).filter((r) => r.sel === '.st-btn.st-picker-chevron');
  assert.equal(base.length, 1, 'the base rule sizes the arrow once');
  const classes = (sel) => (sel.match(/\.[\w-]+/g) || []).length;
  assert.ok(classes(phone[0].sel) > classes(base[0].sel),
    `the phone rule (${phone[0].sel}) must out-specify the base rule (${base[0].sel})`);
  assert.match(phone[0].body, /width: 44px/, 'the arrow is 44px wide on a phone');
  assert.match(phone[0].body, /height: 44px/, 'and 44px tall');
  assert.match(phone[0].body, /min-width: 44px/, 'and never smaller than a thumb');
  assert.match(phone[0].body, /min-height: 44px/, 'in either direction');
});

// ------------------------------------------- the rest of the sweep

test('the image server: a failed load is said in the header, so a closed card does not hide it', { skip: dom.skip }, async () => {
  const env = await page({
    state: { ...STATE, providers: [WF, PROVIDERS[0]], default: 'workflows' },
    extra: { 'GET /api/apps/imagestudio/workflows': () => [500, { detail: 'the image server said no' }] },
  });
  const { doc } = env;
  const note = doc.querySelector('.st-workflows-note');
  assert.equal(note.hidden, false, 'the failure is said');
  assert.match(note.textContent, /the image server said no/);
  // It is in the header: the closed card says it too.
  assert.ok(pickerHead(doc).contains(note), 'the note is in the header');
  assert.equal(pickerBody(doc).hidden, true, 'the card is closed');
  assert.match(pickerHead(doc).textContent, /the image server said no/);
});

test('the image server: while the list loads the card says it is loading, not that it is empty', { skip: dom.skip }, async () => {
  let release = null;
  const gate = new Promise((r) => { release = r; });
  const env = await page({
    state: { ...STATE, providers: [WF, PROVIDERS[0]], default: 'workflows' },
    extra: { 'GET /api/apps/imagestudio/workflows': async () => { await gate; return [200, WPAGE]; } },
  });
  const { doc } = env;
  const note = doc.querySelector('.st-workflows-note');
  assert.equal(note.hidden, false, 'the card says it is loading');
  assert.match(note.textContent, /Loading/);
  assert.match(pickerHead(doc).textContent, /Loading/, 'and it says it in the header');
  release();
  await flush(); await flush();
  assert.equal(note.hidden, true, 'and the note goes when the list is there');
  assert.equal(wfTiles(doc).length, 3);
});

test('a workflow name does not survive the switch to another provider (and back)', { skip: dom.skip }, async () => {
  const env = await wpage({ state: { ...STATE, providers: [WF, PROVIDERS[0]], default: 'workflows' } });
  const { doc, win } = env;
  openGallery(doc);
  wfTiles(doc)[0].click();
  assert.equal(doc.getElementById('st-model').value, 'wf-alpha');
  const sel = doc.getElementById('st-provider');
  sel.value = 'minimax';
  sel.dispatchEvent(new win.Event('change', { bubbles: true }));
  assert.equal(doc.getElementById('st-model').value, '', 'the workflow name is gone: MiniMax never heard of it');
});

test('a name the new provider offers survives the switch', { skip: dom.skip }, async () => {
  const env = await page();
  const { doc, win } = env;
  setModel(doc, win, 'image-01');
  const sel = doc.getElementById('st-provider');
  sel.value = 'openai';
  sel.dispatchEvent(new win.Event('change', { bubbles: true }));
  // OpenAI lists no models in this state, so the name in use stays.
  assert.equal(modelControl(doc).value, 'image-01');
  assert.equal(doc.getElementById('st-model').hidden, false, 'and a provider with no names gets a text box');
});

test('the arrow keys do nothing while the gallery is closed', { skip: dom.skip }, async () => {
  const env = await wpage();
  const { doc } = env;
  const arrow = chevron(doc);
  arrow.focus();
  key(env, arrow, { key: 'ArrowDown' });
  assert.equal(doc.activeElement, arrow, 'the focus stays on the arrow');
  assert.notEqual(doc.activeElement, wfTiles(doc)[0]);
});

test('the workflow names never land on another provider (the list can arrive late)', { skip: dom.skip }, async () => {
  let release = null;
  const gate = new Promise((r) => { release = r; });
  const env = await page({
    state: { ...STATE, providers: [WF, PROVIDERS[0]], default: 'workflows' },
    extra: { 'GET /api/apps/imagestudio/workflows': async () => { await gate; return [200, WPAGE]; } },
  });
  const { doc, win } = env;
  // The person switches to MiniMax while the image server's list is still loading.
  const sel = doc.getElementById('st-provider');
  sel.value = 'minimax';
  sel.dispatchEvent(new win.Event('change', { bubbles: true }));
  release();
  await flush(); await flush();
  const models = [...doc.querySelectorAll('#st-model-select option')].map((o) => o.value);
  assert.deepEqual(models, ['image-01', 'image-01-live'], 'MiniMax still offers its own models');
  assert.equal(doc.getElementById('st-model-select').hidden, false, 'and its own dropdown');
});

// ------------------------------------------- fix round 1: the reviewer's punch-list

test('the header follows a name typed by hand: a closed card never names another workflow', { skip: dom.skip }, async () => {
  const env = await wpage();
  const { doc } = env;
  const model = doc.getElementById('st-model');
  const chosen = doc.getElementById('st-picker-chosen');
  openGallery(doc);
  wfTiles(doc)[0].click();
  assert.match(chosen.textContent, /Alpha Portrait/, 'the header names the chosen workflow');
  // A name typed by hand: the tile marking moves, so the header must move too.
  type(env, model, 'wf-beta');
  assert.equal(wfTiles(doc)[1].getAttribute('aria-pressed'), 'true', 'the tile for it is marked');
  assert.match(chosen.textContent, /Beta Landscape/, 'and the header names the typed one');
  assert.doesNotMatch(chosen.textContent, /Alpha Portrait/, 'the old name is gone from the header');
  // Closed, the header still says what the form will use.
  chevron(doc).click();
  assert.equal(pickerBody(doc).hidden, true, 'the card is closed');
  assert.match(chosen.textContent, /Beta Landscape/, 'the closed card names the workflow in use');
});

test('a workflow name never reaches another provider, not even one that lists no models', { skip: dom.skip }, async () => {
  // OpenAI lists no models in this state: the name must still go.
  const env = await wpage({ state: { ...STATE, providers: [WF, PROVIDERS[1]], default: 'workflows' } });
  const { doc, win } = env;
  openGallery(doc);
  wfTiles(doc)[0].click();
  assert.equal(doc.getElementById('st-model').value, 'wf-alpha');
  const sel = doc.getElementById('st-provider');
  sel.value = 'openai';
  sel.dispatchEvent(new win.Event('change', { bubbles: true }));
  assert.equal(doc.getElementById('st-model').value, '', 'the workflow name is gone: OpenAI never heard of it');
  // And it is not sent to the other provider either.
  fill(env, 'a red fox');
  doc.getElementById('st-generate').click();
  await flush();
  const sent = gens(env).pop();
  assert.equal(sent.body.model, '', 'the request carries no workflow name');
});

test('the count line is silent while the workflow list is loading', { skip: dom.skip }, async () => {
  let release = null;
  const gate = new Promise((r) => { release = r; });
  const env = await page({
    state: { ...STATE, providers: [WF, PROVIDERS[0]], default: 'workflows' },
    extra: { 'GET /api/apps/imagestudio/workflows': async () => { await gate; return [200, WPAGE]; } },
  });
  const { doc } = env;
  const count = doc.querySelector('.st-picker-count');
  assert.equal(count.hidden, true, 'no "0 of 0" beside the loading note');
  assert.equal(count.textContent, '', 'the count line says nothing yet');
  assert.match(pickerHead(doc).textContent, /Loading/, 'and the header says it is loading');
  release();
  await flush(); await flush();
  assert.equal(count.hidden, false, 'the count comes back with the list');
  assert.match(count.textContent, /3 of 3/);
});


// ---- the form is remembered on this device (one draft per profile) -----------

const PK_A = 'a1b2c3d4e5f60718';
const PK_B = '0011223344556677';
const DKEY = (k) => `dispatch-imagestudio-draft:${k}`;
const DRAFT = JSON.stringify({ v: 1, provider: 'minimax', model: 'image-01-live', prompt: 'a red fox', ratio: '16:9', seed: '42', n: 3, optimizer: true });

test('a saved draft restores provider, model, prompt, ratio, count, seed and optimizer; nothing generates', { skip: dom.skip }, async () => {
  const env = await page({ state: { ...STATE, profile_key: PK_A }, store: { [DKEY(PK_A)]: DRAFT } });
  const { doc } = env;
  assert.equal(doc.getElementById('st-provider').value, 'minimax');
  assert.equal(modelControl(doc).value, 'image-01-live');
  assert.equal(doc.getElementById('st-prompt').value, 'a red fox');
  assert.equal(doc.getElementById('st-ratio').value, '16:9');
  assert.equal(doc.getElementById('st-count').value, '3');
  assert.equal(doc.getElementById('st-seed').value, '42');
  assert.equal(doc.getElementById('st-optimizer').checked, true);
  assert.equal(gens(env).length, 0);
});

test('another profile on the same device starts with an empty form', { skip: dom.skip }, async () => {
  const { doc } = await page({ state: { ...STATE, profile_key: PK_B }, store: { [DKEY(PK_A)]: DRAFT } });
  assert.equal(doc.getElementById('st-prompt').value, '');
  assert.equal(doc.getElementById('st-ratio').value, '1:1');
});

test('an invalid provider is dropped, the prompt still restores', { skip: dom.skip }, async () => {
  const bad = JSON.stringify({ v: 1, provider: 'xai', model: 'nope', prompt: 'keep me', ratio: '1:1', seed: '', n: 1, optimizer: false });
  const { doc } = await page({ state: { ...STATE, profile_key: PK_A }, store: { [DKEY(PK_A)]: bad } });
  assert.equal(doc.getElementById('st-provider').value, 'minimax', 'the unusable provider falls back to the default');
  assert.equal(doc.getElementById('st-prompt').value, 'keep me');
});

test('a draft that could not be put back in full is kept as stored until the user edits', { skip: dom.skip }, async () => {
  const bad = JSON.stringify({ v: 1, provider: 'xai', model: 'grok-pic', prompt: 'keep me', ratio: '1:1', seed: '', n: 1, optimizer: false });
  const env = await page({ state: { ...STATE, profile_key: PK_A }, store: { [DKEY(PK_A)]: bad } });
  const { doc, win } = env;
  win.dispatchEvent(new win.Event('pagehide'));
  assert.equal(win.localStorage.getItem(DKEY(PK_A)), bad, 'opening and leaving the page keeps the stored provider and model');
  const box = doc.getElementById('st-prompt');
  box.value = 'keep me, edited';
  box.dispatchEvent(new win.Event('input', { bubbles: true }));
  win.dispatchEvent(new win.Event('pagehide'));
  assert.equal(JSON.parse(win.localStorage.getItem(DKEY(PK_A))).prompt, 'keep me, edited', 'an edit saves the form as it is');
});

test('typing saves under the profile key, the Reset button clears it and the form', { skip: dom.skip }, async () => {
  const env = await page({ state: { ...STATE, profile_key: PK_A } });
  const { doc, win } = env;
  const box = doc.getElementById('st-prompt');
  box.value = 'a blue whale';
  box.dispatchEvent(new win.Event('input', { bubbles: true }));
  win.dispatchEvent(new win.Event('pagehide'));
  const saved = JSON.parse(win.localStorage.getItem(DKEY(PK_A)));
  assert.equal(saved.prompt, 'a blue whale');
  assert.deepEqual(Object.keys(saved).sort(), ['model', 'n', 'optimizer', 'prompt', 'provider', 'ratio', 'seed', 'v']);
  assert.equal(win.localStorage.getItem(DKEY(PK_B)), null);
  doc.getElementById('st-reset').click();
  assert.equal(box.value, '');
  assert.equal(win.localStorage.getItem(DKEY(PK_A)), null, 'the draft is gone');
  assert.equal(doc.activeElement, box, 'the focus lands on the prompt');
});

test('a 403 from /state reads and writes no draft', { skip: dom.skip }, async () => {
  const env = await page({ extra: { 'GET /api/apps/imagestudio/state': () => [403, { detail: 'no' }] }, store: { [DKEY(PK_A)]: DRAFT } });
  assert.equal(env.win.localStorage.getItem(DKEY(PK_A)), DRAFT, 'untouched');
  assert.ok(env.doc.querySelector('.st-locked'), 'the page shows the locked note');
  assert.equal(env.doc.getElementById('st-prompt'), null);
});

test('pictures the server is still making are announced', { skip: dom.skip }, async () => {
  const env = await page({ state: { ...STATE, profile_key: PK_A, in_flight: 2 } });
  const line = env.doc.querySelector('.st-inflight');
  assert.equal(line.hidden, false);
  assert.equal(line.textContent, '2 pictures are still being made');
});
