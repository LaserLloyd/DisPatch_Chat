// Quick actions in the browser (js/quick-actions.js): the list, the pill, the
// menu, the empty-thread chips, the bubble tag, plus the wiring in main.js /
// index.html / the locales that a refactor would silently break.
//
// Run: node --test frontend/tests/

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { domSkip } from './_require-dom.js';

const require = createRequire(import.meta.url);
let jsdom = null;
try { jsdom = require('jsdom'); } catch { /* not installed — DOM tests skip */ }
const dom = { skip: domSkip(jsdom ? false : 'jsdom is not installed') };

const HERE = dirname(fileURLToPath(import.meta.url));
const STATIC = join(HERE, '..', 'static');
const MAIN = readFileSync(join(STATIC, 'js', 'main.js'), 'utf8');
const HTML = readFileSync(join(STATIC, 'index.html'), 'utf8');
const CSS = readFileSync(join(STATIC, 'app.css'), 'utf8');
const MODULE = readFileSync(join(STATIC, 'js', 'quick-actions.js'), 'utf8');

const ACTIONS = [
  { id: 'proof', label: 'Proof this', hint: 'Paste the text to proofread…', tools: false },
  { id: 'insight', label: 'News insight', hint: 'Paste an article…', tools: true },
  { id: 'todo', label: 'Action items', hint: '', tools: false },
];

const PAGE = `<!doctype html><body>
  <div id="qa-bar" hidden></div>
  <span class="quick-wrap">
    <button id="quick-btn" hidden aria-expanded="false"></button>
    <div id="quick-menu" class="menu" hidden></div>
  </span>
  <textarea id="input"></textarea></body>`;

function setup() {
  const win = new jsdom.JSDOM(PAGE, { url: 'http://localhost/' }).window;
  const put = (k, v) => Object.defineProperty(globalThis, k, { value: v, configurable: true, writable: true });
  put('window', win);
  put('document', win.document);
  put('localStorage', win.localStorage);
  put('navigator', win.navigator);
  return win;
}

const load = async () => {
  const m = await import(`../static/js/quick-actions.js?t=${Math.random()}`);
  return m;
};
const $ = (id) => document.getElementById(id);
const key = (win, node, k, extra = {}) => {
  const ev = new win.KeyboardEvent('keydown', { key: k, bubbles: true, cancelable: true, ...extra });
  node.dispatchEvent(ev);
  return ev;
};

test('normalizeActions drops malformed rows and duplicates', dom, async () => {
  setup();
  const { normalizeActions } = await load();
  const out = normalizeActions([
    ACTIONS[0], ACTIONS[0], { id: 'x' }, { label: 'no id' }, null, 'str',
    { id: 'y', label: '  Spaced  ', hint: 5, tools: 1 },
  ]);
  assert.deepEqual(out.map((a) => a.id), ['proof', 'y']);
  assert.deepEqual(out[1], { id: 'y', label: 'Spaced', hint: '', tools: true });
  assert.deepEqual(normalizeActions(null), []);
});

test('loadActions fills the list, shows the bolt button; a failing fetch leaves it empty', dom, async () => {
  setup();
  const m = await load();
  await m.loadActions(async () => { throw new Error('offline'); });
  assert.equal(m.getActions().length, 0);
  await m.loadActions(async () => ({ actions: ACTIONS }));
  assert.equal(m.getActions().length, 3);
  assert.equal($('quick-btn').hidden, false);
  m.setActions([]);
  assert.equal($('quick-btn').hidden, true);
});

test('picking an action sets the pill, the placeholder and currentAction', dom, async () => {
  setup();
  const m = await load();
  m.setActions(ACTIONS);
  m.setComposerAction(ACTIONS[0]);
  assert.equal(m.currentAction().id, 'proof');
  assert.equal($('qa-bar').hidden, false);
  assert.equal($('qa-bar').querySelector('.qa-pill-label').textContent, 'Proof this');
  assert.equal($('input').placeholder, 'Paste the text to proofread…');
  assert.equal(m.placeholderOverride(), 'Paste the text to proofread…');
  // Without a hint the label stands in for it.
  m.setComposerAction(ACTIONS[2]);
  assert.equal(m.placeholderOverride(), 'Action items…');
  m.clearComposerAction();
  assert.equal(m.currentAction(), null);
  assert.equal($('qa-bar').hidden, true);
  assert.equal(m.placeholderOverride(), null);
});

