// Tool windows (js/appwin.js window manager + the "Tool windows" half of
// js/tools.js) rendered against the REAL index.html markup, with the fixture
// tools.json (static / app / url tools) and the backend stubbed at fetch().
// jsdom has no layout, so nothing here asserts pixel geometry.

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { domSkip } from './_require-dom.js';

const HERE = dirname(fileURLToPath(import.meta.url));
const STATIC = join(HERE, '..', 'static');
const FIXTURE = JSON.parse(readFileSync(join(HERE, 'fixtures', 'tools.json'), 'utf8'));
const HTML = readFileSync(join(STATIC, 'index.html'), 'utf8');

const require = createRequire(import.meta.url);
let jsdom = null;
try { jsdom = require('jsdom'); } catch { /* not installed — tests skip */ }
const dom = { skip: domSkip(jsdom ? false : 'jsdom is not installed (see markdown-behaviour.test.js)') };

let routes = {};

function setup(url = 'http://127.0.0.1:8765/') {
  const { JSDOM } = jsdom;
  const win = new JSDOM(HTML, { url, runScripts: 'outside-only' }).window;
  globalThis.window = win;
  globalThis.document = win.document;
  globalThis.location = win.location;
  globalThis.history = win.history;
  globalThis.CSS = win.CSS || { escape: (s) => s };
  // appwin-state.js / appwin.js read these as bare globals.
  globalThis.localStorage = win.localStorage;
  globalThis.getComputedStyle = win.getComputedStyle.bind(win);
  routes = {
    'GET /api/tools': () => [200, FIXTURE],
    'GET /api/tools/benchmark/status': () => [200, { id: 'benchmark', enabled: true, kind: 'static', mtime: Date.now() / 1000 - 600, refreshing: false }],
    'GET /api/tools/rig-panel/status': () => [200, { id: 'rig-panel', enabled: true, kind: 'url', reachable: true, refreshing: false }],
    'GET /api/tools/family-page/status': () => [200, { id: 'family-page', enabled: true, kind: 'static', refreshing: false }],
  };
  globalThis.fetch = async (u, opts = {}) => {
    const h = routes[`${(opts.method || 'GET').toUpperCase()} ${u}`];
    const [status, body] = h ? h(opts) : [404, { detail: 'Not Found' }];
    return { ok: status >= 200 && status < 300, status, statusText: String(status), headers: new win.Headers(), json: async () => body };
  };
  return win;
}

const freshTools = () => import(`../static/js/tools.js?v=${Math.random()}`);

function wire(mod, over = {}) {
  const rec = { threads: [], splitThreads: [], pushed: [], released: 0, announced: [], toasts: [], focusedChat: 0 };
  const state = { selectedBotId: 'main', activeThreadId: 't-1' };
  mod.wireTools({
    state,
    openers: {},
    openThread: (id, opts) => rec.threads.push([id, opts]),
    openThreadInSplit: (id, opts) => rec.splitThreads.push([id, opts]),
    closeToolPanes: () => {},
    builtinOn: () => true,
    builtinDot: () => '',
    restoreView: () => {},
    renderSidebar: () => mod.renderToolRail(),
    isMobile: () => (over.mobile === undefined ? true : !!over.mobile),
    groupState: () => '',
    navigate: () => {},
    toast: (m, err) => rec.toasts.push([m, !!err]),
    isBotId: (id) => id === 'main',
    openSettings: (t) => (rec.settings ||= []).push(t),
    lightbox: (x) => (rec.lightbox ||= []).push(x),
    historyPush: (...a) => { rec.pushed.push(a); return () => { rec.released += 1; }; },
    announce: (m) => rec.announced.push(m),
    focusChat: () => { rec.focusedChat += 1; },
    canDragClose: () => (over.canDragClose === undefined ? true : !!over.canDragClose),
    backToChat: over.backToChat,
  });
  return { rec, state };
}

// ---- helpers ----
const winEl = (doc, id) => doc.querySelector(`.appwin[data-tool="${id}"]`);
const frameOf = (doc, id) => { const w = winEl(doc, id); return w && w.querySelector('iframe.tool-frame'); };
const inWin = (doc, id, sel) => { const w = winEl(doc, id); return w && w.querySelector(sel); };
const pills = (doc) => [...doc.querySelectorAll('#appwin-tray .appwin-pill')];
const pillOf = (doc, id) => doc.querySelector(`#appwin-tray .appwin-pill[data-tool="${id}"]`);
const visible = (doc, id) => { const w = winEl(doc, id); return !!w && !w.hidden; };
const tick = () => new Promise((r) => setTimeout(r, 0));
const key = (win, target, k) => target.dispatchEvent(new win.KeyboardEvent('keydown', { key: k, bubbles: true, cancelable: true }));

