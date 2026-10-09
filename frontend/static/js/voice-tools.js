// Voice tools — Drive mode's advanced pieces, as opt-in tools (2026-10-10).
//
// Settings → Voice keeps the everyday settings. Everything an owner sets up
// once, or only when tuning, lives in three builtin tools of the Tools
// framework (tools.yaml `kind: builtin`; js/tools.js draws their tiles,
// main.js hands the opener to this module):
//
//   voice-cloning  Voice cloning   the Voices manager (js/voices.js), needs
//                                  the separate dispatch-voice package
//   voice-builtin  Built-in voices status + list of an optional built-in
//                                  voices plugin (it names itself)
//   voice-lab      Voice lab       engines, cloud/rig status, timing, the
//                                  thinking level, a quick record-and-replay
//
// They are OFF unless tools.yaml has a row saying `enabled: true` (no row =
// off), unlocked tier only, never in Safe Mode — the server lists them only
// for an operator and their routes 404 while off. A tool whose package is
// missing still opens: it says what is missing and how to install it.
//
// Markup is the Settings .dev-group / .dev-row anatomy, so a tool pane reads
// as the same app as Settings → Voice.

import { t, list as listFormat } from './i18n.js?v=3';
import { el, railIcon } from './util.js?v=20';
import {
  voiceStatus, voiceLab, voiceJson, patchVoiceSettings, row, switchInput, selectInput,
  previewButton, builtinValue, engineLine, setupSteps, stepsBlock, loadDriveSettings,
  saveDriveSettings, THINKING_LEVELS, MIC_ICON, DV_STEPS,
} from './drive-settings.js?v=4';
import { createVoicesPane } from './voices.js?v=4';

/** tool id → the feature name tools.yaml rows carry (`builtin_feature`). */
export const VOICE_TOOLS = Object.freeze({
  'voice-cloning': 'voice_cloning',
  'voice-builtin': 'voice_builtin',
  'voice-lab': 'voice_lab',
});

/** Line icons for the three tiles (24-unit box, currentColor stroke). */
export const VOICE_TOOL_ICONS = Object.freeze({
  'voice-cloning': MIC_ICON,
  'voice-builtin': ['M4 9.5h3.2L12 5.5v13l-4.8-4H4Z', 'M15.5 9a4.2 4.2 0 0 1 0 6', 'M18.2 6.5a8 8 0 0 1 0 11'],
  'voice-lab': ['M4 11v2', 'M8 7.5v9', 'M12 4.5v15', 'M16 8.5v7', 'M20 10.5v3'],
});

/** Install steps for dispatch-voice (DisPatch's own optional package).
 *  Commands are shell, never translated. The built-in voices plugin's steps
 *  come from the server: a registered provider describes itself, and with
 *  none the server names the plugin it suggests (status plugins.builtin). */
export const INSTALL = Object.freeze({
  cloning: [DV_STEPS.models, DV_STEPS.start],
  cloningModels: [DV_STEPS.models],
});

/** The built-in voices plugin as the status describes it: the registered
 *  provider's own name/licence, else the suggested plugin's. */
export function builtinPlugin(st) {
  const b = (st && st.plugins && st.plugins.builtin) || {};
  const sug = b.suggested || {};
  return {
    name: b.name || sug.name || '',
    licence: b.licence || sug.licence || '',
    install: Array.isArray(sug.install) ? sug.install : [],
    enable: Array.isArray(sug.enable) ? sug.enable : [],
    models: Array.isArray(b.models) && b.models.length ? b.models : [],
  };
}

/** 'ready' | 'models' (package, no models) | 'missing' (no package). */
export function cloningState(st) {
  const p = st && st.plugins && st.plugins.cloning;
  if (!p || !p.installed) return 'missing';
  return p.ready ? 'ready' : 'models';
}

/** 'ready' | 'models' | 'not-enabled' (installed, not in
 *  DISPATCH_VOICE_EXTENSIONS) | 'missing' (not installed). */
export function builtinState(st) {
  const p = st && st.plugins && st.plugins.builtin;
  if (!p) return 'missing';
  if (!p.installed) return p.package ? 'not-enabled' : 'missing';
  return (p.missing || []).length ? 'models' : 'ready';
}

