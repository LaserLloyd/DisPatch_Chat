// Drive mode: the pure half of drive.js (no DOM, no audio hardware).
//
// Run: cd frontend && node --test "tests/**/*.test.js"
//
// The DOM half (getUserMedia, AudioWorklet, Wake Lock, Media Session) only
// means something on a real phone; the headless fake-mic round trip in
// backend/tests/e2e_voice_roundtrip.py covers the wire, and the Pixel test
// covers the rest (see docs/voice-drive-mode.md).

import test from 'node:test';
import assert from 'node:assert/strict';

import {
  createAudioPairer, createPlayer, driveAllowed, loadPatience, nextBackoff,
  pcm16ToFloat, savePatience, voiceUrl, PATIENCE_PRESETS,
} from '../static/drive.js';

test('voiceUrl follows the page scheme and escapes the thread id', () => {
  assert.equal(voiceUrl({ protocol: 'https:', host: 'box:8443' }, 't/1'), 'wss://box:8443/ws/voice/t%2F1');
  assert.equal(voiceUrl({ protocol: 'http:', host: 'localhost:8765' }, 'abc'), 'ws://localhost:8765/ws/voice/abc');
});

test('driveAllowed: locked, no thread, safe bot and insecure origin all refuse', () => {
  const thread = { id: 't', bot_id: 'b' };
  assert.equal(driveAllowed({ locked: true, thread, bot: {}, secure: true }).ok, false);
  assert.equal(driveAllowed({ locked: false, thread: null, bot: {}, secure: true }).ok, false);
  assert.equal(driveAllowed({ locked: false, thread, bot: { safe: true }, secure: true }).ok, false);
  const http = driveAllowed({ locked: false, thread, bot: {}, secure: false });
  assert.equal(http.ok, false);
  assert.match(http.why, /https/);
  assert.equal(driveAllowed({ locked: false, thread, bot: { safe: false }, secure: true }).ok, true);
});

test('nextBackoff doubles from 0.5 s and caps at 8 s', () => {
  const seq = [];
  let b = 0;
  for (let i = 0; i < 7; i++) { b = nextBackoff(b); seq.push(b); }
  assert.deepEqual(seq, [500, 1000, 2000, 4000, 8000, 8000, 8000]);
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
    currentTime: 10, destination: {}, started: [],
    createBuffer(ch, len, sr) {
      const data = new Float32Array(len);
      return { duration: len / sr, getChannelData: () => data };
    },
    createBufferSource() {
      const src = {
        connect() {}, onended: null, stopped: false,
        start(at) { ctx.started.push({ at, src }); },
        stop() { src.stopped = true; },
      };
      return src;
    },
  };
  return ctx;
}

test('player schedules chunks back to back without gaps', () => {
  const ctx = fakeCtx();
  const pl = createPlayer(ctx);
  pl.play(new Float32Array(2400), 24000, 0);   // 0.1 s
  pl.play(new Float32Array(4800), 24000, 0);   // 0.2 s
  const [a, b] = ctx.started.map((s) => s.at);
  assert.ok(Math.abs(a - 10.02) < 1e-9);
  assert.ok(Math.abs(b - (a + 0.1)) < 1e-9);
  assert.equal(pl.playing, true);
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

test('patience preference round-trips and rejects junk', () => {
  const store = new Map();
  const s = { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => store.set(k, v) };
  assert.equal(loadPatience(s), 'normal');
  savePatience(s, 'patient');
  assert.equal(loadPatience(s), 'patient');
  savePatience(s, 'reckless');
  assert.equal(loadPatience(s), 'patient');
  assert.equal(loadPatience({ getItem() { throw new Error('denied'); } }), 'normal');
  assert.deepEqual(PATIENCE_PRESETS, ['quick', 'normal', 'patient']);
});

test('reconnectDecision: 1008 stops, 1013 gives up after a streak, network retries', async () => {
  const { reconnectDecision, MAX_UNAVAILABLE } = await import('../static/drive.js');
  assert.deepEqual(reconnectDecision(1008, 0), { retry: false, reason: 'refused', streak: 0 });
  let streak = 0;
  const seen = [];
  for (let i = 0; i < MAX_UNAVAILABLE; i++) {
    const d = reconnectDecision(1013, streak);
    streak = d.streak;
    seen.push(d.retry);
  }
  assert.deepEqual(seen, [...Array(MAX_UNAVAILABLE - 1).fill(true), false]);
  // A dropped connection (tunnel) never gives up, and resets the streak.
  const net = reconnectDecision(1006, 2);
  assert.equal(net.retry, true);
  assert.equal(net.streak, 0);
});
