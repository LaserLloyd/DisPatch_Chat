// "Stored on the host computer": what DisPatch keeps, and who can read it.
//
// Everything a person sends, uploads or makes here -- a chat, a file, a picture,
// an avatar -- is kept by the server on the computer that runs DisPatch, not on
// the device in their hand, and chats are not private from that computer. That
// is said in TWO places:
//
//   * Settings -> Account: the full notice, permanent (static markup in
//     index.html, data-i18n host_notice.*);
//   * a sign-in notice: a popup that comes up after EVERY sign-in (a PIN, a
//     password, a profile tile, recovery, the first Owner's setup), and on the
//     first normal page of a tab. It is never shown over the lock screen, the
//     setup screen or the first-run wizard. There is no permanent strip.
//
// The popup closes on a click or tap ANYWHERE, on the corner ✕, on "Got it" or
// with Escape. It is a note, not a gate: no backdrop, no focus trap, and a click
// outside it still does whatever it was aimed at. It says two things beyond the
// standing text: that the DisPatch profile is shared (when the person is signed
// into it), and any tamper-evident PIN reset that happened to this profile
// (notices[] from the server, kind "pin_reset").
//
// "Shown once per sign-in" is remembered per TAB in sessionStorage under
// SEEN_KEY ("<role>:<profile id>"): a reload of the same tab does not show it
// again, and armSignInNotice() (called at every sign-in and lock) forgets it, so
// the next normal page shows the popup once more. Keep every claim TRUE for the
// code -- docs/security.md "What you are trusting".

import { t } from './i18n.js?v=4';
import { el, spriteIcon } from './util.js?v=21';

/** sessionStorage: "<role>:<profile id>" the popup was last shown for in this tab. */
export const SEEN_KEY = 'dispatch-signin-notice';

let seenThisLoad = null;   // storage off: remember for this page only
let current = null;        // the popup on screen, if any

function readSeen() {
  try { return sessionStorage.getItem(SEEN_KEY); } catch { return seenThisLoad; }
}
function writeSeen(v) {
  seenThisLoad = v;
  try {
    if (v == null) sessionStorage.removeItem(SEEN_KEY); else sessionStorage.setItem(SEEN_KEY, v);
  } catch { /* storage off */ }
}

/** A sign-in (or a lock) just happened: the next normal page shows the popup
 *  again. Also takes down a popup still on screen from the previous session. */
export function armSignInNotice() {
  writeSeen(null);
  if (current) current.dismiss();
}

/** Show the sign-in popup once for this sign-in. `ready()` says whether the app
 *  is on a normal page now (not the lock screen, setup or the wizard); `who`
 *  names the session ("<role>:<profile id>"); `shared` adds the shared-profile
 *  line; `notices` is the server's list (only kind "pin_reset" is shown).
 *  Returns the popup element or null. */
export function maybeShowSignInNotice({
  ready = () => true, who = 'session', shared = false, notices = [], host = document.body,
} = {}) {
  if (current || !ready() || readSeen() === who) return null;
  writeSeen(who);

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
  if (shared) parts.push(el('p', { class: 'privacy-card-shared', text: t('host_notice.shared') }));
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
  host.append(card);
  current = handle;
  try { ok.focus({ preventScroll: true }); } catch { /* old engines */ }
  return card;
}

/** Test hook. */
export function _resetForTest() {
  if (current) current.dismiss();
  seenThisLoad = null;
  try { sessionStorage.removeItem(SEEN_KEY); } catch { /* storage off */ }
}
