// People & Access -> what a profile can do. Owned by WP-FE-PEOPLE.
//
// Everything in Settings -> People that is about permissions and reach lives here, so
// people.js (the list and the editor) and characters.js (the household roster) stay small:
//
//   permissionsPanel   toggles from the role table. Items a role can never have are not
//                      offered. Agent bots carry the plain warning. For the profile that is
//                      the lock target, agents and voice cloning are shown LOCKED with the
//                      reason (the server refuses them with 422 either way).
//   householdPanel     which household bots a profile gets: all (User) or a pick (Basic).
//   backendsPanel      which household connections a profile's own bots may run on.
//   lockTargetGroup    "When locked, show: [the default profile / the profile list]".
//   askDialog          the confirmation dialog with counts (downgrade, reset, delete).
//
// The server decides everything (role table, fence, protected Admin). This module only
// decides what to OFFER, and shows the server's sentence when it refuses.
//
// Mounting: main.js calls initPeoplePerms() once at boot; settings-nav.js SECTION_HOOKS runs
// renderPeoplePerms(pane) after the People pane is built, which adds the lock-target group.
// Nothing runs at import time.

import { api as realApi } from './api.js?v=36';
import { w5 as realW5, failure, errorText } from './w5-api.js?v=2';
import { t } from './i18n.js?v=5';
import { el } from './util.js?v=21';

// ------------------------------------------------------------------ words

export { errorText };

// ------------------------------------------------------------------ pure: the permission table

/** The permissions a profile editor shows, in order. Each has a `perms.<name>` label. */
export const PERM_ORDER = Object.freeze([
  'use_chat', 'use_voice', 'attach_files', 'use_lookups', 'manage_history', 'use_notifications',
  'files_upload', 'files_read', 'use_images', 'voice_lab', 'create_characters', 'choose_backend',
  'dispatch_agents', 'approve_agent_actions',
]);

/** Never available to the lock-target profile (contracts/permissions.py LOCK_TARGET_FENCE). */
export const LOCK_TARGET_FENCE = Object.freeze(['dispatch_agents', 'approve_agent_actions', 'voice_lab']);
/** Agent bots: switching these on carries the plain warning. */
export const AGENT_PERMS = Object.freeze(['dispatch_agents', 'approve_agent_actions']);

/** The three role columns from GET /api/people, as sets. */
export function permTables(data) {
  const col = (o) => Object.fromEntries(Object.entries(o || {}).map(([r, list]) => [r, new Set(list || [])]));
  return { defaults: col(data && data.role_permissions), grantable: col(data && data.role_grantable),
    never: col(data && data.role_never) };
}

/** One row per permission this role can have (an Admin has them all; "never" is not offered).
 *  `on` is the effective state with the profile's overrides; `changed` says it differs from
 *  the role's default; `fenced` marks the lock-target fence (shown locked, switched off). */
export function permissionRows(person, tables, { lockTarget = false } = {}) {
  if (!person || person.role === 'admin') return [];
  const def = tables.defaults[person.role] || new Set();
  const grantable = tables.grantable[person.role] || new Set();
  const grants = new Set(person.grants || []);
  const denies = new Set(person.denies || []);
  const rows = [];
  for (const perm of PERM_ORDER) {
    const isDefault = def.has(perm);
    if (!isDefault && !grantable.has(perm)) continue;       // never: not offered
    const fenced = lockTarget && LOCK_TARGET_FENCE.includes(perm);
    const on = !fenced && ((isDefault && !denies.has(perm)) || (!isDefault && grants.has(perm)));
    const changed = isDefault ? denies.has(perm) : grants.has(perm);
    rows.push({ perm, on, fenced, changed, agent: AGENT_PERMS.includes(perm) });
  }
  return rows;
}

