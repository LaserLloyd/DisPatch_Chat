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
import { api } from './api.js?v=22';

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
  const thumb = document.createElement('img');
  thumb.className = 'job-row__thumb';
  thumb.alt = '';
  thumb.loading = 'lazy';
  thumb.decoding = 'async';
  thumb.src = '/api/bots/jobboard/avatar/thumb';
  // Fall back to the face crop if the thumb route 404s (older install
  // that predates the thumb upload pipeline).
  thumb.addEventListener('error', () => {
    if (thumb.src !== '/api/bots/jobboard/avatar') {
      thumb.src = '/api/bots/jobboard/avatar';
    }
  }, { once: true });

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

function _buildToolbar() {
  const bar = document.createElement('div');
  bar.className = 'jobs-toolbar';

  // Sort
  const sortLbl = document.createElement('label');
  sortLbl.textContent = t('jobs.sort.label') + ': ';
  const sortSel = document.createElement('select');
  const sortLabels = {
    updated: t('jobs.sort.updated'),
    salary: t('jobs.sort.salary'),
    posted: t('jobs.sort.posted'),
  };
  for (const [k, label] of Object.entries(sortLabels)) {
    const o = document.createElement('option');
    o.value = k; o.textContent = label;
    sortSel.appendChild(o);
  }
  sortSel.value = state.sort;
  sortSel.addEventListener('change', () => {
    state.sort = sortSel.value;
    refresh();
  });
  sortLbl.appendChild(sortSel);
  bar.appendChild(sortLbl);

  // State filter
  const stateSel = document.createElement('select');
  for (const s of ['', 'pending', 'yes', 'no', 'maybe', 'applied', 'archived']) {
    const o = document.createElement('option');
    o.value = s;
    o.textContent = s ? t(`jobs.state.${s}`) : t('jobs.filter.all');
    stateSel.appendChild(o);
  }
  stateSel.value = state.filterState;
  stateSel.addEventListener('change', () => {
    state.filterState = stateSel.value;
    refresh();
  });
  bar.appendChild(stateSel);

  // Remote filter
  const remoteSel = document.createElement('select');
  for (const r of ['', 'remote', 'hybrid', 'onsite']) {
    const o = document.createElement('option');
    o.value = r;
    o.textContent = r ? t(`jobs.remote.${r}`) : t('jobs.filter.any_remote');
    remoteSel.appendChild(o);
  }
  remoteSel.value = state.filterRemote;
  remoteSel.addEventListener('change', () => {
    state.filterRemote = remoteSel.value;
    refresh();
  });
  bar.appendChild(remoteSel);

  // Tag filter (free text)
  const tagInput = document.createElement('input');
  tagInput.type = 'text';
  tagInput.placeholder = t('jobs.filter.tag_placeholder');
  tagInput.value = state.filterTag;
  tagInput.addEventListener('change', () => {
    state.filterTag = tagInput.value.trim().toLowerCase();
    refresh();
  });
  bar.appendChild(tagInput);

  return bar;
}

async function refresh() {
  state.loading = true;
  try {
    const params = new URLSearchParams();
    if (state.filterState) params.set('state', state.filterState);
    if (state.filterRemote) params.set('remote_type', state.filterRemote);
    if (state.filterTag) params.set('tag', state.filterTag);
    if (state.sort === 'salary') params.set('sort', 'salary');
    else if (state.sort === 'posted') params.set('sort', 'posted');
    const [listResp, monthsResp] = await Promise.all([
      api.jobs.list(params),
      api.jobs.months().catch(() => ({ months: [], current: null })),
    ]);
    state.jobs = listResp.jobs || [];
    state.months = monthsResp.months || [];
    state.current = monthsResp.current || null;
    state.error = null;
  } catch (e) {
    state.error = e.message || String(e);
    state.jobs = [];
  } finally {
    state.loading = false;
  }
  render();
}

function render() {
  const root = document.querySelector('[data-jobs-root]');
  if (!root) return;
  root.replaceChildren();

  root.appendChild(_buildToolbar());

  if (state.loading) {
    const loading = document.createElement('p');
    loading.className = 'jobs-empty';
    loading.textContent = t('jobs.loading');
    root.appendChild(loading);
    return;
  }
  if (state.error) {
    const err = document.createElement('p');
    err.className = 'jobs-empty jobs-empty--error';
    err.textContent = t('jobs.error', { msg: state.error });
    root.appendChild(err);
    return;
  }

  let visible = state.jobs;
  if (state.filterState) visible = visible.filter((j) => j.effective_state === state.filterState);
  if (state.filterRemote) visible = visible.filter((j) => j.remote_type === state.filterRemote);
  if (state.filterTag) visible = visible.filter((j) => (j.tags || []).includes(state.filterTag));
  if (state.sort === 'salary') {
    visible = [...visible].sort((a, b) => (b.salary_max || b.salary_min || 0) - (a.salary_max || a.salary_min || 0));
  } else if (state.sort === 'posted') {
    visible = [...visible].sort((a, b) => (b.posted_at || '').localeCompare(a.posted_at || ''));
  } else {
    visible = [...visible].sort((a, b) => (b.updated_at || '').localeCompare(a.updated_at || ''));
  }

  if (!visible.length) {
    const empty = document.createElement('p');
    empty.className = 'jobs-empty';
    empty.textContent = t('jobs.empty');
    root.appendChild(empty);
    return;
  }

  // Group into month sections. Months are NOT a chat — they're a
  // grouping label on the structured list, the way Reddit's subreddit
  // list groups by topic or a Gmail inbox groups by date.
  const groups = _groupByMonth(visible);
  for (const g of groups) root.appendChild(_monthSection(g));
}

export async function mountJobs(container) {
  let root = container.querySelector('[data-jobs-root]');
  if (!root) {
    root = document.createElement('div');
    root.dataset.jobsRoot = '1';
    container.appendChild(root);
  }
  await refresh();
}

export function unmountJobs() {
  // Tear down the DOM root, not just the cached state — otherwise the
  // toolbar stays painted into the chat panel when the user opens a
  // job (the toolbar container is appended to the same #chat host the
  // messages list uses, so without removing it the next view stacks
  // on top of stale chrome).
  const root = document.querySelector('[data-jobs-root]');
  if (root) root.remove();
  state.jobs = [];
  state.error = null;
  state.expandedJobs.clear();
}
