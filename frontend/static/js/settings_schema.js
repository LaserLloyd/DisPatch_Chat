// Settings drawn from the server's schema, in the section each one belongs to.
//
// The page knows nothing about any individual setting. It asks GET /api/settings/schema for the
// definitions (key, type, default, range, group, section, basic/advanced, restart, env, secret, and
// since 3.2 `ui_section` and `presets`) and GET /api/settings for the values and where each one comes
// from. Each Settings section has one or more `.schema-host[data-schema="section#anchor"]` slots;
// `mountSchemaRows` fills a slot with one ForgeUI `.ui-setting` row per setting whose `ui_section`
// names it (a slot without an anchor, under the section's Advanced disclosure, takes the rest):
//
//   - a value applies when it is changed (one PATCH per row, a toast with Undo);
//   - durations and sizes are a dropdown of presets plus "Custom…"; booleans are switches;
//   - a "Set by environment" badge and a locked control for a value an environment variable sets;
//   - a refusal is shown on its row; a row that needs a restart says so, and the section shows
//     one "Restart DisPatch to apply N changes" line.
//
// The same module applies the server's default theme / language / avatar style to a device that
// has not chosen its own (`applyDeviceDefaults`).
//
// Pure functions first (buildModel, matches, parseInput, buildPatch, durations and sizes, which
// rows go in which slot) so the tests drive them without a DOM. Nothing runs at import time.

import { w5 as realApi, errorText } from './w5-api.js?v=2';
import { t, lang } from './i18n.js?v=5';
import { el } from './util.js?v=21';

// ------------------------------------------------------------------ pure

/** Group ids in the order the server sends them (the brief's order). */
export function groupOrder(schema) {
  return (schema.groups || []).map((g) => g.id);
}

/** schema + values -> [{ id, label, description, sections: [{ name, rows: [row] }] }].
 *  A row is { def, value, source, env, restartPending, isDefault, secretSet, warning }. */
export function buildModel(schema, values) {
  const vals = (values && values.values) || {};
  const groups = (schema.groups || []).map((g) => ({
    id: g.id, label: g.label, description: g.description, sections: [], count: 0,
  }));
  const byId = new Map(groups.map((g) => [g.id, g]));
  for (const def of schema.settings || []) {
    const g = byId.get(def.group);
    if (!g) continue;
    const v = vals[def.key] || {};
    const row = {
      def,
      value: v.value === undefined ? def.default : v.value,
      source: v.source || 'default',
      env: v.env || null,
      restartPending: !!v.restart_pending,
      isDefault: v.is_default !== false && v.source !== 'store' && v.source !== 'env',
      secretSet: !!v.set,
      warning: v.warning || '',
    };
    const name = def.section || '';
    let sec = g.sections.find((s) => s.name === name);
    if (!sec) { sec = { name, rows: [] }; g.sections.push(sec); }
    sec.rows.push(row);
    g.count += 1;
  }
  return groups;
}

/** Does `row` pass the search text and the Basic/Advanced switch? */
export function matches(row, { q = '', advanced = false } = {}) {
  if (!advanced && row.def.level === 'advanced') return false;
  const needle = String(q || '').trim().toLowerCase();
  if (!needle) return true;
  const hay = `${row.def.key} ${row.def.label} ${row.def.description} ${row.def.section || ''}`.toLowerCase();
  return needle.split(/\s+/).every((w) => hay.includes(w));
}

/** The text of an input -> the typed value for the schema type (or undefined if not yet valid). */
export function parseInput(def, raw, checked) {
  switch (def.type) {
    case 'boolean': return !!checked;
    case 'integer': {
      const s = String(raw).trim();
      return /^-?\d+$/.test(s) ? Number(s) : undefined;
    }
    case 'number': {
      const s = String(raw).trim();
      return s !== '' && Number.isFinite(Number(s)) ? Number(s) : undefined;
    }
    default: return String(raw);
  }
}

const sameValue = (a, b) => JSON.stringify(a) === JSON.stringify(b);

/** Staged edits -> the PATCH body. `edits` is Map(key -> value); `resets` is Set(key). A key
 *  edited back to its stored value is dropped; a key reset AND edited is just the edit. */
