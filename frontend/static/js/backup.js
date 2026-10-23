// Settings -> All settings -> System -> Backups & recovery.
//
//   Encrypted backups   the schedule (read-only here; the switches are ordinary schema rows above),
//                       "Back up now", and every backup the server knows (encrypted archives, the
//                       rotating snapshots, the copies taken before an upgrade) with Verify /
//                       Restore / Delete. A restore is staged and applied at the next start.
//   Support bundle      a redacted zip for a bug report; prompts are left out unless ticked.
//   Profile data        export or erase what DisPatch holds about one profile.
//
// Admins server side (admin_system); this module only decides what to OFFER. Everything that can
// lose data asks first, and a recovery key is shown exactly once.

import { w5 as realApi, saveBlob, errorText } from './w5-api.js?v=2';
import { t, fileSize, fullTimestamp } from './i18n.js?v=5';
import { el } from './util.js?v=21';

// ------------------------------------------------------------------ pure

/** One-line state of the schedule for the header. */
export function scheduleSummary(s) {
  if (!s) return '';
  if (!s.enabled) return t('bkp.sched_off');
  const age = s.last_archive_age_hours;
  const last = age == null ? t('bkp.never') : t('bkp.hours_ago', { count: Math.round(age) });
  return t('bkp.sched_on', { hours: s.interval_hours, keep: s.keep, last });
}

/** Which actions a listing row offers. */
export function actionsFor(b) {
  return {
    verify: b.kind === 'archive',
    restore: b.restorable !== false,
    remove: b.kind !== 'premigration',
    needsPassphrase: b.kind === 'archive',
  };
}

const KIND_KEYS = { archive: 'bkp.kind_archive', snapshot: 'bkp.kind_snapshot', premigration: 'bkp.kind_premigration' };
export const kindLabel = (kind) => t(KIND_KEYS[kind] || KIND_KEYS.snapshot);
// 4.0 scale: a rotating snapshot is kept as a recent, a daily or a weekly one.
const TIER_KEYS = { recent: 'bkp.tier_recent', daily: 'bkp.tier_daily', weekly: 'bkp.tier_weekly' };
export const tierLabel = (b) => (b && b.kind === 'snapshot' && TIER_KEYS[b.tier] ? t(TIER_KEYS[b.tier]) : '');

// ------------------------------------------------------------------ DOM

function section(title, hint, ...kids) {
  return el('div', { class: 'bkp-section' }, [
    el('h4', { class: 'schema-section', text: title }),
    hint ? el('p', { class: 'dev-group-hint', text: hint }) : null,
    ...kids,
  ]);
}

