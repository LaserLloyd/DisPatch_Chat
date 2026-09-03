// Reaction images — embedded in the chat.
//
// Three pieces, one module:
//   1. the EMBED  — a thread-bound reaction renders as an image card right in
//                   the message list. A live `reaction` WS frame expands the
//                   trace row, holds for its duration, then collapses it back
//                   to the one-line chip. Clicking the chip pops the picture
//                   back up in place.
//   2. the MANAGER — upload / generate / edit / delete the pack (unlocked
//                    only, the ⚡ Reactions tab in Settings). Firing is a bot
//                    trait: there is no user-facing picker, and the manager's
//                    Test button fires AS the enabled reaction bot.
//
// Only an app-wide pop (a fire with no thread to live in) still uses the old
// transient OVERLAY, which is deliberately non-focus-stealing: it captures
// clicks but never moves focus, so a reaction landing mid-sentence doesn't eat
// your keystrokes. The chat carries on underneath; dismissing takes one click.
//
// Safe Mode: the server only ever sends frames for reactions flagged `safe`,
// and 403s the image of anything else. The `safe` re-check here is the same
// belt-and-suspenders layer the message renderer uses for media.

import { api } from './api.js?v=21';
import { el } from './util.js?v=12';
import { t } from './i18n.js?v=3';
import { nimEnabled } from './nim.js?v=5';

// ===================== State =====================

const R = {
  loaded: false,
  loading: null,          // in-flight load promise (dedupes concurrent callers)
  items: [],
  settings: { default_duration_ms: 10000, queue_max: 3, enabled: true },
  canManage: false,
  canGenerate: false,
  pool: { remaining: 0 },     // rotating one-shot batch
  reactionBots: [],           // bot ids allowed to carry reactions
  queue: [],              // pending events, oldest first
  active: null,           // { event, hideTimer, startedAt, node }
  seen: new Set(),        // event_ids already shown (WS resend / double-tab guard)
  pendingExpand: new Map(), // trace message id -> { ev, at } awaiting the row render
  liveTimers: new Map(),    // trace message id -> auto-collapse timeout
};

// Injected by main.js so this module holds no app state of its own.
let ctx = {
  toast: () => {},
  isDecoy: () => false,
  threadCtx: () => ({ thread_id: null, bot_id: null }),
  onChanged: () => {},
  confirm: async (msg) => window.confirm(msg),   // main.js swaps in uiConfirm
  lightbox: () => {},                            // main.js swaps in openLightbox
  // The manager is a Settings tab now, not a modal of its own — this is how
  // the legacy openManager() entry point gets there.
  openSettingsTab: () => {},
};

const REDUCED_MOTION = () =>
  window.matchMedia('(prefers-reduced-motion: reduce)').matches;

export function initReactions(next) {
  ctx = { ...ctx, ...next };
  buildOverlay();
  wireDismiss();
}

// ===================== Pack loading =====================

export async function loadReactions(force = false) {
  if (R.loaded && !force) return R;
  if (R.loading) return R.loading;
  R.loading = (async () => {
    try {
      const r = await api.reactions();
      R.items = r.reactions || [];
      R.settings = r.settings || R.settings;
      R.canManage = !!r.can_manage;
      R.canGenerate = !!r.can_generate;
      R.pool = r.pool || { remaining: 0 };
      R.reactionBots = r.reaction_bots || [];
      R.loaded = true;
    } catch {
      // A locked/offline client just gets an empty pack.
      R.items = [];
      R.loaded = true;
    } finally {
      R.loading = null;
    }
    return R;
  })();
  return R.loading;
}

// Called on lock/unlock: the visible pack differs between Safe Mode and full.
export function resetReactions() {
  R.loaded = false;
  R.items = [];
  dismissOverlay(true);
  // The pack manager is an ADMIN surface — thumbnails of every card, the
  // generation prompt box, delete buttons, the pool's filesystem path. It lives
  // in a backdrop this module creates lazily, so main.js's closeAllOverlays
  // (which enumerates fixed dom[] keys) never saw it and it survived a lock
  // with all of that still on screen. Closing it here covers every path that
  // resets reaction state, including the drop to Safe Mode.
  closeManager();
  R.queue.length = 0;
  // Live embeds are children of the message list; drop their auto-collapse
  // timers so a collapsed row can't fire after the app was torn down.
  R.pendingExpand.clear();
  for (const timer of R.liveTimers.values()) clearTimeout(timer);
  R.liveTimers.clear();
  document.querySelectorAll('.msg.reaction-trace-row.expanded')
    .forEach((row) => row.classList.remove('expanded'));
}


// `reaction_pool` WS frame — the background loop finished a sweep.
export function handlePoolFrame(frame) {
  if (!frame || !frame.pool) return;
  R.pool = frame.pool;
  // The ready batch changed, so the cached pack/pool list is stale.
  R.loaded = false;
  if (mgrEl && !mgrEl.classList.contains('hidden')) {
    loadReactions(true).then(() => renderManager());
  }
}