export function buildPatch(edits, resets, model) {
  const rows = new Map();
  for (const g of model) for (const s of g.sections) for (const r of s.rows) rows.set(r.def.key, r);
  const set = {};
  const reset = [];
  for (const [key, value] of edits) {
    const row = rows.get(key);
    if (row && !row.def.secret && sameValue(row.value, value)) continue;
    set[key] = value;
  }
  for (const key of resets) {
    if (key in set) continue;
    const row = rows.get(key);
    if (row && row.source === 'default') continue;
    reset.push(key);
  }
  return { set, reset };
}

/** Appearance defaults for a device that has not chosen: what to apply, given what it has stored. */
export function deviceDefaultsPlan(defaults, stored) {
  const plan = [];
  if (defaults.theme && !stored.theme) plan.push(['theme', defaults.theme]);
  if (defaults.language && !stored.language) plan.push(['language', defaults.language]);
  if (defaults.avatar_style && !stored.avatarStyle) plan.push(['avatar_style', defaults.avatar_style]);
  return plan;
}

// ------------------------------------------------------------------ durations and sizes

/** Seconds per unit, for a schema `unit` and for the Custom… unit picker. */
const SECONDS = { seconds: 1, minutes: 60, hours: 3600, days: 86400 };
const BYTES = { megabytes: 1024 ** 2, gigabytes: 1024 ** 3 };

/** 'duration' (unit seconds / hours / days), 'size' (unit bytes), or '' for any other row. */
export function measureOf(def) {
  if (!def || (def.type !== 'integer' && def.type !== 'number')) return '';
  if (def.unit === 'bytes') return 'size';
  if (def.unit === 'seconds' || def.unit === 'hours' || def.unit === 'days') return 'duration';
  return '';
}

/** A stored value -> { n, unit } in the biggest whole unit (600 s -> 10 minutes). */
export function splitMeasure(def, value) {
  const v = Number(value);
  if (measureOf(def) === 'size') {
    for (const u of ['gigabytes', 'megabytes']) if (v >= BYTES[u] && v % BYTES[u] === 0) return { n: v / BYTES[u], unit: u };
    return { n: Math.round((v / BYTES.megabytes) * 100) / 100, unit: 'megabytes' };
  }
  const own = SECONDS[def.unit || 'seconds'];
  const secs = v * own;
  for (const u of ['days', 'hours', 'minutes', 'seconds']) {
    if (SECONDS[u] < own) break;
    if (secs >= SECONDS[u] && secs % SECONDS[u] === 0) return { n: secs / SECONDS[u], unit: u };
  }
  return { n: v, unit: def.unit || 'seconds' };
}

/** { n, unit } -> the value in the row's own unit (10 minutes -> 600 for a seconds row), or
 *  undefined when it is not a whole number of the row's unit. */
export function joinMeasure(def, n, unit) {
  const x = Number(n);
  if (String(n).trim() === '' || !Number.isFinite(x) || x < 0) return undefined;
  if (measureOf(def) === 'size') return BYTES[unit] ? Math.round(x * BYTES[unit]) : undefined;
  if (!SECONDS[unit]) return undefined;
  const v = (x * SECONDS[unit]) / SECONDS[def.unit || 'seconds'];
  return Number.isInteger(v) ? v : undefined;
}

/** The units the Custom… picker offers for a row (never smaller than the row's own unit). */
export function customUnits(def) {
  if (measureOf(def) === 'size') return ['megabytes', 'gigabytes'];
  const own = SECONDS[def.unit || 'seconds'];
  const units = ['seconds', 'minutes', 'hours', 'days'].filter((u) => SECONDS[u] >= own);
  // Seconds only where a row can really want them (a short poll interval).
  return units.filter((u) => u !== 'seconds' || (def.min != null && def.min < 60));
}

const UNIT_INTL = { seconds: 'second', minutes: 'minute', hours: 'hour', days: 'day', megabytes: 'megabyte', gigabytes: 'gigabyte' };

/** Does 0 mean "off" for this row? (Its presets offer 0.) */
const zeroOff = (def) => Array.isArray(def && def.presets) && def.presets.map(Number).includes(0);

/** "10 minutes", "6 hours", "200 MB" in the active language; 0 reads "Off" where 0 turns a thing off. */
export function formatMeasure(def, value, locale = lang()) {
  if (Number(value) === 0 && zeroOff(def)) return t('schema.off');
  const { n, unit } = splitMeasure(def, value);
  try {
    return new Intl.NumberFormat(locale || 'en', { style: 'unit', unit: UNIT_INTL[unit],
      unitDisplay: measureOf(def) === 'size' ? 'short' : 'long', maximumFractionDigits: 2 }).format(n);
  } catch {
    return `${n} ${unit}`;
  }
}

