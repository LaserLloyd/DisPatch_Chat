// The phone bottom tab "Messages" became "New chat" (ownership plan R0).
//
// Run: node --test frontend/tests/
//
//   * target: the selected character, else the most recently used one this
//     profile can reach, else the character picker;
//   * the tab is hidden when the profile may not start chats;
//   * wiring: the tab is an action (no data-view), main.js routes its click,
//     every locale has the label, the old Messages label is gone.

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { newChatAllowed, pickNewChatTarget, latestByBot, currentCharacter, reusableEmptyThread, readLastCharacter, rememberCharacter } from '../static/js/newchat.js';

const STATIC = join(dirname(fileURLToPath(import.meta.url)), '..', 'static');
const read = (f) => readFileSync(join(STATIC, f), 'utf8');

const base = { usableIds: ['a', 'b', 'c'], recentIds: ['a', 'b', 'c'] };

test('the character on screen wins, even when another was used more recently', () => {
  const r = pickNewChatTarget({ ...base, currentId: 'a', lastId: 'b', latest: { a: '2026-01-01', b: '2026-09-01' } });
  assert.deepEqual(r, { botId: 'a', via: 'current' });
});

test('a character this profile may use but the rail hides is still "current" (roster, not rail)', () => {
  // recentIds (the rail) lacks 'h'; usableIds (the server roster) has it.
  const r = pickNewChatTarget({ currentId: 'h', usableIds: ['a', 'h'], recentIds: ['a'], latest: { a: '2026-09-01' } });
  assert.deepEqual(r, { botId: 'h', via: 'current' });
});

test('an app pane / deep link thread: the open thread beats the stale selection', () => {
  assert.equal(currentCharacter({ view: 'chat', activeBotId: 'charlie', selectedId: 'bravo' }), 'charlie');
  assert.equal(currentCharacter({ view: 'threads', activeBotId: 'charlie', selectedId: 'bravo' }), 'bravo');
  assert.equal(currentCharacter({ view: 'chat', activeBotId: null, selectedId: 'bravo' }), 'bravo');
  assert.equal(currentCharacter({ view: 'bots', activeBotId: null, selectedId: null }), null);
});

test('nothing on screen: the device\'s last-used character, if still usable', () => {
  assert.deepEqual(pickNewChatTarget({ ...base, currentId: null, lastId: 'c', latest: {} }), { botId: 'c', via: 'last' });
  const gone = pickNewChatTarget({ ...base, currentId: null, lastId: 'zzz', latest: { a: '2026-02-01' } });
  assert.deepEqual(gone, { botId: 'a', via: 'recent' });
});

test('no last-used: the usable character with the newest thread', () => {
  const r = pickNewChatTarget({ ...base, currentId: null, lastId: '', latest: { a: '2026-01-01T00:00:00Z', b: '2026-09-01T00:00:00Z', c: '' } });
  assert.deepEqual(r, { botId: 'b', via: 'recent' });
});

test('unusable characters are never picked', () => {
  const r = pickNewChatTarget({ currentId: 'x', usableIds: ['a', 'b'], recentIds: ['a', 'b', 'x'], latest: { a: '2026-01-01', b: '2026-02-01', x: '2026-12-31' } });
  assert.deepEqual(r, { botId: 'b', via: 'recent' });
});

test('no history: open the picker', () => {
  assert.deepEqual(pickNewChatTarget({ ...base, currentId: null, latest: {} }), { botId: null, via: 'picker' });
  assert.deepEqual(pickNewChatTarget({ usableIds: [], recentIds: [], latest: {} }), { botId: null, via: 'picker' });
});

test('lookups: all failed = offline; some failed = picker, never a guess', () => {
  const latest = { a: '2026-09-01' };
  assert.equal(pickNewChatTarget({ ...base, currentId: null, latest: {}, attempted: 3, failed: 3 }).via, 'offline');
  assert.equal(pickNewChatTarget({ ...base, currentId: null, latest, attempted: 3, failed: 1 }).via, 'picker');
  assert.equal(pickNewChatTarget({ ...base, currentId: 'b', latest, attempted: 3, failed: 3 }).via, 'current');
});

