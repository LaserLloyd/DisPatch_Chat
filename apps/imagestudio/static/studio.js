// Image Studio — the page (/apps/imagestudio/).
//
// One form, one grid. The server (apps/imagestudio/backend.py) owns the
// provider call and the key; this page only sends the request the form
// describes and shows what comes back. Pictures open in the SHELL's own
// lightbox (app-sdk lightbox(): zoom, pan, swipe, arrows, download, Esc,
// No-Image Mode). Only when the page is NOT in the shell (its own tab or
// window) does it draw a small viewer of its own, because the post to the
// shell would go nowhere.
//
// No-Image Mode: with the shell's flag set (localStorage 'dispatch-nim') the
// grid shows labelled placeholders and never creates an <img>, so nothing is
// fetched; the lightbox refuses too.

import {
  api, t, ready, theme, toast, lightbox, el, railIcon, RAIL_ICONS,
} from '/static/js/app-sdk.js?v=7';

/** A 403: this profile may not use Image Studio (4.0; 3.x sent a Safe-Mode DecoyError). */
const refused = (e) => !!e && e.status === 403;
import {
  PROMPT_MAX, isGenerateShortcut, shortcutLabel, promptStatus, canGenerate, clampCount,
  buildRequest, cropRect, defaultCropPos, galleryItems, isMetered,
  SORTS, normalizeSort, sortRows, canStartAnother, SHORTCUT_DEBOUNCE_MS,
  DELETE_SCOPES, normalizeScope, deleteConfirmKey, sendMetadata, DELETE_SCOPE_LABELS, estimateKey, modelLabel,
  CURRENCIES, normalizeCurrency, balanceLine, BROWSE_SORTS, normalizeBrowseSort, compactCount, browseLabel,
  cleanBrowseRows, rememberModelName, browseWords, browseMatches,
  ratioText,
  activeModel, isH3Model, keepBrowsePicks, modelCaps, modelPromptMax, modelsNote, refreshSummary,
  stepIndex, wheelShouldChain, canScroll, INNER_SCROLLPORTS,
  draftKey, serializeDraft, parseDraft, resolveDraft, inflightExtra, pollShouldContinue, INFLIGHT_POLL_MS,
} from './studio-logic.js?v=14';
import { installExpiredImages } from '/static/js/media-expired.js?v=1';

export const API = '/api/apps/imagestudio';

const S = {
  info: null,            // GET /state
  images: [],            // GET /images, oldest -> newest
  refreshing: false,     // POST /models/refresh in flight
  busy: false,           // any generation in flight (aria only: Generate is never blocked)
  pending: [],           // this page's jobs: {key, body, state: 'running'|'failed', error}
  jobSeq: 0,
  lastKey: 0,            // when the shortcut last started a job (debounce)
  reference: null,       // {kind, ..., preview}
  status: '',
  statusError: false,
  nim: false,
  estKey: '',            // the form state the shown estimate is for
  estText: '',           // the estimate line (empty = none yet)
  estSeq: 0,
  sort: 'newest',        // the grid's order (SORTS), remembered per device; newest first when none saved
  pay: '',               // Civitai: which Buzz pays ('' = Civitai decides), remembered per device
  buzz: { state: 'idle', text: '', scope: false, helpUrl: '' },   // the Buzz balance line
  buzzLoaded: false,     // the balance was fetched once for this page
  browse: { open: false, sort: 'week', query: '', rows: [], state: 'idle', error: '' },
  workflows: { rows: [], tried: false, loading: false, error: '', sig: '' },   // the image server's gallery
  picker: { open: false, query: '', mode: '' },   // the card under the Model field
  modelMode: 'text',        // the model control on the page: 'select' or 'text'
  viewer: null,             // the picture viewer of this page (only outside the shell)
  profileKey: '',           // GET /state profile_key: the draft is read and written under it, only once /state answered
  draftReady: false,        // the draft was applied: from now on the form is saved
  draftDirty: false,        // the user changed the form here: only then is a draft written
  baseline: '',             // the form's serialised defaults: a form equal to it stores no draft
  poll: { timer: null, since: 0 },   // pictures the server is still making (not started by this page)
};
const R = {};            // DOM refs
const SORT_LABELS = { oldest: 'sort.oldest', newest: 'sort.newest' };
// The grid's order on this device (privacy.js APP_KEYS wipes it in privacy mode).
const SORT_KEY = 'dispatch-imagestudio-sort';
// Which Buzz pays for a Civitai picture on this device (privacy.js APP_KEYS wipes it too).
const PAY_KEY = 'dispatch-imagestudio-pay';
// The form on this device, one key per profile (privacy.js APP_KEY_PREFIXES wipes the family).
// Form values only: never a picture, a data: URL or the reference image.
const DRAFT_PREFIX = 'dispatch-imagestudio-draft:';
const DRAFT_SAVE_MS = 400;
const PAY_LABELS = { '': 'pay.auto', blue: 'pay.blue', yellow: 'pay.yellow' };
const TYPE_LABELS = { blue: 'buzz.type_blue', yellow: 'buzz.type_yellow' };
const BROWSE_LABELS = { week: 'browse.sort_week', month: 'browse.sort_month', rated: 'browse.sort_rated', new: 'browse.sort_new' };
// More models than this and the names do not fit a dropdown: the compact
// chooser (the card under the Model field) takes over, so no provider may push
// a long list of names onto the page.
const PICKER_PILL_LIMIT = 8;

function nimOn() {
  try { return localStorage.getItem('dispatch-nim') === '1'; } catch { return false; }
}

function loadSort() {
  try { return normalizeSort(localStorage.getItem(SORT_KEY)); } catch { return 'newest'; }
}

function loadPay() {
  try { return normalizeCurrency(localStorage.getItem(PAY_KEY)); } catch { return ''; }
}

function savePay(value) {
  try { localStorage.setItem(PAY_KEY, value); } catch { /* private window: this tab only */ }
}

function saveSort(value) {
  try { localStorage.setItem(SORT_KEY, value); } catch { /* private window: this tab only */ }
}

/** The pictures in the order the grid shows them. */
function shown() { return sortRows(S.images, S.sort); }

function provider() {
  const id = R.provider ? R.provider.value : '';
  return (S.info && S.info.providers.find((p) => p.id === id)) || null;
}

function isCivitai() {
  const p = provider();
  return !!(p && p.shape === 'civitai');
}

function currentModel() {
  return activeModel(provider(), modelValue());
}

/** The model name the form is sending: the control that is on the page. */
function modelValue() {
  if (S.modelMode === 'select' && R.modelSelect) return R.modelSelect.value || '';
  return (R.model && R.model.value) || '';
}

/** Put a name in the model control that is on the page. */
function setModelValue(name) {
  const v = String(name == null ? '' : name);
  if (S.modelMode === 'select' && R.modelSelect) {
    if (R.modelSelect.value !== v) R.modelSelect.value = v;
  } else if (R.model && R.model.value !== v) {
    R.model.value = v;
  }
}

/** Put the focus on the model control that is on the page. */
function focusModel() {
  const c = S.modelMode === 'select' ? R.modelSelect : R.model;
  if (c && !c.hidden) c.focus();
}

function caps() {
  return modelCaps(provider(), currentModel());
}

function setStatus(text, isError = false) {
  S.status = text; S.statusError = isError;
  if (R.status) {
    R.status.textContent = text;
    R.status.classList.toggle('st-error', !!isError);
  }
}

function formState() {
  return {
    prompt: R.prompt.value, provider: R.provider.value, model: modelValue(), ratio: R.ratio.value,
    n: R.count.value, optimizer: R.optimizer.checked, seed: R.seed.value, reference: S.reference ? S.reference.ref : null,
    currency: isCivitai() ? normalizeCurrency(R.pay.value) : '',
  };
}

// ---------------------------------------------------------------- the saved draft

/** The values of the form that a draft keeps. */
function draftForm() {
  return {
    provider: R.provider.value, model: modelValue(), prompt: R.prompt.value, ratio: R.ratio.value,
    n: R.count.value, seed: R.seed.value, optimizer: R.optimizer.checked,
  };
}

let draftTimer = null;
function scheduleDraftSave() {
  if (!S.draftReady) return;
  clearTimeout(draftTimer);
  draftTimer = setTimeout(saveDraftNow, DRAFT_SAVE_MS);
}

/** Write the form (or drop the draft when the form is just the defaults). Never throws. */
function saveDraftNow() {
  clearTimeout(draftTimer);
  // Nothing the user did yet: keep what is stored. A draft that could not be put
  // back in full (its provider is off for now) must not be cut down or wiped by a
  // page that merely opened.
  if (!S.draftReady || !S.draftDirty || !R.prompt) return;
  const key = draftKey(DRAFT_PREFIX, S.profileKey);
  if (!key) return;
  const text = serializeDraft(draftForm(), S.info ? S.info.prompt_max : PROMPT_MAX);
  try {
    if (!text || text === S.baseline) localStorage.removeItem(DRAFT_PREFIX + S.profileKey);
    else localStorage.setItem(DRAFT_PREFIX + S.profileKey, text);
  } catch { /* private window / quota: the form just is not remembered */ }
}

function loadDraft() {
  if (!draftKey(DRAFT_PREFIX, S.profileKey)) return null;
  try { return parseDraft(localStorage.getItem(DRAFT_PREFIX + S.profileKey)); } catch { return null; }
}

/** Put a saved draft back on the form. Pieces that no longer fit are dropped, the rest
 *  stays. Starts nothing: no generation, and the estimate follows the form as it always does. */
function applyDraft(draft) {
  const fit = resolveDraft(draft, {
    providers: S.info.providers, ratios: S.info.ratios,
    workflowNames: (S.workflows.rows || []).map((w) => w.name),
  });
  if (fit.provider) {
    R.provider.value = fit.provider;
    refreshForm();                       // the model control follows the provider
    if (fit.model) setModelValue(fit.model);
  }
  if (fit.prompt) R.prompt.value = fit.prompt;
  if (fit.ratio) R.ratio.value = fit.ratio;
  refreshForm();                         // caps (count, seed, optimizer) now match the model
  if (fit.seed && !R.seedRow.hidden) R.seed.value = fit.seed;
  if (fit.optimizer && !R.optimizerRow.hidden) R.optimizer.checked = true;
  if (fit.n) R.count.value = String(clampCount(fit.n, caps().max_n));
  refreshForm();
}

