// The Settings tab strip's overflow affordances (js/tabstrip.js) and the
// markup/CSS they depend on.
//
// 2026-10-14: with ten tabs the strip was wider than the 800px desktop dialog
// as well as a phone, had no scrollbar, and a mouse could not reach Security
// at all — nothing on screen said there was a tab past Health. jsdom has no
// layout, so the strip's geometry is stubbed per test (scrollWidth,
// clientWidth, scrollLeft); what is asserted is the decision the module
// makes from it: which edge buttons show, which fades apply, where a click or
// a wheel turn scrolls, and that a tab added later (an add-in's) re-measures.

import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { domSkip } from './_require-dom.js';
import { stripState, wireTabStrip } from '../static/js/tabstrip.js';

const require = createRequire(import.meta.url);
let jsdom = null;
try { jsdom = require('jsdom'); } catch { /* not installed — DOM tests skip */ }
const dom = { skip: domSkip(jsdom ? false : 'jsdom is not installed') };

const STATIC = join(dirname(fileURLToPath(import.meta.url)), '..', 'static');
const HTML = readFileSync(join(STATIC, 'index.html'), 'utf8');
const CSS = readFileSync(join(STATIC, 'app.css'), 'utf8');

function strip({ scrollWidth = 900, clientWidth = 360, rtl = false } = {}) {
  const { JSDOM } = jsdom;
  const w = new JSDOM(`<!doctype html><html dir="${rtl ? 'rtl' : 'ltr'}"><body>
    <div class="settings-tabbar">
      <button id="stab-prev" hidden></button>
      <div id="settings-tabs" role="tablist"><button class="settings-tab" data-tab="bots"></button></div>
      <button id="stab-next" hidden></button>
    </div></body></html>`, { pretendToBeVisual: true }).window;
  const list = w.document.getElementById('settings-tabs');
  const geo = { scrollWidth, clientWidth, scrollLeft: 0 };
  Object.defineProperty(list, 'scrollWidth', { get: () => geo.scrollWidth });
  Object.defineProperty(list, 'clientWidth', { get: () => geo.clientWidth });
  Object.defineProperty(list, 'scrollLeft', {
    get: () => geo.scrollLeft,
    set: (v) => {
      const slack = Math.max(0, geo.scrollWidth - geo.clientWidth);
      // Chromium/Firefox/Safari: 0..slack in LTR, 0..-slack in RTL.
      geo.scrollLeft = rtl ? Math.max(-slack, Math.min(0, v)) : Math.max(0, Math.min(slack, v));
      list.dispatchEvent(new w.Event('scroll'));
    },
  });
  // jsdom's scrollBy is a no-op; route it through the clamped setter.
  list.scrollBy = ({ left = 0 } = {}) => { list.scrollLeft = geo.scrollLeft + left; };
  const prev = w.document.getElementById('stab-prev');
  const next = w.document.getElementById('stab-next');
  const s = wireTabStrip({ list, prev, next, win: w });
  return { w, list, prev, next, geo, s };
}

test('stripState: measured from the reading start in both directions', () => {
  assert.deepEqual(stripState({ scrollWidth: 500, clientWidth: 500, scrollLeft: 0 }),
    { slack: 0, pos: 0, atStart: true, atEnd: true });
  const mid = stripState({ scrollWidth: 900, clientWidth: 360, scrollLeft: 200 });
  assert.equal(mid.atStart, false); assert.equal(mid.atEnd, false);
  // RTL scrollLeft is negative; the end is -slack.
  const rtlEnd = stripState({ scrollWidth: 900, clientWidth: 360, scrollLeft: -540 });
  assert.equal(rtlEnd.atEnd, true); assert.equal(rtlEnd.atStart, false);
});

test('overflowing strip: only the forward arrow shows at the start', { skip: dom.skip }, () => {
  const { list, prev, next } = strip();
  assert.equal(prev.hidden, true);
  assert.equal(next.hidden, false, 'nothing said there were more tabs — the bug');
  assert.ok(list.classList.contains('fade-end'));
  assert.ok(!list.classList.contains('fade-start'));
  assert.ok(list.parentElement.classList.contains('overflowing'));
});

test('a strip that fits shows no arrows and no fades', { skip: dom.skip }, () => {
  const { list, prev, next } = strip({ scrollWidth: 700, clientWidth: 798 });
  assert.equal(prev.hidden, true);
  assert.equal(next.hidden, true);
  assert.equal(list.className, '');
});

