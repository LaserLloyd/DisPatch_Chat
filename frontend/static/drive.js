// Drive mode — hands-free voice for the open thread (unlocked tier only).
//
// One full-screen view built around the bot's own avatar in a state ring:
// the ring's colour, shape and motion say what is happening, a word under it
// names the state, and a hint line says what a tap on the picture will do.
// Errors are written ON the overlay (a notice strip) and played as a tone,
// never only as a toast. Two 56 px road buttons at the bottom: Pause/Resume
// and End drive. The gear opens a quick sheet of the settings a driver
// changes on the road (js/drive-settings.js).
//
// The browser only moves audio. Mic → AudioWorklet (16 kHz PCM16) →
// /ws/voice/<thread>; the server does turn detection, speech-to-text, sends
// the words through the thread's NORMAL send path, and streams the reply back
// as PCM16 chunks that are scheduled gap-free here. Protocol: the Drive mode
// v2 contract in docs/voice-drive-mode.md.
//
// Keep-alive: Screen Wake Lock and a Media Session (a silent looping <audio>
// started inside the tap, so iOS treats the page as playing and steering-wheel
// play/pause reaches it). Neither keeps the mic alive with the screen LOCKED:
// a hidden page loses the mic, so Drive pauses itself on hide and says so on
// return. That is a platform limit; Drive mode is a foreground app.
//
// Pure helpers are exported for frontend/tests/drive.test.js; the DOM half
// only runs inside openDrive().

import { t } from './js/i18n.js?v=3';
import { el, railIcon, RAIL_ICONS, acquireInert, releaseInert } from './js/util.js?v=20';
import {
  PATIENCE_PRESETS, loadDriveSettings, onDriveSettings, startPayload,
  buildQuickSheet, MIC_ICON,
} from './js/drive-settings.js?v=4';

export { PATIENCE_PRESETS };

// ---------------------------------------------------------------- pure ----

/** Reconnect delay: 0.5 s doubling to 8 s, so a tunnel costs seconds, not minutes. */
export function nextBackoff(prev) {
  return Math.min(8000, prev ? prev * 2 : 500);
}

/** How long a dropped connection keeps retrying before Drive stops and asks. */
export const GIVE_UP_MS = 60000;
/** Heartbeat: ping every 10 s; no pong for 25 s means the socket is dead. */
export const PING_EVERY_MS = 10000;
export const PONG_TIMEOUT_MS = 25000;
/** Mic audio kept while the server is still loading (20 ms frames → 10 s). */
export const PREBUFFER_FRAMES = 500;
/** How long Undo is offered after a transcript (contract: 8 s). */
export const UNDO_MS = 8000;

/**
 * What to do after the voice socket closed (contract + amendment B2):
 *   1008  refused (locked / not allowed)      → stop, say why
 *   1013  voice unavailable on the server     → stop, say why (no retry loop);
 *         the reason "Voice is turned off …" means the switch is off
 *   4001  replaced by a newer socket          → close quietly
 *   1011  a server-side worker died           → retry with backoff (a fresh
 *         session is fine; the driver is told what happened meanwhile)
 *   1001  heartbeat lost / else the network   → retry with backoff
 */
export function reconnectDecision(code, why = '') {
  if (code === 1008) return { retry: false, reason: 'refused' };
  // The server names the switch when voice is simply turned off (app/voice/
  // routes.py OFF_SETTINGS / OFF_SERVER); the driver gets its own words.
  if (code === 1013 && /^Voice is turned off/i.test(String(why || ''))) return { retry: false, reason: 'voice_off' };
  if (code === 1013) return { retry: false, reason: 'unavailable' };
  if (code === 4001) return { retry: false, reason: 'replaced' };
  if (code === 1011) return { retry: true, reason: 'internal' };
  return { retry: true, reason: 'network' };
}

/** "Heard in 420 ms · Reply started in 1.6 s" from the last `metrics`
 *  frames ({heard, first} in ms, either may be null); '' when nothing yet. */
export function timingsLine(times) {
  const fmt = (ms) => (ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(1)} s`);
  const parts = [];
  if (times && typeof times.heard === 'number') parts.push(t('drive.timing_heard', { t: fmt(times.heard) }));
  if (times && typeof times.first === 'number') parts.push(t('drive.timing_first', { t: fmt(times.first) }));
  return parts.join(' · ');
}

/** ws(s)://host/ws/voice/<thread> for the page's own origin. */
export function voiceUrl(loc, threadId, device = '') {
  const proto = loc.protocol === 'https:' ? 'wss' : 'ws';
  const q = device ? `?d=${encodeURIComponent(device)}` : '';
  return `${proto}://${loc.host}/ws/voice/${encodeURIComponent(threadId)}${q}`;
}

export const DEVICE_KEY = 'dispatch-drive-device';

/**
 * This browser's Drive device id: random, kept in localStorage. On an install
 * WITHOUT a PIN there is no session to tell devices apart, so the server
 * counts voice sockets per device id instead (a newer socket from the same
 * device replaces its older one; another device never does). Not a secret
 * and not an identity: with a PIN set the server ignores it.
 */
export function driveDeviceId(storage = globalThis.localStorage) {
  try {
    let id = storage && storage.getItem(DEVICE_KEY);
    if (id && /^[A-Za-z0-9-]{8,64}$/.test(id)) return id;
    const c = globalThis.crypto;
    id = c && c.randomUUID ? c.randomUUID() : `d${Date.now().toString(36)}${Math.random().toString(36).slice(2, 12)}`;
    if (storage) storage.setItem(DEVICE_KEY, id);
    return id;
  } catch { return ''; }
}

/** Can Drive mode open for this thread at all? (The server re-checks.) */
export function driveAllowed({ locked, thread, bot, secure }) {
  const no = (whyKey) => ({ ok: false, whyKey, why: t(whyKey) });
  if (locked) return no('drive.err_locked_device');
  if (!thread) return no('drive.err_no_chat');
  if (bot && bot.safe) return no('drive.err_safe_bot');
  if (!secure) return no('drive.err_https');
  return { ok: true, whyKey: '', why: '' };
}

/**
 * What a tap on the picture does in each state. The hint line under the
 * state word always says this out loud, so no mode is a silent surprise.
 * `playing` = a REPLY is playing (not an ack or a spoken notice: a tap while
 * the bot is still thinking must cancel the turn, not just hush the "Mm-hm").
 */
