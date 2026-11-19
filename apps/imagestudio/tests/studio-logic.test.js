// Image Studio — the pure rules (static/studio-logic.js).

import test from 'node:test';
import assert from 'node:assert/strict';
import {
  PROMPT_MAX, isGenerateShortcut, shortcutLabel, promptStatus, canGenerate, clampCount, parseSeed,
  buildRequest, cropRect, defaultCropPos, galleryItems, galleryInfo, isMetered,
  ratioLabel, ratioText, stepIndex, wheelShouldChain, canScroll, INNER_SCROLLPORTS,
  SORTS, normalizeSort, sortRows, clearFromHere, DELETE_SCOPES, normalizeScope, deleteConfirmKey, sendMetadata,
  DRAFT_MAX_BYTES, isProfileKey, draftKey, draftFromForm, serializeDraft, parseDraft, resolveDraft,
  inflightExtra, pollShouldContinue, INFLIGHT_POLL_CAP_MS,
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

import { activeModel, isH3Model, keepBrowsePicks, modelCaps, modelPromptMax, modelsNote, refreshSummary } from '../static/studio-logic.js';

const MM = {
  id: 'minimax', label: 'MiniMax', shape: 'minimax', model: 'image-01', prompt_max: 1500,
  supports: { reference: true, optimizer: true, seed: true, max_n: 9 },
  supports_by_model: {
    'image-01': { reference: true, optimizer: true, seed: true, max_n: 9 },
    'MiniMax-H3-Image': { reference: false, optimizer: true, seed: false, max_n: 10 },
  },
  prompt_max_by_model: { 'image-01': 1500, 'MiniMax-H3-Image': 4000 },
};

test('the model in use is the typed one, else the provider default', () => {
  assert.equal(activeModel(MM, ''), 'image-01');
  assert.equal(activeModel(MM, '  MiniMax-H3-Image '), 'MiniMax-H3-Image');
});

test('caps and prompt limit follow the selected model, falling back to the provider row', () => {
  assert.equal(modelCaps(MM, 'MiniMax-H3-Image').seed, false);
  assert.equal(modelCaps(MM, 'MiniMax-H3-Image').max_n, 10);
  assert.equal(modelCaps(MM, 'image-01').reference, true);
  assert.equal(modelCaps(MM, 'typed-unknown').max_n, 9);
  assert.equal(modelPromptMax(MM, 'MiniMax-H3-Image'), 4000);
  assert.equal(modelPromptMax(MM, 'image-01'), 1500);
  assert.equal(modelPromptMax(MM, 'unknown'), 1500);
  assert.equal(modelCaps(null, 'x').max_n, 1);
  // a typed H3 id the list has never seen still gets H3's caps, not the current model's
  const typed = modelCaps(MM, 'MiniMax-H3-Image-Next');
  assert.deepEqual([typed.reference, typed.seed, typed.max_n], [false, false, 10]);
  assert.equal(modelCaps({ ...MM, supports_by_model: {} }, 'MiniMax-H3-Image-Next').max_n, 10);
});

test('the note under the model field says where the list came from', () => {
  assert.deepEqual(modelsNote({ ...MM, models_source: 'documented', models_checked_at: '2026-11-11T05:00:00+00:00' }),
    { key: 'models.note_docs', vars: { label: 'MiniMax', date: '2026-11-11' } });
  assert.equal(modelsNote({ ...MM, models_source: 'endpoint', models_checked_at: null }).key, 'models.note_endpoint_nodate');
  assert.equal(modelsNote({ ...MM, models_source: '' }), null);
  // the static docs date wins over the time the key was last checked
  assert.deepEqual(modelsNote({ ...MM, models_source: 'documented', models_checked: '2026-11-11', models_checked_at: '2026-11-12T05:00:00+00:00' }).vars.date, '2026-11-11');
  // only MiniMax says "doesn't list its image models"; the rest get a neutral docs line
  const xai = { id: 'xai', label: 'xAI', models_source: 'documented', models_checked: '2026-11-11' };
  assert.deepEqual(modelsNote(xai), { key: 'models.note_docs_generic', vars: { label: 'xAI', date: '2026-11-11' } });
  assert.equal(modelsNote({ ...xai, models_checked: '' }), null);
  // documented ids merged into a live list
  assert.equal(modelsNote({ ...xai, models_source: 'mixed', models_checked_at: '2026-11-12T01:00:00+00:00' }).key, 'models.note_mixed');
});

test('an H3 model is recognised for the billing hint', () => {
  assert.equal(isH3Model(MM, 'MiniMax-H3-Image-Highspeed'), true);
  assert.equal(isH3Model(MM, 'image-01'), false);
  assert.equal(isH3Model({ id: 'openai', shape: 'openai-images' }, 'MiniMax-H3-Image'), false);
  assert.equal(isH3Model(null, 'MiniMax-H3-Image'), false);
});

test('a refresh is summed up in one plain sentence', () => {
  const provs = [{ id: 'xai', label: 'xAI Grok' }];
  assert.deepEqual(refreshSummary({ results: { xai: { source: 'endpoint', count: 4 } } }, provs),
    { key: 'models.refreshed', vars: { n: 4, name: 'xAI Grok' }, error: false });
  assert.equal(refreshSummary({ results: { xai: { source: 'documented', count: 2, error: 'HTTP 500' } } }, provs).error, true);
  assert.equal(refreshSummary({ results: { minimax: { source: 'documented', count: 4, error: null } } }, provs).key, 'models.refresh_docs_only');
  // singular, cached and failed results never read as "Updated: N models"
  assert.equal(refreshSummary({ results: { xai: { source: 'endpoint', count: 1 } } }, provs).key, 'models.refreshed_one');
  const cached = refreshSummary({ results: { xai: { source: 'endpoint', count: 4, cached: true }, minimax: { source: 'documented', count: 4, cached: true } } }, provs);
  assert.deepEqual([cached.key, cached.error], ['models.refresh_cached', false]);
  const bad = refreshSummary({ results: { xai: { source: 'documented', count: 2, error: 'The key was not accepted' }, minimax: { source: 'documented', count: 4, error: null } } }, provs);
  assert.deepEqual([bad.key, bad.error, bad.vars.error], ['models.refresh_failed', true, 'The key was not accepted']);
  // a MiniMax-only setup: no red message
  assert.equal(refreshSummary({ results: { minimax: { source: 'documented', count: 4, error: null } } }, provs).error, false);
});

test('a refresh keeps the checkpoints picked in Browse', () => {
  const old = [{ id: 'civitai', shape: 'civitai', models: ['urn:a', 'urn:picked'] }, { id: 'xai', shape: 'openai-images', models: ['gone'] }];
  const fresh = [{ id: 'civitai', shape: 'civitai', models: ['urn:a', 'urn:new'] }, { id: 'xai', shape: 'openai-images', models: ['grok'] }];
  const out = keepBrowsePicks(old, fresh);
  assert.deepEqual(out.find((p) => p.id === 'civitai').models, ['urn:a', 'urn:new', 'urn:picked']);
  assert.deepEqual(out.find((p) => p.id === 'xai').models, ['grok']);
});

test('the aspect ratios have plain names, and the ratio always rides beside the name', () => {
  assert.equal(ratioLabel('1:1'), 'ratio.square');
  assert.equal(ratioLabel('4:3'), 'ratio.landscape');
  assert.equal(ratioLabel('3:2'), 'ratio.landscape');
  assert.equal(ratioLabel('16:9'), 'ratio.wide');
  assert.equal(ratioLabel('21:9'), 'ratio.ultrawide');
  assert.equal(ratioLabel('3:4'), 'ratio.portrait');
  assert.equal(ratioLabel('2:3'), 'ratio.portrait');
  assert.equal(ratioLabel('9:16'), 'ratio.portrait_tall');
  // A ratio this table does not name has no name at all.
  assert.equal(ratioLabel('7:5'), '');
  assert.equal(ratioLabel(''), '');
  // Two ratios share a name: the ratio tells them apart.
  const t = (k) => ({ 'ratio.square': 'Square', 'ratio.landscape': 'Landscape', 'ratio.wide': 'Wide',
    'ratio.ultrawide': 'Ultrawide', 'ratio.portrait': 'Portrait', 'ratio.portrait_tall': 'Portrait tall' }[k]);
  assert.equal(ratioText('1:1', t), 'Square (1:1)');
  assert.equal(ratioText('3:2', t), 'Landscape (3:2)');
  assert.equal(ratioText('4:3', t), 'Landscape (4:3)');
  assert.equal(ratioText('21:9', t), 'Ultrawide (21:9)');
  assert.equal(ratioText('9:16', t), 'Portrait tall (9:16)');
  assert.equal(ratioText('7:5', t), '7:5', 'an unnamed ratio is shown as it is');
});

test('the viewer steps one picture at a time and the ends stop', () => {
  assert.equal(stepIndex(0, 1, 3), 1, 'next');
  assert.equal(stepIndex(2, 1, 3), 2, 'the end stops');
  assert.equal(stepIndex(1, -1, 3), 0, 'previous');
  assert.equal(stepIndex(0, -1, 3), 0, 'the start stops');
  assert.equal(stepIndex(0, 1, 1), 0, 'one picture stands still');
  assert.equal(stepIndex(0, 1, 0), 0, 'no picture at all');
  assert.equal(stepIndex(5, 1, 3), 2, 'an index outside the list is clamped');
});

test('the wheel over the controls column goes to the page only when the column cannot move', () => {
  const box = { scrollTop: 0, scrollHeight: 1000, clientHeight: 400 };
  // Not a scrollport at all: the page takes every wheel.
  assert.equal(wheelShouldChain(100, { scrollTop: 0, scrollHeight: 400, clientHeight: 400 }), true);
  assert.equal(wheelShouldChain(-100, { scrollTop: 0, scrollHeight: 200, clientHeight: 400 }), true);
  // The column can still move down: it keeps the wheel.
  assert.equal(wheelShouldChain(100, box), false);
  assert.equal(wheelShouldChain(100, { ...box, scrollTop: 500 }), false);
  // At the bottom, the wheel goes to the page.
  assert.equal(wheelShouldChain(100, { ...box, scrollTop: 600 }), true);
  // The column can still move up: it keeps the wheel.
  assert.equal(wheelShouldChain(-100, { ...box, scrollTop: 100 }), false);
  // At the top, the wheel goes to the page.
  assert.equal(wheelShouldChain(-100, { ...box, scrollTop: 0 }), true);
  // A wheel that goes nowhere changes nothing.
  assert.equal(wheelShouldChain(0, box), true);
});

test('a list inside the column keeps the wheel while it can still move that way', () => {
  const list = { scrollTop: 0, scrollHeight: 600, clientHeight: 300 };
  // The list has room below: the wheel scrolls the list, not the page.
  assert.equal(canScroll(100, list), true);
  assert.equal(canScroll(100, { ...list, scrollTop: 200 }), true);
  // At its end: nothing left to scroll that way.
  assert.equal(canScroll(100, { ...list, scrollTop: 300 }), false);
  // Room above: the wheel scrolls the list back.
  assert.equal(canScroll(-100, { ...list, scrollTop: 100 }), true);
  assert.equal(canScroll(-100, { ...list, scrollTop: 0 }), false);
  // A list that is not a scrollport cannot take the wheel.
  assert.equal(canScroll(100, { scrollTop: 0, scrollHeight: 300, clientHeight: 300 }), false);
  assert.equal(canScroll(-100, { scrollTop: 0, scrollHeight: 300, clientHeight: 300 }), false);
  // The two rules say the same thing the other way round.
  assert.equal(wheelShouldChain(100, list), !canScroll(100, list));
  assert.equal(wheelShouldChain(-100, { ...list, scrollTop: 300 }), !canScroll(-100, { ...list, scrollTop: 300 }));
});

test('the scroll areas inside the controls column are the ones the wheel must leave alone', () => {
  for (const c of ['.st-workflows', '.st-picker-models', '.st-pickgrid', '.st-browse-list', '.st-recents']) {
    assert.ok(INNER_SCROLLPORTS.includes(c), `${c} is a scroll area of its own`);
  }
});


// ---- the saved form draft ---------------------------------------------------

const PREFIX = 'dispatch-imagestudio-draft:';
const KA = 'a1b2c3d4e5f60718';
const KB = '0011223344556677';
const PROVS = [
  { id: 'minimax', configured: true, models: ['image-01', 'image-02'], shape: 'minimax' },
  { id: 'workflows', configured: true, models: [], shape: 'workflows' },
  { id: 'civitai', configured: true, models: ['urn:a'], shape: 'civitai' },
  { id: 'nokey', configured: false, models: [], shape: 'x' },
];
const CTX = { providers: PROVS, ratios: ['1:1', '16:9'], workflowNames: [] };
const FORM = { provider: 'minimax', model: 'image-02', prompt: 'a red fox', ratio: '16:9', n: '3', seed: '42', optimizer: true };

test('a draft survives save -> restore with the exact values', () => {
  const back = parseDraft(serializeDraft(FORM));
  assert.deepEqual(resolveDraft(back, CTX), {
    provider: 'minimax', model: 'image-02', prompt: 'a red fox', ratio: '16:9', seed: '42', n: 3, optimizer: true,
  });
});

test('an invalid provider/model is dropped silently and the prompt still restores', () => {
  const gone = parseDraft(serializeDraft({ ...FORM, provider: 'removed' }));
  assert.deepEqual(resolveDraft(gone, CTX), { prompt: 'a red fox', ratio: '16:9', seed: '42', n: 3, optimizer: true });
  const unkeyed = resolveDraft(parseDraft(serializeDraft({ ...FORM, provider: 'nokey' })), CTX);
  assert.equal(unkeyed.provider, undefined, 'a provider without a key is not usable');
  const badModel = resolveDraft(parseDraft(serializeDraft({ ...FORM, model: 'image-99' })), CTX);
  assert.equal(badModel.provider, 'minimax');
  assert.equal(badModel.model, undefined);
  assert.equal(badModel.prompt, 'a red fox');
  const badRatio = resolveDraft(parseDraft(serializeDraft({ ...FORM, ratio: '7:3', seed: 'x1' })), CTX);
  assert.equal(badRatio.ratio, undefined);
  assert.equal(badRatio.seed, undefined);
});

test('a workflow name never travels to another provider', () => {
  const ctx = { ...CTX, workflowNames: ['anima-Rev8'] };
  const d = parseDraft(serializeDraft({ ...FORM, provider: 'minimax', model: 'anima-Rev8' }));
  assert.equal(resolveDraft(d, ctx).model, undefined);
  const w = resolveDraft(parseDraft(serializeDraft({ ...FORM, provider: 'workflows', model: 'anima-Rev8' })), ctx);
  assert.equal(w.model, 'anima-Rev8');
  const w2 = resolveDraft(parseDraft(serializeDraft({ ...FORM, provider: 'workflows', model: 'gone' })), ctx);
  assert.equal(w2.model, undefined, 'a name the loaded gallery lacks is dropped');
  const early = resolveDraft(parseDraft(serializeDraft({ ...FORM, provider: 'workflows', model: 'anima-Rev8' })), CTX);
  assert.equal(early.model, 'anima-Rev8', 'gallery not loaded yet: the page checks it on arrival');
  assert.equal(resolveDraft(parseDraft(serializeDraft({ ...FORM, provider: 'civitai', model: 'urn:typed' })), CTX).model, 'urn:typed');
});

test('the stored draft is capped and the prompt gives way', () => {
  const big = serializeDraft({ ...FORM, prompt: 'x'.repeat(50000) }, 50000);
  assert.ok(big.length <= DRAFT_MAX_BYTES, `${big.length}`);
  assert.ok(parseDraft(big).prompt.length > 1000);
  assert.equal(parseDraft(serializeDraft({ ...FORM, prompt: 'y'.repeat(9000) })).prompt.length, PROMPT_MAX);
  assert.equal(parseDraft('x'.repeat(DRAFT_MAX_BYTES + 1)), null);
});

test('the key is per profile: B never reads what A stored', () => {
  const store = new Map();
  store.set(draftKey(PREFIX, KA), serializeDraft(FORM));
  assert.equal(draftKey(PREFIX, KA), `${PREFIX}${KA}`);
  assert.notEqual(draftKey(PREFIX, KA), draftKey(PREFIX, KB));
  assert.equal(parseDraft(store.get(draftKey(PREFIX, KB))), null, 'B has nothing');
  assert.equal(draftKey(PREFIX, ''), '', 'no key before /state answered: nothing is read or written');
  assert.equal(draftKey(PREFIX, undefined), '');
  assert.equal(draftKey(PREFIX, '../x'), '');
  assert.ok(isProfileKey('install') && isProfileKey(KA) && !isProfileKey('ABC'));
});

test('reset clears: nothing is left to restore', () => {
  const store = new Map([[draftKey(PREFIX, KA), serializeDraft(FORM)]]);
  store.delete(draftKey(PREFIX, KA));
  assert.deepEqual(resolveDraft(parseDraft(store.get(draftKey(PREFIX, KA)) || ''), CTX), {});
});

test('no image data or data: URL is ever serialised', () => {
  const dirty = { ...FORM, model: 'data:image/png;base64,AAAA', reference: { preview: 'data:image/png;base64,BBBB' }, image: 'data:x', preview: 'data:y' };
  const text = serializeDraft(dirty);
  assert.ok(!/data:/i.test(text), text);
  assert.deepEqual(Object.keys(JSON.parse(text)).sort(), ['model', 'n', 'optimizer', 'prompt', 'provider', 'ratio', 'seed', 'v']);
  assert.equal(draftFromForm({ prompt: 'data:text/plain,hi' }).prompt, '');
});

test('junk or another version restores nothing', () => {
  for (const bad of [null, '', 'nope', '[]', '{"v":99,"prompt":"x"}', '{"prompt":"x"}']) assert.equal(parseDraft(bad), null, String(bad));
});

test('pictures still being made: only what this page did not start, polled until done', () => {
  assert.equal(inflightExtra({ in_flight: 3 }, 1), 2);
  assert.equal(inflightExtra({ in_flight: 1 }, 2), 0);
  assert.equal(inflightExtra(null, 0), 0);
  assert.equal(pollShouldContinue({ extra: 2, elapsedMs: 1000, hidden: false }), true);
  assert.equal(pollShouldContinue({ extra: 0, elapsedMs: 1000, hidden: false }), false);
  assert.equal(pollShouldContinue({ extra: 2, elapsedMs: 1000, hidden: true }), false);
  assert.equal(pollShouldContinue({ extra: 2, elapsedMs: INFLIGHT_POLL_CAP_MS, hidden: false }), false);
});

test('last generation: record -> draft, differs, preview', async () => {
  const { lastToDraft, lastDiffers, lastPreview } = await import('../static/studio-logic.js');
  const last = { v: 1, at: 1, provider: 'workflows', model: 'wf-b', prompt: 'a  lighthouse\nat dusk', ratio: '9:16', n: 3, seed: null, optimizer: false };
  const d = lastToDraft(last);
  assert.deepEqual([d.provider, d.model, d.prompt, d.ratio, d.n, d.seed, d.optimizer],
    ['workflows', 'wf-b', 'a  lighthouse\nat dusk', '9:16', 3, '', false]);
  assert.equal(lastToDraft(null), null);
  assert.equal(lastToDraft({ prompt: '' }), null);
  assert.equal(lastToDraft({ prompt: 'data:image/png;base64,AAAA' }).prompt, '', 'never a data: URL');
  const same = { provider: 'workflows', model: 'wf-b', prompt: 'a  lighthouse\nat dusk', ratio: '9:16', n: '3', seed: '', optimizer: false };
  assert.equal(lastDiffers(last, same), false);
  assert.equal(lastDiffers(last, { ...same, model: 'wf-a' }), true);
  assert.equal(lastDiffers(last, { ...same, prompt: '' }), true);
  assert.equal(lastDiffers(null, same), false);
  assert.equal(lastPreview(last), 'a lighthouse at dusk');
  assert.equal(lastPreview({ prompt: 'x'.repeat(200) }).length, 90);
});

// ---- the daily spend notice -----------------------------------------------------
import { spendNotice, spendAmount } from '../static/studio-logic.js';

test('spendNotice: the profile counter first, then the household; only for the provider\'s own unit and from 80%', () => {
  const near = { unit: 'usd', cap: 5, used: 4.2, share: 0.84, near: true };
  const calm = { unit: 'usd', cap: 5, used: 1, share: 0.2, near: false };
  assert.deepEqual(spendNotice({ lines: [near], install: [calm] }, 'minimax'), { who: 'profile', unit: 'usd', pct: 84, used: 4.2, cap: 5 });
  assert.equal(spendNotice({ lines: [calm], install: [near] }, 'minimax').who, 'install');
  assert.equal(spendNotice({ lines: [calm], install: [calm] }, 'minimax'), null);
  assert.equal(spendNotice({ lines: [near], install: [] }, 'openai'), null);
  assert.equal(spendNotice({ lines: [near], install: [] }, 'workflows'), null, 'the image server has no counter');
  assert.equal(spendNotice(null, 'minimax'), null);
  assert.equal(spendNotice({ lines: [{ ...near, cap: 0 }], install: [] }, 'minimax'), null, 'no limit, no notice');
});

test('spendAmount: dollars, Buzz and pictures', () => {
  const t = (k, p) => (k === 'spend.amount_buzz' ? `${p.n} Buzz` : `${p.n} pictures`);
  assert.equal(spendAmount('usd', 0.035, t), '$0.04');
  assert.equal(spendAmount('buzz', 1200, t), '1,200 Buzz');
  assert.equal(spendAmount('pictures:openai', 30, t), '30 pictures');
});
