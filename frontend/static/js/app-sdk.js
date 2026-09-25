// DisPatch app SDK — the ONE module an app page (or an app's thread hook)
// imports from the shell.
//
// Contract: docs/design/2026-09-25-apps.md, "Frontend contract". An app is a
// trusted package under apps/<id>/: its page is served at /apps/<id>/ and
// framed by the shell WITHOUT a sandbox (same origin), so it can call its own
// API with the operator's session cookie. Everything that page needs from the
// shell comes through here, so the shell's internals (api.js, i18n.js,
// main.js) can change without breaking an app:
//
//   api(path, {method, body, query, signal})   same-origin JSON fetch
//   DecoyError                                  thrown on the Safe-Mode 403
//   t(key, vars) / ready() / has(key)           the app's own locale files
//   theme()                                     the shell's palette + light/dark
//   openThread(id, hint?) / close() / toast(text, isError?) / setTitle(text)
//                                               asks the shell (postMessage)
//   onMessage(fn)                               messages FROM the shell
//
// Plus a few DOM helpers re-exported from the shell's util.js (el, railIcon,
// iconLabel, RAIL_ICONS) so an app's chrome is drawn with the same line icons
// as the rail — and a date formatter that follows the shell's language.
//
// Two ways this module runs:
//   1. Inside an app page (/apps/<id>/…): the app id comes from the path, the
//      module-level t()/ready() load /apps/<id>/locales/<lang>.json.
//   2. Inside the SHELL, imported by main.js and by an app's thread hook: the
//      path says nothing about an app, so main.js builds a bound instance with
//      forApp(id) and hands it to the hook's mount().
// Nothing runs at import time except reading location.pathname: no fetch, no
// listener, no DOM write (it is in the sw.js SHELL and imported by main.js).

import { el, railIcon, iconLabel, RAIL_ICONS } from './util.js?v=20';

export { el, railIcon, iconLabel, RAIL_ICONS };

// ---------------------------------------------------------------------------
// Shared settings — the SAME keys the shell uses. Kept literal (not imported
// from i18n.js/theme.js) so this module never pulls the shell's runtime into
// an app page; tests/app-sdk.test.js pins them against index.html.
// ---------------------------------------------------------------------------

/** localStorage key the shell's language choice lives under (js/i18n.js). */
export const LANG_KEY = 'dispatch-lang';
/** localStorage key the shell's palette lives under (index.html's
 *  ui-theme.js `data-storage-key`). */
export const PALETTE_KEY = 'dispatch-palette';
/** The shell's default palette (index.html's ui-theme.js `data-default`). */
const DEFAULT_PALETTE = 'glacier';
/** The eight shipped languages, same list as i18n.js and the no-FOUC script. */
export const SUPPORTED_LANGS = ['en', 'ar', 'de', 'es', 'fr', 'ja', 'pt', 'zh'];
const RTL = new Set(['ar', 'he', 'fa', 'ur', 'yi', 'dv', 'ckb']);
/** Stylesheets an app page needs to look like the shell, in index.html order
 *  (tokens + the DisPatch adapter). app.css is deliberately NOT here: it is
 *  the shell's own layout, not a theme. */
const THEME_CSS = [
  '/static/theme.css?v=7',
  '/static/ui-theme/ui-theme.css',
  '/static/ui-theme/adapters/dispatch-compat.css',
];
const THEME_ATTRS = ['data-palette', 'data-theme', 'data-contrast-profile'];
const ID_RE = /^[a-z0-9-]{1,40}$/;

/** `/apps/<id>/…` → `<id>`, else null. */
export function appIdFromPath(pathname) {
  const m = /^\/apps\/([a-z0-9-]{1,40})(?:\/|$)/.exec(String(pathname || ''));
  return m ? m[1] : null;
}

const HERE = (() => {
  try { return typeof location !== 'undefined' ? location : null; } catch { return null; }
})();
/** This page's app id, or null inside the shell. */
export const APP_ID = HERE ? appIdFromPath(HERE.pathname) : null;

// ---------------------------------------------------------------------------
// api()
// ---------------------------------------------------------------------------

/** The uniform Safe-Mode answer: the page should say "Unlock for full access",
 *  never pretend the data is empty. */
export class DecoyError extends Error {
  constructor(message = 'Unlock for full access') {
    super(message);
    this.name = 'DecoyError';
    this.status = 403;
    this.decoy = true;
  }
}

function detailText(body, status) {
  const d = body && body.detail;
  if (typeof d === 'string' && d) return d;
  if (Array.isArray(d) && d.length) {
    return d.map((x) => (x && (x.msg || x.message)) || JSON.stringify(x)).join('; ');
  }
  if (d && typeof d === 'object') return d.message || JSON.stringify(d);
  if (body && typeof body.message === 'string' && body.message) return body.message;
  return `HTTP ${status}`;
}

