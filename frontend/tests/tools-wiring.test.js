// Tools (docs/design/2026-09-25-tools-plugins.md) — the wiring pins.
//
// Textual, like harness-wiring.test.js: spinning up the whole of main.js drags
// in the entire import graph, and what these pin are exactly the joints a
// refactor drops silently — a module missing from the offline shell, a
// container missing from the markup, an opener that forgets the full-page
// class, a menu row left behind after its feature moved to the rail.
// The rendering itself is exercised against real markup in tools.test.js.

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const STATIC = join(HERE, '..', 'static');
const read = (p) => readFileSync(join(STATIC, p), 'utf8');
const HTML = read('index.html');
const MAIN = read('js/main.js');
const TOOLS = read('js/tools.js');
// Markup with comments removed, so a comment that NAMES an id is not
// mistaken for the element.
const MARKUP = HTML.replace(/<!--[\s\S]*?-->/g, '');

/** The body of `function name(` in src, up to the next top-level `}`. */
function fnBody(src, name) {
  const start = src.search(new RegExp(`(?:async\\s+)?function\\s+${name}\\s*\\(`));
  assert.ok(start >= 0, `${name} must be defined`);
  const end = src.indexOf('\n}\n', start);
  assert.ok(end > start, `${name} must have a body`);
  return src.slice(start, end);
}

test('js/tools.js is precached in the sw.js SHELL', () => {
  const sw = read('sw.js');
  assert.match(sw, /'\/static\/js\/tools\.js'/, 'tools.js must be in SHELL');
});

test('main.js imports tools.js at the version the rest of the app uses', () => {
  assert.match(MAIN, /from '\.\/tools\.js\?v=\d+'/);
});

test('index.html has the #tool-list rail group directly after #bot-list', () => {
  assert.match(MARKUP, /id="bot-list"><\/div>\s*<nav class="tool-list" id="tool-list"/,
    '#tool-list must be a <nav> right under #bot-list (and outside it — the roster repaint wipes #bot-list)');
});

test('index.html has ONE generic #tool-view with a sandboxed frame', () => {
  const views = MARKUP.match(/id="tool-view"/g) || [];
  assert.equal(views.length, 1);
  const frame = /<iframe[^>]*id="tool-frame"[^>]*>/.exec(MARKUP);
  assert.ok(frame, '#tool-frame iframe must exist');
  // The markup default is the STRICT sandbox — never unsandboxed, never
  // same-origin until tools.js decides the kind.
  assert.match(frame[0], /sandbox="allow-scripts allow-forms allow-popups"/);
  assert.doesNotMatch(frame[0], /allow-same-origin/);
  for (const id of ['tool-close', 'tool-refresh', 'tool-open', 'tool-back', 'tool-error', 'tool-note']) {
    assert.match(MARKUP, new RegExp(`id="${id}"`), `#${id} must exist in #tool-view`);
  }
});

