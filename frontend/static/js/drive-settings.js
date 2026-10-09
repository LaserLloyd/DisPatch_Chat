// Drive mode settings — what a driver can change, and where it is stored.
//
// Three scopes, kept apart on purpose (docs/voice-drive-mode.md, "Settings"):
//   device  one JSON blob in localStorage (`dispatch-drive`), sent to the
//           server on every WS `start` frame and re-sent when it changes;
//   server  the install's voice settings (the on/off switch, default voice,
//           speed; the Voice lab's thinking and timings), GET/PATCH
//           /api/voice/settings — unlocked tier only, like every /api/voice
//           route;
//   bot     `bots[].voice`, saved with the rest of the Bots tab.
//
// The pure half (load/migrate/normalise, the start payload, voice labels) is
// exported for tests/drive.test.js. The DOM half builds the rows for two
// surfaces that must look like the rest of the app: the Drive overlay's quick
// sheet and Settings → Voice (moved out of the Device tab 2026-10-10). Both
// are plain .dev-row markup.
//
// Settings → Voice holds EVERYDAY settings only (2026-10-10): the on/off
// switch on top, then Voice, Listening, Replies and This device. The
// advanced pieces are opt-in tools (js/voice-tools.js): Voice cloning,
// Built-in voices and Voice lab.

import { t, list as listFormat } from './i18n.js?v=3';
import { el, railIcon, RAIL_ICONS } from './util.js?v=20';
import { VOICE_REPO_URL } from './about.js?v=6';

export const DRIVE_KEY = 'dispatch-drive';
export const LEGACY_PATIENCE_KEY = 'dispatch-drive-patience';
export const PATIENCE_PRESETS = ['quick', 'normal', 'patient'];
export const REPLY_STYLES = ['short', 'normal', 'longer'];
export const THINKING_LEVELS = ['off', 'low', 'medium'];
export const SPEED_MIN = 0.8;
export const SPEED_MAX = 1.3;

export const DRIVE_DEFAULTS = Object.freeze({
  patience: 'normal',     // how long the server waits after you stop talking
  bargeIn: true,          // talking over a reply stops it
  autoListen: true,       // false = push-to-talk: pause after every reply
  sounds: true,           // earcons (heard you / working / ready / error)
  replyStyle: 'normal',   // short | normal | longer
  followUp: true,         // the bot may end with a question
  screenOn: true,         // Screen Wake Lock while Drive is open
  micId: '',              // '' = system default
  sinkId: '',             // '' = system default (setSinkId browsers only)
  captions: true,         // show what was heard / said on the Drive screen
  showTimings: false,     // Voice lab: latency numbers on the Drive screen
});

const BOOL_KEYS = ['bargeIn', 'autoListen', 'sounds', 'followUp', 'screenOn', 'captions', 'showTimings'];

/** Any stored value → a complete, valid settings object. Junk falls back. */
export function normalizeDriveSettings(raw) {
  const r = raw && typeof raw === 'object' ? raw : {};
  const out = { ...DRIVE_DEFAULTS };
  if (PATIENCE_PRESETS.includes(r.patience)) out.patience = r.patience;
  if (REPLY_STYLES.includes(r.replyStyle)) out.replyStyle = r.replyStyle;
  for (const k of BOOL_KEYS) if (typeof r[k] === 'boolean') out[k] = r[k];
  for (const k of ['micId', 'sinkId']) {
    if (typeof r[k] === 'string' && r[k].length <= 256) out[k] = r[k];
  }
  return out;
}

/**
 * Read the device settings. 2.1.0 stored only the patience preset, under its
 * own key; that value is carried into the blob once and the old key removed,
 * so a device keeps what it chose across the upgrade.
 */
export function loadDriveSettings(storage) {
  let parsed = null;
  try {
    const raw = storage && storage.getItem(DRIVE_KEY);
    if (raw) parsed = JSON.parse(raw);
  } catch { parsed = null; }
  if (parsed && typeof parsed === 'object') return normalizeDriveSettings(parsed);
  let legacy = null;
  try { legacy = storage && storage.getItem(LEGACY_PATIENCE_KEY); } catch { legacy = null; }
  const s = normalizeDriveSettings(PATIENCE_PRESETS.includes(legacy) ? { patience: legacy } : {});
  if (legacy != null) {
    try {
      storage.setItem(DRIVE_KEY, JSON.stringify(s));
      storage.removeItem(LEGACY_PATIENCE_KEY);
    } catch { /* private mode: migrate again next time */ }
  }
  return s;
}

const listeners = new Set();

