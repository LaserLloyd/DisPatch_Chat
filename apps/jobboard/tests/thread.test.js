// Job Board app — the thread hook (apps/jobboard/static/thread.js).
//
// Moved from frontend/tests/jobs.test.js's job-thread.js half when the board
// became an app. The detail used to be a modal opened from the board; it is
// now a panel the shell mounts into a jobboard thread. The regressions that
// section pinned still hold, now against mount():
//   1. "Applied" posts to /applied, never signal:'applied' to /vote (a 422);
//   2. Feedback for Scout posts {comment, reason_tag} and reports honestly
//      (dispatched / picked up later / not available — never fake success);
//   3. a posting URL that is not http(s) is omitted, never put in an href;
//   4. the vote highlight is server truth (job.last_vote, both shapes).
// Plus the hook contract: it renders only into the host it is given, loads the
// hinted job, keeps up with live frames without a fetch, and unmount() leaves
// the host empty with no late paint.

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { dom, setupDom, flush, appDir } from '../../../frontend/tests/helpers/app-test-env.js';

const APP = appDir('jobboard');
const EN = JSON.parse(readFileSync(join(APP, 'locales', 'en.json'), 'utf8'));
const SRC = readFileSync(join(APP, 'static', 'thread.js'), 'utf8');
const SHELL = '<!doctype html><html><head></head><body><header class="chat-header"></header><div id="app-thread-host"></div><div id="messages"></div></body></html>';
const BASE = '/api/apps/jobboard';

function baseJob(over = {}) {
  const now = new Date().toISOString();
  return {
    job_id: 'j-1', thread_id: 't-1', title: 'Senior Eng @ Vercel', company: 'Vercel', location: 'NYC',
    remote_type: 'hybrid', seniority: 'senior', salary_min: 150000, salary_max: 190000, salary_currency: 'USD',
    tags: ['react'], source_agent: 'scout', posted_at: now, first_seen: now, last_seen: now,
    brief: 'Line one\nLine two', url: 'https://example.com/job/1',
    state: 'pending', effective_state: 'pending', is_expired: false, duplicate_of: null,
    created_at: now, updated_at: now, last_vote: null, ...over,
  };
}

/** Mount the hook the way main.js does, against stubbed routes. */
async function mountHook({ job = baseJob(), hint = { job_id: job.job_id }, routes: extra = {}, others = [] } = {}) {
  const env = setupDom(SHELL, 'http://127.0.0.1:8765/', {
    [`GET ${BASE}?thread_id=${job.thread_id}&limit=500`]: () => [200, { jobs: [job, ...others] }],
    [`GET ${BASE}/${job.job_id}`]: () => [200, { thread: { id: job.thread_id }, job, events: [], score: { score: 0.8, explanation: ['tag match'] } }],
    [`POST ${BASE}/${job.job_id}/vote`]: () => [200, { ok: true }],
    [`POST ${BASE}/${job.job_id}/applied`]: () => [200, { ok: true }],
    [`POST ${BASE}/${job.job_id}/tags`]: () => [200, { ok: true }],
    [`POST ${BASE}/${job.job_id}/archive`]: () => [200, { ok: true }],
    ...extra,
  });
  const sdk = await import('/static/js/app-sdk.js?v=1');
  sdk._resetForTest();
  const app = sdk.forApp('jobboard');
  env.routes['GET /apps/jobboard/locales/en.json'] = () => [200, EN];
  await app.ready('en');
  const hook = await import(`/apps/jobboard/thread.js?t=${Math.random()}`);
  const host = env.win.document.getElementById('app-thread-host');
  const toasts = [];
  const opened = [];
  const ret = await hook.mount({
    threadEl: host,
    headerEl: env.win.document.querySelector('.chat-header'),
    thread: { id: job.thread_id, bot_id: 'jobboard' },
    api: app.api, t: app.t, dateTime: app.dateTime,
    hint, toast: (m, e) => toasts.push([m, !!e]), openThread: (id) => opened.push(id),
  });
  await flush();
  return { ...env, host, ret, hook, toasts, opened, job };
}