test('the x button removes the pill and keeps the typed text', dom, async () => {
  const win = setup();
  const m = await load();
  m.setActions(ACTIONS);
  $('input').value = 'draft';
  m.setComposerAction(ACTIONS[0]);
  const x = $('qa-bar').querySelector('button.qa-pill-remove');
  assert.ok(x.getAttribute('aria-label'));
  x.dispatchEvent(new win.MouseEvent('click', { bubbles: true }));
  assert.equal(m.currentAction(), null);
  assert.equal($('input').value, 'draft');
});

test('Backspace in an empty box clears the pill; with text it does not', dom, async () => {
  const win = setup();
  const m = await load();
  m.setActions(ACTIONS);
  m.wireQuickActions({ announce: () => {} });
  m.setComposerAction(ACTIONS[0]);
  $('input').value = 'x';
  assert.equal(key(win, $('input'), 'Backspace').defaultPrevented, false);
  assert.ok(m.currentAction());
  $('input').value = '';
  assert.equal(key(win, $('input'), 'Backspace').defaultPrevented, true);
  assert.equal(m.currentAction(), null);
});

test('the list changing under a picked action follows it or drops it', dom, async () => {
  setup();
  const m = await load();
  m.setActions(ACTIONS);
  m.setComposerAction(ACTIONS[0]);
  m.setActions([{ ...ACTIONS[0], label: 'Proofread' }, ACTIONS[1]]);
  assert.equal(m.currentAction().label, 'Proofread');
  m.setActions([ACTIONS[1]]);
  assert.equal(m.currentAction(), null);
});

test('the menu lists the actions, picks one, closes on Escape, marks the current one', dom, async () => {
  const win = setup();
  const m = await load();
  m.setActions(ACTIONS);
  m.wireQuickActions({ announce: () => {} });
  $('quick-btn').dispatchEvent(new win.MouseEvent('click', { bubbles: true }));
  assert.equal(m.quickMenuOpen(), true);
  assert.equal($('quick-btn').getAttribute('aria-expanded'), 'true');
  const items = [...$('quick-menu').querySelectorAll('[role="menuitem"]')];
  assert.deepEqual(items.map((b) => b.querySelector('.qm-label').textContent),
    ['Proof this', 'News insight', 'Action items']);
  assert.ok(items[1].querySelector('.qm-tag'), 'a tools action carries the web tag');
  assert.equal(items[0].querySelector('.qm-tag'), null);
  key(win, items[0], 'ArrowDown');
  assert.equal(document.activeElement, items[1]);
  key(win, items[1], 'Escape');
  assert.equal(m.quickMenuOpen(), false);
  $('quick-btn').dispatchEvent(new win.MouseEvent('click', { bubbles: true }));
  [...$('quick-menu').querySelectorAll('[role="menuitem"]')][2].dispatchEvent(new win.MouseEvent('click', { bubbles: true }));
  assert.equal(m.currentAction().id, 'todo');
  assert.equal(m.quickMenuOpen(), false);
  $('quick-btn').dispatchEvent(new win.MouseEvent('click', { bubbles: true }));
  assert.ok($('quick-menu').querySelector('.is-current'));
  // A click elsewhere closes it.
  document.body.dispatchEvent(new win.MouseEvent('click', { bubbles: true }));
  assert.equal(m.quickMenuOpen(), false);
});

test('welcome chips: one per action, title is the hint, a click picks it', dom, async () => {
  const win = setup();
  const m = await load();
  assert.deepEqual(m.welcomeChips(), []);
  m.setActions(ACTIONS);
  const [intro, grid] = m.welcomeChips();
  assert.equal(intro.className, 'qa-intro');
  const chips = [...grid.querySelectorAll('.qa-chip')];
  assert.equal(chips.length, 3);
  assert.equal(chips[0].title, 'Paste the text to proofread…');
  assert.equal(chips[2].title, 'Action items');
  chips[1].dispatchEvent(new win.MouseEvent('click', { bubbles: true }));
  assert.equal(m.currentAction().id, 'insight');
});

test('labels from the server are text, never markup', dom, async () => {
  setup();
  const m = await load();
  const evil = { id: 'evil', label: '<img src=x onerror=alert(1)>', hint: '<b>h</b>', tools: false };
  m.setActions([evil]);
  const [, grid] = m.welcomeChips();
  assert.equal(grid.querySelector('img'), null);
  m.setComposerAction(evil);
  assert.equal($('qa-bar').querySelector('img'), null);
  const tag = m.actionTagEl({ action: { id: 'evil', label: evil.label } });
  assert.equal(tag.querySelector('img'), null);
  assert.equal(tag.querySelector('.msg-action-label').textContent, evil.label);
});

