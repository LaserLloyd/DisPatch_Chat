// Drive mode: the pure half of drive.js, js/drive-settings.js and the mic
// worklet's downsampler (no DOM, no audio hardware).
//
// Run: cd frontend && node --test "tests/**/*.test.js"
//
// The DOM half (getUserMedia, AudioWorklet, Wake Lock, Media Session) only
// means something in a browser; the headless fake-mic round trip in
// backend/tests/e2e_voice_roundtrip.py covers the wire, and a real phone
// covers the rest (see docs/voice-drive-mode.md).

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import {
  createAudioPairer, createPlayer, driveAllowed, nextBackoff, pcm16ToFloat, voiceUrl, driveDeviceId,
  reconnectDecision, tapAction, thinkingPhase, hintKey, createMicBuffer, createUndoWindow,
  createHeartbeat, createHeardTracker, turnErrorKey, errorFrameKey, PATIENCE_PRESETS,
  GIVE_UP_MS, PREBUFFER_FRAMES, UNDO_MS, PING_EVERY_MS, PONG_TIMEOUT_MS, EARCONS, timingsLine,
} from '../static/drive.js';
import {
  DRIVE_KEY, LEGACY_PATIENCE_KEY, DRIVE_DEFAULTS, loadDriveSettings, saveDriveSettings,
  normalizeDriveSettings, onDriveSettings, startPayload, clampSpeed, parseVoice, voiceName,
  voiceOptions, engineLine,
} from '../static/js/drive-settings.js';
import { canBuild, chipClass, voiceMetaLine } from '../static/js/voices.js';

const HERE = dirname(fileURLToPath(import.meta.url));

function memStorage(init = {}) {
  const m = new Map(Object.entries(init));
  return {
    map: m,
    getItem: (k) => (m.has(k) ? m.get(k) : null),
    setItem: (k, v) => m.set(k, String(v)),
    removeItem: (k) => m.delete(k),
  };
}

// ------------------------------------------------------------- basics ---

test('driveDeviceId: stable per browser, well-formed, survives no storage', () => {
  const m = new Map();
  const st = { getItem: (k) => m.get(k) ?? null, setItem: (k, v) => m.set(k, v) };
  const a = driveDeviceId(st);
  assert.match(a, /^[A-Za-z0-9-]{8,64}$/);
  assert.equal(driveDeviceId(st), a);
  assert.equal(voiceUrl({ protocol: 'http:', host: 'h' }, 't', a), `ws://h/ws/voice/t?d=${a}`);
  assert.equal(typeof driveDeviceId(null), 'string');
});

test('voiceUrl follows the page scheme and escapes the thread id', () => {
  assert.equal(voiceUrl({ protocol: 'https:', host: 'box:8443' }, 't/1'), 'wss://box:8443/ws/voice/t%2F1');
  assert.equal(voiceUrl({ protocol: 'http:', host: 'localhost:8765' }, 'abc'), 'ws://localhost:8765/ws/voice/abc');
});

test('driveAllowed: locked, no thread, safe bot and insecure origin all refuse, each with its own reason', () => {
  const thread = { id: 't', bot_id: 'b' };
  assert.equal(driveAllowed({ locked: true, thread, bot: {}, secure: true }).whyKey, 'drive.err_locked_device');
  assert.equal(driveAllowed({ locked: false, thread: null, bot: {}, secure: true }).whyKey, 'drive.err_no_chat');
  assert.equal(driveAllowed({ locked: false, thread, bot: { safe: true }, secure: true }).whyKey, 'drive.err_safe_bot');
  const http = driveAllowed({ locked: false, thread, bot: {}, secure: false });
  assert.equal(http.ok, false);
  assert.equal(http.whyKey, 'drive.err_https');
  assert.equal(typeof http.why, 'string');
  assert.equal(driveAllowed({ locked: false, thread, bot: { safe: false }, secure: true }).ok, true);
});

