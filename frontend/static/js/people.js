// Settings -> People & Access (Admins only).
//
// Groups in the Settings modal's own .dev-group / .dev-row vocabulary, so every palette
// applies and this file carries no colour:
//
//   People        every profile with its type (Admin / User / Basic), the protected first
//                 Admin, the lock-screen profile, PIN / password indicators; create, edit
//                 (name, type, picture, PIN, password, sign-out time, disabled), the
//                 permission toggles, household bots, allowed backends, Reset this profile,
//                 delete, and "sign out everywhere".
//   When locked   "When locked, show: [the default profile / the profile list]" - drawn by
//                 people-perms.js (renderPeoplePerms, run by settings-nav SECTION_HOOKS).
//   Devices       the remembered devices and Forget (a device is not bound to a profile).
//   Activity      per-profile counts for the last 7 days (never message content) and the
//                 admin audit log.
//   Machine access (Admins) the API key, rotated on demand and shown once, and the on-box
//                 trust switch.
//
// The server enforces everything here (type changes, the wipe, the protected Admin, the
// lock-target fence). This module only decides what to OFFER. Chats are private to their
// profile: an Admin sees counts, never content, and the dialogs say so.
//
// Pure-ish: nothing runs at import time. App-shaped things arrive through
// `deps` ({ toast, confirm, role, onChanged?, api?, w5?, extraGroups?, collapseActivity? }),
// which is also how the tests drive it without main.js.

import { api as realApi } from './api.js?v=36';
import { w5 as realW5 } from './w5-api.js?v=2';
import { t, has, relTime, fullTimestamp } from './i18n.js?v=5';
import { el } from './util.js?v=21';
import { disclosureText } from './host-notice.js?v=3';
import { ACTIVITY_COLUMNS, activityRows, activityTotals, avatarSpec } from './profiles.js?v=3';
import {
  askDialog, countsTally, permTables, permissionsPanel, householdPanel, backendsPanel,
  profileCalls, errorText,
} from './people-perms.js?v=2';

let seq = 0;          // a newer mount supersedes a slower, older one
let uid = 0;
const nextId = (p) => `${p}-${++uid}`;

/** Who may use this pane: the protected first Admin ("owner" in the 3.x words) and Admins. */
const isAdminActor = (role) => role === 'admin' || role === 'owner';

/** The people table shows no "limit hits" column: 4.0 has no limits to hit. */
const COLUMNS = ACTIVITY_COLUMNS;

function errText(e) {
  if (!e) return '';
  // 3.0 error envelope: a code we have words for beats the raw detail.
  const known = {
    incorrect_credential: 'signin.wrong',
    permission_denied: 'people.err_denied',
  };
  if (e.code && known[e.code]) return t(known[e.code]);
  return errorText(e);
}

const hintLine = (text, cls = '') =>
  el('span', { class: 'dev-row-hint' + (cls ? ` ${cls}` : ''), text });

/** The line a non-Admin profile is told about what an Admin sees (one mapping, in
 *  host-notice.js: the sign-in popup and Settings -> Account show it too). */
export { disclosureText };

function group(id, title, hint, body) {
  return el('section', { class: 'dev-group', id, 'aria-labelledby': `${id}-h` }, [
    el('h3', { class: 'dev-group-title', id: `${id}-h`, text: title }),
    hint ? el('p', { class: 'dev-group-hint', text: hint }) : null,
    body,
  ]);
}

/** A labelled control. The label is a real <label for>, never a placeholder. */
function field(labelText, input, hint) {
  if (!input.id) input.id = nextId('pf');
  const parts = [el('label', { for: input.id, text: labelText }), input];
  if (hint) {
    const h = el('span', { class: 'people-field-hint', id: `${input.id}-hint`, text: hint });
    input.setAttribute('aria-describedby', h.id);
    parts.push(h);
  }
  return el('div', { class: 'sec-field people-field' }, parts);
}

const numberInput = (value, extra = {}) =>
  el('input', { type: 'number', inputmode: 'numeric', min: '0', step: '1', value: value || '', ...extra });

// ---- pure ------------------------------------------------------------------

/** The profile types, in the order the select lists them. */
export const TYPES = Object.freeze(['user', 'basic', 'admin']);
export const typeLabel = (role) => t(`people.type_${role}`);
export const typeHint = (role) => t(`people.type_${role}_hint`);

/** Does the actor manage this row? Every Admin manages every profile but THE protected one. */
export const canEditRow = (actorRole, p, meId) =>
  isAdminActor(actorRole) && (!p.protected || p.id === meId);

const wholeNumber = (v) => /^\d+$/.test(String(v).trim());