/** A message from a frame's window, the way the browser delivers it. */
function fromFrame(win, id, data, { origin } = {}) {
  const f = frameOf(win.document, id);
  const ev = new win.MessageEvent('message', { data, origin: origin === undefined ? win.location.origin : origin });
  Object.defineProperty(ev, 'source', { value: f.contentWindow });
  win.dispatchEvent(ev);
}
const windowMsg = (win, id, op, value, o) => fromFrame(win, id, { type: 'dispatch:window', op, value }, o);

async function ready(over, url) {
  const win = setup(url);
  const mod = await freshTools();
  const w = wire(mod, over);
  await mod.loadTools();
  return { win, mod, doc: win.document, ...w };
}

/** Make the confirm path deterministic: no <dialog>.showModal, a stubbed window.confirm. */
function stubConfirm(win, answer) {
  win.HTMLDialogElement.prototype.showModal = undefined;
  const asked = [];
  win.confirm = (text) => { asked.push(text); return answer.value; };
  return asked;
}

test('open: the window is visible, a dialog labelled by its title, focus on its title bar', { skip: dom.skip }, async () => {
  const { mod, doc } = await ready();
  assert.equal(mod.openTool('benchmark'), true);
  const w = winEl(doc, 'benchmark');
  assert.equal(w.tagName, 'SECTION');
  assert.equal(w.hidden, false);
  assert.equal(w.getAttribute('role'), 'dialog');
  assert.equal(w.getAttribute('aria-modal'), null, 'non-modal');
  const label = doc.getElementById(w.getAttribute('aria-labelledby'));
  assert.equal(label.textContent, 'Benchmark Board');
  assert.equal(w.dataset.layout, 'sheet', 'phone: a sheet');
  assert.equal(doc.activeElement, w.querySelector('.appwin-handle'));
  assert.equal(doc.getElementById('appwin-layer').contains(w), true);
  assert.equal(pills(doc).length, 0);
  assert.equal(mod.toolOpen(), 'benchmark');
  assert.equal(mod.toolCoversChat(), true, 'a sheet covers the chat');
});

test('Back to chat: hides the window, leaves one pill, focus on the pill', { skip: dom.skip }, async () => {
  const { mod, doc } = await ready();
  mod.openTool('benchmark');
  inWin(doc, 'benchmark', '.appwin-min').click();
  assert.equal(visible(doc, 'benchmark'), false);
  assert.ok(winEl(doc, 'benchmark'), 'minimised, not removed');
  assert.equal(pills(doc).length, 1);
  assert.equal(doc.getElementById('appwin-tray').hidden, false);
  const main = pillOf(doc, 'benchmark').querySelector('.appwin-pill-main');
  assert.equal(doc.activeElement, main);
  assert.equal(mod.toolOpen(), null);
  assert.equal(mod.toolCoversChat(), false);
  assert.equal(doc.defaultView.location.hash, '', 'minimising clears the hash');
});

test('pill click restores the SAME iframe, not reloaded', { skip: dom.skip }, async () => {
  const { win, mod, doc } = await ready();
  mod.openTool('jobboard');
  const frame = frameOf(doc, 'jobboard');
  const touched = [];
  const sa = frame.setAttribute.bind(frame);
  frame.setAttribute = (n, v) => { touched.push(n); sa(n, v); };
  const ra = frame.removeAttribute.bind(frame);
  frame.removeAttribute = (n) => { touched.push('-' + n); ra(n); };
  inWin(doc, 'jobboard', '.appwin-min').click();
  assert.equal(frame.getAttribute('src'), '/apps/jobboard/', 'still loaded while minimised');
  pillOf(doc, 'jobboard').querySelector('.appwin-pill-main').click();
  assert.equal(visible(doc, 'jobboard'), true);
  assert.equal(frameOf(doc, 'jobboard'), frame, 'identity: the very same element');
  assert.equal(frame.isConnected, true);
  assert.equal(frame.getAttribute('src'), '/apps/jobboard/');
  assert.deepEqual(touched, [], 'no src/sandbox attribute touched: no reload');
  assert.equal(pills(doc).length, 0);
  assert.equal(win.location.hash, '#tool=jobboard');
});

test('two minimised tools: two pills, each restoring its own window', { skip: dom.skip }, async () => {
  const { mod, doc } = await ready();
  mod.openTool('benchmark');
  inWin(doc, 'benchmark', '.appwin-min').click();
  mod.openTool('jobboard');
  inWin(doc, 'jobboard', '.appwin-min').click();
  assert.deepEqual(pills(doc).map((p) => p.dataset.tool), ['benchmark', 'jobboard'], 'in minimise order');
  pillOf(doc, 'jobboard').querySelector('.appwin-pill-main').click();
  assert.equal(mod.toolOpen(), 'jobboard');
  assert.equal(visible(doc, 'benchmark'), false);
  assert.deepEqual(pills(doc).map((p) => p.dataset.tool), ['benchmark']);
  pillOf(doc, 'benchmark').querySelector('.appwin-pill-main').click();
  assert.equal(mod.toolOpen(), 'benchmark');
  assert.equal(visible(doc, 'jobboard'), false);
  assert.deepEqual(pills(doc).map((p) => p.dataset.tool), ['jobboard']);
});

