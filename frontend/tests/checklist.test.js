// Checklist table ordering, pinned without a DOM.
//
// checklist.js is split the same way markdown.js is: `checklistOrder` is pure
// logic (runs anywhere) and the widget wiring needs a DOM. The ordering rule is
// the load-bearing half — completed rows always group at the bottom in CHECK
// order, incomplete rows follow the sort or the authored order — so it is
// pinned here, while the DOM half skips cleanly when jsdom is absent.

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const STATIC = join(HERE, '..', 'static');

const { checklistOrder } = await import(join(STATIC, 'js', 'checklist.js'));

// Rows for a 4-exercise workout: Exercise / Sets / Reps / Rest, authored 0..3.
const rows = [
  { index: 0, text: (c) => ['Squat', '3', '10', '60s'][c] },
  { index: 1, text: (c) => ['Push-up', '3', '15', '30s'][c] },
  { index: 2, text: (c) => ['Deadlift', '5', '5', '3min'][c] },
  { index: 3, text: (c) => ['Plank', '1', '60s', '—'][c] },
];

test('authored order with nothing checked and no sort', () => {
  assert.deepEqual(checklistOrder(rows, [], null), [0, 1, 2, 3]);
});

test('completed rows group at the bottom in check order', () => {
  // Check row 1, then row 3.
  assert.deepEqual(checklistOrder(rows, [1, 3], null), [0, 2, 1, 3]);
  // Check order, not authored order: [3, 1] puts 3 before 1 at the bottom.
  assert.deepEqual(checklistOrder(rows, [3, 1], null), [0, 2, 3, 1]);
  // Everything checked keeps the check order.
  assert.deepEqual(checklistOrder(rows, [2, 0, 3, 1], null), [2, 0, 3, 1]);
});

test('sort orders only the incomplete group; completed stay pinned', () => {
  // Completed rows 0 and 2 (in that check order). Sort the rest by Exercise.
  const sort = { col: 0, dir: 'ascending' };
  // Incomplete = rows 1 (Push-up) and 3 (Plank) → ascending: Plank(3) < Push-up(1).
  assert.deepEqual(checklistOrder(rows, [0, 2], sort), [3, 1, 0, 2]);
  // Descending flips the incomplete group; completed still 0, 2 at the bottom.
  assert.deepEqual(checklistOrder(rows, [0, 2], { col: 0, dir: 'descending' }), [1, 3, 0, 2]);
});

test('sort is numeric-aware with blanks last, like markdown tables', () => {
  // Reps column (col 2): 10, 15, 5, 60s. 60s has no number → blank-ish → last.
  assert.deepEqual(checklistOrder(rows, [], { col: 2, dir: 'ascending' }), [2, 0, 1, 3]);
  // Sets column (col 1): 1, 3, 3, 5 → 1 < 3 < 3 < 5, stable tie keeps 0 before 1.
  assert.deepEqual(checklistOrder(rows, [], { col: 1, dir: 'ascending' }), [3, 0, 1, 2]);
});

// ---------------------------------------------------------------------------
// The DOM half (jsdom), mirroring markdown-behaviour.test.js's skip.
// ---------------------------------------------------------------------------

const require = createRequire(import.meta.url);
let jsdom = null;
try { jsdom = require('jsdom'); } catch { /* not installed — tests skip */ }

const dom = { skip: jsdom ? false : 'jsdom is not installed (see markdown-behaviour.test.js)' };

let _win = null;
async function withDom() {
  if (_win) return _win;
  const { JSDOM } = jsdom;
  const win = new JSDOM('<!doctype html><html><body></body></html>', {
    url: 'http://127.0.0.1:8765/',
    runScripts: 'outside-only',
  }).window;
  const run = (file) => {
    const code = readFileSync(join(STATIC, 'vendor', file), 'utf8');
    win.eval(code);
  };
  run('purify.min.js');
  run('marked.min.js');
  // The widget PATCHes on every click. Without a stub this reached a real
  // socket and the assertions raced the revert handler.
  globalThis.fetch = async () => ({
    ok: true, status: 200, headers: new win.Headers(), json: async () => ({ ok: true }),
  });
  globalThis.window = win;
  globalThis.document = win.document;
  globalThis.DOMPurify = win.DOMPurify;
  globalThis.marked = win.marked;
  assert.ok(globalThis.DOMPurify, 'purify.min.js did not define DOMPurify');
  assert.ok(globalThis.marked, 'marked.min.js did not define marked');
  _win = win;
  return win;
}

