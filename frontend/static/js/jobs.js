// Jobs board — the list view.
//
// 2026-09-15 redesign: the board is NOT a chat. It is a structured
// list of job postings, grouped by month (one section per month, with
// the months acting like thread headers). Each job is a clickable
// card; clicking opens a detail panel with the full job metadata +
// voting controls. The month picker (top) lets the operator flip
// between months without leaving the board.
//
// Layout:
//   ┌──────────────────────────────────────────────────────────┐
//   │ < Sep 2026 >  [+ Current month ▼]   [filters]            │
//   ├──────────────────────────────────────────────────────────┤
//   │ ▼ September 2026                                          │
//   │   [ Job card • Staff SWE @ Anthropic ]                   │
//   │   [ Job card • Junior Dev @ Acme ]                        │
//   │ ▼ August 2026                                             │
//   │   [ Job card • Backend @ Stripe ]                         │
//   └──────────────────────────────────────────────────────────┘
//
// Mounting contract (unchanged from 2026-09-14):
//   mountJobs(container)   — main.js calls this when view === 'jobs'
//   unmountJobs()          — main.js calls this on every other view
//
// No hash router; main.js's setView() owns transitions and the
// `data-view` attribute on the root.

import { t } from './i18n.js?v=3';
import { railIcon, RAIL_ICONS, iconLabel } from './util.js?v=18';
import { api } from './api.js?v=27';

// Local state — survives a remount while the view stays on 'jobs'.
const state = {
  sort: 'updated',           // 'updated' | 'salary' | 'posted'
  filterState: '',           // '' | 'yes' | 'no' | 'pending' | 'applied' | 'archived'
  filterTag: '',
  filterRemote: '',
  jobs: [],                  // flat list across every month (single GET)
  months: [],                // month picker options [{year, month, key, label}]
  current: null,             // current month descriptor {year, month, key, label}
  expandedJobs: new Set(),   // job_ids currently expanded inline
  loading: false,
  findOpen: false,           // the "Find jobs" form is expanded
  findStatus: '',            // last find result line ('' = none)
  error: null,
};

function _chipClass(st) {
  // The state chip is colour-coded. CSS provides .job-chip-state--yes etc.
  return `job-chip job-chip-state job-chip-state--${st || 'pending'}`;
}

function _formatSalary(j) {
  if (j.salary_min == null && j.salary_max == null) return '';
  const a = j.salary_min, b = j.salary_max, c = j.salary_currency || 'USD';
  if (a && b) return `${a.toLocaleString()}–${b.toLocaleString()} ${c}`;
  return `${(a || b).toLocaleString()} ${c}`;
}

function _formatAge(j) {
  const last = j.last_seen || j.updated_at || j.created_at;
  if (!last) return '';
  const days = Math.max(0, Math.floor((Date.now() - Date.parse(last)) / 86400000));
  if (days === 0) return t('jobs.age.today');
  if (days === 1) return t('jobs.age.yesterday');
  return t('jobs.age.days_ago', { n: days });
}

// Year+month from an ISO timestamp. Used to group jobs into sections.
function _ym(iso) {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  return { year: d.getUTCFullYear(), month: d.getUTCMonth() + 1,
           key: `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, '0')}` };
}

// Look up a friendly label for a year+month from the months cache;
// falls back to a derived English label when the cache hasn't loaded.
function _monthLabel(year, month) {
  const m = state.months.find((mm) => mm.year === year && mm.month === month);
  if (m) return m.label;
  const names = ['January','February','March','April','May','June',
                 'July','August','September','October','November','December'];
  return `${names[month - 1] || '?'} ${year}`;
}

// Group the flat jobs list into [{year, month, key, label, jobs:[]}, ...]
// newest-month-first. Jobs whose timestamp can't be parsed are dropped
// into a "Unknown" tail bucket so they don't disappear silently.
function _groupByMonth(jobs) {
  const buckets = new Map();
  for (const j of jobs) {
    const ym = _ym(j.created_at || j.posted_at || j.last_seen || j.updated_at);
    if (!ym) continue;
    if (!buckets.has(ym.key)) {
      buckets.set(ym.key, { year: ym.year, month: ym.month, key: ym.key,
                            label: _monthLabel(ym.year, ym.month), jobs: [] });
    }
    buckets.get(ym.key).jobs.push(j);
  }
  return Array.from(buckets.values())
              .sort((a, b) => (b.year - a.year) || (b.month - a.month));
}

