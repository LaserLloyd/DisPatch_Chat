// Image Studio — the pure half of the page (no DOM, no fetch), so the rules
// that matter are one `node --test` away: the generate shortcut, the prompt
// counter, how many pictures a provider allows, the avatar crop and what a
// request body looks like.

export const PROMPT_MAX = 1500;

/** Ctrl+Enter / Cmd+Enter starts a generation; plain Enter stays a newline.
 *  Never while an IME composition is in flight (Enter confirms a candidate). */
export function isGenerateShortcut(ev) {
  if (!ev || ev.key !== 'Enter' || ev.isComposing) return false;
  return !!(ev.ctrlKey || ev.metaKey) && !ev.altKey && !ev.shiftKey;
}

/** Ctrl+Enter, or Cmd+Enter on a Mac, for the button's tooltip. */
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
export function galleryItems(rows) {
  return rows.map((r) => ({
    src: r.url, downloadUrl: r.url, caption: r.prompt || '',
    name: `${(r.id || 'image').slice(0, 12)}${/\.[a-z0-9]{2,4}$/i.test(r.url) ? r.url.slice(r.url.lastIndexOf('.')) : '.png'}`,
  }));
}

/** The note shown for a metered provider. */
export function isMetered(provider) {
  return !!(provider && provider.price_note);
}
