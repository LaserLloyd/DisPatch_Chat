// WP-FE-YOU: My bots, the bot editor, AI build, "Make my own copy" and Reset to default
// (js/my-bots.js, js/bot-editor.js, js/account-reset.js). Real modules and real English
// strings against a stubbed fetch; the live routes are in the WP-PROF / WP-AIB reports.

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { domSkip } from './_require-dom.js';

const HERE = dirname(fileURLToPath(import.meta.url));
const STATIC = join(HERE, '..', 'static');
const HTML = readFileSync(join(STATIC, 'index.html'), 'utf8');
const EN = JSON.parse(readFileSync(join(STATIC, 'locales', 'en.json'), 'utf8'));
const SRC = Object.fromEntries(['my-bots', 'bot-editor', 'account-reset'].map(
  (n) => [n, readFileSync(join(STATIC, 'js', `${n}.js`), 'utf8')]));

const require = createRequire(import.meta.url);
let jsdom = null;
try { jsdom = require('jsdom'); } catch { /* DOM tests skip */ }
const dom = { skip: domSkip(jsdom ? false : 'jsdom is not installed') };

const PRESETS = { presets: [
  { id: 'default', title: 'Default', text: 'You are {name}.' },
  { id: 'sleek', title: 'Sleek', text: 'You are {name}, sleek.' },
] };
const VOICES = { voices: [
  { id: 'v-house', name: 'Household voice', household: true, mine: false },
  { id: 'v-mine', name: 'My clone', household: false, mine: true },
] };

let calls = [];
let routes = {};

async function setup(extra = {}) {
  const win = new jsdom.JSDOM(HTML, { url: 'http://127.0.0.1:8802/', runScripts: 'outside-only' }).window;
  const put = (k, v) => Object.defineProperty(globalThis, k, { value: v, configurable: true, writable: true });
  put('window', win); put('document', win.document); put('location', win.location);
  put('navigator', win.navigator); put('localStorage', win.localStorage);
  put('Node', win.Node); put('HTMLElement', win.HTMLElement); put('HTMLInputElement', win.HTMLInputElement);
  calls = [];
  routes = {
    'GET /static/locales/en.json': () => [200, EN],
    'GET /api/me/personas': () => [200, PRESETS],
    'GET /api/extensions': () => [200, { extensions: [{ id: 'voice', entry: 'index.js' }] }],
    'GET /api/voice/profiles': () => [200, VOICES],
    'GET /api/voice/status': () => [404, { detail: 'Not Found' }],
    'GET /api/me/backends': () => [200, { may_choose: false, default: '', connections: [] }],
    ...extra,
  };
  globalThis.fetch = async (u, opts = {}) => {
    const key = `${(opts.method || 'GET').toUpperCase()} ${u}`;
    const body = typeof opts.body === 'string' ? JSON.parse(opts.body) : (opts.body ? '[form]' : null);
    calls.push({ key, body });
    const h = routes[key];
    const [status, out] = h ? h(body) : [404, { detail: 'Not Found' }];
    return { ok: status >= 200 && status < 300, status, headers: new win.Headers(), json: async () => out, blob: async () => new Blob(['x']) };
  };
  const i18n = await import('../static/js/i18n.js?v=4');
  await i18n.init('en');
  calls.length = 0;
  const editor = await import('../static/js/bot-editor.js?v=1');
  const mybots = await import('../static/js/my-bots.js?v=1');
  const reset = await import('../static/js/account-reset.js?v=1');
  const toasts = [];
  const shell = { toast: (m) => toasts.push(m), confirm: async () => true,
    state: { auth: { permissions: ['create_characters', 'choose_backend', 'manage_history', 'use_images'], profile: { id: 'p1', name: 'Uma' } } } };
  for (const m of [editor, mybots, reset]) m[Object.keys(m).find((k) => k.startsWith('init'))](shell);
  return { win, editor, mybots, reset, toasts, shell };
}

