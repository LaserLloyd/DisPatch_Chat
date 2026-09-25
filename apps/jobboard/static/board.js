// Job Board — the board page (/apps/jobboard/).
//
// The Job Board app (docs/design/2026-09-25-apps.md). This page is framed by
// the shell without a sandbox and imports NOTHING from the shell except
// /static/js/app-sdk.js: its API calls, strings, theme and every hand-off
// back to the shell (open a thread, toast) go through the SDK.
//
// What it shows: every posting, grouped into one section per month (newest
// first), with local sort/filter controls and a "Find jobs" form that asks
// the board's agent (Scout) to search. A job is opened IN ITS THREAD: a click
// hands the job's monthly thread to the shell, whose thread hook (thread.js)
// shows that job's detail — votes, applied, tags, archive, feedback for
// Scout, the activity log — above the conversation about it.
//
// Carried over from the shell's old js/jobs.js, and still the reasons it is
// shaped this way:
//   * the toolbar is built ONCE and never rebuilt — a control that deletes
//     itself when you touch it loses focus and the caret;
//   * sort and filters are local — the whole list is already in memory;
//   * a live `job_updated` frame patches one row and fetches nothing;
//   * a refresh never blanks the rows being read, and a failure is a banner
//     above them, not a replacement for them;
//   * a superseded request never wins (AbortController).

import {
  api, t, ready, theme, openThread, toast, onMessage, DecoyError,
  iconLabel, RAIL_ICONS,
} from '/static/js/app-sdk.js?v=1';

// The router is mounted at /api/apps/jobboard (and, for the scout agent and
// crons, at the legacy /api/jobs too). The page uses the app path.
export const API_BASE = '/api/apps/jobboard';
// The roster bot whose monthly threads hold the jobs (app.yaml `bot.id`).
export const BOT_ID = 'jobboard';

// Local state — survives a re-render while the page stays open.
const state = {
  sort: 'updated',           // 'updated' | 'salary' | 'posted'
  filterState: '',           // '' | 'yes' | 'no' | 'maybe' | 'pending' | 'applied' | 'archived'
  filterTag: '',
  filterRemote: '',
  jobs: [],                  // flat list across every month (single GET)
  months: [],                // [{year, month, key, label}]
  loading: false,
  findOpen: false,
  findStatus: '',
  error: null,
  locked: false,             // Safe Mode answered: nothing here is shown
};

function chipClass(st) {
  return `job-chip job-chip-state job-chip-state--${st || 'pending'}`;
}

// Literal keys (not t(`state.${x}`)) so a key scan can see every one of them.
const STATE_KEY = {
  pending: 'state.pending', yes: 'state.yes', no: 'state.no', maybe: 'state.maybe',
  applied: 'state.applied', archived: 'state.archived', duplicate: 'state.duplicate',
};
const REMOTE_KEY = { remote: 'remote.remote', hybrid: 'remote.hybrid', onsite: 'remote.onsite' };
export const stateLabel = (s) => t(STATE_KEY[s] || STATE_KEY.pending);

function formatSalary(j) {
  if (j.salary_min == null && j.salary_max == null) return '';
  const a = j.salary_min, b = j.salary_max, c = j.salary_currency || 'USD';
  if (a && b) return `${a.toLocaleString()}–${b.toLocaleString()} ${c}`;
  return `${(a || b).toLocaleString()} ${c}`;
}

function formatAge(j) {
  const last = j.last_seen || j.updated_at || j.created_at;
  if (!last) return '';
  const days = Math.max(0, Math.floor((Date.now() - Date.parse(last)) / 86400000));
  if (days === 0) return t('age.today');
  if (days === 1) return t('age.yesterday');
  return t('age.days_ago', { n: days });
}

function ym(iso) {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  return { year: d.getUTCFullYear(), month: d.getUTCMonth() + 1,
           key: `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, '0')}` };
}

function monthLabel(year, month) {
  const m = state.months.find((mm) => mm.year === year && mm.month === month);
  if (m && m.label) return m.label;
  try {
    return new Intl.DateTimeFormat(document.documentElement.lang || 'en', { year: 'numeric', month: 'long', timeZone: 'UTC' })
      .format(new Date(Date.UTC(year, month - 1, 1)));
  } catch { return `${year}-${String(month).padStart(2, '0')}`; }
}

