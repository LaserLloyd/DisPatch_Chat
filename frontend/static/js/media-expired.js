// "image expired": what an <img> becomes when its picture was cleaned up by the image-retention
// setting (BUILD-PLAN 1.13.6). The server answers 410 Gone with `X-Image-Expired: 1` for a name it
// removed, instead of a 404; the message itself is never edited, so every place that shows the
// picture (chat, Image Studio, the reaction history) would show a broken image. One document-wide
// listener swaps it for a small labelled placeholder.
//
// Image Studio runs in its own frame, so its page installs this too (one import + one call).
// Nothing runs at import time; the caller installs it once.

const probed = new Map();       // url -> Promise<boolean>

/** Same-origin URLs only: a third-party host has nothing to say about our retention. */
export function isLocalUrl(src, base) {
  if (!src || /^(data|blob):/i.test(src)) return false;
  try {
    const u = new URL(src, base);
    return u.origin === new URL(base).origin;
  } catch { return false; }
}

/** Was this picture removed on purpose? Asks the server once per URL (a normal broken image is a
 *  network or decode problem and stays as it is). */
export async function isExpired(src, fetchFn = (typeof fetch === 'function' ? fetch : null)) {
  if (!fetchFn) return false;
  if (probed.has(src)) return probed.get(src);
  const p = (async () => {
    try {
      const r = await fetchFn(src, { credentials: 'same-origin', cache: 'no-store' });
      const flagged = r.status === 410 || (r.headers && typeof r.headers.get === 'function' && r.headers.get('X-Image-Expired') === '1');
      try { if (r.body && typeof r.body.cancel === 'function') r.body.cancel(); } catch { /* best effort */ }
      return !!flagged;
    } catch { return false; }
  })();
  probed.set(src, p);
  return p;
}

/** The placeholder node: an icon and the localised words, readable as one image. */
export function expiredNode({ t, el, icon, doc }) {
  const text = t('media.expired');
  const parts = [];
  if (icon) { const i = icon('image-off', 18); if (i) parts.push(i); }
  parts.push(el('span', { class: 'media-expired-text', text }));
  return el('span', { class: 'media-expired', role: 'img', 'aria-label': text }, parts);
}

/** Listen for failing images on `doc`. Returns a function that removes the listener. */
export function installExpiredImages({ t, el, icon = null, doc = document, fetchFn = undefined } = {}) {
  const base = (doc.defaultView && doc.defaultView.location && doc.defaultView.location.href) || (doc.location && doc.location.href) || 'http://localhost/';
  const onError = async (ev) => {
    const img = ev.target;
    if (!img || img.tagName !== 'IMG' || img.dataset.expiredChecked) return;
    img.dataset.expiredChecked = '1';
    const src = img.currentSrc || img.getAttribute('src') || '';
    if (!isLocalUrl(src, base)) return;
    if (!(await isExpired(src, fetchFn))) return;
    if (!img.isConnected) return;
    const node = expiredNode({ t, el, icon, doc });
    node.dataset.expiredSrc = src;
    // keep the picture's own sizing class so a grid cell keeps its shape
    if (img.className) node.className += ` ${String(img.className).split(/\s+/).filter(Boolean).map((c) => `${c}-expired`).join(' ')}`;
    img.replaceWith(node);
  };
  // error does not bubble; the capture phase sees it for every image on the page
  doc.addEventListener('error', onError, true);
  return () => doc.removeEventListener('error', onError, true);
}

/** Test hook: forget the probe cache. */
export function _resetProbeCache() { probed.clear(); }