/** The presets a dropdown lists for a row, in the server's order. */
export function presetValues(def) {
  return Array.isArray(def && def.presets)
    ? def.presets.map(Number).filter((v) => Number.isFinite(v))
    : [];
}

// ------------------------------------------------------------------ which rows go where

/** Keys drawn by a section's own controls, never as a schema row (they stay searchable).
 *  people.lock_target / people.default_profile: the "When locked, show" picker
 *  (people-perms.js lockTargetGroup) sets both together, so no slot draws them a second time. */
export const BESPOKE_KEYS = new Set(['people.machine_loopback_trust', 'tools.livetools_enabled',
  'people.lock_target', 'people.default_profile']);

/** `ui_section` 'people#signin-rules' -> ['people', 'signin-rules']; falls back to the group. */
export function uiTarget(def) {
  const raw = String((def && def.ui_section) || '');
  if (raw) {
    const i = raw.indexOf('#');
    return i < 0 ? [raw, ''] : [raw.slice(0, i), raw.slice(i + 1)];
  }
  const byGroup = { characters: 'characters', connections: 'connections', voice: 'voice', tools: 'plugins',
    people: 'people', appearance: 'appearance', system: 'system' };
  return [byGroup[def && def.group] || 'system', ''];
}

/** The definitions one slot shows. `claimed` = the anchors of this section that have a slot of
 *  their own: a row whose anchor has no slot falls back to the section's anchor-less slot, so no
 *  row is ever lost. */
export function rowsForSlot(settings, section, anchor, claimed = new Set()) {
  return (settings || []).filter((def) => {
    if (BESPOKE_KEYS.has(def.key)) return false;
    const [s, a] = uiTarget(def);
    if (s !== section) return false;
    if (anchor) return a === anchor;
    return !a || !claimed.has(a);
  });
}

// ------------------------------------------------------------------ DOM

