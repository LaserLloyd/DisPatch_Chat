// Settings -> "All settings": every setting, drawn from the server's schema.
//
// The page knows nothing about any individual setting. It asks GET /api/settings/schema for the
// definitions (key, type, default, range, group, section, basic/advanced, restart, env, secret)
// and GET /api/settings for the values and where each one comes from, then builds one
// ForgeUI `.ui-setting` row per setting:
//
//   - groups in the brief's order (Characters, Connections, Voice, Tools & Modules, People &
//     Profiles, Appearance, System), a search box, a Basic / Advanced switch;
//   - a "Set by environment" badge and a locked control for a value an environment variable sets;
//   - "Reset to default" per row (a staged reset: nothing is sent until Save);
//   - Save sends only what changed; the server validates every key, so a refusal comes back
//     per key and is shown on its row; keys that need a restart are listed afterwards.
//
// The same module owns two small things that read the schema's Appearance group: applying the
// server's default theme / language / avatar style to a device that has not chosen its own
// (`applyDeviceDefaults`), and the list of settings that still live in their own tabs.
//
// Pure functions first (buildModel, matches, parseInput, buildPatch) so the tests drive them
// without a DOM; mountSettingsSchema is the DOM half. Nothing runs at import time.

import { w5 as realApi, errorText } from './w5-api.js?v=1';
import { t } from './i18n.js?v=4';
import { el } from './util.js?v=20';
import { mountBackups } from './backup.js?v=2';

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

/** Settings that still live in their own tab (not in the schema yet): [{ tab, i18n }]. */
export const OTHER_TABS = [
  { tab: 'bots', key: 'schema.other_bots' },
  { tab: 'security', key: 'schema.other_security' },
  { tab: 'ai', key: 'schema.other_ai' },
  { tab: 'reactions', key: 'schema.other_reactions' },
  { tab: 'device', key: 'schema.other_device' },
  { tab: 'plugins', key: 'schema.other_plugins' },
];

// ------------------------------------------------------------------ DOM

const GROUP_GLYPH = {
  characters: '🤖', connections: '🔗', voice: '🎙️', tools: '🧰', people: '👥', appearance: '🎨', system: '⚙️',
};

const labelOf = (def) => def.label;

function controlFor(row, ctx) {
  const { def } = row;
  const id = `st-${def.key.replace(/[^a-z0-9]+/gi, '-')}`;
  const locked = row.source === 'env' || (def.owner_only && !ctx.owner);
  const common = { id, 'aria-describedby': `${id}-desc` };
  if (locked) common.disabled = '';
  let input;
  if (def.type === 'boolean') {
    input = el('input', { ...common, type: 'checkbox', class: 'ui-checkbox' });
    input.checked = !!row.value;
    input.addEventListener('change', () => ctx.edit(row, input.checked));
  } else if (def.type === 'choice') {
    input = el('select', { ...common, class: 'ui-select' },
      (def.choices || []).map((c) => el('option', { value: c, text: (def.choice_labels && def.choice_labels[c]) || c })));
    input.value = String(row.value ?? def.default ?? '');
    input.addEventListener('change', () => ctx.edit(row, input.value));
  } else if (def.type === 'secret') {
    input = el('input', { ...common, type: 'password', class: 'ui-input', autocomplete: 'new-password',
      placeholder: row.secretSet ? t('schema.secret_set') : t('schema.secret_unset') });
    input.addEventListener('input', () => ctx.edit(row, input.value));
  } else if (def.type === 'integer' || def.type === 'number') {
    input = el('input', { ...common, type: 'number', class: 'ui-input', inputmode: 'numeric',
      step: def.type === 'integer' ? '1' : 'any' });
    if (def.min != null) input.min = String(def.min);
    if (def.max != null) input.max = String(def.max);
    input.value = row.value == null ? '' : String(row.value);
    input.addEventListener('input', () => {
      const v = parseInput(def, input.value);
      if (v !== undefined) ctx.edit(row, v);
    });
  } else {
    input = el('input', { ...common, type: def.type === 'url' ? 'url' : 'text', class: 'ui-input',
      placeholder: def.placeholder || '', autocomplete: 'off', spellcheck: 'false' });
    input.value = row.value == null ? '' : (Array.isArray(row.value) ? row.value.join(', ') : String(row.value));
    input.addEventListener('input', () => ctx.edit(row, parseInput(def, input.value)));
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
        el('span', { class: 'ui-badge ui-setting__level' }),
        el('span', { class: 'ui-badge ui-badge--info ui-setting__env', title: row.env ? t('schema.env_title', { name: row.env }) : '' }),
        def.owner_only && !ctx.owner ? el('span', { class: 'ui-badge schema-owner-only', text: t('schema.owner_only') }) : null,
        ...tags,
      ]),
      el('p', { class: 'ui-setting__desc', id: `${id}-desc`, text: def.description }),
      row.warning ? el('p', { class: 'ui-setting__desc schema-warning', text: row.warning }) : null,
    ]),
    el('div', { class: 'ui-setting__control' }, [control, reset, err]),
  ]);
  ctx.wire(node, row, control, reset, err);
  return node;
}