test('opening a second tool minimises the first and never closes it', { skip: dom.skip }, async () => {
  const { mod, doc } = await ready();
  mod.openTool('benchmark');
  const first = frameOf(doc, 'benchmark');
  mod.openTool('rig-panel');
  assert.equal(mod.toolOpen(), 'rig-panel');
  assert.equal(visible(doc, 'benchmark'), false);
  assert.ok(pillOf(doc, 'benchmark'), 'the first is a pill');
  assert.equal(frameOf(doc, 'benchmark'), first);
  assert.equal(first.getAttribute('src'), '/tools/benchmark/', 'still loaded');
  assert.equal(doc.querySelectorAll('.appwin:not([hidden])').length, 1, 'one window on screen at a time');
});

test('✕ on a pill closes the tool; its frame goes to about:blank before src is dropped', { skip: dom.skip }, async () => {
  const { mod, doc } = await ready();
  mod.openTool('benchmark');
  const frame = frameOf(doc, 'benchmark');
  const seen = [];
  const desc = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(frame), 'src');
  Object.defineProperty(frame, 'src', { configurable: true, get() { return desc.get.call(this); }, set(v) { seen.push(['set', v]); desc.set.call(this, v); } });
  const ra = frame.removeAttribute.bind(frame);
  frame.removeAttribute = (n) => { seen.push(['remove', n]); ra(n); };
  inWin(doc, 'benchmark', '.appwin-min').click();
  pillOf(doc, 'benchmark').querySelector('.appwin-pill-x').click();
  assert.deepEqual(seen, [['set', 'about:blank'], ['remove', 'src']]);
  assert.equal(winEl(doc, 'benchmark'), null);
  assert.equal(pills(doc).length, 0);
  assert.equal(doc.getElementById('appwin-tray').hidden, true);
});

test('Delete on a focused pill closes it', { skip: dom.skip }, async () => {
  const { win, mod, doc } = await ready();
  mod.openTool('benchmark');
  inWin(doc, 'benchmark', '.appwin-min').click();
  mod.openTool('jobboard');
  inWin(doc, 'jobboard', '.appwin-min').click();
  const main = pillOf(doc, 'jobboard').querySelector('.appwin-pill-main');
  main.focus();
  key(win, main, 'Delete');
  assert.equal(winEl(doc, 'jobboard'), null);
  assert.ok(winEl(doc, 'benchmark'), 'only the focused pill closed');
  assert.deepEqual(pills(doc).map((p) => p.dataset.tool), ['benchmark']);
});

test('Esc inside the window chrome minimises (never closes)', { skip: dom.skip }, async () => {
  const { win, mod, doc } = await ready();
  mod.openTool('benchmark');
  key(win, inWin(doc, 'benchmark', '.appwin-handle'), 'Escape');
  assert.equal(visible(doc, 'benchmark'), false);
  assert.ok(winEl(doc, 'benchmark'));
  assert.ok(pillOf(doc, 'benchmark'));
});

test('busy from the frame makes ✕ ask first; keep = stays, close anyway = closes', { skip: dom.skip }, async () => {
  const { win, mod, doc } = await ready();
  const answer = { value: false };
  const asked = stubConfirm(win, answer);
  mod.openTool('jobboard');
  windowMsg(win, 'jobboard', 'busy', 2);
  inWin(doc, 'jobboard', '.appwin-close').click();
  await tick();
  assert.equal(asked.length, 1, 'it asked');
  assert.ok(winEl(doc, 'jobboard'), 'declined: the window stays');
  assert.equal(frameOf(doc, 'jobboard').getAttribute('src'), '/apps/jobboard/');
  answer.value = true;
  inWin(doc, 'jobboard', '.appwin-close').click();
  await tick();
  assert.equal(asked.length, 2);
  assert.equal(winEl(doc, 'jobboard'), null, 'confirmed: closed');
  // Not busy any more (a fresh window): no question.
  mod.openTool('jobboard');
  inWin(doc, 'jobboard', '.appwin-close').click();
  await tick();
  assert.equal(asked.length, 2, 'a quiet tool closes without asking');
  assert.equal(winEl(doc, 'jobboard'), null);
});