const card = (host) => host.querySelector('.job-card');

test('the hook imports nothing from the shell but app-sdk.js', () => {
  const imports = [...SRC.matchAll(/^import .* from '([^']+)';$/gm)].map((m) => m[1]);
  assert.deepEqual(imports, ['/static/js/app-sdk.js?v=1']);
  assert.doesNotMatch(SRC, /window\.(?:__|toast)/, 'no window globals from the old job-thread.js');
});

test('mount(): the hinted job’s detail, in the host it was given, with its thread’s jobs to pick from', { skip: dom.skip }, async () => {
  const other = baseJob({ job_id: 'j-2', title: 'Staff Eng', created_at: '2026-01-01T00:00:00Z' });
  const { host, win, calls } = await mountHook({ others: [other] });
  assert.ok(card(host), 'the job card is painted');
  assert.equal(card(host).querySelector('.job-card__title').textContent, 'Senior Eng @ Vercel');
  assert.equal(win.document.querySelector('#messages').children.length, 0, 'nothing leaks into the messages');
  const pick = host.querySelector('.jb-thread__pick');
  assert.deepEqual([...pick.options].map((o) => o.value), ['', 'j-1', 'j-2']);
  assert.equal(pick.value, 'j-1');
  assert.equal(host.querySelector('.jb-thread__label').textContent, '2 jobs in this thread');
  assert.ok(win.document.head.querySelector('link[data-app-css="jobboard"][href="/apps/jobboard/board.css?v=2"]'), 'its stylesheet is linked once');
  assert.ok(calls.some((c) => c.url === `${BASE}?thread_id=t-1&limit=500`));
  // The meta line uses translated labels, and the brief keeps its newlines.
  assert.match(card(host).querySelector('.job-card__meta').textContent, /Hybrid · Senior · 150,000–190,000 USD/);
  assert.equal(card(host).querySelector('.job-card__brief').textContent, 'Line one\nLine two');

  // Switching the picker loads the other job.
  calls.length = 0;
  pick.value = 'j-2';
  pick.dispatchEvent(new win.Event('change'));
  await flush();
  assert.ok(calls.some((c) => c.key === `GET ${BASE}/j-2`), 'the picked job is fetched');
});

test('no hint: the panel is a closed picker, and nothing is fetched but the list', { skip: dom.skip }, async () => {
  const { host, calls } = await mountHook({ hint: null });
  assert.equal(card(host), null);
  assert.equal(host.querySelector('.jb-thread__toggle').hidden, true);
  assert.equal(calls.filter((c) => /\/j-1$/.test(c.url)).length, 0);
});

test('"Applied" posts to /applied, never signal:applied to /vote (the 422 regression)', { skip: dom.skip }, async () => {
  const { host, calls, win } = await mountHook();
  card(host).querySelector('[data-signal="applied"]').dispatchEvent(new win.Event('click', { bubbles: true }));
  await flush();
  assert.equal(calls.filter((c) => c.url.endsWith('/applied')).length, 1);
  assert.equal(calls.filter((c) => c.url.endsWith('/vote') && c.body && c.body.signal === 'applied').length, 0);
});

test('No → an inline "why no?" row → the reason goes with the vote', { skip: dom.skip }, async () => {
  const { host, calls, win } = await mountHook();
  card(host).querySelector('[data-signal="no"]').click();
  assert.ok(card(host).querySelector('.job-reason'), 'the reasons are a row in the panel, not a second overlay');
  assert.equal(calls.filter((c) => c.url.endsWith('/vote')).length, 0, 'nothing sent before a reason');
  const note = card(host).querySelector('.job-note__input');
  note.value = 'too far';
  note.dispatchEvent(new win.Event('input'));
  card(host).querySelector('[data-reason="wrong_location"]').click();
  await flush();
  const vote = calls.find((c) => c.url.endsWith('/vote'));
  assert.deepEqual(vote.body, { signal: 'no', reason_tag: 'wrong_location', comment: 'too far' });
  assert.equal(card(host).querySelector('.job-reason'), null, 'the row closes after the vote');
});

