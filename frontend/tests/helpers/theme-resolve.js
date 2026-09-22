// Resolve DisPatch's legacy colour tokens for any theme, from source.
//
// Since the move onto the vendored theme package, no single file holds a
// palette any more: the contract tokens come from static/ui-theme.css (one
// :root base block for Purple plus one [data-palette="<slug>"] block per
// theme, then the DisPatch compat adapter that aliases --bg-primary & co. onto
// contract names), and DisPatch's own roles come from static/theme.css. A test
// that wants "what is --user-bubble in Forest" has to replay the cascade the
// browser runs on <html>. This does exactly that, for the handful of selector
// shapes those two files use, and refuses (throws) on any other shape that
// could apply to <html> — a silent skip would make every parity test pass
// against a cascade it did not understand.
//
// What <html> carries for a theme is read from the runtime's own registry in
// static/ui-theme.js (the runtime removes data-palette for the base theme and
// derives data-theme from the ground), so the simulation cannot drift from
// what the page does.
//
// Values are then substituted (var() with fallbacks, recursively) and folded
// to a canonical colour where possible: hex, rgb()/rgba(), and color-mix() in
// srgb or oklab between two colours or a colour and transparent. Anything
// else (gradients, shadow lists) comes back as whitespace-normalised text with
// its colours canonicalised, which is what the parity fixture stores too.

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
export const STATIC = join(HERE, '..', '..', 'static');

const stripComments = (s) => s.replace(/\/\*[\s\S]*?\*\//g, '');

/** Top-level rules as {selector, decls:[[name, value]], order}. At-rules are
 *  skipped whole (keyframes, reduced-motion blocks, @media width overrides —
 *  none of which may carry a colour for <html>; the width override in
 *  theme.css is layout). */
function parseRules(src, fileOrder) {
  const css = stripComments(src);
  const rules = [];
  let i = 0;
  let order = 0;
  while (i < css.length) {
    const open = css.indexOf('{', i);
    if (open === -1) break;
    const head = css.slice(i, open).trim();
    // Find the matching close brace (at-rules nest).
    let depth = 1;
    let j = open + 1;
    while (j < css.length && depth) {
      if (css[j] === '{') depth += 1;
      else if (css[j] === '}') depth -= 1;
      j += 1;
    }
    const body = css.slice(open + 1, j - 1);
    if (!head.startsWith('@')) {
      const decls = [];
      for (const m of body.matchAll(/(--[\w-]+|color-scheme)\s*:\s*([^;]+);?/g)) {
        decls.push([m[1], m[2].replace(/\s+/g, ' ').trim()]);
      }
      rules.push({ selector: head.replace(/\s+/g, ' '), decls, order: fileOrder * 1e6 + order });
      order += 1;
    }
    i = j;
  }
  return rules;
}

/** The runtime registry (slug, name, ground, colorScheme, contrastProfile …). */
export function registry() {
  const js = readFileSync(join(STATIC, 'ui-theme.js'), 'utf8');
  const m = /\/\*@registry\*\/(\[[\s\S]*?\])\/\*@end-registry\*\//.exec(js);
  if (!m) throw new Error('ui-theme.js: registry markers not found');
  return JSON.parse(m[1]);
}

export function manifest() {
  const js = readFileSync(join(STATIC, 'ui-theme.js'), 'utf8');
  const m = /window\.UI_THEME_MANIFEST = (\{.*\});/.exec(js);
  if (!m) throw new Error('ui-theme.js: UI_THEME_MANIFEST not found');
  return JSON.parse(m[1]);
}

export function revision() {
  const js = readFileSync(join(STATIC, 'ui-theme.js'), 'utf8');
  const m = /\/\*@revision\*\/"([^"]+)"\/\*@end-revision\*\//.exec(js);
  return m ? m[1] : null;
}

const DATA_THEME = { oled: 'amoled', dark: 'dark', light: 'light' };
const BASE = 'purple';

/** The attributes the runtime puts on <html> for one theme. */
export function htmlAttrs(theme) {
  return {
    'data-palette': theme.slug === BASE ? null : theme.slug,
    'data-theme': DATA_THEME[theme.ground] || 'dark',
    'data-contrast-profile': theme.contrastProfile || 'standard',
  };
}

/** Does one simple selector match <html> with these attributes? Returns the
 *  specificity (number of class-level parts) or -1. Throws on a shape it does
 *  not model when that shape could plausibly target <html>. */
function matchHtml(sel, attrs) {
  const s = sel.trim();
  // Descendant / child / sibling combinators target something INSIDE <html>.
  if (/[\s>+~]/.test(s)) return -1;
  // A shape that starts with a tag, class or id other than :root / html is
  // not <html>.
  if (/^[.#a-zA-Z*]/.test(s) && !/^html(\[|:|$)/.test(s)) return -1;
  let rest = s.replace(/^html/, '');
  let spec = 0;
  const parts = rest.match(/:root|:lang\([^)]*\)|\[[^\]]+\]|::?[\w-]+(\([^)]*\))?/g) || [];
  if (parts.join('') !== rest) throw new Error(`theme-resolve: cannot model selector "${sel}"`);
  for (const p of parts) {
    if (p === ':root') { spec += 1; continue; }
    if (p.startsWith(':lang(')) return -1;       // the fixture/tests run in en
    if (p.startsWith('[')) {
      const m = /^\[([\w-]+)(?:="([^"]*)")?\]$/.exec(p);
      if (!m) throw new Error(`theme-resolve: cannot model attribute "${p}" in "${sel}"`);
      const have = attrs[m[1]];
      if (have === null || have === undefined) return -1;
      if (m[2] !== undefined && have !== m[2]) return -1;
      spec += 1;
      continue;
    }
    return -1;                                    // pseudo-classes/elements: not a token host
  }
  return spec;
}

