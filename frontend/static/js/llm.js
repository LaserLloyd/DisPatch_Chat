// "Connect an AI" — the direct-provider setup panel.
//
// Same contract as dashboard.js and reactions.js: this module owns no app
// state. main.js hands it a small `ctx` (toast, the bot-selection callback,
// the lock handler) and everything else it needs it fetches itself.
//
// The markup lives in index.html — now as the 🔌 AI models pane inside the
// Settings modal, previously as a `.modal-backdrop` of its own. Either way the
// app-wide focus trap, the inert-behind behaviour and the Escape handler apply
// without a line of code here, which was the whole reason not to build the
// shell dynamically the way the dashboard does.
//
// What it is for: a fresh install has no agent backend, so nothing answers.
// This is the sixty-second path from that to a working assistant — pick a
// provider, paste a key (or don't, for LM Studio and Ollama), press Test,
// press Save. The saved provider becomes a real bot in config.yaml and chats
// through the normal thread machinery; nothing here is a scratch playground.

import { el, railIcon, RAIL_ICONS } from './util.js?v=20';
import { t, applyDom } from './i18n.js?v=4';

// ===================== State =====================

const L = {
  providers: [],       // preset table from the server
  connected: [],       // bots that already have an api block (key redacted)
  bots: [],            // /api/bots/all rows: the direct-API bots this panel can connect (2.3.0)
  models: [],          // model ids from the last successful Test
  testing: false,
  saving: false,
  // Which provider the current `models` list belongs to. A provider switch
  // must invalidate it — offering OpenAI's catalogue for an Ollama box is
  // worse than offering nothing.
  modelsFor: '',
};

let ctx = {
  toast: () => {},
  isDecoy: () => false,
  onLocked: () => {},
  onConnected: async () => {},   // main.js: refresh roster, select bot, open chat
  // Settings owns the dialog: these are how this module asks for its tab, gets
  // out of the way, and answers "am I the pane on screen?".
  openSettingsTab: () => {},
  closeSettings: () => {},
  settingsTabActive: () => false,
};

const $ = (id) => document.getElementById(id);

// ===================== Tiny REST client =====================
// Self-contained on the network side (same choice dashboard.js made) so adding
// this panel touches no existing frontend file beyond the wiring. The error
// shape matches api.js's ("<status>: detail") so toasts read identically.

async function json(url, opts = {}) {
  const r = await fetch(url, { headers: { Accept: 'application/json' }, ...opts });
  if (!r.ok) {
    let body = null;
    try { body = await r.json(); } catch { /* not JSON — use the status text */ }
    let detail = (body && (body.detail || body.error)) || r.statusText;
    if (detail && typeof detail === 'object') {
      try { detail = JSON.stringify(detail); } catch { detail = String(detail); }
    }
    const err = new Error(`${r.status}: ${detail}`);
    err.status = r.status;
    err.locked = r.status === 401 || !!(body && (body.locked || body.decoy));
    throw err;
  }
  return r.status === 204 ? null : r.json();
}

const post = (url, body) => json(url, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
  body: JSON.stringify(body),
});

const cleanErr = (e) => String((e && e.message) || e).replace(/^\d+:\s*/, '');

// ===================== Open / close =====================

/** Every entry point that used to open the panel — the gear rail's 🔌, the
 *  first-run card in an empty chat. It opens Settings on the AI tab now, and
 *  the tab controller calls activateLlmPanel() once that pane is visible. */
export async function openLlmPanel(next = {}) {
  ctx = { ...ctx, ...next };
  // Belt-and-braces with the tab's own visibility rule: the routes are
  // full-session only, so a Safe-Mode caller would get a 403 anyway — but the
  // panel should never appear in the first place.
  if (ctx.isDecoy()) return;
  ctx.openSettingsTab('ai');
}

/** Load the provider table and paint the pane. Called when the AI tab becomes
 *  visible, which is the moment the fetch is actually worth making. */
export async function activateLlmPanel(next = {}) {
  ctx = { ...ctx, ...next };
  if (ctx.isDecoy()) return;
  resetResult();
  try {
    const r = await json('/api/llm/providers');
    L.providers = r.providers || [];
    L.connected = r.connected || [];
  } catch (e) {
    if (e.locked) { ctx.onLocked(); closeLlmPanel(); return; }
    ctx.toast(cleanErr(e), true);
    closeLlmPanel();
    return;
  }
  // The roster, for "Bot to connect": a direct-API bot (Atlas on a fresh install,
  // or any the owner added) or a new one. A failure here only loses the picker.
  try {
    const r = await json('/api/bots/all');
    L.bots = (r.bots || []).filter((b) => b && b.backend === 'direct');
  } catch { L.bots = []; }
  renderTargets();
  renderProviders();
  applyTarget();
}

// ===================== Bot to connect (2.3.0) =====================