// Is this bot allowed to carry reaction images? (Server enforces; this only
// drives explanatory UI notes.)
export function botHasReactions(botId) {
  return !botId || R.reactionBots.includes(botId);
}

// ===================== Overlay =====================

let ovBackdrop = null;
let ovCard = null;
let ovImg = null;
let ovTitle = null;
let ovMeta = null;
let ovBar = null;
let ovCaption = null;

function buildOverlay() {
  if (ovBackdrop) return;
  ovBackdrop = el('div', { class: 'reaction-overlay hidden' });
  ovBackdrop.setAttribute('aria-hidden', 'true');

  ovImg = el('img', { class: 'reaction-img', alt: '' });
  ovCaption = el('div', { class: 'reaction-caption' });
  ovTitle = el('span', { class: 'reaction-name' });
  ovMeta = el('span', { class: 'reaction-actor' });
  ovBar = el('span', { class: 'reaction-bar-fill' });

  ovCard = el('div', { class: 'reaction-card' }, [
    el('div', { class: 'reaction-img-wrap' }, [ovImg]),
    ovCaption,
    el('div', { class: 'reaction-foot' }, [ovMeta, ovTitle]),
    el('div', { class: 'reaction-bar' }, [ovBar]),
  ]);
  ovBackdrop.append(ovCard);
  document.body.append(ovBackdrop);

  // Click anywhere — including the card — dismisses. "Click out" in the brief,
  // but tapping the picture is the same instinct on a phone. Click-only: a
  // touch device fires touchstart AND click, and each dismiss would advance
  // the queue once (dropping a queued reaction from a burst).
  ovBackdrop.addEventListener('click', () => dismissOverlay());
}

// A reaction frame arrived over the WebSocket.
/** Age out parked expand-events whose trace row never rendered.
 *
 *  An event lands in `pendingExpand` when its picture arrives before the chat
 *  row that owns it. It was only ever removed on consumption, so an event for
 *  a thread the user never opens stayed forever — unbounded growth across a
 *  long session. PENDING_EXPAND_MS was already the intended lifetime; nothing
 *  enforced it. */
function sweepPendingExpand(now) {
  for (const [key, pend] of R.pendingExpand) {
    if (now - (pend?.at || 0) > PENDING_EXPAND_MS) R.pendingExpand.delete(key);
  }
}

export function handleReactionFrame(ev) {
  sweepPendingExpand(Date.now());
  if (!ev || !ev.reaction_id) return;
  // No-Image Mode: a reaction IS a picture, so on this device the event simply
  // does not happen — no overlay, no queue entry, no auto-collapse timer, and
  // crucially no image URL ever constructed. Returning here rather than at the
  // render layer is what keeps the promise that nothing is downloaded.
  if (nimEnabled()) return;
  if (ctx.isDecoy() && !ev.safe) return;         // server already filtered; re-check
  if (ev.event_id) {
    if (R.seen.has(ev.event_id)) return;
    R.seen.add(ev.event_id);
    if (R.seen.size > 200) R.seen = new Set([...R.seen].slice(-100));
  }
  // A pool image is consumed the moment it fires — on EVERY device, not just
  // the one that fired it. Drop the cached list so the next picker open can't
  // offer a picture that no longer exists.
  if (ev.pool) {
    R.loaded = false;
    R.pool = { ...R.pool, remaining: Math.max(0, Number(R.pool.remaining || 1) - 1) };
  }
  // Thread-bound reactions live IN the chat: the trace message (persisted
  // before this frame, so it arrives first on the same socket) is the row the
  // picture embeds in. Expand it here if it's rendered, or remember the event
  // for when the thread's messages render (the load race). A device not
  // viewing that thread simply doesn't see the reaction — it belongs to its
  // chat, not to every screen in the house.
  if (ev.thread_id && ev.trace_id) {
    const { thread_id } = ctx.threadCtx() || {};
    if (thread_id && ev.thread_id === thread_id) {
      if (!expandLiveTrace(ev)) {
        R.pendingExpand.set(ev.trace_id, { ev, at: Date.now() });
      }
    }
    return;
  }
  // App-wide pop (no thread to embed in): the transient overlay fallback.
  if (R.active) {
    const max = Math.max(1, R.settings.queue_max || 3);
    R.queue.push(ev);
    // Drop the OLDEST queued item, never the newest: a burst should leave you
    // looking at what just happened, not a backlog from ten seconds ago.
    while (R.queue.length > max) R.queue.shift();
    return;
  }
  showReaction(ev);
}

// The trace row for this event is already in the message list — expand it.
function expandLiveTrace(ev) {
  if (!ev.trace_id) return false;
  const row = findTraceRow(ev.trace_id);
  if (!row) return false;
  expandReactionRow(row, ev, { auto: true });
  return true;
}

