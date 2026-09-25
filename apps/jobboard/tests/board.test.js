// Job Board app — the board page (apps/jobboard/static/board.js).
//
// Moved from frontend/tests/jobs.test.js when the board became an app
// (docs/design/2026-09-25-apps.md). The pins that survived the move, and why
// they still matter:
//   * the board mounts into its own root and unmount leaves nothing behind;
//   * filters and sort are local — no request per control change;
//   * the toolbar is built once — the control you touched survives;
//   * a live job_updated frame patches one row and fetches nothing;
//   * a refresh never blanks the rows, a failure is a banner above them.
// New with the app: a job opens IN ITS THREAD through the SDK's openThread
// (a same-origin postMessage to the shell), Safe Mode shows a locked note
// instead of an empty board, and the page talks to /api/apps/jobboard.

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { dom, setupDom, flush, appDir } from '../../../frontend/tests/helpers/app-test-env.js';

const APP = appDir('jobboard');
const EN = JSON.parse(readFileSync(join(APP, 'locales', 'en.json'), 'utf8'));
const PAGE_URL = 'http://127.0.0.1:8765/apps/jobboard/';
const PAGE = '<!doctype html><html><head></head><body><main id="jb-root"></main></body></html>';

function job(over = {}) {
  const now = new Date().toISOString();
  return {
    job_id: 'j-1', thread_id: 't-2026-09', title: 'Senior Eng', company: 'Vercel', location: 'NYC',
    remote_type: 'hybrid', tags: ['react'], state: 'pending', effective_state: 'pending',
    created_at: now, updated_at: now, posted_at: now, ...over,
  };
}

const JOBS = [
  job(),
  job({ job_id: 'j-2', title: 'Backend', company: 'Stripe', remote_type: 'remote', tags: ['go'], effective_state: 'yes', updated_at: '2026-09-01T00:00:00Z' }),
  job({ job_id: 'j-3', title: 'Old one', company: 'Acme', created_at: '2026-08-10T00:00:00Z', updated_at: '2026-08-10T00:00:00Z', thread_id: 't-2026-08' }),
];

/** A board page, framed (window.parent is a fake shell recording posts).
 *  The SDK is ONE module instance for the whole file (board.js imports it by
 *  a fixed URL), so its listeners are re-armed on each new window. */
async function page({ jobs = JOBS, routes: extra = {} } = {}) {
  const posted = [];
  const env = setupDom(PAGE, PAGE_URL, {
    'GET /apps/jobboard/locales/en.json': () => [200, EN],
    'GET /api/apps/jobboard': () => [200, { jobs, next_cursor: null }],
    'GET /api/apps/jobboard/months?bot_id=jobboard': () => [200, { months: [], current: null }],
    ...extra,
  });
  env.win.localStorage.setItem('dispatch-lang', 'en');
  Object.defineProperty(env.win, 'parent', { configurable: true, get: () => parentWin });
  const parentWin = { postMessage: (m, o) => posted.push([m, o]) };
  const sdk = await import('/static/js/app-sdk.js?v=1');
  sdk._resetForTest();
  return { ...env, posted, parentWin, sdk };
}

async function freshBoard() {
  // board.js is loaded by its SERVED path; the SDK it imports is shared.
  return await import(`/apps/jobboard/board.js?t=${Math.random()}`);
}

async function mounted(opts) {
  const env = await page(opts);
  await env.sdk.ready('en');
  const mod = await freshBoard();
  const root = env.win.document.getElementById('jb-root');
  await mod.mountBoard(root);
  return { ...env, mod, root };
}

function shellFrame(env, frame) {
  const ev = new env.win.MessageEvent('message', { data: { type: 'dispatch:frame', frame }, origin: env.win.location.origin });
  Object.defineProperty(ev, 'source', { value: env.parentWin });
  env.win.dispatchEvent(ev);
}

const apiCalls = (calls) => calls.filter((c) => c.url.startsWith('/api/'));

test('the board mounts into its own root: toolbar, then months newest first', { skip: dom.skip }, async () => {
  const { root, calls } = await mounted();
  assert.ok(root.querySelector('.jobs-toolbar'), 'toolbar');
  const months = [...root.querySelectorAll('.jobs-month')].map((s) => s.dataset.monthKey);
  assert.equal(months.length, 2);
  assert.ok(months[0] > months[1], 'newest month first');
  assert.equal(root.querySelectorAll('.job-row').length, 3);
  const urls = apiCalls(calls).map((c) => c.url).sort();
  assert.deepEqual(urls, ['/api/apps/jobboard', '/api/apps/jobboard/months?bot_id=jobboard'],
    'the app path, not the legacy /api/jobs');
  assert.ok(apiCalls(calls).every((c) => c.opts.credentials === 'same-origin'));
  assert.equal(root.querySelector('.job-row .job-chip-state').textContent, EN['state.pending']);
});

