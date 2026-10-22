// The owner's permission orders of 2026-10-19, frontend half.
//
//   P-2  "Members should have voice": a Member sees Settings -> Voice once the add-on owns the
//        pane, the shell offers can(permission), and a Member collects no 403s for the
//        character editor's roster (/api/bots/all) or the avatar pools when Settings opens

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const STATIC = join(HERE, '..', 'static');
const read = (f) => readFileSync(join(STATIC, f), 'utf8');
const HTML = read('index.html');
const MAIN = read('js/main.js');
const SDK = read('js/ext-sdk.js');

test('Voice is not an admin tab: a Member who holds use_voice gets it once the add-on owns the pane', () => {
  // The admin sections are the household ones (settings-nav.js perm 'admin'); Voice's perm is its own.
  assert.match(MAIN, /const ADMIN_TABS = SECTIONS\.filter\(\(s\) => s\.perm === 'admin'\)/);
  assert.match(read('js/settings-nav.js'), /\{ id: 'voice', group: 'you', icon: 'mic', label: 'settings\.sec_voice', perm: 'voice' \}/);
  assert.match(HTML, /class="settings-tab hidden" id="stab-voice"/);
  assert.match(MAIN, /const voiceTabAllowed = \(\) => \(canAdmin\(\) \|\| \(sessionCan\('use_voice'\) && !!\(paneHost\('voice'\) && paneHost\('voice'\)\.owned\(\)\)\)\);/);
  // Re-decided when the add-on claims or gives back the pane, and on every auth change.
  assert.match(MAIN, /const dispose = host\.claim\(owner, spec\);\s+syncVoiceTab\(\);/);
  assert.match(MAIN, /onChange: \(\) => \{ renderVoicePane\(\); syncVoiceTab\(\); \}/);
  assert.match(MAIN, /\n  syncVoiceTab\(\);\n/);
});

test('sessionCan reads /api/auth/status permissions; an older server falls back to the role', () => {
  assert.match(MAIN, /permissions: Array\.isArray\(s\.permissions\) \? s\.permissions : null,/);
  assert.match(MAIN, /if \(Array\.isArray\(ps\)\) return ps\.includes\('\*'\) \|\| ps\.includes\(perm\);/);
});

test('the shell offers can(permission) to add-ons (never true for a Restricted session)', () => {
  assert.match(SDK, /can: \(permission\) => !!call\('can', permission\),/);
  assert.match(MAIN, /can: \(perm\) => sessionCan\(String\(perm \|\| ''\)\),/);
  assert.match(MAIN, /from '\.\/ext-sdk\.js\?v=18'/);
});

test('a Member asks for neither the full roster nor the avatar pools', () => {
  assert.match(MAIN, /allBots: \(\) => \(sessionCan\('manage_characters'\)\s+\? api\.allBots\(\)\.then\(\(r\) => \(r && r\.bots\) \|\| \[\]\)\s+: Promise\.resolve\(state\.bots\.slice\(\)\)\),/);
  assert.match(MAIN, /async function refreshAvatarPoolPanel\(\) \{\s+if \(!sessionCan\('manage_characters'\)\) return;/);
});
