// The lightbox's navigation + auto-hiding chrome (js/lightbox-nav.js): the
// order the arrows step in, where they stop, the lower-zone rule, the touch
// toggle and the swipe verdict. The DOM half (openLightbox in main.js) is
// pinned by source below and driven in a real browser by the release check.

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  clampIndex, createGallery, keyDirection, inBottomZone, swipeDirection,
  createChrome, safeGalleryUrl, normalizeGalleryRequest,
} from '../static/js/lightbox-nav.js';

const STATIC = join(dirname(fileURLToPath(import.meta.url)), '..', 'static');
const MAIN = readFileSync(join(STATIC, 'js', 'main.js'), 'utf8');
const CSS = readFileSync(join(STATIC, 'app.css'), 'utf8');

// ---- stepping -------------------------------------------------------------

test('a gallery is oldest -> newest: forward is newer, back is older, both stop at the ends', () => {
  const g = createGallery(['a', 'b', 'c'], 1);
  assert.equal(g.current(), 'b');
  assert.equal(g.step(1), true);  assert.equal(g.current(), 'c');          // newer
  assert.equal(g.step(1), false); assert.equal(g.current(), 'c');          // newest: stays
  assert.equal(g.step(-1), true); assert.equal(g.current(), 'b');          // older
  g.go(0);
  assert.equal(g.step(-1), false); assert.equal(g.current(), 'a');         // oldest: stays
  assert.equal(g.canStep(-1), false);
  assert.equal(g.canStep(1), true);
});

test('the counter reads "n / total" and is empty for a single picture', () => {
  assert.equal(createGallery(['a', 'b', 'c'], 2).label(), '3 / 3');
  assert.equal(createGallery(['a'], 0).label(), '');
});

test('the start index is clamped, junk becomes 0, an empty list is safe', () => {
  assert.equal(clampIndex(9, 3), 2);
  assert.equal(clampIndex(-4, 3), 0);
  assert.equal(clampIndex('x', 3), 0);
  assert.equal(clampIndex(1, 0), 0);
  const g = createGallery([], 5);
  assert.equal(g.current(), undefined);
  assert.equal(g.step(1), false);
});

test('ArrowRight is newer (+1), ArrowLeft older (-1), nothing else steps', () => {
  assert.equal(keyDirection('ArrowRight'), 1);
  assert.equal(keyDirection('ArrowLeft'), -1);
  for (const k of ['ArrowUp', 'Enter', 'Escape', 'a', 'PageDown']) assert.equal(keyDirection(k), 0);
});

test('swiping left reveals the newer picture, right the older; zoomed or vertical never steps', () => {
  assert.equal(swipeDirection(-120, 10), 1);
  assert.equal(swipeDirection(120, -10), -1);
  assert.equal(swipeDirection(-30, 0), 0, 'too short');
  assert.equal(swipeDirection(-120, 100), 0, 'mostly vertical');
  assert.equal(swipeDirection(-300, 0, { zoomed: true }), 0, 'a zoomed picture pans instead');
});

// ---- the bottom zone ------------------------------------------------------

test('the zone is the lower quarter of the picture (at least 120 px) down to the screen edge', () => {
  const tall = { top: 100, bottom: 900, height: 800 };      // quarter = 200
  assert.equal(inBottomZone(650, tall, 1000), false);
  assert.equal(inBottomZone(701, tall, 1000), true);
  assert.equal(inBottomZone(960, tall, 1000), true, 'below the picture, where the controls sit');
  const small = { top: 100, bottom: 300, height: 200 };     // quarter = 50 -> 120 floor
  assert.equal(inBottomZone(170, small, 1000), false);
  assert.equal(inBottomZone(181, small, 1000), true);
  assert.equal(inBottomZone(50, null), false);
});

// ---- the chrome state machine --------------------------------------------

