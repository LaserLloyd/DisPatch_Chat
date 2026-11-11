// Image Studio — the pure half of the page (no DOM, no fetch), so the rules
// that matter are one `node --test` away: the generate shortcut, the prompt
// counter, how many pictures a provider allows, the avatar crop and what a
// request body looks like.

export const PROMPT_MAX = 1500;

/** Shift+Enter, Ctrl+Enter or Cmd+Enter starts a generation; plain Enter stays a newline.
 *  Never while an IME composition is in flight (Enter confirms a candidate), and a held
 *  key (auto-repeat) is one press, not many. */
export function isGenerateShortcut(ev) {
  if (!ev || ev.key !== 'Enter' || ev.isComposing || ev.repeat) return false;
  return !!(ev.ctrlKey || ev.metaKey || ev.shiftKey) && !ev.altKey;
}

/** A second shortcut press this soon after the last is a bounce, not a new job. */
export const SHORTCUT_DEBOUNCE_MS = 250;

/** Ctrl+Enter, or Cmd+Enter on a Mac, for the button's tooltip (Shift+Enter works too). */
export function shortcutLabel(platform = '') {
  return /mac|iphone|ipad/i.test(String(platform)) ? '⌘+Enter' : 'Ctrl+Enter';
}

/** The prompt counter: how many characters, the cap and whether it is over. */
export function promptStatus(text, max = PROMPT_MAX) {
  const n = [...String(text || '')].length;
  return { n, max, over: n > max, empty: !String(text || '').trim() };
}

/** Whether Generate may run (it is also the gate for the shortcut). */
export function canGenerate({ prompt, busy, provider }) {
  const p = promptStatus(prompt);
  return !busy && !!provider && !p.empty && !p.over;
}

/** Whether Generate may run now. Running jobs never block it: the page and the
 *  server each cap how many a profile has in flight (state.max_in_flight). */
export function canStartAnother(running, max) {
  return !(Number.isFinite(max) && max > 0 && running >= max);
}

/** 1..max for the image count, whatever was typed. */
export function clampCount(value, max) {
  const hi = Math.max(1, Math.trunc(Number(max)) || 1);
  const n = Math.trunc(Number(value));
  return Number.isFinite(n) ? Math.min(hi, Math.max(1, n)) : 1;
}

/** A seed field -> null (blank) or a non-negative integer; NaN for junk. */
export function parseSeed(text) {
  const s = String(text == null ? '' : text).trim();
  if (!s) return null;
  if (!/^\d{1,10}$/.test(s)) return NaN;
  const n = Number(s);
  return n <= 2147483647 ? n : NaN;
}

/** The POST /generate body for the form's current state. */
export function buildRequest(form, caps) {
  const body = {
    prompt: String(form.prompt || '').trim(),
    provider: form.provider || '',
    model: String(form.model || '').trim(),
    ratio: form.ratio || '1:1',
    n: clampCount(form.n, caps.max_n),
  };
  if (caps.optimizer && form.optimizer) body.optimizer = true;
  if (caps.seed) {
    const seed = parseSeed(form.seed);
    if (seed !== null && !Number.isNaN(seed)) body.seed = seed;
  }
  if (caps.reference && form.reference) body.reference = form.reference;
  const currency = normalizeCurrency(form.currency);
  if (currency) body.currency = currency;
  return body;
}

/** The square to cut from a w x h picture for an avatar face. `pos` 0..1 slides
 *  the square along the long side (0.5 = centred; portraits default higher). */
export function cropRect(w, h, pos = 0.5) {
  const size = Math.min(w, h);
  const p = Math.min(1, Math.max(0, Number.isFinite(pos) ? pos : 0.5));
  return w >= h
    ? { sx: Math.round((w - size) * p), sy: 0, size }
    : { sx: 0, sy: Math.round((h - size) * p), size };
}

/** Where the crop starts for a given picture: faces sit in the upper third of
 *  a portrait, the middle of a landscape. */
export function defaultCropPos(w, h) {
  return h > w ? 0.25 : 0.5;
}

/** Gallery items for the shell's lightbox, oldest -> newest, from result rows. */
export function galleryItems(rows, { source = 'Image Studio', optimizerNote } = {}) {
  return rows.map((r) => ({
    src: r.url, downloadUrl: r.url, caption: r.prompt || '',
    info: galleryInfo(r, source, optimizerNote),
    name: `${(r.id || 'image').slice(0, 12)}${/\.[a-z0-9]{2,4}$/i.test(r.url) ? r.url.slice(r.url.lastIndexOf('.')) : '.png'}`,
  }));
}

/** The lightbox details for one result: prompt, model (provider · model), seed, when. */
export function galleryInfo(r, source = 'Image Studio', optimizerNote = 'optimizer on') {
  const info = { source };
  if (r.prompt) info.prompt = String(r.prompt);
  // With the provider's optimizer on, the model may have drawn a rewritten prompt; the
  // prompt shown and copied stays what was typed, and the model line says so.
  const model = [r.provider, r.model, r.optimizer ? optimizerNote : ''].filter(Boolean).join(' · ');
  if (model) info.model = model;
  if (r.seed != null && r.seed !== '') info.seed = String(r.seed);
  if (r.created_at) {
    const d = new Date(r.created_at);
    info.created = Number.isNaN(d.getTime()) ? String(r.created_at) : d.toLocaleString();
  }
  return info;
}

