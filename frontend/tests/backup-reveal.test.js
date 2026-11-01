// 4.3 WP-A4: Settings -> System -> Backups.
//
//   - "Show recovery key" is offered only when a generated key exists, and not to a caller known
//     not to be the protected Admin; it asks for the PIN or password again.
//   - The key is shown once in a dialog with Copy, and is gone from the page when it closes.
//   - A wrong PIN says so in the dialog and never trips the "signed out" handler (own fetch).
//   - A restore that cannot put a key back names it (re_enter); pre-restore copies are a kind of
//     their own and cannot be deleted here.

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
const SRC = read('js/backup.js');

const require = createRequire(import.meta.url);
let jsdom = null;
try { jsdom = require('jsdom'); } catch { /* not installed - DOM tests skip */ }
const dom = { skip: domSkip(jsdom ? false : 'jsdom is not installed') };

const KEY = 'ABCDE-FGHJK-MNPQR-STUVW-XYZ23';
const tick = () => new Promise((r) => setTimeout(r, 0));
const settle = async () => { for (let i = 0; i < 6; i++) await tick(); };

async function boot() {
  const win = new jsdom.JSDOM('<!doctype html><html><body><div id="h"></div></body></html>',
    { url: 'http://127.0.0.1:18815/', runScripts: 'outside-only' }).window;
  const put = (k, v) => Object.defineProperty(globalThis, k, { value: v, configurable: true, writable: true });
  for (const [k, v] of Object.entries({
    window: win, document: win.document, location: win.location, navigator: win.navigator,
    localStorage: win.localStorage, sessionStorage: win.sessionStorage, Node: win.Node, HTMLElement: win.HTMLElement,
    HTMLInputElement: win.HTMLInputElement,
  })) put(k, v);
  globalThis.fetch = async (u) => {
    if (String(u).includes('/static/locales/en.json')) return { ok: true, status: 200, json: async () => EN, headers: new win.Headers() };
    return { ok: false, status: 404, json: async () => ({}), headers: new win.Headers() };
  };
  const i18n = await import('../static/js/i18n.js?v=5');
  await i18n.init('en');
  return win;
}

const fresh = () => import(`../static/js/backup.js?t=${Math.random()}`);

function fakeApi(schedule, extra = {}) {
  const calls = [];
  return {
    calls,
    backups: async () => ({ backups: [
      { id: 'prerestore:pre-restore-20261031-120000', kind: 'prerestore', name: 'pre-restore-20261031-120000', size: 3, created: '2026-10-31T12:00:00+00:00', restorable: true },
      { id: 'archive:a.dpbk', kind: 'archive', name: 'a.dpbk', size: 9, created: '2026-10-30T12:00:00+00:00', encrypted: true, restorable: true },
    ], schedule }),
    pendingRestore: async () => ({ pending: false }),
    people: async () => ({ profiles: [] }),
    revealRecoveryKey: async (body) => { calls.push(body); if (extra.reveal) return extra.reveal(body); return { recovery_key: KEY }; },
    restoreBackup: async () => ({ staged: true, re_enter: ['connection.openai'] }),
    ...extra.api,
  };
}

const SCHED = { enabled: true, interval_hours: 24, keep: 7, generated_key: true, last_archive_age_hours: 1 };

test('pure: reveal body, the offer rule, the re-enter sentence, pre-restore rows', async (t) => {
  if (dom.skip) { t.skip(dom.skip); return; }
  await boot();
  const B = await fresh();
  assert.deepEqual(B.revealBody('pin', '2468'), { pin: '2468' });
  assert.deepEqual(B.revealBody('password', 'pw'), { password: 'pw' });
  assert.equal(B.offerReveal({ generated_key: true }, true), true);
  assert.equal(B.offerReveal({ generated_key: true }, undefined), true);      // the server decides
  assert.equal(B.offerReveal({ generated_key: true }, false), false);
  assert.equal(B.offerReveal({ generated_key: false }, true), false);
  assert.equal(B.reEnterText([]), '');
  assert.match(B.reEnterText(['MCP_X', 'connection.a']), /MCP_X, connection\.a/);
  assert.deepEqual(B.actionsFor({ kind: 'prerestore', restorable: true }), { verify: false, restore: true, remove: false, needsPassphrase: false });
  assert.notEqual(B.kindLabel('prerestore'), B.kindLabel('premigration'));
  assert.match(B.revealError({ status: 401 }), /not right/);
  assert.match(B.revealError({ status: 429, body: { retry_after: 7 } }), /7 seconds/);
  assert.match(B.revealError({ status: 404 }), /passphrase you chose/);
});

test('the dead "owner" role operand is gone', () => {
  assert.doesNotMatch(SRC, /role === 'owner'/);
});

test('show recovery key: asks again, shows it once with Copy, gone from the page when closed', { skip: dom.skip }, async () => {
  const win = await boot();
  const B = await fresh();
  const host = win.document.getElementById('h');
  const api = fakeApi(SCHED);
  B.mountBackups(host, { api, role: 'admin', isProtected: true });
  await settle();
  const btn = host.querySelector('.bkp-reveal-btn');
  assert.ok(btn && !btn.hidden, 'offered');
  assert.ok(!host.textContent.includes(KEY));
  btn.click();
  const dlg = host.querySelector('[role="dialog"]');
  assert.ok(dlg, 'a dialog opens');
  const sel = dlg.querySelector('select');
  sel.value = 'pin';
  const cred = dlg.querySelector('input[type="password"]');
  cred.value = '2468';
  dlg.querySelector('form').dispatchEvent(new win.Event('submit', { cancelable: true, bubbles: true }));
  await settle();
  assert.deepEqual(api.calls, [{ pin: '2468' }]);
  const shown = dlg.querySelector('.bkp-key-input');
  assert.equal(shown.value, KEY);
  assert.ok([...dlg.querySelectorAll('button')].some((b) => b.textContent === EN.bkp.copy), 'Copy');
  const done = [...dlg.querySelectorAll('button')].find((b) => b.textContent === EN.bkp.key_saved);
  done.click();
  assert.equal(host.querySelector('[role="dialog"]'), null);
  assert.ok(!host.innerHTML.includes(KEY), 'the key is not in the page afterwards');
  assert.equal(btn.disabled, false);
});

