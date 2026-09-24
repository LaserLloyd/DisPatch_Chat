// The theme set, pinned down.
//
// DisPatch's colours come from the shared drop-in theme folder static/ui-theme/:
// ui-theme.js (the blocking runtime and its registry of themes), ui-theme.css
// (every theme's contract tokens) and adapters/dispatch-compat.css (DisPatch's
// own token names mapped onto them). DisPatch's settings for it are the data-*
// on the runtime's <script> tag in index.html. static/theme.css keeps only
// what the theme folder does not own. Component CSS still reads the legacy names (--bg-primary,
// --user-bubble, --md-em, …), so what matters is what those names RESOLVE to
// on <html> for each theme — and nothing in any one file says that on its own.
// helpers/theme-resolve.js replays the cascade from source to find out.
//
// The move onto the package promised that the six themes DisPatch already
// shipped look the same afterwards, with two documented exceptions: Purple
// adopts the contract's Purple, and Paper's --text-muted is the contract's
// (slightly darker) tertiary ink. fixtures/palette-parity-2026-09.json holds
// every legacy token's value per palette as it was BEFORE the move, generated
// from the old theme.css, and the parity tests below hold the new cascade to
// it. A value that drifts in a core theme fails here, not in someone's eyes.

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import {
  registry, manifest, revision, resolveAll, resolveToken, canon, parseColor, contrast,
} from './helpers/theme-resolve.js';

const HERE = dirname(fileURLToPath(import.meta.url));
const STATIC = join(HERE, '..', 'static');

