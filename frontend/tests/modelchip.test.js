// Unit tests for the per-thread model/thinking override chip's pure math.
//
// Run: node --test frontend/tests/
//
// modelchip.js touches no DOM and no i18n, so it imports cleanly under plain
// node — everything worth pinning here is the shaping/parsing logic; the
// DOM wiring itself is pinned textually in modelchip-wiring.test.js, the
// same pattern harness-wiring.test.js uses for main.js.

import test from 'node:test';
import assert from 'node:assert/strict';

import {
  THINKING_LEVELS, normalizeModelOptions, contextMeterValues, compactTokens,
  meterText, prefsPatchFrom, latestContextBudget,
} from '../static/js/modelchip.js';

test('THINKING_LEVELS is a plain, non-empty list of level strings', () => {
  assert.ok(Array.isArray(THINKING_LEVELS) && THINKING_LEVELS.length > 0);
  assert.ok(THINKING_LEVELS.every((v) => typeof v === 'string' && v));
});

test('normalizeModelOptions passes through well-formed entries', () => {
  const out = normalizeModelOptions([
    { id: 'deepseek/deepseek-v4-pro', label: 'DeepSeek v4 Pro' },
    { id: 'deepseek/deepseek-v4-flash' },
  ]);
  assert.deepEqual(out, [
    { id: 'deepseek/deepseek-v4-pro', label: 'DeepSeek v4 Pro' },
    { id: 'deepseek/deepseek-v4-flash', label: 'deepseek/deepseek-v4-flash' },
  ]);
});

test('normalizeModelOptions drops id-less/blank entries and de-dupes', () => {
  const out = normalizeModelOptions([
    { id: 'm-1', label: 'One' },
    { id: 'm-1', label: 'Duplicate' },
    { id: '', label: 'Blank id' },
    { label: 'No id at all' },
    null,
    'not-an-object',
  ]);
  assert.deepEqual(out, [{ id: 'm-1', label: 'One' }]);
});

test('normalizeModelOptions tolerates a non-array (network hiccup shape)', () => {
  assert.deepEqual(normalizeModelOptions(null), []);
  assert.deepEqual(normalizeModelOptions(undefined), []);
  assert.deepEqual(normalizeModelOptions({}), []);
});

test('contextMeterValues reads the documented gateway field names', () => {
  // These two names are exactly what backend/app/openclaw.py's
  // test_parse_reply_keeps_context_budget_and_split_usage pins on the
  // backend side — the two halves of this contract must agree.
  assert.deepEqual(
    contextMeterValues({ estimatedPromptTokens: 800, contextTokenBudget: 262144 }),
    { used: 800, window: 262144 });
});

test('contextMeterValues falls back through candidate spellings', () => {
  assert.deepEqual(contextMeterValues({ usedTokens: 10, maxTokens: 100 }),
                   { used: 10, window: 100 });
  assert.deepEqual(contextMeterValues({ used: 5, window: 50 }), { used: 5, window: 50 });
});

test('contextMeterValues returns a null window rather than guessing one', () => {
  assert.deepEqual(contextMeterValues({ estimatedPromptTokens: 800 }),
                   { used: 800, window: null });
});

test('contextMeterValues is null for junk input', () => {
  assert.equal(contextMeterValues(null), null);
  assert.equal(contextMeterValues('nope'), null);
  assert.equal(contextMeterValues({}), null);
  assert.equal(contextMeterValues({ estimatedPromptTokens: 'not a number' }), null);
});

test('compactTokens formats thousands and millions', () => {
  assert.equal(compactTokens(0), '0');
  assert.equal(compactTokens(900), '900');
  assert.equal(compactTokens(12345), '12.3k');
  assert.equal(compactTokens(1000), '1k');
  assert.equal(compactTokens(1234567), '1.2M');
});

test('compactTokens is blank for non-numeric input', () => {
  assert.equal(compactTokens(null), '');
  assert.equal(compactTokens(NaN), '');
  assert.equal(compactTokens('12'), '');
});

test('meterText renders used/window, or just used with no window', () => {
  assert.equal(meterText({ estimatedPromptTokens: 15000, contextTokenBudget: 262144 }), '15k/262.1k');
  assert.equal(meterText({ estimatedPromptTokens: 500 }), '500');
  assert.equal(meterText(null), '');
});

test('prefsPatchFrom maps an empty selection to null (back to default)', () => {
  assert.deepEqual(prefsPatchFrom('', ''), { model: null, thinking: null });
  assert.deepEqual(prefsPatchFrom('m-1', ''), { model: 'm-1', thinking: null });
  assert.deepEqual(prefsPatchFrom('', 'high'), { model: null, thinking: 'high' });
  assert.deepEqual(prefsPatchFrom('m-1', 'high'), { model: 'm-1', thinking: 'high' });
});

test('latestContextBudget finds the newest carrying message, not the first', () => {
  const messages = [
    { metadata: { context_budget: { estimatedPromptTokens: 100 } } },
    { metadata: null },
    { metadata: { model: 'm' } },
    { metadata: { context_budget: { estimatedPromptTokens: 900 } } },
  ];
  assert.deepEqual(latestContextBudget(messages), { estimatedPromptTokens: 900 });
});

test('latestContextBudget is null when nothing carries one', () => {
  assert.equal(latestContextBudget([{ metadata: { model: 'm' } }]), null);
  assert.equal(latestContextBudget([]), null);
  assert.equal(latestContextBudget(null), null);
});
