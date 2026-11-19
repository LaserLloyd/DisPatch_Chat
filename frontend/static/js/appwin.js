// App windows - the window manager (docs/design/app-windows.md).
//
// Every framed tool (apps/<id>/, tools.yaml static and url pages) opens in a
// WINDOW: a title bar (icon, title, status, the tool's own buttons, layout,
// Back to chat, Close) over a body that js/tools.js fills with the tool's frame.
// One window is on screen at a time; the others are PILLS in a tray at the
// bottom of the chat column. Minimising never unloads a frame (up to MAX_LIVE
// frames stay alive), so a half-typed form or a running job is as it was left.
//
// Desktop layouts: float (move by the title bar, resize by the edges, snap to an
// edge to dock), dock left / right (the chat column makes room; a divider sets
// the share), full (covers the thread list and chat; the rail stays). Phone: a
// sheet over everything with a grab handle; Back and a swipe down minimise.
//
// No frame ever moves in the DOM (moving an iframe reloads it): every window
// lives in one fixed layer for its whole life and only its styles change.
//
// This module draws and arranges; the state machine and the sums are in
// js/appwin-state.js; what goes INSIDE a window is the host's (js/tools.js
// registers itself with setHost). No side effects at import time.

import { t } from './i18n.js?v=5';
import { el, spriteIcon } from './util.js?v=23';
import * as S from './appwin-state.js?v=2';

/** Frames kept alive at once (the open one + the most recently used pills):
 *  a phone has less memory to spare than a desktop. */
export const MAX_LIVE = 5;
export const MAX_LIVE_PHONE = 3;
/** A pill dragged this far UP closes on release. Sideways never closes: on a
 *  phone that is how the row scrolls. */
export const CLOSE_DRAG_Y = 64;
/** A sheet swiped down this far minimises. */
export const SHEET_SWIPE = 96;

let deps = null;
let host = null;
let model = S.empty();
let profileId = undefined;     // undefined = never set; null = no profile (nothing saved)
let firstSync = true;
const recs = new Map();        // id -> window record (DOM + live state)
let layer = null;
let tray = null;
let trayRow = null;
let divider = null;
let preview = null;
let confirmDlg = null;
let raf = 0;
let lastVisible = null;

const $ = (id) => document.getElementById(id);
const isRtl = () => document.documentElement.getAttribute('dir') === 'rtl';
const mobile = () => !!(deps && deps.isMobile());
const reduceMotion = () => { try { return matchMedia('(prefers-reduced-motion: reduce)').matches; } catch { return false; } };

