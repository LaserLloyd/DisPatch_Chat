// DisPatch Chat — internationalization runtime.
//
// Dependency-free ES module. No build step: locale data is plain JSON fetched
// at runtime from /static/locales/<lang>.json, and every formatting decision
// is delegated to the platform `Intl` object rather than hand-rolled tables.
//
// Design notes (the "why", so nobody has to re-derive it):
//
//  * ENGLISH IS ALWAYS LOADED. en.json is fetched alongside the active locale
//    and kept as the fallback dictionary, so a missing key in (say) ar.json
//    renders the English string rather than a blank node or a raw key. If even
//    en.json fails to load, t() falls back to CRITICAL_EN and then to a
//    humanized form of the key — the UI degrades, it never goes blank. The
//    static markup in index.html is itself English, and with NO dictionary at
//    all applyDom() declines to run (see the guard there), so a total locale
//    failure leaves the original English page exactly as it was rather than
//    stamping key tails over it. `localeLoadFailed()` reports that state so the
//    app can say so out loud instead of degrading silently.
//
//  * NO FLASH OF UNTRANSLATED CONTENT. bootI18n() runs at module-evaluation
//    time (before any rendering) and synchronously stamps <html lang>/<html dir>
//    from localStorage, exactly like the existing no-FOUC theme script. The
//    dictionary fetch is awaited by init() before the app's first render, and
//    the existing boot veil covers that window. init() is time-boxed so a hung
//    request can never hold the veil hostage.
//
//  * PLURALS use Intl.PluralRules. A translatable value may be a plain string
//    or an object whose keys are all CLDR plural categories
//    (zero/one/two/few/many/other) — the object form is selected by `count`.
//    Arabic gets all six categories, Japanese and Chinese get only `other`;
//    neither the runtime nor the call sites need to know which.
//
//  * NUMBERS AND DATES are formatted with Intl using the locale's own
//    "$meta.locale" BCP-47 tag, which lets a locale opt into a numbering
//    system (ar uses `-u-nu-latn` so digits stay Western Arabic, matching the
//    PIN keypad and the model/version strings the app displays).
//
//  * RTL is a first-class mode, not a skin. setLocale() sets <html dir> and a
//    `data-dir` attribute; CSS does the rest via logical properties (see
//    docs/i18n.md — RTL rules).

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

const LOCALE_URL = (lang) => `/static/locales/${lang}.json`;
const STORAGE_KEY = 'dispatch-lang';
const DEFAULT_LANG = 'en';
const FETCH_TIMEOUT_MS = 4000;

// Shipped languages, in the order a picker should show them. `native` is what
// the language calls itself — always show a language in its own script.
export const LANGUAGES = [
  { code: 'en', native: 'English',  english: 'English',              dir: 'ltr' },
  { code: 'ar', native: 'العربية',   english: 'Arabic',               dir: 'rtl' },
  { code: 'de', native: 'Deutsch',  english: 'German',               dir: 'ltr' },
  { code: 'es', native: 'Español',  english: 'Spanish',              dir: 'ltr' },
  { code: 'fr', native: 'Français', english: 'French',               dir: 'ltr' },
  { code: 'ja', native: '日本語',     english: 'Japanese',             dir: 'ltr' },
  { code: 'pt', native: 'Português', english: 'Portuguese',          dir: 'ltr' },
  { code: 'zh', native: '简体中文',    english: 'Chinese (Simplified)', dir: 'ltr' },
];

const SUPPORTED = new Set(LANGUAGES.map((l) => l.code));

// Scripts that run right-to-left. Only `ar` ships today, but the set is the
// honest boundary — adding he/fa/ur later needs no code change here.
const RTL_LANGS = new Set(['ar', 'he', 'fa', 'ur', 'yi', 'dv', 'ckb']);

// CLDR cardinal plural categories. An object whose keys are ALL drawn from this
// set (and which contains `other`) is a plural form, not a namespace.
const PLURAL_CATEGORIES = new Set(['zero', 'one', 'two', 'few', 'many', 'other']);

