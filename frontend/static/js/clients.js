// clients.js — the "Clients" tab (WebBuilder concept): a native DisPatch UI
// against the practice box's client-pipeline API, proxied server-side through
// /api/practice/* (see backend/app/practice_bridge.py). Ported from
// practice/gui/static/app.js's Overview/Clients/stepper logic onto DisPatch's
// own api.js client, i18n and CSS tokens (app.css `.clients-*` rules).
//
// Full-session only: main.js only ever calls openClientsView() when
// state.auth.features.practice is true, which the server only sets for an
// unlocked session — see _require_practice in backend/app/main.py.
//
// Hash routing lives INSIDE this panel's own state (clientsRoute), not the
// document hash, so it doesn't collide with DisPatch's own #chat/#threads
// routing; back/forward inside the panel is handled by the tab buttons +
// an internal history-less stack (Back always returns to the list).

import { api } from './api.js?v=29';
import { t } from './i18n.js?v=3';
import { acquireInert, releaseInert } from './util.js?v=20';

const JOB_POLL_MS = 1500;
const JOB_TIMEOUT_MS = 30 * 60 * 1000;
const COMPLETED_STATES = new Set(['CLOSED', 'DISQUALIFIED']);

const state = {
  tab: 'overview',       // overview | active | completed | detail
  detailId: null,
  search: '',
  client: null,           // full detail payload for the open client
  job: null,
  jobTimer: null,
  stopped: false,   // set while the pane is closed; see stopClientsPolling
  modalInert: null,        // inert hold taken by the confirm dialog
  modalReturnFocus: null,  // what to give focus back to when it closes
  root: null,             // #clients-root
  strip: null,            // #clients-job-strip
};

function escapeHtml(s) {
  if (s === null || s === undefined) return '';
  return String(s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function el(html) {
  const tpl = document.createElement('template');
  tpl.innerHTML = html.trim();
  return tpl.content.firstElementChild;
}

function healthGlyph(health) {
  const h = String(health || '').toLowerCase();
  if (h.includes('hold')) return '■';
  if (h.includes('stall')) return '▲';
  if (h.includes('wait')) return '▶';
  return '●';
}

// --------------------------------------------------------------------------
// Mount / tab switching (called from main.js)
// --------------------------------------------------------------------------

/** Stop the job poll. Called when the Clients pane closes, including on a
 *  drop to Safe Mode.
 *
 *  pollJob() reschedules ITSELF, and closing the pane only hid the view — so
 *  the timer kept firing GET /api/practice/jobs/<id> for the rest of the
 *  session. On a locked device those requests 403, which is the gate doing its
 *  job, but a background loop talking to the practice box from a device
 *  somebody has just locked is not something to leave running. The pane is
 *  also the only thing that would ever show the answer.
 */
export function stopClientsPolling() {
  state.stopped = true;
  // The confirm dialog is parented to <body>, so hiding the pane does not take
  // it with it. Closing the pane means closing the dialog too.
  closeModal();
  if (state.jobTimer) { clearTimeout(state.jobTimer); state.jobTimer = null; }
  state.job = null;
}

export function initClients(rootEl, stripEl) {
  state.root = rootEl;
  state.strip = stripEl;
}

export function clientsTabNav(container) {
  container.querySelectorAll('.clients-tab-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      const tab = btn.getAttribute('data-clients-tab');
      showClientsTab(tab);
    });
  });
}

export async function showClientsTab(tab) {
  state.tab = tab;
  state.detailId = null;
  const container = document.getElementById('clients-tabnav');
  if (container) {
    container.querySelectorAll('.clients-tab-btn').forEach((b) => {
      b.classList.toggle('active', b.getAttribute('data-clients-tab') === tab);
    });
    container.classList.remove('hidden');
  }
  await renderClientsRoute();
}

export async function openClientDetail(clientId) {
  state.tab = 'detail';
  state.detailId = clientId;
  const nav = document.getElementById('clients-tabnav');
  if (nav) nav.classList.add('hidden');
  await renderClientsRoute();
}

async function renderClientsRoute() {
  const root = state.root;
  if (!root) return;
  root.innerHTML = '';
  try {
    if (state.tab === 'overview') await renderOverview(root);
    else if (state.tab === 'active' || state.tab === 'completed') await renderClientsList(root, state.tab);
    else if (state.tab === 'detail') await renderClientDetail(root, state.detailId);
  } catch (e) {
    if (e && e.status === 401) return; // api.js already showed the lock screen
    root.appendChild(el(`<p class="empty-note clients-error">${escapeHtml(t('clients.load_error') || 'Could not load this view.')}</p>`));
  }
}

