// App windows: the pure state machine, geometry and storage (js/appwin-state.js).
import test from 'node:test';
import assert from 'node:assert/strict';
import * as W from '../static/js/appwin-state.js';

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

test('opening a tool makes it the one open window', () => {
  const m = W.openWin(W.empty(), 'imagestudio');
  assert.equal(m.open, 'imagestudio');
  assert.deepEqual(m.wins.map((w) => [w.id, w.state]), [['imagestudio', 'open']]);
});

test('opening a second tool minimises the first (one visible at a time, nothing closed)', () => {
  let m = W.openWin(W.empty(), 'imagestudio');
  m = W.openWin(m, 'jobboard');
  assert.equal(m.open, 'jobboard');
  assert.equal(W.find(m, 'imagestudio').state, 'min');
  assert.deepEqual(W.pills(m).map((w) => w.id), ['imagestudio']);
});

test('opening a tool that already has a window restores it - never a second copy', () => {
  let m = W.openWin(W.empty(), 'imagestudio');
  m = W.minimize(m, 'imagestudio');
  m = W.openWin(m, 'imagestudio');
  assert.equal(m.wins.length, 1);
  assert.equal(m.open, 'imagestudio');
  assert.equal(W.pills(m).length, 0);
});

test('minimise = a pill at the end of the tray; restore takes it out', () => {
  let m = W.openWin(W.empty(), 'a');
  m = W.minimize(m, 'a');
  m = W.openWin(m, 'b');
  m = W.minimize(m, 'b');
  assert.equal(m.open, null);
  assert.deepEqual(W.pills(m).map((w) => w.id), ['a', 'b']);
  m = W.restore(m, 'a');
  assert.deepEqual(W.pills(m).map((w) => w.id), ['b']);
  assert.equal(m.open, 'a');
  // Restoring b now puts a back at the END of the tray.
  m = W.restore(m, 'b');
  assert.deepEqual(W.pills(m).map((w) => w.id), ['a']);
});

test('close removes the window but keeps its remembered layout', () => {
  let m = W.openWin(W.empty(), 'a');
  m = W.setLayout(m, 'a', 'left');
  m = W.closeWin(m, 'a');
  assert.equal(m.wins.length, 0);
  assert.equal(m.open, null);
  assert.equal(W.prefsFor(m, 'a').layout, 'left');
});

test('a bad id never becomes a window', () => {
  assert.equal(W.openWin(W.empty(), '../x').wins.length, 0);
  assert.equal(W.openWin(W.empty(), '').wins.length, 0);
  assert.equal(W.openWin(W.empty(), 'A_B').wins.length, 0);
});

test('the window count is capped: the oldest pill gives way', () => {
  let m = W.empty();
  for (let i = 0; i < W.MAX_WINS + 3; i++) m = W.openWin(m, `t${i}`);
  assert.equal(m.wins.length, W.MAX_WINS);
  assert.ok(!W.find(m, 't0'));
  assert.equal(m.open, `t${W.MAX_WINS + 2}`);
});

test('filterKnown drops windows and prefs of tools this session cannot see', () => {
  let m = W.openWin(W.empty(), 'secret');
  m = W.setLayout(m, 'secret', 'full');
  m = W.openWin(m, 'mine');
  m = W.filterKnown(m, ['mine']);
  assert.deepEqual(m.wins.map((w) => w.id), ['mine']);
  assert.equal(m.prefs.secret, undefined);
  m = W.filterKnown(W.openWin(W.empty(), 'secret'), []);
  assert.equal(m.open, null);
});

test('layout: unknown refused, dock refused for a tool that may not dock', () => {
  let m = W.setLayout(W.empty(), 'a', 'sideways');
  assert.equal(m.prefs.a, undefined);
  m = W.setLayout(W.empty(), 'a', 'left', W.windowOptions({ dockable: false }));
  assert.equal(m.prefs.a, undefined);
  assert.equal(W.prefsFor(m, 'a', W.windowOptions({ layout: 'right', dockable: false })).layout, 'float');
});

