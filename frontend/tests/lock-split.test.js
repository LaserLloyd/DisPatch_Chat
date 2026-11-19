// 4.4 the split lock screen (js/lock-split.js, docs/design/lock-resume.md).
// The bottom half must say nothing about whose session it resumes; the secret is wiped on
// every exit; a wrong secret is handed to main.js (which goes to main).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { domSkip } from './_require-dom.js';

const HERE = dirname(fileURLToPath(import.meta.url));
const SRC = readFileSync(join(HERE, '..', 'static', 'js', 'lock-split.js'), 'utf8');
const MAIN = readFileSync(join(HERE, '..', 'static', 'js', 'main.js'), 'utf8');

let jsdom = null;
try { jsdom = createRequire(import.meta.url)('jsdom'); } catch { /* skipped below */ }

async function mount(opts) {
  const win = new jsdom.JSDOM('<!doctype html><html><body><div id="h"></div></body></html>', { url: 'https://dispatch.test/' }).window;
  for (const k of ['window', 'document', 'localStorage', 'HTMLElement', 'Node']) globalThis[k] = win[k] ?? win;
  globalThis.window = win;
  const { renderLockSplit } = await import('../static/js/lock-split.js');
  const host = win.document.getElementById('h');
  const ctl = renderLockSplit(host, opts);
  return { win, host, ctl };
}

const MAIN_PROFILE = { id: 'p-main', name: 'Family', avatar: null };

test('lock-split takes no held-profile input at all (only main, resumable and callbacks)', () => {
  assert.match(SRC, /export function renderLockSplit\(host, \{ main = null, resumable = false, onMain, onOthers, onResume \}\)/);
  // main.js passes the lock-target default, never state.auth.profile or anything held.
  const fn = MAIN.slice(MAIN.indexOf('function showLockSplit('), MAIN.indexOf('function hideLockSplit('));
  assert.doesNotMatch(fn, /state\.auth\.profile\b/);
  assert.match(fn, /state\.auth\.defaultProfile/);
});

test('the bottom half names nobody and the field is neutral',
  { skip: domSkip(jsdom ? false : 'jsdom is not installed') }, async () => {
    const { host } = await mount({ main: MAIN_PROFILE, resumable: true, onMain() {}, onOthers() {}, onResume: async () => false });
    const bottom = host.querySelector('.lks-bottom');
    assert.ok(bottom, 'resume half shown');
    const html = bottom.outerHTML;
    assert.doesNotMatch(html, /Family|p-main|<img/);
    const f = host.querySelector('#lks-field');
    assert.equal(f.type, 'password');
    assert.equal(f.getAttribute('maxlength'), '128');
    assert.equal(f.getAttribute('autocomplete'), 'one-time-code');
    assert.equal(f.getAttribute('placeholder'), null);
    // The top half is the only identity: main's name.
    assert.match(host.querySelector('.lks-top').textContent, /Family/);
  });

test('no bottom half when nothing is resumable',
  { skip: domSkip(jsdom ? false : 'jsdom is not installed') }, async () => {
    const { host } = await mount({ main: MAIN_PROFILE, resumable: false, onMain() {}, onOthers() {}, onResume: async () => false });
    assert.equal(host.querySelector('.lks-bottom'), null);
    assert.equal(host.querySelector('#lks-field'), null);
  });

test('the pad types, the secret is sent once and wiped before the answer',
  { skip: domSkip(jsdom ? false : 'jsdom is not installed') }, async () => {
    const seen = [];
    let fieldAtCall = null;
    const { host } = await mount({
      main: MAIN_PROFILE, resumable: true, onMain() {}, onOthers() {},
      onResume: async (s) => { seen.push(s); fieldAtCall = host.querySelector('#lks-field').value; return false; },
    });
    for (const k of ['1', '2', '3', '4', 'back', '5']) host.querySelector(`button[data-k="${k}"]`).click();
    host.querySelector('.lks-submit').click();
    await new Promise((r) => setTimeout(r, 0));
    assert.deepEqual(seen, ['1235']);
    assert.equal(fieldAtCall, '', 'wiped before the server answers');
    assert.equal(host.querySelector('#lks-field').value, '');
  });

test('Go to main and destroy wipe the field',
  { skip: domSkip(jsdom ? false : 'jsdom is not installed') }, async () => {
    let went = 0;
    const { host, ctl } = await mount({ main: MAIN_PROFILE, resumable: true, onMain() { went += 1; }, onOthers() {}, onResume: async () => false });
    const f = host.querySelector('#lks-field');
    f.value = 'secret';
    host.querySelector('#lks-main').click();
    assert.equal(went, 1);
    assert.equal(f.value, '');
    f.value = 'again';
    ctl.destroy();
    assert.equal(f.value, '');
    assert.equal(host.children.length, 0);
  });

test('a wrong resume goes to main in main.js (no retry prompt, nothing about whose)', () => {
  const fn = MAIN.slice(MAIN.indexOf('async function resumeWith('), MAIN.indexOf('function keepUnlockedRow('));
  assert.match(fn, /if \(!r\.ok \|\| !\(r\.body && r\.body\.ok\)\) \{[\s\S]*?goToMain\(\);\s+return false;/);
  assert.match(fn, /await reboot\(\{ resumed: true \}\);/);
  assert.match(MAIN, /if \(!resumed\) showSignInNoticeWhenReady\(\);/);
});