/** "420 ms" / "1.3 s" — units are SI, the same in every language. */
export function fmtMs(ms) {
  if (ms === null || ms === undefined || ms === '') return '–';
  const n = Number(ms);
  if (!Number.isFinite(n) || n < 0) return '–';
  if (n < 1000) return `${Math.round(n)} ms`;
  return `${(n / 1000).toFixed(n < 10000 ? 1 : 0)} s`;
}

/** A tool-shaped group: a title, its card of rows. */
function group(titleKey, rows, { hint = null, id = null } = {}) {
  return el('section', { class: 'dev-group drive-group', id }, [
    el('h3', { class: 'dev-group-title', text: t(titleKey) }),
    hint ? el('p', { class: 'dev-group-hint', text: hint }) : null,
    el('div', { class: 'dev-group-body' }, rows.filter(Boolean)),
  ]);
}

function blockRow(title, hint, children = [], id = null) {
  const r = row(title, hint, [], { id });
  r.classList.add('dev-row-block');
  r.append(...children.filter(Boolean));
  return r;
}

/** "Not installed — how to install": one card, the commands, the restart. */
export function notInstalled(titleKey, hintKey, steps, { missing = null } = {}) {
  const list = Array.isArray(missing) ? missing.map(String) : [];
  return group('vtools.group_install', [blockRow(t(titleKey), t(hintKey), [
    list.length ? el('p', { class: 'drive-missing', role: 'note' }, [
      el('strong', { text: t('drive.missing_title') }), ' ', el('span', { text: listFormat(list) }),
    ]) : null,
    stepsBlock(steps),
    el('p', { class: 'dev-row-hint drive-setup-fix', text: t('drive.setup_restart') }),
  ], 'vtool-install')], { id: 'vtool-install-group' });
}

/** The line that says voice itself is off, with the way to turn it on. */
function voiceOffGroup(st, ctx, hintKey) {
  if (!st || st.enabled) return null;
  const btn = el('button', { type: 'button', class: 'btn-secondary', text: t('vtools.open_voice_settings') });
  btn.addEventListener('click', () => ctx.openVoiceSettings && ctx.openVoiceSettings());
  const forcedOff = st.forced === 'off';
  const r = row(
    t(forcedOff || !st.switch ? 'vtools.voice_off' : 'vtools.voice_not_ready'),
    t(forcedOff ? 'drive.switch_forced_off' : hintKey),
    forcedOff ? [] : [btn], { id: 'vtool-voice-off' },
  );
  r.classList.add('dev-row-action');
  return group('drive.group_status', [r]);
}

// ------------------------------------------------------------------------ //
// Mount / unmount
// ------------------------------------------------------------------------ //

let current = null;     // { id, root, cleanup: [] }

/**
 * Render tool `id` into `root` (the voice tool view's body). Replaces what
 * was there. ctx: { toast(msg, err), confirm, prompt, onChanged(),
 * openVoiceSettings() }.
 */
export async function mountVoiceTool(root, id, ctx = {}) {
  unmountVoiceTool();
  if (!root || !Object.prototype.hasOwnProperty.call(VOICE_TOOLS, id)) return;
  const me = { id, root, cleanup: [] };
  current = me;
  root.dataset.tool = id;
  root.replaceChildren(el('p', { class: 'dev-group-hint vtool-loading', text: t('common.loading') }));
  let st = null;
  try {
    st = await voiceStatus();
  } catch (e) {
    if (current !== me) return;
    root.replaceChildren(group('drive.group_status', [row(t('vtools.load_failed'), String((e && e.message) || ''))]));
    return;
  }
  if (current !== me) return;
  if (id === 'voice-cloning') renderCloning(me, st, ctx);
  else if (id === 'voice-builtin') renderBuiltin(me, st, ctx);
  else await renderLab(me, st, ctx);
}

/** Stop whatever the open tool has running (recording, audio, polls). */
export function unmountVoiceTool() {
  if (!current) return;
  for (const fn of current.cleanup.splice(0)) { try { fn(); } catch { /* gone */ } }
  current = null;
}

/** Which voice tool is mounted, or null. */
export function mountedVoiceTool() { return current ? current.id : null; }

// ------------------------------------------------------------------------ //
// Voice cloning
// ------------------------------------------------------------------------ //