/** Group jobs into month sections, newest first. */
export function groupByMonth(jobs) {
  const buckets = new Map();
  for (const j of jobs) {
    const k = ym(j.created_at || j.posted_at || j.last_seen || j.updated_at);
    if (!k) continue;
    if (!buckets.has(k.key)) buckets.set(k.key, { ...k, label: monthLabel(k.year, k.month), jobs: [] });
    buckets.get(k.key).jobs.push(j);
  }
  return Array.from(buckets.values()).sort((a, b) => (b.year - a.year) || (b.month - a.month));
}

/** The jobs to show, given the current filters and sort. Pure, and the ONLY
 *  place filtering happens: the server is never asked again for a filter. */
export function visibleJobs() {
  let out = state.jobs;
  if (state.filterState) out = out.filter((j) => (j.effective_state || j.state) === state.filterState);
  if (state.filterRemote) out = out.filter((j) => j.remote_type === state.filterRemote);
  if (state.filterTag) out = out.filter((j) => (j.tags || []).includes(state.filterTag));
  const by = {
    salary: (a, b) => (b.salary_max || b.salary_min || 0) - (a.salary_max || a.salary_min || 0),
    posted: (a, b) => (b.posted_at || '').localeCompare(a.posted_at || ''),
    updated: (a, b) => (b.updated_at || '').localeCompare(a.updated_at || ''),
  };
  return [...out].sort(by[state.sort] || by.updated);
}

// ------------------- the hand-off --------------------------------------- //

/** Open a job: close the board and show the job in its monthly thread, where
 *  the thread hook paints its detail. */
function openJob(j) {
  if (!j || !j.thread_id) { toast(t('detail.not_found'), true); return; }
  openThread(j.thread_id, { job_id: j.job_id });
}

// ------------------- DOM builders ------------------------------------- //

function jobRow(j) {
  const li = document.createElement('li');
  li.className = 'job-row';
  li.dataset.jobId = j.job_id;
  li.tabIndex = 0;
  li.setAttribute('role', 'button');
  li.setAttribute('aria-label', `${j.title || t('untitled')} — ${j.company || ''}`);
  if ((j.effective_state || j.state) === 'archived' || j.is_expired) li.classList.add('job-row--archived');

  // The board bot's thumbnail, with a two-stage fallback: thumb → full avatar
  // → an emoji tile (a fresh install has no avatar at all; without the last
  // stage every row shows a broken-image icon).
  const thumb = document.createElement('img');
  thumb.className = 'job-row__thumb';
  thumb.alt = '';
  thumb.loading = 'lazy';
  thumb.decoding = 'async';
  thumb.src = `/api/bots/${BOT_ID}/avatar/thumb`;
  thumb.addEventListener('error', () => {
    if (thumb.src.endsWith('/avatar/thumb')) { thumb.src = `/api/bots/${BOT_ID}/avatar`; return; }
    const block = document.createElement('div');
    block.className = 'job-row__thumb job-row__thumb--fallback';
    block.setAttribute('aria-hidden', 'true');
    block.textContent = '🎯';
    thumb.replaceWith(block);
  });

  const body = document.createElement('div');
  body.className = 'job-row__body';

  const header = document.createElement('div');
  header.className = 'job-row__head';
  const st = j.effective_state || j.state;
  const chip = document.createElement('span');
  chip.className = chipClass(st);
  chip.textContent = stateLabel(st);
  const title = document.createElement('span');
  title.className = 'job-row__title';
  title.textContent = j.title || t('untitled');
  header.append(chip, title);
  if (j.duplicate_of) {
    const badge = document.createElement('span');
    badge.className = 'job-chip job-chip--dup';
    badge.textContent = t('badge.duplicate');
    header.appendChild(badge);
  }

  const meta = document.createElement('div');
  meta.className = 'job-row__meta';
  const parts = [];
  if (j.company) parts.push(j.company);
  if (j.location) parts.push(j.location);
  if (REMOTE_KEY[j.remote_type]) parts.push(t(REMOTE_KEY[j.remote_type]));
  meta.textContent = parts.join(' · ');

  const salary = document.createElement('div');
  salary.className = 'job-row__salary';
  salary.textContent = formatSalary(j);

  const tags = document.createElement('div');
  tags.className = 'job-row__tags';
  for (const tag of (j.tags || []).slice(0, 8)) {
    const c = document.createElement('span');
    c.className = 'job-chip';
    c.textContent = tag;
    tags.appendChild(c);
  }

  const foot = document.createElement('div');
  foot.className = 'job-row__foot';
  const age = document.createElement('span');
  age.textContent = formatAge(j);
  const open = document.createElement('span');
  open.className = 'job-row__open';
  open.textContent = document.documentElement.dir === 'rtl' ? '‹' : '›';
  open.setAttribute('aria-hidden', 'true');
  foot.append(age, open);

  body.appendChild(header);
  if (meta.textContent) body.appendChild(meta);
  if (salary.textContent) body.appendChild(salary);
  if (tags.childNodes.length) body.appendChild(tags);
  body.appendChild(foot);
  li.append(thumb, body);

  li.addEventListener('click', () => openJob(j));
  li.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); openJob(j); }
  });
  return li;
}

