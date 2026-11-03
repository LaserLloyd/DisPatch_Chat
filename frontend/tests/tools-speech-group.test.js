// One rail tool per ADD-ON, with a tab per tool (tools.js).
//
// The voice add-on registers three builtins at runtime (voice-cloning,
// voice-builtin, voice-lab) and three rail tiles for them. The rail must show
// ONE entry, "Speech", whose tabs are those three — grouped by the OWNER the
// tiles were registered with, never by a hardcoded id list.
//
// No DOM needed: the rail is inspected through railEntries()/groupFor(), and
// getElementById answers null so renderToolRail() is a no-op.

import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { domSkip } from './_require-dom.js';

const require = createRequire(import.meta.url);
let jsdom = null;
try { jsdom = require('jsdom'); } catch { /* not installed — tests skip */ }
const dom = { skip: domSkip(jsdom ? false : 'jsdom is not installed (see markdown-behaviour.test.js)') };

const ROWS = [
  { id: 'deepseek-harness', title: 'DeepSeek Harness', icon: '🐋', kind: 'builtin',
    enabled: true, builtin_feature: 'harness', available: true },
  { id: 'voice-cloning', title: 'Voice cloning', icon: '🎙️', kind: 'builtin',
    enabled: true, builtin_feature: 'voice_cloning', available: true,
    owner: 'dispatch-voice', owner_title: 'DisPatch Voice' },
  { id: 'voice-builtin', title: 'Built-in voices', icon: '🔊', kind: 'builtin',
    enabled: true, builtin_feature: 'voice_builtin', available: true,
    owner: 'dispatch-voice', owner_title: 'DisPatch Voice' },
  { id: 'voice-lab', title: 'Voice lab', icon: '🧪', kind: 'builtin',
    enabled: true, builtin_feature: 'voice_lab', available: true,
    owner: 'dispatch-voice', owner_title: 'DisPatch Voice' },
];

// A bare document: i18n.js boots on import (it sets <html lang>), and the
// rail's lookups answer null so renderToolRail() is a no-op.
const win = new jsdom.JSDOM('<html><body></body></html>', { url: 'http://127.0.0.1:8765/' }).window;
globalThis.window = win;
globalThis.document = win.document;
globalThis.location = win.location;
globalThis.history = win.history;
globalThis.CSS = win.CSS || { escape: (s) => s };
globalThis.fetch = async () => ({
  ok: true, status: 200, statusText: '200',
  headers: new Headers(), json: async () => ({ tools: ROWS, path: '' }),
});

const mod = await import(`../static/js/tools.js?v=grp${Date.now()}`);
const opened = [];

function wire() {
  mod.wireTools({
    state: { selectedBotId: 'main', activeThreadId: 't-1' },
    openers: { harness: () => opened.push('harness') },
    builtinOn: () => true,
    isMobile: () => false,
    renderSidebar: () => {},
    groupState: () => '',
    navigate: () => {},
    toast: () => {},
  });
}

test('the three voice tools become ONE "Speech" rail entry with three tabs', { skip: dom.skip }, async () => {
  wire();
  mod.registerExtTiles('dispatch-voice', [
    { id: 'voice-cloning', feature: 'voice_cloning', nameKey: 'vtools.cloning_name', open: (id) => opened.push(id) },
    { id: 'voice-builtin', feature: 'voice_builtin', nameKey: 'vtools.builtin_name', open: (id) => opened.push(id) },
    { id: 'voice-lab', feature: 'voice_lab', nameKey: 'vtools.lab_name', open: (id) => opened.push(id) },
  ]);
  await mod.loadTools();

  const ids = mod.railEntries().map((x) => x.id);
  assert.ok(ids.includes('group:dispatch-voice'), `one entry for the add-on: ${ids}`);
  for (const tid of ['voice-cloning', 'voice-builtin', 'voice-lab']) {
    assert.ok(!ids.includes(tid), `${tid} must not be its own rail row`);
  }

  const g = mod.findTool('group:dispatch-voice');
  assert.ok(g, 'the group is a tool the rail can select');
  assert.match(g.title, /speech/i, 'a voice add-on reads as Speech');
  assert.deepEqual(g.tabs.map((x) => x.id), ['voice-cloning', 'voice-builtin', 'voice-lab']);
  assert.ok(mod.isToolId('group:dispatch-voice'));
});

test('the group remembers the tab you were on; a tab id resolves to its group', { skip: dom.skip }, async () => {
  wire();
  await mod.loadTools();
  assert.equal(mod.groupFor('voice-lab').id, 'group:dispatch-voice');
  assert.equal(mod.groupFor('deepseek-harness'), null, 'a core builtin has no group');

  opened.length = 0;
  mod.openTool('group:dispatch-voice');
  assert.deepEqual(opened, ['voice-cloning'], 'first open lands on the first tab');
  opened.length = 0;
  mod.openTool('voice-lab');
  assert.deepEqual(opened, ['voice-lab'], 'a tab opens its own tool');
  opened.length = 0;
  mod.openTool('group:dispatch-voice');
  assert.deepEqual(opened, ['voice-lab'], 'coming back lands on that tab');
});

test('one enabled tool is not folded (a one-tab strip is noise)', { skip: dom.skip }, async () => {
  mod.registerExtTiles('quiet-addon', [
    { id: 'quiet-one', feature: 'quiet_one', nameKey: 'x', open: () => {} },
    { id: 'quiet-two', feature: 'quiet_two', nameKey: 'x', open: () => {} },
  ]);
  ROWS.push(
    { id: 'quiet-one', title: 'One', icon: '', kind: 'builtin', enabled: true,
      builtin_feature: 'quiet_one', available: true, owner: 'quiet-addon', owner_title: 'Quiet' },
    { id: 'quiet-two', title: 'Two', icon: '', kind: 'builtin', enabled: false,
      builtin_feature: 'quiet_two', available: true, owner: 'quiet-addon', owner_title: 'Quiet' },
  );
  await mod.loadTools();
  assert.equal(mod.findTool('group:quiet-addon'), null);
  assert.ok(mod.findTool('quiet-one'), 'the one that is on keeps its own tile');
});
