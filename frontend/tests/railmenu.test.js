// The bar vs "Tools & more" (js/railmenu.js + the popup half in js/tools.js).
//
// Run: node --test frontend/tests/
//
// Pins: the default grouping (profile chip, Tasks, File Server and the theme
// button in the popup; Lock/Unlock, send-a-file and Settings on the bar);
// moving in, out and along both lists; the layout persisting per device; a
// Restricted view getting the shipped bar whatever is stored; the real
// elements moving (so their own visibility rules still apply); drag-and-drop
// between the bar and the popup; the no-drag "Arrange the bar" dialog; and
// the Tools button showing as "Tools & more" when rail buttons live there.

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
const HTML = read('index.html');
const EN = JSON.parse(read('locales/en.json'));
const require = createRequire(import.meta.url);
let jsdom = null;
try { jsdom = require('jsdom'); } catch { /* DOM tests skip */ }
const dom = { skip: domSkip(jsdom ? false : 'jsdom is not installed') };

async function boot() {
  const { JSDOM } = jsdom;
  const win = new JSDOM(HTML, { url: 'http://127.0.0.1:18765/', runScripts: 'outside-only' }).window;
  const put = (k, v) => Object.defineProperty(globalThis, k, { value: v, configurable: true, writable: true });
  for (const [k, v] of Object.entries({ window: win, document: win.document, location: win.location,
    navigator: win.navigator, localStorage: win.localStorage, Node: win.Node, HTMLElement: win.HTMLElement,
    CSS: win.CSS || { escape: (s) => s } })) put(k, v);
  globalThis.fetch = async (u) => (String(u).startsWith('/static/locales/en.json')
    ? { ok: true, status: 200, json: async () => EN, headers: new win.Headers() }
    : { ok: false, status: 404, json: async () => ({}), headers: new win.Headers() });
  const i18n = await import('../static/js/i18n.js?v=4');
  await i18n.init('en');
  win.HTMLDialogElement.prototype.showModal = function showModal() { this.open = true; };
  return win;
}
const R = () => import(`../static/js/railmenu.js?t=${Math.random()}`);
const ids = (host) => [...host.children].map((n) => n.id).filter(Boolean);

test('default: profile chip, Tasks, File Server and theme in the popup; lock, send-a-file and Settings on the bar', async () => {
  const M = await R();
  assert.deepEqual(M.defaultLayout(), {
    rail: ['lock-now', 'unlock-btn', 'drop-btn', 'manage-bots'],
    menu: ['profile-chip', 'tasks-btn', 'fs-chip', 'theme-toggle'],
  });
  for (const id of M.RAIL_ITEMS) assert.ok(HTML.includes(`id="${id}"`), `${id} is a real rail button`);
});

test('moving in, out and along both lists; normalize repairs junk and keeps new ids', async () => {
  const M = await R();
  let l = M.defaultLayout();
  l = M.moveItem(l, 'manage-bots', 'menu', 'tasks-btn');
  assert.deepEqual(l.menu, ['profile-chip', 'manage-bots', 'tasks-btn', 'fs-chip', 'theme-toggle']);
  l = M.moveItem(l, 'profile-chip', 'rail', 'lock-now');
  assert.deepEqual(l.rail, ['profile-chip', 'lock-now', 'unlock-btn', 'drop-btn']);
  l = M.moveItem(l, 'tasks-btn', 'rail', null);
  assert.equal(l.rail.at(-1), 'tasks-btn');
  l = M.nudgeItem(l, 'tasks-btn', -1);
  assert.deepEqual(l.rail.slice(-2), ['tasks-btn', 'drop-btn']);
  assert.deepEqual(M.nudgeItem(l, 'profile-chip', -1), l, 'the first one cannot move up');
  const n = M.normalizeLayout({ rail: ['manage-bots', 'nope', 'manage-bots', 7], menu: ['lock-now'] });
  assert.deepEqual(n.rail, ['manage-bots', 'unlock-btn', 'drop-btn']);
  assert.deepEqual(n.menu, ['lock-now', 'profile-chip', 'tasks-btn', 'fs-chip', 'theme-toggle']);
  assert.deepEqual(M.normalizeLayout('garbage'), M.defaultLayout());
});