test('status from the frame shows under the title and on the pill', { skip: dom.skip }, async () => {
  const { win, mod, doc } = await ready();
  mod.openTool('jobboard');
  windowMsg(win, 'jobboard', 'status', '2 making');
  assert.equal(inWin(doc, 'jobboard', '.appwin-status').textContent, '2 making');
  inWin(doc, 'jobboard', '.appwin-min').click();
  assert.equal(pillOf(doc, 'jobboard').querySelector('.appwin-pill-status').textContent, '2 making');
  windowMsg(win, 'jobboard', 'status', '');
  assert.equal(pillOf(doc, 'jobboard').querySelector('.appwin-pill-status'), null, 'cleared');
});

test('window messages are routed by source and origin: another window\'s frame or a foreign origin cannot move this one', { skip: dom.skip }, async () => {
  const { win, mod, doc } = await ready();
  mod.openTool('jobboard');
  inWin(doc, 'jobboard', '.appwin-min').click();
  mod.openTool('benchmark');
  // The static tool's frame speaks (origin 'null'): it sets ITS status, not the app's.
  windowMsg(win, 'benchmark', 'status', 'mine', { origin: 'null' });
  assert.equal(inWin(doc, 'benchmark', '.appwin-status').textContent, 'mine');
  assert.equal(inWin(doc, 'jobboard', '.appwin-status').textContent, '', 'the other window is untouched');
  assert.equal(pillOf(doc, 'jobboard').querySelector('.appwin-pill-status'), null);
  // An app frame with a foreign origin, and a static frame claiming our origin: ignored.
  windowMsg(win, 'jobboard', 'status', 'evil', { origin: 'http://evil.example' });
  windowMsg(win, 'jobboard', 'status', 'opaque', { origin: 'null' });
  windowMsg(win, 'benchmark', 'status', 'ours', { origin: win.location.origin });
  windowMsg(win, 'benchmark', 'busy', 3, { origin: 'http://evil.example' });
  // A message from some other window (no frame of ours), and one with no source.
  for (const source of [win, null]) {
    const ev = new win.MessageEvent('message', { data: { type: 'dispatch:window', op: 'status', value: 'stray' }, origin: win.location.origin });
    Object.defineProperty(ev, 'source', { value: source });
    win.dispatchEvent(ev);
  }
  assert.equal(inWin(doc, 'jobboard', '.appwin-status').textContent, '');
  assert.equal(inWin(doc, 'benchmark', '.appwin-status').textContent, 'mine');
  // Unknown ops are ignored too.
  windowMsg(win, 'benchmark', 'explode', 1, { origin: 'null' });
  assert.equal(inWin(doc, 'benchmark', '.appwin-status').textContent, 'mine');
});

test('closeAllTools() removes every window and pill', { skip: dom.skip }, async () => {
  const { mod, doc } = await ready();
  mod.openTool('benchmark');
  inWin(doc, 'benchmark', '.appwin-min').click();
  mod.openTool('jobboard');
  inWin(doc, 'jobboard', '.appwin-min').click();
  mod.openTool('rig-panel');
  assert.equal(doc.querySelectorAll('.appwin').length, 3);
  mod.closeAllTools();
  assert.equal(doc.querySelectorAll('.appwin').length, 0);
  assert.equal(pills(doc).length, 0);
  assert.equal(mod.toolOpen(), null);
  assert.equal(doc.defaultView.location.hash, '');
});

test('profiles: windows are stored per profile id; another profile sees none; the first gets its own back as pills on a phone', { skip: dom.skip }, async () => {
  const { win, mod, doc } = await ready({ mobile: true });
  mod.setToolProfile('p1');
  mod.openTool('benchmark');
  inWin(doc, 'benchmark', '.appwin-min').click();
  mod.openTool('jobboard');
  const stored = win.localStorage.getItem('dispatch-appwin:v1:p1');
  assert.ok(stored, 'saved under the profile key');
  assert.doesNotMatch(stored, /Benchmark|Job Board|status/, 'ids and layout only');

  mod.setToolProfile('p2');
  assert.equal(doc.querySelectorAll('.appwin').length, 0, 'p2 sees no windows');
  assert.equal(pills(doc).length, 0);
  assert.equal(mod.toolOpen(), null);
  await mod.loadTools();
  assert.equal(doc.querySelectorAll('.appwin').length, 0);

  mod.setToolProfile('p1');
  await mod.loadTools();
  assert.equal(mod.toolOpen(), null, 'a phone never boots into a sheet');
  assert.deepEqual(pills(doc).map((p) => p.dataset.tool).sort(), ['benchmark', 'jobboard']);
  const p2 = win.localStorage.getItem('dispatch-appwin:v1:p2');
  assert.deepEqual(p2 === null ? [] : JSON.parse(p2).wins, [], 'p2 never inherited p1\'s windows');
});

test('a session with no profile id stores nothing', { skip: dom.skip }, async () => {
  const { win, mod, doc } = await ready();
  mod.openTool('benchmark');               // profile never set
  inWin(doc, 'benchmark', '.appwin-min').click();
  mod.setToolProfile(null);                 // explicit: no profile
  mod.openTool('jobboard');
  inWin(doc, 'jobboard', '.appwin-min').click();
  const keys = [];
  for (let i = 0; i < win.localStorage.length; i++) keys.push(win.localStorage.key(i));
  assert.deepEqual(keys.filter((k) => k.startsWith('dispatch-appwin')), []);
});

