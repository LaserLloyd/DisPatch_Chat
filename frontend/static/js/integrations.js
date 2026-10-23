// Integrations (2.3.7, docs/design/integrations-2-3.md):
//
//   mountQuickSetup(host, deps)   Settings → AI & Connections: "Set up everything
//                                 with one provider" — one key, a checklist back.
//                                 deps.open: false = a closed disclosure.
//   mountImageSection(host, deps) Settings → AI & Connections: "Image generation" —
//                                 provider, base URL, model, key, Test, Save.
//   catalogGroup(deps)            Settings → Plugins & Apps: "Works with DisPatch"
//                                 (under Discover more), one card per companion
//                                 (app/catalog.yaml) with its live status, plus
//                                 "Show setup guide".
//
// Same contract as llm.js: owns no app state, fetches what it needs itself,
// and every route it calls is operator-only on the server. A key typed here is
// sent once and never comes back (the server answers `has_key` only).

import { t } from './i18n.js?v=4';
import { el, spriteIcon } from './util.js?v=20';

async function json(url, opts = {}) {
  const r = await fetch(url, { headers: { Accept: 'application/json' }, ...opts });
  let body = null;
  try { body = await r.json(); } catch { /* not JSON */ }
  if (!r.ok) {
    let detail = (body && (body.detail || body.error)) || r.statusText;
    if (detail && typeof detail === 'object') detail = JSON.stringify(detail);
    const err = new Error(String(detail || r.status));
    err.status = r.status;
    throw err;
  }
  return body;
}

const send = (method, url, body) => json(url, {
  method,
  headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
  body: JSON.stringify(body || {}),
});

const errText = (e) => String((e && e.message) || e || '');

function field(labelText, control, note) {
  const id = control.id;
  return el('div', { class: 'sec-field' }, [
    el('label', { for: id, text: labelText }),
    control,
    note || null,
  ]);
}

const note = (text, id) => el('p', { class: 'sec-note muted', id, text });

// ===================== One key, every capability =====================

const MARK = { ok: 'check', absent: null, failed: 'x', skipped: null };
const MARK_TEXT = { absent: '—', skipped: '•' };

// The last checklist, kept for the life of the page: connecting Atlas pushes a
// new roster, and the AI tab re-mounts on that — which used to wipe the result
// the operator was reading.
let lastResult = null;

export function renderChecklist(list, rows) {
  list.replaceChildren(...(rows || []).map((r) => el('li', {
    class: `qs-row qs-${r.status}`, dataset: { cap: r.id, status: r.status },
  }, [
    el('span', { class: 'qs-mark', 'aria-hidden': 'true' }, [MARK[r.status] ? spriteIcon(MARK[r.status], 14) : (MARK_TEXT[r.status] || '•')]),
    el('span', { class: 'qs-text' }, [
      el('strong', { text: r.label }),
      r.detail ? el('span', { class: 'qs-detail', text: ` — ${r.detail}` }) : null,
    ]),
  ])));
}

