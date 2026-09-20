// Every function called in main.js must actually exist.
//
// This test exists because of a specific, embarrassing bug: applyNimChange()
// called renderBots() and renderThreadList(), NEITHER OF WHICH IS DEFINED
// ANYWHERE — the real names are renderSidebar() and renderThreads(). The
// function threw ReferenceError on its first line and re-rendered nothing, so
// toggling No-Image Mode did nothing until the next page load.
//
// Nothing caught it. `node --check` only parses. The unit tests cover pure
// logic, not DOM plumbing. And every end-to-end test RELOADED the page, which
// re-renders from scratch — so the broken path was never on the critical path
// of any assertion. A green suite, a working-looking app, and a dead function.
//
// A real linter (eslint no-undef) would be the proper tool. This is the
// zero-dependency stand-in that catches the same class of mistake in a repo
// that deliberately has no build step and no node_modules.

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { blankNonCode } from './_source-scan.js';

const HERE = dirname(fileURLToPath(import.meta.url));
const JS_DIR = join(HERE, '..', 'static', 'js');

// Browser and language globals a module may legitimately call without defining.
const GLOBALS = new Set([
  'document', 'window', 'console', 'localStorage', 'sessionStorage', 'fetch',
  'setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'requestAnimationFrame',
  'cancelAnimationFrame', 'queueMicrotask', 'structuredClone', 'alert', 'confirm',
  'prompt', 'atob', 'btoa', 'encodeURIComponent', 'decodeURIComponent', 'escape',
  'unescape', 'isNaN', 'isFinite', 'parseInt', 'parseFloat', 'String', 'Number',
  'Boolean', 'Array', 'Object', 'Date', 'Math', 'JSON', 'Promise', 'Map', 'Set',
  'WeakMap', 'WeakSet', 'Error', 'TypeError', 'RangeError', 'RegExp', 'Symbol',
  'Proxy', 'Reflect', 'BigInt', 'Intl', 'URL', 'URLSearchParams', 'Blob', 'File',
  'FileReader', 'FormData', 'Headers', 'Request', 'Response', 'AbortController',
  'Image', 'Audio', 'Event', 'CustomEvent', 'MouseEvent', 'KeyboardEvent',
  'MutationObserver', 'IntersectionObserver', 'ResizeObserver', 'WebSocket',
  'Notification', 'CSS', 'DOMParser', 'TextEncoder', 'TextDecoder', 'crypto',
  'navigator', 'location', 'history', 'screen', 'matchMedia', 'getComputedStyle',
  'scrollTo', 'open', 'close', 'print', 'focus', 'blur', 'require', 'import',
  'addEventListener', 'removeEventListener', 'dispatchEvent', 'postMessage',
  'reportError', 'clearImmediate', 'setImmediate',
  'super', 'this', 'if', 'for', 'while', 'switch', 'catch', 'return', 'typeof',
  'function', 'await', 'yield', 'new', 'delete', 'void', 'in', 'of', 'do', 'else',
]);

function declaredNames(src) {
  const names = new Set();
  const add = (re) => {
    for (const m of src.matchAll(re)) names.add(m[1]);
  };
  add(/\bfunction\s+([A-Za-z_$][\w$]*)/g);          // function foo()
  add(/\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)/g);  // const foo = ...
  add(/\bclass\s+([A-Za-z_$][\w$]*)/g);
  // import { a, b as c } from '...'  /  import d from '...'
  for (const m of src.matchAll(/import\s+([^;]+?)\s+from\s+['"][^'"]+['"]/g)) {
    for (const part of m[1].replace(/[{}]/g, ',').split(',')) {
      const bit = part.trim();
      if (!bit) continue;
      const asMatch = bit.match(/\bas\s+([A-Za-z_$][\w$]*)$/);
      names.add(asMatch ? asMatch[1] : bit.replace(/^\*\s*/, ''));
    }
  }
  // Destructured bindings and parameters are noisy to parse exactly; take any
  // identifier inside braces or parens that is followed by , } ) or = .
  add(/[{(,]\s*([A-Za-z_$][\w$]*)\s*(?=[,})=])/g);
  // Method DEFINITIONS: class methods and object-literal shorthand, e.g.
  //   connect() { ... }        (ws.js)
  //   codespan(token) { ... }  (markdown.js renderer)
  // These look exactly like calls to a regex, and reporting them was this
  // test's second wave of false positives.
  add(/^\s*(?:async\s+)?(?:get\s+|set\s+|\*\s*)?([A-Za-z_$][\w$]*)\s*\([^()]*\)\s*\{/gm);
  return names;
}

