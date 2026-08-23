// Small DOM helpers.
//
// The date/time/size formatters that used to live here are gone: they hard-coded
// 'en-US' and English words ("Yesterday", "5m", "6.2 MB"), which no amount of
// locale data could reach. Their replacements are in js/i18n.js, where Intl owns
// the formatting and the unit labels are translatable keys. What is left here is
// deliberately language-free — DOM construction and lazy asset loading.

export function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null) continue;
    // No `html:` option on purpose: this helper is the one place the whole app
    // builds DOM, and an innerHTML escape hatch here is how untrusted text
    // reaches the parser. Rendered markup goes through markdown.js's sanitizer.
    if (k === 'class') node.className = v;
    else if (k === 'text') node.textContent = v;
    else if (k.startsWith('on') && typeof v === 'function') node.addEventListener(k.slice(2), v);
    else if (k === 'dataset') Object.assign(node.dataset, v);
    else node.setAttribute(k, v);
  }
  for (const c of [].concat(children)) {
    if (c == null) continue;
    node.append(c.nodeType ? c : document.createTextNode(String(c)));
  }
  return node;
}

// --- Lazy asset loading ----------------------------------------------------
// The heavy vendor bundles (highlight.js 127KB, xterm + addons 317KB) used to
// be plain <script> tags in the document, so every cold start — including a
// phone that only ever reads a message — paid for them before first paint.
// They are now fetched the first time something actually needs them. Both
// helpers memoise per URL, so concurrent callers share one network request.
const _assetCache = new Map();

export function loadScript(src) {
  if (_assetCache.has(src)) return _assetCache.get(src);
  const p = new Promise((resolve, reject) => {
    const s = document.createElement('script');
    s.src = src;
    s.async = false;          // preserve order between dependent bundles
    s.onload = () => resolve();
    s.onerror = () => reject(new Error(`Failed to load ${src}`));
    document.head.append(s);
  });
  _assetCache.set(src, p);
  return p;
}

export function loadStyle(href) {
  if (_assetCache.has(href)) return _assetCache.get(href);
  const p = new Promise((resolve) => {
    const l = document.createElement('link');
    l.rel = 'stylesheet';
    l.href = href;
    // A missing stylesheet is cosmetic — never block the feature on it.
    l.onload = l.onerror = () => resolve();
    document.head.append(l);
  });
  _assetCache.set(href, p);
  return p;
}

export function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}
