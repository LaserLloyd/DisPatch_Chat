// "Stored on the host computer": what DisPatch keeps, and who can read it.
//
// Everything a person sends, uploads or makes here -- a chat, a file, a picture,
// an avatar -- is kept by the server on the computer that runs DisPatch, not on
// the device in their hand, and chats are not private from that computer. That
// is said in TWO places:
//
//   * Settings -> Account: the full notice, permanent (static markup in
//     index.html, data-i18n host_notice.*);
//   * a sign-in notice: a popup that comes up after a sign-in, a switch or on
//     the first normal page, AT MOST ONCE A DAY ON A DEVICE (the owner, 2026-10-31:
//     once per sign-in was too much -- a remembered device switching profiles
//     saw it on every switch). It is never shown over the lock screen, the setup
//     screen or the first-run wizard. There is no permanent strip.
//
// "Once a day" is remembered per DEVICE in localStorage under DAY_KEY (the local
// date it was last shown, for any profile). The one exception is a tamper-evident
// PIN reset: a pin_reset notice this device has not shown yet (RESETS_KEY) brings
// the popup up even on a day it was already shown. armSignInNotice() (called at
// every sign-in and lock) only takes down a popup still on screen. Keep every
// claim TRUE for the code -- docs/security.md "What you are trusting".

import { t, n } from './i18n.js?v=5';
import { el, spriteIcon } from './util.js?v=23';

/** The line a non-Admin profile is told about what an Admin sees. The server sends a code
 *  (`disclosure_code`) mapped to a locale key here; an unknown code (a newer server) falls back
 *  to the server's English `disclosure`. An Admin gets neither: ''. */
export const DISCLOSURE_KEYS = Object.freeze({ activity_summary: 'people.disclosure_activity' });
export function disclosureText(d) {
  const key = d && typeof d.disclosure_code === 'string' ? DISCLOSURE_KEYS[d.disclosure_code] : null;
  if (key) return t(key);
  return d && typeof d.disclosure === 'string' ? d.disclosure : '';
}

/** Settings -> Account: show the disclosure under the host notice (`node` is the
 *  #host-notice-disclosure paragraph). A known code keeps a data-i18n key so a language
 *  change re-translates it; nothing to say (an Admin) hides it. */
export function paintDisclosure(node, d) {
  if (!node) return;
  const key = d && typeof d.disclosure_code === 'string' ? DISCLOSURE_KEYS[d.disclosure_code] : null;
  const text = disclosureText(d);
  if (key) node.setAttribute('data-i18n', key); else node.removeAttribute('data-i18n');
  node.textContent = text;
  node.hidden = !text;
}

/** localStorage: the local date ("YYYY-MM-DD") the popup was last shown on this device. */
export const DAY_KEY = 'dispatch-signin-notice-day';
/** localStorage: the PIN-reset notices ("<by>|<at>") this device has already shown. */
export const RESETS_KEY = 'dispatch-signin-notice-resets';

const memory = {};         // storage off: remember for this page only
let current = null;        // the popup on screen, if any

function readStore(k) {
  try { const v = localStorage.getItem(k); return v == null ? (memory[k] ?? null) : v; } catch { return memory[k] ?? null; }
}
function writeStore(k, v) {
  memory[k] = v;
  try { localStorage.setItem(k, v); } catch { /* storage off */ }
}
function localDay(d) {
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}
function seenResets() {
  try { const a = JSON.parse(readStore(RESETS_KEY) || '[]'); return Array.isArray(a) ? a : []; } catch { return []; }
}
const resetKey = (n) => `${n.by || ''}|${n.at || ''}`;

/** A sign-in (or a lock) just happened: take down a popup still on screen from the
 *  previous session. It does NOT re-arm the popup: that is once a day per device. */
export function armSignInNotice() {
  if (current) current.dismiss();
  dismissLandingBanner();   // 4.3 Fable: "why am I here" is answered once somebody signs in or out
}

/** Show the sign-in popup if this device has not shown it today (or has a PIN reset to tell). `ready()` says whether the app
 *  is on a normal page now (not the lock screen, setup or the wizard); `who`
 *  names the session ("<role>:<profile id>"); `shared` adds the shared-profile
 *  line; `notices` is the server's list (only kind "pin_reset" is shown);
 *  `disclosure` is the auth body's {disclosure, disclosure_code} (what an Admin
 *  can see about a non-Admin profile); `now` is the clock (tests). Returns the popup
 *  element or null. */