/** Same-origin JSON request. Resolves to the parsed body (null for 204);
 *  throws DecoyError on a Safe-Mode 403, else an Error carrying `status`,
 *  `body` and the server's `detail` as its message. A network failure throws
 *  the platform's TypeError with `status` undefined. */
export async function api(path, { method = 'GET', body, query, signal, headers } = {}) {
  const p = String(path || '');
  // Relative to THIS origin only: an absolute or protocol-relative URL here
  // would carry the session cookie's authority to somewhere else.
  if (!p.startsWith('/') || p.startsWith('//')) throw new Error(`api(): path must start with "/": ${p}`);
  let url = p;
  if (query) {
    const qs = query instanceof URLSearchParams ? query : new URLSearchParams(
      Object.entries(query).filter(([, v]) => v != null && v !== '').map(([k, v]) => [k, String(v)]),
    );
    const s = qs.toString();
    if (s) url += (url.includes('?') ? '&' : '?') + s;
  }
  const init = { method: String(method).toUpperCase(), credentials: 'same-origin', headers: { Accept: 'application/json', ...(headers || {}) } };
  if (signal) init.signal = signal;
  if (body !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  }
  const res = await fetch(url, init);
  let data = null;
  if (res.status !== 204) {
    try { data = await res.json(); } catch { data = null; }
  }
  if (res.ok) return data;
  if (res.status === 403 && data && data.decoy === true) throw new DecoyError();
  const err = new Error(detailText(data, res.status));
  err.status = res.status;
  err.body = data;
  throw err;
}

// ---------------------------------------------------------------------------
// i18n — an app's own locale files, flat keys, English fallback.
// ---------------------------------------------------------------------------

function pickLang(tag) {
  const base = String(tag || '').toLowerCase().split(/[-_]/)[0];
  return SUPPORTED_LANGS.includes(base) ? base : null;
}

/** The shell's language: the stored choice, then the browser's list, then en.
 *  The same resolution index.html's no-FOUC script and i18n.js perform. */
export function shellLang() {
  let lang = null;
  try { lang = pickLang(localStorage.getItem(LANG_KEY)); } catch { /* storage blocked */ }
  if (!lang && typeof navigator !== 'undefined') {
    const prefs = (navigator.languages && navigator.languages.length) ? navigator.languages : [navigator.language || 'en'];
    for (const p of prefs) { lang = pickLang(p); if (lang) break; }
  }
  return lang || 'en';
}

function interpolate(str, vars, lang) {
  if (!vars) return str;
  return str.replace(/\{(\w+)\}/g, (whole, name) => {
    if (!Object.prototype.hasOwnProperty.call(vars, name)) return whole;
    const v = vars[name];
    if (v == null) return '';
    if (typeof v === 'number' && Number.isFinite(v)) {
      try { return new Intl.NumberFormat(lang).format(v); } catch { return String(v); }
    }
    return String(v);
  });
}

/** An i18n instance for one app. `t` never throws and never returns blank:
 *  active language → English → the key itself. */