function renderCloning(me, st, ctx) {
  const state = cloningState(st);
  if (state === 'missing') {
    me.root.replaceChildren(notInstalled('vtools.cloning_missing', 'vtools.cloning_missing_hint', INSTALL.cloning));
    return;
  }
  if (state === 'models') {
    me.root.replaceChildren(notInstalled('vtools.cloning_models', 'vtools.cloning_models_hint', INSTALL.cloningModels,
      { missing: (st.plugins.cloning.missing || []) }));
    return;
  }
  const off = voiceOffGroup(st, ctx, 'vtools.cloning_voice_off');
  const host = el('div', { class: 'vtool-voices' });
  me.root.replaceChildren(...[off, host].filter(Boolean));
  const pane = createVoicesPane(host, {
    embedded: true,
    toast: ctx.toast, confirm: ctx.confirm, prompt: ctx.prompt,
    onChanged: () => { if (ctx.onChanged) ctx.onChanged(); },
  });
  pane.open();
  me.cleanup.push(() => pane.close());
}

// ------------------------------------------------------------------------ //
// Built-in voices
// ------------------------------------------------------------------------ //

function renderBuiltin(me, st, ctx) {
  const plug = builtinPlugin(st);
  const about = group('vtools.group_about', [
    row(t('vtools.builtin_about_title'), t('vtools.builtin_about', { name: plug.name, licence: plug.licence })),
  ]);
  const state = builtinState(st);
  if (state === 'missing') {
    me.root.replaceChildren(about, notInstalled('vtools.builtin_missing', 'vtools.builtin_missing_hint', plug.install));
    return;
  }
  if (state === 'not-enabled') {
    me.root.replaceChildren(about, notInstalled('vtools.builtin_not_enabled', 'vtools.builtin_not_enabled_hint', plug.enable));
    return;
  }
  if (state === 'models') {
    me.root.replaceChildren(about, notInstalled('vtools.builtin_models', 'vtools.builtin_models_hint', plug.models,
      { missing: st.plugins.builtin.missing }));
    return;
  }
  const notify = (m) => ctx.toast && ctx.toast(m, true);
  const list = Array.isArray(st.builtin_voices) ? st.builtin_voices : [];
  const canPlay = !!st.enabled;
  const body = [];
  if (!list.length) {
    body.push(row(t('vtools.builtin_empty'), t('vtools.builtin_empty_hint')));
  }
  for (const b of list) {
    const value = builtinValue(b.id);
    const isDefault = st.default_voice === value;
    // ▶ is always the last control, so the column lines up down the list;
    // the default carries its badge beside the name, like the Voices list.
    const controls = [];
    if (!isDefault) {
      const def = el('button', { type: 'button', class: 'btn-secondary', text: t('voices.set_default') });
      def.addEventListener('click', async () => {
        def.disabled = true;
        try {
          await patchVoiceSettings({ default_voice: value });
          if (ctx.toast) ctx.toast(t('voices.default_set'));
          if (ctx.onChanged) ctx.onChanged();
          const fresh = await voiceStatus();
          if (current === me) renderBuiltin(me, fresh, ctx);
        } catch { notify(t('drive.err_save')); def.disabled = false; }
      });
      controls.push(def);
    }
    const play = previewButton(() => value, notify);
    play.disabled = !canPlay;
    if (!canPlay) play.title = t('vtools.preview_needs_voice');
    controls.push(play);
    const r = row(b.name || b.id, '', controls);
    if (isDefault) {
      r.querySelector('.dev-row-text > strong').append(
        el('span', { class: 'pin-toggle pinned voices-default', text: t('voices.default_badge') }));
    }
    r.classList.add('vtool-voice-row');
    r.dataset.voice = value;
    body.push(r);
  }
  const off = voiceOffGroup(st, ctx, 'vtools.builtin_voice_off');
  me.root.replaceChildren(...[about, off,
    group('vtools.group_voices', body, { hint: t('vtools.builtin_list_hint', { n: list.length }) })].filter(Boolean));
}

// ------------------------------------------------------------------------ //
// Voice lab
// ------------------------------------------------------------------------ //