export const NEW_BOT = '__new__';

/** The direct-API bot rows, with "New bot…" last. The default is the first
 *  bot that is not connected yet (Atlas on a fresh install), else the first. */
function renderTargets() {
  const sel = $('llm-target');
  if (!sel) return;
  sel.innerHTML = '';
  for (const b of L.bots) {
    const status = b.connected
      ? t('llm.target_connected', { provider: providerLabel(b.provider), model: b.model || '' })
      : t('llm.target_unconnected');
    sel.append(el('option', { value: b.id, text: `${b.name} — ${status}` }));
  }
  sel.append(el('option', { value: NEW_BOT, text: t('llm.target_new') }));
  const first = L.bots.find((b) => !b.connected) || L.bots[0];
  sel.value = first ? first.id : NEW_BOT;
}

function providerLabel(id) {
  const p = providerById(id);
  return p ? p.label : (id || '');
}

/** The bot picked in "Bot to connect": its row, or null for "New bot…". */
function targetBot() {
  const sel = $('llm-target');
  const id = sel ? sel.value : NEW_BOT;
  return id === NEW_BOT ? null : (L.bots.find((b) => b.id === id) || null);
}

/** Everything that follows the picked bot: the provider it already uses, its
 *  model, whether a key is saved, the lookups switch, and the name field. */
function applyTarget() {
  const b = targetBot();
  const sel = $('llm-provider');
  if (b && b.connected && b.provider && providerById(b.provider)) sel.value = b.provider;
  const tools = $('llm-tools');
  if (tools) tools.checked = b ? b.tools !== false : true;
  const adv = $('llm-advanced');
  if (adv && !b) adv.open = true;       // a new bot wants a name
  applyProvider();
  if (b && b.connected && b.model) {
    renderModels(L.models.length ? L.models : [b.model], b.model);
    $('llm-key').placeholder = t('llm.api_key_saved');
  }
}

/** The id a new bot gets from its name: lowercase, [a-z0-9_-], 1–32 chars. */
export function slugForBot(name) {
  const s = String(name || '').toLowerCase().replace(/[^a-z0-9_-]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 32);
  return s || 'assistant';
}

/** "I am done with this screen" — which now means closing Settings, because
 *  the only callers are the successful save (which opens a chat with the new
 *  bot) and the drop-to-Safe-Mode teardown. Guarded so neither closes a
 *  Settings modal that is sitting on somebody else's tab. */
export function closeLlmPanel() {
  if (llmPanelOpen()) ctx.closeSettings();
}

export function llmPanelOpen() {
  return !!ctx.settingsTabActive('ai');
}

// Re-translate the parts of the panel the DOM pass cannot reach.
//
// applyDom(document) on a language switch handles every `data-i18n` node, which
// covers the labels and hints — but not a placeholder written from JS, and not
// the two notes whose KEY changes with state. Called from main.js's
// reRenderForLocale while the panel is open, the same way the Reaction Manager
// is repainted (and for the same reason: those strings otherwise sit in the
// previous language until the modal is reopened).
//
// Deliberately NOT applyProvider(): that re-reads the saved config into the
// fields, which would throw away whatever the operator has typed. Nothing here
// touches a value.
//
// The one thing left alone is the Test result line — it holds either a
// provider's own sentence (never translated; see the comment at runTest) or a
// count formatted at the time it ran. Re-running the test is what refreshes it,
// and inventing a translated placeholder for a result that is no longer live
// would be worse than leaving the real answer on screen.
export function repaintLlmPanel() {
  if (!llmPanelOpen()) return;
  const p = providerById($('llm-provider').value);
  const api = ((p && connectedFor(p.id)) || {}).api || {};
  $('llm-key').placeholder = api.has_key ? t('llm.api_key_saved') : t('llm.api_key_placeholder');
  $('llm-model-text').placeholder = t('llm.model_placeholder');
  if (p) setNote('llm-base-note', p.local ? 'llm.base_url_local_hint' : 'llm.base_url_hint');
  setNote('llm-model-note', !$('llm-model').hidden ? 'llm.model_listed'
    : (L.modelsFor ? 'llm.model_none_listed' : 'llm.model_pick'));
}

// ===================== Provider select =====================

function providerById(id) {
  return L.providers.find((p) => p.id === id) || null;
}

function connectedFor(providerId) {
  // Matching on provider (not bot id) is what makes re-opening the panel for a
  // provider you already set up feel like an EDIT rather than a second copy:
  // the server derives the id the same way.
  return L.connected.find((b) => (b.api || {}).provider === providerId) || null;
}