/** Reset: the draft goes and the form returns to what a fresh page shows. */
function resetForm() {
  S.draftReady = false;
  S.draftDirty = false;
  clearTimeout(draftTimer);
  try { localStorage.removeItem(DRAFT_PREFIX + S.profileKey); } catch { /* nothing stored */ }
  if (S.info && S.info.default) R.provider.value = S.info.default;
  setModelValue('');
  clearStaleModel(provider());
  R.prompt.value = ''; R.ratio.value = '1:1'; R.count.value = '1'; R.seed.value = ''; R.optimizer.checked = false;
  S.picker.query = ''; R.pickerSearch.value = '';
  setReference(null);
  S.estKey = '';
  S.draftReady = true;
  refreshForm();
  S.baseline = serializeDraft(draftForm(), S.info ? S.info.prompt_max : PROMPT_MAX);
  R.prompt.focus();
}

// ---------------------------------------------------------------- pictures still being made

function renderInflight() {
  const n = S.info ? inflightExtra(S.info, running()) : 0;
  R.inflight.hidden = !n;
  R.inflight.textContent = n ? t(n === 1 ? 'inflight.one' : 'inflight.other', { n }) : '';
}

function stopPolling() { clearTimeout(S.poll.timer); S.poll.timer = null; }

/** The server keeps going after this page was unloaded: watch /state until it is done, and
 *  let the pictures appear. Stops at zero, when the page is hidden, and after about ten minutes. */
function startPolling() {
  stopPolling();
  renderInflight();
  if (!S.poll.since) S.poll.since = Date.now();
  const tick = async () => {
    S.poll.timer = null;
    const extra = S.info ? inflightExtra(S.info, running()) : 0;
    if (!pollShouldContinue({ extra, elapsedMs: Date.now() - S.poll.since, hidden: document.hidden })) {
      renderInflight();
      S.poll.since = 0;      // a later restart (the page shown again) gets a fresh window
      return;
    }
    try {
      await reloadState(false);
      S.images = (await api(`${API}/images`)).images;
      renderGrid();
    } catch { /* try again on the next tick */ }
    renderInflight();
    S.poll.timer = setTimeout(tick, INFLIGHT_POLL_MS);
  };
  S.poll.timer = setTimeout(tick, INFLIGHT_POLL_MS);
}

// ---------------------------------------------------------------- form

function refreshModelBits() {
  const p = provider();
  const c = caps();
  const st = promptStatus(R.prompt.value, modelPromptMax(p, currentModel(), S.info ? S.info.prompt_max : PROMPT_MAX));
  R.counter.textContent = t('prompt.counter', { n: st.n, max: st.max });
  R.counter.classList.toggle('st-over', st.over);
  R.count.max = String(c.max_n);
  if (Number(R.count.value) > c.max_n) R.count.value = String(c.max_n);
  // The count is only worth showing when the provider can make more than one.
  const many = Number(c.max_n) > 1;
  R.countField.hidden = !many;
  R.two.classList.toggle('st-one', !many);
  R.optimizerRow.hidden = !c.optimizer;
  R.h3Hint.hidden = !isH3Model(p, currentModel());
  R.seedRow.hidden = !c.seed;
  R.refBox.hidden = !c.reference;
  R.go.disabled = !canGenerate({ prompt: R.prompt.value, busy: false, provider: p && p.configured ? p.id : '' });
}

function renderModelNote() {
  const n = modelsNote(provider());
  R.modelNote.hidden = !n;
  R.modelNote.textContent = n ? t(n.key, n.vars) : '';
  R.modelRefresh.hidden = !(S.info && S.info.can_refresh_models);
  R.modelRefresh.disabled = S.refreshing;
  R.modelRefresh.textContent = t(S.refreshing ? 'models.refreshing' : 'models.refresh');
}

async function refreshModels() {
  if (S.refreshing) return;
  S.refreshing = true; renderModelNote();
  const chosen = { provider: R.provider.value, model: modelValue() };
  try {
    const r = await api(`${API}/models/refresh`, { method: 'POST' });
    S.info = { ...S.info, providers: keepBrowsePicks(S.info.providers, r.providers), default: r.default };
    renderProviders();
    if (S.info.providers.some((p) => p.id === chosen.provider)) R.provider.value = chosen.provider;
    setModelValue(chosen.model);
    const sum = refreshSummary(r, r.providers);
    setStatus(t(sum.key, sum.vars), sum.error);
    toast(t(sum.key, sum.vars));
  } catch (e) {
    setStatus(t('models.refresh_error', { error: e.message }), true);
  } finally {
    S.refreshing = false;
    refreshForm();
  }
}

function refreshForm() {
  const p = provider();
  refreshModelBits();
  S.busy = running() > 0;
  // Running jobs never disable Generate: more may start (the cap is checked on press).
  R.go.disabled = !canGenerate({ prompt: R.prompt.value, busy: false, provider: p && p.configured ? p.id : '' });
  R.go.textContent = t('generate');
  R.go.title = t('generate.tip', { keys: shortcutLabel(navigator.platform || '') });
  R.go.setAttribute('aria-busy', S.busy ? 'true' : 'false');
  R.cost.hidden = !isMetered(p);
  if (p && isMetered(p)) R.cost.textContent = t('cost.note', { provider: p.label, note: p.price_note });
  const names = (p && p.model_names) || {};
  renderModelControl(p, names);
  renderPickerMode(p);
  renderModelNote();
  refreshWorkflows();
  renderPickerList();
  R.est.hidden = !(p && p.estimate) || !S.estText;
  R.est.textContent = S.estText;
  const civ = isCivitai() && !!p.configured;
  R.payRow.hidden = !civ;
  R.browseBtn.hidden = !civ;
  R.buzzRow.hidden = !civ;
  if (!civ) closeBrowse();
  else if (!S.buzzLoaded) loadBalance();
  scheduleEstimate();
  scheduleDraftSave();
}

/** The model control follows the list the provider has:
 *
 *  * a practical list (a name to eight, and not the image server) is a dropdown
 *    of the display names -- the value is the model id, and the id rides in the
 *    option's title when the name is shorter than the id;
 *  * a provider that names no model at all keeps a text box, so an id can be
 *    typed;
 *  * a long list (and the image server) is the card under the Model field: no
 *    giant dropdown, and no second copy of the list.
 *
 *  The name in use travels with the control, so switching provider never
 *  silently drops it (nor carries a name the new provider never heard of). */
function renderModelControl(p, names) {
  const models = (p && p.models) || [];
  const practical = !isServerProvider() && models.length > 0 && models.length <= PICKER_PILL_LIMIT;
  const keep = modelValue();               // the name in use before the control changes
  S.modelMode = practical ? 'select' : 'text';
  R.modelSelect.hidden = !practical;
  R.model.hidden = practical;
  R.model.placeholder = p && p.shape === 'civitai' ? t('model.civitai_placeholder') : (p ? p.model : '');
  if (practical) {
    R.modelSelect.replaceChildren(...models.map((m) => {
      const o = el('option', { value: m, text: names[m] || m });
      // The id is long and the name is short: the id rides in the title.
      if (names[m] && names[m] !== m) o.setAttribute('title', m);
      return o;
    }));
    // The name in use stays; a name this provider does not list falls back to
    // the provider's own default (the same rule the request body follows).
    R.modelSelect.value = models.includes(keep) ? keep
      : (models.includes(p.model) ? p.model : (models[0] || ''));
  } else if (R.model.value !== keep) {
    R.model.value = keep;
  }
}

// ---------------------------------------------------------------- the picker
//
// One card under the Model field. For the image server it IS the gallery: the
// workflows with the picture each one made last, a search box above them and a
// count line, so the 34 names are listed once and the chosen one is marked in
// its own tile. For a provider with a long model list it is the same card in
// "models" mode, opened by a button.
//
// The card is a section: a header row (the title, the count, the chosen
// workflow) and a body that is CLOSED when the page loads. A small arrow
// (the chevron) opens and closes it; the header keeps saying the count and the
// chosen workflow, so a closed card still tells you what it holds.

/** The card under the Model field: the gallery for the image server, a compact
 *  chooser for a provider whose model list is too long for a dropdown. */
function renderPickerMode(p) {
  const models = (p && p.models) || [];
  const mode = isServerProvider() ? 'workflows'
    : (models.length > PICKER_PILL_LIMIT ? 'models' : '');
  // A different card starts closed: the gallery is closed on every page load.
  if (S.picker.mode !== mode) { S.picker.mode = mode; S.picker.open = false; }
  R.pickerCard.hidden = !mode;
  R.pickerToggle.hidden = mode !== 'models';
  R.pickerChevron.hidden = mode !== 'workflows';
  R.pickerBody.hidden = !S.picker.open;
  R.pickerToggle.setAttribute('aria-expanded', S.picker.open ? 'true' : 'false');
  R.pickerChevron.setAttribute('aria-expanded', S.picker.open ? 'true' : 'false');
  R.pickerChevron.setAttribute('aria-label', t(S.picker.open ? 'picker.collapse' : 'picker.expand'));
  R.pickerTitle.textContent = t(mode === 'models' ? 'picker.models_title' : 'picker.workflows_title');
  const search = t(mode === 'models' ? 'picker.search_models' : 'picker.search_workflows');
  R.pickerSearch.setAttribute('aria-label', search);
  R.pickerSearch.placeholder = search;
  R.pickerChosen.hidden = mode !== 'workflows';
}

/** Open or close the card's body. The chevron and the chooser button share it,
 *  so aria-expanded never disagrees with what is on the page. */
function setPickerOpen(open) {
  S.picker.open = !!open;
  R.pickerBody.hidden = !S.picker.open;
  R.pickerToggle.setAttribute('aria-expanded', S.picker.open ? 'true' : 'false');
  R.pickerChevron.setAttribute('aria-expanded', S.picker.open ? 'true' : 'false');
  R.pickerChevron.setAttribute('aria-label', t(S.picker.open ? 'picker.collapse' : 'picker.expand'));
  if (S.picker.open) { R.pickerSearch.focus(); scrollSelectedIntoView(); }
}

/** The header says which workflow is chosen, so a closed card still says what
 *  it is for. */
function renderChosen() {
  const name = modelValue();
  const row = (S.workflows.rows || []).find((w) => w.name === name);
  const label = row ? (row.display_name || row.name) : name;
  R.pickerChosen.hidden = S.picker.mode !== 'workflows' || !label;
  R.pickerChosen.textContent = label ? t('picker.chosen', { name: label }) : '';
}

/** The rows the card lists: the image server's workflows, or a provider's models. */
function pickerRows() {
  if (S.picker.mode === 'workflows') return S.workflows.rows || [];
  const p = provider();
  const names = (p && p.model_names) || {};
  return ((p && p.models) || []).map((m) => ({ name: m, display_name: names[m] || m }));
}