function monthSection(group) {
  const section = document.createElement('section');
  section.className = 'jobs-month';
  section.dataset.monthKey = group.key;
  const header = document.createElement('header');
  header.className = 'jobs-month__head';
  const label = document.createElement('h2');
  label.className = 'jobs-month__title';
  label.textContent = group.label;
  const count = document.createElement('span');
  count.className = 'jobs-month__count';
  count.textContent = group.jobs.length === 1
    ? t('month.count', { n: group.jobs.length })
    : t('month.count_plural', { n: group.jobs.length });
  header.append(label, count);
  const list = document.createElement('ul');
  list.className = 'jobs-list';
  for (const j of group.jobs) list.appendChild(jobRow(j));
  section.append(header, list);
  return section;
}

// The toolbar is built ONCE per mount and never rebuilt. `onChange` repaints
// the list; it never fetches.
function buildToolbar(onChange) {
  const bar = document.createElement('div');
  bar.className = 'jobs-toolbar';

  const select = (cls, values, labelFor, current, onPick, aria) => {
    const sel = document.createElement('select');
    sel.className = cls;
    if (aria) sel.setAttribute('aria-label', aria);
    for (const v of values) {
      const o = document.createElement('option');
      o.value = v;
      o.textContent = labelFor(v);
      sel.appendChild(o);
    }
    sel.value = current;
    sel.addEventListener('change', () => { onPick(sel.value); onChange(); });
    return sel;
  };

  const sortLbl = document.createElement('label');
  sortLbl.className = 'jobs-toolbar__field';
  sortLbl.append(`${t('sort.label')}: `);
  const sortLabels = { updated: t('sort.updated'), salary: t('sort.salary'), posted: t('sort.posted') };
  sortLbl.appendChild(select('jobs-toolbar__sort', Object.keys(sortLabels), (v) => sortLabels[v], state.sort,
    (v) => { state.sort = v; }));
  bar.appendChild(sortLbl);

  bar.appendChild(select('jobs-toolbar__state',
    ['', 'pending', 'yes', 'no', 'maybe', 'applied', 'archived'],
    (v) => (v ? stateLabel(v) : t('filter.all')),
    state.filterState, (v) => { state.filterState = v; }, t('filter.all')));

  bar.appendChild(select('jobs-toolbar__remote',
    ['', 'remote', 'hybrid', 'onsite'],
    (v) => (v ? t(REMOTE_KEY[v]) : t('filter.any_remote')),
    state.filterRemote, (v) => { state.filterRemote = v; }, t('filter.any_remote')));

  const tagInput = document.createElement('input');
  tagInput.type = 'text';
  tagInput.className = 'jobs-toolbar__tag';
  tagInput.placeholder = t('filter.tag_placeholder');
  tagInput.value = state.filterTag;
  tagInput.setAttribute('aria-label', t('filter.tag_placeholder'));
  let tagTimer = null;
  tagInput.addEventListener('input', () => {
    if (tagTimer) clearTimeout(tagTimer);
    tagTimer = setTimeout(() => {
      tagTimer = null;
      state.filterTag = tagInput.value.trim().toLowerCase();
      onChange();
    }, 120);
  });
  bar.appendChild(tagInput);

  const busy = document.createElement('span');
  busy.className = 'jobs-toolbar__busy';
  busy.hidden = true;
  busy.setAttribute('role', 'status');
  busy.setAttribute('aria-live', 'polite');
  busy.textContent = t('loading');
  bar.appendChild(busy);

  // Hand the search to Scout. New postings arrive as live frames on their own.
  const findBtn = document.createElement('button');
  findBtn.type = 'button';
  findBtn.className = 'btn-primary jobs-find-btn';
  findBtn.append(...iconLabel(RAIL_ICONS.search, t('find.button')));
  findBtn.setAttribute('aria-expanded', String(state.findOpen));
  findBtn.addEventListener('click', () => {
    state.findOpen = !state.findOpen;
    findBtn.setAttribute('aria-expanded', String(state.findOpen));
    renderFindForm();
    if (state.findOpen) hosts.find?.querySelector('.jobs-find__input')?.focus();
  });
  bar.appendChild(findBtn);

  bar._busy = busy;
  bar._findBtn = findBtn;
  return bar;
}

