// Host dashboard — the operator's "is this install healthy?" panel.
//
// Same contract as reactions.js: this module holds no app state of its own. It
// is handed a `ctx` of shared helpers (toast, decoy check, the lock handler)
// and everything else it needs it fetches itself.
//
// Deliberately self-contained on the network side too: it does its own fetch
// rather than going through api.js, so adding the dashboard touches no existing
// frontend file. The error shape it throws matches api.js's ("<status>: detail")
// so the toasts read the same as everywhere else.
//
// Three rules this panel is built around, because it is what you open when the
// box is already misbehaving:
//   1. It polls ONLY while open, and never while the tab is hidden.
//   2. A failed refresh never blanks the page — the last good reading stays up
//      under a "can't reach the server" strip. Stale data with a timestamp
//      beats an empty page.
//   3. The expensive checks (full database scan) happen on a button press, not
//      on the poll.
//
// It used to build its own .modal-backdrop off the gear rail's 🩺. It is now a
// PANE inside Settings (the 🩺 Health tab), so the shell it builds is the body
// and a footer group — the dialog, its header, Escape and the focus trap all
// belong to the Settings modal. Rule 1 is unchanged and now has a second edge
// to hold: leaving the tab is as much a close as closing the modal, and both
// call unmountDashboard().

// `el` only — no i18n import. This module takes everything it needs through
// `ctx` (see reactions.js for the same contract), including the translator, so
// it stays a leaf with exactly one dependency.
//
// What IS and IS NOT translated here, deliberately:
//   * every label, card title, state word, note and toast this file writes —
//     they are ours, and they are what an operator reads;
//   * NOT the findings (title/detail/fix). Those are generated server-side in
//     English and arrive that way; translating their frame and not their text
//     would read worse than leaving the pair consistent;
//   * NOT the numeric formatting (bytes, durations, clock). Unit symbols are
//     near-universal and the values are diagnostic — see docs/dashboard.md.
import { el } from './util.js?v=10';

// ===================== State =====================

const POLL_MS = 5000;
// 200 matches the server's own default, so the select opens on a real option
// rather than an empty box.
const LOG_LINE_CHOICES = [100, 200, 500, 2000];

const D = {
  open: false,
  data: null,          // last good payload
  error: null,         // last refresh error (data stays, banner goes stale)
  fetchedAt: 0,
  loading: false,
  deepBusy: false,
  logOpen: false,
  logLines: 200,
  log: null,
  timer: null,
};

// Injected by main.js.
let ctx = {
  // Handed in by main.js. The fallback returns the key rather than throwing,
  // so a mount that forgot it is obvious on screen instead of fatal.
  t: (key) => key,
  toast: () => {},
  isDecoy: () => false,
  onLocked: () => {},        // main.js swaps in handleLocked
  // Settings owns the modal now: the old openDashboard() entry point routes
  // through this rather than opening anything of its own.
  openSettingsTab: () => {},
};

// ===================== Tiny REST client =====================

async function getJSON(url) {
  const r = await fetch(url, { headers: { Accept: 'application/json' } });
  if (!r.ok) {
    let body = null;
    try { body = await r.json(); } catch { /* not JSON — use the status text */ }
    let detail = (body && (body.detail || body.error)) || r.statusText;
    if (detail && typeof detail === 'object') {
      try { detail = JSON.stringify(detail); } catch { detail = String(detail); }
    }
    const err = new Error(`${r.status}: ${detail}`);
    err.status = r.status;
    err.locked = r.status === 401 || !!(body && (body.locked || body.decoy));
    throw err;
  }
  return r.json();
}

const cleanErr = (e) => String((e && e.message) || e).replace(/^\d+:\s*/, '');

const T = (key, vars) => ctx.t(key, vars);

// ===================== Formatting =====================

const LEVEL_KEY = { ok: 'dash.level_ok', warn: 'dash.level_warn', fail: 'dash.level_fail' };
const LEVEL_GLYPH = { ok: '✓', warn: '!', fail: '✕' };

function fmtBytes(n) {
  if (n === null || n === undefined || isNaN(n)) return '—';
  if (n < 1024) return `${n} B`;
  let v = Number(n);
  for (const u of ['KB', 'MB', 'GB', 'TB']) {
    v /= 1024;
    if (v < 1024) return `${v < 10 ? v.toFixed(1) : Math.round(v)} ${u}`;
  }
  return `${Math.round(v)} PB`;
}

