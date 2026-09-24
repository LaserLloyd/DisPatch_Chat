// Every ES module under static/js/ must be in the service worker's SHELL.
//
// main.js imports them all statically, so an offline cold start that lacks
// even one of them gets the worker's 503 for that import and main.js never
// runs — the boot veil stays up on the very device offline reading exists
// for. store.js was exactly that file on 2026-09-24: named in a CACHE comment,
// absent from the list. assets.test.js checks that SHELL paths exist; this is
// the other direction.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const STATIC = join(dirname(fileURLToPath(import.meta.url)), '..', 'static');

test('every static/js/*.js module is precached by sw.js', () => {
  const sw = readFileSync(join(STATIC, 'sw.js'), 'utf8');
  const shell = new Set([...sw.matchAll(/'(\/static\/js\/[^']+\.js)'/g)].map((m) => m[1]));
  const missing = readdirSync(join(STATIC, 'js'))
    .filter((f) => f.endsWith('.js'))
    .map((f) => `/static/js/${f}`)
    .filter((p) => !shell.has(p));
  assert.deepEqual(missing, [], `not in sw.js SHELL: ${missing.join(', ')}`);
});