function fakeTimers() {
  let now = 0; let nextId = 1; const pending = new Map();
  return {
    setTimer: (fn, ms) => { const id = nextId++; pending.set(id, { at: now + ms, fn }); return id; },
    clearTimer: (id) => pending.delete(id),
    advance(ms) {
      now += ms;
      for (const [id, t] of [...pending]) if (t.at <= now) { pending.delete(id); t.fn(); }
    },
    get count() { return pending.size; },
  };
}

function chromeHarness() {
  const tm = fakeTimers(); const log = [];
  const c = createChrome({ onChange: (v) => log.push(v), delay: 1500, setTimer: tm.setTimer, clearTimer: tm.clearTimer });
  return { c, tm, log };
}

test('the chrome shows near the bottom or on a control, and fades 1.5 s after the pointer leaves', () => {
  const { c, tm, log } = chromeHarness();
  assert.equal(c.visible, false);
  c.pointer(false, false);                 // away from the zone while hidden: nothing happens
  assert.equal(c.visible, false);
  c.pointer(true, false);                  // lower zone
  assert.equal(c.visible, true);
  c.pointer(false, false);                 // left it
  tm.advance(1499); assert.equal(c.visible, true);
  tm.advance(2);    assert.equal(c.visible, false);
  c.pointer(false, true);                  // on a control
  assert.equal(c.visible, true);
  assert.deepEqual(log, [true, false, true]);
});

test('moving back into the zone cancels the pending fade', () => {
  const { c, tm } = chromeHarness();
  c.pointer(true); c.pointer(false);
  tm.advance(1000); c.pointer(true);
  tm.advance(5000);
  assert.equal(c.visible, true);
  assert.equal(tm.count, 0);
});

test('a touch tap toggles the chrome and it STAYS (no hover on a phone, so no timer)', () => {
  const { c, tm } = chromeHarness();
  c.tap();
  assert.equal(c.visible, true); assert.equal(c.sticky, true);
  tm.advance(60000); assert.equal(c.visible, true);
  c.tap();
  assert.equal(c.visible, false); assert.equal(c.sticky, false);
});

test('keyboard focus on a control keeps it up; blur lets it fade', () => {
  const { c, tm } = chromeHarness();
  c.focusIn();
  assert.equal(c.visible, true);
  c.pointer(false); tm.advance(10000);
  assert.equal(c.visible, true, 'focused: no fade');
  c.focusOut(); tm.advance(1501);
  assert.equal(c.visible, false);
});

test('flash() shows it once at open and lets it fade like a pointer that left', () => {
  const { c, tm } = chromeHarness();
  c.flash();
  assert.equal(c.visible, true);
  tm.advance(1501);
  assert.equal(c.visible, false);
});

test('dispose() clears the timer', () => {
  const { c, tm } = chromeHarness();
  c.flash(); c.dispose();
  assert.equal(tm.count, 0);
});

// ---- a gallery request from an app page ----------------------------------

test('only same-origin paths are showable', () => {
  assert.equal(safeGalleryUrl('/media/a.png'), true);
  for (const bad of ['https://evil.example/x.png', '//evil.example/x.png', 'javascript:alert(1)', '', '/a\\b', null, 42]) {
    assert.equal(safeGalleryUrl(bad), false, String(bad));
  }
});

test('a gallery request is validated, capped and its index follows the items that survive', () => {
  const g = normalizeGalleryRequest({
    items: [{ src: 'https://x/y.png' }, { src: '/media/a.png', caption: 'one' }, { src: '/media/b.png' }],
    index: 2,
  });
  assert.equal(g.items.length, 2);
  assert.equal(g.index, 1, 'the dropped first item shifts the index');
  assert.equal(g.items[g.index].src, '/media/b.png');
  assert.equal(g.items[0].downloadUrl, '/media/a.png');
  assert.equal(normalizeGalleryRequest({ items: [{ src: 'http://x' }] }), null);
  assert.equal(normalizeGalleryRequest(null), null);
  const many = normalizeGalleryRequest({ items: Array.from({ length: 500 }, (_, i) => ({ src: `/media/${i}.png` })), index: 499 });
  assert.equal(many.items.length, 200);
  assert.equal(many.index, 199);
});

