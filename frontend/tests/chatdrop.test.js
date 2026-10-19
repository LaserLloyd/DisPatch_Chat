// Drop files on an open chat (js/chatdrop.js): "Insert into chat" or "Upload to
// File Server" (a Restricted device: its one-way "Send a file").
//
// Run: node --test frontend/tests/

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { domSkip } from './_require-dom.js';

const HERE = dirname(fileURLToPath(import.meta.url));
const STATIC = join(HERE, '..', 'static');
const read = (f) => readFileSync(join(STATIC, f), 'utf8');
const EN = JSON.parse(read('locales/en.json'));
const require = createRequire(import.meta.url);
let jsdom = null;
try { jsdom = require('jsdom'); } catch { /* DOM tests skip */ }
const dom = { skip: domSkip(jsdom ? false : 'jsdom is not installed') };

async function boot() {
  const { JSDOM } = jsdom;
  const win = new JSDOM('<!doctype html><body><section id="chatview"><div id="messages"><p id="msg">hi</p></div></section></body>', { url: 'http://127.0.0.1:18765/' }).window;
  const put = (k, v) => Object.defineProperty(globalThis, k, { value: v, configurable: true, writable: true });
  for (const [k, v] of Object.entries({ window: win, document: win.document, location: win.location,
    navigator: win.navigator, localStorage: win.localStorage, Node: win.Node, HTMLElement: win.HTMLElement })) put(k, v);
  globalThis.fetch = async (u) => (String(u).startsWith('/static/locales/en.json')
    ? { ok: true, status: 200, json: async () => EN, headers: new win.Headers() }
    : { ok: false, status: 404, json: async () => ({}), headers: new win.Headers() });
  const i18n = await import('../static/js/i18n.js?v=4');
  await i18n.init('en');
  return { win, M: await import(`../static/js/chatdrop.js?t=${Math.random()}`) };
}

function fire(win, target, type, { files = ['a.png'], types = ['Files'] } = {}) {
  const ev = new win.Event(type, { bubbles: true, cancelable: true });
  ev.dataTransfer = { types, files: files.map((n) => ({ name: n })), dropEffect: '' };
  target.dispatchEvent(ev);
  return ev;
}

function make(win, M, over = {}) {
  const calls = [];
  const host = win.document.getElementById('chatview');
  M.createChatDrop({
    host,
    canInsert: () => (over.insert === undefined ? true : over.insert),
    canServer: () => (over.server === undefined ? 'fileserver' : over.server),
    onInsert: (f) => calls.push(['insert', f.map((x) => x.name)]),
    onServer: (f) => calls.push(['server', f.map((x) => x.name)]),
  });
  return { host, calls, overlay: () => host.querySelector('.chat-drop') };
}

test('dragging files over the chat shows two targets; text or a link drag shows nothing', { skip: dom.skip }, async () => {
  const { win, M } = await boot();
  const { host, overlay } = make(win, M);
  fire(win, host, 'dragenter', { types: ['text/plain', 'text/uri-list'] });
  assert.equal(overlay(), null, 'a text drag is not a file drop');
  const ev = fire(win, host, 'dragenter');
  assert.equal(ev.defaultPrevented, true);
  const o = overlay();
  assert.ok(o);
  assert.deepEqual([...o.querySelectorAll('[data-drop-target]')].map((n) => n.dataset.dropTarget), ['insert', 'server']);
  assert.match(o.textContent, /Insert into chat/);
  assert.match(o.textContent, /Upload to File Server/);
  assert.equal(o.querySelector('.host-notice'), null, 'no callout on the targets');
});

test('each target runs its own flow; dropping elsewhere cancels', { skip: dom.skip }, async () => {
  const { win, M } = await boot();
  const { host, calls, overlay } = make(win, M);
  fire(win, host, 'dragenter');
  fire(win, overlay().querySelector('[data-drop-target="insert"]'), 'drop', { files: ['a.png', 'b.pdf'] });
  assert.deepEqual(calls, [['insert', ['a.png', 'b.pdf']]]);
  assert.equal(overlay(), null);
  fire(win, host, 'dragenter');
  fire(win, overlay().querySelector('[data-drop-target="server"]'), 'drop');
  assert.deepEqual(calls.at(-1), ['server', ['a.png']]);
  fire(win, host, 'dragenter');
  fire(win, overlay().querySelector('.chat-drop-hint'), 'drop');
  assert.equal(calls.length, 2, 'outside both targets = cancel');
  assert.equal(overlay(), null);
});

test('flicker: crossing children keeps the overlay; leaving the chat hides it', { skip: dom.skip }, async () => {
  const { win, M } = await boot();
  const { host, overlay } = make(win, M);
  const msg = win.document.getElementById('msg');
  fire(win, host, 'dragenter');
  fire(win, msg, 'dragenter');
  fire(win, host, 'dragleave');
  assert.ok(overlay(), 'still one level deep');
  fire(win, msg, 'dragleave');
  assert.equal(overlay(), null);
});

test('Escape cancels the drag; a drop after it does nothing', { skip: dom.skip }, async () => {
  const { win, M } = await boot();
  const { host, calls, overlay } = make(win, M);
  fire(win, host, 'dragenter');
  win.dispatchEvent(new win.KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }));
  assert.equal(overlay(), null);
  fire(win, host, 'dragover');
  assert.equal(overlay(), null, 'stays cancelled for this drag');
  fire(win, host, 'drop');
  assert.deepEqual(calls, []);
});

test('roles: only the targets this profile may use; none = no overlay', { skip: dom.skip }, async () => {
  let { win, M } = await boot();
  let r = make(win, M, { server: '' });
  fire(win, r.host, 'dragenter');
  assert.deepEqual([...r.overlay().querySelectorAll('[data-drop-target]')].map((n) => n.dataset.dropTarget), ['insert']);
  ({ win, M } = await boot());
  r = make(win, M, { server: 'send' });
  fire(win, r.host, 'dragenter');
  assert.match(r.overlay().textContent, /Send to your assistants/, 'a Restricted device: its one-way drop');
  ({ win, M } = await boot());
  r = make(win, M, { insert: false, server: '' });
  const ev = fire(win, r.host, 'dragenter');
  assert.equal(r.overlay(), null);
  assert.equal(fire(win, r.host, 'drop').defaultPrevented, true, 'and the browser does not open the file');
  assert.deepEqual(r.calls, []);
  assert.ok(ev);
});

test('main.js: the overlay replaces the old straight-to-composer drop and picks the right paths', () => {
  const main = read('js/main.js');
  assert.match(main, /import \{ createChatDrop \} from '\.\/chatdrop\.js\?v=1';/);
  assert.match(main, /createChatDrop\(\{\s+host: \$\('chatview'\)/);
  assert.match(main, /onInsert: \(files\) => handleFiles\(files\)/);
  assert.match(main, /uploadToFileServer\(files\); return;/);
  assert.match(main, /openDropSheet\(\);\s+sendDrops\(files\);/);
  assert.doesNotMatch(main, /cv\.addEventListener\('drop'/);
  for (const k of ['insert_title', 'server_title', 'send_title', 'cancel_hint']) assert.ok(EN.chatdrop[k], k);
});