test('phone sheet: one history entry; its onBack minimises; the entry is released', { skip: dom.skip }, async () => {
  const { mod, doc, rec } = await ready({ mobile: true });
  mod.openTool('benchmark');
  assert.equal(rec.pushed.length, 1);
  assert.equal(rec.pushed[0][0], 'appwin');
  assert.equal(typeof rec.pushed[0][2], 'function');
  rec.pushed[0][2]();                       // the phone's Back
  assert.equal(visible(doc, 'benchmark'), false);
  assert.ok(pillOf(doc, 'benchmark'));
  assert.equal(mod.toolOpen(), null);
  // Restoring takes a fresh entry; Back to chat by the button releases it.
  pillOf(doc, 'benchmark').querySelector('.appwin-pill-main').click();
  assert.equal(rec.pushed.length, 2);
  inWin(doc, 'benchmark', '.appwin-min').click();
  assert.equal(rec.released, 1, 'the button path releases the entry it holds');
});

test('desktop: the layout menu docks right (body.appwin-dock-right) when there is room', { skip: dom.skip }, async () => {
  const { win, mod, doc } = await ready({ mobile: false });
  win.innerWidth = 2400;
  mod.openTool('benchmark');
  const w = winEl(doc, 'benchmark');
  assert.equal(w.dataset.layout, 'float');
  assert.equal(inWin(doc, 'benchmark', '.appwin-layout').hidden, false, 'the layout menu is a desktop control');
  inWin(doc, 'benchmark', '.appwin-layout').click();
  doc.querySelector('.appwin-menu-item[data-layout="right"]').click();
  assert.equal(w.dataset.layout, 'right');
  assert.ok(doc.body.classList.contains('appwin-dock-right'));
  assert.equal(mod.toolCoversChat(), false, 'a dock leaves the chat usable');
});

test('desktop: Dock right with no room falls back to full screen (the choice is only remembered)', { skip: dom.skip }, async () => {
  const { win, mod, doc, rec } = await ready({ mobile: false });
  win.innerWidth = 500;                      // too narrow for chat >= 320 + tool >= 360
  mod.openTool('benchmark');
  inWin(doc, 'benchmark', '.appwin-layout').click();
  doc.querySelector('.appwin-menu-item[data-layout="right"]').click();
  const w = winEl(doc, 'benchmark');
  assert.equal(w.dataset.layout, 'full', 'documented fallback');
  assert.equal(doc.body.classList.contains('appwin-dock-right'), false);
  assert.equal(mod.toolCoversChat(), true);
  assert.ok(rec.toasts.length >= 1, 'the user is told there is no room');
});

test('Restricted (canDragClose false): the window and its pill still have a ✕', { skip: dom.skip }, async () => {
  const { mod, doc } = await ready({ canDragClose: false });
  mod.openTool('benchmark');
  assert.ok(inWin(doc, 'benchmark', '.appwin-close'));
  inWin(doc, 'benchmark', '.appwin-min').click();
  const x = pillOf(doc, 'benchmark').querySelector('.appwin-pill-x');
  assert.ok(x, 'the one-tap way to close');
  x.click();
  assert.equal(winEl(doc, 'benchmark'), null);
});

test('an app minimised to a pill cannot switch the chat (open-thread is obeyed only from the window on screen)', { skip: dom.skip }, async () => {
  const { mod, doc, win, rec } = await ready();
  mod.openTool('jobboard');
  await tick();
  inWin(doc, 'jobboard', '.appwin-min').click();
  fromFrame(win, 'jobboard', { type: 'dispatch:open-thread', threadId: 'thr-1' });
  assert.deepEqual(rec.threads, [], 'a pill asked: nothing happens');
  pillOf(doc, 'jobboard').querySelector('.appwin-pill-main').click();
  fromFrame(win, 'jobboard', { type: 'dispatch:open-thread', threadId: 'thr-1' });
  assert.equal(rec.threads.length, 1, 'the window on screen asked: the thread opens');
  assert.equal(visible(doc, 'jobboard'), false, 'and the phone sheet steps aside to a pill');
});

test('phone: Back to chat asks the shell to show the chat; system Back does not', { skip: dom.skip }, async () => {
  let asked = 0;
  const { mod, doc, rec } = await ready({ backToChat: () => { asked += 1; } });
  mod.openTool('benchmark');
  inWin(doc, 'benchmark', '.appwin-min').click();
  assert.equal(asked, 1, 'the button: the chat comes up under the pill');
  pillOf(doc, 'benchmark').querySelector('.appwin-pill-main').click();
  const onBack = rec.pushed[rec.pushed.length - 1][2];
  onBack();
  assert.equal(asked, 1, 'system Back only minimises (it steps back, it does not jump to the chat)');
  assert.equal(visible(doc, 'benchmark'), false);
});

