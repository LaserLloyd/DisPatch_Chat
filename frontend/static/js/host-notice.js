// "Stored on the host computer": what DisPatch keeps, and who can read it.
//
// Everything a person sends, uploads or makes here -- a chat, a file, a picture,
// an avatar -- is kept by the server on the computer that runs DisPatch, not on
// the device in their hand, and chats are not private. That is said in TWO
// places, and only two (2026-10-19: per-surface callouts were too loud):
//
//   * Settings -> Security: the full notice, permanent (static markup in
//     index.html, data-i18n host_notice.*);
//   * a one-time card on a device's first normal page load (after sign-in, or
//     the first time a Restricted / anonymous device opens DisPatch, and never
//     over the lock screen, setup or the first-run wizard). "Got it", a click
//     outside it or Escape dismisses it, and the dismissal is remembered on the
//     device under PRIVACY_ACK_KEY with PRIVACY_ACK_VERSION: bump the version
//     when the wording changes materially and every device sees it once more.
//
// The card is a non-modal region (no focus trap, no backdrop that blocks the
// app); the theme's danger colour is only its accent edge. Keep every claim
// TRUE for the code -- docs/security.md "What you are trusting".

import { t } from './i18n.js?v=4';
import { el } from './util.js?v=20';

export const PRIVACY_ACK_KEY = 'dispatch-privacy-ack';
export const PRIVACY_ACK_VERSION = 1;

/** Has this device dismissed the current card? Storage off = treat as seen on
 *  this page only (the card shows at most once per load either way). */
export function privacyAcked() {
  try { return Number(localStorage.getItem(PRIVACY_ACK_KEY)) >= PRIVACY_ACK_VERSION; } catch { return false; }
}

export function ackPrivacy() {
  try { localStorage.setItem(PRIVACY_ACK_KEY, String(PRIVACY_ACK_VERSION)); } catch { /* storage off */ }
}

let shownThisLoad = false;

/** Show the one-time card unless it was dismissed on this device (or already
 *  shown on this page). `ready()` says whether the app is on a normal page now
 *  (not the lock screen, setup or the wizard). Returns the card or null. */
export function maybeShowPrivacyCard({ ready = () => true, host = document.body } = {}) {
  if (shownThisLoad || privacyAcked() || !ready()) return null;
  shownThisLoad = true;
  const ok = el('button', { type: 'button', class: 'btn-primary privacy-card-ok', text: t('host_notice.card_ok') });
  const card = el('section', {
    class: 'privacy-card', role: 'dialog', 'aria-modal': 'false', 'aria-labelledby': 'privacy-card-title',
  }, [
    el('h2', { class: 'privacy-card-title', id: 'privacy-card-title', text: t('host_notice.card_title') }),
    el('ul', { class: 'privacy-card-points' }, [
      el('li', { text: t('host_notice.card_files') }),
      el('li', { text: t('host_notice.card_chats') }),
    ]),
    el('p', { class: 'privacy-card-more muted', text: t('host_notice.card_more') }),
    el('div', { class: 'privacy-card-foot' }, [ok]),
  ]);
  const done = () => {
    ackPrivacy();
    card.remove();
    document.removeEventListener('keydown', onKey, true);
    document.removeEventListener('pointerdown', onOutside, true);
  };
  const onKey = (e) => { if (e.key === 'Escape') { e.preventDefault(); done(); } };
  const onOutside = (e) => { if (!card.contains(e.target)) done(); };
  ok.addEventListener('click', done);
  document.addEventListener('keydown', onKey, true);
  // Pointer, not click: a click outside dismisses the card AND still does what
  // it was aimed at (it is a note, not a gate).
  document.addEventListener('pointerdown', onOutside, true);
  host.append(card);
  try { ok.focus({ preventScroll: true }); } catch { /* old engines */ }
  return card;
}

/** Test hook. */
export function _resetForTest() { shownThisLoad = false; }
