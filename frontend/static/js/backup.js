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
//
// 4.3: "Show recovery key" (the protected Admin only, after typing the PIN or password again)
// opens a small dialog that shows the generated key once, with Copy; closing it removes the key
// from the page. A restore that cannot put a key back lists it (`re_enter`). Pre-restore copies
// are their own kind and, like pre-migration copies, cannot be deleted from here.

import { w5 as realApi, saveBlob, errorText } from './w5-api.js?v=3';
import { t, fileSize, fullTimestamp } from './i18n.js?v=5';
import { el } from './util.js?v=23';

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
    remove: b.kind !== 'premigration' && b.kind !== 'prerestore',
    needsPassphrase: b.kind === 'archive',
  };
}

/** The reveal route's body: the credential typed again, as a PIN or as a password. */
export function revealBody(kind, value) {
  const v = String(value || '');
  return kind === 'password' ? { password: v } : { pin: v };
}

/** The sentence that names the keys to enter again, or '' when there are none. */
export function reEnterText(names, key = 'bkp.re_enter_after') {
  const list = (Array.isArray(names) ? names : []).filter(Boolean);
  return list.length ? t(key, { names: list.join(', ') }) : '';
}

/** Should "Show recovery key" be offered? Only when a generated key exists, and not to a caller
 *  known not to be the protected Admin (the server refuses everyone else anyway). */
export function offerReveal(schedule, isProtected) {
  return !!(schedule && schedule.generated_key) && isProtected !== false;
}

/** Map a failed reveal to what the dialog says. */
export function revealError(e) {
  const status = e && e.status;
  if (status === 401) return t('bkp.reveal_wrong');
  if (status === 429) {
    const secs = (e.body && e.body.retry_after) || e.retryAfter || 0;
    return t('bkp.reveal_wait', { seconds: secs });
  }
  if (status === 404) return t('bkp.reveal_none');
  return errorText(e);
}

/** POST the reveal itself, NOT through w5-api: a 401 here is a mistyped PIN, not an ended
 *  session, so it must not trip the "signed out" handler. Never cached by the browser. */
export async function postReveal(body, fetchImpl = globalThis.fetch) {
  const r = await fetchImpl('/api/backups/recovery-key/reveal', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}),
    credentials: 'same-origin', cache: 'no-store',
  });
  let b = null;
  try { b = await r.json(); } catch { /* not JSON */ }
  if (!r.ok) {
    const msg = (b && (b.detail || (b.error && b.error.message))) || r.statusText || String(r.status);
    const err = new Error(`${r.status}: ${msg}`);
    err.status = r.status;
    err.body = b;
    err.code = b && b.error && b.error.code;
    throw err;
  }
  return b || {};
}

const KIND_KEYS = { archive: 'bkp.kind_archive', snapshot: 'bkp.kind_snapshot', premigration: 'bkp.kind_premigration', prerestore: 'bkp.kind_prerestore' };
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