const tick = async (n = 8) => { for (let i = 0; i < n; i++) await new Promise((r) => setTimeout(r, 0)); };
const click = (win, node) => node.dispatchEvent(new win.Event('click', { bubbles: true }));
const posts = (re) => calls.filter((c) => re.test(c.key));

// ------------------------------------------------------------------ pure logic

test('validateBot: a name is required and capped at 40, the personality at 4,000', { skip: dom.skip }, async () => {
  const { editor } = await setup();
  assert.deepEqual(editor.validateBot({ name: 'Pixel', persona: 'x'.repeat(4000) }), {});
  assert.ok(editor.validateBot({ name: '  ' }).name);
  assert.ok(editor.validateBot({ name: 'n'.repeat(41) }).name);
  assert.match(editor.validateBot({ name: 'a', persona: 'x'.repeat(4001) }).persona, /4,001 characters; the limit is 4,000/);
  assert.ok(editor.validateBot({ name: 'a', emoji: '12345678901' }).emoji);
});

test('botPayload: a preset text goes as its id, own text as persona, a PATCH carries only changes', { skip: dom.skip }, async () => {
  const { editor } = await setup();
  const form = { name: 'Pixel', emoji: '', persona: 'You are {name}, sleek.', voice: '', connection: '' };
  assert.deepEqual(editor.botPayload(form, null, PRESETS.presets), { name: 'Pixel', personality: 'sleek' });
  assert.deepEqual(editor.botPayload({ ...form, persona: 'Custom text', voice: 'v-mine' }, null, PRESETS.presets),
    { name: 'Pixel', persona: 'Custom text', voice: 'v-mine' });
  const orig = { id: 'p1', name: 'Pixel', emoji: '', voice: 'v-mine', personality: { text: 'Custom text' }, backend: { connection: 'c1' } };
  assert.deepEqual(editor.botPayload({ name: 'Pixel', emoji: '', persona: 'Custom text', voice: 'v-mine', connection: 'c1' }, orig, []), {});
  assert.deepEqual(editor.botPayload({ name: 'Pix', emoji: '', persona: 'Custom text', voice: '', connection: 'c2' }, orig, []),
    { name: 'Pix', voice: '', connection: 'c2' });
});

test('voiceGroups splits the caller\'s clones from household voices; options hide what a User cannot read', { skip: dom.skip }, async () => {
  const { editor } = await setup();
  assert.deepEqual(editor.voiceGroups(VOICES.voices), {
    mine: [{ id: 'v-mine', name: 'My clone' }], household: [{ id: 'v-house', name: 'Household voice' }] });
  const o = await editor.loadEditorOptions({ state: { auth: { permissions: ['create_characters'] } } });
  assert.equal(o.voices.mine.length, 1);
  assert.deepEqual(o.connections, []);              // no choose_backend: not even asked
  assert.equal(posts(/\/api\/me\/backends|\/api\/connections/).length, 0);
  routes['GET /api/me/personas'] = () => [403, { detail: 'no' }];
  assert.deepEqual((await editor.loadEditorOptions()).presets, []);   // a 403 hides presets, never throws
});

