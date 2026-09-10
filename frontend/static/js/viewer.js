// DisPatch Chat — the local viewer overlay.
//
// An unlocked operator taps a local path (a message link, a `[[view:]]` card, a
// rail button, the command palette) and the thing opens INSIDE the app: a
// full-screen lightbox with an ✕, a back arrow, and a body that renders by
// kind. The server serves the bytes (see backend/app/localview.py), so this
// works from a phone on the tailnet exactly as it does on the host.
//
// Three contracts this module must not break:
//
//  * It builds `div.lightbox` with `_close` published on the node, exactly like
//    openLightbox in main.js. The global Escape handler finds `.lightbox` and
//    clicks its `.lightbox-close`; closeAllOverlays() calls `_close()` on every
//    `.lightbox` when the app drops to Safe Mode. Rename either and locking the
//    app would leave someone's home directory on screen.
//  * The iframe's `src` is REMOVED before the node is (the harness precedent in
//    main.js): a detached-but-live frame keeps its page running.
//  * Safe Mode never gets here. `isDecoy()` is the client half; the server 403s
//    every viewer route for a decoy session. Both are required.
//
// The pure parts (kind → pane decision, breadcrumbs, the history stack, the
// icon-name table, error classification) are exported separately from the DOM
// so they can be unit-tested under plain node — see frontend/tests/viewer.test.js.

import { el, railIcon, RAIL_ICONS } from './util.js?v=13';
import { t, fileSize } from './i18n.js?v=3';
// markdown.js is versioned in lockstep across every importer (assets.test.js
// enforces it) — this line moves with the rest when the integration pass bumps it.
import { renderMarkdown, enhanceContent } from './markdown.js?v=26';
import { nimEnabled } from './nim.js?v=5';

// ---------------------------------------------------------------------------
// Pure helpers (no DOM, no network) — the testable core
// ---------------------------------------------------------------------------

/** Kinds the backend's `viewer` classifier can emit. */
export const VIEWER_KINDS = [
  'html', 'image', 'svg', 'video', 'audio', 'pdf',
  'markdown', 'text', 'listing', 'download',
];

/** Icon name (a RAIL_ICONS key) for a stat/ls `viewer` kind. */
export function iconNameForKind(kind) {
  return (kind === 'listing' || kind === 'dir' || kind === 'folder')
    ? 'viewer-folder' : 'viewer-file';
}

/**
 * The render decision table: what pane a stat payload gets.
 *
 * Returns one of: iframe | image | video | audio | markdown | text | listing |
 * download | nim | too_large. Deliberately a pure function of the payload plus
 * the two device facts (No-Image Mode) so the table can be asserted directly.
 */
export function paneForStat(stat, { nim = false } = {}) {
  if (!stat) return 'download';
  // A directory is a listing unless the server resolved it to its index page.
  if (stat.kind === 'dir' && stat.viewer !== 'html') return 'listing';
  switch (stat.viewer) {
    case 'html':
    case 'pdf':
      // Explicitly asked for: NIM does not suppress a page the operator opened.
      return 'iframe';
    case 'image':
    case 'svg':
      return nim ? 'nim' : 'image';
    case 'video':
      return nim ? 'nim' : 'video';
    case 'audio':
      return 'audio';                       // no picture to hide
    case 'markdown':
      return stat.text_ok === false ? 'too_large' : 'markdown';
    case 'text':
      return stat.text_ok === false ? 'too_large' : 'text';
    case 'listing':
      return 'listing';
    default:
      return 'download';
  }
}

/** `openViewer({path})` vs `openViewer({url})` — which mode was asked for. */
export function viewMode({ path, url } = {}) {
  if (path) return 'path';
  if (url) return 'url';
  return null;
}

/**
 * Classify a stat/ls failure into a pane we can render IN the overlay.
 * The feature-off case arrives as a 404 whose detail says so; everything else
 * 404 is a genuine miss, and 403 never says why (by design, see the design doc).
 */
export function errorKind(status, detail) {
  const d = String(detail || '');
  if (/local viewer is off/i.test(d)) return 'off';
  if (status === 403) return 'denied';
  if (status === 404) return 'not_found';
  return 'denied';
}

/** Join a directory path and an entry name (no normalisation of `..`: the
 *  server is the authority on what resolves where). */
export function joinPath(dir, name) {
  const base = String(dir || '').replace(/\/+$/, '');
  return `${base}/${name}`;
}

