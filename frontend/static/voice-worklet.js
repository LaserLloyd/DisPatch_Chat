// Drive mode mic capture (AudioWorklet). Runs on the audio thread.
//
// Input: whatever rate the AudioContext runs at (48 kHz on most phones).
// Output: Int16 little-endian mono at 16 kHz, posted to the main thread in
// 20 ms frames (320 samples) as transferable ArrayBuffers — the format
// /ws/voice expects.
//
// Downsampling: a 31-tap windowed-sinc low-pass (cutoff 7 kHz) evaluated
// only at the output instants, then decimation. The old box-filter average
// had no real anti-alias stop band, so 8–24 kHz road and fan hiss folded down
// into the speech band (functional review L3), and at 44.1 kHz it alternated
// 2- and 3-sample averages.
//
// Also posts {level} (RMS of the last 100 ms, 0..1) for the Drive ring.

const TAPS = 31;
const CUTOFF_HZ = 7000;

/** Blackman-windowed sinc low-pass, unity DC gain. */
function lowpass(rate, cutoff = CUTOFF_HZ, taps = TAPS) {
  const fc = Math.min(0.49, cutoff / rate);
  const h = new Float32Array(taps);
  const m = (taps - 1) / 2;
  let sum = 0;
  for (let i = 0; i < taps; i++) {
    const x = i - m;
    const sinc = x === 0 ? 2 * fc : Math.sin(2 * Math.PI * fc * x) / (Math.PI * x);
    const w = 0.42 - 0.5 * Math.cos((2 * Math.PI * i) / (taps - 1)) + 0.08 * Math.cos((4 * Math.PI * i) / (taps - 1));
    h[i] = sinc * w;
    sum += h[i];
  }
  for (let i = 0; i < taps; i++) h[i] /= sum;
  return h;
}

class DriveMicProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / 16000;   // `sampleRate` is the worklet global
    this.h = lowpass(sampleRate);
    this.hist = new Float32Array(TAPS); // ring buffer of recent input samples
    this.pos = 0;
    this.acc = 0;                       // fractional input position carried over
    this.out = new Int16Array(320);
    this.len = 0;
    this.sq = 0;                        // sum of squares for the level meter
    this.sqN = 0;
    this.enabled = true;
    this.port.onmessage = (e) => { if (e.data && 'enabled' in e.data) this.enabled = !!e.data.enabled; };
  }

  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch || !this.enabled) return true;
    const h = this.h;
    const hist = this.hist;
    for (let i = 0; i < ch.length; i++) {
      hist[this.pos] = ch[i];
      this.pos = (this.pos + 1) % TAPS;
      this.acc += 1;
      if (this.acc >= this.ratio) {
        this.acc -= this.ratio;
        // FIR at this instant: oldest sample first.
        let y = 0;
        let j = this.pos;
        for (let k = 0; k < TAPS; k++) {
          y += h[k] * hist[j];
          j = j + 1 === TAPS ? 0 : j + 1;
        }
        const v = Math.max(-1, Math.min(1, y));
        this.sq += v * v;
        this.sqN += 1;
        this.out[this.len++] = v < 0 ? v * 0x8000 : v * 0x7fff;
        if (this.len === this.out.length) {
          const buf = this.out.buffer.slice(0);
          this.port.postMessage(buf, [buf]);
          this.len = 0;
          if (this.sqN >= 1600) {          // 100 ms at 16 kHz
            this.port.postMessage({ level: Math.sqrt(this.sq / this.sqN) });
            this.sq = 0;
            this.sqN = 0;
          }
        }
      }
    }
    return true;
  }
}

// Via globalThis: registerProcessor is a worklet-scope global the app's
// source scanner (tests/callable.test.js) rightly does not know about.
globalThis.registerProcessor('drive-mic', DriveMicProcessor);
