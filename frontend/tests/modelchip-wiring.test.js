// Regression pin: the model/thinking chip must actually be wired up.
//
// Same style as harness-wiring.test.js — a textual pin, not a full jsdom
// boot of main.js (which drags in the entire import graph and is fragile;
// see that file's own note on why). The pure math behind the chip is
// covered for real in modelchip.test.js; this file only pins that main.js
// calls the wiring function and that the wiring function does what its
// name says.

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const STATIC = join(HERE, '..', 'static');
const mainJsPath = join(STATIC, 'js', 'main.js');

function readMain() {
  return readFileSync(mainJsPath, 'utf8');
}

test('main.js defines and calls wireModelChip()', () => {
  const src = readMain();
  assert.match(src, /function\s+wireModelChip\s*\(/,
    'main.js must define wireModelChip');
  // "function wireModelChip() {" itself matches a bare /wireModelChip\(\)/
  // search, so a single match proves only that the function was DEFINED —
  // the harness-wiring.test.js precedent this file follows has the same
  // property. Two-or-more is what proves it is also called somewhere.
  const hits = src.match(/\bwireModelChip\s*\(\s*\)/g) || [];
  assert.ok(hits.length >= 2,
    `main.js must CALL wireModelChip() somewhere besides its own definition ` +
    `(found ${hits.length} occurrence(s)) — or the chip renders but every control is a no-op`);
});

test('wireModelChip attaches a click handler to the chip button', () => {
  const src = readMain();
  const start = src.indexOf('function wireModelChip(');
  assert.ok(start >= 0, 'wireModelChip must be defined');
  const end = src.indexOf('\n}\n', start);
  assert.ok(end > start, 'wireModelChip must have a body');
  const body = src.slice(start, end);
  assert.match(body, /ch-modelchip[\s\S]{0,60}\.addEventListener\(\s*['"]click['"]/,
    'wireModelChip must attach a click listener to #ch-modelchip');
  assert.match(body, /mp-apply[\s\S]{0,40}\.addEventListener\(\s*['"]click['"]/,
    'wireModelChip must wire the Apply button');
  assert.match(body, /mp-reset[\s\S]{0,40}\.addEventListener\(\s*['"]click['"]/,
    'wireModelChip must wire the Reset (use bot default) button');
});

test('a click inside the picker panel does not bubble to the document-level close handler', () => {
  // The bug this guards: opening the native <select> fires a click that
  // bubbles to document, and the existing document listener (from the
  // thread-menu feature) would hide ANY open .menu on a bare click — without
  // stopPropagation here, opening the model dropdown closes the panel it is
  // part of.
  const src = readMain();
  assert.match(src, /model-picker'\]\.addEventListener\(\s*['"]click['"]\s*,\s*\(e\)\s*=>\s*e\.stopPropagation\(\)/,
    "main.js must stop a click inside #model-picker from bubbling to document");
});

test('renderChatHeader repaints the chip, and clearChatView hides it', () => {
  const src = readMain();
  const headerStart = src.indexOf('function renderChatHeader(');
  const headerEnd = src.indexOf('\nfunction ', headerStart + 1);
  assert.ok(headerStart >= 0 && headerEnd > headerStart, 'renderChatHeader must be defined');
  assert.match(src.slice(headerStart, headerEnd), /\brenderModelChip\s*\(\s*\)/,
    'renderChatHeader must call renderModelChip(), or the chip goes stale on every navigation');

  const clearStart = src.indexOf('function clearChatView(');
  const clearEnd = src.indexOf('\nfunction ', clearStart + 1);
  assert.ok(clearStart >= 0 && clearEnd > clearStart, 'clearChatView must be defined');
  const clearBody = src.slice(clearStart, clearEnd);
  assert.match(clearBody, /ch-modelchip'\]\.hidden\s*=\s*true/,
    'clearChatView must hide the chip when no thread is open');
});

test('the AgentRefused error frame sets the chip warning, and a new turn clears it', () => {
  const src = readMain();
  assert.match(src, /data\.refused[\s\S]{0,120}modelChipWarn\[data\.thread_id\]\s*=\s*data\.message/,
    "the WS 'error' handler must record the gateway's verbatim refusal text, keyed by thread");
  assert.match(src, /delete state\.modelChipWarn\[data\.thread_id\]/,
    "a fresh turn (WS 'thinking' started) must clear a stale warning");
});