/** What an estimate depends on: same key = same answer, so no new call. Blank when
 *  there is nothing to estimate yet (no prompt, no provider). */
export function estimateKey(form) {
  const prompt = String(form.prompt || '').trim();
  if (!prompt || !form.provider) return '';
  return JSON.stringify([form.provider, String(form.model || '').trim(), form.ratio || '1:1', form.n, prompt,
    normalizeCurrency(form.currency)]);
}

/** The model name a result tile shows: the provider's friendly name for a model id when it
 *  has one (Civitai's AIRs read as "Juggernaut XL (Ragnarok)"), else the id as stored. */
export function modelLabel(r, providers = []) {
  const p = (providers || []).find((x) => x && x.id === r.provider);
  const names = (p && p.model_names) || {};
  return (r.model && names[r.model]) || r.model || '';
}

/** The note shown for a metered provider. */
export function isMetered(provider) {
  return !!(provider && provider.price_note);
}

// ---------------------------------------------------------------- sort + clear

/** The two orders the results grid offers. Newest first is the default for a
 *  viewer with no saved choice (3.1.0 to 4.0 showed oldest first). */
export const SORTS = ['oldest', 'newest'];

/** A stored or picked sort value -> one of SORTS. No saved preference = newest first. */
export function normalizeSort(value) {
  return SORTS.includes(value) ? value : 'newest';
}

/** Rows (as /images returns them, oldest first) in display order. Never mutates. */
export function sortRows(rows, order) {
  const list = Array.isArray(rows) ? rows.slice() : [];
  return normalizeSort(order) === 'newest' ? list.reverse() : list;
}

/** "Clear from here down": the ids of the picture `id` and every picture after
 *  it in the grid's order -- what POST /clear removes for the same order. */
export function clearFromHere(rows, id, order) {
  const shown = sortRows(rows, order);
  const i = shown.findIndex((r) => r && r.id === id);
  return i < 0 ? [] : shown.slice(i).map((r) => r.id);
}

// ---------------------------------------------------------------- delete by age

/** The scopes the Delete dialog offers; the server turns each into a window from ITS clock. */
export const DELETE_SCOPES = ['hour', 'day', 'all'];

export function normalizeScope(value) {
  return DELETE_SCOPES.includes(value) ? value : 'hour';
}

/** The locale key of the confirmation question for a scope and count. */
export function deleteConfirmKey(scope, n) {
  const k = DELETE_CONFIRM[normalizeScope(scope)];
  return n === 1 ? k.one : k.many;
}
// Literal keys (the locale test finds every key by its quoted name).
const DELETE_CONFIRM = {
  hour: { many: 'delete.confirm_hour', one: 'delete.confirm_hour_one' },
  day: { many: 'delete.confirm_day', one: 'delete.confirm_day_one' },
  all: { many: 'delete.confirm_all', one: 'delete.confirm_all_one' },
};
export const DELETE_SCOPE_LABELS = { hour: 'delete.scope_hour', day: 'delete.scope_day', all: 'delete.scope_all' };

/** The metadata a picture carries into a chat, so the chat's Details panel shows the
 *  prompt it was made from (not the caption typed when sending). */
export function sendMetadata(r) {
  const meta = { source: 'image-studio', image_id: r.id };
  if (r.prompt) meta.prompt = String(r.prompt);
  const model = [r.provider, r.model].filter(Boolean).join(' · ');
  if (model) meta.model = model;
  if (r.seed != null && r.seed !== '') meta.seed = r.seed;
  // The provider's optimizer may have drawn a rewritten prompt; the Details panel says so.
  if (r.optimizer) meta.optimizer = true;
  return meta;
}

// ---------------------------------------------------------------- Buzz type + balance

/** Which Buzz pays for a Civitai picture. '' = let Civitai choose. */
export const CURRENCIES = ['blue', 'yellow'];

/** A stored or picked value -> 'blue', 'yellow' or '' (anything else). */
export function normalizeCurrency(value) {
  return CURRENCIES.includes(value) ? value : '';
}

/** Whole numbers with the viewer's separators: 1234 -> "1,234". Never throws. */
export function formatCount(n, locale) {
  const v = Number(n);
  if (!Number.isFinite(v)) return '0';
  try { return new Intl.NumberFormat(locale || undefined).format(Math.trunc(v)); } catch { return String(Math.trunc(v)); }
}

/** The balance map -> display parts, yellow first, then blue, then any other type A-Z.
 *  Junk entries (non-numbers, odd type names) are dropped. */
export function balanceParts(balances) {
  if (!balances || typeof balances !== 'object') return [];
  const rank = (k) => (k === 'yellow' ? 0 : k === 'blue' ? 1 : 9);
  return Object.entries(balances)
    .filter(([k, v]) => /^[a-z]{1,16}$/.test(k) && typeof v === 'number' && Number.isFinite(v))
    .sort((a, b) => rank(a[0]) - rank(b[0]) || a[0].localeCompare(b[0]))
    .map(([type, n]) => ({ type, n: Math.trunc(n) }));
}

