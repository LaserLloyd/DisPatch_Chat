// Settings -> AI & Connections: the servers DisPatch talks to.
//
// One card per connection (name, type, address, the result of its last Test, the characters that use
// it) with Test / Edit / Delete, an "Add a connection" form (pick a type, address, key, Test before you
// save) and "Find servers", which looks for StudioForge, LM Studio, Ollama and OpenAI-compatible
// servers on this computer (and, if you ask, the home network). A key is typed once and never shown
// again; an existing connection shows "key saved" and lets you replace or remove it.
//
// The server does the real work (address checks, the SSRF guard, the Test); this file only turns its
// answers into plain sentences and decides what to offer. Pure helpers are exported for the tests.

import { w5 as realApi, errorText } from './w5-api.js?v=1';
import { t } from './i18n.js?v=4';
import { el } from './util.js?v=20';

// ------------------------------------------------------------------ pure

/** The words for a Test result: [kind, headline, detail], kind = ok | warn | bad | idle. */
export function describeTest(r) {
  if (!r) return ['idle', t('cxn.not_tested'), ''];
  if (r.ok) {
    const bits = [];
    if (typeof r.latency_ms === 'number') bits.push(t('cxn.latency', { ms: r.latency_ms }));
    if (r.product) bits.push(r.version ? `${r.product} ${r.version}` : r.product);
    const n = (r.models && r.models.length) || 0;
    if (r.models_listed === false) return ['ok', t('cxn.ok_no_models'), bits.join(' · ')];
    // Reachable but offering nothing is not a success (3.2): usually a key without
    // access, or the wrong address.
    if (!n) return ['warn', t('cxn.ok_models', { count: 0 }), [t('cxn.check_key_url'), ...bits].join(' · ')];
    return ['ok', t('cxn.ok_models', { count: n }), bits.join(' · ')];
  }
  switch (r.problem) {
    case 'auth': return ['bad', t('cxn.bad_auth'), r.fix || r.message || ''];
    case 'unreachable': return ['bad', t('cxn.bad_unreachable'), r.fix || r.message || ''];
    case 'blocked_target': return ['bad', t('cxn.bad_blocked'), r.message || ''];
    case 'invalid_url': return ['bad', t('cxn.bad_url'), r.message || ''];
    case 'no_model_list': return ['warn', t('cxn.warn_no_list'), r.fix || r.message || ''];
    default: return ['bad', t('cxn.bad_other'), r.message || r.fix || ''];
  }
}

/** The stored last_test of a connection, in the same shape. */
export const describeStored = (c) => describeTest(c.last_test && Object.keys(c.last_test).length ? c.last_test : null);

/** "Used by" as names, at most `max`, then "+N more". `nameOf(id)` maps a character id. */
export function usedByText(ids, nameOf = (id) => id, max = 5) {
  const list = (ids || []).map((id) => nameOf(id) || id);
  if (!list.length) return t('cxn.unused');
  const shown = list.slice(0, max).join(', ');
  return list.length > max
    ? t('cxn.used_by_more', { names: shown, count: list.length - max })
    : t('cxn.used_by', { names: shown });
}

/** Does the connection's name already say its type? Then the type chip is noise. */
export const nameSaysType = (c) => !!(c && c.type_label && String(c.name || '').toLowerCase().includes(String(c.type_label).toLowerCase()));

/** The values the "Add" form starts with for a type. */
const CAP_KEYS = {
  chat: 'cxn.cap_chat', vision: 'cxn.cap_vision', images: 'cxn.cap_images',
  embeddings: 'cxn.cap_embeddings', tools: 'cxn.cap_tools', agent: 'cxn.cap_agent',
};

export function formDefaults(type) {
  return {
    type: type.id, name: type.label, base_url: type.base_url || '',
    needsKey: type.key !== 'none', keyRequired: type.key === 'required',
    editableUrl: !!type.editable_base_url || !type.base_url,
    keyEnv: type.key_env || '', capabilities: [...(type.capabilities || [])],
  };
}

/** The request body for create / patch from the form fields. A blank key on an EXISTING connection
 *  leaves the stored key alone; clearKey removes it; an env name switches to a variable. */
export function formPayload(f, { editing = false } = {}) {
  const o = {};
  if (!editing) o.type = f.type;
  if (f.name != null) o.name = f.name.trim();
  if (f.base_url != null) o.base_url = f.base_url.trim();
  if (f.api_key && f.api_key.trim()) o.api_key = f.api_key.trim();
  else if (f.api_key_env && f.api_key_env.trim()) o.api_key_env = f.api_key_env.trim();
  else if (editing && f.clearKey) o.clear_key = true;
  if (f.default_model != null) o.default_model = f.default_model.trim();
  if (Array.isArray(f.capabilities)) o.capabilities = f.capabilities;
  return o;
}