// Absolute last-ditch strings, used only if en.json itself cannot be loaded
// (offline first visit with no service worker, or a broken deploy). Deliberately
// tiny — just enough that the failure is legible instead of silent.
const CRITICAL_EN = {
  'toast.offline': 'Not connected — reconnecting…',
  'conn.reconnecting': 'Reconnecting…',
  'composer.send': 'Send',
  'composer.placeholder': 'Type a message…',
  // Both of these are written by JS OVER shipped English markup (the composer
  // placeholder, the chat header) the moment the app boots, so leaving them out
  // would have re-opened the hole applyDom's guard just closed — from the other
  // side. Anything else JS builds from scratch degrades to a humanized key,
  // which is ugly but is not overwriting something better.
  'composer.placeholder_no_thread': 'Pick a chat to start typing',
  'chat.select': 'Select a chat',
  'common.cancel': 'Cancel',
  'common.ok': 'OK',
  'common.save': 'Save',
  'common.close': 'Close',
  'common.done': 'Done',
  'common.delete': 'Delete',
  'common.retry': 'Retry',
  'error.locale_load': 'Could not load translations — showing English.',
};

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

const state = {
  lang: DEFAULT_LANG,       // active language code, e.g. 'ar'
  tag: DEFAULT_LANG,        // BCP-47 tag handed to Intl, e.g. 'ar-u-nu-latn'
  dir: 'ltr',
  dict: null,               // active locale dictionary (null until loaded)
  fallback: null,           // en.json dictionary
  ready: false,
  loadFailed: false,        // true when NEITHER the locale nor en.json loaded
  warned: new Set(),        // keys already warned about (dev noise control)
};

const cache = new Map();    // lang -> dictionary (avoids refetching on switch)
const listeners = new Set();

// Intl instances are expensive to construct; build once per locale+shape.
const intlCache = new Map();
function intl(kind, options) {
  const key = `${kind}|${state.tag}|${JSON.stringify(options || {})}`;
  let inst = intlCache.get(key);
  if (inst) return inst;
  try {
    if (kind === 'number') inst = new Intl.NumberFormat(state.tag, options);
    else if (kind === 'date') inst = new Intl.DateTimeFormat(state.tag, options);
    else if (kind === 'rel') inst = new Intl.RelativeTimeFormat(state.tag, options);
    else if (kind === 'plural') inst = new Intl.PluralRules(state.tag, options);
    else if (kind === 'list') inst = new Intl.ListFormat(state.tag, options);
  } catch {
    // A bad/unsupported tag must never break rendering — fall back to English.
    if (kind === 'number') inst = new Intl.NumberFormat('en', options);
    else if (kind === 'date') inst = new Intl.DateTimeFormat('en', options);
    else if (kind === 'rel') inst = new Intl.RelativeTimeFormat('en', options);
    else if (kind === 'plural') inst = new Intl.PluralRules('en', options);
    else inst = { format: (v) => String(v) };
  }
  intlCache.set(key, inst);
  return inst;
}

// ---------------------------------------------------------------------------
// Language negotiation
// ---------------------------------------------------------------------------

function normalize(tag) {
  if (!tag) return null;
  const base = String(tag).toLowerCase().split(/[-_]/)[0];
  return SUPPORTED.has(base) ? base : null;
}

function stored() {
  try {
    const v = localStorage.getItem(STORAGE_KEY);
    return v && SUPPORTED.has(v) ? v : null;
  } catch { return null; }
}

// First run: honour the browser's ordered preference list, matching on the base
// language so pt-BR → pt, zh-Hans-CN → zh, en-GB → en.
function detect() {
  const prefs = (navigator.languages && navigator.languages.length)
    ? navigator.languages
    : [navigator.language || DEFAULT_LANG];
  for (const p of prefs) {
    const m = normalize(p);
    if (m) return m;
  }
  return DEFAULT_LANG;
}

export function resolveLang() { return stored() || detect(); }

export function isRtl(lang = state.lang) { return RTL_LANGS.has(lang); }

