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
import { readFileSync, existsSync } from 'node:fs';
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

test('the ⌥ button is gone: parked bots are rows in the ONE Tools popup', () => {
  const body = fnBody(MAIN, 'renderSidebarInner');
  assert.doesNotMatch(body, /tools-btn|toolsAvailable/, 'no separate ⌥ rail button any more');
  assert.match(body, /setParkedBots\(parkedBotRows\(menuBots, thinkingBots, unreadBots\)\)/);
  assert.ok(body.indexOf('setParkedBots(') < body.indexOf('renderToolRail()'), 'rows handed over before the rail paints');
  for (const fn of ['toggleToolsMenu', 'openToolsMenu', 'closeToolsMenu', 'wireToolsMenu', 'renderMenuBots']) {
    assert.doesNotMatch(MAIN, new RegExp(`function\\s+${fn}\\s*\\(`), `main.js must not define ${fn} (tools.js owns the popup)`);
  }
  // menubots.js stays the data source for WHICH bots are parked.
  assert.match(MAIN, /from '\.\/menubots\.js\?v=\d+'/);
  assert.match(MAIN, /groupState: toolsGroupState/, 'the desktop button gets the worst-of dot');
});

test('index.html: one popover — #tools-menu is the Tools popup, labelled by its button', () => {
  const menus = MARKUP.match(/id="tools-menu"/g) || [];
  assert.equal(menus.length, 1);
  const tag = /<div[^>]*id="tools-menu"[^>]*>/.exec(MARKUP)[0];
  assert.match(tag, /role="menu"/);
  assert.match(tag, /aria-labelledby="tools-menu-btn"/);
  assert.match(tag, /\shidden[\s>]/);
  const inner = MARKUP.slice(MARKUP.indexOf(tag));
  assert.ok(inner.indexOf('id="tools-menu-tools"') < inner.indexOf('id="tools-bots-sep"')
    && inner.indexOf('id="tools-bots-sep"') < inner.indexOf('id="tools-bots"'), 'tools, rule, parked bots — in that order');
  assert.doesNotMatch(MARKUP, /id="tools-btn"/);
});

