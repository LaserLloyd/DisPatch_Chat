// Static guards for the axe findings the role-matrix e2e (tests/e2e/roles_axe.py) reported
// before 4.3 (WP-AX). Each test pins the source shape of one fix, so a revert fails here
// without a browser.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const STATIC = join(HERE, '..', 'static');
const read = (f) => readFileSync(join(STATIC, f), 'utf8');
const HTML = read('index.html');
const CSS = read('app.css');
const MAIN = read('js/main.js');
const TOOLS = read('js/tools.js');
const SCHEMA = read('js/settings_schema.js');
const STUDIO_CSS = readFileSync(join(HERE, '..', '..', 'apps', 'imagestudio', 'static', 'studio.css'), 'utf8');

function settingsTablist() {
  const start = HTML.indexOf('id="settings-tabs"');
  const open = HTML.lastIndexOf('<div', start);
  const end = HTML.indexOf('</nav>', start);
  return HTML.slice(open, end);
}

test('#settings-tabs (critical aria-required-children): every element child of the tablist is a tab or presentational', () => {
  const list = settingsTablist();
  const children = [...list.matchAll(/^\s{12}<(\w+)([^>]*)>/gm)];
  assert.ok(children.length >= 10, 'the tablist children were found');
  for (const [, tag, attrs] of children) {
    if (/\bhidden\b/.test(attrs) && tag === 'span') continue;          // #snav-addons-end marker
    const ok = /role="tab"/.test(attrs) || /role="(presentation|none)"/.test(attrs);
    assert.ok(ok, `<${tag}${attrs}> inside role=tablist must be role=tab or presentational`);
  }
  assert.ok(/<h3 role="presentation" class="snav-heading/.test(list), 'the group headings are presentational');
});

test('#conn-dot (aria-prohibited-attr): the named status dot has a role, and its name carries the state', () => {
  assert.match(HTML, /<span class="conn-dot[^"]*" id="conn-dot" role="img"[^>]*aria-label=/);
  assert.match(MAIN, /dot\.setAttribute\('aria-label', `\$\{t\('conn\.aria'\)\}: \$\{dot\.title\}`\)/);
});

test('#tools-menu-btn (label-content-name-mismatch): the name is the visible label', () => {
  const at = TOOLS.indexOf("id: 'tools-menu-btn'");
  const block = TOOLS.slice(at, TOOLS.indexOf('});', at));
  assert.match(block, /'aria-label': label,/);
  assert.match(TOOLS, /el\('span', \{ class: 'bot-name-label', text: label \}\)/, 'the same label is the visible text');
});

test('thread rows (label-content-name-mismatch): named by their content, not a title-only aria-label', () => {
  const at = MAIN.indexOf("class: 'thread-item'");
  const block = MAIN.slice(at, MAIN.indexOf('onclick:', at));
  assert.match(block, /role: 'button'/);
  assert.doesNotMatch(block, /'aria-label'/);
});

test('rail bot buttons / thread rows: a letter avatar draws its glyph in CSS, not as text joining the name', () => {
  const fn = MAIN.slice(MAIN.indexOf('function letterAvatarNode'), MAIN.indexOf('function letterAvatarNode') + 400);
  assert.match(fn, /dataset: \{ letter: botLetter\(bot\) \}/);
  assert.match(fn, /'aria-hidden': 'true'/);
  assert.doesNotMatch(fn, /text: botLetter/);
  assert.match(CSS, /\.letter-avatar\[data-letter\]::before \{ content: attr\(data-letter\); \}/);
});

test('schema reset button (label-content-name-mismatch): its name starts with its visible words', () => {
  const at = SCHEMA.indexOf("ui-setting__reset");
  const block = SCHEMA.slice(at, at + 300);
  assert.match(block, /text: t\('schema\.reset'\), 'aria-label': `\$\{t\('schema\.reset'\)\}: \$\{labelOf\(def\)\}`/);
});

test('#toast (color-contrast): a hidden toast leaves the accessibility tree once it has faded', () => {
  assert.match(CSS, /\.toast\.hidden \{[^}]*visibility: hidden;[^}]*visibility 0s linear \.25s; \}/);
});

test('lock-screen options (color-contrast): labels use --text-primary under the group\'s rest opacity', () => {
  assert.match(CSS, /\.lock-options \.lock-remember \{ color: var\(--text-primary\); \}/);
});

test('Image Studio .st-primary (color-contrast): beats `.st-page button { color: inherit }`', () => {
  assert.match(STUDIO_CSS, /\.st-page \.st-primary, \.st-primary \{ background: var\(--accent\); color: var\(--on-accent\);/);
});

test('icon-only chrome buttons carry a tooltip from the same locale key as their name', () => {
  for (const [id, key] of [['back-btn', 'common.back'], ['send', 'composer.send'], ['tool-back', 'common.back'], ['bm-close', 'common.close']]) {
    const m = HTML.match(new RegExp(`<button[^>]*id="${id}"[^>]*>`));
    assert.ok(m, id);
    assert.match(m[0], / title="[^"]+"/, `${id} has a title`);
    assert.ok(m[0].includes(`title=${key}`), `${id} title is translated from ${key}`);
  }
});