test('› pages forward to the end, then ‹ appears and › goes', { skip: dom.skip }, () => {
  const { prev, next, geo } = strip();
  next.click();
  assert.equal(geo.scrollLeft, Math.round(360 * 0.7));
  assert.equal(prev.hidden, false);
  next.click(); next.click();
  assert.equal(geo.scrollLeft, 540, 'clamped at the end');
  assert.equal(next.hidden, true);
  prev.click(); prev.click(); prev.click();
  assert.equal(geo.scrollLeft, 0);
  assert.equal(prev.hidden, true);
});

test('RTL: › moves towards the end, which is a negative scrollLeft', { skip: dom.skip }, () => {
  const { prev, next, geo } = strip({ rtl: true });
  assert.equal(next.hidden, false);
  next.click();
  assert.ok(geo.scrollLeft < 0, `scrollLeft ${geo.scrollLeft}`);
  assert.equal(prev.hidden, false);
});

test('a vertical wheel scrolls the strip sideways and is consumed', { skip: dom.skip }, () => {
  const { w, list, geo } = strip();
  const ev = new w.WheelEvent('wheel', { deltaY: 120, cancelable: true });
  list.dispatchEvent(ev);
  assert.equal(geo.scrollLeft, 120);
  assert.equal(ev.defaultPrevented, true);
  // Already at the start: a wheel back is not trapped.
  geo.scrollLeft = 0;
  const up = new w.WheelEvent('wheel', { deltaY: -120, cancelable: true });
  list.dispatchEvent(up);
  assert.equal(up.defaultPrevented, false);
  // A sideways (trackpad) gesture is the browser's own business.
  const side = new w.WheelEvent('wheel', { deltaX: 80, deltaY: 5, cancelable: true });
  list.dispatchEvent(side);
  assert.equal(side.defaultPrevented, false);
});

test('a strip that fits ignores the wheel', { skip: dom.skip }, () => {
  const { w, list } = strip({ scrollWidth: 300, clientWidth: 360 });
  const ev = new w.WheelEvent('wheel', { deltaY: 120, cancelable: true });
  list.dispatchEvent(ev);
  assert.equal(ev.defaultPrevented, false);
});

test('an add-in tab joining the strip re-measures it', { skip: dom.skip }, async () => {
  const { w, list, next, geo } = strip({ scrollWidth: 700, clientWidth: 798 });
  assert.equal(next.hidden, true);
  geo.scrollWidth = 1000;                      // what the new tab does to layout
  const b = w.document.createElement('button');
  b.className = 'settings-tab'; b.dataset.tab = 'stats';
  list.append(b);
  await new Promise((r) => setTimeout(r, 0));  // MutationObserver microtask
  assert.equal(next.hidden, false);
});

test('markup: the edge buttons flank the tablist, outside it and out of the Tab order', () => {
  const bar = HTML.slice(HTML.indexOf('<div class="settings-tabbar">'), HTML.indexOf('<!-- ---- Bots ---- -->'));
  assert.ok(bar.length > 0, 'settings-tabbar wrapper missing');
  const iPrev = bar.indexOf('id="stab-prev"');
  const iList = bar.indexOf('id="settings-tabs"');
  const iNext = bar.indexOf('id="stab-next"');
  assert.ok(iPrev > 0 && iPrev < iList && iList < iNext, 'prev, tablist, next order');
  for (const id of ['stab-prev', 'stab-next']) {
    const tag = bar.match(new RegExp(`<button[^>]*id="${id}"[^>]*>`))[0];
    assert.match(tag, /tabindex="-1"/, id);
    assert.match(tag, /aria-hidden="true"/, id);
    assert.doesNotMatch(tag, /role="tab"/, id);
  }
});

test('css: arrows overlay the strip and the active tab scrolls clear of them', () => {
  assert.match(CSS, /\.settings-tabbar\s*\{[^}]*position:\s*relative/);
  assert.match(CSS, /\.stab-nav\s*\{[^}]*position:\s*absolute/);
  assert.match(CSS, /\.stab-nav\[hidden\]\s*\{\s*display:\s*none/);
  assert.match(CSS, /\.settings-tabs\s*\{[^}]*scroll-padding-inline:/);
  assert.match(CSS, /\[dir="rtl"\] \.stab-nav svg\s*\{[^}]*scaleX\(-1\)/);
});

test('main.js wires the strip with both edge buttons', () => {
  const MAIN = readFileSync(join(STATIC, 'js', 'main.js'), 'utf8');
  assert.match(MAIN, /import \{ wireTabStrip \} from '\.\/tabstrip\.js\?v=\d+';/);
  assert.match(MAIN, /wireTabStrip\(\{\s*list,\s*prev: document\.getElementById\('stab-prev'\),\s*next: document\.getElementById\('stab-next'\),/);
});
