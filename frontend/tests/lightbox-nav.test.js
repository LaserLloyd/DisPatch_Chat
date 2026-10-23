// The lightbox's navigation + auto-hiding chrome (js/lightbox-nav.js): the
// order the arrows step in, where they stop, the over-the-picture rule, the touch
// toggle and the swipe verdict. The DOM half (openLightbox in main.js) is
// pinned by source below and driven in a real browser by the release check.

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  clampIndex, createGallery, keyDirection, overMedia, swipeDirection,
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

// ---- over the picture ----------------------------------------------------

test('the controls show only while the pointer is over the picture', () => {
  const r = { left: 100, right: 900, top: 100, bottom: 700, width: 800, height: 600 };
  assert.equal(overMedia(500, 400, r), true);
  assert.equal(overMedia(100, 100, r), true, 'the edge counts');
  assert.equal(overMedia(50, 400, r), false, 'beside the picture');
  assert.equal(overMedia(500, 760, r), false, 'below the picture');
  assert.equal(overMedia(500, 400, null), false);
  assert.equal(overMedia(500, 400, { left: 0, right: 0, top: 0, bottom: 0, width: 0, height: 0 }), false);
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

test('the chrome shows over the picture or on a control, and fades 1.5 s (default) after the pointer leaves', () => {
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

// ---- zoom and details (4.0) ------------------------------------------------

import {
  clampZoom, zoomAt, clampPan, zoomLabel, formatBytes, fileNameFromUrl, typeLabel,
  detailRows, itemPrompt, cleanInfo, normalizeGalleryRequest as normGallery, ZOOM_MAX,
} from '../static/js/lightbox-nav.js';

test('zooming about a point keeps that point still', () => {
  const rect = { left: 100, top: 100, width: 400, height: 300 };
  const v = zoomAt({ s: 1, tx: 0, ty: 0 }, 2, 200, 150, rect);
  // The point (200,150) is 100 px left / 100 px up of the centre (300,250): at 2x it must
  // still be under the cursor, so the picture moves by the same offset.
  assert.deepEqual(v, { s: 2, tx: 100, ty: 100 });
  const zoomed = { left: 100 + 100 - 200, top: 100 + 100 - 150, width: 800, height: 600 };
  const back = zoomAt(v, 1, 200, 150, zoomed);
  assert.deepEqual(back, { s: 1, tx: 0, ty: 0 }, 'back to fit is centred');
  assert.equal(clampZoom(99), ZOOM_MAX);
  assert.equal(clampZoom(0.2), 1);
  assert.equal(zoomLabel(2.5), '250%');
});

test('a zoomed picture can only be panned as far as it sticks out', () => {
  const base = { w: 400, h: 300 }, vp = { w: 1000, h: 800 };
  assert.deepEqual(clampPan({ s: 2, tx: 500, ty: -500 }, base, vp), { s: 2, tx: 0, ty: 0 }, 'still fits: centred');
  assert.deepEqual(clampPan({ s: 4, tx: 9999, ty: -9999 }, base, vp), { s: 4, tx: 300, ty: -200 });
});

test('the details rows say what is known, in order, and drop the rest', () => {
  const item = { src: '/media/ab%20c.png?x=1', info: { prompt: 'a red fox', model: 'img-1', seed: '42', source: 'Chat' } };
  const rows = detailRows(item, { width: 1024, height: 768, bytes: 2048, mime: 'image/png' });
  assert.deepEqual(rows.map((r) => r.key), ['lightbox.d_file', 'lightbox.d_size', 'lightbox.d_source', 'lightbox.d_model', 'lightbox.d_seed']);
  assert.equal(rows[0].value, 'ab c.png');
  assert.equal(rows[1].value, '1024 × 768 · 2.0 KB · PNG');
  assert.equal(itemPrompt(item), 'a red fox');
  assert.equal(itemPrompt({ caption: ' just a caption ' }), 'just a caption');
  assert.equal(formatBytes(-1), null);
  assert.equal(formatBytes(5 * 1024 * 1024), '5.0 MB');
  assert.equal(typeLabel('image/jpeg'), 'JPEG');
  assert.equal(fileNameFromUrl(42), '');
});

test('an app page can send details, strings only and capped', () => {
  const g = normGallery({ items: [{ src: '/media/a.png', info: { prompt: 'x'.repeat(5000), seed: 7, evil: { a: 1 }, bot: ['no'] } }] });
  assert.equal(g.items[0].info.prompt.length, 4000);
  assert.equal(g.items[0].info.seed, '7');
  assert.ok(!('evil' in g.items[0].info) && !('bot' in g.items[0].info));
  assert.deepEqual(cleanInfo(null), {});
});

// ---- review #2 (LB-1..LB-6) -------------------------------------------------

test('a mouse press never focuses a viewer control (a later key would pin the faded chrome)', () => {
  assert.match(MAIN, /addEventListener\('mousedown', \(e\) => \{ if \(e\.target\.closest\('button, a'\)\) e\.preventDefault\(\); \}\)/);
});

test('copy confirmations show above the viewer', () => {
  const z = (sel) => Number(new RegExp(`\\n${sel.replace('.', '\\.')} \\{[^}]*z-index: (\\d+)`).exec(CSS)[1]);
  assert.ok(z('.toast') > z('.lightbox'), 'toast z-index must beat .lightbox');
});

test('RTL: the counter takes the page direction (no dir=ltr on the box) and the left arrow clears the panel', () => {
  assert.doesNotMatch(MAIN, /lightbox-counter \$\{chromeCls\}`, 'aria-live': 'polite', dir: 'ltr'/);
  assert.match(MAIN, /el\('bdi', \{ dir: 'ltr', text: nav\.label\(\) \}\)/);
  assert.match(CSS, /:root\[dir="rtl"\] \.lightbox\.details-open \.lightbox-prev \{ left: 354px; \}/);
});

test('opening the viewer closes the thread menu; Esc leaves Details alone while a dialog is open', () => {
  const fn = MAIN.slice(MAIN.indexOf('function openLightbox('));
  assert.match(fn.slice(0, 400), /toggleThreadMenu\(false\)/);
  assert.match(fn, /if \(e\.key !== 'Escape' \|\| !detailsOpen\) return;[\s\S]{0,140}dialog\[open\]/);
});

test('the Details toggle has a name when its text is hidden on a phone', () => {
  assert.match(MAIN, /lb-details-toggle', type: 'button', 'aria-expanded': 'false',\s*'aria-label': t\('lightbox\.details'\)/);
});

test('a viewer button tapped right after a pan answers its own tap (Chrome sends no click then)', () => {
  assert.match(MAIN, /lb\.addEventListener\('touchend', \(e\) => \{[\s\S]{0,500}e\.preventDefault\(\);\s*s\.ctl\.click\(\);/);
});