test('nextBackoff doubles from 0.5 s and caps at 8 s', () => {
  const seq = [];
  let b = 0;
  for (let i = 0; i < 7; i++) { b = nextBackoff(b); seq.push(b); }
  assert.deepEqual(seq, [500, 1000, 2000, 4000, 8000, 8000, 8000]);
  assert.equal(GIVE_UP_MS, 60000);
});

test('pcm16ToFloat maps the int16 range onto [-1, 1)', () => {
  const buf = new Int16Array([0, 16384, -32768, 32767]).buffer;
  const f = pcm16ToFloat(buf);
  assert.deepEqual(Array.from(f).map((x) => Math.round(x * 1000) / 1000), [0, 0.5, -1, 1]);
});

test('audio pairer pairs a header with the NEXT binary frame only', () => {
  const p = createAudioPairer();
  assert.equal(p.binary(new ArrayBuffer(2)), null);         // orphan dropped
  p.header({ seq: 1 });
  const pair = p.binary(new ArrayBuffer(4));
  assert.equal(pair.header.seq, 1);
  assert.equal(pair.pcm.byteLength, 4);
  assert.equal(p.binary(new ArrayBuffer(4)), null);         // header consumed
  p.header({ seq: 2 }); p.reset();
  assert.equal(p.binary(new ArrayBuffer(4)), null);         // reconnect clears it
});

function fakeCtx() {
  const ctx = {
    currentTime: 10, destination: { name: 'dest' }, started: [],
    createBuffer(ch, len, sr) {
      const data = new Float32Array(len);
      return { duration: len / sr, getChannelData: () => data };
    },
    createBufferSource() {
      const src = {
        connected: null, onended: null, stopped: false,
        connect(n) { src.connected = n; },
        start(at) { ctx.started.push({ at, src }); },
        stop() { src.stopped = true; },
      };
      return src;
    },
  };
  return ctx;
}

test('player schedules chunks back to back without gaps, through the analyser when given one', () => {
  const ctx = fakeCtx();
  const analyser = { name: 'analyser' };
  const pl = createPlayer(ctx, { out: analyser });
  pl.play(new Float32Array(2400), 24000, 0);   // 0.1 s
  pl.play(new Float32Array(4800), 24000, 0);   // 0.2 s
  const [a, b] = ctx.started.map((s) => s.at);
  assert.ok(Math.abs(a - 10.02) < 1e-9);
  assert.ok(Math.abs(b - (a + 0.1)) < 1e-9);
  assert.ok(Math.abs(pl.lastStart - b) < 1e-9);
  assert.equal(pl.playing, true);
  assert.equal(ctx.started[0].src.connected, analyser);
  const plain = createPlayer(ctx);
  plain.play(new Float32Array(10), 24000, 0);
  assert.equal(ctx.started.at(-1).src.connected, ctx.destination);
});

test('barge-in stops everything and drops chunks from the old generation', () => {
  const ctx = fakeCtx();
  const idle = [];
  const pl = createPlayer(ctx, { onIdle: (g) => idle.push(g) });
  pl.play(new Float32Array(2400), 24000, 0);
  pl.stop(1);
  assert.ok(ctx.started[0].src.stopped);
  assert.equal(pl.playing, false);
  assert.equal(pl.play(new Float32Array(10), 24000, 0), false);    // stale gen
  assert.equal(pl.play(new Float32Array(10), 24000, 1), true);
  ctx.started.at(-1).src.onended();
  assert.deepEqual(idle, [1]);                                      // playback_done(gen)
});

test('playback_done carries the seq of the last chunk played (B6): an ack drain never ends a reply', () => {
  const ctx = fakeCtx();
  const idle = [];
  const pl = createPlayer(ctx, { onIdle: (g, seq) => idle.push([g, seq]) });
  assert.equal(pl.seq, null);
  pl.play(new Float32Array(240), 24000, 0, 7);     // an ack (seq 7)
  ctx.started.at(-1).src.onended();
  pl.play(new Float32Array(240), 24000, 0, 9);     // the reply's first sentence (seq 9)
  pl.play(new Float32Array(240), 24000, 0, 9);
  ctx.started.at(-2).src.onended();
  ctx.started.at(-1).src.onended();
  assert.deepEqual(idle, [[0, 7], [0, 9]]);
  pl.play(new Float32Array(240), 24000, 0);        // a header without seq keeps the last one
  assert.equal(pl.seq, 9);
});

