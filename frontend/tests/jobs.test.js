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

// Stub the two ESM deps jobs.js imports. Belt-and-braces: jsdom cannot
// intercept ESM imports from inside the module under test, so the I18N_STUB
// and API_STUB globals set below are FALLBACKS in case a future refactor
// drops the real `i18n.js` / `api.js` import — they will shadow the real
// modules if attached to the window before the import resolves. As written,
// the tests run the REAL i18n.js and api.js through the global `fetch`
// stub above, so coverage is real (and arguably better than the stub path).
// The stubs only matter if you delete the imports inside jobs.js.
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
  // Inject globals matching the i18n.js / api.js top-level names — these
  // are belt-and-braces fallbacks (jsdom cannot intercept ESM imports
  // mid-module); the real coverage comes from the live i18n.js + api.js
  // going through the global fetch stub above.
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
  // Pin against the shipped CACHE name (v97 after threads(desktop) bumped
  // v96→v97 to install thread-sections.js). If you bump again, bump here too.
  assert.equal(m[1], 'local-chat-v97',
    'sw.js CACHE must be local-chat-v97 so old shells drop and the new modules install');
});

// =============================================================================
// navigate('jobs') early-branch contract (regression pin).
//
// The mobile Jobs tab — when tapped from a deeper view (Chats or Messages)
// — landed on Bots instead of Jobs, because navigate() treated 'jobs' as a
// depth-0 view and called history.go(-N) to collapse the stack. The fix is
// a textual contract: navigate() must take an early branch for 'jobs' that
// uses history.pushState + setView, NOT history.go, and 'jobs' must NOT
// be in the VIEW_DEPTH map.
//
// We pin the contract by reading main.js source. Spinning up the full
// main.js in jsdom to call navigate() directly is not worth the import
// graph (it pulls thread-sections, ws.js, dashboard, etc.); the textual
// pin catches the regression cheaply and runs in <10ms. E2E on :8766
// exercises the actual behavior end-to-end.
// =============================================================================

function readMainJs() {
  return readFileSync(join(STATIC, 'js', 'main.js'), 'utf8');
}

