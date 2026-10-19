// Ops check of the 3.0 security surfaces (2026-10-19), frontend half.
//
//   S-2  the lock screen's recovery form sends a new PIN (the server needs one when other
//        people have profiles) and keeps the code when only the PIN was missing
//   S-3  an Admin opening Settings -> Health got the dashboard's 403 (admin_system) and the
//        dashboard treated ANY 403 as "locked": the Admin's whole UI flipped to Restricted
//   S-4  a Member could not open Settings at all: the gear first loaded the character list
//        (manage_characters, 403) and gave up, so Theme / Device / Security were unreachable
//   S-5  the Device tab asked an Admin's or Member's browser for the local viewer's roots
//        (admin_system) on every open

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { domSkip } from './_require-dom.js';

const HERE = dirname(fileURLToPath(import.meta.url));
const STATIC = join(HERE, '..', 'static');
const read = (f) => readFileSync(join(STATIC, f), 'utf8');
const HTML = read('index.html');
const MAIN = read('js/main.js');

const require = createRequire(import.meta.url);
let jsdom = null;
try { jsdom = require('jsdom'); } catch { /* not installed - DOM tests skip */ }
const dom = { skip: domSkip(jsdom ? false : 'jsdom is not installed') };

// S-3 ---------------------------------------------------------------------------

async function mountWith(status, body) {
  const { JSDOM } = jsdom;
  const win = new JSDOM('<!doctype html><body><div id="pane"></div><div id="foot"></div></body>').window;
  const saved = {};
  for (const k of ['window', 'document', 'HTMLElement', 'Node', 'fetch']) saved[k] = globalThis[k];
  globalThis.window = win; globalThis.document = win.document;
  globalThis.HTMLElement = win.HTMLElement; globalThis.Node = win.Node;
  globalThis.fetch = async () => ({ ok: false, status, statusText: 'Forbidden', json: async () => body });
  let locked = 0;
  try {
    const mod = await import(`../static/js/dashboard.js?case=${status}-${Object.keys(body).join('-')}`);
    await mod.mountDashboard(win.document.getElementById('pane'), win.document.getElementById('foot'), {
      t: (k) => k, toast: () => {}, isDecoy: () => false, onLocked: () => { locked += 1; },
    });
    mod.unmountDashboard();
  } finally {
    for (const [k, v] of Object.entries(saved)) globalThis[k] = v;
  }
  return locked;
}

test('dashboard: a permission 403 (an Admin on the Owner\'s Health tab) is not a lock', dom, async () => {
  const n = await mountWith(403, { detail: 'Your profile does not have permission to do that.',
    required_permission: 'admin_system', error: { code: 'permission_denied' } });
  assert.equal(n, 0);
});

test('dashboard: a real downgrade (403 decoy) still hands off to the lock handler', dom, async () => {
  const n = await mountWith(403, { detail: 'Unlock for full access', decoy: true });
  assert.equal(n, 1);
});

test('main.js imports the fixed dashboard at a new ?v=', () => {
  assert.match(MAIN, /from '\.\/dashboard\.js\?v=10'/);
});

test('main.js: since 2026-10-19 Health is an Admin\'s tab too (Admins hold admin_system)', () => {
  assert.match(MAIN, /const OWNER_TABS = \[\];/);
  assert.match(MAIN, /const isHostRole = \(\) => canAdmin\(\);/);
});

// S-4 ---------------------------------------------------------------------------

test('main.js: Settings opens for a Member without the character list, on a tab they have', () => {
  const fn = MAIN.slice(MAIN.indexOf('async function openBotManager('), MAIN.indexOf('async function closeBotManager('));
  assert.ok(fn.length > 0);
  assert.match(fn, /if \(canAdmin\(\)\) \{\s+try \{\s+const r = await api\.allBots\(\);/);
  assert.match(fn, /bmBots = \[\];\s+if \(CHARACTER_TABS\.includes\(tab\)\) tab = homeSettingsTab\(\);/);
  assert.match(MAIN, /const homeSettingsTab = \(\) => \(state\.decoy \|\| canAdmin\(\) \? 'bots' : 'theme'\);/);
  // A hidden tab falls back to the profile's home tab, not unconditionally to Bots.
  assert.match(MAIN, /btn\.classList\.contains\('hidden'\)\) \{ id = homeSettingsTab\(\);/);
});

// S-5 ---------------------------------------------------------------------------

test('main.js: the local viewer section is built for the Owner and Admins (admin_system)', () => {
  assert.match(MAIN, /if \(isHostRole\(\) && filesBody\) filesBody\.append\(viewerSection\(\)\);/);
  assert.match(MAIN, /if \(filesGroup\) filesGroup\.hidden = !isHostRole\(\);/);
});

// S-2 ---------------------------------------------------------------------------

test('index.html: the recovery form has a New PIN field, labelled', () => {
  assert.match(HTML, /<input type="password" id="recover-new-pin"[^>]*aria-label="New PIN"/);
  assert.match(HTML, /\/static\/js\/main\.js\?v=\d+/);
});

test('main.js: recovery sends new_pin and keeps the code when only the PIN is missing', () => {
  assert.match(MAIN, /'recover-code', 'recover-new-pin',/);
  assert.match(MAIN, /JSON\.stringify\(newPin \? \{ code, new_pin: newPin \} : \{ code \}\)/);
  assert.match(MAIN, /if \(e && e\.code === 'recovery_needs_new_pin'\) \{/);
});
