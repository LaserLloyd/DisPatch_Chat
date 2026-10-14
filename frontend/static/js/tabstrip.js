// tabstrip.js — the Settings tab strip's overflow affordances.
//
// Ten-plus tabs (more once an add-on claims one) never fit a phone and do not
// fit the 800px desktop dialog either, and the strip has no scrollbar on
// purpose. Before 2.3.5 the only hint was a 22px edge fade: on a phone you
// could swipe, but with a mouse there was no way at all to reach Security,
// and nothing said it existed. This module adds what was missing:
//
//   * ‹ / › buttons at each edge, shown only while there are tabs that way
//     (measured, never assumed — so they are right under RTL too);
//   * a vertical mouse wheel scrolls the strip sideways (a trackpad's own
//     horizontal swipe already does);
//   * the fade classes the CSS already keys on (fade-start / fade-end).
//
// Keyboard reach stays where it was: the tablist's roving ←/→/Home/End in
// main.js, and setSettingsTab() scrolls the chosen tab into view (the strip's
// scroll-padding keeps it clear of the arrow buttons).
//
// No DOM is created here: index.html carries the two buttons. Pure enough to
// test under jsdom with scrollWidth/clientWidth stubbed.

/** Where the strip is: { slack, pos } with pos measured from the reading start. */
export function stripState(list) {
  const slack = Math.max(0, list.scrollWidth - list.clientWidth);
  // scrollLeft is 0..-slack in an RTL container in every engine we target.
  const pos = Math.min(slack, Math.abs(list.scrollLeft || 0));
  return { slack, pos, atStart: slack <= 1 || pos <= 2, atEnd: slack <= 1 || pos >= slack - 2 };
}

const isRtl = (list) => {
  const doc = list.ownerDocument;
  const win = doc && doc.defaultView;
  try {
    if (win && win.getComputedStyle) {
      const d = win.getComputedStyle(list).direction;
      if (d) return d === 'rtl';
    }
  } catch { /* fall through */ }
  return !!(doc && doc.documentElement.getAttribute('dir') === 'rtl');
};

/** Scroll the strip by `dx` in READING direction (positive = towards the end). */
export function scrollStrip(list, dx, { smooth = true } = {}) {
  const left = isRtl(list) ? -dx : dx;
  if (typeof list.scrollBy === 'function') {
    try { list.scrollBy({ left, behavior: smooth ? 'smooth' : 'auto' }); return; } catch { /* old engine */ }
  }
  list.scrollLeft = (list.scrollLeft || 0) + left;
}

/**
 * Wire the strip. `prev` / `next` are the edge buttons (either may be null).
 * Returns { update } — call it whenever tabs appear, disappear or relabel; the
 * strip also updates itself on scroll, resize and tab-list mutations.
 */
export function wireTabStrip({ list, prev = null, next = null, win = (list.ownerDocument || {}).defaultView }) {
  const bar = list.parentElement;

  function update() {
    const { slack, atStart, atEnd } = stripState(list);
    const over = slack > 1;
    list.classList.toggle('fade-start', over && !atStart);
    list.classList.toggle('fade-end', over && !atEnd);
    if (bar) bar.classList.toggle('overflowing', over);
    // `hidden` (not disabled): an arrow with nothing beyond it is not a control.
    if (prev) prev.hidden = !over || atStart;
    if (next) next.hidden = !over || atEnd;
  }

  // One "page" = most of the visible strip, so the tab at the far edge stays
  // on screen as the anchor of where you were.
  const page = () => Math.max(80, Math.round(list.clientWidth * 0.7));
  if (prev) prev.addEventListener('click', () => scrollStrip(list, -page()));
  if (next) next.addEventListener('click', () => scrollStrip(list, page()));

  // A plain mouse wheel only ever sends deltaY. Turn it into sideways motion
  // while the strip can move that way; at the end let the event go (nothing
  // under the strip scrolls vertically, but never trap it for no reason).
  list.addEventListener('wheel', (e) => {
    if (e.ctrlKey) return;                                     // pinch-zoom
    if (Math.abs(e.deltaX) >= Math.abs(e.deltaY)) return;      // already sideways
    const { slack, atStart, atEnd } = stripState(list);
    if (slack <= 1) return;
    const unit = e.deltaMode === 1 ? 32 : (e.deltaMode === 2 ? list.clientWidth : 1);
    const dy = e.deltaY * unit;
    if ((dy < 0 && atStart) || (dy > 0 && atEnd)) return;
    e.preventDefault();
    scrollStrip(list, dy, { smooth: false });
  }, { passive: false });

  list.addEventListener('scroll', update, { passive: true });
  if (win) win.addEventListener('resize', update);
  if (win && typeof win.ResizeObserver === 'function') {
    const ro = new win.ResizeObserver(() => update());
    ro.observe(list);
  }
  if (win && typeof win.MutationObserver === 'function') {
    // Tabs hidden/shown with the session, add-in tabs claimed/disposed, labels
    // relabelled by a locale switch — all change the strip's width.
    new win.MutationObserver(() => update()).observe(list, {
      childList: true, subtree: true, characterData: true, attributes: true, attributeFilter: ['class'],
    });
  }
  update();
  return { update };
}