test('the layout persists per device, and Restricted gets the shipped bar whatever is stored', { skip: dom.skip }, async () => {
  const win = await boot();
  const M = await R();
  M.saveLayout(M.moveItem(M.defaultLayout(), 'manage-bots', 'menu', null));
  assert.ok(JSON.parse(win.localStorage.getItem('dispatch-rail-layout')).menu.includes('manage-bots'));
  assert.ok(M.loadLayout().menu.includes('manage-bots'));
  assert.deepEqual(M.activeLayout(true), { rail: M.RAIL_ITEMS, menu: [] });
  win.localStorage.setItem('dispatch-rail-layout', '{not json');
  assert.deepEqual(M.loadLayout(), M.defaultLayout());
});

test('applyLayout moves the real buttons; hidden ones stay hidden; pins stay before Settings', { skip: dom.skip }, async () => {
  const win = await boot();
  const M = await R();
  const doc = win.document;
  const rail = doc.getElementById('gear-row');
  const menu = doc.getElementById('tools-more');
  const pin = doc.createElement('button'); pin.className = 'gear-btn pin-btn'; pin.id = 'pin-x';
  rail.insertBefore(pin, doc.getElementById('manage-bots'));
  const tasks = doc.getElementById('tasks-btn');
  const shown = M.applyLayout(M.defaultLayout(), { rail, menu });
  assert.deepEqual(ids(menu), ['profile-chip', 'tasks-btn', 'fs-chip', 'theme-toggle']);
  assert.deepEqual(ids(rail), ['lock-now', 'unlock-btn', 'drop-btn', 'pin-x', 'manage-bots']);
  assert.equal(doc.getElementById('tasks-btn'), tasks, 'the same element, handlers and all');
  assert.equal(tasks.getAttribute('role'), 'menuitem');
  assert.ok(tasks.classList.contains('hidden'), 'its own visibility rule still applies');
  assert.equal(shown, [...menu.children].filter((b) => !b.classList.contains('hidden')).length);
  // Restricted: everything back on the bar, no menu roles, nothing draggable.
  M.applyLayout(M.activeLayout(true), { rail, menu, decoy: true });
  assert.deepEqual(ids(menu), []);
  assert.deepEqual(ids(rail), [...M.RAIL_ITEMS.slice(0, -1), 'pin-x', 'manage-bots']);
  assert.equal(tasks.getAttribute('role'), null);
  assert.equal(doc.getElementById('lock-now').getAttribute('draggable'), 'false');
});

function drag(win, target, type, { x = 0, y = 0 } = {}) {
  const ev = new win.Event(type, { bubbles: true, cancelable: true });
  Object.assign(ev, { clientX: x, clientY: y, dataTransfer: { setData() {}, effectAllowed: '' } });
  target.dispatchEvent(ev);
  return ev;
}

