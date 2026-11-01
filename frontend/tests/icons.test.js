// The chrome icons come from the vendored ThemeForge sprite (ui-theme/icons.svg) through
// ICON_MAP: every mapped name must exist in the sprite and icons.json, railIcon() must emit
// a same-origin <use>, the service worker must precache the sprite, and nothing may load
// an icon from outside the app (the CSP is script-src/img-src 'self').
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const STATIC = join(HERE, '..', 'static');
const read = (rel) => readFileSync(join(STATIC, rel), 'utf8');

const store = new Map();
globalThis.localStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
  removeItem: (k) => store.delete(k),
};
function fakeEl(name) {
  return {
    nodeName: name, attrs: {}, children: [], classes: [], dataset: {},
    removeAttribute(k) { delete this.attrs[k]; },
    classList: { add(c) { this.owner.classes.push(c); }, owner: null },
    setAttribute(k, v) { this.attrs[k] = v; },
    append(...kids) { this.children.push(...kids); },
  };
}
const mk = (name) => { const e = fakeEl(name); e.classList.owner = e; return e; };
globalThis.document = {
  documentElement: mk('html'),
  createElementNS: (_ns, name) => mk(name),
  createElement: (name) => mk(name),
  createTextNode: (text) => ({ nodeName: '#text', text }),
};
const { ICON_MAP, RAIL_ICONS, railIcon, uiIcon } = await import('../static/js/util.js');

const manifest = JSON.parse(read('ui-theme/icons.json'));
const sprite = read('ui-theme/icons.svg');
const symbols = new Set([...sprite.matchAll(/<symbol id="([^"]+)"/g)].map((m) => m[1]));

test('the vendored sprite and icons.json list the same icons', () => {
  assert.deepEqual([...symbols].sort(), Object.keys(manifest.icons).sort());
});

test('every ICON_MAP value exists in ui-theme/icons.json and the sprite', () => {
  const missing = Object.entries(ICON_MAP).filter(([, name]) => !manifest.icons[name] || !symbols.has(name));
  assert.deepEqual(missing, []);
});

test('ICON_MAP covers exactly the RAIL_ICONS keys', () => {
  assert.deepEqual(Object.keys(ICON_MAP).sort(), Object.keys(RAIL_ICONS).sort());
});

test('railIcon draws a same-origin <use> for every shared icon', () => {
  for (const [key, paths] of Object.entries(RAIL_ICONS)) {
    const svg = railIcon(paths);
    assert.equal(svg.children.length, 1, key);
    const use = svg.children[0];
    assert.equal(use.nodeName, 'use', key);
    assert.equal(use.attrs.href, `/static/ui-theme/icons.svg#${ICON_MAP[key]}`, key);
    assert.equal(svg.attrs['aria-hidden'], 'true', key);
    assert.ok(svg.classes.includes('rail-icon'), key);
  }
});

test('an app-owned glyph (a path array not in the shared set) still draws inline', () => {
  const svg = railIcon(['M1 1L2 2']);
  assert.equal(svg.children[0].nodeName, 'path');
  assert.equal(svg.attrs.stroke, 'currentColor');
});

test('uiIcon names, labels and sizes the sprite icon', () => {
  const named = uiIcon('gear', { label: 'Settings', size: 20 });
  assert.equal(named.children[0].attrs.href, '/static/ui-theme/icons.svg#settings');
  assert.equal(named.attrs.role, 'img');
  assert.equal(named.attrs.width, '20');
  assert.equal(uiIcon('no-such-icon'), null);
});

test('the service worker precaches the sprite', () => {
  assert.match(read('sw.js'), /'\/static\/ui-theme\/icons\.svg'/);
});

test('the sprite is self-contained: no external reference, script or handler', () => {
  assert.ok(!/https?:\/\/(?!www\.w3\.org\/2000\/svg)/.test(sprite.replace(/<!--[\s\S]*?-->/g, '')), 'external URL');
  assert.ok(!/<script|\son\w+=|xlink:href="(?!#)/i.test(sprite));
});

test('the CSP allows same-origin images and no third-party icon host', () => {
  const html = read('index.html');
  assert.ok(!/(?:fonts|cdn)\.\w+|cdnjs|jsdelivr|unpkg/.test(html), 'index.html loads from a CDN');
});

// A home-screen icon is captured at install time and re-read only when its URL changes,
// so the app-icon URLs carry a ?v= that moves with the artwork (2 = the 4.0 "D" mark).
test('the install icons are versioned and precached; the manifest id never moves', () => {
  const wm = JSON.parse(read('manifest.webmanifest'));
  assert.equal(wm.id, '/?source=pwa');
  for (const icon of wm.icons) assert.match(icon.src, /^\/static\/[\w.-]+\?v=2$/, icon.src);
  const html = read('index.html');
  assert.match(html, /<link rel="apple-touch-icon" href="\/static\/apple-touch-icon\.png\?v=2" \/>/);
  assert.match(html, /<link rel="icon" href="\/static\/favicon\.svg\?v=2" \/>/);
  const sw = read('sw.js');
  for (const f of ['favicon.svg', 'icon-192.png', 'icon-512.png', 'icon-maskable-512.png', 'apple-touch-icon.png']) {
    assert.ok(sw.includes(`'/static/${f}'`), `sw.js SHELL lacks ${f}`);
  }
});