export async function refreshClientsCurrentView() {
  await renderClientsRoute();
}

// --------------------------------------------------------------------------
// Overview
// --------------------------------------------------------------------------

async function renderOverview(root) {
  const ov = await api.practiceGet('overview');
  const view = el('<div class="view clients-view-overview"></div>');

  const doctor = ov.doctor || {};
  // The practice doctor's real status vocabulary is OK/WARN/HUMAN/FIXED
  // (practice/gui/doctor.py::summarize) — not "healthy"/"warn"/"needs_human"
  // as the practice GUI's own app.js checks for (a pre-existing bug there:
  // its health card renders the RIGHT text but the WRONG colour/glyph for
  // every status, always falling through to the "needs attention" class).
  // Fixed here rather than carried over, since it costs nothing.
  const norm = String(doctor.status || 'unknown').toLowerCase();
  const healthCls = norm === 'ok' ? 'status-healthy' : (norm === 'warn' ? 'status-warn' : 'status-needs_human');
  view.appendChild(el(`
    <section class="ov-card health-card ${healthCls}">
      <div class="health-card-glyph">${norm === 'ok' ? '●' : (norm === 'warn' ? '▲' : '■')}</div>
      <div>
        <h3>${escapeHtml((doctor.status || 'UNKNOWN').toString().toUpperCase())}</h3>
        <p class="empty-note">${doctor.rows_bad ? `${doctor.rows_bad} check(s) need attention. ` : 'All checks clean. '}${escapeHtml(t('clients.last_checked') || 'Last checked')} ${escapeHtml(doctor.ts || '—')}.</p>
      </div>
    </section>
  `));

  view.appendChild(renderOverviewList(t('clients.waiting_on_you') || 'Waiting on you', ov.waiting_on_you || [], t('clients.waiting_empty') || 'Nothing is waiting on you right now.', 'waiting'));
  view.appendChild(renderOverviewList(t('clients.holds') || 'Security holds', ov.holds || [], t('clients.holds_empty') || 'No clients on hold.', 'hold'));
  view.appendChild(renderOverviewList(t('clients.stalled') || 'Stalled', ov.stalled || [], t('clients.stalled_empty') || 'No stalled clients.', 'stalled'));

  const active = ov.active || [];
  const tableSect = el(`<section class="ov-card"><h3>${escapeHtml(t('clients.tab_active') || 'Active clients')}</h3></section>`);
  if (!active.length) {
    tableSect.appendChild(el(`<p class="empty-note">${escapeHtml(t('clients.no_active') || 'No active clients.')}</p>`));
  } else {
    const table = el(`
      <table class="compact-table">
        <thead><tr><th>${escapeHtml(t('clients.col_name') || 'Name')}</th><th>${escapeHtml(t('clients.col_branch') || 'Branch')}</th><th>${escapeHtml(t('clients.col_state') || 'State')}</th><th>${escapeHtml(t('clients.col_next') || 'Next')}</th></tr></thead>
        <tbody></tbody>
      </table>
    `);
    const tbody = table.querySelector('tbody');
    active.forEach((c) => {
      const row = el(`
        <tr class="client-row" tabindex="0" role="button" data-id="${escapeHtml(c.id)}">
          <td>${escapeHtml(c.name)}</td><td>${escapeHtml(c.branch || '')}</td>
          <td>${escapeHtml(c.state)}</td><td>${escapeHtml(c.next || '—')}</td>
        </tr>
      `);
      row.addEventListener('click', () => openClientDetail(c.id));
      row.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); row.click(); } });
      tbody.appendChild(row);
    });
    tableSect.appendChild(table);
  }
  view.appendChild(tableSect);

  const events = ov.recent_events || [];
  const eventsSect = el(`<section class="ov-card"><h3>${escapeHtml(t('clients.recent_activity') || 'Recent activity')}</h3></section>`);
  if (!events.length) {
    eventsSect.appendChild(el(`<p class="empty-note">${escapeHtml(t('clients.no_activity') || 'Nothing has happened yet.')}</p>`));
  } else {
    const list = el('<ul class="log-list"></ul>');
    events.slice(0, 20).forEach((ev) => {
      list.appendChild(el(`<li><span class="log-client">${escapeHtml(ev.client_name || ev.client_id || '')}</span> ${escapeHtml(ev.plain || '')}</li>`));
    });
    eventsSect.appendChild(list);
  }
  view.appendChild(eventsSect);

  root.appendChild(view);
}

