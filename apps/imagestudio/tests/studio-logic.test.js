// Image Studio — the pure rules (static/studio-logic.js).

import test from 'node:test';
import assert from 'node:assert/strict';
import {
  PROMPT_MAX, isGenerateShortcut, shortcutLabel, promptStatus, canGenerate, clampCount, parseSeed,
  buildRequest, cropRect, defaultCropPos, galleryItems, isMetered,
  SORTS, normalizeSort, sortRows, clearFromHere,
} from '../static/studio-logic.js';

const key = (over = {}) => ({ key: 'Enter', ctrlKey: false, metaKey: false, altKey: false, shiftKey: false, isComposing: false, ...over });

test('Ctrl+Enter and Cmd+Enter generate; plain, Shift, Alt and IME Enter do not', () => {
  assert.equal(isGenerateShortcut(key({ ctrlKey: true })), true);
  assert.equal(isGenerateShortcut(key({ metaKey: true })), true);
  assert.equal(isGenerateShortcut(key()), false, 'plain Enter is a newline');
  assert.equal(isGenerateShortcut(key({ shiftKey: true })), false);
  assert.equal(isGenerateShortcut(key({ ctrlKey: true, altKey: true })), false);
  assert.equal(isGenerateShortcut(key({ ctrlKey: true, isComposing: true })), false, 'an IME candidate confirm');
  assert.equal(isGenerateShortcut(key({ key: 'a', ctrlKey: true })), false);
  assert.equal(isGenerateShortcut(null), false);
});

test('the tooltip names the right shortcut for the platform', () => {
  assert.equal(shortcutLabel('MacIntel'), '⌘+Enter');
  assert.equal(shortcutLabel('iPhone'), '⌘+Enter');
  assert.equal(shortcutLabel('Linux x86_64'), 'Ctrl+Enter');
  assert.equal(shortcutLabel(''), 'Ctrl+Enter');
});

test('the counter counts characters (not UTF-16 units), flags over-limit and blank', () => {
  assert.deepEqual(promptStatus('abc'), { n: 3, max: PROMPT_MAX, over: false, empty: false });
  assert.equal(promptStatus('\u{1F600}'.repeat(3)).n, 3);
  assert.equal(promptStatus('x'.repeat(1500)).over, false);
  assert.equal(promptStatus('x'.repeat(1501)).over, true);
  assert.equal(promptStatus('  \n ').empty, true);
});

test('Generate needs a prompt in range, a configured provider and no run in flight', () => {
  const ok = { prompt: 'a cat', busy: false, provider: 'minimax' };
  assert.equal(canGenerate(ok), true);
  assert.equal(canGenerate({ ...ok, prompt: '   ' }), false);
  assert.equal(canGenerate({ ...ok, prompt: 'x'.repeat(1501) }), false);
  assert.equal(canGenerate({ ...ok, busy: true }), false);
  assert.equal(canGenerate({ ...ok, provider: '' }), false);
});

test('the image count is 1..max whatever is typed', () => {
  assert.equal(clampCount('12', 9), 9);
  assert.equal(clampCount('0', 9), 1);
  assert.equal(clampCount('abc', 9), 1);
  assert.equal(clampCount('3', 1), 1);
  assert.equal(clampCount(4.9, 9), 4);
});

test('a seed is blank (none), a whole number, or rejected', () => {
  assert.equal(parseSeed(''), null);
  assert.equal(parseSeed(' 42 '), 42);
  assert.ok(Number.isNaN(parseSeed('-1')));
  assert.ok(Number.isNaN(parseSeed('1.5')));
  assert.ok(Number.isNaN(parseSeed('99999999999')));
});

test('the request carries only what the provider supports', () => {
  const form = { prompt: ' hi ', provider: 'minimax', model: '', ratio: '16:9', n: '4', optimizer: true, seed: '7', reference: { kind: 'result', id: 'a'.repeat(32) } };
  const full = buildRequest(form, { max_n: 9, optimizer: true, seed: true, reference: true });
  assert.deepEqual(full, { prompt: 'hi', provider: 'minimax', model: '', ratio: '16:9', n: 4, optimizer: true, seed: 7, reference: form.reference });
  const plain = buildRequest(form, { max_n: 2, optimizer: false, seed: false, reference: false });
  assert.deepEqual(plain, { prompt: 'hi', provider: 'minimax', model: '', ratio: '16:9', n: 2 });
  assert.equal('seed' in buildRequest({ ...form, seed: '' }, { max_n: 9, seed: true }), false);
  assert.equal('seed' in buildRequest({ ...form, seed: 'x' }, { max_n: 9, seed: true }), false, 'junk seed is dropped, not sent');
});

test('the avatar crop is a square inside the picture, slid along the long side', () => {
  assert.deepEqual(cropRect(1000, 500, 0.5), { sx: 250, sy: 0, size: 500 });
  assert.deepEqual(cropRect(500, 1000, 0.25), { sx: 0, sy: 125, size: 500 });
  assert.deepEqual(cropRect(500, 1000, 9), { sx: 0, sy: 500, size: 500 }, 'clamped');
  assert.deepEqual(cropRect(800, 800, 0.9), { sx: 0, sy: 0, size: 800 });
  assert.equal(defaultCropPos(512, 1024), 0.25);
  assert.equal(defaultCropPos(1024, 512), 0.5);
});

test('gallery items for the shell lightbox keep the order they were given (oldest -> newest)', () => {
  const items = galleryItems([
    { id: 'a'.repeat(32), url: '/media/a.png', prompt: 'one' },
    { id: 'b'.repeat(32), url: '/media/b.jpg', prompt: 'two' },
  ]);
  assert.deepEqual(items.map((i) => i.src), ['/media/a.png', '/media/b.jpg']);
  assert.equal(items[1].caption, 'two');
  assert.match(items[1].name, /\.jpg$/);
  assert.equal(items[0].downloadUrl, '/media/a.png');
});

test('only a provider with a price note is called metered', () => {
  assert.equal(isMetered({ price_note: 'Billed per image.' }), true);
  assert.equal(isMetered({ price_note: '' }), false);
  assert.equal(isMetered(null), false);
});

test('the grid sorts oldest first by default or newest first, without touching the input', () => {
  const rows = [{ id: 'a' }, { id: 'b' }, { id: 'c' }];
  assert.deepEqual(SORTS, ['oldest', 'newest']);
  assert.equal(normalizeSort(null), 'oldest');
  assert.equal(normalizeSort('junk'), 'oldest');
  assert.equal(normalizeSort('newest'), 'newest');
  assert.deepEqual(sortRows(rows, 'oldest').map((r) => r.id), ['a', 'b', 'c']);
  assert.deepEqual(sortRows(rows, 'newest').map((r) => r.id), ['c', 'b', 'a']);
  assert.deepEqual(rows.map((r) => r.id), ['a', 'b', 'c'], 'the input is not reordered in place');
  assert.deepEqual(sortRows(null, 'newest'), []);
});

test('"clear from here down" is the picture and everything after it in the shown order', () => {
  const rows = [{ id: 'a' }, { id: 'b' }, { id: 'c' }, { id: 'd' }];
  assert.deepEqual(clearFromHere(rows, 'b', 'oldest'), ['b', 'c', 'd']);
  assert.deepEqual(clearFromHere(rows, 'b', 'newest'), ['b', 'a']);
  assert.deepEqual(clearFromHere(rows, 'd', 'newest'), ['d', 'c', 'b', 'a']);
  assert.deepEqual(clearFromHere(rows, 'zz', 'oldest'), []);
});