test('review: a url tool (someone else\'s site) cannot use the window controls; a static page can', { skip: dom.skip }, async () => {
  const { mod, doc, win } = await ready();
  mod.openTool('rig-panel');
  const f = frameOf(doc, 'rig-panel');
  const origin = new URL(f.getAttribute('src'), win.location.href).origin;
  windowMsg(win, 'rig-panel', 'status', 'Sign in again', { origin });
  windowMsg(win, 'rig-panel', 'busy', 999, { origin });
  assert.equal(inWin(doc, 'rig-panel', '.appwin-status').textContent, '');
  const asked = stubConfirm(win, { value: false });
  inWin(doc, 'rig-panel', '.appwin-close').click();
  await tick();
  assert.equal(asked.length, 0, 'no "still working" question held over Close');
  assert.equal(winEl(doc, 'rig-panel'), null);
  mod.openTool('benchmark');
  windowMsg(win, 'benchmark', 'status', 'ready', { origin: 'null' });
  assert.equal(inWin(doc, 'benchmark', '.appwin-status').textContent, 'ready');
});

test('review: a pill cannot pop the lightbox, Settings or a toast over the chat', { skip: dom.skip }, async () => {
  const { mod, doc, win, rec } = await ready();
  mod.openTool('jobboard');
  await tick();
  inWin(doc, 'jobboard', '.appwin-min').click();
  fromFrame(win, 'jobboard', { type: 'dispatch:lightbox', items: [], index: 0 });
  fromFrame(win, 'jobboard', { type: 'dispatch:open-settings', target: 'images' });
  fromFrame(win, 'jobboard', { type: 'dispatch:toast', text: 'hi' });
  assert.equal(rec.toasts.length, 0);
  assert.equal(rec.lightbox, undefined);
  assert.equal(rec.settings, undefined);
  // The same messages from the window on screen still work.
  pillOf(doc, 'jobboard').querySelector('.appwin-pill-main').click();
  fromFrame(win, 'jobboard', { type: 'dispatch:toast', text: 'hi' });
  fromFrame(win, 'jobboard', { type: 'dispatch:lightbox', items: [], index: 0 });
  assert.equal(rec.toasts.length, 1);
  assert.equal(rec.lightbox.length, 1);
});

test('review: an offline boot (network error on /api/tools) never drops the saved windows', { skip: dom.skip }, async () => {
  const win = setup();
  win.localStorage.setItem('dispatch-appwin:v1:p1', JSON.stringify({ v: 1, wins: [{ id: 'benchmark', state: 'min', order: 1 }], open: null, prefs: {}, seq: 1 }));
  const mod = await freshTools();
  wire(mod);
  mod.setToolProfile('p1');
  globalThis.fetch = async () => { throw new TypeError('offline'); };
  await mod.loadTools();
  const kept = JSON.parse(win.localStorage.getItem('dispatch-appwin:v1:p1'));
  assert.deepEqual(kept.wins.map((w) => w.id), ['benchmark'], 'still saved for when the list comes back');
});

// ---------- fix round (hands-on review) ----------

const pointer = (win, target, type, x, y, id = 1) => {
  const ev = new win.MouseEvent(type, { bubbles: true, cancelable: true, clientX: x, clientY: y, button: 0 });
  Object.defineProperty(ev, 'pointerId', { value: id });
  target.dispatchEvent(ev);
};

test('fix 10: pills keep first-open order whatever order they were minimised in', { skip: dom.skip }, async () => {
  const { mod, doc } = await ready({ mobile: false });
  mod.openTool('benchmark');
  mod.openTool('family-page');
  mod.openTool('rig-panel');
  inWin(doc, 'rig-panel', '.appwin-min').click();
  assert.deepEqual(pills(doc).map((p) => p.dataset.tool), ['benchmark', 'family-page', 'rig-panel']);
  pillOf(doc, 'benchmark').querySelector('.appwin-pill-main').click();
  inWin(doc, 'benchmark', '.appwin-min').click();
  assert.deepEqual(pills(doc).map((p) => p.dataset.tool), ['benchmark', 'family-page', 'rig-panel'], 'back in its own place');
});