test('tap-to-stop drops the chunks of this reply still on the wire (L2), and a new socket resets gen', () => {
  const ctx = fakeCtx();
  const pl = createPlayer(ctx);
  pl.play(new Float32Array(240), 24000, 3);
  pl.drop();
  assert.equal(pl.playing, false);
  assert.equal(pl.play(new Float32Array(240), 24000, 3), false);   // late chunk of the stopped reply
  assert.equal(pl.play(new Float32Array(240), 24000, 4), true);    // the server's barge_in gen
  pl.resetGen();
  assert.equal(pl.gen, 0);
  assert.equal(pl.play(new Float32Array(240), 24000, 0), true);    // a fresh socket counts from 0
});

test('earcons: ready rises, err falls, all four exist', () => {
  assert.deepEqual(Object.keys(EARCONS).sort(), ['ack', 'eot', 'err', 'ready']);
  const r = EARCONS.ready;
  assert.ok(r.every((f, i) => i === 0 || f > r[i - 1]));
  assert.ok(EARCONS.err[1] < EARCONS.err[0]);
});

// ------------------------------------------------------- reconnect rules ---

test('reconnectDecision follows the v2 contract: 1008/1013 stop, 4001 leaves quietly, 1011 and the rest retry', () => {
  assert.deepEqual(reconnectDecision(1008), { retry: false, reason: 'refused' });
  assert.deepEqual(reconnectDecision(1013), { retry: false, reason: 'unavailable' });
  assert.deepEqual(reconnectDecision(4001), { retry: false, reason: 'replaced' });
  // Amendment B2: 1011 (a worker died) and 1001 (heartbeat lost) are network-like.
  assert.deepEqual(reconnectDecision(1011), { retry: true, reason: 'internal' });
  for (const code of [1006, 1001, 1000, 1012]) {
    assert.deepEqual(reconnectDecision(code), { retry: true, reason: 'network' }, `code ${code}`);
  }
});

test('heartbeat pings on every tick and declares the socket dead after 25 s of silence', () => {
  let now = 0;
  const sent = [];
  let dead = 0;
  let cleared = 0;
  const hb = createHeartbeat({
    send: (f) => sent.push(f), onDead: () => { dead += 1; }, now: () => now,
    setInt: () => 42, clearInt: () => { cleared += 1; },
  });
  assert.equal(PING_EVERY_MS, 10000);
  assert.equal(PONG_TIMEOUT_MS, 25000);
  hb.start();
  now = 10000; hb.tick();
  assert.equal(sent.length, 1);
  assert.equal(sent[0].type, 'ping');
  now = 15000; hb.alive();                      // a pong (or any frame) arrives
  now = 30000; hb.tick();                        // 15 s since the last frame: fine
  assert.equal(dead, 0);
  now = 40000; hb.tick();                        // 25 s: dead
  assert.equal(dead, 1);
  assert.ok(cleared >= 1, 'a dead heartbeat stops its own timer');
  assert.equal(sent.length, 2);                  // no ping on the dead tick
});

// ------------------------------------------------------- state machine ---

test('tapAction: one meaning per state, and playback always means interrupt', () => {
  assert.equal(tapAction('listening'), 'pause');
  assert.equal(tapAction('hearing'), 'pause');
  assert.equal(tapAction('paused'), 'resume');
  assert.equal(tapAction('thinking'), 'cancel');
  assert.equal(tapAction('transcribing'), 'cancel');
  assert.equal(tapAction('speaking'), 'interrupt');
  assert.equal(tapAction('listening', { playing: true }), 'interrupt');
  assert.equal(tapAction('stopped'), 'retry');
  for (const s of ['connecting', 'offline', 'locked']) assert.equal(tapAction(s), 'none', s);
});

