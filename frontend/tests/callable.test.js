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

function stripNoise(src) {
  // Block comments FIRST — prose inside them is full of words like "tab()"
  // and "progress()", and every one was reported as a missing function on the
  // first run of this test. Newlines are preserved so line numbers survive.
  let out = src.replace(/\/\*[\s\S]*?\*\//g, (m) => m.replace(/[^\n]/g, ' '));
  out = out.replace(/(^|[^:])\/\/[^\n]*/g, (m, p1) => p1 + ' '.repeat(m.length - p1.length));
  // Strings, including multi-line template literals — they carry CSS like
  // rgba(...) and HTML that would otherwise read as calls.
  out = out.replace(/`(?:\\.|[^`\\])*`/gs, (m) => m.replace(/[^\n]/g, ' '));
  out = out.replace(/'(?:\\.|[^'\\\n])*'/g, '""').replace(/"(?:\\.|[^"\\\n])*"/g, '""');
  // Regex literals. A pattern like /[ \t]*\u{E200}cite(?:...)/g contains
  // "cite(" and was reported as a call to a missing cite(). Only strip where a
  // regex can legally START, so the `/` of a division is left alone.
  out = out.replace(/([=(,:[!&|?{;]|\breturn)(\s*)\/(?![*/])(?:\\.|\[(?:\\.|[^\]\\])*\]|[^/\\\n])+\/[gimsuy]*/g,
                    (m, pre, ws) => pre + ws + '/RE/');
  return out;
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
