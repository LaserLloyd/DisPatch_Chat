// Settings 3.2 (js/settings-nav.js): the seven sections, the old tab ids that
// still lead to them (the compatibility contract for add-ons, pins and links),
// the settings search, and the shell wiring in main.js / index.html.

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import {
  SECTIONS, SECTION_IDS, TAB_ALIASES, RESERVED_TAB_IDS, resolveSettingsTarget, STATIC_ROWS,
  schemaTarget, buildSearchIndex, searchSettings,
} from '../static/js/settings-nav.js';

const HERE = dirname(fileURLToPath(import.meta.url));
const STATIC = join(HERE, '..', 'static');
const read = (f) => readFileSync(join(STATIC, f), 'utf8');
const HTML = read('index.html');
const MAIN = read('js/main.js');
const EN = JSON.parse(read('locales/en.json'));
const get = (o, k) => k.split('.').reduce((a, p) => (a == null ? a : a[p]), o);

test('seven sections in two groups (plus Voice), in nav order', () => {
  assert.deepEqual(SECTION_IDS, ['appearance', 'account', 'voice', 'characters', 'connections', 'people', 'plugins', 'system']);
  assert.deepEqual(SECTIONS.filter((s) => s.group === 'you').map((s) => s.id), ['appearance', 'account', 'voice']);
  assert.deepEqual(SECTIONS.filter((s) => s.group === 'household').map((s) => s.id),
    ['characters', 'connections', 'people', 'plugins', 'system']);
  for (const s of SECTIONS) assert.ok(get(EN, s.label), `${s.id}: ${s.label} is in en.json`);
});

test('every tab id Settings ever had resolves to a section (and keeps its subsection)', () => {
  const old = ['bots', 'theme', 'device', 'voice', 'plugins', 'connections', 'agents', 'ai', 'reactions', 'health', 'people', 'security', 'settings'];
  const expect = {
    bots: ['characters', ''], theme: ['appearance', ''], device: ['appearance', ''], voice: ['voice', ''],
    plugins: ['plugins', ''], connections: ['connections', ''], agents: ['connections', 'agents'], ai: ['connections', ''],
    reactions: ['characters', 'reactions'], health: ['system', ''], people: ['people', ''], security: ['account', 'signin'],
    settings: ['system', ''],
  };
  for (const id of old) {
    const r = resolveSettingsTarget(id);
    assert.deepEqual([r.id, r.anchor], expect[id], id);
    assert.ok(SECTION_IDS.includes(r.id), id);
  }
  // A caller's own anchor wins over the alias's.
  assert.deepEqual(resolveSettingsTarget('security#notice'), { id: 'account', anchor: 'notice' });
  assert.deepEqual(resolveSettingsTarget('people#key:people.lock_timeout_seconds'), { id: 'people', anchor: 'key:people.lock_timeout_seconds' });
  // An add-in tab id is itself; nothing is no section.
  assert.deepEqual(resolveSettingsTarget('weather-tab'), { id: 'weather-tab', anchor: '' });
  assert.deepEqual(resolveSettingsTarget(''), { id: null, anchor: '' });
  assert.deepEqual(resolveSettingsTarget(null), { id: null, anchor: '' });
});

test('no add-in tab may claim a section id or an old tab id', () => {
  for (const id of [...SECTION_IDS, ...Object.keys(TAB_ALIASES)]) assert.ok(RESERVED_TAB_IDS.has(id), id);
  assert.match(MAIN, /const CORE_SETTINGS_TABS = RESERVED_TAB_IDS;/);
});

