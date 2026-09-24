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
// The heavy vendor bundle (highlight.js, 127KB) used to be a plain <script>
// tag in the document, so every cold start — including a phone that only ever
// reads a message — paid for it before first paint. It is now fetched the
// first time something actually needs it. Both helpers memoise per URL, so
// concurrent callers share one network request.
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

/** Would a browser refuse `target` purely because `pageUrl` is secure?
 *
 *  A document loaded over https: may not embed, or even fetch, an http: origin
 *  — the request is blocked as mixed content before it reaches the network.
 *  That is a property of the two URLs, so it is decidable up front, and it has
 *  to be decided up front: a `no-cors` fetch of a blocked URL still RESOLVES
 *  with an opaque response, so probing tells you it worked when nothing was
 *  retrieved at all.
 *
 *  This is the whole reason the StudioForge pane came up blank over Tailscale.
 *  Tailscale Serve fronts DisPatch over HTTPS on a tailnet name while the rig is
 *  configured as a bare http:// address; over plain http on the LAN the very
 *  same build is fine. Lives here, as a pure function of two strings, so it can
 *  be tested without a browser or a tailnet.
 *
 *  Loopback is exempt because browsers treat 127.0.0.1 and localhost as
 *  potentially-trustworthy origins (W3C secure-contexts) and do not block them.
 */
export function isMixedContent(pageUrl, target) {
  let page, dest;
  try { page = new URL(pageUrl); } catch { return false; }
  if (page.protocol !== 'https:') return false;
  try { dest = new URL(target, pageUrl); } catch { return false; }
  if (dest.protocol !== 'http:') return false;
  const h = dest.hostname;
  if (h === 'localhost' || h === '127.0.0.1' || h === '[::1]' || h === '::1') return false;
  if (h.endsWith('.localhost')) return false;
  return true;
}

// --- Rail icons -------------------------------------------------------------
// Thin-stroke line icons in currentColor for the sidebar rail and anywhere
// else an emoji would clash with monochrome chrome: emoji draw in the
// platform's colour set, so a rail mixing them with line art reads as two
// different apps. One language, one place. 24px grid, 1.8px rounded strokes;
// colour comes from the element, so themes and accent states apply for free.
//
// Built with createElementNS from constant path data. No innerHTML: el()
// above bans it, and a hand-rolled exception for "trusted" markup is how that
// ban erodes.

const SVG_NS = 'http://www.w3.org/2000/svg';

/** Strip a decorative leading glyph from a translated label.
 *
 *  Some labels ship their icon inside the string — "📷 Change photo",
 *  "🛡️ Safe". Editing that out of eight locale files would mean 200-odd
 *  translated strings changed by hand, and the next translation pass could put
 *  the emoji straight back. Stripping at RENDER time instead leaves the
 *  locales alone, survives a re-translation, and keeps the glyph as the
 *  fallback anywhere this helper is not used.
 *
 *  Deliberately conservative: only leading pictographs, arrows, dingbats and
 *  the spaces after them. A label that begins with a letter, a digit or a
 *  quote is returned untouched, so a language whose word order puts real text
 *  first can never lose a character.
 */
const LEADING_GLYPH = /^(?:[\p{Extended_Pictographic}←-⇿⌀-➿⬀-⯿️‍]|\s)+/u;
/** Ref-counted `inert` on the page behind a modal surface.
 *
 *  Four places independently set and cleared `inert` on #app: the modal-focus
 *  guard, the image lightbox, the Local Viewer and the job detail overlay.
 *  Each wrote the attribute unconditionally, so whichever surface closed FIRST
 *  lifted it for all of them. Two real paths hit this: closing an avatar
 *  lightbox opened from the Bot Manager re-enabled the Bot Manager's backdrop
 *  underneath, and the drop backdrop toggling during a drag made the guard
 *  decide "no modal is open" and strip the job overlay's inert. Either way Tab
 *  escapes a dialog that declares aria-modal — the exact promise the trap
 *  exists to keep.
 *
 *  A count fixes it: the page becomes inert on the first acquire and reachable
 *  again only on the last release. Every owner holds a token and releases its
 *  own, so order stops mattering.
 *
 *  Releasing twice is a no-op rather than an error: cleanup paths in this app
 *  run more than once by design (a close handler plus an unmount), and
 *  double-counting down would leave the app permanently inert, which is far
 *  worse than an extra call.
 */