export function createI18n(appId) {
  const st = { lang: 'en', dict: null, en: null, loading: null, loadedFor: null };
  const listeners = new Set();
  const base = appId && ID_RE.test(appId) ? `/apps/${appId}/locales/` : null;

  async function fetchDict(lang) {
    if (!base) return null;
    try {
      const r = await fetch(`${base}${lang}.json`, { credentials: 'same-origin' });
      if (!r.ok) return null;
      const d = await r.json();
      return d && typeof d === 'object' ? d : null;
    } catch { return null; }
  }

  let seq = 0;
  function load(lang) {
    const want = pickLang(lang) || 'en';
    if (st.loadedFor === want && st.dict && !st.loading) return Promise.resolve(api_);
    if (st.loading && st.loading.lang === want) return st.loading.p;
    const mine = ++seq;
    const p = (async () => {
      const en = st.en || await fetchDict('en');
      const dict = want === 'en' ? en : await fetchDict(want);
      // A newer load() superseded this one while it was out: the newest
      // request wins, whatever order the responses arrive in.
      if (mine !== seq) return api_;
      st.en = en;
      st.dict = dict || en;
      st.lang = want;
      st.loadedFor = want;
      st.loading = null;
      for (const fn of listeners) { try { fn(want); } catch { /* a listener is not our problem */ } }
      return api_;
    })();
    // The IIFE may already have finished synchronously (everything cached);
    // only record it as in flight while it actually is.
    if (st.loadedFor !== want || mine !== seq) st.loading = { lang: want, p };
    return p;
  }

  function lookup(dict, key) {
    if (!dict || !Object.prototype.hasOwnProperty.call(dict, key)) return null;
    return dict[key];
  }

  function pick(value, vars) {
    // A plural object ({one, other, …}) is chosen by `count`, like the shell.
    if (value && typeof value === 'object' && !Array.isArray(value) && 'other' in value) {
      const n = vars && typeof vars.count === 'number' ? vars.count : null;
      if (n == null) return value.other;
      let cat = 'other';
      try { cat = new Intl.PluralRules(st.lang).select(Math.abs(n)); } catch { /* keep other */ }
      return value[cat] != null ? value[cat] : value.other;
    }
    return value;
  }

  const api_ = {
    t(key, vars) {
      if (!key) return '';
      let raw = pick(lookup(st.dict, key), vars);
      if (raw == null) raw = pick(lookup(st.en, key), vars);
      if (raw == null) return String(key);
      return interpolate(String(raw), vars, st.lang);
    },
    has(key) { return lookup(st.dict, key) != null; },
    /** Load the dictionaries for `lang` (default: the shell's). */
    ready(lang) { return load(lang || shellLang()); },
    lang() { return st.lang; },
    dir() { return RTL.has(st.lang) ? 'rtl' : 'ltr'; },
    onChange(fn) { listeners.add(fn); return () => listeners.delete(fn); },
    /** Locale-aware date + time for an ISO string (empty for garbage). */
    dateTime(iso) {
      if (!iso) return '';
      const d = new Date(iso);
      if (Number.isNaN(d.getTime())) return '';
      try {
        return new Intl.DateTimeFormat(st.lang, { dateStyle: 'medium', timeStyle: 'short' }).format(d);
      } catch { return d.toISOString(); }
    },
  };
  return api_;
}

/** A bound {api, t, has, ready, lang, dateTime} for app `id` — what main.js
 *  hands to an app's thread hook, which runs in the shell document where the
 *  path names no app. */
export function forApp(id) {
  const i18n = createI18n(id);
  return {
    appId: id,
    api,
    t: i18n.t,
    has: i18n.has,
    ready: i18n.ready,
    lang: i18n.lang,
    dateTime: i18n.dateTime,
  };
}

const pageI18n = createI18n(APP_ID);

/** Translate a key from this app's locale files. */
export const t = (key, vars) => pageI18n.t(key, vars);
/** True when the active language (not just English) has the key. */
export const has = (key) => pageI18n.has(key);
/** The active language code. */
export const lang = () => pageI18n.lang();
/** Locale-aware date + time. */
export const dateTime = (iso) => pageI18n.dateTime(iso);

/** Load this app's dictionaries for the shell's language and stamp
 *  <html lang/dir>. Await it before the first render. Safe to call again. */
export async function ready(language) {
  await pageI18n.ready(language);
  if (APP_ID && typeof document !== 'undefined') {
    document.documentElement.setAttribute('lang', pageI18n.lang());
    document.documentElement.setAttribute('dir', pageI18n.dir());
  }
  wirePage();
  return pageI18n;
}

// ---------------------------------------------------------------------------
// Messaging — app page ⇄ shell. Same origin only, parent only.
// ---------------------------------------------------------------------------

function inFrame() {
  try { return typeof window !== 'undefined' && window.parent && window.parent !== window; } catch { return false; }
}

function post(name, payload = {}) {
  if (!inFrame()) return false;
  try {
    // targetOrigin is OUR origin: the message is never delivered to a parent
    // that is somebody else's page framing this one.
    window.parent.postMessage({ ...payload, type: `dispatch:${name}` }, location.origin);
    return true;
  } catch { return false; }
}

const handlers = new Set();
let messageWired = false;
function wireMessages() {
  if (messageWired || typeof window === 'undefined') return;
  messageWired = true;
  window.addEventListener('message', async (ev) => {
    // Only the shell that framed us, and only from our own origin.
    if (ev.origin !== location.origin) return;
    if (!inFrame() || ev.source !== window.parent) return;
    const msg = ev.data;
    if (!msg || typeof msg !== 'object' || typeof msg.type !== 'string' || !msg.type.startsWith('dispatch:')) return;
    if (msg.type === 'dispatch:theme') applyTheme(msg);
    // The shell switched language: load the new dictionaries BEFORE telling
    // the page, so a handler that re-renders reads the new strings.
    if (msg.type === 'dispatch:lang' && APP_ID) {
      const want = pickLang(msg.lang);
      if (!want || want === pageI18n.lang()) return;
      await ready(want);
    }
    for (const fn of handlers) { try { fn(msg); } catch (e) { console.error('[app-sdk] onMessage handler', e); } }
  });
}

