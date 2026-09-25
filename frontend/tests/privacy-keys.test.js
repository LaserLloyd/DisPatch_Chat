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
//
// localStorage is no longer the only thing this app keeps. js/store.js holds
// drafts, a queued outbox and the tail of recently-opened threads in
// IndexedDB — actual message text, not a preference — so the same derive-it
// rule now covers database names too (APP_DATABASES, checked at the bottom of
// this file). A privacy wipe that cleared the theme and left the conversation
// would be the promise broken in the loudest possible way.

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import { APP_KEYS, APP_KEY_PREFIXES, APP_DATABASES } from '../static/js/privacy.js';

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
  // The theme runtime lives in the drop-in folder static/ui-theme/, not under
  // js/, and it is the ONLY writer of the theme pick (`dispatch-palette`). Leaving it out
  // would make that key look like a phantom in APP_KEYS — or, worse, let a
  // future storage key the runtime adds slip past the wipe unseen.
  // Its storage key is not in the file: the page sets it as data-storage-key
  // on the runtime's <script> tag, so the two are read together (rule 4).
  const html = out[0][1].replace(/<!--[\s\S]*?-->/g, '');
  const tag = /<script\b[^>]*src="\/static\/ui-theme\/ui-theme\.js"[^>]*>/.exec(html);
  const key = tag && /\bdata-storage-key="([^"]+)"/.exec(tag[0]);
  // Comments are dropped first: the runtime's usage notes show example tags.
  const runtime = readFileSync(join(STATIC, 'ui-theme', 'ui-theme.js'), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');
  out.push(['ui-theme/ui-theme.js', runtime
    + (key ? `\n/* index.html */ data-storage-key="${key[1]}"` : '')]);
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
 *   3. a file that writes through a helper (a set(key, val) that takes the
 *      key as a PARAMETER, so there is nothing at the call site to read):
 *      every `const *_KEY = '<literal>'` the module declares.
 *   4. the theme runtime, whose helper writes the key the page names on its
 *      <script> tag: `data-storage-key="…"` (sources() appends it).
 */
function keysWrittenBy(src) {
  const found = new Set();
  const prefixes = new Set();
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
    // 5. a key family: setItem(SOMETHING_PREFIX + id, …) with the prefix a
    //    const in the same file — must match privacy.js APP_KEY_PREFIXES.
    const fam = /^([A-Za-z_$][\w$]*_PREFIX)\s*\+/.exec(arg);
    if (fam && consts.has(fam[1])) { prefixes.add(consts.get(fam[1])); continue; }
    unresolved = true;                       // a parameter — fall back to (3)
  }
  if (unresolved) {
    for (const [name, val] of consts) if (/_KEY$/.test(name)) found.add(val);
    const tagKey = /\bdata-storage-key="([^"]+)"/.exec(src);
    if (tagKey) found.add(tagKey[1]);
  }
  return { keys: found, prefixes, unresolved };
}

test('every localStorage key the app writes is wiped, or explicitly exempt', () => {
  const wiped = new Set(APP_KEYS);
  const orphans = [];
  const opaque = [];
  for (const [file, src] of sources()) {
    const { keys, prefixes, unresolved } = keysWrittenBy(src);
    if (unresolved && !keys.size) opaque.push(file);
    for (const p of prefixes) {
      if (!APP_KEY_PREFIXES.includes(p)) orphans.push(`${p}*  (key family written by ${file})`);
    }
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

// ===========================================================================
// Databases. Same rule, different storage: derive the list, do not guess at it.
//
// store.js does not call indexedDB.open directly — it takes its factory as an
// argument so the tests can hand it an in-memory one — so the name is read off
// the `export const DB_NAME` it declares. Any future module that opens a
// database by literal is caught by the second pattern.
// ===========================================================================

/** Every database name this file can be shown to open. */
function databasesNamedBy(src) {
  const found = new Set();
  // 1. `export const DB_NAME = 'dispatch-store';` — a module that declares the
  //    name it hands to an injected factory.
  for (const m of src.matchAll(/\bconst\s+[\w$]*DB_NAME[\w$]*\s*=\s*(['"`])([^'"`]*)\1\s*;/g)) {
    found.add(m[2]);
  }
  // 2. a literal passed straight to the real thing.
  for (const m of src.matchAll(/indexedDB\.(?:open|deleteDatabase)\(\s*(['"`])([^'"`]*)\1/g)) {
    found.add(m[2]);
  }
  return found;
}

test('every IndexedDB database the app opens is wiped', () => {
  const wiped = new Set(APP_DATABASES);
  const orphans = [];
  for (const [file, src] of sources()) {
    // privacy.js itself names them in order to delete them.
    if (file === 'js/privacy.js') continue;
    for (const name of databasesNamedBy(src)) {
      if (wiped.has(name)) continue;
      orphans.push(`${name}  (opened by ${file})`);
    }
  }
  assert.deepEqual(orphans.sort(), [],
    '\nThese IndexedDB databases survive privacy mode\'s wipe. They hold real\n'
    + 'content — drafts, queued sends, cached messages — not preferences, so a\n'
    + 'device that kept them kept the conversation. Add each to APP_DATABASES in\n'
    + 'privacy.js:\n\n' + orphans.join('\n'));
});

test('APP_DATABASES names no database that nothing opens', () => {
  const opened = new Set();
  for (const [file, src] of sources()) {
    if (file === 'js/privacy.js') continue;
    for (const name of databasesNamedBy(src)) opened.add(name);
  }
  const phantom = APP_DATABASES.filter((n) => !opened.has(n));
  assert.deepEqual(phantom, [],
    '\nAPP_DATABASES lists databases no code opens. Remove them — a padded list\n'
    + 'reads as a maintained one:\n' + phantom.join('\n'));
});

test('APP_KEY_PREFIXES names no key family that nothing writes, and the wipe clears a family', async () => {
  const written = new Set();
  for (const [, src] of sources()) for (const p of keysWrittenBy(src).prefixes) written.add(p);
  assert.deepEqual(APP_KEY_PREFIXES.filter((p) => !written.has(p)), []);
  assert.ok(written.has('dispatch-tool-state:'), 'tools.js writes the per-tool layout family');
  assert.match(readFileSync(join(JS_DIR, 'privacy.js'), 'utf8'), /APP_KEY_PREFIXES\.some\(/,
    'wipeAppKeys must clear every key of each family');
});

