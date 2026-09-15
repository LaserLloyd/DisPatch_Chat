// Regression pin: the harness pane must wire its click handlers.
//
// 2026-09-15 — wireHarnessView() and wireStudioForgeView() were orphaned
// by commit 7eee8f1 (the terminal-pane removal). The functions still
// existed; nothing called them; the pane rendered correctly but every
// button was a no-op. This file pins both calls into init() so a
// future refactor that drops them again fails loudly here.
//
// We pin via textual grep, not by spinning up the full main.js — that
// drags in the entire import graph and is fragile. The pin is the
// minimum assertion that catches the regression.

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

test('main.js wires the harness pane (wireHarnessView() must be called from init)', () => {
  const src = readMain();
  // Find the function definition (the orphan we don't want to recreate).
  assert.match(src, /function\s+wireHarnessView\s*\(/,
    'main.js must define wireHarnessView');
  // Find the function invocation. The bug was the definition without a call.
  // We look for a non-definition occurrence: a bare "wireHarnessView()" or
  // "wireHarnessView();" pattern. The definition itself is "function wireHarnessView(",
  // which the regex above excludes.
  assert.match(src, /\bwireHarnessView\s*\(\s*\)/,
    'main.js must CALL wireHarnessView() at least once — orphaning the wiring ' +
    'was the bug closed on 2026-09-15');
});

test('main.js wires the StudioForge pane (wireStudioForgeView() must be called from init)', () => {
  const src = readMain();
  assert.match(src, /function\s+wireStudioForgeView\s*\(/,
    'main.js must define wireStudioForgeView');
  assert.match(src, /\bwireStudioForgeView\s*\(\s*\)/,
    'main.js must CALL wireStudioForgeView() at least once — sister regression ' +
    'to the harness-pane orphan, closed on 2026-09-15');
});

test('wireHarnessView attaches a click handler to #harness-start', () => {
  // The pane's Start button is what the user reported broken. The fix
  // attaches the listener; this test pins that the listener is wired
  // inside the function body. A future refactor that removes the
  // attachEventListener call will fail this test.
  const src = readMain();
  const start = src.indexOf('function wireHarnessView(');
  assert.ok(start >= 0, 'wireHarnessView must be defined');
  const end = src.indexOf('\n}\n', start);
  assert.ok(end > start, 'wireHarnessView must have a body');
  const body = src.slice(start, end);
  // The body must reference #harness-start in some form. The actual code
  // uses `dom['harness-start']` (the dom map at the top of main.js);
  // either spelling counts as long as it ends up at the same element.
  assert.match(body, /harness-start/,
    'wireHarnessView must reference #harness-start');
  assert.match(body, /harness-start[\s\S]{0,40}\.addEventListener\(\s*['"]click['"]/,
    'wireHarnessView must attach a click listener to #harness-start');
});

test('main.js init block calls wireHarnessView and wireStudioForgeView near wireAuthEvents', () => {
  // The fix lives next to wireAuthEvents() in the init() bootstrap.
  // A future refactor that moves wireHarnessView() into the wrong
  // place (e.g. inside a view-mount handler) would still pass the
  // "called from init" test above but break boot ordering — so this
  // second pin keeps them next to each other.
  const src = readMain();
  const authAt = src.indexOf('wireAuthEvents();');
  assert.ok(authAt >= 0, 'main.js must call wireAuthEvents();');
  const harnessAt = src.indexOf('wireHarnessView();', authAt);
  assert.ok(harnessAt > 0 && harnessAt < authAt + 400,
    'wireHarnessView() must be called shortly after wireAuthEvents(); ' +
    'a future refactor that moves it elsewhere risks breaking boot ordering');
  const studioAt = src.indexOf('wireStudioForgeView();', authAt);
  assert.ok(studioAt > 0 && studioAt < authAt + 400,
    'wireStudioForgeView() must be called shortly after wireAuthEvents()');
});
