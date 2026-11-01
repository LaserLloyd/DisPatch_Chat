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
  return JSON.stringify([form.provider, String(form.model || '').trim(), form.ratio || '1:1', form.n, prompt]);
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
