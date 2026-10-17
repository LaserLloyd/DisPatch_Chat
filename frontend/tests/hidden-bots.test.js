// 2.4.0: bots hidden on unlocked devices (`bot.hide_unlocked`).
//
// Run: node --test frontend/tests/
//
// What must hold:
//   * an unlocked device leaves a hidden bot off its rail, unless THIS device
//     opted back in ("Show hidden bots on this device");
//   * Safe Mode ignores the flag entirely — a locked tablet lists every safe
//     bot the server sent, hidden or not;
//   * storage being unavailable means "hide" (the flag does what it says);
//   * main.js draws the rail / Ctrl-K / companions from visibleBots(), while
//     state.bots stays the whole reachable roster (a hidden bot's thread opened
//     by URL still finds its bot), and the Bot Manager saves the flag.

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const store = new Map();
let broken = false;
globalThis.localStorage = {
  getItem: (k) => { if (broken) throw new Error('denied'); return store.has(k) ? store.get(k) : null; },
  setItem: (k, v) => { if (broken) throw new Error('denied'); store.set(k, String(v)); },
  removeItem: (k) => { if (broken) throw new Error('denied'); store.delete(k); },
};

const { hiddenHere, showHiddenBots, setShowHiddenBots } = await import('../static/js/menubots.js');

const KID = { id: 'kids_lobster', name: 'Lulu', safe: true, hide_unlocked: true };
const ATLAS = { id: 'atlas', name: 'Atlas', safe: true, hide_unlocked: false };
const reset = () => { store.clear(); broken = false; };

test('an unlocked device hides a flagged bot and keeps the rest', () => {
  reset();
  assert.equal(hiddenHere(KID, false), true);
  assert.equal(hiddenHere(ATLAS, false), false);
  assert.equal(hiddenHere({ id: 'old' }, false), false, 'a bot without the field is shown');
});

test('Safe Mode ignores the flag: a locked device shows every safe bot it was sent', () => {
  reset();
  assert.equal(hiddenHere(KID, true), false);
  setShowHiddenBots(false);
  assert.equal(hiddenHere(KID, true), false);
});

test('the per-device opt-in brings hidden bots back, and is remembered', () => {
  reset();
  assert.equal(showHiddenBots(), false, 'off by default');
  assert.equal(setShowHiddenBots(true), true);
  assert.equal(store.get('dispatch-show-hidden-bots'), '1');
  assert.equal(hiddenHere(KID, false), false);
  assert.equal(setShowHiddenBots(false), false);
  assert.equal(store.has('dispatch-show-hidden-bots'), false, 'off removes the key');
  assert.equal(hiddenHere(KID, false), true);
});

test('storage that throws means hidden, never a crash', () => {
  reset();
  store.set('dispatch-show-hidden-bots', '1');
  broken = true;
  assert.equal(showHiddenBots(), false);
  assert.equal(setShowHiddenBots(true), false);
  assert.equal(hiddenHere(KID, false), true);
  broken = false;
});

// ---- main.js wiring -------------------------------------------------------

const HERE = dirname(fileURLToPath(import.meta.url));
const read = (p) => readFileSync(join(HERE, '..', 'static', p), 'utf8');
const MAIN = read('js/main.js');

function fnBody(src, name) {
  const start = src.search(new RegExp(`(?:async\\s+)?function\\s+${name}\\s*\\(`));
  assert.ok(start >= 0, `${name} must be defined`);
  const end = src.indexOf('\n}\n', start);
  return src.slice(start, end);
}

test('visibleBots() filters with hiddenHere and the current tier', () => {
  assert.match(fnBody(MAIN, 'visibleBots'), /state\.bots\.filter\(\(b\) => !hiddenHere\(b, state\.decoy\)\)/);
});

test('the rail, the companions list and Ctrl-K draw from visibleBots()', () => {
  assert.match(fnBody(MAIN, 'renderSidebarInner'), /const allVisible = visibleBots\(\);/);
  assert.match(fnBody(MAIN, 'renderCompanions'), /const bots = visibleBots\(\);/);
  assert.match(fnBody(MAIN, 'cmdkBuild'), /for \(const b of visibleBots\(\)\)/);
  assert.doesNotMatch(fnBody(MAIN, 'cmdkBuild'), /for \(const b of state\.bots\)/);
});