test('an empty newest thread is reused; a used or archived one is not', () => {
  const T = (id, at, extra = {}) => ({ id, updated_at: at, message_count: 0, ...extra });
  assert.equal(reusableEmptyThread([T('old', '2026-01-01', { message_count: 4 }), T('new', '2026-02-01')]).id, 'new');
  assert.equal(reusableEmptyThread([T('old', '2026-01-01'), T('new', '2026-02-01', { message_count: 2 })]), null);
  assert.equal(reusableEmptyThread([T('n', '2026-02-01', { last_message: 'hi' })]), null);
  assert.equal(reusableEmptyThread([T('old', '2026-01-01', { message_count: 1 }), T('arch', '2026-03-01', { is_archived: true })]), null);
  assert.equal(reusableEmptyThread([]), null);
});

test('the last-used character is remembered per device, and storage failing is harmless', () => {
  const mem = new Map();
  globalThis.localStorage = { getItem: (k) => (mem.has(k) ? mem.get(k) : null), setItem: (k, v) => mem.set(k, v) };
  assert.equal(readLastCharacter(), '');
  rememberCharacter('bee');
  assert.equal(readLastCharacter(), 'bee');
  rememberCharacter('');
  assert.equal(readLastCharacter(), 'bee');
  globalThis.localStorage = { getItem() { throw new Error('denied'); }, setItem() { throw new Error('denied'); } };
  assert.equal(readLastCharacter(), '');
  assert.doesNotThrow(() => rememberCharacter('x'));
});

test('latestByBot takes the newest updated_at per character', () => {
  assert.deepEqual(latestByBot({ a: [{ updated_at: '2026-01-01' }, { updated_at: '2026-03-01' }], b: [] }), { a: '2026-03-01', b: '' });
});

test('the tab is offered unless the profile lacks use_chat', () => {
  assert.equal(newChatAllowed(['*']), true);
  assert.equal(newChatAllowed(['use_chat', 'use_voice']), true);
  assert.equal(newChatAllowed(['use_voice']), false);
  assert.equal(newChatAllowed([]), false);
  assert.equal(newChatAllowed(null), true);   // an older server: same as the + button
});

test('wiring: index.html tab is an action, main.js routes and hides it', () => {
  const html = read('index.html');
  const nav = /<nav class="mobile-tabs"[\s\S]*?<\/nav>/.exec(html)[0];
  assert.ok(!/data-view="chat"/.test(nav), 'no Messages tab');
  assert.ok(/id="tab-new-chat"[^>]*data-i18n="nav\.tab_new_chat"|data-action="new-chat"[\s\S]*?nav\.tab_new_chat/.test(nav));
  assert.ok(!nav.includes('nav.tab_messages'));
  const main = read('js/main.js');
  assert.match(main, /dataset\.action === 'new-chat'/);
  assert.match(main, /syncNewChatTab\(\);/);
  assert.match(main, /from '\.\/newchat\.js\?v=3'/);
  assert.ok(read('sw.js').includes("'/static/js/newchat.js'"));
  // review fixes: usability from the roster, the rail only for the fallback; the
  // active tab in the chat view; the last-used key is wiped by privacy mode.
  assert.match(main, /usableIds = state\.bots\.filter/);
  assert.match(main, /new-chat' && view === 'chat'/);
  assert.match(main, /rememberCharacter\(/);
  assert.match(main, /reusableEmptyThread\(/);
  assert.ok(read('js/privacy.js').includes("'dispatch-last-character'"));
  assert.match(read('index.html'), /main\.js\?v=156/);
  assert.match(readFileSync(join(STATIC, '..', '..', 'README.md'), 'utf8'), /Bots, Chats and New chat/);
});

test('every locale labels the tab and has dropped Messages', () => {
  const names = readdirSync(join(STATIC, 'locales')).filter((f) => f.endsWith('.json'));
  const en = JSON.parse(read('locales/en.json')).nav.tab_new_chat;
  assert.equal(en, 'New chat');
  for (const f of names) {
    const nav = JSON.parse(read(`locales/${f}`)).nav;
    assert.ok(nav.tab_new_chat, f);
    assert.equal(nav.tab_messages, undefined, f);
    if (f !== 'en.json') assert.notEqual(nav.tab_new_chat, en, `${f} is translated`);
  }
});