/** The rows that match what was typed: the name and the display name. */
function filterRows(rows, query) {
  const words = String(query || '').toLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) return rows;
  return rows.filter((r) => {
    const hay = `${r.name} ${r.display_name || ''}`.toLowerCase();
    return words.every((w) => hay.includes(w));
  });
}

/** What the card shows now: the rows that match, the count and the mark on the
 *  chosen one. The tiles themselves are kept (and their pictures with them);
 *  the ones that do not match are hidden. */
function renderPickerList() {
  const mode = S.picker.mode;
  const rows = pickerRows();
  const shown = filterRows(rows, S.picker.query);
  const keep = new Set(shown.map((r) => r.name));
  R.wfList.hidden = mode !== 'workflows';
  R.modelList.hidden = mode !== 'models';
  for (const li of R.wfList.children) li.hidden = !keep.has(li.dataset.name);
  if (mode === 'models') {
    R.modelList.replaceChildren(...shown.map((r) => {
      const b = el('button', { type: 'button', class: 'st-picker-row', title: r.name, text: r.display_name, 'aria-pressed': 'false' });
      b.dataset.name = r.name;
      b.addEventListener('click', () => pickModelName(r.name));
      return el('li', {}, [b]);
    }));
  }
  // While the list loads the header says so: a count of "0 of 0" would be a lie.
  R.pickerCount.hidden = !!S.workflows.loading;
  R.pickerCount.textContent = S.workflows.loading ? ''
    : t('picker.count', { shown: shown.length, total: rows.length });
  R.pickerEmpty.hidden = !(shown.length === 0 && S.picker.query);
  if (!R.pickerEmpty.hidden) R.pickerEmpty.textContent = t('picker.no_match', { query: S.picker.query });
  markSelected();
  renderChosen();
}

/** The chosen one is marked in the tile itself: the accent border, the badge
 *  and aria-pressed (aria-current as well). */
function markSelected() {
  const cur = modelValue();
  for (const li of R.wfList.children) {
    const b = li.querySelector('.st-wf-tile');
    if (!b) continue;
    const on = b.dataset.name === cur;
    b.setAttribute('aria-pressed', on ? 'true' : 'false');
    if (on) b.setAttribute('aria-current', 'true'); else b.removeAttribute('aria-current');
    const badge = b.querySelector('.st-wf-selected');
    if (badge) badge.hidden = !on;
  }
  for (const b of R.modelList.querySelectorAll('.st-picker-row')) {
    b.setAttribute('aria-pressed', b.dataset.name === cur ? 'true' : 'false');
  }
}

/** The chosen tile comes into view when the list appears. */
function scrollSelectedIntoView() {
  const b = R.wfList.querySelector('.st-wf-tile[aria-pressed="true"]');
  if (b) b.scrollIntoView?.({ block: 'nearest' });
}

/** Put a name in the Model box and keep the focus where it was. */
function pickModelName(name) {
  setModelValue(name);
  refreshForm();
  const again = [...R.modelList.querySelectorAll('.st-picker-row')].find((b) => b.dataset.name === name);
  if (again) again.focus();
}

/** The rows a person can walk to with the arrow keys. */
function pickerItems() {
  const list = S.picker.mode === 'models' ? R.modelList : R.wfList;
  return [...list.querySelectorAll('button')].filter((b) => !(b.closest('li') || {}).hidden);
}

/** The image server's provider: the gallery is shown for it and for nothing else. */
function isServerProvider() {
  const p = provider();
  return !!(p && p.id === 'workflows');
}

/** The names the provider on the form can actually draw with. */
function providerModels(p) {
  if (isServerProvider()) return (S.workflows.rows || []).map((w) => w.name);
  return (p && p.models) || [];
}

/** The Model box must not keep a name the new provider never heard of: the image
 *  server's workflow names mean nothing to another provider, and the other way
 *  round. A name typed for a provider that lists no names at all is left alone
 *  (that provider may still take it), but a workflow name never travels. */
function clearStaleModel(p) {
  const name = modelValue();
  if (!name) return;
  const server = !!(p && p.id === 'workflows');
  // The image server's own names belong to the image server: another provider
  // never heard of them, so they go even when that provider lists no models.
  if (!server && (S.workflows.rows || []).some((w) => w.name === name)) {
    setModelValue('');
    return;
  }
  const models = providerModels(p);
  if (!models.length) return;             // the provider lists no names: leave it be
  if (models.includes(name)) return;      // the new provider offers this name
  setModelValue('');
}

/** The thumbnail comes from this server, never from the image server's own address. */
const thumbUrl = (u) => `${API}/workflows/thumb?u=${encodeURIComponent(u)}`;

/** The image server's provider row learns the workflow names, so the Model box
 *  offers them too. Only the image server's own row: the names must never land
 *  on another provider (the list can arrive after the person switched). */
function teachProviderNames() {
  const p = ((S.info && S.info.providers) || []).find((x) => x && x.id === 'workflows');
  if (!p) return;
  p.models = S.workflows.rows.map((w) => w.name);
  p.model_names = Object.fromEntries(
    S.workflows.rows.map((w) => [w.name, w.display_name || w.name]));
}

/** The gallery: what the image server can render, with the picture it made last. */
function refreshWorkflows() {
  const on = isServerProvider();
  if (on && !S.workflows.tried) {
    S.workflows.tried = true;
    loadWorkflows();
    return;
  }
  renderWorkflows();
}

async function loadWorkflows() {
  S.workflows.loading = true;
  renderWorkflows();                    // the card says it is loading
  try {
    const r = await api(`${API}/workflows`);
    S.workflows.rows = r.workflows || [];
    S.workflows.error = '';
    teachProviderNames();
    // A name the image server never heard of is of no use to it.
    if (isServerProvider()) clearStaleModel(provider());
  } catch (e) {
    S.workflows.rows = [];
    S.workflows.error = (e && e.message) || String(e);
  } finally {
    S.workflows.loading = false;
    renderWorkflows();
    refreshForm();
  }
}

function renderWorkflows() {
  const rows = S.workflows.rows || [];
  // Another provider shows no workflows: the list is emptied AND its signature
  // forgotten, so coming back rebuilds the tiles (the count still says 34).
  // The note goes too: the card now belongs to that provider, and the image
  // server's error must not sit in another provider's chooser.
  if (!isServerProvider()) {
    R.wfList.replaceChildren();
    S.workflows.sig = '';
    R.pickerNote.hidden = true;
    R.pickerNote.textContent = '';
    return;
  }
  const sig = rows.map((w) => w.name).join('|');
  if (sig !== S.workflows.sig) {
    S.workflows.sig = sig;
    R.wfList.replaceChildren(...rows.map((w) => {
      const label = w.display_name || w.name;
      // No-Image Mode never asks for a picture: the name only.
      const media = S.nim
        ? el('div', { class: 'st-noimg', text: t('nim.hidden') })
        : (w.thumb
          ? el('img', { src: thumbUrl(w.thumb), alt: label, loading: 'lazy' })
          : el('div', { class: 'st-noimg', text: label }));
      const b = el('button', { type: 'button', class: 'st-wf-tile', title: label, 'aria-pressed': 'false' }, [
        media, el('span', { class: 'st-wf-name', text: label }),
        el('span', { class: 'st-wf-selected', text: t('picker.selected'), hidden: '' }),
      ]);
      b.dataset.name = w.name;
      b.addEventListener('click', () => { setModelValue(w.name); refreshForm(); });
      return el('li', { class: 'st-wf', dataset: { name: w.name } }, [b]);
    }));
    renderPickerList();
    scrollSelectedIntoView();
  }
  // The note is in the header, so it is read whether the card is open or closed.
  R.pickerNote.hidden = !(S.workflows.loading || S.workflows.error);
  R.pickerNote.textContent = S.workflows.loading ? t('loading') : (S.workflows.error || '');
}

// ---------------------------------------------------------------- Buzz balance

function renderBuzz() {
  const b = S.buzz;
  R.buzzText.textContent = b.state === 'loading' ? t('buzz.loading')
    : b.state === 'error' ? b.text
    : b.state === 'ok' ? b.text : '';
  R.buzzText.classList.toggle('st-error', b.state === 'error');
  R.buzzScope.hidden = !b.scope;
  R.buzzRefresh.disabled = b.state === 'loading';
  R.buzzLink.hidden = !(b.scope && b.helpUrl);
  if (b.scope && b.helpUrl) R.buzzLink.setAttribute('href', b.helpUrl);
  R.buzzSettings.hidden = !(b.scope && S.info && S.info.can_configure);
}

/** Ask the server for the Civitai Buzz balance. The Refresh button, the first time Civitai is
 *  picked and a successful Civitai picture are the only things that call this. */
async function loadBalance() {
  if (S.buzz.state === 'loading') return;
  S.buzzLoaded = true;
  S.buzz = { state: 'loading', text: '', scope: false, helpUrl: '' };
  renderBuzz();
  try {
    const r = await api(`${API}/civitai/balance`);
    if (r.scope_missing) {
      S.buzz = { state: 'scope', text: '', scope: true, helpUrl: String(r.help_url || '') };
    } else {
      const line = balanceLine(r.balances, (type) => (TYPE_LABELS[type] ? t(TYPE_LABELS[type]) : type), document.documentElement.lang);
      S.buzz = { state: 'ok', text: line ? t('buzz.line', { balances: line }) : t('buzz.empty'), scope: false, helpUrl: '' };
    }
  } catch (e) {
    const detail = e && e.body && typeof e.body.detail === 'string' ? e.body.detail : '';
    S.buzz = { state: 'error', text: detail || t('buzz.failed'), scope: false, helpUrl: '' };
  }
  renderBuzz();
}

// ---------------------------------------------------------------- model browser

function closeBrowse() {
  S.browse.open = false;
  if (R.browsePanel) R.browsePanel.hidden = true;
}

function renderBrowse() {
  const b = S.browse;
  R.browsePanel.hidden = !b.open;
  R.browseList.replaceChildren();
  if (!b.open) return;
  R.browseRefresh.disabled = b.state === 'loading';
  // The rows on hand are narrowed at once while typing; the server's filtered list follows.
  const words = browseWords(b.query);
  const rows = b.rows.filter((row) => browseMatches(row, words));
  R.browseMsg.classList.toggle('st-error', b.state === 'error');
  R.browseMsg.textContent = b.state === 'error' ? b.error
    : rows.length ? (b.state === 'loading' ? t('browse.loading') : '')
    : b.state === 'loading' ? t('browse.loading')
    : words.length ? t('browse.no_match', { query: words.join(' ') }) : t('browse.empty');
  R.browseMsg.hidden = !R.browseMsg.textContent;
  for (const row of rows) {
    const dl = row.downloads === 1 ? t('browse.downloads_one') : t('browse.downloads', { n: compactCount(row.downloads) });
    const btn = el('button', { type: 'button', class: 'st-browse-row', title: t('browse.pick', { name: row.name }) }, [
      el('span', { class: 'st-browse-name', text: browseLabel(row, dl) }),
      row.nsfw ? el('span', { class: 'st-badge', text: t('browse.adult') }) : null,
    ]);
    btn.addEventListener('click', () => pickModel(row));
    R.browseList.append(el('li', {}, [btn]));
  }
}

