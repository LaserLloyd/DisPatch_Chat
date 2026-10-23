// main.js wiring that 4.0 changed: role words, the No-Image Mode ratchet on the rail, and the
// first-password form. Source checks (main.js boots a whole app, so these pin the call sites).
//
// Run: node --test frontend/tests/

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const MAIN = readFileSync(join(HERE, '..', 'static', 'js', 'main.js'), 'utf8');

test('4.0 sends no "owner" role: main.js never compares with it or falls back to it', () => {
  assert.doesNotMatch(MAIN, /state\.role === 'owner'/);
  assert.doesNotMatch(MAIN, /state\.role \|\| 'owner'/);
});

test('a pinned No-Image Mode button keeps the same ratchet as the Settings row', () => {
  const at = MAIN.indexOf("renderPinnedRail(document.getElementById('gear-row')");
  assert.ok(at > 0);
  assert.match(MAIN.slice(at, at + 400), /decoy: nimLocked\(\)/);
});

test('the first password asks for the PIN this Admin signs in with (install PIN for the main Admin, else its own)', () => {
  assert.match(MAIN, /needsPin: accountChoice === 'pw_set' && \(ownerAccountView\(\) \? a\.pinSet : selfHasPin\(\)\)/);
  const fn = MAIN.slice(MAIN.indexOf('function selfHasPin()'), MAIN.indexOf('function selfHasPin()') + 300);
  assert.match(fn, /\/pin\/\.test\(String\(me\.credential/);
});
