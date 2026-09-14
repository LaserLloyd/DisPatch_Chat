// Jobs board — the list view. Mounts into the chat area when
// `state.view === 'jobs'`. No hash router; main.js's setView() owns
// transitions and the `data-view` attribute on the root.
//
// Exports: mountJobs(container), unmountJobs(). The contract mirrors
// mountDashboard (main.js:24, 2905): main.js calls mountJobs in the
// `view === 'jobs'` branch and unmountJobs on every other view switch.

import { t } from './i18n.js?v=3';
import { api } from './api.js?v=22';

// Local state — survives a remount while the view stays on 'jobs'.
const state = {
  sort: 'updated',   // 'updated' | 'salary' | 'posted'
  filterState: '',   // '' | 'yes' | 'no' | 'pending' | 'applied' | 'archived'
  filterTag: '',
  filterRemote: '',
  jobs: [],
  loading: false,
  error: null,
};

const SORT_LABELS = {
  updated: t('jobs.sort.updated'),
  salary: t('jobs.sort.salary'),
  posted: t('jobs.sort.posted'),
};

function _chipClass(state) {
  // The state chip is colour-coded. CSS provides .job-chip-state--yes etc;
  // the brand colours pick up from the existing palette.
  return `job-chip job-chip-state job-chip-state--${state || 'pending'}`;
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

function _jobRow(j) {
  const li = document.createElement('li');
  li.className = 'job-row';
  li.dataset.threadId = j.thread_id;
  if (j.effective_state === 'archived' || j.is_expired) li.classList.add('job-row--archived');

  const header = document.createElement('div');
  header.className = 'job-row__head';

  const chip = document.createElement('span');
  chip.className = _chipClass(j.effective_state || j.state);
  chip.textContent = t(`jobs.state.${j.effective_state || j.state}`);
  header.appendChild(chip);

  const title = document.createElement('a');
  title.className = 'job-row__title';
  title.href = `#${j.thread_id}`;
  title.textContent = j.title;
  title.addEventListener('click', (ev) => {
    ev.preventDefault();
    // Tear down the board view BEFORE opening the thread so the chat
    // panel swaps back to the messages list (the board toolbar was
    // painted into #chat, not a sibling — without unmountJobs it stays
    // on screen under the job card).
    if (typeof window.__setView === 'function') window.__setView('chat');
    else if (typeof window.unmountJobs === 'function') window.unmountJobs();
    // Defer to the main.js thread-open path so the composer / mobile
    // tabs sync exactly as for any other thread. Pass botId='jobboard' so
    // openThread() can stub state.activeThread synchronously — without
    // that hint the thread isn't in state.threads (selectBot was never
    // called for jobboard), state.activeThread stays null, and the job
    // card never auto-mounts in renderMessages().
    if (window.__openThread) window.__openThread(j.thread_id, { botId: 'jobboard' });
  });
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
    const t = document.createElement('span');
    t.className = 'job-chip';
    t.textContent = tag;
    tags.appendChild(t);
  }

  const foot = document.createElement('div');
  foot.className = 'job-row__foot';
  const age = document.createElement('span');
  age.textContent = _formatAge(j);
  foot.appendChild(age);

  li.appendChild(header);
  if (meta.textContent) li.appendChild(meta);
  if (salary.textContent) li.appendChild(salary);
  if (tags.childNodes.length) li.appendChild(tags);
  li.appendChild(foot);
  return li;
}

function _buildToolbar() {
  const bar = document.createElement('div');
  bar.className = 'jobs-toolbar';

  // SORT_LABELS is intentionally NOT cached at module top-level — t() must
  // run after i18n.init() resolves the dictionary (see main.js's init
  // ordering). Computing it here makes every render see fresh strings.

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

  // Tag filter (free text — chip into URL later)
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
    const r = await api.jobs.list(params);
    state.jobs = r.jobs || [];
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

  const list = document.createElement('ul');
  list.className = 'jobs-list';
  for (const j of visible) list.appendChild(_jobRow(j));
  root.appendChild(list);
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
}