/** The create form -> POST /api/people. PINs and passwords are optional on every type. */
export function createBody(form) {
  const errors = {};
  const name = String(form.name || '').trim();
  if (!name) errors.name = 'required';
  const role = form.role;
  const body = { name, role };
  if (form.pin) {
    if (String(form.pin).length < 4) errors.pin = 'min_4'; else body.pin = String(form.pin);
  }
  if (form.password) {
    if (role !== 'admin') errors.password = 'role';
    else if (String(form.password).length < 8) errors.password = 'min_8';
    else body.password = String(form.password);
  }
  if (form.avatar) body.avatar = String(form.avatar).trim();
  const sm = String(form.sessionMinutes || '').trim();
  if (sm !== '') {
    if (!wholeNumber(sm) || Number(sm) < 1 || Number(sm) > 43200) errors.sessionMinutes = 'range_1_43200';
    else body.session_timeout = Number(sm) * 60;
  }
  if (form.householdBots !== undefined && form.householdBots !== null && role !== 'admin') {
    body.household_bots = form.householdBots;
  }
  return { body, errors };
}

/** The edit form -> PATCH /api/people/{id}: only what changed. The type has its own route. */
export function patchBody(person, form) {
  const errors = {};
  const patch = {};
  const name = String(form.name || '').trim();
  if (!name) errors.name = 'required';
  else if (name !== person.name) patch.name = name;
  const avatar = String(form.avatar || '').trim();
  if (avatar !== (person.avatar || '')) patch.avatar = avatar;
  const sm = String(form.sessionMinutes || '').trim();
  const before = Number.isInteger(person.session_timeout) ? Math.round(person.session_timeout / 60) : null;
  if (sm !== '') {
    if (!wholeNumber(sm) || Number(sm) < 1 || Number(sm) > 43200) errors.sessionMinutes = 'range_1_43200';
    else if (Number(sm) !== before) patch.session_timeout = Number(sm) * 60;
  }
  if (typeof form.disabled === 'boolean' && form.disabled !== !!person.disabled) patch.disabled = form.disabled;
  return { patch, errors };
}

const FORM_ERRORS = {
  required: 'people.err_required', min_4: 'people.err_min_4', min_8: 'people.err_min_8',
  range_1_43200: 'people.err_range_1_43200',
};
const formError = (errors) => {
  const first = Object.entries(errors)[0];
  if (!first) return '';
  const [k, code] = first;
  return FORM_ERRORS[code] ? t(FORM_ERRORS[code], { field: k }) : String(code);
};

export function avatarNode(profile, cls = 'people-avatar') {
  const spec = avatarSpec(profile);
  if (spec.kind === 'img') {
    return el('img', { class: cls, src: spec.src, alt: '', width: '32', height: '32', draggable: 'false' });
  }
  return el('span', { class: `${cls} people-avatar-text`, 'aria-hidden': 'true', text: spec.text });
}

function typeBadge(role) {
  return el('span', { class: `people-role people-role-${role}`, text: typeLabel(role) });
}

// ---- People ---------------------------------------------------------------

function indicators(p) {
  const out = [];
  out.push(hintLine(p.has_pin ? t('people.pin_set') : t('people.pin_none'), 'people-ind'));
  if (p.role === 'admin') {
    out.push(hintLine(p.has_password ? t('people.password_set') : t('people.password_none'), 'people-ind'));
  }
  if (p.disabled) out.push(hintLine(t('people.disabled_tag'), 'people-ind people-disabled'));
  return out;
}

function personRow(p, ctx) {
  const mine = p.id === ctx.data.me;
  const edit = el('button', {
    type: 'button', class: 'btn-secondary', text: t('people.edit'),
    'aria-label': t('people.edit_aria', { name: p.name }),
  });
  edit.addEventListener('click', () => ctx.openEditor(p));
  const warn = p.role === 'admin' && p.no_credential
    ? hintLine(t('people.no_credential_warning'), 'people-warn') : null;
  return el('div', { class: 'dev-row people-row' + (p.disabled ? ' people-row-off' : ''), dataset: { person: p.id } }, [
    avatarNode(p),
    el('span', { class: 'dev-row-text' }, [
      el('strong', {}, [p.name, ' ', typeBadge(p.role),
        p.protected ? el('span', { class: 'ui-badge people-protected', title: t('people.protected_hint'), text: t('people.protected') }) : null,
        p.lock_target ? el('span', { class: 'ui-badge people-lockscreen', text: t('people.lock_target_badge') }) : null,
        mine ? el('span', { class: 'ui-badge', text: t('people.you_tag') }) : null]),
      el('span', { class: 'people-inds' }, indicators(p)),
      warn,
    ]),
    canEditRow(ctx.role, p, ctx.data.me) ? edit : null,
  ]);
}

