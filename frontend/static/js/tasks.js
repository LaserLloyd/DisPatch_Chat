// Tasks: assigning work to an agent, and watching it.
//
// What this module owns
//   * the Tasks page (a dialog opened from the rail): filters (state, agent, person, date), a row
//     per task, and the live task card under a row when you open it;
//   * "Assign task": a dialog reachable from the thread menu, and by typing `@agent ...` at the
//     start of a message - the mention opens the same dialog, filled in, and nothing runs until
//     you press Assign;
//   * the inline task card that rides in a chat (a message whose server-stamped metadata carries
//     `task_card`), which is the same card as on the Tasks page;
//   * routing the WebSocket `task.*` frames to every card and row that shows that task, and the
//     "approvals waiting" badge on the rail button.
//
// What it does not own: who may do what. The server decides (manage_agents / dispatch_agents /
// approve_agent_actions, route_policy.py); this module only decides what to OFFER, from
// GET /api/auth/me. A profile without those permissions is offered none of it, and every call
// it could make would answer 403 anyway.
//
// Wording: UI verbs are "Assign task" / "Send to agent" (the brief); the backend is "OpenClaw" or
// "Claude Agent" as the server names it; costs are always "estimate".

import { api as realApi } from './api.js?v=37';
import { t, relTime } from './i18n.js?v=5';
import { el, errorText, spriteIcon } from './util.js?v=23';
import { createTaskCard, stateLabel, costLabel } from './task_card.js?v=4';
import { agentApi } from './agents.js?v=6';

export const FILTER_STATES = ['', 'queued', 'running', 'awaiting_approval', 'succeeded', 'failed', 'cancelled'];
const PROMPT_MAX = 20000;

// ----------------------------------------------------------------------------- pure helpers

/** "Code Reviewer" -> "code-reviewer": what an @mention matches against. */
export function slug(name) {
  return String(name || '').toLowerCase().normalize('NFC').replace(/[^\p{L}\p{N}\p{M}]+/gu, '-').replace(/^-+|-+$/g, '');
}

/**
 * `@agent do the thing` -> {agent, prompt}, when `agent` names one of `agents` (by slug, so
 * `@code-reviewer` finds "Code Reviewer"). Null when the text is not a mention of a known agent or
 * has nothing after it. A mention of an unknown name is ordinary chat, not an error.
 */
export function parseMention(text, agents) {
  const m = /^\s*@([\p{L}\p{N}._-]+)\s+([\s\S]+)$/u.exec(String(text || ''));
  if (!m) return null;
  const want = slug(m[1]);
  const agent = (agents || []).find((a) => a && a.available !== false && slug(a.name) === want);
  const prompt = m[2].trim();
  return agent && prompt ? { agent, prompt } : null;
}

/** The query the Tasks page sends: empty filters are left out. */
export function listQuery(f) {
  const q = {};
  if (f.state) q.state = f.state;
  if (f.agent) q.agent_id = f.agent;
  if (f.profile) q.profile = f.profile;
  if (f.since) q.since = f.since;
  q.limit = f.limit || 100;
  return q;
}

/** What the person may be offered, from /api/auth/me. Since 4.0 the roles are admin / user /
 *  basic; the Owner is the protected Admin (`me.protected`), not a role of its own, so there is
 *  no 'owner' role to test for. */
export function accessFrom(me) {
  const perms = Array.isArray(me && me.permissions) ? me.permissions : [];
  const all = perms.includes('*');
  const has = (p) => all || perms.includes(p);
  const role = me && me.role;
  return {
    dispatch: !!me && has('dispatch_agents'),
    approve: !!me && has('approve_agent_actions'),
    manage: !!me && has('manage_agents'),
    operator: !!me && (all || role === 'admin'),
  };
}

export const NO_ACCESS = Object.freeze({ dispatch: false, approve: false, manage: false, operator: false });

// ----------------------------------------------------------------------------- schedules (4.3 #13)

export const DAYS = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'];
const DAY_KEYS = {
  mon: 'sched.day_mon', tue: 'sched.day_tue', wed: 'sched.day_wed', thu: 'sched.day_thu',
  fri: 'sched.day_fri', sat: 'sched.day_sat', sun: 'sched.day_sun',
};
export const dayLabel = (d) => (DAY_KEYS[d] ? t(DAY_KEYS[d]) : String(d || ''));
export const EVERY_MIN = 15;

/** This browser's time zone: a schedule's daily/weekly times are wall-clock times here. */
export function localZone() {
  try { return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'; } catch { return 'UTC'; }
}

/** The editor's fields -> the API rule, or {error: message}. */
export function scheduleRule(f) {
  if (f.repeat === 'every') {
    const n = Number(f.every);
    if (!Number.isInteger(n) || n < EVERY_MIN) return { error: t('sched.err_every', { min: EVERY_MIN }) };
    return { rule: { every_minutes: n } };
  }
  const at = String(f.at || '').trim();
  if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(at)) return { error: t('sched.err_time') };
  if (f.repeat === 'daily') return { rule: { daily_at: at } };
  if (f.repeat === 'weekly') {
    if (!DAYS.includes(f.day)) return { error: t('sched.err_day') };
    return { rule: { weekly_day_at: { day: f.day, at } } };
  }
  return { error: t('sched.err_repeat') };
}

