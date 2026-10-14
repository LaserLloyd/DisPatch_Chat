// Settings -> Bots -> Personality (js/personality.js): preset picker, the
// system-prompt editor, Save / Revert / Reset, the Safe-Mode note and the
// agent-bot pointer. Driven through the real api client against a stubbed
// fetch, with real English strings.

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { domSkip } from './_require-dom.js';

const HERE = dirname(fileURLToPath(import.meta.url));
const STATIC = join(HERE, '..', 'static');
const HTML = readFileSync(join(STATIC, 'index.html'), 'utf8');
const MAIN = readFileSync(join(STATIC, 'js', 'main.js'), 'utf8');
const BOTS_EDIT = readFileSync(join(STATIC, 'js', 'bots-edit.js'), 'utf8');
const LLM = readFileSync(join(STATIC, 'js', 'llm.js'), 'utf8');
const SW = readFileSync(join(STATIC, 'sw.js'), 'utf8');
const EN = JSON.parse(readFileSync(join(STATIC, 'locales', 'en.json'), 'utf8'));

const require = createRequire(import.meta.url);
let jsdom = null;
try { jsdom = require('jsdom'); } catch { /* not installed - DOM tests skip */ }
const dom = { skip: domSkip(jsdom ? false : 'jsdom is not installed') };

const PRESETS = {
  presets: [
    { id: 'default', title: 'DisPatch default', description: 'Shipped.', text: 'You are {name}, default.' },
    { id: 'sleek', title: 'Sleek assistant', description: 'Spare.', text: 'You are {name}, sleek, for {owner}.' },
    { id: 'contrarian', title: 'Contrarian', description: 'Disagrees.', text: 'You are {name}, contrarian, for {owner}.' },
  ],
  default_id: 'default', max_chars: 8000,
};

let calls = [];
let routes = {};

async function setup(extra = {}) {
  const win = new jsdom.JSDOM(HTML, { url: 'http://127.0.0.1:8765/', runScripts: 'outside-only' }).window;
  const put = (k, v) => Object.defineProperty(globalThis, k, { value: v, configurable: true, writable: true });
  put('window', win);
  put('document', win.document);
  put('location', win.location);
  put('navigator', win.navigator);
  put('localStorage', win.localStorage);
  put('Node', win.Node);
  put('HTMLElement', win.HTMLElement);
  put('HTMLInputElement', win.HTMLInputElement);
  calls = [];
  routes = {
    'GET /static/locales/en.json': () => [200, EN],
    'GET /api/bots/personas': () => [200, PRESETS],
    ...extra,
  };
  globalThis.fetch = async (u, opts = {}) => {
    const key = `${(opts.method || 'GET').toUpperCase()} ${u}`;
    calls.push({ key, body: opts.body ? JSON.parse(opts.body) : null });
    const h = routes[key];
    const [status, body] = h ? h(opts) : [404, { detail: 'Not Found' }];
    return {
      ok: status >= 200 && status < 300, status, statusText: String(status),
      headers: new win.Headers(), json: async () => body,
    };
  };
  const i18n = await import('../static/js/i18n.js?v=4');
  await i18n.init('en');
  calls.length = 0;
  const pmod = await import('../static/js/personality.js?v=1');
  pmod._reset();
  const mod = await import(`../static/js/bots-edit.js?t=${Math.random()}`);
  const rec = { toasts: [], patched: [] };
  const ctx = {
    toast: (m, err) => rec.toasts.push([m, !!err]),
    confirm: async () => true,
    openAi: () => {},
    onPatched: (id, row) => rec.patched.push([id, row]),
    onRemoved: () => {},
    onAdded: () => {},
  };
  return { win, mod, pmod, rec, ctx };
}

const tick = async () => { for (let i = 0; i < 6; i++) await new Promise((r) => setTimeout(r, 0)); };
const fire = (win, node, type = 'click') => node.dispatchEvent(new win.Event(type, { bubbles: true }));

const ATLAS = {
  id: 'atlas', name: 'Atlas', backend: 'direct', tools: true, connected: true, safe: true,
  persona_kind: 'direct', persona: 'You are {name}, default.', persona_custom: false,
};