export async function mountQuickSetup(host, deps = {}) {
  if (!host) return;
  let plans = [];
  try {
    plans = (await json('/api/llm/quicksetup')).plans || [];
  } catch { host.replaceChildren(); return; }
  const sel = el('select', { id: 'qs-provider' },
    plans.map((p) => el('option', { value: p.id, text: p.label })));
  const key = el('input', {
    type: 'password', id: 'qs-key', autocomplete: 'off', spellcheck: 'false',
    placeholder: t('integrations.qs_key_placeholder'),
  });
  const docs = el('a', { class: 'llm-docs', target: '_blank', rel: 'noopener noreferrer',
    text: t('llm.docs_link') });
  const what = note('', 'qs-what');
  const go = el('button', { type: 'button', class: 'btn-primary', id: 'qs-go',
    text: t('integrations.qs_go') });
  const result = el('span', { class: 'llm-result', id: 'qs-result', role: 'status', 'aria-live': 'polite' });
  const list = el('ul', { class: 'qs-list', id: 'qs-list' });
  const paint = () => {
    const p = plans.find((x) => x.id === sel.value) || plans[0];
    if (!p) return;
    docs.href = p.docs || '#';
    docs.hidden = !p.docs;
    const caps = [t('integrations.cap_chat', { model: p.chat_model })];
    if (p.images) caps.push(t('integrations.cap_images'));
    if (p.embeddings) caps.push(t('integrations.cap_embeddings'));
    what.textContent = t('integrations.qs_what', { caps: caps.join(', ') });
  };
  sel.addEventListener('change', paint);
  paint();
  go.addEventListener('click', async () => {
    if (!key.value.trim()) { result.textContent = t('integrations.qs_need_key'); result.className = 'llm-result bad'; key.focus(); return; }
    go.disabled = true;
    result.className = 'llm-result';
    result.textContent = t('integrations.qs_running');
    list.replaceChildren();
    try {
      const r = await send('POST', '/api/llm/quicksetup', { provider: sel.value, api_key: key.value.trim() });
      key.value = '';
      const bad = (r.rows || []).some((x) => x.status === 'failed');
      lastResult = { provider: sel.value, rows: r.rows || [], bad };
      renderChecklist(list, r.rows);
      result.textContent = t(bad ? 'integrations.qs_done_partial' : 'integrations.qs_done');
      result.className = `llm-result ${bad ? 'bad' : 'good'}`;
      if (deps.onConfigured) await deps.onConfigured(r);
    } catch (e) {
      result.textContent = errText(e);
      result.className = 'llm-result bad';
    } finally {
      go.disabled = false;
    }
  });
  if (lastResult) {
    sel.value = lastResult.provider;
    paint();
    renderChecklist(list, lastResult.rows);
    result.textContent = t(lastResult.bad ? 'integrations.qs_done_partial' : 'integrations.qs_done');
    result.className = `llm-result ${lastResult.bad ? 'bad' : 'good'}`;
  }
  // Open when there is nothing to talk to yet (the empty state); a closed
  // disclosure once a connection exists (3.2).
  host.replaceChildren(el('details', { class: 'qs-box llm-body', id: 'qs-box', open: deps.open === false ? null : '' }, [
    el('summary', { text: t('integrations.qs_title') }),
    note(t('integrations.qs_hint')),
    field(t('llm.provider'), sel, docs),
    field(t('llm.api_key'), key, what),
    el('div', { class: 'llm-test-row' }, [go, result]),
    list,
  ]));
}

// ===================== Image generation =====================

const AUTO = '', NONE = 'none', SERVER = 'clawforge';

export function imageStatusText(state) {
  const cfg = (state && state.config) || {};
  const preset = (state.presets || []).find((p) => p.id === cfg.provider);
  if (state.flag_off) return t('integrations.img_status_flag_off');
  if (preset) {
    const src = cfg.key_source === 'shared' ? t('integrations.img_key_shared')
      : cfg.key_source === 'env' ? t('integrations.img_key_env') : '';
    return t('integrations.img_status_api', { provider: preset.label, model: cfg.model || preset.model }) + (src ? ` ${src}` : '');
  }
  if (cfg.provider === NONE) {
    return state.clawforge ? t('integrations.img_status_none_server') : t('integrations.img_status_off');
  }
  return state.clawforge ? t('integrations.img_status_server') : t('integrations.img_status_off');
}

