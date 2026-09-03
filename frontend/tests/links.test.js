// Unit tests for custom link buttons.
//
// Run: node --test frontend/tests/
//
// The branches worth testing are the ones that keep the feature inside "opens
// a web page, or a file the local viewer is allowed to serve": target
// validation (on write AND on every read, so a hand-edited storage row cannot
// smuggle javascript: or a `..` traversal onto the rail) and the junk-tolerant
// read.
//
// links.js imports util.js, which touches `document` only inside function
// bodies — so like pins.test.js this imports cleanly under plain node,
// provided we hand it a localStorage before the module reads one. The rail and
// editor tests DO need a DOM; they use jsdom when it is installed and skip
// cleanly when it is not (same deal as markdown-behaviour.test.js):
//
//   mkdir /tmp/jsdom && cd /tmp/jsdom && npm i --no-save jsdom
//   NODE_PATH=/tmp/jsdom/node_modules node --test frontend/tests/

import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

const store = new Map();
globalThis.localStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
  removeItem: (k) => store.delete(k),
};

const { customLinks, addLink, removeLink, validUrl, validTarget,
        renderLinkRail, linksSection } =
  await import('../static/js/links.js');

const KEY = 'dispatch-custom-links';
const reset = () => store.clear();

// --- validation ------------------------------------------------------------

test('no links by default', () => {
  reset();
  assert.deepEqual(customLinks(), []);
});

test('add stores; remove forgets; unknown remove is a no-op', () => {
  reset();
  const e = addLink({ glyph: '🎛', label: 'Panel', url: 'http://example.com:8080' });
  assert.ok(e);
  assert.equal(customLinks().length, 1);
  assert.equal(customLinks()[0].label, 'Panel');
  removeLink('not-a-real-id');
  assert.equal(customLinks().length, 1);
  removeLink(e.id);
  assert.deepEqual(customLinks(), []);
});

test('only http(s) URLs or absolute local paths are accepted on write', () => {
  reset();
  for (const url of ['javascript:alert(1)', 'data:text/html,x', 'file:///etc/passwd',
                     'ftp://x', 'not a url', '', '   ', 'example.com',
                     'foo/bar', './x', '../x', '~notes/x']) {
    assert.equal(addLink({ label: 'x', url }), null, url);
  }
  assert.deepEqual(customLinks(), []);
  assert.ok(addLink({ label: 'x', url: 'https://example.com/panel' }));
  assert.ok(addLink({ label: 'y', url: '/var/home/user/site/index.html' }));
  assert.ok(addLink({ label: 'z', url: '~/reports/x.md' }));
});

test('validTarget classifies URLs and local paths', () => {
  assert.deepEqual(validTarget('https://example.com'),
    { kind: 'url', value: 'https://example.com' });
  assert.deepEqual(validTarget('http://192.0.2.1:8080/x'),
    { kind: 'url', value: 'http://192.0.2.1:8080/x' });
  assert.deepEqual(validTarget('  ~/x.md  '), { kind: 'path', value: '~/x.md' });
  assert.deepEqual(validTarget('/var/x/'), { kind: 'path', value: '/var/x/' });
  assert.deepEqual(validTarget('/'), { kind: 'path', value: '/' });
  assert.deepEqual(validTarget('~'), { kind: 'path', value: '~' });
  // A `..` SEGMENT is traversal; `..` inside a name is not.
  assert.deepEqual(validTarget('/var/..foo/x'), { kind: 'path', value: '/var/..foo/x' });
});

test('validTarget refuses traversal, control chars, junk and over-long paths', () => {
  for (const bad of ['/var/../etc/passwd', '/var/x/../y', '~/../etc', '/..',
                     '../x', 'foo/bar', '~notes',
                     '/var/x\n/etc', '/var/\u0000x', '/var/x\ty',
                     'file:///etc/passwd', 'javascript:alert(1)', 'data:text/html,x',
                     'ftp://x', 'not a url', '', '   ', null, undefined, 123]) {
    assert.equal(validTarget(bad), null, String(bad));
  }
  assert.equal(validTarget(`/${'a'.repeat(1024)}`), null);
  assert.ok(validTarget(`/${'a'.repeat(1000)}`));
});

test('validUrl still matches the http(s)-only rule (kept for compatibility)', () => {
  assert.equal(validUrl('https://example.com'), true);
  assert.equal(validUrl('http://192.0.2.1:8080/x'), true);
  assert.equal(validUrl('javascript:alert(1)'), false);
  assert.equal(validUrl('~/x.md'), false);
  assert.equal(validUrl(null), false);
});

test('a label is required; a glyph is not (empty means the line icon)', () => {
  reset();
  assert.equal(addLink({ label: '   ', url: 'https://example.com' }), null);
  const e = addLink({ label: 'Panel', url: 'https://example.com' });
  assert.equal(e.glyph, '');
});

