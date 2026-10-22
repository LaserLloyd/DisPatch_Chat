// Settings -> You -> Account: "Reset to default" (preview with counts, blunt confirm,
// token, 409 on a stale preview) and "My data" (export own). Owned by WP-FE-YOU.
//
//   GET  /api/me/reset/preview -> {counts:{bots,chats,pictures,files,voices}, token, expires_in,
//                                  keeps_voices, outcome:"atlas"|"household"}
//   POST /api/me/reset {token} -> {deleted:{...}, keeps_voices}      409 reset_stale: look again
//   GET  /api/me/export        -> the caller's chats and bot setup as a download
//
// main.js calls initAccountReset() once at boot; settings-nav.js SECTION_HOOKS runs
// renderAccountReset() when Account mounts. Nothing runs at import time.

import { t } from './i18n.js?v=4';
import { el } from './util.js?v=20';
import { profileReq, withShell, initBotEditor } from './bot-editor.js?v=1';

let shell = {};
/** Called once at boot with the shell's helpers. */
export function initAccountReset(deps = {}) {
  shell = deps || {};
  initBotEditor(deps);
}

// ------------------------------------------------------------------ pure helpers

/** The blunt sentences for a preview, in order. `shared` = the profile everyone lands on. */
export function confirmLines(preview, { name = '', shared = false } = {}) {
  const c = (preview && preview.counts) || {};
  const lines = [
    t('reset.confirm_counts', {
      bots: Number(c.bots || 0), chats: Number(c.chats || 0), pictures: Number(c.pictures || 0), files: Number(c.files || 0),
    }),
    preview && preview.outcome === 'household' ? t('reset.confirm_basic') : t('reset.confirm_atlas'),
  ];
  if (preview && preview.keeps_voices !== false) lines.push(t('reset.voices_kept'));
  if (shared) lines.push(t('reset.shared_warning'));
  void name;
  return lines;
}

/** Is the signed-in profile the shared default one (/api/auth/status default_profile)? */
export function isSharedProfile(auth) {
  const a = auth || {};
  const mine = a.profile && a.profile.id;
  const def = a.default_profile && (a.default_profile.id || a.default_profile);
  return !!(mine && def && mine === def);
}

// ------------------------------------------------------------------ DOM

/** Add the Reset / My data group to the Account pane `host`. Safe to call again on every mount. */
export function renderAccountReset(host, deps = {}) {
  if (!host) return;
  const d = withShell({ ...shell, ...deps });
  for (const old of host.querySelectorAll('[data-anchor="reset"], [data-anchor="mydata"]')) old.remove();

  const auth = (d.state && d.state.auth) || {};
  const name = (auth.profile && auth.profile.name) || '';
  const shared = isSharedProfile(auth);

  const panel = el('div', { class: 'ar-confirm', role: 'alertdialog', 'aria-labelledby': 'ar-confirm-title', hidden: 'hidden' });
  const status = el('div', { class: 'ar-status', role: 'status', 'aria-live': 'polite' });
  const openBtn = el('button', { class: 'ui-btn ui-btn--danger', type: 'button', id: 'ar-open', text: t('reset.button') });

  const closePanel = () => { panel.hidden = true; panel.replaceChildren(); openBtn.disabled = false; openBtn.focus(); };

  async function showPreview(stale = false) {
    openBtn.disabled = true;
    status.textContent = '';
    let preview;
    try {
      preview = await profileReq('GET', '/api/me/reset/preview');
    } catch (e) {
      status.textContent = e.message; openBtn.disabled = false; return;
    }
    const confirmBtn = el('button', { class: 'ui-btn ui-btn--danger', type: 'button', id: 'ar-confirm', text: t('reset.confirm_button') });
    const cancelBtn = el('button', { class: 'ui-btn', type: 'button', id: 'ar-cancel', text: t('common.cancel'), onclick: closePanel });
    panel.replaceChildren(...[
      el('h4', { class: 'ar-title', id: 'ar-confirm-title', text: t('reset.confirm_title', { name }) }),
      stale ? el('p', { class: 'yo-error', role: 'alert', text: t('reset.stale') }) : null,
      ...confirmLines(preview, { name, shared }).map((l) => el('p', { class: 'ar-line', text: l })),
      el('div', { class: 'yo-actions yo-foot' }, [cancelBtn, confirmBtn]),
    ].filter(Boolean));
    panel.hidden = false;
    cancelBtn.focus();
    panel.onkeydown = (e) => { if (e.key === 'Escape') { e.stopPropagation(); closePanel(); } };
    confirmBtn.addEventListener('click', async () => {
      confirmBtn.disabled = true; cancelBtn.disabled = true;
      try {
        await profileReq('POST', '/api/me/reset', { json: { token: preview.token } });
      } catch (e) {
        if (e.status === 409 || e.code === 'reset_stale') { await showPreview(true); return; }
        panel.replaceChildren(el('p', { class: 'yo-error', role: 'alert', text: e.message }),
          el('div', { class: 'yo-actions yo-foot' }, [el('button', { class: 'ui-btn', type: 'button', text: t('common.close'), onclick: closePanel })]));
        return;
      }
      panel.hidden = true; panel.replaceChildren();
      status.textContent = t('reset.done');
      if (d.toast) d.toast(t('reset.done'));
      if (typeof d.onReset === 'function') d.onReset();
      else setTimeout(() => { try { location.reload(); } catch { /* not in a page */ } }, 900);
    });
  }
  openBtn.addEventListener('click', () => showPreview(false));

  const resetGroup = el('section', { class: 'dev-group', 'data-anchor': 'reset', 'aria-labelledby': 'ar-h-reset' }, [
    el('h3', { class: 'dev-group-title', id: 'ar-h-reset', text: t('reset.title') }),
    el('p', { class: 'muted', text: t('reset.desc') }),
    shared ? el('p', { class: 'muted', text: t('reset.shared_warning') }) : null,
    openBtn, panel, status,
  ]);
  const dataGroup = el('section', { class: 'dev-group', 'data-anchor': 'mydata', 'aria-labelledby': 'ar-h-data' }, [
    el('h3', { class: 'dev-group-title', id: 'ar-h-data', text: t('reset.my_data') }),
    el('p', { class: 'muted', text: t('reset.export_hint') }),
    el('a', { class: 'ui-btn', id: 'ar-export', href: '/api/me/export', download: '', text: t('reset.export') }),
  ]);
  host.append(dataGroup, resetGroup);
}
