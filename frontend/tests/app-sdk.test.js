// js/app-sdk.js — the one module an app page imports from the shell
// (docs/design/2026-09-25-apps.md). Exercised against jsdom with fetch
// stubbed: the api() error contract, the app's own locale files with English
// fallback, the theme copied from the shell, and the postMessage channel in
// both directions (same origin, parent only).

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { jsdom, dom, setupDom, flush } from './helpers/app-test-env.js';

const HERE = dirname(fileURLToPath(import.meta.url));
const STATIC = join(HERE, '..', 'static');
const SDK_SRC = readFileSync(join(STATIC, 'js', 'app-sdk.js'), 'utf8');
const INDEX = readFileSync(join(STATIC, 'index.html'), 'utf8');

const PAGE = '<!doctype html><html><head><link rel="stylesheet" href="/apps/demo/app.css"></head><body></body></html>';

async function freshSdk() {
  return await import(`../static/js/app-sdk.js?t=${Math.random()}`);
}

/** Make this jsdom window look framed by `parent` (a fake shell window). */
function frameUnder(win, parent) {
  Object.defineProperty(win, 'parent', { configurable: true, get: () => parent });
}

function shellMessage(win, data, { origin = win.location.origin, source = win.parent } = {}) {
  const ev = new win.MessageEvent('message', { data, origin });
  Object.defineProperty(ev, 'source', { value: source });
  win.dispatchEvent(ev);
}

// ------------------------------------------------------------------ static pins

test('the SDK reads the SAME storage keys, languages and theme sheets as the shell', () => {
  // Palette key + default: index.html's ui-theme.js script tag.
  const tag = /<script src="\/static\/ui-theme\/ui-theme\.js"[^>]*>/.exec(INDEX)[0];
  assert.match(tag, /data-storage-key="dispatch-palette"/);
  assert.match(SDK_SRC, /PALETTE_KEY = 'dispatch-palette'/);
  assert.match(tag, /data-default="glacier"/);
  assert.match(SDK_SRC, /DEFAULT_PALETTE = 'glacier'/);
  // Language key + list: i18n.js / the no-FOUC script.
  const i18n = readFileSync(join(STATIC, 'js', 'i18n.js'), 'utf8');
  assert.match(i18n, /STORAGE_KEY = 'dispatch-lang'/);
  assert.match(SDK_SRC, /LANG_KEY = 'dispatch-lang'/);
  const fouc = /var SUPPORTED = (\[[^\]]+\]);/.exec(INDEX)[1].replace(/'/g, '"');
  assert.match(SDK_SRC, new RegExp(`SUPPORTED_LANGS = ${fouc.replace(/[[\]]/g, '\\$&').replace(/"/g, "'")}`));
  // The token sheets, at the version index.html links them.
  const themeCss = /href="(\/static\/theme\.css\?v=\d+)"/.exec(INDEX)[1];
  assert.ok(SDK_SRC.includes(`'${themeCss}'`), `app-sdk.js must link ${themeCss}`);
  for (const href of ['/static/ui-theme/ui-theme.css', '/static/ui-theme/adapters/dispatch-extra.css']) {
    assert.ok(INDEX.includes(`href="${href}"`) && SDK_SRC.includes(`'${href}'`), href);
  }
});

test('the SDK has no side effects at import and imports only util.js from the shell', () => {
  const imports = [...SDK_SRC.matchAll(/^import .* from '([^']+)';$/gm)].map((m) => m[1]);
  assert.deepEqual(imports, ['./util.js?v=23']);
  // No top-level listener, fetch or DOM write (they all live inside functions).
  const top = SDK_SRC.split('\n').filter((l) => /^(?:window|document|fetch|addEventListener)\b/.test(l));
  assert.deepEqual(top, []);
});