test('matchPreset: equal text (trimmed) is that preset, anything else is custom', async () => {
  const { matchPreset, CUSTOM } = await import('../static/js/personality.js?v=1');
  assert.equal(matchPreset(PRESETS.presets, '  You are {name}, sleek, for {owner}.\n'), 'sleek');
  assert.equal(matchPreset(PRESETS.presets, 'You are {name}, sleek, for {owner}!'), CUSTOM);
  assert.equal(matchPreset([], 'x'), CUSTOM);
});

test('a direct bot gets the button and a panel; picking a preset fills the editor, editing makes it Custom', { skip: dom.skip }, async () => {
  const { mod, ctx, win } = await setup();
  const c = mod.rowControls({ ...ATLAS }, ctx);
  assert.ok(c.persona && c.personaPanel);
  assert.ok(c.personaPanel.classList.contains('hidden'));
  fire(win, c.persona);
  await tick();
  const p = c.personaPanel;
  assert.ok(!p.classList.contains('hidden'));
  const f = (k) => p.querySelector(`[data-bm="${k}"]`);
  const sel = f('persona-preset');
  assert.deepEqual([...sel.options].map((o) => o.value), ['default', 'sleek', 'contrarian', 'custom']);
  assert.equal(sel.value, 'default');
  assert.equal(f('persona-text').value, 'You are {name}, default.');
  assert.equal(f('persona-save').disabled, true);           // nothing changed yet
  sel.value = 'contrarian';
  fire(win, sel, 'change');
  assert.equal(f('persona-text').value, 'You are {name}, contrarian, for {owner}.');
  assert.equal(f('persona-desc').textContent, 'Disagrees.');
  assert.equal(f('persona-save').disabled, false);
  f('persona-text').value += ' Extra.';
  fire(win, f('persona-text'), 'input');
  assert.equal(sel.value, 'custom');
  assert.match(f('persona-count').textContent, /^\d+ \/ 8000$/);
  // Reset to preset goes back to the last preset picked (Contrarian).
  fire(win, f('persona-reset'));
  assert.equal(f('persona-text').value, 'You are {name}, contrarian, for {owner}.');
  assert.equal(sel.value, 'contrarian');
  // Revert goes back to what is saved.
  fire(win, f('persona-revert'));
  assert.equal(f('persona-text').value, 'You are {name}, default.');
  assert.equal(f('persona-save').disabled, true);
});