function fmtDuration(s) {
  if (s === null || s === undefined) return '—';
  const n = Math.max(0, Math.floor(s));
  if (n < 60) return `${n}s`;
  if (n < 3600) return `${Math.floor(n / 60)}m`;
  if (n < 86400) return `${Math.floor(n / 3600)}h ${Math.floor((n % 3600) / 60)}m`;
  return `${Math.floor(n / 86400)}d ${Math.floor((n % 86400) / 3600)}h`;
}

function fmtAgo(ms) {
  const s = Math.max(0, Math.round((Date.now() - ms) / 1000));
  if (s < 2) return T('dash.just_now');
  if (s < 60) return T('dash.secs_ago', { count: s });
  return T('dash.mins_ago', { count: Math.floor(s / 60) });
}

function fmtPercent(ratio) {
  return (ratio === null || ratio === undefined) ? '—' : `${Math.round(ratio * 100)}%`;
}

// One blob store's line. Sizes are null (not 0) while a walk is in flight, and
// "—" has to mean "not measured" — showing 0 files for an unmeasured directory
// is the same lie the server-side placeholder exists to avoid.
function fmtStore(entry) {
  if (!entry) return '—';
  // `files` is null (not 0) while a walk is in flight — "0 files" would be a
  // lie, so an unmeasured count stays the em dash it always was.
  const files = entry.files == null ? '—' : T('dash.n_files', { count: entry.files });
  return `${fmtBytes(entry.bytes)} · ${files}`;
}

function fmtClock(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  return isNaN(d) ? '—' : d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' });
}

// A row's value can carry its own severity tint without becoming a finding.
function row(label, value, tone) {
  return el('div', { class: 'dash-row' }, [
    el('span', { class: 'dash-row-label', text: label }),
    el('span', { class: `dash-row-val${tone ? ` tone-${tone}` : ''}`, text: String(value) }),
  ]);
}

// ===================== Shell =====================

let root = null;      // the Settings pane we render into
let foot = null;      // the Settings footer group we own while the tab is up
let nodes = {};       // stable references we repaint into

// Build the body once and keep it: `nodes` are the handles every paint writes
// through, so a rebuild on every tab visit would throw away the last good
// reading (rule 2) along with the log the operator had open.
function buildShell() {
  if (nodes.body) return;

  // ---- banner ----
  nodes.bannerDot = el('span', { class: 'dash-banner-dot', 'aria-hidden': 'true' });
  nodes.bannerTitle = el('div', { class: 'dash-banner-title', text: T('dash.checking') });
  nodes.bannerSub = el('div', { class: 'dash-banner-sub', text: '' });
  nodes.bannerCounts = el('div', { class: 'dash-banner-counts' });
  nodes.banner = el('div', { class: 'dash-banner level-unknown' }, [
    nodes.bannerDot,
    el('div', { class: 'dash-banner-text' }, [nodes.bannerTitle, nodes.bannerSub]),
    nodes.bannerCounts,
  ]);
  // Announce status changes without stealing focus.
  nodes.banner.setAttribute('role', 'status');
  nodes.banner.setAttribute('aria-live', 'polite');

  nodes.stale = el('div', { class: 'dash-stale hidden' });

  // ---- body ----
  nodes.cards = el('div', { class: 'dash-cards' });
  nodes.findings = el('div', { class: 'dash-findings' });

  // ---- log viewer ----
  nodes.logToggle = el('button', { class: 'dash-log-toggle', text: `📜 ${T('dash.log_show')}` });
  nodes.logToggle.setAttribute('aria-expanded', 'false');
  nodes.logToggle.addEventListener('click', () => toggleLog());

  nodes.logSelect = el('select', { class: 'dash-log-lines', 'aria-label': T('dash.lines_aria') });
  for (const n of LOG_LINE_CHOICES) {
    nodes.logSelect.append(el('option', { value: String(n), text: T('dash.n_lines', { count: n }) }));
  }
  nodes.logSelect.value = String(D.logLines);
  nodes.logSelect.addEventListener('change', () => {
    D.logLines = Number(nodes.logSelect.value) || 200;
    refreshLog();
  });
  nodes.logRefresh = el('button', { class: 'btn-secondary dash-mini', text: `⟳ ${T('dash.refresh')}` });
  nodes.logRefresh.addEventListener('click', () => refreshLog());
  nodes.logPath = el('code', { class: 'dash-log-path', text: '' });
  nodes.logBody = el('pre', { class: 'dash-log-body', tabindex: '0' });
  nodes.logPanel = el('section', { class: 'dash-log hidden' }, [
    el('div', { class: 'dash-log-bar' }, [nodes.logPath, nodes.logSelect, nodes.logRefresh]),
    nodes.logBody,
  ]);

  nodes.body = el('div', { class: 'dash-body' }, [
    nodes.banner, nodes.stale, nodes.cards,
    nodes.checksHead = el('h3', { class: 'dash-h3', text: T('dash.checks') }),
    nodes.findings,
    el('div', { class: 'dash-log-head' }, [nodes.logToggle]),
    nodes.logPanel,
  ]);

  // ---- footer group (lives in the Settings footer while this tab is up) ----
  // "Done" is gone with the standalone modal: the dialog's own ✕ closes it, and
  // a second button that only ever did the same thing is the dead button the
  // contextual footer exists to avoid.
  nodes.deepBtn = el('button', { class: 'btn-secondary', text: `🔬 ${T('dash.deep_run')}` });
  nodes.deepBtn.title = T('dash.deep_title');
  nodes.deepBtn.addEventListener('click', () => runDeep());
  nodes.updated = el('span', { class: 'dash-updated muted', text: '' });
}