/** A labelled slider row in seconds: name + hint, range, value. */
function secondsRow(title, hint, spec, onSave, id) {
  const input = el('input', {
    type: 'range', class: 'drive-range', min: String(spec.min), max: String(spec.max), step: '0.1',
    'aria-label': title, id: `${id}-input`,
  });
  const out = el('output', { class: 'drive-range-value', for: `${id}-input` });
  const show = () => {
    const v = Number(input.value);
    out.textContent = t('vtools.seconds', { s: v.toFixed(1) });
  };
  input.value = String(spec.value);
  show();
  input.addEventListener('input', show);
  input.addEventListener('change', () => onSave(Math.round(Number(input.value) * 10) / 10));
  const r = row(title, hint, [input, out], { id });
  const d = el('span', { class: 'dev-row-hint vtool-default', text: t('vtools.default_is', { s: Number(spec.default).toFixed(1) }) });
  r.querySelector('.dev-row-text').append(d);
  return r;
}

async function renderLab(me, st, ctx) {
  let lab;
  try {
    lab = await voiceLab();
  } catch (e) {
    if (current !== me) return;
    me.root.replaceChildren(group('drive.group_status', [row(t('vtools.load_failed'), String((e && e.message) || ''))]));
    return;
  }
  if (current !== me) return;
  const notify = (m) => ctx.toast && ctx.toast(m, true);
  const rerender = async () => {
    try {
      const fresh = await voiceStatus();
      if (current === me) await renderLab(me, fresh, ctx);
    } catch { /* keep what is on screen */ }
  };
  const save = async (patch) => {
    try { await patchVoiceSettings(patch); return true; } catch (e) {
      notify(e && e.message ? `${t('drive.err_save')} ${e.message}` : t('drive.err_save'));
      return false;
    }
  };

  // ---- Status
  const voiceHint = lab.forced === 'off' ? t('drive.switch_forced_off')
    : !lab.switch ? t('vtools.lab_voice_off')
      : lab.enabled && lab.ready ? t('vtools.lab_voice_on') : t('vtools.lab_voice_not_ready');
  const statusRow = blockRow(t('vtools.lab_voice'), voiceHint, [], 'vtool-lab-voice');
  const missing = Array.isArray(lab.missing) ? lab.missing : [];
  if (missing.length) {
    statusRow.append(el('p', { class: 'drive-missing', role: 'note' }, [
      el('strong', { text: t('drive.missing_title') }), ' ', el('span', { text: listFormat(missing.map(String)) }),
    ]));
    const steps = setupSteps(missing);
    if (steps.length) statusRow.append(stepsBlock(steps));
  }
  if (!lab.switch && lab.forced !== 'off') {
    const btn = el('button', { type: 'button', class: 'btn-secondary', text: t('vtools.open_voice_settings') });
    btn.addEventListener('click', () => ctx.openVoiceSettings && ctx.openVoiceSettings());
    statusRow.append(el('div', { class: 'voices-actions' }, [btn]));
  }
  let modelsHint = t('vtools.models_not_loaded');
  if (lab.warm === 'done') modelsHint = lab.warm_seconds != null ? t('vtools.models_warm', { s: lab.warm_seconds }) : t('vtools.models_loaded');
  else if (lab.warm === 'running') modelsHint = t('vtools.models_loading');
  else if (lab.loaded) modelsHint = t('vtools.models_loaded');
  let engine = engineLine({ ...st, tts: lab.tts_resolved || lab.tts, smart_turn: lab.smart_turn, stt: lab.stt });
  if (lab.fallback) engine += ` · ${t('vtools.fallback', { name: lab.fallback })}`;
  const status = group('drive.group_status', [
    statusRow,
    row(t('vtools.lab_engine'), engine, [], { id: 'vtool-lab-engine' }),
    row(t('vtools.lab_models'), modelsHint, [], { id: 'vtool-lab-models' }),
  ]);

  // ---- Cloud and rig voices (status only: keys and addresses stay env-only)
  const mm = lab.minimax || {};
  const mmHint = [
    mm.selected ? t('vtools.minimax_in_use') : t('vtools.minimax_not_used'),
    mm.key_set ? t('vtools.key_set') : t('vtools.key_missing'),
  ].join(' · ');
  const hq = lab.hq || {};
  const hqHint = hq.configured
    ? t('vtools.hq_on', { host: hq.host || '?', model: hq.model || '—', s: hq.timeout_s })
    : t('vtools.hq_off');
  const cloud = group('vtools.group_cloud', [
    blockRow(t('vtools.minimax'), mmHint, [
      el('p', { class: 'dev-row-hint', text: t('vtools.minimax_how') }),
      el('pre', { class: 'voice-cmd', dir: 'ltr' }, [el('code', { text: 'DISPATCH_VOICE_TTS=minimax\nMINIMAX_API_KEY=…' })]),
    ], 'vtool-minimax'),
    blockRow(t('vtools.hq'), hqHint, [
      el('p', { class: 'dev-row-hint', text: t('vtools.hq_how') }),
      el('pre', { class: 'voice-cmd', dir: 'ltr' }, [el('code', { text: 'DISPATCH_VOICE_HQ_URL=http://host:port\nDISPATCH_VOICE_HQ_MODEL=…' })]),
    ], 'vtool-hq'),
  ], { hint: t('vtools.cloud_hint') });

  // ---- Turn-taking and thinking (server-wide, applied from the next drive)
  const tm = lab.timings || {};
  const thinking = selectInput(t('drive.thinking'),
    (lab.thinking_levels || THINKING_LEVELS).map((v) => ({
      value: v, label: v === 'low' ? t('drive.thinking_low') : v === 'medium' ? t('drive.thinking_medium') : t('drive.thinking_off'),
    })),
    lab.thinking || 'off', (v) => save({ voice_turn_thinking: v }));
  const reset = el('button', { type: 'button', class: 'btn-secondary', id: 'vtool-reset', text: t('vtools.reset') });
  reset.addEventListener('click', async () => {
    if (await save({ reset: ['merge_window_s', 'hard_silence_s', 'voice_turn_thinking'] })) {
      if (ctx.toast) ctx.toast(t('vtools.reset_done'));
      rerender();
    }
  });
  const resetRow = row(t('vtools.reset_title'), t('vtools.reset_hint'), [reset]);
  resetRow.classList.add('dev-row-action');
  const timing = group('vtools.group_timing', [
    tm.merge_window_s ? secondsRow(t('vtools.merge'), t('vtools.merge_hint'), tm.merge_window_s,
      (v) => save({ merge_window_s: v }), 'vtool-merge') : null,
    tm.hard_silence_s ? secondsRow(t('vtools.hard_silence'), t('vtools.hard_silence_hint'), tm.hard_silence_s,
      (v) => save({ hard_silence_s: v }), 'vtool-hard') : null,
    row(t('drive.thinking'), t('drive.thinking_hint'), [thinking], { id: 'vtool-thinking' }),
    resetRow,
  ], { hint: t('vtools.timing_hint') });

  // ---- This device
  const s = loadDriveSettings(globalThis.localStorage);
  const device = group('drive.group_device', [
    row(t('vtools.show_timings'), t('vtools.show_timings_hint'),
      [switchInput(s.showTimings, t('vtools.show_timings'), (v) => saveDriveSettings(globalThis.localStorage, { showTimings: v }))],
      { id: 'vtool-show-timings' }),
  ]);

  // ---- Quick test
  const test = group('vtools.group_test', [quickTestRow(me, lab, ctx)]);

  me.root.replaceChildren(status, timing, device, test, cloud);
}