/** A stored rule -> the editor's fields. */
export function ruleToForm(rule) {
  const r = rule || {};
  if (r.every_minutes) return { repeat: 'every', every: String(r.every_minutes), at: '09:00', day: 'mon' };
  if (r.daily_at) return { repeat: 'daily', every: '60', at: r.daily_at, day: 'mon' };
  if (r.weekly_day_at) return { repeat: 'weekly', every: '60', at: r.weekly_day_at.at, day: r.weekly_day_at.day };
  return { repeat: 'daily', every: '60', at: '09:00', day: 'mon' };
}

/** "Every 30 minutes" / "Every day at 09:00" / "Every Monday at 09:00". */
export function ruleText(rule) {
  const r = rule || {};
  if (r.every_minutes) return t('sched.rule_every', { count: r.every_minutes });
  if (r.daily_at) return t('sched.rule_daily', { at: r.daily_at });
  if (r.weekly_day_at) return t('sched.rule_weekly', { day: dayLabel(r.weekly_day_at.day), at: r.weekly_day_at.at });
  return '';
}

/** The next `n` run times of `rule` after `now`, on THIS browser's clock (the preview; the server
 *  computes the real ones in the schedule's zone, which is this browser's when it was saved). */
export function previewRuns(rule, now = new Date(), n = 3) {
  const out = [];
  if (!rule) return out;
  if (rule.every_minutes) {
    for (let i = 1; i <= n; i++) out.push(new Date(now.getTime() + i * rule.every_minutes * 60000));
    return out;
  }
  const spec = rule.daily_at ? { at: rule.daily_at, day: null } : rule.weekly_day_at ? { at: rule.weekly_day_at.at, day: rule.weekly_day_at.day } : null;
  if (!spec) return out;
  const [hh, mm] = spec.at.split(':').map(Number);
  const want = spec.day == null ? null : (DAYS.indexOf(spec.day) + 1) % 7;   // JS: 0 = Sunday
  const d = new Date(now.getFullYear(), now.getMonth(), now.getDate(), hh, mm, 0, 0);
  for (let i = 0; out.length < n && i < 400; i++) {
    const c = new Date(d.getFullYear(), d.getMonth(), d.getDate() + i, hh, mm, 0, 0);
    if (c <= now) continue;
    if (want != null && c.getDay() !== want) continue;
    out.push(c);
  }
  return out;
}

