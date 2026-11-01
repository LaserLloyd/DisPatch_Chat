// Settings window: drag its edges or corner to resize it on a desktop, maximize
// and restore it, and remember the size on this device.
//
// The dialog stays centred, so dragging an edge moves it by half the size change
// on each side: the edge you hold follows the pointer. Desktop only (a fine
// pointer and a viewport of 900px or more, the same width where Settings shows
// its side nav); a phone keeps the full-screen drill-in and gets no handles.
// What the sections look like at each width is CSS container queries on the
// dialog (app.css, "Settings window sizes"), never the viewport.
//
// Pure first (defaultSize, clampSize, parseStored, edgeResize, nudge, load/saveSize)
// so the tests need no DOM; mountSettingsResize is the DOM half and only runs
// when main.js calls it.

import { t } from './i18n.js?v=5';

export const MIN_W = 640;
export const MIN_H = 420;
/** Space kept free around the window at its largest, each side. */
export const MARGIN = 12;
/** The remembered size. Listed in privacy.js PERSISTED_KEYS so privacy mode wipes it. */
export const SIZE_KEY = 'dispatch-settings-size';
const STEP = 24;
const BIG_STEP = 96;

const num = (v) => (typeof v === 'number' && Number.isFinite(v) ? v : NaN);

/** The most the window may grow to inside a vw x vh viewport. */
export function maxSize(vw, vh) {
  return { w: Math.max(0, vw - 2 * MARGIN), h: Math.max(0, vh - 2 * MARGIN) };
}

/** The size the dialog had before it could be resized: 980x760 capped at 96vw x 92vh. */
export function defaultSize(vw, vh) {
  return { w: Math.round(Math.min(980, vw * 0.96)), h: Math.round(Math.min(760, vh * 0.92)) };
}

/** The size Restore goes back to. A remembered size that IS the maximum would make Restore
 *  look like nothing happened, so it (and no size at all) becomes the default size. */
export function restoreSize(size, vw, vh) {
  const m = maxSize(vw, vh);
  if (!size || (Math.abs(size.w - m.w) <= 2 && Math.abs(size.h - m.h) <= 2)) return defaultSize(vw, vh);
  return clampSize(size, vw, vh);
}

/** Keep a size between the minimum and the viewport minus the margin. A viewport
 *  smaller than the minimum wins over the minimum. Junk gives the default. */
export function clampSize(size, vw, vh) {
  const w = num(size && size.w);
  const h = num(size && size.h);
  if (Number.isNaN(w) || Number.isNaN(h)) return defaultSize(vw, vh);
  const max = maxSize(vw, vh);
  return {
    w: Math.round(Math.min(Math.max(w, Math.min(MIN_W, max.w)), max.w)),
    h: Math.round(Math.min(Math.max(h, Math.min(MIN_H, max.h)), max.h)),
  };
}

/** A stored value back into {w, h, max}, or null when it is not a sane size. */
export function parseStored(raw) {
  let o;
  try { o = JSON.parse(raw); } catch { return null; }
  if (!o || typeof o !== 'object') return null;
  const w = num(o.w);
  const h = num(o.h);
  if (Number.isNaN(w) || Number.isNaN(h) || w < 1 || h < 1 || w > 20000 || h > 20000) return null;
  return { w: Math.round(w), h: Math.round(h), max: o.max === true };
}

function store() {
  try { return globalThis.localStorage || null; } catch { return null; }
}

/** The remembered size, or null (nothing stored, or storage blocked). */
export function loadSize(storage = store()) {
  try { return storage ? parseStored(storage.getItem(SIZE_KEY)) : null; } catch { return null; }
}

/** Remember a size ({w, h, max}); null forgets it. Never throws. `storage` is for
 *  tests; the app writes localStorage itself (privacy-keys.test.js reads that call). */
export function saveSize(size, storage = null) {
  const v = size ? JSON.stringify({ w: Math.round(size.w), h: Math.round(size.h), max: size.max === true }) : '';
  try {
    if (storage) storage.setItem(SIZE_KEY, v);
    else localStorage.setItem(SIZE_KEY, v);
  } catch { /* storage blocked: the size just is not remembered */ }
}

/** The size after the pointer travelled (dx, dy) from where it pressed `edge`
 *  (n s e w ne nw se sw). Doubled, because the window stays centred. */
export function edgeResize(start, edge, dx, dy) {
  let { w, h } = start;
  if (edge.includes('e')) w += 2 * dx;
  if (edge.includes('w')) w -= 2 * dx;
  if (edge.includes('s')) h += 2 * dy;
  if (edge.includes('n')) h -= 2 * dy;
  return { w, h };
}

/** The size after an arrow key on the keyboard handle (Shift = a bigger step),
 *  or null for any other key. `rtl` swaps left and right so the arrow points the
 *  way the window's edge moves. */
export function nudge(size, key, shift, rtl = false) {
  const d = shift ? BIG_STEP : STEP;
  const grow = rtl ? 'ArrowLeft' : 'ArrowRight';
  const shrink = rtl ? 'ArrowRight' : 'ArrowLeft';
  if (key === grow) return { w: size.w + d, h: size.h };
  if (key === shrink) return { w: size.w - d, h: size.h };
  if (key === 'ArrowDown') return { w: size.w, h: size.h + d };
  if (key === 'ArrowUp') return { w: size.w, h: size.h - d };
  return null;
}

const EDGES = ['n', 's', 'e', 'w', 'ne', 'nw', 'se', 'sw'];

