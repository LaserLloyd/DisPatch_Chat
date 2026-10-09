// Drive mode — hands-free voice for the open thread (unlocked tier only).
//
// One full-screen view: a huge tap target (listen / pause), a state word
// (Listening · Hearing you · Thinking · Speaking), the last thing you said and
// the last thing the bot said. No reading required.
//
// The browser only moves audio. Mic → AudioWorklet (16 kHz PCM16) →
// /ws/voice/<thread>; the server does turn detection, speech-to-text, sends
// the words through the thread's NORMAL send path, and streams the reply back
// as PCM16 chunks that are scheduled gap-free here. See
// docs/voice-drive-mode.md for the protocol and what is and is not verified.
//
// Keep-alive: Screen Wake Lock (screen stays on while Drive is open) and a
// Media Session (so a Bluetooth / steering-wheel play-pause can toggle
// listening). Neither keeps the mic alive with the screen LOCKED — on Android
// Chrome a page loses the mic when it is hidden. That is a platform limit;
// Drive mode is a foreground app.
//
// Pure helpers are exported for frontend/tests/drive.test.js; the DOM half
// only runs inside openDrive().

export const PATIENCE_PRESETS = ['quick', 'normal', 'patient'];
const PATIENCE_KEY = 'dispatch-drive-patience';
const STATE_WORDS = {
  connecting: 'Connecting…', listening: 'Listening', hearing: 'Hearing you',
  transcribing: 'Got it…', thinking: 'Thinking', speaking: 'Speaking',
  paused: 'Paused — tap to talk', offline: 'Reconnecting…', locked: 'Locked',
};

// ---------------------------------------------------------------- pure ----

/** Reconnect delay: 0.5 s doubling to 8 s, so a tunnel costs seconds, not minutes. */
export function nextBackoff(prev) {
  return Math.min(8000, prev ? prev * 2 : 500);
}

/** Consecutive 1013 ("voice unavailable") closes before Drive mode gives up. */
export const MAX_UNAVAILABLE = 3;

/**
 * What to do after the voice socket closed.
 * 1008 = refused (locked / not allowed): stop at once. 1013 = the server
 * can't do voice right now (models missing, too many voice sockets): retry
 * with backoff, but give up after MAX_UNAVAILABLE in a row instead of
 * hammering a server that keeps saying no. Anything else (a tunnel, a
 * dropped data connection) retries with the capped backoff indefinitely.
 */
export function reconnectDecision(code, unavailableStreak) {
  if (code === 1008) return { retry: false, reason: 'refused', streak: 0 };
  if (code === 1013) {
    const streak = unavailableStreak + 1;
    return streak >= MAX_UNAVAILABLE
      ? { retry: false, reason: 'unavailable', streak }
      : { retry: true, reason: 'unavailable', streak };
  }
  return { retry: true, reason: 'network', streak: 0 };
}

/** ws(s)://host/ws/voice/<thread> for the page's own origin. */
export function voiceUrl(loc, threadId) {
  const proto = loc.protocol === 'https:' ? 'wss' : 'ws';
  return `${proto}://${loc.host}/ws/voice/${encodeURIComponent(threadId)}`;
}

