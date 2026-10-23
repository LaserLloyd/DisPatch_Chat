// DisPatch extension SDK — the shell object an installed add-on is handed.
//
// Contract: docs/extensions.md ("Frontend") and CONTRACTS §E.1. An extension
// (an installed Python package with its own frontend under /ext/<id>/, e.g.
// the DisPatch Voice add-on) renders INTO the shell's DOM, with the shell's
// classes, so it inherits every palette. It reaches the shell only through the
// object js/extensions.js passes to its `mount(shell, { assetsVersion })`:
//
//   EXT_API, version                        the handshake number, the app version
//   el, railIcon, RAIL_ICONS, loadStyle, acquireInert, releaseInert, isMobile
//   i18n: { t, tHtml, has, applyDom, list, n, lang }    shell + merged add-on strings
//   onLocale(fn) / onLocked(fn) / onThread(fn)          → unsubscribe
//   api(path, { method, body, query, signal })          same-origin JSON; DecoyError on 403
//   activeThread(), bots(), botById(id), allBots(), isLocked(), language(), features()
//   can(permission)                     does this session hold it (e.g. 'manage_plugins')?
//   threadFace(thread, bot) → { src, letter }           honours No-Image Mode
//   toast, confirm, prompt, navigate
//   chatHeader.addAction(node, { before })              → remove()
//   settings.pane(id, { render, onShow, onHide })       → dispose()
//       id 'voice' is the pre-declared Voice tab. Any other id matching
//       [a-z][a-z0-9-]{1,30} (not a built-in tab) makes the shell create a tab
//       button + pane for you, after Voice and before Plugins, titled by
//       `title` (text) or `titleKey` (a key in your merged locale bundle);
//       also `glyph` (a short emoji, default a puzzle piece) and
//       `public: true` (visible in Safe Mode too; default admin-only).
//       dispose() removes the tab again.
//   settings.open(id) / settings.close()
//   bots.rowExtra(fn(bot, onChange) → Node|null)        → dispose()
//   tools.register([{ id, feature, iconPaths, nameKey, ariaKey, open }]) → dispose()
//   tools.openPane({ id, title, subtitle, iconPaths, render, onClose })   → close()
//   history.push(tag, onBack)                           → pop()
//   activity()                                          the person is using the app
//                                                        (re-arms the idle auto-lock)
//
// Additive growth never bumps EXT_API (an extension feature-tests with
// `'x' in shell`); a removed or changed member does. Additions so far
// (CONTRACTS amendments T7a-2 / T1-4): `onThread` (the mic button follows the
// open chat; `onThreadChange` is the same function under its first name),
// `settings.close()`, `tools.openPane({ onClose })`, and history entries that
// stack — Settings opened over an extension's overlay takes its own Back.
// 2.2.0 (amendment FINAL-FIX-3): `activity()` — an extension whose use makes no
// touches (a voice chat) tells the shell the person is active, so the idle
// auto-lock does not lock the device mid-conversation. It only re-arms the
// timer the shell already runs: it never unlocks, never extends a lapsed server
// session, and does nothing on a locked or remembered device.
// 3.0 ops (2026-10-19): `can(permission)` — Members use voice now, so an add-on
// that used to assume "unlocked = may configure the server" asks instead (voice
// administration is `manage_plugins`; the server decides either way).
//
// main.js installs the implementations once (installShell). Each extension gets
// its OWN frozen shell (createShell(id)) so everything it registered can be
// torn down when it unmounts or fails, whatever it forgot to dispose itself.
// Pure module: nothing runs at import time (it is in the sw.js SHELL).

import { el, railIcon, RAIL_ICONS, loadStyle, acquireInert, releaseInert } from './util.js?v=21';
import { t, tHtml, has, applyDom, list, n, lang } from './i18n.js?v=4';
import { api, DecoyError } from './app-sdk.js?v=4';
import { APP_VERSION } from './about.js?v=23';

/** The extension API this shell speaks. Bump ONLY on a breaking change. */
export const EXT_API = 1;
// DEPRECATED alias kept for add-ons written against 3.x (never thrown in 4.0; see app-sdk.js).
export { DecoyError };