/** grants/denies after switching `perm` on or off. Keeps unrelated entries untouched. */
export function withToggle(person, perm, on, tables) {
  const grants = new Set(person.grants || []);
  const denies = new Set(person.denies || []);
  if ((tables.defaults[person.role] || new Set()).has(perm)) {
    if (on) denies.delete(perm); else denies.add(perm);
  } else if (on) grants.add(perm);
  else grants.delete(perm);
  return { grants: [...grants], denies: [...denies] };
}

/** Do the profile's overrides differ from its role's defaults at all? */
export const hasOverrides = (person) => (person.grants || []).length > 0 || (person.denies || []).length > 0;

/** The `household_bots` value of a form: 'all' only for a User, otherwise the ticked ids. */
export function householdValue(role, mode, picked) {
  return role === 'user' && mode === 'all' ? 'all' : [...picked];
}

// ------------------------------------------------------------------ calls

const JSON_HEADERS = { 'Content-Type': 'application/json' };

async function call(method, url, body) {
  const r = await fetch(url, { method, ...(body === undefined ? {} : { headers: JSON_HEADERS, body: JSON.stringify(body) }) });
  if (!r.ok) throw await failure(r, url);
  return r.status === 204 ? null : r.json();
}
const enc = encodeURIComponent;

/** Calls that api.js / w5-api.js do not carry yet. Same error contract as w5-api.js. */
export const profileCalls = {
  setBackends: (id, allowed) => call('PUT', `/api/people/${enc(id)}/backends`, { allowed }),
  resetPreview: (id) => call('GET', `/api/people/${enc(id)}/reset/preview`),
  reset: (id, token) => call('POST', `/api/people/${enc(id)}/reset`, { token }),
  cloneTo: (botId, profileId) => call('POST', `/api/characters/${enc(botId)}/clone-to/${enc(profileId)}`),
  characterDeletePreview: (id) => call('GET', `/api/characters/${enc(id)}/delete-preview`),
  deleteCharacter: (id) => call('DELETE', `/api/characters/${enc(id)}`),
};

// ------------------------------------------------------------------ DOM: the dialog

let dlgSeq = 0;

/**
 * The confirmation dialog with counts. Resolves 'ok', 'alt' (the optional second button) or
 * null (Cancel, Esc, a click outside). `tally` is [{n, label}] drawn as big numbers.
 *  askDialog({ title, body, notes: [string], tally, okText, altText, danger })
 */
export function askDialog({ title, body, notes = [], tally = [], okText, altText = '', danger = true } = {},
  doc = document) {
  return new Promise((resolve) => {
    const id = `pe-dlg-${++dlgSeq}`;
    const opener = doc.activeElement;
    const done = (v) => {
      doc.removeEventListener('keydown', onKey, true);
      back.remove();
      try { if (opener && opener.focus) opener.focus(); } catch { /* ignore */ }
      resolve(v);
    };
    const ok = el('button', { type: 'button', class: danger ? 'btn-primary danger' : 'btn-primary', text: okText || t('common.ok') });
    const cancel = el('button', { type: 'button', class: 'btn-secondary', text: t('people.dialog_cancel') });
    const alt = altText ? el('button', { type: 'button', class: 'btn-secondary', text: altText }) : null;
    ok.addEventListener('click', () => done('ok'));
    cancel.addEventListener('click', () => done(null));
    if (alt) alt.addEventListener('click', () => done('alt'));
    const box = el('div', { class: 'pe-dialog', role: 'alertdialog', 'aria-modal': 'true',
      'aria-labelledby': `${id}-t`, 'aria-describedby': `${id}-b` }, [
      el('h3', { class: 'pe-dialog-title', id: `${id}-t`, text: title }),
      body ? el('p', { class: 'pe-dialog-body', id: `${id}-b`, text: body }) : null,
      tally.length ? el('ul', { class: 'pe-tally' }, tally.map((x) => el('li', {}, [
        el('strong', { class: 'pe-tally-n', text: String(x.n) }), el('span', { text: x.label })]))) : null,
      ...notes.map((n) => el('p', { class: 'pe-dialog-note', text: n })),
      el('div', { class: 'pe-dialog-actions' }, [cancel, alt, ok]),
    ]);
    const back = el('div', { class: 'pe-backdrop' }, [box]);
    back.addEventListener('mousedown', (e) => { if (e.target === back) done(null); });
    const onKey = (e) => {
      if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); done(null); return; }
      if (e.key !== 'Tab') return;
      const f = [...box.querySelectorAll('button')];
      const i = f.indexOf(doc.activeElement);
      if (e.shiftKey && i <= 0) { e.preventDefault(); f[f.length - 1].focus(); }
      else if (!e.shiftKey && i === f.length - 1) { e.preventDefault(); f[0].focus(); }
    };
    doc.addEventListener('keydown', onKey, true);
    doc.body.append(back);
    cancel.focus();           // the safe choice is the default
  });
}

