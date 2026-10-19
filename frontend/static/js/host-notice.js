// Host-storage notices: "this is kept on the computer that runs DisPatch".
//
// Everything a person makes or sends here -- an uploaded file, a picture, an
// avatar, a chat -- is stored by the server on the host computer, not on the
// device in their hand. These notices say so, plainly, wherever something is
// created or uploaded, and in Settings for the chats themselves. The shape is
// the LaserLloyd footer disclaimer's (a red outlined bubble with a bold title
// and a plain sentence or two), drawn with the active theme's danger tokens
// (app.css .host-notice) so it follows the palette instead of a fixed red.
//
// The static ones (Security tab, File Server, the send-a-file sheet, the avatar
// crop dialog) are plain markup in index.html with data-i18n; this module draws
// the ones built in JS. The wording per kind is in the shell locales under
// host_notice.* -- keep every claim TRUE for the code (see docs/security.md
// "What you are trusting" and docs/privacy.md).

import { t } from './i18n.js?v=4';
import { el } from './util.js?v=20';

/** kind -> [title key, body key]. Literal keys so the i18n key checks see them. */
export const HOST_NOTICE_KEYS = {
  upload: ['host_notice.upload_title', 'host_notice.upload_body'],
  fileserver: ['host_notice.fileserver_title', 'host_notice.fileserver_body'],
  images: ['host_notice.images_title', 'host_notice.images_body'],
  chats: ['host_notice.chats_title', 'host_notice.chats_body'],
  chats_restricted: ['host_notice.chats_restricted_title', 'host_notice.chats_restricted_body'],
};

/** The notice for `kind` as an <aside role="note">. `compact` = one smaller block
 *  for tight spots such as the composer's attachment row. */
export function hostNotice(kind, { compact = false } = {}) {
  const keys = HOST_NOTICE_KEYS[kind] || HOST_NOTICE_KEYS.upload;
  return el('aside', {
    class: `host-notice${compact ? ' host-notice-compact' : ''}`, role: 'note', 'data-kind': kind,
  }, [
    el('strong', { class: 'host-notice-title', text: t(keys[0]) }),
    el('span', { class: 'host-notice-body', text: ` ${t(keys[1])}` }),
  ]);
}
