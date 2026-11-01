// 4.3 L2: the old Settings -> Characters roster (main.js renderBotManager / fillDrawer /
// saveRoster, js/bots-edit.js rowControls, js/personality.js, js/characters.js) is gone, and the
// live side effects it carried survive on the 4.2 Characters page (js/characters-page.js):
//
//   - every place that redrew the old roster still repaints the summaries and refreshes the page;
//   - a `bots` frame refreshes the page (the server changed a character somewhere else);
//   - the four capabilities ported in 4.3 F1b stay wired through the page's deps
//     (Reactions / Avatar pool switches, reorder, Tools & more park/unpark, the add-on's
//     ext.bots.rowExtra voice control);
//   - the avatar-pool panel, the connections' bot names and the face picker read state.bots
//     (the signed-in profile's own roster, emptied when the profile goes) instead of bmBots;
//   - System -> Local viewer asks /api/local/roots only for the host tier (A14: Owner-only).

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const STATIC = join(HERE, '..', 'static');
const read = (f) => readFileSync(join(STATIC, f), 'utf8');
const MAIN = read('js/main.js');
const HTML = read('index.html');
const SW = read('sw.js');

const fnBody = (src, head) => {
  const i = src.indexOf(head);
  assert.ok(i >= 0, `${head} exists`);
  // Up to the next top-level function / const at column 0.
  const rest = src.slice(i + head.length);
  const m = rest.search(/\n(?:async function |function |const |let |\/\/ ----)/);
  return head + (m < 0 ? rest : rest.slice(0, m));
};

test('the old roster cluster is gone from main.js', () => {
  for (const name of ['renderBotManager', 'fillDrawer', 'saveRoster', 'doSaveRoster', 'wireDrag', 'pickAvatarFile',
    'openAvatarLightbox', 'botEditCtx', 'characterCtx', 'bmOpenDetails', 'charsReorder', 'EXT_BOT_FIELDS', 'BOT_ROW_FIELDS']) {
    assert.ok(!new RegExp(`\\b${name}\\b`).test(MAIN), `${name} is gone`);
  }
  assert.ok(!/\bbmBots\b/.test(MAIN), 'bmBots is gone: its readers use state.bots');
  assert.ok(!/'bm-list'/.test(MAIN), 'no #bm-list in the dom list or the sign-out wipe');
  assert.ok(!/id="bm-list"/.test(HTML), 'no #bm-list element');
  assert.ok(!/chars-reorder/.test(MAIN), 'no wiring for the Reorder button that never existed');
});