// ------------------- DOM builders ------------------------------------- //

function _jobRow(j) {
  const li = document.createElement('li');
  li.className = 'job-row';
  li.dataset.jobId = j.job_id;
  li.tabIndex = 0;
  li.setAttribute('role', 'button');
  li.setAttribute('aria-label', `${j.title || t('jobs.untitled')} — ${j.company || ''}`);
  if (j.effective_state === 'archived' || j.is_expired) {
    li.classList.add('job-row--archived');
  }

  // Bot thumbnail (128×128) next to each row — cheap on the wire, sharp on
  // retina. The full-resolution image is fetched by the modal lightbox; the
  // card list never loads anything bigger than the thumb.
  //
  // Two-stage fallback: thumb route -> full avatar route -> a plain emoji
  // block. Scout (the jobboard bot) has no avatar on a fresh install, so
  // BOTH image routes 404 — without the final stage every row shows a
  // broken-image icon instead of a small letter/emoji tile.
  const thumb = document.createElement('img');
  thumb.className = 'job-row__thumb';
  thumb.alt = '';
  thumb.loading = 'lazy';
  thumb.decoding = 'async';
  thumb.src = '/api/bots/jobboard/avatar/thumb';
  const emojiFallback = () => {
    const block = document.createElement('div');
    block.className = 'job-row__thumb job-row__thumb--fallback';
    block.setAttribute('aria-hidden', 'true');
    block.textContent = '🎯';
    thumb.replaceWith(block);
  };
  // Fall back to the face crop if the thumb route 404s (older install
  // that predates the thumb upload pipeline); if THAT also fails, drop the
  // emoji tile in instead of leaving a broken-image icon.
  thumb.addEventListener('error', () => {
    if (thumb.src.endsWith('/avatar/thumb')) {
      thumb.src = '/api/bots/jobboard/avatar';
    } else {
      emojiFallback();
    }
  });

  const body = document.createElement('div');
  body.className = 'job-row__body';

  const header = document.createElement('div');
  header.className = 'job-row__head';

  const chip = document.createElement('span');
  chip.className = _chipClass(j.effective_state || j.state);
  chip.textContent = t(`jobs.state.${j.effective_state || j.state}`);
  header.appendChild(chip);

  const title = document.createElement('span');
  title.className = 'job-row__title';
  title.textContent = j.title || t('jobs.untitled');
  header.appendChild(title);

  if (j.duplicate_of) {
    const badge = document.createElement('span');
    badge.className = 'job-chip job-chip--dup';
    badge.textContent = t('jobs.badge.duplicate');
    header.appendChild(badge);
  }

  const meta = document.createElement('div');
  meta.className = 'job-row__meta';
  const parts = [];
  if (j.company) parts.push(j.company);
  if (j.location) parts.push(j.location);
  if (j.remote_type && j.remote_type !== 'unknown') parts.push(j.remote_type);
  meta.textContent = parts.join(' · ');

  const salary = document.createElement('div');
  salary.className = 'job-row__salary';
  salary.textContent = _formatSalary(j);

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
  age.textContent = _formatAge(j);
  foot.appendChild(age);

  // Inline chevron hint that this is clickable.
  const open = document.createElement('span');
  open.className = 'job-row__open';
  open.textContent = '›';
  open.setAttribute('aria-hidden', 'true');
  foot.appendChild(open);

  body.appendChild(header);
  if (meta.textContent) body.appendChild(meta);
  if (salary.textContent) body.appendChild(salary);
  if (tags.childNodes.length) body.appendChild(tags);
  body.appendChild(foot);

  li.appendChild(thumb);
  li.appendChild(body);

  // Open the detail modal on click / Enter / Space.
  const openModal = () => {
    if (typeof window.__openJobDetail === 'function') {
      window.__openJobDetail(j.job_id);
    }
  };
  li.addEventListener('click', openModal);
  li.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter' || ev.key === ' ') {
      ev.preventDefault();
      openModal();
    }
  });
  return li;
}

