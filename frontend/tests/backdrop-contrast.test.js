// Feature 23 (scene backdrop), tier 1 — the contrast proof.
//
// The message pane's backdrop (app.css, `.messages`) is a deterministic
// colour tint derived from each palette's OWN --bg-primary with CSS relative
// colour syntax: `oklch(from var(--bg-primary) l <chroma> var(--backdrop-hue,
// h))`. `l` (lightness) is the KEYWORD, copied unchanged from the origin
// colour; only chroma and hue move. That is supposed to be what keeps every
// palette's promised contrast intact — this file is what actually PROVES it,
// the way the brief asked for: resolve every theme from source (ui-theme.css
// through helpers/theme-resolve.js), derive
// the backdrop this exact CSS rule would produce for hues across the full
// range, and assert the text that is actually painted directly on that pane
// (no bubble of its own underneath it) still clears WCAG AA and does not
// fall meaningfully below the palette's own baseline.
//
// Follows theme-palettes.test.js's approach: read the real files, derive
// everything from them, assert invariants — never a hand-copied palette
// table that can drift from the CSS it is supposed to be checking.
//
// THE PROOF: before this feature, `.messages` had no `background-color` rule
// at all and app.css contained no `oklch(from …)` relative-colour syntax, so
// this file's own extraction of --backdrop-chroma-in-CSS below finds nothing
// and every test here fails outright — not because contrast is bad, but
// because the mechanism this file measures does not exist yet.

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import { registry, resolveAll } from './helpers/theme-resolve.js';

const HERE = dirname(fileURLToPath(import.meta.url));
const STATIC = join(HERE, '..', 'static');

const stripComments = (s) => s.replace(/\/\*[\s\S]*?\*\//g, '');
const APP_CSS = stripComments(readFileSync(join(STATIC, 'app.css'), 'utf8'));

// --------------------------------------------------------------------------- #
// 1. Resolve every theme's tokens from source. Since the move onto the
//    vendored theme package the values live in ui-theme.css (contract tokens
//    plus the adapter onto DisPatch's names), so the cascade is replayed by
//    helpers/theme-resolve.js — the same resolver the palette tests use —
//    rather than read out of one file.
// --------------------------------------------------------------------------- #

// Three text tokens that land directly on `.messages`' own background with
// no bubble underneath them, so the backdrop tint is literally what they sit
// on: date-sep (--text-muted), the empty-state prompt (--text-secondary),
// and --text-primary as the strongest case any in-pane prose would use.
const PALETTES = registry().map((t) => {
  const r = resolveAll(t.slug, ['--bg-primary', '--text-primary', '--text-secondary', '--text-muted']);
  return {
    id: t.slug,
    night: t.contrastProfile === 'night',
    bgPrimary: r['--bg-primary'],
    textPrimary: r['--text-primary'],
    textSecondary: r['--text-secondary'],
    textMuted: r['--text-muted'],
  };
});
const STANDARD = PALETTES.filter((p) => !p.night);
const NIGHT = PALETTES.filter((p) => p.night);
const HEX6 = /^#[0-9a-f]{6}$/;

test('setup: found all ten themes with opaque hex tokens', () => {
  assert.equal(PALETTES.length, 10, `expected 10 themes, found ${PALETTES.length}`);
  for (const p of PALETTES) {
    for (const k of ['bgPrimary', 'textPrimary', 'textSecondary', 'textMuted']) {
      assert.match(p[k] || '', HEX6, `${p.id}: ${k} did not resolve to an opaque hex (${p[k]})`);
    }
  }
});

// --------------------------------------------------------------------------- #
// 2. Pull the ACTUAL backdrop rule out of app.css, not a hand-copied number —
//    if the chroma constant ever changes, this file re-derives against the
//    new value instead of silently checking a stale one.
// --------------------------------------------------------------------------- #

const SUPPORTS_RE = /@supports\s*\(color:\s*oklch\(from red l c h\)\)\s*\{\s*\.messages\s*\{([\s\S]*?)\}\s*\}/;
const OKLCH_RULE_RE = /background-color:\s*oklch\(from var\(--bg-primary\)\s+l\s+([\d.]+)\s+var\(--backdrop-hue,\s*h\)\)/;

test('setup: app.css declares the relative-colour backdrop rule on .messages', () => {
  const supportsBlock = SUPPORTS_RE.exec(APP_CSS);
  assert.ok(supportsBlock,
    '.messages has no @supports(oklch relative-colour) block in app.css — '
    + 'the backdrop mechanism this file tests does not exist');
  const rule = OKLCH_RULE_RE.exec(supportsBlock[1]);
  assert.ok(rule,
    'the @supports block does not declare `background-color: oklch(from '
    + 'var(--bg-primary) l <chroma> var(--backdrop-hue, h))` — check the '
    + 'exact shape (lightness must be the bare `l` keyword, not a variable, '
    + 'or the palette\'s promised contrast is no longer guaranteed)');
});

const CHROMA = Number((OKLCH_RULE_RE.exec(SUPPORTS_RE.exec(APP_CSS)?.[1] ?? '') ?? [])[1] ?? NaN);

test('setup: a plain background-color fallback precedes the @supports block', () => {
  // Order matters: the plain rule must come first in the SAME .messages
  // block that opens this file's excerpt, so a browser with no relative-
  // colour support (or a page where JS never set --backdrop-hue at all)
  // still paints a full, valid background — never "no rule matched".
  const plain = /\.messages\s*\{[^}]*background-color:\s*var\(--bg-primary\)/;
  assert.ok(plain.test(APP_CSS),
    '.messages has no plain `background-color: var(--bg-primary)` fallback '
    + 'ahead of the @supports block');
});

// --------------------------------------------------------------------------- #
// 3. OKLab/OKLCH <-> sRGB and WCAG contrast — the same public formulas a
//    browser uses for `oklch()` (Björn Ottosson's OKLab) and for contrast
//    (WCAG 2.x relative luminance). No dependency: this is the one place a
//    committed dev dependency would earn its keep least — six palettes and a
//    hue sweep is a few hundred multiplications.
// --------------------------------------------------------------------------- #

function hexToSrgb(hex) {
  const h = hex.replace('#', '');
  return [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16) / 255);
}