test('windowOptions fills defaults and ignores junk', () => {
  assert.deepEqual(W.windowOptions(null), { ...W.OPTION_DEFAULTS });
  const o = W.windowOptions({ layout: 'full', min_width: 400, width: 1100, height: 'tall', keep_alive: false, dockable: 'yes' });
  assert.equal(o.layout, 'full');
  assert.equal(o.min_width, 400);
  assert.equal(o.width, 1100);
  assert.equal(o.height, undefined);
  assert.equal(o.keep_alive, false);
  assert.equal(o.dockable, true);
  assert.equal(W.windowOptions({ min_width: 10 }).min_width, 360);
});

test('effectiveLayout: phone = sheet; a dock that does not fit shows full', () => {
  assert.equal(W.effectiveLayout('float', { mobile: true }), 'sheet');
  assert.equal(W.effectiveLayout('left', { mobile: true }), 'sheet');
  assert.equal(W.effectiveLayout('left', { canDock: false }), 'full');
  assert.equal(W.effectiveLayout('right', { dockable: false }), 'full');
  assert.equal(W.effectiveLayout('right'), 'right');
  assert.equal(W.effectiveLayout('bogus'), 'float');
});

test('clampRect keeps a float on screen and no smaller than its minimum', () => {
  const b = { w: 1000, h: 700 };
  const o = W.windowOptions({ min_width: 400, min_height: 300 });
  assert.deepEqual(W.clampRect({ x: 900, y: 650, w: 500, h: 400 }, b, o), { x: 500, y: 300, w: 500, h: 400 });
  assert.deepEqual(W.clampRect({ x: -50, y: -50, w: 100, h: 100 }, b, o), { x: 0, y: 0, w: 400, h: 300 });
  // The window shrank under a big float: it shrinks too.
  assert.deepEqual(W.clampRect({ x: 0, y: 0, w: 1400, h: 900 }, b, o), { x: 0, y: 0, w: 1000, h: 700 });
  // The area is smaller than the minimum: fill it.
  assert.deepEqual(W.clampRect({ x: 10, y: 10, w: 500, h: 500 }, { w: 300, h: 250 }, o), { x: 0, y: 0, w: 300, h: 250 });
  // No rect yet: the initial one.
  assert.deepEqual(W.clampRect(null, b, o), W.initialRect(o, b));
});

test('initialRect: at most 60% wide and 760 tall, never under the minimum, at the inline end', () => {
  const o = W.windowOptions({ width: 1100, height: 800, min_width: 400, min_height: 360 });
  assert.deepEqual(W.initialRect(o, { w: 1500, h: 900 }), { x: 1500 - 900 - 16, y: 16, w: 900, h: 760 });
  assert.deepEqual(W.initialRect(o, { w: 1500, h: 900 }, { rtl: true }), { x: 16, y: 16, w: 900, h: 760 });
  const small = W.windowOptions({ width: 500, height: 400 });
  assert.deepEqual(W.initialRect(small, { w: 1500, h: 900 }), { x: 1500 - 500 - 16, y: 16, w: 500, h: 400 });
  // A narrow area: the minimum wins over the 60 % rule.
  const n = W.initialRect(o, { w: 600, h: 500 });
  assert.equal(n.w, 400);
  assert.ok(n.x >= 0 && n.x + n.w <= 600 && n.y + n.h <= 500);
});

test('snapZone: left/right edges dock, top fills, middle floats; a non-dockable tool only fills', () => {
  const b = { w: 1000, h: 700 };
  assert.equal(W.snapZone({ x: 5, y: 300 }, b), 'left');
  assert.equal(W.snapZone({ x: 995, y: 300 }, b), 'right');
  assert.equal(W.snapZone({ x: 500, y: 3 }, b), 'full');
  assert.equal(W.snapZone({ x: 500, y: 300 }, b), null);
  assert.equal(W.snapZone({ x: 5, y: 300 }, b, { dockable: false }), null);
  assert.equal(W.snapZone({ x: NaN, y: 1 }, b), null);
});