function listBody(ctx) {
  const { people } = ctx.data;
  const rows = people.length
    ? people.map((p) => personRow(p, ctx))
    : [el('div', { class: 'dev-row' }, [hintLine(t('people.empty'))])];
  return el('div', { class: 'dev-group-body', id: 'people-list' }, rows);
}

// ---- the dialogs (counts, never content) ----------------------------------------

async function countsOrError(ctx, fn) {
  try { return await fn(); } catch (e) {
    ctx.toast(t('people.counts_failed', { error: errText(e) }), true);
    return null;
  }
}

/** User -> Basic: the downgrade dialog, with the counts including File Server files and cloned voices. */
async function confirmDowngrade(ctx, person, pv) {
  const c = pv.counts || {};
  const r = await askDialog({
    title: t('people.downgrade_title', { name: person.name }),
    body: t('people.downgrade_confirm', { bots: c.bots || 0, chats: c.chats || 0, files: c.files || 0, voices: c.voices || 0 }),
    tally: countsTally(c),
    notes: [t('people.downgrade_kept')],
    okText: t('people.downgrade_button'),
  });
  return r === 'ok';
}

async function resetProfile(ctx, person, showErr) {
  const pv = await countsOrError(ctx, () => profileCalls.resetPreview(person.id));
  if (!pv) return;
  const c = pv.counts || {};
  const notes = [t(pv.outcome === 'household' ? 'people.reset_outcome_household' : 'people.reset_outcome_atlas', { name: person.name })];
  if (pv.keeps_voices !== false) notes.push(t('reset.voices_kept'));
  if (person.lock_target) notes.push(t('reset.shared_warning'));
  const r = await askDialog({
    title: t('people.reset_title', { name: person.name }),
    body: `${t('reset.admin_desc')} ${t('reset.confirm_counts', { bots: c.bots || 0, chats: c.chats || 0, pictures: c.pictures || 0, files: c.files || 0 })}`,
    tally: countsTally(c, { voices: false }),
    notes,
    okText: t('reset.confirm_button'),
  });
  if (r !== 'ok') return;
  try {
    await profileCalls.reset(person.id, pv.token);
    ctx.toast(t('people.reset_done', { name: person.name }));
    ctx.closeEditor();
    await ctx.reload();
  } catch (e) {
    if (e && e.code === 'reset_stale') { showErr(t('reset.stale')); return; }
    showErr(errText(e));
  }
}

async function deleteProfile(ctx, person, showErr) {
  const pv = await countsOrError(ctx, () => ctx.api.deletePreview(person.id));
  if (!pv) return;
  const r = await askDialog({
    title: t('people.delete_title', { name: person.name }),
    body: t('people.delete_body', { name: person.name }),
    tally: countsTally(pv.counts),
    okText: t('people.delete_button'),
  });
  if (r !== 'ok') return;
  try {
    await ctx.api.deletePerson(person.id, pv.token);
    ctx.toast(t('people.deleted', { name: person.name }));
    ctx.closeEditor();
    await ctx.reload();
  } catch (e) { showErr(errText(e)); }
}

// ---- the editor ---------------------------------------------------------------

