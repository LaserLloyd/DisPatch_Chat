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

import { api as realApi } from './api.js?v=36';
import { t, relTime } from './i18n.js?v=5';
import { el, spriteIcon } from './util.js?v=21';
import { createTaskCard, stateLabel, costLabel } from './task_card.js?v=2';

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

/** What the person may be offered, from /api/auth/me. */
export function accessFrom(me) {
  const perms = Array.isArray(me && me.permissions) ? me.permissions : [];
  const all = perms.includes('*');
  const has = (p) => all || perms.includes(p);
  const role = me && me.role;
  return {
    dispatch: !!me && has('dispatch_agents'),
    approve: !!me && has('approve_agent_actions'),
    manage: !!me && has('manage_agents'),
    operator: !!me && (all || role === 'owner' || role === 'admin'),
  };
}

export const NO_ACCESS = Object.freeze({ dispatch: false, approve: false, manage: false, operator: false });

// ----------------------------------------------------------------------------- the module

/**
 * deps: { api?, toast, confirm, announce, sessionKey, navButton, onAssigned(task, {fromComposer}) }
 */
export function createTasksUi(deps = {}) {
  const api = deps.api || realApi;
  let access = NO_ACCESS;
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
    showOwner: access.operator, ...extra,
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
    waiting = 0;
  }

  // ---- the Tasks page ----

  function buildPage() {
    if (backdrop) return;
    const title = el('h2', { id: 'tasks-title', text: t('tasks.title') });
    const close = el('button', { type: 'button', class: 'icon-btn ghost', 'aria-label': t('common.close') }, [spriteIcon('x', 18)]);
    close.addEventListener('click', closePage);
    const assignBtn = el('button', { type: 'button', class: 'btn-primary', id: 'tasks-assign', text: t('tasks.assign') });
    assignBtn.addEventListener('click', () => openAssign({}));
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
      el('header', { class: 'modal-header' }, [title, el('div', { class: 'tasks-header-actions' }, [assignBtn, close])]),
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
      listEl.replaceChildren(el('p', { class: 'task-error', role: 'alert', text: String((e && (e.userMessage || e.message)) || e) }));
      return;
    }
    rows.clear();
    const items = res.tasks || [];
    listEl.replaceChildren(...items.map(buildRow));
    emptyEl.hidden = items.length > 0;
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
    const closeBtn = el('button', { type: 'button', class: 'icon-btn ghost', 'aria-label': t('common.close') }, [spriteIcon('x', 18)]);
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
    refreshAccess, openPage, closePage, openAssign, interceptSend, cardForMessage, onFrame,
    get access() { return access; },
    get agents() { return agents; },
    get isOpen() { return !!backdrop && !backdrop.classList.contains('hidden'); },
    closeAll() {
      if (backdrop) backdrop.classList.add('hidden');
      if (assignBackdrop) assignBackdrop.classList.add('hidden');
    },
  };
}

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