test('the vote highlight is server truth, flat or object shape', { skip: dom.skip }, async () => {
  let { host } = await mountHook({ job: baseJob({ last_vote: 'yes' }) });
  assert.ok(card(host).querySelector('[data-signal="yes"]').classList.contains('job-vote__btn--active'));
  ({ host } = await mountHook({ job: baseJob({ last_vote: { signal: 'vote_maybe', actor: 'user' } }) }));
  assert.ok(card(host).querySelector('[data-signal="maybe"]').classList.contains('job-vote__btn--active'));
  assert.equal(card(host).querySelectorAll('.job-vote__btn--active').length, 1);
});

test('an invalid posting URL is omitted entirely; a valid one is a safe link', { skip: dom.skip }, async () => {
  let { host } = await mountHook({ job: baseJob({ url: 'javascript:alert(1)' }) });
  assert.equal(card(host).querySelector('.job-card__link'), null);
  assert.equal(card(host).querySelector('a[href^="javascript"]'), null);
  ({ host } = await mountHook());
  const a = card(host).querySelector('.job-card__link');
  assert.equal(a.getAttribute('href'), 'https://example.com/job/1');
  assert.equal(a.getAttribute('rel'), 'noopener noreferrer');
  assert.equal(a.getAttribute('target'), '_blank');
});

test('tags: × removes one, Enter adds one; Archive archives', { skip: dom.skip }, async () => {
  const { host, calls, win, toasts } = await mountHook();
  card(host).querySelector('.job-chip__remove').click();
  await flush();
  assert.deepEqual(calls.find((c) => c.url.endsWith('/tags')).body, { add: [], remove: ['react'] });
  const input = card(host).querySelector('.job-card__tag-input');
  input.value = 'Remote-OK';
  input.dispatchEvent(new win.KeyboardEvent('keydown', { key: 'Enter' }));
  await flush();
  assert.deepEqual(calls.filter((c) => c.url.endsWith('/tags'))[1].body, { add: ['remote-ok'], remove: [] });
  card(host).querySelector('.job-card__archive').click();
  await flush();
  assert.equal(calls.filter((c) => c.url.endsWith('/archive') && c.method === 'POST').length, 1);
  assert.deepEqual(toasts, [[EN['archive.done'], false]]);
});

test('Feedback for Scout posts {comment, reason_tag} and shows the dispatched confirmation', { skip: dom.skip }, async () => {
  const job = baseJob();
  const { host, calls } = await mountHook({
    job, routes: { [`POST ${BASE}/j-1/feedback`]: () => [200, { ok: true, dispatched: true, thread_id: 't-1' }] },
  });
  const sec = card(host).querySelector('.job-feedback');
  sec.querySelector('.job-feedback__input').value = 'more remote roles';
  sec.querySelector('.job-feedback__reason').value = 'wrong_location';
  sec.querySelector('.job-feedback__send').click();
  await flush();
  assert.deepEqual(calls.find((c) => c.url.endsWith('/feedback')).body, { comment: 'more remote roles', reason_tag: 'wrong_location' });
  assert.equal(card(host).querySelector('.job-feedback__confirm p').textContent, EN['feedback.sent']);
  assert.equal(card(host).querySelector('.job-feedback__open-thread'), null, 'Scout answers in THIS thread: no hop offered');
});