test('appIdFromPath', { skip: dom.skip }, async () => {
  setupDom(PAGE, 'http://127.0.0.1:8765/');
  const sdk = await freshSdk();
  assert.equal(sdk.appIdFromPath('/apps/jobboard/'), 'jobboard');
  assert.equal(sdk.appIdFromPath('/apps/job-board/x/y.js'), 'job-board');
  assert.equal(sdk.appIdFromPath('/apps/Bad/'), null);
  assert.equal(sdk.appIdFromPath('/static/js/main.js'), null);
  assert.equal(sdk.APP_ID, null, 'in the shell the path names no app');
});

// ------------------------------------------------------------------ api()

test('api(): same-origin JSON with credentials, query + body, server detail; a 403 is a plain error', { skip: dom.skip }, async () => {
  const { calls, routes } = setupDom(PAGE, 'http://127.0.0.1:8765/apps/demo/');
  const sdk = await freshSdk();
  routes['GET /api/apps/demo/items?state=yes&n=2'] = () => [200, { items: [1] }];
  assert.deepEqual(await sdk.api('/api/apps/demo/items', { query: { state: 'yes', n: 2, empty: '', gone: null } }), { items: [1] });
  assert.equal(calls[0].opts.credentials, 'same-origin');
  assert.equal(calls[0].opts.headers.Accept, 'application/json');

  routes['POST /api/apps/demo/items'] = () => [200, { ok: true }];
  await sdk.api('/api/apps/demo/items', { method: 'post', body: { a: 1 } });
  assert.equal(calls[1].method, 'POST');
  assert.deepEqual(calls[1].body, { a: 1 });
  assert.equal(calls[1].opts.headers['Content-Type'], 'application/json');

  // 4.0: no Safe Mode, so even a 3.x-shaped `decoy` body is an ordinary 403 (DecoyError is a
  // deprecated export that is never thrown).
  routes['GET /api/apps/demo/secret'] = () => [403, { detail: 'Unlock for full access', decoy: true }];
  await assert.rejects(sdk.api('/api/apps/demo/secret'), (e) => e.status === 403 && !(e instanceof sdk.DecoyError));
  assert.equal(typeof sdk.DecoyError, 'function');

  routes['GET /api/apps/demo/bad'] = () => [422, { detail: [{ msg: 'field required' }] }];
  await assert.rejects(sdk.api('/api/apps/demo/bad'), (e) => e.status === 422 && e.message === 'field required' && !(e instanceof sdk.DecoyError));
  routes['GET /api/apps/demo/gone'] = () => [404, { detail: 'Job not found' }];
  await assert.rejects(sdk.api('/api/apps/demo/gone'), (e) => e.status === 404 && e.message === 'Job not found');
  // A plain 403 is an ordinary error.
  routes['GET /api/apps/demo/forbidden'] = () => [403, { detail: 'nope' }];
  await assert.rejects(sdk.api('/api/apps/demo/forbidden'), (e) => e.status === 403 && !(e instanceof sdk.DecoyError));

  // Only this origin: an absolute or protocol-relative URL is refused outright.
  await assert.rejects(sdk.api('https://evil.example/x'), /must start with/);
  await assert.rejects(sdk.api('//evil.example/x'), /must start with/);
  assert.equal(calls.filter((c) => c.url.includes('evil')).length, 0);
});

// ------------------------------------------------------------------ i18n