function srgbToLinear(c) {
  return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
}

function linearToSrgb(c) {
  const clamped = Math.min(1, Math.max(0, c));
  return clamped <= 0.0031308 ? clamped * 12.92 : 1.055 * clamped ** (1 / 2.4) - 0.055;
}

function srgbToOklab([r, g, b]) {
  const [lr, lg, lb] = [r, g, b].map(srgbToLinear);
  const l = 0.4122214708 * lr + 0.5363325363 * lg + 0.0514459929 * lb;
  const m = 0.2119034982 * lr + 0.6806995451 * lg + 0.1073969566 * lb;
  const s = 0.0883024619 * lr + 0.2817188376 * lg + 0.6299787005 * lb;
  const cbrt = (x) => (x >= 0 ? x ** (1 / 3) : -((-x) ** (1 / 3)));
  const [l_, m_, s_] = [cbrt(l), cbrt(m), cbrt(s)];
  return [
    0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_,
    1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_,
    0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_,
  ];
}

function oklabToSrgb([L, a, b]) {
  const l_ = L + 0.3963377774 * a + 0.2158037573 * b;
  const m_ = L - 0.1055613458 * a - 0.0638541728 * b;
  const s_ = L - 0.0894841775 * a - 1.2914855480 * b;
  const [l, m, s] = [l_ ** 3, m_ ** 3, s_ ** 3];
  return [
    linearToSrgb(4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s),
    linearToSrgb(-1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s),
    linearToSrgb(-0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s),
  ];
}

// The exact operation `oklch(from var(--bg-primary) l <chroma> <hue>)`
// performs: take the origin's OWN L (lightness, unchanged — the keyword),
// substitute chroma/hue, convert back to sRGB. Out-of-gamut results clamp
// (linearToSrgb), matching what a browser does.
function backdropRgb(bgHex, chroma, hueDeg) {
  const [L] = srgbToOklab(hexToSrgb(bgHex));
  const hr = (hueDeg * Math.PI) / 180;
  return oklabToSrgb([L, chroma * Math.cos(hr), chroma * Math.sin(hr)]);
}

function relLuminance([r, g, b]) {
  const [lr, lg, lb] = [r, g, b].map(srgbToLinear);
  return 0.2126 * lr + 0.7152 * lg + 0.0722 * lb;
}

function contrast(rgb1, rgb2) {
  const l1 = relLuminance(rgb1);
  const l2 = relLuminance(rgb2);
  const [hi, lo] = l1 >= l2 ? [l1, l2] : [l2, l1];
  return (hi + 0.05) / (lo + 0.05);
}

// --------------------------------------------------------------------------- #
// 4. The proof.
// --------------------------------------------------------------------------- #

const WCAG_AA = 4.5;
// How far a backdrop may drop a palette's own baseline contrast before it
// counts as "meaningfully below" it, per the brief. 15% relative — generous
// enough that a legitimate chroma tune does not make this test brittle, tight
// enough that it would catch a chroma raised so far it starts eating into the
// margin the palette was solved for.
const MAX_RELATIVE_DROP = 0.15;
const HUE_STEP = 15; // 24 samples across the full circle, per palette