/** Wire the resize handles and the maximize button onto the Settings dialog.
 *  Returns { apply, reset } for callers and tests. */
export function mountSettingsResize({ modal, maxBtn, tabs }) {
  if (!modal) return null;
  const mq = globalThis.matchMedia ? globalThis.matchMedia('(pointer: fine) and (min-width: 900px)') : null;
  const enabled = () => !!(mq && mq.matches);
  const view = () => ({ vw: globalThis.innerWidth, vh: globalThis.innerHeight });
  const rtl = () => getComputedStyle(modal).direction === 'rtl';

  // size: what the window is when not maximized (null = the default); max: maximized.
  let size = null;
  let max = false;
  const saved = loadSize();
  if (saved) { size = { w: saved.w, h: saved.h }; max = saved.max; }

  function effective() {
    const { vw, vh } = view();
    if (max) return maxSize(vw, vh);
    return size ? clampSize(size, vw, vh) : defaultSize(vw, vh);
  }

  function paintMaxBtn() {
    if (!maxBtn) return;
    const label = t(max ? 'settings.restore' : 'settings.maximize');
    maxBtn.setAttribute('aria-pressed', max ? 'true' : 'false');
    maxBtn.setAttribute('aria-label', label);
    maxBtn.title = label;
  }

  /** Put the size on the dialog; re-run on a viewport change so it never overflows. */
  function apply() {
    paintMaxBtn();
    if (!enabled() || (!size && !max)) {
      modal.classList.remove('is-sized');
      modal.style.removeProperty('--sm-w');
      modal.style.removeProperty('--sm-h');
      return;
    }
    const e = effective();
    modal.classList.add('is-sized');
    modal.classList.toggle('is-maximized', max);
    modal.style.setProperty('--sm-w', `${e.w}px`);
    modal.style.setProperty('--sm-h', `${e.h}px`);
  }

  const persist = () => saveSize(size || max ? { ...(size || effective()), max } : null);

  function set(next, keepMax = false) {
    const { vw, vh } = view();
    size = clampSize(next, vw, vh);
    max = keepMax;
    apply();
  }

  function reset() {
    size = null;
    max = false;
    apply();
    persist();
  }

  function toggleMax() {
    if (!enabled()) return;
    if (!max) { if (!size) size = effective(); }   // Restore comes back to what it was
    else { const { vw, vh } = view(); size = restoreSize(size, vw, vh); }
    max = !max;
    apply();
    persist();
  }

  // Handles: eight edges and corners for the pointer, and the bottom corner
  // doubles as the keyboard handle (focusable, arrow keys, an aria-label).
  let drag = null;
  const mk = (edge) => {
    const keyboard = edge === 'se';
    const h = document.createElement(keyboard ? 'button' : 'div');
    h.className = `sr-handle sr-${edge}${keyboard ? ' sr-grip' : ''}`;
    h.dataset.edge = edge;
    if (keyboard) {
      h.type = 'button';
      h.setAttribute('aria-label', t('settings.resize'));
      h.title = t('settings.resize');
    } else {
      h.setAttribute('aria-hidden', 'true');
    }
    h.addEventListener('pointerdown', (ev) => {
      if (!enabled() || ev.button !== 0) return;
      ev.preventDefault();
      // The grip sits at the bottom-inline-end corner: bottom-left in RTL.
      let edgeName = edge;
      if (keyboard && rtl()) edgeName = 'sw';
      h.setPointerCapture(ev.pointerId);
      drag = { id: ev.pointerId, edge: edgeName, x: ev.clientX, y: ev.clientY, start: effective() };
      modal.classList.add('is-resizing');
    });
    h.addEventListener('pointermove', (ev) => {
      if (!drag || ev.pointerId !== drag.id) return;
      set(edgeResize(drag.start, drag.edge, ev.clientX - drag.x, ev.clientY - drag.y));
    });
    const end = (ev) => {
      if (!drag || ev.pointerId !== drag.id) return;
      drag = null;
      modal.classList.remove('is-resizing');
      persist();
    };
    h.addEventListener('pointerup', end);
    h.addEventListener('pointercancel', end);
    h.addEventListener('dblclick', (ev) => { ev.preventDefault(); reset(); });
    if (keyboard) {
      h.addEventListener('keydown', (ev) => {
        if (!enabled()) return;
        if (ev.key === 'Home' || ev.key === 'Enter') { ev.preventDefault(); reset(); return; }
        const next = nudge(effective(), ev.key, ev.shiftKey, rtl());
        if (!next) return;
        ev.preventDefault();
        set(next);
        persist();
      });
    }
    return h;
  };
  for (const e of EDGES) modal.appendChild(mk(e));

  // Icons-only nav (a narrow window, app.css): the label is the tooltip. Kept on
  // the buttons always; it costs nothing at wider sizes where the text shows.
  const labelTabs = () => {
    if (!tabs) return;
    for (const b of tabs.querySelectorAll('.settings-tab')) {
      const l = b.querySelector('.stab-label');
      if (l && l.textContent) b.title = l.textContent.trim();
    }
  };
  labelTabs();
  if (tabs && globalThis.MutationObserver) new MutationObserver(labelTabs).observe(tabs, { childList: true, subtree: true, characterData: true });

  if (maxBtn) maxBtn.addEventListener('click', toggleMax);
  globalThis.addEventListener('resize', apply);
  if (mq && mq.addEventListener) mq.addEventListener('change', apply);
  document.addEventListener('i18n:change', paintMaxBtn);
  apply();
  return { apply, reset };
}