export async function mountImageSection(host, deps = {}) {
  if (!host) return;
  let state;
  try { state = await json('/api/llm/image'); } catch (e) {
    host.replaceChildren(note(errText(e)));
    return;
  }
  const presets = state.presets || [];
  const sel = el('select', { id: 'img-provider' });
  const opts = [[AUTO, t('integrations.img_auto')], [NONE, t('integrations.img_none')]];
  if (state.clawforge) opts.push([SERVER, t('integrations.img_server')]);
  for (const p of presets) opts.push([p.id, p.label]);
  for (const [v, label] of opts) sel.append(el('option', { value: v, text: label }));
  const base = el('input', { type: 'url', id: 'img-base-url', autocomplete: 'off', spellcheck: 'false', inputmode: 'url' });
  const model = el('input', { type: 'text', id: 'img-model', autocomplete: 'off', spellcheck: 'false' });
  const key = el('input', { type: 'password', id: 'img-key', autocomplete: 'off', spellcheck: 'false' });
  const keyEnv = el('input', { type: 'text', id: 'img-key-env', autocomplete: 'off', spellcheck: 'false' });
  const docs = el('a', { class: 'llm-docs', target: '_blank', rel: 'noopener noreferrer', text: t('llm.docs_link') });
  const price = note('', 'img-price');
  const status = el('p', { class: 'sec-note', id: 'img-status', role: 'status' });
  const test = el('button', { type: 'button', class: 'btn-secondary', id: 'img-test', text: t('integrations.img_test') });
  const save = el('button', { type: 'button', class: 'btn-primary', id: 'img-save', text: t('integrations.img_save') });
  const result = el('span', { class: 'llm-result', id: 'img-result', role: 'status', 'aria-live': 'polite' });
  const preview = el('img', { class: 'img-preview', id: 'img-preview', alt: t('integrations.img_preview_alt'), hidden: '' });
  const apiFields = el('div', { class: 'img-api-fields', id: 'img-api-fields' }, [
    field(t('llm.base_url'), base),
    field(t('llm.model'), model),
    field(t('llm.api_key'), key, note(t('integrations.img_key_hint'))),
    field(t('llm.key_env_label'), keyEnv),
    el('div', {}, [docs, price]),
  ]);

  const paint = (keepInputs = false) => {
    const cfg = state.config || {};
    const p = presets.find((x) => x.id === sel.value);
    apiFields.hidden = !p;
    test.hidden = !p;
    if (p) {
      const same = cfg.provider === p.id;
      if (!keepInputs) {
        base.value = same && cfg.base_url ? cfg.base_url : p.base_url;
        model.value = same && cfg.model ? cfg.model : p.model;
        keyEnv.value = same ? (cfg.api_key_env || '') : '';
        key.value = '';
      }
      base.readOnly = false;
      base.placeholder = p.base_url || 'https://…/v1';
      keyEnv.placeholder = p.key_env || 'MY_API_KEY';
      key.placeholder = same && (cfg.has_key || cfg.key_source)
        ? t('llm.api_key_saved') : t('integrations.img_key_placeholder');
      docs.hidden = !p.docs;
      if (p.docs) docs.href = p.docs;
      price.textContent = p.price_note || '';
    }
    status.textContent = imageStatusText(state);
  };
  sel.value = (state.config && state.config.provider) || AUTO;
  if (![...sel.options].some((o) => o.value === sel.value)) sel.value = AUTO;
  sel.addEventListener('change', () => { preview.hidden = true; result.textContent = ''; paint(); });
  paint();

  const body = () => {
    const p = presets.find((x) => x.id === sel.value);
    // The preset's own base URL / model are sent as blanks so the file keeps
    // following the preset rather than freezing today's default.
    return {
      provider: sel.value,
      base_url: p && base.value.trim() !== p.base_url ? base.value.trim() : '',
      model: p && model.value.trim() !== p.model ? model.value.trim() : '',
      api_key: key.value.trim(),
      api_key_env: keyEnv.value.trim(),
    };
  };

  test.addEventListener('click', async () => {
    test.disabled = true;
    preview.hidden = true;
    result.className = 'llm-result';
    result.textContent = t('integrations.img_testing');
    try {
      const r = await send('POST', '/api/llm/image/test', body());
      if (r && r.ok) {
        preview.src = r.preview;
        preview.hidden = false;
        result.textContent = t('integrations.img_test_ok', { kb: Math.round((r.bytes || 0) / 1024) });
        result.className = 'llm-result good';
      } else {
        result.textContent = (r && r.error) || t('common.unknown');
        result.className = 'llm-result bad';
      }
    } catch (e) {
      result.textContent = errText(e);
      result.className = 'llm-result bad';
    } finally {
      test.disabled = false;
    }
  });
  save.addEventListener('click', async () => {
    save.disabled = true;
    try {
      state = await send('PUT', '/api/llm/image', body());
      paint();
      result.textContent = t('integrations.img_saved');
      result.className = 'llm-result good';
      if (deps.toast) deps.toast(t('integrations.img_saved'));
    } catch (e) {
      result.textContent = errText(e);
      result.className = 'llm-result bad';
    } finally {
      save.disabled = false;
    }
  });

  host.replaceChildren(el('section', { class: 'img-section llm-body', id: 'img-section', 'aria-labelledby': 'img-h' }, [
    el('h3', { class: 'dev-group-title', id: 'img-h', text: t('integrations.img_title') }),
    note(t('integrations.img_hint')),
    field(t('integrations.img_provider'), sel, status),
    apiFields,
    el('div', { class: 'llm-test-row' }, [test, save, result]),
    preview,
  ]));
}