// --- the open mode ---------------------------------------------------------

test('open defaults to tab for a URL and is honoured when asked for', () => {
  reset();
  assert.equal(addLink({ label: 'a', url: 'https://example.com' }).open, 'tab');
  assert.equal(addLink({ label: 'b', url: 'https://example.com', open: 'viewer' }).open,
    'viewer');
  // Junk mode falls back to the safe default.
  assert.equal(addLink({ label: 'c', url: 'https://example.com', open: 'nope' }).open, 'tab');
});

test('a local-path entry is always viewer, whatever was asked for or stored', () => {
  reset();
  assert.equal(addLink({ label: 'a', url: '~/x.md' }).open, 'viewer');
  assert.equal(addLink({ label: 'b', url: '/var/x', open: 'tab' }).open, 'viewer');
  reset();
  store.set(KEY, JSON.stringify([
    { id: 'p', label: 'path', url: '~/x.md', open: 'tab' },
  ]));
  assert.equal(customLinks()[0].open, 'viewer');
});

test('legacy rows without an open mode read as tab', () => {
  reset();
  store.set(KEY, JSON.stringify([
    { id: 'a', glyph: '', label: 'old', url: 'https://example.com' },
  ]));
  assert.equal(customLinks()[0].open, 'tab');
});

// --- the junk-tolerant read ------------------------------------------------

test('a hand-edited storage row cannot smuggle a bad target onto the rail', () => {
  reset();
  store.set(KEY, JSON.stringify([
    { id: 'a', glyph: '💣', label: 'evil', url: 'javascript:alert(1)' },
    { id: 'x', glyph: '💣', label: 'traversal', url: '/var/../etc/passwd' },
    { id: 'y', glyph: '💣', label: 'file url', url: 'file:///etc/shadow' },
    { id: 'b', glyph: '✅', label: 'fine', url: 'https://example.com' },
    { id: 'c', label: 'no url' },
    'not even an object',
  ]));
  const links = customLinks();
  assert.equal(links.length, 1);
  assert.equal(links[0].id, 'b');
});

test('junk storage reads as no links, not a throw', () => {
  reset();
  store.set(KEY, '{not json');
  assert.deepEqual(customLinks(), []);
  store.set(KEY, '"a string, not a list"');
  assert.deepEqual(customLinks(), []);
});

// --- rail + editor (need a DOM) --------------------------------------------

const require = createRequire(import.meta.url);
let JSDOM = null;
try { ({ JSDOM } = require('jsdom')); } catch { /* not installed — tests skip */ }
const domTest = (name, fn) => test(name, { skip: JSDOM ? false : 'jsdom not installed' }, fn);

function withDom(fn) {
  const dom = new JSDOM('<!doctype html><div id="rail"><button id="manage-bots"></button></div>');
  const prev = globalThis.document;
  globalThis.document = dom.window.document;
  try { return fn(dom.window.document); } finally { globalThis.document = prev; }
}

domTest('rail: an http(s) tab link is an <a> that opens a new tab', () => {
  reset();
  addLink({ label: 'Panel', url: 'https://example.com/panel' });
  withDom((doc) => {
    const rail = doc.querySelector('#rail');
    renderLinkRail(rail, { decoy: false, openViewer: () => {} });
    const btns = rail.querySelectorAll('.link-btn');
    assert.equal(btns.length, 1);
    assert.equal(btns[0].tagName, 'A');
    assert.equal(btns[0].getAttribute('target'), '_blank');
    assert.equal(btns[0].getAttribute('rel'), 'noopener noreferrer');
    assert.equal(btns[0].getAttribute('href'), 'https://example.com/panel');
  });
});

domTest('rail: a local path is a button that calls openViewer({path})', () => {
  reset();
  addLink({ label: 'Report', url: '~/reports/x.md' });
  withDom((doc) => {
    const rail = doc.querySelector('#rail');
    const calls = [];
    renderLinkRail(rail, { decoy: false, openViewer: (a) => calls.push(a) });
    const btn = rail.querySelector('.link-btn');
    assert.equal(btn.tagName, 'BUTTON');
    assert.equal(btn.getAttribute('type'), 'button');
    assert.equal(btn.getAttribute('href'), null);
    btn.dispatchEvent(new doc.defaultView.Event('click'));
    assert.deepEqual(calls, [{ path: '~/reports/x.md' }]);
  });
});

domTest('rail: a viewer-mode URL is a button that calls openViewer({url})', () => {
  reset();
  addLink({ label: 'Site', url: 'https://example.com/', open: 'viewer' });
  withDom((doc) => {
    const rail = doc.querySelector('#rail');
    const calls = [];
    renderLinkRail(rail, { decoy: false, openViewer: (a) => calls.push(a) });
    const btn = rail.querySelector('.link-btn');
    assert.equal(btn.tagName, 'BUTTON');
    btn.dispatchEvent(new doc.defaultView.Event('click'));
    assert.deepEqual(calls, [{ url: 'https://example.com/' }]);
  });
});