/** The four counts of a wipe as a tally (bots, chats, pictures, cloned voices; zeros stay). */
export function countsTally(c, { voices = true } = {}) {
  const x = c || {};
  const out = [
    { n: x.bots || 0, label: t('people.tally_bots') },
    { n: x.chats || 0, label: t('people.tally_chats') },
    { n: x.pictures || 0, label: t('people.tally_pictures') },
    { n: x.files || 0, label: t('people.tally_files') },
  ];
  if (voices) out.push({ n: x.voices || 0, label: t('people.tally_voices') });
  return out;
}

// ------------------------------------------------------------------ DOM: permissions

const uidn = (() => { let n = 0; return (p) => `${p}-${++n}`; })();

function switchRow({ id, label, hint, checked, disabled, tag, warn, dataPerm }) {
  const cb = el('input', { type: 'checkbox', role: 'switch', id, class: 'ui-checkbox' });
  cb.checked = !!checked;
  cb.disabled = !!disabled;
  const row = el('div', { class: 'dev-row pe-perm' + (disabled ? ' pe-perm-locked' : ''), dataset: { perm: dataPerm || '' } }, [
    el('label', { class: 'dev-row-text', for: id }, [
      el('strong', { text: label }), tag ? el('span', { class: 'ui-badge pe-tag', text: tag }) : null,
      hint ? el('span', { class: 'dev-row-hint', text: hint }) : null,
      warn ? el('span', { class: 'dev-row-hint pe-warn', role: 'note', text: warn }) : null,
    ]),
    cb,
  ]);
  return { row, cb };
}

/**
 * The permission toggles of one profile. Applies each change at once (PATCH grants/denies),
 * puts the switch back and shows the server's sentence when it refuses.
 *   ctx: { tables, api, toast, confirm, onChanged(person) }
 */