function findTraceRow(msgId) {
  try { return document.querySelector(`.msg.reaction-trace-row[data-id="${CSS.escape(msgId)}"]`); }
  catch { return document.querySelector(`.msg.reaction-trace-row[data-id="${msgId}"]`); }
}

function showReaction(ev) {
  buildOverlay();
  const url = ev.image_url || api.reactionImageUrl(ev.reaction_id);
  const duration = Math.max(800, ev.duration_ms || R.settings.default_duration_ms || 10000);

  // Preload so the card never flashes empty. A failed load is skipped entirely
  // rather than popping a broken-image box over the chat.
  const pre = new Image();
  pre.onload = () => paint();
  pre.onerror = () => { R.active = null; next(); };
  pre.src = url;
  // Cached images resolve synchronously-ish; guard against a double paint.
  R.active = { event: ev, hideTimer: null, startedAt: 0 };

  function paint() {
    if (!R.active || R.active.event !== ev) return;
    ovImg.src = url;
    ovImg.alt = ev.name ? t('reactions.alt', { name: ev.name }) : t('reactions.alt_generic');
    ovTitle.textContent = ev.name || ev.reaction_id;
    const who = ev.actor || t(ev.actor_kind === 'agent' ? 'common.assistant' : 'common.you');
    ovMeta.textContent = `${ev.actor_kind === 'agent' ? '⚡' : '🫵'} ${who}`;
    if (ev.caption) {
      ovCaption.textContent = ev.caption;
      ovCaption.hidden = false;
    } else {
      ovCaption.textContent = '';
      ovCaption.hidden = true;
    }

    ovBackdrop.classList.remove('hidden');
    ovBackdrop.setAttribute('aria-hidden', 'false');
    // Restart the entry animation for a back-to-back reaction.
    ovCard.classList.remove('in');
    void ovCard.offsetWidth;
    ovCard.classList.add('in');

    // The timer bar is pure CSS transition — no rAF loop to leak.
    if (REDUCED_MOTION()) {
      ovBar.style.transition = 'none';
      ovBar.style.width = '100%';
    } else {
      ovBar.style.transition = 'none';
      ovBar.style.width = '100%';
      void ovBar.offsetWidth;
      ovBar.style.transition = `width ${duration}ms linear`;
      ovBar.style.width = '0%';
    }

    R.active.startedAt = Date.now();
    R.active.hideTimer = setTimeout(() => dismissOverlay(), duration);

    // Announce for screen readers without moving focus.
    const live = document.getElementById('sr-live');
    if (live) live.textContent = t('reactions.announce', { name: who, reaction: ev.name || ev.reaction_id });
  }
}

export function dismissOverlay(silent = false) {
  if (!ovBackdrop) return;
  const wasActive = !!R.active;
  if (R.active && R.active.hideTimer) clearTimeout(R.active.hideTimer);
  R.active = null;
  ovBackdrop.classList.add('hidden');
  ovBackdrop.setAttribute('aria-hidden', 'true');
  ovCard.classList.remove('in');
  ovImg.removeAttribute('src');
  // Only advance the queue when there actually WAS an overlay up — a second
  // dismiss (e.g. a queued click landing after the first) must not skip a
  // queued reaction.
  if (!silent && wasActive) next();
}

function next() {
  const ev = R.queue.shift();
  if (ev) setTimeout(() => showReaction(ev), REDUCED_MOTION() ? 0 : 180);
}


// ===================== Firing =====================

export async function fireReaction(idOrAlias, opts = {}) {
  // Fires are agent-only server-side: reactions belong to the bots. The one
  // in-app caller left is the manager's Test button, which fires AS the
  // enabled reaction bot — with none enabled there is nobody to fire as.
  const asBot = opts.bot_id || R.reactionBots[0] || null;
  if (!asBot) {
    ctx.toast(t('reactions.no_bot_enabled'), true);
    return false;
  }
  const { thread_id } = ctx.threadCtx() || {};
  try {
    await api.fireReaction({
      reaction: idOrAlias,
      thread_id: opts.thread_id !== undefined ? opts.thread_id : thread_id || null,
      bot_id: asBot,
      actor_kind: 'agent',
      caption: opts.caption || null,
      trace: opts.trace !== false,
    });
    return true;
  } catch (e) {
    // 429 is the rate limiter doing its job — say so plainly, don't shout.
    ctx.toast(String(e.message || e).replace(/^\d+:\s*/, ''), true);
    return false;
  }
}

// ===================== Overlay dismissal =====================
// (The composer picker is gone on purpose — firing is a bot trait. Only the
// dismiss-a-live-overlay keyboard wiring remains from it.)

function wireDismiss() {
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    if (R.active) { dismissOverlay(); e.stopPropagation(); }
  }, true);
  // Any keypress dismisses a live overlay — you reached for the keyboard, so
  // you're done looking. Ignore pure modifiers so Shift-to-type doesn't count.
  document.addEventListener('keydown', (e) => {
    if (!R.active) return;
    if (['Shift', 'Control', 'Alt', 'Meta'].includes(e.key)) return;
    dismissOverlay();
  });
}