test('every literal section target in the app resolves (openSettingsTab / setSettingsTab / openTab)', () => {
  const files = readdirSync(join(STATIC, 'js')).filter((f) => f.endsWith('.js')).map((f) => read(`js/${f}`));
  files.push(readFileSync(join(STATIC, '..', '..', 'apps', 'imagestudio', 'static', 'studio.js'), 'utf8'));
  const targets = new Set();
  for (const src of files) {
    for (const m of src.matchAll(/(?:openSettingsTab|setSettingsTab|openTab|openShellSettings)\(\s*'([^']+)'/g)) targets.add(m[1]);
  }
  assert.ok(targets.size > 5);
  for (const t of targets) {
    const { id } = resolveSettingsTarget(t);
    assert.ok(SECTION_IDS.includes(id), `${t} resolves to ${id}`);
  }
});

test('the voice add-on\'s settings.open(\'voice\') and shell.settings.pane(\'voice\') still land on Voice', () => {
  assert.deepEqual(resolveSettingsTarget('voice'), { id: 'voice', anchor: '' });
  assert.match(HTML, /id="spane-voice"[^>]*>\s*<div class="ext-pane" id="ext-pane-voice"><\/div>/);
  assert.match(MAIN, /openSettings: \(id\) => openSettingsTab\(id\)/);
  // The add-on's per-character control saves at once now, like the switches beside it.
  assert.match(MAIN, /const extras = botRowExtrasFor\(cur\(\), \(patch\) => \{[\s\S]{0,400}if \(any\) saveRoster\(/);
});

test('every static search row names a section, an anchor that exists in the markup or a module, and a real label', () => {
  for (const r of STATIC_ROWS) {
    assert.ok(SECTION_IDS.includes(r.section), r.section);
    assert.ok(get(EN, r.label), `${r.label} is in en.json`);
  }
  const anchors = new Set([...HTML.matchAll(/data-anchor="([^"]+)"/g)].map((m) => m[1]));
  // Anchors set by modules at mount time.
  for (const a of ['people', 'device', 'machine', 'activity', 'signin-rules', 'restricted-limits', 'addons', 'tools', 'lookups',
    'discover', 'hidden-bots']) anchors.add(a);
  for (const r of STATIC_ROWS) if (r.anchor) assert.ok(anchors.has(r.anchor), `anchor ${r.anchor}`);
});

test('search: static rows and schema rows, only what this session may see, label matches first', () => {
  const schema = { settings: [
    { key: 'people.lock_timeout_seconds', label: 'Owner and Admin auto-lock', description: 'How long a session may sit idle.', group: 'people', ui_section: 'people#signin-rules' },
    { key: 'system.backup_keep', label: 'Snapshots to keep', description: 'How many.', group: 'system', ui_section: 'system#backups' },
  ] };
  const t = (k) => get(EN, k) || k;
  const label = (id) => t(SECTIONS.find((s) => s.id === id).label);
  const owner = buildSearchIndex({ t, schema, sectionLabel: label });
  const hits = searchSettings(owner, 'auto-lock');
  assert.equal(hits[0].section, 'people');
  assert.equal(hits[0].anchor, 'key:people.lock_timeout_seconds');
  assert.equal(hits[0].where, 'People & Access');
  assert.ok(searchSettings(owner, 'privacy').some((h) => h.section === 'account' && h.anchor === 'notice'));
  assert.ok(searchSettings(owner, 'snapshots')[0].anchor === 'key:system.backup_keep');
  assert.deepEqual(searchSettings(owner, '   '), []);
  assert.deepEqual(searchSettings(owner, 'zzzz qqqq'), []);
  // A Member: no household sections, no schema rows.
  const member = buildSearchIndex({ t, schema, sectionLabel: label, allowed: (s, perm) => perm !== 'admin' && ['appearance', 'account', 'voice'].includes(s) });
  assert.deepEqual(searchSettings(member, 'auto-lock'), []);
  assert.ok(searchSettings(member, 'theme').length >= 1);
  assert.equal(searchSettings(member, 'hidden characters').length, 0, 'the hidden-bots row is an admin one');
  // Accents fold: "théme" finds Theme.
  assert.ok(searchSettings(owner, 'théme').length >= 1);
});

test('schemaTarget: ui_section wins, else the group\'s section', () => {
  assert.deepEqual(schemaTarget({ ui_section: 'people#signin-rules' }), { id: 'people', anchor: 'signin-rules' });
  assert.deepEqual(schemaTarget({ group: 'tools' }), { id: 'plugins', anchor: '' });
  assert.deepEqual(schemaTarget({ group: 'nope' }), { id: 'system', anchor: '' });
});

test('index.html: a vertical tablist with group headings, a search box and a back button; no tab strip', () => {
  assert.match(HTML, /<div class="settings-tabs" id="settings-tabs" role="tablist" aria-orientation="vertical"/);
  for (const g of ['you', 'household', 'addons']) assert.match(HTML, new RegExp(`data-i18n="settings\\.group_${g}"`));
  assert.match(HTML, /<input type="search" class="ui-input settings-search-input" id="settings-search"[^>]*role="combobox"/);
  assert.match(HTML, /id="settings-search-results" role="listbox"/);
  assert.match(HTML, /id="settings-back" hidden/);
  assert.doesNotMatch(HTML, /stab-prev|stab-next|settings-tabbar/);
  // Every section a phone list shows carries a one-line summary slot.
  for (const id of SECTION_IDS) assert.match(HTML, new RegExp(`id="stab-${id}"[\\s\\S]{0,400}class="stab-summary"`), id);
  // No footer: every change applies when it is made.
  assert.doesNotMatch(HTML, /id="bm-footer"|id="bm-done"|id="tools-save"|id="llm-save"/);
});

test('main.js: phone list <-> section with history, last section and Advanced per device', () => {
  assert.match(MAIN, /settingsPaneHistoryPop = historyPush\(null, 'settings-pane', \(\) => \{/);
  assert.match(MAIN, /modal\.dataset\.view = settingsListView \? 'list' : 'pane';/);
  assert.match(MAIN, /const SETTINGS_LAST_KEY = 'dispatch-settings-last';/);
  assert.match(MAIN, /const SETTINGS_ADV_KEY = 'dispatch-settings-adv';/);
  // Leaving a section for the list stops what it runs (the dashboard poll).
  const back = MAIN.slice(MAIN.indexOf('function backToSettingsList('), MAIN.indexOf('async function setSettingsTab('));
  assert.match(back, /deactivateSettingsTab\(\);/);
  assert.match(MAIN, /if \(settingsTab === 'system'\) unmountDashboard\(\);/);
  // Storage is wrapped: private mode never throws.
  assert.match(MAIN, /try \{ localStorage\.setItem\(SETTINGS_LAST_KEY, id\); \} catch/);
});

test('app.css: the side nav is a desktop column; under 900px the nav is the page', () => {
  const css = read('app.css');
  assert.match(css, /\.settings-nav \{\n  flex: 0 0 224px;/);
  assert.match(css, /@media \(max-width: 899px\) \{\n  \.settings-body \{ display: block; position: relative; \}\n  \.settings-modal\[data-view="list"\] \.settings-panes \{ display: none; \}\n  \.settings-modal\[data-view="pane"\] \.settings-nav \{ display: none; \}/);
  // Mirrors under RTL (logical properties, flipped chevrons).
  assert.match(css, /border-inline-end: 1px solid var\(--border-strong\);/);
  assert.match(css, /\[dir="rtl"\] \.settings-back svg \{ transform: scaleX\(-1\); \}/);
});

test('Account: one sign-in change at a time, by role; a Member changes their own PIN; paths only for the Owner', () => {
  const fn = MAIN.slice(MAIN.indexOf('function accountChoices()'), MAIN.indexOf('let accountChoice ='));
  assert.match(fn, /out\.push\(a\.pinSet \? 'pin_change' : 'pin_set'\);/);
  assert.match(fn, /if \(a\.claimed\) out\.push\(a\.needsPassword \? 'pw_set' : 'pw_change'\);/);
  assert.match(fn, /if \(a\.pinSet\) out\.push\('pin_remove'\);/);
  assert.match(fn, /\} else if \(state\.role === 'admin'\) \{\s+out\.push\(a\.needsPassword \? 'pw_set' : 'pw_change'\);/);
  assert.match(fn, /\} else if \(state\.role === 'member'\) \{\s+out\.push\('my_pin'\);/);
  // The PIN form and the password form never show together.
  const open = MAIN.slice(MAIN.indexOf('function openAccountForm('), MAIN.indexOf('function activateSecurityPane('));
  assert.match(open, /pinForm\.hidden = !pinModes\.includes\(accountChoice\);/);
  assert.match(open, /pwHost\.hidden = !accountChoice\.startsWith\('pw_'\);/);
  assert.match(MAIN, /await appApi\('\/api\/auth\/pin', \{ method: 'POST', body: \{ current: cur, new: np \} \}\);/);
  // The recovery note (server paths) only for the Owner with a PIN.
  assert.match(MAIN, /note\.hidden = !\(owner && pinSet\);/);
  // The old always-open form and the auto-lock sentence are gone from the markup.
  assert.match(HTML, /<div class="sec-body acct-form" id="sec-pin-form" hidden>/);
  assert.doesNotMatch(HTML, /Auto-locks after 10 min|Safe-Mode view/);
  for (const c of ['pin_change', 'pin_set', 'pin_remove', 'pw_set', 'pw_change', 'my_pin']) assert.ok(EN.acct[`opt_${c}`], c);
});

test('Restricted: the gear says Companions (with a people icon), not Settings', () => {
  const fn = MAIN.slice(MAIN.indexOf('function syncGearButton()'), MAIN.indexOf('function wireRecoveryUi()'));
  assert.match(fn, /const key = comp \? 'companions\.title' : 'nav\.settings';/);
  assert.match(fn, /railIcon\(comp \? RAIL_ICONS\.users : RAIL_ICONS\.gear\)/);
  assert.match(MAIN, /if \(dom\['unlock-btn'\]\) dom\['unlock-btn'\]\.classList\.toggle\('hidden', !state\.decoy\);\n  syncGearButton\(\);/);
});

// Every literal Settings target in the code must land where its content lives:
// an alias names the section an old tab *mostly* became, which is wrong for a
// group that moved somewhere else (the local viewer left Device for System).
const paneOf = (html, id) => {
  const start = html.indexOf(`id="spane-${id}"`);
  if (start < 0) return '';
  const next = html.indexOf('id="spane-', start + 10);
  return html.slice(start, next < 0 ? undefined : next);
};

test('the viewer\'s "Open settings" lands on the System pane\'s viewer group', () => {
  const src = read('js/viewer.js');
  const m = src.match(/'dispatch:open-settings',\s*\{\s*detail:\s*\{\s*tab:\s*'([^']+)'/);
  assert.ok(m, 'viewer.js sends dispatch:open-settings with a literal tab');
  const r = resolveSettingsTarget(m[1]);
  assert.equal(r.anchor, 'viewer');
  assert.match(paneOf(HTML, r.id), /data-anchor="viewer"/, `#spane-${r.id} holds data-anchor="viewer"`);
});

test('every literal settings target in the frontend and the bundled apps resolves to a section holding its anchor', () => {
  const files = [];
  const walk = (dir) => {
    for (const f of readdirSync(dir, { withFileTypes: true })) {
      const p = join(dir, f.name);
      if (f.isDirectory()) { if (f.name !== 'vendor' && f.name !== 'node_modules') walk(p); } else if (f.name.endsWith('.js')) files.push(p);
    }
  };
  walk(join(STATIC, 'js'));
  walk(join(HERE, '..', '..', 'apps'));
  const re = /(?:openSettingsTab|setSettingsTab|openTab|settings\.open)\(\s*'([a-z][\w-]*(?:#[\w:.-]+)?)'|'dispatch:open-settings',\s*\{\s*detail:\s*\{\s*tab:\s*'([^']+)'/g;
  const seen = [];
  for (const f of files) {
    for (const m of readFileSync(f, 'utf8').matchAll(re)) {
      const target = m[1] || m[2];
      const r = resolveSettingsTarget(target);
      seen.push(target);
      assert.ok(SECTION_IDS.includes(r.id), `${f}: '${target}' resolves to a section (got ${r.id})`);
      if (r.anchor && !r.anchor.startsWith('key:')) {
        const pane = paneOf(HTML, r.id);
        const inMarkup = pane.includes(`data-anchor="${r.anchor}"`) || pane.includes(`data-schema="${r.id}#${r.anchor}"`);
        const inRows = STATIC_ROWS.some((row) => row.section === r.id && row.anchor === r.anchor);
        assert.ok(inMarkup || inRows, `${f}: '${target}' -> #spane-${r.id} has anchor ${r.anchor}`);
      }
    }
  }
  assert.ok(seen.length >= 10, `found the callers (${seen.length})`);
});

// Deep links (settings-nav.js revealAnchor): the target opens, scrolls to the top
// and STAYS there while the section's late content lands above it, until the
// reader scrolls. Fake timers; jsdom has no layout, so positions are stubbed.
test('revealAnchor waits for a late target, opens its <details>, and keeps it at the top until the reader scrolls', async () => {
  const { createRequire } = await import('node:module');
  let jsdom = null;
  try { jsdom = createRequire(import.meta.url)('jsdom'); } catch { /* skip below */ }
  if (!jsdom) { if (process.env.CI_REQUIRE_DOM) assert.fail('jsdom is required'); return; }
  const win = new jsdom.JSDOM('<div id="pane"><div id="above"></div><details id="adv"><summary>Advanced</summary><div id="slot"></div></details></div>').window;
  globalThis.Event = win.Event;
  const { revealAnchor } = await import(`../static/js/settings-nav.js?t=${Math.random()}`);
  const doc = win.document;
  const pane = doc.getElementById('pane');
  // A fake clock: advance(ms) runs every timer due by then, in time order.
  let now = 0;
  let seq = 0;
  const due = new Map();
  const timers = { setTimeout: (fn, ms) => { seq += 1; due.set(seq, { fn, at: now + ms }); return seq; }, clearTimeout: (h) => { due.delete(h); } };
  const advance = (ms) => {
    const end = now + ms;
    for (;;) {
      const next = [...due.entries()].filter(([, v]) => v.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
      if (!next) break;
      due.delete(next[0]);
      now = next[1].at;
      next[1].fn();
    }
    now = end;
  };
  pane.getBoundingClientRect = () => ({ top: 100 });
  let top = 100;     // the target's top; 100 = at the pane's top
  let scrolls = 0;
  revealAnchor(pane, 'viewer', { timers, settleMs: 600, step: 150 });
  advance(300);      // not there yet: it keeps looking
  assert.equal(scrolls, 0);
  const target = doc.createElement('section');
  target.dataset.anchor = 'viewer';
  target.getBoundingClientRect = () => ({ top });
  target.scrollIntoView = () => { scrolls += 1; top = 100; };
  doc.getElementById('slot').append(target);
  advance(100);
  assert.equal(doc.getElementById('adv').open, true, 'the Advanced around it opened');
  assert.equal(scrolls, 1, 'scrolled once when found');
  // The backups card loads above it and pushes it down: it is put back.
  top = 900;
  advance(150);
  assert.equal(scrolls, 2, 'followed the shift');
  advance(150);
  assert.equal(scrolls, 2, 'no scroll when it is already in place');
  // The reader scrolls: the follow ends.
  top = 1500;
  pane.dispatchEvent(new win.Event('wheel'));
  advance(1000);
  assert.equal(scrolls, 2, 'a wheel stops it');
  // Without the reader, the follow still ends on its own once the section settled.
  const t2 = doc.createElement('div');
  t2.dataset.anchor = 'other';
  let n2 = 0;
  t2.getBoundingClientRect = () => ({ top: 700 });   // never settles
  t2.scrollIntoView = () => { n2 += 1; };
  pane.append(t2);
  revealAnchor(pane, 'other', { timers, settleMs: 600, step: 150 });
  advance(5000);
  assert.equal(n2, 1 + 4, 'found once, then at most settleMs/step corrections');
});

test('app.css: plain notes inside Settings cards read at the card size, not the fluid body size', () => {
  const css = read('app.css');
  assert.match(css, /#botmanager \.settings-pane :is\(p\.muted, \.bkp-check\) \{ font-size: 13px;/);
});