export function permissionsPanel(person, ctx) {
  const api = ctx.api || realApi;
  const box = el('fieldset', { class: 'pe-perms dev-group', 'aria-label': t('perms.title') }, []);
  const paint = () => {
    const rows = permissionRows(person, ctx.tables, { lockTarget: !!person.lock_target });
    box.replaceChildren(el('legend', { class: 'dev-group-title', text: t('perms.title') }));
    if (person.role === 'admin') {
      box.append(el('p', { class: 'dev-group-hint', text: t('perms.admin_all') }));
      return;
    }
    // The reason once for the whole group, not under every locked row.
    if (rows.some((r) => r.fenced)) {
      box.append(el('p', { class: 'dev-group-hint pe-fence-note', role: 'note', text: t('perms.lock_target_fence', { name: person.name }) }));
    }
    const body = el('div', { class: 'dev-group-body' });
    for (const r of rows) {
      const id = uidn(`pe-${r.perm}`);
      const sw = switchRow({
        id, dataPerm: r.perm, label: t(`perms.${r.perm}`), checked: r.on, disabled: r.fenced,
        tag: r.fenced ? t('perms.locked') : (r.changed ? t('perms.changed') : ''),
        warn: r.perm === 'dispatch_agents' && !r.fenced ? t('perms.agents_warning') : '',
      });
      sw.cb.addEventListener('change', async () => {
        const want = sw.cb.checked;
        if (want && r.agent && !await ctx.confirm(t('perms.agents_confirm', { name: person.name }),
          { danger: true, okText: t('perms.agents_confirm_ok') })) {
          sw.cb.checked = false;
          return;
        }
        sw.cb.disabled = true;
        try {
          const next = withToggle(person, r.perm, want, ctx.tables);
          const saved = await api.patchPerson(person.id, next);
          Object.assign(person, { grants: saved.grants || next.grants, denies: saved.denies || next.denies });
          ctx.toast(t('perms.saved', { name: person.name }));
          if (ctx.onChanged) ctx.onChanged(person);
          paint();
        } catch (e) {
          sw.cb.checked = !want;
          sw.cb.disabled = false;
          ctx.toast(errorText(e), true);
        }
      });
      body.append(sw.row);
    }
    box.append(body);
    if (hasOverrides(person)) {
      const back = el('button', { type: 'button', class: 'btn-secondary pe-defaults',
        text: t('perms.reset_defaults', { type: t(`people.type_${person.role}`) }) });
      back.addEventListener('click', async () => {
        back.disabled = true;
        try {
          const saved = await api.patchPerson(person.id, { grants: [], denies: [] });
          Object.assign(person, { grants: saved.grants || [], denies: saved.denies || [] });
          ctx.toast(t('perms.saved', { name: person.name }));
          if (ctx.onChanged) ctx.onChanged(person);
          paint();
        } catch (e) { back.disabled = false; ctx.toast(errorText(e), true); }
      });
      box.append(el('div', { class: 'pe-actions' }, [back]));
    }
  };
  paint();
  return box;
}

// ------------------------------------------------------------------ DOM: household bots

/**
 * Which household bots a profile gets. `bots` = GET /api/characters/all rows. Returns the
 * panel with `getValue()` ('all' | [ids]) for the create form; with `person.id` and `save`
 * on it saves through PUT household-bots.
 *   ctx: { bots, api, toast, save: true|false, onChanged }
 */
export function householdPanel(person, ctx) {
  const api = ctx.api || realApi;
  const role = person.role;
  const bots = (ctx.bots || []).filter((b) => b && b.id);
  const current = person.household_bots;
  let mode = role === 'user' && (current === 'all' || current === undefined) ? 'all' : 'pick';
  const picked = new Set(Array.isArray(current) ? current : []);
  const name = uidn('pe-hh');
  const box = el('fieldset', { class: 'pe-household dev-group', 'aria-label': t('perms.household_bots') });
  const list = el('div', { class: 'pe-household-list dev-group-body' });
  const err = el('div', { class: 'lock-error people-error', role: 'alert', hidden: '' });

  const radio = (value, label) => {
    const id = uidn('pe-hr');
    const input = el('input', { type: 'radio', name, id, value, class: 'ui-radio' });
    input.checked = mode === value;
    input.addEventListener('change', () => { mode = value; paintList(); });
    return el('label', { class: 'ui-check pe-radio', for: id }, [input, ' ', label]);
  };
  const paintList = () => {
    list.replaceChildren();
    list.hidden = mode === 'all';
    if (mode === 'all') return;
    if (!bots.length) { list.append(el('p', { class: 'muted', text: t('perms.household_empty') })); return; }
    for (const b of bots) {
      const id = uidn('pe-hb');
      const cb = el('input', { type: 'checkbox', id, class: 'ui-checkbox', value: b.id });
      cb.checked = picked.has(b.id);
      cb.addEventListener('change', () => { if (cb.checked) picked.add(b.id); else picked.delete(b.id); });
      const tags = [];
      if (b.backend_kind === 'agent' || (b.backend && b.backend.kind === 'agent') || b.agent) tags.push(t('perms.household_agent'));
      if (b.hidden || (b.flags && b.flags.hidden)) tags.push(t('perms.household_hidden'));
      list.append(el('label', { class: 'bkp-check pe-hb', for: id }, [
        cb, ' ', `${b.emoji ? `${b.emoji} ` : ''}${b.name}`,
        tags.length ? el('span', { class: 'dev-row-hint', text: ` (${tags.join(', ')})` }) : null]));
    }
  };

  box.append(el('legend', { class: 'dev-group-title', text: t('perms.household_bots') }),
    el('p', { class: 'dev-group-hint', text: t(role === 'basic' ? 'perms.household_basic_hint' : 'perms.household_user_hint') }),
    el('div', { class: 'pe-radios', role: 'radiogroup', 'aria-label': t('perms.household_bots') }, [
      role === 'user' ? radio('all', t('perms.household_all')) : null,
      radio('pick', t('perms.household_pick'))]),
    list, err);
  paintList();

  box.getValue = () => householdValue(role, mode, picked);
  if (ctx.save) {
    const save = el('button', { type: 'button', class: 'btn-secondary', text: t('perms.household_save') });
    save.addEventListener('click', async () => {
      save.disabled = true;
      err.hidden = true;
      try {
        const v = box.getValue();
        const saved = await api.setHouseholdBots(person.id, v);
        person.household_bots = saved && saved.household_bots !== undefined ? saved.household_bots : v;
        ctx.toast(t('perms.household_saved'));
        if (ctx.onChanged) ctx.onChanged(person);
      } catch (e) { err.textContent = errorText(e); err.hidden = false; } finally { save.disabled = false; }
    });
    box.append(el('div', { class: 'pe-actions' }, [save]));
  }
  return box;
}