// ---------------------------------------------------------------------------
// Boot: runs at import time, before anything renders.
// ---------------------------------------------------------------------------

// Stamps <html lang>/<html dir> synchronously so the very first paint is
// already laid out in the right direction. Safe to call more than once.
export function bootI18n() {
  const lang = resolveLang();
  state.lang = lang;
  state.tag = lang;
  state.dir = isRtl(lang) ? 'rtl' : 'ltr';
  const html = document.documentElement;
  html.setAttribute('lang', lang);
  html.setAttribute('dir', state.dir);
  html.setAttribute('data-dir', state.dir);
  return lang;
}

// Guarded so this module (and everything importing it) can be loaded under
// plain node by the test suite. In a browser `document` always exists, so the
// synchronous no-FOUC stamp above is unchanged.
if (typeof document !== 'undefined') bootI18n();

// ---------------------------------------------------------------------------
// Dictionary loading
// ---------------------------------------------------------------------------

async function fetchLocale(lang) {
  if (cache.has(lang)) return cache.get(lang);
  const ctrl = typeof AbortController !== 'undefined' ? new AbortController() : null;
  const timer = ctrl ? setTimeout(() => ctrl.abort(), FETCH_TIMEOUT_MS) : null;
  try {
    const r = await fetch(LOCALE_URL(lang), {
      credentials: 'same-origin',
      signal: ctrl ? ctrl.signal : undefined,
    });
    if (!r.ok) throw new Error(`${r.status}`);
    const data = await r.json();
    if (!data || typeof data !== 'object') throw new Error('not an object');
    cache.set(lang, data);
    return data;
  } catch (e) {
    console.warn(`[i18n] could not load locale "${lang}":`, e && e.message);
    return null;
  } finally {
    if (timer) clearTimeout(timer);
  }
}

// Load English (always, as the fallback) plus the target locale, in parallel.
async function loadDictionaries(lang) {
  if (lang === DEFAULT_LANG) {
    const en = await fetchLocale(DEFAULT_LANG);
    return { dict: en, fallback: en };
  }
  const [target, en] = await Promise.all([fetchLocale(lang), fetchLocale(DEFAULT_LANG)]);
  return { dict: target || en, fallback: en };
}

function applyMeta(dict, lang) {
  const meta = (dict && dict.$meta) || {};
  state.tag = typeof meta.locale === 'string' && meta.locale ? meta.locale : lang;
  state.dir = meta.dir === 'rtl' || isRtl(lang) ? 'rtl' : 'ltr';
  intlCache.clear();
}

/**
 * Boot the i18n system. Call once, and AWAIT it before the app's first render.
 * Resolves to the active language code.
 */
export async function init(preferred) {
  const lang = (preferred && SUPPORTED.has(preferred)) ? preferred : resolveLang();
  const { dict, fallback } = await loadDictionaries(lang);
  state.lang = lang;
  state.dict = dict;
  state.fallback = fallback;
  state.ready = true;
  noteLoadFailure();
  applyMeta(dict, lang);
  stamp();
  applyDom(document);
  return lang;
}

/**
 * True when NEITHER the requested locale NOR en.json could be fetched — the
 * total-failure case (offline first visit, dead deploy, a 4s timeout on a slow
 * LAN). The app keeps working in its shipped English, but the language the user
 * picked silently did not happen, so the caller is expected to say so once boot
 * is far enough along to show a toast.
 */
export function localeLoadFailed() { return state.loadFailed; }

// Record + announce a total dictionary failure. CRITICAL_EN carries the string
// for exactly this moment: t('error.locale_load') would resolve it too, but the
// console line has to work even if this module is the thing that is broken.
function noteLoadFailure() {
  state.loadFailed = !state.dict && !state.fallback;
  if (state.loadFailed) console.warn(`[i18n] ${CRITICAL_EN['error.locale_load']}`);
}

/**
 * Switch language at runtime. Persists the choice, re-fetches if needed,
 * re-applies the static DOM, and notifies listeners so the app can re-render
 * its dynamically-built views.
 */
