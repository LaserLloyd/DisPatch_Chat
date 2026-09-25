// Jobs board mount / unmount contract.
//
// Two regressions caught on 2026-09-15 after the v3 jobs board shipped:
//   1. Mobile shows no jobs when the user opens the board via the sidebar.
//   2. The toolbar stays painted across view changes (`unmountJobs` not
//      effective on every transition path).
// This file pins the contract: the board lives in its own host
// (#job-board-host — the body of the Job Board tool pane since 2026-09-25,
// see index.html), and `unmountJobs()` always removes the [data-jobs-root]
// the mount created.
//
// 2026-09-25: the Job Board became a builtin TOOL. The `data-view="jobs"`
// screen, its navigate()/setView() branches, the selectBot() exit guard and
// the mobile Jobs tab are gone; the pins for them are replaced by pins on the
// tool pane (openJobsView / closeJobsView) further down.

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { domSkip } from './_require-dom.js';

const HERE = dirname(fileURLToPath(import.meta.url));
const STATIC = join(HERE, '..', 'static');

const require = createRequire(import.meta.url);
let jsdom = null;
try { jsdom = require('jsdom'); } catch { /* not installed — tests skip */ }

const dom = { skip: domSkip(jsdom ? false : 'jsdom is not installed (see markdown-behaviour.test.js)') };