test('no leftover hardcoded tool rows in the ⌥ menu', () => {
  for (const id of ['tools-harness', 'tools-studioforge', 'tools-mail', 'tools-clients']) {
    assert.doesNotMatch(MARKUP, new RegExp(`id="${id}"`), `#${id} must be gone from index.html`);
    assert.doesNotMatch(MAIN, new RegExp(`'${id}'`), `main.js must not reference '${id}' any more`);
  }
  assert.doesNotMatch(MAIN, /function\s+toolsMenuEntries\s*\(/);
  // The parked-bot rows stay.
  assert.match(MARKUP, /id="tools-bots"/);
});

test('the ⌥ button shows only when parked bots exist', () => {
  const body = fnBody(MAIN, 'renderSidebarInner');
  assert.match(body, /const toolsAvailable = !state\.decoy && menuBots\.length > 0;/);
});

test('every tool opener toggles body.tool-full (and nothing uses terminal-active)', () => {
  for (const [open, close] of [
    ['openHarnessView', 'closeHarnessView'],
    ['openStudioForgeView', 'closeStudioForgeView'],
    ['openMailView', 'closeMailView'],
    ['openClientsPanel', 'closeClientsView'],
    ['openJobsView', 'closeJobsView'],
  ]) {
    assert.match(fnBody(MAIN, open), /document\.body\.classList\.add\('tool-full'\)/, `${open} must add tool-full`);
    assert.match(fnBody(MAIN, close), /document\.body\.classList\.remove\('tool-full'\)/, `${close} must remove tool-full`);
  }
  assert.match(fnBody(TOOLS, 'openTool'), /document\.body\.classList\.add\('tool-full'\)/, 'openTool must add tool-full');
  assert.match(fnBody(TOOLS, 'closeTool'), /document\.body\.classList\.remove\('tool-full'\)/, 'closeTool must remove tool-full');
  assert.doesNotMatch(MAIN, /terminal-active/);
  assert.doesNotMatch(read('app.css').replace(/\/\*[\s\S]*?\*\//g, ''), /terminal-active/);
});

test('app.css collapses the grid to rail + content under tool-full', () => {
  const css = read('app.css');
  assert.match(css, /body\.tool-full \.app[^{]*\{\s*grid-template-columns: var\(--sidebar-width\) 1fr;/);
  assert.match(css, /body\.tool-full \.threadlist \{ display: none; \}/);
});

test('selectBot routes every tool id to openTool', () => {
  assert.match(fnBody(MAIN, 'selectBot'), /if \(isToolId\(id\)\) \{ openTool\(id\); return; \}/);
});

test('closeToolPanes also closes the generic pane (except when it is the opener)', () => {
  assert.match(fnBody(MAIN, 'closeToolPanes'), /if \(except !== 'tool'\) closeTool\(\{ restore: false \}\);/);
});

test('wireTools() is called from init with the five builtin openers', () => {
  assert.match(MAIN, /\bwireTools\(\{/);
  for (const opener of ['openHarnessView', 'openStudioForgeView', 'openMailView', 'openClientsPanel', 'openJobsView']) {
    assert.match(MAIN, new RegExp(`:\\s*${opener},`), `wireTools must be handed ${opener}`);
  }
});

test('Settings → Tools is an admin-only tab with its pane and Save', () => {
  assert.match(MARKUP, /class="settings-tab admin-only" id="stab-tools"[^>]*data-tab="tools"/);
  assert.match(MARKUP, /id="spane-tools"/);
  assert.match(MARKUP, /data-tab="tools">\s*<button class="btn-primary" id="tools-save"/);
  assert.match(MAIN, /const ADMIN_TABS = \[[^\]]*'tools'[^\]]*\];/);
  assert.match(MAIN, /const SETTINGS_TABS = \[[^\]]*'tools'[^\]]*\];/);
});

test('the deep link opens only unlocked, after the feature probes', () => {
  assert.match(MAIN, /if \(!state\.decoy\) openFromHash\(\);/);
  assert.match(fnBody(TOOLS, 'openFromHash'), /deps\.state\.decoy/);
});

test('api.js exposes the four tools calls', () => {
  const api = read('js/api.js');
  for (const name of ['tools', 'toolStatus', 'toolRefresh', 'toolsSave']) {
    assert.match(api, new RegExp(`\\b${name}: \\(`), `api.${name} must exist`);
  }
  assert.match(api, /toolsSave: \(list\) => j\('\/api\/tools', \{ method: 'PUT'/);
  assert.match(api, /\/api\/tools\/\$\{encodeURIComponent\(id\)\}\/refresh`, \{ method: 'POST'/);
});

test('every builtin opener remembers the chat it covers, before the selection moves', () => {
  for (const open of ['openHarnessView', 'openMailView', 'openClientsPanel', 'openJobsView']) {
    const body = fnBody(MAIN, open);
    assert.match(body, /rememberPrev\(\)/, `${open} must call rememberPrev()`);
    assert.ok(body.indexOf('rememberPrev()') < body.indexOf('state.selectedBotId ='), `${open}: rememberPrev before the selection moves`);
  }
});

test('the Job Board closes back to the remembered chat (✕) and to the rail (‹)', () => {
  const body = fnBody(MAIN, 'wireJobsView');
  assert.match(body, /jobs-close[\s\S]*closeJobsView\(\);\s*restorePrev\(\)/);
  assert.match(body, /jobs-back[\s\S]*closeJobsView\(\);[\s\S]*restorePrev\(\{ stay: true \}\);[\s\S]*navigate\('bots'\)/);
  assert.match(MAIN, /wireJobsView\(\);/);
});

test('tool tiles are bot-shaped themed tiles, not hard-coded hue blocks', () => {
  const css = read('app.css').replace(/\/\*[\s\S]*?\*\//g, '');
  const rule = css.match(/\.tool-avatar \{([^}]*)\}/);
  assert.ok(rule, '.tool-avatar has its own rule');
  assert.match(rule[1], /background: var\(--bg-tertiary\)/);
  assert.match(rule[1], /color: var\(--text-secondary\)/);
  assert.match(css, /\.bot-btn\.active \.tool-avatar \{[^}]*color: var\(--accent-text\)/);
  // The old per-service hues are gone from the rail and the pane heads.
  assert.doesNotMatch(css, /\.(?:bot-avatar|terminal-avatar-mini)\.(?:harness|studioforge)-avatar/);
  assert.doesNotMatch(read('js/tools.js'), /terminal-avatar/);
  // The builtins name line icons that exist.
  const util = read('js/util.js');
  for (const name of ['terminal', 'mail', 'users', 'chart', 'jobs', 'tools']) {
    assert.match(util, new RegExp(`\\n  ${name}: \\[`), `RAIL_ICONS.${name}`);
  }
});