test('thinking feedback: plain, then a counter from 4 s, then "still working" from 25 s', () => {
  assert.equal(thinkingPhase(0), 'plain');
  assert.equal(thinkingPhase(3999), 'plain');
  assert.equal(thinkingPhase(4000), 'counting');
  assert.equal(thinkingPhase(24999), 'counting');
  assert.equal(thinkingPhase(25000), 'long');
  assert.equal(hintKey('thinking', { thinkingMs: 5000 }), 'drive.hint_cancel');
  assert.equal(hintKey('thinking', { thinkingMs: 26000 }), 'drive.hint_still_working');
});

test('every state that can be tapped says what the tap does', () => {
  assert.equal(hintKey('listening'), 'drive.hint_pause');
  assert.equal(hintKey('listening', { autoListen: false }), 'drive.hint_pause_ptt');
  assert.equal(hintKey('hearing'), 'drive.hint_keep_talking');
  assert.equal(hintKey('paused'), 'drive.hint_resume');
  assert.equal(hintKey('speaking'), 'drive.hint_interrupt');
  assert.equal(hintKey('connecting'), 'drive.hint_wait_tone');
  assert.equal(hintKey('offline'), 'drive.hint_offline');
  assert.equal(hintKey('stopped'), null);        // the road buttons carry Try again / Back to chat
});

test('mic pre-buffer keeps the first words until ready, capped at 10 s (oldest dropped)', () => {
  assert.equal(PREBUFFER_FRAMES, 500);           // 500 × 20 ms
  const b = createMicBuffer(3);
  for (let i = 0; i < 5; i++) b.push(i);
  assert.equal(b.size, 3);
  assert.deepEqual(b.flush(), [2, 3, 4]);
  assert.equal(b.size, 0);
  assert.deepEqual(b.flush(), []);
});

test('undo is offered for 8 s after a transcript, counting down, then refused', () => {
  let now = 1000;
  const u = createUndoWindow(UNDO_MS, () => now);
  assert.equal(UNDO_MS, 8000);
  assert.equal(u.canUndo(), false);
  u.open();
  assert.equal(u.canUndo(), true);
  assert.equal(u.remaining(), 8);
  now += 2500;
  assert.equal(u.remaining(), 6);
  now += 5499;
  assert.equal(u.canUndo(), true);
  now += 1;
  assert.equal(u.canUndo(), false);
  assert.equal(u.remaining(), 0);
  u.open(); u.close();
  assert.equal(u.canUndo(), false);
});

test('heard tracker: newest first, deduplicated, capped — for after / after_ids (L4)', () => {
  const h = createHeardTracker(3);
  assert.equal(h.after, null);
  h.note('prov-1'); h.note('prov-1'); h.note('m-1'); h.note('prov-2'); h.note('m-2');
  assert.equal(h.after, 'm-2');
  assert.deepEqual(h.ids, ['m-2', 'prov-2', 'm-1']);
  h.note('m-1');
  assert.deepEqual(h.ids, ['m-1', 'm-2', 'prov-2']);
  h.note(null);
  assert.equal(h.after, 'm-1');
});

test('turn errors and error frames map to plain words; old 2.1.0 messages still read well', () => {
  assert.equal(turnErrorKey('agent_failed'), 'drive.err_agent');
  assert.equal(turnErrorKey('no_speech_in_reply'), 'drive.err_nothing_to_say');
  assert.equal(turnErrorKey('timeout'), 'drive.err_timeout');
  assert.equal(turnErrorKey('tts_failed'), 'drive.err_tts');
  assert.equal(turnErrorKey('something-new'), 'drive.err_agent');
  assert.equal(errorFrameKey({ code: 'undo_expired' }), 'drive.err_undo_expired');
  assert.equal(errorFrameKey({ code: 'stt_failed' }), 'drive.err_stt');
  assert.equal(errorFrameKey({ code: 'rate_limited' }), 'drive.err_rate');
  // `unavailable` precedes a 1011 close (amendment B2): a notice, never a stop —
  // the close code drives the reconnect.
  assert.equal(errorFrameKey({ code: 'unavailable' }), 'drive.err_internal_retry');
  assert.equal(errorFrameKey({ code: 'bad_frame' }), null);
  assert.equal(errorFrameKey({ message: 'Speech synthesis failed.' }), 'drive.err_tts');
  assert.equal(errorFrameKey({ message: 'Could not send that message.' }), 'drive.err_send');
  assert.equal(errorFrameKey({ message: 'Speech recognition failed.' }), 'drive.err_stt');
  assert.equal(errorFrameKey({ message: 'Drive mode hit an internal error and stopped.' }), 'drive.err_internal_retry');
  assert.equal(errorFrameKey({ message: 'Voice error' }), 'drive.err_generic');
});