export function maybeShowSignInNotice({
  ready = () => true, who = 'session', shared = false, switchHint = false, notices = [], disclosure = null,
  host = null, now = () => new Date(),
} = {}) {
  void who;   // the rule is per device now; callers still name the session
  if (current || !ready()) return null;
  const resets = (Array.isArray(notices) ? notices : []).filter((n) => n && n.kind === 'pin_reset');
  const seen = seenResets();
  const fresh = resets.filter((n) => !seen.includes(resetKey(n)));
  const today = localDay(now());
  if (readStore(DAY_KEY) === today && !fresh.length) return null;
  writeStore(DAY_KEY, today);
  if (fresh.length) writeStore(RESETS_KEY, JSON.stringify([...seen, ...fresh.map(resetKey)].slice(-20)));

  const close = el('button', {
    type: 'button', class: 'privacy-card-close', 'aria-label': t('common.close'),
    title: t('common.close'),
  }, [spriteIcon('x', 16)]);
  const ok = el('button', { type: 'button', class: 'btn-primary privacy-card-ok', text: t('host_notice.card_ok') });
  const parts = [
    close,
    el('h2', { class: 'privacy-card-title', id: 'privacy-card-title', text: t('host_notice.card_title') }),
    el('ul', { class: 'privacy-card-points' }, [
      el('li', { text: t('host_notice.card_files') }),
      el('li', { text: t('host_notice.card_chats') }),
    ]),
  ];
  // On the shared profile the disclosure ("chats are private to this profile") is not true:
  // the shared line below says what is.
  const told = shared ? '' : disclosureText(disclosure);
  if (told) parts[2].append(el('li', { class: 'privacy-card-disclosure', text: told }));
  if (shared) parts.push(el('p', { class: 'privacy-card-shared', text: t('host_notice.shared') }));
  // 4.3 Fable: a fresh device on the shared profile is told how to get to its own profile
  // (the desktop chip alone is a small logo). Only where Switch profile is on the bar.
  if (shared && switchHint) parts.push(el('p', { class: 'privacy-card-shared', text: t('host_notice.shared_switch') }));
  for (const n of Array.isArray(notices) ? notices : []) {
    if (n && n.kind === 'pin_reset') {
      parts.push(el('p', { class: 'privacy-card-notice', text: t('host_notice.pin_reset', { by: n.by || '', at: n.at || '' }) }));
    }
  }
  parts.push(
    el('p', { class: 'privacy-card-more muted', text: t('host_notice.card_more') }),
    el('div', { class: 'privacy-card-foot' }, [ok]),
  );
  const card = el('section', {
    class: 'privacy-card', role: 'dialog', 'aria-modal': 'false', 'aria-labelledby': 'privacy-card-title',
  }, parts);

  function dismiss() {
    if (current !== handle) return;
    current = null;
    card.remove();
    document.removeEventListener('keydown', onKey, true);
    document.removeEventListener('pointerdown', onPointer, true);
  }
  const handle = { card, dismiss };
  const onKey = (e) => { if (e.key === 'Escape') { e.preventDefault(); dismiss(); } };
  // Pointer, not click, and capture phase: a press anywhere dismisses the popup
  // and the press still reaches whatever it was aimed at.
  const onPointer = () => dismiss();
  close.addEventListener('click', dismiss);
  ok.addEventListener('click', dismiss);
  document.addEventListener('keydown', onKey, true);
  document.addEventListener('pointerdown', onPointer, true);
  (host || noticeStack()).append(card);
  current = handle;
  try { ok.focus({ preventScroll: true }); } catch { /* old engines */ }
  return card;
}

// ---- The top stack: the cards that say who you are and what just happened ----------------
//
// The sign-in card, the landing banner and the password card all sit in one fixed column at
// the top of the screen, so they never cover each other or the phone's bottom row.
let stack = null;
export function noticeStack() {
  if (stack && stack.isConnected && stack.ownerDocument === document) return stack;
  stack = el('div', { class: 'notice-stack' });
  document.body.prepend(stack);   // early in the Tab order
  return stack;
}

function closeButton(onClick) {
  const b = el('button', {
    type: 'button', class: 'privacy-card-close', 'aria-label': t('common.close'), title: t('common.close'),
  }, [spriteIcon('x', 16)]);
  b.addEventListener('click', onClick);
  return b;
}

/** "1 hour", "10 minutes", "30 seconds" in the page's language (no locale keys needed). */
export function durationText(seconds) {
  const s = Math.max(0, Math.round(Number(seconds) || 0));
  const unit = (value, u) => n(value, { style: 'unit', unit: u, unitDisplay: 'long' });
  if (s >= 3600 && s % 3600 === 0) return unit(s / 3600, 'hour');
  if (s >= 60) return unit(Math.round(s / 60), 'minute');
  return unit(s, 'second');
}

