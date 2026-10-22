// 4.0 chat-first profiles, the app shell (WP-FE-CORE): no Restricted/decoy tier left in the
// shell, the sign-in / lock-target flow, the rail's Mine / Household groups, per-profile menu
// placements, and the wizard's People step.

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const read = (f) => readFileSync(join(HERE, '..', 'static', f), 'utf8');
const MAIN = read('js/main.js');

const store = new Map();
globalThis.localStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => { store.set(k, String(v)); },
  removeItem: (k) => { store.delete(k); },
};

test('no decoy / Restricted / device-binding / companions code is left in the shell modules', () => {
  const gone = /state\.decoy|auth\.decoy|\bgoSafe\b|anonymousAccess|anonymous_access|openCompanions|renderCompanions|bindDevice|bindableProfiles|openRestrictedPanel|paintAccessNotice|hiddenHere|hide_unlocked|\.safe\b/;
  for (const f of ['js/main.js', 'js/signin.js', 'js/profiles.js', 'js/api.js', 'js/menubots.js', 'js/wizard.js', 'js/nim.js']) {
    const m = gone.exec(read(f).replace(/\/\/[^\n]*/g, ''));
    assert.equal(m, null, `${f} still mentions ${m && m[0]}`);
  }
  // (profiles.js keeps the budgets form helpers only until people.js stops importing them.)
  for (const f of ['js/main.js', 'js/signin.js', 'js/api.js']) assert.doesNotMatch(read(f), /time_limit|quiet_hours/, f);
  const html = read('index.html');
  for (const id of ['unlock-btn', 'drop-btn', 'companions-backdrop', 'drop-backdrop', 'access-notice']) {
    assert.ok(!html.includes(`id="${id}"`), `${id} left with its screen`);
  }
});

test('lock lands on the lock target: sign out, re-read the status, reboot (gate or shared landing)', () => {
  const fn = MAIN.slice(MAIN.indexOf('async function lockToTarget('), MAIN.indexOf('async function lockNow('));
  assert.match(fn, /await api\.lock\(\)/);
  assert.match(fn, /refreshAuthQuiet\(\)/);
  assert.match(fn, /await reboot\(\);/);
  assert.match(fn, /if \(!await refreshAuthQuiet\(\)\) \{ state\.auth\.signedIn = false; state\.auth\.profile = null; \}/, 'fail closed when the status cannot be read');
  assert.match(MAIN, /const gateActive = \(\) => !!\(state\.auth\.claimed && !state\.auth\.signedIn && !state\.auth\.profile\);/);
  assert.match(MAIN, /if \(gateActive\(\)\) \{ enterGate\(\); return; \}\s+\/\/ Otherwise the app starts/);
});

test('a hello for a different profile re-reads the status; it never signs anybody out', () => {
  assert.match(MAIN, /sockProfile !== pageProfile\) \{ reconcileIdentity\(\); break; \}/);
  const fn = MAIN.slice(MAIN.indexOf('async function reconcileIdentity('), MAIN.indexOf('async function lockToTarget('));
  assert.doesNotMatch(fn, /api\.lock/);
});

test('the wizard tells the app when the first Admin exists', () => {
  assert.match(MAIN, /onAccountCreated: async \(\) => \{ await refreshAuthQuiet\(\)/);
  assert.match(read('js/wizard.js'), /ctx\.onAccountCreated/);
});

test('the rail draws Mine and Household groups from visibleBots()', () => {
  assert.match(MAIN, /const g = railGroups\(state\.bots\);\s+return \[\.\.\.g\.mine, \.\.\.g\.household\];/);
  assert.match(MAIN, /const grouped = hasMine && hasHousehold;/);
  assert.match(MAIN, /draggable: canAdmin\(\) \? 'true' : 'false'/);
});

test('menu placements are kept per profile in the one storage key, and never pruned by another profile', async () => {
  store.clear();
  const M = await import('../static/js/menubots.js');
  M.setMenuScope('ada');
  M.toggleMenuBot('a'); M.toggleMenuBot('b');
  M.setMenuScope('uma');
  assert.deepEqual([...M.menuBotIds()], [], "Uma starts with none of Ada's");
  M.toggleMenuBot('c');
  M.pruneMenuBots(['c']);                       // Uma's roster: only c
  M.setMenuScope('ada');
  assert.deepEqual([...M.menuBotIds()].sort(), ['a', 'b'], "Uma's roster never prunes Ada's placements");
  M.pruneMenuBots([]);                          // roster not arrived yet: nothing is dropped
  assert.equal(M.menuBotIds().size, 2);
  assert.deepEqual(Object.keys(JSON.parse(store.get('dispatch-menu-bots'))).sort(), ['ada', 'uma']);
  store.set('dispatch-menu-bots', JSON.stringify(['x']));   // the pre-4.0 shape: a plain list
  assert.deepEqual([...M.menuBotIds()], ['x']);
});

test('the wizard People step builds User and Basic rows, with an optional PIN', async () => {
  const W = await import('../static/js/wizard.js');
  assert.deepEqual(W.familyPayload([{ role: 'user', name: ' A ', pin: '1234' }, { role: 'basic', name: 'B', pin: '' }, { role: 'user', name: ' ', pin: '' }]),
    { people: [{ name: 'A', role: 'user', pin: '1234' }, { name: 'B', role: 'basic' }] });
  const src = read('js/wizard.js');
  assert.doesNotMatch(src, /member|restricted|animals|assistants/);
});
