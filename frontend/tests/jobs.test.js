// Jobs board mount / unmount contract.
//
// Two regressions caught on 2026-09-15 after the v3 jobs board shipped:
//   1. Mobile shows no jobs when the user opens the board via the sidebar.
//   2. The toolbar stays painted across view changes (`unmountJobs` not
//      effective on every transition path).
// This file pins the new contract: the board lives in a dedicated sibling
// (#job-board-host — see index.html), and `unmountJobs()` always removes the
// [data-jobs-root] the mount created.
//
// What we DO NOT pin here: view-switch orchestration in main.js, CSS
// layout, and the click handlers on a rendered row. Those are integration
// territory — covered by the dispatch-e2e smoke tests on staging (:8766).

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const STATIC = join(HERE, '..', 'static');

const require = createRequire(import.meta.url);
let jsdom = null;
try { jsdom = require('jsdom'); } catch { /* not installed — tests skip */ }

const dom = { skip: jsdom ? false : 'jsdom is not installed (see markdown-behaviour.test.js)' };

let _win = null;
async function withDom() {
  if (_win) return _win;
  const { JSDOM } = jsdom;
  // Mirror the real index.html host shape: an #app containing the new
  // #job-board-host SIBLING (not child) of #chatview. The brief specifies
  // this slot; if a refactor inlines it back into #chatview, these tests
  // should fail loudly.
  const html = `<!doctype html><html><body>
    <div id="app" class="app" data-view="bots">
      <section class="panel chatview" id="chatview">
        <div class="messages" id="messages"></div>
      </section>
      <section class="panel jobboard-host" id="job-board-host" hidden></section>
    </div>
  </body></html>`;
  const win = new JSDOM(html, { url: 'http://127.0.0.1:8765/', runScripts: 'outside-only' }).window;
  // Stub the network: jobs.js calls api.jobs.list() which goes through
  // fetch(). A controlled response lets the mount path run end-to-end
  // without 404s, and the test can vary the payload per case.
  globalThis.fetch = async () => ({
    ok: true, status: 200, headers: new win.Headers(),
    json: async () => ({ jobs: [
      { thread_id: 't-1', title: 'Senior Eng @ Vercel NYC', company: 'Vercel',
        location: 'NYC', remote_type: 'hybrid', tags: ['react'],
        effective_state: 'pending', updated_at: new Date().toISOString(),
        created_at: new Date().toISOString() },
    ], next_cursor: null }),
  });
  globalThis.window = win;
  globalThis.document = win.document;
  _win = win;
  return win;
}

// Stub the two ESM deps jobs.js imports. We do not want a real i18n
// init() (it fetches a locale) or a real api.js (it sets up locked-state
// hooks). Both have the surface jobs.js touches: `t(key)` and
// `api.jobs.list(params)`.
const I18N_STUB = {
  t: (key, vars) => {
    if (vars && 'n' in vars) return `${key}{n=${vars.n}}`;
    if (vars && 'msg' in vars) return `${key}{msg=${vars.msg}}`;
    return key;
  },
};
const API_STUB = {
  jobs: { list: () => Promise.resolve({ jobs: [], next_cursor: null }) },
  setOnLocked: () => {},
};

// We re-import jobs.js per test so its module-level state (sort, filter,
// `state.jobs`) does not bleed across cases. Each test owns its own import.
async function freshJobs() {
  return await import(`../static/js/jobs.js?v=${Math.random()}`);
}

test('mountJobs creates a [data-jobs-root] inside the host, not in #chatview', { skip: dom.skip }, async () => {
  const win = await withDom();
  // Patch the imports jobs.js will resolve to. We cannot easily intercept
  // module specifiers inside a jsdom VM, so instead we inject globals
  // matching the imports' top-level names.
  win.t = I18N_STUB.t;
  win.api = API_STUB;

  // The host is hidden by default — mirror that, so we can prove the
  // test (not main.js) is responsible for showing it.
  assert.equal(win.document.getElementById('job-board-host').hidden, true);

  const { mountJobs } = await freshJobs();
  const host = win.document.getElementById('job-board-host');
  await mountJobs(host);

  const root = win.document.querySelector('[data-jobs-root]');
  assert.ok(root, 'mountJobs must create a [data-jobs-root]');
  assert.equal(root.parentElement, host,
    'the [data-jobs-root] must live inside #job-board-host (sibling of #chatview), not inside #messages / #chatview');
  // Toolbar paint check: at minimum a .jobs-toolbar exists.
  assert.ok(root.querySelector('.jobs-toolbar'),
    'a freshly-mounted board must render the toolbar');
  // The chat view must NOT carry the root — that was the original leak.
  assert.equal(win.document.querySelector('#chatview [data-jobs-root]'), null,
    '#chatview must not contain a [data-jobs-root] (jobs(unmount) regression guard)');
});