test('the backdrop clears WCAG AA and never meaningfully undercuts baseline', () => {
  assert.ok(Number.isFinite(CHROMA) && CHROMA > 0,
    'could not read a positive chroma constant out of app.css — see the '
    + 'setup test above for the exact shape expected');

  const failures = [];

  for (const p of STANDARD) {
    const bg = hexToSrgb(p.bgPrimary);
    for (const label of ['textMuted', 'textSecondary', 'textPrimary']) {
      const fg = hexToSrgb(p[label]);
      const baseline = contrast(bg, fg);
      const floor = Math.max(WCAG_AA, baseline * (1 - MAX_RELATIVE_DROP));

      for (let hue = 0; hue < 360; hue += HUE_STEP) {
        const tinted = backdropRgb(p.bgPrimary, CHROMA, hue);
        const ratio = contrast(tinted, fg);
        if (ratio < floor) {
          failures.push(
            `${p.id}/${label} @hue=${hue}: ratio ${ratio.toFixed(2)} < floor `
            + `${floor.toFixed(2)} (baseline ${baseline.toFixed(2)}, WCAG AA ${WCAG_AA})`);
        }
      }
    }
  }

  assert.deepEqual(failures, [],
    `\n${failures.length} palette/hue combination(s) fall below AA or too far `
    + `under baseline:\n${failures.join('\n')}`);
});

test('a pure-black or pure-white --bg-primary is inert under this formula '
   + '(no hue exists to move at L=0 or L=1, so those palettes are '
   + 'contrast-invariant by construction, not by luck)', () => {
  for (const p of STANDARD) {
    if (p.bgPrimary !== '#000000' && p.bgPrimary !== '#ffffff') continue;
    const baseline = contrast(hexToSrgb(p.bgPrimary), hexToSrgb(p.textMuted));
    for (let hue = 0; hue < 360; hue += HUE_STEP) {
      const tinted = backdropRgb(p.bgPrimary, CHROMA, hue);
      const ratio = contrast(tinted, hexToSrgb(p.textMuted));
      assert.ok(Math.abs(ratio - baseline) < 0.5,
        `${p.id} moved by ${(ratio - baseline).toFixed(3)} at hue=${hue} — `
        + 'expected near-zero movement at an extreme lightness');
    }
  }
});

// --------------------------------------------------------------------------- #
// 5. Night themes. A night theme exists to light no blue sub-pixel, and the
//    tint rotates --bg-primary's hue through the whole wheel, so under the
//    night contrast profile app.css pins the pane back to the flat ground.
//    Pure red peaks at 5.25:1 on black, so the night profile's floors are
//    its own: primary 5, secondary 4.5, tertiary (--text-muted) 3.5.
// --------------------------------------------------------------------------- #

const NIGHT_FLOORS = { textPrimary: 5, textSecondary: 4.5, textMuted: 3.5 };

test('night themes: the backdrop tint is switched off under the night profile', () => {
  assert.ok(NIGHT.length >= 1, 'expected at least one night theme');
  const rule = /@supports\s*\(color:\s*oklch\(from red l c h\)\)\s*\{\s*:root\[data-contrast-profile="night"\]\s*\.messages\s*\{\s*background-color:\s*var\(--bg-primary\);?\s*\}\s*\}/;
  assert.match(APP_CSS, rule,
    'app.css must pin .messages to var(--bg-primary) under :root[data-contrast-profile="night"], '
    + 'inside the same @supports as the tint, or a night theme paints a hue-rotated (blue-lit) pane');
});

test('night themes: the message pane ground lights no blue, and text clears the night floors', () => {
  const failures = [];
  for (const p of NIGHT) {
    const bg = hexToSrgb(p.bgPrimary);
    if (Math.round(bg[2] * 255) !== 0) failures.push(`${p.id}: --bg-primary ${p.bgPrimary} has a blue channel`);
    for (const [label, floor] of Object.entries(NIGHT_FLOORS)) {
      const fg = hexToSrgb(p[label]);
      if (Math.round(fg[2] * 255) !== 0) failures.push(`${p.id}: ${label} ${p[label]} has a blue channel`);
      const ratio = contrast(bg, fg);
      if (ratio < floor) failures.push(`${p.id}/${label}: ${ratio.toFixed(2)} < night floor ${floor}`);
    }
  }
  assert.deepEqual(failures, [], `\n${failures.join('\n')}`);
});