// ------------------------------------------------------------------ DOM: backends

/** Which connections a profile's own bots may run on (`PUT .../backends`). */
export function backendsPanel(person, ctx) {
  const calls = ctx.calls || profileCalls;
  const conns = ctx.connections || [];
  const allowed = person.backends && Array.isArray(person.backends.allowed) ? person.backends.allowed : null;
  let mode = allowed ? 'only' : 'all';
  const picked = new Set(allowed || []);
  const name = uidn('pe-be');
  const box = el('fieldset', { class: 'pe-backends dev-group', 'aria-label': t('perms.backends') });
  const list = el('div', { class: 'dev-group-body' });
  const err = el('div', { class: 'lock-error people-error', role: 'alert', hidden: '' });
  const radio = (value, label) => {
    const id = uidn('pe-br');
    const input = el('input', { type: 'radio', name, id, value, class: 'ui-radio' });
    input.checked = mode === value;
    input.addEventListener('change', () => { mode = value; paintList(); });
    return el('label', { class: 'ui-check pe-radio', for: id }, [input, ' ', label]);
  };
  const paintList = () => {
    list.replaceChildren();
    list.hidden = mode === 'all';
    if (mode === 'all') return;
    if (!conns.length) { list.append(el('p', { class: 'muted', text: t('perms.backends_none') })); return; }
    for (const c of conns) {
      const id = uidn('pe-bc');
      const cb = el('input', { type: 'checkbox', id, class: 'ui-checkbox', value: c.id });
      cb.checked = picked.has(c.id);
      cb.addEventListener('change', () => { if (cb.checked) picked.add(c.id); else picked.delete(c.id); });
      list.append(el('label', { class: 'bkp-check', for: id }, [cb, ' ', `${c.name}${c.type_label ? ` (${c.type_label})` : ''}`]));
    }
  };
  const save = el('button', { type: 'button', class: 'btn-secondary', text: t('perms.backends_save') });
  save.addEventListener('click', async () => {
    save.disabled = true;
    err.hidden = true;
    try {
      const r = await calls.setBackends(person.id, mode === 'all' ? null : [...picked]);
      person.backends = (r && r.backends) || (mode === 'all' ? {} : { allowed: [...picked] });
      ctx.toast(t('perms.backends_saved'));
    } catch (e) { err.textContent = errorText(e); err.hidden = false; } finally { save.disabled = false; }
  });
  box.append(el('legend', { class: 'dev-group-title', text: t('perms.backends') }),
    el('p', { class: 'dev-group-hint', text: t('perms.backends_hint') }),
    el('div', { class: 'pe-radios', role: 'radiogroup', 'aria-label': t('perms.backends') }, [
      radio('all', t('perms.backends_all')), radio('only', t('perms.backends_only'))]),
    list, err, el('div', { class: 'pe-actions' }, [save]));
  paintList();
  return box;
}