test('unmountJobs removes the [data-jobs-root] (the toolbar cannot leak)', { skip: dom.skip }, async () => {
  const win = await withDom();
  win.t = I18N_STUB.t;
  win.api = API_STUB;
  const { mountJobs, unmountJobs } = await freshJobs();
  const host = win.document.getElementById('job-board-host');
  await mountJobs(host);
  assert.ok(win.document.querySelector('[data-jobs-root]'));

  unmountJobs();
  assert.equal(win.document.querySelector('[data-jobs-root]'), null,
    'after unmountJobs, [data-jobs-root] is gone — the toolbar cannot stay painted');
  // The host is left intact; main.js toggles its `hidden` attribute.
  assert.ok(host,
    'unmountJobs removes the inner root, not the host slot — main.js hides the slot');
});

test('unmountJobs is idempotent — calling it twice does not throw', { skip: dom.skip }, async () => {
  const win = await withDom();
  win.t = I18N_STUB.t;
  win.api = API_STUB;
  const { mountJobs, unmountJobs } = await freshJobs();
  await mountJobs(win.document.getElementById('job-board-host'));
  unmountJobs();
  unmountJobs();   // must be a no-op
  assert.equal(win.document.querySelector('[data-jobs-root]'), null);
});

test('mountJobs reuses an existing [data-jobs-root] instead of stacking', { skip: dom.skip }, async () => {
  const win = await withDom();
  win.t = I18N_STUB.t;
  win.api = API_STUB;
  const { mountJobs, unmountJobs } = await freshJobs();
  const host = win.document.getElementById('job-board-host');
  await mountJobs(host);
  await mountJobs(host);   // second mount, no prior unmount
  const roots = host.querySelectorAll('[data-jobs-root]');
  assert.equal(roots.length, 1,
    'mountJobs must not create a second [data-jobs-root] if one already exists');
  unmountJobs();
});

test('the index.html in this tree carries the dedicated #job-board-host slot', () => {
  // The brief is explicit: a sibling of #chatview inside #app, NOT a child.
  // If a future refactor drops this slot, the regressions come back.
  const html = readFileSync(join(STATIC, 'index.html'), 'utf8');
  assert.match(html, /id="job-board-host"/,
    'index.html must carry the dedicated #job-board-host slot (jobs(mobile))');
  // Belt and braces: it must not be nested inside #chatview.
  const chatviewMatch = html.match(/<section[^>]*id="chatview"[\s\S]*?<\/section>/);
  assert.ok(chatviewMatch, 'chatview section missing');
  assert.equal(chatviewMatch[0].includes('id="job-board-host"'), false,
    '#job-board-host must be a SIBLING of #chatview, not a child (jobs(unmount) regression guard)');
  // The mobile Jobs tab exists and is hidden by default.
  assert.match(html, /id="tab-jobs"/,
    'mobile Jobs tab must exist (jobs(mobile))');
  assert.match(html, /id="tab-jobs"[^>]*\bhidden\b/,
    'mobile Jobs tab must start hidden and be revealed once state.bots includes a jobboard bot');
});

test('the sw.js CACHE was bumped for the new module paths', () => {
  const sw = readFileSync(join(STATIC, 'sw.js'), 'utf8');
  const m = /const CACHE = '([^']+)'/.exec(sw);
  assert.ok(m, 'sw.js must declare CACHE');
  // Pin against the shipped CACHE name (v96). If you bump again, bump here too.
  assert.equal(m[1], 'local-chat-v96',
    'sw.js CACHE must be local-chat-v96 so old shells drop and the new modules install');
});