function buildFindForm() {
  const form = document.createElement('form');
  form.className = 'jobs-find';
  const input = document.createElement('input');
  input.type = 'text';
  input.className = 'jobs-find__input';
  input.maxLength = 1000;
  input.placeholder = t('find.placeholder');
  input.setAttribute('aria-label', t('find.placeholder'));
  const send = document.createElement('button');
  send.type = 'submit';
  send.className = 'btn-primary';
  send.textContent = t('find.send');
  const cancel = document.createElement('button');
  cancel.type = 'button';
  cancel.className = 'btn-secondary';
  cancel.textContent = t('vote.cancel');
  cancel.addEventListener('click', () => {
    state.findOpen = false;
    renderFindForm();
    const btn = hosts.toolbar && hosts.toolbar._findBtn;
    if (btn) { btn.setAttribute('aria-expanded', 'false'); btn.focus(); }
  });
  form.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    send.disabled = true;
    try {
      const r = await api(`${API_BASE}/find`, {
        method: 'POST', query: { bot_id: BOT_ID }, body: { query: input.value.trim() || null },
      });
      state.findStatus = r && r.dispatched ? t('find.sent') : t('find.not_sent');
      state.findOpen = false;
    } catch (e) {
      state.findStatus = t('error', { msg: (e && e.message) || String(e) });
      send.disabled = false;
    }
    renderFindForm();
    renderStatus();
    const btn = hosts.toolbar && hosts.toolbar._findBtn;
    if (btn) btn.setAttribute('aria-expanded', String(state.findOpen));
  });
  form.append(input, send, cancel);
  return form;
}

// ------------------- the page's own DOM ------------------------------- //
// Four stable hosts, created once per mount. render() only ever replaces the
// contents of `list`; the toolbar, the find form and the status line keep
// their identity (and their focus) across every update.
const hosts = { root: null, toolbar: null, find: null, status: null, list: null };

let inflight = null;      // AbortController for the one request we care about
let loadedOnce = false;   // first paint shows a placeholder; later ones do not
let offMessage = null;

function setBusy(on) {
  const busy = hosts.toolbar && hosts.toolbar._busy;
  if (busy) busy.hidden = !on;
}

function renderFindForm() {
  if (!hosts.find) return;
  hosts.find.replaceChildren();
  if (state.findOpen) hosts.find.appendChild(buildFindForm());
}

function renderStatus() {
  if (!hosts.status) return;
  hosts.status.replaceChildren();
  // A banner above the rows, never a replacement for them.
  if (state.error) {
    const err = document.createElement('p');
    err.className = 'jobs-find__status jobs-empty--error';
    err.setAttribute('role', 'alert');
    err.textContent = t('error', { msg: state.error });
    hosts.status.appendChild(err);
  }
  if (!state.findStatus) return;
  const note = document.createElement('p');
  note.className = 'jobs-find__status';
  note.setAttribute('role', 'status');
  note.textContent = state.findStatus;
  hosts.status.appendChild(note);
}

function renderLocked() {
  if (!hosts.root) return;
  const p = document.createElement('p');
  p.className = 'jb-locked';
  p.setAttribute('role', 'status');
  p.textContent = t('locked');
  hosts.root.replaceChildren(p);
}

/** Repaint the list — and ONLY the list. */
export function render() {
  if (!hosts.list) return;
  if (!loadedOnce && state.loading) {
    const skel = document.createElement('p');
    skel.className = 'jobs-empty';
    skel.textContent = t('loading');
    hosts.list.replaceChildren(skel);
    return;
  }
  const visible = visibleJobs();
  if (!visible.length) {
    const empty = document.createElement('p');
    empty.className = 'jobs-empty';
    empty.textContent = t('empty');
    hosts.list.replaceChildren(empty);
    return;
  }
  const frag = document.createDocumentFragment();
  for (const g of groupByMonth(visible)) frag.appendChild(monthSection(g));
  hosts.list.replaceChildren(frag);
}

/** Apply a job the server has already sent us, without asking for it again.
 *  False ONLY for a job this board has never seen — the one case a re-fetch
 *  tells us something new. */
