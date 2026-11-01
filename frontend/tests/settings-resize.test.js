// The desktop Settings window can be resized (js/settings-resize.js).
//
// Pure half: the size clamp, the stored-size parser and the per-device
// persistence (which must survive blocked storage). Wiring half: the markup,
// the locale strings, the container queries and the privacy key list.
//
// Run: node --test frontend/tests/

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  MIN_W, MIN_H, MARGIN, SIZE_KEY, clampSize, defaultSize, maxSize, restoreSize, parseStored, loadSize, saveSize, nudge, edgeResize,
} from '../static/js/settings-resize.js';

const STATIC = join(dirname(fileURLToPath(import.meta.url)), '..', 'static');
const read = (f) => readFileSync(join(STATIC, f), 'utf8');

test('the default size is the old fixed one, capped to the viewport', () => {
  assert.deepEqual(defaultSize(1600, 1000), { w: 980, h: 760 });
  // 96vw / 92vh as before on a small laptop
  assert.deepEqual(defaultSize(1000, 700), { w: 960, h: 644 });
});

test('clampSize keeps the window between the minimum and the viewport minus a margin', () => {
  assert.deepEqual(clampSize({ w: 100, h: 100 }, 1600, 1000), { w: MIN_W, h: MIN_H });
  assert.deepEqual(clampSize({ w: 5000, h: 5000 }, 1600, 1000), { w: 1600 - 2 * MARGIN, h: 1000 - 2 * MARGIN });
  assert.deepEqual(clampSize({ w: 800, h: 500 }, 1600, 1000), { w: 800, h: 500 });
});

test('a viewport smaller than the minimum wins over the minimum', () => {
  assert.deepEqual(clampSize({ w: 900, h: 900 }, 700, 450), { w: 700 - 2 * MARGIN, h: 450 - 2 * MARGIN });
  assert.deepEqual(maxSize(1600, 1000), { w: 1576, h: 976 });
});

test('clampSize survives junk', () => {
  assert.deepEqual(clampSize({ w: NaN, h: undefined }, 1600, 1000), defaultSize(1600, 1000));
  assert.deepEqual(clampSize(null, 1600, 1000), defaultSize(1600, 1000));
});

test('parseStored accepts only a sane stored size', () => {
  assert.deepEqual(parseStored('{"w":800,"h":500}'), { w: 800, h: 500, max: false });
  assert.deepEqual(parseStored('{"w":800,"h":500,"max":true}'), { w: 800, h: 500, max: true });
  for (const bad of [null, '', 'x', '[]', '{}', '{"w":"a","h":5}', '{"w":-1,"h":400}', '{"w":1e9,"h":400}']) {
    assert.equal(parseStored(bad), null, String(bad));
  }
});

test('save then load round-trips through storage', () => {
  const mem = new Map();
  const store = { getItem: (k) => (mem.has(k) ? mem.get(k) : null), setItem: (k, v) => mem.set(k, v) };
  saveSize({ w: 801.6, h: 500, max: false }, store);
  assert.ok(mem.has(SIZE_KEY));
  assert.deepEqual(loadSize(store), { w: 802, h: 500, max: false });
});

test('blocked storage never throws and just means "no remembered size"', () => {
  const boom = { getItem() { throw new Error('denied'); }, setItem() { throw new Error('denied'); } };
  assert.doesNotThrow(() => saveSize({ w: 800, h: 500 }, boom));
  assert.equal(loadSize(boom), null);
  assert.equal(loadSize(null), null);
});

test('the window stays centred: dragging an edge moves the size by twice the pointer travel', () => {
  const s = { w: 900, h: 600 };
  assert.deepEqual(edgeResize(s, 'e', 50, 0, 'ltr'), { w: 1000, h: 600 });
  assert.deepEqual(edgeResize(s, 'w', -50, 0, 'ltr'), { w: 1000, h: 600 });
  assert.deepEqual(edgeResize(s, 's', 0, -30), { w: 900, h: 540 });
  assert.deepEqual(edgeResize(s, 'n', 0, -30), { w: 900, h: 660 });
  assert.deepEqual(edgeResize(s, 'se', 10, 10), { w: 920, h: 620 });
  assert.deepEqual(edgeResize(s, 'nw', -10, -10), { w: 920, h: 620 });
});

test('arrow keys nudge the size; Shift takes a bigger step', () => {
  const s = { w: 900, h: 600 };
  assert.deepEqual(nudge(s, 'ArrowRight', false), { w: 924, h: 600 });
  assert.deepEqual(nudge(s, 'ArrowLeft', false), { w: 876, h: 600 });
  assert.deepEqual(nudge(s, 'ArrowDown', true), { w: 900, h: 696 });
  assert.deepEqual(nudge(s, 'ArrowUp', false), { w: 900, h: 576 });
  assert.equal(nudge(s, 'a', false), null);
});

test('wiring: handle, maximize button, locales, container queries, privacy key', () => {
  const html = read('index.html');
  assert.match(html, /id="bm-max"/);
  assert.match(html, /data-i18n-attr="[^"]*aria-label=settings\.maximize/);
  const css = read('app.css');
  assert.match(css, /container:\s*smodal \/ inline-size/);
  assert.match(css, /@container smodal \(max-width:/);
  assert.match(css, /@container smodal \(min-width:/);
  assert.match(read('js/privacy.js'), new RegExp(`'${SIZE_KEY}'`));
  assert.match(read('js/main.js'), /from '\.\/settings-resize\.js\?v=\d+'/);
  assert.ok(read('sw.js').includes("'/static/js/settings-resize.js'"));
  for (const f of readdirSync(join(STATIC, 'locales')).filter((n) => n.endsWith('.json'))) {
    const s = JSON.parse(read(`locales/${f}`)).settings;
    for (const k of ['resize', 'maximize', 'restore']) assert.ok(s[k], `${f} settings.${k}`);
  }
});

test('restoreSize: the remembered maximum restores to the default size, a real size stays', () => {
  const m = maxSize(1600, 1000);
  assert.equal(MARGIN, 12);
  assert.deepEqual(restoreSize(m, 1600, 1000), defaultSize(1600, 1000));
  assert.deepEqual(restoreSize({ w: m.w - 1, h: m.h }, 1600, 1000), defaultSize(1600, 1000));
  assert.deepEqual(restoreSize(null, 1600, 1000), defaultSize(1600, 1000));
  assert.deepEqual(restoreSize({ w: 800, h: 500 }, 1600, 1000), { w: 800, h: 500 });
});