// ---- small inline icons for the layout menu (not in the shared sprite) ----
const SVG_NS = 'http://www.w3.org/2000/svg';
function lineIcon(paths, size = 16) {
  const svg = document.createElementNS(SVG_NS, 'svg');
  for (const [k, v] of Object.entries({ viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': '2', 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'aria-hidden': 'true', width: String(size), height: String(size) })) svg.setAttribute(k, v);
  svg.classList.add('rail-icon');
  for (const d of paths) { const p = document.createElementNS(SVG_NS, 'path'); p.setAttribute('d', d); svg.append(p); }
  return svg;
}
const ICONS = {
  float: ['M3 5h12v10H3z', 'M9 15v4h12V9h-6'],
  left: ['M3 4h18v16H3z', 'M11 4v16', 'M5 8h4', 'M5 12h4'],
  right: ['M3 4h18v16H3z', 'M13 4v16', 'M15 8h4', 'M15 12h4'],
  full: ['M3 4h18v16H3z', 'M3 8h18'],
};
const LAYOUT_LABEL = { float: 'win.float', left: 'win.dock_left', right: 'win.dock_right', full: 'win.full' };

// ===================== public API =====================

export function setHost(h) { host = h; }

/** The window on screen (id) or null. */
export function visibleId() { return model.open; }

/** Every tool that has a window (open or a pill). */
export function windowIds() { return model.wins.map((w) => w.id); }

export function isMinimized(id) { const w = S.find(model, id); return !!w && w.state === 'min'; }

/** What the window on screen is drawn as: 'float' | 'left' | 'right' | 'full' |
 *  'sheet', or null when none is up. */
export function visibleLayout() {
  const r = model.open ? recs.get(model.open) : null;
  return r ? r.eff || null : null;
}

/** Does the window on screen hide the chat (full or a phone sheet)? */
export function coversChat() {
  const l = visibleLayout();
  return l === 'full' || l === 'sheet';
}

export function recordFor(id) { return recs.get(id) || null; }

/** Open a tool's window or bring back the one it has. */
export function openWindow(id) {
  if (!host || !host.info(id)) return false;
  model = S.openWin(model, id);
  if (!S.find(model, id)) return false;
  // The window cap may have dropped the oldest pill: its record goes with it.
  for (const r of [...recs.values()]) if (!S.find(model, r.id)) destroyRec(r);
  ensureRec(id);
  touch(id);
  apply({ focus: id });
  return true;
}

/** Back to chat. `toChat`: a pointer or touch did it, so the composer gets the
 *  focus (typing goes where the eyes are); the keyboard keeps it on the pill. */
export function minimizeWindow(id = model.open, { toChat = false } = {}) {
  if (!id || !S.find(model, id)) return false;
  model = S.minimize(model, id);
  apply(toChat ? { focusChat: true } : { focusPill: id });
  return true;
}

/** Close a window. Asks first while the tool says it is busy or has unsaved
 *  work, unless `force` (the tool asked to close itself). Resolves true when
 *  the window is gone. */
export async function closeWindow(id, { force = false } = {}) {
  const r = recs.get(id);
  if (!r) return false;
  if (!force && (r.live.busy > 0 || r.live.unsaved)) {
    const name = r.title;
    const ok = await askConfirm(t(r.live.busy > 0 ? 'win.confirm_busy' : 'win.confirm_unsaved', { name }));
    // The same window still (a lock or a profile switch may have replaced it).
    if (!ok || recs.get(id) !== r) return false;
  }
  const wasOpen = model.open === id;
  const trayIds = S.pills(model).map((w) => w.id);
  const at = trayIds.indexOf(id);
  model = S.closeWin(model, id);
  destroyRec(r);
  // Focus: a closed window hands it to the chat (or the window under it);
  // a closed pill to its neighbour in the tray (design §3).
  apply(wasOpen ? { focusAfterClose: true } : { focusAfterClose: true, focusNear: at });
  return true;
}

/** Tear every window down (a lock, a profile switch). The saved layout stays
 *  for that profile; nothing is shown to whoever comes next. */
export function closeAll() {
  for (const r of [...recs.values()]) destroyRec(r);
  model = S.empty();
  profileId = undefined;
  firstSync = true;
  apply({ save: false });
}

/** Which profile is using the app. A change tears everything down and loads
 *  that profile's windows (as pills / the open one, once the tool list is in). */
export function setProfile(pid) {
  const next = typeof pid === 'string' && pid ? pid : null;
  if (next === profileId) return;
  for (const r of [...recs.values()]) destroyRec(r);
  profileId = next;
  model = next ? S.migrateSplit(S.load(next), { rtl: isRtl() }) : S.empty();
  firstSync = true;
  apply({ save: false, quiet: true });
  if (host && typeof host.ready === 'function' && host.ready()) syncKnown();
}

/** The tool list changed (loaded, a tool switched off, a different tier): drop
 *  windows this session may no longer open; after a reload, bring back the
 *  saved ones (a phone gets them all as pills). */
export function syncKnown() {
  if (!host || profileId === undefined) return;
  const ids = host.knownIds();
  model = S.filterKnown(model, ids);
  for (const r of [...recs.values()]) if (!S.find(model, r.id)) destroyRec(r);
  if (firstSync) {
    firstSync = false;
    if (mobile() && model.open) model = S.minimize(model, model.open);
  }
  for (const w of model.wins) ensureRec(w.id);
  apply({ quiet: true });
}

/** A tool's live state: status text, a badge count, attention, busy, unsaved. */
export function setLive(id, op, value) {
  const r = recs.get(id);
  if (!r) return false;
  const L = r.live;
  if (op === 'status') L.status = String(value == null ? '' : value).replace(/\s+/g, ' ').trim().slice(0, 40);
  else if (op === 'badge') { const n = Number(value); L.badge = Number.isInteger(n) && n > 0 ? Math.min(n, 999) : 0; }
  else if (op === 'busy') { const n = Number(value); L.busy = Number.isInteger(n) && n > 0 ? Math.min(n, 999) : 0; }
  else if (op === 'unsaved') L.unsaved = value === true;
  else if (op === 'attention') {
    if (model.open === id) return true;    // on screen: nothing to flag
    const w = S.find(model, id);
    if (w && !w.att && deps && typeof deps.announce === 'function') deps.announce(t('win.finished', { name: r.title }));
    model = S.setAttention(model, id, true);
    if (profileId) S.save(profileId, model);
  } else if (op === 'minimize') { minimizeWindow(id); return true; }
  else return false;
  paintBar(r);
  paintTray();
  return true;
}

/** The title a tool asked for ('' = its own name back). */
export function setTitle(id, text) {
  const r = recs.get(id);
  if (!r) return;
  r.customTitle = String(text || '').slice(0, 120);
  paintBar(r);
  paintTray();
}

/** The window on screen is not loaded yet (its host was not ready): try again.
 *  The host calls this when what it was waiting for arrived. */
export function retryLoad() {
  const r = model.open ? recs.get(model.open) : null;
  if (!r || r.loaded || applying) return;
  apply({ save: false, quiet: true });
}

/** Re-measure and re-place everything (resize, a language switch, the chat
 *  column changed). Coalesced to one frame. */
export function relayout() {
  if (raf) return;
  const go = () => { raf = 0; apply({ save: false, quiet: true }); };
  raf = typeof requestAnimationFrame === 'function' ? requestAnimationFrame(go) : (go(), 0);
}

/** Relabel every bar and pill (language switch). */
export function repaintAll() {
  for (const r of recs.values()) { r.title = (host && host.info(r.id) && host.info(r.id).title) || r.title; paintChrome(r); paintBar(r); }
  paintTray();
  relayout();
}

// ===================== records =====================

function ensureRec(id) {
  if (recs.has(id)) return recs.get(id);
  const info = host.info(id);
  if (!info) return null;
  const r = {
    id,
    title: info.title,
    glyph: info.glyph,
    opts: S.windowOptions(info.window),
    live: { status: '', badge: 0, busy: 0, unsaved: false },
    customTitle: '',
    asleep: false,
    loaded: false,
    used: 0,
    eff: null,
    sent: '',
  };
  buildWindow(r);
  recs.set(id, r);
  host.mount(r);
  return r;
}

function destroyRec(r) {
  try { host && host.destroy(r); } catch (e) { console.warn('[appwin] destroy', e); }
  r.el.remove();
  recs.delete(r.id);
}

/** A page's frame goes (past the live cap, or keep_alive: false): what it
 *  said about itself goes with it, or its pill would say "2 making" forever. */
function unloadRec(r, { asleep = false } = {}) {
  r.loaded = false;
  r.sent = '';
  r.asleep = asleep;
  r.live = { status: '', badge: 0, busy: 0, unsaved: false };
  host.unload(r);
}

/** Has the page work the owner would lose (busy, unsaved, or typed text)? */
function hasWork(r) {
  if (r.live.busy > 0 || r.live.unsaved) return true;
  try { return !!(host && typeof host.typed === 'function' && host.typed(r)); } catch { return false; }
}

// The sheet's history entry, and calls waiting for its pop to land.
let sheetPop = null;
let histWaiters = null;
function holdHistory() {
  if (histWaiters) return;
  histWaiters = [];
  const flush = () => {
    const fns = histWaiters || []; histWaiters = null;
    for (const fn of fns) { try { fn(); } catch (e) { console.warn('[appwin] after history', e); } }
  };
  window.addEventListener('popstate', () => setTimeout(flush, 0), { once: true });
  setTimeout(() => { if (histWaiters) flush(); }, 400);   // a browser that never pops
}
/** Run `fn` once a sheet's history entry has gone (at once when none is going):
 *  navigation pushed before that pop lands would be the entry it pops. */
export function afterHistory(fn) {
  if (histWaiters) histWaiters.push(fn); else fn();
}

let useSeq = 0;
function touch(id) { const r = recs.get(id); if (r) r.used = ++useSeq; }

// ===================== DOM =====================

function ensureLayer() {
  if (layer) return;
  layer = $('appwin-layer') || document.body.appendChild(el('div', { id: 'appwin-layer', class: 'appwin-layer' }));
  tray = $('appwin-tray') || document.body.appendChild(el('div', { id: 'appwin-tray', class: 'appwin-tray' }));
  tray.setAttribute('role', 'toolbar');
  tray.hidden = true;
  tray.addEventListener('keydown', onTrayKey);
  // The pills live in a row that scrolls sideways (a phone fits two or three).
  trayRow = tray.querySelector('.appwin-tray-row') || tray.appendChild(el('div', { class: 'appwin-tray-row' }));
  trayRow.addEventListener('scroll', paintFade, { passive: true });
  divider = $('appwin-divider') || document.body.appendChild(el('div', { id: 'appwin-divider', class: 'appwin-divider' }));
  divider.setAttribute('role', 'separator');
  divider.setAttribute('aria-orientation', 'vertical');
  divider.tabIndex = 0;
  divider.hidden = true;
  wireDivider();
  preview = $('appwin-preview') || layer.appendChild(el('div', { id: 'appwin-preview', class: 'appwin-preview', 'aria-hidden': 'true' }));
  preview.hidden = true;
}

function iconBtn(cls, icon, label) {
  const b = el('button', { type: 'button', class: 'icon-btn ghost appwin-btn ' + cls, 'aria-label': label, title: label });
  b.append(icon);
  return b;
}

function buildWindow(r) {
  ensureLayer();
  const titleId = `appwin-title-${cssSafe(r.id)}`;
  const descId = `appwin-desc-${cssSafe(r.id)}`;
  const win = el('section', { class: 'appwin', role: 'dialog', 'aria-labelledby': titleId, dataset: { tool: r.id } });
  win.hidden = true;
  const bar = el('header', { class: 'appwin-bar' });
  const grip = el('div', { class: 'appwin-grip', 'aria-hidden': 'true' });
  const handle = el('button', { type: 'button', class: 'appwin-handle', 'aria-describedby': descId });
  const desc = el('span', { class: 'sr-only', id: descId });
  const icon = el('span', { class: 'appwin-icon', 'aria-hidden': 'true' });
  const text = el('span', { class: 'appwin-titles' });
  const title = el('span', { class: 'appwin-title', id: titleId });
  const status = el('span', { class: 'appwin-status muted' });
  text.append(title, status);
  handle.append(icon, text);
  const slot = el('div', { class: 'appwin-slot' });
  const layoutBtn = iconBtn('appwin-layout', lineIcon(ICONS.float, 18), '');
  layoutBtn.setAttribute('aria-haspopup', 'menu');
  layoutBtn.setAttribute('aria-expanded', 'false');
  const minBtn = iconBtn('appwin-min', spriteIcon('chevron-down', 18), '');
  const minLabel = el('span', { class: 'appwin-min-label' });
  minBtn.append(minLabel);
  const closeBtn = iconBtn('appwin-close', spriteIcon('x', 18), '');
  bar.append(grip, handle, desc, slot, layoutBtn, minBtn, closeBtn);
  const body = el('div', { class: 'appwin-body' });
  win.append(bar, body);
  for (const [dir, cls] of [['e', 'appwin-rs-e'], ['w', 'appwin-rs-w'], ['s', 'appwin-rs-s'], ['se', 'appwin-rs-se'], ['sw', 'appwin-rs-sw']]) {
    win.append(el('div', { class: `appwin-rs ${cls}`, dataset: { dir }, 'aria-hidden': 'true' }));
  }
  layer.append(win);
  Object.assign(r, { el: win, bar, handle, desc, icon, titleEl: title, statusEl: status, slot, layoutBtn, minBtn, minLabel, closeBtn, body });

  // "Back to chat" means it: on a phone opened from the bot list, the chat
  // that was open comes up under the pill (system Back only steps back).
  minBtn.addEventListener('click', (e) => {
    if (mobile() && deps && typeof deps.backToChat === 'function') { try { deps.backToChat(); } catch { /* the pill still comes */ } }
    // detail > 0 = a pointer or a touch (Enter / Space on the button report 0).
    minimizeWindow(r.id, { toChat: e.detail > 0 });
  });
  closeBtn.addEventListener('click', () => closeWindow(r.id));
  layoutBtn.addEventListener('click', () => toggleLayoutMenu(r));
  handle.addEventListener('dblclick', () => { if (!mobile()) chooseLayout(r, r.eff === 'full' ? 'float' : 'full'); });
  handle.addEventListener('keydown', (e) => onHandleKey(r, e));
  win.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !e.defaultPrevented) {
      if (r.menu) { closeLayoutMenu(r, true); e.preventDefault(); return; }
      e.preventDefault();
      minimizeWindow(r.id);
    }
  });
  win.addEventListener('pointerdown', () => touch(r.id), true);
  wireMove(r);
  wireResize(r);
  wireSwipe(r);
  paintChrome(r);
  paintBar(r);
}