const labelOf = (def) => def.label;
/** A value inside a quoted attribute selector (keys are [a-z0-9._-]; this is the belt). */
const attrValue = (v) => String(v).replace(/["\\]/g, '\\$&');

// One fetch of the schema and the values per Settings visit; a write refreshes the values.
let cache = null;
/** Drop the cached schema + values (Settings re-opened, a language change, a restore). */
export function invalidateSchema() { cache = null; }
async function load(api, force = false) {
  if (!cache || force) {
    cache = Promise.all([api.settingsSchema(), api.settings()]).catch((e) => { cache = null; throw e; });
  }
  return cache;
}

/** A row of the model for one definition (see buildModel). */
function rowOf(def, values) {
  const v = ((values && values.values) || {})[def.key] || {};
  return {
    def,
    value: v.value === undefined ? def.default : v.value,
    source: v.source || 'default',
    env: v.env || null,
    restartPending: !!v.restart_pending,
    isDefault: v.is_default !== false && v.source !== 'store' && v.source !== 'env',
    secretSet: !!v.set,
    warning: v.warning || '',
  };
}

/** A duration or size: a dropdown of presets (plus the current value) and "Custom…",
 *  which reveals a number box and a unit picker. Calls commit(value) on a complete choice. */
function measureControl(row, common, commit) {
  const { def } = row;
  const wrap = el('span', { class: 'schema-measure' });
  const sel = el('select', { ...common, class: 'ui-select' });
  const cur = row.value == null ? def.default : Number(row.value);
  const values = presetValues(def);
  const inList = values.includes(cur);
  for (const v of values) sel.append(el('option', { value: String(v), text: formatMeasure(def, v) }));
  sel.append(el('option', { value: 'custom', text: inList ? t('schema.custom') : t('schema.custom_value', { value: formatMeasure(def, cur) }) }));
  const parts = splitMeasure(def, cur);
  const num = el('input', { type: 'number', class: 'ui-input schema-measure-n', min: '0', step: 'any', inputmode: 'decimal',
    'aria-label': t('schema.custom_amount', { name: def.label }), value: String(parts.n) });
  const unit = el('select', { class: 'ui-select schema-measure-u', 'aria-label': t('schema.custom_unit', { name: def.label }) },
    customUnits(def).map((u) => el('option', { value: u, text: t(`schema.unit_${u}`) })));
  unit.value = customUnits(def).includes(parts.unit) ? parts.unit : customUnits(def)[0];
  if (!customUnits(def).includes(parts.unit)) num.value = String(splitMeasure({ ...def }, cur).n);
  const custom = el('span', { class: 'schema-measure-custom' }, [num, unit]);
  sel.value = inList ? String(cur) : 'custom';
  custom.hidden = inList;
  const fromCustom = () => {
    const v = joinMeasure(def, num.value, unit.value);
    if (v !== undefined) commit(v);
  };
  sel.addEventListener('change', () => {
    custom.hidden = sel.value !== 'custom';
    if (sel.value === 'custom') { num.focus(); return; }
    commit(Number(sel.value));
  });
  num.addEventListener('change', fromCustom);
  unit.addEventListener('change', fromCustom);
  if (common.disabled !== undefined) { num.disabled = true; unit.disabled = true; }
  wrap.append(sel, custom);
  wrap.focusTarget = sel;
  return wrap;
}

function controlFor(row, ctx) {
  const { def } = row;
  const id = `st-${def.key.replace(/[^a-z0-9]+/gi, '-')}`;
  const locked = row.source === 'env' || (def.owner_only && !ctx.owner);
  const common = { id, 'aria-describedby': `${id}-desc` };
  if (locked) common.disabled = '';
  let input;
  if (def.type === 'boolean') {
    input = el('input', { ...common, type: 'checkbox', role: 'switch' });
    input.checked = !!row.value;
    input.addEventListener('change', () => ctx.commit(row, input.checked, input));
  } else if (def.type === 'choice') {
    const current = String(row.value ?? def.default ?? '');
    const opts = (def.choices || []).map((c) => el('option', { value: c, text: (def.choice_labels && def.choice_labels[c]) || c || t('schema.none') }));
    // A stored connection id whose connection was deleted is still the value (it was
    // free text before 3.2): show it as missing rather than as "None".
    if (def.choices_from === 'connections' && current && !(def.choices || []).includes(current)) {
      opts.push(el('option', { value: current, text: t('schema.missing_connection', { id: current }) }));
    }
    input = el('select', { ...common, class: 'ui-select' }, opts);
    input.value = current;
    input.addEventListener('change', () => ctx.commit(row, input.value, input));
  } else if (measureOf(def) && Array.isArray(def.presets) && def.presets.length) {
    input = measureControl(row, common, (v) => ctx.commit(row, v, input));
  } else if (def.type === 'secret') {
    input = el('input', { ...common, type: 'password', class: 'ui-input', autocomplete: 'new-password',
      placeholder: row.secretSet ? t('schema.secret_set') : t('schema.secret_unset') });
    input.addEventListener('change', () => { if (input.value) ctx.commit(row, input.value, input); });
  } else if (def.type === 'integer' || def.type === 'number') {
    input = el('input', { ...common, type: 'number', class: 'ui-input', inputmode: 'numeric',
      step: def.type === 'integer' ? '1' : 'any' });
    if (def.min != null) input.min = String(def.min);
    if (def.max != null) input.max = String(def.max);
    input.value = row.value == null ? '' : String(row.value);
    input.addEventListener('change', () => {
      const v = parseInput(def, input.value);
      if (v !== undefined) ctx.commit(row, v, input);
    });
  } else {
    input = el('input', { ...common, type: def.type === 'url' ? 'url' : 'text', class: 'ui-input',
      placeholder: def.placeholder || '', autocomplete: 'off', spellcheck: 'false' });
    input.value = row.value == null ? '' : (Array.isArray(row.value) ? row.value.join(', ') : String(row.value));
    // Text applies on blur or Enter (the change event), never per keystroke.
    input.addEventListener('change', () => ctx.commit(row, parseInput(def, input.value), input));
  }
  return input;
}

function rowNode(row, ctx) {
  const { def } = row;
  const id = `st-${def.key.replace(/[^a-z0-9]+/gi, '-')}`;
  const control = controlFor(row, ctx);
  const attrs = { class: 'ui-setting', dataset: { key: def.key } };
  if (def.level === 'advanced') attrs['data-level'] = 'advanced';
  if (row.source === 'env') attrs['data-source'] = 'env';
  if (def.default !== null && def.default !== undefined && def.type !== 'secret') {
    attrs['data-default'] = Array.isArray(def.default) ? def.default.join(', ') : String(def.default);
  }
  const reset = el('button', { type: 'button', class: 'ui-btn ui-btn--ghost ui-btn--sm ui-setting__reset', hidden: '' });
  const err = el('p', { class: 'ui-error schema-error', id: `${id}-err`, role: 'alert', hidden: '' });
  const tags = [];
  if (row.restartPending) tags.push(el('span', { class: 'ui-badge schema-restart', text: t('schema.restart_pending') }));
  else if (def.restart_required) tags.push(el('span', { class: 'ui-badge schema-restart-note', text: t('schema.needs_restart') }));
  const node = el('div', attrs, [
    el('div', { class: 'ui-setting__text' }, [
      el('div', { class: 'ui-setting__label' }, [
        el('label', { for: id, text: labelOf(def) }),
        el('span', { class: 'ui-badge ui-badge--info ui-setting__env', title: row.env ? t('schema.env_title', { name: row.env }) : '' }),
        def.owner_only && !ctx.owner ? el('span', { class: 'ui-badge schema-owner-only', text: t('schema.owner_only') }) : null,
        ...tags,
      ]),
      el('p', { class: 'ui-setting__desc', id: `${id}-desc`, text: def.description }),
      row.warning ? el('p', { class: 'ui-setting__desc schema-warning', text: row.warning }) : null,
    ]),
    el('div', { class: 'ui-setting__control' }, [control, reset, err]),
  ]);
  ctx.wire(node, row, control.focusTarget || control, reset, err);
  return node;
}

/**
 * Fill `host` with the rows for `section#anchor` (anchor '' = the rows of the section that have
 * no slot of their own). opts:
 *   section, anchor   where the rows belong (see uiTarget / rowsForSlot)
 *   claimed           Set of the section's anchors that have their own slot
 *   api, toast(msg, isErr, { undo }), role, onRestart(count)   the app; api defaults to the real client
 * Returns { rows } (the drawn definitions), or null when nothing was drawn.
 */
export async function mountSchemaRows(host, opts = {}) {
  if (!host) return null;
  const api = opts.api || realApi;
  const toast = opts.toast || (() => {});
  const mine = (host._schemaSeq = (host._schemaSeq || 0) + 1);
  let schema;
  let values;
  try {
    [schema, values] = await load(api, !!opts.force);
  } catch (e) {
    if (mine !== host._schemaSeq) return null;
    host.replaceChildren(el('p', { class: 'muted lock-error', role: 'status', text: errorText(e) }));
    host.hidden = false;
    return null;
  }
  if (mine !== host._schemaSeq) return null;
  const defs = rowsForSlot(schema.settings, opts.section, opts.anchor || '', opts.claimed || new Set());
  host.replaceChildren();
  host.hidden = !defs.length;
  if (!defs.length) return null;

  const ctx = {
    // Host-control (owner_only) settings need admin_system, which every Admin holds. A profile merely
    // granted manage_settings is refused by the server; the page just doesn't offer it.
    owner: opts.role === 'owner' || opts.role === 'admin' || opts.role == null,
    async commit(row, value, control) {
      if (value === undefined) return;
      const k = row.def.key;
      if (!row.def.secret && sameValue(row.value, value)) return;
      const node = host.querySelector(`[data-key="${attrValue(k)}"]`);
      const errNode = node && node.querySelector('.schema-error');
      const before = row.value;
      const wasDefault = row.source === 'default';
      try {
        const res = await api.patchSettings({ [k]: value }, []);
        await refreshRow(row, node);
        const restart = (res && res.restart_required || []).includes(k);
        if (errNode) { errNode.hidden = true; errNode.textContent = ''; }
        if (control && control.removeAttribute) control.removeAttribute('aria-invalid');
        const undo = row.def.secret ? null : async () => {
          try {
            await api.patchSettings(wasDefault ? {} : { [k]: before }, wasDefault ? [k] : []);
            await refreshRow(row, null, true);
            toast(t('schema.undone', { name: row.def.label }));
          } catch (e) { toast(errorText(e), true); }
        };
        toast(restart ? t('schema.saved_one_restart', { name: row.def.label }) : t('schema.saved_one', { name: row.def.label }),
          false, undo ? { undo } : undefined);
      } catch (e) {
        const msg = (e && e.errors && e.errors[k]) || errorText(e);
        if (errNode) { errNode.textContent = msg; errNode.hidden = false; }
        if (control && control.setAttribute) control.setAttribute('aria-invalid', 'true');
      }
    },
    wire(node, row, control, resetBtn, errNode) {
      const k = row.def.key;
      void errNode;
      const doReset = async () => {
        try {
          await api.patchSettings({}, [k]);
          await refreshRow(row, null, true);
          toast(t('schema.reset_done', { name: row.def.label }));
        } catch (e) { toast(errorText(e), true); }
      };
      const ui = typeof window !== 'undefined' && window.UIComponents;
      if (ui && ui.setting) {
        ui.setting(node, { default: node.getAttribute('data-default') ?? undefined, onReset: doReset });
      } else if (row.def.default != null && row.def.type !== 'secret' && row.source === 'store') {
        // No component layer (a stripped page, the tests): a small reset link of our own.
        resetBtn.hidden = false;
        resetBtn.textContent = t('schema.reset');
        resetBtn.addEventListener('click', doReset);
      }
      void control;
    },
  };

  /** Re-read the values and redraw one row (or the whole slot) from the server's answer. */
  async function refreshRow(row, node, whole = false) {
    cache = Promise.all([Promise.resolve(schema), api.settings()]);
    try { [schema, values] = await cache; } catch { cache = null; return; }
    if (whole || !node) { await draw(); return; }
    const fresh = rowOf(row.def, values);
    Object.assign(row, fresh);
    const next = rowNode(row, ctx);
    node.replaceWith(next);
    paintRestart();
  }

  function paintRestart() {
    if (typeof opts.onRestart !== 'function') return;
    const vals = (values && values.values) || {};
    opts.onRestart(Object.values(vals).filter((v) => v && v.restart_pending).length);
  }

  async function draw() {
    host.replaceChildren();
    // Sub-headings by the schema's own `section` field ("Network", "Agents"...),
    // only when a slot mixes more than one.
    const bySection = new Map();
    for (const def of defs) {
      const s = def.section || '';
      if (!bySection.has(s)) bySection.set(s, []);
      bySection.get(s).push(def);
    }
    const heads = bySection.size > 1;
    for (const [name, list] of bySection) {
      if (heads && name) host.append(el('h4', { class: 'schema-section', text: name }));
      const box = el('div', { class: 'ui-settings', 'data-level-filter': 'all' });
      for (const def of list) box.append(rowNode(rowOf(def, values), ctx));
      host.append(box);
    }
    const ui = typeof window !== 'undefined' && window.UIComponents;
    if (ui && ui.init) ui.init(host);
    paintRestart();
  }

  await draw();
  return { rows: defs };
}

// ------------------------------------------------------------------ device defaults

/**
 * Apply the server's default theme / language / avatar style to a device that has none of its own.
 * A default is not a choice: nothing is left in localStorage afterwards, so changing the default
 * in Settings reaches the device on its next load. Never throws.
 */
export async function applyDeviceDefaults({ api = realApi, win = typeof window === 'undefined' ? null : window } = {}) {
  if (!win) return [];
  let defaults;
  try { defaults = await api.settingsDefaults(); } catch { return []; }
  const ls = (k) => { try { return win.localStorage.getItem(k); } catch { return null; } };
  const drop = (k) => { try { win.localStorage.removeItem(k); } catch { /* ignore */ } };
  const plan = deviceDefaultsPlan(defaults, {
    theme: ls('dispatch-palette'), language: ls('dispatch-lang'), avatarStyle: ls('dispatch-avatar-style'),
  });
  const done = [];
  for (const [what, value] of plan) {
    try {
      if (what === 'theme' && win.UITheme && typeof win.UITheme.set === 'function') {
        if (!win.UITheme.list().some((x) => x.slug === value || x === value)) continue;
        win.UITheme.set(value); drop('dispatch-palette'); done.push(what);
      } else if (what === 'avatar_style' && value === 'minimal') {
        win.document.documentElement.setAttribute('data-avatar-style', 'minimal'); done.push(what);
      } else if (what === 'language') {
        const i18n = await import('./i18n.js?v=5');
        if (value !== i18n.lang()) { await i18n.setLocale(value); drop('dispatch-lang'); done.push(what); }
      }
    } catch { /* a default must never break the page */ }
  }
  return done;
}
