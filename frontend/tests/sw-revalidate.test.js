// 4.2.0: a warm phone kept an old en.json (no ?v=, no Cache-Control), so new
// labels read as humanized keys ("Pill agent", "F all"). The worker must fetch
// every unversioned shell file with cache: 'no-cache'.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const SW = readFileSync(join(dirname(fileURLToPath(import.meta.url)), '..', 'static', 'sw.js'), 'utf8');

test('sw.js revalidates unversioned files instead of trusting the HTTP cache', () => {
  assert.match(SW, /!url\.search && e\.request\.mode !== 'navigate'/);
  assert.match(SW, /new Request\(e\.request, \{ cache: 'no-cache' \}\)/);
  assert.match(SW, /fetch\(req\)/);
});