function displayTitle(r) { return r.customTitle || r.title; }

/** Labels that depend on the language and the layout. */
function paintChrome(r) {
  const name = r.title;
  const phone = mobile();
  const back = t('win.back_to_chat');
  r.minBtn.setAttribute('aria-label', back);
  r.minBtn.setAttribute('title', back);
  r.minLabel.textContent = back;
  const close = t('win.close', { name });
  r.closeBtn.setAttribute('aria-label', close);
  r.closeBtn.setAttribute('title', close);
  const lay = t('win.layout');
  r.layoutBtn.setAttribute('aria-label', lay);
  r.layoutBtn.setAttribute('title', lay);
  r.layoutBtn.hidden = phone;
  // The NAME is the label; how to move it is the description (desktop only).
  r.handle.setAttribute('aria-label', name);
  r.desc.textContent = phone ? '' : t('win.move', { name });
  r.icon.replaceChildren(r.glyph ? r.glyph() : '');
}

function paintBar(r) {
  r.titleEl.textContent = displayTitle(r);
  const L = r.live;
  r.statusEl.textContent = L.status;
  r.statusEl.hidden = !L.status;
}

// ===================== arrange =====================

/** The content area (everything but the rail), in viewport px. */
function contentArea() {
  const vw = window.innerWidth; const vh = window.innerHeight;
  const rail = document.querySelector('.app > .sidebar');
  const rr = rail && rail.offsetParent !== null ? rail.getBoundingClientRect() : null;
  if (!rr || mobile()) return { left: 0, top: 0, width: vw, height: vh };
  return isRtl() ? { left: 0, top: 0, width: Math.max(0, rr.left), height: vh }
    : { left: rr.right, top: 0, width: Math.max(0, vw - rr.right), height: vh };
}

/** The widths the chat column + a docked tool could share: with the thread
 *  list showing and without it. */
function dockRoom() {
  const a = contentArea();
  const app = document.querySelector('.app');
  const collapsed = !!(app && app.classList.contains('tl-collapsed'));
  const tl = collapsed ? 0 : parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--threadlist-width')) || 320;
  return { withList: Math.floor(a.width - tl), noList: Math.floor(a.width) };
}

function clearDock() {
  const body = document.body;
  body.classList.remove('appwin-dock', 'appwin-dock-nolist', 'appwin-dock-left', 'appwin-dock-right');
  const cv = $('chatview');
  if (cv) cv.style.removeProperty('--appwin-dock-w');
  if (divider) divider.hidden = true;
}

/** What the layout default may depend on: the width beside the rail. */
function env() { return { contentW: mobile() ? null : contentArea().width, rtl: isRtl() }; }
function prefs(r) { return S.prefsFor(model, r.id, r.opts, env()); }
/** Did the owner (or a migrated default) choose this tool's layout? */
function chosen(r) { const p = model.prefs[r.id]; return !!((p && p.layout) || (model.def && model.def.layout)); }

let applying = false;
function apply(opts = {}) {
  applying = true;
  try { applyNow(opts); } finally { applying = false; }
}