function renderOverviewList(title, items, emptyText, kind) {
  const sect = el(`<section class="ov-card ov-list-${kind}"><h3>${escapeHtml(title)}</h3></section>`);
  if (!items.length) {
    sect.appendChild(el(`<p class="empty-note">${escapeHtml(emptyText)}</p>`));
    return sect;
  }
  const list = el('<ul class="waiting-list"></ul>');
  items.forEach((c) => {
    const label = c.state ? `${escapeHtml(c.state)}` : '';
    const row = el(`
      <li class="waiting-row">
        <span class="waiting-glyph">${healthGlyph(kind)}</span>
        <span class="waiting-body">
          <span class="waiting-name">${escapeHtml(c.name)}</span>
          <span class="waiting-detail">${label}</span>
        </span>
        <button type="button" class="secondary jump-btn">${escapeHtml(t('clients.jump') || 'Jump ›')}</button>
      </li>
    `);
    row.querySelector('.jump-btn').addEventListener('click', () => openClientDetail(c.id));
    list.appendChild(row);
  });
  sect.appendChild(list);
  return sect;
}

// --------------------------------------------------------------------------
// Active / Completed lists
// --------------------------------------------------------------------------

async function renderClientsList(root, bucket) {
  const counts = await api.practiceGet('clients/counts');
  const view = el(`
    <div class="view clients-view-list">
      <div class="field client-search-field">
        <input type="text" id="client-search" placeholder="${escapeHtml(t('clients.search_placeholder') || 'Search by name…')}">
      </div>
      <div id="client-list-holder"></div>
    </div>
  `);
  const nav = document.getElementById('clients-tabnav');
  if (nav) {
    const activeBtn = nav.querySelector('[data-clients-tab="active"]');
    const completedBtn = nav.querySelector('[data-clients-tab="completed"]');
    if (activeBtn) activeBtn.textContent = `${t('clients.tab_active') || 'Active'} (${counts.active ?? 0})`;
    if (completedBtn) completedBtn.textContent = `${t('clients.tab_completed') || 'Completed'} (${counts.completed ?? 0})`;
  }
  root.appendChild(view);

  const holder = view.querySelector('#client-list-holder');
  const searchInput = view.querySelector('#client-search');
  searchInput.value = state.search;

  const items = await api.practiceGet(`clients?status=${encodeURIComponent(bucket)}`);

  function paint() {
    const q = state.search.trim().toLowerCase();
    const filtered = q ? items.filter((c) => String(c.name || '').toLowerCase().includes(q)) : items;
    paintClientList(holder, filtered, bucket, q);
  }
  paint();
  searchInput.addEventListener('input', () => { state.search = searchInput.value; paint(); });
}

function paintClientList(holder, items, bucket, query) {
  holder.innerHTML = '';
  if (!items.length) {
    const msg = query
      ? (t('clients.no_matches') || 'No clients match that search.')
      : (bucket === 'active' ? (t('clients.no_active') || 'No active clients.') : (t('clients.no_completed') || 'No completed clients yet.'));
    holder.appendChild(el(`<p class="empty-note">${escapeHtml(msg)}</p>`));
    return;
  }
  const list = el('<ul class="client-list"></ul>');
  items.forEach((c) => {
    const row = el(`
      <li>
        <button type="button" class="client-list-item">
          <span class="chip-glyph">${healthGlyph(c.health)}</span>
          <span class="chip-text">
            <span class="chip-name">${escapeHtml(c.name)}</span>
            <span class="chip-state">${escapeHtml(c.branch || '')} · ${escapeHtml(c.state)}</span>
          </span>
          <span class="list-chevron" aria-hidden="true">›</span>
        </button>
      </li>
    `);
    row.querySelector('button').addEventListener('click', () => openClientDetail(c.id));
    list.appendChild(row);
  });
  holder.appendChild(list);
}

// --------------------------------------------------------------------------
// Client detail: header + hold banner + 12-step stepper
// --------------------------------------------------------------------------