// ===================== Mount / unmount =====================

/** Show the dashboard in `host` and start polling.
 *
 *  Called by main.js when the 🩺 Health tab becomes visible — and ONLY then.
 *  `footHost` is the footer group for that tab; the deep-check button and the
 *  "updated 3s ago" line move into it so the pane itself is pure readout.
 */
export async function mountDashboard(host, footHost, next = {}) {
  ctx = { ...ctx, ...next };
  if (ctx.isDecoy()) { ctx.toast(T('dash.locked'), true); return; }
  buildShell();
  root = host;
  foot = footHost || null;
  if (!nodes.body.isConnected || nodes.body.parentNode !== root) root.append(nodes.body);
  if (foot) foot.append(nodes.deepBtn, nodes.updated);
  D.open = true;
  // repaintDashboard, not paint(): buildShell() runs ONCE, so a mount after a
  // language switch would otherwise show the previous language's labels until
  // something else redrew them (the deep-check button in the footer kept its
  // old text across two language changes).
  repaintDashboard();            // show whatever we had, immediately
  await refresh();
  startPolling();
}

/** Leave the tab (or close Settings): stop polling, let go of the host.
 *
 *  The DOM stays built and `D.data` stays warm, so coming back paints the last
 *  reading instantly instead of an empty page — but nothing is on a timer.
 */
export function unmountDashboard() {
  D.open = false;
  stopPolling();
  if (nodes.body && nodes.body.isConnected) nodes.body.remove();
  if (nodes.deepBtn && nodes.deepBtn.isConnected) nodes.deepBtn.remove();
  if (nodes.updated && nodes.updated.isConnected) nodes.updated.remove();
  root = null;
  foot = null;
}

/** The old gear-rail entry point. Kept so every caller (and any deep link)
 *  keeps working — it now opens Settings on the Health tab. */
export async function openDashboard(next = {}) {
  ctx = { ...ctx, ...next };
  if (ctx.isDecoy()) { ctx.toast(T('dash.locked'), true); return; }
  ctx.openSettingsTab('health');
}

export function closeDashboard() { unmountDashboard(); }

/** Repaint every string after a language switch.
 *
 *  buildShell() runs ONCE and is deliberately never rebuilt (rule 2: the last
 *  good reading and an open log survive a tab visit), so the labels it wrote
 *  are frozen in whatever language was active then. main.js calls this from
 *  reRenderForLocale when the Health tab is up — the same treatment the
 *  Reaction Manager gets, and for the same reason.
 */