function _monthSection(group) {
  const section = document.createElement('section');
  section.className = 'jobs-month';
  section.dataset.monthKey = group.key;

  const header = document.createElement('header');
  header.className = 'jobs-month__head';

  const label = document.createElement('h2');
  label.className = 'jobs-month__title';
  label.textContent = group.label;
  header.appendChild(label);

  const count = document.createElement('span');
  count.className = 'jobs-month__count';
  count.textContent = group.jobs.length === 1
    ? t('jobs.month.count', { n: group.jobs.length })
    : t('jobs.month.count_plural', { n: group.jobs.length });
  header.appendChild(count);

  section.appendChild(header);

  const list = document.createElement('ul');
  list.className = 'jobs-list';
  for (const j of group.jobs) list.appendChild(_jobRow(j));
  section.appendChild(list);

  return section;
}

// The toolbar is built ONCE, at mount, and never rebuilt.
//
// It used to be recreated inside render(), which ran on every filter change,
// every sort change and every incoming job. Changing a <select> therefore
// destroyed the <select> you had just used: focus was lost, and a caret in the
// tag box went with it. A control that deletes itself when you touch it is the
// single biggest reason this board felt unresponsive.
//
// `onChange` is called after a control updates `state`. It never fetches —
// every filter and sort below is a pure operation on `state.jobs`, which
// already holds the whole list.
function _buildToolbar(onChange) {
  const bar = document.createElement('div');
  bar.className = 'jobs-toolbar';

  const select = (cls, values, labelFor, current, onPick) => {
    const sel = document.createElement('select');
    sel.className = cls;
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

  // Sort
  const sortLbl = document.createElement('label');
  sortLbl.className = 'jobs-toolbar__field';
  sortLbl.textContent = t('jobs.sort.label') + ': ';
  const sortLabels = {
    updated: t('jobs.sort.updated'),
    salary: t('jobs.sort.salary'),
    posted: t('jobs.sort.posted'),
  };
  sortLbl.appendChild(select(
    'jobs-toolbar__sort', Object.keys(sortLabels), (v) => sortLabels[v], state.sort,
    (v) => { state.sort = v; },
  ));
  bar.appendChild(sortLbl);

  // State filter
  bar.appendChild(select(
    'jobs-toolbar__state',
    ['', 'pending', 'yes', 'no', 'maybe', 'applied', 'archived'],
    (v) => (v ? t(`jobs.state.${v}`) : t('jobs.filter.all')),
    state.filterState,
    (v) => { state.filterState = v; },
  ));

  // Remote filter
  bar.appendChild(select(
    'jobs-toolbar__remote',
    ['', 'remote', 'hybrid', 'onsite'],
    (v) => (v ? t(`jobs.remote.${v}`) : t('jobs.filter.any_remote')),
    state.filterRemote,
    (v) => { state.filterRemote = v; },
  ));

  // Tag filter. Filters as you type against the in-memory list — there is no
  // request behind it, so the only reason to debounce is to avoid rebuilding
  // the list on every keystroke in a long board.
  const tagInput = document.createElement('input');
  tagInput.type = 'text';
  tagInput.className = 'jobs-toolbar__tag';
  tagInput.placeholder = t('jobs.filter.tag_placeholder');
  tagInput.value = state.filterTag;
  tagInput.setAttribute('aria-label', t('jobs.filter.tag_placeholder'));
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

  // A quiet spinner that lives in the toolbar rather than replacing the list.
  // The board used to swap its entire contents for the word "Loading" on every
  // refresh, so the rows you were reading vanished and came back.
  const busy = document.createElement('span');
  busy.className = 'jobs-toolbar__busy';
  busy.hidden = true;
  busy.setAttribute('role', 'status');
  busy.setAttribute('aria-live', 'polite');
  busy.textContent = t('jobs.loading');
  bar.appendChild(busy);

  // Hand the search to the board's agent (Scout). New postings arrive over
  // the WebSocket (job_created) and refresh the list on their own.
  const findBtn = document.createElement('button');
  findBtn.type = 'button';
  findBtn.className = 'btn-primary jobs-find-btn';
  findBtn.append(...iconLabel(RAIL_ICONS.search, t('jobs.find.button')));
  findBtn.setAttribute('aria-expanded', String(state.findOpen));
  findBtn.addEventListener('click', () => {
    state.findOpen = !state.findOpen;
    findBtn.setAttribute('aria-expanded', String(state.findOpen));
    _renderFindForm();
    if (state.findOpen) document.querySelector('.jobs-find__input')?.focus();
  });
  bar.appendChild(findBtn);

  bar._busy = busy;
  bar._findBtn = findBtn;
  return bar;
}

function _buildFindForm() {
  const form = document.createElement('form');
  form.className = 'jobs-find';
  const input = document.createElement('input');
  input.type = 'text';
  input.className = 'jobs-find__input';
  input.maxLength = 1000;
  input.placeholder = t('jobs.find.placeholder');
  input.setAttribute('aria-label', t('jobs.find.placeholder'));
  const send = document.createElement('button');
  send.type = 'submit';
  send.className = 'btn-primary';
  send.textContent = t('jobs.find.send');
  const cancel = document.createElement('button');
  cancel.type = 'button';
  cancel.className = 'btn-secondary';
  cancel.textContent = t('jobs.vote.cancel');
  cancel.addEventListener('click', () => {
    state.findOpen = false;
    _renderFindForm();
    // The button owns the expanded state it announces, so it has to be told.
    const btn = hosts.toolbar && hosts.toolbar._findBtn;
    if (btn) { btn.setAttribute('aria-expanded', 'false'); btn.focus(); }
  });
  form.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    send.disabled = true;
    try {
      const r = await api.jobs.find({ query: input.value.trim() || null });
      state.findStatus = r && r.dispatched ? t('jobs.find.sent') : t('jobs.find.not_sent');
      state.findOpen = false;
    } catch (e) {
      state.findStatus = t('jobs.error', { msg: e.message || String(e) });
      send.disabled = false;
    }
    // Only the find form and its status line changed; the list is untouched,
    // and the new postings arrive on their own over the WebSocket.
    _renderFindForm();
    _renderStatus();
    const btn = hosts.toolbar && hosts.toolbar._findBtn;
    if (btn) btn.setAttribute('aria-expanded', String(state.findOpen));
  });
  form.append(input, send, cancel);
  return form;
}