export function patchJob(job) {
  if (!job || !job.job_id) return false;
  const i = state.jobs.findIndex((j) => j.job_id === job.job_id);
  if (i === -1) return false;
  const before = visibleJobs();
  state.jobs = state.jobs.slice();
  state.jobs[i] = job;
  const after = visibleJobs();
  const sameSet = before.length === after.length && !before.some((j, n) => j.job_id !== after[n].job_id);
  if (sameSet && hosts.list) {
    // Walk the rows instead of building a selector from a server id.
    for (const row of hosts.list.querySelectorAll('.job-row')) {
      if (row.dataset.jobId !== job.job_id) continue;
      row.replaceWith(jobRow(job));
      return true;
    }
  }
  render();   // the set or the order moved: repaint from memory, still no request
  return true;
}

/** Fetch the board. Filters are NOT sent — they are applied locally. */
export async function refresh() {
  if (inflight) inflight.abort();
  const ctl = new AbortController();
  inflight = ctl;
  state.loading = true;
  state.error = null;
  renderStatus();
  setBusy(true);
  if (!loadedOnce) render();
  try {
    const [listResp, monthsResp] = await Promise.all([
      api(API_BASE, { signal: ctl.signal }),
      api(`${API_BASE}/months`, { query: { bot_id: BOT_ID }, signal: ctl.signal }).catch((e) => {
        if (e instanceof DecoyError) throw e;
        return { months: [] };
      }),
    ]);
    if (ctl.signal.aborted) return;
    state.jobs = (listResp && listResp.jobs) || [];
    state.months = (monthsResp && monthsResp.months) || [];
    state.error = null;
    loadedOnce = true;
  } catch (e) {
    if (ctl.signal.aborted || (e && e.name === 'AbortError')) return;
    if (e instanceof DecoyError) {
      state.locked = true;
      renderLocked();
      return;
    }
    state.error = (e && e.message) || String(e);
    renderStatus();
    if (!loadedOnce) state.jobs = [];
  } finally {
    if (inflight === ctl) { inflight = null; state.loading = false; setBusy(false); }
  }
  if (!state.locked) render();
}

/** A live frame from the shell (the server's `app:jobboard:*` broadcast). */
function onFrame(frame) {
  if (!frame || typeof frame.type !== 'string') return;
  if (!/(?:^|:)job_(?:created|updated)$/.test(frame.type)) return;
  if (state.locked) return;
  // A patch applied while a refresh is in flight would be reverted by the
  // older response landing; restart from a fresh snapshot instead.
  if (inflight) { refresh(); return; }
  try {
    if (frame.job && patchJob(frame.job)) return;
  } catch { /* fall through to a full refresh */ }
  refresh();
}

/** Build the board into `root` and load it. Idempotent. */
export async function mountBoard(root) {
  unmountBoard();
  hosts.root = root;
  root.replaceChildren();
  hosts.toolbar = buildToolbar(() => render());
  hosts.find = document.createElement('div');
  hosts.status = document.createElement('div');
  hosts.list = document.createElement('div');
  hosts.list.className = 'jobs-list-host';
  root.append(hosts.toolbar, hosts.find, hosts.status, hosts.list);
  loadedOnce = false;
  state.locked = false;
  offMessage = onMessage((msg) => {
    if (msg.type === 'dispatch:frame') onFrame(msg.frame);
    // The SDK has already switched the dictionaries: rebuild the chrome.
    else if (msg.type === 'dispatch:lang') { document.title = t('title'); mountBoard(root); }
  });
  renderFindForm();
  renderStatus();
  await refresh();
}

/** Tear the board down: listener, in-flight request, DOM. */
export function unmountBoard() {
  if (offMessage) { offMessage(); offMessage = null; }
  if (inflight) { inflight.abort(); inflight = null; }
  if (hosts.root) hosts.root.replaceChildren();
  hosts.root = hosts.toolbar = hosts.find = hosts.status = hosts.list = null;
  loadedOnce = false;
  state.loading = false;
  state.error = null;
}

/** Test hook: the module's state object. */
export function _state() { return state; }

// Boot: only on the real page (its <body class="jb-page">), never when a
// test imports this module into some other document.
async function boot() {
  const root = document.getElementById('jb-root');
  if (!root) return;
  theme();
  await ready();
  document.title = t('title');
  document.body.classList.add('jb-ready');
  await mountBoard(root);
}
if (typeof document !== 'undefined' && document.body && document.body.classList.contains('jb-page')) {
  boot().catch((e) => {
    console.error('[jobboard] boot failed', e);
    document.body.classList.add('jb-ready');
  });
}