// ------------------------------------------------------------- settings ---

test('settings: defaults on a fresh device, and the blob is the only key written', () => {
  const s = memStorage();
  assert.deepEqual(loadDriveSettings(s), { ...DRIVE_DEFAULTS });
  assert.equal(s.map.size, 0, 'reading must not write on a fresh device');
  saveDriveSettings(s, { bargeIn: false });
  assert.deepEqual([...s.map.keys()], [DRIVE_KEY]);
  assert.equal(loadDriveSettings(s).bargeIn, false);
  assert.deepEqual(PATIENCE_PRESETS, ['quick', 'normal', 'patient']);
});

test('settings: the 2.1.0 patience key migrates into the blob once and is removed', () => {
  const s = memStorage({ [LEGACY_PATIENCE_KEY]: 'patient' });
  const got = loadDriveSettings(s);
  assert.equal(got.patience, 'patient');
  assert.equal(s.map.has(LEGACY_PATIENCE_KEY), false);
  assert.equal(JSON.parse(s.map.get(DRIVE_KEY)).patience, 'patient');
  // A junk legacy value still retires the key, with the default in its place.
  const j = memStorage({ [LEGACY_PATIENCE_KEY]: 'reckless' });
  assert.equal(loadDriveSettings(j).patience, 'normal');
  assert.equal(j.map.has(LEGACY_PATIENCE_KEY), false);
  // A blob wins over a stale legacy key.
  const both = memStorage({ [DRIVE_KEY]: JSON.stringify({ patience: 'quick' }), [LEGACY_PATIENCE_KEY]: 'patient' });
  assert.equal(loadDriveSettings(both).patience, 'quick');
});

test('settings: junk values fall back per key; storage that throws still loads', () => {
  const s = memStorage({ [DRIVE_KEY]: JSON.stringify({ patience: 'eager', replyStyle: 'essay', bargeIn: 'yes', sounds: false, micId: 7 }) });
  const got = loadDriveSettings(s);
  assert.equal(got.patience, 'normal');
  assert.equal(got.replyStyle, 'normal');
  assert.equal(got.bargeIn, true);
  assert.equal(got.sounds, false);
  assert.equal(got.micId, '');
  assert.deepEqual(loadDriveSettings({ getItem() { throw new Error('denied'); } }), { ...DRIVE_DEFAULTS });
  assert.deepEqual(loadDriveSettings(memStorage({ [DRIVE_KEY]: '{not json' })), { ...DRIVE_DEFAULTS });
  assert.deepEqual(normalizeDriveSettings(null), { ...DRIVE_DEFAULTS });
});

test('settings: a change notifies listeners (the open Drive view re-sends start)', () => {
  const s = memStorage();
  const seen = [];
  const off = onDriveSettings((v) => seen.push(v.replyStyle));
  saveDriveSettings(s, { replyStyle: 'short' });
  off();
  saveDriveSettings(s, { replyStyle: 'longer' });
  assert.deepEqual(seen, ['short']);
});

test('start payload carries exactly the contract fields', () => {
  const frame = startPayload({ ...DRIVE_DEFAULTS, patience: 'patient', bargeIn: false, replyStyle: 'short', followUp: false, sounds: false });
  assert.deepEqual(frame, { type: 'start', patience: 'patient', barge_in: false, reply_style: 'short', follow_up: false });
  const resumed = startPayload(DRIVE_DEFAULTS, 'm-9', ['m-9', 'p-3']);
  assert.equal(resumed.after, 'm-9');
  assert.deepEqual(resumed.after_ids, ['m-9', 'p-3']);
  // Device-only preferences never leave the device.
  for (const k of ['sounds', 'screenOn', 'micId', 'sinkId', 'captions', 'autoListen']) assert.ok(!(k in resumed), k);
});