// ===================== Pack manager =====================

// The host is the ⚡ Reactions pane inside Settings, handed to us by main.js
// when that tab becomes visible. It used to be a .modal-backdrop this module
// created and appended to <body>; the dialog, its Escape key and its focus trap
// now all belong to the Settings modal, so nothing of the sort is built here.
let mgrEl = null;
let mgrBusy = false;

/** Render the pack manager into `host` (the Settings ⚡ pane). */
export async function mountManager(host) {
  mgrEl = host;
  host.innerHTML = '';
  host.append(el('p', { class: 'muted modal-hint', text: t('common.loading') }));
  await loadReactions(true);
  // Belt-and-braces with the tab's own visibility rule: curation is full-session
  // only server-side, so a Safe-Mode caller would only ever collect 403s.
  if (!R.canManage) {
    host.innerHTML = '';
    host.append(el('p', { class: 'muted modal-hint', text: t('reactions.locked') }));
    return;
  }
  renderManager();
}

/** Leaving the tab (or closing Settings). Nothing is on a timer here, so this
 *  only lets go of the host — `closeManager` is the name main.js has always
 *  imported, kept so the call sites don't have to care. */
export function closeManager() {
  if (mgrEl) mgrEl.innerHTML = '';
  mgrEl = null;
}

/** The old gear-rail entry point: now opens Settings on the ⚡ tab. */
export async function openManager() {
  ctx.openSettingsTab('reactions');
}

/** Is the manager on screen right now?
 *
 *  main.js's "repaint every open modal" pass walks its own `dom` map, which
 *  this pane's CONTENTS were never in — language switches were leaving the
 *  whole manager in the previous language for exactly that reason. Asking the
 *  module is what fixed it, and the question is still ours to answer.
 */
export function managerOpen() {
  return !!mgrEl && mgrEl.isConnected && !mgrEl.classList.contains('hidden');
}

/** Rebuild the manager's contents in the CURRENT language.
 *
 *  Every string in here was resolved by t() at build time, so re-rendering is
 *  the only way to re-translate it. Safe to call when closed (no-op) and it
 *  re-reads the already-loaded pack rather than re-fetching.
 */
export function repaintManager() {
  if (managerOpen()) renderManager();
}

function fieldRow(label, control, hint) {
  return el('label', { class: 'rx-field' }, [
    el('span', { class: 'rx-field-label', text: label }),
    control,
    ...(hint ? [el('span', { class: 'rx-field-hint', text: hint })] : []),
  ]);
}