async function renderClientDetail(root, clientId) {
  const view = el('<div class="view clients-view-detail"></div>');
  root.appendChild(view);

  let client;
  try {
    client = await api.practiceGet(`clients/${encodeURIComponent(clientId)}`);
  } catch (e) {
    if (e && e.status === 401) return;
    view.innerHTML = `<button type="button" class="secondary clients-inline-back" id="cd-back">‹ ${escapeHtml(t('common.back') || 'Back')}</button><p class="empty-note">${escapeHtml(t('clients.load_error') || 'Could not load this client.')}</p>`;
    view.querySelector('#cd-back').addEventListener('click', () => goBackToClients());
    return;
  }
  state.client = client;

  const backBar = el(`
    <div class="clients-detail-header">
      <button type="button" class="secondary clients-inline-back" id="cd-back">‹ ${escapeHtml(t('common.back') || 'Back')}</button>
      <h3 class="clients-detail-title">${escapeHtml(client.client.name)} <span class="clients-detail-sub">${escapeHtml(client.client.branch || '')} · ${escapeHtml(client.state)}</span></h3>
      <button type="button" class="secondary" id="cd-open-thread" hidden>${escapeHtml(t('clients.open_thread') || 'Open thread')}</button>
    </div>
  `);
  backBar.querySelector('#cd-back').addEventListener('click', () => goBackToClients());
  view.appendChild(backBar);
  wireOpenThreadButton(backBar.querySelector('#cd-open-thread'), client.client);

  const holdSlot = el('<div class="detail-hold-slot"></div>');
  view.appendChild(holdSlot);
  renderHoldBanner(holdSlot, client);

  const stepsRoot = el('<div class="steps-root"></div>');
  view.appendChild(stepsRoot);
  renderSteps(stepsRoot, client);
}

function goBackToClients() {
  const bucket = state.client && COMPLETED_STATES.has(state.client.state) ? 'completed' : 'active';
  showClientsTab(bucket);
}

async function reloadClientDetail(clientId) {
  const root = state.root;
  root.innerHTML = '';
  await renderClientDetail(root, clientId);
}

// A real client has exactly one DisPatch thread titled "client-<id>". We
// only OFFER the button — never create a thread here — and only for
// non-test-mode clients (test clients never get a thread by design).
async function wireOpenThreadButton(btn, clientMeta) {
  if (!btn || !clientMeta || !clientMeta.id) return;
  const title = `client-${clientMeta.id}`;
  try {
    const threads = await api.threads ? await api.threads() : null;
    const match = Array.isArray(threads) ? threads.find((th) => th.title === title) : null;
    if (match) {
      btn.hidden = false;
      btn.addEventListener('click', () => {
        window.dispatchEvent(new CustomEvent('clients:open-thread', { detail: { threadId: match.id } }));
      });
    }
  } catch (e) { /* no thread API surface reachable, or none matches — leave hidden */ }
}

function renderHoldBanner(slot, client) {
  slot.innerHTML = '';
  if (!client || !client.hold || !client.hold.active) return;
  const hold = client.hold;
  const banner = el(`
    <div class="hold-banner" role="alert">
      <h3>■ ${escapeHtml(t('clients.security_hold') || 'Security hold')}</h3>
      <p>${escapeHtml(hold.reason || t('clients.hold_generic') || 'This client is on hold.')}</p>
      <form id="hold-clear-form">
        <input type="text" id="hold-clear-note" placeholder="${escapeHtml(t('clients.hold_note_placeholder') || 'What did you check, and why is it safe to continue?')}" required>
        <button type="submit" class="danger">${escapeHtml(t('clients.clear_hold') || 'Clear hold')}</button>
      </form>
    </div>
  `);
  banner.querySelector('#hold-clear-form').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const note = banner.querySelector('#hold-clear-note').value.trim();
    if (!note) return;
    try {
      const res = await api.practicePost(`clients/${encodeURIComponent(client.client.id)}/actions/hold-clear`, { note });
      startJob(res.job_id, t('clients.clear_hold') || 'Clear hold');
    } catch (e) {
      if (!e || e.status !== 401) alert(t('clients.hold_clear_failed') || 'Could not clear the hold. Try again.');
    }
  });
  slot.appendChild(banner);
}

const STATUS_GLYPH = { done: '✓', current: '▶', upcoming: '○' };

function renderSteps(container, client) {
  container.innerHTML = '';
  const steps = (client && client.steps) || [];
  if (!steps.length) {
    container.innerHTML = `<p class="empty-note">${escapeHtml(t('clients.no_steps') || 'This client has no steps yet.')}</p>`;
    return;
  }
  steps.forEach((step) => container.appendChild(renderStep(client, step)));
}

