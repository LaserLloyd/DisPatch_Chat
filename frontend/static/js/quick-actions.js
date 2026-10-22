// Quick actions: one-tap prompt templates ("Proof this", "Summarize", ...).
// Ported from ChatForge (MIT, same author); the server side is
// backend/app/quick_actions.py.
//
// What the user sees:
//   * the empty-thread welcome block shows the actions as chips (desktop);
//   * a small bolt button in the composer opens a menu of the same actions
//     (desktop and mobile);
//   * picking one puts a pill in the composer (label, an x button), changes the
//     placeholder to the action's hint, and the next send carries
//     `action: "<id>"` in its WS frame. The pill is cleared by sending, by the
//     x, and by Backspace in an empty box;
//   * a sent message shows a small tag with the action's label over its bubble;
//   * "Upload a file" heads the menu and "Upload to chat" leads the chips: the
//     composer's attach flow (main.js hands it over with setUpload). While the
//     bolt button is shown it IS the attach button -- the standalone paperclip
//     folds away (class qa-folded) -- and where the bolt is hidden (a bot with
//     `quick_actions: false`) the paperclip stays.
//
// This module owns the action list and the pill; main.js owns the sending (it
// asks `currentAction()` and calls `clearComposerAction()` once the frame is
// out). Every string goes through t(); every node is built with el() and
// textContent, never innerHTML, because the labels come from an operator file.

import { el, railIcon, RAIL_ICONS } from './util.js?v=20';
import { t } from './i18n.js?v=4';

const ICON_BOLT = RAIL_ICONS.bolt;
const ICON_CLOSE = ['M6 6l12 12', 'M18 6 6 18'];
const MAX_TAG = 60;

let actions = [];        // [{id, label, hint, tools}] as served
let current = null;      // the action picked in the composer, or null
let wired = false;
let allowed = true;      // false while the open chat's bot opted out (`quick_actions: false`)
let announceFn = () => {};
let uploadFn = null;     // main.js's "open the attach picker", or null when uploads are off

const $ = (id) => document.getElementById(id);

/** Keep only well-formed entries ({id, label} strings), with string hint and bool tools. */
export function normalizeActions(list) {
  const seen = new Set();
  const out = [];
  for (const a of Array.isArray(list) ? list : []) {
    if (!a || typeof a.id !== 'string' || !a.id || typeof a.label !== 'string' || !a.label.trim()) continue;
    if (seen.has(a.id)) continue;
    seen.add(a.id);
    out.push({
      id: a.id,
      label: a.label.trim(),
      hint: typeof a.hint === 'string' ? a.hint : '',
      tools: !!a.tools,
    });
  }
  return out;
}

/** Let the open chat's bot opt out: no chips, no composer button, no pill. Idempotent. */
export function setAllowed(on) {
  on = on !== false;
  if (on === allowed) return;
  allowed = on;
  if (!allowed) {
    if (current) setComposerAction(null, { focus: false });
    closeQuickMenu({ focusButton: false });
  }
  syncButtons();
}

/** Show the bolt when it has something to offer, and fold the paperclip into it
 *  while it is shown (upload is its first item). Where the bolt is hidden the
 *  paperclip stays, so attaching is always one button away. */
function syncButtons() {
  const btn = $('quick-btn');
  const off = !allowed || (!actions.length && !uploadFn);
  if (btn) btn.hidden = off;
  const attach = $('attach-btn');
  if (attach) attach.classList.toggle('qa-folded', !off && !!uploadFn);
}

/** main.js: the attach flow (`fn()` opens the same picker as the paperclip), or
 *  null to take the upload entries out. */
export function setUpload(fn) {
  uploadFn = typeof fn === 'function' ? fn : null;
  syncButtons();
}

function startUpload() {
  closeQuickMenu({ focusButton: false });
  if (uploadFn) uploadFn();
}

export function getActions() { return actions.slice(); }
export function currentAction() { return current; }

/** The composer placeholder while a pill is set, else null (main.js owns the default). */
export function placeholderOverride() {
  return current ? (current.hint || `${current.label}…`) : null;
}

/** Replace the served list. A picked action follows its new label/hint, or leaves
 *  the composer when the operator removed it. Repaints the composer button. */
export function setActions(list) {
  actions = normalizeActions(list);
  if (current) {
    const now = actions.find((a) => a.id === current.id);
    if (!now) setComposerAction(null, { focus: false });
    else if (now.label !== current.label || now.hint !== current.hint) setComposerAction(now, { focus: false });
  }
  syncButtons();
  if ((!actions.length && !uploadFn) || !allowed) closeQuickMenu({ focusButton: false });
}