export function tapAction(state, { playing = false } = {}) {
  if (playing || state === 'speaking') return 'interrupt';
  switch (state) {
    case 'listening': case 'hearing': return 'pause';
    case 'paused': return 'resume';
    case 'thinking': case 'transcribing': return 'cancel';
    case 'stopped': return 'retry';
    default: return 'none';
  }
}

/** Thinking feedback: a plain word, then a counter at 4 s, then "still working" at 25 s. */
export function thinkingPhase(ms) {
  if (ms >= 25000) return 'long';
  if (ms >= 4000) return 'counting';
  return 'plain';
}

/** The hint-line key for a state (null = no hint). */
export function hintKey(state, { playing = false, thinkingMs = 0, autoListen = true } = {}) {
  switch (tapAction(state, { playing })) {
    case 'interrupt': return 'drive.hint_interrupt';
    case 'pause': return state === 'hearing' ? 'drive.hint_keep_talking' : (autoListen ? 'drive.hint_pause' : 'drive.hint_pause_ptt');
    case 'resume': return 'drive.hint_resume';
    case 'cancel': return thinkingPhase(thinkingMs) === 'long' ? 'drive.hint_still_working' : 'drive.hint_cancel';
    case 'retry': return null;
    default: break;
  }
  if (state === 'connecting') return 'drive.hint_wait_tone';
  if (state === 'offline') return 'drive.hint_offline';
  return null;
}

/** Int16 PCM (ArrayBuffer, little-endian) → Float32 in [-1, 1). */
export function pcm16ToFloat(buf) {
  const i16 = new Int16Array(buf);
  const out = new Float32Array(i16.length);
  for (let i = 0; i < i16.length; i++) out[i] = i16[i] / 32768;
  return out;
}

/**
 * Pairs the server's JSON `audio` header with the binary frame that follows
 * it. Returns {header, pcm} when a pair completes, null otherwise. A binary
 * frame with no header (should never happen) is dropped, not guessed at.
 */
export function createAudioPairer() {
  let pending = null;
  return {
    header(h) { pending = h; },
    binary(buf) {
      if (!pending) return null;
      const h = pending; pending = null;
      return { header: h, pcm: buf };
    },
    reset() { pending = null; },
  };
}

/**
 * Gap-free scheduler over a Web Audio context. `ctx` needs currentTime,
 * createBuffer, createBufferSource, destination — a fake works in tests.
 * `out` (optional) is the node chunks connect to — an analyser in the app,
 * so the ring can follow the reply's loudness. Chunks from an older `gen`
 * (before a barge-in or a tap-to-stop) are dropped.
 */
export function createPlayer(ctx, { onIdle, out } = {}) {
  let nextAt = 0;
  let gen = 0;
  let seq = null;
  let lastStart = 0;
  const live = new Set();
  const player = {
    get gen() { return gen; },
    get playing() { return live.size > 0; },
    /** When the most recently scheduled chunk starts (ctx time). */
    get lastStart() { return lastStart; },
    /** The server `seq` of the most recently scheduled chunk (null before any). */
    get seq() { return seq; },
    /**
     * `chunkSeq` is the server's audio seq, reported back with `playback_done`
     * (amendment B6): a drain after an ack or a notice then carries a seq older
     * than the reply's, and the server never takes it for the end of the reply.
     */
    play(floatPcm, sampleRate, chunkGen, chunkSeq = null) {
      if (chunkGen < gen) return false;
      gen = chunkGen;
      if (typeof chunkSeq === 'number') seq = chunkSeq;
      const buf = ctx.createBuffer(1, floatPcm.length, sampleRate);
      buf.getChannelData(0).set(floatPcm);
      const src = ctx.createBufferSource();
      src.buffer = buf;
      src.connect(out || ctx.destination);
      const start = Math.max(ctx.currentTime + 0.02, nextAt);
      nextAt = start + floatPcm.length / sampleRate;
      lastStart = start;
      live.add(src);
      src.onended = () => {
        live.delete(src);
        if (!live.size && onIdle) onIdle(gen, seq);
      };
      src.start(start);
      return true;
    },
    /** Barge-in: silence everything now and ignore chunks older than newGen. */
    stop(newGen) {
      if (typeof newGen === 'number') gen = Math.max(gen, newGen);
      for (const s of live) { try { s.onended = null; s.stop(); } catch { /* already ended */ } }
      live.clear();
      nextAt = 0;
    },
    /**
     * A tap-to-stop: silence now AND drop the chunks of this reply that are
     * still on the wire (they carry the current gen). The server answers
     * `stop_playback` with `barge_in {gen+1}`, so the next reply plays.
     */
    drop() { player.stop(gen + 1); },
    /** A new socket starts its own gen count at 0. */
    resetGen() { player.stop(); gen = 0; },
  };
  return player;
}

/** Short earcons (~150–250 ms). `ready` rises, `err` falls. */
export const EARCONS = {
  eot: [660, 880],
  ack: [520, 520],
  ready: [523, 659, 784],
  err: [440, 330],
};

export function playEarcon(ctx, kind = 'eot') {
  const tones = EARCONS[kind] || EARCONS.err;
  const t0 = ctx.currentTime + 0.01;
  tones.forEach((f, i) => {
    const o = ctx.createOscillator();
    const g = ctx.createGain();
    o.frequency.value = f;
    g.gain.setValueAtTime(0.0001, t0 + i * 0.075);
    g.gain.exponentialRampToValueAtTime(0.25, t0 + i * 0.075 + 0.01);
    g.gain.exponentialRampToValueAtTime(0.0001, t0 + i * 0.075 + 0.07);
    o.connect(g); g.connect(ctx.destination);
    o.start(t0 + i * 0.075); o.stop(t0 + i * 0.075 + 0.08);
  });
}

/**
 * Mic audio captured before the server said `ready`. The first words after
 * the tap used to be lost (the socket opens ~3 s after the mic); now they
 * wait here and are sent the moment the server can hear. Oldest frames go
 * first when the cap is hit.
 */
export function createMicBuffer(max = PREBUFFER_FRAMES) {
  let frames = [];
  return {
    push(buf) { frames.push(buf); if (frames.length > max) frames.splice(0, frames.length - max); },
    flush() { const out = frames; frames = []; return out; },
    clear() { frames = []; },
    get size() { return frames.length; },
  };
}