test('fix 1: a sideways drag on a pill never closes it; an upward one does', { skip: dom.skip }, async () => {
  const { mod, doc, win } = await ready();
  mod.openTool('benchmark');
  inWin(doc, 'benchmark', '.appwin-min').click();
  let main = pillOf(doc, 'benchmark').querySelector('.appwin-pill-main');
  pointer(win, main, 'pointerdown', 100, 500);
  pointer(win, main, 'pointermove', 300, 505);
  assert.equal(pillOf(doc, 'benchmark').classList.contains('armed'), false, 'sideways never arms');
  pointer(win, main, 'pointerup', 300, 505);
  await tick();
  assert.ok(pillOf(doc, 'benchmark'), 'still there');
  main = pillOf(doc, 'benchmark').querySelector('.appwin-pill-main');
  pointer(win, main, 'pointerdown', 100, 500, 2);
  pointer(win, main, 'pointermove', 102, 420, 2);
  assert.equal(pillOf(doc, 'benchmark').classList.contains('armed'), true);
  pointer(win, main, 'pointerup', 102, 420, 2);
  await tick(); await tick();
  assert.equal(pillOf(doc, 'benchmark'), null, 'an upward drag closes');
  assert.ok(doc.querySelector('#appwin-tray .appwin-tray-row'), 'pills live in a scrolling row');
});

test('fix 2: a phone sheet hides the tab bar behind a solid tray strip', { skip: dom.skip }, async () => {
  const { mod, doc } = await ready();
  mod.openTool('benchmark');
  inWin(doc, 'benchmark', '.appwin-min').click();
  mod.openTool('family-page');
  assert.ok(doc.body.classList.contains('appwin-sheet'));
  assert.ok(doc.getElementById('appwin-tray').classList.contains('strip'));
  inWin(doc, 'family-page', '.appwin-min').click();
  assert.equal(doc.body.classList.contains('appwin-sheet'), false);
  assert.equal(doc.getElementById('appwin-tray').classList.contains('strip'), false);
});

test('fix 7: Back to chat by pointer focuses the chat; by keyboard the pill', { skip: dom.skip }, async () => {
  const { mod, doc, win, rec } = await ready({ mobile: false });
  mod.openTool('benchmark');
  inWin(doc, 'benchmark', '.appwin-min').dispatchEvent(new win.MouseEvent('click', { bubbles: true, detail: 1 }));
  assert.equal(rec.focusedChat, 1, 'a pointer minimise hands focus to the composer');
  mod.openTool('benchmark');
  key(win, inWin(doc, 'benchmark', '.appwin-handle'), 'Escape');
  assert.equal(doc.activeElement, pillOf(doc, 'benchmark').querySelector('.appwin-pill-main'));
});

test('fix 12: finished while minimised tints the pill, survives a reload, clears when shown', { skip: dom.skip }, async () => {
  const { mod, doc, win } = await ready({ mobile: false });
  mod.setToolProfile('p1');
  mod.openTool('benchmark');
  mod.openTool('family-page');           // benchmark -> pill
  windowMsg(win, 'benchmark', 'attention', true, { origin: 'null' });
  assert.ok(pillOf(doc, 'benchmark').classList.contains('attention'));
  assert.equal(pillOf(doc, 'benchmark').querySelector('.appwin-pill-dot'), null, 'the whole pill, not a dot');
  const saved = JSON.parse(win.localStorage.getItem('dispatch-appwin:v1:p1'));
  assert.equal(saved.wins.find((w) => w.id === 'benchmark').att, true, 'kept for a reload');
  pillOf(doc, 'benchmark').querySelector('.appwin-pill-main').click();
  inWin(doc, 'benchmark', '.appwin-min').click();
  assert.equal(pillOf(doc, 'benchmark').classList.contains('attention'), false, 'shown = seen');
});

test('fix 14: the move handle is labelled by the name; the how-to is its description; the tray is one tab stop', { skip: dom.skip }, async () => {
  const { mod, doc } = await ready({ mobile: false });
  mod.openTool('benchmark');
  const h = inWin(doc, 'benchmark', '.appwin-handle');
  assert.equal(h.getAttribute('aria-label'), 'Benchmark Board');
  const d = doc.getElementById(h.getAttribute('aria-describedby'));
  assert.ok(d.textContent && d.textContent !== h.getAttribute('aria-label'), 'a how-to sentence, not the name');
  mod.openTool('family-page');
  inWin(doc, 'family-page', '.appwin-min').click();
  const stops = [...doc.querySelectorAll('#appwin-tray button')].filter((b) => b.tabIndex === 0);
  assert.equal(stops.length, 1, 'roving tabindex: one stop for the whole tray');
});

