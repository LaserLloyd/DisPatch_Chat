// Settings -> People & Profiles (owner and admin only).
//
// Four groups in the Settings modal's own .dev-group / .dev-row vocabulary, so
// every palette applies and this file carries no colour:
//
//   People        every profile with its role badge, PIN / password
//                 indicators and disabled state; create, edit (name, role,
//                 PIN, password, budgets, session timeout, disabled), delete,
//                 and "sign out everywhere".
//   This device   "Open this device as..." (binds THIS browser to a Member or
//                 Restricted profile) and the devices the server knows.
//   Activity      per-profile counts for the last 7 days (never message
//                 content) and the admin audit log.
//   Machine access (Owner only) the API key, rotated on demand and shown once,
//                 and the on-box trust switch.
//
// The server enforces everything here (role, budgets, who may create an
// Admin). This module only decides what to OFFER, via profiles.js.
//
// Pure-ish: nothing runs at import time. App-shaped things arrive through
// `deps` ({ toast, confirm, role, onSelfChanged, api? }), which is also how the
// tests drive it without main.js.

import { api as realApi } from './api.js?v=35';
import { t, has, relTime, fullTimestamp } from './i18n.js?v=4';
import { el } from './util.js?v=20';
import {
  ACTIVITY_COLUMNS, activityRows, activityTotals, bindableProfiles, budgetsToForm, canManage,
  createPayload, patchPayload, roleOptions, avatarSpec,
} from './profiles.js?v=1';

let seq = 0;          // a newer mount supersedes a slower, older one
let uid = 0;
const nextId = (p) => `${p}-${++uid}`;

function errText(e) {
  if (!e) return '';
  // 3.0 error envelope: a code we have words for beats the raw detail.
  const known = {
    password_required: 'people.err_password_required',
    incorrect_credential: 'signin.wrong',
    permission_denied: 'people.err_denied',
  };
  if (e.code && known[e.code]) return t(known[e.code]);
  const msg = String(e.message || e).replace(/^\d{3}:\s*/, '');
  return e.fix ? `${msg} ${e.fix}` : msg;
}

const hintLine = (text, cls = '') =>
  el('span', { class: 'dev-row-hint' + (cls ? ` ${cls}` : ''), text });

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
  const kids = [el('label', { for: input.id, text: labelText }), input];
  if (hint) {
    const h = el('span', { class: 'people-field-hint', id: `${input.id}-hint`, text: hint });
    input.setAttribute('aria-describedby', h.id);
    kids.push(h);
  }
  return el('div', { class: 'sec-field people-field' }, kids);
}

const numberInput = (value, extra = {}) =>
  el('input', { type: 'number', inputmode: 'numeric', min: '0', step: '1', value: value || '', ...extra });

const roleLabel = (role) => t(`role.${role}`);

export function avatarNode(profile, cls = 'people-avatar') {
  const spec = avatarSpec(profile);
  if (spec.kind === 'img') {
    return el('img', { class: cls, src: spec.src, alt: '', width: '32', height: '32', draggable: 'false' });
  }
  return el('span', { class: `${cls} people-avatar-text`, 'aria-hidden': 'true', text: spec.text });
}

function roleBadge(role) {
  return el('span', { class: `people-role people-role-${role}`, text: roleLabel(role) });
}

// ---- People ---------------------------------------------------------------

function indicators(p) {
  const out = [];
  out.push(hintLine(p.has_pin ? t('people.pin_set') : t('people.pin_none'), 'people-ind'));
  if (p.role === 'owner' || p.role === 'admin') {
    out.push(hintLine(p.has_password ? t('people.password_set') : t('people.password_none'), 'people-ind'));
  }
  if (p.disabled) out.push(hintLine(t('people.disabled_tag'), 'people-ind people-disabled'));
  return out;
}

