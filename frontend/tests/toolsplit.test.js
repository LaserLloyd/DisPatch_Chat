// Split view for the generic tool pane: the pure sums (js/toolsplit.js) and the
// wiring that has to stay put (the toggle, the divider and its aria, the pins).
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import * as S from '../static/js/toolsplit.js';

const STATIC = join(dirname(fileURLToPath(import.meta.url)), '..', 'static');
const read = (p) => readFileSync(join(STATIC, p), 'utf8');

function fakeStorage(init = {}, { throws = false } = {}) {
  const m = new Map(Object.entries(init));
  const guard = () => { if (throws) throw new Error('blocked'); };
  return {
    getItem: (k) => { guard(); return m.has(k) ? m.get(k) : null; },
    setItem: (k, v) => { guard(); m.set(k, String(v)); },
    removeItem: (k) => { guard(); m.delete(k); },
    _m: m,
  };
}
function withStorage(st, fn) {
  const old = Object.getOwnPropertyDescriptor(globalThis, 'localStorage');
  Object.defineProperty(globalThis, 'localStorage', { value: st, configurable: true, writable: true });
  try { return fn(); } finally {
    if (old) Object.defineProperty(globalThis, 'localStorage', old); else delete globalThis.localStorage;
  }
}

test('room and canSplit: minimums 320 + 360 + a 10px divider', () => {
  assert.equal(S.room(1000), 990);
  assert.equal(S.canSplit(690), true);    // 680 = 320 + 360 exactly
  assert.equal(S.canSplit(689), false);
  assert.equal(S.canSplit(NaN), false);
  assert.equal(S.canSplit(undefined), false);
});

test('shouldSplit needs the preference, a desktop layout and room', () => {
  assert.equal(S.shouldSplit({ pref: true, mobile: false, total: 1000 }), true);
  assert.equal(S.shouldSplit({ pref: false, mobile: false, total: 1000 }), false);
  assert.equal(S.shouldSplit({ pref: true, mobile: true, total: 1400 }), false);
  assert.equal(S.shouldSplit({ pref: true, mobile: false, total: 600 }), false);
});

test('clampRatio keeps both sides over their minimum', () => {
  const total = 1010;                       // room 1000
  assert.equal(S.clampRatio(0.5, total), 0.5);
  assert.equal(S.clampRatio(0.01, total), 0.36);          // tool min 360/1000
  assert.ok(Math.abs(S.clampRatio(0.99, total) - 0.68) < 1e-9);          // chat min 320/1000
  assert.equal(S.clampRatio('junk', total), 0.5);         // default
  assert.equal(S.clampRatio(0.5, 500), null);             // cannot split
});

test('widths sum to the room and respect the minimums', () => {
  const w = S.widths(0.5, 1010);
  assert.deepEqual(w, { chat: 500, tool: 500 });
  const lo = S.widths(0, 1010);
  assert.equal(lo.tool, 360);
  assert.equal(lo.chat + lo.tool, 1000);
  const hi = S.widths(1, 1010);
  assert.equal(hi.chat, 320);
  assert.equal(S.widths(0.5, 400), null);
});

test('ratioForChat inverts widths', () => {
  assert.equal(S.ratioForChat(600, 1010), 0.4);
  assert.ok(Math.abs(S.ratioForChat(10, 1010) - 0.68) < 1e-9);           // clamped to the chat minimum
});

test('stepRatio: arrows move the divider, Shift is bigger, Home/End jump, others are ignored', () => {
  const base = { ratio: 0.5, total: 1010 };           // chat 500
  assert.equal(S.widths(S.stepRatio('ArrowRight', base), 1010).chat, 524);
  assert.equal(S.widths(S.stepRatio('ArrowLeft', base), 1010).chat, 476);
  assert.equal(S.widths(S.stepRatio('ArrowRight', { ...base, shift: true }), 1010).chat, 596);
  // right-to-left: the chat is on the right, so the sign flips
  assert.equal(S.widths(S.stepRatio('ArrowRight', { ...base, rtl: true }), 1010).chat, 476);
  assert.equal(S.widths(S.stepRatio('Home', base), 1010).chat, 320);
  assert.equal(S.widths(S.stepRatio('End', base), 1010).chat, 640);
  assert.equal(S.stepRatio('a', base), null);
  assert.equal(S.stepRatio('ArrowLeft', { ratio: 0.5, total: 300 }), null);
  // pressed at the end of the range it stays there
  const atMin = S.stepRatio('ArrowLeft', { ratio: S.stepRatio('Home', base), total: 1010 });
  assert.equal(S.widths(atMin, 1010).chat, 320);
});