function renderManager() {
  if (!mgrEl) return;
  mgrEl.innerHTML = '';

  const content = el('div', { class: 'rx-body' });

  // ---- Add: upload + generate ----
  const add = el('section', { class: 'rx-section' });
  add.append(el('h3', { class: 'rx-h3', text: t('reactions.add_heading') }));

  const fileInput = el('input', { type: 'file', accept: 'image/*', hidden: true });
  const uploadBtn = el('button', { class: 'btn-secondary', text: t('reactions.upload') });
  uploadBtn.addEventListener('click', () => fileInput.click());
  fileInput.addEventListener('change', async () => {
    const f = fileInput.files && fileInput.files[0];
    fileInput.value = '';
    if (!f) return;
    const name = (f.name || 'reaction').replace(/\.[^.]+$/, '').slice(0, 60);
    await withBusy(async () => {
      await api.addReaction(f, { name, category: 'custom', safe: 'false' });
      ctx.toast(t('reactions.added', { name }));
      await loadReactions(true);
      renderManager();
      ctx.onChanged();
    });
  });

  const genRow = el('div', { class: 'rx-gen-row' });
  const genPrompt = el('input', {
    class: 'rx-input', type: 'text', placeholder: t('reactions.generate_placeholder'),
    autocomplete: 'off',
  });
  const genBtn = el('button', { class: 'btn-primary', text: t('reactions.generate') });
  genBtn.disabled = !R.canGenerate;
  if (!R.canGenerate) genBtn.title = t('reactions.generate_unavailable');
  const runGen = async () => {
    const p = genPrompt.value.trim();
    if (!p) { genPrompt.focus(); return; }
    await withBusy(async () => {
      ctx.toast(t('reactions.generating'));
      const r = await api.generateReaction({ prompt: p, safe: false });
      genPrompt.value = '';
      ctx.toast(t('reactions.generated', { name: r.reaction.name }));
      await loadReactions(true);
      renderManager();
      ctx.onChanged();
    }, t('reactions.generate_failed'));
  };
  genBtn.addEventListener('click', runGen);
  genPrompt.addEventListener('keydown', (e) => { if (e.key === 'Enter') runGen(); });
  genRow.append(genPrompt, genBtn);

  add.append(el('div', { class: 'rx-add-row' }, [uploadBtn, fileInput]), genRow);
  if (!R.canGenerate) {
    add.append(el('p', { class: 'muted rx-note', text: t('reactions.generate_note') }));
  }
  content.append(add);

  // ---- The pack ----
  const listSec = el('section', { class: 'rx-section' });
  listSec.append(el('h3', { class: 'rx-h3', text: t('reactions.pack_heading', { count: R.items.length }) }));
  const list = el('div', { class: 'rx-list' });
  if (!R.items.length) {
    list.append(el('div', { class: 'reaction-empty', text: t('reactions.pack_empty') }));
  }
  for (const r of R.items) list.append(managerRow(r));
  listSec.append(list);
  content.append(listSec);

  // ---- The rotating pool ----
  const pool = R.pool || {};
  const poolSec = el('section', { class: 'rx-section' });
  poolSec.append(el('h3', { class: 'rx-h3', text: t('reactions.pool_heading') }));
  poolSec.append(el('p', { class: 'muted rx-note', text: t('reactions.pool_note') }));
  if (pool.dir) {
    // The path is a real filesystem path — an LTR artefact. It stays in its own
    // <code>, which app.css pins to direction:ltr, so the sentence around it can
    // mirror without dragging the path apart.
    poolSec.append(el('p', { class: 'muted rx-note' }, [
      t('reactions.pool_dir'), el('code', { text: pool.dir }),
    ]));
  }

  const lowMoods = pool.low_moods || [];
  const stat = el('div', { class: 'rx-pool-stat' }, [
    el('span', {}, [el('b', { text: String(pool.remaining ?? 0) }), t('reactions.pool_on_hand')]),
    el('span', {}, [el('b', { text: String(pool.per_mood ?? 0) }), t('reactions.pool_target')]),
    el('span', {}, [t('reactions.pool_refilled'), el('b', { text: pool.batch_date || '—' })]),
    el('span', { class: pool.available ? '' : 'rx-pool-warn',
                 text: t(pool.available ? 'reactions.pool_reachable' : 'reactions.pool_unreachable') }),
  ]);
  if (lowMoods.length) {
    stat.append(el('span', { class: 'rx-pool-warn', text: t('reactions.pool_low', { moods: lowMoods.join(', ') }) }));
  }
  if (pool.last_error) stat.append(el('span', { class: 'rx-pool-warn', text: pool.last_error }));
  poolSec.append(stat);

  const moods = pool.moods || {};
  const moodNames = Object.keys(moods);
  if (moodNames.length) {
    poolSec.append(el('div', { class: 'muted rx-note rx-moods',
      text: moodNames.map((m) => `${m} ${moods[m]}`).join(' · ') }));
  }

  const poolOn = el('input', { type: 'checkbox' });
  poolOn.checked = pool.enabled !== false;
  const perMood = el('input', { class: 'rx-input rx-num', type: 'number', min: '0', max: '100', step: '1' });
  perMood.value = pool.per_mood ?? 20;
  const minMood = el('input', { class: 'rx-input rx-num', type: 'number', min: '0', max: '100', step: '1' });
  minMood.value = pool.min_per_mood ?? 5;
  const hour = el('input', { class: 'rx-input rx-num', type: 'number', min: '0', max: '23', step: '1' });
  hour.value = pool.refresh_hour ?? 4;
  const poolSafe = el('input', { type: 'checkbox' });
  poolSafe.checked = !!pool.safe;

  poolSec.append(
    fieldRow(t('reactions.pool_enabled'), poolOn, t('reactions.pool_enabled_hint')),
    fieldRow(t('reactions.pool_per_mood'), perMood, t('reactions.pool_per_mood_hint')),
    fieldRow(t('reactions.pool_min'), minMood, t('reactions.pool_min_hint')),
    fieldRow(t('reactions.pool_hour'), hour, t('reactions.pool_hour_hint')),
    fieldRow(t('reactions.pool_safe'), poolSafe, t('reactions.pool_safe_hint')),
  );

  const poolSave = el('button', { class: 'btn-primary', text: t('reactions.pool_save') });
  poolSave.addEventListener('click', async () => {
    await withBusy(async () => {
      const r = await api.reactionPool({
        enabled: poolOn.checked,
        per_mood: Number(perMood.value || 0),
        min_per_mood: Number(minMood.value || 0),
        refresh_hour: Number(hour.value || 4),
        safe: poolSafe.checked,
      });
      R.pool = r.pool;
      ctx.toast(t('reactions.saved'));
      renderManager();
    });
  });
  const topUp = el('button', { class: 'btn-secondary', text: t('reactions.pool_topup') });
  topUp.disabled = !pool.available;
  topUp.title = t(pool.available ? 'reactions.pool_topup_title' : 'reactions.pool_topup_disabled');
  topUp.addEventListener('click', () => runRefill(false));
  const replaceNow = el('button', { class: 'btn-secondary', text: t('reactions.pool_replace') });
  replaceNow.disabled = !pool.available;
  replaceNow.title = t('reactions.pool_replace_title');
  replaceNow.addEventListener('click', () => runRefill(true));

  async function runRefill(replace) {
    if (replace && !await ctx.confirm(t('reactions.pool_replace_confirm'))) return;
    await withBusy(async () => {
      const r = await api.refillReactionPool(replace);
      R.pool = r.pool || R.pool;
      // Generation runs server-side in the background; reaction_pool frames
      // update the counts here as images land.
      ctx.toast(t('reactions.pool_refilling'));
      renderManager();
    }, t('reactions.pool_refill_failed'));
  }

  poolSec.append(el('div', { class: 'rx-actions' }, [poolSave, topUp, replaceNow]));
  content.append(poolSec);

  // ---- Settings ----
  const s = R.settings;
  const setSec = el('section', { class: 'rx-section' });
  setSec.append(el('h3', { class: 'rx-h3', text: t('reactions.behaviour_heading') }));

  const enabled = el('input', { type: 'checkbox' });
  enabled.checked = s.enabled !== false;
  const dur = el('input', { class: 'rx-input rx-num', type: 'number', min: '1', max: '60', step: '1' });
  dur.value = Math.round((s.default_duration_ms || 10000) / 1000);
  const cool = el('input', { class: 'rx-input rx-num', type: 'number', min: '0', max: '60', step: '1' });
  cool.value = Math.round((s.cooldown_ms || 0) / 1000);
  const queue = el('input', { class: 'rx-input rx-num', type: 'number', min: '1', max: '10', step: '1' });
  queue.value = s.queue_max || 3;

  setSec.append(
    fieldRow(t('reactions.enabled'), enabled, t('reactions.enabled_hint')),
    fieldRow(t('reactions.duration'), dur, t('reactions.duration_hint')),
    fieldRow(t('reactions.cooldown'), cool, t('reactions.cooldown_hint')),
    fieldRow(t('reactions.queue'), queue, t('reactions.queue_hint')),
  );

  const saveBtn = el('button', { class: 'btn-primary', text: t('reactions.save_behaviour') });
  saveBtn.addEventListener('click', async () => {
    await withBusy(async () => {
      const r = await api.reactionSettings({
        enabled: enabled.checked,
        default_duration_ms: Math.round(Number(dur.value || 10) * 1000),
        cooldown_ms: Math.round(Number(cool.value || 0) * 1000),
        queue_max: Number(queue.value || 3),
      });
      R.settings = r.settings;
      ctx.toast(t('reactions.saved'));
    });
  });
  const reseedBtn = el('button', { class: 'btn-secondary', text: t('reactions.reseed') });
  reseedBtn.title = t('reactions.reseed_title');
  reseedBtn.addEventListener('click', async () => {
    await withBusy(async () => {
      await api.reseedReactions();
      await loadReactions(true);
      renderManager();
      ctx.toast(t('reactions.reseeded'));
      ctx.onChanged();
    });
  });
  setSec.append(el('div', { class: 'rx-actions' }, [saveBtn, reseedBtn]));
  content.append(setSec);

  // No footer: every action in here is a section's own button (Save behaviour,
  // Save pool, Top up, Reseed), so the tab's footer group stays empty and the
  // modal footer collapses. A "Done" that only closed the dialog would be the
  // one dead button on the screen.
  mgrEl.append(content);
}