test('fix 15: past the live limit a pill is unloaded and says it is asleep; typed text is spared', { skip: dom.skip }, async () => {
  const { mod, doc, win } = await ready();       // phone: 3 live
  routes['GET /api/tools'] = () => [200, { ...FIXTURE, tools: [...FIXTURE.tools,
    { id: 'extra-a', title: 'Extra A', icon: 'A', kind: 'static', enabled: true, entry: 'index.html', has_refresh: false },
    { id: 'extra-b', title: 'Extra B', icon: 'B', kind: 'static', enabled: true, entry: 'index.html', has_refresh: false }] }];
  await mod.loadTools();
  mod.openTool('benchmark');
  // something typed in benchmark's own document (a same-origin page)
  const fdoc = frameOf(doc, 'benchmark').contentDocument;
  if (fdoc && fdoc.body) { const ta = fdoc.createElement('textarea'); ta.value = 'half a note'; fdoc.body.append(ta); }
  for (const id of ['family-page', 'rig-panel', 'extra-a', 'extra-b']) mod.openTool(id);
  inWin(doc, 'extra-b', '.appwin-min').click();
  const asleep = pills(doc).filter((p) => p.classList.contains('asleep')).map((p) => p.dataset.tool);
  assert.ok(asleep.length >= 1, 'something went to sleep');
  if (fdoc && fdoc.body) assert.ok(!asleep.includes('benchmark'), 'the page with typed text was spared');
  const p = pillOf(doc, asleep[0]).querySelector('.appwin-pill-main');
  assert.match(p.getAttribute('aria-label'), /sleep/i);
  void win;
});

test('A: a core built-in pane opens in a window (its view moves in), minimises to a pill and goes home on close', { skip: dom.skip }, async () => {
  const win = setup();
  const mod = await freshTools();
  const calls = [];
  const view = () => win.document.getElementById('harness-view');
  const home = view().parentElement;
  const { rec } = wire(mod, { mobile: false });
  void rec;
  // main.js's adapter
  mod.wireTools({
    state: { selectedBotId: 'main', activeThreadId: 't-1' },
    openers: { harness: () => calls.push('legacy') },
    panes: { harness: { el: view, show: () => { calls.push('show'); view().classList.remove('hidden'); }, hide: () => { calls.push('hide'); view().classList.add('hidden'); } } },
    closeToolPanes: () => {}, restoreView: () => {}, builtinOn: () => true, builtinDot: () => '',
    renderSidebar: () => {}, isMobile: () => false, groupState: () => '', navigate: () => {}, toast: () => {},
    isBotId: () => false, historyPush: () => () => {}, announce: () => {}, focusChat: () => {}, canDragClose: () => true,
  });
  await mod.loadTools();
  assert.equal(mod.openTool('deepseek-harness'), true);
  const w = winEl(win.document, 'deepseek-harness');
  assert.ok(w && !w.hidden, 'a tool window');
  assert.ok(w.contains(view()), 'the pane lives in it');
  assert.deepEqual(calls, ['show'], 'shown in place: no full pane, no selection change');
  inWin(win.document, 'deepseek-harness', '.appwin-min').click();
  assert.ok(pillOf(win.document, 'deepseek-harness'), 'a pill like any tool');
  pillOf(win.document, 'deepseek-harness').querySelector('.appwin-pill-x').click();
  await tick();
  assert.equal(view().parentElement, home, 'back where it came from');
  assert.ok(view().classList.contains('hidden'));
  assert.deepEqual(calls, ['show', 'hide']);
});

test('B: a phone chat header has a Tools button that opens the tools sheet', { skip: dom.skip }, async () => {
  const { doc } = await ready();
  const b = doc.getElementById('ch-tools-btn');
  assert.ok(b && !b.hidden, 'present on the chat header');
  assert.ok(b.closest('.chat-header'));
  b.click();
  assert.equal(doc.getElementById('tools-menu').hidden, false);
  assert.ok(doc.querySelector('#tools-menu [data-tool="benchmark"]'));
});

test('review: a built-in window restored before its probe answered loads once the probe says yes', { skip: dom.skip }, async () => {
  const win = setup();
  const mod = await freshTools();
  const calls = [];
  let on = false;
  const view = () => win.document.getElementById('harness-view');
  mod.wireTools({
    state: { selectedBotId: 'main', activeThreadId: 't-1' },
    openers: { harness: () => calls.push('legacy') },
    panes: { harness: { el: view, show: () => calls.push('show'), hide: () => calls.push('hide') } },
    closeToolPanes: () => {}, restoreView: () => {}, builtinOn: () => on, builtinDot: () => '',
    renderSidebar: () => {}, isMobile: () => false, groupState: () => '', navigate: () => {}, toast: () => {},
    isBotId: () => false, historyPush: () => () => {}, announce: () => {}, focusChat: () => {}, canDragClose: () => true,
  });
  win.localStorage.setItem('dispatch-appwin:v1:p1', JSON.stringify({ v: 1, wins: [{ id: 'deepseek-harness', state: 'open', order: 1 }], open: 'deepseek-harness', prefs: {}, seq: 1 }));
  mod.setToolProfile('p1');
  await mod.loadTools();
  assert.deepEqual(calls, [], 'probe not in yet: nothing shown, not marked loaded');
  on = true;
  mod.renderToolRail();               // what main does when a probe lands
  assert.deepEqual(calls, ['show']);
});