/** Can Drive mode open for this thread at all? (The server re-checks.) */
export function driveAllowed({ locked, thread, bot, secure }) {
  if (locked) return { ok: false, why: 'Unlock DisPatch to use Drive mode.' };
  if (!thread) return { ok: false, why: 'Open a chat first.' };
  if (bot && bot.safe) return { ok: false, why: 'Drive mode is not available for this bot.' };
  if (!secure) return { ok: false, why: 'The microphone needs a secure (https) address. Open DisPatch through its https link.' };
  return { ok: true, why: '' };
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
 * Chunks from an older `gen` (before a barge-in) are dropped.
 */
export function createPlayer(ctx, { onIdle } = {}) {
  let nextAt = 0;
  let gen = 0;
  const live = new Set();
  const player = {
    get gen() { return gen; },
    get playing() { return live.size > 0; },
    play(floatPcm, sampleRate, chunkGen) {
      if (chunkGen < gen) return false;
      gen = chunkGen;
      const buf = ctx.createBuffer(1, floatPcm.length, sampleRate);
      buf.getChannelData(0).set(floatPcm);
      const src = ctx.createBufferSource();
      src.buffer = buf;
      src.connect(ctx.destination);
      const start = Math.max(ctx.currentTime + 0.02, nextAt);
      nextAt = start + floatPcm.length / sampleRate;
      live.add(src);
      src.onended = () => {
        live.delete(src);
        if (!live.size && onIdle) onIdle(gen);
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
  };
  return player;
}

/** A short two-tone "heard you" earcon (~150 ms). */
export function playEarcon(ctx, kind = 'eot') {
  const tones = kind === 'eot' ? [660, 880] : kind === 'ack' ? [520, 520] : [440, 330];
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

export function loadPatience(storage) {
  try {
    const v = storage && storage.getItem(PATIENCE_KEY);
    return PATIENCE_PRESETS.includes(v) ? v : 'normal';
  } catch { return 'normal'; }
}

export function savePatience(storage, v) {
  try { if (storage && PATIENCE_PRESETS.includes(v)) storage.setItem(PATIENCE_KEY, v); } catch { /* private mode */ }
}

// ----------------------------------------------------------------- DOM ----

let active = null;   // the one open Drive session

/**
 * Wire the header button. `ctx` supplies the app's view of the world:
 *   getThread() -> {id, bot_id} | null, getBot(id) -> bot | null,
 *   isLocked() -> bool, toast(msg, isError)
 * Call refresh() whenever the open thread or the lock state changes.
 */
export function initDrive(button, ctx) {
  if (!button) return { refresh() {} };
  const refresh = () => {
    const thread = ctx.getThread();
    const verdict = driveAllowed({
      locked: ctx.isLocked(), thread, bot: thread && ctx.getBot(thread.bot_id),
      secure: !!globalThis.isSecureContext,
    });
    // Hidden when locked or for a safe bot; shown-but-explaining on http.
    button.hidden = ctx.isLocked() || !thread || !!(thread && ctx.getBot(thread.bot_id)?.safe);
    button.title = verdict.ok ? 'Drive mode (hands-free voice)' : verdict.why;
    button.dataset.ready = verdict.ok ? '1' : '0';
  };
  button.addEventListener('click', () => {
    const thread = ctx.getThread();
    const verdict = driveAllowed({
      locked: ctx.isLocked(), thread, bot: thread && ctx.getBot(thread.bot_id),
      secure: !!globalThis.isSecureContext,
    });
    if (!verdict.ok) { ctx.toast(verdict.why, true); return; }
    openDrive(thread, ctx);
  });
  refresh();
  return { refresh, close: () => active && active.close() };
}

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
}

export async function openDrive(thread, ctx) {
  if (active) active.close();
  const bot = ctx.getBot(thread.bot_id);
  const root = el('div', 'drive-overlay');
  root.setAttribute('role', 'dialog');
  root.setAttribute('aria-modal', 'true');
  root.setAttribute('aria-label', 'Drive mode');
  const top = el('div', 'drive-top');
  const title = el('div', 'drive-title', bot ? `${bot.emoji || ''} ${bot.name}`.trim() : 'Drive mode');
  const exit = el('button', 'drive-exit', 'Exit');
  top.append(title, exit);
  const big = el('button', 'drive-big');
  big.setAttribute('aria-live', 'polite');
  const stateWord = el('span', 'drive-state', STATE_WORDS.connecting);
  big.append(stateWord);
  const you = el('div', 'drive-line drive-you');
  const them = el('div', 'drive-line drive-them');
  const pat = el('div', 'drive-patience');
  pat.append(el('span', 'drive-patience-label', 'Patience'));
  let patience = loadPatience(globalThis.localStorage);
  const patButtons = PATIENCE_PRESETS.map((p) => {
    const b = el('button', 'drive-pat', p[0].toUpperCase() + p.slice(1));
    b.setAttribute('aria-pressed', String(p === patience));
    b.addEventListener('click', () => {
      patience = p; savePatience(globalThis.localStorage, p);
      patButtons.forEach((x, i) => x.setAttribute('aria-pressed', String(PATIENCE_PRESETS[i] === p)));
      if (listening) send({ type: 'start', patience, after: lastHeard });
    });
    pat.append(b);
    return b;
  });
  const metrics = el('div', 'drive-metrics');
  root.append(top, big, you, them, pat, metrics);
  document.body.append(root);
  document.body.classList.add('drive-open');

  // ---- audio plumbing ----
  const ac = new (globalThis.AudioContext || globalThis.webkitAudioContext)();
  await ac.resume().catch(() => {});
  const player = createPlayer(ac, {
    onIdle: (gen) => { send({ type: 'playback_done', gen }); },
  });
  const pairer = createAudioPairer();
  let stream = null;
  let node = null;
  let ws = null;
  let backoff = 0;
  let unavailableStreak = 0;
  let reconnectTimer = null;
  let closed = false;
  let listening = true;
  let lastHeard = null;
  let wakeLock = null;
  let keepAlive = null;

  function setState(s) {
    stateWord.textContent = STATE_WORDS[s] || s;
    root.dataset.state = s;
  }

  function send(obj) {
    if (ws && ws.readyState === 1) ws.send(JSON.stringify(obj));
  }

  async function startMic() {
    stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
    await ac.audioWorklet.addModule('/static/voice-worklet.js?v=1');
    const srcNode = ac.createMediaStreamSource(stream);
    node = new AudioWorkletNode(ac, 'drive-mic');
    node.port.onmessage = (e) => {
      if (listening && ws && ws.readyState === 1) ws.send(e.data);
    };
    srcNode.connect(node);
    // Not connected to the destination: the mic must never play back.
  }

  async function holdWakeLock() {
    try {
      if ('wakeLock' in navigator && document.visibilityState === 'visible') {
        wakeLock = await navigator.wakeLock.request('screen');
      }
    } catch { /* battery saver or unsupported: the screen may sleep */ }
  }

  function setupMediaSession() {
    // Hardware media keys only reach a page that is playing an <audio>
    // element, so a silent loop keeps the session alive.
    try {
      keepAlive = new Audio(silentWavUrl());
      keepAlive.loop = true;
      keepAlive.volume = 0.0001;
      keepAlive.play().catch(() => {});
      if ('mediaSession' in navigator) {
        navigator.mediaSession.metadata = new MediaMetadata({
          title: 'DisPatch Drive', artist: bot ? bot.name : 'DisPatch',
        });
        const toggle = () => toggleListening();
        navigator.mediaSession.setActionHandler('play', toggle);
        navigator.mediaSession.setActionHandler('pause', toggle);
        try { navigator.mediaSession.setActionHandler('stop', () => close()); } catch { /* optional */ }
      }
    } catch { /* not fatal */ }
  }

  function toggleListening() {
    // While a reply is playing, a tap means "stop talking", not "pause".
    if (player.playing) { player.stop(); send({ type: 'stop_playback' }); return; }
    listening = !listening;
    if (node) node.port.postMessage({ enabled: listening });
    send(listening ? { type: 'start', patience, after: lastHeard } : { type: 'pause' });
    if (!listening) setState('paused');
  }

  function connect() {
    if (closed) return;
    setState(ws ? 'offline' : 'connecting');
    ws = new WebSocket(voiceUrl(location, thread.id));
    ws.binaryType = 'arraybuffer';
    ws.onopen = () => {
      backoff = 0;
      if (listening) send({ type: 'start', patience, after: lastHeard });
      else send({ type: 'pause' });
    };
    ws.onmessage = (e) => {
      if (typeof e.data !== 'string') {
        const pair = pairer.binary(e.data);
        if (pair) onAudio(pair.header, pair.pcm);
        return;
      }
      let m; try { m = JSON.parse(e.data); } catch { return; }
      onFrame(m);
    };
    ws.onclose = (e) => {
      pairer.reset();
      if (closed) return;
      const d = reconnectDecision(e.code, unavailableStreak);
      unavailableStreak = d.streak;
      if (!d.retry && d.reason === 'refused') {   // locked / not allowed — don't hammer
        setState('locked');
        ctx.toast('Drive mode was refused (locked or not allowed).', true);
        return;
      }
      if (!d.retry) {
        setState('offline');
        ctx.toast('Drive mode is unavailable on the server right now.', true);
        return;
      }
      setState('offline');
      backoff = nextBackoff(backoff);
      reconnectTimer = setTimeout(connect, backoff);
    };
  }

  function onAudio(h, pcm) {
    if (h.message_id) lastHeard = h.message_id;
    if (h.kind === 'reply' && h.text) them.textContent = h.text;
    player.play(pcm16ToFloat(pcm), h.sample_rate || 24000, h.gen || 0);
  }

  function onFrame(m) {
    switch (m.type) {
      case 'ready': unavailableStreak = 0; break;
      case 'audio': pairer.header(m); break;
      case 'state': if (listening || m.state === 'speaking') setState(m.state); break;
      case 'eot': playEarcon(ac, 'eot'); break;
      case 'ack': playEarcon(ac, 'ack'); break;
      case 'transcript':
        you.textContent = m.text ? `“${m.text}”` : '(didn’t catch that)';
        break;
      case 'barge_in': player.stop(m.gen); break;
      case 'metrics':
        if (m.eot_to_first_audio_ms != null) metrics.textContent = `reply audio after ${(m.eot_to_first_audio_ms / 1000).toFixed(1)} s`;
        break;
      case 'locked':
        setState('locked'); ctx.toast('DisPatch locked — Drive mode closed.', true); close(); break;
      case 'error': ctx.toast(m.message || 'Voice error', true); break;
      default: break;
    }
  }

  function onVisibility() {
    if (document.visibilityState === 'visible') {
      holdWakeLock();
      ac.resume().catch(() => {});
    }
  }

  function close() {
    if (closed) return;
    closed = true;
    clearTimeout(reconnectTimer);
    try { ws && ws.close(); } catch { /* gone */ }
    try { stream && stream.getTracks().forEach((t) => t.stop()); } catch { /* gone */ }
    try { player.stop(); ac.close(); } catch { /* gone */ }
    try { wakeLock && wakeLock.release(); } catch { /* gone */ }
    try { keepAlive && keepAlive.pause(); } catch { /* gone */ }
    if ('mediaSession' in navigator) {
      for (const a of ['play', 'pause', 'stop']) { try { navigator.mediaSession.setActionHandler(a, null); } catch { /* ok */ } }
    }
    document.removeEventListener('visibilitychange', onVisibility);
    root.remove();
    document.body.classList.remove('drive-open');
    active = null;
  }

  big.addEventListener('click', toggleListening);
  exit.addEventListener('click', close);
  root.addEventListener('keydown', (e) => { if (e.key === 'Escape') close(); });
  document.addEventListener('visibilitychange', onVisibility);
  active = { close };

  try {
    await startMic();
  } catch (err) {
    ctx.toast('Microphone unavailable: ' + (err && err.name ? err.name : 'denied'), true);
    close();
    return null;
  }
  await holdWakeLock();
  setupMediaSession();
  connect();
  big.focus();
  return active;
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