function managerRow(r) {
  const row = el('div', { class: 'rx-row' });
  // Same oversight as the Bot Manager's avatar picker: the Reaction Manager is
  // a wall of thumbnails, and under NIM it was the other admin screen still
  // fetching and showing pictures. The row keeps its name/alias/safe controls
  // so the pack stays CURATABLE without images — only the preview goes.
  if (!nimEnabled()) {
    row.append(el('img', { class: 'rx-row-thumb', src: r.image_url, alt: '', loading: 'lazy' }));
  } else {
    row.append(el('div', { class: 'rx-row-thumb rx-row-thumb-none', 'aria-hidden': 'true' }));
  }

  const name = el('input', { class: 'rx-input rx-row-name', type: 'text', value: r.name });
  name.maxLength = 60;
  const aliases = el('input', {
    class: 'rx-input rx-row-aliases', type: 'text',
    value: (r.aliases || []).join(' '), placeholder: t('reactions.aliases_placeholder'),
  });

  const safe = el('input', { type: 'checkbox' });
  safe.checked = !!r.safe;
  const safeLabel = el('label', { class: 'rx-safe' }, [
    safe, el('span', { text: t('reactions.safe_label') }),
  ]);
  safeLabel.title = t('reactions.safe_title');

  const meta = el('div', { class: 'rx-row-meta' }, [
    el('code', { class: 'rx-row-id', text: `:react:${r.id}:` }),
    el('span', { class: 'rx-row-src muted', text: r.source }),
  ]);

  const save = el('button', { class: 'btn-secondary rx-row-save', text: t('common.save') });
  save.addEventListener('click', async () => {
    await withBusy(async () => {
      await api.patchReaction(r.id, {
        name: name.value.trim() || r.name,
        aliases: aliases.value.split(/[\s,]+/).filter(Boolean),
        safe: safe.checked,
      });
      ctx.toast(t('reactions.saved'));
      await loadReactions(true);
      ctx.onChanged();
    });
  });

  const test = el('button', { class: 'btn-secondary rx-row-test', text: '▶' });
  test.title = t('reactions.test');
  // App-wide test pop, fired as the enabled reaction bot (agents-only API).
  test.addEventListener('click', () => fireReaction(r.id, { thread_id: null, trace: false }));

  const del = el('button', { class: 'icon-btn ghost danger-text rx-row-del', text: '🗑' });
  del.setAttribute('aria-label', t('reactions.delete_aria', { name: r.name }));
  del.addEventListener('click', async () => {
    if (!await ctx.confirm(t('reactions.delete_confirm', { name: r.name }))) return;
    await withBusy(async () => {
      await api.deleteReaction(r.id);
      await loadReactions(true);
      renderManager();
      ctx.toast(t('reactions.deleted'));
      ctx.onChanged();
    });
  });

  row.append(el('div', { class: 'rx-row-fields' }, [name, aliases, meta]),
             el('div', { class: 'rx-row-actions' }, [safeLabel, test, save, del]));
  return row;
}