// ------------------------------------------------------------------ DOM: when locked, show

/** Profiles that can be the default one: an Admin opens without a credential there, so no. */
/** Who may be the default profile: the server's rule (auth.check_lock_target) mirrored. An Admin
 *  never (it would open on every device); a profile with a PIN or password never (it would open
 *  without it); a disabled profile never. */
export const defaultCandidates = (profiles) =>
  (profiles || []).filter((p) => p.role !== 'admin' && !p.disabled && (p.lock_target || !(p.has_pin || p.has_password)));

/** The non-Admin profiles the picker leaves out because they carry a PIN or password. The current
 *  default stays listed whatever it carries (an Admin may give it a PIN afterwards; unsigned devices
 *  then see the sign-in screen until it is cleared), so the picker shows the state as it is. */
export const pinnedOut = (profiles) =>
  (profiles || []).filter((p) => p.role !== 'admin' && !p.disabled && !p.lock_target && (p.has_pin || p.has_password));

/** A settings refusal as one line: the same sentence comes back under every key of the PATCH
 *  (``people.lock_target`` and ``people.default_profile``), so keys are dropped and duplicates folded. */
export function lockRefusalText(e) {
  if (e && e.errors && typeof e.errors === 'object') {
    const seen = [];
    for (const v of Object.values(e.errors)) {
      const sentence = String(v || '').trim();
      if (sentence && !seen.includes(sentence)) seen.push(sentence);
    }
    if (seen.length) return seen.join(' ');
  }
  return errorText(e);
}

/** Where the lock screen goes now, from the profile rows (`lock_target` marks the default). */
export function lockState(profiles) {
  const cur = (profiles || []).find((p) => p.lock_target);
  return { target: cur ? 'default_profile' : 'profile_list', profileId: cur ? cur.id : '' };
}

/**
 * "When locked, show: [DisPatch (the default profile) / the profile list]".
 *   deps: { profiles, toast, confirm, onChanged(), w5? }
 * Applies on change through PATCH /api/settings (default_profile first, so a single request
 * names the profile before it becomes the target).
 */