domTest('rail: with no openViewer, a viewer URL falls back to <a> and a path is skipped', () => {
  reset();
  addLink({ label: 'Site', url: 'https://example.com/', open: 'viewer' });
  addLink({ label: 'Report', url: '~/reports/x.md' });
  withDom((doc) => {
    const rail = doc.querySelector('#rail');
    renderLinkRail(rail, { decoy: false });
    const btns = [...rail.querySelectorAll('.link-btn')];
    assert.equal(btns.length, 1);
    assert.equal(btns[0].tagName, 'A');
    assert.equal(btns[0].getAttribute('href'), 'https://example.com/');
  });
});

domTest('rail: Safe Mode renders nothing, and a rebuild replaces wholesale', () => {
  reset();
  addLink({ label: 'Panel', url: 'https://example.com/panel' });
  addLink({ label: 'Report', url: '~/x.md' });
  withDom((doc) => {
    const rail = doc.querySelector('#rail');
    renderLinkRail(rail, { decoy: false, openViewer: () => {} });
    assert.equal(rail.querySelectorAll('.link-btn').length, 2);
    renderLinkRail(rail, { decoy: false, openViewer: () => {} });
    assert.equal(rail.querySelectorAll('.link-btn').length, 2);
    renderLinkRail(rail, { decoy: true, openViewer: () => {} });
    assert.equal(rail.querySelectorAll('.link-btn').length, 0);
    assert.ok(rail.querySelector('#manage-bots'));
  });
});

const t = (k) => k;

domTest('editor: the add row has an "open in viewer" checkbox and a path placeholder', () => {
  reset();
  withDom(() => {
    const sec = linksSection(t, {});
    const url = sec.querySelector('.bm-links-in-url');
    assert.equal(url.getAttribute('placeholder'), 'links.url_or_path');
    const box = sec.querySelector('.bm-links-in-viewer');
    assert.ok(box, 'checkbox present');
    assert.equal(box.type, 'checkbox');
    assert.equal(box.checked, false);
    assert.equal(box.disabled, false);
  });
});

domTest('editor: typing a local path checks and disables the viewer checkbox', () => {
  reset();
  withDom((doc) => {
    const sec = linksSection(t, {});
    const url = sec.querySelector('.bm-links-in-url');
    const box = sec.querySelector('.bm-links-in-viewer');
    url.value = '~/reports/x.md';
    url.dispatchEvent(new doc.defaultView.Event('input'));
    assert.equal(box.checked, true);
    assert.equal(box.disabled, true);
    url.value = 'https://example.com';
    url.dispatchEvent(new doc.defaultView.Event('input'));
    assert.equal(box.disabled, false);
  });
});

domTest('editor: adding stores the chosen open mode; invalid input shows need_url', () => {
  reset();
  withDom((doc) => {
    const sec = linksSection(t, {});
    const label = sec.querySelector('.bm-links-in-label');
    const url = sec.querySelector('.bm-links-in-url');
    const box = sec.querySelector('.bm-links-in-viewer');
    const add = sec.querySelector('.bm-links-add');
    const err = sec.querySelector('.bm-links-error');

    label.value = 'Bad';
    url.value = 'file:///etc/passwd';
    add.dispatchEvent(new doc.defaultView.Event('click'));
    assert.equal(err.hidden, false);
    assert.equal(err.textContent, 'links.need_url');
    assert.deepEqual(customLinks(), []);

    label.value = 'Site';
    url.value = 'https://example.com';
    box.checked = true;
    add.dispatchEvent(new doc.defaultView.Event('click'));
    assert.equal(customLinks().length, 1);
    assert.equal(customLinks()[0].open, 'viewer');
    assert.equal(err.hidden, true);
    assert.equal(url.value, '');
  });
});

domTest('editor: viewer-mode rows carry a marker glyph', () => {
  reset();
  addLink({ label: 'Report', url: '~/x.md' });
  addLink({ label: 'Panel', url: 'https://example.com' });
  withDom(() => {
    const sec = linksSection(t, {});
    const marks = sec.querySelectorAll('.link-viewer-mark');
    assert.equal(marks.length, 1);
    assert.equal(marks[0].getAttribute('title'), 'links.open_in_viewer');
    const rows = sec.querySelectorAll('.bm-links-item');
    assert.equal(rows.length, 2);
    assert.ok(rows[0].querySelector('.link-viewer-mark'));
    assert.equal(rows[1].querySelector('.link-viewer-mark'), null);
  });
});
