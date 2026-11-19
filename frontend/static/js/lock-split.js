// The lock screen of a device that holds a locked session (docs/design/lock-resume.md).
//
//   TOP     the main profile (the lock-target default the device falls back to): its picture
//           and name, one tap goes there. The ONLY identity on the screen. An install with
//           no default profile shows "Choose a profile" instead.
//   BOTTOM  "Resume previous session": one neutral, masked field that takes the on-screen
//           pad AND typed text (a password goes in the same field), and a Resume button.
//           Nothing here may say or hint whose session it is: no name, picture, initials,
//           colour, length hint, profile theme; the server tells this page one bit
//           (resumable) and never whose.
//
// A wrong secret goes to main (main.js closes this screen); the pad never says "try again".
// The secret is sent to the server once and never kept: the field is wiped on every exit.
// No network and no state here: main.js passes the callbacks.

import { t } from './i18n.js?v=5';
import { el } from './util.js?v=23';
import { nimEnabled } from './nim.js?v=8';
import { avatarSpec, markNode } from './profiles.js?v=6';

const KEYS = ['1', '2', '3', '4', '5', '6', '7', '8', '9', 'abc', '0', 'back'];
/** One cap for every profile kind, so the field itself says nothing (a password fits). */
export const SECRET_MAX = 256;

function mainAvatar(profile) {
  const spec = avatarSpec(profile, { noImages: nimEnabled() });
  if (spec.kind === 'img') {
    return el('img', { class: 'lks-avatar', src: spec.src, alt: '', width: '96', height: '96', draggable: 'false' });
  }
  if (spec.kind === 'mark') return markNode('lks-avatar');
  return el('span', { class: 'lks-avatar lks-avatar--text', 'aria-hidden': 'true', text: spec.text });
}

/**
 * Fill `host` with the split screen. Returns a controller {destroy, focus, field}.
 *   main       the lock-target default profile {name, avatar, ...} or null (profile list)
 *   resumable  show the bottom half
 *   onMain     () -> go to main (or the profile chooser when main is null)
 *   onOthers   () -> the normal sign-in tiles (someone else)
 *   onResume   async (secret) -> true (resumed) | false (wrong / refused: go to main)
 */
export function renderLockSplit(host, { main = null, resumable = false, onMain, onOthers, onResume }) {
  host.replaceChildren();
  let busy = false;

  const top = el('section', { class: 'lks-top', 'aria-labelledby': 'lks-main-label' });
  const go = el('button', { type: 'button', class: 'lks-main', id: 'lks-main' });
  if (main) {
    go.append(mainAvatar(main), el('span', { class: 'lks-main-name', id: 'lks-main-label', text: main.name || '' }),
      el('span', { class: 'lks-main-go', text: t('lockr.go_main') }));
  } else {
    go.append(el('span', { class: 'lks-main-name', id: 'lks-main-label', text: t('lockr.choose') }));
  }
  go.addEventListener('click', () => { if (!busy) { wipe(); onMain(); } });
  const others = el('button', { type: 'button', class: 'lks-others', text: t('lockr.others') });
  others.addEventListener('click', () => { if (!busy) { wipe(); onOthers(); } });
  top.append(go, others);
  host.append(top);

  let field = null;
  if (resumable) {
    const bottom = el('section', { class: 'lks-bottom', 'aria-labelledby': 'lks-resume-title' });
    const title = el('h2', { class: 'lks-title', id: 'lks-resume-title', text: t('lockr.resume_title') });
    field = el('input', {
      type: 'password', class: 'lks-field', id: 'lks-field', maxlength: String(SECRET_MAX),
      autocomplete: 'one-time-code', inputmode: 'numeric', spellcheck: 'false', autocapitalize: 'off',
      'aria-label': t('lockr.field_label'), dir: 'ltr',
    });
    const pad = el('div', { class: 'keypad lks-pad', role: 'group', 'aria-label': t('lockr.pad_label') });
    for (const k of KEYS) {
      let b;
      if (k === 'back') {
        b = el('button', { type: 'button', class: 'kp-action', 'data-k': k, text: '⌫', 'aria-label': t('lock.backspace'), title: t('lock.backspace') });
      } else if (k === 'abc') {
        b = el('button', { type: 'button', class: 'kp-action', 'data-k': k, text: t('lockr.abc'), 'aria-label': t('lockr.abc_label'), title: t('lockr.abc_label') });
      } else {
        b = el('button', { type: 'button', 'data-k': k, text: k });
      }
      b.addEventListener('click', () => press(k));
      pad.append(b);
    }
    const submit = el('button', { type: 'button', class: 'btn-primary lks-submit', text: t('lockr.resume') });
    submit.addEventListener('click', () => send());
    field.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); send(); } });
    bottom.append(title, field, pad, submit);
    host.append(bottom);
  }

  function press(k) {
    if (busy || !field) return;
    if (k === 'back') field.value = field.value.slice(0, -1);
    else if (k === 'abc') {
      // A password: the phone's text keyboard instead of the number pad.
      field.setAttribute('inputmode', field.getAttribute('inputmode') === 'text' ? 'numeric' : 'text');
      field.focus();
      return;
    } else if (field.value.length < SECRET_MAX) field.value += k;
  }

  async function send() {
    if (busy || !field || !field.value) return;
    busy = true;
    host.classList.add('lks-busy');
    const secret = field.value;
    field.value = '';
    let ok = false;
    try { ok = await onResume(secret); } catch { ok = false; }
    busy = false;
    host.classList.remove('lks-busy');
    void ok;   // main.js moves on either way (resumed, or to main)
  }

  function wipe() { if (field) field.value = ''; }

  return {
    field,
    focus() { (field || go).focus(); },
    destroy() { wipe(); host.replaceChildren(); },
  };
}