test('a ```checklist fence renders as a .checklist-widget table, not a code block', { skip: dom.skip }, async () => {
  const win = await withDom();
  const { renderMarkdown, enhanceContent } = await import(join(STATIC, 'js', 'markdown.js'));
  const { installChecklists } = await import(join(STATIC, 'js', 'checklist.js'));
  const md = '```checklist\n| Exercise | Sets | Reps | Rest |\n|---|---|---|---|\n| Squat | 3 | 10 | 60s |\n| Push-up | 3 | 15 | 30s |\n```';
  const div = win.document.createElement('div');
  div.innerHTML = renderMarkdown(md);
  win.document.body.appendChild(div);

  assert.ok(div.querySelector('.checklist-widget'), 'checklist widget exists');
  assert.equal(div.querySelector('.checklist-widget table').tBodies[0].rows.length, 2);
  // No code block: the fence was consumed as a table.
  assert.equal(div.querySelector('pre code'), null);

  enhanceContent(div);
  installChecklists(div, { message: { id: 'm1', metadata: {} }, readonly: false });

  const table = div.querySelector('table');
  assert.equal(table.tHead.rows[0].cells[0].classList.contains('cl-check'), true, 'checkbox column header');
  const bodyRows = Array.from(table.tBodies[0].rows);
  assert.equal(bodyRows.length, 2);
  assert.equal(bodyRows[0].cells[0].querySelector('input.cl-checkbox')?.type, 'checkbox');
  // Data columns sortable, checkbox column not.
  assert.equal(table.tHead.rows[0].cells[1].classList.contains('md-th'), true);

  // Clicking a checkbox moves the row to the bottom.
  bodyRows[0].cells[0].querySelector('input.cl-checkbox').click();
  assert.deepEqual(
    Array.from(table.tBodies[0].rows).map((r) => r.cells[1].textContent),
    ['Push-up', 'Squat'], 'checked row moved to the bottom');
  assert.ok(table.tBodies[0].rows[1].classList.contains('cl-done'));
  div.remove();
});

test('readonly (Safe Mode) checkboxes are disabled', { skip: dom.skip }, async () => {
  const win = await withDom();
  const { renderMarkdown, enhanceContent } = await import(join(STATIC, 'js', 'markdown.js'));
  const { installChecklists } = await import(join(STATIC, 'js', 'checklist.js'));
  const div = win.document.createElement('div');
  div.innerHTML = renderMarkdown('```checklist\n| a |\n|---|\n| x |\n```');
  win.document.body.appendChild(div);
  enhanceContent(div);
  installChecklists(div, { message: { id: 'm2', metadata: {} }, readonly: true });
  const cb = div.querySelector('input.cl-checkbox');
  assert.equal(cb.disabled, true);
  div.remove();
});


test('a rejected PATCH puts the row back', { skip: dom.skip }, async () => {
  // The bug this pins: `change` fires after the browser has already flipped
  // the checkbox, so reading the DOM for the "previous" state returned the NEW
  // state and the revert repainted what it was meant to undo. A locked device
  // (403), an offline tap or a 500 left the row checked and pinned to the
  // bottom for good, telling the user something was done that the server never
  // recorded.
  const win = await withDom();
  const { renderMarkdown, enhanceContent } = await import(join(STATIC, 'js', 'markdown.js'));
  const { installChecklists } = await import(join(STATIC, 'js', 'checklist.js'));

  globalThis.fetch = async () => { throw new Error('403 as a locked device would'); };

  const div = win.document.createElement('div');
  div.innerHTML = renderMarkdown(
    '```checklist\n| Exercise |\n|---|\n| Squat |\n| Push-up |\n```');
  win.document.body.appendChild(div);
  enhanceContent(div);
  installChecklists(div, { message: { id: 'm3', metadata: {} }, readonly: false });

  const table = div.querySelector('table');
  const first = table.tBodies[0].rows[0].querySelector('input.cl-checkbox');
  first.click();
  await new Promise((r) => setTimeout(r, 20));   // let the rejected promise settle

  assert.equal(
    table.tBodies[0].rows[0].querySelector('input.cl-checkbox').checked, false,
    'the checkbox must go back to unchecked when the server refuses');
  assert.deepEqual(
    Array.from(table.tBodies[0].rows).map((r) => r.cells[1].textContent),
    ['Squat', 'Push-up'], 'and the row must return to its original position');
  div.remove();
});