export function repaintDashboard() {
  if (!nodes.body) return;
  nodes.logToggle.textContent = `📜 ${T(D.logOpen ? 'dash.log_hide' : 'dash.log_show')}`;
  nodes.logSelect.setAttribute('aria-label', T('dash.lines_aria'));
  Array.from(nodes.logSelect.options).forEach((opt) => {
    opt.textContent = T('dash.n_lines', { count: Number(opt.value) });
  });
  nodes.logRefresh.textContent = `⟳ ${T('dash.refresh')}`;
  if (nodes.checksHead) nodes.checksHead.textContent = T('dash.checks');
  if (nodes.deepBtn) {
    nodes.deepBtn.textContent = `🔬 ${T(D.deepBusy ? 'dash.deep_running' : 'dash.deep_run')}`;
    nodes.deepBtn.title = T('dash.deep_title');
  }
  // Cards memoise on their rendered values, so a language switch alone would
  // not redraw them — drop the signatures and let paint() rebuild.
  cardSig.clear();
  paint();
  paintLog();
}

function startPolling() {
  // Always clear first: two starts must never leave two intervals running.
  stopPolling();
  // The panel can be gone by the time the opening refresh resolves — closed by
  // hand, or torn down because the session went limited and refresh() called
  // closeDashboard(). Installing an interval then would leave one polling
  // forever with nothing to clear it, which is the leak this guard exists for.
  if (!D.open) return;
  D.timer = setInterval(() => {
    // Never poll a panel nobody is looking at — a backgrounded phone tab would
    // otherwise wake the server every 5s forever.
    if (!D.open || document.hidden) return;
    refresh();
  }, POLL_MS);
}

function stopPolling() {
  if (D.timer) clearInterval(D.timer);
  D.timer = null;
}

// ===================== Data =====================

async function refresh() {
  if (D.loading) return;
  D.loading = true;
  paintUpdated();
  try {
    const data = await getJSON('/api/dashboard');
    D.data = data;
    D.error = null;
    D.fetchedAt = Date.now();
  } catch (e) {
    D.error = cleanErr(e);
    // Locked out mid-session: hand off to main.js and get out of the way.
    if (e.locked || e.status === 403) {
      stopPolling();
      closeDashboard();
      ctx.onLocked();
      return;
    }
  } finally {
    D.loading = false;
  }
  paint();
}

async function runDeep() {
  if (D.deepBusy) return;
  D.deepBusy = true;
  nodes.deepBtn.disabled = true;
  nodes.deepBtn.textContent = `🔬 ${T('dash.deep_running')}`;
  try {
    const data = await getJSON('/api/dashboard/deep');
    D.data = data;
    D.error = null;
    D.fetchedAt = Date.now();
    ctx.toast(T(data.database?.check?.ok === false
      ? 'dash.deep_done_problem' : 'dash.deep_done'));
  } catch (e) {
    ctx.toast(e.status === 409
      ? T('dash.deep_busy')
      : T('dash.deep_failed', { error: cleanErr(e) }), true);
  } finally {
    D.deepBusy = false;
    nodes.deepBtn.disabled = false;
    nodes.deepBtn.textContent = `🔬 ${T('dash.deep_run')}`;
    paint();
  }
}

function toggleLog() {
  D.logOpen = !D.logOpen;
  nodes.logPanel.classList.toggle('hidden', !D.logOpen);
  nodes.logToggle.setAttribute('aria-expanded', String(D.logOpen));
  nodes.logToggle.textContent = `📜 ${T(D.logOpen ? 'dash.log_hide' : 'dash.log_show')}`;
  if (D.logOpen && !D.log) refreshLog();
}

async function refreshLog() {
  if (!D.logOpen) return;
  nodes.logBody.textContent = T('dash.loading');
  try {
    D.log = await getJSON(`/api/dashboard/logs?lines=${encodeURIComponent(D.logLines)}`);
  } catch (e) {
    D.log = { available: false, reason: cleanErr(e), lines: [] };
  }
  paintLog();
}

// ===================== Paint =====================

function paint() {
  if (!root) return;
  paintBanner();
  paintCards();
  paintFindings();
  paintUpdated();
}