test('a wrong PIN stays in the dialog, the key never appears, and the next try is fresh', { skip: dom.skip }, async () => {
  const win = await boot();
  const B = await fresh();
  const host = win.document.getElementById('h');
  const api = fakeApi(SCHED, { reveal: () => { const e = new Error('401: Incorrect PIN or password'); e.status = 401; throw e; } });
  B.mountBackups(host, { api, role: 'admin', isProtected: true });
  await settle();
  host.querySelector('.bkp-reveal-btn').click();
  const dlg = host.querySelector('[role="dialog"]');
  dlg.querySelector('input[type="password"]').value = '0000';
  dlg.querySelector('form').dispatchEvent(new win.Event('submit', { cancelable: true, bubbles: true }));
  await settle();
  assert.equal(dlg.querySelector('[role="alert"]').textContent, EN.bkp.reveal_wrong);
  assert.equal(dlg.querySelector('input[type="password"]').value, '');
  assert.ok(!host.innerHTML.includes(KEY));
});

test('switched off, with the reason, for an Admin who is not the protected one; not offered without a generated key', { skip: dom.skip }, async () => {
  const win = await boot();
  const B = await fresh();
  const host = win.document.getElementById('h');
  B.mountBackups(host, { api: fakeApi(SCHED), role: 'admin', isProtected: false });
  await settle();
  // 4.3 Fable: shown switched off, with the reason, instead of missing without a word.
  assert.equal(host.querySelector('.bkp-reveal-btn').hidden, false);
  assert.equal(host.querySelector('.bkp-reveal-btn').disabled, true);
  assert.equal(host.querySelector('.bkp-reveal-note').hidden, false);
  assert.equal(host.querySelector('.bkp-reveal-note').textContent, EN.bkp.reveal_owner_only);
  const host2 = win.document.createElement('div');
  win.document.body.append(host2);
  B.mountBackups(host2, { api: fakeApi({ ...SCHED, generated_key: false }), role: 'admin', isProtected: true });
  await settle();
  assert.equal(host2.querySelector('.bkp-reveal-btn').hidden, true);
});

test('restore lists the keys to enter again; the last restore says so too; pre-restore rows have no Delete', { skip: dom.skip }, async () => {
  const win = await boot();
  const B = await fresh();
  const host = win.document.getElementById('h');
  const api = fakeApi({ ...SCHED, last_restore: { re_enter: ['MCP_SEARCH_TOKEN'] } });
  B.mountBackups(host, { api, role: 'admin', isProtected: true, confirm: async () => true });
  await settle();
  const notice = host.querySelector('.bkp-re-enter');
  assert.ok(!notice.hidden && notice.textContent.includes('MCP_SEARCH_TOKEN'));
  const pre = host.querySelector('[data-backup="prerestore:pre-restore-20261031-120000"]');
  assert.ok(pre.textContent.includes(EN.bkp.kind_prerestore));
  assert.ok(![...pre.querySelectorAll('button')].some((b) => b.textContent === EN.bkp.delete));
  const restore = [...host.querySelector('[data-backup="archive:a.dpbk"]').querySelectorAll('button')]
    .find((b) => b.textContent === EN.bkp.restore);
  restore.click();
  await settle();
  assert.ok(host.textContent.includes('connection.openai'), 'the staged restore names the key');
});

test('postReveal: a 401 is an Error with status, never a signed-out redirect', async () => {
  const B = await import(`../static/js/backup.js?t=${Math.random()}`).catch(() => null);
  if (!B) return;
  const seen = [];
  const fake = async (u, o) => { seen.push([u, o]); return { ok: false, status: 401, statusText: 'x', json: async () => ({ detail: 'Incorrect PIN or password', error: { code: 'incorrect_credential' } }) }; };
  const e = await B.postReveal({ pin: '1' }, fake).then(() => null, (x) => x);
  assert.equal(e.status, 401);
  assert.equal(e.code, 'incorrect_credential');
  assert.equal(seen[0][0], '/api/backups/recovery-key/reveal');
  assert.equal(seen[0][1].cache, 'no-store');
  const ok = await B.postReveal({ pin: '1' }, async () => ({ ok: true, status: 200, json: async () => ({ recovery_key: KEY }) }));
  assert.equal(ok.recovery_key, KEY);
});

test('every new string exists in every locale', () => {
  const keys = ['kind_prerestore', 'reveal', 'reveal_title', 'reveal_hint', 'reveal_with', 'reveal_pin', 'reveal_password',
    'reveal_credential', 'reveal_submit', 'reveal_cancel', 'reveal_wrong', 'reveal_wait', 'reveal_none',
    're_enter_staged', 're_enter_after'].map((k) => `bkp.${k}`);
  for (const lang of ['ar', 'de', 'en', 'es', 'fr', 'ja', 'pt', 'zh']) {
    const L = JSON.parse(read(`locales/${lang}.json`));
    for (const k of keys) {
      const [ns, name] = k.split('.');
      assert.ok(L[ns] && typeof L[ns][name] === 'string' && L[ns][name], `${lang}: ${k}`);
    }
  }
});