function applyNow({ focus = null, focusPill = null, focusChat = false, focusAfterClose = false, focusNear = -1, save = true, quiet = false } = {}) {
  ensureLayer();
  const phone = mobile();
  const open = model.open ? recs.get(model.open) : null;
  // 1. which layout each window is drawn in
  let dock = null;
  for (const r of recs.values()) {
    const w = S.find(model, r.id);
    const visible = !!w && w.state === 'open';
    if (!visible) { r.eff = null; continue; }
    let layout = prefs(r).layout;
    let can = true;
    if (!phone && S.DOCKS.includes(layout)) {
      dock = S.dockMode(dockRoom(), r.opts);
      can = !!dock;
      // A dock nobody chose that does not fit: a float, never a full cover.
      if (!can && !chosen(r)) { layout = 'float'; can = true; dock = null; }
    }
    r.eff = S.effectiveLayout(layout, { mobile: phone, canDock: can, dockable: r.opts.dockable });
  }
  // 2. live frames: the open one loads; past the cap the least recently used
  // pill unloads. Kept first: the open one, then pages with work in hand
  // (busy, unsaved, typed text), then the most recent. An unloaded pill says so.
  for (const r of recs.values()) {
    const w = S.find(model, r.id);
    // A host that is not ready yet (a built-in pane before its probe answered,
    // an add-on before its tiles registered) says false: tried again by retryLoad().
    if (w && w.state === 'open' && !r.loaded) { r.loaded = true; r.asleep = false; if (host.load(r) === false) r.loaded = false; }
    if (w && w.state === 'min' && r.loaded && r.opts.keep_alive === false) unloadRec(r, { asleep: true });
  }
  const cap = phone ? MAX_LIVE_PHONE : MAX_LIVE;
  const loaded = [...recs.values()].filter((r) => r.loaded);
  if (loaded.length > cap) {
    const keep = (r) => (model.open === r.id ? 2 : (hasWork(r) ? 1 : 0));
    const live = loaded.map((r) => [r, keep(r)]).sort((a, b) => (b[1] - a[1]) || (b[0].used - a[0].used)).map((x) => x[0]);
    for (const r of live.slice(cap)) {
      if (model.open === r.id) continue;
      unloadRec(r, { asleep: true });
    }
  }
  // 3. what covers the chat decides where the tray goes; the tray goes first
  // (a float, a full window and a sheet all stop above it)
  const cover = !!open && (open.eff === 'full' || open.eff === 'sheet');
  document.body.classList.toggle('appwin-open', !!open);
  document.body.classList.toggle('appwin-covers', cover);
  document.body.classList.toggle('appwin-sheet', !!open && open.eff === 'sheet');
  paintTray();
  clearDock();
  placeTray();
  // 4. place the window on screen
  for (const r of recs.values()) {
    r.el.hidden = !r.eff;
    r.el.dataset.layout = r.eff || 'min';
    r.layoutBtn.hidden = phone;
    if (r.eff) { place(r, dock); paintLayoutBtn(r); }
    if (!r.eff && r.menu) closeLayoutMenu(r, false);
  }
  // a dock moved the chat column: the tray follows it
  placeTray();
  paintListToggle();
  // 5. phone Back minimises a sheet: ONE history entry while any sheet is up.
  // A sheet replacing another takes the entry over (a pop then a push would
  // race: history.back() lands later and would take the new sheet down).
  const sheet = open && open.eff === 'sheet' ? open : null;
  if (sheet && !sheetPop && deps && typeof deps.historyPush === 'function') {
    sheetPop = deps.historyPush('appwin', 'appwin', () => {
      sheetPop = null;
      const cur = model.open && recs.get(model.open);
      if (cur && cur.eff === 'sheet') minimizeWindow(cur.id);
    });
  } else if (!sheet && sheetPop) {
    const pop = sheetPop; sheetPop = null;
    const willPop = !!(history.state && history.state.ext === 'appwin');
    if (willPop) holdHistory();
    pop();
  }
  // 6. tell each frame its state when it changed
  for (const r of recs.values()) {
    const w = S.find(model, r.id);
    if (!w) continue;
    const key = `${w.state}|${r.eff || ''}`;
    if (key !== r.sent && r.loaded) { r.sent = key; host.stateChanged(r, { state: w.state, layout: r.eff || 'min' }); }
  }
  // 7. focus
  if (focus && recs.get(focus) && !quiet) {
    const r = recs.get(focus);
    try { r.handle.focus({ preventScroll: true }); } catch { /* not fatal */ }
  } else if (focusChat) {
    if (deps && typeof deps.focusChat === 'function') deps.focusChat();
  } else if (focusPill) {
    const b = tray.querySelector(`.appwin-pill[data-tool="${cssId(focusPill)}"] .appwin-pill-main`);
    if (b) focusTrayItem(b);
  } else if (focusAfterClose) {
    const pills = tray.querySelectorAll('.appwin-pill-main');
    if (focusNear >= 0 && pills.length) focusTrayItem(pills[Math.min(focusNear, pills.length - 1)]);
    else if (open) open.handle.focus({ preventScroll: true });
    else if (pills.length) focusTrayItem(pills[pills.length - 1]);
    else if (deps && typeof deps.focusChat === 'function') deps.focusChat();
  }
  if (save && profileId) S.save(profileId, model);
  if (lastVisible !== model.open || !quiet) {
    lastVisible = model.open;
    if (host && typeof host.visibleChanged === 'function') host.visibleChanged(model.open);
  }
}

function cssSafe(id) { return String(id).replace(/[^A-Za-z0-9_-]/g, '_'); }

function cssId(id) { return typeof CSS !== 'undefined' && CSS.escape ? CSS.escape(id) : id; }

function setBox(elm, { left, top, width, height }) {
  elm.style.left = `${Math.round(left)}px`;
  elm.style.top = `${Math.round(top)}px`;
  elm.style.width = `${Math.round(width)}px`;
  elm.style.height = `${Math.round(height)}px`;
}

function clearBox(elm) { for (const k of ['left', 'top', 'width', 'height', 'bottom', 'right']) elm.style.removeProperty(k); }

/** How much of the viewport's bottom the tray takes (its top edge and a gap),
 *  0 when no pill shows. */
function trayReserve() {
  if (!tray || tray.hidden || tray.classList.contains('kbd-hidden')) return 0;
  const tr = tray.getBoundingClientRect();
  if (!tr.height) return 0;
  return Math.max(0, window.innerHeight - tr.top + 8);
}

/** Bounds a float may use: the content area, its bottom stopping above the tray
 *  WHEREVER the tray is (above the composer, or at the bottom edge). */
function floatBounds() {
  const a = contentArea();
  const bottom = Math.min(a.top + a.height, window.innerHeight - trayReserve());
  return { left: a.left, top: a.top, w: a.width, h: Math.max(0, bottom - a.top) };
}

function place(r, dock) {
  const e = r.eff;
  clearBox(r.el);
  if (e === 'sheet') {
    // CSS owns the sheet; its bottom is the top of the tray strip under it.
    const tr = tray && !tray.hidden && !tray.classList.contains('kbd-hidden') ? tray.getBoundingClientRect() : null;
    r.el.style.setProperty('--appwin-tray-h', `${tr && tr.height ? Math.max(0, Math.round(window.innerHeight - tr.top)) : 0}px`);
    return;
  }
  if (e === 'float') {
    const b = floatBounds();
    const p = prefs(r);
    const rect = S.clampRect(p.rect || S.initialRect(r.opts, b, { rtl: isRtl() }), b, r.opts);
    setBox(r.el, { left: b.left + rect.x, top: b.top + rect.y, width: rect.w, height: rect.h });
    return;
  }
  if (e === 'full') {
    const b = floatBounds();
    setBox(r.el, { left: b.left, top: b.top, width: b.w, height: b.h });
    return;
  }
  // dock
  const side = e;
  const room = dockRoom();
  const total = dock === 'nolist' ? room.noList : room.withList;
  const p = prefs(r);
  const w = S.dockWidths(p.ratio, total, r.opts);
  if (!w) return;
  const body = document.body;
  body.classList.add('appwin-dock', side === 'left' ? 'appwin-dock-left' : 'appwin-dock-right');
  body.classList.toggle('appwin-dock-nolist', dock === 'nolist');
  const cv = $('chatview');
  if (cv) cv.style.setProperty('--appwin-dock-w', `${w.tool + S.DIVIDER}px`);
  const rect = cv ? cv.getBoundingClientRect() : contentArea();
  const top = rect.top; const height = (rect.height || window.innerHeight);
  const left = side === 'left' ? rect.left : rect.right - w.tool;
  setBox(r.el, { left, top, width: w.tool, height });
  divider.hidden = false;
  setBox(divider, { left: side === 'left' ? left + w.tool : left - S.DIVIDER, top, width: S.DIVIDER, height });
  divider.dataset.tool = r.id;
  divider.dataset.side = side;
  const range = { min: S.MIN_CHAT, max: S.room(total) - Math.max(S.MIN_TOOL, r.opts.min_width) };
  divider.setAttribute('aria-valuemin', String(range.min));
  divider.setAttribute('aria-valuemax', String(range.max));
  divider.setAttribute('aria-valuenow', String(w.chat));
  divider.setAttribute('aria-label', t('tools.divider'));
  divider.setAttribute('aria-controls', `appwin-title-${cssSafe(r.id)}`);
  divider._total = total;
}