/** Mount into `host`. deps: { api?, toast, confirm(msg, opts), role } */
export function mountBackups(host, deps = {}) {
  const api = deps.api || realApi;
  const toast = deps.toast || (() => {});
  const ask = deps.confirm || (async () => true);
  host.textContent = '';
  const owner = deps.role === 'owner' || deps.role === 'admin' || deps.role == null;   // admin_system; the server decides
  if (!owner) {
    host.append(el('p', { class: 'muted', text: t('bkp.owner_only') }));
    return;
  }

  const head = el('div', { class: 'bkp-head' }, [el('h3', { class: 'dev-group-title', text: t('bkp.title') })]);
  const summary = el('p', { class: 'muted', role: 'status' });
  const pending = el('div', { class: 'bkp-pending', role: 'status', hidden: '' });
  const list = el('div', { class: 'bkp-list dev-group-body' });
  const pass = el('input', { type: 'password', class: 'ui-input', autocomplete: 'new-password',
    placeholder: t('bkp.passphrase_placeholder'), 'aria-label': t('bkp.passphrase_label') });
  const media = el('input', { type: 'checkbox', id: 'bkp-media', class: 'ui-checkbox' });
  const now = el('button', { type: 'button', class: 'btn-primary', text: t('bkp.now') });
  const keyBox = el('div', { class: 'bkp-key', role: 'alert', hidden: '' });
  const msg = el('p', { class: 'muted', role: 'status', 'aria-live': 'polite' });

  async function refresh() {
    let d;
    try { d = await api.backups(); } catch (e) { list.textContent = errorText(e); return; }
    summary.textContent = scheduleSummary(d.schedule);
    list.textContent = '';
    if (!d.backups.length) list.append(el('p', { class: 'muted', text: t('bkp.none') }));
    for (const b of d.backups) list.append(row(b));
    try {
      const p = await api.pendingRestore();
      pending.hidden = !p.pending;
      if (p.pending) {
        pending.textContent = '';
        const cancel = el('button', { type: 'button', class: 'btn-secondary', text: t('bkp.cancel_restore') });
        cancel.addEventListener('click', async () => { await api.cancelPendingRestore(); toast(t('bkp.restore_cancelled')); refresh(); });
        pending.append(el('strong', { text: t('bkp.restore_staged_title') }), ' ', t('bkp.restore_staged', { source: p.source || '' }), ' ', cancel);
      }
    } catch { pending.hidden = true; }
  }

  function row(b) {
    const a = actionsFor(b);
    const buttons = [];
    const need = () => (a.needsPassphrase ? (pass.value.trim() || null) : null);
    if (a.verify) {
      const v = el('button', { type: 'button', class: 'btn-secondary', text: t('bkp.verify'), 'aria-label': t('bkp.verify_aria', { name: b.name }) });
      v.addEventListener('click', async () => {
        try {
          const r = await api.verifyBackup(b.id, need());
          toast(t('bkp.verified', { files: r.files || 0 }));
        } catch (e) { toast(errorText(e), true); }
      });
      buttons.push(v);
    }
    if (a.restore) {
      const r = el('button', { type: 'button', class: 'btn-secondary', text: t('bkp.restore'), 'aria-label': t('bkp.restore_aria', { name: b.name }) });
      r.addEventListener('click', async () => {
        if (!await ask(t('bkp.restore_confirm', { name: b.name }), { okText: t('bkp.restore'), danger: true })) return;
        try {
          const out = await api.restoreBackup(b.id, need());
          toast(out.message || t('bkp.restore_staged_toast'));
          refresh();
        } catch (e) { toast(errorText(e), true); }
      });
      buttons.push(r);
    }
    if (a.remove) {
      const d = el('button', { type: 'button', class: 'btn-secondary danger-text', text: t('bkp.delete'), 'aria-label': t('bkp.delete_aria', { name: b.name }) });
      d.addEventListener('click', async () => {
        if (!await ask(t('bkp.delete_confirm', { name: b.name }), { okText: t('bkp.delete'), danger: true })) return;
        try { await api.deleteBackup(b.id); refresh(); } catch (e) { toast(errorText(e), true); }
      });
      buttons.push(d);
    }
    return el('div', { class: 'dev-row bkp-row', dataset: { backup: b.id } }, [
      el('span', { class: 'dev-row-text' }, [
        el('strong', { text: b.name }), ' ',
        el('span', { class: 'ui-badge', text: kindLabel(b.kind) }), ' ',
        tierLabel(b) ? el('span', { class: 'ui-badge ui-badge--info bkp-tier', text: tierLabel(b) }) : null, ' ',
        el('span', { class: 'dev-row-hint', text: `${fileSize(b.size)} · ${fullTimestamp(b.created)}${b.encrypted ? ` · ${t('bkp.encrypted')}` : ''}` }),
      ]),
      ...buttons,
    ]);
  }

  now.addEventListener('click', async () => {
    now.disabled = true;
    msg.textContent = t('bkp.working');
    try {
      const r = await api.createBackup({ ...(pass.value.trim() ? { passphrase: pass.value.trim() } : {}),
        include_media: media.checked });
      msg.textContent = t('bkp.done', { name: r.backup.name });
      if (r.recovery_key) showKey(r.recovery_key);
      pass.value = '';
      await refresh();
    } catch (e) { msg.textContent = errorText(e); }
    finally { now.disabled = false; }
  });

  function showKey(key) {
    keyBox.hidden = false;
    keyBox.textContent = '';
    const input = el('input', { type: 'text', readonly: '', class: 'ui-input bkp-key-input', value: key, 'aria-label': t('bkp.key_label') });
    const copy = el('button', { type: 'button', class: 'btn-secondary', text: t('bkp.copy') });
    copy.addEventListener('click', async () => {
      try { await navigator.clipboard.writeText(key); toast(t('bkp.copied')); } catch { input.select(); }
    });
    const done = el('button', { type: 'button', class: 'btn-secondary', text: t('bkp.key_saved') });
    done.addEventListener('click', () => { keyBox.hidden = true; keyBox.textContent = ''; });
    keyBox.append(el('strong', { text: t('bkp.key_title') }), el('p', { text: t('bkp.key_note') }), input, copy, done);
    input.focus();
  }

  // ---- support bundle
  const prompts = el('input', { type: 'checkbox', id: 'bkp-prompts', class: 'ui-checkbox' });
  const bundle = el('button', { type: 'button', class: 'btn-secondary', text: t('bkp.bundle') });
  bundle.addEventListener('click', async () => {
    bundle.disabled = true;
    try {
      const { blob, name } = await api.supportBundle(prompts.checked);
      saveBlob(blob, name);
      toast(t('bkp.bundle_done'));
    } catch (e) { toast(errorText(e), true); }
    finally { bundle.disabled = false; }
  });

  // ---- profile data
  const who = el('select', { class: 'ui-select', 'aria-label': t('bkp.profile_label') });
  const exportBtn = el('button', { type: 'button', class: 'btn-secondary', text: t('bkp.export'), disabled: '' });
  const eraseBtn = el('button', { type: 'button', class: 'btn-secondary danger-text', text: t('bkp.erase'), disabled: '' });
  who.addEventListener('change', () => { exportBtn.disabled = eraseBtn.disabled = !who.value; });
  api.people().then((d) => {
    who.append(el('option', { value: '', text: t('bkp.pick_profile') }));
    for (const p of d.profiles || []) who.append(el('option', { value: p.id, text: `${p.name} (${t(`role.${p.role}`)})` }));
  }).catch(() => {});
  exportBtn.addEventListener('click', async () => {
    try { const { blob, name } = await api.exportUser(who.value); saveBlob(blob, name); } catch (e) { toast(errorText(e), true); }
  });
  eraseBtn.addEventListener('click', async () => {
    const name = who.options[who.selectedIndex].text;
    if (!await ask(t('bkp.erase_confirm', { name }), { okText: t('bkp.erase'), danger: true })) return;
    try {
      await api.eraseUser(who.value);
      toast(t('bkp.erased'));
      who.remove(who.selectedIndex); who.value = ''; exportBtn.disabled = eraseBtn.disabled = true;
    } catch (e) { toast(errorText(e), true); }
  });

  host.append(
    head, summary, pending,
    section(t('bkp.encrypted_title'), t('bkp.encrypted_hint'),
      el('div', { class: 'bkp-form' }, [
        pass,
        el('label', { class: 'bkp-check', for: 'bkp-media' }, [media, ' ', t('bkp.include_media')]),
        now,
      ]), msg, keyBox, list),
    section(t('bkp.bundle_title'), t('bkp.bundle_hint'),
      el('div', { class: 'bkp-form' }, [el('label', { class: 'bkp-check', for: 'bkp-prompts' }, [prompts, ' ', t('bkp.include_prompts')]), bundle])),
    section(t('bkp.profile_title'), t('bkp.profile_hint'), el('div', { class: 'bkp-form' }, [who, exportBtn, eraseBtn])),
  );
  refresh();
}