test('app.css: the desktop rail collapses to one button; the phone grid rules stay', () => {
  const css = read('app.css').replace(/\/\*[\s\S]*?\*\//g, '');
  assert.match(css, /\.tool-list\.tool-list-compact \{/);
  assert.match(css, /\.app\[data-view="bots"\] \.tool-list \{[^}]*display: grid/);
  assert.match(css, /\.tools-menu \{[^}]*position: fixed/);
  assert.match(css, /\.tools-menu \{[^}]*overflow-y: auto/);
  for (const s of ['running', 'starting', 'error']) {
    assert.match(css, new RegExp(`\\.terminal-sidedot\\.tools-sidedot\\.${s} \\{`), `group dot colour for ${s}`);
  }
});

test('every tool opener toggles body.tool-full (and nothing uses terminal-active)', () => {
  for (const [open, close] of [
    ['openHarnessView', 'closeHarnessView'],
    ['openStudioForgeView', 'closeStudioForgeView'],
    ['openMailView', 'closeMailView'],
    ['openClientsPanel', 'closeClientsView'],
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

test('wireTools() is called from init with the four builtin openers and the app thread opener', () => {
  assert.match(MAIN, /\bwireTools\(\{/);
  assert.match(MAIN, /openThread: openThreadFromApp,/);
  for (const opener of ['openHarnessView', 'openStudioForgeView', 'openMailView', 'openClientsPanel']) {
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
  for (const open of ['openHarnessView', 'openMailView', 'openClientsPanel']) {
    const body = fnBody(MAIN, open);
    assert.match(body, /rememberPrev\(\)/, `${open} must call rememberPrev()`);
    assert.ok(body.indexOf('rememberPrev()') < body.indexOf('state.selectedBotId ='), `${open}: rememberPrev before the selection moves`);
  }
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

// =============================================================================
// Apps (docs/design/2026-09-25-apps.md) — the wiring pins. The behaviour is
// exercised in tools.test.js (pane + messaging) and app-sdk.test.js (the SDK);
// these are the joints: the offline shell, the frame's sandbox, the listener's
// two checks, the generic thread hook, and nothing Job-Board-shaped left in
// the shell.
// =============================================================================

const SW = read('sw.js');
const REPO = join(STATIC, '..', '..');

test('the sw.js CACHE is local-chat-v133 (apps review round)', () => {
  const m = /const CACHE = '([^']+)'/.exec(SW);
  assert.ok(m, 'sw.js must declare CACHE');
  // v133: app hints keyed by app+thread, Safe-Mode guard on an app's
  // open-thread (main.js 108→109, index.html).
  // v132: the desktop rail's Tools group is one button with a popup (tools.js
  // 5→6, menubots.js 1→2, main.js 107→108, app.css 89→90, index.html, every
  // locale).
  // v130: themed tool tiles + the Job Board as a builtin tool. v131: apps —
  // js/app-sdk.js joins SHELL, js/jobs.js + js/job-thread.js leave it, api.js
  // and every importer, main.js, app.css, index.html and every locale moved.
  // The shell is cached by PATH, so an installed client keeps all of the old
  // shell without this bump. If you bump again, bump here too.
  assert.equal(m[1], 'local-chat-v133');
});

test('js/app-sdk.js is precached; the old Job Board modules are not', () => {
  assert.match(SW, /'\/static\/js\/app-sdk\.js'/);
  assert.doesNotMatch(SW, /'\/static\/js\/jobs\.js'|'\/static\/js\/job-thread\.js'/);
});

test('/apps/* is network-only in the service worker', () => {
  const body = SW.slice(SW.indexOf("self.addEventListener('fetch'"));
  assert.match(body, /if \(p\.startsWith\('\/apps\/'\)\) return;/);
  assert.ok(body.indexOf("p.startsWith('/apps/')") < body.indexOf('e.respondWith('), 'decided before any cache use');
});

test('an app frame gets NO sandbox; every other frame keeps one', () => {
  const body = fnBody(TOOLS, 'loadFrame');
  assert.match(body, /if \(isApp\(tool\)\) frame\.removeAttribute\('sandbox'\);\s*else frame\.setAttribute\('sandbox', tool\.kind === 'url' \? SANDBOX_URL : SANDBOX_STATIC\);/);
  assert.ok(body.indexOf("removeAttribute('sandbox')") < body.indexOf("setAttribute('src'"), 'sandbox decided BEFORE src');
  assert.doesNotMatch(TOOLS, /allow-same-origin'[^;]*isApp|SANDBOX_APP/, 'no third sandbox flavour for apps');
});

test('the app message listener checks origin AND source before anything else', () => {
  const body = fnBody(TOOLS, 'onAppMessage');
  assert.match(body, /ev\.origin !== location\.origin/);
  assert.match(body, /ev\.source !== frame\.contentWindow/);
  const guard = body.indexOf('ev.source !== frame.contentWindow');
  assert.ok(guard > 0 && guard < body.indexOf('switch (msg.type)'), 'the guard precedes every handler');
  for (const type of ['open-thread', 'close', 'toast', 'set-title']) {
    assert.match(body, new RegExp(`case 'dispatch:${type}'`), `handles dispatch:${type}`);
  }
  assert.match(fnBody(TOOLS, 'postToApp'), /postMessage\(msg, location\.origin\)/, 'posts only to our own origin');
  assert.match(fnBody(TOOLS, 'wireTools'), /addEventListener\('message', onAppMessage\)/);
});

test('main.js mounts app thread hooks generically', () => {
  const sync = fnBody(MAIN, 'syncAppHook');
  assert.match(sync, /appForBot\(th\.bot_id\)/, 'the hook is found by the thread’s bot, not by name');
  assert.match(sync, /await import\(\/\* @vite-ignore \*\/ url\)/);
  assert.match(sync, /mod\.mount\(\{/);
  assert.match(sync, /threadEl: slot,/, 'each mount gets its own slot, so a stale unmount cannot clear the next panel');
  for (const k of ['threadEl', 'headerEl', 'thread', 'api', 't', 'openThread']) {
    assert.match(sync, new RegExp(`\\b${k}:`), `mount() receives ${k}`);
  }
  assert.match(sync, /\.catch\(\(e\) => \{\s*console\.error\('\[apps\] thread hook failed to load'/, 'a broken hook is logged, never fatal');
  assert.match(fnBody(MAIN, 'renderChatHeader'), /syncAppHook\(\);/);
  assert.match(fnBody(MAIN, 'clearChatView'), /syncAppHook\(\);/);
  assert.match(fnBody(MAIN, 'unmountAppHook'), /h\.unmount\(\)/);
  assert.match(fnBody(MAIN, 'hookUrl'), /url\.startsWith\(`\/apps\/\$\{app\.id\}\/`\)/, 'only the app’s own code is imported');
  assert.match(MAIN, /import \{ forApp \} from '\.\/app-sdk\.js\?v=\d+';/);
  // Live app frames go to the app, never through the shell's own switch.
  assert.match(fnBody(MAIN, 'handleWs'), /data\.type\.startsWith\('app:'\)\) \{ dispatchAppFrame\(data\); return; \}/);
});

test('index.html gives the hook a host between the chat header and the messages', () => {
  const host = MARKUP.indexOf('id="app-thread-host"');
  assert.ok(host > MARKUP.indexOf('class="chat-header"') && host < MARKUP.indexOf('id="messages"'));
  assert.match(MARKUP, /<div class="app-thread-host hidden" id="app-thread-host"><\/div>/);
});

test('nothing Job-Board-shaped is left in the shell', () => {
  for (const f of ['jobs.js', 'job-thread.js']) {
    assert.equal(existsSync(join(STATIC, 'js', f)), false, `js/${f} moved to apps/jobboard/static/`);
  }
  assert.equal(existsSync(join(HERE, 'jobs.test.js')), false, 'its tests moved to apps/jobboard/tests/');
  for (const pat of [/'\.\/jobs\.js\?v=/, /'\.\/job-thread\.js\?v=/, /\bJOBS_ID\b/, /openJobsView|closeJobsView|refreshJobsFeature/,
    /jobsEnabled/, /job-board-host|jobs-view/, /openJobDetail|closeJobDetail|__openJobDetail|__openThread/, /'jobboard'/]) {
    assert.doesNotMatch(MAIN, pat, `main.js still carries ${pat}`);
  }
  assert.doesNotMatch(read('js/api.js'), /\bjobs:\s*\{|\/api\/jobs/, 'api.js has no jobs surface');
  assert.doesNotMatch(TOOLS, /id: 'jobboard'|feature: 'jobs'/, 'the Job Board is not a builtin');
  assert.doesNotMatch(MARKUP, /id="jobs-view"|id="job-board-host"/);
  // (.job-headline/.job-spinner/.job-console are the Clients tab's, unrelated.)
  assert.doesNotMatch(read('app.css'), /\.job-(?:row|card|chip|vote|modal|reason|note|feedback|activity)|\.jobs-|#job-board|\[data-jobs-root\]/,
    'its CSS moved to apps/jobboard/static/board.css');
  for (const lang of ['en', 'ar', 'de', 'es', 'fr', 'ja', 'pt', 'zh']) {
    const d = JSON.parse(read(`locales/${lang}.json`));
    assert.equal('jobs' in d, false, `locales/${lang}.json still has a jobs namespace`);
  }
});

test('the app tests are collected by npm test', () => {
  const pkg = JSON.parse(readFileSync(join(STATIC, '..', 'package.json'), 'utf8'));
  for (const script of ['test', 'test:ci']) {
    assert.match(pkg.scripts[script], /"\.\.\/apps\/\*\/tests\/\*\.test\.js"/, `${script} collects apps/*/tests`);
  }
  assert.ok(existsSync(join(REPO, 'apps', 'jobboard', 'tests')));
});

// -----------------------------------------------------------------------------
// Review round (v133): openThreadFromApp, run for real. Its three functions
// are lifted out of main.js verbatim and evaluated against stubs — the whole
// of main.js cannot be imported here (see the header), but these are pure
// enough that the behaviour, not just the text, can be pinned.
// -----------------------------------------------------------------------------

function appOpener({ decoy = false, bots = [], threads = [], apps = {} } = {}) {
  const hintKeyDecl = /const hintKey = \([^;]+;/.exec(MAIN);
  assert.ok(hintKeyDecl, 'main.js declares hintKey');
  const src = [
    hintKeyDecl[0],
    'const pendingHints = new Map();',
    fnBody(MAIN, 'appThreadAllowed') + '\n}',
    fnBody(MAIN, 'openThreadFromApp') + '\n}',
    'return { openThreadFromApp, pendingHints, hintKey };',
  ].join('\n');
  const opened = [];
  const state = { decoy, bots, threads, activeThreadId: null };
  const make = new Function('state', 'appForBot', 'openThread', 'syncAppHook', 'isMobile', 'navigate', src);
  const api = make(state, (b) => apps[b] || null, (id, o) => opened.push([id, o]), () => {}, () => false, () => {});
  return { ...api, opened, state };
}

test('an app hint is keyed by app AND thread, so another app on the same bot cannot consume it', () => {
  const o = appOpener({ apps: { scout: { id: 'jobboard' } } });
  o.openThreadFromApp('thr-1', { botId: 'scout', hint: { job_id: 'j-1' }, appId: 'jobboard' });
  assert.deepEqual(o.pendingHints.get('jobboard|thr-1'), { job_id: 'j-1' });
  assert.equal(o.pendingHints.get('otherapp|thr-1'), undefined, "app B's hook sees nothing");
  assert.equal(o.pendingHints.get('thr-1'), undefined, 'no bare thread-id key any more');
  assert.deepEqual(o.opened, [['thr-1', { botId: 'scout' }]]);
  // With no appId (an older caller), the bot's app is the owner.
  o.openThreadFromApp('thr-2', { botId: 'scout', hint: { job_id: 'j-2' } });
  assert.deepEqual(o.pendingHints.get('jobboard|thr-2'), { job_id: 'j-2' });
  // syncAppHook looks the hint up — and clears it — under the resolved app id.
  const sync = fnBody(MAIN, 'syncAppHook');
  assert.match(sync, /pendingHints\.get\(hintKey\(app\.id, th\.id\)\)/);
  assert.match(sync, /pendingHints\.delete\(hintKey\(app\.id, th\.id\)\)/);
  // Both callers name the app.
  assert.match(fnBody(TOOLS, 'onAppMessage'), /deps\.openThread\(id, \{[^}]*appId: tool\.id \}\)/);
  assert.match(sync, /openThreadFromApp\(id, \{[^}]*appId: app\.id \}\)/);
});

test('Safe Mode: an app may only open a thread whose bot is in the (safe) roster', () => {
  const safe = appOpener({ decoy: true, bots: [{ id: 'alpha' }],
    threads: [{ id: 'thr-a', bot_id: 'Alpha' }, { id: 'thr-s', bot_id: 'scout' }] });
  safe.openThreadFromApp('thr-s', { botId: 'scout', hint: { x: 1 }, appId: 'jobboard' });
  safe.openThreadFromApp('thr-unknown', { botId: 'scout', appId: 'jobboard' });
  safe.openThreadFromApp('thr-unknown', { appId: 'jobboard' });
  // A thread known to belong to a hidden bot is refused even if the app claims a safe one.
  safe.openThreadFromApp('thr-s', { botId: 'alpha', appId: 'jobboard' });
  assert.deepEqual(safe.opened, [], 'nothing opened for a bot this session cannot see');
  assert.equal(safe.pendingHints.size, 0, 'and no hint was parked');
  safe.openThreadFromApp('thr-a', { botId: null, appId: 'x' });
  assert.deepEqual(safe.opened, [['thr-a', { botId: null }]], 'the thread’s own bot is safe (case-blind)');
  // Unlocked: unchanged.
  const open = appOpener({ decoy: false, bots: [], threads: [] });
  open.openThreadFromApp('thr-s', { botId: 'scout', appId: 'jobboard' });
  assert.deepEqual(open.opened, [['thr-s', { botId: 'scout' }]]);
});