function paintBanner() {
  const d = D.data;
  const level = d ? d.status : 'unknown';
  nodes.banner.className = `dash-banner level-${level}`;
  nodes.bannerDot.textContent = LEVEL_GLYPH[level] || '…';
  nodes.bannerTitle.textContent = d
    ? (LEVEL_KEY[level] ? T(LEVEL_KEY[level]) : T('dash.level_unknown'))
    : T('dash.checking');
  // `summary` is the server's own sentence (English, like the findings).
  nodes.bannerSub.textContent = d ? d.summary : T('dash.checking_sub');

  nodes.bannerCounts.innerHTML = '';
  if (d && d.counts) {
    for (const lv of ['fail', 'warn', 'ok']) {
      const n = d.counts[lv] || 0;
      if (!n) continue;
      nodes.bannerCounts.append(el('span', {
        class: `dash-count level-${lv}`,
        text: T(`dash.count_${lv}`, { count: n }),
      }));
    }
  }

  const stale = !!D.error;
  nodes.stale.classList.toggle('hidden', !stale);
  if (stale) {
    nodes.stale.textContent = D.data
      ? `⚠ ${T('dash.stale', { error: D.error, ago: fmtAgo(D.fetchedAt) })}`
      : `⚠ ${T('dash.stale_no_data', { error: D.error })}`;
  }
}

function paintUpdated() {
  if (!nodes.updated) return;
  const bits = [];
  if (D.loading) bits.push(T('dash.refreshing'));
  else if (D.fetchedAt) bits.push(T('dash.updated', { ago: fmtAgo(D.fetchedAt) }));
  if (D.data && D.data.deep) bits.push(T('dash.deep_check'));
  if (D.data && typeof D.data.took_ms === 'number') bits.push(`${D.data.took_ms}ms`);
  nodes.updated.textContent = bits.join(' · ');
}

// Cards are cheap to rebuild, but rebuilding identical DOM every 5s kills text
// selection mid-copy — so each card only redraws when its own values changed.
const cardSig = new Map();

function card(key, title, rows, note) {
  const sig = JSON.stringify([title, rows.map((r) => r && [r.l, r.v, r.t]), note]);
  const existing = nodes.cards.querySelector(`[data-card="${key}"]`);
  if (existing && cardSig.get(key) === sig) return existing;
  cardSig.set(key, sig);
  const node = el('section', { class: 'dash-card', dataset: { card: key } }, [
    el('h4', { class: 'dash-card-title', text: title }),
    ...rows.filter(Boolean).map((r) => row(r.l, r.v, r.t)),
    ...(note ? [el('p', { class: 'dash-card-note muted', text: note })] : []),
  ]);
  if (existing) existing.replaceWith(node);
  return node;
}