// ---- wiring (source pins) ---------------------------------------------------

test('chat images open the lightbox with every picture of the open thread, in message order', () => {
  const fn = /function chatGalleryFor\(thumb\) \{[\s\S]*?\n\}\n/.exec(MAIN);
  assert.ok(fn, 'chatGalleryFor exists');
  assert.match(fn[0], /nimEnabled\(\)/, 'No-Image Mode gets no gallery');
  assert.match(fn[0], /closest\('#messages'\)/);
  assert.match(fn[0], /querySelectorAll\('\.msg-media img\[data-full\], \.bubble img\[data-full\]'\)/, 'message pictures only (not avatars), document order');
  assert.match(MAIN, /const chat = chatGalleryFor\(thumb\);/);
});

test('the shared lightbox steps with the arrow keys, the side buttons and a swipe, and only loads the picture on screen', () => {
  const lb = /function openLightbox\([\s\S]*?\n\}\n\n\/\/ Full-resolution avatar viewer/.exec(MAIN)[0];
  assert.match(lb, /window\.addEventListener\('keydown', onKey\)/);
  assert.match(lb, /keyDirection\(e\.key\)/);
  assert.match(lb, /swipeDirection\(/);
  assert.match(lb, /prevBtn\.addEventListener\('click'/);
  assert.match(lb, /nextBtn\.addEventListener\('click'/);
  assert.match(lb, /const useGallery = [^;]*!nimEnabled\(\)/, 'No-Image Mode never gets a gallery');
  assert.equal((lb.match(/makeMedia\(/g) || []).length, 2, 'media is created only for the shown item (first + show)');
  assert.match(lb, /removeEventListener\('keydown', onKey\)/, 'the key handler dies with the lightbox');
  assert.match(lb, /chrome\.tap\(\)/, 'a touch tap toggles the chrome');
  assert.match(lb, /chrome\.focusIn\(\)/);
});

test('Esc still closes (the global handler clicks ✕, which stays clickable programmatically)', () => {
  assert.match(MAIN, /if \(e\.key === 'Escape'\) \{\s*\n\s*const lb = \[\.\.\.document\.querySelectorAll\('\.lightbox'\)\]\.at\(-1\);/);
  assert.match(MAIN, /closeBtn = el\('button', \{ class: `lightbox-close \$\{chromeCls\}`/);
});

test('an app page gets the same viewer: dispatch:lightbox -> openAppGallery, validated, refused in No-Image Mode', () => {
  assert.match(MAIN, /lightbox: openAppGallery,/);
  const fn = /function openAppGallery\(req\) \{[\s\S]*?\n\}\n/.exec(MAIN)[0];
  assert.match(fn, /nimEnabled\(\)/);
  assert.match(fn, /normalizeGalleryRequest\(req\)/);
  const tools = readFileSync(join(STATIC, 'js', 'tools.js'), 'utf8');
  assert.match(tools, /case 'dispatch:lightbox':/);
  const sdk = readFileSync(join(STATIC, 'js', 'app-sdk.js'), 'utf8');
  assert.match(sdk, /export function lightbox\(items, index = 0\)/);
});

test('the CSS hides the chrome unless .chrome-on or focused, never fades under reduced motion, and the module is precached', () => {
  assert.match(CSS, /\.lightbox:not\(\.chrome-on\) \.lightbox-chrome:not\(:focus-visible\) \{ opacity: 0; pointer-events: none; \}/);
  assert.match(CSS, /@media \(prefers-reduced-motion: reduce\) \{\s*\n\s*\.lightbox \.lightbox-chrome \{ transition: none; \}/);
  assert.match(CSS, /\.lightbox \{ touch-action: none; \}/);
  assert.match(readFileSync(join(STATIC, 'sw.js'), 'utf8'), /'\/static\/js\/lightbox-nav\.js'/);
});