/** Fetch the list with `fetchJson` (resolving {actions}) and apply it. Never throws. */
export async function loadActions(fetchJson) {
  try {
    const r = await fetchJson();
    setActions(r && r.actions);
  } catch { /* the chips and the menu simply stay absent */ }
  return actions;
}

// ------------------------------------------------------------------ the pill ----

function applyPlaceholder() {
  const input = $('input');
  if (!input) return;
  const over = placeholderOverride();
  input.placeholder = over != null ? over : t('composer.placeholder');
}

/** Put `action` ({id, label, hint}) in the composer, or take it out (null). Typed text stays. */
export function setComposerAction(action, { focus = true } = {}) {
  current = action ? {
    id: action.id, label: String(action.label || action.id), hint: String(action.hint || ''),
  } : null;
  const bar = $('qa-bar');
  if (bar) {
    if (!current) {
      bar.replaceChildren();
      bar.hidden = true;
    } else {
      const remove = el('button', {
        class: 'qa-pill-remove', type: 'button',
        'aria-label': t('quick.remove', { name: current.label }),
        title: t('quick.remove_title'),
      }, [railIcon(ICON_CLOSE)]);
      remove.addEventListener('click', () => {
        setComposerAction(null);
        announceFn(t('quick.removed'));
      });
      bar.replaceChildren(el('span', {
        class: 'qa-pill', role: 'group', 'aria-label': t('quick.pill', { name: current.label }),
      }, [
        railIcon(ICON_BOLT), el('span', { class: 'qa-pill-label', text: current.label }), remove,
      ]));
      bar.hidden = false;
    }
  }
  applyPlaceholder();
  const btn = $('quick-btn');
  if (btn) btn.classList.toggle('active', !!current);
  const input = $('input');
  if (focus && input) input.focus();
}

export function clearComposerAction(opts) { setComposerAction(null, { focus: false, ...opts }); }

/** A chip or menu item was picked. */
export function chooseAction(action) {
  closeQuickMenu({ focusButton: false });
  setComposerAction(action);
  announceFn(t('quick.picked', { name: action.label }));
}

// ------------------------------------------------------ welcome chips / bubble tag ----

/** The empty-thread block: a short line, then a grid of chips. [] when there are none. */
export function welcomeChips() {
  // The upload chip comes with uploads, not with the served actions: a server with
  // no actions still gets "Upload to chat", like the ⚡ menu still offers upload.
  if (!allowed || (!actions.length && !uploadFn)) return [];
  const grid = el('div', { class: 'qa-grid', role: 'group', 'aria-label': t('quick.title') });
  if (uploadFn) {
    const up = el('button', { class: 'qa-chip qa-upload', type: 'button', title: t('quick.upload_title'), dataset: { upload: '1' } }, [
      railIcon(RAIL_ICONS.attach), el('span', { class: 'qa-label', text: t('quick.upload_chip') }),
    ]);
    up.addEventListener('click', () => startUpload());
    grid.append(up);
  }
  for (const a of actions) {
    const b = el('button', { class: 'qa-chip', type: 'button', title: a.hint || a.label, dataset: { action: a.id } }, [
      railIcon(ICON_BOLT), el('span', { class: 'qa-label', text: a.label }),
    ]);
    b.addEventListener('click', () => chooseAction(a));
    grid.append(b);
  }
  return actions.length ? [el('p', { class: 'qa-intro', text: t('quick.intro') }), grid] : [grid];
}

/** The small tag over a sent message's bubble, or null when it carries no action. */
export function actionTagEl(metadata) {
  const a = metadata && metadata.action;
  if (!a || typeof a !== 'object') return null;
  const raw = typeof a.label === 'string' && a.label.trim() ? a.label.trim() : (typeof a.id === 'string' ? a.id : '');
  if (!raw) return null;
  const label = raw.length > MAX_TAG ? `${raw.slice(0, MAX_TAG - 1)}…` : raw;
  return el('div', { class: 'msg-action', title: t('quick.sent_with', { name: label }) }, [
    railIcon(ICON_BOLT), el('span', { class: 'msg-action-label', text: label }),
  ]);
}

// ------------------------------------------------------------------- the menu ----

const menu = () => $('quick-menu');
export function quickMenuOpen() { const m = menu(); return !!m && !m.hidden; }
const menuItems = () => [...(menu()?.querySelectorAll('[role="menuitem"]') || [])];

