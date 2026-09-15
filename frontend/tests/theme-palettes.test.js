// The palette set, pinned down across its three declarations.
//
// DisPatch has no light/dark switch any more: appearance is one fixed palette
// selected by `data-palette` on <html>. That palette id is written down in
// THREE places that cannot import from each other:
//
//   1. theme.css            — the [data-palette] blocks and the default block
//   2. js/theme.js          — PALETTES (the picker) + DEFAULT_PALETTE
//   3. index.html           — the no-FOUC script's list, which runs before any
//                             module and therefore cannot read js/theme.js
//
// Drift between them is silent and ugly in a specific way: a palette missing
// from (3) flashes the default for one frame before theme.js re-stamps it, a
// palette missing from (2) simply cannot be picked, and a palette that omits a
// token in theme.css inherits the default's value for it — which is how a
// forest theme ends up with a lavender focus ring. None of those throw.
//
// So this file derives all three from source and compares them, and pins the
// two invariants the CSS depends on: every palette defines the SAME complete
// token set, and no palette redefines a structural token (a radius, a font
// stack, a layout width) that must stay identical across skins.

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const STATIC = join(HERE, '..', 'static');

const stripComments = (s) => s.replace(/\/\*[\s\S]*?\*\//g, '');
const CSS = stripComments(readFileSync(join(STATIC, 'theme.css'), 'utf8'));
const THEME_JS = readFileSync(join(STATIC, 'js', 'theme.js'), 'utf8');
const HTML = readFileSync(join(STATIC, 'index.html'), 'utf8');

/** Every `selector { body }` pair, innermost wins. The @media wrapper around
 *  the responsive width override is skipped by construction: `[^{}]*` cannot
 *  cross the inner `{`, so the inner `:root` rule is what comes back — which is
 *  what we want, since that is the declaration that matters. */
function blocks(src) {
  return [...src.matchAll(/([^{}]+)\{([^{}]*)\}/g)]
    .map((m) => ({ selector: m[1].trim(), body: m[2] }));
}

/** Custom properties DECLARED in a block (values are not inspected, so a
 *  var() reference inside one cannot be mistaken for a declaration). */
function declared(body) {
  return [...body.matchAll(/(--[a-z0-9-]+)\s*:/g)].map((m) => m[1]);
}

const ALL = blocks(CSS);
const paletteBlocks = ALL
  .map((b) => {
    const m = /\[data-palette="([a-z-]+)"\]/.exec(b.selector);
    return m ? { id: m[1], selector: b.selector, body: b.body } : null;
  })
  .filter(Boolean);

// The structural block is the one whose selector is EXACTLY `:root`. The
// default palette's selector is `:root, [data-palette="glacier"]`, which is a
// different string on purpose — it is a palette, not the structural block.
const structural = new Set(
  ALL.filter((b) => b.selector === ':root').flatMap((b) => declared(b.body)),
);
const parts = (b) => new Set(declared(b.body));

// --- (1) the CSS side ------------------------------------------------------

test('theme.css declares every palette exactly once, default first', () => {
  const ids = paletteBlocks.map((p) => p.id);
  assert.deepEqual(ids, [...new Set(ids)], `a palette block is declared twice: ${ids}`);
  assert.ok(ids.length >= 6, `expected the shipped palette set, found ${ids.length}: ${ids}`);
  // The default block must be FIRST: every palette selector has the same
  // specificity, so source order is what lets an explicit palette override the
  // default when <html> carries the attribute.
  assert.equal(ids[0], 'glacier', 'the default (glacier) palette must be the first block');
  assert.match(paletteBlocks[0].selector, /:root/,
    'the default palette must also match :root, or a page with no data-palette has no skin');
});

test('every palette defines the same complete token set', () => {
  const [first, ...rest] = paletteBlocks;
  const reference = parts(first);
  assert.ok(reference.size > 25, `only ${reference.size} tokens in ${first.id} — inventory looks broken`);
  for (const p of rest) {
    const have = parts(p);
    const missing = [...reference].filter((t) => !have.has(t));
    const extra = [...have].filter((t) => !reference.has(t));
    assert.deepEqual({ missing, extra }, { missing: [], extra: [] },
      `[data-palette="${p.id}"] does not define the same tokens as "${first.id}".\n`
      + 'A token missing here inherits the default palette\'s value, which is\n'
      + 'exactly the half-themed result this test exists to prevent:');
  }
});

test('palettes define the thematic tokens and nothing structural', () => {
  // Structural = declared on the bare `:root` block (radii, layout widths,
  // fonts, the fluid type scale, motion, and the two aliases). A palette that
  // redefines one of these makes a skin that also moves layout, which the
  // design deliberately does not allow.
  for (const p of paletteBlocks) {
    const clash = [...parts(p)].filter((t) => structural.has(t));
    assert.deepEqual(clash, [],
      `[data-palette="${p.id}"] redefines structural token(s) ${clash.join(', ')} — `
      + 'move them to the :root block; a palette changes colour, not geometry');
  }
  // …and the thematic set really is complete: these are the tokens the app's
  // components read, and the list is deliberately explicit rather than derived,
  // so deleting one from every palette at once still fails here.
  const required = [
    '--bg-primary', '--bg-secondary', '--bg-tertiary', '--bg-elevated', '--bg-hover',
    '--bg-input', '--bg-sidebar', '--tint-subtle', '--code-inline',
    '--text-primary', '--text-secondary', '--text-muted',
    '--accent', '--accent-hover', '--accent-muted', '--accent-text', '--focus', '--accent-grad',
    '--border', '--border-light', '--user-bubble', '--bot-bubble',
    '--md-quote', '--md-quote-text', '--md-em', '--md-strong', '--md-mark-bg',
    '--success', '--success-text', '--error', '--error-text', '--warning', '--warning-text',
    '--shadow-sm', '--shadow-md', '--shadow-lg',
  ];
  for (const p of paletteBlocks) {
    const have = parts(p);
    const missing = required.filter((t) => !have.has(t));
    assert.deepEqual(missing, [], `[data-palette="${p.id}"] is missing: ${missing.join(', ')}`);
  }
});

test('theme.css carries no light-dark() and no data-theme selector', () => {
  // Both are the retired mechanism. light-dark() still appears in app.css and
  // dashboard.css for two narrow, colour-scheme-driven cases (minimal-avatar
  // name tints, host dashboard severities) — those resolve per palette through
  // each block's color-scheme. It must not come back HERE, where a palette
  // value that quietly follows the OS is precisely the old behaviour.
  assert.ok(!/light-dark\s*\(/.test(CSS), 'theme.css still uses light-dark()');
  assert.ok(!/\[data-theme/.test(CSS), 'theme.css still has a [data-theme] selector');
});

test('each palette declares the color-scheme matching its badge', () => {
  const jsModes = new Map(
    [...THEME_JS.matchAll(/\{\s*id:\s*'([a-z-]+)',\s*name:\s*'[^']*',\s*mode:\s*'(dark|light)'/g)]
      .map((m) => [m[1], m[2]]),
  );
  for (const p of paletteBlocks) {
    const m = /color-scheme\s*:\s*(dark|light)/.exec(p.body);
    assert.ok(m, `[data-palette="${p.id}"] declares no color-scheme — native `
      + 'scrollbars and form controls will not match the skin');
    assert.equal(m[1], jsModes.get(p.id),
      `[data-palette="${p.id}"] says color-scheme: ${m[1]} but js/theme.js badges it `
      + `as ${jsModes.get(p.id)}`);
  }
});

// --- (2) js/theme.js -------------------------------------------------------

test('js/theme.js lists exactly the palettes theme.css defines', () => {
  const ids = [...THEME_JS.matchAll(/id:\s*'([a-z-]+)'/g)].map((m) => m[1]);
  assert.deepEqual(ids.sort(), paletteBlocks.map((p) => p.id).sort(),
    'PALETTES in js/theme.js and the [data-palette] blocks in theme.css disagree');
  const def = /const DEFAULT_PALETTE = '([a-z-]+)'/.exec(THEME_JS);
  assert.ok(def, 'js/theme.js no longer declares DEFAULT_PALETTE');
  assert.equal(def[1], paletteBlocks[0].id, 'DEFAULT_PALETTE is not the first CSS palette');
  // The default must be reachable through the picker, or the CSS default is a
  // palette no user can return to.
  assert.ok(ids.includes(def[1]), 'DEFAULT_PALETTE is not in PALETTES');
});

// --- (3) index.html --------------------------------------------------------

test('the no-FOUC palette list matches js/theme.js', () => {
  // Three copies of one list (HTML script, theme.js, theme.css). The HTML one
  // cannot import the other two: it runs before any module loads, which is the
  // whole point of a no-FOUC script.
  const m = /var P = \[([^\]]*)\]/.exec(HTML);
  assert.ok(m, 'the no-FOUC palette list in index.html changed shape');
  const htmlIds = [...m[1].matchAll(/'([a-z-]+)'/g)].map((x) => x[1]);
  const jsIds = [...THEME_JS.matchAll(/id:\s*'([a-z-]+)'/g)].map((x) => x[1]);
  assert.deepEqual(htmlIds.sort(), jsIds.sort(),
    'the no-FOUC palette list in index.html and PALETTES in js/theme.js disagree');
  // …and its fallback is the same default, or a first visit paints one skin
  // and theme.js immediately swaps to another.
  const fallback = /P\.indexOf\(p\)\s*>=\s*0\s*\?\s*p\s*:\s*'([a-z-]+)'/.exec(HTML);
  assert.ok(fallback, 'the no-FOUC palette fallback changed shape');
  const def = /const DEFAULT_PALETTE = '([a-z-]+)'/.exec(THEME_JS);
  assert.ok(def, 'js/theme.js no longer declares DEFAULT_PALETTE');
  assert.equal(fallback[1], def[1], 'the no-FOUC fallback is not DEFAULT_PALETTE');
});

test('the dark/light switch left no trace in the markup', () => {
  assert.ok(!/data-theme/.test(HTML), 'index.html still stamps data-theme');
  assert.ok(!/dispatch-theme/.test(HTML), 'index.html still reads dispatch-theme');
  assert.ok(!/theme\.dark|theme\.light/.test(HTML), 'index.html still references the old theme labels');
});