/** Record 3 s → transcript + the same words spoken back, with timings. */
function quickTestRow(me, lab, ctx) {
  const ready = !!(lab.enabled && lab.ready);
  const btn = el('button', { type: 'button', class: 'btn-primary', id: 'vtool-test-btn' }, [
    railIcon(MIC_ICON), document.createTextNode(` ${t('vtools.test_record')}`),
  ]);
  btn.disabled = !ready;
  const status = el('p', { class: 'voices-status', role: 'status', 'aria-live': 'polite' });
  const result = el('div', { class: 'vtool-test-result', hidden: '' });
  const r = blockRow(t('vtools.test_title'), ready ? t('vtools.test_hint') : t('vtools.test_needs_voice'),
    [el('div', { class: 'voices-actions' }, [btn]), status, result], 'vtool-test');
  let audio = null;
  let rec = null;
  me.cleanup.push(() => {
    if (rec) rec.cancel();
    if (audio) { try { audio.pause(); } catch { /* gone */ } }
  });
  btn.addEventListener('click', async () => {
    btn.disabled = true;
    result.hidden = true;
    status.classList.remove('err');
    try {
      rec = await startRecording(loadDriveSettings(globalThis.localStorage).micId);
      status.textContent = t('vtools.test_listening');
      const pcm = await rec.done(3000);
      rec = null;
      if (current !== me) return;
      status.textContent = t('vtools.test_working');
      const j = await voiceJson('/api/voice/lab/test', {
        method: 'POST', headers: { 'Content-Type': 'application/octet-stream' }, body: pcm,
      });
      if (current !== me) return;
      status.textContent = '';
      result.hidden = false;
      result.replaceChildren(
        el('p', { class: 'vtool-heard' }, [
          el('strong', { text: t('vtools.test_heard') }), ' ',
          el('span', { dir: 'auto', text: j.transcript ? `“${j.transcript}”` : t('vtools.test_nothing') }),
        ]),
        el('div', { class: 'voices-chips vtool-timings' }, [
          j.load_ms >= 50 ? el('span', { class: 'drive-chip', text: t('vtools.t_load', { t: fmtMs(j.load_ms) }) }) : null,
          el('span', { class: 'drive-chip', text: t('vtools.t_stt', { t: fmtMs(j.stt_ms) }) }),
          el('span', { class: 'drive-chip', text: t('vtools.t_first', { t: fmtMs(j.tts_first_ms) }) }),
          el('span', { class: 'drive-chip', text: t('vtools.t_total', { t: fmtMs(j.tts_total_ms) }) }),
        ].filter(Boolean)),
      );
      if (j.audio_wav_b64) {
        const bytes = Uint8Array.from(atob(j.audio_wav_b64), (c) => c.charCodeAt(0));
        const url = URL.createObjectURL(new Blob([bytes], { type: 'audio/wav' }));
        audio = new Audio(url);
        audio.onended = () => URL.revokeObjectURL(url);
        audio.play().catch(() => {});
      }
    } catch (e) {
      if (rec) { rec.cancel(); rec = null; }
      status.classList.add('err');
      status.textContent = e && e.name === 'NotAllowedError' ? t('drive.err_mic_denied')
        : (e && e.message) ? t('vtools.test_failed', { error: e.message }) : t('vtools.test_failed', { error: '' });
    } finally {
      if (current === me) btn.disabled = false;
    }
  });
  return r;
}

