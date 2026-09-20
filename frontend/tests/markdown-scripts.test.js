// Behavioural tests for the inline extensions DisPatch adds on top of GFM:
// `H~2~O` subscript, `x^2^` superscript and `==highlight==`.
//
// These need a real render, not a string scan, because the thing under test is
// a marked tokenizer — so the test builds the same stack the browser does: a
// jsdom window, the vendored marked and DOMPurify from frontend/static/vendor,
// then markdown.js on top. That is also why this lives in its own file rather
// than in markdown-behaviour.test.js, which deliberately imports markdown.js
// under bare node and can only exercise the DOM-free half.
//
// Skips cleanly when jsdom is absent, like its sibling. A skipped test is not
// a passing one: if you are touching the tokenizers, make sure these RUN.
//
//     mkdir /tmp/jsdom && cd /tmp/jsdom && npm i --no-save jsdom
//     NODE_PATH=/tmp/jsdom/node_modules node --test frontend/tests/
//
// WHY THIS FILE EXISTS
// --------------------
// The sub/superscript tokenizers originally allowed whitespace inside their
// delimiters, which meant the delimiters did not delimit anything — the opener
// paired with the next caret/tilde anywhere on the line and swallowed the text
// between them:
//
//     2^10 = 1024 and x^n   ->  2<sup>10 = 1024 and x</sup>n
//     hash = a ^ b ^ c      ->  hash = a <sup> b </sup> c
//
// Agents write exponents, XOR and pointer notation constantly, so ordinary
// technical prose came out mangled. The guard is that a real sub/superscript
// contains no whitespace.

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { domSkip } from './_require-dom.js';

const HERE = dirname(fileURLToPath(import.meta.url));
const STATIC = join(HERE, '..', 'static');
const require = createRequire(import.meta.url);

let JSDOM = null;
try {
  ({ JSDOM } = require('jsdom'));
} catch {
  JSDOM = null;
}

/** Build a window with marked + DOMPurify on it, exactly as index.html does,
 *  and return markdown.js bound to that window. */
async function loadRenderer() {
  // `runScripts: 'dangerously'` is required, not incidental: without it jsdom
  // gives `window.eval` a realm where a UMD bundle's global assignment goes
  // nowhere, and `window.marked` stays undefined — which would silently skip
  // every test below. There is no untrusted input here; the only scripts run
  // are this repo's own vendored files.
  const dom = new JSDOM('<!doctype html><html><body></body></html>', {
    url: 'http://localhost/',
    pretendToBeVisual: true,
    runScripts: 'dangerously',
  });
  const { window } = dom;

  // markdown.js reads these off the global `window` at call time. Assigning
  // the globals BEFORE importing it is what lets the module's own
  // `typeof window` guards pass.
  //
  // defineProperty rather than plain assignment: on node >= 21 some of these
  // (`navigator` in particular) exist on globalThis as accessor properties
  // with no setter, so `globalThis.navigator = …` throws TypeError. That throw
  // used to be swallowed by the caller's catch and every test below silently
  // SKIPPED — a green run that tested nothing.
  for (const [key, value] of Object.entries({
    window,
    document: window.document,
    navigator: window.navigator,
    Node: window.Node,
    NodeFilter: window.NodeFilter,
    Element: window.Element,
    DocumentFragment: window.DocumentFragment,
  })) {
    Object.defineProperty(globalThis, key, {
      value, writable: true, configurable: true, enumerable: false,
    });
  }

  // The vendored libraries are plain UMD bundles. `window.eval` runs them in
  // the jsdom realm so their global assignment lands on THIS window, which is
  // how the browser loads them too. (`new window.Function(src)()` looks
  // equivalent and is not — the bundle's global detection finds nothing and
  // the library never attaches.)
  for (const lib of ['marked.min.js', 'purify.min.js']) {
    window.eval(readFileSync(join(STATIC, 'vendor', lib), 'utf8'));
  }
  if (!window.marked || !window.DOMPurify) return null;

  // markdown.js guards with `window.marked` but then calls BARE `marked`
  // (markdown.js:84-85) — legal in a browser, where the window IS the global
  // scope, and a ReferenceError under node, where it is not. Mirror the
  // browser by publishing both names.
  for (const name of ['marked', 'DOMPurify', 'hljs']) {
    if (window[name] === undefined) continue;
    Object.defineProperty(globalThis, name, {
      value: window[name], writable: true, configurable: true, enumerable: false,
    });
  }

  const mod = await import(join(STATIC, 'js', 'markdown.js') + `?scripts=${Date.now()}`);
  return mod;
}

// A loader failure must not look like "jsdom is not installed". It is reported
// with its real cause, and the skip reason carries that cause, so a broken
// harness reads as a broken harness instead of a clean green run.
let loadError = null;
let ready = null;
if (JSDOM) {
  try {
    ready = await loadRenderer();
  } catch (err) {
    loadError = err;
    // eslint-disable-next-line no-console
    console.error('[markdown-scripts] renderer failed to load:', err);
  }
}
const skip = domSkip(ready
  ? false
  : (loadError
    ? `renderer failed to load: ${loadError.message}`
    : 'needs jsdom plus the vendored marked/DOMPurify'));

test('a superscript never spans whitespace', { skip }, () => {
  // The exact prose that was being mangled. Two carets on one line must stay
  // two carets, not a 17-character superscript.
  const out = ready.renderMarkdown('2^10 = 1024 and x^n');
  assert.ok(!/<sup>/.test(out), `expected no <sup> in: ${out}`);
  assert.match(out, /2\^10/, 'the literal text must survive');
});

test('a subscript never spans whitespace', { skip }, () => {
  const out = ready.renderMarkdown('a~1 and b~2 differ');
  assert.ok(!/<sub>/.test(out), `expected no <sub> in: ${out}`);
});

test('an XOR expression is not a superscript', { skip }, () => {
  const out = ready.renderMarkdown('hash = a ^ b ^ c');
  assert.ok(!/<sup>/.test(out), `expected no <sup> in: ${out}`);
});

test('the intended forms still render', { skip }, () => {
  assert.match(ready.renderMarkdown('x^2^ is fine'), /<sup>2<\/sup>/);
  assert.match(ready.renderMarkdown('H~2~O is fine'), /<sub>2<\/sub>/);
  // The reason these extensions exist at all: a single tilde must not be read
  // as GFM strikethrough. Double tildes still belong to marked's own `del`.
  assert.match(ready.renderMarkdown('~~struck~~'), /<(?:del|s)>struck<\/(?:del|s)>/);
});

test('inline code protects carets and tildes from both tokenizers', { skip }, () => {
  const out = ready.renderMarkdown('`2^10 = 1024 and x^n`');
  assert.match(out, /<code>/);
  assert.ok(!/<sup>/.test(out), `expected no <sup> in: ${out}`);
});

test('the == highlight rule keeps its own whitespace guard', { skip }, () => {
  // This one was already correct; the assertion pins it so the three rules
  // cannot drift apart again.
  const out = ready.renderMarkdown('if a == b and c == d then');
  assert.ok(!/<mark>/.test(out), `expected no <mark> in: ${out}`);
  assert.match(ready.renderMarkdown('==really=='), /<mark>really<\/mark>/);
});