test('speaking speed clamps to 0.8–1.3 on a 0.05 grid', () => {
  assert.equal(clampSpeed(1.07), 1.05);
  assert.equal(clampSpeed(2), 1.3);
  assert.equal(clampSpeed(0.1), 0.8);
  assert.equal(clampSpeed('junk'), 1);
});

test('voice values: profile ids, builtin:<id>, and options that never hide the saved voice', () => {
  assert.deepEqual(parseVoice('builtin:af_heart'), { kind: 'builtin', id: 'af_heart' });
  assert.deepEqual(parseVoice('alex'), { kind: 'profile', id: 'alex' });
  assert.deepEqual(parseVoice(''), { kind: 'default', id: '' });
  const st = {
    default_voice: 'alex',
    profiles: [{ id: 'alex', name: 'Alex' }],
    builtin_voices: [{ id: 'af_heart', name: 'Heart', lang: 'en-us' }],
  };
  assert.equal(voiceName(st, 'alex'), 'Alex');
  const opts = voiceOptions(st, { withDefault: true });
  assert.deepEqual(opts.map((o) => o.value), ['', 'alex', 'builtin:af_heart']);
  // A server that lists ids already prefixed means the same voices.
  const pre = { ...st, builtin_voices: [{ id: 'builtin:af_heart', name: 'Heart' }] };
  assert.deepEqual(voiceOptions(pre).map((o) => o.value), ['alex', 'builtin:af_heart']);
  assert.equal(voiceName(pre, 'builtin:af_heart'), voiceName(st, 'builtin:af_heart'));
  const kept = voiceOptions(st, { current: 'gone-voice' });
  assert.equal(kept.at(-1).value, 'gone-voice');
  assert.deepEqual(voiceOptions(null), []);
});

test('engine line names the engine the driver will actually hear', () => {
  // The full case matrix (cloned + fallback / cloned only / stock only /
  // built-in / cloud) is pinned in voice-tab.test.js; here: three parts.
  const line = engineLine({ tts: 'auto', profiles: [{ id: 'a' }], voices_ui: '/api/voices/ui', smart_turn: true, stt: 'parakeet' });
  assert.equal(line.split(' · ').length, 3);
});

test('reconnectDecision: a 1013 that names the switch is "voice off", not "not set up"', () => {
  assert.deepEqual(reconnectDecision(1013, 'Voice is turned off in Settings › Voice'), { retry: false, reason: 'voice_off' });
  assert.deepEqual(reconnectDecision(1013, 'Voice is turned off on this server (DISPATCH_VOICE)'), { retry: false, reason: 'voice_off' });
  assert.deepEqual(reconnectDecision(1013, 'voice models missing'), { retry: false, reason: 'unavailable' });
  assert.deepEqual(reconnectDecision(1008, 'Voice is turned off'), { retry: false, reason: 'refused' });
});

test('timingsLine: what the Drive screen shows from the server metrics', () => {
  // No dictionary under node (t() answers with the humanised key), so the
  // shape is pinned here; the words and {t} are in the locales.
  assert.equal(timingsLine({ heard: null, first: null }), '');
  assert.equal(timingsLine({ heard: 420, first: null }).split(' · ').length, 1);
  assert.equal(timingsLine({ heard: 420, first: 1640 }).split(' · ').length, 2);
});

test('show timings is a device setting, off by default', () => {
  assert.equal(DRIVE_DEFAULTS.showTimings, false);
  assert.equal(normalizeDriveSettings({ showTimings: true }).showTimings, true);
  assert.equal(normalizeDriveSettings({ showTimings: 'yes' }).showTimings, false);
});

// --------------------------------------------------------------- voices ---