function renderStep(client, step) {
  const status = step.status || 'upcoming';
  const details = el(`
    <details class="step status-${escapeHtml(status)}" ${status === 'current' ? 'open' : ''}>
      <summary>
        <span class="step-glyph">${STATUS_GLYPH[status] || '○'}</span>
        <span class="step-title">${escapeHtml(step.n)}. ${escapeHtml(step.title)}</span>
        ${step.gate ? `<span class="gate-tag">${escapeHtml(step.gate)}</span>` : ''}
      </summary>
      <div class="step-body"></div>
    </details>
  `);
  const body = details.querySelector('.step-body');

  const needs = step.needs || [];
  if (needs.length) {
    const sect = el(`<div class="step-section"><h4>${escapeHtml(t('clients.needs') || 'Needs')}</h4><div class="needs-list"></div></div>`);
    const list = sect.querySelector('.needs-list');
    needs.forEach((n) => list.appendChild(renderNeedItem(client, step, n)));
    body.appendChild(sect);
  }

  const actions = step.actions || [];
  const doSect = el(`<div class="step-section"><h4>${escapeHtml(t('clients.do') || 'Do')}</h4><div class="do-actions"></div></div>`);
  const doList = doSect.querySelector('.do-actions');
  if (actions.length) {
    actions.forEach((a) => doList.appendChild(renderActionButton(client, step, a)));
  } else {
    doList.appendChild(el(`<p class="empty-note">${escapeHtml(t('clients.nothing_to_do') || 'Nothing to do here yet.')}</p>`));
  }
  body.appendChild(doSect);

  const events = step.events || [];
  const logSect = el(`<div class="step-section"><h4>${escapeHtml(t('clients.log') || 'Log')}</h4></div>`);
  if (events.length) {
    const sorted = events.slice().reverse().slice(0, 5);
    const list = el('<ul class="log-list"></ul>');
    sorted.forEach((ev) => list.appendChild(renderLogLine(ev)));
    logSect.appendChild(list);
  } else {
    logSect.appendChild(el(`<p class="empty-note">${escapeHtml(t('clients.nothing_logged') || 'Nothing logged yet.')}</p>`));
  }
  body.appendChild(logSect);

  const ifFails = step.if_fails || step.ifFails;
  if (ifFails) {
    body.appendChild(el(`
      <details class="step-section if-fails">
        <summary>${escapeHtml(t('clients.if_it_fails') || 'If it fails')}</summary>
        <p class="if-fails-body">${escapeHtml(ifFails)}</p>
      </details>
    `));
  }
  return details;
}

function renderLogLine(ev) {
  const text = typeof ev === 'string' ? ev : (ev.text || ev.message || JSON.stringify(ev));
  const time = typeof ev === 'object' && ev && ev.ts ? ev.ts : '';
  return el(`<li>${time ? `<span class="log-time">${escapeHtml(time)}</span>` : ''}${escapeHtml(text)}</li>`);
}

function renderNeedItem(client, step, need) {
  const present = !!need.present;
  const wrap = el(`
    <div class="needs-item ${present ? 'present' : 'missing'}">
      <span class="needs-glyph">${present ? '✓' : '✗'}</span>
      <span class="needs-body">
        <span class="needs-name">${escapeHtml(need.name)}</span>
        ${need.hint ? `<span class="needs-hint">${escapeHtml(need.hint)}</span>` : ''}
      </span>
    </div>
  `);
  if (!present && need.schema) {
    const formHolder = el('<div class="needs-form"></div>');
    formHolder.appendChild(buildSchemaForm(need.schema, async (values) => {
      const clientId = client.client.id;
      await api.practicePost(`clients/${encodeURIComponent(clientId)}/files/${encodeURIComponent(need.name)}`, values);
      await reloadClientDetail(clientId);
    }));
    wrap.querySelector('.needs-body').appendChild(formHolder);
  }
  return wrap;
}