/** Merge a change into the stored settings, tell every listener, return the result. */
export function saveDriveSettings(storage, patch) {
  const next = normalizeDriveSettings({ ...loadDriveSettings(storage), ...(patch || {}) });
  try { storage && storage.setItem(DRIVE_KEY, JSON.stringify(next)); } catch { /* private mode */ }
  for (const fn of [...listeners]) { try { fn(next); } catch { /* a listener's problem */ } }
  return next;
}

/** Subscribe to device-setting changes (the open Drive view re-sends `start`). */
export function onDriveSettings(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

/**
 * The WS `start` frame for these settings (contract: drive-contract.md).
 * `after` is the newest reply the driver heard; `afterIds` are the recent
 * ones, newest first — see heard-id tracking in drive.js for why both.
 */
export function startPayload(s, after = null, afterIds = null) {
  const v = normalizeDriveSettings(s);
  const frame = {
    type: 'start',
    patience: v.patience,
    barge_in: v.bargeIn,
    reply_style: v.replyStyle,
    follow_up: v.followUp,
  };
  if (after) frame.after = after;
  if (afterIds && afterIds.length) frame.after_ids = afterIds.slice(0, 8);
  return frame;
}

/** Clamp a speaking speed to the server's range, on a 0.05 grid. */
export function clampSpeed(x) {
  const v = Number(x);
  if (!Number.isFinite(v)) return 1;
  return Math.round(Math.min(SPEED_MAX, Math.max(SPEED_MIN, v)) * 20) / 20;
}

// ------------------------------------------------------------ server API --

async function voiceJson(url, opts = {}) {
  const r = await fetch(url, { credentials: 'same-origin', ...opts });
  if (!r.ok) {
    let detail = r.statusText;
    try { const b = await r.json(); detail = (b && (b.detail || b.error)) || detail; } catch { /* not json */ }
    if (detail && typeof detail === 'object') {
      try { detail = JSON.stringify(detail); } catch { detail = String(detail); }
    }
    const err = new Error(String(detail || r.status));
    err.status = r.status;
    throw err;
  }
  return r.status === 204 ? null : r.json();
}

export const voiceStatus = () => voiceJson('/api/voice/status');
export const voiceLab = () => voiceJson('/api/voice/lab');
export const voiceSettings = () => voiceJson('/api/voice/settings');
export const patchVoiceSettings = (patch) => voiceJson('/api/voice/settings', {
  method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(patch),
});
export { voiceJson };

// ---------------------------------------------------------- voice labels --

/** "builtin:af_heart" → {kind:'builtin', id:'af_heart'}; anything else is a profile id. */
export function parseVoice(v) {
  const s = String(v || '');
  if (!s) return { kind: 'default', id: '' };
  if (s.startsWith('builtin:')) return { kind: 'builtin', id: s.slice(8) };
  return { kind: 'profile', id: s };
}

/**
 * The select value for a built-in voice. The contract writes `builtin:<id>`;
 * a server may list ids already prefixed or bare, and both mean the same.
 */
export function builtinValue(id) {
  const s = String(id || '');
  return s.startsWith('builtin:') ? s : `builtin:${s}`;
}

/** A readable name for a voice value, from /api/voice/status. */
export function voiceName(status, value) {
  const p = parseVoice(value);
  const st = status || {};
  if (p.kind === 'profile') {
    if (st.stock_voice && p.id === st.stock_voice) return t('drive.voice_stock');
    const hit = (st.profiles || []).find((x) => x.id === p.id);
    return hit ? hit.name : p.id;
  }
  if (p.kind === 'builtin') {
    const hit = (st.builtin_voices || []).find((x) => builtinValue(x.id) === builtinValue(p.id));
    return t('drive.voice_builtin', { name: hit ? (hit.name || p.id) : p.id });
  }
  return '';
}

/**
 * Options for a voice <select>: cloned voices first (what people add), then
 * the built-in ones. `withDefault` adds the per-bot "Use the default" entry.
 * The current value is always present, even when the server no longer lists
 * it, so a select never silently shows a different voice than the one saved.
 */
export function voiceOptions(status, { withDefault = false, current = '', placeholder = false } = {}) {
  const st = status || {};
  const out = [];
  // No default voice yet (default_voice ""): a select must not pretend the
  // first voice is chosen. A neutral first entry says so until one is picked.
  if (placeholder && !current) out.push({ value: '', label: t('drive.voice_pick'), group: 'none' });
  if (withDefault) {
    const def = st.default_voice ? voiceName(st, st.default_voice) : '';
    out.push({ value: '', label: def ? t('drive.voice_use_default_named', { name: def }) : t('drive.voice_use_default') });
  }
  for (const p of st.profiles || []) {
    const stock = !!st.stock_voice && p.id === st.stock_voice;
    out.push({ value: p.id, label: stock ? t('drive.voice_stock') : p.name, group: stock ? 'stock' : 'profile' });
  }
  for (const b of st.builtin_voices || []) {
    out.push({ value: builtinValue(b.id), label: t('drive.voice_builtin', { name: b.name || b.id }), group: 'builtin' });
  }
  if (current && !out.some((o) => o.value === current)) {
    out.push({ value: current, label: voiceName(st, current) || current, group: 'missing' });
  }
  return out;
}

// ------------------------------------------------------------- preview ---

let previewAudio = null;
let previewUrl = null;
let previewBtn = null;

function stopPreview() {
  if (previewAudio) { try { previewAudio.pause(); } catch { /* gone */ } }
  if (previewUrl) { try { URL.revokeObjectURL(previewUrl); } catch { /* gone */ } }
  if (previewBtn) { previewBtn.setAttribute('aria-pressed', 'false'); previewBtn.classList.remove('busy'); }
  previewAudio = null; previewUrl = null; previewBtn = null;
}

/**
 * ▶ a short sample of `voice` (a profile id or builtin:<id>). A second tap on
 * the same button stops it. Errors go to `onError` in plain words.
 */
export async function playPreview(voice, btn, onError) {
  if (previewBtn && previewBtn === btn) { stopPreview(); return; }
  stopPreview();
  if (!voice) return;
  previewBtn = btn || null;
  if (btn) { btn.setAttribute('aria-pressed', 'true'); btn.classList.add('busy'); }
  try {
    const r = await fetch(`/api/voice/preview?voice=${encodeURIComponent(voice)}`, { credentials: 'same-origin' });
    if (!r.ok) throw new Error(String(r.status));
    if (previewBtn !== btn) return;            // superseded while loading
    previewUrl = URL.createObjectURL(await r.blob());
    previewAudio = new Audio(previewUrl);
    if (btn) btn.classList.remove('busy');
    previewAudio.onended = () => { if (previewBtn === btn) stopPreview(); };
    await previewAudio.play();
  } catch {
    if (previewBtn === btn) stopPreview();
    if (onError) onError(t('drive.err_preview'));
  }
}

// ------------------------------------------------------------ controls ---

const PLAY_ICON = ['M8 5.5v13l10.5-6.5Z'];
export const MIC_ICON = [
  'M12 3a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V6a3 3 0 0 0-3-3Z',
  'M19 11a7 7 0 0 1-14 0',
  'M12 18v3',
];

/** One settings row: name + hint on the start side, controls on the end. */
export function row(title, hint, controls = [], { id, block = false } = {}) {
  const text = el('span', { class: 'dev-row-text' }, [
    el('strong', { text: title, id: id ? `${id}-label` : null }),
    hint ? el('span', { class: 'dev-row-hint', text: hint, id: id ? `${id}-hint` : null }) : null,
  ]);
  return el('div', { class: 'dev-row drive-row' + (block ? ' drive-row-block' : ''), id }, [text, ...controls]);
}

/** On/off switch: the Device tab's switch, as a plain checkbox. */
export function switchInput(checked, label, onChange) {
  const cb = el('input', { type: 'checkbox', class: 'dev-switch', 'aria-label': label });
  cb.checked = !!checked;
  cb.addEventListener('change', () => onChange(cb.checked));
  return cb;
}

/** Segmented control (radio group) — one choice of a few. */
export function segmented(label, options, value, onChange) {
  const box = el('div', { class: 'drive-seg', role: 'radiogroup', 'aria-label': label });
  const btns = options.map((o) => {
    const b = el('button', { type: 'button', role: 'radio', class: 'drive-seg-btn', text: o.label, dataset: { value: o.value } });
    b.addEventListener('click', () => { set(o.value); onChange(o.value); });
    return b;
  });
  function set(v) {
    btns.forEach((b) => {
      const on = b.dataset.value === v;
      b.setAttribute('aria-checked', String(on));
      b.tabIndex = on ? 0 : -1;
    });
  }
  box.addEventListener('keydown', (e) => {
    const keys = ['ArrowRight', 'ArrowLeft', 'ArrowDown', 'ArrowUp'];
    if (!keys.includes(e.key)) return;
    e.preventDefault();
    const rtl = document.documentElement.dir === 'rtl';
    const fwd = e.key === 'ArrowDown' || e.key === (rtl ? 'ArrowLeft' : 'ArrowRight');
    const cur = btns.findIndex((b) => b.getAttribute('aria-checked') === 'true');
    const next = btns[(cur + (fwd ? 1 : btns.length - 1)) % btns.length];
    next.click(); next.focus();
  });
  box.append(...btns);
  set(value);
  box.setValue = set;
  return box;
}

/** A <select> in the Device tab's style. */
export function selectInput(label, options, value, onChange) {
  const sel = el('select', { class: 'lang-select drive-select', 'aria-label': label });
  fillSelect(sel, options, value);
  sel.addEventListener('change', () => onChange(sel.value));
  return sel;
}

export function fillSelect(sel, options, value) {
  sel.replaceChildren(...options.map((o) => {
    const opt = el('option', { value: o.value, text: o.label });
    if (o.value === value) opt.selected = true;
    return opt;
  }));
  sel.value = value;
}

/** ▶ preview button for whatever the paired select shows. */
export function previewButton(getVoice, onError) {
  const b = el('button', {
    type: 'button', class: 'icon-btn drive-preview', title: t('drive.preview'),
    'aria-label': t('drive.preview'), 'aria-pressed': 'false',
  }, [railIcon(PLAY_ICON)]);
  b.addEventListener('click', () => playPreview(getVoice(), b, onError));
  return b;
}

const patienceOptions = () => [
  { value: 'quick', label: t('drive.patience_quick') },
  { value: 'normal', label: t('drive.patience_normal') },
  { value: 'patient', label: t('drive.patience_patient') },
];
const replyOptions = () => [
  { value: 'short', label: t('drive.reply_short') },
  { value: 'normal', label: t('drive.reply_normal') },
  { value: 'longer', label: t('drive.reply_longer') },
];

/** Microphone / speaker choices. Labels need mic permission; before that they are blank. */
export async function deviceOptions(kind) {
  const out = [{ value: '', label: t('drive.device_default') }];
  try {
    const devs = await navigator.mediaDevices.enumerateDevices();
    let n = 0;
    for (const d of devs) {
      if (d.kind !== kind || !d.deviceId || d.deviceId === 'default' || d.deviceId === 'communications') continue;
      n += 1;
      out.push({ value: d.deviceId, label: d.label || t(kind === 'audioinput' ? 'drive.mic_n' : 'drive.speaker_n', { n }) });
    }
  } catch { /* no mediaDevices (http, old browser) */ }
  return out;
}

export function canPickSpeaker() {
  const AC = globalThis.AudioContext;
  return !!(AC && AC.prototype && 'setSinkId' in AC.prototype);
}

function storage() { return globalThis.localStorage; }

// --------------------------------------------------------- quick sheet ---

/**
 * The overlay gear's bottom sheet: only what a driver changes on the road.
 * Device settings apply at once (the Drive view re-sends `start` through
 * onDriveSettings); the voice is the install default, saved to the server.
 */
export function buildQuickSheet({ status, bot, onClose, onAllSettings, notify }) {
  const s = loadDriveSettings(storage());
  const set = (patch) => saveDriveSettings(storage(), patch);
  const st = status || {};
  const body = el('div', { class: 'dev-group-body drive-sheet-body' });

  // Voice: the default voice for the install. When this bot speaks with its
  // own voice the default does not reach it, and the row says so.
  const own = bot && bot.voice ? bot.voice : '';
  const voiceSel = selectInput(t('drive.voice'), voiceOptions(st, { current: st.default_voice || '', placeholder: true }), st.default_voice || '', async (v) => {
    if (!v) return;
    try {
      await patchVoiceSettings({ default_voice: v });
      st.default_voice = v;
    } catch { notify(t('drive.err_save')); }
  });
  const voiceHint = own
    ? t('drive.voice_bot_own', { bot: bot.name, name: voiceName(st, own) })
    : t('drive.voice_sheet_hint');
  const hasVoices = voiceOptions(st).length > 0;
  if (hasVoices) {
    body.append(row(t('drive.voice'), voiceHint, [voiceSel, previewButton(() => voiceSel.value, notify)]));
  }
  body.append(
    row(t('drive.patience'), t('drive.patience_hint'),
      [segmented(t('drive.patience'), patienceOptions(), s.patience, (v) => set({ patience: v }))],
      { block: true }),
    row(t('drive.barge_in'), t('drive.barge_in_hint'),
      [switchInput(s.bargeIn, t('drive.barge_in'), (v) => set({ bargeIn: v }))]),
    row(t('drive.reply_length'), t('drive.reply_length_hint'),
      [segmented(t('drive.reply_length'), replyOptions(), s.replyStyle, (v) => set({ replyStyle: v }))],
      { block: true }),
  );
  const micSel = selectInput(t('drive.mic'), [{ value: '', label: t('drive.device_default') }], s.micId, (v) => set({ micId: v }));
  deviceOptions('audioinput').then((opts) => fillSelect(micSel, opts, opts.some((o) => o.value === s.micId) ? s.micId : ''));
  body.append(row(t('drive.mic'), t('drive.mic_hint'), [micSel]));

  const all = el('button', { type: 'button', class: 'dev-row drive-row drive-row-link' }, [
    el('span', { class: 'dev-row-text' }, [
      el('strong', { text: t('drive.all_settings') }),
      el('span', { class: 'dev-row-hint', text: t('drive.all_settings_hint') }),
    ]),
    railIcon(RAIL_ICONS.forward),
  ]);
  all.addEventListener('click', () => onAllSettings && onAllSettings());
  body.append(all);

  const title = el('h2', { class: 'drive-sheet-title', id: 'drive-sheet-title', text: t('drive.sheet_title') });
  const closeBtn = el('button', {
    type: 'button', class: 'icon-btn ghost drive-sheet-close', 'aria-label': t('common.close'),
  }, [railIcon(RAIL_ICONS.close)]);
  closeBtn.addEventListener('click', () => onClose && onClose());
  const sheet = el('div', {
    class: 'drive-sheet', role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': 'drive-sheet-title',
  }, [el('div', { class: 'drive-sheet-head' }, [title, closeBtn]), body]);
  return sheet;
}

// ------------------------------------------------------ Settings → Voice --

/**
 * What Settings → Voice shows for this /api/voice/status answer.
 *
 *   'none'    no status (locked, network error, or a server whose voice
 *             package did not import): no tab at all.
 *   'forced'  DISPATCH_VOICE=0 on the server: no switch, one line that says
 *             so and who can change it.
 *   'off'     the switch is off (the default on a fresh install): the switch,
 *             and what turning it on needs.
 *   'setup'   the switch is on, but voice can't run yet: the switch, what is
 *             missing and the commands that fix it.
 *   'on'      voice works: the switch, then the everyday settings.
 */
export function voiceTabState(st) {
  if (!st || typeof st !== 'object') return 'none';
  if (st.forced === 'off') return 'forced';
  const on = typeof st.switch === 'boolean' ? st.switch : !!st.enabled;
  if (!on) return 'off';
  const missing = Array.isArray(st.missing) ? st.missing : [];
  return st.enabled && !missing.length ? 'on' : 'setup';
}

/** Where a setup command runs. Literal keys so the i18n scan sees them. */
const WHERE = {
  backend: 'drive.where_backend',
  start: 'drive.where_start',
  plugin: 'drive.where_plugin',
  env: 'drive.where_env',
};

/** dispatch-voice (MIT) is published on its own; DisPatch runs with it in
 *  its environment. The same `--with` on every `uv run` keeps it there. */
export const DV_REPO = VOICE_REPO_URL;
export const DV_WITH = `--with "dispatch-voice @ git+${DV_REPO}"`;
export const DV_STEPS = Object.freeze({
  models: { where: 'backend', cmd: `uv run --group voice ${DV_WITH} dispatch-voice fetch-models` },
  start: { where: 'start', cmd: `uv run --group voice ${DV_WITH} uvicorn app.main:app` },
});

/**
 * The commands that fix a /api/voice/status `missing` list, in the order
 * to run them: [{where, cmd}]. Commands are shell, never translated.
 */
export function setupSteps(missing) {
  const m = (Array.isArray(missing) ? missing : []).map(String);
  const has = (re) => m.some((x) => re.test(x));
  const steps = [];
  if (has(/^python package/)) steps.push({ where: 'backend', cmd: 'uv sync --group voice' });
  if (has(/^model (silero|parakeet|smart-turn)/)) steps.push({ where: 'backend', cmd: 'uv run python -m app.voice.download' });
  // dispatch-voice: install it into DisPatch's environment and fetch its
  // models in one go, then start DisPatch with it.
  const dvPkg = has(/^dispatch-voice package|^a text-to-speech engine/);
  if (dvPkg || has(/^dispatch-voice models/)) steps.push(DV_STEPS.models);
  if (dvPkg) steps.push(DV_STEPS.start);
  if (has(/^MINIMAX_API_KEY/)) steps.push({ where: 'env', cmd: 'MINIMAX_API_KEY=…' });
  return steps;
}

/** One "what to run" block: a hint line per step and the command as code. */
export function stepsBlock(steps) {
  const box = el('div', { class: 'voice-steps' });
  for (const s of steps || []) {
    box.append(
      el('p', { class: 'dev-row-hint voice-step-where', text: t(WHERE[s.where] || WHERE.backend) }),
      el('pre', { class: 'voice-cmd', dir: 'ltr' }, [el('code', { text: s.cmd })]),
    );
  }
  return box;
}

/** The missing list as one note, or null when nothing is missing. */
function missingNote(missing) {
  const list = Array.isArray(missing) ? missing.map(String) : [];
  if (!list.length) return null;
  return el('p', { class: 'drive-missing', role: 'note' }, [
    el('strong', { text: t('drive.missing_title') }), ' ',
    el('span', { text: listFormat(list) }),
  ]);
}

function group(titleKey, rows, extraClass = '') {
  return el('section', { class: 'dev-group drive-group' + (extraClass ? ' ' + extraClass : '') }, [
    el('h3', { class: 'dev-group-title', text: t(titleKey) }),
    el('div', { class: 'dev-group-body' }, rows.filter(Boolean)),
  ]);
}

/**
 * The switch on top of Settings → Voice. ctx.setEnabled(on) saves it and
 * resolves true/false; the caller rebuilds the tab from the fresh status.
 */
function switchGroup(st, ctx) {
  const forced = st.forced || null;
  if (forced === 'off') {
    return group('drive.group_switch', [row(t('drive.switch'), t('drive.switch_forced_off'), [], { id: 'drive-switch' })]);
  }
  const on = forced === 'on' ? true : !!st.switch;
  const cb = switchInput(on, t('drive.switch'), async (v) => {
    cb.disabled = true;
    const ok = ctx.setEnabled ? await ctx.setEnabled(v) : false;
    if (!ok) { cb.checked = !v; cb.disabled = false; }
  });
  cb.id = 'drive-switch-input';
  if (forced === 'on') cb.disabled = true;
  const hint = forced === 'on' ? t('drive.switch_forced_on') : t('drive.switch_hint');
  return group('drive.group_switch', [row(t('drive.switch'), hint, [cb], { id: 'drive-switch' })]);
}

/** Below the switch while voice is off or not ready: what it needs. */
function needsGroup(st, state) {
  const missing = Array.isArray(st.missing) ? st.missing : [];
  const r = row(
    state === 'off' ? t('drive.needs_title_off') : t('drive.setup_title'),
    state === 'off' ? t('drive.needs_hint_off') : t('drive.setup_hint'),
    [], { id: 'drive-setup' },
  );
  r.classList.add('dev-row-block');
  const note = missingNote(missing);
  if (note) r.append(note);
  const steps = setupSteps(missing);
  if (steps.length) {
    r.append(stepsBlock(steps));
    r.append(el('p', { class: 'dev-row-hint drive-setup-fix', text: t('drive.setup_restart') }));
  } else if (missing.length) {
    r.append(el('p', { class: 'dev-row-hint drive-setup-fix', text: t('drive.setup_docs') }));
  }
  return group('drive.group_status', [r]);
}

/**
 * Settings → Voice. `host` is the #dev-group-voice wrapper. Re-callable: it
 * rebuilds from scratch (cheap) whenever the status changes.
 *
 * ctx: {
 *   status,                       the last /api/voice/status
 *   bots: () => [{id,name,voice?,safe,visible}],
 *   setEnabled(on) → Promise<bool>  save the switch (main.js re-reads status)
 *   cloning: bool                 the Voice cloning tool is on and installed
 *   openCloning()                 open that tool
 *   toast(msg, err)
 * }
 */
export function mountDriveSettings(host, ctx) {
  if (!host) return;
  const st = ctx.status || {};
  const state = voiceTabState(st);
  const groups = [switchGroup(st, ctx)];
  if (state !== 'on') {
    // Off with nothing missing: the switch alone says it all (its hint names
    // the microphone button). Off with something missing, or on but not
    // ready: what it needs and how to get it.
    const missing = Array.isArray(st.missing) ? st.missing : [];
    if (state === 'setup' || (state === 'off' && missing.length)) groups.push(needsGroup(st, state));
    host.replaceChildren(...groups);
    return;
  }
  const s = loadDriveSettings(storage());
  const set = (patch) => saveDriveSettings(storage(), patch);
  const notify = (m) => ctx.toast && ctx.toast(m, true);
  const save = async (patch) => {
    try {
      const r = await patchVoiceSettings(patch);
      Object.assign(st, patch, r && typeof r === 'object' ? r : {});
      return true;
    } catch (e) {
      notify(e && e.status === 404 ? t('drive.err_settings_missing') : t('drive.err_save'));
      return false;
    }
  };

  // ---- Voice
  const defVoice = st.default_voice || '';
  const voiceRows = [];
  const builtins = Array.isArray(st.builtin_voices) ? st.builtin_voices : [];
  const stock = st.stock_voice || null;
  const own = (st.profiles || []).filter((p) => p.id !== stock);
  if (voiceOptions(st).length) {
    const opts = () => voiceOptions(st, { current: st.default_voice || '', placeholder: true });
    const sel = selectInput(t('drive.default_voice'), opts(), defVoice, async (v) => {
      if (!v) return;
      if (await save({ default_voice: v })) {
        // The placeholder goes once a voice is chosen.
        fillSelect(sel, opts(), st.default_voice || v);
        paintSpeed();
      }
    });
    const r = row(t('drive.default_voice'), t('drive.default_voice_hint'), [sel, previewButton(() => sel.value, notify)]);
    // ONE hint line, written from the roster when it is known: who uses the
    // default, or that nobody does right now. The generic "every bot that has
    // no voice of its own" is only the fallback before the roster is loaded —
    // never stacked under the live sentence, where the pair read as a
    // contradiction ("used by every bot…" / "every bot has its own").
    const hintEl = r.querySelector('.dev-row-hint');
    const paintUsers = () => {
      const bots = (ctx.bots ? ctx.bots() : []).filter((b) => !b.safe && b.visible !== false);
      const known = bots.length > 0 && bots.every((b) => typeof b.voice === 'string');
      if (!known) { hintEl.textContent = t('drive.default_voice_hint'); return; }
      const uses = bots.filter((b) => !b.voice).map((b) => b.name);
      hintEl.textContent = uses.length
        ? t('drive.voice_users', { names: listFormat(uses) })
        : t('drive.voice_users_none');
    };
    paintUsers();
    // What you hear when you have not added a voice of your own: built-in
    // voices (the optional plugin), or dispatch-voice's stock voice.
    const note = !own.length ? (builtins.length ? 'drive.voice_builtin_only' : (stock ? 'drive.voice_stock_only' : null)) : null;
    if (note) r.querySelector('.dev-row-text').append(el('span', { class: 'dev-row-hint drive-note', text: t(note) }));
    voiceRows.push(r);

    // Speaking speed: built-in voices only (a cloned or the stock voice
    // speaks at its own pace), so no row at all without the plugin.
    const speed = el('input', {
      type: 'range', class: 'drive-range', min: String(SPEED_MIN), max: String(SPEED_MAX), step: '0.05',
      'aria-label': t('drive.speed'),
    });
    const out = el('output', { class: 'drive-range-value' });
    const showSpeed = () => {
      const v = clampSpeed(speed.value);
      out.textContent = Math.abs(v - 1) < 0.001 ? t('drive.speed_normal') : t('drive.speed_value', { x: v.toFixed(2) });
    };
    speed.value = String(clampSpeed(st.speed != null ? st.speed : 1));
    showSpeed();
    speed.addEventListener('input', showSpeed);
    speed.addEventListener('change', () => save({ speed: clampSpeed(speed.value) }));
    const speedRow = row(t('drive.speed'), t('drive.speed_hint'), [speed, out], { id: 'drive-speed' });
    function paintSpeed() {
      const cloned = parseVoice(sel.value).kind === 'profile';
      speedRow.querySelector('.dev-row-hint').textContent = cloned ? t('drive.speed_hint_cloned') : t('drive.speed_hint');
    }
    paintSpeed();
    if (builtins.length) voiceRows.push(speedRow);
  } else {
    voiceRows.push(row(t('drive.voice_none'), t('drive.voice_none_hint'), [], { id: 'drive-no-voice' }));
  }
  if (ctx.cloning) {
    const open = el('button', { type: 'button', class: 'btn-secondary', id: 'dev-voices-open' }, [
      railIcon(MIC_ICON), document.createTextNode(` ${t('drive.manage_voices_open')}`),
    ]);
    open.addEventListener('click', () => ctx.openCloning && ctx.openCloning());
    const r = row(t('drive.manage_voices'), t('drive.manage_voices_hint'), [open]);
    r.classList.add('dev-row-action');
    voiceRows.push(r);
  }
  groups.push(group('drive.group_voice', voiceRows));

  // ---- Listening
  groups.push(group('drive.group_listening', [
    row(t('drive.patience'), t('drive.patience_hint'),
      [segmented(t('drive.patience'), patienceOptions(), s.patience, (v) => set({ patience: v }))], { block: true }),
    row(t('drive.barge_in'), t('drive.barge_in_hint'),
      [switchInput(s.bargeIn, t('drive.barge_in'), (v) => set({ bargeIn: v }))]),
    row(t('drive.auto_listen'), t('drive.auto_listen_hint'),
      [switchInput(s.autoListen, t('drive.auto_listen'), (v) => set({ autoListen: v }))]),
    row(t('drive.sounds'), t('drive.sounds_hint'),
      [switchInput(s.sounds, t('drive.sounds'), (v) => set({ sounds: v }))]),
  ]));

  // ---- Replies
  groups.push(group('drive.group_replies', [
    row(t('drive.reply_length'), t('drive.reply_length_hint'),
      [segmented(t('drive.reply_length'), replyOptions(), s.replyStyle, (v) => set({ replyStyle: v }))], { block: true }),
    row(t('drive.follow_up'), t('drive.follow_up_hint'),
      [switchInput(s.followUp, t('drive.follow_up'), (v) => set({ followUp: v }))]),
  ]));

  // ---- This device
  const micSel = selectInput(t('drive.mic'), [{ value: '', label: t('drive.device_default') }], s.micId, (v) => set({ micId: v }));
  deviceOptions('audioinput').then((o) => fillSelect(micSel, o, o.some((x) => x.value === s.micId) ? s.micId : ''));
  let spkRow = null;
  if (canPickSpeaker()) {
    const spk = selectInput(t('drive.speaker'), [{ value: '', label: t('drive.device_default') }], s.sinkId, (v) => set({ sinkId: v }));
    deviceOptions('audiooutput').then((o) => fillSelect(spk, o, o.some((x) => x.value === s.sinkId) ? s.sinkId : ''));
    spkRow = row(t('drive.speaker'), t('drive.speaker_hint'), [spk]);
  }
  groups.push(group('drive.group_device', [
    row(t('drive.screen_on'), t('drive.screen_on_hint'),
      [switchInput(s.screenOn, t('drive.screen_on'), (v) => set({ screenOn: v }))]),
    row(t('drive.mic'), t('drive.mic_hint'), [micSel]),
    spkRow,
    row(t('drive.captions'), t('drive.captions_hint'),
      [switchInput(s.captions, t('drive.captions'), (v) => set({ captions: v }))]),
  ]));

  host.replaceChildren(...groups);
}

/**
 * "Your voices, with a built-in fallback · Smart Turn on · Speech
 * recognition: parakeet" in the user's words (the Voice lab's engine row).
 *
 *   cloud         DISPATCH_VOICE_TTS=minimax
 *   cloned+fallback  dispatch-voice speaks, the built-in plugin backs it up
 *   cloned only   dispatch-voice with voices of your own, no plugin
 *   stock only    dispatch-voice with only its stock voice, no plugin
 *   built-in      the plugin alone (no dispatch-voice)
 */
export function engineLine(st) {
  const s = st || {};
  const parts = [];
  const tts = String(s.tts || 'auto');
  const stock = s.stock_voice || null;
  const own = (s.profiles || []).filter((p) => p && p.id !== stock);
  const builtin = (s.builtin_voices || []).length > 0;
  const dv = !!(s.voices_ui || stock || (s.extensions || []).includes('dispatch-voice'));
  const cloner = tts === 'auto' || tts === 'chatterbox';
  if (tts === 'minimax') parts.push(t('drive.engine_cloud'));
  else if (dv && cloner && builtin) parts.push(t('drive.engine_cloned'));
  else if (dv && cloner && own.length) parts.push(t('drive.engine_cloned_only'));
  else if (dv && cloner) parts.push(t('drive.engine_stock'));
  else parts.push(t('drive.engine_builtin'));
  parts.push(s.smart_turn ? t('drive.engine_turn_on') : t('drive.engine_turn_off'));
  if (s.stt) parts.push(t('drive.engine_stt', { name: String(s.stt) }));
  return parts.join(' · ');
}

// ----------------------------------------------------- Bots tab pill ---

/**
 * The fourth pill on a Bots-tab card. A <select> dressed as the pill: tap it,
 * pick a voice, and the change waits for Save like Safe/React/Pool do.
 * `onChange(value)` marks the roster dirty.
 */
export function botVoicePill(bot, status, onChange) {
  const cur = typeof bot.voice === 'string' ? bot.voice : '';
  const wrap = el('label', { class: 'bm-safe-btn bm-voice-pill' + (cur ? ' on' : ''), title: t('drive.bot_voice_title') });
  const sel = el('select', { class: 'bm-voice-select', 'aria-label': t('drive.bot_voice_aria', { name: bot.name }) });
  fillSelect(sel, voiceOptions(status, { withDefault: true, current: cur }), cur);
  const word = el('span', { class: 'bm-voice-word' });
  // Off: the word "Voice". On: the voice's own name, so the card answers
  // "why does a bot sound like that" at a glance.
  const paint = () => {
    word.textContent = sel.value ? voiceName(status, sel.value) : t('drive.bot_voice_badge');
    wrap.classList.toggle('on', !!sel.value);
  };
  paint();
  sel.addEventListener('change', () => { paint(); onChange(sel.value); });
  sel.addEventListener('click', (e) => e.stopPropagation());
  wrap.append(railIcon(MIC_ICON), word, sel);
  return wrap;
}
