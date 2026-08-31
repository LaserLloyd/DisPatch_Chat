// Unit tests for pinnable settings.
//
// Run: node --test frontend/tests/
//
// The branch worth pinning is the GATE: a pin must never survive into a tier
// that may not use it. Everything else in pins.js is DOM plumbing.
//
// pins.js imports nim.js and privacy.js, which touch `document`/`localStorage`
// only inside function bodies — so like nim.test.js this imports cleanly under
// plain node, provided we hand it a localStorage before the module reads one.

import test from 'node:test';
import assert from 'node:assert/strict';

// Minimal storage stand-in. Installed before the import so pinnedIds() has
// something to read; the real one is the browser's.
const store = new Map();
globalThis.localStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
  removeItem: (k) => store.delete(k),
};

const { PINNABLE, pinnedIds, setPinned, isPinned, visiblePins, pinnableById } =
  await import('../static/js/pins.js');

const reset = () => store.clear();

test('nothing is pinned by default', () => {
  reset();
  assert.deepEqual(pinnedIds(), []);
});

test('pinning is remembered, and unpinning removes it', () => {
  reset();
  setPinned('nim', true);
  assert.equal(isPinned('nim'), true);
  setPinned('nim', false);
  assert.equal(isPinned('nim'), false);
});

test('an unknown id cannot be pinned', () => {
  reset();
  setPinned('definitely-not-a-setting', true);
  assert.deepEqual(pinnedIds(), []);
});

test('an id left in storage by an older build is ignored, not drawn', () => {
  reset();
  // Hand-edited storage, or a setting removed in a later release.
  store.set('dispatch-pinned-settings', JSON.stringify(['nim', 'gone-away']));
  assert.deepEqual(pinnedIds(), ['nim']);
});

test('corrupt storage reads as nothing pinned rather than throwing', () => {
  reset();
  store.set('dispatch-pinned-settings', '{not json');
  assert.deepEqual(pinnedIds(), []);
  store.set('dispatch-pinned-settings', '"a string, not a list"');
  assert.deepEqual(pinnedIds(), []);
});

// --- the gate ------------------------------------------------------------

test('Safe Mode sees only pins marked safe', () => {
  reset();
  for (const entry of PINNABLE) setPinned(entry.id, true);
  const locked = visiblePins(true).map((e) => e.id);
  const unlocked = visiblePins(false).map((e) => e.id);
  // Every entry the locked tier is shown must have declared itself safe.
  for (const id of locked) assert.equal(pinnableById(id).safe, true);
  // An unsafe entry is OMITTED from Safe Mode, not merely disabled: a greyed
  // button still advertises a surface a locked device may never reach.
  for (const entry of PINNABLE) {
    if (!entry.safe) assert.equal(locked.includes(entry.id), false);
  }
  // Unlocked sees everything that is pinned.
  assert.equal(unlocked.length, PINNABLE.length);
});

test('every registry entry declares the fields the rail relies on', () => {
  for (const entry of PINNABLE) {
    assert.equal(typeof entry.id, 'string');
    assert.equal(typeof entry.glyph, 'string');
    assert.equal(typeof entry.enabled, 'function');
    assert.equal(typeof entry.toggle, 'function');
    assert.equal(typeof entry.safe, 'boolean');
    // titleEn is the fallback when no translator is passed; without it a
    // pinned button would render an empty aria-label.
    assert.ok(entry.titleEn && entry.titleKey);
  }
});

// --- the avatars pin under NIM -------------------------------------------
// NIM borrows data-avatar-style, so while it is on the Minimal-avatars pin
// must read as ON and be blocked, not operable — the same contract the
// Settings checkbox keeps (ticked + disabled + "No-Image Mode controls this").

test('minimal-avatars pin reports on-and-blocked while NIM holds the attribute', () => {
  reset();
  const av = pinnableById('avatars');
  assert.ok(av, 'avatars entry missing from the registry');
  assert.equal(av.enabled(), false);
  assert.equal(av.blocked(false), null);
  store.set('dispatch-nim', '1');
  assert.equal(av.enabled(), true, 'NIM forces minimal avatars');
  assert.equal(av.blocked(false), 'nim.controls_avatars');
});

test('minimal-avatars pin reflects the stored preference when NIM is off', () => {
  reset();
  const av = pinnableById('avatars');
  store.set('dispatch-avatar-style', 'minimal');
  assert.equal(av.enabled(), true);
  store.delete('dispatch-avatar-style');
  assert.equal(av.enabled(), false);
});