/**
 * Breadcrumb segments for a directory path.
 *
 * A crumb is only navigable when we KNOW it is at or below a served root: the
 * `ls` payload gives us `parent` (null at a root), and any root we have already
 * landed on is remembered. Everything above that boundary renders as plain
 * text — the viewer must never offer a walk up out of the served tree.
 */
export function computeCrumbs(path, { parent = null, root = null } = {}) {
  const p = String(path || '').replace(/\/+$/, '') || '/';
  const home = p === '~' || p.startsWith('~/');
  const rest = home ? p.slice(1).replace(/^\//, '') : p.replace(/^\//, '');
  const segs = rest ? rest.split('/') : [];
  const out = [];
  let acc = home ? '~' : '';
  out.push({ name: home ? '~' : '/', path: home ? '~' : '/', nav: false });
  for (const s of segs) {
    acc = `${acc}/${s}`;
    out.push({ name: s, path: acc, nav: false });
  }
  const boundary = (root && p.startsWith(root)) ? root : parent;
  for (const c of out) {
    c.nav = Boolean(boundary)
      && c.path !== p
      && (c.path === boundary || c.path.startsWith(`${boundary}/`));
  }
  return out;
}

/**
 * The in-viewer history stack (listing → file → ‹ back). Separate from the
 * browser's: one browser entry is pushed for the overlay as a whole, so a
 * phone's Back closes it, while ‹ walks this.
 */
export function makeStack() {
  const items = [];
  return {
    push(entry) { items.push(entry); return entry; },
    /** Drop the current entry and return the one beneath it (null at depth 1). */
    back() {
      if (items.length < 2) return null;
      items.pop();
      return items[items.length - 1];
    },
    replace(entry) {
      if (items.length) items[items.length - 1] = entry; else items.push(entry);
      return entry;
    },
    current() { return items.length ? items[items.length - 1] : null; },
    depth() { return items.length; },
    canGoBack() { return items.length > 1; },
    all() { return items.slice(); },
  };
}

// Roots we have confirmed by landing on them (an `ls` with `parent: null`).
// Used only to widen the navigable part of a breadcrumb; never trusted for
// access decisions — the server decides those.
const knownRoots = new Set();
export function noteRoot(path) { if (path) knownRoots.add(String(path).replace(/\/+$/, '')); }
export function rootFor(path) {
  const p = String(path || '').replace(/\/+$/, '');
  let best = null;
  for (const r of knownRoots) {
    if (p === r || p.startsWith(`${r}/`)) {
      if (!best || r.length > best.length) best = r;
    }
  }
  return best;
}
export function _resetRoots() { knownRoots.clear(); }

// ---------------------------------------------------------------------------
// Module wiring
// ---------------------------------------------------------------------------

async function fetchJson(url) {
  const r = await fetch(url, { credentials: 'same-origin' });
  let body = null;
  try { body = await r.json(); } catch { /* a non-JSON error body is still an error */ }
  if (!r.ok) {
    const err = new Error(`${r.status}`);
    err.status = r.status;
    err.detail = (body && (body.detail || body.error)) || '';
    throw err;
  }
  return body;
}

const DEFAULT_API = {
  stat: (path) => fetchJson(`/api/local/stat?path=${encodeURIComponent(path)}`),
  ls: (path) => fetchJson(`/api/local/ls?path=${encodeURIComponent(path)}`),
};

const cfg = {
  onToast: () => {},
  isDecoy: () => false,
  api: DEFAULT_API,
};

/**
 * Wire the module to the app.
 *   onToast(message, isError)  — the app's toast (never imported from main.js)
 *   isDecoy()                  — true in Safe Mode; the viewer refuses to open
 *   api                        — { stat(path), ls(path) }; defaults to fetch()
 */
export function installViewerHandlers({ onToast, isDecoy, api } = {}) {
  if (typeof onToast === 'function') cfg.onToast = onToast;
  if (typeof isDecoy === 'function') cfg.isDecoy = isDecoy;
  if (api && typeof api.stat === 'function' && typeof api.ls === 'function') cfg.api = api;
  return cfg;
}

/** An <svg> for a stat/ls `viewer` kind (folder vs file). */
export function viewerIcon(kind) {
  return railIcon(RAIL_ICONS[iconNameForKind(kind)] || RAIL_ICONS['viewer-file']);
}

// ---------------------------------------------------------------------------
// The overlay
// ---------------------------------------------------------------------------

let current = null;          // { node, close, ... } while an overlay is open

/** Close the open viewer, if any. A no-op when nothing is open. */
export function closeViewer() {
  if (current && typeof current.close === 'function') current.close();
  return null;
}

/** True while an overlay is on screen (used by tests and the integrator). */
export function viewerOpen() { return Boolean(current); }

const FRAME_SANDBOX = 'allow-scripts allow-forms allow-popups allow-modals allow-downloads';
const FRAME_HINT_MS = 8000;

function iconBtn(name, labelKey, onClick, extraClass = '') {
  const b = el('button', {
    class: `viewer-btn ${extraClass}`.trim(),
    type: 'button',
    'aria-label': t(labelKey),
    title: t(labelKey),
    onclick: onClick,
  });
  b.append(railIcon(RAIL_ICONS[name]));
  return b;
}

/**
 * Open the viewer.
 *   openViewer({ path })  — stat the path, then render by kind
 *   openViewer({ url })   — frame an http(s) URL directly (no stat)
 * Returns the overlay element, or null when Safe Mode refuses it.
 */
export function openViewer({ path, url, title, returnFocus } = {}) {
  const mode = viewMode({ path, url });
  if (!mode) return null;
  if (cfg.isDecoy()) return null;
  // One viewer at a time: a second open replaces the first rather than
  // stacking two modal dialogs (and two `app.inert = true` owners).
  //
  // The outgoing instance must NOT pop its own history entry on the way out:
  // history.back() is asynchronous, so the popstate would land AFTER this
  // instance has pushed its own entry and would close the NEW overlay on
  // arrival (open a file link from inside a rendered document and the pane
  // vanished the moment it appeared). It hands the entry over instead.
  let adoptedHistory = false;
  if (current) {
    adoptedHistory = current.releaseHistory ? current.releaseHistory() : false;
    current.close();
  }

  const doc = document;
  const stack = makeStack();
  const cleanups = [];
  let closed = false;
  let pushedHistory = false;      // did WE add the browser history entry?
  let poppedBySystem = false;     // did the browser already consume it?

  const backBtn = iconBtn('viewer-back', 'viewer.back', () => goBack(), 'viewer-back');
  const titleBtn = el('button', {
    class: 'viewer-title', type: 'button', title: t('viewer.copy_path'),
    'aria-label': t('viewer.copy_path'),
  });
  const titleIcon = el('span', { class: 'viewer-title-icon' });
  const titleText = el('span', { class: 'viewer-title-text', text: title || path || url || '' });
  titleBtn.append(titleIcon, titleText);
  titleBtn.addEventListener('click', () => copyCurrentPath());

  const openBtn = iconBtn('viewer-open', 'viewer.open_tab', () => openInTab());
  const dlBtn = iconBtn('viewer-download', 'viewer.download', () => downloadCurrent());
  const reloadBtn = iconBtn('viewer-reload', 'viewer.reload', () => reload());
  const closeBtn = el('button', {
    class: 'lightbox-close viewer-close', type: 'button', text: '✕',
    'aria-label': t('viewer.close'),
  });

  const header = el('div', { class: 'viewer-header' }, [
    backBtn, titleBtn, el('div', { class: 'viewer-actions' }, [openBtn, dlBtn, reloadBtn, closeBtn]),
  ]);
  const body = el('div', { class: 'viewer-body' });
  const node = el('div', {
    class: 'lightbox viewer',
    role: 'dialog',
    'aria-modal': 'true',
    'aria-label': t('viewer.title'),
    tabindex: '-1',
  }, [header, body]);

  const focusBack = returnFocus || doc.activeElement;

  const close = () => {
    if (closed) return;
    closed = true;
    unloadFrames();
    cleanups.forEach((f) => { try { f(); } catch { /* teardown is best effort */ } });
    node.remove();
    current = null;
    // Undo our own history entry — unless we never pushed one, or the browser
    // already consumed it (popstate). Popping one we do not own would take the
    // whole app back a page.
    if (pushedHistory && !poppedBySystem && typeof history !== 'undefined' && history.back) {
      try { history.back(); } catch { /* history is best effort */ }
    }
    if (focusBack && focusBack.isConnected && typeof focusBack.focus === 'function') {
      try { focusBack.focus(); } catch { /* focus is best effort */ }
    }
  };
  // Published on the node: closeAllOverlays() only has the DOM.
  node._close = close;
  closeBtn.addEventListener('click', close);
  // NOTE: the backdrop is deliberately NOT a close target — the content fills
  // the screen, so "click outside" would mean "click the document you are
  // reading".

  function unloadFrames() {
    body.querySelectorAll('iframe').forEach((f) => f.removeAttribute('src'));
    body.querySelectorAll('video, audio').forEach((m) => { try { m.pause(); } catch {} });
  }

  // --- navigation ---------------------------------------------------------

  // Framed content prefers the server's ticket URL: the sandboxed frame is
  // an opaque origin and sends no session cookie, so a page's own stylesheets
  // and scripts would 403 behind a PIN through the plain /local/file route.
  // The ticket also makes "Open in a new tab" land on a styled page.
  function entryUrl(e) {
    if (!e) return null;
    if (e.mode === 'url') return e.url;
    return (e.stat && (e.stat.frame_url || e.stat.url)) || null;
  }

  function copyCurrentPath() {
    const e = stack.current();
    const p = (e && (e.mode === 'url' ? e.url : (e.stat ? e.stat.path : e.path))) || path || url;
    if (!p) return;
    // Toast only on a REAL copy — the same rule as the file-link handler in
    // markdown.js. A "Copied …" for a clipboard that refused (no API, no
    // secure context, permission denied) is a claim the operator acts on.
    const clip = typeof navigator !== 'undefined' ? navigator.clipboard : null;
    if (!clip || typeof clip.writeText !== 'function') return;
    try {
      clip.writeText(p).then(
        () => cfg.onToast(t('viewer.copied', { path: p })),
        () => { /* clipboard refused */ },
      );
    } catch { /* clipboard unavailable */ }
  }

  function openInTab() {
    const u = entryUrl(stack.current());
    if (!u) return;
    try { window.open(u, '_blank', 'noopener,noreferrer'); } catch { /* popup blocked */ }
  }

  function downloadCurrent() {
    const e = stack.current();
    const u = entryUrl(e);
    if (!u) return;
    const name = (e && e.stat && e.stat.name) || '';
    const a = el('a', { href: u, download: name || '', style: 'display:none' });
    doc.body.append(a);
    a.click();
    a.remove();
  }

  function reload() {
    const e = stack.current();
    if (!e) return;
    if (e.mode === 'url') { paint(e); return; }
    navigate(e.path, { replace: true });
  }

  function goBack() {
    const prev = stack.back();
    if (prev) paint(prev);
  }

  function setChrome(entry) {
    backBtn.classList.toggle('hidden', !stack.canGoBack());
    const kind = entry && (entry.mode === 'url' ? 'html'
      : (entry.stat ? entry.stat.viewer : 'download'));
    titleIcon.replaceChildren(viewerIcon(kind));
    const label = entry && (entry.title
      || (entry.mode === 'url' ? entry.url : (entry.stat ? entry.stat.path : entry.path)));
    titleText.textContent = label || '';
    const hasUrl = Boolean(entryUrl(entry));
    openBtn.classList.toggle('hidden', !hasUrl);
    dlBtn.classList.toggle('hidden', !hasUrl || entry.mode === 'url');
  }

  async function navigate(target, { replace = false } = {}) {
    body.replaceChildren(el('div', { class: 'viewer-note', text: t('viewer.loading') }));
    let stat = null;
    try {
      stat = await cfg.api.stat(target);
    } catch (err) {
      const entry = { mode: 'path', path: target, error: errorKind(err.status, err.detail) };
      if (replace) stack.replace(entry); else stack.push(entry);
      paint(entry);
      return entry;
    }
    if (closed) return null;
    const entry = { mode: 'path', path: target, stat };
    if (replace) stack.replace(entry); else stack.push(entry);
    await paint(entry);
    return entry;
  }

  // --- painting -----------------------------------------------------------

  async function paint(entry) {
    unloadFrames();
    setChrome(entry);
    if (entry.error) { body.replaceChildren(errorPane(entry.error)); return; }
    if (entry.mode === 'url') { body.replaceChildren(framePane(entry.url, true)); return; }
    const stat = entry.stat;
    const pane = paneForStat(stat, { nim: nimEnabled() });
    switch (pane) {
      case 'iframe':
        body.replaceChildren(framePane(stat.frame_url || stat.url, false));
        break;
      case 'image':
        body.replaceChildren(el('div', { class: 'viewer-media' }, [
          el('img', { src: stat.url, alt: stat.name || '' }),
        ]));
        break;
      case 'video':
        body.replaceChildren(el('div', { class: 'viewer-media' }, [
          el('video', { src: stat.url, controls: '', playsinline: '' }),
        ]));
        break;
      case 'audio':
        body.replaceChildren(el('div', { class: 'viewer-media viewer-audio' }, [
          el('audio', { src: stat.url, controls: '' }),
        ]));
        break;
      case 'nim':
        body.replaceChildren(nimPane(stat));
        break;
      case 'too_large':
        body.replaceChildren(downloadPane(stat, t('viewer.too_large', { size: fileSize(stat.size) })));
        break;
      case 'markdown':
      case 'text':
        body.replaceChildren(el('div', { class: 'viewer-note', text: t('viewer.loading') }));
        await textPane(stat, pane === 'markdown');
        break;
      case 'listing':
        await listingPane(entry);
        break;
      default:
        body.replaceChildren(downloadPane(stat, null));
    }
  }

  function framePane(src, isUrlMode) {
    // `sandbox` and `referrerpolicy` are set BEFORE `src`: attribute order is
    // insertion order in el(), and a sandbox applied after the source is a
    // sandbox that might not have been there when the load began.
    const frame = el('iframe', {
      class: 'viewer-frame',
      sandbox: FRAME_SANDBOX,
      referrerpolicy: 'no-referrer',
      title: t('viewer.title'),
      src,
    });
    const wrap = el('div', { class: 'viewer-frame-wrap' }, [frame]);
    if (isUrlMode) {
      // A site that refuses to be framed does not reliably fire `load` or
      // `error` — the frame just stays blank. So: a timer and a visible bar,
      // never a promise of detection.
      const hint = el('div', { class: 'viewer-frame-hint hidden' }, [
        el('span', { text: t('viewer.frame_hint') }),
        el('button', { class: 'viewer-hint-btn', type: 'button', text: t('viewer.open_tab'), onclick: openInTab }),
      ]);
      wrap.append(hint);
      const timer = setTimeout(() => hint.classList.remove('hidden'), FRAME_HINT_MS);
      cleanups.push(() => clearTimeout(timer));
    }
    return wrap;
  }

  function nimPane(stat) {
    const card = el('div', { class: 'viewer-card' }, [
      el('div', { class: 'viewer-card-name', text: stat.name || stat.path || '' }),
      el('div', { class: 'viewer-card-meta', text: t('viewer.nim_hidden') }),
      el('div', { class: 'viewer-card-meta', text: fileSize(stat.size) }),
    ]);
    const btn = el('button', { class: 'viewer-card-btn', type: 'button', text: t('viewer.open_tab'), onclick: openInTab });
    card.append(btn);
    return card;
  }

  function downloadPane(stat, note) {
    const card = el('div', { class: 'viewer-card' }, [
      el('div', { class: 'viewer-card-name', text: stat.name || stat.path || '' }),
      el('div', { class: 'viewer-card-meta', text: fileSize(stat.size) }),
    ]);
    if (note) card.append(el('div', { class: 'viewer-card-note', text: note }));
    const a = el('a', { class: 'viewer-card-btn', href: stat.url, download: stat.name || '', text: t('viewer.download') });
    card.append(a);
    return card;
  }

  async function textPane(stat, asMarkdown) {
    let text = '';
    try {
      const r = await fetch(stat.url, { credentials: 'same-origin' });
      if (!r.ok) throw Object.assign(new Error(String(r.status)), { status: r.status });
      text = await r.text();
    } catch (err) {
      body.replaceChildren(errorPane(errorKind(err.status, err.detail)));
      return;
    }
    if (closed) return;
    if (asMarkdown) {
      // `bubble` on purpose: every markdown rule in app.css (tables, code
      // blocks, callouts, the sort chrome) hangs off `.bubble`, and a rendered
      // document that does not carry it would silently lose all of it. The
      // bubble's own chrome is neutralised in the viewer's CSS section.
      const docEl = el('div', { class: 'viewer-doc bubble', dir: 'auto' });
      try {
        // `noLocal: false` — a path inside a viewed document is itself openable.
        // Passed defensively: an older markdown.js ignores the option.
        docEl.innerHTML = renderMarkdown(text, { noMedia: nimEnabled(), noLocal: false });
        enhanceContent(docEl);
      } catch {
        docEl.replaceChildren(el('pre', { class: 'viewer-text', text }));
      }
      body.replaceChildren(docEl);
      return;
    }
    const pre = el('pre', { class: 'viewer-text', text });
    const wrapBtn = el('button', {
      class: 'viewer-wrap-btn', type: 'button', text: t('viewer.wrap'),
      'aria-pressed': 'false',
      onclick: () => {
        const on = pre.classList.toggle('wrap');
        wrapBtn.setAttribute('aria-pressed', on ? 'true' : 'false');
      },
    });
    body.replaceChildren(el('div', { class: 'viewer-textwrap' }, [wrapBtn, pre]));
  }

  async function listingPane(entry) {
    let data = null;
    try {
      data = await cfg.api.ls(entry.path);
    } catch (err) {
      body.replaceChildren(errorPane(errorKind(err.status, err.detail)));
      return;
    }
    if (closed) return;
    if (data.parent == null) noteRoot(data.path || entry.path);
    const here = data.path || entry.path;
    const list = el('div', { class: 'viewer-list' });

    const crumbBar = el('nav', { class: 'viewer-crumbs', 'aria-label': t('viewer.folder') });
    for (const c of computeCrumbs(here, { parent: data.parent, root: rootFor(here) })) {
      crumbBar.append(c.nav
        ? el('button', { class: 'viewer-crumb', type: 'button', text: c.name, onclick: () => navigate(c.path) })
        : el('span', { class: 'viewer-crumb static', text: c.name }));
    }

    const entries = Array.isArray(data.entries) ? data.entries : [];
    const meta = el('div', { class: 'viewer-list-meta' }, [
      el('span', { text: t('viewer.items', { n: entries.length, count: entries.length }) }),
    ]);
    if (data.truncated) meta.append(el('span', { class: 'viewer-warn', text: t('viewer.truncated', { n: entries.length }) }));
    if (entry.noIndex) meta.append(el('span', { class: 'viewer-warn', text: t('viewer.no_index') }));

    if (!entries.length) {
      list.append(el('div', { class: 'viewer-note', text: t('viewer.empty_folder') }));
    }
    for (const it of entries) {
      const row = el('button', {
        class: 'viewer-entry', type: 'button',
        onclick: () => navigate(joinPath(here, it.name)),
      });
      row.append(viewerIcon(it.kind === 'dir' ? 'listing' : it.viewer));
      row.append(el('span', { class: 'viewer-entry-name', text: it.name }));
      row.append(el('span', {
        class: 'viewer-entry-meta',
        text: it.kind === 'dir' ? t('viewer.folder') : fileSize(it.size),
      }));
      list.append(row);
    }
    body.replaceChildren(el('div', { class: 'viewer-listing' }, [crumbBar, meta, list]));
  }

  function errorPane(kind) {
    const key = kind === 'off' ? 'viewer.off'
      : kind === 'not_found' ? 'viewer.not_found'
        : 'viewer.denied';
    const pane = el('div', { class: 'viewer-error', role: 'alert' }, [
      el('div', { class: 'viewer-error-text', text: t(key) }),
    ]);
    if (kind === 'off') {
      pane.append(el('button', {
        class: 'viewer-card-btn', type: 'button', text: t('viewer.open_settings'),
        onclick: () => {
          close();
          doc.dispatchEvent(new CustomEvent('dispatch:open-settings', { detail: { tab: 'device' } }));
        },
      }));
    }
    return pane;
  }

  // --- mount --------------------------------------------------------------

  const app = doc.getElementById('app');
  if (app) {
    app.inert = true;
    cleanups.push(() => { app.inert = false; });
  }
  doc.body.append(node);
  current = {
    node,
    close,
    // Give up ownership of the history entry without popping it (see the
    // replace path at the top of openViewer). Returns whether there was one.
    releaseHistory: () => { const had = pushedHistory; pushedHistory = false; return had; },
  };
  closeBtn.focus();

  // One browser history entry for the overlay, so a phone's Back closes it.
  // `poppedBySystem` keeps close() from popping an entry the browser already
  // consumed (the double-pop that would take the app back a whole page).
  if (typeof history !== 'undefined' && history.pushState) {
    if (adoptedHistory) {
      pushedHistory = true;    // reuse the entry the replaced viewer left behind
    } else {
      try { history.pushState({ viewer: 1 }, ''); pushedHistory = true; } catch { /* best effort */ }
    }
    const onPop = () => { poppedBySystem = true; close(); };
    window.addEventListener('popstate', onPop);
    cleanups.push(() => window.removeEventListener('popstate', onPop));
  }

  if (mode === 'url') {
    const entry = stack.push({ mode: 'url', url, title });
    paint(entry);
  } else {
    navigate(path);
  }
  return node;
}