// ===================== the chat list beside a dock =====================

/** A dock that pushed the thread list away leaves a "Show chat list" button in
 *  the chat header: the list comes back over the chat until a chat is picked. */
let listBtn = null;
function paintListToggle() {
  const nolist = document.body.classList.contains('appwin-dock-nolist') && !mobile();
  if (!nolist) document.body.classList.remove('appwin-list-peek');
  const head = document.querySelector('#chatview .chat-header');
  if (!head) return;
  if (!listBtn || !head.contains(listBtn)) {
    listBtn = el('button', { type: 'button', class: 'icon-btn ghost appwin-list-btn', 'aria-expanded': 'false' });
    listBtn.append(spriteIcon('menu', 18));
    listBtn.addEventListener('click', () => {
      const on = !document.body.classList.contains('appwin-list-peek');
      document.body.classList.toggle('appwin-list-peek', on);
      listBtn.setAttribute('aria-expanded', String(on));
      if (on) { const first = document.querySelector('.threadlist button, .threadlist [tabindex="0"]'); if (first) first.focus({ preventScroll: true }); }
    });
    const back = $('back-btn');
    if (back && back.parentElement === head) back.after(listBtn); else head.prepend(listBtn);
    const tl = document.querySelector('.threadlist');
    if (tl) {
      // Picking a chat (or Escape) puts the list away again.
      tl.addEventListener('click', (e) => {
        if (document.body.classList.contains('appwin-list-peek') && e.target.closest('.thread-item, [data-thread], [data-id]')) closeListPeek(false);
      });
      tl.addEventListener('keydown', (e) => { if (e.key === 'Escape' && document.body.classList.contains('appwin-list-peek')) { e.preventDefault(); closeListPeek(true); } });
    }
  }
  const label = t('threads.expand');
  listBtn.setAttribute('aria-label', label);
  listBtn.setAttribute('title', label);
  listBtn.hidden = !nolist;
  listBtn.setAttribute('aria-expanded', String(document.body.classList.contains('appwin-list-peek')));
}
function closeListPeek(refocus) {
  document.body.classList.remove('appwin-list-peek');
  if (listBtn) { listBtn.setAttribute('aria-expanded', 'false'); if (refocus) listBtn.focus({ preventScroll: true }); }
}

// ===================== the pill tray =====================

function paintTray() {
  if (!tray) return;
  const list = S.pills(model).map((w) => recs.get(w.id)).filter(Boolean);
  tray.setAttribute('aria-label', t('win.tray'));
  const ae = document.activeElement;
  const focusedId = ae && tray.contains(ae) ? ae.closest('.appwin-pill')?.dataset.tool : null;
  const focusedX = !!(ae && ae.classList.contains('appwin-pill-x'));
  // Roving tab stop: the tray is ONE stop; which item holds it survives a repaint.
  const stopId = tray._stop || null;
  const scroll = trayRow ? trayRow.scrollLeft : 0;
  trayRow.textContent = '';
  for (const r of list) trayRow.append(pillFor(r));
  tray.hidden = !list.length;
  document.body.classList.toggle('appwin-has-pills', !!list.length);
  const items = trayItems();
  const keep = (stopId && items.find((b) => itemKey(b) === stopId)) || items[0];
  for (const b of items) b.tabIndex = b === keep ? 0 : -1;
  trayRow.scrollLeft = scroll;
  paintFade();
  if (focusedId) {
    const p = trayRow.querySelector(`.appwin-pill[data-tool="${cssId(focusedId)}"] ${focusedX ? '.appwin-pill-x' : '.appwin-pill-main'}`);
    if (p) focusTrayItem(p);
  }
}

function trayItems() { return trayRow ? [...trayRow.querySelectorAll('.appwin-pill-main, .appwin-pill-x')] : []; }
function itemKey(b) { const w = b.closest('.appwin-pill'); return w ? `${w.dataset.tool}|${b.classList.contains('appwin-pill-x') ? 'x' : 'm'}` : ''; }
function focusTrayItem(b) {
  for (const x of trayItems()) x.tabIndex = x === b ? 0 : -1;
  tray._stop = itemKey(b);
  b.focus({ preventScroll: false });
}

/** A fade on an edge that hides more pills (the row scrolls sideways). */
function paintFade() {
  if (!trayRow) return;
  const max = trayRow.scrollWidth - trayRow.clientWidth;
  const pos = Math.abs(trayRow.scrollLeft);    // RTL scrolls negative
  tray.classList.toggle('fade-start', max > 1 && pos > 1);
  tray.classList.toggle('fade-end', max > 1 && pos < max - 1);
}

function pillFor(r) {
  const L = r.live;
  const name = displayTitle(r);
  const w = S.find(model, r.id);
  const att = !!(w && w.att);
  const wrap = el('div', { class: 'appwin-pill' + (att ? ' attention' : '') + (L.busy ? ' busy' : '') + (r.asleep ? ' asleep' : ''), dataset: { tool: r.id } });
  let label = L.status ? t('win.pill_status', { name, status: L.status }) : name;
  if (att) label = t('win.finished', { name: label });
  const tip = r.asleep ? t('win.asleep', { name }) : t('win.restore', { name: label });
  const main = el('button', { type: 'button', class: 'appwin-pill-main', 'aria-label': tip, title: tip, 'aria-keyshortcuts': 'Delete' });
  const ic = el('span', { class: 'appwin-pill-icon', 'aria-hidden': 'true' });
  ic.append(r.glyph ? r.glyph() : '');
  main.append(ic, el('span', { class: 'appwin-pill-name', text: name }));
  if (r.asleep) main.append(el('span', { class: 'appwin-pill-status muted', text: t('win.asleep_short'), 'aria-hidden': 'true' }));
  else if (L.status) main.append(el('span', { class: 'appwin-pill-status muted', text: L.status }));
  if (L.badge) main.append(el('span', { class: 'appwin-pill-badge', text: String(L.badge), 'aria-hidden': 'true' }));
  main.addEventListener('click', (e) => {
    if (wrap.dataset.dragged === '1') { e.preventDefault(); return; }
    openWindow(r.id);
  });
  const x = el('button', { type: 'button', class: 'appwin-pill-x', 'aria-label': t('win.close', { name }), title: t('win.close', { name }) });
  x.append(spriteIcon('x', 14));
  x.addEventListener('click', () => closeWindow(r.id));
  wrap.append(main, x);
  wirePillDrag(r, wrap, main);
  return wrap;
}

/** The tray sits right above the composer when a chat is showing it, else above
 *  the phone's bottom tabs, else at the bottom edge. A covering window (full,
 *  sheet) has the tray at the bottom of the content area instead; on a phone
 *  under a sheet it is a solid strip at the very bottom that takes the tab
 *  bar's place (the tabs hide: nothing behind it can be tapped). On a phone
 *  it hides while the on-screen keyboard is up. */