async function loadBrowse(fresh = false) {
  const b = S.browse;
  const sort = b.sort;
  const query = browseWords(b.query).join(' ');
  const current = () => sort === b.sort && query === browseWords(b.query).join(' ');
  b.state = 'loading'; b.error = '';
  renderBrowse();
  try {
    const q = query ? `&q=${encodeURIComponent(query)}` : '';
    const r = await api(`${API}/civitai/models?sort=${encodeURIComponent(sort)}${q}${fresh ? '&fresh=true' : ''}`);
    if (!current()) return;                      // the person picked another list or typed on meanwhile
    b.rows = cleanBrowseRows(r.models);
    b.state = 'ok';
  } catch (e) {
    if (!current()) return;
    const detail = e && e.body && typeof e.body.detail === 'string' ? e.body.detail : '';
    b.rows = []; b.state = 'error'; b.error = refused(e) ? t('locked') : (detail || t('browse.failed'));
  }
  renderBrowse();
}

function toggleBrowse() {
  const b = S.browse;
  b.open = !b.open;
  R.browseBtn.setAttribute('aria-expanded', b.open ? 'true' : 'false');
  if (b.open && b.state !== 'ok') loadBrowse(false);
  else renderBrowse();
}

/** Fill the Model box with a picked checkpoint and give it its name in the list and on the tiles. */
function pickModel(row) {
  const p = provider();
  if (p) {
    p.model_names = rememberModelName(p.model_names, row);
    p.models = [...new Set([...(p.models || []), row.air])];
  }
  refreshForm();                          // the chooser now offers the picked model
  setModelValue(row.air);                 // and it is the one in use
  refreshForm();
  closeBrowse();
  R.browseBtn.setAttribute('aria-expanded', 'false');
  focusModel();
}

// Civitai's own price for the form as it stands. Information only: it never blocks Generate.
let estTimer = null;
function scheduleEstimate() {
  const p = provider();
  if (!p || !p.estimate || !p.configured) { S.estKey = ''; S.estText = ''; clearTimeout(estTimer); return; }
  const form = formState();
  const key = estimateKey(form);
  if (key === S.estKey) return;
  S.estKey = key; S.estText = ''; R.est.hidden = true;
  clearTimeout(estTimer);
  if (!key) return;
  const seq = ++S.estSeq;
  estTimer = setTimeout(async () => {
    let text;
    try {
      const body = buildRequest(form, caps());
      const r = await api(`${API}/estimate`, { method: 'POST', body });
      text = t(r.n === 1 ? 'cost.estimate_one' : 'cost.estimate_other', { buzz: r.buzz, n: r.n });
    } catch (e) {
      // The server's own sentence when it gave one (a refused prompt, a model that is not a
      // checkpoint, Civitai's "not enough Buzz"): the same words Generate would answer with.
      const detail = e && e.body && typeof e.body.detail === 'string' ? e.body.detail : '';
      text = detail || t('cost.estimate_failed');
    }
    if (seq !== S.estSeq) return;
    S.estText = text; R.est.textContent = text; R.est.hidden = false;
  }, 600);
}

/** Ask the shell to open Settings on `target` ('section#anchor'). The shell checks
 *  the target and the session; a page outside the shell does nothing. */
function openShellSettings(target) {
  try {
    if (window.parent && window.parent !== window) {
      window.parent.postMessage({ type: 'dispatch:open-settings', target }, location.origin);
    }
  } catch { /* not framed */ }
}

function renderProviders() {
  const { providers, default: def } = S.info;
  R.provider.replaceChildren(...providers.map((p) => el('option', {
    value: p.id,
    text: p.configured ? p.label : t('provider.no_key', { name: p.label }),
    ...(p.configured ? {} : { disabled: '' }),
  })));
  if (def) R.provider.value = def;
  R.none.hidden = !!def;
  R.noneText.textContent = t(S.info.blocked ? 'provider.not_allowed' : 'provider.none');
  R.setup.hidden = !!S.info.blocked || !S.info.can_configure;
  R.ask.hidden = !!S.info.blocked || !!S.info.can_configure;
  R.form.hidden = !def;
}

function renderRatios() {
  // The name says the shape ("Square", "Portrait tall"); the ratio beside it tells
  // two ratios of one name apart.
  R.ratio.replaceChildren(...S.info.ratios.map((r) => el('option', { value: r, text: ratioText(r, t) })));
  R.ratio.value = '1:1';
}

function renderRecent() {
  const list = S.info.prompts || [];
  R.recent.replaceChildren();
  R.recentWrap.hidden = false;
  if (!list.length) {
    R.recent.append(el('li', { class: 'st-muted', text: t('prompt.recent_empty') }));
    return;
  }
  for (const p of list) {
    const b = el('button', { type: 'button', class: 'st-recent', title: p, text: p.length > 140 ? `${p.slice(0, 140)}…` : p });
    b.addEventListener('click', () => { R.prompt.value = p; refreshForm(); R.prompt.focus(); });
    R.recent.append(el('li', {}, [b]));
  }
}

function renderToday() {
  R.today.textContent = t('today.count', { n: S.info.today_count });
  R.today.title = (S.info.days || []).map((d) => `${d.day}: ${d.count}`).join('\n');
}

// ---------------------------------------------------------------- reference

function setReference(ref) {
  S.reference = ref;
  R.refPreview.replaceChildren();
  if (ref) {
    R.refPreview.append(S.nim ? el('span', { class: 'st-muted', text: t('nim.hidden') })
      : el('img', { src: ref.preview, alt: t('ref.label') }));
  }
  R.refClear.hidden = !ref;
}

function readFileAsDataUrl(file) {
  return new Promise((resolve, reject) => {
    const fr = new FileReader();
    fr.onload = () => resolve(String(fr.result));
    fr.onerror = () => reject(new Error('read'));
    fr.readAsDataURL(file);
  });
}