/**
 * Mount the page into `pane`.
 * deps: { toast, confirm, openTab(tab), role, api? } - api defaults to the real client.
 */
export async function mountSettingsSchema(pane, deps = {}) {
  const api = deps.api || realApi;
  const toast = deps.toast || (() => {});
  pane.textContent = '';
  const state = {
    schema: null, values: null, model: [], group: deps.group || 'characters', q: '', advanced: false,
    edits: new Map(), resets: new Set(), errors: {}, restart: [],
  };
  const status = el('p', { class: 'muted modal-hint', role: 'status', text: t('schema.loading') });
  pane.append(status);
  try {
    [state.schema, state.values] = await Promise.all([api.settingsSchema(), api.settings()]);
  } catch (e) {
    status.textContent = errorText(e);
    status.classList.add('lock-error');
    return;
  }
  status.remove();
  state.model = buildModel(state.schema, state.values);

  const search = el('input', { type: 'search', class: 'ui-input schema-search', 'aria-label': t('schema.search_label'),
    placeholder: t('schema.search_placeholder'), autocomplete: 'off' });
  const toggle = el('button', { type: 'button', class: 'btn-secondary schema-level', 'aria-pressed': 'false', text: t('schema.show_advanced') });
  const nav = el('div', { class: 'schema-nav', role: 'tablist', 'aria-label': t('schema.groups_aria') });
  const body = el('div', { class: 'schema-body' });
  const bar = el('div', { class: 'schema-savebar', role: 'region', 'aria-label': t('schema.save_bar_aria') });
  const saveBtn = el('button', { type: 'button', class: 'btn-primary', text: t('schema.save'), disabled: '' });
  const discardBtn = el('button', { type: 'button', class: 'btn-secondary', text: t('schema.discard'), disabled: '' });
  const saveMsg = el('span', { class: 'schema-savemsg', role: 'status', 'aria-live': 'polite' });
  bar.append(discardBtn, saveBtn, saveMsg);
  pane.append(el('div', { class: 'schema-toolbar' }, [search, toggle]), nav, body, bar);

  const dirtyCount = () => state.edits.size + state.resets.size;
  const paintBar = () => {
    const n = dirtyCount();
    saveBtn.disabled = n === 0;
    discardBtn.disabled = n === 0;
    saveBtn.textContent = n ? t('schema.save_n', { count: n }) : t('schema.save');
  };

  const ctx = {
    // Host-control (owner_only) settings need admin_system: the Owner and Admins. A profile merely
    // granted manage_settings is refused by the server; the page just doesn't offer it.
    owner: deps.role === 'owner' || deps.role === 'admin' || deps.role == null,
    edit(row, value) {
      if (value === undefined) return;
      state.resets.delete(row.def.key);
      const same = !row.def.secret && sameValue(row.value, value) || (row.def.secret && value === '');
      if (same) state.edits.delete(row.def.key); else state.edits.set(row.def.key, value);
      delete state.errors[row.def.key];
      paintBar();
    },
    wire(node, row, control, resetBtn, errNode) {
      const k = row.def.key;
      const e = state.errors[k];
      if (e) {
        errNode.textContent = e;
        errNode.hidden = false;
        control.setAttribute('aria-invalid', 'true');
        control.setAttribute('aria-describedby', `${control.id}-desc ${errNode.id}`);
      }
      const ui = typeof window !== 'undefined' && window.UIComponents;
      if (ui && ui.setting) {
        ui.setting(node, {
          default: node.getAttribute('data-default') ?? undefined,
          onReset: () => { state.edits.delete(k); state.resets.add(k); paintBar(); },
        });
      } else if (row.def.default != null && row.def.type !== 'secret') {
        // No component layer (a stripped page, the tests): a small reset link of our own.
        resetBtn.hidden = false;
        resetBtn.textContent = t('schema.reset');
        resetBtn.addEventListener('click', () => {
          state.edits.delete(k); state.resets.add(k);
          if (control.type === 'checkbox') control.checked = !!row.def.default;
          else control.value = String(row.def.default);
          paintBar();
        });
      }
    },
  };

  function paintNav() {
    nav.textContent = '';
    for (const g of state.model) {
      const b = el('button', {
        type: 'button', role: 'tab', class: 'schema-tab' + (g.id === state.group ? ' active' : ''),
        'aria-selected': String(g.id === state.group), dataset: { group: g.id },
      }, [el('span', { 'aria-hidden': 'true', text: GROUP_GLYPH[g.id] || '•' }), ' ', g.label]);
      b.addEventListener('click', () => { state.group = g.id; state.q = ''; search.value = ''; paint(); });
      nav.append(b);
    }
  }

  function paint() {
    paintNav();
    body.textContent = '';
    const needle = state.q.trim();
    const groups = needle ? state.model : state.model.filter((g) => g.id === state.group);
    let shown = 0;
    for (const g of groups) {
      const sections = [];
      for (const s of g.sections) {
        const rows = s.rows.filter((r) => matches(r, { q: needle, advanced: state.advanced }));
        if (rows.length) sections.push({ name: s.name, rows });
      }
      const extras = needle ? null : groupExtras(g);
      if (!sections.length && !extras) continue;
      const sec = el('section', { class: 'dev-group schema-group', id: `schema-${g.id}`, 'aria-labelledby': `schema-${g.id}-h` }, [
        el('h3', { class: 'dev-group-title', id: `schema-${g.id}-h`, text: g.label }),
        el('p', { class: 'dev-group-hint', text: g.description }),
      ]);
      for (const s of sections) {
        const list = el('div', { class: 'ui-settings', 'data-level-filter': state.advanced ? 'all' : 'basic' });
        if (s.name) sec.append(el('h4', { class: 'schema-section', text: s.name }));
        for (const r of s.rows) { list.append(rowNode(r, ctx)); shown += 1; }
        sec.append(list);
      }
      if (extras) sec.append(extras);
      body.append(sec);
    }
    if (!shown && needle) body.append(el('p', { class: 'muted', text: t('schema.no_match') }));
    if (!needle) body.append(otherTabs());
    const ui = typeof window !== 'undefined' && window.UIComponents;
    if (ui && ui.init) ui.init(body);
  }

  /** Content that belongs to a group but is not a schema row. */
  function groupExtras(g) {
    if (g.id === 'voice') {
      const open = el('button', { type: 'button', class: 'btn-secondary', text: t('schema.open_voice') });
      open.addEventListener('click', () => deps.openTab && deps.openTab('voice'));
      return el('div', { class: 'schema-extra' }, [el('p', { class: 'muted', text: t('schema.voice_note') }), open]);
    }
    if (g.id === 'connections') {
      const open = el('button', { type: 'button', class: 'btn-secondary', text: t('schema.open_connections') });
      open.addEventListener('click', () => deps.openTab && deps.openTab('connections'));
      return el('div', { class: 'schema-extra' }, [open]);
    }
    if (g.id === 'system') {
      const host = el('div', { class: 'schema-backups' });
      mountBackups(host, { api, toast, confirm: deps.confirm, role: deps.role });
      return host;
    }
    return null;
  }

  function otherTabs() {
    const items = OTHER_TABS.map((o) => {
      const b = el('button', { type: 'button', class: 'schema-link', text: t(o.key) });
      b.addEventListener('click', () => deps.openTab && deps.openTab(o.tab));
      return el('li', {}, [b]);
    });
    return el('section', { class: 'dev-group schema-other' }, [
      el('h3', { class: 'dev-group-title', text: t('schema.other_title') }),
      el('p', { class: 'dev-group-hint', text: t('schema.other_hint') }),
      el('ul', { class: 'schema-other-list' }, items),
    ]);
  }

  search.addEventListener('input', () => { state.q = search.value; paint(); });
  toggle.addEventListener('click', () => {
    state.advanced = !state.advanced;
    toggle.setAttribute('aria-pressed', String(state.advanced));
    toggle.textContent = state.advanced ? t('schema.hide_advanced') : t('schema.show_advanced');
    paint();
  });
  discardBtn.addEventListener('click', async () => {
    state.edits.clear(); state.resets.clear(); state.errors = {};
    saveMsg.textContent = '';
    state.values = await api.settings();
    state.model = buildModel(state.schema, state.values);
    paint(); paintBar();
  });
  saveBtn.addEventListener('click', async () => {
    const { set, reset } = buildPatch(state.edits, state.resets, state.model);
    if (!Object.keys(set).length && !reset.length) { state.edits.clear(); state.resets.clear(); paintBar(); return; }
    saveBtn.disabled = true;
    saveMsg.textContent = '';
    try {
      const res = await api.patchSettings(set, reset);
      state.edits.clear(); state.resets.clear(); state.errors = {};
      state.values = await api.settings();
      state.model = buildModel(state.schema, state.values);
      state.restart = res.restart_required || [];
      saveMsg.textContent = state.restart.length
        ? t('schema.saved_restart', { count: state.restart.length })
        : t('schema.saved');
      toast(saveMsg.textContent);
      paint();
    } catch (e) {
      if (e.errors) {
        state.errors = e.errors;
        saveMsg.textContent = t('schema.not_saved');
        paint();
        const first = body.querySelector('[aria-invalid="true"]');
        if (first && first.focus) first.focus();
      } else {
        saveMsg.textContent = errorText(e);
      }
    } finally {
      paintBar();
    }
  });

  paint();
  paintBar();
  return { state, paint };
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
        const i18n = await import('./i18n.js?v=4');
        if (value !== i18n.lang()) { await i18n.setLocale(value); drop('dispatch-lang'); done.push(what); }
      }
    } catch { /* a default must never break the page */ }
  }
  return done;
}