// ------------------------------------------------------------------ DOM

function field(label, input, hint) {
  if (!input.id) input.id = `cxn-${Math.random().toString(36).slice(2, 8)}`;
  const kids = [el('label', { for: input.id, text: label }), input];
  if (hint) kids.push(el('span', { class: 'people-field-hint', text: hint }));
  return el('div', { class: 'sec-field people-field' }, kids);
}

function resultBox() {
  return el('div', { class: 'cxn-result', role: 'status', 'aria-live': 'polite', hidden: '' });
}

function paintResult(box, r, onPickModel) {
  const [kind, head, detail] = describeTest(r);
  box.hidden = false;
  box.className = `cxn-result cxn-${kind}`;
  box.textContent = '';
  box.append(el('strong', { text: head }));
  if (detail) box.append(' ', el('span', { text: detail }));
  if (r && r.ok && r.models && r.models.length && onPickModel) {
    const list = el('div', { class: 'cxn-models' }, r.models.slice(0, 12).map((m) => {
      const b = el('button', { type: 'button', class: 'btn-secondary cxn-model', text: m });
      b.addEventListener('click', () => onPickModel(m));
      return b;
    }));
    box.append(el('p', { class: 'muted', text: t('cxn.pick_model') }), list);
  }
}

/**
 * Mount into `pane`. deps: { api?, toast, confirm(msg, opts), openTab(tab), onChanged(),
 * botName(id) -> the character's display name for "Used by" }
 */
