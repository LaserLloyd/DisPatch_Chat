// Split view for the generic tool pane (js/tools.js): the chat on one side, the
// tool on the other, with a draggable divider between them. This file is the
// pure half - sizes, the on/off decision, keyboard steps and the two stored
// preferences - so it can be tested without a DOM. No side effects at import.
//
// Sizes are in CSS pixels of the content column (the width #chatview has when
// the thread list is showing). `ratio` is the TOOL's share of the room that is
// left once the divider is taken out; the chat gets the rest.

export const SPLIT_KEY = 'dispatch-tool-split';
export const RATIO_KEY = 'dispatch-tool-split-ratio';

/** Narrowest each side may get. Below MIN_CHAT + MIN_TOOL + DIVIDER the shell
 *  shows the tool full width again (and remembers that split was asked for). */
export const MIN_CHAT = 320;
export const MIN_TOOL = 360;
export const DIVIDER = 10;
export const DEFAULT_RATIO = 0.5;
export const KEY_STEP = 24;          // px per arrow press
export const KEY_STEP_BIG = 96;      // px with Shift

/** Room for the two panes (the divider is not part of it). */
export function room(total) {
  const n = Number(total);
  return Number.isFinite(n) ? Math.max(0, Math.floor(n) - DIVIDER) : 0;
}

/** Is there space for both sides at their minimum? */
export function canSplit(total) {
  return room(total) >= MIN_CHAT + MIN_TOOL;
}

/** Should the view be split right now? Needs the saved preference, a wide
 *  window (never on the phone layout) and room for both minimums. */
export function shouldSplit({ pref, mobile, total }) {
  return !!pref && !mobile && canSplit(total);
}

/** The legal range of the chat side's width, in px (null when it cannot split). */
export function chatRange(total) {
  if (!canSplit(total)) return null;
  const r = room(total);
  return { min: MIN_CHAT, max: r - MIN_TOOL };
}

/** Clamp a ratio so neither side goes under its minimum. A junk ratio falls back
 *  to the default. Returns a number in (0, 1), or null when it cannot split. */
export function clampRatio(ratio, total) {
  const range = chatRange(total);
  if (!range) return null;
  const r = room(total);
  let v = Number(ratio);
  if (!Number.isFinite(v)) v = DEFAULT_RATIO;
  const lo = MIN_TOOL / r;
  const hi = 1 - MIN_CHAT / r;
  return Math.min(hi, Math.max(lo, v));
}

/** Widths for a ratio: {chat, tool} in whole px, always summing to room(total). */
export function widths(ratio, total) {
  const v = clampRatio(ratio, total);
  if (v == null) return null;
  const r = room(total);
  const tool = Math.round(r * v);
  return { chat: r - tool, tool };
}

/** The ratio that gives the chat side `chatPx` (what a drag works in). */
export function ratioForChat(chatPx, total) {
  const r = room(total);
  if (!r) return DEFAULT_RATIO;
  return clampRatio(1 - Number(chatPx) / r, total);
}

/** A key on the divider -> the new ratio, or null when the key is not ours.
 *  Left/Right move the divider that way on screen (so in a right-to-left
 *  layout, where the chat sits on the right, the sign flips); Shift = a bigger
 *  step; Home/End send it to the chat's minimum / maximum. */
export function stepRatio(key, { ratio, total, rtl = false, shift = false } = {}) {
  const range = chatRange(total);
  if (!range) return null;
  const w = widths(ratio, total);
  const step = shift ? KEY_STEP_BIG : KEY_STEP;
  let chat;
  if (key === 'ArrowRight') chat = w.chat + (rtl ? -step : step);
  else if (key === 'ArrowLeft') chat = w.chat + (rtl ? step : -step);
  else if (key === 'Home') chat = range.min;
  else if (key === 'End') chat = range.max;
  else return null;
  chat = Math.min(range.max, Math.max(range.min, chat));
  return ratioForChat(chat, total);
}

// ---- storage (every access guarded: private windows, blocked site data) ----
// Both keys are on the privacy wipe list (js/privacy.js APP_KEYS). The calls
// name localStorage directly because tests/privacy-keys.test.js finds writers
// by that spelling; tests stub globalThis.localStorage.

export function readPref() {
  try { return localStorage.getItem(SPLIT_KEY) === '1'; } catch { return false; }
}

export function writePref(on) {
  try {
    if (on) localStorage.setItem(SPLIT_KEY, '1'); else localStorage.removeItem(SPLIT_KEY);
    return true;
  } catch { return false; }
}

/** The saved ratio, or the default when it is missing, not a number or outside
 *  (0, 1). */
export function readRatio() {
  try {
    const raw = localStorage.getItem(RATIO_KEY);
    if (raw == null || raw === '') return DEFAULT_RATIO;
    const v = Number(raw);
    return Number.isFinite(v) && v > 0 && v < 1 ? v : DEFAULT_RATIO;
  } catch { return DEFAULT_RATIO; }
}

export function writeRatio(ratio) {
  const v = Number(ratio);
  if (!Number.isFinite(v) || v <= 0 || v >= 1) return false;
  try { localStorage.setItem(RATIO_KEY, String(Math.round(v * 10000) / 10000)); return true; } catch { return false; }
}