// ------------------- the board's own DOM ------------------------------ //
//
// Four stable hosts, created once at mount. render() only ever replaces the
// contents of `list`; the toolbar, the find form and the status line keep
// their identity (and their focus) across every update.
const hosts = { root: null, toolbar: null, find: null, status: null, list: null };

let inflight = null;      // AbortController for the one request we care about
let loadedOnce = false;   // first paint shows a skeleton; later ones do not

function _setBusy(on) {
  const busy = hosts.toolbar && hosts.toolbar._busy;
  if (busy) busy.hidden = !on;
}

function _renderFindForm() {
  if (!hosts.find) return;
  hosts.find.replaceChildren();
  if (state.findOpen) hosts.find.appendChild(_buildFindForm());
}

function _renderStatus() {
  if (!hosts.status) return;
  hosts.status.replaceChildren();
  // The error is a BANNER, not a replacement for the board.
  //
  // It used to be rendered into hosts.list with an early return, which threw
  // away every row on any failed refresh — so one dropped request emptied a
  // board the operator was reading, and because state.error was only cleared
  // by a later SUCCESSFUL refresh (and nothing triggered one), every
  // subsequent filter change repainted the error over the rows that were
  // still sitting in state.jobs.
  if (state.error) {
    const err = document.createElement('p');
    err.className = 'jobs-find__status jobs-empty--error';
    err.setAttribute('role', 'alert');
    err.textContent = t('jobs.error', { msg: state.error });
    hosts.status.appendChild(err);
  }
  if (!state.findStatus) return;
  const note = document.createElement('p');
  note.className = 'jobs-find__status';
  note.setAttribute('role', 'status');
  note.textContent = state.findStatus;
  hosts.status.appendChild(note);
}

/** The jobs to show, given the current filters and sort.
 *
 *  Pure, and deliberately the ONLY place filtering happens. The board used to
 *  ALSO send these same filters to the server as query params and re-fetch on
 *  every change — a round-trip whose answer this function then reproduced from
 *  data already in memory.
 */