function buildSchemaForm(schema, onSubmit) {
  const form = document.createElement('form');
  form.className = 'schema-form';
  const props = (schema && schema.properties) || {};
  const required = new Set((schema && schema.required) || []);
  Object.entries(props).forEach(([key, spec]) => {
    const field = el('<div class="field"></div>');
    const label = el(`<label>${escapeHtml(spec.title || key)}${required.has(key) ? ' *' : ''}</label>`);
    field.appendChild(label);
    let input;
    if (spec.type === 'boolean') {
      input = el('<input type="checkbox">');
    } else if (spec.enum) {
      input = document.createElement('select');
      spec.enum.forEach((opt) => input.appendChild(el(`<option value="${escapeHtml(opt)}">${escapeHtml(opt)}</option>`)));
    } else if (spec.format === 'textarea' || spec.type === 'object') {
      input = el('<textarea rows="3"></textarea>');
    } else {
      input = el(`<input type="text">`);
    }
    input.name = key;
    if (required.has(key) && input.tagName !== 'SELECT') input.required = true;
    field.appendChild(input);
    form.appendChild(field);
  });
  const submitBtn = el(`<button type="submit" class="primary">${escapeHtml(t('clients.save') || 'Save')}</button>`);
  form.appendChild(submitBtn);
  const err = el('<p class="empty-note needs-form-error hidden"></p>');
  form.appendChild(err);
  form.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const values = {};
    Object.entries(props).forEach(([key, spec]) => {
      const input = form.elements.namedItem(key);
      if (!input) return;
      if (spec.type === 'boolean') values[key] = !!input.checked;
      else values[key] = input.value;
    });
    submitBtn.disabled = true;
    try {
      await onSubmit(values);
    } catch (e) {
      err.textContent = (e && e.message) || (t('clients.save_failed') || 'Could not save.');
      err.classList.remove('hidden');
      submitBtn.disabled = false;
    }
  });
  return form;
}

function renderActionButton(client, step, action) {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = action.primary ? 'primary' : 'secondary';
  btn.textContent = action.label || action.id;
  btn.addEventListener('click', () => {
    if (action.confirm) openConfirmModal(client, action);
    else runAction(client, action, {});
  });
  return btn;
}

async function runAction(client, action, extraBody) {
  const clientId = client && client.client && client.client.id;
  try {
    const res = await api.practicePost(`clients/${encodeURIComponent(clientId)}/actions/${encodeURIComponent(action.id)}`, extraBody || {});
    startJob(res.job_id, action.label || action.id);
  } catch (e) {
    if (!e || e.status !== 401) alert((e && e.message) || (t('clients.action_failed') || 'That action failed to start.'));
  }
}

function openConfirmModal(client, action) {
  const modal = mountModal(`
    <h3>${escapeHtml(action.label || action.id)}</h3>
    ${action.confirm_text ? `<p>${escapeHtml(t('clients.confirm_before') || 'Confirm this before it runs:')}</p><div class="confirm-hash">${escapeHtml(action.confirm_text)}</div>` : `<p>${escapeHtml(t('clients.are_you_sure') || 'Are you sure?')}</p>`}
    <div class="modal-actions">
      <button type="button" class="secondary" data-close>${escapeHtml(t('common.cancel') || 'Cancel')}</button>
      <button type="button" class="primary" id="confirm-approve">${escapeHtml(t('clients.approve') || 'Approve')}</button>
    </div>
  `);
  modal.querySelector('#confirm-approve').addEventListener('click', async () => {
    closeModal();
    await runAction(client, action, { confirm: true });
  });
}

function mountModal(innerHtml) {
  closeModal();
  const backdrop = el(`<div class="modal-backdrop clients-modal" role="dialog" aria-modal="true"><div class="modal-card">${innerHtml}</div></div>`);
  backdrop.addEventListener('click', (ev) => { if (ev.target === backdrop) closeModal(); });
  backdrop.querySelectorAll('[data-close]').forEach((b) => b.addEventListener('click', closeModal));
  document.body.appendChild(backdrop);
  state.modal = backdrop;
  // This backdrop is built at RUNTIME, so main.js's modal guard — which only
  // observes the .modal-backdrop nodes present when the page booted — never
  // saw it. It therefore had no inert, no focus move-in and no focus return,
  // and closing the Clients pane or locking the device left it on screen with
  // its document-level keydown listener still bound.
  state.modalReturnFocus = document.activeElement;
  state.modalInert = acquireInert();
  document.addEventListener('keydown', modalEscHandler);
  // Focus the first control rather than the card, so a keyboard user can act
  // immediately and a screen reader reads the dialog on the way in.
  const first = backdrop.querySelector('button, [href], input, select, textarea');
  if (first) { try { first.focus(); } catch { /* not focusable yet */ } }
  return backdrop.querySelector('.modal-card');
}
function modalEscHandler(e) { if (e.key === 'Escape') closeModal(); }

