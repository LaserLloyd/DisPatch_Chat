// 4.3 L3: every class selector in app.css / w5.css / profiles.css has a producer somewhere
// (a literal in js/html/py/md/json, an app package, or the voice add-on's read-only release
// worktree), or belongs to a dynamic-prefix family whose class names are built at run time.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync, existsSync, statSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = fileURLToPath(new URL('.', import.meta.url));
const REPO = resolve(HERE, '..', '..');
const STATIC = join(REPO, 'frontend', 'static');

// Class names built from a prefix at run time (template strings / concatenation). A selector
// starting with one of these is treated as produced. Keep in step with LEGACY-INVENTORY.
const DYNAMIC_PREFIXES = [
  'hljs-',            // highlight.js token classes (markdown.js highlighter output)
  'cat-status-',      // integrations.js: `cat-status-${entry.status}`
  'cxn-',             // connections.js: `cxn-${kind}`
  'harness-',         // main.js: 'harness-' + harnessServiceState()
  'studioforge-',     // main.js: same builder for the StudioForge dot
  'md-callout--',     // markdown.js: `md-callout--${kind}`
  'notice-',          // main.js: `notice-${notice.level}`
  'people-role-',     // people.js: `people-role-${role}`
  'plugins-state-',   // plugins.js: `plugins-state-${ext.state}`
  'qs-',              // integrations.js: `qs-${r.status}`
  'snip-line-',       // theme.js: `snip-line-${w}`
  'sr-',              // settings-resize.js: `sr-${edge}`
  'status-',          // clients.js: `status-${status}`
  'verdict-',         // clients.js: verdict-* names returned by a function
  'advisor-',         // advisor.js: `advisor-${h.state}`
  'job-chip-state--', // apps/jobboard board.css states
];
// Built as `${cls}--text` (profile-picture.js previewNode), which a literal scan cannot see.
const DYNAMIC_EXACT = new Set(['pp-preview--text']);
// Host classes the DisPatch Voice add-on renders into (its own repo; not on a CI runner).
// Set DISPATCH_VOICE_SRC=<add-on src dir> to scan the add-on itself as well.
const ADDON_CLASSES = new Set(['bm-safe-btn']);
// hljs compound selectors such as `.hljs-title.class_.inherited__`.
const HLJS_SUFFIX = /^(class_|function_|inherited__|language_)$/;

const walk = (dir, exts, out = []) => {
  if (!existsSync(dir)) return out;
  for (const name of readdirSync(dir)) {
    if (['node_modules', '.venv', '__pycache__', 'tests', '.git'].includes(name)) continue;
    const p = join(dir, name);
    const st = statSync(p);
    if (st.isDirectory()) walk(p, exts, out);
    else if (exts.some((e) => name.endsWith(e))) out.push(p);
  }
  return out;
};

function corpus() {
  const files = [
    ...walk(STATIC, ['.js', '.html', '.json']),
    ...walk(join(REPO, 'apps'), ['.js', '.html', '.py', '.json', '.md']),
    ...walk(join(REPO, 'backend', 'app'), ['.py', '.md', '.html', '.json']),
    ...(process.env.DISPATCH_VOICE_SRC ? walk(process.env.DISPATCH_VOICE_SRC, ['.js', '.html', '.py', '.css']) : []),
  ];
  return files.map((f) => readFileSync(f, 'utf8')).join('\n');
}

function classesOf(file) {
  const css = readFileSync(join(STATIC, file), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');
  const preludes = css.replace(/\{[^{}]*\}/g, '{}');   // declaration bodies out (innermost first, twice for @media)
  const sels = preludes.replace(/\{[^{}]*\}/g, '{}');
  const found = new Set();
  for (const m of sels.matchAll(/\.(-?[A-Za-z_][\w-]*)/g)) found.add(m[1]);
  return found;
}

const escapeRe = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

test('every class selector in app.css, w5.css and profiles.css has a producer', () => {
  const text = corpus();
  const dead = [];
  for (const file of ['app.css', 'w5.css', 'profiles.css']) {
    for (const cls of classesOf(file)) {
      if (DYNAMIC_PREFIXES.some((p) => cls.startsWith(p)) || HLJS_SUFFIX.test(cls) || DYNAMIC_EXACT.has(cls) || ADDON_CLASSES.has(cls)) continue;
      if (!new RegExp(`(?<![\\w-])${escapeRe(cls)}(?![\\w-])`).test(text)) dead.push(`${file}: .${cls}`);
    }
  }
  assert.deepEqual(dead, [], `selectors with no producer:\n${dead.join('\n')}`);
});

test('every dynamic prefix still matches a class in the stylesheets (no stale allowlist rows)', () => {
  const all = new Set();
  for (const f of ['app.css', 'w5.css', 'profiles.css']) for (const c of classesOf(f)) all.add(c);
  const unused = DYNAMIC_PREFIXES.filter((p) => ![...all].some((c) => c.startsWith(p)));
  // job-chip-state-- lives in apps/jobboard/static/board.css, outside these three files
  assert.deepEqual(unused.filter((p) => p !== 'job-chip-state--'), []);
});
