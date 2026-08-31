// Unit tests for custom link buttons.
//
// Run: node --test frontend/tests/
//
// The branches worth testing are the ones that keep the feature inside "opens
// a web page": URL scheme validation (on write AND on every read, so a
// hand-edited storage row cannot smuggle javascript: onto the rail) and the
// junk-tolerant read. The rail rendering itself is DOM plumbing.
//
// links.js imports util.js, which touches `document` only inside function
// bodies — so like pins.test.js this imports cleanly under plain node,
// provided we hand it a localStorage before the module reads one.

import test from 'node:test';
import assert from 'node:assert/strict';

const store = new Map();
globalThis.localStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
  removeItem: (k) => store.delete(k),
};

const { customLinks, addLink, removeLink, validUrl } =
  await import('../static/js/links.js');

const KEY = 'dispatch-custom-links';
const reset = () => store.clear();

test('no links by default', () => {
  reset();
  assert.deepEqual(customLinks(), []);
});

test('add stores; remove forgets; unknown remove is a no-op', () => {
  reset();
  const e = addLink({ glyph: '🎛', label: 'Panel', url: 'http://example.com:8080' });
  assert.ok(e);
  assert.equal(customLinks().length, 1);
  assert.equal(customLinks()[0].label, 'Panel');
  removeLink('not-a-real-id');
  assert.equal(customLinks().length, 1);
  removeLink(e.id);
  assert.deepEqual(customLinks(), []);
});

test('only http(s) URLs are accepted on write', () => {
  reset();
  for (const url of ['javascript:alert(1)', 'data:text/html,x', 'file:///etc/passwd',
                     'ftp://x', 'not a url', '', 'example.com']) {
    assert.equal(addLink({ label: 'x', url }), null, url);
  }
  assert.deepEqual(customLinks(), []);
  assert.ok(addLink({ label: 'x', url: 'https://example.com/panel' }));
});

test('a label is required; a glyph is not (empty means the line icon)', () => {
  reset();
  assert.equal(addLink({ label: '   ', url: 'https://example.com' }), null);
  const e = addLink({ label: 'Panel', url: 'https://example.com' });
  assert.equal(e.glyph, '');
});

test('a hand-edited storage row cannot smuggle a bad scheme onto the rail', () => {
  reset();
  store.set(KEY, JSON.stringify([
    { id: 'a', glyph: '💣', label: 'evil', url: 'javascript:alert(1)' },
    { id: 'b', glyph: '✅', label: 'fine', url: 'https://example.com' },
    { id: 'c', label: 'no url' },
    'not even an object',
  ]));
  const links = customLinks();
  assert.equal(links.length, 1);
  assert.equal(links[0].id, 'b');
});

test('junk storage reads as no links, not a throw', () => {
  reset();
  store.set(KEY, '{not json');
  assert.deepEqual(customLinks(), []);
  store.set(KEY, '"a string, not a list"');
  assert.deepEqual(customLinks(), []);
});

test('validUrl matches the write-path rule', () => {
  assert.equal(validUrl('https://example.com'), true);
  assert.equal(validUrl('http://192.0.2.1:8080/x'), true);
  assert.equal(validUrl('javascript:alert(1)'), false);
  assert.equal(validUrl(null), false);
});