/**
 * Undo is offered for UNDO_MS after a transcript lands. `now` is injectable
 * so the window is testable without waiting.
 */
export function createUndoWindow(ms = UNDO_MS, now = () => Date.now()) {
  let openedAt = null;
  return {
    open() { openedAt = now(); },
    close() { openedAt = null; },
    canUndo() { return openedAt != null && now() - openedAt < ms; },
    remaining() { return openedAt == null ? 0 : Math.max(0, Math.ceil((ms - (now() - openedAt)) / 1000)); },
  };
}

/**
 * Heartbeat (functional review H2): a half-open socket used to sit on
 * "Listening" forever. ping every PING_EVERY_MS; `check()` reports dead when
 * nothing came back for PONG_TIMEOUT_MS. Timers are injected for tests.
 */
export function createHeartbeat({ send, onDead, now = () => Date.now(), every = PING_EVERY_MS, timeout = PONG_TIMEOUT_MS,
  setInt = globalThis.setInterval, clearInt = globalThis.clearInterval } = {}) {
  let last = 0;
  let timer = null;
  const hb = {
    start() { last = now(); hb.stop(); timer = setInt(() => hb.tick(), every); },
    stop() { if (timer != null) clearInt(timer); timer = null; },
    alive() { last = now(); },
    dead() { return now() - last >= timeout; },
    tick() {
      if (hb.dead()) { hb.stop(); onDead && onDead(); return; }
      send && send({ type: 'ping', t: now() });
    },
  };
  return hb;
}

/**
 * The replies the driver has heard, newest first. A streamed reply's audio
 * carries its PROVISIONAL id until the stream completes (main.py
 * stream_chunk ids are not DB ids), and the server cannot resume after an id
 * it has never stored (functional review L4). So the reconnect `start` sends
 * the newest id as `after` and the recent ones as `after_ids`; the server
 * resumes after the newest one it knows.
 */
export function createHeardTracker(max = 8) {
  let ids = [];
  return {
    note(id) {
      if (!id) return;
      if (ids[0] === id) return;
      ids = [id, ...ids.filter((x) => x !== id)].slice(0, max);
    },
    get after() { return ids[0] || null; },
    get ids() { return ids.slice(); },
  };
}

/** Plain words for a `turn_error` reason (contract). */
export function turnErrorKey(reason) {
  switch (reason) {
    case 'no_speech_in_reply': return 'drive.err_nothing_to_say';
    case 'timeout': return 'drive.err_timeout';
    case 'tts_failed': return 'drive.err_tts';
    default: return 'drive.err_agent';
  }
}

/**
 * Plain words for an `error` frame (null = nothing to show). New servers send
 * a `code`; a 2.1.0 server sends only an English `message`, matched here so
 * its errors still read well. No error frame ends the session by itself: when
 * the server cannot go on it CLOSES the socket (1011 after `unavailable`),
 * and the close code decides what happens next (reconnectDecision).
 */
export function errorFrameKey(m) {
  const code = m && m.code;
  const msg = String((m && m.message) || '');
  if (code === 'undo_expired') return 'drive.err_undo_expired';
  if (code === 'stt_failed' || /recognition/i.test(msg)) return 'drive.err_stt';
  if (code === 'rate_limited') return 'drive.err_rate';
  if (code === 'unavailable' || /internal error/i.test(msg)) return 'drive.err_internal_retry';
  if (code === 'bad_frame') return null;
  if (/synthesis/i.test(msg)) return 'drive.err_tts';
  if (/could not send/i.test(msg)) return 'drive.err_send';
  return 'drive.err_generic';
}

// ----------------------------------------------------------------- DOM ----

let active = null;    // the one open Drive session
let opening = false;  // set synchronously by openDrive: a double tap opens one

/**
 * Wire the header button. `ctx` supplies the app's view of the world:
 *   getThread() -> {id, bot_id, title} | null, getBot(id) -> bot | null,
 *   isLocked() -> bool, toast(msg, isError), getAvatar(thread, bot) ->
 *   {src, letter}, voiceStatus() -> /api/voice/status | null,
 *   openDriveSettings() -> Settings → Voice.
 * Call refresh() whenever the open thread or the lock state changes.
 */
export function initDrive(button, ctx) {
  if (!button) return { refresh() {} };
  const verdict = () => {
    const thread = ctx.getThread();
    return {
      thread,
      v: driveAllowed({
        locked: ctx.isLocked(), thread, bot: thread && ctx.getBot(thread.bot_id),
        secure: !!globalThis.isSecureContext,
      }),
    };
  };
  const refresh = () => {
    const { thread, v } = verdict();
    // Hidden when locked or for a safe bot; shown-but-explaining on http.
    button.hidden = ctx.isLocked() || !thread || !!(thread && ctx.getBot(thread.bot_id)?.safe);
    button.title = v.ok ? t('chat.drive_aria') : v.why;
    button.dataset.ready = v.ok ? '1' : '0';
  };
  button.addEventListener('click', () => {
    const { thread, v } = verdict();
    if (!v.ok) { ctx.toast(v.why, true); return; }
    openDrive(thread, { ...ctx, returnFocus: button });
  });
  refresh();
  return { refresh, close: () => active && active.close() };
}

let silentUrl = null;
function silentWavUrl() {
  if (silentUrl) return silentUrl;
  // 1 s of 8 kHz 8-bit silence.
  const n = 8000;
  const b = new Uint8Array(44 + n);
  const dv = new DataView(b.buffer);
  const w = (o, s) => { for (let i = 0; i < s.length; i++) b[o + i] = s.charCodeAt(i); };
  w(0, 'RIFF'); dv.setUint32(4, 36 + n, true); w(8, 'WAVE'); w(12, 'fmt ');
  dv.setUint32(16, 16, true); dv.setUint16(20, 1, true); dv.setUint16(22, 1, true);
  dv.setUint32(24, 8000, true); dv.setUint32(28, 8000, true); dv.setUint16(32, 1, true);
  dv.setUint16(34, 8, true); w(36, 'data'); dv.setUint32(40, n, true);
  b.fill(128, 44);
  silentUrl = URL.createObjectURL(new Blob([b], { type: 'audio/wav' }));
  return silentUrl;
}

