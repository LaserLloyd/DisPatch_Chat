// The privacy wipe list, enforced.
//
// Run: node --test frontend/tests/
//
// Privacy mode promises the device keeps nothing. It keeps that promise by
// removing a HAND-WRITTEN list of keys (privacy.js APP_KEYS), so the promise is
// only as good as somebody remembering to extend the list when they add a
// preference. Twice now they did not: `lc-remember` (the remembered-unlock
// preference — the single worst thing to leave on a shared tablet) survived one
// release, and `dispatch-pinned-settings` survived another, quietly telling the
// next user which settings the last one had pinned.
//
// The list's own comment already says "DERIVE THIS LIST, DO NOT GUESS AT IT"
// and gives the grep. This runs that grep.
//
// A key that is NOT wiped is a deliberate decision, and there are exactly two.
// They live in EXEMPT below with the reason, so declining to wipe something is
// a line of code somebody has to write rather than a line nobody wrote.

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import { APP_KEYS } from '../static/js/privacy.js';

const HERE = dirname(fileURLToPath(import.meta.url));
const STATIC = join(HERE, '..', 'static');
const JS_DIR = join(STATIC, 'js');

const EXEMPT = new Map([
  // The privacy flag itself. Wiping it would turn privacy mode off on the way
  // out and the device would silently start caching again on the next load.
  ['dispatch-privacy', 'privacy.js FLAG_KEY — the setting that drives the wipe'],
  // No-Image Mode is a RATCHET: once on, it takes the PIN to turn off. Wiping
  // it would make "clear this device" a one-click way around that gate on a
  // handed-over tablet, which is the exact scenario NIM exists for. See the
  // matching note in privacy.js — do not "fix" this by wiping it.
  ['dispatch-nim', 'nim.js FLAG_KEY — a PIN-gated ratchet, deliberately kept'],
]);

const sources = () => {
  const out = [['index.html', readFileSync(join(STATIC, 'index.html'), 'utf8')]];
  for (const f of readdirSync(JS_DIR).filter((f) => f.endsWith('.js'))) {
    out.push([`js/${f}`, readFileSync(join(JS_DIR, f), 'utf8')]);
  }
  return out;
};

const STR = /^(['"`])([^'"`]*)\1$/;

/** Every localStorage key this file can write, and how we know.
 *
 *  Three resolutions, in order — a writer must be readable by ONE of them or
 *  the test fails rather than shrugging:
 *
 *   1. a string literal at the call:   setItem('tl-collapsed', …)
 *   2. a const in the same file:       setItem(FLAG_KEY, …) + const FLAG_KEY = '…'
 *   3. a file that writes through a helper (theme.js's set(key, val) takes the
 *      key as a PARAMETER, so there is nothing at the call site to read):
 *      every `const *_KEY = '<literal>'` the module declares.
 */
function keysWrittenBy(src) {
  const found = new Set();
  const consts = new Map();
  for (const m of src.matchAll(/\bconst\s+([A-Za-z_$][\w$]*)\s*=\s*(['"`])([^'"`]*)\2\s*;/g)) {
    consts.set(m[1], m[3]);
  }
  let unresolved = false;
  for (const m of src.matchAll(/localStorage\.setItem\(\s*([^,]+?)\s*,/g)) {
    const arg = m[1].trim();
    const lit = STR.exec(arg);
    if (lit) { found.add(lit[2]); continue; }
    if (consts.has(arg)) { found.add(consts.get(arg)); continue; }
    unresolved = true;                       // a parameter — fall back to (3)
  }
  if (unresolved) {
    for (const [name, val] of consts) if (/_KEY$/.test(name)) found.add(val);
  }
  return { keys: found, unresolved };
}

test('every localStorage key the app writes is wiped, or explicitly exempt', () => {
  const wiped = new Set(APP_KEYS);
  const orphans = [];
  const opaque = [];
  for (const [file, src] of sources()) {
    const { keys, unresolved } = keysWrittenBy(src);
    if (unresolved && !keys.size) opaque.push(file);
    for (const k of keys) {
      if (wiped.has(k) || EXEMPT.has(k)) continue;
      orphans.push(`${k}  (written by ${file})`);
    }
  }
  assert.deepEqual(opaque, [], '\nThese files write localStorage through a key this test cannot resolve.\n'
    + 'Name the key in a `const SOMETHING_KEY = \'…\'` so the wipe list can be checked:\n'
    + opaque.join('\n'));
  assert.deepEqual(orphans.sort(), [],
    '\nThese keys survive privacy mode\'s wipe. Privacy mode promises the device\n'
    + 'keeps NOTHING, so each one is either a miss (add it to APP_KEYS in\n'
    + 'privacy.js) or a deliberate exception (add it to EXEMPT in this file,\n'
    + 'with the reason):\n\n' + orphans.join('\n'));
});

// The other direction: a key in APP_KEYS that nothing writes any more is dead
// weight, and dead weight is how the list came to look maintained while two
// real keys were missing from it (three phantom entries — dispatch-draft,
// dispatch-thread-collapsed, dispatch-last-bot — padded it out).
test('APP_KEYS names no key that nothing writes', () => {
  const written = new Set();
  for (const [, src] of sources()) for (const k of keysWrittenBy(src).keys) written.add(k);
  const phantom = APP_KEYS.filter((k) => !written.has(k));
  assert.deepEqual(phantom, [],
    '\nAPP_KEYS lists keys no code writes. Remove them — a padded list reads as\n'
    + 'a maintained one:\n' + phantom.join('\n'));
});

test('the two documented exemptions are still the only ones', () => {
  // A cheap tripwire on the exemption list itself: growing it is exactly how a
  // wipe promise gets hollowed out one "just this key" at a time, so adding an
  // entry should require deliberately editing this expectation too.
  assert.deepEqual([...EXEMPT.keys()].sort(), ['dispatch-nim', 'dispatch-privacy']);
});