/** A date for people: "Mon 3 Nov, 09:00" in the browser's language. */
export function whenText(d) {
  const date = d instanceof Date ? d : new Date(d);
  if (Number.isNaN(date.getTime())) return '';
  try {
    return date.toLocaleString(undefined, { weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
  } catch { return date.toISOString(); }
}

/** POST/PATCH body from the editor, or {error}. */
export function scheduleBody(f, tz = localZone()) {
  const r = scheduleRule(f);
  if (r.error) return r;
  const prompt = String(f.prompt || '').trim();
  if (!f.agent_id) return { error: t('sched.err_agent') };
  if (!prompt) return { error: t('sched.err_prompt') };
  // The chat the results are posted in (one of the caller's own; the server checks that too).
  // null = no chat: the result stays on the Tasks page only.
  const body = { agent_id: f.agent_id, prompt, rule: r.rule, tz, thread_id: f.thread_id ? String(f.thread_id) : null };
  if (f.enabled === false) body.enabled = false;
  return body;
}

/** The caller's own chats for "Post results in", newest first. GET /api/threads without a
 *  bot_id is the server's own-chats listing (it never returns another profile's chat, nor one
 *  whose character the caller may no longer use). Fails soft: no list = "no chat" only. */
export async function ownChats(api) {
  let res = null;
  try {
    if (api && typeof api.ownThreads === 'function') res = await api.ownThreads();
    else {
      const r = await fetch('/api/threads?limit=200', { credentials: 'same-origin' });
      res = r && r.ok ? await r.json() : null;
    }
  } catch { res = null; }
  const rows = (res && Array.isArray(res.threads) ? res.threads : []).filter((th) => th && th.id && !th.is_archived);
  return rows.slice().sort((a, b) => String(b.updated_at || '').localeCompare(String(a.updated_at || '')));
}

/** "Bits · Morning notes" (the character, then the chat's title). */
export function chatLabel(th, botNames = {}) {
  const title = String((th && th.title) || '').trim() || t('sched.chat_untitled');
  const bot = botNames[th && th.bot_id] || '';
  return bot ? t('sched.chat_option', { bot, title }) : title;
}


// ----------------------------------------------------------------------------- the module

/**
 * deps: { api?, toast, confirm, announce, sessionKey, navButton, onAssigned(task, {fromComposer}),
 *         currentThreadId?() (the chat open now: a new schedule's default "Post results in") }
 */
export function createTasksUi(deps = {}) {
  const api = deps.api || realApi;
  const extra = agentApi(api);
  let access = NO_ACCESS;
  let approvalTimeoutS = 0;                // known to profiles that read the agent settings
  let schedBackdrop = null;
  let agents = [];                         // agents published to me (api.agentsAvailable)
  let backdrop = null;
  let listEl = null;
  let emptyEl = null;
  let filterForm = null;
  let lastFocus = null;
  let assignBackdrop = null;
  const cards = new Map();                 // task id -> Set(card controller)
  const rows = new Map();                  // task id -> row controller
  const expanded = new Set();              // task ids whose row is open, kept across list reloads
  const inline = new Map();                // task id -> {el, card}
  let waiting = 0;                         // tasks awaiting approval (for the badge)
  let reloadTimer = 0;
  const filters = { state: '', agent: '', profile: '', since: '' };
  let people = [];

  const toast = (m, bad) => (deps.toast ? deps.toast(m, bad) : undefined);
  const cardDeps = (extra = {}) => ({
    api, canApprove: access.approve, toast, confirm: deps.confirm, announce: deps.announce,
    showOwner: access.operator, approvalTimeoutS: () => approvalTimeoutS, ...extra,
  });

  function register(card) {
    const id = card.taskId;
    if (!cards.has(id)) cards.set(id, new Set());
    cards.get(id).add(card);
  }

  async function mountCard(host, row, extra) {
    const card = createTaskCard(cardDeps(extra));
    host.append(card.el);
    try {
      const [detail, evs] = await Promise.all([api.task(row.id), api.taskEvents(row.id, -1)]);
      card.load(detail.task, evs.events || [], detail.approvals || []);
      register(card);
    } catch (e) {
      host.replaceChildren(el('p', { class: 'muted task-gone', text: t('tasks.not_available') }));
    }
    return card;
  }

  // ---- access, nav button, badge ----

  async function refreshAccess() {
    // The app calls this whenever the sign-in state may have changed; asking the server on every
    // call would be noise, so a burst within a few seconds is answered from the last answer.
    const key = deps.sessionKey ? deps.sessionKey() : '';
    const now = Date.now();
    if (refreshAccess.last && refreshAccess.key === key && now - refreshAccess.last < 3000) return access;
    refreshAccess.last = now;
    if (refreshAccess.key !== undefined && refreshAccess.key !== key) resetForNewSession();
    refreshAccess.key = key;
    try {
      access = accessFrom(await api.me());
    } catch {
      access = NO_ACCESS;
    }
    if (access.dispatch) {
      try {
        const res = await api.agentsAvailable();
        agents = res.agents || [];
        const waitingRes = await api.tasks({ state: 'awaiting_approval', limit: 100 });
        waiting = (waitingRes.tasks || []).length;
      } catch { agents = []; }
      approvalTimeoutS = 0;
      if (access.manage && typeof api.agentSettings === 'function') {
        try { approvalTimeoutS = Number((await api.agentSettings()).approval_timeout_s) || 0; } catch { /* unknown */ }
      }
    } else {
      agents = [];
      waiting = 0;
    }
    paintNav();
    return access;
  }

  function paintNav() {
    const b = deps.navButton;
    if (!b) return;
    b.classList.toggle('hidden', !access.dispatch);
    b.dataset.waiting = String(waiting);
    b.classList.toggle('has-waiting', waiting > 0);
    b.setAttribute('aria-label', waiting
      ? t('tasks.nav_aria_waiting', { count: waiting }) : t('tasks.nav_aria'));
    b.title = t('tasks.nav_title');
  }

  /** Somebody else is using this browser now: nothing of the last person's tasks may stay on
   *  screen or in a cache (an inline card holds a prompt, tool output and a result). */
  function resetForNewSession() {
    for (const set of cards.values()) for (const c of set) c.dispose();
    cards.clear();
    rows.clear();
    inline.clear();
    expanded.clear();
    people = [];
    filters.state = filters.agent = filters.profile = filters.since = '';
    if (backdrop) { backdrop.remove(); backdrop = null; listEl = null; emptyEl = null; filterForm = null; }
    if (assignBackdrop) { assignBackdrop.remove(); assignBackdrop = null; }
    if (schedBackdrop) { schedBackdrop.remove(); schedBackdrop = null; }
    waiting = 0;
    approvalTimeoutS = 0;
  }

  // ---- the Tasks page ----

  function buildPage() {
    if (backdrop) return;
    const title = el('h2', { id: 'tasks-title', text: t('tasks.title') });
    const close = el('button', { type: 'button', class: 'icon-btn ghost', title: t('common.close'), 'aria-label': t('common.close') }, [spriteIcon('x', 18)]);
    close.addEventListener('click', closePage);
    const assignBtn = el('button', { type: 'button', class: 'btn-primary', id: 'tasks-assign', text: t('tasks.assign') });
    assignBtn.addEventListener('click', () => openAssign({}));
    // 4.3 #13: repeat a task on a timetable. Anyone who may assign tasks has their own schedules.
    const schedBtn = el('button', { type: 'button', class: 'btn-secondary', id: 'tasks-schedules', title: t('sched.open_title') },
      [spriteIcon('clock', 16), ` ${t('sched.open')}`]);
    schedBtn.addEventListener('click', () => openSchedules());
    const stateSel = el('select', { id: 'tasks-f-state', 'aria-label': t('tasks.filter_state') },
      FILTER_STATES.map((s) => el('option', { value: s, text: s ? stateLabel(s) : t('tasks.filter_any_state') })));
    const agentSel = el('select', { id: 'tasks-f-agent', 'aria-label': t('tasks.filter_agent') });
    const profileSel = el('select', { id: 'tasks-f-profile', 'aria-label': t('tasks.filter_person'), hidden: '' });
    const since = el('input', { type: 'date', id: 'tasks-f-since', 'aria-label': t('tasks.filter_since') });
    filterForm = el('form', { class: 'tasks-filters', role: 'search', 'aria-label': t('tasks.filters_aria') }, [
      stateSel, agentSel, profileSel, since,
    ]);
    filterForm.addEventListener('submit', (e) => e.preventDefault());
    filterForm.addEventListener('change', () => {
      filters.state = stateSel.value;
      filters.agent = agentSel.value;
      filters.profile = profileSel.value;
      filters.since = since.value;
      loadList();
    });
    listEl = el('div', { class: 'tasks-list', role: 'list', 'aria-live': 'polite', 'aria-relevant': 'additions removals' });
    emptyEl = el('p', { class: 'muted tasks-empty', text: t('tasks.empty'), hidden: '' });
    const box = el('div', { class: 'modal tasks-modal', role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': 'tasks-title' }, [
      el('header', { class: 'modal-header' }, [title, el('div', { class: 'tasks-header-actions' }, [schedBtn, assignBtn, close])]),
      filterForm,
      el('div', { class: 'tasks-body' }, [listEl, emptyEl]),
    ]);
    backdrop = el('div', { class: 'modal-backdrop hidden', id: 'tasks-backdrop' }, [box]);
    backdrop.addEventListener('click', (e) => { if (e.target === backdrop) closePage(); });
    backdrop.addEventListener('keydown', (e) => trapKeys(e, box, closePage));
    document.body.append(backdrop);
  }

  async function fillFilters() {
    const agentSel = backdrop.querySelector('#tasks-f-agent');
    const profileSel = backdrop.querySelector('#tasks-f-profile');
    let known = agents;
    if (access.manage) {
      try {
        const full = await api.agents();
        known = full.agents || agents;
        people = full.profiles || [];
      } catch { /* keep the published list */ }
    }
    agentSel.replaceChildren(el('option', { value: '', text: t('tasks.filter_any_agent') }),
      ...known.map((a) => el('option', { value: a.id, text: a.name })));
    agentSel.value = filters.agent;
    if (access.operator && people.length) {
      profileSel.hidden = false;
      profileSel.replaceChildren(el('option', { value: '', text: t('tasks.filter_everyone') }),
        ...people.map((p) => el('option', { value: p.id, text: p.name })));
      profileSel.value = filters.profile;
    } else {
      profileSel.hidden = true;
    }
  }

  async function openPage(opts = {}) {
    if (!access.dispatch) await refreshAccess();
    if (!access.dispatch) return;
    buildPage();
    lastFocus = document.activeElement;
    backdrop.classList.remove('hidden');
    await fillFilters();
    await loadList(opts.focusTask);
    const first = backdrop.querySelector('#tasks-assign');
    if (first) first.focus();
  }

  function closePage() {
    if (!backdrop) return;
    backdrop.classList.add('hidden');
    if (lastFocus && lastFocus.focus) { try { lastFocus.focus(); } catch { /* gone */ } }
  }

  async function loadList(focusTask) {
    if (!backdrop) return;
    let res;
    try {
      res = await api.tasks(listQuery(filters));
    } catch (e) {
      listEl.replaceChildren(el('p', { class: 'task-error', role: 'alert', text: errSentence(e) }));
      return;
    }
    rows.clear();
    const items = res.tasks || [];
    listEl.replaceChildren(...items.map(buildRow));
    emptyEl.hidden = items.length > 0;
    // "No tasks match." only makes sense with a filter; an empty page says how to start.
    const filtered = !!(filters.state || filters.agent || filters.profile || filters.since);
    emptyEl.textContent = t(filtered ? 'tasks.empty' : 'tasks.empty_none');
    recount(items);
    if (focusTask) expanded.add(focusTask);
    for (const id of expanded) {
      const r = rows.get(id);
      if (r) r.toggle(true);
    }
  }

  function recount(items) {
    if (!filters.state && !filters.agent && !filters.profile && !filters.since) {
      waiting = items.filter((x) => x.state === 'awaiting_approval').length;
      paintNav();
    }
  }

  function buildRow(task) {
    const row = el('article', { class: 'tasks-row', role: 'listitem', 'data-task': task.id });
    const bodyId = `tasks-body-${task.id}`;
    const head = el('button', { type: 'button', class: 'tasks-row-head', 'aria-expanded': 'false', 'aria-controls': bodyId });
    const body = el('div', { class: 'tasks-row-body', id: bodyId, hidden: '' });
    let opened = false;
    let card = null;
    const paint = (tk) => {
      const line = [];
      if (access.operator && tk.owner_name) line.push(tk.owner_name);
      line.push(relTime(tk.created_at));
      const cost = costLabel(tk);
      if (cost) line.push(cost);
      head.replaceChildren(
        el('span', { class: 'tasks-row-agent', text: tk.agent_name }),
        el('span', { class: `task-pill task-pill-${tk.state}`, text: stateLabel(tk.state) }),
        tk.pending_approval_count || tk.state === 'awaiting_approval'
          ? el('span', { class: 'task-pill task-pill-hold', text: t('tasks.needs_you') }) : null,
        el('span', { class: 'tasks-row-meta', text: line.join(' · ') }),
      );
    };
    const toggle = async (open) => {
      const want = open === undefined ? !opened : !!open;
      if (want === opened) return;
      opened = want;
      if (opened) expanded.add(task.id); else expanded.delete(task.id);
      head.setAttribute('aria-expanded', String(opened));
      body.hidden = !opened;
      if (opened && !card) {
        card = await mountCard(body, task);
      }
    };
    head.addEventListener('click', () => toggle());
    paint(task);
    row.append(head, body);
    rows.set(task.id, {
      el: row, paint, toggle, task,
      update: (summary) => {
        Object.assign(task, summary);
        if (summary.state && summary.state !== 'awaiting_approval') task.pending_approval_count = 0;
        paint(task);
      },
    });
    return row;
  }

  // ---- schedules (4.3 #13) ----

  /** The Schedules dialog: my schedules (an Admin may also look at everyone's, and only switch
   *  another person's off or delete it - the server enforces that too), and the editor. */
  async function openSchedules(opts = {}) {
    if (!access.dispatch) await refreshAccess();
    if (!access.dispatch) return false;
    if (!agents.length) {
      try { agents = (await api.agentsAvailable()).agents || []; } catch { /* leave empty */ }
    }
    if (!schedBackdrop) {
      schedBackdrop = el('div', { class: 'modal-backdrop hidden', id: 'sched-backdrop' });
      document.body.append(schedBackdrop);
    }
    if (access.manage && !people.length && typeof api.agents === 'function') {
      try { people = (await api.agents()).profiles || []; } catch { /* names stay unknown */ }
    }
    // "Post results in": this person's own chats only (fetched each time the dialog opens).
    const [chats, botNames] = await Promise.all([ownChats(api), (async () => {
      const out = {};
      if (typeof api.bots !== 'function') return out;
      try {
        const r = await api.bots();
        for (const b of (Array.isArray(r) ? r : (r && r.bots) || [])) if (b && b.id) out[b.id] = b.name || b.id;
      } catch { /* titles alone */ }
      return out;
    })()]);
    const host = schedBackdrop;
    const prevFocus = document.activeElement;
    let scope = opts.scope || 'mine';
    const heading = el('h2', { id: 'sched-title', text: t('sched.title') });
    const closeBtn = el('button', { type: 'button', class: 'icon-btn ghost', 'aria-label': t('common.close'), title: t('common.close') }, [spriteIcon('x', 18)]);
    const newBtn = el('button', { type: 'button', class: 'btn-primary', id: 'sched-new', title: t('sched.new') }, [spriteIcon('plus', 16), ` ${t('sched.new')}`]);
    const listEl2 = el('div', { class: 'sched-list', role: 'list', 'aria-live': 'polite' });
    const editorHost = el('div', { class: 'sched-editor-host' });
    const status = el('p', { class: 'muted sched-limits' });
    const everyone = el('input', { type: 'checkbox', id: 'sched-everyone' });
    // Back to where the limits are set (for the people who may change them).
    const limitsLink = access.manage && typeof deps.openSettings === 'function'
      ? el('button', { type: 'button', class: 'text-link-btn sched-limits-link', text: t('sched.limits_link') }) : null;
    if (limitsLink) {
      limitsLink.addEventListener('click', () => {
        schedBackdrop.classList.add('hidden');
        if (backdrop) backdrop.classList.add('hidden');
        deps.openSettings('connections#agents');
      });
    }
    const everyoneRow = access.manage
      ? el('label', { class: 'sched-everyone', for: 'sched-everyone' }, [everyone, ` ${t('sched.everyone')}`]) : null;
    everyone.addEventListener('change', () => { scope = everyone.checked ? 'all' : 'mine'; load(); });
    const box = el('div', { class: 'modal sched-modal', role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': 'sched-title' }, [
      el('header', { class: 'modal-header' }, [heading, el('div', { class: 'tasks-header-actions' }, [newBtn, closeBtn])]),
      el('p', { class: 'modal-hint', text: t('sched.hint') }),
      limitsLink,
      everyoneRow, status, editorHost, listEl2,
    ]);
    host.replaceChildren(box);
    const done = () => {
      host.classList.add('hidden');
      if (prevFocus && prevFocus.focus) { try { prevFocus.focus(); } catch { /* gone */ } }
    };
    closeBtn.addEventListener('click', done);
    host.onkeydown = (e) => trapKeys(e, box, done);
    host.onclick = (e) => { if (e.target === host) done(); };
    newBtn.addEventListener('click', () => edit(null));
    const agentName = (id) => (agents.find((a) => a.id === id) || {}).name || t('sched.agent_unknown');

    async function load() {
      let res;
      try {
        res = await extra.schedules(scope);
      } catch (e) {
        listEl2.replaceChildren(el('p', { class: 'task-error', role: 'alert', text: errSentence(e) }));
        return;
      }
      const rows = res.schedules || [];
      const lim = res.limits || {};
      status.textContent = lim.per_profile ? t('sched.limits', { max: lim.per_profile, min: lim.every_minutes_min || EVERY_MIN }) : '';
      listEl2.replaceChildren(...(rows.length ? rows.map(row) : [el('p', { class: 'muted', text: t('sched.empty') })]));
    }

    function row(sc) {
      const name = agentName(sc.agent_id);
      const who = !sc.own && people.length ? (people.find((p) => p.id === sc.owner_profile) || {}).name : '';
      const next = sc.enabled && sc.next_run ? t('sched.next', { when: whenText(sc.next_run) }) : t('sched.off');
      const toggle = el('button', {
        type: 'button', class: 'btn-secondary',
        title: sc.enabled ? t('sched.pause_aria', { name }) : t('sched.resume_aria', { name }),
        'aria-label': sc.enabled ? t('sched.pause_aria', { name }) : t('sched.resume_aria', { name }),
      }, [spriteIcon(sc.enabled ? 'pause' : 'play', 14), ` ${sc.enabled ? t('sched.pause') : t('sched.resume')}`]);
      // Somebody else's schedule: an Admin may switch it off, never on (no acting as them).
      if (!sc.own && !sc.enabled) toggle.hidden = true;
      toggle.addEventListener('click', () => act(async () => { await extra.patchSchedule(sc.id, { enabled: !sc.enabled }); }));
      const editBtn = el('button', { type: 'button', class: 'btn-secondary', title: t('sched.edit_aria', { name }), 'aria-label': t('sched.edit_aria', { name }) },
        [spriteIcon('pencil', 14), ` ${t('sched.edit')}`]);
      editBtn.hidden = !sc.own;
      editBtn.addEventListener('click', () => edit(sc));
      const delBtn = el('button', { type: 'button', class: 'btn-secondary danger-text', title: t('sched.delete_aria', { name }), 'aria-label': t('sched.delete_aria', { name }) },
        [spriteIcon('trash', 14), ` ${t('sched.delete')}`]);
      delBtn.addEventListener('click', () => act(async () => {
        if (deps.confirm && !(await deps.confirm(t('sched.delete_confirm', { name }), { danger: true, okText: t('sched.delete') }))) return;
        await extra.deleteSchedule(sc.id);
      }));
      const bad = sc.status && sc.status !== 'ok' && sc.status !== 'disabled';
      return el('article', { class: `sched-row${sc.enabled ? '' : ' sched-off'}`, role: 'listitem', 'data-schedule': sc.id }, [
        el('div', { class: 'sched-row-text' }, [
          el('strong', { text: name }),
          el('span', { class: 'sched-rule', text: ruleText(sc.rule) }),
          el('span', { class: `task-pill task-pill-${sc.enabled ? 'running' : 'hold'}`, text: next }),
          who ? el('span', { class: 'sched-owner', text: t('sched.belongs_to', { name: who }) }) : null,
          !sc.own && !who ? el('span', { class: 'sched-owner', text: t('sched.someone_else') }) : null,
          sc.own && sc.prompt ? el('span', { class: 'sched-prompt', dir: 'auto', text: sc.prompt }) : null,
          bad || sc.status_detail ? el('span', { class: bad ? 'task-error' : 'muted', text: sc.status_detail || sc.status }) : null,
        ]),
        el('div', { class: 'sched-actions' }, [toggle, editBtn, delBtn]),
      ]);
    }

    async function act(fn) {
      try { await fn(); await load(); } catch (e) { toast(errSentence(e), true); }
    }

    function edit(sc) {
      const usable = agents.filter((a) => a.available !== false || (sc && a.id === sc.agent_id));
      // A new schedule posts in the chat open now when it is one of mine, else my newest chat;
      // an existing one keeps the chat it has (or none).
      const current = typeof deps.currentThreadId === 'function' ? deps.currentThreadId() : null;
      const defaultChat = (current && chats.some((c) => c.id === current)) ? current : ((chats[0] || {}).id || '');
      const f = {
        agent_id: sc ? sc.agent_id : (usable[0] || {}).id || '', prompt: sc ? sc.prompt || '' : '',
        thread_id: sc ? sc.thread_id || '' : defaultChat, ...ruleToForm(sc && sc.rule),
      };
      if (!sc) f.repeat = 'daily';
      const ids = { agent: 'sched-agent', prompt: 'sched-prompt', thread: 'sched-thread', repeat: 'sched-repeat', every: 'sched-every', at: 'sched-at', day: 'sched-day' };
      const agentSel = el('select', { id: ids.agent, class: 'ui-select' }, usable.map((a) => el('option', { value: a.id, text: a.name })));
      agentSel.value = f.agent_id;
      agentSel.addEventListener('change', () => { f.agent_id = agentSel.value; });
      const prompt = el('textarea', { id: ids.prompt, rows: '4', maxlength: String(PROMPT_MAX), dir: 'auto', required: '' });
      prompt.value = f.prompt;
      prompt.addEventListener('input', () => { f.prompt = prompt.value; });
      const chatOpts = chats.map((c) => el('option', { value: c.id, text: chatLabel(c, botNames) }));
      // The schedule's own chat beyond the first page of chats: still offered, never dropped.
      if (f.thread_id && !chats.some((c) => c.id === f.thread_id)) {
        chatOpts.unshift(el('option', { value: f.thread_id, text: t('sched.post_kept') }));
      }
      chatOpts.push(el('option', { value: '', text: t('sched.post_none') }));
      const threadSel = el('select', { id: ids.thread, class: 'ui-select', 'aria-describedby': 'sched-thread-help' }, chatOpts);
      threadSel.value = f.thread_id;
      threadSel.addEventListener('change', () => { f.thread_id = threadSel.value; });
      const repeat = el('select', { id: ids.repeat, class: 'ui-select' }, [
        el('option', { value: 'every', text: t('sched.repeat_every') }),
        el('option', { value: 'daily', text: t('sched.repeat_daily') }),
        el('option', { value: 'weekly', text: t('sched.repeat_weekly') }),
      ]);
      repeat.value = f.repeat;
      const every = el('input', { id: ids.every, type: 'number', min: String(EVERY_MIN), max: '10080', step: '1', inputmode: 'numeric', value: f.every });
      const at = el('input', { id: ids.at, type: 'time', step: '60', value: f.at });
      const day = el('select', { id: ids.day, class: 'ui-select' }, DAYS.map((d) => el('option', { value: d, text: dayLabel(d) })));
      day.value = f.day;
      const lbl = (id, key) => el('label', { for: id, text: t(key) });
      const everyRow = el('div', { class: 'sched-field sched-every-row' }, [lbl(ids.every, 'sched.every_label'), el('div', { class: 'sched-inline' }, [every, el('span', { class: 'agents-unit', text: t('sched.minutes') })])]);
      const dayRow = el('div', { class: 'sched-field' }, [lbl(ids.day, 'sched.day_label'), day]);
      const atRow = el('div', { class: 'sched-field' }, [lbl(ids.at, 'sched.at_label'), at]);
      const preview = el('p', { class: 'sched-preview', role: 'status', 'aria-live': 'polite' });
      const err = el('p', { class: 'task-error', role: 'alert', hidden: '' });
      const paint = () => {
        f.repeat = repeat.value; f.every = every.value; f.at = at.value; f.day = day.value;
        everyRow.hidden = f.repeat !== 'every';
        atRow.hidden = f.repeat === 'every';
        dayRow.hidden = f.repeat !== 'weekly';
        const r = scheduleRule(f);
        preview.textContent = r.error ? r.error
          : t('sched.preview', { rule: ruleText(r.rule), when: previewRuns(r.rule).map(whenText).join(' · '), tz: localZone() });
        preview.classList.toggle('task-error', !!r.error);
      };
      for (const n of [repeat, every, at, day]) { n.addEventListener('input', paint); n.addEventListener('change', paint); }
      const save = el('button', { type: 'submit', class: 'btn-primary', text: sc ? t('sched.save') : t('sched.create') });
      const cancel = el('button', { type: 'button', class: 'btn-secondary', text: t('common.cancel') });
      cancel.addEventListener('click', () => { editorHost.replaceChildren(); newBtn.focus(); });
      const form = el('form', { class: 'sched-form', 'aria-label': sc ? t('sched.edit_title') : t('sched.new_title') }, [
        el('h3', { text: sc ? t('sched.edit_title') : t('sched.new_title') }),
        el('div', { class: 'sched-field' }, [lbl(ids.agent, 'sched.agent_label'), agentSel]),
        el('div', { class: 'sched-field' }, [lbl(ids.prompt, 'sched.prompt_label'), prompt]),
        el('div', { class: 'sched-field' }, [lbl(ids.thread, 'sched.post_label'), threadSel,
          el('p', { class: 'muted sched-help', id: 'sched-thread-help', text: t('sched.post_help') })]),
        el('div', { class: 'sched-when' }, [el('div', { class: 'sched-field' }, [lbl(ids.repeat, 'sched.repeat_label'), repeat]), everyRow, dayRow, atRow]),
        preview, err,
        el('div', { class: 'assign-actions' }, [cancel, save]),
      ]);
      form.addEventListener('submit', async (e) => {
        e.preventDefault();
        paint();
        const body = scheduleBody(f);
        if (body.error) { err.hidden = false; err.textContent = body.error; return; }
        err.hidden = true;
        save.disabled = true;
        try {
          if (sc) await extra.patchSchedule(sc.id, body);
          else await extra.createSchedule(body);
          toast(t('sched.saved'));
          editorHost.replaceChildren();
          await load();
          newBtn.focus();
        } catch (ex) {
          err.hidden = false;
          err.textContent = errSentence(ex);
        } finally { save.disabled = false; }
      });
      if (!usable.length) {
        editorHost.replaceChildren(el('p', { class: 'modal-hint', text: t('tasks.assign_none') }));
        return;
      }
      editorHost.replaceChildren(form);
      paint();
      agentSel.focus();
    }

    host.classList.remove('hidden');
    await load();
    newBtn.focus();
    return true;
  }

  // ---- assign ----

  function buildAssign() {
    if (assignBackdrop) return assignBackdrop;
    assignBackdrop = el('div', { class: 'modal-backdrop hidden', id: 'assign-backdrop' });
    document.body.append(assignBackdrop);
    return assignBackdrop;
  }

  async function openAssign({ threadId = null, agentId = '', prompt = '', fromComposer = false } = {}) {
    if (!access.dispatch) await refreshAccess();
    if (!access.dispatch) return false;
    if (!agents.length) {
      try { agents = (await api.agentsAvailable()).agents || []; } catch { /* leave empty */ }
    }
    const host = buildAssign();
    const prevFocus = document.activeElement;
    const usable = agents.filter((a) => a.available !== false);
    const heading = el('h2', { id: 'assign-title', text: t('tasks.assign') });
    const closeBtn = el('button', { type: 'button', class: 'icon-btn ghost', title: t('common.close'), 'aria-label': t('common.close') }, [spriteIcon('x', 18)]);
    const select = el('select', { id: 'assign-agent', required: '' },
      usable.map((a) => el('option', { value: a.id, text: `${a.name} · ${a.backend_label || a.backend}` })));
    if (agentId && usable.some((a) => a.id === agentId)) select.value = agentId;
    const area = el('textarea', { id: 'assign-prompt', rows: '6', maxlength: String(PROMPT_MAX), required: '', dir: 'auto' });
    area.value = prompt;
    const hint = el('p', { class: 'assign-hint', id: 'assign-hint' });
    const paintHint = () => {
      const a = usable.find((x) => x.id === select.value);
      const bits = [];
      if (a && a.description) bits.push(a.description);
      if (a) bits.push(a.data_leaves_host ? t('tasks.assign_leaves_host', { backend: a.backend_label || a.backend }) : t('tasks.assign_stays_here'));
      bits.push(t('tasks.assign_asks_first'));
      hint.textContent = bits.join(' ');
    };
    select.addEventListener('change', paintHint);
    const err = el('p', { class: 'task-error', role: 'alert', hidden: '' });
    const go = el('button', { type: 'submit', class: 'btn-primary', text: t('tasks.assign_submit') });
    const cancel = el('button', { type: 'button', class: 'btn-secondary', text: t('common.cancel') });
    const form = el('form', { class: 'assign-form' }, [
      el('label', { for: 'assign-agent', text: t('tasks.assign_agent') }), select,
      hint,
      el('label', { for: 'assign-prompt', text: t('tasks.assign_prompt') }), area,
      err,
      el('div', { class: 'assign-actions' }, [cancel, go]),
    ]);
    const box = el('div', { class: 'modal assign-modal', role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': 'assign-title' }, [
      el('header', { class: 'modal-header' }, [heading, closeBtn]),
      usable.length ? form : el('p', { class: 'modal-hint', text: t('tasks.assign_none') }),
    ]);
    host.replaceChildren(box);
    paintHint();
    const done = (result) => {
      host.classList.add('hidden');
      if (prevFocus && prevFocus.focus) { try { prevFocus.focus(); } catch { /* gone */ } }
      return result;
    };
    closeBtn.addEventListener('click', () => done(false));
    cancel.addEventListener('click', () => done(false));
    host.onkeydown = (e) => trapKeys(e, box, () => done(false));
    host.onclick = (e) => { if (e.target === host) done(false); };
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const text = area.value.trim();
      if (!text) { area.focus(); return; }
      go.disabled = true;
      err.hidden = true;
      try {
        const res = await api.createTask({ agent_id: select.value, prompt: text, origin_thread_id: threadId || undefined });
        done(true);
        toast(t('tasks.assigned_toast', { agent: res.task.agent_name }));
        if (deps.onAssigned) deps.onAssigned(res.task, { fromComposer });
        if (!threadId) openPage({ focusTask: res.task.id });
      } catch (ex) {
        go.disabled = false;
        err.hidden = false;
        err.textContent = `${(ex && (ex.userMessage || ex.message)) || ex}${ex && ex.fix ? ` ${ex.fix}` : ''}`;
      }
    });
    host.classList.remove('hidden');
    (usable.length ? area : closeBtn).focus();
    return true;
  }

  /** The composer asks before a send: is this an `@agent ...` that should become a task? */
  function interceptSend(text, threadId) {
    if (!access.dispatch || !agents.length) return false;
    const m = parseMention(text, agents);
    if (!m) return false;
    openAssign({ threadId, agentId: m.agent.id, prompt: m.prompt, fromComposer: true });
    return true;
  }

  // ---- inline cards in a chat ----

  /** The card for a message the server stamped with `metadata.task_card.kind === 'assigned'`. */
  function cardForMessage(msg) {
    const tc = msg && msg.metadata && msg.metadata.task_card;
    if (!tc || typeof tc.task_id !== 'string' || tc.kind !== 'assigned') return null;
    if (!access.dispatch) return null;
    const id = tc.task_id;
    const hit = inline.get(id);
    if (hit) return hit.el;
    const wrap = el('div', { class: 'task-inline' });
    inline.set(id, { el: wrap });
    mountCard(wrap, { id }, { compact: true }).then((card) => { inline.get(id).card = card; });
    return wrap;
  }

  // ---- frames ----

  function onFrame(frame) {
    if (!frame || typeof frame.type !== 'string' || !frame.type.startsWith('task.')) return;
    const id = frame.task_id;
    const set = cards.get(id);
    if (set) {
      for (const c of [...set]) {
        // A card that is no longer on the page (a collapsed row, a re-rendered message) is
        // dropped - unless it is the one cached for a chat bubble, which may be re-attached.
        const kept = inline.get(id) && inline.get(id).card === c;
        if (!c.connected && !kept) { c.dispose(); set.delete(c); continue; }
        c.applyFrame(frame);
      }
    }
    const row = rows.get(id);
    if (frame.task && row) row.update(frame.task);
    if (!row && backdrop && !backdrop.classList.contains('hidden') && frame.type !== 'task.approval') {
      clearTimeout(reloadTimer);
      reloadTimer = setTimeout(() => loadList(), 400);          // a task made elsewhere appears
    }
    if (frame.type === 'task.approval') {
      waiting = Math.max(0, waiting + (frame.state === 'pending' ? 1 : -1));
      paintNav();
      if (frame.state === 'pending' && deps.announce) deps.announce(t('approval.said_needed', { tool: frame.tool || '' }), true);
    }
  }

  return {
    refreshAccess, openPage, closePage, openAssign, openSchedules, interceptSend, cardForMessage, onFrame,
    get access() { return access; },
    get agents() { return agents; },
    get isOpen() { return !!backdrop && !backdrop.classList.contains('hidden'); },
    closeAll() {
      if (backdrop) backdrop.classList.add('hidden');
      if (assignBackdrop) assignBackdrop.classList.add('hidden');
      if (schedBackdrop) schedBackdrop.classList.add('hidden');
    },
  };
}

/** An API error as a sentence for people: the server's own words (it sends whole sentences),
 *  without the "403:" status prefix, plus the fix when it gives one. */
export const errSentence = errorText;

/** Keep Tab inside `box` and let Escape close it. Shared by both dialogs. */
export function trapKeys(e, box, onEscape) {
  if (e.key === 'Escape') {
    e.stopPropagation();
    onEscape();
    return;
  }
  if (e.key !== 'Tab') return;
  const items = [...box.querySelectorAll('button, [href], input, select, textarea, summary, [tabindex]:not([tabindex="-1"])')]
    .filter((n) => !n.disabled && !n.hidden && n.offsetParent !== null);
  if (!items.length) return;
  const first = items[0];
  const last = items[items.length - 1];
  const active = document.activeElement;
  if (e.shiftKey && (active === first || !box.contains(active))) { e.preventDefault(); last.focus(); }
  else if (!e.shiftKey && (active === last || !box.contains(active))) { e.preventDefault(); first.focus(); }
}