export async function mountConnections(pane, deps = {}) {
  const api = deps.api || realApi;
  const toast = deps.toast || (() => {});
  const ask = deps.confirm || (async () => true);
  pane.textContent = '';
  const state = { types: [], list: [], editing: null, mode: null };
  const top = el('div', { class: 'cxn-top' }, [
    el('h3', { class: 'dev-group-title', text: t('cxn.title') }),
    el('p', { class: 'dev-group-hint', text: t('cxn.hint') }),
  ]);
  const addBtn = el('button', { type: 'button', class: 'btn-primary', text: t('cxn.add') });
  const findBtn = el('button', { type: 'button', class: 'btn-secondary', text: t('cxn.find') });
  const lanBox = el('input', { type: 'checkbox', id: 'cxn-lan', class: 'ui-checkbox' });
  const adoptBtn = el('button', { type: 'button', class: 'btn-secondary', text: t('cxn.adopt'), title: t('cxn.adopt_title') });
  const bar = el('div', { class: 'cxn-bar' }, [addBtn, findBtn, el('label', { class: 'bkp-check', for: 'cxn-lan' }, [lanBox, ' ', t('cxn.lan')]), adoptBtn]);
  const found = el('div', { class: 'cxn-found dev-group-body', hidden: '' });
  const formHost = el('div', { class: 'cxn-formhost' });
  const listHost = el('div', { class: 'cxn-list dev-group-body' });
  pane.append(top, bar, found, formHost, listHost);

  async function load() {
    try {
      const d = await api.connections();
      state.types = d.types;
      state.list = d.connections.map((c) => ({ ...c, is_default: !!d.default && c.id === d.default }));
    } catch (e) {
      listHost.textContent = errorText(e);
      return;
    }
    paintList();
  }

  function paintList() {
    listHost.textContent = '';
    if (!state.list.length) {
      listHost.append(el('p', { class: 'muted cxn-empty', text: t('cxn.empty') }));
      return;
    }
    for (const c of state.list) listHost.append(card(c));
  }

  function card(c) {
    const [kind, head, detail] = describeStored(c);
    const test = el('button', { type: 'button', class: 'btn-secondary', text: t('cxn.test'), 'aria-label': t('cxn.test_aria', { name: c.name }) });
    const edit = el('button', { type: 'button', class: 'btn-secondary', text: t('cxn.edit'), 'aria-label': t('cxn.edit_aria', { name: c.name }) });
    const del = el('button', { type: 'button', class: 'btn-secondary danger-text', text: t('cxn.delete'), 'aria-label': t('cxn.delete_aria', { name: c.name }) });
    const res = resultBox();
    test.addEventListener('click', async () => {
      test.disabled = true;
      res.hidden = false; res.className = 'cxn-result cxn-idle'; res.textContent = t('cxn.testing');
      try {
        const r = await api.testConnection(c.id);
        paintResult(res, r);
        Object.assign(c, r.connection || {});
        deps.onChanged && deps.onChanged();
      } catch (e) { paintResult(res, { ok: false, message: errorText(e) }); }
      finally { test.disabled = false; }
    });
    edit.addEventListener('click', () => openForm(c));
    del.addEventListener('click', async () => {
      if (!await ask(t('cxn.delete_confirm', { name: c.name }), { okText: t('cxn.delete'), danger: true })) return;
      try { await api.deleteConnection(c.id); toast(t('cxn.deleted')); deps.onChanged && deps.onChanged(); load(); }
      catch (e) { toast(errorText(e), true); }
    });
    const users = usedByText(c.in_use_by, deps.botName);
    return el('div', { class: 'dev-row cxn-card', dataset: { connection: c.id } }, [
      el('span', { class: 'dev-row-text' }, [
        el('strong', { text: c.name }), ' ', nameSaysType(c) ? null : el('span', { class: 'ui-badge', text: c.type_label }),
        c.is_default ? el('span', { class: 'ui-badge ui-badge--info', text: t('cxn.default') }) : null,
        el('span', { class: 'dev-row-hint cxn-url', text: c.base_url }),
        el('span', { class: `dev-row-hint cxn-status cxn-${kind}`, text: detail ? `${head} · ${detail}` : head }),
        el('span', { class: 'dev-row-hint', text: `${c.credential.has_key ? t('cxn.key_saved') : (c.credential.kind === 'env' ? t('cxn.key_env', { name: c.credential.env }) : t('cxn.no_key'))} · ${users}` }),
        res,
      ]),
      test, edit, del,
    ]);
  }

  function closeForm() { formHost.textContent = ''; state.editing = null; }

  function openForm(existing, preset) {
    closeForm();
    state.editing = existing || null;
    const editing = !!existing;
    const typeSel = el('select', { class: 'ui-select' },
      state.types.map((x) => el('option', { value: x.id, text: x.label })));
    const name = el('input', { type: 'text', class: 'ui-input', maxlength: '80' });
    const url = el('input', { type: 'url', class: 'ui-input', autocomplete: 'off', spellcheck: 'false', placeholder: 'http://127.0.0.1:1234/v1' });
    const key = el('input', { type: 'password', class: 'ui-input', autocomplete: 'new-password' });
    const env = el('input', { type: 'text', class: 'ui-input', autocomplete: 'off', spellcheck: 'false', placeholder: 'OPENAI_API_KEY' });
    const model = el('input', { type: 'text', class: 'ui-input', autocomplete: 'off', spellcheck: 'false', list: 'cxn-models' });
    const modelList = el('datalist', { id: 'cxn-models' });
    const clear = el('input', { type: 'checkbox', class: 'ui-checkbox', id: 'cxn-clear' });
    const caps = el('div', { class: 'cxn-caps', role: 'group', 'aria-label': t('cxn.capabilities') });
    const result = resultBox();
    const keyRow = field(t('cxn.key'), key);
    const keyHint = el('span', { class: 'people-field-hint' });
    keyRow.append(keyHint);
    const envRow = el('details', { class: 'cxn-adv' }, [el('summary', { text: t('cxn.key_env_title') }), field(t('cxn.key_env_label'), env, t('cxn.key_env_hint'))]);
    const clearRow = el('label', { class: 'bkp-check', for: 'cxn-clear', hidden: '' }, [clear, ' ', t('cxn.clear_key')]);
    const save = el('button', { type: 'button', class: 'btn-primary', text: editing ? t('cxn.save') : t('cxn.add_save') });
    const testBtn = el('button', { type: 'button', class: 'btn-secondary', text: t('cxn.test') });
    const cancel = el('button', { type: 'button', class: 'btn-secondary', text: t('common.cancel') });
    const err = el('p', { class: 'lock-error', role: 'alert', hidden: '' });

    const paintCaps = (selected, offered) => {
      caps.textContent = '';
      for (const cap of ['chat', 'vision', 'images', 'embeddings', 'tools', 'agent']) {
        const id = `cxn-cap-${cap}`;
        const cb = el('input', { type: 'checkbox', id, class: 'ui-checkbox', value: cap });
        cb.checked = selected.includes(cap);
        caps.append(el('label', { class: 'bkp-check', for: id }, [cb, ' ', t(CAP_KEYS[cap])]));
      }
      void offered;
    };
    const selectedCaps = () => [...caps.querySelectorAll('input:checked')].map((i) => i.value);
    const typeOf = () => state.types.find((x) => x.id === typeSel.value) || state.types[0];

    function applyType(resetFields) {
      const d = formDefaults(typeOf());
      if (resetFields) { name.value = d.name; url.value = d.base_url; env.value = d.keyEnv; paintCaps(d.capabilities); }
      keyRow.hidden = !d.needsKey;
      envRow.hidden = !d.needsKey;
      key.required = d.keyRequired && !editing;
      keyHint.textContent = d.keyRequired ? t('cxn.key_required_hint') : t('cxn.key_optional_hint');
      url.readOnly = false;
      url.placeholder = d.base_url || 'http://host:port/v1';
    }

    if (editing) {
      typeSel.value = existing.type; typeSel.disabled = true;
      name.value = existing.name; url.value = existing.base_url; model.value = existing.default_model || '';
      env.value = existing.credential.env || '';
      paintCaps(existing.capabilities || []);
      applyType(false);
      key.placeholder = existing.credential.has_key ? t('cxn.key_saved_placeholder') : '';
      clearRow.hidden = !existing.credential.has_key;
      (existing.models || []).forEach((m) => modelList.append(el('option', { value: m })));
    } else {
      typeSel.value = (preset && preset.type) || state.types[0].id;
      applyType(true);
      if (preset) { url.value = preset.base_url; if (preset.label) name.value = preset.label; }
    }
    typeSel.addEventListener('change', () => applyType(true));

    const gather = () => ({
      type: typeSel.value, name: name.value, base_url: url.value, api_key: key.value, api_key_env: env.value,
      default_model: model.value, capabilities: selectedCaps(), clearKey: clear.checked,
    });
    const pickModel = (m) => { model.value = m; };
    testBtn.addEventListener('click', async () => {
      testBtn.disabled = true;
      result.hidden = false; result.className = 'cxn-result cxn-idle'; result.textContent = t('cxn.testing');
      try {
        const g = gather();
        const r = await api.testConnectionForm({ type: g.type, base_url: g.base_url, api_key: g.api_key,
          ...(editing ? { use_connection: existing.id } : {}) });
        paintResult(result, r, pickModel);
        if (r.models) { modelList.textContent = ''; r.models.forEach((m) => modelList.append(el('option', { value: m }))); }
      } catch (e) { paintResult(result, { ok: false, message: errorText(e) }); }
      finally { testBtn.disabled = false; }
    });
    save.addEventListener('click', async () => {
      err.hidden = true;
      save.disabled = true;
      try {
        const g = gather();
        if (editing) await api.patchConnection(existing.id, formPayload(g, { editing: true }));
        else await api.createConnection(formPayload(g));
        toast(t('cxn.saved'));
        closeForm();
        deps.onChanged && deps.onChanged();
        await load();
      } catch (e) {
        err.textContent = errorText(e); err.hidden = false;
      } finally { save.disabled = false; }
    });
    cancel.addEventListener('click', closeForm);

    formHost.append(el('div', { class: 'cxn-form dev-group' }, [
      el('h3', { class: 'dev-group-title', text: editing ? t('cxn.edit_title', { name: existing.name }) : t('cxn.add_title') }),
      field(t('cxn.type'), typeSel), field(t('cxn.name'), name), field(t('cxn.address'), url, t('cxn.address_hint')),
      keyRow, envRow, clearRow,
      field(t('cxn.default_model'), model, t('cxn.default_model_hint')), modelList,
      el('fieldset', { class: 'cxn-capfield' }, [el('legend', { text: t('cxn.capabilities') }), caps]),
      err, result,
      el('div', { class: 'cxn-actions' }, [testBtn, save, cancel]),
    ]));
    name.focus();
  }

  addBtn.addEventListener('click', () => openForm(null));
  findBtn.addEventListener('click', async () => {
    findBtn.disabled = true;
    found.hidden = false; found.textContent = t('cxn.searching');
    try {
      const d = await api.discoverConnections({ lan: lanBox.checked });
      found.textContent = '';
      if (!d.found.length) { found.append(el('p', { class: 'muted', text: t('cxn.found_none') })); return; }
      found.append(el('p', { class: 'dev-group-hint', text: t('cxn.found_title', { count: d.found.length }) }));
      for (const f of d.found) {
        const add = el('button', { type: 'button', class: 'btn-secondary', text: f.already_added ? t('cxn.already_added') : t('cxn.use_this'), 'aria-label': t('cxn.use_aria', { name: f.product }) });
        if (f.already_added) add.disabled = true;
        add.addEventListener('click', () => { openForm(null, { type: f.type, base_url: f.base_url, label: f.product }); });
        found.append(el('div', { class: 'dev-row' }, [
          el('span', { class: 'dev-row-text' }, [el('strong', { text: f.product }), ' ', el('span', { class: 'dev-row-hint', text: `${f.base_url}${f.version ? ` · ${f.version}` : ''}${f.models && f.models.length ? ` · ${t('cxn.ok_models', { count: f.models.length })}` : ''}` })]),
          add,
        ]));
      }
    } catch (e) { found.textContent = errorText(e); }
    finally { findBtn.disabled = false; }
  });
  adoptBtn.addEventListener('click', async () => {
    try {
      const r = await api.adoptConnections();
      toast(r.linked ? t('cxn.adopted', { count: r.linked }) : t('cxn.adopted_none'));
      deps.onChanged && deps.onChanged();
      load();
    } catch (e) { toast(errorText(e), true); }
  });

  await load();
  return { reload: load, openForm, count: () => state.list.length };
}