export async function setLocale(lang) {
  if (!SUPPORTED.has(lang)) return state.lang;
  if (lang === state.lang && state.ready) return lang;
  const { dict, fallback } = await loadDictionaries(lang);
  state.lang = lang;
  state.dict = dict;
  state.fallback = fallback || state.fallback;
  state.ready = true;
  noteLoadFailure();
  try { localStorage.setItem(STORAGE_KEY, lang); } catch { /* private mode */ }
  applyMeta(dict, lang);
  stamp();
  applyDom(document);
  emit();
  return lang;
}

function stamp() {
  const html = document.documentElement;
  html.setAttribute('lang', state.lang);
  html.setAttribute('dir', state.dir);
  html.setAttribute('data-dir', state.dir);
  // Keep the installed-PWA manifest hint in step for the next launch.
  const m = document.querySelector('meta[name="i18n-lang"]');
  if (m) m.setAttribute('content', state.lang);
}

function emit() {
  const detail = { lang: state.lang, dir: state.dir, tag: state.tag };
  for (const fn of listeners) { try { fn(detail); } catch { /* listener's problem */ } }
  try {
    document.dispatchEvent(new CustomEvent('i18n:change', { detail }));
  } catch { /* very old browser */ }
}

/** Subscribe to language changes. Returns an unsubscribe function. */
export function onChange(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

// ---------------------------------------------------------------------------
// Lookup + translation
// ---------------------------------------------------------------------------

function walk(dict, key) {
  if (!dict) return undefined;
  // Flat key wins (locale files may use either shape; ours are nested).
  if (Object.prototype.hasOwnProperty.call(dict, key)) return dict[key];
  let node = dict;
  for (const part of key.split('.')) {
    if (node == null || typeof node !== 'object') return undefined;
    node = node[part];
  }
  return node;
}

function isPluralForm(v) {
  if (!v || typeof v !== 'object' || Array.isArray(v)) return false;
  const keys = Object.keys(v);
  if (!keys.length || !keys.includes('other')) return false;
  return keys.every((k) => PLURAL_CATEGORIES.has(k));
}

// Turn "files.wipe_confirm" into "Wipe confirm" — an ugly but readable last
// resort that is still obviously a missing string rather than an empty box.
function humanize(key) {
  const tail = String(key).split('.').pop() || String(key);
  const words = tail.replace(/[_-]+/g, ' ').trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

function pick(value, vars) {
  if (isPluralForm(value)) {
    const n = Number(vars && vars.count);
    if (!Number.isFinite(n)) return value.other;
    // Exact-value overrides ("=0" style) are handled by the `zero` category
    // where CLDR defines one; otherwise a literal 0 falls through to `other`
    // for languages that have no zero rule, which is correct.
    const cat = intl('plural').select(Math.abs(n));
    return value[cat] != null ? value[cat] : value.other;
  }
  return value;
}

function interpolate(str, vars) {
  if (!vars || typeof str !== 'string') return str;
  return str.replace(/\{(\w+)\}/g, (whole, name) => {
    if (!Object.prototype.hasOwnProperty.call(vars, name)) return whole;
    const v = vars[name];
    if (v == null) return '';
    // Numbers get locale digit grouping/decimals for free. Everything else is
    // inserted verbatim; callers assign via textContent, so no escaping here.
    return typeof v === 'number' && Number.isFinite(v) ? intl('number').format(v) : String(v);
  }).replace(/#/g, () => (
    vars && typeof vars.count === 'number' ? intl('number').format(vars.count) : '#'
  ));
}

/**
 * Translate a key.
 *   t('composer.send')
 *   t('files.uploaded', { count: 3 })
 *   t('chat.with_bot', { name: 'Nova' })
 *
 * `{name}` placeholders interpolate from `vars`; `#` expands to the formatted
 * `count`. Plural objects are selected via Intl.PluralRules.
 */
export function t(key, vars) {
  if (!key) return '';
  let raw = pick(walk(state.dict, key), vars);
  if (raw == null) raw = pick(walk(state.fallback, key), vars);
  if (raw == null) raw = CRITICAL_EN[key];
  if (raw == null) {
    if (!state.warned.has(key)) {
      state.warned.add(key);
      console.warn(`[i18n] missing key: ${key}`);
    }
    raw = humanize(key);
  }
  if (typeof raw !== 'string') return String(raw);
  return interpolate(raw, vars);
}

/** True when a key exists in the active locale (not just the fallback). */
export function has(key) {
  return walk(state.dict, key) != null;
}

/** Translate a key whose value carries markup WE own, for an innerHTML sink.
 *
 *  The locale string itself is trusted (it ships in this repo and
 *  scripts/check-locales.py holds it to a tag allowlist). The interpolated
 *  VARS are not: they are app state, and some of them are server-supplied —
 *  a recovery-file path, a bot name, an error string. t() inserts them
 *  verbatim by design (every other call site assigns through textContent), so
 *  the HTML path has to escape them, and it has to do it HERE rather than at
 *  each call site: applyDom() re-renders [data-i18n-html] from the stored
 *  data-i18n-vars on every language switch, so a caller that escaped its own
 *  value would be correct once and unescaped forever after.
 *
 *  Numbers pass through unescaped on purpose — they go to Intl for grouping
 *  and cannot contain markup.
 */
export function tHtml(key, vars) {
  if (!vars) return t(key);
  const safe = {};
  for (const [k, v] of Object.entries(vars)) {
    safe[k] = (typeof v === 'number' && Number.isFinite(v)) ? v : escapeForHtml(v);
  }
  return t(key, safe);
}

function escapeForHtml(v) {
  if (v == null) return v;
  return String(v).replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

// ---------------------------------------------------------------------------
// Static DOM pass
// ---------------------------------------------------------------------------
//
//   <h2 data-i18n="settings.title">Settings</h2>
//   <button data-i18n-attr="title=nav.lock;aria-label=nav.lock">🔒</button>
//   <input data-i18n-attr="placeholder=search.placeholder" />
//   <p data-i18n-html="settings.hint">…markup we control…</p>
//   <span data-i18n="files.count" data-i18n-vars='{"count":3}'></span>
//
// `data-i18n` writes textContent, so put it on a LEAF element. When a string
// carries inline markup (<strong>, <code>, <br>), use `data-i18n-html` — those
// values come from our own locale files, never from user input, and
// scripts/check-locales.py restricts them to a safe tag allowlist.

const ATTR_SEP = /\s*[;,]\s*/;

function nodeVars(node) {
  const raw = node.getAttribute('data-i18n-vars');
  if (!raw) return undefined;
  try { return JSON.parse(raw); } catch { return undefined; }
}

/**
 * True once at least one dictionary (the locale's or English) is in memory.
 * Callers that translate a subtree OUTSIDE the normal boot order — theme.js
 * runs from <head>, before init() has fetched anything — check this so they
 * don't paint humanized keys over perfectly good markup.
 */
export function hasDictionary() { return !!(state.dict || state.fallback); }

/** Re-translate every tagged node under `root` (defaults to the document). */
export function applyDom(root = document) {
  // TOTAL FAILURE = LEAVE THE PAGE ALONE. With no dictionary at all, t() falls
  // through to humanize(), and this pass would overwrite all ~170 tagged nodes
  // with key tails — "Title", "Note", "Unlock title" — i.e. it would replace
  // the shipped English markup with something strictly worse. The header
  // comment on this file promises the opposite ("a total locale failure leaves
  // the original English page exactly as it was"), and declining to run is the
  // only way to keep that promise. Also covers the pre-init window: any module
  // that calls applyDom() before init() resolves is now a safe no-op.
  if (!hasDictionary()) return;
  const scope = root && root.querySelectorAll ? root : document;

  // querySelectorAll only sees DESCENDANTS, so a caller that hands us a single
  // tagged element ("translate just this button") would silently get nothing
  // done. "Under `root`" means root included — match it first, then its subtree.
  const tagged = (sel) => {
    const own = scope.matches && scope.matches(sel) ? [scope] : [];
    return own.concat(Array.from(scope.querySelectorAll(sel)));
  };

  tagged('[data-i18n]').forEach((node) => {
    node.textContent = t(node.getAttribute('data-i18n'), nodeVars(node));
  });

  tagged('[data-i18n-html]').forEach((node) => {
    node.innerHTML = tHtml(node.getAttribute('data-i18n-html'), nodeVars(node));
  });

  tagged('[data-i18n-attr]').forEach((node) => {
    const spec = node.getAttribute('data-i18n-attr') || '';
    const vars = nodeVars(node);
    for (const pair of spec.split(ATTR_SEP)) {
      if (!pair) continue;
      const eq = pair.indexOf('=');
      if (eq < 0) continue;
      const attr = pair.slice(0, eq).trim();
      const key = pair.slice(eq + 1).trim();
      if (!attr || !key) continue;
      node.setAttribute(attr, t(key, vars));
    }
  });

  // <title> is not reachable by the selectors above (it has no attributes we
  // want to litter), so it is handled by convention.
  const title = document.querySelector('title[data-i18n]');
  if (title) document.title = title.textContent;
}

/**
 * Translate a subtree that was just built by JS (same attribute contract).
 * Useful when a renderer clones a template rather than calling t() inline.
 */
export function applyTo(node) { return applyDom(node); }

// ---------------------------------------------------------------------------
// Formatters (locale-aware replacements for the hand-rolled util.js helpers)
// ---------------------------------------------------------------------------

/** Format a number: 1234567 → "1,234,567" / "1.234.567" / "١٬٢٣٤٬٥٦٧". */
export function n(value, options) {
  const num = Number(value);
  if (!Number.isFinite(num)) return '';
  return intl('number', options).format(num);
}

/** "42%" with the locale's own percent conventions. `value` is 0..100. */
export function percent(value) {
  const num = Number(value);
  if (!Number.isFinite(num)) return '';
  return intl('number', { style: 'percent', maximumFractionDigits: 0 }).format(num / 100);
}

function toDate(iso) {
  if (!iso) return null;
  const d = iso instanceof Date ? iso : new Date(iso);
  return isNaN(d.getTime()) ? null : d;
}

/** "2:34 PM" (en) / "14:34" (de, fr, ja) — the locale decides 12h vs 24h. */
export function clockTime(iso) {
  const d = toDate(iso);
  if (!d) return '';
  return intl('date', { hour: 'numeric', minute: '2-digit' }).format(d);
}

/** "June 10, 2026" / "10. Juni 2026" / "2026年6月10日", with Today/Yesterday. */
export function dayLabel(iso) {
  const d = toDate(iso);
  if (!d) return '';
  const today = new Date();
  const yest = new Date(); yest.setDate(today.getDate() - 1);
  const same = (a, b) => a.toDateString() === b.toDateString();
  if (same(d, today)) return t('time.today');
  if (same(d, yest)) return t('time.yesterday');
  return intl('date', { year: 'numeric', month: 'long', day: 'numeric' }).format(d);
}

/** Stable per-day grouping key. Locale-independent on purpose. */
export function dayKey(iso) {
  const d = toDate(iso);
  return d ? d.toDateString() : '';
}

/**
 * Compact relative time for the thread list: "now" / "5m" / "3h" / "Yesterday"
 * / "4d" / "Jun 4". The compact unit labels are translatable (time.short.*)
 * because Intl has no genuinely compact cardinal form; anything older than a
 * week uses Intl.DateTimeFormat.
 */
export function relTime(iso) {
  const d = toDate(iso);
  if (!d) return '';
  const mins = Math.floor((Date.now() - d.getTime()) / 60000);
  if (mins < 1) return t('time.now');
  if (mins < 60) return t('time.short.minutes', { count: mins });
  const hours = Math.floor(mins / 60);
  if (hours < 24) return t('time.short.hours', { count: hours });
  const days = Math.floor(hours / 24);
  if (days === 1) return t('time.yesterday');
  if (days < 7) return t('time.short.days', { count: days });
  return intl('date', { month: 'short', day: 'numeric' }).format(d);
}

/**
 * Full relative phrasing for tooltips and screen readers: "5 minutes ago",
 * "il y a 5 minutes", "قبل ٥ دقائق". Uses Intl.RelativeTimeFormat, so no
 * translation work is needed for it at all.
 */
export function relTimeLong(iso) {
  const d = toDate(iso);
  if (!d) return '';
  const secs = Math.round((d.getTime() - Date.now()) / 1000);
  const abs = Math.abs(secs);
  const rtf = intl('rel', { numeric: 'auto' });
  if (abs < 60) return rtf.format(Math.round(secs), 'second');
  if (abs < 3600) return rtf.format(Math.round(secs / 60), 'minute');
  if (abs < 86400) return rtf.format(Math.round(secs / 3600), 'hour');
  if (abs < 604800) return rtf.format(Math.round(secs / 86400), 'day');
  if (abs < 2629800) return rtf.format(Math.round(secs / 604800), 'week');
  if (abs < 31557600) return rtf.format(Math.round(secs / 2629800), 'month');
  return rtf.format(Math.round(secs / 31557600), 'year');
}

/** Absolute timestamp for title attributes: "10 June 2026 at 14:34". */
export function fullTimestamp(iso) {
  const d = toDate(iso);
  if (!d) return '';
  return intl('date', { dateStyle: 'medium', timeStyle: 'short' }).format(d);
}

/** "6.2 MB" with localized digits/separators and translatable unit labels. */
export function fileSize(bytes) {
  const b = Number(bytes);
  if (!Number.isFinite(b)) return '';
  if (b < 1024) return t('unit.bytes', { count: b, size: n(b) });
  const units = ['kb', 'mb', 'gb', 'tb', 'pb'];
  let v = b;
  for (const u of units) {
    v /= 1024;
    if (v < 1024 || u === 'pb') {
      const rounded = v < 10 ? Math.round(v * 10) / 10 : Math.round(v);
      return t(`unit.${u}`, {
        size: n(rounded, { maximumFractionDigits: v < 10 ? 1 : 0 }),
      });
    }
  }
  return '';
}

/** "a, b and c" — used for option summaries. */
export function list(items, type = 'conjunction') {
  const arr = (items || []).filter(Boolean).map(String);
  if (!arr.length) return '';
  try { return intl('list', { style: 'long', type }).format(arr); }
  catch { return arr.join(', '); }
}

// ---------------------------------------------------------------------------
// UI helper
// ---------------------------------------------------------------------------

/**
 * Build a ready-to-use <select> for switching language. The caller owns
 * placement and styling; changing it calls setLocale().
 */
export function languageSelect({ className = 'lang-select', id } = {}) {
  const sel = document.createElement('select');
  if (id) sel.id = id;
  sel.className = className;
  sel.setAttribute('aria-label', t('settings.language'));
  for (const l of LANGUAGES) {
    const opt = document.createElement('option');
    opt.value = l.code;
    // Native name first (a Japanese speaker looks for 日本語, not "Japanese"),
    // English name in parentheses so a lost user can still find their way back.
    opt.textContent = l.code === state.lang ? l.native : `${l.native} (${l.english})`;
    if (l.code === state.lang) opt.selected = true;
    sel.append(opt);
  }
  sel.addEventListener('change', () => { setLocale(sel.value); });
  return sel;
}

// ---------------------------------------------------------------------------
// Introspection
// ---------------------------------------------------------------------------

export const i18n = {
  t, has, init, setLocale, applyDom, applyTo, onChange, languageSelect,
  n, percent, clockTime, dayLabel, dayKey, relTime, relTimeLong, fullTimestamp,
  fileSize, list, isRtl, resolveLang, hasDictionary, localeLoadFailed, LANGUAGES,
  get lang() { return state.lang; },
  get dir() { return state.dir; },
  get tag() { return state.tag; },
  get ready() { return state.ready; },
};

export default i18n;