test('actionTagEl: label, id fallback, truncation, and null without an action', dom, async () => {
  setup();
  const { actionTagEl } = await load();
  assert.equal(actionTagEl(null), null);
  assert.equal(actionTagEl({}), null);
  assert.equal(actionTagEl({ action: 'proof' }), null);
  assert.equal(actionTagEl({ action: { id: 'proof', label: 'Proof this' } })
    .querySelector('.msg-action-label').textContent, 'Proof this');
  assert.equal(actionTagEl({ action: { id: 'proof' } })
    .querySelector('.msg-action-label').textContent, 'proof');
  const long = actionTagEl({ action: { id: 'a', label: 'x'.repeat(200) } })
    .querySelector('.msg-action-label').textContent;
  assert.ok(long.length <= 60 && long.endsWith('…'));
});

// ---------------------------------------------------------------------------
// Wiring (textual, like the other *-wiring tests: main.js is not importable)
// ---------------------------------------------------------------------------

test('sendMessage puts the action id in the WS frame, and clears the pill after the frame is out', () => {
  const body = MAIN.slice(MAIN.indexOf('function sendMessage()'));
  const send = body.slice(0, body.indexOf('\n}\n'));
  assert.match(send, /const quickAction = currentQuickAction\(\)/);
  assert.match(send, /if \(quickAction\) frame\.action = quickAction\.id/);
  assert.ok(send.indexOf('frame.action') < send.indexOf('socket.send(frame)'),
    'the id must be in the frame before it is sent (a replay resends the same object)');
  assert.ok(send.indexOf('socket.send(frame)') < send.indexOf('clearQuickAction()'),
    'the pill stays until the frame is out');
  assert.match(send, /showOptimisticSend\([^)]*quickAction/s, 'the optimistic bubble carries the tag too');
});

test('main.js renders the tag, switches chips by viewport, and handles bad_action', () => {
  assert.match(MAIN, /actionTagEl\(msg\.metadata\)/);
  assert.match(MAIN, /!state\.decoy && !isMobile\(\)\) \? quickWelcomeChips\(\)/);
  assert.match(MAIN, /data\.code === 'bad_action'/);
  assert.match(MAIN, /wireQuickActions\(\{ announce \}\)/);
  assert.match(MAIN, /api\.quickActions/);
  // The pill belongs to the thread it was picked in.
  assert.match(MAIN, /clearReplyTarget\(\);\n\s+clearQuickAction\(\);/);
});

test('index.html has the pill bar, the bolt button and the menu', () => {
  for (const id of ['qa-bar', 'quick-btn', 'quick-menu']) {
    assert.match(HTML, new RegExp(`id="${id}"`), id);
  }
  assert.match(HTML, /id="quick-btn"[^>]*hidden/);
  assert.match(HTML, /aria-haspopup="menu"/);
});

test('the CSS uses theme tokens only for the quick-action rules', () => {
  const start = CSS.indexOf('/* ---- Quick actions');
  const block = CSS.slice(start, CSS.indexOf('/* ---- Message actions', start));
  assert.ok(block.length > 500);
  assert.doesNotMatch(block, /#[0-9a-fA-F]{3,8}\b/, 'no literal hex colours');
  assert.doesNotMatch(block, /\brgba?\(/, 'no literal rgb colours');
});

test('the quick locale namespace sits right after welcome and exists in all eight locales', () => {
  const dir = join(STATIC, 'locales');
  const used = [...new Set([...MODULE.matchAll(/t\('(quick\.[a-z_]+)'/g)].map((m) => m[1]))];
  const mainUsed = [...MAIN.matchAll(/t\('(quick\.[a-z_]+)'/g)].map((m) => m[1]);
  const htmlUsed = [...HTML.matchAll(/=(quick\.[a-z_]+)/g)].map((m) => m[1]);
  const all = [...new Set([...used, ...mainUsed, ...htmlUsed])];
  assert.ok(all.length >= 10, `found ${all.length} keys`);
  for (const f of readdirSync(dir).filter((x) => x.endsWith('.json'))) {
    const j = JSON.parse(readFileSync(join(dir, f), 'utf8'));
    const names = Object.keys(j);
    assert.equal(names[names.indexOf('welcome') + 1], 'quick', `${f}: quick follows welcome`);
    for (const k of all) {
      const v = k.split('.').slice(1).reduce((o, p) => o && o[p], j.quick);
      assert.equal(typeof v, 'string', `${f}: ${k}`);
      assert.ok(v.trim(), `${f}: ${k} is empty`);
    }
    // Placeholders survive translation.
    for (const k of ['pill', 'remove', 'picked', 'sent_with']) {
      assert.match(j.quick[k], /\{name\}/, `${f}: quick.${k} keeps {name}`);
    }
  }
});