function placeTray() {
  if (!tray) return;
  const phone = mobile();
  const vv = window.visualViewport;
  const kbd = phone && vv && vv.height < window.innerHeight * 0.75;
  tray.classList.toggle('kbd-hidden', !!kbd);
  const cover = document.body.classList.contains('appwin-covers');
  const strip = phone && document.body.classList.contains('appwin-sheet');
  tray.classList.toggle('strip', strip);
  if (tray.hidden) { setTrayVar(0); return; }
  const a = contentArea();
  let bottom = 8;
  let left = a.left + 8; let width = a.width - 16;
  if (strip) { bottom = 0; left = 0; width = window.innerWidth; } else if (!cover) {
    const comp = $('composer');
    const cv = $('chatview');
    const compOn = comp && comp.offsetParent !== null && comp.getBoundingClientRect().height > 0;
    if (compOn) {
      const cr = comp.getBoundingClientRect();
      bottom = window.innerHeight - cr.top + 6;
      const cvr = cv ? cv.getBoundingClientRect() : cr;
      // the chat column, minus a docked tool's room
      let l = cvr.left; let w = cvr.width;
      const dw = parseFloat(cv && cv.style.getPropertyValue('--appwin-dock-w')) || 0;
      if (document.body.classList.contains('appwin-dock-left')) { l += dw; w -= dw; } else if (document.body.classList.contains('appwin-dock-right')) w -= dw;
      left = l + 8; width = w - 16;
    } else if (phone) {
      const tabs = $('mobile-tabs');
      // position: fixed, so offsetParent is always null: ask the style.
      if (tabs && getComputedStyle(tabs).display !== 'none') bottom = window.innerHeight - tabs.getBoundingClientRect().top + 6;
    }
  }
  tray.style.bottom = `${Math.max(0, Math.round(bottom))}px`;
  tray.style.left = `${Math.round(left)}px`;
  tray.style.width = `${Math.max(0, Math.round(width))}px`;
  paintFade();
  setTrayVar(kbd || cover ? 0 : tray.offsetHeight + 6);
}

/** The message list keeps its last message clear of the tray, and a chat that
 *  was scrolled to the bottom stays there when the tray appears or grows. */
function setTrayVar(px) {
  const root = document.documentElement;
  const prev = root.style.getPropertyValue('--appwin-tray-space');
  const next = `${px}px`;
  if (prev === next) return;
  const m = $('messages');
  const pinned = !!m && m.scrollHeight - m.scrollTop - m.clientHeight < 48;
  root.style.setProperty('--appwin-tray-space', next);
  if (pinned) m.scrollTop = m.scrollHeight;
}

function onTrayKey(e) {
  const items = trayItems();
  const i = items.indexOf(document.activeElement);
  const wrap = document.activeElement && document.activeElement.closest('.appwin-pill');
  if ((e.key === 'Delete' || e.key === 'Backspace') && wrap) {
    e.preventDefault();
    closeWindow(wrap.dataset.tool);
    return;
  }
  if (i < 0) return;
  const fwd = isRtl() ? 'ArrowLeft' : 'ArrowRight';
  const back = isRtl() ? 'ArrowRight' : 'ArrowLeft';
  let j = null;
  if (e.key === fwd) j = Math.min(items.length - 1, i + 1);
  else if (e.key === back) j = Math.max(0, i - 1);
  else if (e.key === 'Home') j = 0;
  else if (e.key === 'End') j = items.length - 1;
  if (j === null) return;
  e.preventDefault();
  focusTrayItem(items[j]);
}

/** Drag a pill UP and let go to close it. A sideways move is the row scrolling
 *  (touch-action: pan-x) and never closes. The ✕ beside it is the one-tap way
 *  (WCAG 2.5.7); a profile that may not drag-close (a Basic profile: a child's
 *  phone) only has the ✕. */
function wirePillDrag(r, wrap, main) {
  let start = null;
  main.addEventListener('pointerdown', (e) => {
    if (e.button !== 0 || (deps && typeof deps.canDragClose === 'function' && !deps.canDragClose())) return;
    start = { x: e.clientX, y: e.clientY, id: e.pointerId, moved: false, armed: false, side: false };
    wrap.dataset.dragged = '';
  });
  main.addEventListener('pointermove', (e) => {
    if (!start || e.pointerId !== start.id || start.side) return;
    const dx = e.clientX - start.x; const dy = e.clientY - start.y;
    if (!start.moved && Math.hypot(dx, dy) < 8) return;
    if (!start.moved) {
      // Mostly sideways: the row's own scroll, not a close drag.
      if (Math.abs(dx) > Math.abs(dy) || dy > 0) { start.side = true; return; }
      start.moved = true;
      try { main.setPointerCapture(e.pointerId); } catch { /* not fatal */ }
      wrap.classList.add('dragging');
    }
    wrap.style.transform = `translateY(${Math.min(0, dy)}px)`;
    const armed = dy <= -CLOSE_DRAG_Y;
    if (armed !== start.armed) {
      start.armed = armed;
      wrap.classList.toggle('armed', armed);
      const n = wrap.querySelector('.appwin-pill-name');
      if (n) n.textContent = armed ? t('win.release_close') : displayTitle(r);
    }
  });
  const end = (e) => {
    if (!start || e.pointerId !== start.id) return;
    const s = start; start = null;
    try { main.releasePointerCapture(e.pointerId); } catch { /* released */ }
    wrap.classList.remove('dragging', 'armed');
    wrap.style.transform = '';
    if (s.moved || s.side) {
      wrap.dataset.dragged = '1';
      setTimeout(() => { wrap.dataset.dragged = ''; }, 0);
    }
    if (s.moved) {
      if (s.armed && e.type === 'pointerup') closeWindow(r.id).then((gone) => { if (!gone) paintTray(); });
      else paintTray();
    }
  };
  main.addEventListener('pointerup', end);
  main.addEventListener('pointercancel', end);
}

// ===================== layout menu =====================

function toggleLayoutMenu(r) {
  if (r.menu) { closeLayoutMenu(r, true); return; }
  const menu = el('div', { class: 'appwin-menu', role: 'menu', 'aria-label': t('win.layout') });
  const cur = prefs(r).layout;
  for (const lay of S.LAYOUTS) {
    if (S.DOCKS.includes(lay) && !r.opts.dockable) continue;
    const item = el('button', { type: 'button', class: 'appwin-menu-item', role: 'menuitemradio', 'aria-checked': lay === cur ? 'true' : 'false', tabindex: '-1', dataset: { layout: lay } });
    item.append(lineIcon(ICONS[lay], 16), el('span', { text: t(LAYOUT_LABEL[lay]) }));
    item.addEventListener('click', () => { closeLayoutMenu(r, true); chooseLayout(r, lay); });
    menu.append(item);
  }
  menu.addEventListener('keydown', (e) => {
    const items = [...menu.querySelectorAll('[role="menuitemradio"]')];
    const i = items.indexOf(document.activeElement);
    if (e.key === 'ArrowDown') { e.preventDefault(); items[(i + 1) % items.length].focus(); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); items[(i - 1 + items.length) % items.length].focus(); }
    else if (e.key === 'Home') { e.preventDefault(); items[0].focus(); }
    else if (e.key === 'End') { e.preventDefault(); items[items.length - 1].focus(); }
    else if (e.key === 'Tab') closeLayoutMenu(r, false);
  });
  r.bar.append(menu);
  r.menu = menu;
  r.layoutBtn.setAttribute('aria-expanded', 'true');
  const checked = menu.querySelector('[aria-checked="true"]') || menu.firstElementChild;
  if (checked) checked.focus();
  r.menuOutside = (e) => { if (!menu.contains(e.target) && e.target !== r.layoutBtn && !r.layoutBtn.contains(e.target)) closeLayoutMenu(r, false); };
  document.addEventListener('pointerdown', r.menuOutside, true);
}