test('t()/ready()/has(): the app’s own locale, English fallback, the shell’s language', { skip: dom.skip }, async () => {
  const { win, calls, routes } = setupDom(PAGE, 'http://127.0.0.1:8765/apps/demo/');
  win.localStorage.setItem('dispatch-lang', 'de');
  routes['GET /apps/demo/locales/en.json'] = () => [200, { $meta: {}, hello: 'Hello {name}', only_en: 'Only English', n: { one: '{count} job', other: '{count} jobs' } }];
  routes['GET /apps/demo/locales/de.json'] = () => [200, { $meta: {}, hello: 'Hallo {name}', n: { one: '{count} Stelle', other: '{count} Stellen' } }];
  const sdk = await freshSdk();
  assert.equal(sdk.APP_ID, 'demo');
  assert.equal(sdk.t('hello'), 'hello', 'before ready(): the key, never blank');
  await sdk.ready();
  assert.equal(sdk.lang(), 'de');
  assert.equal(win.document.documentElement.getAttribute('lang'), 'de');
  assert.equal(sdk.t('hello', { name: 'Ana' }), 'Hallo Ana');
  assert.equal(sdk.t('only_en'), 'Only English', 'English fills a gap');
  assert.equal(sdk.t('nope'), 'nope');
  assert.equal(sdk.has('hello'), true);
  assert.equal(sdk.has('only_en'), false, 'has() asks the ACTIVE language');
  assert.equal(sdk.t('n', { count: 1 }), '1 Stelle');
  assert.equal(sdk.t('n', { count: 3 }), '3 Stellen');
  assert.deepEqual(calls.map((c) => c.url).sort(), ['/apps/demo/locales/de.json', '/apps/demo/locales/en.json']);

  // Arabic sets the direction too.
  routes['GET /apps/demo/locales/ar.json'] = () => [200, { hello: 'مرحبا {name}' }];
  await sdk.ready('ar');
  assert.equal(win.document.documentElement.getAttribute('dir'), 'rtl');
});

test('forApp(id): a bound instance for a thread hook running in the shell', { skip: dom.skip }, async () => {
  const { routes } = setupDom(PAGE, 'http://127.0.0.1:8765/');
  routes['GET /apps/jobboard/locales/en.json'] = () => [200, { title: 'Job Board' }];
  routes['GET /apps/jobboard/locales/fr.json'] = () => [404, {}];
  const sdk = await freshSdk();
  const app = sdk.forApp('jobboard');
  await app.ready('fr');
  assert.equal(app.t('title'), 'Job Board', 'a missing locale file falls back to English');
  assert.equal(app.api, sdk.api);
  assert.equal(document.documentElement.getAttribute('lang'), null, 'a hook instance never restamps the shell');
});

// ------------------------------------------------------------------ messaging

test('openThread/close/toast/setTitle post to the parent, at our own origin only', { skip: dom.skip }, async () => {
  const { win } = setupDom(PAGE, 'http://127.0.0.1:8765/apps/demo/');
  const posted = [];
  frameUnder(win, { postMessage: (m, o) => posted.push([m, o]) });
  const sdk = await freshSdk();
  assert.equal(sdk.openThread('t-1', { job_id: 'j-1' }), true);
  sdk.close();
  sdk.toast('Saved');
  sdk.toast('Broke', true);
  sdk.setTitle('March');
  assert.deepEqual(posted.map(([m]) => m), [
    { threadId: 't-1', hint: { job_id: 'j-1' }, type: 'dispatch:open-thread' },
    { type: 'dispatch:close' },
    { text: 'Saved', error: false, type: 'dispatch:toast' },
    { text: 'Broke', error: true, type: 'dispatch:toast' },
    { text: 'March', type: 'dispatch:set-title' },
  ]);
  assert.ok(posted.every(([, o]) => o === 'http://127.0.0.1:8765'), 'targetOrigin is location.origin, never "*"');
  assert.equal(sdk.openThread(''), false);
});

test('opened on its own (no parent), the channel is a quiet no-op', { skip: dom.skip }, async () => {
  setupDom(PAGE, 'http://127.0.0.1:8765/apps/demo/');
  const sdk = await freshSdk();
  assert.equal(sdk.openThread('t-1'), false);
  assert.equal(sdk.toast('x'), false);
});