let cachedRules = null;
function allRules() {
  if (cachedRules) return cachedRules;
  // Load order in index.html: theme.css, app.css, dashboard.css, ui-theme.css.
  // app.css / dashboard.css declare no tokens on <html> (asserted by a test),
  // so only the two token files matter here.
  cachedRules = [
    ...parseRules(readFileSync(join(STATIC, 'theme.css'), 'utf8'), 0),
    ...parseRules(readFileSync(join(STATIC, 'ui-theme.css'), 'utf8'), 3),
  ];
  return cachedRules;
}

/** Declared (unsubstituted) values on <html> for one theme slug. */
export function declared(slug) {
  const theme = registry().find((t) => t.slug === slug);
  if (!theme) throw new Error(`unknown theme ${slug}`);
  const attrs = htmlAttrs(theme);
  const winners = new Map();                        // name -> {spec, order, value}
  for (const rule of allRules()) {
    let best = -1;
    for (const sel of rule.selector.split(',')) best = Math.max(best, matchHtml(sel, attrs));
    if (best < 0) continue;
    for (const [name, value] of rule.decls) {
      const prev = winners.get(name);
      if (!prev || best > prev.spec || (best === prev.spec && rule.order >= prev.order)) {
        winners.set(name, { spec: best, order: rule.order, value });
      }
    }
  }
  return new Map([...winners].map(([k, v]) => [k, v.value]));
}

// ---------------------------------------------------------------- colours --

function clamp01(x) { return Math.min(1, Math.max(0, x)); }

export function parseColor(text) {
  const t = String(text).trim().toLowerCase();
  if (t === 'transparent') return [0, 0, 0, 0];
  if (t === 'white') return [255, 255, 255, 1];
  if (t === 'black') return [0, 0, 0, 1];
  let m = /^#([0-9a-f]{3,8})$/.exec(t);
  if (m) {
    let h = m[1];
    if (h.length === 3 || h.length === 4) h = [...h].map((c) => c + c).join('');
    if (h.length !== 6 && h.length !== 8) return null;
    const n = [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16));
    const a = h.length === 8 ? parseInt(h.slice(6, 8), 16) / 255 : 1;
    return [...n, a];
  }
  m = /^rgba?\(\s*([\d.]+)[\s,]+([\d.]+)[\s,]+([\d.]+)(?:\s*[,/]\s*([\d.]+%?))?\s*\)$/.exec(t);
  if (m) {
    let a = m[4] === undefined ? 1 : m[4].endsWith('%') ? parseFloat(m[4]) / 100 : parseFloat(m[4]);
    return [+m[1], +m[2], +m[3], a];
  }
  return null;
}

function srgbToLinear(c) { c /= 255; return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4; }
function linearToSrgb(c) { const v = c <= 0.0031308 ? 12.92 * c : 1.055 * c ** (1 / 2.4) - 0.055; return clamp01(v) * 255; }

function toOklab([r, g, b]) {
  const [lr, lg, lb] = [r, g, b].map(srgbToLinear);
  const l = Math.cbrt(0.4122214708 * lr + 0.5363325363 * lg + 0.0514459929 * lb);
  const m = Math.cbrt(0.2119034982 * lr + 0.6806995451 * lg + 0.1073969566 * lb);
  const s = Math.cbrt(0.0883024619 * lr + 0.2817188376 * lg + 0.6299787005 * lb);
  return [
    0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s,
    1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s,
    0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s,
  ];
}
function fromOklab([L, a, b]) {
  const l = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3;
  const m = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3;
  const s = (L - 0.0894841775 * a - 1.2914855480 * b) ** 3;
  return [
    4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
    -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
    -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s,
  ].map(linearToSrgb);
}

/** color-mix(in <space>, A p%, B [q%]) with premultiplied alpha, per CSS
 *  Color 5. Only srgb and oklab — the two spaces these files use. */
function mix(space, a, pa, b, pb) {
  if (pa === null && pb === null) { pa = 0.5; pb = 0.5; }
  else if (pa === null) pa = 1 - pb;
  else if (pb === null) pb = 1 - pa;
  const sum = pa + pb;
  const scale = sum < 1 ? sum : 1;                 // CSS: sum < 100% scales alpha
  pa /= sum; pb /= sum;
  const alpha = a[3] * pa + b[3] * pb;
  if (alpha === 0) return [0, 0, 0, 0];
  const conv = space === 'oklab' ? toOklab : (c) => c.slice(0, 3);
  const back = space === 'oklab' ? fromOklab : (c) => c;
  // A fully transparent side contributes no hue (premultiplied).
  const ca = conv(a); const cb = conv(b);
  const out = [0, 1, 2].map((i) => (ca[i] * a[3] * pa + cb[i] * b[3] * pb) / alpha);
  const rgb = back(out);
  return [...rgb, alpha * scale];
}