async function withBusy(fn, errPrefix) {
  if (mgrBusy) return;
  mgrBusy = true;
  if (mgrEl) mgrEl.classList.add('busy');
  try {
    await fn();
  } catch (e) {
    const msg = String(e.message || e).replace(/^\d+:\s*/, '');
    ctx.toast(errPrefix ? `${errPrefix}: ${msg}` : msg, true);
  } finally {
    mgrBusy = false;
    if (mgrEl) mgrEl.classList.remove('busy');
  }
}

// ===================== In-chat embed =====================

// A thread-bound reaction lives in the message list. Its row carries BOTH the
// collapsed chip and the expanded picture; an `.expanded` class toggles which
// is visible:
//   * live `reaction` frame → expanded automatically, countdown bar runs, the
//     row collapses back to the chip after the duration (10s by default)
//   * clicking the chip → the picture pops back up in place and stays (this
//     device chose to look — no broadcast, no timer pressure)
//   * clicking the picture → collapses back to the chip
// Returns null when the message isn't a reaction trace.
export function reactionMessageEl(msg, { decoy = false } = {}) {
  const meta = msg && msg.metadata;
  if (!meta || meta.kind !== 'reaction') return null;
  // In NIM the trace row goes too — chip, thumbnail and all. Half a reaction
  // ("<bot> reacted", no picture) is a visible trace of the thing NIM removed,
  // so the device behaves as though reactions do not exist. The caller already
  // tolerates null here (it is how non-reaction messages fall through).
  if (nimEnabled()) return null;

  const row = el('div', { class: 'msg system reaction-trace-row', dataset: { id: msg.id } });

  // The collapsed chip — the one-line trace history has always shown.
  const chip = el('div', { class: 'reaction-trace' });
  // Fired images are kept (spent/ is chat history), so traces replay on
  // click. replayable:false only appears on legacy rows from before that
  // retention — those blobs are already purged, so they stay text-only.
  const rid = (meta.replayable === false) ? null : meta.reaction_id;
  if (rid && !decoy) {
    chip.append(el('img', {
      class: 'reaction-trace-thumb', src: api.reactionImageUrl(rid), alt: '', loading: 'lazy',
    }));
  }
  chip.append(el('span', {
    class: 'reaction-trace-text',
    // msg.content is the server-rendered trace line and wins when present; the
    // key is the fallback for a row that never carried one.
    text: msg.content || t('reactions.trace', { name: meta.actor || t('common.someone') }),
  }));
  row.append(chip);

  // The expanded picture — display:none until the row is expanded.
  const img = el('img', { class: 'reaction-embed-img', alt: '', loading: 'lazy' });
  const caption = el('div', { class: 'reaction-caption' });
  const actor = el('span', { class: 'reaction-actor' });
  const name = el('span', { class: 'reaction-name' });
  const bar = el('span', { class: 'reaction-bar-fill' });
  const embed = el('div', { class: 'reaction-embed' }, [
    el('div', { class: 'reaction-img-wrap' }, [img]),
    caption,
    el('div', { class: 'reaction-foot' }, [actor, name]),
    el('div', { class: 'reaction-bar' }, [bar]),
  ]);
  row.append(embed);

  // The picture itself opens the standard lightbox — same instinct as any
  // chat image; clicking the rest of the card collapses it back to the chip.
  embed.addEventListener('click', (e) => {
    const im = row.querySelector('.reaction-embed-img');
    if (im && e.target === im && im.src) {
      e.stopPropagation();
      ctx.lightbox(im.src);
      return;
    }
    collapseReactionRow(row);
  });

  // The chip toggles the picture back up in place, or tucks it away again.
  // (The expanded picture itself opens the lightbox, so the chip is the
  // collapse affordance.) This is wired BEFORE the pending-expand branch
  // below: an auto-expand used to `return row` early and skip this, so a row
  // that opened from a load-race had a dead chip after its 10s auto-collapse
  // and could not be re-opened until some later renderMessages rebuilt it.
  if (rid && !decoy) {
    chip.classList.add('clickable');
    chip.title = t('reactions.show');
    chip.addEventListener('click', () => {
      if (row.classList.contains('expanded')) { collapseReactionRow(row); return; }
      expandReactionRow(row, {
        reaction_id: rid,
        name: meta.reaction_name || rid,
        image_url: api.reactionImageUrl(rid),
        actor: meta.actor || t('common.someone'),
        actor_kind: meta.actor_kind || 'agent',
        safe: true,
        caption: '',
        duration_ms: null,
      }, { auto: false });
    });
  }

  // A pending live frame (the reaction fired while this thread's messages were
  // still loading) — expand now, and never again for this row.
  const pend = R.pendingExpand.get(msg.id);
  if (pend) {
    R.pendingExpand.delete(msg.id);
    if (Date.now() - pend.at < PENDING_EXPAND_MS) expandReactionRow(row, pend.ev, { auto: true });
  }
  return row;
}