/**
 * Open the mic through Drive's own capture worklet (16 kHz mono PCM16, the
 * format the server speaks) and collect frames. → { done(ms) → Uint8Array,
 * cancel() }. Never connected to the speakers.
 */
async function startRecording(micId) {
  const audio = { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true };
  if (micId) audio.deviceId = { ideal: micId };
  const stream = await navigator.mediaDevices.getUserMedia({ audio });
  const ac = new AudioContext();
  const frames = [];
  let node = null;
  let src = null;
  const stop = () => {
    try { stream.getTracks().forEach((tr) => tr.stop()); } catch { /* gone */ }
    try { src && src.disconnect(); } catch { /* gone */ }
    try { ac.close(); } catch { /* gone */ }
  };
  try {
    await ac.audioWorklet.addModule('/static/voice-worklet.js?v=2');
    node = new AudioWorkletNode(ac, 'drive-mic');
    node.port.onmessage = (e) => { if (e.data instanceof ArrayBuffer) frames.push(e.data); };
    node.port.postMessage({ enabled: true });
    src = ac.createMediaStreamSource(stream);
    src.connect(node);
    if (ac.state === 'suspended') await ac.resume();
  } catch (e) { stop(); throw e; }
  let timer = null;
  let cancelled = false;
  return {
    done(ms) {
      return new Promise((resolve, reject) => {
        timer = setTimeout(() => {
          stop();
          if (cancelled) { reject(new Error('cancelled')); return; }
          const total = frames.reduce((n, b) => n + b.byteLength, 0);
          const out = new Uint8Array(total);
          let o = 0;
          for (const b of frames) { out.set(new Uint8Array(b), o); o += b.byteLength; }
          resolve(out);
        }, ms);
      });
    },
    cancel() { cancelled = true; clearTimeout(timer); stop(); },
  };
}