/** Why this device just landed where it did, as one plain sentence (4.3 Fable).
 *  reason: 'idle' (no use for {seconds}), 'manual' (Sign out to <default>), 'lapsed' (the
 *  session ended on the server for another reason), 'default_disabled' (an Admin turned the
 *  shared profile off), 'default_changed' (the shared profile got a PIN or stopped being the
 *  default). `name` is the shared profile's name. */
export function landingText(reason, { name = '', seconds = 0 } = {}) {
  switch (reason) {
    case 'idle': return t('lock.idle_landed', { time: durationText(seconds), name });
    case 'default_disabled': return t('lock.default_disabled', { name });
    case 'default_changed': return t('lock.default_changed');
    default: return t('lock.landed', { name });
  }
}

let banner = null;
/** Take the landing banner down (a sign-in, or a newer one). */
export function dismissLandingBanner() {
  if (banner) banner.dismiss();
}

/** The landing banner: stays until the person dismisses it (✕, Got it, Escape inside it, or
 *  Switch profile when `onSwitch` is given). A press elsewhere leaves it up: it is the answer
 *  to "why am I on this profile?", not a passing toast. Returns the element. */
export function showLandingBanner({ text = '', onSwitch = null } = {}) {
  dismissLandingBanner();
  const foot = [];
  if (typeof onSwitch === 'function') {
    const sw = el('button', { type: 'button', class: 'btn-primary', text: t('chip.switch') });
    sw.addEventListener('click', () => { dismiss(); onSwitch(); });
    foot.push(sw);
  }
  const ok = el('button', { type: 'button', class: 'btn-secondary privacy-card-ok', text: t('host_notice.card_ok') });
  ok.addEventListener('click', () => dismiss());
  foot.push(ok);
  const card = el('section', { class: 'privacy-card landing-banner', role: 'status', 'aria-live': 'polite' }, [
    closeButton(() => dismiss()),
    el('p', { class: 'landing-banner-text', text }),
    el('div', { class: 'privacy-card-foot' }, foot),
  ]);
  card.addEventListener('keydown', (e) => { if (e.key === 'Escape') { e.preventDefault(); dismiss(); } });
  function dismiss() {
    if (banner !== handle) return;
    banner = null;
    card.remove();
  }
  const handle = { card, dismiss };
  noticeStack().prepend(card);
  banner = handle;
  return card;
}

/** localStorage: this device has shown the "set a password" card (once per device). */
export const PASSWORD_CARD_KEY = 'dispatch-password-card';
let pwCard = null;

/** The Owner signed in with the install PIN and has no password: say so ONCE on this device,
 *  with a button that does it (`onSet` opens Settings -> Account). Never over the sign-in
 *  screen (`ready()`). Returns the card or null. */
export function maybeShowPasswordCard({ ready = () => true, onSet = () => {} } = {}) {
  if (pwCard || !ready() || readStore(PASSWORD_CARD_KEY)) return null;
  writeStore(PASSWORD_CARD_KEY, '1');
  const set = el('button', { type: 'button', class: 'btn-primary', text: t('password.save') });
  const later = el('button', { type: 'button', class: 'btn-secondary privacy-card-later', text: t('setup.not_now') });
  const card = el('section', {
    class: 'privacy-card password-card', role: 'dialog', 'aria-modal': 'false', 'aria-labelledby': 'password-card-title',
  }, [
    closeButton(() => dismiss()),
    el('h2', { class: 'privacy-card-title', id: 'password-card-title', text: t('password.prompt_title') }),
    el('p', { text: t('password.prompt_body') }),
    el('div', { class: 'privacy-card-foot' }, [later, set]),
  ]);
  set.addEventListener('click', () => { dismiss(); onSet(); });
  later.addEventListener('click', () => dismiss());
  card.addEventListener('keydown', (e) => { if (e.key === 'Escape') { e.preventDefault(); dismiss(); } });
  function dismiss() {
    if (pwCard !== card) return;
    pwCard = null;
    card.remove();
  }
  noticeStack().append(card);
  pwCard = card;
  return card;
}

/** Test hook. */
export function _resetForTest() {
  if (current) current.dismiss();
  dismissLandingBanner();
  if (pwCard) { pwCard.remove(); pwCard = null; }
  for (const k of [DAY_KEY, RESETS_KEY, PASSWORD_CARD_KEY]) {
    delete memory[k];
    try { localStorage.removeItem(k); } catch { /* storage off */ }
  }
}