/** "1,234 yellow · 56 blue". `label(type)` gives the (translated) type name; '' when there is nothing. */
export function balanceLine(balances, label = (x) => x, locale) {
  return balanceParts(balances).map((p) => `${formatCount(p.n, locale)} ${label(p.type)}`).join(' · ');
}

// ---------------------------------------------------------------- model browser

/** The four lists the Browse panel offers (the server's sort names). */
export const BROWSE_SORTS = ['week', 'month', 'rated', 'new'];

export function normalizeBrowseSort(value) {
  return BROWSE_SORTS.includes(value) ? value : 'week';
}

/** 286000 -> "286k", 1250000 -> "1.3M", 950 -> "950". */
export function compactCount(n) {
  const v = Math.max(0, Math.trunc(Number(n) || 0));
  if (v >= 1e6) return `${(v / 1e6).toFixed(v >= 1e7 ? 0 : 1).replace(/\.0$/, '')}M`;
  if (v >= 1e4) return `${Math.round(v / 1e3)}k`;
  if (v >= 1e3) return `${(v / 1e3).toFixed(1).replace(/\.0$/, '')}k`;
  return String(v);
}

/** One row of the Browse list as plain text: "Name · Illustrious · 286k downloads".
 *  `downloads` is the already-translated "{n} downloads" text. */
export function browseLabel(row, downloads) {
  return [row && row.name, row && row.base, downloads].filter(Boolean).join(' · ');
}

/** Keep only well-formed rows from /civitai/models (the server already checks; this keeps a
 *  wrong reply from reaching the DOM). */
export function cleanBrowseRows(rows) {
  const ok = /^urn:air:sdxl:checkpoint:civitai:\d+@\d+$/;
  return (Array.isArray(rows) ? rows : []).filter((r) => r && typeof r.name === 'string' && r.name && ok.test(String(r.air)));
}

/** The Browse filter's words: whitespace folded, lower-cased, at most 60 characters (the
 *  server's limit), duplicates dropped. '' -> []. */
export const BROWSE_QUERY_MAX = 60;
export function browseWords(query) {
  const q = String(query || '').split(/\s+/).filter(Boolean).join(' ').slice(0, BROWSE_QUERY_MAX).trim();
  return [...new Set(q.toLowerCase().split(' ').filter(Boolean))];
}

/** Whether a Browse row's NAME holds every filter word (case-blind). No words: every row. */
export function browseMatches(row, words) {
  if (!words || !words.length) return true;
  const name = String((row && row.name) || '').toLowerCase();
  return words.every((w) => name.includes(w));
}

/** The names the Model box and the tiles show for picked models: AIR -> name. */
export function rememberModelName(names, row) {
  return { ...(names || {}), [row.air]: row.name };
}

/** The model the form will send: the typed one, else the provider's default. */
export function activeModel(provider, typed = '') {
  const m = String(typed || '').trim();
  return m || (provider && provider.model) || '';
}

/** What ``model`` can do on ``provider``: the per-model row when the server sent one,
 *  else the provider-wide ``supports`` (a typed id the server has never heard of). */
export function modelCaps(provider, model) {
  const none = { reference: false, optimizer: false, seed: false, max_n: 1 };
  if (!provider) return none;
  const by = provider.supports_by_model || {};
  return by[model] || provider.supports || none;
}

/** The longest prompt ``model`` takes on ``provider``. */
export function modelPromptMax(provider, model, fallback = PROMPT_MAX) {
  if (!provider) return fallback;
  const by = provider.prompt_max_by_model || {};
  return by[model] || provider.prompt_max || fallback;
}

/** The small line under the model field: {key, vars} for t(), or null. */
export function modelsNote(provider) {
  if (!provider) return null;
  const date = String(provider.models_checked_at || '').slice(0, 10);
  if (provider.models_source === 'endpoint') {
    return { key: date ? 'models.note_endpoint' : 'models.note_endpoint_nodate', vars: { label: provider.label, date } };
  }
  if (provider.models_source === 'documented') {
    return { key: date ? 'models.note_docs' : 'models.note_docs_nodate', vars: { label: provider.label, date } };
  }
  return null;
}

/** One plain sentence about a refresh: {key, vars, error}. */
export function refreshSummary(result, providers = []) {
  const res = (result && result.results) || {};
  const label = (id) => ((providers.find((p) => p.id === id) || {}).label) || id;
  const failed = Object.entries(res).filter(([, r]) => r && r.error);
  const live = Object.entries(res).filter(([, r]) => r && !r.error && r.source === 'endpoint');
  if (failed.length && !live.length) {
    return { key: 'models.refresh_failed', vars: { name: label(failed[0][0]), error: String(failed[0][1].error) }, error: true };
  }
  if (live.length) {
    const [id, r] = live[0];
    return { key: 'models.refreshed', vars: { n: r.count, name: label(id) }, error: false };
  }
  return { key: 'models.refresh_docs_only', vars: {}, error: false };
}