function paintCards() {
  const d = D.data;
  if (!d) return;
  const p = d.process || {};
  const s = d.storage || {};
  const db = d.database || {};
  const c = d.connections || {};
  const a = d.agent || {};
  const disk = s.disk || {};
  const backup = db.backup || {};
  const bind = p.bind || {};

  // Never present the configured host as the live one: the server says where
  // the value came from, and the label has to match. `caveat` is the server's
  // own words for what it could not confirm.
  const bindObserved = bind.source === 'observed';
  const bindLabel = T(bindObserved ? 'dash.listening_on' : 'dash.configured_host');
  const bindValue = p.host
    ? `${p.host}:${p.port ?? '—'}${bind.caveat ? ` (${bind.caveat})` : ''}`
    : '—';

  const storeNote = [];
  if (s.data_dir) storeNote.push(T('dash.data_dir', { path: s.data_dir }));
  if (s.measuring) storeNote.push(T('dash.note_measuring'));
  else if (s.complete === false) storeNote.push(T('dash.note_approx'));
  if (s.reactions) storeNote.push(T('dash.note_reactions'));

  // Two words, three keys: `yes` reads as a plain fact, `no_emphatic` is the
  // shouted one that marks a broken state (an unwritable data directory, a
  // missing agent binary) and is a separate key so a translator can shout in
  // their own language rather than uppercase ours.
  const yes = T('dash.yes'), noBad = T('dash.no_emphatic');

  const built = [
    card('process', `⚙ ${T('dash.card_process')}`, [
      { l: T('dash.version'), v: p.version || '—' },
      { l: T('dash.uptime'), v: fmtDuration(p.uptime_seconds) },
      { l: T('dash.memory'), v: fmtBytes(p.rss_bytes) + (p.rss_is_peak ? ` ${T('dash.peak')}` : '') },
      { l: T('dash.cpu'), v: p.cpu_percent === null || p.cpu_percent === undefined
        ? '—' : `${p.cpu_percent}%` },
      { l: T('dash.python'), v: p.python || '—' },
      { l: T('dash.platform'), v: p.platform || '—' },
      { l: T('dash.container'), v: p.container || T('dash.bare_metal') },
      { l: T('dash.user'), v: p.root ? T('dash.root_uid') : (p.user || (p.uid ?? '—')),
        t: p.root && !p.container ? 'warn' : null },
      { l: bindLabel, v: bindValue, t: bind.verified === false ? 'warn' : null },
      { l: T('dash.pid'), v: p.pid ?? '—' },
    ]),

    card('storage', `💾 ${T('dash.card_storage')}`, [
      { l: T('dash.free_space'), v: T('dash.free_of', {
          free: fmtBytes(disk.free_bytes), total: fmtBytes(disk.total_bytes) }),
        t: diskTone(disk) },
      { l: T('dash.database'), v: fmtBytes(s.db_bytes) },
      { l: T('dash.wal'), v: fmtBytes(s.wal_bytes) },
      { l: T('dash.images'), v: fmtStore(s.media) },
      { l: T('dash.files'), v: fmtStore(s.files) },
      { l: T('dash.backups'), v: fmtBytes(s.backups?.bytes) },
      // Outside the cap maths on purpose (the server counts media+files only),
      // but the fastest-growing directory on the box — spent reaction images
      // are kept forever by design, so it has to be visible somewhere.
      { l: T('dash.reactions'), v: fmtStore(s.reactions) },
      { l: T('dash.against_cap'), v: !s.cap_enabled ? T('dash.no_cap')
        : (s.cap_ratio === null || s.cap_ratio === undefined)
          ? T('dash.cap_measuring', { cap: fmtBytes(s.cap_bytes) })
          : T(s.blob_complete === false ? 'dash.cap_at_least' : 'dash.cap_of', {
              used: fmtBytes(s.blob_bytes), cap: fmtBytes(s.cap_bytes),
              percent: fmtPercent(s.cap_ratio) }),
        t: s.cap_ratio >= 1 ? 'fail' : (s.cap_ratio >= (s.cap_warn_ratio ?? 0.8) ? 'warn' : null) },
      { l: T('dash.writable'), v: s.writable === false ? noBad : yes,
        t: s.writable === false ? 'fail' : null },
    ], storeNote.join(' · ')),

    card('database', `🗄 ${T('dash.card_database')}`, [
      { l: T('dash.integrity'), v: db.check
        ? `${db.check.ok ? T('dash.integrity_ok') : T('dash.integrity_failed')} (${db.check.kind})` : '—',
        t: db.check && !db.check.ok ? 'fail' : null },
      { l: T('dash.journal_mode'), v: db.journal_mode || '—',
        t: db.journal_mode && db.journal_mode.toLowerCase() !== 'wal' ? 'warn' : null },
      { l: T('dash.pages'), v: db.page_count ?? '—' },
      { l: T('dash.free_pages'), v: db.freelist_pages ?? '—' },
      { l: T('dash.search_index'), v: db.fts_enabled ? T('dash.on') : T('dash.off_fallback') },
      db.messages !== undefined
        ? { l: T('dash.messages'), v: T('dash.messages_in_threads', {
              messages: db.messages, threads: db.threads }) } : null,
      { l: T('dash.last_backup'), v: backup.last_backup_epoch
        ? T('dash.backup_when', { clock: fmtClock(backup.last_backup_at), ago: fmtDuration(
            Math.max(0, Date.now() / 1000 - backup.last_backup_epoch)) })
        : T('dash.never'),
        t: backupTone(backup) },
      { l: T('dash.snapshots'), v: backup.count ?? '—' },
    ]),

    card('connections', `🔌 ${T('dash.card_connections')}`, [
      { l: T('dash.total'), v: c.total ?? '—' },
      { l: T('dash.full_access'), v: c.tiers_available ? c.full : T('dash.unknown') },
      { l: T('dash.limited'), v: c.tiers_available ? c.limited : T('dash.unknown') },
    ], T('dash.note_connections')),

    card('agent', `🤖 ${T('dash.card_agent')}`, [
      { l: T('dash.configured'), v: a.configured ? (a.bin || yes) : T('dash.none') },
      // Direct LLM providers are the OTHER way a bot can answer, and they need
      // none of the rows below. Listed first so "Configured: none" is read in
      // the right light — with a provider connected it is a choice, not a gap.
      a.api_bots ? { l: T('dash.direct_providers'),
                     v: T('dash.n_connected', { count: a.api_bots }) } : null,
      a.configured ? { l: T('dash.found'), v: a.present ? (a.path || yes) : noBad,
        t: a.present ? null : 'warn' } : null,
      a.configured ? { l: T('dash.executable'), v: a.executable ? yes : noBad,
        t: a.executable ? null : 'warn' } : null,
      a.configured && a.version ? { l: T('dash.version'), v: a.version } : null,
      // The CLI being on disk says nothing about whether a turn can be
      // delivered — a dead gateway is what "nobody ever replies" looks like.
      a.configured && a.gateway
        ? { l: T('dash.gateway'), v: `${a.gateway.host}:${a.gateway.port} — `
            + T(a.gateway.reachable ? 'dash.answering' : 'dash.not_answering'),
            t: a.gateway.reachable ? null : 'warn' }
        : null,
    ], a.configured ? null
      : T(a.api_bots ? 'dash.note_agent_providers' : 'dash.note_agent_none')),
  ];

  // Append any card that isn't in the DOM yet, in declaration order.
  for (const node of built) {
    if (!node.isConnected) nodes.cards.append(node);
  }
}