let impl = null;
const localeFns = new Set();
const lockedFns = new Set();
const threadFns = new Set();
/** extension id → [dispose, …] for everything it registered. */
const owned = new Map();

/** main.js: plug the shell's implementations in. */
export function installShell(implementation) {
  impl = implementation;
}

function call(name, ...args) {
  if (!impl || typeof impl[name] !== 'function') throw new Error(`shell.${name} is not available`);
  return impl[name](...args);
}

function subscribe(set, fn) {
  if (typeof fn !== 'function') return () => {};
  set.add(fn);
  return () => set.delete(fn);
}

function emit(set, arg) {
  for (const fn of [...set]) {
    try { fn(arg); } catch (e) { console.warn('[ext] listener failed', e); }
  }
}

/** Fired by js/extensions.js after the extensions' bundles were re-merged. */
export function emitLocale(langCode) { emit(localeFns, langCode); }
/** Fired by main.js when the device locks or logs out (Safe Mode). */
export function emitLocked() { emit(lockedFns); }
/** Fired by main.js when the open chat changes (or closes). */
export function emitThreadChange(thread) { emit(threadFns, thread || null); }

function track(owner, dispose) {
  if (!owner) return dispose;
  if (!owned.has(owner)) owned.set(owner, []);
  const list = owned.get(owner);
  let done = false;
  const once = () => {
    if (done) return;
    done = true;
    const i = list.indexOf(once);
    if (i >= 0) list.splice(i, 1);
    try { dispose(); } catch (e) { console.warn('[ext] dispose failed', e); }
  };
  list.push(once);
  return once;
}

/** Undo everything extension `owner` registered (unmount, lock, failure). */
export function disposeExtension(owner) {
  const list = owned.get(owner);
  owned.delete(owner);
  for (const fn of (list || []).slice().reverse()) fn();
}

/** The frozen shell for one extension (`owner` = its id; null = untracked).
 *  `bots` is both a getter (the roster) and a slot (`bots.rowExtra`) in the
 *  contract, so it is a function carrying a frozen `rowExtra` member. */
export function createShell(owner = null) {
  const own = (fn) => (...args) => track(owner, call(fn, owner, ...args));
  const listen = (set) => (fn) => track(owner, subscribe(set, fn));
  const bots = () => call('bots');
  bots.rowExtra = own('botRowExtra');
  Object.freeze(bots);
  return Object.freeze({
    EXT_API,
    version: APP_VERSION,
    // DOM / theme
    el, railIcon, RAIL_ICONS, loadStyle, acquireInert, releaseInert,
    isMobile: () => call('isMobile'),
    // i18n (the shell's strings plus every mounted extension's)
    i18n: Object.freeze({ t, tHtml, has, applyDom, list, n, lang }),
    onLocale: listen(localeFns),
    onLocked: listen(lockedFns),
    onThread: listen(threadFns),
    onThreadChange: listen(threadFns),
    // net
    api: (path, opts) => api(path, opts),
    // state (getters)
    activeThread: () => call('activeThread'),
    bots,
    botById: (id) => call('botById', id),
    allBots: () => call('allBots'),
    isLocked: () => call('isLocked'),
    can: (permission) => !!call('can', permission),
    language: () => lang(),
    features: () => call('features'),
    // media
    threadFace: (thread, bot) => call('threadFace', thread, bot),
    // UI
    toast: (text, isError = false) => call('toast', text, isError),
    confirm: (text) => call('confirm', text),
    prompt: (text, def = '') => call('prompt', text, def),
    navigate: (view) => call('navigate', view),
    // slots
    chatHeader: Object.freeze({ addAction: own('addHeaderAction') }),
    settings: Object.freeze({
      pane: own('settingsPane'),
      open: (id) => call('openSettings', id),
      close: () => call('closeSettings'),
    }),
    tools: Object.freeze({ register: own('registerTools'), openPane: own('openToolPane') }),
    history: Object.freeze({ push: own('historyPush') }),
    // activity
    activity: () => { if (impl && typeof impl.activity === 'function') impl.activity(); },
  });
}

/** An untracked shell (for an extension module that imports this file only
 *  for EXT_API, and for tests). js/extensions.js hands each extension its own
 *  createShell(id). */
export const shell = createShell(null);