const _inertTargets = () => ['app', 'mobile-tabs']
  .map((id) => (typeof document === 'undefined' ? null : document.getElementById(id)))
  .filter(Boolean);
const _inertHeld = new Set();
let _inertSeq = 0;

export function acquireInert() {
  const token = `inert-${++_inertSeq}`;
  _inertHeld.add(token);
  // Applied on EVERY acquire, not just the first. Setting the attribute when
  // it is already set is a no-op, and doing it unconditionally means a target
  // that was replaced since the first hold (a re-render swapping #app, or a
  // fresh document under a test harness) still ends up inert. Only the
  // REMOVAL is gated on the count reaching zero, which is the half that has to
  // be careful.
  for (const node of _inertTargets()) node.setAttribute('inert', '');
  return token;
}

export function releaseInert(token) {
  if (!token || !_inertHeld.delete(token)) return;
  if (_inertHeld.size === 0) {
    for (const node of _inertTargets()) node.removeAttribute('inert');
  }
}

/** Escape hatch for a hard reset (a lock, a view teardown): drop every hold. */
export function resetInert() {
  _inertHeld.clear();
  for (const node of _inertTargets()) node.removeAttribute('inert');
}

export function glyphless(s) {
  return String(s ?? '').replace(LEADING_GLYPH, '').trim();
}

/** An icon + its label, as children for el(): the button keeps one accessible
 *  name and the glyph is marked aria-hidden by railIcon(). */
export function iconLabel(icon, label) {
  const text = glyphless(label);
  return [railIcon(icon), document.createTextNode(text ? ` ${text}` : '')];
}

/** One icon: an array of path `d` strings on a 24×24 grid → an <svg>. */
export function railIcon(paths) {
  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('fill', 'none');
  svg.setAttribute('stroke', 'currentColor');
  svg.setAttribute('stroke-width', '1.8');
  svg.setAttribute('stroke-linecap', 'round');
  svg.setAttribute('stroke-linejoin', 'round');
  svg.setAttribute('aria-hidden', 'true');
  svg.classList.add('rail-icon');
  for (const d of paths) {
    const p = document.createElementNS(SVG_NS, 'path');
    p.setAttribute('d', d);
    svg.append(p);
  }
  return svg;
}

/** The shared set, named by meaning.
 *
 *  Grew past the rail in 2026-09: the tab bar, the settings tab strip, the
 *  chat header and the composer all shipped colour EMOJI as their glyphs, and
 *  emoji are drawn by the platform in its own palette. They cannot take a
 *  theme. Next to the rail's line art — and on Paper or Daylight especially —
 *  they read as another app's furniture pasted into this one. Same rule as
 *  before, applied everywhere: chrome is line art in currentColor, so a glyph
 *  is the palette's colour, dims with its button, and takes the accent when
 *  its tab is the active one, for free.
 *
 *  The line that decides what belongs here: colour emoji used as CHROME get an
 *  icon. Monochrome typographic marks (the ⌫ on the keypad, the ↑↓↵ in the
 *  command palette's key hints, the ▸/▾ on a details summary, the ⇅/▲/▼ on a
 *  sortable table head) already inherit currentColor and already theme, so
 *  they stay as text. An emoji a PERSON chose — a bot's avatar, a reaction, a
 *  custom link's glyph — is content, not chrome, and is never touched.
 */