async function onUpload() {
  const f = R.file.files && R.file.files[0];
  R.file.value = '';
  if (!f) return;
  if (!/^image\//.test(f.type)) { setStatus(t('ref.not_image'), true); return; }
  if (f.size > 20 * 1024 * 1024) { setStatus(t('ref.too_big'), true); return; }
  try {
    const data = await readFileAsDataUrl(f);
    setReference({ ref: { kind: 'data_url', data }, preview: data });
  } catch { setStatus(t('ref.not_image'), true); }
}

async function openChatPicker() {
  R.picker.hidden = false;
  R.pickerGrid.replaceChildren(el('p', { class: 'st-muted', text: t('loading') }));
  try {
    const r = await api(`${API}/chat-images`);
    R.pickerGrid.replaceChildren();
    if (!r.images.length) { R.pickerGrid.append(el('p', { class: 'st-muted', text: t('ref.none_in_chat') })); return; }
    for (const im of r.images) {
      const b = el('button', { type: 'button', class: 'st-pick', title: im.thread || '' });
      if (S.nim) b.append(el('span', { class: 'st-muted', text: t('nim.hidden') }));
      else b.append(el('img', { src: im.url, alt: im.thread || t('ref.label'), loading: 'lazy' }));
      b.addEventListener('click', () => {
        setReference({ ref: { kind: 'media', url: im.url }, preview: im.url });
        R.picker.hidden = true;
      });
      R.pickerGrid.append(b);
    }
  } catch (e) { R.pickerGrid.replaceChildren(el('p', { class: 'st-error', text: e.message })); }
}

// ---------------------------------------------------------------- results

function tileMeta(r) {
  const bits = [modelLabel(r, (S.info && S.info.providers) || []), r.ratio];
  if (r.currency && PAY_LABELS[r.currency]) bits.push(t(PAY_LABELS[r.currency]));
  if (r.seed != null) bits.push(t('tile.seed', { n: r.seed }));
  if (r.reference) bits.push(t('tile.ref_used'));
  return bits.filter(Boolean).join(' · ');
}

function iconBtn(icon, label, onClick) {
  const b = el('button', { type: 'button', class: 'st-icon', title: label, 'aria-label': label }, [railIcon(icon)]);
  b.addEventListener('click', onClick);
  return b;
}

function renderGrid() {
  R.grid.replaceChildren();
  R.empty.hidden = S.images.length > 0 || S.pending.length > 0;
  if (R.deleteBtn) R.deleteBtn.hidden = !(S.info && S.info.can_clear) || S.images.length < 1;
  const jobs = S.pending.map(pendingTile);
  if (S.sort === 'newest') R.grid.append(...jobs.reverse());
  R.sortWrap.hidden = S.images.length < 2;
  shown().forEach((r, i) => {
    const media = S.nim
      ? el('div', { class: 'st-noimg', text: t('nim.hidden') })
      : el('img', { src: r.url, alt: r.prompt, loading: 'lazy' });
    const open = el('button', { type: 'button', class: 'st-open', title: t('tile.open'), 'aria-label': t('tile.open') }, [media]);
    open.addEventListener('click', () => openFull(i, r.id));
    const dl = el('a', { class: 'st-icon', href: r.url, download: '', title: t('tile.download'), 'aria-label': t('tile.download') }, [railIcon(RAIL_ICONS['viewer-download'])]);
    const actions = el('div', { class: 'st-actions' }, [
      dl,
      iconBtn(RAIL_ICONS.copy, t('tile.copy'), () => copyPrompt(r)),
      iconBtn(RAIL_ICONS.attach, t('tile.reference'), () => useAsReference(r)),
      iconBtn(RAIL_ICONS.send, t('tile.send'), () => openSend(r)),
      iconBtn(RAIL_ICONS.user, t('tile.avatar'), () => openAvatar(r)),
    ]);
    if (!caps().reference) actions.children[2].hidden = true;
    // Send posts through the inject route: only for a profile the server lets do that.
    if (S.info && S.info.can_send === false) actions.children[3].hidden = true;
    if (S.info && S.info.can_clear) {
      const clear = iconBtn(RAIL_ICONS.trash, t('tile.clear'), () => openClear(r));
      clear.classList.add('st-icon-danger');
      actions.append(clear);
    }
    R.grid.append(el('li', { class: 'st-tile', dataset: { id: r.id } }, [
      open, actions,
      el('div', { class: 'st-tilemeta' }, [
        el('div', { class: 'st-tileprompt', title: r.prompt, text: r.prompt }),
        el('div', { class: 'st-muted', text: tileMeta(r) }),
      ]),
    ]));
  });
  if (S.sort !== 'newest') R.grid.append(...jobs);
}

function running() { return S.pending.filter((j) => j.state === 'running').length; }

/** One placeholder per request: bouncing dots while it runs, the error + Retry/Dismiss after. */
function pendingTile(job) {
  const failed = job.state === 'failed';
  const snippet = job.body.prompt.length > 90 ? `${job.body.prompt.slice(0, 90)}…` : job.body.prompt;
  const face = failed
    ? el('div', { class: 'st-pendface st-error', role: 'alert', text: t('pending.failed', { error: job.error }) })
    : el('div', { class: 'st-pendface', role: 'status', 'aria-label': t('pending.running') }, [
      el('span', { class: 'st-dots', 'aria-hidden': 'true' }, [el('span'), el('span'), el('span')]),
      el('span', { class: 'st-muted', text: t('pending.running') }),
    ]);
  const kids = [face];
  if (failed) {
    const retry = el('button', { type: 'button', class: 'st-btn', text: t('pending.retry') });
    retry.addEventListener('click', () => {
      const max = S.info && S.info.max_in_flight;
      if (!canStartAnother(running(), max)) { setStatus(t('pending.cap', { n: max }), true); return; }
      startJob(job);
    });
    const drop = el('button', { type: 'button', class: 'st-btn', text: t('pending.dismiss') });
    drop.addEventListener('click', () => { S.pending = S.pending.filter((j) => j !== job); renderGrid(); refreshForm(); });
    kids.push(el('div', { class: 'st-pendactions' }, [retry, drop]));
  }
  kids.push(el('div', { class: 'st-tilemeta' }, [el('div', { class: 'st-tileprompt', title: job.body.prompt, text: snippet })]));
  return el('li', { class: `st-tile st-pending${failed ? ' st-failed' : ''}` }, kids);
}

/** Whether the shell is around: the page is framed by it. */
function isFramed() {
  try { return !!window.parent && window.parent !== window; } catch { return false; }
}

function openFull(index, id) {
  if (S.nim) { setStatus(t('nim.hidden')); return; }
  const items = galleryItems(shown(), { source: t('title'), optimizerNote: t('details.optimizer') });
  // Framed: the shell draws the viewer (it knows about zoom, pan and No-Image Mode).
  if (isFramed()) { lightbox(items, index); return; }
  // On its own: the shell would not hear the post, so this page opens the
  // picture itself.
  openViewer(items, index, id);
}

// ---------------------------------------------------------------- the viewer
//
// The shell's lightbox, in this page: for a page opened on its own (its own tab
// or window), the post to the shell would go nowhere. The dialog shows the
// picture, steps through the same set in the same order (oldest -> newest) and
// gives the focus back to the tile it came from.

function openViewer(items, index, id) {
  const at = Math.min(Math.max(0, Math.trunc(Number(index)) || 0), Math.max(0, items.length - 1));
  S.viewer = { items, index: at, id: id || '' };
  renderViewer();
  R.viewer.hidden = false;
  const close = R.viewer.querySelector('.st-viewer-close');
  if (close) close.focus();
}

function renderViewer() {
  const v = S.viewer;
  const item = v.items[v.index] || {};
  const many = v.items.length > 1;
  const img = el('img', { class: 'st-viewerimg', src: item.src || '', alt: item.caption || t('viewer.title') });
  const prev = el('button', { type: 'button', class: 'st-btn st-viewer-prev', text: t('viewer.prev') });
  const next = el('button', { type: 'button', class: 'st-btn st-viewer-next', text: t('viewer.next') });
  prev.addEventListener('click', () => stepViewer(-1));
  next.addEventListener('click', () => stepViewer(1));
  const close = el('button', { type: 'button', class: 'st-btn st-viewer-close', text: t('common.close') });
  close.addEventListener('click', closeViewer);
  const dl = el('a', { class: 'st-btn', href: item.downloadUrl || item.src || '#', download: '', text: t('tile.download') });
  prev.hidden = !many;
  next.hidden = !many;
  R.viewer.replaceChildren(el('div', { class: 'st-dlg st-viewerdlg', role: 'dialog', 'aria-modal': 'true', 'aria-label': t('viewer.title') }, [
    el('div', { class: 'st-viewerframe' }, [img]),
    item.caption ? el('p', { class: 'st-viewercap', text: item.caption }) : null,
    el('div', { class: 'st-dlg-actions' }, [prev, next, dl, close]),
  ]));
}

/** One picture older (-1) or newer (+1); the ends stop. */
function stepViewer(dir) {
  const v = S.viewer;
  if (!v) return;
  const at = stepIndex(v.index, dir, v.items.length);
  if (at === v.index) return;
  v.index = at;
  renderViewer();
  const b = R.viewer.querySelector(dir > 0 ? '.st-viewer-next' : '.st-viewer-prev');
  if (b) b.focus();
}

function closeViewer() {
  const id = S.viewer && S.viewer.id;
  R.viewer.hidden = true;
  R.viewer.replaceChildren();
  S.viewer = null;
  // The focus goes back to the tile the picture was opened from.
  const tile = id ? R.grid.querySelector(`.st-tile[data-id="${id}"] .st-open`) : null;
  if (tile) tile.focus();
}

async function copyPrompt(r) {
  try {
    await navigator.clipboard.writeText(r.prompt);
  } catch {
    const ta = el('textarea', { class: 'st-sr' });
    ta.value = r.prompt; document.body.append(ta); ta.select();
    try { document.execCommand('copy'); } catch { /* best effort */ }
    ta.remove();
  }
  setStatus(t('tile.copied'));
  toast(t('tile.copied'));
}

function useAsReference(r) {
  if (!caps().reference) return;
  setReference({ ref: { kind: 'result', id: r.id }, preview: r.url });
  setStatus(t('tile.reference_set'));
  R.refBox.scrollIntoView?.({ block: 'nearest' });
}

// ---------------------------------------------------------------- dialogs

function closeDialog() {
  R.dlg.hidden = true;
  R.dlg.replaceChildren();
}

function dialog(title, body, actions) {
  R.dlg.replaceChildren(el('div', { class: 'st-dlg', role: 'dialog', 'aria-modal': 'true', 'aria-label': title }, [
    el('h2', { text: title }), ...body, el('div', { class: 'st-dlg-actions' }, actions),
  ]));
  R.dlg.hidden = false;
  const first = R.dlg.querySelector('select, input, button');
  if (first) first.focus();
}

async function openSend(r) {
  let targets;
  try { targets = await api(`${API}/targets`); } catch (e) { setStatus(e.message, true); return; }
  if (!targets.threads.length) { setStatus(t('send.no_threads'), true); return; }
  const thread = el('select', { id: 'st-send-thread' }, targets.threads.map((th) => el('option', {
    value: th.id, text: `${th.bot} — ${th.title || t('send.untitled')}`,
  })));
  const asBot = el('input', { type: 'radio', name: 'st-as', value: 'assistant', checked: '' });
  const asSys = el('input', { type: 'radio', name: 'st-as', value: 'system' });
  const cap = el('input', { type: 'text', maxlength: '500', id: 'st-send-caption' });
  const go = el('button', { type: 'button', class: 'st-btn st-primary', text: t('send.go') });
  const cancel = el('button', { type: 'button', class: 'st-btn', text: t('common.cancel') });
  cancel.addEventListener('click', closeDialog);
  go.addEventListener('click', async () => {
    go.disabled = true;
    const th = targets.threads.find((x) => x.id === thread.value);
    try {
      await api('/api/inject', { method: 'POST', body: {
        thread_id: thread.value, role: asSys.checked ? 'system' : 'assistant',
        content: cap.value.trim(), media_url: r.url,
        // The picture's own record (the chat's Details panel reads it): what was typed.
        metadata: sendMetadata(r),
      } });
      closeDialog();
      setStatus(t('send.done', { title: (th && (th.title || th.bot)) || '' }));
      toast(t('send.done', { title: (th && (th.title || th.bot)) || '' }));
    } catch (e) { go.disabled = false; setStatus(t('send.failed', { error: e.message }), true); }
  });
  dialog(t('send.title'), [
    el('label', { for: 'st-send-thread', text: t('send.thread') }), thread,
    el('fieldset', { class: 'st-radios' }, [
      el('legend', { text: t('send.as') }),
      el('label', {}, [asBot, ` ${t('send.as_bot')}`]),
      el('label', {}, [asSys, ` ${t('send.as_system')}`]),
    ]),
    el('label', { for: 'st-send-caption', text: t('send.caption') }), cap,
  ], [cancel, go]);
}

function loadImage(url) {
  return new Promise((resolve, reject) => {
    const im = new Image();
    im.onload = () => resolve(im);
    im.onerror = () => reject(new Error('image'));
    im.src = url;
  });
}

async function openAvatar(r) {
  let targets;
  try { targets = await api(`${API}/targets`); } catch (e) { setStatus(e.message, true); return; }
  if (S.nim) { setStatus(t('nim.hidden')); return; }
  if (!targets.bots.length) { setStatus(t('avatar.no_bots'), true); return; }
  let im;
  try { im = await loadImage(r.url); } catch { setStatus(t('avatar.failed', { error: 'image' }), true); return; }
  const bot = el('select', { id: 'st-av-bot' }, targets.bots.map((b) => el('option', { value: b.id, text: b.name || b.id })));
  const pos = el('input', { type: 'range', min: '0', max: '100', value: String(Math.round(defaultCropPos(im.naturalWidth, im.naturalHeight) * 100)), id: 'st-av-pos' });
  const canvas = el('canvas', { width: '160', height: '160', class: 'st-av-prev', 'aria-label': t('avatar.preview') });
  const draw = (c, px) => {
    const rc = cropRect(im.naturalWidth, im.naturalHeight, Number(pos.value) / 100);
    c.width = px; c.height = px;
    c.getContext('2d').drawImage(im, rc.sx, rc.sy, rc.size, rc.size, 0, 0, px, px);
  };
  const redraw = () => draw(canvas, 160);
  pos.addEventListener('input', redraw);
  redraw();
  const go = el('button', { type: 'button', class: 'st-btn st-primary', text: t('avatar.go') });
  const cancel = el('button', { type: 'button', class: 'st-btn', text: t('common.cancel') });
  cancel.addEventListener('click', closeDialog);
  go.addEventListener('click', async () => {
    go.disabled = true;
    try {
      const full = await (await fetch(r.url, { credentials: 'same-origin' })).blob();
      const face = el('canvas');
      draw(face, Math.min(512, Math.min(im.naturalWidth, im.naturalHeight)));
      const faceBlob = await new Promise((res) => face.toBlob(res, 'image/png'));
      const fd = new FormData();
      fd.append('file', full, `studio-${r.id.slice(0, 8)}.${/\.jpe?g$/i.test(r.url) ? 'jpg' : 'png'}`);
      const pick = targets.bots.find((b) => b.id === bot.value) || {};
      let url;
      if (pick.personal) {
        // The caller's own bot: its route takes the square as fractions and crops itself.
        const rc = cropRect(im.naturalWidth, im.naturalHeight, Number(pos.value) / 100);
        const q = new URLSearchParams({ crop_x: rc.sx / im.naturalWidth, crop_y: rc.sy / im.naturalHeight,
          crop_size: rc.size / Math.min(im.naturalWidth, im.naturalHeight) });
        url = `/api/me/characters/${encodeURIComponent(bot.value)}/avatar?${q}`;
      } else {
        fd.append('face', faceBlob, `studio-${r.id.slice(0, 8)}-face.png`);
        url = `/api/bots/${encodeURIComponent(bot.value)}/avatar`;
      }
      const res = await fetch(url, { method: 'POST', body: fd, credentials: 'same-origin' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const name = (targets.bots.find((b) => b.id === bot.value) || {}).name || bot.value;
      closeDialog();
      setStatus(t('avatar.done', { name }));
      toast(t('avatar.done', { name }));
    } catch (e) { go.disabled = false; setStatus(t('avatar.failed', { error: e.message }), true); }
  });
  dialog(t('avatar.title'), [
    el('label', { for: 'st-av-bot', text: t('avatar.bot') }), bot,
    el('div', { class: 'st-av-row' }, [canvas, el('div', {}, [
      el('label', { for: 'st-av-pos', text: t('avatar.crop') }), pos,
      el('p', { class: 'st-muted', text: t('avatar.note') }),
    ])]),
  ], [cancel, go]);
}

// ---------------------------------------------------------------- clear from here down
//
// The File Server's "Delete this & older" for the studio: this picture and every
// picture after it in the grid's current order. The server counts first (dry run,
// so the number covers pictures beyond the grid's last 60 too), the person
// confirms in a danger dialog, then the same request runs for real.

async function openClear(r) {
  let plan;
  try {
    plan = await api(`${API}/clear`, { method: 'POST', body: { id: r.id, order: S.sort, dry_run: true } });
  } catch (e) { setStatus(t('clear.failed', { error: e.message }), true); return; }
  const go = el('button', { type: 'button', class: 'st-btn st-danger', text: t('clear.go') });
  const cancel = el('button', { type: 'button', class: 'st-btn', text: t('common.cancel') });
  cancel.addEventListener('click', closeDialog);
  go.addEventListener('click', async () => {
    go.disabled = true;
    try {
      const res = await api(`${API}/clear`, { method: 'POST', body: { id: r.id, order: S.sort, expect: plan.count } });
      closeDialog();
      S.images = (await api(`${API}/images`)).images;
      await reloadState(false);
      renderGrid();
      setStatus(t('clear.done', { n: res.deleted }));
      toast(t('clear.done', { n: res.deleted }));
    } catch (e) {
      // A picture came or went since the count (another tab): nothing was
      // deleted; count again and ask again rather than delete what was not seen.
      // The route's other 409 (a generation is running) is not a recount: it
      // falls through to the server's own message below.
      const code = e && e.body && e.body.error && e.body.error.code;
      if (code === 'imagestudio_pictures_changed') {
        closeDialog();
        setStatus(t('clear.changed'));
        try {
          S.images = (await api(`${API}/images`)).images;
        } catch (e2) { setStatus(t('clear.failed', { error: e2.message }), true); return; }
        renderGrid();
        openClear(r);
        return;
      }
      go.disabled = false; setStatus(t('clear.failed', { error: e.message }), true);
    }
  });
  const body = [el('p', { class: 'st-clear-msg', text: t(S.sort === 'newest' ? 'clear.confirm_newest' : 'clear.confirm_oldest', { n: plan.count }) })];
  if (plan.in_chat) body.push(el('p', { class: 'st-muted', text: t('clear.in_chat', { n: plan.in_chat }) }));
  dialog(t('clear.title'), body, [cancel, go]);
  cancel.focus();
}

// ---------------------------------------------------------------- delete by age
//
// Delete (All / Past hour / Past day): the page names a scope, never a time. The server
// counts the signed-in profile's own pictures in that window on ITS clock (dry run),
// the dialog asks "Delete N pictures from the past hour?", and the confirmed count rides
// along so a picture that arrived meanwhile triggers a re-count instead of a surprise.
// Files a chat still shows stay (the same /clear path as "Clear from here down").

async function openDelete(scopeIn = 'hour') {
  let scope = normalizeScope(scopeIn);
  let plan = null;
  const sel = el('select', { id: 'st-del-scope', 'aria-label': t('delete.scope') },
    DELETE_SCOPES.map((v) => el('option', { value: v, text: t(DELETE_SCOPE_LABELS[v]) })));
  sel.value = scope;
  const msg = el('p', { class: 'st-clear-msg', role: 'status', text: '…' });
  const note = el('p', { class: 'st-muted', hidden: '' });
  const go = el('button', { type: 'button', class: 'st-btn st-danger', text: t('clear.go'), disabled: '' });
  const cancel = el('button', { type: 'button', class: 'st-btn', text: t('common.cancel') });
  cancel.addEventListener('click', closeDialog);
  async function count() {
    go.disabled = true; plan = null; msg.textContent = '…';
    const mine = scope;
    try {
      const r = await api(`${API}/clear`, { method: 'POST', body: { since: mine, dry_run: true } });
      if (mine !== scope) return;      // the person moved on to another scope
      plan = r;
      note.hidden = !r.in_chat;
      note.textContent = r.in_chat ? t('clear.in_chat', { n: r.in_chat }) : '';
      msg.textContent = r.count ? t(deleteConfirmKey(scope, r.count), { n: r.count }) : t('delete.none');
      go.disabled = !r.count;
    } catch (e) { msg.textContent = t('clear.failed', { error: e.message }); }
  }
  sel.addEventListener('change', () => { scope = normalizeScope(sel.value); count(); });
  go.addEventListener('click', async () => {
    if (!plan) return;
    go.disabled = true;
    try {
      const res = await api(`${API}/clear`, { method: 'POST', body: { since: scope, expect: plan.count } });
      closeDialog();
      S.images = (await api(`${API}/images`)).images;
      await reloadState(false);
      renderGrid();
      setStatus(t('clear.done', { n: res.deleted }));
      toast(t('clear.done', { n: res.deleted }));
    } catch (e) {
      const code = e && e.body && e.body.error && e.body.error.code;
      if (code === 'imagestudio_pictures_changed') {
        setStatus(t('clear.changed'));
        try { S.images = (await api(`${API}/images`)).images; renderGrid(); } catch { /* the count below reports it */ }
        await count();
        return;
      }
      go.disabled = false; setStatus(t('clear.failed', { error: e.message }), true);
    }
  });
  dialog(t('delete.title'), [el('label', { for: 'st-del-scope', text: t('delete.scope') }), sel, msg, note], [cancel, go]);
  cancel.focus();
  await count();
}

// ---------------------------------------------------------------- generate

function generate() {
  const p = provider();
  if (!p || !canGenerate({ prompt: R.prompt.value, busy: false, provider: p.configured ? p.id : '' })) return;
  const c = caps();
  const max = S.info && S.info.max_in_flight;
  const asked = clampCount(R.count.value, c.max_n);
  if (!batches(c)) {
    // The provider makes one picture per request (the image server has no batch
    // size): one click is one job per picture, each with its own tile. The jobs
    // stop at the free in-flight slots, and the count that did not fit is said.
    const free = freeSlots(running(), max);
    const started = Math.min(asked, free);
    if (!started) {
      setStatus(t('pending.cap', { n: max }), true);
      return;
    }
    for (let i = 0; i < started; i++) {
      startJob({ key: ++S.jobSeq, body: { ...buildRequest(formState(), c), n: 1 } });
    }
    // What the count became: the jobs that started, and the ones that did not fit.
    if (started < asked) {
      setStatus(t('results.fanout_short', { started, asked, skipped: asked - started, max }));
    } else if (started > 1) {
      setStatus(t('results.fanout', { n: started }));
    }
    return;
  }
  if (!canStartAnother(running(), max)) {
    setStatus(t('pending.cap', { n: max }), true);
    return;
  }
  startJob({ key: ++S.jobSeq, body: buildRequest(formState(), c) });
}

/** Whether the provider takes the whole count in one request. A provider that
 *  says nothing about it is taken at its word: one request, as it always was. */
function batches(c) {
  return !c || c.batch !== false;
}

/** How many more jobs may start: the in-flight slots still free. */
function freeSlots(running, max) {
  const cap = Number(max);
  if (!Number.isFinite(cap) || cap <= 0) return Infinity;
  return Math.max(0, cap - running);
}

/** Send one job (new or a retry). Several run at once; each lands in its own tile. */
async function startJob(job) {
  job.state = 'running'; job.error = '';
  if (!S.pending.includes(job)) S.pending.push(job);
  renderGrid(); refreshForm();
  setStatus(t('generating_note'));
  try {
    const r = await api(`${API}/generate`, { method: 'POST', body: job.body });
    S.pending = S.pending.filter((j) => j !== job);
    S.images = S.images.concat(r.images);
    if (isCivitai()) loadBalance();          // the picture cost Buzz: show what is left
    await reloadState(false);
    renderGrid();
    setStatus(r.images.length < r.asked
      ? t('results.partial', { got: r.images.length, asked: r.asked })
      : t('results.done', { n: r.images.length }));
  } catch (e) {
    job.state = 'failed';
    job.error = refused(e) ? t('locked') : e.message;
    renderGrid();
    // The reason is on the job's own tile (role=alert); saying it again here showed it twice.
    setStatus('');
  } finally {
    refreshForm();
  }
}

async function reloadState(rebuildProviders = true) {
  S.info = await api(`${API}/state`);
  if (rebuildProviders) renderProviders();
  renderToday(); renderRecent(); refreshForm(); renderInflight();
}

// ---------------------------------------------------------------- build

function field(label, control, extra) {
  return el('div', { class: 'st-field' }, [el('label', { text: label }), control, extra].filter(Boolean));
}

function build(root) {
  R.prompt = el('textarea', { id: 'st-prompt', rows: '6', placeholder: t('prompt.placeholder'), 'aria-label': t('prompt.label') });
  R.counter = el('div', { class: 'st-counter', 'aria-live': 'polite' });
  R.recent = el('ul', { class: 'st-recents' });
  R.recentWrap = el('details', { class: 'st-recentwrap' }, [el('summary', { text: t('prompt.recent') }), R.recent]);
  R.provider = el('select', { id: 'st-provider' });
  // The model control: a dropdown of the provider's names, or a text box for an
  // id no list carries. Only one of the two is on the page at a time.
  R.modelSelect = el('select', { id: 'st-model-select', 'aria-label': t('model.label'), hidden: '' });
  R.model = el('input', { type: 'text', id: 'st-model', autocomplete: 'off', spellcheck: 'false' });
  R.modelBox = el('div', { class: 'st-modelbox' }, [R.modelSelect, R.model]);
  R.modelNote = el('p', { class: 'st-muted st-modelnote', hidden: '' });
  R.modelRefresh = el('button', { type: 'button', class: 'st-btn st-browse-btn', id: 'st-model-refresh', text: t('models.refresh'), hidden: '' });
  R.browseBtn = el('button', { type: 'button', class: 'st-btn st-browse-btn', id: 'st-browse', text: t('browse.button'), hidden: '', 'aria-expanded': 'false', 'aria-controls': 'st-browse-panel' });
  R.pay = el('select', { id: 'st-pay' }, ['', ...CURRENCIES].map((v) => el('option', { value: v, text: t(PAY_LABELS[v]) })));
  R.pay.value = S.pay;
  R.payRow = el('div', { class: 'st-field', hidden: '' }, [el('label', { for: 'st-pay', text: t('pay.label') }), R.pay]);
  R.buzzText = el('span', { class: 'st-buzz-text' });
  R.buzzRefresh = el('button', { type: 'button', class: 'st-btn st-small', id: 'st-buzz-refresh', text: t('buzz.refresh') });
  R.buzzLink = el('a', { class: 'st-link', href: 'https://civitai.com/user/account', target: '_blank', rel: 'noopener noreferrer', text: t('buzz.open_civitai'), hidden: '' });
  R.buzzSettings = el('button', { type: 'button', class: 'st-link st-linkbtn', text: t('buzz.open_settings'), hidden: '' });
  R.buzzSettings.addEventListener('click', () => openShellSettings('connections#images'));
  R.buzzScope = el('p', { class: 'st-muted st-buzz-scope', text: t('buzz.scope'), hidden: '' });
  R.buzzRow = el('div', { class: 'st-buzz', role: 'status', hidden: '' }, [
    el('div', { class: 'st-buzz-line' }, [R.buzzText, R.buzzRefresh]),
    R.buzzScope, el('div', { class: 'st-buzz-links' }, [R.buzzLink, R.buzzSettings]),
  ]);
  R.browseSort = el('select', { id: 'st-browse-sort', 'aria-label': t('browse.sort') }, BROWSE_SORTS.map((v) => el('option', { value: v, text: t(BROWSE_LABELS[v]) })));
  R.browseFilter = el('input', { type: 'search', id: 'st-browse-filter', class: 'st-browse-filter', maxlength: '60', autocomplete: 'off', spellcheck: 'false', placeholder: t('browse.filter_hint'), 'aria-label': t('browse.filter') });
  R.browseRefresh = el('button', { type: 'button', class: 'st-btn st-small', text: t('browse.refresh') });
  const browseClose = el('button', { type: 'button', class: 'st-btn st-small', text: t('common.close') });
  R.browseMsg = el('p', { class: 'st-muted', role: 'status', hidden: '' });
  R.browseList = el('ul', { class: 'st-browse-list' });
  R.browsePanel = el('div', { class: 'st-picker st-browse', id: 'st-browse-panel', hidden: '' }, [
    el('strong', { text: t('browse.title') }), el('div', { class: 'st-muted', text: t('browse.note') }),
    el('div', { class: 'st-row' }, [R.browseSort, R.browseRefresh, browseClose]),
    R.browseFilter, R.browseMsg, R.browseList,
  ]);
  R.ratio = el('select', { id: 'st-ratio' });
  R.count = el('input', { type: 'number', id: 'st-count', min: '1', max: '9', value: '1', inputmode: 'numeric' });
  // The count only shows when the provider can make more than one picture.
  R.countField = field(t('count.label'), R.count);
  R.two = el('div', { class: 'st-two' }, [field(t('ratio.label'), R.ratio), R.countField]);
  R.optimizer = el('input', { type: 'checkbox', id: 'st-optimizer' });
  R.optimizerRow = el('div', { class: 'st-check' }, [R.optimizer, el('label', { for: 'st-optimizer', text: t('optimizer.label') }), el('div', { class: 'st-muted', text: t('optimizer.hint') })]);
  R.seed = el('input', { type: 'text', id: 'st-seed', inputmode: 'numeric', placeholder: t('seed.placeholder'), autocomplete: 'off' });
  R.seedRow = field(t('seed.label'), R.seed);
  R.file = el('input', { type: 'file', accept: 'image/*', class: 'st-sr', id: 'st-file', tabindex: '-1' });
  R.refPreview = el('div', { class: 'st-refprev' });
  R.refClear = el('button', { type: 'button', class: 'st-btn', text: t('ref.remove'), hidden: '' });
  const refUp = el('button', { type: 'button', class: 'st-btn', text: t('ref.upload') });
  const refChat = el('button', { type: 'button', class: 'st-btn', text: t('ref.from_chat') });
  R.pickerGrid = el('div', { class: 'st-pickgrid' });
  const pickerClose = el('button', { type: 'button', class: 'st-btn', text: t('common.close') });
  R.picker = el('div', { class: 'st-picker', hidden: '' }, [el('strong', { text: t('ref.pick_title') }), R.pickerGrid, pickerClose]);
  R.refBox = el('div', { class: 'st-field st-ref' }, [
    el('label', { text: t('ref.label') }), el('div', { class: 'st-muted', text: t('ref.hint') }),
    R.refPreview, el('div', { class: 'st-row' }, [refUp, refChat, R.refClear]), R.file, R.picker,
  ]);
  R.wfList = el('ul', { class: 'st-workflows', id: 'st-workflows', hidden: '' });
  R.pickerNote = el('p', { class: 'st-muted st-workflows-note', hidden: '' });
  R.pickerSearch = el('input', { type: 'search', id: 'st-picker-search', class: 'st-picker-search', maxlength: '60', autocomplete: 'off', spellcheck: 'false', placeholder: t('picker.search_workflows'), 'aria-label': t('picker.search_workflows') });
  R.pickerCount = el('p', { class: 'st-muted st-picker-count', role: 'status', 'aria-live': 'polite' });
  R.pickerEmpty = el('p', { class: 'st-muted st-picker-empty', role: 'status', hidden: '' });
  R.pickerTitle = el('h3', { class: 'st-pickertitle', id: 'st-picker-title', text: t('picker.workflows_title') });
  // The chosen workflow, in the header: a closed card still says what it holds.
  R.pickerChosen = el('span', { class: 'st-picker-chosen', id: 'st-picker-chosen', hidden: '' });
  // The small arrow that opens and closes the gallery.
  R.pickerChevron = el('button', { type: 'button', class: 'st-btn st-small st-picker-chevron', id: 'st-picker-chevron', 'aria-label': t('picker.expand'), 'aria-expanded': 'false', 'aria-controls': 'st-picker-body', hidden: '' }, [
    el('span', { class: 'st-chevron', 'aria-hidden': 'true', text: '▸' }),
  ]);
  R.pickerToggle = el('button', { type: 'button', class: 'st-btn st-small st-picker-toggle', id: 'st-picker-toggle', text: t('picker.choose_model'), hidden: '', 'aria-expanded': 'false', 'aria-controls': 'st-picker-body' });
  R.modelList = el('ul', { class: 'st-picker-models', id: 'st-picker-models', hidden: '' });
  R.pickerBody = el('div', { class: 'st-pickerbody', id: 'st-picker-body' }, [
    R.pickerSearch, R.pickerEmpty, R.wfList, R.modelList,
  ]);
  R.pickerCard = el('div', { class: 'st-picker st-pickercard', id: 'st-picker', hidden: '' }, [
    // The note lives in the HEADER: a closed card must not hide the reason it
    // has nothing to show (the image server is loading, or it said no).
    el('div', { class: 'st-pickerhead' }, [R.pickerTitle, R.pickerCount, R.pickerChosen, R.pickerChevron, R.pickerToggle, R.pickerNote]),
    R.pickerBody,
  ]);
  R.go = el('button', { type: 'button', class: 'st-btn st-primary st-go', id: 'st-generate', text: t('generate') });
  // Generate rides in a bar that sticks to the bottom of the form column, so
  // it is always in reach while the form scrolls.
  R.reset = el('button', { type: 'button', class: 'st-btn st-reset', id: 'st-reset', text: t('reset'), title: t('reset.tip') });
  R.reset.addEventListener('click', resetForm);
  R.inflight = el('p', { class: 'st-muted st-inflight', role: 'status', 'aria-live': 'polite', hidden: '' });
  R.goBar = el('div', { class: 'st-gobar' }, [R.go, R.reset]);
  R.cost = el('p', { class: 'st-cost', hidden: '' });
  R.h3Hint = el('p', { class: 'st-muted st-h3hint', hidden: '', text: t('models.h3_hint') });
  R.est = el('p', { class: 'st-cost st-estimate', role: 'status', hidden: '' });
  R.status = el('p', { class: 'st-status', role: 'status', 'aria-live': 'polite' });
  R.today = el('span', { class: 'st-today' });
  // No provider yet: say so, and (for a profile that may change it) open the
  // shell's Settings -> AI & Connections -> Image generation from here (3.2).
  R.setup = el('button', { type: 'button', class: 'st-btn st-primary', hidden: '', text: t('provider.setup') });
  R.setup.addEventListener('click', () => openShellSettings('connections#images'));
  R.ask = el('p', { class: 'st-muted', hidden: '', text: t('provider.ask_owner') });
  R.noneText = el('p', { text: t('provider.none') });
  R.none = el('div', { class: 'st-none', hidden: '' }, [
    R.noneText, R.setup, R.ask,
  ]);
  // The controls column: it scrolls itself, and at its end the wheel goes to
  // the page, so the results keep moving.
  R.controls = el('section', { class: 'st-controls' }, []);
  R.form = el('div', { class: 'st-form' }, [
    field(t('prompt.label'), R.prompt, R.counter), R.recentWrap,
    // The picker card sits under the Model field and spans the row: it is the
    // image server's gallery, or a compact chooser for a long model list.
    el('div', { class: 'st-two st-modelrow' }, [field(t('provider.label'), R.provider),
      field(t('model.label'), R.modelBox),
      R.pickerCard]),
    R.modelRefresh, R.modelNote,
    R.browseBtn, R.browsePanel,
    R.two,
    R.payRow,
    // The Generate bar is the LAST thing in the form: nothing scrolls under it.
    R.optimizerRow, R.seedRow, R.refBox, R.h3Hint, R.cost, R.est, R.buzzRow, R.inflight, R.goBar,
  ]);
  R.controls.append(R.none, R.form, R.status);
  R.sort = el('select', { id: 'st-sort', class: 'st-sort' }, SORTS.map((v) => el('option', { value: v, text: t(SORT_LABELS[v]) })));
  R.sort.value = S.sort;
  R.sortWrap = el('label', { class: 'st-sortwrap', for: 'st-sort' }, [`${t('sort.label')}: `, R.sort]);
  R.deleteBtn = el('button', { type: 'button', id: 'st-delete', class: 'st-btn st-danger-outline', text: t('delete.button'), hidden: '' });
  R.deleteBtn.addEventListener('click', () => openDelete());
  R.grid = el('ul', { class: 'st-grid' });
  R.empty = el('p', { class: 'st-muted st-emptynote', text: t('results.empty') });
  R.dlg = el('div', { class: 'st-dlgwrap', hidden: '' });
  // The viewer of this page (only when the shell is not around): closed and
  // hidden, so it never sits on top of the page.
  R.viewer = el('div', { class: 'st-dlgwrap st-viewer', hidden: '' });
  root.append(
    el('header', { class: 'st-head' }, [R.today]),
    el('div', { class: 'st-layout' }, [
      R.controls,
      el('section', { class: 'st-results' }, [
        el('div', { class: 'st-resultshead' }, [el('h2', { text: t('results.title') }), el('div', { class: 'st-headtools' }, [R.sortWrap, R.deleteBtn])]),
        R.empty, R.grid]),
    ]),
    R.dlg, R.viewer,
  );

  // The wheel over the controls column: while the column can still move that
  // way, it scrolls itself; at its end the page scrolls, so the results move
  // instead of the page standing still.
  //
  // A wheel over a list INSIDE the column (the workflow gallery, the model
  // list, the picker grid, the browse list, the recent prompts) belongs to
  // that list while the list can still move that way: the column must not
  // swallow it, or the list would only scroll by its scrollbar.
  R.controls.addEventListener('wheel', (ev) => {
    const inner = ev.target && ev.target.closest ? ev.target.closest(INNER_SCROLLPORTS) : null;
    if (inner && R.controls.contains(inner) && canScroll(ev.deltaY, inner)) return;
    if (!wheelShouldChain(ev.deltaY, R.controls)) return;
    ev.preventDefault();
    const se = document.scrollingElement || document.documentElement;
    se.scrollTop = (se.scrollTop || 0) + ev.deltaY;
  }, { passive: false });
  R.prompt.addEventListener('input', refreshForm);
  // The form is saved on every change (debounced) and at once when the page is left.
  const userEdit = () => { S.draftDirty = true; scheduleDraftSave(); };
  R.form.addEventListener('input', userEdit);
  R.form.addEventListener('change', userEdit);
  // A workflow tile, a listed model or a recent prompt is picked by a click, not an input.
  R.form.addEventListener('click', (ev) => {
    const tgt = ev.target;
    if (tgt && tgt.closest && (tgt.closest('[data-name]') || R.modelList.contains(tgt) || R.recentWrap.contains(tgt))) userEdit();
  });
  window.addEventListener('pagehide', saveDraftNow);
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) { saveDraftNow(); stopPolling(); S.poll.since = 0; }
    else if (S.info && inflightExtra(S.info, running())) startPolling();
  });
  // Shift/Ctrl/Cmd+Enter = the Generate button, and only while it is enabled.
  R.prompt.addEventListener('keydown', (ev) => {
    if (!isGenerateShortcut(ev)) return;
    ev.preventDefault();
    // One keypress = one job: a bounce right after the last start is ignored.
    const now = Date.now();
    if (now - S.lastKey < SHORTCUT_DEBOUNCE_MS) return;
    if (R.go.disabled) return;
    S.lastKey = now;
    generate();
  });
  R.provider.addEventListener('change', () => {
    // A new provider means a new list: the old filter must not narrow it (and
    // the count must go back to the whole list), and the Model box must not
    // keep a name the new provider never heard of.
    S.picker.query = '';
    R.pickerSearch.value = '';
    clearStaleModel(provider());
    refreshForm();
  });
  R.count.addEventListener('change', () => { R.count.value = String(clampCount(R.count.value, caps().max_n)); scheduleEstimate(); });
  // A name typed by hand is kept, and the tile for it (if there is one) is marked.
  // The header follows too: a closed card must not name a workflow the form
  // will not use.
  R.model.addEventListener('input', () => { refreshModelBits(); scheduleEstimate(); markSelected(); renderChosen(); });
  R.modelSelect.addEventListener('change', () => { refreshForm(); });
  R.modelRefresh.addEventListener('click', refreshModels);
  R.pay.addEventListener('change', () => { S.pay = normalizeCurrency(R.pay.value); savePay(S.pay); scheduleEstimate(); });
  R.buzzRefresh.addEventListener('click', () => loadBalance());
  R.browseBtn.addEventListener('click', toggleBrowse);
  R.browseSort.addEventListener('change', () => { S.browse.sort = normalizeBrowseSort(R.browseSort.value); S.browse.rows = []; loadBrowse(false); });
  R.browseRefresh.addEventListener('click', () => loadBrowse(true));
  let browseTimer = 0;
  R.browseFilter.addEventListener('input', () => {
    const before = browseWords(S.browse.query).join(' ');
    S.browse.query = R.browseFilter.value;
    renderBrowse();                              // narrow what is on hand at once
    clearTimeout(browseTimer);
    if (browseWords(S.browse.query).join(' ') === before) return;
    browseTimer = setTimeout(() => loadBrowse(false), 450);   // then ask Civitai for the rest
  });
  browseClose.addEventListener('click', () => { closeBrowse(); R.browseBtn.setAttribute('aria-expanded', 'false'); R.browseBtn.focus(); });
  R.ratio.addEventListener('change', scheduleEstimate);
  R.go.addEventListener('click', (ev) => {
    // The second click of a double-click is not a second job (every Generate is metered).
    if (ev && ev.detail > 1) return;
    generate();
  });
  R.sort.addEventListener('change', () => {
    S.sort = normalizeSort(R.sort.value);
    saveSort(S.sort);
    renderGrid();
  });
  refUp.addEventListener('click', () => R.file.click());
  R.file.addEventListener('change', onUpload);
  refChat.addEventListener('click', openChatPicker);
  pickerClose.addEventListener('click', () => { R.picker.hidden = true; });
  R.refClear.addEventListener('click', () => setReference(null));
  R.dlg.addEventListener('click', (e) => { if (e.target === R.dlg) closeDialog(); });
  R.viewer.addEventListener('click', (e) => { if (e.target === R.viewer) closeViewer(); });
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    if (!R.viewer.hidden) { closeViewer(); return; }
    if (!R.dlg.hidden) closeDialog();
  });
  R.browsePanel.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && S.browse.open) { closeBrowse(); R.browseBtn.setAttribute('aria-expanded', 'false'); R.browseBtn.focus(); }
  });
  // The picker card: type to narrow the list, arrows to walk it, Escape to
  // clear the filter.
  R.pickerSearch.addEventListener('input', () => {
    S.picker.query = R.pickerSearch.value;
    renderPickerList();
  });
  R.pickerToggle.addEventListener('click', () => setPickerOpen(!S.picker.open));
  // The small arrow opens and closes the gallery.
  R.pickerChevron.addEventListener('click', () => setPickerOpen(!S.picker.open));
  R.pickerCard.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      if (!S.picker.query) return;
      R.pickerSearch.value = '';
      S.picker.query = '';
      renderPickerList();
      e.stopPropagation();
      return;
    }
    if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
    if (R.pickerBody.hidden) return;      // the card is closed: there is nothing to walk
    const items = pickerItems();
    if (!items.length) return;
    const doc = R.pickerCard.ownerDocument;
    const at = items.indexOf(doc.activeElement);
    const next = e.key === 'ArrowDown'
      ? (at < 0 ? 0 : Math.min(at + 1, items.length - 1))
      : (at < 0 ? items.length - 1 : Math.max(at - 1, 0));
    e.preventDefault();
    items[next].focus();
  });
}