const stripComments = (s) => s.replace(/\/\*[\s\S]*?\*\//g, '');
const read = (f) => readFileSync(join(STATIC, f), 'utf8');
const THEME_CSS = stripComments(read('theme.css'));
const APP = stripComments(read('app.css'));
const DASH = stripComments(read('dashboard.css'));
const UI_CSS = stripComments(read('ui-theme/ui-theme.css'));
const COMPAT_CSS = stripComments(read('ui-theme/adapters/dispatch-compat.css'));
const THEME_JS = read('js/theme.js');
const HTML = read('index.html').replace(/<!--[\s\S]*?-->/g, '');   // comments discuss <script> by name
const FIXTURE = JSON.parse(readFileSync(join(HERE, 'fixtures', 'palette-parity-2026-09.json'), 'utf8'));

const THEMES = registry();
const SLUGS = THEMES.map((t) => t.slug);
const CORE = ['glacier', 'midnight-gold', 'forest', 'paper', 'daylight', 'purple'];

// Three legacy names clashed with contract names of a different meaning, so
// component CSS reads them under new names; --ring was an unused alias.
const RENAMED = {
  '--accent-muted': '--accent-wash',
  '--border': '--border-strong',
  '--scrollbar-thumb': '--border-light',
  '--ring': '--focus',
};
const LEGACY = Object.keys(FIXTURE.palettes.glacier.tokens);
const now = (legacy) => RENAMED[legacy] || legacy;

// Colour roles DisPatch owns (theme.css) or that component CSS now reads
// straight from the contract. Every theme must resolve all of them.
const EXTRA = ['--on-accent', '--danger', '--code-card-bg', '--code-card-chrome',
  '--code-block-text', '--on-error-fill', '--on-warning-fill', '--notify', '--dot-idle'];

// Purple is the one theme DisPatch deliberately re-bases onto the contract.
// These are the tokens where that shows, and ONLY these may differ.
const PURPLE_EXCEPTIONS = new Set([
  '--bg-secondary', '--bg-input', '--bg-sidebar', '--tint-subtle',
  '--text-secondary', '--text-muted', '--border', '--border-light',
  '--scrollbar-thumb', '--user-bubble', '--bot-bubble',
]);


// --- the set -----------------------------------------------------------------

test('the runtime offers the ten themes, glacier first-class default', () => {
  const m = manifest();
  assert.equal(m.default, 'glacier');
  assert.equal(m.storageKey, 'dispatch-palette',
    'the storage key must stay dispatch-palette, or every device forgets its theme');
  assert.equal(m.themes.length, 10);
  for (const slug of m.themes) assert.ok(SLUGS.includes(slug), `${slug} is enabled but not registered`);
  for (const slug of CORE) assert.ok(m.themes.includes(slug), `core theme ${slug} is not enabled`);
  assert.match(revision() || '', /^\d{4}-\d{2}-\d{2}$/, 'ui-theme.js carries no revision date');
});

test('ui-theme.css defines a block for every enabled theme', () => {
  for (const slug of manifest().themes) {
    if (slug === 'purple') {
      assert.match(UI_CSS, /:root,\s*\[data-palette\]\s*\{/, 'the Purple base block (:root, [data-palette]) is missing');
    } else {
      assert.match(UI_CSS, new RegExp(`\\[data-palette="${slug}"\\]\\s*\\{`), `no [data-palette="${slug}"] block`);
    }
  }
});

test('every theme declares the color-scheme its registry entry states', () => {
  for (const t of THEMES) {
    assert.equal(resolveToken(t.slug, 'color-scheme'), t.colorScheme,
      `${t.slug}: color-scheme does not match the registry — native controls and the light-dark() pairs would pick the wrong side`);
  }
});

// --- parity with the pre-package palettes -----------------------------------

for (const slug of CORE) {
  test(`parity: ${slug} resolves every legacy token to its pre-package value`, () => {
    const want = FIXTURE.palettes[slug].tokens;
    const got = resolveAll(slug, LEGACY.map(now));
    const diffs = [];
    for (const name of LEGACY) {
      if (slug === 'purple' && PURPLE_EXCEPTIONS.has(name)) continue;
      if (slug === 'paper' && name === '--text-muted') continue;
      const a = canon(want[name]);
      const b = got[now(name)];
      if (a !== b) diffs.push(`${name}${RENAMED[name] ? ` (now ${now(name)})` : ''}: ${a} -> ${b}`);
    }
    assert.deepEqual(diffs, [], `\n${slug} drifted from the parity fixture:\n${diffs.join('\n')}`);
  });
}

test('parity: the documented exceptions are real, and still the only ones', () => {
  // An exception list that no longer excepts anything is a hole waiting for a
  // regression, so each entry must actually differ.
  const purple = resolveAll('purple', LEGACY.map(now));
  for (const name of PURPLE_EXCEPTIONS) {
    assert.notEqual(purple[now(name)], canon(FIXTURE.palettes.purple.tokens[name]),
      `purple ${name} no longer differs from the fixture — drop it from PURPLE_EXCEPTIONS`);
  }
  const paperMuted = resolveToken('paper', '--text-muted');
  assert.notEqual(paperMuted, canon(FIXTURE.palettes.paper.tokens['--text-muted']));
  // "Slightly darker", and still an AA ink on the Paper ground.
  const bg = resolveToken('paper', '--bg-primary');
  assert.ok(contrast(paperMuted, bg) > contrast(FIXTURE.palettes.paper.tokens['--text-muted'], bg),
    'Paper --text-muted was meant to get DARKER');
});

test('the hard-coded colours the sweep replaced keep their core values', () => {
  const imp = FIXTURE.implicit;
  for (const slug of CORE) {
    const r = resolveAll(slug, ['--on-accent', '--code-card-bg', '--code-card-chrome',
      '--on-error-fill', '--on-warning-fill', '--danger', '--success', '--warning']);
    assert.equal(r['--on-accent'], canon(imp['--on-accent']), `${slug} --on-accent`);
    assert.equal(r['--code-card-bg'], canon(imp['--code-card-bg']), `${slug} --code-card-bg`);
    assert.equal(r['--code-card-chrome'], canon(imp['--code-card-chrome']), `${slug} --code-card-chrome`);
    assert.equal(r['--on-error-fill'], canon(imp['--on-error-fill']), `${slug} --on-error-fill`);
    assert.equal(r['--on-warning-fill'], canon(imp['--on-warning-fill']), `${slug} --on-warning-fill`);
    // The washes and the dashboard's dark severity inks were literals of these.
    assert.equal(r['--danger'], canon(imp['danger-rgb (rgba(248,113,113,a) washes)']), `${slug} --danger`);
    assert.equal(r['--success'], '#34d399', `${slug} --success`);
    assert.equal(r['--warning'], '#fbbf24', `${slug} --warning`);
  }
});

// --- every theme, every legacy token ----------------------------------------

test('all ten themes resolve every legacy token to a concrete value', () => {
  const bad = [];
  for (const slug of SLUGS) {
    const got = resolveAll(slug, [...LEGACY.map(now), ...EXTRA]);
    for (const [name, v] of Object.entries(got)) {
      if (v === null || /UNRESOLVED|var\(|color-mix\(/.test(v)) bad.push(`${slug} ${name}: ${v}`);
    }
  }
  assert.deepEqual(bad, [], '\n' + bad.join('\n'));
});

test('night themes light no blue sub-pixel through any legacy token', () => {
  const night = THEMES.filter((t) => t.contrastProfile === 'night');
  assert.ok(night.length >= 1, 'expected at least one night theme (Night Red)');
  const hits = [];
  for (const t of night) {
    const got = resolveAll(t.slug, [...LEGACY.map(now), ...EXTRA]);
    for (const [name, v] of Object.entries(got)) {
      for (const m of v.matchAll(/#[0-9a-f]{6}|rgba\([^)]*\)/g)) {
        const c = parseColor(m[0]);
        if (c && c[2] > 0 && c[3] > 0) hits.push(`${t.slug} ${name}: ${m[0]}`);
      }
    }
  }
  assert.deepEqual(hits, [], '\n' + hits.join('\n'));
});

// --- the notification contract (all ten) -------------------------------------
//
// --notify is the ONLY thing whose job is to be noticed, --dot-idle the only
// thing whose job is not to be. Solved as a pair, per theme, against the
// sidebar the dots sit on:
//   --notify vs the sidebar   >= 4.5:1   (impossible to miss)
//   --dot-idle vs the sidebar <= 3:1     (present, unremarkable)
//   --notify vs --dot-idle    >= 3:1     (the two states never blur)

test('every theme solves the notification pair against its own sidebar', () => {
  const failures = [];
  for (const slug of SLUGS) {
    const r = resolveAll(slug, ['--bg-sidebar', '--notify', '--dot-idle']);
    const nb = contrast(r['--notify'], r['--bg-sidebar']);
    const ib = contrast(r['--dot-idle'], r['--bg-sidebar']);
    const ni = contrast(r['--notify'], r['--dot-idle']);
    if (nb < 4.5) failures.push(`${slug}: --notify only ${nb.toFixed(2)}:1 on the sidebar (need >= 4.5)`);
    if (ib > 3.0) failures.push(`${slug}: --dot-idle is ${ib.toFixed(2)}:1 on the sidebar (need <= 3.0)`);
    if (ni < 3.0) failures.push(`${slug}: --notify vs --dot-idle only ${ni.toFixed(2)}:1 (need >= 3.0)`);
  }
  assert.deepEqual(failures, [], '\n' + failures.join('\n'));
});

test('the thinking dot is visible in every theme, including the light ones', () => {
  const rule = /\.bot-status-dot\.thinking\s*\{([^}]*)\}/.exec(APP);
  assert.ok(rule, '.bot-status-dot.thinking rule not found in app.css');
  assert.ok(!/var\(--warning\)/.test(rule[1]),
    'the thinking dot is back on the raw --warning, which is invisible on the light themes');
  const failures = [];
  for (const slug of SLUGS) {
    const r = resolveAll(slug, ['--bg-sidebar', '--warning-text']);
    const ratio = contrast(r['--warning-text'], r['--bg-sidebar']);
    if (ratio < 3.0) failures.push(`${slug}: --warning-text only ${ratio.toFixed(2)}:1 on the sidebar`);
  }
  assert.deepEqual(failures, [], '\n' + failures.join('\n'));
});

test('the unread indicator does not rely on colour alone', () => {
  const rule = /\.bot-status-dot\.unread\s*\{([^}]*)\}/.exec(APP);
  assert.ok(rule, '.bot-status-dot.unread rule not found in app.css');
  assert.match(rule[1], /--notify/, 'the unread dot no longer uses --notify');
  assert.match(rule[1], /\bwidth:/, 'the unread dot must also change size, not just colour');
});

test('no indicator still reaches for the retired token pair', () => {
  for (const sel of ['.bot-status-dot.unread', '.thread-unread-dot']) {
    const rule = new RegExp(`\\${sel}\\s*\\{([^}]*)\\}`).exec(APP);
    assert.ok(rule, `${sel} rule not found in app.css`);
    assert.ok(!/--accent-hover/.test(rule[1]),
      `${sel} is back on --accent-hover, which is tuned for button fills and fails the notification contract`);
  }
});

test('no status indicator paints a raw status token', () => {
  // --success / --error / --danger / --warning are authored against a dark
  // surface; only the *-text variants adapt to light grounds. A dot or tick
  // painted with a raw one is invisible on Paper or Daylight.
  const RAW = /var\(--(?:success|error|danger|warning)[,)]/;
  const MARK = /(^|[\s,])\.[\w-]*(?:dot|drop-icon|pip|badge-state)\b/;
  const offenders = [];
  for (const m of APP.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    const selector = m[1].trim();
    if (!MARK.test(selector)) continue;
    for (const decl of m[2].split(';')) {
      if (!/^\s*(background|background-color|color|box-shadow|border-color)\s*:/.test(decl)) continue;
      if (RAW.test(decl)) offenders.push(`${selector.slice(0, 70)} -> ${decl.trim().slice(0, 80)}`);
    }
  }
  assert.deepEqual(offenders, [], '\nUse the --*-text variant for these status marks:\n' + offenders.join('\n'));
});

// --- theme.css is DisPatch-only ----------------------------------------------

test('theme.css holds no theme: no palette blocks, no light/dark mechanism', () => {
  assert.ok(!/\[data-palette/.test(THEME_CSS), 'theme.css names a palette — themes live in ui-theme.css');
  assert.ok(!/light-dark\s*\(/.test(THEME_CSS), 'theme.css uses light-dark()');
  assert.ok(!/\[data-theme/.test(THEME_CSS), 'theme.css keys on data-theme');
});

test('theme.css never redeclares a token the package owns', () => {
  const pkg = new Set([...`${UI_CSS}\n${COMPAT_CSS}`.matchAll(/(--[\w-]+)\s*:/g)].map((m) => m[1]));
  const mine = [...THEME_CSS.matchAll(/(--[\w-]+)\s*:/g)].map((m) => m[1]);
  const clash = [...new Set(mine.filter((n) => pkg.has(n)))];
  assert.deepEqual(clash, [], `theme.css redeclares package tokens: ${clash.join(', ')} — ui-theme.css loads later and wins, so these are dead or, worse, a second source of truth`);
});

test('component CSS declares no token on <html>', () => {
  // theme-resolve.js models only theme.css + ui-theme.css. A :root token in
  // app.css or dashboard.css would sit outside that model (and outside the
  // package), so it is not allowed to exist.
  for (const [name, css] of [['app.css', APP], ['dashboard.css', DASH]]) {
    for (const m of css.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
      const sel = m[1].trim();
      if (!/^(:root|html)(\s*,|\s*$)/.test(sel)) continue;
      assert.ok(!/--[\w-]+\s*:/.test(m[2]), `${name}: "${sel}" declares a custom property on <html>`);
    }
  }
});

test('the contract names that clash with DisPatch meanings are not read raw', () => {
  // The contract owns --border, --accent-muted and --scrollbar-thumb with a
  // different meaning; component CSS reads --border-strong / --accent-wash /
  // --border-light for DisPatch's own versions.
  for (const [name, css] of [['app.css', APP], ['dashboard.css', DASH]]) {
    for (const t of ['--border', '--accent-muted', '--scrollbar-thumb']) {
      assert.ok(!new RegExp(`var\\(${t}[,)]`).test(css), `${name} reads var(${t})`);
    }
  }
});

test('every custom property a rule reads is defined somewhere', () => {
  // `var(--accent-soft, …)` shipped for months with no --accent-soft ever
  // declared — the fallback hid it. The package's tokens count as defined.
  const all = `${THEME_CSS}\n${APP}\n${DASH}\n${UI_CSS}\n${COMPAT_CSS}`;
  const defined = new Set([...all.matchAll(/(--[\w-]+)\s*:/g)].map((m) => m[1]));
  for (const f of readdirSync(join(STATIC, 'js')).filter((n) => n.endsWith('.js'))) {
    const js = read(`js/${f}`);
    for (const m of js.matchAll(/setProperty\(\s*['"`](--[\w-]+)/g)) defined.add(m[1]);
    for (const m of js.matchAll(/(--[\w-]+)\s*:/g)) defined.add(m[1]);
  }
  const used = new Set([...`${THEME_CSS}\n${APP}\n${DASH}`.matchAll(/var\(\s*(--[\w-]+)/g)].map((m) => m[1]));
  const missing = [...used].filter((name) => !defined.has(name)).sort();
  assert.deepEqual(missing, [], '\nThese custom properties are read but never declared:\n' + missing.join('\n'));
});

// --- the markup and the picker -----------------------------------------------

test('ui-theme.js is the first script in <head>, blocking, from the drop-in folder', () => {
  const head = HTML.slice(0, HTML.indexOf('</head>'));
  const scripts = [...head.matchAll(/<script\b([^>]*)>/g)];
  assert.ok(scripts.length, 'no <script> in <head>');
  const first = scripts[0][1];
  assert.match(first, /src="\/static\/ui-theme\/ui-theme\.js"/,
    `the first <head> script must be ui-theme/ui-theme.js, found: <script${first}>`);
  assert.ok(!/\b(defer|async|type="module")/.test(first),
    'ui-theme.js must be BLOCKING — deferred, it paints the theme after first paint');
});

test('the theme folder loads after app.css and dashboard.css, adapter last, with no ?v=', () => {
  const at = (re) => { const m = re.exec(HTML); return m ? m.index : -1; };
  const ui = at(/href="\/static\/ui-theme\/ui-theme\.css"/);
  const compat = at(/href="\/static\/ui-theme\/adapters\/dispatch-compat\.css"/);
  assert.ok(ui > 0, 'index.html does not link ui-theme/ui-theme.css');
  assert.ok(compat > ui, 'adapters/dispatch-compat.css must load right after ui-theme.css');
  assert.ok(ui > at(/href="\/static\/app\.css/), 'ui-theme.css must load after app.css');
  assert.ok(ui > at(/href="\/static\/dashboard\.css/), 'ui-theme.css must load after dashboard.css');
  assert.ok(ui > at(/href="\/static\/theme\.css/), 'ui-theme.css must load after theme.css');
  // Replacing the folder is the whole update, so its URLs never carry a
  // version: the server revalidates them instead (main.py, Cache-Control).
  assert.ok(!/\/static\/ui-theme\/[^"]*\?v=/.test(HTML), 'a ui-theme/ URL carries ?v= — the folder is served no-cache instead');
  assert.ok(!/\/static\/ui-theme\.(js|css)/.test(HTML), 'index.html still loads the old loose ui-theme files');
});

test('the server revalidates the theme folder on every load', () => {
  const MAIN = readFileSync(join(HERE, '..', '..', 'backend', 'app', 'main.py'), 'utf8');
  assert.match(MAIN, /path\.startswith\("\/static\/ui-theme\/"\)/,
    'main.py no longer serves /static/ui-theme/ no-cache — a replaced folder would sit behind a stale browser cache');
});

test('the service worker precaches the theme folder files the page loads', () => {
  const SW = read('sw.js');
  for (const f of ['ui-theme/ui-theme.js', 'ui-theme/ui-theme.css', 'ui-theme/adapters/dispatch-compat.css']) {
    assert.ok(SW.includes(`'/static/${f}'`), `sw.js SHELL lacks /static/${f}`);
  }
});

test('the pre-paint inline script no longer knows about themes', () => {
  const inline = [...HTML.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g)].map((m) => m[1]).join('\n');
  assert.ok(!/dispatch-palette|data-palette|glacier/.test(inline),
    'an inline script still stamps the palette — ui-theme.js owns that, and two writers race');
  assert.ok(!/\bdata-theme=/.test(HTML), 'index.html markup stamps data-theme');
});

test('js/theme.js renders the runtime\'s list and writes no storage of its own', () => {
  assert.match(THEME_JS, /UITheme/, 'theme.js does not use the theme runtime');
  assert.match(THEME_JS, /\.list\(\)/, 'the gallery is not built from UITheme.list()');
  assert.match(THEME_JS, /\.onChange\(/, 'the selection does not follow UITheme.onChange');
  assert.ok(!/localStorage/.test(THEME_JS), 'theme.js touches localStorage — the runtime owns the pick');
  assert.ok(!/PALETTES|DEFAULT_PALETTE/.test(THEME_JS), 'theme.js still carries its own palette list');
});

test('the theme revision line is wired into the Theme pane', () => {
  assert.match(HTML, /id="theme-revision"[^>]*data-i18n="settings\.theme_revision"/);
  assert.match(THEME_JS, /revision\(\)/);
});

test('the theme folder files are free-standing and carry their revision', () => {
  // The generated files identify themselves only by a revision date.
  for (const f of ['ui-theme/ui-theme.js', 'ui-theme/ui-theme.css', 'ui-theme/adapters/dispatch-compat.css']) {
    assert.match(read(f), /^\/\* Theme revision \d{4}-\d{2}-\d{2} — [^\n]*Generated file, do not edit by hand/, `${f} header`);
    assert.ok(!/https?:\/\/|\.git\b/.test(read(f).split('\n').slice(0, 3).join('\n')), `${f} header names a source`);
  }
});
