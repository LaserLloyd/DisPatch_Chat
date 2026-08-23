// The cache-busting invariant, enforced.
//
// DisPatch has no build step: freshness is a hand-maintained `?v=N` on every
// asset URL plus a `CACHE` constant in sw.js. That works exactly as long as
// somebody remembers, and commit ff051aa is the proof that somebody does not:
// it changed app.css, js/main.js and js/markdown.js and bumped NONE of the four
// counters that point at them — it even edited the sw.js version COMMENT while
// leaving the constant at v60. Every device with a warm cache kept the old
// files, and nothing failed loudly.
//
// Three cheap invariants, all statically checkable:
//   1. every `?v=` URL points at a file that exists;
//   2. every module is imported at ONE version across the whole app (the
//      two-instances bug — see below);
//   3. the newest entry in the sw.js CACHE comment names the CACHE constant.

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, existsSync, readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const STATIC = join(HERE, '..', 'static');
const JS_DIR = join(STATIC, 'js');

const sources = () => {
  const out = [['index.html', readFileSync(join(STATIC, 'index.html'), 'utf8')]];
  for (const f of readdirSync(JS_DIR).filter((f) => f.endsWith('.js'))) {
    out.push([`js/${f}`, readFileSync(join(JS_DIR, f), 'utf8')]);
  }
  return out;
};

// Matches both `/static/app.css?v=42` (index.html) and `./api.js?v=18` (module
// specifiers). Captures the bare filename and the version.
const VERSIONED = /(?:\/static\/|\.\/|\/)?((?:js\/)?[A-Za-z0-9_.-]+\.(?:js|css))\?v=(\d+)/g;

test('every ?v= URL points at a file that exists', () => {
  const missing = [];
  for (const [file, src] of sources()) {
    for (const m of src.matchAll(VERSIONED)) {
      const name = m[1].replace(/^js\//, '');
      const candidates = [join(STATIC, m[1]), join(JS_DIR, name), join(STATIC, name)];
      if (!candidates.some(existsSync)) missing.push(`${file}: ${m[0]}`);
    }
  }
  assert.deepEqual(missing, [], `\nThese ?v= URLs name a file that is not there:\n${missing.join('\n')}`);
});

// THE BUG THIS PINS DOWN: main.js imported './api.js?v=18' while reactions.js
// imported './api.js?v=16'. To the browser those are two different module URLs,
// so api.js was instantiated TWICE with two separate module scopes. api.js
// keeps `onLocked` in module scope and main.js registers it via setOnLocked(),
// so reactions.js's copy had a null handler: when a session expired, every
// reaction request 401'd and the app never dropped to Safe Mode. Nothing threw.
test('a module is imported at the same ?v= everywhere', () => {
  const seen = new Map();   // filename -> Map(version -> [call sites])
  for (const [file, src] of sources()) {
    for (const m of src.matchAll(VERSIONED)) {
      const name = m[1].replace(/^js\//, '');
      if (!seen.has(name)) seen.set(name, new Map());
      const byVer = seen.get(name);
      if (!byVer.has(m[2])) byVer.set(m[2], []);
      byVer.get(m[2]).push(file);
    }
  }
  const split = [];
  for (const [name, byVer] of seen) {
    if (byVer.size > 1) {
      const detail = [...byVer].map(([v, files]) => `  v=${v} in ${files.join(', ')}`).join('\n');
      split.push(`${name} is referenced at ${byVer.size} versions:\n${detail}`);
    }
  }
  assert.deepEqual(split, [],
    '\nSame file, different ?v= — the browser loads it TWICE, with separate\n'
    + 'module state. Pick one version and use it everywhere:\n\n' + split.join('\n\n'));
});

// The weaker half of the sw.js invariant, and the only half a static test can
// honestly enforce: nothing here can know whether the shipped bytes changed, so
// this does not (and cannot) assert "you bumped CACHE when you edited a file".
// What it CAN assert is that the two halves of the line agree — the ff051aa
// failure mode where the comment gained a new entry and the constant did not.
test('the newest sw.js CACHE comment entry names the CACHE constant', () => {
  const sw = readFileSync(join(STATIC, 'sw.js'), 'utf8');
  const line = sw.split('\n').find((l) => l.includes('const CACHE'));
  assert.ok(line, 'sw.js has no `const CACHE` line');
  const constant = /const CACHE = 'local-chat-(v\d+)'/.exec(line);
  assert.ok(constant, `could not read the CACHE constant from: ${line.slice(0, 80)}`);
  const firstEntry = /\/\/\s*(v\d+):/.exec(line);
  assert.ok(firstEntry, 'the CACHE comment must start with the newest entry, e.g. "// v61: …"');
  assert.equal(firstEntry[1], constant[1],
    `sw.js CACHE is ${constant[1]} but the newest comment entry is ${firstEntry[1]} — `
    + 'bump both, newest entry first.');
});

test('every file the service worker precaches actually exists', () => {
  const sw = readFileSync(join(STATIC, 'sw.js'), 'utf8');
  const block = /const SHELL = \[([\s\S]*?)\];/.exec(sw);
  assert.ok(block, 'sw.js has no SHELL array');
  const paths = [...block[1].matchAll(/'([^']+)'/g)].map((m) => m[1]);
  assert.ok(paths.length > 10, 'SHELL looks empty — check the parse');
  // '/' is the app document, served by the backend, not a file on disk.
  const missing = paths
    .filter((p) => p !== '/')
    .filter((p) => !existsSync(join(STATIC, p.replace(/^\/static\//, ''))));
  assert.deepEqual(missing, [],
    '\nSHELL precaches paths that do not exist. addAll() rejects ATOMICALLY, so\n'
    + 'ONE bad entry silently disables the whole offline cache:\n' + missing.join('\n'));
});