export async function mount(root = document.getElementById('st-root')) {
  theme();
  await ready();
  S.nim = nimOn();
  S.sort = loadSort();
  S.pay = loadPay();
  document.title = t('title');
  root.setAttribute('aria-label', t('title'));
  document.documentElement.lang = document.documentElement.lang || 'en';
  // A picture the image-retention setting removed answers 410 + X-Image-Expired: the tile
  // shows the "image expired" placeholder instead of a broken image (the shell's listener
  // does not reach into this frame).
  installExpiredImages({ t: () => t('expired'), el, icon: () => railIcon(RAIL_ICONS.nim) });
  build(root);
  try {
    await reloadState();
    renderRatios();
    S.images = (await api(`${API}/images`)).images;
    renderGrid();
    refreshForm();
    // /state answered, so this profile may use Studio and we know its key: only now
    // is the draft read (never before, never on a 403).
    S.profileKey = (S.info && S.info.profile_key) || '';
    S.baseline = serializeDraft(draftForm(), S.info.prompt_max);
    const draft = S.info.default ? loadDraft() : null;
    if (draft) applyDraft(draft);
    S.draftReady = !!draftKey(DRAFT_PREFIX, S.profileKey);
    if (inflightExtra(S.info, running())) startPolling();
  } catch (e) {
    root.replaceChildren(el('p', { class: 'st-locked', text: refused(e) ? t('locked') : e.message }));
  }
  document.body.classList.add('st-ready');
}

if (typeof document !== 'undefined' && document.getElementById('st-root') && !globalThis.__ST_NO_AUTOMOUNT) {
  mount();
}