/** Close the confirm dialog. Exported so the pane's own teardown can reach it:
 *  hiding the Clients view left this backdrop parented to <body>. */
export function closeClientsModal() { closeModal(); }

function closeModal() {
  if (state.modal) {
    state.modal.remove();
    state.modal = null;
    document.removeEventListener('keydown', modalEscHandler);
  }
  // Outside the `if`: a stuck inert hold is an app nobody can click, so it is
  // always released even if the node was already gone.
  if (state.modalInert) { releaseInert(state.modalInert); state.modalInert = null; }
  const ret = state.modalReturnFocus; state.modalReturnFocus = null;
  if (ret && ret.focus && ret.isConnected) { try { ret.focus(); } catch { /* gone */ } }
}

// --------------------------------------------------------------------------
// Jobs: sticky strip + polling, same verdict vocabulary as the practice GUI
// --------------------------------------------------------------------------

function startJob(jobId, label) {
  if (!jobId) return;
  state.stopped = false;
  if (state.jobTimer) { clearTimeout(state.jobTimer); state.jobTimer = null; }
  state.job = { id: jobId, label, startedAt: Date.now(), status: 'running' };
  renderJobStrip();
  pollJob();
}

async function pollJob() {
  const job = state.job;
  if (!job) return;
  // stopClientsPolling() clears state.job, and this re-entry check is what
  // makes that stick: a poll already awaiting its fetch when the pane closed
  // would otherwise come back and reschedule itself regardless.
  if (state.stopped) return;
  if (Date.now() - job.startedAt > JOB_TIMEOUT_MS) {
    job.status = 'timeout';
    renderJobStrip();
    return;
  }
  try {
    const res = await api.practiceGet(`jobs/${encodeURIComponent(job.id)}`);
    job.status = res.status; job.rc = res.rc; job.verdict = res.verdict; job.tail = res.tail;
    renderJobStrip();
    if (res.status === 'done') { await refreshClientsCurrentView(); return; }
  } catch (e) {
    if (e && e.status === 401) return;
  }
  if (state.stopped) return;
  state.jobTimer = setTimeout(pollJob, JOB_POLL_MS);
}

function verdictClass(verdict) {
  const v = String(verdict || '').toUpperCase();
  if (v.startsWith('OK') || v.startsWith('PASS')) return 'verdict-ok';
  if (v.startsWith('REFUSED')) return 'verdict-refused';
  if (v.startsWith('HOLD') || v.startsWith('FAIL')) return 'verdict-hold';
  if (v.startsWith('WAITING FOR GATE')) return 'verdict-waiting';
  return 'verdict-other';
}

function renderJobStrip() {
  const strip = state.strip;
  if (!strip) return;
  const job = state.job;
  if (!job) { strip.classList.add('hidden'); strip.innerHTML = ''; return; }
  strip.classList.remove('hidden');
  if (job.status === 'running') {
    strip.innerHTML = `<div class="job-headline"><span class="job-spinner" aria-hidden="true"></span><span>${escapeHtml(t('clients.working') || 'Working…')} ${escapeHtml(job.label || '')}</span></div>`;
    return;
  }
  if (job.status === 'timeout') {
    strip.innerHTML = `<div class="job-headline"><span class="verdict-pill verdict-hold">${escapeHtml(t('clients.timed_out') || 'TIMED OUT')}</span><button type="button" class="close-job secondary">${escapeHtml(t('common.dismiss') || 'Dismiss')}</button></div>`;
    strip.querySelector('.close-job').addEventListener('click', () => { state.job = null; renderJobStrip(); });
    return;
  }
  const verdict = job.verdict || (job.rc === 0 ? 'OK' : 'FAIL');
  strip.innerHTML = `
    <div class="job-headline">
      <span class="verdict-pill ${verdictClass(verdict)}">${escapeHtml(verdict)}</span>
      <span>${escapeHtml(job.label || '')}</span>
      <button type="button" class="close-job secondary">${escapeHtml(t('common.dismiss') || 'Dismiss')}</button>
    </div>
    <details class="job-console"><summary>${escapeHtml(t('clients.console_output') || 'Console output')}</summary><pre>${escapeHtml(job.tail || '(no output)')}</pre></details>
  `;
  strip.querySelector('.close-job').addEventListener('click', () => { state.job = null; renderJobStrip(); });
}
