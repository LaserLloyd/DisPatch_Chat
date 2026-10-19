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

test('app.css: the desktop rail collapses to one button; the phone gets one row and a bottom sheet', () => {
  const css = read('app.css').replace(/\/\*[\s\S]*?\*\//g, '');
  assert.match(css, /\.tool-list\.tool-list-compact \{/);
  // Phone: no tool grid at the bots' size any more — one row, one sheet.
  assert.doesNotMatch(css, /\.app\[data-view="bots"\] \.tool-list \{[^}]*display: grid/);
  assert.match(css, /\.app\[data-view="bots"\] \.tool-list\.tool-list-row \{[^}]*display: block/);
  assert.match(css, /\.tools-menu\.tools-sheet \{[^}]*bottom: 0/);
  assert.match(css, /body\.tools-sheet-open::after \{[^}]*position: fixed/);
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

test('Settings → Plugins is an admin-only tab; the Tools table, its host and Save live in it', () => {
  assert.match(MARKUP, /class="settings-tab admin-only" id="stab-plugins"[^>]*data-tab="plugins"/);
  assert.match(MARKUP, /id="spane-plugins"/);
  // The old standalone Tools tab and pane are gone.
  assert.doesNotMatch(MARKUP, /id="stab-tools"|id="spane-tools"/);
  assert.match(MARKUP, /data-tab="plugins">\s*<button class="btn-primary" id="tools-save"/);
  assert.match(MAIN, /const ADMIN_TABS = \[[^\]]*'plugins'[^\]]*\];/);
  assert.match(MAIN, /const SETTINGS_TABS = \[[^\]]*'plugins'[^\]]*\];/);
  // main.js hands the Plugins pane the Tools mounter.
  assert.match(MAIN, /mountPluginsSettings\(pane, \{\s*mountTools: \(host\) => mountToolsSettings\(host\)/);
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

test('the sw.js CACHE is local-chat-v177 (3.0.0, W5 + W11b, the api.js importer bumps, then the 2026-10-19 permission orders)', () => {
  const m = /const CACHE = '([^']+)'/.exec(SW);
  assert.ok(m, 'sw.js must declare CACHE');
  // v176: review fixes on the v175 round (phone sheet + profile menu, drag MIME, card timing, wording).
  // v175: Image Studio sort/clear, the Settings host-storage notice + first-load card,
  // Tools & more (railmenu.js), upload in the quick-actions menu, drop files on a chat.
  // v154: 2.3.1 review — llm.js 9→10 (Test sends target + api_key_env),
  // plugins.js 2→3 (restart hint rule), main.js 130→131 (settings.pane guard).
  // v153: 2.3.1 — about.js 8→9 (safe bots get voice).
  // v152: 2.3.0 — quick-actions.js, plugins.js and bots-edit.js join SHELL,
  // the Tools tab folds into Plugins, add-in tabs (main.js 128→129, api.js
  // 32→33, extensions.js 1→2, ext-sdk.js 2→3, voice-absent.js 1→2, app.css
  // 99→100, index.html, locales).
  // v151: Web Push — js/push.js joins SHELL, push + notificationclick
  // handlers (main.js 127→128, index.html, locales).
  // v150: password fields wrapped in <form data-no-submit>; advisor bots hide
  // the thinking row (main.js 126→127, index.html).
  // v149: 2.2.0 final review — js/voice-heard.js joins SHELL (the "not heard in
  // voice chat" marker), ext-sdk.js 1→2 (shell.activity), main.js 125→126,
  // app.css 98→99, index.html, locales.
  // v148: 2.2.0 — voice chat left the shell for the DisPatch Voice add-on:
  // drive.js, voice-worklet.js, voice.css, drive-settings.js, voices.js and
  // voice-tools.js leave SHELL; js/extensions.js, js/ext-sdk.js and
  // js/voice-absent.js join it; /ext/ is network-only; i18n.js 3→4
  // (mergeBundle) and every importer, tools.js 11→12, about.js 6→7
  // (APP_VERSION 2.2.0), main.js 124→125, app.css 97→98, index.html, locales.
  // v147: voice is optional (Settings → Voice opens with an on/off switch)
  // and its advanced pieces are opt-in tools — new js/voice-tools.js joins
  // SHELL; drive-settings.js 3→4, voices.js 3→4, drive.js 5→6, voice.css
  // 3→4, tools.js 10→11, about.js 5→6, main.js 123→124, app.css 96→97,
  // index.html, locales.
  // v146: Drive settings + the Voices manager move from Settings → Device to
  // their own Voice tab — drive-settings.js 2→3, voices.js 2→3, drive.js 4→5,
  // voice.css 2→3, main.js 122→123, app.css 95→96, index.html, locales.
  // v145: neutral example names — drive-settings.js 1→2, voices.js 1→2,
  // drive.js 3→4, main.js 121→122, index.html, locales.
  // v144: Drive mode v2 — drive.js 2→3, voice-worklet.js 1→2, voice.css 1→2,
  // js/drive-settings.js + js/voices.js join SHELL; main.js 120→121, app.css
  // 94→95, index.html, locales.
  // v143: 2.1.0 — the four modules that import api.js v32 got their own ?v bumps.
  // v142: 2.1.0 RC review — drive.js 1→2 (reconnect gives up on repeated
  // 1013), Drive-mode strings localised; main.js 118→119, index.html, locales.
  // v141: Drive mode — drive.js, voice-worklet.js, voice.css join SHELL;
  // main.js 117→118, index.html.
  // v140: release 2.1.0 — about.js 4→5 (APP_VERSION), main.js 116→117,
  // index.html.
  // v139: release 2.0.0 — about.js 3→4 (APP_VERSION), main.js 114→115,
  // index.html.
  // v138: public-repo scrub — notice.js 3→4, about.js 2→3, main.js 113→114,
  // index.html.
  // v137: review round — tool fallback-card/menu/Settings CSS, three dead
  // locale keys dropped (main.js 112→113, app.css 92→93, index.html, locales).
  // v136: tools open:window + fallback card, clients site links, Device-tab
  // groups, pin pills (main.js 111→112, app.css 91→92, api.js 30→31, tools.js
  // 8→9, clients.js 8→9, pins.js 12→13, index.html, locales).
  // v135: the phone's Tools row + bottom sheet, Back closes a tool, remote
  // addresses for host-bound tools (tools.js 7→8, main.js 110→111, app.css
  // 90→91, index.html, locales).
  // v134: per-tool layout state bridge (tools.js 6→7, privacy.js 8→9,
  // pins.js 11→12, main.js 109→110, index.html).
  // v133: app hints keyed by app+thread, Safe-Mode guard on an app's
  // open-thread (main.js 108→109, index.html).
  // v132: the desktop rail's Tools group is one button with a popup (tools.js
  // 5→6, menubots.js 1→2, main.js 107→108, app.css 89→90, index.html, every
  // locale).
  // v130: themed tool tiles + the Job Board as a builtin tool. v131: apps —
  // js/app-sdk.js joins SHELL, js/jobs.js + js/job-thread.js leave it, api.js
  // and every importer, main.js, app.css, index.html and every locale moved.
  // v169: W5 - settings schema page, Connections, character details, wizard, backups.
  // v168: ForgeUI 1.2.0 vendored whole (ui-components + locales join SHELL; dispatch-extra.css).
  // The shell is cached by PATH, so an installed client keeps all of the old
  // shell without this bump. If you bump again, bump here too.
  assert.equal(m[1], 'local-chat-v177');
});

test('js/app-sdk.js is precached; the old Job Board modules are not', () => {
  assert.match(SW, /'\/static\/js\/app-sdk\.js'/);
  assert.doesNotMatch(SW, /'\/static\/js\/jobs\.js'|'\/static\/js\/job-thread\.js'/);
});

test('/apps/* and /ext/* are network-only in the service worker', () => {
  const body = SW.slice(SW.indexOf("self.addEventListener('fetch'"));
  assert.match(body, /if \(p\.startsWith\('\/apps\/'\) \|\| p\.startsWith\('\/ext\/'\)\) return;/);
  assert.ok(body.indexOf("p.startsWith('/apps/')") < body.indexOf('e.respondWith('), 'decided before any cache use');
  assert.ok(body.indexOf("p.startsWith('/ext/')") < body.indexOf('e.respondWith('), 'decided before any cache use');
  assert.doesNotMatch(SW.slice(SW.indexOf('const SHELL'), SW.indexOf('];', SW.indexOf('const SHELL'))), /'\/ext\//,
    'no extension file is ever precached');
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
  assert.match(MAIN, /import \{ forApp(, api as appApi)? \} from '\.\/app-sdk\.js\?v=\d+';/);
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
