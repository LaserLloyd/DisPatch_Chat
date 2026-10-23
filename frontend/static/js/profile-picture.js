// Profile pictures: upload / remove a picture for a profile (WP-AVATAR).
//
//   POST   /api/me/avatar               multipart "file"   your own picture
//   DELETE /api/me/avatar                                  back to the initial / emoji
//   POST   /api/people/{id}/avatar      (Admins, People editor)   same, for another profile
//   DELETE /api/people/{id}/avatar
//
// A picture is visible to EVERYONE who uses this DisPatch, the sign-in screen included, so
// the note saying so is part of the row (always on screen, never behind a tooltip).
// The server decodes, crops to a square, resizes to 256 px and re-encodes (metadata dropped);
// the checks here only save a round trip. Nothing runs at import time.

import { t } from './i18n.js?v=5';
import { el } from './util.js?v=21';
import { profileReq } from './bot-editor.js?v=1';
import { avatarSpec } from './profiles.js?v=3';
import { nimEnabled } from './nim.js?v=6';

export const MAX_BYTES = 5 * 1024 * 1024;
export const ACCEPT = 'image/png,image/jpeg,image/webp,image/gif';
const TYPES = new Set(['image/png', 'image/jpeg', 'image/webp', 'image/gif']);
const URL_PREFIX = '/api/profile-avatars/';

/** Is this profile `avatar` value an uploaded picture (a served path) rather than an emoji? */
export function isUploadedPicture(avatar) {
  return typeof avatar === 'string' && avatar.startsWith(URL_PREFIX);
}

/** A file that cannot work -> the locale key of the reason, else ''. */
export function pictureProblem(file) {
  if (!file) return 'picture.err_type';
  if (file.type && !TYPES.has(String(file.type).toLowerCase())) return 'picture.err_type';
  if (Number(file.size) > MAX_BYTES) return 'picture.err_size';
  return '';
}

/** The words for a failed call. */
export function pictureError(e) {
  const code = e && e.code;
  if (code === 'avatar_too_large') return t('picture.err_size');
  if (code === 'avatar_invalid') return t('picture.err_type');
  if (code === 'lock_target_fence') return t('picture.shared_admin_only');
  const msg = String((e && e.message) || e || '').replace(/^\d{3}:\s*/, '');
  return t('picture.err_generic', { error: msg });
}

export async function uploadPicture(base, file) {
  const fd = new FormData();
  fd.append('file', file, file.name || 'picture');
  return profileReq('POST', base, { form: fd });
}

export const removePicture = (base) => profileReq('DELETE', base);

/** A round preview of a profile's picture (or its emoji / initial; the initial under No-Image Mode). */
export function previewNode(profile, cls = 'pp-preview') {
  const spec = avatarSpec(profile, { noImages: nimEnabled() });
  if (spec.kind === 'img') {
    return el('img', { class: cls, src: spec.src, alt: '', width: '56', height: '56', draggable: 'false' });
  }
  return el('span', { class: `${cls} ${cls}--text`, 'aria-hidden': 'true', text: spec.text });
}

/**
 * The picture row. `profile` is {id, name, avatar}; `base` the avatar route; `onChanged(profile)`
 * runs after a successful upload or removal with the server's profile. `locked` (the shared
 * default profile, seen by a non-Admin) shows the preview and the reason, no buttons.
 */
export function pictureRow({ profile, base, onChanged, toast, locked = false }) {
  let current = profile || {};
  const holder = el('span', { class: 'pp-preview-wrap' });
  const status = el('div', { class: 'pp-status muted', role: 'status', 'aria-live': 'polite' });
  const input = el('input', { type: 'file', accept: ACCEPT, class: 'pp-file', hidden: '', tabindex: '-1', 'aria-label': t('picture.title') });
  const upload = el('button', { class: 'ui-btn', type: 'button', 'data-pp': 'upload' });
  const remove = el('button', { class: 'ui-btn', type: 'button', 'data-pp': 'remove', text: t('picture.remove') });

  const paint = () => {
    holder.replaceChildren(previewNode(current));
    upload.textContent = t(isUploadedPicture(current.avatar) ? 'picture.change' : 'picture.upload');
    remove.hidden = !current.avatar;
    upload.disabled = remove.disabled = !!locked;
  };
  const say = (msg, bad = false) => { status.textContent = msg || ''; status.classList.toggle('pp-bad', !!bad); };
  const busy = (on) => { upload.disabled = remove.disabled = on || !!locked; input.disabled = on; };

  const done = async (next, msg) => {
    current = { ...current, ...(next || {}) };
    paint();
    say(msg);
    if (toast) toast(msg);
    if (onChanged) await onChanged(current);
  };

  upload.addEventListener('click', () => { say(''); input.click(); });
  input.addEventListener('change', async () => {
    const file = input.files && input.files[0];
    input.value = '';
    if (!file) return;
    const bad = pictureProblem(file);
    if (bad) { say(t(bad), true); return; }
    busy(true);
    say(t('picture.uploading'));
    try {
      await done(await uploadPicture(base, file), t('picture.saved'));
    } catch (e) {
      say(pictureError(e), true);
    } finally {
      busy(false);
    }
  });
  remove.addEventListener('click', async () => {
    busy(true);
    say('');
    try {
      await done(await removePicture(base), t('picture.removed'));
    } catch (e) {
      say(pictureError(e), true);
    } finally {
      busy(false);
    }
  });

  paint();
  return el('div', { class: 'pp-row' }, [
    el('div', { class: 'pp-main' }, [
      holder,
      el('div', { class: 'pp-actions' }, [upload, remove]),
    ]),
    input,
    el('p', { class: 'pp-note', role: 'note', text: t('picture.note') }),
    locked ? el('p', { class: 'pp-note muted', text: t('picture.shared_admin_only') }) : null,
    el('p', { class: 'pp-hint muted', text: t('picture.formats') }),
    status,
  ]);
}

// ---- Settings -> You -> Account (run by settings-nav SECTION_HOOKS) ------------------------

let shell = {};
/** Called once at boot with the shell's helpers (toast, state, onProfileChanged). */
export function initProfilePicture(deps = {}) { shell = deps || {}; }

/** Add the Profile picture group right after the "who you are" card. Safe on every mount. */
export function renderProfilePicture(host, deps = {}) {
  if (!host) return;
  const d = { ...shell, ...deps };
  for (const old of host.querySelectorAll('[data-anchor="picture"]')) old.remove();
  const auth = (d.state && d.state.auth) || {};
  const prof = auth.profile;
  if (!prof || !prof.id) return;                       // an open install has no profile yet
  const admin = auth.role === 'admin';
  const locked = !!(prof.shared && !admin);
  const group = el('section', { class: 'dev-group', 'data-anchor': 'picture', 'aria-labelledby': 'pp-h' }, [
    el('h3', { class: 'dev-group-title', id: 'pp-h', text: t('picture.title') }),
    pictureRow({
      profile: prof, base: '/api/me/avatar', locked, toast: d.toast,
      onChanged: async () => { if (d.onProfileChanged) await d.onProfileChanged(); },
    }),
  ]);
  const who = host.querySelector('.acct-who-group');
  if (who) who.after(group); else host.prepend(group);
}