function buildMenu() {
  const items = actions.map((a) => {
    const isCurrent = !!current && current.id === a.id;
    const b = el('button', {
      class: `qm-item${isCurrent ? ' is-current' : ''}`, type: 'button', role: 'menuitem', tabindex: '-1',
      title: a.hint || a.label, dataset: { action: a.id },
    }, [
      el('span', { class: 'qm-check', 'aria-hidden': 'true' }, isCurrent ? [railIcon(RAIL_ICONS.tick)] : []),
      el('span', { class: 'qm-label', text: a.label }),
      a.tools ? el('span', { class: 'qm-tag', text: t('quick.web') }) : null,
    ]);
    b.addEventListener('click', () => chooseAction(a));
    return b;
  });
  const upload = uploadFn ? [el('button', {
    class: 'qm-item qm-upload', type: 'button', role: 'menuitem', tabindex: '-1',
    title: t('quick.upload_title'), dataset: { upload: '1' },
  }, [
    el('span', { class: 'qm-check qm-icon', 'aria-hidden': 'true' }, [railIcon(RAIL_ICONS.attach)]),
    el('span', { class: 'qm-label', text: t('quick.upload') }),
  ])] : [];
  if (upload.length) upload[0].addEventListener('click', () => startUpload());
  menu().replaceChildren(
    ...upload,
    ...(upload.length && items.length ? [el('div', { class: 'qm-sep', role: 'separator' })] : []),
    ...(items.length ? [el('div', { class: 'qm-head', 'aria-hidden': 'true', text: t('quick.title') }), ...items]
      : upload.length ? [] : [el('div', { class: 'qm-head', 'aria-hidden': 'true', text: t('quick.title') }),
        el('div', { class: 'qm-empty', text: t('quick.none') })]),
  );
}

export function openQuickMenu() {
  const m = menu();
  if (!m || (!actions.length && !uploadFn) || !allowed) return;
  buildMenu();
  m.hidden = false;
  $('quick-btn')?.setAttribute('aria-expanded', 'true');
  const items = menuItems();
  (items.find((b) => b.classList.contains('is-current')) || items[0])?.focus();
}

export function closeQuickMenu({ focusButton = true } = {}) {
  const m = menu();
  if (!m || m.hidden) return;
  m.hidden = true;
  const btn = $('quick-btn');
  btn?.setAttribute('aria-expanded', 'false');
  if (focusButton) btn?.focus();
}

function onMenuKey(e) {
  const items = menuItems();
  const i = items.indexOf(document.activeElement);
  if (e.key === 'ArrowDown') { e.preventDefault(); items[(i + 1) % items.length]?.focus(); }
  else if (e.key === 'ArrowUp') { e.preventDefault(); items[(i - 1 + items.length) % items.length]?.focus(); }
  else if (e.key === 'Home') { e.preventDefault(); items[0]?.focus(); }
  else if (e.key === 'End') { e.preventDefault(); items[items.length - 1]?.focus(); }
  else if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); closeQuickMenu(); }
  else if (e.key === 'Tab') closeQuickMenu({ focusButton: false });
}

/** Wire the composer button, the menu and Backspace. Call once after the DOM exists.
 *  `announce(text)` speaks to screen readers. */
export function wireQuickActions({ announce } = {}) {
  if (wired) return;
  const btn = $('quick-btn');
  const m = menu();
  if (!btn || !m) return;
  wired = true;
  if (typeof announce === 'function') announceFn = announce;
  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    if (quickMenuOpen()) closeQuickMenu(); else openQuickMenu();
  });
  btn.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowUp' || e.key === 'ArrowDown') { e.preventDefault(); openQuickMenu(); }
  });
  m.addEventListener('keydown', onMenuKey);
  document.addEventListener('click', (e) => {
    if (quickMenuOpen() && !e.target.closest('#quick-menu, #quick-btn')) closeQuickMenu({ focusButton: false });
  });
  // Backspace in an empty box takes the pill out, like a chip in a search field.
  $('input')?.addEventListener('keydown', (e) => {
    if (e.key === 'Backspace' && current && !e.target.value && !e.isComposing) {
      e.preventDefault();
      setComposerAction(null);
      announceFn(t('quick.removed'));
    }
  });
}

/** Repaint what holds translated text (language switch). */
export function repaintQuickActions() {
  if (current) setComposerAction(current, { focus: false });
  if (quickMenuOpen()) buildMenu();
}

/** Test hook: forget module state. */
export function _reset() {
  actions = []; current = null; wired = false; allowed = true; announceFn = () => {}; uploadFn = null;
}