test('without the voice add-on the editor never calls the voice routes (no 404 in the console)', { skip: dom.skip }, async () => {
  const { editor } = await setup({ 'GET /api/extensions': () => [200, { extensions: [] }] });
  const o = await editor.loadEditorOptions({ state: { auth: { permissions: ['create_characters'] } } });
  assert.deepEqual(o.voices, { mine: [], household: [], builtin: [] });
  assert.equal(posts(/\/api\/voice\//).length, 0);
});

// ------------------------------------------------------------------ the editor

test('editor: an empty name is refused before any request; a valid form POSTs once and resolves the bot', { skip: dom.skip }, async () => {
  const { editor, win } = await setup({
    'POST /api/me/characters': (b) => [201, { id: 'p9', name: b.name, emoji: b.emoji || '' }],
  });
  const host = win.document.createElement('div');
  const done = editor.renderBotEditor(host, null, { toast: () => {} });
  await tick();
  host.querySelector('form').dispatchEvent(new win.Event('submit', { cancelable: true }));
  await tick();
  assert.equal(posts(/POST \/api\/me\/characters$/).length, 0);
  assert.match(host.querySelector('#yo-name + .yo-error').textContent, /name/i);
  // voice picker: default, then my voices, then household voices
  const groups = [...host.querySelectorAll('#yo-voice optgroup')].map((g) => g.label);
  assert.deepEqual(groups, ['My voices', 'Household voices']);
  host.querySelector('#yo-name').value = 'Pixel';
  host.querySelector('#yo-persona').value = 'You are {name}, sleek.';
  host.querySelector('#yo-voice').value = 'v-mine';
  host.querySelector('form').dispatchEvent(new win.Event('submit', { cancelable: true }));
  const saved = await done;
  assert.equal(saved.id, 'p9');
  const sent = posts(/POST \/api\/me\/characters$/);
  assert.equal(sent.length, 1);
  assert.deepEqual(sent[0].body, { name: 'Pixel', personality: 'sleek', voice: 'v-mine' });
});

test('editor: a picture chosen in the form is uploaded after the bot exists', { skip: dom.skip }, async () => {
  const { editor, win } = await setup({
    'POST /api/me/characters': () => [201, { id: 'p9', name: 'Pixel' }],
    'POST /api/me/characters/p9/avatar': () => [200, { ok: true, avatar_url: '/static/avatars/p9-face.png' }],
  });
  const host = win.document.createElement('div');
  const done = editor.renderBotEditor(host, null, { toast: () => {} });
  await tick();
  host.querySelector('#yo-name').value = 'Pixel';
  Object.defineProperty(host.querySelector('#yo-file'), 'files', { value: [new Blob(['png'], { type: 'image/png' })] });
  URL.createObjectURL = () => 'blob:x'; URL.revokeObjectURL = () => {};
  host.querySelector('#yo-file').dispatchEvent(new win.Event('change'));
  host.querySelector('form').dispatchEvent(new win.Event('submit', { cancelable: true }));
  const saved = await done;
  assert.equal(saved.avatar_url, '/static/avatars/p9-face.png');
  assert.deepEqual(calls.filter((c) => /p9|characters$/.test(c.key) && c.key.startsWith('POST')).map((c) => c.key),
    ['POST /api/me/characters', 'POST /api/me/characters/p9/avatar']);
});

test('editor: a backend picker only appears when there is more than one connection', { skip: dom.skip }, async () => {
  const { editor, win } = await setup({ 'GET /api/me/backends': () => [200, { may_choose: true, default: 'b', connections: [{ id: 'a', name: 'A' }, { id: 'b', name: 'B' }] }] });
  const host = win.document.createElement('div');
  editor.renderBotEditor(host, null, {});
  await tick();
  assert.ok(host.querySelector('#yo-conn'));
  assert.equal(host.querySelector('#yo-conn').value, 'b', 'the household default is preselected');
  assert.ok(calls.some((c) => c.key === 'GET /api/me/backends'), 'the profile-safe list, not /api/connections');
  assert.ok(!calls.some((c) => c.key === 'GET /api/connections'));
  routes['GET /api/me/backends'] = () => [200, { may_choose: true, default: '', connections: [{ id: 'a', name: 'A' }] }];
  const host2 = win.document.createElement('div');
  editor.renderBotEditor(host2, null, {});
  await tick();
  assert.equal(host2.querySelector('#yo-conn'), null);
});

// ------------------------------------------------------------------ AI build

test('AI build: draft -> editor prefilled, nothing saved until Save; a failed draft offers Try again', { skip: dom.skip }, async () => {
  let draftCalls = 0;
  const { editor, win } = await setup({
    'POST /api/me/characters/draft': () => (++draftCalls === 1
      ? [502, { detail: 'draft_failed', error: { code: 'draft_failed', message: 'x' } }]
      : [200, { draft: { name: 'Mochi', emoji: '🍡', description: 'd', persona: 'You are Mochi.' } }]),
    'POST /api/me/characters': (b) => [201, { id: 'p2', name: b.name }],
  });
  const host = win.document.createElement('div');
  const done = editor.renderAiBuild(host, { toast: () => {} });
  await tick();
  host.querySelector('#yo-desc').value = 'A cheerful cooking helper';
  host.querySelector('form').dispatchEvent(new win.Event('submit', { cancelable: true }));
  await tick(20);
  assert.match(host.textContent, /didn't come out right/);
  click(win, [...host.querySelectorAll('button')].find((b) => b.textContent === 'Try again'));
  await tick(10);
  assert.equal(host.querySelector('#yo-desc').value, '', 'Try again returns to the description, to retry or reword');
  host.querySelector('#yo-desc').value = 'A cheerful cooking helper';
  host.querySelector('form').dispatchEvent(new win.Event('submit', { cancelable: true }));
  await tick(30);
  assert.equal(draftCalls, 2);
  assert.equal(host.querySelector('#yo-name').value, 'Mochi');
  assert.equal(host.querySelector('#yo-persona').value, 'You are Mochi.');
  assert.match(host.textContent, /Check the draft/);
  assert.equal(posts(/POST \/api\/me\/characters$/).length, 0, 'nothing is saved by the draft itself');
  assert.deepEqual(calls.find((c) => c.key.endsWith('/draft')).body, { description: 'A cheerful cooking helper' });
  host.querySelector('form').dispatchEvent(new win.Event('submit', { cancelable: true }));
  assert.equal((await done).id, 'p2');
  assert.equal(posts(/POST \/api\/me\/characters$/).length, 1);
});

test('AI build: a missing connection (503) says so in plain words', { skip: dom.skip }, async () => {
  const { editor, win } = await setup({ 'POST /api/me/characters/draft': () => [503, { detail: 'x' }] });
  const host = win.document.createElement('div');
  editor.renderAiBuild(host, {});
  await tick();
  host.querySelector('#yo-desc').value = 'x';
  host.querySelector('form').dispatchEvent(new win.Event('submit', { cancelable: true }));
  await tick(20);
  assert.match(host.textContent, /needs a connected model/);
});

// ------------------------------------------------------------------ My bots: list, delete, copy

const MINE = [{ id: 'p1', name: 'Pixel', emoji: '🎨', chats: 2, avatar_url: '' }];
const HOUSEHOLD = [
  { id: 'atlas', name: 'Atlas', advisor: true, agent: null },
  { id: 'seat', name: 'Seat', agent: 'seat', advisor: false },
];

test('My bots: delete asks with the chat count, deletes only after yes, and refreshes', { skip: dom.skip }, async () => {
  const { mybots, win, toasts, shell } = await setup({
    'GET /api/me/characters': () => [200, { characters: MINE }],
    'GET /api/characters': () => [200, { characters: HOUSEHOLD }],
    'DELETE /api/me/characters/p1': () => [200, { removed: 'p1', deleted: { chats: 2, pictures: 0 } }],
  });
  const asked = [];
  let answer = false;
  const deps = { ...shell, confirm: async (msg) => { asked.push(msg); return answer; } };
  const host = win.document.createElement('div');
  mybots.renderMyBots(host, deps);
  await tick(20);
  assert.equal(host.querySelectorAll('.mb-list:not(.mb-copy-list) .mb-row').length, 1);
  const del = () => [...host.querySelectorAll('.mb-row button')].find((b) => b.textContent === 'Delete');
  click(win, del()); await tick();
  assert.match(asked[0], /Delete Pixel\? This also deletes your chats with Pixel \(2\)\./);
  assert.equal(posts(/^DELETE/).length, 0, 'a "no" deletes nothing');
  answer = true;
  click(win, del()); await tick(20);
  assert.equal(posts(/^DELETE \/api\/me\/characters\/p1$/).length, 1);
  assert.ok(toasts.some((m) => /Pixel deleted/.test(m)));
});

test('My bots: only direct household bots are offered for a copy, and the copy POSTs clone', { skip: dom.skip }, async () => {
  const { mybots, win, toasts, shell } = await setup({
    'GET /api/me/characters': () => [200, { characters: [] }],
    'GET /api/characters': () => [200, { characters: HOUSEHOLD }],
    'POST /api/characters/atlas/clone': () => [201, { bot: { id: 'p5', name: 'Atlas' }, voice_cleared: true }],
  });
  const host = win.document.createElement('div');
  mybots.renderMyBots(host, shell);
  await tick(20);
  const offered = [...host.querySelectorAll('.mb-copy-list .mb-name')].map((n) => n.textContent);
  assert.deepEqual(offered, ['Atlas'], 'the agent seat is not offered');
  click(win, host.querySelector('.mb-copy-list button')); await tick(20);
  assert.equal(posts(/clone$/).length, 1);
  assert.ok(toasts.some((m) => /Copied\. Atlas is now one of your bots/.test(m)));
  assert.ok(toasts.some((m) => /voice wasn't copied/.test(m)));
});

test('copy menu: hidden for agent bots, personal bots, and a profile without create_characters', { skip: dom.skip }, async () => {
  const { mybots, shell } = await setup();
  assert.equal(mybots.copyMenuItem(HOUSEHOLD[1], shell), null);                       // agent seat
  assert.equal(mybots.copyMenuItem({ id: 'p1', name: 'P', scope: 'personal', advisor: true }, shell), null);
  assert.equal(mybots.copyMenuItem(HOUSEHOLD[0], { state: { auth: { permissions: ['use_chat'] } } }), null);
  assert.equal(mybots.copyMenuItem({ id: 'x', name: 'X', backend_kind: 'agent' }, shell), null);
  const item = mybots.copyMenuItem(HOUSEHOLD[0], shell);
  assert.equal(item.textContent, 'Make my own copy');
  assert.equal(mybots.isCopyable({ id: 'x', backend_kind: 'direct' }), true);
  assert.equal(mybots.canCopyBot(HOUSEHOLD[0], shell, [{ cloned_from: 'atlas' }]), false, 'already copied');
});

test('copy: an agent bot the server refuses (422 not_copyable) shows the plain refusal', { skip: dom.skip }, async () => {
  const { mybots, toasts, shell } = await setup({
    'POST /api/characters/atlas/clone': () => [422, { detail: 'x', error: { code: 'not_copyable', message: 'nope' } }],
  });
  assert.equal(await mybots.copyHouseholdBot(HOUSEHOLD[0], shell), null);
  assert.deepEqual(toasts, ["Agent bots can't be copied."]);
});

// ------------------------------------------------------------------ Reset to default

const PREVIEW = (bots) => ({ counts: { bots, chats: 5, pictures: 3, files: 4, voices: 1 }, token: `tok-${bots}`,
  expires_in: 300, keeps_voices: true, outcome: 'atlas' });

test('Reset: preview names the counts, the POST carries the token, a stale 409 re-previews', { skip: dom.skip }, async () => {
  let preview = 0;
  let post = 0;
  const { reset, win, shell } = await setup({
    'GET /api/me/reset/preview': () => [200, PREVIEW(++preview === 1 ? 2 : 3)],
    'POST /api/me/reset': () => (++post === 1
      ? [409, { detail: 'stale', error: { code: 'reset_stale', message: 'stale' } }]
      : [200, { deleted: { bots: 3 }, keeps_voices: true }]),
  });
  let reloaded = 0;
  const host = win.document.createElement('div');
  reset.renderAccountReset(host, { ...shell, onReset: () => { reloaded += 1; } });
  reset.renderAccountReset(host, { ...shell, onReset: () => { reloaded += 1; } });   // mounting twice adds nothing twice
  assert.equal(host.querySelectorAll('#ar-open').length, 1);
  click(win, host.querySelector('#ar-open')); await tick(20);
  const panel = host.querySelector('.ar-confirm');
  assert.equal(panel.hidden, false);
  assert.match(panel.textContent, /Reset Uma\?/);
  assert.match(panel.textContent, /This deletes: bots 2, chats 5, pictures 3, File Server files 4\. It can't be undone\./);
  assert.match(panel.textContent, /fresh Atlas/);
  assert.match(panel.textContent, /Your cloned voices are kept\./);
  click(win, host.querySelector('#ar-confirm')); await tick(30);
  assert.deepEqual(calls.find((c) => c.key === 'POST /api/me/reset').body, { token: 'tok-2' });
  assert.match(panel.textContent, /Something changed since you opened this/);
  assert.match(panel.textContent, /This deletes: bots 3,/);
  assert.equal(reloaded, 0, 'a stale token resets nothing');
  click(win, host.querySelector('#ar-confirm')); await tick(30);
  assert.deepEqual(calls.filter((c) => c.key === 'POST /api/me/reset').map((c) => c.body.token), ['tok-2', 'tok-3']);
  assert.equal(reloaded, 1);
});

test('Reset: a Basic profile is told it goes back to the Admin\'s bots; the default profile gets the shared warning', { skip: dom.skip }, async () => {
  const { reset, win, shell } = await setup({
    'GET /api/me/reset/preview': () => [200, { ...PREVIEW(1), outcome: 'household' }],
  });
  const state = { auth: { profile: { id: 'd1', name: 'DisPatch' }, default_profile: { id: 'd1' }, permissions: ['manage_history'] } };
  const host = win.document.createElement('div');
  reset.renderAccountReset(host, { ...shell, state });
  click(win, host.querySelector('#ar-open')); await tick(20);
  const txt = host.querySelector('.ar-confirm').textContent;
  assert.match(txt, /goes back to the bots an Admin picked/);
  assert.match(txt, /shared: everyone who uses it loses its bots and chats/);
  assert.ok(host.querySelector('#ar-export').getAttribute('href') === '/api/me/export');
});

test('Reset: Cancel and Esc close the panel without a POST', { skip: dom.skip }, async () => {
  const { reset, win, shell } = await setup({ 'GET /api/me/reset/preview': () => [200, PREVIEW(1)] });
  const host = win.document.createElement('div');
  win.document.body.append(host);
  reset.renderAccountReset(host, shell);
  click(win, host.querySelector('#ar-open')); await tick(20);
  click(win, host.querySelector('#ar-cancel'));
  assert.equal(host.querySelector('.ar-confirm').hidden, true);
  assert.equal(posts(/POST/).length, 0);
});

// ------------------------------------------------------------------ wiring

test('wiring: every locale key these modules use exists, and the section is declared', () => {
  const get = (k) => k.split('.').reduce((o, p) => (o == null ? o : o[p]), EN);
  const keys = new Set();
  for (const src of Object.values(SRC)) for (const m of src.matchAll(/\bt\('([a-z_]+\.[a-z_]+)'/g)) keys.add(m[1]);
  for (const k of keys) assert.equal(typeof get(k), 'string', `${k} is in en.json`);
  const nav = readFileSync(join(STATIC, 'js', 'settings-nav.js'), 'utf8');
  assert.match(nav, /id: 'mybots', group: 'you'.*perm: 'create_characters'/);
  assert.match(nav, /mybots: \[renderMyBots\]/);
  assert.match(HTML, /id="stab-mybots"/);
  assert.match(HTML, /id="spane-mybots"/);
});

test('Reset: "cloned voices are kept" only when there are clones; a Basic profile reads the Basic description', { skip: dom.skip }, async () => {
  const { reset } = await setup();
  const base = { counts: { bots: 0, chats: 2, pictures: 0, files: 0, voices: 0 }, keeps_voices: true, outcome: 'household' };
  assert.ok(!reset.confirmLines(base).includes(EN.reset.voices_kept), 'no clones: nothing to keep');
  assert.ok(reset.confirmLines({ ...base, counts: { ...base.counts, voices: 1 } }).includes(EN.reset.voices_kept));
  assert.match(SRC['account-reset'], /t\(auth && auth\.role === 'basic' \? 'reset\.desc_basic' : 'reset\.desc'\)/);
  assert.ok(EN.reset.desc_basic && !/bot/i.test(EN.reset.desc_basic));
});