test('onMessage: only the parent, only our origin, only dispatch:* — and theme applies itself', { skip: dom.skip }, async () => {
  const { win } = setupDom(PAGE, 'http://127.0.0.1:8765/apps/demo/');
  const parent = { postMessage() {} };
  frameUnder(win, parent);
  const sdk = await freshSdk();
  const got = [];
  const off = sdk.onMessage((m) => got.push(m.type));
  shellMessage(win, { type: 'dispatch:frame', frame: { type: 'app:demo:x' } });
  shellMessage(win, { type: 'dispatch:frame' }, { origin: 'http://evil.example' });
  shellMessage(win, { type: 'dispatch:frame' }, { source: win });
  shellMessage(win, { type: 'not-ours' });
  shellMessage(win, 'a string');
  shellMessage(win, { type: 'dispatch:theme', palette: 'paper', theme: 'light', contrast: 'standard' });
  await flush();
  assert.deepEqual(got, ['dispatch:frame', 'dispatch:theme']);
  const root = win.document.documentElement;
  assert.equal(root.getAttribute('data-palette'), 'paper');
  assert.equal(root.getAttribute('data-theme'), 'light');
  // Purple is the :root base: no attribute.
  shellMessage(win, { type: 'dispatch:theme', palette: null, theme: 'dark', contrast: 'standard' });
  await flush();
  assert.equal(root.hasAttribute('data-palette'), false);
  off();
  shellMessage(win, { type: 'dispatch:frame' });
  await flush();
  assert.equal(got.length, 3, 'unsubscribed');
});

test('dispatch:lang reloads the dictionaries BEFORE the page hears about it', { skip: dom.skip }, async () => {
  const { win, routes } = setupDom(PAGE, 'http://127.0.0.1:8765/apps/demo/');
  frameUnder(win, { postMessage() {} });
  routes['GET /apps/demo/locales/en.json'] = () => [200, { hi: 'Hi' }];
  routes['GET /apps/demo/locales/ja.json'] = () => [200, { hi: 'こんにちは' }];
  const sdk = await freshSdk();
  await sdk.ready('en');
  const seen = [];
  sdk.onMessage((m) => { if (m.type === 'dispatch:lang') seen.push(sdk.t('hi')); });
  shellMessage(win, { type: 'dispatch:lang', lang: 'ja' });
  await flush();
  assert.deepEqual(seen, ['こんにちは']);
  assert.equal(win.document.documentElement.getAttribute('lang'), 'ja');
});

// ------------------------------------------------------------------ theme

test('theme(): framed, it copies the shell’s own attributes and links the token sheets first', { skip: dom.skip }, async () => {
  const { win } = setupDom(PAGE, 'http://127.0.0.1:8765/apps/demo/');
  const shell = new jsdom.JSDOM('<!doctype html><html data-palette="forest" data-theme="dark" data-contrast-profile="standard"><body></body></html>').window;
  frameUnder(win, { postMessage() {}, document: shell.document });
  const sdk = await freshSdk();
  sdk.theme();
  const root = win.document.documentElement;
  assert.equal(root.getAttribute('data-palette'), 'forest');
  assert.equal(root.getAttribute('data-theme'), 'dark');
  const links = [...win.document.head.querySelectorAll('link[rel="stylesheet"]')].map((l) => l.getAttribute('href'));
  assert.deepEqual(links, ['/static/theme.css?v=8', '/static/ui-theme/ui-theme.css', '/static/ui-theme/adapters/dispatch-extra.css', '/apps/demo/app.css'],
    'tokens first, the app’s own sheet last (it wins ties)');
  sdk.theme();
  assert.equal(win.document.head.querySelectorAll('link[rel="stylesheet"]').length, 4, 'idempotent');

  // A palette change in the shell: it writes the storage key, we re-read.
  shell.document.documentElement.setAttribute('data-palette', 'daylight');
  shell.document.documentElement.setAttribute('data-theme', 'light');
  win.dispatchEvent(new win.StorageEvent('storage', { key: 'dispatch-palette' }));
  assert.equal(root.getAttribute('data-palette'), 'daylight');
  assert.equal(root.getAttribute('data-theme'), 'light');
});

test('theme(): standalone, it follows the stored palette and learns its ground', { skip: dom.skip }, async () => {
  const { win, routes } = setupDom(PAGE, 'http://127.0.0.1:8765/apps/demo/');
  win.localStorage.setItem('dispatch-palette', 'paper');
  routes['GET /static/ui-theme/themes.json'] = () => [200, { themes: [{ slug: 'paper', ground: 'light' }, { slug: 'glacier', ground: 'oled' }] }];
  const sdk = await freshSdk();
  sdk.theme();
  const root = win.document.documentElement;
  assert.equal(root.getAttribute('data-palette'), 'paper');
  await flush();
  assert.equal(root.getAttribute('data-theme'), 'light');
  win.localStorage.removeItem('dispatch-palette');
  win.dispatchEvent(new win.StorageEvent('storage', { key: 'dispatch-palette' }));
  assert.equal(root.getAttribute('data-palette'), 'glacier', 'the shell default');
  assert.equal(root.getAttribute('data-theme'), 'amoled');
});

