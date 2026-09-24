// Per-thread model + thinking override (Feature 7): the header chip's math.
//
// Kept free of the DOM and of i18n on purpose. main.js owns the wiring
// (populating the <select>s, opening/closing the popover, calling the API) —
// everything HERE is the part worth testing without a browser: shaping a
// models.list response into <select> options, reading a context-budget
// object whose field names are not a contract this app owns, and turning a
// picker selection into the PATCH body the backend expects.

// A curated, common set — not the full list any one model actually supports.
// The REFUSAL is the real corrector here (see openclaw.AgentRefused): if a
// model only supports a subset, picking an unsupported level names the
// supported ones back, verbatim, rather than this list trying to predict it
// per model.
export const THINKING_LEVELS = ['off', 'low', 'medium', 'high', 'max', 'adaptive'];

/**
 * De-duplicate + defend a GET /api/bots/{id}/models response for a <select>.
 * The backend already normalizes each entry to {id, label}; this is the
 * last line of defense against a duplicate id (two providers advertising
 * the same string) reaching the DOM as two identical options.
 */
export function normalizeModelOptions(models) {
  const seen = new Set();
  const out = [];
  for (const m of Array.isArray(models) ? models : []) {
    if (!m || typeof m.id !== 'string' || !m.id.trim() || seen.has(m.id)) continue;
    seen.add(m.id);
    const label = typeof m.label === 'string' && m.label.trim() ? m.label : m.id;
    out.push({ id: m.id, label });
  }
  return out;
}

// Candidate field names for the gateway's contextBudgetStatus object, tried
// in order. openclaw.py's _parse_reply keeps this object VERBATIM. The first
// name in each list is what the gateway (2026.9.x) actually emits — read off
// real rows in the family app's database: `estimatedPromptTokens` and
// `contextTokenBudget`. (The first version of this file pinned
// `contextWindow`, a name the gateway never sends, so the meter only ever
// showed the used count — review 2026-09-24.) The rest are defensive so a
// gateway revision that renames the field does not blank the meter outright.
const USED_KEYS = ['estimatedPromptTokens', 'usedTokens', 'used', 'promptTokens'];
const WINDOW_KEYS = ['contextTokenBudget', 'contextWindow', 'windowTokens', 'window', 'maxTokens', 'limit'];

function firstFiniteNumber(obj, keys) {
  for (const k of keys) {
    const v = obj[k];
    if (typeof v === 'number' && Number.isFinite(v)) return v;
  }
  return null;
}

/**
 * {used, window} out of a contextBudgetStatus object, or null when there is
 * nothing usable in it (an unrecognised shape, or not an object at all).
 * `window` may be null even when `used` is present — some gateways may
 * report the estimate without a configured ceiling.
 */
export function contextMeterValues(contextBudget) {
  if (!contextBudget || typeof contextBudget !== 'object') return null;
  const used = firstFiniteNumber(contextBudget, USED_KEYS);
  if (used == null) return null;
  return { used, window: firstFiniteNumber(contextBudget, WINDOW_KEYS) };
}

/** Compact token count for a small chip: 900, 12.3k, 1.2M. Never negative
 *  input is expected (token counts), so no sign handling. */
export function compactTokens(n) {
  if (typeof n !== 'number' || !Number.isFinite(n)) return '';
  if (n < 1000) return String(Math.round(n));
  if (n < 1_000_000) return `${(n / 1000).toFixed(1).replace(/\.0$/, '')}k`;
  return `${(n / 1_000_000).toFixed(1).replace(/\.0$/, '')}M`;
}

/** The chip's "used/window" text, or '' when there is nothing to show yet
 *  (no assistant reply in the thread has carried a context_budget). */
export function meterText(contextBudget) {
  const v = contextMeterValues(contextBudget);
  if (!v) return '';
  return v.window != null
    ? `${compactTokens(v.used)}/${compactTokens(v.window)}`
    : compactTokens(v.used);
}

/**
 * A picker selection -> the PATCH .../prefs body. An empty-string selection
 * means "back to the bot's default", which the backend spells as a null
 * value (db.update_thread_prefs removes that key on null, merges on a
 * string) — see backend/app/main.py's patch_thread.
 */
export function prefsPatchFrom(modelValue, thinkingValue) {
  return {
    model: modelValue ? modelValue : null,
    thinking: thinkingValue ? thinkingValue : null,
  };
}

/**
 * The latest message (newest first) in `messages` that carries a
 * context_budget, or undefined. Mirrors the existing "model badge" lookup
 * in main.js's renderChatHeader (latest assistant message with metadata) so
 * the meter and the model text never disagree about which message they are
 * describing.
 */
export function latestContextBudget(messages) {
  if (!Array.isArray(messages)) return null;
  for (let i = messages.length - 1; i >= 0; i--) {
    const meta = messages[i] && messages[i].metadata;
    if (meta && meta.context_budget) return meta.context_budget;
  }
  return null;
}
