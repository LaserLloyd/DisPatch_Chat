// Image Studio — the pure rules (static/studio-logic.js).

import test from 'node:test';
import assert from 'node:assert/strict';
import {
  PROMPT_MAX, isGenerateShortcut, shortcutLabel, promptStatus, canGenerate, clampCount, parseSeed,
  buildRequest, cropRect, defaultCropPos, galleryItems, galleryInfo, isMetered,
  SORTS, normalizeSort, sortRows, clearFromHere, DELETE_SCOPES, normalizeScope, deleteConfirmKey, sendMetadata,
} from '../static/studio-logic.js';

const key = (over = {}) => ({ key: 'Enter', ctrlKey: false, metaKey: false, altKey: false, shiftKey: false, isComposing: false, ...over });

test('Shift+Enter, Ctrl+Enter and Cmd+Enter generate; plain, Alt and IME Enter do not', () => {
  assert.equal(isGenerateShortcut(key({ ctrlKey: true })), true);
  assert.equal(isGenerateShortcut(key({ metaKey: true })), true);
  assert.equal(isGenerateShortcut(key()), false, 'plain Enter is a newline');
  assert.equal(isGenerateShortcut(key({ shiftKey: true })), true);
  assert.equal(isGenerateShortcut(key({ shiftKey: true, isComposing: true })), false);
  assert.equal(isGenerateShortcut(key({ shiftKey: true, altKey: true })), false);
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
  assert.equal(normalizeSort(null), 'newest', 'no saved preference = newest first');
  assert.equal(normalizeSort(undefined), 'newest');
  assert.equal(normalizeSort('junk'), 'newest');
  assert.equal(normalizeSort('oldest'), 'oldest', 'a saved preference still wins');
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

test('delete scopes: all, hour, day; junk falls back to the narrowest sensible one', () => {
  assert.deepEqual(DELETE_SCOPES, ['hour', 'day', 'all']);
  assert.equal(normalizeScope('day'), 'day');
  assert.equal(normalizeScope('week'), 'hour');
  assert.equal(normalizeScope(null), 'hour');
});

test('the delete confirmation key follows the scope and singular/plural', () => {
  assert.equal(deleteConfirmKey('hour', 14), 'delete.confirm_hour');
  assert.equal(deleteConfirmKey('hour', 1), 'delete.confirm_hour_one');
  assert.equal(deleteConfirmKey('day', 2), 'delete.confirm_day');
  assert.equal(deleteConfirmKey('all', 1), 'delete.confirm_all_one');
  assert.equal(deleteConfirmKey('bogus', 3), 'delete.confirm_hour');
});

test('Details say when the provider optimizer may have rewritten the prompt', () => {
  const base = { id: 'a', url: '/media/a.png', prompt: 'typed', provider: 'minimax', model: 'image-01' };
  assert.equal(galleryInfo(base).model, 'minimax · image-01');
  assert.equal(galleryInfo({ ...base, optimizer: true }).model, 'minimax · image-01 · optimizer on');
  assert.equal(galleryInfo({ ...base, optimizer: true }).prompt, 'typed', 'the typed prompt is what is shown and copied');
});

test('sendMetadata carries the prompt, model, seed and the optimizer flag into the chat record', () => {
  const r = { id: 'i1', prompt: 'a fox', provider: 'minimax', model: 'image-01', seed: 7, optimizer: true };
  assert.deepEqual(sendMetadata(r), { source: 'image-studio', image_id: 'i1', prompt: 'a fox', model: 'minimax · image-01', seed: 7, optimizer: true });
  assert.ok(!('optimizer' in sendMetadata({ ...r, optimizer: false })));
});

test('modelLabel: the provider\'s friendly name for a known model id, else the id', async () => {
  const { modelLabel } = await import('../static/studio-logic.js');
  const providers = [{ id: 'civitai', model_names: { 'urn:air:sdxl:checkpoint:civitai:1@2': 'Juggernaut XL' } }, { id: 'minimax' }];
  assert.equal(modelLabel({ provider: 'civitai', model: 'urn:air:sdxl:checkpoint:civitai:1@2' }, providers), 'Juggernaut XL');
  assert.equal(modelLabel({ provider: 'civitai', model: 'urn:air:sdxl:checkpoint:civitai:9@9' }, providers), 'urn:air:sdxl:checkpoint:civitai:9@9');
  assert.equal(modelLabel({ provider: 'minimax', model: 'image-01' }, providers), 'image-01');
  assert.equal(modelLabel({ provider: 'minimax' }, undefined), '');
});

test('estimateKey: blank without a prompt or provider, stable for the same form, new when anything changes', async () => {
  const { estimateKey } = await import('../static/studio-logic.js');
  const f = { prompt: ' a fox ', provider: 'civitai', model: 'm', ratio: '1:1', n: 2 };
  assert.equal(estimateKey({ ...f, prompt: '  ' }), '');
  assert.equal(estimateKey({ ...f, provider: '' }), '');
  assert.equal(estimateKey(f), estimateKey({ ...f, prompt: 'a fox' }));
  for (const ch of [{ n: 3 }, { ratio: '16:9' }, { model: 'x' }, { prompt: 'b' }, { currency: 'yellow' }]) assert.notEqual(estimateKey({ ...f, ...ch }), estimateKey(f));
});

test('currency: only blue or yellow reach the request, and it is part of the estimate key', async () => {
  const { buildRequest, normalizeCurrency, estimateKey } = await import('../static/studio-logic.js');
  const f = { prompt: 'a fox', provider: 'civitai', model: '', ratio: '1:1', n: 1 };
  assert.equal(normalizeCurrency('blue'), 'blue');
  assert.equal(normalizeCurrency('yellow'), 'yellow');
  for (const bad of ['', 'green', 'Blue', null, undefined, 3]) assert.equal(normalizeCurrency(bad), '');
  assert.equal('currency' in buildRequest(f, { max_n: 4 }), false);
  assert.equal('currency' in buildRequest({ ...f, currency: 'green' }, { max_n: 4 }), false);
  assert.equal(buildRequest({ ...f, currency: 'yellow' }, { max_n: 4 }).currency, 'yellow');
  assert.notEqual(estimateKey({ ...f, currency: 'blue' }), estimateKey({ ...f, currency: 'yellow' }));
  assert.equal(estimateKey({ ...f, currency: 'nope' }), estimateKey(f));
});

test('balance line: yellow first, then blue, thousands separators, junk dropped', async () => {
  const { balanceLine, balanceParts, formatCount } = await import('../static/studio-logic.js');
  assert.equal(formatCount(1234, 'en'), '1,234');
  assert.equal(formatCount('x'), '0');
  assert.deepEqual(balanceParts({ blue: 56, yellow: 1234, green: 7 }).map((p) => p.type), ['yellow', 'blue', 'green']);
  assert.equal(balanceLine({ blue: 56, yellow: 1234 }, (x) => x, 'en'), '1,234 yellow · 56 blue');
  assert.equal(balanceLine({ blue: 56.9 }, (x) => x.toUpperCase(), 'en'), '56 BLUE');
  assert.equal(balanceLine({ yellow: 'x', Bad: 1, 'a b': 2, nan: NaN, nul: null }, (x) => x, 'en'), '');
  assert.equal(balanceLine(null), '');
  assert.equal(balanceLine([1, 2]), '');
});

test('browse rows: compact counts, one-line labels, malformed rows dropped', async () => {
  const { compactCount, browseLabel, cleanBrowseRows, normalizeBrowseSort, rememberModelName, BROWSE_SORTS } = await import('../static/studio-logic.js');
  assert.deepEqual([950, 1500, 12000, 286000, 1250000, 12000000, -4, 'x'].map(compactCount), ['950', '1.5k', '12k', '286k', '1.3M', '12M', '0', '0']);
  assert.equal(browseLabel({ name: 'Cool XL', base: 'Illustrious' }, '286k downloads'), 'Cool XL · Illustrious · 286k downloads');
  assert.equal(browseLabel({ name: 'Cool XL', base: '' }, '5 downloads'), 'Cool XL · 5 downloads');
  const good = { air: 'urn:air:sdxl:checkpoint:civitai:1@2', name: 'A' };
  assert.deepEqual(cleanBrowseRows([good, null, { air: 'urn:air:sdxl:lora:civitai:1@2', name: 'L' }, { air: good.air, name: '' }, 'x', { name: 'n' }]), [good]);
  assert.deepEqual(cleanBrowseRows(null), []);
  assert.deepEqual(BROWSE_SORTS, ['week', 'month', 'rated', 'new']);
  assert.equal(normalizeBrowseSort('rated'), 'rated');
  assert.equal(normalizeBrowseSort('zzz'), 'week');
  assert.deepEqual(rememberModelName({ a: 'A' }, good), { a: 'A', [good.air]: 'A' });
});

test('Browse filter: words are folded, lower-cased, deduplicated and capped; a row needs every word in its name', async () => {
  const { browseWords, browseMatches, BROWSE_QUERY_MAX } = await import('../static/studio-logic.js');
  assert.deepEqual(browseWords('  Mistoon   ANIME mistoon '), ['mistoon', 'anime']);
  assert.deepEqual(browseWords(''), []);
  assert.deepEqual(browseWords(null), []);
  assert.equal(browseWords('x'.repeat(100))[0].length, BROWSE_QUERY_MAX);
  const row = { name: 'Mistoon_Anime XL' };
  assert.equal(browseMatches(row, []), true);
  assert.equal(browseMatches(row, browseWords('anime mistoon')), true);
  assert.equal(browseMatches(row, browseWords('anime pony')), false);
  assert.equal(browseMatches({}, ['a']), false);
});