/** Subscribe to messages from the shell: `dispatch:theme`, `dispatch:lang`
 *  (after this module has already switched the dictionaries) and
 *  `dispatch:frame` (a live `app:<id>:*` WebSocket frame). Returns an
 *  unsubscribe function. */
export function onMessage(fn) {
  wireMessages();
  handlers.add(fn);
  return () => handlers.delete(fn);
}

let pageWired = false;
function wirePage() {
  if (pageWired || !APP_ID) return;
  pageWired = true;
  wireMessages();
}

/** Ask the shell to close this app and open a chat thread. `hint` is a small
 *  JSON object handed to the thread's hook (e.g. {job_id}). */
export function openThread(threadId, hint) {
  if (!threadId) return false;
  const msg = { threadId: String(threadId) };
  if (hint && typeof hint === 'object') msg.hint = hint;
  return post('open-thread', msg);
}
/** Ask the shell to close this app's pane. */
export function close() { return post('close'); }
/** Ask the shell to show a toast. */
export function toast(text, isError = false) { return post('toast', { text: String(text || ''), error: !!isError }); }
/** Ask the shell to retitle this app's pane header. */
export function setTitle(text) { return post('set-title', { text: String(text || '') }); }

// ---------------------------------------------------------------------------
// Theme
// ---------------------------------------------------------------------------

// Ground → data-theme, as ui-theme.js derives it. Only used when the page is
// opened on its own (a new tab): framed, the shell's own attributes are copied.
const DATA_THEME = { oled: 'amoled', dark: 'dark', light: 'light' };
let groundBySlug = null;

function readShellTheme() {
  // Framed by the shell: copy exactly what its theme runtime stamped.
  if (inFrame()) {
    try {
      const root = window.parent.document.documentElement;
      const out = {};
      for (const a of THEME_ATTRS) out[a] = root.getAttribute(a);
      return out;
    } catch { /* not readable — fall through to storage */ }
  }
  let slug = null;
  try { slug = localStorage.getItem(PALETTE_KEY); } catch { /* blocked */ }
  slug = slug || DEFAULT_PALETTE;
  const ground = groundBySlug && groundBySlug[slug];
  return {
    // Purple is the :root base in ui-theme.css and carries no attribute.
    'data-palette': slug === 'purple' ? null : slug,
    'data-theme': DATA_THEME[ground] || 'dark',
    'data-contrast-profile': slug === 'night-red' ? 'night' : 'standard',
  };
}

/** Put a theme on this document. Accepts either the attribute map or the
 *  shell's `dispatch:theme` message ({palette, theme, contrast}). */
export function applyTheme(src) {
  if (typeof document === 'undefined' || !src) return;
  const attrs = 'palette' in src || 'theme' in src
    ? { 'data-palette': src.palette, 'data-theme': src.theme, 'data-contrast-profile': src.contrast }
    : src;
  const root = document.documentElement;
  for (const a of THEME_ATTRS) {
    const v = attrs[a];
    if (v == null || v === '') root.removeAttribute(a);
    else root.setAttribute(a, String(v));
  }
}

function ensureThemeLinks() {
  const head = document.head;
  if (!head) return;
  // Before the page's own stylesheets, so the app's rules win ties — the same
  // order index.html uses (tokens, then components).
  const first = head.querySelector('link[rel="stylesheet"]');
  for (const href of THEME_CSS) {
    if (head.querySelector(`link[data-app-sdk-theme="${href}"]`)) continue;
    const link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = href;
    link.dataset.appSdkTheme = href;
    if (first) head.insertBefore(link, first); else head.append(link);
  }
}

let themeWired = false;
/** Theme this page like the shell and keep it in step: a palette change in
 *  the shell arrives as a `storage` event (another same-origin document wrote
 *  the key) and as a `dispatch:theme` message. */
export function theme() {
  if (typeof document === 'undefined') return;
  ensureThemeLinks();
  applyTheme(readShellTheme());
  if (themeWired) return;
  themeWired = true;
  wireMessages();
  window.addEventListener('storage', (ev) => {
    if (ev.key === null || ev.key === PALETTE_KEY) applyTheme(readShellTheme());
  });
  if (!inFrame()) {
    // Standalone: learn each palette's ground so light palettes read light.
    fetch('/static/ui-theme/themes.json', { credentials: 'same-origin' })
      .then((r) => (r.ok ? r.json() : null))
      .then((j) => {
        if (!j || !Array.isArray(j.themes)) return;
        groundBySlug = Object.fromEntries(j.themes.map((x) => [x.slug, x.ground]));
        applyTheme(readShellTheme());
      })
      .catch(() => {});
  }
}

// Test hook: reset module state between jsdom cases.
export function _resetForTest() {
  handlers.clear();
  messageWired = false;
  pageWired = false;
  themeWired = false;
  groundBySlug = null;
}