test('personality.js and characters.js are deleted; bot-ids.js keeps isHidden + voiceHash', async () => {
  assert.ok(!existsSync(join(STATIC, 'js', 'personality.js')));
  assert.ok(!existsSync(join(STATIC, 'js', 'characters.js')));
  const ids = await import('../static/js/bot-ids.js');
  assert.equal(ids.isHidden({ hidden: true }), true);
  assert.equal(ids.isHidden({ flags: { hidden: true } }), true);
  assert.equal(ids.isHidden({ hidden: false, flags: { hidden: true } }), false);
  assert.equal(ids.isHidden(null), false);
  assert.equal(ids.voiceHash('a b'), '#voice/voices/a%20b');
  // Nothing imports the deleted modules any more.
  for (const f of ['js/main.js', 'js/my-bots.js', 'js/characters-page.js', 'js/bots-edit.js']) {
    assert.ok(!/from '\.\/(?:characters|personality)\.js/.test(read(f)), f);
  }
});

test('sw.js SHELL: personality.js and characters.js out, bot-ids.js in', () => {
  const shell = SW.slice(SW.indexOf('const SHELL'));
  assert.ok(!/\/static\/js\/personality\.js/.test(shell));
  assert.ok(!/\/static\/js\/characters\.js/.test(shell));
  assert.match(shell, /'\/static\/js\/bot-ids\.js'/);
});

test('bots-edit.js keeps only the Add form', async () => {
  const src = read('js/bots-edit.js');
  for (const name of ['rowControls', 'editPanel', 'editButton', 'removeButton', 'lookupsPill', 'notConnectedLink',
    'backendBadge', 'backendKind', 'personaButton']) {
    assert.ok(!new RegExp(`\\b${name}\\b`).test(src), `${name} is gone`);
  }
  assert.match(src, /export function addBotRow\(/);
});

test('every former renderBotManager call site keeps the summaries + page refresh', () => {
  // Opening Settings, Characters becoming the open section, a language change and a NIM flip.
  assert.match(fnBody(MAIN, 'async function openBotManager('), /paintSettingsSummaries\(\);[\s\S]*refreshCharactersPage\(\);/);
  assert.match(fnBody(MAIN, 'function mountCharactersSection('), /refreshCharactersPage\(\);/);
  // The add-on's rowExtra register / dispose: the page redraws its open Details panels.
  const hook = fnBody(MAIN, 'function botRowExtra(');
  assert.equal((hook.match(/refreshCharactersPage\(\{ extras: true \}\);/g) || []).length, 2);
  assert.ok(!/settingsOpen\('bots'\)/.test(hook));
});

test('the ported capabilities stay wired through the page deps (F1b)', () => {
  const deps = /botRowExtrasFor, isMenuBot, toggleMenuBot, renderSidebar/g;
  assert.ok((MAIN.match(deps) || []).length >= 2, 'mountCharactersPage and initCharactersPage both get them');
  assert.match(MAIN, /import \{[^}]*\bisMenuBot\b[^}]*\btoggleMenuBot\b[^}]*\} from '\.\/menubots\.js/);
  assert.match(MAIN, /function botRowExtrasFor\(bot, onChange\)/);
  assert.match(MAIN, /railDragAllowed\(/);
  assert.match(MAIN, /saveRailOrder\(/);
});

test('a bots frame refreshes the Characters page (spy through the handler)', () => {
  const start = MAIN.indexOf("    case 'bots':");
  const end = MAIN.indexOf("    case 'thread_created':", start);
  assert.ok(start > 0 && end > start);
  const body = MAIN.slice(start, end);
  const calls = [];
  const noop = () => {};
  const names = ['state', 'visibleBots', 'isToolId', 'selectBot', 'renderSidebar', 'renderThreads', 'clearChatView',
    'reconcileIdentity', 'applyAuthChrome', 'settingsRoute', 'changedBotLooks', 'updateThreadListHeader',
    'renderChatHeader', 'renderMessages', 'refreshCharactersPage'];
  const run = new Function(...names, 'data', `switch (data.type) {\n    case 'hello':\n${body}\n}`);
  const state = { bots: [], selectedBotId: null, started: true, auth: { profile: null }, role: 'admin', activeThread: null };
  const args = [state, () => state.bots, () => false, noop, noop, noop, noop, noop, noop, { apply: noop },
    () => null, noop, noop, noop, (o) => calls.push(o)];
  run(...args, { type: 'bots', bots: [{ id: 'a' }] });
  assert.equal(calls.length, 1, 'one refresh per bots frame');
  assert.deepEqual(state.bots, [{ id: 'a' }]);
  // A frame without a roster (a hello on a socket that lists none) leaves the page alone.
  run(...args, { type: 'bots' });
  assert.equal(calls.length, 1);
});

test('avatar pools and connection bot names: household names by one fetch, then state.bots; no roster upload in the face picker', () => {
  // Household templates are not in state.bots (a profile holds copies): one targeted fetch names them.
  assert.match(fnBody(MAIN, 'function renderAvatarPoolPanel('), /const name = botNameOf\(bid, apNames\);/);
  assert.match(MAIN, /botName: \(id\) => botNameOf\(id, names\),/);
  assert.match(MAIN, /const botNameOf = \(id, names\) => \(names && names\.get\(id\)\) \|\| \(botById\(id\) \|\| \{\}\)\.name \|\| id;/);
  assert.match(fnBody(MAIN, 'async function householdNames('), /if \(!sessionCan\('manage_characters'\)\) return new Map\(\);/);
  // Wiped with the pools when the profile goes.
  assert.match(MAIN, /apPools = null;\n  apNames = new Map\(\);/);
  // The face picker only answers chooseFace(): the roster's direct household upload is gone.
  assert.match(MAIN, /function openCropModal\(file, name, hint, generated = false\)/);
  assert.ok(!/crop\.botId/.test(MAIN));
});

test('signing out empties the roster: the next profile never sees the last one\'s bots', () => {
  const reboot = fnBody(MAIN, 'async function reboot(');
  assert.match(reboot, /state\.bots = \[\];/);
  assert.ok(reboot.indexOf('state.bots = [];') < reboot.indexOf('await startApp();'));
});

test('System -> Local viewer is the host tier\'s only (A14: /api/local/roots is Owner-only)', () => {
  assert.match(MAIN, /const isHostRole = \(\) => canAdmin\(\) && !!state\.auth\.protected;/);
  assert.match(MAIN, /if \(filesGroup\) filesGroup\.hidden = !isHostRole\(\);/);
  assert.match(MAIN, /if \(isHostRole\(\) && filesBody\) filesBody\.append\(viewerSection\(\)\);/);
  // Health stays every Admin's: only a non-Admin loses the dashboard on a profile change.
  assert.match(MAIN, /if \(!canAdmin\(\)\) unmountDashboard\(\);/);
  // The roots call itself lives only inside viewerSection().
  assert.equal((MAIN.match(/api\.localRoots\(\)/g) || []).length, 1);
  assert.match(fnBody(MAIN, 'function viewerSection('), /api\.localRoots\(\)/);
});

test('the dead "owner" role operand is gone (the server sends admin | user | basic)', () => {
  for (const f of ['js/bot-editor.js', 'js/settings_schema.js', 'js/people.js']) {
    assert.ok(!/role === 'owner'/.test(read(f)), f);
  }
});

test('unreferenced exports are gone', () => {
  const gone = {
    'js/clients.js': ['closeClientsModal'], 'js/llm.js': ['closeLlmPanel', 'slugForBot'],
    'js/reactions.js': ['botHasReactions'], 'js/profiles.js': ['wsErrorKind', 'isBasicRole', 'roleOptions', 'canManage', 'ROLES'],
    'js/newchat.js': ['LAST_CHARACTER_KEY'], 'js/settings_schema.js': ['groupOrder'], 'js/thread-pager.js': ['THREAD_PAGE'],
    'js/util.js': ['resetInert'], 'js/quick-actions.js': ['_reset'], 'js/tools.js': ['_resetForTest'],
    'js/w5-api.js': ['createCharacter', 'pluginManifests', 'setExtensionEnabled'],
    'js/my-bots.js': ['renderMyBots', 'botRow', 'offeredCopies', 'deleteMessage'],
    '../../apps/jobboard/static/thread.js': ['USED_KEYS'],
  };
  for (const [f, names] of Object.entries(gone)) {
    const src = read(f);
    for (const n of names) assert.ok(!new RegExp(`export (?:const|function|async function) ${n}\\b`).test(src), `${f}: ${n}`);
  }
  assert.ok(!/export const STATES\b/.test(read('js/extensions.js')));
  const api = read('js/api.js');
  for (const n of ['myPersonas', 'myBackends', 'avatarPoolConfig']) assert.ok(!new RegExp(`\\b${n}:`).test(api), n);
  // The contract-named wrappers stay (capabilities.py ui=).
  for (const n of ['rememberConfig', 'allBots', 'patchBot', 'deleteBot', 'saveOrder', 'uploadAvatar']) assert.match(api, new RegExp(`\\b${n}:`));
});