test('voices: build is offered only with a usable clip, a name and a consent choice', () => {
  const clip = { blob: {} };
  assert.equal(canBuild({ clip, analysis: { ok: true }, name: 'Alex', consent: 'self' }), true);
  assert.equal(canBuild({ clip, analysis: { ok: false }, name: 'Alex', consent: 'self' }), false);
  assert.equal(canBuild({ clip, analysis: { ok: true }, name: '  ', consent: 'self' }), false);
  assert.equal(canBuild({ clip, analysis: { ok: true }, name: 'Alex', consent: '' }), false);
  assert.equal(canBuild({ clip: null, analysis: { ok: true }, name: 'Alex', consent: 'self' }), false);
  assert.equal(chipClass('fail'), 'drive-chip fail');
  assert.equal(chipClass('warn'), 'drive-chip warn');
  assert.equal(chipClass('ok'), 'drive-chip ok');
  const line = voiceMetaLine({ consent: { kind: 'self' }, created: 1760000000, reference_s: 14.2 }, () => 'D');
  assert.equal(line.split(' · ').length, 3);
});

// ------------------------------------------------------- the worklet (L3) ---

async function loadWorklet(rate) {
  let Proc = null;
  const posted = [];
  const g = globalThis;
  const saved = { AWP: g.AudioWorkletProcessor, sr: g.sampleRate, reg: g.registerProcessor };
  g.AudioWorkletProcessor = class { constructor() { this.port = { onmessage: null, postMessage: (m) => posted.push(m) }; } };
  g.sampleRate = rate;
  g.registerProcessor = (name, cls) => { Proc = cls; };
  const src = readFileSync(join(HERE, '..', 'static', 'voice-worklet.js'), 'utf8');
  // A fresh evaluation per rate (an import would be cached once).
  new Function(src)();
  const p = new Proc();
  Object.assign(g, { AudioWorkletProcessor: saved.AWP, sampleRate: saved.sr, registerProcessor: saved.reg });
  return { p, posted };
}

function feed(p, rate, freq, seconds) {
  const n = Math.round(rate * seconds);
  for (let off = 0; off < n; off += 128) {
    const ch = new Float32Array(128);
    for (let i = 0; i < 128; i++) ch[i] = 0.5 * Math.sin((2 * Math.PI * freq * (off + i)) / rate);
    p.process([[ch]]);
  }
}

function rmsOf(posted) {
  let sum = 0; let n = 0;
  for (const m of posted) {
    if (!(m instanceof ArrayBuffer)) continue;
    const a = new Int16Array(m);
    for (let i = 0; i < a.length; i++) { sum += (a[i] / 32768) ** 2; n += 1; }
  }
  return { rms: Math.sqrt(sum / Math.max(1, n)), samples: n };
}

for (const rate of [48000, 44100]) {
  test(`worklet at ${rate} Hz: 16 kHz out, speech band kept, hiss above 8 kHz filtered (L3)`, async () => {
    const speech = await loadWorklet(rate);
    feed(speech.p, rate, 1000, 1);
    const s = rmsOf(speech.posted);
    assert.ok(Math.abs(s.samples - 16000) <= 320, `~16000 samples per second, got ${s.samples}`);
    assert.ok(s.rms > 0.3, `1 kHz passes (rms ${s.rms.toFixed(3)} of 0.354)`);
    assert.ok(speech.posted.some((m) => m && typeof m.level === 'number'), 'level meter messages');

    const hiss = await loadWorklet(rate);
    feed(hiss.p, rate, 12000, 1);                 // would alias to 4 kHz unfiltered
    const h = rmsOf(hiss.posted);
    assert.ok(h.rms < 0.02, `12 kHz is stopped before decimation (rms ${h.rms.toFixed(4)})`);
  });
}

test('worklet stays silent while disabled (paused)', async () => {
  const w = await loadWorklet(48000);
  w.p.port.onmessage({ data: { enabled: false } });
  feed(w.p, 48000, 1000, 0.2);
  assert.equal(w.posted.length, 0);
});