function visibleJobs() {
  let out = state.jobs;
  if (state.filterState) out = out.filter((j) => j.effective_state === state.filterState);
  if (state.filterRemote) out = out.filter((j) => j.remote_type === state.filterRemote);
  if (state.filterTag) out = out.filter((j) => (j.tags || []).includes(state.filterTag));
  const by = {
    salary: (a, b) => (b.salary_max || b.salary_min || 0) - (a.salary_max || a.salary_min || 0),
    posted: (a, b) => (b.posted_at || '').localeCompare(a.posted_at || ''),
    updated: (a, b) => (b.updated_at || '').localeCompare(a.updated_at || ''),
  };
  return [...out].sort(by[state.sort] || by.updated);
}

/** Repaint the list — and ONLY the list. */
function render() {
  if (!hosts.list) return;

  if (!loadedOnce && state.loading) {
    // Only the FIRST load gets a placeholder. After that the previous rows
    // stay on screen while a refresh runs, and the toolbar spinner is the
    // only sign anything is happening.
    const skel = document.createElement('p');
    skel.className = 'jobs-empty';
    skel.textContent = t('jobs.loading');
    hosts.list.replaceChildren(skel);
    return;
  }

  const visible = visibleJobs();
  if (!visible.length) {
    const empty = document.createElement('p');
    empty.className = 'jobs-empty';
    empty.textContent = t('jobs.empty');
    hosts.list.replaceChildren(empty);
    return;
  }

  // Group into month sections. Months are NOT a chat — they're a
  // grouping label on the structured list, the way Reddit's subreddit
  // list groups by topic or a Gmail inbox groups by date.
  const frag = document.createDocumentFragment();
  for (const g of _groupByMonth(visible)) frag.appendChild(_monthSection(g));
  hosts.list.replaceChildren(frag);
}

/** Apply a job the server has already sent us, without asking for it again.
 *
 *  Returns true when the update was applied locally (no request needed) and
 *  false ONLY for a job this board has never seen — which is the one case
 *  where a re-fetch actually tells us something new.
 *
 *  This used to bail whenever the update changed the visible set or its order,
 *  which sounded conservative and in practice meant the fast path almost never
 *  ran: every vote bumps `updated_at` server-side (database.py's
 *  update_job_state), the default sort IS `updated`, so the voted job jumps to
 *  the top and the order always differed. One vote therefore still cost a full
 *  board re-fetch — the thing this path exists to avoid. Repainting the list
 *  from `state.jobs` is cheap and always correct, so reordering is no longer a
 *  reason to go to the network.
 */
function patchJob(job) {
  if (!job || !job.job_id) return false;
  const i = state.jobs.findIndex((j) => j.job_id === job.job_id);
  if (i === -1) return false;          // never seen it — a refresh is the answer

  const before = visibleJobs();
  state.jobs = state.jobs.slice();
  state.jobs[i] = job;
  const after = visibleJobs();

  // Same rows in the same order: swap the one node and leave the rest of the
  // DOM (and the reader's scroll position) alone.
  const sameSet = before.length === after.length
    && !before.some((j, n) => j.job_id !== after[n].job_id);
  if (sameSet) {
    // Found by walking the rows rather than by building an attribute selector.
    // A job id is server-generated, but interpolating one into a selector
    // still needs CSS.escape — which does not exist in every environment this
    // module is exercised in, and a ReferenceError here would take the whole
    // listener down and skip the refresh fallback too. Comparing dataset
    // values needs no escaping and cannot throw.
    const rows = hosts.list ? hosts.list.querySelectorAll('.job-row') : [];
    for (const el of rows) {
      if (el.dataset.jobId !== job.job_id) continue;
      el.replaceWith(_jobRow(job));
      return true;
    }
  }

  // The set or the order moved (a vote bumps updated_at, or a filter no longer
  // matches): repaint the list from memory. Still no request.
  render();
  return true;
}