export function lockTargetGroup(deps) {
  const w5 = deps.w5 || realW5;
  const profiles = deps.profiles || [];
  const cands = defaultCandidates(profiles);
  const state = lockState(profiles);
  let target = state.target;
  let pid = state.profileId || (cands[0] ? cands[0].id : '');
  const name = uidn('pe-lt');
  const err = el('div', { class: 'lock-error people-error', role: 'alert', hidden: '' });
  const pick = el('select', { class: 'ui-select pe-lock-pick', 'aria-label': t('people.lock_default_pick') },
    cands.map((p) => el('option', { value: p.id, text: p.name })));
  pick.value = pid;
  const fence = el('p', { class: 'dev-row-hint pe-fence', role: 'note' });
  const paintFence = () => {
    const p = cands.find((x) => x.id === pid);
    const lab = group.querySelector('.pe-lock-default-label');
    if (lab) lab.textContent = p ? t('lock.target_default', { name: p.name }) : t('people.lock_default_none');
    fence.hidden = target !== 'default_profile' || !p;
    fence.textContent = p ? t('perms.lock_target_fence', { name: p.name }) : '';
    pick.disabled = !cands.length;
  };

  const radio = (value, label, extra) => {
    const id = uidn('pe-lr');
    const input = el('input', { type: 'radio', name, id, value, class: 'ui-radio' });
    input.checked = target === value;
    input.disabled = value === 'default_profile' && !cands.length;
    input.addEventListener('change', () => { if (input.checked) apply(value, pid); });
    return el('div', { class: 'dev-row pe-lock-row' }, [
      el('label', { class: 'ui-check pe-lock-label', for: id }, [input, ' ',
        el('strong', { class: value === 'default_profile' ? 'pe-lock-default-label' : '', text: label })]), extra]);
  };

  async function apply(nextTarget, nextPid) {
    err.hidden = true;
    const prev = { target, pid };
    const p = cands.find((x) => x.id === nextPid);
    if (nextTarget === 'default_profile' && p && (prev.target !== 'default_profile' || prev.pid !== nextPid)
      && !await deps.confirm(t('people.lock_confirm', { name: p.name }), { danger: false })) {
      repaint(prev.target, prev.pid);
      return;
    }
    try {
      await w5.patchSettings({ 'people.default_profile': nextPid || '', 'people.lock_target': nextTarget });
      target = nextTarget;
      pid = nextPid;
      deps.toast(t('people.lock_saved'));
      paintFence();
      if (deps.onChanged) deps.onChanged();
    } catch (e) {
      err.textContent = lockRefusalText(e);
      err.hidden = false;
      repaint(prev.target, prev.pid);
    }
  }
  function repaint(tg, id) {
    target = tg; pid = id;
    pick.value = id;
    for (const r of group.querySelectorAll(`input[name="${name}"]`)) r.checked = r.value === tg;
    paintFence();
  }
  // Choosing a profile in the list is choosing it as the default.
  pick.addEventListener('change', () => apply('default_profile', pick.value));

  const group = el('section', { class: 'dev-group people-lock', id: 'people-lock', 'data-anchor': 'lock-target',
    'aria-labelledby': 'people-lock-h' }, [
    el('h3', { class: 'dev-group-title', id: 'people-lock-h', text: t('lock.target_title') }),
    el('p', { class: 'dev-group-hint', text: t('lock.target_hint') }),
    el('div', { class: 'dev-group-body', role: 'radiogroup', 'aria-labelledby': 'people-lock-h' }, [
      radio('default_profile', '', pick),
      radio('profile_list', t('lock.target_list'), null),
    ]),
    cands.length ? null : el('p', { class: 'dev-row-hint', text: t('people.lock_none') }),
    pinnedOut(profiles).length ? el('p', { class: 'dev-row-hint pe-lock-pin-hint', role: 'note',
      text: t('people.lock_pin_hint', { names: pinnedOut(profiles).map((p) => p.name).join(', ') }) }) : null,
    fence, err,
  ]);
  paintFence();
  return group;
}

// ------------------------------------------------------------------ mounting

let shared = {};

/** Called once at boot with the shell's helpers. */
export function initPeoplePerms(deps = {}) {
  shared = { ...shared, ...deps };
}

/**
 * The People pane was built: add "When locked, show" under the list, and keep it in step
 * with it (people.js fires `people:changed` on the pane, with the fresh rows, after every
 * reload). The lock group asks people.js to reload with `people:refresh`.
 */
export async function renderPeoplePerms(pane, deps = {}) {
  if (!pane) return;
  const anchor = pane.querySelector('.people-editor-host');
  if (!anchor) return;                         // the pane is the error / loading state
  const d = { ...shared, ...deps };
  const a = d.api || realApi;
  const paint = (host, profiles) => host.replaceChildren(lockTargetGroup({
    profiles, toast: d.toast || (() => {}), confirm: d.confirm || (async () => true), w5: d.w5,
    onChanged: () => pane.dispatchEvent(new CustomEvent('people:refresh')),
  }));
  let host = pane.querySelector('.people-lock-host');
  if (!host) {
    host = el('div', { class: 'people-lock-host' });
    anchor.after(host);
  }
  if (!pane._peLockWired) {
    pane._peLockWired = true;
    pane.addEventListener('people:changed', (ev) => {
      const h = pane.querySelector('.people-lock-host');
      if (h && ev.detail && ev.detail.profiles) paint(h, ev.detail.profiles);
    });
  }
  let data;
  try { data = await a.people(); } catch { return; }
  if (host.isConnected) paint(host, data.profiles || []);
}
