// Chrome glyphs (emoji or text symbols used as UI icons) are ThemeForge sprite icons.
// Content glyphs (a bot's own emoji, user text, LaTeX symbols, matcher tables) stay and carry
// `forgeui: allow-icons` on their line. This guards the sweep: index.html and js/* draw no
// chrome glyph as text, the locale label strings carry no leading glyph, and every sprite
// name the code draws exists in the vendored ThemeForge icons.json.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const STATIC = join(HERE, '..', 'static');
const read = (rel) => readFileSync(join(STATIC, rel), 'utf8');
const MARKER = 'forgeui: allow-icons';

function fakeEl(name) {
  return {
    nodeName: name, attrs: {}, children: [], classes: [],
    setAttribute(k, v) { this.attrs[k] = v; },
    append(...kids) { this.children.push(...kids); },
    classList: { add(c) { this.owner.classes.push(c); }, owner: null },
  };
}
globalThis.document = {
  createElementNS: (_ns, name) => { const e = fakeEl(name); e.classList.owner = e; return e; },
};
globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
const { spriteIcon, spriteIconHtml } = await import('../static/js/util.js');

const manifest = JSON.parse(read('ui-theme/icons.json'));
const NAMES = new Set(Object.keys(manifest.icons));
const sprite = read('ui-theme/icons.svg');

// The glyphs this sweep replaced. A line holding one of these without the marker is a regression.
const CHROME = /[✕✗✓✔⚙⧉⤓⏹⏸▶■▲▼▾▸◂‹›⋯⠿↑↓↗✉✏✨⚡⚠❌✅🚨🛑🎨🔒🔓📋📁📂🔍🔎＋💬👥👤🤖🧩🗑🖼🔗📊🎛💻⌨💼🗄💾📣📥🧠🔧📷🫵]/u;

function codeLines(src) {
  const out = [];
  src.split('\n').forEach((raw, i) => {
    const t = raw.trim();
    if (t.startsWith('//') || t.startsWith('*') || t.startsWith('/*') || raw.includes(MARKER)) return;
    out.push([i + 1, raw.replace(/(^|\s)\/\/\s.*$/u, '$1')]);   // drop a trailing line comment
  });
  return out;
}

test('index.html draws every chrome glyph as a sprite icon', () => {
  const html = read('index.html').replace(/<!--[\s\S]*?-->/g, '').replace(/<script[\s\S]*?<\/script>/g, '');
  const bad = [];
  for (const line of html.split('\n')) {
    if (line.includes(MARKER)) continue;
    // Text between tags, not attribute values (placeholders and titles are words).
    const text = line.replace(/<[^>]*>/g, '\u0001').split('\u0001').join(' ');
    if (CHROME.test(text)) bad.push(line.trim().slice(0, 100));
  }
  assert.deepEqual(bad, [], 'chrome glyph left as text in index.html');
});

test('index.html icons point at the vendored sprite and name icons that exist', () => {
  const html = read('index.html');
  const names = [...html.matchAll(/<use href="\/static\/ui-theme\/icons\.svg#([a-z0-9-]+)"/g)].map((m) => m[1]);
  assert.ok(names.length >= 40, `expected the static chrome to carry sprite icons, found ${names.length}`);
  for (const n of names) {
    assert.ok(NAMES.has(n), `index.html names #${n}, not in ThemeForge icons.json`);
    assert.ok(sprite.includes(`id="${n}"`), `sprite lacks #${n}`);
  }
  // Every icon-only control keeps an accessible name.
  for (const m of html.matchAll(/<(button|a)\b([^>]*)>\s*<svg[^>]*><use[^>]*\/><\/svg>\s*<\/\1>/g)) {
    assert.match(m[2], /aria-label=/, `icon-only control without aria-label: ${m[0].slice(0, 80)}`);
  }
});

test('js/* draws no chrome glyph as text (content glyphs carry the allow marker)', () => {
  const bad = [];
  for (const f of readdirSync(join(STATIC, 'js')).filter((n) => n.endsWith('.js'))) {
    for (const [no, line] of codeLines(read(`js/${f}`))) {
      if (line.includes(MARKER) || !CHROME.test(line)) continue;
      bad.push(`js/${f}:${no} ${line.trim().slice(0, 90)}`);
    }
  }
  assert.deepEqual(bad, []);
});

test('every sprite name the code asks for exists in ThemeForge icons.json', () => {
  const asked = new Set();
  for (const f of readdirSync(join(STATIC, 'js')).filter((n) => n.endsWith('.js'))) {
    const src = read(`js/${f}`);
    for (const m of src.matchAll(/spriteIcon(?:Html)?\(\s*(?:[^'"()]*\?\s*)?'([a-z0-9-]+)'(?:\s*:\s*'([a-z0-9-]+)')?/g)) {
      asked.add(m[1]); if (m[2]) asked.add(m[2]);
    }
    for (const m of src.matchAll(/icon\s*:\s*'((?:[a-z0-9]+-)+[a-z0-9]+)'/g)) asked.add(m[1]);
  }
  assert.ok(asked.size >= 15, `found only ${asked.size} sprite names`);
  for (const n of asked) assert.ok(NAMES.has(n), `code draws #${n}, which ThemeForge does not ship`);
});

test('spriteIcon() and spriteIconHtml() emit a same-origin <use> on the sprite', () => {
  const svg = spriteIcon('play', 14);
  assert.equal(svg.attrs.width, '14');
  assert.equal(svg.attrs['aria-hidden'], 'true');
  assert.ok(svg.classes.includes('rail-icon'));
  assert.equal(svg.children[0].attrs.href, '/static/ui-theme/icons.svg#play');
  const html = spriteIconHtml('check', 12);
  assert.match(html, /^<svg class="rail-icon"[^>]*aria-hidden="true"[^>]*><use href="\/static\/ui-theme\/icons\.svg#check"\/><\/svg>$/);
});

test('locale label strings carry no leading glyph (the icon is drawn by the code)', () => {
  const KEYS = new Set(['working', 'change_photo', 'filter_text', 'filter_note', 'filter_user', 'filter_tool',
    'filter_tool_result', 'filter_thinking', 'generate', 'pool_low', 'trace', 'zero', 'one', 'two', 'few', 'many']);
  const LEAD = /^[\s]*[⚙📷🗑💬📣🧑🔧📥🧠✨⚠⚡]/u;
  for (const f of readdirSync(join(STATIC, 'locales')).filter((n) => n.endsWith('.json'))) {
    const walk = (o, path) => {
      for (const [k, v] of Object.entries(o)) {
        if (v && typeof v === 'object') walk(v, `${path}${k}.`);
        else if (typeof v === 'string' && KEYS.has(k) && LEAD.test(v)) assert.fail(`${f} ${path}${k} starts with a glyph: ${v}`);
      }
    };
    walk(JSON.parse(read(`locales/${f}`)), '');
  }
});