function closeLayoutMenu(r, refocus) {
  if (!r.menu) return;
  r.menu.remove();
  r.menu = null;
  document.removeEventListener('pointerdown', r.menuOutside, true);
  r.layoutBtn.setAttribute('aria-expanded', 'false');
  if (refocus) r.layoutBtn.focus({ preventScroll: true });
}

function chooseLayout(r, lay) {
  model = S.setLayout(model, r.id, lay, r.opts);
  if (S.DOCKS.includes(lay) && !S.dockMode(dockRoom(), r.opts) && deps && typeof deps.toast === 'function') deps.toast(t('win.narrow'));
  apply({ quiet: true });
  paintLayoutBtn(r);
}

function paintLayoutBtn(r) {
  const lay = prefs(r).layout;
  r.layoutBtn.replaceChildren(lineIcon(ICONS[lay] || ICONS.float, 18));
}

// ===================== move, snap, resize =====================

let dragging = null;

function startDragUi() { document.body.classList.add('appwin-dragging'); }
function endDragUi() { document.body.classList.remove('appwin-dragging'); if (preview) preview.hidden = true; }

function wireMove(r) {
  r.handle.addEventListener('pointerdown', (e) => {
    if (e.button !== 0 || mobile() || !r.eff) return;
    const box = r.el.getBoundingClientRect();
    dragging = { r, id: e.pointerId, sx: e.clientX, sy: e.clientY, ox: e.clientX - box.left, oy: e.clientY - box.top, moved: false, zone: null };
    // Capture now: a quick first move can leave the 40 px bar before the
    // threshold is crossed, and the drag must still follow the pointer.
    try { r.handle.setPointerCapture(e.pointerId); } catch { /* not fatal */ }
  });
  r.handle.addEventListener('pointermove', (e) => {
    const d = dragging;
    if (!d || d.r !== r || e.pointerId !== d.id) return;
    if (!d.moved) {
      if (Math.hypot(e.clientX - d.sx, e.clientY - d.sy) < 6) return;
      d.moved = true;
      startDragUi();
      if (r.eff !== 'float') {
        // Pulled out of a dock / full: a float under the pointer, its remembered size.
        const b = floatBounds();
        const p = prefs(r);
        const size = S.clampRect(p.rect || S.initialRect(r.opts, b, { rtl: isRtl() }), b, r.opts);
        d.ox = Math.min(size.w - 40, Math.max(40, size.w / 2));
        d.oy = 16;
        model = S.setRect(model, r.id, { ...size, x: e.clientX - b.left - d.ox, y: e.clientY - b.top - d.oy });
        model = S.setLayout(model, r.id, 'float', r.opts);
        apply({ save: false, quiet: true });
      }
    }
    const b = floatBounds();
    const p = prefs(r);
    const cur = S.clampRect(p.rect, b, r.opts);
    const rect = S.clampRect({ ...cur, x: e.clientX - b.left - d.ox, y: e.clientY - b.top - d.oy }, b, r.opts);
    setBox(r.el, { left: b.left + rect.x, top: b.top + rect.y, width: rect.w, height: rect.h });
    d.rect = rect;
    d.zone = S.snapZone({ x: e.clientX - b.left, y: e.clientY - b.top }, b, { dockable: r.opts.dockable });
    showPreview(d.zone, r);
  });
  const end = (e) => {
    const d = dragging;
    if (!d || d.r !== r || e.pointerId !== d.id) return;
    dragging = null;
    try { r.handle.releasePointerCapture(e.pointerId); } catch { /* released */ }
    endDragUi();
    if (!d.moved) return;
    if (d.rect) model = S.setRect(model, r.id, d.rect);
    if (d.zone && e.type === 'pointerup') chooseLayout(r, d.zone);
    else apply({ quiet: true });
  };
  r.handle.addEventListener('pointerup', end);
  r.handle.addEventListener('pointercancel', end);
}

function showPreview(zone, r) {
  if (!preview) return;
  if (!zone) { preview.hidden = true; return; }
  const a = contentArea();
  if (zone === 'full') setBox(preview, { left: a.left, top: a.top, width: a.width, height: a.height });
  else {
    const room = dockRoom();
    const mode = S.dockMode(room, r.opts);
    if (!mode) { setBox(preview, { left: a.left, top: a.top, width: a.width, height: a.height }); }
    else {
      const w = S.dockWidths(prefs(r).ratio, mode === 'nolist' ? room.noList : room.withList, r.opts);
      const left = zone === 'left' ? (mode === 'nolist' || isRtl() ? a.left : a.left + (room.noList - room.withList)) : a.left + a.width - w.tool;
      setBox(preview, { left, top: a.top, width: w.tool, height: a.height });
    }
  }
  preview.hidden = false;
}

function wireResize(r) {
  for (const h of r.el.querySelectorAll('.appwin-rs')) {
    h.addEventListener('pointerdown', (e) => {
      if (e.button !== 0 || r.eff !== 'float') return;
      e.preventDefault();
      const b = floatBounds();
      const start = S.clampRect(prefs(r).rect || S.initialRect(r.opts, b, { rtl: isRtl() }), b, r.opts);
      const dir = h.dataset.dir;
      const s = { id: e.pointerId, x: e.clientX, y: e.clientY, start, dir, rect: start };
      try { h.setPointerCapture(e.pointerId); } catch { /* not fatal */ }
      startDragUi();
      const move = (ev) => {
        if (ev.pointerId !== s.id) return;
        const dx = ev.clientX - s.x; const dy = ev.clientY - s.y;
        let { x, y, w, hh } = { x: start.x, y: start.y, w: start.w, hh: start.h };
        if (dir.includes('e')) w = start.w + dx;
        if (dir.includes('w')) { w = start.w - dx; x = start.x + dx; }
        if (dir.includes('s')) hh = start.h + dy;
        // never past the minimum: a west edge stops moving x once the width is pinned
        const minW = Math.min(b.w, r.opts.min_width);
        if (w < minW) { if (dir.includes('w')) x -= (minW - w); w = minW; }
        s.rect = S.clampRect({ x, y, w, h: hh }, b, r.opts);
        setBox(r.el, { left: b.left + s.rect.x, top: b.top + s.rect.y, width: s.rect.w, height: s.rect.h });
      };
      const up = (ev) => {
        if (ev.pointerId !== s.id) return;
        h.removeEventListener('pointermove', move);
        h.removeEventListener('pointerup', up);
        h.removeEventListener('pointercancel', up);
        endDragUi();
        model = S.setRect(model, r.id, s.rect);
        apply({ quiet: true });
      };
      h.addEventListener('pointermove', move);
      h.addEventListener('pointerup', up);
      h.addEventListener('pointercancel', up);
    });
  }
}