test('keyRect: arrows move, Alt+arrows resize, Shift is bigger, always clamped', () => {
  const b = { w: 1000, h: 700 };
  const o = W.windowOptions({ min_width: 400, min_height: 300 });
  const r = { x: 100, y: 100, w: 500, h: 400 };
  assert.deepEqual(W.keyRect('ArrowRight', r, b, o), { ...r, x: 124 });
  assert.deepEqual(W.keyRect('ArrowUp', r, b, o, { shift: true }), { ...r, y: 4 });
  assert.deepEqual(W.keyRect('ArrowLeft', r, b, o, { alt: true }), { ...r, w: 476 });
  assert.deepEqual(W.keyRect('ArrowLeft', { ...r, w: 400 }, b, o, { alt: true }), { ...r, w: 400 });
  assert.equal(W.keyRect('Enter', r, b, o), null);
});

test('serialize -> parse round trip keeps layout, rect, ratio and the open window', () => {
  let m = W.openWin(W.empty(), 'a');
  m = W.setLayout(m, 'a', 'float');
  m = W.setRect(m, 'a', { x: 10, y: 20, w: 600, h: 400 });
  m = W.openWin(m, 'b');
  m = W.setLayout(m, 'b', 'right');
  m = W.setRatio(m, 'b', 0.4);
  const back = W.parse(W.serialize(m));
  assert.equal(back.open, 'b');
  assert.deepEqual(W.pills(back).map((w) => w.id), ['a']);
  assert.deepEqual(back.prefs.a.rect, { x: 10, y: 20, w: 600, h: 400 });
  assert.equal(back.prefs.b.ratio, 0.4);
});

test('serialize never carries anything a tool said (status, badge, unsaved)', () => {
  const m = W.openWin(W.empty(), 'a');
  m.wins[0].status = 'secret prompt text';
  m.wins[0].unsaved = true;
  const s = W.serialize(m);
  assert.ok(!s.includes('secret'));
  assert.ok(!s.includes('unsaved'));
});

test('parse refuses junk piece by piece', () => {
  assert.deepEqual(W.parse('{nope'), W.empty());
  assert.deepEqual(W.parse(JSON.stringify({ v: 99, wins: [] })), W.empty());
  assert.deepEqual(W.parse('x'.repeat(W.MAX_BYTES + 1)), W.empty());
  const m = W.parse(JSON.stringify({
    v: 1,
    wins: [{ id: 'ok', state: 'open' }, { id: 'ok', state: 'min' }, { id: '<b>', state: 'open' },
      { id: 'two', state: 'open' }, { id: 'odd', state: 'gone' }],
    open: 'two',
    prefs: { ok: { layout: 'diagonal', rect: { x: 'a' }, ratio: 2 }, 'BAD ID': { layout: 'full' } },
    def: { layout: 'float' },
  }));
  assert.deepEqual(m.wins.map((w) => w.id), ['ok', 'two']);
  assert.equal(m.open, 'two');
  assert.equal(W.find(m, 'ok').state, 'min', 'two "open" windows: only the named one stays open');
  assert.deepEqual(m.prefs, { ok: {} });
  assert.equal(m.def, null);
});

test('storage keys are per profile; no profile id = nothing stored at all', () => {
  assert.equal(W.storageKey('abc123'), 'dispatch-appwin:v1:abc123');
  assert.equal(W.storageKey(null), null);
  assert.equal(W.storageKey('../../x'), null);
  const st = fakeStorage();
  withStorage(st, () => {
    assert.equal(W.save(null, W.openWin(W.empty(), 'a')), false);
    assert.equal(st._m.size, 0);
    assert.deepEqual(W.load(null), W.empty());
  });
});