test('unmount leaves nothing behind and is idempotent; a remount does not stack', { skip: dom.skip }, async () => {
  const { root, mod } = await mounted();
  mod.unmountBoard();
  assert.equal(root.children.length, 0);
  mod.unmountBoard();
  await mod.mountBoard(root);
  await mod.mountBoard(root);
  assert.equal(root.querySelectorAll('.jobs-toolbar').length, 1);
});

test('filters and sort are local: no request, and the toolbar survives', { skip: dom.skip }, async () => {
  const { root, calls, win } = await mounted();
  const before = apiCalls(calls).length;
  const toolbar = root.querySelector('.jobs-toolbar');
  const stateSel = root.querySelector('.jobs-toolbar__state');
  stateSel.value = 'yes';
  stateSel.dispatchEvent(new win.Event('change'));
  assert.deepEqual([...root.querySelectorAll('.job-row')].map((r) => r.dataset.jobId), ['j-2']);
  const sortSel = root.querySelector('.jobs-toolbar__sort');
  sortSel.value = 'salary';
  sortSel.dispatchEvent(new win.Event('change'));
  stateSel.value = '';
  stateSel.dispatchEvent(new win.Event('change'));
  const remote = root.querySelector('.jobs-toolbar__remote');
  remote.value = 'remote';
  remote.dispatchEvent(new win.Event('change'));
  assert.deepEqual([...root.querySelectorAll('.job-row')].map((r) => r.dataset.jobId), ['j-2']);
  assert.equal(apiCalls(calls).length, before, 'not one request for a filter or a sort');
  assert.equal(root.querySelector('.jobs-toolbar'), toolbar, 'the toolbar is the same node');
  assert.equal(root.querySelector('.jobs-toolbar__state'), stateSel, 'the control you touched survives');
});

test('the tag filter narrows the rows without refetching', { skip: dom.skip }, async () => {
  const { root, calls, win } = await mounted();
  const before = apiCalls(calls).length;
  const tag = root.querySelector('.jobs-toolbar__tag');
  tag.value = 'Go';
  tag.dispatchEvent(new win.Event('input'));
  await new Promise((r) => setTimeout(r, 150));
  assert.deepEqual([...root.querySelectorAll('.job-row')].map((r) => r.dataset.jobId), ['j-2']);
  assert.equal(apiCalls(calls).length, before);
});

test('a job opens in its thread: the SDK asks the shell, with the job as the hook’s hint', { skip: dom.skip }, async () => {
  const { root, posted, win } = await mounted();
  root.querySelector('.job-row[data-job-id="j-1"]').click();
  assert.deepEqual(posted, [[{ threadId: 't-2026-09', hint: { job_id: 'j-1' }, type: 'dispatch:open-thread' }, win.location.origin]]);
  // Keyboard too.
  posted.length = 0;
  root.querySelector('.job-row[data-job-id="j-3"]').dispatchEvent(new win.KeyboardEvent('keydown', { key: 'Enter' }));
  assert.equal(posted[0][0].threadId, 't-2026-08');
});

test('a job_updated frame patches one row and fetches nothing', { skip: dom.skip }, async () => {
  const env = await mounted();
  const before = apiCalls(env.calls).length;
  const untouched = env.root.querySelector('.job-row[data-job-id="j-2"]');
  shellFrame(env, { type: 'app:jobboard:job_updated', job: { ...JOBS[0], title: 'Senior Eng (updated)' } });
  await flush();
  assert.equal(env.root.querySelector('.job-row[data-job-id="j-1"] .job-row__title').textContent, 'Senior Eng (updated)');
  assert.equal(env.root.querySelector('.job-row[data-job-id="j-2"]'), untouched, 'the other rows are not rebuilt');
  assert.equal(apiCalls(env.calls).length, before);
});