/** Keyboard on the title (move handle): arrows move a float, Alt+arrows resize
 *  it, Enter toggles full. */
function onHandleKey(r, e) {
  if (mobile()) return;
  if (e.key === 'Enter' || e.key === ' ') {
    e.preventDefault();
    chooseLayout(r, r.eff === 'full' ? 'float' : 'full');
    return;
  }
  if (r.eff !== 'float' || e.ctrlKey || e.metaKey) return;
  const b = floatBounds();
  const cur = S.clampRect(prefs(r).rect || S.initialRect(r.opts, b, { rtl: isRtl() }), b, r.opts);
  const next = S.keyRect(e.key, cur, b, r.opts, { shift: e.shiftKey, alt: e.altKey });
  if (!next) return;
  e.preventDefault();
  model = S.setRect(model, r.id, next);
  apply({ quiet: true });
}

/** Phone: swipe the sheet's bar down to minimise it (⌄ is the tap way). */
function wireSwipe(r) {
  let s = null;
  r.bar.addEventListener('pointerdown', (e) => {
    if (!mobile() || r.eff !== 'sheet' || e.target.closest('button:not(.appwin-handle)')) return;
    s = { id: e.pointerId, y: e.clientY, dy: 0 };
  });
  r.bar.addEventListener('pointermove', (e) => {
    if (!s || e.pointerId !== s.id) return;
    s.dy = Math.max(0, e.clientY - s.y);
    if (s.dy > 6) { try { r.bar.setPointerCapture(e.pointerId); } catch { /* not fatal */ } }
    if (!reduceMotion()) r.el.style.transform = `translateY(${s.dy}px)`;
  });
  const end = (e) => {
    if (!s || e.pointerId !== s.id) return;
    const dy = s.dy; s = null;
    r.el.style.transform = '';
    try { r.bar.releasePointerCapture(e.pointerId); } catch { /* released */ }
    if (dy >= SHEET_SWIPE && e.type === 'pointerup') minimizeWindow(r.id);
  };
  r.bar.addEventListener('pointerup', end);
  r.bar.addEventListener('pointercancel', end);
}

// ===================== the divider =====================

function wireDivider() {
  let drag = null;
  const rec = () => recs.get(divider.dataset.tool);
  divider.addEventListener('pointerdown', (e) => {
    if (e.button !== 0 || divider.hidden) return;
    e.preventDefault();
    drag = e.pointerId;
    try { divider.setPointerCapture(e.pointerId); } catch { /* not fatal */ }
    startDragUi();
  });
  divider.addEventListener('pointermove', (e) => {
    if (drag !== e.pointerId) return;
    const r = rec();
    const cv = $('chatview');
    if (!r || !cv) return;
    const rect = cv.getBoundingClientRect();
    const side = divider.dataset.side;
    const toolPx = side === 'left' ? e.clientX - rect.left - S.DIVIDER / 2 : rect.right - e.clientX - S.DIVIDER / 2;
    const ratio = S.ratioForTool(toolPx, divider._total, r.opts);
    if (ratio == null) return;
    model = S.setRatio(model, r.id, ratio);
    apply({ save: false, quiet: true });
  });
  const end = (e) => {
    if (drag !== e.pointerId) return;
    drag = null;
    try { divider.releasePointerCapture(e.pointerId); } catch { /* released */ }
    endDragUi();
    apply({ quiet: true });
  };
  divider.addEventListener('pointerup', end);
  divider.addEventListener('pointercancel', end);
  divider.addEventListener('keydown', (e) => {
    const r = rec();
    if (!r || e.altKey || e.ctrlKey || e.metaKey) return;
    const next = S.stepDock(e.key, { ratio: prefs(r).ratio, total: divider._total, side: divider.dataset.side, shift: e.shiftKey, opts: r.opts });
    if (next == null) return;
    e.preventDefault();
    model = S.setRatio(model, r.id, next);
    apply({ quiet: true });
  });
}

// ===================== confirm =====================

function askConfirm(text) {
  return new Promise((resolve) => {
    if (!confirmDlg) {
      confirmDlg = el('dialog', { class: 'appwin-confirm', 'aria-labelledby': 'appwin-confirm-text' });
      confirmDlg.append(
        el('p', { id: 'appwin-confirm-text' }),
        el('div', { class: 'appwin-confirm-actions' }, [
          el('button', { type: 'button', class: 'btn-secondary', value: 'no' }),
          el('button', { type: 'button', class: 'btn-primary danger', value: 'yes' }),
        ]),
      );
      document.body.append(confirmDlg);
    }
    const [no, yes] = confirmDlg.querySelectorAll('button');
    no.textContent = t('win.keep_open');
    yes.textContent = t('win.close_anyway');
    confirmDlg.querySelector('p').textContent = text;
    let settled = false;
    const done = (v) => {
      if (settled) return;
      settled = true;
      no.onclick = null; yes.onclick = null; confirmDlg.oncancel = null;
      if (confirmDlg.open) confirmDlg.close();
      resolve(v);
    };
    no.onclick = () => done(false);
    yes.onclick = () => done(true);
    confirmDlg.oncancel = (e) => { e.preventDefault(); done(false); };
    // Closed from outside (a lock tears every overlay down): the answer is no.
    confirmDlg.addEventListener('close', () => done(false), { once: true });
    if (typeof confirmDlg.showModal === 'function') { confirmDlg.showModal(); no.focus(); }
    else resolve(window.confirm(text));
  });
}

// ===================== wiring =====================

/** F6: between the window on screen and the chat. */
function onGlobalKey(e) {
  if (e.key !== 'F6' || e.altKey || e.ctrlKey || e.metaKey) return;
  const r = model.open ? recs.get(model.open) : null;
  if (!r) return;
  e.preventDefault();
  if (r.el.contains(document.activeElement)) { if (deps && typeof deps.focusChat === 'function') deps.focusChat(); }
  else r.handle.focus({ preventScroll: true });
}

export function wireAppWindows(d) {
  // Wired once per page; a second call (a test's fresh document) starts clean.
  for (const r of recs.values()) { try { r.el.remove(); } catch { /* gone with its document */ } }
  recs.clear();
  model = S.empty(); profileId = undefined; firstSync = true; lastVisible = null;
  layer = tray = trayRow = divider = preview = confirmDlg = null; listBtn = null;
  sheetPop = null; histWaiters = null;
  deps = d;
  ensureLayer();
  window.addEventListener('resize', relayout);
  if (window.visualViewport) window.visualViewport.addEventListener('resize', relayout);
  document.addEventListener('keydown', onGlobalKey);
  // The composer grows and shrinks as the user types: the tray follows it.
  const comp = $('composer');
  if (comp && typeof ResizeObserver === 'function') new ResizeObserver(() => placeTray()).observe(comp);
}

/** Tests only. */
export function _resetForTest() {
  for (const r of [...recs.values()]) { try { r.el.remove(); } catch { /* */ } }
  recs.clear();
  model = S.empty(); profileId = undefined; firstSync = true; deps = null; host = null;
  layer = tray = trayRow = divider = preview = confirmDlg = null; lastVisible = null; listBtn = null;
  sheetPop = null; histWaiters = null;
}
export function _model() { return model; }