test('load/save: one profile never reads another profile\'s windows', () => {
  const st = fakeStorage();
  withStorage(st, () => {
    W.save('p1', W.openWin(W.empty(), 'imagestudio'));
    assert.equal(W.load('p1').open, 'imagestudio');
    assert.deepEqual(W.load('p2'), W.empty());
  });
});

test('load/save survive blocked storage', () => {
  withStorage(fakeStorage({}, { throws: true }), () => {
    assert.deepEqual(W.load('p1'), W.empty());
    assert.equal(W.save('p1', W.empty()), false);
  });
});

test('migrateSplit: an "on" split preference becomes an inline-end dock default, then the keys go', () => {
  const st = fakeStorage({ 'dispatch-tool-split': '1', 'dispatch-tool-split-ratio': '0.42' });
  withStorage(st, () => {
    const m = W.migrateSplit(W.empty());
    assert.deepEqual(m.def, { layout: 'right', ratio: 0.42 });
    assert.equal(W.prefsFor(m, 'imagestudio').layout, 'right');
    assert.equal(W.prefsFor(m, 'imagestudio').ratio, 0.42);
    assert.equal(st._m.size, 0);
  });
  withStorage(fakeStorage({ 'dispatch-tool-split': '1' }), () => {
    assert.deepEqual(W.migrateSplit(W.empty(), { rtl: true }).def, { layout: 'left', ratio: 0.5 });
  });
  withStorage(fakeStorage({}), () => {
    assert.equal(W.migrateSplit(W.empty()).def, null);
  });
});

test('a tool with remembered prefs ignores the migrated default', () => {
  let m = { ...W.empty(), def: { layout: 'right', ratio: 0.5 } };
  m = W.setLayout(m, 'a', 'float');
  assert.equal(W.prefsFor(m, 'a').layout, 'float');
  assert.equal(W.prefsFor(m, 'b').layout, 'right');
});

test('dock sums: both minimums or no dock; the thread list steps aside first', () => {
  const o = W.windowOptions({});
  assert.equal(W.canDock(W.MIN_CHAT + W.MIN_TOOL + W.DIVIDER, o), true);
  assert.equal(W.canDock(W.MIN_CHAT + W.MIN_TOOL + W.DIVIDER - 1, o), false);
  assert.equal(W.canDock(800, W.windowOptions({ min_width: 600 })), false);
  assert.equal(W.dockMode({ withList: 1200, noList: 1520 }, o), 'list');
  assert.equal(W.dockMode({ withList: 600, noList: 920 }, o), 'nolist');
  assert.equal(W.dockMode({ withList: 300, noList: 600 }, o), null);
  const w = W.dockWidths(0.5, 1010, o);
  assert.equal(w.chat + w.tool, 1000);
  assert.deepEqual(W.dockWidths(0.99, 1010, o), { chat: W.MIN_CHAT, tool: 1000 - W.MIN_CHAT });
  assert.equal(W.dockWidths(0.5, 500, o), null);
});

test('divider keys move the seam the way the arrow points, for either side', () => {
  const o = W.windowOptions({});
  const total = 1010;
  const base = W.dockWidths(0.5, total, o).tool;
  const right = W.dockWidths(W.stepDock('ArrowRight', { ratio: 0.5, total, side: 'right', opts: o }), total, o).tool;
  assert.equal(right, base - W.KEY_STEP);
  const left = W.dockWidths(W.stepDock('ArrowRight', { ratio: 0.5, total, side: 'left', opts: o }), total, o).tool;
  assert.equal(left, base + W.KEY_STEP);
  assert.equal(W.dockWidths(W.stepDock('Home', { ratio: 0.5, total, opts: o }), total, o).tool, W.MIN_TOOL);
  assert.equal(W.stepDock('x', { ratio: 0.5, total, opts: o }), null);
});