export function formatColor([r, g, b, a]) {
  const R = Math.round(r); const G = Math.round(g); const B = Math.round(b);
  if (a >= 0.9995) return '#' + [R, G, B].map((n) => n.toString(16).padStart(2, '0')).join('');
  return `rgba(${R},${G},${B},${+a.toFixed(4)})`;
}

/** Split a function's argument list on top-level commas. */
function splitArgs(s) {
  const out = []; let depth = 0; let cur = '';
  for (const ch of s) {
    if (ch === '(') depth += 1;
    if (ch === ')') depth -= 1;
    if (ch === ',' && depth === 0) { out.push(cur.trim()); cur = ''; continue; }
    cur += ch;
  }
  if (cur.trim()) out.push(cur.trim());
  return out;
}

function evalColorMix(inner) {
  const args = splitArgs(inner);
  const sp = /^in\s+(srgb|oklab)$/.exec(args[0]);
  if (!sp || args.length !== 3) return null;
  const part = (s) => {
    const m = /^(.*?)(?:\s+([\d.]+)%)?$/.exec(s.trim());
    const c = parseColor(fold(m[1]));
    return c ? [c, m[2] === undefined ? null : parseFloat(m[2]) / 100] : null;
  };
  const A = part(args[1]); const B = part(args[2]);
  if (!A || !B) return null;
  return mix(sp[1], A[0], A[1], B[0], B[1]);
}

/** Fold every color-mix() and literal colour in a value to canonical text. */
function fold(value) {
  let v = value.trim();
  // Innermost color-mix first.
  for (let guard = 0; guard < 20; guard++) {
    const at = v.lastIndexOf('color-mix(');
    if (at === -1) break;
    let depth = 0; let end = -1;
    for (let k = at + 'color-mix'.length; k < v.length; k++) {
      if (v[k] === '(') depth += 1;
      else if (v[k] === ')') { depth -= 1; if (depth === 0) { end = k; break; } }
    }
    if (end === -1) break;
    const c = evalColorMix(v.slice(at + 'color-mix('.length, end));
    if (!c) break;
    v = v.slice(0, at) + formatColor(c) + v.slice(end + 1);
  }
  const whole = parseColor(v);
  if (whole) return formatColor(whole);
  return v
    .replace(/#[0-9a-fA-F]{3,8}\b|rgba?\([^()]*\)/g, (m) => { const c = parseColor(m); return c ? formatColor(c) : m; })
    .replace(/\s+/g, ' ')
    .replace(/\s*,\s*/g, ', ');
}

/** Substitute var() references against a declared map, recursively. */
function substitute(value, decl, seen = new Set()) {
  return value.replace(/var\(\s*(--[\w-]+)\s*(?:,((?:[^()]|\([^()]*\))*))?\)/g, (_, name, fb) => {
    if (seen.has(name)) throw new Error(`theme-resolve: var() cycle through ${name}`);
    if (decl.has(name)) return substitute(decl.get(name), decl, new Set([...seen, name]));
    if (fb !== undefined) return substitute(fb.trim(), decl, seen);
    return `UNRESOLVED(${name})`;
  });
}

/** Resolved, canonical value of one token for one theme (or null if the
 *  theme leaves it undeclared). */
export function resolveToken(slug, name, decl = declared(slug)) {
  if (!decl.has(name)) return null;
  let v = substitute(decl.get(name), decl);
  // Nested var() inside a fallback can take a couple of passes.
  for (let i = 0; i < 5 && /var\(/.test(v); i++) v = substitute(v, decl);
  return fold(v);
}

export function resolveAll(slug, names) {
  const decl = declared(slug);
  return Object.fromEntries(names.map((n) => [n, resolveToken(slug, n, decl)]));
}

// --------------------------------------------------------------- contrast --

export function luminance(color) {
  const c = typeof color === 'string' ? parseColor(color) : color;
  const f = (x) => { x /= 255; return x <= 0.03928 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4; };
  return 0.2126 * f(c[0]) + 0.7152 * f(c[1]) + 0.0722 * f(c[2]);
}

export function contrast(a, b) {
  const [x, y] = [luminance(a), luminance(b)];
  return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05);
}

/** Composite a possibly-translucent colour over an opaque ground. */
export function over(fg, bg) {
  const f = typeof fg === 'string' ? parseColor(fg) : fg;
  const b = typeof bg === 'string' ? parseColor(bg) : bg;
  const a = f[3];
  return formatColor([0, 1, 2].map((i) => f[i] * a + b[i] * (1 - a)).concat(1));
}

/** Canonical form of an already-concrete value (e.g. a fixture entry), so it
 *  compares equal to resolveToken() output regardless of spacing or `.4` vs
 *  `0.4`. */
export function canon(value) { return fold(String(value)); }
