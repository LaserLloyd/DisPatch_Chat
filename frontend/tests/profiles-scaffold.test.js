// 4.0 chat-first profiles, Foundation scaffolding (BUILD-PLAN-v2 §2.2 step 10): the
// shapes the frontend packages build on. ICON_MAP names a ThemeForge icon for every
// RAIL_ICONS key; uiIcon() draws today's icon (no behaviour change); the four new
// modules are inert and wired once; SECTION_HOOKS survives a failing hook.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { domSkip } from './_require-dom.js';
import { ICON_MAP, RAIL_ICONS, uiIcon } from '../static/js/util.js';
import { SECTION_HOOKS, runSectionHooks } from '../static/js/settings-nav.js';
import * as myBots from '../static/js/my-bots.js';
import * as botEditor from '../static/js/bot-editor.js';
import * as accountReset from '../static/js/account-reset.js';
import * as peoplePerms from '../static/js/people-perms.js';

const STATIC = join(dirname(fileURLToPath(import.meta.url)), '..', 'static');
let jsdom = null;
try { jsdom = await import('jsdom'); } catch { /* skip the DOM half */ }

test('ICON_MAP has exactly the 67 RAIL_ICONS keys', () => {
  assert.deepEqual(Object.keys(ICON_MAP).sort(), Object.keys(RAIL_ICONS).sort());
  assert.equal(Object.keys(ICON_MAP).length, 67);
  for (const [k, v] of Object.entries({ help: 'circle-question-mark', user: 'user', brain: 'brain', eye: 'eye' })) {
    assert.equal(ICON_MAP[k], v);
  }
  assert.ok(Object.isFrozen(ICON_MAP));
});

test('the four profile modules export inert init/render entry points', async () => {
  assert.equal(myBots.initMyBots({}), undefined);
  assert.equal(myBots.renderMyBots(null, {}), undefined);
  assert.equal(botEditor.initBotEditor({}), undefined);
  assert.equal(await botEditor.renderBotEditor(null, null, {}), null);
  assert.equal(accountReset.initAccountReset({}), undefined);
  assert.equal(accountReset.renderAccountReset(null, {}), undefined);
  assert.equal(peoplePerms.initPeoplePerms({}), undefined);
  assert.equal(peoplePerms.renderPeoplePerms(null, {}), undefined);
});

test('SECTION_HOOKS names the sections and a failing hook stops nothing', () => {
  assert.deepEqual(Object.keys(SECTION_HOOKS).sort(), ['account', 'mybots', 'people']);
  // (settings-nav imports them with ?v=, a separate module record: compare by name)
  assert.equal(SECTION_HOOKS.account[0].name, 'renderAccountReset');
  assert.equal(SECTION_HOOKS.people[0].name, 'renderPeoplePerms');
  assert.equal(SECTION_HOOKS.mybots[0].name, 'renderMyBots');
  const warn = console.warn;
  console.warn = () => {};
  try {
    assert.doesNotThrow(() => runSectionHooks('nope', null));
    assert.doesNotThrow(() => runSectionHooks('account', null, {}));
  } finally { console.warn = warn; }
});

test('main.js wires the four modules once and index.html loads profiles.css', () => {
  const main = readFileSync(join(STATIC, 'js', 'main.js'), 'utf8');
  for (const name of ['initMyBots', 'initBotEditor', 'initAccountReset', 'initPeoplePerms']) {
    assert.equal(main.split(`import { ${name} }`).length - 1, 1, name);
  }
  assert.equal(main.split('runSectionHooks(id, pane').length - 1, 1);
  const html = readFileSync(join(STATIC, 'index.html'), 'utf8');
  assert.match(html, /<link rel="stylesheet" href="\/static\/profiles\.css\?v=\d+" \/>/);
  const css = readFileSync(join(STATIC, 'profiles.css'), 'utf8');
  for (const pkg of ['WP-FE-CORE', 'WP-FE-YOU', 'WP-FE-PEOPLE', 'WP-UIREV', 'WP-SCALE-FE']) {
    assert.ok(css.includes(`===== ${pkg}:`), pkg);
  }
  const sw = readFileSync(join(STATIC, 'sw.js'), 'utf8');
  assert.ok(sw.includes("'/static/profiles.css'"));
});

test('uiIcon draws the ThemeForge sprite icon, labelled or decorative', { skip: domSkip(jsdom ? false : 'jsdom is not installed') }, () => {
  const win = new jsdom.JSDOM('<!doctype html><html><body></body></html>').window;
  const prev = globalThis.document;
  globalThis.document = win.document;
  try {
    const plain = uiIcon('gear');
    assert.equal(plain.getAttribute('aria-hidden'), 'true');
    assert.equal(plain.dataset.icon, 'gear');
    assert.equal(plain.querySelector('use').getAttribute('href'), '/static/ui-theme/icons.svg#settings');
    const named = uiIcon('gear', { label: 'Settings', size: 20 });
    assert.equal(named.getAttribute('aria-hidden'), null);
    assert.equal(named.getAttribute('role'), 'img');
    assert.equal(named.getAttribute('aria-label'), 'Settings');
    assert.equal(named.getAttribute('width'), '20');
    assert.equal(uiIcon('no-such-icon'), null);
  } finally {
    globalThis.document = prev;
  }
});
