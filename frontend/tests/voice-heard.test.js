// What the listener did not hear of a spoken reply (js/voice-heard.js, 2.2.0):
// the split at metadata.voice_heard.upto (never inside markup), the "cut off"
// note for a reply stored only up to the cut, and the DOM it draws.

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { domSkip } from './_require-dom.js';

const require = createRequire(import.meta.url);
let jsdom = null;
try { jsdom = require('jsdom'); } catch { /* not installed: DOM tests skip */ }
const dom = { skip: domSkip(jsdom ? false : 'jsdom is not installed') };

const load = async () => import(`../static/js/voice-heard.js?t=${Math.random()}`);
const HEARD = (upto, extra = {}) => ({ voice_heard: { upto, interrupted: true, heard: 'x', unheard: 'y', ...extra } });

test('voiceHeardParts: the split at upto; nothing for an ordinary row', async () => {
  const { voiceHeardParts } = await load();
  const text = 'You have two things today. The dentist at three. Then the school run.';
  assert.equal(voiceHeardParts(text, {}), null);
  assert.equal(voiceHeardParts(text, { voice_heard: { interrupted: false, upto: 10 } }), null);
  const at = text.indexOf('The dentist');
  assert.deepEqual(voiceHeardParts(text, HEARD(at)),
    { heard: 'You have two things today. ', unheard: 'The dentist at three. Then the school run.' });
  // Stored only up to the cut (an advisor / API partial row), or all of it heard.
  assert.deepEqual(voiceHeardParts('Partial reply', { voice_heard: { interrupted: true } }), { heard: 'Partial reply', cut: true });
  assert.deepEqual(voiceHeardParts(text, HEARD(text.length)), { heard: text, cut: true });
  assert.deepEqual(voiceHeardParts(text, HEARD(0)).heard, '');
});

test('safeSplit never leaves a fence, code span, bold or link open', async () => {
  const { safeSplit } = await load();
  const fence = 'Here it is:\n```\nprint(1)\nprint(2)\n```\nDone.';
  assert.equal(safeSplit(fence, fence.indexOf('print(2)')), fence.indexOf('```'));
  const code = 'Run `npm test now` please.';
  assert.equal(safeSplit(code, code.indexOf('test')), code.indexOf('`'));
  const bold = 'It is **really quite** late.';
  assert.equal(safeSplit(bold, bold.indexOf('quite')), bold.indexOf('**'));
  const link = 'See [the map here](https://example.com/m) now.';
  assert.equal(safeSplit(link, link.indexOf('here')), link.indexOf('['));
  assert.equal(safeSplit(link, link.indexOf('example')), link.indexOf('['));
  const plain = 'One. Two.';
  assert.equal(safeSplit(plain, 5), 5);
});

test('renderVoiceHeard draws the unheard part muted behind a titled mic-off mark', dom, async () => {
  const { JSDOM } = jsdom;
  const win = new JSDOM('<!doctype html><body></body>').window;
  const saved = globalThis.document;
  globalThis.document = win.document;
  try {
    const { renderVoiceHeard, MIC_OFF_ICON } = await load();
    const el = (tag, attrs = {}, kids = []) => {
      const n = win.document.createElement(tag);
      for (const [k, v] of Object.entries(attrs)) { if (k === 'class') n.className = v; else n.setAttribute(k, v); }
      for (const c of [].concat(kids)) if (c != null) n.append(c);
      return n;
    };
    const railIcon = (paths) => { const s = win.document.createElement('span'); s.dataset.paths = String(paths.length); return s; };
    const t = (k) => ({ 'msg.not_heard': 'Not heard in voice chat', 'msg.voice_cut': 'Cut off in voice chat' })[k] || k;
    const render = (md) => md.split(/\n\n/).map((p) => `<p>${p.trim()}</p>`).join('');
    const bubble = el('div');
    const text = 'You have two things today. The dentist at three.';
    assert.equal(renderVoiceHeard(bubble, text, {}, { render, el, railIcon, t }), false);
    assert.equal(renderVoiceHeard(bubble, text, HEARD(text.indexOf('The dentist')), { render, el, railIcon, t }), true);
    assert.equal(bubble.querySelector('.msg-heard').textContent, 'You have two things today.');
    const rest = bubble.querySelector('.msg-unheard');
    assert.match(rest.textContent, /The dentist at three\./);
    const mark = rest.querySelector('.msg-unheard-mark');
    assert.equal(mark.getAttribute('title'), 'Not heard in voice chat');
    assert.equal(mark.getAttribute('aria-label'), 'Not heard in voice chat');
    assert.equal(mark.parentElement.tagName, 'P', 'inline at the start of the first unheard paragraph');
    assert.equal(mark.firstElementChild.dataset.paths, String(MIC_OFF_ICON.length));
    const cut = el('div');
    renderVoiceHeard(cut, 'I was saying', { voice_heard: { interrupted: true } }, { render, el, railIcon, t });
    assert.match(cut.querySelector('.msg-voice-cut').textContent, /Cut off in voice chat/);
  } finally { globalThis.document = saved; }
});

test('main.js draws assistant rows through renderVoiceHeard; the strings are DisPatch\'s own, in every locale', () => {
  const src = readFileSync(new URL('../static/js/main.js', import.meta.url), 'utf8');
  assert.match(src, /import \{ renderVoiceHeard \} from '\.\/voice-heard\.js\?v=\d+';/);
  assert.match(src, /renderVoiceHeard\(bubble, msg\.content \|\| '', msg\.metadata,/);
  for (const lang of ['en', 'ar', 'de', 'es', 'fr', 'ja', 'pt', 'zh']) {
    const b = JSON.parse(readFileSync(new URL(`../static/locales/${lang}.json`, import.meta.url), 'utf8'));
    assert.ok(b.msg.not_heard && b.msg.voice_cut, lang);
  }
  const css = readFileSync(new URL('../static/app.css', import.meta.url), 'utf8');
  assert.match(css, /\.bubble \.msg-unheard \{[^}]*color: var\(--text-secondary\)/);
});
