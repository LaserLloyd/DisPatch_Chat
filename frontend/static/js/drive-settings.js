// Drive mode settings — what a driver can change, and where it is stored.
//
// Three scopes, kept apart on purpose (docs/voice-drive-mode.md, "Settings"):
//   device  one JSON blob in localStorage (`dispatch-drive`), sent to the
//           server on every WS `start` frame and re-sent when it changes;
//   server  the install's voice settings (default voice, speed, thinking),
//           GET/PATCH /api/voice/settings — unlocked tier only, like every
//           /api/voice route;
//   bot     `bots[].voice`, saved with the rest of the Bots tab.
//
// The pure half (load/migrate/normalise, the start payload, voice labels) is
// exported for tests/drive.test.js. The DOM half builds the rows for two
// surfaces that must look like the rest of the app: the Drive overlay's quick
// sheet and Settings → Device → Drive mode. Both are plain .dev-row markup.

import { t, list as listFormat } from './i18n.js?v=3';
import { el, railIcon, RAIL_ICONS } from './util.js?v=20';

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
});

const BOOL_KEYS = ['bargeIn', 'autoListen', 'sounds', 'followUp', 'screenOn', 'captions'];

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
export function voiceOptions(status, { withDefault = false, current = '' } = {}) {
  const st = status || {};
  const out = [];
  if (withDefault) {
    const def = st.default_voice ? voiceName(st, st.default_voice) : '';
    out.push({ value: '', label: def ? t('drive.voice_use_default_named', { name: def }) : t('drive.voice_use_default') });
  }
  for (const p of st.profiles || []) out.push({ value: p.id, label: p.name, group: 'profile' });
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
  const voiceSel = selectInput(t('drive.voice'), voiceOptions(st, { current: st.default_voice || '' }), st.default_voice || '', async (v) => {
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

// ------------------------------------------------ Settings → Device group --

/**
 * Settings → Device → Drive mode. `host` is the #dev-group-voice wrapper.
 * ctx: { status, bots: () => [{id,name,voice?,safe}], openVoices(), toast(msg, err) }
 * Re-callable: it rebuilds from scratch (cheap) when the status changes.
 */
export function mountDriveSettings(host, ctx) {
  if (!host) return;
  const st = ctx.status || {};
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
  const groups = [];
  const group = (titleKey, rows) => {
    const sec = el('section', { class: 'dev-group drive-group' }, [
      el('h3', { class: 'dev-group-title', text: t(titleKey) }),
      el('div', { class: 'dev-group-body' }, rows.filter(Boolean)),
    ]);
    groups.push(sec);
  };

  // ---- Voice
  const defVoice = st.default_voice || '';
  const voiceRows = [];
  if (voiceOptions(st).length) {
    const sel = selectInput(t('drive.default_voice'), voiceOptions(st, { current: defVoice }), defVoice, async (v) => {
      if (await save({ default_voice: v })) paintSpeed();
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
    if (!(st.profiles || []).length) {
      r.querySelector('.dev-row-text').append(el('span', { class: 'dev-row-hint drive-note', text: t('drive.voice_builtin_only') }));
    }
    voiceRows.push(r);

    // Speaking speed: Kokoro only — a cloned voice speaks at its own pace.
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
    const speedRow = row(t('drive.speed'), t('drive.speed_hint'), [speed, out]);
    function paintSpeed() {
      const cloned = parseVoice(sel.value).kind === 'profile';
      speedRow.querySelector('.dev-row-hint').textContent = cloned ? t('drive.speed_hint_cloned') : t('drive.speed_hint');
    }
    paintSpeed();
    voiceRows.push(speedRow);
  }
  if (st.voices_ui) {
    const open = el('button', { type: 'button', class: 'btn-secondary', id: 'dev-voices-open' }, [
      railIcon(MIC_ICON), document.createTextNode(` ${t('drive.manage_voices_open')}`),
    ]);
    open.addEventListener('click', () => ctx.openVoices && ctx.openVoices());
    const r = row(t('drive.manage_voices'), t('drive.manage_voices_hint'), [open]);
    r.classList.add('dev-row-action');
    voiceRows.push(r);
  }
  group('drive.group_voice', voiceRows);

  // ---- Listening
  group('drive.group_listening', [
    row(t('drive.patience'), t('drive.patience_hint'),
      [segmented(t('drive.patience'), patienceOptions(), s.patience, (v) => set({ patience: v }))], { block: true }),
    row(t('drive.barge_in'), t('drive.barge_in_hint'),
      [switchInput(s.bargeIn, t('drive.barge_in'), (v) => set({ bargeIn: v }))]),
    row(t('drive.auto_listen'), t('drive.auto_listen_hint'),
      [switchInput(s.autoListen, t('drive.auto_listen'), (v) => set({ autoListen: v }))]),
    row(t('drive.sounds'), t('drive.sounds_hint'),
      [switchInput(s.sounds, t('drive.sounds'), (v) => set({ sounds: v }))]),
  ]);

  // ---- Replies
  group('drive.group_replies', [
    row(t('drive.reply_length'), t('drive.reply_length_hint'),
      [segmented(t('drive.reply_length'), replyOptions(), s.replyStyle, (v) => set({ replyStyle: v }))], { block: true }),
    row(t('drive.follow_up'), t('drive.follow_up_hint'),
      [switchInput(s.followUp, t('drive.follow_up'), (v) => set({ followUp: v }))]),
  ]);

  // ---- This device
  const micSel = selectInput(t('drive.mic'), [{ value: '', label: t('drive.device_default') }], s.micId, (v) => set({ micId: v }));
  deviceOptions('audioinput').then((o) => fillSelect(micSel, o, o.some((x) => x.value === s.micId) ? s.micId : ''));
  let spkRow = null;
  if (canPickSpeaker()) {
    const spk = selectInput(t('drive.speaker'), [{ value: '', label: t('drive.device_default') }], s.sinkId, (v) => set({ sinkId: v }));
    deviceOptions('audiooutput').then((o) => fillSelect(spk, o, o.some((x) => x.value === s.sinkId) ? s.sinkId : ''));
    spkRow = row(t('drive.speaker'), t('drive.speaker_hint'), [spk]);
  }
  group('drive.group_device', [
    row(t('drive.screen_on'), t('drive.screen_on_hint'),
      [switchInput(s.screenOn, t('drive.screen_on'), (v) => set({ screenOn: v }))]),
    row(t('drive.mic'), t('drive.mic_hint'), [micSel]),
    spkRow,
    row(t('drive.captions'), t('drive.captions_hint'),
      [switchInput(s.captions, t('drive.captions'), (v) => set({ captions: v }))]),
    advancedRow(st, save),
  ]);

  host.replaceChildren(...groups);
}

/** Engine status + the one operator knob that is safe in the UI. */
function advancedRow(st, save) {
  const det = el('details', { class: 'dev-row dev-row-block drive-advanced' });
  const sum = el('summary', { class: 'drive-advanced-summary' }, [
    el('span', { class: 'dev-row-text' }, [
      el('strong', { text: t('drive.advanced') }),
      el('span', { class: 'dev-row-hint', text: engineLine(st) }),
    ]),
  ]);
  det.append(sum);
  const missing = Array.isArray(st.missing) ? st.missing : [];
  if (missing.length) {
    det.append(el('p', { class: 'drive-missing', role: 'note' }, [
      el('strong', { text: t('drive.missing_title') }), ' ',
      el('span', { text: listFormat(missing.map(String)) }),
    ]));
  }
  if (st.ready === false && !missing.length) {
    det.append(el('p', { class: 'drive-missing', text: t('drive.engine_not_ready') }));
  }
  const thinking = selectInput(t('drive.thinking'), THINKING_LEVELS.map((v) => ({ value: v, label: t(`drive.thinking_${v}`) })),
    THINKING_LEVELS.includes(st.voice_turn_thinking) ? st.voice_turn_thinking : 'off',
    (v) => save({ voice_turn_thinking: v }));
  det.append(el('div', { class: 'drive-advanced-row' }, [
    el('span', { class: 'dev-row-text' }, [
      el('strong', { text: t('drive.thinking') }),
      el('span', { class: 'dev-row-hint', text: t('drive.thinking_hint') }),
    ]),
    thinking,
  ]));
  return det;
}

/** "Cloned voices with built-in fallback · Smart Turn on · Parakeet" in the user's words. */
export function engineLine(st) {
  const s = st || {};
  const parts = [];
  const tts = String(s.tts || 'auto');
  if ((s.profiles || []).length && (tts === 'auto' || tts === 'chatterbox')) parts.push(t('drive.engine_cloned'));
  else if (tts === 'minimax') parts.push(t('drive.engine_cloud'));
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