test('Feedback with dispatched:false reads "picked up later"; elsewhere offers the hop', { skip: dom.skip }, async () => {
  const { host, opened } = await mountHook({
    routes: { [`POST ${BASE}/j-1/feedback`]: () => [200, { ok: true, dispatched: false, thread_id: 't-other' }] },
  });
  const sec = card(host).querySelector('.job-feedback');
  sec.querySelector('.job-feedback__input').value = 'x';
  sec.querySelector('.job-feedback__send').click();
  await flush();
  assert.equal(card(host).querySelector('.job-feedback__confirm p').textContent, EN['feedback.sent_no_agent']);
  card(host).querySelector('.job-feedback__open-thread').click();
  assert.deepEqual(opened, ['t-other']);
});

test('Feedback: an empty note is refused inline; a 404 is "not available", never fake success', { skip: dom.skip }, async () => {
  const { host, calls } = await mountHook({ routes: { [`POST ${BASE}/j-1/feedback`]: () => [404, { detail: 'Not Found' }] } });
  const sec = card(host).querySelector('.job-feedback');
  sec.querySelector('.job-feedback__send').click();
  await flush();
  assert.equal(sec.querySelector('.job-feedback__error').textContent, EN['feedback.need_comment']);
  assert.equal(calls.filter((c) => c.url.endsWith('/feedback')).length, 0);
  sec.querySelector('.job-feedback__input').value = 'x';
  sec.querySelector('.job-feedback__send').click();
  await flush();
  assert.equal(sec.querySelector('.job-feedback__error').textContent, EN['feedback.not_available']);
  assert.equal(card(host).querySelector('.job-feedback__confirm'), null);
});

test('a job that cannot be loaded says why (gone / locked / offline)', { skip: dom.skip }, async () => {
  let { host } = await mountHook({ routes: { [`GET ${BASE}/j-1`]: () => [404, { detail: 'Job not found' }] } });
  assert.equal(host.querySelector('.jb-thread__status').textContent, EN['detail.not_found']);
  ({ host } = await mountHook({ routes: { [`GET ${BASE}/j-1`]: () => [403, { detail: 'Unlock', decoy: true }] } }));
  assert.equal(host.querySelector('.jb-thread__status').textContent, EN['detail.locked']);
  ({ host } = await mountHook({ routes: { [`GET ${BASE}/j-1`]: () => { throw new TypeError('Failed to fetch'); } } }));
  assert.equal(host.querySelector('.jb-thread__status').textContent, EN['detail.offline']);
});

test('onFrame: a job_updated for this thread repaints without a fetch; others are ignored', { skip: dom.skip }, async () => {
  const { host, ret, calls, job } = await mountHook();
  const before = calls.length;
  ret.onFrame({ type: 'app:jobboard:job_updated', job: { ...job, title: 'Renamed', effective_state: 'yes', state: 'yes' } });
  assert.equal(card(host).querySelector('.job-card__title').textContent, 'Renamed');
  assert.equal(card(host).querySelector('.job-chip-state').textContent, EN['state.yes']);
  ret.onFrame({ type: 'app:jobboard:job_updated', job: { ...job, thread_id: 't-elsewhere', title: 'Nope' } });
  ret.onFrame({ type: 'message', job: { ...job, title: 'Nope' } });
  assert.equal(card(host).querySelector('.job-card__title').textContent, 'Renamed');
  assert.equal(calls.length, before, 'no request');
});

test('unmount() empties the host, and a load that lands afterwards never paints', { skip: dom.skip }, async () => {
  const { host, ret, win } = await mountHook();
  let release;
  globalThis.fetch = () => new Promise((r) => { release = () => r({ ok: true, status: 200, headers: new win.Headers(), json: async () => ({ job: baseJob({ title: 'LATE' }), events: [] }) }); });
  host.querySelector('.jb-thread__pick').dispatchEvent(new win.Event('change'));
  ret.unmount();
  assert.equal(host.children.length, 0);
  release();
  await flush();
  assert.equal(host.children.length, 0, 'no late paint into a thread that was left');
});