// Keywords that are followed by `(` but are not calls.
const KEYWORDS = new Set(['if', 'for', 'while', 'switch', 'catch', 'return',
  'typeof', 'await', 'new', 'of', 'in', 'do', 'else', 'function', 'async',
  'case', 'yield', 'delete', 'void', 'instanceof', 'throw', 'with', 'try']);

// The scanner lives in tests/_source-scan.js because i18n-keys.test.js needs
// exactly the same thing, and two copies of a lexer is one copy that drifts.
// It walks the source ONCE instead of running ordered regex passes — the
// nested-template and `'image/*'` holes were both order bugs, not pattern bugs.
function stripNoise(src) {
  return blankNonCode(src);
}

function calledNames(src) {
  // Bare `name(` calls only — never `obj.name(` (a method, not a binding) and
  // never a declaration site. That keeps this honest: it can miss things, but
  // what it DOES flag is always a real free identifier being invoked.
  const out = new Map();
  stripNoise(src).split('\n').forEach((line, i) => {
    for (const m of line.matchAll(/(^|[^\w$.?'"`])([a-z_$][\w$]*)\s*\(/g)) {
      const name = m[2];
      if (KEYWORDS.has(name)) continue;
      if (!out.has(name)) out.set(name, i + 1);
    }
  });
  return out;
}

const files = readdirSync(JS_DIR).filter((f) => f.endsWith('.js'));

for (const file of files) {
  test(`${file}: every function it calls exists`, () => {
    const src = readFileSync(join(JS_DIR, file), 'utf8');
    const declared = declaredNames(src);
    const missing = [];
    for (const [name, line] of calledNames(src)) {
      if (declared.has(name) || GLOBALS.has(name)) continue;
      missing.push(`${file}:${line} calls ${name}() — not defined or imported`);
    }
    assert.deepEqual(missing, [], '\n' + missing.join('\n'));
  });
}

// --- the scanner's own fixtures ------------------------------------------
//
// Each of these is a hole that actually shipped. They are asserted here rather
// than in a file of their own because this suite is the scanner's main
// consumer and the failure they cause is silent: the guard keeps passing while
// reading less and less of the source.

test('the source scanner survives the three holes that shipped', () => {
  const cases = [
    // 1. A string containing `/*` must not open a comment. main.js:
    //    `accept: 'image/*'` blanked 23+ consecutive lines of live code.
    ["const a = 'image/*'; brokenOne();", 'brokenOne'],
    // 2. A nested template literal. clients.js builds markup this way, and the
    //    old pattern paired the outer backtick with the inner one.
    ['const s = `x ${c ? `${y} check(s) here` : ""} z`; brokenTwo();', 'brokenTwo'],
    // 3. A regex or string that looks like the start of a comment.
    ['const q = /"/g; brokenThree();', 'brokenThree'],
    ["const sep = '//'; brokenFour();", 'brokenFour'],
    // And a block comment still IS a block comment.
    ['/* hidden() */ brokenFive();', 'brokenFive'],
  ];
  for (const [src, expected] of cases) {
    const names = [...calledNames(src).keys()];
    assert.ok(names.includes(expected),
      `scanner lost sight of ${expected}() in: ${src}`);
  }
  // The inverse: things inside a comment or a string are NOT calls.
  const hidden = calledNames([
    '// notACall()',
    '/* alsoNot() */',
    "const t = 'stillNot()';",
    'const u = `norThis()`;',
  ].join('\n'));
  assert.deepEqual([...hidden.keys()], [],
    'the scanner reported a call that is inside a comment or a literal');
});

test('the scanner reads the whole of main.js, not a prefix of it', () => {
  // The `'image/*'` hole blanked everything from that literal to the next
  // `*/`. A cheap invariant that would have caught it: the scanner must leave
  // roughly as many non-blank code lines as the file has non-comment lines.
  const src = readFileSync(join(JS_DIR, 'main.js'), 'utf8');
  const stripped = stripNoise(src);
  const codeLines = src.split('\n')
    .filter((l) => l.trim() && !l.trim().startsWith('//') && !l.trim().startsWith('*'));
  const survived = stripped.split('\n').filter((l) => l.trim()).length;
  assert.ok(survived > codeLines.length * 0.85,
    `only ${survived} lines survived stripping against ~${codeLines.length} code lines — `
    + 'the scanner is blanking live code');
});