test('Save PATCHes {persona}, reports the row, and the panel stays open across a re-render', { skip: dom.skip }, async () => {
  const saved = 'You are {name}, contrarian, for {owner}.';
  const { mod, ctx, rec, win } = await setup({
    'PATCH /api/bots/atlas': () => [200, { ...ATLAS, persona: saved, persona_custom: true }],
  });
  const c = mod.rowControls({ ...ATLAS }, ctx);
  fire(win, c.persona);
  await tick();
  const f = (k) => c.personaPanel.querySelector(`[data-bm="${k}"]`);
  f('persona-preset').value = 'contrarian';
  fire(win, f('persona-preset'), 'change');
  fire(win, f('persona-save'));
  await tick();
  const patch = calls.find((x) => x.key === 'PATCH /api/bots/atlas');
  assert.deepEqual(patch.body, { persona: saved });
  assert.equal(rec.patched[0][0], 'atlas');
  assert.equal(rec.patched[0][1].persona_custom, true);
  assert.match(rec.toasts.at(-1)[0], /Atlas's personality saved/);
  assert.equal(f('persona-save').disabled, true);
  // main.js rebuilds the row after onPatched: the new panel opens by itself.
  const again = mod.rowControls({ ...ATLAS, persona: saved, persona_custom: true }, ctx);
  assert.ok(!again.personaPanel.classList.contains('hidden'));
  await tick();
  assert.equal(again.personaPanel.querySelector('[data-bm="persona-preset"]').value, 'contrarian');
});

test('a refused Save keeps the text and says why', { skip: dom.skip }, async () => {
  const { mod, ctx, rec, win } = await setup({
    'PATCH /api/bots/atlas': () => [403, { detail: 'Unlock for full access' }],
  });
  const c = mod.rowControls({ ...ATLAS }, ctx);
  fire(win, c.persona);
  await tick();
  const f = (k) => c.personaPanel.querySelector(`[data-bm="${k}"]`);
  f('persona-text').value = 'Mine';
  fire(win, f('persona-text'), 'input');
  fire(win, f('persona-save'));
  await tick();
  assert.equal(f('persona-text').value, 'Mine');
  assert.equal(rec.patched.length, 0);
  assert.match(rec.toasts.at(-1)[0], /Could not save: Unlock for full access/);
  assert.equal(rec.toasts.at(-1)[1], true);
});

test('safe bots carry the family-device note; others do not', { skip: dom.skip }, async () => {
  const { mod, ctx, win } = await setup();
  for (const [bot, want] of [[{ ...ATLAS }, true], [{ ...ATLAS, id: 'sage', safe: false }, false]]) {
    const c = mod.rowControls(bot, ctx);
    fire(win, c.persona);
    await tick();
    const note = c.personaPanel.querySelector('[data-bm="persona-safe-note"]');
    assert.equal(!!note, want, bot.id);
    if (note) assert.equal(note.textContent, EN.persona.safe_note);
  }
});

test('an agent bot gets a pointer to its workspace and no editor; an app bot gets nothing', { skip: dom.skip }, async () => {
  const { mod, ctx, win } = await setup();
  const agent = mod.rowControls({ id: 'main', name: 'Bits', backend: 'agent', persona_kind: 'none' }, ctx);
  fire(win, agent.persona);
  await tick();
  assert.ok(agent.personaPanel.querySelector('[data-bm="persona-agent-note"]'));
  assert.equal(agent.personaPanel.querySelector('textarea'), null);
  assert.ok(!calls.some((x) => x.key === 'GET /api/bots/personas'));
  const app = mod.rowControls({ id: 'jobboard', name: 'Jobs', backend: 'app', persona_kind: 'none' }, ctx);
  assert.equal(app.persona, null);
  assert.equal(app.personaPanel, null);
});

test('a legacy-API bot edits its system prompt with the same panel', { skip: dom.skip }, async () => {
  const { mod, ctx, win } = await setup();
  const c = mod.rowControls({ id: 'llm-x', name: 'X', backend: 'legacy-api', persona_kind: 'legacy-api', persona: '' }, ctx);
  fire(win, c.persona);
  await tick();
  const ta = c.personaPanel.querySelector('textarea');
  assert.ok(ta);
  assert.equal(ta.getAttribute('maxlength'), '8000');
  assert.equal(c.personaPanel.querySelector('[data-bm="persona-preset"]').value, 'custom');
});

test('wiring: main.js places the slots, bots-edit imports the module, Connect reads the same field, sw caches it', () => {
  assert.match(MAIN, /ctl && ctl\.persona,/);
  assert.match(MAIN, /ctl && ctl\.personaPanel,/);
  assert.match(MAIN, /'persona', 'persona_custom', 'persona_kind'/);
  assert.match(BOTS_EDIT, /from '\.\/personality\.js\?v=\d+'/);
  assert.match(LLM, /target\.persona/);
  assert.match(SW, /'\/static\/js\/personality\.js'/);
  assert.match(HTML, /id="llm-system"[^>]*maxlength="8000"/);
  for (const k of ['title', 'preset', 'custom', 'revert', 'reset', 'saved', 'safe_note', 'agent_note', 'hint']) {
    assert.ok(EN.persona[k], k);
  }
});

test('every language has the persona strings, with the same placeholders', () => {
  for (const lang of ['ar', 'de', 'es', 'fr', 'ja', 'pt', 'zh']) {
    const d = JSON.parse(readFileSync(join(STATIC, 'locales', `${lang}.json`), 'utf8'));
    assert.deepEqual(Object.keys(d.persona || {}), Object.keys(EN.persona), lang);
    const ph = (s) => [...s.matchAll(/\{(\w+)\}/g)].map((m) => m[1]).sort();
    for (const [k, v] of Object.entries(EN.persona)) {
      assert.deepEqual(ph(d.persona[k]), ph(v), `${lang}.persona.${k}`);
    }
  }
});
