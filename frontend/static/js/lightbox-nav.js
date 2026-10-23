// Lightbox navigation + auto-hiding chrome — the pure half of openLightbox
// (main.js). No DOM, no timers of its own: everything a test needs to pin
// (step order, the end stops, the over-the-picture rule, the touch toggle, the swipe
// verdict) is a plain function or a small state machine driven by the caller.
//
// ORDER. A gallery is listed oldest -> newest, the way a thread reads and the
// way Image Studio lists results (newest last). "Next" (-> key, right arrow
// button, swipe left) goes to the NEWER picture, "previous" (<- key, left
// button, swipe right) to the OLDER one, and both STOP at the ends: there is no
// wrap-around, so the last picture of a chat never silently becomes the first.

/** Keep an index inside a list; 0 for an empty one. */
export function clampIndex(i, length) {
  const n = Math.trunc(Number(i));
  if (!Number.isFinite(n) || length <= 0) return 0;
  return Math.min(length - 1, Math.max(0, n));
}

/**
 * A cursor over a list of items. `step(+1)` is newer, `step(-1)` older; at an
 * end it returns false and does not move. `label()` is the "n / total" counter.
 */
export function createGallery(items, index = 0) {
  const list = Array.isArray(items) ? items.slice() : [];
  let at = clampIndex(index, list.length);
  return {
    get items() { return list; },
    get index() { return at; },
    get size() { return list.length; },
    current() { return list[at]; },
    canStep(dir) { return dir > 0 ? at < list.length - 1 : at > 0; },
    step(dir) {
      const next = at + (dir > 0 ? 1 : -1);
      if (next < 0 || next >= list.length) return false;
      at = next;
      return true;
    },
    go(i) { at = clampIndex(i, list.length); return at; },
    label() { return list.length > 1 ? `${at + 1} / ${list.length}` : ''; },
  };
}

/** The key -> direction map: ArrowRight = newer (+1), ArrowLeft = older (-1). */
export function keyDirection(key) {
  if (key === 'ArrowRight') return 1;
  if (key === 'ArrowLeft') return -1;
  return 0;
}

/**
 * Is the pointer over the picture? The controls show only then (or while it is on one of
 * them), and hide a moment after it leaves. `rect` is the media element's bounding rect.
 */
export function overMedia(clientX, clientY, rect) {
  if (!rect || !(rect.width > 0) || !(rect.height > 0)) return false;
  return clientX >= rect.left && clientX <= rect.right && clientY >= rect.top && clientY <= rect.bottom;
}

/**
 * A finger drag -> a step direction, or 0. Only an unzoomed picture swipes (a
 * zoomed one pans), the move must be mostly horizontal and long enough to be
 * deliberate. Swiping LEFT (dx < 0) reveals the newer picture, as a page turn.
 */
export function swipeDirection(dx, dy, { zoomed = false, min = 50 } = {}) {
  if (zoomed) return 0;
  if (Math.abs(dx) < min || Math.abs(dx) < Math.abs(dy) * 1.5) return 0;
  return dx < 0 ? 1 : -1;
}

/**
 * The visibility of the lightbox chrome (close, download, caption, counter,
 * arrows). One boolean out, `onChange(visible)`, and three ways in:
 *
 *   pointer(inZone, onControl)  a mouse/pen moved: stay up while it is over the
 *                               picture or on a control, else fade after
 *                               `delay` ms
 *   tap()                       a touch tap on the picture: toggle, and STAY
 *                               (no hover on a phone, so no timer either)
 *   focusIn()/focusOut()        keyboard focus on a control keeps it up
 *
 * `flash()` shows it briefly (a keyboard step), then lets it fade like a
 * pointer that left. Nothing shows at open until the mouse is over the picture. Timers are injectable for the tests.
 */
export function createChrome({ onChange, delay = 1500, setTimer = setTimeout, clearTimer = clearTimeout } = {}) {
  let visible = false;
  let sticky = false;
  let focused = false;
  let timer = null;

  const emit = (v) => { if (v !== visible) { visible = v; if (onChange) onChange(v); } };
  const cancel = () => { if (timer !== null) { clearTimer(timer); timer = null; } };
  const fadeLater = () => {
    cancel();
    if (!visible || sticky || focused) return;
    timer = setTimer(() => { timer = null; if (!sticky && !focused) emit(false); }, delay);
  };

  return {
    get visible() { return visible; },
    get sticky() { return sticky; },
    flash() { sticky = false; emit(true); fadeLater(); },
    pointer(inZone, onControl = false) {
      if (inZone || onControl) { sticky = false; cancel(); emit(true); } else fadeLater();
    },
    leave() { fadeLater(); },
    tap() {
      cancel();
      if (visible) { sticky = false; emit(false); } else { sticky = true; emit(true); }
    },
    focusIn() { focused = true; cancel(); emit(true); },
    focusOut() { focused = false; fadeLater(); },
    dispose() { cancel(); },
  };
}

/** Is `url` something the shell may be asked to show: a same-origin path. */
export function safeGalleryUrl(url) {
  return typeof url === 'string' && url.startsWith('/') && !url.startsWith('//')
    && !/[\u0000-\u001f\\]/.test(url) && url.length <= 2048;
}

/**
 * An app page's gallery request, validated (it crosses a postMessage): at most
 * 200 same-origin items, a clamped index, short text. Returns null when there is
 * nothing showable.
 */
export function normalizeGalleryRequest(msg) {
  const raw = msg && Array.isArray(msg.items) ? msg.items.slice(0, 200) : [];
  const items = [];
  const want = Math.trunc(Number(msg && msg.index)) || 0;
  let index = want;
  raw.forEach((it, i) => {
    const src = it && it.src;
    if (!safeGalleryUrl(src)) { if (i < want) index -= 1; return; }
    items.push({
      src,
      downloadUrl: safeGalleryUrl(it.downloadUrl) ? it.downloadUrl : src,
      downloadName: typeof it.name === 'string' ? it.name.slice(0, 120) : '',
      caption: typeof it.caption === 'string' ? it.caption.slice(0, 1500) : '',
    });
  });
  if (!items.length) return null;
  return { items, index: clampIndex(index, items.length) };
}