/** The create / edit form. `person` null = create. */
function editorPanel(ctx, person) {
  const creating = !person;
  const mine = !creating && person.id === ctx.data.me;
  const limited = mine || (!creating && person.protected);          // own profile: name, picture, reset
  const title = creating ? t('people.new_title') : t('people.edit_title', { name: person.name });

  const name = el('input', { type: 'text', maxlength: '40', value: person ? person.name : '', autocomplete: 'off' });
  const role = el('select', { class: 'ui-select people-type' }, TYPES.map((r) => el('option', { value: r, text: typeLabel(r) })));
  role.value = person ? person.role : 'user';
  if (limited) role.disabled = true;
  const roleHint = el('span', { class: 'people-field-hint', id: nextId('pe-rh') });
  role.setAttribute('aria-describedby', roleHint.id);
  const avatar = el('input', { type: 'text', maxlength: '8', value: person && !/^\//.test(person.avatar || '') ? (person.avatar || '') : '', autocomplete: 'off' });
  const pin = el('input', { type: 'password', inputmode: 'numeric', autocomplete: 'new-password', maxlength: '32' });
  const password = el('input', { type: 'password', autocomplete: 'new-password', maxlength: '128' });
  const fSession = numberInput(person && Number.isInteger(person.session_timeout) ? String(Math.round(person.session_timeout / 60)) : '', { min: '1', max: '43200' });
  const disabled = el('input', { type: 'checkbox' });
  disabled.checked = !!(person && person.disabled);

  const err = el('div', { class: 'lock-error people-error people-editor-error', role: 'alert', 'aria-live': 'assertive', hidden: '' });
  const showErr = (m) => { err.textContent = m || ''; err.hidden = !m; };

  const pinField = field(t(creating ? 'people.pin_new' : 'people.pin_change'), pin, t('people.pin_hint'));
  if (creating) pinField.querySelector('label').textContent = t('people.pin_optional');
  const pwField = field(t(creating ? 'people.password_new' : 'people.password_change'), password, t('people.password_hint'));
  if (creating) pwField.querySelector('label').textContent = t('people.password_optional');
  const noCred = el('p', { class: 'people-field-hint people-warn', role: 'note', hidden: '', text: t('people.no_credential_warning') });
  const syncRole = () => {
    const r = role.value;
    roleHint.textContent = r === 'user' && person && person.role === 'basic'
      ? `${typeHint(r)} ${t('people.upgrade_note')}` : typeHint(r);
    pwField.hidden = r !== 'admin' || limited;
    pinField.hidden = limited;
    const none = creating && r === 'admin' && !pin.value && !password.value;
    noCred.hidden = !none;
  };
  role.addEventListener('change', syncRole);
  pin.addEventListener('input', syncRole);
  password.addEventListener('input', syncRole);

  // Sections that need data from the server fill in after the form is up.
  const lower = el('div', { class: 'pe-lower' });
  let households = null;             // the create form's household picker (getValue())
  const fill = async () => {
    const [chars, conns] = await Promise.all([
      ctx.w5.allCharacters().catch(() => ({ characters: [] })),
      ctx.w5.connections().catch(() => ({ connections: [] })),
    ]);
    const bots = chars.characters || [];
    if (creating) {
      const draft = { role: role.value, household_bots: role.value === 'basic' ? [] : 'all' };
      const mountHouse = () => {
        const r = role.value;
        const host = lower.querySelector('.pe-house-host');
        if (r === 'admin') { host.replaceChildren(); households = null; return; }
        const pp = householdPanel({ ...draft, role: r, household_bots: r === 'basic' ? [] : 'all' }, { bots, toast: ctx.toast });
        households = pp;
        host.replaceChildren(pp);
      };
      lower.replaceChildren(el('div', { class: 'pe-house-host' }));
      role.addEventListener('change', mountHouse);
      mountHouse();
      return;
    }
    const parts = [];
    if (!limited) {
      parts.push(permissionsPanel(person, { tables: ctx.tables, api: ctx.api, toast: ctx.toast, confirm: ctx.confirm,
        onChanged: () => ctx.refreshRows() }));
      if (person.role !== 'admin') {
        parts.push(householdPanel(person, { bots, api: ctx.api, toast: ctx.toast, save: true }));
      }
      if (person.role === 'user') {
        parts.push(backendsPanel(person, { connections: conns.connections || [], toast: ctx.toast, calls: ctx.calls }));
      }
    }
    lower.replaceChildren(...parts);
  };

  const save = el('button', { type: 'button', class: 'btn-primary', text: t(creating ? 'people.create' : 'common.save') });
  const cancel = el('button', { type: 'button', class: 'btn-secondary', text: t('common.cancel') });
  const actions = [save, cancel];
  const extra = [];

  if (!creating) {
    const reset = el('button', { type: 'button', class: 'btn-secondary', text: t('people.reset_button') });
    reset.addEventListener('click', () => resetProfile(ctx, person, showErr));
    extra.push(reset);
    if (!limited) {
      const out = el('button', { type: 'button', class: 'btn-secondary', text: t('people.sign_out_everywhere') });
      out.addEventListener('click', async () => {
        try {
          await ctx.api.signOutPerson(person.id);
          ctx.toast(t('people.signed_out', { name: person.name }));
        } catch (e) { showErr(errText(e)); }
      });
      const del = el('button', { type: 'button', class: 'btn-secondary danger-text', text: t('people.delete') });
      del.addEventListener('click', () => deleteProfile(ctx, person, showErr));
      extra.push(out, del);

      const setPin = el('button', { type: 'button', class: 'btn-secondary', text: t('people.pin_save') });
      setPin.addEventListener('click', async () => {
        if (String(pin.value).length < 4) { showErr(t('people.err_pin_short')); return; }
        try {
          await ctx.api.setPersonPin(person.id, pin.value);
          pin.value = '';
          ctx.toast(t('people.pin_saved', { name: person.name }));
          await ctx.reload({ keep: person.id });
        } catch (e) { showErr(errText(e)); }
      });
      extra.unshift(setPin);
      if (person.has_pin) {
        const clr = el('button', { type: 'button', class: 'btn-secondary', text: t('people.pin_remove') });
        clr.addEventListener('click', async () => {
          try {
            await ctx.api.clearPersonPin(person.id);
            ctx.toast(t('people.pin_removed', { name: person.name }));
            await ctx.reload({ keep: person.id });
          } catch (e) { showErr(errText(e)); }
        });
        extra.splice(1, 0, clr);
      }
      if (person.role === 'admin') {
        const setPw = el('button', { type: 'button', class: 'btn-secondary', text: t('people.password_save') });
        setPw.addEventListener('click', async () => {
          if (String(password.value).length < 8) { showErr(t('people.err_password_short')); return; }
          try {
            await ctx.api.setPersonPassword(person.id, password.value);
            password.value = '';
            ctx.toast(t('people.password_saved', { name: person.name }));
            await ctx.reload({ keep: person.id });
          } catch (e) { showErr(errText(e)); }
        });
        extra.unshift(setPw);
      }
    }
  }

  const collect = () => ({
    name: name.value, role: role.value, avatar: avatar.value, pin: pin.value, password: password.value,
    sessionMinutes: fSession.value, disabled: disabled.checked,
    householdBots: households ? households.getValue() : undefined,
  });

  save.addEventListener('click', async () => {
    showErr('');
    const f = collect();
    save.disabled = true;
    try {
      if (creating) {
        const { body, errors } = createBody(f);
        if (Object.keys(errors).length) { showErr(formError(errors)); return; }
        const made = await ctx.api.createPerson(body);
        pin.value = ''; password.value = '';
        ctx.toast(t('people.created', { name: body.name }));
        ctx.closeEditor();
        await ctx.reload({ keep: made && made.id });
      } else {
        const { patch, errors } = patchBody(person, f);
        if (Object.keys(errors).length) { showErr(formError(errors)); return; }
        // The type has its own route: a preview first, and User -> Basic deletes the profile's
        // own bots and chats (and cloned voices), so it is confirmed with the counts.
        const newRole = !limited && f.role !== person.role ? f.role : null;
        if (newRole) {
          const pv = await ctx.api.rolePreview(person.id, newRole);
          const body = { role: newRole };
          if (pv && pv.deletes) {
            if (!await confirmDowngrade(ctx, person, pv)) return;
            body.confirm_token = pv.token;
          }
          await ctx.api.changeRole(person.id, body);
        }
        if (Object.keys(patch).length) await ctx.api.patchPerson(person.id, patch);
        if (!newRole && !Object.keys(patch).length) { ctx.closeEditor(); return; }
        ctx.toast(t('people.saved', { name: f.name.trim() || person.name }));
        ctx.closeEditor();
        await ctx.reload(newRole ? { keep: person.id } : {});
      }
    } catch (e) {
      showErr(errText(e));
    } finally {
      save.disabled = false;
    }
  });
  cancel.addEventListener('click', () => ctx.closeEditor());

  const root = el('div', { class: 'people-editor dev-group-body', id: 'people-editor', role: 'group', 'aria-label': title }, [
    el('div', { class: 'people-form' }, [
      el('h4', { class: 'people-editor-title', text: title }),
      limited ? el('p', { class: 'people-field-hint', text: t(mine ? 'people.self_note' : 'people.protected_note') }) : null,
      field(t('people.name'), name),
      field(t('people.type_label'), role), roleHint,
      field(t('people.avatar'), avatar, t('people.avatar_hint')),
      pinField,
      pwField,
      noCred,
      limited ? null : field(t('people.session_minutes'), fSession, t('people.session_hint')),
      creating || limited ? null : el('label', { class: 'sec-remember-row people-disabled-row' }, [
        disabled, el('span', { text: t('people.disabled_label') })]),
      lower,
      err,
      el('div', { class: 'bm-actions people-actions' }, actions),
      extra.length ? el('div', { class: 'bm-actions people-actions people-actions-extra' }, extra) : null,
    ]),
  ]);
  syncRole();
  fill().catch((e) => lower.replaceChildren(hintLine(t('people.load_failed', { error: errText(e) }))));
  return { root, focus: () => name.focus() };
}

// ---- Devices ------------------------------------------------------------------

function deviceGroup(ctx) {
  const dv = ctx.devices || { devices: [], this_device: null };
  // A device is not bound to a profile (unsigned devices land on the lock target, set above);
  // only the list of remembered devices and Forget remain.
  const rows = [];

  for (const d of dv.devices) {
    const label = d.label || t('people.device_unnamed');
    const forget = el('button', {
      type: 'button', class: 'btn-secondary', text: t('people.device_forget'),
      'aria-label': t('people.device_forget_aria', { name: label }),
    });
    forget.addEventListener('click', async () => {
      if (!await ctx.confirm(t('people.device_forget_confirm', { name: label }), { danger: true })) return;
      try {
        await ctx.api.forgetDevice(d.id);
        ctx.toast(t('people.device_forgotten'));
        await ctx.reloadDevices();
      } catch (e) { ctx.toast(errText(e), true); }
    });
    rows.push(el('div', { class: 'dev-row people-device', dataset: { device: d.id } }, [
      el('span', { class: 'dev-row-text' }, [
        el('strong', {}, [label, d.this_device ? ` (${t('people.device_this')})` : '']),
        d.last_seen ? hintLine(t('people.device_seen', { when: relTime(d.last_seen) })) : null,
      ]),
      el('span', { class: 'people-row-actions' }, [forget]),
    ]));
  }
  return group('people-devices', t('people.devices_title'), t('people.devices_intro'),
    el('div', { class: 'dev-group-body', id: 'people-devices-body' }, rows));
}

// ---- Activity -----------------------------------------------------------------

function activityTable(rows) {
  const head = el('tr', {}, [
    el('th', { scope: 'col', text: t('people.col_day') }),
    ...COLUMNS.map((c) => el('th', { scope: 'col', class: 'people-num', text: t(`people.col_${c}`) })),
  ]);
  const body = rows.map((r) => el('tr', {}, [
    el('th', { scope: 'row', text: r.day }),
    ...COLUMNS.map((c) => el('td', { class: 'people-num', text: String(r.counts[c]) })),
  ]));
  const totals = activityTotals(rows);
  const foot = el('tr', { class: 'people-total' }, [
    el('th', { scope: 'row', text: t('people.col_total') }),
    ...COLUMNS.map((c) => el('td', { class: 'people-num', text: String(totals[c]) })),
  ]);
  return el('div', { class: 'people-table-wrap', tabindex: '0', role: 'region', 'aria-label': t('people.activity_title') }, [
    el('table', { class: 'people-table' }, [
      el('caption', { class: 'people-caption', text: t('people.activity_caption') }),
      el('thead', {}, [head]),
      el('tbody', {}, body),
      rows.length ? el('tfoot', {}, [foot]) : null,
    ]),
  ]);
}

function auditList(entries, ctx) {
  if (!entries.length) return el('div', { class: 'dev-row' }, [hintLine(t('people.audit_empty'))]);
  const nameOf = (id) => {
    const p = id && ctx.data.people.find((x) => x.id === id);
    return p ? p.name : '';
  };
  return el('ol', { class: 'people-audit' }, entries.map((e) => el('li', {}, [
    el('time', { datetime: e.ts, text: fullTimestamp(e.ts) }),
    ' ',
    el('strong', { text: auditKind(e.kind) }),
    nameOf(e.profile_id) ? el('span', { class: 'people-audit-who', text: ` ${nameOf(e.profile_id)}` }) : null,
    e.subject ? el('span', { class: 'people-audit-sub', text: ` ${e.subject}` }) : null,
    e.detail ? el('span', { class: 'people-audit-detail', text: ` ${e.detail}` }) : null,
  ])));
}

// A kind we have words for is localized; anything else is shown as the server
// wrote it, so a new audit kind is never invisible.
function auditKind(kind) {
  // A dotted kind (plugin.approve) would walk a nested path; its key uses `_`.
  const key = `people.audit_${String(kind).replace(/\./g, '_')}`;
  return has(key) ? t(key) : String(kind);
}

function activityGroup(ctx) {
  const select = el('select', { id: 'people-activity-who', class: 'ui-select' }, [
    el('option', { value: '', text: t('people.activity_everyone') }),
    ...ctx.data.people.map((p) => el('option', { value: p.id, text: p.name })),
  ]);
  const holder = el('div', { class: 'people-activity-holder', 'aria-live': 'polite' });
  const audit = el('div', { class: 'people-audit-holder' });

  async function loadSummary() {
    holder.replaceChildren(hintLine(t('common.loading')));
    try {
      const r = await ctx.api.peopleActivity({ profileId: select.value || undefined, days: 7 });
      const rows = activityRows(r.summary, 7);
      holder.replaceChildren(rows.length ? activityTable(rows) : hintLine(t('people.activity_empty')));
    } catch (e) {
      holder.replaceChildren(hintLine(t('people.load_failed', { error: errText(e) })));
    }
  }
  async function loadAudit() {
    try {
      const r = await ctx.api.peopleAudit();
      audit.replaceChildren(auditList(r.entries || [], ctx));
    } catch (e) {
      audit.replaceChildren(hintLine(t('people.load_failed', { error: errText(e) })));
    }
  }
  select.addEventListener('change', loadSummary);
  loadSummary();
  loadAudit();

  return group('people-activity', t('people.activity_title'), t('people.activity_intro'),
    el('div', { class: 'dev-group-body dev-group-body--block', id: 'people-activity-body' }, [
      el('div', { class: 'dev-row dev-row-block' }, [
        el('label', { for: 'people-activity-who', class: 'dev-row-text' }, [el('strong', { text: t('people.activity_who') })]),
        select,
      ]),
      el('div', { class: 'dev-row dev-row-block' }, [holder]),
      el('div', { class: 'dev-row dev-row-block' }, [
        el('h4', { class: 'people-subtitle', text: t('people.audit_title') }),
        audit,
      ]),
    ]));
}

// ---- Machine access (Admins) ---------------------------------------------------------

async function machineGroup(ctx) {
  let info;
  try { info = await ctx.api.apiTokenInfo(); } catch { return null; }
  const status = hintLine(info.has_token ? t('people.machine_has_key') : t('people.machine_no_key'), 'people-machine-status');
  const shown = el('div', { class: 'people-key', hidden: '' });
  const rotate = el('button', { type: 'button', class: 'btn-secondary', text: t(info.has_token ? 'people.machine_rotate' : 'people.machine_create') });
  rotate.addEventListener('click', async () => {
    if (info.has_token && !await ctx.confirm(t('people.machine_rotate_confirm'), { danger: true })) return;
    rotate.disabled = true;
    try {
      const r = await ctx.api.rotateApiToken();
      info.has_token = true;
      status.textContent = t('people.machine_has_key');
      rotate.textContent = t('people.machine_rotate');
      if (r && r.api_token) showKey(shown, r.api_token, ctx);
    } catch (e) { ctx.toast(errText(e), true); } finally { rotate.disabled = false; }
  });

  const trust = el('input', { type: 'checkbox', 'aria-label': t('people.machine_trust_aria') });
  trust.checked = !!info.machine_loopback_trust;
  trust.addEventListener('change', async () => {
    const want = trust.checked;
    trust.disabled = true;
    try {
      await ctx.api.setMachineAccess({ machineLoopbackTrust: want });
      ctx.toast(t(want ? 'people.machine_trust_on' : 'people.machine_trust_off'));
    } catch (e) {
      trust.checked = !want;
      ctx.toast(errText(e), true);
    } finally { trust.disabled = false; }
  });

  const scopes = (info.machine_scopes || []).length
    ? hintLine(t('people.machine_scopes', { scopes: info.machine_scopes.join(', ') }))
    : null;

  return group('people-machine', t('people.machine_title'), t('people.machine_intro'),
    el('div', { class: 'dev-group-body', id: 'people-machine-body' }, [
      el('div', { class: 'dev-row dev-row-block' }, [
        el('span', { class: 'dev-row-text' }, [el('strong', { text: t('people.machine_key') }), status, scopes]),
        rotate,
      ]),
      el('div', { class: 'dev-row dev-row-block' }, [shown]),
      el('div', { class: 'dev-row plugins-row' }, [
        el('span', { class: 'dev-row-text' }, [
          el('strong', { text: t('people.machine_trust') }),
          hintLine(t('people.machine_trust_hint')),
        ]),
        trust,
      ]),
    ]));
}

/** The new key, once, with a Copy button. It is never stored by the page. */
function showKey(host, key, ctx) {
  const input = el('input', { type: 'text', readonly: '', value: key, class: 'people-key-input', dir: 'ltr',
    'aria-label': t('people.machine_key_label'), autocomplete: 'off', spellcheck: 'false' });
  const copy = el('button', { type: 'button', class: 'btn-primary', text: t('people.machine_copy') });
  copy.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(key);
      ctx.toast(t('people.machine_copied'));
    } catch {
      input.focus(); input.select();
      ctx.toast(t('people.machine_copy_manual'));
    }
  });
  const done = el('button', { type: 'button', class: 'btn-secondary', text: t('people.machine_hide') });
  done.addEventListener('click', () => { host.replaceChildren(); host.hidden = true; });
  host.replaceChildren(
    el('p', { class: 'people-key-warn', role: 'status', text: t('people.machine_once') }),
    input,
    el('div', { class: 'bm-actions' }, [copy, done]),
  );
  host.hidden = false;
  input.focus(); input.select();
}