// ===================== Works with DisPatch =====================

const STATUS_KEY = {
  connected: 'integrations.st_connected',
  installed: 'integrations.st_installed',
  not_configured: 'integrations.st_not_configured',
  bundled: 'integrations.st_bundled',
  unknown: 'integrations.st_unknown',
};

export function catalogCard(entry) {
  const links = [];
  if (entry.repo) {
    links.push(el('a', { href: entry.repo, target: '_blank', rel: 'noopener noreferrer',
      class: 'cat-link', text: t('integrations.repo_link') }));
  } else if (entry.repo_note) {
    links.push(el('span', { class: 'cat-note', text: entry.repo_note }));
  }
  if (entry.docs) {
    links.push(el('a', { href: entry.docs, target: '_blank', rel: 'noopener noreferrer',
      class: 'cat-link', text: t('integrations.howto_link') }));
  }
  return el('div', { class: `cat-card cat-${entry.status}`, dataset: { cat: entry.id, status: entry.status } }, [
    el('div', { class: 'cat-head' }, [
      el('strong', { class: 'cat-name', text: entry.name }),
      el('span', { class: `cat-status cat-status-${entry.status}`, text: t(STATUS_KEY[entry.status] || STATUS_KEY.unknown) }),
    ]),
    el('p', { class: 'cat-summary', text: entry.summary }),
    entry.detail ? el('p', { class: 'cat-detail muted', text: entry.detail }) : null,
    entry.connects ? el('p', { class: 'cat-connects muted', text: entry.connects }) : null,
    entry.toggle ? el('p', { class: 'cat-toggle muted', text: t('integrations.toggle_below') }) : null,
    links.length ? el('p', { class: 'cat-links' }, links) : null,
  ]);
}

export function catalogGroup(deps = {}) {
  const body = el('div', { class: 'dev-group-body cat-grid', id: 'plugins-catalog-body' },
    [el('p', { class: 'muted', text: t('common.loading') })]);
  const guideBtn = el('button', { type: 'button', class: 'btn-secondary', id: 'cat-guide',
    text: t('integrations.show_guide') });
  guideBtn.addEventListener('click', async () => {
    guideBtn.disabled = true;
    try {
      await send('POST', '/api/guide/seed?name=set-up-add-ons', {});
      if (deps.toast) deps.toast(t('integrations.guide_added'));
      if (deps.onGuide) await deps.onGuide();
    } catch (e) {
      if (deps.toast) deps.toast(e.status === 409 ? t('integrations.guide_exists') : errText(e), e.status !== 409);
    } finally {
      guideBtn.disabled = false;
    }
  });
  const section = el('section', { class: 'dev-group', id: 'plugins-catalog', 'aria-labelledby': 'plugins-catalog-h' }, [
    el('h3', { class: 'dev-group-title', id: 'plugins-catalog-h', text: t('integrations.catalog_title') }),
    el('p', { class: 'dev-group-hint', text: t('integrations.catalog_hint') }),
    el('div', { class: 'ext-actions' }, [guideBtn]),
    body,
  ]);
  const load = deps.loadCatalog || (() => json('/api/plugins/catalog'));
  section.ready = (async () => {
    try {
      const r = await load();
      body.replaceChildren(...(r.entries || []).map(catalogCard));
    } catch (e) {
      body.replaceChildren(el('p', { class: 'muted', role: 'alert', text: errText(e) }));
    }
  })();
  return section;
}