function renderProviders() {
  const sel = $('llm-provider');
  sel.innerHTML = '';
  for (const p of L.providers) {
    // Local servers first-class in the label: "LM Studio" alone does not tell a
    // new operator that it needs no key and no account.
    sel.append(el('option', { value: p.id, text: p.label }));
  }
  // Default to whatever is already connected, else the first preset. A box
  // running LM Studio is the common case, and it is first in the table.
  const existing = L.connected[0];
  sel.value = (existing && (existing.api || {}).provider) || (L.providers[0] || {}).id || '';
}

// Everything that depends on WHICH provider is selected, in one pass.
function applyProvider() {
  const p = providerById($('llm-provider').value);
  if (!p) return;
  const existing = connectedFor(p.id);
  const api = (existing && existing.api) || {};

  $('llm-base-url').value = api.base_url || p.base_url || '';
  $('llm-base-url').readOnly = false;
  $('llm-base-url').placeholder = p.base_url || 'https://…/v1';
  setNote('llm-base-note', p.local ? 'llm.base_url_local_hint' : 'llm.base_url_hint');

  const keyField = $('llm-key-field');
  keyField.hidden = !p.key_accepted;
  $('llm-key').value = '';
  $('llm-key').placeholder = api.has_key ? t('llm.api_key_saved') : t('llm.api_key_placeholder');
  $('llm-key-env').value = api.api_key_env || '';
  $('llm-key-env').placeholder = p.key_env || 'MY_API_KEY';

  const docs = $('llm-docs');
  docs.hidden = !p.docs;
  if (p.docs) docs.href = p.docs;

  const target = targetBot();
  $('llm-name').value = target ? target.name : ((existing && existing.name) || '');
  $('llm-name').placeholder = p.bot_name || p.label;
  // The same field the Bots tab's Personality editor shows (advisor.persona
  // for a direct-API bot, served on the row as `persona`), so the two can
  // never disagree. A new bot starts blank (= the shipped default).
  $('llm-system').value = target ? String(target.persona || '') : (api.system_prompt || '');

  // A provider switch invalidates the model list, but its OWN suggestions are
  // still worth offering — they save a Test round-trip for the vendors whose
  // model ids are stable.
  if (L.modelsFor !== p.id) {
    L.models = [];
    L.modelsFor = '';
    resetResult();
  }
  const suggested = L.models.length ? L.models : (p.models || []);
  renderModels(suggested, api.model || (p.models || [])[0] || '');
}

function setNote(id, key) {
  const node = $(id);
  if (!node) return;
  node.setAttribute('data-i18n', key);
  node.textContent = t(key);
}

// ===================== Model picker =====================

// Two controls, deliberately: a <select> when the server told us what it has,
// and a free-text input that is ALWAYS available underneath. Servers that do
// not publish /models are common (and a brand-new model is always missing from
// a list somebody cached), so a picker with no escape hatch is a dead end.
function renderModels(list, selected) {
  const sel = $('llm-model');
  const text = $('llm-model-text');
  sel.innerHTML = '';
  const have = (list || []).filter(Boolean);
  sel.hidden = !have.length;
  for (const m of have) sel.append(el('option', { value: m, text: m }));
  if (have.length) {
    sel.value = have.includes(selected) ? selected : have[0];
    text.value = '';
    text.placeholder = t('llm.model_placeholder');
    setNote('llm-model-note', 'llm.model_listed');
  } else {
    text.value = selected || '';
    setNote('llm-model-note', L.modelsFor ? 'llm.model_none_listed' : 'llm.model_pick');
  }
}

function chosenModel() {
  const typed = $('llm-model-text').value.trim();
  if (typed) return typed;                       // free text always wins
  const sel = $('llm-model');
  return sel.hidden ? '' : (sel.value || '').trim();
}

// ===================== Result line =====================

function resetResult() {
  const node = $('llm-result');
  node.textContent = '';
  node.className = 'llm-result';
}

function showResult(text, tone) {
  const node = $('llm-result');
  node.textContent = text;
  node.className = `llm-result${tone ? ` ${tone}` : ''}`;
}

// ===================== Test =====================

