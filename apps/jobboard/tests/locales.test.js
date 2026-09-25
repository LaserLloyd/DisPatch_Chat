// Job Board app — its locale files and its static page, held to the shell's
// rules.
//
// The strings moved out of the shell's `jobs.*` namespace into
// apps/jobboard/locales/<lang>.json (flat keys) when the board became an app.
// The shell validates its own locales with scripts/check-locales.py and
// frontend/tests/i18n-keys.test.js; this file does the same for the app:
//   * the same eight languages, each with the shell's $meta block;
//   * key parity with English, both directions;
//   * placeholder parity ({n}, {msg}, … never dropped or invented);
//   * every key the code asks for exists, and every key is used;
//   * scripts/check-locales.py passes on the app's directory (its HTML/URI
//     allowlist is a security boundary for any locale file);
// and the page's own rules: no inline script/style (its CSP is script-src
// 'self'), every ?v= it names points at a file that exists, and the SDK is
// imported at the version the shell itself uses.

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync, existsSync } from 'node:fs';
import { join } from 'node:path';
import { spawnSync } from 'node:child_process';
import { appDir, REPO, STATIC } from '../../../frontend/tests/helpers/app-test-env.js';

const APP = appDir('jobboard');
const LOCALES = join(APP, 'locales');
const LANGS = ['en', 'ar', 'de', 'es', 'fr', 'ja', 'pt', 'zh'];
const load = (l) => JSON.parse(readFileSync(join(LOCALES, `${l}.json`), 'utf8'));
const keysOf = (d) => Object.keys(d).filter((k) => k !== '$meta');
const placeholders = (v) => new Set([...String(v).matchAll(/\{(\w+)\}/g)].map((m) => m[1]));
const SHELL_LOCALES = join(STATIC, 'locales');

const SOURCES = ['board.js', 'thread.js'].map((f) => readFileSync(join(APP, 'static', f), 'utf8'));
const CODE = SOURCES.join('\n');

test('exactly the shell’s eight languages, each with the shell’s $meta', () => {
  const files = readdirSync(LOCALES).filter((f) => f.endsWith('.json')).map((f) => f.replace(/\.json$/, '')).sort();
  assert.deepEqual(files, [...LANGS].sort());
  for (const l of LANGS) {
    const shellMeta = JSON.parse(readFileSync(join(SHELL_LOCALES, `${l}.json`), 'utf8')).$meta;
    assert.deepEqual(load(l).$meta, shellMeta, `${l}: $meta matches the shell`);
  }
});

test('flat keys, and every language has exactly English’s keys', () => {
  const en = keysOf(load('en'));
  for (const k of en) assert.equal(typeof load('en')[k], 'string', `${k}: flat string values`);
  for (const l of LANGS) {
    const have = keysOf(load(l));
    assert.deepEqual([...have].sort(), [...en].sort(), `${l}: key set differs from en`);
  }
});

test('placeholders are never dropped or invented by a translation', () => {
  const en = load('en');
  for (const l of LANGS) {
    const d = load(l);
    for (const k of keysOf(en)) {
      assert.deepEqual([...placeholders(d[k])].sort(), [...placeholders(en[k])].sort(), `${l}:${k}`);
    }
  }
});

test('every key the code asks for exists, and every key is used', async () => {
  const en = load('en');
  const asked = new Set([...CODE.matchAll(/\bt\(\s*'([^']+)'/g)].map((m) => m[1]));
  // Keys the code reaches through literal lookup tables (STATE_KEY & co).
  for (const m of CODE.matchAll(/:\s*'((?:state|remote|seniority|reasons|activity\.type)\.[a-z_]+)'/g)) asked.add(m[1]);
  const missing = [...asked].filter((k) => !(k in en));
  assert.deepEqual(missing, [], 'keys the code asks for that en.json does not have');
  // Used = named as a quoted literal somewhere in the code (t('x'), a lookup
  // table, or a helper argument like btn('yes', 'vote.yes', …)).
  const unused = keysOf(en).filter((k) => !asked.has(k) && !CODE.includes(`'${k}'`));
  assert.deepEqual(unused, [], 'keys in en.json that no code asks for');
});

test('scripts/check-locales.py passes on the app’s locales', { skip: spawnSync('python3', ['--version']).status !== 0 && 'python3 not available' }, () => {
  const r = spawnSync('python3', [join(REPO, 'scripts', 'check-locales.py'), '--locales-dir', LOCALES], { encoding: 'utf8' });
  assert.equal(r.status, 0, r.stdout + r.stderr);
  assert.match(r.stdout, /\b0 errors\b/);
});

test('the page has no inline script or style (its CSP is script-src \'self\')', () => {
  const html = readFileSync(join(APP, 'static', 'index.html'), 'utf8').replace(/<!--[\s\S]*?-->/g, '');
  assert.deepEqual([...html.matchAll(/<script(?![^>]*\bsrc=)[^>]*>/g)].map((m) => m[0]), []);
  assert.doesNotMatch(html, /<style\b/);
  assert.doesNotMatch(html, /\sstyle="/);
  assert.doesNotMatch(html, /\son[a-z]+=/i, 'no inline event handlers');
  for (const f of SOURCES) {
    assert.doesNotMatch(f, /setAttribute\(\s*['"]style['"]|(?:^|[{,]\s*)style\s*:\s*['"`]/m, 'no JS-built style attribute');
    assert.doesNotMatch(f, /\.innerHTML\s*=/, 'text only — no innerHTML sinks');
  }
});

test('every ?v= the app names points at a file, and the SDK version matches the shell’s', () => {
  const files = ['index.html', 'board.js', 'thread.js'].map((f) => [f, readFileSync(join(APP, 'static', f), 'utf8')]);
  const main = readFileSync(join(STATIC, 'js', 'main.js'), 'utf8');
  const shellSdk = /from '\.\/app-sdk\.js\?v=(\d+)'/.exec(main)[1];
  const cssVersions = new Set();
  for (const [name, src] of files) {
    for (const m of src.matchAll(/['"](\/(?:apps\/jobboard|static)\/[^'"?]+)\?v=(\d+)['"]/g)) {
      const [, path, v] = m;
      const file = path.startsWith('/static/') ? join(STATIC, path.slice('/static/'.length))
        : join(APP, 'static', path.slice('/apps/jobboard/'.length));
      assert.ok(existsSync(file), `${name}: ${path} does not exist`);
      if (path === '/static/js/app-sdk.js') assert.equal(v, shellSdk, `${name} imports app-sdk.js at ?v=${v}, main.js at ?v=${shellSdk}`);
      if (path.endsWith('board.css')) cssVersions.add(v);
    }
  }
  assert.equal(cssVersions.size, 1, 'index.html and thread.js link board.css at the same ?v=');
});