const PAUSE_ICON = ['M9 5.5v13', 'M15 5.5v13'];
const reducedMotion = () => !!(globalThis.matchMedia && globalThis.matchMedia('(prefers-reduced-motion: reduce)').matches);

/**
 * Open the Drive view. MUST be called from inside the tap's click handler:
 * the silent keep-alive <audio> and the AudioContext are started before the
 * first await, while the browser still counts this as a user gesture (iOS
 * refuses both otherwise, and the Media Session never activates).
 */
export function openDrive(thread, ctx) {
  if (active) return active;
  if (opening) return null;
  opening = true;

  const bot = ctx.getBot(thread.bot_id);
  let settings = loadDriveSettings(globalThis.localStorage);

  // ---- inside the gesture: audio session, keep-alive, context ----------
  try { if (navigator.audioSession) navigator.audioSession.type = 'play-and-record'; } catch { /* Safari 17+ only */ }
  let keepAlive = null;
  try {
    keepAlive = new Audio(silentWavUrl());
    keepAlive.loop = true;
    keepAlive.volume = 0.0001;
    keepAlive.play().catch(() => {});
  } catch { keepAlive = null; }
  const AC = globalThis.AudioContext || globalThis.webkitAudioContext;
  const ac = new AC();
  ac.resume().catch(() => {});
  if (settings.sinkId && typeof ac.setSinkId === 'function') ac.setSinkId(settings.sinkId).catch(() => {});
  // Reply audio → duck gain (lowered while the server checks whether the
  // driver is talking over it) → analyser (the ring's speaking level) → out.
  let analyser = null;
  let duckGain = null;
  try {
    duckGain = ac.createGain();
    analyser = ac.createAnalyser();
    analyser.fftSize = 512;
    duckGain.connect(analyser);
    analyser.connect(ac.destination);
  } catch { analyser = null; duckGain = null; }

  // ---- DOM --------------------------------------------------------------
  const avatar = (ctx.getAvatar && ctx.getAvatar(thread, bot)) || {};
  const root = el('div', {
    class: 'drive-overlay', role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': 'drive-name',
    dataset: { state: 'connecting' },
  });
  const name = el('div', { class: 'drive-name', id: 'drive-name', dir: 'auto', text: bot ? bot.name : t('drive.title') });
  const sub = el('div', { class: 'drive-sub', dir: 'auto', text: (thread.title || '').trim() });
  const gear = el('button', {
    type: 'button', class: 'icon-btn ghost drive-gear', title: t('drive.sheet_title'),
    'aria-label': t('drive.sheet_title'), 'aria-haspopup': 'dialog',
  }, [railIcon(RAIL_ICONS.gear)]);
  const head = el('header', { class: 'drive-head' }, [
    el('div', { class: 'drive-id' }, [name, sub.textContent ? sub : null]), gear,
  ]);

  const face = avatar.src
    ? el('img', { class: 'drive-face', src: avatar.src, alt: '' })
    : el('span', { class: 'drive-face drive-letter', 'aria-hidden': 'true', text: avatar.letter || (bot && bot.name ? bot.name[0] : '•') });
  if (face.tagName === 'IMG') {
    face.addEventListener('error', () => {
      face.replaceWith(el('span', { class: 'drive-face drive-letter', 'aria-hidden': 'true', text: avatar.letter || (bot && bot.name ? bot.name[0] : '•') }));
    }, { once: true });
  }
  const ring = el('button', { type: 'button', class: 'drive-ring' }, [face, el('span', { class: 'drive-dot', 'aria-hidden': 'true' })]);
  const stateWord = el('div', { class: 'drive-state', 'aria-hidden': 'true' });
  const hint = el('div', { class: 'drive-hint', 'aria-hidden': 'true' });
  const notice = el('div', { class: 'drive-notice', role: 'alert', hidden: '' });
  // Voice lab's "show timings": the server's `metrics` frames, in plain words.
  const timings = el('div', { class: 'drive-timings', 'aria-hidden': 'true', hidden: '' });
  const hero = el('div', { class: 'drive-hero' }, [ring, stateWord, hint, notice, timings]);

  const you = el('p', { class: 'drive-you', dir: 'auto' });
  const them = el('p', { class: 'drive-them', dir: 'auto' });
  const undoBtn = el('button', { type: 'button', class: 'btn-secondary drive-undo', hidden: '' });
  const talk = el('section', { class: 'drive-talk', 'aria-label': t('drive.transcript') }, [you, them, undoBtn]);

  const pauseBtn = el('button', { type: 'button', class: 'btn-secondary drive-road drive-pause' });
  const endBtn = el('button', { type: 'button', class: 'btn-primary drive-road drive-end' });
  const bar = el('div', { class: 'drive-bar' }, [pauseBtn, endBtn]);
  const status = el('div', { class: 'sr-only', role: 'status', 'aria-live': 'polite' });

  const col = el('div', { class: 'drive-col' }, [head, hero, talk, bar]);
  root.append(col, status);
  document.body.append(root);
  document.body.classList.add('drive-open');
  const inertToken = acquireInert();

  // ---- state -------------------------------------------------------------
  const player = createPlayer(ac, {
    out: duckGain || undefined,
    onIdle: (gen, seq) => {
      const done = { type: 'playback_done', gen };
      if (typeof seq === 'number') done.seq = seq;
      send(done);
      if (!closed) paint();
    },
  });
  const pairer = createAudioPairer();
  const micBuf = createMicBuffer();
  const undo = createUndoWindow();
  const heard = createHeardTracker();
  let stream = null;
  let node = null;
  let ws = null;
  let ready = false;
  let everReady = false;
  let backoff = 0;
  let reconnectTimer = null;
  let offlineSince = 0;
  let offlineTicker = null;
  let closed = false;
  let listening = true;          // the driver wants the mic on
  let pausedByHide = false;
  let wakeLock = null;
  let state = 'connecting';
  let stopReason = null;
  let thinkingAt = 0;
  let thinkingTicker = null;
  let undoTicker = null;
  let noticeTimer = null;
  let wasSpeaking = false;
  let level = 0;
  let raf = 0;
  let sheet = null;
  let pushedHistory = false;
  let poppedBySystem = false;
  let playingKind = null;        // kind of the audio last scheduled (reply | ack | notice)
  const sentences = new Map();   // seq → <span> in the reply line
  const replyPlaying = () => player.playing && playingKind === 'reply';
  const hb = createHeartbeat({ send: (f) => send(f), onDead: () => dropSocket() });

  const can = () => !closed;

  function send(obj) {
    if (ws && ws.readyState === 1) ws.send(JSON.stringify(obj));
  }
  function sendStart() {
    send(startPayload(settings, heard.after, heard.ids));
  }
  function earcon(kind) {
    if (settings.sounds) { try { playEarcon(ac, kind); } catch { /* closed context */ } }
  }
  function announce(text) {
    status.textContent = '';
    if (text) setTimeout(() => { status.textContent = text; }, 30);
  }

  // ---- notice strip ------------------------------------------------------
  function showNotice(text, { kind = 'error', ms = 0 } = {}) {
    clearTimeout(noticeTimer);
    if (!text) { notice.hidden = true; notice.textContent = ''; return; }
    notice.dataset.kind = kind;
    notice.textContent = text;
    notice.hidden = false;
    if (ms) noticeTimer = setTimeout(() => { notice.hidden = true; }, ms);
  }

  // ---- state painting ----------------------------------------------------
  function paint() {
    root.dataset.state = state;
    let word;
    if (state === 'thinking') {
      const ms = Date.now() - thinkingAt;
      word = thinkingPhase(ms) === 'plain' ? t('drive.state_thinking')
        : t('drive.state_thinking_elapsed', { s: Math.floor(ms / 1000) });
    } else if (state === 'offline') {
      word = t('drive.state_offline');
    } else if (state === 'stopped') {
      word = t('drive.state_stopped');
    } else {
      word = t(`drive.state_${state}`);
    }
    stateWord.textContent = word;
    const hk = hintKey(state, { playing: replyPlaying(), thinkingMs: state === 'thinking' ? Date.now() - thinkingAt : 0, autoListen: settings.autoListen });
    let hintText = hk ? t(hk) : '';
    if (state === 'offline') {
      const left = reconnectTimer ? Math.max(1, Math.ceil(backoffLeft() / 1000)) : 0;
      hintText = left ? t('drive.hint_offline', { s: left }) : t('drive.hint_reconnecting');
    }
    hint.textContent = hintText;
    ring.setAttribute('aria-label', hintText ? `${word}. ${hintText}` : word);
    ring.disabled = tapAction(state, { playing: replyPlaying() }) === 'none';
    paintBar();
  }
  let atScheduled = 0;
  function backoffLeft() { return atScheduled - Date.now(); }

  function paintBar() {
    if (state === 'stopped') {
      pauseBtn.replaceChildren(document.createTextNode(t('drive.try_again')));
      pauseBtn.hidden = stopReason === 'refused';
      endBtn.replaceChildren(document.createTextNode(t('drive.back_to_chat')));
      return;
    }
    pauseBtn.hidden = false;
    pauseBtn.replaceChildren(listening ? railIcon(PAUSE_ICON) : railIcon(MIC_ICON),
      document.createTextNode(` ${listening ? t('drive.pause') : t('drive.resume')}`));
    pauseBtn.setAttribute('aria-pressed', String(!listening));
    endBtn.replaceChildren(document.createTextNode(t('drive.end')));
  }

  const ANNOUNCE = new Set(['listening', 'paused', 'offline', 'stopped', 'locked']);
  function setState(s) {
    const prev = state;
    state = s;
    if (s === 'thinking' && prev !== 'thinking') {
      thinkingAt = Date.now();
      clearInterval(thinkingTicker);
      thinkingTicker = setInterval(() => { if (state === 'thinking') paint(); }, 1000);
    } else if (s !== 'thinking') {
      clearInterval(thinkingTicker); thinkingTicker = null;
    }
    if (s !== 'offline') { clearInterval(offlineTicker); offlineTicker = null; }
    paint();
    if (prev !== s && ANNOUNCE.has(s)) announce(stateWord.textContent);
    // Push-to-talk: a finished reply leaves the mic paused.
    if (prev === 'speaking' && s === 'listening' && !settings.autoListen && listening) setListening(false);
    wasSpeaking = s === 'speaking';
  }

  function stop(reason) {
    stopReason = reason;
    hb.stop();
    clearTimeout(reconnectTimer); reconnectTimer = null;
    // Whatever stopped us, the socket is finished: detach it so its close
    // event cannot turn "stopped" back into "retrying".
    if (ws) { const old = ws; ws = null; old.onclose = null; old.onmessage = null; try { old.close(); } catch { /* gone */ } }
    ready = false;
    // A stopped drive must not keep the microphone open (the browser's mic
    // indicator would say it is still listening). Try again reopens it.
    stopMic();
    const msg = {
      refused: t('drive.err_refused'),
      unavailable: t('drive.err_unavailable'),
      voice_off: t('drive.err_voice_off'),
      internal: t('drive.err_internal'),
      network: t('drive.err_network'),
      mic_denied: t('drive.err_mic_denied'),
      mic_missing: t('drive.err_mic_missing'),
      https: t('drive.err_https'),
    }[reason] || t('drive.err_internal');
    player.stop();
    earcon('err');
    setState('stopped');
    showNotice(msg);
  }

  // ---- timings (Voice lab → "Show timings on the Drive screen") ----------
  const lastTimes = { heard: null, first: null };
  function paintTimings(m) {
    if (typeof m.eot_to_transcript_ms === 'number') { lastTimes.heard = m.eot_to_transcript_ms; lastTimes.first = null; }
    if (typeof m.eot_to_first_audio_ms === 'number') lastTimes.first = m.eot_to_first_audio_ms;
    const line = timingsLine(lastTimes);
    timings.textContent = line;
    timings.hidden = !settings.showTimings || !line;
  }

  // ---- duck: the driver may be talking over the reply --------------------
  // The server lowers the reply while it checks the words (H3 soft barge-in);
  // `duck {on:false}` = it was our own echo, carry on at full volume.
  function setDuck(on) {
    if (!duckGain) return;
    try {
      const g = duckGain.gain;
      g.cancelScheduledValues(ac.currentTime);
      g.setTargetAtTime(on ? 0.15 : 1, ac.currentTime, 0.04);
    } catch { /* closed context */ }
  }

  // ---- ring level (the one moving thing) --------------------------------
  function setLevel(v) {
    level = Math.max(0, Math.min(1, v));
    ring.style.setProperty('--lvl', level.toFixed(3));
  }
  const buf = analyser ? new Float32Array(analyser.fftSize) : null;
  function loop() {
    raf = 0;
    if (closed) return;
    if (analyser && player.playing && !reducedMotion()) {
      analyser.getFloatTimeDomainData(buf);
      let sum = 0;
      for (let i = 0; i < buf.length; i++) sum += buf[i] * buf[i];
      setLevel(Math.min(1, Math.sqrt(sum / buf.length) * 4));
      raf = requestAnimationFrame(loop);
    } else if (state === 'speaking') {
      setLevel(0);
    }
  }
  function kickLevelLoop() { if (!raf && analyser) raf = requestAnimationFrame(loop); }

  // ---- mic ---------------------------------------------------------------
  async function startMic() {
    const audio = { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true };
    if (settings.micId) audio.deviceId = { exact: settings.micId };
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio });
    } catch (err) {
      // A remembered mic that is gone (the car is not connected today) falls
      // back to the default instead of failing the drive.
      if (settings.micId && err && (err.name === 'OverconstrainedError' || err.name === 'NotFoundError')) {
        delete audio.deviceId;
        stream = await navigator.mediaDevices.getUserMedia({ audio });
      } else {
        throw err;
      }
    }
    if (closed || state === 'stopped') { stream.getTracks().forEach((tr) => tr.stop()); stream = null; return; }
    if (!node) {
      await ac.audioWorklet.addModule('/static/voice-worklet.js?v=2');
      if (closed) return;
      node = new AudioWorkletNode(ac, 'drive-mic');
      node.port.onmessage = (e) => {
        const d = e.data;
        if (d instanceof ArrayBuffer) onMicFrame(d);
        else if (d && typeof d.level === 'number' && (state === 'hearing' || state === 'listening') && !reducedMotion()) setLevel(Math.min(1, d.level * 6));
      };
    }
    const srcNode = ac.createMediaStreamSource(stream);
    srcNode.connect(node);
    node._src = srcNode;
    node.port.postMessage({ enabled: listening });
    // Not connected to the destination: the mic must never play back.
  }
  function stopMic() {
    try { stream && stream.getTracks().forEach((tr) => tr.stop()); } catch { /* gone */ }
    try { node && node._src && node._src.disconnect(); } catch { /* gone */ }
    stream = null;
  }
  function onMicFrame(buf) {
    if (!listening) return;
    if (ready && ws && ws.readyState === 1) { ws.send(buf); return; }
    // Before the first `ready`: keep the driver's first words (H5). After a
    // drop, old speech is stale — do not replay it into a new socket.
    if (!everReady) micBuf.push(buf);
  }
  function micError(err) {
    const n = err && err.name;
    if (!globalThis.isSecureContext) return 'https';
    if (n === 'NotFoundError' || n === 'OverconstrainedError') return 'mic_missing';
    return 'mic_denied';
  }

  // ---- socket ------------------------------------------------------------
  function connect() {
    if (closed) return;
    clearTimeout(reconnectTimer); reconnectTimer = null;
    ready = false;
    player.resetGen();
    pairer.reset();
    const sock = new WebSocket(voiceUrl(location, thread.id, driveDeviceId()));
    ws = sock;
    sock.binaryType = 'arraybuffer';
    sock.onopen = () => {
      if (ws !== sock) return;
      backoff = 0;
      hb.start();
      if (listening) sendStart(); else send({ type: 'pause' });
    };
    sock.onmessage = (e) => {
      if (ws !== sock) return;
      hb.alive();
      if (typeof e.data !== 'string') {
        const pair = pairer.binary(e.data);
        if (pair) onAudio(pair.header, pair.pcm);
        return;
      }
      let m; try { m = JSON.parse(e.data); } catch { return; }
      onFrame(m);
    };
    sock.onclose = (e) => { if (ws === sock) onSocketClosed(e.code, e.reason); };
  }

  /** A heartbeat miss: the socket is half-open. Abandon it and reconnect now. */
  function dropSocket() {
    const old = ws;
    ws = null;
    if (old) { old.onclose = null; old.onmessage = null; try { old.close(); } catch { /* gone */ } }
    onSocketClosed(1006);
  }

  function onSocketClosed(code, why = '') {
    hb.stop();
    ready = false;
    pairer.reset();
    if (closed || state === 'stopped' || state === 'locked') return;
    const d = reconnectDecision(code, why);
    if (d.reason === 'replaced') {
      // Another tab or device took over this session's voice: leave quietly.
      close({ quiet: true });
      ctx.toast(t('drive.replaced'));
      return;
    }
    if (!d.retry) { stop(d.reason); return; }
    // A worker died server-side (1011): the reconnect gets a fresh session;
    // say what happened so "No connection" is not the only word on screen.
    if (d.reason === 'internal') showNotice(t('drive.err_internal_retry'), { ms: 8000 });
    if (!offlineSince) offlineSince = Date.now();
    if (Date.now() - offlineSince >= GIVE_UP_MS) { stop('network'); return; }
    backoff = nextBackoff(backoff);
    atScheduled = Date.now() + backoff;
    reconnectTimer = setTimeout(() => { reconnectTimer = null; connect(); paint(); }, backoff);
    player.stop();
    setState('offline');
    if (!offlineTicker) offlineTicker = setInterval(() => { if (state === 'offline') paint(); }, 1000);
  }

  // ---- incoming -----------------------------------------------------------
  function onAudio(h, pcm) {
    if (h.message_id && h.kind === 'reply') heard.note(h.message_id);
    // The answer has started: taking the question back now would leave the
    // reply orphaned in the thread (the server refuses it too).
    if (h.kind === 'reply' && !undoBtn.hidden) hideUndo();
    if (h.kind === 'reply' && h.text && settings.captions) {
      const span = el('span', { class: 'drive-sentence', text: `${h.text} ` });
      them.append(span);
      sentences.set(h.seq, span);
    }
    const ok = player.play(pcm16ToFloat(pcm), h.sample_rate || 24000, h.gen || 0,
      typeof h.seq === 'number' ? h.seq : null);
    if (ok) {
      playingKind = h.kind || 'reply';
      kickLevelLoop();
      const span = h.chunk === 0 ? sentences.get(h.seq) : null;
      if (span) {
        const delay = Math.max(0, (player.lastStart - ac.currentTime) * 1000);
        setTimeout(() => {
          if (closed) return;
          them.querySelectorAll('.drive-sentence.now').forEach((n) => n.classList.remove('now'));
          span.classList.add('now');
        }, delay);
      }
    }
  }

  function onFrame(m) {
    switch (m.type) {
      case 'ready': {
        ready = true;
        offlineSince = 0;
        const reconnected = everReady;
        if (!everReady) {
          everReady = true;
          for (const f of micBuf.flush()) { if (ws && ws.readyState === 1) ws.send(f); }
        }
        earcon('ready');
        // Back after a drop or a Try again: whatever the notice said is over.
        if (stopReason || reconnected) { stopReason = null; showNotice(''); }
        if (listening) { if (state === 'connecting' || state === 'offline') setState('listening'); }
        else setState('paused');
        break;
      }
      case 'pong': break;
      case 'audio': pairer.header(m); break;
      case 'state':
        if (!ready && m.state === 'listening') break;    // wait for `ready` (the tone)
        if (listening || m.state === 'speaking' || m.state === 'paused') setState(m.state);
        break;
      case 'eot': earcon('eot'); break;
      case 'ack': earcon('ack'); break;
      case 'transcript': onTranscript(m); break;
      case 'undone': onUndone(); break;
      case 'barge_in': setDuck(false); player.stop(m.gen); break;
      case 'duck': setDuck(!!m.on); break;
      case 'turn_error':
        earcon('err');
        showNotice(t(turnErrorKey(m.reason)), { ms: 8000 });
        break;
      case 'metrics':
        if (globalThis.console && console.debug) console.debug('[drive] metrics', m);
        paintTimings(m);
        break;
      case 'locked':
        showNotice(t('drive.locked_notice'));
        setState('locked');
        setTimeout(() => close({ quiet: true }), 2000);
        break;
      case 'error': {
        const key = errorFrameKey(m);
        if (!key) break;
        earcon('err');
        showNotice(t(key), { ms: 8000 });
        if (key === 'drive.err_undo_expired') hideUndo();
        break;
      }
      default: break;
    }
  }

  function onTranscript(m) {
    // `message_id` (amendment B2) is the user row Undo would take back. null
    // = the words were heard but nothing was sent (filtered out), so there is
    // nothing to undo; absent = a 2.1.0 server, which still accepts `undo`.
    const row = m.message_id === undefined ? undefined : (m.message_id || null);
    if (!settings.captions) { if (m.text && row !== null) openUndo(row); return; }
    if (!m.text) {
      you.textContent = t('drive.you_empty');
      you.classList.add('drive-empty');
      earcon('err');
      return;
    }
    you.classList.remove('drive-empty');
    you.textContent = t('drive.you_said', { text: m.text });
    if (!m.merged) { them.replaceChildren(); sentences.clear(); }
    showNotice('');
    if (row !== null) openUndo(row);
  }

  let undoFor = null;          // the row id the offered Undo refers to
  function openUndo(row) {
    undoFor = row || null;
    undo.open();
    undoBtn.hidden = false;
    const tick = () => {
      if (!undo.canUndo()) { hideUndo(); return; }
      undoBtn.textContent = t('drive.undo_countdown', { s: undo.remaining() });
    };
    clearInterval(undoTicker);
    undoTicker = setInterval(tick, 250);
    tick();
    announce(t('drive.undo_available'));
  }
  function hideUndo() {
    undo.close();
    clearInterval(undoTicker); undoTicker = null;
    undoBtn.hidden = true;
  }
  function doUndo() {
    if (!undo.canUndo()) { hideUndo(); return; }
    const frame = { type: 'undo' };
    if (undoFor) frame.message_id = undoFor;     // which row, for the server's records
    send(frame);
    hideUndo();
  }
  function onUndone() {
    hideUndo();
    // The server's barge_in (sent just before `undone`) already moved gen on;
    // bumping it again here would drop the next reply as stale.
    player.stop();
    you.textContent = '';
    them.replaceChildren(); sentences.clear();
    showNotice(t('drive.undone'), { kind: 'info', ms: 4000 });
  }

  // ---- actions -------------------------------------------------------------
  function setListening(on) {
    listening = on;
    if (node) node.port.postMessage({ enabled: on });
    if (!on) {
      // Only a reply that is actually playing is stopped: stop_playback makes
      // the server barge in (gen + 1), which drop() mirrors. Sent with nothing
      // playing it would also mute a reply still being written.
      if (player.playing) { player.drop(); send({ type: 'stop_playback' }); }
      send({ type: 'pause' });
      setState('paused');
    }
    else { sendStart(); setState(ready ? 'listening' : state); }
  }

  function tap() {
    const act = tapAction(state, { playing: replyPlaying() });
    if (act === 'interrupt') { player.drop(); send({ type: 'stop_playback' }); paint(); return; }
    if (act === 'pause') { setListening(false); return; }
    if (act === 'resume') { setListening(true); return; }
    // cancel → the server barges in (gen + 1) and abandons the turn; drop()
    // mirrors that gen so late chunks of the cancelled reply stay silent.
    if (act === 'cancel') { player.drop(); send({ type: 'cancel' }); hideUndo(); return; }
    if (act === 'retry') retry();
  }

  async function retry() {
    showNotice('');
    stopReason = null;
    offlineSince = 0;
    backoff = 0;
    setState('connecting');
    if (!stream) {
      try { await startMic(); } catch (err) { stop(micError(err)); return; }
    }
    connect();
  }

  // ---- quick sheet ----------------------------------------------------------
  function openSheet() {
    if (sheet) return;
    const scrim = el('div', { class: 'drive-scrim' });
    const status_ = ctx.voiceStatus ? ctx.voiceStatus() : null;
    const panel = buildQuickSheet({
      status: status_, bot,
      onClose: () => closeSheet(),
      onAllSettings: () => { close(); if (ctx.openDriveSettings) ctx.openDriveSettings(); },
      notify: (msg) => showNotice(msg, { ms: 6000 }),
    });
    scrim.addEventListener('click', () => closeSheet());
    sheet = { scrim, panel };
    root.append(scrim, panel);
    col.inert = true;
    const first = panel.querySelector('select, button, input');
    if (first) first.focus();
  }
  function closeSheet() {
    if (!sheet) return;
    sheet.scrim.remove(); sheet.panel.remove();
    sheet = null;
    col.inert = false;
    gear.focus();
  }

  // ---- lifecycle -------------------------------------------------------------
  async function holdWakeLock() {
    if (!settings.screenOn) return;
    try {
      if ('wakeLock' in navigator && document.visibilityState === 'visible') {
        wakeLock = await navigator.wakeLock.request('screen');
      }
    } catch { /* battery saver or unsupported: the screen may sleep */ }
  }

  function setupMediaSession() {
    if (!('mediaSession' in navigator)) return;
    try {
      const meta = { title: (thread.title || '').trim() || t('drive.title'), artist: bot ? bot.name : 'DisPatch', album: 'DisPatch' };
      if (avatar.src) meta.artwork = [{ src: new URL(avatar.src, location.href).href, sizes: '256x256' }];
      navigator.mediaSession.metadata = new MediaMetadata(meta);
      navigator.mediaSession.setActionHandler('play', () => tap());
      navigator.mediaSession.setActionHandler('pause', () => tap());
      try { navigator.mediaSession.setActionHandler('stop', () => close()); } catch { /* optional */ }
    } catch { /* not fatal */ }
  }

  function onVisibility() {
    if (document.visibilityState === 'hidden') {
      // The mic dies with a hidden page anyway; say so to the server so a
      // half-heard turn is not submitted, and to the driver on return (M6d).
      if (listening && state !== 'stopped') { pausedByHide = true; setListening(false); }
      return;
    }
    if (settings.screenOn) holdWakeLock();
    ac.resume().catch(() => {});
    if (pausedByHide) {
      pausedByHide = false;
      setListening(true);
      showNotice(t('drive.paused_screen_off'), { kind: 'info', ms: 4000 });
    }
  }

  // An iOS call or Siri interrupts the context without hiding the page (M6c).
  ac.onstatechange = () => {
    if (closed) return;
    if (ac.state === 'interrupted' || ac.state === 'suspended') {
      ac.resume().catch(() => {
        if (document.visibilityState === 'visible') showNotice(t('drive.audio_interrupted'), { kind: 'info' });
      });
    }
  };

  const unsubscribe = onDriveSettings((next) => {
    const prev = settings;
    settings = next;
    if (listening) sendStart();
    talk.hidden = !settings.captions;
    if (!settings.showTimings) timings.hidden = true;
    if (prev.screenOn !== next.screenOn) {
      if (next.screenOn) holdWakeLock(); else { try { wakeLock && wakeLock.release(); } catch { /* gone */ } wakeLock = null; }
    }
    if (prev.micId !== next.micId && stream) {
      stopMic();
      startMic().catch((err) => stop(micError(err)));
    }
    if (prev.sinkId !== next.sinkId && typeof ac.setSinkId === 'function') ac.setSinkId(next.sinkId || '').catch(() => {});
    paint();
  });

  function onKey(e) {
    if (e.key === 'Escape') {
      e.preventDefault();
      if (sheet) closeSheet(); else close();
      return;
    }
    if (e.key === 'Tab') { trapFocus(e); return; }
    const tag = (e.target && e.target.tagName) || '';
    if (/^(BUTTON|SELECT|INPUT|TEXTAREA)$/.test(tag) || sheet) return;
    if (e.key === ' ' || e.code === 'Space') { e.preventDefault(); if (state !== 'stopped') setListening(!listening); }
    else if ((e.key === 'u' || e.key === 'U') && !undoBtn.hidden) { e.preventDefault(); doUndo(); }
  }
  function trapFocus(e) {
    const scope = sheet ? sheet.panel : root;
    const items = [...scope.querySelectorAll('button, select, input, [tabindex]:not([tabindex="-1"])')]
      .filter((n) => !n.disabled && !n.hidden && n.offsetParent !== null);
    if (!items.length) return;
    const first = items[0];
    const last = items[items.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  }

  function onPop() { poppedBySystem = true; close(); }

  function close({ quiet = false } = {}) {
    if (closed) return;
    closed = true;
    opening = false;
    hb.stop();
    clearTimeout(reconnectTimer);
    clearInterval(thinkingTicker); clearInterval(undoTicker); clearInterval(offlineTicker);
    clearTimeout(noticeTimer);
    if (raf) cancelAnimationFrame(raf);
    unsubscribe();
    try { if (ws) { ws.onclose = null; ws.close(); } } catch { /* gone */ }
    stopMic();
    try { player.stop(); ac.close(); } catch { /* gone */ }
    try { wakeLock && wakeLock.release(); } catch { /* gone */ }
    try { keepAlive && keepAlive.pause(); } catch { /* gone */ }
    if ('mediaSession' in navigator) {
      for (const a of ['play', 'pause', 'stop']) { try { navigator.mediaSession.setActionHandler(a, null); } catch { /* ok */ } }
      try { navigator.mediaSession.metadata = null; } catch { /* ok */ }
    }
    document.removeEventListener('visibilitychange', onVisibility);
    window.removeEventListener('popstate', onPop);
    root.remove();
    document.body.classList.remove('drive-open');
    releaseInert(inertToken);
    active = null;
    if (pushedHistory && !poppedBySystem) { try { history.back(); } catch { /* best effort */ } }
    const back = ctx.returnFocus;
    if (back && back.isConnected) { try { back.focus(); } catch { /* best effort */ } }
    if (!quiet) ctx.toast(t('drive.ended'));
  }

  ring.addEventListener('click', tap);
  pauseBtn.addEventListener('click', () => { if (state === 'stopped') retry(); else setListening(!listening); });
  endBtn.addEventListener('click', () => close());
  undoBtn.addEventListener('click', doUndo);
  gear.addEventListener('click', openSheet);
  root.addEventListener('keydown', onKey);
  document.addEventListener('visibilitychange', onVisibility);
  // One history entry: a phone's Back closes Drive instead of leaving the app.
  try { history.pushState({ drive: 1 }, ''); pushedHistory = true; window.addEventListener('popstate', onPop); } catch { /* best effort */ }

  talk.hidden = !settings.captions;
  setState('connecting');
  active = { close, get state() { return state; } };
  opening = false;
  setupMediaSession();
  ring.focus();

  // Socket and mic start together: the mic prompt can take seconds, and the
  // server loads its models meanwhile. Audio is held until `ready` either way.
  connect();
  startMic().then(() => holdWakeLock()).catch((err) => { if (can()) stop(micError(err)); });
  return active;
}