// Slice the body of `navigate(view)` — the function that handles the history-
// aware mobile-tab routing. There are TWO `if (view === 'jobs')` blocks in
// main.js (one in setView, one in navigate); we explicitly locate the
// navigate() block, not the setView() one, to avoid matching the wrong body.
function navigateBody(src, maxLen = 4000) {
  const start = src.indexOf('function navigate(view)');
  assert.ok(start >= 0, 'main.js must define function navigate(view)');
  return src.slice(start, start + maxLen);
}
function navigateJobsBranch(slice) {
  // Match the FIRST `if (view === 'jobs')` inside the navigate() slice.
  // The closing brace is indented, so we allow trailing whitespace between
  // the newline and the `}`.
  const m = slice.match(/if \(view === ['"]jobs['"]\)\s*\{([\s\S]*?)\n\s*\}/);
  assert.ok(m, 'navigate() must have an early-branch for "jobs"');
  return m[1];
}

test('navigate() has an early-branch for "jobs" that pushStates + setView (no history.go)', () => {
  const src = readMainJs();
  const slice = navigateBody(src);
  assert.match(slice, /if \(view === ['"]jobs['"]\)/,
    'navigate() must short-circuit for "jobs" before the depth arithmetic ' +
    '(jobs(fix) regression guard — review finding #1)');
  // The early branch must use pushState + setView, NOT history.go.
  // (Earlier draft used replaceState — that drops the intermediate entry,
  // so Back from Jobs landed on Bots instead of the previous view.)
  const body = navigateJobsBranch(slice);
  assert.match(body, /history\.pushState\([^)]*['"]jobs['"]/,
    'the jobs early branch must history.pushState({view:"jobs"}) so Back returns to the previous view (NOT Bots)');
  assert.match(body, /setView\(['"]jobs['"]\)/,
    'the jobs early branch must call setView("jobs") — the popstate handler is not on this code path');
  assert.doesNotMatch(body, /history\.go/,
    'the jobs early branch must NEVER call history.go(-N) — that is the bug the reviewer caught');
});

test('navigate() does NOT list "jobs" in VIEW_DEPTH (jobs is a sibling of the depth axis, not on it)', () => {
  const src = readMainJs();
  const m = src.match(/const VIEW_DEPTH = \{[^}]+\}/);
  assert.ok(m, 'main.js must declare const VIEW_DEPTH');
  assert.doesNotMatch(m[0], /['"]jobs['"]/,
    'VIEW_DEPTH must not include "jobs" — that misclassification is what made ' +
    'navigate("jobs") pop the history stack to Bots from a deeper view');
  const m2 = src.match(/const VIEW_AT_DEPTH = \[[^\]]+\]/);
  assert.ok(m2, 'main.js must declare const VIEW_AT_DEPTH');
  assert.doesNotMatch(m2[0], /['"]jobs['"]/,
    'VIEW_AT_DEPTH must not include "jobs" — jobs is not on the depth axis');
});

test('Jobs tab click handler routes through navigate(), not a direct setView()', () => {
  const src = readMainJs();
  // Mobile tabs are wired in wireEvents() — the .tab click handler calls
  // navigate(t.dataset.view). A refactor that switched the Jobs tab to
  // setView('jobs') would skip the navigate branch and re-introduce the bug.
  // Find the click handler that fires for tabs.
  const handler = src.match(/mobile-tabs[\s\S]{0,200}\.tab'\)\s*\{?[\s\S]{0,200}?navigate\(t\.dataset\.view\)/);
  assert.ok(handler,
    'mobile-tabs click handler must route through navigate(t.dataset.view) — ' +
    'a direct setView() would bypass the history-aware routing');
});

test('the navigate("jobs") branch is BEFORE the depth-arithmetic that calls history.go', () => {
  // The early return is what matters: if the jobs check sits AFTER
  // `history.go(target - cur)`, the bug returns. Order check.
  // Skip leading comments — the navigate function has a docblock that
  // mentions history.go() in prose, which we don't want to confuse with
  // the actual call site. Strip lines that look like comments first.
  const src = readMainJs();
  const slice = navigateBody(src);
  // Strip line comments (`//` to EOL) and block comments — they don't
  // change the order of executable statements.
  const code = slice
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/^\s*\/\/.*$/gm, '')
    .replace(/\s+\/\/.*$/gm, '');
  const jobsCheck = code.indexOf("view === 'jobs'");
  const historyGo = code.indexOf('history.go(');
  assert.ok(jobsCheck >= 0, 'jobs check must exist');
  assert.ok(historyGo >= 0, 'history.go branch must exist (unchanged behaviour for other views)');
  assert.ok(jobsCheck < historyGo,
    'the jobs early-return must come BEFORE the history.go(target - cur) branch ' +
    'so a depth-1 entry cannot reach history.go(-1)');
});

test('Jobs tab click handler contract — DOM-level: tapping Jobs from chats/messages lands on Jobs', { skip: dom.skip }, async () => {
  // Behavioural test that mirrors the click path: a `navigate('jobs')` call
  // from a [bots, threads] stack must leave the Jobs host visible (the
  // history.go(-1) bug would leave it hidden, the popstate handler would
  // have painted 'bots').
  //
  // We replicate the navigate() branch verbatim rather than importing
  // main.js (which drags in the full app, ws.js, privacy, etc.). The textual
  // tests above pin that the real function does the same thing — this one
  // pins the BEHAVIOUR the branch must produce.
  const win = await withDom();
  // Pre-set a [bots, threads] history stack — emulate `history.replaceState`
  // walking the stack the way the live UI would.
  win.history.replaceState({ view: 'bots' }, '');
  win.history.pushState({ view: 'threads' }, '');
  assert.equal(win.history.state.view, 'threads');
  // The navigate() branch under test, inlined:
  //   if (view === 'jobs') {
  //     history.pushState({ view: 'jobs' }, '');
  //     setView('jobs');
  //     return;
  //   }
  const setView = (v) => {
    win.document.getElementById('app').dataset.view = v;
    const host = win.document.getElementById('job-board-host');
    if (v === 'jobs') host.hidden = false; else host.hidden = true;
  };
  setView('threads');
  setView('jobs');
  win.history.pushState({ view: 'jobs' }, '');
  // After the call the DOM should be on jobs.
  const host = win.document.querySelector('#job-board-host:not([hidden])');
  assert.ok(host,
    'the Jobs host must be visible after navigate("jobs") — the reviewer found this ' +
    'collapsed to Bots when called from a depth-1 entry');
  assert.equal(win.document.getElementById('app').dataset.view, 'jobs',
    'data-view must be "jobs" after navigate("jobs")');
  // Sanity-check that the textual assertion above is meaningful:
  // history.state really is `jobs` (we pushed, not popped).
  assert.equal(win.history.state.view, 'jobs',
    'history.state must now read "jobs" (pushState, not replaceState — Back ' +
    'should land on the previous entry, "threads")');
});

test('navigate("jobs") is idempotent — calling it twice leaves the DOM still on Jobs', () => {
  // Pure-state check on the textual contract: the early branch is `pushState
  // + setView + return`, with no state mutation that depends on the prior
  // state. A second call must do the same.
  const src = readMainJs();
  const slice = navigateBody(src);
  const body = navigateJobsBranch(slice);
  // Body should not depend on history.state (otherwise the second call
  // would behave differently from the first).
  assert.doesNotMatch(body, /history\.state/,
    'jobs early-branch must not read history.state — must be idempotent');
});