// ---- Mount ------------------------------------------------------------------------

/**
 * Settings -> People & Access came on screen: (re)build the pane.
 *   pane  #spane-people
 *   deps  { toast(msg, isErr), confirm(msg, opts) -> Promise<bool>, role,
 *           onChanged?(), api?, w5?, extraGroups?(), collapseActivity? }
 */
export async function mountPeopleSettings(pane, deps) {
  const a = deps.api || realApi;
  const w5 = deps.w5 || realW5;
  const mine = ++seq;
  const intro = el('p', { class: 'muted modal-hint', text: t('people.page_hint') });
  pane.replaceChildren(intro, el('p', { class: 'muted modal-hint', text: t('common.loading') }));
  let data;
  let devices = null;
  try {
    [data, devices] = await Promise.all([a.people(), a.devices().catch(() => null)]);
  } catch (e) {
    if (mine !== seq) return;
    const retry = el('button', { type: 'button', class: 'btn-secondary', text: t('common.retry') });
    retry.addEventListener('click', () => mountPeopleSettings(pane, deps));
    pane.replaceChildren(intro, el('div', { class: 'dev-row dev-row-action', role: 'alert' }, [
      hintLine(t('people.load_failed', { error: errText(e) })), retry]));
    return;
  }
  if (mine !== seq) return;

  const role = deps.role || 'admin';
  const read = (d) => ({
    people: d.profiles || [], me: typeof d.me === 'string' ? d.me : (d.me && d.me.id) || '',
    disclosure: disclosureText(d),
  });
  const ctx = {
    api: a, w5, role, calls: profileCalls, data: read(data), tables: permTables(data),
    devices, toast: deps.toast, confirm: deps.confirm,
    openEditor: () => {}, closeEditor: () => {}, reload: () => {}, reloadDevices: () => {}, refreshRows: () => {},
  };

  const listHost = el('div', { class: 'people-list-host' });
  const editorHost = el('div', { class: 'people-editor-host' });
  const deviceHost = el('div', { class: 'people-device-host' });
  const activityHost = el('div', { class: 'people-activity-host' });
  const machineHost = el('div', { class: 'people-machine-host' });

  const renderList = () => {
    const g = group('people-people', t('people.title'), t('people.page_intro'), listBody(ctx));
    if (ctx.data.disclosure) {
      g.querySelector('.dev-group-hint').after(el('p', { class: 'dev-group-hint people-disclosure', text: ctx.data.disclosure }));
    }
    listHost.replaceChildren(g);
    if (isAdminActor(role)) {
      const add = el('button', { type: 'button', class: 'btn-primary people-add', id: 'people-add', text: t('people.add') });
      add.addEventListener('click', () => ctx.openEditor(null));
      g.append(el('div', { class: 'people-add-row' }, [add]));
    }
  };
  const renderDevices = () => deviceHost.replaceChildren(deviceGroup(ctx));
  const renderActivity = () => activityHost.replaceChildren(activityGroup(ctx));

  ctx.openEditor = (person) => {
    const ed = editorPanel(ctx, person);
    editorHost.replaceChildren(ed.root);
    try { ed.root.scrollIntoView({ block: 'nearest' }); } catch { /* ignore */ }
    ed.focus();
  };
  ctx.closeEditor = () => { editorHost.replaceChildren(); };
  const announce = () => pane.dispatchEvent(new CustomEvent('people:changed', { detail: { profiles: ctx.data.people } }));
  ctx.reload = async ({ keep } = {}) => {
    try {
      const fresh = await a.people();
      ctx.data = read(fresh);
      ctx.tables = permTables(fresh);
    } catch (e) { ctx.toast(errText(e), true); return; }
    renderList();
    renderDevices();
    renderActivity();
    announce();
    if (keep) {
      const p = ctx.data.people.find((x) => x.id === keep);
      if (p) ctx.openEditor(p);
    }
    if (deps.onChanged) deps.onChanged();
  };
  // A permission switch changed a row's badges (the lock-screen profile, the warning): redraw
  // the list without touching the open editor.
  ctx.refreshRows = async () => {
    try {
      ctx.data = read(await a.people());
    } catch { return; }
    renderList();
    announce();
  };
  ctx.reloadDevices = async () => {
    try { ctx.devices = await a.devices(); } catch { /* keep the old list */ }
    renderDevices();
  };
  // The lock-screen group changed who the default profile is.
  pane.addEventListener('people:refresh', () => ctx.reload());

  renderList();
  renderDevices();
  renderActivity();
  // The caller's groups after Devices (the sign-in rules), and Activity & audit as a closed
  // disclosure.
  const extra = (deps.extraGroups && deps.extraGroups()) || {};
  listHost.dataset.anchor = 'people';
  deviceHost.dataset.anchor = 'device';
  machineHost.dataset.anchor = 'machine';
  let activityPart = activityHost;
  if (deps.collapseActivity) {
    activityPart = el('details', { class: 'settings-sub people-activity-sub', dataset: { anchor: 'activity' } }, [
      el('summary', {}, [el('span', { text: t('people.activity_audit') }),
        el('span', { class: 'settings-sub-hint', text: t('people.activity_audit_hint') })]),
      activityHost,
    ]);
  } else {
    activityHost.dataset.anchor = 'activity';
  }
  const parts = [intro, listHost, editorHost, deviceHost, ...(extra.afterDevices || [])];
  if (isAdminActor(role)) {
    const g = await machineGroup(ctx);
    if (mine !== seq) return;
    if (g) machineHost.replaceChildren(g);
    parts.push(machineHost);
  }
  parts.push(activityPart, ...(extra.end || []));
  pane.replaceChildren(...parts);
}