let _win = null;
async function withDom() {
  if (_win) return _win;
  const { JSDOM } = jsdom;
  // Mirror the real index.html host shape: the Job Board tool pane
  // (#jobs-view, a terminal-view inside #chatview, hidden while closed) with
  // #job-board-host as its body — never inside #messages.
  const html = `<!doctype html><html><body>
    <div id="app" class="app" data-view="bots">
      <section class="panel chatview" id="chatview">
        <div class="messages" id="messages"></div>
        <div class="terminal-view jobs-view hidden" id="jobs-view">
          <div class="terminal-body jobs-body">
            <section class="jobboard-host" id="job-board-host"></section>
          </div>
        </div>
      </section>
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

test('mountJobs creates a [data-jobs-root] inside the host, not in #messages', { skip: dom.skip }, async () => {
  const win = await withDom();
  // Inject globals matching the i18n.js / api.js top-level names — these
  // are belt-and-braces fallbacks (jsdom cannot intercept ESM imports
  // mid-module); the real coverage comes from the live i18n.js + api.js
  // going through the global fetch stub above.
  win.t = I18N_STUB.t;
  win.api = API_STUB;

  // The pane is hidden by default — main.js's openJobsView() shows it.
  assert.ok(win.document.getElementById('jobs-view').classList.contains('hidden'));

  const { mountJobs } = await freshJobs();
  const host = win.document.getElementById('job-board-host');
  await mountJobs(host);

  const root = win.document.querySelector('[data-jobs-root]');
  assert.ok(root, 'mountJobs must create a [data-jobs-root]');
  assert.equal(root.parentElement, host,
    'the [data-jobs-root] must live inside #job-board-host (the tool pane body), not inside #messages');
  // Toolbar paint check: at minimum a .jobs-toolbar exists.
  assert.ok(root.querySelector('.jobs-toolbar'),
    'a freshly-mounted board must render the toolbar');
  // The message list must NOT carry the root — that was the original leak.
  assert.equal(win.document.querySelector('#messages [data-jobs-root]'), null,
    '#messages must not contain a [data-jobs-root] (jobs(unmount) regression guard)');
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
  // The host is left intact; main.js hides the pane around it.
  assert.ok(host,
    'unmountJobs removes the inner root, not the host slot — main.js hides the pane');
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

test('index.html: the Job Board is a tool pane inside #chatview, with no Jobs screen or tab', () => {
  const html = readFileSync(join(STATIC, 'index.html'), 'utf8');
  const pane = html.match(/<div class="terminal-view jobs-view hidden" id="jobs-view">[\s\S]*?<\/section>\s*<\/div>\s*<\/div>/);
  assert.ok(pane, 'index.html must carry the Job Board pane #jobs-view (a hidden terminal-view)');
  assert.match(pane[0], /id="job-board-host"/, '#job-board-host must be the body of #jobs-view');
  assert.match(pane[0], /id="jobs-close"/, 'the pane has a ✕ that restores the previous chat');
  assert.match(pane[0], /id="jobs-back"/, 'the pane has a ‹ for phones');
  // Same parent as every other tool pane: inside #chatview, never inside #messages.
  const cvStart = html.indexOf('id="chatview"');
  const paneAt = html.indexOf('id="jobs-view"');
  const toolView = html.indexOf('id="tool-view"');
  assert.ok(cvStart >= 0 && paneAt > cvStart && paneAt < toolView,
    '#jobs-view sits with the other tool panes inside #chatview');
  assert.doesNotMatch(html, /id="tab-jobs"/, 'the mobile Jobs tab is gone — the rail tile is the entry point');
  assert.doesNotMatch(html, /nav\.tab_jobs/, 'the Jobs tab label key is gone with the tab');
});

test('app.css: no `data-view="jobs"` screen is left', () => {
  const css = readFileSync(join(STATIC, 'app.css'), 'utf8');
  assert.doesNotMatch(css, /data-view="jobs"/,
    'the Jobs screen is gone; a leftover rule would hide #chatview (and every tool pane in it)');
});

test('the sw.js CACHE was bumped for the new module paths', () => {
  const sw = readFileSync(join(STATIC, 'sw.js'), 'utf8');
  const m = /const CACHE = '([^']+)'/.exec(sw);
  assert.ok(m, 'sw.js must declare CACHE');
  // Pin against the shipped CACHE name. v100 was the palette switch, which
  // replaced theme.css's whole token block and swapped theme.js/main.js/app.css
  // under it; v101 was the DeepSeek Harness live-sessions pane; v102 is the
  // Job Board detail-panel rewrite (modal CSS + scroll fix, single-render
  // job-thread.js, feedback-for-Scout, activity log, applied-endpoint fix),
  // which changed index.html, main.js, api.js, jobs.js, job-thread.js,
  // app.css and every locale; v103 is the tablet rail-overflow fix (app.css
  // ?v=67→68 + index.html), where a stale shell means the tablet that
  // reported the bug keeps painting the broken layout. v104 was the PIN
  // screen following the theme; v105 swapped the two big empty-state emoji
  // (and the "no threads yet"/file-server-empty/"connect an AI" glyphs) for
  // themed line-icon <svg>; v106 finished the sweep — mobile tabs, settings
  // tabs, header, composer, Bot Manager badges, lock face. v107 redrew the
  // mark itself — favicon.svg, the .ico, favicon-32/icon-192/icon-512.png and
  // index.html's inline .boot-logo — and the icons are in SHELL, cached by
  // path, so an installed client keeps the old fish without the bump; v108
  // redrew it again, because the first pass read as an ordinary fish. v109
  // added the Emails (MailForge dashboard iframe) and Clients ("WebBuilder")
  // tabs — new module js/clients.js, plus index.html/main.js/api.js/app.css/
  // en.json/ja.json changes. v110 is the mobile/contrast/markdown round:
  // the viewport gained interactive-widget=resizes-content (without a new
  // shell an INSTALLED Android PWA keeps the old meta tag, which is exactly
  // the client this fix is for), every palette gained --notify/--dot-idle,
  // markdown.js stopped superscripting whole sentences, and util.js changed
  // — so every module importing it moved from ?v=15 to ?v=16 and a warm
  // cache holding both would run two copies of RAIL_ICONS. The shell is
  // cached by PATH, so without the bump an installed client keeps the old
  // markup and the fixes simply are not there. If you bump again, bump here
  // too. v116 fixed the cold-start flash (the installed PWA reloaded itself on
  // every first reopen) and the resume path. v117 is the feature wave —
  // regenerate/edit-and-rerun/quote-reply/per-thread model/mood face/scene
  // backdrops/drafts+outbox/feedback — merged from five parallel groups under
  // ONE bump, and the reason that bump is load-bearing here: six modules had
  // their content rewritten by a later group while still carrying an earlier
  // group's ?v=, so a warm client would have run new main.js beside stale
  // copies of them. The shell is cached by PATH; without this name change an
  // installed client keeps all of it. v118 moved the themes onto the shared
  // theme package: two new vendored shell files (ui-theme.js, ui-theme.css)
  // that an offline start cannot paint without.
  // v119 (2026-09-23): the Job Board detail's double-open fix changed
  // job-thread.js, its importer main.js, and app.css.
  // v120 (2026-09-24): the theme moved into the drop-in folder static/ui-theme/,
  // so the shell's theme entries changed path.
  // v121 (2026-09-24): the calmer chat — per-message Copy + ⋯ menu, model
  // picker in the header ⋯; util.js/main.js/app.css/index.html/locales moved.
  // v122 (2026-09-24): system notices collapse (new module js/notice.js).
  // v123 (2026-09-24 review): store.js joins SHELL; main.js/store.js/modelchip.js,
  // index.html, theme.css and the locales moved.
  // v124 (2026-09-24 review, round 2): main.js/notice.js moved.
  // v125 (2026-09-24): the scene backdrop tint is off; theme background only.
  // v126 (2026-09-25): Tools — new module js/tools.js, api.js and all its
  // importers moved, index.html/app.css/main.js/locales moved.
  // v127 (2026-09-25): metadata.notice convention — notice.js/main.js/app.css
  // and index.html moved.
  // v128 (2026-09-25): Tools review round — tools.js/main.js and index.html moved.
  // v129 (2026-09-25): a builtin's Settings toggle saves the manifest switch.
  // v130 (2026-09-25): tool tiles are themed bot-shaped tiles with line icons
  // (util.js gained terminal/mail/users/chart), and the Job Board is a builtin
  // tool — index.html, app.css, main.js, tools.js, util.js and every util.js
  // importer, jobs.js, job-thread.js and the locales moved.
  assert.equal(m[1], 'local-chat-v130',
    'sw.js CACHE must be local-chat-v130 so installed clients pick up the themed tool tiles + Job Board tool');
});

// =============================================================================
// The Job Board as a builtin tool (2026-09-25).
//
// Textual pins on main.js: spinning the full app up in jsdom is not worth the
// import graph; the E2E drive on a scratch instance exercises the behaviour.
// =============================================================================

function readMainJs() {
  return readFileSync(join(STATIC, 'js', 'main.js'), 'utf8');
}
function fnBody(src, sig, maxLen = 2500) {
  const start = src.indexOf(sig);
  assert.ok(start >= 0, `main.js must define ${sig}`);
  const code = src.slice(start, start + maxLen);
  const end = code.indexOf('\n}\n');
  return end > 0 ? code.slice(0, end) : code;
}

test('no Jobs screen routing is left in main.js', () => {
  const src = readMainJs();
  assert.doesNotMatch(fnBody(src, 'function setView('), /['"]jobs['"]/, 'setView has no jobs branch');
  assert.doesNotMatch(fnBody(src, 'function navigate(view)'), /['"]jobs['"]/, 'navigate has no jobs branch');
  assert.doesNotMatch(fnBody(src, 'async function selectBot('), /dataset\.view\s*===\s*['"]jobs['"]/,
    'selectBot needs no Jobs-screen exit guard: closeToolPanes() closes the board like any tool');
  assert.doesNotMatch(fnBody(src, 'function renderSidebarInner(', 5000), /jobboard/,
    'the rail has no hard-coded jobboard click — the tool tile is the entry point');
  assert.doesNotMatch(src, /['"#]tab-jobs\b/, 'the mobile Jobs tab wiring is gone (harness-tab-jobs is the Harness pane, unrelated)');
  const m = src.match(/const VIEW_DEPTH = \{[^}]+\}/);
  assert.ok(m);
  assert.doesNotMatch(m[0], /['"]jobs['"]/);
});

test('openJobsView() opens the board full-page, like every tool', () => {
  const body = fnBody(readMainJs(), 'function openJobsView(');
  assert.match(body, /state\.decoy\s*\|\|\s*!state\.jobsEnabled/, 'never in Safe Mode, only when the feature is on');
  assert.match(body, /closeToolPanes\(['"]jobs['"]\)/, 'closes the other panes first');
  assert.match(body, /rememberPrev\(\)/, 'remembers the chat it covers, so ✕ can hand it back');
  assert.match(body, /closeSearch\(/, 'dismisses the messages-search modal (the old "search header sticks" fix)');
  assert.match(body, /classList\.add\(['"]tool-full['"]\)/, 'puts body.tool-full on');
  assert.match(body, /mountJobs\(/, 'mounts the board into its host');
  assert.ok(body.indexOf('rememberPrev()') < body.indexOf('state.selectedBotId = JOBS_ID'),
    'rememberPrev runs BEFORE the selection moves to the tool');
});

test('closeJobsView() tears the board down; every other opener closes it', () => {
  const src = readMainJs();
  const body = fnBody(src, 'function closeJobsView(');
  assert.match(body, /classList\.remove\(['"]tool-full['"]\)/);
  assert.match(body, /unmountJobs\(\)/, 'unmount, not just hide: no listener or in-flight load survives');
  assert.match(fnBody(src, 'function closeToolPanes('), /jobsOpen[\s\S]*closeJobsView\(\)/,
    'closeToolPanes() closes the board, so a bot click or another tool leaves it cleanly');
  assert.match(src, /jobs:\s*openJobsView/, 'wireTools() hands tools.js the jobs opener');
  assert.match(src, /window\.__openThread = openThreadFromBoard/,
    'a thread opened from the board leaves the full-page tool first');
});

// =============================================================================
// job-thread.js — detail panel (2026-09-16 rewrite).
//
// Four regressions the rewrite fixes:
//   1. The "Applied" vote button posted signal:'applied' to /vote, which the
//      backend 422s — it must use the dedicated /applied endpoint.
//   2. A new /feedback endpoint (Feedback for Scout) had no client call at
//      all.
//   3. `link.href = data.job.url` had no scheme check (stored-XSS surface);
//      an invalid scheme must be OMITTED, never assigned to href.
//   4. Vote-button "active" state was a client-only in-memory Map; it must
//      reflect server truth (`job.last_vote`) on every render.
//
// Uses the same jsdom infra as the board tests above (shared `withDom()`
// window); each test installs its own `fetch` stub and imports job-thread.js
// fresh (cache-busted) so module-level state (`_activeOverlay` etc.) never
// bleeds across cases.
// =============================================================================

function baseJob(overrides = {}) {
  const now = new Date().toISOString();
  return {
    job_id: 'j-1', thread_id: 't-1', message_id: null,
    title: 'Senior Eng @ Vercel', company: 'Vercel', location: 'NYC',
    remote_type: 'hybrid', seniority: 'senior',
    salary_min: null, salary_max: null, salary_currency: 'USD',
    tags: ['react'], source_agent: 'scout', source_run_id: null,
    posted_at: now, first_seen: now, last_seen: now,
    brief: 'Line one\nLine two',
    url: 'https://example.com/job/1',
    state: 'pending', effective_state: 'pending', is_expired: false,
    duplicate_of: null, expires_at: null, created_at: now, updated_at: now,
    last_vote: null,
    ...overrides,
  };
}

async function freshJobThread() {
  return await import(`../static/js/job-thread.js?v=${Math.random()}`);
}

async function flush(n = 8) {
  for (let i = 0; i < n; i++) await new Promise((r) => setTimeout(r, 0));
}

// withDom() returns a SHARED window across every test in this file (see the
// module-level `_win` cache above), and each `freshJobThread()` import is a
// distinct module instance with its own `_activeOverlay` — so a previous
// test's overlay has no reachable close handle from a freshly-imported
// module and is never torn down on its own. Without this, a later test's
// querySelector('.job-modal ...') can silently match a PRIOR test's
// leftover overlay instead of the one it just opened.
function cleanOverlays(win) {
  win.document.querySelectorAll('.job-modal-overlay').forEach((n) => n.remove());
  win.document.body.classList.remove('job-modal-open');
}

// Routes a stubbed fetch() to canned responses and records every call, so
// assertions can check WHICH endpoint a click actually hit.
function installFetchRouter(win, { job, feedbackResponse } = {}) {
  const calls = [];
  globalThis.fetch = async (url, opts = {}) => {
    const method = (opts.method || 'GET').toUpperCase();
    let body = null;
    try { body = opts.body ? JSON.parse(opts.body) : null; } catch { /* not JSON */ }
    calls.push({ url: String(url), method, body });
    const ok = (json) => ({ ok: true, status: 200, headers: new win.Headers(), json: async () => json });
    // Path-only match against the specific job id (excluding the query
    // string) — a naive `/^\/api\/jobs\/[^/]+$/` also matches
    // `/api/jobs/months?bot_id=...` (no literal `/` after "months" once the
    // `?` is counted as non-slash), which would silently hand the board's
    // months() call a single-job payload instead of {months, current}.
    if (String(url).split('?')[0] === `/api/jobs/${job.job_id}` && method === 'GET') {
      return ok({ thread: { id: job.thread_id }, job, events: [], score: { score: 0.8, explanation: ['tag match'] } });
    }
    if (url.endsWith('/vote') && method === 'POST') return ok({ ok: true });
    if (url.endsWith('/applied') && method === 'POST') return ok({ ok: true });
    if (url.endsWith('/feedback') && method === 'POST') {
      if (feedbackResponse === 404) {
        return { ok: false, status: 404, headers: new win.Headers(), json: async () => ({ detail: 'Not Found' }) };
      }
      return ok(feedbackResponse || { ok: true, dispatched: true, thread_id: job.thread_id, event_id: 'e-1', message_id: 'm-1' });
    }
    return { ok: false, status: 404, headers: new win.Headers(), json: async () => ({ detail: 'unhandled route in test stub: ' + url }) };
  };
  return calls;
}

test('the "Applied" vote button calls api.jobs.applied(), never /vote (422 regression)', { skip: dom.skip }, async () => {
  const win = await withDom();
  cleanOverlays(win);
  win.toast = () => {};
  const job = baseJob();
  const calls = installFetchRouter(win, { job });
  const { openJobDetail } = await freshJobThread();
  await openJobDetail(job.job_id);
  await flush();

  const appliedBtn = win.document.querySelector('.job-modal [data-signal="applied"]');
  assert.ok(appliedBtn, 'an Applied vote button must exist');
  appliedBtn.dispatchEvent(new win.Event('click', { bubbles: true }));
  await flush();

  const appliedCalls = calls.filter((c) => c.url.endsWith('/applied'));
  const voteCallsWithApplied = calls.filter((c) => c.url.endsWith('/vote') && c.body && c.body.signal === 'applied');
  assert.equal(appliedCalls.length, 1, 'clicking Applied must POST /api/jobs/<id>/applied exactly once');
  assert.equal(voteCallsWithApplied.length, 0, "clicking Applied must NEVER POST signal:'applied' to /vote — that is the 422 the backend rejects");
});

test('vote buttons highlight from job.last_vote (server truth), not client-only memory', { skip: dom.skip }, async () => {
  const win = await withDom();
  cleanOverlays(win);
  win.toast = () => {};
  const job = baseJob({ last_vote: 'maybe' });
  installFetchRouter(win, { job });
  const { openJobDetail } = await freshJobThread();
  await openJobDetail(job.job_id);
  await flush();

  const active = win.document.querySelectorAll('.job-modal .job-vote__btn--active');
  assert.equal(active.length, 1, 'exactly one vote button reflects the active state');
  assert.equal(active[0].dataset.signal, 'maybe', 'the ACTIVE button must be the one matching job.last_vote, not a stale local guess');
});

test('vote highlight also handles the live backend\'s object shape for last_vote', { skip: dom.skip }, async () => {
  // Verified against staging 2026-09-16: the shipped backend returns
  // last_vote as {signal:'vote_yes', reason_tag, comment, actor,
  // created_at} — NOT the flat 'yes' string the original API contract
  // specified. Both shapes must render the same highlight.
  const win = await withDom();
  cleanOverlays(win);
  win.toast = () => {};
  const job = baseJob({ last_vote: { signal: 'vote_yes', reason_tag: null, comment: null, actor: 'user', created_at: new Date().toISOString() } });
  installFetchRouter(win, { job });
  const { openJobDetail } = await freshJobThread();
  await openJobDetail(job.job_id);
  await flush();

  const active = win.document.querySelectorAll('.job-modal .job-vote__btn--active');
  assert.equal(active.length, 1, 'the object-shaped last_vote must still resolve to exactly one active button');
  assert.equal(active[0].dataset.signal, 'yes', "last_vote.signal:'vote_yes' must map to the 'yes' button");
});

test('an invalid posting URL is never assigned to the link — it is omitted entirely', { skip: dom.skip }, async () => {
  const win = await withDom();
  cleanOverlays(win);
  win.toast = () => {};
  const job = baseJob({ url: 'javascript:alert(1)' });
  installFetchRouter(win, { job });
  const { openJobDetail } = await freshJobThread();
  await openJobDetail(job.job_id);
  await flush();

  const link = win.document.querySelector('.job-modal .job-card__link');
  assert.equal(link, null,
    'a non-http(s) job.url must be OMITTED from the panel, never assigned to an <a href> (stored-XSS guard)');
});

test('a valid http(s) posting URL IS rendered as a safe link', { skip: dom.skip }, async () => {
  const win = await withDom();
  cleanOverlays(win);
  win.toast = () => {};
  const job = baseJob({ url: 'https://example.com/job/1' });
  installFetchRouter(win, { job });
  const { openJobDetail } = await freshJobThread();
  await openJobDetail(job.job_id);
  await flush();

  const link = win.document.querySelector('.job-modal .job-card__link');
  assert.ok(link, 'a valid http(s) job.url must render as a link');
  assert.equal(link.getAttribute('href'), 'https://example.com/job/1');
  assert.equal(link.getAttribute('rel'), 'noopener noreferrer');
});

test('Feedback for Scout posts {comment, reason_tag} to /feedback, and shows the dispatched confirmation', { skip: dom.skip }, async () => {
  const win = await withDom();
  cleanOverlays(win);
  win.toast = () => {};
  const job = baseJob();
  const calls = installFetchRouter(win, {
    job, feedbackResponse: { ok: true, dispatched: true, thread_id: 't-1', event_id: 'e-9', message_id: 'm-9' },
  });
  const { openJobDetail } = await freshJobThread();
  await openJobDetail(job.job_id);
  await flush();

  const textarea = win.document.querySelector('.job-modal .job-feedback__input');
  const reasonSelect = win.document.querySelector('.job-modal .job-feedback__reason');
  const sendBtn = win.document.querySelector('.job-modal .job-feedback__send');
  assert.ok(textarea && reasonSelect && sendBtn, 'the feedback form must render textarea + reason select + send button');

  textarea.value = 'Please find more remote roles like this.';
  reasonSelect.value = 'wrong_location';
  sendBtn.dispatchEvent(new win.Event('click', { bubbles: true }));
  await flush();

  const fbCalls = calls.filter((c) => c.url.endsWith('/feedback'));
  assert.equal(fbCalls.length, 1, 'Send to Scout must POST /api/jobs/<id>/feedback exactly once');
  assert.equal(fbCalls[0].body.comment, 'Please find more remote roles like this.');
  assert.equal(fbCalls[0].body.reason_tag, 'wrong_location');

  // dispatched:true renders the "will reply in the thread" confirmation with
  // an Open-thread button, not the "wasn't reachable" variant.
  const confirm = win.document.querySelector('.job-modal .job-feedback__confirm');
  assert.ok(confirm, 'a confirmation must replace the form after a successful send');
  assert.ok(confirm.querySelector('.job-feedback__open-thread'), 'a dispatched:true response must offer an Open-thread button');
});

test('Feedback for Scout with dispatched:false shows the "picked up later" copy, not a failure', { skip: dom.skip }, async () => {
  const win = await withDom();
  cleanOverlays(win);
  win.toast = () => {};
  const job = baseJob();
  installFetchRouter(win, { job, feedbackResponse: { ok: true, dispatched: false, thread_id: 't-1' } });
  const { openJobDetail } = await freshJobThread();
  await openJobDetail(job.job_id);
  await flush();

  win.document.querySelector('.job-modal .job-feedback__input').value = 'note';
  win.document.querySelector('.job-modal .job-feedback__send').dispatchEvent(new win.Event('click', { bubbles: true }));
  await flush();

  const confirm = win.document.querySelector('.job-modal .job-feedback__confirm');
  assert.ok(confirm, 'dispatched:false is still a successful save, not an error state');
  assert.doesNotMatch(confirm.textContent, /could not|error/i,
    'dispatched:false must not read like a failure — the comment WAS saved');
});

test('a 404 from /feedback (endpoint not shipped yet) surfaces as a clear message, never fakes success', { skip: dom.skip }, async () => {
  const win = await withDom();
  cleanOverlays(win);
  win.toast = () => {};
  const job = baseJob();
  const calls = installFetchRouter(win, { job, feedbackResponse: 404 });
  const { openJobDetail } = await freshJobThread();
  await openJobDetail(job.job_id);
  await flush();

  win.document.querySelector('.job-modal .job-feedback__input').value = 'note';
  win.document.querySelector('.job-modal .job-feedback__send').dispatchEvent(new win.Event('click', { bubbles: true }));
  await flush();

  assert.equal(calls.filter((c) => c.url.endsWith('/feedback')).length, 1);
  assert.equal(win.document.querySelector('.job-modal .job-feedback__confirm'), null,
    'a 404 must never render the success confirmation');
  const err = win.document.querySelector('.job-modal .job-feedback__error');
  assert.ok(err && !err.hidden && err.textContent, 'a 404 must surface a visible error, not fail silently');
});

test('board list refreshes after a vote — jobs.js listens for dispatch:jobs-changed', { skip: dom.skip }, async () => {
  const win = await withDom();
  cleanOverlays(win);
  win.toast = () => {};
  const job = baseJob();
  installFetchRouter(win, { job });
  const { mountJobs, unmountJobs } = await freshJobs();
  const host = win.document.getElementById('job-board-host');
  await mountJobs(host);

  let refreshed = false;
  const origList = globalThis.fetch;
  // After mountJobs's own initial fetch, watch for a SECOND jobs-list call
  // triggered purely by the event (not by a re-mount).
  let listCalls = 0;
  globalThis.fetch = async (url, opts) => {
    if (String(url).startsWith('/api/jobs') && !/\/j-1/.test(String(url)) && !String(url).includes('/vote') && !String(url).includes('/applied') && !String(url).includes('/feedback')) {
      listCalls++;
      refreshed = true;
    }
    return origList(url, opts);
  };
  document.dispatchEvent(new win.CustomEvent('dispatch:jobs-changed'));
  await flush();
  assert.ok(refreshed && listCalls >= 1, 'jobs.js must re-fetch the board list on a dispatch:jobs-changed event');
  unmountJobs();
});

// =============================================================================
// Responsiveness contract (2026-09-20).
//
// The owner's report was "the Job Board has low responsiveness". The API was
// not the cause — measured on staging, every jobs endpoint answers in 1-22 ms
// against a fully indexed table. The cause was the render architecture:
//
//   * render() opened with root.replaceChildren() and rebuilt the toolbar, so
//     changing a <select> destroyed the <select> you had just used;
//   * every filter and sort change fired a network round-trip whose filters
//     render() then applied AGAIN, client-side, from data already in memory;
//   * refresh() set state.loading and render() early-returned a "Loading"
//     paragraph, so the whole board vanished and reappeared on every change;
//   * nothing held an AbortController, so two refreshes resolved in arrival
//     order and a slow earlier response could overwrite a newer one;
//   * a single vote re-fetched and rebuilt the entire list, on every open tab.
//
// These pin the fixes. They are behavioural — they count fetches and compare
// DOM identity — because every one of the above would still "work" under a
// test that only asserted the right rows eventually appear.
// =============================================================================

/** A window of its own, for the counting tests below.
 *
 *  withDom() memoises one window for the whole file, and several of the older
 *  tests mount a board and never unmount it. Those boards keep a
 *  `dispatch:jobs-changed` listener on the shared document — a different
 *  function object each time, since every test imports its own copy of
 *  jobs.js — so an event dispatched here would also wake them and they would
 *  each re-fetch. The counting assertions below would then be measuring the
 *  leaked listeners rather than the board under test.
 */
function freshDom() {
  const { JSDOM } = jsdom;
  const html = `<!doctype html><html><body>
    <div id="app" class="app" data-view="jobs">
      <section class="panel chatview" id="chatview"><div class="messages" id="messages"></div></section>
      <section class="panel jobboard-host" id="job-board-host"></section>
    </div>
  </body></html>`;
  const win = new JSDOM(html, { url: 'http://127.0.0.1:8765/', runScripts: 'outside-only' }).window;
  globalThis.window = win;
  globalThis.document = win.document;
  return win;
}

/** Mount a board over a counting fetch stub, and hand back the levers. */
async function mountCounting(win, jobs) {
  const calls = [];
  const rows = jobs || [
    { job_id: 'j-1', thread_id: 't-1', title: 'Alpha', company: 'A',
      remote_type: 'remote', tags: ['react'], effective_state: 'pending',
      updated_at: '2026-09-10T00:00:00Z', created_at: '2026-09-10T00:00:00Z' },
    { job_id: 'j-2', thread_id: 't-1', title: 'Beta', company: 'B',
      remote_type: 'onsite', tags: ['go'], effective_state: 'yes',
      updated_at: '2026-09-11T00:00:00Z', created_at: '2026-09-11T00:00:00Z' },
  ];
  globalThis.fetch = async (url) => {
    calls.push(String(url));
    return {
      ok: true, status: 200, headers: new win.Headers(),
      json: async () => (String(url).includes('/months')
        ? { months: [], current: null }
        : { jobs: rows, next_cursor: null }),
    };
  };
  win.t = I18N_STUB.t;
  win.api = API_STUB;
  const mod = await freshJobs();
  const host = win.document.getElementById('job-board-host');
  host.hidden = false;
  await mod.mountJobs(host);
  return { mod, calls, rows, host };
}

test('a filter change makes no network request — the data is already here', { skip: dom.skip }, async () => {
  const win = freshDom();
  const { mod, calls } = await mountCounting(win);
  const afterMount = calls.length;
  assert.ok(afterMount > 0, 'the mount itself must fetch');

  const stateSel = win.document.querySelector('.jobs-toolbar__state');
  assert.ok(stateSel, 'the toolbar must expose a state filter');
  stateSel.value = 'yes';
  stateSel.dispatchEvent(new win.Event('change'));
  await new Promise((r) => setTimeout(r, 30));

  assert.equal(calls.length, afterMount,
    `changing a filter fetched ${calls.length - afterMount} extra time(s); `
    + 'filtering is a pure operation on state.jobs');
  mod.unmountJobs();
});

test('a sort change makes no network request either', { skip: dom.skip }, async () => {
  const win = freshDom();
  const { mod, calls } = await mountCounting(win);
  const afterMount = calls.length;

  const sortSel = win.document.querySelector('.jobs-toolbar__sort');
  sortSel.value = 'salary';
  sortSel.dispatchEvent(new win.Event('change'));
  await new Promise((r) => setTimeout(r, 30));

  assert.equal(calls.length, afterMount, 'sorting must not hit the network');
  mod.unmountJobs();
});

test('the toolbar is never destroyed — the control you touched survives', { skip: dom.skip }, async () => {
  const win = freshDom();
  const { mod } = await mountCounting(win);

  const before = win.document.querySelector('.jobs-toolbar');
  const selBefore = win.document.querySelector('.jobs-toolbar__state');
  selBefore.value = 'yes';
  selBefore.dispatchEvent(new win.Event('change'));
  await new Promise((r) => setTimeout(r, 30));

  assert.equal(win.document.querySelector('.jobs-toolbar'), before,
    'the toolbar element was replaced — focus and caret position go with it');
  assert.equal(win.document.querySelector('.jobs-toolbar__state'), selBefore,
    'the <select> was replaced by its own change handler');
  assert.equal(selBefore.value, 'yes', 'and it kept the value the user chose');
  mod.unmountJobs();
});

test('a filter narrows the rendered rows without refetching', { skip: dom.skip }, async () => {
  const win = freshDom();
  const { mod } = await mountCounting(win);
  assert.equal(win.document.querySelectorAll('.job-row').length, 2);

  const stateSel = win.document.querySelector('.jobs-toolbar__state');
  stateSel.value = 'yes';
  stateSel.dispatchEvent(new win.Event('change'));
  await new Promise((r) => setTimeout(r, 30));

  const rows = win.document.querySelectorAll('.job-row');
  assert.equal(rows.length, 1, 'only the job in state "yes" should remain');
  assert.match(rows[0].textContent, /Beta/);
  mod.unmountJobs();
});

test('a job_updated payload patches one row and fetches nothing', { skip: dom.skip }, async () => {
  const win = freshDom();
  const { mod, calls, rows } = await mountCounting(win);
  const afterMount = calls.length;

  const firstRow = win.document.querySelector('[data-job-id="j-1"]');
  assert.ok(firstRow, 'the first job must be on screen');
  assert.match(firstRow.textContent, /Alpha/);

  // Exactly the shape main.js forwards from the server's job_updated frame.
  const updated = { ...rows[0], title: 'Alpha (renamed)' };
  win.document.dispatchEvent(new win.CustomEvent('dispatch:jobs-changed', {
    detail: { type: 'job_updated', job_id: 'j-1', job: updated },
  }));
  await new Promise((r) => setTimeout(r, 30));

  assert.equal(calls.length, afterMount,
    'a job_updated frame carrying the job must not trigger a re-fetch — '
    + 'the server already sent the row');
  const patched = win.document.querySelector('[data-job-id="j-1"]');
  assert.match(patched.textContent, /Alpha \(renamed\)/, 'the row must show the new title');
  assert.equal(win.document.querySelectorAll('.job-row').length, 2,
    'patching one row must not disturb the others');
  mod.unmountJobs();
});

test('an event with no job payload still falls back to a refresh', { skip: dom.skip }, async () => {
  const win = freshDom();
  const { mod, calls } = await mountCounting(win);
  const afterMount = calls.length;

  win.document.dispatchEvent(new win.CustomEvent('dispatch:jobs-changed', { detail: {} }));
  await new Promise((r) => setTimeout(r, 40));

  assert.ok(calls.length > afterMount,
    'without a job payload there is nothing to patch, so the board must re-fetch');
  mod.unmountJobs();
});

test('the list is not blanked while a later refresh runs', { skip: dom.skip }, async () => {
  const win = freshDom();
  const { mod } = await mountCounting(win);
  assert.equal(win.document.querySelectorAll('.job-row').length, 2);

  // A refresh that never resolves: the rows already on screen must stay.
  let release;
  globalThis.fetch = (url) => new Promise((resolve) => {
    release = () => resolve({
      ok: true, status: 200, headers: new win.Headers(),
      json: async () => (String(url).includes('/months')
        ? { months: [], current: null } : { jobs: [], next_cursor: null }),
    });
  });
  win.document.dispatchEvent(new win.CustomEvent('dispatch:jobs-changed', { detail: {} }));
  await new Promise((r) => setTimeout(r, 30));

  assert.equal(win.document.querySelectorAll('.job-row').length, 2,
    'the board emptied itself while loading — that flash IS the reported lag');
  if (release) release();
  await new Promise((r) => setTimeout(r, 20));
  mod.unmountJobs();
});

test('the job detail modal makes the page behind it inert, and lifts it on close', { skip: dom.skip }, async () => {
  // role="dialog" aria-modal="true" is a promise the markup cannot keep on its
  // own: without inert, Tab walks straight out of the card into the board and
  // the rail underneath. main.js's own guard only watches the .modal-backdrop
  // nodes present at boot, and this overlay is built at click time, so it was
  // never covered.
  const win = freshDom();
  win.t = I18N_STUB.t;
  win.api = API_STUB;
  // The inert counter in util.js is a module singleton, and earlier tests in
  // this file open a job modal without closing it — so the count never
  // returns to zero and this window would start with a stale hold. Reset it,
  // the way a fresh page load would.
  //
  // The version is READ from job-thread.js rather than written here: util.js
  // is bumped whenever it changes, and a hardcoded `?v=` would quietly import
  // a SECOND copy of the module whose counter the code under test is not
  // using — the test would then reset nothing and fail for the wrong reason.
  const jtSrc = readFileSync(join(STATIC, 'js', 'job-thread.js'), 'utf8');
  const utilSpec = /from\s+'(\.\/util\.js\?v=\d+)'/.exec(jtSrc);
  assert.ok(utilSpec, 'job-thread.js no longer imports util.js under a version');
  const { resetInert } = await import(join(STATIC, 'js', utilSpec[1].replace('./', '')));
  resetInert();
  const job = {
    job_id: 'j-9', thread_id: 't-1', title: 'Trap', company: 'C',
    effective_state: 'pending', tags: [],
    updated_at: '2026-09-12T00:00:00Z', created_at: '2026-09-12T00:00:00Z',
  };
  globalThis.fetch = async (url) => ({
    ok: true, status: 200, headers: new win.Headers(),
    json: async () => (String(url).includes('/months')
      ? { months: [], current: null }
      : String(url).match(/\/api\/jobs\/[^/?]+$/)
        ? { job, events: [], score: null }
        : { jobs: [job], next_cursor: null }),
  });

  const mod = await import(`../static/js/job-thread.js?v=${Math.random()}`);
  const app = win.document.getElementById('app');
  assert.equal(app.hasAttribute('inert'), false, 'nothing is inert before opening');

  await mod.openJobDetail('j-9', null);
  await new Promise((r) => setTimeout(r, 30));
  assert.ok(win.document.querySelector('.job-modal-overlay'), 'the overlay must be on screen');
  assert.equal(app.hasAttribute('inert'), true,
    'the page behind an aria-modal dialog must be inert, or Tab escapes it');

  mod.closeJobDetail();
  assert.equal(app.hasAttribute('inert'), false,
    'inert must lift on close — a stuck inert is an app nobody can click');
  assert.equal(win.document.querySelector('.job-modal-overlay'), null);
});

test('a vote that reorders the board still makes no request', { skip: dom.skip }, async () => {
  // The production shape, which the earlier patch test did not have: every
  // vote bumps updated_at server-side, and the default sort IS `updated`, so
  // the voted job moves to the top. patchJob used to treat any reorder as
  // "cannot apply locally" and fall back to a full re-fetch — which meant the
  // fast path essentially never ran and one vote still cost a board reload.
  const win = freshDom();
  const { mod, calls, rows } = await mountCounting(win);
  const afterMount = calls.length;

  const order = () => Array.from(win.document.querySelectorAll('.job-row')).map((n) => n.dataset.jobId);
  assert.deepEqual(order(), ['j-2', 'j-1'], 'newest updated_at first');

  // j-1 voted: new state AND a newer updated_at, exactly as the server sends.
  const voted = { ...rows[0], effective_state: 'yes', updated_at: '2026-09-12T00:00:00Z' };
  win.document.dispatchEvent(new win.CustomEvent('dispatch:jobs-changed', {
    detail: { type: 'job_updated', job_id: 'j-1', job: voted },
  }));
  await new Promise((r) => setTimeout(r, 30));

  assert.equal(calls.length, afterMount,
    'a reordering update must still be applied from the payload, not re-fetched');
  assert.deepEqual(order(), ['j-1', 'j-2'], 'and the board must show the new order');
  mod.unmountJobs();
});

test('a failed refresh keeps the rows and shows the error beside them', { skip: dom.skip }, async () => {
  // render() used to replace the whole list with the error and return, so one
  // dropped request emptied a board the operator was reading — and state.error
  // was only cleared by a later SUCCESS, so every filter change afterwards
  // repainted the error over rows that were still in memory.
  const win = freshDom();
  const { mod } = await mountCounting(win);
  assert.equal(win.document.querySelectorAll('.job-row').length, 2);

  globalThis.fetch = async () => { throw new Error('network is down'); };
  win.document.dispatchEvent(new win.CustomEvent('dispatch:jobs-changed', { detail: {} }));
  await new Promise((r) => setTimeout(r, 40));

  assert.equal(win.document.querySelectorAll('.job-row').length, 2,
    'a transient failure must not empty the board');
  assert.ok(win.document.querySelector('.jobs-empty--error'),
    'the failure must be visible, not silent');

  // And a filter change must not repaint the error over the rows.
  const stateSel = win.document.querySelector('.jobs-toolbar__state');
  stateSel.value = '';
  stateSel.dispatchEvent(new win.Event('change'));
  await new Promise((r) => setTimeout(r, 30));
  assert.equal(win.document.querySelectorAll('.job-row').length, 2,
    'the rows must survive a repaint while an error is showing');
  mod.unmountJobs();
});

// =============================================================================
// 2026-09-23: a second open while the first is still loading must REPLACE it.
// A double-click (or an impatient second click during a slow fetch) used to
// stack two overlays; outside-click and Escape only closed the newest, and the
// older one stayed on screen until a reload.
// =============================================================================

function deferredJobFetch(win, job) {
  // Later suites in this file swap the global document for their own jsdom;
  // point it back at the shared window these tests query.
  globalThis.window = win;
  globalThis.document = win.document;
  const pending = [];
  globalThis.fetch = (url) => new Promise((resolve) => {
    pending.push(() => resolve({
      ok: true, status: 200, headers: new win.Headers(),
      json: async () => ({ thread: { id: job.thread_id }, job, events: [], score: null }),
    }));
  });
  return pending;
}

test('two opens in flight leave exactly one overlay, and outside-click closes it', { skip: dom.skip }, async () => {
  const win = await withDom();
  cleanOverlays(win);
  win.toast = () => {};
  const job = baseJob();
  const pending = deferredJobFetch(win, job);
  const { openJobDetail } = await freshJobThread();
  const a = openJobDetail(job.job_id);
  const b = openJobDetail(job.job_id);
  await flush();
  pending.forEach((r) => r());            // both fetches land, in order
  await Promise.all([a, b]);
  await flush();

  const overlays = win.document.querySelectorAll('.job-modal-overlay');
  assert.equal(overlays.length, 1, 'a superseded open must not paint a second overlay');
  overlays[0].dispatchEvent(new win.MouseEvent('click', { bubbles: true }));
  assert.equal(win.document.querySelectorAll('.job-modal-overlay').length, 0,
    'clicking the backdrop must close the (only) overlay');
});

test('the overlay is up before the fetch lands, and closing it cancels the pending paint', { skip: dom.skip }, async () => {
  const win = await withDom();
  cleanOverlays(win);
  win.toast = () => {};
  const job = baseJob();
  const pending = deferredJobFetch(win, job);
  const { openJobDetail } = await freshJobThread();
  const p = openJobDetail(job.job_id);
  await flush();
  const overlay = win.document.querySelector('.job-modal-overlay');
  assert.ok(overlay, 'the click must be acknowledged at once with a loading overlay');
  assert.ok(win.document.querySelector('.job-modal__loading'), 'the loading card shows while the job loads');
  overlay.dispatchEvent(new win.MouseEvent('click', { bubbles: true }));
  assert.equal(win.document.querySelectorAll('.job-modal-overlay').length, 0, 'backdrop click closes it while loading');
  pending.forEach((r) => r());
  await p;
  await flush();
  assert.equal(win.document.querySelectorAll('.job-modal-overlay').length, 0,
    'a closed-while-loading detail must not reappear when its fetch lands');
});

test('the second click of a double-click does not close the detail it just opened', { skip: dom.skip }, async () => {
  const win = await withDom();
  cleanOverlays(win);
  win.toast = () => {};
  const job = baseJob();
  const pending = deferredJobFetch(win, job);
  const { openJobDetail } = await freshJobThread();
  const p = openJobDetail(job.job_id);
  await flush();
  const overlay = win.document.querySelector('.job-modal-overlay');
  overlay.dispatchEvent(new win.MouseEvent('click', { bubbles: true, detail: 2 }));
  assert.equal(win.document.querySelectorAll('.job-modal-overlay').length, 1,
    'a detail=2 click on the backdrop is the tail of the opening double-click');
  pending.forEach((r) => r());
  await p;
  await flush();
  assert.ok(win.document.querySelector('.job-modal [data-signal="yes"]'), 'the detail rendered');
  win.document.querySelector('.job-modal-overlay').dispatchEvent(new win.MouseEvent('click', { bubbles: true, detail: 1 }));
  assert.equal(win.document.querySelectorAll('.job-modal-overlay').length, 0, 'a single backdrop click still closes');
});