/** Fetch the board. Filters are NOT sent — they are applied locally. */
async function refresh() {
  // A superseded request must never win. Without this, two refreshes in quick
  // succession resolve in arrival order, so a slow earlier response can
  // overwrite a newer one.
  if (inflight) inflight.abort();
  const ctl = new AbortController();
  inflight = ctl;

  state.loading = true;
  // Clear the previous failure as the retry begins: leaving it up until a
  // SUCCESS arrives meant a single dropped request showed an error banner for
  // the rest of the session if nothing else triggered a refresh.
  state.error = null;
  _renderStatus();
  _setBusy(true);
  if (!loadedOnce) render();

  try {
    const [listResp, monthsResp] = await Promise.all([
      api.jobs.list(new URLSearchParams(), { signal: ctl.signal }),
      api.jobs.months({ signal: ctl.signal }).catch(() => ({ months: [], current: null })),
    ]);
    if (ctl.signal.aborted) return;
    state.jobs = listResp.jobs || [];
    state.months = monthsResp.months || [];
    state.current = monthsResp.current || null;
    state.error = null;
    loadedOnce = true;
  } catch (e) {
    if (ctl.signal.aborted || e.name === 'AbortError') return;
    state.error = e.message || String(e);
    _renderStatus();
    // Keep whatever is already on screen: a transient failure should not
    // empty a board the operator is reading. The error line renders above it.
    if (!loadedOnce) state.jobs = [];
  } finally {
    if (inflight === ctl) {
      inflight = null;
      state.loading = false;
      _setBusy(false);
    }
  }
  render();
}

// The detail modal (job-thread.js) fires this after a vote/applied/undo/
// feedback so a change made in the panel is reflected in the list behind
// it — without it, voting "No" left a job sitting under "Pending" in the
// board until the next manual refresh. A CustomEvent (rather than a direct
// import) keeps job-thread.js and jobs.js decoupled — the board doesn't
// need to exist yet, or ever, for the modal to work standalone.
//
// The same event carries the WebSocket's `job_updated` payload, so the common
// case patches one row and makes no request at all.
const JOBS_CHANGED_EVENT = 'dispatch:jobs-changed';
function _onJobsChanged(ev) {
  const job = ev && ev.detail && ev.detail.job;
  // A patch applied while a refresh is in flight would be silently reverted
  // when that older response lands and overwrites state.jobs. Restart from a
  // fresh snapshot instead — the in-flight request is superseded and aborted
  // by refresh() itself.
  if (inflight) { refresh(); return; }
  // A patch that throws must still leave the board correct. Falling through to
  // refresh() is the safe answer to every failure here; swallowing the error
  // and doing nothing would leave a stale row on screen with no sign of it.
  try {
    if (job && patchJob(job)) return;
  } catch { /* fall through to a full refresh */ }
  refresh();
}

export async function mountJobs(container) {
  let root = container.querySelector('[data-jobs-root]');
  if (!root) {
    root = document.createElement('div');
    root.dataset.jobsRoot = '1';
    container.appendChild(root);
  }
  // Idempotent: a second mountJobs() without an intervening unmount (the
  // "reuses an existing root" case the tests pin) must not double-subscribe,
  // nor stack a second toolbar on top of the first.
  document.removeEventListener(JOBS_CHANGED_EVENT, _onJobsChanged);
  document.addEventListener(JOBS_CHANGED_EVENT, _onJobsChanged);

  root.replaceChildren();
  hosts.root = root;
  // Sorting and filtering are local, so a toolbar change repaints and stops.
  hosts.toolbar = _buildToolbar(() => render());
  hosts.find = document.createElement('div');
  hosts.status = document.createElement('div');
  hosts.list = document.createElement('div');
  hosts.list.className = 'jobs-list-host';
  root.append(hosts.toolbar, hosts.find, hosts.status, hosts.list);

  loadedOnce = false;
  _renderFindForm();
  _renderStatus();
  await refresh();
}

export function unmountJobs() {
  // Tear down the DOM root, not just the cached state — otherwise the
  // toolbar stays painted into the chat panel when the user opens a
  // job (the toolbar container is appended to the same #chat host the
  // messages list uses, so without removing it the next view stacks
  // on top of stale chrome).
  document.removeEventListener(JOBS_CHANGED_EVENT, _onJobsChanged);
  // Drop any request still in flight. Without this its `finally` runs against
  // a board that no longer exists, and a late response repaints a torn-down
  // host on the way past.
  if (inflight) { inflight.abort(); inflight = null; }
  const root = document.querySelector('[data-jobs-root]');
  if (root) root.remove();
  hosts.root = hosts.toolbar = hosts.find = hosts.status = hosts.list = null;
  loadedOnce = false;
  state.jobs = [];
  state.error = null;
  state.loading = false;
  state.expandedJobs.clear();
}