/** Mount into `host`. deps: { api?, toast, confirm(msg, opts), role, isProtected } */
export function mountBackups(host, deps = {}) {
  const api = deps.api || realApi;
  const toast = deps.toast || (() => {});
  const ask = deps.confirm || (async () => true);
  host.textContent = '';
  const owner = deps.role === 'admin' || deps.role == null;   // admin_system; the server decides
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
  const reEnter = el('p', { class: 'bkp-re-enter', role: 'status', hidden: '' });
  const revealBtn = el('button', { type: 'button', class: 'btn-secondary bkp-reveal-btn', text: t('bkp.reveal'), hidden: '' });
  const revealHost = el('div', { class: 'bkp-reveal-host' });
  // 4.3 Fable: a second Admin sees the button, switched off, with the reason (not nothing).
  const revealNote = el('p', { class: 'muted bkp-reveal-note', hidden: '', text: t('bkp.reveal_owner_only') });
  revealBtn.addEventListener('click', () => openReveal());

  async function refresh() {
    let d;
    try { d = await api.backups(); } catch (e) { list.textContent = errorText(e); return; }
    summary.textContent = scheduleSummary(d.schedule);
    const ownerOnly = !!(d.schedule && d.schedule.generated_key) && deps.isProtected === false;
    revealBtn.hidden = !offerReveal(d.schedule, deps.isProtected) && !ownerOnly;
    revealBtn.disabled = ownerOnly;
    revealNote.hidden = !ownerOnly;
    const again = reEnterText(d.schedule && d.schedule.last_restore && d.schedule.last_restore.re_enter);
    reEnter.hidden = !again;
    reEnter.textContent = again;
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
          const missing = reEnterText(out.re_enter, 'bkp.re_enter_staged');
          if (missing) msg.textContent = missing;
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

  // ---- show the generated recovery key again (protected Admin, credential re-entered)
  function closeReveal() {
    revealHost.textContent = '';          // the key leaves the page with the dialog
    revealBtn.disabled = false;
    revealBtn.focus();
  }

  function openReveal() {
    revealHost.textContent = '';
    revealBtn.disabled = true;
    const titleId = `bkp-reveal-title-${Math.random().toString(36).slice(2, 8)}`;
    const dlg = el('div', { class: 'bkp-key bkp-reveal', role: 'dialog', 'aria-labelledby': titleId });
    const kind = el('select', { class: 'ui-select', 'aria-label': t('bkp.reveal_with') }, [
      el('option', { value: 'pin', text: t('bkp.reveal_pin') }),
      el('option', { value: 'password', text: t('bkp.reveal_password') }),
    ]);
    const cred = el('input', { type: 'password', class: 'ui-input', autocomplete: 'current-password',
      'aria-label': t('bkp.reveal_credential') });
    const err = el('p', { class: 'muted bkp-reveal-error', role: 'alert' });
    const go = el('button', { type: 'submit', class: 'btn-primary', text: t('bkp.reveal_submit') });
    const cancel = el('button', { type: 'button', class: 'btn-secondary', text: t('bkp.reveal_cancel') });
    const form = el('form', { class: 'bkp-form' }, [kind, cred, go, cancel]);
    cancel.addEventListener('click', closeReveal);
    dlg.addEventListener('keydown', (ev) => { if (ev.key === 'Escape') closeReveal(); });
    form.addEventListener('submit', async (ev) => {
      ev.preventDefault();
      if (!cred.value) { cred.focus(); return; }
      go.disabled = true;
      err.textContent = '';
      try {
        const body = revealBody(kind.value, cred.value);
        const out = await (api.revealRecoveryKey ? api.revealRecoveryKey(body) : postReveal(body));
        cred.value = '';
        showRevealed(dlg, titleId, out.recovery_key || '');
      } catch (e) {
        cred.value = '';
        err.textContent = revealError(e);
        go.disabled = false;
        cred.focus();
      }
    });
    dlg.append(el('strong', { id: titleId, text: t('bkp.reveal_title') }), el('p', { text: t('bkp.reveal_hint') }), form, err);
    revealHost.append(dlg);
    cred.focus();
  }

  function showRevealed(dlg, titleId, key) {
    dlg.textContent = '';
    const input = el('input', { type: 'text', readonly: '', class: 'ui-input bkp-key-input', value: key, 'aria-label': t('bkp.key_label') });
    const copy = el('button', { type: 'button', class: 'btn-secondary', text: t('bkp.copy') });
    copy.addEventListener('click', async () => {
      try { await navigator.clipboard.writeText(key); toast(t('bkp.copied')); } catch { input.select(); }
    });
    const done = el('button', { type: 'button', class: 'btn-secondary', text: t('bkp.key_saved') });
    done.addEventListener('click', closeReveal);
    dlg.append(el('strong', { id: titleId, text: t('bkp.key_title') }), el('p', { text: t('bkp.key_note') }), input, copy, done);
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
    head, summary, pending, reEnter,
    section(t('bkp.encrypted_title'), t('bkp.encrypted_hint'),
      el('div', { class: 'bkp-form' }, [
        pass,
        el('label', { class: 'bkp-check', for: 'bkp-media' }, [media, ' ', t('bkp.include_media')]),
        now, revealBtn,
      ]), revealNote, revealHost, msg, keyBox, list),
    section(t('bkp.bundle_title'), t('bkp.bundle_hint'),
      el('div', { class: 'bkp-form' }, [el('label', { class: 'bkp-check', for: 'bkp-prompts' }, [prompts, ' ', t('bkp.include_prompts')]), bundle])),
    section(t('bkp.profile_title'), t('bkp.profile_hint'), el('div', { class: 'bkp-form' }, [who, exportBtn, eraseBtn])),
  );
  refresh();
}