// How long a pending live expand stays valid while a thread is still loading.
const PENDING_EXPAND_MS = 30_000;

function expandReactionRow(row, ev, { auto = false } = {}) {
  if (!row || !row.querySelector('.reaction-embed')) return;
  collapseReactionRow(row);            // cancel any previous timer, if any
  paintEmbed(row, ev);
  row.classList.add('expanded');
  if (auto) {
    const duration = Math.max(800, ev.duration_ms || R.settings.default_duration_ms || 10000);
    runCountdown(row, duration);
    const tid = setTimeout(() => collapseReactionRow(row), duration);
    R.liveTimers.set(row.dataset.id, tid);
    // Announce for screen readers without moving focus.
    const live = document.getElementById('sr-live');
    if (live) live.textContent = t('reactions.announce', { name: who(ev), reaction: ev.name || ev.reaction_id });
  } else {
    // Manual pop-up: no countdown, the bar just sits full.
    const bar = row.querySelector('.reaction-bar-fill');
    if (bar) { bar.style.transition = 'none'; bar.style.width = '100%'; }
  }
}

function paintEmbed(row, ev) {
  const img = row.querySelector('.reaction-embed-img');
  const caption = row.querySelector('.reaction-caption');
  const actor = row.querySelector('.reaction-actor');
  const name = row.querySelector('.reaction-name');
  // Safe Mode may only paint safe reactions — the server already filtered the
  // frame, but this is the same belt-and-suspenders layer the renderer uses.
  if (!ctx.isDecoy() || ev.safe) {
    img.src = ev.image_url || api.reactionImageUrl(ev.reaction_id);
    img.alt = ev.name ? t('reactions.alt', { name: ev.name }) : t('reactions.alt_generic');
    // Fired images are kept, but a blob can still vanish (hand-deleted, or a
    // legacy purge) — collapse back to the chip, never a broken picture.
    img.onerror = () => collapseReactionRow(row);
  } else {
    img.removeAttribute('src');
    img.alt = '';
  }
  name.textContent = ev.name || ev.reaction_id;
  actor.textContent = `${ev.actor_kind === 'agent' ? '⚡' : '🫵'} ${who(ev)}`;
  if (ev.caption) { caption.textContent = ev.caption; caption.hidden = false; }
  else { caption.textContent = ''; caption.hidden = true; }
}

function who(ev) {
  return ev.actor || t(ev.actor_kind === 'agent' ? 'common.assistant' : 'common.you');
}

function runCountdown(row, duration) {
  const bar = row.querySelector('.reaction-bar-fill');
  if (!bar) return;
  if (REDUCED_MOTION()) { bar.style.transition = 'none'; bar.style.width = '100%'; return; }
  bar.style.transition = 'none';
  bar.style.width = '100%';
  void bar.offsetWidth;
  bar.style.transition = `width ${duration}ms linear`;
  bar.style.width = '0%';
}

export function collapseReactionRow(row) {
  if (!row) return;
  const id = row.dataset.id;
  const tid = R.liveTimers.get(id);
  if (tid) { clearTimeout(tid); R.liveTimers.delete(id); }
  row.classList.remove('expanded');
  const bar = row.querySelector('.reaction-bar-fill');
  if (bar) { bar.style.transition = 'none'; bar.style.width = '100%'; }
}