test('a rail drag moves by id, so hidden and parked bots cannot skew the drop', () => {
  const body = fnBody(MAIN, 'renderSidebarInner');
  assert.match(body, /state\.bots\.findIndex\(\(b\) => b\.id === bots\[sidebarDragIdx\]\?\.id\)/);
  assert.doesNotMatch(body, /state\.bots\.splice\(sidebarDragIdx, 1\)/);
});

test('a roster frame keeps a hidden bot\'s open chat (selection checked against state.bots)', () => {
  assert.match(MAIN, /&& !state\.bots\.find\(\(b\) => b\.id === state\.selectedBotId\)\) \{/);
});

// 2.4.2: "Show locked" (`safe`) + "Show unlocked" (`!hide_unlocked`); neither
// = shown nowhere, the row dimmed and tagged Hidden. The old `visible` switch
// is gone (the server folds a stray `visible: false` into "neither").
test('the Bot Manager has Show locked + Show unlocked and saves both with the roster', () => {
  const rows = fnBody(MAIN, 'renderBotManager');
  assert.match(rows, /bmBots\[idx\]\.safe = !bmBots\[idx\]\.safe/);
  assert.match(rows, /bmBots\[idx\]\.hide_unlocked = !bmBots\[idx\]\.hide_unlocked/);
  assert.match(rows, /t\('settings\.show_locked_badge'\)/);
  assert.match(rows, /t\('settings\.show_unlocked_badge'\)/);
  // "Show unlocked" is ON when the bot is NOT hidden on unlocked devices.
  assert.match(rows, /\(!bot\.hide_unlocked \? ' on' : ''\)/);
  assert.match(rows, /t\('settings\.hidden_badge'\)/);
  assert.match(rows, /row\.classList\.toggle\('bm-row--hidden', none\)/);
  assert.doesNotMatch(rows, /bmBots\[idx\]\.visible/);
  assert.doesNotMatch(rows, /settings\.show_in_sidebar/);
  const save = fnBody(MAIN, 'saveBotManager');
  assert.match(save, /safe: !!b\.safe/);
  assert.match(save, /hide_unlocked: !!b\.hide_unlocked/);
  assert.doesNotMatch(save, /visible:/);
});

test('shownNowhere is exactly "neither switch on"', () => {
  const body = fnBody(MAIN, 'shownNowhere');
  assert.match(body, /return !!bot && !bot\.safe && !!bot\.hide_unlocked;/);
});

test('no roster path drops a bot client-side for `visible` (Hidden bots stay reachable by URL)', () => {
  assert.doesNotMatch(MAIN, /bots\.filter\(\(b\) => b\.visible/);
  assert.match(MAIN, /state\.bots = data\.bots\.filter\(\(b\) => !state\.decoy \|\| b\.safe\);/);
  assert.doesNotMatch(MAIN, /visible: b\.visible/);
});

test('every locale has the new labels and none of the retired ones', () => {
  for (const lang of ['en', 'de', 'es', 'fr', 'pt', 'ja', 'zh', 'ar']) {
    const s = JSON.parse(read(`locales/${lang}.json`)).settings;
    for (const k of ['show_locked_badge', 'show_locked_title', 'show_unlocked_badge',
      'show_unlocked_title', 'hidden_badge', 'hidden_title']) assert.ok(s[k], `${lang} ${k}`);
    for (const k of ['safe_badge', 'hide_unlocked_badge', 'hide_unlocked_title', 'show_in_sidebar']) {
      assert.equal(s[k], undefined, `${lang} still has ${k}`);
    }
  }
});

test('the device switch is unlocked-only and only offered when a bot is hidden', () => {
  const body = fnBody(MAIN, 'mountLanguagePicker');
  assert.match(body, /!state\.decoy && displayBody && state\.bots\.some\(\(b\) => b\.hide_unlocked\)/);
  assert.match(body, /setShowHiddenBots\(cb\.checked\)/);
});

test('"clear this device" wipes the switch', () => {
  assert.match(read('js/privacy.js'), /'dispatch-show-hidden-bots'/);
});