async function runTest() {
  if (L.testing) return;
  const p = providerById($('llm-provider').value);
  if (!p) { showResult(t('llm.need_provider'), 'bad'); return; }
  L.testing = true;
  $('llm-test').disabled = true;
  showResult(t('llm.testing'), '');
  try {
    const target = targetBot();
    const r = await post('/api/llm/test', {
      provider: p.id,
      base_url: $('llm-base-url').value.trim(),
      // A saved key never comes back out of the API, so re-testing a connected
      // bot posts an empty key; the server then falls back to the named
      // environment variable, else to the key stored on `target` for this same
      // provider (main._stored_probe_key). Without `target` that fallback has
      // nothing to look at and the Test says "needs an API key".
      api_key: $('llm-key').value,
      api_key_env: $('llm-key-env').value.trim(),
      target: target ? target.id : '',
      model: chosenModel(),
    });
    if (!r || !r.ok) {
      // The provider's own sentence, verbatim. Deliberately NOT a translated
      // key: the useful half is the model id / URL / status the server put in
      // it, and a wrapper phrase would only push that further down the line.
      // The probe always fills `error` when ok is false; the fallback exists
      // for a malformed response, not for a normal failure.
      showResult((r && r.error) || t('common.unknown'), 'bad');
      return;
    }
    const models = r.models || [];
    L.models = models;
    L.modelsFor = p.id;
    // Keep whatever the operator had already chosen if the server offers it —
    // re-testing after a typo in the key must not silently change the model.
    renderModels(models.length ? models : (p.models || []), chosenModel());
    showResult(models.length
      ? t('llm.test_ok', { count: models.length })
      : t('llm.test_ok_nolist'), 'good');
  } catch (e) {
    if (e.locked) { ctx.onLocked(); closeLlmPanel(); return; }
    showResult(cleanErr(e), 'bad');
  } finally {
    L.testing = false;
    $('llm-test').disabled = false;
  }
}

// ===================== Save =====================

async function save() {
  if (L.saving) return;
  const p = providerById($('llm-provider').value);
  if (!p) { showResult(t('llm.need_provider'), 'bad'); return; }
  const model = chosenModel();
  if (!model) { showResult(t('llm.need_model'), 'bad'); $('llm-model-text').focus(); return; }
  const target = targetBot();
  const name = $('llm-name').value.trim();
  if (!target && !name) { showResult(t('llm.need_name'), 'bad'); $('llm-advanced').open = true; $('llm-name').focus(); return; }
  L.saving = true;
  $('llm-save').disabled = true;
  showResult(t('llm.saving'), '');
  try {
    const r = await post('/api/llm/connect', {
      // 2.3.0: the connection lands on a bot (the advisor backend = "direct
      // API"): the one picked above, or a new one named in Advanced.
      backend: 'direct',
      target: target ? target.id : slugForBot(name),
      tools: !!($('llm-tools') && $('llm-tools').checked),
      provider: p.id,
      base_url: $('llm-base-url').value.trim(),
      // Blank means "keep the key already saved" — the server only overwrites
      // when a non-empty one arrives, so re-saving to change the model never
      // wipes the credential.
      api_key: $('llm-key').value,
      api_key_env: $('llm-key-env').value.trim(),
      model,
      name,
      system_prompt: $('llm-system').value.trim(),
    });
    const bot = (r && r.bot) || null;
    closeLlmPanel();
    if (bot) {
      ctx.toast(t('llm.saved', { name: bot.name }));
      await ctx.onConnected(bot);
    }
  } catch (e) {
    if (e.locked) { ctx.onLocked(); closeLlmPanel(); return; }
    showResult(cleanErr(e), 'bad');
  } finally {
    L.saving = false;
    $('llm-save').disabled = false;
  }
}

// ===================== First-run card =====================

// Rendered into the chat empty state by main.js when there is nothing to talk
// to yet. Returns a node (never touches the DOM itself) so the caller keeps
// full control of when and where it appears.
export function firstRunCard(onOpen) {
  return el('div', { class: 'llm-firstrun' }, [
    el('div', { class: 'empty-emoji' }, [railIcon(RAIL_ICONS.plug)]),
    el('p', { class: 'llm-firstrun-title', 'data-i18n': 'llm.card_title',
      text: t('llm.card_title') }),
    el('p', { class: 'llm-firstrun-body', 'data-i18n': 'llm.card_body',
      text: t('llm.card_body') }),
    el('button', { class: 'btn-primary', id: 'llm-firstrun-btn',
      'data-i18n': 'llm.card_button', text: t('llm.card_button'),
      onclick: onOpen }),
  ]);
}

// ===================== Wiring =====================

let wired = false;

export function initLlmPanel(next = {}) {
  ctx = { ...ctx, ...next };
  if (wired) return;
  wired = true;
  // No ✕ / Cancel / backdrop-click of its own any more: the Settings dialog
  // owns all three, and a "Cancel" that only repeated the header ✕ would be a
  // dead button on a tab.
  $('llm-provider').addEventListener('change', applyProvider);
  if ($('llm-target')) $('llm-target').addEventListener('change', applyTarget);
  $('llm-test').addEventListener('click', runTest);
  $('llm-save').addEventListener('click', save);
  // Enter in any single-line field tests rather than submitting nothing —
  // these inputs are not in a <form>, so the browser does nothing by default.
  for (const id of ['llm-base-url', 'llm-key', 'llm-key-env', 'llm-model-text']) {
    $(id).addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); runTest(); }
    });
  }
  // The <select> is built from server data, so a language switch has to
  // re-apply the translated labels around it (applyDom only walks data-i18n
  // nodes, which the option list deliberately is not).
  applyDom($('spane-ai'));
}
