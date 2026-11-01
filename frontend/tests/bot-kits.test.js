// 4.1 managed bots: Settings -> Characters -> Details -> Copies & chats (js/bot-kits.js).
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
import { domSkip } from './_require-dom.js';

const require = createRequire(import.meta.url);
let jsdom = null;
try { jsdom = require('jsdom'); } catch { /* DOM tests skip */ }
const dom = { skip: domSkip(jsdom ? false : 'jsdom is not installed') };

const EN = JSON.parse(readFileSync(new URL('../static/locales/en.json', import.meta.url), 'utf8'));

async function useDom() {
  const win = new jsdom.JSDOM('<!doctype html><body></body>', { url: 'http://127.0.0.1:8802/' }).window;
  const put = (k, v) => Object.defineProperty(globalThis, k, { value: v, configurable: true, writable: true });
  put('window', win); put('document', win.document); put('location', win.location);
  put('navigator', win.navigator); put('localStorage', win.localStorage);
  put('Node', win.Node); put('HTMLElement', win.HTMLElement);
  globalThis.fetch = async () => ({ ok: true, status: 200, headers: new win.Headers(), json: async () => EN });
  const i18n = await import('../static/js/i18n.js?v=5');
  await i18n.init('en');
  return win;
}

test('kitBody sends only the household chats, with the resend choice', async () => {
  const { kitBody } = await import('../static/js/bot-kits.js?v=3');
  const kit = { auto_clone: true, chats: [
    { id: 'meet-atlas', title: 'Meet Atlas', audience: 'everyone', builtin: true, body: '' },
    { id: 'c0000abcd', title: 'Rules', body: 'Be kind', audience: 'admins' }] };
  assert.deepEqual(kitBody(kit), { chats: [{ id: 'c0000abcd', title: 'Rules', body: 'Be kind', audience: 'admins' }] });
  assert.ok(!('auto_clone' in kitBody(kit, { chats: [] })), 'a chat save never touches give_to');
  assert.deepEqual(kitBody(kit, { autoClone: false, chats: [{ title: 'New', body: 'x', audience: 'weird', resend: false }] }),
    { auto_clone: false, chats: [{ title: 'New', body: 'x', audience: 'everyone', resend: false }] });
});

test('the panel: start managing, toggle auto-clone, add a chat, push; built-in chats are read-only', { skip: dom.skip }, async () => {
  await useDom();
  const { kitSection } = await import('../static/js/bot-kits.js?v=3');
  const sent = [];
  let kit = { managed: false, copyable: true, shipped: false, auto_clone: false, chats: [], copies: 0 };
  const calls = {
    get: async () => kit,
    save: async (id, body) => { sent.push(['save', id, body]); kit = { ...kit, managed: true, auto_clone: body.auto_clone,
      chats: body.chats.map((c, i) => ({ id: c.id || `c0000000${i}`, rev: 1, ...c })), copies: 2 }; return kit; },
    push: async (id) => { sent.push(['push', id]); return { ...kit, posted: 3 }; },
    remove: async () => kit,
  };
  const toasts = [];
  const box = await kitSection({ id: 'otter', name: 'Otter' }, { toast: (m) => toasts.push(m), ask: async () => 'ok' }, calls);
  document.body.append(box);
  const btn = (text) => [...box.querySelectorAll('button')].find((b) => b.textContent === text);
  btn('Manage copies of this bot').click();
  await new Promise((r) => setTimeout(r, 0));
  assert.deepEqual(sent[0], ['save', 'otter', { auto_clone: false, chats: [] }]);
  const sw = box.querySelector('input[role="switch"]');
  sw.checked = true; sw.dispatchEvent(new window.Event('change'));
  await new Promise((r) => setTimeout(r, 0));
  assert.equal(sent[1][2].auto_clone, true);
  assert.match(box.textContent, /Copies: 2/);
  assert.match(box.textContent, /New profiles get a copy/);
  assert.match(box.textContent, /only stops copies for profiles created later/, 'the switch says it never removes a copy');
  btn('Add a chat').click();
  box.querySelector('input[type="text"]').value = 'Welcome';
  box.querySelector('textarea').value = 'Hello **all**';
  box.querySelector('select').value = 'admins';
  btn('Save chat').click();
  await new Promise((r) => setTimeout(r, 0));
  assert.deepEqual(sent[2][2].chats, [{ title: 'Welcome', body: 'Hello **all**', audience: 'admins' }]);
  btn('Send to copies now').click();
  await new Promise((r) => setTimeout(r, 0));
  assert.deepEqual(sent[3], ['push', 'otter']);
  assert.ok(toasts.some((m) => /3/.test(m)));
  // A shipped kit: built-in chats have no Edit/Remove and there is no "Stop managing"
  kit = { managed: true, copyable: true, shipped: true, auto_clone: true, copies: 1,
    chats: [{ id: 'meet-atlas', title: 'Meet Atlas', audience: 'everyone', builtin: true, body: '' }] };
  const box2 = await kitSection({ id: 'atlas', name: 'Atlas' }, {}, calls);
  assert.equal([...box2.querySelectorAll('li button')].length, 0);
  assert.ok(![...box2.querySelectorAll('button')].some((b) => b.textContent === 'Stop managing copies'));
});

test('chatsOnly (a 4.2 template\'s Details): chats and Push only, no auto-clone switch, no Manage/Stop managing', { skip: dom.skip }, async () => {
  await useDom();
  const { kitSection } = await import('../static/js/bot-kits.js?v=3');
  for (const managed of [true, false]) {
    const kit = { managed, copyable: false, shipped: false, auto_clone: true, copies: 3,
      chats: [{ id: 'c0000abcd', title: 'Rules', body: 'Be kind', audience: 'everyone' }] };
    const box = await kitSection({ id: 'otter', name: 'Otter' }, { toast() {}, ask: async () => 'ok', chatsOnly: true },
      { get: async () => kit, save: async () => kit, push: async () => ({ ...kit, posted: 1 }), remove: async () => kit });
    const texts = [...box.querySelectorAll('button')].map((b) => b.textContent);
    assert.ok(!box.querySelector('input[role="switch"]'), `managed=${managed}: no switch`);
    assert.ok(!texts.includes('Stop managing copies') && !texts.includes('Manage copies of this bot'), texts.join('|'));
    assert.ok(texts.includes('Send to copies now') && texts.includes('Add a chat'), texts.join('|'));
    assert.ok(!/New profiles get a copy|Copies: \d|Make this bot a template/.test(box.textContent), box.textContent);
    assert.match(box.textContent, /Rules/);
  }
});
