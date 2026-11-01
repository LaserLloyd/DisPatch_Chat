// A label must never be a humanised key. i18n.js falls back to "Pill agent" for
// t('cpage.backend_agent') when a string is missing (or a phone holds an old en.json),
// so this test resolves EVERY key the front end can ask for -- including keys that
// live in maps (FILTER_LABEL, PILL, COPY_STATE ...) and template literals -- and
// requires each to exist in en.json and in every other locale.

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { basename, dirname, join } from 'node:path';
import { blankNonCode } from './_source-scan.js';

const HERE = dirname(fileURLToPath(import.meta.url));
const STATIC = join(HERE, '..', 'static');
const LOC = join(STATIC, 'locales');
const load = (f) => JSON.parse(readFileSync(join(LOC, f), 'utf8'));
const flat = (o, p = '', out = new Map()) => {
  for (const [k, v] of Object.entries(o)) {
    if (k === '$meta') continue;
    if (v && typeof v === 'object' && !Array.isArray(v) && v.other === undefined) flat(v, `${p}${k}.`, out);
    else out.set(p + k, v);
  }
  return out;
};
const EN = flat(load('en.json'));
const NAMESPACES = new Set([...EN.keys()].map((k) => k.split('.')[0]));
const LANGS = readdirSync(LOC).filter((f) => f.endsWith('.json') && f !== 'en.json');

const jsFiles = readdirSync(join(STATIC, 'js')).filter((f) => f.endsWith('.js')).map((f) => join(STATIC, 'js', f));
const sources = jsFiles.map((f) => ({ f, text: blankNonCode(readFileSync(f, 'utf8'), { keepStrings: true, keepTemplates: true }) }));
const html = readFileSync(join(STATIC, 'index.html'), 'utf8').replace(/<!--[\s\S]*?-->/g, ' ');

// Server setting ids that look like keys (PATCH /api/settings names), not labels.
const SETTING_IDS = new Set(['people.default_profile', 'people.lock_target']);
const KEY = /^[a-z][a-z0-9]*(?:\.[a-z0-9_]+)+$/;
const used = new Map(); // key -> where
const prefixes = []; // template-literal prefixes
for (const { f, text } of sources) {
  const name = basename(f); // not split('/'): Windows paths use backslashes
  if (name === 'settings_schema.js') continue; // server setting ids, not label keys
  // every quoted literal that is shaped like a key in a real namespace (resolves map values)
  for (const m of text.matchAll(/(['"`])([a-z][a-z0-9]*(?:\.[a-z0-9_]+)+)\1/g)) {
    if (NAMESPACES.has(m[2].split('.')[0]) && !m[2].endsWith('_') && !/\.(yaml|json|js|css)$/.test(m[2]) && !SETTING_IDS.has(m[2])) used.set(m[2], name);
  }
  for (const m of text.matchAll(/`([a-z][a-z0-9]*\.[a-z0-9_.]*)\$\{/g)) if (NAMESPACES.has(m[1].split('.')[0])) prefixes.push([m[1], name]);
}
for (const m of html.matchAll(/data-i18n(?:-html)?="([^"]+)"/g)) used.set(m[1], 'index.html');
for (const m of html.matchAll(/data-i18n-attr="([^"]+)"/g)) {
  for (const p of m[1].split(/[;,]/)) { const k = (p.split('=')[1] || '').trim(); if (k) used.set(k, 'index.html'); }
}

test('every key a Settings label can resolve to exists in en.json', () => {
  const missing = [...used].filter(([k]) => KEY.test(k) && !EN.has(k)
    && ![...EN.keys()].some((e) => e.startsWith(`${k}.`))); // namespace use, e.g. has('nim')
  assert.deepEqual(missing.map(([k, w]) => `${k} (${w})`), []);
});

test('every template-literal key prefix has at least one en.json key', () => {
  const bad = prefixes.filter(([p]) => ![...EN.keys()].some((e) => e.startsWith(p)));
  assert.deepEqual(bad.map(([p, w]) => `${p} (${w})`), []);
});

for (const lang of LANGS) {
  test(`${lang} locale has every en.json key`, () => {
    const L = flat(load(lang));
    const gone = [...EN.keys()].filter((k) => !L.has(k));
    assert.deepEqual(gone, []);
  });
}