// ------------------------------------------------------------------ win (tool window)

test('win.* posts the window ops, sanitised, at our own origin', { skip: dom.skip }, async () => {
  const { win } = setupDom(PAGE, 'http://127.0.0.1:8765/apps/demo/');
  const posted = [];
  frameUnder(win, { postMessage: (m, o) => posted.push([m, o]) });
  const sdk = await freshSdk();
  sdk.win.setStatus('2 making');
  sdk.win.setStatus('x'.repeat(100));
  sdk.win.setStatus(null);
  sdk.win.setBadge(3);
  sdk.win.setBadge(null);
  sdk.win.setBadge(-4);
  sdk.win.attention();
  sdk.win.setUnsaved(1);
  sdk.win.setUnsaved(false);
  sdk.win.setBusy(2.9);
  sdk.win.setBusy('junk');
  sdk.win.setBusy(-1);
  sdk.win.minimize();
  const T = 'dispatch:window';
  assert.deepEqual(posted.map(([m]) => m), [
    { op: 'status', value: '2 making', type: T },
    { op: 'status', value: 'x'.repeat(40), type: T },
    { op: 'status', value: '', type: T },
    { op: 'badge', value: 3, type: T },
    { op: 'badge', value: null, type: T },
    { op: 'badge', value: 0, type: T },
    { op: 'attention', value: true, type: T },
    { op: 'unsaved', value: true, type: T },
    { op: 'unsaved', value: false, type: T },
    { op: 'busy', value: 2, type: T },
    { op: 'busy', value: 0, type: T },
    { op: 'busy', value: 0, type: T },
    { op: 'minimize', value: true, type: T },
  ]);
  assert.ok(posted.every(([, o]) => o === 'http://127.0.0.1:8765'));
});

test('win.onState accepts only the framing shell, same origin, with a valid shape', { skip: dom.skip }, async () => {
  const { win } = setupDom(PAGE, 'http://127.0.0.1:8765/apps/demo/');
  frameUnder(win, { postMessage() {} });
  const sdk = await freshSdk();
  const seen = [];
  const off = sdk.win.onState((s) => seen.push(s));
  assert.equal(sdk.win.state(), null);
  const good = { type: 'dispatch:window-state', state: 'min', layout: 'right' };
  shellMessage(win, good, { origin: 'http://evil.example' });
  shellMessage(win, good, { source: {} });
  shellMessage(win, { ...good, state: 'closed' });
  shellMessage(win, { ...good, layout: 'weird' });
  shellMessage(win, { ...good, type: 'dispatch:other' });
  shellMessage(win, 'junk');
  assert.deepEqual(seen, []);
  assert.equal(sdk.win.state(), null);
  shellMessage(win, good);
  assert.deepEqual(seen, [{ state: 'min', layout: 'right' }]);
  assert.deepEqual(sdk.win.state(), { state: 'min', layout: 'right' });
  off();
  shellMessage(win, { ...good, state: 'open' });
  assert.equal(seen.length, 1);
  assert.equal(sdk.win.state().state, 'open');
});

test('win.* is a quiet no-op when not framed', { skip: dom.skip }, async () => {
  setupDom(PAGE, 'http://127.0.0.1:8765/apps/demo/');
  const sdk = await freshSdk();
  assert.equal(sdk.win.setStatus('x'), false);
  assert.equal(sdk.win.setBusy(1), false);
  assert.equal(sdk.win.attention(), false);
  assert.equal(sdk.win.minimize(), false);
  assert.equal(sdk.win.state(), null);
  assert.equal(typeof sdk.win.onState(() => {}), 'function');
});