test('drag: rail -> popup button -> into the popup; popup row -> bar; reorder inside the popup', { skip: dom.skip }, async () => {
  const win = await boot();
  const M = await R();
  const doc = win.document;
  const rail = doc.getElementById('gear-row');
  const menu = doc.getElementById('tools-more');
  const toolList = doc.getElementById('tool-list');
  const btn = doc.createElement('button'); btn.id = 'tools-menu-btn'; toolList.append(btn);
  let opened = 0;
  const apply = () => M.applyLayout(M.loadLayout(), { rail, menu });
  M.wireRailDrag({
    rail, menu, toolList, isDecoy: () => false, openMenu: () => { opened++; },
    onMove: (id, zone, before) => { M.saveLayout(M.moveItem(M.loadLayout(), id, zone, before)); apply(); },
  });
  apply();
  const lock = doc.getElementById('lock-now');
  drag(win, lock, 'dragstart');
  assert.equal(drag(win, btn, 'dragenter').defaultPrevented, true);
  assert.equal(opened, 1, 'hovering the Tools button opens the popup');
  drag(win, btn, 'drop');
  assert.deepEqual(ids(menu).at(-1), 'lock-now', 'dropped on the button: the end of the popup');
  assert.equal(M.loadLayout().menu.at(-1), 'lock-now', 'and it is saved');

  const fs = doc.getElementById('fs-chip');
  drag(win, fs, 'dragstart');
  const gear = doc.getElementById('manage-bots');
  assert.equal(drag(win, gear, 'dragover').defaultPrevented, true);
  drag(win, gear, 'drop', { y: -100 });   // the top half of Settings: before it
  assert.deepEqual(ids(rail).slice(-2), ['fs-chip', 'manage-bots']);
  assert.ok(!ids(menu).includes('fs-chip'));

  const chip = doc.getElementById('profile-chip');
  drag(win, doc.getElementById('lock-now'), 'dragstart');
  drag(win, chip, 'drop', { y: -100 });
  assert.equal(ids(menu)[0], 'lock-now', 'reordered inside the popup');
  // A text/bot drag that did not start on a rail button does nothing.
  assert.equal(drag(win, gear, 'dragover').defaultPrevented, false);
});

test('a Restricted device cannot start a rail drag', { skip: dom.skip }, async () => {
  const win = await boot();
  const M = await R();
  const doc = win.document;
  const rail = doc.getElementById('gear-row');
  let moved = 0;
  M.wireRailDrag({ rail, menu: doc.getElementById('tools-more'), toolList: null, isDecoy: () => true,
    openMenu() {}, onMove: () => { moved++; } });
  drag(win, doc.getElementById('lock-now'), 'dragstart');
  drag(win, doc.getElementById('manage-bots'), 'drop');
  assert.equal(moved, 0);
});

test('"Arrange the bar" moves without dragging, and Reset restores the default', { skip: dom.skip }, async () => {
  const win = await boot();
  const M = await R();
  const { el } = await import('../static/js/util.js?v=20');
  const { t } = await import('../static/js/i18n.js?v=4');
  let changes = 0;
  const { dialog } = M.openArrangeDialog({ t, el, labelOf: (id) => id, visible: () => true, onChange: () => { changes++; } });
  const rowOf = (id) => dialog.querySelector(`.rail-arrange-row[data-id="${id}"]`);
  assert.match(dialog.textContent, /On the bar/);
  assert.match(dialog.textContent, /In Tools & more/);
  const move = [...rowOf('manage-bots').querySelectorAll('button')].find((b) => b.textContent === 'Move to Tools');
  move.click();
  assert.ok(M.loadLayout().menu.includes('manage-bots'));
  assert.equal(changes, 1);
  [...rowOf('tasks-btn').querySelectorAll('button')].find((b) => b.textContent === 'Move to the bar').click();
  assert.equal(M.loadLayout().rail.at(-1), 'tasks-btn');
  [...dialog.querySelectorAll('button')].find((b) => b.textContent === 'Reset').click();
  assert.deepEqual(M.loadLayout(), M.defaultLayout());
  assert.equal(win.document.activeElement.textContent, 'Reset');
});

test('wiring: main.js applies the layout with the pins, the popup has the section, the key is wiped by privacy mode', () => {
  const main = read('js/main.js');
  assert.match(main, /from '\.\/railmenu\.js\?v=1'/);
  assert.match(main, /renderLinkRail\(document\.getElementById\('gear-row'\), \{ decoy: state\.decoy, openViewer \}\);\n  applyRailLayout\(\);/);
  assert.match(main, /applyLayout\(activeLayout\(state\.decoy\)/);
  assert.match(HTML, /<div class="tools-more" id="tools-more" role="none"><\/div>/);
  assert.match(read('js/privacy.js'), /'dispatch-rail-layout'/);
  assert.match(read('sw.js'), /'\/static\/js\/railmenu\.js'/);
  assert.equal(EN.nav.tools_more, 'Tools & more');
});
