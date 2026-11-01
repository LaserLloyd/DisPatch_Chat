// The rail Switch profile / "Sign out to <default profile>" round (sw v196), test pass v197, merged v198.
// Each assertion below failed on 0dfc54d and pins one fix found by driving the app.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const STATIC = join(dirname(fileURLToPath(import.meta.url)), '..', 'static');
const read = (p) => readFileSync(join(STATIC, p), 'utf8');
const MAIN = read('js/main.js');
const CSS = read('app.css');

test('the chip menu and the command palette offer the lock under the rail button\'s rule and name', () => {
  // Was: chip menu said "Lock" (and the palette "Lock now") while the rail said "Go to DisPatch".
  assert.match(MAIN, /function lockOffered\(\)/);
  assert.match(MAIN, /function lockLabel\(/);
  assert.match(MAIN, /if \(lockOffered\(\)\) items\.push\(\{ label: lockLabel\('chip\.lock'\), onSelect: \(\) => lockNow\(\) \}\);/);
  assert.match(MAIN, /if \(lockOffered\(\)\) a\.push\(\{ icon: RAIL_ICONS\.lock, label: lockLabel\('cmdk\.action_lock'\)/);
  assert.match(MAIN, /btn\.classList\.toggle\('hidden', !lockOffered\(\)\);/);
  assert.doesNotMatch(MAIN, /label: t\('chip\.lock'\)/);
});

test('Settings -> People changes repaint the rail (Default profile, profiles added or removed)', () => {
  // Was: picking "The profile list" left the rail saying "Go to DisPatch" until a reload.
  const call = MAIN.slice(MAIN.indexOf('await mountPeopleSettings(pane, {'), MAIN.indexOf('mountSchemaHosts(pane);'));
  assert.match(call, /onChanged: async \(\) => \{ if \(await refreshAuthQuiet\(\)\) \{ paintProfileChrome\(\); paintLockButton\(\); \} \}/);
});

test('popup rows and the Arrange dialog name Switch profile and Go to <name> in full', () => {
  assert.match(MAIN, /btn\.setAttribute\('data-menu-label', label\);/);
  assert.match(MAIN, /dom\['switch-profile'\]\.setAttribute\('data-menu-label', t\('chip\.switch'\)\)/);
  assert.match(MAIN, /b\.dataset\.menuLabel \|\| b\.dataset\.label/);
  assert.match(CSS, /\.app \.tools-menu \.gear-btn\.rail-menu-item\[data-menu-label\]::after \{ content: attr\(data-menu-label\); \}/);
});

test('phone bar: the Switch caption sits on the same line as its neighbours', () => {
  // A lone .rail-icon has margin:auto (the caption goes to the foot); the glyph pair needs it too.
  assert.match(CSS, /\.gear-btn > \.switch-glyphs \{ margin: auto; \}/);
});

test('desktop rail: Settings stays pinned above Switch profile when the column scrolls', () => {
  assert.doesNotMatch(CSS, /> #manage-bots \{ position: static; box-shadow: none; \}/);
  assert.match(CSS, /\.gear-row:has\(> \.switch-profile-btn:not\(\.hidden\)\) > #manage-bots \{ bottom: 34px; \}/);
});

test('locale text: the Default profile hint names the no-Admin rule; the landing toast names Switch profile', () => {
  const en = JSON.parse(read('locales/en.json'));
  assert.match(en.lock.target_hint, /is not an Admin/);
  assert.doesNotMatch(en.lock.landed, /Tap Profiles/);
  for (const lang of ['ar', 'de', 'es', 'fr', 'ja', 'pt', 'zh']) {
    const d = JSON.parse(read(`locales/${lang}.json`));
    assert.ok(d.lock.landed.includes(d.chip.switch), `${lang}: landed names "${d.chip.switch}"`);
  }
});

test('the lock button shows ONE glyph: a lock (profile list) or a house (default profile)', () => {
  // `.gear-btn .rail-icon { display: block }` (later, same weight) beat `.lock-now-btn .home-glyph`.
  assert.match(CSS, /\.gear-btn\.lock-now-btn \.home-glyph, \.gear-btn\.lock-now-btn\.to-default \.lock-glyph \{ display: none; \}/);
  assert.match(CSS, /\.gear-btn\.lock-now-btn\.to-default \.home-glyph \{ display: block; \}/);
  const hide = CSS.indexOf('.gear-btn.lock-now-btn .home-glyph');
  const block = CSS.indexOf('.gear-btn .rail-icon {');
  assert.ok(hide > 0 && block > 0, 'both rules exist');
});