function personRow(p, ctx) {
  const manage = canManage(ctx.role, p.role);
  const edit = el('button', {
    type: 'button', class: 'btn-secondary', text: t('people.edit'),
    'aria-label': t('people.edit_aria', { name: p.name }),
  });
  edit.addEventListener('click', () => ctx.openEditor(p));
  return el('div', { class: 'dev-row people-row' + (p.disabled ? ' people-row-off' : ''), dataset: { person: p.id } }, [
    avatarNode(p),
    el('span', { class: 'dev-row-text' }, [
      el('strong', {}, [p.name, ' ', roleBadge(p.role)]),
      el('span', { class: 'people-inds' }, indicators(p)),
    ]),
    manage ? edit : null,
  ]);
}

function listBody(ctx) {
  const { people } = ctx.data;
  const rows = people.length
    ? people.map((p) => personRow(p, ctx))
    : [el('div', { class: 'dev-row' }, [hintLine(t('people.empty'))])];
  return el('div', { class: 'dev-group-body', id: 'people-list' }, rows);
}

/** The create / edit form. `person` null = create. */
function editorPanel(ctx, person) {
  const creating = !person;
  const form = budgetsToForm(person || {});
  const { options, adminBlocked } = roleOptions(ctx.role, ctx.data.owner_has_password);
  const roleChoices = (person && !options.includes(person.role) ? [person.role, ...options] : options);

  const name = el('input', { type: 'text', maxlength: '40', value: person ? person.name : '', autocomplete: 'off' });
  const role = el('select', {}, roleChoices.map((r) => {
    const o = el('option', { value: r, text: roleLabel(r) });
    if ((person ? person.role : 'member') === r) o.selected = true;
    return o;
  }));
  if (person && person.role === 'owner') role.disabled = true;
  const avatar = el('input', { type: 'text', maxlength: '8', value: person && !/^\//.test(person.avatar || '') ? (person.avatar || '') : '', autocomplete: 'off' });
  const pin = el('input', { type: 'password', inputmode: 'numeric', autocomplete: 'new-password', maxlength: '32' });
  const password = el('input', { type: 'password', autocomplete: 'new-password', maxlength: '128' });

  const fMinutes = numberInput(form.minutes, { min: '1', max: '1440' });
  const qStart = el('input', { type: 'time', value: form.quietStart });
  const qEnd = el('input', { type: 'time', value: form.quietEnd });
  const fTurns = numberInput(form.turns, { max: '100000' });
  const fThreads = numberInput(form.threads, { max: '100000' });
  const fFeedback = numberInput(form.feedback, { max: '100000' });
  const fUpload = numberInput(form.uploadMb, { max: '100000' });
  const fSession = numberInput(form.sessionMinutes, { min: '1', max: '43200' });
  const disabled = el('input', { type: 'checkbox' });
  disabled.checked = !!(person && person.disabled);

  const err = el('div', { class: 'lock-error people-error', role: 'alert', 'aria-live': 'assertive', hidden: '' });
  const showErr = (m) => { err.textContent = m || ''; err.hidden = !m; };

  const pinField = field(t(creating ? 'people.pin_new' : 'people.pin_change'), pin, t('people.pin_hint'));
  const pwField = field(t(creating ? 'people.password_new' : 'people.password_change'), password, t('people.password_hint'));
  const syncCredFields = () => {
    const r = role.value;
    // Member and Restricted use a PIN; an Admin uses a password (the Owner
    // sets it). Members must have a PIN, a Restricted profile may go without.
    pinField.hidden = !(r === 'member' || r === 'restricted');
    pwField.hidden = r !== 'admin' || (!creating && ctx.role !== 'owner');
  };
  role.addEventListener('change', syncCredFields);
  syncCredFields();

  const quiet = el('div', { class: 'sec-field people-field people-quiet', role: 'group', 'aria-labelledby': 'people-quiet-l' }, [
    el('span', { class: 'people-group-label', id: 'people-quiet-l', text: t('people.quiet_hours') }),
    el('div', { class: 'people-quiet-row' }, [
      field(t('people.quiet_from'), qStart),
      field(t('people.quiet_to'), qEnd),
    ]),
    el('span', { class: 'people-field-hint', text: t('people.quiet_hint') }),
  ]);

  const budgets = el('fieldset', { class: 'people-budgets' }, [
    el('legend', { text: t('people.budgets') }),
    el('p', { class: 'people-field-hint', text: t('people.budgets_hint') }),
    field(t('people.minutes_per_day'), fMinutes),
    quiet,
    field(t('people.turns_per_day'), fTurns),
    field(t('people.threads_per_day'), fThreads),
    field(t('people.feedback_per_day'), fFeedback),
    field(t('people.upload_mb'), fUpload),
    field(t('people.session_minutes'), fSession, t('people.session_hint')),
  ]);

  const save = el('button', { type: 'button', class: 'btn-primary', text: t(creating ? 'people.create' : 'common.save') });
  const cancel = el('button', { type: 'button', class: 'btn-secondary', text: t('common.cancel') });
  const actions = [save, cancel];
  const extra = [];

  if (!creating) {
    const del = el('button', { type: 'button', class: 'btn-secondary danger-text', text: t('people.delete') });
    del.addEventListener('click', async () => {
      if (!await ctx.confirm(t('people.delete_confirm', { name: person.name }), { danger: true })) return;
      try {
        await ctx.api.deletePerson(person.id);
        ctx.toast(t('people.deleted', { name: person.name }));
        ctx.closeEditor();
        await ctx.reload();
      } catch (e) { showErr(errText(e)); }
    });
    const out = el('button', { type: 'button', class: 'btn-secondary', text: t('people.sign_out_everywhere') });
    out.addEventListener('click', async () => {
      try {
        await ctx.api.signOutPerson(person.id);
        ctx.toast(t('people.signed_out', { name: person.name }));
      } catch (e) { showErr(errText(e)); }
    });
    extra.push(out, del);

    if (!pinField.hidden || person.role === 'member' || person.role === 'restricted') {
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
      if (person.role === 'restricted' && person.has_pin) {
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
    }
    if (person.role === 'admin' && ctx.role === 'owner') {
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

  const collect = () => ({
    name: name.value, role: role.value, avatar: avatar.value, pin: pin.value, password: password.value,
    minutes: fMinutes.value, quietStart: qStart.value, quietEnd: qEnd.value, turns: fTurns.value,
    threads: fThreads.value, feedback: fFeedback.value, uploadMb: fUpload.value,
    sessionMinutes: fSession.value, disabled: disabled.checked,
  });
  const errorText = (errors) => {
    const first = Object.entries(errors)[0];
    if (!first) return '';
    const [k, code] = first;
    return t(`people.err_${code}`, { field: k });
  };

  save.addEventListener('click', async () => {
    showErr('');
    const f = collect();
    save.disabled = true;
    try {
      if (creating) {
        const { body, errors } = createPayload(f);
        if (Object.keys(errors).length) { showErr(errorText(errors)); return; }
        await ctx.api.createPerson(body);
        pin.value = ''; password.value = '';
        ctx.toast(t('people.created', { name: body.name }));
        ctx.closeEditor();
        await ctx.reload();
      } else {
        const { patch, errors } = patchPayload(person, f);
        if (Object.keys(errors).length) { showErr(errorText(errors)); return; }
        if (!Object.keys(patch).length) { ctx.closeEditor(); return; }
        await ctx.api.patchPerson(person.id, patch);
        ctx.toast(t('people.saved', { name: f.name.trim() || person.name }));
        ctx.closeEditor();
        await ctx.reload();
      }
    } catch (e) {
      showErr(errText(e));
    } finally {
      save.disabled = false;
    }
  });
  cancel.addEventListener('click', () => ctx.closeEditor());

  const blocked = adminBlocked && creating
    ? hintLine(t(adminBlocked === 'owner_needs_password' ? 'people.admin_needs_password' : 'people.admin_owner_only'), 'people-admin-note')
    : null;

  const root = el('div', { class: 'people-editor dev-group-body', id: 'people-editor', role: 'group',
    'aria-label': creating ? t('people.new_title') : t('people.edit_title', { name: person.name }) }, [
    el('div', { class: 'people-form' }, [
      el('h4', { class: 'people-editor-title', text: creating ? t('people.new_title') : t('people.edit_title', { name: person.name }) }),
      field(t('people.name'), name),
      field(t('people.role'), role),
      blocked,
      field(t('people.avatar'), avatar, t('people.avatar_hint')),
      pinField,
      pwField,
      budgets,
      creating ? null : el('label', { class: 'sec-remember-row people-disabled-row' }, [
        disabled, el('span', { text: t('people.disabled_label') })]),
      err,
      el('div', { class: 'bm-actions people-actions' }, actions),
      extra.length ? el('div', { class: 'bm-actions people-actions people-actions-extra' }, extra) : null,
    ]),
  ]);
  return { root, focus: () => name.focus() };
}

// ---- This device ------------------------------------------------------------

function deviceGroup(ctx) {
  const dv = ctx.devices || { devices: [], this_device: null };
  const mine = dv.devices.find((d) => d.this_device) || null;
  const bound = mine ? mine.default_profile_id : null;
  const choices = bindableProfiles(ctx.data.people);

  const select = el('select', { id: 'people-device-select' }, [
    el('option', { value: '', text: t('people.device_nobody') }),
    ...choices.map((p) => el('option', { value: p.id, text: `${p.name} (${roleLabel(p.role)})` })),
  ]);
  select.value = bound && choices.some((p) => p.id === bound) ? bound : '';
  select.addEventListener('change', async () => {
    const want = select.value || null;
    select.disabled = true;
    try {
      await ctx.api.bindThisDevice(want);
      ctx.toast(t(want ? 'people.device_bound' : 'people.device_unbound'));
      await ctx.reloadDevices();
    } catch (e) {
      select.value = bound || '';
      ctx.toast(errText(e), true);
    } finally { select.disabled = false; }
  });

  const rows = [
    el('div', { class: 'dev-row dev-row-block people-device-pick' }, [
      el('label', { for: 'people-device-select', class: 'dev-row-text' }, [
        el('strong', { text: t('people.device_open_as') }),
        hintLine(t('people.device_hint')),
      ]),
      select,
    ]),
  ];

  const nameOf = (id) => {
    const p = ctx.data.people.find((x) => x.id === id);
    return p ? p.name : null;
  };
  for (const d of dv.devices) {
    const who = d.default_profile_id ? nameOf(d.default_profile_id) : null;
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
    const btns = [];
    if (d.this_device && d.default_profile_id) {
      const unbind = el('button', { type: 'button', class: 'btn-secondary', text: t('people.device_unbind') });
      unbind.addEventListener('click', async () => {
        try {
          await ctx.api.bindThisDevice(null);
          ctx.toast(t('people.device_unbound'));
          await ctx.reloadDevices();
        } catch (e) { ctx.toast(errText(e), true); }
      });
      btns.push(unbind);
    }
    btns.push(forget);
    rows.push(el('div', { class: 'dev-row people-device', dataset: { device: d.id } }, [
      el('span', { class: 'dev-row-text' }, [
        el('strong', {}, [label, d.this_device ? ` (${t('people.device_this')})` : '']),
        hintLine(who ? t('people.device_opens_as', { name: who }) : t('people.device_opens_ask')),
        d.last_seen ? hintLine(t('people.device_seen', { when: relTime(d.last_seen) })) : null,
      ]),
      el('span', { class: 'people-row-actions' }, btns),
    ]));
  }
  return group('people-devices', t('people.device_title'), t('people.device_intro'),
    el('div', { class: 'dev-group-body', id: 'people-devices-body' }, rows));
}

// ---- Activity -----------------------------------------------------------------

function activityTable(rows) {
  const head = el('tr', {}, [
    el('th', { scope: 'col', text: t('people.col_day') }),
    ...ACTIVITY_COLUMNS.map((c) => el('th', { scope: 'col', class: 'people-num', text: t(`people.col_${c}`) })),
  ]);
  const body = rows.map((r) => el('tr', {}, [
    el('th', { scope: 'row', text: r.day }),
    ...ACTIVITY_COLUMNS.map((c) => el('td', { class: 'people-num', text: String(r.counts[c]) })),
  ]));
  const totals = activityTotals(rows);
  const foot = el('tr', { class: 'people-total' }, [
    el('th', { scope: 'row', text: t('people.col_total') }),
    ...ACTIVITY_COLUMNS.map((c) => el('td', { class: 'people-num', text: String(totals[c]) })),
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
  const key = `people.audit_${kind}`;
  return has(key) ? t(key) : String(kind);
}

function activityGroup(ctx) {
  const select = el('select', { id: 'people-activity-who' }, [
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

// ---- Machine access (Owner only) -----------------------------------------------

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
 * Settings -> People & Profiles came on screen: (re)build the pane.
 *   pane  #spane-people
 *   deps  { toast(msg, isErr), confirm(msg, opts) -> Promise<bool>, role,
 *           onSelfChanged?(), api? }
 */
export async function mountPeopleSettings(pane, deps) {
  const a = deps.api || realApi;
  const mine = ++seq;
  const intro = el('p', { class: 'muted modal-hint', text: t('people.hint') });
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

  const role = deps.role || (data.me && data.me.role) || 'admin';
  const ctx = {
    api: a, role, data: { people: data.profiles || [], owner_has_password: !!data.owner_has_password },
    devices, toast: deps.toast, confirm: deps.confirm,
    openEditor: () => {}, closeEditor: () => {}, reload: () => {}, reloadDevices: () => {},
  };

  const listHost = el('div', { class: 'people-list-host' });
  const editorHost = el('div', { class: 'people-editor-host' });
  const deviceHost = el('div', { class: 'people-device-host' });
  const activityHost = el('div', { class: 'people-activity-host' });
  const machineHost = el('div', { class: 'people-machine-host' });
  let editing = null;      // profile id being edited, 'new', or null

  const renderList = () => {
    listHost.replaceChildren(group('people-people', t('people.title'), t('people.intro'), listBody(ctx)));
    if (canCreate(role)) {
      const add = el('button', { type: 'button', class: 'btn-primary people-add', id: 'people-add', text: t('people.add') });
      add.addEventListener('click', () => ctx.openEditor(null));
      listHost.querySelector('.dev-group').append(el('div', { class: 'people-add-row' }, [add]));
    }
  };
  const renderDevices = () => deviceHost.replaceChildren(deviceGroup(ctx));
  const renderActivity = () => activityHost.replaceChildren(activityGroup(ctx));

  ctx.openEditor = (person) => {
    editing = person ? person.id : 'new';
    const ed = editorPanel(ctx, person);
    editorHost.replaceChildren(ed.root);
    try { ed.root.scrollIntoView({ block: 'nearest' }); } catch { /* ignore */ }
    ed.focus();
  };
  ctx.closeEditor = () => { editing = null; editorHost.replaceChildren(); };
  ctx.reload = async ({ keep } = {}) => {
    try {
      const fresh = await a.people();
      ctx.data = { people: fresh.profiles || [], owner_has_password: !!fresh.owner_has_password };
    } catch (e) { ctx.toast(errText(e), true); return; }
    renderList();
    renderDevices();
    renderActivity();
    if (keep) {
      const p = ctx.data.people.find((x) => x.id === keep);
      if (p) ctx.openEditor(p);
    }
    if (deps.onChanged) deps.onChanged();
  };
  ctx.reloadDevices = async () => {
    try { ctx.devices = await a.devices(); } catch { /* keep the old list */ }
    renderDevices();
  };

  renderList();
  renderDevices();
  renderActivity();
  const parts = [intro, listHost, editorHost, deviceHost, activityHost];
  if (role === 'owner') {
    const g = await machineGroup(ctx);
    if (mine !== seq) return;
    if (g) machineHost.replaceChildren(g);
    parts.push(machineHost);
  }
  pane.replaceChildren(...parts);
}

const canCreate = (role) => role === 'owner' || role === 'admin';
