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
import { newChatAllowed, pickNewChatTarget, latestByBot } from '../static/js/newchat.js';

const STATIC = join(dirname(fileURLToPath(import.meta.url)), '..', 'static');
const read = (f) => readFileSync(join(STATIC, f), 'utf8');

test('the selected character wins, even when another was used more recently', () => {
  const r = pickNewChatTarget({ selectedId: 'a', usableIds: ['a', 'b'], latest: { a: '2026-01-01', b: '2026-09-01' } });
  assert.deepEqual(r, { botId: 'a', via: 'current' });
});

test('nothing selected: the usable character with the newest thread', () => {
  const r = pickNewChatTarget({ selectedId: null, usableIds: ['a', 'b', 'c'], latest: { a: '2026-01-01T00:00:00Z', b: '2026-09-01T00:00:00Z', c: '' } });
  assert.deepEqual(r, { botId: 'b', via: 'recent' });
});

test('a selection this profile cannot use is ignored; unusable characters are never picked', () => {
  const r = pickNewChatTarget({ selectedId: 'x', usableIds: ['a', 'b'], latest: { a: '2026-01-01', b: '2026-02-01', x: '2026-12-31' } });
  assert.deepEqual(r, { botId: 'b', via: 'recent' });
});

test('no selection and no history: open the picker', () => {
  assert.deepEqual(pickNewChatTarget({ selectedId: null, usableIds: ['a', 'b'], latest: {} }), { botId: null, via: 'picker' });
  assert.deepEqual(pickNewChatTarget({ selectedId: null, usableIds: [], latest: {} }), { botId: null, via: 'picker' });
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
  assert.match(main, /from '\.\/newchat\.js\?v=1'/);
  assert.ok(read('sw.js').includes("'/static/js/newchat.js'"));
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
