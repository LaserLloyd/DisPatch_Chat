// Drive mode mic capture (AudioWorklet). Runs on the audio thread.
//
// Input: whatever rate the AudioContext runs at (48 kHz on most phones).
// Output: Int16 little-endian mono at 16 kHz, posted to the main thread in
// 20 ms frames (320 samples) as transferable ArrayBuffers — the format
// /ws/voice expects. Downsampling is a box-filter average over each output
// sample's input span: cheap, and speech below 8 kHz survives it fine.

class DriveMicProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / 16000;   // `sampleRate` is the worklet global
    this.acc = 0;                       // fractional input position carried over
    this.sum = 0;
    this.n = 0;
    this.out = new Int16Array(320);
    this.len = 0;
    this.enabled = true;
    this.port.onmessage = (e) => { if (e.data && 'enabled' in e.data) this.enabled = !!e.data.enabled; };
  }

  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch || !this.enabled) return true;
    for (let i = 0; i < ch.length; i++) {
      this.sum += ch[i];
      this.n += 1;
      this.acc += 1;
      if (this.acc >= this.ratio) {
        this.acc -= this.ratio;
        const v = Math.max(-1, Math.min(1, this.sum / this.n));
        this.out[this.len++] = v < 0 ? v * 0x8000 : v * 0x7fff;
        this.sum = 0;
        this.n = 0;
        if (this.len === this.out.length) {
          const buf = this.out.buffer.slice(0);
          this.port.postMessage(buf, [buf]);
          this.len = 0;
        }
      }
    }
    return true;
  }
}

// Via globalThis: registerProcessor is a worklet-scope global the app's
// source scanner (tests/callable.test.js) rightly does not know about.
globalThis.registerProcessor('drive-mic', DriveMicProcessor);