test('storage: preference and ratio round-trip, bad values fall back, a blocked store never throws', () => {
  const st = fakeStorage();
  withStorage(st, () => {
    assert.equal(S.readPref(), false);
    assert.equal(S.writePref(true), true);
    assert.equal(st._m.get('dispatch-tool-split'), '1');
    assert.equal(S.readPref(), true);
    S.writePref(false);
    assert.equal(st._m.has('dispatch-tool-split'), false);
    assert.equal(S.readRatio(), 0.5);
    assert.equal(S.writeRatio(0.4123456), true);
    assert.equal(st._m.get('dispatch-tool-split-ratio'), '0.4123');
    assert.equal(S.readRatio(), 0.4123);
    for (const v of ['abc', '0', '1', '-2', '3']) {
      st._m.set('dispatch-tool-split-ratio', v);
      assert.equal(S.readRatio(), 0.5, `bad value ${v}`);
    }
    assert.equal(S.writeRatio(NaN), false);
    assert.equal(S.writeRatio(1.5), false);
  });
  withStorage(fakeStorage({}, { throws: true }), () => {
    assert.equal(S.readPref(), false);
    assert.equal(S.writePref(true), false);
    assert.equal(S.readRatio(), 0.5);
    assert.equal(S.writeRatio(0.4), false);
  });
});

test('wiring: the toggle and the divider exist with their aria, and the pins are in place', () => {
  const html = read('index.html').replace(/<!--[\s\S]*?-->/g, '');
  const btn = /<button[^>]*id="tool-split"[^>]*>/.exec(html);
  assert.ok(btn, '#tool-split toggle in the tool header');
  assert.match(btn[0], /aria-pressed="false"/);
  assert.match(btn[0], /aria-label="[^"]+"/);
  assert.match(btn[0], /title="[^"]+"/);
  assert.match(btn[0], /aria-label=tools\.split/);
  const div = /<div[^>]*id="tool-split-divider"[^>]*>/.exec(html);
  assert.ok(div, '#tool-split-divider');
  for (const re of [/role="separator"/, /aria-orientation="vertical"/, /aria-valuenow="/, /aria-valuemin="/, /aria-valuemax="/, /tabindex="0"/, /aria-label="[^"]+"/]) {
    assert.match(div[0], re);
  }
  assert.ok(/id="tool-return"/.test(html), 'the phone chip');
  const priv = read('js/privacy.js');
  assert.match(priv, /'dispatch-tool-split',/);
  assert.match(priv, /'dispatch-tool-split-ratio',/);
  assert.match(read('sw.js'), /'\/static\/js\/toolsplit\.js'/);
  assert.match(read('js/tools.js'), /from '\.\/toolsplit\.js\?v=1'/);
  const main = read('js/main.js');
  assert.match(main, /closeToolPanes\(toolSplitActive\(\) \? 'tool' : undefined\)/, 'picking another chat keeps the tool');
  // frame security is unchanged: an app's origin + source checks stay
  const tools = read('js/tools.js');
  assert.match(tools, /ev\.origin !== location\.origin \|\| ev\.source !== frame\.contentWindow/);
});

test('every locale carries the split strings', () => {
  for (const l of ['en', 'de', 'es', 'fr', 'pt', 'ja', 'zh', 'ar']) {
    const t = JSON.parse(read(`locales/${l}.json`)).tools;
    for (const k of ['split', 'split_narrow', 'to_chat', 'back_to', 'divider']) {
      assert.ok(typeof t[k] === 'string' && t[k].length > 0, `${l}.tools.${k}`);
    }
    assert.match(t.back_to, /\{title\}/, `${l} keeps {title}`);
  }
});