export const RAIL_ICONS = {
  // Picture frame, broken by the slash: "no images".
  nim: [
    'M2 2 22 22',
    'M10.41 10.41a2 2 0 1 1-2.83-2.83',
    'M13.5 13.5 6 21',
    'M18 12l3 3',
    'M3.59 3.59A1.99 1.99 0 0 0 3 5v14a2 2 0 0 0 2 2h14c.55 0 1.052-.22 1.41-.59',
    'M21 15V5a2 2 0 0 0-2-2H9',
  ],
  // Eye, struck through: "this device sees nothing".
  privacy: [
    'M2 2 22 22',
    'M9.88 9.88a3 3 0 1 0 4.24 4.24',
    'M10.73 5.08A10.43 10.43 0 0 1 12 5c7 0 10 7 10 7a13.16 13.16 0 0 1-1.67 2.68',
    'M6.61 6.61A13.53 13.53 0 0 0 2 12s3 7 10 7a9.74 9.74 0 0 0 5.39-1.61',
  ],
  // The silhouette Minimal avatars switches to.
  avatars: [
    'M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2',
    'M16 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0',
  ],
  // External-link arrow: a custom link button with no chosen emoji.
  link: [
    'M15 3h6v6',
    'M10 14 21 3',
    'M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6',
  ],
  // `bots`, `messages` and `plug` used to have their own empty-state-only
  // shapes here; consolidated 2026-09-18 into the single definitions under
  // "Navigation: the four mobile tabs" below (a duplicate object key just
  // silently shadows the earlier one, so two icons under one name is a bug,
  // not a feature) — the empty states now draw the same glyph as the tab/
  // first-run-card that means the same thing, which is the point.
  moon: ['M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z'],
  sun: [
    'M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8',
    'M12 2v2', 'M12 20v2', 'M4.93 4.93l1.41 1.41', 'M17.66 17.66l1.41 1.41',
    'M2 12h2', 'M20 12h2', 'M6.34 17.66l-1.41 1.41', 'M19.07 4.93l-1.41 1.41',
  ],
  lock: [
    'M5 11h14a2 2 0 0 1 2 2v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-6a2 2 0 0 1 2-2Z',
    'M7 11V7a5 5 0 0 1 10 0v4',
  ],
  unlock: [
    'M5 11h14a2 2 0 0 1 2 2v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-6a2 2 0 0 1 2-2Z',
    'M7 11V7a5 5 0 0 1 9.9-1',
  ],
  folder: [
    'M4 20h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13c0 1.1.9 2 2 2Z',
  ],
  upload: [
    'M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4',
    'M17 8l-5-5-5 5',
    'M12 3v12',
  ],
  // --- Local viewer chrome -------------------------------------------------
  // Chevron pointing back. The viewer's own history (listing → file), not the
  // browser's — hidden at depth 0.
  'viewer-back': ['M15 5 8 12l7 7'],
  // The same external-link arrow as `link`, kept separate so the rail's
  // meaning ("a link button") and the viewer's ("open this in a tab") can
  // drift apart later without one of them silently changing.
  'viewer-open': [
    'M15 3h6v6',
    'M10 14 21 3',
    'M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6',
  ],
  // Tray with an arrow into it: save this file.
  'viewer-download': [
    'M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4',
    'M7 10l5 5 5-5',
    'M12 15V3',
  ],
  // Circular arrow: re-fetch the pane.
  'viewer-reload': [
    'M21 12a9 9 0 1 1-2.64-6.36',
    'M21 3v6h-6',
  ],
  // Folder, in the listing and as a directory entry's icon.
  'viewer-folder': [
    'M4 20h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13c0 1.1.9 2 2 2Z',
  ],
  // Page with a folded corner: every non-directory entry.
  'viewer-file': [
    'M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8Z',
    'M14 3v5h5',
  ],
  gear: [
    'M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z',
    'M15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0Z',
  ],

  // --- Navigation: the four mobile tabs ------------------------------------
  // A companion's head. The roster is drawn with real avatars everywhere else;
  // this is the stand-in for "all of them", so it is deliberately generic —
  // a friendly machine, not one of the bots.
  bots: [
    'M5 8.5h14a1.5 1.5 0 0 1 1.5 1.5v7a1.5 1.5 0 0 1-1.5 1.5H5A1.5 1.5 0 0 1 3.5 17v-7A1.5 1.5 0 0 1 5 8.5Z',
    'M12 5.5v3',
    'M12 3.6v.01',
    'M8.8 12.6v.01',
    'M15.2 12.6v.01',
    'M9.5 15.6h5',
  ],
  // TWO bubbles for the thread LIST, one for the conversation you are in.
  // Drawn as a pair rather than a single bubble because at 19px a lone bubble
  // and a lone bubble-with-lines are the same picture, and these two sit side
  // by side in the tab bar where the difference has to survive a glance.
  chats: [
    'M17 12.5a4.5 4.5 0 0 1-4.5 4.5H8.2L4.5 19.6v-2.9A4.5 4.5 0 0 1 3 13.3V10a4.5 4.5 0 0 1 4.5-4.5h5A4.5 4.5 0 0 1 17 10Z',
    'M8.5 5.6A4.5 4.5 0 0 1 12.2 3.5h4.3A4.5 4.5 0 0 1 21 8v3.3a4.5 4.5 0 0 1-1.5 3.35',
  ],
  messages: [
    'M20.5 12a8.5 8.5 0 0 1-12.3 7.6L3.5 21l1.4-4.7A8.5 8.5 0 1 1 20.5 12Z',
    'M8.8 10.5h6.4',
    'M8.8 14h4.2',
  ],
  // Clipboard: the job board.
  jobs: [
    'M9.8 3.5h4.4a1 1 0 0 1 1 1v1.6H8.8V4.5a1 1 0 0 1 1-1Z',
    'M15.2 5.6h1.8A1.8 1.8 0 0 1 18.8 7.4v12.3a1.8 1.8 0 0 1-1.8 1.8H7A1.8 1.8 0 0 1 5.2 19.7V7.4A1.8 1.8 0 0 1 7 5.6h1.8',
    'M9 11.5h6',
    'M9 15.3h3.8',
  ],

  // --- The settings tab strip ----------------------------------------------
  bolt: ['M13.5 3 5.5 13.5h5.2L10 21l8.2-10.6H13Z'],          // reactions
  pulse: ['M3 12.5h3.6L9 5.5l4.2 13 2.4-6h5.4'],              // host health
  chip: [                                                      // AI models
    'M7.5 7.5h9v9h-9Z',
    'M10.5 3.5v3', 'M13.5 3.5v3', 'M10.5 17.5v3', 'M13.5 17.5v3',
    'M3.5 10.5h3', 'M3.5 13.5h3', 'M17.5 10.5h3', 'M17.5 13.5h3',
  ],
  palette: [                                                   // theme
    'M12 3.2a8.8 8.8 0 0 0 0 17.6 1.9 1.9 0 0 0 1.9-1.9 1.9 1.9 0 0 1 1.9-1.9h1.4a3.6 3.6 0 0 0 3.6-3.6A8.8 8.8 0 0 0 12 3.2Z',
    'M7.4 12.4v.01', 'M9.6 8.6v.01', 'M14.4 7.9v.01',
  ],
  device: [                                                    // this device
    'M8.5 2.8h7a1.8 1.8 0 0 1 1.8 1.8v14.8a1.8 1.8 0 0 1-1.8 1.8h-7a1.8 1.8 0 0 1-1.8-1.8V4.6a1.8 1.8 0 0 1 1.8-1.8Z',
    'M11 18.4h2',
  ],
  shield: [                                                    // PIN + sessions
    'M12 3.2 5.4 5.9v5.4c0 4.1 2.8 7.9 6.6 9 3.8-1.1 6.6-4.9 6.6-9V5.9Z',
    'M12 11.2v2.6',
    'M12 9.4v.01',
  ],

  // --- Header, composer and modal chrome -----------------------------------
  // Stacked frames: this bot draws its thread faces from an avatar pool.
  images: [
    'M8.5 3.5h11A1.5 1.5 0 0 1 21 5v9a1.5 1.5 0 0 1-1.5 1.5h-11A1.5 1.5 0 0 1 7 14V5a1.5 1.5 0 0 1 1.5-1.5Z',
    'M7 18.5H4.5A1.5 1.5 0 0 1 3 17V7.5',
    'M11 8.2v.01',
    'M21 12.5 17 9l-5.5 6.5',
  ],
  // Camera: change this bot's picture.
  camera: [
    'M4.5 7.5h3l1.4-2.2h6.2L16.5 7.5h3A1.5 1.5 0 0 1 21 9v9a1.5 1.5 0 0 1-1.5 1.5h-15A1.5 1.5 0 0 1 3 18V9a1.5 1.5 0 0 1 1.5-1.5Z',
    'M12 16.6a3.3 3.3 0 1 0 0-6.6 3.3 3.3 0 0 0 0 6.6Z',
  ],
  // Plug: the "no model connected yet" first-run card.
  plug: ['M9 3v5', 'M15 3v5', 'M6.5 8h11v2.8a5.5 5.5 0 0 1-11 0Z', 'M12 16.3V21'],
  search: ['M11 4.2a6.8 6.8 0 1 0 0 13.6 6.8 6.8 0 0 0 0-13.6', 'M16.1 16.1 20.5 20.5'],
  plus: ['M12 5.5v13', 'M5.5 12h13'],
  close: ['M6.4 6.4 17.6 17.6', 'M17.6 6.4 6.4 17.6'],
  back: ['M14.5 5.5 8 12l6.5 6.5'],
  forward: ['M9.5 5.5 16 12l-6.5 6.5'],
  'chevron-down': ['M5.5 9 12 15.5 18.5 9'],
  // Two overlapping frames: this thread in its own window.
  popout: [
    'M9 3.5h11.5V15',
    'M3.5 9h11.5v11.5H3.5Z',
  ],
  menu: ['M6 12v.01', 'M12 12v.01', 'M18 12v.01'],
  // Two stacked sheets: copy this message's text.
  copy: [
    'M9 9h10.5v11.5H9Z',
    'M15 9V4.5A1 1 0 0 0 14 3.5H5.5a1 1 0 0 0-1 1V14a1 1 0 0 0 1 1H9',
  ],
  // A plain tick: the copy landed.
  tick: ['M5 12.5l4.5 4.5L19 7.5'],
  // A hooked arrow: quote this message in a reply.
  reply: ['M9.5 6 4.5 11l5 5', 'M4.5 11h9a6 6 0 0 1 6 6v1.5'],
  // A circular arrow: ask for this reply again.
  regenerate: ['M19.5 12a7.5 7.5 0 1 1-2.2-5.3', 'M19.5 4.5v4.5H15'],
  attach: ['M19.4 11.7 12 19.1a4.7 4.7 0 0 1-6.6-6.6l7.4-7.4a3.2 3.2 0 0 1 4.5 4.5l-7.4 7.4a1.7 1.7 0 0 1-2.4-2.4l6.8-6.8'],
  send: ['M12 19.5V5.2', 'M6.2 11 12 5.2 17.8 11'],
  stop: ['M8 8h8v8H8Z'],
  // Sliders: the operator tools menu.
  tools: [
    'M4 7.5h7', 'M15 7.5h5', 'M4 12h3', 'M11 12h9', 'M4 16.5h7', 'M15 16.5h5',
    'M13 7.5v.01', 'M9 12v.01', 'M13 16.5v.01',
  ],

  // --- Thread menu, recovery panel, transcript, jobs, dashboard (2026-09-18) -
  // A map pin: pin/unpin a thread or a settings row.
  pin: [
    'M12 21s-6.5-5.8-6.5-11A6.5 6.5 0 0 1 18.5 10c0 5.2-6.5 11-6.5 11Z',
    'M12 12.5a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5Z',
  ],
  // A pencil: rename this thread.
  rename: [
    'M17.5 3.5a2.1 2.1 0 0 1 3 3L8 19 3.5 20.5 5 16Z',
    'M14.5 6.5l3 3',
  ],
  // A box with a lid line: archive this thread.
  archive: [
    'M3.5 6.5h17v3h-17Z',
    'M4.5 9.5v9a1.5 1.5 0 0 0 1.5 1.5h12a1.5 1.5 0 0 0 1.5-1.5v-9',
    'M10 13.2h4',
  ],
  // A trash can: delete this thread / wipe old files.
  trash: [
    'M4.5 6.5h15', 'M9.5 6.5V4.8a1 1 0 0 1 1-1h3a1 1 0 0 1 1 1v1.7',
    'M6.5 6.5 7.3 19.3a1.5 1.5 0 0 0 1.5 1.4h6.4a1.5 1.5 0 0 0 1.5-1.4l.8-12.8',
    'M10 10.5v6', 'M14 10.5v6',
  ],
  // A life-ring: recovery & export.
  lifering: [
    'M12 20a8 8 0 1 0 0-16 8 8 0 0 0 0 16Z',
    'M12 15.5a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7Z',
    'M6.3 6.3l3.3 3.3', 'M17.7 6.3l-3.3 3.3', 'M6.3 17.7l3.3-3.3', 'M17.7 17.7l-3.3-3.3',
  ],
  thumbsup: [
    'M8 21H5.5a1.5 1.5 0 0 1-1.5-1.5v-7A1.5 1.5 0 0 1 5.5 11H8',
    'M8 21V11l4.5-7a2 2 0 0 1 3.7 1.4L15 10h4a2 2 0 0 1 1.9 2.7l-2.6 7A2 2 0 0 1 16.4 21H8Z',
  ],
  thumbsdown: [
    'M16 3h2.5A1.5 1.5 0 0 1 20 4.5v7a1.5 1.5 0 0 1-1.5 1.5H16',
    'M16 3v10l-4.5 7a2 2 0 0 1-3.7-1.4L9 14H5a2 2 0 0 1-1.9-2.7l2.6-7A2 2 0 0 1 7.6 3H16Z',
  ],
  // A sparkle: generate a new reaction picture.
  sparkle: [
    'M12 3.5l1.4 4.1 4.1 1.4-4.1 1.4-1.4 4.1-1.4-4.1-4.1-1.4 4.1-1.4Z',
    'M19 14l.7 2 2 .7-2 .7-.7 2-.7-2-2-.7 2-.7Z',
  ],
  // A circled check: a finished/successful status (an upload that landed).
  check: [
    'M12 20.5a8.5 8.5 0 1 0 0-17 8.5 8.5 0 0 0 0 17Z',
    'M8.3 12.3l2.5 2.5 5-5.2',
  ],
  // A triangle bang: a stale/low/failed status banner.
  alert: [
    'M12 3.5 21.5 20h-19Z',
    'M12 9.5v5', 'M12 17.3v.01',
  ],
  // A clock with a sweep: transcript / agent-session history.
  history: [
    'M12 20a8 8 0 1 0-7.2-4.5',
    'M4.5 15.5v-4h4',
    'M12 7.5V12l3 2',
  ],
  // A floppy/disk: storage card.
  disk: [
    'M5 3.5h11.5L19 6v14.5H5Z',
    'M8 3.5v5h7v-5', 'M7.5 14h9v6.5h-9Z',
  ],
  // A stacked cylinder: database card.
  database: [
    'M12 4c4.1 0 7.5 1.1 7.5 2.5S16.1 9 12 9s-7.5-1.1-7.5-2.5S7.9 4 12 4Z',
    'M4.5 6.5V12c0 1.4 3.4 2.5 7.5 2.5s7.5-1.1 7.5-2.5V6.5',
    'M4.5 12v5.5c0 1.4 3.4 2.5 7.5 2.5s7.5-1.1 7.5-2.5V12',
  ],
  // A circled question mark: "maybe" on a job vote.
  help: [
    'M12 20.5a8.5 8.5 0 1 0 0-17 8.5 8.5 0 0 0 0 17Z',
    'M9.5 9.2a2.5 2.5 0 1 1 3.7 2.2c-.8.5-1.2 1-1.2 1.9',
    'M12 16.5v.01',
  ],
  // A person: the transcript's "you" filter chip.
  user: [
    'M12 12.2a4 4 0 1 0 0-8 4 4 0 0 0 0 8Z',
    'M4.8 19.5a7.2 7.2 0 0 1 14.4 0',
  ],
  // A brain: the transcript's "thinking" filter chip.
  brain: [
    'M9.5 4.3a2.8 2.8 0 0 0-2.8 2.8v.3a2.6 2.6 0 0 0-1.5 4.6 2.7 2.7 0 0 0 .6 4.9A2.8 2.8 0 0 0 8.5 20a2.8 2.8 0 0 0 1.9-.8',
    'M14.5 4.3a2.8 2.8 0 0 1 2.8 2.8v.3a2.6 2.6 0 0 1 1.5 4.6 2.7 2.7 0 0 1-.6 4.9 2.8 2.8 0 0 1-2.7 3.1 2.8 2.8 0 0 1-1.9-.8',
    'M12 6v13',
  ],
  // An open eye: preview this file.
  eye: [
    'M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12Z',
    'M12 14.7a2.7 2.7 0 1 0 0-5.4 2.7 2.7 0 0 0 0 5.4Z',
  ],
};
