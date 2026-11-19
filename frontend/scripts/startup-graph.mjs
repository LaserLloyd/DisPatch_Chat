#!/usr/bin/env node
// The service worker's version table, generated so it is never kept by hand.
//
//   node frontend/scripts/startup-graph.mjs           rewrite the table (after any ?v= bump)
//   node frontend/scripts/startup-graph.mjs --check   exit 1 when it is out of date
//
// sw.js, between `startup-versions:begin` / `:end`: the ?v= every versioned shell file is
// loaded at (index.html + js/*.js). The install step precaches `<path>?v=<n>`, the exact
// URL the page used, so the copy the page just put in the HTTP cache is reused instead of
// downloaded again, and the fetch handler answers that version from its own cache.
//
// It also computes the startup module graph (what main.js reaches by STATIC imports) for
// the budget test. (A generated <link rel="modulepreload"> list for that graph was tried
// and measured: on a throttled phone it cost ~0.8 s of first paint on every warm open
// and gained ~0.6 s only on a first visit, so it is not written; docs/design/first-load.md.)
//
// No dependencies, no bundling: it reads the same files the browser does. The node suite
// (tests/startup-graph.test.js) runs the check, so a ?v= bump without a re-run fails there.
import { readFileSync, writeFileSync, readdirSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

export const STATIC = join(dirname(fileURLToPath(import.meta.url)), '..', 'static');
const JS = join(STATIC, 'js');

// `import x from './a.js?v=3'`, `import './a.js?v=3'`, `export { y } from './a.js?v=3'`.
// Dynamic import() is deliberately NOT matched: a lazy module is not on the startup path.
const STATIC_IMPORT = /^\s*(?:import|export)\s+(?:[^'";]*?\s+from\s+)?['"]\.\/([\w.-]+\.js)\?v=(\d+)['"]/gm;
const JS_REF = /['"]\.\/([\w.-]+\.(?:js|css))\?v=(\d+)['"]/g;
const ABS_REF = /['"](\/static\/[\w./-]+\.\w+)\?v=(\d+)['"]/g;

const read = (p) => readFileSync(p, 'utf8');

/** Modules reachable from main.js by static imports, in breadth-first order (main.js excluded). */
export function startupGraph(entry = 'main.js') {
  const order = [];
  const versions = new Map();
  const seen = new Set([entry]);
  const queue = [entry];
  while (queue.length) {
    const f = queue.shift();
    for (const m of read(join(JS, f)).matchAll(STATIC_IMPORT)) {
      const [, name, v] = m;
      if (!versions.has(name)) versions.set(name, v);
      if (!seen.has(name)) { seen.add(name); order.push(name); queue.push(name); }
    }
  }
  return order.map((name) => ({ path: `/static/js/${name}`, v: versions.get(name) }));
}

/** Every `?v=` URL the shell uses (index.html + js/*.js): bare path -> version. */
export function shellVersions() {
  const out = new Map();
  const add = (path, v) => { if (!out.has(path)) out.set(path, v); };
  const html = read(join(STATIC, 'index.html'));
  for (const m of html.matchAll(/(?:src|href)="(\/static\/[^"?]+)\?v=(\d+)"/g)) add(m[1], m[2]);
  for (const f of readdirSync(JS).filter((n) => n.endsWith('.js')).sort()) {
    const src = read(join(JS, f));
    for (const m of src.matchAll(JS_REF)) add(`/static/js/${m[1]}`, m[2]);
    for (const m of src.matchAll(ABS_REF)) add(m[1], m[2]);
  }
  return new Map([...out].sort(([a], [b]) => (a < b ? -1 : 1)));
}

function replaceBlock(text, begin, end, body, file) {
  const i = text.indexOf(begin);
  const j = text.indexOf(end);
  if (i < 0 || j < i) throw new Error(`${file}: markers ${begin} / ${end} not found`);
  const lineStart = text.lastIndexOf('\n', i) + 1;
  const indent = text.slice(lineStart, i);
  return text.slice(0, i) + begin + '\n' + body.map((l) => indent + l + '\n').join('') + indent + text.slice(j);
}

export function render() {
  const swPath = join(STATIC, 'sw.js');
  const sw = read(swPath);
  const vers = [...shellVersions()].map(([p, v]) => `'${p}': '${v}',`);
  return {
    [swPath]: [sw, replaceBlock(sw, '// startup-versions:begin', '// startup-versions:end',
      ['const VERSIONS = {', ...vers.map((l) => '  ' + l), '};'], 'sw.js')],
  };
}

/** The files whose generated block is stale (empty = up to date). */
export function stale() {
  return Object.entries(render()).filter(([, [now, want]]) => now !== want).map(([p]) => p);
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  if (process.argv.includes('--check')) {
    const s = stale();
    if (s.length) {
      console.error(`out of date: ${s.join(', ')}\nrun: node frontend/scripts/startup-graph.mjs`);
      process.exit(1);
    }
    console.log('startup blocks up to date');
  } else {
    for (const [p, [now, want]] of Object.entries(render())) {
      if (now !== want) { writeFileSync(p, want); console.log(`wrote ${p}`); }
    }
  }
}