function diskTone(disk) {
  const free = disk.free_bytes;
  const ratio = disk.free_ratio;
  if (free === undefined || free === null) return null;
  if (free < 200 * 1024 * 1024 || (ratio !== null && ratio < 0.02)) return 'fail';
  if (free < 1024 * 1024 * 1024 || (ratio !== null && ratio < 0.10)) return 'warn';
  return null;
}

function backupTone(backup) {
  if (!backup) return null;
  if (backup.last_backup_ok === false) return 'fail';
  if (!backup.last_backup_epoch && backup.interval_seconds) return 'warn';
  return null;
}

// Findings redraw only when something actually changed, for the same reason
// the cards do — and because the list is what people read.
let findingsSig = '';

function paintFindings() {
  const list = (D.data && D.data.findings) || [];
  const sig = JSON.stringify(list.map((f) => [f.id, f.level, f.detail]));
  if (sig === findingsSig) return;
  findingsSig = sig;

  nodes.findings.innerHTML = '';
  if (!list.length) {
    nodes.findings.append(el('div', { class: 'dash-empty muted', text: T('dash.no_checks') }));
    return;
  }
  // Worst first: an operator should never have to scroll past ten greens to
  // find the red one.
  const order = { fail: 0, warn: 1, ok: 2 };
  for (const f of [...list].sort((a, b) => (order[a.level] ?? 3) - (order[b.level] ?? 3))) {
    const item = el('div', { class: `dash-finding level-${f.level}` });
    item.append(el('span', { class: 'dash-finding-pill', text: LEVEL_GLYPH[f.level] || '?' }));
    const main = el('div', { class: 'dash-finding-main' }, [
      el('div', { class: 'dash-finding-title', text: f.title }),
      el('div', { class: 'dash-finding-detail', text: f.detail }),
    ]);
    if (f.fix) {
      main.append(el('div', { class: 'dash-finding-fix' }, [
        el('span', { class: 'dash-fix-glyph', 'aria-hidden': 'true', text: '🛠' }),
        el('span', { text: f.fix }),
      ]));
    }
    // The id is what docs/dashboard.md is indexed by — show it, quietly.
    main.append(el('code', { class: 'dash-finding-id', text: f.id }));
    item.append(main);
    nodes.findings.append(item);
  }
}

function paintLog() {
  const l = D.log;
  if (!l) { nodes.logBody.textContent = ''; return; }
  nodes.logPath.textContent = l.path || '';
  if (l.available === false) {
    nodes.logBody.textContent = l.reason || T('dash.no_log');
    nodes.logBody.classList.add('dash-log-note');
    return;
  }
  nodes.logBody.classList.remove('dash-log-note');
  nodes.logBody.textContent = (l.lines || []).join('\n');
  nodes.logBody.scrollTop = nodes.logBody.scrollHeight;
}