test('a vote that reorders the board still makes no request', { skip: dom.skip }, async () => {
  const env = await mounted();
  const before = apiCalls(env.calls).length;
  shellFrame(env, { type: 'app:jobboard:job_updated', job: { ...JOBS[1], updated_at: new Date(Date.now() + 1000).toISOString(), effective_state: 'no' } });
  await flush();
  assert.equal(env.root.querySelector('.job-row').dataset.jobId, 'j-2', 'moved to the top');
  assert.equal(apiCalls(env.calls).length, before);
});

test('a job the board has never seen (job_created) falls back to a refresh', { skip: dom.skip }, async () => {
  const env = await mounted();
  const before = apiCalls(env.calls).length;
  shellFrame(env, { type: 'app:jobboard:job_created', job: job({ job_id: 'j-new' }) });
  await flush();
  assert.ok(apiCalls(env.calls).length > before);
});

test('frames from anyone but the shell are ignored', { skip: dom.skip }, async () => {
  const env = await mounted();
  const ev = new env.win.MessageEvent('message', { data: { type: 'dispatch:frame', frame: { type: 'app:jobboard:job_updated', job: { ...JOBS[0], title: 'HIJACK' } } }, origin: env.win.location.origin });
  Object.defineProperty(ev, 'source', { value: env.win });
  env.win.dispatchEvent(ev);
  await flush();
  assert.notEqual(env.root.querySelector('.job-row[data-job-id="j-1"] .job-row__title').textContent, 'HIJACK');
});

test('a failed refresh keeps the rows and shows the error beside them', { skip: dom.skip }, async () => {
  const env = await mounted();
  env.routes['GET /api/apps/jobboard'] = () => [500, { detail: 'db locked' }];
  await env.mod.refresh();
  assert.equal(env.root.querySelectorAll('.job-row').length, 3, 'the rows stay');
  const banner = env.root.querySelector('[role="alert"]');
  assert.ok(banner && banner.textContent.includes('db locked'));
});

test('the list is not blanked while a later refresh runs', { skip: dom.skip }, async () => {
  const env = await mounted();
  let release;
  env.routes['GET /api/apps/jobboard'] = () => new Promise((r) => { release = () => r([200, { jobs: JOBS }]); });
  const p = env.mod.refresh();
  await flush();
  assert.equal(env.root.querySelectorAll('.job-row').length, 3);
  assert.equal(env.root.querySelector('.jobs-toolbar__busy').hidden, false, 'the toolbar spinner is the only sign');
  release();
  await p;
  assert.equal(env.root.querySelector('.jobs-toolbar__busy').hidden, true);
});

test('Safe Mode: the decoy 403 shows a locked note, never an empty board', { skip: dom.skip }, async () => {
  const env = await page({ routes: { 'GET /api/apps/jobboard': () => [403, { detail: 'Unlock for full access', decoy: true }] } });
  await env.sdk.ready('en');
  const mod = await freshBoard();
  const root = env.win.document.getElementById('jb-root');
  await mod.mountBoard(root);
  assert.equal(root.querySelector('.jb-locked').textContent, EN.locked);
  assert.equal(root.querySelector('.job-row'), null);
});

test('Find jobs posts the query to /find for the board’s bot', { skip: dom.skip }, async () => {
  const env = await mounted({ routes: { 'POST /api/apps/jobboard/find?bot_id=jobboard': () => [200, { ok: true, dispatched: true }] } });
  env.root.querySelector('.jobs-find-btn').click();
  const form = env.root.querySelector('.jobs-find');
  form.querySelector('.jobs-find__input').value = 'rust, remote';
  form.dispatchEvent(new env.win.Event('submit', { cancelable: true }));
  await flush();
  const call = env.calls.find((c) => c.method === 'POST');
  assert.equal(call.url, '/api/apps/jobboard/find?bot_id=jobboard');
  assert.deepEqual(call.body, { query: 'rust, remote' });
  assert.equal(env.root.querySelector('.jobs-find__status[role="status"]').textContent, EN['find.sent']);
});

test('boot: on the real page it themes itself, loads its strings and shows when ready', { skip: dom.skip }, async () => {
  const env = await page();
  env.win.document.body.classList.add('jb-page');
  env.routes['GET /static/ui-theme/themes.json'] = () => [200, { themes: [] }];
  await freshBoard();
  await flush(20);
  const doc = env.win.document;
  assert.ok(doc.body.classList.contains('jb-ready'));
  assert.equal(doc.title, EN.title);
  assert.ok(doc.querySelector('link[href="/static/ui-theme/ui-theme.css"]'), 'the theme sheets are linked');
  assert.ok(doc.querySelectorAll('.job-row').length > 0);
});
